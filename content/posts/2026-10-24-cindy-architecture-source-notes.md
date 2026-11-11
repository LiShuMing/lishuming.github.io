---
title: "【源码】Cindy：从输入事务到多引擎 Agent 的客户端架构"
slug: "cindy-architecture-source-notes"
date: 2026-10-24T00:00:00+08:00
lastmod: 2026-10-24T00:00:00+08:00
categories:
  - AI Engineering
tags:
  - Cindy
  - Electron
  - Agent
  - TypeScript
  - Source Code
description: "沿一次任务的创建、发送、模型选路与恢复，解析 Cindy 的 Electron 进程边界、Maker/Session、Codex/Pi 适配、SQLite 状态及远程控制，并解释本地定制应落在哪些层。"
draft: false
toc: true
math: false
---

Cindy 把多个 Agent 引擎、模型供应商、项目文件、工具和任务历史放进同一个客户端。理解它的关键，是找出每种状态由谁持有，以及一条输入在哪个时刻真正交给底层引擎。界面上的模型名称、数据库里的任务状态、原生 CLI 的会话编号，分别回答不同的问题；混用这些概念，会同时影响模型接入、失败恢复和功能裁剪。

这次阅读源码的实际起点，是制作一个保留原版界面的本地定制构建：留下 Codex、Pi 和公司模型接口，将不需要的云服务及后台集成默认关闭。最初的独立精简界面没有保留足够的原版体验，后续改造转向原生产入口。具体补丁、鉴权故障和运行核验放在[下一篇实践记录]({{< ref "2026-10-24-cindy-company-local-build-notes.md" >}})；本篇先说明这些改动为什么应落在对应的架构层。

分析基线是本地 checkout 的上游提交 `b1d73b1f79ae812d0950c632db4785b1f06d48e7`，个人改动截至 `61db88572f`，日期为 2026-10-24。上游源码链接固定到基线提交；个人提交仅保存在本地，不能假定已经进入上游。本文主要深入 Desktop 的生产调用链，Mobile、SSH、插件与协同只解释客户端边界；独立服务端仓库不在此次分析范围内。[项目仓库](https://github.com/makecindy/cindy)也明确把自身界定为 desktop、mobile 和共享 packages 的客户端仓。

## 第一层边界：界面表达意图，Main 持有特权

Cindy 使用 pnpm 工作区组织客户端代码。`apps/desktop` 是 Electron 桌面端，`apps/mobile` 是 Expo / React Native 手机端，`packages/` 提供编排、模型目录、调度、远程协议和工具等共享能力。Desktop 内部又分成 Main、preload、Renderer 和 shared。这些目录不是同一种业务代码的随意拆分，而是对权限和状态归属的约束。

![Cindy 客户端的进程、编排、引擎和存储边界](/assets/images/cindy-source-notes/architecture.svg)

*箭头表示主要依赖和请求方向；返回的 Agent 事件会沿宿主桥重新投影到界面。右下方模型服务在客户端进程之外，本地运行 Agent 不代表模型推理离线。*

Renderer 负责 React 组件、表单、消息展示、模型选择和可重建缓存。它经由 preload 暴露的窄接口，通过进程间通信（Inter-Process Communication，IPC）向 Main 提交操作。Main 负责窗口、进程、文件、SQLite、凭证、受控网络以及授权裁决；shared 则保存跨进程类型、常量和纯判据。可复用领域逻辑进入 packages，通过宿主注入与 Electron 解耦。

从 [`src/main/index.ts`](https://github.com/makecindy/cindy/blob/b1d73b1f79ae812d0950c632db4785b1f06d48e7/apps/desktop/src/main/index.ts) 进入，可以看到 profile、日志、环境清理和启动编排；后续的 [`bootstrap-electron.ts`](https://github.com/makecindy/cindy/blob/b1d73b1f79ae812d0950c632db4785b1f06d48e7/apps/desktop/src/main/bootstrap-electron.ts) 完成大量窗口与服务接线。React 入口在 `renderer/main-entry.tsx`，跨进程接口集中于 `preload/preload.ts`。读一个功能时，应沿接口反查 Main 的注册和实际副作用，不能停在组件是否渲染。

这也是“隐藏设置项”不能等价于“禁用功能”的原因。界面不再展示某个页签，不会自动阻止 Main 的启动定时器、系统事件回调或者原生助手。反过来，仅删除 Main 的 handler，也可能让原版 Renderer 在每次挂载时反复报错。保留原界面的裁剪需要同时处理能力可见性、服务启动和关闭态读取。

[`trustedAppRenderer.ts`](https://github.com/makecindy/cindy/blob/b1d73b1f79ae812d0950c632db4785b1f06d48e7/apps/desktop/src/main/security/trustedAppRenderer.ts) 进一步说明了信任边界。特权 IPC 的来源判断取自 Electron 持有的 `sender`、`senderFrame`、顶层页面位置和应用窗口登记，不接受 Renderer 自报“我是可信窗口”。这道检查解决来源问题，具体操作仍须校验参数、路径、归属和权限。模型返回的文字、内嵌网页和插件页面都不能凭一段前端调用获得宿主权力。

## 一条消息跨过的是输入事务

用户点击发送之后，真正困难的部分是协调排队、可见消息、持久化和底层派发。请求可能在任务正在运行时到达，也可能要求插话、停止后继续、重试或恢复。让 Renderer 同时决定这几份状态，会产生“气泡已经出现，实际没有发出”或“重试时把同一条输入再落库”的问题。

[`AgentInputCoordinator`](https://github.com/makecindy/cindy/blob/b1d73b1f79ae812d0950c632db4785b1f06d48e7/apps/desktop/src/main/maker-ipc/agent-input-coordinator.ts) 把这件事作为 Main 侧的输入事务（Input Transaction）处理。Renderer 准备可序列化的文本、附件和引用；宿主负责排序、派发、回滚与持久化。`maker-ipc/register.ts` 的 `sendToSessionInternal` 是宿主发送的重要接线入口，不能把它误认为所有步骤都在其中同步完成的一条直线。

把普通发送路径压缩后，可以得到下面的关系：

```text
ChatInput 提交输入意图
  → preload / maker IPC
  → AgentInputCoordinator 接纳、排队并协调回滚
  → maker-host 解析任务、执行位置、模型和权限
  → maker-core Session.send
  → AgentSessionHandle.send
  → 底层引擎运行，产生事件
  → 宿主事件处理、持久化与 Renderer 投影
```

这里有一个需要精确保留的时序：产品层的 `onAccepted` 和底层已经接受输入，不是同一个瞬间。在 [`session.ts`](https://github.com/makecindy/cindy/blob/b1d73b1f79ae812d0950c632db4785b1f06d48e7/packages/maker-core/src/session.ts) 中，`Session.send` 进入 `dispatchSend`，经过轮次预留、授权和宿主钩子后调用 `handle.send`；其成功返回才构成对应的派发接受边界。失败恢复必须知道错误发生在这道边界之前还是之后，否则可能重复发送。

几类操作因此有不同的状态语义：

| 操作 | 宿主需要维持的约束 |
| --- | --- |
| 普通发送 | 队首先被 drain；派发前失败可以回滚，不能只凭气泡认定执行成功 |
| 插话 | 与当前轮次协调；底层拒绝时保留或恢复待发状态 |
| 停止 | 保留并暂停待发输入，在 abort 边界后释放相应调度 |
| 重试 | 根据错误阶段恢复，不能无条件复制原文本再次发送 |
| 编辑或移除待发输入 | 只操作仍未被接受的条目，避免改动已经执行的历史 |

这些约束会被普通任务、伙伴、定时任务和消息渠道共同使用。新增一个入口时，复用协调器能够保留已有的接受、回滚和恢复语义；直接绕到某个 CLI 的 `send`，就需要重新承担这些一致性成本。

## Maker、Session 和引擎适配分别持有什么

[`Maker`](https://github.com/makecindy/cindy/blob/b1d73b1f79ae812d0950c632db4785b1f06d48e7/packages/maker-core/src/maker.ts) 是 Agent 注册中心和 Session 编排入口。它按 `AgentKind` 查找实现，结合工作目录、执行位置与启动选项创建会话，协调宿主准备、底层启动、元数据保存和失败清理。其依赖包含存储、日志、认证及生命周期钩子；Electron 相关行为由 Desktop 宿主装配。

[`BaseAgent`](https://github.com/makecindy/cindy/blob/b1d73b1f79ae812d0950c632db4785b1f06d48e7/packages/maker-core/src/agents/base-agent.ts) 定义统一适配面，具体引擎提供会话 handle、事件流和能力声明。统一接口不意味着能力完全相同：不支持的操作需要明确拒绝，调用方根据 capabilities 决定是否展示或调用。`packages/maker-core/src/agents/` 下的 Claude Code、Codex、Pi 实现封装各自的原生运行方式，Desktop 的 `main/maker-host/` 再注入二进制路径、环境、认证、工具桥及存储。

`Session` 持有一次运行时会话的 handle、轮次状态、权限状态和事件消费。Maker 创建它之后，宿主订阅事件，将流式文本、错误、结束和会话编号变更接入产品层。不能把 `Maker.createSession` 的成功、第一条模型回复到达，以及整轮任务完成视为同一个成功条件。

这条链还有启动回滚：引擎启动成功之后，存储步骤仍可能失败。Maker 的创建路径会尝试关闭已经启动的 handle，并通知失败钩子；若底层清理没有确认完成，宿主仍需保持相应的运行保护。只看数据库是否插入成功，可能漏掉遗留子进程。

任务 ID 与原生引擎 ID 也应分别看待。产品任务使用 Cindy 自己的 ID，底层 Codex thread 或 Pi session 有自己的恢复标识，后者通过 `sdkSessionId` 等字段建立关联。切换引擎、恢复原生记录和删除产品任务，涉及不同的生命周期，不能只改一个编号便声称完成迁移。

## 模型、协议、凭证和引擎是四个维度

Cindy 的模型选择需要至少回答四个问题：使用哪个连接、哪个上游型号、哪种传输协议，以及由哪个 Agent 引擎执行。一个模型名称不能替代这组信息。Codex 可以经兼容桥调用 Anthropic Messages 接口上的其他模型，Pi 也可以使用自己的原生协议适配；界面显示 Codex，不意味着上游一定是 OpenAI。

模型资料的共享层在 [`packages/model-providers`](https://github.com/makecindy/cindy/tree/b1d73b1f79ae812d0950c632db4785b1f06d48e7/packages/model-providers)，Desktop 活动目录在 `maker-host/active-catalog.ts`，最终路由集中于 [`provider-route.ts`](https://github.com/makecindy/cindy/blob/b1d73b1f79ae812d0950c632db4785b1f06d48e7/apps/desktop/src/main/maker-host/provider-route.ts)。目录把公共型号资料、供应商路由、引擎差异、实报和用户覆盖组合起来；服务端目录与客户端离线副本的关系，见基线的 `docs/dev-rules/model-catalog-maintenance.md`。

目录中的“可显示”、连接的“已认证”、路由的“可执行”和真实请求的“已成功”是不同证据。添加模型条目，只能说明客户端有了描述；接口、鉴权头、模型 wire ID 或协议字段仍可能不匹配。后续实践中出现的“列表完全为空”和“有模型但请求 401”，分别发生在目录初始化和实际鉴权阶段，修复位置也不同。

Codex 的 Desktop 代理生命周期由 [`codex-proxy-host.ts`](https://github.com/makecindy/cindy/blob/b1d73b1f79ae812d0950c632db4785b1f06d48e7/apps/desktop/src/main/maker-host/codex-proxy-host.ts) 管理。本地回环（Loopback）代理承担路由、请求适配和宿主策略；`responses-anthropic-bridge` 则提供 Responses 到 Anthropic Messages 的转换。桥接只是协议和策略执行路径，模型推理仍在选中的上游完成。

Pi 的宿主装配位于 [`pi-host.ts`](https://github.com/makecindy/cindy/blob/b1d73b1f79ae812d0950c632db4785b1f06d48e7/apps/desktop/src/main/maker-host/pi-host.ts)。需要根据供应商分支理解它：订阅来源可以经宿主代理注入凭证，自定义 API key 来源则有原生供应商配置和环境变量注入路径。文件头对某个阶段或订阅路径的说明，不能代替函数内部对所有来源的实际分支。接入自己的模型时，应该检查 `buildPiNativeProvidersFromConfigs` 怎样生成 API、base URL、模型列表和密钥引用。

## 存储负责恢复事实，界面状态负责展示

Desktop 的 `main/localDb/` 使用 SQLite 和 Drizzle 组织产品数据；[`DesktopSessionStorage`](https://github.com/makecindy/cindy/blob/b1d73b1f79ae812d0950c632db4785b1f06d48e7/apps/desktop/src/main/maker-host/session-storage.ts) 把核心会话接口映射到数据库。任务元数据、消息、模型、执行位置和原生会话编号等信息，因而能被宿主与界面共同使用。

这里最容易误读的是 `sessions.status`。存储映射的注释明确把它界定为产品生命周期的 `active / archived / deleted`，而不是当前轮次的“运行中 / 已完成”。因此，运行核验不能因为一行仍是 `active` 就认定 Agent 还在执行。要结合轮次开始与结束标记、存活进程和界面事件判断；同样，留下开始标记但对应进程已死，也可能是中断任务。

原生引擎还维护自己的历史或转录，产品 SQLite 并不是所有运行数据的唯一物理文件。记忆、Skill、附件、凭证和插件持久数据也有不同的作用域与存储合同。分析其中一个 FTS 查询入口，不能据此推断整个客户端只有一种检索方式；查看一个 Pi transcript，也不能认为它包含所有 Cindy 产品状态。

重启恢复由这些事实共同驱动。`sessionActiveTurn.ts` 判断启动时的中断候选，`interruptedResumeCandidates.ts` 补充展示信息，运行期恢复则由 `maker-ipc/interruptedTurnAutoResume.ts` 分类，再回到输入协调器。启动时发现中断和运行中自动续跑是两条不同的路径。保留原版 SQLite 与任务入口，正是本次定制没有重新实现这些状态机的主要收益。

## 外围能力通过宿主接线扩展基础链路

在理解发送和恢复后，再看插件、手机与协同，会更容易判断它们的位置。下面的表是代码导航，不表示所有能力都已在本次环境中联调。

| 能力 | 客户端入口 | 与基础任务链的关系 |
| --- | --- | --- |
| MCP、文件、浏览器和电脑操作 | `main/mcp-integrations/`、`packages/lizi-mcps`、`browser-control-runtime` | 宿主装配工具与权限，Agent 在任务中按需调用 |
| 插件发现与运行 | `main/plugin-market/`、`packages/cindy-tools` 及 Ghost 相关模块 | 市场发现、已装插件运行与批准记录是不同层，关闭市场不能顺手破坏存量数据 |
| 伙伴与 Orca 协同 | Desktop 伙伴宿主、`packages/orca-workflow` | 增加身份、调度和多 worker 行为，不能把伙伴输入直接视为用户授权 |
| 定时任务 | `packages/maker-scheduler` 与 Desktop runner | 调度入口复用执行链，保留来源和恢复语义 |
| 手机控制桌面 | `apps/mobile`、`packages/device-link`、`main/device-link/` | 手机持有控制和镜像状态，任务在被选中的执行电脑上运行 |
| SSH 执行 | `maker-remote-ssh`、`maker-cc-manager`、`maker-pi-manager` | 远端进程和工作目录带来独立的断链、恢复与凭证边界 |
| 语音、外设、菜单栏和灵动岛 | 对应 Main 服务与 native helper | 通常有独立启动、预热、系统事件和清理路径，不能只审查任务工具入口 |

原仓有多类“远程”，也有多类“后台”。手机远控一台桌面、通过 SSH 在服务器运行 CLI、上游模型在网络服务推理，分别跨越不同的边界。本地 profile 可以保留网络模型；关闭设备互联可以保留任务内按需操作本机电脑；关闭插件市场也不必删除用户已批准的插件。这些取舍必须落在具体能力上。

## 从架构推导定制的最小改动面

在上述约束下，保留原版基础逻辑的定制可以集中在三个位置。第一，在启动配置与能力判据中选择要注册的 Agent、服务和 UI 入口。第二，在既有模型目录及路由中加入公司供应商，复用协议转换和凭证读取。第三，在设置存储中保留默认值与用户覆盖的区别，让 Main、preload、Renderer 使用一致的启动快照。

任务协调器、Session 派发、消息落库和恢复逻辑则继续复用。这会保留原系统的复杂性，也要求审查所有可能唤起服务的旁路：窗口显示、睡眠恢复、启动补传、原生助手预热、模块初始化与 IPC 查询。下一篇记录中的 WorkLouder 和日志补传遗漏，正说明“主初始化函数有判断”不足以覆盖完整生命周期。

要继续深入阅读，可以按下面的顺序复核源码：

1. `main/index.ts → bootstrap-electron.ts`：确认 profile、生效配置与服务接线时机。
2. `renderer/components/new-chat/ChatInput.tsx → preload/preload.ts → maker-ipc/register.ts`：跟踪一次发送意图。
3. `agent-input-coordinator.ts → maker-core/src/maker.ts → session.ts → agents/base-agent.ts`：识别事务、派发和运行时边界。
4. `active-catalog.ts → provider-route.ts → codex-proxy-host.ts / pi-host.ts`：区分目录、协议和真实凭证路径。
5. `session-storage.ts → localDb/sessionActiveTurn.ts`：用持久化事实理解恢复，而不是只看界面状态。

这套阅读顺序适用于修改本地执行、模型接入或后台服务策略。它不构成对独立服务端、所有插件权限、全部远程组合和完整电脑控制任务的验收；这些路径仍需要按各自的协议、身份与失败条件单独验证。
