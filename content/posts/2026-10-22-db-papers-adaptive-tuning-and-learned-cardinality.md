---
title: "【Paper】从执行反馈到深度基数估计：DB2 LEO/ASC、Oracle 自动调优、AQP 与 Naru/NeuroCard 演进"
slug: "db-papers-adaptive-tuning-and-learned-cardinality"
date: 2026-10-22T12:40:00+08:00
lastmod: 2026-11-12T00:00:00+08:00
draft: false
toc: true
math: true
categories: ["数据库"]
tags: ["论文精读", "自适应查询处理", "基数估计", "自动调优"]
series: ["数据库经典论文精读"]
---

> 本系列第 5/10 篇 · [数据库经典论文精读总索引]({{< relref "2026-10-22-database-papers-source-reading.md" >}}) · [上一篇：数据摘要与概率计数]({{< relref "2026-10-22-db-papers-histograms-synopses-and-sketches.md" >}}) · [下一篇：查询执行范式演进]({{< relref "2026-10-22-db-papers-query-execution-vectorization-and-jit.md" >}})

上一篇我们看到，等深直方图、小波摘要与概率计数器（Sketch）能够用几 KB 内存把**单列**分布刻画得相当精准。然而在真实生产库里，哪怕每一张表、每一列的单列直方图都刚跑完 `ANALYZE` 且零误差，一条只涉及 4 到 5 张表连接（Join）的分析查询，依然经常在基数估计（Cardinality Estimation）上偏离真实行数 **\(10^3\) 到 \(10^4\) 倍**。

这不是直方图桶数不够多造成的工程瑕疵，而是传统基于代价的优化器（Cost-Based Optimizer, CBO）为了把目录开销压在常数级，在概率建模底层硬塞进去了两条极易失真的简化假设。

---

## 一、为什么单表直方图在三到五次连接后误差会飙升一万倍？

不妨拿交通事故登记库（DMV / `ACCIDENTS`）里一个最朴素的四表关联查询在白板上算一笔账：我们要统计“在多伦多市驾驶本田雅阁（Honda Accord）且车主年龄低于 25 岁的夜间事故记录”，涉及车辆表 `Car`、车主表 `Owner`、地区表 `Demographics` 与事故表 `Accident`。

即便每列单列统计完全精确，传统优化器在拼装联合选择率时也会连续踩中两颗地雷：

1. **属性值独立假设（Attribute Value Independence, `AVI`）**：在 `Car` 表内，`MAKE = 'Honda'` 占 \(10\%\)，`MODEL = 'Accord'` 占 \(2\%\)。优化器按独立事件直接相乘得到 \(0.10 \times 0.02 = 0.002\)；而现实世界里只要车型是 `Accord`，品牌几乎 \(100\%\) 是 `Honda`，真实联合选择率就是 \(0.02\)——**单表两列过滤就已经低估了 \(10\) 倍**。在 `Demographics` 表上，`COUNTRY = 'Canada' AND CITY = 'Toronto'` 同样因为函数依赖再低估 \(10\) 倍。
2. **连接均匀性与包含假设（Join Uniformity & Containment）**：跨越外键连接 `Car ⋈ Owner ⋈ Accident` 时，优化器默认两表连接键分布彼此独立、均匀匹配：
   $$\widehat{|R \bowtie S|} \approx \frac{|R| \cdot |S|}{\max\big(\text{NDV}(R.k),\; \text{NDV}(S.k)\big)}$$
   但在真实数据里，“年轻车主 + 特定车型 + 夜间驾驶”在事故表中的连接扇出（Fanout）远高于全表均值，高频连接键与过滤谓词之间存在强烈的跨表共现偏斜。每跨过一次连接，乘法误差就再放大 \(5 \sim 10\) 倍。

仅仅经过两次表内函数依赖与三次外键连接，逐层累乘的失真账单就会在搜索树顶端放大成灾难：

| 算子阶段与谓词/连接步骤 | 传统 CBO 独立/均匀假设估计 | 真实数据共现基数 | 累计低估倍数 | 诱发的错误物理算子决策 |
| :--- | :---: | :---: | :---: | :--- |
| **1. `Car` 表过滤**：`MAKE='Honda' AND MODEL='Accord'` | \(100\text{K} \times 0.10 \times 0.02 = 200\) | \(2{,}000\) | \(10\times\) | 误判为极小驱动集，优先走二级索引随机回表 |
| **2. `Demographics` 过滤**：`COUNTRY='Canada' AND CITY='Toronto'` | \(50\text{K} \times 0.10 \times 0.01 = 50\) | \(500\) | \(10\times\)（各自低估） | 误估维度过滤结果可全驻留寄存器/L1 缓存 |
| **3. 连接 `Car ⋈ Owner`**（年轻车主偏好特定车型） | \(\approx 25\) | \(2{,}000\) | \(80\times\) | 放弃 `Hash Join`，继续沿用 `Index NLJ` 逐行探测 |
| **4. 再连接 `⋈ Demographics`**（城市人口与车型分布偏斜） | \(\approx 3\) | \(2{,}400\) | \(800\times\) | 把实际膨胀的一侧错误选作内存哈希 Build 端 |
| **5. 最终连接 `⋈ Accident`**（高风险时段高频出险扇出） | **\(\approx 52\) 行** | **\(500{,}000\) 行** | **\(\approx 9{,}600\times\)** | **50 万次随机索引回表 + 哈希溢出落盘风暴** |

对搜索算法而言，把 50 万行中间结果误判成 52 行是致命的：优化器会自信地选中随机回表的 `Index Nested-Loop Join`，或者把真正有 50 万行的那一侧塞进内存做 `Hash Join` 的 Build 表，让本该 1 秒扫完的顺序流退化成持续数十分钟的随机 I/O 与落盘风暴。

面对多维联合分布指数级膨胀的组合墙，数据库工业界与学术界在过去二十年里演化出了三条截然不同的反击路线：

- **路线一：查询间执行反馈与自治调优（Inter-Query Feedback & Autonomic Tuning）**——承认静态单列公式会算错，利用已执行查询的真实算子行数或后台分钟级采样试跑，生成选择率补偿因子（DB2 `LEO`、Oracle `SQL Profile`），或自动识别相关列并调度后台收集列组统计（DB2 `ASC`）与全局瓶颈诊断（Oracle `ADDM`）。
- **路线二：查询内中途自适应执行（Intra-Query Adaptive Query Processing, `AQP`）**——不等当前慢查询跑崩再拯救下一条，而是在同一条查询执行中途监测真实基数，一旦越过计划稳健区间就当场重编剩余子图、迁移算子状态，甚至用 `Eddies` 动态路由每一行元组。
- **路线三：深度自回归联合分布学习（Deep Autoregressive Cardinality Estimation）**——直接推翻 `AVI` 与连接均匀性假设，用深度自回归网络（`Naru`）和无环全外连接均匀采样 + 扇出除权（`NeuroCard`）显式拟合全表乃至跨表的精确联合概率分布。

![反馈修正、自治统计维护与中途 AQP 闭环对比](/images/diagrams/paper-ce-feedback-vs-aqp-loop.svg)

在深入七篇经典论文之前，我们先用一张全局对照表锚定它们各自的介入时机、核心武器与工程代价：

| 论文与系统 | 所属路线 | 介入时机 | 核心机制 | 运行时开销 | 主要工程边界 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **#50 DB2 LEO** (VLDB'01) | 查询间反馈 | 执行后异步回填 | 保存 `QEP skeleton`，按算子实测行数写二层调整目录 | 计数器监控 \(< 5\%\) | 索引范围扫描与早停连接会截断观测；局部补丁易引发最大熵一致性冲突 |
| **#08 DB2 ASC** (VLDB'04) | 自治统计维护 | 变更触发 + 反馈驱动 | `AA` 页采样算 CDF 面积差 + `QFA` 识别相关列并限流跑 `RUNSTATS` | 监控 \(1\%\sim 2\%\)，前台减速 \(\le 7\%\) | 小比例剧烈篡改（\(<10\%\) 行）会绕过 `DAC`，须靠首次慢查询触发反馈 |
| **#38 Oracle ADDM** (CIDR'05) | 全局根因诊断 | 快照窗口周期诊断 | 以 `DB Time` 统一 CPU/等待度量，在 DAG 上按父子包含律剪枝 | 全套自治特性约 \(3\%\) | 聚焦实例级瓶颈定位，复杂 SQL 本身的基数纠偏需转交 `ATO` |
| **#39 Oracle SQL Tuning** (VLDB'04) | 离线/后台深度调优 | 分钟级调优窗口 | 动态采样 + 片段试跑验证中间基数，下发免改 SQL 的 `SQL Profile` | 后台调优，零侵入业务文本 | 语义不等价改写仍需人工确认；连接爆炸极端子图试跑受时间预算约束 |
| **#07 AQP Looking Glass** (CIDR'05) | 查询内中途自适应 | 单条查询执行中途 | 拆解 Plan-based 重优化与 `Eddies` 路由，提出主动重优化防震荡 | 取决于物化检查点与探测比例 | 极易陷入未执行分支的“观测盲区”，重度有状态连接中途切换有震荡风险 |
| **#48 Naru** (PVLDB'19) | 单表深度自回归 | 离线训练 + 编译期推理 | 链式法则无损分解 \(\prod P(X_i\mid X_{<i})\) + 渐进重要性采样算范围查询 | GPU 推理约 \(5\sim 10\text{ ms}\) | 仅限单表；高频数据追加若不重训会导致分区尾部估计迅速退化 |
| **#51 NeuroCard** (PVLDB'21) | 跨表深度自回归 | DP 采样训练 + 编译期推理 | 树形 DP 全外连接均匀采样 + `Indicator` 过滤与逐路径 `Fanout` 除权 | 数 MB 至数百 MB 模型，毫秒级 | 仅原生覆盖无环等值连接 Schema；自连接、环路连接与纯 CPU 低延迟仍受限 |

---

## 二、DB2 的两代自学习闭环：从 LEO 计划骨骼校正到 ASC 双引擎统计自治

### 1. DB2 LEO：执行算子真实计数如何反哺下一次编译？ {#paper-50}

2001 年 IBM Almaden 团队发表的 **LEO（DB2's LEarning Optimizer）** 提出了一个极具吸引力的工程设想：既然数据库每天都在真刀真枪地执行查询，每个物理算子吐出了多少行元组原本就是现成的副产品，为什么优化器对昨天的错误视而不见，今天还在同一个谓词上重复犯错？

- **论文信息**：Michael Stillger, Guy Lohman, Volker Markl, Mokhtar Kandil, *LEO – DB2’s LEarning Optimizer*, VLDB 2001 · [原始 PDF（共 10 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/probabilistic-counting/leo-%E2%80%93-db2%E2%80%99s-learning-optimizer.pdf)（核心架构与 `QEP skeleton` 见 PDF p. 3 `Figure 1–2`，偏斜修正曲线见 p. 7 `Figure 4–6`，动态负载计划翻转见 p. 9 `Figure 9`）。

LEO 在传统编译—执行路径旁搭起了一条四段式异步闭环：编译期把优化器做出的基数估计快照裁剪成轻量级的**计划骨骼（`QEP skeleton`）**落盘；执行期在代码片段（`section`）的算子出口维护行计数器；后台分析守护进程在空闲时比对 `skeleton` 里的预测值与实测计数；最后把算出的乘法**调整因子（Adjustment Factor）**写入独立的二层系统表（`LEO_TABLES`、`LEO_COLUMNS`、`LEO_JOINS`），绝不直接覆写 `RUNSTATS` 产出的基础统计表。

这里藏着第一个极易写错的数学细节：**如何防止调整因子在多轮反馈中发生复利叠乘爆炸？** 假设基表统计给出的基础选择率是 \(\text{stats} = 0.25\)，上一轮 LEO 已经挂了一个 \(\text{old\_adj} = 2.0\) 的补丁，因此本次编译预测的选择率是 \(\text{old\_est} = 0.25 \times 2.0 = 0.50\)。结果本次执行实测真实选择率是 \(\text{act} = 0.60\)（相对误差 \(|0.50 - 0.60|/0.60 \approx 16.7\% > 5\%\) 触发阈值）。由于二层目录始终锚定在未修改的基础统计 \(\text{stats}\) 之上，新调整因子必须先把旧补丁剥离干净：

$$\text{adj} = \frac{\text{act}}{\text{stats}} = \frac{\text{act} \times \text{old\_adj}}{\text{old\_est}} = \frac{0.60 \times 2.0}{0.50} = 2.4$$

下一轮编译读取基础值 \(0.25 \times 2.4 = 0.60\)，恰好一步到位收敛，而不会误算成 \(0.50 \times (0.60/0.50)\) 再叠乘的发散序列。遇到范围谓词或直方图分位点修正时，LEO 还要检查斜率单调性，防止局部打补丁把累积分布函数拗成概率为负的非法曲线。

> "LEO is not a replacement for statistics, but a rather a complement: LEO gives the most improvement to the modeling of queries that are either repetitive or are similar to earlier queries, i.e., queries for which the optimizer’s model exploits the same statistical information." *(PDF p. 5)*

更深刻的系统教训来自**执行引擎的部分可观测性陷阱与局部补丁的一致性悖论**：
- **索引范围截断（`IXSCAN` Start/Stop Key）**：当谓词 `PRICE BETWEEN 100 AND 200` 充当 B+ 树索引的起止键时，存储引擎压根没碰区间外的叶子页，此时计数器只能拿到谓词过滤后的行数，无法反推整张基表的真实总行数 \(|T|\)；
- **合并连接的短路早停（`Merge Join` Early-Out）**：一旦左输入提前耗尽，算子立刻停止拉取右输入，导致右支路计数器停留在半截残值，除非右支路下方恰好横着一个阻塞式的排序或物化大坝（`SORT` / `TEMP` Dam）提前把右表全量消费完毕；
- **局部调整因子的概率公理冲突（Maximum-Entropy Tension）**：假设历史查询先后实测得知 \(P(A=1) = 0.4\)、\(P(B=1) = 0.5\) 以及合取谓词 \(P(A=1 \land B=1) = 0.35\)，写入了二层调整表；可下一次编译一条从未跑过的补集查询 `WHERE A=1 AND B<>1` 时，若优化器对未记录的子区间仍按旧独立公式 \(0.4 \times (1 - 0.5) = 0.20\) 估算，就会出现 \(P(A=1 \land B=1) + P(A=1 \land B\neq 1) = 0.35 + 0.20 = 0.55 > P(A=1) = 0.40\) 的概率破产！这也直接促使后续 Markl、Haas 等人在 DB2 中引入最大熵原理（Maximum Entropy），把已知局部反馈当作线性约束，求解全局无偏且总和严格守恒的联合分布。

> "Our performance measurements on a 10 GB TPC-H database [TPC00] show that for our prototype of LEO the monitoring overhead is below 5% of the total query execution time, and therefore may be neglected for most applications." *(PDF p. 9)*

在 10 GB TPC-H（Q2、Q14 串行与 SMP）实测中，LEO 运行时计数器开销始终低于 \(5\%\)。论文 PDF p. 8–9 `Figure 9` 展示的动态偏斜翻转过程尤为直观：假设促销季更新后，过滤条件 `X` 的真实命中数飙升至 `21,623` 行、另一侧条件 `Y` 涨至 `17,949` 行，而旧统计表仍停留在百行量级：

```text
// 第 1 次执行（依据陈旧 RUNSTATS）：严重低估导致误选嵌套循环 + 二级索引随机回表
NLJOIN (est = 142, act = 18,450)        // LEO 守护进程捕获百倍偏差，回填二层目录
  outer: IXSCAN Tab_A [X = 'DEC_PROMO'] (est = 120, act = 21,623)  => adj_X = 180.2
  inner: IXSCAN Tab_B [Y = 'GIFT_CARD'] (est = 95,  act = 17,949)  => adj_Y = 188.9

// 第 2 次重编译同族查询（叠加 LEO_COLUMNS 调整因子后）：自动切换为顺序表扫 + 哈希连接
HSJOIN (est = 18,450)                   // 端到端执行速度直接提升 14 倍！
  build: TBSCAN Tab_B (est = 17,949)
  probe: TBSCAN Tab_A (est = 21,623)
```

需要留意的是，对于已经静态编译并驻留在包缓存（Package Cache）中的老计划，2001 年原型版 LEO 并不会在查询执行半途强行重绑，而是留给后续新编译或显式重绑定消费——如何自动化管理全库统计生命周期，很快交棒给了下一代系统 ASC。

### 2. DB2 UDB ASC：主动变更检测与查询反馈驱动的列组统计收集 {#paper-08}

LEO 证明了执行反馈威力巨大，但在生产环境里靠零散的局部调整因子硬扛陈旧的基础统计，终究是治标不治本：如果底层表刚批量灌入百万行新分区，或者某两列之间本来就存在长期稳定的函数依赖，最彻底的解法不是维护一万条易冲突的局部补丁，而是**让数据库自己在正确的时机、针对正确的列与列组（Column Group）重跑 `RUNSTATS`**。这就是三年后落地在 DB2 UDB v8.2 中的 **ASC（Automated Statistics Collection）**。

- **论文信息**：Ashraf Aboulnaga, Peter J. Haas, Mokhtar Kandil, Sam Lightstone, Guy Lohman, Volker Markl, Ivan Popivanov, Vijayshankar Raman, *Automated Statistics Collection in DB2 UDB*, VLDB 2004 · [原始 PDF（共 12 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/statistics/automated-statistics-collection-in-db2-udb.pdf)（双闭环架构见 PDF p. 3 `Figure 1`，CDF 面积差扫描见 p. 4 `Figure 2–3`，`ACCIDENTS` 案例响应时间见 p. 10 `Figure 10–11`）。

ASC 在架构上把“收集什么统计”（持久化在 `SYSSTAT.PROFILE`）与“统计值本身”彻底解耦，用两套互补的分析引擎共同喂养一个五级优先级（`critical > urgent > pressing > needed > useful`）的后台限流 `RUNSTATS` 调度器（非维护窗口内通过自适应休眠把前台查询减速严格压在 \(\sim 7\%\) 以内）：

第一套引擎是面向**主动预防**的 **Activity Analyzer（`AA`）**，专门防止批量 DML 后新查询一上来就踩雷。为了避免对全库表盲目重算，`AA` 设了两道关卡：先由内存计数器检查器 `DAC`（Data-Activity Checker）筛出增删改（UDI）累计超过 \(\tau\%\)（默认 \(\tau = 10\)）的活跃表；过关后再由变更分析器 `CA`（Change Analyzer）对表做约 **2,000 页**的页级伯努利抽样（Page-level Bernoulli Sampling），用扫描线算法计算样本经验分布 \(e_S\) 与目录旧分位数分布 \(e_R\) 之间的归一化 \(L_1\) 累积分布面积差：

$$\text{change} = \frac{1}{u - l}\int_{l}^{u} \big|e_R(C \le v) - e_S(C \le v)\big|\,dv > \theta$$

但作者敏锐地指出了主动抽样的天然盲区——如果业务只改了不到 \(10\%\) 的行，却把这批行的关键状态码改成了极度偏斜的热点值，又或者表压根没变、纯粹是因为两列相关导致 `AVI` 算错，`AA` 就完全束手无策：

> "The current implementation of DAC uses a value of τ = 10. It is possible that in some unusual cases a small number of records in a given table are changed, but the data values in these records are altered so drastically that query performance is affected. In this case, the table may not be detected by the DAC, and hence the AA." *(PDF p. 4)*

> "We have found that a sample consisting of about 2000 pages of table data, selected using page-level Bernoulli sampling, provides sufficient statistical precision for our purposes; see [PIHS96, IMHB04]." *(PDF p. 5)*

这时第二套引擎 **Query Feedback Analyzer（`QFA`）** 就接管了战场。它继承自 LEO 的查询反馈仓库（`QFW`），下辖表基数分析器 `TCA`、动态扩充高频值个数 \(K \leftarrow \min(m+n, \beta)\) 的简单谓词分析器 `SPA`，以及最关键的**相关性分析器 `COA`（Correlation Analyzer）**。当 `QFW` 里积累了某张表 \(m\) 行上的单列真实计数 \(\alpha_1, \alpha_2\) 与合取谓词真实计数 \(\alpha_{12}\) 时，`COA` 直接检验独立性比值是否落在容忍带 \([1-\Theta, 1+\Theta]\) 内：

$$1 - \Theta \le \frac{\alpha_{12}\cdot m}{\alpha_1 \cdot \alpha_2} \le 1 + \Theta$$

一旦越界，它就按绝对误差 \(|\alpha_{12} - \alpha_1\alpha_2/m|\) 排序，自动往 `SYSSTAT.PROFILE` 里写入联合列组（Column Group）收集指令，交由后台 `RUNSTATS` 永久固化。

论文在 1.5 GB 四表 `ACCIDENTS` 库（含 `(COUNTRY3, CITY)` 与 `(MAKE, MODEL)` 强相关列对）上的五阶段演进实验堪称教科书级（PDF p. 10 `Figure 10–11`）：

| 实验阶段与统计状态 | Query 10 耗时（强列组相关） | Query 11 耗时（范围与基数漂移） | 触发的核心因果机制 |
| :--- | :---: | :---: | :--- |
| **A. 空统计初始态（默认目录值）** | `21.8 s` | `46.2 s` | 缺省均匀假设歪打正着避开了极端坏计划 |
| **B. 仅刷新单列基础统计** | `> 100 s`（显著退化） | `20.1 s` | 单列频率越精确，独立相乘后的联合选择率反而低估得更离谱，诱使优化器误选嵌套循环 |
| **C. 批量灌入加拿大事故记录后（未重跑统计）** | `> 100 s` | `> 900 s` | 表行数与城市分布剧烈漂移，两类查询双双雪崩 |
| **D. 仅开启主动变更引擎 `AA`** | `130.5 s` | `20.4 s` | `DAC + CA` 捕捉到 UDI 与 CDF 面积差，救回单列漂移的 Q11，但看不见跨列函数依赖 |
| **E. 开启 `AA + QFA` 全闭环** | **`13.1 s`** | **`18.1 s`** | `COA` 识别联合误差并写入 `SYSSTAT.PROFILE`，后台补齐列组统计后两类查询同时收敛 |

这一组对比极具警示意义：在强相关列上，**“更勤快地只刷单列直方图”非但救不了联合基数，反而可能破坏原有的误差对冲而触发严重回退**；唯有把主动页抽样（`AA`）与反馈相关性挖掘（`QFA`）结合起来，才能在仅 \(1\%\sim 2\%\) 监控开销、不到 1 分钟分析两万条历史查询的代价下实现闭环自愈。

---

## 三、Oracle 10g 的全局诊断树与非侵入式 SQL Profile

就在 DB2 打磨 LEO 与 ASC 的同一时期，Oracle 10g 团队从企业级运维的另一个痛点切入：在跑着成百上千个并发会话的商业 ERP（如 SAP、Siebel）生产库上，DBA 第一眼看到的往往不是“某条 SQL 少算了 100 倍行数”，而是满屏互相打架的局部指标——缓冲区命中率 \(99\%\)、平均磁盘响应 \(8\text{ ms}\)、每秒软解析两万次；等到好不容易抓出一条慢 SQL，应用厂商又严禁修改哪怕一个字符的 SQL 源码（不能加 Hint），也不能随便锁定死板的执行计划大纲（Plan Outline）。Oracle 用 **ADDM** 与 **Automatic SQL Tuning** 给出了完整的双层工程答卷。

### 3. Oracle ADDM：用统一货币 DB Time 剪枝全局根因诊断树 {#paper-38}

如果不同子系统各自汇报不可通约的比率指标，任何自动化诊断都会沦为拍脑袋的阈值报警器。Oracle 10g 在 **ADDM（Automatic Database Diagnostic Monitor）** 中做的第一件事，就是确立全库唯一的性能度量硬通货——**数据库时间（`DB Time`）**。

- **论文信息**：Karl Dias, Mark Ramacher, Uri Shaft, Venkateshwaran Venkataramani, Graham Wood, *Automatic Performance Diagnosis and Tuning in Oracle*, CIDR 2005 · [原始 PDF（共 11 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/diagnosis-and-tuning/automatic-performance-diagnosis-and-tuning-in-oracle.pdf)（`DB Time` 定义见 PDF p. 3 `Figure 1–2`，`DBTime-graph` 见 p. 5 `Figure 3`，`ASH` 采样与闭环见 p. 6 & p. 9）。

`DB Time` 被严格定义为数据库服务器花在**响应前台用户请求**上的累计耗时（CPU 执行时间 + 各类非空闲等待时间之和），显式剔除了客户端思考与网络往返耗时；后台刷脏或归档进程本身不直接计入 `DB Time`，只有当它们争抢锁或拖慢 I/O 间接卡住前台会话时，那部分影响才会自然投射到前台 `DB Time` 上；对于并行查询（Parallel Query），框架还专门扣除了进程间协调等待，杜绝重复记账。

> "One of the things that makes performance tuning difficult is that the performance of different components of the database is often measured using unrelated metrics like buffer hit-ratio, transactions per second and average I/O latency. Such unrelated metrics make it infeasible to weigh the performance impact of one component of the database with the impact of others components." *(PDF p. 2)*

有了统一货币，Oracle 围绕“极低侵入原则”搭建了 `Observe -> Diagnose -> Resolve` 闭环：底层用单调递增累积计数器的区间差分（每小时持久化的 `AWR` 快照）叠加采样间隔比分析窗口小两个数量级以上的内存活动会话历史（`ASH`, Active Session History），在不加全局重锁的前提下拿到细粒度切面；上层 `ADDM` 则构建一棵覆盖“连接建立 / 硬软解析 / SQL 执行 / 事务提交”与 700 余种等待事件层级的有向无环图 **`DBTime-graph`**。

这棵图最精妙的数学性质在于**严格的父子时间包含不变量（Parent-Child Containment）**：任意子节点 \(v\) 所归属的数据库时间必然是其每一个父节点 \(u \in \text{Parents}(v)\) 的严格子集：

$$\forall u \in \text{Parents}(v), \qquad \text{DBTime}(v) \subseteq \text{DBTime}(u), \qquad \Delta C(t_1, t_2) = C(t_2) - C(t_1)$$

其中底层 `AWR` 一律只持久化无锁单调递增的累积计数器 \(C(t)\)，分析任意快照区间 \([t_1, t_2]\) 时只做一次减法即可拿到零漂移的区间增量；而 `ASH` 则以每秒一次的轻量内存环形缓冲区对活动会话抽样，填补快照间的瞬态尖峰。依托父子时间包含律，`ADDM` 自顶向下遍历时可以直接实施分支限界剪枝——只要某个父节点（例如“解析阶段”或“用户 I/O 类”）占总 `DB Time` 的比重低于显著性阈值，其下方整片子树便被瞬间裁掉，并在叶节点结合内存 `what-if` 收益曲线区分“症状”与“根因”。

> "Our second requirement is the “minimal intrusion principle”; it states that the act of collecting measurements for performance diagnostics should not cause a significant degradation in performance." *(PDF p. 5)*

压测表明，Oracle 10g 全部可管理性采集与诊断特性合在一起仅造成约 **\(3\%\)** 的吞吐折损，单次快照 `ADDM` 根因树遍历耗时不到 **\(10\text{ s}\)**。论文复盘的三起真实生产排障案例清晰展示了“沿着 `DBTime-graph` 剥离症状与根因”的威力（PDF p. 9–10）：

| 生产现场案例 | 传统监控看到的表层症状 | `ADDM` 沿 `DBTime-graph` 剪枝下钻路径 | 最终锁定的根因与处置动作 |
| :--- | :--- | :--- | :--- |
| **高通（Qualcomm）补丁升级后系统假死** | 整机 CPU 飙满，吞吐骤降，数百会话排队 | `SQL Execution (>90% DB Time)` \(\to\) 定位单条高频 `UPDATE` 语句消耗绝大部分逻辑读 | 补丁升级脚本漏建关键二级索引；转交 `SQL Tuning` 补建索引后秒级恢复 |
| **惠普 PA-RISC 缺陷库间歇卡顿** | 满屏报警 `free buffer waits`，直觉极易误判为 Buffer Cache 太小 | `Buffer Busy / Free Buffer Waits` \(\to\) 顺因果边追踪后台 `DBWR` 刷脏阻塞 \(\to\) `Write I/O Latency` | 否决盲目扩内存，揪出底层存储开启了退化的同步写 I/O 拖慢脏页回收 |
| **跨 12 时区全球业务库解析风暴** | `library cache latch` 与共享池锁争用高企 | `Parse Time` 分支突破显著性阈值 \(\to\) `Hard Parse` \(\to\) 字面量 SQL 聚类比对 | 应用拼接常量未用绑定变量（Bind Variables），引发跨时区高峰硬解析雪崩 |

### 4. Oracle Automatic SQL Tuning：分钟级调优优化器与免改 SQL 的辅助画像 {#paper-39}

当 `ADDM` 沿着 `DB Time` 抓出那几条吃掉全库大半 CPU 与 I/O 的高负载 SQL 后，接力棒就交到了 **Automatic Tuning Optimizer（`ATO`，对外暴露为 `SQL Tuning Advisor`）** 手中。它的出发点直指常规编译器的物理软肋：正常编译必须在亚秒级（几毫秒到几十毫秒）内出计划，根本来不及去磁盘上试探复杂谓词组合或非等值连接的真实基数；可一旦转入后台离线调优窗口，为什么不给优化器**几分钟**的时间预算，让它亲自下场采样和局部试跑？

- **论文信息**：Benoit Dageville, Dinesh Das, Karl Dias, Khaled Yagoub, Mohamed Zait, Mohamed Ziauddin, *Automatic SQL Tuning in Oracle 10g*, VLDB 2004 · [原始 PDF（共 12 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/diagnosis-and-tuning/automatic-sql-tuning-in-oracle-10g.pdf)（整体架构与 `SQL Profiling` 循环见 PDF p. 2–3 `Figure 1–2`，73 条客户复杂查询实测见 p. 8 `Table 1` 与 `Figure 4–6`）。

> "The main difference is that the Automatic Tuning Optimizer is allowed to run for a much longer period of time, generally minutes versus a sub-second during the regular optimization mode. The Automatic Tuning Optimizer takes advantage of this extra time to profile the SQL statement and validate the statistics and estimates used in the process of building an execution plan." *(PDF p. 2)*

`ATO` 接收封装了 SQL 文本、绑定变量、Schema 与历史执行统计的调优集（`STS`），依次开展四项深度体检：
1. **统计信息分析（Statistics Analysis）**：通过逐步倍增样本量的动态采样验证基础目录统计，一旦发现缺统计或陈旧漂移，优先建议重采基础统计，仅在未重采前生成临时补偿系数。
2. **SQL 画像分析（SQL Profiling）**：针对包含复杂函数表达式、多列强相关或跨表连接过滤的子表达式，启动动态采样与**查询片段局部执行（Partial Execution）**，实测中间结果真实基数；若历史执行统计显示应用游标每次只拉取前 \(n\) 行就关闭，还会自动定制 `FIRST_ROWS(n)` 优化目标。
3. **访问路径分析（Access Path Analysis）**：在优化器内部模拟虚拟索引（`what-if`）测算单语句加速比；若需权衡全库并发 DML 维护索引的写放大代价，则无缝移交 `SQL Access Advisor` 做全局归并推荐。
4. **SQL 结构分析（SQL Structure Analysis）**：专门扫描计划树中代价占比 **\(> 10\%\)** 的重度算子，识别因应用写法欠佳导致的物理瓶颈——例如程序员随手写下 `UNION`（触发昂贵的全量排序去重，而业务主键本就无重复，改 `UNION ALL` 可直接免排）、在可空列上误用 `NOT IN`（迫使引擎退化为极其缓慢的空值感知反连接，改 `NOT EXISTS` 即可走高效哈希反连接），或视图层叠留下的冗余外连接。由于这类改写在严格集合/三值逻辑下并不恒等，唯有业务开发者知晓约束是否成立，`ATO` 严守边界只输出诊断报告供人工拍板，绝不后台静默篡改结果集语义。

> "The Automatic Tuning Optimizer uses dynamic sampling and partial execution (i.e. execute fragments of the SQL statement) techniques to verify its standard estimates of cardinality, cost, etc." *(PDF p. 3)*

在工程落地上，`SQL Profile` 相比传统的 Hint 或 Plan Outline 做出了一个极其老辣的边界切割：**它既不碰应用发来的 SQL 文本，也不把物理执行计划焊死**。固化在 `SQL Tuning Base (STB)` 里的 `SQL Profile` 仅仅是一组辅助性的选择率校正因子与环境参数；当未来表规模翻倍或新增了更好的索引时，常规亚秒级优化器依然照常做完整的代价搜索，只是在算到相关子表达式时把 `SQL Profile` 的校正系数叠加上去，从而兼顾了免改源码的兼容性与随数据演化的灵活性。

在收集自真实客户的 **73 条高难度 DSS 慢查询**（普遍含多表相关与复杂非等值连接）上，即便严令禁止创建新索引、禁止修改 SQL 文本，**仅接受自动生成的 `SQL Profile`**，实测对比也极具冲击力（PDF p. 8 `Table 1`）：

| 调优策略 | 73 条查询总响应时间 | 单条平均耗时 | 最慢单条耗时 | 是否需要改写 SQL / 焊死计划 |
| :--- | :---: | :---: | :---: | :--- |
| **调优前默认优化器（Untuned）** | `58,821 s` | `817 s` | `5,751 s` | 否 |
| **资深专家手工添加 Hint** | `2,131 s` | `30 s` | `275 s` | 需侵入修改 SQL 文本并固定物理算子 |
| **Oracle `ATO` 自动生成 `SQL Profile`** | **`929 s`** | **`13 s`** | **`59 s`** | **零文本改动，保留未来代价重搜索空间** |

全部 73 条复杂 SQL 的后台总调优耗时仅 **74 分钟**（平均每条约 1 分钟），却换来了相对调优前 **\(63\times\)**、甚至比人工硬绑 Hint 还快 **\(2.3\times\)** 的整体提速——原因正在于人工 Hint 往往只盯住表面最慢的一个连接算子强扭顺序，而 `ATO` 通过片段局部试跑一次性校准了整棵算子树上多处连环失真的中间基数。

---

## 四、当慢查询等不及下一次编译：查询内自适应处理（AQP）的收益与震荡边界

前面四篇工业界杰作都有一个共同前提：**容忍第一条探路查询慢一点（或者等后台分钟级 `ATO` 跑完），去拯救后续成千上万条同类查询**。然而在即席交互分析（Ad-hoc BI）、联邦跨源查询或持续数据流场景下，一条估计失真的 8 表关联查询第一次跑就可能卡死 6 个小时，业务根本等不到“下一次编译”。于是，能否在**同一条查询正在执行的半路上**动态改弦更张，便成了自适应查询处理（Adaptive Query Processing, **AQP**）试图攻克的险峰。

### 5. Babu & Bizarro：镜中审视 Plan-based 重优化与 Eddies 元组路由 {#paper-07}

面对 1990 年代末至 2005 年间井喷的数十种 AQP 原型系统，Shivnath Babu 与 Pedro Bizarro 在 CIDR 2005 发表的这篇思辨综述没有堆砌系统编年史，而是像拆机一样把“在飞行中换引擎”拆成了统一的反馈控制闭环，并毫不留情地戳破了中途变道潜藏的观测盲区与震荡陷阱。

- **论文信息**：Shivnath Babu, Pedro Bizarro, *Adaptive Query Processing in the Looking Glass*, CIDR 2005 · [原始 PDF（共 12 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/statistics/adaptive-query-processing-in-the-looking-glass.pdf)（AQP 演化谱系见 PDF p. 3 `Figure 1`，Plan-based 闭环见 p. 4 `Figure 2–3`，重优化质量与变化率权衡见 p. 9 `Table 6–7`）。

作者首先按“如何采集统计”与“谁来拍板变道”把查询内自适应系统拆成清晰的二维谱系：
- 在**统计监测机制**上，分为不改动物理算子的被动**观测（Observation）**与路由**探索（Exploration）**，以及需要侵入改造算子内部逻辑的多分支**竞争执行（Competition，如 Antoshenkov 双计划竞速）**与采样**探针（Profiling）**；
- 在**控制环路形态**上，则收敛为三大范式：
  1. **基于计划的自适应（Plan-based AQP）**：代表系统如 Kabra & DeWitt 的中段重优化、Tukwila 以及 DB2 后续演进出的 `POP`（Progressive Optimization）。它们保留传统优化器，在物化阻塞点或流水线检查点比对实测行数与编译预期，一旦冲破稳健区间就暂停当前管道，把已物化的中间表当作新基表重新调用优化器编译剩余子查询。
  2. **面向持续查询的自适应（CQ-based AQP）**：在非阻塞流式长查询上跟踪滑窗到达率与选择率漂移，分摊周期性计划重组开销。
  3. **基于路由的自适应（Routing-based AQP）**：以 **Eddies** 与 **River** 为代表，干脆废掉了显式的“重编译”阶段——在可交换的无状态或对称连接算子池（如双侧哈希连接 `Ripple Join` / `XJoin`）中央放一个 `Eddy` 路由器，每行元组携带 `Ready` 与 `Done` 位图，路由器采用类似彩票调度（Lottery Scheduling）的记分规则（算子每吃进一行发一张票、每吐出一行退回一张票），过滤率越高、处理越快的算子攒下的净票数越多，自然赢得绝大多数后续元组，仅留极小比例随机探索（Exploration）持续探测分布漂移。

听起来似乎只要监测够快、路由够灵，初版估计错得再离谱都能当场救回来？作者用一个极具杀伤力的四表反例（PDF p. 8 `Example 4.1`）浇了一盆冷水：假设查询 \(R \bowtie S \bowtie T \bowtie U\) 真正的全局最优连接顺序是 \(((R \bowtie U) \bowtie S) \bowtie T\)，但初始优化器因为低估误选了 \(((R \bowtie S) \bowtie T) \bowtie U\)。当执行完前两步，监控器惊恐地发现 \(|R \bowtie S \bowtie T|\) 远大于预期并立即触发中途重优化——**然而此时此刻，系统只观测到了已执行前缀的真实基数，对于从未执行过的分支 \(|R \bowtie U|\) 与 \(|R \bowtie T|\) 依然两眼一抹黑！** 被迫盲猜的重优化器极有可能跳进另一个同样糟糕的次优坑里。

除此之外，中途切计划还横亘着两道硬约束：
- **有状态算子的精确迁移不变量**：不妨设想对称流水线哈希连接正在执行 \((R \bowtie S) \bowtie T\)，在时刻 \(t_{\text{cut}}\) 已分别读入前缀 \(R_1, S_1, T_1\) 并向下游吐出了 \(R_1 \bowtie S_1 \bowtie T_1\)；此时监控器把剩余连接顺序切换为 \((R \bowtie T) \bowtie S\)，后续新流入的后缀记为 \(R_2, S_2, T_2\)。根据集合分配律，完整连接结果等于八个子项的并集：
  $$(R_1 \cup R_2) \bowtie (S_1 \cup S_2) \bowtie (T_1 \cup T_2) = \underbrace{(R_1 \bowtie S_1 \bowtie T_1)}_{\text{切换前已输出，严禁重发}} \;\cup\; \underbrace{\bigcup_{(i,j,k)\neq(1,1,1)} (R_i \bowtie S_j \bowtie T_k)}_{\text{剩余 7 项跨阶段组合必须补齐且不重不漏}}$$
  如果状态迁移逻辑简单丢弃旧哈希表里的 \(R_1, S_1\)，就会永久漏掉 \(R_1 \bowtie S_2 \bowtie T_2\) 等跨切点匹配；反过来若不打切换代数戳记直接重放，又会把 \(R_1 \bowtie S_1 \bowtie T_1\) 重复吐给客户端！维护这套跨阶段完备性的簿记成本，往往比连接本身还要沉重。
- **过度敏感引发的震荡（Thrashing）**：如果底层数据按日期或地区聚簇存放，局部数据页上的瞬态偏斜会诱使激进的 AQP 系统每隔几毫秒就推翻重编、搬迁哈希状态，把绝大部分 CPU 烧在切换动作本身而非推进查询进度上。

> "The AQP system can identify statistics that are prone to transient changes, so that it can reduce chances of thrashing on such changes. Note that plan logging and proactive re-optimization are orthogonal and compatible ideas, so the best approach may be to do both." *(PDF p. 11)*

为此，论文给出了后来直接影响 Spark AQE（Adaptive Query Execution）与 SQL Server Adaptive Joins 的两条务实出路：一是**主动重优化（Proactive Re-optimization）**——编译期就显式计算统计不确定性区间，宁可选对基数波动更稳健、容易在检查点截断切换的 `Hybrid Hash Join`，也别赌一旦算错就万劫不复的 `Index Nested-Loop Join`，或顺带混入 \(5\%\) 随机页序抽样提前探路；二是**计划日志（Plan Logging）**，记录 \(\langle t, S, P \rangle\) 轨迹识别瞬态抖动，避免在同一个局部波峰上反复横跳。今天工业界主流引擎之所以普遍把中途自适应收敛在 **Shuffle 物化边界之后**（如动态合并倾斜分区、把 Shuffle Hash / Sort-Merge Join 降级为 Broadcast Hash Join），正是为了避开流水线中途迁移有状态算子的泥潭，在“确定性观测点”上稳稳吃下 Plan-based AQP 最大的红利。

---

## 五、彻底抛弃独立性假设：深度自回归联合分布与跨表无偏采样

无论是查询间的 `ASC` / `SQL Profile`，还是查询内的 `AQP`，归根结底都是在传统优化器算错之后做修补与止损——列组统计受制于管理员或反馈能覆盖的少数列对，不可能为一张 15 列表的 \(\binom{15}{4}\) 种任意四列组合全都预建多维直方图。到了 2019–2021 年，UC Berkeley RISELab 团队（Zongheng Yang、Ion Stoica、Joseph M. Hellerstein 等）从第一性原理重新发问：**既然根源在于概率分布被强行拆成了独立边缘分布的乘积，我们能不能直接用一个紧凑的深度生成模型，把整张表乃至整张数据库无环连接图的联合概率分布 \(P(X_1, \dots, X_n)\) 原原本本地学下来？**

![Naru 与 NeuroCard 深度自回归基数估计与渐进采样积分流水线](/images/diagrams/paper-ce-naru-neurocard-sampling.svg)

### 6. Naru：精确链式法则分解与范围查询的渐进重要性采样 {#paper-48}

早期机器学习基数估计（如 `MSCN`）走的是**有监督回归**路线：先收集一大批历史查询和它们的真实基数标签，再训练神经网络把“查询特征向量”映射成“预测行数”。这条路在生产中有一个致命硬伤——训练标签采集极其昂贵，一旦线上来了训练集中没见过的谓词组合，黑盒回归器的泛化误差立刻失控。

2019 年发表的 **Naru（Deep Unsupervised Cardinality Estimation）** 彻底换了赛道：**不看任何历史查询，直接对原始表元组做无监督密度估计**。

- **论文信息**：Zongheng Yang, Eric Liang, Amog Kamsetty, Chenggang Wu, Yan Duan, Xi Chen, Pieter Abbeel, Joseph M. Hellerstein, Sanjay Krishnan, Ion Stoica, *Deep Unsupervised Cardinality Estimation*, PVLDB 13(3): 279–292, 2019 · [原始 PDF（共 14 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/probabilistic-counting/deep-unsupervised-cardinality-estimation.pdf)（掩码自回归架构见 PDF p. 4 `Figure 2`，渐进采样直觉与算法见 p. 5–6 `Figure 3 & Algorithm 1`，DMV 与 Conviva 评测见 p. 8–9）。

设有限关系表 \(T\) 共 \(|T|\) 行、\(n\) 列，把表中每一行看作经验多重集分布的一次独立观测，任意元组取值 \((x_1, \dots, x_n)\) 的精确联合概率由概率论链式法则唯一给出，**零独立性假设**：

$$P(x_1, \dots, x_n) = \prod_{i=1}^{n} P(X_i = x_i \mid X_1 = x_1, \dots, X_{i-1} = x_{i-1})$$

Naru 先将每列不同取值映射为保序整数词表 ID（当列基数 \(|A_i| > 64\) 时采用维度 \(h=64\) 的输入输出共享嵌入矩阵压缩参数），再用带前缀权重掩码的自回归网络（`MADE` / `ResMADE` 或 `Transformer`）一次性在一次前向传播中吐出全部 \(n\) 个条件分类分布 \(\hat{P}(X_i \mid x_{<i})\)，直接最小化原始表元组的负对数似然（交叉熵）完成无监督极大似然训练。

> "Thus, autoregressiveness is achieved via such input masking. Training these model outputs to be as close as possible to the true conditional densities is done via maximum likelihood estimation." *(PDF p. 4)*

学会单点概率 \(\hat{P}(x_1, \dots, x_n)\) 只是万里长征第一步。真正的拦路虎在**范围查询（Range Query）推理**：一条形如 `WHERE X1 BETWEEN 10 AND 500 AND X2 >= 2000 ...` 的查询等价于对超矩形区域 \(R = R_1 \times R_2 \times \cdots \times R_n\) 求离散积分 \(\sum_{x \in R} \hat{P}(x)\)。如果逐点枚举，11 列表上的普通范围查询就会把 GPU 算到天荒地老：

> "Naive estimation of the range density by integrating over the query region requires summing up an enormous number of points. In an 11-dimensional table we consider, a challenging range query has 10^10 points in the query region, which would take more than 1,000 hours to sum over by a naive enumeration scheme." *(PDF p. 1)*

如果改用朴素前向蒙特卡洛（从 \(\hat{P}\) 盲抽 \(S\) 个样本看几个落在 \(R\) 里），遇到选择率 \(10^{-5}\) 的窄区间，抽 \(2,000\) 次几乎全军覆没返回 \(0\)。Naru 的核心算法贡献 **Progressive Sampling（渐进重要性采样，PDF p. 6 `Algorithm 1`）** 漂亮地化解了这个死结——**强迫每一条采样路径自左向右永远只往合法区间 \(R_i\) 内部走，同时把每一步的截断质量乘进路径权重**：

1. 初始化 \(S\) 条并行采样路径（如 \(S = 2,000\)），每条路径初始权重 \(w^{(k)} = 1\)。
2. 对第 \(i = 1, \dots, n\) 列：喂入已抽出的前缀 \(s_{<i}^{(k)}\)，由自回归网络输出第 \(i\) 列的完整离散分布 \(\hat{P}(X_i \mid s_{<i}^{(k)})\)。
3. 计算落在谓词合法区间 \(R_i\) 内的局部概率和 \(z_i^{(k)} = \sum_{v \in R_i} \hat{P}(X_i = v \mid s_{<i}^{(k)})\)，更新路径权重 \(w^{(k)} \leftarrow w^{(k)} \cdot z_i^{(k)}\)。
4. 将 \(R_i\) 以外的槽位全部清零，对剩余合法分布重归一化得到提议分布 \(q_i(v \mid s_{<i}^{(k)}) = \frac{\hat{P}(X_i = v \mid s_{<i}^{(k)})}{z_i^{(k)}} \cdot \mathbb{1}[v \in R_i]\)，从中抽出第 \(i\) 列取值 \(s_i^{(k)}\) 传给下一列。

把整条路径抽到头的提议概率乘起来看一眼：
$$q\big(s^{(k)}\big) = \prod_{i=1}^{n} \frac{\hat{P}(s_i^{(k)} \mid s_{<i}^{(k)})}{z_i^{(k)}} = \frac{\hat{P}\big(s^{(k)}\big)}{\prod_{i=1}^n z_i^{(k)}} = \frac{\hat{P}\big(s^{(k)}\big)}{w^{(k)}}$$
重要性比率 \(\frac{\hat{P}(s^{(k)})}{q(s^{(k)})}\) 分子分母中的 \(\hat{P}(s^{(k)})\) **完美约掉**，恰好就等于算法一路顺手累乘出来的标量 \(w^{(k)} = \prod_{i=1}^n z_i^{(k)}\)！用几行矩阵化伪代码就能看清它在 GPU 上为何能跑进毫秒级：

```python
# Naru Progressive Sampling (批量矩阵化推理，S 条路径同步前向)
w = ones(S)                           # S 条采样路径初始权重全为 1
prefix = empty(S, 0)                  # 已抽出的列前缀 s_{<i}
for i in range(n):
    if col[i] is UNCONSTRAINED:       # Wildcard-skipping：未过滤列直接填预训练 MASK
        prefix = append(prefix, MASK_TOKEN)
        continue
    logits = ar_model.forward_step(i, prefix)   # (S, |A_i|) 条件对数概率
    probs  = softmax(logits, dim=-1)
    valid  = probs * range_mask(R[i])           # 抹零合法区间 R_i 以外的所有槽位
    z_i    = valid.sum(dim=-1)                  # (S,) 当前列落在 R_i 内的条件概率质量
    w      = w * z_i                            # 累乘路径重要性权重
    s_i    = multinomial(valid / z_i[:, None])  # 在合法区间内按重归一化分布抽样
    prefix = append(prefix, s_i)
est_card = table_rows * w.mean()
```

由此，\(\frac{1}{S}\sum_{k=1}^S w^{(k)}\) 是固定自回归模型 \(\hat{P}\) 下区域概率 \(\hat{P}(X \in R)\) 的**精确无偏估计**（注：这里的无偏针对已学出的模型分布 \(\hat{P}\) 而言，有限隐藏层容量对真实表分布 \(P\) 仍存在拟合近似误差）。衡量基数估计精度的标准指标采用对称乘法误差 **Q-error**，定义为预测值与真实值之比的上确界：

$$\text{Q-error}(\widehat{\text{card}}, \text{card}) = \max\!\left(\frac{\widehat{\text{card}}}{\text{card}},\; \frac{\text{card}}{\widehat{\text{card}}}\right) \ge 1$$

之所以在优化器评测中必须看 Q-error 的 `p95`、`p99` 与 `max` 而非平均相对误差，是因为代价搜索对低估与高估同样敏感：只要在几百个候选子计划里有一处爆出 \(10^3\) 的尾部 Q-error，动态规划就会被误导进灾难性的连接分支。

在真实纽约州机动车辆表 `DMV`（约 1,159 万行、11 列，模型仅 `12.7 MB`）上，2,000 条含多列强相关范围谓词的测试查询在 V100 GPU 上单次推理仅需 **\(5\sim 10\text{ ms}\)**，**最大 Q-error 被死死压在 `8.0`**（中位数接近 `1.0`），而传统直方图、采样与多维核密度估计在尾部动辄飙出 \(10^3 \sim 10^4\) 倍误差；在 15 列 `Conviva-A` 流媒体会话表上，将采样数 \(S\) 从 `1,000` 增至 `4,000` 即可把极低选择率查询的最大 Q-error 从 `185` 压到 **`4`**。不过作者也坦诚展示了它的软肋：当连续追加 5 个新日期分区却不刷新模型时，尾部最大误差会从 `2` 劣化到 `53.5`。

### 7. NeuroCard：全外连接均匀采样、扇出除权与全库单模型基数估计 {#paper-51}

Naru 攻克了单表多列相关，但一进入真实星型或雪花型多表 Schema，最难啃的骨头才真正浮出水面：**能不能给每张表各训一个 Naru，在连接时拼起来？** 答案是不能——那恰恰又退回了跨表独立假设（实测在 `JOB-light` 上 `p95` 误差照样飙到 \(\sim 9 \times 10^4\)）。可如果要直接对整张数据库的多表全连接结果表 \(J = T_1 \ T_2 \ \cdots \ T_m\)（采用全外连接 Full Outer Join 语义，确保任何未匹配上的基表孤儿行也不丢失）训一个大自回归模型，立刻就会撞上三堵高墙：
1. 在 `IMDb`（`JOB-M` 16 张表）上，全外连接表 \(|J|\) 的行数高达 **\(2 \times 10^{12} \sim 10^{13}\) 行**，物理物化完全不可能；
2. 绝大多数业务查询只连接其中两三张表组成的子图 \(Q \subset J\)，同一条基表记录在全外连接 \(J\) 中已经被未参与查询的邻居表重复复制了成百上千次；
3. 主外键 ID 列动辄几百万个离散值，直接塞进 Softmax 分类层会让模型体积炸到几 GB。

2020/2021 年发表的 **NeuroCard** 用三项严丝合缝的算法设计把这三堵墙逐一凿穿。

- **论文信息**：Zongheng Yang, Amog Kamsetty, Sifei Luan, Eric Liang, Yan Duan, Xi Chen, Ion Stoica, *NeuroCard: One Cardinality Estimator for All Tables*, PVLDB 14(1): 61–73, 2020/2021 · [原始 PDF（共 13 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/probabilistic-counting/neurocard%3A-one-cardinality-estimator-for-all-tables.pdf)（整体流水线见 PDF p. 2 `Figure 2`，5 行全外连接与扇出除权算例见 p. 5 `Figure 4`，列拆位动态掩码见 p. 7，三大基准评测见 p. 9–10 `Table 2–4`）。

> "Despite decades of research, existing cardinality estimators are inaccurate for complex queries, due to making lossy modeling assumptions and not capturing inter-table correlations. In this work, we show that it is possible to learn the correlations across all tables in a database without any independence assumptions." *(PDF p. 1)*

**第一招：基于树形动态规划的均匀全外连接采样器（Exact-Weight Join Sampler）。** 只要 Schema 连接图是无环树，NeuroCard 就不物化任何连接行，而是先把任意一张表选为树根，沿着连接树自底向上做一趟线性复杂度 \(O(\sum |T_i|)\) 的动态规划：对表 \(T\) 中的任意元组 \(t\)，其在以 \(T\) 为根的子树全外连接中的精确扩展重数 \(W_T(t)\) 等于它在每一个子表 \(C \in \text{Children}(T)\) 上匹配重数之和的乘积（若在子表 \(C\) 中无匹配行，全外连接补一行 `NULL`，因此匹配项保底取 \(1\)）：

$$W_T(t) = \prod_{C \in \text{Children}(T)} \max\!\left(1,\; \sum_{t' \in C:\; t'.k = t.k} W_C(t')\right)$$

这张计数索引在 6 表 Schema 上仅需 `13 s`、在 16 表 `JOB-M` 上仅需 `4 min` 即可构建完毕，根节点权重之和 \(\sum_{t \in R} W_R(t)\) 就是全外连接精确总行数 \(|J|\)。随后采样器自根向叶按 \(W_C(t')\) 比例有放回抽样，源源不断地在线吐出严格独立同分布（i.i.d.）的均匀全外连接训练元组。论文消融实验证明，若偷懒用未加权的有偏连接采样器训练 MLE，`JOB-light` 中位误差会从 `1.9` 直接崩盘到 `33`。

**第二招：虚拟指示列（`Indicator`）与逐路径局部扇出除权（`Fanout Scaling`）。** 这是整篇论文数学最精妙的地方，我们跟着论文 PDF p. 5 `Figure 4` 的 **5 行微型全外连接算例**亲手算一遍就彻底通透了：
设三张表连接链为 \(A \bowtie B \bowtie C\)，它们展开后的 5 行全外连接表 \(J\) 中，满足谓词 `A.x = 2` 的记录共出现了 **3 行**：
- 第 1 行：`A.x = 2` 在 `B` 中匹配了 2 次（\(f_B = 2\)），但该 `B` 行在 `C` 中无匹配补了 `NULL`（故 \(\mathbb{1}_C = 0\)，补空扇出记 \(f_C = 1\)）；
- 第 2、3 行：`A.x = 2` 的另一个 `B` 伙伴（同样 \(f_B = 2\)）在 `C` 中扇出了 2 条真实记录（\(\mathbb{1}_C = 1,\; f_C = 2\)）。

现在看模型如何用**同一个**建在 \(J\) 上的自回归网络回答任意子图查询（把 \(\mathbb{1}_T\) 和 \(f_T\) 作为附加虚拟列一并拼进自回归序列学习）：
- 若查询三表全连接 `A ⋈ B ⋈ C WHERE A.x = 2`：约束 \(\mathbb{1}_A = \mathbb{1}_B = \mathbb{1}_C = 1\)，第 1 行补空记录被指示位剔除，精确命中第 2、3 行，返回基数 \(5 \times \frac{2}{5} = 2\)。
- 若只查单表 `SELECT COUNT(*) FROM A WHERE A.x = 2`（真实答案显然是 **1 行**，只是在 \(J\) 里被未参与查询的 \(B, C\) 放大成了 3 行！）：此时约束 \(\mathbb{1}_A = 1\)，并在**每一条渐进采样路径内部**除以所有未查询表 \(T \notin Q\) 沿唯一树边指向 \(Q\) 的局部扇出乘积：
  $$\widehat{\text{Card}}(Q) = |J| \cdot \frac{1}{S}\sum_{k=1}^{S} \frac{w^{(k)}}{\prod_{T \notin Q} f_T^{(k)}}$$
  把刚才命中的 3 行代入逐行除权求和：
  $$\frac{1}{2 \times 1} + \frac{1}{2 \times 2} + \frac{1}{2 \times 2} = \frac{1}{2} + \frac{1}{4} + \frac{1}{4} = 1$$
  分毫不差还原出 `A` 表真实基数 `1`！反过来，如果谁图省事写成“先求命中概率期望，再除以平均扇出期望”，算出来的就会是 \(3 \big/ \frac{2 + 4 + 4}{3} = 0.9 \neq 1\)——因为随机变量比值的期望绝不等于期望的比值（\(\mathbb{E}[\mathbb{1}_Q / F] \neq \mathbb{E}[\mathbb{1}_Q] / \mathbb{E}[F]\)）！

**第三招：无损列按位分块（Column Factorization）与动态范围掩码。** 面对百万基数的 ID 列，NeuroCard 将二进制整数按 \(N\)-bit（如 10-bit，词表大小仅 \(1024\)）切分成高位子列 `subcol1` 与低位子列 `subcol2` 依次放入自回归链。遇到范围过滤如 `col < 1,000,000`（其中 \(1{,}000{,}000 = 976 \times 1024 + 576\)）时，先限制高位 `subcol1 <= 976` 抽样；随后在并行的 \(S\) 条路径上按刚抽出的高位值分流——**只有那些高位恰好抽中边界值 `976` 的路径，才把低位约束收紧为 `subcol2 < 576`；其余抽中 `< 976` 的路径本身已经严格满足原不等式，低位直接填 `MASK` 通配跳过！**

> "If the drawn subcol1 is 976, then the filter on subcol2 is set to “< 576”; otherwise, the high-bits already satisfy the original filter so a wildcard is placed on the low-bits subcolumn." *(PDF p. 7)*

用一段向量化伪代码把 `col < 1,000,000`（按 10-bit 切分为 `hi = col >> 10` 与 `lo = col & 1023`，其中 \(1{,}000{,}000 = 976 \times 1024 + 576\)）的动态掩码流转写出来，就能看清它如何在完全不损失原谓词精度的前提下压缩词表维度：

```python
# NeuroCard 10-bit 列拆位后的批量动态范围掩码（S 条采样路径并行）
# Step 1: 高位子列约束为 hi <= 976，按渐进采样抽出每条路径的高位值 s_hi
valid_hi = probs_hi * (arange(1024) <= 976)
z_hi     = valid_hi.sum(dim=-1)
w        = w * z_hi
s_hi     = multinomial(valid_hi / z_hi[:, None])     # shape: (S,)

# Step 2: 逐路径分流！仅当 s_hi == 976 时需继续约束 lo < 576；若 s_hi < 976，低位任意取值均合法！
on_boundary = (s_hi == 976)
# 对内部路径 (s_hi < 976) 直接注入 MASK 通配符跳过低位条件抽样，边界路径正常计算 z_lo
probs_lo = ar_model.forward_subcol_lo(prefix_with(s_hi, mask_where=~on_boundary))
valid_lo = where(on_boundary[:, None], probs_lo * (arange(1024) < 576), probs_lo)
w        = w * valid_lo.sum(dim=-1)                  # 内部路径 sum == 1.0，权重自动保持不变
```

跨三个难度递进基准的实测数据（PDF p. 9–10 `Table 2–4`）既展示了惊人的精度跨越，也诚实标出了模型体积随 Schema 复杂度增长的真实代价：

| 评测基准与连接规模 | 模型配置与体积 | Median Q-error | 95th Q-error | Max Q-error | 对比参照表现 |
| :--- | :---: | :---: | :---: | :---: | :--- |
| **`JOB-light`**（6 表，70 条等值与范围连接） | ResMADE (`3.8 MB`) | **`1.57`** | **`5.91`** | **`8.51`** | PostgreSQL 最大误差 \(> 10^3\)；单表 AR 独立拼接 `p95` 达 \(\sim 9\times 10^4\) |
| **`JOB-light-ranges`**（6 表，1,000 条密集多列范围连接） | Base (`4.1 MB`) / Large (`21 MB`) | `1.87` (Large) | `57.1` (Large) | `8,169` / **`1,029`** | 次优深度与采样式跨表方法最大误差普遍在 \(10^4\sim 10^5\) |
| **`JOB-M`**（16 表，2–11 次连接，含 `IN`/`LIKE`） | Base (`27.3 MB`) / Transformer (`409 MB`) | `2.25` (Large) | — | \(\sim 2\times 10^4\) / **`874`** | 覆盖多对多雪花连接图；增量抽 7 万样本微调仅需 **约 `3 s`**（全量重训 `3 min`） |

可以看到，NeuroCard 并没有变魔术般地消灭维度灾难：当查询从 6 表简单过滤迈向 16 表、`LIKE` 字符串与宽范围组合时，要把尾部最大误差压进千倍以内，模型体积也必须从 `3.8 MB` 扩充到 `409 MB`；而且对于同一张表多次出现的自连接（Self-Join）、含环连接图（Cyclic Join）以及无 GPU 的纯 CPU 超低延迟编译路径，全外连接单树采样仍需辅以图展开或混合代价回退机制。

---

## 六、工程选型启示与参考文献基线

把这二十年的七篇代表作连成一条演进轴，我们会发现三条路线在现代云原生与企业级数据库中并不是“后浪淘汰前浪”的替代关系，而是按**延迟预算与不确定性烈度**分层协同的防御纵深：

1. **常规亚秒级编译底座（`ASC` + 柱状图/列组统计 + `SQL Profile`）**：95% 以上的日常查询靠后台自治维护的单列统计、高频相关列组（Column Group）以及针对核心慢 SQL 固化的非侵入式选择率画像，就能在亚毫秒内拿到足够稳健的执行计划，且完全不需要 GPU 或额外推理算力。
2. **Shuffle 边界运行时兜底（务实派 Plan-based `AQP`）**：无论静态或学习型估计器多聪明，遇到 UDF 黑盒过滤、未见过的极端倾斜键或跨源联邦表，编译期估计总有失手的时候。现代引擎（如 Spark AQE、Snowflake、SQL Server）吸收了 Babu & Bizarro 指出的教训，放弃高风险的算子内逐元组路由，专门在 Shuffle / 物化落盘点核对真实字节数与行数，动态把大表连接切成 Broadcast Join 或拆分倾斜分区，以极低的防震荡成本兜住灾难性长尾。
3. **重度相关连接子图的深度模型赋能（`Naru` / `NeuroCard`）**：在星型/雪花型数仓的核心无环连接热区，轻量级自回归或因子化神经估计器正逐步以异步编译插件或代价顾问（类似 `ATO` 的后台画像生成器）的形态切入——无需把全库每一次毫秒级 OLTP 编译都绑在 GPU 上，只须为高价值分析负载提供精准的多维与跨表联合选择率校准，就能撬动巨大的执行计划收益。

### 参考文献与阅读基线

本文精读的 7 篇论文均基于 [`lonng/db-papers`](https://github.com/lonng/db-papers/tree/4ff1331069c7cf52ac97dd769e5f484c1c35c200) 仓库基线（commit `4ff1331`）核对原始 PDF 页码、公式与实验图表：

| 序号 | 锚点 | 论文题目与作者 | 发表会议/期刊 | 原文链接 |
| :---: | :---: | :--- | :--- | :---: |
| 1 | `#paper-50` | *LEO – DB2’s LEarning Optimizer* (Michael Stillger, Guy Lohman, Volker Markl, Mokhtar Kandil) | VLDB 2001 | [PDF (10p)](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/probabilistic-counting/leo-%E2%80%93-db2%E2%80%99s-learning-optimizer.pdf) |
| 2 | `#paper-08` | *Automated Statistics Collection in DB2 UDB* (A. Aboulnaga, P. Haas, M. Kandil, S. Lightstone, G. Lohman, V. Markl et al.) | VLDB 2004 | [PDF (12p)](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/statistics/automated-statistics-collection-in-db2-udb.pdf) |
| 3 | `#paper-38` | *Automatic Performance Diagnosis and Tuning in Oracle* (Karl Dias, Mark Ramacher, Uri Shaft, V. Venkataramani, Graham Wood) | CIDR 2005 | [PDF (11p)](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/diagnosis-and-tuning/automatic-performance-diagnosis-and-tuning-in-oracle.pdf) |
| 4 | `#paper-39` | *Automatic SQL Tuning in Oracle 10g* (Benoit Dageville, Dinesh Das, Karl Dias, Khaled Yagoub, Mohamed Zait, Mohamed Ziauddin) | VLDB 2004 | [PDF (12p)](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/diagnosis-and-tuning/automatic-sql-tuning-in-oracle-10g.pdf) |
| 5 | `#paper-07` | *Adaptive Query Processing in the Looking Glass* (Shivnath Babu, Pedro Bizarro) | CIDR 2005 | [PDF (12p)](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/statistics/adaptive-query-processing-in-the-looking-glass.pdf) |
| 6 | `#paper-48` | *Deep Unsupervised Cardinality Estimation (Naru)* (Zongheng Yang, Eric Liang, Amog Kamsetty et al.) | PVLDB 2019 | [PDF (14p)](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/probabilistic-counting/deep-unsupervised-cardinality-estimation.pdf) |
| 7 | `#paper-51` | *NeuroCard: One Cardinality Estimator for All Tables* (Zongheng Yang, Amog Kamsetty, Sifei Luan et al.) | PVLDB 2021 | [PDF (13p)](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/probabilistic-counting/neurocard%3A-one-cardinality-estimator-for-all-tables.pdf) |
