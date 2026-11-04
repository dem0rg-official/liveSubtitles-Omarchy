# Live Subtitles for Omarchy

Реальное время: системный звук → офлайн-распознавание → офлайн-перевод (Argos Translate) → субтитры внизу экрана в стиле Omarchy.

**Всё работает офлайн.** Все модели — локальные; никаких облачных API.

Два движка распознавания:
- **Стриминговый** (sherpa-onnx streaming zipformer, en) — гипотеза обновляется после каждых 100 мс речи: слова появляются в субтитрах почти одновременно с речью (Gboard-style). Оригинал показывается мгновенно, перевод подтягивается за ~0.25 с.
- **faster-whisper** — для всех языков и для точного финального прохода при паузе (заменяет live-строку на аккуратный текст).

```
System audio (PipeWire monitor)
        │  parec @DEFAULT_MONITOR@  (не микрофон, без loopback)
        ▼
VAD (energy gate) ──► faster-whisper (small, int8, офлайн) ──► Argos Translate (офлайн)
        │
        ▼
~/.local/share/livesubtitles/{state.json, subtitle.txt}
        │  (FileView watch)
        ▼
Omarchy shell plugin (Quickshell): LS-кнопка в баре + слой субтитров (wlr-layer-shell)
```

## Как это выглядит

- **Кнопка `LS`** в панели — клик включает/выключает субтитры, правый клик открывает настройки (язык перевода, вкл/выкл), тултип показывает статус.
- **Оверлей субтитров** внизу экрана: карточка в стиле уведомлений Omarchy (та же тема, шрифт, скругления `Style.cornerRadius`, цвета `Color.notifications.*`), клик-тру поверх остальной поверхности.
- **Настройки сохраняются** между сессиями в `~/.config/livesubtitles/config.json`.

## Установка

```bash
cd ~/Projects/liveSubtitles-Omarchy
./install.sh
```

Скрипт:
1. Проверяет `python3 / pactl / systemctl / quickshell`.
2. Создаёт venv в `~/.local/share/livesubtitles/venv` и ставит `faster-whisper`, `argostranslate`, `numpy`.
3. Устанавливает CLI в `~/.local/bin`.
4. Ставит systemd **user**-юнит `livesubtitles.service` (автозапуск с сессией).
5. Регистрирует плагин Omarchy shell (`dem0rg.livesubtitles`) и добавляет кнопку в бар (правая секция).
6. Предзагружает **Whisper small** (~460 МБ, далее полностью офлайн) и базовый пакет Argos `en→*`.

Перезагрузка не требуется — плагин подхватывается `omarchy-shell shell rescanPlugins` (install.sh вызывает это автоматически). Кнопка `LS` появится в правой части бара.

## Использование

```bash
livesubtitles-cli toggle              # вкл/выкл
livesubtitles-cli enable | disable    # явно включить / выключить
livesubtitles-cli lang ru             # язык перевода (en ru uk es fr de it pt pl tr ja zh ko)
livesubtitles-cli source auto         # язык речи: auto (whisper detect) или фиксированный
livesubtitles-cli original on|off     # строка оригинала над переводом
livesubtitles-cli speed turbo         # battery | balanced | turbo (CPU ради задержки)
livesubtitles-cli opacity 0.6         # прозрачность субтитров (0.2–1.0)
livesubtitles-cli status              # JSON-статус
```

- **Клик по LS** — вкл/выкл, **правый клик** — настройки.
- Язык перевода меняется на лету; если для пары `ru→de` нет пакета Argos, сервис покажет сырую транскрипцию вместо перевода и напишет в лог.

### Установка языковых пакетов Argos

```bash
~/.local/share/livesubtitles/venv/bin/argospm update
~/.local/share/livesubtitles/venv/bin/argospm install translate-en_ru   # en→ru
~/.local/share/livesubtitles/venv/bin/argospm install translate-ru_en   # ru→en
# общий синтаксис: translate-<src>_<dst> (или translate-from_<src> — все пары от языка)
```

Без пакета для нужной пары субтитры показываются без перевода (fallback на транскрипт) — ничего не падает.

## Как это работает

| Компонент | Файл | Роль |
|---|---| native-topic---|
| Сервис | `livesubtitles/service.py` | захват monitor-источника (`parec`), VAD-окна, faster-whisper, Argos, публикация `state.json` + `subtitle.txt` |
| CLI | `livesubtitles/cli.py` | команды через `control.fifo` (toggle/lang/source/status) |
| Плагин Omarchy | `omarchy-plugin/` | LS-кнопка (bar-widget) + overlay-слой субтитров (panel, wlr-layer-shell) |
| Обёртки | `bin/` | venv-загрузчики и waybar-совместимый JSON-эндпоинт |

- **Захват**: `pactl get-default-sink` → `<sink>.monitor` (fallback `@DEFAULT_MONITOR@`) — только монитор-источник выхода, **не микрофон**; никакой звук не дублируется в колонки (чтение monitor не создаёт loopback).
- **STT**: faster-whisper `small` (int8/auto), VAD-фильтр, автоопределение языка речи (`source auto`) или фиксированный. Офлайн после первичной загрузки модели.
- **Перевод**: Argos Translate, полностью офлайн, пары ставятся через `argospm`.
- **Отрисовка**: плагин читает `state.json` и `subtitle.txt` через `FileView.watchChanges`, карточка субтитра — на `Color.notifications.*` и `Style.cornerRadius` текущей темы Omarchy, окно — layer-shell Overlay, клик-тру маской `Region`.
- **Смена темы** Omarchy подтягивается автоматически: плагин берёт цвета из `qs.Commons` синглтонов, живущих в `omarchy-shell`.

## Компоненты

```
livesubtitles-Omarchy/
├── livesubtitles/
│   ├── service.py        # захват + STT + перевод + публикация
│   └── cli.py            # CLI поверх control.fifo
├── omarchy-plugin/       # плагин Omarchy shell (LS-кнопка + субтитры)
│   ├── manifest.json
│   ├── BarWidget.qml     # кнопка LS + popup настроек
│   ├── Subtitles.qml     # layer-shell overlay субтитров
│   ├── State.qml         # singleton: state.json/subtitle.txt watchers
│   └── qmldir
├── bin/
│   ├── livesubtitles-cli
│   ├── livesubtitles-service
│   └── livesubtitles-waybar-status  # waybar-совместимый JSON
├── packaging/
│   └── livesubtitles.service
├── install.sh
└── uninstall.sh
```

## Настройка через окружение

| Переменная | По умолчанию | Смысл |
|---|---|---|
| `LS_WHISPER_MODEL` | `small` | tiny/base/small/medium (модель качается при первом использовании) |
| `LS_WHISPER_DEVICE` | `auto` | `cpu` / `cuda` / `auto` |
| `LS_WHISPER_COMPUTE` | `auto` | int8/int8_float16/float16/float32 |
| `LS_SILENCE_RMS` | `220` | порог тишины (VAD), поднять при шуме |
| `LS_NOISE_FLOOR` | `60` | отсечение цифрового нуля |
| `LS_STATE_DIR` / `LS_CONFIG_DIR` | `~/.local/share` / `~/.config` | каталоги данных |

## Автозапуск

systemd user-юнит привязан к `graphical-session.target` — стартует с сессией. Включение/выключение субтитров (state `enabled`) также сохраняется между сессиями: если субтитры были включены при выходе, сервис подхватит их при следующем входе.

Проверка: `journalctl --user -u livesubtitles -f`

## Waybar (опционально, если у вас чистый Waybar, а не Omarchy shell)

Omarchy 4.x использует Quickshell-бар, поэтому способ по умолчанию — плагин. Для классического Waybar:

```jsonc
// ~/.config/waybar/config.jsonc (фрагмент)
"custom/livesubtitles": {
  "exec": "~/.local/bin/livesubtitles-waybar-status",
  "return-type": "json",
  "interval": 1,
  "on-click": "livesubtitles-cli toggle",
  "on-click-right": "hyprctl dispatch exec ...",  // или ваш дропдаун настроек
  "tooltip": true
}
```

`livesubtitles-waybar-status` печатает `{"text":"LS","tooltip":"Live Subtitles — on → Русский\nClick: toggle · Right click: settings","class":"on"}` — состояние из `state.json`, `class` = `off|on|loading|error` для стилизации в `style.css`.

## Устранение неполадок

| Симптом | Что делать |
|---|---|
| LS-кнопки нет в баре | `omarchy-shell shell rescanPlugins`, проверьте `omarchy plugin list` |
| «service not running» в тултипе | `systemctl --user status livesubtitles`, логи: `journalctl --user -u livesubtitles -f` |
| Субтитры не появляются | проверьте, что звук действительно играет в sink; поднимите громкость; LS_SILENCE_RMS выше/ниже |
| Перевод не работает | нет пакета Argos: `$VENV/bin/argospm install translate-en_ru`; в этом случае показывается транскрипт |
| Распознавание плохое | `LS_WHISPER_MODEL=medium` в юните (медленнее, но точнее), или `source en` вместо auto |
| Монитор-источник не найден | `pactl list short sources \| grep monitor` — проверьте, что sink существует |

## Требования

- Omarchy (Hyprland + omarchy-shell 4.x) или просто Hyprland + Waybar
- PipeWire (pactl)
- Python 3.10+
- ~460 МБ диска под модель Whisper small
- ~1–2 ГБ ОЗУ для int8 small на CPU
