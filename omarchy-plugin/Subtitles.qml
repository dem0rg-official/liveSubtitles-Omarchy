import QtQuick
import Quickshell
import Quickshell.Wayland
import Quickshell.Io
import qs.Commons
import qs.Ui

// Subtitle overlay. Mounted as a keepLoaded panel so the layer-shell windows
// survive toggles; visibility follows service state + subtitle text.
// Reads the same service files as the bar widget (its own FileView watchers).
// Shows the translation (subtitle.txt) with an optional dimmer original line
// (original.txt) above it, at the configured opacity.
Item {
  id: root

  property bool opened: true

  // Open by default: the overlay rides on service state. hide()/close()
  // (host panel lifecycle or IPC) suppresses it until open()/toggle().

  // ------------------------------------------------------------- service state
  property bool enabled: false
  property bool showOriginal: false
  property real opacitySetting: 1.0
  property string subtitle: ""
  property string original: ""

  function applyState(raw) {
    try {
      var p = JSON.parse(raw || "{}")
      root.enabled = p.enabled === true
      root.showOriginal = p.show_original === true
      var op = Number(p.opacity)
      root.opacitySetting = isFinite(op) && op > 0 ? Math.min(1.0, Math.max(0.2, op)) : 1.0
    } catch (e) {
      root.enabled = false
    }
  }

  function statePath() {
    var dir = Quickshell.env("LS_STATE_DIR")
    return (dir && dir !== "" ? dir : Quickshell.env("HOME") + "/.local/share/livesubtitles")
      + "/state.json"
  }

  function subtitlePath() {
    var dir = Quickshell.env("LS_STATE_DIR")
    return (dir && dir !== "" ? dir : Quickshell.env("HOME") + "/.local/share/livesubtitles")
      + "/subtitle.txt"
  }

  function originalPath() {
    var dir = Quickshell.env("LS_STATE_DIR")
    return (dir && dir !== "" ? dir : Quickshell.env("HOME") + "/.local/share/livesubtitles")
      + "/original.txt"
  }

  FileView {
    path: root.statePath()
    watchChanges: true
    printErrors: false
    // `text()` is stale inside onFileChanged — reload() → onLoaded gives fresh
    // content (same pattern as Commons/Color.qml).
    onLoaded: root.applyState(text())
    onFileChanged: reload()
    onLoadFailed: root.applyState("{}")
  }

  FileView {
    path: root.subtitlePath()
    watchChanges: true
    printErrors: false
    onLoaded: root.subtitle = text()
    onFileChanged: reload()
    onLoadFailed: root.subtitle = ""
  }

  FileView {
    path: root.originalPath()
    watchChanges: true
    printErrors: false
    onLoaded: root.original = text()
    onFileChanged: reload()
    onLoadFailed: root.original = ""
  }

  // Overlay shows whenever the plugin is open AND the service has a line.
  readonly property bool overlayVisible: opened && enabled && subtitle !== ""
  readonly property bool originalVisible: showOriginal && original !== ""

  function open() { opened = true }
  function close() { opened = false }
  function toggle() { opened = !opened }

  IpcHandler {
    target: "dem0rg.livesubtitles.overlay"

    function toggle(): void { root.toggle() }
    function show(): void { root.open() }
    function hide(): void { root.close() }
  }

  Variants {
    model: Quickshell.screens

    PanelWindow {
      id: window
      required property var modelData
      screen: modelData
      visible: root.overlayVisible

      WlrLayershell.namespace: "dem0rg-livesubtitles"
      WlrLayershell.layer: WlrLayer.Overlay
      WlrLayershell.keyboardFocus: WlrKeyboardFocus.None
      exclusionMode: ExclusionMode.Ignore
      color: "transparent"

      anchors { top: true; bottom: true; left: true; right: true }

      // Click-through everywhere except the subtitle card itself.
      mask: Region { item: card }

      readonly property real bottomMargin: Style.gapsOut + Style.space(24)

      BorderSurface {
        id: card

        // opacity is an Item property — the layer-shell PanelWindow itself
        // rejects it, so the setting applies to the card content instead.
        opacity: root.opacitySetting
        anchors.horizontalCenter: parent.horizontalCenter
        anchors.bottom: parent.bottom
        anchors.bottomMargin: window.bottomMargin

        radius: Style.cornerRadius
        color: Color.notifications.background
        borderSpec: Border.localOrSurfaceSpec(
          "notifications", "border",
          Color.notifications.border, Color.notifications.border, 1)

        implicitWidth: Math.min(
          Math.max(
            originalLabel.visible ? originalLabel.implicitWidth : 0,
            label.implicitWidth) + Style.space(28),
          window.width - Style.space(48))
        implicitHeight: (originalLabel.visible ? originalLabel.implicitHeight + Style.space(4) : 0)
          + label.implicitHeight + Style.space(16)

        Column {
          id: column
          anchors.centerIn: parent
          spacing: Style.space(4)

          Text {
            id: originalLabel
            visible: root.originalVisible
            textFormat: Text.PlainText
            text: root.original
            color: Color.notifications.text
            opacity: 0.65
            font.family: Style.font.family
            font.pixelSize: Style.font.caption
            font.italic: true
            horizontalAlignment: Text.AlignHCenter
            wrapMode: Text.WrapAtWordBoundaryOrAnywhere
            width: Math.min(implicitWidth, Style.space(560))
          }

          Text {
            id: label
            textFormat: Text.PlainText
            text: root.subtitle
            color: Color.notifications.text
            font.family: Style.font.family
            font.pixelSize: Style.font.heading
            horizontalAlignment: Text.AlignHCenter
            wrapMode: Text.WrapAtWordBoundaryOrAnywhere
            width: Math.min(implicitWidth, Style.space(560))
          }
        }

        Behavior on implicitWidth { NumberAnimation { duration: 120; easing.type: Easing.OutCubic } }
        Behavior on implicitHeight { NumberAnimation { duration: 120; easing.type: Easing.OutCubic } }
      }
    }
  }
}
