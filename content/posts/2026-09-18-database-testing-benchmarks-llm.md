---
title: "【论文】数据库测试与 Benchmark：从开源数据集、语义反例到 LLM 驱动的引擎边界探索"
date: 2026-09-18T00:00:00+08:00
lastmod: 2026-09-18T00:00:00+08:00
slug: "database-testing-benchmarks-llm"
categories:
  - 数据库
tags:
  - 数据库测试
  - Benchmark
  - SQLancer
  - Fuzzing
  - 查询优化器
  - LLM
description: "梳理 TPC、JOB、ClickBench、SQLStorm、SQLancer、Velox Fuzzer 等公开工作，讨论如何联合构造数据、查询、执行状态与判定器，发现数据库正确性错误、性能悬崖及 corner case，并用 LLM 扩展引擎能力边界的测试覆盖。"
draft: false
---

读数据库论文时，实验部分经常从 TPC-H、TPC-DS、JOB 开始；开发数据库时，回归目录里则是一批 SQL、expected result 和历史 bug。两边都在执行查询，却回答着不同的问题：前者通常关心某种负载下系统表现如何，后者关心某条语义承诺是否仍然成立。

当一个引擎已经能跑完 TPC-DS，支持 Array、Map、JSON，也接入向量检索后，真正困难的问题才出现：**怎样知道这些功能在组合使用、物理路径切换和异常状态下仍然正确？怎样构造输入，让隐藏在“通常成立”背后的假设失效？**

这与前面讨论的 [Dynamic Filter 穿越 Outer Join]({{< relref "2026-08-24-dynamic-filter-outer-join.md" >}}) 是同一个问题。一个过滤器在大多数数据上都有效，并不能证明它不会在“原本匹配的右行被过滤，随后补出的 NULL 又通过上层谓词”时改变结果。触发这种错误可能只需要两行数据。

本文沿着三个问题组织调研：公开数据集和工具分别提供了什么；如何把它们变成能判错的测试集；LLM 能怎样帮助我们跨过手写生成器的覆盖边界。资料核对截至 **2026-09-18**，采用论文原文、作者页面和项目文档。后文的边界矩阵、测试契约与实施路线是基于这些工作的工程设计，不是某篇论文已经实现的统一框架。

## 核心判断

1. **测试集的单位应该是可重放的实验，而不只是一份数据文件。** Schema、数据、SQL、统计信息、配置、执行历史和判定器共同决定测试是否有效。
2. **Benchmark 提供负载骨架，最小反例提供语义分辨率。** 大规模、复杂 SQL 和真实数据分别有价值，但都不能替代对 NULL、重复值、空输入和状态转移的定向构造。
3. **生成器和判定器是两个独立问题。** 能生成合法 SQL，不等于知道它是否返回正确结果；能发现 crash，也不等于能发现 silent wrong result。
4. **引擎边界通常是一个条件组合。** “支持嵌套类型”需要进一步展开为“在哪些算子、编码、深度、内存与执行模式下支持”。
5. **LLM 最有价值的工作是扩展假设空间。** 从文档和源码提取约束、补充 SQL 片段、设计破坏前提的输入，再交给可执行的 oracle 判定；模型口头认可不能成为 correctness oracle。
6. **覆盖应沿着语义、计划与运行状态共同计量。** 新增一万条相同计划的 SQL，可能不如新增一个确实进入 spill 分支的测试。

## 一、先区分四类经常被混称为“数据集”的资产

设一次测试为：

```text
Test = (Schema, Data, Query, State, Configuration, Environment, Oracle)

Schema        类型、约束、索引、分区、视图
Data          值域、重复度、相关性、排序、文件和物理编码
Query         SQL / API 调用 / 事务程序
State         统计信息、缓存、版本、提交历史、索引或 MV 刷新进度
Configuration 优化规则、并行度、内存预算、执行模式
Environment   引擎版本、硬件、依赖、时区、排序规则、故障序列
Oracle        预期结果、等价关系、状态不变量或性能判定条件
```

据此，公开资产可以分为四类。

| 资产 | 典型内容 | 能直接回答什么 | 还需要补什么 |
|---|---|---|---|
| 数据语料 | CSV、Parquet、文档、向量、图 | 输入是否具有目标分布或编码特征 | 查询、语义契约、判定器 |
| Benchmark workload | Schema、生成器、查询模板、驱动和指标 | 特定负载下的吞吐、延迟与资源开销 | 定向语义反例、异常状态、覆盖证明 |
| Regression corpus | 初始化 SQL、查询、预期结果、历史 reproducer | 已知行为是否回退 | 新功能组合、未知缺陷的探索 |
| Testing framework | 生成器、mutator、oracle、reducer | 能否持续产生并缩减失败实验 | 引擎适配、能力模型、资源预算 |

例如，一份 Parquet 数据可以同时用于扫描 benchmark 和格式兼容性测试，但前者通常关心吞吐，后者关心 nested null、decimal metadata 或损坏页是否按契约处理。文件相同，不意味着测试目标相同。

还要区分**可公开下载、代码开源、数据可再分发**。TPC 的规范与工具、IMDb 派生数据、Stack Exchange 数据、论文 artifact 各有自己的使用条件；不能因为一个 GitHub 仓库使用 MIT，就把它引用的所有数据一起视为 MIT 数据集。这里将它们统称为“公开可获取资产”，具体复用要记录各自来源与许可。

## 二、公开 Benchmark 与测试语料：应该怎样选

### 2.1 关系型与事务负载

| 工作与入口 | 公开资产及主要用途 | 最值得保留的特征 | 测试时需要补齐的边界 |
|---|---|---|---|
| [TPC-H / TPC-DS](https://tpc.org/TPC_Documents_Current_Versions/current_specifications5.asp) | 官方规范、数据和查询生成工具；决策支持负载 | 可控规模、固定业务关系、复杂分析查询 | 模板之外的 SQL 组合、特殊值、异常历史；修改后的 workload 应说明是派生实验 |
| [Join Order Benchmark，JOB](https://github.com/gregrahn/join-order-benchmark) | IMDb 派生关系数据入口、schema、113 条查询 | 多表连接和真实数据相关性 | Outer Join、嵌套类型、DML、恢复；固定 IMDb 快照，不能随意换成今日数据后沿用旧结果 |
| [ClickBench](https://github.com/ClickHouse/ClickBench) | 数据下载入口、各系统 DDL/查询/运行脚本和结果 | 宽表分析、扫描、过滤、聚合及字符串处理 | 多表关联、事务、复杂状态；统一冷暖缓存和加载成本的口径 |
| [SQLStorm](https://github.com/SQL-Storm/SQLStorm) | LLM 生成查询、数据准备及 benchmark 工具 | 更丰富的分析 SQL 与计划形态 | 方言差异、非确定性、筛选偏差；不能把所有跨引擎差异直接记为 bug |
| [YCSB](https://github.com/brianfrankcooper/YCSB) | 可配置 workload、数据生成和数据库 bindings | 读写比例、键访问分布和并发压力 | SQL 代数、事务隔离与复杂查询语义 |
| [BenchBase](https://github.com/cmu-db/benchbase) | 基于 JDBC 的多数据库 benchmark 框架，包含多种事务负载 | 统一驱动、事务组合和执行统计 | 事务历史判定、故障注入；事务吞吐高不代表隔离级别实现正确 |

这些资产适合作为起点，但不应该只按“能跑哪些 benchmark”选择。要先明确正在测试的失效机制：Join order 适合从 JOB 出发，Scan/表达式适合从 ClickBench 出发，事务调度需要带历史的 workload，优化规则的 NULL 语义往往更适合自己构造小表。

TPC-H 和 TPC-DS 的价值也不只在总耗时。不过，把生成参数改成极端倾斜、把 schema 改成 nullable 或新增自定义查询后，得到的是**从 TPC 派生的测试负载**，不能据此宣称获得符合官方规范的 TPC 成绩。

### 2.2 复杂类型、文件、图与检索

| 工作与入口 | 可以复用的资产 | 适合检验 | 不能直接替代 |
|---|---|---|---|
| [DuckDB sqllogictest](https://duckdb.org/docs/current/dev/sqllogictest/overview) | SQL 测试格式与引擎测试实践 | 功能回归、结果与预期错误 | 自动发现所有新 bug 的生成器 |
| [Apache parquet-testing](https://github.com/apache/parquet-testing) | `data`、`bad_data`、Variant 等样例 | 格式互操作、特殊编码、损坏输入处理 | 查询优化器的语义验证 |
| [Velox Aggregation / Window Fuzzer](https://facebookincubator.github.io/velox/develop/testing/fuzzer.html) | 算子生成、计划变体与验证工具 | 聚合、窗口以及不同执行路径的一致性 | 完整 DBMS 的事务和恢复 |
| [LDBC SNB](https://github.com/ldbc/ldbc_snb_interactive_v2_impls) | 规范、数据生成器入口、图查询和更新实现 | 图结构、相关性、交互负载 | 关系引擎所有 SQL 语义；不同 workload/version 应分别固定 |
| [ANN-Benchmarks](https://github.com/erikbern/ann-benchmarks) | 向量数据、ground truth、算法适配及评测代码 | recall 与查询性能的权衡 | 带过滤、更新和权限的完整向量数据库语义 |
| [BEIR](https://github.com/beir-cellar/beir) | 多领域文档、query 和 relevance judgments | 文本检索质量及跨领域变化 | 存储、索引可见性、事务或 SQL 正确性 |

这里有两个容易忽略的区别。**Array、Map、Struct 是有明确类型结构的数据；JSON/Variant 通常是半结构化数据；文本、图像、音频才涉及更开放的内容解释。向量则已经是定长数值表示。**它们的输入契约和 oracle 不应混成一个“非结构化类型测试”。

另一个区别是公开程度。[SQLite 的测试说明](https://www.sqlite.org/testing.html) 同时介绍了公开测试和 TH3 等资产，但 TH3 是专有测试套件。不能把 SQLite 的全部测试能力写成一个可以直接下载的开源 corpus。

### 2.3 怎样把现成 workload 变成自己的测试集

我更倾向于保留三层资产：

```text
原始基线：固定快照、工具版本和查询，保留可比性
    ↓ 定向扰动
探索集合：改变值域、相关性、物理路径与历史，寻找失败
    ↓ 缩减与解释
回归集合：最小数据 + 必要配置 + 可执行判定 + 缺陷说明
```

原始基线用于比较趋势，探索集合可以不断扩张，回归集合则要尽量小而稳定。把几百万随机 SQL 永久塞进每次提交的 CI，通常只会增加成本；更好的做法是把它们发现的**不同失效机制**沉淀下来。

## 三、学术测试路线：先解决“谁来判断结果对不对”

### 3.1 从 crash oracle 到逻辑 oracle

[SQLancer](https://github.com/sqlancer/sqlancer) 将 schema/data/query 生成与 test oracle 分开，集成了 PQS、NoREC、TLP、DQP、CODDTest 等方法，也提供基于查询计划的生成反馈。它是一个测试框架，不是一份固定数据集；不同 provider 的语法和 oracle 覆盖并不相同。

几条代表性路线的区别如下。年份对应所链接工作或项目说明，不意味着这些技术只能用于当年的版本。

| 路线 | 判定依据 | 擅长发现 | 关键限制 |
|---|---|---|---|
| [SQLsmith](https://github.com/anse1/sqlsmith) | 随机生成类型和 schema 感知的 SQL，观察异常 | crash、断言和内部错误 | 没有独立结果 oracle 时，无法判断正常返回的错误结果 |
| [SQUIRREL，2020](https://arxiv.org/abs/2006.02398) / [SQLRight，2022](https://github.com/PSU-Security-Universe/sqlright) | 结构化变异、合法性修复、覆盖反馈；SQLRight 结合逻辑 oracle | 更深的执行路径和逻辑缺陷 | 代码覆盖高不等于语义组合覆盖全 |
| [PQS，OSDI 2020](https://www.usenix.org/conference/osdi20/presentation/rigger) | 生成必须包含某个 pivot row 的查询 | 漏行等逻辑错误 | 需要在测试器中理解表达式语义；只验证保证的性质 |
| [NoREC，ESEC/FSE 2020](https://arxiv.org/abs/2007.08292) | 优化查询与较难被优化的参照查询一致 | 过滤相关优化错误 | 不覆盖任意查询；两条路径仍可能共享错误 |
| [TLP，OOPSLA 2020](https://www.research-collection.ethz.ch/bitstream/20.500.11850/456967/2/3428279.pdf) | TRUE/FALSE/UNKNOWN 分区后重组结果 | 三值逻辑及相应查询变换错误 | 聚合、DISTINCT、窗口需要各自合法的分解方式 |
| [DQP，SIGMOD 2024](https://nus-test.github.io/publication/2024-sigmod-dqp/) | 同一查询用不同执行计划，结果一致 | 计划和算子实现差异 | 计划必须真的不同；相同公共路径上的 bug 可能漏掉 |
| [CODDTest，SIGMOD 2025](https://github.com/sqlancer/sqlancer/pull/1054) | 固定状态下，表达式/子查询与其求值结果替换前后一致 | 常量折叠、传播及相关语义错误 | 要保证作用域、类型、相关绑定和求值时机合法 |
| [EET，OSDI 2024](https://www.usenix.org/conference/osdi24/presentation/jiang) | 等价表达式变换前后结果一致 | 表达式求值和优化错误 | 等价性依赖适用的 SQL 语义与前提 |

DQP 与 CODDTest 的论文和实现入口见 [SQLancer approaches](https://github.com/sqlancer/sqlancer#approaches-and-papers)。它们共同说明：无需给每条随机 SQL 手写完整答案，也可以通过**应当成立的关系**发现错误。

### 3.2 TLP：把三值逻辑变成可执行判定

给定确定性的谓词 `p`，每一行恰好属于 `p` 为 TRUE、FALSE、UNKNOWN 中的一类。对适合直接分区的查询，可以验证 bag 等价：

```sql
-- 原始结果
SELECT x FROM t;

-- 分区重组；必须保留重复值
SELECT x FROM t WHERE x > 0
UNION ALL
SELECT x FROM t WHERE NOT (x > 0)
UNION ALL
SELECT x FROM t WHERE (x > 0) IS NULL;
```

令 `t.x = [NULL, -1, 0, 1, 1]`，两侧都应返回这五个值。去掉 UNKNOWN 分区会丢失 NULL；把 `UNION ALL` 改成 `UNION` 会丢失重复的 `1`。因此，这个小表同时检验了三值逻辑和 bag semantics。

不能把这个公式机械地套在 `LIMIT`、窗口、volatile function 或所有聚合外面。`AVG` 的分区合并需要 sum/count，而不是平均各分区的平均值；`COUNT(DISTINCT)` 也不能简单相加。测试器本身必须携带变换的前提。

### 3.3 NoREC：把过滤从 WHERE 移到表达式求值

以下是用于说明思路的 NoREC 风格查询，而非某个 provider 的原样实现：

```sql
SELECT COUNT(*) FROM t WHERE x > 0;

SELECT COALESCE(SUM(CASE WHEN x > 0 THEN 1 ELSE 0 END), 0)
FROM t;
```

第一条可走过滤优化，第二条逐行计算谓词并累计真值。上述输入下二者都为 `2`。这里的 `COALESCE` 很重要：空表上的 `COUNT(*)` 为 `0`，而 `SUM` 为 NULL。

这只是针对一个受限过滤问题构造参照。不能因为两条 SQL 相同就证明整个引擎正确，也不能假设关闭一个 optimizer 开关就得到“完全未优化的参考引擎”。还要观察计划，确认待测规则或执行路径确实形成差异。

### 3.4 形式语义参照：减少“两个实现一起错”的盲区

[Semantic Conformance Testing of Relational DBMS / SemConT](https://arxiv.org/abs/2406.09469) 将 SQL 语义形式化并用 Prolog 实现参照，按语义覆盖生成测试。它与“拿 PostgreSQL 当正确答案”的区别在于，判定依据来自显式的语义模型。

这种方式对一个受限 SQL 子集很有吸引力，但仍然需要明确模型覆盖的构造及目标方言。SQL 标准、产品文档和历史兼容行为之间可能存在差异；一个 mismatch 首先说明“不一致”，还需要依据目标引擎的契约判断是不是缺陷。

工程上可以组合三种证据：同引擎等价变换、跨实现差分、受限语义模型。它们减少彼此盲区，却都不构成对无限输入空间的证明。

### 3.5 性能与事务需要另外的 oracle

[APOLLO](https://github.com/sslab-gatech/apollo) 面向数据库性能回退的生成、缩减和定位。[CERT](https://arxiv.org/abs/2306.00355) 则利用“限制更强的查询不应获得更大的估计基数”这样的预期寻找 CE 异常线索。后者不等于证明实际执行变慢：统计估计违反预期后，仍需要检查计划与运行数据。

事务方面，[Jepsen](https://github.com/jepsen-io/jepsen) 提供带故障注入的分布式测试框架，[Elle](https://github.com/jepsen-io/elle) 根据可观察历史推导依赖关系并检测事务异常；[Graph-Based Oracle Construction，OSDI 2023](https://www.usenix.org/conference/osdi23/presentation/jiang) 也研究如何为事务缺陷构造判定依据。此时输入已经不是一个 SELECT，而是事务程序、交错、提交结果和故障历史。

因此，“这一批查询的结果相同”不能覆盖丢失更新、错误可见性或恢复后丢数据；“并发 benchmark 跑完”也不能代替 isolation checking。

## 四、怎样构造真正有区分力的测试数据

### 4.1 从优化前提反推反例

比“生成随机数据”更有针对性的问题是：**这条规则靠什么前提成立，哪个最小输入可以区分前提成立与不成立？**

| 优化或功能 | 容易被隐含的前提 | 应主动构造的输入 |
|---|---|---|
| Join elimination | 被消除表的唯一性、匹配存在性 | 无匹配、重复匹配、nullable key；若已有约束禁止，改用不声明该约束的 schema |
| Predicate pushdown | 谓词对 NULL、异常和求值位置的行为 | NULL 补齐、`COALESCE`、转换失败、过滤顺序变化 |
| Subquery decorrelation | 标量子查询最多一行、绑定范围正确 | 零行、一行、多行、重复外层键、内层 NULL |
| Aggregation split | 中间状态可合并、类型和空输入一致 | 空分区、全 NULL、溢出边缘、不同 merge 顺序 |
| Partition pruning | 元数据、比较语义和边界一致 | 恰落边界的值、NULL 分区、时区转换、过期统计 |
| Top-K / late materialization | 排序键与行标识对应、tie 语义合法 | 相同排序键、稀疏选择、删除后重排、跨 batch tie |
| Incremental view maintenance | delta 与快照、重复度及删除处理一致 | 最后一个匹配被删除、重复插入、迟到更新、刷新重试 |

这里的重点不是绕过 schema。一个声明 `PRIMARY KEY` 的表拒绝重复键，可能正是正确行为。生成器要区分“合法状态下的规则测试”和“非法输入下的拒绝测试”，不能靠破坏前提后仍要求系统优化正确来制造假 bug。

### 4.2 一个 Outer Join 反例：过滤之后新产生的 NULL

构造两张只有一行的表：

```sql
CREATE TABLE a(k INTEGER);
CREATE TABLE b(k INTEGER, v INTEGER);
INSERT INTO a VALUES (1);
INSERT INTO b VALUES (1, 7);

SELECT a.k, b.v
FROM a LEFT JOIN b ON a.k = b.k
WHERE COALESCE(b.v, 0) = 0;
```

正确结果为空：匹配行的 `v=7`，不满足谓词。

假设错误地把谓词复制进右侧输入，同时保留上层过滤：

```sql
SELECT a.k, b2.v
FROM a
LEFT JOIN (
  SELECT * FROM b WHERE COALESCE(v, 0) = 0
) AS b2 ON a.k = b2.k
WHERE COALESCE(b2.v, 0) = 0;
```

第二条返回 `(1, NULL)`。提前过滤删除了原匹配行，LEFT JOIN 生成的 NULL 又被 `COALESCE` 变为 `0`，通过上层过滤。

**这两条 SQL 故意不等价。**它们用于证明这种 rewrite 不合法，不表示某个实际引擎当前会做这个错误改写。一个有意义的优化器测试还需要确认目标引擎走到了相关规则，并检查其保留了必要保护。

由此扩展 case family：右侧空表、真实 NULL、重复匹配、左右键均为 NULL、普通等值与 null-safe equality、过滤器提前/延迟完成。每个变体都对应一条不同的语义前提。

### 4.3 `NOT IN` 与 `NOT EXISTS`：一个 NULL 足以推翻直觉

```sql
CREATE TABLE outer_t(k INTEGER);
CREATE TABLE inner_t(k INTEGER);
INSERT INTO outer_t VALUES (1);
INSERT INTO inner_t VALUES (NULL);

SELECT k FROM outer_t WHERE k NOT IN (SELECT k FROM inner_t);

SELECT k FROM outer_t
WHERE NOT EXISTS (
  SELECT 1 FROM inner_t WHERE inner_t.k = outer_t.k
);
```

第一条结果为空，第二条返回 `1`。这里不是引擎差异，而是三值逻辑下两条 SQL 本来就不同。对 Anti Join 的测试，应该把 outer key 是否 NULL、inner 是否空、inner 是否含 NULL、是否匹配分别列出来。

LLM 很容易把这两条 SQL 当作通用等价改写。因此，让模型生成“等价查询”之后，仍要检查其前提；模型说等价并不能授权 oracle 把差异报成 bug。

### 4.4 改变联合分布，而不只改变数据量

考虑两列各自都有 100 个值，每个值出现次数相同：

```text
数据 A：x 与 y 近似独立
数据 B：y = x
数据 C：大部分行 y = x，少量行打破相关性

查询：WHERE x = 7 AND y = 7
```

即使单列 NDV、min/max 和边际分布相同，A 与 B 的联合选择率也可能相差两个数量级。把数据量从一千万扩大到一亿，未必比构造这两种联合分布更容易发现 CE 问题。

建议将生成参数显式化：row count、NDV、null ratio、top-key frequency、列间相关性、Join overlap、fanout、字符串长度分布、排序程度、文件数和 row-group 大小。还要区分“生成器期望的分布”与“生成后实际测出的分布”，避免约束修复和采样改变原本设计。

### 4.5 测试阈值两侧，并证明路径确实切换

对已知阈值 `B`，重点测 `B-1 / B / B+1`。但这里的 B 要来自目标版本的实现或观测：

- batch size 边界：空 batch、恰好满 batch、最后一个 batch 只有一行；
- hash table 扩容边界：rehash 前后键值及重复链仍一致；
- 内存预算边界：首次 spill、递归 spill、多个分区同时回读；
- 字符串 inline/out-of-line 边界：字节长度而非仅字符数量；
- dictionary 与普通编码切换：NDV、空值和索引宽度变化；
- optimizer 预算边界：枚举上限、超时退出、fallback plan；
- 文件/page 边界：变长值跨页、空 row group、统计缺失。

对于内存相关阈值，行数只是近似代理，值宽、allocator、并发都会改变触发点。应先通过 profile 找到转换区间，再围绕区间扫描，记录 `spill_bytes`、实际 batch/编码、rule hit 或 plan signature。

**如果测试宣称覆盖 spill，却没有发生 spill，就不能将它记为该分支通过。**这条要求比增加更多随机种子更能提高结果可信度。

## 五、按引擎边界组织测试，而不只按 SQL 功能组织

### 5.1 能力模型应该是条件集合

一个简单的 `supports_array=true` 太粗糙。更有用的能力描述至少应区分：

```text
语法可解析 → 类型可绑定 → 能生成计划 → 能执行 → 可持久化/恢复
                                                ↓
                         满足结果、错误、资源与生命周期契约
```

“支持”还应附带条件：类型参数、嵌套深度、算子、编码、执行模式和版本。把观测状态记为 `supported / expected_rejection / unknown / inconsistent`，不要把一次失败自动学习成“不支持”，否则 crash 或回归可能被生成器悄悄绕开。

一个功能的边界至少应有三组测试：边界内成功、边界处成功或按契约拒绝、边界外明确拒绝且状态完整。错误消息字面值可能演化，通常应匹配错误类别及必要语义；如果协议承诺了稳定错误码，再验证错误码。

### 5.2 一张覆盖复杂类型与系统状态的矩阵

| 边界 | 必须区分的输入/状态 | 主要 oracle | 必须观察到的证据 |
|---|---|---|---|
| NULL 与空值 | SQL NULL、空字符串、空集合、集合内 NULL | 明确 expected + TLP | 各种值仍可区分 |
| 数值与转换 | 最小/最大值、decimal scale、溢出、NaN、±0 | 方言契约、参考实现、合法变换 | 输出类型、异常及舍入方式 |
| 时间与排序规则 | DST 重叠/跳跃、时区转换、Unicode normalization、大小写 | 固定时区/collation 的 expected | session 配置与实际比较路径 |
| 嵌套类型 | NULL list、`[]`、`[NULL]`、NULL struct、字段为 NULL 的 struct | typed result、round trip、跨表示对照 | 类型与 null bitmap 没有塌缩 |
| JSON / Variant | missing、JSON null、SQL NULL、重复 key、混合数值类型 | 明确解析/路径访问契约 | 存在性、类型和值分别检查 |
| 列式执行 | flat、constant、dictionary、lazy，非连续 selection | 不同表示上的相同逻辑输入 | 实际编码/选择向量及结果 |
| 聚合与窗口 | 空分区、NULL peer、ties、frame 边界、多级合并 | 单阶段/多阶段、参考计算 | frame 定义、merge 次序、类型 |
| 内存与调度 | spill、并行取消、重试、提前结束的 LIMIT | 路径差分 + 资源不变量 | spill/retry/cancel 确实发生，资源被回收 |
| 存储与外表 | 截断文件、异常 metadata、schema evolution、分区缺失 | 文件语料、独立 reader、预期拒绝 | 错误后的事务和文件状态 |
| MV / 索引 | update/delete、刷新延迟、重建、读写交错 | 全量重算/基表扫描 | snapshot 与可见性水位一致 |
| 分布式与恢复 | worker 失败、消息重复/乱序、提交结果不明 | history checker、恢复不变量 | 故障时点、已确认提交和重试身份 |
| 权限与隔离 | 租户过滤、行列权限、索引/缓存复用 | 明确的允许集合 | 无越权行、无跨身份结果复用 |

表中的每一格都需要适配目标引擎。例如 map duplicate key 可以被拒绝，也可以按明确定义处理；浮点 NaN 的比较与排序也存在方言差异。测试的目标是核对承诺，而非强迫所有产品采用同一行为。

全矩阵笛卡尔积通常不可承受。可先做 pairwise 覆盖，再为高风险交互保留定向三元组，例如 `NULL × dictionary × selection vector`、`decimal × partial aggregation × spill`、`Outer Join × null-safe predicate × runtime filter`。pairwise 是预算策略，不是充分性证明。

### 5.3 嵌套类型：先保持逻辑值，再改变物理表示

对一个 list/struct case，最小值域至少应包含：

```text
NULL
[]
[NULL]
[1, NULL, 1]
NULL struct
{a: NULL}
{a: []}
{a: [NULL]}
```

接下来不是继续增加随机值，而是让同一批逻辑值走不同路径：内存构造与文件读入、直接投影与 UNNEST、flat 与 dictionary、单线程与并行、内存聚合与 spill 后合并。

[Velox Expression Fuzzer](https://facebookincubator.github.io/velox/develop/testing/expression-fuzzer.html) 对表达式不同求值路径做验证；[Aggregation / Window Fuzzer](https://facebookincubator.github.io/velox/develop/testing/fuzzer.html) 则将随机输入与执行计划变体结合起来。它们提供的工程启发是：**相同逻辑值的不同物理表示，本身就能形成高价值的测试轴。**

这里尤其要注意测试比较器。把所有值转成字符串，可能把 NULL、字符串 `"null"`、JSON null 混为一谈；把结果转成 set 会抹掉重复值；对 list 排序会破坏元素次序。比较器应该递归保留类型、nullness 和必要的顺序。

### 5.4 非结构化数据：把输入、索引、检索质量分开

以文本或图像入库为例，完整路径可能是：

```text
原始字节 → 解码/解析 → 分块/元数据 → embedding → 索引 → 检索 → SQL 组合
```

每一段都有不同的测试问题。损坏编码、截断图片、超长文本是 ingestion 的输入边界；doc-id 与 chunk-id 错配是映射错误；删除之后仍可被索引查到是可见性错误；返回内容相关性差则属于检索质量问题。

测试确定性路径时，固定解析器、分块参数、embedding 模型和预生成向量，让同一文档稳定映射到同一测试输入。模型服务另做质量、漂移、超时和部分失败评估。否则 embedding 变化可能被误报成存储或检索引擎回退。

BEIR 的 relevance judgments 可以帮助评价文本检索质量，但不是每个未标注文档都已被证明不相关。对数据库层，还应加入一组人工可判定的小 corpus：同内容不同 ID、更新前后文档、空文本、多语言、被删除文档、无权限文档。

### 5.5 向量 + Filter + Top-K：最容易被一个 recall 数字掩盖的边界

若接口承诺返回满足过滤条件的最近邻，ground truth 应该在过滤后的集合中计算：

```text
Candidates = {v ∈ D | predicate(v)}
Truth      = ExactTopK(Candidates, query_vector, metric)
```

`Filter(TopK(D))` 与 `TopK(Filter(D))` 一般不同。最小反例只需两个点：最近的点不满足过滤，次近的点满足过滤；`k=1` 时，先取 top-1 再过滤会得到空集，而过滤后精确搜索应该返回第二个点。

对 ANN，不能要求每个输入都与精确 top-k 完全相同。需要分别判定：

1. **硬正确性**：维度和距离契约正确，结果符合过滤与权限条件，不返回已不可见的数据。
2. **质量契约**：针对约定 workload 与参数，测量 recall、结果不足率、延迟和索引成本。
3. **边界行为**：过滤后集合少于 k、零个候选、重复向量、等距离 tie、更新与重建期间的可见性。

ground truth 必须与被测查询使用相同快照、距离定义及归一化方式。若候选数不足 k，recall 分母按实际 truth 大小定义；若没有候选，则单独检查应为空，避免除零。ties 需要固定 tie-breaker，或采用允许等距离替代的判定方式。

### 5.6 增量计算：数据集应包含时间轴

一次 insert 后查询正确，并不能覆盖 IVM。更有用的是逐步执行：

```text
S0：左表一行，右表空
S1：右表插入一个匹配
S2：右表再插入一个重复匹配
S3：删除其中一个匹配
S4：删除最后一个匹配
```

对 LEFT JOIN，输出经历 NULL 补齐、一条匹配、两条匹配、一条匹配、重新 NULL 补齐。每一步都应在**相同已提交快照/刷新水位**下，比较增量结果与基表完整重算的 bag。

重放重复消息时是否去重，取决于入口协议；不能无条件要求所有更新幂等。测试必须携带 operation identity、commit order、watermark/checkpoint 以及产品承诺的 delivery semantics。

## 六、性能测试：寻找悬崖，而不是只比较平均分

### 6.1 正确性回归与性能回归要共享 case，分开判定

同一个测试可以先核对结果，再记录 latency、CPU、峰值内存、扫描字节、网络、spill 和编译时间。一个版本快了 20%，但少输出一半行，不是优化；一个版本修复了错误结果而变慢，也不应只被汇总成性能回退。

对性能问题，至少需要三类比较：

| 比较 | 回答的问题 | 需要控制的条件 |
|---|---|---|
| 同 case，旧版本 vs 新版本 | 是否发生版本回退 | 硬件、数据、配置、并发、缓存、结果语义 |
| 同逻辑，同版本，不同计划/开关 | 是否存在明显更好的可行路径 | 确认计划不同且结果一致，记录强制计划方式 |
| 参数连续变化 | 是否存在不合理悬崖或规模增长趋势 | 每一步的数据统计、路径切换和资源限制 |

不要规定“更少行就必须更快”或“加一个过滤条件就必须更快”。计划、压缩率、缓存命中和并行度变化都可能使耗时不单调。类似关系可以用于提名异常，再通过 profile 判断。

### 6.2 一个可执行的性能判定协议

以下是建议的实验协议，不是某篇论文的固定阈值：

- 先做独立预热，再随机交错或采用 ABBA 顺序运行基线与候选，减少温度和系统漂移影响。
- 冷缓存与暖缓存分组；仅重启数据库未必清掉 OS page cache，也不等于冷数据测试。
- 起步可做至少 10 组配对测量，再根据方差增加次数；不能用这么少的样本声称得到稳定 p99。
- 同时使用相对和绝对门槛，例如中位耗时比超过 `1.2` 且增加超过 `50 ms`，作为初始候选，而非普适标准。
- 保存原始样本，检查配对差异或置信区间，补采稳定后再归类；记录查询是否超时、OOM 或出错。
- timeout 是删失观测：若基线 1 秒完成、候选超过 60 秒上限，只能说回退至少超过该界，不能把 60 秒当真实耗时。

总体报告应同时保留覆盖率、正确结果比例、失败类别、逐查询变化和最坏回退。只在双方都完成的查询上算几何平均会产生幸存者偏差；可以报告这个统计，但必须同时报告被排除的查询。

### 6.3 缩减性能 case 时不要把触发条件一起缩没

正确性 reducer 通常尽量删行、删列、删表达式。但性能 case 可能恰好依赖某个规模、倾斜度或编译复杂度：缩到十行之后，spill 消失，问题也消失。

性能 reducer 的 interestingness predicate 应同时要求：结果仍一致、退化可重复、目标计划/资源路径仍出现。可以先保持数据分布和阈值，只缩 query；也可以固定 query，二分查找最小触发规模。

最终保留的未必是“字节数最小的 SQL”，而是**能够稳定表达该性能失效机制的最小实验**。

## 七、LLM 时代：从生成 SQL 走向覆盖反馈

### 7.1 三项近期工作应放在不同位置理解

**SQLStorm：用 LLM 扩充分析 workload。**[PVLDB 2025 论文](https://db.in.tum.de/~schmidt/papers/sqlstorm.pdf) 发布了含 18,251 条查询的初始集合，并在 1 GB、12 GB、220 GB 数据规模上研究覆盖与执行表现。论文不仅统计 SQL 数量，也观察计划和执行 trace 的多样性；[代码与查询](https://github.com/SQL-Storm/SQLStorm) 公开。它支持“LLM 可以产生传统少量模板之外的负载”这一判断，但没有证明这些查询代表所有生产负载。

**SQLancer++：让生成器适应不同引擎。**[Scaling Automated Database System Testing](https://arxiv.org/abs/2503.21424v2) 的重点是通过与 DBMS 交互，学习哪些 SQL 特征能够被接受，降低逐方言手写生成器的成本。它不是“用 LLM 判断结果”的同义词；自适应生成与逻辑 oracle 是不同职责。

**ShQveL：让 LLM 补充生成器缺失的特征。**[2026-08 更新的 v2](https://arxiv.org/abs/2505.02012v2) 标题为 *Automated Database Testing via LLM-Synthesized SQL Features*；2025 年 v1 标题是 *Testing Database Systems with Large Language Model Synthesized Fragments*。其核心是用 SQL sketch 留出待填片段，把 LLM 产生的功能纳入现有生成过程。v2 摘要报告在五个 DBMS 上发现 55 个此前未知 bug、其中 50 个已修复；这是作者实验结果，不是本文复现。

另外，[FuzzySQL，2026 年预印本](https://arxiv.org/abs/2602.19490) 将注意力放在 GTID、存储过程、KILL 等通常较少被探索的专有功能，结合生成、变异、错误修复和 replay-guided crash validation。它拓展的是功能和状态序列覆盖，不能据此推断已解决任意 SQL 的结果正确性问题。

SQLancer++、ShQveL 和 FuzzySQL 在这里按论文路线讨论；本文未安装这些研究原型，也未验证其 artifact 在当前环境可重跑，不把论文公开等同于完整工具链已经验收。

### 7.2 LLM 应进入哪些环节

```text
文档 / 类型签名 / 优化规则 / 历史 bug
                  │
                  ▼
        LLM 提取契约和候选失效假设
                  │
                  ▼
  功能描述 + 前提 + 数据草图 + SQL sketch
                  │
                  ▼
  确定性生成器：绑定 schema、补值、检查约束
                  │
                  ▼
  沙箱执行：结果 / 错误 / 计划 / profile / history
                  │
          ┌───────┴────────┐
          ▼                ▼
  已验证 oracle 判定     覆盖反馈选择下一轮
          │                │
          ▼                └──→ LLM 补充新假设/片段
  replay → reduce → triage → 固定回归 case
```

这里最值得交给 LLM 的不是重复生成十万条 SELECT，而是以下几类成本高、知识分散的工作。

| 环节 | 给模型的上下文 | 期望产物 | 接受产物的依据 |
|---|---|---|---|
| 契约提取 | 固定版本文档、函数签名、错误定义 | 参数范围、NULL/溢出/状态前提及来源位置 | 人工或规则核对契约，不直接采信描述 |
| Patch 定向测试 | diff、相邻代码、相关回归 | 哪个旧假设变了，哪些输入可区分前后 | 触达变更路径，旧缺陷/注入 mutant 被检出 |
| 方言适配 | schema、类型和功能探测结果 | 可绑定的 SQL sketch | parser/binder 和目标功能检查 |
| 边界扩展 | 已覆盖组合与未覆盖条件 | 高风险的新交互，而非措辞变化 | 新语义标签、计划或运行分支 |
| 失败分析 | 最小重放、日志、plan diff | 可能的公共机制和进一步实验 | replay 验证，不能仅凭描述合并 bug |
| 报告整理 | 原始证据与缩减产物 | 可审阅的复现步骤、预期/实际差异 | 附件完整、结论不超出证据 |

例如修改了 HashAgg 的中间状态序列化，prompt 应提供状态类型、merge 约束和 spill 触发方式，要求覆盖全 NULL、空分区、大 decimal、不同 merge tree。只有“帮我生成复杂 SQL”通常无法把预算集中到这次改动。

### 7.3 生成得更合法，也可能测得更少

LLM 的自我修复有一个危险倾向：遇到失败就把 SQL 改简单。一个 JSON + window + outer join 的测试在修复后变成普通 SELECT，成功率上升了，目标覆盖却消失了。

因此，每个 case 要保留不可随意删除的目标条件，例如：

```text
必须包含 nested field projection
必须使用 nullable key 的 LEFT JOIN
必须命中指定规则或替代计划
必须发生至少一次 spill
```

修复只允许改变非目标部分；如果无法满足，记录为未覆盖或待诊断。不能把结果 mismatch 交给模型改 expected，让测试“恢复通过”。对于模型新提出的 metamorphic relation，也要先用受限语义模型、小域枚举或审阅确认其前提，再成为正式 oracle。

### 7.4 LLM 赋能是否有效，需要消融实验

至少比较四组：手写生成器、LLM 直接生成、LLM sketch + 既有 oracle、加入覆盖反馈的混合方案。给它们相同 CPU/墙钟预算，另外报告 token 和推理费用，使用多个独立种子。

最值得观察的指标是：

- 有效执行率，同时分开 syntax error、binding error、预期运行时拒绝；
- 目标路径触达率，而非只有 SQL 条数；
- 新增语义组合、计划结构与运行分支；
- 首次发现时间、每小时/每单位成本的独立缺陷数；
- 可重现率、缩减成功率、误报率及修复后回归保留率。

不要把同一个根因的几千个 SQL 变体算成几千个 bug。历史 bug 可以作为可控评估集，但应按时间或根因家族隔离训练/提示材料与评测集，减少模型记住公开 issue 后产生的虚高收益。保存模型版本、prompt、生成产物；即使设置随机种子，也不能假定远端模型调用永久逐字可复现。

最终进入日常回归的应该是固定 artifact。日常验证不需要每次重新询问模型，探索阶段才持续调用 LLM。

## 八、一个可落地的测试集架构

### 8.1 每个 case 都有 manifest

下面是建议的 case 描述格式，值为设计示例，不代表已有 runner 的真实输出：

```yaml
id: outer-join-null-extension-001
intent: predicate-pushdown-must-preserve-null-extension
engine:
  version: "<commit-or-release>"
inputs:
  setup: setup.sql
  query: query.sql
  data_sha256: "<hash>"
semantics:
  result: typed-bag
  deterministic: true
  preconditions:
    - fixed-snapshot
    - no-volatile-functions
oracle:
  kind: explicit-expected
  expected: expected.json
coverage:
  required:
    - left-outer-join
    - nullable-right-output
    - null-accepting-predicate
execution:
  seed: 17
  timeout_seconds: 30
artifacts:
  - logical-plan.json
  - physical-plan.json
  - profile.json
  - engine.log
```

性能 case 追加硬件、缓存状态、重复次数、baseline、阈值及原始样本；事务 case 追加 session、barrier、operation ID、history 和故障时间轴。数据生成器版本与参数也必须保留：只有随机种子，不足以跨生成器版本复现。

结果分类应至少有 `pass / result_mismatch / unexpected_error / crash / timeout / resource_exhausted / expected_rejection / not_covered / inconclusive`。这比一个 exit code 更能防止“没有跑到目标分支却算通过”。

### 8.2 比较器也是数据库组件，需要语义设计

默认无序 SQL 结果按 **typed multiset** 比较，保留重复次数。明确要求顺序时比较序列；如果 ORDER BY 不能完全打破 ties，就比较合法的等价组，或给测试补充稳定排序键。

整数、decimal 与字符串通常精确比较；浮点先区分 exact contract 和允许重排的数值误差，再约定绝对/相对误差以及 NaN、infinity 的策略。不能给所有数值套一个宽松 epsilon，否则很容易掩盖真实错误。

对大结果可以先做类型化 hash 预筛，但 hash 不能成为唯一证据；发现差异后应能够定位到具体行和值。跨引擎的错误、隐式 cast、collation 与时间语义需要先对齐，不一致时归为“待契约判定”，不能让多数投票自动裁决。

### 8.3 四个测试池，四种预算

| 测试池 | 内容 | 运行位置 | 验收重点 |
|---|---|---|---|
| 语义核心池 | 小数据、明确 expected、已修复 bug | 每次提交 | 快、确定、可定位 |
| 功能组合池 | TLP/NoREC、plan/encoding 差分、边界矩阵 | PR 或 nightly | 触达目标组合，失败可重放 |
| 性能鲁棒性池 | 固定 benchmark 子集、相关性与阈值扫描 | 隔离性能环境 | 原始样本、资源画像、回退界限 |
| 探索池 | 覆盖引导、LLM sketch、历史状态和故障序列 | 受预算约束的长时间任务 | 独立新缺陷、有效覆盖增长 |

选择 benchmark 子集时，不宜只保留最慢的查询。可按“查询覆盖了哪些算子、表达式、计划边、数据分布和运行状态”做预算化集合覆盖，同时为历史故障和风险高的 case 保留硬约束。

### 8.4 验证测试集自身是否能抓错

如果所有测试在一个新引擎上第一次运行就全绿，未必是好消息。需要用已知历史缺陷或局部 mutation testing 检查 oracle：例如故意遗漏 TLP 的 UNKNOWN 分区、把 bag 比较改成 set、去掉 null-safe 保护。

被注入的错误应该被对应测试检出。未检出时，检查是数据没有激活错误、目标路径未执行，还是比较器抹掉了差异。mutation score 仍不是完整正确性的证明，但比单看通过率更能暴露测试设计漏洞。

## 九、先做哪几步：从一个语义切面开始

如果面向一个已有的分析引擎，我会先选择 **Outer Join + NULL + runtime filter** 或 **nested type + aggregation + spill** 这样的切面，而不是一开始搭建“覆盖所有 SQL”的平台。

第一步，固定引擎版本与语义契约，写出十几个可手算的最小 case，并建立 typed-bag 比较。验收标准是每个 case 都有明确预期，且能检出至少一种对应的错误变换。

第二步，接入一个已有 benchmark 子集和一种自动 oracle。为目标规则增加开关或计划证据，把数据量、重复度、NULL、相关性和内存阈值参数化。验收标准是失败可完整重放，并能区分 mismatch、预期拒绝和未覆盖。

第三步，再引入 LLM。只让模型补充当前未覆盖的函数、类型或组合，比较引入前后的目标覆盖与独立缺陷产出。若只有 SQL 数量上升，没有新增计划、运行路径或有效反例，就没有证据表明测试能力提高了。

最后才扩展到事务历史、文件格式、向量过滤和故障恢复。每扩展一条边界，都要补齐相应的 oracle，而不是复用一个“查询成功且结果非空”的通用断言。

这条路线的最终产物应是一个能够持续演化的测试资产库：公开 benchmark 提供负载骨架，定向生成补齐边界，LLM 帮助提出新假设，执行器和 oracle 产生证据，reducer 把证据压缩成可以长期保存的反例。

## 附：几个语义例子的可运行验证

本文配套提供一个仅依赖 Python 标准库的 [示例脚本](/examples/database-testing-boundaries.py)：

```bash
python3 database-testing-boundaries.py
```

脚本使用内存 SQLite 验证 TLP/NoREC 示例、Outer Join 非法改写、`NOT IN` 的 NULL 反例，以及逐步更新后 Outer Join 的预期结果；另以两个数值点验证 Filter 与 Top-K 的顺序差异。2026-09-18 本地运行时，SQLite 版本为 3.51.0，五组验证均通过，包含遗漏 UNKNOWN 分区和错误去重的负向检查。它检查的是文中推导，不是对这些数据库测试框架的复现，也没有测量引擎性能或证明真实 MV 实现正确。

## 参考资料与阅读顺序

以上表格已给出可复用项目入口。若希望先把机制读透，可以按下面的顺序继续：

1. [NoREC](https://arxiv.org/abs/2007.08292) 与 [TLP](https://www.research-collection.ethz.ch/bitstream/20.500.11850/456967/2/3428279.pdf)：先理解没有完整标准答案时，怎样构造可验证的关系。
2. [SQLancer](https://github.com/sqlancer/sqlancer) 与 [Velox Fuzzer](https://facebookincubator.github.io/velox/develop/testing/fuzzer.html)：分别观察 SQL 层生成/判定和执行层计划/表示变体的职责。
3. [SemConT](https://arxiv.org/abs/2406.09469)、[EET](https://www.usenix.org/conference/osdi24/presentation/jiang)、[Elle](https://github.com/jepsen-io/elle)：理解语义模型、表达式关系和事务历史三种不同 oracle。
4. [APOLLO](https://github.com/sslab-gatech/apollo) 与 [SQLStorm](https://db.in.tum.de/~schmidt/papers/sqlstorm.pdf)：把正确性探索连接到性能回退与 workload 多样性。
5. [SQLancer++ v2](https://arxiv.org/abs/2503.21424v2)、[ShQveL v2](https://arxiv.org/abs/2505.02012v2) 与 [FuzzySQL](https://arxiv.org/abs/2602.19490)：比较适应方言、LLM 片段扩展和专有功能探索的不同边界。
