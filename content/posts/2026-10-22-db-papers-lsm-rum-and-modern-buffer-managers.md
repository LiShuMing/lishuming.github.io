---
title: "【Paper】存储结构与缓冲管理演进精读：五分钟法则、RUM 猜想、LSM 家族与 LeanStore/Umbra"
slug: "db-papers-lsm-rum-and-modern-buffer-managers"
date: 2026-10-22T13:10:00+08:00
lastmod: 2026-11-12T00:00:00+08:00
description: "从五分钟法则三十年硬件经济学与 RUM 放大率猜想出发，系统拆解 LSM-Tree 异位写演进谱系（经典滚动合并、Leveling/Tiering 综述、Fractal Tree、SuRF 前缀过滤、Bourbon 学习索引、CaaS-LSM 近存卸载）以及现代 NVMe 页式缓冲管理革命（LeanStore 指针混洗到 Umbra 变长页零页回收）。"
draft: false
toc: true
math: true
categories: ["数据库"]
tags: ["论文精读", "存储引擎", "LSM-Tree", "Buffer Pool", "LeanStore", "Umbra"]
series: ["数据库经典论文精读"]
---

> 本系列第 8/10 篇 · [数据库经典论文精读总索引]({{< relref "2026-10-22-database-papers-source-reading.md" >}}) · [上一篇：多核并行、主存连接与高速网络]({{< relref "2026-10-22-db-papers-parallel-joins-formats-and-networks.md" >}}) · [下一篇：事务隔离与分布式存储]({{< relref "2026-10-22-db-papers-isolation-concurrency-and-distributed-transactions.md" >}})

在数据库内核的所有子系统里，存储引擎面对的物理矛盾最为冷酷：**没有任何一种数据结构能同时让随机写入只落盘一次、点查与范围扫描只读目标字节、且不消耗额外辅助空间**；
更棘手的是，底层存储介质本身的读写代价比例还在每隔十年发生一次剧烈翻转。

在翻开十篇横跨三十年的经典文献之前，我们先在白板上算两笔跨越时代的硬件经济账，看看为什么传统的原位更新 B+-Tree 先后在“温数据随机写”与“高速 NVMe 随机读”两端撞上了南墙：

1. **第一堵墙：温数据随机写引发的磁盘寻道灾难**。假设业务每秒插入 `1,000` 条 16 字节的账户流水索引项 `AcctID || Timestamp`，累计 20 个工作日共产生约 `9.2 GB` 索引数据。
   由于 `AcctID` 随机分布，每次插入都会散落到 B+-Tree 几十万个温热叶子页中的任意一页：为了塞进区区 **16 字节**，存储引擎必须先把旧的 `4 KB`（或 `8 KB`）叶子页随机读入内存，修改后再随机写回磁盘——字节级写放大瞬间超过 **250 倍**，两块机械磁臂的随机 IOPS 被彻底打满。
   这逼出了**异位写（Out-of-Place Update）与日志结构合并树（LSM-Tree）**的整条演进脉络。
2. **第二堵墙：百万 IOPS NVMe 时代全局缓冲池的 CPU 自残**。三十年后，一块企业级 PCIe NVMe SSD 就能轻松提供数十万乃至数百万随机读 IOPS，大内存服务器更能让 90% 以上的工作集常驻 DRAM。
   按理说 B+-Tree 应该重获新生，可实测却发现传统页式存储引擎（如 BerkeleyDB、WiredTiger）在 10 核机器上跑内存命中的 TPC-C 只能勉强维持 `10K~18K TPS`，比纯内存指针树慢了整整 **30 多倍**！瓶颈早已不在固态盘，而在于传统 `Buffer Manager` 每次沿着 B+-Tree 下探一层，都要去全局 `PageID` 哈希表抢锁翻译地址、修改共享 LRU 链表缓存行并加解读页锁。
   这又逼出了 **LeanStore 与 Umbra** 彻底重塑现代缓冲管理器的第二场革命。

本篇将十篇奠基与前沿论文编织为三幕演进长卷：**第一幕（`#55`, `#58`）** 建立硬件经济学盈亏公式与 RUM 三轴放大率坐标；
**第二幕（`#63`, `#61`, `#56`, `#62`, `#59`, `#57`）** 追索 LSM 家族如何用顺序合并驯服随机写，又如何借消息缓冲、紧凑前缀树、学习索引与近存卸载逐一修补读放大与云上合并抖动；
**第三幕（`#60`, `#64`）** 剖析页式引擎如何凭指针混洗（Pointer Swizzling）与操作系统虚拟内存零页映射，在超内存 NVMe 上夺回逼近裸内存的吞吐极限。

---

## 第一幕：经济学与数学边界——五分钟法则与 RUM 三角

### 五分钟法则三十年：当 DRAM 与 NVMe 的盈亏平衡跌破一分钟 {#paper-55}

> **论文信息**：Raja Appuswamy, Renata Borovica-Gajic, Goetz Graefe, Anastasia Ailamaki. *The Five-Minute Rule Thirty Years Later and Its Impact on the Storage Hierarchy*. ADMS@VLDB 2017（29 页技术报告讲义）. [原始 PDF（共 29 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/storage-media/the-five-minute-rule-thirty-years-later-and-its-impact-on-the-storage-hierarchy.pdf)（核心页面见 p. 2–4 1987 原始推导、p. 8–12 DRAM-HDD/SSD 对比、p. 16–17 PCIe NVMe / Optane 收缩及 p. 22–29 冷热分层重构）。

一个数据页究竟多长时间被访问一次，才值得花钱把它常驻在更快的上层存储介质（如 DRAM）里，而不是每次需要时从慢速下层介质（如磁盘或闪存）临时读取？1987 年 Jim Gray 与 Gianfranco Putzolu 给出了著名的**五分钟法则（The Five-Minute Rule）**：

> "Pages referenced every 5 minutes should be memory resident" *(PDF p. 2)*

支撑这条经验法则的并非拍脑袋猜测，而是一个干净利落的硬件租用成本平衡方程。设页面间隔 \(t\) 秒被随机访问一次：若不缓存，它每秒消耗慢速设备 \(\frac{1}{t}\) 个 IOPS，对应硬件分摊成本为 \(\frac{1}{t} \times \$/\text{IOPS}_{\text{slow}}\)；
若常驻快速介质，它占住一页容量，成本为 \(\frac{\$/\text{MB}_{\text{fast}}}{\text{PagesPerMB}}\)。
令两项成本相等，即得**盈亏平衡重访间隔（Break-Even Interval）**：

$$\text{Break-even Interval } t = \frac{\text{PagesPerMB} \times \$/\text{IOPS}}{\$/\text{MB}_{\text{RAM}}} = \frac{\text{PagesPerMB}}{\text{IOPS}_{\text{slow}}} \times \frac{\text{PricePerDevice}_{\text{slow}}}{\text{PricePerMB}_{\text{fast}}}$$

把 1987 年的价格与技术参数代入（`1 KiB` 页即 \(\text{PagesPerMB}=1024\)，一块 `15 IOPS` 的磁盘连控制器售 `$30,000`，而 1 MB 内存高达 `$5,000`）：

$$t_{1987} = \frac{1024}{15} \times \frac{30000}{5000} \approx 409.6 \text{ 秒} \approx 6.8 \text{ 分钟（经验口诀记作 5 分钟）}$$

三十年间，摩尔定律在不同硬件维度上的演进极不对称：DRAM 每 MB 价格暴跌了近六个数量级，但机械硬盘（HDD）是**容量暴涨了数万倍、磁臂随机 IOPS 却只涨了十来倍**；
与此同时，夹在 DRAM 与 HDD 之间的 SATA SSD、PCIe NVMe SSD 与字节寻址持久内存（SCM / Optane）异军突起。
Appuswamy 等人把 2017 年真实硬件目录价代入同一公式（以 `4 KB` 页、即 \(\text{PagesPerMB}=256\) 为基准，见 PDF p. 8–17），算出了极具冲击力的跨代分化图景：

| 快速层 ↔ 慢速层配对 | 1987 年盈亏间隔 | 2007 年（20 周年） | 2017 年（30 周年，4KB 页口径） | 架构演进含义 |
| :--- | :--- | :--- | :--- | :--- |
| **DRAM ↔ HDD** | `~5 分钟` (1KB 页) | `~1.5 小时` | **`~4 ~ 5 小时`** | HDD 沦为纯容量层，靠加大内存吸收随机 I/O |
| **DRAM ↔ SATA SSD** | — | `~15 分钟` | **读 `~7 分钟` / 写 `~24 分钟`** | 闪存读写不对称使写密集页更值得留驻内存 |
| **SATA SSD ↔ HDD** | — | — | **`~1 天`** | 一天内会被再碰一次的温数据都该放进 SSD |
| **DRAM ↔ PCIe NVMe / Optane** | — | — | **`~41 秒 ~ 1.5 分钟`**（Optane 约 `47s~52s`） | 百万级低价随机 IOPS 倒逼引擎从纯内存退回快闪存 |
| **HDD ↔ 磁带（Tape）** | — | — | 随机指标荒谬达 `~300 年`；按扫描带宽 `$/TBscan` 差 `10×` | 冷层不能按随机 `$/IOPS` 计价，顺序批处理可合并归档层 |

这张表揭示了两条深刻塑造今天存储架构的物理推论：

第一，**DRAM 与高速 NVMe / SCM 之间的盈亏间隔正在急剧跌破 1 分钟**：

> "DRAM-NVM break-even interval is shrinking ... Impending shift from DRAM to NVM-based data management engines" *(PDF p. 17)*

当一块几百美元的 NVMe 固态盘就能跑出数十万 IOPS 时，只有每隔几十秒就被高频触碰的极热页才配留在昂贵的 DRAM 里；把全库硬塞进纯内存数据库的经济合理性大幅萎缩，**能像操作内存一样低开销调度 NVMe 页面的新型缓冲引擎（即第三幕的 LeanStore 与 Umbra）**成为时代刚需（注：原文幻灯片 p. 16–17 个别算例存在四舍五入口径尾差，但跌入数十秒至一两分钟量级的趋势确凿无疑）。

第二，在存储金字塔底端，如果死守随机 IOPS 指标衡量磁带或叠瓦磁记录（SMR）盘，其每千次随机访问成本比 SSD 贵 \(1.6 \times 10^6\) 倍，算出的 300 年缓存周期毫无意义；
唯有改用顺序吞吐指标 **`$/TBscan`**，才能看清现代分层存储正在收敛为“**DRAM + 快闪存性能层（Performance Tier）**”与“**面向顺序批扫描的高密度冷数据湖层（Cold Batch Tier）**”两极结构。

---

### RUM 猜想：读放大、写放大与空间放大的三轴守恒账本 {#paper-58}

> **论文信息**：Manos Athanassoulis, Michael S. Kester, Lukas M. Maas, Radu Stoica, Stratos Idreos, Anastasia Ailamaki, Mark Callaghan. *Designing Access Methods: The RUM Conjecture*. EDBT 2016, 共 6 页. [原始 PDF（共 6 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/storage-structure/designing-access-methods%3A-the-rum-conjecture.pdf)（核心图示见 p. 2 `Figure 1` RUM 三角、p. 4 `Table 1` 与 `Figure 2` 经典索引复杂度与层级转移、p. 5 `Figure 3` 自适应可调索引愿景）。

如果说五分钟法则决定了数据该驻留在哪一层介质，那么在这层介质之上组织索引与访问方法（Access Method）时，我们又受制于怎样的数学缰绳？哈佛、EPFL 与 Facebook RocksDB 团队联手提出的 **RUM 猜想（The RUM Conjecture）**，将千变万化的索引结构统一拉回到了三项以**基础业务数据（Base Data）**为分母的归一化开销比率上：

1. **读开销（Read Overhead, `RO`）**：检索目标数据时读取的总字节数（含辅助索引与基础数据）除以实际需要取回的基础数据字节数：
   > "We refer to RO as the read amplification: the ratio between the total amount of data read including auxiliary and base data, divided by the amount of retrieved data." *(PDF p. 2)*
2. **更新开销（Update Overhead, `UO`）**：一次逻辑更新引发的物理写入字节总量（含原位页重写、索引维护或后台合并摊销）除以逻辑更新字节量。
3. **存储/内存开销（Memory / Storage Overhead, `MO`）**：基础数据加上全部辅助结构（索引树、Bloom 位图、未合并历史版本与墓碑）的总占用字节数除以纯基础数据字节数。

注意这三个比值在分子里都包含了基础数据本身，因此**各自理论理想下界均为 \(1.0\)（而非 \(0\)）**：例如额外多占 25% 辅助索引空间记作 \(\text{MO} = 1.25\)。
为了直观展示为什么压死其中一两项就会在另一端鼓起大包，论文第 2 节在白板上推演了三个推向极致的玩具数据结构（PDF p. 3）：

- **只压极小读开销（直接值寻址数组）**：令物理槽号直接等于整数键值 `blkID = value`。假设当前集合里只有 `{1, 17}` 两个整数，点查直接按值定槽，\(\text{RO} = 1.0\) 达到完美下界；
  但为了容纳 `17` 必须开辟 `1..17` 共 17 个槽（此时 \(\text{MO} = 17/2 = 8.5\)，随值域上界趋向 \(\infty\)），且把 `17` 改成 `18` 时既要清空 17 号旧槽又要写 18 号新槽，单次逻辑修改触发两次物理块写（\(\text{UO} = 2.0\)）。
- **只压极小写开销（纯追加日志 Append-Only Log）**：每次插入、更新、删除都只向文件末尾追加一条记录，\(\text{UO} = 1.0\) 达到写下界；
  但只要不做后台垃圾回收与合并，失效旧版本便无限堆积推高 \(\text{MO}\)，点查也被迫倒序扫描整条历史日志推高 \(\text{RO}\)；
  而一旦引入后台合并回收空间，\(\text{UO}\) 就立刻大于 \(1.0\)。
- **零辅助结构的稠密无序数组**：不建任何附加索引，\(\text{MO} = 1.0\)；若已知槽位原位覆写则物理写也是 \(\text{UO} = 1.0\)，但任何按值查找或定位更新目标的操作在最坏情况下都必须全表扫描 \(N\) 条记录，\(\text{RO} = O(N)\)。

把视野扩展到工业界四大主力访问方法——**B+-Tree、哈希索引（Hash Index）、稀疏分区统计（ZoneMaps）与分层 LSM-Tree**——同样找不到全能冠军：

> "The I/O cost [2] (time and space complexity) of four representative access methods (B+-Trees, Hash Indexes, ZoneMaps, and levelled LSM) illustrates that there is no single winner." *(PDF p. 4)*

据此，作者提出了指导现代存储设计的 **RUM 猜想**：在设计访问方法时，一旦为 `R`、`U`、`M` 中的任意两项开销设定了上界，就会不可避免地为第三项开销锁死一个下界。
在工程应用这套模型时，有两个极易踩坑的关键细节必须拎清：

- **猜想不是已证的刚性守恒等式，CPU 与数据先验属于第四隐藏轴**：原文明确将其定性为 *Conjecture*（把严格形式化证明留作开放问题）。
  对一个尚未达到帕累托前沿（Pareto Frontier）的粗糙基线，利用字典/前缀压缩或有序分布先验完全可能同时降低 `RO`、`UO` 和 `MO` 的字节比——只不过代价往往转移到了三轴之外的 **CPU 解压与模型计算开销**上（正如后文 `#62` SuRF 与 `#59` Bourbon 所展示的那样）。
- **跨层与跨论文对比必须先对齐统计口径**：在多层存储金字塔中，在 DRAM 多放一层 Bloom 过滤器（提高内存层 `MO`）是为了削减 SSD 层的无效随机读（降低闪存层 `RO`）；
  同时，本文按**字节比**定义 `RO`，而许多存储论文（如 `#56` Fractal Tree 白皮书）按**磁盘 I/O 次数**定义读放大——脱离页大小、负载输出行数与缓存命中率硬比数字没有任何意义。

---

## 第二幕：异位写优化的演进谱系——从经典 LSM-Tree 到云原生近存合并

一旦站在 RUM 三角的高度俯瞰，整个 LSM-Tree 家族二十余年的演进逻辑便豁然开朗：先用内存缓冲加顺序批量归并把随机写的 `UO` 打下来（`#63`），接着在 `Leveling`、`Tiering` 与单树内节点消息缓冲（`#56`）之间精细调拨写放大、空间放大与读放大的配比（`#61`），再针对不可变 SSTable 发明保序前缀过滤器 `SuRF`（`#62`）与分段线性学习索引 `Bourbon`（`#59`）把多段查找的 `RO` 压回去，最终在云原生存算分离架构下把占 90% I/O 的后台合并剥离成弹性近存服务 `CaaS-LSM`（`#57`）。

![RUM 权衡与 LSM 家族演进：合并拓扑、消息树、范围/学习过滤与存算分离卸载](/images/diagrams/paper-store-rum-and-lsm-lineage.svg)

### 经典 LSM-Tree (1996)：温数据磁盘臂瓶颈、滚动合并与等比层比证明 {#paper-63}

> **论文信息**：Patrick O'Neil, Edward Cheng, Dieter Gawlick, Elizabeth O'Neil. *The Log-Structured Merge-Tree (LSM-Tree)*. Acta Informatica 33(4): 351–385, 1996（单行本共 32 页）. [原始 PDF（共 32 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/storage-structure/the-log-structured-merge-tree-%28lsm-tree%29.pdf)（核心图示见 p. 5 `Figure 2.2` \(C_0/C_1\) 滚动合并游标、p. 11 `Figure 3.1` 温冷热数据成本折线及 p. 18–25 硬件成本算例与并发恢复）。

回到本文开篇那个每秒插入 `1,000` 条 16 字节 `AcctID || Timestamp` 历史记录的 TPC-A 场景（Example 3.1 & 3.3，PDF p. 18–19）。
O'Neil 等人首先把五分钟法则推广成了连续的**访问温度（Temperature \(T = H / S\)，即每秒 I/O 率除以数据兆字节数）成本函数**：

> "COST-TOT = min(max(S.COSTd, H.COSTP), S.COSTm + S.COSTd)" *(PDF p. 10)*

其中 \(\text{COST}_d\) 是磁盘每 MB 容量单价，\(\text{COST}_P\) 是提供 1 IOPS 所需的磁盘磁臂单价，\(\text{COST}_m\) 是内存每 MB 单价。
当数据极冷（\(H/S\) 很小）时，总价由磁盘容量 \(S \cdot \text{COST}_d\) 决定；当数据极热时，不如干脆花 \(S \cdot \text{COST}_m\) 全放进内存；
最要命的恰恰是**温数据（Warm Data）**——总体积 `9.2 GB` 放进上世纪九十年代的内存太贵，留在磁盘 B-Tree 上又因每秒 `1,000` 次随机叶页读写（`2,000 IOPS`）把磁臂成本 \(H \cdot \text{COST}_P\) 顶成天花板：

> "Using a B-tree to support the index, the disk I/O will be the limiting factor as we saw in Example 3.1 — the leaf-level data is warm." *(PDF p. 19)*

按当时硬件报价核算，维护这棵 B-Tree 需要高达 **`$56,400`** 的磁盘阵列以凑够随机磁臂 IOPS，而两层 LSM-Tree 将总硬件成本直接砍到了 **`$11,400`**（省下近 80%）！这笔巨额节省来自**两重正交的物理摊销效应**：

1. **多页块顺序 I/O 替代单页随机寻道（\(\text{COST}_\pi \ll \text{COST}_P\)）**：LSM-Tree 将最新写入先插入内存驻留组件 \(C_0\)（无需遵循磁盘页格式，可用紧凑 AVL 或 2-3 树）。
   当 \(C_0\) 达到阈值，后台启动异步**滚动合并游标（Rolling Merge Cursor，`Figure 2.2`）**，每次从磁盘组件 \(C_1\) 顺序读入包含数十个连续页面的**多页块（Multi-Page Block）**，与 \(C_0\) 中落在该键域区间的全部新项做归并排序，再以满载新多页块顺序写回磁盘空闲区——单页顺序流式读写代价 \(\text{COST}_\pi\) 仅为随机磁臂寻道 \(\text{COST}_P\) 的约十分之一。
2. **单次叶页读写批量捎带多条新记录（Batching Factor \(M\)）**：设单个磁盘页大小为 \(S_p\)、单条索引项大小为 \(S_e\)（例如一页可装 \(S_p / S_e = 200\) 条记录），内存 \(C_0\) 容量为 \(S_0\)，磁盘 \(C_1\) 容量为 \(S_1\)。
   滚动游标每扫过 \(C_1\) 的一个叶页，平均能从 \(C_0\) 顺道合并进来的新记录条数为：

   $$M = \frac{S_p}{S_e} \cdot \frac{S_0}{S_0 + S_1}$$

哪怕磁盘 \(C_1\) 比内存 \(C_0\) 大 40 倍（\(S_1 = 40 S_0\)），\(M = 200 \times \frac{1}{41} \approx \mathbf{4.88}\)——原本在 B-Tree 里每插 1 条记录就要付 2 次随机页 I/O，如今在 LSM-Tree 里每读写 1 个顺序页就能摊销近 5 条记录！

如果工作集更大、单靠 \(C_0, C_1\) 两层令 \(S_1 / S_0\) 悬殊过大导致 \(M < 1\) 怎么办？自然推广为 \(K+1\) 层组件 \(C_0, C_1, \dots, C_K\)。
设稳定纯插入负载下各层容量依次为 \(S_0 < S_1 < \dots < S_K\)，定义相邻层容量比 \(r_i = S_i / S_{i-1}\)。
由于总倍率 \(\prod_{i=1}^K r_i = S_K / S_0\) 固定，而每条插入记录在从 \(C_{i-1}\) 滚入 \(C_i\) 时分摊的磁盘归并读写量正比于 \((r_i + 1)\)，因此跨全部 \(K\) 层的总合并 I/O 率正比于 \(\sum_{i=1}^K r_i\)。
由**均值不等式（AM-GM Inequality）**：

$$\frac{1}{K} \sum_{i=1}^{K} r_i \ge \left(\prod_{i=1}^{K} r_i\right)^{1/K} = \left(\frac{S_K}{S_0}\right)^{1/K}$$

当且仅当 **\(r_1 = r_2 = \dots = r_K = r\)** 时取等号！这就是今天所有现代 LSM 引擎（LevelDB、RocksDB、Pebble）默认让各层容量呈**等比数列（Geometric Progression）**增长的原始数学出处。
此外，论文第 5–6 节还细致设计了基于节点级短物理锁的并发滚动游标与不阻塞长合并的检查点恢复（短暂挂起新写入、刷空 \(C_0\)、记录 `LSN0` 与各层合并游标位置），为工业落地铺平了道路。

---

### LSM 存储技术综述：不可变 SSTable、Leveling/Tiering 谱系与次级索引防复活陷阱 {#paper-61}

> **论文信息**：Chen Luo, Michael J. Carey. *LSM-Based Storage Techniques: A Survey*. The VLDB Journal 29: 393–418, 2020（arXiv 预印本共 25 页）. [原始 PDF（共 25 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/storage-structure/lsm-based-storage-techniques%3A-a-survey.pdf)（核心图表见 p. 4–5 `Figure 3–6` 分区合并拓扑、p. 6 `Table 1` 复杂度矩阵、p. 10 `Table 2–3` 代表系统分类与 p. 16–19 次级索引及过滤一致性）。

从 1996 年的理论原型走向 Bigtable、LevelDB、RocksDB、Cassandra 与 AsterixDB 等现代工业系统，LSM-Tree 首先完成了一次至关重要的工程蜕变：**放弃早期复杂的B-Tree 原位滚动游标，全面转向不可变有序字符串表（Immutable SSTable）**。
内存 `MemTable` 写满后冻结刷成全新的只读 SSTable，后台 Compaction 也是读取若干旧 SSTable 顺序写出新 SSTable，最后通过原子更新版本元数据（`Manifest / VersionSet`）切换视图。
不可变性瞬间简化了并发控制与崩溃恢复：只要遵循事务未提交不刷组件（或内存组件等活跃写事务排空再落盘）的 **`no-steal` 策略**，故障恢复只需重放预写日志（WAL）做 REDO，连传统 ARIES 里最繁琐的盘页 UNDO 都彻底免除了：

> "During recovery for an LSM-tree, the transaction log is replayed to redo all successful transactions, but no undo is needed due to the no-steal policy." *(PDF p. 6)*

Luo 与 Carey 将五花八门的 LSM 改进方案统一收束进 **层间合并策略（Merge Policy）** 的二维主轴——设层间容量倍率（Size Ratio）为 \(T\)，总层数为 \(L \approx \lceil \log_T (N / M_{\text{mem}}) \rceil\)，页容量为 \(B\) 条记录：

| 合并拓扑与代表变体 | 每层有序运行段数（Runs） | 摊销写开销（I/O 页数/项） | 无 Bloom 点查最坏读 | 稳态空间放大（SA） | 核心适用倾向与代表系统 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Leveling（分层合并）** | 每层严格 **1 个**全局有序段（常按细粒度 SST 分区） | \(O\left(\frac{T \cdot L}{B}\right)\)（单层反复被重合并约 \(T\) 次） | \(O(L)\) | \(O\left(1 + \frac{1}{T}\right)\)（最底一统天下，约 `1.11×` @ \(T=10\)） | 偏点查、范围扫描与省磁盘空间（LevelDB, RocksDB 默认） |
| **Tiering（大小分阶合并）** | 每层攒满 **\(T\) 个**有序段才一次性合并推入下一层 | \(O\left(\frac{L}{B}\right)\)（每层每项仅被合并 **1 次**！） | \(O(T \cdot L)\) | \(O(T)\)（大量重复旧版本并存） | 偏极致写吞吐，容忍空间与多路读（Cassandra STCS, ScyllaDB） |
| **Monkey（非均匀 Bloom）** | 适用 Leveling / Tiering | 不变 | 将点查假阳性期望压至指数级更低 | 固定总内存预算 | 高层组件体积指数级更小，给高层分更多 bits/key 收益远胜平均分配 |
| **Dostoevsky（惰性分层）** | 上层 `1..L-1` 用 Tiering，最底层 `L` 用 Leveling | \(O\left(\frac{L + T}{B}\right)\) | 混合受控 + Monkey 过滤 | \(O\left(1 + \frac{1}{T}\right)\)（90% 数据在最底层！） | 同时拿走 Tiering 的低写放与 Leveling 的低空间放大及单段底层读 |

除了梳理复杂度谱系，这篇综述对一线存储工程师还有两处极有价值的实战警示：

1. **次级索引与组件过滤器的“旧值复活”一致性陷阱（PDF p. 18）**：在带主键 `k` 与可更新属性 `ts`（如时间戳）的表中，引擎常为每个不可变 SST 组件记录 `[min_ts, max_ts]` 区间过滤器以加速时间范围裁剪：
   > "Thus, a component can be pruned by a query if the search condition is disjoint with the minimum and maximum values of its filter." *(PDF p. 18)*
现在假设磁盘旧组件 \(C_1\) 里躺着记录 `(k1, valA, ts = T1)`；随后用户把 `k1` 更新为 `(k1, valB, ts = T4)` 写入内存新组件 \(C_0\)。
如果 \(C_0\) 的时间过滤器天真地只记录新值 `[T4, T4]`，那么当一条查询检索 **`WHERE ts <= T2`** 时，执行器看到 \(C_0\) 的区间 `[T4, T4]` 与 `<= T2` 不相交，直接把包含更新/墓碑的 \(C_0\) 整块剪枝跳过，转头从旧组件 \(C_1\) 里把早已作废的 `(k1, valA, T1)` 当作有效记录查了出来！要堵死这个致命 Bug，写入新值或反物质墓碑（Antimatter / Tombstone）时，新组件的过滤器区间必须**同时覆盖被覆写旧记录的属性值 `T1` 与新值 `T4`**（或者维护主键级可见性校验）。
2. **论文评测中的“未调优 RocksDB 稻草人”反模式（PDF p. 20–21）**：许多新索引论文用默认 `T=10` Leveling 的 RocksDB 跑重写入负载，宣称自己写吞吐高几倍、读性能差不多——却绝口不提只要把 RocksDB 调成 Tiering 或调小 \(T\)、配上 Monkey 非均匀 Bloom 预算，就能在同一条 RUM 曲线上换出同等写吞吐。
   公正比较必须在相同缓存预算、相同空间上限与匹配调参下对比整条帕累托曲线。

---

### Fractal Tree 对比 LSM-Tree：用内节点消息缓冲保住单树范围扫描 {#paper-56}

> **论文信息**：Bradley C. Kuszmaul. *A Comparison of Fractal Trees to Log-Structured Merge (LSM) Trees*. Tokutek White Paper, 2014, 共 15 页. [原始 PDF（共 15 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/storage-structure/a-comparison-of-fractal-trees-to-log-structured-merge-%28lsm%29-trees.pdf)（核心图示见 p. 6–7 `Figure 2–3` 内节点消息缓冲级联刷盘与 p. 12 `Figure 8` 10 TB 数据库渐近及具象模型对比）。

既然 LSM-Tree 的读放大根源在于“同一键域分散在 \(L\) 层乃至 \(T \cdot L\) 个互不统属的有序运行段（Runs）里，短范围扫描无法靠 Bloom 过滤器脱身，只能对所有运行段做多路归并”，那么**能不能只维护唯一一棵全局有序树，却依然享受到批量顺序写的摊销红利？**

源自缓存无关外存理论 **\(B^\epsilon\)-Tree**、由 Tokutek（TokuDB / TokuMX）商业化的 **分形树（Fractal Tree）** 给出了肯定回答。
与传统 B+-Tree 内节点只存路由分隔键与子节点指针不同，Fractal Tree 把每个树节点放大为数 MB 的大块 \(B\)（例如写块 `4 MB`，读时可按 `64 KB` 压缩子块 ` Basement Node` 解压读取），把内节点扇出控制在较小的 \(k\)（如 \(k = \sqrt{B}\) 或工程取值 \(k \approx 10 \sim 16\)），并将每个内节点剩余的绝大部分空间开辟为**消息缓冲区（Message Buffer）**。

写入、更新或删除不再一路沿着树穿透到叶子页，而是封装成一条轻量消息直接塞进根节点缓冲区便立即返回（配合 WAL 持久化）。当某个内节点的缓冲区攒满容量 \(B\) 时，它挑选名下积压消息最多的那个子节点，一次性至少向下级联冲刷（Flush）**\(B / k\) 条消息**：

> "Moving B/k objects down the tree incurs the write cost of B, for write amplification per level of k, and a total write amplification of O(klogk N/B)." *(PDF p. 7)*

算一下这笔账：把 \(B / k\) 个对象推下一层需要重写大小为 \(B\) 的子节点块，因此每下推一层，单个字节承受的写放大恰好是 \(\frac{B}{B / k} = k\)；
穿过树高 \(\log_k(N / B)\) 层到达叶子后，总字节写放大为 \(\Theta\left(k \log_k \frac{N}{B}\right)\)——**在渐近意义上与容量比 \(k\) 的分层（Leveled）LSM-Tree 完全同阶**，且一旦遇到局部顺序或热点倾斜写入，同一个最满子分支一次能捎带远超 \(B/k\) 条消息，写放大更会自动退化逼近 \(\Theta(\log_k(N/B))\)！

而它换回的核心回报落在**短范围查询（Short Range Scan）**上。因为全库始终只有**一棵树**，所有悬而未决的更新消息都严格挂载在根到目标叶子的唯一搜索路径上！当所有内节点（约占总库体积的 \(1/k\)，或热路径上层内节点）缓存在内存中时，查一个局部键区间只需顺着唯一路径把沿途内节点缓冲里的相关消息在内存里合并，最终只做 **1 次叶子子块 I/O** 即可拿到精确结果。
白皮书在 `10 TB` 数据库配 `64 GB` 内存的模型推演中指出，为了压低写放大而采用 Size-Tiered LSM 会让短范围扫描被迫去磁盘摸十几个运行段：

> "In my experience, it is an unusual application that is so short on write bandwidth that it can gladly give up a factor of 13 on read amplification." *(PDF p. 12)*

当然，从第一性原理审视这篇白皮书，我们也必须看清 Fractal Tree 为“单树消息化”付出的真实工程代价（这也是为何后来开源生态中 RocksDB 系 LSM 依然占据主流的原因）：首先，白皮书横向表格把字节写放大与**I/O 次数读放大**混列，而 Fractal Tree 一次叶节点读取拉起的是 `64 KB` 乃至更大的块，随机小点查实际搬运的闪存字节数高于 4 KB 页引擎；
其次，每次满缓冲级联下推都要原位读改写数 MB 的内节点并维护树级锁/日志，在多核并发查找与内节点超出内存预算（冷内节点路径）时，路径消息解码与大块锁同步的 CPU 复杂度显著高于不可变 SSTable 的无锁只读扫描。

---

### SuRF：用紧凑双层前缀树填补短范围查询的过滤盲区 {#paper-62}

> **论文信息**：Huanchen Zhang, Hyeontaek Lim, Viktor Leis, David G. Andersen, Michael Kaminsky, Kimberly Keeton, Andrew Pavlo. *SuRF: Practical Range Query Filtering with Fast Succinct Tries*. SIGMOD 2018, pp. 323–336. [原始 PDF（共 14 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/storage-structure/surf-practical-range-query-filtering-with-fast-succinct-tries.pdf)（核心图示见 p. 2–4 `Figure 1–3` LOUDS-DS 双层位图编码、p. 5 `Figure 4` 最小区分前缀与混合后缀变体及 p. 10–11 `Figure 12–13` RocksDB 100 GB 实测）。

如果不改用复杂的 Fractal Tree，继续留在简洁高效的不可变 SSTable 架构里，能不能直接造出一种**既像 Bloom 过滤器一样常驻内存只有十几个 bit/key，又能回答范围存在性查询**的新型过滤器？

CMU 与慕尼黑工大联手打造的 **SuRF（Succinct Range Filter）** 直击传统 LSM 的最大软肋：

> "Although Bloom filters are useful for single-key lookups (“Is key 42 in the SSTable?”), they cannot handle range queries (“Are there keys between 42 and 1000 in the SSTable?”)." *(PDF p. 1)*

原因很简单：哈希函数天生打碎了键的字典序（Lexicographical Order）（注：RocksDB 的 Prefix Bloom 虽能对固定长度前缀做哈希过滤，却无法处理任意开闭区间或变长范围边界）。
要保留偏序关系，最自然的数据结构是字典树（Trie）；但传统指针字典树每个节点动辄几十字节指针，内存膨胀十倍以上；而学术界已有的紧凑位图字典树（如纯 `LOUDS-Sparse` 的 `tx-trie`）每次向下走一层都要在大位图上跑多次二分搜索或 `rank/select` 位计数，比 Bloom 过滤器慢出一个数量级。

SuRF 的第一项核心贡献是底层引擎 **快速紧凑字典树（Fast Succinct Trie, `FST`）** 的双层混合编码（`LOUDS-DS`，见 `Figure 2`，PDF p. 3）：

- **上层热节点采用 `LOUDS-Dense`（每节点 3 组 256-bit 位图）**：字典树靠近树根的前几层节点极少、扇出极高，却承接了 100% 的查询流量。
  `LOUDS-Dense` 为每个节点直接分配 `D-Labels`（哪些字节分支存在）、`D-HasChild`（该分支是叶子还是内节点）和 `D-IsPrefixKey` 三个定长位图，查找某字节分支只需 **1 次直接位测试**加常数表加速的 `rank1` 计算子节点下标，速度媲美数组直访！
- **下层海量稀疏节点切换为 `LOUDS-Sparse`（每分支仅 `10 bits`）**：一旦层规模跨过容量阈值（默认上下层大小比 \(R = 64\)），自动切为紧凑字节数组 `S-Labels`（8b）+ `S-HasChild`（1b）+ `S-Louds`（1b 节点边界标志），配合 `select` 采样表把平均每键空间死死按住。
  正因兼顾了上层吞吐与下层体积，`FST` 才得以甩开前人实现：

  > "Although tx-trie shares the LOUDS-Sparse design with FST, it is slower without the performance boost from LOUDS-Dense and other optimizations." *(PDF p. 7)*

看看 `LOUDS-Dense` 在上层热节点走一步字节转移只需几条无指针位运算，就能明白它为何比传统压缩字典树快 4~15 倍：

  ```cpp
  // LOUDS-Dense 热层单步下探：每个节点占 256 bits (4 个 uint64_t)，零指针解引用
  struct LoudsDense {
      const uint64_t *dLabels, *dHasChild, *dIsPrefixKey;

      // 在第 nodeNum 号节点查找字节 label (0..255)
      bool moveToChild(uint32_t& nodeNum, uint8_t label, bool& isLeaf) const {
          uint64_t bitPos = (uint64_t(nodeNum) << 8) + label; // 256 * nodeNum + label
          if (!testBit(dLabels, bitPos)) return false;        // 1 次位测试即判不存在！
          if (!testBit(dHasChild, bitPos)) {
              isLeaf = true;                                  // 命中截断前缀叶分支
              return true;
          }
          // 预计算 64-entry popcount 查找表在 O(1) 时间内算出目标子节点编号
          nodeNum = rank1Fast(dHasChild, bitPos);
          isLeaf  = false;
          return true;
      }
  };
  ```

但光把全量完整长键塞进 `FST` 还是太大（比如 64 字节 URL 键）。SuRF 的第二项核心贡献是**最小区分前缀截断（Minimum Distinguishing Prefix）与可控后缀位扩展**（`Figure 4`，PDF p. 5）：

1. **`SuRF-Base`（零假阴性保证）**：构建不可变 SSTable 时，所有键本就严格有序排列。SuRF 将每个键截断为**刚好能与紧邻左右两个键区分开的最短前缀**（例如在 `... "sigmod", "sigops", "vldb" ...` 中，`"sigops"` 截断为 `"sigo"`）。
   由于任何真实存在于该 SSTable 且落在查询区间 \([l_k, h_k]\) 内的完整键 \(k\)，其截断前缀在字典树上的路径必定不会跳出 \([l_k, h_k]\) 对应的边界子树，因此 **SuRF 严格保证 0% 假阴性（Zero False Negatives）**——绝不会漏掉任何真实包含目标记录的 SSTable！
2. **后缀位精细调参（`SuRF-Hash` / `SuRF-Real` / `SuRF-Mixed`）**：截断前缀虽然保证不漏检，但当查询落在边界截断键附近时会产生**假阳性（False Positive）**。
   为此，SuRF 支持在每个叶子后追加 \(n\) 个额外比特：追加 \(n\) 位键哈希值（`SuRF-Hash`）能大幅压低纯点查假阳性，却帮不上范围边界比对；
   追加 \(n\) 位紧随截断处之后的**真实键比特（`SuRF-Real`）**则能继续收紧范围上下界判定；`SuRF-Mixed` 折中各取一半。

在 `100 GB` RocksDB 数据集（压缩后落盘 `52 GB` SSD，单机内存仅够缓存约三分之一热块，统一给过滤器分配约 `14 bits/key` 内存预算）的端到端压测中，取舍边界异常清晰：

- 在开放短范围扫描（`Seek` 后取若干条）与 99% 为空的闭合区间探测上，替换 Bloom 为 4-bit `SuRF-Real` 让系统总吞吐分别提升 **1.57×**（`1.97 → 3.10 Kops/s`）与最高 **5×**，大量原本必吃的底层 SSTable 磁盘随机读被当场拦截；
- 反过来，在**纯不存在键的单点查询（Negative Point Lookup）**负载下，由于通用字典树无法像多重哈希 Bloom 那样在全部键值分布上均匀摊平碰撞，SuRF 吞吐比同内存的 14-BPK Bloom 低约 **40%~50%**（`19.8 / 16.4 Kops/s` vs Bloom `32.9 Kops/s`）。
  因此，SuRF 并非无条件消灭 Bloom，而是范围 `Seek` 密集型 LSM 负载的专用利器。

---

### From WiscKey to Bourbon：为什么不可变 SSTable 是学习索引的天选载体 {#paper-59}

> **论文信息**：Yifan Dai, Yien Xu, Aishwarya Ganesan, Ramnatthan Alagappan, Brian Kroth, Andrea C. Arpaci-Dusseau, Remzi H. Arpaci-Dusseau. *From WiscKey to Bourbon: A Learned Index for Log-Structured Merge Trees*. OSDI 2020, pp. 155–171. [原始 PDF（共 18 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/storage-structure/from-wisckey-to-bourbon%3A-a-learned-index-for-log-structured-merge-trees.pdf)（核心图表见 p. 5 `Figure 2` 快介质下索引查找占比、p. 6–7 `Figure 3` 与 `Table 1` SST 寿命分析、p. 10 `Figure 6` 七步查找流水线及 p. 15 `Figure 17` 误差界消融）。

用过 SuRF 或 Bloom 挡掉了不包含目标键的 SSTable 之后，对于**真正包含目标键（或通过了过滤器）的那张 SSTable**，引擎接下来要干什么？答案是：先二分查找文件级索引块（Index Block）定位数据块（Data Block），从 SSD 读出整个 4 KB 数据块，再在数据块内二次二分查找目标键。
在慢速 HDD 或早期 SATA SSD 年代，这点内存二分查找耗时完全被几百微秒的设备 I/O 淹没；可一旦换到超低延迟的 Optane SSD 或缓存命中路径（`Figure 2`，PDF p. 5），**索引块与数据块查找（Search）加上过宽块读取居然占到了单次点查总耗时的近一半**！

与此同时，Kraska 等人在 2018 年提出的**学习型索引（Learned Index）**虽然能用简单线性回归函数 \(pos = f(key)\) 一步算出键在有序数组中的近似下标，却一直苦于**原地插入更新会不断打乱数组偏移、迫使模型高频重训**。
威斯康星大学团队敏锐地把这两块拼图扣在了一起：**LSM-Tree 是写密集引擎，但它的基本组成单元 SSTable 从落盘那一刻起就是绝对只读、永不原位修改的有序数组！**

> "We also realize that the immutable nature of sstable files makes them an ideal unit for learning. Once learned, these files are never updated and thus a model can be useful until the file is replaced." *(PDF p. 5)*

更妙的是，Bourbon 选择构建在键值分离的 **WiscKey** 之上：由于变长大 Value 已经分离存储在追加写 `vLog` 中，SSTable 里排列的全是严格定长的 **`(Key, vLog_Pointer)`**（例如 `16B + 8B = 24B`）记录！一旦模型预测出某键的序号区间 \([pos - \delta, pos + \delta]\)，乘以 24 字节就能**直接算出目标记录在 SSTable 文件里的精确字节区间**，连传统 4 KB 整块加载都缩减成了只读几百字节的微型 `LoadChunk`！对比传统 WiscKey 与 Bourbon 在单个已学习 SSTable 内部的查找路径（`Figure 6`，PDF p. 10）：

```cpp
// Bourbon 在单个已学习定长 SSTable（每项 ENTRY_SZ = 24B，最大误差 δ = 8）上的点查流程
bool bourbonLookupSST(const LearnedSST& sst, uint64_t key, VlogPtr* outPtr) {
    if (!sst.bloomFilter.mayContain(key)) return false;     // 1. Bloom 先挡绝大部分负查找

    // 2. 在分段线性回归模型（Greedy-PLR）中二分定位所属直线段 seg: pos ≈ slope * key + intercept
    const PlrSegment& seg = sst.findSegment(key);
    int64_t pred = std::clamp<int64_t>(seg.predict(key), 0, sst.numEntries - 1);
    size_t  lo   = (pred > 8) ? (pred - 8) : 0;
    size_t  hi   = std::min<size_t>(pred + 8, sst.numEntries - 1);

    // 3. 定长条目使槽位区间直接映射为精确文件字节偏移（至多 17 * 24B = 408B，远小于 4KB 块！）
    Entry24B chunk[17];
    size_t cnt = hi - lo + 1;
    pread(sst.fd, chunk, cnt * sizeof(Entry24B), lo * sizeof(Entry24B));

    // 4. 仅在 <= 17 个局部条目内做微型二分确认，命中后再去 vLog 读真实 Value
    return binarySearchChunk(chunk, cnt, key, outPtr);
}
```

模型体积仅增加总库空间的 `0% ~ 2.05%`，却一次性省掉了二级索引块查找与整块冗余 I/O。

但真正的系统级挑战在于：**该按“整层（Level）”训练，还是按“单个 SSTable 文件”训练？什么时候值得花 CPU 去训练？**

1. **为什么整层学习在写负载下必败无疑**：`L0` 文件键域互相重叠根本无序；即便在有序的 `L1..Ln`，只要后台发生一次局部 Compaction 替换了该层几百个文件里的两三个，整层的全局偏移下标就会发生连锁平移：
   > "Level learning cannot be applied at L0 because it is unsorted: files in L0 can have overlapping key ranges. Once a level is learned, any change to the level causes a re-learning." *(PDF p. 7)*
实测 50% 写入负载下，`L4` 整层每隔约 `25 秒` 就发生一次结构变更（论文统计整层学习尝试 66 次失败 66 次！），而单个 `L4` SSTable 文件的平均存活寿命却长达 **约 60 分钟**！因此，**文件级粒度（File-Granularity Learning）**是唯一可行的工程选择。
2. **50ms 等待窗口 + 成本收益分析器（Cost-Benefit Analyzer, `CBA`）**：高层（`L0/L1`）许多短命 SSTable 生成后几十毫秒就被合并销毁了，而训练一个 SSTable 的 PLR 模型大约耗时 \(T_{\text{build}} \approx 40\text{ ms}\)——盲目落地即训会白白烧光后台 CPU。
   Bourbon 先让新生成的 SSTable 静置等待固定阈值 \(T_{\text{wait}} = 50\text{ ms}\)（在线竞争分析中相对离线寿命神谕 2-competitive 的经典等待策略），筛掉夭折文件；
   随后由 `CBA` 根据同层历史同辈文件的平均存活期内负查找次数 \(N_n\)（在过滤器或微块处终止的内部查找）与正查找次数 \(N_p\)，对比基线路径（`.b`）与模型路径（`.m`）的单次实测微秒耗时，仅当预期累计节省超过构建成本时才放行后台异步训练（训练未完成前查询无缝走原生二分回退路径）：

   $$B_{\text{model}} = (T_{n.b} - T_{n.m}) \cdot N_n + (T_{p.b} - T_{p.m}) \cdot N_p > C_{\text{model}} = T_{\text{build}}$$

实验显示（PDF p. 11–16）：在只读与真实分布数据集（Amazon、OSM 经纬度、YCSB）上，Bourbon 将查找吞吐提升了 **1.23× 至 1.78×**（在超快 Optane SSD 上依然稳拿 **1.25×~1.28×** 端到端收益）；
而在 50% 读写混合压测下，`CBA` 将后台学习总 CPU 时间从盲目全训的 `134 秒` 狠砍了近 **10 倍**降至 **`13.9 秒`**，前台写吞吐零退化。

---

### CaaS-LSM：存算分离架构下的无状态近存合并与源端原子提交 {#paper-57}

> **论文信息**：Qiaolin Yu, Chang Guo, Jay Zhuang, Viraj Thakkar, Jianguo Wang, Zhichao Cao. *CaaS-LSM: Compaction-as-a-Service for LSM-based Key-Value Stores in Storage Disaggregated Infrastructure*. PACMMOD (SIGMOD 2024), 2(3): 124, 共 28 页. [原始 PDF（共 28 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/storage-structure/caas-lsm%3A-compaction-as-a-service-for-lsm-based-key-value-stores-in-storage-disaggregated-infrastructure.pdf)（核心图示见 p. 4 `Figure 2` 存算分离写停顿、p. 10–11 `Figure 6–7` 控制面/无状态 CSA 架构与源端安装协议及 p. 14–15 混合回退调度）。

把单机 LSM-Tree 搬上今天云厂商标配的**存算分离（Storage Disaggregation）**底座后，一个新的分布式痛点立刻浮出水面：前台计算节点通过数据中心网络挂载远端共享云盘或分布式块/文件存储，而 LSM 的后台 Compaction 恰恰是吞吐黑洞：

> "Since compaction creates most of the I/Os from LSM-KVS (e.g., can be as high as 90% [70]), achieving near- or in-storage compaction is the main focus of related studies" *(PDF p. 2)*

如果不做卸载，计算节点为了做一次多路归并，必须先把几十 GB 的旧 SSTable 跨网拉回计算侧解压归并，再把新 SSTable 跨网写回远端存储——双倍网络带宽争抢直接卡死前台 `MemTable` 刷盘，诱发剧烈的写停顿（Write Stall）与 P99 尾延迟毛刺（`Figure 2` 在 Apache Kvrocks 上实测尤为明显）。
而早期远程 Compaction 尝试（如静态绑定远端 Worker 并每次远程 `Open DB` 加载全套元数据）不仅准备阶段极慢（`Figure 5`），一旦远端 Worker 饱和或宕机还会把前台实例一起拖垮。

**CaaS-LSM** 给出了一套职责切分极其克制、可直接指导生产落地的工业级架构（已深度对齐 RocksDB `CompactionService` 演进路线）：

1. **自包含无状态执行面（Stateless CSA）与源端独占原子安装边界**：谁拥有数据库版本一致性？**永远只有源端 LSM-KVS 实例自己**！当源实例挑选出待合并的输入 SST 集合后，它将本次归并所需的列族选项（Options）、活跃快照列表（Snapshots，决定哪些旧版本和墓碑可以被安全物理清理）、自定义 `MergeOperator` / `CompactionFilter` 序列化打包成一份**自包含任务包（Self-Contained `CompactionServiceInput`）**，发往贴近存储池部署的无状态合并代理（Compaction Service Agent, **`CSA`**）。
   `CSA` 无需执行沉重的完整 `Open DB`，直接在近存端流式读取输入 SST、完成 CPU 密集解压归并压缩、将临时输出文件写入共享存储，随后只把轻量级输出文件元数据 `CompactionServiceResult` 回传给源实例。
   **最终的结果校验、文件原子重命名、向 `Manifest` 注册新版本集以及清理旧 SST，100% 留在源端实例串行完成**——这样哪怕 `CSA` 中途崩溃或网络分区，至多丢弃几个未被引用的临时孤儿文件，绝无可能污染数据库状态！
2. **基于 EWMA 的混合本地/远程自适应回退（Hybrid Local-Remote Fallback）**：全局控制面按优先级（手动 > `L0/L1` 防停顿 > 高分深层任务）调度并过滤掉 CPU 或内存超 `80%` 水位的过载 `CSA`（靠 2 秒内轻量心跳探活），同时用指数加权移动平均（EWMA）持续追踪每兆字节 SST 的实际处理速率倒数：

   $$\text{Rate}_{\text{new}} = \alpha \cdot \text{Rate}_{\text{old}} + (1 - \alpha) \cdot \frac{T_{\text{actual}}}{\text{SstSize}}, \qquad T_{\text{estimated}} = \text{Rate}_{\text{current}} \times \text{CurrentSstSize}$$

最精髓的是它从不迷信“远程一定比本地快”，而是把本地线程池与远程 `CSA` 纳入统一的闭环判据：

   > "1) If the local thread pool utilization is too high (e.g., more than 95%), execute the job remotely; 2) If a message indicating a lack of CSA resources is received, execute the job locally; 3) If the estimated execution time of the local compaction execution is shorter than remote execution, execute the job in its LSM-KVS instance." *(PDF p. 15)*

在多租户 RocksDB `db_bench` 与真实分布式 Redis 兼容引擎 **Apache Kvrocks** 压测中，这套混合闭环将多实例总吞吐拉升了 **45% 至 61%**；
尤其在跨数据中心或远端 `CSA` 短暂拥塞场景下，纯远程卸载因排队阻塞反被拖慢，而 CaaS-LSM 混合调度在提升 **56% OPS** 的同时把 P99 写尾延迟直接削减了 **76%**。

---

## 第三幕：夺回纯内存速度——面向高速 NVMe 的页式缓冲管理革命

看完了异位写 LSM 家族，让我们转身回到原位更新页式引擎（B+-Tree）的主战场。第一幕的 `#55` 已经算出：在百万 IOPS 的现代 PCIe NVMe SSD 阵列面前，DRAM 与闪存的盈亏间隔已跌至一分钟左右。
慕尼黑工大 Viktor Leis 与 Thomas Neumann 团队连发两篇里程碑之作——**LeanStore (`#60`)** 与 **Umbra (`#64`)**——向延续了四十年的教科书级 `Buffer Pool Manager` 发起了总攻：**当工作集热页在内存里时，页式存储引擎凭什么不能跑得和纯内存数据库一模一样快？**

![现代 NVMe 缓冲管理器演进：从 LeanStore 指针混洗到 Umbra 变长页零页回收](/images/diagrams/paper-store-leanstore-to-umbra.svg)

### LeanStore：用指针混洗、冷却队列与乐观版本锁拆掉全局缓冲池哈希表 {#paper-60}

> **论文信息**：Viktor Leis, Michael Haubenschild, Alfons Kemper, Thomas Neumann. *LeanStore: In-Memory Data Management Beyond Main Memory*. ICDE 2018, 共 12 页. [原始 PDF（共 12 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/storage-structure/leanstore%3A-in-memory-data-management-beyond-main-memory.pdf)（核心图示见 p. 3 `Figure 2` Swip 混洗格式、p. 5 `Figure 4` 热/冷却/冷三态流转、p. 7 `Figure 6` Epoch 内存回收及 p. 9 `Figure 7` 10 核至 60 核逐项消融曲线）。

为什么全库 100% 缓存在内存里时，传统磁盘引擎依然比纯内存引擎慢几十倍？论文第一页一针见血地指出了病灶所在：

> "In the canonical buffer pool implementation [1], each page access requires a hash table lookup in order to translate a logical page identifier into an in-memory pointer. Even worse, in typical implementations the data structures involved are synchronized using multiple latches, which does not scale on modern multi-core CPUs." *(PDF p. 1)*

想象从 B+-Tree 根节点下探 4 层到达叶子页：纯内存树只需解引用 4 次裸指针；而传统缓冲池要连续做 **4 次全局哈希表查找**、**4 次引用计数/页锁原子写**外加 **4 次 LRU 双向链表或时钟位更新**——在 60 核多插槽机器上，根页和上层内节点的锁字与 LRU 元数据缓存行会在所有核心之间疯狂乒乓失效（Cache-Line Ping-Pong）！

LeanStore 用三记环环相扣的重拳，把热路径上的哈希表、LRU 追踪和悲观读锁**全部清零**：

1. **单拥有者树形指针混洗（Tree-Structured Pointer Swizzling, `Swip`）**：早在面向对象数据库年代就有“把磁盘 `PageID` 原地替换成内存虚拟地址指针”的指针混洗概念，但过去之所以极其丑陋，是因为任意图状引用（包括经典 B+-Tree 叶子间的**左右兄弟指针**！）会让同一个页面被多处混洗引用，一旦淘汰该页就不得不反向追踪全图所有入边去解混洗（Unswizzle）。
   LeanStore 立下一条铁律：**彻底砍掉 B+-Tree 的横向兄弟指针**（范围扫描改靠父节点下发的上下界 Fence Keys 导航），从而保证除全局根之外，**内存中每个页面有且仅有一个父节点持有指向它的 64 位 `Swip`**，且只要一个内节点还持有已混洗的子页，该父节点就禁止先于子页淘汰！在 64 位 `Swip` 中仅借用 1 个标志位区分状态：当 `tag == 0`（热态）时，它就是一个纯粹的 64 位内存裸指针，B+-Tree 下探热路径退化为 **1 条高度可预测的位测试分支加直接内存解引用**，全局缓冲池哈希表在热路径上彻底消失！
2. **投机解混洗与 10% 冷却 FIFO 队列（Speculative Unswizzling & Cooling Stage）**：既然热页访问连缓冲池代码都不经过，引擎怎么知道哪些页变冷了该淘汰？难道每次读页面还要去改一个访问位？LeanStore 的回答是：**热页访问什么统计都不记！** 后台/工作线程需要腾空间时，直接随机抽样挑选热页，将其父节点里的 `Swip` 原子改回未混洗的逻辑 `PageID`（**投机解混洗**，此时并不写盘也不释放内存），并把该页送入占缓冲池总容量约 **10%** 的 **冷却 FIFO 队列（Cooling Stage，配一张仅索引这 10% 页面的小型 `PageID` 哈希表）**。
   精妙之处在于：如果被抽中的其实是个高频热页，几微秒后就会有查询踩到这个刚变回 `PageID` 的 `Swip`，随即在冷却队列小哈希表里**零磁盘 I/O 瞬间命中并立即重新混洗（Re-swizzle）回热态**！只有真正无人问津的冷页才会一路滑到 FIFO 队尾（若脏则刷盘）被真正驱逐。
   轨迹模拟显示（PDF p. 11），10% 冷却窗口的命中率高达 **`92.8%`**，与完美全局 LRU（`93.1%`）几乎分毫不差，却消灭了 99% 以上的每次读统计写争用！
3. **乐观版本锁 + In-Flight 缺页合并 + Epoch 帧回收**：为了消除上层内节点的读锁写争用，LeanStore 采用乐观锁耦合（Optimistic Lock Coupling）：读者下探时**完全不改写任何锁字**，仅读取页面 64 位锁版本头并在访问完后校验版本号未变（若遇并发写或页面分裂导致版本变化，重读重试即可）。
   万一顺着未混洗 `Swip` 撞上真正的冷页缺页，不能让十几个并发线程对同一个 `PageID` 发起十几次重复 `pread`：
   > "we maintain a hash table for all pages currently being loaded from disk (P3 in the figure). The hash table maps page identifiers to I/O frames, which contain an operating system mutex and a pointer to a newly allocated page." *(PDF p. 6)*
并发缺页者挂在同一 `I/O frame` 互斥锁上合并等待，读完混洗后通过轻量级异常回退重试。最后，由于乐观读者不加锁就解引用裸指针，万一某页刚被解混洗驱逐、物理内存帧就被挪给别的新页岂不读错内存？LeanStore 引入基于线程局部 **Epoch（纪元）** 的安全回收：被淘汰的 `16 KB` 页面帧先打上当前全局纪元放入待回收链，只有当全体活跃工作线程的局部纪元均已跨过该退役纪元后，该物理帧才允许重分配复用。
将 `Swip` 解引用与乐观锁耦合组合在一起，B+-Tree 热路径下探精简到了令人发指的程度：

   ```cpp
   // LeanStore 热路径无锁乐观下探：零哈希表查找、零 LRU 更新、零缓存行原子写！
   Page* traverseToLeafOptimistic(Swip<Page> rootSwip, Key k) {
   restart:
       EnterEpochGuard epochGuard;                  // 仅更新线程本地纪元，防止活跃帧被提前复用
       Page* parent = rootSwip.asHotPtr();
       uint64_t pVer = parent->readOptimisticOrRestart(); // 读版本号，若正被排他写则自旋/重启

       while (parent->isInner()) {
           Swip<Page> childSwip = parent->lookupChildSwip(k);
           if (UNLIKELY(!childSwip.isHot())) {      // 罕见慢路径：命中冷却 FIFO 或合并 pread
               parent->checkVersionOrRestart(pVer); // 先确认刚才读出的 childSwip 不是脏垃圾
               resolveColdOrCoolingSwip(parent, childSwip);
               goto restart;
           }
           Page* child = childSwip.asHotPtr();      // 热路径：单条位判断后直接拿到裸内存指针！
           uint64_t cVer = child->readOptimisticOrRestart();
           parent->checkVersionOrRestart(pVer);     // 乐观锁耦合交接：校验父节点在读取期间未分裂修改
           parent = child;
           pVer   = cVer;
       }
       return parent;
   }
   ```

论文 `Figure 7`（PDF p. 9）在 10 核机器上跑内存驻留 TPC-C（聚焦存储层，关闭跨事务日志瓶颈）给出了教科书级的逐项消融阶梯：传统哈希表缓冲池仅跑出 **`18K TPS`** → 单独加上指针混洗微升至 **`23K TPS`**（被 LRU 锁卡死）→ 换上冷却队列淘汰跃升至 **`109K TPS`**（被悲观读写锁卡死）→ 最后换上乐观版本锁直接轰出 **`597K TPS`**（单线程 `67K` 对比纯内存基线 `69K`，追平裸内存 **97%** 的极限速度；60 核 NUMA 机器更一路线性冲上 **`2.56M TPS`**，外存超内存配置连接 4 块 NVMe RAID0 跑满设备带宽上限）。

---

### Umbra：变长尺寸类、MADV_DONTNEED 零页回收与 16 字节德式字符串 {#paper-64}

> **论文信息**：Thomas Neumann, Michael Freitag. *Umbra: A Disk-Based System with In-Memory Performance*. CIDR 2020, 共 7 页. [原始 PDF（共 7 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/storage-structure/umbra%3A-a-disk-based-system-with-in-memory-performance.pdf)（核心图示见 p. 2–3 `Figure 1–2` 变长尺寸类匿名 `mmap` 与 Swip 编码、p. 4 `Figure 3` 混合版本锁、p. 5 `Figure 4–5` 16B German String 与自适应编译及 p. 6 `Figure 6` JOB/TPC-H 实测）。

LeanStore 完美解决了定长 `16 KB` 页面的 OLTP 负载，但当 Neumann 团队着手构建接替纯内存引擎 HyPer 的新一代盘基分析型系统 **Umbra** 时，定长小页面立刻撞上了 OLAP 的现实痛点：现代分析查询里塞满了长 JSON/文本字符串、几十 KB 的列式压缩字典以及超宽表元组——如果硬拆成一串 `16 KB` 溢出链表页，大扫描与大对象访问会退化得惨不忍睹；
可如果在同一块内存池里混排 `64 KiB`、`128 KiB`、`256 KiB`、`512 KiB`……指数级增长的**变长尺寸类页面（Variable-Size Page Classes, \(64\text{ KiB} \cdot 2^c\)）**，淘汰两个 `64 KiB` 页如果不相邻根本凑不出一个连续的 `128 KiB` 槽位，严重的**外部内存碎片（External Fragmentation）**与 LeanStore 的定长帧 Epoch 池立刻陷入死局！

有人会问：既然操作系统页表本来就能把不连续的 `4 KiB` 物理页拼成连续虚拟地址，为什么不干脆直接对数据库文件调 `mmap`？之前 Andy Pavlo 与 Viktor Leis 早已系统论证过直接文件 `mmap` 在 DBMS 里的灾难（OS 随时偷刷未写 WAL 的脏页破坏事务原子性、缺页中断阻塞工作线程无法调度 Morsel 等）。
Umbra 给出的解法堪称神来之笔——**向操作系统借“虚拟地址翻译与零页”，但把“磁盘 I/O 与淘汰控制权”100% 攥在数据库自己手里**！

假设物理内存预算为 \(M = 32\text{ GiB}\)，共设 \(K\) 个指数尺寸类（`64 KiB, 128 KiB, ...,` 直至单页数 MB）。
Umbra 在启动时为**每一个尺寸类各自调用一次私有匿名映射 `mmap(MAP_PRIVATE | MAP_ANONYMOUS)`，每个区都豪横地预留整整 \(M = 32\text{ GiB}\) 的虚拟地址空间**！因为在 64 位 Linux 上，`256 TiB` 用户态虚拟地址空间几乎免费，只要不触碰，这 \(K \times 32\text{ GiB}\) 虚拟区消耗的物理内存是 **0 字节**。
对应的未混洗 64 位 `Swip` 编码为 `[63..7: 57-bit PageID | 6..1: 6-bit SizeClass | 0: Tag=1]`（混洗后最低位 `Tag=0` 即为 8 字节对齐的真实虚拟地址）：

```cpp
// Umbra 变长页缓冲池核心生命周期：私有匿名虚拟地址区 + 显式 I/O + MADV_DONTNEED 零页归还
struct UmbraBufferPool {
    char* regions[NUM_CLASSES]; // 每个尺寸类 c 独立 mmap(nullptr, budgetM, ..., MAP_PRIVATE|MAP_ANONYMOUS)

    void* loadPage(uint64_t pid, uint8_t sizeClass, size_t slotIdx) {
        size_t pageBytes = (64ULL << 10) << sizeClass;   // 64 KiB * 2^sizeClass
        char*  vaddr     = regions[sizeClass] + slotIdx * pageBytes;
        pread(dbFd, vaddr, pageBytes, diskOffset(pid));  // 内核按需分配物理 4KiB 帧，零外碎片
        return vaddr;                                    // 最低位必为 0，直接写入父节点 Swip！
    }

    void evictPage(void* vaddr, uint64_t pid, uint8_t sizeClass, bool isDirty) {
        size_t pageBytes = (64ULL << 10) << sizeClass;
        if (isDirty) pwrite(dbFd, vaddr, pageBytes, diskOffset(pid)); // 数据库严格掌控 WAL 与刷盘时机
        // 归还物理页帧给 OS，同时保持 vaddr 合法并自动指向全局只读零页（Zero Page）！
        madvise(vaddr, pageBytes, MADV_DONTNEED);
    }
};
```

这一招一石三鸟，顺手连 LeanStore 的全局 Epoch 内存回收停顿都一并干掉了：

1. **彻底根除变长页外部物理碎片**：`MADV_DONTNEED` 立即将该 `64 KiB` 背后的 16 个物理 `4 KiB` 页帧归还给 Linux 内核空闲池，紧接着在 `256 KiB` 虚拟区 `pread` 时内核随时可以把这些离散物理帧重新映射成连续的 `256 KiB` 虚拟槽，**零数据搬移紧缩**！
2. **共享零页（Zero Page）兜底，乐观迟读永不段错误**：在私有匿名映射上执行 `MADV_DONTNEED` 不会解除虚拟地址映射（不会像 `munmap` 那样触发致命 `SIGSEGV`），而是把这段虚拟地址重新指向内核全局只读的**共享零页**：
   > "read accesses to a memory region that was marked with the MADV_DONTNEED flag simply result in zero bytes. No additional physical memory is allocated in this case, as all such accesses are mapped to the same zero page." *(PDF p. 4)*
假如有个慢半拍的乐观读者还攥着刚被淘汰的旧指针去读页面，它只会平平安安读到一串全零字节（包括锁头版本号也变成 0，且不会触发物理内存分配！），紧接着在乐观版本校验处发现版本号不匹配，干净利落地丢弃脏读并回到父节点重试！
3. **混合共享/乐观版本锁与同尺寸类 ARIES 恢复防线**：全靠异常重启的纯乐观读对简单 B+-Tree 点查很爽，但要让整条复杂分析型连接/聚合算子流水线处处支持半途回滚异常会令编译器苦不堪言。
   Umbra 将 64 位页面锁升级为 `[59-bit Version | 5-bit State (Exclusive + Shared Reader Count)]`，允许扫描复杂页的算子退化加轻量共享读计数锁固定页面。
   同时在磁盘空闲空间管理上，Umbra 严禁把淘汰释放的一个 `128 KiB` 磁盘块直接拆成两个 `64 KiB` 新页复用——否则崩溃恢复跑 ARIES 重放日志时，旧 `128 KiB` 页面残留在 `64 KiB` 偏移处的数据体可能碰巧被当成第二个 `64 KiB` 页的头部 `LSN` 误读，破坏幂等恢复！

在记录与执行层，Umbra 还贡献了今天被 **DuckDB、Velox、Polars 与 Apache Arrow `StringView`** 竞相采纳的工业标准数据结构——**16 字节德式字符串（16-Byte German Strings）**：

```cpp
// Umbra 16 字节定长字符串描述符（现已演化为 DuckDB string_t / Arrow StringView）
struct GermanString {
    uint32_t length;                  // 0..3 字节：字符串精确字节长度
    union {
        char inlined[12];             // 情况 A (length <= 12)：全文直接内联，零指针跳转！
        struct {                      // 情况 B (length >  12)：
            char     prefix[4];       //   4..7  字节：前 4 字节前缀，寄存器一条整数比较快筛！
            uint64_t ptrWithTag;      //   8..15 字节：指向页内/缓冲池/临时堆 + 2-bit 生命周期标签
        } ref;
    };
};
```

> "Short strings that contain 12 or fewer characters are stored directly within the remaining 12 bytes of the string header, thus avoiding an expensive pointer indirection." *(PDF p. 5)*

不仅 `<= 12` 字节的常见短编码全程留在 16 字节元组槽内免除一切指针追逐，哪怕遇到上百字节长字符串，任何等值或范围过滤（如 `WHERE url LIKE 'https://...'` 或排序比较）都能先在 CPU 寄存器里直接比对 4 字节 `prefix`，95% 以上的不匹配项根本不用去碰远端变长数据区；
而指针内嵌的 2 位生命周期标签（持久化页 / 算子暂态缓冲 / 表达式临时串）更在编译期指导执行器何时可零拷贝传引用、何时在跨出页面锁作用域前必须深拷贝物化。
配合页内列簇布局（PAX）与自适应两阶段编译（先用零编译延迟字节码虚拟机 Flying Start 立即开跑小查询，遇到大表热点循环无缝后台切入 LLVM 机器码），Umbra 将整套变长缓冲管理相对裸 `mmap` 的平均性能损耗压进了 **`< 6%`**，冷缓存 SSD 扫描聚合跑到 `1.13 GiB/s`（贴近 `1.15 GiB/s` 裸盘物理极限），并在复杂连接基准 **JOB** 与 **TPC-H** 上分别以几何平均 **3.0×** 与 **1.8×** 击败了它的纯内存前辈 HyPer！

---

## 横向对比：十篇存储与缓冲管理经典论文的设计权衡全景

把这十篇横跨硬件经济学、理论猜想、异位写 LSM 演进与原位页式缓冲重构的代表作置于同一张对照表中，现代存储引擎设计空间里的每一处取舍都清晰可辨：

| 幕次 | 论文与年份 | 核心解决矛盾 | 关键机制 / 数学支点 | 代表量化收益（原文口径） | 代价转移与工程边界 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **第一幕** | **[#55](#paper-55) *Five-Minute Rule 30Y* (2017)** | 异构介质间的驻留盈亏分界 | \(t = \frac{\text{PagesPerMB} \times \$/\text{IOPS}}{\$/\text{MB}_{\text{RAM}}}\) 与 `$/TBscan` | DRAM–HDD 拉长至 `~4~5h`；DRAM–NVMe/Optane 缩至 **`~41s~1.5m`** | 单纯随机 IOPS 指标不适用磁带/SMR 冷层；闪存读写寿命非对称 |
| **第一幕** | **[#58](#paper-58) *RUM Conjecture* (EDBT 2016)** | 读/写/空间开销无法三全 | 归一化比率 `RO / UO / MO >= 1.0` 帕累托约束 | 三极玩具构造与 B+-Tree/Hash/ZoneMap/LSM 分类账本 | 属设计猜想而非刚性等式；压缩与模型先验把字节代价转嫁给 CPU |
| **第二幕** | **[#63](#paper-63) O'Neil *LSM-Tree* (1996)** | 温数据随机叶更新打爆磁臂 | \(C_0/C_1\) 多页块滚动合并 + 批量因子 \(M\) + **等比层比 \(r\)** | TPC-A 20 天索引硬件成本从 B-Tree `$56,400` 降至 **`$11,400`** | 点查与范围扫描需跨多组件合并；原位滚动游标实现复杂 |
| **第二幕** | **[#61](#paper-61) Luo & Carey *LSM Survey* (2020)** | 合并拓扑与次级索引正确性 | 不可变 SST + `no-steal` 仅 REDO + Leveling/Tiering/Monkey | Tiering 写降至 \(O(L/B)\)；Dostoevsky 兼顾底层 \(O(1+1/T)\) 空间 | 次级属性更新若不扩组件过滤边界会导致旧值“死而复生” |
| **第二幕** | **[#56](#paper-56) Kuszmaul *Fractal Tree* (2014)** | 多段 LSM 短范围扫描读放大高 | 单树 \(B^\epsilon\) 内节点消息缓冲（每次向最满子推 \(\ge B/k\) 项） | 写放同为 \(\Theta(k \log_k(N/B))\)，热内节点下短范围仅 **1 次叶 I/O** | 读子块粒度偏大（如 64KB）；多核下内节点消息解码与大块锁更重 |
| **第二幕** | **[#62](#paper-62) Zhang et al. *SuRF* (SIGMOD 2018)** | Bloom 无法过滤短范围 `Seek` | `LOUDS-Dense/Sparse` 紧凑前缀树 + 最小区分前缀（零假阴性） | 100GB RocksDB 开放/空闭范围吞吐提升 **`1.57× ~ 5×`** | 同 14-BPK 内存预算下，纯不存在单点查找比 Bloom 慢约 `40%~50%` |
| **第二幕** | **[#59](#paper-59) *Bourbon* (OSDI 2020)** | 快闪存/缓存下 SST 内部二分贵 | KV 分离定长项 + 文件级 `Greedy-PLR` (\(\delta=8\)) + `50ms` `CBA` | 点查提速 **`1.23×~1.78×`**；混合写下 `CBA` 省 **`~10×`** 学习 CPU | 依赖定长记录或额外间接层；极高频覆写高层短命 SST 不宜强训 |
| **第二幕** | **[#57](#paper-57) *CaaS-LSM* (SIGMOD 2024)** | 存算分离下合并抢占 90% I/O | 自包含无状态近存 `CSA` + **源端独占 Manifest 安装** + EWMA 回退 | 多实例吞吐 **`+45%~61%`**；跨中心 Kvrocks 尾延迟 **`-76%`** | 远端资源满载时需靠 `UseLocal` 兜底；极小任务网络往返不划算 |
| **第三幕** | **[#60](#paper-60) *LeanStore* (ICDE 2018)** | 全局页表哈希与 LRU/读锁卡死多核 | 单拥有者 `Swip` 混洗 + **10% 冷却 FIFO** + 乐观版本锁 + Epoch | 10 核 TPC-C 从传统池 `18K` 跃至 **`597K TPS`**（达裸内存 `~97%`） | 定长 16KB 页难承载分析型大字符串与大字典；纯乐观异常侵入算子 |
| **第三幕** | **[#64](#paper-64) *Umbra* (CIDR 2020)** | 盘基引擎兼顾变长大页与分析吞吐 | \(64\text{KiB}\cdot 2^c\) 匿名 `mmap` + **`MADV_DONTNEED` 零页** + 16B 德式串 | 缓冲开销 **`< 6%`**；JOB 与 TPC-H 比纯内存 HyPer 快 **`3.0× / 1.8×`** | 磁盘空闲块禁止跨尺寸类拆分复用，以防 ARIES 恢复误读旧中部 LSN |

纵观这三幕演进史，最迷人的地方莫过于两大家族在硬件演进驱动下的殊途同归：异位写 LSM 阵营拼命用 `Dostoevsky`、`SuRF` 与 `Bourbon` 把自己最痛的多段读放大往单树和常数定位方向修补，并在云上靠 `CaaS-LSM` 把后台归并剥离出去；
而原位页式阵营则靠 `LeanStore` 与 `Umbra` 拆掉了沉重的全局页表与悲观锁，让 NVMe 上的磁盘引擎跑出了超越早期纯内存数据库的速度。
理解了 `五分钟法则` 的硬件相对价格漂移与 `RUM` 的三轴守恒边界，面对任何新存储引擎架构时，只需追问一句“它把哪一轴的随机字节代价转嫁给了哪一层的顺序带宽或 CPU 指令”，一切复杂设计便迎刃而解。

---

## 参考文献与阅读基线

本文精读的所有论文均对齐开源论文集 [`lonng/db-papers`](https://github.com/lonng/db-papers)（Git 基线提交 `4ff1331`），文中引用的页码均为对应文件内的 PDF 物理页码：

1. **#55**：Raja Appuswamy, Renata Borovica-Gajic, Goetz Graefe, Anastasia Ailamaki. *The Five-Minute Rule Thirty Years Later and Its Impact on the Storage Hierarchy*. ADMS@VLDB 2017, 29 pages. [原始 PDF（共 29 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/storage-media/the-five-minute-rule-thirty-years-later-and-its-impact-on-the-storage-hierarchy.pdf)
2. **#58**：Manos Athanassoulis, Michael S. Kester, Lukas M. Maas, Radu Stoica, Stratos Idreos, Anastasia Ailamaki, Mark Callaghan. *Designing Access Methods: The RUM Conjecture*. EDBT 2016, 6 pages. [原始 PDF（共 6 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/storage-structure/designing-access-methods%3A-the-rum-conjecture.pdf)
3. **#63**：Patrick O'Neil, Edward Cheng, Dieter Gawlick, Elizabeth O'Neil. *The Log-Structured Merge-Tree (LSM-Tree)*. Acta Informatica 33(4): 351–385, 1996 (32 pages). [原始 PDF（共 32 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/storage-structure/the-log-structured-merge-tree-%28lsm-tree%29.pdf)
4. **#61**：Chen Luo, Michael J. Carey. *LSM-Based Storage Techniques: A Survey*. The VLDB Journal 29: 393–418, 2020 (arXiv 25 pages). [原始 PDF（共 25 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/storage-structure/lsm-based-storage-techniques%3A-a-survey.pdf)
5. **#56**：Bradley C. Kuszmaul. *A Comparison of Fractal Trees to Log-Structured Merge (LSM) Trees*. Tokutek White Paper, 2014 (15 pages). [原始 PDF（共 15 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/storage-structure/a-comparison-of-fractal-trees-to-log-structured-merge-%28lsm%29-trees.pdf)
6. **#62**：Huanchen Zhang, Hyeontaek Lim, Viktor Leis, David G. Andersen, Michael Kaminsky, Kimberly Keeton, Andrew Pavlo. *SuRF: Practical Range Query Filtering with Fast Succinct Tries*. SIGMOD 2018, pp. 323–336. [原始 PDF（共 14 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/storage-structure/surf-practical-range-query-filtering-with-fast-succinct-tries.pdf)
7. **#59**：Yifan Dai, Yien Xu, Aishwarya Ganesan, Ramnatthan Alagappan, Brian Kroth, Andrea C. Arpaci-Dusseau, Remzi H. Arpaci-Dusseau. *From WiscKey to Bourbon: A Learned Index for Log-Structured Merge Trees*. OSDI 2020, pp. 155–171. [原始 PDF（共 18 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/storage-structure/from-wisckey-to-bourbon%3A-a-learned-index-for-log-structured-merge-trees.pdf)
8. **#57**：Qiaolin Yu, Chang Guo, Jay Zhuang, Viraj Thakkar, Jianguo Wang, Zhichao Cao. *CaaS-LSM: Compaction-as-a-Service for LSM-based Key-Value Stores in Storage Disaggregated Infrastructure*. PACMMOD (SIGMOD 2024), 2(3): 124 (28 pages). [原始 PDF（共 28 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/storage-structure/caas-lsm%3A-compaction-as-a-service-for-lsm-based-key-value-stores-in-storage-disaggregated-infrastructure.pdf)
9. **#60**：Viktor Leis, Michael Haubenschild, Alfons Kemper, Thomas Neumann. *LeanStore: In-Memory Data Management Beyond Main Memory*. ICDE 2018, 12 pages. [原始 PDF（共 12 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/storage-structure/leanstore%3A-in-memory-data-management-beyond-main-memory.pdf)
10. **#64**：Thomas Neumann, Michael Freitag. *Umbra: A Disk-Based System with In-Memory Performance*. CIDR 2020, 7 pages. [原始 PDF（共 7 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/storage-structure/umbra%3A-a-disk-based-system-with-in-memory-performance.pdf)
