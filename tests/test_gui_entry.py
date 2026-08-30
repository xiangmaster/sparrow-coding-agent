"""桌面端可选依赖入口测试。"""

import builtins

import pytest

from sparrow_agent import gui


def test_gui_entry_explains_missing_optional_dependency(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "sparrow_agent.qml_app":
            error = ModuleNotFoundError("No module named 'PySide6'")
            error.name = "PySide6"
            raise error
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    assert gui.main() == 2
    assert "pip install -e '.[gui]'" in capsys.readouterr().err


def test_gui_entry_does_not_hide_unrelated_import_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "sparrow_agent.qml_app":
            error = ModuleNotFoundError("No module named 'unexpected'")
            error.name = "unexpected"
            raise error
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    with pytest.raises(ModuleNotFoundError, match="unexpected"):
        gui.main()
