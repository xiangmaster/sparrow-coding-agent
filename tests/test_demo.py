"""固定演示模板和准备脚本的回归测试。"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from sparrow_agent.demo import latest_trace, prepare_demo_workspace

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEMO_TEMPLATE = PROJECT_ROOT / "demo" / "parcel_fee"


def test_demo_template_contains_real_regressions() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "unittest", "-v"],
        cwd=DEMO_TEMPLATE,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 1
    assert "test_regular_user_reaches_free_shipping_threshold" in output
    assert "test_started_kilogram_is_charged" in output
    assert "FAILED" in output


def test_prepare_demo_rebuilds_workspace_and_protects_env(tmp_path: Path) -> None:
    source = tmp_path / "template"
    source.mkdir()
    (source / "app.py").write_text("BUG = True\n", encoding="utf-8")
    (source / "__pycache__").mkdir()
    (source / "__pycache__" / "app.pyc").write_bytes(b"cache")
    env_file = tmp_path / "source.env"
    env_file.write_text("DEEPSEEK_API_KEY=secret-test-value\n", encoding="utf-8")
    target = tmp_path / "generated" / "demo"

    prepared = prepare_demo_workspace(source, target, env_file)
    (prepared / "app.py").write_text("BUG = False\n", encoding="utf-8")
    prepared = prepare_demo_workspace(source, target, env_file)

    assert prepared == target
    assert (prepared / "app.py").read_text(encoding="utf-8") == "BUG = True\n"
    assert (prepared / ".sparrow-demo-workspace").is_file()
    assert not (prepared / "__pycache__").exists()
    assert (prepared / ".env").read_text(encoding="utf-8") == (
        "DEEPSEEK_API_KEY=secret-test-value\n"
    )
    assert os.stat(prepared / ".env").st_mode & 0o777 == 0o600


def test_prepare_demo_rejects_target_inside_template(tmp_path: Path) -> None:
    source = tmp_path / "template"
    source.mkdir()
    env_file = tmp_path / ".env"
    env_file.write_text("key=value\n", encoding="utf-8")

    with pytest.raises(ValueError, match="模板内部"):
        prepare_demo_workspace(source, source / "generated", env_file)


def test_prepare_demo_refuses_to_delete_unmarked_directory(tmp_path: Path) -> None:
    source = tmp_path / "template"
    source.mkdir()
    env_file = tmp_path / ".env"
    env_file.write_text("key=value\n", encoding="utf-8")
    existing_target = tmp_path / "existing"
    existing_target.mkdir()
    important = existing_target / "important.txt"
    important.write_text("keep\n", encoding="utf-8")

    with pytest.raises(ValueError, match="拒绝覆盖"):
        prepare_demo_workspace(source, existing_target, env_file)

    assert important.read_text(encoding="utf-8") == "keep\n"


def test_latest_trace_selects_most_recent_file(tmp_path: Path) -> None:
    older = tmp_path / "older.jsonl"
    newer = tmp_path / "newer.jsonl"
    older.write_text("{}\n", encoding="utf-8")
    newer.write_text("{}\n", encoding="utf-8")
    os.utime(older, (1, 1))
    os.utime(newer, (2, 2))

    assert latest_trace(tmp_path) == newer


def test_latest_trace_requires_an_existing_run(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="还没有演示轨迹"):
        latest_trace(tmp_path)
