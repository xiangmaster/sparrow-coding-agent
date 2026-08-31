import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Dialogs
import QtQuick.Layouts

ApplicationWindow {
    id: root
    objectName: "sparrowMainWindow"
    width: 1420
    height: 860
    minimumWidth: 1180
    minimumHeight: 720
    visible: true
    title: "Sparrow Agent"
    color: ink

    readonly property color ink: "#07171B"
    readonly property color inkRaised: "#0D2024"
    readonly property color inkSoft: "#142A2D"
    readonly property color paper: "#F2F5F1"
    readonly property color paperRaised: "#FBFCF9"
    readonly property color textPrimary: "#182522"
    readonly property color textSecondary: "#687772"
    readonly property color line: "#D7E0DB"
    readonly property color brand: "#118F79"
    readonly property color brandBright: "#54D7B2"
    readonly property color brandSoft: "#DDF3EB"
    readonly property color jade: "#247D69"
    readonly property color jadeSoft: "#DDF0E9"
    readonly property color vermilion: "#C7523D"
    readonly property color amber: "#C58A36"

    property int selectedPreview: 0

    function toneColor(tone) {
        if (tone === "success") return jade
        if (tone === "error") return vermilion
        if (tone === "warning") return amber
        if (tone === "muted") return textSecondary
        return brand
    }

    function stateColor(key) {
        if (key === "completed") return jade
        if (key === "running" || key === "cancelling") return brand
        if (key === "idle") return textSecondary
        return vermilion
    }

    component FlatButton: Button {
        id: control
        property color accent: root.brand
        property bool filled: false
        implicitHeight: 40
        leftPadding: 15
        rightPadding: 15
        font.pixelSize: 13
        font.weight: Font.DemiBold
        contentItem: Text {
            text: control.text
            color: control.filled ? "white" : control.accent
            font: control.font
            horizontalAlignment: Text.AlignHCenter
            verticalAlignment: Text.AlignVCenter
        }
        background: Rectangle {
            radius: 9
            color: control.filled ? control.accent : "transparent"
            border.width: control.filled ? 0 : 1
            border.color: control.enabled ? control.accent : "#AAA6AD"
            opacity: control.enabled ? (control.hovered ? 0.88 : 1) : 0.45
            Behavior on opacity { NumberAnimation { duration: 120 } }
        }
    }

    component SmallLabel: Text {
        color: root.textSecondary
        font.pixelSize: 11
        font.weight: Font.DemiBold
        font.letterSpacing: 0.7
    }

    component Divider: Rectangle {
        implicitHeight: 1
        color: root.line
    }

    component BrandMark: Item {
        Image {
            anchors.fill: parent
            source: "assets/logo-mark.svg"
            fillMode: Image.PreserveAspectFit
            smooth: true
            mipmap: true
        }
    }

    FolderDialog {
        id: folderDialog
        title: "选择 Sparrow 工作区"
        currentFolder: "file://" + controller.workspacePath
        onAccepted: controller.setWorkspace(selectedFolder.toString())
    }

    Dialog {
        id: alertDialog
        modal: true
        anchors.centerIn: parent
        width: 430
        padding: 24
        property string message: ""
        property string kind: "info"
        standardButtons: Dialog.Ok
        background: Rectangle {
            color: root.paperRaised
            radius: 14
            border.color: root.line
        }
        header: Item {
            implicitHeight: 54
            Text {
                anchors.left: parent.left
                anchors.leftMargin: 24
                anchors.verticalCenter: parent.verticalCenter
                text: alertDialog.title
                color: root.textPrimary
                font.pixelSize: 18
                font.weight: Font.Bold
            }
        }
        contentItem: Text {
            text: alertDialog.message
            color: root.textSecondary
            font.pixelSize: 14
            wrapMode: Text.Wrap
        }
    }

    Dialog {
        id: diffDialog
        modal: true
        anchors.centerIn: parent
        width: Math.min(root.width - 120, 920)
        height: Math.min(root.height - 100, 680)
        padding: 0
        title: controller.previews.length > selectedPreview
               ? controller.previews[selectedPreview].title : "修改细节"
        standardButtons: Dialog.Close
        background: Rectangle {
            color: root.inkRaised
            radius: 14
            border.color: "#393842"
        }
        header: Rectangle {
            implicitHeight: 62
            color: "transparent"
            Text {
                anchors.left: parent.left
                anchors.leftMargin: 22
                anchors.verticalCenter: parent.verticalCenter
                text: "修改细节  /  " + diffDialog.title
                color: "#F2EEF8"
                font.pixelSize: 16
                font.weight: Font.DemiBold
            }
        }
        contentItem: Rectangle {
            color: "#111116"
            TextArea {
                anchors.fill: parent
                anchors.margins: 18
                readOnly: true
                wrapMode: TextEdit.NoWrap
                selectByMouse: true
                text: controller.previews.length > selectedPreview
                      ? controller.previews[selectedPreview].text : ""
                color: "#D9D5E0"
                selectionColor: root.brand
                font.family: "Menlo"
                font.pixelSize: 12
                background: null
            }
        }
    }

    Popup {
        id: settingsPopup
        objectName: "settingsPopup"
        x: root.width - width - 24
        y: 64
        width: 320
        padding: 20
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
        background: Rectangle {
            color: root.paperRaised
            radius: 14
            border.color: root.line
        }
        contentItem: ColumnLayout {
            spacing: 12
            Text {
                text: "运行设置"
                color: root.textPrimary
                font.pixelSize: 17
                font.weight: Font.Bold
            }
            SmallLabel { text: "模型" }
            ComboBox {
                id: modelInput
                Layout.fillWidth: true
                editable: true
                model: ["deepseek-v4-flash", "deepseek-v4-pro"]
                currentIndex: 0
            }
            SmallLabel { text: "推理强度" }
            ComboBox {
                id: reasoningInput
                Layout.fillWidth: true
                model: ["low", "high", "max"]
                currentIndex: 0
            }
            SmallLabel { text: "最大迭代次数" }
            SpinBox {
                id: iterationInput
                Layout.fillWidth: true
                from: 1
                to: 100
                value: 20
            }
            SmallLabel { text: "累计 Token 预算（千）" }
            SpinBox {
                id: tokenBudgetInput
                objectName: "tokenBudgetInput"
                Layout.fillWidth: true
                from: 50
                to: 2000
                stepSize: 50
                value: 400
            }
            Text {
                Layout.fillWidth: true
                text: "达到上限时会安全停止；复杂工程建议 400k，简单任务可降低。"
                color: root.textSecondary
                font.pixelSize: 10
                wrapMode: Text.Wrap
            }
            Text {
                Layout.fillWidth: true
                text: controller.hasApiConfig ? "Sparrow API 配置已就绪" : "Sparrow 启动目录尚未配置 .env"
                color: controller.hasApiConfig ? root.jade : root.vermilion
                font.pixelSize: 12
                wrapMode: Text.Wrap
            }
        }
    }

    Connections {
        target: controller
        function onAlert(title, message, kind) {
            alertDialog.title = title
            alertDialog.message = message
            alertDialog.kind = kind
            alertDialog.open()
        }
    }

    Rectangle {
        id: topBar
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        height: 64
        color: root.ink

        RowLayout {
            anchors.fill: parent
            anchors.leftMargin: 24
            anchors.rightMargin: 24
            spacing: 14

            BrandMark {
                Layout.preferredWidth: 36
                Layout.preferredHeight: 36
            }
            Text {
                text: "SPARROW"
                color: "#F5F1FA"
                font.pixelSize: 16
                font.weight: Font.Bold
                font.letterSpacing: 2.2
            }
            Rectangle { Layout.preferredWidth: 1; Layout.preferredHeight: 22; color: "#393841" }
            Text {
                text: "行动留下轨迹，完成必须有证据"
                color: "#9B98A4"
                font.pixelSize: 12
            }
            Item { Layout.fillWidth: true }

            Button {
                id: workspaceButton
                text: controller.workspaceName
                onClicked: folderDialog.open()
                contentItem: Text {
                    text: workspaceButton.text
                    color: "#D8D4DE"
                    font.pixelSize: 12
                    elide: Text.ElideMiddle
                    horizontalAlignment: Text.AlignHCenter
                    verticalAlignment: Text.AlignVCenter
                }
                background: Rectangle {
                    color: workspaceButton.hovered ? "#163136" : "#10262A"
                    radius: 8
                    border.color: "#25464A"
                }
                implicitWidth: 190
                implicitHeight: 36
                enabled: !controller.isBusy
                ToolTip.visible: hovered
                ToolTip.text: controller.workspacePath
            }
            Button {
                text: "设置"
                onClicked: settingsPopup.open()
                contentItem: Text {
                    text: parent.text
                    color: "#D8D4DE"
                    font.pixelSize: 12
                    horizontalAlignment: Text.AlignHCenter
                    verticalAlignment: Text.AlignVCenter
                }
                background: Rectangle { color: parent.hovered ? "#163136" : "transparent"; radius: 8 }
                implicitWidth: 62
                implicitHeight: 36
            }
            Rectangle {
                Layout.preferredWidth: stateText.implicitWidth + 25
                Layout.preferredHeight: 32
                radius: 16
                color: Qt.alpha(root.stateColor(controller.stateKey), 0.18)
                Row {
                    anchors.centerIn: parent
                    spacing: 7
                    Rectangle {
                        width: 7; height: 7; radius: 4
                        anchors.verticalCenter: parent.verticalCenter
                        color: root.stateColor(controller.stateKey)
                        SequentialAnimation on opacity {
                            running: controller.stateKey === "running"
                            loops: Animation.Infinite
                            NumberAnimation { to: 0.3; duration: 700 }
                            NumberAnimation { to: 1; duration: 700 }
                        }
                    }
                    Text {
                        id: stateText
                        text: controller.stateText
                        color: "#E7E2ED"
                        font.pixelSize: 11
                        font.weight: Font.DemiBold
                    }
                }
            }
        }
    }

    RowLayout {
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: topBar.bottom
        anchors.bottom: parent.bottom
        spacing: 0

        Rectangle {
            id: sidebar
            Layout.preferredWidth: 238
            Layout.fillHeight: true
            color: root.inkRaised

            ColumnLayout {
                anchors.fill: parent
                anchors.margins: 16
                spacing: 10

                Button {
                    id: newTaskButton
                    Layout.fillWidth: true
                    implicitHeight: 44
                    enabled: !controller.isBusy
                    onClicked: controller.newTask()
                    contentItem: Row {
                        anchors.centerIn: parent
                        spacing: 9
                        Text { text: "+"; color: "white"; font.pixelSize: 20; font.weight: Font.Light }
                        Text { text: "新建任务"; color: "white"; font.pixelSize: 13; font.weight: Font.DemiBold }
                    }
                    background: Rectangle {
                        radius: 10
                        color: newTaskButton.hovered ? "#0C7867" : root.brand
                        opacity: newTaskButton.enabled ? 1 : 0.45
                    }
                }

                RowLayout {
                    Layout.topMargin: 14
                    Layout.fillWidth: true
                    Text {
                        text: "运行档案"
                        color: "#AAA6B2"
                        font.pixelSize: 11
                        font.weight: Font.DemiBold
                        font.letterSpacing: 0.8
                    }
                    Item { Layout.fillWidth: true }
                    Button {
                        text: "↻"
                        enabled: !controller.isBusy
                        onClicked: controller.refresh_history()
                        contentItem: Text { text: parent.text; color: "#777480"; horizontalAlignment: Text.AlignHCenter }
                        background: null
                        implicitWidth: 26; implicitHeight: 26
                    }
                }

                ListView {
                    id: historyList
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    clip: true
                    spacing: 6
                    model: controller.history
                    boundsBehavior: Flickable.StopAtBounds
                    delegate: Rectangle {
                        required property var modelData
                        required property int index
                        width: historyList.width
                        height: 60
                        radius: 9
                        color: historyMouse.containsMouse ? "#163034" : "transparent"
                        border.color: "transparent"
                        RowLayout {
                            anchors.fill: parent
                            anchors.leftMargin: 11
                            anchors.rightMargin: 9
                            spacing: 10
                            Rectangle {
                                Layout.preferredWidth: 7
                                Layout.preferredHeight: 7
                                radius: 4
                                color: root.stateColor(modelData.stateKey)
                            }
                            ColumnLayout {
                                Layout.fillWidth: true
                                spacing: 3
                                Text {
                                    Layout.fillWidth: true
                                    text: modelData.title
                                    color: "#E2DEE7"
                                    font.pixelSize: 12
                                    elide: Text.ElideRight
                                }
                                RowLayout {
                                    Layout.fillWidth: true
                                    spacing: 6
                                    Text {
                                        text: modelData.time
                                        color: "#777480"
                                        font.pixelSize: 9
                                    }
                                    Text {
                                        text: "·  " + modelData.stateText
                                        color: root.stateColor(modelData.stateKey)
                                        font.pixelSize: 9
                                    }
                                }
                            }
                            Text { text: "›"; color: "#65626D"; font.pixelSize: 18 }
                        }
                        MouseArea {
                            id: historyMouse
                            anchors.fill: parent
                            hoverEnabled: true
                            enabled: !controller.isBusy
                            onClicked: controller.loadHistory(index)
                        }
                    }
                    Text {
                        anchors.centerIn: parent
                        visible: historyList.count === 0
                        text: "还没有运行记录\n完成一次任务后会出现在这里"
                        color: "#686570"
                        font.pixelSize: 11
                        horizontalAlignment: Text.AlignHCenter
                        lineHeight: 1.5
                    }
                }

                Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 1; color: "#20383B" }
                Text {
                    Layout.fillWidth: true
                    text: "轨迹可能包含代码，仅保存在本机"
                    color: "#66636D"
                    font.pixelSize: 10
                    wrapMode: Text.Wrap
                }
            }
        }

        Rectangle {
            Layout.fillWidth: true
            Layout.fillHeight: true
            color: root.paper

            Item {
                id: homePage
                objectName: "homePage"
                anchors.fill: parent
                visible: controller.mode === "home"

                Column {
                    anchors.centerIn: parent
                    width: Math.min(parent.width - 120, 760)
                    spacing: 18

                    BrandMark {
                        anchors.horizontalCenter: parent.horizontalCenter
                        width: 104; height: 104
                    }
                    Text {
                        anchors.horizontalCenter: parent.horizontalCenter
                        text: "今天想让 Sparrow 完成什么？"
                        color: root.textPrimary
                        font.pixelSize: 30
                        font.weight: Font.Bold
                    }
                    Text {
                        anchors.horizontalCenter: parent.horizontalCenter
                        text: "它会先理解、再行动，并用真实变更和本地验证证明结果。"
                        color: root.textSecondary
                        font.pixelSize: 14
                    }
                    Rectangle {
                        width: parent.width
                        height: 184
                        radius: 16
                        color: root.paperRaised
                        border.color: homeTask.activeFocus ? root.brand : root.line
                        border.width: homeTask.activeFocus ? 2 : 1
                        Behavior on border.color { ColorAnimation { duration: 140 } }

                        TextArea {
                            id: homeTask
                            objectName: "taskInput"
                            anchors.left: parent.left
                            anchors.right: parent.right
                            anchors.top: parent.top
                            anchors.bottom: homeActions.top
                            anchors.margins: 16
                            placeholderText: "例如：阅读订单模块，修复配送费边界缺陷；不要修改测试文件。"
                            placeholderTextColor: "#AAA5AC"
                            color: root.textPrimary
                            font.pixelSize: 15
                            wrapMode: TextEdit.Wrap
                            background: null
                            selectByMouse: true
                        }
                        RowLayout {
                            id: homeActions
                            anchors.left: parent.left
                            anchors.right: parent.right
                            anchors.bottom: parent.bottom
                            anchors.margins: 14
                            height: 42
                            spacing: 8
                            Rectangle {
                                Layout.preferredWidth: configuredText.implicitWidth + 22
                                Layout.preferredHeight: 30
                                radius: 15
                                color: controller.hasApiConfig ? root.jadeSoft : "#F5DEDA"
                                Text {
                                    id: configuredText
                                    anchors.centerIn: parent
                                    text: controller.hasApiConfig ? "DeepSeek 已配置" : "需要配置 .env"
                                    color: controller.hasApiConfig ? root.jade : root.vermilion
                                    font.pixelSize: 10
                                    font.weight: Font.DemiBold
                                }
                            }
                            Item { Layout.fillWidth: true }
                            FlatButton {
                                objectName: "startTaskButton"
                                text: "运行设置"
                                accent: root.textSecondary
                                onClicked: settingsPopup.open()
                            }
                            FlatButton {
                                text: "开始任务  →"
                                filled: true
                                enabled: homeTask.text.trim().length > 0 && !controller.isBusy
                                onClicked: controller.startTask(
                                    homeTask.text,
                                    modelInput.editText,
                                    reasoningInput.currentText,
                                    iterationInput.value,
                                    tokenBudgetInput.value * 1000
                                )
                            }
                        }
                    }
                    Row {
                        anchors.horizontalCenter: parent.horizontalCenter
                        spacing: 28
                        Repeater {
                            model: ["工作区边界保护", "全过程可回放", "完成必须有证据"]
                            Row {
                                spacing: 7
                                Rectangle { width: 6; height: 6; radius: 3; color: root.jade; anchors.verticalCenter: parent.verticalCenter }
                                Text { text: modelData; color: root.textSecondary; font.pixelSize: 11 }
                            }
                        }
                    }
                }
            }

            Item {
                id: runPage
                objectName: "runPage"
                anchors.fill: parent
                visible: controller.mode !== "home"

                ColumnLayout {
                    anchors.fill: parent
                    anchors.leftMargin: 28
                    anchors.rightMargin: 24
                    anchors.topMargin: 22
                    anchors.bottomMargin: 18
                    spacing: 14

                    RowLayout {
                        Layout.fillWidth: true
                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 4
                            RowLayout {
                                Text {
                                    Layout.fillWidth: true
                                    text: controller.taskText.split("\n")[0]
                                    color: root.textPrimary
                                    font.pixelSize: 22
                                    font.weight: Font.Bold
                                    elide: Text.ElideRight
                                }
                                Rectangle {
                                    visible: controller.mode === "history"
                                    Layout.preferredWidth: historyModeText.implicitWidth + 20
                                    Layout.preferredHeight: 27
                                    radius: 14
                                    color: root.brandSoft
                                    Text {
                                        id: historyModeText
                                        anchors.centerIn: parent
                                        text: "离线回放"
                                        color: root.brand
                                        font.pixelSize: 10
                                        font.weight: Font.Bold
                                    }
                                }
                            }
                            Text {
                                text: controller.statusText
                                color: root.textSecondary
                                font.pixelSize: 11
                            }
                        }
                        FlatButton {
                            visible: controller.isBusy
                            text: controller.stateKey === "cancelling" ? "正在取消…" : "安全停止"
                            accent: root.vermilion
                            enabled: controller.stateKey === "running"
                            onClicked: controller.cancelTask()
                        }
                    }

                    Rectangle {
                        Layout.fillWidth: true
                        Layout.preferredHeight: 62
                        radius: 12
                        color: root.paperRaised
                        border.color: root.line
                        RowLayout {
                            anchors.fill: parent
                            anchors.leftMargin: 18
                            anchors.rightMargin: 18
                            spacing: 0
                            Repeater {
                                model: ["理解", "检查", "修改", "验证", "完成"]
                                RowLayout {
                                    required property int index
                                    required property string modelData
                                    Layout.fillWidth: true
                                    spacing: 8
                                    Rectangle {
                                        width: 25; height: 25; radius: 13
                                        color: index <= controller.phase ? root.brand : "#E3E9E5"
                                        Text {
                                            anchors.centerIn: parent
                                            text: index < controller.phase
                                                  || (index === 4 && controller.stateKey === "completed")
                                                  ? "✓" : String(index + 1)
                                            color: index <= controller.phase ? "white" : "#96919A"
                                            font.pixelSize: 10
                                            font.weight: Font.Bold
                                        }
                                    }
                                    Text {
                                        text: modelData
                                        color: index <= controller.phase ? root.textPrimary : "#9B969F"
                                        font.pixelSize: 11
                                        font.weight: index === controller.phase ? Font.Bold : Font.Normal
                                    }
                                    Rectangle {
                                        visible: index < 4
                                        Layout.fillWidth: true
                                        height: 1
                                        color: index < controller.phase ? root.brand : "#D9E1DC"
                                    }
                                }
                            }
                        }
                    }

                    RowLayout {
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        spacing: 16

                        Rectangle {
                            Layout.fillWidth: true
                            Layout.fillHeight: true
                            radius: 14
                            color: root.paperRaised
                            border.color: root.line

                            ListView {
                                id: eventList
                                anchors.fill: parent
                                anchors.margins: 14
                                clip: true
                                spacing: 8
                                model: controller.events
                                boundsBehavior: Flickable.StopAtBounds
                                ScrollBar.vertical: ScrollBar { }
                                onCountChanged: positionViewAtEnd()
                                delegate: Rectangle {
                                    required property var modelData
                                    width: eventList.width - 8
                                    height: Math.max(66, eventContent.implicitHeight + 24)
                                    radius: 10
                                    color: eventMouse.containsMouse ? "#F1EDE6" : "#F7F4EE"
                                    border.color: "#E5E0D7"
                                    Behavior on color { ColorAnimation { duration: 100 } }
                                    RowLayout {
                                        id: eventContent
                                        anchors.left: parent.left
                                        anchors.right: parent.right
                                        anchors.verticalCenter: parent.verticalCenter
                                        anchors.leftMargin: 15
                                        anchors.rightMargin: 15
                                        spacing: 13
                                        Rectangle {
                                            Layout.preferredWidth: 9
                                            Layout.preferredHeight: 9
                                            radius: 5
                                            color: root.toneColor(modelData.tone)
                                        }
                                        ColumnLayout {
                                            Layout.fillWidth: true
                                            spacing: 4
                                            RowLayout {
                                                Layout.fillWidth: true
                                                Text {
                                                    Layout.fillWidth: true
                                                    text: modelData.title
                                                    color: root.textPrimary
                                                    font.pixelSize: 13
                                                    font.weight: Font.DemiBold
                                                    elide: Text.ElideRight
                                                }
                                                Text {
                                                    visible: modelData.iteration !== ""
                                                    text: "ROUND " + modelData.iteration
                                                    color: "#98939D"
                                                    font.pixelSize: 9
                                                    font.weight: Font.Bold
                                                    font.letterSpacing: 0.7
                                                }
                                            }
                                            Text {
                                                Layout.fillWidth: true
                                                visible: text.length > 0
                                                text: modelData.detail
                                                color: root.textSecondary
                                                font.pixelSize: 11
                                                wrapMode: Text.Wrap
                                            }
                                        }
                                    }
                                    MouseArea { id: eventMouse; anchors.fill: parent; hoverEnabled: true; acceptedButtons: Qt.NoButton }
                                }
                                Text {
                                    anchors.centerIn: parent
                                    visible: eventList.count === 0
                                    text: "正在等待第一条行动轨迹…"
                                    color: root.textSecondary
                                    font.pixelSize: 13
                                }
                            }
                        }

                        Rectangle {
                            Layout.preferredWidth: 318
                            Layout.fillHeight: true
                            radius: 14
                            color: "#ECE8E1"
                            border.color: "#D9D4CB"

                            ScrollView {
                                anchors.fill: parent
                                anchors.margins: 16
                                clip: true
                                ColumnLayout {
                                    width: 284
                                    spacing: 16
                                    SmallLabel { text: "EVIDENCE  /  本地证据" }

                                    ColumnLayout {
                                        Layout.fillWidth: true
                                        spacing: 7
                                        Text { text: "工作区变化"; color: root.textPrimary; font.pixelSize: 13; font.weight: Font.Bold }
                                        Repeater {
                                            model: controller.changedFiles
                                            Rectangle {
                                                required property string modelData
                                                Layout.fillWidth: true
                                                implicitHeight: 31
                                                radius: 7
                                                color: root.paperRaised
                                                Text {
                                                    anchors.left: parent.left; anchors.leftMargin: 10
                                                    anchors.right: parent.right; anchors.rightMargin: 8
                                                    anchors.verticalCenter: parent.verticalCenter
                                                    text: modelData
                                                    color: root.textPrimary
                                                    font.family: "Menlo"
                                                    font.pixelSize: 10
                                                    elide: Text.ElideMiddle
                                                }
                                            }
                                        }
                                        Text {
                                            visible: controller.changedFiles.length === 0
                                            text: "尚未检测到文件变化"
                                            color: root.textSecondary
                                            font.pixelSize: 11
                                        }
                                    }

                                    Divider { Layout.fillWidth: true }
                                    ColumnLayout {
                                        Layout.fillWidth: true
                                        spacing: 7
                                        Text { text: "验证记录"; color: root.textPrimary; font.pixelSize: 13; font.weight: Font.Bold }
                                        Text {
                                            Layout.fillWidth: true
                                            text: controller.verificationText
                                            color: controller.verificationText.startsWith("✓") ? root.jade : root.textSecondary
                                            font.family: "Menlo"
                                            font.pixelSize: 10
                                            wrapMode: Text.Wrap
                                        }
                                    }

                                    Divider { Layout.fillWidth: true }
                                    ColumnLayout {
                                        Layout.fillWidth: true
                                        spacing: 7
                                        Text { text: "完成审查"; color: root.textPrimary; font.pixelSize: 13; font.weight: Font.Bold }
                                        Text {
                                            Layout.fillWidth: true
                                            text: controller.gateText
                                            color: controller.stateKey === "completed" ? root.jade : root.textSecondary
                                            font.pixelSize: 11
                                            wrapMode: Text.Wrap
                                            lineHeight: 1.35
                                        }
                                    }

                                    Divider { Layout.fillWidth: true }
                                    ColumnLayout {
                                        visible: controller.previews.length > 0
                                        Layout.fillWidth: true
                                        spacing: 8
                                        Text { text: "修改细节"; color: root.textPrimary; font.pixelSize: 13; font.weight: Font.Bold }
                                        Repeater {
                                            model: controller.previews
                                            FlatButton {
                                                required property var modelData
                                                required property int index
                                                Layout.fillWidth: true
                                                text: modelData.title + "  ↗"
                                                accent: root.brand
                                                onClicked: {
                                                    root.selectedPreview = index
                                                    diffDialog.open()
                                                }
                                            }
                                        }
                                    }

                                    Text {
                                        visible: controller.tracePath.length > 0
                                        Layout.fillWidth: true
                                        text: "轨迹：" + controller.tracePath
                                        color: "#99949D"
                                        font.pixelSize: 9
                                        wrapMode: Text.WrapAnywhere
                                    }
                                }
                            }
                        }
                    }

                    Rectangle {
                        Layout.fillWidth: true
                        Layout.preferredHeight: 54
                        radius: 12
                        color: root.ink
                        RowLayout {
                            anchors.fill: parent
                            anchors.leftMargin: 18
                            anchors.rightMargin: 18
                            spacing: 22
                            Row {
                                spacing: 7
                                Rectangle { width: 7; height: 7; radius: 4; color: controller.changedFiles.length ? root.brandBright : "#627276"; anchors.verticalCenter: parent.verticalCenter }
                                Text { text: controller.changedFiles.length + " 个文件变化"; color: "#D4CFD9"; font.pixelSize: 11 }
                            }
                            Row {
                                spacing: 7
                                Rectangle { width: 7; height: 7; radius: 4; color: controller.verificationText.startsWith("✓") ? root.jade : "#65626C"; anchors.verticalCenter: parent.verticalCenter }
                                Text { text: controller.verificationText.startsWith("✓") ? "验证已通过" : "等待验证"; color: "#D4CFD9"; font.pixelSize: 11 }
                            }
                            Item { Layout.fillWidth: true }
                            Text {
                                text: controller.stateKey === "completed" ? "完成门  /  已放行" : "完成门  /  审查中"
                                color: controller.stateKey === "completed" ? root.brandBright : "#9DABA7"
                                font.pixelSize: 11
                                font.weight: Font.Bold
                                font.letterSpacing: 0.5
                            }
                        }
                    }
                }
            }
        }
    }
}
