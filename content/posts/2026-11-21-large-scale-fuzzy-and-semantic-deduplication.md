---
title: "PB 级大模型语料去重深潜：从 MinHash 星型建边、大星小星图计算到 SQL 混合语义去重"
slug: "large-scale-fuzzy-and-semantic-deduplication"
date: 2026-11-21T23:35:00+08:00
lastmod: 2026-11-21T23:35:00+08:00
categories:
  - AI Infra
  - 大数据
  - 分布式系统
tags:
  - MinHash
  - LSH
  - 数据去重
  - WCC
  - NeMo Curator
  - FineWeb
  - SemDeDup
  - Vector Search
  - SQL
  - LLM
description: "从第一性原理剖析 30TB 至 PB 级大模型语料模糊去重的核心瓶颈：证明完全图到星型树 O(k) 建边等价性，拆解分布式大星-小星 WCC 收敛与防倾斜调度，给出六阶段生产架构与 SLO 监控体系，结合 FineWeb、SlimPajama、Dolma、NeMo Curator 公开经验与现代 SQL+Vector/Text Search 混合去重演进，并从业务 ROI 视角反思数据去重的战略价值。"
draft: false
toc: true
math: true
---

如果你要对 10 亿篇网页文档做“精确去重”，只需给每篇文档算一个 MD5 或 SHA-256 哈希，跑一次 `GROUP BY` 就能收工。可真实互联网语料里，大量重复并不长成一模一样的字节串：同一篇新闻在不同门户网站上被塞进了不同的页眉、广告位和评论数；同一份开源代码只是改了几行注释或版权年份。

为了把这些“换汤不换药”的近似重复（Near-Duplicate）揪出来，工业界普遍采用 **MinHash + 局部敏感哈希（LSH）+ 弱连通分量（WCC）** 这一套经典管线：先将文档切成滑动窗口片段（Shingles），用 \(K\) 个随机哈希函数把数万字的正文压成几百字节的 MinHash 指纹；再把指纹拆成 \(B\) 个 Band 分桶，落入同一个 `(band_id, bucket_hash)` 桶的文档视为近似重复候选；最后把候选关系连成图，求出每个连通分量（Connected Component），每个簇只留一篇质量最高的代表。

这套流程在百万文档的实验脚本里优雅顺滑，可一旦数据规模越过 **30TB 乃至 PB 级**，工程现场几乎必然撞上两堵硬墙：

1. **热点桶的建边爆炸**：互联网抓取中充斥着海量通用模板（如“404 Not Found”、开源协议、网站免责声明）。如果某个模板桶聚集了 \(k = 10^5\) 篇文档，朴素地两两连边会瞬间炸出 \(\binom{10^5}{2} \approx 5 \times 10^9\) 条边，网络 Shuffle 与 Worker 内存当场打爆。
2. **千亿边超大规模图的 OOM 与长尾倾斜**：即便修掉了单桶爆炸，全库十亿文档叠加 20 个 Band 依然可能产生数千亿条去重边。单机并查集（Union-Find）根本装不下整张图；而在分布式集群里，图度数服从幂律分布，少数度数高达数百万的“超级节点（Supernode）”会把 Reducer 或 Ray Actor 逐个拖入 OOM 与 GC 假死。

下面先立清去重体系里最容易混淆的几个基础概念（精确、词面 Jaccard、语义 SemDeDup 与 Pairwise），再从上述两处最致命的工程瓶颈切入，逐步展开算法破局、六阶段生产架构、学术与工业界公开踩坑经验、现代 SQL 引擎融合向量/全文检索的混合演进，以及这项基建背后的商业逻辑。

---

## 零、概念坐标：精确去重、词面模糊去重（Jaccard）、语义去重与 Pairwise

在深入分布式算法前，有必要把去重领域常混在一起说的四个词放回各自正确的维度：**去重目标层（精确 / 词面 / 语义）**、**相似度度量公式（Jaccard vs. Cosine）** 与 **计算形态（Pairwise + Blocking）**。

看下面四句语义高度相关但形态迥异的文本：
- **文档 A**：`苹果昨晚发布了搭载 M5 芯片的新款 MacBook Pro。`
- **文档 B**：`苹果昨晚发布了搭载 M5 芯片的新款 MacBook Pro。（点击关注公众号）`
- **文档 C**：`昨晚，苹果正式推出配备 M5 处理器的新一代 MacBook Pro 笔记本。`
- **文档 D**：`Apple unveiled the new M5 MacBook Pro last night.`

1. **Jaccard 相似度（管的是“词面重合度”）**：
   将两篇文档切分为词块集合（Shingles）\(S_1, S_2\)，Jaccard 定义为交集与并集大小之比：
   $$\text{Jaccard}(S_1, S_2) = \frac{|S_1 \cap S_2|}{|S_1 \cup S_2|}$$
   - **A vs B** 绝大部分词块重合（Jaccard ≈ `0.85`），**MinHash + Jaccard** 轻松判重；
   - **A vs C** 同义改写了动词与名词（Jaccard ≈ `0.25`），**A vs D** 更是跨语言翻译（Jaccard = `0`）——纯词面去重必然漏判。
2. **语义去重（SemDeDup，管的是“意思相不相同”）**：
   语义去重不数重合了多少字，而是用 **Embedding 模型**（如 BGE、Qwen-Embedding 或多模态 CLIP）将文档/图像映射到高维向量空间，再计算向量夹角的**余弦相似度（Cosine Similarity）**：
   $$\cos(\mathbf{v}_1, \mathbf{v}_2) = \frac{\mathbf{v}_1 \cdot \mathbf{v}_2}{\|\mathbf{v}_1\| \|\mathbf{v}_2\|}$$
   在语义空间里，**A vs C**（Cosine ≈ `0.96`）、**A vs D**（多语言向量 Cosine ≈ `0.92`），乃至被裁剪调色的同一张图片（CLIP 向量 Cosine 接近 `1`），都能被语义去重精准捕获。
3. **Pairwise（成对比对：去重里的“两两精算动作”）**：
   只要需要把两个样本放在一起算精确分数——无论是算 **精确 Jaccard**、算 **精确 Cosine**，还是让 **LLM-as-a-Judge** 判别边界难例——针对样本对 \((d_i, d_j)\) 的计算形态统称 **Pairwise**。由于全库直接跑 Pairwise 是 \(O(N^2)\) 组合爆炸，工业界所有去重系统都遵循同一套统一范式——**先粗分桶（Blocking），再桶内 Pairwise 精算**：

| 维度 | 精确去重 | 词面模糊去重（MinHash） | 语义去重（SemDeDup） |
| :--- | :--- | :--- | :--- |
| **去重依据** | 字节完全一致 | 字面词块（Shingles）高度重合 | 文本含义或视觉语义高度一致 |
| **核心相似度指标** | MD5 / SHA-256 相等 | **Jaccard 相似度** | **向量 Cosine 相似度**（或 LLM 裁决） |
| **粗分桶方式（避开 `O(N²)`）** | `GROUP BY hash` | **MinHash + LSH Band 分桶** | **KMeans 语义聚类** 或 **ANN 向量检索** |
| **Pairwise 精算用在哪** | 不需要 | 桶内候选对做 **Pairwise 精确 Jaccard** 剔除 LSH 假阳性 | 簇内用 GPU 矩乘（`E · E^T`）做 **Pairwise 精确 Cosine** 精排 |
| **典型捕获目标** | 镜像拷贝、重复抓取 | 改页眉、插广告、改代码注释 | 同义改写、翻译搬运、图片裁剪调色 |

正是因为 MinHash 无需神经网络推理且成本极低、语义去重精度极高但 GPU 与索引成本昂贵，现代大模型数据平台普遍采用**“先词面 MinHash 粗筛干掉 80% 模板抄袭，再语义 SemDeDup 精排干掉改写与跨模态冗余”**的两级联动架构。

---

## 一、从完全图到星型树：根除 \(O(k^2)\) 建边爆炸

假设某个 LSH 桶里落入了 \(k = 10^5\) 篇文档 \(C = \{d_1, d_2, \dots, d_k\}\)。朴素建边认为“桶内任意两篇文档都相似”，于是两两连边，构成一张**完全图（Clique, \(K_k\)）**：

$$E_{\text{clique}}(C) = \{(d_i, d_j) \mid 1 \le i < j \le k\}, \quad |E_{\text{clique}}(C)| = \frac{k(k-1)}{2} = O(k^2)$$

但回到连边的原始目的：我们并不是要把桶里的每一对文档都两两介绍认识，而只是为了让后续的连通分量算法知道**这 \(k\) 篇文档同属一个连通块**。

只要在桶内选出一个全局确定性的锚点（例如文档 ID 最小的 \(d_{\min} = \arg\min_{d \in C}(d)\)），让桶内其余文档只向 \(d_{\min}\) 连一条边，就能把完全图压缩成一棵**星型生成树（Star Graph, \(S_k\)）**：

$$E_{\text{star}}(C) = \{(d_{\min}, d_i) \mid d_i \in C \setminus \{d_{\min}\}\}, \quad |E_{\text{star}}(C)| = k - 1 = O(k)$$

把每个桶的完全图替换成星型树，会不会改变最终全局的连通分量划分？答案是**严格等价**。

设全局所有桶生成的朴素图为 \(G_{\text{clique}} = (V, \bigcup_b E_{\text{clique}}(C_b))\)，星型优化图为 \(G_{\text{star}} = (V, \bigcup_b E_{\text{star}}(C_b))\)。连通分量由顶点间的可达等价关系唯一决定，因此只需证明对任意顶点 \(u, v \in V\)：

$$u \overset{G_{\text{clique}}}{\longleftrightarrow}^* v \iff u \overset{G_{\text{star}}}{\longleftrightarrow}^* v$$

- **充分性（\(G_{\text{star}} \implies G_{\text{clique}}\)）**：对任意桶 \(C\)，星型图的每一条边都天然属于完全图，即 \(E_{\text{star}}(C) \subset E_{\text{clique}}(C)\)，故 \(G_{\text{star}}\) 是 \(G_{\text{clique}}\) 的子图。子图上连通的节点对，在超图上必然连通。
- **必要性（\(G_{\text{clique}} \implies G_{\text{star}}\)）**：考察 \(G_{\text{clique}}\) 中的任意一条直连边 \((u, v)\)，它必然源自某个同时包含 \(u\) 和 \(v\) 的桶 \(C_b\)。在 \(G_{\text{star}}\) 针对桶 \(C_b\) 的子图中：若 \(u\) 或 \(v\) 恰好是锚点 \(d_{\min}\)，则边 \((u, v)\) 直接存在（1 跳连通）；若二者都不是 \(d_{\min}\)，则星型图中必然同时存在 \((d_{\min}, u)\) 与 \((d_{\min}, v)\)，构成长度为 2 的路径 \(u \leftrightarrow d_{\min} \leftrightarrow v\)（2 跳连通）。既然原图的每一条单跳边都能被星型图中的至多 2 跳路径替代，由路径连通的传递性归纳可知，原图中任意连通的节点对在星型图中依然严格连通。

对于 \(k = 10^5\) 的热点模板桶，这一步把边数从 **50 亿条** 压到了 **99,999 条**，削减幅度高达 **99.998%**，直接消除了 Shuffle 阶段的网络与内存爆炸。

---

## 二、分布式内存受限下的 WCC 收敛：大星-小星与防倾斜调度

压平单桶建边后，全库仍会留下数千亿条无向边，远超单机内存容量。在 MapReduce、Spark 或 Ray Actor Pool 上求解弱连通分量（WCC），如果直接采用朴素的“向最小邻居传播（Hash-to-Min）”，遇到一条长度为 \(D\) 的链状图，需要迭代 \(O(D)\) 轮才能收敛，跨节点通信开销难以承受。

### 2.1 交替大星-小星（Alternating Large-Star / Small-Star）规则

Rastogi 等人提出的交替大星-小星算法，通过非对称的邻居收缩规则，能把收敛轮数压到近似对数级（生产上通常 **5–10 轮** 即可全图收敛）。

记节点 \(u\) 当前的邻居集合（含自身）为 \(N(u) = \{v \mid (u, v) \in E\} \cup \{u\}\)，按节点 ID 大小将邻居拆成两半：

- **高位邻居**：\(N^+(u) = \{v \in N(u) \mid v > u\} \cup \{u\}\)
- **低位邻居**：\(N^-(u) = \{v \in N(u) \mid v \le u\}\)

每轮迭代交替执行两步：

1. **Large-Star（高位向全局最小坍缩）**：对每个节点 \(u\)，算出全体邻居中的最小值 \(m(u) = \min(N(u))\)，然后将所有大于 \(u\) 的高位邻居 \(v \in N^+(u)\) 重新连向 \(m(u)\)，发射新边 \((v, m(u))\)。这一步让高位节点跳过中间人，直接挂接到已知最小根节点上，迅速压扁树高。
2. **Small-Star（低位内部对齐，打断长链）**：对每个节点 \(u\)，仅在低位邻居中取最小值 \(m_{\text{small}}(u) = \min(N^-(u))\)，将所有小于等于 \(u\) 的低位邻居 \(v \in N^-(u)\) 连向 \(m_{\text{small}}(u)\)，发射新边 \((v, m_{\text{small}}(u))\)。这一步专门折断低位节点之间的长链，防止迭代轮数退化为图直径。

当完整执行一次大星加小星后**不再产生任何新边**（所有节点仅指向所属连通分量的全局最小 ID），算法立即收敛退出。

> **手算走一遍 4 节点链 `4 — 2 — 5 — 1`（目标根为 `1`）**：
> - **第 1 轮 Large-Star**：节点 `2` 邻居为 `{2, 4, 5}`，最小为 `2`，向高位发射 `(4, 2)`、`(5, 2)`；节点 `5` 邻居为 `{1, 2, 5}`，最小为 `1`，发射 `(5, 1)`。此时 `5` 同时连着 `2` 和 `1`。
> - **第 1 轮 Small-Star**：节点 `5` 低位邻居 `{1, 2, 5}`，最小为 `1`，发射 `(2, 1)`、`(1, 1)`；节点 `4` 低位邻居 `{2, 4}`，最小为 `2`，发射 `(2, 2)`、`(4, 2)`。
> - **第 2 轮 Large-Star**：节点 `2` 邻居已更新为 `{1, 2, 4}`，最小为 `1`，立刻将高位 `4` 直接连向 `1`（发射 `(4, 1)`）。全图 `{1, 2, 4, 5}` 全部收敛到 `1`。

### 2.2 幂律倾斜下的三层工程护栏

真实语料图的顶点度数呈极端幂律分布，少数超级节点的度数可达数百万。若按 `Hash(vertex_id) % P` 均匀分区，单个 Worker 会因拉取巨型邻居列表瞬间 OOM。生产集群必须叠加三层护栏：

| 防倾斜机制 | 核心做法 | 工程收益 |
|---|---|---|
| **Map-Side Combiner** | 在边进入网络 Shuffle 前，Mapper/Actor 本地内存先对相同 Key 求局部 \(\min(N_{\text{local}}(u))\) 并剪枝重复边 | 提前砍掉 60%–80% 的跨节点重复边传输 |
| **虚拟节点加盐（Virtual Vertex Salting）** | 当检测到节点度数 \(\deg(u) > \tau\)（如 50,000）时，将 \(u\) 逻辑拆为 \(u_0, \dots, u_{M-1}\) 分片并行求局部最小值，再做二阶段规约算出全局 \(m(u)\) 并广播回各分片 | 彻底消除超级节点打爆单机内存与 JVM Full GC |
| **加权装箱调度 + NVMe Spill** | 按分片总边数权重 \(W(P_i) = \sum_{u \in P_i} \deg(u)\) 降序做贪心装箱（Bin-Packing）分配给低水位 Worker；内存占用达 75% 强制异步溢写到本地 NVMe 盘 | 压平长尾 Straggler，以可控磁盘 I/O 换取确定性完成 |

---

## 三、生产级完整架构：六阶段端到端流水线设计

将上述算法嵌入 PB 级数据平台，完整的模糊去重流水线通常解耦为六个阶段，每一层都有明确的吞吐目标与防错边界。

![PB 级模糊去重六阶段生产流水线与星型建边优化架构](/images/diagrams/ai-fuzzy-dedup-pipeline.svg)

### Stage 1：前置脏数据拦截与零拷贝 Shingling
生产中最隐蔽的 OOM 往往源自**超短文本、空白页或机器生成的重复日志行**直接进了 MinHash。前置阶段需要拦截字符数少于 100 或信息熵极低的文本（统一转交单机精确哈希去重），随后通过 C++/Rust 扩展或 GPU Kernel 在连续内存 Buffer 上滑动提取 K-Shingle（英文/代码常用 Word 5-gram，中文常用 Char 13-gram），避免在 Python 层分配海量短字符串切片。

### Stage 2：异构并行 MinHash 签名（Classic vs. DOPH）与 LSH 阈值调优
对每篇文档生成 \(K = 128\) 维（512 字节）的 MinHash 签名，并切分为 \(B\) 个 Band、每个 Band 包含 \(R\) 行（\(K = B \times R\)）。两篇 Jaccard 相似度为 \(s\) 的文档至少在一个 Band 碰撞的概率为 \(P = 1 - (1 - s^R)^B\)，S 曲线拐点近似位于 \(s^* \approx (1/B)^{1/R}\)：

- **偏重召回（强去重）**：\(K=128, B=16, R=8 \implies s^* \approx 0.71\)
- **偏重精度（严防误杀）**：\(K=128, B=8, R=16 \implies s^* \approx 0.88\)

在具体生成这 \(K\) 维签名时，工程上存在 **Classic MinHash** 与 **DOPH（Densified One-Permutation Hashing，稠密化单置换哈希）** 两种核心算法路线——两者产出格式完全兼容下游同一套 LSH，但在计算复杂度与硬件亲和性上差异悬殊：

- **Classic MinHash（经典 `K`-Hash）**：对文档的每一个 Shingle（共 \(d\) 个）独立套用 \(K\) 个通用哈希函数 \(h_i(x) = (a_i x + b_i) \bmod p\) 并逐维取最小值，时间复杂度为 `O(K · d)`。其优势在于数学性质最干净、哪怕极短文档（\(d \ll K\)）也不存在空桶问题；短板是 CPU 算力被 `K` 倍放大。
- **DOPH（单次哈希分桶 + 最优稠密化）**：每个 Shingle 只做 **1 次** 哈希，按哈希高位分入 \(0 \dots K-1\) 中的唯一一个桶并用低位更新该桶最小值（OPH 阶段仅 `O(d)`）。针对短文档导致的空桶陷阱（严禁填 `0` 等常量，否则会引发无关短文档在 LSH 中灾难性误碰撞），Shrivastava（ICML 2014 / ICML 2017）提出**无偏稠密化**——特别是 **Optimal Densification** 对每个空桶用 2-通用哈希独立随机探测其他非空桶借值（附带探测轮次编码防伪碰撞），在保持 `O(d + K)` 单遍扫描极速的同时，实现了与 Classic MinHash 同级的碰撞概率与估计方差。

| 对比维度 | Classic MinHash | DOPH（Optimal Densification） |
| :--- | :--- | :--- |
| **时间复杂度** | `O(K · d)` | **`O(d + K)`**（CPU 单核常提速 10×–50×） |
| **极短文档（\(d \ll K\)）** | 天然无空桶、零分支、方差最优 | 空桶率 > 90% 时随机探测循环上升，强依赖 Stage 1 长度拦截 |
| **硬件亲和性** | **极佳适配 GPU**（寄存器内并行算 `K` 次线性同余，无散射写冲突）与 AVX SIMD | **极佳适配纯 CPU 集群**（Rust/C++/Spark CPU 节点单遍扫描吞吐极高）；GPU 上随机桶散射写与空桶探测易引发 Warp 分化 |

因此算力选型边界非常清晰：**纯 CPU 集群跑长文本/网页去重（已前置拦截超短文本）优先选 Optimal DOPH**；**GPU 加速去重（如 NVIDIA NeMo Curator / RAPIDS cuDF）或极短文本（标题/Query/商品短句）去重则优先选 Classic MinHash**，借助 GPU 并行规约将吞吐相对传统 CPU 集群提升一个数量级。

### Stage 3：热点大桶熔断与 \(O(k)\) 星型建边
在执行星型建边前必须加装**热点大桶熔断器**。若某个桶聚集的文档数超过阈值（如 `max_bucket_size = 20,000`），它极大概率是全网通用页脚或开源协议文本，不应送入主图污染连通分量，而应记录审计指标并旁路至专用的模板黑名单剥离模块。

```python
def emit_star_edges(bucket_key, doc_ids, max_bucket_threshold=20000):
    k = len(doc_ids)
    if k > max_bucket_threshold:
        # 超级热点桶旁路：避免全网通用模板触发主图污染
        emit_metric("heavy_bucket_diverted", bucket_key, count=k)
        return

    anchor = min(doc_ids)
    for d in doc_ids:
        if d != anchor:
            yield (anchor, d) if anchor < d else (d, anchor)
```

### Stage 4：假阳性消除（防连通分量“雪崩合并”）
LSH 是概率分桶，同一桶内的两篇文档并不保证真实 Jaccard 相似度达标。如果把含噪声的假阳性边直接送入 WCC，图的传递性会触发**滚雪球合并**（A 略像 B、B 略像 C……最终把数百万篇毫不相干的文档并成一个巨型 Cluster）。

因此建边之后必须插入一道低成本过滤器：直接比对候选对 \((u, v)\) 已有的 128 维 MinHash 签名重合比例 \(\hat{J}(u, v) = \frac{1}{128}\sum_{i=1}^{128} \mathbb{I}[M_i(u) == M_i(v)]\)，低于目标阈值 \(s\) 的边立即丢弃；对高价值语料还可在第二阶段做精确 Token 集合复验。

### Stage 5：分布式 WCC 引擎选型与容错
- **Ray + Arrow/Plasma Actor Pool**：现代大模型数据栈首选，零拷贝共享内存且可显式控制内存水位与 NVMe Spill，与上游 GPU NeMo Curator 衔接顺畅。
- **云原生分布式数仓图引擎**：依托成熟的高吞吐 Shuffle Service 与列存溢写能力，在 30TB+ 规模下运维成本低、稳定性强。
- **Spark GraphX**：生态完善，但长尾大星迭代期间对堆外内存与 Kryo 序列化调优要求较高。

### Stage 6：多维生存策略（Survival Policy）与血缘审计
WCC 收敛后，每个连通分量 \(\mathcal{C}_i\) 对应一组重复文档。切忌简单保留 `min(doc_id)`，而应按综合收益函数选出唯一的幸存者（Survivor）：

$$\text{Score}(d) = w_1 \cdot \text{Quality}(d) + w_2 \cdot \text{InfoLength}(d) + w_3 \cdot \text{Freshness}(d) - w_4 \cdot \text{Toxicity}(d)$$

用质量分类器（FastText/DeBERTa）淘汰抓取乱码、优先保留上下文更完整与更新鲜的版本、压制含广告与 SEO 堆砌的页面。除干净主语料表外，必须落盘一份**血缘映射表** `(cluster_id, duplicate_doc_id, survivor_doc_id, jaccard_score)`，为后续误杀回溯、版权审计与模型遗忘（Machine Unlearning）留足证据。

### 线上可观测性与四项黄金 SLO

周期性批处理去重作业上线后，监控大盘应紧盯四项指标：

1. **LSH 桶倾斜度（Top 0.01% Bucket Size）**：第一时间捕捉新出现的全网模板污染。
2. **去重压缩比（Raw Docs / Deduplicated Docs）**：若单日压缩比出现非预期跳变（如从 `1.25x` 骤升至 `3.0x`），立即熔断下游发布，防止参数误配引发语料大面积误删。
3. **WCC 收敛曲线（Delta Edges per Round）**：正常应呈指数衰减；超过 12 轮仍未归零时自动中止并排查超级节点。
4. **分片耗时标准差（Task Latency StdDev）**：衡量加权装箱调度效果，动态校准虚拟加盐阈值 \(\tau\)。

---

## 四、学术界与工业界实战调研：公开案例与反直觉教训

过去几年，学术界厘清了重复数据损害模型的机理，而各大实验室在万亿 Token 真实语料上交出的学费，则颠覆了许多直觉假设。

### 4.1 两篇奠基性学术论文

- **Google Research（Lee et al., ACL 2022）—— *Deduplicating Training Data Makes Language Models Better***：在 C4、Wiki-40B 上开展系统消融实验后发现，未去重模型在无提示生成时有超过 **1%** 的文本是从训练集**逐字照抄（Verbatim Memorization）**的。执行去重后，模型不仅用更少的训练步数达到同等甚至更低的困惑度（Perplexity），逐字复现率更暴降 **10 倍**，同时清除了训练集与评测集的重叠污染。
- **OpenAI（Hernandez et al., 2022）—— *Scaling Laws and Interpretability of Learning from Repeated Data***：量化揭示了重复数据对 Scaling Laws 的非线性破坏——只要把训练集中 **0.1%** 的样本重复 **100 次**（哪怕其余 99.9% 的 Token 完全唯一），就足以让一个 **800M 参数模型的实际表现退化到 400M 模型的水平**，相当于一半模型容量被凭空吞噬。微观电路分析进一步指出，高频重复会严重损伤负责上下文学习的 **Induction Heads（归纳头）**。

### 4.2 工业界四大标杆实战对比

| 标杆项目 / 机构 | 语料规模 | 公开暴露的核心难点 | 工程解法与反直觉经验 |
|---|---|---|---|
| **FineWeb / FineWeb-2**<br>（Hugging Face） | 96 个 Common Crawl 快照<br>（15T–20T Tokens） | **全局去重反而让模型变差**。最初跨 96 个快照从新到旧做全局 MinHash，砍掉老快照 90% 数据（仅剩 4T），代理模型评测却不升反降。 | 排查发现老快照（2013–2015）幸存的 10% 多是广告与关键词堆砌，优质历史文本反因模板重合被误杀。团队果断转向**单快照内独立去重（Per-Dump）**保留 20T 语料，效果反超 RefinedWeb。到 **FineWeb-2** 时进一步演化为“按语种全局去重 + **Rehydration（簇规模回补重采样）**”——元数据记录 `minhash_cluster_size`，训练时对高价值大簇受控上采样，找回被一刀切抹掉的自然频率先验。 |
| **SlimPajama**<br>（Cerebras） | RedPajama 1.21T \(\to\) 627B Tokens | **万亿 Token 级 LSH 内存墙**。常规 Python 库（如 `datasketch`）在全局跨域去重时直接 OOM；即便自研多线程管线，生成全局候选对的峰值内存仍高达 **1.4 TB RAM**。 | 将签名计算与 LSH 索引解耦落盘：先并行算出 13-gram MinHash 签名持久化，再按调优后的 Band/Range 分片加载匹配，验证了超大规模去重离不开磁盘溢写与分布式图引擎。 |
| **Dolma / OLMo**<br>（AI2） | 3T Tokens | **Python 吞吐与索引膨胀瓶颈**。十亿级文档下传统全量倒排索引与 Python 处理耗时过长、内存成本过高。 | 采用 **Rust 高并发工具链** 压榨单机多核 I/O；生态中进一步演进出 **LSHBloom** 等轻量方案，用布隆过滤器替代臃肿的 LSH 倒排表，大幅压缩内存与磁盘占用。 |
| **NeMo Curator**<br>（NVIDIA） | 数十 TB 至 PB 级多模态/文本 | **CPU 集群算力墙**。传统 Spark/Dask CPU 集群跑模糊去重吞吐约 ~300 GB/h，清洗一次 PB 级抓取动辄数周，严重拖慢算法迭代。 | 依托 **RAPIDS cuDF + Dask-GPU** 将切词、MinHash 与 LSH 分桶全面 GPU 化，相对 CPU 提速 **10x–16x**（8×H100 节点模糊去重吞吐约 **~4 TB/h**，精确去重约 **~18 TB/h**），把数周清洗压缩至小时级。 |

FineWeb 系列实验带来了一条极具分量的方法论修正：**去重并非“删得越多越好”的单调优化**。一切去重阈值、跨度与回补策略的取舍，最终都应由小尺寸代理模型（Proxy Model）在下游基准上的真实收益来裁决。

---

## 五、架构演进：当 SQL 引擎融合 Vector Search 与 Text Search

除了面向 PB 级预训练语料的 MinHash 离线管线，近两年数据基础设施另一条主线正在快速成型：**现代 SQL 引擎直接吸收 Vector Search（向量检索）与 Text Search（全文/词法检索），将“清洗 + 模糊/语义去重（SemDeDup）”收敛为声明式 SQL 流水线**（关于数仓语义算子与 LLM 数据管线的系统横评，可参见 [AI Functions、LLM 数据管线与云数仓引擎内核横评]({{< relref "2026-11-20-ai-functions-and-llm-data-pipelines.md" >}})）。

过去为了做语义去重，团队往往要维护“Spark 做清洗 \(\to\) 同步 Elasticsearch 做全文 \(\to\) 同步专用向量库做语义检索”三套系统，存储多份拷贝且一致性难以保障。如今业界已分化出四大 SQL / AI-Native 阵营：

| 引擎阵营 | 代表产品 | 核心能力与去重/管线实现机制 | 典型主战场 |
|---|---|---|---|
| **云原生数仓 / Lakehouse** | **BigQuery**<br>**Snowflake Cortex**<br>**Databricks** | 原生提供 `ML.GENERATE_EMBEDDING`、`VECTOR` 类型与 ANN 检索表函数（BigQuery 基于 ScaNN 的 `VECTOR_SEARCH`、Snowflake Cortex Search 混合检索、Databricks Delta + `ai_query` + GraphFrames WCC）。 | 云上湖仓内的批量语义自连接去重、实体消歧与企业 AI-ETL。 |
| **实时分析型 OLAP 引擎** | **Hologres**<br>**StarRocks**<br>**Apache Doris**<br>**ClickHouse** | 将结构化谓词裁剪、倒排全文索引（BM25/N-gram）与向量索引（Proxima / HNSW / IVF-PQ）统一收口到 C++ 向量化内核；配合 Primary Key Upsert 或 ClickHouse `ReplacingMergeTree` 承接实时写入精确去重与混合召回。 | 实时资讯/商品同款去重、高并发企业 RAG 知识库与在线风控防重。 |
| **PG 生态与嵌入式引擎** | **PostgreSQL**（`pgvector` + `ParadeDB` + `pg_trgm`）<br>**DuckDB**（`vss` + `fts`） | PG 借助 Rust/Tantivy 驱动的 BM25（ParadeDB）、HNSW（pgvector）与 Trigram 模糊匹配，在 ACID 事务内零外部同步实现**在线 SemDeDup 拦截**与 **RRF（倒数排名融合）**；DuckDB 在单机进程内直读 S3 Parquet 完成向量+全文清洗。 | 千万级业务库一体化去重、本地或中小规模（GB–TB 级）RAG 语料管线。 |
| **AI-Native 多模态引擎** | **Daft + LanceDB**<br>**Pixeltable**<br>**LOTUS** | **Daft**（Rust 向量化 SQL/DataFrame）流式解码图像/视频/PDF 并调度 GPU 推理，落盘至支持多模态 Blob + ANN + FTS 的 **LanceDB**；**Pixeltable** 以“表即管线”自动维护计算列与索引；**LOTUS** 在关系代数中引入 `sem_dedup`、`sem_join` 等语义算子。 | 图像/视频/PDF 多模态清洗与声明式语义数据流水线。 |

在 SQL 引擎里如果直接对大表做 `VECTOR_COSINE_DISTANCE` 自连接，依然会撞上 \(O(N^2)\) 计算墙。生产实践普遍借助 **ANN 向量索引（如 BigQuery `VECTOR_SEARCH`）** 与 **四层混合漏斗** 压缩比对空间：

```sql
-- BigQuery 风格：用 ScaNN 向量索引替代 O(N²) 暴力自连接，召回高置信语义重复对
SELECT
  query.doc_id AS src_id,
  base.doc_id  AS dup_candidate_id,
  distance
FROM VECTOR_SEARCH(
  TABLE `prod.corpus_embeddings`, 'embedding',
  TABLE `prod.corpus_embeddings`, 'embedding',
  top_k => 10,
  distance_type => 'COSINE'
)
WHERE query.doc_id < base.doc_id
  AND distance < 0.05; -- 下游接 Recursive CTE 或分布式 WCC 聚类
```

![SQL 引擎融合全文检索与向量检索的四层混合去重漏斗](/images/diagrams/ai-sql-hybrid-dedup-funnel.svg)

从内核成本模型审视，**SQL + Vector Search 并不会取代 MinHash + LSH，两者在生产中构成严格的上下游互补**：

- **为什么不能用 Vector Search 硬扛百亿级原始爬虫去重？** 给百亿篇原始网页跑一遍 Embedding 推理本身就要消耗惊人的 GPU 算力，且十亿级 1536 维 HNSW 图索引的内存开销动辄数 TB 到数十 TB；相比之下，MinHash 无需神经网络推理，每篇文档仅占 512 字节指纹。
- **两级联动的最优分工**：第一层由 **MinHash + LSH + 分布式 WCC**（如 NeMo Curator / Ray）以极低成本压减 80% 以上的词面与模板冗余；第二层落入 **SQL/Lakehouse 或 Daft + LanceDB**，结合 FTS 全文与 Vector Search 执行高精度语义去重（SemDeDup，捕获改写、翻译与跨模态同款）及实时在线 RAG 入库拦截。

### 5.1 全链路视角：在 SQL 引擎中，Vector/Text Search 如何与 LLM 串成闭环？

如果把视野从“去重”进一步放大到业界完整的 **LLM 生产链路（企业 RAG、Agent 长期记忆、AI-ETL 与语义分析）**，会发现 SQL 引擎里的 **Text Search（词法/BM25）**、**Vector Search（稠密语义/ANN）** 与 **LLM（Embedding / Reranker / 生成式大模型）** 并不是三个孤立插件，而是沿着“**摄入索引 → 混合召回 → 精排生成 → 反馈写回**”咬合在一起的完整流水线：

1. **摄入与双路索引构建（LLM 编码 + FTS/ANN 同步落盘）**：
   原始工单、日志、合同或网页写入表（或 Object Table）后，SQL 引擎在声明式计算列或增量管道中调用 **Embedding 模型**（如 `ML.GENERATE_EMBEDDING` / `AI_EMBED`）生成稠密向量，同时在同一张表上维护三套结构：**结构化列（租户、权限 ACL、时间戳、金额）**、**倒排全文索引（BM25 / Tantivy）** 与 **向量索引（HNSW / ScaNN / IVF-PQ）**。词法索引负责死咬精确专有名词、错误码（如 `ERR_504`）和商品 SKU，向量索引负责捕捉同义改写与跨语言意图，两者天然互补。
2. **查询改写与多路混合召回（Structured Filter + BM25 + Vector + RRF）**：
   线上请求或离线批任务进入后，先由轻量级 LLM（或查询路由）将自然语言拆解为结构化过滤条件、关键词与查询向量；随后在**单条 SQL 执行计划**内，优化器将 `tenant_id`、权限与时间窗口下推至索引层，并行执行 **Filtered Vector Search** 与 **Filtered Text Search**，最后通过 **RRF（Reciprocal Rank Fusion，倒数排名融合）** 把两路 Top-K 候选合并为高召回率集合。
3. **重排与 LLM 语义裁决 / 生成（Rerank + Generative LLM）**：
   双路召回速度快（毫秒级），但双塔 Embedding 缺乏细粒度交叉注意力。SQL 引擎将融合后的 Top-50 候选直接喂给 **Cross-Encoder 重排模型（`AI_RERANK`）** 或 **生成式 LLM（`AI_GENERATE` / `sem_join`）**：
   - 在 **RAG / Agent 问答** 中，把精排后的 Top-5 文本片段与实时 SQL 聚合出的结构化业务事实（如最新库存、客单价）拼装进上下文，生成带行级引用（Citation）的回答；
   - 在 **语义去重 / 实体消歧 / AI-ETL** 中，对于向量距离落在灰色区间（例如 `0.05–0.15`）的边界候选对，交由 LLM 做最终“是否同义/同一实体”的高精度裁决或抽取结构化 JSON。
4. **状态写回与事务闭环（ACID Write-back）**：
   LLM 产出的去重幸存标记、结构化标签、Agent 记忆摘要或人工反馈，直接写回同一张 ACID 表并增量刷新全文与向量索引，彻底消除多系统异步同步延迟。

用一条贴近现代云数仓（如 Snowflake Cortex / BigQuery / Hologres）风格的声明式 SQL，就能直观看到这三者如何在一次查询里完成接力：

```sql
WITH
-- 1. 词法召回：精准锁定错误码与专有术语（先过结构化租户与时间裁剪）
fts_top AS (
  SELECT doc_id, content,
         ROW_NUMBER() OVER (ORDER BY BM25_SCORE(content, 'ERR_504 checkout timeout') DESC) AS r_fts
  FROM prod.support_tickets
  WHERE tenant_id = 'acme' AND created_at >= CURRENT_DATE - INTERVAL '7' DAY
  LIMIT 50
),
-- 2. 向量召回：捕获“支付页卡死”“下单超时”等语义近义表达
vec_top AS (
  SELECT doc_id, content,
         ROW_NUMBER() OVER (ORDER BY COSINE_DISTANCE(embedding, AI_EMBED('checkout timeout issue')) ASC) AS r_vec
  FROM prod.support_tickets
  WHERE tenant_id = 'acme' AND created_at >= CURRENT_DATE - INTERVAL '7' DAY
  LIMIT 50
),
-- 3. RRF 倒数排名融合：把两路候选收敛为 Top-20
fused AS (
  SELECT COALESCE(f.doc_id, v.doc_id) AS doc_id,
         COALESCE(f.content, v.content) AS content,
         COALESCE(1.0 / (60 + f.r_fts), 0) + COALESCE(1.0 / (60 + v.r_vec), 0) AS rrf_score
  FROM fts_top f FULL OUTER JOIN vec_top v ON f.doc_id = v.doc_id
  ORDER BY rrf_score DESC
  LIMIT 20
)
-- 4. LLM 精排 + 归因总结：只对最精炼的候选调用昂贵的模型推理
SELECT AI_GENERATE(
  '基于以下工单归纳支付超时根因并输出引用 doc_id：' || STRING_AGG(doc_id || ': ' || content, '\n')
) AS root_cause_summary
FROM (
  SELECT doc_id, content
  FROM fused
  ORDER BY AI_RERANK('checkout timeout root cause', content) DESC
  LIMIT 5
);
```

### 5.2 为什么把“检索 + LLM”收口进 SQL 引擎？四大核心场景价值

过去很多团队用“Python 胶水代码 + 业务数仓 + Elasticsearch + 独立向量库”四套系统硬拼这套流程，看似解耦，真上生产却处处是坑。把 Vector/Text Search 与 LLM 收口进 SQL 引擎，真正释放的是四项硬核场景价值：

1. **企业级权限与实时一致性（消灭 RAG 越权与“幽灵数据”）**：
   企业落地 RAG 与内部 Agent 最大的拦路虎往往不是模型不够聪明，而是**权限泄漏**——某份薪酬或法务文档在主库里已经删除或收紧了访问角色，但外部向量库还没来得及同步，导致普通员工通过 LLM 问出了机密内容。在 SQL 引擎内部，**行级权限控制（RLS）、RBAC 与 ACID 事务更新**天然同时作用在结构化字段、全文索引与向量索引上，文档一删即不可见，从根源消除数据一致性漏洞。
2. **打通“结构化事实 + 非结构化语义”的复合决策**：
   真实业务问题很少是孤立的向量相似度查找，而通常是“*找出最近 7 天内（时间过滤）、年消费额超过 5 万美元的 VIP 客户（关系表 Join）、工单语义接近支付超时（Vector Search）且包含 `ERR_504`（Text Search）的记录，并按大区汇总根因（GroupBy + LLM）*”。把它们放进同一个基于代价的优化器（CBO），才能统一决定先做关系裁剪还是先走索引，避免拉出几十万条无关向量再在应用层硬拼。
3. **用毫秒级检索给昂贵的 LLM 做“前置漏斗”，把推理成本打下来 2–4 个数量级**：
   直接对千万行非结构化表全量跑大模型推理，Token 费用和延迟都是天文数字，且把过多噪声塞进 Prompt 还会触发“上下文稀释（Lost in the Middle）”。SQL 引擎用 **结构化裁剪 → BM25 + ANN 混合召回 → RRF** 充当极低成本的前置漏斗，在毫秒内把 1,000 万行压成 5–20 条高信噪比证据再交给 LLM 精排与生成，既把推理成本压缩 **100×–10,000×**，又显著压低了模型幻觉率。
4. **大幅降低架构熵增与多份存储拷贝税（TCO 收敛）**：
   把原本横跨数仓、ES、专用向量库和异步 CDC 同步链路的四套基础设施收敛为单一引擎与单一表存储，直接砍掉 2–3 份冗余存储拷贝与复杂的双写一致性运维；数据工程师用熟悉的 SQL、视图与 Git CI/CD 就能统一迭代传统报表、语义去重、RAG 知识库与多模态 AI-ETL，极大缩短了 AI 应用从原型到生产的交付周期。

### 5.3 从“检索”到“全生命周期”：数据管线与 SQL 引擎的六大深度结合面与边界

如果把视角进一步扩展到整个 AI 数据工程全生命周期（摄入 → 清洗 → 特征/语料治理 → 模型编排 → 在线服务/Agent 进化），数据管线与 SQL 引擎的融合早已不止于“混合检索”，而是在六个方向上系统性收敛：

1. **增量声明式管线（Dynamic Tables / IVM 取代传统调度胶水）**：
   过去为了保证下游特征表与 Embedding 索引新鲜，团队往往要同时维护 Airflow + Spark 批处理 + Flink 流计算三套链路；上游只改几百行，下游重算全量 Embedding 账单直接爆炸。以 **Snowflake Dynamic Tables、Hologres Dynamic Table、Databricks Lakeflow、RisingWave** 为代表的增量视图维护（IVM），只需声明目标表的 SQL 定义与刷新延迟（如 `TARGET_LAG = '1 minute'`），引擎自动追踪 CDC 增量，**只对变更行触发 `AI_EMBED` 或清洗计算**，将“编排式 ETL”升级为“声明式数据流”。
2. **多模态非结构化管线的“表化”统一（Object Tables + 惰性解码）**：
   通过 **Object Table（BigQuery / MaxCompute / Snowflake）** 与 **多模态执行引擎（Daft + Lance / Pixeltable）**，对象存储里上亿级的图片、视频、PDF 被映射为带版本控制的结构化元数据表（`uri`、`size`、`etag`）。SQL 优化器先做元数据裁剪与**按字节大小均衡分片**（防止单个超大视频拖垮 Worker OOM），再通过流式惰性解码将 Tensor 直接送入 GPU 批推理，避免全量下盘。
3. **语义关系代数与 CBO 代价感知的 LLM 编排**：
   将 `sem_filter`、`sem_join`、`sem_agg`（如 **LOTUS、Palimpzest** 及各云数仓 `AI Functions`）纳入 SQL 代价优化器（CBO），释放三项核心优化：**谓词重排**（廉价结构化/正则过滤永远排在昂贵 LLM 之前）、**模型级联**（90% 高置信样本走廉价本地小模型，仅 10% 难例升级旗舰大模型），以及 **KV-Cache 感知的批处理重排**（发往 vLLM/SGLang 前按共享文档前缀或 System Prompt 排序分组，大幅拉高 Prefix Cache 命中率）。
4. **训练数据治理、配比回补与时间旅行审计**：
   依托 **开放湖仓时间旅行（Iceberg / Delta / Paimon / Lance）+ SQL 窗口函数**，用标准 SQL 声明多垂类语料配比、质量分分位数截断（`NTILE`）与 FineWeb-2 式的 **Rehydration（簇规模加权回补采样）**；同时借助跨库 SQL + FTS/ANN 一键完成评测集防污染（Decontamination）与可追溯的版权删数（Machine Unlearning）审计。
5. **特征工程与离线-在线一致性（`ASOF JOIN` 消灭 Training-Serving Skew）**：
   借助现代 SQL 引擎原生的**时序点查连接（`ASOF JOIN` / Point-in-Time Join）**与批流一体执行，同一套 SQL 定义既能离线严格按事件时间戳回溯无未来数据泄漏的训练样本，又能直接承接在线低延迟特征服务，彻底消灭离线与在线两套代码口径漂移。
6. **Agent 运行时轨迹、记忆与 RL 后训练闭环（Trace Analytics → RLVR）**：
   将 Agent 的多轮工具调用轨迹、延迟、可验证奖励信号（Reward）与长期记忆（Episodic Memory）直接沉淀为统一 SQL 表：离线直接用 SQL 挖掘高失败率工具链路、筛选高质量轨迹喂给 **GRPO / RLVR 后训练**，在线则为 Agent 提供带租户/会话隔离的混合记忆检索。

| 管线环节 | 与 SQL 引擎结合的核心机制 | 核心工程收益 |
| :--- | :--- | :--- |
| **增量 ETL / 索引保鲜** | Dynamic Tables / 增量视图维护（IVM） | 仅重算变更行，消灭全量重算的 GPU/LLM 浪费 |
| **多模态预处理** | Object Table + 惰性流式解码（Daft / Lance） | 元数据下推、按文件大小防倾斜、零本地落盘 |
| **LLM 批量调用** | 语义算子 + CBO 谓词重排 + 模型级联 + 前缀排序 | 压低 10×–100× Token 成本，拉高 Prefix KV-Cache 命中 |
| **语料配比与审计** | Lakehouse Time-Travel + 窗口采样 + 血缘表 | 可复现训练快照、防评测污染、满足版权合规 |
| **特征工程** | `ASOF JOIN` + 批流一体 SQL | 消除时间穿越与离线/在线特征口径漂移 |
| **Agent 记忆与进化** | Trace 表化 + 混合检索 + RLVR 轨迹筛选 | 打通“线上运行 → 故障分析 → 后训练进化”闭环 |

> **清晰的架构边界**：拥抱 SQL 融合并不意味着把所有计算都硬塞进 SQL 引擎。**超大规模多轮迭代图算法（如百亿边 WCC）**、**带任意代码沙箱的复杂多步 Agent 控制流**，以及 **分布式 GPU 模型训练（FSDP / Megatron）** 仍应留在 Ray 或专用图/训练运行时中，通过 **Apache Arrow 零拷贝内存** 与 **开放表格式（Parquet / Lance / Iceberg）** 与 SQL 引擎高速衔接、各司其职。

---

## 六、业务视角反思：为什么去重是 AI 时代 ROI 最高的“隐形基建”？

跳出底层哈希、图算法与 SQL 优化器，站在业务与研发资源分配的视角回看，投入重兵建设 PB 级去重平台绝非工程师的过度打磨，而是直接牵动大模型商业成败的四条主线：

1. **算力经济学与直接 ROI**：万卡 GPU 集群按小时燃烧巨额成本，预训练的财务目标是用有限 FLOPs 换取尽可能高的有效知识密度。原始网页语料中常见 30%–40% 的精确重复与更高比例的模板近似重复；结合 OpenAI 的实验结论（0.1% 样本重复 100 次即令有效模型能力腰斩），前端低成本的数据清洗与去重管线，往往能换来下游万卡训练集群数倍的有效算力倍增。
2. **版权合规与隐私安全防线**：商业模型面对媒体版权诉讼时，最致命的举证往往是模型逐字复现付费原文；而在 B 端与海外业务中，包含个人隐私（PII）的镜像页面一旦被模型背诵，便会触发严峻合规风险。模糊去重将逐字记忆概率压低一个数量级，配合全链路血缘映射表（Lineage Audit），使企业在面对监管审计与 GDPR“被遗忘权”删除请求时具备可追溯、可举证的工程抓手。
3. **评测榜单防污染**：业务团队依赖 MMLU、GSM8K、HumanEval 等基准做模型上线决策。互联网语料中混杂大量被博客和仓库二次搬运的变体题库，缺少针对测试集的跨库模糊去重（Decontamination），就会让内部评估沦为“背题分数虚高、线上真实体验崩盘”的昂贵陷阱。
4. **从“唯 Token 规模论”走向“信息密度治理”**：当公开人类语料逼近存量天花板，行业竞争正从“喂了多少万亿 Token”转向“单位 Token 承载了多高的信息密度与合理分布”。FineWeb-2 的 Rehydration 与多维 Survival Policy 都在指向同一个方向：去重不再只是粗暴的“删除过滤器”，而是通过构建全库冗余图谱，为不同垂类（代码、数学、多语言、多模态）动态调节知识密度的核心数据资产调度层。

---

## 参考与基线说明

- 算法与学术文献：Rastogi et al., *Connected Components in MapReduce and Beyond* (2013)；Shrivastava & Li, *Densifying One Permutation Hashing via Rotation for Fast Near Neighbor Search* (ICML 2014) 与 Shrivastava, *Optimal Densification for Fast and Accurate Minwise Hashing* (ICML 2017)；Lee et al., *Deduplicating Training Data Makes Language Models Better* (ACL 2022)；Hernandez et al., *Scaling Laws and Interpretability of Learning from Repeated Data* (OpenAI, 2022)。
- 工业界公开项目与文档：Hugging Face FineWeb / FineWeb-2 技术报告、Cerebras SlimPajama 公开工程博客、AI2 Dolma 工具链与 LSHBloom 论文、NVIDIA NeMo Curator 官方文档（cuDF / Dask-GPU 模糊去重工作流），以及 Google BigQuery、Snowflake Cortex、PostgreSQL（pgvector / ParadeDB）、DuckDB、Hologres、StarRocks、Daft 与 LanceDB 的公开文档。
