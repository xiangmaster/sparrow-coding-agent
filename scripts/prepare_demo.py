"""从只读样例准备一个可重复修改的 Sparrow 演示工作区。"""

from __future__ import annotations

import argparse

from sparrow_agent.demo import prepare_demo_workspace


def main() -> int:
    parser = argparse.ArgumentParser(description="准备可重复的 Sparrow 固定演示项目")
    parser.parse_args()
    try:
        target = prepare_demo_workspace()
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(f"演示工作区已重建：{target}")
    print("本地 .env 已复制且权限设为 0600；不会显示或提交其中的密钥。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
