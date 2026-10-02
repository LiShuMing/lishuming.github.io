---
title: "【调研】数据库性能优化如何形成自主闭环：Agent、Benchmark 与可信实验"
date: 2026-09-30T12:00:00+02:00
lastmod: 2026-09-30T12:00:00+02:00
slug: "dive-autonomous-database-performance-loop"
categories:
  - 数据库
tags:
  - 性能工程
  - 查询优化器
  - 执行引擎
  - LLM Agent
  - Benchmark
  - 实验平台
description: "讨论如何将数据库性能优化组织成可恢复、可审计、可证伪的自主实验闭环，系统分析 Agent 控制面、实验身份、幂等恢复、搜索策略、统计测量以及无状态查询与有状态增量维护的不同验证方法。"
toc: true
math: true
draft: false
---

> 本文讨论的是通用的数据库性能研究方法，而不是某个具体数据库的内部平台设计。公开项目与文档核对截至 2026-09-30；项目自述、固定版本源码事实、架构推演和本文观点会尽量分开。

## 引言：性能优化不是一句可以直接执行的目标

“让 Agent 持续优化数据库性能，直到它变快”为何听上去很自然，实现起来却很危险？

因为性能优化并不是一个可以直接交给模型求解的单一目标。一次看似简单的参数调整，背后至少包含五类彼此独立的问题：

1. **语义问题**：候选是否仍然产生正确结果？
2. **因果问题**：性能变化真的是候选造成的吗？
3. **测量问题**：差异是稳定收益，还是缓存、排队和共享资源噪声？
4. **搜索问题**：下一次实验应该探索哪个方向？
5. **系统问题**：提交超时、Worker 重启或回调重复以后，实验还能否恢复？

LLM 擅长读计划、关联源码、提出假设和改变搜索方向，但它不天然拥有可靠的时间、事务、幂等和统计语义。Benchmark 能产生数据，却不知道为什么做这个实验，也不知道结果是否覆盖了完整目标。工作流系统可以恢复任务，却无法替数据库工程师定义什么叫“正确”和“更好”。

因此，真正值得构建的不是一个无限运行的提示词循环，而是一套职责清楚的研究闭环：

```text
问题与约束
    │
    ▼
可证伪假设 ──► 候选配置/补丁 ──► 独立实验
    ▲                                  │
    │                                  ▼
研究记忆 ◄── 接受/拒绝/未知 ◄── 规范化观测与评测
```

这里最重要的词不是“自主”，而是“闭环”。自主只表示系统可以在有新证据时继续推进；闭环则要求每一步都有输入、状态、权限、输出和停止条件。

---

## 1. 先定义三个循环，而不是先选 Agent 框架

### 1.1 单轮工具循环

单轮工具循环发生在一个 Agent turn 内：阅读源码、搜索日志、执行允许的命令、生成配置或补丁、返回结构化提案。编码 Agent 已经较好地解决了这一层。

这一层的完成只表示：

- 工具调用结束；
- 进程返回成功；
- 结构化输出符合 Schema；
- 候选文件被生成。

它不表示候选正确，更不表示性能提升。`exit code = 0` 只是工具执行事实，不是研究结论。

### 1.2 实验循环

实验循环把一个长期研究目标拆成多个假设和候选：

```text
读取最新证据
   → 选择下一项最有信息量的动作
   → 登记候选与实验
   → 异步执行
   → 收集并独立评价
   → 决定继续、补测、回退或结束
```

这一层需要跨进程保存状态，也要跨越分钟乃至数天的外部任务。它才是“数据库性能自主优化”的核心。

### 1.3 Agent 方法改进循环

第三个循环研究的不是数据库候选，而是 Agent 自身：换一种提示模板、上下文压缩、工具集或搜索策略，是否能在同样预算下提出更多有效假设？

这个循环必须与线上性能任务隔离。否则 Agent 可以在结果不理想时修改自己的评分器、覆盖集合或停止条件，最终得到一个无法解释的“自我证明”。

| 循环 | 优化对象 | 权威状态 | 正确完成的含义 |
| --- | --- | --- | --- |
| 单轮工具循环 | 一次分析或实现动作 | AgentTurn | 工具和输出合同完成 |
| 实验循环 | 工作负载上的策略或实现 | Campaign、Trial、Evaluation | 在冻结政策下形成可复核结论 |
| 方法改进循环 | Agent 的提示、工具和搜索方法 | 独立任务集与评测器 | 同预算下可靠产出改善 |

第一个循环不能靠“永不退出”变成第二个循环；第三个循环也不能在一次性能任务中随意修改前两个循环的验收规则。

### 1.4 两种成功必须分开

**平台成功**是指系统无需人反复输入“继续”，可以等待外部任务、恢复故障、消费新结果并形成交付物。

**优化成功**是指候选在规定覆盖、正确性、资源和统计条件下优于基线。

一个 Campaign 最终得到“在当前预算和动作范围内没有发现可信改善”，可能是高质量的平台成功，却不是性能优化成功。承认这一点，才能避免系统为了证明自身价值而挑选有利样本。

---

## 2. 关键名词：一次实验究竟是什么

自主闭环最容易失败的地方，不是模型不够强，而是 `candidate`、`trial`、`run`、`retry` 等词在不同组件中代表不同对象。

### 2.1 Campaign、Hypothesis 与 Candidate

- **Campaign**：一个版本化的研究任务，包含目标、约束、允许动作、工作负载、预算与验收政策。
- **Hypothesis**：可以被观测证伪的解释。例如“Hash 聚合的状态压力导致 Spill，切换算法后峰值内存应下降”。
- **Candidate**：假设对应的具体实现，可以是配置、每条查询的策略映射、代码补丁或构建制品。

“把参数调小试试”不是完整假设，因为它没有说明预期观测和反证条件。一条可执行假设至少需要：

```json
{
  "statement": "状态表过大是当前聚合阶段 Spill 的主要原因",
  "prediction": [
    "目标算子发生改变",
    "峰值内存与 Spill bytes 下降",
    "端到端耗时在确认测量中改善"
  ],
  "falsification": [
    "算子改变但 Spill 不变",
    "Spill 下降但排序或网络代价抵消收益"
  ]
}
```

### 2.2 Trial、ExecutionAttempt 与 Replicate

这三个对象不能合并：

| 对象 | 含义 | 为什么需要独立身份 |
| --- | --- | --- |
| Trial | 在明确候选、场景和测量政策下的一次逻辑实验 | 决定要回答哪个研究问题 |
| ExecutionAttempt | Trial 的一次基础设施执行尝试 | 网络失败或机器故障可以重试，但不是新样本 |
| Replicate | 预先计划的一次独立统计重复 | 用于估计噪声，不能被候选去重删除 |

如果第一次提交成功但响应丢失，重发相同请求是传输重试，不应凭空产生第二个统计样本。如果研究者为了估计方差有意再测一次，则必须创建新的 replicate。把两者都叫 `retry`，会同时破坏幂等性和统计分析。

### 2.3 Observation、Evaluation 与 Promotion

**Observation** 是观测事实：实际配置、计划、耗时、内存、Spill、错误、环境和原始产物。

**Evaluation** 是依据冻结政策对一组 Observation 作出的判定：`ACCEPT`、`REJECT`、`INCONCLUSIVE` 或 `INVALID`。

**Promotion** 是把通过评价的候选提升为“当前最佳已验证候选”。它与把候选发布到生产环境是两种不同动作。

正确性失败可以是一条有效 Observation：实验成功证明候选不可接受。相反，结果文件丢失、环境身份不明或覆盖不完整，通常意味着 Observation 无效或证据不足，而不是候选性能差。

### 2.4 Harness、Agent Runtime 与 Control Plane

- **Agent Runtime** 提供模型、工具调用、文件与命令执行能力。
- **Harness** 组织单次或多次 Agent 执行的上下文、权限和输出合同。
- **Control Plane** 保存长期目标、预算、事件、所有权、状态转移和恢复信息。
- **Benchmark Runner** 运行已定义的工作负载并产生观测。
- **Evaluator** 根据受控政策判断证据，不接受候选 Agent 临时改写标准。

模型接口不等于编码 Agent，编码 Agent 也不等于性能实验平台。直接调用 LLM API 时，文件工具、命令执行、上下文压缩、会话恢复和权限治理仍需要平台实现。

### 2.5 Workload Snapshot 与 Environment Fingerprint

性能数字只有放在完整身份中才有意义：

```text
workload = 查询/变更序列 + 数据快照 + 统计信息 + 执行日程
environment = 引擎制品 + 配置 + 资源池 + 硬件 + 采集器版本
```

Workload Snapshot 表达“测了什么”，Environment Fingerprint 表达“在哪里、用什么版本测”。指纹一致是可比性的必要记录，却不是无噪声的保证；共享集群干扰、温度、缓存和后台任务仍可能没有被完整观测。

---

## 3. 开源项目提供了哪些拼图

下面的项目解决不同层次的问题，不宜放在一张“谁最智能”的排行榜中。

### 3.1 Code Factory：把长期 Agent 工作变成可见产品

[Code Factory](https://github.com/luohaha/code-factory)的公开定位是面向 Agent 驱动软件交付的本地控制面。Requirement、长生命周期 Session、多次 Run、对话、定时器和外部事件被持久化，用户可以观察、纠正和中断任务。

它最值得性能平台借鉴的是：

- Agent 会话不是业务任务本身；
- 一次任务可以跨多个 Run 继续；
- 新消息按顺序进入下一轮，而不是污染正在执行的轮次；
- 页面展示、事件流和任务事实由同一持久状态支撑。

但软件交付中的 PR 状态不能直接代替性能研究中的基线、覆盖、噪声和正确性。其公开安全说明还明确指出，无界面 Agent 可能继承启动用户的文件、网络与命令权限，工作区隔离不能只依赖提示词。这个边界对任何后台 Agent 都成立。

### 3.2 GoaLoop：最小循环为何能够成立

[GoaLoop](https://github.com/luohaha/GoaLoop)展示了一种很小的目标驱动循环：目标文件定义完成条件，后台 orchestrator 启动新的 Runner，Runner 验证当前状态、推进一步并记录结果，直到验证通过或停止。

它说明最小循环只需要：明确目标、一次有界执行、可读取历史、验证和继续/停止决策。它也暴露了数据库性能场景的额外需求：如果“是否通过”来自同一个 Agent 的自报，而不是外部测量和独立 evaluator，循环很容易把合理解释误当成成功。

### 3.3 LoopX：长期目标需要独立控制状态

[LoopX](https://github.com/loopx-project/loopx)当前把自己定义为 long-horizon agents 的本地控制面，而不是新的 Agent runtime。它强调 goal、todo、gate、evidence、quota、bounded continuation 与 recoverable handoff；运行时执行一次工作，控制面决定是否还有权限与配额继续。

这个分层很适合性能研究：Agent 可以生成下一步建议，控制器仍要检查状态版本、预算和证据，再决定是否提交昂贵实验。浏览器、聊天会话或模型上下文都不应成为任务的唯一事实源。

### 3.4 Raven-Oncall：昂贵外部任务的提案—执行模式

[Raven](https://github.com/EverMind-AI/Raven)覆盖 Agent 宿主、多 Agent 编排、Oncall 长任务和方法演进。对性能实验最有启发的是 Oncall 风格的外部任务合同：Proposer 根据历史提出 Trial，Campaign 协调执行，JobBackend 提交和查询外部任务，Ledger 保存状态与结果，完成结果再进入下一轮历史。

这个模型接近数据库 Benchmark，因为真实执行通常昂贵、异步且不由模型进程完成。不过，通用外部任务框架通常仍缺少数据库领域中的：

- 计划是否真正变化；
- 配置是否实际生效；
- SQL 结果是否正确；
- 多查询覆盖是否完整；
- 有状态维护是否处于相同逻辑快照；
- 局部最优能否组合成整个工作负载的改进。

因此，适合复用的是外部任务合同和恢复思想，不是把一个通用 `score` 直接当成数据库评价。

### 3.5 Temporal、Optuna 与 MLflow 各自站在哪一层

| 工具 | 擅长的问题 | 不能替代的部分 |
| --- | --- | --- |
| [Temporal](https://docs.temporal.io/activity-definition) | 持久工作流、等待、Activity 重试与恢复 | 外部副作用仍需幂等；不定义数据库候选是否正确 |
| [Optuna](https://optuna.readthedocs.io/en/stable/tutorial/20_recipes/009_ask_and_tell.html) | 参数建议、ask-and-tell、剪枝与结构化搜索 | 不应接收覆盖不同或伪造的单一分数 |
| [MLflow Tracking](https://mlflow.org/docs/latest/ml/tracking/) | Run、参数、指标和制品的记录与展示 | 不应成为业务状态机与候选晋升的唯一权威 |

它们是可选拼图，不是第一版必须同时引入的技术清单。只有当持久调度、参数搜索或实验展示成为真实瓶颈时，增加对应组件才有意义。

---

## 4. 从调研中得到的五条架构原则

### 4.1 长期任务状态不能寄存在 Agent 会话里

模型上下文会压缩，会话可能丢失，Runner 可能升级。Campaign 的目标、预算、候选、实验、证据和未解决问题必须可以在没有原会话的情况下重建。

Agent 会话适合保存局部探索连续性；平台状态负责长期事实。恢复会话是一种优化，不是正确性前提。

### 4.2 提案、执行和评价必须分离

```text
Agent / Searcher           Controller             Evaluator
为什么值得试？     →     是否允许执行？     →     证据支持什么？
```

Agent 可以质疑评价政策并提出修改建议，但不能在同一任务中修改通过条件。Controller 检查权限、预算和状态版本；Evaluator 只消费真实观测和固定政策。

### 4.3 数据库中只能有一个任务事实源

本地 Ledger、队列、工作流历史和实验展示工具都可以存在，但只能有一个组件权威地决定 Trial、预算与 Promotion。否则控制器可能认为实验失败并重提，而外部协调器已经把它标记成功；两个“最佳候选”也可能在页面和报告中分叉。

### 4.4 事件驱动优于 Agent 忙等

外部实验运行一小时，并不需要模型每分钟询问状态。模型应在新 Observation、用户指令、依赖恢复或受控定时唤醒到来时才继续。

等待属于控制器和任务系统；分析属于 Agent。把二者分开，既降低成本，也让“没有新证据”成为合法静默状态。

### 4.5 Unknown 是一种必要状态

提交请求超时后，我们可能不知道任务是否创建；计划采集缺失时，我们可能不知道候选是否生效；区间跨过接纳边界时，我们可能不知道收益是否稳定。

成熟系统不会强迫所有状态立即二值化。`SUBMISSION_UNKNOWN`、`INCONCLUSIVE` 和 `evidence_missing` 是防止伪造确定性的安全阀。

---

## 5. 一套最小但完整的架构

```text
┌─────────────────────────────────────────────────────────────┐
│                    Task API / Research UI                   │
│  目标、预算、当前结论、实验矩阵、事件、人工输入、交付物      │
└──────────────────────────────┬──────────────────────────────┘
                               │
┌──────────────────────────────▼──────────────────────────────┐
│                     Campaign Controller                     │
│    状态机、版本、预算、租约、Outbox、继续/暂停/停止决策       │
└──────────────┬───────────────────────────────┬──────────────┘
               │                               │
┌──────────────▼──────────────┐  ┌────────────▼───────────────┐
│ Context Builder + Agent     │  │ Domain Pack + Evaluator    │
│ 读取证据、提出假设和候选      │  │ 合法性、实验编译、独立验收    │
└──────────────┬──────────────┘  └────────────┬───────────────┘
               │                               │
               └──────────────┬────────────────┘
                              │
┌─────────────────────────────▼───────────────────────────────┐
│         Build / Benchmark / Profile / Result Adapters       │
│    submit、lookup、status、collect、cancel、artifact refs     │
└─────────────────────────────┬───────────────────────────────┘
                              │
┌─────────────────────────────▼───────────────────────────────┐
│              Relational Store + Artifact Store              │
│  任务事实、事件、评价、预算；计划/Profile/日志/补丁不可变引用  │
└─────────────────────────────────────────────────────────────┘
```

第一版可以是模块化单体：一个 API 服务、后台控制器、Agent Worker、Benchmark Worker、关系数据库和制品目录。模块边界要先清楚，进程数量不必一开始就复杂化。

### 5.1 四种权威

| 问题 | 权威组件 | 其他组件能做什么 |
| --- | --- | --- |
| 目标、预算、允许动作是什么 | GoalVersion / Campaign | Agent 可以建议，用户显式修改 |
| 外部任务实际处于什么状态 | Benchmark 系统 | 平台同步并标记未知或过期 |
| 候选是否正确、是否改善 | Evaluation Policy / Evaluator | Agent 可以解释和申请补测 |
| 是否发布到生产 | 既有发布流程与人 | 研究平台只交付证据和候选 |

“采用为任务内最佳候选”与“上线”必须分开。前者是研究判断，后者涉及更广的回归、责任和变更治理。

---

## 6. 数据模型：让结论能够重放

### 6.1 核心实体

| 实体 | 关键字段 | 主要写入者 |
| --- | --- | --- |
| Campaign | owner、status、goal_version、state_version、budget | 用户与 Controller |
| GoalVersion | objective、constraints、allowed_actions、evaluation_policy | 受控任务接口 |
| WorkloadSnapshot | cases、data、stats、schedule、environment refs | Domain Pack |
| Hypothesis | statement、prediction、falsification、evidence refs | Agent 提案，平台登记 |
| Candidate | parent、kind、content digest、artifact ref、scope | Candidate Registry |
| Trial | candidate、scenario、replicate、measurement policy、state | Controller |
| ExecutionAttempt | attempt_no、execution_key、external job、failure class、cost | Execution Adapter |
| Observation | metrics、correctness、effective config、plan、environment | Result Collector |
| Evaluation | policy version、coverage、verdict、reason、uncertainty | Evaluator |
| AgentTurn | input manifest、runtime、proposal、usage、outcome | Agent Runner |
| Event | sequence、type、causation、payload、goal version | Controller / Inbox |
| Promotion | candidate、evaluation、effective scope | Controller |

Candidate 与 Trial 是一对多。一份配置可以在多个场景、多个环境或多个 replicate 中测试。Trial 与 ExecutionAttempt 也是一对多，因为基础设施失败可能要求新的执行尝试。

### 6.2 四种身份不要压成一个哈希

| 身份 | 回答的问题 | 重复规则 |
| --- | --- | --- |
| Candidate digest | 配置、补丁或制品内容是否相同 | 内容相同可复用候选对象 |
| Workload/environment fingerprint | 两次观测是否处在可比较条件 | 变化时分层或重建对照 |
| Trial + replicate_id | 是否是一项新的计划测量 | 合法统计重复必须保留 |
| execution_key | 是否在重放同一次外部提交 | 网络重发复用，确认失败后才新建 attempt |

“配置内容相同”不代表测量应被去重。相同候选在不同日期、数据快照或资源池上的实验仍然是不同证据。

### 6.3 Observation 不应只保存一个 score

```json
{
  "schema_version": "observation_v1",
  "trial_id": "trial-042-r2",
  "attempt_id": "attempt-042-1",
  "case_id": "query-A",
  "candidate_digest": "sha256:EXAMPLE",
  "workload_fingerprint": "workload-v3",
  "environment_fingerprint": "engine-and-resource-v7",
  "execution_status": "succeeded",
  "correctness": {
    "status": "pass",
    "evidence_ref": "artifact://result-check/query-A"
  },
  "metrics": {
    "execution_wall_ms": {"value": 12500, "unit": "ms"},
    "queue_wait_ms": {"value": 800, "unit": "ms"},
    "peak_memory_bytes": {"value": null, "status": "missing"},
    "spill_bytes": {"value": 1048576, "unit": "bytes"}
  },
  "effective_config_ref": "artifact://effective-config/query-A",
  "plan_ref": "artifact://plan/query-A",
  "raw_result_ref": "artifact://raw/query-A"
}
```

缺失指标应保留 `missing`，不能填成 0；超时只有下界时应保留 censored 状态，不能伪装成精确耗时。原始产物不可变保存，规范化结果携带采集器版本。

---

## 7. 用 Domain Pack 表达不同优化场景

“通用平台”不意味着所有工作负载都被压成一个浮点数。通用的是生命周期、身份、预算、证据和恢复合同；场景语义由版本化 Domain Pack 定义。

### 7.1 Domain Pack 的六部分

1. **Manifest**：场景名称、支持动作、参数 Schema 和资源需求。
2. **Context**：如何把计划、Profile、源码与历史组织给 Agent。
3. **Candidate**：候选合法性、依赖、作用域和是否需要构建。
4. **Execution**：如何编译 Trial、提交 Benchmark 和恢复环境。
5. **Evaluation**：正确性、指标单位、覆盖、回归与结果分类。
6. **Knowledge**：常见机制、失败边界和经验适用范围。

接口可以保持很小：

```python
class DomainPack:
    def build_context(self, campaign, evidence_index) -> dict: ...
    def validate_candidate(self, candidate, goal) -> dict: ...
    def compile_trials(self, proposal, workload_snapshot) -> list: ...
    def normalize_result(self, raw_result) -> dict: ...
    def evaluate(self, observations, frozen_policy) -> dict: ...

class BenchmarkAdapter:
    def submit(self, request, execution_key) -> str: ...
    def lookup(self, execution_key) -> dict: ...
    def status(self, job_id) -> dict: ...
    def collect(self, job_id) -> dict: ...
    def cancel(self, job_id) -> dict: ...
```

`lookup` 很重要：提交响应丢失后，系统必须先确认任务是否已经创建。如果后端无法按幂等键查找，可以用可搜索标签或提交网关补足；仍然无法确认时，应进入未知状态并停止盲目重提。

### 7.2 可支持的场景

| Domain Pack | 候选 | 关键验收 |
| --- | --- | --- |
| query_strategy_tuning | 全局参数、每查询映射、特征规则 | 全集覆盖、配置生效、组合工作负载回归 |
| incremental_view_maintenance | 刷新参数、维护策略、候选实现 | 快照正确、新鲜度、状态历史、回退路径 |
| optimizer_rule_validation | 规则开关、代价阈值、代码补丁 | 语义正确、规则命中、计划变化、运行收益 |
| resource_policy_tuning | 并发、内存、并行度和队列策略 | 延迟、吞吐、资源成本与公平性 |
| source_diagnosis | 源码假设和验证计划 | 区分事实与推断；无运行证据不声称收益 |

一个无状态查询 Trial 可以是一次运行；一个增量视图 Trial 可能包含初始化、重放变更、刷新、检查点验证和清理。核心状态机无需知道每个步骤的数据库语义，但插件必须声明哪些步骤可重试、哪些副作用不可重复。

---

## 8. Agent 在闭环中的正确位置

### 8.1 Agent 负责提出“下一步最值得知道什么”

Agent 适合：

- 解释计划与 Profile 的异常；
- 从源码定位配置、规则和执行路径；
- 把自然语言猜想改写为可证伪假设；
- 构造候选配置或受限补丁；
- 根据失败结果改变搜索方向；
- 判断当前证据还缺少哪个反例。

Agent 不应该直接拥有：

- 修改验收政策的权限；
- 绕过预算提交实验的权限；
- 删除失败观测的权限；
- 把候选标为已发布的权限；
- 长期轮询外部任务的职责。

### 8.2 每轮上下文应该是 manifest，而不是聊天历史

Context Manifest 至少包含：

- 当前 GoalVersion 与允许动作；
- Campaign 状态版本；
- 基线和当前最佳已验证候选；
- 新增 Observation 与 Evaluation；
- 已否定假设和仍未知的问题；
- 剩余预算；
- 相关源码、计划和制品引用；
- 本轮必须回答的问题。

完整日志保持可检索，不应每轮塞进上下文。压缩结论必须保留证据引用和适用范围。这样即使原会话丢失，也能重建一次新的有界执行。

### 8.3 结构化提案仍然需要语义校验

```json
{
  "schema_version": "performance_proposal_v1",
  "campaign_id": "campaign-demo",
  "goal_version": 3,
  "based_on_state_version": 18,
  "action": "run_experiments",
  "hypothesis_id": "hypothesis-07",
  "evidence_refs": ["observation-104/profile"],
  "candidate_ref": "candidate-sort-02",
  "case_ids": ["query-A", "query-B"],
  "expected_observation": "目标算子改变，Spill 与耗时下降",
  "falsification": "Spill 下降但端到端收益不足，则拒绝该解释"
}
```

JSON 通过 Schema 只说明语法正确。平台还要检查 case 是否存在、参数是否合法、配置依赖是否满足、证据引用是否存在、状态版本是否过期以及预算是否足够。

### 8.4 权限边界必须由运行时实现

“你是只读分析员”写在提示词中，不会阻止进程写文件或访问网络。真正的边界来自：

- 隔离工作区；
- 明确的只读/可写路径；
- 独立的 Benchmark 提交身份；
- evaluator 和原始证据不可被候选修改；
- 凭据不进入提示与可下载产物；
- 源码、日志和网页中的文字都作为不可信数据，而不是指令。

源码候选的路径应是：隔离工作区 → 补丁检查 → 语义测试 → 构建 → 制品登记 → Benchmark。工作区中改了代码却测到旧制品，是自动优化平台最隐蔽也最常见的身份错误之一。

---

## 9. 可靠循环：状态机、幂等和恢复

### 9.1 两层状态机

Campaign 管理研究生命周期：

```text
CREATED → ACTIVE → PAUSING → PAUSED
                ├──────────→ BLOCKED
                ├──────────→ COMPLETED
                ├──────────→ EXHAUSTED
                └──────────→ CANCELLED
```

Trial 管理一次逻辑实验：

```text
PLANNED → RESERVED → SUBMITTING → QUEUED → RUNNING
                        │                     │
                        └→ SUBMISSION_UNKNOWN │
                                              ▼
                              COLLECTING → VALIDATING
                                      ├→ SUCCEEDED
                                      ├→ FAILED
                                      └→ INVALID
```

Trial 的 `SUCCEEDED` 只表示执行完成并形成合法观测。它是否改善性能，由 Evaluation 单独判断。

### 9.2 一轮控制过程

1. Controller 获取 Campaign 的短租约，读取状态版本与未消费事件。
2. 优先处理新实验结果、暂停请求和目标变化。
3. 如果没有新证据和可执行动作，不调用模型。
4. 固化 Context Manifest 和 AgentTurn，释放数据库长事务。
5. Agent 返回提案后，再次校验 GoalVersion 与 state_version。
6. 在同一事务中登记 Candidate、Trial、预算预留和 Outbox 事件。
7. Worker 消费 Outbox，用 execution key 查找或提交外部任务。
8. Collector 规范化结果，Evaluator 写入判定并触发下一轮。

Outbox 解决“数据库已写入 Trial，但队列消息没有发出”的原子性缺口。它不能让外部 Benchmark 自动 exactly-once；跨系统副作用仍依赖后端幂等、查询确认和未知状态处理。

### 9.3 为什么幂等不是一个布尔属性

假设 `submit()` 超时，有三种可能：

1. 请求没有到达后端；
2. 后端创建了任务，但响应丢失；
3. 后端创建任务后自身状态尚未可查。

客户端本地没有足够信息判断是哪一种。如果立即换一个新 key 重试，就可能制造重复昂贵任务；如果永远不重试，又可能丢掉实验。因此更稳妥的顺序是：按原 key 查询 → 在有界窗口内 reconcile → 仍无法确认则进入 `SUBMISSION_UNKNOWN` → 人工或更强的后端查询介入。

[Temporal 的 Activity 文档](https://docs.temporal.io/activity-definition)也强调 Activity 应设计为幂等。工作流引擎可以重新调度代码，却不能替外部系统凭空制造幂等语义。

### 9.4 租约、Fencing Token 与外部副作用

Worker 租约到期后，旧 Worker 可能仍在运行。新 Worker 接管时，数据库可以用递增 fencing token 拒绝旧写入；但只有外部提交网关也检查 token 或幂等键，才能阻止旧 Worker 再次触发外部副作用。

这说明：本地“只有一个 owner”与跨系统 exactly-once 是两回事。可靠设计不轻易承诺 exactly-once，而是明确哪些操作幂等、哪些可查询、哪些必须人工核对。

### 9.5 暂停、取消与目标修改

- **暂停**：停止新提交，默认允许在途任务完成并保留结果。
- **取消**：主动终止可取消任务，不再继续研究。
- **修改目标**：生成新的 GoalVersion；旧结果保留，但按新政策重新评价后才能晋升。

控制命令由 Controller 直接处理，不必等 Agent 在下一轮读到。否则一个等待中的模型会话就变成系统控制面的单点故障。

---

## 10. 搜索策略：语义探索与数值搜索如何配合

### 10.1 Agent 不是贝叶斯优化器，搜索器也不懂源码

Agent 擅长发现新的变量、解释非线性交互和构造反例；网格、随机或贝叶斯搜索擅长在稳定的结构化空间内系统取样。二者可以通过统一接口组合：

```text
suggest(context, history, budget) -> candidates
observe(evaluations)              -> updated search state
```

第一阶段可以让 Agent 提出小而合法的参数空间，再由确定性枚举验证主要因素。历史足够以后，使用 [Optuna ask-and-tell](https://optuna.readthedocs.io/en/stable/tutorial/20_recipes/009_ask_and_tell.html)一类接口把建议器和昂贵外部执行解耦。

### 10.2 什么时候不应该继续增加实验

| 当前证据 | 更合理的下一步 |
| --- | --- |
| 配置未生效、计划未变化 | 查作用域、依赖和代码读取路径 |
| 局部收益清楚但覆盖不足 | 扩展同类查询与反例 |
| 结果波动大且接近阈值 | 在预设预算内补充配对测量 |
| 两个参数单独有效、组合退化 | 检查交互、资源峰值和计划变化 |
| 有状态场景小批有效、大批退化 | 测量转换边界与回退路径 |
| 现有参数无法表达有效策略 | 形成源码问题；有权限时再生成补丁 |
| 多轮没有新增证据 | 切换假设或停止 |

重复改写总结不算研究进展。新增证据、排除假设、缩小边界或明确停止，才是一次有效轮次。

### 10.3 局部最优不等于组合最优

每条查询单独最快的配置，组合后可能提高并发内存峰值、网络争用或 makespan。多个物化视图各自最省资源的刷新计划，也可能在共享源表和存储上相互冲突。

因此，候选可以先按 case 筛选，但 Promotion 必须回到任务定义的真实组合方式。把单查询耗时简单相加，不能替代并发工作负载的真实测量。

---

## 11. 性能评价：如何避免优化出一个统计幻觉

### 11.1 先冻结问题，再测量

Campaign 开始时冻结：

- 主目标与硬约束；
- 必需 case 和覆盖定义；
- 正确性判据；
- 超时、OOM 与缺失结果的处理；
- 环境分层；
- replicate 和停止政策；
- 最小有意义收益。

探索过程中可以换候选、换假设，但不能因为结果不好就删除退化查询、修改分母或切换指标。确需改变目标时，应创建新版本并说明旧实验如何解释。

### 11.2 原始基线与当前最佳基线

- **B0**：Campaign 启动时冻结的原始基线，用于报告净收益。
- **B\***：当前最佳已验证候选，用于判断边际改善。

候选相对 B\* 有改善，不代表它仍满足相对 B0 的所有资源和回归约束。不同环境下多轮小收益也不能机械相乘。

### 11.3 多查询指标不能只剩一个平均数

令第 (i) 条查询的基线与候选耗时为 (B_i) 和 (C_i)。加权几何平均加速比可作为辅助指标：

$$
S_{geo}=\exp\left(\sum_i w_i\log(B_i/C_i)\right),\qquad \sum_i w_i=1
$$

但它不等于整套 wall time，也会隐藏关键查询的严重回归。若目标是串行总时长，可以报告：

$$
S_{sum}=\frac{\sum_i B_i}{\sum_i C_i}
$$

若目标是并发 makespan，则必须测真实并发日程，不能用前两种公式替代。完整报告至少包含：主指标、每 case 比值、最差回归、关键约束、资源变化和覆盖率。

### 11.4 噪声、配对与选择偏差

数据库实验常受缓存、共享资源、编译、排队和后台负载影响。对接近接纳边界的候选，可以在相同资源块中交替或随机安排基线与候选，再使用配对比值或块级 bootstrap 估计不确定性。

固定“每个候选跑三次”并不是通用统计保证。样本是否独立、噪声多大、最小收益是多少，都会改变需要的测量量。多次尝试后只挑最小值会产生 winner's curse，因此最终候选需要未参与选择的新测量确认。

### 11.5 失败与缺失如何进入评价

| 情况 | 合理处理 |
| --- | --- |
| 临时机器或服务故障 | 记录 infrastructure failure，按政策重试并记成本 |
| 候选真实 OOM、超时、错误结果 | 按固定规则拒绝或标为受限，不能移出分母 |
| 缺少 Profile 但结果与正确性可信 | 可保留性能结论，根因解释受限 |
| 缺少正确性或生效配置证明 | 标记无效或证据不足 |
| 超时只有耗时下界 | 保存 censored observation |
| 引擎、容量或数据发生漂移 | 新环境分层或重建对照 |

无法区分候选错误与基础设施异常时，保留 `UNKNOWN`，不要向搜索器输入一个伪造的惩罚分数。

---

## 12. 场景一：从 Hash/Sort 策略到工作负载级结论

考虑一个分析型查询集，我们想研究某类 Hash 与 Sort 实现如何选择。这里的“Hash/Sort”必须先绑定具体算子：Join、Aggregation、Distinct 或 Shuffle partitioning 的成本结构并不相同。

### 12.1 候选的四个层级

| 层级 | 交付内容 | 可以声称什么 |
| --- | --- | --- |
| 全局配置 | 全部查询采用相同策略 | 在当前工作负载中是否有更好默认值 |
| 每查询映射 | query/template → 配置 | 固定工作负载是否能进一步提速 |
| 特征规则 | 根据可得计划特征选择策略 | 是否可能推广到未参与搜索的查询 |
| 引擎实现 | 代价模型、算子或运行时修改 | 是否形成更通用、可维护的能力 |

每查询映射是合法的工程交付，但它的适用范围就是固定工作负载。若要声称策略具有泛化性，必须使用搜索阶段未见过的模板或参数变体，并确保特征在决策时可获得，而不是偷看运行后信息。

### 12.2 一条可审阅的实验路径

1. 固定引擎、数据、统计信息、资源和查询日程。
2. 从基线识别耗时贡献、Spill、倾斜、估计偏差和资源峰值异常。
3. 按执行机制分组，先选少量正例与反例。
4. 验证参数真实生效、目标算子和计划确实改变。
5. 观察峰值内存、Spill、排序、网络与端到端耗时，而非只看一个局部指标。
6. 对有希望的候选扩展覆盖，检查交互和最差回归。
7. 在原定组合/并发方式下运行完整候选。
8. 用独立新测量确认并交付适用边界。

如果 Sort 降低了 Hash 状态压力，却增加排序与网络开销，那么“Spill 下降”只支持局部机制判断，不能支持最终性能结论。

### 12.3 为什么小规模筛选不能替代目标规模

Hash 表容量、Spill 阈值、倾斜和网络拥塞都有明显的规模效应。小数据上的赢家可能在目标规模进入完全不同的执行区间。便宜的低保真场景可以用于筛选，但 Promotion 必须回到任务规定的规模和覆盖。

---

## 13. 场景二：增量物化视图为何是另一类实验

增量物化视图不是“把一条查询多跑几次”。它的结果依赖初始状态、变更顺序、事务边界、刷新日程和长期维护历史。

### 13.1 有状态实验的最小身份

```text
(D0,
 view_definition,
 delta_sequence,
 transaction_schedule,
 refresh_schedule,
 query_checkpoints,
 horizon)
```

其中 `D0` 是初始逻辑数据，`delta_sequence` 是有序变更，`horizon` 是观察窗口。是否计入初始构建和状态准备成本必须提前声明，不能在候选之间使用不同口径。

### 13.2 必须同时记录的维度

| 维度 | 需要记录 | 常见错误结论 |
| --- | --- | --- |
| 变更模式 | 插入、删除、更新、分布和事务边界 | 插入有效就声称所有增量模式有效 |
| 规模 | 基表量、增量比例、热点和分区变化 | 小批次优势直接外推到大批次 |
| 计算路径 | 纯增量、部分重算、全量回退 | 刷新成功就认为增量路径成功 |
| 状态历史 | 状态大小、累计次数、整理和清理 | 一次空状态刷新代表长期成本 |
| 一致性 | 提交边界、可见版本、验证时间点 | 比较两个不同快照的结果 |
| 新鲜度 | 提交到可查询的延迟 | 通过少刷新制造表面成本下降 |

缺少可观测性时应报告 Unknown，不能由 Agent 猜测回退原因。

### 13.3 正确性与恢复

每个检查点都应在相同逻辑提交边界上，将增量结果与参考结果比较。验证器必须明确重复行、NULL、类型、排序和数值容差语义。若参考查询也可能被优化器重写到待验证物化视图，就会形成循环验证，需要显式关闭该路径或采用独立参考。

候选使用不同私有状态格式时，应从同一逻辑 `D0` 各自初始化，不能直接复制另一候选的内部状态。中途失败后，读指标可以重试；已经提交的非幂等变更不能无条件重放。无法确认提交边界时，重建隔离环境比盲目继续更安全。

### 13.4 单次刷新更快并不代表长期更好

主目标可以是观察窗口内总刷新耗时或总资源成本，但还要限制：

- 正确性；
- 分位与最坏刷新延迟；
- 新鲜度 SLA；
- 状态占用；
- 全量回退频率；
- 清理与维护成本。

当时间、CPU、存储和金钱没有统一权重时，保留指标向量比随意相加更诚实。

---

## 14. 证据和研究记忆

### 14.1 证据不是单调增加的置信分

可以区分四种证据：

- **SOURCE_SUPPORTED**：源码或文档支持机制判断；
- **PLAN_CONFIRMED**：计划或实际配置证明候选生效；
- **MEASURED**：产生了合法运行观测；
- **VALIDATED**：满足任务的确认政策。

它们不一定严格逐级。例如运行时调度策略可能没有明显计划变化，却有真实测量收益；纯源码诊断任务也可能在第一层完成。报告应展示证据类型，而不是强行压成一个 0–100 的“可信度”。

### 14.2 失败经验也要可检索

一条跨任务经验至少记录：

- 适用的引擎与工作负载版本；
- 采取的动作；
- 观测与评价；
- 失败边界；
- 证据引用；
- 未来何时值得重新验证。

如果知识库只保存成功故事，Agent 会反复踩过被摘要删除的坑。新版本或数据分布明显变化时，旧经验应降级为“待复验”，而不是无限期传播。

### 14.3 研究记录不是模型思维链

每轮保存的应是可审阅决策：当前问题、引用证据、可选动作、选择、预期观测、反证条件、成本估计与最终判断。工具调用、补丁、实验和评测才是执行证据；平台不需要保存或展示模型的内部推理过程。

---

## 15. 如何从 MVP 开始，而不是先造一座大平台

### 15.1 第一条闭环的最小范围

一个可用 MVP 只需要：

- 一个版本化 Campaign；
- 一个受限 Domain Pack；
- 一个 Agent Runner；
- 一个 Benchmark Adapter；
- 一个独立 Evaluator；
- 一个关系数据库和不可变制品目录；
- 一页能看到目标、预算、候选、实验和证据的 UI。

首个演示不应追求惊人的加速比，而应证明：系统读取真实基线，提出两个有限候选，自主完成筛选，根据结果补测或停止，并在 Controller 重启后接回同一外部任务。

### 15.2 分阶段验收

| 阶段 | 交付 | 通过条件 |
| --- | --- | --- |
| P0 接口打通 | Proposal、Job、Observation、Evaluation 合同 | 提案—提交—取结果—再提案无需人工输入“继续” |
| P1 可信闭环 | 状态、预算、版本、独立验收、恢复 | 加速、退化、错误和无效结果分类正确 |
| P2 场景复用 | 无状态查询与有状态维护两个插件 | 新场景不修改循环核心 |
| P3 团队使用 | 多任务隔离、页面、权限和交付 | 任务互不污染，成本与恢复可解释 |
| P4 搜索增强 | 参数建议、经验检索、源码候选 | 同预算下有效产出确实改善 |

### 15.3 必须演练的失败

- 提交成功但响应丢失；
- 回调重复或乱序；
- Controller 和 Worker 重启；
- Agent 输出非法 JSON 或过期状态版本；
- 候选构建失败；
- 目标修改与旧实验同时完成；
- 预算耗尽；
- 取消与完成竞争；
- 有状态变更不确定是否提交。

故障演练比再增加一个 Agent 更能决定系统是否可信。

### 15.4 用什么指标评价平台本身

平台质量可以看：

- 最终独立复测通过率；
- 错误接纳与回归漏检数；
- 每个有效结论的模型和实验成本；
- 重复外部提交数量；
- 任务恢复成功率；
- 需要人工介入的次数与原因；
- 与简单枚举或人工流程在相同预算下的有效产出差异。

补丁数量、对话轮数和报告长度不应成为主要指标。

---

## 16. 进一步思考：数据库性能 Agent 的本质是什么

### 16.1 它首先是实验设计者，其次才是代码生成器

数据库优化的困难往往不在“写出另一段代码”，而在辨认什么值得改、怎样观察、哪些反例会推翻假设。一个只会不断修改代码的 Agent，会快速扩大搜索空间；一个能主动要求验证配置生效、补测边界和停止无效方向的 Agent，才真正接近性能工程师。

### 16.2 自主性的上限由观测质量决定

如果系统只有总耗时，没有实际配置、计划、Profile、资源和正确性证据，Agent 再聪明也只能在一个低信息环境中试错。与其先增加模型轮次，不如先让每次实验产生更可解释的 Observation。

### 16.3 最危险的不是失败，而是错误的成功

基础设施失败通常很明显；更危险的是候选没有生效却被判为改善、丢掉退化 case 后计算出漂亮平均数、使用同一噪声样本同时搜索和确认，或在有状态实验中比较了两个不同快照。

平台设计的重心应放在防止错误晋升，而不是保证每轮都有“进展”。

### 16.4 通用平台与通用优化策略是两回事

同一个平台可以支持查询参数、优化器规则、资源策略和增量维护，这叫生命周期通用。某个在固定查询集上有效的每查询配置映射，并不因此成为通用优化策略。

只有把交付物的适用边界说清楚，平台的通用性才不会被误写成结论的普适性。

### 16.5 最值得自动化的是证据流，而不是最终责任

自动化最有价值的部分是：固定身份、提交和恢复实验、收集证据、暴露缺口、维护失败记忆和生成可审查报告。是否把一个候选带入更广回归或生产发布，仍然包含风险、业务优先级和责任边界。

保留人工决策不是自主系统的失败，而是把不可逆责任放回正确的位置。

## 结语

数据库性能优化的自主闭环，不是让模型永远运行，而是让研究在没有新证据时安静等待，在证据不足时诚实地说未知，在候选失败时保存反例，在条件满足时给出可重放的结论。

如果只保留四条原则，我会选择：

1. **Agent 提出假设，控制器治理行动，Evaluator 判断证据。**
2. **Candidate、Trial、Attempt 和 Replicate 必须拥有不同身份。**
3. **先保证正确性、覆盖和可比性，再讨论单一性能分数。**
4. **自主循环的目标不是持续产生修改，而是持续减少不确定性。**

真正成熟的性能 Agent 不会每一轮都告诉我们“已经更快”。它会知道什么时候应该读源码，什么时候应该测量，什么时候需要反例，什么时候证据不足，以及什么时候继续实验已经不再值得。

## 附录 A：单次 Campaign 检查表

1. 目标、硬约束、允许动作和停止条件是否已版本化？
2. Workload Snapshot 与 Environment Fingerprint 是否完整？
3. 假设是否包含预期观测和反证条件？
4. Candidate、Trial、Attempt 与 Replicate 是否正确区分？
5. 配置、计划或制品是否证明候选真实生效？
6. 正确性、覆盖和性能指标是否由独立 Evaluator 判断？
7. 基础设施失败、候选失败、证据缺失和未知是否分开？
8. 提交响应丢失时能否按 execution key 查询？
9. 基线和候选是否处于可比较的环境分层？
10. 最终候选是否使用了未参与选择的新测量确认？
11. 失败经验、成本和适用边界是否进入交付物？
12. 发布是否仍由独立、受控流程决定？

## 附录 B：公开资料索引

### Agent 控制面与循环框架

- [Code Factory — GitHub](https://github.com/luohaha/code-factory)
- [Code Factory architecture and domain model](https://github.com/luohaha/code-factory/blob/main/docs/architecture.en.md)
- [GoaLoop — GitHub](https://github.com/luohaha/GoaLoop)
- [GoaLoop orchestrator](https://github.com/luohaha/GoaLoop/blob/main/goaloop/orchestrator.py)
- [LoopX — GitHub](https://github.com/loopx-project/loopx)
- [LoopX Turn for Codex CLI](https://github.com/loopx-project/loopx/tree/main/docs)
- [Raven — GitHub](https://github.com/EverMind-AI/Raven)
- [Raven Oncall documentation](https://evermind-ai.github.io/Raven/oncall/)

### 编排、搜索与实验记录

- [Temporal Activity Definition](https://docs.temporal.io/activity-definition)
- [Optuna Ask-and-Tell Interface](https://optuna.readthedocs.io/en/stable/tutorial/20_recipes/009_ask_and_tell.html)
- [MLflow Tracking](https://mlflow.org/docs/latest/ml/tracking/)
- [TPC-DS 官方项目页](https://www.tpc.org/tpcds/)

这些项目提供可借鉴的状态、执行、搜索和展示机制，但不直接证明任何具体数据库候选的性能收益。本文中的 Domain Pack、数据模型和实验合同是基于公开实现抽象出的设计思考，而不是上述项目已经提供的完整数据库优化产品。
