---
title: "【源码】OpenAI Codex：源码理解、实验与 develop 笔记"
date: 2026-08-22T00:00:00+08:00
lastmod: 2026-10-07T00:00:00+08:00
categories:
  - AI Engineering
tags:
  - Codex
  - Rust
  - Agent
  - App Server
  - TUI
  - MCP
  - Source Code
description: "基于本机 WSL2 环境和 openai/codex 源码，从 CLI、App Server、codex-core、协议、沙箱与持久化出发，记录一次可复现的源码阅读、实验和 develop 思考。"
draft: false
toc: true
math: false
---

作为一名日常重度依赖终端编程助手的工程师，我现在每天打交道最多的工具已经变成了 Codex（相比之下 Claude Code 的日常账单对我来说确实偏高，Codex 成了更务实的主力选择）。用得越频繁，心里就越痒：最初我以为它不过是一个“在终端里调大模型 API、拿到命令跑一下再把输出塞回去”的命令行脚本；可真正在重度任务里用起来，很快就会撞上一连串脚本思维解释不通的问题。

设想一下，假如我们用最朴素的同步 `while` 循环去写一个编码 Agent——每次拼接历史消息发一次 HTTP 请求、等完整响应返回、调用 `subprocess.run()` 执行 Shell 命令、再进下一轮循环——哪怕只跑十分钟真实开发任务，立刻就会在四个地方卡死：

1. **流式输出与并发工具保序冲突**：模型一边流式吐出推理过程（Reasoning），一边同时发出三个只读文件搜索和一条有状态的构建命令。同步阻塞会让终端界面彻底假死；而如果简单用无锁并发跑完所有工具，先跑完的短命令就会排到慢命令前面，打乱模型预期的工具输出顺序。
2. **在途中断与运行中“补一句话”**：测试卡住时用户按下 `Ctrl-C`，或者在 Agent 读代码读到一半时敲入一句补充约束（“别动公共接口，只修单测”）。阻塞调用栈既没法安全撤回半截子进程，也没法篡改正在飞行的 HTTP 请求，只能在安全的采样边界把新输入并入上下文。
3. **反向审批引发的双向等待死锁**：当模型试图写入仓库外的目录时，后端运行时必须原地挂起当前工具调用，**反向**往客户端发一条审批请求等待人工拍板；如果客户端此时恰好在同步等待上一条指令的响应、暂时没去消费事件管道，有界队列一旦塞满，双方就会立刻陷入循环死锁（Deadlock）。
4. **提示词守不住操作系统的物理边界**：系统提示词写一百遍“禁止修改系统目录”，也拦不住一条写歪的脚本误删宿主机文件，或者顺手篡改 `.git/hooks` 在用户下次敲 `git status` 时静默提权。权限约束必须下沉到宿主操作系统的内核沙箱原语里。

读过 OpenAI 那篇 [Codex as a platform: build on the open agent harness](https://developers.openai.com/blog/codex-as-a-platform) 后再扎进 `openai/codex` 的 Rust 主干源码，整座大厦的设计脉络就彻底清晰了：开源仓库交付的正是这套被称为 **Agent Harness** 的本机运行时底座（云端模型推理与桌面端商业托管服务则位于仓库边界之外）。它把 TUI、非交互 `codex exec` 与 IDE 插件全部收敛到统一的 **App Server 控制面**之下，用 **提交队列（Submission Queue）与事件队列（Event Queue）** 驱动核心状态机，配合正交的审批/内核沙箱策略与“JSONL 事件日志 + SQLite 物化索引”双层存储，把不可控的模型采样关进了工程边界清晰的笼子里。

---

## 一、从一次 `codex exec` 追踪全链路控制面与 Turn 状态机

### 1. 为什么连进程内的 TUI 和 `exec` 都要绕道 App Server？

打开 `codex-rs`（当前 Cargo Workspace 已从早期的 61 个主要目录拆细至 135 个成员 Crate，仓库自身的 `AGENTS.md` 甚至专门叮嘱贡献者“克制往 `codex-core` 里塞新代码的冲动”），程序总入口 `codex-rs/cli/src/main.rs` 是一个典型的 Unix 多工具分发器：启动时先经 `arg0_dispatch_or_else` 检查 `argv[0]`——同一个二进制既能化身为 Linux 沙箱辅助进程或 `apply-patch` 内部助手，也能正常按 Clap 子命令分发到默认交互界面 `codex-tui`、非交互批处理 `codex-exec`、`app-server`、`mcp-server` 或 `sandbox/debug` 诊断工具。

顺着 `codex exec "解释这个仓库"` 往里追，最让人眼前一亮的设计就浮出水面了：**TUI 并不是系统的中心，App Server 也绝不只是给外部 VS Code 插件准备的旁路服务。** 无论是 `codex-exec` 还是 `codex-tui`（见 `tui/src/app_server_session.rs`），都会通过共享的 `InProcessAppServerClient` 在进程内拉起一套完整的 App Server，统一发送 `ClientRequest::ThreadStart` 与 `ClientRequest::TurnStart`，再持续消费 `ServerNotification` 事件流直至收到 `TurnCompleted`。

`app-server-client` 在进程内走零序列化的强类型通道（Typed Channel），跨进程时才切换为 JSON-RPC / WebSocket，但两边严格遵守同一套请求、响应与通知契约。初看似乎比“TUI 直接调 `codex-core` 函数”多绕了一层，细想却像极了成熟数据库把 SQL 前端、RPC 协议与底层执行引擎的契约钉死在一起：

- TUI、批处理 CLI 与外部 IDE 对会话（`Thread`）和回合（`Turn`）的生命周期语义百分之百对齐，控制面协议每天都在被主产品路径高频锤炼，不会退化成无人维护的二等公民；
- 收到 `thread/start` 后，服务端不仅返回 `ThreadStartResponse` 告知调用方“请求已受理”，还会向所有监听者广播 `ThreadStartedNotification` 宣告“系统状态已演进”——在多客户端挂载与断线重连场景下，把“命令确认”与“状态广播”拆开远比单靠函数返回值稳健。

把客户端控制面、底层 `codex-protocol` 定义的 **SQ/EQ（Submission Queue / Event Queue）** 双队列、`RegularTask::run_turn` 核心采样循环、审批沙箱以及双层持久化串成一张完整流程图，单次用户提问在 Harness 内部的异步流转轨迹如下（亦可打开[交互式运行时流转图](/diagrams/aug-ai-codex-sq-eq-turn-loop.html)分阶段查看各泳道细节）：

![OpenAI Codex 异步控制面、SQ/EQ 双队列与 Agent Turn 执行闭环：从客户端 ThreadStart/TurnStart 入队 Op::UserTurn，经 RegularTask::run_turn 上下文压缩与流式采样、读写锁并发工具沙箱执行，由无界本地缓冲推送 Event 直至 TurnCompleted](/images/diagrams/aug-ai-codex-sq-eq-turn-loop.svg)

### 2. `Thread / Turn / Item` 三级粒度与 `run_turn` 双层循环

在 `codex-core` 内部，状态不再是扁平的聊天数组，而是按三级生命周期严格归属：**`Thread`** 代表一条可持久化、可恢复、可分叉（Fork）的完整会话；**`Turn`** 代表一次用户输入触发的闭环工作周期；**`Item`** 则是回合内部可独立观测的原子单元（助手流式文本、Reasoning 片段、Shell 命令、文件补丁或 MCP 调用）。

很多人好奇 Codex 内部是不是藏着一套把复杂需求预先拆成有向无环图（DAG）的硬编码规划器？翻开 `core/src/tasks/regular.rs` 与 `core/src/session/turn.rs` 就会发现：**Harness 根本不替模型做任务拆解**。要不要调 `update_plan` 更新步骤清单、要不要调 `spawn_agent` 拉起拥有独立事件循环与身份的子智能体（Subagent，后续通过 `send_input / wait_agent / close_agent` 协同），完全由模型根据当前上下文自主决策；Harness 的职责是把每一轮采样与工具副作用执行得滴水不漏：

1. **内层采样-工具循环（`run_turn`）**：每轮先检查上下文长度并在逼近阈值时触发自动压缩（Compaction），解析当前所需的 Skills、插件与环境状态片段（`WorldState` 仅在发生变动时追加差异补丁），随后复用同一个 `ModelClientSession` 发起流式采样。如果模型发起了工具调用，运行时执行完工具后将输出按序塞回下一轮采样请求；只有当模型仅返回最终助手回复、运行期间队列里没有新塞入的用户补充输入（Pending Input），且停止钩子（Stop Hook）未要求续跑时，内层循环才真正 `break`。
2. **外层输入排空循环（`RegularTask::run`）**：这正是“Agent 干活中途用户插话”不乱套的秘诀。用户在终端补敲的一句话并不会去硬改半空中的网络包，而是投递进信箱队列；等当前采样步安全落地或内层 `run_turn` 收尾时，外层循环发现信箱非空，立刻无缝衔接拉起下一轮修正采样。
3. **读写锁准入的保序并发工具执行**：当模型在单次响应里声明 `parallel_tool_calls` 并发调起多个工具时，`ToolCallRuntime`（见 `core/src/tools/parallel.rs`）并没有鲁莽地直接 `join_all`。它用一把异步读写锁做并发准入：纯只读、无副作用的工具处理器拿**读锁**并行齐飞；涉及 Shell 状态变更、人工审批或有状态外部连接的工具拿**写锁**独占互斥；所有任务统一装进 `FuturesOrdered`，无论底层谁先跑完，最终写回历史记录的顺序严格对齐模型发起调用的原始次序。

### 3. 有界背压遇上反向请求：用一小段进程内无界队列破除死锁

异步通道（Channel）隔开了用户按键、模型流式吐字与终端 UI 渲染三种截然不同的速度，但也埋下了一颗非常隐蔽的定时炸弹。

在早期设计中，如果从底层运行时到客户端门面（Facade）的事件队列一律设为固定容量 `N` 的有界队列，就会在初始化或高频输出阶段撞上经典的环形等待：调用方发出一个请求后阻塞等待它的 `Response`，暂时还没腾出手去读事件流；而后台 Worker 在把排在前面的启动 `Notification` 塞进已满的有界事件队列时被 `await send` 挂起，导致它永远没法继续往下处理并吐出调用方苦苦等待的那个 `Response`！

最新版本的 `app-server-client` 对此做了一处极具工程分寸感的手术：**底层核心运行时与命令队列依然维持有界容量以守住背压（Backpressure）红线，唯独把进程内 Facade 到本地 TUI / Exec 消费者的最后一段事件缓冲改成了无界队列**，由专职 Worker 持续排空底层管道并按序转存。这样既没有放任外部输入无限撑爆内存，又用一个生命周期完全受控的进程内泄压阀彻底斩断了“等响应不读事件 ↔ 等事件空位不发响应”的死锁环。

### 4. 上下文压缩（Compaction）与前缀缓存（Prefix Cache）对齐

长周期编码任务动辄上百轮工具往返，既不能让上下文冲破窗口上限，也不能每轮都把几万 Token 的历史重新付一遍全价计算费。Codex 在 `core/src/context_manager/history.rs` 与 `core/src/client.rs` 里把成本控制拆成了三步配合：

- **裁剪无效载荷**：对超长终端标准输出做头尾截断，过滤与当前模型模态不匹配的片段；逼近窗口上限时在回合前或回合中触发 Compaction，将早期冗长交互浓缩为可延续的状态摘要。
- **复用长连接与路由粘性**：同一回合内固定复用 `ModelClientSession` 的 WebSocket 连接；同时以会话 ID（子智能体场景下归并至父会话）生成稳定的 `prompt_cache_key` 辅助服务端路由命中。
- **严格保持前缀只增不改**：`prompt_cache_key` 只是敲门砖，服务端 Prefix Cache 真正命中的物理前提是**前部 Token 序列逐字一致**。因此稳定的系统指令（Instructions）与工具 Schema 固定钉死在最前端，动态变化的环境补丁与新增工具输出一律往尾部追加，并通过每轮返回的 `cached_input_tokens` 与 `cache_write_input_tokens` 指标量化真实命中率。

---

## 二、安全与持久化：内核级沙箱与“日志 + 索引”双写分离

### 1. 正交的审批与沙箱策略：为什么 `.git` 在可写工作区里也要锁成只读？

允许大模型在开发机上跑 Shell、打补丁和连 MCP，安全防线就绝不能停留在口头约定。Codex 从协议层（`SandboxPolicy` 与 `AskForApproval`）一直到子进程派生边界，把权限控制拆成了两条相互正交的坐标轴——**沙箱模式决定“不升级权限时子进程物理上能干什么”**，**审批策略决定“一旦越界时允不允许停下来问人类要授权”**：

![Codex 正交审批策略与跨平台内核沙箱隔离决策流：工具意图先经 SandboxMode 与 AskForApproval 双轴判定，越权请求挂起等待客户端人工审批，放行后编译为锁死 .git/.codex 只读的宿主内核沙箱拉起子进程](/images/diagrams/aug-ai-codex-sandbox-approval.svg)

沿着这条决策流看源码里的五个硬核细节：

1. **三档用户模式加一档外部容器模式**：CLI 提供 `read-only`（全盘只读、断网）、`workspace-write`（仅当前工作目录、临时目录及显式声明的白名单目录可写，网络默认仍关闭）与需格外慎用的 `danger-full-access`；`codex-core` 内部还额外定义了 `external-sandbox`，专供宿主进程本身已被外部容器隔离的集成环境使用，避免套娃施加冲突的系统调用限制。
2. **工作区可写不等于元数据可篡改**：即便处于 `workspace-write` 模式，沙箱编译器依然会把工作区内部的 `.git`、`.codex` 等敏感子路径单独钉死为**只读**——否则恶意或跑偏的脚本只需往 `.git/hooks/pre-commit` 或 `.codex` 配置里塞一行命令，就能在用户下次操作时轻松逃逸出沙箱！
3. **跨平台内核级落地**：上层传递平台无关的权限轮廓（Permission Profile），直到真正派生子进程前的一刻才按操作系统下沉转换——macOS 调用内核 Seatbelt（`sandbox-exec`），Linux 组合 `bubblewrap` 与 Landlock 文件系统访问控制，Windows 则走受限令牌（Restricted Token）后端；文件系统权限与受管网络代理（Managed Proxy）分开独立表达。
4. **绝不静默降级**：一旦宿主沙箱初始化失败，运行时直接抛错中止该工具调用，绝不悄悄退回无保护裸跑。

### 2. 双层持久化：为什么同时维护 JSONL 滚动日志与 SQLite 库？

进程退出、断电崩溃或者切换终端恢复会话时，Harness 靠什么重建现场？在 `codex-thread-store` 存储边界背后，本地持久化并没有把所有东西硬塞进一张大表，而是由 `codex-rollout` 与 `codex-state` 分工搭起了典型的 **事件溯源日志（Event Log）+ 物化查询索引（Materialized Index）** 架构：

| 存储组件 | 物理载体 | 核心写入内容 | 承担的系统职责 |
| --- | --- | --- | --- |
| **`codex-rollout`** | 追加写 JSONL 文件 | `SessionMeta`、`ResponseItem`（消息/推理/工具输入输出）、`TurnContext`、`WorldState`、`Compacted` 替换摘要与关键 `EventMsg` | **唯一事实源（Canonical History）**：精确按序回放回合轨迹、构造下一轮模型上下文 |
| **`codex-state`** | 本机 SQLite 数据库 | `thread_id`、日志路径、时间戳、`cwd`、模型与推理档位、沙箱配置、Token 用量、标题预览、Git 分支及父子智能体派生图 | **高速检索索引**：支撑 `thread/list` 分页、全文搜索、归档置顶与跨目录项目归属查询 |

这两者缺一不可：如果只留 SQLite 摘要表，恢复长会话时就丢掉了拼装精确 Prompt 所需的事件先后次序与压缩断点；如果只留满地 JSONL 文件，每次打开侧边栏渲染历史会话列表就得全盘扫描解析文本。源码还专门在记录下用户提问、即将发出首轮模型请求前埋下了 `PersistContext::TurnStart` 落盘点——哪怕进程在等待网络响应的半途崩溃，重启后依然能准确知晓“哪一轮提问已经发起但尚未完成”。

至于 Skills、Plugins、Apps 与 MCP 等外围扩展机制，本质上也是沿着同一套协议管道接入：Skills 负责按需向模型注入本地工作流规范，MCP Client 将外部工具与资源挂载进当前会话的路由表（反向的 `codex-mcp-server` 则把 Codex 自身包装成工具暴露给其他宿主），Plugins 再把上述能力打包成可分发安装单元——所有外部调用最终都统一转化为受审批和沙箱管辖的标准 `Item / Event`。

---

## 三、本机 WSL2 源码验证实验：亲历沙箱拦截与协议单测

纸上读完架构，必须拉起编译器跑通最小验证闭环，才算真正摸准了工程脉搏。我在本机 WSL2 环境下对仓库做了三步递进实测：

第一步，**对齐工具链解析 135 个 Crate 的依赖拓扑**。仓库 `rust-toolchain` 锁定的是 `1.93.0`，而我机器上已装好的是 `1.94.0`；直接裸跑 `cargo` 时，`rustup` 试图联网下载 `1.93.0` 并往 `~/.rustup/tmp` 写临时文件，当场被受限环境拦下。显式指定本机已有工具链后，Workspace 元数据一次解析成功：

```bash
RUSTUP_TOOLCHAIN=1.94.0 cargo metadata --no-deps --format-version 1
```

第二步，**挑战核心领域协议库 `codex-protocol` 单元测试**（注：仓库最新贡献规范推荐使用 `just test -p codex-protocol` 统一入口，因当时本机未装 `just`，此处直接通过 Cargo 驱动最小库测试）：

```bash
RUSTUP_TOOLCHAIN=1.94.0 cargo test -p codex-protocol --lib
```

第一次敲下回车时，终端立刻抛出了 `Read-only file system` 错误——Cargo 需要往用户主目录下的 `~/.cargo/git/db/crossterm-...` 写入锁定的 Git 依赖缓存，而当时的工作区沙箱只放行了当前代码仓库与 `/tmp` 临时目录！这个报错非但不是意外干扰，反而成了前文沙箱章节最生动的现场教学：**“允许写代码仓库”绝不等于“放开整个 `$HOME` 目录”，哪怕是构建工具链自身的全局缓存也是必须显式申报授权的外部副作用。**

按需放行依赖缓存写入权限后重新执行，冷编译与测试结果如下：

```text
Finished test profile in 2m 29s
running 140 tests
test result: ok. 140 passed; 0 failed; finished in 0.06s
```

`140` 项覆盖消息序列化、权限模型、沙箱语义映射与事件转换的单元测试在 **0.06 秒**内全部通过，而首次获取与编译依赖足足花了 **2 分 29 秒**。这也直观印证了为什么维护者如此警觉 `codex-core` 的体积膨胀：在百余个 Crate 规模的 Rust 单体仓库里，清晰的协议与工具层切分直接决定了日常增量开发的反馈生死线。

---

## 四、给二次开发者的演进切入口与源码阅读顺序

如果你也打算基于 `openai/codex` 做二次定制或提交补丁，千万别一上来就扎进几千行的 TUI 渲染组件或核心状态机大改特改。顺着耦合度从低到高，最稳妥的四步切入梯度是：

1. **先走通一条只读控制面协议字段**：在 `app-server-protocol`（v2）里新增一个小型诊断查询的 `*Params / *Response`（确保 Wire JSON 驼峰命名并同步更新 TypeScript Schema Fixture），在 `app-server` 挂上 Handler 并补齐强类型请求单测。副作用为零，却能一次性摸熟协议代码生成与服务端路由分发。
2. **贯通单次 Turn 的全链路可观测性**：核对并补齐 `thread_id → turn_id → tool_call_id` 在 `tracing` 日志里的跨层透传，亲眼看着一个 ID 从 `turn/start` 一路穿过 `Op::UserTurn`、模型流式响应、工具执行直到 `TurnCompleted`。
3. **挂载一个纯只读自定义工具**：注册 JSON Schema、观察它如何注入前缀提示词、经工具路由器分发、产出 `ItemStarted / ItemCompleted` 事件并同时被 TUI 与 JSONL `exec` 消费渲染。
4. **最后再谨慎触碰审批与沙箱边界**：任何涉及安全策略的改动都绝不能只测正常放行路径，必须完整覆盖用户拒绝、审批超时、客户端中途断连、管道组合命令拆解以及沙箱启动失败时的硬阻断行为。

重新从零精读这套代码库时，推荐按下面九站由外向内顺藤摸瓜，每经过一站都试着回答“**谁拥有状态、跨界走什么通道、副作用在哪受审、崩溃后靠谁恢复**”这四个问题：

1. **多工具二进制入口**：`codex-rs/cli/src/main.rs`
2. **最精简的无头客户端**：`codex-rs/exec/src/lib.rs` 与 `event_processor*.rs`
3. **进程内/外共享控制面客户端**：`codex-rs/app-server-client/src/lib.rs`
4. **App Server 会话编排**：`codex-rs/app-server/src/message_processor.rs` 与 `codex_message_processor.rs`
5. **内外双层协议契约**：`codex-rs/protocol/src/protocol.rs` 与 `app-server-protocol/src/protocol/v2.rs`
6. **核心会话与双层 Turn 循环**：`core/src/thread_manager.rs`、`codex_thread.rs`、`tasks/regular.rs`、`session/turn.rs`
7. **并发工具路由与平台沙箱**：`core/src/tools/parallel.rs`、`core/src/exec.rs`、`sandboxing/`
8. **事件日志与索引双层存储**：`thread-store/`、`history/src/lib.rs`、`rollout/src/lib.rs`、`state/src/lib.rs`
9. **终端交互状态消费端**：`tui/src/app_server_session.rs`、`app.rs`、`chatwidget.rs`

---

## 五、源码基线与实验环境说明

本文架构拆解与源码路径交叉核对基于 `openai/codex` 仓库提交 [`d36a3ead`](https://github.com/openai/codex/tree/d36a3ead3c896d0552207763ef483262bce9ac73) 及远端主干 `422239eb`（早期本机编译与单测实验起跑于工作区快照 `343074d4`）。本机 WSL2 实验环境如下：

| 环境项 | 本机配置状态 |
| --- | --- |
| **宿主内核与架构** | Linux `6.6.87.2-microsoft-standard-WSL2` (`x86_64`) |
| **Rust 工具链** | 系统 `rustc 1.94.1` / `rustup` 已装 `1.94.0`（仓库声明 `1.93.0`） |
| **辅助运行时** | Node.js `v24.14.0`（用于协议 Schema 对照；`just` / Bazel 未纳入本次最小单测路径） |
