#!/usr/bin/env python3
"""Live Subtitles service: system-audio STT and offline translation.

Captures the PipeWire *monitor* source of a sink (never a microphone, and no
loopback into the speakers), transcribes with faster-whisper offline and
translates with Argos Translate offline, then publishes the result to files
that the Omarchy shell plugin renders as subtitles.

Runtime files (~/.local/share/livesubtitles):
  state.json       enabled/status/language for the bar widget and overlay
  subtitle.txt     the current subtitle line (UTF-8, max 2 lines)
  control.fifo     command channel: toggle, start, stop, lang <code>, status
  models/          faster-whisper model cache

Settings (~/.config/livesubtitles/config.json): {"enabled": bool, "language": code}
"""

import json
import math
import os
import queue
import signal
import struct
import subprocess
import sys
import threading
import time

STATE_DIR = os.environ.get(
    "LS_STATE_DIR", os.path.expanduser("~/.local/share/livesubtitles")
)
CONFIG_DIR = os.environ.get("LS_CONFIG_DIR", os.path.expanduser("~/.config/livesubtitles"))
STATE_FILE = os.path.join(STATE_DIR, "state.json")
SUBTITLE_FILE = os.path.join(STATE_DIR, "subtitle.txt")
ORIGINAL_FILE = os.path.join(STATE_DIR, "original.txt")
FIFO_FILE = os.path.join(STATE_DIR, "control.fifo")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")

SAMPLE_RATE = 16000
FRAME_BYTES = SAMPLE_RATE * 2 // 10  # 100 ms of s16le mono
SILENCE_RMS = float(os.environ.get("LS_SILENCE_RMS", "220"))
NOISE_FLOOR = float(os.environ.get("LS_NOISE_FLOOR", "60"))
HANGOVER_FRAMES = 5  # 500 ms of silence closes an utterance
MAX_UTTERANCE_FRAMES = 80  # hard cap, 8 s — longer speech becomes sequential lines
PARTIAL_EVERY_FRAMES = 10  # default refresh cadence, overridden by speed profile
SUBTITLE_TTL = 4.0  # hide the line after this many seconds without updates
MAX_LINES = 2
MAX_LINE_CHARS = 160  # bound the overlay: keep only the tail of a long run
MAX_LINE_CHARS = 160  # bound the overlay: keep only the tail of a long run

# Speed profiles: how much CPU is traded for lower latency. "beam" is the
# whisper beam size (1 = greedy, 2-3 = better accuracy on noisy audio) and
# "partial_every" is how often interim lines refresh, in 100 ms frames.
SPEED_PROFILES = {
    "battery": {"beam": 1, "partial_every": 15, "threads": 2},
    "balanced": {"beam": 1, "partial_every": 10, "threads": 4},
    "turbo": {"beam": 1, "partial_every": 6, "threads": 8},
}

# Streaming (Gboard-style) engine: sherpa-onnx online zipformer decodes every
# 100 ms chunk as it arrives and keeps a live hypothesis — words appear in the
# subtitle almost as they are spoken, no tail re-transcription needed.
STREAM_MODEL_DIR = os.path.join(STATE_DIR, "models", "sherpa-streaming-en")
# Translation refresh throttle for the live hypothesis (seconds).
STREAM_TRANSLATE_EVERY = 0.25

LANGUAGES = {
    "en": "English", "ru": "Русский", "uk": "Українська", "es": "Español",
    "fr": "Français", "de": "Deutsch", "it": "Italiano", "pt": "Português",
    "pl": "Polski", "tr": "Türkçe", "ja": "日本語", "zh": "中文", "ko": "한국어",
}


def log(msg: str) -> None:
    print(f"[livesubtitles] {time.strftime('%H:%M:%S')} {msg}", flush=True)


def atomic_write(path: str, data: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(data)
    os.replace(tmp, path)


# ----------------------------------------------------------------- published

class State:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.data = {
            "enabled": False,
            "status": "off",  # off | listening | loading | error
            "detail": "",
            "language": "en",
            "show_original": False,
            "speed": "balanced",
            "opacity": 1.0,
            "pid": os.getpid(),
        }
        self._flush()

    def update(self, **fields) -> None:
        with self._lock:
            self.data.update(fields)
            self._flush()

    def get(self) -> dict:
        with self._lock:
            return dict(self.data)

    def _flush(self) -> None:
        atomic_write(STATE_FILE, json.dumps(self.data))


state = State()


def publish_subtitle(text: str) -> None:
    global last_subtitle_at
    last_subtitle_at = time.monotonic()
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    atomic_write(SUBTITLE_FILE, "\n".join(lines[-MAX_LINES:]))


def _tail(text: str, limit: int = MAX_LINE_CHARS) -> str:
    """Keep only the last `limit` chars of an ever-growing line."""
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[len(text) - limit :]
    # Don't start mid-word: drop the partial word at the cut.
    return cut[cut.find(" ") + 1 :].strip() if " " in cut else cut


def publish_original(text: str) -> None:
    global last_subtitle_at
    last_subtitle_at = time.monotonic()
    atomic_write(ORIGINAL_FILE, text.strip())


last_subtitle_at = 0.0


def load_config() -> dict:
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        raw = {}
    language = str(raw.get("language", "en"))
    speed = str(raw.get("speed", "balanced"))
    return {
        "enabled": bool(raw.get("enabled", False)),
        "language": language if language in LANGUAGES else "en",
        "source": str(raw.get("source", "auto")),  # "auto" or a language code
        "show_original": bool(raw.get("show_original", False)),
        "speed": speed if speed in SPEED_PROFILES else "balanced",
        "opacity": min(1.0, max(0.2, float(raw.get("opacity", 1.0)))),
    }


def save_config(cfg: dict) -> None:
    atomic_write(CONFIG_FILE, json.dumps(
        {
            "enabled": cfg["enabled"],
            "language": cfg["language"],
            "source": cfg["source"],
            "show_original": cfg["show_original"],
            "speed": cfg["speed"],
            "opacity": cfg["opacity"],
        },
        indent=2,
    ))


# ------------------------------------------------------------------- capture

def resolve_monitor_source() -> str | None:
    """The monitor source paired with the default sink, e.g. sink.monitor."""
    try:
        sink = subprocess.run(
            ["pactl", "get-default-sink"], capture_output=True, text=True, timeout=5
        ).stdout.strip()
        if not sink:
            return None
        listing = subprocess.run(
            ["pactl", "list", "short", "sources"], capture_output=True, text=True, timeout=5
        ).stdout
        monitor = sink + ".monitor"
        for line in listing.splitlines():
            parts = line.split("\t")
            if len(parts) > 1 and parts[1] == monitor:
                return monitor
        # PipeWire also resolves this alias to the default sink's monitor.
        return "@DEFAULT_MONITOR@"
    except (OSError, subprocess.TimeoutExpired) as e:
        log(f"pactl failed: {e}")
        return None


def rms_s16(chunk: bytes) -> float:
    count = len(chunk) // 2
    if not count:
        return 0.0
    samples = struct.unpack(f"<{count}h", chunk)
    return math.sqrt(sum(s * s for s in samples) / count)


class Capture(threading.Thread):
    """Reads 100 ms frames from parec and hands utterances to the worker."""

    def __init__(
        self, sink: "queue.Queue[tuple]", partial_every: int, stream_live: bool = False
    ) -> None:
        super().__init__(daemon=True, name="Capture")
        self.sink = sink
        self.partial_every = max(4, int(partial_every))
        # stream_live: feed every 100 ms frame to the streaming engine so the
        # hypothesis updates while the person is still speaking.
        self.stream_live = stream_live
        self.halt = threading.Event()
        self.proc: subprocess.Popen | None = None
        self.seen_loud = False

    def stop(self) -> None:
        self.halt.set()
        proc = self.proc
        if proc and proc.poll() is None:
            proc.terminate()

    def run(self) -> None:
        while not self.halt.is_set():
            source = resolve_monitor_source()
            if source is None:
                state.update(status="error", detail="no audio output to capture")
                self.halt.wait(3)
                continue
            cmd = [
                "parec", "-d", source,
                "--format=s16le", "--rate=16000", "--channels=1",
            ]
            try:
                self.proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
                )
            except OSError as e:
                state.update(status="error", detail=f"parec: {e}")
                self.halt.wait(3)
                continue
            log(f"capturing {source}")
            state.update(status="listening", detail=source)
            self._pump()
            if self.proc.poll() is None:
                self.proc.terminate()
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            self.proc = None
            if not self.halt.is_set():
                state.update(status="error", detail="capture dropped, retrying")
                self.halt.wait(2)

    def _pump(self) -> None:
        # tail: speech audio not yet handed to whisper at all. Both interim
        # refreshes and the pause-final pass transcribe ONLY this tail —
        # ~0.6-1 s of audio, constant cost — so transcription always outruns
        # speech and no backlog ever builds up. The pipeline accumulates the
        # confirmed text in last_final/last_original instead.
        tail: list[bytes] = []
        speech = 0
        hangover = 0
        since_partial = 0
        stdout = self.proc.stdout
        while not self.halt.is_set():
            frame = stdout.read(FRAME_BYTES)
            if not frame:
                return
            while len(frame) < FRAME_BYTES:
                more = stdout.read(FRAME_BYTES - len(frame))
                if not more:
                    return
                frame += more
            level = rms_s16(frame)
            if level <= NOISE_FLOOR and not self.seen_loud:
                continue  # digital silence before any real audio
            self.seen_loud = True
            if level > SILENCE_RMS:
                tail.append(frame)
                speech += 1
                hangover = 0
                since_partial += 1
                if self.stream_live:
                    # Gboard-style: every speech frame goes to the streaming
                    # decoder; whisper is not involved while speaking.
                    self.sink.put(("stream", frame))
                    continue
                # Interim refresh while the sentence is still being spoken:
                # transcribe only the new tail (~1 s, cheap), the pipeline
                # appends the confirmed words to the growing interim line.
                if since_partial >= self.partial_every or speech >= MAX_UTTERANCE_FRAMES:
                    self.sink.put(("chunk", b"".join(tail), True))
                    tail, since_partial = [], 0
                    speech = min(speech, MAX_UTTERANCE_FRAMES)
            elif speech:
                hangover += 1
                tail.append(frame)
                if hangover >= HANGOVER_FRAMES:
                    # Pause: confirm the last tail only — never re-transcribe
                    # the whole utterance (that pass took seconds and stalled
                    # the pipeline behind live speech).
                    self.sink.put(("chunk", b"".join(tail), False))
                    tail, speech, hangover, since_partial = [], 0, 0, 0


# ------------------------------------------------------------------ pipeline

class TranslationWorker(threading.Thread):
    """Translates the newest submitted line OFF the pipeline thread: the
    original goes out immediately, the translation lands whenever Argos is
    done — it never blocks the live hypothesis."""

    def __init__(self, translator: "Translator", pipeline: "Pipeline") -> None:
        super().__init__(daemon=True, name="Translate")
        self.translator = translator
        self.pipeline = pipeline
        self._cv = threading.Condition()
        self._job: tuple[str, str, str] | None = None

    def submit(self, text: str, source: str, target: str) -> None:
        with self._cv:
            self._job = (text, source, target)
            self._cv.notify()

    def run(self) -> None:
        while True:
            with self._cv:
                while self._job is None:
                    self._cv.wait()
                text, source, target = self._job
                self._job = None
            try:
                out = self.translator.translate(text, source, target)
            except Exception as e:
                log(f"translate failed ({source}->{target}): {e}")
                out = text  # never lose the transcript to a missing model
            with self.pipeline.cfg_lock:
                if not self.pipeline.cfg["enabled"]:
                    continue
            publish_subtitle(out)


class StreamingASR:
    """sherpa-onnx online zipformer: decode audio as it arrives, keep a live
    hypothesis. English-only, much lower latency than whisper partials."""

    def __init__(self) -> None:
        self.recognizer = None
        self.stream = None
        self.tail_words = ""  # hypothesis text not yet confirmed by a pause
        self.spoke = False  # produced at least one word this utterance
        self._lock = threading.Lock()

    def available(self) -> bool:
        needed = ("encoder.onnx", "decoder.onnx", "joiner.onnx", "tokens.txt")
        return os.path.isdir(STREAM_MODEL_DIR) and all(
            os.path.exists(os.path.join(STREAM_MODEL_DIR, f)) for f in needed
        )

    def ensure(self) -> bool:
        with self._lock:
            if self.stream is not None:
                return True
            if not self.available():
                return False
            import sherpa_onnx

            log("loading sherpa-onnx streaming zipformer")
            state.update(status="loading", detail="streaming zipformer")
            d = STREAM_MODEL_DIR
            self.recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
                tokens=os.path.join(d, "tokens.txt"),
                encoder=os.path.join(d, "encoder.onnx"),
                decoder=os.path.join(d, "decoder.onnx"),
                joiner=os.path.join(d, "joiner.onnx"),
                decoding_method="modified_beam_search",
            )
            self.stream = self.recognizer.create_stream()
            log("streaming zipformer ready")
        return True

    def reset(self) -> None:
        with self._lock:
            self.tail_words = ""
            self.spoke = False
            if self.recognizer is not None and self.stream is not None:
                self.stream = self.recognizer.create_stream()

    def saw_speech(self) -> bool:
        """True if the streaming engine produced words for the current line —
        the whisper final pass then has nothing to add."""
        return self.spoke

    def feed(self, pcm: bytes) -> str:
        """Feed a 100 ms s16le frame, return the current hypothesis text."""
        import numpy as np

        with self._lock:
            if self.stream is None:
                return self.tail_words
            samples = np.frombuffer(pcm, dtype=np.int16).astype("float32") / 32768.0
            self.stream.accept_waveform(SAMPLE_RATE, samples)
            while self.recognizer.is_ready(self.stream):
                self.recognizer.decode_stream(self.stream)
            text = self.recognizer.get_result(self.stream).strip()
            if text:
                self.tail_words = text
                self.spoke = True
            return self.tail_words


class Transcriber:
    def __init__(self) -> None:
        self._model = None
        self._lock = threading.Lock()
        self._lang_cache: str | None = None  # pinned after the first detection

    def model(self, threads: int):
        with self._lock:
            if self._model is None:
                from faster_whisper import WhisperModel

                size = os.environ.get("LS_WHISPER_MODEL", "small")
                log(f"loading faster-whisper '{size}' (offline, no network)")
                state.update(status="loading", detail=f"whisper {size}")
                self._model = WhisperModel(
                    size,
                    device=os.environ.get("LS_WHISPER_DEVICE", "auto"),
                    compute_type=os.environ.get("LS_WHISPER_COMPUTE", "auto"),
                    cpu_threads=threads,
                    download_root=os.path.join(STATE_DIR, "models"),
                )
                log(f"whisper ready (cpu_threads={threads})")
            return self._model

    def reset_language(self) -> None:
        """Forget the pinned language so the next utterance re-detects it."""
        self._lang_cache = None

    def __call__(
        self, pcm: bytes, forced_lang: str | None, beam: int, threads: int
    ) -> tuple[str, str]:
        import numpy as np

        # Language detection costs an extra encode pass per call; once the
        # session's language is known, pin it for the rest of the run.
        lang = forced_lang or self._lang_cache
        audio = np.frombuffer(pcm, dtype=np.int16).astype("float32") / 32768.0
        segments, info = self.model(threads).transcribe(
            audio,
            language=lang,
            beam_size=beam,
            # Feed only speech frames from our RMS VAD — whisper's internal
            # silero VAD would re-scan the whole window and burn CPU for the
            # same result.
            vad_filter=False,
            condition_on_previous_text=False,
        )
        text = " ".join(s.text.strip() for s in segments if s.text.strip())
        detected = info.language or "en"
        if not forced_lang and self._lang_cache is None:
            self._lang_cache = detected
            log(f"detected source language: {detected}")
        return text, detected


class Translator:
    """Argos Translate pairs, cached per (source, target)."""

    def __init__(self) -> None:
        self._pairs: dict[tuple[str, str], object] = {}
        self._lock = threading.Lock()

    def translate(self, text: str, source: str, target: str) -> str:
        if source == target:
            return text
        key = (source, target)
        with self._lock:
            pair = self._pairs.get(key)
            if pair is None:
                import argostranslate.translate

                installed = argostranslate.translate.get_installed_languages()
                src = next((l for l in installed if l.code == source), None)
                dst = next((l for l in installed if l.code == target), None)
                if src is None or dst is None:
                    raise RuntimeError(f"missing Argos pair {source}->{target}")
                pair = src.get_translation(dst)
                if pair is None:
                    raise RuntimeError(f"no Argos path {source}->{target}")
                self._pairs[key] = pair
            return pair.translate(text)


class Pipeline(threading.Thread):
    def __init__(self, cfg: dict, cfg_lock: threading.Lock) -> None:
        super().__init__(daemon=True, name="Pipeline")
        self.cfg = cfg
        self.cfg_lock = cfg_lock
        # Items: ("stream", frame) live 100 ms frame for the streaming engine,
        # ("chunk", pcm, partial) whisper partial/final chunks.
        self.queue: "queue.Queue[tuple]" = queue.Queue(maxsize=1200)
        self.transcriber = Transcriber()
        self.streaming = StreamingASR()
        self.translator = Translator()
        self.tr_worker = TranslationWorker(self.translator, self)
        self.tr_worker.start()
        self.last_final = ""  # finalized text of the current utterance
        self.last_original = ""  # original text accumulated for the same span
        self._last_tr = 0.0  # last translation publish of the live hypothesis

    def use_streaming(self) -> bool:
        return self.streaming.available()

    def _publish_live(self, original: str, target: str, source: str) -> None:
        """Publish the live streaming hypothesis: original immediately,
        translation off-thread (Argos on every 100 ms tick would burn CPU)."""
        original = _tail(original)
        publish_original(original)
        now = time.monotonic()
        if now - self._last_tr < STREAM_TRANSLATE_EVERY:
            return
        self._last_tr = now
        self.tr_worker.submit(original, source, target)

    def run(self) -> None:
        while True:
            item = self.queue.get()
            try:
                if item[0] == "stream":
                    _, frame = item
                    with self.cfg_lock:
                        if not self.cfg["enabled"]:
                            self.streaming.reset()
                            continue
                        target = self.cfg["language"]
                        source_cfg = self.cfg["source"]
                    # The streaming model is English-only: run it only when the
                    # source language is explicitly "en". With "auto" (or any
                    # other source) live frames are ignored — whisper handles
                    # those utterances and detects the language itself.
                    if source_cfg != "en":
                        continue
                    if not self.streaming.ensure():
                        # Model not installed — ignore live frames (the capture
                        # falls back to whisper chunks on its own).
                        continue
                    hyp = self.streaming.feed(frame)
                    if hyp:
                        self._publish_live(hyp, target, source)
                    continue

                _, pcm, partial = item
                with self.cfg_lock:
                    if not self.cfg["enabled"]:
                        self.streaming.reset()
                        self.last_final = ""
                        self.last_original = ""
                        continue
                # When the streaming engine is live (explicit en source), it
                # already showed the words as they were spoken; a pause just
                # closes the line — no heavy whisper pass that would freeze
                # the pipeline for seconds.
                if not partial and self.streaming.saw_speech():
                    self.streaming.reset()
                    self.last_final = ""
                    self.last_original = ""
                    self._last_tr = 0.0
                    continue
                if not partial:
                    self.streaming.reset()
                with self.cfg_lock:
                    target = self.cfg["language"]
                    source_cfg = self.cfg["source"]
                forced = None if source_cfg == "auto" else source_cfg
                with self.cfg_lock:
                    beam = SPEED_PROFILES[self.cfg["speed"]]["beam"]
                    threads = SPEED_PROFILES[self.cfg["speed"]]["threads"]
                try:
                    text, detected = self.transcriber(pcm, forced, beam, threads)
                except Exception as e:  # model load failure, bad audio, ...
                    log(f"STT failed: {e}")
                    state.update(status="error", detail=str(e))
                    continue
                if not text:
                    if not partial:
                        self.last_final = ""
                        self.last_original = ""
                    continue
                source = forced or detected
                # Original goes out first — no translation step on its path.
                # Long runs (credit rolls, songs) grow without bound: keep
                # only the tail so the overlay stays readable.
                self.last_original = _tail((self.last_original + text).strip())
                publish_original(self.last_original)
                if partial:
                    # Interim line: confirmed words + the fresh tail. The
                    # accumulated text is now "final" — the next partial
                    # appends to it, nothing is ever re-transcribed.
                    self.last_final = _tail((self.last_final + text).strip())
                    self.tr_worker.submit(self.last_final, source, target)
                    continue
                # Pause confirmed the tail: the accumulated line is complete.
                log(f"heard[{source}]: {self.last_final[:100]}")
                self.tr_worker.submit(self.last_final, source, target)
                self.last_final = ""
                self.last_original = ""
                if self.transcriber._model is not None:
                    state.update(status="listening")
            finally:
                self.queue.task_done()


# ------------------------------------------------------------------ service

class Service:
    def __init__(self) -> None:
        self.cfg = load_config()
        self.cfg_lock = threading.Lock()
        self.pipeline = Pipeline(self.cfg, self.cfg_lock)
        self.capture: Capture | None = None
        self.halt = threading.Event()

    def up(self) -> None:
        state.update(
            enabled=self.cfg["enabled"],
            status="listening" if self.cfg["enabled"] else "off",
            language=self.cfg["language"],
            show_original=self.cfg["show_original"],
            speed=self.cfg["speed"],
            opacity=self.cfg["opacity"],
            detail="",
        )
        self.pipeline.start()
        threading.Thread(target=self._expire_subtitles, daemon=True, name="Expiry").start()
        if self.cfg["enabled"]:
            publish_subtitle("")
            publish_original("")
            self.start_capture()

    def _expire_subtitles(self) -> None:
        """Fade the overlay: clear subtitle.txt once speech has gone quiet."""
        global last_subtitle_at
        while True:
            time.sleep(1.0)
            if last_subtitle_at and time.monotonic() - last_subtitle_at > SUBTITLE_TTL:
                try:
                    with open(SUBTITLE_FILE, encoding="utf-8") as f:
                        current = f.read()
                except OSError:
                    current = ""
                if current.strip():
                    publish_subtitle("")
                try:
                    with open(ORIGINAL_FILE, encoding="utf-8") as f:
                        if f.read().strip():
                            publish_original("")
                except OSError:
                    pass
                last_subtitle_at = 0.0

    def start_capture(self) -> None:
        if self.capture and self.capture.is_alive():
            return
        with self.cfg_lock:
            partial_every = SPEED_PROFILES[self.cfg["speed"]]["partial_every"]
            source_cfg = self.cfg["source"]
        # Live frames are only consumed when the source is explicitly "en"
        # (the streaming model is English-only). Otherwise whisper must
        # receive the chunks — enabling stream_live with source=auto would
        # route every speech frame to a decoder that drops them, producing
        # nothing at all.
        stream_live = self.pipeline.use_streaming() and source_cfg == "en"
        if stream_live:
            log("streaming engine active — live hypothesis while speaking")
        # Re-detect the spoken language for every capture session: pinning it
        # across sessions turned Russian speech into English hallucinations.
        self.pipeline.transcriber.reset_language()
        self.pipeline.streaming.reset()
        self.capture = Capture(self.pipeline.queue, partial_every, stream_live)
        self.capture.start()

    def stop_capture(self) -> None:
        if self.capture:
            self.capture.stop()
            self.capture = None
        self.drain()

    def drain(self) -> None:
        try:
            while True:
                self.pipeline.queue.get_nowait()
                self.pipeline.queue.task_done()
        except queue.Empty:
            pass

    def set_enabled(self, enabled: bool) -> None:
        with self.cfg_lock:
            if self.cfg["enabled"] == enabled:
                return
            self.cfg["enabled"] = enabled
            save_config(self.cfg)
        global last_subtitle_at
        publish_subtitle("")
        publish_original("")
        last_subtitle_at = 0.0
        self.pipeline.last_final = ""
        self.pipeline.last_original = ""
        if enabled:
            state.update(enabled=True, status="listening", detail="")
            self.start_capture()
        else:
            state.update(enabled=False, status="off", detail="")
            self.stop_capture()
        log(f"enabled={enabled}")

    def set_language(self, language: str | None) -> None:
        with self.cfg_lock:
            if language is not None:
                if language not in LANGUAGES:
                    log(f"unknown language: {language}")
                    return
                self.cfg["language"] = language
            save_config(self.cfg)
            current = self.cfg["language"]
        state.update(language=current)
        log(f"target language={current}")

    def set_show_original(self, show: bool) -> None:
        with self.cfg_lock:
            self.cfg["show_original"] = show
            save_config(self.cfg)
        state.update(show_original=show)
        if not show:
            publish_original("")
        log(f"show_original={show}")

    def set_speed(self, speed: str) -> None:
        if speed not in SPEED_PROFILES:
            log(f"unknown speed profile: {speed}")
            return
        with self.cfg_lock:
            self.cfg["speed"] = speed
            save_config(self.cfg)
        state.update(speed=speed)
        # Restart the capture so the new partial cadence takes effect.
        self.stop_capture()
        if self.cfg["enabled"]:
            self.start_capture()
        log(f"speed profile={speed} ({SPEED_PROFILES[speed]})")

    def set_opacity(self, value: float) -> None:
        try:
            value = min(1.0, max(0.2, float(value)))
        except (TypeError, ValueError):
            return
        with self.cfg_lock:
            self.cfg["opacity"] = value
            save_config(self.cfg)
        state.update(opacity=value)
        log(f"opacity={value:.2f}")

    def set_source(self, source: str) -> None:
        with self.cfg_lock:
            if source != "auto" and source not in LANGUAGES:
                return
            self.cfg["source"] = source
            save_config(self.cfg)
        log(f"source language={source}")
        # Capture mode depends on the source (live streaming only for en):
        # restart it so the routing change takes effect immediately.
        if self.cfg["enabled"]:
            self.stop_capture()
            self.start_capture()

    def handle(self, line: str) -> None:
        parts = line.strip().split(None, 1)
        if not parts:
            return
        cmd, arg = parts[0], (parts[1].strip() if len(parts) > 1 else None)
        if cmd == "toggle":
            self.set_enabled(not self.cfg["enabled"])
        elif cmd == "enable":
            self.set_enabled(True)
        elif cmd == "disable":
            self.set_enabled(False)
        elif cmd == "lang":
            self.set_language(arg)
        elif cmd == "source":
            self.set_source(arg or "auto")
        elif cmd == "original":
            self.set_show_original(arg is None or arg in ("on", "1", "true"))
        elif cmd == "speed":
            self.set_speed(arg or "balanced")
        elif cmd == "opacity":
            self.set_opacity(arg)
        elif cmd == "status":
            log(f"status: {json.dumps(state.get(), ensure_ascii=False)}")
        else:
            log(f"unknown command: {cmd}")

    def run(self) -> int:
        self.up()
        log(f"ready (enabled={self.cfg['enabled']}, target={self.cfg['language']})")
        while not self.halt.is_set():
            try:
                self.serve_fifo()
            except OSError as e:
                log(f"fifo error: {e}")
                self.halt.wait(1)
        return 0

    def serve_fifo(self) -> None:
        os.makedirs(STATE_DIR, exist_ok=True)
        try:
            os.unlink(FIFO_FILE)
        except FileNotFoundError:
            pass
        os.mkfifo(FIFO_FILE, 0o600)
        while not self.halt.is_set():
            # Each `echo cmd > fifo` opens a fresh writer; EOF ends the read.
            with open(FIFO_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    self.handle(line)

    def shutdown(self) -> None:
        self.halt.set()
        self.stop_capture()
        state.update(enabled=False, status="off", detail="")
        global last_subtitle_at
        publish_subtitle("")
        publish_original("")
        last_subtitle_at = 0.0
        self.pipeline.last_final = ""
        self.pipeline.last_original = ""
        try:
            os.unlink(FIFO_FILE)
        except OSError:
            pass


def main() -> int:
    if "--status" in sys.argv:
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                print(f.read().strip())
        except OSError:
            print(json.dumps({"enabled": False, "status": "off", "detail": "not running"}))
        return 0

    service = Service()

    def on_signal(_signum, _frame):
        service.shutdown()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    return service.run()


if __name__ == "__main__":
    sys.exit(main())
