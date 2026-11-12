---
title: "【论文】数据库测试与 Benchmark：从开源数据集、语义反例到 LLM 驱动的引擎边界探索"
date: 2026-09-18T00:00:00+08:00
lastmod: 2026-10-07T00:00:00+08:00
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
toc: true
math: true
---

测试一个普通 Web 接口时，工程师面对的是已知答案的命题：喂进去三条订单参数，断言返回的金额等于 `199.00`、状态码等于 `200`，对错一目了然。

可一旦把被测对象换成数据库内核，这套朴素直觉立刻撞上一堵高墙：**当一条包含八表外连接、相关子查询、`COALESCE` 空值转换与滑动窗口的复杂 SQL 在千万行数据上跑出 `41,829` 行结果时，谁知道标准答案究竟该是 `41,829` 行还是 `41,830` 行？** 除非你手里已经有一台绝对无 Bug、且在方言隐式转换、排序规则（Collation）、浮点舍入与空值语义上分毫不差的“全知参考引擎”，否则面对一条随机生成的复杂查询，测试者连预期结果都写不出来——这就是数据库测试最核心的**判定器难题（Oracle Problem）**。

更棘手的是，即便跑通了全套标准基准测试（Benchmark），依然挡不住静默错数（Silent Wrong Result）。就像我们在 [Dynamic Filter 穿越 Outer Join]({{< relref "2026-08-24-dynamic-filter-outer-join.md" >}}) 里拆解过的那样：一个运行时过滤器在 99.9% 的均匀合成数据上都能合法加速，唯独遇到“右表原本有匹配行、被提前过滤掉后触发左外连接补 `NULL`、补出的 `NULL` 又恰好穿过上层 `COALESCE(b.v, 0) = 0`”的组合时，就会凭空多吐出一行脏数据。触发这个致命缺陷不需要 100 GB 的标准数仓，两张单行小表就够了；反过来，跑上百遍均匀分布的大规模扫描，却可能一次都踩不中这个死角。

把**“组合爆炸的输入与执行状态空间”**和**“缺乏现成标准答案的判定器难题”**放在一起，就能看清数据库测试过去十年的演进主线：公开 Benchmark、差分测试与形式化模型、SQLancer 代表的变形测试（Metamorphic Testing）、面向算子物理路径的 Fuzzer，以及近期引入大语言模型（LLM）的语法与特征探索，其实是在从不同侧面围剿同一对矛盾。下面我们就顺着“资产拆解 → 判定器构造 → 语义与物理反例 → 现代引擎多模态边界 → LLM 协同闭环 → 工程沉淀”这条脉络，把整套测试体系拆开看清。

---

## 一、拆解“数据集”幻觉：为什么跑通 Benchmark 抓不住两行数据的 Bug？

日常讨论里，大家常把 CSV/Parquet 数据文件、TPC-H 负载脚本和单元测试用例统称为“测试数据集”。但如果一个失败现场想要跨机器稳定复现，光存下 SQL 文本和数据文件远远不够。从第一性原理看，一次完整的数据库测试其实是一个七元组：

```text
Test = (Schema, Data, Query, State, Configuration, Environment, Oracle)

Schema        列类型、非空/唯一约束、索引、分区键、物化视图定义
Data          值域边界、重复度、列间相关性、物理排序、文件切分与编码
Query         单条 SQL、API 调用或并发交错的事务序列
State         统计信息快照、Buffer/OS 缓存冷暖、MVCC 版本历史、刷新水位
Configuration 优化器规则开关、并行度（DOP）、内存上限与溢写（Spill）阈值
Environment   引擎提交版本、CPU 架构、时区、排序规则与注入的故障序列
Oracle        显式预期结果、代数等价关系、状态不变量或性能回退界限
```

同一份 Parquet 文件，拿去测列式扫描吞吐时看的是解压带宽与 SIMD 过滤速度，拿去测格式兼容性时盯的却是嵌套空值位图（Null Bitmap）、高精度 Decimal 元数据或损坏页能否按契约报错。输入文件一模一样，只要 `State`、`Configuration` 和 `Oracle` 换了，验证的就完全是两码事。

沿着每种公开资产最擅长暴露的失效机制去挑选起点，才不会拿着锤子找钉子：

| 公开资产与入口 | 核心内容与定位 | 最值得复用的结构特征 | 单独使用时的结构性盲区 |
| --- | --- | --- | --- |
| [TPC-H / TPC-DS](https://tpc.org/TPC_Documents_Current_Versions/current_specifications5.asp) | 官方决策支持规范、数据与查询生成器 | 规模可伸缩、多表业务模式与复杂分析模板 | 合成数据偏均匀独立，缺极端倾斜与异常提交历史；改动 Schema 或参数后属派生负载，不可混称官方 TPC 成绩 |
| [Join Order Benchmark（JOB）](https://github.com/gregrahn/join-order-benchmark) | IMDb 真实电影关系快照与 113 条连接查询 | 真实世界的列间强相关性与倾斜分布，专治基数估计偏差 | 聚焦内连接分析，不含更新恢复与复杂嵌套类型；必须锁定同一份 IMDb 快照才具横向可比性 |
| [ClickBench](https://github.com/ClickHouse/ClickBench) | 真实流量宽表数据、各引擎 DDL 与 43 条查询 | 宽表扫描、高基数字符串处理、过滤与聚合压测 | 无多表关联与事务语义；对比时极易受冷暖缓存与数据加载口径干扰 |
| [SQLStorm](https://github.com/SQL-Storm/SQLStorm)（[PVLDB 2025](https://db.in.tum.de/~schmidt/papers/sqlstorm.pdf)） | LLM 生成的 18,251 条复杂查询（覆盖 1/12/220 GB 规模） | 跳出几十条手工模板，显著拓宽 SQL 语法与物理计划形态 | 跨引擎方言与非确定性行为未完全对齐，跨系统结果差异需先排查方言契约而非直接记为 Bug |
| [YCSB](https://github.com/brianfrankcooper/YCSB) / [BenchBase](https://github.com/cmu-db/benchbase) | 键值读写比例压测框架与多数据库 JDBC 事务套件 | 可控键分布（如 Zipfian）、并发争用与标准事务混合负载 | 吞吐数字高不代表隔离级别实现正确，缺少对并发历史异常与崩溃恢复的语义裁决 |
| [DuckDB sqllogictest](https://duckdb.org/docs/current/dev/sqllogictest/overview) | 声明式 SQL 逻辑测试格式与引擎回归语料 | 精确绑定初始化 SQL、查询结果与预期错误类型 | 依赖人工沉淀已知边界，无法自动探索未知的功能交互死角 |
| [Apache parquet-testing](https://github.com/apache/parquet-testing) | 包含 `data`、`bad_data` 与 Variant 等边界文件样例 | 跨引擎列存格式互操作、特殊编码与截断/损坏页防御 | 只检验存储读写契约，不触及上层查询优化器改写 |
| [Velox Fuzzers](https://facebookincubator.github.io/velox/develop/testing/fuzzer.html) | 表达式、聚合与窗口算子的随机输入与计划变体工具 | 在同一批逻辑数据上强行切换编码布局、内存与溢写路径 | 聚焦向量化执行层，不涵盖完整数据库的事务、权限与崩溃恢复 |
| [LDBC SNB](https://github.com/ldbc/ldbc_snb_interactive_v2_impls) / [ANN-Benchmarks](https://github.com/erikbern/ann-benchmarks) / [BEIR](https://github.com/beir-cellar/beir) | 社交图交互负载、向量近邻数据集与多领域文本检索评测 | 图跳数相关性、向量精确 Ground Truth、文档相关性标注 | 评测近似召回率或图遍历性能，不能替代带标量过滤、并发删除与快照可见性的数据库语义验证 |

复用这些公开资产时，还有两道工程红线容易踩错。第一是**开源许可与公开程度的区分**：一个测试仓库代码采用 MIT 协议，不等于它脚本里拉取的 TPC 工具、IMDb 快照或 Stack Exchange 语料也能自由再分发；同样，阅读 [SQLite 的测试说明](https://www.sqlite.org/testing.html) 时要注意区分其公开测试套件与闭源专有的 TH3 高可靠测试集，后者并不是一份能直接下载复用的开源语料。第二是**基准可比性与破坏性探索的隔离**：为了抓 Bug，我们经常需要把表的列改成可空（Nullable）、注入极端热点键或强行压低内存预算，如果直接原地篡改基准目录，就会毁掉与历史版本对比性能趋势的尺子。最稳妥的办法是把资产拆成三层漏斗管理：

```text
原始基线层：冻结数据快照、生成工具版本与标准查询，专供跨版本趋势对照
      │  定向注入空值、列相关性、内存压力与并发历史
      ▼
开放探索层：大规模变异与模糊测试，允许海量试错，只求撞出异常现场
      │  自动缩减（Reduce）并定位失效前提
      ▼
核心回归层：最小数据集 + 必要配置开关 + 可执行判定器，随每次提交快速运行
```

不要把探索层生成的几百万条随机 SQL 原封不动塞进日常 CI——真正值得永久留存进回归集的，是从海量试错里提炼出来的**独立失效机制**。

---

## 二、破解判定器难题：没有标准答案，靠什么抓出逻辑错数？

把语法生成器和类型绑定器写对，只能保证数据库“不把 SQL 拒之门外”。真正在生产环境惹出大祸的，往往是不报错、不崩溃、平静返回错误行数的逻辑缺陷。在缺乏人工标准答案的前提下，现代数据库测试沿着跨引擎差分、同引擎代数变形（`TLP`、`NoREC`、`PQS`）与黑盒并发依赖环（`Elle`）三条验证主线构造判定器，从不同维度围捕静默错数与隔离级别异常：

![数据库测试判定器三条核心验证主线](/images/diagrams/dbres-testing-oracles.svg)

### 2.1 从崩溃检测到变形测试（Metamorphic Testing）

最早普及的 [SQLsmith](https://github.com/anse1/sqlsmith) 依据数据库元数据随机拼装类型合法的抽象语法树（AST），靠进程崩溃、断言失败或内部错误来抓 Bug（即 Crash Oracle）；随后的 [SQUIRREL（2020）](https://arxiv.org/abs/2006.02398) 和 [SQLRight（2022）](https://github.com/PSU-Security-Universe/sqlright) 引入了结构化变异、语法语义修复与代码覆盖率反馈，把随机触角伸进了更深的内核分支。但只要没有独立的结果判定器，哪怕一条查询少算了一半数据，进程照样返回成功。

最直观的补救思路是**差分测试（Differential Testing）**：把同一条 SQL 同时发给 PostgreSQL、DuckDB 和被测引擎，谁跟大家不一样谁就有嫌疑。但差分测试很快就会撞上两个痛点：一是各家方言在隐式类型转换、溢出报错、排序稳定性上本来就各执一词，海量“假阳性”噪声会淹没真正的 Bug；二是遇到反直觉的优化规则边界，两个引擎完全可能犯下一模一样的错。[SemConT](https://arxiv.org/abs/2406.09469) 尝试从源头消解“两个实现一起错”的盲区——它把受限 SQL 子集的形式化语义写成可执行的 Prolog 参考模型，用显式规则替代“拿老牌数据库当标准答案”，只是形式化模型覆盖的语法范围与目标产品的兼容契约仍需逐条对齐。

有没有办法**不依赖外部参考引擎，只靠被测数据库自己跟自己对账**，就能抓出逻辑错误？[SQLancer](https://github.com/sqlancer/sqlancer) 将 Schema/数据生成与逻辑判定解耦，集成了一整套精妙的变形测试方法（详见 [SQLancer approaches](https://github.com/sqlancer/sqlancer#approaches-and-papers)）。它的第一性原理非常干净：虽然我算不出复杂查询 `Q(D)` 的绝对正确结果，但我可以构造一组语义上必然成立的变换关系——如果引擎自己算出的左右两边对不上，内核里一定有一边撒了谎。

看看其中最具代表性的两条构造思路，就能体会这种四两拨千斤的效果。

第一条是 **[TLP（Ternary Logic Partitioning，OOPSLA 2020）](https://www.research-collection.ethz.ch/bitstream/20.500.11850/456967/2/3428279.pdf)**。在 SQL 的三值逻辑（Three-Valued Logic）下，给定任意确定性谓词 `p`，表里的每一行数据在求值 `p` 时，必然且只能落入 `TRUE`、`FALSE`、`UNKNOWN`（即 `IS NULL`）三个互斥分区之一。于是，不带过滤的原查询结果，在多重集（Bag，保留重复行次数）意义下必然等于三个分支的 `UNION ALL`：

```sql
-- 原始查询：直接扫描全表
SELECT x FROM t;

-- TLP 分区重组查询：强迫优化器走三条不同的谓词过滤路径，再用 UNION ALL 拼回
SELECT x FROM t WHERE x > 0
UNION ALL
SELECT x FROM t WHERE NOT (x > 0)
UNION ALL
SELECT x FROM t WHERE (x > 0) IS NULL;
```

拿一张只有 5 行的小表 `t.x = [NULL, -1, 0, 1, 1]` 试一下：第一分支吐出 `[1, 1]`，第二分支吐出 `[-1, 0]`，第三分支吐出 `[NULL]`，合起来恰好还原完整的 5 个值。倘若某个索引裁剪或条件下推规则漏掉了 `UNKNOWN` 分支，`NULL` 就会人间蒸发；倘若测试器误把 `UNION ALL` 写成去重的 `UNION`，重复的 `1` 就会少掉一个。当然，要把 TLP 推广到更复杂的 SQL，测试生成器自身必须懂代数分解——遇到 `AVG(x)` 不能直接把三个分区的均值再求平均，必须拆成各分区的 `SUM` 之和除以 `COUNT` 之和；遇到 `COUNT(DISTINCT)`、`LIMIT`、窗口函数或非确定性函数（如 `RANDOM()`），也必须遵守各自的等价拆解前提。

第二条是 **[NoREC（Non-optimizing Reference Engine Construction，ESEC/FSE 2020）](https://arxiv.org/abs/2007.08292)**。优化器最喜欢对 `WHERE` 子句大刀阔斧地做索引选择、分区裁剪和谓词下推，却很少去改动 `SELECT` 投影列表里的复杂条件求和。于是我们可以把同一条过滤条件平移到投影表达式里，人为造出一个“难以被激进优化”的参照查询：

```sql
-- 待测路径：触发 WHERE 过滤下推、索引扫描或动态裁剪
SELECT COUNT(*) FROM t WHERE x > 0;

-- 参照路径：全表扫描逐行求值，绕开大部分过滤优化规则
SELECT COALESCE(SUM(CASE WHEN x > 0 THEN 1 ELSE 0 END), 0) FROM t;
```

在刚才的 5 行输入上，两条 SQL 都应该返回 `2`。注意参照查询外层包着的那层 `COALESCE(..., 0)` 绝不是多余装饰：一旦表 `t` 是空表，`COUNT(*)` 按标准返回 `0`，而空集上的 `SUM(...)` 会返回 `NULL`！这也点破了变形测试的铁律——测试框架构造的等价式本身必须严丝合缝，同时最好检查执行计划，确认两条 SQL 确实走了不同的内核路径，避免两条查询被同一个前端重写器改成了完全相同的计划而漏掉公共路径上的缺陷。

顺着这个思路往下看，近年顶会涌现的新 Oracle 其实都在变换不同的“不动点”：

| 代表工作 | 核心等价或锚定关系 | 最擅长猎杀哪类内核缺陷 | 适用前提与固有盲区 |
| --- | --- | --- | --- |
| **[PQS](https://www.usenix.org/conference/osdi20/presentation/rigger)**（OSDI 2020） | 先从表中挑定一行数据（Pivot Row），再反向合成保证使该行为 `TRUE` 的复杂谓词 | 复杂连接与表达式求值中的漏行错误 | 测试器内部需自带一套精确的表达式求值器来挑选谓词 |
| **[TLP](https://www.research-collection.ethz.ch/bitstream/20.500.11850/456967/2/3428279.pdf)**（OOPSLA 2020） | `TRUE / FALSE / UNKNOWN` 三值分区合并且保留多重集重数 | 三值逻辑处理、外连接空值与分区裁剪错误 | 含窗口、`LIMIT` 或不可加聚合时需专门设计合并算式 |
| **[NoREC](https://arxiv.org/abs/2007.08292)**（ESEC/FSE 2020） | `WHERE` 过滤计数与全表扫描 `CASE WHEN` 累加结果一致 | 索引扫描、谓词下推与过滤路径优化错误 | 主要针对过滤计数形态，不直接覆盖任意复杂输出结构 |
| **[DQP](https://nus-test.github.io/publication/2024-sigmod-dqp/)**（SIGMOD 2024） | 同一条 SQL 通过 Hint 或优化开关强行走不同物理计划，结果必一致 | Join 算法切换、并行算子与特定物理实现 Bug | 若错误藏在所有计划共享的前置解析或重写阶段，则双方同错 |
| **[EET](https://www.usenix.org/conference/osdi24/presentation/jiang)**（OSDI 2024） | 对查询内子表达式做恒等变换（如布尔/算术等价改写），结果不变 | 表达式折叠、短路求值与优化器规则交互 Bug | 改写必须严格避开整数溢出、除零异常改变与空值传播陷阱 |
| **[CODDTest](https://github.com/sqlancer/sqlancer/pull/1054)**（SIGMOD 2025） | 固定数据库快照下，将子查询或表达式替换为其预先求出的常量值，结果一致 | 常量折叠、常量传播与相关子查询绑定错误 | 必须精确控制变量作用域、类型精度与求值时机 |

### 2.2 跳出单次查询结果：性能退化与并发事务的判定器

除了“单条 `SELECT` 算没算对”，数据库还有两类致命问题无法靠核对静态结果集抓住。

一类是**性能与基数估计退化**。查询结果完全正确，但新版本耗时暴涨了 50 倍。针对这类问题，[APOLLO](https://github.com/sslab-gatech/apollo) 自动生成、缩减并定位跨版本性能回退用例；而 [CERT](https://arxiv.org/abs/2306.00355) 则把变形测试的思想搬进了优化器的基数估计器（Cardinality Estimator）——给一条查询追加更严格的过滤条件（例如把 `a > 10` 收紧成 `a > 100`），优化器估算出的输出行数理应单调不增。一旦发现“条件越严、估行越多”的反常跳跃，往往就揪出了统计直方图或相关性推导里的隐患，随后再结合真实执行计划核验它是否诱发了烂计划。

另一类是**并发事务与故障恢复异常**。压测工具汇报“每秒跑完十万笔事务且零报错”，丝毫不能证明系统兑现了可串行化（Serializable）或快照隔离（Snapshot Isolation）承诺。到了这一层，测试输入不再是一条 SQL，而是多客户端并发交错的读写序列与网络分区、进程强杀故障；判定器也随之升级为历史依赖图检查器——例如 [Jepsen](https://github.com/jepsen-io/jepsen) 注入分布式故障、[Elle](https://github.com/jepsen-io/elle) 从可观测的读写历史反推事务依赖环来捕获脏读、丢失更新与写偏序（Write Skew），以及 [OSDI 2023 的 Graph-Based Oracle Construction](https://www.usenix.org/conference/osdi23/presentation/jiang) 面向通用事务隔离级别自动构造图判定器。

---

## 三、从优化器前提反推反例：两行小数据如何击穿复杂规则？

盲目扩大随机表的数据量，在很多优化器 Bug 面前效率极低。原因很简单：**每一条查询改写规则背后，都依赖一组往往没有写进类型系统的隐式数学前提**——比如连接键是否唯一、谓词是否拒绝空值（Null-Rejecting）、标量子查询是否至多返回一行、两阶段聚合的中间状态是否满足结合律。只要顺着这些前提去构造“恰好踩破假设”的最小输入，两行数据就能让错误的优化规则当场现形。

### 3.1 两个经典的最小语义反例

先看**外连接谓词下推**。设左表 `a` 只有一行 `(1)`，右表 `b(k, v)` 也只有一行 `(1, 7)`：

```sql
CREATE TABLE a(k INTEGER);
CREATE TABLE b(k INTEGER, v INTEGER);
INSERT INTO a VALUES (1);
INSERT INTO b VALUES (1, 7);

-- 原始查询：先做左外连接得到 (1, 7)，再过滤 COALESCE(7, 0) = 0，正确结果为空集
SELECT a.k, b.v
FROM a LEFT JOIN b ON a.k = b.k
WHERE COALESCE(b.v, 0) = 0;

-- 错误改写示例：若优化器把过滤条件提前压进右表输入
SELECT a.k, b2.v
FROM a
LEFT JOIN (
  SELECT * FROM b WHERE COALESCE(v, 0) = 0
) AS b2 ON a.k = b2.k
WHERE COALESCE(b2.v, 0) = 0;
```

对比一下发生了什么：原查询里 `a` 和 `b` 成功连上，产出 `(1, 7)`，接着被 `WHERE` 干净利落地滤掉，最终返回空集。可一旦某条不够严谨的优化规则把 `COALESCE(v, 0) = 0` 提前压进了 `LEFT JOIN` 右侧，`b` 表里的 `(1, 7)` 在连接前就被提前杀死了！随后 `LEFT JOIN` 看到左表 `1` 找不到右表伙伴，按语义贴心地补出一行 `(1, NULL)`；这行新造出来的 `NULL` 撞上外层的 `COALESCE(b2.v, 0) = 0`，摇身一变成了 `0 = 0`，堂而皇之地混进了最终结果！这个两行反例直观地说明了：任何不拒绝 `NULL` 的上层谓词，绝不能越过外连接的补空侧提前下推。

再看大模型和初级开发者最常混淆的 **`NOT IN` 与 `NOT EXISTS` 互换**：

```sql
CREATE TABLE outer_t(k INTEGER);
CREATE TABLE inner_t(k INTEGER);
INSERT INTO outer_t VALUES (1);
INSERT INTO inner_t VALUES (NULL);

-- 查询 A：计算 1 <> NULL 得到 UNKNOWN，NOT IN 结果为空集
SELECT k FROM outer_t WHERE k NOT IN (SELECT k FROM inner_t);

-- 查询 B：相关子查询无匹配行返回，NOT EXISTS 结果返回 1
SELECT k FROM outer_t
WHERE NOT EXISTS (
  SELECT 1 FROM inner_t WHERE inner_t.k = outer_t.k
);
```

只要内表里混进哪怕一个 `NULL`，`x NOT IN (SELECT y ...)` 展开成的合取式 `x <> y1 AND x <> NULL` 永远不可能为 `TRUE`，整条查询瞬间颗粒无收；而按等值条件关联的 `NOT EXISTS` 只关心“存不存在让 `inner_t.k = outer_t.k` 为真的行”，遇到 `NULL` 判定无匹配，照常把外表的 `1` 吐出来。因此，任何反半连接（Anti Join）去相关改写都必须把“外表键可空、内表为空集、内表含 `NULL`”四象限逐一测透；同时这也提醒我们，绝不能轻信大模型随口声称的“这两条 SQL 等价”，否则测试器会把完全合法的三值逻辑差异误报成引擎 Bug。

沿着同样的第一性原理，内核里常见高危优化的反例靶心可以一目了然地列出来：

| 内核优化机制 | 隐含的成立前提 | 定向构造的最小破防输入 |
| --- | --- | --- |
| **连接消除（Join Elimination）** | 被消除侧连接键唯一，且外键匹配存在 | 无匹配行、一对多重复匹配、可空连接键（在未声明主键约束的表上验证） |
| **标量子查询去相关** | 子查询对每个外层行至多返回一行，空集补 `NULL` | 子查询返回 0 行、多行报错边界、外层存在重复键与 `NULL` 键 |
| **两阶段聚合拆分（Partial/Final Agg）** | 局部状态可无损合并，空输入与溢出行为一致 | 空分组、全 `NULL` 输入、高精度 Decimal 累加溢出临界点、乱序合并 |
| **分区裁剪（Partition Pruning）** | 分区元数据、时区/排序规则与查询比较算子完全同构 | 恰好卡在分区右开/闭边界的值、`NULL` 分区、夏令时跨越与过期统计元数据 |
| **Top-K 与延迟物化** | 排序键与物理行号（Row ID）映射在重排/过滤后不漂移 | 大量并列排序值（Ties）跨向量批次切分、带删除标记行的稀疏延迟回表 |

### 3.2 联合分布倾斜与物理阈值的“骑墙测试”

抓逻辑错数靠两行精巧反例，抓优化器选错计划和执行器崩溃则要瞄准**列间联合分布**与**物理状态切换阈值**。

许多基准生成器只管单列的不同值个数（NDV）和最大最小值，但优化器最容易栽跟头的地方恰恰是单列统计看不见的列间耦合。设想表里有 `x` 和 `y` 两列，取值都是 `1..100` 且每个数字各出现一万次——单看每一列的直方图，下面三份数据一模一样：

1. **数据集 A**：`x` 与 `y` 完全独立随机配对，`WHERE x = 7 AND y = 7` 命中约 `1` 行；
2. **数据集 B**：严格满足 `y = x`，同样的过滤条件瞬间命中 `10,000` 行，与独立假设估算出的基数整整差了四个数量级；
3. **数据集 C**：99% 的行满足 `y = x`，唯独 1% 的异常行打破规律，专治那些采样过浅就盲目推导函数依赖的激进策略。

把均匀表从一千万行灌到一亿行，远不如造出 A、B、C 这三组对照更能逼出基数估计与 Join 顺序的死角。

而在执行引擎内部，真正的暗礁几乎全埋在物理状态切换的临界点 `B` 附近——构造输入时应当专门围绕 `B - 1`、`B`、`B + 1` 做“骑墙扫描”：

- **向量批次（Batch Size，如 1024 或 2048 行）**：空批次、恰好装满一整批、最后一批只剩孤零零 1 行；
- **字符串内联边界（如 12 字节前缀内联）**：按 UTF-8 字节长度（而非字符个数）卡在 11、12、13 字节的多语言字符串比较与哈希；
- **编码与哈希表切换**：字典编码（Dictionary Encoding）退化为平坦向量的临界 NDV、哈希表触发扩容重哈希（Rehash）且伴随长冲突链的瞬间；
- **内存溢写（Spill）边界**：算子内存刚好够用、第一次触发落盘、极端热点分组迫使溢写分区发生**递归二次落盘（Recursive Spill）**。

这里必须立下一条硬规矩：**声称测试某个内部物理分支，就必须拿到运行时计数器或计划签名作为铁证。** 由于不同类型宽度、内存分配器碎片和并发线程数都会移动真实的溢写触发线，单凭“我灌了十万行数据”根本不作数；如果跑完用例一看 `spill_bytes == 0`，这条测试只能归类为 `not_covered`（未触达目标路径），绝不能算作溢写分支测试通过。

---

## 四、跨出平坦关系表：复杂类型、向量过滤、增量维护与性能悬崖

现代分析与湖仓引擎早已不局限于平坦的整数和字符串。当嵌套类型、半结构化 JSON、近似向量检索与增量物化视图交织进同一条查询时，输入契约和结果比较器都必须跟着升级。

### 4.1 嵌套类型与列式编码：固定逻辑边界值，轮换物理表示

在引擎里写一句 `supports_array = true` 很容易，但真正的支持意味着从语法解析、类型推导、列式字典编码、向量选择掩码（Selection Vector），一直到分组聚合落盘再读回，全链路都不丢状态。测试 `Array`、`Map`、`Struct` 时，与其随机塞几万个普通整数列表，不如先锁死最容易让空值位图（Null Bitmap）坍缩的 8 个最小逻辑值：

```text
NULL               （整个列表字段为 SQL NULL）
[]                 （非空列表，但长度为 0）
[NULL]             （含 1 个空元素的列表，长度必须为 1）
[1, NULL, 1]       （混合重复值与空元素，保序且保重数）
NULL struct        （整个结构体为 SQL NULL）
{a: NULL}          （结构体本身非空，内部字段 a 为 NULL）
{a: []}            （结构体内嵌空列表）
{a: [NULL]}        （结构体内嵌含 NULL 的列表）
```

如果是 `JSON` 或 `Variant` 半结构化列，还要把**字段缺失（Missing）**、**JSON 字面量 `null`** 与外层 **SQL `NULL`** 严格区分开。锁定这组逻辑值之后，借鉴 [Velox Expression Fuzzer](https://facebookincubator.github.io/velox/develop/testing/expression-fuzzer.html) 与 [Aggregation / Window Fuzzer](https://facebookincubator.github.io/velox/develop/testing/fuzzer.html) 的思路，保持逻辑输入不动，强行切换底层物理表示与执行路径——同一批值，从内存常量构造与从 Parquet 读入结果一致吗？套上非连续过滤掩码与字典压缩后做 `UNNEST` 一致吗？内存直接聚合与触发 Spill 落盘合并后一致吗？

配套的结果比较器也得按数据库类型系统重写：一旦测试脚本偷懒把所有输出 `toString()` 成字符串再转成集合（Set）比对，SQL `NULL`、字符串 `"null"` 和 JSON `null` 就会糊成一团，列表元素顺序和多重集重复次数也会被悄悄抹平。

### 4.2 非结构化链路与“向量 Top-K + 标量过滤”的顺序陷阱

当数据库接入文本检索与向量索引时，从原始字节到最终返回行中间隔着好几道工序：`原始文档解码 → 分块与元数据绑定 → Embedding 向量化 → 索引构建与可见性控制 → 混合过滤检索`。端到端跑通一句检索只说明管道通了：截断图片或非法 UTF-8 测的是解析器防御；文档删除后还能被搜出来抓的是索引快照可见性 Bug；而要隔离数据库自身逻辑与外部大模型服务的波动，就必须冻结预生成的向量，不能让上游 Embedding 模型的微小漂移背上“存储引擎回退”的黑锅。

在这条链路上，最容易被单一召回率（Recall）数字掩盖的语义坑，是**带标量过滤条件的近似最近邻检索（Filtered Vector Search）**。只要接口向用户承诺“返回满足过滤条件 `predicate(v)` 的前 `K` 个最近邻”，正确的基准真值（Ground Truth）就必须在过滤后的合法候选子集上计算：

$$\text{Candidates} = \{v \in D \mid \text{predicate}(v)\}, \qquad \text{Truth} = \operatorname{ExactTopK}(\text{Candidates}, q, \text{metric})$$

为什么不能先在全局向量索引里取出最近的 `K` 个点，再套上 `WHERE` 过滤（Post-filtering）？构造一个只有两个点的极简反例就一清二楚：点 `p1` 离查询向量距离为 `1`，但不满足权限或租户过滤条件；点 `p2` 距离为 `2`，满足过滤条件。当查询请求 `K = 1` 时，`Filter(TopK(D, 1))` 先抓出 `p1` 随后滤掉，直接返回**空集**；而符合契约的 `TopK(Filter(D), 1)` 则会正确返回 `p2`！因此，向量检索测试必须拆成三层核对：**硬正确性**（绝不返回越权、已删除或不满足 `WHERE` 谓词的脏数据）、**近似质量契约**（在约定参数下统计相对于过滤后精确真值的 Recall 与结果不足率），以及**退化边界**（合法候选数少于 `K` 甚至为 0 时的分母处理、等距并列点的打破规则，以及索引后台重建期间的并发读写可见性）。

### 4.3 增量计算的时间轴与性能测试的“幸存者偏差”

一旦测到物化视图增量维护（IVM）或流式更新，静态快照数据集就不够用了——测试输入必须拉出一条包含生老病死的**时间轴**。依然拿 `LEFT JOIN` 举例，一次插入跑通说明不了问题，连续推进下面五步状态跃迁才能摸到计数器的底：

```text
S0：左表插入 (1)，右表为空             → 视图应输出补空行 (1, NULL)
S1：右表插入第 1 条匹配 (1, 7)         → 之前的补空行必须撤回，输出变为 (1, 7)
S2：右表再插入重复匹配 (1, 7)          → 多重集计数加一，输出两行 (1, 7)
S3：右表删除其中一条 (1, 7)            → 仍剩一条匹配，绝不能提前恢复 (1, NULL)
S4：右表删除最后一条匹配 (1, 7)        → 匹配归零，视图必须重新补回 (1, NULL)
```

每走完一步，都在相同的已提交快照水位上，把增量刷新后的视图内容与基表全量重算的多重集逐行比对，中间但凡漏掉一次撤回（Retraction）或把引用计数算错，立刻无所遁形。

回到**性能回归测试**，它与正确性测试应当共享同一批可重放用例，但必须遵守一套严苛得多的测量与缩减协议：

1. **先验对错，再量快慢**：新版本快了 30% 但少吐出一半数据是严重事故；反过来，修复了漏算数据的 Bug 导致耗时增加，也不能糊里糊涂算作性能劣化。
2. **隔离缓存与系统漂移**：独立预热后，采用基线与候选交替运行的 `ABBA` 配对测量（例如起步至少 10 组配对样本观察方差，同时设定中位耗时比 `> 1.2` 且绝对增量 `> 50 ms` 才进入异常候选）；区分冷暖数据时要格外小心——只重启数据库进程清不掉操作系统的 Page Cache。
3. **警惕超时删失（Censored Observation）与幸存者偏差**：如果基线跑 1 秒，新版本卡死到 60 秒被超时强杀，只能记录“回退幅度至少超过 60 倍下界”，既不能把 60 秒当成真实完成耗时，更不能在汇总报告时“只挑双方都跑完的查询算几何平均加速比”——把跑崩跑超时的慢查询剔除出分母，是性能报告最常见的自欺欺人。
4. **缩减用例时守住触发条件**：正确性 Reducer 习惯疯狂删行删列，可性能悬崖往往高度依赖数据规模或倾斜度；一旦把十万行删成十行，溢写（Spill）消失了，性能回退也跟着消失了。因此性能 Reducer 的保留判据（Interestingness Predicate）必须同时锁死“结果一致 + 耗时比值仍超阈值 + 目标物理路径/计数器依然命中”，只二分裁剪无关分支，最终留下能稳定复现该性能悬崖的最小实验。

把复杂类型、存储状态与系统边界浓缩在一张检查表里，日常设计测试矩阵时就可以按对角线做成对组合（Pairwise Coverage），再对高危三元组（如 `NULL × 字典编码 × 过滤掩码`、`Decimal × 两阶段聚合 × Spill`、`Outer Join × 空值敏感谓词 × Runtime Filter`）做重点爆破：

| 引擎边界维度 | 必须区分的最小输入或状态 | 核心判定依据（Oracle） | 证明路径触达的运行证据 |
| --- | --- | --- | --- |
| **空值、数值与时区** | SQL `NULL`、空串、Decimal 极值/缩放、`NaN`/`±0`、夏令时跳变 | 方言契约 + TLP / 参考实现 | 实际输出类型、异常码与会话时区/排序规则记录 |
| **嵌套与半结构化类型** | `NULL`、`[]`、`[NULL]`、Missing 键、JSON `null` | 递归类型化比对 + 跨物理表示差分 | Null Bitmap 未坍缩，Flat/Dictionary 编码均命中 |
| **列式算子、内存与调度** | 非连续 Selection Vector、空窗口帧、Spill、提前取消的 `LIMIT` | 单/多阶段计划差分 + 资源回收不变量 | `spill_bytes > 0`、取消/重试计数器与内存回落 |
| **外表、索引与分布式恢复** | 截断 Parquet、Schema 演进、MV 并发刷新、Worker 崩溃重放 | 独立 Reader / 基表重算 / 事务历史检查器 | 快照水位一致，故障恢复后无脏文件或重复提交副作用 |

---

## 五、LLM 时代的模糊测试：让模型补齐语法盲区，把裁决权留给确定性工具

手工给每个数据库方言写一套覆盖几百个内置函数、窗口帧语法与专有扩展的随机生成器，工程维护成本极高。这两年大模型进入数据库测试领域，几项代表性工作恰好切中了不同的痛点：

- **拓宽分析负载形态（[SQLStorm，PVLDB 2025](https://db.in.tum.de/~schmidt/papers/sqlstorm.pdf)）**：用大模型在真实数据集上合成了 18,251 条复杂分析查询，显著突破了传统基准几十个固定模板的计划同质化，用来压测解析、优化与执行多样性非常顺手。
- **自动适配多引擎方言（[SQLancer++](https://arxiv.org/abs/2503.21424v2)）**：通过与目标数据库交互探测哪些语法特性受支持，大幅降低为新引擎逐条手写生成器的门槛。
- **往成熟测试器里注入缺失特征（[ShQveL](https://arxiv.org/abs/2505.02012v2)）**：不让大模型漫无目的地瞎编整条查询，而是用带占位槽位的 SQL 草图（SQL Sketch）把模型合成的新函数/语法片段嵌入现有的变形测试循环（作者论文报告在 5 个主流 DBMS 中找出了 55 个未知 Bug，其中 50 个获官方修复）。
- **探索长期被冷落的运维与状态特性（[FuzzySQL](https://arxiv.org/abs/2602.19490)）**：瞄准 GTID 复制、存储过程、`KILL` 并发中断等传统 SQL 生成器极少触及的专有状态序列，结合变异修复与崩溃重放验证挖掘深层故障。

把这些前沿探索放回工程流水线里审视，最核心的架构原则只有一条：**让大模型干它最擅长的“读文档提假设、填补语法盲区”，把“绑定约束、执行度量与对错裁决”死死留在确定性程序手里**——绝不能既让模型出题，又让模型凭语感批改试卷。一旦沙箱执行触发判定器违例或崩溃，再交由 AST 感知的缩减器（如 `C-Reduce` 或 Delta Debugging）在锁死失效判据的前提下剥离无关语法分支，沉淀出最小可复现用例：

![LLM 引导的模糊测试与确定性缩减闭环](/images/diagrams/dbres-llm-fuzz-reduce-loop.svg)

比如团队刚刚重构了 `HashAggregate` 中间状态的序列化代码，与其笼统地让模型“生成一万条复杂 SQL”，不如把状态结构定义、合并约束和溢写触发条件喂给它，定向要求生成覆盖“全 `NULL` 分组、空分区、超长变长键、高精度 Decimal 溢写合并”的 SQL Sketch。

在落地这套协同循环时，有一个极隐蔽的反模式必须防范：**“为了修通语法而把测试目标修没了”**。设想我们本来要测 `JSON 路径提取 + 窗口函数 + LEFT JOIN` 的组合边界，结果初版 SQL 报了个绑定错误；如果自动修复循环只以“查询跑成功（Exit Code 0）”为奖励目标，模型最省事的改法就是删掉报错的 JSON 和窗口子句，把它退化成一句人畜无害的 `SELECT k FROM t`——表面上看“有效执行率”飙升到了 99%，实际上高危交互分支一次都没测到！

因此，每条下发给生成与修复环节的任务都必须锁死不可妥协的**覆盖不变量**（例如强制检查计划与运行证据必须同时命中 `nested_projection`、`left_outer_join` 与 `spill_bytes > 0`），修不通就诚实记为 `not_covered`，绝不允许靠删减测试条件来刷绿灯。同理，评估“引入 LLM 到底值不值”也必须在同等 CPU 墙钟预算（外加单独核算的 Token 推理账单）下，与纯手写生成器做严格的消融对照（Ablation），按时间切分隔离历史公开 Issue 以防模型背题，只认**新增有效物理分支**与**去重后的独立根因缺陷数**，最后把抓出 Bug 的最小复现包固化为静态文件——日常 CI 回归只跑确定性构件，无需每次提交都去调大模型。

---

## 六、把探索沉淀为工程资产：契约清单、比较器与分池演进

当测试从一次性找茬变成长期演进的基础设施时，每一个沉淀下来的回归用例都应当自带一份自包含的契约清单（Manifest），把输入哈希、语义前提、判定方式和必须命中的运行证据绑在一起：

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
  preconditions: [fixed-snapshot, no-volatile-functions]
oracle:
  kind: explicit-expected
  expected: expected.json
coverage:
  required: [left-outer-join, nullable-right-output, null-accepting-predicate]
execution:
  seed: 17
  timeout_seconds: 30
artifacts: [logical-plan.json, physical-plan.json, profile.json, engine.log]
```

围绕这份契约，整套测试底座的日常运转靠三块基石撑住：

1. **细粒度结论分类与类型化比较器**：告别非黑即白的 `0 / 1` 退出码，明确区分 `pass / result_mismatch / unexpected_error / crash / timeout / expected_rejection / not_covered / inconclusive`。无序查询默认按保留重数的**类型化多重集（Typed Multiset）**精确比对；带 `ORDER BY` 但存在并列值（Ties）时，验证等价组内的合法排列或补齐确定性决胜键；浮点数严格区分精确位级契约与允许重排误差的聚合计算，绝不给所有数值一刀切套个粗糙的 `epsilon` 掩盖精度事故。
2. **按预算切分四个测试池**：
   - **语义核心池（每次提交必跑）**：几行小表、毫秒级执行、明确预期答案与历史修复 Bug，追求零噪点秒级定位；
   - **功能组合池（PR 或每晚定时）**：运行 TLP/NoREC 变形验证、列式编码与计划变体差分，强制核验覆盖证据；
   - **性能鲁棒性池（独占隔离硬件）**：运行冻结的 Benchmark 代表子集、列间相关性与内存阈值骑墙扫描，保留原始配对样本与资源画像；
   - **开放探索池（受预算约束的后台长任务）**：持续运转覆盖率引导的 Fuzzer、LLM Sketch 合成与故障注入序列，专职猎杀未知缺陷。
3. **用变异测试（Mutation Testing）反测判定器自身**：测试跑得全绿，究竟是因为内核真没 Bug，还是因为判定器坏了？定期在测试环境里故意注释掉一条 `null-safe` 保护、故意漏掉 TLP 的 `IS NULL` 分区、或者把多重集比较器偷偷改成集合去重——如果对应的测试池没有立刻报警变红，说明要么数据没激活分支，要么 Oracle 存在漏网之鱼。

如果团队打算从零起步搭建这套能力，最务实的推进节奏不是一上来铺大摊子接大模型，而是选定一个高风险语义切面（例如 **Outer Join + NULL + Runtime Filter**，或者 **嵌套类型 + 两阶段聚合 + Spill**）走好三步：先手写十几个两三行的小表反例并打通 `typed-bag` 比较器；再接入一个公开 Benchmark 子集与 TLP/算子差分判定器，把相关性参数和路径命中证据跑顺；等确定性底座立稳了，最后再引入 LLM 定向填补未覆盖的函数与语法组合，用实打实的独立新缺陷数检验投入产出。

---

## 参考资料与推荐阅读顺序

想把上述机制的来龙去脉一次读透，可以沿着下面五步顺藤摸瓜：

1. **变形判定入门**：先读 [NoREC（ESEC/FSE 2020）](https://arxiv.org/abs/2007.08292) 与 [TLP（OOPSLA 2020）](https://www.research-collection.ethz.ch/bitstream/20.500.11850/456967/2/3428279.pdf)，看清没有标准答案时如何靠三值逻辑与代数重写抓出逻辑错数；
2. **SQL 层与算子层双轮驱动**：对照 [SQLancer](https://github.com/sqlancer/sqlancer)（含 [PQS](https://www.usenix.org/conference/osdi20/presentation/rigger)、[DQP](https://nus-test.github.io/publication/2024-sigmod-dqp/)、[CODDTest](https://github.com/sqlancer/sqlancer/pull/1054)）与 [Velox Expression](https://facebookincubator.github.io/velox/develop/testing/expression-fuzzer.html) / [Aggregation & Window Fuzzer](https://facebookincubator.github.io/velox/develop/testing/fuzzer.html)，体会高层 SQL 等价变换与底层向量物理表示差分的分工；
3. **多元判定扩展**：继续读 [SemConT](https://arxiv.org/abs/2406.09469)、[EET（OSDI 2024）](https://www.usenix.org/conference/osdi24/presentation/jiang) 与 [Jepsen](https://github.com/jepsen-io/jepsen) / [Elle](https://github.com/jepsen-io/elle) / [OSDI 2023 事务图判定器](https://www.usenix.org/conference/osdi23/presentation/jiang)，补齐形式化语义模型、子表达式变换与并发事务历史验证；
4. **性能悬崖与负载多样性**：结合 [APOLLO](https://github.com/sslab-gatech/apollo)、[CERT](https://arxiv.org/abs/2306.00355) 与 [SQLStorm（PVLDB 2025）](https://db.in.tum.de/~schmidt/papers/sqlstorm.pdf)，连接基数单调性检查、性能回退缩减与大规模负载构造；
5. **LLM 协同探索前沿**：对比 [SQLancer++ v2](https://arxiv.org/abs/2503.21424v2)、[ShQveL v2](https://arxiv.org/abs/2505.02012v2) 与 [FuzzySQL](https://arxiv.org/abs/2602.19490)，看清方言自适应探测、SQL Sketch 特征合成与运维状态序列探索各自的能力边界。

---

> **调研基线与小域验证说明**：本文公开文献与开源项目资料检索截至 **2026-09-18**。文中引用的五组最小语义反例（TLP/NoREC 三值分区与空表 `COALESCE`、Outer Join 提前下推补 `NULL`、`NOT IN` 与 `NOT EXISTS` 遇 `NULL` 差异、Outer Join 五步增删状态跃迁、`Filter` 与 `Top-K` 先后顺序反例）已于 **2026-10-07** 使用 Python 标准库内存 SQLite（`3.51.3`）完成多重集语义核验与遗漏 `UNKNOWN` 分支/去重的负向回归检查；`SQLancer++`、`ShQveL`、`FuzzySQL` 依据论文描述讨论其架构机制，未在本地复现实验原型，第六节中的测试清单与分池方案为基于上述工作的工程落地设计。
