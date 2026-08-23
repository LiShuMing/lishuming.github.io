---
title: "【源码】OpenAI Codex：源码理解、实验与 develop 笔记"
date: 2026-08-22T00:00:00+08:00
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

说来惭愧，作为一名 Engineer，我当前使用最多的工具已经变成了 Codex。Claude Code 的成本对我来说有些高，Codex 只能算是更现实的选择。工欲善其事，必先利其器，所以还是要把 Codex 真正理解清楚。

还是希望能系统阅读 Codex 源码。最初我把它理解成一个“可以在终端里调用模型并执行命令的 CLI”，但真正沿着代码走进去后，这个认识很快就不够用了。

OpenAI 的文章 [Codex as a platform: build on the open agent harness](https://developers.openai.com/blog/codex-as-a-platform) 又让我想重新认识一下 Harness 工程的内部。文章把可复用部分称为 agent harness：它负责维护上下文、调用工具、暴露进度、处理失败、执行审批，并把工作延续到后续 turn。

Codex 更接近一个运行在本机的 agent runtime：TUI、非交互 CLI 和 IDE 只是不同的交互面；App Server 提供统一控制面；`codex-core` 管理 thread、turn、模型请求、工具调用、审批和上下文；协议层把持续发生的事件送回客户端；沙箱和执行策略决定 agent 到底可以对机器做什么。

同时要注意，我们在电脑上用到的 Codex 产品和 `openai/codex` 开源仓库不是同一个边界。官方文章确认 App、CLI 和 IDE Extension 使用同一个开源 Harness，但也明确说模型访问和托管服务是独立层。仅凭这个仓库，不能反推出桌面产品的云端同步、运行环境和企业合规实现。

```text
┌──────────────────────────────────────────────────────────────────────┐
│ 交互与产品层：Codex App / CLI / IDE Extension / 自己开发的应用       │
└───────────────────────────────┬──────────────────────────────────────┘
                                │ CLI / SDK / App Server protocol
                                ▼
┌──────────────────────────────────────────────────────────────────────┐
│ 开源 Harness：context、agent loop、tools、approval、sandbox、events  │
│               thread persistence、MCP、TUI / exec integration       │
└───────────────────────────────┬──────────────────────────────────────┘
                                │ Responses / model access
                                ▼
┌──────────────────────────────────────────────────────────────────────┐
│ 模型与托管服务层：不属于这个开源仓库可完整解释的实现边界             │
└──────────────────────────────────────────────────────────────────────┘
```

这次源码扫读，希望弄明白两件事情：

- Harness 工程编码的整体框架：一次 prompt 或任务，从工程层面如何被推进并最终结束；
- Agent 工程如何支持并优化同 LLM 的交互：Context 压缩与长上下文、工具并发、subagent 管理，以及怎样尽量构造对 Prefix Cache 友好的 prompt。


本文不是完整的模块手册，而是一次个人学习过程的整理。我更关心四个问题：一次用户输入如何穿过系统、边界为什么这样划分、安全限制在哪里生效，以及如果要开始 develop，第一刀应该切在哪里。

## 1. 本机环境

本次阅读发生在一台 WSL2 x86_64 机器上：

| 项目 | 本机状态 |
| --- | --- |
| Linux | `6.6.87.2-microsoft-standard-WSL2` |
| CPU 架构 | x86_64 |
| 系统 Rust | `rustc 1.94.1` |
| rustup 已安装工具链 | stable、`1.94.0` |
| 仓库声明工具链 | `1.93.0` |
| Node.js | `v24.14.0` |
| `just` / Bazel / pnpm | 当前不在 PATH |

本文最初的本机实验基于当时 checkout 的提交；这次补充疑问时，我重新 fetch 了当前分支，并以 `origin/main@422239eb4b`（2026-08-23）交叉核对源码。下面凡是谈“最新实现”的地方都以这个远端引用为准，不把本机 detached HEAD `343074d420` 冒充成最新源码。


仓库顶层同时存在 Rust、TypeScript、Python、Bazel、Nix 和 pnpm 相关文件，但主干非常明确：

```text
codex/
├── codex-rs/       Rust 主实现
├── codex-cli/      已被取代的 TypeScript CLI
├── sdk/            Python / TypeScript SDK
├── docs/           用户和开发文档
├── tools/          仓库级检查工具
├── scripts/        构建与维护脚本
├── MODULE.bazel    Bazel 模块与跨平台工具链
└── justfile        常用开发命令入口
```

`codex-rs` 是一个很大的 Cargo workspace。最初按二级目录统计到 61 个主要 crate；本次复查时，最新 `codex-rs/Cargo.toml` 已直接列出 135 个 workspace member。数字增长很快，重要的不是记住数量，而是它在尝试把几类边界固定下来：

- 产品入口与业务运行时分离；
- 内部领域协议与对外 App Server 协议分离；
- 会话事件日志与 SQLite 查询索引分离；
- 平台沙箱、命令策略、认证、MCP、插件和 telemetry 分别独立；
- 小型通用能力尽量下沉到 utility crate，而不是继续扩大 `codex-core`。

仓库自己的 `AGENTS.md` 甚至明确提醒贡献者“抵抗把新代码加进 `codex-core` 的诱惑”。这说明 core 膨胀已经不是理论风险，而是维护者正在主动处理的架构压力。

## 2. 整体架构

把几十个 crate 压缩成一张图，大致是下面这样：

```text
                    用户 / IDE / 自动化
                           │
             ┌─────────────┼─────────────┐
             ▼             ▼             ▼
          codex TUI     codex exec     IDE / App
             │             │             │
             └──────┬──────┘             │
                    ▼                    │ JSON-RPC
          app-server-client              │
          (in-process / remote)           │
                    │                    │
                    └────────┬───────────┘
                             ▼
                       app-server
                    thread / turn API
                             │
                             ▼
                        codex-core
               session / model / tools / context
                  │          │          │
          protocol/events  sandbox    rollout/state
                  │          │          │
                  ▼          ▼          ▼
              客户端事件   本机进程    JSONL + SQLite
```

这张图改变了我最初的两个判断。

第一，TUI 并不是系统中心。它代码很多、用户最容易看到，但它更像复杂的事件消费者和状态展示器。

第二，App Server 不是只给 VS Code 准备的附加服务。当前 TUI 和 Exec 也通过共享的 `app-server-client` 启动 in-process App Server。也就是说，同一个控制面同时服务进程内 typed channel 和进程外 JSON-RPC/WebSocket 边界。

### 从 `codex` 命令开始追控制流

主程序入口位于 `codex-rs/cli/src/main.rs`。`codex-cli` 实际上是一个 multitool dispatcher：

```text
codex
├── 无子命令       -> codex-tui
├── exec/review     -> codex-exec
├── app-server      -> codex-app-server
├── mcp-server      -> codex-mcp-server
├── login/logout    -> codex-login
├── sandbox/debug   -> 平台安全与诊断工具
└── apply/features  -> 其他产品能力
```

`main()` 先经过 `arg0_dispatch_or_else`。这个设计很有 Unix 味道：同一个二进制可以根据 `argv[0]` 承担内部 helper 身份，例如 Linux sandbox 或 apply-patch 模式，然后才进入正常 Clap 子命令分发。

默认路径进入 TUI，`exec` 和 `review` 则进入 `codex_exec::run_main`。这里的 CLI 层主要做三件事：

1. 解析参数；
2. 合并配置覆盖与 feature toggle；
3. 把控制权交给真正的运行面。

这是一种比较健康的入口设计。参数解析没有直接长成 agent runtime，CLI 只是 composition root。

### TUI 和 Exec 为什么都经过 App Server

继续追 `codex-exec` 会看到一条很清楚的路径：

```text
InProcessAppServerClient::start
  -> thread/start 或 thread/resume
  -> turn/start
  -> 持续消费 ServerNotification
  -> TurnCompleted
```

TUI 的 `app_server_session.rs` 也使用相同的 `ClientRequest::ThreadStart` 和 `ClientRequest::TurnStart`。两者都依赖部分 `codex-core` 配置类型，但会话生命周期经过 App Server 统一表达。

`app-server-client` 的 README 对这个边界解释得很直接：进程内路径使用 typed channel，进程外路径才做 JSON 序列化；但即使在进程内，请求仍然保留 App Server 的响应语义，而不是为 TUI 和 Exec 再发明第二套 contract。

我很喜欢这个选择。它牺牲了一点“直接调用 core 最短路径”的简单感，却换来了几个更重要的性质：

- TUI、Exec 和 IDE 对 thread/turn 生命周期的理解一致；
- in-process 与 remote transport 可以共享上层 session 逻辑；
- App Server API 不再是无人使用的外围协议，而是被主产品路径持续验证；
- 背压、初始化握手、shutdown 和 server request resolution 有统一实现。

这更像数据库里把 SQL frontend、RPC 和执行引擎之间的 contract 固定下来：协议边界一旦成为主路径，长期漂移会少很多。

### 一次用户输入到底怎样运行

以 `codex exec "解释这个仓库"` 为例，可以把主要控制流整理成：

```text
cli_main()
  │
  └─> codex_exec::run_main()
       │
       ├─> 加载 config / auth / sandbox / approval policy
       ├─> InProcessAppServerClient::start()
       ├─> ClientRequest::ThreadStart
       │    └─> app-server::thread_start_task()
       │         └─> ThreadManager::start_thread_with_tools...
       │              └─> spawn_thread()
       │                   └─> CodexThread
       │
       ├─> ClientRequest::TurnStart
       │    └─> CodexThread::submit(Op::UserTurn)
       │         └─> model response / tool loop / context updates
       │
       └─> ServerNotification stream
            ├─> TurnStarted
            ├─> ItemStarted / delta / ItemCompleted
            ├─> approval request / tool events
            └─> TurnCompleted
```

App Server 收到 `thread/start` 后并不是简单 new 一个对象。它先从请求参数和全局配置派生 thread config，校验 dynamic tools，再调用 `ThreadManager` 创建 thread，挂接 listener，更新 thread watch 状态，返回 `ThreadStartResponse`，随后再发送 `ThreadStartedNotification`。

这个“response + notification”看起来有些重复，但两者语义不同：response 回答请求是否成功，notification 告诉订阅者系统状态发生了变化。多个客户端、恢复会话、远程 transport 出现后，这种区分会比函数返回值可靠。

## 3. 基本概念

### Agent Loop：任务如何推进，又如何结束

我最想先弄明白的问题是：一条任务如何拆解，Agent Loop 如何调用，又在什么条件下结束？

先说一个容易误解的结论：Codex Harness 里没有一个把任意 prompt 硬编码成 DAG 的“任务拆解算法”。拆不拆、拆成哪些步骤、是否调用 `update_plan`、是否启动 subagent，主要由模型根据 instructions、当前上下文和可见工具决定。Harness 不替模型规划任务，它负责把模型的决定可靠地执行、记录，再把结果送回模型。

最新主路径已经从过去的大文件拆到 `core/src/tasks/regular.rs` 和 `core/src/session/turn.rs`。一次普通 turn 大致是：

```text
RegularTask::run
  └─ run_turn(input)
       ├─ compact（必要时）
       ├─ 解析本轮需要的 MCP / skills / plugins
       ├─ 记录用户输入与当前 world state
       └─ loop
            ├─ history.for_prompt()，构造本次模型输入
            ├─ ModelClientSession::stream()
            ├─ 消费 assistant / reasoning / tool-call 流
            ├─ 并发或串行执行工具，并按顺序记录输出
            ├─ tool output / steer 需要 follow-up？── 是 ──> 下一次采样
            └─ 只有最终 assistant message？──────── 是 ──> 结束 turn
```

`run_turn` 的注释把终止条件写得很直接：如果模型请求 function call，就执行工具，并在下一次 sampling request 中回送结果；如果模型只返回 assistant message，就把它记入历史，并认为 turn 可以完成。实际代码还会额外检查三件事：用户运行中追加的 pending input、上下文是否需要 auto-compact，以及 stop hook 是否要求阻止结束并注入 continuation。只有 `needs_follow_up == false` 且 stop hook 没有继续任务，循环才真正 `break`。

外面还有 `RegularTask::run` 的第二层循环。如果 `run_turn` 结束时 input queue 又收到了 steer/mailbox 输入，它会以空的初始输入再进入一次 `run_turn`。因此 UI 中“Agent 正在执行时继续补一句”并不是修改一个正在飞行的 HTTP request，而是先入队，再在安全的采样边界并入 history。

任务拆解有两种显式工具化表达：

- `update_plan` 维护步骤及 `pending / in_progress / completed` 状态，但它是给模型使用和给用户观察的计划状态，不是调度器本身；
- `spawn_agent` 创建独立 child thread，随后通过 `send_input`、`wait_agent`、`close_agent` 等工具管理。subagent 默认继承父任务的部分运行配置和上下文，但拥有独立的 agent loop 与持久化身份。

同一轮多个工具调用则是另一种并行。Responses 请求声明 `parallel_tool_calls`，返回的 tool future 被放入 `FuturesOrdered`。`ToolCallRuntime` 用读写锁做准入：声明支持并行的 handler 取得读锁，可以彼此并行；不支持并行的 handler 取得写锁，与其他调用互斥。最后按调用顺序 drain 结果，避免“执行完成顺序”污染“模型看到的工具输出顺序”。这比简单 `join_all` 更谨慎，因为 shell、审批和有状态 MCP 并不天然可并发。

### `Thread`、`Turn` 和 `Item`

我最初把 Codex 想成一个 request/response 程序：输入 prompt，等待模型返回文本。源码实际采用的是持续事件模型：

- `Thread`：一条可恢复、可持久化、可 fork 的 agent 会话；
- `Turn`：一次用户输入引发的完整工作周期；
- `Item`：turn 内部可独立观察的消息、reasoning、命令、补丁和工具调用。

在更底层的 `codex-protocol` 中，注释直接把交互描述成 SQ/EQ：Submission Queue 与 Event Queue。`CodexThread::submit()` 把 `Op` 送入运行时，`next_event()` 则从另一侧持续取出 `Event`。

这解释了很多表面现象：

- 为什么 assistant 文本可以流式显示；
- 为什么命令运行期间还能请求审批；
- 为什么 Ctrl-C 可以转化为 interrupt op；
- 为什么 TUI 与 JSONL Exec 可以消费同一条执行流；
- 为什么一次 turn 中可以穿插多个模型响应和工具调用。

Agent 并不是“调用一次大模型的函数”，而是一台以事件推进的状态机。

### Queue 与 Channel：解耦什么，为什么还会背压

这里的问题是：为什么需要 Queue 和 Channel，它们究竟在解耦什么，为什么有界队列又会产生背压？这确实涉及计算机系统设计的本质。

Codex 至少存在三类需要错开的节奏：用户提交 `Op` 与 core 执行的节奏、模型/工具产生事件与 TUI 渲染的节奏、App Server 请求与异步 notification 的节奏。如果都改成同步函数调用，慢消费者会占住生产者调用栈，审批这种“服务端发请求、客户端再回答”的反向调用还可能形成递归等待。Channel 在这里同时建立了：

- **时间解耦**：生产者不要求消费者此刻正在运行；
- **所有权解耦**：任务只持有 sender/receiver，不共享整块可变状态；
- **并发边界**：不同 Tokio task 可以独立调度、取消和 shutdown；
- **顺序 contract**：同一 channel 内事件按约定顺序被观察。

但 queue 只把速度不匹配变成了缓冲，并没有消灭它。有界队列容量为 `N`，满后 producer 必须等待，这就是背压。好处是内存有上限，坏处是如果 A 等 B、B 又因为队列满而等 A，就会死锁。

最新 `app-server-client` 正好留下了一个很有价值的修正：command queue 和 embedded runtime 仍是有界的，但 facade 到 TUI/Exec 的本地 consumer event queue 改成了**无界**。原因不是开发者忘了内存风险，而是典型调用顺序可能是“先发 request 并等待 response，之后才读启动 notifications”；如果 worker 向一个已满的有界事件队列 `await send`，它就无法继续取到排在 notifications 后面的 response，于是双方循环等待。

```text
caller 等 response
   ↑                 ↓
暂时不读 event    worker 等 event queue 空位
   └──────────── deadlock ────────────┘
```

现在 worker 持续 drain 有界 runtime，把事件按序放入本地无界队列，切断这条等待环。`Lagged` 仍可能从底层 runtime 传上来，但不是这个本地队列主动丢 transcript。这个设计不是在“有界/无界谁更高级”之间站队，而是把有界背压放在能正确承受的位置，把无界队列限制在进程内、生命周期受控的一小段 escape valve 上。

这里让我想到数据库 change stream 或 replication log：buffer、顺序、可靠性、背压和关闭协议从来不是纯性能参数，它们共同定义业务正确性。

### Context、Compaction 与 Prefix Cache
> TODO: 更加细化地结合源码地了解。不同的harness工程这里到底有哪些不同？有无理论的优化空间。

长任务并不是把所有历史无条件拼接到 prompt。每个 sampling step 会从 `ConversationHistory` 生成适合当前模型模态的输入；环境、权限、skills、插件和 world state 以结构化 contextual fragments 注入，world state 只在发生变化时记录新的 patch。上下文接近 token limit 时，`run_turn` 会在首轮采样前或中途执行 compaction，再带着压缩后的 history 继续，而不是直接结束当前任务。

我把这里的优化分成三层：

1. **减少无效上下文**：规范化历史，过滤不适合当前输入模态的内容，对命令输出执行截断，并用 compaction 把旧窗口压成可延续的状态；
2. **保持请求稳定**：一个 turn 内复用同一个 `ModelClientSession`，保留 WebSocket 与 sticky routing 状态；instructions、tools、reasoning 配置等请求属性不变时，还可以复用前一响应链路；
3. **提高缓存可复用性**：普通请求使用 session id 作为 `prompt_cache_key`，部分 internal/subagent 场景按 parent thread 归并 key；历史总体按追加方向增长，稳定 instructions 和 tool schema 尽量留在前缀，变化内容放在后部。

这里要避免过度解读：`prompt_cache_key` 只是帮助服务端路由缓存，稳定 key 也不等于一定 cache hit。Prefix Cache 是否命中还取决于实际 token 前缀是否一致；临时改 instructions、工具列表或前部上下文，都会让缓存收益下降。源码同时记录 `cached_input_tokens` 和 `cache_write_input_tokens`，说明正确的验证方式不是凭感觉，而是观察每轮 usage 数据。

### Sandbox：策略、平台实现与审批

Codex 需要执行 shell、写文件、连接 MCP、运行补丁，这使安全边界无法只靠一句 prompt 保证。源码把安全拆成多层：

```text
用户配置 / CLI flags
        │
        ▼
approval policy + sandbox policy
        │
        ▼
execpolicy / guardian / permission request
        │
        ▼
platform sandbox
  macOS Seatbelt
  Linux Landlock / bubblewrap
  Windows restricted sandbox
        │
        ▼
实际子进程与文件系统访问
```

协议类型里就已经存在 `SandboxPolicy`、`AskForApproval`、filesystem access 和 network access；App Server 的 `turn/start` 也允许显式传递 approval 与 sandbox 配置。换句话说，安全不是执行器最后套上的 bool，而是从 API、配置、领域类型一路传播到平台实现。

从用户配置看，`SandboxMode` 有三种：

- `read-only`：文件系统只读，网络默认关闭；
- `workspace-write`：允许 cwd、临时目录和额外 writable roots 写入，网络仍默认关闭；
- `danger-full-access`：不施加这些限制，源码注释明确要求谨慎使用。

core 内部的 `SandboxPolicy` 还多一个 `external-sandbox`，表示进程已经被外部环境隔离，Codex 不再重复施加本机文件沙箱，但仍保留网络能力描述。它是运行时集成语义，不是普通 CLI 的第四个可选 mode。

平台落地并不相同：macOS 使用 Seatbelt；Linux 当前有 bubblewrap 路径并保留 legacy Landlock 实现，manager 侧统一为 Linux sandbox 类型；Windows 使用 restricted-token/elevated backend。上层传递的是统一 permission profile，真正启动子进程时才转换成宿主平台命令。

安全考虑也不只是“目录是否可写”：

- writable root 内仍可把 `.git`、`.codex` 等 metadata 子路径降为只读，避免通过 hooks 或配置修改扩大权限；
- filesystem 与 network 分开表达，网络可以经 managed proxy 约束，而不是跟文件写权限捆成一个 bool；
- approval policy 与 sandbox policy 正交：前者决定越权时能否询问用户，后者决定未升级命令实际拥有什么权限；
- 命令、cwd、环境和 additional permissions 到执行边界才转换，降低跨平台 transport 提前解释本机路径的风险；
- sandbox 准备失败是显式错误，而不是悄悄退回 full access。

本次源码阅读本身就验证了这条链路。第一次运行 Cargo 测试时，它需要创建：

```text
~/.cargo/git/db/crossterm-...
```

当前工作区沙箱只允许写仓库和临时目录，因此命令以 `Read-only file system` 失败。扩大权限后，同一条测试才开始下载锁定依赖并编译。这个失败不是麻烦的噪声，它恰好说明“工作区可写”不等于“整个 home 可写”，而工具链缓存也是需要显式授权的副作用。

### 持久化：为什么存，具体存什么

需要持久化的不是“模型当前在想什么”，而是进程退出后仍需恢复、回放、查询或审计的状态。最新源码又向前抽象了一层：`codex-thread-store` 成为 thread 的 storage boundary，提供 local 与 in-memory 实现；本地实现再使用 `codex-rollout` JSONL 保存 canonical history，并在可用时用 `codex-state` SQLite 保存可查询 metadata。

JSONL 中的 `RolloutItem`(轨迹，这个同deepseek harness的轨迹有什么不同？) 主要包括：

- `SessionMeta`：thread/session 身份、来源、基础 instructions、history mode 等；
- `ResponseItem`：用户/assistant 消息、reasoning、tool call 与 tool output；
- `TurnContext` 与 `WorldState`：当轮运行配置和环境状态；
- `Compacted`：压缩摘要及必要的 replacement history；
- inter-agent communication、security risk score 和需要回放的 `EventMsg`。

SQLite 则保存 thread id、rollout path、创建/更新时间、source、cwd、model/provider、reasoning effort、sandbox/approval、token usage、标题/预览、archive/pin/section、Git 信息和 subagent spawn graph 等索引字段。它服务 `thread/list`、搜索、排序、分区和项目归属，不替代原始 history。

这是一种 event log + materialized index 的组合。只保留 SQLite，会失去构造模型上下文和精确恢复 turn 所需的顺序记录；只保留 JSONL，列表和搜索又会退化成反复扫描文件。`ThreadStore::append_items` 因而只追加 canonical history，metadata 更新走独立 API，避免 store 从事件内容偷偷推导业务语义。

我还注意到 `PersistContext::TurnStart`：用户输入被记录、即将开始 sampling 时，会把 turn start 作为明确持久化边界。这个细节很重要，因为崩溃发生在模型返回前，恢复系统至少仍知道“用户提交过什么、哪一轮已经开始”，而不是得到一个看似从未发生的请求。

对我来说，这是 Codex 源码里最像数据库系统的一部分：写路径保留事件事实，读路径维护索引，backfill 和 rollout migration 则负责修复历史演进后的派生状态。

> TODO: Rollout只用户backup/rollup还是在每次turn的时候会用来回溯，当做记忆context使用？

### Skills、Plugins、Apps 和 MCP

这些扩展机制名字很多，容易在第一次阅读时混在一起。我目前的理解是：

- Skills 主要向模型注入可发现的工作流和本地说明；
- Plugins 把 skills、apps、MCP 等能力组织成可安装单元；
- Apps/Connectors 表达外部应用能力与认证状态；
- MCP client 把外部 server 的 tools/resources 接入当前 thread；
- MCP server 则反过来把 Codex 暴露给其他 MCP client。

它们最终都需要回答相似的问题：什么时候被发现、怎样进入当前 turn、是否需要认证或审批、调用结果如何变成 Item/Event、状态怎样展示给客户端。

因此扩展系统并不只是“多注册几个工具”。它会穿过 config、protocol、core、App Server 和 UI 多层，这也是新功能很容易顺手被塞进 `codex-core` 的原因。

## 4. 源码实验

### 建立 crate 地图

我先从 `Cargo.toml`、各 crate README、`lib.rs` 和 `main.rs` 建立分层，而不是直接打开最大的文件。结果确认：

- Rust workspace 是产品主干；
- `codex-cli` 是 composition root；
- TUI/Exec 共享 App Server client；
- `codex-protocol` 是内部领域类型；
- `codex-app-server-protocol` 是外部控制面 contract；
- rollout 与 state 分担事实记录和查询索引。

### 解析 Cargo workspace

仓库锁定 Rust 1.93，但本机只有 1.94 工具链。直接运行 Cargo 时，rustup 尝试安装 1.93，又因为沙箱不能写 `~/.rustup/tmp` 而失败。

我没有把这个问题伪装成“源码不能编译”，而是显式使用已有工具链：

```bash
RUSTUP_TOOLCHAIN=1.94.0 cargo metadata --no-deps --format-version 1
```

metadata 成功解析，说明 workspace manifest 至少能被当前 Cargo 理解。但这不等于官方 1.93 构建已验证，因此文章仍然保留环境偏差。

### 运行最小协议测试

随后执行：

```bash
RUSTUP_TOOLCHAIN=1.94.0 cargo test -p codex-protocol --lib
```

这是当时为验证最小 crate 所做的实际操作，所以我保留原命令和结果。不过最新仓库开发规范已经要求不要直接运行 `cargo test`，而应通过 `just test -p codex-protocol` 进入仓库统一测试入口。由于当时本机 `just` 不在 PATH，这个结果只能算源码阅读实验，不能替代按贡献规范完成的验证。

首次运行因 `~/.cargo/git` 只读失败；获得依赖缓存写入和网络权限后，冷构建结果为：

```text
Finished test profile in 2m 29s
running 140 tests
test result: ok. 140 passed; 0 failed; finished in 0.06s
```

这个结果有两层含义。

第一，`codex-protocol` 的序列化、permission model、sandbox semantics、event mapping 和 model types 在当前本机工具链下通过了单元测试。

第二，测试本身只有 0.06 秒，绝大多数时间花在第一次获取和编译依赖。大型 Rust monorepo 的日常反馈速度高度依赖缓存、crate 边界和测试选择；“只跑一个轻量 crate”在冷环境里也不一定轻。


## 5. 如果开始 develop，我会从哪里下手

我不会第一步就改 agent loop 或 TUI 大文件，而会按可观察性和边界清晰度推进。

### 第一步：做一个只读协议变化

例如为 App Server v2 增加一个小型诊断字段或查询能力：

1. 在 `app-server-protocol` 定义 `*Params` / `*Response`；
2. 保证 wire 字段为 camelCase；
3. 更新 TypeScript/schema fixture；
4. 在 App Server 实现 handler；
5. 写 typed request 的行为测试；
6. 更新 `app-server/README.md`。

这条路径能快速熟悉协议生成、实验性 API gating 和 server dispatch，同时副作用较小。

### 第二步：追踪一个完整 Turn

给现有 tracing 增加或核对 thread id、turn id、tool call id 的贯通关系，然后从：

```text
turn/start -> Op::UserTurn -> model stream -> tool call -> TurnCompleted
```

验证一个 ID 是否能跨 App Server、core 和 event processor 串起来。对异步系统而言，可观察性通常比新增功能更适合作为第一次贡献。

### 第三步：做一个有界的工具实验

增加一个无副作用或只读工具，观察它如何：

- 注册 schema；
- 进入模型上下文；
- 被 tool router 分发；
- 产生 started/completed item；
- 被 TUI 与 JSONL Exec 渲染。

这能把 tool、protocol、core 和 UI 串起来，又不必一开始处理复杂沙箱语义。

### 第四步：再碰审批与沙箱

安全相关修改需要同时验证 policy、协议、平台实现和失败模式。这里不适合凭“看起来更方便”改默认值，也不能只测 happy path。至少要覆盖：允许、拒绝、升级、超时、客户端掉线和命令分段后的实际权限。

## 6. 我建议的源码阅读路线

如果重新开始一次，我会按下面的顺序阅读：

```text
1. 产品入口
   cli/src/main.rs

2. 一个具体前端
   exec/src/lib.rs
   exec/event_processor*.rs

3. 共享控制面客户端
   app-server-client/README.md
   app-server-client/src/lib.rs

4. App Server 生命周期
   app-server/README.md
   app-server/src/message_processor.rs
   app-server/src/codex_message_processor.rs

5. 领域协议
   protocol/src/protocol.rs
   app-server-protocol/src/protocol/v2.rs

6. 核心 thread
   core/src/thread_manager.rs
   core/src/codex_thread.rs
   core/src/tasks/regular.rs
   core/src/session/turn.rs

7. 一条工具执行路径
   core/src/tools/
   core/src/exec.rs
   sandboxing/

8. 持久化
   thread-store/
   history/
   rollout/
   state/

9. 最后再进入 TUI
   tui/src/app_server_session.rs
   tui/src/app.rs
   tui/src/chatwidget.rs
```

每读一层，我会固定回答几个问题：

1. 这一层拥有哪部分状态？
2. 输入和输出是函数调用、typed channel，还是 JSON-RPC？
3. 哪些事件必须可靠送达，哪些可以降级？
4. 副作用在哪里发生，谁决定是否需要审批？
5. thread、turn、item id 在这里怎样传播？
6. 崩溃或客户端断开后，状态能否恢复？

这些问题比“这个文件每个函数做什么”更容易形成稳定的系统认识。

## 结语

读完第一轮源码后，我对 Codex 的理解从“会调用工具的 AI CLI”变成了：

> Codex 是一个以 Thread/Turn/Item 为领域模型、以事件流驱动、通过 App Server 暴露统一控制面、在本机安全执行副作用并持久化会话状态的 agent runtime。

它最值得学习的地方不只是怎样调用模型，而是怎样把一个不确定、流式、会产生副作用的 agent，放进相对清晰的协议、权限和生命周期边界中。

这次我只完成了架构地图、关键控制流和最小协议测试。下一步真正有价值的工作，是选择一条足够小的 develop 路径，做出变更、补齐协议与测试，再从运行事件反过来验证自己对系统边界的理解。源码只有在能被实验推翻或证实时，才真正从“读过”变成“理解过”。

## 本次阅读的本地入口

- `codex-rs/README.md`
- `codex-rs/cli/src/main.rs`
- `codex-rs/exec/src/lib.rs`
- `codex-rs/tui/src/app_server_session.rs`
- `codex-rs/app-server/README.md`
- `codex-rs/app-server-client/README.md`
- `codex-rs/app-server/src/codex_message_processor.rs`
- `codex-rs/protocol/src/protocol.rs`
- `codex-rs/core/src/thread_manager.rs`
- `codex-rs/core/src/codex_thread.rs`
- `codex-rs/core/src/tasks/regular.rs`
- `codex-rs/core/src/session/turn.rs`
- `codex-rs/core/src/tools/parallel.rs`
- `codex-rs/thread-store/README.md`
- `codex-rs/history/src/lib.rs`
- `codex-rs/rollout/src/lib.rs`
- `codex-rs/state/src/lib.rs`
