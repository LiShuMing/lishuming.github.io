---
title: "【Paper】查询执行范式演进精读：从 Volcano 迭代器、MonetDB/X100 向量化到松弛算子融合与自适应编译"
slug: "db-papers-query-execution-vectorization-and-jit"
date: 2026-10-22T12:50:00+08:00
lastmod: 2026-11-12T00:00:00+08:00
toc: true
math: true
categories: ["数据库"]
tags: ["论文精读", "执行引擎", "向量化执行", "JIT编译"]
series: ["数据库经典论文精读"]
---

> 本系列第 6/10 篇 · [数据库经典论文精读总索引]({{< relref "2026-10-22-database-papers-source-reading.md" >}}) · [上一篇：执行反馈与深度基数估计]({{< relref "2026-10-22-db-papers-adaptive-tuning-and-learned-cardinality.md" >}}) · [下一篇：多核并行、主存连接与高速网络]({{< relref "2026-10-22-db-papers-parallel-joins-formats-and-networks.md" >}})

如果优化器历经千辛万苦终于挑中了正确的连接顺序与物理算法，接下来的任务就移交给了**查询执行引擎（Query Execution Engine）**：把算子树上的过滤、投影、连接与聚合真正变成跑在硅片上的机器指令。

在很长一段时间里，教科书都把执行引擎当成一个已经彻底定型的模块——每个算子实现统一的 `open() / next() / close()` 接口，父算子调一次 `next()` 拉一行元组上来，清晰、优雅、高度模块化。可进入 2000 年代以后，这套统治关系数据库二十余年的黄金抽象突然成了吞噬单核性能的头号黑洞：**在同一块现代多核 CPU 上跑同一条内存常驻的 TPC-H Q1，传统逐行解释引擎与精心设计的现代引擎之间，端到端耗时竟然相差整整一到两个数量级！**

要看懂这三十年从 `Volcano` 到 `MonetDB/X100`、再到 `HyPer` / `Peloton (ROF)` / `Umbra` 的剧烈范式跃迁，我们必须先回到存储与超标量 CPU 的底层物理账本。

---

## 一、从磁盘寻道墙到超标量 CPU 流水线墙：为什么 Volcano next() 会烧掉 90% 的无用指令？

把时钟拨回 1990 年。那时的机械硬盘做一次随机寻道加旋转等待大约需要 **\(25\text{ ms}\)**，而在一颗 20 MIPS 的处理器上，\(25\text{ ms}\) 足够执行 **50 万条指令**。只要数据还躺在磁盘上，哪怕执行引擎为了传递一行元组多跑了 300 条虚函数跳转、类型分支判断与缓冲区定位指令，分摊在缓慢的磁盘 I/O 面前连 \(0.1\%\) 的水花都溅不起来——那时的第一性矛盾是**如何用有限内存最小化外存随机读写**。

到了 2005 至 2020 年代，大内存服务器与列存压缩让热工作集稳稳装进了主存（DRAM）乃至三级缓存（L3 Cache），硬件天平发生了根本性的翻转：
- **超标量乱序流水线极度厌恶控制流不确定性**：现代 x86/ARM 核心每周期能并行发射 \(4\sim 6\) 条微指令（IPC 上限极高），但流水线深达 \(14\sim 20\) 级。一旦遇到无法预测的间接虚函数跳转（`child->next()`）或数据相关的 `if (val > const)` 分支预测失败，整条流水线必须冲刷重填，白白赔掉 \(15\sim 20\) 个时钟周期。
- **DRAM 访问成了新的“慢磁盘”**：一次 L1 数据缓存命中只需 \(\sim 4\) 个周期，而一次穿透 LLC 的主存随机读取需要 \(\sim 50\sim 70\text{ ns}\)，在 3.6 GHz 主频下折合 **约 \(180\sim 250\) 个时钟周期**！如果引擎不能喂饱硬件预取器或同时挂起多个独立的内存加载请求（内存级并行度，Memory-Level Parallelism, **MLP**），超标量 ALU 就会长期处于“等米下锅”的饥饿停顿中。

让我们跟着 Boncz 等人在 MySQL 4.1（典型的 Volcano 逐行引擎）上剖析 TPC-H Q1 的硬件计数器账单看一眼真相：执行表达式里一次简单的定点数乘法 `l_extendedprice * (1 - l_discount)`，CPU 居然平均耗费了 **49 个时钟周期**！在整条 Q1 执行的全部机器指令中，**真正用于算术求值与业务比较的有效指令占比不到 \(10\%\)**——约 \(62\%\) 的指令耗在逐字段解析定长/变长行槽位与内存拷贝上，另有 \(28\%\) 耗在哈希聚合桶链寻址与函数调用栈帧进出上，单核 IPC 被死死钉在可怜的 `0.8`。

围绕“如何消灭逐行解释开销、同时藏住主存延迟并吃满 SIMD 与寄存器带宽”，数据库执行引擎走过了四代清晰的架构演进：

![四代查询执行引擎范式微架构对比](/images/diagrams/paper-exec-volcano-vectorized-jit-rof.svg)

下面这张总表浓缩了本篇精读的六篇奠基论文及其在硬件微架构各维度的取舍：

| 论文与代表系统 | 执行范式 | 算子间数据粒度 | 分派开销 | 缓存局部性与寄存器复用 | SIMD 与隐藏内存延迟能力 | 编译启动延迟 |
| :--- | :--- | :--- | :---: | :--- | :--- | :---: |
| **#28 Volcano** (TKDE'94) | 拉模型逐行迭代 + `Exchange` | 单行元组指针 | 极高（每行虚调） | 差（指令缓存与分支预测双抖动） | 无法利用 SIMD；串行等锁/等缓存 | 零（纯解释） |
| **#26 Graefe Survey** (CSUR'93) | 外存物理算子对偶体系 | 页簇（Cluster）与元组流 | 高 | 聚焦内存页缓冲分配与多路归并/分区深度 | 依靠大块连续页簇摊薄磁盘寻道 | 零 |
| **#24 MonetDB/X100** (CIDR'05) | 驻缓存列块**向量化执行** | 约 `1,000` 行列向量 | 极低（摊薄千倍） | 活跃向量常驻 L1/L2，避免全列写爆 DRAM | 紧凑数组循环天然亲和 SIMD 与乱序预取 | 零（预编译原语） |
| **#21 Typer vs Tectorwise** (PVLDB'18) | **编译融合 vs 向量化**同台实测 | 寄存器元组 vs 千行向量 | 零 vs 极低 | `Typer` 计算密集省指令；`Tectorwise` 连接密集高 MLP | 揭示满查询 SIMD 常被内存停顿吞噬 | 毫秒至百毫秒 vs 零 |
| **#27 Peloton ROF** (PVLDB'17) | **松弛算子融合**（分段编译） | 阶段内寄存器 + 阶段间小批 `TID` | 零 | L1 小缓冲解耦长流水线，兼得融合与批处理 | AVX2 查表压缩 + `GP=16` 软件组预取 | 数十毫秒（LLVM） |
| **#20 HyPer Adaptive** (ICDE'18) | **字节码 VM → 后台渐进 JIT** | Pipeline Morsel（约 \(10^4\) 行） | 极低 \(\to\) 零 | 支配树压缩虚拟寄存器至 `6 KB` 常驻 L1 | 短流免编译秒回，长流无栈帧热切机器码 | **亚毫秒（\(\sim 0.4\text{ ms}\)）** |

---

## 二、可扩展迭代器的奠基时代：Volcano 协议、Exchange 元算子与外存对偶律

批评 `Volcano` 在 2020 年代主存算术密集查询上的微架构开销之前，我们必须先读懂 Goetz Graefe 在 1990 年代初设计它时究竟解决了怎样一场工程危机——在那个每加一种新数据类型、新连接算法或把单机引擎搬上多处理器机器都要把执行器推倒重写的年代，Volcano 用不到 **15,000 行 C 代码**确立了延续至今的关系执行骨架。

### 1. Volcano 执行引擎：open/next/close 迭代器协议与正交并行的 Exchange 抽象 {#paper-28}

Volcano 的核心设计哲学只有一句话：**把“关系集合上的控制流与并行调度”同“单个数据对象的内存布局与谓词语义”彻底剥离**。

- **论文信息**：Goetz Graefe, *Volcano—An Extensible and Parallel Query Evaluation System*, IEEE TKDE 6(1): 120–135, 1994 · [原始 PDF（共 16 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/execution-engine/volcano---an-extensible-and-parallel-query-evaluation-system.pdf)（状态记录与迭代器结构见 PDF p. 5，通用一比一匹配见 p. 7 `Figure 3`，`Exchange` 并行拓扑与 `Interchange` 见 p. 11–12 `Figure 6–7`）。

在单线程控制流上，每一个物理算子都只暴露三个标准入口：`open()` 递归初始化子树并分配算子实例专属的**状态记录（State Record）**（从而允许同一查询计划中安全挂载多个哈希连接实例而绝无全局变量串扰）；`next()` 按需求驱动（Demand-driven）拉取下一条结果元组指针或返回流结束符 `End-of-Stream`；`close()` 递归释放资源。算子本身根本不知道元组里存的是整数、字符串还是复杂科学对象——所有键比较、哈希计算与投影拷贝统统通过状态记录里的函数指针委托给编译期或解释期注入的**支持函数（Support Functions）**：

```c
// Volcano 算子统一描述符与实例状态隔离示意
typedef struct OperatorNode {
    ErrCode (*open)(void *state);
    ErrCode (*next)(void *state, Record **out_rec);
    ErrCode (*close)(void *state);
    void    *state;               // 含左右子算子句柄、哈希桶/游标状态与类型无关支持函数指针
} OperatorNode;
```

读源码级细节时会发现两处极见功力的工程边界处理：
- **阻塞阶段与溢出冲刷到底放在哪一步？** 在 Volcano 的哈希算子中，`open()` 负责拉空整棵 Build 子树建起内存哈希表；而 Probe 探测以及**溢出到磁盘分区的递归回读与冲刷（Flush）全部封装在 `next()` 内部的状态机里**，`close()` 只做纯粹的销毁清场。对于内存紧俏时的哈希桶管理，Volcano 还区分了轻量级的**紧凑化阈值（Packing Threshold）**与重量级的**溢出阈值（Spilling Threshold）**：前者先把零散固定（Pinned）在缓冲池页里的冷清分区记录抽出来，密集打包进共享溢出缓冲页而无需写盘；直到连紧凑页都塞不下时，才真正解固定（Unfix）刷往临时磁盘文件。
- **用单算子封装全部并行复杂度的 `Exchange` 元算子**：如果为了跑在 16 核共享内存机器上，要把 `Scan`、`Hash Join`、`Sort` 等二十几个算子全都改写成多线程并发版，测试与死锁排查会直接失控。Graefe 发明了传世的 **`Exchange` 元算子**：所有普通算子依然当作纯单线程拉模型编写，毫不知晓并行的存在；只要在计划树里插入一个 `Exchange`，下半截（Producer）就在子进程/线程里循环调下层 `next()`，把抽出的元组攒成 `1 ~ 32,000` 行的紧凑数据包（Packet）按哈希/范围分区推入共享内存队列；上半截（Consumer）则在父进程里从队列取包，伪装成标准 `next()` 喂给上层算子！

> "The exchange meta-operator supports intra-operator parallelism on partitioned datasets and both vertical and horizontal inter-operator parallelism, translating between demand-driven dataflow within processes and data-driven dataflow between processes." *(PDF p. 1)*

> "To parallelize a new operator, it only has to be combined with the exchange operator in a query evaluation plan. To port all Volcano operators to a new parallel machine, only the exchange operator requires appropriate modifications." *(PDF p. 15)*

用精简的双端伪代码对照一看，就能体会 `Exchange` 是如何把“进程内按需拉取（Pull）”与“进程间数据驱动推送（Push）”缝合得天衣无缝的：

```c
// 子进程/线程侧（Exchange Producer）：主动抽干下层单线程迭代器，打包推入分区队列
void exchange_producer_loop(OperatorNode *child, Port *out_ports, int num_parts) {
    Record *rec;
    child->open(child->state);
    while (child->next(child->state, &rec) != END_OF_STREAM) {
        int p = part_func(rec) % num_parts;
        append_to_packet(&out_ports[p], rec);      // 攒够 1 ~ 32,000 行再发包，摊薄同步锁开销
        if (packet_full(&out_ports[p]))
            push_packet_with_flow_control(&out_ports[p]); // 信号量反压防止打爆慢消费者内存
    }
    flush_all_eos_packets(out_ports, num_parts);
    child->close(child->state);
}

// 父进程/上游侧（Exchange Consumer）：对外伪装成普通单线程 next() 迭代器！
ErrCode exchange_consumer_next(ExchangeState *st, Record **out_rec) {
    while (st->cur_pkt == NULL || st->pkt_idx >= st->cur_pkt->count) {
        if (st->active_producers == 0) return END_OF_STREAM;
        st->cur_pkt = wait_next_packet(st->in_port); // Merge 变体在此改用最小堆按序归并各路队头
        st->pkt_idx = 0;
    }
    *out_rec = st->cur_pkt->records[st->pkt_idx++];
    return OK;
}
```

不仅如此，遇到两层连续连接按不同连接键重分区（例如先按 `partkey` 连接再按 `suppkey` 连接）时，如果上下各插一个普通 `Exchange` 就会白白多 `fork` 一倍进程；Volcano 在 `Exchange` 内置了 **`Interchange`（中间交换）**模式，让同一批工作进程在消费下层包的同时直接按新键横向重洗牌给对等伙伴，并在 16 颗 CPU 的 Sequent Symmetry 上跑出了 **14.9 倍**的并行外部排序加速比。配合在 `open()` 瞬间根据运行时绑定参数与空闲内存动态挑子计划的 **`choose-plan`** 元算子，Volcano 几乎预演了后来所有 MPP 数据库（Greenplum、Impala、Spark Exchange）的骨架。

### 2. Graefe 1993 经典综述：排序—哈希对偶律、混合哈希与外存 I/O 簇大小博弈 {#paper-26}

如果说 Volcano 论文讲透了算子外壳怎么搭，那么 Graefe 前一年发表在 *ACM Computing Surveys* 上长达 **98 页**的鸿篇巨制，则把关系执行引擎内部**排序（Sorting）与哈希（Hashing）两大门派的数学对偶律与外存缓冲物理边界**推演到了极致。

- **论文信息**：Goetz Graefe, *Query Evaluation Techniques for Large Databases*, ACM Computing Surveys 25(2): 73–170, 1993 · [原始 PDF（共 98 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/execution-engine/query-evaluation-techniquesfor-large-databases.pdf)（排序—哈希对偶表见 PDF p. 13 `Table 2`，外部排序簇大小实测见 p. 17–18 `Table 4 & Figure 7`，聚合与双输入连接对比见 p. 30 `Figure 11` 与 p. 38 `Figure 15`）。

> "Another significant difference between logical and physical algebras is the fact that specific algorithms and therefore cost functions are associated only with physical operators, not with logical algebra operators." *(PDF p. 6)*

初学者常把 `Merge-Join` 与 `Hash-Join` 当成两套毫不相干的算法背诵，Graefe 却一针见血地指出：**基于排序的算法与基于哈希的算法在分治结构上严格对偶（Dual）**——假设输入规模远超可用内存 \(M\) 页，每次连续读写磁盘的页簇（Cluster）大小为 \(C\) 页，则单轮最大扇入/扇出度为 \(F \approx \lfloor M / C \rfloor - 1\)：

| 对比维度 | 基于外部排序（Sort-Based） | 基于分区哈希（Hash-Based） |
| :--- | :--- | :--- |
| **分阶段方向** | 先做**物理切分**（生成初始顺串 \(W = \lceil R/M \rceil\)，用堆置换选择期望减半至 \(\approx R/(2M)\)），再按值做**逻辑归并** | 先按哈希函数做**逻辑切分**（写出 \(F\) 个溢出分区文件），再逐分区回读做**物理拼接** |
| **递归深度决定者** | 双输入排序连接需分别排左右两表，深度分别受 \(\lceil \log_F(R/M)\rceil\) 与 \(\lceil \log_F(S/M)\rceil\) 支配 | **非对称红利**：递归分区深度**仅由较小的 Build 表 \(R\) 决定** \(\lceil \log_F(R/M) \rceil\)，但每层探针表 \(S\) 也要跟着做同构哈希落盘，总分区 I/O 约为 \(2\cdot\lceil \log_F(R/M)\rceil\cdot(R+S)\) |
| **分组聚合内存边界** | 内存消耗取决于输入全量规模 \(R\) | **输出敏感红利**：哈希聚合内存仅取决于**去重后的分组数 \(O\)**——哪怕输入 \(10\text{ TB}\)，只要分组只有 \(1{,}000\) 个就能全程零落盘！ |
| **偏斜与保序特性** | 天然抗数据偏斜（顺串长度不受重复键影响），且产出有趣的有序属性（Interesting Order）供上层复用 | 遇到极端重复哈希键会导致单个分区突破 \(M\) 而退化，需递归改换哈希种子或回退嵌套循环/排序 |

在内存只是略小于 Build 表（\(M < R \le F \cdot M\)）的常见灰区，傻乎乎把所有分区全写盘的纯 `Grace Hash Join` 会白白浪费宝贵内存。**混合哈希连接（Hybrid Hash Join）** 的算账公式在文中给得极其精炼：只划出 \(K\) 个落盘分区，每个落盘分区在内存中仅留 \(1\) 个输出页簇缓冲（共 \(K \cdot C\) 页，另留 \(1\) 个输入页簇 \(C\)），把省下来的全部内存 \(M - (K+1)C\) 都留给**常驻内存的 0 号分区**！只要满足 \((F\text{ 范围内的 }K)\) 使得其余分区平均大小不超过 \(M - C\)，所需最小落盘分区数为：

$$K = \left\lceil \frac{R - M + C}{M - C} \right\rceil$$

举个直观数字例子：设 Build 表 \(R = 240\) 页、Probe 表 \(S = 240\) 页、内存 \(M = 80\) 页、页簇 \(C = 8\) 页。纯 `Grace Hash` 会把两表全部写盘再读回，产生 \(2 \times (240 + 240) = 960\) 页临时 I/O（单侧 \(480\) 页）；而代入上式得 \(K = \lceil (240 - 80 + 8)/(80 - 8)\rceil = \lceil 168/72\rceil = 3\) 个落盘分区，内存可常驻 \(80 - 4 \times 8 = 48\) 页（占总数据 \(20\%\)），双表总临时读写立刻降为 \(960 \times 0.8 = 768\) 页（单侧 \(384\) 页），随 \(M \to R\) 平滑过渡到零落盘内存哈希！

更反直觉的是论文实测揭示的**页簇大小 \(C\) 与归并扇入度 \(F\) 的物理博弈**（PDF p. 17–18 `Table 4`）：给定固定内存 \(M = 40\) 页对 \(100{,}000\) 条 100 字节记录（约 \(10\text{ MB}\)）做外部归并排序，在“每次寻道旋转 \(25\text{ ms}\) + 每页传输 \(2\text{ ms}\)”的真实物理账本下对比不同簇大小：

| 页簇大小 \(C\)（页） | 最大归并扇入度 \(F = M/C\) | 平均归并轮数 | 累计读写总页数 | 累计磁盘 I/O 次数 | 端到端总 I/O 耗时 |
| :---: | :---: | :---: | :---: | :---: | :---: |
| **\(C = 1\)**（课本最小轮数） | `40` | `1.376` | `6,874` 页 | `6,874` 次 | **`185.598 s`** |
| **\(C = 2\)** | `20` | `2.000` | `10,000` 页 | `5,000` 次 | `145.000 s` |
| **\(C = 5\)** | `8` | `2.525` | `12,624` 页 | `2,525` 次 | `88.373 s` |
| **\(C = 10\)**（大簇顺序读写） | `4` | `3.000` | **`14,900` 页（\(2.17\times\)）** | **`1,490` 次** | **`67.050 s`（快 \(2.77\times\)）** |

如果死抠算法课本追求最小归并轮数取 \(C = 1\)，虽然总传输页数最少，但 `6,874` 次随机寻道直接把磁盘卡死在 `185.6 s`；大胆把页簇扩到 \(C = 10\) 页后，尽管归并轮数涨到 3 轮、总搬运字节数翻了一倍有余，随机寻道次数却锐减了近五分之四，总耗时反而暴跌至 **`67.050 s`**！这一物理洞察即便在今天的 NVMe SSD 与云对象存储分块读写（Multipart Read/Write）中依然完全成立。

> "We counted the number of I/O operations and the transferred pages for all run files, and calculated the total I/O cost by charging 25 ms per I/O operation (for seek and rotational latency) and 2 ms for each transferred page (assuming 2 MB/sec transfer rate)." *(PDF p. 17)*

文末还点出了并行引擎工程师至今仍在踩的**保序流控死锁定律**：当（1）生产者按某顺序吐数据、（2）消费者做保序归并（`Merge Exchange` 阻塞等待特定上游槽位下一行）、（3）重分区函数与排序键不一致（例如哈希分发破坏了范围单调性）、且（4）进程间队列容量有界这四个条件同时成立时，生产者会因某条满队列阻塞停产，而消费者正死等该生产者往另一条空队列发包，瞬间形成环路死锁！

---

## 三、冲破解释开销与内存带宽的双重夹击：向量化与代码生成的正面交锋

### 3. MonetDB/X100：为什么全列物化会撞上内存带宽墙，而千行驻缓存 Vector 刚刚好？ {#paper-24}

既然 Volcano 逐行调 `next()` 在主存查询上把 90% 的指令都浪费在了函数分派与字段导航上，阿姆斯特丹 CWI 团队最初给出的猛药是 **MonetDB/MIL** 的**一次一列（Column-at-a-Time）**模型：把每列存成紧凑数组 `BAT`，算 `a * (1 - b)` 时直接调一个写死的 C 循环一次性扫完整列几百万个元素，输出一根全新的中间结果列数组传给下一个算子。解释开销瞬间降为零——**可为什么在 1 GB 内存常驻的 TPC-H Q1 上，MonetDB/MIL 还是跑了足足 `3.72 s`，比手写 C 程序（`0.22 s`）慢了近 17 倍？**

2005 年 Peter Boncz、Marcin Zukowski 与 Niels Nes 发表的 **MonetDB/X100（即后来商业化的 VectorWise）** 把病根剖得一清二楚：**消灭了解释器，却把主存带宽写爆了**。

- **论文信息**：Peter Boncz, Marcin Zukowski, Niels Nes, *MonetDB/X100: Hyper-Pipelining Query Execution*, CIDR 2005 · [原始 PDF（共 14 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/execution-engine/monetdn-x100-hyper-pipelining-query-execution.pdf)（分支预测惩罚见 PDF p. 4 `Figure 2`，MySQL/MIL/X100 指令拆解见 p. 5–6 `Table 1–3`，向量化代数与缓存窗口见 p. 9 & p. 12）。

在 TPC-H Q1（SF1 约 600 万行）的算术与聚合表达式树里，一共会产生十几个中间临时列。`MonetDB/MIL` 每算完一步就把几千万字节的中间列全量写穿 CPU 缓存落回慢速 DRAM，下一步再从 DRAM 重新拉回缓存——整条查询超过 \(80\%\) 的时间都在空等内存总线搬运临时数组！

> "However, its policy of full column materialization causes it to generate large data streams during query execution. On our decision support workload, we found MonetDB/MIL to become heavily constrained by memory bandwidth, causing its CPU efficiency to drop sharply." *(PDF p. 2)*

一旦看清“单行太碎烧指令、全列太大爆内存”的两端极限，X100 的破局之道就像物理定律推导出的必然解：**保留 Volcano 按需拉取的流水线骨架，但让每次 `next()` 传递一小块恰好能全部塞进 L1/L2 数据缓存的垂直列切片——Vector（向量，默认约 `1,000` 行）！**

> "Therefore, we propose to strike a balance between the Volcano and MonetDB execution models. This leads to pipelined operators that pass to each other small, cache-resident, vertical data fragments called vectors." *(PDF p. 14)*

为什么偏偏是 **`1,000` 行左右**（论文实测甜点窗口在 `128 ~ 8,192` 行之间）？算一笔容量账就明白了：假设流水线上同时活跃着 6 到 8 根 8 字节列向量，每根 `1,000` 行只占 `8 KB`，全部加起来不过 **`48 ~ 64 KB`**，稳稳当当躺在高速 L1/L2 Cache 内部生老病死，一行中间结果都不用写回主存；与此同时，原本每 1 行就要承受一次的 `next()` 虚函数调用与算子状态切换，现在被 `1,000` 行整批摊薄了整整一千倍！

为了让底层 C 数组原语真正跑满超标量流水线与 SIMD 寄存器，X100 还配套了三项关键微架构技法：
1. **无别名紧凑循环与复合原语（Compound Primitives）**：所有预编译类型化算术原语一律标注 C99 `restrict` 指针消除编译器别名顾虑，展开循环流水线（Software Pipelining），并把高频多步算术表达式合成为复合原语，让中间结果直接留在物理寄存器里减少 L1 读写。
2. **延迟拷贝的选择向量（Selection Vector `sel[]`）**：过滤算子评估完谓词后，绝不急着把存活的列数据搬家重排，而是只维护一个紧凑的下标数组 `sel = [0, 3, 7, ...]`；后续算术或哈希原语只需按 `col[sel[i]]` 原地处理有效槽位，彻底省掉中间列物化拷贝。
3. **无分支谓词化（Branchless Predication）**：面对选择率在 \(10\% \sim 90\%\) 之间摇摆的过滤条件，传统条件跳转会触发密集的 CPU 分支预测冲刷；X100 改用无分支指针算术把控制流依赖转化为纯数据流流水线：

```c
// 传统分支写法：选择率 ~50% 时每行约损失 15~20 个分支预测失败周期
int map_select_lt_col_val_branched(int n, double * restrict x, double v, int * restrict sel) {
    int k = 0;
    for (int i = 0; i < n; i++)
        if (x[i] < v) sel[k++] = i;
    return k;
}

// X100 无分支谓词化原语：零分支跳转，超标量流水线满速吞吐
int map_select_lt_col_val_predicated(int n, double * restrict x, double v, int * restrict sel) {
    int k = 0;
    for (int i = 0; i < n; i++) {
        sel[k] = i;          // 无条件预写当前下标（未命中时会被下一轮覆写）
        k += (x[i] < v);     // 布尔结果 0/1 直接推进有效写指针
    }
    return k;
}
```

实测效果立竿见影（PDF p. 5–6 `Table 1–3` 与 p. 11）：在同等内存常驻 TPC-H Q1（SF1，约 600 万行）条件下，三代执行模型呈现出教科书级的微架构阶梯跃迁：

| 被测系统与执行模型 | 算子间传递对象 | 单次乘法平均周期 | 有效算术指令占比 | 主要微架构瓶颈 | TPC-H Q1 (SF1) 耗时 |
| :--- | :--- | :---: | :---: | :--- | :---: |
| **MySQL 4.1**（`Volcano` 逐行） | 单行记录指针 | `~49 cycles` | `< 10%` | 62% 字段槽位导航拷贝 + 28% 哈希桶链寻址，IPC 仅 `0.8` | `~27.8 s` |
| **MonetDB/MIL**（全列物化） | 整列百万行 `BAT` | 低（但等内存） | 高 | 中间列冲破缓存反复读写主存，80%+ 时间卡在 DRAM 带宽 | `3.72 s` |
| **MonetDB/X100**（千行向量化） | `~1,000` 行驻缓存列块 | **`~2.2 cycles`** | **主导（近似手写 C）** | 活跃向量常驻 L1/L2（内部吞吐 `7.5 GB/s`），逼近硬件算力天花板 | **`0.50 s`（Itanium2 `0.31 s`）** |
| *参照基线：手写融合 C 循环* | *CPU 物理寄存器* | *`~1.5 cycles`* | *接近 100%* | *受限于 CPU 浮点/定点发射端口上限* | *`0.22 s`* |

### 4. Kersten 等人微架构实证：Typer 编译融合与 Tectorwise 向量化究竟谁更快？ {#paper-21}

X100 掀起向量化浪潮仅六年之后，Thomas Neumann 在 2011 年提出 **HyPer 以数据为中心的查询编译（Data-Centric JIT Compilation）**：通过 `produce() / consume()` 把同一条流水线（Pipeline Breaker 之间）的所有算子反向融合成一段紧凑的循环代码，交给 LLVM 编译成本机机器码——元组字段从头到尾驻留在 **CPU 寄存器**里，连 X100 读写 L1 缓存向量的指令都省掉了！

于是数据库社区争论了整整七年：**究竟是 `X100` 式的向量化引擎更快，还是 `HyPer` 式的 JIT 编译融合引擎更快？** 以前拿 VectorWise 和 HyPer 直接跑分根本说明不了问题，因为两套系统的哈希表布局、并行调度器、字符串编码乃至物理执行计划全都不同。2018 年，来自慕尼黑工大（HyPer 阵营）、CMU（Peloton 阵营）与 CWI（VectorWise 阵营）的原班核心作者联手撰写了这篇里程碑式的实证论文：在**同一个代码库**里共享完全相同的物理计划、带 16-bit 指针标签摘要的链式哈希表、两阶段聚合与 Morsel-driven 并行调度器，分别实现了纯编译融合引擎 **`Typer`** 与千行向量化引擎 **`Tectorwise`**，在 Intel Skylake-X（i9-7900X，支持 AVX-512）上用硬件性能计数器逐项对账。

- **论文信息**：Timo Kersten, Viktor Leis, Alfons Kemper, Thomas Neumann, Andrew Pavlo, Peter Boncz, *Everything You Always Wanted to Know About Compiled and Vectorized Queries But Were Afraid to Ask*, PVLDB 11(13): 2209–2222, 2018 · [原始 PDF（共 14 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/execution-engine/everything-you-always-wanted-to-know-about-compiled-and-cectorized-queries-but-were-afraid-to-ask.pdf)（单线程微架构硬件计数器总表见 PDF p. 4 `Table 1 & Figure 3`，内存停顿与向量大小见 p. 5 `Figure 4–5`，AVX-512 实测见 p. 7 `Figure 6–8`）。

先把论文 PDF p. 4 `Table 1`（TPC-H SF1 单线程，所有指标均按**每处理一行输入元组**归一化）这组颠覆无数人直觉的实测数字摆在台面上：

| 查询与负载特征 | 引擎 | Cycles / tuple | Instructions / tuple | IPC | L1 Misses | LLC Misses | Branch Misses | 胜出方与底层物理机制 |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :--- |
| **`Q1`**（纯算术与小表聚合，计算密集） | `Typer` (编译)<br>`Tectorwise` (向量) | **`34`**<br>`59` | **`68`**<br>`162` | `2.0`<br>**`2.8`** | `0.15`<br>`0.64` | `0.02`<br>`0.02` | `0.02`<br>`0.03` | **`Typer` 快 \(1.7\times\)**：寄存器直传砍掉近 60% 读写 L1 向量的搬运指令 |
| **`Q6`**（纯扫描过滤累加） | `Typer`<br>`Tectorwise` | **`6.0`**<br>`7.7` | **`15`**<br>`18` | `2.5`<br>`2.3` | `0.11`<br>`0.13` | `0.02`<br>`0.02` | `0.04`<br>`0.01` | **`Typer` 略胜**：短路求值直接跳过后续列读取 |
| **`Q3`**（中等哈希连接与聚合） | `Typer`<br>`Tectorwise` | `15`<br>**`14`** | **`21`**<br>`30` | `1.5`<br>`2.2` | `0.36`<br>`0.40` | `0.11`<br>`0.11` | `0.09`<br>`0.05` | **基本打平**：连接缓存未命中开始抵消编译省下的指令 |
| **`Q9`**（多表大哈希连接，随机访存密集） | `Typer`<br>`Tectorwise` | `74`<br>**`56`** | **`42`**<br>`76` | `0.6`<br>`1.3` | `1.39`<br>`1.75` | `0.47`<br>`0.46` | `0.36`<br>`0.20` | **`Tectorwise` 反超快 \(1.3\times\)**：小循环激发乱序窗口并发挂起更多 DRAM 读取！ |
| **`Q18`**（重度大分组哈希聚合） | `Typer`<br>`Tectorwise` | `30`<br>**`23`** | **`33`**<br>`56` | `1.1`<br>`2.4` | `0.73`<br>`0.99` | `0.21`<br>`0.21` | `0.11`<br>`0.05` | **`Tectorwise` 胜出**：分步向量循环隐藏哈希桶链缺页停顿 |

仔细端详这张表，你会同时收获两条价值千金的微架构铁律：

**铁律一（看 `Q1`）：高 IPC 绝不等于跑得快！** 在计算密集的 `Q1` 上，`Tectorwise` 的 IPC 高达 `2.8`，比 `Typer`（`2.0`）漂亮得多；然而 `Typer` 每行只花了 `34` 个周期，比 `Tectorwise`（`59` 个周期）快了 `1.7` 倍！原因在于向量化引擎每一步原语算完都必须把千行结果写进 L1 缓存数组、下一步再读出来，整整执行了 `162` 条指令（这些规整的 L1 加载/存储极其好并行，所以把 IPC 刷得虚高）；而 `Typer` 把十几个算术子表达式全融合在 CPU 寄存器里，总共只跑 `68` 条指令就收工了。

> "Note that for these simple operators the Tectorwise implementation must already change the structure of the algorithms and deviate from the Typer data access patterns. The resulting materialization of intermediates makes fast caches very important for vectorized engines." *(PDF p. 2)*

**铁律二（看 `Q9` 与 `Q18`）：一旦撞上大哈希表的 LLC 缓存未命中，向量化小循环反而靠“硬件乱序窗口 + 内存级并行（MLP）”反杀大融合循环！** 看 `Q9` 的两列数字：`Typer` 执行的指令数依然比 `Tectorwise` 少将近一半（`42` vs `76`），两者的末级缓存未命中次数也几乎一模一样（`0.47` vs `0.46` 次/行）——按常理同等的 DRAM 缺页加更少的指令，`Typer` 怎么可能输掉 `18` 个周期？

答案就藏在现代 CPU 的**重排序缓冲区（Reorder Buffer, ROB，Skylake 上为 224 项）**里！把两套引擎在多表哈希连接流水线上的核心循环摆在一起对比，物理差异一目了然：

```cpp
// [Typer 编译融合] 单条巨型紧耦合循环：算术省指令，但循环体膨胀超百条机器指令！
for (size_t i = 0; i < morsel_n; i++) {
    if (pred(col_a[i])) {
        Entry *e1 = ht1_lookup(key1[i]);     // <-- 一旦此处穿透 LLC 等 DRAM 200 周期，
        while (e1) {                         //     下方连环分支与长指令序列迅速填满 224 项 ROB，
            if (e1->k == key1[i]) {          //     CPU 无法跨迭代提前挂起 i+1, i+2 的内存读取！
                Entry *e2 = ht2_lookup(e1->fk2);
                while (e2) { /* 继续嵌套探测 ht3, ht4 并累加聚合寄存器... */ }
            }
            e1 = e1->next;
        }
    }
}

// [Tectorwise 向量化] 拆成多个微型独立原语循环：多出 L1 数组读写，但单步循环仅数条指令！
vec_hash_u64(n, key1_vec, hash_vec);                     // Step 1: 纯计算，满速 SIMD/超标量
vec_lookup_initial_buckets(n, ht1, hash_vec, bkt_vec);   // Step 2: 极小循环！224 项 ROB 一次横跨十余行，
vec_check_keys_and_advance(n, key1_vec, bkt_vec, match); //         并发向内存控制器发射 10 个独立 DRAM 请求！
```

在 `Typer` 中，6 张表的哈希探测、载荷比对与表达式计算被融合成了一个长达上百条指令的巨型循环体：当第 `i` 行元组卡在 `0.47` 次 LLC Miss 等主存返回数据的 200 个周期里，CPU 乱序引擎往后投机看不了两圈就会把 224 项 ROB 塞满（且大循环里夹杂的复杂分支一旦预测错还会整段回滚），导致同一时刻只能串行挂起 1 到 2 个主存请求（IPC 跌至 `0.6`）。反观 `Tectorwise` 把哈希连接拆成了几个只有寥寥数行指令的超小紧凑循环：当第 \(k\) 个元素发生 LLC Miss 停顿时，224 项 ROB 足以轻松跨越后续十余次循环迭代，**同时向内存控制器并发投递大批互不依赖的哈希桶加载请求**，把多次 200 周期的 DRAM 等待重叠在了同一个时间窗口里！

至于备受吹捧的 **AVX-512 显式向量化**，论文也给出了极其冷静的祛魅数据：在孤立的 L1 常驻单列密集过滤微基准上，手写 AVX-512 确实能跑出耀眼的 **`8.4x`** 加速；可一旦接上第二道带 `40%` 选择向量的稀疏过滤，加速比就跌到 `2.7x`；放进完整的端到端查询里，`Q6` 仅快了 **`1.4x`**，而受困于随机访存 `Gather` 停顿的 `Q3` 与 `Q9` **仅仅快了约 `1.1x`**！

> "We observe that most of the time is spent waiting for data. Thus the memory subsystem becomes the bottleneck of the selection operation and the positive effect of utilizing SIMD instructions disappears." *(PDF p. 7)*

在多核扩展性（最高 60 硬件线程）与系统分类学上，论文还提炼出一张极具统摄力的 **`3 × 3` 流水线粒度 × 求值方式分类矩阵**（PDF p. 12 `Table 6`）：

| 控制流与缓存粒度 \ 表达式求值机制 | 通用解释求值（Interpreted） | 预编译类型化原语（Precompiled Primitives） | 查询专属 JIT 机器码（Compiled Code） |
| :--- | :--- | :--- | :--- |
| **全算子物化（Operator-at-a-Time）** | 早期教学原型 | `MonetDB/MIL`（全列 `BAT` 落回主存） | `HIQUE`（整算子代码生成并物化中间表） |
| **向量块流水线（Vector-at-a-Time）** | 列块解释表达式树 | **`VectorWise` / `Tectorwise` / `DuckDB`** | 向量化 + 融合表达式 JIT（如 `Photon` 部分路径） |
| **元组寄存器流水线（Tuple-at-a-Time）** | 传统 **`Volcano`**（MySQL / PG） | 模板化算子组合 | **`HyPer` / `Typer`**（严格算子融合）与 **`Peloton ROF`**（分段融合） |

这张表彻底厘清了社区长久以来的概念混淆：“向量化”与“JIT 编译”根本不是互斥的反义词——前者回答的是**算子之间按多大粒度切分控制流以适配缓存与 ROB**，后者回答的是**单个阶段内部用函数表查表还是生成专属机器指令来消灭解释分派**。这也自然引出了接下来两篇融合大成之作。

---

## 四、融合两条路线的终极形态：算子内松弛缓冲与亚毫秒自适应编译

读懂了 `Typer` 与 `Tectorwise` 各自的胜负手，一个再自然不过的工程追问就呼之欲出了：**既然寄存器融合在算术流水线上省指令，而小批向量在过滤压缩与随机哈希探测上能释放 SIMD 和内存级并行，谁规定一条 Pipeline 必须从头融到尾？我们能不能在编译生成的机器码内部，只在最需要跨元组并行的关节处轻轻切一刀？**

### 5. Relaxed Operator Fusion (ROF)：在编译流水线内精准切入暂存边界，协同 SIMD 与组预取 {#paper-27}

CMU 的 Prashanth Menon、Todd C. Mowry 与 Andrew Pavlo 在 **Peloton** 内存数据库中提出的 **松弛算子融合（Relaxed Operator Fusion, `ROF`）**，正是把上述设想落成严谨编译器机制的代表作。

- **论文信息**：Prashanth Menon, Todd C. Mowry, Andrew Pavlo, *Relaxed Operator Fusion for In-Memory Databases: Making Compilation, Vectorization, and Prefetching Work Together At Last*, PVLDB 11(1): 1–13, 2017 · [原始 PDF（共 13 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/execution-engine/relaxed-operator-fusion-for-in-memory-databases-making-compilation-vectorization-and-prefetching-work-together-at-last.pdf)（Q19 编译 vs 向量化动机见 PDF p. 2 `Figure 1–3`，阶段切分、AVX2 压缩与 `GP` 状态机见 p. 4–6，消融与灵敏度实验见 p. 8–10 `Figure 9–15`）。

> "Because in-memory DBMSs are designed such that the bulk of the working dataset fits in memory, disk accesses are no longer the main bottleneck in query execution. Instead, cache misses to memory and computational throughput are much more important factors in the performance of in-memory DBMSs." *(PDF p. 1)*

在经典的严格算子融合（Strict Operator Fusion）中，一条元组只要通过过滤谓词，就立刻顺着同一层 `for` 循环扎进上方的哈希探测与算术求值——这不仅让过滤后的存活元组变得支离破碎（无法凑满 8 路 SIMD 通道做批量操作），还让软件预取（`__builtin_prefetch`）彻底失灵：你刚对当前元组的哈希桶发出预取指令，下一条指令马上就要解引用它，预取距离为零，根本藏不住 200 周期的 DRAM 延迟。

`ROF` 对编译期生成的 Pipeline 图做选择性切分——阶段（Stage）内部的纯计算与投影算子照常走紧凑的 LLVM 寄存器融合；但每当遇到**（a）可 SIMD 化的过滤扫描出口**，或**（b）哈希表体积超出缓存容量的随机探测入口**时，编译器就插入一个轻量级的 L1 常驻**暂存边界（Stage Boundary，大小在数百至数千项，约几 KB）**，只传递紧凑的**元组下标向量（`TID[]`）**以及按需附带的伴随槽位指针向量：

1. **全满批次的 AVX2 查表压缩输出**：扫描阶段每次用 256-bit AVX2 寄存器并行比较 8 行列值，通过 `movemask` 抽出 8-bit 掩码，查一张预建的 **`2 KB` 置换查找表（Permutation LUT，\(256 \times 8 \times 1\text{B}\)）** 驱动 `permutevar8x32` 将存活的 `TID` 紧挨着挤到左侧，再用 `popcnt` 累加写出指针；外层循环一直攒够整批（例如 1,024 个百分之百存活的有效 `TID`）才把控制权交给下一阶段，确保下游算子永远吃满规整的稠密批次：

```cpp
// ROF Stage 1 出口：AVX2 8 路并行过滤 + 2KB 查表紧缩，攒满 1024 个有效 TID 再刷给下一阶段
__m256i v_tids = _mm256_setr_epi32(row, row+1, row+2, row+3, row+4, row+5, row+6, row+7);
__m256i v_mask = _mm256_cmpgt_epi32(v_threshold, _mm256_loadu_si256((__m256i*)&col[row]));
int mask8      = _mm256_movemask_ps(_mm256_castsi256_ps(v_mask));       // 抽出 8-bit 存活位图 (0..255)
__m256i v_perm = _mm256_cvtepu8_epi32(_mm_loadl_epi64((__m128i*)&PERM_LUT_2KB[mask8]));
__m256i packed = _mm256_permutevar8x32_epi32(v_tids, v_perm);           // 存活 TID 全部左对齐紧缩
_mm256_storeu_si256((__m256i*)&tid_stage_buf[out_cnt], packed);
out_cnt       += __builtin_popcount(mask8);                             // 无分支推进有效缓冲计数
```
2. **对齐硬件 Line Fill Buffer 的软件组预取（Group Prefetching, `GP`）**：在跨越主存的开放寻址哈希连接阶段，`ROF` 不再一条条元组串行探测，而是每次从 `TID` 缓冲中取出固定大小的一组元组（实测在每核拥有 **10 个硬件行填充缓冲区 LFB** 的 Xeon E5-2630v4 上，甜点组大小为 **`GP = 16`**），让这 16 行元组像流水线波次一样分步推进：

```c
// ROF 哈希探测组预取（GP = 16）核心时序示意
for (int base = 0; base < batch_size; base += 16) {
    // 第 1 拍：批量算出 16 个元组的哈希槽位，一次性发射 16 条非阻塞软件预取！
    for (int k = 0; k < 16; k++) {
        slots[k] = hash(key[tids[base + k]]) & mask;
        __builtin_prefetch(&ht_buckets[slots[k]], 0, 1);
    }
    // 第 2 拍：当第 0 号元组回头读取 ht_buckets[slots[0]] 时，前面 15 轮循环与哈希计算
    // 已经替它垫付了宝贵的内存往返时间，10 个硬件 LFB 满载并发拉回缓存行！
    for (int k = 0; k < 16; k++) {
        probe_and_advance_state(&ht_buckets[slots[k]], tids[base + k]);
    }
}
```

> "It is still the case, however, that a DBMS needs to be careful not to launch too many prefetches in a short period of time, because the CPU is likely to drop them once the hardware buffer that tracks outstanding requests fills up." *(PDF p. 3)*

尤为难得的是，论文没有把“SIMD + 预取”吹成包治百病的银弹，而是用精确的消融数据标出了**何时开启反而亏钱**：
- 在选择率仅 \(2\%\) 且连接大表的 **`TPC-H Q14`** 上，SIMD 过滤贡献了 `1.37x`、软件组预取贡献了 `1.45x`，合起来比严格算子融合快了约 **`2.0x`**（全套基准最高提速 `2.2x`）；
- 可在前面提过的 **`TPC-H Q1`** 上，过滤选择率高达 \(98\%\)（扫描仅占总耗时 \(4\%\)），且分组聚合哈希表总共只有 **4 个条目**，从头到尾稳坐 L1 Cache！此时开 SIMD 仅微增 `1.04x`，而**如果盲目给这张常驻 L1 的 4 行小哈希表套上软件组预取状态机，多出来的状态数组读写与预取指令反而会让总执行时间倒退变慢！** 因此 `ROF` 在生成代码时专门保留了按运行时表规模切换预取路径的动态分支，并在分支汇合处垫一层输出暂存边界，避免下游算子代码膨胀两份。

### 6. HyPer 自适应执行：线性时间 SSA 字节码虚拟机与零 OSR 的 Morsel 边界机器码切换 {#paper-20}

到这里，主存大查询的单核吞吐似乎已经被编译融合与松弛向量化榨干了。然而当慕尼黑工大团队把基于 LLVM JIT 的 `HyPer` 真正推向混合负载生产环境时，却撞上了一堵让在线用户抓狂的墙——**LLVM 编译本身太慢了！**

看两个论文实测的极端痛点：其一，前端管理工具 `pgAdmin` 连上数据库刷新元数据时发来的轻量系统表查询，生成的机器码真正跑完只需 **`< 1 ms`**，可开启优化的 LLVM 后端光编译它就要花掉 **`54 ms`**——整整 **98% 的端到端响应时间被浪费在了编译等待上**！其二，面对商业智能工具机器生成的超长复杂报表 SQL（单条查询膨胀出 **160,000 条 LLVM IR 指令**），哪怕关掉全部优化通道走 `LLVM -O0`，非线性复杂度的后端寄存器分配与指令选择也要卡住 **`> 4 s`**；可如果退回 LLVM 自带的通用解释器 `lli`，它的逐指令解释速度又比机器码慢 **`800 倍`以上**！

2018 年 André Kohn、Viktor Leis 与 Thomas Neumann 发表的 **Adaptive Execution of Compiled Queries**，用一套精巧绝伦的**共享 IR 三层执行架构**彻底终结了“解释快启动”与“编译高吞吐”之间的二选一困境。

- **论文信息**：André Kohn, Viktor Leis, Thomas Neumann, *Adaptive Execution of Compiled Queries*, ICDE 2018 · [原始 PDF（共 12 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/execution-engine/adaptive-execution-of-compiled-queries.pdf)（编译延迟痛点见 PDF p. 1–2 `Figure 1–3`，Morsel 句柄与耗时外推公式见 p. 4–5 `Figure 4–7`，支配树线性寄存器分配见 p. 7 `Figure 10–12`，Q11 逐流水线时间轴见 p. 10 `Figure 14`）。

> "With optimizations enabled, LLVM takes 54ms to compile this query. In other words, compilation takes 50 times longer than execution. Assuming a workload where similar queries are executed frequently, 98% of the time will be wasted on compilation." *(PDF p. 1)*

![HyPer 自适应执行：从线性 SSA 字节码解释到 Morsel 边界机器码热切换](/images/diagrams/paper-exec-adaptive-vm-to-llvm.svg)

整套系统最核心的工程洞见在于**绝不维护两套独立的执行引擎**（否则语义对齐和算子翻倍维护会要命），而是让查询编译器照常花 \(\sim 0.7\text{ ms}\) 生成同一份 LLVM IR，随后在**单条 Pipeline（流水线）粒度**上动态调度三档执行后端：

**第一档（第 0 毫秒立即起跑）：线性时间翻译的静态类型寄存器字节码虚拟机（`Bytecode VM`）。** 为什么 LLVM 自带解释器慢 800 倍，而 HyPer 新写的这台只有约 800 行主循环 C++ 代码、约 500 个静态类型操作码的虚拟机却能跑得**比 PostgreSQL 9.6 传统解释引擎还快 `2.1 倍`**？
- 首先，翻译器利用输入已是合法 SSA 形式的优势，只做**严格线性时间 \(O(N)\)** 的遍历——在 TPC-H Q1 上翻译仅需 **`0.4 ms`**，就算面对前面那条 16 万条 IR 指令的巨无霸查询，`0.9 s` 内也能翻译完毕立刻开跑；
- 其次，如果不做寄存器复用，SSA 每个临时变量占一个槽位会把 `TPC-DS Q55` 的虚拟寄存器数组撑到 `36 KB`（冲破 `32 KB` L1 数据缓存）。作者没用昂贵的图着色或全局线性扫描，而是利用 SSA 支配树性质（变量定义必支配所有使用点），仅在跨越循环嵌套头节点时延长活跃区间，线性一遍就把寄存器文件从 **`36 KB` 压到 `6 KB`**，稳稳锁在 L1 Cache 内；同时将高频 4 步溢出检查算术序列折叠为单条宏字节码指令，大幅砍掉分派次数。

**第二档与第三档（后台异步无优化机器码 `Unopt Native` 与全优化机器码 `Opt Native`）：基于实测吞吐外推的编译重叠决策。** 查询一启动，所有 \(w\) 个工作线程立刻在 `Bytecode VM` 下按 **Morsel（约 \(10{,}000\) 行元组的数据分片）** 拉取执行。跑满仅仅 **`1 ms`** 后，其中一个线程在 Morsel 间隙停下几微秒，利用刚测得的单线程字节码真实吞吐率 \(r_0\)（行/毫秒）、当前流水线已知的剩余输入行数 \(n\)，以及根据本流水线 LLVM IR 指令数线性回归预测出的两档后台编译耗时 \(c_1, c_2\) 与经验加速比 \(\text{speedup}_i\)（即预期机器码速率 \(r_i = r_0 \cdot \text{speedup}_i\)），算一道极其漂亮的物理应用题：

$$t_0 = \frac{n}{r_0 \cdot w}, \qquad t_i = c_i + \frac{\max\big(n - (w - 1)\cdot r_0 \cdot c_i,\; 0\big)}{r_i \cdot w} \quad (i \in \{1, 2\})$$

请特别留意分子里的减项 **\((w - 1)\cdot r_0 \cdot c_i\)**：当系统挑出 1 个后台线程去跑耗时 \(c_i\) 的 LLVM 编译时，**剩下的 \(w - 1\) 个工作线程并没有闲着等它，而是在这段编译时间里继续全速跑着字节码消化剩余元组！** 只有扣除掉这段并发消化的工作量之后，剩下的硬骨头除以提速后的全员吞吐 \(r_i \cdot w\) 再加上 \(c_i\)，依然比继续纯解释的 \(t_0\) 更短，触发后台编译才真正划算！

**点睛之笔：零栈上替换（Zero-OSR）的 Morsel 边界原子热切换。** 在 Java HotSpot 或 V8 引擎里，把一段正在循环正中央跑着的解释帧切换到 JIT 机器码，需要极其惊险复杂的**栈上替换（On-Stack Replacement, OSR）**来重构寄存器与调用栈。HyPer 借力自己的 Morsel-driven 架构四两拨千斤地绕开了整个难题：无论字节码解释入口还是 LLVM 编出来的本机机器码，全都遵守完全相同的 C++ 算子状态内存布局，并暴露一模一样的函数签名（为对齐调用约定，机器码入口顺带接收一个忽略不用的 `VMModule*` 尾参数）！

```cpp
// HyPer 自适应 Pipeline 共享句柄：解释器与机器码共享完全一致的签名与内存布局
using MorselWorkerFn = void (*)(ExecState *state, Morsel *morsel, BytecodeModule *vm_mod);

struct PipelineHandle {
    std::atomic<MorselWorkerFn> best_worker{&run_bytecode_vm}; // 初始指向亚毫秒启动的字节码 VM
    BytecodeModule             *vm_mod;
};

// 前台工作线程循环：无需任何 OSR 栈帧手术，Morsel 间隙自动无锁升级至机器码！
void worker_thread_loop(PipelineHandle *pipe, ExecState *thread_state) {
    Morsel morsel;
    while (scheduler_fetch_next_morsel(&morsel)) {             // 每次领取约 10,000 行分片
        MorselWorkerFn fn = pipe->best_worker.load(std::memory_order_relaxed);
        fn(thread_state, &morsel, pipe->vm_mod);               // 下一个分片即刻跑进新编译好的机器码
    }
}
```

当后台编译线程编好机器码后，只需调用 `best_worker.store(compiled_fn)` 把共享句柄里的函数指针原子替换为新地址——正在跑当前 Morsel 的线程安安静静把手里这一万行解释完，**下一次循环伸手领下一个 Morsel 时，解引用的函数指针就已经天然跳进了飞驰的本机机器码**！

> "In this approach, all decisions are made at a pipeline granularity and are based on runtime feedback instead of having to decide up-front. We further proposed a bytecode interpreter that features a linear-time translation of LLVM IR into efficient bytecode." *(PDF p. 12)*

论文 PDF p. 10 `Figure 14` 对 **`TPC-H Q11`（SF1，4 线程，共 7 条流水线）** 的毫秒级解剖完美展示了这套机制的威力：

| 执行策略 | 编译等待耗时 | 实际执行耗时 | 端到端总响应时间 | 7 条 Pipeline 的真实运行轨迹 |
| :--- | :---: | :---: | :---: | :--- |
| **静态全量优化编译（`LLVM -O3`）** | `103 ms` | `~9 ms` | `~112 ms` | 全部 7 条流水线不论轻重一律干等优化通道编完才起跑 |
| **静态无优化编译（`LLVM -O0`）** | `~28 ms` | `~16 ms` | `~44 ms` | 省掉大半编译耗时，但重负载连接流水线吞吐折损近半 |
| **纯字节码解释（`Bytecode VM`）** | `~1.5 ms`（IR+翻译） | `~39 ms` | `~41 ms` | 零等待起跑，5 条小流水线秒完，但 2 条大表流水线拖尾 |
| **HyPer 自适应混合执行（Adaptive）** | **后台完全重叠** | **`~23 ms`（含重叠期）** | **`~24 ms`（全场最快）** | **5 条轻量流 1ms 内字节码直接跑完；仅 2 条重负载流后台编完热切机器码！** |

正因为把自适应开关下沉到了每一条独立的 Pipeline 而非粗暴的一刀切整条 SQL，该方案从 `SF0.01`（几毫秒极短查询）一路横跨到 `SF30`（百 GB 大查询），在全部 22 条 TPC-H 查询上同时贴着甚至反超所有静态策略的最优包络线。

---

## 五、现代执行引擎设计收敛与参考文献基线

站在今天回望这六篇经典文献，曾经剑拔弩张的“迭代器派”、“向量化派”与“JIT 编译派”已经在最新一代分析型数据库内核中走向了殊途同归的深度融合：

- **以列块 Vector 为骨架、融合表达式求值为血肉（DuckDB / Meta Velox / ClickHouse / Databricks Photon）**：在通用开源与云数仓底座中，维护全量查询级 LLVM 后端的工程复杂度与安全沙箱调试成本依然高昂。因此 DuckDB 与 Velox 完整继承了 `Volcano + Exchange` 的正交调度外壳与 `MonetDB/X100` 的驻缓存列向量（通常 `1,024 ~ 2,048` 行，附带 `Selection Vector` 与紧凑空值位图），吸收了 `Kersten et al.` 与 `ROF` 的微架构教训——在哈希探测与分区洗牌处采用多步紧凑小循环与软件组预取榨干内存级并行（MLP），而在复杂过滤与投影表达式内部则引入轻量级字节码融合或算术原语链以减少 L1 中间向量落盘。
- **分层自适应编译引擎的演进（Umbra / SingleStore / Hyper 商业版）**：在追求极致主存 OLTP + OLAP（HTAP）混合吞吐的编译派系里，TUM 下一代系统 `Umbra` 进一步沿着 `#20` 的分层道路演进——干脆连首轮生成重量级 LLVM IR 的开销都砍掉，直接生成极简自定义中间表示并一步直译为 x86-64 机器码（Flying Start，取代字节码 VM 获得更高起跑吞吐），仅对运行超过数毫秒的热流水线才在后台唤醒重度优化通道，并在 Morsel 边界无感切换。

无论外壳叫向量化还是自适应编译，底层的第一性物理判据从未改变：**小算术靠寄存器减少无效指令，大连接靠紧凑缓冲与组预取喂饱硬件乱序窗口和 LFB，外存溢出靠大页簇摊薄设备寻道延迟，短查询靠分层启动守住亚毫秒响应底线**。

### 参考文献与阅读基线

本文精读的 6 篇论文均基于 [`lonng/db-papers`](https://github.com/lonng/db-papers/tree/4ff1331069c7cf52ac97dd769e5f484c1c35c200) 仓库基线（commit `4ff1331`）核对原始 PDF 页码、公式与实验图表：

| 序号 | 锚点 | 论文题目与作者 | 发表会议/期刊 | 原文链接 |
| :---: | :---: | :--- | :--- | :---: |
| 1 | `#paper-28` | *Volcano—An Extensible and Parallel Query Evaluation System* (Goetz Graefe) | IEEE TKDE 1994 | [PDF (16p)](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/execution-engine/volcano---an-extensible-and-parallel-query-evaluation-system.pdf) |
| 2 | `#paper-26` | *Query Evaluation Techniques for Large Databases* (Goetz Graefe) | ACM Computing Surveys 1993 | [PDF (98p)](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/execution-engine/query-evaluation-techniquesfor-large-databases.pdf) |
| 3 | `#paper-24` | *MonetDB/X100: Hyper-Pipelining Query Execution* (Peter Boncz, Marcin Zukowski, Niels Nes) | CIDR 2005 | [PDF (14p)](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/execution-engine/monetdn-x100-hyper-pipelining-query-execution.pdf) |
| 4 | `#paper-21` | *Everything You Always Wanted to Know About Compiled and Vectorized Queries But Were Afraid to Ask* (Timo Kersten, Viktor Leis, Alfons Kemper, Thomas Neumann, Andrew Pavlo, Peter Boncz) | PVLDB 2018 | [PDF (14p)](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/execution-engine/everything-you-always-wanted-to-know-about-compiled-and-cectorized-queries-but-were-afraid-to-ask.pdf) |
| 5 | `#paper-27` | *Relaxed Operator Fusion for In-Memory Databases: Making Compilation, Vectorization, and Prefetching Work Together At Last* (Prashanth Menon, Todd C. Mowry, Andrew Pavlo) | PVLDB 2017 | [PDF (13p)](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/execution-engine/relaxed-operator-fusion-for-in-memory-databases-making-compilation-vectorization-and-prefetching-work-together-at-last.pdf) |
| 6 | `#paper-20` | *Adaptive Execution of Compiled Queries* (André Kohn, Viktor Leis, Thomas Neumann) | ICDE 2018 | [PDF (12p)](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/execution-engine/adaptive-execution-of-compiled-queries.pdf) |
