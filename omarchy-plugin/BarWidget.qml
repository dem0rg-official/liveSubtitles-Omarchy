import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

// "LS" button for the Omarchy bar with an inline settings popup.
// Left click toggles subtitles on/off, right click opens settings
// (target language dropdown + enable toggle), hover shows the shared tooltip.
//
// State is read from the service's files (state.json / subtitle.txt) via
// FileView watchers; the overlay instance (Subtitles.qml) keeps its own
// watcher on subtitle.txt.
BarWidget {
  id: root
  moduleName: "dem0rg.livesubtitles"

  // The bar host sizes the slot from the root item's implicit size, so the
  // button's geometry must be lifted up here (slot width is 0 otherwise).
  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  // ------------------------------------------------------------ service state
  property bool enabled: false
  property string status: "off" // off | listening | loading | error
  property string detail: ""
  property string language: "en"
  property bool showOriginal: false
  property string speed: "balanced"
  property real opacitySetting: 1.0
  property string capture: "system" // "system" | "si<N>"
  property var audioApps: []        // [{id, name}] from `livesubtitles-cli apps`
  // True between a slider move and the service echoing the new value back in
  // state.json. state.json is rewritten on every transcription, and each
  // reload would otherwise reset the slider to the stale value before the
  // debounced CLI call lands.
  property bool opacityPending: false

  readonly property bool busy: status === "loading"
  readonly property bool running: enabled && status !== "off"

  readonly property var speedLabels: ["Battery saver", "Balanced", "Turbo"]
  readonly property string speedLabel: {
    var i = ["battery", "balanced", "turbo"].indexOf(speed)
    return i >= 0 ? speedLabels[i] : speedLabels[1]
  }

  readonly property var languages: [
    { code: "en", label: "English" },
    { code: "ru", label: "Русский" },
    { code: "uk", label: "Українська" },
    { code: "es", label: "Español" },
    { code: "fr", label: "Français" },
    { code: "de", label: "Deutsch" },
    { code: "it", label: "Italiano" },
    { code: "pt", label: "Português" },
    { code: "pl", label: "Polski" },
    { code: "tr", label: "Türkçe" },
    { code: "ja", label: "日本語" },
    { code: "zh", label: "中文" },
    { code: "ko", label: "한국어" }
  ]

  function languageLabel(code) {
    for (var i = 0; i < languages.length; i++)
      if (languages[i].code === code) return languages[i].label
    return code
  }

  function captureLabel(target) {
    if (target === "system") return "Whole system"
    for (var i = 0; i < audioApps.length; i++)
      if (audioApps[i].id === target) return audioApps[i].name
    return target
  }

  // Refresh the app list each time the popup opens (apps come and go).
  onPopupOpenChanged: if (popupOpen) { appsProc.running = false; appsProc.running = true }

  function applyState(raw) {
    try {
      var p = JSON.parse(raw || "{}")
      root.enabled = p.enabled === true
      root.status = String(p.status || "off")
      root.detail = String(p.detail || "")
      root.language = String(p.language || root.language)
      root.showOriginal = p.show_original === true
      root.speed = String(p.speed || "balanced")
      root.capture = String(p.capture || "system")
      var op = Number(p.opacity)
      var applied = isFinite(op) && op > 0 ? Math.min(1.0, Math.max(0.2, op)) : 1.0
      // Ignore service echoes while a local edit is in flight (see opacityPending).
      if (!root.opacityPending || Math.abs(applied - root.opacitySetting) < 0.001)
        root.opacitySetting = applied
      if (root.opacityPending && Math.abs(applied - root.opacitySetting) < 0.001)
        root.opacityPending = false
    } catch (e) {
      root.enabled = false
      root.status = "off"
      root.detail = "service not running"
    }
  }

  FileView {
    path: root.statePath()
    watchChanges: true
    printErrors: false
    // `text()` is stale inside onFileChanged (see Commons/Color.qml) — route
    // through reload() so onLoaded always parses fresh content.
    onLoaded: root.applyState(text())
    onFileChanged: reload()
    onLoadFailed: root.applyState("{}")
  }

  function statePath() {
    var dir = Quickshell.env("LS_STATE_DIR")
    return (dir && dir !== "" ? dir : Quickshell.env("HOME") + "/.local/share/livesubtitles")
      + "/state.json"
  }

  // ------------------------------------------------------------ actions
  function runCli(args) {
    cliProc.command = ["livesubtitles-cli"].concat(args)
    cliProc.running = true
  }

  // Debounce for the opacity slider: one CLI call when dragging settles.
  Timer {
    id: opacityTimer
    interval: 400
    onTriggered: root.runCli(["opacity", root.opacitySetting.toFixed(2)])
  }

  property bool popupOpen: false
  function togglePopup() { popupOpen = !popupOpen }
  // PopupCard calls owner.close() when its focus grab clears (click outside);
  // without this the open flag stays true and the grab keeps swallowing
  // every subsequent bar click.
  function close() { popupOpen = false }

  readonly property string tooltip: {
    if (status === "error") return "Live Subtitles — error: " + detail
    if (busy) return "Live Subtitles — loading model…"
    if (!running) return "Live Subtitles — off\nLeft click: enable · Right click: settings"
    return "Live Subtitles — " + languageLabel(language)
      + "\nLeft click: toggle · Right click: settings"
  }

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: "" // Nerd Font material closed-caption (U+F194, in JetBrainsMono NF)
    tooltipText: root.tooltip
    active: root.enabled
    dimmed: root.status === "error"

    onPressed: function(pressed) {
      if (pressed === Qt.RightButton) root.togglePopup()
      else root.runCli(["toggle"])
    }
  }

  PopupCard {
    id: popup
    anchorItem: root
    bar: root.bar
    owner: root
    open: root.popupOpen
    contentWidth: popup.fittedContentWidth(Style.space(280))
    contentHeight: popup.fittedContentHeight(column.implicitHeight)

    Column {
      id: column
      anchors.fill: parent
      spacing: Style.space(12)

      Text {
        textFormat: Text.PlainText
        text: "Live Subtitles"
        color: root.bar ? root.bar.foreground : Color.foreground
        font.family: root.bar ? root.bar.fontFamily : Style.font.family
        font.pixelSize: Style.font.subtitle
        font.bold: true
      }

      Text {
        textFormat: Text.PlainText
        width: parent.width
        text: {
          if (root.status === "error") return "Error: " + root.detail
          if (root.busy) return "Loading speech model…"
          if (root.enabled) return "Listening to " + (root.capture === "system" ? "system audio" : root.captureLabel(root.capture)) + " → " + root.languageLabel(root.language)
          return "Off. Click the switch below or the LS button."
        }
        color: Color.muted
        font.family: root.bar ? root.bar.fontFamily : Style.font.family
        font.pixelSize: Style.font.caption
        wrapMode: Text.WordWrap
      }

      Dropdown {
        width: parent.width
        label: "Translate into"
        options: root.languages.map(function(l) { return l.label })
        value: root.languageLabel(root.language)
        onChanged: function(label) {
          for (var i = 0; i < root.languages.length; i++)
            if (root.languages[i].label === label) {
              root.runCli(["lang", root.languages[i].code])
              return
            }
        }
      }

      Dropdown {
        width: parent.width
        label: "Latency (CPU trade-off)"
        options: root.speedLabels
        value: root.speedLabel
        onChanged: function(label) {
          var codes = ["battery", "balanced", "turbo"]
          for (var i = 0; i < root.speedLabels.length; i++)
            if (root.speedLabels[i] === label) {
              root.runCli(["speed", codes[i]])
              return
            }
        }
      }

      Dropdown {
        width: parent.width
        label: "Capture audio from"
        options: ["Whole system"].concat(root.audioApps.map(function(a) { return a.name }))
        value: root.captureLabel(root.capture)
        onChanged: function(label) {
          if (label === "Whole system") {
            root.runCli(["capture", "system"])
            return
          }
          for (var i = 0; i < root.audioApps.length; i++)
            if (root.audioApps[i].name === label) {
              root.runCli(["capture", root.audioApps[i].id])
              return
            }
        }
      }

      Toggle {
        width: parent.width
        label: "Show original"
        description: "Dimmed source line above the translation"
        checked: root.showOriginal
        onClicked: root.runCli(["original", root.showOriginal ? "off" : "on"])
      }

      Column {
        width: parent.width
        spacing: Style.space(4)

        Text {
          textFormat: Text.PlainText
          text: "Opacity"
          color: Color.muted
          font.family: root.bar ? root.bar.fontFamily : Style.font.family
          font.pixelSize: Style.font.caption
        }

        PanelSlider {
          bar: root.bar
          width: parent.width
          minimum: 0.2
          maximum: 1.0
          step: 0.05
          value: root.opacitySetting
          onMoved: function(v) {
            root.opacitySetting = v
            root.opacityPending = true
            opacityTimer.restart()
          }
        }
      }

      Toggle {
        width: parent.width
        label: "Enabled"
        description: "Capture system audio and show translated subtitles"
        checked: root.enabled
        onClicked: root.runCli(["toggle"])
      }
    }
  }

  Process {
    id: appsProc
    command: ["livesubtitles-cli", "apps"]
    stdout: StdioCollector {
      onStreamFinished: {
        var list = []
        var lines = this.text.trim().split("\n")
        for (var i = 0; i < lines.length; i++) {
          var parts = lines[i].split("\t")
          if (parts.length === 2 && parts[0].indexOf("si") === 0)
            list.push({ id: parts[0], name: parts[1] })
        }
        root.audioApps = list
      }
    }
  }

  Process { id: cliProc }
}
