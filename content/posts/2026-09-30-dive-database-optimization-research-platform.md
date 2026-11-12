---
title: "【调研】数据库优化研究平台与自主性能闭环：从情报证据到可验收实验"
date: 2026-09-30T00:00:00+02:00
lastmod: 2026-10-18T00:00:00+08:00
slug: "dive-database-optimization-research-platform"
categories:
  - 数据库
tags:
  - 性能工程
  - 查询优化器
  - 执行引擎
  - 技术情报
  - LLM Agent
  - RAG
  - Benchmark
  - 实验平台
description: "从数据库内核团队的两个真实痛点出发：上篇探讨如何把外部论文、PR 与源码变成有前提、可失效、能结案的版本化研究记录；下篇拆解如何分离候选生成、幂等调度与独立验收，构建崩溃可恢复的自主性能实验闭环。"
draft: false
toc: true
math: true
---

数据库内核团队的日常工作群里，几乎每周都会上演两幕似曾相识的场景。

第一幕发生在读论文或看竞品动态时：有人往群里甩了一条链接——“某引擎刚合入了自适应外连接简化”或者“DuckDB 发了一篇分区外部聚合的深度博客，我们要不要跟进？”如果只靠一个自动抓 RSS 的大模型摘要机器人，它会热情洋溢地把论文里的最大加速比抄下来，再顺手去自家代码库里搜一圈类名，没搜到同名文件就断定“我方存在能力缺口”。可真正动手排期前，内核工程师关心的问题它一个都没回答：**这项优化成立的语义前提是什么？我们的分析器或运行时是不是已经换了个名字实现了同等效果？单机共享缓冲池的做法搬到分布式 Shuffle 上会不会因为序列化开销倒贴钱？**

第二幕发生在动手调优时：既然编码 Agent 已经能读代码改配置了，有人干脆写了个 `while true` 脚本，让 Agent 连夜在集群上调优 TPC-DS 的 Hash 与 Sort 聚合参数。第二天早上一看日志，脚本报喜“退出码 `0`，耗时下降 18%”。可仔细一查现场却让人后背发凉：要么配置名拼错了压根没生效，所谓的加速全是操作系统 Page Cache 变暖的功劳；要么激进改写漏算了空值分组，靠少吐数据换来了“提速”；更糟的是，半夜有一次提交请求遭遇网关 30 秒超时，Agent 醒来二话不说重发了一遍，直接在共享集群上撞出了两份一模一样的昂贵压测作业。

把这两幕放在一起看，就能摸清数据库性能工程的一体两面：**前半程是静态情报研究**，目标是在不动机器的前提下，把散乱的外部论文、PR 和源码收敛成“前提清楚、边界明确、知道何时结案”的工程判断；**后半程是动态实验验证**，一旦静态分析走到“机制可行、但真实负载收益未知”的边界，就必须交由一套能管住预算、扛住进程崩溃、且不允许出题人自己改评分标准的实验系统去证伪。

本文将这两半程合并为一套连贯的系统设计：**上篇**聚焦只读研究平台，拆解外部技术动态如何穿过语义前提与反向检索，沉淀为版本化的研究合同；**下篇**接过静态研究移交的待验假设，对照开源控制面与执行宿主源码，推演一套可恢复、可审计的自主性能实验闭环。

---

## 上篇：从外部情报到可证伪的研究记录

### 一、为什么“AI 摘要订阅 + 仓库 Wiki”做不出内核决策？

设想我们要持续跟踪三四个主流开源引擎与顶会论文。最省事的做法是拿 [Miniflux](https://github.com/miniflux/v2) 订一批技术博客，用 [changedetection.io](https://github.com/dgtlmoon/changedetection.io) 盯住没有 RSS 的 Release 页面，再接个大模型做每日总结；遇到需要看源码的地方，就打开 [DeepWiki](https://docs.devin.ai/work-with-devin/deepwiki)（或开源社区的 [DeepWiki-Open](https://github.com/AsyncFuncAI/deepwiki-open)、[CodeWiki](https://github.com/FSoft-AI4Code/CodeWiki)、[OpenDeepWiki](https://github.com/AIDotNet/OpenDeepWiki)）问两句架构脉络，配合 [CodeGraphContext](https://github.com/CodeGraphContext/CodeGraphContext) 这类基于 [Tree-sitter](https://tree-sitter.github.io/tree-sitter/) 或 [SCIP](https://github.com/sourcegraph/scip) 协议的索引工具查调用链。

这套工具链用来给陌生仓库“认路”非常高效——比如 DeepWiki 官方文档本身就支持通过配置文件显式指定关注目录与背景说明，省去了人工大海捞针的时间。但如果你指望把它们直接拼成一个“自动产出内核优化建议”的平台，马上就会在三个地方卡壳：

1. **同一事件的多重传播会制造“假新意”**：一项内核特性往往先有设计 RFC，接着合入 PR、写进 Release Note，半年后又被技术公众号翻译成解读长文。如果按网页粒度做摘要，同一项改动会被当成四次“重大突破”反复推送；反过来，某项激进优化合入两周后因线上错数被悄悄 Revert，旧博客却依然挂在网上误导后续判断。
2. **代码说明解决不了“跨阶段同义实现”**：数据库里同一个优化效果，既可以在 Analyzer/Binder 阶段做表达式规范化，也可以写成 Cascades Memo 里的逻辑探索规则、物理属性推导（Property Derivation & Enforcer）、代价模型交叉点调整，甚至留到 Runtime 做自适应动态过滤。只搜类名或语法树（尤其在缺乏完整编译数据库导致 C++ 宏与模板语义索引残缺时），极易把“换了位置实现”误判成“功能缺失”。
3. **周报配额会倒逼系统编造缺口**：如果给平台定下“每周必须产出十条优化机会”的 KPI，当外部风平浪静或目标引擎早已覆盖相关机制时，模型就会被迫把单机玩具实验硬往分布式架构上套。真正可靠的研究系统必须**允许不生成建议**——明确给出“目标系统已覆盖”“执行模型不兼容”或“缺少关键证据，保持未知”，同样是高价值的结案。

再往深一层看，学术界与工业界针对 SQL 改写和验证其实已经打磨出不少专用武器，但每件武器都有严格的语义射程：

| 工具类别与代表项目 | 核心解决的问题 | 对研究平台的架构启发 | 为什么不能直接替代研究平台 |
| --- | --- | --- | --- |
| **仓库导航与代码索引**<br>DeepWiki、[CodeWiki](https://github.com/FSoft-AI4Code/CodeWiki)、[CodeGraphContext](https://github.com/CodeGraphContext/CodeGraphContext)、[SCIP](https://github.com/sourcegraph/scip) | 快速梳理陌生仓库模块职责、符号定义与跨文件调用关系 | 研究任务应显式注入领域术语与核心目录，缩小源码探索半径 | 懂代码结构不等于懂 SQL 三值逻辑、数据倾斜退化边界与跨版本演进状态 |
| **证据辅助改写与规则发现**<br>[R-Bot](https://github.com/curtis-sun/LLM4Rewrite)（[PVLDB 2025](https://doi.org/10.14778/3750601.3750625)）、[WeTune](https://github.com/WeTune/WeTune-code) | R-Bot 用混合检索召回改写证据约束 LLM 选规则；WeTune 自动枚举发现等价重写规则 | 先把零散经验结构化为带前提的可检索规则与证据，再让模型推理 | 聚焦查询级或特定算子子集的改写生成，不负责跨引擎架构映射与情报生命周期管理 |
| **等价性求解与形式化证明**<br>[SQLSolver](https://github.com/SJTU-IPADS/SQLSolver)、[Cosette](https://github.com/uwdb/Cosette)、[Lean](https://lean-lang.org/)、[TLA+](https://lamport.azurewebsites.net/tla/tla.html) | 在受限关系代数/SQL 子集上证明语义等价，或用 TLA+ 检查并发/溢写恢复状态机 | 求解器返回 `UNKNOWN` 时必须原样保留“模型无法判定”，绝不能偷换成“不等价”或“已证明” | 形式化模型只覆盖显式建模的集合/多重集与空值子集，证明等价也不回答真实硬件上快不快 |
| **反例构造与优化实验台**<br>[SQLancer](https://github.com/sqlancer/sqlancer)、[PostBOUND](https://github.com/Optimizer-Playground/PostBOUND) | SQLancer 用变形测试抓逻辑反例；PostBOUND 解耦优化策略原型与负载评估 | 任何正向能力分析都必须同步配套构造破防反例与独立实验组织 | 属于动态运行期验证工具，需在只读研究筛出明确假设后再按需介入 |

回看近年的工业界实践，同样印证了“边界隔离”的重要性：华为在 [R-Bot 论文](https://doi.org/10.14778/3750601.3750625) 中靠多源改写证据约束大模型幻觉；微软在 SCOPE 生产环境落地 [QO-Advisor（SIGMOD 2022）](https://www.microsoft.com/en-us/research/publication/deploying-a-steered-query-optimizer-in-production-at-microsoft/) 时，把高开销的计划探索严格关在离线流水线里，并为坏计划保留可逆回退路径；而 CIDR 2026 的 [《Leveraging Query Optimizers to Verify the Soundness of LLM-based Query Rewrites》](https://www.vldb.org/cidrdb/2026/leveraging-query-optimizers-to-verify-the-soundness-of-llm-based-query-rewrites-for-real-world-workloads.html) 在真实企业负载上指出，语义等价性校验才是阻碍 LLM 改写落地的最大门槛，但反过来，模型提出的合法改写又能像镜子一样照出既有优化器漏掉的候选变换。

因此，在组织持续研究平台时，与其走向“纯人工维护静态卡片”（规模一大就靠个人记忆，人员流动即断档）或“全开放 Agent 自由漫游”（每次把固定订阅源都变成不可预测的漫长搜索，账单与轨迹失控）这两个极端，最务实的架构是**确定性资料流水线 + 按需受控源码探索**：抓取快照、哈希去重、游标推进、版本归并与权限预算全部交给确定性程序；大模型 API 负责结构化提取机制与前提；只有当关键局部问题悬而未决时，才拉起只读工作区里的代码 Agent 去追踪调用链（底层执行宿主的取舍详见下篇[第六节执行宿主选型](#execution-harness)）。在整个过程中，**Agent 的工作区只是一次短命任务的临时草稿纸，抓取游标、能力档案与历史结论的所有权永远归属平台的控制面数据库**。

---

### 二、版本化研究合同：拆开 Source、Event、Capability 与 Finding

要让一条研究记录经得起半年后的复查，首先得把生命周期完全不同的四个概念拆开存进账本，而不能混成一张“文章表”：

- **`Source`（不可变资料源）**：一篇具体的 Release Note、论文 PDF、PR 页面或固定 Commit 下的源码与测试文件。网页保存规范化 URL、抓取时间与正文片段哈希；源码优先绑定 `commit + path + symbol`（行号会随代码演进漂移，符号引用稳定得多）。
- **`Event`（技术事件）**：多份 `Source` 共同指向的同一项技术演进。它沿着 `proposal/issue → implementation merged → available in release → enabled by default → restricted / reverted` 的状态机流转——合入主干不等于发版可用，发版可用更不等于默认开启；旧文首次被爬虫抓到只能记为“新增收录”，绝不能谎报成“本周新发布”。
- **`Capability`（目标系统能力档案）**：围绕自家引擎维护的一份**最小决策记忆**（起步几十条结构化 Markdown 即可，无需一上来给全仓建庞大知识图谱）。它只记录已确认的能力边界、关键架构约束、以及“历史上某条建议为什么被否决”，专门用来拦截重复造轮子的误判。
- **`Finding`（版本化研究结论）**：绑定特定 `Event` 版本与 `Capability` 档案快照后得出的结构化判断，明确写清下一步核对动作（`next_check`）与结案条件（`close_when`）。

把这四类实体串起来，异构情报源穿过两阶段提取与反向检索，沉淀为带证据阶梯的研究结论；同时一旦上游提交被回退或自家引擎升级了大版本，失效监控会立刻把受波及的旧结论降级为待复核：

![数据库研究平台两阶段知识资产管线与失效重验机制](/images/diagrams/dbres-research-knowledge-pipeline.svg)

注意流程中间特意切出的**两阶段盲态隔离**：第一阶段提取外部机制时，故意不把自家系统的能力档案塞进模型上下文！如果一上来就让模型带着“找自家缺口”的任务去读外部论文，模型极易为了凑出差异报告而曲解原文前提；先把外部材料客观说了什么、依赖什么假设原原本本钉死，第二阶段再对照自家档案做**反向检索**（依次排查同义术语、Analyzer 提前简化、属性 Enforcer、共享执行库、Runtime 自适应回退以及商业/开源分支差异），误报率就会大幅下降。

在落笔写 `Finding` 时，平台还要守住三条铁律：

第一，**严格区分来源谱系（Provenance）与证据效力（Evidence）**。JSON 校验通过、链接能打开，只证明这句话“有出处”（Provenance）；至于那段源码是不是死代码、优化器代价模型会不会选它、论文里的合成数据集加速比能不能外推，则是证据效力问题。与其把不同层级的材料揉成一个虚幻的 `0.93 置信度`，不如诚实记录证据处于哪一级阶梯（官方声明 → 设计文档/论文 → 固定版本源码与 Guard → 正反回归测试 → 同版本 `EXPLAIN` 计划 → 受控 Benchmark → 生产回滚验证）。

第二，**正向证据与负向证据高度不对称**。证明系统“支持某能力”，只要指明一条可达代码路径加上正反测试就够了；但要想断言“整个引擎不支持某能力”，搜不到某个类名根本不算数——最起码要在固定版本源码里找到某条明确拒绝条件 `C` 的 Guard（确认为局部缺口），并排除其他阶段兜底的可能。没查清之前，目标状态必须老老实实标为 `未知（unknown）`，否则开源资料少的引擎会在系统里被自动扣成“低能儿”：

```json
{
  "finding_id": "OPT-2026-0042-v3",
  "event_version": "EV-018@5",
  "target_profile_version": "engine-profile@31",
  "facts": ["F-112", "F-113"],
  "mechanism": {
    "problem": "skewed aggregation state",
    "stage": "runtime",
    "action": "partition and spill selected state",
    "preconditions": ["spillable aggregate state"],
    "limitations": ["high repartition cost for extreme heavy hitters"]
  },
  "target_status": "unknown",
  "unknowns": ["current state ownership during recovery"],
  "next_check": "trace state layout, spill trigger and restore path",
  "close_when": "the supported state types and peak-memory behavior are known"
}
```

第三，**句子级拆分事实与推断，并把外部文本一律视作不可信输入**。在报告正文里显式分开 `[事实]`（某版本 Guard 在条件 C 返回 false）、`[推断]`（该规则分支不处理 C）、`[未知]`（尚未排除 Builder 阶段完成同类简化）与 `[建议]`，不让前半句真实的源码链接替后半句跳跃的猜测背书；同时每次分析运行按 `event_version + analysis_scope + target_profile_version + source_snapshot_set + schema_version + policy_version` 计算幂等键，外部网页与仓库 README/注释在只读沙箱中解析，绝不执行任何仓库自带脚本，防止提示词注入污染结论或穿透宿主机。

---

### 三、两个真实机制走读：外连接简化与外部溢写聚合

光谈数据合同太抽象，我们直接拿一个查询优化器问题和一个执行引擎运行时问题走一遍流程，看看合格的研究记录长什么样。

先看优化器侧的**外连接简化（Outer Join Simplification）**。假设外部引擎发布了一条改进：“增强了复杂表达式下的外连接转内连接规则”。我们先看最基础的 SQL 形态：

```sql
SELECT a.id, b.amount
FROM A AS a
LEFT JOIN B AS b ON a.k = b.k
WHERE b.amount > 0;
```

从第一性原理推导它为什么能把 `LEFT JOIN` 安全改写成 `INNER JOIN`：在 SQL 默认的多重集语义（Bag Semantics，保留重复行次数）与三值逻辑下，左表某行如果在右表找不到匹配，`LEFT JOIN` 会补出一行 `b.amount = NULL`；紧接着上层 `WHERE` 计算 `NULL > 0` 得到 `UNKNOWN`，而 `WHERE` 子句只放行 `TRUE`，这行千辛万苦补出来的空值行注定被丢弃！因此，只要上层谓词对右表补空列是**拒绝空值的（Null-Rejecting，即输入为 `NULL` 时结果绝不可能为 `TRUE`）**，且表达式确定、不改变除零或类型转换异常的触发时机，直接把 `LEFT JOIN` 压成 `INNER JOIN` 就完全等价——注意，这里甚至**不需要要求连接键唯一**，因为匹配行的重复重数在改写前后分毫不差。

一旦把前提剥得这么清楚，破防反例随手就能写出来：

```sql
-- 反例 1：COALESCE 把补出的 NULL 变成了 0，谓词不再拒绝空值，改写会丢行！
WHERE COALESCE(b.amount, 0) >= 0

-- 反例 2：析取分支（OR）左侧可能单独为 TRUE，把右表未匹配行放过去！
WHERE b.amount > 0 OR a.category = 'important'
```

带着这套前提与反例去做第二阶段反向检索，我们就不会傻乎乎只搜 `OuterJoinSimplificationRule` 这个类名了，而是顺着 Analyzer 表达式折叠、谓词空值属性推导（Nullability / Null-Rejecting Inference）、Join 重写注册顺序以及正反回归测试一路摸排，把宏大的“要不要学竞品”收敛为一个两小时就能结案的具体核查项：**“固定当前主干版本，我方空值拒绝推导能否穿透确定性 `CAST` 表达式？若已有规则链与正反用例覆盖则直接结案；若 Guard 提前拦截，再判断是故意规避溢出异常的语义保护，还是值得补齐的局部缺口。”**

再看跨层运行时的**外部溢写聚合（External Aggregation）**。读到 [DuckDB 关于 External Aggregation 的技术解析（2024）](https://duckdb.org/2024/03/29/external-aggregation) 时，文章漂亮地展示了它如何结合统一缓冲池（Buffer Manager）、可换出内存页、变长字符串指针恢复、线程局部预聚合（Pre-aggregation）与按分区聚合（Partition-wise Aggregation），在内存受限时优雅退化。

如果摘要机器人看到这里就高呼“我们也应该照搬这套 Spill 机制”，那就踩进了典型的**执行模型错配陷阱**。在运行时研究里，从来没有白捡的性能，只有**资源交换**——省下了内存峰值，必然要拿序列化 CPU、磁盘 I/O、并发同步或长尾延迟去换。把单机引擎的外部聚合放到分布式分析引擎面前对照时，研究卡片必须逐一拷问七笔硬账：

1. **聚合状态可序列化吗？** `SUM`/`COUNT` 只是定长数字，但 `HLL`、分位数草图或用户自定义聚合函数（UDAF）如果在进程堆内存里挂着裸指针，落盘或跨网搬运后怎么恢复？
2. **状态归属权（State Ownership）在谁手里？** 是线程私有无锁分区，还是多线程共享哈希表？这直接决定了溢写瞬间怎么安全冻结状态。
3. **高基数（高 NDV）与极端倾斜（Heavy Hitters）怎么退化？** 当分组键几乎不重复时，上游局部预聚合基本失效，只会白白浪费哈希探测 CPU；而当某一个热点分组塞满了单分区内存时，支不支持递归二次重分区（Recursive Repartitioning）？
4. **内存仲裁是算子各自为政还是全局统筹？** 一条复杂查询里三个 Hash Join 加两个 Hash Aggregate 同时跑，如果每个算子都按固定上限各自申请内存才触发 Spill，整机内存照样会被峰值叠加打爆（OOM）。
5. **单机 Buffer Pool 与分布式 Shuffle 的地基差异**：单机引擎落盘后页还在本机 NVMe 上，而存算分离或分布式引擎还要跟 Shuffle 分区粒度、远端对象存储小文件放大以及节点故障重算打交道。
6. **优化器代价模型跟得上吗？** 一旦运行时换了新的外部聚合实现，Hash Aggregate 与 Sort Aggregate 的内存开销交叉点（Crossover Point）就会跟着移动，优化器如果不更新成本公式，依然会选错物理算子。

你看，经过这样一番拆解，输出的 `Finding` 就不再是一句空洞的口号，而是一份逻辑严密的对照清单：哪些状态紧凑布局可借鉴、哪些受制于分布式执行模型不适用、以及如果要评估真实收益需要构造怎样的高基数与倾斜负载——而这最后一步，正好停在只读研究的边界线上，准备移交给下篇的动态实验平台。

---

### 四、研究平台如何自证有效：时间切分回放、反例验收与按瓶颈演进

在还没接通动态压测集群之前，怎么证明这套只读研究平台真的帮内核团队省了力气，而不是用更精致的排版生产废话？

答案是构造**按事件与时间严格切分的历史回放集（30–50 个真实技术事件即可起步）**。把同一项 PR 衍生的设计文档、代码提交、Release Note 和二手博客打包进同一个 `Event` 分组，防止训练/提示侧见过博客、评测侧拿 Release Note 来考的“数据穿越”；回放时只喂给系统截至当时可见的快照和能力档案版本。统计指标必须把分母摊在阳光下——分别核算**证据可追溯率**（带有效快照定位的事实占比）与**事实支持率**（人工抽查中原文真正支撑该论断的占比，戳破“链接能打开但驴唇不对马嘴”的幻觉）、**重大语义错误率**、**重复建议率**以及**单条有效建议的综合成本**（模型 Token + 工具开销 + 专家审核纠偏耗时），并拿它跟“专家原生态阅读”和“裸大模型读新闻摘要”做消融对比。

更关键的是，验收用例必须专门拿**异常与失效传播**开刀：同一 PR 先上博客后进 Release 时，系统有没有老老实实归并到同一个 `Event` 而不虚报新能力？任务中途断网重跑，幂等键有没有拦住重复发布？上游文章勘误或自家引擎升了大版本后，依赖旧证据的 `Finding` 有没有自动挂起为 `待复核`？

在演进节奏上，千万别犯“还没跑通十条事件结案，就先花三个月搭全量向量库、代码图谱和微服务消息队列”的通病。第一版靠命令行脚本、单个 SQLite 库加版本化 Markdown 目录就能把采集、两阶段盲态提取和人工评审跑通；以后只有当观测到**同义机制频繁漏检**时再加 Embedding 与术语表，**精确配置名检索变慢**时再上全文检索（BM25），**跨模块追调用链耗时占大头**时再接 SCIP 代码索引，而当越来越多的 `Finding` 停留在“源码路径已确认，但真实负载收益未知”时——就是启动下篇自主性能实验闭环的准确信号。

---

## 下篇：从候选假设到可恢复、可验收的自主性能闭环

### 五、为什么 `while true` 跑 Agent 调优一上真集群就会翻车？

回到文章开头的第二幕：当上篇的研究卡片把一个具体问题——比如“某类高基数查询在当前内存阈值下频繁触发 Hash 聚合落盘，调整策略或切到 Sort 聚合能否改善端到端延迟”——交到实验阶段时，为什么写个简单的脚本循环调编码 Agent 很快就会失控？

把一次真实集群上的性能实验拆开，会发现里面套着**三个优化对象完全不同的循环**，而朴素脚本把它们揉成了一团：

1. **单轮工具循环（`AgentTurn`）**：在一个模型轮次内读源码、查 Profile、改配置文件、输出 JSON 提案。它的进程返回 `exit_code == 0` 且 JSON 格式合法，仅仅证明**“工具调用没崩”**，既不代表配置真的被内核吃进去了，更不代表查询结果算对了。
2. **跨进程实验循环（`Campaign → Trial → Evaluation`）**：提交编译与压测作业、跨小时乃至跨天等待集群返回、比对结果多重集、核查内存峰值与统计置信度。这个周期远长于单次大模型连接寿命，绝不能靠挂着一个长连接轮询等待。
3. **方法改进循环（离线评测）**：调整提示词模板、上下文裁剪策略或搜索算法，看同样预算下能不能提出更准的假设。如果允许线上正在跑调优任务的 Agent 顺手修改自己的评分脚本、删掉跑慢的退化查询、或者放宽超时门槛，优化过程瞬间就会异化为**“改尺子凑及格”的自我证明（Goodhart 定律）**。

不仅如此，我们还必须把**平台运行成功**和**性能优化成功**彻底解耦：一套合格的自主实验平台，在无人值守跑完一整夜、优雅恢复了一次网关超时、最后诚实交付“在当前预算与参数空间内未发现统计显著的正向收益”时，**平台本身是 100% 成功的**；反过来，靠挑拣噪声低谷样本报出虚假的 15% 加速，则是彻底的工程事故。

由此可以推导出数据库自主性能平台不可妥协的五条底线约束：**可复核（R1）**——冻结负载快照、引擎制品与评价政策，重跑能对得上账；**可恢复且有界（R2）**——进程重启或响应丢失后能找回在途集群作业，绝不盲目双提烧穿预算；**独立验收（R3）**——提候选的 Agent 对评价器与原始观测零写权限；**场景可插拔（R4）**——无状态查询调优与有状态增量物化视图能复用同一套调度骨架，但各自执行专属的语义校验；**分步演进（R5）**——首版只对接现有压测器产出已验证候选，生产上线仍走人工变更审批。

---

### 六、开源控制面与执行宿主选型：谁管会话，谁管外部副作用？ {#execution-harness}

自己动手写控制面之前，先看看社区里主打“长周期自主运行”的开源 Agent 项目和成熟的编排/实验底座各自走到了哪一步。对照固定源码快照与官方文档走读一圈，各层的真实能力边界其实非常清晰：

| 项目 / 组件 | 检查基线与核心模块 | 值得复用的架构机制 | 距离数据库性能实验闭环的核心缺口 |
| --- | --- | --- | --- |
| **[Code Factory](https://github.com/luohaha/code-factory)** | 提交 [`595c35e`](https://github.com/luohaha/code-factory/tree/595c35e1445885f9bd5c2acdb171c63e537676f4)（[`sqlite-store.ts`](https://github.com/luohaha/code-factory/blob/595c35e1445885f9bd5c2acdb171c63e537676f4/packages/agent-manager/src/sqlite-store.ts#L638)、[`agent-manager.ts`](https://github.com/luohaha/code-factory/blob/595c35e1445885f9bd5c2acdb171c63e537676f4/packages/agent-manager/src/agent-manager.ts#L1900)） | 将长期 `Requirement` 与单次 `AgentSession / Run` 解耦；`appendAgentTriggerMessage()` 在 `BEGIN IMMEDIATE` 事务中按 `(trigger_id, idempotency_key)` 去重写回执后再派发 | 解决的是“入站触发消息不重发”，管不到出站集群 Benchmark 作业的幂等恢复与 SQL 结果多维验收 |
| **[GoaLoop](https://github.com/luohaha/GoaLoop)** | 提交 [`e1ea3e0`](https://github.com/luohaha/GoaLoop/tree/e1ea3e0841aae8253c97eb0bafc4bd64b2b7e799)（[`orchestrator.py`](https://github.com/luohaha/GoaLoop/blob/e1ea3e0841aae8253c97eb0bafc4bd64b2b7e799/goaloop/orchestrator.py#L232)） | 有界单轮循环；先持久化检查点，目标文件变更时拒绝盲目续跑旧会话，通过 [终止符解析](https://github.com/luohaha/GoaLoop/blob/e1ea3e0841aae8253c97eb0bafc4bd64b2b7e799/goaloop/orchestrator.py#L365) 驱动验证闭环 | 适合单人串行轻量验证；仅靠提示词约定无法构成 R3 所需的物理写隔离与跨样本统计确认 |
| **[LoopX](https://github.com/loopx-project/loopx)** | 提交 [`5cb9e4b`](https://github.com/loopx-project/loopx/tree/5cb9e4b8222a94a24a38ad2d7163e32322c11f73)（[`turn_run_once.py`](https://github.com/loopx-project/loopx/blob/5cb9e4b8222a94a24a38ad2d7163e32322c11f73/loopx/cli_commands/turn_run_once.py#L104)） | 将验证命令封装为 `task_validator`，在 [单轮驱动入口](https://github.com/loopx-project/loopx/blob/5cb9e4b8222a94a24a38ad2d7163e32322c11f73/loopx/cli_commands/turn_run_once.py#L866) 把模型执行、外部校验、账单结算与恢复查询拆成独立接口 | 证明了宿主与结算解耦的接口形态，但验证器背后的配置生效证明、配对统计与负载快照仍需领域自建 |
| **[Raven-Oncall](https://github.com/EverMind-AI/Raven)** | 提交 [`3632e60`](https://github.com/EverMind-AI/Raven/tree/3632e6040c7038a60ec418ce39ccae185c72c19f)（[`runner.py`](https://github.com/EverMind-AI/Raven/blob/3632e6040c7038a60ec418ce39ccae185c72c19f/agents/raven-oncall/plugins/oncall-flow/oncall_flow/runner.py)、[`campaign.py`](https://github.com/EverMind-AI/Raven/blob/3632e6040c7038a60ec418ce39ccae185c72c19f/agents/raven-oncall/plugins/oncall-flow/oncall_flow/campaign.py)、[`backend.py`](https://github.com/EverMind-AI/Raven/blob/3632e6040c7038a60ec418ce39ccae185c72c19f/agents/raven-oncall/plugins/oncall-flow/oncall_flow/backend.py)） | 最贴近异步调优：拆开 `Proposer → Campaign._reconcile_job() → JobBackend → Ledger`，用 `JobSpec.idem_key` 区分提交身份并分离基础设施异常 | `Campaign.best()` 仅按单一标量指标取极值，无法直接表达“结果正确 + 覆盖完整 + 资源不越界 + 留出集复测确认” |
| **工作流 / 搜索 / 追踪底座** | [Temporal Activity](https://docs.temporal.io/activity-definition)、[Optuna Ask-and-Tell](https://optuna.readthedocs.io/en/stable/tutorial/20_recipes/009_ask_and_tell.html)、[MLflow Tracking](https://mlflow.org/docs/latest/ml/tracking/) | Temporal 管持久重试与异步等待；Optuna 解耦参数推荐与外部昂贵求值；MLflow 归档指标与制品 | Temporal 官方文档明确提醒重试不会替外部系统凭空造出幂等；MLflow 是展示账本而非晋升裁决权威 |

看清了这张版图，建设路线的取舍就顺理成章了：如果只是临时试一两个参数，借鉴 GoaLoop 写个受保护的验证脚本（路线 A）最快；如果团队已经在重度运维 Temporal 集群，把外部压测封装成幂等 Activity 再挂上 Optuna（路线 C）也很自然；而对于“手头已有 Benchmark 执行集群、实验昂贵且异步、但不想一上来背负整套分布式工作流微服务运维”的内核团队，最稳健的起点是**以模块化单体构建“领域控制面（Control Plane）+ 可替换执行组件”（路线 B）**——先把实验身份、预算事务和独立评价器立起来。

确定了上层控制面，紧接着就会遇到一个工程选型分岔路口：**控制面底下用来读源码、看 Profile、提候选的 Agent 执行层，究竟该直接编排现成的 Codex / Claude Code，还是嵌入 `pi` 等开源框架自己接大模型 API？**

回答这个问题前，得先拨开一层概念迷雾。直接调大模型 HTTP API 只拿到了一次文本补全；要把代码改对，外围还得包上文件读写、终端命令沙箱、上下文截断压缩、流式事件与错误重试——这一整套围绕模型的运行机制才是 **Agent 执行宿主（Agent Harness）**。如今“编排现成编码工具”早已不是用 `expect` 脚本去抠终端字符了：[Claude Code 程序化模式](https://code.claude.com/docs/en/headless) 与 [Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk/overview) 直接在 Python/TypeScript 里暴露了结构化 JSON 输出与流式事件；[Codex SDK](https://learn.chatgpt.com/docs/codex-sdk) 与 [Codex App Server](https://learn.chatgpt.com/docs/app-server) 同样提供了会话与审批协议——翻看本地 Codex 提交 `d36a3ea` 的 [`sdk/typescript/src/exec.ts`](https://github.com/openai/codex/blob/d36a3ead3c896d0552207763ef483262bce9ac73/sdk/typescript/src/exec.ts#L86) 就能看到，`CodexExec.run()` 直接以结构化参数通过 `spawn()` 拉起底层 CLI 进程并桥接输入输出事件流，让我们无需从零重写工具循环就能复用成熟的代码搜索与补丁编辑能力。

反观另一侧的开源生态，[pi coding-agent](https://raw.githubusercontent.com/badlogic/pi-mono/main/packages/coding-agent/README.md) 提供了同时支持 CLI、RPC 与 TypeScript SDK 的可扩展 Harness；而 [OpenClaw](https://docs.openclaw.ai/concepts/architecture)（主打连接多消息渠道与客户端的常驻 Gateway）和 [Hermes Agent](https://hermes-agent.nousresearch.com/docs/developer-guide/architecture)（自带跨会话记忆、技能沉淀与定时任务的持续助手）则各自往上叠了网关入口或长期记忆职责。把两条主线摆在一起对比，差异根本不在“用不用 API”，而在**控制粒度与维护包袱的交换**：

| 权衡维度 | 直接编排 Codex / Claude Code 运行时 | 嵌入开源 Agent 框架（如 `pi`）+ 自接模型 API |
| --- | --- | --- |
| **工具链与上下文能力** | 开箱即享成熟的代码搜索、补丁应用、上下文压缩与沙箱审批机制 | 可深度定制每次请求的 Prompt 拼装、工具分派与内部网关路由，但需自行回归测试工具截断与补丁容错 |
| **版本演进与维护责任** | 跟随上游工具升级做外部输入输出合同测试，内部循环受产品开放接口限制 | 掌控底层依赖，若需魔改核心循环则要承担长期维护私有 Fork 的工程成本 |
| **会话恢复与跨引擎切换** | 可绑定原生会话 ID 续跑单轮上下文，但不同工具间的原始聊天记录不可无损互转 | 可自主定制会话落盘（如 Code Factory [`595c35e`](https://github.com/luohaha/code-factory/blob/595c35e1445885f9bd5c2acdb171c63e537676f4/packages/agent-manager/src/native-agent.ts#L190) 中区分 [`AgentProvider`](https://github.com/luohaha/code-factory/blob/595c35e1445885f9bd5c2acdb171c63e537676f4/packages/agent-manager/src/types.ts#L1) 并接入 `pi-durable`），但内存态依然不等于持久工作流 |

无论选哪一边，有一条铁律是共通的：**外部集群副作用绝对不能跟着 Agent 的聊天会话走！** 设想 Agent 刚刚调工具向集群发出了一个两小时的 Benchmark 请求，还没收到返回的 `job_id` 进程就崩了。此时不管你是拿 Codex 原生 Session ID 恢复对话，还是从框架检查点续跑，只要 Agent 再调一次提交工具，集群上就会跑起双份作业。安全恢复唯一可靠的办法，是让一层薄薄的 `Agent Adapter` 只负责消费结构化证据清单、吐出候选提案（Proposal），而所有外部提交一律穿过控制面登记的 `execution_key` 去后端查账——这样哪怕第一轮由 Codex 读 C++ 源码提补丁、下一轮换 `pi` 接内部模型分析 Profile，平台账本也稳如泰山。同理，接入模型上下文协议（MCP）也只省掉了工具描述格式，权限校验与预算扣减依然必须守在服务端网关里。

---

### 七、领域控制面设计：四种身份、两层状态机与超时重连协议

把“提建议、管状态、跑压测、做裁决”的写权限彻底拆开后，受限编码 Agent、领域控制面、独占隔离压测集群与独立评价器构成如下闭环（也可打开[交互式自主性能闭环视图](/diagrams/dbres-autonomous-perf-loop.html)按阶段聚焦浏览）：

![数据库自主性能优化闭环](/images/diagrams/dbres-autonomous-perf-loop.svg)

在这套控制面里，最容易踩出致命 Bug 的地方是**把所有“去重”混成一个配置哈希**。仔细推敲一次真实的性能实验，里面其实同时存在着**四个绝不能合并的身份标识**：

1. **`Candidate Digest`（候选内容摘要）**：参数组合或代码补丁的 SHA-256。内容完全一样时复用同一个 `Candidate` 实体，免得重复编译构建。
2. **`Workload + Environment Fingerprint`（负载与环境指纹）**：锁定查询/变更序列、数据快照、统计信息、引擎二进制制品与机器资源池。指纹变了（比如集群升级或换了机型），新旧数字就不能直接混在一张表里比快慢。
3. **`Trial + replicate_id`（逻辑实验与计划内统计重复）**：为了对抗系统噪声，我们经常**故意对同一个候选连测 5 次独立样本（Replicate 1..5）**！如果去重器傻乎乎按 `Candidate Digest` 一刀切，第 2 到第 5 次独立采样就会被当成重复请求吞掉，方差估计当场报废。
4. **`execution_key`（单次基础设施提交的传输幂等键）**：绑定到具体的某一次执行尝试（`ExecutionAttempt`）。当网络抖动重发同一个 `Attempt` 时，靠它拦住集群重复建任务；只有确认前一次尝试因机器故障彻底失败后，才递增 `attempt_no` 生成新键重跑。

立住了这四重身份，跨天运行的**双层状态机**与**崩溃恢复协议**就能严丝合缝地运转起来。上层 `Campaign` 管理整个研究周期的启停与预算枯竭（`CREATED → ACTIVE → PAUSING/PAUSED / BLOCKED / COMPLETED / EXHAUSTED / CANCELLED`），并且暂停、取消和修改目标（生成新 `GoalVersion`）由 Controller 直接落库生效，绝不傻等挂起的大模型下一轮醒来“理解用户意图”。下层单次实验任务与 Worker 租约（Lease）则沿着 `Queued → Admitted → Leased → Running → Verifying` 主路径推进，并把超时失联的对账恢复与未知在途隔离严格拆开：

![控制面实验任务与租约状态机](/images/diagrams/dbres-execution-lease-lifecycle.svg)

注意看 `Leased / Running` 向下进入 `LeaseExpired → Retry → Quarantined` 的那条恢复分支——当 Worker 调外部集群的 `submit(request, execution_key)` 遭遇超时或心跳失联，客户端其实面临三种无法在本地分辨的真相：① 请求压根没到服务端；② 集群已经建好作业跑起来了，只是回包丢在了半路；③ 服务端刚收下请求还在异步落库。

这时候如果莽撞地换一个新 key 重试，就会在集群上制造双倍开销的幽灵作业；如果干脆放弃不管，又丢掉了本轮实验。唯一稳妥的协议是：**先进入 `Retry` 拿原 `execution_key` 调 `lookup()` 反查对账（Reconcile）→ 确认前次未运行或已安全终止才重签租约续跑 → 若后端接口不支持幂等反查或作业依然下落不明，立刻转入 `Quarantined`（在途未知隔离态）冻结自动重提**，交由人工或底层运维接口核实。同理，当后台 Worker 租约（Lease）过期发生主备切换时，本地关系库靠单调递增的 `fencing_token` 就能拒绝旧 Worker 的过期写回，但外部压测网关也必须同步校验 `execution_key` 或令牌，才能真正拦住旧进程残余的外部副作用。

在这样的底座之上，Agent 每轮醒来拿到的不再是一团越滚越长、随时可能因上下文压缩丢掉关键约束的聊天记录，而是一份干净的 **`Context Manifest`**（包含当前 `GoalVersion`、最新状态版本号 `state_version`、原始基线 `B0` 与当前擂主 `B*`、新增观测引用、已证伪假设及剩余预算）。Agent 提交提案时必须附带 `based_on_state_version` 和明确的证伪条件（例如：“预期切换策略后目标算子改变且 `spill_bytes` 下降；若落盘减少但排序与网络耗时反超，则证伪该假设”）；一旦提案在飞行途中遇上人工修改了目标版本，乐观锁检查立刻将其拦截作废。而采集回来的 `Observation` 更是严禁只存一个干瘪的总分——执行耗时、排队等待、峰值内存、落盘字节、结果校验凭据、实际生效配置（`effective_config`）与物理计划引用必须原样封存，采集缺失标为 `missing`（绝不填 `0` 充数），超时强杀标为删失观测（`censored`）。

---

### 八、独立验收与场景插件（Domain Pack）：从无状态查询到有状态增量维护

观测数据干干净净收进账本之后，独立的 `Evaluator` 怎么拍板一个候选到底能不能晋升（Promote）？

第一步是**事前冻结政策与双基线对照**。在 `Campaign` 启动那一刻，主指标、硬性资源红线、必测查询全集、超时处置规则与最小有意义提升幅度就已经锁死——绝不允许看到某条慢查询拖后腿就把它踢出分母。多轮迭代时，系统同时盯住两根标尺：**原始冻结基线 `B0`**（衡量累计净收益，确保多轮微调后内存或尾延迟没有悄悄越过最初的安全红线）与**当前最佳已验证候选 `B*`**（衡量本轮改动的边际增量）。

第二步是**选对多查询汇总数学模型，并用留出测量（Holdout Confirmation）打破“赢家诅咒”**。跑几十条分析查询时，如果关心每条查询无量纲的相对提速比例，常用加权几何平均加速比（设第 `i` 条查询基线耗时为 `B_i`，候选耗时为 `C_i`）：

$$S_{\text{geo}} = \exp\left(\sum_i w_i \log\frac{B_i}{C_i}\right), \qquad \sum_i w_i = 1$$

但几何平均会掩盖长耗时大查询的绝对权重——一条 0.1 秒的小查询快了 2 倍，绝不该抵消一条 100 秒核心报表慢了 2 倍的灾难！如果业务关心批处理窗口的串行总完工时长，就应当看总耗时比：

$$S_{\text{sum}} = \frac{\sum_i B_i}{\sum_i C_i}$$

而如果线上是高并发混跑，单条查询耗时相加也算不出内存争抢与网络拥塞，必须实测并发调度的端到端完工时间（Makespan），同时单独汇报最差单查询回退幅度。更微妙的是统计上的**选择偏差（Winner's Curse）**：当 Agent 或搜索器连续试了 30 组参数、挑出账面数字最好的一组时，哪怕所有参数真实效果完全一样，排第一的那个极大概率也只是恰好撞上了集群背景噪声波谷的“幸运儿”。因此，凡是跨过初筛门槛的准擂主，晋升前都必须在相同资源块里用**未参与挑选过程的新一轮配对测量**完成独立复核。在搜索分工上，也不用强求大模型自己当数值优化器——让擅长读源码与归因的 Agent 负责圈定小而合法的参数子空间或构造结构化特征，再通过 [Optuna Ask-and-Tell](https://optuna.readthedocs.io/en/stable/tutorial/20_recipes/009_ask_and_tell.html) 这类接口把昂贵的黑盒采样预算科学分配出去，两者配合远比让大模型盲猜浮点数高效得多。

第三步，为了不让控制面和数据库特定负载绑死，我们把场景专属的语义抽成版本化的 **`DomainPack` 插件**，与底层的 `BenchmarkAdapter` 解耦：

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

对照两个性格截然不同的场景插件，就能看清这套抽象的威力：

- **无状态查询策略插件（如 `Hash / Sort` 算子与参数调优）**：候选交付物按泛化能力分为四级——**全局默认参数**、**固定负载的每查询/模板映射**、**基于编译期计划特征的选择规则**（严禁偷看运行后真实基数作弊，且必须在搜索未见过的查询模板上验证泛化性），以及**内核代价模型或算子代码补丁**（必须走完“隔离工作区打补丁 → 单测与语义回归 → 编译登记新二进制制品哈希 → 提交压测”全链路，杜绝“改了源码却测到旧二进制”的乌龙）。实验推进遵循漏斗原则：先在少量正反代表查询上确认 `effective_config` 真生效、目标物理算子真变了，再放量跑全集与并发混跑，因为小规模低保真数据上的赢家一旦撞上真实数据量的 Hash 表扩容与 Spill 阈值，表现可能彻底反转。
- **有状态增量物化视图插件（`Incremental View Maintenance，IVM`）**：测物化视图绝不是“把一条 SQL 多跑几遍”。同一句视图定义，面对全插入流和面对高频删除热点键，内部状态膨胀天差地别。因此一个有状态 `Trial` 的实验身份必须锁死完整的七元组历史：
  ```text
  (D0 初始快照, view_definition, delta_sequence 有序变更流,
   transaction_schedule, refresh_schedule, query_checkpoints, horizon 观察窗口)
  ```
  在验收时，除了要防备**“循环验证陷阱”**（即用来算标准答案的基表参考查询，被优化器自作聪明地透明重写到了正在被测的物化视图自己身上！必须显式关闭重写开关或走独立参照），还要防备中途断连后的**非幂等变更重放污染**——一旦提交水位不明，宁可推倒隔离环境从 `D0` 重放，也绝不盲目续灌增量包。最终评价更不能只看“单次刷新快了几毫秒”（靠偷懒少刷新或推迟状态清理就能刷出漂亮的局部数字），而必须在完整观察窗口内交付包含**快照结果正确性、P99/最坏刷新延迟、数据新鲜度 SLA、后台状态存储膨胀、全量重算回退率与清理开销**的指标向量。

每跑完一轮，平台沉淀下来的**研究记忆**也不存冗长脆弱的模型思维链（Chain-of-Thought），而是按证据硬核程度分级归档（`SOURCE_SUPPORTED` 源码支持 → `PLAN_CONFIRMED` 计划确认生效 → `MEASURED` 拿到合法观测 → `VALIDATED` 通过留出复测确认），同时把**失败经验与反例边界绑定引擎版本一并入库**——否则摘要一删，下一轮 Agent 又会在同一个死胡同里把昂贵的集群预算再烧一遍。

---

### 九、最小闭环验证与演进账本：用故障注入测试控制面，按回本不等式扩张

落地这套自主性能平台时，最忌讳的顺序是“先花大力气把多 Agent 对话界面造得天花乱坠，底下却连参数生没生效、任务重没重提都查不清”。真正稳健的六步依赖链应当倒过来从底层确定性合同往上长：

1. **D0 冻结实验合同**：选定单一引擎与代表负载（如 [TPC-DS](https://www.tpc.org/tpcds/) 子集），打通结果多重集校验、覆盖率检查与 `Attempt / Replicate` 身份区分；
2. **D1 接通可反查的压测后端**：实现 `submit / lookup / status / collect / cancel`，确保不靠 Agent 也能手工跑完单个候选，并在提交超时未知时稳稳停住；
3. **D2 立起恢复事务与独立评价器**：加上预算预留、Outbox 与冻结政策 `Evaluator`，用假后端做一轮残酷的**故障注入验收**——故意丢弃 `submit()` 响应并重启 Controller 看会不会双提作业、注入过期 Worker 延迟写回测 `fencing_token`、塞入“快一倍但少吐一行数据”或“改了配置却没生效”的毒丸候选看 `Evaluator` 会不会铁面拒收、在实验半途升级 `GoalVersion` 看旧结果会不会未经重评就越权晋升；
4. **D3 接入受限编码 Agent**：喂入结构化 `Context Manifest`，实现“无新证据静默等待、有新观测自动唤醒提案”，彻底免去人工敲回车续命；
5. **D4 跑通首个无状态查询策略闭环**：完成 `B0 / B*` 双基线对照与留出集复测，在同等预算下跟简单网格枚举及人工调优算清真实投入产出；
6. **D5 按需扩展有状态场景与团队并发**：等单链路完全跑稳，再引入有状态 IVM 插件、Optuna 搜索服务，或在跨服务高并发压力真正浮现时平滑迁入 Temporal 工作流底座。

归根结底，自主性能优化平台自己也是一项需要算总账的工程投资。决定它该做到多深、扩到多大的标尺，不是运行了多少轮对话，而是一条实打实的回本不等式：

$$\text{节省的专家盯盘与交接工时} + \text{拦下的重复与无效压测账单} + \text{经独立确认的内核优化业务收益} > \text{控制面研发维护成本} + \text{新增模型推理、留出复测与制品存储开销}$$

把上篇的只读研究平台与下篇的自主实验闭环合拢，数据库内核团队面对浩如烟海的外部论文与内部调优空间时，终于有了一条首尾相接的工程轨道：**前半程靠版本化证据合同与反向检索挡住盲目跟风，把真问题提准；后半程靠身份隔离、幂等恢复与独立评价挡住噪声与自嗨，把真收益测实**——最后再带着可复现的完整证据链，放心交回工程师手里拍板合入。

---

## 参考资料与源码基线

**上篇：技术情报、仓库理解与 SQL 等价验证**
- **仓库导航、索引与信息源监控**：[DeepWiki 官方文档](https://docs.devin.ai/work-with-devin/deepwiki)、[DeepWiki-Open](https://github.com/AsyncFuncAI/deepwiki-open)、[CodeWiki](https://github.com/FSoft-AI4Code/CodeWiki)、[OpenDeepWiki](https://github.com/AIDotNet/OpenDeepWiki)、[CodeGraphContext](https://github.com/CodeGraphContext/CodeGraphContext)、[Tree-sitter](https://tree-sitter.github.io/tree-sitter/)、[SCIP 协议](https://github.com/sourcegraph/scip)、[Miniflux](https://github.com/miniflux/v2)、[changedetection.io](https://github.com/dgtlmoon/changedetection.io)。
- **SQL 改写、形式化验证与实验组织**：[R-Bot 论文（PVLDB 2025）](https://doi.org/10.14778/3750601.3750625) 与 [开源代码](https://github.com/curtis-sun/LLM4Rewrite)、[WeTune](https://github.com/WeTune/WeTune-code)、[SQLSolver](https://github.com/SJTU-IPADS/SQLSolver)、[Cosette](https://github.com/uwdb/Cosette)、[SQLancer](https://github.com/sqlancer/sqlancer)、[PostBOUND](https://github.com/Optimizer-Playground/PostBOUND)、[Lean 定理证明器](https://lean-lang.org/)、[TLA+](https://lamport.azurewebsites.net/tla/tla.html)。
- **工业界优化器治理与运行时机制案例**：[Microsoft QO-Advisor（SIGMOD 2022）](https://www.microsoft.com/en-us/research/publication/deploying-a-steered-query-optimizer-in-production-at-microsoft/)、[Leveraging Query Optimizers to Verify the Soundness of LLM-based Query Rewrites（CIDR 2026）](https://www.vldb.org/cidrdb/2026/leveraging-query-optimizers-to-verify-the-soundness-of-llm-based-query-rewrites-for-real-world-workloads.html)、[DuckDB External Aggregation（2024）](https://duckdb.org/2024/03/29/external-aggregation)。

**下篇：Agent 控制面、执行宿主与实验基础设施**
- **开源控制循环与去重/对账实现**：[Code Factory](https://github.com/luohaha/code-factory)（[架构说明](https://github.com/luohaha/code-factory/blob/595c35e1445885f9bd5c2acdb171c63e537676f4/docs/architecture.en.md)）、[GoaLoop](https://github.com/luohaha/GoaLoop)（[`orchestrator.py`](https://github.com/luohaha/GoaLoop/blob/e1ea3e0841aae8253c97eb0bafc4bd64b2b7e799/goaloop/orchestrator.py)）、[LoopX](https://github.com/loopx-project/loopx)（[`turn_run_once.py`](https://github.com/loopx-project/loopx/blob/5cb9e4b8222a94a24a38ad2d7163e32322c11f73/loopx/cli_commands/turn_run_once.py)）、[Raven](https://github.com/EverMind-AI/Raven)（[Oncall 文档](https://evermind-ai.github.io/Raven/oncall/) 与 [`campaign.py`](https://github.com/EverMind-AI/Raven/blob/3632e6040c7038a60ec418ce39ccae185c72c19f/agents/raven-oncall/plugins/oncall-flow/oncall_flow/campaign.py)）。
- **编码运行时与可扩展 Harness**：[Codex SDK](https://learn.chatgpt.com/docs/codex-sdk)、[Codex App Server](https://learn.chatgpt.com/docs/app-server)、[Codex TS SDK 进程封装 `exec.ts`](https://github.com/openai/codex/blob/d36a3ead3c896d0552207763ef483262bce9ac73/sdk/typescript/src/exec.ts)、[Claude Code Headless](https://code.claude.com/docs/en/headless)、[Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk/overview)、[pi coding-agent](https://raw.githubusercontent.com/badlogic/pi-mono/main/packages/coding-agent/README.md)、[OpenClaw Gateway 架构](https://docs.openclaw.ai/concepts/architecture)、[Hermes Agent 架构](https://hermes-agent.nousresearch.com/docs/developer-guide/architecture)。
- **持久工作流、黑盒搜索与指标追踪**：[Temporal Activity Definition](https://docs.temporal.io/activity-definition)、[Optuna Ask-and-Tell](https://optuna.readthedocs.io/en/stable/tutorial/20_recipes/009_ask_and_tell.html)、[MLflow Tracking](https://mlflow.org/docs/latest/ml/tracking/)、[TPC-DS 官方主页](https://www.tpc.org/tpcds/)。

---

> **调研与源码基线说明**：本篇合并原「数据库优化研究平台」（上篇）与「自主性能闭环」（下篇）两份方案调研（初稿完成于 **2026-09-30**，**2026-10-08** 至 **2026-10-18** 完成源码路径与官方文档复核）。下篇对照分析的开源代码固定于以下提交快照：`Code Factory`（`595c35e`）、`GoaLoop`（`e1ea3e0`）、`LoopX`（`5cb9e4b`）、`Raven`（`3632e60`）及本地 `Codex` TypeScript SDK（`d36a3ea`）；其余 SDK、Temporal、Optuna 与 MLflow 等能力边界依据复核当日的官方文档整理。文中提出的 `Source/Event/Finding` 数据合同、双层状态机与 `DomainPack` 接口均为基于上述公开机制推演的系统架构设计，未部署竞品或运行端到端平台实测。
