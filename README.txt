Sparrow Agent（小麻雀）

Git 仓库：https://github.com/xiangmaster/sparrow-coding-agent

一、项目概述
Sparrow 是一个从零实现的本地编程智能体。用户给出任务与工作区后，大语言模型通过原生 tool calling 选择操作；Sparrow 在本地校验并执行工具，将结果写回上下文，持续完成“决策—行动—观察”循环，直至本地证据满足完成条件。项目未使用任何 Agent 框架，也未依赖服务端托管的文件、Shell 或代码执行工具；上下文管理、工具协议与执行、模型输出解析、循环终止和错误处理均为自行实现。

二、运行方法
要求 Python 3.11 及以上。
1. git clone https://github.com/xiangmaster/sparrow-coding-agent.git
2. cd sparrow-coding-agent && python3 -m venv .venv
3. .venv/bin/pip install -e '.[dev,gui]'
4. 将 .env.example 复制为 .env，按注释配置 DeepSeek 凭据。
5. 启动桌面端：.venv/bin/sparrow-gui
命令行入口：.venv/bin/sparrow run "任务描述" --workspace /path/to/project
运行测试：.venv/bin/python -m pytest

三、架构与特色
系统按交互层、会话层、Agent 核心、Provider、工具与证据层划分。Qt Quick/QML 界面只消费结构化事件；CLI 与 GUI 复用同一套 Agent、工具注册表、工作区边界和完成证据门。Provider 将内部消息与工具定义转换为 DeepSeek Chat Completions 协议，并通过 SSE 增量呈现回复，模型后端与 Agent 控制逻辑彼此隔离。

Sparrow 提供十个本地工具，覆盖文件列表、读取、搜索、目录与文件创建、精确替换、补丁修改、重命名、删除和受限命令执行。文件路径必须通过工作区边界、敏感路径和符号链接检查；命令执行禁用 Shell，并限制工作目录、环境变量、运行时间与输出长度，降低本地执行风险。

多轮会话采用 Thread、Turn、Message 模型持久化，可连续追问并在重启后恢复。上下文超限时，以完整 Assistant 工具调用及其 Tool 结果为单位压缩较早历史，保留原始任务、确定性事实摘要和最近因果链，避免破坏协议完整性。网络异常采用有上限的退避重试；重复工具调用、Token 预算、最大迭代次数和用户取消均对应明确终止状态。

项目的核心设计是完成证据门。Agent 运行前为可操作文件建立 SHA-256 快照，工具执行后持续核对真实变化；模型申请完成时，系统检查其文件声明是否与快照一致，并要求最后一次修改之后存在退出码为零的验证。证据不足会作为观察结果返回模型继续处理，防止模型在缺少可检查事实时仅凭文字宣布完成。

桌面端围绕持续对话组织任务，工具动作以轻量卡片区分；代码修改保存真实前后内容，提供带行号的红删绿增差异、悬停预览和多文件横向浏览。任务、模型响应、工具调用、验证结果与终止原因写入版本化 JSONL 轨迹，可在不请求模型、不重新执行工具的情况下离线回放。
