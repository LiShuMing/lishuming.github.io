---
title: "【实践】保留 Cindy 原版逻辑的本地定制：模型接入、可选功能与运行核验"
slug: "cindy-company-local-build-notes"
date: 2026-10-24T00:00:00+08:00
lastmod: 2026-10-24T00:00:00+08:00
categories:
  - AI Engineering
tags:
  - Cindy
  - Codex
  - Pi
  - Agent
  - Security
  - Engineering Practice
description: "基于原版 Cindy 主干构建个人可控版本：接入公司内部模型网关、梳理 Codex 与 Pi 的凭证隔离差异、用启动快照门禁收敛九项后台能力，并通过进程与日志核验揪出三条旁路遗漏。"
draft: false
toc: true
math: false
---

在日常研发里使用桌面 Agent 客户端时，我想要的是一个边界干净的本地工作台：保留 [Cindy](https://github.com/makecindy/cindy) 原版的任务侧边栏、多项目布局、历史恢复、工具审批，以及按需触发的截图、键鼠与浏览器控制；底层执行引擎只留 Codex 和 Pi（按环境约束排除 Claude Code），统一接入公司内部的 Anthropic Messages 兼容模型接口；至于云账号、插件市场、全局语音唤醒、外设灯效和使用统计等后台常驻能力，全部默认关闭，只在设置页留出显式开关。

如果按最省事的直觉动手，很容易踩进两个极端：

- **另起炉灶写一套“极简聊天壳”**：我最初真的试过这条路。界面很快能跑起来，但只要进入真实编码场景，就会发现消息排队、运行中插话、工具权限弹窗、崩溃后的未闭合轮次恢复，全都绑在原版主干上。抛开原版壳子，等于把最容易出隐蔽 Bug 的状态机从头再写一遍。
- **只在前端藏掉几个设置组件**：如[上一篇架构笔记]({{< ref "2026-10-24-cindy-architecture-source-notes.md" >}})所分析，Cindy 的后台定时器、原生硬件助手和网络服务都活在 Electron 的 Main（主进程）里。前端把开关藏起来，后台照样自启；反过来，如果粗暴删掉 Main 里的进程间通信（IPC）处理函数，原版 React 组件挂载时发起的同步读取（`sendSync`）又会直接卡死界面。

退回原版生产主干做减法之后，整个改造过程变成了一场跨层排障：明明在配置里注册了公司模型，界面下拉框却一片空白；目录修通后，Codex 先卡在本地环境变量预检，接着又因为请求头冲突被上游网关拒之门外；等到九个功能开关在界面上全都显示“关闭”并跑通双引擎对话后，交叉核对进程内存映射与启动日志，还是抓出了三条绕过主初始化门禁的后台旁路。下面沿着真实的改造与排障顺序，把这套本地定制的设计取舍和验收细节完整摊开。

---

## 一、回到原版生产入口，先把运行目录隔离开

放弃独立精简界面、回到原版代码主干，意味着完整复用 Electron、preload、React、SQLite、`Maker` 编排器以及原有的任务生命周期。排队、审批、停止、历史归档和中断恢复全部照旧运转，服务端代码、协议白名单、数据库迁移（Migration）和自动更新状态机也无需改动；不带定制标记的普通发行构建完全维持上游行为。

为了不污染官方客户端的数据，定制版通过构建标记 `VITE_CINDY_COMPANY_LOCAL=1` 区分身份，并在启动时分配独立的 `CindyCompany` 配置目录（Profile）。它与官方发行版、早期精简试验版的数据目录彻底分开，既不共用本地 SQLite 数据库，也不自动复制旧目录里的任务或登录凭证。至于本次不使用的 Claude Code，只在二进制准备和 Agent 注册入口将其排除，而不是去仓库里大面积删改共享源码。

刚把独立 Profile 接进打包流程时，打包自检（Packaged Smoke Test）就暴露了一个隐蔽陷阱：自检脚本会显式传入临时的 `--user-data-dir` 来做隔离启动检查，但定制构建最初在入口处无条件覆盖成了日常的 `CindyCompany` 目录。结果本机如果正开着日常版应用，自检进程就会撞上目录锁冲突；更糟糕的是，一旦复用了正在运行的实例，“自检通过”就成了假象。把逻辑修正为“仅在未显式指定临时目录时回落到 `CindyCompany`”，才让打包自检与日常安装真正隔离开。

---

## 二、接入公司模型：同一套网关，两条截然不同的凭证路径

模型接入同样复用 Cindy 原有的自定义供应商（Custom Provider）、活动目录与路由机制，绝不在 Renderer 渲染进程里直接发起带密钥的请求。新增的 `main/company-local/connection.ts` 在 Main 侧读取本机已有的 `~/.config/opencode/opencode.json` 配置，为 Codex 和 Pi 同时生成供应商描述，绑定上游型号 `bailian/deepseek-v4.1-flash`（界面显示为 `DeepSeek V4.1 Flash`）。

读取外部配置时，代码对 URL 做了严格收口：强制校验 `https:` 协议、精确主机名、端口与路径，并拒绝携带 `userinfo`、`query` 或 `fragment` 的地址，防止配置被意外篡改时把公司密钥发往同名的其他端点（下文统一用 `https://models.corp.example/anthropic` 指代该网关基础地址；末尾的 `/v1/messages` 由下游协议适配层按需拼接，避免多层重复追加 `/v1`）。

虽然界面上选的是同一个模型名字，但把 Codex 和 Pi 放进源码调用链里对比，两者的协议适配与密钥交付边界其实完全不同：

![本次个人构建中 Codex、Pi 与公司模型的两条调用和凭证路径](/assets/images/cindy-source-notes/company-model-routing.svg)

*上方是 Codex 的本地回环代理桥，下方是 Pi 的原生自定义供应商路径。两者共用同一套上游型号与凭证源，但协议转换位置和密钥暴露边界不同。*

- **Codex 走 Main 侧本地回环（Loopback）代理桥**：Codex CLI 子进程启动时，拿到的只是一个指向本机回环地址的代理入口和合成占位鉴权值。当它发出 OpenAI Responses 格式请求后，Main 进程里的路由层读取真实公司密钥，完成 Responses 到 Anthropic Messages 的协议转换并发往上游。在这条路径上，真实密钥始终留在 Main 进程内存里，Codex 子进程及其拉起的工具都碰不到它。
- **Pi 走原生自带模型（Bring Your Own Model，BYOM）环境变量注入**：Pi 的自定义 API Key 供应商复用了 `buildPiNativeProvidersFromConfigs`。Main 调用 `readKey` 取出密钥后，会生成一个独立的环境变量名，把真实密钥塞进传给 Pi 运行时的子进程 `env`，再由模型配置通过 `apiKeyEnvVar` 引用，让 Pi 直接用原生 Anthropic Messages 协议请求公司网关。

看清这个差异很有必要：在这套定制构建里，Renderer、Git 仓库与安装包确实都不保存真实密钥，密钥源头也统一由 Main 管控；但在 Pi 的原生执行模型下，真实密钥进入了 Pi 子进程的环境变量空间，一旦任务期间执行的外部脚本或工具读取进程环境，其接触面比 Codex 的回环代理更宽。本次没有为了追求形式统一而重写 Pi 的底层代理机制，把这个真实边界记在明处，后续评估工具权限时才不会误判。

### 连闯三关：目录空白、本地预检拦截与上游鉴权头冲突

把 `connection.ts` 接进主干后，模型并没有立刻跑通，而是先后在三个不同层级撞上了故障。

**第一关：安装后模型下拉框一片空白。**
起初以为是自定义供应商没写进目录，顺着数据流往回查才发现，根源不在模型配置，而在前端的 `renderer/hooks/useAgentCapabilities.ts`。Renderer 初始化本地引擎能力时，依然硬编码去探测已被排除的 Claude Code；探测失败直接中断了整份本地目录快照的提交，连带让模型偏好镜像也卡在未就绪状态。修改该 Hook，让本地能力探测只针对已注册的 Codex 和 Pi（同时保留远端及其他 Agent 的既有生命周期），模型选择器立刻恢复正常。

**第二关：选中 Codex 发送消息，秒报缺少 `XDT_CODEX_API_KEY`。**
明明真实密钥由 Main 代理注入，为什么本地 CLI 还会要 Key？原来 Codex CLI 在真正发网络请求前有一道本地启动预检，要求环境中必须存在回环 Provider 声明的对应变量名。在 `maker-host/auth-adapters.ts` 的定制分支里，为它补回合成占位值并分配独立的 `CODEX_HOME`，既顺利通过了 CLI 本地预检，又避免了把真实公司密钥或个人订阅凭证直接塞给 Codex 进程。

**第三关：请求发到上游网关，返回鉴权失败。**
抓出请求头对比后真相大白：公司网关的 Anthropic Messages 接口有一条严格校验——**拒绝同一个 HTTP 请求同时携带 `Authorization` 和 `x-api-key` 两个鉴权头**。而 Cindy 原版的 Anthropic 协议桥会注入 `x-api-key`，外层通用路由又顺手附带了 `Bearer` 形式的 `Authorization` 头，两边一叠加，请求还没走到推理层就被网关拦下了。

定位到这一层后，修复只需要在公司供应商的 Codex 路由上精准摘除冲突头：

```ts
export function companyProvider(): Provider {
  const provider = buildUserProvider(companyProviderConfig());
  provider.routing.codex!.headerDelete = ['authorization'];
  return provider;
}
```

因为这项限制只针对公司网关在 Codex 代理桥下的请求头拼装，将 `headerDelete` 限定在 `companyProvider` 的 `codex` 路由分支里，Pi 的原生路径和其他供应商配置就完全不会受到波及。

---

## 三、九项可选能力：用启动快照代替热启停，补齐关闭态读取

解决了“怎么连上模型”，下一步是“怎么关掉不需要的后台能力”。如果允许用户在 Agent 正在执行任务时热切换云同步、语音监听或外设宿主，很容易把运行中的异步状态撕裂。因此本次定制明确放弃热启停，改用**启动期快照（Startup Snapshot）**：

1. 应用冷启动时，Main 先读取本地覆盖配置，冻结出本次运行周期的生效快照（`applied`）；
2. `preload` 将这份快照同步传递给 Renderer，确保主进程与前端在初始化阶段依据同一份判据挂载模块；
3. 用户在「设置 → 通用 → 可选功能」里拨动开关时，修改的只是下一次启动的配置值（`configured`）；只要 `configured` 与 `applied` 出现差异，界面就会根据计算出的 `restartRequired` 提示“完全退出并重新打开应用后生效”。

九项受控能力在定制构建中全部默认关闭：

| 字段 | 可选能力 | 默认值 |
| --- | --- | --- |
| `cloudAccount` | 云账号 | 关闭 |
| `pluginMarket` | 插件市场 | 关闭 |
| `deviceLink` | 设备互联 | 关闭 |
| `voiceInput` | 语音输入 | 关闭 |
| `globalVoice` | 全局语音快捷键 | 关闭 |
| `inputDevices` | 外设输入 | 关闭 |
| `nativePanels` | 灵动岛与菜单栏 | 关闭 |
| `messaging` | 消息平台连接 | 关闭 |
| `usageReporting` | 使用统计与日志上报 | 关闭 |

核心门禁判据集中在 `shared/companyLocalBuild.ts`：

```ts
export function isCompanyFeatureEnabled(feature: CompanyFeature): boolean {
  if (!IS_COMPANY_LOCAL_BUILD) return true;
  return appliedFeatures[feature] &&
    (feature !== 'globalVoice' || appliedFeatures.voiceInput);
}
```

这段不到十行的代码处理了两层关系：一是非定制构建直接返回 `true`，保证上游原版零侵入；二是声明了 `globalVoice` 对 `voiceInput` 的依赖——没开基础语音输入时，全局语音快捷键不会单独生效。而在交互细节上，即便用户先关掉了 `voiceInput`，设置页依然允许把此前勾选过的 `globalVoice` 切回关闭，防止开关被锁死在无法撤销的半开状态；同理，`cloudAccount` 只管账号体系准入，插件市场、设备互联等能力即便打开开关，仍需满足各自原有的登录与授权条件。

配置持久化直接复用了仓库现成的 `createOverrideSettingsFile`，落在独立 Profile 目录下的 `optional-features.json`。它只记录用户显式改过的覆盖项（Override），缺失字段一律按默认关闭解释，同时继承了原版的原子写入（Atomic Write）、跨进程文件锁、大小上限与损坏保护；点击“恢复默认”只需清掉这份覆盖文件，不会误伤任何历史任务或模型配置。前端的 `CompanyFeaturesSection.tsx` 则沿用原版设计系统的组件与语义色，补齐了五种语言文案、设置项搜索索引、读取失败重试以及写入期间的防重复提交锁。

### 光拦住初始化还不够：给同步查询留好“安全空响应”

把 Main 侧的语音、设备互联、原生面板、消息渠道、插件市场和日志上报入口全部套上门禁后，新的问题马上来了：原版 React 界面里有不少组件在挂载时会主动发 IPC 查询当前状态，语音模块甚至用了阻塞式的 `sendSync`。如果 Main 因为功能关闭而干脆不注册这些 IPC 通道，`sendSync` 就会因无人应答直接挂起渲染进程。

为此，新增的 `main/company-local/disabledReadbacks.ts` 专门负责提供**无副作用的关闭态响应**：当语音关闭时，直接向前端返回空历史列表、已禁用的快捷键配置和 `disabled` 权限状态，全程不触碰麦克风硬件；原生面板的任务状态投影与远程桌面查询也同样返回“未启用”确认，绝不为了回答一次状态查询而反向拉起后台助手，也不暗中留存本应丢弃的输入内容。

不过，后续实机运行日志显示，仍有少量 `voice-input`、`voice-conversation` 和插件市场通知的查询通道漏注册了关闭态兜底，在控制台留下了缺少 Handler 的告警。虽然报错恰恰说明对应后台服务确实没启动，但为了消除噪音并防止前端异常分支退化，这几处关闭态响应仍需在下一轮补齐。

---

## 四、打包流水线、分层验证与双引擎实机连通

打包脚本 `scripts/package-company.mjs` 串起了原版构建与自检流程：把本机预先缓存好的固定版本运行时（Codex `0.159.2`、Pi `1.0.2`）封入包内，生成应用与归档包，在完成严格的 ad-hoc `codesign` 签名核验后，先备份原有的 `/Applications/Cindy.app` 再执行替换（重编译变更二进制签名后，macOS 弹出的钥匙串访问授权交由用户正常确认，测试流程绝不绕过系统安全弹窗）。

打通整条本地打包链的过程中，还顺手排掉了三个工程故障：

1. **运行时依赖缺失**：为打包后的生产运行时补齐了漏装的 `@mixmark-io/domino`；
2. **离线完整性校验**：Electron 打包阶段拉取远程 `SHASUMS256.txt` 偶发超时，改为直接读取本地已安装 `@electron/get` 附带的官方 SHA-256 清单核对缓存包，在不降低完整性校验标准的前提下消除了网络抖动；
3. **测试视口读取兼容**：原版一个订阅相关的 Fast UI 测试夹具在原生 Electron 下拿到了空的 `viewportSize()`，改为读取真实窗口宽度后，原有的越界布局断言恢复正常。

在具备本地定制补丁与固定版本运行时缓存的工作区中，完整的自动化验证与打包可通过以下命令复现：

```bash
pnpm --filter desktop run typecheck
pnpm --filter desktop exec vitest run \
  src/main/__tests__/companyLocalBuild.test.ts \
  src/main/maker-host/__tests__/codexAuthIsolatedSandbox.test.ts \
  src/main/maker-host/__tests__/providerRoute.test.ts \
  src/renderer/__tests__/agentCapabilitiesDeviceCache.test.ts \
  src/renderer/__tests__/localCatalogSnapshot.test.ts \
  src/renderer/components/settings/__tests__/settingsSearchCatalog.test.ts
pnpm ui:check --changed --theme all
pnpm ui:check --page company-features --case all --theme all --locale all
pnpm release:package:company
```

各层检查的实跑结果与覆盖范围如下：

| 验证层 | 执行结果 | 验证的契约边界 |
| --- | --- | --- |
| Desktop 类型检查 | 通过 | 跨进程共享判据、路由与组件改动的 TypeScript 类型一致 |
| 6 个定向回归测试文件 | 213 项通过 | 定制构建开关、覆盖存储、目录收敛、鉴权头裁剪与关闭态响应 |
| 受改动影响的 UI 场景 | 778 / 778 通过 | 测试运行器覆盖的真实组件渲染（标记 `UNCOVERED` 的入口需实机另验） |
| 可选功能页多语言与主题矩阵 | 60 / 60 通过 | 五种语言、深浅色主题下的默认态、保存恢复、语音依赖、报错与窄窗口布局 |
| 国际化、术语与仓库卫生检查 | 通过（既有告警保留） | 新增文案 key、术语规范与检查规则无新增违规 |
| 打包自检与严格签名检查 | 通过（数据库 schema 130） | 隔离临时目录下的冷启动、数据库迁移与安装包签名完整 |
| 安装版实机界面目检 | 浅色模式及搜索已核验 | 九项开关默认全关、依赖联动、恢复默认与搜索定位正常（深色实机未单独目检） |

安装就绪、九项开关保持全关后，我在正式版界面里分别用 Codex 和 Pi 新建了两个最小验证任务——只要求模型返回固定口令，不调用任何本地工具、也不读取工作区文件：

- **Codex 任务**：界面正常流式返回 `CINDY_CODEX_OK`，轮次记录耗时 `2.733` 秒；
- **Pi 任务**：界面返回 `CINDY_PI_OK`，轮次记录耗时 `1.552` 秒。

核查本地 SQLite 数据库，两条会话记录的引擎类型、公司供应商标识、`bailian/deepseek-v4.1-flash` 型号以及轮次正常闭合标记全部与界面一致，且各自只包含一条用户消息和一条助手回复，没有任何越权工具调用。这证明在九项外围能力关闭的前提下，两条引擎的鉴权、协议转换与核心账本链路都已经端到端跑通。

---

## 五、运行核验：为什么“主初始化加了门禁”还会漏掉后台动作？

如果验收只停留在“界面开关全关、发消息能回”，故事到这里就结束了。但在实机核验阶段，我把启动日志、子进程树、进程加载的原生模块映射（Memory-mapped Native Modules）、本地监听套接字、安装包内容和源码放在一起交叉比对，结果推翻了“守住主初始化函数就够了”的乐观假设。

把启动快照的正常收敛链路与核验抓出的后台路径画在同一张图上，就能一眼看清问题出在哪里：`optional-features.json` 冻结的快照虽然拦住了主服务初始化、前端入口和同步查询兜底，但 Electron 主进程里的窗口事件回调、独立延迟定时器与组合状态检查仍然留着旁路缺口。

![Cindy 启动快照门禁与主进程旁路审计边界](/images/diagrams/app-cindy-optional-features-gate.svg)

在截至提交 `61db885` 的构建中，具体有三条旁路入口绕过了主门禁（以下三项均为核验发现的待修问题）：

### 1. 窗口显示回调叠加默认状态，意外唤醒了外设宿主

在主启动流程里，`startInputDeviceRuntime` 和 `startInputDevices` 确实已经被 `inputDevices` 开关拦住了；然而 `bootstrap-electron.ts` 在绑定主窗口事件时，仍然无条件挂载了 `attachWorkLouderCodexWindowReveal(mainWindow)`。每当主窗口显示，回调就会触发 `playWindowReveal` 去刷新硬件灯效。

更巧的是初始化顺序放大了这个漏洞：`WorkLouderCodexHostClient` 内部的 `deviceEnabled` 字段默认值竟然是 `true`！正常流程下，如果设备未启用，初始化函数会把它改写为 `false`；而在我们的关闭态分支里，正因为跳过了设备初始化，`deviceEnabled` 反而一直保持着初始的 `true`。于是窗口一亮相，灯效回调畅通无阻地拉起了底层外设宿主。

这一点在进程检查中拿到了铁证：单纯看进程名，Electron 的 Utility Process 只顶着一个看不出用途的通用 Helper 名字；但检查子进程映射的动态库时，能清楚看到它已经把包内的 WorkLouder `node-hid` 和 `serialport` 原生模块加载进了内存，与运行日志里的外设宿主启动记录完全吻合。下一轮修复必须把窗口生命周期回调（show / restore / hide）与 `WorkLouderCodexHostClient` 的默认状态一并收口。

### 2. `ready-to-show` 独立定时器绕过了日志上报初始化门禁

`initLogUploadService` 同样已经受 `usageReporting` 控制，但主窗口 `ready-to-show` 事件里的另一个独立回调仍无条件调用了 `scheduleStartupBackfill`，在启动后延迟扫描历史崩溃标记并尝试执行日志补传。

运行日志显示，该定时器确实被触发并扫到了一个待补传标记，最后因为当前环境没有配置上传目标才以 `skipped-not-configured` 退出。换句话说，这次没发生网络上传靠的是“恰好没配目标地址”，而不是开关真正拦住了调度。修复时必须在 `scheduleStartupBackfill` 入口补上同一套 `usageReporting` 判据，同时保留原有的隐私授权、脱敏白名单与并发代次约束。

### 3. 云心跳缺少“开账号、关统计”的组合判据

第三处是在源码交叉审计时顺藤摸瓜抓出来的静态隐患：`appCapabilities.ts` 已经声明了云心跳能力受统计开关约束，但 `heartbeatService.ts` 里的 `verifiedCloudUserId` 实际只检查了云模式、登录态与用户 ID。由于本次测试全程未开启云账号，这条路径在实机中并未触发；但如果后续用户在设置里单独打开 `cloudAccount` 登录、同时维持 `usageReporting` 关闭，心跳服务就会忽略统计开关直接发包。后续补丁需要把组合条件测全，而不能只测“九项全关”一种状态。

### 区分“真正的漏网之鱼”与“符合预期的常驻基础进程”

做进程与网络核验时，同样需要把保留能力的正常后台行为分辨清楚，避免对着正常进程误报：

- **拖拽释放辅助进程**：进程树里还能看到一个 Swift 编写的任务拖拽助手在启动时被预热拉起。翻看它的 Swift 源码可以确认，它平时处于休眠态，只在收到拖拽开始的 `arm` 信号后才注册 `leftMouseUp` 监听，鼠标松开或收到 `disarm` 后立即卸载事件钩子，既不监听键盘，也不记录日常鼠标轨迹。后续可以评估把“启动预热”改成“首次拖拽按需拉起”，但它与上面的外设模块误加载属于不同性质的问题。
- **基础进程与网络套接字**：Crashpad 启动参数明确为 `local-only`；Electron 自身的 Renderer、GPU、Network 进程以及 SQLite 维护、本地 MCP 工具桥均属保留的宿主基础骨架。采样当时的套接字快照中，所有本地监听均绑定在 `127.0.0.1` 回环地址，活跃外连仅指向公司模型网关解析出的目标地址。

---

## 六、改动地图与下一轮收口清单

回看整次定制，所有代码改动都收束在清晰的边界之内，没有触碰 `Maker` 会话状态机和数据库核心表结构：

| 模块路径 | 改动职责 |
| --- | --- |
| `shared/companyLocalBuild.ts` | 定义定制构建标记、九项能力默认值与跨进程共享门禁判据 |
| `main/company-local/connection.ts` | 校验并读取本机既有凭证，装配公司模型 Provider 与 Codex 请求头裁剪规则 |
| `main/company-local/featureStore.ts`、`featurePolicy.ts` | 管理 `optional-features.json` 原子覆盖存储、冻结启动快照、暴露可信设置 IPC |
| `main/company-local/disabledReadbacks.ts` | 提供语音、原生面板与远控的关闭态无副作用应答，防止前端 `sendSync` 挂起 |
| `main/index.ts`、`bootstrap-electron.ts`、`appCapabilities.ts` | 隔离 `CindyCompany` 数据目录、收敛注册引擎、挂载主服务门禁 |
| `maker-host/active-catalog.ts`、`custom-provider-store.ts`、`auth-adapters.ts` | 将公司模型汇入原版目录，补齐 Codex 本地占位环境变量与独立 `CODEX_HOME` |
| `preload/preload.ts`、`renderer/main-entry.tsx` | 在前端挂载前同步注入生效快照，约束 Renderer 侧自启调用 |
| `renderer/hooks/useAgentCapabilities.ts` | 仅探测已注册的本地引擎能力，修复目录快照提交中断导致的模型列表空白 |
| `CompanyFeaturesSection.tsx`、搜索索引与五语种词条 | 实现「可选功能」设置分区、依赖联动、异常反馈、恢复默认与搜索定位 |
| `scripts/package-company.mjs`、Vite / Forge 配置 | 封装固定版本 Codex/Pi 运行时、离线 SHA-256 校验、自检隔离与备份安装 |
| `CHANGES_LOG.md`、`docs/dev-rules/local-desktop-build.md` | 记录本地定制改动背景、构建命令与验收留档 |

沿着这次运行核验抓出的线索，下一轮迭代的收口顺序非常明确：先堵住 WorkLouder 窗口动效回调、`scheduleStartupBackfill` 延迟补传和 `heartbeatService` 组合判据这三处旁路入口，再把语音与市场的剩余关闭态 IPC 兜底补齐；重新打包替换后，不再满足于点开设置页看开关状态，而是完整观测冷启动、窗口反复显隐、延迟定时器触发以及“云账号开 + 统计关”组合下的进程映射与日志表现。

在复杂桌面客户端上做安全与隐私减法，“代码写了 `if`”“设置页显示关闭”“进程名里没看到可疑名字”和“完整生命周期内无旁路副作用”，其实是四件层层递进的事——只有把验证压到最后一环，那些藏在窗口回调和默认布尔值里的漏网之鱼才会真正浮出水面。

---

> **源码与环境基线说明**：本文基于 `makecindy/cindy` 上游提交 `b1d73b1`（2026-10-24）展开；本地定制实现对应个人分支提交 `fee7e3d`，安装与运行核验记录截至 `61db885`（个人本地提交，未推送至上游）。实测环境为 Apple Silicon macOS、Node.js 22、pnpm 10.33.2，封装运行时版本为 Codex `0.159.2` 与 Pi `1.0.2`；涉及公司内部网关的真实域名、密钥及本地进程标识均已做脱敏处理。
