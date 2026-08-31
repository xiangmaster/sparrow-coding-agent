import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Layouts

Item {
    id: page
    required property var backend
    required property var hostWindow
    property color paper: "#F2F5F1"
    property color paperRaised: "#FBFCF9"
    property color textPrimary: "#182522"
    property color textSecondary: "#687772"
    property color line: "#D7E0DB"
    property color brand: "#118F79"
    property color brandSoft: "#DDF3EB"
    property color jade: "#247D69"
    property color vermilion: "#C7523D"

    function sendMessage() {
        const message = composer.text.trim()
        if (message.length === 0 || backend.isBusy || backend.mode === "history")
            return
        composer.text = ""
        backend.continueTask(message)
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.leftMargin: 30
        anchors.rightMargin: 30
        anchors.topMargin: 20
        anchors.bottomMargin: 18
        spacing: 0

        RowLayout {
            Layout.fillWidth: true
            Layout.preferredHeight: 58
            spacing: 12

            ColumnLayout {
                Layout.fillWidth: true
                spacing: 3
                Text {
                    Layout.fillWidth: true
                    text: backend.taskText.split("\n")[0]
                    color: page.textPrimary
                    font.pixelSize: 19
                    font.weight: Font.Bold
                    elide: Text.ElideRight
                }
                Text {
                    text: backend.statusText
                    color: page.textSecondary
                    font.pixelSize: 10
                }
            }

            Rectangle {
                visible: backend.mode === "history"
                Layout.preferredWidth: 76
                Layout.preferredHeight: 28
                radius: 14
                color: page.brandSoft
                Text {
                    anchors.centerIn: parent
                    text: "离线回放"
                    color: page.brand
                    font.pixelSize: 10
                    font.weight: Font.DemiBold
                }
            }

            Button {
                visible: backend.isBusy
                enabled: backend.stateKey === "running"
                text: backend.stateKey === "cancelling" ? "正在停止…" : "停止"
                onClicked: backend.cancelTask()
                contentItem: Text {
                    text: parent.text
                    color: page.vermilion
                    font.pixelSize: 11
                    font.weight: Font.DemiBold
                    horizontalAlignment: Text.AlignHCenter
                    verticalAlignment: Text.AlignVCenter
                }
                background: Rectangle {
                    radius: 8
                    color: parent.hovered ? "#F8E8E4" : "transparent"
                    border.color: page.vermilion
                    opacity: parent.enabled ? 1 : 0.5
                }
                implicitWidth: 86
                implicitHeight: 34
            }
        }

        Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 1; color: page.line }

        ListView {
            id: conversationList
            objectName: "conversationList"
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.topMargin: 10
            Layout.bottomMargin: 8
            clip: true
            spacing: 4
            model: backend.conversationMessages
            boundsBehavior: Flickable.StopAtBounds
            ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
            onCountChanged: positionViewAtEnd()

            delegate: Item {
                id: messageDelegate
                required property var modelData
                width: conversationList.width
                height: messageBubble.height + 12
                readonly property bool isUser: modelData.kind === "user"
                readonly property bool isTool: modelData.kind === "tool"

                Rectangle {
                    id: messageBubble
                    width: messageDelegate.isTool
                           ? Math.min(parent.width - 84, 780)
                           : messageDelegate.isUser
                             ? Math.min(parent.width * 0.72, 720)
                             : Math.min(parent.width - 36, 860)
                    height: messageContent.implicitHeight + (messageDelegate.isTool ? 16 : 24)
                    x: messageDelegate.isUser ? parent.width - width - 8 : 8
                    radius: messageDelegate.isTool ? 8 : 13
                    color: messageDelegate.isUser
                           ? page.brandSoft
                           : messageDelegate.isTool
                             ? (toolMouse.containsMouse ? "#EDF1EE" : "#F5F7F4")
                             : "transparent"
                    border.width: messageDelegate.isTool ? 1 : 0
                    border.color: modelData.tone === "error" ? "#E7B9B0" : page.line
                    Behavior on color { ColorAnimation { duration: 100 } }

                    RowLayout {
                        id: messageContent
                        anchors.left: parent.left
                        anchors.right: parent.right
                        anchors.verticalCenter: parent.verticalCenter
                        anchors.leftMargin: messageDelegate.isTool ? 12 : 16
                        anchors.rightMargin: messageDelegate.isTool ? 12 : 16
                        spacing: 10

                        Image {
                            visible: !messageDelegate.isUser && !messageDelegate.isTool
                            Layout.preferredWidth: 25
                            Layout.preferredHeight: 25
                            Layout.alignment: Qt.AlignTop
                            source: "assets/logo-mark.svg"
                            fillMode: Image.PreserveAspectFit
                            smooth: true
                        }

                        Rectangle {
                            visible: messageDelegate.isTool
                            Layout.preferredWidth: 7
                            Layout.preferredHeight: 7
                            radius: 4
                            color: modelData.tone === "error" ? page.vermilion : page.textSecondary
                        }

                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: messageDelegate.isTool ? 2 : 6
                            Text {
                                visible: !messageDelegate.isUser
                                Layout.fillWidth: true
                                text: modelData.title
                                color: messageDelegate.isTool ? page.textSecondary : page.textPrimary
                                font.pixelSize: messageDelegate.isTool ? 11 : 12
                                font.weight: Font.DemiBold
                                elide: Text.ElideRight
                            }
                            Text {
                                Layout.fillWidth: true
                                visible: text.length > 0
                                text: modelData.text
                                color: messageDelegate.isTool ? page.textSecondary : page.textPrimary
                                font.pixelSize: messageDelegate.isTool ? 10 : 14
                                wrapMode: Text.Wrap
                                lineHeight: 1.35
                            }
                        }
                    }

                    MouseArea {
                        id: toolMouse
                        anchors.fill: parent
                        hoverEnabled: true
                        acceptedButtons: Qt.NoButton
                    }
                }
            }

            Text {
                anchors.centerIn: parent
                visible: conversationList.count === 0
                text: "对话将在这里展开"
                color: page.textSecondary
                font.pixelSize: 13
            }
        }

        Flow {
            visible: backend.previews.length > 0
            Layout.fillWidth: true
            Layout.bottomMargin: 8
            spacing: 7
            Repeater {
                model: backend.previews
                Button {
                    id: changeChip
                    required property var modelData
                    required property int index
                    text: modelData.title + "   " + modelData.summary
                    onClicked: hostWindow.showDiff(index)
                    contentItem: Text {
                        text: parent.text
                        color: page.brand
                        font.family: "Menlo"
                        font.pixelSize: 10
                        horizontalAlignment: Text.AlignHCenter
                        verticalAlignment: Text.AlignVCenter
                    }
                    background: Rectangle {
                        radius: 7
                        color: parent.hovered ? page.brandSoft : page.paperRaised
                        border.color: page.line
                    }
                    implicitHeight: 30
                    leftPadding: 12
                    rightPadding: 12

                    ToolTip {
                        id: hoverReview
                        visible: changeChip.hovered
                        delay: 320
                        timeout: 8000
                        width: Math.min(620, page.width - 80)
                        height: Math.min(330, 62 + modelData.lines.length * 22)
                        padding: 0
                        background: Rectangle {
                            color: "#111A1C"
                            radius: 10
                            border.color: "#315057"
                        }
                        contentItem: ColumnLayout {
                            spacing: 0
                            Rectangle {
                                Layout.fillWidth: true
                                Layout.preferredHeight: 42
                                color: "#0D2024"
                                Text {
                                    anchors.left: parent.left
                                    anchors.leftMargin: 13
                                    anchors.verticalCenter: parent.verticalCenter
                                    text: modelData.title + "    " + modelData.summary
                                    color: "#E8F0EC"
                                    font.family: "Menlo"
                                    font.pixelSize: 10
                                    font.weight: Font.DemiBold
                                }
                            }
                            ListView {
                                Layout.fillWidth: true
                                Layout.fillHeight: true
                                clip: true
                                model: modelData.lines
                                boundsBehavior: Flickable.StopAtBounds
                                delegate: Rectangle {
                                    required property var modelData
                                    width: ListView.view.width
                                    height: 21
                                    color: modelData.kind === "add" ? "#14372D"
                                         : modelData.kind === "remove" ? "#43231F"
                                         : modelData.kind === "hunk" ? "#182C32"
                                         : "transparent"
                                    Text {
                                        anchors.left: parent.left
                                        anchors.leftMargin: 11
                                        anchors.right: parent.right
                                        anchors.rightMargin: 8
                                        anchors.verticalCenter: parent.verticalCenter
                                        text: modelData.text
                                        color: modelData.kind === "add" ? "#A9E9C9"
                                             : modelData.kind === "remove" ? "#F0B6AD"
                                             : modelData.kind === "hunk" ? "#80C9D6"
                                             : "#C7D0CC"
                                        font.family: "Menlo"
                                        font.pixelSize: 9
                                        elide: Text.ElideRight
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }

        Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: backend.mode === "history" ? 54 : 116
            radius: 14
            color: page.paperRaised
            border.color: composer.activeFocus ? page.brand : page.line
            border.width: composer.activeFocus ? 2 : 1

            TextArea {
                id: composer
                objectName: "conversationComposer"
                visible: backend.mode !== "history"
                anchors.left: parent.left
                anchors.right: sendButton.left
                anchors.top: parent.top
                anchors.bottom: parent.bottom
                anchors.margins: 13
                placeholderText: backend.isBusy
                                 ? "Sparrow 正在处理当前消息…"
                                 : "继续提出要求、补充信息或让 Sparrow 检查刚才的修改…"
                placeholderTextColor: "#9AA6A1"
                enabled: !backend.isBusy
                color: page.textPrimary
                font.pixelSize: 14
                wrapMode: TextEdit.Wrap
                selectByMouse: true
                background: null
                Keys.onPressed: function(event) {
                    if ((event.modifiers & Qt.ControlModifier)
                            && (event.key === Qt.Key_Return || event.key === Qt.Key_Enter)) {
                        page.sendMessage()
                        event.accepted = true
                    }
                }
            }

            Button {
                id: sendButton
                objectName: "continueTaskButton"
                visible: backend.mode !== "history"
                anchors.right: parent.right
                anchors.bottom: parent.bottom
                anchors.margins: 13
                width: 76
                height: 38
                enabled: composer.text.trim().length > 0 && !backend.isBusy
                text: backend.isBusy ? "处理中" : "发送"
                onClicked: page.sendMessage()
                contentItem: Text {
                    text: parent.text
                    color: "white"
                    font.pixelSize: 12
                    font.weight: Font.DemiBold
                    horizontalAlignment: Text.AlignHCenter
                    verticalAlignment: Text.AlignVCenter
                }
                background: Rectangle {
                    radius: 9
                    color: page.brand
                    opacity: parent.enabled ? (parent.hovered ? 0.84 : 1) : 0.38
                }
            }

            Text {
                visible: backend.mode === "history"
                anchors.centerIn: parent
                text: "这是旧版运行轨迹的离线回放"
                color: page.textSecondary
                font.pixelSize: 11
            }
        }

        Text {
            visible: backend.mode !== "history"
            Layout.alignment: Qt.AlignHCenter
            Layout.topMargin: 6
            text: "Ctrl + Enter 发送  ·  工作区操作受安全边界保护"
            color: "#8B9893"
            font.pixelSize: 9
        }
    }
}
