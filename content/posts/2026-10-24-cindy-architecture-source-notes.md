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
description: "从桌面多引擎 Agent 客户端的四个核心矛盾出发，拆解 Cindy 如何隔离界面意图与宿主特权、用输入事务消解并发发送与中断竞态、解耦模型与协议路由，并在重启后恢复未完成任务。"
draft: false
toc: true
math: false
---

如果只做一个最简单的桌面 Agent 套壳工具，两百行代码就够了：用 Electron 画一个聊天框，用户按下回车时启动底层的命令行进程，把标准输出流式渲染成气泡，顺手存进本地数据库。

但只要把它放进真实的日常研发里，这个朴素模型马上就会在四个地方出问题：

1. **特权边界失守**：Agent 会读取外部网页、第三方仓库和不可信的模型输出。如果前端渲染层直接持有文件系统、子进程和 API 密钥权限，任何前端注入都可能演变成宿主机命令执行。
2. **发送与中断竞态**：当底层 Agent 正在改代码时，用户可能连续追问、插话（Steer）、点击停止，或者进程恰好在发送瞬间崩溃。如果界面刚点发送就落库，会出现“气泡还在、底层根本没收到”，或者重试时把同一句话发两遍。
3. **“选模型”不再是一个名字**：当客户端同时容纳 Claude Code、Codex、Pi 等多种引擎，又允许接入自建网关时，“用 Codex 跑公司网关上的 DeepSeek”其实横跨了执行引擎、传输协议、凭证注入位置和上游型号四个独立维度。
4. **跨进程状态分裂**：界面上的会话卡片、SQLite 里的产品记录，以及 Codex/Pi 各自维护的原生会话文件（Transcript），生命周期并不一致；合上电脑再打开时，谁说了算？

[Cindy](https://github.com/makecindy/cindy) 把多 Agent 引擎、模型目录、工具审批、本地任务历史和远程控制整合在同一个客户端里，它的架构几乎就是围绕上述四个矛盾长出来的。这次读源码的起因很实际：我想基于原版界面做一个本地定制构建——只保留 Codex、Pi 和公司内部的模型接口，同时把云账号、插件市场、语音和外设等后台集成默认关掉。最初我试过另起炉灶写一套极简界面，很快发现排队、审批、中断恢复这些脏活全得重做一遍，于是退回原版主干做减法。具体的改造过程和排障细节记在[下一篇实践笔记]({{< ref "2026-10-24-cindy-company-local-build-notes.md" >}})里，本篇先沿着一条消息的生命周期，把这套客户端的骨架讲清楚。

---

## 一、特权边界：界面只表达“我想做什么”，Main 决定“能不能做”

在 Electron 应用里，目录划分本质上是权限划分。Cindy 使用 pnpm Workspace 管理代码：`apps/desktop` 是桌面端，`apps/mobile` 是手机端，`packages/` 存放与宿主解耦的编排、模型目录、调度和远程协议。单看 `apps/desktop` 内部，代码被严格切成了四块：**Renderer**、**preload**、**Main** 和 **shared**。

![Cindy 客户端的进程、编排、引擎和存储边界](/assets/images/cindy-source-notes/architecture.svg)

*箭头表示主要依赖和请求方向；Agent 产生的事件流会沿宿主桥反向推回界面。右下方的模型推理运行在远端或外部服务上——“本地运行 Agent”指的是本地宿主掌控工具与会话，不代表大模型离线推理。*

为什么要切得这么碎？因为聊天窗口里展示的内容天然不可信。模型返回的一段 Markdown、预览窗口打开的网页，或者某个插件渲染的视图，都活在 Renderer（浏览器渲染进程）里。因此 Cindy 把所有带副作用的资源——窗口管理、子进程拉起、本地文件读写、SQLite 数据库、密钥读取和网络代理——全部收归 **Main（主进程）** 持有。Renderer 只能管 React 视图、表单输入和随时可丢弃的内存缓存；它想做任何越界操作，都必须穿过 `preload/preload.ts` 暴露的窄接口，通过进程间通信（Inter-Process Communication，IPC）向 Main 发起请求。

翻开 [`trustedAppRenderer.ts`](https://github.com/makecindy/cindy/blob/b1d73b1f79ae812d0950c632db4785b1f06d48e7/apps/desktop/src/main/security/trustedAppRenderer.ts) 就能看到这道防线是怎么落地的：当一条特权 IPC 消息到达 Main 时，宿主绝不轻信前端传来的任何“我是主窗口”参数，而是直接核查 Electron 底层持有的 `sender`、`senderFrame`、顶层 URL 和已登记的应用窗口表。只有验明正身之后，具体业务才会继续检查路径、归属权和审批状态。这样一来，哪怕内嵌网页或模型输出试图伪造前端调用，也拿不到宿主的系统权限。

理解了“前端只发意图、Main 持有实体”，就能明白启动链路和“功能开关”该怎么看。应用从 [`src/main/index.ts`](https://github.com/makecindy/cindy/blob/b1d73b1f79ae812d0950c632db4785b1f06d48e7/apps/desktop/src/main/index.ts) 起步，先敲定运行配置（Profile）、日志和环境清理，随后交给 [`bootstrap-electron.ts`](https://github.com/makecindy/cindy/blob/b1d73b1f79ae812d0950c632db4785b1f06d48e7/apps/desktop/src/main/bootstrap-electron.ts) 把窗口与各类后台服务逐一接好。

这也给本地裁剪提了个醒：**在 React 里把某个设置页藏起来，根本不等于关掉了这项功能。** 只要 Main 里的定时器、系统睡眠唤醒回调或原生硬件助手还在照常初始化，后台行为就一点没少；反过来，如果只粗暴删掉 Main 里的 IPC 处理函数，原版前端组件每次挂载读取状态时就会疯狂报错。想在不破坏原版界面的前提下做裁剪，必须让 Main 停止后台服务、让设置项处于关闭态，同时保留安全的空状态返回——这三步缺一不可。

---

## 二、输入事务：为什么点下“发送”不能直接调 CLI？

假设用户正在让 Agent 重构一个模块，看了一半觉得方向偏了，立刻敲入一句补充说明并回车；或者干脆点了“停止”，改两个字重发。如果前端点下发送的瞬间就直接往数据库写死一条用户消息、同时往子进程管道里塞文本，一旦撞上上一轮尚未收尾、权限弹窗被拒或者进程异常退出，界面看到的对话历史就会和底层引擎实际收到的上下文彻底错位。

为了解决这个异步竞态，Cindy 在 Main 侧引入了 [`AgentInputCoordinator`](https://github.com/makecindy/cindy/blob/b1d73b1f79ae812d0950c632db4785b1f06d48e7/apps/desktop/src/main/maker-ipc/agent-input-coordinator.ts)，把“发送消息”变成了一次受控的**输入事务（Input Transaction）**。

把整条发送主链与异常分支放到一起看，`AgentSessionHandle.send` 刚好将一次提交切成了两段语义截然不同的区间：

![Cindy 两阶段输入事务与派发接受边界](/images/diagrams/app-cindy-input-transaction.svg)

*在跨过 `AgentSessionHandle.send` 之前发生异常，协调器直接回滚队首并原样恢复输入框与附件；跨过边界后再遇中断，则转入会话内运行态异常轮次，避免草稿丢失或静默重发（可打开[交互式流向图](/diagrams/app-cindy-input-transaction.html)切换正常提交、边界前回滚与边界后异常三条路径）。*

阅读 [`session.ts`](https://github.com/makecindy/cindy/blob/b1d73b1f79ae812d0950c632db4785b1f06d48e7/packages/maker-core/src/session.ts) 和 `maker-ipc/register.ts` 里的 `sendToSessionInternal` 时，最值得留心的是图中的**派发接受边界**。产品层收到请求（`onAccepted`）与底层引擎真正接手这轮输入，并不是同一个时刻。只有当 `Session.send` 走完 `dispatchSend`、完成轮次预留与授权检查，最终调用底层 `handle.send` 成功返回后，这条消息才算真正跨过了边界。

一旦画清这条线，各种复杂的交互就有了干净的账本规则：

| 用户动作 | 跨过接受边界前（仍在协调队列） | 跨过接受边界后（底层引擎已接手） |
| --- | --- | --- |
| **普通发送** | 按序排队；若环境准备或鉴权失败，直接回滚队首，不留脏历史 | 进入正式轮次，等待引擎流式事件驱动状态更新 |
| **运行中插话** | 暂存为待发条目；若底层引擎拒绝插话，原样退回待发区 | 交由引擎与当前轮次合并处理 |
| **点击停止** | 暂停队列继续下发，保留用户已敲好的待发草稿 | 向引擎发送中止信号（Abort），等边界收尾后释放调度锁 |
| **编辑 / 撤回** | 允许直接修改或移除尚未下发的排队条目 | 已进入引擎上下文的历史不再篡改 |
| **失败重试** | 无需复制消息，恢复排队状态即可 | 需结合失败阶段决定续跑还是开启新轮次，防止重复执行副作用 |

更妙的是，这套协调器不仅服务于主聊天框，定时任务、自动化伙伴（Partner）和外部消息渠道也共用同一入口。不管输入从哪里来，只要走协调器，排队、回滚和中断语义就是一致的。

---

## 三、会话编排：Maker、Session 与双层会话 ID

跨过输入协调器往下走，就进入了与 Electron 解耦的编排核心 `packages/maker-core`。这里有三个核心角色各司其职：

- **[`Maker`](https://github.com/makecindy/cindy/blob/b1d73b1f79ae812d0950c632db4785b1f06d48e7/packages/maker-core/src/maker.ts)（总调度室）**：按引擎类型（`AgentKind`）管理注册表。每当要启动新会话时，它结合工作目录、本地或远端位置以及启动参数创建实例，串联起宿主环境准备、底层进程拉起、元数据落库和异常清理。
- **[`BaseAgent`](https://github.com/makecindy/cindy/blob/b1d73b1f79ae812d0950c632db4785b1f06d48e7/packages/maker-core/src/agents/base-agent.ts)（引擎适配器）**：为 Claude Code、Codex、Pi 定义统一的会话句柄（Handle）和事件流接口，同时通过能力声明（Capabilities）诚实暴露差异——比如某个引擎不支持某项热切换，上层就据此置灰入口，而不是硬调报错。Desktop 侧的 `main/maker-host/` 则通过依赖注入，把具体的二进制路径、环境变量、鉴权桥和 MCP 工具装配进去。
- **`Session`（单次运行管家）**：持有当前活跃句柄、轮次状态机和权限上下文，把底层吐出的增量文本、工具调用请求和报错翻译成统一的产品层事件。

这里藏着两处极易踩坑的工程细节。

第一处是**启动半途失败的孤儿进程清理**。设想 `Maker.createSession` 已经成功拉起了底层的 Codex 子进程，但紧接着写本地数据库时报错了。如果只盯着数据库事务回滚，后台就会留下一个没人管的孤儿进程。因此 `Maker` 在创建链路里专门做了逆向补偿：一旦后续装配失败，立即关闭已启动的句柄并触发失败钩子；如果底层进程未能确认退出，宿主还会维持运行态保护，防止状态失联。

第二处是**双层 ID 映射**。Cindy 侧边栏里的每个任务有自己的产品级 ID，而底层运行的 Codex Thread 或 Pi Session 又有各自原生的会话标识，两者通过 `sdkSessionId` 等字段关联。为什么要分开？因为用户可能在一个产品任务里查看归档、恢复原生上下文，或者删除本地记录；把产品生命周期和底层 CLI 的会话文件解耦后，各自演进才不会互相绑死。

---

## 四、模型选路：把“一个模型名字”拆回四个正交维度

在单模型客户端里，下拉框选中一个名字，直接发请求就完了。但在多引擎客户端里，界面上选一个模型，背后其实要同时对齐四件事：

1. **走哪条连接与凭证**（官方订阅登录、企业网关，还是用户自填的 API Key）？
2. **上游推理型号叫什么**（例如网关侧真实的 `bailian/deepseek-v4.1-flash`）？
3. **走什么传输协议**（OpenAI Responses 协议，还是 Anthropic Messages 协议）？
4. **由哪个 Agent 引擎驱动工具循环**（Codex 还是 Pi）？

举个直观的例子：界面上选了 Codex 引擎，但配置的是公司内部的 Anthropic Messages 兼容网关。这时“界面显示 Codex”只决定了本地由谁来跑读写文件和终端命令的循环，真正的推理请求完全可以发往非 OpenAI 的大模型。

Cindy 把这套组合逻辑放在了三层代码里：跨端共享的模型元数据位于 [`packages/model-providers`](https://github.com/makecindy/cindy/tree/b1d73b1f79ae812d0950c632db4785b1f06d48e7/packages/model-providers)，桌面端汇总可用列表在 `maker-host/active-catalog.ts`，而真正发请求前的路由拍板则集中在 [`provider-route.ts`](https://github.com/makecindy/cindy/blob/b1d73b1f79ae812d0950c632db4785b1f06d48e7/apps/desktop/src/main/maker-host/provider-route.ts)。

更关键的是，**Codex 和 Pi 拿到凭证与协议适配的方式截然不同**：

- **Codex 走“本地回环代理桥”**（[`codex-proxy-host.ts`](https://github.com/makecindy/cindy/blob/b1d73b1f79ae812d0950c632db4785b1f06d48e7/apps/desktop/src/main/maker-host/codex-proxy-host.ts)）：Codex CLI 启动时拿到的只是一个指向本机 `127.0.0.1` 的代理地址和占位 Key。当它发出 OpenAI Responses 格式的请求后，Main 进程里的本地代理负责拦截请求，通过 `responses-anthropic-bridge` 把协议转换成 Anthropic Messages 格式，再贴上真实的网关密钥发往上游。这样子进程本身根本碰不到真实密钥。
- **Pi 的自定义模型走“原生配置注入”**（[`pi-host.ts`](https://github.com/makecindy/cindy/blob/b1d73b1f79ae812d0950c632db4785b1f06d48e7/apps/desktop/src/main/maker-host/pi-host.ts)）：当使用官方订阅时，Pi 同样可以走宿主代理；但当接入自定义 API Key 供应商（BYOM）时，`buildPiNativeProvidersFromConfigs` 会组装好 Base URL 和模型列表，把真实密钥放进为子进程准备的独立环境变量里，通过 `apiKeyEnvVar` 交给 Pi 原生运行时直接调用。

把这两条路径对比着看，很多诡异的故障就一目了然了：如果“下拉框里压根没出现模型”，病根一定在 `active-catalog` 初始化阶段（比如某个被裁剪引擎的能力探测卡死了整张目录）；如果“能选模型，一发消息报 401”，病根就在 `provider-route` 或本地代理桥的请求头拼装（比如同时带上了冲突的 `Authorization` 和 `x-api-key`）。

---

## 五、持久化与崩溃恢复：别被 `sessions.status` 的名字骗了

桌面应用随时可能遇到系统休眠、强制退出或进程崩溃。Cindy 在 `main/localDb/` 下用 SQLite 配合 Drizzle ORM 持久化本地状态，并通过 [`DesktopSessionStorage`](https://github.com/makecindy/cindy/blob/b1d73b1f79ae812d0950c632db4785b1f06d48e7/apps/desktop/src/main/maker-host/session-storage.ts) 与核心编排层对接。

第一次查这张表时，几乎所有人都会踩一个命名陷阱：看到 `sessions.status` 字段的值是 `active`，就以为“当前有个 Agent 正在后台跑”。其实源码注释写得很清楚，这个字段表达的只是侧边栏卡片的归档状态——**`active`（正常展示）、`archived`（已归档）、`deleted`（已删除）**，跟此刻有没有子进程在干活毫无关系。

那客户端怎么知道哪些任务是被意外腰斩的？答案是看**轮次账本与存活进程的差集**：

- 如果数据库里某条会话留着“轮次已开始、尚未写入结束标记”的记录，而当前内存里又找不到对应的活跃子进程，说明它在上一轮运行中途断掉了。
- 应用冷启动时，`sessionActiveTurn.ts` 会扫描这类未闭合轮次，`interruptedResumeCandidates.ts` 据此在界面上提示用户是否恢复；而在运行期间如果发生意外中断，则由 `maker-ipc/interruptedTurnAutoResume.ts` 判定错误类型，再送回前面的 `AgentInputCoordinator` 决定自动续跑还是交给用户处理。

与此同时，SQLite 只管 Cindy 自己的产品账本；Codex 的本地目录、Pi 的会话转录文件、MCP 配置以及系统钥匙串里的凭证，都按各自的作用域分开存放。这也是我在做本地定制时决定保留整套 SQLite 和任务主干的原因——自己重写一个聊天界面很容易，但要把这套跨重启的中断检测和双层会话对齐重做一遍，极易引入丢消息的隐蔽 Bug。

---

## 六、外围能力与定制切入点

顺着“前端意图 → 输入协调器 → Maker/Session → 模型路由 → SQLite 账本”这条主干往外看，仓库里其余庞大的模块不过是挂在宿主上的扩展插件：

| 扩展方向 | 核心模块位置 | 架构角色与边界 |
| --- | --- | --- |
| **本地工具与电脑控制** | `main/mcp-integrations/`、`packages/lizi-mcps`、`browser-control-runtime` | 由 Main 统一装配权限与审批，供 Agent 在轮次内按需调用文件、终端与浏览器 |
| **插件体系** | `main/plugin-market/`、`packages/cindy-tools` | 市场浏览、本地已安装插件运行与权限记录分层管理；关掉在线市场不影响存量本地工具 |
| **多 Agent 协同与定时任务** | `packages/orca-workflow`、`packages/maker-scheduler` | 本质上是主任务链的另一类输入源，复用相同的会话编排与恢复机制 |
| **跨设备与远端执行** | `apps/mobile`、`packages/device-link`、`maker-remote-ssh` | 手机远控桌面改变的是“谁发指令”；SSH 远端执行改变的是“CLI 跑在哪台机器上” |
| **系统常驻与外设集成** | 语音、外设监听（如 WorkLouder）、菜单栏与灵动岛相关 Main 服务 | 往往在启动、窗口唤醒或睡眠恢复时独立自启，裁剪时必须逐一排查生命周期入口 |

摸清了这张全景图，我们就能回答文章开头的问题：**想做一个干净、可控、保留原版体验的个人定制版，刀该下在哪里？**

完全不需要去动 `AgentInputCoordinator`、`Session` 或 SQLite 表结构，只要守住三个咽喉要道就够了：

1. **启动快照门禁**：在 Main 初始化阶段固定一份能力开关快照（同步给 preload 与 Renderer），把不需要的引擎（如本次排除的 Claude Code）和后台常驻服务在注册入口直接拦住，同时排查睡眠恢复、日志补传和硬件预热等旁路入口。
2. **目录与路由注入**：顺着 `active-catalog.ts → provider-route.ts` 注册内部模型供应商，分别接好 Codex 的本地代理桥和 Pi 的原生配置路径。
3. **独立数据目录（Profile）**：在 `main/index.ts` 为定制版划分独立的用户数据目录，与官方发行版彻底隔离。

如果你想亲自对照源码走读一遍，最省力的路线就是沿着下面五步顺藤摸瓜：

1. `main/index.ts → bootstrap-electron.ts`：看应用启动时如何确定数据目录并拉起后台服务；
2. `ChatInput.tsx → preload/preload.ts → maker-ipc/register.ts`：看一次敲击回车如何跨过进程隔离墙；
3. `agent-input-coordinator.ts → maker.ts → session.ts → base-agent.ts`：看输入事务如何切分排队回滚与引擎执行；
4. `active-catalog.ts → provider-route.ts → codex-proxy-host.ts / pi-host.ts`：看模型名称如何落地成真实的协议转换与鉴权请求；
5. `session-storage.ts → localDb/sessionActiveTurn.ts`：看客户端如何靠持久化账本找回中断现场。

下一篇[《保留 Cindy 原版逻辑的本地定制：模型接入、可选功能与运行核验》]({{< ref "2026-10-24-cindy-company-local-build-notes.md" >}})，我们就把这套推导变成真实的代码补丁，看看目录空白、鉴权头冲突和后台漏网之鱼是怎么一个个抓出来的。

---

> **源码基线说明**：文中源码路径固定在 `makecindy/cindy` 上游提交 `b1d73b1`（2026-10-24，本地定制验证分支截至 `61db885`）。该仓库为涵盖 Desktop、Mobile 与共享 `packages/` 的客户端单仓，本文聚焦桌面端核心调用链，不涉及独立闭源服务端。
