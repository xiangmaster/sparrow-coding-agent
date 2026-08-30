"""Sparrow 的 Qt Quick/QML 桌面应用入口。"""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QEvent, QUrl
from PySide6.QtGui import QFont, QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine, QQmlEngine
from PySide6.QtQuickControls2 import QQuickStyle

from sparrow_agent.qml_controller import DesktopController


def build_qml_application(
    argv: list[str] | None = None,
    *,
    workspace: str | Path | None = None,
) -> tuple[QGuiApplication, QQmlApplicationEngine, DesktopController]:
    """构造应用、QML 引擎与控制器，供入口和离屏测试共用。"""

    QQuickStyle.setStyle("Basic")
    existing = QGuiApplication.instance()
    app = existing if isinstance(existing, QGuiApplication) else QGuiApplication(argv or [])
    QCoreApplication.setApplicationName("Sparrow Agent")
    QCoreApplication.setOrganizationName("Sparrow")
    app.setFont(QFont("PingFang SC", 13))
    engine = QQmlApplicationEngine()
    controller = DesktopController(workspace)
    QQmlEngine.setObjectOwnership(controller, QQmlEngine.ObjectOwnership.CppOwnership)
    engine._sparrow_controller = controller  # type: ignore[attr-defined]
    engine.rootContext().setContextProperty("controller", controller)
    qml_path = Path(__file__).with_name("qml") / "Main.qml"
    engine.load(QUrl.fromLocalFile(str(qml_path)))
    return app, engine, controller


def run_qml_desktop() -> int:
    app, engine, controller = build_qml_application(sys.argv)
    if not engine.rootObjects():
        return 1
    app.aboutToQuit.connect(controller.shutdown)
    exit_code = app.exec()
    dispose_qml_application(app, engine)
    return exit_code


def dispose_qml_application(
    app: QGuiApplication, engine: QQmlApplicationEngine
) -> None:
    """先销毁 QML 根对象，避免退出阶段访问已经释放的上下文属性。"""

    for root in engine.rootObjects():
        root.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()
