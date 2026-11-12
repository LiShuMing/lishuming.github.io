---
title: "从 raft-source 到单机部署：把多个 coding agent 组织成团队"
date: 2026-10-24T19:30:00+08:00
slug: "raft-standalone-multi-agent-engineering"
categories:
  - 工程实践
tags:
  - AI Agent
  - Raft
  - 本地部署
  - 源码分析
  - 多智能体
  - TypeScript
description: "为什么同时开几个终端跑 Agent 很快就会陷入互相踩文件与“编译通过即完工”的混乱？从 raft-source 的跨运行时协作模型出发，拆解状态边界、单机化改造与分层验证实录。"
draft: false
toc: true
math: false
---

如果你试过在同一个代码仓库里同时打开三个终端窗口，一边让 Claude Code 改后端接口、一边让 Codex 补前端表单、再来一个会话跑构建脚本，大概不出半小时就会撞上三件让人头疼的事：

1. **上下文随进程烟消云散**：某个终端会话一旦关闭或切换模型工具，“刚才为什么否决了方案 A”“哪几个边界已经测过了”全锁在各自的本地转录文件里，接手的下一个 Agent 只能从头盲猜。
2. **并发修改互相踩踏**：两个 Agent 都觉得自己在“顺手修一下公共类型文件”，没有明确的写入范围隔离，后提交的一方直接把前一方未提交的改动覆盖得干干净净。
3. **“命令返回 0 = 大功告成”的幻觉**：构建脚本退出了 `0`，或者某个 Agent 把任务状态标成了“待评审”，大家就以为系统已经跑通了——直到真正拉起服务，才发现 TypeScript 配置里写着 `noEmit: true`，磁盘上连一个可执行的后端 JS 文件都没生成。

要让多个编码智能体（Coding Agent）真正像一支靠谱的工程团队那样配合，核心难点从来不是“多拉起几个模型进程”，而是**把组织记忆、任务归属、写入契约与独立验证从单次模型对话里剥离出来**。

这也是 [raft-source](https://github.com/botiverse/raft-source) 吸引我深入读源码并动手做单机化改造的原因（注：这里的 Raft 是人与 Agent 共用的团队协作工作区，与分布式系统里的同名共识算法无关）。它没有试图重新发明一个替代 Claude Code 或 Codex 的底层编码循环，而是把这些异构工具放进了一套接近真实研发团队的协作结构里：人在频道（Channel）里抛出问题，特定职责的 Agent 认领任务，在消息线程（Thread）里沉淀讨论与交接证据，最后交付验收。

要判断这套模式能不能以个人可负担的维护成本长期跑起来，我们需要先看清它在架构上怎样解耦“组织状态”与“底层执行器”，再穿过源码与本地单机模式（Standalone Mode）的改造实战，看看从“源码能读通”到“单机真能跑”之间究竟隔着多少工程暗礁。

---

## 一、把“组织账本”与“执行引擎”拆开：raft-source 的核心抽象

在单智能体工具里，“会话（Session）”就是一切：人对着一个黑盒对话框发指令，历史记录与工具状态都绑死在这一个 CLI 进程上。一旦你想让 Claude Code 负责实现、让 Codex 负责交叉审查，两边就没有共同的任务视图了。

翻开公开镜像 [`db5e399`](https://github.com/botiverse/raft-source/tree/db5e399) 的源码，可以看到 Raft 用上下两层边界解开了这个死结：

- **上层是独立于模型的组织账本**：在 `packages/server/src/db/schema.ts` 中，服务端数据库专门持久化了 Agent 的固定身份、运行时（Runtime）配置、会话映射以及所在的执行机器信息；`channelAgents` 与 `channelHumans` 把人类成员和智能体放在同一套频道成员体系下管理，所有上下文讨论通过 `parentMessageId` 串成结构化线程；而每个 Agent 的岗位职责（`agents.description`）则在拉起任务时经由 `drivers/systemPrompt.ts` 动态注入。这样一来，哪怕底层换了模型或重启了机器，任务的来龙去脉、负责人和讨论记录依然完好地留在频道线程里。
- **下层是保留工具原生差异的驱动接口**：在 `packages/daemon/src/drivers/types.ts` 中，`RuntimeDriver` 抽象出了四个极简的核心钩子——`spawn()`、`parseLine()`、`encodeStdinMessage()` 与 `buildSystemPrompt()`，并在同目录的 `index.ts` 注册具体实现，配合 `DaemonCore` 与 `AgentProcessManager` 统一纳管子进程生命周期与事件流规范化（服务端自身的翻译 Provider 等辅助功能则直接调 OpenAI 兼容接口或进程内 SDK）。

这层 `RuntimeDriver` 设计得很克制：它并不妄想让 Claude Code 和 Codex 互相读取对方的原生会话文件，而是只要求它们在“接收任务输入、吐出标准化进度事件、汇报结束状态”这几个交接面上对齐。底层工具各自擅长的代码搜索、文件编辑与终端执行能力原封不动，上层团队协作却有了一本统一的台账。结合后文单机化改造采用的嵌入式 `PGlite` 存储底座，可以把这套“上层共享关系型账本 + 下层异构 CLI 驱动”的协作拓扑浓缩为一张全景图：

![Raft 单机多智能体共享关系型账本架构：人类编排方与异构 Worker Agents 围绕嵌入式 PGlite 持久化的 Channels 线程、Tasks 乐观锁工单及内存 Worker Leases 协同运转](/images/diagrams/gpu-agent-raft-ledger.svg)

---

## 二、横向坐标：执行工具、编排框架与协作工作区各管哪一段？

把多个 Agent 组织起来并不是只有这一种解法。在决定自己维护一套协作服务之前，不妨先沿着**“代码在哪里跑、状态归谁存、交接怎么定”**这三个维度，把市面上的主流方案放在同一张坐标系里比一比：

| 方案类别 | 代表项目 / 产品 | 核心职责边界 | 留给工程团队自己补齐的部分 |
| --- | --- | --- | --- |
| **原生编码 CLI 与内建团队** | [Codex CLI](https://developers.openai.com/codex/cli/) / [Cloud](https://developers.openai.com/codex/cloud/)、Claude Code（含 [Subagents](https://code.claude.com/docs/en/sub-agents) 与实验性 [agent teams](https://code.claude.com/docs/en/agent-teams)） | 直接读写仓库与跑命令；单生态内支持子代理隔离上下文或同构实例通信 | 跨异构工具（如 Claude + Codex 混编）时缺乏统一的外部任务认领与留痕台账 |
| **云端并行开发沙箱** | Cursor [Cloud Agents](https://cursor.com/docs/cloud-agent)、Cognition Devin（及[收购 Windsurf](https://cognition.com/blog/windsurf) 后的 IDE 协同布局） | 在云端隔离虚拟机中后台并行跑多个任务，解放本地算力与在线要求 | “多个沙箱同时跑”不等于团队协作；跨任务的接口对齐、冲突合并与验收仍靠人工串联 |
| **应用级智能体开发 SDK** | OpenAI [Agents SDK](https://developers.openai.com/tracks/building-agents)、[Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk/overview)、[LangGraph](https://docs.langchain.com/oss/python/langgraph/overview)、[CrewAI](https://github.com/crewAIInc/crewAI)、[OpenHands](https://github.com/OpenHands/OpenHands) | 提供代码级的控制权交接（Handoffs）、护栏（Guardrails）、状态图持久化与追踪（Tracing） | 需要开发者自己用代码搭建交互界面、频道结构、持久身份与审批闭环 |
| **角色化软件工程编排** | [MetaGPT](https://github.com/FoundationAgents/MetaGPT)、[ChatDev](https://github.com/OpenBMB/ChatDev) 2.0、[Microsoft Agent Framework](https://learn.microsoft.com/en-us/agent-framework/overview/) | 用产品、架构、研发等角色与标准作业程序（SOP）驱动多步产物生成 | 预设流水线在高度探索性的存量大仓库排障与局部重构中往往需要大幅裁剪 |
| **个人常驻跨入口助手** | [OpenClaw](https://openclaw.ai/blog/introducing-openclaw)（[开源仓库](https://github.com/openclaw/openclaw)） | 跑在个人设备上，打通日常聊天软件入口，保持个人助手的跨端连续性 | 侧重个人单线助理体验，而非多角色代码仓库分工与评审流转 |
| **跨运行时协作工作区** | **[raft-source](https://github.com/botiverse/raft-source)** | 把异构编码 CLI 接入人与 Agent 共用的长期频道，用认领、线程与投递记录维持分工 | 需要自行承担中心服务部署、运行环境适配以及人工写入隔离与验收规范 |

从这张表就能得出一个很务实的选型准绳：**如果你的日常研发完全在单一工具（比如纯 Claude Code）里就能闭环，优先用好它自带的 Subagents 就够了，根本没必要额外背一套服务端架构。** 只有当你确实需要让异构编码引擎（例如 Claude Code 搭配 Codex）、多台执行机器以及人类开发者在长期演进的项目里共享同一套任务归属与交接上下文时，Raft 这类协作工作区才真正站到了甜点位上。

---

## 三、从管好一个 Agent 到管住一群 Agent：源码里的四道工程关卡

把多个 Agent 拉进同一个频道只是第一步。顺着 `raft-source` 的核心模块往下读，能清楚看到要把松散的模型调用变成可控的工程协作，必须依次跨过四道关卡。

### 1. 身份常驻与会话续接：进程活着不代表能接活

单个 Agent 首先要解决生命周期对齐：后台进程还在不在？服务重启后是接着上回的上下文聊，还是开新局？当前正在忙着改代码时，频道里又有人 `@` 它怎么办？

Raft 把“Agent 的长期身份”和“某次具体的运行时 Session”拆开存储：重启时优先尝试恢复既有会话，显式重置时才切换新会话，从而保证工作目录和频道成员身份不会随一次对话结束而丢失。同时，各驱动显式声明自身支持的会话恢复、上下文压缩（Compaction）与忙时投递策略——通过 `busyDeliveryMode` 精细区分是直接塞入运行中流（`direct`）、转为通知提醒（`notification`）还是暂不打扰（`none`）。这提醒我们，在多智能体系统里，**“进程存活”“消息已投递”和“任务真正执行完毕”是三个截然不同的状态**，每一层都得有独立的确认信号。

### 2. 启动上下文与跨会话记忆预算：给 `MEMORY.md` 装上限流阀

长周期协作最怕两件事：要么新开会话彻底失忆，要么把历史记录一股脑全塞进提示词直接撑爆上下文窗口。

在 `packages/daemon/src/drivers/claudeInputBudget.ts` 中，守护进程（Daemon）在真正拉起命令行之前做了一道轻量级启动预算预检：默认上限设为 `200,000` tokens（匹配特定长窗口模型名时放宽至 `1,000,000`），固定预留 `32,000` tokens 余量；它按可见文本的 UTF-8 字节数除以 `3` 向上取整做快速估算，一旦超限就在启动进程前直接抛出 `INPUT_TOO_LARGE` 快速失败。

而在跨会话记忆注入上，`startupMemoryBlock.ts` 采取了“只注新局、不截原件”的策略：只有当服务端显式开启 `constructedWakeContext` 且本地未禁用时，才为**全新启动**的会话注入 `MEMORY.md` 头部摘要；恢复既有会话时则直接跳过，避免重复污染上下文，且预算裁剪只作用于注入视图而不破坏磁盘上的源记忆文件。顺着这个机制设计日常记忆文件，最有效的写法就是**只把 `MEMORY.md` 当作精简的索引目录**——记录任务 ID、核心架构拍板、关键证据文件路径与下一步动作，每次唤醒后再去实时核查分支、进程等易变状态。

### 3. 权限幻觉：别把状态机里的 `in_review` 当成安全门禁

多人多 Agent 混编时，最危险的误区是看到界面上有个“评审中（`in_review`）”状态，就以为所有高危操作都有人把关。

仔细核对源码里的权限链路，会发现不同层面的门禁松紧截然不同：

- **平台与凭证层**：服务端在 `routes/internalAgentApi.ts` 中确实对第三方集成登录等特定平台级动作返回 `approval_required`，交由 `actionCardsService.ts` 的 `prepareActionCard()` 与 `executeActionCard()` 渲染交互式审批卡片；同时 Daemon 通过绑定本地回环来源的代理代发请求，避免让 Agent 子进程直接拿到中心服务的原始凭证。
- **底层 CLI 执行层**：一旦进入本地仓库干活，为了不让无人值守循环卡在终端交互提示上，`codex.ts` 默认配置了 `approvalPolicy: "never"`，而 `claudeLaunch.ts` 则直接传了 `--permission-mode bypassPermissions`！
- **任务流转层**：`taskService.ts` 里的任务虽然沿着 `in_progress → in_review → done` 推进，但 `in_review` 在代码里并没有强制绑定“必须由人类管理员点击通过”的硬锁——任何拥有该频道写权限的成员都能推动状态流转。

看清这三层真相，实操纪律就非常明确了：**绝不能指望状态标签替你守门。** 团队在开跑前必须明确约定谁负责写代码、谁负责跑独立验证、谁有资格把任务置为 `done`，并且一切验收只认真实的 Git Diff、测试输出与失败日志。

### 4. 认领防重与注意力路由：锁住了工单，锁不住共享文件

当频道里同时有好几个 Agent 待命时，怎么防止一窝蜂抢同一个活，或者一句话唤醒所有 Agent 空耗 Token？

在工单层面，`packages/server/src/services/taskService.ts` 通过 `claimTaskDetailed()`、`claimTask()`、`unclaimTask()` 和 `assignTask()` 管理任务认领与改派，利用版本号（Revision）乐观锁拦截并发抢单冲突；任务交接则依托改派接口与关联线程回复来完成。在消息降噪层面，`mentionDeliveryOccurrences` 完整记录服务端派发决策、Daemon 接收与确认（Ack）或终止错误，而 `threadFollows` 把“谁能在频道里看到消息”与“谁订阅了该线程需要被实时唤醒”彻底解耦——不在关注列表里的 Agent 不会被无关讨论反复拉起。

但工单锁有一个天然盲区：**数据库里锁住了 Task A 和 Task B，文件系统里可没人替你锁住共享的工作树！** 就在我准备动手改本地单机部署代码时，一查 `git status` 就发现工作区里已经躺着 19 个被修改过的运行时表单相关文件。如果此时直接放两个 Agent 进去自由发挥，这些半途改动分分钟就会被搅成一团乱麻。因此，多 Agent 并行要么配合独立的 Git Worktree 物理隔离目录，要么在认领任务的第一时间就白纸黑字划定各自允许触碰的目录边界（例如本轮改造严格把写入范围锁死在桌面环境配置、`standalone` 目录、构建脚本和 `workerPool` 内，对那 19 个存量改动秋毫无犯）。

把工单版本号乐观锁（`Revision` CAS）、启动上下文预算快检、进程心跳续租（`Lease` TTL）以及跨引擎交接串到一起，单个任务在崩溃与异常场景下的完整状态流转如下：

![多智能体任务认领与 Worker Lease 崩溃恢复状态机：任务经前置依赖就绪进入 Ready，由 Revision CAS 原子认领并绑定 Lease 心跳续租；进程失联自动过期回收重入待领池，超预算或耗尽重试则转入 Failed](/images/diagrams/gpu-agent-raft-lease-lifecycle.svg)

---

## 四、单机化改造实战：砍掉重型外部依赖，逐层击穿“假构建”陷阱

摸清了协作机制，接下来就要面对最现实的落地问题：原版公开镜像的 `./raftdev start` 默认是一套标准的云原生重型全家桶——要同时拉起 PostgreSQL、Redis、对象存储、Server、Web 前端和 Daemon。如果只是个人或小团队日常调度几个本地 Agent，光维护这套底层基础设施的心智负担就已经超过收益了。

于是我在本地工作区基于单机模式（Standalone）分支做了一轮深度改造与验证。整个过程就像剥洋葱一样，把“看起来能跑”和“真正可交付”之间的四层断层挨个暴露了出来。

### 第一层：用嵌入式 PGlite 与内存租约替代外部数据库和缓存

单机化的核心思路是把外部中间件收进单进程与独立本地目录：

- **持久化数据收归 PGlite**：在本地 `standalone/config.ts` 中将数据库连接指向 `pglite://...`，由 `db/index.ts` 加载嵌入式 PostgreSQL 引擎 PGlite，并通过 `db/pgliteMigrations.ts` 自动跑完表结构迁移；同时将上传附件与签名密钥（Signing Key）统一收进独立的本地状态目录，主动清空环境变量里可能残留的云端数据库、对象存储与通知配置，防止本地实例误连远端资源。
- **协调状态退化为内存表**：原本依赖多实例共享存储的连接租约（Lease）、唤醒锁与在线活跃状态，在单 Server 实例前提下直接由 `standalone/localReplicaStateStore.ts` 存入进程内存 `Map`，进程重启后按需重建即可。
- **保留中心与执行机分离能力**：即便控制面缩成了单机，执行任务的机器依然可以按需外接。`standalone/remoteComputerRoutes.ts` 在登记远程节点与下发 Runner 时严格核验包名、体积与 SHA-256 校验和；执行端 `daemon/src/standaloneRemote.ts` 先通过 `setup` 走浏览器设备授权，再用 `start` 复用已批准的机器凭证。这也顺带揭示了未来的备份边界：单备份一份 PGlite 数据库是不够的，上传文件、签名密钥、机器接入凭证与各 CLI 原生会话目录各有归属，缺一不可。

### 第二层：修补启动预检副作用与桌面端环境身份串台

真正跑单机启动链路时，很快抓到了两处状态污染隐患：

1. **未验明前端产物就提前落盘写密钥**：旧版入口在还没检查前端静态页面 `index.html` 是否存在之前，就先一步调了 `configureStandalone()`，导致一旦忘记先编前端包，进程虽然报错退出，磁盘上却已经建好了一半的空状态目录和签名密钥。修改后的 `standalone/startup.ts` 把前端入口可读性检查严格前置，缺失时干净利落地提示构建前端或指定 `RAFT_STANDALONE_WEB_DIST` 并直接退出。配套的 6 项配置、预检与路由测试（含子进程验证非零退出码且零目录残留）全部通过。
2. **桌面壳切换环境时的旧缓存串台**：`packages/web/src/desktopRuntimeEnvironment.ts` 原本只靠一个递增数字 `generation` 判断环境身份；当共用本地存储切换不同目标地址时，很容易拿着旧状态连向新后端。修复后将环境身份键扩展为同时比对环境 ID、前端地址、API 地址、Socket 地址与 `generation`（升级后自动触发一次旧数字缓存清理与重登），34 项关联单测全部跑通。

### 第三层：填平 `noEmit: true` 构建空洞与 Worker 加载器冲突

这是整轮改造中最典型的一处**“命令成功 != 产物可用”**案例。

原本仓库里的 `standalone:build` 命令跑得顺风顺水，依次编出了 Daemon、Runner 包和 Web 前端——唯独漏掉了最核心的独立 Server！点进 Server 的构建配置一看，它虽然调了 `tsc`，但 `tsconfig` 里赫然写着 `noEmit: true`：编译器只做检查、根本不往磁盘吐任何 JavaScript 文件。之前之所以能在开发环境跑起来，全靠源码模式下的即时加载器撑着。

为了让 Server 脱离源码加载器独立运行，我编写了实验构建脚本 `scripts/build-standalone-server.mjs`，顺着入口递归遍历依赖图、重写相对路径的 JavaScript 导入后缀、把静态引用的 Worker URL 一并纳入编译图谱，并同步拷贝数据库迁移脚本与版本元数据到全新输出目录。跑完构建后做静态可达性审计：**共生成 709 个模块，从主入口递归解析出的 706 个 JS 模块相对导入全部闭环**（多出的 3 个模块正好来自静态 Worker URL 分支）。

紧接着，后台工作线程池（Worker Pool）又露出了第二个坑：`services/workerPool/workerExecutor.ts` 在拉起子线程时，原本无脑往启动参数里塞 `--import @oxc-node/core/register` 源码加载器。一旦切成编译后的纯 JS 产物，这个多余的参数反而会导致脱离开发依赖时直接炸裂。我把这里的分发逻辑改成了按入口后缀分流——`.ts` 入口保留开发态加载器，编译后的 `.js`、`.mjs`、`.cjs` 入口直接干净地继承父进程参数。用纯 Node 实测编译产物，从 Worker 注册、双向消息往返、业务异常抛错到模拟线程崩溃后自动拉起恢复全部通过，实测回传的 `process.execArgv` 为干净的 `[]`。

### 第四层：临门一脚撞上宿主机 `GLIBC_2.27` 硬墙

如果只看“709 个模块构建闭环 + 40 项单测全绿 + Worker 崩溃恢复实测通过”，很容易产生一种“单机发行版已经大功告成”的错觉。但只要恪守端到端验收纪律——在全新状态目录与随机本地端口上真正拉起编译后的 `standalone.js`——现实立刻浇下一盆冷水：

服务在进入 HTTP 端口监听之前直接退出，报错指向图像处理原生依赖 `sharp` 要求宿主机提供 **`GLIBC_2.27`**，而当时这台实验宿主机的底层 C 库停留在老旧的 `glibc 2.17`，且本机 Node 版本为 `22.18.0`（低于仓库要求的 Node `24`）。此外，当前的实验构建脚本基于 `transpileModule` 做单文件转译，尚未跑全仓严格类型检查，外部 `node_modules` 仍通过符号链接复用本地工作区安装（产物标记为 `compiled-workspace`）。

这恰恰印证了系统工程里最朴素的那条铁律：**静态模块完整、局部线程跑通、HTTP 服务成功监听、以及打成跨机器可搬运的独立安装包，是四道完全不同的验收门禁，任何一道都不能替下一道背书。**

---

## 五、把多个 Agent 用成真正团队的实战纪律

回看这一路从源码拆解到单机改造的折腾，无论是用 Raft 还是用其他编排工具，想让多个 Coding Agent 真正帮上忙而不是添乱，有四条纪律必须在动手前立好：

1. **任务认领必须绑定“物理写入契约”**：人在频道下发任务时，先写清目标、禁止改动的边界与验收命令；让 Claude Code 写功能、让 Codex 做审查或补测时，要么通过 Git Worktree 拆开物理目录，要么明确锁定各自允许修改的子目录列表，绝不让两个活跃 Agent 同时写同一批文件。
2. **交接记录只认“四要素证据包”**：上下游 Agent 交接或提请人工评审时，严禁只留一句“已完成，请查收”，必须在任务线程里留下**代码改动位置、当前分支/进程状态、实际跑过的验证命令及输出、以及明确尚未覆盖的边界**（就像上文如实区分“Worker 线程已打通”和“`sharp` 卡在 `glibc 2.17` 尚未监听 HTTP”一样）。
3. **跨会话记忆做减法**：把 `MEMORY.md` 严格控制在目录索引级别，只存长期有效的架构决定与文件路径；进程存活、端口号、分支状态这类随时变化的事实，一律要求 Agent 唤醒后现场执行命令核实。
4. **先打通最轻量的控制面连接，再决定重打包方案**：比如后续把日常使用的 Mac 接入这套体系时，最省力的路径是先让本地桌面或 Web 控制台连上跑在达标 Linux 环境（满足 Node 24 与 `GLIBC_2.27+`）上的单机中心，把重计算和工具执行留在远端 Runner；等整套认证握手、目录搬迁与备份恢复演练全部跑熟之后，再去权衡值不值得用 Electron 或 Tauri 封装全本地一体化安装包。

下一步的计划也很明确：先在满足 `glibc` 与 Node 24 要求的环境里跑通完整的 HTTP 启动与备份恢复闭环，随后在自己的数据库实验仓库里拉起 Claude Code 与 Codex 的真实双引擎分工实验——把环境维护成本、线程沟通开销和人工审阅耗时全算进总账里，看看这套“人类定契约、多 Agent 分工留痕”的团队模式究竟能跑出多大的复利。

---

> **源码与实验基线说明**：文中上游架构分析基于公开镜像 [`botiverse/raft-source`](https://github.com/botiverse/raft-source/tree/db5e399) 快照 `db5e399`（Release `v1.21.23-source.1`，采用 FSL-1.1-ALv2 源码可见协议，按版本导出且不接收外部 PR）；单机模式（Standalone）改造与构建验证基于本地工作区提交 `849d036`，实验记录截至 **2026-10-24**。
