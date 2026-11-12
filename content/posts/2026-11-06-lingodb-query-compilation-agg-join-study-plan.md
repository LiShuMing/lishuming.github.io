---
title: "【源码】LingoDB 学习报告：聚合、Join 如何变成查询专用程序"
slug: "lingodb-query-compilation-agg-join-study-plan"
date: 2026-11-06T00:00:00+08:00
lastmod: 2026-11-06T00:00:00+08:00
categories:
  - 数据库
tags:
  - LingoDB
  - MLIR
  - LLVM
  - Query Optimizer
  - Hash Join
  - Aggregation
description: "基于 LingoDB 固定版本源码与 Docker 实验，追踪聚合和 Join 从关系计划、子操作与并行状态到 LLVM 的转换，比较 PostgreSQL、StarRocks 的执行边界，并制定可验收的学习与开发计划。"
toc: true
math: false
---

阅读 PostgreSQL 和 StarRocks 的执行代码时，通常可以找到与执行计划（Execution Plan）对应的执行节点或算子（Operator）：扫描向上返回记录，或者 Driver 在算子之间传递一批列式数据。换到 LingoDB，沿着执行入口继续向下看，关系运算逐渐变成状态创建、查找、更新和合并，再变成循环、分支与内存访问。到实际执行时，计划中的聚合（Aggregation）不一定还对应一个运行时的聚合算子对象。

这带来一个更具体的问题：**SQL 中的聚合与 Join，如何经过编译变成查询专用程序？在这个过程中，优化器决定什么，LLVM 又负责什么？** 理解这一点，才能判断应该在哪一层分析问题、修改实现，以及怎样证明改动有效。

本文基于 LingoDB 提交 [`fab813e87b9461ca041cf2943d6b03e1f4d1d386`][lingodb-commit]，分析默认 CPU/LLVM 路径。材料包括源码阅读、Docker 中完成的构建与 TPC-H 基线，以及一个专门用于跟读的 Join＋聚合小实验。TPC-H 的数据规模因子（Scale Factor，SF）取 1，下文记为 SF1。基线和小实验是不同的运行；快照运行不用于性能结论。后文的开发方向属于规划，尚未实现。

## 1. 查询编译的关键：逐层暴露算法与状态

即时编译（Just-in-Time Compilation，JIT）意味着运行时再生成机器码，但“使用了 JIT”并没有说明生成代码的范围。只把金额表达式编译成一个函数，与把扫描后的计算、哈希检查和聚合状态更新放进同一条生成路径，覆盖的边界明显不同。

LingoDB 使用多层中间表示（Multi-Level Intermediate Representation，MLIR）逐步表达查询。中间表示（Intermediate Representation，IR）可以理解为供编译器分析和改写的程序；一次编译变换称为 pass，降低（Lowering）则是把较高层操作改写为更接近实际执行的操作。

![LingoDB 从关系计划到查询专用代码的转换，以及运行时参与的位置](/images/lingodb-query-compilation.svg)

在关系代数（Relational Algebra）层，编译器知道表、过滤、连接和分组语义，适合做谓词下推（Predicate Pushdown）、Join 重排等数据库优化。在子操作（SubOperator）层，表示进一步细化为数据流及其访问的状态：创建什么结构、按哪些键查找、读取哪些字段、如何更新和合并。执行步骤随后明确这些状态的生产者、消费者及依赖，再引入并行执行和线程本地状态。

这样的分层让“执行什么计算”和“用什么结构保存状态”可以分别变化。例如，逻辑上的 `Map` 可以特化为哈希表（Hash Table），Join 的 `MultiMap` 可以特化为缓冲区上的哈希索引，而上层仍然保留查找和更新的语义。[Execution.cpp][execution] 展示了关系优化、关系运算降低、子操作优化与并行化的实际顺序。

需要保留一个边界：生成代码仍然调用预编译的运行时（Runtime）。数据源访问、分配、哈希索引构建、部分合并、排序和结果组织仍有通用实现。当前默认后端也会生成和调用多个函数；不能把“查询编译”理解成整个查询及所有依赖都内联进一个 LLVM 函数。

## 2. 先用 PostgreSQL、StarRocks 校准比较尺度

三者最终都执行机器码。差别在于哪些逻辑在数据库构建时编译好，哪些逻辑根据当前查询再生成，以及运行时保留多大的通用执行框架。把“逐条”“向量化（Vectorization）”“JIT”当成互斥选项，会掩盖它们可以组合的事实。

| 比较维度 | PostgreSQL | StarRocks | 当前 LingoDB 默认 CPU 路径 |
| --- | --- | --- | --- |
| 主要执行骨架 | 执行节点拉取下一条记录 | Driver 在算子之间传递 Chunk | 生成执行步骤、回调和查询专用循环，配合运行时 |
| 常见数据单位 | 一条 tuple，即一条记录 | 一批列式数据 | 扫描以批为入口，生成代码处理批内记录 |
| 查询时的代码生成范围 | 表达式、记录解包及聚合更新相关逻辑 | 受支持的表达式子树 | 可以覆盖表达式、哈希操作和状态字段读写 |
| 跨关系运算的处理方式 | 保留执行节点驱动 | 保留列式算子流水线 | 在降低过程中把更多相邻计算组织进专用执行路径 |

PostgreSQL 的 [`ExecProcNode`][pg-exec] 根据执行节点的函数入口获取下一条记录；哈希聚合的 [`agg_fill_hash_table`][pg-agg] 保留读取输入、查找分组、推进聚合的通用 C 循环。不过它的 JIT 并不只处理简单算术：`ExecBuildAggTrans()` 会把聚合参数计算与状态更新函数调用组织成一个较大表达式，允许一起编译。外层节点驱动和哈希查找框架仍然保留。

StarRocks 的 [`PipelineDriver`][sr-driver] 检查相邻算子的 `has_output()` 与 `need_input()`，再执行 `pull_chunk()`、`push_chunk()`。同时，[表达式 JIT][sr-jit] 可以生成接收列数据、遍历一批行并写出结果列的 LLVM 函数。因此批量列式执行与 JIT 可以同时存在。

这里比较的是本地检出版本的代码机制。PostgreSQL 与 StarRocks 没有在本次工作中运行相同查询、相同数据和资源配置；不能据此给出三者的性能排名。对应源码版本在文末列出。

## 3. 聚合：把分组状态、逐行更新和局部合并分别表达

以 `SELECT k, SUM(v), COUNT(*) FROM t GROUP BY k` 为例，LingoDB 首先需要表达的是：每个 `k` 对应什么状态，输入一行如何更新，多个局部结果如何合并。这些信息明确之后，才继续选择具体状态结构与并行策略。

```text
Create Map<k, {sum, count}>
        │
输入流 → LookupOrInsert(k) → Reduce：更新状态
        │
Scan 聚合状态 → 输出
```

### 3.1 聚合函数在编译阶段构造更新逻辑

[`performAggregation()`][aggregation] 区分两条路径：没有分组键时创建一个 `SimpleState`，有分组键时创建 `Map`，再生成键比较、初始值和 `LookupOrInsert`。随后，`performAggrFuncReduce()` 生成逐行更新及局部状态合并的代码区域。

基本聚合函数通过 `DistAggrFunc` 提供 `createDefaultValue`、`aggregate`、`combine` 三类逻辑。对 `COUNT(*)`，它们分别是零、加一、两份计数相加。这里的 C++ 虚函数主要在编译阶段构造 IR，不能把这些调用直接理解为生成程序每处理一行的虚函数开销。

SQL 语义也在这些阶段保留下来。`COUNT(v)` 要跳过 NULL；可空 `SUM` 的初始状态要表达尚无有效输入，不能无条件用数值零代替。`AVG` 则在前面的[聚合简化 pass][simplify-aggr] 中改写为 `SUM / COUNT`，类型转换与可空性继续进入后续编译。

`DISTINCT` 聚合需要单独看。当前降低实现会按去重输入组织聚合分支，先对分组键加去重列去重，再计算聚合，最后按分组键汇合结果。这意味着研究多个 `COUNT(DISTINCT ...)` 时，需要同时观察去重状态、分支共享及后续汇合，不能只盯一个累加器。

### 3.2 并行化依赖状态可合并，而不只是给循环加线程

子操作层暴露了状态的读写及合并方式，编译器才有条件判断：哪些状态可以为每个线程创建一份，哪些更新需要协调，哪些结果可以在输入结束后合并。相关实现分布在 `ParallelizePass.cpp` 和 [`SpecializeParallelPass.cpp`][parallel-specialize]，并不是所有执行步骤都无条件并行。

当前预聚合路径会引入线程本地的 `PreAggregationHashtableFragment`。它具有一个固定的 1024 桶快速表，输出按哈希分为 64 个分区，这两个参数定义在[状态头文件][preaggr-header]中。其局部查找策略可以概括为：

```cpp
// 算法示意，省略类型、可空性和锁处理
candidate = fragment.table[bucket(hash(key))];
if (candidate && candidate.hash == hash && candidate.key == key) {
    update(candidate.state, row);
} else {
    entry = append_new_entry(key, initial_state);
    fragment.table[bucket(hash)] = entry;
    update(entry.state, row);
}
```

这是一种乐观预聚合（Optimistic Pre-Aggregation）：扫描阶段只检查桶中的快速候选，未命中就追加局部条目，不要求立即沿完整冲突链找到已经出现过的分组。旧条目仍保留在输出缓冲区中，因此同一个分组可以暂时存在多份状态。

最后，[运行时 merge][preaggr-runtime] 收集各线程的分区输出，在分区内建立哈希结构，使用生成的键比较和 `combine` 函数合并重复分组。局部阶段减少了查找工作，代价是可能产生更多条目、占用更多内存，并增加最终合并成本。

因此，这条路径的研究问题应该是“输入被局部预聚合压缩了多少，合并又花了多少”，而不是简单判断哈希表大小是否足够。分组数量很少、相邻输入经常命中相同键时，局部压缩可能较好；高基数或冲突频繁时，新增条目和合并成本可能上升。这是机制推导，仍需要不同数据分布下的实测。

## 4. Join：从多值映射到哈希索引，再连接下游计算

Join 的算法没有因为使用 MLIR 就消失。等值哈希连接（Hash Join）仍需要构建侧与探测侧，仍需要处理冲突、重复键和剩余条件；变化在于这些步骤被拆成可改写的子操作，而不是封装在一个不可见的执行函数里。

### 4.1 MultiMap 保留重复键语义

[`translateHJ()`][hash-join] 首先创建 `MultiMap`，把构建侧所需字段插入，再用探测侧的键执行 `Lookup`，随后通过 `NestedMap` 和 `ScanList` 遍历候选记录，读取字段、组合输入并计算剩余谓词。

`MultiMap` 的“多值”直接对应 SQL 语义。同一个键在两边分别出现两次，内连接需要产生四个匹配组合；只给每个键保存一个指针会丢失行。构建侧通常只保存后续所需的键和字段，也不必复制整张输入表的全部列。

相等条件可以用作哈希键，其他条件仍需要计算。哈希值相同也不能代替真实键相等；普通相等连接与允许 NULL 相等的比较方式还需要分别处理。外连接和半连接等变体继续有各自的匹配标记及输出语义，不能只替换输出字段就视为完成。

### 4.2 状态特化把存储记录与建立索引分开

默认启用的 [`MultiMapAsHashIndexedView`][subop-specialize] 会尝试把多值映射改成两部分：先把 `link`、`hash`、键及所需字段存入 Buffer，再通过 `CreateHashIndexedView` 给已有缓冲区建立哈希索引。探测阶段计算哈希，遍历候选链，再执行键检查和后续计算。

[`HashIndexedView::build()`][join-runtime] 展示了运行时的职责：根据缓冲区记录数分配桶数组，并行遍历记录，通过原子操作把条目接到桶链上。索引可以引用原有记录，减少再复制一套完整记录的需要；但缓冲区、桶数组和链访问本身仍有内存及缓存成本。

Join 匹配后的表达式与聚合更新，可以继续放在同一生成执行步骤中。因此，逻辑上存在“Join → 计算 → Agg”，不等于必须先物化完整 Join 结果，再交给下一个算子。

### 4.3 GroupJoin 进一步复用连接与聚合的状态访问

源码还有一条更强的变换：当 Join 后接按连接键聚合，并满足一侧连接键唯一等条件时，把它改写为 `GroupJoin`。普通流水线融合已经可以减少中间结果物化；这里进一步把聚合状态放入连接查找到的状态中，减少另一套分组结构及重复查找。

```text
普通路径：探测 Join 状态 → 找到匹配 → 查找聚合分组 → 更新
GroupJoin：查找到包含聚合状态的条目 → 更新
```

这个规则具有明确边界。[`OptimizeImplementations.cpp`][group-join-rule] 检查连接键类型、分组键对应关系和键唯一性等条件，并排除部分去重与外连接计数情形；代码中也有行数启发式。随后 `GroupJoinLowering` 创建带聚合字段的状态，在探测时更新。不能因为最终都能生成机器码，就忽略这些关系语义上的前提。

## 5. 优化器：规则、Join 搜索与实现特化分阶段合作

LingoDB 的优化器包含传统数据库的计划优化，也包含面向程序和状态结构的编译优化。要分析“为什么选这个实现”，需要先确定决策发生在哪一层，不能只看最终 LLVM 是否生成得足够好。

[`createQueryOptPipeline()`][relalg-passes] 串起关系优化顺序：聚合简化、表达式分解、隐式连接转换、过滤下推、子查询展开、元数据附着、Join 消除、分组键精简、Join 重排和实现选择等。这些步骤的顺序会影响后续规则能否识别模式。

Join 重排先建立查询图（Query Graph），估算关系及谓词的输出规模，再选择搜索方式。当前 [`OptimizeJoinOrder`][join-order] 在 `countSubGraphs(1000) < 1000` 时使用 `DPHyp`，按连接图做动态规划（Dynamic Programming）；达到该搜索规模阈值时转向 `GOO` 贪心优化（Greedy Operator Ordering）。1000 对应子图计数判断，不是表数量。

当前 `Plan` 的代价模型（Cost Model）的[定义][plan-cost]主要为：

```text
Cost(plan) = EstimatedRows(plan) + Σ Cost(child)
```

它偏向减少中间结果规模，但这里没有统一表达键宽、构建与探测的不同 CPU 成本、内存压力和状态合并成本。基数估计（Cardinality Estimation）会使用表行数、过滤样本及主键信息，也存在固定选择率和其他启发式；例如有分组键的聚合，在当前部分估计路径中直接沿用输入行数，仍有细化空间。

连接顺序确定后，实现选择还会识别等值键，设置 `useHashJoin` 等属性；满足索引及规模条件时，也可以采用索引嵌套循环连接（Index Nested Loop Join）。当前相关分支包含 `20 * numRowsRight < numRowsLeft` 这样的启发式，而不是把所有物理算法统一放进一个细粒度代价搜索。[`OptimizeImplementations.cpp`][implementation] 是分析此类决策的入口。

进入子操作层后，状态结构特化、局部复用、字段读取位置调整、执行步骤组织和并行化继续改写程序。LLVM 接手时，主要算法与数据流边界已经形成。它能优化已有循环和计算，却不能自动纠正一个导致巨大中间结果的 Join 顺序。

## 6. 实验怎样把这些层次连接起来

为了避免只凭源码名称理解实现，我在 Docker 中执行了下面的小例子。两边都有重复键，既可以核对 Join 匹配数，也方便追踪聚合状态。可下载[完整 SQL](/code/lingodb-study-20261106/demo.sql)和[实验记录](/code/lingodb-study-20261106/evidence.json)。

```sql
SELECT a.k, SUM(a.v * b.w) AS total, COUNT(*) AS n
FROM (VALUES (1, 10), (1, 20), (2, 7), (3, 9)) AS a(k, v)
JOIN (VALUES (1, 2), (1, 3), (2, 4)) AS b(k, w) ON a.k = b.k
GROUP BY a.k
ORDER BY a.k;
```

默认配置返回 `(1, 150, 4)`、`(2, 28, 1)`，与手工计算一致：键 1 的金额是 `(10 + 20) × (2 + 3)`，有四个组合；键 2 只有 `7 × 4` 一个组合。开启和关闭快照（Snapshot）均得到相同结果，开启快照的运行保留了 56 个阶段文件。

| 本次快照 | 观察到的变化 | 能建立的对应关系 |
| --- | --- | --- |
| 22，`relalg-optimize-implementations` | Join 标注 `impl = "hash"`，后面保留 Aggregation | 关系层已经选择哈希实现 |
| 27，`to-subop` | Join 变成 MultiMap、插入、查找、列表扫描；Agg 变成 Map、查找或插入、Reduce | 关系运算已经分解成状态操作 |
| 31，`subop-specialize` | Join 使用 Buffer＋HashIndexedView；Agg 使用 HashMap | 状态表示继续特化 |
| 37，`subop-specialize-parallel` | 出现线程本地片段与 Merge；Join 匹配后的乘法和聚合更新处于同一执行步骤 | 并行状态及相邻计算在此阶段连接起来 |
| 41，`lower-subop-to-cf` | 生成循环、分支、字段访问及运行时调用 | 子操作逐步变成可执行程序 |

快照序号只对这次运行成立。其他查询应读取 `detailed-snapshot-info.json`，按 pass 名称查找对应文件。最后的 LLVM 方言快照还可以用 `mlir-translate --mlir-to-llvmir` 转成文本 LLVM IR；该文本在运行时 LLVM 优化之前，不能标为最终机器码。

### 6.1 一次关闭特化的尝试，暴露了非默认路径的失败

默认 `LINGODB_SUBOP_OPT` 为 `ReuseLocal,Specialize,PullGatherUp,Compression`。我只去掉其中的 `Specialize`，保留其他条目，原本想观察状态结构变化。Join 随后进入 `HashMultiMap` 路径，在降低到 LLVM 时失败：

```text
'llvm.getelementptr' op expected index 1 indexing a struct to be constant
BACKEND: Could not lower module to llvm dialect
```

关闭快照后再次运行仍然失败，两次退出码均为 1；默认配置的两次运行均成功。失败前的控制流快照已出现异常的状态字段索引，这是定位线索，尚不足以确定是哪一个映射或转换造成的，也没有完成修复。

这里“关闭 Specialize”也不等于禁止所有类型转换：`Map → HashMap`、`MultiMap → HashMultiMap` 等基础变换仍然执行，只是没有采用可选的 HashIndexedView 特化。这个实验说明了一个具体的开发边界：**比较优化开关的收益之前，必须先确认两条路径都能执行并返回正确结果。** 失败分支不能记成零耗时、缺失样本或一次成功的性能对照。

### 6.2 SF1 基线展示了编译成本，但没有给出优化收益

另一次独立的 TPC-H SF1 基线使用 Release 构建、Ubuntu 24.04、Clang/LLVM/MLIR 20.1.8 和 Arrow 24.0.0。默认 CPU/LLVM 后端开启 4 个工作线程，进程绑定同一非统一内存访问（Non-Uniform Memory Access，NUMA）节点的四个不同物理核心；CPU 为 Xeon Platinum 8163，共享开发机没有独占核心。数据中 `lineitem` 有 6,001,215 行。

22 条查询各预热 3 次、测量 10 次，预热 66/66、测量 220/220 完成，没有失败或缺失轮次。独立执行的 SF1 结果校验覆盖 22 条查询。测量关闭快照，每次请求仍重新编译，数据库启动装载、数据生成和导入不计入正式采样。

| 指标 | Q1 的 10 次中位数 | 22 条查询各自中位数之和 |
| --- | ---: | ---: |
| 引擎执行时间 | 32.688 ms | 646.950 ms |
| 引擎各编译阶段计时之和 | 46.035 ms | 1287.708 ms |
| 客户端往返时间 | 83.657 ms | 2056.228 ms |

右列不是整套查询一次运行的墙钟时间；各指标独立取中位数，也不保证严格相加。引擎执行时间不包含全部格式化与传输开销，客户端往返口径更宽。部分查询存在较明显波动，例如 Q12 执行时间的变异系数（Coefficient of Variation，CV）约为 38.7%。

这组数据支持把编译延迟纳入优化目标，尤其是小规模查询；它不能证明 LingoDB 比其他系统快，也没有候选版本对照。SF1 数据已预热、共享主机及线程配置等条件都限制了外推范围，这也不是官方 TPC-H Power 或 Throughput 成绩。

## 7. 学习与开发规划：每个阶段都交付可核对的材料

后续工作按依赖拆成四个阶段。阶段长度取决于问题定位，不预先承诺固定周数；推进条件是上一阶段的材料足以支持下一项判断。

| 阶段 | 具体工作 | 交付与验收条件 |
| --- | --- | --- |
| 一：建立跨层对应关系 | 用本文小 SQL 跟读，再分别跟 Q1 聚合和一条包含 Join 的 TPC-H 查询 | 保存 SQL、版本、配置、pass 索引和关键片段；能解释每个状态的创建、更新、消费与释放位置 |
| 二：修复一个有边界的失败 | 定位关闭 Specialize 后的 HashMultiMap 降低失败 | 最小复现、首次出现错误的 IR、根因、针对性修复与回归；默认路径和非默认路径均验证 |
| 三：建立状态成本模型 | 分析预聚合命中、局部新增条目和 merge；检查 Q1 重复状态的来源 | 单独统计扫描、合并、峰值内存和编译成本；形成可被反例推翻的优化假设 |
| 四：改进计划选择 | 选择一个确定的基数误估或 Join 实现选择问题，比较候选计划 | 估计与实际行数、计划差异、结果一致性和受控运行对比完整；保留无收益或退化案例 |

### 7.1 第一个开发题目选择非默认 Join 路径

HashMultiMap 失败已有很小的输入、成功路径和失败快照，适合练习跨层调试。先跟踪插入操作的键、值成员映射，找到状态字段索引第一次不合法的位置，再决定修复应该落在状态特化、子操作降低还是更底层的转换中。

测试应分别证明程序结构与 SQL 结果。结构测试可参考 `test/lit/RelAlg/lowering.mlir` 的输入和 FileCheck 断言；执行结果可参考 `test/sqlite-small/join.test`、`groupby.test`。除了本例的重复键，还要补足无匹配、空输入、NULL 和单/多线程情形。修复一个示例后，不能立即宣称所有 Join 变体或所有关闭特化的查询已经通过。

### 7.2 聚合研究先建立实验矩阵，再调整常量

预聚合值得测试的假设是：固定快速表的命中情况决定了扫描阶段压缩率，而局部重复条目会把成本转移到最终合并。可以先固定输入行数、类型和线程，改变分组数与键分布；随后只改变线程数，观察局部状态与合并成本如何增长。

| 变量 | 首轮实验安排 | 主要观察 |
| --- | --- | --- |
| 不同分组数 | 例如固定百万行，分组数从少量增加到接近输入行数；具体规模按资源调整 | 新增条目数、局部压缩率、merge 时间 |
| 键分布 | 均匀、少量热点、相同键连续出现 | 快速候选命中率与线程负载 |
| 并行度 | 固定相同数据，比较 1、2、4 个工作线程 | 局部状态总量、合并成本、执行时间 |
| 键及状态宽度 | 在前一组稳定后分别改变键宽和聚合字段数 | 内存、缓存行为及字段访问成本 |

上述是待执行的实验安排，没有测量结果。1024 桶和 64 分区也不是可以只改一个头文件的独立参数：编译器降低代码还包含配套的桶索引和分区计算，修改前必须核对两侧约定。

另一个候选是聚合状态共享。Q1 的已导出 IR 中可以看到 `SUM` 与 `AVG` 拆解后仍保留部分重复累加字段，可以研究是否能够共享。但表达式相同不等于状态一定可合并，还需要核对输入范围、类型、NULL、去重、过滤条件及并行合并语义。先证明等价，再看状态字节数和更新次数是否下降。

### 7.3 优化器研究从一个估算误差开始

与其一开始替换整个搜索框架，更适合先选择一条输出规模估计明显失真的过滤、分组或连接边，记录估计行数与实际行数，再观察误差是否改变 Join 顺序、构建侧或实现选择。

只有确认主要误差来自基数估计，才投入不同值数量（Number of Distinct Values，NDV）、列相关性或分组数估计；如果估计接近实际，却仍选到较差实现，才进一步研究键宽、哈希构建和探测成本、局部预聚合和 merge 成本如何进入代价模型。这样可以避免用更复杂的模型掩盖错误输入。

性能采样关闭快照，固定数据、统计样本、线程及核心配置，多轮记录编译和执行时间。快照能解释发生了什么变换，结构测试能验证预期变换，SQL 校验能证明指定输入上的结果，受控运行才能支持性能判断；这些材料需要一起保存，但各自回答不同的问题。

## 8. 可直接开始的跟读方法与源码索引

在已经构建好当前版本的 Linux 开发环境中，可以把下载的 `demo.sql` 放在仓库根目录，选择一个已初始化、允许执行只读查询的数据库目录，再创建独立输出目录运行。下面的构建路径来自本次容器配置，其他环境需要替换；命令不会负责安装依赖或初始化数据库。

```bash
# 在源码根目录执行；构建产物需与当前容器工具链匹配
study_root="$PWD"
study_bin="$study_root/build/lingodb-container-release"
study_db="$study_root/build/assessment/smoke-db" # 替换为已有数据库目录
study_out=$(mktemp -d "$study_root/build/lingodb-study-XXXXXX")
mkdir -p "$study_out/snapshots"
cd "$study_out"

LINGODB_PARALLELISM=4 \
LINGODB_EXECUTION_MODE=DEFAULT \
LINGODB_SUBOP_OPT=ReuseLocal,Specialize,PullGatherUp,Compression \
LINGODB_SNAPSHOT_PASSES=true \
LINGODB_SNAPSHOT_DIR="$study_out/snapshots" \
"$study_bin/run-sql" "$study_root/demo.sql" "$study_db"
```

首次阅读按 `detailed-snapshot-info.json` 找关系优化、`to-subop`、特化、并行化和控制流降低的边界。调试时先找到相邻两份快照中不变量第一次被破坏的位置；决定修改层次后，再阅读对应 pass。仅用 `mlir-db-opt` 重放某个优化时，还要核对目录元数据与配置：快照序列化会临时去掉表上的 `meta` 对象，不能假设文本快照天然包含原查询全部统计信息。

| 问题 | 固定版本源码入口 |
| --- | --- |
| 整体编译与执行顺序 | [Execution.cpp][execution]、[LLVMBackends.cpp][llvm-backend] |
| 关系优化 pass 顺序与聚合简化 | [RelAlg/Passes.cpp][relalg-passes]、[SimplifyAggregations.cpp][simplify-aggr] |
| 聚合与 Join 如何拆成子操作 | [RelAlgToSubOp.cpp][aggregation]，搜索 `performAggregation`、`performAggrFuncReduce`、`translateHJ`、`GroupJoinLowering` |
| 子操作、状态及引用类型的定义 | [SubOperatorOps.td][subop-definitions] |
| 状态结构特化 | [SpecializeSubOpPass.cpp][subop-specialize] |
| 执行步骤及并行状态 | [OrganizeExecutionStepsPass.cpp][execution-steps]、[ParallelizePass.cpp][parallelize]、[SpecializeParallelPass.cpp][parallel-specialize] |
| 子操作降低为循环及内存访问 | [SubOpToControlFlow.cpp][subop-to-cf] |
| 预聚合及 Join 索引的运行时算法 | [PreAggregationHashtable.cpp][preaggr-runtime]、[LazyJoinHashtable.cpp][join-runtime] |
| Join 搜索、估算、代价与实现选择 | [OptimizeJoinOrder.cpp][join-order]、[QueryGraph.cpp][query-graph]、[utils.h][plan-cost]、[OptimizeImplementations.cpp][implementation] |

对比源码的版本分别是 PostgreSQL [`126976a8ffcfa47f40d3df09243ebf4606001912`](https://github.com/postgres/postgres/commit/126976a8ffcfa47f40d3df09243ebf4606001912) 和 StarRocks [`f977b53c13eab90425f552675f3b3463ada8b2fc`](https://github.com/StarRocks/starrocks/commit/f977b53c13eab90425f552675f3b3463ada8b2fc)，不代表所有发行版本的实现范围。

本地原始材料保存在 LingoDB 仓库的 `build/assessment/agg-join-walkthrough-20261106/`、`build/assessment/q1-codegen-20261106/` 与 `build/assessment/tpch-sf1-baseline-20261106/`，详细基线报告位于 `docs/tpch-sf1-baseline-2026-11-06.md`。这些本地路径用于定位本次实验，不是博客读者下载完整构建产物的入口；本文只附带小例子的 SQL 和运行状态记录。

这套实现适合研究的，是关系语义如何约束状态变换，以及编译器如何把算法组合成执行程序。下一步先修复有明确边界的失败，再用可解释的状态计数和受控实验决定优化方向；在拿到完整证据之前，编译成功、计划更小或某个例子更快，都只支持各自范围内的判断。

[lingodb-commit]: https://github.com/lingo-db/lingo-db/commit/fab813e87b9461ca041cf2943d6b03e1f4d1d386
[execution]: https://github.com/lingo-db/lingo-db/blob/fab813e87b9461ca041cf2943d6b03e1f4d1d386/src/execution/Execution.cpp#L44
[llvm-backend]: https://github.com/lingo-db/lingo-db/blob/fab813e87b9461ca041cf2943d6b03e1f4d1d386/src/execution/LLVMBackends.cpp#L795
[aggregation]: https://github.com/lingo-db/lingo-db/blob/fab813e87b9461ca041cf2943d6b03e1f4d1d386/src/compiler/Conversion/RelAlgToSubOp/RelAlgToSubOp.cpp#L2128
[simplify-aggr]: https://github.com/lingo-db/lingo-db/blob/fab813e87b9461ca041cf2943d6b03e1f4d1d386/src/compiler/Dialect/RelAlg/Transforms/SimplifyAggregations.cpp#L160
[parallel-specialize]: https://github.com/lingo-db/lingo-db/blob/fab813e87b9461ca041cf2943d6b03e1f4d1d386/src/compiler/Dialect/SubOperator/Transforms/SpecializeParallelPass.cpp#L23
[preaggr-header]: https://github.com/lingo-db/lingo-db/blob/fab813e87b9461ca041cf2943d6b03e1f4d1d386/include/lingodb/runtime/PreAggregationHashtable.h#L9
[preaggr-runtime]: https://github.com/lingo-db/lingo-db/blob/fab813e87b9461ca041cf2943d6b03e1f4d1d386/src/runtime/PreAggregationHashtable.cpp#L46
[hash-join]: https://github.com/lingo-db/lingo-db/blob/fab813e87b9461ca041cf2943d6b03e1f4d1d386/src/compiler/Conversion/RelAlgToSubOp/RelAlgToSubOp.cpp#L1097
[subop-specialize]: https://github.com/lingo-db/lingo-db/blob/fab813e87b9461ca041cf2943d6b03e1f4d1d386/src/compiler/Dialect/SubOperator/Transforms/SpecializeSubOpPass.cpp#L36
[join-runtime]: https://github.com/lingo-db/lingo-db/blob/fab813e87b9461ca041cf2943d6b03e1f4d1d386/src/runtime/LazyJoinHashtable.cpp#L13
[group-join-rule]: https://github.com/lingo-db/lingo-db/blob/fab813e87b9461ca041cf2943d6b03e1f4d1d386/src/compiler/Dialect/RelAlg/Transforms/OptimizeImplementations.cpp#L771
[relalg-passes]: https://github.com/lingo-db/lingo-db/blob/fab813e87b9461ca041cf2943d6b03e1f4d1d386/src/compiler/Dialect/RelAlg/Passes.cpp#L17
[join-order]: https://github.com/lingo-db/lingo-db/blob/fab813e87b9461ca041cf2943d6b03e1f4d1d386/src/compiler/Dialect/RelAlg/Transforms/OptimizeJoinOrder.cpp#L108
[plan-cost]: https://github.com/lingo-db/lingo-db/blob/fab813e87b9461ca041cf2943d6b03e1f4d1d386/include/lingodb/compiler/Dialect/RelAlg/Transforms/queryopt/utils.h#L153
[implementation]: https://github.com/lingo-db/lingo-db/blob/fab813e87b9461ca041cf2943d6b03e1f4d1d386/src/compiler/Dialect/RelAlg/Transforms/OptimizeImplementations.cpp#L493
[query-graph]: https://github.com/lingo-db/lingo-db/blob/fab813e87b9461ca041cf2943d6b03e1f4d1d386/src/compiler/Dialect/RelAlg/Transforms/queryopt/QueryGraph.cpp#L299
[subop-definitions]: https://github.com/lingo-db/lingo-db/blob/fab813e87b9461ca041cf2943d6b03e1f4d1d386/include/lingodb/compiler/Dialect/SubOperator/SubOperatorOps.td
[execution-steps]: https://github.com/lingo-db/lingo-db/blob/fab813e87b9461ca041cf2943d6b03e1f4d1d386/src/compiler/Dialect/SubOperator/Transforms/OrganizeExecutionStepsPass.cpp
[parallelize]: https://github.com/lingo-db/lingo-db/blob/fab813e87b9461ca041cf2943d6b03e1f4d1d386/src/compiler/Dialect/SubOperator/Transforms/ParallelizePass.cpp
[subop-to-cf]: https://github.com/lingo-db/lingo-db/blob/fab813e87b9461ca041cf2943d6b03e1f4d1d386/src/compiler/Conversion/SubOpToControlFlow/SubOpToControlFlow.cpp#L3065
[pg-exec]: https://github.com/postgres/postgres/blob/126976a8ffcfa47f40d3df09243ebf4606001912/src/include/executor/executor.h#L322
[pg-agg]: https://github.com/postgres/postgres/blob/126976a8ffcfa47f40d3df09243ebf4606001912/src/backend/executor/nodeAgg.c#L2627
[sr-driver]: https://github.com/StarRocks/starrocks/blob/f977b53c13eab90425f552675f3b3463ada8b2fc/be/src/exec/runtime/pipeline_driver.cpp#L413
[sr-jit]: https://github.com/StarRocks/starrocks/blob/f977b53c13eab90425f552675f3b3463ada8b2fc/be/src/exprs/jit/jit_engine.cpp#L75
