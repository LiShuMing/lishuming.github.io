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
description: "记录基于原版 Cindy 的个人构建：保留 Codex/Pi 与公司模型、九项功能默认关闭且可配置，修复目录和鉴权故障，并通过运行核验找出外设、日志补传和心跳门禁遗漏。"
draft: false
toc: true
math: false
---

这次 Cindy 定制的目标，是保留原版界面、任务、项目、历史和工具审批，同时减少不需要的云端及后台行为。选择的执行引擎是 Codex 和 Pi，模型使用已有公司的 Anthropic Messages 兼容接口；Claude Code 按本次环境约束排除。按需截图、鼠标键盘和浏览器操作继续保留，后台全局语音、外设输入等功能默认关闭，并允许用户在设置中自行选择。

实施中最有价值的几次纠正，都来自不同层次的证据互相冲突：独立精简界面能启动，但没有保留足够的原版体验；模型存在于配置，却完全不出现在选择器；目录修好之后，Codex 又分别遇到本地鉴权变量缺失和上游鉴权头冲突；最后，九个开关全部显示关闭，运行日志仍暴露了漏过门禁的后台路径。

本文保存这些改动的内容、原因和验收边界。[架构篇]({{< ref "2026-10-24-cindy-architecture-source-notes.md" >}})解释 Main、Maker、Session 与引擎之间的关系，这里沿着实际改造过程展开。源码基线为 `b1d73b1f79ae812d0950c632db4785b1f06d48e7`，本地实现提交为 `fee7e3d627`，安装验证记录提交为 `61db88572f`；这些个人提交未推送，也不代表上游已经采用。接口域名、密钥、个人任务编号、进程编号和原始业务日志不进入本文。

## 从独立精简界面回到原生产入口

“精简”最初被实现为一个单独的简单界面，随后明确改为在 Cindy 原代码上控制可选能力。这个调整改变了改造位置：不再重做一套聊天与任务管理，而是沿用 Electron、preload、React、SQLite、Maker 和原版任务生命周期。

保留这些入口，才能继续使用原有的排队、审批、停止、历史、恢复、项目和布局逻辑。代价是仍需理解原系统的初始化关系，无法靠删除几个组件得到一个独立、完整的安全边界。本次没有改服务端、协议 allowlist、历史数据库 migration 或更新器状态机；普通发行构建也继续走原行为。

实现使用构建标记 `VITE_CINDY_COMPANY_LOCAL=1`，并给个人应用分配独立的 `CindyCompany` profile。它与原发行版和此前精简版的数据目录分开，原有任务与凭证没有通过复制数据库的方式导入。二进制准备及 Agent 注册排除 Claude Code，留下 Codex、Pi；这与“把所有相关源码从仓库删除”有明确区别。

有一处构建验证问题也来自 profile：打包自检（Packaged Smoke Test）显式传入临时 `--user-data-dir` 时，个人构建原先仍强制改回日常 profile，导致与正在运行的应用冲突。修正后，个人默认目录只在没有显式临时目录时使用。自检与日常安装必须隔离，否则“测试通过”可能只是复用了用户正在运行的实例。

## 公司模型进入原目录，两个引擎走各自协议路径

模型接入复用 Cindy 的 custom provider、活动目录与路由，不在 Renderer 新建带密钥的 HTTP 请求。新增的 `main/company-local/connection.ts` 读取已有 OpenCode 配置，生成两种 runtime 都认识的供应商描述，型号是 `bailian/deepseek-v4.1-flash`，显示名为 DeepSeek V4.1 Flash。

![本次个人构建中 Codex、Pi 与公司模型的两条调用和凭证路径](/assets/images/cindy-source-notes/company-model-routing.svg)

*上方是 Codex 的本地协议桥，下方是 Pi 的原生自定义供应商路径。两条路径使用同一型号，但协议适配和密钥交付位置不同。图中省略了普通订阅账号及其他供应商分支。*

凭证来源仍是 `~/.config/opencode/opencode.json`。读取函数校验 HTTPS、精确主机、端口、路径以及 userinfo、query、fragment，避免把公司密钥交给同品牌但不同地址的服务。本文只用 `https://models.corp.example/anthropic` 表示端点，示例不包含真实地址或密钥。基础地址和最终 `/v1/messages` 路径应由对应协议适配负责，不能在多个层级反复追加 `/v1`。

这里需要明确区分两种凭证路径。Codex 启动后访问本地回环（Loopback）桥；它得到既有的合成占位鉴权值，真实公司密钥由 Main 的路由读取并在上游请求中使用。Pi 的 API key 自定义供应商则复用原生 BYOM（Bring Your Own Model）路径：`buildPiNativeProvidersFromConfigs` 调用 Main 的 `readKey`，生成独立环境变量名，将真实 key 放入返回的 `env`，再交给 Pi 运行时；模型配置通过 `apiKeyEnvVar` 引用它。

因此，准确的结论是“密钥来源读取和目录装配由 Main 控制，Renderer、仓库与安装包不保存真实密钥”。不能扩展成“两个引擎都只拿占位 key”或“真实密钥始终不离开 Main”。尤其在 Pi 的原生执行语义下，还要考虑子进程及任务工具可能接触其环境的风险。此次没有把 Pi 强行改成另一种宿主代理模型，这个边界应被明确记录，而不是借一句泛化的安全声明掩盖。

### 三次故障分别发生在目录、本地启动和上游鉴权

第一个现象是安装后没有模型可选。原因并非模型条目未添加，而是 Renderer 的本地能力初始化仍强制读取已经排除的 Claude Code；失败导致整份目录快照无法正常提交，模型偏好镜像也不能就绪。修复将本地能力初始化收敛到已注册的 Codex、Pi，保留远端及其他 Agent 的既有生命周期。对应位置是 `renderer/hooks/useAgentCapabilities.ts`，而不是模型名称所在的 UI 组件。

第二个现象是 Codex 报缺少 `XDT_CODEX_API_KEY`。即使真实公司密钥由 Main 注入，CLI 仍需要 loopback provider 声明的环境变量通过本地预检。`maker-host/auth-adapters.ts` 的个人构建分支保留原来的占位值以及独立 `CODEX_HOME`，避免把真实公司 key 或订阅账号凭证直接交给 Codex。

第三个现象是上游返回鉴权错误。公司 Messages 接口拒绝同一请求同时携带 `Authorization` 与 `x-api-key`。原版桥会注入 `x-api-key`，通用路由又可能带上 Bearer 头，于是请求在到达推理之前就被拒绝。修复只为本次公司供应商的 Codex 路由声明：

```ts
export function companyProvider(): Provider {
  const provider = buildUserProvider(companyProviderConfig());
  provider.routing.codex!.headerDelete = ['authorization'];
  return provider;
}
```

这是该端点的兼容条件，不是 Anthropic Messages 接口的通用规则。Pi 原生路径和其他供应商不需要跟着删除自己的鉴权字段。把协议名、引擎名和供应商政策拆开，才容易把修复限制在正确范围。

## 九项配置使用启动快照，设置只保存用户覆盖

为了避免切换开关时打断正在运行的任务，本次没有实现热启停。Main 先读取配置形成生效快照，preload 同步提供启动值，Renderer 在应用初始化时配置同一组判据。设置页展示已保存的选择，修改后提示完全退出并重新打开应用才生效。

默认值、已保存配置和当前生效值分别存在：`configured` 表示下一次启动要采用的值，`applied` 表示本实例的启动值，`restartRequired` 由两者差异计算。用户看见开关已打开，不代表当前服务已被即时启动；这个差异必须由文案解释。

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

核心判据在 `shared/companyLocalBuild.ts`。下面是实现中的关键部分：

```ts
export function isCompanyFeatureEnabled(feature: CompanyFeature): boolean {
  if (!IS_COMPANY_LOCAL_BUILD) return true;
  return appliedFeatures[feature] &&
    (feature !== 'globalVoice' || appliedFeatures.voiceInput);
}
```

它保留普通构建行为，并明确全局语音依赖语音输入。设置页还允许把已经选中的全局语音关闭，即使语音输入的依赖已经关掉，避免用户无法撤销既有选择。云账号只决定账号能力的准入；插件市场、设备互联等服务还要同时满足自己的开关、登录和原版授权条件。

持久化复用既有 `createOverrideSettingsFile`，保存位置是独立 profile 下的 `optional-features.json`。缺失文件或字段采用默认关闭；只写用户覆盖（Override），不把整份默认值永久固化。原子写入（Atomic Write）、跨进程锁、文件大小限制以及损坏文件保护继续沿用既有实现。恢复默认删除这一组覆盖，不删除任务、账号或其他设置。

新设置位于「设置 → 通用 → 可选功能」，使用原版 Switch、Button、语义颜色和设置行组件，五种语言都补齐文案，并提供逐项搜索定位。保存失败保留上次已保存状态，读取失败提供重新读取入口；执行中的写入会禁用重复提交。这些都属于配置状态的一致性要求，不应仅靠一个本地 React boolean 完成。

### 主进程门禁与关闭态读取需要一起实现

Main 给语音、设备互联、原生助手、消息连接、市场和上报服务的主要初始化入口增加判断，Renderer 同时控制相应入口及部分启动调用。移除语音初始化后，原版界面还有同步读取，因此新增不访问设备的关闭态响应：返回空历史、关闭的快捷键配置和 disabled 权限状态，防止 `sendSync` 因无响应挂起。

原生助手任务投影和远程桌面状态也增加了关闭态应答。此类兼容响应只确认通知或返回“未启用”，不能为了回答一个查询而启动助手，更不能默默保留原本应被丢弃的输入内容。

但这些适配没有覆盖所有 Renderer 调用。后续运行核验仍观察到部分 `voice-input`、`voice-conversation` 和市场通知查询缺少 handler。它们是待修复的关闭态兼容问题；报错本身不证明录音或市场后台已经启动。相反，也不能因为几个查询都返回关闭，就推导所有旁路副作用都已被阻断。

## 构建复用原流程，安装验证分层进行

`scripts/package-company.mjs` 调用原版打包和自检入口，封装本机已经准备好的固定版本 Codex、Pi runtime，然后生成个人应用和 ZIP。执行记录中的环境是 Apple Silicon macOS、Node.js 22、pnpm 10.33.2；公司 runtime 缓存版本为 Codex 0.159.2、Pi 1.0.2。这里只复用二进制分发，不复制原版账号状态。

个人包采用 ad-hoc 签名，完成严格签名检查后安装到 `/Applications/Cindy.app`，替换前保留旧应用备份。重新构建可能让 macOS 再次询问钥匙串访问，系统安全窗口需要用户自行处理。测试不能通过绕过这道系统授权获得所谓的“启动成功”。

构建过程中还修复了两处工程问题：一是为打包运行时补齐 `@mixmark-io/domino` 依赖；二是使用 Electron 已安装包附带的 SHA-256 清单校验缓存，避免远程校验清单下载超时。完整性校验仍保留，没有关闭证书检查或改动企业防护。另一个订阅 Fast UI fixture 在原生 Electron 下拿到空的 `viewportSize()`，改为读取实际窗口宽度，保留原越界断言。

当时的验证记录如下。它们覆盖不同的契约，数字不能相加当成一个“安全通过率”。

| 验证层 | 已执行结果 | 可以说明什么 |
| --- | --- | --- |
| Desktop 类型检查 | 通过 | 相关 TypeScript 类型一致 |
| 六个定向回归文件 | 213 项通过 | 公司构建、配置存储、目录、鉴权路由和关闭态响应等已覆盖契约 |
| 受改动影响的 UI 场景 | 778/778 通过 | runner 列出的真实组件场景；标为 `UNCOVERED` 的入口仍需另验 |
| 新设置页五语、深浅色场景 | 60/60 通过 | 默认态、保存恢复、依赖、失败与窄窗口 |
| i18n、术语、测试卫生和文档检查 | 通过，既有告警保留 | 翻译 key、约定和检查合同没有新增违规 |
| 打包自检及签名 | 通过，schema 130 | 安装包能在隔离条件下通过对应启动检查 |
| 安装后完整应用 | 已目检浅色设置页及搜索 | 九项均关闭，语音依赖、恢复默认状态及定位可见；完整应用深色未另做实机目检 |

具备本地个人补丁和固定版本 runtime 缓存后，以下命令从 Cindy 仓库根目录执行。它们是重复验证入口，不是让普通上游 checkout 自动获得个人补丁的安装脚本：

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

随后，在全部开关关闭的真实安装版中，新建两个独立的最小任务：仅要求固定回复，不调用工具、不读取文件。Codex 返回 `CINDY_CODEX_OK`，记录的轮次耗时为 2.733 秒；Pi 返回 `CINDY_PI_OK`，耗时为 1.552 秒。界面回答与 SQLite 中的引擎、供应商、型号及轮次结束标记一致；每条任务各有一条用户消息和一条助手消息，没有工具消息。

这两次单样本只验证文本推理、鉴权和协议链路，不是性能基准，也不是两种引擎速度的比较。它们没有覆盖工具循环、文件修改、全套电脑控制、长期稳定性或高并发；目录可见性、真实文本调用和完整任务能力仍是三种验收。

## 运行核验推翻了“主初始化有门禁就够了”的假设

2026-10-24 的关闭态检查交叉使用了当前启动日志、进程树、原生模块映射、监听套接字、安装包内容和源码。结论是：两个引擎的最小模型调用正常，但关闭态后台行为没有全部达标。以下三项截至 `61db88572f` **仍未修复**，不能把它们写进已完成优化清单。

### 外设窗口动效绕过了 inputDevices 判断

虽然 `startInputDeviceRuntime` 和 `startInputDevices` 已被门禁限制，`bootstrap-electron.ts` 仍无条件调用 `attachWorkLouderCodexWindowReveal(mainWindow)`。窗口显示触发 `playWindowReveal`，后者更新灯效并进入底层宿主。

问题又被初始化顺序放大：`WorkLouderCodexHostClient` 的 `deviceEnabled` 初值为 `true`，关闭态跳过正常设备初始化，没有把这层状态设成 `false`。于是窗口动效绕过了本应关闭的外设入口。运行日志确实记录宿主启动，Cindy 的子进程实际映射了已有应用中的 WorkLouder `node-hid` 和 `serialport` 原生模块。这比只搜索一个具有明显名称的 helper 进程更有证明力，因为 Electron utility process 的进程名可能只是通用 Helper。

这些证据确认外设宿主及原生模块启动，尚不能证明它记录了全部键盘内容。需要修复的是窗口回调和底层启动边界，并验证关闭态的窗口 show、restore、hide 等路径都不会加载宿主。只在设置页继续隐藏一个开关，解决不了这个问题。

### 服务初始化关闭，日志补传定时器仍执行

`initLogUploadService` 已受 `usageReporting` 限制，但主窗口 `ready-to-show` 的另一个回调仍无条件调用 `scheduleStartupBackfill`。它延迟执行崩溃标记检查和补传路径，绕过了主初始化函数。

本次日志显示找到一个待补传标记，最终结果为 `skipped-not-configured`，没有成功上传的证据。这个结果依赖当时没有上传目标，不能当成开关已经阻断调度。修复应在独立调度入口使用同一判据，同时保留原有隐私同意、脱敏白名单和并发标记代次合同；不能为追求简化削弱上传链的原安全约束。

### 云心跳只跟随认证态，没有消费统计能力判据

这是源码发现，当前未登录的本地模式没有真实触发。`appCapabilities.ts` 已定义云心跳能力与统计开关的关系，但 `heartbeatService.ts` 的 `verifiedCloudUserId` 只判断云模式、认证与用户。以后若用户开启云账号并登录、仍保持统计关闭，该服务没有完整消费对应的功能判据。

后续验收应覆盖“云账号开、统计关”的组合，而不只验证“全部关闭”。本次没有为了证明这条路径而启用云账号、建立真实云连接或更改隐私选择。

### 保留的按需能力和常驻进程应分别解释

运行中还存在任务拖拽释放助手。Swift 实现仅在收到拖拽 `arm` 后监听 `leftMouseUp`，释放或 `disarm` 时移除事件监视，不监听键盘，也不记录鼠标位置。应用会启动预热它，因此“进程常驻”和“此刻存在活跃事件监视”不能互相替代。可以把预热调整为首次拖拽按需启动，但这仍是待实施的取舍，不能据此宣称已经移除了该进程。

Crashpad 的启动日志则明确为 local-only。Electron 的 Renderer、GPU、网络进程，本地工具桥和数据库维护仍属于保留的基础运行逻辑。套接字快照中观察到的监听地址均为回环地址，对外连接与当时公司模型域名解析结果一致；点时快照不等于完整抓包，也不能证明历史上从未出现其他请求。

## 改动地图与下一轮验收

已经落地的修改可以按职责回查：

| 路径 | 内容与原因 |
| --- | --- |
| `shared/companyLocalBuild.ts` | 构建身份、九项默认值和共享能力判据 |
| `main/company-local/connection.ts` | 受限读取既有密钥、生成公司 provider 和专属鉴权头策略 |
| `main/company-local/featureStore.ts`、`featurePolicy.ts` | 原子 override、启动快照、可信来源的 get/set/reset IPC |
| `main/company-local/disabledReadbacks.ts` | 关闭态同步读取及通知兼容，避免为查询启动服务 |
| `main/index.ts`、`bootstrap-electron.ts`、`appCapabilities.ts` | profile、Agent 注册与主要服务门禁；仍有本文记录的旁路遗漏 |
| `maker-host/active-catalog.ts`、`custom-provider-store.ts`、`auth-adapters.ts` | 复用原目录和执行链，修复本地预检与认证装配 |
| `preload/preload.ts`、`renderer/main-entry.tsx` | 初始化前传递生效快照，控制 Renderer 启动行为 |
| `renderer/hooks/useAgentCapabilities.ts` | 本地只请求已注册引擎，恢复模型目录提交 |
| `CompanyFeaturesSection.tsx` 与搜索 sidecar、五语 locale | 配置界面、依赖、失败反馈、恢复默认与逐项定位 |
| `scripts/package-company.mjs`、Vite / Forge 配置 | 原版打包自检、个人 runtime 和可回退安装产物 |
| 根目录 `CHANGES_LOG.md`、`docs/dev-rules/local-desktop-build.md` | 保存实现原因、构建方式及当时验收结果 |

下一轮修复需要先覆盖已定位的 WorkLouder 窗口动效、日志补传和云心跳入口，再用真实生产入口验证关闭态副作用。语音与市场的关闭态查询兼容也应补齐，以减少错误噪音，并保证查询不会唤起被关闭的服务。重建后应在任务空闲时正常退出、备份和替换应用，重新观察冷启动、窗口切换以及延迟定时器，而不是只重复点开设置页。

本次实践提供的是一个保留原版逻辑的个人构建，以及一次暴露遗漏的分层核验。企业防护告警没有被当作已证实漏洞，系统安全策略也没有被关闭。可复用的经验是把“源码有判断”“配置显示关闭”“当前没有进程”和“完整生命周期无副作用”分别验收；最后一项仍需要有针对性的失败与时序证据。
