import QtQuick
import Quickshell
import Quickshell.Io
import Quickshell.Services.UPower
import qs.Common
import qs.DCommon.Widgets
import qs.Modules.Plugins

// Geräteakkus: UPower (iPhone, Bluetooth) + geraete-akkus (Razer, Headset)
PluginComponent {
    id: root

    readonly property int warnLevel: 20
    property var extraDevices: []
    property var warned: ({})

    // Peripherie aus UPower (keine Laptop-Akkus, kein Netzteil)
    readonly property var upowerDevices: UPower.devices.values
        .filter(d => d.ready && !d.isLaptopBattery && d.percentage > 0
                && d.type !== UPowerDeviceType.LinePower && d.type !== UPowerDeviceType.Ups)
        .map(d => ({
            name: d.model || "Gerät",
            icon: iconFor(d.type),
            percent: Math.round(d.percentage * 100),
            charging: d.state === UPowerDeviceState.Charging,
            source: "upower"
        }))

    readonly property var devices: upowerDevices.concat(extraDevices)
        .sort((a, b) => a.percent - b.percent)
    readonly property var lowest: devices.length > 0 ? devices[0] : null

    function iconFor(t) {
        switch (t) {
        case UPowerDeviceType.Phone: return "smartphone";
        case UPowerDeviceType.Mouse: return "mouse";
        case UPowerDeviceType.Keyboard: return "keyboard";
        case UPowerDeviceType.Headset:
        case UPowerDeviceType.Headphones: return "headphones";
        case UPowerDeviceType.GamingInput: return "sports_esports";
        case UPowerDeviceType.Tablet: return "tablet";
        case UPowerDeviceType.Wearable: return "watch";
        default: return "bluetooth";
        }
    }

    function levelColor(dev) {
        if (!dev) return Theme.surfaceText;
        if (dev.charging) return Theme.primary;
        if (dev.percent <= 10) return Theme.error;
        if (dev.percent <= warnLevel) return Theme.warning;
        return Theme.surfaceText;
    }

    function batteryIcon(dev) {
        if (dev.charging) return "battery_charging_full";
        const steps = ["battery_0_bar", "battery_1_bar", "battery_2_bar", "battery_3_bar",
                       "battery_4_bar", "battery_5_bar", "battery_6_bar", "battery_full"];
        return steps[Math.min(7, Math.floor(dev.percent / 12.5))];
    }

    // einmalige Warnung je Gerät, zurückgesetzt nach dem Laden
    onDevicesChanged: {
        const w = Object.assign({}, warned);
        for (const d of devices) {
            if (d.charging || d.percent > warnLevel + 5) {
                delete w[d.name];
            } else if (d.percent <= warnLevel && !w[d.name]) {
                w[d.name] = true;
                Quickshell.execDetached(["notify-send", "-a", "Geräteakkus", "-i", "battery-caution",
                    d.name + ": Akku schwach", "Noch " + d.percent + " % – bitte laden."]);
            }
        }
        warned = w;
    }

    Process {
        id: extraProc
        command: ["sh", "-c", "exec \"$HOME/.local/bin/geraete-akkus\""]
        stdout: StdioCollector {
            onStreamFinished: {
                try { root.extraDevices = JSON.parse(text || "[]"); }
                catch (e) { root.extraDevices = []; }
            }
        }
    }

    Timer {
        interval: 60000
        running: true
        repeat: true
        triggeredOnStart: true
        onTriggered: if (!extraProc.running) extraProc.running = true
    }

    horizontalBarPill: Component {
        Row {
            spacing: Theme.spacingXS
            visible: root.lowest !== null

            DIcon {
                name: root.lowest ? root.lowest.icon : "battery_unknown"
                size: root.iconSize
                color: root.levelColor(root.lowest)
                anchors.verticalCenter: parent.verticalCenter
            }
            StyledText {
                text: root.lowest ? root.lowest.percent + "%" : ""
                font.pixelSize: root.textSize
                color: root.levelColor(root.lowest)
                anchors.verticalCenter: parent.verticalCenter
            }
        }
    }

    verticalBarPill: Component {
        Column {
            spacing: Theme.spacingXS
            visible: root.lowest !== null

            DIcon {
                name: root.lowest ? root.lowest.icon : "battery_unknown"
                size: root.iconSize
                color: root.levelColor(root.lowest)
                anchors.horizontalCenter: parent.horizontalCenter
            }
            StyledText {
                text: root.lowest ? root.lowest.percent : ""
                font.pixelSize: root.textSize
                color: root.levelColor(root.lowest)
                anchors.horizontalCenter: parent.horizontalCenter
            }
        }
    }

    popoutWidth: 340
    popoutHeight: 90 + Math.max(1, devices.length) * 44

    popoutContent: Component {
        PopoutComponent {
            headerText: "Geräteakkus"
            detailsText: root.devices.length === 0 ? "Keine Geräte mit Akkustand gefunden" : ""

            Column {
                width: parent.width
                spacing: Theme.spacingS

                Repeater {
                    model: root.devices
                    delegate: Row {
                        required property var modelData
                        width: parent.width
                        height: 36
                        spacing: Theme.spacingM

                        DIcon {
                            name: modelData.icon
                            size: 22
                            color: Theme.surfaceText
                            anchors.verticalCenter: parent.verticalCenter
                        }
                        StyledText {
                            width: parent.width - 22 - 110 - Theme.spacingM * 2
                            text: modelData.name
                            elide: Text.ElideRight
                            font.pixelSize: Theme.fontSizeMedium
                            color: Theme.surfaceText
                            anchors.verticalCenter: parent.verticalCenter
                        }
                        Row {
                            width: 110
                            spacing: Theme.spacingXS
                            layoutDirection: Qt.RightToLeft
                            anchors.verticalCenter: parent.verticalCenter
                            StyledText {
                                text: modelData.percent + "%"
                                font.pixelSize: Theme.fontSizeMedium
                                color: root.levelColor(modelData)
                            }
                            DIcon {
                                name: root.batteryIcon(modelData)
                                size: 20
                                color: root.levelColor(modelData)
                            }
                        }
                    }
                }
            }
        }
    }
}
