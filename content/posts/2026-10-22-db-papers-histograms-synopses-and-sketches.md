---
title: "【Paper】数据摘要与概率计数精读：直方图、小波、FM/PCSA、Count-Min Sketch 与 Distinct Sampling"
date: 2026-10-22T12:30:00+08:00
lastmod: 2026-11-12T00:00:00+08:00
slug: "db-papers-histograms-synopses-and-sketches"
description: "从 B ≪ N 的有损压缩下界与均匀行采样在 COUNT(DISTINCT) 上的百倍失真反例出发，系统拆解直方图五维谱系与串行主化最优性、四大摘要统一误差契约、FM/PCSA 随机平均位图、Count-Min Sketch 单侧碰撞上界及 Gibbons 协调去重采样。"
draft: false
toc: true
math: true
categories: ["数据库"]
tags: ["论文精读", "直方图", "Sketch", "基数估计"]
series: ["数据库经典论文精读"]
---

> 本系列第 4/10 篇 · [数据库经典论文精读总索引]({{< relref "2026-10-22-database-papers-source-reading.md" >}}) · [上一篇：外连接重排与子查询去相关]({{< relref "2026-10-22-db-papers-outerjoin-and-subquery-unnesting.md" >}}) · [下一篇：执行反馈与深度基数估计]({{< relref "2026-10-22-db-papers-adaptive-tuning-and-learned-cardinality.md" >}})

上一篇我们解决了查询优化器的“语义合法性”——把任意复杂的关联子查询与外连接安全还原为扁平的关系代数图。然而当动态规划枚举器面对几十个语义完全等价的候选连接计划时，它必须在真正扫描数据之前回答一个定量问题：**每一个过滤谓词会留下多少行？每一次等值连接会膨胀出多少行？每一次 `GROUP BY` 或 `COUNT(DISTINCT)` 会聚出多少个不同的组？**

如果我们允许优化器把整张 \(N\) 行表（或其值域大小为 \(M\) 的完整精确频数向量 \(a = (a_1, a_2, \dots, a_M)\)）原封不动驻留在目录缓存里，基数估计根本不成问题。现实的物理约束在于：**统计信息与流式监控器的内存预算只有 \(B \ll \min(N, M)\) 个字节**——商业优化器留给单列的直方图通常只有区区 200 个桶（几千字节），而高速网络与实时数仓面对的更是无法回头重扫第二遍的增删数据流（Turnstile Stream）。把长达 \(M\) 维的向量压缩进 \(B\) 字节的有损摘要（Synopsis），立刻在信息论层面触发了不可调和的博弈：点查询关心的单点频数 \(a_i\)、范围谓词关心的区间前缀和 \(\sum_{l \le i \le r} a_i\)、等值连接关心的向量内积 \(\langle a, b \rangle\)（及其自连接二阶矩 \(F_2 = \sum_i a_i^2\)），以及去重聚合关心的零阶矩 `NDV`（\(F_0 = |\{i : a_i > 0\}|\)），各自对信息的敏感维度截然相反！

在进入六篇奠基文献之前，我们先用两个几行算式就能算清的具体反例，看看最朴素的直觉方案是如何在 `COUNT(DISTINCT)` 和等值连接大小估计上彻底崩盘的。

## 从两个具体反例看有损压缩的物理边界

### 反例一：为什么 1% 均匀行采样在 COUNT(DISTINCT) 上会酿成百倍误判？

许多工程师遇到大数据量估算时的第一反应是：“何必发明复杂的数学结构？直接对原表做 `1%` 的均匀随机行采样（Uniform Row Sampling），在样本上跑一遍查询再等比例放大不就完了？”

对于 `SUM` 或简单过滤 `COUNT(*)`，这个直觉完全成立；但只要换成去重计数 `COUNT(DISTINCT key)`，均匀行采样就会一头撞上通信复杂度下界。设两张总行数同为 \(N = 1,000,000\) 的表：
- **表 X（全唯一键）**：包含 \(1,000,000\) 个互不相同的 `user_id`，每个键恰好出现 `1` 次，真实 `NDV(X) = 1,000,000`。
- **表 Y（中度重复键）**：仅包含 \(10,000\) 个不同的 `user_id`，每个键均匀重复出现 `100` 次，真实 `NDV(Y) = 10,000`——两张表的真实基数相差整整 **\(100\) 倍**！

现在对两张表各自独立抽取 \(p = 1\%\) 的伯努利均匀行样本（期望样本量均为 \(n = 10,000\) 行）：
- 在表 X 的样本中，抽到的每一行都来自不同用户，观测到的样本不同键数 \(d_X \approx 10,000\)；
- 在表 Y 中，任意一个真实存在的键（重数 \(c = 100\)）在 \(1\%\) 采样下至少被抽中一次的概率为 \(1 - (1 - 0.01)^{100} \approx 1 - e^{-1} \approx 0.634\)，因此样本中观测到的不同键数期望为 \(d_Y \approx 10,000 \times 0.634 = 6,340\)！

再构造一张极端一点的**表 Z**：其中 `6,340` 个高频热键各出现 `156` 次（占去 `989,040` 行），另有 `10,960` 个长尾冷键各出现 `1` 次（真实 `NDV(Z) = 17,300`）；若把热键换成 `6,230` 个各出现 `158` 次、再配 `15,580` 个出现 `1` 次的冷键，样本中观测到的去重键数几乎一模一样。更致命的是，对比“全表 \(10^6\) 行全由唯一冷键组成（`NDV = 1,000,000`）”与“\(5,000\) 个热键占掉 \(99.5\%\) 行数、外加 \(5,000\) 个唯一冷键（`NDV = 10,000`）”：一个小比例行样本能看到的几乎全是高频热键的反复打卡，而占 `NDV` 绝对大头的低频单次键（Singletons）一旦漏采，你从样本内部根本无法区分“表外藏着一百万个只出现一次的新键”还是“表外全是样本里老面孔的第 101 次重复”！理论界（Charikar et al.）早已严格证明：若想靠无索引随机行采样在最坏数据分布下把 `NDV` 估计误差控制在常数倍以内，采样率必须达到 \(\Omega(N)\) 级别——也就是说，不扫全表几乎等于盲猜。

### 反例二：按属性值相邻分桶，为什么会把自连接基数砍掉一半？

再看第二个反例。假设属性列 \(X\) 取值域为 `{1, 2, 3, 4}`，四个值的真实出现频数交替高低分布：\(f = (100, 1, 100, 1)\)。我们要计算自连接 `R1 ⋈ R2 ON R1.X = R2.X` 的精确输出行数：

$$S_{\text{true}} = \sum_{k=1}^{4} f(k)^2 = 100^2 + 1^2 + 100^2 + 1^2 = 20,002$$

现在优化器只有 \(\beta = 2\) 个直方图桶的预算，每个桶记录桶内元素个数与总频数，查询时按桶内均匀假设（Uniform Frequency Assumption）用均值回填：
- **方案 A（按属性值连续分桶）**：这是大家最熟悉的区间直方图，把相邻值 `[1, 2]` 放进桶 1（总频数 `101`，均值 `50.5`），把 `[3, 4]` 放进桶 2（总频数 `101`，均值 `50.5`）。据此估算的自连接大小为：

$$\hat{S}_{\text{range}} = 2 \times 50.5^2 + 2 \times 50.5^2 = 10,201$$

明明只有 4 个不同的数、用了足足一半数量的桶，估计值却直接腰斩，相对误差接近 **`50%`**！原因在于高频峰 `100` 与低频谷 `1` 被关进了同一个桶，平方和的方差能量 \(2\sum (f_i - \bar{f})^2\) 在均值抹平瞬间灰飞烟灭。
- **方案 B（按频数大小排序后再连续分桶——串行直方图 Serial Histogram）**：打破“桶必须是值域连续区间”的思维定势，按频数高低将 `{1, 3}`（频数同为 `100`）归入桶 1，将 `{2, 4}`（频数同为 `1`）归入桶 2！此时桶内方差为零，估算出的自连接大小 \(\hat{S}_{\text{serial}} = 2 \times 100^2 + 2 \times 1^2 = 20,002\)，**误差精确归零**！当然代价也很清楚：一旦放弃值域连续性，原本只需存桶边界端点的紧凑目录就得变成显式键值映射，范围查询 `WHERE X BETWEEN 1 AND 2` 也没法直接按端点插值了。

这两个反例把数据摘要领域的全部核心矛盾摆上了桌面：**没有一种数据结构能在 \(B \ll N\) 空间内同时把范围和、点频数、连接内积与带过滤去重计数做到最优**。本篇精读的 6 篇经典论文，正是围绕这一物理边界构建起的完整知识大厦：从 `#10` 与 `#11` 奠定直方图的五维分类与主化（Majorization）方差理论，到 `#09` 统合样本、直方图、小波与 Sketch 四大家族的误差契约，再到 `#52`（FM/PCSA）、`#47`（Count-Min Sketch）与 `#49`（Distinct Sampling）分别攻克亚线性内存去重计数、单侧误差高频流点查以及**未知谓词下的条件 `COUNT(DISTINCT)`**。

---

## 直方图理论基石：五维分类谱系与串行主化最优性

在静态或周期性刷新的关系表上，直方图始终是代价优化器最核心的统计底座。下面两篇由 Yannis Ioannidis 主导的经典论文，一篇从宏观视角厘清了数十年直方图变体的统一坐标轴，另一篇则从偏序数学高度证明了等值连接估计的方差极限。

### Ioannidis (VLDB 2003)：The History of Histograms——五维正交谱系与多连接误差放大 {#paper-10}

- **论文来源**：Yannis Ioannidis, *The History of Histograms (abridged)*, VLDB 2003 · [原始 PDF（共 12 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/statistics/the-history-of-histograms.pdf)（五维分类坐标见 PDF p. 2–3；主流商用系统统计见 PDF p. 8）。

从 1980 年 System R 采用最粗糙的全域均匀假设，到 Piatetsky-Shapiro 与 Connell (1984) 引入等深直方图（Equi-depth），再到 1990 年代涌现出 V-Optimal、MaxDiff、Compressed、End-biased 等十余种命名各异的直方图，文献术语一度高度混乱。Ioannidis 在这篇受邀综述中抽丝剥茧，指明任何直方图都可以统一表示为**五个正交设计维度**唯一确定的坐标点（PDF p. 2–3）：

1. **划分规则（Partition Rule）——记号 `p(s, u)`**：这是决定桶边界的核心旋钮，内部又细分为四个子分量：
   - **划分类（Partition Class）**：无约束任意子集划分、`serial`（按某排序参数连续成桶、严禁交叉穿插），以及它的实用受限子类 **`end-biased`**（至多允许 1 个桶包含多个元素，其余 \(\beta - 1\) 个桶全都是单独隔离极高频或极低频异常值的单元素桶 Singleton Bucket）。
   - **排序参数 `s`（Sort Parameter）**：决定谁和谁算“相邻”——按属性值 `V`（天然支持范围查询）、按频数 `F`（最小化桶内频数方差），或按面积 `A`。
   - **源参数 `u`（Source Parameter）**：桶约束所衡量的物理量——频数 `F`、跨度 `S`（定义为相邻出现值间距 \(s(k) = v(k+1) - v(k)\)），以及兼顾频数高低与值域疏密的**面积（Area）** \(a(k) = f(k) \cdot s(k)\)！引入面积 `A` 的洞察非常深刻：传统桶只假设频数均匀，却不知两个相邻实数之间究竟隔着 `1` 还是 `10,000` 的空隙；把跨度乘进源参数，就能同时惩罚“频数剧烈跳变”和“值域大片留白”。
   - **划分约束 `p`（Partition Constraint）**：给定排序轴 `s` 与度量源 `u` 后的数学切分准则，主流包括四类：

| 划分约束 `p` | 桶边界数学定义（设总桶数为 \(\beta\)） | 强项与代价特征 | 典型工业采用（2003 基线） |
| :--- | :--- | :--- | :--- |
| **`equi-sum(V, S)`** | 等宽（Equi-width）：各桶值域跨度相等 | 静态分界无需排序，但单个高频尖峰即可毁掉整桶估计 | 早期轻量统计 |
| **`equi-sum(V, F)`** | 等深（Equi-depth）：各桶累计频数约等于 \(N / \beta\) | 单表范围误差稳控在桶容量级，防倾斜远胜等宽 | Oracle、Sybase |
| **`v-optimal(s, u)`** | 最小化各桶源参数的桶内加权方差总和 \(\sum_{i=1}^{\beta} n_i \sigma_i^2\) | 1D 下动态规划求全局最优（\(O(D^2\beta)\)），自连接与点频均方误差最小 | 学术黄金基准 |
| **`maxdiff(s, u)`** | 在相邻排序元素的源参数差值最大的前 \(\beta - 1\) 处切刀 | 仅需一次排序扫描即可逼近 V-Optimal，无须平方级 DP | Microsoft SQL Server（`maxdiff(V,F)`，最多 199 桶） |
| **`compressed(s, u)`** | 先把频数超过阈值的重尾元素单独装入单元素桶，剩余做 `equi-sum` | 精准剥离高频偏斜（Heavy Hitters），注意它允许多个非单元素桶，比 `end-biased` 更宽泛 | IBM DB2（`compressed(V,F)`，默认 10 单值桶 + 20 等深桶） |

2. **其余四大正交维度**：
   - **构造算法**：一维 V-Optimal 可动态规划精确求解，而二维及以上联合矩形最优划分已被证明为 NP-Hard（连特例都等价于平面矩形最优剖分），迫使多维直方图转向贪心或查询驱动自适应切分。
   - **值域近似假设（Value Approximation）**：桶内到底出现过哪些值？早期 Equi-width / Equi-depth 默认采用粗糙的**连续值假设（Continuous Value Assumption）**，把整数桶 `[1, 100]` 当成 100 个点全出现过；而 Poosala 与 Ioannidis 提出的**等间距假设（`uniform spread`）**只需在每个桶里多存一个整数 \(d_i\)（桶内真实出现的不同值个数 `NDV`），将这 \(d_i\) 个点按等步长 \((v_{\max} - v_{\min}) / (d_i - 1)\) 铺开，点查和范围基数的估计精度立即脱胎换骨！例如桶 `[1, 101]` 总频数为 `100`，但真实只出现过 `{1, 51, 101}` 这 `3` 个不同值：连续值假设会把单点频数算成 `100 / 101 ≈ 0.99`，而 `uniform spread` 精准还原出每个出现值的平均频数为 `100 / 3 ≈ 33.3`！
   - **频数近似假设（Frequency Approximation）**与**误差保证**：几乎所有商用直方图都在桶内采用均值频数回填（等于桶总频数除以 \(d_i\)），这也使得最小化桶内方差（`v-optimal`）与最小化点查平方误差天然同构。

综述中最振聋发聩的警钟敲在多表连接误差传递上（PDF p. 3）：哪怕单表基数估计只有温和的相对偏差，一旦进入包含 \(J\) 次连接的复杂分析查询，局部误差在乘法级联下就会按连接次数呈**指数级爆炸（Exponential Error Propagation）**：

> "In particular, it was shown that, under some rather general conditions, in the worst case, errors propagate exponentially in the query size (i.e., in the number of joins), removing any hope for high-quality estimates for large multi-join queries." *(PDF p. 3)*

为了缓解跨列独立性假设失效，论文还横向梳理了多维直方图的三条核心演进路线（PDF p. 5–6）：

| 多维直方图代表作 | 空间划分拓扑 | 构造方式与数据来源 | 核心机制与代价权衡 |
| :--- | :--- | :--- | :--- |
| **`MHIST-2` (Poosala & Ioannidis)** | 两两不交的超矩形树（层次递归二分） | 离线扫描联合频数表 | 每步贪心挑选**边际分布最急需切分（MaxDiff 最大）**的单个维度一刀两断，显著优于多维等深交替切分 |
| **`GENHIST` (Gunopulos et al.)** | 允许高密度小桶叠在大背景桶之上（重叠覆盖） | 离线多趟网格密度识别 | 重叠区的估计密度等于各覆盖桶密度之和，用少量桶即可刻画斜向相关带，但需多趟扫描原表 |
| **`STHoles` (Bruno, Chaudhuri & Gravano)** | 父桶内递归“挖洞”嵌套子桶（扣洞后**有效区域严格不交**！） | **零离线扫描**，纯靠查询执行流逐算子反馈行数在线演化 | 遇到高误差子区间即从父桶分裂出子洞桶并扣减父桶计数与体积，合并相似兄弟桶防超预算 |

而一旦离开静态磁盘表、步入只能单遍过目的高速数据流场景，传统离线多遍直方图更是从根基上失去立足之地，直接催生了后文的小波与线性 Sketch：

> "Moreover, in a data stream environment, static histograms are not an option at all, as there is no opportunity to store the incoming data or examine it more than once." *(PDF p. 6)*

### Ioannidis (VLDB 1993)：Universality of Serial Histograms——主化理论与等值连接方差最优性 {#paper-11}

- **论文来源**：Yannis E. Ioannidis, *Universality of Serial Histograms*, VLDB 1993 · [原始 PDF（共 12 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/statistics/universality-of-serial-histograms.pdf)（Zipf 分布见 `Figure 1` PDF p. 4；10 表连接对比见 `Example 4.2` PDF p. 8；串行重组直观图见 `Figure 3` PDF p. 9；零期望偏差反例见 `Example 6.1` PDF p. 11）。

在前文的 4 元素反例中，我们看到按频数排序连续分桶的串行直方图（Serial Histogram，即排序参数为频数 `F` 而非属性值 `V`）把单次自连接误差降到了零。Ioannidis 与 Christodoulakis 在 1991–1993 年初证明了它在 2 表连接（1-join）最坏情况下的最优性；但真正的硬骨头在于：**如果是一棵包含 10 张甚至 \(N\) 张表的任意树状多表等值连接（Tree Equality-Join），且表内多列之间还存在函数依赖或强相关性（违反属性独立假设），串行直方图还能保持全局最优吗？**

Ioannidis 在这篇单人署名力作中给出了漂亮的肯定证明：

> "Specifically, serial histograms are shown to be optimal for arbitrary tree equality-join queries when the query result size is maximized, whether or not the attribute independence assumption holds, and when the query result size is minimized and the attribute independence assumption holds." *(PDF p. 1)*

论文首先把任意树状等值连接（通过引入双向函数依赖 \(a_j \leftrightarrow a_{j+1}\) 将多关系同键团规范化为链状 `t-clique` 查询）的真实连接基数 \(S\) 改写为**联合频数矩阵乘积的元素总和**（`Theorem 2.3`，PDF p. 4）：

$$S = \mathbf{1}^T \cdot Z_0 Z_1 Z_2 \cdots Z_N \cdot \mathbf{1}$$

其中对于同时参与两次相邻连接的关系 \(R_j(a_j, a_{j+1})\)，\(Z_j\) 直接定义为二维联合频数矩阵（第 \((u, w)\) 项记录 \((a_j=v_u, a_{j+1}=v_w)\) 的真实共现行数，无需假设两列独立！）；若某处存在函数依赖 \(a_j \to a_{j+1}\)，矩阵每行至多只有一个非零元；两端关系则为向量或经双向 FD 提升的对角矩阵。

给定直方图分桶后，优化器只知道每个桶的桶内平均频数，却丢失了具体哪个属性值对应哪个真实频数。因此在与该直方图一致的所有可能真实世界集合 \(\mathcal{W}_H\) 中，真实连接基数落在区间 \([S_{\min}(H), S_{\max}(H)]\) 内，任何确定性点估计在最坏情况下的最大偏离误差至少为半区间宽度 \((S_{\max} - S_{\min}) / 2\)，或相对偏离函数 \(g(S_{\max}, S')\)。要让最坏误差最小，就等价于在固定桶结构规模下**让上界 \(S_{\max}\) 尽可能压低、让下界 \(S_{\min}\) 尽可能抬高**！

证明的数学武器是组合分析中的**主化偏序（Majorization）**：对于两个降序排列的 \(M\) 维实向量 \(a, b\)，若对一切前缀 \(1 \le k < M\) 都有 \(\sum_{i=1}^k a_i \ge \sum_{i=1}^k b_i\) 且全和相等 \(\sum_{i=1}^M a_i = \sum_{i=1}^M b_i\)，则称 \(a\) 主化 \(b\)（记作 \(a \succ b\)，直观含义是 \(a\) 的贫富分化比 \(b\) 更剧烈，而桶内取均值恰恰是一种抹平贫富差距的“罗宾汉平均”操作）。作者由此建立两条漂亮的偏序链（`Theorem 3.1 / 3.2`，PDF p. 5–6）：

1. 根据 Hardy-Littlewood-Pólya 重排不等式，要让矩阵连乘和 \(S\) 达到最坏上界 \(S_{\max}\)，各关系必须将最大频数与最大频数同序对齐配对（全降序同向排列）；要压至下界 \(S_{\min}\)（属性独立时），则将最大频数与最小频数反向错配。
2. 对于任意一个把高低频穿插混装的非串行直方图 \(H_{\text{non}}\)，只要保持每个桶的元素个数 \(n_i\) 不变、重新按真实频数降序连续切分成**串行直方图 \(H_{\text{ser}}\)**（`Figure 3`，PDF p. 9），由于大数全聚进大桶、小数全聚进小桶，必定满足主化关系：

$$f_{\text{true}} \;\succ\; \bar{f}(H_{\text{ser}}) \;\succ\; \bar{f}(H_{\text{non}})$$

由 Schur 凸函数性质（\(\sum x_i^2\) 及同序内积均为严格 Schur 凸），夹在真实向量与非串行向量之间的 \(\bar{f}(H_{\text{ser}})\) 离真实极值最近，从而严格锁定最小最坏偏差与最小方差！`Theorem 4.2` 由此证得：**无论表内属性是否独立，最大化树状等值连接结果 \(S\) 时，必然存在由清一色串行直方图组成的全局最优直方图元组**；`Theorem 5.4` 进一步证得在属性独立假设下最小化 \(S\) 时，全串行直方图同样达到全局最优。

看一眼论文 `Example 4.2`（PDF p. 8）在 \(D = 100\) 个取值、Zipf 偏斜参数 \(z = 0.2\) 的 **10 表连接查询**上算出的真实相对误差对比，差距令人咋舌：

| 直方图方案（每关系桶结构固定） | 桶划分方式简述 | 10 表等值连接相对误差 |
| :--- | :--- | :--- |
| **`Trivial`（平凡单桶）** | 全表退化为 1 个均匀桶（System R 默认假设） | **`57.39%`** |
| **`Nonserial`（5 桶非串行）** | 将高低频元素交错分入 `{2, 2, 2, 2, 92}` 五个桶 | **`54.99%`**（建了 5 个桶却几乎没比单桶强！） |
| **`Serial-(a)`（5 桶串行）** | 按频数降序连续切分为 `{2, 2, 2, 2, 92}`（隔离前 8 个高频尖峰） | **`17.16%`** |
| **`Serial-(b)`（5 桶均匀串行）** | 按频数降序连续切分为 `{14, 15, 19, 25, 27}` | **`11.56%`**（误差立降近 5 倍！） |

论文第 6 节（`Theorem 6.1` 与 `Example 6.1`，PDF p. 10–11）还顺手戳破了一个极具迷惑性的统计学陷阱：如果我们固定直方图桶统计量，在所有满足函数依赖的频数排列上求**有符号误差的数学期望**，代数展开竟然恒等于零：

> "Simple algebraic manipulations show that their sum is equal to the sum of the first two parentheses in (4)... Hence, E[S] - E[S'] = 0." *(PDF p. 11)*

“期望无偏（\(\mathbb{E}[S - S'] = 0\)）”能不能说明随便怎么分桶都准？绝对不能！作者给出两值玩具反例：关系 \(R_0, R_2\) 的频数为 `{1, 5}`，中间表 \(R_1\) 的两对值频数为 `{2, 10}`，统一用单桶均值估计永远输出 `72`；但在真实排列中，同向对齐时 \(S = 1\cdot 2\cdot 1 + 5\cdot 10\cdot 5 = 252\)（低估 `180`），反向对齐时 \(S = 1\cdot 10\cdot 5 + 5\cdot 2\cdot 1 = 60\)（高估 `12`）——正负误差在概率加权下碰巧完美抵消成了 `0`，可每一次单次查询的实际绝对误差 \(\mathbb{E}[|S - S'|]\) 却大得惊人！这也再次印证了优化器为什么必须死磕由串行直方图主导的**二阶方差（V-Optimality）**而非一阶无偏性。当然，工程上完整维护带显式键值目录的纯串行直方图开销高昂，因此工业界最终折中演化出了前文 `#10` 里的 **`end-biased` / `compressed` 直方图**：只花十几个单元素桶把最致命的高频尖峰按频数单独拎出来（吸收串行直方图 80% 的降方差红利），其余平滑尾部依然按属性值连续做等深或 MaxDiff 分桶以兼顾范围查询。

---

## 跨越静态与流式边界：四大摘要家族的统一误差契约

当数据规模膨胀至 TB/PB 级或演变为分布式实时流时，单靠直方图已无法覆盖所有近似查询处理（AQP）与优化需求。下图将数据库领域的四大核心数据摘要家族置于同一张“更新约束—查询目标—误差契约”路由图下：

![数据摘要四大家族谱系与误差契约路由](/images/diagrams/paper-sketch-synopsis-taxonomy.svg)

### Cormode, Garofalakis, Haas & Jermaine (FnTDB 2011)：Synopses for Massive Data——四大摘要的统一误差契约 {#paper-09}

- **论文来源**：Graham Cormode, Minos Garofalakis, Peter J. Haas, Chris Jermaine, *Synopses for Massive Data: Samples, Histograms, Wavelets, Sketches*, Foundations and Trends in Databases, Vol. 4, Nos. 1–3, 2011 (© 2012) · [原始 PDF（共 296 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/statistics/synopses-for-massive-data%3A-samples%2C-histograms%2C-wavelets%2C-sketches.pdf)（等深直方图范围界见 `Fig. 3.2` PDF p. 76；V-Optimal DP 见 PDF p. 103–104；Haar 小波反例见 `Table 4.1` PDF p. 158；KMV 去重示意参见 `Fig. 5.11` PDF p. 251）。

这部近三百页的权威长篇专著由四位分别深耕随机采样、小波直方图与流式 Sketch 的领军学者合著。全书最大的理论贡献，是撕掉了各门派各自为政的术语外衣，用**线性可合并性（Linear Mergeability）、更新流模型（Cash-Register 纯增流 vs Turnstile 增删流）以及误差归一化基准（\(L_1\)、\(L_2\) 还是相对误差）**统一审视四大摘要的真实能力边界：

#### 1. 随机采样（Samples）：Horvitz-Thompson 方差耦合与连接独立性幻觉
任何不等概率采样估计器的基石都是 **Horvitz-Thompson (HT) 估计量**：若元素 \(i\) 被纳入样本 \(S\) 的边际概率为 \(\pi_i > 0\)，则总和 \(Y = \sum_{i=1}^N y_i\) 的无偏估计为 \(\hat{Y} = \sum_{i \in S} y_i / \pi_i\)。专著一针见血地指出采样在复杂算子下失效的数学病根：

> "The key point here is that, whereas E[Y] only depends on the individual inclusion probabilities pi, the variance of Y depends on the joint inclusion probabilities pij, which can be challenging to calculate." *(PDF p. 29)*

这句话直接解释了为什么**两个独立均匀样本的连接不再是连接结果的均匀样本**（PDF p. 45）。不妨现场算一笔精确方差账：设对表 \(R\) 和表 \(S\) 各取 \(p = 10\%\) 的独立伯努利样本做连接 `Sample(R) ⋈ Sample(S)`，并乘上 \(1/p^2 = 100\) 做无偏放大。对比真实连接输出同为 **`100` 行**的两种极端拓扑：
- **拓扑甲（一对一均匀匹配）**：\(R\) 中 `100` 条不同元组各自匹配 \(S\) 中 `1` 条不同元组。任意两条输出行完全不共享基表硬币，联合包含概率 \(\pi_{ij} = p^4 = 0.0001 = \pi_i \pi_j\)（两两独立！），HT 方差为 \(100 \times (1/0.01 - 1) = 9,900\)（标准差约 `99.5`）。
- **拓扑乙（一对百星型倾斜）**：\(R\) 中仅 **1 条**核心元组 \(r_1\) 匹配了 \(S\) 中 `100` 条元组 \(\{s_1, \dots, s_{100}\}\)。单条输出行的边际入选概率仍是 \(\pi_i = p^2 = 0.01\)（期望照样等于 `100` 无偏！）；但任意两条输出行 \((r_1, s_u)\) 与 \((r_1, s_v)\) 共享同一个左表硬币 \(r_1\)，联合包含概率跃升了十倍：

$$\pi_{ij} = \Pr[r_1 \in S_R \land s_u \in S_S \land s_v \in S_S] = p^3 = 0.001 \;\gg\; \pi_i \pi_j = 0.0001$$

代入 HT 方差公式 \(\text{Var}(\hat{Y}) = \sum_i (\frac{1}{\pi_i} - 1) + \sum_{i \neq j} (\frac{\pi_{ij}}{\pi_i \pi_j} - 1)\)，交叉协方差项瞬间灌入 \(100 \times 99 \times (\frac{0.001}{0.0001} - 1) = 89,100\)，总方差飙升至 **`99,000`**（标准差超过 `314`，\(90\%\) 的运行直接给出 **`0` 行**，剩下 \(10\%\) 暴吐近 **`1,000` 行**）！在流式单表维护侧，经典**水塘采样（Reservoir Sampling）**以 \(n / i\) 的递减概率接纳第 \(i\) 个到达元素并随机替换水塘旧槽位，仅扫一遍即可维持精确固定容量 \(n\) 的均匀无放回样本；若遇到任意删除，则需升级为基于可逆计数的增删水塘变体。

#### 2. 直方图（Histograms）：确定性跨界误差与动态规划前缀和加速
在等深直方图（每桶累计频数 \(N / B\)）上回答任意一维范围计数查询 \([l, r]\) 时，完全落入区间内部的桶贡献精确计数，误差**只可能来自首尾最多两个被区间端点劈开的跨界桶**（`Fig. 3.2`，PDF p. 76），因此无需任何分布假设即可给出确定性加性误差上界 \(\le 2 \cdot (N / B)\)（若用桶半值折中估计更可收紧至 \(N / B\)）。而对于一维 **V-Optimal 直方图**，令 \(P[k] = \sum_{i=1}^k f_i\) 与 \(PP[k] = \sum_{i=1}^k f_i^2\) 为预计算的一阶与二阶前缀和数组，单桶区间 \([i, j]\) 的平方误差代价：

$$\text{SSE}(i, j) = \big(PP[j] - PP[i-1]\big) - \frac{\big(P[j] - P[i-1]\big)^2}{j - i + 1}$$

可在 \(O(1)\) 时间内直接查表算出，从而通过标准动态规划递推：

$$D[b, j] = \min_{1 \le i \le j} \Big\{ D[b-1, i-1] + \text{SSE}(i, j) \Big\}$$

在 \(O(M^2 B)\) 时间与 \(O(MB)\) 空间内求得全局精确最优解（PDF p. 103–104）。以四点频数数组 \(f = [10, 12, 50, 52]\)、桶预算 \(B = 2\) 为例：预计算得 \(P = [10, 22, 72, 124]\)，\(PP = [100, 244, 2744, 5448]\)。枚举切分点 \(i \in \{2, 3, 4\}\)：
- 切在 \(i = 2\)（即 `[10]` 与 `[12, 50, 52]`）：\(\text{SSE}(1,1) + \text{SSE}(2,4) = 0 + (5348 - 114^2/3) = 1016\)；
- 切在 \(i = 3\)（即 `[10, 12]` 与 `[50, 52]`）：\(\text{SSE}(1,2) + \text{SSE}(3,4) = (244 - 22^2/2) + (2704 - 102^2/2) = 2 + 2 = \mathbf{4}\)！
- 切在 \(i = 4\)（即 `[10, 12, 50]` 与 `[52]`）：\(\text{SSE}(1,3) + \text{SSE}(4,4) = (2744 - 72^2/3) + 0 = 1016\)。

DP 毫不犹豫地锁死 \(i = 3\)，将全局平方误差从上千压缩到 `4`（进一步还可用 \((1+\epsilon)\) 稀疏单调切分将其加速至近线性时间）。专著同时提醒（PDF p. 86）：当为省全表扫描开销而在随机样本上构造等深或 V-Optimal 直方图时，桶边界自身会叠加采样涨落，确定性误差界必须退化为带置信度的概率界。

#### 3. 小波摘要（Wavelets）：Parseval 全局 \(L_2\) 最优与单点查询失真的反差
Haar 小波对长度为 \(M = 2^k\) 的频数数组做自底向上的多分辨率成对求平均与求半差值（Detail Coefficients），树高为 \(\log_2 M\)。不妨用 4 元素数组 `a = [2, 2, 0, 4]` 手算一遍两层分解：
- **第 1 层（相邻成对）**：前半对 `[2, 2]` 均值为 `2`、半差为 `(2 - 2)/2 = 0`；后半对 `[0, 4]` 均值为 `2`、半差为 `(0 - 4)/2 = -2`；
- **第 0 层（全局根）**：对上一层均值 `[2, 2]` 再求均值得到全局均值 `2`，半差为 `(2 - 2)/2 = 0`。

最终 4 个原始频数被无损转换为小波系数向量 \(W_a = [2, 0, 0, -2]\)（4 个系数里天然有 2 个是零！）。一般地，只要对分辨率第 \(\ell\) 层（\(\ell = 0\) 为根层）的细节系数除以 \(\sqrt{2^{k - \ell}}\)（等价于自底向上乘以支撑集半宽因子），Haar 小波基就构成一组标准正交基：

$$\|a - \hat{a}\|_2^2 = \sum_{i=0}^{M-1} (a_i - \hat{a}_i)^2 \;=\; \sum_{j \notin \text{Top-}B} (c_j^*)^2$$

根据上式（**Parseval 定理**），原向量与重构向量的全局平方误差总和严格等于被丢弃的归一化小波系数 \(c_j^*\) 的平方和——因此**贪心保留绝对值最大的前 \(B\) 个归一化系数，在所有 \(B\) 项小波截断中天然达到全局 \(L_2\) 误差最优**，且任意单点重构只需沿小波树根到叶路径累加 \(\log_2 M + 1\) 项，任意范围和只需在左右端点路径累加至多 \(2\log_2 M\) 项！

然而专著在 `Table 4.1`（PDF p. 158）用一组 16 元素频数数组的重构实验，给了盲目迷信“全局 \(L_2\) 最优”的读者一记猛醒：

> "Conventional wavelet synopses optimized for overall L2 error may not always be the best choice for AQP systems. The quality of the approximate answers such synopses provide can vary widely, and users have no way of knowing the accuracy of any particular answer." *(PDF p. 157)*

在那组 16 个元素的实例中，保留最大的 8 个归一化系数后，区间 `[0, 2]` 的真实和是 **`285`**，区间 `[3, 5]` 的真实和只有 **`93`**（相差三倍多），可小波重构对这两个区间给出的估计值竟然**分毫不差全都是 `195`**！因为局部中等幅度的细节系数被一刀切置零后，误差集中倾泻在了特定子树上。若业务要求控制单点最大相对误差或最大绝对误差，就必须放弃贪心截断，改用在小波树上分配空间配额的动态规划或随机取整（Randomized Rounding）构造。

#### 4. 线性素描（Sketches）：可加同态映射与三大误差标尺
面对任意分布式分片合并与高频增删流（General Turnstile Stream，允许元素计数加减但通常要求最终频数非负或有界），只有满足**线性同态性 \(S(a + \Delta) = S(a) + S(\Delta)\)** 的随机投影 Sketch 能够以每次更新 \(O(\log(1/\delta))\) 的微小代价自由加减汇总。专著清晰对比了频数矩家族的误差基准差异：
- **Count-Min Sketch**（详拆见下节 `#47`）：空间 \(O(\epsilon^{-1} \log(1/\delta))\)，误差标尺为 \(\epsilon \|a\|_1\)（一阶总流长）；
- **Count Sketch (Charikar et al.)**：在哈希进桶的同时再乘一个随机正负号 \(\xi_j(i) \in \{-1, +1\}\) 抵消碰撞偏置，中位数聚合下空间增至 \(O(\epsilon^{-2} \log(1/\delta))\)，但误差标尺收紧为二阶范数 \(\epsilon \|a\|_2 = \epsilon \sqrt{F_2}\)——当分布极度平缓或已剥离头部尖峰时，\(\|a\|_2 \ll \|a\|_1\)，精度远超 CMS；
- **AMS Sketch (Alon-Matias-Szegedy)**：利用 **4-wise 独立**随机符号函数 \(\xi: \{1..M\} \to \{-1, +1\}\) 维护单个线性计数器 \(Z = \sum_i \xi(i) a_i\)。为什么两两独立不够、非要 4-wise 独立？展开平方期望与方差便一清二楚：
  - 一阶无偏只需两两独立（\(\mathbb{E}[\xi(i)\xi(j)] = 0, i\neq j\)）：

$$\mathbb{E}[Z^2] = \sum_i a_i^2 \underbrace{\mathbb{E}[\xi(i)^2]}_{=1} + \sum_{i \neq j} a_i a_j \underbrace{\mathbb{E}[\xi(i)\xi(j)]}_{=0} = \sum_i a_i^2 = F_2$$

  - 但要压住 \(Z^2\) 的方差 \(\mathbb{E}[Z^4] - (\mathbb{E}[Z^2])^2\)，展开四次乘积 \(\xi(i)\xi(j)\xi(k)\xi(\ell)\) 时会出现形如 \(a_i^2 a_j b_k\) 或四元组异号交叉项——若只有两两独立，四个不同下标的乘积期望未必为零！唯有 **4-wise 独立**才能把所有非成对四次交叉项全部归零，锁定 \(\text{Var}(Z^2) = 2\sum_{i\neq j} a_i^2 a_j^2 \le 2 F_2^2\)，进而在 \(O(\epsilon^{-2}\log(1/\delta))\) 空间内无偏估计自连接大小 \(F_2\) 与两表连接内积 \(\langle a, b \rangle\)（误差 \(\epsilon \|a\|_2 \|b\|_2\)）。
- **零阶矩去重摘要对比（KMV vs 位图族，PDF p. 251–258）**：保留归一化哈希值 \(h(x) \in [0, 1]\) 最小前 \(k\) 个不同键的 **KMV（\(k\)-Minimum Values，`Fig. 5.11`）**利用第 \(k\) 小顺序统计量 \(U_{(k)}\) 的期望 \(\mathbb{E}[U_{(k)}] = k / (\text{NDV} + 1)\)，给出无偏化估计 \(\widehat{\text{NDV}} = (k - 1) / U_{(k)}\)（相对标准误约 \(1/\sqrt{k-2}\)）。专著强调没有通吃一切的 `NDV` 摘要：位图/寄存器族在纯粹单列 `NDV` 字节效率上遥遥领先，而 KMV 因为手里攥着真实的最小哈希键值集合，天然支持多集合并集、交集与 Jaccard 相似度的低误差复合估算！

---

## 亚线性概率计数与条件去重采样：从前导零位图到协调蓄水池

理解了宏观版图后，我们深入拆解流式与海量统计中最具创造力的三座里程碑算法——它们分别用精妙的概率不变量解决了一个看似不可能的工程极限。下图并列展示了三套引擎内部的哈希路由、内存状态机与查询解码公式：

![概率计数与去重采样三大引擎：FM/PCSA、Count-Min Sketch 与 Distinct Sampling](/images/diagrams/paper-sketch-fm-cms-distinct-sampling.svg)

### Flajolet & Martin (JCSS 1985)：Probabilistic Counting (FM / PCSA)——前导零位图与随机平均对数内存计数 {#paper-52}

- **论文来源**：Philippe Flajolet, G. Nigel Martin, *Probabilistic Counting Algorithms for Data Base Applications*, Journal of Computer and System Sciences 31(2), 1985, pp. 182–209 · [原始 PDF（共 28 页）](https://algo.inria.fr/flajolet/Publications/FlMa85.pdf)（`PCSA` 伪代码与误差表见 `FIG. 1` & `TABLE II` PDF p. 16；位图长度界见 PDF p. 21；Unix `man` 模拟实验见 `TABLE III / IV` PDF p. 23）。

回到本文开篇的第一个死局：如何在不建哈希表存键、也不排全表序（内存严格远小于 \(N\)）的前提下，单遍扫描精确估算 `COUNT(DISTINCT)`？

Flajolet 与 Martin 从第一性原理抓住了一个唯有去重计数才需要的代数性质——**幂等性（Idempotence）**：同一个键值 \(x\) 无论重复到达 1 次还是 100 万次，它对摘要状态的修改结果必须完全相同！确定性哈希函数 \(h(x) \in \{0, 1\}^L\) 恰好把任意输入幂等地映射为一条伪随机均匀比特串。定义一个比特串 \(y\) 的秩 \(\rho(y)\) 为其**最低有效位（最右侧）首个 `1` 出现的 0 起始下标**（例如 \(\rho(\dots\texttt{1}) = 0\)，\(\rho(\dots\texttt{10}) = 1\)，\(\rho(\dots\texttt{1000}) = 3\)）。在均匀哈希下，任意不同键命中 \(\rho(h(x)) = k\) 的概率严格呈几何衰减 \(2^{-(k+1)}\)：平均每见到 \(2\) 个不同键会出现一次 \(\rho=0\)，每见到 \(1024\) 个不同键才会出现一次 \(\rho=9\)！

我们维护一个长度仅为 \(L\) 比特（32 位足够覆盖数十亿基数）的位图 `BITMAP[0..L-1]`，初始全 `0`；每来一条记录 \(x\)，将 `BITMAP[ρ(h(x))]` 置为 `1`。扫完全表后，我们该读位图上的哪个统计量来反推不同键总数 \(n\)？很多二手资料误以为初代 FM 算法也是读“最高置 `1` 位（Max Rank）”，但精读 1985 年原典会发现，Flajolet 刻意放弃了波动偏大的最高位 \(P_n\)，而是选择读取 **`BITMAP` 中最左侧首个仍为 `0` 的下标 \(R_n\)（即最小未见秩）**！借助 Mellin 变换与复分析泊松化方法，作者在 `Theorem 1` 中算出了 \(R_n\) 令人惊叹的渐近分布特征：

$$\mathbb{E}[R_n] = \log_2(\varphi n) + P(\log_2 n) + o(1), \qquad \varphi \approx 0.77351, \qquad \sigma(R_n) \approx 1.12127$$

其中周期摆动项 \(|P(u)| < 10^{-5}\) 小到在工程双精度下几乎无法察觉，且标准差 \(\sigma(R_n) \approx 1.12\) 比最高置 `1` 位的标准差（约 \(1.87\)）收窄了近四成！注意这里必须除以修正因子 \(\varphi \approx 0.77351\)——如果漏掉 \(\varphi\) 直接算 \(2^{R_n}\)，估计值就会系统性偏低约 \(23\%\)。

然而单个位图的 \(R_n\) 终究有约 \(1.12\) 比特的标准差，指数还原成 \(2^{R_n} / \varphi\) 后单次估计会有两倍左右的抖动。如果硬建 \(m\) 个独立哈希函数对每条记录各算 \(m\) 次哈希求平均，CPU 开销立刻暴涨 \(m\) 倍！为了用 **每次仅 1 次哈希、\(O(1)\) 更新时间** 换取 \(\sqrt{m}\) 倍的误差收敛，论文提出了直接启发后世 LogLog 与 HyperLogLog 分桶架构的神来之笔——**随机平均（Stochastic Averaging，即 `PCSA` 算法）**：

> "It turns out that an effect very similar to straight averaging may be achieved by a device that we call stochastic averaging. The idea consists in using the hashing function in order to distribute each record into one of m lots, computing a = h(x) mod m." *(PDF p. 15)*

具体操作简洁得如同艺术品（`FIG. 1`，PDF p. 16）：取 \(m = 2^p\) 个位图（例如 \(m = 64\) 或 \(256\)）。每来一条记录 \(x\)，只算一次哈希 \(h(x)\)，用几行位运算即可完成更新与解码：

```c
// PCSA 单遍流式更新（每个元素仅需 1 次哈希与 2 条位指令！）
uint32_t hv    = hash32(x);
uint32_t alpha = hv & (m - 1);          // 低 p 位决定分桶号 α = h(x) mod m
uint32_t quot  = hv >> p;               // 高位商 q = h(x) div m
int      rank  = __builtin_ctz(quot);   // ρ(q)：最低有效位首个 1 的 0 起始下标
BITMAP[alpha] |= (1u << rank);          // 幂等置位，天然支持重复与并行 OR 合并

// 查询解码：统计每桶最左 0 位下标 R[j]（即最低未见秩），对数均值后指数还原
double sum_R = 0.0;
for (int j = 0; j < m; ++j)
    sum_R += __builtin_ctz(~BITMAP[j]); // 取反后 ctz 恰为最左 0 的下标！
double est_ndv = ((double)m / 0.77351) * pow(2.0, sum_R / m);
```

这样 \(n\) 个不同元素被均匀分流进 \(m\) 个子桶，每桶期望承接 \(n / m\) 个不同元素。查询解码时，先在**对数尺度上**求 \(m\) 个位图最左零位下标的算术均值，再做一次指数还原并乘回总桶数 \(m / \varphi\)：

$$\hat{n}_{\text{PCSA}} = \left\lfloor \frac{m}{\varphi} \cdot 2^{\frac{1}{m}\sum_{j=0}^{m-1} R^{(j)}} \right\rfloor$$

为什么必须先对 \(R^{(j)}\) 求均值再取 \(2^{(\cdot)}\)（等价于单桶基数估计的几何平均），而不能先算 \(2^{R^{(j)}}\) 再求算术平均？因为指数放大后的重尾偶发大值会严重拉高算术平均的方差与偏置！`Theorem 2` 严格推导出 `PCSA` 的乘性偏置仅为 \(1 + 0.31/m + o(1/m)\)，渐近相对标准误差为：

$$\text{RSE}(\hat{n}_{\text{PCSA}}) \approx \frac{0.78}{\sqrt{m}}$$

对照论文 `TABLE II`（PDF p. 16）列出的典型分桶配置，内存与精度的兑换比一目了然：

| 子位图数 \(m\) | 32 位字总内存开销 | 理论乘性偏置 \(1 + 0.31/m\) | 渐近相对标准误 \(\text{RSE}\) | 典型适用场景 |
| :--- | :--- | :--- | :--- | :--- |
| **`m = 16`** | `64 Bytes` | `1.019`（`+1.9%`） | **`19.5%`** | 极窄内存槽位下的粗粒度基数数量级感知 |
| **`m = 64`** | `256 Bytes` | `1.005`（`+0.5%`） | **`9.7%`** | 数据库单列常规统计（论文推荐甜点值） |
| **`m = 256`** | `1,024 Bytes` (`1 KB`) | `1.001`（`+0.1%`） | **`4.8%`** | 高精度优化器编目与跨分片 `COUNT(DISTINCT)` |
| **`m = 1024`** | `4,096 Bytes` (`4 KB`) | `1.0003` | **`2.4%`** | 近似报表仪表盘与实时流监控 |

关于位图字长 \(L\) 的选取，作者给出精准上界（PDF p. 21）：

> "Since the probability distribution of the R-parameter has a very steep distribution, it suffices to select L in such a way that L > log2(n/nmap) + 4." *(PDF p. 21)*

甚至连这 \(L\) 个比特都可以再压缩（PDF p. 21–22）：因为随着不同元素不断涌入，每个位图的低位很快全变成实心的 `111...1`、高位全保持全空的 `000...0`，真正决定 \(R^{(j)}\) 的信息只集中在 `1` 与 `0` 交界处宽约 8 比特的狭窄过渡带（Fringe）上！因此每个子桶只需维护：
- 一个宽度为 \(\lceil \log_2 L \rceil \approx 5\) 比特的**左边界基址指针 `base`**（记录连续全 `1` 前缀已推进到第几位）；
- 一个固定 `8` 比特的**滑动窗口位图**（一旦窗口最低位变为 `1`，立即右移窗口并递增 `base`）。

这一招直接把单桶空间从 \(O(\log N)\) 比特压到了理论下界级的 **\(O(\log \log N)\) 比特**（总空间 \(O(m \log \log N)\) 比特）——它正是后来 Durand & Flajolet (2003) 提出只存最高位下标 5 比特寄存器、并用调和平均压制重尾离群值的 **LogLog / HyperLogLog** 的直接母体！此外，由于位图更新纯属单调置 `1`，跨节点分布式并行扫描后的 `PCSA` 摘要只需做按位或（Bitwise `OR`）即可**零精度损失合并**。工程落地时只需留意两处机制边界：
- **小基数非线性区**：当每桶平均不同键数过少（\(n / m \lesssim 10\text{–}20\)，大量子桶仍处于全零状态）时，渐近指数公式会出现显著正偏置，需切换原文给出的小基数修正表或退回空桶计数（Linear Counting）；
- **删除流扩展**：单比特位图一旦置 `1` 便无法区分该位是由 1 个键还是 50 个不同键共同点亮的，若需支持增删流（Turnstile），须按论文 PDF p. 24 方案将每个比特位替换为小宽度饱和引用计数器（增流 `+1`、删流 `-1`）。

### Cormode & Muthukrishnan (J. Algorithms 2005)：Count-Min Sketch——单侧哈希碰撞噪声与线性流摘要 {#paper-47}

- **论文来源**：Graham Cormode, S. Muthukrishnan, *An Improved Data Stream Summary: The Count-Min Sketch and its Applications*, Journal of Algorithms 55(1), 2005, pp. 58–75 · [原始 PDF（共 18 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/probabilistic-counting/an-improved-data-stream-summary%3A-the-count-min-sketch-and-its-applications%2C-journal-of-algorithms.pdf)（点查与内积证明见 PDF p. 6–9；二进区间树见 PDF p. 10–11；复杂度汇总见 `Table 1` PDF p. 15）。

`PCSA` 完美解决了零阶矩 `NDV`，但如果我们想知道某个具体热点键 \(i\) 到底出现了多少次（点频数 \(a_i\)）、哪些键占了总流量的 `1%` 以上（Heavy Hitters），或者流数据的分位数（Quantiles）呢？早期 AMS 与 Count Sketch 依赖 4-wise 独立哈希或正负号对冲加上 Chernoff 集中不等式，不仅空间关于 \(1/\epsilon\) 是平方级 \(O(\epsilon^{-2})\)，大 \(O\) 记号里隐藏的常数因子更是高达 `256` 以上。

Cormode 与 Muthukrishnan 回归最朴素的第一性原理问了一个问题：**既然在非负频数流（\(a_k \ge 0\)）里，哈希碰撞只会把别人的正频数误加到我的桶里（永远只增不减），那我干嘛还要引入随机正负号去制造双侧震荡？直接多建几行独立哈希表，查询时取所有行的最小值（`Min`），那个被别人污染得最轻的桶不就是最干净的紧上界吗？**

让我们在白板上用短短四行初等概率论，完整复现 **Count-Min Sketch (`CMS`)** 那段堪称教科书典范的证明（`Theorem 1`，PDF p. 7–8）：
1. **结构与更新**：分配一个 \(d\) 行 \(w\) 列的非负整数计数器矩阵 \(\text{count}[1..d, 1..w]\)，其中列宽取 \(w = \lceil e / \epsilon \rceil\)（\(e \approx 2.718\) 为自然底数；注：原文 PDF p. 7 正文有一处明显的印刷笔误将 \(w = b/\epsilon\) 印成了 `w = \epsilon / b`，对照紧随其后的期望上界推导即可确认应为 \(w = \lceil e/\epsilon \rceil\)），行数取 \(d = \lceil \ln(1/\delta) \rceil\)。每行配备一个仅需**两两独立（Pairwise Independent，如极快的一次模乘加 `(a*x + b) mod p mod w`）**的哈希函数 \(h_j: \{1..n\} \to \{1..w\}\)。收到更新 \((i_t, c_t)\) 时，对每一行执行 \(\text{count}[j, h_j(i_t)] \mathrel{+}= c_t\)。
2. **碰撞噪声拆解**：当底层状态满足全向量非负 \(a_k \ge 0\)（涵盖纯增流以及不出现负库存的严格增删流 Strict Turnstile）时，第 \(j\) 行对应桶的读数等于真实频数加上同桶其他键的碰撞噪声 \(X_{i,j}\)：

$$\text{count}[j, h_j(i)] = a_i + X_{i,j}, \qquad X_{i,j} = \sum_{k \neq i} \mathbb{I}\big[h_j(k) = h_j(i)\big] \cdot a_k \;\ge\; 0$$

3. **两两独立期望 + Markov 不等式**：利用两两独立性，\(\Pr[h_j(k) = h_j(i)] \le 1/w \le \epsilon / e\)，由期望线性性立得：

$$\mathbb{E}[X_{i,j}] = \sum_{k \neq i} \Pr\big[h_j(k) = h_j(i)\big] \cdot a_k \;\le\; \frac{1}{w} \sum_{k=1}^{n} a_k = \frac{\|a\|_1}{w} \;\le\; \frac{\epsilon \|a\|_1}{e}$$

因为 \(X_{i,j} \ge 0\) 非负，直接套用最基础的 **Markov 不等式**，单行碰撞噪声越界超标的坏事件概率严格不超过 \(1/e\)：

$$\Pr\big[X_{i,j} > \epsilon \|a\|_1\big] \;\le\; \frac{\mathbb{E}[X_{i,j}]}{\epsilon \|a\|_1} \;\le\; \frac{1}{e}$$

4. **取 `Min` 指数压低失效率**：输出 \(\hat{a}_i = \min_{1 \le j \le d} \text{count}[j, h_j(i)]\)。由于每一行都有 \(\text{count}[j, h_j(i)] \ge a_i\)，下界 \(\hat{a}_i \ge a_i\) **以 100% 确定性恒成立（单侧误差！）**；而 \(\hat{a}_i > a_i + \epsilon \|a\|_1\) 当且仅当全部 \(d\) 个相互独立的哈希行**同时**发生严重碰撞，其概率至多为 \((1/e)^d \le e^{-\ln(1/\delta)} = \delta\)！

$$\Pr\Big[a_i \;\le\; \hat{a}_i \;\le\; a_i + \epsilon \|a\|_1\Big] \;\ge\; 1 - \delta$$

不妨看一个 \(d = 3, w = 4\) 的微缩快照：设真实流中包含热键 `A: 100`、`B: 80` 以及目标查询键 **`C: 5`**。
- 在第 1 行哈希下，`C` 不幸与热键 `A` 撞进同一桶，桶读数为 `5 + 100 = 105`；
- 在第 2 行哈希下，`C` 又与热键 `B` 撞进同一桶，桶读数为 `5 + 80 = 85`；
- 在第 3 行哈希下，`C` 独占空桶，桶读数干净地保持 **`5`**！

查询 `C` 时只需执行 \(\min(105, 85, 5) = 5\)，前两行上百倍的正向碰撞噪声瞬间被第 3 行的干净桶一票否决！全程没有用到任何高阶矩或 Chernoff 界，隐藏常数因子从 `256+` 骤降到 **`e ≈ 2.72 < 3`**：

> "This allows a simple calculation of the failure probability, without recourse to Chernoff bounds. This significantly improves the constants involved: in [5], for example, the constant factors within the big-Oh notation is at least 256; here, the constant factor is less than 3." *(PDF p. 8)*

> "The error bound here is one-sided, as opposed to all previous constructions which gave two-sided errors. This brings benefits for many applications which use sketches." *(PDF p. 8)*

当然，单侧 `Min` 聚合的生命线在于 \(X_{i,j} \ge 0\)：如果输入是允许最终分量取负值的广义有符号向量（General Cash/Turnstile with negative entries），某一行碰上一大堆负数碰撞就会把 \(\min_j\) 拽到远低于真实值 \(a_i\) 的深渊！此时论文 `Theorem 2`（PDF p. 8）证明必须改取各行读数的**中位数（`Median`）**（设单行越界概率 \(< 1/8\)，用 Chernoff 界控制超过半数行出错的概率），代价是失去单侧确定性下界，退回 \(a_i \pm 3\epsilon \|a\|_1\) 的双侧概率保证。

凭借极简的线性矩阵结构，`CMS` 一口气解锁了三项核心数据库统计任务（PDF p. 9–15，`Table 1`）：

- **两表连接大小 / 内积估计 \(\langle a, b \rangle\)**：对每行计算同构哈希桶的逐位乘积和：

$$\widehat{\langle a, b \rangle}_j = \sum_{k=1}^{w} \text{count}_a[j, k]\cdot \text{count}_b[j, k] = \langle a, b \rangle + \sum_{\substack{p \neq q \\ h_j(p) = h_j(q)}} a_p b_q$$

非负交叉碰撞同样支持直接取 \(\min_{1 \le j \le d}\)，仅用 \(O(\epsilon^{-1}\log(1/\delta))\) 空间就给出 \(\langle a, b \rangle + \epsilon \|a\|_1 \|b\|_1\) 的单侧上界！

- **二进区间分层（Dyadic Ranges）求一维范围和与 \(\epsilon\)-分位数**：如果在长区间 \([l, r]\) 上逐点调用 \(r - l + 1\) 次 `CMS` 求和，加性误差会按区间长度放大成 \((r - l + 1)\epsilon \|a\|_1\) 而彻底失效。作者在值域 \(\{1..n\}\) 上建立 \(\log_2 n + 1\) 层二进区间树（第 \(\ell\) 层跟踪粒度为 \(2^\ell\) 的对齐块 \([k\cdot 2^\ell + 1,\, (k+1)\cdot 2^\ell]\)），每层各放一个精度缩放为 \(\epsilon' = \epsilon / (2\log_2 n)\)、失效率缩放为 \(\delta' = \delta / (2\log_2 n)\) 的 `CMS`。由于任意连续范围 \([l, r]\) 都可以贪心拆分为**至多 \(2\log_2 n\) 个互不相交的二进对齐块**——例如在值域 `[1..16]` 上，长度为 13 的区间 `[2, 14]` 只需拆成 5 个二进块：

$$[2, 14] \;=\; [2, 2] \;\cup\; [3, 4] \;\cup\; [5, 8] \;\cup\; [9, 12] \;\cup\; [13, 14]$$

将这 \(\le 2\log_2 n\) 个子块的 `CMS` 单侧估值相加，总过估计被严丝合缝地锁死在 \(2\log_2 n \cdot \epsilon' \|a\|_1 = \epsilon \|a\|_1\) 以内！有了任意前缀和 \([1, x]\) 的 \(\epsilon \|a\|_1\) 近似，只需在值域上做一次 \(\log_2 n\) 步二分查找（寻找首个前缀和跨过 \(\phi \|a\|_1\) 的位置），即可在流式增删环境下算出 \(\epsilon\)-近似分位数！

- **高频重尾项（\(\phi\)-Heavy Hitters）**：在纯增流中维护总计数 \(N\) 与容量 \(O(1/\phi)\) 的最小堆，新元素更新 `CMS` 后若 \(\hat{a}_i \ge \phi N\) 即入堆；在增删流中则复用上述二进区间树自顶向下做阈值剪枝搜索，凡分块估值低于 \(\phi \|a\|_1\) 的子树整枝丢弃。

将论文 `Table 1`（PDF p. 15）对比既有最优流式算法的空间复杂度浓缩如下（隐去多项式对数低阶项时差异尤为醒目）：

| 流式近似统计问题 | 既有 Sketch 最优空间（AMS / Count Sketch 等） | Count-Min Sketch 空间复杂度 | 关键突破点 |
| :--- | :--- | :--- | :--- |
| **点频数与内积估计** | \(O\big(\frac{1}{\epsilon^2}\log\frac{1}{\delta}\big)\)（双侧误差） | **\(O\big(\frac{1}{\epsilon}\log\frac{1}{\delta}\big)\)**（单侧误差） | \(1/\epsilon\) 从二次方降至一次方，常数因子 `< 3` |
| **一维范围和与 \(\epsilon\)-分位数** | \(O\big(\frac{1}{\epsilon^2}\log^2 n \log\frac{\log n}{\delta}\big)\) 或仅支持纯增流 | **\(O\big(\frac{1}{\epsilon}\log^2 n \log\frac{\log n}{\delta}\big)\)** | 原生支持增删流（Strict Turnstile）分位数二分定位 |
| **增删流 \(\phi\)-Heavy Hitters** | \(O\big(\frac{1}{\phi^2}\log n \log\frac{\log n}{\delta}\big)\) | **\(O\big(\frac{1}{\phi}\log n \log\frac{n}{\phi\delta}\big)\)** | 二进树剪枝将对阈值 \(\phi\) 的依赖压回线性反比 |

也要清醒把握 `CMS` 的物理软肋：它的误差标尺始终锚定在全流一阶范数 \(\epsilon \|a\|_1\) 上——如果查询的是一个真实频数只有 `5` 的极冷门长尾键，而总流长 \(\|a\|_1 = 10^8\)、\(\epsilon = 10^{-4}\)，那么 \(\epsilon \|a\|_1 = 10,000\) 的加性宽容度对高频热键毫发无伤，对冷门小键却是足以淹没真值的白噪声。

### Gibbons (VLDB 2001)：Distinct Sampling——哈希层级协调准入与带谓词条件去重采样 {#paper-49}

- **论文来源**：Phillip B. Gibbons, *Distinct Sampling for Highly-Accurate Answers to Distinct Values Queries and Event Reports*, VLDB 2001 · [原始 PDF（共 10 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/probabilistic-counting/distinct-sampling-for-highly-accurate-answers-to-distinct-values-queries-and-event-reports.pdf)（精度与误差对比见 `Figure 5–10` & `Table 1` PDF p. 7–8；呼叫中心实测见 `Table 2` PDF p. 9）。

读到这里，细心的读者一定会发现一个巨大的实战缺口：真实业务报表极少只跑光秃秃的全表 `SELECT COUNT(DISTINCT customer_id) FROM orders`；分析师写得最多的是**带临时过滤谓词的条件去重查询**：

```sql
SELECT COUNT(DISTINCT customer_id)
FROM   orders
WHERE  region = 'APAC'
  AND  order_date >= '2026-10-01'
  AND  discount > 0.20;
```

请注意，构建离线摘要或维护流式样本时，我们**根本无法预知未来用户会在 `WHERE` 里写哪几列的什么组合条件**！此时回头检视前面的武器库：
- `FM / PCSA / HyperLogLog` 在哈希入桶瞬间就已经把 `region`、`order_date`、`discount` 等同行其他列信息抹得一干二净（你不可能为指数级组合的每个潜在谓词各建一个 Sketch！）；
- 而传统均匀行采样虽然保留了完整元组列，却又像本文开篇“反例一”证明的那样，被高频大客户占满样本槽位，漏光了所有只下过一单的长尾客户！

贝尔实验室的 Phillip Gibbons 在 VLDB 2001 提出的 **Distinct Sampling（去重协调采样）**，用一套极其优雅的**双层协调蓄水池结构**同时兼顾了这两头——既像 `FM` 位图一样按不同键值均匀抽样（彻底免疫键值频数偏斜），又完整保留每个入选键的代表性原始数据行以供事后任意 `WHERE` 谓词过滤！

在给定的最大内存行槽位预算 \(B\) 下，单遍扫描全表维护去重样本 `S` 与当前全局准入层级 \(\ell\)（初始 \(\ell = 0\)）的核心机制分两层协同运转（PDF p. 3–5）：

1. **第一层：基于确定性 `die-hash` 的键值一致性准入（跨行协调硬币！）**
   借用 `FM` 算法的几何哈希思想，定义一个只依赖目标列键值 \(v\) 的确定性哈希函数 \(\text{die-hash}(v) \in \{0, 1, \dots\}\)，使得任意不同键命中层级 \(\ge k\) 的概率恰为 \(2^{-k}\)。每扫到表中的一行元组 \(r\)（其目标列取值为 \(v\)）：
   - 若 \(\text{die-hash}(v) < \ell\)：直接忽略跳过；
   - 若 \(\text{die-hash}(v) \ge \ell\)：**该键值获得准入资格**！因为 `die-hash(v)` 是键 \(v\) 的确定性函数，同一个客户 `cust_42` 无论在表里分散出现 1 次还是 10,000 次，它每次算出的层级都一模一样——**要么它的历史记录全都有资格进候选池，要么一次都不进，绝不独立抛硬币**！
   - **批量升级淘汰**：一旦样本总占槽数突破预算 \(B\)，将所有恰处于最低层级 `die-hash(v) == ℓ` 的键及其关联行整批驱逐，并将门槛层级自增 \(\ell \leftarrow \ell + 1\)。这一步在任意时刻严格维持如下核心不变量：

> "The invariant we maintain throughout is that S contains all the distinct values appearing in the scanned portion of the table whose die-level is at least l, and no other distinct values. Because levels for distinct values are chosen at random, S contains a uniform sample of the distinct values in the scanned portion." *(PDF p. 4)*

2. **第二层：每键定长水塘 `valSampSize = t` 与 `dummy` 计数行（防止单个大户撑爆内存！）**
   光有第一层还不够：万一某个幸运跨过门槛 \(\ell\) 的批发商客户名下有 `500,000` 条订单，把他的行全存下来瞬间就会把整个预算 \(B\) 挤爆、迫使 \(\ell\) 疯狂升高而杀光其他键！因此 Gibbons 为每个入选的不同键值 \(v\) 设定单键样本上限 **`valSampSize`（记作 \(t\)）**：
   - 当键 \(v\) 累计出现次数 \(c_v \le t\) 时，原封不动保留它的全部 \(c_v\) 条完整元组（单次长尾键一行不丢！）；
   - 一旦出现第 \(t+1\) 次（\(c_v > t\)），立即为键 \(v\) 切换为容量为 \(t\) 的**键内均匀水塘采样**（第 \(c_v\) 次到达时以 \(t / c_v\) 概率随机替换该键已存的 \(t\) 行之一），并在样本槽中额外挂入 1 条不参与查询输出的 **`dummy` 元数据行**专门记录精确累计数 \(c_v\)（否则后续无法算准水塘替换概率 \(t / c_v\)；该 `dummy` 行同样计入总槽位预算 \(B\)）。

查询执行时的解码逻辑清爽至极：扫描样本 `S` 中所有非 `dummy` 的真实元组，原样评估用户的任意 `WHERE P(...)` 谓词，统计通过过滤的不同目标键值个数 \(d_{\text{pass}}\)，最后直接乘以层级放大因子输出无偏估计：

$$\widehat{\text{NDV}}(P) = d_{\text{pass}} \cdot 2^{\ell}$$

用一张总预算 \(B = 4\) 槽、单键水塘上限 \(t = 2\) 的 5 步流式追踪表，就能把整套协调状态机看得明明白白（设确定性哈希给出 \(\text{die-hash}(u_1) = 1\)、\(\text{die-hash}(u_2) = 0\)、\(\text{die-hash}(u_3) = 2\)）：

| 到达记录 | 准入门槛 \(\ell\) | 协调动作与槽位变化 | 当前样本池 `S`（最多 \(B=4\) 槽） |
| :--- | :--- | :--- | :--- |
| **`(u1, 'APAC')`** | `ℓ = 0` | `level(u1)=1 >= 0`，接纳第 1 行 | `{(u1, 'APAC')}`（1 槽） |
| **`(u1, 'EMEA')`** | `ℓ = 0` | `u1` 第 2 行，`c_u1 = 2 <= t`，直接接纳 | `{(u1,'APAC'), (u1,'EMEA')}`（2 槽） |
| **`(u1, 'APAC')`** | `ℓ = 0` | `u1` 第 3 行（`c_u1=3 > t`）！以 `2/3` 概率水塘替换，并挂入 `dummy` 计数槽 | `{(u1,'APAC'), (u1,'APAC'), dummy(u1, cnt=3)}`（3 槽） |
| **`(u2, 'APAC')`** | `ℓ = 0` | `level(u2)=0 >= 0`，接纳后恰满 4 槽 | 上述 3 槽 `+ {(u2, 'APAC')}`（4 槽满） |
| **`(u3, 'APAC')`** | `ℓ = 0 → 1` | `level(u3)=2 >= 0` 触发溢出！整批驱逐所有 `level==0` 的键（`u2` 出局），门槛升为 **`ℓ = 1`** | `u1` 的 3 槽 `+ {(u3, 'APAC')}`（4 槽） |

此时若用户发起查询 `SELECT COUNT(DISTINCT user) FROM orders WHERE region = 'APAC'`：引擎忽略 `dummy(u1, cnt=3)`，在真实样本行中测得 `u1` 和 `u3` 两个不同键值满足 `region = 'APAC'`（\(d_{\text{pass}} = 2\)），乘上 \(2^{\ell} = 2^1 = 2\) 输出估计值 **`4`**！既没有让连刷 3 单的热户 `u1` 挤死罕见键 `u3`，又保留了 `region` 列供事后过滤。

参数 \(t\) 的取舍直指空间分配的物理本质：如果已知未来所有查询都没有非目标列过滤（或谓词只写在目标列自己身上），完全可以把 \(t\) 设为 `1`，把 100% 的内存槽位都用来装尽可能多的不同键：

> "For queries without predicates, or queries with predicates only on the target attribute, the valSampSize t could be set to 1, resulting in the best performance for distinct sampling on such queries, because the available storage would be devoted entirely to distinct values (and not to multiple rows with the same value)." *(PDF p. 5)*

反之，当查询带有非目标列条件 `P` 时，误差来源于两处耦合选择率（`Theorem 2`，PDF p. 5–6）：
- **键间目标选择率 \(q_{\text{tar}}\)**：真实满足谓词的不同键占总 `NDV` 的比例；
- **键内行选择率 \(q_{\text{val}}(v)\)**：某个真实满足条件的键 \(v\) 名下，真正满足 `P` 的行占该键总行数 \(c_v\) 的比例！

算一笔账就明白键内漏检风险有多真实：倘若某个大客户共下了 `100` 单，其中只有 `1` 单用了八折优惠券（\(q_{\text{val}}(v) = 0.01\)），而我们的单键水塘只留了 \(t = 5\) 行，那么这位真实合规的客户在样本里恰好漏掉那唯一一张优惠订单、被误判为不满足谓词的概率高达 \(1 - 5/100 = 95\%\)！定理证明，只要针对关心的最低选择率下限 \((q_{\text{tar}}^*, q_{\text{val}}^*)\) 取单键水塘容量 \(t = \Theta\big(\ln(1/\delta) / q_{\text{val}}^*\big)\)、总预算 \(B = \Theta\big(t \ln(1/\delta) / (q_{\text{tar}}^* \epsilon^2)\big)\)（工程经验默认值取 \(t = \min(\lceil 2/q \rceil, B/50)\)，如支持最低 `2%` 谓词选择率时取 \(t = 100\)），就能以 \(1 - \delta\) 置信度保证相对误差不超过 \(\epsilon\)！

实验数据极具说服力（PDF p. 7–9）：在合成偏斜数据与百万级记录的真实电信呼叫中心日志上，传统 `GEE`、`AE` 均匀行样本估计器相对误差动辄高达 `70%–3,000%`，而 Distinct Sampling 全程将误差稳稳压在 **`0%–10%`** 区间内；在复杂的多属性关联会话报表（Event Reports）上，仅用 `1%` 样本空间即将查询耗时从 `216` 秒压缩至 **`2` 秒**，而在内存受限导致原表精确排序发生严重磁盘颠簸（耗时 `5 小时 26 分钟`）的场景下，当 \(t = 25\) 覆盖单客户最大会话长度时，`5%` 去重样本仅用 **`10` 秒**就输出了几乎零误差的报表！唯一需要留意的维护边界是高频删除：增量插入可在线直接走双层准入，但若大量删除导致留存样本稀疏萎缩，则需触发后台重扫以回降层级 \(\ell\)。

---

## 全景对比：六篇经典文献的选型坐标与误差契约

把这六篇横跨二十六年的代表作放在同一张工程决策表上，我们可以清晰看到每一类摘要是用什么代价换取了哪一项统计不变量：

| 论文与结构 | 核心空间复杂度 | 单次更新 / 构造代价 | 最擅长的目标算子 | 误差契约形式 | 关键失效机制与工程边界 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **[#10](#paper-10) Ioannidis (2003)**<br>直方图五维谱系 `p(s,u)` | \(\beta\) 个桶（通常百字节至数 KB） | 离线排序扫描；1D V-Optimal 为 \(O(D^2\beta)\)，MaxDiff 为 \(O(N\log N)\) | 单列范围选择率、点查与代价优化器基数传递 | 等深范围误差 \(\le 2N/\beta\)；多连接最坏误差随连接数指数放大 | 多维最优划分 NP-Hard；静态直方图无法在无落盘单遍流上直接构造 |
| **[#11](#paper-11) Ioannidis (1993)**<br>串行直方图 `Serial` | \(\beta\) 个频数桶 + 显式键值归属映射 | 需按频数 `F` 排序聚类（工业常退化为 `end-biased` / `compressed`） | 任意树状多表等值连接基数上下界与均方误差 | 主化偏序证明最大化 \(S\)（无论独立与否）及独立最小化 \(S\) 全局最优 | 破坏值域区间连续性，纯串行结构无法直接插值范围谓词；期望零偏掩盖高方差 |
| **[#09](#paper-09) Cormode et al. (2011)**<br>四大摘要统一综述 | 样本 \(O(n)\)；小波 \(O(B)\)；Count/AMS \(O(\epsilon^{-2}\log\frac{1}{\delta})\) | 水塘/Sketch 单遍流式更新；Haar 小波分解 \(O(M)\) | 覆盖聚合和、范围和、\(F_2\) 内积与流式近似查询 (AQP) | HT 依赖联合包含概率 \(\pi_{ij}\)；Count/AMS 达 \(\epsilon\|a\|_2\) 级双侧界 | 独立行样本直接做 Join 因共享基表元组破坏独立性；小波全局 \(L_2\) 最优不保单点精度 |
| **[#52](#paper-52) Flajolet & Martin (1985)**<br>`FM` / `PCSA` 位图 | \(m\) 个 \(L\) 比特位图（滑动窗极限 \(O(m\log\log N)\) 比特） | 每次仅 1 次哈希分桶置位，\(O(1)\) 时间，支持按位 `OR` 并行合并 | 全表或单维度超大规模去重计数 `COUNT(DISTINCT)` (\(F_0\)) | 渐近相对标准误 \(\approx 0.78/\sqrt{m}\)，偏置 \(1 + 0.31/m\) | 丢弃非目标列故不支持事后任意 `WHERE` 过滤；\(n/m \lesssim 20\) 需小基数非线性修正；原生不支持删除 |
| **[#47](#paper-47) Cormode & Muthukrishnan (2005)**<br>`Count-Min Sketch` | \(w=\lceil e/\epsilon\rceil, d=\lceil\ln\frac{1}{\delta}\rceil\)，总计 \(O(\epsilon^{-1}\log\frac{1}{\delta})\) 计数器 | \(d\) 次两两独立哈希累加，\(O(\log\frac{1}{\delta})\)，原生支持线性加减合并 | 高频点查、Heavy Hitters、内积、二进区间范围和与流式分位数 | 非负向量下 `Min` 单侧保证 \(a_i \le \hat{a}_i \le a_i + \epsilon\|a\|_1\)（概率 \(\ge 1-\delta\)） | 误差正比于总流长 \(\|a\|_1\)，长尾低频键易被碰撞噪声淹没；出现负分量时须改用 `Median` 双侧界 |
| **[#49](#paper-49) Gibbons (2001)**<br>`Distinct Sampling` | 固定行槽位预算 \(B = \Theta\big(\frac{t\ln(1/\delta)}{q_{\text{tar}}^*\epsilon^2}\big)\) | 单遍扫描协调 `die-hash` 层级准入 + 键内水塘替换 | **带事后未知 `WHERE` 谓词的条件 `COUNT(DISTINCT)`** 与复杂会话报表 | 以 \(1-\delta\) 概率达相对误差 \(\le \epsilon\)（需 \(t = \Theta(\ln(1/\delta)/q_{\text{val}}^*)\)） | 若键内满足谓词的行占比极低（\(q_{\text{val}} \ll 1/t\)）会产生漏检低估；大量删除后需重扫回降层级 \(\ell\) |

站在现代数据库系统的架构高度回望，这六篇论文共同勾勒出了一条极其清晰的工程分工准则：
- 在**优化器编目层（Catalog Statistics）**，现代引擎（PostgreSQL、MySQL 8.0、Oracle、SQL Server、DuckDB）普遍采用 `#10` 与 `#11` 结晶出的混合形态——用一张**高频单值列表（MCV / Top-\(N\) Singletons）**充当轻量级串行桶精确锁死头部连接方差，用**等深或 MaxDiff 直方图**覆盖平滑尾部范围选择率，再配一个仅占 `1–2 KB`、源自 `#52` 思想的 **HyperLogLog 寄存器阵列**在单遍 `ANALYZE` 中给出毫秒级全列 `NDV`；
- 在**流处理、引擎自适应算子与可观测监控层**，`#47` 的 **Count-Min Sketch** 凭借 \(<3\) 的极小常数因子与单侧不低估特性，成为实时识别倾斜连接热键（Skewed Join Heavy Hitters）与缓存准入（TinyLFU）的首选组件；
- 而在**交互式 BI 近似分析（AQP）与预计算物化层**，一旦业务需要支持多维切片下钻后的快速去重指标，`#49` 的**哈希层级协调去重采样**（及其现代演化版 Theta Sketch / Tuple Sketch）依然是唯一能突破均匀行采样失真魔咒的理论正途。

但故事到这里还没有结束：哪怕我们把每张单表的直方图、MCV 和 HLL 都建到了理论极致，一旦查询包含跨表相关谓词或五表以上的链式连接，静态单表摘要依然会撞上 `#10` 所警告的**多连接指数级误差放大墙**。如何利用查询执行运行时的真实基数反馈修正优化器模型、乃至用深度自回归与图神经网络直接学习多表联合分布？这正是下一篇（第 5/10 篇：执行反馈与深度基数估计）即将展开的核心命题。

---

## 参考文献与阅读基线

本文精读的所有论文均对齐开源论文集 [`lonng/db-papers`](https://github.com/lonng/db-papers)（Git 基线提交 `4ff1331`，其中 `#52` 同步对齐作者公开归档原版 PDF），文中引用的页码均为对应文件内的 PDF 物理页码：

1. **#10**：Yannis Ioannidis. *The History of Histograms (abridged)*. VLDB 2003, pp. 19–30. [PDF (12 页)](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/statistics/the-history-of-histograms.pdf)
2. **#11**：Yannis E. Ioannidis. *Universality of Serial Histograms*. VLDB 1993, pp. 256–267. [PDF (12 页)](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/statistics/universality-of-serial-histograms.pdf)
3. **#09**：Graham Cormode, Minos Garofalakis, Peter J. Haas, Chris Jermaine. *Synopses for Massive Data: Samples, Histograms, Wavelets, Sketches*. Foundations and Trends in Databases, 4(1–3): 1–294, 2011 (© 2012). [PDF (296 页)](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/statistics/synopses-for-massive-data%3A-samples%2C-histograms%2C-wavelets%2C-sketches.pdf)
4. **#52**：Philippe Flajolet, G. Nigel Martin. *Probabilistic Counting Algorithms for Data Base Applications*. Journal of Computer and System Sciences, 31(2): 182–209, 1985. [PDF (28 页)](https://algo.inria.fr/flajolet/Publications/FlMa85.pdf)
5. **#47**：Graham Cormode, S. Muthukrishnan. *An Improved Data Stream Summary: The Count-Min Sketch and its Applications*. Journal of Algorithms, 55(1): 58–75, 2005. [PDF (18 页)](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/probabilistic-counting/an-improved-data-stream-summary%3A-the-count-min-sketch-and-its-applications%2C-journal-of-algorithms.pdf)
6. **#49**：Phillip B. Gibbons. *Distinct Sampling for Highly-Accurate Answers to Distinct Values Queries and Event Reports*. VLDB 2001, pp. 541–550. [PDF (10 页)](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/probabilistic-counting/distinct-sampling-for-highly-accurate-answers-to-distinct-values-queries-and-event-reports.pdf)
