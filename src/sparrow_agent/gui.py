"""Sparrow 桌面端的可选依赖入口。"""

from __future__ import annotations

import sys


def main() -> int:
    try:
        from sparrow_agent.qml_app import run_qml_desktop
    except ModuleNotFoundError as exc:
        if exc.name == "PySide6" or (exc.name or "").startswith("PySide6."):
            print(
                "错误：桌面端依赖尚未安装，请运行 "
                ".venv/bin/pip install -e '.[gui]'",
                file=sys.stderr,
            )
            return 2
        raise
    return run_qml_desktop()
