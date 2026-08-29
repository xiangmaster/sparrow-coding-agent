# Sparrow Agent

Sparrow Agent（小麻雀）是一个从零实现、便于理解和检查的编程智能体。
它让大语言模型选择下一步操作，在本地执行该操作，将结果返回模型，
并重复这一过程，直到有可检查的证据说明编程任务已经完成。

本项目用于 2026 年南京大学软件学院推免项目考核。

## 设计目标

- **易于理解：** Agent 循环和所有本地工具均由本仓库自行实现。
- **默认安全：** 文件操作不得越出工作区，命令执行受到时间和输出长度限制。
- **可验证：** 最后一次修改之后必须有有效验证，否则 Agent 不接受“任务完成”。
- **可审计：** 记录模型决策、工具调用、执行结果和终止原因，并支持离线重放。
- **可替换：** 模型后端被隔离在简洁的 Provider 接口之后。

默认后端为 DeepSeek-V4-Pro，通过 DeepSeek 兼容 OpenAI 格式的
Chat Completions API 访问。开发阶段可切换至 DeepSeek-V4-Flash，
以更低成本完成高频测试，无需修改 Agent 实现。

## 计划中的工作流

```text
用户任务
  -> 连同工具定义请求模型
  -> 模型返回零个或多个本地工具调用
  -> 将工具结果追加至对话上下文
  -> 模型申请完成，本地完成门检查修改与验证证据
  -> 证据充足则结束，否则返回缺失项继续循环
```

首个版本提供工作区文件列表、文件读取、文本搜索、补丁修改和
受限命令执行能力。项目不使用任何 Agent 框架，也不调用 API 服务端托管的
文件、Shell、代码执行或补丁工具。

## 当前状态

已经完成内部数据模型、工作区安全边界、五个本地工具、DeepSeek Provider、
上下文管理、Agent 主循环和完成证据门。确定性离线场景已覆盖“读取、修改、
验证失败、再次修改、验证成功、申请完成”的完整纠错链路。
当前还支持版本化 JSONL 运行轨迹、无副作用离线重放和完整命令行入口。
下一阶段将准备固定演示项目，并完成真实端到端验收。

每次启用记录器运行时，版本化 JSONL 轨迹和中文摘要日志会写入被 Git 忽略的
`.sparrow/runs/`，文件权限为 `0600`。轨迹包含用户任务、模型响应、
`reasoning_content` 和工具输出，便于复盘但也可能含有项目敏感信息，请勿随意分享。

## 安装与使用

项目要求 Python 3.11 及以上：

```bash
python3.13 -m venv .venv
.venv/bin/pip install -e '.[dev]'
cp -n .env.example .env
```

在本地 `.env` 中填写 `DEEPSEEK_API_KEY`，随后运行：

```bash
.venv/bin/sparrow run "检查项目并修复测试失败" --workspace .
```

命令会实时显示模型轮次和工具结果，结束时输出明确终止原因。默认轨迹位于
`.sparrow/runs/`，可以在不请求模型、不执行工具的情况下重放：

```bash
.venv/bin/sparrow replay .sparrow/runs/<run-id>.jsonl --events
```

返回码 `0` 表示完成证据门已通过，`2` 表示参数或配置错误，`3` 表示任务未完成，
`130` 表示用户取消。`run_command` 具有防护边界，但不是操作系统级沙箱；
请只在你信任的项目中运行 Sparrow。

真实 API 冒烟测试默认跳过。将 `.env.example` 复制为不会被 Git 跟踪的 `.env`，
在其中设置 `DEEPSEEK_API_KEY` 后，使用
`pytest --run-api-smoke tests/test_api_smoke.py` 显式执行。测试只读取项目根目录的
`.env`，不会回退到全局环境；该测试固定采用
`deepseek-v4-flash`、低推理强度和较小输出上限，并会产生少量 API 费用。

设计原理见 [TECHNICAL_DESIGN.md](TECHNICAL_DESIGN.md)，交付计划见 [ROADMAP.md](ROADMAP.md)。
