# Sparrow Agent

Sparrow Agent（小麻雀）是一个从零实现的本地编程智能体。它让大语言模型决定下一步操作，
在用户指定的工作区内执行文件工具或命令，将结果送回模型，并持续循环，直到本地证据能够
证明任务完成。

本项目用于 2026 年南京大学软件学院推免项目考核。仓库地址：
<https://github.com/xiangmaster/sparrow-coding-agent>。

## 核心能力

- **对话式工作台：** 支持同一任务内连续追问、历史任务恢复、任务删除与撤销。
- **真实本地工具：** 支持文件列表、读取、搜索、创建目录、创建文件、精确替换、补丁修改、
  重命名、删除和受限命令执行。
- **可审查修改：** 保存修改前后内容，提供文件标签、悬停预览和带行号的红删绿增差异。
- **流式反馈：** DeepSeek 的文本增量通过 SSE 实时进入同一条 Agent 回复，工具动作以轻量卡片展示。
- **完成证据门：** 最后一次真实修改之后必须存在成功验证，模型声明的文件还必须与工作区快照一致。
- **安全与审计：** 文件工具不能越出工作区或读取敏感路径；运行过程写入本地 JSONL 轨迹，可离线重放。

项目不使用 LangChain、LlamaIndex、OpenAI Agents SDK、AutoGen、CrewAI 等 Agent 框架，
也不调用供应商托管的文件、Shell、代码执行或补丁工具。上下文、工具协议、本地执行、
循环控制、错误处理和完成检查均位于本仓库中；`openai` 包只作为访问 DeepSeek
Chat Completions API 的客户端。

## 工作原理

```text
用户消息
  → 将上下文和本地工具定义发送给 DeepSeek
  → 模型返回说明文字和零个或多个工具调用
  → Sparrow 校验并在本地执行工具
  → 工具结果回填上下文，继续下一轮决策
  → 工作区快照与验证记录进入完成证据门
  → 证据通过后结束，否则把缺失项反馈给模型继续处理
```

桌面端由 Qt Quick/QML、`DesktopController`、`ConversationSession` 和 Agent 核心组成，
通过 Qt 信号在线程之间传递结构化事件，不解析终端文本，也不启动额外的 Web 服务。
CLI 与桌面端复用相同的 Provider、工具注册表、工作区边界和完成证据门。

## 安装

要求 Python 3.11 或更高版本。

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev,gui]'
cp -n .env.example .env
```

在 Sparrow 仓库根目录的 `.env` 中填写 `DEEPSEEK_API_KEY`。该文件已被 Git 忽略；
不要把真实凭据写入代码、文档或目标项目。Sparrow 只读取自身启动目录中的 `.env`，
切换目标工作区不会读取或覆盖目标项目的配置。

## 运行

启动桌面应用：

```bash
.venv/bin/sparrow-gui
```

运行命令行任务：

```bash
.venv/bin/sparrow run "检查项目并修复测试失败" --workspace /path/to/project
```

离线重放一次运行轨迹：

```bash
.venv/bin/sparrow replay /path/to/project/.sparrow/runs/<run-id>.jsonl --events
```

桌面设置与 CLI 参数均可覆盖模型、推理强度、最大迭代次数和累计 Token 预算。
达到迭代或预算上限时，Sparrow 会保留已有修改与轨迹并明确说明停止原因。

## 测试

```bash
.venv/bin/python -m pytest
```

普通测试使用伪造 Provider，不访问网络，也不会产生模型费用。真实 API 冒烟测试默认跳过；
确认 `.env` 已配置后，可显式运行：

```bash
.venv/bin/python -m pytest --run-api-smoke tests/test_api_smoke.py
```

## 数据与安全边界

任务会话、运行轨迹和回收记录位于目标工作区的 `.sparrow/`，该目录默认应保持在
版本控制之外。轨迹可能包含用户任务、模型响应、文件片段和命令输出，请勿随意分享。

`run_command` 会禁用 Shell、清理环境变量并限制工作目录、命令类型、运行时间和输出长度，
但它不是操作系统级沙箱。被执行的项目脚本仍具有当前用户账号的系统权限，因此请只在
可信项目中运行 Sparrow，并始终审查代码差异和验证结果。

更完整的架构与设计取舍见 [TECHNICAL_DESIGN.md](TECHNICAL_DESIGN.md)。
