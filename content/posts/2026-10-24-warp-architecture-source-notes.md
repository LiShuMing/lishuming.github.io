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
description: "从现代终端与 AI Agent 的三个核心矛盾出发，拆解 Warp 的双前端分工、Entity/Handle 状态归属、PTY 到命令 Block 的解析链路，以及原生 Agent 协议与本地直连的改造边界。"
draft: false
toc: true
math: false
---

传统的终端模拟器（比如 xterm 或 iTerm2）建立在一个极其克制的抽象之上：子进程往伪终端（Pseudo Terminal，PTY）里吐出一串字节流和 ANSI 转义序列，终端负责把字符画到二维网格上。画完之后，终端本身并不知道哪几行属于上一次 `cargo test` 的报错、当时的工作目录在哪里、退出码是多少。

如果你想在这个传统字符网格旁边硬塞一个原生的 AI Agent，最朴素的做法是“抓取屏幕上的文字拼进 Prompt，调完大模型接口再把返回的字符串画到侧边栏”。但只要动手写下去，马上就会撞上三堵墙：

1. **终端输出缺乏结构边界**：仅凭屏幕上出现了 `$` 或 `#` 去猜命令起止，一旦遇到自定义提示符、并发交错输出或者 `vim`、`htop` 这类全屏程序，上下文切分就会彻底乱套。
2. **高频异步 I/O 与多视图渲染抢状态**：一边是 PTY 每秒涌入几万字节的编译日志，另一边是网络流式推送的模型回复，外加窗口分屏、历史列表乃至桌面 GPU 与无头终端两套前端同时读写。如果异步回调直接去改界面组件，或者在调用栈里随意嵌套加锁，不是数据撕裂就是界面卡死。
3. **“配了自己的模型密钥”不等于“本地直连推理”**：这也是促使我翻开 [Warp](https://github.com/warpdotdev/warp) 源码的直接导火索——我在设置里填好了阿里云百炼（Alibaba Cloud Model Studio）的自定义 Endpoint 和 API Key，本以为客户端会直接把请求发往百炼，结果按下回车后，依然可能被 Warp 自己的 `Out of credits` 拦在门外。

为什么填了自己的 API Key 还会受制于官方额度？终端里的字节流到底怎样变成可引用的命令块（Block）？如果我们想绕开云端中转、复用 Warp 原生输入框和流式界面直连自己的大模型，刀该切在哪一层？本篇先顺着终端和 Agent 两条数据流把 Warp 的骨架拆清楚；具体的百炼直连改造、Metal 编译踩坑与实机验证则记录在[下一篇实践笔记]({{< ref "2026-10-24-warp-bailian-native-direct-poc.md" >}})里。

---

## 一、双前端与两种“Server”：先拆开名字里的歧义

初看 Warp 的 Rust 工作区，最容易让人产生误判的是两对概念：**GUI 与 TUI 两套前端**，以及 **本地 `TerminalServer` 与云端 Agent 服务**。

![Warp 的前端、共享状态、终端与 Agent 边界](/assets/images/warp-source-notes/architecture.svg)

*箭头表示主要依赖或数据流向。左下方的 `TerminalServer` 是管理本机 PTY 生命周期的子进程；右下方的 Warp 云端服务则承载 `/ai/multi-agent` 编排协议，两者职责完全正交。*

先看顶层的两个前端。桌面图形版位于 `app/`，基于自研的 WarpUI 框架做像素级布局和 GPU 渲染（在 macOS 上依赖完整的 Metal 着色器工具链）；而无头终端版位于 `crates/warp_tui/`（基础组件在 `crates/warpui_core/src/elements/tui/`），直接画在字符网格上并消费终端输入事件。虽然开源桌面入口 [`app/src/bin/oss.rs::main`](https://github.com/warpdotdev/warp/blob/1e6c3ae465d6a925a9b97ae03877aaadd159b8f1/app/src/bin/oss.rs#L10) 和 TUI 入口最终都汇入 [`app/src/lib.rs::run`](https://github.com/warpdotdev/warp/blob/1e6c3ae465d6a925a9b97ae03877aaadd159b8f1/app/src/lib.rs#L780) 所在的库，共用底层的 `App`、实体句柄（Entity / Handle）、动作派发和核心业务模型，但两者的布局计算、绘制管线与输入事件转换是各自独立实现的——跑通了 TUI 单元测试，依然要用真实的 Metal 构建去验证桌面窗口的渲染和鼠标交互。顺带一提，`oss.rs` 里的 `OSS` 只是构建渠道标识，入口初始化时照样会装配默认的服务端地址，具体功能是否联网完全取决于下游模块。

再看底层的两种“Server”。翻源码时看到 [`local_tty/server/mod.rs`](https://github.com/warpdotdev/warp/blob/1e6c3ae465d6a925a9b97ae03877aaadd159b8f1/crates/warp_terminal/src/local_tty/server/mod.rs#L113) 里的 `TerminalServer`，很容易误以为它跟远程服务有关。实际上，它是一个纯本机运行的守护子进程，专门负责创建和托管本地 PTY 的生命周期，并通过 Unix Domain Socket 把控制消息与底层文件描述符（File Descriptor）移交给主程序。一旦主程序拿到了 PTY 文件描述符，后续海量的终端字节读写就直接在主程序与 PTY 之间完成，不需要每敲一个字符都去绕一趟子进程转发。

搞清了这层分工，“绕开 Warp 服务端做模型直连”的目标就非常清晰了：**我们要接管的是右下方发往 `/ai/multi-agent` 的云端 AI 编排请求，而左下方管理本地 Shell 的 `TerminalServer` 属于终端基础设施，一丁点都不需要动。**

---

## 二、UI 状态归属：Entity / Handle 模型与锁边界

设想一个日常瞬间：后台编译正在往终端狂吐日志，用户用鼠标滚轮往上翻看报错，与此同时，右侧的 Agent 恰好收到了下一段流式回复。三股力量在同一毫秒读写界面相关的数据，WarpUI 是怎么防止它们撞车的？

答案藏在 [`AppContext`](https://github.com/warpdotdev/warp/blob/1e6c3ae465d6a925a9b97ae03877aaadd159b8f1/crates/warpui_core/src/core/app.rs#L600)、`add_model` 和 `add_view` 构成的实体归属模型里。WarpUI 的全局 `App` 统一持有所有业务模型与视图实体的所有权，外部代码手里拿到的只是轻量级的 `ModelHandle<T>` 和 `ViewHandle<T>` 引用。终端面板、Agent 历史、设置页和工作区不需要各自拷贝一份数据副本，而是通过句柄在受控的 `AppContext` 作用域内读写同一份真值。

这也是读 Warp 源码时最关键的思维转换：**异步 I/O 绝不会直接调用“把这段字画到屏幕上”的函数。** 无论是 PTY 读到了新字节，还是网络流解出了新事件，第一步永远是更新对应的 Model，再通过事件订阅与脏标记通知（Notification）驱动 View 在下一帧重绘。如果你顺着普通同步调用栈一路追到发出 HTTP 请求就停下，刚好会漏掉“状态变更如何反向点亮界面”的后半截闭环。

这套状态共享机制还决定了改代码时的一条铁律——**严防跨层重入死锁**。以核心的 `TerminalModel` 为例，它内部受互斥锁保护，而视图渲染和事件分发又经常需要间接读取终端状态。如果上层调用栈已经拿住了 `TerminalModel` 的锁，调进某个辅助函数后又试图二次 `.lock()`，整个图形界面就会当场冻死。因此在终端相关模块里，显式把已锁定的引用往下传、尽量压缩持锁区间，远比每个小函数各自去抢锁安全得多。

往下细分，客户端里的核心状态其实分属三个独立的层级：

- **工作区（Workspace）**：管窗口、标签页（Tab）与分屏布局；
- **终端会话（Terminal Session）**：管具体的 Shell 进程、PTY 通道与命令输出块；
- **AI 会话（AI Conversation）**：管任务（Task）、对话消息、请求快照与工具执行状态。

这三层状态虽然可以互相引用（比如在某次 AI 提问里附带某个终端分屏的某块输出），但各自的 ID 体系、落盘表结构和生命周期彼此独立。理解了这一点，就能明白为什么后面做百炼直连时，光改 HTTP 请求远远不够，还必须为直连对话单独定义会话驱动（Conversation Driver）——因为终端活着，不代表云端 AI 会话的同步和恢复逻辑会自动放过你的本地对话。

---

## 三、终端路径：无结构字节流怎样变成可操作的 Block？

回到终端主业。要把 Shell 吐出的原始字节流变成既能由 GPU 高速渲染、又能被用户一键框选塞进 Agent 上下文的命令块（Block），客户端需要把原始 PTY 输出与 Shell 钩子注入的控制序列汇入同一条解析管线：

![Warp 从无结构 PTY 字节流到结构化 Block 的数据流](/images/diagrams/app-warp-pty-block-pipeline.svg)

*图中展示的是运行时数据流向，跨阶段通过 `AppContext` 脏标记与前端唤醒事件衔接，并非单一同步调用栈。*

顺着源码看，这条链路上解决了两个硬核工程问题。

第一个问题是**高吞吐输出与界面响应的平衡**。打开 [`EventLoop::pty_read`](https://github.com/warpdotdev/warp/blob/1e6c3ae465d6a925a9b97ae03877aaadd159b8f1/crates/warp_terminal/src/local_tty/event_loop.rs#L201) 可以看到，当后台读取线程从 PTY 拿到一批字节后，会获取终端状态锁并调用 `state.parser.parse_bytes(...)`（底层在 [`model/ansi/mod.rs`](https://github.com/warpdotdev/warp/blob/1e6c3ae465d6a925a9b97ae03877aaadd159b8f1/crates/warp_terminal/src/model/ansi/mod.rs) 中用 `Processor` 包装 VTE 解析器，由 `Performer` 将控制序列转译为 `crates/warp_terminal/src/model/grid/` 上的网格变更，并把需要回传给终端的控制响应塞入写队列）。试想，如果跑一句 `cat huge.log` 吐出几百兆日志，`pty_read` 死死抱住状态锁一直解析到文件末尾，主线程就根本拿不到锁去画界面。所以 `pty_read` 专门做了定量分批处理：消化一定字节后主动让出锁，并在产生可见变更时向前端发唤醒事件，让渲染帧得以插空执行。

第二个问题是**命令边界的精确识别**。Warp 并不是靠正则去屏幕文本里猜哪里是提示符，而是通过 Shell 集成脚本，在命令即将执行、刚刚结束等生命周期节点主动注入设备控制字符串（Device Control String，DCS）转义序列，由解析侧的 [`dcs_hooks.rs`](https://github.com/warpdotdev/warp/blob/1e6c3ae465d6a925a9b97ae03877aaadd159b8f1/crates/warp_terminal/src/model/ansi/dcs_hooks.rs) 捕获并通知上层 `app/src/terminal/model/`。有了这套带外信号，客户端就能准确切分出每次执行的命令文本、标准输出、当前工作目录与退出状态，组装成结构化的 Block（反过来，如果连上一台没装 Shell 集成的裸远端机器，或者切进全屏 TUI 程序，能拿到的元数据自然也会随之退化）。

这也道出了 Warp 与经典的 `iTerm2 + tmux` 工作流在根子上的分野：`tmux` 把会话持久化和分屏复用留在了终端字节流协议内部，跟远端服务器脚本配合得天衣无缝；而 Warp 选择把命令块、富文本输入框和 AI 上下文全部提升到客户端的应用状态层——当你点中一段编译失败的 Block 继续追问 Agent 时，传过去的是天然带边界的结构化对象，而不是从字符网格里生抠出来的一段纯文本。

---

## 四、Agent 路径：为什么配了自定义密钥还会走 Warp 云端？

现在我们可以解开文章开头的那个谜团了：在原生 Agent 输入框敲下回车，请求究竟去了哪里？

顺着 [`Input::input_enter`](https://github.com/warpdotdev/warp/blob/1e6c3ae465d6a925a9b97ae03877aaadd159b8f1/app/src/terminal/input.rs#L13924) 往下追，输入先经过模式判定、额度与可用性预检，再走 `submit_ai_query_with_routing` 进入 `submit_ai_query_local`（注意：函数名里的 `local` 指的是“当前客户端本地分屏路由”，用来区别共享会话转发，跟本地跑大模型毫无关系）。接着，`BlocklistAIController` 把用户输入封装成 `AIAgentInput`，按当前选中项、权限与脱敏规则收集引用的命令块、工作目录或附件，并构造 `RequestInput` 与 `RequestParams` 快照，把这一轮的模型标识、任务上下文和自定义提供商配置冻结下来。

真正的分水岭出现在下一步——[`generate_multi_agent_output`](https://github.com/warpdotdev/warp/blob/1e6c3ae465d6a925a9b97ae03877aaadd159b8f1/app/src/ai/agent/api/impl.rs#L16)：

1. 客户端把 Prompt、任务历史、可用工具清单，**连同你在设置里配好的自定义 Endpoint 地址与 API Key（`custom_model_providers`）一起**，打包进 `warp_multi_agent_api::Request`（Protobuf 结构）；
2. 随后交给 [`warp_multi_agent_client`](https://github.com/warpdotdev/warp/blob/1e6c3ae465d6a925a9b97ae03877aaadd159b8f1/crates/warp_multi_agent_client/src/lib.rs#L48)，附带 Warp 账号鉴权令牌，统一 POST 到 **Warp 云端服务端的 `/ai/multi-agent` 接口**；
3. 云端完成多智能体编排、提示词组织与上游模型调用后，再把结果编码成 Warp 私有的响应事件流推回客户端。

真相一目了然：原生的“自定义 Endpoint”在架构上属于**服务端托管转发（Cloud-orchestrated BYOK）**，而不是客户端本机直连。正因为整条链路依然穿过客户端的额度预检门禁（`AIRequestUsageModel`）和云端的 `/ai/multi-agent` 编排层，哪怕你自己的百炼账户里额度充足，只要撞上客户端或服务端的配额策略，请求照样会被 `Out of credits` 拦下来。

### 事件契约：流式回复如何点亮原生界面？

既然 `/ai/multi-agent` 返回的不是 OpenAI 兼容接口那种简单的 `choices[].delta.content` JSON，客户端是怎么把回复渲染出来的？追进响应消费链路就能看到一套非常干净的分层：

```text
ResponseStream（消费 SSE，经 Base64 与 Protobuf 解码出 ResponseEvent）
    → BlocklistAIController::apply_response_event
    → AIHistoryModel::apply_client_actions
    → AIConversation::apply_client_action
        ├─ AddMessagesToTask       在任务下创建用户与助手消息槽位
        └─ AppendToMessageContent  按消息 ID 与字段掩码追加增量文本
    → UpdatedStreamingExchange / history events
    → AIBlock::render（驱动原生 Markdown 流式视图重绘）
```

这段链路里最值得标星号的设计，就是**模型层与视图层之间的 `ClientActions` 事件契约**。`AIBlock::render` 根本不关心网络那头连的是 Warp 官方云端还是别的什么服务；它只认 `AIConversation` 里的状态变更。只要我们在本地新写一个轻量 HTTP 客户端，直连百炼拿到标准的服务端发送事件（Server-Sent Events，SSE）文本增量，再当场把它们翻译成符合顺序和 ID 约束的 `AddMessagesToTask`、`AppendToMessageContent` 与完成事件喂给 `ResponseStream`，整套原生的输入框、代码高亮、流式滚动和多轮追问界面就能原封不动地跑起来！

### 工具调用为什么会让复杂度陡增？

当然，完整的终端 Agent 不只是聊聊天。在原版闭环里，如果服务端返回的 `ClientActions` 包含执行 Shell 命令或读写文件的动作，就会交由 `action_model.rs`、`action_model/execute.rs` 排队、弹出用户审批确认并在本地执行，最后通过 Controller 的 `send_follow_up_for_conversation` 把执行结果编码成下一轮输入发回服务端。

也就是说，`ClientActions` 既承载了纯 UI 级别的消息创建与文本追加，也承载了带副作用的本地工具调用。如果我们一上来就想把工具循环也改成客户端直连，就得在本地接手工具 Schema 维护、权限弹窗对接、中断时子进程清理、以及断流重试时的防重复执行等一整套编排逻辑。反过来，先只复用消息类的 `ClientActions` 跑通纯文本直连，就能用最小的改动验证原生 UI 解耦的可行性。

---

## 五、第三方接入的四条路线与存储、账户边界

平时大家聊“在 Warp 里接第三方模型或外部 Agent”，其实往往把四种改造深度完全不同的方案混在了一起。把它们放到同一张架构切面图上对比，各自的代价就一清二楚了：

| 接入方式 | 复用了 Warp 的哪一层 | 需要适配或重写的环节 | 运行边界 |
| --- | --- | --- | --- |
| **在终端分屏里直接跑 Codex / Pi 等 CLI** | 本地 PTY、Shell 渲染与分屏管理 | 仅需配置 CLI 自身的鉴权与模型路由 | 纯终端子进程交互，不经过 Warp 原生 Agent 面板 |
| **在设置里填原生自定义 Endpoint** | 原生输入框、任务历史、工具闭环与云端协议 | 按提供商要求填写 Schema、模型映射与密钥 | 请求仍发往 Warp `/ai/multi-agent` 云端，受配额与服务端策略约束 |
| **通过外部 Harness（如 ACP）接入** | 部分原生消息结构与 `AIBlock` 展示组件 | 会话生命周期、事件映射、工具归属与同步裁剪 | 仓库虽有 ACP 转换层，但关联模块仍带有绑定云端会话与上传记录的逻辑 |
| **客户端原生推理直连（本次百炼 PoC）** | 原生输入框、多轮文本历史与流式渲染 UI | 新增 HTTP/SSE 客户端、事件翻译器与独立会话驱动 | 客户端直接请求百炼接口；首期仅支持纯文本内存会话，暂不含工具与持久化 |

Warp 源码里其实专门抽出了一个枚举来刻画不同接入方式的行为边界——[`ConversationDriver`](https://github.com/warpdotdev/warp/blob/1e6c3ae465d6a925a9b97ae03877aaadd159b8f1/app/src/ai/agent/conversation_driver.rs#L5)。它用一组明确的行为谓词回答六个问题：谁掌控轮次生命周期？谁在本地执行工具？是否从消息流重建输入？是否向云端上报任务状态？界面是否只读？会话是否落盘持久化？

千万别小看这六个开关。举个例子，`ExternalHarness` 虽然自己驱动事件和工具，但默认仍可能触发任务状态上报或本地持久化；如果做直连改造时图省事借用了一个看似相近的现有枚举，本地随手生成的任务 ID 就可能被拿去请求云端同步接口，或者在重启恢复时撞上云端 `conversation_token` 校验报错。

这也引出了最后两道横切边界——**本地存储**与**账号门禁**：

- **持久化与云同步各自独立**：`crates/persistence/` 下的 SQLite（Diesel Schema 与 Migrations）管本地落盘，`app/src/drive/`、`app/src/auth/` 与 GraphQL 模块管云端资产与账户同步。直连会话既然没有云端颁发的 `conversation_token`，最稳妥的起步策略就是给它单独定义一个 `DirectBailian` 驱动，明确关闭云端状态上报和重启恢复，让会话先纯粹驻留在内存里（代价是退出应用后直连历史清空，但彻底杜绝了脏 ID 污染数据库）。
- **推理直连不等于整个客户端免登录离线化**：GUI 启动时的引导流程，以及原生 Agent 面板是否可用（如 `Settings → Agents → Warp Agent` 总开关），依然受账户登录态与基础设置控制。把推理 HTTP 请求切到百炼，解决的是“谁来跑大模型、扣谁的 Token”，客户端自身的账号入口门禁仍在原位。

---

## 六、源码走读地图

如果你也想基于 Warp 源码修改模型路由、接入内部网关，或者单纯想读懂一个大型 Rust GUI 终端是怎么组织的，顺着下面这张“问题驱动”的导航表往下跳，比按字母顺序翻目录高效得多：

| 想搞清楚的问题 | 关键目录或文件 | 核心入口类型 / 函数 |
| --- | --- | --- |
| 开源构建与双前端从哪启动 | `app/src/bin/oss.rs`、`app/src/lib.rs` | `main`、`run`、`run_tui` |
| 谁持有全局模型与视图实体 | `crates/warpui_core/src/core/app.rs` | `AppContext`、`add_model`、`add_view` |
| 本地 PTY 怎样创建与分批读取 | `crates/warp_terminal/src/local_tty/` | `TerminalServer`、`EventLoop::pty_read` |
| 原始字节流怎样解析成终端操作 | `crates/warp_terminal/src/model/ansi/` | `Processor`、`Performer`、`dcs_hooks.rs` |
| 命令 Block 与终端会话如何关联 | `app/src/terminal/model/` | `TerminalModel`、`Session`、`BlockList` |
| 敲下回车怎样变成一轮 Agent 请求 | `app/src/terminal/input.rs`、`app/src/ai/blocklist/controller.rs` | `input_enter`、`send_query`、`send_request_input` |
| 模型选择与自定义密钥如何装配 | `app/src/ai/llms.rs`、`crates/ai/src/api_keys.rs` | `get_active_base_model`、`ApiKeyManager` |
| 默认请求打包后发往哪里 | `app/src/ai/agent/api/impl.rs`、`crates/warp_multi_agent_client/src/lib.rs` | 两处 `generate_multi_agent_output` |
| 流式事件怎样一步步改写对话历史 | `app/src/ai/blocklist/history_model.rs`、`app/src/ai/agent/conversation.rs` | `apply_client_actions`、`apply_client_action` |
| 状态更新后由谁渲染成界面气泡 | `app/src/ai/blocklist/block/view_impl.rs` | `AIBlock::render` |
| 工具动作怎样审批、执行与回传 | `app/src/ai/blocklist/action_model/`、Controller | `try_to_execute_action`、`send_follow_up_for_conversation` |
| 不同后端怎样约束会话生命周期 | `app/src/ai/agent/conversation_driver.rs` | `ConversationDriver` 及其行为谓词 |
| 本地历史按什么条件落盘与恢复 | `crates/persistence/` | Diesel Schema、Migrations 与调用侧恢复判据 |

摸清了这条“输入快照 → 传输协议 → `ClientActions` → `AIHistoryModel` → `AIBlock`”的主干，下一步的动手方案就水到渠成了：在 `generate_multi_agent_output` 构造云端请求之前劈开一条本地直连分支，用百炼的 `deepseek-v4.1-flash` 跑通纯文本流式对话。具体的协议翻译细节、macOS Metal 工具链构建排障以及 GUI 实测过程，请看下一篇[《Warp 原生直连百炼：从请求链路到 GUI 验证》]({{< ref "2026-10-24-warp-bailian-native-direct-poc.md" >}})。

---

> **源码基线说明**：本文源码链接与模块路径固定在 [`warpdotdev/warp`](https://github.com/warpdotdev/warp/tree/1e6c3ae465d6a925a9b97ae03877aaadd159b8f1) 上游提交 `1e6c3ae`（源码阅读与本机验证日期为 2026-10-24）。仓库涵盖客户端 GUI、TUI 与共享 Rust Crates，不包含 Warp 闭源云端 `/ai/multi-agent` 服务端实现；文中提及的百炼客户端直连属于基于该快照的本地实验分支改动。
