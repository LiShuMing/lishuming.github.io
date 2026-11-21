---
title: "资深 C++ 与计算引擎内核底座：内存分配器、句柄布局与核心数据结构"
slug: "cpp-systems-core-data-structures-and-kernel-design"
date: 2026-11-19T09:42:00+08:00
lastmod: 2026-11-21T00:00:00+08:00
aliases:
  - "/2026/11/19/modern-memory-allocators-and-compute-engines/"
  - "/2026/11/18/vectorized-engine-cpp-memory-and-gpu-mapping/"
categories:
  - 算法与数据结构
  - 系统工程
  - 数据库
tags:
  - C++
  - 内存分配器
  - jemalloc
  - tcmalloc
  - mimalloc
  - Velox
  - 向量化执行
  - 无锁队列
  - RadixSort
  - RoaringBitmap
  - BloomFilter
  - LSM-Tree
  - 性能优化
description: "自底向上打通资深 C++ 与计算引擎内核底座：从 ptmalloc2 到 jemalloc/tcmalloc/mimalloc 的分配器演进与 ClickHouse/Doris/StarRocks/Velox 内存治理，到 intrusive_ptr 句柄布局、内存池与两阶段多态，再到无锁队列、排序归并、无分支二分、位图、概率过滤器、存储树与扁平化字符串匹配。"
draft: false
toc: true
math: true
---

在一台 64 核服务器上运行高并发 C++ 计算引擎，工程师往往会在第一时间撞上几件“反教科书”的现实：并发度刚拉到 32 线程，`perf top` 榜首不是业务算子而是 `glibc` 内部的 `__lll_lock_wait` 与 `_int_malloc`；一波大 SQL 跑完、逻辑存活对象只剩 2 GB，容器 RSS 却死死停在 40 GB 直到触发 `OOMKilled`；翻开 **Meta Velox**、**ClickHouse** 或 **DuckDB** 的源码，核心热路径几乎一致抛弃了 `std::shared_ptr`、`std::unordered_map`、`std::list`、`std::sort` 与全局 `new`/`malloc`。

这些取舍很少源于渐近时间复杂度 $O(N \log N)$ 或 $O(1)$ 的优劣，而是被现代硬件的四把物理标尺逼出来的：**多核锁竞争与 64B 缓存行一致性（Cache Coherency）**、**TLB 覆盖率与核间 Shootdown**、**不可预测分支引发的流水线冲刷**，以及**随机小块写带来的存储放大**。下文沿着“用户态分配器与引擎内存治理 → 数据面句柄与两阶段多态 → 内核核心数据结构”的自底向上脉络，把这套 C++ 系统底座拆解清楚。

---

## 一、现代 CPU 下的用户态内存分配器与双层治理

如果把内存分配器看作“应用程序与操作系统虚拟内存之间的翻译官”，它的每一次架构换代（`dlmalloc` → `ptmalloc2` → `jemalloc` / `tcmalloc` → `mimalloc`），都在回答多核锁竞争、空间局部性、TLB Shootdown 与跨线程释放碎片这四个物理难题。

### 1.1 为什么 `dlmalloc` 与 `glibc ptmalloc2` 在现代 CPU 上会崩？

1990 年代的 `dlmalloc` 为单核设计：向内核 `brk`/`sbrk` 申请连续虚拟地址后按任意大小切分，用双向空闲链表分桶管理，并在每个对象头尾紧贴着写入**内联边界标签**（Inline Boundary Tags）以便释放时合并相邻空闲块。放到现代多核服务器上，这套设计暴露出两个原生缺陷：一是 **`brk` 堆顶钉死效应**——只要堆顶附近还有哪怕一个 64 字节长生命周期对象未释放，其下方空出的 20 GB 碎片就无法收缩归还给内核；二是**内联元数据污染缓存行**，任意切分合并把同一循环连续分配的对象打散到不同的 4 KB 页上，拖垮 L1/L2 Cache 与 TLB。

Linux 默认的 `glibc malloc`（`ptmalloc2`）为了适配多核，在 `dlmalloc` 外套了一层 **Multi-Arena**（最多创建 `8 × CPU 核数` 个独立持锁堆区），并在 glibc 2.26 后补了每线程 `tcache`。但在高并发分析引擎里，这套补丁架构依然频频踩坑：

1. **跨线程释放与 Arena 锁竞争**：OLAP 引擎普遍存在生产者-消费者模型（I/O 线程分配 Buffer，Worker 线程消费后释放）。`ptmalloc2` 要求释放的内存必须归还给**当初分配它的 Arena**，迫使 Worker 线程跨核抢占另一个 Arena 的互斥锁；线程频繁创建销毁时，大量空洞滞留在互不相通的 Arena 中导致 RSS 虚高。
2. **高频 `mmap`/`munmap` 引发 TLB Shootdown 风暴**：超过 `M_MMAP_THRESHOLD`（动态调整，初始 128 KB）的分配直接走 `mmap`、释放立刻 `munmap`。在 64–128 核服务器上，每次 `munmap` 或 `madvise(MADV_DONTNEED)` 改页表，内核都必须通过**核间中断**（IPI）通知所有运行该进程的 CPU 核心刷新 TLB（**TLB Shootdown**），高并发下直接把多核打满在内核态软中断上。
3. **缺乏在线可观测性**：`ptmalloc2` 没有内置低开销堆采样机制，也无法精细控制脏页衰减周期，线上 RSS 缓慢爬升时难以定位。

### 1.2 现代三大分配器范式：`jemalloc`、`tcmalloc` 与 `mimalloc`

- **`jemalloc`：Size Class、两阶段衰减与可观测性之王**
  `jemalloc`（Jason Evans 为 FreeBSD 设计，后在 Meta 打磨）的核心哲学是“用可控的微小内部碎片，彻底消灭不可控的外部碎片与锁竞争”：
  - 将小对象严格对齐到几何级数 **Size Class**（内部碎片锁死在 ~12.5%–20%），同一 Slab 仅服务单一 Size Class，**元数据完全外置**在全局基数树 `emap` 中，既不污染用户缓存行又保证相邻对象高度紧凑。
  - 小对象分配/释放优先在每线程私有 `tcache` 无锁完成，仅在耗尽或溢满时批量与所属 Arena 交互。
  - **两阶段惰性回收抗 TLB Shootdown**：释放的整页先进入 `dirty` 态（页表映射仍在、TLB 保持热态，零缺页极速复用）；超过 `dirty_decay_ms` 后降级为 `muzzy` 态（`MADV_FREE`，内存紧张时内核可随时回收、不紧张不刷 TLB）；超过 `muzzy_decay_ms` 才真正 `MADV_DONTNEED` 归还物理页（进入 `retained`）。
  - 内置低开销采样剖析器（`prof:true` / `jeprof`），线上 24×7 数据库无需重启即可 dump 堆火焰图定位内存泄漏。

- **`tcmalloc`（现代版）：Linux `rseq` Per-CPU 缓存与大页感知（HPAA）**
  Google 的现代 `tcmalloc` 完成了两项针对超多核云服务器的里程碑重构：
  - **基于 Linux `rseq` 的 Per-CPU Cache**：当 64 核机器跑着 2,000 个线程时，传统 Per-Thread Cache 内存开销按 `O(N_threads)` 膨胀。`tcmalloc` 借助 Linux 4.18+ 的 **Restartable Sequences（`rseq`）** 将前端缓存锁定在 `O(N_cores)`，且快路径读写当前核心本地缓存**连一条原子 CAS（`lock cmpxchg`）都不需要**——若临界区中途被内核抢占或迁移，内核自动将指令指针回滚到 Abort Handler 重试。
  - **`HugePageAwareAllocator`（HPAA）**：相比盲开全局 `THP=always` 引发的碎片与容器 OOM，HPAA 后端通过 `huge_page_filler` 主动感知 2 MB 大页边界，把大量小对象紧密打包塞进同一大页，归还时优先拼装清空整张大页再还给内核——既吃满大页 TLB 红利，又避开了全局 THP 的内存膨胀。

- **`mimalloc`：Free List Sharding 与确定性心跳**
  Microsoft Research（APLAS 2019）推出的 `mimalloc` 代码精简（不足一万行 C）、元数据开销仅 ~0.2%，核心创新是 **Free List Sharding**：
  - 向系统申请按 **4 MB（或 32 MB）严格对齐**的 Segment，切分为 64 KB Page。拿到任意指针 `ptr` 只需一次位运算 `ptr & ~(SEGMENT_SIZE - 1)` 即可 `O(1)` 定位 Segment 元数据，无需查基数树。
  - 空闲链表下沉分片到**每个 64 KB Page 内部**，每页维护三条链：`free`（归属线程极速弹出）、`local_free`（本地释放暂存，不立即回填 `free`）与 `thread_free`（跨线程释放以无锁原子头插法挂入，杜绝跨核抢锁）。
  - **确定性心跳**：每当 `free` 耗尽，触发一次轻量维护心跳，整体接管 `local_free` 并用一次原子交换收编 `thread_free`。既让连续分配对象几乎全挤在同一 64 KB 页内拉满局部性，又确保极端跨线程释放下空页被及时回收。

| 维度 | `glibc ptmalloc2` | `jemalloc` | `tcmalloc`（现代版） | `mimalloc` |
| :--- | :--- | :--- | :--- | :--- |
| **前端无锁机制** | Multi-Arena + `tcache`（跨线程释放抢锁） | Per-Thread `tcache` + Multi-Arena | **Per-CPU Cache（基于 Linux `rseq`，无原子指令）** | 线程本地 Page `free` 链 + `thread_free` 原子链 |
| **元数据寻址** | 对象内联边界标签（污染缓存行） | 外置全局基数树 `emap` | 外置 PageMap 基数树 | **对齐 Segment 位掩码 `O(1)` 寻址**（~0.2% 开销） |
| **局部性与碎片控制** | 任意切分合并，碎片与 `brk` 堆顶钉死严重 | 固定 Size Class + Slab，抗长周期碎片极强 | Size Class + **HPAA 2MB 大页紧凑打包** | **Page 级 Free List Sharding**，极强空间局部性 |
| **物理页归还策略** | 阈值触发 `munmap` / `trim`，易发 TLB Shootdown | **两阶段衰减（`dirty` → `muzzy` → `retained`）** | 后台 Release 线程配合 HPAA 按大页精准归还 | 确定性心跳及时回收空 Page，支持延迟 purge |
| **运维与可观测性** | 弱（无内置在线 Profiling） | **极强**（内置低开销 `jeprof` 采样火焰图） | 强（Google 生态 Heap Profiler / 指标丰富） | 轻量简洁，但生产级在线采样工具链不如 `jemalloc` |

### 1.3 计算引擎的双层内存治理与生产调优 Checklist

一线 C++ 分析引擎（ClickHouse、Apache Doris、StarRocks、Meta Velox）不约而同采用**双层内存治理架构**：底层用 `jemalloc`（或 `tcmalloc`）兜底通用堆分配，上层坚决自建核心内存池与 Arena。原因在于通用分配器永远无法满足计算引擎的三个硬需求：**查询级精准记账与超限 Spill 仲裁（防止单条大 SQL 触发整机 OOM）**、**算子级批量生命周期（数百万聚合字符串 Key 用 Bump Allocator 顺序切分并在算子结束时 `O(1)` 整块丢弃）**，以及**显式 64B 对齐、`MADV_HUGEPAGE` 与 `mremap` 零拷贝扩容**。

| 计算引擎 | 底层默认通用分配器 | 上层自研内存治理与核心定制机制 | 选型背后的核心考量 |
| :--- | :--- | :--- | :--- |
| **ClickHouse** | **`jemalloc`**（静态链接，拦截 `operator new` 接入 `MemoryTracker`） | 核心列与 Hash Table 使用自研 **`Allocator<clear_memory>`**：大块内存（≥ 64 MB）绕过 `jemalloc` 直接走 `mmap` + **`mremap` 零拷贝扩容**；聚合状态与变长字符串全部写入按页递增的 **`Arena`** | 用 `jemalloc` 稳住通用小对象碎片与在线 Profiling；用 `mremap` 消除超大 Hash Table Rehash 时的双倍内存峰值与拷贝开销 |
| **Apache Doris** | **`jemalloc`**（支持编译切换 `tcmalloc`，生产默认 `jemalloc`） | 多层 **`MemTracker`** 结合 `jemalloc` 的 `stats.allocated` / `dirty` 指标与线程钩子双重校准 RSS，防容器 OOM；算子内部使用 `Arena` / `ObjectPool` | 早期从 `tcmalloc` 切向 `jemalloc` 的核心动因：长周期混合负载下更低的外部碎片、通过 `dirty_decay_ms` 可控释放 RSS，以及线上 `jeprof` 排查泄漏 |
| **StarRocks** | **`jemalloc`** | 通过 `jemalloc` + 全局 `operator new/delete` 钩子将所有分配纳入分层 **`MemTracker`**（Process → QueryPool → Query → Operator），配合 `MemPool` / `Chunk` 复用与 `percpu_arena` 调优 | 高并发 MPP 节点线程数多，开启 `percpu_arena` 压低跨核锁争用，并用较激进的 decay 参数防止容器 RSS 虚高 |
| **Meta Velox** | **`jemalloc`**（Meta 内部标配）或 **`MmapAllocator`** | 上层 **`MemoryPool` 树 + `MemoryArbitrator`**（支持跨查询动态抢占内存并回调算子触发 Spill）；底层支持 **`MmapAllocator`** 管理 4 KB–2 MB 分级页池，算子内专设 **`HashStringAllocator`** 与 **`StreamArena`** | 作为嵌入式执行底座（服务 Prestissimo、Spark Gluten 等），必须把内存上限、Spill 回调与大页分配完全控制在引擎内部 |

在工程选型与生产调优上，可直接对照四条准绳：
1. **通用 C++ 数据库与长生命周期后台服务首选 `jemalloc`**，生产环境推荐显式配置衰减与按核分片（切忌把 `dirty_decay_ms` 设为 `0`，否则高并发短查询会引发剧烈 TLB Shootdown 与缺页中断风暴，`2000–10000` ms 通常是吞吐与容器 RSS 水位的平衡点）：
   ```bash
   export MALLOC_CONF="percpu_arena:percpu,background_thread:true,dirty_decay_ms:5000,muzzy_decay_ms:5000,metadata_thp:auto"
   ```
2. **超多核万级并发、重度 TLB 敏感服务重点评估现代 `tcmalloc`**（Linux 4.18+ `rseq` + HPAA）。
3. **高频短命小对象、重度跨线程生产-消费释放首选 `mimalloc`**。
4. **热路径绝不直撞通用 `malloc`**：把聚合 Key 与中间列收口到查询级 `Arena` 与受控 `MemoryPool`。

---

## 二、数据面内存句柄与执行底座：`intrusive_ptr`、对齐池与两阶段多态

跨过分配器进入向量化引擎的数据面，C++ 基本功集中体现在三处高频工程设计：**侵入式智能指针句柄**、**生命周期对齐的内存池与伪共享防御**，以及**按 Batch 边界切开的“外虚内实”两阶段多态**。

![向量化引擎 C++ 侵入式内存底座与 CPU-GPU 三级异构流水线映射](/images/diagrams/db-cpp-intrusive-and-gpu-tiering.svg)

### 2.1 数据面为何弃用 `std::shared_ptr` 而选 `intrusive_ptr`？

列式引擎中，切片（Slice）、字典包装（Dictionary Wrap）和跨算子投影每时每刻都在共享同一块不可变字节缓冲区，引用计数是零拷贝视图（Zero-Copy View）的刚需。`std::shared_ptr<Buffer>` 与 `boost::intrusive_ptr<Buffer>` 的分水岭只有一句话：**引用计数器到底放在对象外面，还是嵌在对象身体里面。**

`std::shared_ptr<T>` 为了兼容无法修改源码的第三方类型，采用非侵入式设计：必须在对象外单独开辟控制块（存 `strong_count`、`weak_count`、虚表指针与删除器），导致每个句柄都是占 **16 字节的双指针胖结构（`{T* ptr, ControlBlock* ctrl}`）**。而 `intrusive_ptr<T>` 要求类 `T` 自身内嵌 `std::atomic<int32_t> referenceCount_`，外部无需控制块，句柄退化为与裸指针完全等价的 **8 字节单指针（`{T* ptr}`）**。

很多人会立刻追问：“C++11 不是有 `std::make_shared<T>()` 可以把控制块和对象合并进同一次分配吗？为什么 Velox 的 `BufferPtr` 和 ClickHouse 的 `ColumnPtr` 依然不用？”只要亲手写一次列存 `Buffer` 分配，就会撞上 `std::make_shared` 跨不过去的五道硬墙：

1. **无法支持“定长头部 + 变长连续数据区”的自定义内存池分配**：向量化引擎的 `64 KB` 列缓冲区通常采用“头部 `Buffer` 元数据 + 紧随其后的变长裸字节 Payload”紧凑布局——先向受配额管控的 `MemoryPool` 一次切出 `sizeof(Buffer) + 64KB` 对齐内存，再用 **Placement `new`** 在头部原地构造 `Buffer`。`std::make_shared<Buffer>()` 只能分配编译期固定大小 `sizeof(Buffer)`，根本捎带不了尾随数据区；而若先用 `MemoryPool` 拿到 `Buffer* raw` 再交给 `std::shared_ptr<Buffer>(raw, pool_deleter)`，**`shared_ptr` 构造函数会背着你在后台偷偷调全局 `operator new` 单独分配 24–32 字节控制块**，瞬间击穿“数据面严禁触碰全局堆”的铁律。反观 `intrusive_ptr`，计数器本就在 `Buffer` 头部结构体里，真正做到 **0 次额外堆分配**。
2. **缓存行密度翻倍与 ABI 传参优势**：200 列宽表传递 `std::vector<BufferPtr>` 时，8 字节 `intrusive_ptr` 让单条 `64B` Cache Line 装下 **8 个**列句柄，而 16 字节 `shared_ptr` 只能装 **4 个**，栈拷贝与寄存器传参开销同步减半。
3. **归零后挂回空闲链表原地复活（Object Pool Recycling）**：当临时 `Buffer` 计数归零时，在 `intrusive_ptr_release(Buffer* p)` 钩子里只需将 `p` 挂回 `MemoryPool` 空闲链，下次取出将体内 `referenceCount_` 复位为 `1` 即可满血复活；而 `std::shared_ptr` 的外部控制块一旦强引用归零便不可逆销毁，复用时不得不重新分配控制块。
4. **从裸指针 `this` 零成本安全恢复**：计数器跟着对象走，任何成员函数或 C 回调里拿到 `Buffer*` 均可随时构造 `intrusive_ptr<Buffer>(this)`；而 `std::shared_ptr` 直接包装裸指针会制造双控制块触发 **Double Free**，若继承 `std::enable_shared_from_this<T>` 又要在每个对象里白塞进 16 字节 `std::weak_ptr`。
5. **剔除无用的 `weak_count` 与定制原子内存序**：高频数据缓冲从不需要弱引用，自研侵入式计数只需唯一一个 `int32_t`，递增用最轻量的 `memory_order_relaxed`，递减用 `memory_order_acq_rel`，在明确由单 `PipelineDriver` 线程独占的局部结构上甚至可切换为非原子普通整数，彻底省去硬件 `lock xadd`。

当然，在低频创建的查询控制面（`PlanNode`、`QueryCtx` 以及需要 `std::weak_ptr` 打破父子循环引用的 `MemoryPool` 树节点）上，`std::shared_ptr` 仍是兼顾安全性与可读性的标准选择。**控制面重安全用 `shared_ptr`，数据面抠字节用 `intrusive_ptr` + 轻量 `span/string_view`**，构成了现代 C++ 引擎清晰的分界线。

### 2.2 生命周期对齐的内存池与“外虚内实”两阶段多态

除了内存句柄，向量化内核还依靠两项设计守住 L1i/L1d 缓存与多核扩展性：
- **生命周期对齐与伪共享防御**：大量短命中间字符串与哈希节点交由绑定批次/查询生命周期的线性 **`Arena`** 与页级 **Slab / Buddy Pool** 接管；多线程并发写入的局部统计结构严格标注 `alignas(64)`，防止相邻线程改写同一 `64B` Cache Line 引发 MESI 失效风暴。
- **“外层虚函数 + 内层模板特化”两阶段多态**：数据库算子既要动态组合任意执行计划树，又要处理十几种物理类型。若逐行调用虚函数 `Value eval(Row)`，间接跳转会吃光 CPU；若把整棵计划树全用 C++ 模板拼死，二进制膨胀会撑爆 `32 KB` 的一级指令缓存（`L1i`）。标准解法是按 **Batch 边界切开多态层次**：算子间调度保持 `Operator::getOutput()` 虚接口，每次吐出 `4,096` 行，虚调用开销被摊薄四千倍；进入算子内部后立刻通过一层 `TypeSwitch` 跳进具体类型的紧凑模板循环 `evalTyped<int64_t>(const int64_t* __restrict__ in, ...)`，消除分支与指针别名顾虑，让编译器顺利生成满血 AVX2 / AVX-512 / NEON 向量指令。（如果要进一步把这套列式内存底座映射到 GPU SIMT 合并访存、RMM 流序池与锁页 NUMA 传输，可继续阅读 [CPU/GPU 异构计算中的 SQL/ETL 加速陷阱、算子设计与调度抉择]({{< relref "2026-11-17-cpu-gpu-heterogeneous-sql-etl-survey.md" >}})。）

---

## 三、内核核心数据结构全景与缓存淘汰架构

有了可靠的分配器与零额外分配的句柄底座，我们就能沿着数据进入计算与存储内核的完整路径，逐一拆解资深 C++ 与数据库研发最常打磨的八类核心数据结构。

![数据库与系统内核中的八大核心数据结构协同执行流水线](/images/diagrams/sys-core-data-structures-pipeline.svg)

先看最贴近内存池边界的**缓存淘汰结构**。教科书里的标准答案 `std::unordered_map` + `std::list` 在高并发 Buffer Pool 或内存缓存中会同时撞上两堵墙：一是每次插入触发双倍堆节点分裂与指针追逐（单次 `get()` 串行 4–5 次 LLC Cache Miss），二是严格 LRU 要求每次命中都把节点挪到链表头，迫使 100% 纯读流量退化为抢占排他写锁、在多核上剧烈打废头节点所在的 `64B` Cache Line。生产级内核（RocksDB `HyperClockCache`、Meta CacheLib、Dragonfly）的物理布局手术——包括侵入式单块结构体合一、32 位下标压缩、SIMD SwissTable、2 MB 大页 Slab 与用原子 `ref_bit` 替代链表移动的无锁 Clock / SIEVE 淘汰——我们在专题 [生产级 C++ 缓存的侵入式布局与大模型 KV Cache 淘汰演进]({{< relref "2026-11-18-intrusive-cpp-cache-and-llm-kvcache-evolution.md" >}}) 中已有逐行拆解，此处聚焦**抗扫描污染策略**与**引擎级生命周期约束**：

| 算法 | 核心机制 | 工程优势 | 代价与局限 |
| :--- | :--- | :--- | :--- |
| **LRU-K（如 LRU-2）** | 记录倒数第 $K$ 次访问时间；未满 $K$ 次留在历史队列，满 $K$ 次才进主缓存 | 精准过滤单次顺序全表扫描 | 需额外维护 Ghost History Map，堆维护淘汰需 $O(\log N)$ |
| **2Q / InnoDB Midpoint** | 拆分为 Young 区（5/8）与 Old 区（3/8），新页插入交界处，驻留超冷却时间再访问才晋升 | 严格 $O(1)$，实现极简，有效防大表扫描 | 比例与冷却窗口依赖静态配置，无法感知长期频次分布 |
| **传统 LFU** | 按精确访问频次分桶淘汰（双层双向链表可做到 $O(1)$） | 稳态热点命中率极高 | 存在**历史包袱（Aging）**：旧热点即便彻底变冷也长期占坑 |
| **W-TinyLFU** | 1% Window LRU + Count-Min Sketch 准入过滤器 + 99% 分段 LRU（Probation 20% + Protected 80%） | 兼顾突发流量与长期频次，元数据极省 | 需要周期性衰减 Sketch，并维护动态调参逻辑 |

W-TinyLFU 的巧妙之处在于用 **Count-Min Sketch（CMS）** 作准入门卫：Window LRU 淘汰的候选者必须与主缓存 Probation 区的 Victim 比拼频次，仅当 $Freq(\text{Candidate}) > Freq(\text{Victim})$ 才准许替换。每个计数器仅占 **4 bits**（上限 15），单个 `uint64_t` 塞下 16 个计数器；每当采样数达到阈值 $W$，一条位运算 `word = (word >> 1) & 0x7777777777777777ULL` 即可让整字频率同时折半衰减（Aging），极低成本清退旧热点。

进入存储引擎 Buffer Pool 与 LLM 推理底座后，淘汰逻辑还必须服从两条硬约束：
1. **Pin Count 保护与异步刷盘解耦**：算子正在扫描的页维护 `pin_count > 0`，绝不可被驱逐；淘汰脏页前必须保证对应 WAL 先落盘（WAL-before-Data），且严禁持有缓存分片锁执行磁盘写入——应先将页标为 `IO_IN_PROGRESS` 并摘出候选链，释放锁后再异步刷盘。
2. **前缀树叶到根级联淘汰（RadixAttention / Prefix Cache）**：LLM 共享 System Prompt 或多轮对话形成前缀树，子节点 KV Block 强依赖父节点，节点可驱逐不变量收紧为 `ref_count == 0 && children.empty()`。工程上让侵入式 LRU 仅挂载当前可驱逐的叶子节点；每当叶子被 `Evict()` 摘除时顺带检查其父节点是否变为空壳，若是则立即推入 LRU 队列，保持严格 $O(1)$ 级联回收（`xlab` 仓库 `prefix_cache_lru.h` 实现了这一机制）。

---

## 四、并发队列：Ring Buffer、Vyukov MPMC 与任务调度

多线程通信最直观的写法是 `std::mutex` 套 `std::queue`，但在微秒级任务频繁流转的 Pipeline 执行引擎里，每次入队/出队都要抢锁、触发内存节点分配，并发稍高就会把大量 CPU 周期浪费在上下文切换上。无锁环形队列（Ring Buffer）用预分配连续数组替代动态链表，但在并发模型上，**SPSC（单生产单消费）与 MPMC（多生产多消费）有着本质的复杂度鸿沟**。

### 4.1 SPSC 的极简优化 vs Vyukov MPMC 的槽位序号同步

在 SPSC 队列里，`write_idx_` 只有生产者修改，`read_idx_` 只有消费者修改，完全不需要任何 CAS（Compare-And-Swap）原子交换指令：生产者只需 `load(read_idx_, acquire)` 判断未满，写入数据后 `store(write_idx_ + 1, release)`；在 x86 架构上，`acquire/release` 甚至不产生额外的内存栅栏指令开销。进一步的优化叫 **Shadow Index（本地索引缓存）**：生产者自己在本地存一份 `cached_read_idx_`，只要发现空间还够，连跨核读取 `read_idx_` 的动作都省掉，直到看似写满时才真正去同步一次最新的消费者进度。

但到了 MPMC 场景，只靠全局的 `head` 和 `tail` 两个原子索引就会直接崩溃。设想生产者 $P_1$ 和 $P_2$ 同时用 `fetch_add` 抢到了槽位 `0` 和 `1`，$P_2$ 很快写完了槽位 `1`，而 $P_1$ 恰好被操作系统切走还没写槽位 `0`。如果消费者只看 `tail` 已经推进到了 `2` 就去读槽位 `0`，读到的将是未初始化的脏内存。

工业界最经典的解法是 **Dmitry Vyukov 有界 MPMC 队列**（`xlab` 仓库的 `cc/cclab/src/common/queue/mpmc_queue.h` 即采用此设计）：让每个槽位 `cell_t` 自己携带一个单调递增的原子序号 `std::atomic<size_t> sequence_`，初始化时 `buffer_[i].sequence_ = i`。

```cpp
// 入队核心：先对比槽位 sequence_ 与当前 pos 的差值，再 CAS 抢占 enqueue_pos_
size_t pos = enqueue_pos_.load(std::memory_order_relaxed);
while (true) {
    cell_t* cell = &buffer_[pos & buffer_mask_];
    size_t seq = cell->sequence_.load(std::memory_order_acquire);
    intptr_t diff = static_cast<intptr_t>(seq) - static_cast<intptr_t>(pos);
    if (diff == 0) {
        if (enqueue_pos_.compare_exchange_weak(pos, pos + 1, std::memory_order_relaxed)) {
            break; // 抢槽成功
        }
    } else if (diff < 0) {
        return false; // 上一圈数据还没被消费完，队列已满
    } else {
        pos = enqueue_pos_.load(std::memory_order_relaxed); // 被其他生产者抢先，刷新进度
    }
}
cell->data_ = data;
cell->sequence_.store(pos + 1, std::memory_order_release); // 发布数据就绪
```

这套设计的精妙之处在于三点：**第一，消除了 ABA 问题**，`sequence_` 单调递增而不回绕模长；**第二，全局 `enqueue_pos_` 仅用作领号器**，CAS 可以用最轻量的 `memory_order_relaxed`，真正的 **Happens-Before 同步边**建立在单个槽位的 `cell->sequence_` 的 `release` 写与消费者的 `acquire` 读之间；**第三，出队完毕后将 `sequence_` 设为 `pos + buffer_mask_ + 1`（即 `pos + capacity`）**，刚好为下一圈同一物理槽位的写入解锁。

### 4.2 两级伪共享（False Sharing）与 `EventCount` 防丢唤醒

写无锁队列时最容易漏掉的是**两级伪共享**：
1. **控制头伪共享**：`enqueue_pos_`、`dequeue_pos_` 与只读的 `buffer_` 必须用 `alignas(64)`（或 `std::hardware_destructive_interference_size`）隔开，否则所有生产者和消费者会在同一条 64 字节缓存行上剧烈乒乓。
2. **数组相邻槽位伪共享**：如果 `sizeof(cell_t)` 只有 16 字节，那么逻辑上相邻的 `buffer_[0..3]` 会挤进同一条 Cache Line。4 个核心同时操作相邻槽位，依然会触发缓存行失效风暴。工程上要么直接把 `cell_t` 也做 `alignas(64)` 填充，要么采用 **Index Shuffling（缓存行步长重映射）**，让逻辑相邻的序号跨越到不同的物理缓存行中。

此外，无锁队列只解决非阻塞的 `try_enqueue` / `try_dequeue`，当队列为空时，消费者如果一直空转会烧光 CPU。为了避免退回重量级的 `std::mutex + condition_variable`，现代运行时普遍采用 **Two-Phase Wait + `EventCount`** 机制：消费者先短时间自旋吸收微秒级抖动；决定休眠前，先递增原子等待计数并施加 `seq_cst` 屏障，然后**必须再 Double-Check 一次 `queue.dequeue()`**，确认依然为空才调用 `futex_wait` / `std::atomic::wait` 进入内核休眠。生产者入队并 `release` 之后，同样先过屏障再检查等待计数，仅在有线程明确宣告休眠时才触发 `futex_wake`——既保证了正常吞吐路径零系统调用，又从数学上根除了唤醒信号在消费者入睡前擦肩而过的 **Lost Wakeup** 问题。

### 4.3 从全局 MPMC 到 Work-Stealing Deque 与 Pipeline 背压

即便 MPMC 队列做了全套内存序与缓存行对齐，在 32 核以上的任务调度器（如向量化执行引擎或异步运行时）中，所有 Worker 线程共同竞争同一个全局 MPMC 队列的 `enqueue_pos_` 依然会撞上硬件扩展性瓶颈。

因此，高性能调度器（如 Tokio、Go Runtime、`labs/runtime/pool/`）普遍采用“**每个 Worker 私有 Chase-Lev Work-Stealing Deque + 全局注入队列**”的混合架构：
- **本地队列分离两端热点**：Worker 自己从队列底部（`bottom_`）以 **LIFO** 方式压入和弹出子任务，99% 的情况无任何跨核竞争（只有当队列仅剩最后 1 个元素时，才需要一次 CAS 与窃取者决胜负）；而空闲的 Thief 线程从其他核队列的顶部（`top_`）以 **FIFO** 方式窃取。这同时兼顾了**本地数据 Cache 热度**（LIFO）与**偷取大粒度父任务以减少窃取频次**（FIFO）两个目标。
- **有界队列驱动 Pipeline 背压（Backpressure）**：在 Push-based 执行引擎里，上游算子向 Exchange Ring Buffer 写入数据批时如果队列已满，**绝不能在 Worker 线程里阻塞或自旋等待**（否则如果所有线程都被上游算子占满，下游消费算子将永远拿不到 CPU 而发生死锁）。上游算子必须将自身状态置为 `BLOCKED_ON_SINK` 并立即让出线程去调度下游，直到下游消费者消费掉一定水位线后，再将上游重新推回就绪队列。

---

## 五、排序与外部归并：PDQSort、Radix Sort 与败者树

如果问“现代列式数据库（DuckDB、Velox、ClickHouse）里的 `ORDER BY` 算子为什么比直接调用 `std::sort` 快出一个数量级”，答案往往藏在**分支预测**与**内存布局转换**里。

### 5.1 Introsort 的瓶颈与 PDQSort / Radix Sort 的突围

GCC 标准库的 `std::sort` 采用 Introsort（快排为主，递归深度超 $2\log_2 N$ 切堆排序兜底，小数组切插入排序）。它在工程中有两个显著短板：面对大量重复值（如低基数维度列）时二路分区依然产生过多无效交换；更重要的是，经典快排内层循环的 `if (a[i] < pivot)` 分支在随机数据下有一半概率预测失败，每一次失败都会清空现代 CPU 长达十余级的指令流水线。

- **PDQSort（Pattern-Defeating Quicksort）**：针对上述痛点引入了 **BlockQuicksort 无分支分区**——先用无条件算术逻辑批量比较 64 个元素并将需要交换的下标写进一个定长栈上偏移数组，随后再成对交换，彻底抹平了分支预测惩罚；同时在检测到连续相等 Pivot 时自动切换为三路分区，遇到全相等列直接线性时间收敛。
- **Radix Sort（基数排序）与保序编码（Order-Preserving Normalization）**：比较排序的理论下界是 $\Omega(N \log N)$，而按 8-bit 或 11-bit 分桶的 LSD/MSD Radix Sort 复杂度是 $O(W \cdot N)$。为了让任意类型都能走 Radix Sort，数据库内核会对 Key 做保序位变换：
  - 有符号整数 `int64_t` 将最高位（符号位）异或 `1`（`u ^ (1ULL << 63)`），负数即自然排在正数前；
  - IEEE-754 浮点数 `double` 若符号位为 `1`（负数）则按位全部取反，若符号位为 `0`（正数）则仅翻转最高位（并预先统一 `-0.0` 与 `NaN` 的位置），从而让任意浮点数可以直接按无符号整数甚至字节序直接比较；
  - 多列混合排序（如 `ORDER BY c1 ASC, c2 DESC NULLS LAST`）则根本不会拿行号数组 `perm[i]` 去各列里做随机 Gather 比较，而是先将排序键拼接编码为定长的 **Normalized Prefix Key**（带 `NULL` 标记位、大端序翻转整数以及字符串前 8~12 字节截断，降序字段整体按位取反），把复杂的多列比较直接降维成紧凑连续数组上的定长整数比较与 MSD 基数排序；仅当固定前缀完全相等时，才顺着尾随指针去比对完整字符串后缀。

在实现高基数 Radix Sort（例如 256 个桶同时散射写入）时，还有一个极易被忽视的硬件陷阱：**L1 dTLB 抖动**。256 个目标桶往往横跨 256 个不同的 4KB 内存页，远超 CPU L1 dTLB（通常仅 64–96 项）的容量。工程上通常在栈或连续内存里为每个桶配一个 **64 字节（1 条 Cache Line）的软件写合并缓冲区（Write-Combining Buffer）**，写满 64 字节再一次性整行刷入目标页，将 TLB Miss 压缩几十倍。

### 5.2 外部归并排序（External Merge Sort）的极致压榨

当数据量超出内存预算、必须溢写磁盘归并多路有序文件（Runs）时，工程重心转向减少比较次数和掩盖磁盘 I/O 延迟：

1. **为什么 $K$-路归并用败者树（Loser Tree）不用小顶堆？**  
   在含 $K$ 路输入的 `std::priority_queue` 中，每次弹出最小值再塞入新元素做下滤（Sift-Down），每往下一层都要先比较**左右两个子节点谁更小**，再拿胜出者跟父节点比，共需 $2 \log_2 K$ 次比较。而**败者树**把每一轮的败者留在内部节点，胜者一路向上只与父节点保存的败者单挑，精确只需 $\log_2 K$ 次比较——当排序键是较长的字符串或复合结构时，比较次数直接减半，且树节点数组大小固定、Cache 局部性极佳。
2. **扇入比 $K$ 的 I/O 权衡与 Block-Max 异步预读**：  
   给定可用缓冲内存 $M$，若同时归并 $K$ 路，每路分到的 Read Buffer 只有 $M/K$。扇入过大导致每次读盘粒度太小、随机 I/O 退化；扇入太小则增加总归并趟数 $\lceil \log_K(N/M) \rceil$ 放大写磁盘量。在 NVMe SSD 上工程经验通常取 $K \in [64, 256]$ 以保证单次异步预读块在 $256\text{KB} \sim 1\text{MB}$ 以上。更进一步，预读调度器不应平均轮询 $K$ 路，而是直接比较这 $K$ 路当前内存块末尾元素的最大值（**Run Max-Key Prediction**）——谁当前块的最大键最小，谁的缓冲区就一定会最先被耗尽，优先对其发起双缓冲异步 `io_uring` 预读。

---

## 六、二分查找与边界不变量：从无分支查找到谓词下推

二分查找看似基础，却在系统工程里贡献了最多的 Off-by-one Bug；而在硬件层面，教科书式二分又恰恰是对 CPU Cache 和分支预测最不友好的写法。

### 6.1 用“单调布尔谓词 + 半开区间 `[L, R)`”消灭边界 Bug

把二分查找从“找等于 `target` 的下标”抽象为**寻找单调布尔序列 `[False, False, ..., True, True]` 的第一个 `True` 分界点**：
- 维护左闭右开区间 `[left, right)`，初始为 `[0, n]`。
- **循环不变量**：任何时刻，所有 `i < left` 都严格满足 `P(a[i]) == false`，所有 `i >= right` 都严格满足 `P(a[i]) == true`。
- 只要 `left < right`，取 `mid = left + ((right - left) >> 1)`（严格保证 `left <= mid < right`）。若 `P(a[mid])` 为真，则令 `right = mid`；否则令 `left = mid + 1`。循环终止时 `left == right` 即为第一个 `True` 位置（`lower_bound` 对应 `a[mid] >= target`，`upper_bound` 对应 `a[mid] > target`）。

### 6.2 突破二分硬件瓶颈：Branchless、Eytzinger 布局与页内 SIMD

标准二分的前 $\log_2 N - 4$ 步跨度很大，几乎步步 L3 Cache Miss，且 `if (arr[mid] < target)` 有一半概率触发分支预测失败。工程上有三种逐层递进的硬件加速写法：

```cpp
// 无分支二分 + 双路硬件预取（编译为单条 cmov 指令）
const T* base = arr;
size_t len = n;
while (len > 1) {
    size_t half = len >> 1;
    __builtin_prefetch(base + (half >> 1), 0, 0);
    __builtin_prefetch(base + half + (half >> 1), 0, 0);
    base += (base[half - 1] < target) ? half : 0; // 条件传送，无分支跳转
    len -= half;
}
return (base - arr) + (*base < target);
```

如果数组是静态只读的索引，还可以将其重排为 **Eytzinger Layout（1-based 广度优先二叉搜索树数组）**：根节点放 `b[1]`，节点 `k` 的左右子节点分别在 `2*k` 与 `2*k + 1`。这样树的前 4 层（共 15 个节点）恰好全部挤在同一条 64 字节 Cache Line 里，而且下一步访问的左右孩子在内存中紧挨在一起，只需一句 `__builtin_prefetch(&b[k * 16])` 就能提前 4 层把整条子树的缓存行一次性拉进 L1 Cache，实测吞吐比 `std::lower_bound` 高 2 到 4 倍。而在 B+ 树节点或 Parquet 页索引等固定宽度块内，则常用 AVX2 指令一次加载 8 个分割键并配合 `_mm256_movemask_ps` 与 `__builtin_ctz` 在单周期内完成多路定位。

### 6.3 谓词下推（Predicate Pushdown）与索引裁剪中的工程陷阱

在存储引擎用二分查找定位文件块（ZoneMap / SSTable Index Block）时，有三个经典边界极易翻车：
1. **寻址区间块边界**：对于有序块索引，查询 `col >= L` 不能二分找 `first_key >= L` 的块（因为 `L` 可能落在前一个块的 `[first_key, last_key]` 中间），必须找**第一个满足 `last_key >= L` 的块**（或者等价地找第一个 `first_key > L` 的块再回退一格）。
2. **跨类型常量下推严禁直接强转**：例如列类型为 `int32_t`，查询条件是 `col <= 2.7` 或 `col >= 2.3`。不能把浮点数直接 `static_cast<int32_t>` 截断为 `2`：`col <= 2.7` 应向下取整为 `col <= 2`，而 `col >= 2.3` 必须向上取整为 `col >= 3`；若常量直接超出 `int32_t` 的表示极值，则应在计划期直接折叠为常量布尔值，避免整数溢出后做二分产生错漏。
3. **IEEE-754 全序破坏**：`NaN` 与任何值比较均返回 `false`，会直接破坏二分与排序依赖的严格弱序（Strict Weak Ordering）传递性；建索引与裁剪前必须显式规范化 `-0.0` 到 `+0.0`，并将 `NaN` 固定归位到序列末尾。

---

## 七、位图结构：Roaring Bitmap、向量化选择与 Rank/Select

位图是列式分析引擎、倒排索引和集合运算的骨架，核心命题是如何在**稀疏内存开销**与**极速批量位运算**之间取得平衡。

### 7.1 Roaring Bitmap 的二级分桶与自适应容器

传统纯 `Bitset` 哪怕只存一个大整数 `100,000,000` 也要占用 12MB；而早期的 WAH/EWAH 游程压缩位图虽然省空间，却丧失了 $O(1)$ 随机定位能力，集合求交必须从头流式解码。

**Roaring Bitmap** 将 32 位无符号整数拆为高 16 位和低 16 位：高 16 位作为一级有序桶索引，每个桶覆盖 $[0, 65535]$ 的范围，并根据桶内实际元素基数 $C$ 动态切换三种底层容器：
- **`ArrayContainer`（$C \le 4096$）**：有序 `uint16_t` 数组。4096 这个阈值并非拍脑袋——$4096 \times 2\text{B} = 8\text{KB}$，恰好与完整覆盖 65536 个比特位的位图体积完全相等。
- **`BitmapContainer`（$C > 4096$）**：定长 `uint64_t[1024]`（恒占 8KB），再稠密也不会继续膨胀。
- **`RunContainer`（连续区间压缩）**：用 `{ uint16_t start, uint16_t len }` 记录连续 1 区间，适合自增主键或局部聚类列。

做集合求交（`AND`）时，先在一级高 16 位索引上做双指针或跳跃（Galloping）对齐，不匹配的 65536 范围直接整块跳过；桶内再按容器类型走异构快速路径——`Bitmap & Bitmap` 直接跑 AVX2 按位与并用硬件 `popcnt` 实时统计新基数（低于 4096 则自动降级为 `ArrayContainer`），`Array & Bitmap` 则只需 $O(|Array|)$ 次查表，完全无需解压。

### 7.2 列存执行：Bitmap 转 Selection Vector 与 $O(1)$ Rank/Select

在向量化算子之间传递过滤结果时，多条件组合用 **Validity Bitmap**（每行 1 bit）最爽快，直接一条按位与算完 64 行；但当过滤后仅剩千分之几的存活行时，继续遍历全 `0` 字得不偿失，需要将其提取为紧凑行号数组 **Selection Vector**。

切忌逐个 bit 循环判断 `if (word & (1ULL << b))`。正确写法是利用硬件 BMI1 指令集的 `tzcnt`（计数尾随零）与 `blsr`（清除最低位 1），让循环次数严格等于 `1` 的个数：

```cpp
size_t out_idx = 0;
for (size_t w = 0; w < num_words; ++w) {
    uint64_t word = bitmap[w];
    uint32_t base = static_cast<uint32_t>(w * 64);
    while (word != 0) {
        uint32_t tz = static_cast<uint32_t>(__builtin_ctzll(word));
        sel_vec[out_idx++] = base + tz;
        word &= (word - 1); // 清除最低位的 1
    }
}
```

如果数据列中 `NULL` 值极多（例如 95% 为空），还可以引入紧凑数据结构（Succinct Data Structure）的 **$O(1)$ `Rank1(i)` 查询**：在位图之上按每 2048 bits 一级 Superblock（存全局前缀 Popcount）和每 512 bits 一级 Basic Block（恰好一条 64B Cache Line，存相对局部 Popcount，即 Poppy 两级索引）仅付出约 3%~6% 的额外空间代价，即可在常数时间内算出前 $i$ 行有几个非空值，从而直接凭 `CompactValues[Rank1(row_id)]` 压缩掉所有 `NULL` 占位内存。

---

## 八、概率数据结构：Blocked Bloom、Xor/Ribbon Filter 与 HyperLogLog

在海量数据过滤与统计中，允许极小的可控误差（如 1% 假阳性或 <1% 基数误差）往往能把内存和 I/O 开销压低一到两个数量级。

### 8.1 从标准 Bloom Filter 到 Blocked Bloom 与 Ribbon Filter

标准 Bloom Filter 在 1% 假阳性率下需要 $m/n \approx 9.6\text{ bits/key}$ 与 $k=7$ 个哈希位置。工业界早已用 **Kirsch-Mitzenmacher 双哈希定理**（$g_i(x) = h_1 + i \cdot h_2 \pmod m$）把哈希计算降到了只算 1 次 128-bit 哈希，但真正的瓶颈依然存在：**这 7 个 bit 随机散布在几百 MB 位图中，每次查询触发 7 次独立的 DRAM 随机访存（7 次 L3 Cache Miss）**。

- **Blocked Bloom Filter（Split-Block Bloom Filter）**：Parquet、DuckDB 与 Velox 的标配方案。把位图切分为 **32 字节**（256 bits，恰好一个 AVX2 寄存器）或 **64 字节**（一条 Cache Line）的小块。查询时先用高 32 位哈希确定落在哪一个 Block（**整次查询最多只读 1 次内存**），再用低 32 位哈希在 AVX2 寄存器内乘以 8 个奇数常数一次生成 8 路探测掩码，单条 `_mm256_testc_si256` 指令即可一周期判定是否命中，吞吐量暴涨 5~10 倍。
- **Xor Filter 与 RocksDB Ribbon Filter**：Bloom Filter 距离信息论理论下界 $\log_2(1/p)$ 还有 44% 的空间冗余。当集合构建后只读不改（例如 SSTable 落盘或 Hash Join Build 完成）时，利用超图剥离（Xor Filter）或高斯消元求解稀疏线性方程组（Ribbon Filter），只需约 **7.0~7.7 bits/key** 就能达到同样的 1% 假阳性率，直接省下近三成内存，且每次查询固定只有 3 次访存。

在向量化引擎的 **Hash Join Runtime Filter** 中，Build 端顺手生成的 Blocked Bloom Filter 会被下推给大表的 Scan 算子提前剪枝，但工程上必须配合两道**自适应熔断**：如果 Build 端表太大导致 Filter 体积撑爆 L3 Cache，直接放弃生成；如果 Scan 端运行几个 Morsel 后统计发现 Filter 拦截掉的行数不足 10%，立即熔断关闭该 Filter，避免为几乎全命中的表白交哈希计算税。

### 8.2 HyperLogLog（HLL）基数估算的核心直觉

HyperLogLog 解决“极低内存估算亿级不同元素个数（NDV）”的问题。它的数学直觉非常优雅：均匀哈希后的二进制串里，观测到“连续 $k$ 个前导 `0`”的概率是 $2^{-(k+1)}$，因此最大前导零长度能反推数据规模。

为了防止单次撞大运出现极值把结果拉偏，HLL 用哈希高 $p$ 位（如 $p=14$）将数据随机分入 $m = 2^{14} = 16384$ 个桶中，每个桶仅需 **6 bits** 记录该桶见过的最大前导零位数（总内存恒为 **12KB**，标准误差 $\approx 1.04/\sqrt{m} \approx 0.81\%$）。汇总计算时，HLL **绝对不能用算术平均数**——因为 $2^{M[j]}$ 是重尾分布，个别偏大的桶会彻底拉高均值；采用**调和平均数** $E = \alpha_m m^2 (\sum 2^{-M[j]})^{-1}$ 取倒数后能天然压制异常大值的权重。而当真实基数很小、大量桶仍为 `0`（空桶数 $V$ 较高）时，则需无缝切换至基于泊松分布的 **Linear Counting** 公式 $m \ln(m/V)$ 来修正小基数偏差。

---

## 九、存储引擎核心：B+ Tree 与 LSM Tree 的读写放大与并发演进

如果用 **RUM 猜想（Read / Update / Memory-Space Overhead 三者不可兼得）** 来统领存储引擎选型，B+ 树与 LSM 树恰好站在设计光谱的两端：

- **B+ 树的隐性写放大**：B+ 树面向**读优化**，点查稳定 $O(\log_B N)$。但它的原地更新（In-place Update）在随机写下极其沉重：哪怕只修改一行 100 字节的记录，也要把整张 16KB 数据页刷盘；为了防断电写半页损坏（Torn Page），还得先写 Redo Log 再写 16KB Doublewrite Buffer，实际随机写放大往往超过 300 倍，且页分裂使平均空间填充率停留在 $\ln 2 \approx 69\%$。
- **LSM 树的 Compaction 权衡**：LSM 树通过 `WAL + MemTable (SkipList)` 把所有随机写变成内存写与顺序追加，将代价后移给后台 Compaction。主流策略在读写空间放大之间做取舍：
  - **Leveled Compaction（RocksDB 默认）**：层内 SSTable 范围互不重叠，层间呈 $T=10$ 倍增长。点查每层至多看 1 个文件、空间放大极低（约 10%），但同一条数据在每层都要被反复重写归并，写放大较大（典型值 10~30 倍）。
  - **Size-Tiered / Universal Compaction**：同量级文件攒够就合并，每层只写 1 次，写放大极低（2~5 倍）；代价是同层文件范围大量重叠导致读放大飙升，且 Major Compaction 期间需要预留 **50%~100%** 的额外磁盘空间。

### 9.1 高并发 B+ 树的并发控制演进：从 Crabbing 到 OLC

传统悲观锁蟹行（Latch Crabbing）在向下遍历时必须同时锁住父子节点，甚至连读操作都要在根节点上对 `RWLatch` 做原子引用计数修改（`fetch_add/sub`），在 64 核 CPU 上会把根节点的 Cache Line 彻底打瘫。现代内核的突破分为两条路：
- **B-link Tree（PostgreSQL 采用）**：每个节点增加 `right_sibling` 指针与最大边界 `high_key`，把“节点分裂”与“向父节点插入分隔键”解耦。并发读线程顺着旧指针跌进刚被分裂的节点时，只要发现 `search_key > high_key`，直接顺着右兄弟指针横向平移即可，彻底无需同时锁住父子两层。
- **Optimistic Lock Coupling（OLC，LeanStore / Umbra 标配）**：每个节点仅维护一个 64-bit `version_lock`。读线程遍历时**完全不执行任何原子写内存操作**——只读取父节点版本号 $v_p$，查到子节点指针并读子节点版本号 $v_c$ 后，回头校验父节点版本号是否依然等于 $v_p$（未变则安全放行，变了则乐观重试）。读路径零共享内存写入，让多核读扩展性呈完美线性。

### 9.2 大 Value 下的 KV 分离（WiscKey / Integrated BlobDB）

当 Value 体积超过 1KB 时，让 LSM Compaction 反复搬运庞大的 Value 纯属带宽灾难。**WiscKey / RocksDB BlobDB** 的思路是**把 Key 和 Value 拆开**：大 Value 直接顺序追加写进独立的 Blob 文件，LSM 树内部只留 `Key -> {blob_file_id, offset, size}` 指针，把 LSM 体积缩减几十倍。随之而来的两个工程难点分别是：**Blob 垃圾回收（GC）**（RocksDB Integrated BlobDB 将 Blob 生命周期与 SSTable Level 绑定，在 Compaction 时按废弃率顺带流式重写，避免独立 GC 反向查询 LSM 的高昂开销）以及 **Range Scan 随机读退化**（借助 NVMe SSD 高队列深度，结合 `io_uring` 对后续一批相邻 Key 指向的离散 Blob Offset 发起批量异步并行预读）。

---

## 十、字符串匹配：SIMD 预过滤、Z-Algorithm 与扁平化 AC 自动机

面试算法题常考 KMP 的 `next` 数组，但在 ClickHouse、Velox 或高性能 `grep` 源码里，几乎见不到用标准 KMP 来跑 SQL `LIKE '%pattern%'`。

### 10.1 为什么工业界放弃纯 KMP，转向 SIMD Rare-Pair Filtering？

KMP 虽然拥有严格的 $O(N + M)$ 理论上界，但工程上必须**逐字节**检查主串，且内层失配回跳 `while (j > 0 && text[i] != pat[j]) j = next[j - 1];` 存在强数据依赖与频繁的条件跳转，彻底锁死了超标量 CPU 的指令级并行。

现代向量化引擎的标准范式是 **SIMD 稀有字符对预过滤**（如 `memchr::memmem` 与 Volnitsky 思想）：从模式串中挑选首尾字符（或英文语料中出现频率最低的两个字符及其相对间距 $\Delta$），将其广播填满两个 256-bit AVX2 寄存器。扫描主串时，每次步进 32 字节并行比较位置 `i` 和 `i + delta` 是否同时命中这两个字符：

```cpp
__m256i eq1 = _mm256_cmpeq_epi8(_mm256_loadu_si256((const __m256i*)(text + i)), v_first);
__m256i eq2 = _mm256_cmpeq_epi8(_mm256_loadu_si256((const __m256i*)(text + i + delta)), v_last);
uint32_t mask = _mm256_movemask_epi8(_mm256_and_si256(eq1, eq2));
```

99.9% 的情况下 `mask == 0`，主循环无任何内部分支直接 `i += 32` 飞跃前进；只有极少数情况 `mask != 0` 时，才用 `__builtin_ctz(mask)` 提取位点调一次 `memcmp` 确认中间字符，单核吞吐轻松达到十余 GB/s。

### 10.2 Z-Algorithm 的对称性与 AC 自动机工程重构

- **Z-Algorithm vs KMP**：KMP 的 $\pi[i]$ 定义为“以 $i$ **结尾**的最长匹配真前缀”；而 Z-Algorithm 的 $Z[i]$ 定义为“以 $i$ **开头**的后缀与原串的最长公共前缀（LCP）”。维护当前向右延伸最远的匹配窗口 $[L, R]$（Z-Box），当 $i \le R$ 时直接复用镜像 $Z[i - L]$ 跳过已知相等的字符，仅在超出 $R$ 时向右扩展，求字符串周期与所有后缀前缀关系时比 KMP 更直观。
- **Aho-Corasick（AC 自动机）的扁平化重构**：多模式匹配（如 `LIKE ANY` 关键词集合或敏感词过滤）如果按教科书写成 `struct Node { Node* next[256]; Node* fail; }`，单节点光指针就超过 2KB，直接撑爆缓存，且匹配时沿着 `fail` 链反复 `while` 回跳会产生极不稳定的 Pointer Chasing 延迟。工程化重构有两步：
  1. **补全为 DFA 状态转移表（Trie Graph）**：在 BFS 构建 `fail` 指针时，若当前节点缺失字符 `c` 的出边，直接将 `trie[u].next[c] = trie[trie[u].fail].next[c]` 固化进转移表。匹配主串的内层循环立刻退化为**零分支、零回跳的一行查表**：`state = dfa[state].next[byte]`。
  2. **字符集等价类压缩 + Double-Array Trie（双数组 DAT）**：先用 256 字节查找表把所有未在模式串中出现的无关字符全部折叠为同一个 `class 0`，将状态表列宽压缩数倍；再用紧凑的 `base[]` 与 `check[]` 两个一维 `int32_t` 数组替代二维稀疏矩阵，将整个自动机稳稳装进 L2/L3 Cache。

---

> **参考资料与源码基线**
> - **内存分配器**：Jason Evans, *A Scalable Concurrent malloc(3) Implementation for FreeBSD* (BSDCan 2006) & `jemalloc` 调优文档 · Google TCMalloc Design Docs（[Restartable Sequence `rseq` Per-CPU Caching](https://github.com/google/tcmalloc/blob/master/docs/rseq.md) & `HugePageAwareAllocator`）· Daan Leijen et al., *Mimalloc: Free List Sharding in Action* (APLAS 2019)。
> - **计算引擎内存治理与数据面句柄**：Meta Velox `velox/buffer/Buffer.h`（`boost::intrusive_ptr<Buffer>`）与 `velox/common/memory/`（`MemoryPool`、`MemoryArbitrator`、`MmapAllocator`、`HashStringAllocator`）· ClickHouse `src/Common/Allocator.h`、`Arena.h`、`COW<IColumn>::intrusive_ptr` · DuckDB `DataChunk` / `ArenaAllocator` · Apache Doris / StarRocks `MemTracker` 实践。
> - **内核数据结构参考原型（`xlab` 仓库）**：缓存管理（[lru.hpp](https://github.com/LiShuMing/xlab/blob/main/cc/cclab/src/utils/lru.hpp)、[prefix_cache_lru.h](https://github.com/LiShuMing/xlab/blob/main/cc/cclab/src/utils/prefix_cache_lru.h)）、无锁队列（[mpmc_queue.h](https://github.com/LiShuMing/xlab/blob/main/cc/cclab/src/common/queue/mpmc_queue.h)）与 Work-Stealing 线程池（[labs/runtime/pool/](https://github.com/LiShuMing/xlab/tree/main/labs/runtime/pool)）。
