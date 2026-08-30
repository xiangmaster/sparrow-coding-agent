"""PySide6 安装后执行的离屏桌面窗口冒烟测试。"""

import os

import pytest

pytest.importorskip("PySide6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from sparrow_agent.desktop import MainWindow, create_application  # noqa: E402


@pytest.mark.gui_smoke
def test_desktop_window_builds_three_panels_and_initial_controls() -> None:
    app = create_application([])
    window = MainWindow()
    window.show()
    app.processEvents()

    assert window.minimumSize().width() == 1180
    assert window.minimumSize().height() == 720
    assert window.start_button.isEnabled() is True
    assert window.stop_button.isEnabled() is False
    assert window.timeline.count() == 0
    assert window.gate_label.text() == "等待运行"

    window.close()
    app.processEvents()
