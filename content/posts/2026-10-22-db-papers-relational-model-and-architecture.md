---
title: "【Paper】从关系模型到数据库内核架构：Codd、INGRES、SEQUEL、Postgres 与现代 DBMS 全局解剖"
date: 2026-10-22T12:00:00+08:00
lastmod: 2026-11-12T00:00:00+08:00
slug: "db-papers-relational-model-and-architecture"
description: "从导航式物理指针耦合的工程困境出发，串联 Codd 1970 关系模型与 RM/T 语义扩展、INGRES QUEL 与 SEQUEL 查询语言分野、C. J. Date 对 SQL 多重集与三值逻辑的等价性批判、Postgres 可扩展内核复盘，直至现代关系数据库进程、执行、缓冲与 ARIES WAL 全局架构边界。"
draft: false
toc: true
math: true
categories: ["数据库"]
tags: ["论文精读", "关系模型", "SQL", "数据库架构"]
series: ["数据库经典论文精读"]
---

> 本系列第 1/10 篇 · [数据库经典论文精读总索引]({{< relref "2026-10-22-database-papers-source-reading.md" >}}) · [下一篇：查询优化器演进]({{< relref "2026-10-22-db-papers-cascades-properties-and-cost.md" >}})

如果要设计一套供上百个业务程序共享读写的持久化数据系统，最朴素的做法是什么？1960 年代的层次型数据库（如 IBM IMS）与网状数据库（CODASYL DBTG）给出了符合直觉的机械答案：把业务实体存成磁盘上的定长记录，再用物理块地址或双向链表指针把“部门 -> 员工 -> 项目”串成树与网。只要查询顺着预埋的指针链往下跳，单次查找的速度快得惊人。

然而，一旦业务负载发生演化，这套紧耦合架构就会立刻触发雪崩。假设原本按“供应商 -> 零件”组织的指针树，因为采购部门新增了高频的“按零件反查所有合格供应商”需求，管理员不得不调整底层的物理聚簇顺序、拆分溢出页或删改辅助索引链；瞬间，所有在循环代码里写死了 `GET NEXT WITHIN PARENT` 导航路径的应用程序全部崩溃重写。**应用逻辑被迫为物理存储布局陪葬**——这就是 E. F. Codd 在 1970 年发起关系革命时直面的第一性原理矛盾。

Codd 给出的解药是用数学上的**集合关系（Set Relation）**彻底斩断物理指针：用户只声明“我要满足什么谓词的数据集合”，至于走索引、扫堆表还是做哈希连接，全部下沉给数据库系统内部裁决。但当系统工程师真正动手把纯粹的一阶谓词逻辑变成工业级数据库时，两场跨越半个世纪的硬仗才刚刚打响：

1. **集合纯粹性与工程查询语言的妥协裂痕**：为了省去中间结果频繁排序去重的物理开销，并表达现实世界中的缺失信息，商业化的 SQL 引入了允许重复行的多重集（Bag）与三值逻辑（Three-Valued Logic, 3VL）`NULL`。这两个看似贴心的实用补丁，在底层直接击穿了大量经典代数恒等式。
2. **声明式黑盒背后的状态所有权切分**：当应用程序交出导航控制权后，数据库内核自己必须接管并发调度、重写优化、零拷贝页驻留、毫秒级锁与纳秒级闩锁隔离，以及崩溃后跨页结构不变量的原子恢复。

在展开本篇的七篇奠基文献之前，我们先在白板上推演两个只有两行数据的 SQL 反例，亲眼看看多重集与三值逻辑是如何在毫不起眼的地方让等价改写当场翻车的。

---

## 白板推演：两行数据如何击穿代数恒等式

设有一张两行两列的极简表 `T(x, y) = {(1, NULL), (NULL, 2)}`。在线性代数与普通集合运算里，和的求和恒等于求和的和，平均值恒等于总和除以基数。但在任何遵循标准 SQL 语义的引擎里运行下面两条语句：

```sql
SELECT SUM(x + y) AS sum_expr,
       SUM(x) + SUM(y) AS expr_sum,
       AVG(x) AS avg_col,
       SUM(x) * 1.0 / COUNT(*) AS sum_div_cnt
FROM T;
```

第一行计算 `1 + NULL` 得 `NULL`，第二行计算 `NULL + 2` 也得 `NULL`；`SUM` 算子自动忽略所有输入 `NULL`，面对全空输入最终吐出 **`NULL`**。而右侧的 `SUM(x)` 忽略第二行算出 `1`，`SUM(y)` 忽略第一行算出 `2`，两者相加算出 **`3`**——`SUM(x + y)` 与 `SUM(x) + SUM(y)` 在两行数据上彻底分道扬镳！同理，`AVG(x)` 只按非空行计数算出 `1 / 1 = 1.0`，而 `SUM(x) / COUNT(*)` 把含空值的第二行也计入分母，算出 `1.0 / 2 = 0.5`。

再看查询优化器最常遇到的子查询改写。设外表行满足 `x = 5`，内表 `S(y) = {(2), (NULL)}`。业务写出全称比较谓词 `WHERE 5 > ALL (SELECT y FROM S)`，新手优化器很容易按德摩根定律（De Morgan's Law）将“大于所有元素”改写为“不存在不大于的元素”：

```sql
-- 原查询：5 > ALL ({2, NULL})
SELECT 1 WHERE 5 > ALL (SELECT y FROM S);

-- 朴素德摩根改写：NOT EXISTS (... WHERE NOT (5 > y))
SELECT 1 WHERE NOT EXISTS (
    SELECT 1 FROM S WHERE NOT (5 > y)
);
```

逐行核对真值：在原查询中，`5 > 2` 为 `TRUE`，`5 > NULL` 为 `UNKNOWN`，合取结果 `TRUE AND UNKNOWN` 等于 **`UNKNOWN`**，顶层 `WHERE` 只放行 `TRUE`，因此返回 **0 行**。而在改写后的子查询里，第一行 `NOT (5 > 2)` 为 `FALSE` 被内层 `WHERE` 丢弃；第二行 `5 > NULL` 为 `UNKNOWN`，**在三值逻辑中 `NOT UNKNOWN` 依然是 `UNKNOWN`**，同样被内层 `WHERE` 静默丢弃！结果内层子查询返回空集，外层 `NOT EXISTS (∅)` 赫然判定为 **`TRUE`**，凭空吐出 **1 行**错误结果。

为什么 Codd 最初的数学模型完全免疫这类陷阱？QUEL 与早期 SEQUEL 是怎样一步步滑向这套语义的？Postgres 与现代 DBMS 内核又是如何把声明式查询安全落地到磁盘扇区与内存槽位上的？下面我们顺着历史的因果链条逐篇拆解。

---

## 关系公理与语义边界：从 Codd 集合解耦到 RM/T 实体身份

从物理指针迷宫走向声明式查询语言，第一步是在数学上确立“哪些信息属于逻辑契约、哪些细节属于随时可换的物理外衣”。下图串联了从导航指针耦合、Codd 集合关系、QUEL 与 SEQUEL 分野，一直到 SQL 多重集与三值逻辑改写陷阱的完整演化路径：

![从导航指针到 SQL 多重集与三值逻辑陷阱的演进脉络](/images/diagrams/paper-rel-model-to-sql-traps.svg)

### Codd (CACM 1970)：用关系集合切断物理指针依赖 {#paper-41}

- **论文来源**：E. F. Codd, *A Relational Model of Data for Large Shared Data Banks*, Communications of the ACM 13(6), June 1970, pp. 377–387 · [原始 PDF（共 11 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/essentials/a-relational-model-of-data-for-large-shared-data-banks.pdf)（核心图示见 PDF p. 3 `FIG. 1–2` 的度与重复域角色关系，以及 PDF p. 7 `FIG. 5–7` 的广义连接与自然连接对比）。

Codd 在开篇（PDF p. 1–3）就把靶子立得毫不含糊：大型共享数据库一旦增长，存储表示就必然重排；要把应用程序彻底保护起来，必须切断三类最致命的物理耦合——**排序依赖（Ordering Dependence）**、**索引依赖（Indexing Dependence）**与**访问路径依赖（Access Path Dependence）**。当一个拥有 \(n\) 个部分的关系在磁盘上按某条树路径存放时，一旦业务代码绑定了这条树路径，面对其余 \(n! - 1\) 种访问顺序就会束手无策。

为了消灭位置与指针依赖，Codd 分两层定义了“关系”（PDF p. 3–4）：在纯数学形式层，\(n\) 元关系是笛卡尔积 \(S_1 \times S_2 \times \dots \times S_n\) 的子集，此时域的位置仍有意义；到了面向用户的数据子语言层，当同一个域在关系中重复出现（如零件装配表 `component` 同时包含两个 `part` 域，见 `FIG. 2`），直接按列名引用会产生歧义，必须引入**角色名（Role Name，如 `sub.part`、`super.part`）**唯一标识每一列。经过命名化的二维数组表示遵守严苛的集合不变量：每一行代表一个 \(n\) 元组，行的次序毫无意义，所有行互不相同，列的排列次序同样无关紧要。阅读原文时需留心一处早期术语差异：Codd 在第 4 页允许一张关系拥有多个“无冗余主键（nonredundant primary keys）”，这对应今天所说的**候选键（Candidate Keys）**，后来才演变为从中选定唯一主键。

在今天被统称为“范式化”的议题上，1970 年原文提出的 **Normalization（PDF p. 5）** 解决的其实是嵌套表消除问题，而非后来基于函数依赖的 3NF/BCNF。当时系统常在员工记录内部直接嵌套 `jobhistory` 表，在履历内部再嵌套 `salaryhistory` 表（即取值本身又是关系的**非简单域，Nonsimple Domain**）。Codd 给出的消除算法是**自顶向下将父关系的主键下推复制到子关系中**，把嵌套结构展平为清一色只含原子标量的二维表；该过程要求非简单域构成有向树森林，且任何主键自身不得包含非简单域。

在操作语义与系统实现的关系上，Codd 留下两句极具洞察力的论断——逻辑访问必须完全对称，而物理执行性能天然不对称；投影算子从定义上就自带去重义务：

> "Naturally, symmetry in performance is not to be expected." *(PDF p. 6)*

> "Suppose now we select certain columns of a relation (striking out the others) and then remove from the resulting array any duplication in the rows. The final array represents a relation which is said to be a projection of the given relation." *(PDF p. 7)*

尤其值得细读的是第 7–9 页对 **Join（连接）**、**Composition（复合）** 与 **Connection Trap（连接陷阱）** 的分析。在 Codd 1970 的术语里，广义的“连接”指**任何能无损保持原关系投影的候选三元关系 \(U\)**（即 \(\pi_{12}(U) = R\) 且 \(\pi_{23}(U) = S\)），这样的 \(U\) 往往不止一个！我们直接把原文 `FIG. 5–7`（PDF p. 7）的元组摆到桌面上对比：

| 输入关系 \(R(\text{supplier}, \text{part})\) | 输入关系 \(S(\text{part}, \text{project})\) | 自然连接 \(R * S\)（`FIG. 6`，共 5 行） | 另一保投影连接 \(U\)（`FIG. 7`，仅 3 行） |
| :--- | :--- | :--- | :--- |
| `(1, 1)` | `(1, 1)` | `(1, 1, 1)` | `(1, 1, 2)` |
| `(2, 1)` | `(1, 2)` | `(1, 1, 2)` | `(2, 1, 1)` |
| `(2, 2)` | `(2, 1)` | `(2, 1, 1)` | `(2, 2, 1)` |
| — | — | `(2, 1, 2)` | — |
| — | — | `(2, 2, 1)` | — |

核对右侧两列：无论是 5 行的自然连接 \(R * S\)，还是仅含 3 行的 \(U\)，抹去第三列去重后都恰好还原出 `{(1,1), (2,1), (2,2)}` = \(R\)，抹去第一列去重后也都恰好还原出 `{(1,1), (1,2), (2,1)}` = \(S\)！自然连接只是所有保投影连接中做笛卡尔全配对的**最大元（极大线性连接）**，只有在连接键无重复（点对点映射）时候选连接才唯一。

紧接着，如果我们把 \(R\)（供应商生产某零件）与 \(S\)（项目需要某零件）做复合（连接后抹去中间的 `part` 列），看到 `(supplier=1, project=1)` 出现在结果里，业务方极易误以为“供应商 1 是项目 1 的实际供货商”——但看一眼右边的真实三元关系 \(U\)，供应商 1 的 1 号零件其实只供给了项目 2，项目 1 用的 1 号零件是由供应商 2 提供的！这就是著名的**连接陷阱（Connection Trap）**。它揭示了一个深刻的建模边界：除非目标关系在**所有时刻（independent of time）**都能由其它关系通过给定算子无损推导（原文定义为强冗余 / O-redundancy，PDF p. 9–10），否则绝不能随意把一张独立的三元业务事实表拆成两张二元表再去拼凑。全文最后（PDF p. 10–11）将数据库一致性（Consistency）界定为瞬时快照集合 \(V\) 对声明式约束集 \(Z\) 的满足性校验，把何时检查、如何解决违例留给了后来的事务引擎。

### Codd (TODS 1979)：RM/T 系统代理键、语义图与元代数 {#paper-42}

- **论文来源**：E. F. Codd, *Extending the Database Relational Model to Capture More Meaning*, ACM Transactions on Database Systems 4(4), Dec 1979, pp. 397–434 · [原始 PDF（共 38 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/essentials/extending-the-database-relational-model-to-capture-more-meaning.pdf)（三值逻辑真值表见 PDF p. 3；特征实体层次结构见 PDF p. 19 `Fig. 3`）。

1970 年的基础关系模型把一切都化归为扁平原子值表格（Atomic Semantics），简洁归简洁，却很快暴露出一道现实痛点：如果用自然业务属性（如零件编号、员工工号）充当主键，一旦企业重组修改编号，所有引用该主键的子表外键都要级联重写；更糟的是，新登记的员工可能尚未分配工号，此时连主键本身都缺失！与此同时，1970 年代末兴起了大批语义数据模型（如 Chen 的实体—联系图），画出了漂亮的框线分类，却弄丢了关系模型最宝贵的闭合代数运算能力。Codd 在 RM/T 开篇一针见血地指出：

> "In recent papers on semantic data modeling there is a strong emphasis on structural aspects, sometimes to the detriment of manipulative aspects. Structure without corresponding operators or inferencing techniques is rather like anatomy without physiology." *(PDF p. 2)*

为了给语义解剖学配齐“代数生理学”，RM/T 首先将实体身份与用户业务键彻底剥离，引入全库唯一、由系统生成且永久不可修改的**代理键（Surrogate，取自专用 `E-domain`，PDF p. 14–15）**。用户永远看不见也改不了代理键的内部比特；当现实中发现两条记录其实指向同一个现实个体时，系统通过专门的 `coalesce` 操作合并两个代理键，自动完成全局引用归一。每种实体类型配有一张一元的 **`E-relation`**（仅存放存活实体的代理键集合，充当存在性总闸门），而实体的直接属性则拆入以代理键引用的 **`P-relations`** 中。

在此地基上，RM/T 用一套本身也是标准关系的目录图（Catalog Graph）刻画分子级语义（Molecular Semantics，PDF p. 18–27）。以原文 `Fig. 3`（PDF p. 19）的“员工 -> 工作履历 -> 调薪明细”三级特征层次为例，物理上不再使用任何业务工号做外键，而是完全靠系统代理键 `e#` 编织存在依赖网：

```text
/* 1. 一元存在总闸门（E-relations）：声明当前存活的实体代理键 */
EMP_E(emp¢)          = { α1, α2 }
JOBHIST_E(jh¢)       = { β1, β2 }
SALHIST_E(sh¢)       = { γ1 }

/* 2. 属性表与上下级特征绑定（P-relations & Characteristic Ties） */
EMP_PROP(emp¢, serial#, name)      = { (α1, 'E009', 'Alice'), (α2, ω_null, 'Bob') }  -- Bob 尚无工号仍可入库！
JOBHIST_PROP(jh¢, emp¢, jobdate)   = { (β1, α1, '1976-03'), (β2, α1, '1978-09') }
SALHIST_PROP(sh¢, jh¢, salary)     = { (γ1, β2, 24000) }

/* 3. 目录图元数据表 CG(sub, sup)：供内核自动实施级联存在检查 */
CG = { ('JOBHIST', 'EMP'), ('SALHIST', 'JOBHIST') }
```

看清这套分解的威力：新员工 `Bob` 入职当天哪怕工号还是未知空值 \(\omega\)，系统早已为他分配了永恒代理键 `α2`；日后 `Alice` 的工号从 `'E009'` 改成 `'E900'`，只需原地改 `EMP_PROP` 的一个单元格，`JOBHIST_PROP` 与 `SALHIST_PROP` 里的引用丝毫不动；而一旦 `α1` 从 `EMP_E` 中注销，内核顺着目录表 `CG` 自动递归抹除 `β1, β2` 及下属 `γ1`。在此之外，RM/T 进一步区分了另外三类语义图关系：
- **关联实体（Associative Entities，目录表 `AG`）**：将多方参与的联系（如一次采购合同）升格为拥有自己独立代理键的一等实体，甚至允许部分参与方暂时取值为未知空代理 `E-null`，删除下属参与方时关联实体自身无需强制销毁。
- **泛化层次与集合覆盖分离**：用 `UGI`（无条件 IS-A 子类继承，构成有向无环偏序图）与 `AGI`（互斥分支泛化）表达子类型语义，同时用 `KG` 单独表达“船队包含某艘船”这类不继承属性的集合成员（Cover Membership）关系，杜绝把 `member-of` 与 `subtype-of` 混为一谈；另设 `US`/`AS`/`UP`/`AP` 四张图关系表达事件流的前驱与后继编排。

更超前的是，RM/T为了让查询能跨未知表名做元编程与图遍历，把关系名本身纳入 `RN-domain`，设计了名字与关系互转的元算子（`NOTE`、`TAG`、`DENOTE`、`APPLY` 以及要求二元算子满足交换律和结合律的归约算子 `COMPRESS`，PDF p. 29–30）、针对有向图的传递闭包算子（`OPEN`、`CLOSE`、`STEP`，PDF p. 30–31），以及适配三值空值的 `TRUE DIVISION`、`MAYBE DIVISION` 和异构并集算子 `OUTER UNION`（先为缺列关系补空再求并，完全不同于外连接，PDF p. 10–11）。论文尾声（PDF p. 36）提出了数据模型著名的“四重人格”论断：

> "A data model that is to act as (1) a conceptual framework for defining a wide class of formatted databases and (2) a mediator between stored representations and user views should probably have at least four personalities; a tabular personality (e.g., the extensions of relations in the relational model), a set-theoretic personality (e.g., the relational algebra), an inferential string-formula personality (e.g., predicate logic in modern notation), and a graph-theoretic personality (e.g., labeled, directed hypergraphs for relations)." *(PDF p. 36)*

今天主流 RDBMS 里的 `IDENTITY` / 系统隐藏 `ROWID`、级联外键、表继承以及递归 `WITH RECURSIVE` 图查询，几乎都能在 1979 年这篇长达 38 页的蓝图里找到精确的理论原型。

---

## 查询语言的工程分野：QUEL、SEQUEL 与 SQL 正交性批判

Codd 给出了关系代数与关系演算两套数学标尺，但终端工程师不可能每天手写希腊字母 \(\pi\)、\(\sigma\) 和存在量词 \(\exists\)。1970 年代中期，加州大学伯克利分校的 INGRES 团队与 IBM 圣何塞实验室几乎同时交出了两份截然不同的工程答卷——**QUEL** 与 **SEQUEL**。十年后，当脱胎于 SEQUEL 的 SQL 横扫市场时，C. J. Date 写下了长达 47 页的系统性批判，精准预言了此后四十年优化器工程师每天都要填补的语义深坑。

### Held, Stonebraker & Wong (NCC 1975)：INGRES、QUEL 分层语义与查询修改 {#paper-43}

- **论文来源**：Gerald D. Held, Michael R. Stonebraker, Eugene Wong, *INGRES—A Relational Data Base System*, National Computer Conference (NCC), 1975, pp. 409–416 · [原始 PDF（共 8 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/essentials/ingres%3A-a-relational-data-base-system.pdf)（运行环境与架构见 PDF p. 1；`QUEL_n` 层次与 `SET`/`SET'` 定义见 PDF p. 2；分解与查询修改见 PDF p. 5–7）。

INGRES 是最早在小型机（DEC PDP-11/40）UNIX 操作系统上用 C 语言与 YACC 构建的全功能关系数据库原型。其查询语言 **QUEL** 直接脱胎于元组关系演算：用户显式声明元组变量（`RANGE OF E IS EMP`），再用 `RETRIEVE` 指定输出目标列与合取/析取过滤条件。

阅读 1975 原文最令人拍案叫绝的设计，是它对**集合与多重集边界的绝对诚实（PDF p. 2–3）**。QUEL 按函数与聚合嵌套深度将语言划分为递增的 `QUEL_0, QUEL_1, ..., QUEL_n` 层次（注意下标 \(n\) 度量的是嵌套聚合层数，不含聚合的多表连接本身就已经属于 `QUEL_0`）。当涉及集合与聚合时，QUEL 强制把构造算子拆成两套平行的原语：
- **去重集合族（`SET` 与 `COUNT`、`SUM`、`AVG`）**：先消除投影重复值再参与运算。例如输入元组在某列取值为 `[10, 10, 20]`，`SUM(SET(...))` 严格按集合 `{10, 20}` 算出 **`30`**。
- **保重多重集族（`SET'` 与 `COUNT'`、`SUM'`、`AVG'`）**：完整保留原始元组重数，`SUM'(SET'(...))` 算出 **`40`**。

不仅如此，QUEL 还把全局无分组 `aggregate` 与带 `BY X.g` 分组键的 `aggregate-function` 严格区分开来。在将高层 `QUEL_n` 降级消除为 `QUEL_{n-1}` 时（PDF p. 5），系统依次物化三张临时表完成空组兜底：

```text
/* 示例：统计每个部门（含尚无达标员工的空部门 Lab）的高薪人数 COUNT'(E.NAME BY D.DNAME WHERE ...) */
Step 1 -> TEMP0(DNAME, NAME):  先按内层 WHERE 资格条件过滤连接行，此时 'Sales' 有 2 行，'Lab' 0 行；
Step 2 -> TEMP1(DNAME, CNT):   对 TEMP0 中现存的非空分组求聚合，产出 { ('Sales', 2) }；
Step 3 -> TEMP2(DNAME, CNT):   扫描外层分组全集 D.DNAME，凡未出现在 TEMP1 中的键（'Lab'），
                               按聚合算子数学单位元补齐默认行 { ('Lab', 0) }，再与 TEMP1 求并！
```

其中空集默认单位元规定得毫不含糊：空集 `COUNT` 与 `SUM` 补 **`0`**，`MAX` / `MIN` 分别补域下界与上界，而空集 `AVG` 明确界定为无意义（Undefined）——这一步显式的 `TEMP2` 补空并集，使早期 INGRES 从根源上免疫了后来困扰无数 SQL 引擎的“关联聚合空组蒸发（COUNT Bug）”灾难！

在没有代价模型优化器的年代，INGRES 如何执行多变量连接查询 `QUEL_0`？Wong 与 Stonebraker 提出了著名的**查询分解（Decomposition）与元组替换（Tuple Substitution，PDF p. 5–6）**：先剥离单变量选择与投影条件缩减基表体积，随后每次挑出当前基数最小的元组变量，将其每一行具体数值逐一代入剩余查询模板，把 \(k\) 变量查询降维成 \(k-1\) 变量查询，递归直至单表扫描。作者在文中坦率记录了这条贪心启发式的局限：

> "Take the variable with the fewest tuples in its range and substitute in turn the actual values of its tuples. This reduces the number of variables by 1." *(PDF p. 5)*

> "Our criterion of choosing the variable with the fewest values is by no means optimal in general." *(PDF p. 6)*

在存储与安全边界上，INGRES 在底层提供堆表、哈希、保序地址计算及压缩变体共 5 种物理结构（PDF p. 6），在顶层则发明了统一的**查询修改（Query Modification，PDF p. 7）**架构——无论是视图展开、行列级权限控制，还是完整性约束，全部在语法树层通过追加 `WHERE` 谓词一次性改写完成。例如管理员声明完整性断言“员工薪水不得低于 8000”，当用户发起一笔批量降薪请求时，重写器在进入执行器之前直接改写语法树：

```text
/* 用户原始 QUEL 更新请求 */
RANGE OF E IS EMP
REPLACE E(SALARY = 0.9 * E.SALARY)
WHERE E.DEPT = "TOY"

/* 经 Query Modification 自动注入更新后镜像（Post-image）约束 */
RANGE OF E IS EMP
REPLACE E(SALARY = 0.9 * E.SALARY)
WHERE E.DEPT = "TOY"
  AND (0.9 * E.SALARY >= 8000)
```

细看改写后的 `AND` 条件便能发现其完整性语义与现代 SQL 事务回滚的关键区别：INGRES 把约束直接并入更新语句的资格条件，因此降薪后跌破 8000 的个别员工元组会被**静默跳过不予更新**（其余合规员工照常降薪），而非像现代 `CHECK` 约束那样中止整条语句报错回滚。

### Chamberlin & Boyce (SIGFIDET 1974)：SEQUEL 块映射的起源与早期语义 {#paper-44}

- **论文来源**：Donald D. Chamberlin, Raymond F. Boyce, *SEQUEL: A Structured English Query Language*, ACM SIGFIDET Workshop, 1974, pp. 249–264 · [原始 PDF（共 16 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/essentials/sequel%3A-a-structured-english-query-language.pdf)（设计动机见 PDF p. 1；百货商场示例模式 `EMP`/`SALES`/`SUPPLY`/`LOC`/`CLASS` 见 PDF p. 2 `Figure 1`）。

几乎在 INGRES 孕育 QUEL 的同时，IBM 的 Chamberlin 与 Boyce 在总结前代语言 SQUARE 的经验时发现：要求商业分析师像写一阶逻辑证明一样定义绑定元组变量（`RANGE OF X`）和全称/存在量词，心智门槛实在太高。能不能利用英语里天然的层层定语从句结构，设计一种完全不需要显式绑定变量、又能达到关系完备性的线性关键字语言？这就是 **SEQUEL** 的诞生初衷（PDF p. 1, 4–5）。

SEQUEL 的核心构件是 **`SELECT-FROM-WHERE` 映射块（Mapping Block）**：把已知列值代入 `WHERE` 匹配，返回对应的 `SELECT` 目标列集合；当需要跨表查找时，直接把内层映射块嵌进外层 `WHERE` 右侧（如 `WHERE DEPT IN SELECT DEPT FROM LOC WHERE FLOOR = 2`），只有遇到内外层同表自关联时才按需给外层块贴上块标签（如 `B1`，写成 `NAME = B1.MGR`，见 PDF p. 11）。

然而，如果拿着今天 ANSI SQL 的肌肉记忆去读 1974 年的 SEQUEL 原文，会发现四处令人大吃一惊的**历史语义反转**，而恰恰是这些早期设计埋下了后来 SQL 演化的伏笔：

1. **按上下文自动翻转的去重规则（PDF p. 8）**：现代 SQL 默认保留重复行（`SELECT ALL`），去重必须显式写 `DISTINCT`。而在 1974 年的 SEQUEL 中，**普通查询映射默认自动消除重复行；唯独当外层套上 `SUM`、`AVG` 等算术聚合函数时，内层映射才自动切换为保留重复行**！作者的本意是防止对员工薪水求平均时把相同薪水的两个人误当成一个人，却直接导致“子表达式的值取决于外层谁在调用它”：

> "if a mathematical function is applied to the results of a mapping, duplicates are preserved by the mapping; otherwise, duplicates are eliminated." *(PDF p. 8)*

2. **裸等号默认代表存在量词 `some`（PDF p. 7）**：在现代标量子查询中，`x = (SELECT ...)` 遇到多行返回会立即报错；而在 1974 SEQUEL 里，集合比较在不写修饰词 `ALL` 时，默认就是集合成员匹配（等价于 `IN` / `= ANY`）：

> "a set is used in a comparison in the absence of the word 'ALL', the default meaning is 'some'." *(PDF p. 7)*

3. **尚无 `HAVING` 子句，分组过滤直接混写在 `WHERE` 中（PDF p. 10）**：带有 `GROUP BY DNAME` 的查询直接在 `WHERE COUNT(NAME) > 10` 里过滤分组，行级过滤与组级过滤共用同一个子句，直到后续修订才拆出 `HAVING` 消除二义性。
4. **行内合取 `AND` 与集合交集 `∩` 的严格区分（PDF p. 8）**：在百货商场销售表 `SALES(DEPT, ITEM)` 或供货表 `SUPPLY(SUPPLIER, ITEM)` 中查找“同时供应 `'SHOE'` 和 `'DRESS'` 两类商品的供应商”时，初学者常误写成单行合取，而 1974 SEQUEL 原生提供了两个映射块的集合交集算子 `∩`（原文 `Q5`）：

```sql
-- 写法 A（行内 AND）：要求单条记录同时满足两个互斥值，或同组双属性匹配，原文 Q5 仅返回 {JOHN}
SELECT SUPPLIER FROM SUPPLY
WHERE ITEM = 'SHOE' AND ITEM = 'DRESS';

-- 写法 B（映射块集合交集 ∩）：分别求两类商品的供应商集合再求交，原文 Q5 正确返回 {JOHN, FRED}
SELECT SUPPLIER FROM SUPPLY WHERE ITEM = 'SHOE'
∩
SELECT SUPPLIER FROM SUPPLY WHERE ITEM = 'DRESS';
```

后来商业化初期的 SQL 砍掉了原生的 `INTERSECT`（直到 SQL-92 才重新引入），逼得整整一代程序员不得不绕道自连接或双层 `EXISTS` 来表达简单的集合交集。

### C. J. Date (1983)：SQL 语言批判——正交性缺失、重复行与三值逻辑等价崩塌 {#paper-40}

- **论文来源**：C. J. Date, *A Critique of the SQL Database Language*, Dec 1983 Manuscript (published in ACM SIGMOD Record 14(3), 1984, pp. 8–54) · [原始 PDF（共 47 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/essentials/a-critique-of-the-sql-database-language.pdf)（数据对象四元偏序菱形格见 PDF p. 5；`FROM` 派生表正交性缺失见 PDF p. 9；`NULL` 与重复行等价崩塌见 PDF p. 27–28, 37–39）。

到 1983 年，演化自 SEQUEL 的 SQL 已随 IBM SQL/DS 与 DB2 成为事实标准。Codd 的长期合作伙伴 C. J. Date 以当时 IBM 发布的 SQL 方言为靶本，从编程语言设计的**正交性（Orthogonality）**与**关系闭合性（Closure）**出发，对 SQL 进行了一次外科手术式的全面体检。Date 首先肯定了 SQL 相比导航式语言的巨大生产力飞跃及其关系代数完备性（PDF p. 2, 6），随即揭示了三大结构性内伤：

**第一重内伤：处处受限的非正交语法与物理概念越界**。关系模型最优雅的性质在于闭合性——任何关系运算的输出依然是一张关系，理应能无缝充当下一层运算的输入。然而在 1983 年的 SQL 中，表、行、列、标量四类对象构成了互不兼容的偏序格（PDF p. 5，其中单行多列元组与单列多行集合彼此不可比较），语法规则到处充满专断的特例：`FROM` 子句竟然只允许写基表或视图名，禁止直接嵌套派生子查询 `(SELECT ...)`（PDF p. 9，这一限制直到 SQL-92 才被补齐）；`SELECT` 默认保留重复行（`ALL`），而同处一个语言里的 `UNION` 却强制去重（`DISTINCT`）且根本不提供 `UNION ALL`（PDF p. 26）；比较谓词 `x <= (subquery)` 允许嵌套子查询，等价的语法糖 `x BETWEEN a AND (subquery)` 却直接报语法错误（PDF p. 11）。更严重的是，当时的 SQL 竟然没有在 `CREATE TABLE` 中提供逻辑主键或 `UNIQUE` 约束，反而强迫用户通过物理建索引语句 `CREATE UNIQUE INDEX` 来声明列唯一性，公然践踏了 Codd 确立的数据独立性底线：

> "Field uniqueness is a logical property of the data, not a physical property of an access path. It should be specified on CREATE TABLE, not on CREATE INDEX." *(PDF p. 36)*

**第二重内伤：多重集重复行与自然语言错觉**。SQL 为了省去投影去重排序的默认开销，让表退化为允许重复行的多重集，同时保留了极易误导人类的英语关键字。例如英语里说 *"younger than any member of department D1"* 通常指“比谁都年轻（小于最小值）”，但 SQL 的 `ANY` 实际上是存在量词 \(\exists\)，写成 `AGE < ANY (...)` 变成“只要比其中某一个人年轻即可”（PDF p. 15–16）。Date 在第 37–38 页列举了同一个“查询供应零件 `'P2'` 的供应商姓名”的 **7 种常见 SQL 写法**，取其中最具代表性的三种并排对照：

```sql
-- 写法 1（平铺连接）：若同一供应商分多批次供应 P2（或非主键连接），直接吐出重复姓名！
SELECT S.SNAME FROM S, SP WHERE S.S# = SP.S# AND SP.P# = 'P2';

-- 写法 2（IN / = ANY 子查询）：外层逐行扫描 S，即使 SP 中有多条匹配也只输出一次 S.SNAME
SELECT S.SNAME FROM S WHERE S.S# IN (SELECT SP.S# FROM SP WHERE SP.P# = 'P2');

-- 写法 3（EXISTS 相关子查询）：对重复行免疫，但改为 NOT IN vs NOT EXISTS 时一旦混入 NULL 立刻分裂！
SELECT S.SNAME FROM S WHERE EXISTS (SELECT * FROM SP WHERE SP.S# = S.S# AND SP.P# = 'P2');
```

在严格无重复、无空值的纯关系集合上，这 7 种写法完全等价；一旦表里混入重复行或 `NULL`，它们算出的结果基数与真值立刻分崩离析！

**第三重内伤：`NULL` 三值逻辑摧毁优化器等价重写**。除了本文开头演示的 `SUM(x+y) != SUM(x)+SUM(y)`、`AVG(x) != SUM(x)/COUNT(*)`（PDF p. 27–28）以及 `> ALL` 转 `NOT EXISTS` 失效（PDF p. 39）之外，Date 指出 SQL 内部对 `NULL` 的处理本身就是精神分裂的：在 `WHERE` 比较中 `NULL = NULL` 为 `UNKNOWN`（视为不等），到了 `GROUP BY` 分组与 `ORDER BY`、`UNIQUE` 判重时，两个 `NULL` 却又突然被当成相等归入同一组！这意味着优化器每写一条看似天经地义的逻辑变换规则，都必须挂满繁琐的 `NOT NULL` 与 `DISTINCT` 前置守卫条件。Date 在文末写下的警告，在此后四十年的 SQL 标准膨胀与优化器 Bug 史中字字应验：

> "The mistakes we make now will come back to haunt us in the future. Indeed, the language in its present form is already proving difficult to extend in some (desirable) ways because of limitations in its current structure." *(PDF p. 43)*

---

## 从可扩展原型到工业级内核：Postgres 复盘与现代 DBMS 架构

查询语言定型之后，压力全数传导到了数据库内核。一方面，CAD 几何图形、地理信息、时序与文本检索纷纷涌现，只会存整数和定长字符串的传统引擎捉襟见肘；另一方面，成千上万并发连接同时读写缓冲池与 B+Tree 页面，任何一处锁粒度失当或刷盘顺序颠倒都会造成死锁或持久性损坏。Postgres 项目的十年探索与 Hellerstein 等人的现代 DBMS 架构长文，为我们勾勒出了工业级数据库内核的终极形态。

![现代关系数据库内核全局架构与状态隔离边界](/images/diagrams/paper-rel-dbms-architecture.svg)

### Hellerstein / Stonebraker & Kemnitz (1991/2019)：回望 Postgres——类型可扩展性的胜利与无覆盖存储的教训 {#paper-53}

- **论文来源**：Joseph M. Hellerstein, *Looking Back at Postgres*, arXiv:1901.01973v1, 2019（为 Stonebraker 图灵奖文集撰写的历史复盘，系统回顾了 Stonebraker 团队 1986–1994 年伯克利 Postgres 原型及 Stonebraker & Kemnitz 1991 里程碑论文） · [原始 PDF（共 9 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/rdbms/looking-back-at-postgres.pdf)（1986 与 1991 特性演进对照表见 PDF p. 2 `Figure 1`）。

1980 年代中期，面向对象数据库（OODB）阵营猛烈抨击关系数据库无法高效表达复杂嵌套对象，主张退回指针导航式的持久化对象图。INGRES 创始人 Stonebraker 在伯克利启动继任项目 **Postgres（Post-INGRES）** 时，提出了一条“鱼与熊掌兼得”的对象—关系（Object-Relational）路线：**最外层依然坚守声明式关系表，但把列类型、算子、聚合函数与索引访问方法彻底做成由系统目录表（System Catalog）驱动的可插拔扩展接口**（PDF p. 2）：

> "A key aspect of Postgres was to \"have your cake and eat it too\" from a data modeling perspective: Postgres retained tables as its \"outermost\" data type, but allowed columns to have \"complex\" types including nested tuples or tables." *(PDF p. 2)*

今天成为各大现代引擎标配的抽象数据类型（ADT）、用户自定义函数与聚合（UDF / UDA）正是从这里走向成熟。但把黑盒 C 函数塞进声明式 SQL 立刻引爆了两道硬核内核难题（PDF p. 3）：
1. **可扩展索引不能只管搜索，必须打通并发与恢复**：早期 Postgres 为空间数据引入 R-tree 自定义访问方法接口时，起初只抽象了键值比较与分裂逻辑，真正在高并发环境跑起来才发现，自定义索引必须深度耦合页闩锁（Latching）协议与崩溃恢复日志，这一痛点直接催生了后来著名的通用搜索树框架 **GiST（Generalized Search Tree）**。
2. **昂贵 UDF 谓词颠覆“无脑谓词下推”铁律**：传统优化器假设单行过滤谓词几乎零成本，总是尽早评估选择率最低的谓词。但在 Sequoia 2000 遥感图像项目中，一个裁剪卫星影像的 UDF 谓词 \(A\) 单次调用耗时 \(c_A = 100\)、选择率 \(s_A = 0.1\)，而另一个普通整数过滤谓词 \(B\) 耗时 \(c_B = 1\)、选择率 \(s_B = 0.5\)。若按传统“选择率优先”先算 \(A\) 再算 \(B\)，每行期望代价为 \(100 + 0.1 \times 1 = \mathbf{100.1}\)；若综合单次开销与过滤收益按秩公式 \(\text{rank} = (1 - s) / c\) 排序，先算廉价的 \(B\) 筛掉一半数据再调昂贵的 \(A\)，每行期望代价骤降至 \(1 + 0.5 \times 100 = \mathbf{51}\)！Hellerstein 据此实现了著名的 **Predicate Migration（谓词迁移）** 算法（后来开源 PostgreSQL 一度因 UDF 统计维护复杂将其关闭，近年又重新支持手写函数 `COST` 标注）。

这篇回顾最珍贵的工程价值，在于它毫不避讳地解剖了 Postgres 当年雄心勃勃却最终**在工业演化中被淘汰的失败设计（PDF p. 4）**：
- **无覆盖存储引擎（No-Overwrite Storage）与时间旅行（Time-Travel）的退场**：Stonebraker 当年厌恶传统预写日志（WAL）冗长的崩溃恢复代码，提出“日志即数据（The log is the data）”——每次更新绝不原地覆盖旧元组，而是直接追加新版本并维护时间戳链，顺带白送任意历史时刻的 `AS OF` 时间旅行查询，崩溃重启瞬间即可对外服务（只需查一张两比特的事务状态表）。然而在真实负载下，这套存储暴露出三大致命软肋：常规读取被迫跨越大量历史死版本做可见性过滤，磁盘随机 I/O 放大严重；后台把冷历史版本迁移到光盘塔（Vacuum）成为沉重的同步负担；没有独立的重做字节流，根本做不了异地日志传送主从复制（Log-Shipping Replication）！最终在开源演进为 PostgreSQL 后，社区彻底砍掉了时间旅行特性，老老实实换回了经典的 **WAL 预写日志**，仅将多版本元组链保留下来用作高并发 **MVCC（多版本并发控制）**：

> "In the end, the Postgres storage system never excelled on performance; versioning and time-travel were removed from PostgreSQL over time and replaced by write-ahead logging." *(PDF p. 4)*

  用一张对照表即可看清为什么“无覆盖时间旅行”在工程演化中败给了“WAL + MVCC 垃圾回收”组合：

| 架构维度 | 伯克利 Postgres 无覆盖存储（1987–1994） | 现代 PostgreSQL（WAL + 堆内 MVCC） |
| :--- | :--- | :--- |
| **崩溃恢复延迟** | 瞬时恢复（仅需校验 2-bit 事务提交状态表） | 需从最近 Checkpoint 起扫描重放 WAL 记录 |
| **常规 OLTP 读路径** | 需跨越漫长的历史版本链并频繁查事务时间表 | 结合提示位（Hint Bits）与定期 Vacuum 清理死元组 |
| **历史冷数据归档** | 后台吸尘器（Vacuum）将旧版本异步迁往索尼光盘塔 | 放弃内置时间旅行，仅保留活跃快照可见窗口内的版本 |
| **高可用物理复制** | **天生缺失线性重做字节流**，难以构建流式备库 | 直接通过网络流式发送 WAL 日志实现毫秒级主从同步 |

- **元组级规则系统（Tuple-level Rules）的泥潭与其它遗产**：Postgres 曾试图用一套统一规则系统包办视图、触发器、完整性与缓存失效，并提供“执行期逐元组触发”与“编译期查询重写”两种底层实现。结果发现两套路径在语句中间状态可见性与递归终止条件上极难调和，最终分化为今天职责清晰的 `VIEW` 重写与标准 SQL `TRIGGER`。此外，论文还复盘了绕过 SQL 解析直调内核 C 函数的 `Fast Path` 接口（PDF p. 6），以及多查询/并行两阶段优化器 XPRS（PDF p. 5）为现代分析引擎留下的探路经验。

### Hellerstein, Stonebraker & Hamilton (FnTDB 2007)：现代数据库内核全局架构解剖 {#paper-29}

- **论文来源**：Joseph M. Hellerstein, Michael Stonebraker, James Hamilton, *Architecture of a Database System*, Foundations and Trends in Databases 1(2), 2007, pp. 141–259 · [原始 PDF（共 119 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/architecture/architecture-of-a-database-system.-foundations-and-trends-in-databases.pdf)（五大核心子系统总图见 PDF p. 4 `Fig. 1.1`；每工作者单进程模型见 PDF p. 13 `Fig. 2.1`；万圣节问题更新计划对比见 PDF p. 52 `Fig. 4.3`）。

如果只能挑一篇文献给新入职的数据库内核研发建立全局坐标系，这篇 119 页的经典综述当仁不让。三位作者没有堆砌学术算法细节，而是紧扣**一条 SQL 请求在真实商用引擎（DB2、Oracle、SQL Server、PostgreSQL、MySQL）内部的完整生命周期**，拆解了进程调度、查询编译执行、缓冲池管理与事务日志恢复四大疆域的状态所有权边界。

#### 1. 进程模型与双层准入控制（Process Models & Admission Control）

当成千上万个客户端并发打入数据库时，内核首先要决定用什么执行载体承载每个活跃会话（PDF p. 10–21）：
- **每工作者一 OS 进程（Process per DBMS Worker，如 PostgreSQL、Oracle 默认模式，`Fig. 2.1`）**：借用操作系统现成的地址空间隔离与抢占式调度，单个会话崩溃不易冲毁全局，但必须显式申请大块跨进程共享内存（Shared Memory）来存放缓冲池、锁表与日志缓冲区，且高并发下进程上下文切换与每连接内存开销高昂。
- **每工作者一 OS 线程 / 线程池（Thread per Worker，如 IBM DB2、SQL Server 绝大多数部署）**：所有线程天然共享同一虚拟地址空间，指针传递零转换，但任何一处野指针越界都会直接拖垮整个实例，且对跨平台线程库的可伸缩性要求极高。
- **DBMS 自管轻量级协程 / 纤程（Lightweight Threads，如 Sybase、SQL Server 可选纤程模式）**：在少量内核线程上由引擎自主调度数万个用户态纤程，彻底省去内核态切换；前提是底层所有磁盘与网络读写必须全面改造成**非阻塞异步 I/O**，否则一次同步缺页阻塞就会冻住整条内核线程上的所有纤程。

更关键的是防止内存雪崩的**双层准入控制（PDF p. 22–23）**：第一道关卡在网络层限制并发连接数；但真正保命的是位于**查询解析与优化之后的第二道执行准入控制器（Execution Admission Controller）**——只有在优化器选出物理计划后，内核才知道这条查询究竟是个只读几页的点查，还是个要吃掉数 GB 排序与哈希工作区（Work Memory）的分析巨兽，从而决定立即放行、缩减内存配额降级运行，还是排队推迟以避免缓冲池颠簸（Thrashing）：

> "This execution admission controller runs after the query is parsed and optimized, and determines whether a query is postponed, begins execution with fewer resources, or begins execution without additional constraints." *(PDF p. 23)*

#### 2. 查询处理器：延迟鉴权、迭代器零拷贝引用与万圣节问题

SQL 进入关系查询处理器后（PDF p. 36–55），依次穿过解析器、语义重写器、优化器与执行器：
- **延迟到执行期的权限校验（PDF p. 38）**：为了最大化共享查询计划缓存（Plan Cache），工业级引擎在编译优化阶段只验证表列存在性，故意把用户级的权限检查推迟到执行器启动瞬间——否则两个权限略有不同的业务账号执行一模一样的参数化模板 SQL，都无法复用同一份物理执行计划。
- **基于完整性约束的语义重写（PDF p. 39–41）**：除了常规视图展开与常量折叠，重写器还能利用外键做**冗余连接消除（Join Elimination）**。例如 `EMP` 的 `dept_id` 引用 `DEPT(id)` 主键且声明为 `NOT NULL`，当查询 `SELECT E.name FROM EMP E JOIN DEPT D ON E.dept_id = D.id` 完全不读取 `DEPT` 的其它列时，重写器无需访问磁盘就能静态证明每行员工必有且仅有一行匹配部门，直接把整张 `DEPT` 表和连接算子从计划树里凭空抹去！
- **迭代器数据流、`BP-tuple` 与万圣节问题（Halloween Problem，PDF p. 48–54, 101–102）**：物理计划按经典的 `open / get_next / close` 火山迭代器（加上封装跨线程/跨节点网络洗牌的 `Exchange` 算子）驱动数据上流。为了避免频繁调用 `malloc/free` 造成内存碎片与泄漏，执行器统一在**内存上下文（Memory Context / Arena）**中批量分配内存，查询结束或操作符重置时一键整块回收。在算子间传递元组时，内核精细权衡两种形态：**`BP-tuple`** 直接持有一个指向缓冲池已固定（Pinned）槽位的页内指针，实现极致零拷贝，但长驻留会加剧缓冲池页饥饿；**`M-tuple`** 则把所需列复制到算子私有堆内存中并立刻 `unpin` 释放缓冲页（排序与哈希构建算子必选）。
  最经典的执行陷阱莫过于 `Fig. 4.3`（PDF p. 52）展示的**万圣节问题（Halloween Problem）**。设员工表 `EMP` 在 `salary` 列上建有升序 B+Tree 索引，现执行一条看似人畜无害的普调语句：

```sql
UPDATE EMP
SET salary = salary * 1.1
WHERE salary < 1000;
```

  若底层计划直接用 `IndexScan(EMP.salary < 1000)` 边扫描边将可搜索谓词（SARG, Search Argument，PDF p. 54）下推到索引页原地更新键项：一名初始薪水为 `800` 的员工被首次扫出后涨薪至 `880`，更新算子随即把新键项 `(880, RID)` 插回当前 B+Tree 游标的**前方右侧**；几微秒后，向右推进的索引游标再次撞上刚才自己亲手写进去的 `880`，又把它涨到 `968`，接着再涨到 `1064.8` 越过 `1000` 才罢休！现代优化器通过静态检测扫描访问路径与更新目标索引键的交集，要么强制切换为堆表顺序扫描，要么在 `IndexScan` 与 `Update` 算子之间插入一个阻塞物化屏障（先收集齐全部待改记录的 `RID` 临时表或排序流，再统一启动写入），从物理流水线上彻底斩断“边读边追尾自写数据”的正反馈环。

- **存储、三级并发原语（`Pin` / `Latch` / `Lock`）与 WAL 刷盘不等式**：
  在最底层的缓冲池与日志管理器之间，`STEAL` + `NOT-FORCE` 策略之所以绝不会写坏数据页，靠的是两条镌刻在缓冲槽位与提交路径上的日志序列号（LSN）硬不等式：

$$\text{Flush Dirty Page } P \implies \text{pageLSN}(P) \le \text{flushedLSN}$$

$$\text{Ack Client Commit } T \implies \text{commitLSN}(T) \le \text{flushedLSN}$$

#### 3. 存储、三级并发原语（`Pin` / `Latch` / `Lock`）与 ARIES 生理日志

深入到最底层的事务存储管理器（PDF p. 71–98），为什么主流商业数据库一律抛弃操作系统文件系统页缓存，坚持用 Direct I/O 或裸设备自管缓冲池（原文引用经验测算显示裸设备比经 OS 缓存的文件系统在 TPC-C 上仍有约 `6%` 吞吐优势，PDF p. 72）？核心原因在于操作系统根本不知道页与日志之间的刷盘因果律！

为了在同一个 8KB 缓冲页上同时跑满多线程吞吐并守住 ACID 隔离，内核严格划定了三层职责绝不混用的并发原语（PDF p. 84, 94–96）：
1. **`Pin`（页驻留引用计数）**：告诉缓冲池淘汰算法（如 Clock / LRU-2）“当前有算子正在读或改这块内存槽位，绝不允许把这页换出覆盖”，本身不提供读写互斥。
2. **`Latch`（物理内存闩锁）**：保护缓冲页内部偏移数组、B+Tree 分裂指针或全局哈希表桶的**短临界区读写互斥**，仅持有几十到几百个 CPU 周期；由内核代码完全掌控获取顺序（如 B+Tree Latch Crabbing 耦合攀爬），**绝对禁止发生死锁**，也无需做昂贵的等待图死锁检测。
3. **`Lock`（逻辑事务锁）**：登记在全局动态哈希锁表中，保护行、键区间或表对并发事务的逻辑可见性，一直持有到事务提交或回滚（**严格两阶段封锁 Strict 2PL**，防止物理页级回滚误伤已读脏数据的并发事务）。为了阻断范围查询下的“幻读（Phantom）”，B+Tree 访问方法采用 **Next-Key Locking（下一键锁）**：例如索引中现存键值 `{1, 5, 100}`，事务查询 `WHERE x BETWEEN 3 AND 7` 时，不仅锁住命中的 `5`，还必须把紧邻其后的间隙上界 **`100`** 一并锁死——哪怕另一个事务只想插入毫不相干的 `50`，也会因撞上 `(5, 100]` 区间锁而被阻塞，这就是物理键区间锁换取串行化隔离的真实代价。

在持久性与崩溃恢复防线上（PDF p. 88–93），为了兼顾大事务内存容量与极速提交，几乎所有现代引擎都选择了 **`STEAL`（允许在事务未提交前把脏页提前刷盘腾出内存）+ `NOT-FORCE`（事务提交时不必强制把所有随机脏页刷入磁盘）** 策略，并靠三条铁律驱动 **WAL（预写日志）** 与 **ARIES 生理日志（Physiological Logging）**：
- 脏页落盘前，其页面 `pageLSN` 对应的日志记录必须先一步落盘（支撑 `STEAL` 回滚安全）；
- 所有日志记录必须严格按序列号（LSN）保序刷盘；
- 向客户端返回 `COMMIT` 成功响应前，提交日志记录必须已持久化落盘（支撑 `NOT-FORCE` 重做安全，配合 Group Commit 摊薄单次刷盘延迟）。

为什么 ARIES 要区分“物理 REDO”与“逻辑 UNDO”？设想事务 \(T_1\) 向 B+Tree 插入一行触发了叶子页分裂，紧接着事务 \(T_2\) 向分裂出来的新叶子页插入了另一行并成功提交，随后系统断电且 \(T_1\) 需要回滚。如果对 \(T_1\) 做傻瓜式的“物理字节镜像回滚”，直接把分裂前的老页面盖回去，\(T_2\) 已经提交的数据就会人间蒸发！ARIES 的解法是：**先按物理页面面向前“重演历史（Repeating History）”精确恢复到崩溃瞬间的物理页状态（用称为 Nested Top Actions 的嵌套顶层动作把已完成的 B+Tree 页分裂永久固化，不再物理撤销），再对未提交事务执行逻辑逆操作（如从当前 B+Tree 中按逻辑键删除 \(T_1\) 插入的那条记录）**：

> "In ARIES, physical logging is generally used to support REDO, and logical logging is used to support UNDO. This is part of the ARIES rule of \"repeating history\" during recovery to reach the crash state, and then rolling back transactions from that point." *(PDF p. 90)*

---

## 横向对比：从关系逻辑公理到内核物理契约

把这七篇跨越近半个世纪的文献放到同一张工程蓝图上审视，可以清晰看到数据库领域是如何在**上层逻辑表达力**与**底层物理可控性**之间一步步划清边界的。

### 表 1：关系模型与早期查询语言的语义契约演进

| 文献与里程碑 | 数据结构与身份抽象 | 重复行（集合 vs 多重集） | 缺失值与空集聚合处理 | 等价重写安全性与历史遗产 |
| :--- | :--- | :--- | :--- | :--- |
| **[#41](#paper-41) Codd (1970)** | 角色命名原子关系集合；候选键标识；自顶向下消除嵌套表 | **严格集合**（关系定义无重复，投影强制去重） | 未展开三值逻辑；揭示关系复合的**连接陷阱** | 集合代数恒等式完全成立；奠定逻辑与物理路径解耦基石 |
| **[#42](#paper-42) Codd RM/T (1979)** | 系统不可变代理键（`E-domain`）+ `CG`/`AG`/`UGI` 目录图 | 严格集合 + 异构补空并集 `OUTER UNION` | 引入三值逻辑真值表与 `TRUE`/`MAYBE DIVISION` | 用关系自身自举表达实体身份、继承图与传递闭包元运算 |
| **[#43](#paper-43) INGRES QUEL (1975)** | 绑定元组变量演算；按聚合嵌套深度分层 `QUEL_n` | **显式双轨隔离**：`SET`/`SUM` 去重，`SET'`/`SUM'` 保重 | `TEMP2` 为空分组显式补数学默认值（`0` 等），免疫空组蒸发 | 元组替换贪心分解；**查询修改**在语法树层统一视图、权限与约束 |
| **[#44](#paper-44) SEQUEL (1974)** | 无变量嵌套 `SELECT-FROM-WHERE` 英语块映射与块标签 | **上下文隐式翻转**：普通映射去重，套算术聚合时保重 | 裸 `=` 默认表示存在量词 `some`；组过滤混写于 `WHERE` | 极低的用户心智门槛奠定商业胜局，隐式规则埋下语义包袱 |
| **[#40](#paper-40) Date SQL 批判 (1983)** | 非正交四元对象格（`FROM` 禁子查询，唯一性绑死物理索引） | `SELECT` 默认保重 Bag，`UNION` 强制去重且缺 `ALL` | `NULL` 三值逻辑在 `WHERE` 判未知、在 `GROUP BY` 判相等 | 算术分配律、德摩根 `ALL -> NOT EXISTS` 等改写需严苛非空守卫 |

### 表 2：现代关系数据库内核的关键状态隔离机制

| 内核层次与机制对比 | 核心职责与保护对象 | 生命周期与开销尺度 | 一旦混用或失守引发的典型故障 |
| :--- | :--- | :--- | :--- |
| **执行准入控制 vs 连接网关**（`#29`） | 基于优化器估算的排序/哈希工作内存门控活跃查询 | 单条查询执行期 | 大并发重型分析耗尽内存，引发缓冲池与临时盘剧烈颠簸 |
| **`BP-tuple` vs `M-tuple`**（`#29`） | 页内零拷贝槽位指针 vs 算子私有内存堆拷贝 | 单次 `get_next` 跨越算子边界 vs 阻塞物化期 | 滥用 `BP-tuple` 导致缓冲页长期被 `Pin` 死无法淘汰；滥用拷贝浪费 CPU 带宽 |
| **RID 物化屏障**（`#29`） | 先收集全部待改行标识再驱动索引写入 | 单条 `UPDATE` 语句内部 | 索引扫描游标追上自身刚写入的新键值，触发无限更新的**万圣节问题** |
| **`Pin` vs `Latch` vs `Lock`**（`#29`） | 内存页防换出计数 vs 物理内存结构短互斥 vs 事务逻辑可见性 | 算子读页期 / 数十 CPU 周期（禁死锁）/ 直至事务结束（Strict 2PL） | 用重型事务锁保护页分裂拖垮多核吞吐；缺乏 Next-Key 锁放过范围**幻读** |
| **ARIES WAL vs Postgres 无覆盖存储**（`#53`, `#29`） | `STEAL`/`NOT-FORCE` 原地更新 + 生理重做/逻辑回滚 vs 追加历史链 | 顺序追加日志毫秒刷盘 vs 每次读取遍历历史版本与后台重型 GC | 无覆盖存储拖慢常规点查与范围扫，且缺少独立日志流阻断主从复制 |

从 Codd 1970 年那篇仅有 11 页的公理宣言，到今天动辄百万行 C++/Rust 代码的工业级数据库内核，整条演化主线其实一直在回答同一个问题：**如何让上层业务像操纵纯净数学集合一样简单地声明意图，同时由底层内核在不牺牲一丝并发吞吐与崩溃安全的前提下，替用户扛下重复行、空值、内存受限、多核争用与断电恢复的全部物理泥潭**。在下一篇中，我们将拆开整个关系查询处理器里算法密度最高的黑盒——**查询优化器（Query Optimizer）**，看 Volcano 与 Cascades 框架如何在指数爆炸的等价计划森林中剪枝突围。

---

## 参考文献与阅读基线

本文精读的所有论文均对齐开源论文集 [`lonng/db-papers`](https://github.com/lonng/db-papers)（Git 基线提交 `4ff1331`），文中引用的页码均为对应文件内的 PDF 物理页码：

1. **#41**：E. F. Codd. *A Relational Model of Data for Large Shared Data Banks*. Communications of the ACM 13(6), June 1970, pp. 377–387. [原始 PDF（共 11 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/essentials/a-relational-model-of-data-for-large-shared-data-banks.pdf)
2. **#42**：E. F. Codd. *Extending the Database Relational Model to Capture More Meaning*. ACM Transactions on Database Systems 4(4), Dec 1979, pp. 397–434. [原始 PDF（共 38 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/essentials/extending-the-database-relational-model-to-capture-more-meaning.pdf)
3. **#43**：Gerald D. Held, Michael R. Stonebraker, Eugene Wong. *INGRES—A Relational Data Base System*. National Computer Conference (NCC), 1975, pp. 409–416. [原始 PDF（共 8 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/essentials/ingres%3A-a-relational-data-base-system.pdf)
4. **#44**：Donald D. Chamberlin, Raymond F. Boyce. *SEQUEL: A Structured English Query Language*. ACM SIGFIDET Workshop on Data Description, Access and Control, 1974, pp. 249–264. [原始 PDF（共 16 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/essentials/sequel%3A-a-structured-english-query-language.pdf)
5. **#40**：C. J. Date. *A Critique of the SQL Database Language*. Dec 1983 Manuscript (ACM SIGMOD Record 14(3), 1984, pp. 8–54). [原始 PDF（共 47 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/essentials/a-critique-of-the-sql-database-language.pdf)
6. **#53**：Joseph M. Hellerstein. *Looking Back at Postgres* (reviewing Berkeley Postgres 1986–1994 & Stonebraker & Kemnitz 1991). arXiv:1901.01973v1, 2019. [原始 PDF（共 9 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/rdbms/looking-back-at-postgres.pdf)
7. **#29**：Joseph M. Hellerstein, Michael Stonebraker, James Hamilton. *Architecture of a Database System*. Foundations and Trends in Databases 1(2), 2007, pp. 141–259. [原始 PDF（共 119 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/architecture/architecture-of-a-database-system.-foundations-and-trends-in-databases.pdf)
