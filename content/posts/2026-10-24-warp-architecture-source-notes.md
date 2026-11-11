---
title: "【源码】Warp：终端、UI 与 Agent 如何共享状态"
slug: "warp-architecture-source-notes"
date: 2026-10-24T00:00:00+08:00
lastmod: 2026-10-24T00:00:00+08:00
categories:
  - AI Engineering
tags:
  - Warp
  - Rust
  - Terminal
  - Agent
  - Source Code
description: "沿着终端输入、PTY 输出和 Agent 请求阅读 Warp，梳理双前端、Entity/Handle 状态模型、Block 语义、云端编排与本地执行的边界，以及第三方模型和外部 Agent 的接入位置。"
draft: false
toc: true
math: false
---

这次研究 Warp，从一个很实际的问题开始：能否让它使用自己的百炼（Alibaba Cloud Model Studio）模型额度，并保留原生输入框、多轮对话和流式展示？设置里的自定义 endpoint 看起来已经具备这些条件，但配置成功后，发送消息仍可能被 `Out of credits` 阻止。要解释这个现象，需要把模型配置、请求入口、会话状态和服务端协议连起来看。

本文先建立 Warp 的整体结构，再沿着终端和 Agent 两条输入输出路径解释状态如何流动。具体的百炼请求调用图、直连改造、构建过程与运行验证放在[下一篇实践记录]({{< ref "2026-10-24-warp-bailian-native-direct-poc.md" >}})。两篇文章基于同一个源码快照，避免把架构分析和实验过程混成一份目录清单。

源码基线为 `warpdotdev/warp` 的提交 [`1e6c3ae465d6a925a9b97ae03877aaadd159b8f1`](https://github.com/warpdotdev/warp/tree/1e6c3ae465d6a925a9b97ae03877aaadd159b8f1)，阅读与本机验证日期为 **2026-10-24**。文中的上游链接固定到这个提交；百炼直连属于基于此快照的本地改动，不代表上游已经提供该功能。已安装的 Warp、从源码构建的 WarpOss，以及仓库之外的 Warp 云端服务，也分别具有自己的边界。

## 先分清两个前端和两种 server

Warp 的主干是 Rust 工作区。GUI、终端模拟器和 Agent 共用不少模型与基础设施，但不能据此把 GUI 和 TUI 当作同一套界面的不同皮肤。

GUI 桌面版位于 `app/`，使用自定义 WarpUI 的像素布局与 GPU 渲染。macOS 构建需要真实的 Metal 工具链。TUI 位于 `crates/warp_tui/`，使用终端字符网格和相应的输入事件；其元素库在 `crates/warpui_core/src/elements/tui/`。两者共享 `App`、Entity、Handle、动作系统以及一部分业务模型，布局、绘制、输入转换和验证方式分别实现。能通过 TUI 测试，不足以证明 GUI 的 Metal 渲染或鼠标交互正常。

![Warp 的前端、共享状态、终端与 Agent 边界](/assets/images/warp-source-notes/architecture.svg)

*图中的箭头表示主要依赖或数据交互。TerminalServer 是本机 PTY 生命周期管理进程；Warp 服务端承担的云端 Agent 协议属于另一条路径。*

GUI 的 OSS 入口是 [`app/src/bin/oss.rs::main`](https://github.com/warpdotdev/warp/blob/1e6c3ae465d6a925a9b97ae03877aaadd159b8f1/app/src/bin/oss.rs#L10)。它设置 OSS 渠道、应用身份和相关配置，然后进入 [`app/src/lib.rs::run`](https://github.com/warpdotdev/warp/blob/1e6c3ae465d6a925a9b97ae03877aaadd159b8f1/app/src/lib.rs#L780)。同一个库还提供 `run_tui` 等入口。OSS 是构建渠道，不能由这个名称推导出“所有功能都在本机运行”；入口仍会配置服务端地址，具体功能是否访问网络取决于后续模块。

源码中还有一个容易混淆的 `TerminalServer`。它负责创建和管理伪终端（Pseudo Terminal，PTY），通过 Unix domain socket 传递控制消息和文件描述符。拿到描述符后，应用进程可以直接与 PTY 交互，不必把所有终端字节经由该子进程转发。这个设计的目的和远程 AI 推理完全不同，源码注释在 [`local_tty/server/mod.rs`](https://github.com/warpdotdev/warp/blob/1e6c3ae465d6a925a9b97ae03877aaadd159b8f1/crates/warp_terminal/src/local_tty/server/mod.rs#L113) 中写得很明确。

因此，讨论“去掉 Warp server”之前，需要先问清楚：是替换 AI 请求的云端编排，还是改变本机终端的进程管理？百炼直连只涉及前者。

## UI 的核心约束是状态归属

WarpUI 的 `App` 管理模型和视图实体，业务代码通过 `ModelHandle<T>`、`ViewHandle<T>` 引用它们，再经由 `AppContext` 在合适的作用域内读写。这使终端视图、Agent 历史、设置和工作区能够引用同一份状态，而不必各自维护完整副本。入口可从 [`AppContext`](https://github.com/warpdotdev/warp/blob/1e6c3ae465d6a925a9b97ae03877aaadd159b8f1/crates/warpui_core/src/core/app.rs#L600)、`add_model` 和 `add_view` 读起。

这里需要关注的不是类型名字，而是两个操作之间如何衔接：网络或 PTY 事件先修改所属模型，再通过事件订阅、通知和视图更新让界面发生变化。一个异步响应通常不会直接调用“把这段文字画出来”的函数。沿着普通同步调用栈读到网络请求之后就停止，会漏掉后一半的数据流。

这个模型也解释了修改源码时的一类风险。终端模型有锁，渲染和事件处理又可能间接访问它；如果调用栈上已经持有 `TerminalModel` 的锁，下层再次 `lock()`，就可能造成界面冻结。给下层传递已经锁定的引用、缩短锁的持有范围，比在每个函数里自行获取锁更容易维持约束。

工作区、终端 session 和 AI conversation 则是不同层级的状态。工作区组织窗口、标签页和分屏；终端 session 组织 shell 与命令输出；AI conversation 保存任务、消息、请求和执行状态。它们可以相互关联，但修改其中一种 ID、持久化方式或生命周期，不会自动替另外两种完成迁移。这是后续直连改造必须单独处理会话驱动的原因。

## 终端路径：字节怎样成为可操作的 Block

终端模拟器仍需完成传统终端的基本工作：将输入交给 shell，读取 PTY 输出，解析控制序列，更新网格，再通知界面重绘。Warp 在这条路径上叠加了命令块语义，才有条件把某次命令、对应输出、工作目录和结束状态作为一个整体交给用户或 Agent。

读取路径可以压缩成下面的关系。它表示运行时的数据流，并非所有节点都在同一个同步栈上：

```text
shell / child process
    → PTY reader
    → EventLoop::pty_read
    → parser.parse_bytes + ANSI / DCS handlers
    → terminal model / grid / command blocks
    → wakeup and frontend events
    → terminal view / block grid renderer
```

[`EventLoop::pty_read`](https://github.com/warpdotdev/warp/blob/1e6c3ae465d6a925a9b97ae03877aaadd159b8f1/crates/warp_terminal/src/local_tty/event_loop.rs#L201) 在读取到字节后获取终端状态，调用 `state.parser.parse_bytes(...)`，并把终端需要回写的控制序列放入写队列。处理一定量的数据后会让出锁，存在可见更新时发送唤醒事件。这里同时承担吞吐与交互之间的协调：一直持锁解析大批输出，会影响其他需要访问模型的工作。

ANSI 解析位于 [`model/ansi/mod.rs`](https://github.com/warpdotdev/warp/blob/1e6c3ae465d6a925a9b97ae03877aaadd159b8f1/crates/warp_terminal/src/model/ansi/mod.rs)。`Processor` 包装 VTE parser，`Performer` 实现相应的处理接口，把文本与控制序列转换为 Handler 操作。网格实现位于 `crates/warp_terminal/src/model/grid/`，应用侧的终端、session 与 Block 状态则可以继续追到 `app/src/terminal/model/`。

命令边界并非仅凭屏幕上出现了 `$` 或某一段文本推测出来。Warp 的 shell 集成会发送附加控制信息，解析端的 [`dcs_hooks.rs`](https://github.com/warpdotdev/warp/blob/1e6c3ae465d6a925a9b97ae03877aaadd159b8f1/crates/warp_terminal/src/model/ansi/dcs_hooks.rs) 处理相应的 shell 生命周期事件。这些信号使程序能够把命令输入和输出组织为 Block。缺少 shell 集成、远程环境不兼容或程序进入全屏模式时，能获得的语义也会变化，不能把普通文本输出等同于完整命令元数据。

这解释了 Warp 相对于 iTerm2 + tmux 的一个结构差异：它把命令块、编辑输入和 AI 上下文放进同一个应用状态模型。选择一段命令输出继续追问，不必只依赖从终端复制文本。iTerm2 + tmux 的组合则适合依靠 shell、脚本和持久终端会话组织工作；tmux 的会话管理不等于 Warp 的 AI conversation。两者是否适合自己，取决于工作中更需要命令块上下文和原生 Agent 交互，还是既有的远程终端与脚本流程。这是机制上的取舍，本次实验没有做效率对比测量。

## Agent 路径：模型回复之前还有编排协议

在原生 Agent 输入框按下回车，通常会先经过输入模式判断、可用性检查和会话路由，然后才形成请求。关键入口是 [`Input::input_enter`](https://github.com/warpdotdev/warp/blob/1e6c3ae465d6a925a9b97ae03877aaadd159b8f1/app/src/terminal/input.rs#L13924)、`submit_ai_query_with_routing` 和 `submit_ai_query_local`。最后一个函数名字中的 `local` 指这段客户端路由，不能由此判断模型运行在本机。

`BlocklistAIController` 把输入变成 `AIAgentInput`，收集本次请求需要的上下文，再用 `RequestInput` 和 `RequestParams` 快照化模型、任务、设置与提供商配置。上下文可能包含选择的终端块、工作目录和附件等；这不代表它无条件发送所有环境变量或整个仓库。判断某项数据是否会发出，应继续看收集条件、权限和转换代码。

默认请求随后进入 [`generate_multi_agent_output`](https://github.com/warpdotdev/warp/blob/1e6c3ae465d6a925a9b97ae03877aaadd159b8f1/app/src/ai/agent/api/impl.rs#L16)，构造 `warp_multi_agent_api::Request`，通过 [`warp_multi_agent_client`](https://github.com/warpdotdev/warp/blob/1e6c3ae465d6a925a9b97ae03877aaadd159b8f1/crates/warp_multi_agent_client/src/lib.rs#L48) 向 Warp 服务端的 `/ai/multi-agent` 发出请求。请求包含模型配置、任务上下文、可用工具以及自定义提供商等信息。客户端收到的是 Warp 的响应事件协议，和 OpenAI 兼容接口中的文本 delta 不是同一种消息结构。

这个边界决定了“设置了自己的 API key”和“客户端直接请求自己的提供商”是两个不同命题。源码能够证明客户端把自定义 endpoint 及其凭据装入请求，再发给 Warp 服务端；这个仓库没有给出服务端内部的完整模型调用实现，因此无法继续证明其 SDK、提示词、重试与计费细节。

### 回复如何逐步成为原生界面

服务端响应使用服务端发送事件（Server-Sent Events，SSE）作为传输形式，事件数据经过解码形成 `ResponseEvent`。`ResponseStream` 负责异步消费、取消以及相应的错误处理；Controller 再将事件应用到历史模型和会话。GUI 展示发生在状态变化之后。

```text
ResponseStream
    → BlocklistAIController::apply_response_event
    → AIHistoryModel::apply_client_actions
    → AIConversation::apply_client_action
        ├─ AddMessagesToTask       建立用户与助手消息
        └─ AppendToMessageContent  追加指定消息字段
    → UpdatedStreamingExchange / history events
    → AIBlock::render
```

这里最有复用价值的是**消息和视图之间的事件契约**。只要新后端能够提供符合约束的任务、消息 ID、增量内容和完成事件，就有机会保留原生 UI，而无需重新实现 Markdown 展示和对话输入。百炼 PoC 正是在这个位置做适配，下一篇会展开它为什么还需要修改会话身份、同步和错误恢复。

### 工具执行让这条链路形成闭环

完整 Agent 并不在生成一条助手消息之后结束。部分响应要求客户端执行动作，动作模型负责排队、权限确认与执行，再把结果作为后续输入继续发送。源码入口包括 `action_model.rs`、`action_model/execute.rs`，以及 Controller 的 `send_follow_up_for_conversation`。

因此，接入一个 OpenAI 兼容模型接口，最多先解决了模型请求与响应格式。要获得完整 Agent，还需回答：谁维护工具 schema，谁执行审批，工具输出如何编码为下一轮输入，取消时如何处理已启动的进程，以及断流重试是否会重复执行。响应协议中的 `ClientActions` 也不都代表可执行工具；创建消息和追加文本本身同样通过这一类动作传达。PoC 复用了这些 UI 状态动作，没有开放命令执行。

## 第三方接入，分别改变哪一层

在终端内运行 Codex 或 pi，替换原生 Agent 的模型，以及将外部 Harness 的事件显示进原生 UI，改造深度不同。把它们统一称作“支持第三方”会掩盖工作量。

| 接入方式 | 沿用什么 | 需要适配什么 | 当前证据边界 |
| --- | --- | --- | --- |
| 在终端运行第三方 CLI | PTY、shell、终端渲染 | CLI 自己的认证与配置 | 可从终端结构解释；本次未重新验证 Codex / pi CLI |
| 配置原生自定义模型 endpoint | Warp 原生输入、任务、工具及响应协议 | 提供商 schema、模型映射、密钥与服务端策略 | 基线客户端存在对应配置与序列化代码；不等于本机直连 |
| 通过外部 Harness 接入 | 部分原生消息与展示模型 | 会话生命周期、事件映射、状态同步、工具归属 | 仓库已有 ACP 相关映射；需要逐条检查后端与同步依赖 |
| 本次百炼文本直连 | 原生输入、文本历史、流式 UI | HTTP/SSE 到 Warp 事件的适配和独立驱动 | 已验证 macOS GUI 两轮；无工具、无持久化 |

基线的 [`ConversationDriver`](https://github.com/warpdotdev/warp/blob/1e6c3ae465d6a925a9b97ae03877aaadd159b8f1/app/src/ai/agent/conversation_driver.rs#L5) 把这些差异表达为一组行为：是否拥有轮次生命周期、是否本地执行工具、是否根据消息重建输入、是否上报任务状态，以及是否只读和持久化。改造时应该逐项给出答案，避免简单复用一个“看起来接近”的枚举项。

例如，`ExternalHarness` 自己驱动事件和工具，却仍可能参与任务状态上报及本地持久化；ACP 映射模块会把外部更新转换为原生消息，但关联模块仍有绑定服务端会话和上传 transcript 的逻辑。已有适配层说明原生展示可以接收其他事件来源，不能据此宣称它天然不依赖云端。

## 存储与网络边界决定改造范围

本地 SQLite、Warp Drive 同步、账户状态和 AI 服务端任务不是同一个存储层。`crates/persistence/` 中的 Diesel schema 和 migrations 负责本地数据；`app/src/drive/`、`app/src/auth/` 与 GraphQL 相关模块处理各自的云端交互。某个 AI 会话使用了本地模型，并不会自动关闭这些功能。

这个区别在实验中有直接后果。文本直连会话没有 Warp 服务端 conversation token。如果仍按原生云端任务进行状态上报，或者重启时按照云端身份恢复，就会将本地生成的 ID 带入不匹配的协议。最小方案先让它仅驻留内存，明确不进入这些同步和恢复路径；代价是退出进程后无法恢复 PoC 对话。

同样，原生 Agent 的入口还受账户和设置控制。将推理 HTTP 请求改成百炼，仍可能遇到整个 Agent 菜单禁用的问题。实验确实需要正常登录并打开 Agent 总开关。因此，当前结果只能称为**模型推理直连**，不能称为整个应用离线运行。

## 如何继续读，才能为下一次改动服务

如果目标是调整原生模型链路，优先阅读“输入 → RequestParams → 传输 → ResponseEvent → 历史模型 → 视图”这条闭环，再补上工具结果回传与会话驱动。若目标只是使用已有 Codex CLI，则先配置 CLI 和终端流程，未必需要修改 Warp 内部编排。要新增断线恢复或持久化，才需要进一步进入任务身份、数据库与同步模型。

后续源码导航可以按下面的状态问题展开。路径均相对上述固定快照；它们是阅读入口，不代表一个目录就完整负责该能力。

| 要回答的问题 | 关键文件或目录 | 重点对象 / 函数 |
| --- | --- | --- |
| 程序从哪里启动 | `app/src/bin/oss.rs`、`app/src/lib.rs` | `main`、`run`、`run_tui` |
| 谁拥有业务状态 | `crates/warpui_core/src/core/app.rs` | `AppContext`、`add_model`、`add_view` |
| PTY 怎样创建和读取 | `crates/warp_terminal/src/local_tty/` | `TerminalServer`、`EventLoop::pty_read` |
| 字节怎样变成终端操作 | `crates/warp_terminal/src/model/ansi/` | `Processor`、`Performer`、DCS hooks |
| 命令块和 session 如何关联 | `app/src/terminal/model/` | `TerminalModel`、`Session`、`BlockList` |
| 输入怎样成为 Agent 请求 | `app/src/terminal/input.rs`、`app/src/ai/blocklist/controller.rs` | `input_enter`、`send_query`、`send_request_input` |
| 模型和凭据如何解析 | `app/src/ai/llms.rs`、`crates/ai/src/api_keys.rs` | `get_active_base_model`、`ApiKeyManager` |
| 请求发往哪里 | `app/src/ai/agent/api/impl.rs`、`crates/warp_multi_agent_client/src/lib.rs` | `generate_multi_agent_output` |
| 回复怎样改变状态 | `app/src/ai/blocklist/history_model.rs`、`app/src/ai/agent/conversation.rs` | `apply_client_actions`、`apply_client_action` |
| 回复怎样显示 | `app/src/ai/blocklist/block/view_impl.rs` | `AIBlock::render` |
| 工具怎样执行和回传 | `app/src/ai/blocklist/action_model/`、Controller | `try_to_execute_action`、`send_follow_up_for_conversation` |
| 谁负责会话生命周期 | `app/src/ai/agent/conversation_driver.rs` | `ConversationDriver` 的行为谓词 |
| 本地状态怎样落盘 | `crates/persistence/` | schema、migrations、调用侧恢复条件 |

对我这次的目标，研究源码是有回报的：它解释了自定义 endpoint 仍经过服务端的原因，也找到了复用原生 UI 的事件边界。下一步的工程判断则更具体：先用一个固定模型、纯文本、无工具的闭环验证这个边界，再决定是否值得承担持久化、工具协议和账户解耦的工作。实际改动与已验证范围见[百炼直连 PoC 记录]({{< ref "2026-10-24-warp-bailian-native-direct-poc.md" >}})。
