---
title: "把 Codex 当成一个本地 Agent Runtime：源码理解、实验与 develop 笔记"
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

说来好惭愧，做一位Enigneer，当前使用最多的工具，目前变成了Codex。Claude Code高不可攀，Codex只能勉为其难。工欲善其事必先利其器，所以还是要把Codex理解学习啊。

还是希望能系统阅读 Codex 源码。最初我把它理解成一个“可以在终端里调用模型并执行命令的 CLI”，但真正沿着代码走进去后，这个认识很快就不够用了。

Codex 更接近一个运行在本机的 agent runtime：TUI、非交互 CLI 和 IDE 只是不同的交互面；App Server 提供统一控制面；`codex-core` 管理 thread、turn、模型请求、工具调用、审批和上下文；协议层把持续发生的事件送回客户端；沙箱和执行策略决定 agent 到底可以对机器做什么。

这次源码阅读也带有一点递归意味：我正在使用 Codex 阅读 Codex 自己。它在同一个工作区里执行 `rg`、Git、Cargo 和 Hugo；当 Cargo 需要写入沙箱之外的 `~/.cargo` 时，它先失败，再明确请求扩大权限；当长时间编译没有结束时，它持续汇报状态。源码里那些抽象并不是纸面设计，我在阅读过程中恰好也在经历它们。

本文不是完整的模块手册，而是一次个人学习过程的整理。我更关心四个问题：一次用户输入如何穿过系统、边界为什么这样划分、安全限制在哪里生效，以及如果要开始 develop，第一刀应该切在哪里。

## 1. 本机环境与源码基线

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

本地仓库状态为：

```text
branch: main
HEAD:   5bbfee69b6 nit: deny field v2 (#16427)
```

我先只 fetch 了当前分支：

```bash
git fetch origin refs/heads/main:refs/remotes/origin/main
```

远端 `origin/main` 更新到了 `343074d420`，本地 `main` 落后 4697 个提交。我没有 pull、merge 或 rebase，因此本文所有源码判断都以本地 `5bbfee69b6` 为准，而不是把刚 fetch 到的远端代码和当前工作树混在一起。

这个细节很重要。Codex 仓库变化很快，如果不记录 commit，今天正确的入口、协议字段和 crate 边界，几个月后可能已经完全不同。源码笔记首先应该是一份可定位的观察，而不是永远正确的产品说明。

## 2. 先看仓库，而不是先看 `core`

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

`codex-rs` 是一个很大的 Cargo workspace。本机按二级目录统计到 61 个主要 crate，其中 22 个位于 `utils/`。这不是单纯为了把文件拆小，而是在尝试把几类边界固定下来：

- 产品入口与业务运行时分离；
- 内部领域协议与对外 App Server 协议分离；
- 会话事件日志与 SQLite 查询索引分离；
- 平台沙箱、命令策略、认证、MCP、插件和 telemetry 分别独立；
- 小型通用能力尽量下沉到 utility crate，而不是继续扩大 `codex-core`。

仓库自己的 `AGENTS.md` 甚至明确提醒贡献者“抵抗把新代码加进 `codex-core` 的诱惑”。这说明 core 膨胀已经不是理论风险，而是维护者正在主动处理的架构压力。

## 3. 我看到的整体架构

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

## 4. 从 `codex` 命令开始追控制流

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

## 5. TUI 和 Exec 为什么都经过 App Server

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

## 6. 一次用户输入到底怎样运行

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

## 7. `Thread`、`Turn` 和 `Item` 是真正的领域模型

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

## 8. 背压不是性能细节，而是正确性问题

`app-server-client` 使用有界 channel。消费者跟不上时，并不是简单无限堆积消息，而是把事件分成两类：

- lossless：assistant transcript delta、item completion、turn completion；
- best effort：部分命令输出和进度事件。

必须可靠送达的事件会等待消费者腾出容量；允许丢弃的事件会累计 skipped 数量并发送 `Lagged`。如果被丢掉的是一个需要客户端回答的 server request，客户端还会主动 reject，避免服务端永远等待审批结果。

这里让我想到数据库 change stream 或 replication log：并不是所有消息价值相同。丢一条进度刷新只是 UI 不够平滑，丢一条完成事件却可能让状态机永远卡在 running。背压策略实际上编码了业务正确性。

## 9. 安全不是外层开关

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

本次源码阅读本身就验证了这条链路。第一次运行 Cargo 测试时，它需要创建：

```text
~/.cargo/git/db/crossterm-...
```

当前工作区沙箱只允许写仓库和临时目录，因此命令以 `Read-only file system` 失败。扩大权限后，同一条测试才开始下载锁定依赖并编译。这个失败不是麻烦的噪声，它恰好说明“工作区可写”不等于“整个 home 可写”，而工具链缓存也是需要显式授权的副作用。

## 10. 持久化为什么同时需要 JSONL 和 SQLite

Codex 的持久化不是单一数据库：

- `codex-rollout` 保存会话事件、归档和恢复所需的 rollout；
- `codex-state` 从 rollout 抽取 metadata，镜像到本地 SQLite；
- core 负责 backfill、扫描和两者之间的编排。

这是一种 event log + materialized index 的组合。

JSONL rollout 更接近事实来源：它适合顺序追加、回放和保留完整历史。SQLite 更适合 `thread/list`、搜索、排序和状态查询。只保留 SQLite 会让原始执行轨迹难以重建；只扫描 JSONL 又会让列表和检索成本不断增加。

对我来说，这是 Codex 源码里最像数据库系统的一部分：写路径保留事件，读路径维护索引，两者允许通过 backfill 修复派生状态。

## 11. Skills、Plugins、Apps 和 MCP 在哪里接入

这些扩展机制名字很多，容易在第一次阅读时混在一起。我目前的理解是：

- Skills 主要向模型注入可发现的工作流和本地说明；
- Plugins 把 skills、apps、MCP 等能力组织成可安装单元；
- Apps/Connectors 表达外部应用能力与认证状态；
- MCP client 把外部 server 的 tools/resources 接入当前 thread；
- MCP server 则反过来把 Codex 暴露给其他 MCP client。

它们最终都需要回答相似的问题：什么时候被发现、怎样进入当前 turn、是否需要认证或审批、调用结果如何变成 Item/Event、状态怎样展示给客户端。

因此扩展系统并不只是“多注册几个工具”。它会穿过 config、protocol、core、App Server 和 UI 多层，这也是新功能很容易顺手被塞进 `codex-core` 的原因。

## 12. 我实际做的源码实验

### 实验一：建立 crate 地图

我先从 `Cargo.toml`、各 crate README、`lib.rs` 和 `main.rs` 建立分层，而不是直接打开最大的文件。结果确认：

- Rust workspace 是产品主干；
- `codex-cli` 是 composition root；
- TUI/Exec 共享 App Server client；
- `codex-protocol` 是内部领域类型；
- `codex-app-server-protocol` 是外部控制面 contract；
- rollout 与 state 分担事实记录和查询索引。

### 实验二：只 fetch 当前分支

为了不让调研过程无意更新多个 remote branch，我使用显式 refspec 只更新 `origin/main`。这也让我看到本地源码与最新远端之间已经有 4697 个提交差距，因此文章必须绑定本地 commit。

### 实验三：解析 Cargo workspace

仓库锁定 Rust 1.93，但本机只有 1.94 工具链。直接运行 Cargo 时，rustup 尝试安装 1.93，又因为沙箱不能写 `~/.rustup/tmp` 而失败。

我没有把这个问题伪装成“源码不能编译”，而是显式使用已有工具链：

```bash
RUSTUP_TOOLCHAIN=1.94.0 cargo metadata --no-deps --format-version 1
```

metadata 成功解析，说明 workspace manifest 至少能被当前 Cargo 理解。但这不等于官方 1.93 构建已验证，因此文章仍然保留环境偏差。

### 实验四：运行最小协议测试

随后执行：

```bash
RUSTUP_TOOLCHAIN=1.94.0 cargo test -p codex-protocol --lib
```

首次运行因 `~/.cargo/git` 只读失败；获得依赖缓存写入和网络权限后，冷构建结果为：

```text
Finished test profile in 2m 29s
running 140 tests
test result: ok. 140 passed; 0 failed; finished in 0.06s
```

这个结果有两层含义。

第一，`codex-protocol` 的序列化、permission model、sandbox semantics、event mapping 和 model types 在当前本机工具链下通过了单元测试。

第二，测试本身只有 0.06 秒，绝大多数时间花在第一次获取和编译依赖。大型 Rust monorepo 的日常反馈速度高度依赖缓存、crate 边界和测试选择；“只跑一个轻量 crate”在冷环境里也不一定轻。

## 13. 我目前形成的几个认识

### Codex 首先是 runtime，其次才是 CLI

CLI 可以被替换，TUI 可以重写，IDE 可以走远程 App Server，但 thread/turn/item、工具循环、审批、沙箱和持久化仍然存在。把产品理解成 runtime 后，很多 crate 的位置会自然清晰。

### App Server 是内部架构边界，不只是外部 API

TUI 和 Exec 走 in-process App Server，使协议成为主路径。它有点像数据库中本地 client 仍然走统一 query frontend：减少了一条“只有内部调用才知道”的隐藏语义。

### Agent 的复杂度主要来自副作用

如果只有 prompt -> text，系统不会需要这么多 crate。真正复杂的是命令、文件、网络、MCP、审批、恢复、并发事件和客户端背压。模型调用只是控制循环的一部分。

### 安全策略需要端到端传播

权限如果只存在于 UI checkbox，就无法约束内部调用；如果只存在于 sandbox，又无法给用户解释和审批。Codex 把它放进 config、protocol、turn 参数、policy decision 和平台执行器，是更可靠但也更复杂的做法。

### `codex-core` 的“大”是一种架构信号

core 当前承担太多编排责任。继续阅读或贡献时，不能因为“这里什么都能访问”就默认把功能放进去。一个新概念若可以独立成 crate，或者已经有 config、plugin、state、tools 等归属，就应先维护边界。

## 14. 如果开始 develop，我会从哪里下手

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

## 15. 我建议的源码阅读路线

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
   core/src/codex.rs

7. 一条工具执行路径
   core/src/tools/
   core/src/exec.rs
   sandboxing/

8. 持久化
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
- `codex-rs/rollout/src/lib.rs`
- `codex-rs/state/src/lib.rs`
