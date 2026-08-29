"""固定演示工作区的准备和轨迹定位。"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SOURCE = PROJECT_ROOT / "demo" / "parcel_fee"
DEFAULT_TARGET = PROJECT_ROOT / ".sparrow" / "demo-workspace"
DEFAULT_ENV_FILE = PROJECT_ROOT / ".env"
DEFAULT_RUN_DIRECTORY = DEFAULT_TARGET / ".sparrow" / "runs"
_DEMO_MARKER = ".sparrow-demo-workspace"


def prepare_demo_workspace(
    source: Path = DEFAULT_SOURCE,
    target: Path = DEFAULT_TARGET,
    env_file: Path = DEFAULT_ENV_FILE,
) -> Path:
    """重建演示目录并安全复制本地配置，返回演示工作区路径。"""

    source = source.resolve(strict=True)
    env_file = env_file.resolve(strict=True)
    target = target.resolve(strict=False)
    if not source.is_dir():
        raise ValueError(f"演示模板不是目录：{source}")
    if not env_file.is_file():
        raise ValueError(f"本地配置不是文件：{env_file}")
    if target == source or source in target.parents:
        raise ValueError("演示目标不能覆盖模板或位于模板内部")

    if target.exists():
        if not target.is_dir() or target.is_symlink():
            raise ValueError(f"演示目标不是可重建的普通目录：{target}")
        if not (target / _DEMO_MARKER).is_file():
            raise ValueError(f"拒绝覆盖没有 Sparrow 演示标记的目录：{target}")
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        source,
        target,
        ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc"),
    )
    (target / _DEMO_MARKER).write_text(
        "此目录由 scripts/prepare_demo.py 生成，可安全重建。\n",
        encoding="utf-8",
    )
    local_env = target / ".env"
    shutil.copyfile(env_file, local_env)
    os.chmod(local_env, 0o600)
    return target


def latest_trace(run_directory: Path = DEFAULT_RUN_DIRECTORY) -> Path:
    """返回指定演示运行目录中修改时间最新的 JSONL 轨迹。"""

    traces = sorted(run_directory.glob("*.jsonl"), key=lambda path: path.stat().st_mtime)
    if not traces:
        raise FileNotFoundError("还没有演示轨迹，请先运行 Sparrow 演示")
    return traces[-1]
