---
title: "【Paper】多核并行、主存连接与数据表示精读：Morsel 调度、硬件哈希连接、LIP、开放列存与高速网络"
slug: "db-papers-parallel-joins-formats-and-networks"
date: 2026-10-22T13:00:00+08:00
lastmod: 2026-11-12T00:00:00+08:00
description: "从跨 NUMA 插槽带宽塌缩、2MiB 大页 TLB 抖动、星型连接基数估错、列存解码墙到微秒级 RDMA 网络协议税，深度拆解 Morsel 驱动并行、硬件感知主存哈希连接、LIP 前瞻过滤、开放列存内嵌执行与高速网络数据库重设计。"
toc: true
math: true
categories: ["数据库"]
tags: ["论文精读", "并行执行", "Hash Join", "列式存储", "RDMA"]
series: ["数据库经典论文精读"]
---

> 本系列第 7/10 篇 · [数据库经典论文精读总索引]({{< relref "2026-10-22-database-papers-source-reading.md" >}}) · [上一篇：查询执行范式演进]({{< relref "2026-10-22-db-papers-query-execution-vectorization-and-jit.md" >}}) · [下一篇：存储结构与缓冲管理演进]({{< relref "2026-10-22-db-papers-lsm-rum-and-modern-buffer-managers.md" >}})

上一篇我们沿着向量化批处理（Vectorization）与即时编译（JIT Compilation）两条路线，把火山模型（Volcano Iterator）虚函数逐行调用的指令开销成功压了下去。
然而，一旦单核指令流水线不再空转，现代分析型引擎会立刻撞上一堵更隐蔽、也更昂贵的物理高墙：**数据在存储层次、处理器插槽、执行算子与网卡之间的搬运及格式转换成本**。

不妨在白板上推演一条再普通不过的分析查询：在一台 4 插槽（4-Socket）、64 硬件线程的服务器上，从 Parquet 文件扫描 1 亿行事实表，与几张维表或另一张上亿行大表做等值连接，再跨 100GbE / InfiniBand 网络汇聚分布式状态。
如果我们用最朴素的教科书方案实现它，五个环节会依次崩塌：

1. **跨 NUMA 插槽带宽断崖**：如果在计划编译期用静态 `Exchange` 算子把数据按哈希切给 64 个线程，一旦上游过滤产生数据倾斜，或者哈希重分区让工作线程频繁读写远端插槽内存，4 路 Sandy Bridge EP 的总有效内存带宽会从全本地访问时的 `121 GB/s`（延迟 `101 ns`）暴跌至 25% 本地 + 75% 远端混合时的 `41 GB/s`（延迟 `257 ns`），整整三分之二的物理内存带宽蒸发在跨插槽互连链路上。
2. **大页 TLB 与缓存行未对齐惩罚**：就算在单插槽内做并行哈希连接，为了装下上亿行大表开启 `2 MiB` 操作系统大页后，Nehalem 处理器的数据页表缓存（DTLB）往往只剩 **32 个有效表项**。
   只要单趟基数分区（Radix Partitioning）扇出超过 \(2^5 = 32\) 路，并发随机散列写就会瞬间击穿 TLB；
   反过来，若不做分区直接探测共享大哈希表，紧凑打包的 48 字节桶又会横跨 64 字节缓存行边界，让每次查找平白多背 50% 的缓存缺失。
3. **星型连接顺序的指数级脆弱性**：当事实表连续连接 \(n\) 张带过滤条件的维表时，左深树有 \(n!\) 种排列。优化器只要对其中一张维表的基数估计失真，把高选择率维表排在前面、高淘汰率维表排在最后，数千万本该早早淘汰的事实表元组就会在中间连接算子间反复做哈希探测与列物化，让同一条 SQL 的最坏计划比最优计划慢上十余倍。
4. **开放列存的加载与转码高墙**：如果引擎试图省掉私有存储层、直接读取数据湖里的 Parquet / ORC 或将其转为内存 Arrow 表再过滤，超过 **90%** 的查询时间根本不在谓词求值本身，而是消耗在列块解压、变长偏移解析与跨格式内存拷贝上；
   一旦遇到 `0.1%` 选择率的随机稀疏点查，万行级数据块的元数据跳过率甚至跌到不足万分之一。
5. **微秒级网络下的 CPU 协调税**：当多端口 FDR InfiniBand / RDMA 把节点间往返延迟压到 `1 μs`、把聚合网络带宽推到 `78 GB/s`（追平单机 DDR3 内存带宽）时，如果分布式数据库只是把底层 Socket 替换成 IPoIB 或双边 RDMA 消息，不可分区事务在两阶段提交（2PC）中高达 \(5 + 8n\) 条控制消息、每条约 `3,750` 个 CPU 周期的内核与事务管理器开销，会把扩容新增的算力吞噬殆尽。

本篇精读的五篇经典论文——**Morsel-Driven Parallelism (`#25`)**、**Main-Memory Hash Joins (`#23`)**、**Looking Ahead Makes Query Plans Robust (`#22`)**、**Open Formats Deep Dive (`#45`)** 与 **The End of Slow Networks (`#46`)**——恰好沿着“**NUMA 多核调度 → L1/L2/TLB 哈希连接 → 跨连接流水线早筛 → 列存内嵌解码 → 跨机 RDMA 协议重构**”的物理路径，层层拆解了如何消除无谓的数据移动与序列化税。

---

## 多核 NUMA 与主存哈希连接：从静态 Exchange 到细粒度硬件协同

在多核主存时代，“要不要给连接输入做物理重分区”、“如何在倾斜与外部干扰下喂饱所有硬件线程”、“怎样防止一次基数估错毁掉整条星型流水线”其实指向同一组底层硬件约束。
我们先通过一张全局流水线图俯瞰 `#25`、`#23` 与 `#22` 三套机制是如何在执行引擎内部咬合运转的：

![多核主存连接流水线：NUMA Morsel 调度、LIP 前瞻过滤与硬件感知哈希连接](/images/diagrams/paper-par-morsel-radix-lip.svg)

### Morsel-Driven Parallelism：用十万行细粒度工作窃取统一 NUMA 亲和与多核弹性 {#paper-25}

> **论文信息**：Viktor Leis, Peter Boncz, Alfons Kemper, Thomas Neumann. *Morsel-Driven Parallelism: A NUMA-Aware Query Evaluation Framework for the Many-Core Age*. SIGMOD 2014, pp. 743–754. [原始 PDF（共 12 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/execution-engine/morsel-driven-parallelism-a-numa-aware-query-evaluation-framework-for-the-many-core-age.pdf)（核心图示见 p. 1 `Figure 1` 流水线切分、p. 4 `Figure 4–5` 调度器状态机、p. 5 `Figure 6` Morsel 粒度曲线与 p. 6 `Figure 7` 带标签哈希表指针）。

传统并行数据库自 Volcano 时代起便奉行**计划驱动并行（Plan-Driven Parallelism）**：优化器在编译期向计划树插入 `Exchange` 算子，把并行度 \(p\) 硬编码为 \(p\) 条独立克隆的子计划实例，靠哈希或轮询队列在线程间推送数据。
这种模型在磁盘瓶颈年代运转良好，但搬到 4 插槽 32 核 64 硬件线程的主存机器上却暴露出三重结构性硬伤：

- **编译期静态分片无法应对运行时倾斜与干扰**：哪怕把输入表按行数绝对均分（\(n/t\)），不同分片经过 `WHERE` 谓词后的存活率可能相差百倍；
  即使过滤完全均匀，现代乱序执行 CPU 上的缓存争用、超线程共享资源或另一条并发查询占走 1 个核，也会拖慢某条子计划，让其余 63 个线程在屏障处空等。
- **跨插槽 `Exchange` 洗牌引爆 QPI 互连流量**：每次跨线程重分区都意味着一次全量内存写出加跨插槽读取。论文在 4 路机器上实测表明，Nehalem EX 全本地带宽为 `93 GB/s`（延迟 `161 ns`），混入 75% 远端访问即跌至 `60 GB/s`；
  到内存控制器更强的 Sandy Bridge EP 上，全本地 `121 GB/s`（`101 ns`）更会断崖跌至 `41 GB/s`（`257 ns`）——本地与远端性能鸿沟随硬件演进反而持续拉大。
- **工作线程数无法随并发负载瞬时伸缩**：一旦 \(p\) 条流水线已经启动并持有局部算子状态，中途既没法把线程让给新到达的高优短查询，也没法在它结束后立刻接管空闲核心。

HyPer 团队提出的 **Morsel 驱动并行（Morsel-Driven Parallelism）** 彻底抛弃了计划期 `Exchange` 算子。
引擎为每个硬件线程固定绑定（Pin）且仅绑定一个工作线程，杜绝操作系统线程迁移与上下文切换；关系表在物理存储上按插槽交错切分为若干小数据块——**Morsel（碎块，默认约 \(100{,}000\) 行）**。
编译器将查询树在物化边界处拆解成若干可并行流水线段（如先并行扫描构建 \(S\)、再并行扫描构建 \(T\)、最后流式扫描 \(R\) 连续探测 \(S\) 与 \(T\)），封装进跟踪依赖关系的 `QEPobject`：

> "A task that is assigned to such a worker thread consists of a pipeline job and a particular morsel on which the pipeline has to be executed." *(PDF p. 4)*

被动式无锁调度器（Dispatcher）只在 **Morsel 边界** 分派任务，同时兑现**弹性**与**局部性**两项承诺：一方面，每个线程优先领取自己所属 NUMA 插槽上的本地 Morsel，并在本插槽本地存储区写出一切中间或结果数据；
只有当本插槽 Morsel 彻底耗尽时，才跨插槽“窃取（Work Stealing）”慢核尚未处理的远端小块，且窃取算出的输出依然写回本线程所在的本地插槽，绝不留下后续阶段的远端读写欠账。
另一方面，如果新查询到达或优先级变更，工作线程只要跑完手头这十万行（微秒至毫秒级），就能零锁开销地切换到新查询的 `QEPobject` 上。
论文 `Figure 6`（PDF p. 5）在 64 线程上扫描了 Morsel 大小：只要超过约 \(10{,}000\) 行，无锁原子队列的调度开销便彻底隐没在噪声之下；
而因为 HyPer 用 LLVM JIT 把同一流水线段内的算子编译成了寄存器紧耦合循环，Morsel **根本不需要像向量化引擎的 Vector 那样缩小去塞进 L1 Cache**，定在 \(100{,}000\) 行恰好兼顾了低调度频率与细粒度抗倾斜弹性。

要让所有线程不经 `Exchange` 重分区就能直接共享连接、聚合与排序状态，算子本身也必须与硬件原语深度协同：

1. **两阶段定容分配 + 16-bit Tag 无锁哈希连接**：构建端若边扫描边向全局哈希表扩容插入，必然陷入锁竞争或容量猜测浪费。
   HyPer 把构建拆成两步：第一阶段各线程将过滤后的构建元组收集在 NUMA 本地私有缓冲中，屏障处相加即得**精确基数**（Perfect Sizing），据此用 `mmap` 分配不小于 \(2\times\) 指针数的桶槽数组（按插槽交错并以首次触碰映射 `2 MB` 大页）；
   第二阶段所有线程并发把元组指针用单条原子 `CAS` 挂入桶头链表。之所以如此轻量，是因为连接哈希表具有纯粹的阶段分离性质：

   > "For low-cost synchronization we exploit the fact that in a join the hash table is insert-only and lookups occur only after all inserts are completed." *(PDF p. 6)*

更巧妙的是 x86-64 虚拟地址仅使用低 48 位，HyPer 利用桶头指针的高 **16 位嵌入微型标签 Bloom 过滤器**（依据哈希高比特置入 1~3 个 bit，见 `Figure 7`）。
用一段精简的 C++ 核心循环就能看清为什么这套无锁插入与早筛如此锋利：

   ```cpp
   // 64 位桶头条目：[63..48: 16-bit Bloom Tag] | [47..0: 48-bit Tuple Pointer]
   static constexpr uint64_t PTR_MASK = (1ULL << 48) - 1;

   // 构建阶段（Insert-Only）：无锁原子 CAS 挂入链表并累积 16 位标签位图
   void insertTagged(std::atomic<uint64_t>& head, Tuple* t, uint64_t hash) {
       uint64_t tagBit = 1ULL << (48 + (hash >> 58 & 0xF)); // 取高位映射 1-bit 标签
       uint64_t oldHead = head.load(std::memory_order_relaxed);
       uint64_t newHead;
       do {
           t->next = reinterpret_cast<Tuple*>(oldHead & PTR_MASK);
           newHead = reinterpret_cast<uint64_t>(t) | (oldHead & ~PTR_MASK) | tagBit;
       } while (!head.compare_exchange_weak(oldHead, newHead, std::memory_order_release));
   }

   // 探测阶段（只读免锁）：高 16 位未命中即当场淘汰，省去跨 NUMA 追逐元组指针
   inline Tuple* probeFirst(const std::atomic<uint64_t>& head, uint64_t hash) {
       uint64_t entry = head.load(std::memory_order_relaxed);
       uint64_t probeTag = 1ULL << (48 + (hash >> 58 & 0xF));
       if ((entry & probeTag) == 0) return nullptr; // 零指针解引用早筛！
       return reinterpret_cast<Tuple*>(entry & PTR_MASK);
   }
   ```

当大事实表探测遭遇大量未命中行时，只需比对桶头 64 位字的高 16 位标签即可当场拒绝，连那一次跨插槽追逐构建元组的缓存缺失都省掉了。
统计 TPC-H 与星型基准 SSB 可知，最大探测表分别占连接总输入行数的 **97.4%** 与 **99.5%**；保留维表小哈希表在全局、让探测端流式本地跑完，远比对百倍体量的探测表做物理哈希洗牌划算。

2. **两级抗倾斜聚合与精确分隔符归并排序**：如果 `GROUP BY` 既有极少数占总行数 80% 的超高频热键（如默认国家码 `'US'`），又有数百万个长尾低频键，纯共享哈希表会在热键槽上引爆原子锁争用，纯线程私有全量哈希表又会让内存膨胀 \(64\times\) 并拖垮合并阶段。
   HyPer 采用极轻量的**定容 L1/L2 线程私有小哈希表 + 本地分区溢出**：每个工作线程先在几千槽的私有表里原地累加高频热键；一旦槽位冲突满溢，便按哈希高比特把原始或局部聚合元组追加进本插槽的溢出分区缓冲，待扫描屏障结束后再由各线程按分区无锁认领完成精确汇总。
   同理，在 `ORDER BY` 并行归并排序中，各线程先对本地块独立排序，随后在全局有序段集合上通过跨段二分查找精确切分出 \(t\) 个等体积分位边界（Exact Separators）：

   ```cpp
   // 精确分隔符并行归并：在 p 个已排序本地段 runs[0..p-1] 中二分定位全局排名 targetRank
   // 使第 k 号工作线程独占归并 [sep[k], sep[k+1]) 区间，全程零锁、零负载倾斜
   Key findExactGlobalQuantile(const SortedRun* runs, int p, size_t targetRank) {
       Key lo = minKeyAllRuns(runs, p), hi = maxKeyAllRuns(runs, p);
       while (lo < hi) {
           Key mid = midpoint(lo, hi);
           size_t rankSum = 0;
           for (int i = 0; i < p; ++i) {
               rankSum += runs[i].lowerBoundIndex(mid); // 每段 O(log N_i) 二分计数
           }
           if (rankSum < targetRank) lo = nextKey(mid);
           else                      hi = mid;
       }
       return lo;
   }
   ```

把论文在两代 4 插槽硬件上的微基准与端到端实测放在一起（PDF p. 2, 9–10），NUMA 局部性与细粒度弹性的收益一目了然：

| 评测维度 | 4 路 Nehalem EX（32 核 / 64 线程） | 4 路 Sandy Bridge EP（32 核 / 64 线程） | 关键物理原因 |
| :--- | :--- | :--- | :--- |
| **全本地内存带宽 / 延迟** | `93 GB/s` / `161 ns` | `121 GB/s` / `101 ns` | 每插槽独立内存控制器就近喂数 |
| **25% 本地 + 75% 远端混合** | `60 GB/s` / `186 ns` | `41 GB/s` / `257 ns` | QPI 跨插槽链路拥塞随单路带宽提升而恶化 |
| **NUMA 感知 vs OS 默认放置** | 平均快 **`1.57×`**（最高 `4.95×`） | 平均快 **`2.40×`**（最高 `5.81×`） | 优先本地消费 Morsel + 中间态必写本地插槽 |
| **TPC-H SF100 / 单核受扰降幅** | 均耗 **`0.45s`**（`28.1×`）；受扰仅跌 **`4.7%`** | 静态 \(n/t\) 均分受单核干扰暴跌 **`36.8%`** | 十万行粒度无锁工作窃取抹平长尾慢核 |

---

### Main-Memory Hash Joins：无分区直探与多趟 Radix 在 TLB 及缓存行上的真实较量 {#paper-23}

> **论文信息**：Cagri Balkesen, Jens Teubner, Gustavo Alonso, M. Tamer Özsu. *Main-Memory Hash Joins on Modern Processor Architectures*. IEEE TKDE（扩展自 ICDE 2013）, 共 14 页. [原始 PDF（共 14 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/execution-engine/main-memory-hash-joins-on-modern-processor-architectures.pdf)（核心图示见 p. 2 `Figure 1–2` 两大家族框架、p. 5 `Figure 5–7` 桶锁同置与对齐、p. 7–9 `Figure 10–13` 六平台吞吐对比与 p. 12–13 `Figure 19` 大页 TLB 崩塌分析）。

读完 `#25` 用无分区全局哈希表横扫 TPC-H 后，一个尖锐的问题自然浮现：**如果连接的不是“小维表 × 大事实表”，而是两张双双达到上亿行、远超 L3 Cache 与 TLB 覆盖范围的巨型关系表，硬件无关（Hardware-Oblivious）的无分区连接还能赢过硬件感知（Hardware-Conscious）的多趟基数分区连接（Radix Join）吗？**

此前 Blanas 等人在 SIGMOD 2011 曾给出过“现代多核乱序执行与超线程已能掩盖缓存缺失，简单无分区连接胜过复杂 Radix 连接”的流行结论。
ETH Zurich 的 Balkesen 团队横跨 **6 套异构服务器平台**（从单路 Intel Nehalem / Sandy Bridge、4 路 Xeon E5-4640、AMD Bulldozer，到主打极致硬件多线程的 Sun UltraSPARC Niagara T2 与 256 线程 SPARC T4），设计了两组对照负载——模拟主外键不对称场景的 **Workload A**（\(|R| = 16 \times 10^6, |S| = 256 \times 10^6\)，16B 元组）与模拟双大表等体量连接的 **Workload B**（\(|R| = |S| = 128 \times 10^6\)，8B 元组）——把两大家族全部推向微架构实现极限后，揭开了截然不同的真相。

先看无分区连接（No-Partitioning Join, `NP`）自己身上的硬件陷阱。Blanas 原版用独立锁数组保护哈希桶，每次加解锁都要额外访问一块远离桶数据的内存，光把 1 字节自旋锁嵌入 8 字节桶头内部与键值同置（Co-located Latch，`Figure 5`），构建吞吐就立刻飙升 **3 倍**。
更隐蔽的是缓存行边界：当每个哈希桶紧凑封装 3 个键值槽加 8 字节头，总长恰好是 **48 字节**。在 64 字节硬件缓存行上，每 4 个连续桶占用 \(4 \times 48 = 192 = 3 \times 64\) 字节，意味着**半数哈希桶横跨两条缓存行**，单次桶访问平均触碰 \(1.5\) 条缓存行！实测硬件计数器印证了这一点（PDF p. 6）：未做对齐前，每次构建与探测的 L2/L3 缺失高达 `1.56 / 1.39` 次；
将桶填充对齐到 **64 字节** 并配合显式软件预取环形队列（Software Prefetching Ring Buffer，提前 `PDIST` 轮发出 `_mm_prefetch` 隐藏约 `200` 周期的 DRAM 访问延迟）后，缺失瞬间回落至理论单次极限 `1.01 / 1.00`，在 Intel 与 AMD 上凭空多榨出 **40% 以上**吞吐：

```c
// 64B 严格缓存行对齐哈希桶（Figure 5）：1B 自旋锁同置于 8B 桶头，杜绝跨缓存行分裂访问
struct alignas(64) AlignedBucket64B {
    uint8_t  latch;              // 同置 1 字节桶锁，构建吞吐较独立锁数组提升 3x
    uint8_t  count;              // 当前桶内已填充元组数 (0..2)
    uint16_t flags;
    uint32_t paddingHdr;
    Tuple16B tuples[3];          // 3 * 16B = 48B 键值槽
    struct AlignedBucket64B* next; // 8B 溢出桶指针 -> 8 + 48 + 8 = 刚好 64 字节！
};

// 软件预取环形缓冲探测：提前 PDIST (如 10~16) 步发起非阻塞预取，掩盖 LLC/DRAM 缺失延迟
size_t probeNPPrefetch(const AlignedBucket64B* ht, uint32_t mask,
                       const Tuple16B* s, size_t n) {
    size_t matches = 0;
    for (size_t i = 0; i < n; ++i) {
        if (i + PDIST < n) {
            uint32_t pfIdx = hashKey(s[i + PDIST].key) & mask;
            __builtin_prefetch(&ht[pfIdx], 0, 0); // 提前把未来要摸的 64B 桶拉向缓存
        }
        const AlignedBucket64B* b = &ht[hashKey(s[i].key) & mask];
        matches += scanBucketChain(b, s[i].key);
    }
    return matches;
}
```

只不过走到这一步，所谓的“硬件无关算法”早已深度依赖缓存行宽度与预取距离调参了。

再看硬件感知阵营的**并行基数连接（Parallel Radix Join, `PRO`）**。其核心思想是用相同哈希前缀将 \(R\) 和 \(S\) 物理散列成数千个极小分区 \(R_i, S_i\)，使得每个 \(R_i\) 的哈希表完全常驻 L1/L2 Cache，且紧挨着完成对应 \(S_i\) 的构建与探测以榨取时间局部性。
为了在多线程散列写出时不加写锁，`PRO` 对每趟分区跑两遍扫描：第一遍统计每线程每分区计数直方图（Histogram），做前缀和算出每个线程在每个目标分区里的独占写入偏移区间；
第二遍各线程无锁并行散列写出。但为什么不能一趟直接切成 \(2^{14} = 16{,}384\) 个分区？答案就藏在**页表缓存（TLB）**里：

> "With now only 32 TLB entries available, conventional radix partitioning seriously suffers from TLB misses even for five radix bits" *(PDF p. 13)*

在 Xeon L5520 上启用 `2 MiB` 大页后，DTLB 有效条目仅剩 **32 项**。如果单趟散列扇出超过 \(2^5 = 32\)，32 个以上并发活跃写目标页会令每次写元组都触发昂贵的硬件页表遍历（Page Walk）！同时，若扇出接近缓存组相联映射步长（2 的幂次对齐），还会诱发剧烈的 L1 组冲突缺失（Conflict Misses）。
`PRO` 用两把手术刀化解了这一危机：

1. **多趟低扇出分解（Multi-Pass Radix）**：将 14 位基数拆成 **2 趟 × 7 位**（每趟仅 128 个活跃目标流，加少量页填充错开 2 的幂次缓存组冲突），代价是一次顺序读写，换来全程留在 TLB 覆盖圈内；
2. **64 字节软件写组合缓冲（Software-Managed Buffers）**：为当前活跃的每个子分区在 L1 Cache 内开辟一个刚好等于单条缓存行（64B，即 4 个 16B 元组或 8 个 8B 元组）的微型驻留槽，先在缓存内凑满整行再一次性批量刷向主存（或配合非临时流式写指令绕过回读），彻底把随机页级散列转化成缓存行级整块传输：

   ```c
   // 单趟无锁 Radix 散列核心（以 8B 元组 Workload B 为例：每缓存行 N = 8 个槽位）
   union alignas(64) CacheLineBuffer {
       Tuple8B tuples[8];
       struct { Tuple8B data[7]; uint64_t dstOffset; } meta; // 复用末槽暂存写出游标
   };

   void radixClusterPass(const Tuple8B* in, size_t n, Tuple8B* out,
                         uint32_t* threadOffsets, CacheLineBuffer buf[FANOUT]) {
       // 1. 初始化每分区 L1 驻留缓存行槽位与由前缀和算出的独占目标偏移
       for (int p = 0; p < FANOUT; ++p) buf[p].meta.dstOffset = threadOffsets[p];

       // 2. 扫描输入，在 L1 内凑满 64B 整缓存行后再整块刷向对应输出分区
       for (size_t i = 0; i < n; ++i) {
           uint32_t p = radixBits(in[i].key);
           uint64_t dst = buf[p].meta.dstOffset;
           uint32_t slot = dst & 7U;            // 当前缓存行内槽下标 0..7
           buf[p].tuples[slot] = in[i];
           if (slot == 7U) {                    // 凑满 64 字节：整条缓存行顺序刷出！
               storeCacheLine(&out[dst - 7U], &buf[p]);
           }
           buf[p].meta.dstOffset = dst + 1;
       }
       // 3. 尾部残余不满 8 个元组的部分补刷写回
   }
   ```

六大异构微架构平台的最终对决（`Figure 10–13`，PDF p. 7–10）清晰划定了两类算法的适用疆界：

| 处理器架构 | 硬件并发与缓存特征 | Workload A（\(16\text{M} \bowtie 256\text{M}\)） | Workload B（\(128\text{M} \bowtie 128\text{M}\)） | 核心微架构胜负手 |
| :--- | :--- | :--- | :--- | :--- |
| **Intel Nehalem / Sandy Bridge** | 乱序超标量，32 KiB L1，256 KiB L2，32 项大页 TLB | `PRO` 快约 **`25%`** | `PRO` 快 **`3.5×`**（单路达 `1.96 亿/s`） | 2 趟 7-bit + 64B 写缓冲彻底消灭 TLB 与 LLC 缺失 |
| **4 路 Xeon E5-4640** | 32 核 64 线程，多插槽大内存带宽 | `PRO` 稳居领先 | 总吞吐冲至 **`4~6.5 亿元组/s`** | 分区后子任务独立常驻各核私有 L1/L2 |
| **AMD Bulldozer** | 模块化共享前端，大页 TLB 容量较宽裕 | `PRO` 领先 | `PRO` 快约 **`2.5×`** | 11-bit 单趟即可避开 TLB 断崖，省去二次读写 |
| **Sun Niagara T2** | 顺序核，每核 8 硬线程隐藏访存延迟 | `NP` 略快（省构建重排） | `PRO` 快约 **`2.5×`** | 双大表下随机探测缺失仍超过线程轮转掩盖能力 |
| **4 路 SPARC T4（256 线程）** | 每核 8 线程共争 **16 KiB** 4 路 L1（仅 512 行） | `NP` 反超 | 256 线程 `PRO` **慢于 32 线程**，`NP` 扩 >`4×` | 8 线程同核挤爆 16 KiB L1，彻底摧毁 Radix 局部性 |

最后的 SPARC T4 反例令人印象极其深刻：

> "Surprisingly, using more threads ruins the hardware-consciousness of radix join making it slower than the 32-thread case." *(PDF p. 10)*

因为极窄核心配海量硬件线程的芯片本就是靠线程轮转隐藏内存延迟，而非依赖大容量每核私有缓存。这也完美解释了为何 `#25`（非对称星型流水线，事实表不分区）与 `#23`（对称双巨表，多趟 Radix 分区）并非非此即彼的对立面，而是现代引擎按两侧基数与缓存容量动态切换的双引擎。

---

### Looking Ahead Makes Query Plans Robust (LIP)：用前瞻位图自适应重排熨平星型连接顺序风险 {#paper-22}

> **论文信息**：Jianqiao Zhu, Navneet Potti, Saket Saurabh, Jignesh M. Patel. *Looking Ahead Makes Query Plans Robust: Making the Initial Case with In-Memory Star Schema Data Warehouse Workloads*. PVLDB 2017, 10(8): 889–900. [原始 PDF（共 12 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/execution-engine/looking-ahead%20-makes-query-plans-robust.pdf)（核心图示见 p. 1–2 `Figure 1–2` SSB 24 种连接序分布、p. 6 `Algorithm 1` 自适应批采样与 p. 9–11 `Figure 4–10` SSB/TPC-H 鲁棒性剖析）。

即便单次哈希连接的微架构已经被打磨到极致，一旦放入包含一张事实表 \(F\) 与 \(n\) 张维表 \(D_1, \dots, D_n\) 的星型数据仓库流水线（\(F \bowtie D_1 \bowtie \dots \bowtie D_n\)），查询优化器仍面临一个棘手难题：如果维表上带有复杂字符串 `LIKE`、用户自定义函数或多列关联过滤条件，编译期基数估计极易失准。

看一个论文开篇在单插槽 20 核 Xeon E5-2660 v3 上实测的具体例子（`Figure 1` / `Figure 4`）：星型基准 SSB `Q4.3` 包含 4 张维表，左深连接树共有 \(4! = 24\) 种排列；
朴素哈希连接流水线在不同顺序下耗时从 `1.1s` 剧烈震荡到 `2.7s`，而在 SF100 的 `Q4.1` 上更从最优计划 `2.9s` 飙升至最坏计划 **`18.1s`**（相差 **6.2 倍**）；
在包含更复杂拓扑的 TPC-H `Q8` 枚举出的 675 个合法计划里，朴素执行更是横跨 **`2.1s` 到 `58s`**！拆解慢计划的 CPU 时间（`Figure 10`，PDF p. 11）会发现：**63% 的时间花在中间结果列复制与物化上，37% 花在昂贵的哈希表查找上**——因为原本能被最后一张高选择率维表淘汰掉的 99% 事实表元组，被迫先穿透前三张宽松维表，白白走完了多轮重型探测与元组搬运。

威斯康星大学 Quickstep 团队提出的 **前瞻信息传递（Lookahead Information Passing, `LIP`）** 给出了一个极其优雅的解耦思路：**既然哈希表探测与中间物化太贵，为什么不在构建每张维表 \(D_i\) 的哈希表时顺手生成一张极紧凑的常驻缓存 Bloom 过滤器，把全部 \(n\) 张维表的半连接（Semi-Join）摘要统统前推到最底层的事实表扫描端先过一遍筛子？**

> "a lookahead filter can be forwarded from the build tables involved in downstream joins to the probe table, where they can be applied prior to performing the hash table probe." *(PDF p. 5)*

但只把 \(n\) 个 Bloom 过滤器下推到事实表还不够——如果按优化器选错的糟糕顺序依次探测这 \(n\) 个 Bloom 过滤器，高通过率过滤器依然排在最前面！`LIP` 的点睛之笔在于：**物理连接算子树的拓扑顺序一旦编好很难在线重排，但下推到扫描端的 \(n\) 个无状态 Bloom 过滤器却是纯集合交集判定，完全可以在运行期零代价动态重排！**

具体在每个数 MB 大小的存储块（Storage Block）扫描开始时，`LIP` 启动**倍增批自适应采样**：先取首批 `64` 行事实表外键引用，逐一穿过当前序列的各张 Bloom 过滤器并记录各自在这个批次上的实测通过率（Passing Selectivity）；
随后立即将拒绝率最高（即实测通过率 \(\sigma'\) 最低）的 Bloom 过滤器调到最前端，并把下一轮采样批次翻倍（`64 → 128 → 256 ...`），既在块首几百行内迅速锁定当前数据块的局部最优过滤次序，又在新数据块边界重置统计以适应时间/地域分区的数据倾斜（注：论文 p. 6 `Algorithm 1` 伪代码第 18 行将 `miss/count` 标成了升序，阅读时应以 p. 6 正文 §4.1.3 按通过选择率升序、即淘汰率降序排列的物理机制为准）。

在进入公式之前，我们先用一组 **1,000 万行事实表连接 4 张维表** 的具体数字直观感受 `LIP` 究竟省下了什么。设 4 张维表的真实过滤选择率分别为 \(\sigma_1 = 0.8\)、\(\sigma_2 = 0.5\)、\(\sigma_3 = 0.2\)、\(\sigma_4 = 0.01\)，优化器因估错把最苛刻的 \(D_4\) 排在了最后一跳：

- **朴素最坏连接序 \((D_1 \to D_2 \to D_3 \to D_4)\)**：事实表需要依次执行 \(10\text{M} + 8\text{M} + 4\text{M} + 0.8\text{M} = \mathbf{22.8\text{M}}\) 次重型哈希表查找，并在前三层算子间搬运物化 **\(12.8\text{M}\) 行**中间元组，最后才发现只有 \(8{,}000\) 行真正存活！
- **LIP 自适应重排后**：首个存储块前 `64` 行采样一结束，过滤器链立即重排为 \((\text{BF}_4 \to \text{BF}_3 \to \text{BF}_2 \to \text{BF}_1)\)（设 8-bit Bloom 假阳性率 \(\epsilon \approx 0.02\)）。
  1,000 万行在首道 \(\text{BF}_4\) 处就被拦下 97%（仅约 \(30\text{ 万}\) 行进入 \(\text{BF}_3\)），全程总共只做约 **\(10.37\text{M}\) 次极轻量的缓存内位运算**，最终放进上层物理哈希连接树的幸存元组骤降至约 **1 万行**——哪怕上层哈希连接算子树仍然维持糟糕的 \((D_1 \to D_2 \to D_3 \to D_4)\) 顺序不动，重型哈希探测与列物化也已经缩水了 **99.9%**！

```python
# LIP 单存储块（Storage Block）内的倍增批自适应过滤器重排核心逻辑
def lip_filter_block(fact_block_refs, bloom_filters):
    batch_size = 64                       # 新存储块重置起始采样窗口，适应局部数据分布倾斜
    offset = 0
    order = list(range(len(bloom_filters)))
    survivors = []

    while offset < len(fact_block_refs):
        cur = fact_block_refs[offset : offset + batch_size]
        offset += batch_size
        stats = []                        # 记录当前轮每个过滤器的实测通过率 pass_ratio
        for idx in order:
            bf = bloom_filters[idx]
            in_len = len(cur)
            if in_len == 0:
                stats.append((1.0, idx))
                continue
            cur = [r for r in cur if bf.contains(r.fk[idx])]
            stats.append((len(cur) / in_len, idx))
        survivors.extend(cur)
        # 按实测通过选择率升序（即拒绝率最高者最先执行）重排后续批次的位图探测序
        stats.sort(key=lambda x: x[0])
        order = [idx for _, idx in stats]
        batch_size *= 2                   # 采样窗口指数倍增，摊薄统计与排序开销
    return survivors                      # 仅极少数幸存引用进入下游固定顺序的 Hash Join 树
```

用论文构建的代价与 **\(\Theta\)-鲁棒性模型** 可以严格推广这一结论。记各维表真实选择率为 \(\sigma_1, \dots, \sigma_n\)（重排后最优递增序为 \(\sigma'_1 \le \sigma'_2 \le \dots \le \sigma'_n\)），单次哈希探测单位代价设为 \(1\)，单次 L1/L2 驻留 Bloom 探测代价为 \(\beta \ll 1\)（默认每元组分配 8 bits、对连续整数代理键取 \(k=1\) 恒等映射函数时，构建可由线程局部生成后按位 `OR` 无锁合并，\(\beta\) 仅占完整哈希探测加物化的几十分之一），Bloom 假阳性率为 \(\epsilon\)。

- **朴素流水线的顺序敏感度**：对于任意给定的物理连接序 \((1, \dots, n)\)，探测总成本正比于 \(|F| \sum_{i=1}^n \prod_{j=1}^{i-1} \sigma_j\)。
  将最坏计划 \(E_w\) 与最优计划 \(E_b\) 之差按 \(|F|(\sigma_{\max} - \sigma_{\min})\) 归一化得到脆弱度指标（当 \(\sigma_{\max} \neq \sigma_{\min}\) 时，见 p. 4 Eq. 9）：

  $$R = \frac{T(E_w) - T(E_b)}{(\sigma_{\max} - \sigma_{\min})|F|}$$

朴素计划直接暴露在维表选择率跨度的常数倍惩罚之下。
- **LIP 流水线的顺序免疫性**：所有事实表元组先按自适应锁定的最优次序 \((1', \dots, n')\) 穿过廉价 Bloom 链，只有最终幸存的约 \(|F| \prod_{i=1}^n (\sigma_i + \epsilon)\) 行才会真正进入上层物理哈希连接树：

  $$\text{Cost}_{\text{filter}} \approx \beta |F| \left(1 + \sum_{i=1}^{n-1} \prod_{j=1}^{i} (\sigma'_j + \epsilon)\right)$$

注意等式右侧完全由**最优排序 \(\sigma'_j\)** 决定，与优化器选定的物理连接次序 \((1, \dots, n)\) 彻底解耦：

  > "Note that this cost has the optimal selectivity order 1', 2', ..., n' on the right hand side, but is independent of the selected join order 1, 2, ..., n." *(PDF p. 7)*

至于上层物理哈希树在不同顺序间的残留成本差，仅剩假阳性漏网之鱼引发的一阶微量，归一化最大计划散度收敛至：

  $$\Theta = \frac{1}{2} \frac{\prod_{i=1}^n \sigma_i}{\sigma_{\min}\sigma_{\max}} \epsilon n(n+1)$$

（PDF p. 8 Eq. 17）。这里 \(\epsilon\) 与 \(\beta\) 构成了清晰的缓存权衡：位图给得越大，\(\epsilon\) 越小、上层残留散度越趋近于 0，但一旦撑破 L2/L3 Cache 推高 \(\beta\)，扫描端位图探测本身就会变慢——实测 8 bits/tuple 恰好落在甜点区。

实验结果令人惊叹：在 SSB SF100 全套查询的所有 24 种左深计划中，`LIP` 把 `Q4.1` 的计划耗时极差压进了 **5% 以内**，全基准**最坏**连接序下的 `LIP` 总耗时（`17.4s`）甚至快过了朴素引擎精挑细选出的**最优**连接序（`17.6s`，最坏朴素则高达 `90.9s`）！中间物化占比从 63% 骤降至 **4%**；
在 10 GB、12 维表的合成压力基准上，平均最坏—最优计划极差从 `14 秒` 被直接熨平到 **`150 毫秒`**。当然，若查询并非星型等值内连接、或者维表几乎没有过滤性（\(\sigma_i \to 1\)）导致 \(\beta |F|\) 筛除不了多少元组，现代引擎（如 DuckDB、StarRocks 与 Spark）便会结合 `#25`/`#22` 的思路，在运行期监控首批 Morsel 的实际拦截率，对毫无收益的过滤器实施动态熔断旁路。

---

## 列存编码与高速 RDMA 网络：打破解码转码与分布式协调的 CPU 新高墙

解决了单机内存里的多核连接流水线之后，数据从哪里进入内存、又如何跨节点交换？在云原生湖仓一体与高速数据中心网络兴起的今天，存储层普遍拥抱开放列式格式（Parquet / ORC / Arrow），网络层普遍升级到百吉比特 RDMA。
然而 `#45` 与 `#46` 两篇论文共同揭示了一个反直觉事实：**当磁盘 I/O 与慢速以太网瓶颈退场后，照搬旧软件栈会把绝大部分时间浪费在 CPU 解码转码与双边控制消息上**。

![开放列存与高速 RDMA 网络：绕开解码转码与双边协议的 CPU 新高墙](/images/diagrams/paper-par-formats-and-fast-network.svg)

### Open Formats Deep Dive：为什么直接把 Parquet/ORC/Arrow 塞进分析引擎会撞上转码墙 {#paper-45}

> **论文信息**：Chunwei Liu, Anna Pavlenko, Matteo Interlandi, Brandon Haynes. *A Deep Dive into Common Open Formats for Analytical DBMSs*. PVLDB 2023, 16(11): 3044–3056. [原始 PDF（共 13 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/formats/a-deep-dive-into-common-open-formats-for-analytical-dbmss.pdf)（核心图表见 p. 3 `Table 1–2` 与 `Figure 2–3` 物理布局对齐、p. 5 `Table 5–6` 3.1 万真实列压缩率、p. 7–9 `Figure 9–18` 转码与投影锁剖析及 p. 12 `Figure 22` 原位字典 SIMD 加速）。

在湖仓架构下，既然数据已经以 **Apache Parquet** 或 **Apache ORC** 躺在对象存储上，内存交换又有 **Apache Arrow** 标准，分析型 DBMS 能不能干脆放弃专有内部存储格式，直接把这些开放格式“原样吞入（Direct Subsumption）”当作原生底座？微软 Gray Systems Lab 团队基于真实企业语料库（约 **31,000 个真实业务列**、原始 CSV 约 `489.7 GB`）及 TPC-H / TPC-DS，对 Arrow `5.0.0`、ORC `1.7.2` 与 Parquet（Java `1.9.0` / C++）展开了像素级解剖，揪出了横亘在磁盘文件与向量化寄存器之间的四道暗礁。

第一道暗礁是**空间紧凑度与内存直接寻址的天然撕裂**。在不叠加 Zstd/Snappy 等通用字节块压缩、纯粹考察列编码本身时（`Table 5`，PDF p. 5），三种开放格式在真实企业宽表语料库上的表现大相径庭：

| 维度 | Apache Parquet | Apache ORC | Apache Arrow (Feather) |
| :--- | :--- | :--- | :--- |
| **水平分批单元（Row Batch）** | Row Group（常按字节数切，默认数万~百万行） | Stripe（默认约 64 MB）+ 10,000 行索引步长 | Record Batch（内存零拷贝切片单元） |
| **核心轻量列编码** | 积极全类型字典 + `RLE_DICTIONARY` 混合位打包 + Delta | `RLEv2`（Short Repeat / Direct / Patched Base / Delta）+ 保守字符串字典 | 默认无编码扁平缓冲区（变长列带 4B 偏移数组）；可选字典 |
| **489.7 GB 真实 CSV 纯编码体积** | **`64.7 GB`（压缩比 `0.13`）** | **`133.9 GB`（压缩比 `0.27`）** | 默认 **`522.5 GB`（`1.07`）**；开字典 `237.4 GB`（`0.48`） |
| **嵌套类型表达与细粒度投影** | Dremel Repetition / Definition Level；列块独立寻址 | 显式 Length / Present 流；列条带投影严格线性缩放 | 嵌套偏移与 Validity 位图数组；评测读取器受整批解析与批锁拖累 |

为什么面向零反序列化内存随机访问设计的默认 Arrow Feather 竟然膨胀到了 **`522.5 GB`**（比原始 CSV 文本还大 7%）？罪魁祸首正是真实业务语料中大量短字符串/空值列每行强制携带 4 字节偏移头与 8/64 字节缓冲区对齐填充。
顺带一提，通用字节压缩级别也并非越高越好：把 Zstd 从 Level 5 拉到 Level 9，空间节省不到 `1.5%`，压缩 CPU 耗时却暴涨 `2~3 倍`（PDF p. 6）。

第二道暗礁是**跨格式桥接转码与细粒度投影失灵**。许多现代引擎采用“Parquet 读盘 → 解压转码成内存 Arrow Table → 在 Arrow 上跑向量化过滤”的流行管线。
然而实测火焰图无情指出：

> "we found that the majority of the time (i.e., >90%) is spent on data loading and decoding, whereas the contribution of the execution of the filter condition is minimal." *(PDF p. 9)*

超过 **90%** 的端到端时间都花在了把压缩页拆解并物化成完整内存列数组上！更糟糕的是库实现层面的同步陷阱：在论文评测的 Arrow Feather 读取器上，从宽表中仅投影 **1 列**竟然只比读取**整张表全部列**快约 **2 倍**——因为它不仅把整个 `Record Batch` 悉数解析解压，更有高达约 **80%** 的运行时间消耗在批次级锁获取（Lock Acquisition）上（`Figure 12–13`，PDF p. 8）；
反观 ORC 的列块条带（Stripe）独立寻址设计则严格实现了耗时随投影列数成比例下降。

第三道暗礁是**稀疏随机过滤下块级元数据跳过（Chunk-Level Skipping, `CLS`）的概率崩塌**。Parquet Row Group（常设为数十万至百万行）和 Page 依赖 `Min/Max` 统计量跳块。
假设过滤条件在全表呈均匀随机分布，单行命中选择率仅为 \(s = 0.001\)（千分之一），而一个数据块包含 \(m = 10{,}000\) 行；
那么该块至少命中一行、从而**无法被跳过**的概率高达：

$$P(\text{hit}) = 1 - (1 - s)^m = 1 - (0.999)^{10000} \approx 99.9955\%$$

换言之，只要数据未按过滤列预排序，哪怕总选择率低至 `0.1%`，99.995% 的数据块也照样得老老实实加载进内存解码！

那么，磁盘紧凑格式真的注定没法跑出原生内存分析引擎的速度吗？论文第 6 节做了一组极具启发性的受控改造实验（`Figure 22`，PDF p. 12）：他们在内存中保留 Parquet 的紧凑页结构（`P-IM`），砍掉面向行的流式物化与多余拷贝，把投影列按需延迟映射，再将等值/范围谓词**直接下推到字典编码域（Direct Execution on Dictionary Codes, `+D`）**先对小字典求值生成命中码字位图，最后用 **`AVX-512` SIMD** 指令直接在紧密位打包（Bit-packed）的整数码流上批量比对：

```cpp
// P-IM + D + AVX-512 核心机制：跳过整列字符串解码，直接在压缩字典码字上做 SIMD 过滤
void filterParquetDictPageSIMD(const DictPage& dict, const uint32_t* bitPackedCodes,
                               size_t numRows, const Predicate& pred, uint64_t* outSelMask) {
    // 1. 仅对几百/几千项的小字典求值一次，将昂贵字符串谓词编译为紧凑字典码命中集
    //    若有序字典满足单调范围，更可直接压缩为单个连续整数区间 [codeLo, codeHi]！
    auto [codeLo, codeHi] = dict.evalToContiguousCodeRange(pred);
    __m512i vLo = _mm512_set1_epi32(codeLo);
    __m512i vHi = _mm512_set1_epi32(codeHi);

    // 2. 单指令解包 16 个字典整数码字并生成 16-bit 选择掩码，零 Arrow 字符串物化拷贝！
    for (size_t i = 0; i < numRows; i += 16) {
        __m512i vCodes = unpack16CodesAVX512(bitPackedCodes, i);
        __mmask16 geLo = _mm512_cmpge_epu32_mask(vCodes, vLo);
        __mmask16 leHi = _mm512_cmple_epu32_mask(vCodes, vHi);
        appendMask16(outSelMask, i, geLo & leHi); // 仅对最终幸存行按需延迟解码投影列
    }
}
```

> "P-IM+D is up to 60× faster than the Parquet baseline. Finally, if we enable SIMD execution (AVX_512) we observe up to 100× speedup compared with the Parquet baseline." *(PDF p. 12)*

**60 倍乃至 100 倍**的端到端提速一针见血地指明了演进方向（这也与后来 Meta Velox、DuckDB 新版 Parquet 扫描器及新一代开放列格式 Vortex / Lance 的设计不谋而合）：开放格式本身并不慢，慢的是“先全量解码成扁平 Arrow 数组再计算”的分层割裂；
唯有让执行引擎直接理解字典、RLE 与紧密位宽编码，把延迟物化（Late Materialization）贯彻到列块内部，才能同时兼得 `0.13` 的存储压缩比与百倍寄存器吞吐。

---

### The End of Slow Networks：当 RDMA 追平内存带宽，分布式事务与连接必须如何重设计 {#paper-46}

> **论文信息**：Carsten Binnig, Andrew Crotty, Alex Galakatos, Tim Kraska, Erfan Zamanian. *The End of Slow Networks: It's Time for a Redesign*. PVLDB 2016, 9(7): 528–539（arXiv 预印本共 12 页）. [原始 PDF（共 12 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/network/the-end-of-slow-networks%3A-it%27s-time-for-a-redesign.pdf)（核心图示见 p. 1 `Figure 1` 网存带宽收敛、p. 3 `Figure 2–4` RDMA 延迟带宽与 NAM 架构、p. 7–8 `Figure 5–6` RSI 事务吞吐及 p. 10–11 `Figure 7–8` RRJ 连接对比）。

过去三十年里，几乎所有分布式数据库（从早期 Gamma、R\* 到现代 NewSQL 与 MPP 数仓）都建立在同一条铁律之上：**跨机网络比本机内存慢几个数量级，因此架构上必须采用无共享（Shared-Nothing）静态分片，事务上能分区绝不跨节点，连接上宁可本机多扫几遍做半连接压缩也绝不多传一个字节**。

然而硬件天平在 InfiniBand 与 RoCE（RDMA over Converged Ethernet）普及时发生了历史性翻转。
论文开篇在真实机器上给出了一组震撼对比（`Figure 1`，PDF p. 1）：单通道 DDR3-1600 内存子系统的理论峰值带宽为 `102.4 GB/s`（实测半双工约 `78.4 GB/s`），而一台插满 4 张双端口 FDR 4× InfiniBand 网卡的服务器，其聚合全双工理论网络带宽已达 **`109.1 GB/s`**（实测 **`78.0 GB/s`**）——**跨节点网络总带宽第一次与单插槽本地内存带宽站在了同一水平线上**！单端口单边 RDMA 在消息大于 `2 KB` 时即可跑满 `~6.8 GB/s`，发送端仅耗费约 `450` 个 CPU 周期且远端 CPU 零参与。
不过，作者在第一页就异常清醒地划清了物理边界——带宽追平绝不等于延迟归零，更不等于拥有跨机硬件缓存一致性：

> "Yet, we do not argue that the network latency will become as fast as the memory latency. Instead, cache- and memory-locality will play an even more important role" *(PDF p. 1)*

实测 `1 KB` 单边 RDMA 读延迟约为 **`1 μs`**，虽比传统 TCP/IP 快数十倍，却仍比本地 DRAM（`~0.08 μs = 80 ns`）慢 **12.5 倍**，比 L1/L2 Cache 更慢上百倍。

那么，如果保留传统 Shared-Nothing 架构不变，仅仅把底层 TCP Socket 换成 IPoIB 或者双边 RDMA `SEND/RECV` 消息，能不能白捡硬件红利？论文通过严谨的微观周期拆解证明：**完全行不通**。
考虑一个无法静态完美分区的跨 \(n\) 节点分布式事务，标准两阶段提交（2PC）总共需要交换 **\(5 + 8n\) 条消息**（即便并行群发，客户端感知延迟也包含 4 个单向网络跳，锁持有与可见性窗口更跨越 6~9 个跳步）。
在 TCP/IPoIB 栈下，内核协议栈拷贝、中断与分布式事务管理器（TM）锁表调度每处理一条消息就要烧掉数千个 CPU 周期；哪怕换用绕过内核的双边 RDMA Verbs，远端工作线程仍需轮询接收、反序列化请求、竞争本地锁表与日志缓冲（平均每消息仍需约 `3,750` 周期）。
结果就是残酷的反扩展悖论（`Figure 6` 理论上限与实测）：

> "the message overhead essentially consumes all the added CPU power, making the system inherently unscalable (if the workload cannot be partitioned)." *(PDF p. 7)*

在重跨机事务下，IPoIB 仅跑出 `22k TPS`、IPoEth `32k TPS`；双边 RDMA 虽一度冲到 `1.1M TPS`，但客户端增至 70 个时便因远端 CPU 协调饱和反跌回 `320k TPS`。

为了真正释放微秒级 RDMA 的潜力，Binnig 等人提出了将计算节点与“愚蠢”的共享内存池在逻辑上彻底解耦的 **网络附加内存架构（Network-Attached Memory, `NAM`）**，并针对 OLTP 与 OLAP 分别重构了两套零远端 CPU 参与的核心协议：

1. **面向 OLTP 的单边 RDMA 快照隔离（`RSI`）**：既然双边消息卡在远端 CPU 锁管理器上，不如让内存池节点完全不跑事务逻辑，由计算客户端直接通过网卡硬件原子指令操作远端内存槽位！每条记录的版本槽头部是一个精心编排的 **64 位字：`[1-bit Lock | 63-bit Latest Commit ID]`**。
   走一遍客户端提交写集记录的硬件原语序列（PDF p. 8），就能明白它为何完全不需要远端锁管理器：

   ```c
   // 远端 NAM 内存槽头：最高位 bit 63 为排他写锁，低 63 位为最新提交时间戳 CID
   static constexpr uint64_t LOCK_BIT = 1ULL << 63;

   bool rsiCommitRecord(RdmaQp* qp, RemoteAddr slotHdr, RemoteAddr slotData,
                        uint64_t readSnapshotTs, uint64_t commitId, const Row& newRow) {
       // 1. 单边硬件原子 CAS：仅当记录未加锁（bit63=0）且版本仍等于快照读到的 20003 时，
       //    一次网络往返同时完成“排他加锁 + 写写冲突校验”，远端 CPU 0 周期参与！
       uint64_t expected = readSnapshotTs;             // 例如 20003
       uint64_t desired  = LOCK_BIT | readSnapshotTs;  // (1ULL << 63) | 20003
       if (rdmaCompareAndSwap(qp, slotHdr, expected, desired) != expected) {
           return false; // 版本已推进或正被并发事务锁定 -> 立即回滚重试
       }

       // 2. 单边 RDMA WRITE 写入新记录体，并以新 commitId 覆盖头部同时原子清零锁位
       rdmaWriteUnsignaled(qp, slotData, &newRow, sizeof(Row));
       rdmaWriteSignaled(qp, slotHdr, &commitId, sizeof(uint64_t)); // bit63=0 解锁！
       return true;
   }
   ```

完成写集更新后，客户端再用选择性信号（Selective Signaling，每隔若干次无信号操作才请求一次完成队列 CQE 通知，防止网卡完成队列溢出）推进一个 60,000 位的紧凑提交完成位图向量。
在 8 节点集群低冲突闭环压测下，`RSI` 轰出了 **180 万分布式 TPS（`1.8M TPS`）**，比双边 RDMA 再提速数倍且随节点线性扩展（当然，该原型每槽仅存 \(n=1\) 个版本、遇旧快照直接回滚，且客户端崩溃遗留远端死锁的租约恢复与持久化日志留作了后续工程课题，见后续 NAM-DB / FaRM 演进）。

2. **面向 OLAP 的缓存驻留 RDMA 基数连接（`RRJ`）**：在分布式大表连接上，传统无共享 Grace Hash Join（`GHJ`）因恐惧慢网络，往往不惜在本地多遍读写内存构造分区或半连接过滤。
   设总输入字节数为 \(B = w_R |R| + w_S |S|\)，本地内存单字节访问代价为 \(c_{\text{mem}}\)，网络单字节传输代价为 \(c_{\text{net}}\)：

   $$\begin{aligned}
   \text{Cost}_{\text{GHJ}} &= B \cdot (4 c_{\text{mem}} + c_{\text{net}}) \\
   \text{Cost}_{\text{RRJ}} &\approx B \cdot \max(2 c_{\text{mem}},\; c_{\text{net}}) \xrightarrow{c_{\text{net}} \approx c_{\text{mem}}} 2 c_{\text{mem}} B
   \end{aligned}$$

当高速网络令 \(c_{\text{net}} \approx c_{\text{mem}}\) 时，**本地多做两次内存读写拷贝（\(2 c_{\text{mem}} B\)）居然比直接跨网发走还要贵**！`RRJ` 借鉴 `#23` 的软件写组合思想，在计算节点 **L3 Cache** 内为每个远端目标分区维护 \(\ge 2\text{ KB}\) 的驻留输出缓冲，凑满即调用异步无信号单边 **`RDMA WRITE`** 直写远端内存目标偏移，将本地扫描与跨网散列完美流水重叠，把理想均匀一趟分区的连接总代价一路压向 **\(2 c_{\text{mem}} B\)** 的物理下界，在 `Figure 8` 实测中全面击败分布式 `GHJ` 与半连接变体。

---

## 横向对比：五篇经典论文背后的数据移动与 CPU 成本统一账本

把这五篇横跨多核调度、CPU 缓存/TLB、查询计划鲁棒性、列存编码与高速网络的论文放在同一张系统工程账本下对照，就能看清现代高性能分析与混合负载引擎的完整拼图：

| 论文与年份 | 所处硬件 / 系统边界 | 朴素方案的真实崩塌点 | 核心机制创新 | 关键量化收益（原文口径） | 适用前提与代价边界 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **[#25](#paper-25) Leis et al. *Morsel* (SIGMOD 2014)** | 多插槽 NUMA 多核调度与共享连接/聚合 | 静态 `Exchange` 受倾斜与慢核拖累；跨插槽洗牌令带宽从 `121` 跌至 `41 GB/s` | ~10 万行 Morsel 无锁工作窃取 + 本地写回 + 两阶段定容 16b-Tag `CAS` 哈希表 | TPC-H SF100 均耗 `0.45s`（64 线程 `28.1×`）；单核受扰仅跌 `4.7%` vs 静态 `36.8%` | 依赖探测表远大于构建表的星型偏态；极端双巨表超缓存时探测仍有随机缺失 |
| **[#23](#paper-23) Balkesen et al. *Hash Joins* (TKDE)** | 单/多路 CPU 的 L1/L2 Cache、缓存行与大页 DTLB | `NP` 48B 桶跨缓存行多 50% 缺失；单趟高扇出散列在 5 bit 即打穿 32 项大页 TLB | 64B 桶锁同置对齐 + 软件预取（`NP`）；多趟低扇出直方图散列 + 64B 写缓冲（`PRO`） | 双 1.28 亿行连接在 Intel 上 `PRO` 比调优 `NP` 快 **`3.5×`**（单路 `1.96 亿行/s`） | 在每核仅 16 KiB L1 却配 8 硬线程的 SPARC T4 上，满线程 `PRO` 因争抢 L1 反输给 `NP` |
| **[#22](#paper-22) Zhu et al. *LIP* (PVLDB 2017)** | 星型多表连接流水线与基数估计误差 | 估错连接序导致海量待淘汰事实表行做无效哈希探测与中间列复制（占时 63%+37%） | 全维表紧凑 Bloom 下推事实表扫描 + 块内 64 行起倍增采样**自适应过滤器重排** | SSB SF100 最坏 `LIP` (`17.4s`) 快于最优朴素 (`17.6s`)；12 维极差 `14s → 150ms` | 聚焦星型等值内连接；当维表几无选择性（\(\sigma_i \to 1\)）时需防纯过滤器探测负优化 |
| **[#45](#paper-45) Liu et al. *Open Formats* (PVLDB 2023)** | 开放列存文件（Parquet/ORC）与内存交换格式（Arrow） | 默认 Feather 膨胀至 CSV `1.07×`；过滤 `>90%` 耗时在加载解码；稀疏 `s=0.1%` 跳块失灵 | 原位内存列块按需延迟映射（`P-IM`）+ 字典码字域直接求值（`+D`）+ `AVX-512` SIMD | Parquet 编码达 CSV `0.13×`；`P-IM+D+SIMD` 比流式基线最高提速 **`60×~100×`** | 要求引擎算子打破分层黑盒、直接感知字典与位打包编码；高阶 Zstd 压缩写代价陡增 |
| **[#46](#paper-46) Binnig et al. *Slow Networks* (PVLDB 2016)** | 多口 FDR InfiniBand / RDMA 集群事务与分布式连接 | 仅换 IPoIB/双边 RDMA 仍困于 `5+8n` 条 2PC 消息（每条 `~3750` 周期），扩容不涨吞吐 | `NAM` 存算解耦 + 单边 `CAS` 64b 锁版本头 `RSI` + L3 驻留 `>=2KB` 异步写 `RRJ` | 8 节点 `RSI` 达 **`1.8M` 分布式 TPS**；`RRJ` 重叠网存带宽逼近 \(2 c_{\text{mem}} B\) | RDMA `1 μs` 仍比本地 DRAM 慢 `12.5×` 且无跨机缓存一致性；客户端死锁恢复与多版本留待完善 |

今天审视 DuckDB、Umbra、Velox 与云原生分布式引擎的内核代码，这五篇论文几乎拼成了它们的标准骨架：调度层普遍采用 `#25` 的 Morsel 并行工作窃取取代僵化的静态线程绑定；
哈希连接算子内部同时内置 `#25` 的全局带标签指针快速探测通道与 `#23` 的多趟分区溢出/缓存局部化通道，并在扫描端挂载 `#22` 的动态自适应 Bloom 下推过滤；
存储扫描器照着 `#45` 的结论把字典谓词和延迟物化一路压进 Parquet 页解码器内部；而在跨节点高速互连层，`#46` 对“硬件变快后 CPU 序列化与双边协调反成头号瓶颈”的洞察，依然是今天设计 RDMA / CXL 内存池化数据库的最高准则。

---

## 参考文献与阅读基线

本文精读的所有论文均对齐开源论文集 [`lonng/db-papers`](https://github.com/lonng/db-papers)（Git 基线提交 `4ff1331`），文中引用的页码均为对应文件内的 PDF 物理页码：

1. **#25**：Viktor Leis, Peter Boncz, Alfons Kemper, Thomas Neumann. *Morsel-Driven Parallelism: A NUMA-Aware Query Evaluation Framework for the Many-Core Age*. SIGMOD 2014, pp. 743–754. [原始 PDF（共 12 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/execution-engine/morsel-driven-parallelism-a-numa-aware-query-evaluation-framework-for-the-many-core-age.pdf)
2. **#23**：Cagri Balkesen, Jens Teubner, Gustavo Alonso, M. Tamer Özsu. *Main-Memory Hash Joins on Modern Processor Architectures*. IEEE TKDE, 14 pages. [原始 PDF（共 14 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/execution-engine/main-memory-hash-joins-on-modern-processor-architectures.pdf)
3. **#22**：Jianqiao Zhu, Navneet Potti, Saket Saurabh, Jignesh M. Patel. *Looking Ahead Makes Query Plans Robust: Making the Initial Case with In-Memory Star Schema Data Warehouse Workloads*. PVLDB 2017, 10(8): 889–900. [原始 PDF（共 12 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/execution-engine/looking-ahead%20-makes-query-plans-robust.pdf)
4. **#45**：Chunwei Liu, Anna Pavlenko, Matteo Interlandi, Brandon Haynes. *A Deep Dive into Common Open Formats for Analytical DBMSs*. PVLDB 2023, 16(11): 3044–3056. [原始 PDF（共 13 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/formats/a-deep-dive-into-common-open-formats-for-analytical-dbmss.pdf)
5. **#46**：Carsten Binnig, Andrew Crotty, Alex Galakatos, Tim Kraska, Erfan Zamanian. *The End of Slow Networks: It's Time for a Redesign*. PVLDB 2016 / arXiv:1504.01048v2, 12 pages. [原始 PDF（共 12 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/network/the-end-of-slow-networks%3A-it%27s-time-for-a-redesign.pdf)
