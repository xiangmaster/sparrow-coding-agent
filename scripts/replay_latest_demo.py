"""重放最近一次固定演示的轨迹，不调用模型或执行项目代码。"""

from __future__ import annotations

from sparrow_agent.cli import main as sparrow_main
from sparrow_agent.demo import latest_trace


def main() -> int:
    try:
        trace = latest_trace()
    except FileNotFoundError as exc:
        print(f"错误：{exc}")
        return 2
    print(f"重放轨迹：{trace}")
    return sparrow_main(["replay", str(trace), "--events"])


if __name__ == "__main__":
    raise SystemExit(main())
