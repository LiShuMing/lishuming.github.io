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
description: "梳理 Warp 原生 Agent 从输入到响应的调用链，记录 deepseek-v4.1-flash 客户端直连 PoC、SSE 事件适配、会话隔离、Xcode/Metal 构建与原生 GUI 多轮及流式验证，并说明尚未完成的工具、恢复和账户解耦。"
draft: false
toc: true
math: false
---

给 Warp 配上百炼（Alibaba Cloud Model Studio）的 endpoint 和 API key 之后，我遇到了两个问题：默认的 `qwen3.8-max` 响应不够快；即使选中自己的模型，发送请求仍可能被 Warp 的 `Out of credits` 阻止。前一个问题可以尝试更换模型，后一个问题需要确认请求到底在哪里发出、额度判断又发生在哪里。

这次没有直接扩展成完整 Agent 后端，而是先完成一个最小闭环：固定使用 `deepseek-v4.1-flash`，客户端直接访问百炼 Token Plan，保留 Warp 原生输入框、多轮文本历史和流式展示，暂不支持工具调用。**macOS GUI 的原生输入、两轮上下文和流式增长已经验证通过；整个应用仍需要正常 Warp 登录及启用 Agent。**

源码基线为 [`1e6c3ae465d6a925a9b97ae03877aaadd159b8f1`](https://github.com/warpdotdev/warp/tree/1e6c3ae465d6a925a9b97ae03877aaadd159b8f1)，实验日期为 **2026-10-24**。整体架构与终端部分见[上一篇源码笔记]({{< ref "2026-10-24-warp-architecture-source-notes.md" >}})。本文附上[本地 PoC 补丁](/assets/code/warp-bailian-direct-poc.patch)，其中新增文件和行为均属于实验改动，不能在上述上游快照中直接找到。

## 自定义 endpoint 为什么还会受到 Warp 额度影响

原生自定义 endpoint 解决的是“使用哪个提供商和模型”的配置问题。沿源码继续追，会发现默认传输仍指向 Warp 的 `/ai/multi-agent`。客户端把提供商 URL、凭据、schema 和模型映射装进请求，由 Warp 服务端接收。这个边界足以解释：拥有百炼额度，不代表所有客户端门禁与服务端策略都会自动按预期放行。

配置层还有几种身份需要分清。endpoint 的配置 ID 用于内部引用，模型的 `config_key` 关联内部模型标识，提供商的模型 slug 用于实际调用，alias 则用于界面显示。`get_active_base_model` 与 `get_preferred_base_model` 还要处理 pane 选择、profile 和回退。修改 profile 的默认模型，并不保证已打开 pane 的选择同时改变。

[`ApiKeyManager`](https://github.com/warpdotdev/warp/blob/1e6c3ae465d6a925a9b97ae03877aaadd159b8f1/crates/ai/src/api_keys.rs) 将 endpoint 定义和密钥管理分开：元数据进入设置，macOS 密钥进入 Keychain，再在请求时组装为 `custom_model_providers`。这能解释配置的存储方式，不能证明默认请求没有把凭据交给 Warp 服务端。后者需要看序列化和 HTTP 发送路径。

此前排查还发现，部分可用性判断读取旧的 `ApiKeys.custom_endpoints`，而新的 endpoint 注册与解析在另一套结构中。这构成一种有源码依据的兼容性疑点，但没有足够证据将它定为已安装版本出现额度错误的唯一根因。尝试补齐兼容配置，也不能代替“原生输入框实际发送成功”的验证。

## 输入到响应：完整调用链在哪里跨越网络

下图概括基线原生路径。上下文组装、异步事件回调和视图更新共同组成闭环；箭头不能全部理解为同步函数调用。

![Warp 原生 Agent 从输入到流式展示的调用链](/assets/images/warp-source-notes/native-request-flow.svg)

### 输入先变成请求快照

`Input::input_enter` 判断输入模式，Agent 请求经过 `submit_ai_query_with_routing` 再进入本地 Controller 路径。`AIRequestUsageModel` 和 `PromptAlert` 的检查可能在 HTTP 请求之前阻止提交，所以额度报错并不必然来自模型提供商。

新会话与已有会话分别进入 `send_user_query_in_new_conversation`、`send_user_query_in_conversation`，随后汇聚到 `send_query`。Controller 的 `input_for_query` 创建 `AIAgentInput::UserQuery`；`input_context_for_request` 根据本次选择、附件、环境与功能条件构造上下文。`RequestInput` 接着固定模型和工作目录等字段，`send_request_input` 汇集当前任务、服务端会话身份和团队作用域，建立 `RequestParams` 与 `ResponseStream`。

快照的作用是让异步请求使用一组一致的参数，避免网络等待期间界面设置变化影响同一轮请求。PoC 也在这个边界捕获直连配置，而不是在流式回调中反复读取设置。

### 默认传输使用 Warp 事件协议

`app/src/ai/agent/api/impl.rs::generate_multi_agent_output` 将参数转换为 `warp_multi_agent_api::Request`。其中不只是 prompt，还包括任务、模型配置、工具能力以及自定义模型提供商。随后 `warp_multi_agent_client::generate_multi_agent_output` 获取 Warp 访问凭据，向 `/ai/multi-agent` POST 请求。

返回传输使用服务端发送事件（Server-Sent Events，SSE），事件内容再经过 Base64 URL-safe 和 protobuf 解码，形成 `ResponseEvent`。它不等于提供商 OpenAI 兼容接口的 `choices[].delta.content`。服务端内部怎样组织提示词、访问百炼、重试和记账，不在这份客户端源码所能证明的范围内。

### 事件修改历史，历史再驱动视图

`ResponseStream` 消费网络事件，Controller 的 `apply_response_event` 继续分派。`AIHistoryModel::apply_client_actions` 与 `AIConversation::apply_client_action` 更新任务和消息：`AddMessagesToTask` 建立消息，`AppendToMessageContent` 按字段追加内容。历史更新事件再通知 `AIBlock` 渲染，完成态则由流结束事件和 Controller 的收尾逻辑协调。

如果返回工具动作，动作模型还会经过权限确认和本机执行，再将工具结果回传，形成下一轮请求。这个闭环解释了为何直接把 OpenAI SDK 的文本打印到屏幕上不够：原生 UI 还需要消息身份、任务归属、轮次状态和完成语义。

阅读时可以从以下固定版本入口逐步向下追，避免被 Controller 的体量淹没：

| 阶段 | 关键函数 | 固定版本入口 |
| --- | --- | --- |
| 输入与路由 | `input_enter`、`submit_ai_query_with_routing`、`submit_ai_query_local` | [input.rs:13924](https://github.com/warpdotdev/warp/blob/1e6c3ae465d6a925a9b97ae03877aaadd159b8f1/app/src/terminal/input.rs#L13924)、[4598](https://github.com/warpdotdev/warp/blob/1e6c3ae465d6a925a9b97ae03877aaadd159b8f1/app/src/terminal/input.rs#L4598)、[15057](https://github.com/warpdotdev/warp/blob/1e6c3ae465d6a925a9b97ae03877aaadd159b8f1/app/src/terminal/input.rs#L15057) |
| 模型解析 | `get_active_base_model`、`get_preferred_base_model` | [llms.rs:908](https://github.com/warpdotdev/warp/blob/1e6c3ae465d6a925a9b97ae03877aaadd159b8f1/app/src/ai/llms.rs#L908) |
| 凭据序列化 | `custom_model_providers_for_request` | [api_keys.rs:1125](https://github.com/warpdotdev/warp/blob/1e6c3ae465d6a925a9b97ae03877aaadd159b8f1/crates/ai/src/api_keys.rs#L1125) |
| 发送准备 | `send_query`、`send_request_input` | [controller.rs:775](https://github.com/warpdotdev/warp/blob/1e6c3ae465d6a925a9b97ae03877aaadd159b8f1/app/src/ai/blocklist/controller.rs#L775)、[3047](https://github.com/warpdotdev/warp/blob/1e6c3ae465d6a925a9b97ae03877aaadd159b8f1/app/src/ai/blocklist/controller.rs#L3047) |
| 请求构造与发送 | 两层 `generate_multi_agent_output` | [api/impl.rs:16](https://github.com/warpdotdev/warp/blob/1e6c3ae465d6a925a9b97ae03877aaadd159b8f1/app/src/ai/agent/api/impl.rs#L16)、[client/lib.rs:48](https://github.com/warpdotdev/warp/blob/1e6c3ae465d6a925a9b97ae03877aaadd159b8f1/crates/warp_multi_agent_client/src/lib.rs#L48) |
| 异步流生命周期 | `ResponseStream::new`、`spawn_request`、`spawn_generate` | [response_stream.rs:374](https://github.com/warpdotdev/warp/blob/1e6c3ae465d6a925a9b97ae03877aaadd159b8f1/app/src/ai/blocklist/controller/response_stream.rs#L374) |
| 事件应用 | `apply_response_event`、`apply_client_actions`、`apply_client_action` | [controller.rs:3616](https://github.com/warpdotdev/warp/blob/1e6c3ae465d6a925a9b97ae03877aaadd159b8f1/app/src/ai/blocklist/controller.rs#L3616)、[history_model.rs:2044](https://github.com/warpdotdev/warp/blob/1e6c3ae465d6a925a9b97ae03877aaadd159b8f1/app/src/ai/blocklist/history_model.rs#L2044)、[conversation.rs:2875](https://github.com/warpdotdev/warp/blob/1e6c3ae465d6a925a9b97ae03877aaadd159b8f1/app/src/ai/agent/conversation.rs#L2875) |
| 原生展示与工具回传 | `AIBlock::render`、`send_follow_up_for_conversation` | [view_impl.rs:873](https://github.com/warpdotdev/warp/blob/1e6c3ae465d6a925a9b97ae03877aaadd159b8f1/app/src/ai/blocklist/block/view_impl.rs#L873)、[controller.rs:2155](https://github.com/warpdotdev/warp/blob/1e6c3ae465d6a925a9b97ae03877aaadd159b8f1/app/src/ai/blocklist/controller.rs#L2155) |

## 最小改造选在哪里，为什么先不做工具

有三条可行路线。继续修正自定义 endpoint 配置，改动最少，但仍沿用 Warp 服务端编排；在终端里运行已有 CLI，可以复用 CLI 的工具和会话能力，但不是原生 Agent 输入框；在原生请求出口增加直连分支，需要自己适配协议和状态，却能直接验证现有 UI 是否可以脱离该推理中转。

本次选择第三条，并把目标收窄为文本对话。暂不实现工具循环，可以先避免工具权限、执行幂等性、结果回传和多任务恢复这几个互相依赖的问题。这个选择也限定了成果：它是原生文本后端 PoC，不能替代完整 Warp Agent。

![百炼直连分支与原生 UI 事件适配](/assets/images/warp-source-notes/direct-bailian-flow.svg)

### 在云端请求构造之前截取一轮

新增 `app/src/ai/direct_bailian.rs` 提供进程级开关和凭据快照。只有显式设置 `WARP_DIRECT_BAILIAN=1` 才开启。`DirectBailianRequest::snapshot` 优先读取 `LLM_API_KEY`，缺省时复用当前渠道 `ApiKeyManager` 中 URL 匹配百炼 Token Plan 的 endpoint。结果保存在本轮 `RequestParams` 中。

`api/impl.rs::generate_multi_agent_output` 在正常 protobuf 请求构造和 Warp 认证之前检查这份快照。直连只接受无引用附件的 `UserQuery`，提取普通文本；配置要求脱敏时先执行已有输入脱敏逻辑，再交给新增客户端。其他输入返回不支持错误，不回退到 Warp 模型。

这意味着即使原生 Controller 收集了自动上下文，PoC 也不会将整份上下文直接转给百炼。终端输出、文件或图片附件、MCP 工具及工具结果都不在此次支持范围内。切换到直连模式后也不能接续旧的 Warp 云端会话，以免混用历史和身份。

### 新客户端只负责文本协议与事件翻译

新增 `crates/warp_bailian_client/` 独立封装请求、SSE 解码和原生事件适配。固定配置如下：

```text
POST https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/chat/completions
model: deepseek-v4.1-flash
stream: true
stream_options.include_usage: true
enable_thinking: false
max_tokens: 2048
tools: 不发送
```

这个 endpoint 和模型组合在本次账户及时间点完成了实际调用；不能推导为所有地域、工作空间和套餐均可使用同一模型。OpenAI 兼容接口形式及模型访问条件可继续核对[百炼 DeepSeek API 文档](https://help.aliyun.com/zh/model-studio/deepseek-api)。

`request_body` 将同一任务中已有的 `UserQuery` 和 `AgentOutput` 转成 `user`、`assistant` 消息，忽略推理和模型标记类元数据，拒绝其他不兼容历史及多个任务。再加入当前输入与文本能力范围的 system 消息。这样第二轮能带上前一轮文本，但还没有上下文压缩、预算控制或复杂任务恢复。

原生客户端设置 120 秒请求超时并禁止 HTTP 重定向；HTTP 错误只保留状态码，避免把提供商原始错误正文中的敏感信息加入新增日志或错误。`Config` 自定义 `Debug`，不输出密钥。这里降低了新增路径泄漏凭据的机会，不代表整个应用已经经过完整安全审计。

百炼 SSE 的文本来自 `choices[].delta.content`；开启 usage 时，最后的统计片段可能具有空 `choices`；流以 `[DONE]` 标记结束。这些协议细节对应[官方流式输出说明](https://help.aliyun.com/zh/model-studio/stream)。客户端按字节缓冲，支持跨网络分片的 UTF-8、LF/CRLF、多行 `data:` 和注释，并限制单事件为 1 MiB。请求了 usage，但本 PoC 没有展示或记账这些统计。

转换后的原生事件需要满足以下顺序和身份约束：

| 提供商侧状态 | Warp 侧事件 | 需要维持的约束 |
| --- | --- | --- |
| HTTP 成功且为事件流 | `Init` | 生成本地 request ID，不伪造 Warp conversation token |
| 建立本轮消息 | 必要时 `CreateTask`，随后 `AddMessagesToTask` | 回显用户消息并建立空助手消息，后续追加沿用同一 message ID |
| 收到文本 delta | `AppendToMessageContent` | mask 指向 `agent_output.text`，维持 task / request / message 归属 |
| 收到停止与 `[DONE]` | `Finished(Done)` | 只有完整完成才进入成功态 |
| 工具调用、超限或截断 | 错误事件 | 不把部分文本当作完整成功，不自动重试或切换提供商 |

其中一段实际代码体现了适配的核心：

```rust
yield client_actions(vec![Action::AppendToMessageContent(AppendToMessageContent {
    task_id: task_id.clone(),
    message: Some(message(message_id.clone(), MessageKind::AgentOutput(AgentOutput { text }))),
    mask: Some(prost_types::FieldMask { paths: vec!["agent_output.text".to_owned()] }),
})]);
```

稳定的消息 ID 和字段 mask 使既有历史模型知道“继续增长哪条消息”，因此可保留原生展示。仅输出一串字符串，无法提供同样的状态语义。

完成判定也刻意收紧：需要正常 `stop` 和 `[DONE]`；缺少结束标记的 EOF、`length`、工具调用或其他异常结束都返回错误。直连分支关闭原有自动重试和恢复，避免已显示部分文本后重放请求产生重复内容。底层流被丢弃时会取消 HTTP，应用层沿用取消信号；这项取消机制有源码实现，但本次没有专项点击 GUI 停止按钮验证。

### 会话驱动决定哪些副作用必须停下来

增加 HTTP 客户端还不够。默认原生会话会使用服务端 conversation token、状态同步和持久化路径。为此新增 `ConversationDriver::DirectBailian`，逐项定义行为：

| 行为 | PoC 选择 | 原因与代价 |
| --- | --- | --- |
| 拥有本轮生命周期 | 是 | 本机负责发送、结束与取消 |
| 本机执行工具调用 | 否 | 文本范围不开放工具执行 |
| 从流消息重建输入 | 否 | 沿用当前本地输入组织方式 |
| 上报任务状态 | 否 | 不把本地任务 ID 当作服务端任务 |
| 只读界面 | 否 | 允许原生后续提问 |
| 本地持久化 | 否 | 先避免不匹配的恢复协议；退出即失去 PoC 历史 |

`conversation.rs` 不给这类会话设置服务端身份；`local_agent_task_sync_model.rs` 按驱动行为决定是否上报；Controller 限制接续不兼容的旧会话，并避开共享会话转发及特定专用请求入口。`ResponseStream` 则为直连关闭云端恢复分支。这些是会话语义改动，不能用一个 endpoint 字段替代。

`request_usage_model.rs` 和 `prompt_alert.rs` 还在显式 PoC 模式下放行对应的云端额度与提示判断。但原生应用的账户和 Agent 总开关仍存在：实际启动时，跳过登录会使 AI 菜单禁用；正常登录后还需要到 **Settings → Agents → Warp Agent** 打开开关。`settings/ai.rs::is_any_ai_enabled` 与 onboarding 设置解释了这一现象。因此，这次只拆开了推理路径，尚未拆开整个产品的账户依赖。

## 凭据如何进入 GUI：SSH 环境不会自动继承

最初的凭据保存在 SSH 开发机的 `~/.env`。远端 shell 的变量不会自动出现在本机 GUI 进程中；在另一个终端 `export` 也不会改变已经运行的 Warp 进程。需要明确读取来源并让启动者把值传给新进程。

本地新增的 `script/run-bailian --from-dev1` 完成这一步：通过已有 SSH alias `dev1` 读取指定的 `LLM_API_KEY`，父进程捕获返回值，再放入待启动进程的环境。它没有导入全部 `LLM_` 变量，也没有把 key 写进仓库或设置文件；实现仍会让凭据经过 SSH 管道与进程内存，应按自己的凭据管理要求使用。没有远端环境时，可以直接使用当前环境的 `LLM_API_KEY`，或配置当前 OSS 渠道可解析的 Keychain endpoint。

安装版与 OSS 版的应用身份和 Keychain 命名空间可能不同，所以“安装版已经配过 key”不等于 OSS 版一定能读取。PoC 固定的有效模型标签为 `deepseek-v4.1-flash · 百炼直连 PoC`，用来表明实际路由；模型下拉框暂不控制这个固定模型。

```sh
# 使用当前进程环境或 OSS 渠道可解析的 Keychain；不重建已打包 GUI
./script/run-bailian --no-build

# 本次实验的远端凭据来源；复现者需自行配置对应 SSH alias
./script/run-bailian --no-build --from-dev1
```

`--no-build` 通过 Cargo metadata 定位已有 macOS debug 应用包，直接执行其中的 `warp-oss`，不重新编译或签名。该参数目前只支持 macOS。省略它会进入原有构建与启动流程；`--smoke` 则运行独立的真实请求示例，会消耗提供商额度。

## 构建记录：通过类型检查到真正能显示 GUI

本机环境为 Apple M4 Pro、24 GiB 内存、macOS 15.5。起初只有 Command Line Tools；后续安装 Xcode 16.4、完成官方许可证与首次初始化，并补齐 Metal 工具链。选择 16.4 与系统兼容性有关，不能不检查宿主系统就直接安装任意最新版，具体约束见 [Apple Xcode 系统要求](https://developer.apple.com/xcode/system-requirements)。最终 GUI 使用仓库锁定的 Rust 1.92.0、Metal 32023.620 与 cargo-bundle 0.11.0。

### 空 Metal 库只能帮助检查类型

仓库基线已有 `WARP_LOCAL_SKIP_METAL` 分支，允许在缺少 Metal 时写入空的 metallib，使部分类型检查继续。这不是本次新增的优化，更不能产生可用的 GPU 构建。此前通过 `cargo check`，并没有证明图形窗口能正常渲染。

补齐工具链后，要清理相关缓存并取消这个环境变量，再做真实构建。本次重新构建 `warpui` 后生成了 31,233 字节的实际 Metal 库，并确认其字节嵌入最终可执行文件。这个检查回答的是“构建产物确实带了真实着色器库”，实际显示仍需运行验证。

```sh
# 曾使用跳过 Metal 的类型检查缓存时，先让相关 crate 重新构建
cargo clean -p warpui

env -u WARP_LOCAL_SKIP_METAL \
  RUSTUP_TOOLCHAIN=1.92.0 CARGO_BUILD_JOBS=6 FRAMEWORK_OVERRIDE=dev \
  cargo build -p warp --bin warp-oss --features gui --locked
```

### 将锁定编译和打包分开

首次打包尝试遇到 cargo-bundle 0.11.0 不接受 `--locked` 的问题。解决方式是先用 Cargo 完成锁定依赖的编译，再让打包阶段跳过构建。这个顺序还使后续报备和运行对应同一份二进制。

```sh
RUSTUP_TOOLCHAIN=1.92.0 CARGO_BUNDLE_SKIP_BUILD=1 \
  WARP_BIN_NAME=warp-oss WARP_CHANNEL=oss FEATURES=gui \
  GENERATE_SCHEMA=false SKIP_SETTINGS_SCHEMA=1 \
  ./script/macos/run --dont-open
```

当时要求先构建、暂不运行，因此除了 `--dont-open`，还关闭了会执行新程序的 schema 生成。cargo-bundle 初次生成的 DMG 早于脚本后续的资源复制、plist 更新与签名，所以需要在这些步骤完成后重新制作最终镜像，再用 `hdiutil verify` 检查完整性。

最终产物是 arm64 的 `WarpOss.app`，开发签名为 ad-hoc；`codesign --verify --deep --strict` 与 Info.plist 校验通过。主可执行文件 SHA-256 为：

```text
51fa14d5a63c9a9a4e838e5a3f0a46cd16f0240456810606643b17e867499e4d
```

这些属于产物与静态验证，不能代替原生输入、多轮和流式交互测试。

### 企业终端防护改变了验证顺序

早期独立驱动测试程序被企业终端防护阻止。最初曾把进程异常退出当作签名问题处理；收到明确的防护提示后，修正了判断，停止运行该测试程序并删除临时产物。后续先完成工具链、编译和静态检查，再由用户按正常流程报备 GUI，批准后只运行同一份已报备产物。

这段过程保留下来，是为了避免以后把“编译成功”“签名检查成功”和“允许执行”混成一个问题。更换路径、重新签名或关闭系统防护，都不是这次验证采用的解决方式。

## 验证结果：分别证明了什么

验证分为协议测试、真实接口测试、构建与原生 GUI 四层。各层回答不同问题，没有用一次成功请求替代整个应用的正确性。

| 验证层 | 已有结果 | 解释范围 |
| --- | --- | --- |
| 客户端定向测试 | `cargo nextest run -p warp_bailian_client` 的 15 项通过 | SSE 分片、历史转换、原生事件、401、截断、工具拒绝和密钥脱敏等 |
| 客户端静态检查 | 严格 Clippy 通过 | 客户端范围；不是整个工作区无告警 |
| 真实 Rust smoke | 两轮分别回复 `OK`、正确回忆 `42` | 同一客户端与事件适配真实连通；不是 GUI 测试 |
| GUI 编译与打包 | Rust 1.92.0、真实 Metal、arm64、静态签名和镜像检查通过 | macOS 这份产物；没有验证其他平台 |
| GUI 原生交互 | 输入、同会话两轮、流式增长完成 | 已报备同一哈希产物上的原生界面交互 |

客户端测试覆盖了跨分片 UTF-8、CRLF、多行 `data:`、事件大小上限、空 `choices` usage、最后停止片段附带正文、无工具请求、工具响应拒绝、输出超限、缺少 `[DONE]`、HTTP 401 不重试、缺少 key 不发请求，以及模拟 HTTP 到原生 `Init / Add / Append / Finished` 的转换。前期测试与静态检查使用 Rust 1.94.1；后续兼容性调整由锁定的 Rust 1.92.0 和完整 GUI 构建验证。没有在企业防护提示后重跑独立测试或 smoke，不能声称最终版本所有单测又重新执行了一遍。

应用严格 Clippy 曾被基线中的 `unnecessary_unwrap` 告警阻塞；仅在定向检查中放宽该项后通过，没有修改这些无关代码，也没有运行整个工作区 presubmit。独立会话驱动测试程序的执行被阻止，属于未完成项。

真实 smoke 两轮的首文本延迟约为 1.21 秒和 0.84 秒。这只是两次短请求的观察值，没有控制网络与服务负载，也没有对 `qwen3.8-max` 做同条件 A/B 测试，不能由此推导提速倍数或稳定延迟。

### 原生 GUI 中确实发生了增量显示

报备通过后，使用 `--no-build --from-dev1` 启动，启动前后主程序哈希一致。完成正常登录并打开 Agent 总开关后，在新建原生 Agent pane 中进行两轮测试：

1. 输入“请记住测试代号‘青竹-7319’。只回复：已记住。”，收到“已记住。”。
2. 第二轮不再提供代号，要求回忆并继续生成 20 条编号的终端使用提示。模型正确给出 `青竹-7319`，随后完成 20 条文本。

第二轮保留了生成中与完成后的本地截图。中间画面中第 17 条仍未完成，界面显示 `Warping...` 与停止控件；最终补齐第 20 条并出现完成标记。这个现象证明原生界面实际接收了增量内容，不只是代码里设置了 `stream: true`。截图包含个人工作环境信息，本文仅记录验证方法与结果，不公开原始桌面截图。

同时，对 GUI 主进程进行了 30 秒、约 0.25 秒间隔的 TCP 连接采样，其中两个采样点匹配百炼 Token Plan 主机解析出的 IPv6 地址与 443 端口。没有解密 TLS 或抓取请求载荷；应用也仍有其他 Warp 网络连接。连接采样与直连源码分支、运行时有效模型标签和原生响应共同佐证本次推理路径，不能证明整个应用不访问 Warp，也不能单独证明具体请求内容或账单归属。

本次两轮未被 `Out of credits` 阻止，但没有核对前后 Warp 账单计数。百炼自己的鉴权、套餐额度和模型权限仍有效。

## 已修改哪些模块，下一步还缺什么

为了让后续继续开发时可以找回设计依据，改动按责任记录如下。下载补丁包含这些实现、客户端测试和启动脚本；没有包含密钥、账户数据、截图或本机连接记录。

| 文件 / 模块 | 本次改动 | 后续扩展时需保留的约束 |
| --- | --- | --- |
| `Cargo.toml`、`Cargo.lock`、`app/Cargo.toml` | 注册独立客户端及依赖 | 保持锁定版本可构建 |
| `app/src/ai/direct_bailian.rs`、`mod.rs` | 显式进程开关、配置快照 | 默认路径不受影响，凭据不进入调试输出 |
| `agent/api.rs`、`agent/api/impl.rs` | 请求携带直连配置，提前选择后端 | 错误不隐式回退至 Warp |
| `crates/warp_bailian_client/` | 纯文本 HTTP、SSE、历史及原生事件 | 消息身份稳定，完整结束才成功 |
| `conversation_driver.rs`、`conversation.rs` 及驱动测试 | 新驱动、服务端身份与持久化边界 | 本地身份不冒充云端身份 |
| `controller.rs`、`response_stream.rs` | 路由约束、取消、禁止云端恢复 | 不混用旧会话、共享转发及自动重放 |
| `local_agent_task_sync_model.rs` | 按驱动判断是否上报状态 | 无服务端身份的会话不触发上报 |
| `request_usage_model.rs`、`prompt_alert.rs` | 显式模式下放行对应云端额度提示 | 不改变百炼鉴权，不等于关闭整个账户门禁 |
| `llms.rs` | 显示实际固定模型 | 下拉框暂不承诺切换直连模型 |
| `script/run-bailian` | 凭据来源、smoke、已有 GUI 启动 | `--no-build` 不改变已核验产物 |

补丁以本文固定提交为基线，可先用 `git apply --check` 检查是否适合自己的工作区，再决定应用。它是源码复现材料，不包含预编译应用或完整构建依赖：

```sh
git checkout 1e6c3ae465d6a925a9b97ae03877aaadd159b8f1
git apply --check /path/to/warp-bailian-direct-poc.patch
git apply /path/to/warp-bailian-direct-poc.patch
```

继续推进时，应先验证 GUI 点击取消、401/429、主动断网、超长上下文与不兼容历史的行为，再补齐会话驱动测试。文本后端稳定后，才适合把模型列表和可配置参数提取出来。工具调用需要重新设计输入上下文、工具 schema、审批、执行结果和多轮循环；持久化需要处理身份、恢复与重放；账户解耦则需要审计入口设置、同步和其他网络依赖。这些工作没有完成，也没有基于实际开发统计给出人日承诺。

当前 PoC 适合验证“自己的模型能否驱动 Warp 原生文本对话”，以及继续研究原生事件与会话状态。需要读写文件、执行命令、SSH 工作流、长任务恢复或跨设备同步时，仍应使用已有完整 Agent 路径，或在终端中运行具备这些能力的 CLI，不能把这次文本实验当作完整替代。
