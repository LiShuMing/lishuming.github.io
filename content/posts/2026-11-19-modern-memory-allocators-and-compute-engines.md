---
title: "现代 CPU 下的内存分配器演进：从 ptmalloc2 到 jemalloc/tcmalloc/mimalloc，以及计算引擎的选型逻辑"
slug: "modern-memory-allocators-and-compute-engines"
date: 2026-11-19T09:15:00+08:00
lastmod: 2026-11-19T09:15:00+08:00
categories:
  - 数据库
  - 系统架构
tags:
  - 内存分配器
  - jemalloc
  - tcmalloc
  - mimalloc
  - ClickHouse
  - Doris
  - Velox
  - 性能优化
description: "从多核锁竞争、缓存局部性、TLB Shootdown 与跨线程释放切入，梳理 glibc ptmalloc2 到 jemalloc、tcmalloc（rseq + HPAA）与 mimalloc（Free List Sharding）的架构演进，并剖析 ClickHouse、Apache Doris、StarRocks 与 Meta Velox 的真实内存治理逻辑。"
draft: false
toc: true
---

在一台 64 核服务器上运行高并发 C++ 分析引擎，经常会遇到两种让人抓狂的线上怪象：
第一种，并发度刚拉到 32 线程，CPU 利用率却上不去，`perf top` 里排在第一位的不是业务算子，而是 `glibc` 内部的 `__lll_lock_wait` 和 `_int_malloc`；
第二种，一波复杂 SQL 跑完、所有查询句柄都释放了，进程逻辑存活对象只剩 2 GB，但 `top` 里的物理 **RSS 却死死停在 40 GB 降不下来**，下一波查询一来直接触发容器 `OOMKilled`。

这两个问题的根源，都指向同一个被上层业务长期当成黑盒的基础设施——**用户态内存分配器（Memory Allocator）**。

如果把内存分配器看作“应用程序与操作系统虚拟内存之间的翻译官”，那么它的每一次架构换代（`dlmalloc` → `ptmalloc2` → `jemalloc` / `tcmalloc` → `mimalloc`），本质上都是在回答现代 CPU 硬件抛出的四个物理难题：**多核锁竞争、64B 缓存行与空间局部性、TLB 覆盖率与核间 Shootdown，以及跨线程释放导致的内存碎片膨胀**。

---

## 一、为什么传统系统分配器（`dlmalloc` / `ptmalloc2`）在现代 CPU 上会崩？

### 1.1 单核时代的遗产：`dlmalloc`、内联元数据与 `brk` 堆顶钉死

1990 年代的经典分配器 `dlmalloc`（Doug Lea's malloc）是为单核 CPU 设计的。它把向内核 `brk`/`sbrk` 申请来的连续虚拟地址空间切分成任意大小的 Chunk，用双向空闲链表（Free List）按大小分桶管理，并在每个对象头部和尾部紧贴着写入**内联边界标签（Inline Boundary Tags）**，方便释放时与前后相邻空闲块合并（Coalescing）。

在单核低并发时代，这套设计简洁高效，但在现代服务器软件里却暴露出两个原生缺陷：

1. **`brk` 堆顶钉死效应（Top-of-Heap Pinning）**：操作系统通过 `brk` 只能像栈一样从堆顶向下收缩归还物理内存。只要堆顶附近还有**哪怕一个 64 字节的长生命周期对象没有释放**，其下方空出来的 20 GB 碎片内存就彻底无法通过 `brk` 归还给内核，进程 RSS 居高不下。
2. **内联元数据污染缓存与空间局部性**：边界标签紧贴用户数据存放，既拉低了 64 字节 Cache Line 的有效数据密度，又让任意大小的切分与合并把同一循环里连续分配的对象打散到不同的 4 KB 页上，拖垮 L1/L2 Cache 和 TLB 命中率。

### 1.2 多核补丁的代价：`glibc ptmalloc2` 的三大硬伤

Linux 默认的 `glibc malloc`（基于 `ptmalloc2`）为了适配多核 CPU，在 `dlmalloc` 外面套了一层 **Multi-Arena**（最多创建 `8 × CPU 核数` 个独立堆区，各自持锁），并在 glibc 2.26 之后补上了每线程的 `tcache`（Thread Cache）。但这套“在单核底座上打多核补丁”的架构，在现代高并发数据库引擎里依然频频踩坑：

1. **跨线程释放与 Arena 锁竞争**：OLAP 引擎和网络服务普遍存在**生产者-消费者模型**（例如 I/O 线程分配 Buffer，Worker 线程消费完释放）。在 `ptmalloc2` 中，释放内存必须归还给**当初分配它的那个 Arena**，这意味着 Worker 线程必须去抢占另一个线程的 Arena 互斥锁；当线程频繁创建销毁时，大量空洞内存被滞留在几十个互不相通的 Arena 中，造成严重的 RSS 膨胀。
2. **高频 `mmap`/`munmap` 引发 TLB Shootdown 风暴**：对于超过 `M_MMAP_THRESHOLD`（动态调整，初始 128 KB）的大块分配，`ptmalloc2` 会绕过堆直接调用 `mmap` 向内核要页，释放时立刻 `munmap`。在 64 核甚至 128 核服务器上，每一次 `munmap` 或 `madvise(MADV_DONTNEED)` 修改页表，Linux 内核都必须通过 **核间中断（IPI）** 通知所有运行该进程线程的 CPU 核心刷新各自的 TLB（即 **TLB Shootdown**）。高并发下频繁触发 Shootdown 会把整个多核 CPU 打满在内核态软中断上。
3. **缺乏可观测性与不可控的碎片合并**：`ptmalloc2` 既没有内置低开销的在线堆采样（Heap Profiling）机制，也很难精细控制“脏页保留多久再还给内核”，线上出现 RSS 缓慢爬升时几乎只能抓瞎。

---

## 二、现代内存分配器的架构范式：`jemalloc`、`tcmalloc` 与 `mimalloc`

为了彻底解决多核竞争、局部性劣化与 TLB Shootdown，现代三大工业级分配器各自给出了极具代表性的架构答案。

### 2.1 `jemalloc`：Size Class、两阶段衰减与生产可观测性之王

`jemalloc`（由 Jason Evans 为 FreeBSD 设计，后在 Meta 大规模打磨）是当前开源数据库领域装机量最高的通用分配器，其核心哲学是**“用可控的微小内部碎片，彻底消灭不可控的外部碎片与锁竞争”**：

- **几何级数 Size Class + 独立 Slab/Extent 元数据**：
  - `jemalloc` 放弃了任意大小切分，而是将小对象（Small，如 8B、16B、32B、48B、64B… 直到 14 KB）严格对齐到几十个固定的 **Size Class**，将内部碎片上限死死锁在 ~12.5%–20% 以内。
  - 同一块 Slab（由连续页组成）只服务于同一种 Size Class，对象紧挨着排列，**元数据完全外置（Out-of-Band）**在全局基数树（`emap` / Extent Map）中，既不污染用户缓存行，又让相邻对象拥有极佳的空间局部性。
- **Per-Thread Cache（`tcache`）+ 多 Arena 隔离**：
  - 小对象分配与释放优先在当前线程私有的 `tcache` 里无锁完成；只有 `tcache` 耗尽或溢满时，才批量向所属 Arena 申请或刷回一个 Slab，把锁竞争稀释两个数量级。
- **两阶段惰性回收（`dirty_decay_ms` / `muzzy_decay_ms`）抗 TLB Shootdown**：
  - 这是 `jemalloc` 最精妙的工程设计之一。释放的整页不会立刻 `munmap` 触发 TLB Shootdown，而是先进入 **`dirty` 状态**（页表映射仍在、TLB 仍是热的，下次分配零缺页开销复用）；
  - 超过 `dirty_decay_ms` 后，后台线程将其降级为 **`muzzy` 状态**（调用 `MADV_FREE`，告诉内核“内存紧张时你随时拿走，不紧张就留着别刷 TLB”）；
  - 超过 `muzzy_decay_ms` 后，才真正执行 `MADV_DONTNEED` 归还物理页（进入 `retained`）。这套平滑衰减曲线让数据库工程师可以通过两个参数精准权衡“极速复用”与“及时降 RSS”。
- **内置采样剖析（`prof:true` / `jeprof`）**：
  - `jemalloc` 原生内置了极低开销的按字节采样剖析器，线上进程无需重启即可随时 dump 堆火焰图，定位哪条 SQL 或哪个模块泄露了内存——这是它在 24×7 数据库生产环境中不可替代的杀手锏。

### 2.2 `tcmalloc`：Linux `rseq` Per-CPU 缓存与大页感知（HPAA）

Google 的 **`tcmalloc`**（Thread-Caching Malloc）在过去几年完成了两次里程碑式的架构重构，专门针对超大规模多核云服务器的两个新痛点：

1. **从 Per-Thread 演进到基于 Linux `rseq` 的 Per-CPU 缓存**：
   - 当一台 64 核机器上跑着 2,000 个业务线程或协程调度线程时，传统的 Per-Thread Cache 会把缓存内存开销放大成 `O(N_threads)`，光线程缓存就吃掉好几个 GB。
   - 现代 `tcmalloc` 利用 Linux 4.18+ 引入的内核特性 **Restartable Sequences（`rseq`）**，将前端缓存改为 **Per-CPU Cache**——缓存占用严格锁定在 `O(N_cores)`！
   - 更惊人的是，借助 `rseq`，线程读写当前 CPU 核心的本地缓存**连一条原子 CAS 指令（`lock cmpxchg`）都不需要**。如果线程在执行临界区指令中途被内核抢占或迁移到别的核，内核会自动把指令指针回滚到预设的 Abort Handler 重试。在绝大多数不被抢占的快路径上，开销接近纯本地指令。
2. **`HugePageAwareAllocator`（HPAA）破解 TLB 难题**：
   - 我们在上一篇文章拆解过，盲目开全局 `THP=always` 会导致碎片膨胀与 OOM；但完全不用 2 MB 大页又会让大内存服务死在 TLB Miss 上。
   - 现代 `tcmalloc` 的后端直接内置了 **HugePageAwareAllocator**，通过 `huge_page_filler` 主动感知 2 MB 大页边界，把大量小对象紧密打包塞进同一个 2 MB 大页里，同时在归还内存时优先把零散小页拼装清空整张大页再归还——**既吃到了大页的 TLB 红利，又绕开了全局 THP 的内存膨胀陷阱**。

### 2.3 `mimalloc`：Free List Sharding 与确定性心跳

Microsoft Research（Daan Leijen et al., APLAS 2019）推出的 **`mimalloc`** 在近几年异军突起，以极精简的代码（不到 1 万行 C）、极低的元数据开销（~0.2%）和惊人的吞吐成为新一代标杆。它的核心创新是 **Free List Sharding（空闲链表分片）**：

- **对齐 Segment + O(1) 位掩码寻址**：
  - `mimalloc` 向操作系统申请按 **4 MB（或 32 MB）严格对齐**的 Segment，每个 Segment 切成若干 **64 KB 的 Page**，每个 Page 存放同一种 Size Class 的对象。
  - 任意拿到一个用户指针 `ptr`，只需做一次位运算 `ptr & ~(SEGMENT_SIZE - 1)` 就能瞬间定位到 Segment 头部元数据，**完全不需要像 `jemalloc`/`tcmalloc` 那样查基数树或哈希表**。
- **每页三条分片空闲链表（3-Tier Page-Local Free Lists）**：
  - 传统分配器按 Size Class 维护一条大空闲链表，久而久之链表里的空闲块指向分布在天南海北的物理页，空间局部性彻底崩坏。
  - `mimalloc` 把空闲链表下沉并分片到**每一个 64 KB Page 内部**，每个 Page 拥有三条链表：
    1. `free`：归属线程的极速分配链表，快路径只需一次指针弹出（几条汇编指令）。
    2. `local_free`：归属线程本地释放的对象先暂存在这里，不立刻塞回 `free`。
    3. `thread_free`：**其他线程跨线程释放（Cross-thread Free）** 时，用无锁原子头插法挂到这条链表上，彻底避免与归属线程抢锁。
- **确定性心跳（Deterministic Heartbeat）**：
  - 由于本地释放不直接回填 `free`，每隔几百次分配，`free` 链表必然会耗尽一次。每当 `free` 耗尽，分配器就触发一次轻量级维护心跳：把 `local_free` 整体接管过来，并用一次原子交换把 `thread_free` 整条收编。
  - 这带来了两个绝佳效果：第一，同一循环连续分配的对象几乎全部挤在**同一个 64 KB 页内**（1 个 TLB 项全覆盖，缓存局部性拉满）；第二，即使在极端的“线程 A 只管分配、线程 B 只管释放”场景下，跨线程释放的空页也能被心跳及时回收，彻底杜绝内存无限膨胀。

### 2.4 四大分配器核心架构对比

| 维度 | `glibc ptmalloc2` | `jemalloc` | `tcmalloc`（现代版） | `mimalloc` |
| :--- | :--- | :--- | :--- | :--- |
| **前端无锁机制** | Multi-Arena + `tcache`（跨线程释放需抢锁） | Per-Thread `tcache` + Multi-Arena | **Per-CPU Cache（基于 Linux `rseq`，无原子指令）** | 线程本地 Page `free` 链表 + `thread_free` 原子链 |
| **元数据寻址** | 对象内联边界标签（污染缓存行） | 外置全局基数树 `emap` | 外置 PageMap 基数树 | **对齐 Segment 位掩码 `O(1)` 寻址**（~0.2% 开销） |
| **局部性与碎片控制** | 任意切分合并，碎片与 `brk` 堆顶钉死严重 | 固定 Size Class + Slab，抗长周期碎片极强 | Size Class + **HPAA 2MB 大页紧凑打包** | **Page 级 Free List Sharding**，极强空间局部性 |
| **物理页归还策略** | 阈值触发 `munmap` / `trim`，易发 TLB Shootdown | **两阶段衰减（`dirty` → `muzzy` → `retained`）** | 后台 Release 线程配合 HPAA 按大页精准归还 | 确定性心跳及时回收空 Page，支持延迟 purge |
| **运维与可观测性** | 弱（无内置在线 Profiling） | **极强**（内置低开销 `jeprof` 采样火焰图） | 强（Google 生态 Heap Profiler / 指标丰富） | 轻量简洁，但生产级在线采样工具链不如 `jemalloc` |

---

## 三、计算引擎视角：ClickHouse、Doris、StarRocks 与 Velox 的真实选型逻辑

理解了通用分配器的演进，再看一线 C++ 分析型计算引擎（ClickHouse、Apache Doris、StarRocks、Meta Velox），你会发现它们不约而同地采用了一种**“双层内存治理架构”**：底层用 `jemalloc`（或 `tcmalloc`）兜底通用堆分配，上层却坚决自己接管核心算子的内存池与 Arena。

### 3.1 为什么计算引擎不能只靠“换个 `libjemalloc.so`”？

因为通用内存分配器面对的是“生命周期未知、大小任意”的普通对象，而数据库计算引擎有三个通用分配器永远给不了的硬需求：

1. **严格的查询级记账与 Spill 仲裁（Memory Accounting & Arbitration）**：
   多租户集群同时跑 50 条 SQL，如果 1 条失控的笛卡尔积把整机内存吃光触发 Linux OOM Killer，另外 49 条正常查询会跟着一起陪葬。引擎必须在每次大块分配前通过 **`MemoryTracker` / `MemoryArbitrator`** 精准扣减配额，超限时触发 **Spill-to-Disk（溢写磁盘）** 或精准中止单条大查询。
2. **算子级批量生命周期（Bulk Allocation & O(1) Free）**：
   一个 `GROUP BY` 聚合算子可能产生 500 万个变长字符串 Key。如果逐个调用 `malloc`/`free` 500 万次，光分配器开销和碎片就能吃掉 30% CPU；而这些对象的生命周期完全绑定在当前算子上——用 **Bump Allocator（Arena）** 顺序切分，查询结束直接 `O(1)` 整块丢弃，连析构都省了。
3. **显式的大页、对齐与零拷贝扩容控制**：
   32 GB 的 Hash Table 和 AVX-512 列向量需要 64 字节对齐与显式 `madvise(MADV_HUGEPAGE)` 控制，甚至希望在扩容时利用 Linux `mremap` 直接改页表映射而避免物理内存拷贝。

### 3.2 四大主流计算引擎的实战架构拆解

| 计算引擎 | 底层默认通用分配器 | 上层自研内存治理与核心定制机制 | 选型背后的核心考量 |
| :--- | :--- | :--- | :--- |
| **ClickHouse** | **`jemalloc`**（静态链接，拦截 `operator new` 接入 `MemoryTracker`） | 核心列与 Hash Table 使用自研 **`Allocator<clear_memory>`**：大块内存（≥ 64 MB）绕过 `jemalloc` 直接走 `mmap` + **`mremap` 零拷贝扩容**；聚合状态与变长字符串全部写入按页递增的 **`Arena`** | 用 `jemalloc` 稳住通用小对象碎片与在线 Profiling；用 `mremap` 消除超大 Hash Table Rehash 时的双倍内存峰值与拷贝开销 |
| **Apache Doris** | **`jemalloc`**（支持编译切换 `tcmalloc`，生产默认 `jemalloc`） | 构建了多层 **`MemTracker`** 体系，结合 `jemalloc` 的 `stats.allocated` / `dirty` 指标与线程局部钩子双重校准 RSS，防容器 OOM；算子内部使用 `Arena` / `ObjectPool` | 早期从 `tcmalloc` 切向 `jemalloc` 的核心动因：长周期混合负载下更低的外部碎片、通过 `dirty_decay_ms` 可控释放 RSS，以及线上 `jeprof` 精准排查内存泄漏 |
| **StarRocks** | **`jemalloc`** | 通过 `jemalloc` + 全局 `operator new/delete` 钩子将所有分配纳入分层 **`MemTracker`**（Process → QueryPool → Query → Operator），配合 `MemPool` / `Chunk` 复用与 `percpu_arena` 调优 | 高并发 MPP 节点线程数多，开启 `jemalloc` 的 `percpu_arena` 压低跨核锁争用，同时用较激进的 decay 参数防止容器环境下 RSS 虚高 |
| **Meta Velox** | **`jemalloc`**（Meta 内部标配）或 **`MmapAllocator`** | 极具代表性的模块化治理：上层 **`MemoryPool` 树 + `MemoryArbitrator`**（支持跨查询动态抢占内存并回调算子触发 Spill）；底层支持直接用 **`MmapAllocator`** 管理大块页池（按 4 KB–2 MB 分级），算子内专设 **`HashStringAllocator`** 与 **`StreamArena`** | 作为嵌入式执行底座（服务 Presto/Prestissimo、Spark Gluten 等），必须把内存上限、Spill 回调与大页分配完全控制在引擎自己手里 |

---

## 四、工程选型与生产调优 Checklist

如果团队正在从零搭建 C++ 计算引擎、存储引擎或高并发微服务，关于内存分配器的选型与调优可以直接遵循以下四条实战准绳：

1. **通用 C++ 数据库与长生命周期后台服务：首选 `jemalloc`**
   - 抗长周期外部碎片能力最稳健，且自带 `jeprof` 线上排查能力。
   - **生产必调参数（避免容器 OOM 与 TLB 抖动）**：
     ```bash
     # 启用按 CPU 绑定 Arena（减少高线程数下的缓存膨胀）+ 后台异步衰减线程 + 合理收敛 RSS + 仅对元数据开启 THP
     export MALLOC_CONF="percpu_arena:percpu,background_thread:true,dirty_decay_ms:5000,muzzy_decay_ms:5000,metadata_thp:auto"
     ```
     注意：不要轻易把 `dirty_decay_ms` 设为 `0`（零衰减会导致每次释放立刻 `MADV_DONTNEED`，在高并发短查询下引发剧烈的 TLB Shootdown 和缺页中断风暴）；通常设在 `2000`–`10000` ms 是吞吐与容器 RSS 水位的黄金平衡点。

2. **超多核、超大内存单机服务（如万级并发连接、重度 TLB 敏感）：重点评估现代 `tcmalloc`**
   - 在支持 Linux `rseq` 的内核（4.18+）上，其 Per-CPU Cache 内存开销极低且快路径无原子指令；配合内置的 **HPAA（HugePageAwareAllocator）**，非常适合不想开全局 `THP=always`、但又想吃满 2 MB 大页红利的大内存服务。

3. **高频短生命周期小对象、重度跨线程释放或新语言运行时：首选 `mimalloc`**
   - 如果业务特征是“大量小对象在生产者-消费者线程间流转”，`mimalloc` 的 Page 级 Free List Sharding 与确定性心跳往往能跑出比 `jemalloc` 更高的微基准吞吐和更低的尾延迟。

4. **计算引擎核心热路径：一定要建自己的 `Arena` + `MemoryPool`**
   - 永远不要让 `HashTable` 里的几百万个字符串 Key 或每行表达式的中间对象直接去撞通用 `malloc`。把热路径收口到**查询级 `Arena`（Bump Allocation + `O(1)` 整块释放）**与**显式 `MemoryPool`（精准限额 + Spill 触发 + 大块 `MADV_HUGEPAGE`）**，才是现代分析引擎真正跑赢内存墙的终极答案。

---

> **参考资料与技术基线**
> - **jemalloc**: Jason Evans, *A Scalable Concurrent malloc(3) Implementation for FreeBSD* (BSDCan 2006) · `jemalloc` 官方调优文档（`dirty_decay_ms` / `muzzy_decay_ms` / `percpu_arena` / `metadata_thp`）。
> - **tcmalloc**: Google TCMalloc Design Docs — [Restartable Sequence (`rseq`) Per-CPU Caching](https://github.com/google/tcmalloc/blob/master/docs/rseq.md) & `HugePageAwareAllocator` · Hunter et al., *Characterizing a Memory Allocator at Warehouse Scale* (USENIX)。
> - **mimalloc**: Daan Leijen, Benjamin Zorn, Leonardo de Moura, *Mimalloc: Free List Sharding in Action* (APLAS 2019 / Microsoft Research Technical Report)。
> - **计算引擎实现**：ClickHouse `src/Common/Allocator.h`、`Arena.h` 与 `MemoryTracker` · Apache Doris / StarRocks `MemTracker` 与 `jemalloc` 生产实践 · Meta Velox `velox/common/memory/`（`MemoryPool`、`MemoryArbitrator`、`MmapAllocator`、`HashStringAllocator`）。
