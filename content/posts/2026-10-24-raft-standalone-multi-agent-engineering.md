---
title: "从源码构建 Raft standalone：部署适配、故障修复与多 Agent 协作设计"
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
description: "记录 Raft standalone 从源码运行到实验 JavaScript 构建的适配过程，解释状态、运行时、上下文和记忆的边界，以及多 Agent 任务认领、协作投递和证据管理的优化方向。"
draft: false
---

把一个 AI 协作平台放到自己的机器上，遇到的问题很快就会超出“能不能启动”。网页关闭以后任务能否继续执行？模型对话损坏以后还能不能接着工作？换一台执行机器，需要搬的是数据库、工作目录，还是模型运行时的会话文件？这些问题决定了部署边界，也决定了多智能体（Multi-Agent）协作是否可维护。

这篇文章记录一次基于 [Raft 源码](https://github.com/botiverse/raft-source)的独立部署模式（Standalone Mode）适配与迭代。这里的 Raft 是人和 Agent 共用的协作平台，与同名的一致性算法不是同一个项目。目标是复用已有网页和运行时，在本地保留协作数据，再逐步补上桌面接入与发行能力。

阅读源码时使用的基线是 `849d036f0e26e06dd518c72f988119450b0a87b0`。下文的四批修复是该工作区中的本地改动，尚未发布；构建与测试结果截至 2026-10-24。完整 HTTP 服务的编译产物启动仍受原生依赖兼容性阻塞，Mac 安装包也没有实机验证。本文讨论已走通的链路、失败的尝试和下一步的工程条件；后半部分（§7–§11）回到源码结构，谈我为什么喜欢这套 agentic 架构、它与同类开源项目的对比，以及面向个人的 standalone 改造思路。

## 1. 先确认平台管理的状态，再决定如何本地化

Raft 的主要职责是保存协作事实，并管理执行这些工作的运行时（Runtime）。把它当成一个聊天网页，会漏掉后台进程、任务归属和数据恢复；把它当成一个统一模型 SDK，又会把外部运行时的上下文（Context）管理错误地算到平台头上。

沿源码可以建立下面这张地图：

```text
人 / Agent
    │ 网页或 CLI：消息、任务、提及、附件
    ▼
server ─────── 持久协作数据与访问权限
    │ 执行机器连接、输入投递、状态回传
    ▼
daemon / computer
    │ RuntimeDriver 与事件规范化
    ▼
Claude Code / Codex / Pi / 其他运行时
    │ 模型请求、工具调用、原生会话
    ▼
模型服务与本机工具
```

`packages/server` 保存身份、消息、任务等平台状态；`packages/web` 提供界面，`packages/cli` 提供供人和 Agent 调用的命令。执行侧的 `packages/daemon/src/drivers/index.ts` 注册不同运行时驱动，统一导出 `RuntimeDriver`、`RuntimeSession`、`SpawnContext` 等接口。驱动与事件规范化代码把不同运行时的输出转换成平台能处理的事件。

这里应限定“平台不直接管理模型调用”这句话的范围：**Agent 主对话通过运行时驱动接入**，不意味着整个仓库没有直接调用模型服务的代码。例如 server 的翻译 provider 中就有 OpenAI-compatible 实现，部分运行时驱动还采用进程内 SDK。把所有模型请求都画成一个外部 CLI，会掩盖这些差异。

本地化首先要处理的是 server 的依赖。`standalone/config.ts` 将数据库地址指向 `pglite://...`，把上传目录和签名密钥（Signing Key）放入独立状态目录，并清除一批可能继承自云端的数据库、存储及通知配置。数据库入口 `db/index.ts` 选择 PGlite，迁移由 `db/pgliteMigrations.ts` 执行。PGlite 是嵌入式 PostgreSQL 实现，减少了本地启动所需的独立服务。

但不能把所有分布式组件一律换成嵌入式数据库。`standalone/localReplicaStateStore.ts` 将单 server 进程内的连接租约（Lease）、唤醒锁和运行活动状态放进 `Map`，注释明确说明这些状态在退出时重置。持久协作数据由 PGlite 保存，连接状态由进程重建。这个边界适用于单 server 实例，不能据此推导出多副本部署已经不需要共享协调存储。

本地中心和执行机器也需要分别部署。`standalone/remoteComputerRoutes.ts` 检查登记机器的权限，在下载 runner 前校验包名、大小与 SHA-256；`daemon/src/standaloneRemote.ts` 通过 `setup` 进行浏览器设备批准，再由 `start` 复用已保存的机器接入状态。源码要求独立的私有状态目录，拒绝把同一个目录绑定到另一中心；运行时配置留在执行机器上。这个设计让中心与执行端可以分离，但每个 runner 仍需满足 Node 版本和平台架构要求。

这段是源码确认的流程，本轮没有重新做远程登记实验。它也解释了备份为何不能只复制一个数据库目录：平台数据、上传文件、签名密钥、机器接入状态和运行时原生会话承担不同职责。本文尚未完成整套备份恢复验证，不能把这些路径的存在当成恢复能力已经验收。

## 2. 从源码能运行，到能发行，中间还有一条构建链

最初检查构建命令时，容易以为存在 `build` 就有完整服务产物。实际源码给出了两个反例。

根目录的 `standalone:build` 构建 daemon、远程 runner 包和 web，没有生成独立 server。server 自己的 `build` 是 `tsc`，而 `tsconfig.json` 设置了 `noEmit: true`。因此即使这一命令成功，也不能证明存在可执行的 `dist/server.js`。源码入口 `src/standalone.ts` 通过 `@oxc-node/core/register` 启动，依赖 TypeScript 加载器处理导入。

如果直接把入口文件转成 JavaScript，仍会留下几类问题：省略扩展名的相对导入、指向工作区 TypeScript 文件的包导入、以 `new URL(..., import.meta.url)` 定位的 worker，以及相对路径上的数据库迁移和版本信息。转译语法只解决了其中一部分。

为验证这条链路，本轮新增了实验脚本 `scripts/build-standalone-server.mjs`。它从 standalone 入口遍历转译后仍被引用的工作区模块，解析本地依赖，把导入改写为产物之间的相对 JavaScript 路径；静态 TypeScript worker URL 也加入构建图。同时复制数据库迁移和相关包的版本文件。输出目录必须是新目录，避免覆盖正在使用的产物。

实际构建生成了 **709 个模块**。从主入口递归检查的 **706 个 JavaScript 模块**均能解析，相对导入目标均存在；两者数量不同，是因为构建还包含静态 worker URL 引入的模块。使用纯 Node 加载已输出的启动预检和配置模块，也执行通过。

这些结果只能证明转译和部分加载链路成立。脚本使用 `transpileModule`，没有完成全仓库类型检查（Type Checking）；外部 npm 依赖仍用符号链接（Symbolic Link）复用现有安装，网页和 daemon 需另供。产物有明确的 `compiled-workspace` 阶段标记，尚不能搬到另一台机器当作安装包使用。

接着进行真正的启动尝试：在全新状态目录、随机本地端口上，用 `node .../standalone.js` 启动，不传源码加载器。进程在监听前退出，原因是已安装的 `sharp` 原生依赖要求 `GLIBC_2.27`，宿主机只有 glibc 2.17。当前 Node 为 22.18.0，也低于仓库声明的 24。

这次失败把问题从“有没有产物”推进到了“产物依赖什么运行环境”。本轮没有替换系统库、修改共享依赖或重启现有服务。后续必须在符合要求的 Node 和原生依赖环境下重新验证；静态导入检查无法替代这一步。

## 3. 修复启动失败路径，让失败留下可解释的状态

构建过程中先完成了两项较小、但能独立验证的修复。它们的共同点是让失败边界更明确。

第一项发生在启动入口。原代码先执行 `configureStandalone()`，创建状态目录和签名密钥，再检查网页 `index.html`。如果用户只拿到了服务源码、没有网页产物，启动虽然失败，却已经留下了一部分持久状态，而且错误主要表现为文件不存在。

修复后由 `standalone/startup.ts` 先检查网页入口是否为可读文件，再进入状态配置。失败提示同时给出构建网页和设置 `RAFT_STANDALONE_WEB_DIST` 两条路径。测试不仅调用预检函数，还实际启动子进程，指定不存在的网页目录与全新状态路径，确认进程非零退出、提示正确，而且状态目录没有创建。相关配置、预检和网页路由共 **6 项测试通过**。

第二项涉及桌面切换环境时的登录状态清理。原逻辑仅保存数字 `generation`，用它判断缓存是否过期。同一存储空间如果切换了目标环境，却碰巧拿到相同数字，就不会触发清理。

新的比较键包含环境 ID、前端地址、API 地址、socket 地址及 generation：

```ts
const identity = JSON.stringify([
  environment.environmentId,
  environment.frontendOrigin,
  environment.apiOrigin,
  environment.socketOrigin,
  environment.generation,
]);
```

地址改变时旧状态失效，同一目标重复加载则不重复清理。桌面运行环境、握手及相关消费者的 **34 项测试通过**。旧版本只保存数字，因此升级后第一次读取会清理一次，需要重新登录。

这个修复也有适用条件。现有 production 和 staging 通常由不同页面源隔离，测试不能证明它们已经发生过跨环境凭据泄漏。它验证的是共用存储时的切换防护，也没有放宽原先只允许官方环境的校验。自建中心接入仍须同时处理运行时配置、握手契约和原生桌面壳。

## 4. Worker 的运行方式也必须跟随构建产物变化

主入口去掉 TypeScript 加载器以后，后台线程仍可能偷偷依赖它。这个问题出现在 `services/workerPool/workerExecutor.ts`：执行器发现父进程没有 loader，就补上 `--import @oxc-node/core/register`，即使 worker 已经编译成 JavaScript。

对于源码执行，这个行为合理：worker 需要解析 TypeScript 和省略扩展名的导入。对于编译产物，它却引入了不必要的运行依赖。如果只测试主线程入口，这一问题可能一直藏到附件处理等异步任务开始时才出现。

修复将判断绑定到 worker 入口。`.js`、`.mjs` 和 `.cjs` 入口继承父进程参数，不再补源码加载器；`.ts` 入口保留现有逻辑。测试分两层完成：先跑真实 JavaScript worker 和原有 TypeScript worker 的集成测试，再重新构建，在产物目录里用纯 Node 加载编译后的执行器与默认 `workerEntry.js`。

产物中的真实线程完成了 bootstrap 注册、消息往返、处理器抛错、线程崩溃和恢复后再次执行。线程回传的 `process.execArgv` 为 `[]`，说明这次执行没有携带源码加载器。这比只检查输出文件里存在 `.js` 更强，但仍只覆盖 worker 链路，不代表完整 HTTP 服务已经启动成功。

到这里，可以把当前交付状态放在同一张表里，避免后续维护时混淆：

| 验证对象 | 实际结果 | 尚不能推出什么 |
|---|---|---|
| 桌面环境状态清理 | 34 项相关测试通过 | 自建中心和 Mac 桌面已适配 |
| 启动预检、配置、网页路由 | 6 项测试通过 | 整个服务已完成运行验证 |
| 实验 JS 构建 | 输出 709 模块，主入口 706 模块静态检查通过 | 类型检查和可搬运发行包完成 |
| 编译产物 worker | 真实线程执行及崩溃恢复通过 | 全部后台任务都已覆盖 |
| 编译产物 HTTP 启动 | sharp 原生依赖加载失败 | 不能记为运行通过 |

## 5. 多 Agent 优化先从状态归属和协作投递开始

运行环境可恢复以后，才适合讨论多 Agent 的效率。增加执行进程数量，并不能自动减少重复工作；任务归属不清、消息过度投递和过期上下文，反而会放大模型调用与人工核对成本。

源码和手册首先区分 Agent 身份与运行时会话（Session）。Agent 的身份、工作目录、频道成员关系等由平台保存，底下通常维持一个活动运行时会话。重启尝试延续会话，重置则换一个新会话。频道、私信和线程保存的是协作历史，它们不是多个模型会话的简单拼接。

这个区分允许一种恢复策略：模型上下文失效时重建会话，从工作区记忆和平台消息找回任务，而不删除协作事实。但恢复质量取决于工作记录。如果只写“继续之前的工作”，新会话仍需重新猜测代码位置、测试状态和待办事项。

### 用任务认领控制写入，用线程控制讨论范围

本轮实践中，执行者开始时发现工作区已有 **19 个运行时表单相关文件**被修改。这些是其他工作的改动，本轮保留，不能因为由当前执行者看到就变成其所有。后续修复限定在桌面环境、standalone 启动、构建脚本和 worker 相关文件中。

由此得到的协作建议是：任务认领应同时记录产物与写入范围，协作者可以并发读取，但不要让多个 Agent 同时修改同一个共享 Git 状态。拆分工作时，优先按互不重叠的接口或成果拆分；有前后依赖的步骤保留明确交接。并行度的收益来自减少等待，代价是更多接口、版本和结果核对。

线程投递也有类似边界。Raft 将线程可见性与关注者投递分开：能看到一个频道，并不意味着每个 Agent 都需要消费其全部讨论。实际使用时，明确负责者、在相关线程回复、完成后减少不必要的关注，可以降低无关唤醒。这里没有测量 token 节省或吞吐提升，属于基于投递机制的使用建议。

### 上下文需要预算，但预算不等于完整模型历史

`drivers/claudeInputBudget.ts` 对 daemon 负责的启动文本做预算：代码中默认上限为 200,000 tokens，特定模型名匹配路径使用 1,000,000，预留 32,000。估计方法是可见文本的 UTF-8 字节数除以 3 后向上取整。超过预算会在拉起命令前报告 `INPUT_TOO_LARGE`。

这是一项运行时专用的预检，不是模型服务的权威 tokenizer，也不覆盖恢复会话时 Claude 自己载入的历史。不能拿这组参数评价所有运行时的上下文窗口。

记忆注入同样有条件。`startupMemoryBlock.ts` 为新会话注入 `MEMORY.md` 头部，但配置要求服务端 `constructedWakeContext` 显式启用，并允许本地开关关闭；恢复会话跳过注入，以免重复污染已有上下文。注入视图有预算，源文件不会因此被截断。

因此跨会话记忆（Memory）适合保存任务索引、重要决定、证据位置和下一步，不适合不断堆积全部日志。更长的记忆会消耗输入预算，旧的状态还可能诱导下一次会话作出错误判断。可行的优化是让入口文件保持可扫描，把细节放到按主题组织的文件；重新操作前，再验证进程、分支和服务等会变化的事实。平台能提供读取入口，记忆内容是否准确仍需要维护。

## 6. 把优化成果组织成可以复核的交付

这次迭代说明，多 Agent 的可维护性与交付格式密切相关。一个执行者说“构建好了”，另一个执行者可能把它理解成可安装，用户又可能理解成当前服务已经更新。如果没有阶段与路径，这些说法很难在后续会话中纠正。

我更倾向把一次交付写成四个连续问题：修改了哪个版本和哪些文件，生成了什么产物，实际执行了哪些检查，还缺什么证据。失败也落到同一条链上：这次 HTTP 启动失败发生在 sharp 加载阶段，不能归为业务请求失败，也不能用 worker 测试通过把它覆盖掉。

这套做法适合继续扩展为平台能力：任务关联源码快照与构建清单，结果分开记录源码检查、产物、运行和性能；交接时附上可重跑命令与明确的失效条件。它目前是本文提出的优化方向，并非已经全部实现的产品功能。自动化越深入，越需要防止把“缺失”转成“通过”，或让一次成功长期替代当前状态。

对 Mac App，我仍建议先实现桌面控制台连接现有中心，让任务继续在执行机器上运行，再讨论 Mac 完全本地运行。前者优先解决连接、认证和握手，后者还要承担本机依赖、数据目录、后台进程、升级和恢复。是否选择 Electron 或 Tauri，应在这些接口与打包链核对后决定；当前源码中的桌面接口存在，不等于已经有可用的自建中心 Mac 发行版。

后续工作的顺序也因此明确：先用符合项目要求的运行环境打通完整 HTTP 启动，再收齐可搬运的依赖与资产，验证移动目录和数据恢复；桌面接入同步完成自建中心协议适配，最后在 macOS 上验证安装、退出、重启与升级。每一步都应保留自己的证据，不能由前一步的成功代签。

## 7. 回到源码：Raft 怎样把 Agent 组织成一个团队

前面几节讲的是部署和修复，这一节回到我最初被吸引的地方：Raft 的数据模型。以下内容基于公开镜像 [botiverse/raft-source](https://github.com/botiverse/raft-source) 的 `db5e399`（Release v1.21.23-source.1，2026-10-20 07:55 北京时间）。我先用 Codex 只读扫了一遍仓库，整理出结构报告，再逐条用 `rg` 核对路径和符号。下文提到的路径和符号都在该快照中确认存在。这个仓库是 release mirror，按版本整包导出，不接受 PR，许可证是 FSL-1.1-ALv2，属于 source-available（源码可见），不是 OSI 意义上的开源。

**【事实】Agent 是一等成员，不是函数调用。** `packages/server/src/db/schema.ts` 的 `agents` 表保存 Agent 的名称、描述、runtime/model/runtimeConfig、sessionId、所在 machineId 和执行模式。`channelAgents` 与 `channelHumans` 分别记录 Agent 和人的频道成员关系，权限角色分 member/admin。`agents.description` 由 `packages/daemon/src/drivers/systemPrompt.ts` 写进初始提示词，相当于这名成员的岗位说明。频道类型覆盖 channel/private/joint/dm/thread，线程通过 `parentMessageId` 挂在父消息下。人和 Agent 用的是同一套组织结构。

**【事实】协作原语和人的工作流一一对应。**
- 认领与改派：`packages/server/src/services/taskService.ts` 提供 `claimTaskDetailed()`、`claimTask()`、`unclaimTask()`、`assignTask()`，认领时有冲突检查，修改带 revision 并发控制。
- 状态机：同一文件里的状态迁移表包含 `in_progress → in_review → done`，`in_review` 可以退回 `in_progress`。
- 提及投递：`mentionDeliveryOccurrences` 把每次 @ 的投递状态（server 决策、daemon 接收、ack 或终止错误）记成可追踪的记录。
- 线程关注：`threadFollows` 记录谁因为发起、回复、被提及或手动操作而关注了某个线程，也记录完成和取消关注。

我没有找到独立的 handoff API，交接是通过改派和线程回复完成的。

**【事实】异构运行时被收敛到一个接口。** `packages/daemon/src/drivers/types.ts` 定义 `RuntimeDriver`，核心成员包括：
- `spawn()`、`parseLine()`、`encodeStdinMessage()`、`buildSystemPrompt()`，负责拉起进程、解析输出、投递输入和构造提示词；
- `busyDeliveryMode`（`direct`/`notification`/`none`），表示 Agent 正在忙时新消息怎么送达；
- 一组会话恢复和 compaction（上下文压缩）期间的投递能力声明。

`packages/daemon/src/drivers/index.ts` 的 `driverFactories` 注册了 builtin、claude、codex、grok、cursor、gemini、kimi、kimi-sdk、opencode、pi，以及已弃用、仅为老 Agent 保留的 copilot 和 antigravity。每个运行时配有自己的 EventNormalizer，例如 `claudeEventNormalizer.ts`、`codexEventNormalizer.ts`、`piEventNormalizer.ts`，把各家不同的流式输出统一成平台事件。daemon 侧由 `packages/daemon/src/core.ts` 的 `DaemonCore` 和 `agentProcessManager.ts` 的 `AgentProcessManager` 管理进程生命周期。

**【事实】人工把关是局部的，不是全局闸门。** 明确的审批流程在 `packages/server/src/routes/internalAgentApi.ts`：集成登录这类操作会返回 `approval_required` 并生成 action card，由 `actionCardsService.ts` 的 `prepareActionCard()`、`executeActionCard()` 执行。在运行时内部，`codex.ts` 设置了 `approvalPolicy: "never"`，`claudeLaunch.ts` 使用 `--permission-mode bypassPermissions`。也就是说，平台把单个运行时的工具审批关掉，把约束上移到身份、频道权限、凭据代理（README 说明 daemon 通过本地 origin-bound proxy 代发凭据，Agent 不直接持有 server 凭据）和任务的 review 状态上。

【推断】`in_review` 状态并不强制必须由人审核。任何有频道写权限的成员都能推动状态迁移，所以 review 是一种组织约定，而不是系统强制。我认为这个取舍符合它“把 Agent 当同事”的定位，但团队需要自己约定谁有权把任务推到 done。

## 8. 为什么我喜欢这套 agentic 架构

我真正欣赏的是 Raft 选择抽象的层次。它没有再造一个 coding agent，而是把 Claude Code、Codex、Cursor、Gemini、Kimi、OpenCode 这些已经各自成熟、又互不兼容的 vibe coding 工具当成可替换的“劳动力”，在它们上面加一层组织抽象：身份、频道、任务、提及、关注、记忆。

今天的工具生态是割裂的。每家都有自己的会话格式、上下文管理、权限模型和 subagent 机制，换一个工具就意味着重新搬一遍上下文和习惯。Raft 的 `RuntimeDriver` 把这种割裂压缩到 daemon 里的一个驱动文件。上层只关心这名成员是谁、在哪个频道、认领了什么、交付了什么。【推断】这和数据库领域的做法很像：存储引擎可以插拔，优化器和事务语义保持稳定。运行时会快速迭代甚至被淘汰（注册表里已经有两个 deprecated 驱动），组织层的事实却需要长期保存。

另一个打动我的点是它把 Agent 编织进人的组织方式，而不是让人去适应 Agent。人习惯的分工、认领、在线程里讨论、@ 负责人、提交 review、交接时留记录，在 Raft 里都是数据库里的一等实体，人和 Agent 走同一条路径。我在本地实践中感受最深的是第 5 节提到的写入范围冲突：这类问题在人的团队里早有成熟解法（认领、分支、评审），Raft 让我可以直接复用这些经验，而不必为 Agent 发明一套新的管理学。

## 9. 与同形态开源项目的对比

先界定可比对象。多 Agent 领域大致有三类项目：一是 Agent 开发框架（用代码编排 LLM 调用），二是单体 coding agent（一个 Agent 写代码），三是“虚拟团队”模拟。Raft 不完全属于其中任何一类，它更接近“人和异构 Agent 共用的协作工作区”。下表列出各项目截至 2026-10-24 的状态，数据来自 GitHub API 和官方 README/文档。

| 项目 | 形态 | 优势 | 相对 Raft 的短板 / 差异 |
|---|---|---|---|
| [Raft](https://github.com/botiverse/raft-source) | 人与 Agent 共用工作区，编排现成 CLI 运行时 | 复用 Claude Code/Codex 等成熟运行时；持久身份、频道、任务、投递状态都落库；可自建执行机器 | FSL source-available，不收 PR；部署链重（Node 24、Postgres/Redis/对象存储，见 README）；审批多靠约定 |
| [OpenHands](https://github.com/OpenHands/OpenHands) | 开源 coding agent 平台与 SDK（MIT） | 社区最大（约 9.2 万 star）、沙箱执行完善、可自托管 | 核心是自家 agent loop，不以编排第三方 CLI 为中心；组织层（频道、认领）弱 |
| [MetaGPT](https://github.com/FoundationAgents/MetaGPT) | 按 SOP 分角色的“软件公司” | 角色分工和产物模板化，论文影响大 | 偏一次性生成流水线；仓库最近推送在 2026-01，活跃度下降；人很难作为平等成员持续参与 |
| [ChatDev](https://github.com/OpenBMB/ChatDev) | 角色对话链组成的虚拟软件公司（Apache-2.0） | 角色化协作可视化，适合研究 | 研究导向，不面向长期运行的团队工作区 |
| [CrewAI](https://github.com/crewAIInc/crewAI) | Python 多 Agent 框架：roles/tasks/crews/flows（MIT） | 上手快，角色与任务抽象直观 | 需要自己写编排代码；Agent 是进程内对象，不是持久成员；不直接驱动 Claude Code/Codex 这类 CLI |
| [LangGraph](https://github.com/langchain-ai/langgraph) | 有状态图编排（MIT） | 状态机、checkpoint、人类中断点表达力强，适合生产工作流 | 底层原语，组织语义要自己搭；同样以 LLM API 为中心 |
| [AutoGen](https://github.com/microsoft/autogen) → [Microsoft Agent Framework](https://github.com/microsoft/agent-framework) | 对话式多 Agent 框架 | MAF 1.0 主打 A2A、MCP 与多 provider | AutoGen README 已声明正在转入归档，新用户转 MAF；社区分支 [AG2](https://github.com/ag2ai/ag2) 另行维护 |
| [OpenAI Agents SDK](https://github.com/openai/openai-agents-python) | 轻量 SDK：handoffs、guardrails、tracing（MIT） | handoff 语义清晰、简单 | 开发库而非工作区；没有持久组织状态 |
| Claude Code [subagents](https://code.claude.com/docs/en/sub-agents) / [agent teams](https://code.claude.com/docs/en/agent-teams) | 单一运行时内的子 Agent 与团队 | 零部署、与 Claude Code 深度集成 | 绑定单一厂商运行时；团队状态随会话存在，不是跨工具的长期组织 |
| [Goose](https://github.com/aaif-goose/goose) | 本地通用 Agent，MCP 扩展（Apache-2.0） | 本地优先，扩展生态好 | 单 Agent 为主，多 Agent 组织不是核心 |

我的判断【推断】：
1. 编排层的竞争正在从“谁的 agent loop 更聪明”转向“谁能管住多个 agent loop”。CrewAI、LangGraph、MAF 解决的是开发者如何用代码编排 LLM；Raft 解决的是一个团队如何长期和一群异构 Agent 共事。后者的护城河在组织数据和投递语义，不在模型调用。
2. Raft 最大的风险不在技术，在于许可和部署门槛。FSL 加上不收 PR，意味着个人只能 fork 自用，改进很难回流；依赖栈重，个人试用成本高。这正是我做 standalone 的动机。
3. 厂商自带的 subagent/team 会把轻量场景吃掉。Raft 的价值要体现在跨运行时、跨机器、需要长期留痕的协作上。
4. 如果只需要一条确定的工作流，LangGraph 更合适；如果要研究角色化生成，MetaGPT/ChatDev 足够；如果想让 Claude Code 和 Codex 像两名同事一样在同一个频道里认领任务、互相 review，目前我只在 Raft 里看到了比较完整的建模。

## 10. 我的改造：面向个人的 standalone 模式

改造思路很简单：个人场景不需要多租户云服务，但需要保留 Raft 的组织抽象。公开镜像的 `./raftdev start` 要拉起 Postgres、Redis、对象存储、server、web、daemon（见 README），对一个人、一台机器来说太重。需要说明的是，公开镜像 `db5e399` 中没有 `packages/server/src/standalone*` 目录，前文引用的 standalone 相关文件都在我的本地工作区，本文不给出它们的永久链接。

改造内容仅限前文已经记录的部分，这里按“个人使用需要什么”重新归纳：

- **一个进程、一个状态目录**：`standalone/config.ts` 把数据库指向 `pglite://`，上传目录和签名密钥放入独立状态目录，清掉继承自云端的配置（§1）。上游 `db/index.ts` 本身就支持 PGlite 分支，改造是在复用这条路径，而不是另起炉灶。
- **单进程协调状态不落库**：`localReplicaStateStore.ts` 把租约、唤醒锁、运行活动放在内存 `Map` 中，重启即重建（§1）。这是“单实例”前提下的简化，不适用于多副本部署。
- **中心与执行机器可分离**：`remoteComputerRoutes.ts` 和 `standaloneRemote.ts` 支持在另一台机器上通过设备批准接入 runner，并校验包的大小与 SHA-256（§1）。个人场景下，可以在家里服务器上放中心，在工作机或 GPU 机上跑 Agent。
- **可交付的构建链与失败边界**：启动预检先于状态创建、桌面环境身份键、worker 按入口类型决定是否加载源码 loader、实验性 JS 构建脚本（§2–§4）。

【推断】个人场景真正需要的是三件事：低启动成本、数据在自己手里、能同时调度手头已经付费的多个 coding 工具。上面的改造分别对应这三点。目前离“开箱即用”还差完整 HTTP 启动（受 sharp/glibc 和 Node 版本阻塞）、备份恢复验证和 Mac 桌面接入（§2、§6），这些仍在待办里，不能算已经交付。

## 11. 后续：多接触这类 agentic 工具

这次折腾让我确认了一件事：未来几年，工程师的一部分工作会从“写代码”变成“组织一群会写代码的 Agent”，而组织能力本身也需要工程化，包括状态归属、投递语义、证据链和可恢复性。这些恰好是我做数据库内核多年反复处理的问题：事务边界、并发控制、日志和恢复。

接下来我想做几件事：
- 在合规的运行环境里把 standalone 的 HTTP 链路彻底跑通，并做一次完整的备份恢复演练；
- 让 Claude Code 和 Codex 在同一个频道里分工，一个实现、一个 review，用真实的数据库项目（例如我自己的引擎实验仓库）观察交接和返工成本；
- 继续跟踪 OpenHands、LangGraph、Microsoft Agent Framework 和厂商 agent teams 的演进，看组织层的抽象最后会收敛到平台、协议（MCP/A2A）还是运行时本身。

我很期待这种形态继续成熟。工具会换代，人和 Agent 一起工作的组织方式，值得认真设计。

## 源码与验证入口

公开项目入口：[Raft](https://github.com/botiverse/raft-source)。本次阅读快照为 `849d036f0e26e06dd518c72f988119450b0a87b0`；新增修复仍是本地工作区内容，因此下表给出定位路径，不伪造尚未发布改动的永久链接。

| 关注点 | 主要源码路径 |
|---|---|
| 运行时驱动 | `packages/daemon/src/drivers/index.ts`、`types.ts` |
| 启动输入预算 | `packages/daemon/src/drivers/claudeInputBudget.ts` |
| 新会话记忆注入与开关 | `packages/daemon/src/startupMemoryBlock.ts` |
| 本地状态与环境隔离 | `packages/server/src/standalone/config.ts` |
| PGlite 与迁移 | `packages/server/src/db/index.ts`、`pgliteMigrations.ts` |
| 单进程连接协调 | `packages/server/src/standalone/localReplicaStateStore.ts` |
| 本轮启动预检 | `packages/server/src/standalone.ts`、`standalone/startup.ts` |
| 桌面环境与缓存身份 | `packages/web/src/desktopRuntimeEnvironment.ts` |
| 实验构建 | `scripts/build-standalone-server.mjs` |
| 任务认领与状态机 | `packages/server/src/services/taskService.ts` |
| 协作数据模型 | `packages/server/src/db/schema.ts`（`agents`、`channelAgents`、`threadFollows`、`mentionDeliveryOccurrences`） |
| Worker 执行 | `packages/server/src/services/workerPool/workerExecutor.ts`、`workerEntry.ts` |

复查时应固定同一源码快照、输入和运行环境。文章中的模块数与测试数对应本次实验，不能作为未来版本的默认保证。
