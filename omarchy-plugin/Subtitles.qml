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
  // Reading-time model (mirrors service.py constants; state.json overrides).
  property real ttlBase: 4.0
  property real ttlPerChar: 0.04
  property real ttlMax: 10.0
  // Overlay-side expiry: clears stale text after base + per-char reading
  // time, so longer lines linger longer instead of vanishing too fast.
  property real lastUpdate: 0
  Timer {
    id: expireTimer
    interval: 500
    running: root.enabled
    repeat: true
    onTriggered: {
      if (root.subtitle === "" && root.original === "") return
      var n = Math.max(root.subtitle.length, root.original.length)
      var ttl = Math.min(root.ttlMax, root.ttlBase + n * root.ttlPerChar)
      if (root.lastUpdate > 0 && (Date.now() - root.lastUpdate) > ttl * 1000) {
        root.subtitle = ""
        root.original = ""
        root.lastUpdate = 0
      }
    }
  }
  onSubtitleChanged: {
    lastUpdate = Date.now()
    if (subtitle !== "") revealAnim.restart()
  }
  onOriginalChanged: lastUpdate = Date.now()

  function applyState(raw) {
    try {
      var p = JSON.parse(raw || "{}")
      root.enabled = p.enabled === true
      root.showOriginal = p.show_original === true
      var op = Number(p.opacity)
      root.opacitySetting = isFinite(op) && op > 0 ? Math.min(1.0, Math.max(0.2, op)) : 1.0
      if (isFinite(Number(p.ttl_base))) root.ttlBase = Number(p.ttl_base)
      if (isFinite(Number(p.ttl_per_char))) root.ttlPerChar = Number(p.ttl_per_char)
      if (isFinite(Number(p.ttl_max))) root.ttlMax = Number(p.ttl_max)
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

      // Fully click-through: subtitles are read-only, anything underneath
      // (buttons, links, videos) stays interactive.
      mask: Region {}

      readonly property real bottomMargin: Style.gapsOut + Style.space(24)
      // Same breathing room on the sides as at the bottom.
      readonly property real sideMargin: bottomMargin

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

        // Longest width the text may occupy: window minus side margins
        // (minus card padding, handled by the width math below).
        readonly property real maxTextWidth: Math.max(
          Style.space(120), window.width - 2 * window.sideMargin - Style.space(28))

        implicitWidth: Math.min(
          Math.max(
            originalLabel.visible ? originalLabel.implicitWidth : 0,
            label.implicitWidth) + Style.space(28),
          maxTextWidth + Style.space(28))
        implicitHeight: (originalLabel.visible ? originalLabel.implicitHeight + Style.space(4) : 0)
          + label.implicitHeight + Style.space(16)

        // Reveal holder: text laid out at FULL width from the first frame
        // (no re-wrap "squeezing"); the wipe is a card-colored "curtain"
        // rectangle shrinking leftward as the reveal progresses.
        Item {
          id: revealHolder
          width: parent.width
          height: parent.implicitHeight - Style.space(16)
          anchors.verticalCenter: parent.verticalCenter
          readonly property real cut: width * Math.max(0.001, window.revealProgress)

          Column {
            id: column
            anchors.horizontalCenter: parent.horizontalCenter
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
              width: Math.min(implicitWidth, card.maxTextWidth)
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
              width: Math.min(implicitWidth, card.maxTextWidth)
            }
          }

          // Curtain over the not-yet-revealed part (card-colored so it blends
          // with the card background), shrinking leftward as the wipe moves.
          Rectangle {
            anchors.top: parent.top
            anchors.bottom: parent.bottom
            x: revealHolder.cut
            width: parent.width - revealHolder.cut + 2
            color: card.color
            visible: window.revealProgress < 1.0
          }
        }

        Behavior on implicitWidth { NumberAnimation { duration: 120; easing.type: Easing.OutCubic } }
        Behavior on implicitHeight { NumberAnimation { duration: 120; easing.type: Easing.OutCubic } }
      }

      // Progressive text reveal: a card-colored curtain slides off rightward,
      // uncovering the text left→right. Soft ease, replays on every new line.
      property real revealProgress: 1.0
      SequentialAnimation on revealProgress {
        id: revealAnim
        running: false
        NumberAnimation {
          from: 0.0
          to: 1.0
          duration: 700
          easing.type: Easing.InOutSine
        }
      }
      onVisibleChanged: if (window.visible) revealAnim.restart()
    }
  }
}
