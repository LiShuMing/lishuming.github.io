---
title: "【实践】Warp 原生直连百炼：从请求链路到 GUI 验证"
slug: "warp-bailian-native-direct-poc"
date: 2026-10-24T00:00:00+08:00
lastmod: 2026-10-24T00:00:00+08:00
categories:
  - AI Engineering
tags:
  - Warp
  - Bailian
  - DeepSeek
  - Rust
  - Agent
  - SSE
description: "追踪 Warp 自定义模型仍触发 Out of credits 的根源，通过拦截原生请求快照、将百炼 SSE 流翻译为 ClientActions 并隔离会话驱动，在 macOS Metal GUI 上跑通 deepseek-v4.1-flash 原生多轮流式对话。"
draft: false
toc: true
math: false
---

往 [Warp](https://github.com/warpdotdev/warp) 设置里填好阿里云百炼（Alibaba Cloud Model Studio）的自定义 Endpoint 和 API Key 后，我原本以为事情已经结束了，结果一上手就撞上了两个棘手问题：一是默认选中的 `qwen3.8-max` 在日常快问快答里响应偏慢；二是哪怕切到自己配好的模型、百炼账户里额度充足，在原生输入框按下回车时，依然可能被 Warp 弹出的 `Out of credits` 直接拦下。

换一个更轻快的模型很容易，但“用自己的 Key 还会报官方额度耗尽”逼出了一个更本质的架构追问：**按下回车后，HTTP 请求到底是从哪里发出去的？配额门禁又卡在哪一层？**

顺着[上一篇架构笔记]({{< ref "2026-10-24-warp-architecture-source-notes.md" >}})梳理的源码链路往下追，原因水落石出：Warp 原生的自定义 Endpoint 并不是客户端本机直连，而是把提供商地址和密钥打包发给 Warp 云端的 `/ai/multi-agent` 编排服务。要想让桌面客户端真正绕开这层云端推理中转、直接把请求发往百炼，同时完整保留 Warp 原生的输入框、多轮对话卡片与丝滑的流式 Markdown 渲染，就必须在请求跨出进程之前把它截下来，并亲手把百炼返回的标准流式文本翻译成 Warp 内部认识的状态动作。

这次我没有一上来就去啃极其庞杂的完整 Agent 工具链，而是先瞄准一个干净的最小闭环：固定接入百炼 Token Plan 上的 `deepseek-v4.1-flash`，实现客户端纯文本 HTTPS 直连，并在 macOS 桌面 GUI（`WarpOss.app`）里实测跑通原生输入、多轮上下文记忆与增量流式渲染（客户端正常登录与 Agent 总开关门禁暂维持原状，工具执行与本地持久化留待后续演进）。本文完整记录从调用链定位、协议翻译、Metal 编译打包排障到实机验证的全过程，并附上可复现的[本地 PoC 补丁](/assets/code/warp-bailian-direct-poc.patch)。

---

## 一、为什么配了自定义 Endpoint 还会撞上 `Out of credits`？

在动手改代码之前，先把原版“自定义 Endpoint”的配置流和请求流对齐，才能看清只改配置文件为什么救不了场。

在配置层，Warp 的模型身份本身就分成了四层：内部引用的 Endpoint 配置 ID、关联底层能力的 `config_key`、实际发给模型服务商的 `slug`，以及界面上展示的别名（Alias）。当 `get_active_base_model` 和 `get_preferred_base_model` 解析当前该用哪个模型时，还要综合当前分屏（Pane）的局部选择、Profile 默认值和回退策略——光改了 Profile 默认模型，已经打开的分屏未必会跟着切换。与此同时，[`ApiKeyManager`](https://github.com/warpdotdev/warp/blob/1e6c3ae465d6a925a9b97ae03877aaadd159b8f1/crates/ai/src/api_keys.rs) 把 Endpoint 元数据存在普通设置里，把敏感密钥存进 macOS 钥匙串（Keychain），等到发请求时再拼装成 `custom_model_providers`（排查时甚至能看到部分旧的可用性检查还在读历史遗留的 `ApiKeys.custom_endpoints`，而新注册逻辑已经迁到了另一套结构里）。

但比配置结构更致命的，是从敲下回车到屏幕出字这条完整的跨网络调用链：

![Warp 原生 Agent 从输入到流式展示的调用链](/assets/images/warp-source-notes/native-request-flow.svg)

*图中涵盖输入快照、异步流回调与视图重绘构成的完整闭环，箭头代表数据与事件流向，并非单一同步调用栈。*

沿着图中的编号往下走，每一段都在回答“额度拦截和协议绑定发生在哪”：

1. **发包前的客户端门禁与请求快照（步骤 1～3）**：用户在输入框敲下回车，`Input::input_enter` 识别出 Agent 模式后经 `submit_ai_query_with_routing` 路由给本地 `BlocklistAIController`。注意，**在任何网络请求发出之前**，`AIRequestUsageModel` 和 `PromptAlert` 就会先做一轮可用性与配额检查——这意味着单靠客户端本地的账户配额状态，就足以提前拦下提交。通过预检后，新会话与存量会话汇入 `send_query`，由 `input_for_query` 和 `input_context_for_request` 收集文本、选中块与环境上下文，再通过 `RequestInput` 与 `send_request_input` 冻结出一份不可变的 `RequestParams` 快照（防止等待网络响应期间用户乱改界面设置污染当前轮次），同时拉起 `ResponseStream`。
2. **发往 Warp 云端的私有编排协议（步骤 4～5）**：`app/src/ai/agent/api/impl.rs::generate_multi_agent_output` 把刚刚冻结的快照转成 Protobuf 格式的 `warp_multi_agent_api::Request`——里面不仅有 Prompt、任务历史、工具能力声明，还把 `custom_model_providers`（含自定义 URL 与密钥）一并装了进去。紧接着，`warp_multi_agent_client::generate_multi_agent_output` 换取 Warp 访问令牌，将请求 POST 到 Warp 官方的 `/ai/multi-agent` 端点。
3. **服务端事件解码与 `ClientActions` 状态驱动（步骤 6～8）**：云端返回的虽然也是服务端发送事件（Server-Sent Events，SSE）流，但载荷经过了 Base64 URL-safe 与 Protobuf 编码，解出来的是 `ResponseEvent`，跟 OpenAI 兼容接口的 `choices[].delta.content` 完全是两码事。`ResponseStream` 拿到事件后交给 `apply_response_event`，最终由 `AIHistoryModel::apply_client_actions` 和 `AIConversation::apply_client_action` 修改本地会话状态：`AddMessagesToTask` 负责创建消息槽位，`AppendToMessageContent` 负责往指定字段追加文本；状态变更抛出通知后，`AIBlock::render` 才把增长的 Markdown 渲染到屏幕上（若涉及工具调用，则再经动作模型审批执行，由 `send_follow_up_for_conversation` 开启下一轮请求）。

如果想自己对照源码顺一遍，直接跳下面这八个锚点，就能避开数千行 Controller 代码的迷宫：

| 阶段 | 关键函数 | 源码定位 |
| --- | --- | --- |
| 输入与路由 | `input_enter`、`submit_ai_query_with_routing`、`submit_ai_query_local` | [input.rs:13924](https://github.com/warpdotdev/warp/blob/1e6c3ae465d6a925a9b97ae03877aaadd159b8f1/app/src/terminal/input.rs#L13924)、[4598](https://github.com/warpdotdev/warp/blob/1e6c3ae465d6a925a9b97ae03877aaadd159b8f1/app/src/terminal/input.rs#L4598)、[15057](https://github.com/warpdotdev/warp/blob/1e6c3ae465d6a925a9b97ae03877aaadd159b8f1/app/src/terminal/input.rs#L15057) |
| 模型解析 | `get_active_base_model`、`get_preferred_base_model` | [llms.rs:908](https://github.com/warpdotdev/warp/blob/1e6c3ae465d6a925a9b97ae03877aaadd159b8f1/app/src/ai/llms.rs#L908) |
| 凭据序列化 | `custom_model_providers_for_request` | [api_keys.rs:1125](https://github.com/warpdotdev/warp/blob/1e6c3ae465d6a925a9b97ae03877aaadd159b8f1/crates/ai/src/api_keys.rs#L1125) |
| 发送快照准备 | `send_query`、`send_request_input` | [controller.rs:775](https://github.com/warpdotdev/warp/blob/1e6c3ae465d6a925a9b97ae03877aaadd159b8f1/app/src/ai/blocklist/controller.rs#L775)、[3047](https://github.com/warpdotdev/warp/blob/1e6c3ae465d6a925a9b97ae03877aaadd159b8f1/app/src/ai/blocklist/controller.rs#L3047) |
| 请求构造与发送 | 两层 `generate_multi_agent_output` | [api/impl.rs:16](https://github.com/warpdotdev/warp/blob/1e6c3ae465d6a925a9b97ae03877aaadd159b8f1/app/src/ai/agent/api/impl.rs#L16)、[client/lib.rs:48](https://github.com/warpdotdev/warp/blob/1e6c3ae465d6a925a9b97ae03877aaadd159b8f1/crates/warp_multi_agent_client/src/lib.rs#L48) |
| 异步流生命周期 | `ResponseStream::new`、`spawn_request`、`spawn_generate` | [response_stream.rs:374](https://github.com/warpdotdev/warp/blob/1e6c3ae465d6a925a9b97ae03877aaadd159b8f1/app/src/ai/blocklist/controller/response_stream.rs#L374) |
| 事件写入历史 | `apply_response_event`、`apply_client_actions`、`apply_client_action` | [controller.rs:3616](https://github.com/warpdotdev/warp/blob/1e6c3ae465d6a925a9b97ae03877aaadd159b8f1/app/src/ai/blocklist/controller.rs#L3616)、[history_model.rs:2044](https://github.com/warpdotdev/warp/blob/1e6c3ae465d6a925a9b97ae03877aaadd159b8f1/app/src/ai/blocklist/history_model.rs#L2044)、[conversation.rs:2875](https://github.com/warpdotdev/warp/blob/1e6c3ae465d6a925a9b97ae03877aaadd159b8f1/app/src/ai/agent/conversation.rs#L2875) |
| 界面渲染与工具回传 | `AIBlock::render`、`send_follow_up_for_conversation` | [view_impl.rs:873](https://github.com/warpdotdev/warp/blob/1e6c3ae465d6a925a9b97ae03877aaadd159b8f1/app/src/ai/blocklist/block/view_impl.rs#L873)、[controller.rs:2155](https://github.com/warpdotdev/warp/blob/1e6c3ae465d6a925a9b97ae03877aaadd159b8f1/app/src/ai/blocklist/controller.rs#L2155) |

---

## 二、直连方案设计：在发往云端前截流，把百炼 SSE 翻译成原生事件

看清了整条调用链，摆在面前的有三条路：一是继续折腾自定义 Endpoint 配置，但请求终究还要绕道 `/ai/multi-agent`；二是干脆退回终端分屏里跑第三方命令行 Agent，但这等于放弃了 Warp 原生输入框和对话卡片；三是在 `generate_multi_agent_output` 即将构造云端请求的前一刻劈开一条本地分支，直连百炼并把返回的流式增量翻译成 `ResponseEvent`。

第三条路最对胃口。为了把首期改动控制在干净可验证的范围内，我特意把边界收紧在**纯文本多轮对话**，暂不开放工具调用——这样就能把工具权限审批、命令幂等性、执行结果回传与跨任务恢复这几团相互缠绕的状态机先剥离开，专注验证“原生 UI 能否完全脱离官方推理中转独立运转”。

![百炼直连分支与原生 UI 事件适配](/assets/images/warp-source-notes/direct-bailian-flow.svg)

整套直连改造由三个紧耦合的环节组成：**请求快照截流**、**SSE 协议与事件状态机翻译**，以及**独立会话驱动隔离**。

### 1. 显式进程开关与请求前截流

新增的 `app/src/ai/direct_bailian.rs` 负责管理进程级开关与凭据快照。只有启动环境中显式设置了 `WARP_DIRECT_BAILIAN=1` 时，直连模式才会激活，确保日常默认路径毫发无损。构造请求快照时，`DirectBailianRequest::snapshot` 优先读取进程环境变量 `LLM_API_KEY`；若未设置，则回退查找当前构建渠道 `ApiKeyManager` 中 URL 匹配百炼 Token Plan 的配置项，并将快照冻结进本轮的 `RequestParams`。

当执行流走到 `api/impl.rs::generate_multi_agent_output` 时，代码在拼装 Protobuf 请求和获取 Warp 云端令牌**之前**先检查这份快照：

- 只放行不带引用块、文件或图片附件的纯文本 `UserQuery`（如果用户开启了输入脱敏策略，照常先跑一遍客户端既有的脱敏函数）；
- 即使外层 Controller 自动收集了终端输出或环境上下文，直连分支也不会把这些额外载荷透传给百炼；
- 一旦遇到不支持的输入类型，或者试图在旧的云端会话上混用直连模式，直接返回明确错误，绝不悄悄回退去调 Warp 官方模型。

### 2. 独立客户端：从百炼 SSE 字节流到 `AppendToMessageContent`

新增的独立 Crate `crates/warp_bailian_client/` 承担了全部 HTTP 通信、SSE 分片解析与 Warp 事件翻译工作。请求侧直接对齐百炼的 OpenAI 兼容接口（接口规范与模型开通条件见[百炼 DeepSeek API 文档](https://help.aliyun.com/zh/model-studio/deepseek-api)）：

```text
POST https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/chat/completions
model: deepseek-v4.1-flash
stream: true
stream_options.include_usage: true
enable_thinking: false
max_tokens: 2048
tools: 不发送
```

组装请求体时，`request_body` 会扫描当前单一任务里已有的 `UserQuery` 和 `AgentOutput`，还原成标准的 `user` / `assistant` 消息数组（自动剥离模型标记等内部元数据，若检测到多任务或含工具历史则拒绝发送），再前置一条说明纯文本助手边界的 `system` 提示词。这样第二轮提问就能自然带上第一轮的上下文。网络层设置了 120 秒超时并严禁 HTTP 重定向；遇到非 200 响应时只向上报告状态码而不拼接原始响应正文，同时为 `Config` 手写脱敏版 `Debug` 实现，防止密钥或敏感报错意外落入日志。

响应侧则需要按字节流严谨处理百炼 SSE 细节（参见[官方流式输出说明](https://help.aliyun.com/zh/model-studio/stream)）：增量正文位于 `choices[].delta.content`，开启 `include_usage` 后末尾统计帧的 `choices` 可能为空数组，整条流以 `[DONE]` 收尾。解析器在字节缓冲区上妥善处理了跨 TCP 分片的 UTF-8 多字节字符、`LF` / `CRLF` 换行、多行 `data:` 拼接与注释行，并给单个事件加上 1 MiB 体积上限。

拿到增量文本只是第一步，真正的巧思在于由本地适配层把多轮消息角色映射、SSE 增量切片还原、未授权 `tool_calls` 拦截与原生 `ResponseEvent` 重建串成严格保序的时序闭环：

![Warp 百炼直连中的流式协议与状态动作翻译时序](/images/diagrams/app-warp-stream-translation.svg)

对照时序图里的三个阶段，适配层在每个流状态节点都必须守住对应的状态机不变量：

| 百炼流状态 | 翻译出的 Warp 原生事件 | 必须维持的状态机不变量 |
| --- | --- | --- |
| HTTP 200 且响应头为事件流 | `Init` | 生成本地唯一的 `request_id`，绝不伪造云端 `conversation_token` |
| 首包准备落位 | 按需发 `CreateTask`，紧接 `AddMessagesToTask` | 回显本轮用户消息并预先建好空的助手消息槽位，锁定 `message_id` |
| 收到 `choices[].delta.content` | `AppendToMessageContent` | `FieldMask` 精确指向 `agent_output.text`，复用同一个 `task_id` 与 `message_id` |
| 收到正常 `stop` 且读到 `[DONE]` | `Finished(Done)` | 只有停止原因与流结束标记双双齐全，才算本轮成功闭合 |
| 触发 `length` 截断、工具调用或提前断流 | 错误事件 | 拒绝把半截回复当成功；关闭自动重试，防止已上屏的半截文字被重复刷一遍 |

核心的增量追加翻译浓缩在下面几行 Rust 代码里：

```rust
yield client_actions(vec![Action::AppendToMessageContent(AppendToMessageContent {
    task_id: task_id.clone(),
    message: Some(message(message_id.clone(), MessageKind::AgentOutput(AgentOutput { text }))),
    mask: Some(prost_types::FieldMask { paths: vec!["agent_output.text".to_owned()] }),
})]);
```

正是因为每次 yield 都带着恒定的 `message_id` 和指向 `agent_output.text` 的 `FieldMask`，上游的 `AIHistoryModel` 才知道“把这几个新字接在刚才那条助手气泡末尾”，`AIBlock::render` 也才能像对待官方云端流一样实时拉长 Markdown 视图。此外，当底层流被上层丢弃（Drop）时，HTTP 连接会随之立即中止。

### 3. `ConversationDriver::DirectBailian`：切断不匹配的云端副作用

如果只换掉 HTTP 客户端，原生会话默认还是会拿本地生成的任务 ID 去调云端状态同步，或者在退出重启时试图按云端身份恢复历史。为此，我们在 `conversation_driver.rs` 中专门新增了 `ConversationDriver::DirectBailian`，把六项会话行为逐一定死：

| 会话行为谓词 | PoC 设定 | 这样设计的工程原因 |
| --- | --- | --- |
| 掌控本轮生命周期 | **是** | 由本机直连流全权负责发起、流式推进与中止收尾 |
| 本机执行工具调用 | **否** | 首期锁定纯文本范围，不开放命令与文件副作用 |
| 从消息流重建输入 | **否** | 直接沿用当前本地输入快照组装请求 |
| 上报任务状态 | **否** | 本地生成的任务 ID 在 Warp 云端并不存在，禁止向后台同步 |
| 界面只读模式 | **否** | 保持底部原生输入框可用，支持同会话连续多轮追问 |
| 本地落盘持久化 | **否** | 暂不写入 SQLite，避开云端恢复协议冲突（代价是退出进程后 PoC 会话即清空） |

围绕这个新驱动，`conversation.rs` 不再为它分配服务端身份，`local_agent_task_sync_model.rs` 跳过后台状态上报，Controller 禁止将它与旧会话或共享会话转发混用，`ResponseStream` 也关掉了针对云端断流的自动重连恢复；同时，`request_usage_model.rs` 和 `prompt_alert.rs` 在 `WARP_DIRECT_BAILIAN=1` 模式下放行了客户端本地的官方额度拦截，并在 `llms.rs` 里把界面模型标签固定显示为 `deepseek-v4.1-flash · 百炼直连 PoC`，一眼就能确认当前走的是直连路由。

不过有一道门禁在首期刻意保持了原状：实测发现，如果启动 GUI 时完全跳过账号登录，整个 AI 菜单会处于置灰禁用状态；即使正常登录，也需要去 **Settings → Agents → Warp Agent** 打开总开关（对应 `settings/ai.rs::is_any_ai_enabled` 与引导状态检查）。也就是说，这次手术精准切开了“模型推理与额度计费路径”，尚未去拆整个桌面产品的账号体系。

---

## 三、从代码到可运行的 macOS GUI：凭据传递、Metal 工具链与终端防护

源码逻辑写通之后，真正要在 macOS 上弹出一个能交互的 GPU 渲染窗口，还接连跨过了三道环境与构建关卡。

### 1. 把远端开发机的凭据安全送进本地 GUI 进程

我的百炼密钥原本放在一台远端 SSH 开发机（别名 `dev1`）的 `~/.env` 里。远端 Shell 的环境变量显然不会自动飘到本机桌面进程里；在本地随便开个终端敲 `export` 也影响不到已经运行的 GUI；更何况已安装的商业版 `Warp.app` 与源码编译的 `WarpOss.app` 拥有不同的应用身份和钥匙串命名空间，商业版配过的 Keychain 条目未必能被开源渠道直接读取。

为此，本地脚本 `script/run-bailian` 提供了清晰的启动入口：当传入 `--from-dev1` 时，父进程通过 SSH 仅提取远端的 `LLM_API_KEY` 单一变量值到内存，随即注入即将拉起的 `warp-oss` 子进程环境，全程不落盘、不写配置文件、也不顺手导入其他无关变量（若密钥就在本机环境或开源渠道可解析的 Keychain 里，直接省掉 `--from-dev1` 即可）：

```sh
# 使用当前终端环境中的 LLM_API_KEY（或 OSS 渠道 Keychain），直接启动已构建好的应用包
./script/run-bailian --no-build

# 从远端 SSH 主机 dev1 按需读取密钥并启动已构建好的 GUI（复现时需替换为自己的 SSH 别名）
./script/run-bailian --no-build --from-dev1
```

这里的 `--no-build` 会通过 `cargo metadata` 定位已打包好的 macOS Debug 应用目录并直接拉起里面的 `warp-oss` 可执行文件，既省去重复编译签名，又保证每次运行的是同一份已核验二进制（脚本另提供 `--smoke` 参数用于跑独立的命令行真机连通性测试）。

### 2. 拆掉“空 Metal 库”假象，把锁定编译与打包签名解耦

本机编译环境为 Apple M4 Pro（24 GiB 内存，macOS 15.5）。起初机器上只装了 Command Line Tools，顺着仓库已有的 `WARP_LOCAL_SKIP_METAL` 环境变量虽然能跑过 `cargo check`，但翻看构建脚本就知道：**那个开关只是往产物里塞了一个空的占位 `.metallib` 来骗过类型检查，真要打开 GPU 窗口非崩不可。**

结合 macOS 15.5 的版本兼容矩阵（见 [Apple Xcode 系统要求](https://developer.apple.com/xcode/system-requirements)），我安装了 Xcode 16.4 并配齐 Metal `32023.620` 工具链，配合仓库锁定的 Rust `1.92.0` 与 `cargo-bundle` `0.11.0` 重新构建。注意，切回真机构建前必须先清掉 `warpui` 之前留下的空着色器缓存，并显式清除 `WARP_LOCAL_SKIP_METAL`：

```sh
# 清除此前跳过 Metal 类型检查时留下的空着色器构建缓存
cargo clean -p warpui

env -u WARP_LOCAL_SKIP_METAL \
  RUSTUP_TOOLCHAIN=1.92.0 CARGO_BUILD_JOBS=6 FRAMEWORK_OVERRIDE=dev \
  cargo build -p warp --bin warp-oss --features gui --locked
```

重编完成后，构建目录生成了 `31,233` 字节的真实 Metal 着色器库，并核实已完整嵌入最终可执行文件。

接下来的应用打包又遇到了 `cargo-bundle 0.11.0` 不认识 `--locked` 参数的问题。解决办法是把“锁定依赖编译”和“目录封装签名”拆成前后两步：上面已经用 `--locked` 编出了确定性的二进制，打包阶段直接挂上 `CARGO_BUNDLE_SKIP_BUILD=1` 跳过二次编译；同时加上 `--dont-open`、`GENERATE_SCHEMA=false` 和 `SKIP_SETTINGS_SCHEMA=1`，防止打包脚本中途拉起尚未报备的新二进制去生成配置文件：

```sh
RUSTUP_TOOLCHAIN=1.92.0 CARGO_BUNDLE_SKIP_BUILD=1 \
  WARP_BIN_NAME=warp-oss WARP_CHANNEL=oss FEATURES=gui \
  GENERATE_SCHEMA=false SKIP_SETTINGS_SCHEMA=1 \
  ./script/macos/run --dont-open
```

由于 `cargo-bundle` 初次打出的 DMG 早于外层脚本后续的资源拷贝、`Info.plist` 修正与 ad-hoc 签名，等全部后处理跑完后再重新压制最终镜像并用 `hdiutil verify` 校验。最终生成的 arm64 `WarpOss.app` 顺利通过 `codesign --verify --deep --strict` 检查，主可执行文件 SHA-256 固定为：

```text
51fa14d5a63c9a9a4e838e5a3f0a46cd16f0240456810606643b17e867499e4d
```

### 3. 企业终端防护拦截带来的验证顺序调整

在打包期间还有一个很真实的排障插曲：早期我写了一个独立的会话驱动测试二进制，一运行就异常退出。第一反应以为是 ad-hoc 签名没签好，直到桌面弹出了明确的企业终端安全防护拦截提示，才意识到是未报备的新增可执行文件触发了系统的执行管控。

确认原因后，我立刻停掉了那个临时测试程序并清理掉产物，绝对不去尝试绕过或关闭企业安全策略，而是把验证流程调整为：**先在编译期跑完单元测试、静态检查与固定产物的完整打包，拿着上面那串确定的二进制哈希按正常流程提交报备；等审批放行后，全程只用 `--no-build` 运行同一份已报备的 `WarpOss.app`。** 把“编译通过”“签名合法”和“安全策略放行”三件事分清，少走了很多弯路。

---

## 四、分层验证与原生 GUI 流式实测

为了确保每一层改动都有扎实证据，验证从底向上分成了五层推进：

| 验证层级 | 实跑结果 | 覆盖的验证边界 |
| --- | --- | --- |
| `warp_bailian_client` 单元测试 | `cargo nextest run -p warp_bailian_client` 15 项全绿 | 跨分片 UTF-8、CRLF、多行 `data:`、1 MiB 上限、空 `choices` 统计帧、停机带正文、拒接工具调用、截断报错、401 不重试、缺 Key 拦截及 `Init/Add/Append/Finished` 全链路翻译 |
| 静态代码检查 | 客户端严格 Clippy 通过；主应用定向检查通过 | 新增客户端零告警（主应用检查时仅针对基线遗留的 `unnecessary_unwrap` 局部放宽，未触碰无关源码） |
| 命令行真机连通（Smoke） | 两轮真实请求返回 `OK` 并准确回忆数字 `42` | 证实 HTTP/TLS、鉴权与 SSE 解析器对百炼线上接口真实可用（两次短请求观测首字延迟约 `1.21` 秒与 `0.84` 秒） |
| GUI 锁定编译与打包校验 | Rust `1.92.0` + 真实 Metal + 严格签名与镜像校验通过 | 确认 arm64 `WarpOss.app` 携带真实 GPU 着色器且包结构完整 |
| 原生 GUI 多轮流式交互 | 同会话两轮记忆与流式增量渲染实测通过 | 在已报备的同一哈希二进制上完成端到端桌面交互闭环 |

*注：早期单元测试与命令行 Smoke 在安全拦截提示前基于本机 Rust `1.94.1` 跑通，随后切回工作区锁定的 Rust `1.92.0` 完成兼容性对齐与完整 GUI 构建；独立驱动测试程序因上述终端管控未执行，留待后续补跑。*

报备放行后，通过 `./script/run-bailian --no-build --from-dev1` 拉起 `WarpOss.app`（核对启动前后主程序 SHA-256 完全一致），登录账号并开启 Warp Agent 开关，在新建的原生 Agent 面板里进行了两轮连续测试：

1. **第一轮写入上下文**：在原生输入框发送 `请记住测试代号“青竹-7319”。只回复：已记住。`，界面干净地返回 `已记住。`。
2. **第二轮检验多轮记忆与增量流式渲染**：不再提示代号，直接要求模型回忆刚才的测试代号，并接着生成 20 条带编号的终端使用技巧。模型第一句便准确报出 `青竹-7319`，随后一条接一条向下滚动生成列表。

为了确认界面是真正的逐块增量渲染、而不是等全文收完才一次性刷屏，我在第二轮生成中途截取了画面：当时列表正停在尚未写完的第 17 条，底部清晰显示着 `Warping...` 动画与停止按钮；稍等片刻后，第 20 条补齐并切换为完成态。与此同时，以约 `0.25` 秒为间隔对 GUI 主进程做了 30 秒的 TCP 连接采样，其中两个采样点准确命中了百炼 Token Plan 域名解析出的 IPv6 地址与 `443` 端口（伴随客户端自身原有的常规后台连接），两轮对话全程未再触发 `Out of credits` 拦截。

---

## 五、改动清单、补丁复现与后续演进路线

本次 PoC 涉及的全部源码文件与各自守住的边界汇总如下（随附补丁已包含全部实现、15 项客户端测试与启动脚本，不含任何密钥或个人环境数据）：

| 文件 / 模块 | 本次改动内容 | 后续扩展需守住的不变量 |
| --- | --- | --- |
| `Cargo.toml`、`Cargo.lock`、`app/Cargo.toml` | 注册 `warp_bailian_client` 工作区成员与依赖 | 保持 `--locked` 确定性构建可复现 |
| `app/src/ai/direct_bailian.rs`、`mod.rs` | 实现 `WARP_DIRECT_BAILIAN=1` 开关与凭据快照 | 未开启时零侵入默认路径，`Debug` 输出严禁泄露密钥 |
| `agent/api.rs`、`agent/api/impl.rs` | `RequestParams` 携带直连快照，在云端序列化前分流 | 直连报错直接终止本轮，绝不隐式回退至 Warp 云端 |
| `crates/warp_bailian_client/` | 封装百炼 HTTP 请求、字节级 SSE 解析与 `ClientActions` 翻译 | 维持 `task_id` / `message_id` 稳定，仅在 `stop` + `[DONE]` 时标记成功 |
| `conversation_driver.rs`、`conversation.rs` | 新增 `DirectBailian` 驱动，剥离服务端会话身份与落盘 | 本地生成的 ID 绝不冒充云端 `conversation_token` |
| `controller.rs`、`response_stream.rs` | 约束会话接续边界、透传取消信号、关闭云端断流重放 | 禁止与旧云端会话混用，防止半截流重放产生重复文本 |
| `local_agent_task_sync_model.rs` | 按 `ConversationDriver` 谓词跳过后台状态上报 | 无云端身份的本地会话不触发同步请求 |
| `request_usage_model.rs`、`prompt_alert.rs` | 在显式直连模式下绕开客户端本地配额弹窗拦截 | 仅放行本地 UI 配额预检，不影响百炼侧真实鉴权与计费 |
| `llms.rs` | 将直连模式下的有效模型显示为 `deepseek-v4.1-flash · 百炼直连 PoC` | 界面诚实反映固定模型路由，避免下拉框产生虚假切换预期 |
| `script/run-bailian` | 封装环境变量 / SSH 凭据注入、Smoke 测试与 `--no-build` 启动 | `--no-build` 直接运行已核验产物，不触发重编译改写签名 |

如果你想在本地检出同一份源码快照体验或继续改造，下载[本地 PoC 补丁](/assets/code/warp-bailian-direct-poc.patch)后按下面三步即可应用：

```sh
git checkout 1e6c3ae
git apply --check /path/to/warp-bailian-direct-poc.patch
git apply /path/to/warp-bailian-direct-poc.patch
```

站在“原生纯文本流式直连已跑通”的台阶上往后看，演进路线非常清晰：

- **近期加固（纯文本闭环内）**：实机补测点击 GUI 停止按钮时的 HTTP 连接即时中断表现，覆盖 `401` / `429`、中途断网、超长上下文截断等异常交互，补跑会话驱动测试，再把写死的 `deepseek-v4.1-flash` 提取成可由模型下拉框动态切换的配置列表。
- **深水区演进（走向完整本地 Agent）**：一旦跨出纯文本问答，就需要依次接管三块硬骨头——设计 OpenAI `tools` Schema 到 Warp 本地动作审批与 `send_follow_up_for_conversation` 的多轮工具闭环；在 SQLite 中为本地直连会话设计独立的持久化与重启恢复协议；以及全面梳理账户登录门禁与云同步开关，评估真正的离线解耦成本。在此之前，遇到需要改文件、跑命令或长周期恢复的重度任务，在 Warp 终端分屏里直接跑成熟的 CLI 工具，依然是最务实的互补方案。

---

> **源码与实验基线说明**：本文基于 [`warpdotdev/warp`](https://github.com/warpdotdev/warp/tree/1e6c3ae465d6a925a9b97ae03877aaadd159b8f1) 上游提交 `1e6c3ae`（实验与验证日期为 2026-10-24）编写；配套改动见 [`warp-bailian-direct-poc.patch`](/assets/code/warp-bailian-direct-poc.patch)。本机编译与实测环境为 Apple M4 Pro（24 GiB 内存，macOS 15.5）、Xcode 16.4、Metal `32023.620`、Rust `1.92.0` 与 `cargo-bundle` `0.11.0`。
