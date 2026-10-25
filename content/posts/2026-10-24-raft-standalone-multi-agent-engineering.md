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
description: "从人类组织式协作理解 Raft，比较 coding agent 产品与开发框架，记录 standalone 改造、构建验证及尚未打通的部署边界。"
draft: false
---

## 1. 缘起：为何对 raft-source 感兴趣

我对 [raft-source](https://github.com/botiverse/raft-source) 感兴趣，首先是因为它把 Claude Code、Codex 等 vibe-coding 工具放进了一套接近人类组织的协作关系：人在频道里提出问题，Agent 认领任务，在相关线程中讨论，再交付评审。多智能体（Multi-Agent）的吸引力在这里变得具体：换一个执行工具以后，任务归属、讨论记录和交接依据是否还能留下来？这比单独增加几个模型进程，更接近我想持续使用的工作方式。本文的 Raft 是人与 Agent 共用的协作平台，与同名的共识算法（Consensus Algorithm）无关；公开镜像采用 FSL-1.1-ALv2，属于源码可见（Source-available），不是 OSI 意义上的开源，并且按 release 导出、不收 PR。

这种组织方式有源码上的对应关系。公开镜像 `db5e399`（Release v1.21.23-source.1）中的 `packages/server/src/db/schema.ts` 保存 Agent 身份、运行时（Runtime）配置、会话和执行机器信息，`channelAgents` 与 `channelHumans` 保存频道成员关系；线程用 `parentMessageId` 连接父消息。岗位说明由 `agents.description` 经 `drivers/systemPrompt.ts` 注入。人和 Agent 共用频道组织，平台还把身份、成员关系和讨论结构从一次模型对话中分离出来。这里的版本定位沿用原有源码阅读记录，公开材料可从[固定快照](https://github.com/botiverse/raft-source/tree/db5e399)追溯。

执行侧则保留工具之间的差异。`packages/daemon/src/drivers/types.ts` 的 `RuntimeDriver` 通过 `spawn()`、`parseLine()`、`encodeStdinMessage()` 和 `buildSystemPrompt()` 对接进程、输出、输入和提示；驱动注册位于同目录的 `index.ts`，事件规范化与 `DaemonCore`、`AgentProcessManager` 一起管理运行过程。我的判断：这层抽象的价值是让组织记录独立于执行工具，而不是承诺 Claude Code 与 Codex 的原生会话可以互换。还要限定其覆盖范围：Agent 主对话经运行时接入，不代表整个仓库没有直接请求模型服务的代码，server 的翻译 provider 就有 OpenAI-compatible 实现，部分驱动也使用进程内 SDK。

接下来的问题是，这样的工作区能否以个人可以维护的成本运行。本文面向已经使用 coding agent、希望理解协作和部署边界的工程师：先比较产品与框架承担哪些责任，再分析单个 Agent 和团队各自的难点，最后记录独立部署模式（Standalone Mode）的本地改造。部署实践沿用 `849d036f0e26e06dd518c72f988119450b0a87b0` 工作区基线，本地修复尚未发布；它与前述公开镜像分析分开。实验结果截至 2026-10-24，完整 HTTP 启动和 Mac 安装包均未验收，不能从源码中的接口推导出已经可用的发行能力。

## 2. 竞品与公司调研：执行、编排与个人助手各自承担什么

比较这些工具，需要先问任务在哪里执行、谁保存状态、谁决定交接。OpenAI 的 [Codex CLI](https://developers.openai.com/codex/cli/) 在本地仓库读取、修改文件并运行命令，[Codex cloud](https://developers.openai.com/codex/cloud/) 提供云端执行入口；[Agents SDK](https://developers.openai.com/tracks/building-agents) 则面向用代码构造应用，提供控制权交接（Handoffs）、护栏（Guardrails）和执行追踪（Tracing）。这些抽象分别处理执行与编排，SDK 能保存会话也不等于应用自动拥有频道、岗位和任务认领。我的判断：选现成 coding 产品，是把更多执行管理交给产品；选 SDK，则仍要自己定义业务状态、工具和审批责任。

Anthropic 同时提供 Claude Code 和可嵌入应用的 [Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk/overview)，后者复用 Claude Code 的 Agent 循环、工具和上下文管理能力。[子 Agent（Subagents）](https://code.claude.com/docs/en/sub-agents)可以有独立上下文与工具配置；[agent teams](https://code.claude.com/docs/en/agent-teams)允许多个 Claude Code 实例共享任务、直接通信，由团队负责人协调。官方文档仍将 agent teams 标为实验功能，默认关闭，并列出会话恢复、任务协调与关闭行为方面的限制。我的判断：如果工作已经集中在 Claude Code，先验证它自带的分工能力更省部署成本；只有确实需要跨工具保留组织关系时，才值得再引入一个协作平台。

Cursor 的 [Cloud Agents](https://cursor.com/docs/cloud-agent) 原称 Background Agents，在隔离的云端虚拟机中运行开发环境，并支持并行执行，不要求本地机器持续在线；并行能力本身不能证明任务之间已经形成协作闭环。Cognition 围绕 Devin 提供软件工程 Agent 产品，并在 [2025 年 7 月 14 日的公告](https://cognition.com/blog/windsurf)中宣布签署收购 Windsurf 的最终协议，范围包含产品、知识产权和品牌。公告中“未来数月整合能力”属于当时的计划，不能作为功能已经交付的证据。我的判断：考察这类产品时，应沿着任务输入、执行环境、结果审阅这条路径验证，不能从收购或并行执行直接推出跨运行时组织能力。

个人助手又提出另一组需求。[OpenClaw 的项目介绍](https://openclaw.ai/blog/introducing-openclaw)记录了 Clawdbot、Moltbot 到 OpenClaw 的更名过程，将其定位为运行在自有机器、通过既有聊天应用使用的助手；[源码仓库](https://github.com/openclaw/openclaw)提供开源实现。它关注个人跨入口使用助手的连续性。开发框架则让开发者自己组织执行：[OpenHands](https://github.com/OpenHands/OpenHands)提供软件开发 Agent 平台与 SDK；[CrewAI](https://github.com/crewAIInc/crewAI)以角色、任务和 Crews 表达协作，以 Flows 控制事件驱动流程；[LangGraph](https://docs.langchain.com/oss/python/langgraph/overview)提供有状态图、持久执行及人工介入能力。我的判断：这些方案把交互入口和状态设计交给了不同的责任方，不能只按 Agent 数量比较。

角色化的软件组织也已有不同实现：[MetaGPT](https://github.com/FoundationAgents/MetaGPT)用产品经理、架构师、工程师等角色与标准作业程序（Standard Operating Procedure）组织产物；[ChatDev](https://github.com/OpenBMB/ChatDev)将虚拟软件公司的 1.0 保留为旧版，2.0 已转向可配置的多 Agent 编排平台。[Microsoft Agent Framework](https://learn.microsoft.com/en-us/agent-framework/overview/)也提供 Agent 与多 Agent 工作流的构建能力，这里不把任何版本路线或预告当成正式可用能力。我的判断：Raft 所处的位置，是把现成的异构 coding 运行时放进人与 Agent 共用的长期工作区，用频道、认领、线程和投递记录维持协作。这个定位来自前述 Raft 源码事实，不表示其他产品无法扩展出类似能力，也不意味着 Raft 已解决部署和审阅成本。

## 3. 难点：先管好单个 Agent，再承担团队的协调成本

单个 Agent 的管理首先涉及工具执行与状态归属：它能调用什么工具、进程是否仍在运行、重启后延续哪次会话，都需要明确答案。Raft 将身份与运行时会话（Session）分开保存，重启尝试延续会话，重置则换新会话；工作目录和频道关系不必随一次对话结束而消失。驱动还声明会话恢复、上下文压缩（Compaction）以及忙时投递能力，`busyDeliveryMode` 区分 direct、notification 和 none。我的判断：统一接口能让平台知道应如何投递与恢复，但执行工具的行为仍须逐一验证；进程存在、消息被接受、任务正确完成，是不同的验收条件。

上下文（Context）与跨会话记忆（Memory）也需要各自的预算。`packages/daemon/src/drivers/claudeInputBudget.ts` 对 daemon 构造的启动文本预检：默认上限为 200,000 tokens，特定模型名匹配路径为 1,000,000，预留 32,000；估算方法是可见文本 UTF-8 字节数除以 3 后向上取整，超限在启动命令前报告 `INPUT_TOO_LARGE`。这不是模型服务的权威 tokenizer，也不覆盖 Claude 恢复会话时自行载入的历史。`startupMemoryBlock.ts` 在服务端 `constructedWakeContext` 显式启用且未被本地关闭时，为新会话注入 `MEMORY.md` 头部，恢复会话跳过注入；预算限制的是注入视图，不截断源文件。由此得到的使用建议是让记忆保存任务索引、决定、证据位置与下一步，并在重新操作前核对仍会变化的进程、分支和服务状态。

权限与评估不能由“有评审状态”代替。公开镜像中，`routes/internalAgentApi.ts` 对集成登录等特定操作返回 `approval_required`，再由 `actionCardsService.ts` 的 `prepareActionCard()`、`executeActionCard()`处理；与此同时，`codex.ts` 设置 `approvalPolicy: "never"`，`claudeLaunch.ts` 使用 `--permission-mode bypassPermissions`。README 描述 daemon 通过绑定本地来源的代理代发凭据，Agent 不直接持有 server 凭据；这些机制的边界不同，不能概括为每次工具执行都有人审批。任务服务中的 `in_review` 也不强制人工审核，有频道写权限的成员可以推动状态迁移。我的判断：实际使用前必须约定谁审阅、审什么、谁能置为 done，并用代码差异、测试条件和失败记录评价产物；本文没有完成模型质量或团队效率评估。

增加多个 Agent 后，任务编排（Orchestration）还要约束写入和交接。`packages/server/src/services/taskService.ts` 的 `claimTaskDetailed()`、`claimTask()`、`unclaimTask()`、`assignTask()`处理认领与改派，认领检查冲突、修改使用 revision，状态为 `in_progress → in_review → done`，评审可以退回。原有阅读没有找到独立 handoff API，交接通过改派和线程回复完成。任务冲突检查却不会自动隔离共享文件：这次本地实践开始时，工作区已有 19 个运行时表单相关文件被修改，修复保留了这些改动，并限定在桌面环境、standalone、构建脚本和 worker 范围。我的判断：认领应同时说明产物与写入范围，依赖步骤应明确交接；并发读取与并发修改共享 Git 状态，需要不同的约束。

通信成本来自投递和消费，而不只是消息数量。`mentionDeliveryOccurrences` 记录 server 决策、daemon 接收及 ack 或终止错误，`threadFollows` 保存关注来源、完成和取消关注；线程可见性与关注者投递分开，因此看到频道不等于每个 Agent 都要消费全部讨论。我的判断：让负责人在对应线程接收证据、完成后减少无关关注，有助于控制唤醒范围，但这里没有 token 节省或吞吐提升的测量。更多执行者还会增加重复读取、接口核对、合并与人类审阅工作；只有任务能够独立推进、交付证据足够清楚时，并发节省的等待才可能覆盖这些成本。

## 4. 单机改造与部署：保留持久数据，逐层验证启动链

单机改造的目标是降低个人部署的依赖数量，同时说清哪些状态能恢复。公开镜像 `db5e399` 的 `./raftdev start` 涉及 Postgres、Redis、对象存储、server、web 和 daemon，其中没有 `packages/server/src/standalone*`；下述 standalone 路径属于本地工作区，不能附上伪造的上游改动链接。本轮复用 PGlite 分支，`standalone/config.ts` 将数据库指向 `pglite://...`，把上传文件和签名密钥（Signing Key）放进独立状态目录，并清除可能继承的云端数据库、存储和通知配置；`db/index.ts` 选择嵌入式 PostgreSQL 实现 PGlite，`db/pgliteMigrations.ts` 负责迁移。`standalone/localReplicaStateStore.ts` 将连接租约（Lease）、唤醒锁和活动状态保存在内存 `Map` 中，退出后重建。这个取舍依赖单 server 实例，不能推广到需要共享协调状态的多副本环境。

中心与执行端仍可分开。`standalone/remoteComputerRoutes.ts` 检查登记权限，并在下载 runner 前校验包名、大小与 SHA-256；`daemon/src/standaloneRemote.ts` 通过 `setup` 进行浏览器设备批准，再以 `start` 复用机器接入状态。独立的私有状态目录不能绑定到另一中心，运行时配置留在执行机器上，runner 仍受 Node 版本和平台架构约束。这些是原有源码阅读确认的流程，本轮没有重新执行远程登记实验。它同时说明备份不能只看数据库：平台数据、上传文件、签名密钥、机器接入状态和原生会话各有职责，整套备份恢复还没有验收。

启动失败路径的修复先处理副作用顺序。原入口在检查网页 `index.html` 前执行 `configureStandalone()`，会先创建目录与密钥；修改后的 `standalone/startup.ts` 先检查网页入口是否可读，失败提示构建网页或设置 `RAFT_STANDALONE_WEB_DIST`。原有 6 项配置、预检和网页路由测试通过，其中子进程验证了缺少网页时非零退出且不创建状态目录。另一处是 `packages/web/src/desktopRuntimeEnvironment.ts` 的环境身份键：原先只比较数字 `generation`，现在同时比较环境 ID、前端地址、API 地址、socket 地址和 generation，避免共用存储时目标变了却沿用旧状态；原有 34 项相关测试通过。旧数字缓存升级后会清理一次、需要重新登录；这些测试没有证明曾发生凭据泄漏，也没有放宽官方环境校验，自建中心握手与桌面壳仍待适配。

构建链的缺口来自“命令成功”与“存在可运行产物”的差别。原有 `standalone:build` 构建 daemon、runner 包和 web，没有输出独立 server；server 的 `build` 调用 `tsc`，而配置为 `noEmit: true`。本地实验脚本 `scripts/build-standalone-server.mjs` 遍历依赖、重写相对 JavaScript 导入，将静态 worker URL 纳入构建图，并复制迁移和版本文件，要求输出到新目录。本次生成 709 个模块，从主入口递归检查的 706 个 JavaScript 模块均可解析且相对导入存在，差额来自静态 worker URL 引入的模块。后台线程还需独立修复：`services/workerPool/workerExecutor.ts` 对 `.js`、`.mjs`、`.cjs` 入口继承父进程参数，不再补 `--import @oxc-node/core/register`，`.ts` 保留加载器逻辑。原有集成验证覆盖真实 JavaScript 与 TypeScript worker；重建后的编译产物用纯 Node 完成注册、消息往返、处理器抛错、线程崩溃及恢复执行，回传 `process.execArgv` 为 `[]`。这证明了被测试的线程链路，不覆盖所有后台任务。

完整服务启动仍然失败。在全新状态目录、随机本地端口上直接运行编译后的 `standalone.js`，进程在监听前因 `sharp` 原生依赖退出：它要求 `GLIBC_2.27`，宿主机为 glibc 2.17；当时 Node 为 22.18.0，也低于仓库要求的 24。实验脚本使用 `transpileModule`，没有完成全仓库类型检查（Type Checking），外部 npm 依赖通过符号链接（Symbolic Link）复用现有安装，网页与 daemon 还需另供，产物标为 `compiled-workspace`。因此 709/706 的检查结果不能算可搬运发行包验收，也不能算 HTTP 运行通过。上述数字均是原实践记录，本次文章重写没有重跑 Raft 实验；符合要求的运行环境、完整启动、备份恢复和 Mac 实机验证仍是后续工作。

## 5. 使用思路：让协作留下可继续执行的依据

我希望把日常使用组织成围绕具体任务的协作：人在频道说明目标、约束和验收依据，由负责的 Agent 认领，在任务线程里积累决定和证据；实现与评审可以分给 Claude Code 和 Codex，但各自的写入范围需要先说清。这是下一步准备验证的用法，不是已经运行成熟的团队流程。对于有依赖的工作，我会要求交接包含代码位置、当前状态、执行过的检查及未解决问题，避免新会话只收到一句“继续之前的工作”。记忆入口只保留便于扫描的索引，细节按主题保存，恢复时重新核对易变事实。

我的判断：Raft 更值得用于跨运行时、跨机器、需要持续认领与留痕的任务，因为其持久身份、频道和投递状态正好承接这些需求。若一次工作在单个运行时中就能完成，额外部署中心、维护驱动与恢复数据的成本可能超过收益；若任务不断争用相同文件或人类审阅时间，增加 Agent 也不会自然缩短交付。审批要求严格时，还必须先核对运行时权限与平台动作卡片的实际覆盖范围，不能把 `in_review` 当成强制人工关卡。是否适合长期使用，需要用交接、冲突、返工和恢复成本检验，而本文尚无这些收益测量。

这次实践给我的具体心得是，组织抽象与运行交付需要分别拿出证据。频道、认领和线程让我能描述谁负责什么，worker 验证让我确认某条执行链不再依赖源码加载器，`sharp` 的失败则说明部署环境尚未满足要求。每份证据都有用，但不能互相代替。对 Mac 接入，我仍倾向先让桌面控制台连接现有中心、把任务留在执行机器，再讨论完全本地运行；前者主要验证连接、认证和握手，后者还涉及本机依赖、数据目录、后台进程、升级与恢复。是否选 Electron 或 Tauri，应等这些接口和打包链核对后决定。

接下来先在符合项目要求的 Node 与原生依赖环境打通 HTTP，收齐可搬运资产，再做移动目录与备份恢复演练；桌面方向继续验证自建中心协议及 macOS 的安装、退出、重启和升级。协作方向则准备在自己的数据库实验仓库，让 Claude Code 与 Codex 分工实现和评审，记录交接、合并与返工，再与运行时自带团队能力及相关框架比较。我的判断：只有把环境维护、协调和人工验证一起算进去，才能决定是否长期保留这层组织；当前最该完成的是可复核的恢复与交付验证。
