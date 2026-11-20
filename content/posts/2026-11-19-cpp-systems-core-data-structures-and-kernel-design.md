---
title: "资深 C++ 与系统研发核心数据结构：从硬件约束到数据库内核落地"
slug: "cpp-systems-core-data-structures-and-kernel-design"
date: 2026-11-19T09:42:00+08:00
lastmod: 2026-11-19T09:42:00+08:00
categories:
  - 算法与数据结构
  - 系统工程
  - 数据库
tags:
  - C++
  - 面试总结
  - Buffer Pool
  - 无锁队列
  - RadixSort
  - RoaringBitmap
  - BloomFilter
  - LSM-Tree
description: "围绕资深 C++ 与数据库内核研发高频考察的八大核心数据结构（缓存淘汰、无锁并发队列、排序与外排、二分与边界不变量、位图索引、概率数据结构、B+ 树与 LSM 树、字符串匹配），从 CPU Cache Line、分支预测、内存序与 I/O 放大等第一性硬件约束出发，梳理其工业界演进路径与工程落地细节。"
draft: false
toc: true
math: true
---

在编写业务代码时，`std::unordered_map`、`std::list`、`std::sort` 和 `std::mutex` 足以解决绝大多数问题；但只要把场景切换到高并发存储引擎的 Buffer Pool、向量化执行引擎的 Pipeline 调度器或百 GB 级数据的外部排序，这些教科书里的标准组件往往会在第一时间成为性能瓶颈。

问题的根源很少出在渐近时间复杂度 $O(N \log N)$ 或 $O(1)$ 上，而是出在现代硬件的三个物理约束：**跨核缓存一致性通信（Cache Coherency）与 L3 Cache Miss**、**不可预测条件分支引发的流水线冲刷（Branch Misprediction）**，以及**随机小块写带来的存储放大**。如果我们从这三把物理标尺出发，重新审视系统与数据库内核中最常被打磨的八类数据结构，就会发现它们的演进路径其实是高度一致的。

![数据库与系统内核中的八大核心数据结构协同执行流水线](/images/diagrams/sys-core-data-structures-pipeline.svg)

---

## 一、 缓存淘汰结构：LRU、LFU、TinyLFU 与 Buffer Pool 落地

教科书里的 LRU 标准答案是 `std::unordered_map` 加 `std::list`：哈希表存 Key 到链表迭代器的映射，链表维护访问顺序。这套写法在单线程算法题里无懈可击，但放进高并发 C++ 服务或数据库 Buffer Pool 时，会同时撞上两堵墙：

第一堵墙是**内存碎片与指针追逐**。每次插入新元素，`std::unordered_map`（链地址法）和 `std::list` 会各自独立向堆分配器申请节点，一个 16 字节的 Key-Value 往往伴随近百字节的元数据与分配器头部开销。更致命的是，一次 `get()` 命中需要先查桶数组、再追哈希节点指针、再跳去链表节点修改前后指针，串行触发 4 到 5 次 LLC Cache Miss。

第二堵墙是**读操作退化为互斥写锁**。普通查找本来是只读的，但因为 LRU 规定“每次命中都必须把节点挪到链表头”，导致哪怕 100% 纯读流量也必须抢占排他写锁。多核 CPU 不仅要在同一把 Mutex 上排队，还会把链表头节点所在的 64 字节 Cache Line 来回打成 `Invalid` 状态。

![教科书式标准库 LRU 与生产级 C++ 侵入式无锁缓存的物理布局对比](/images/diagrams/sys-cpp-intrusive-clock-cache.svg)

### 1. 消除堆分配与减轻读路径锁竞争

生产级引擎（如 RocksDB BlockCache、Meta CacheLib、Dragonfly）的改造思路很直接：既然痛点在堆节点分裂和链表指针挪动，那就把它们全部拆掉。

- **侵入式链表（Intrusive List）与开放寻址槽位**：将 `prev_idx` / `next_idx`（用 4 字节数组下标替代 8 字节裸指针）直接内嵌在预分配连续数组的 `PageFrame` 描述符中，配合 `absl::flat_hash_map` 或 SIMD 探测的开放寻址哈希表存储 `Key -> slot_id`，可以把一次命中的内存跳转压到极低，且链表的移动不再产生任何 `malloc/free`。
- **分片与延迟批量更新**：按 Key 哈希高位切分为 $64$ 或 $128$ 个独立 Shard，把锁粒度拆细；或者像 Caffeine 那样，读线程命中时不加锁改链表，而是将命中的节点指针写入线程本地或无锁环形 Read Buffer，由后台或淘汰线程攒批重排。
- **用原子标记位替代链表移动（Clock / HyperClock / SIEVE）**：更彻底的做法是直接放弃精确 LRU 链表。在环形数组里为每个槽位保留一个 `std::atomic<uint8_t> ref_bit`，读命中时先读判断——如果已经是 `1` 就什么都不写，只有为 `0` 时才置 `1`。这样读路径完全无锁且不污染 Cache Line，仅在容量满需要驱逐时，由时钟指针顺时针扫过并清零 `ref_bit`。

### 2. 抗扫描污染：从 LRU-K、2Q 到 W-TinyLFU

解决了并发吞吐，下一个问题来自工作负载本身。传统 LRU 只看最近一次访问时间（Recency），不看历史频次（Frequency）。在数据库里跑一次全表扫描（`SELECT COUNT(*) FROM huge_table`），海量只读一次的冷数据页会瞬间把高频热点索引页全部挤出内存，这就是**缓存污染（Cache Pollution）**。

为了过滤“只路过一次”的扫描流量，工业界演化出三条主要路线：

| 算法 | 核心机制 | 工程优势 | 代价与局限 |
| :--- | :--- | :--- | :--- |
| **LRU-K（如 LRU-2）** | 记录倒数第 $K$ 次访问时间；未满 $K$ 次留在历史队列，满 $K$ 次才进主缓存 | 精准过滤单次顺序扫描 | 需额外维护 Ghost History Map，堆维护淘汰需 $O(\log N)$ |
| **2Q / InnoDB Midpoint** | 拆分为 Young 区（5/8）与 Old 区（3/8），新页插入交界处，驻留超冷却时间再访问才晋升 | 严格 $O(1)$，实现极简，有效防大表扫描 | 比例与冷却窗口依赖静态配置，无法感知长期频次分布 |
| **传统 LFU** | 按精确访问频次分桶淘汰（双层双向链表可做到 $O(1)$） | 稳态热点命中率极高 | 存在**历史包袱（Aging）**：过去爆火的旧热点即便彻底变冷也长期占坑 |
| **W-TinyLFU** | 1% Window LRU + Count-Min Sketch 准入过滤器 + 99% 分段 LRU（Probation 20% + Protected 80%） | 兼顾突发流量与长期频次，元数据极省 | 需要周期性衰减 Sketch，并维护动态调参逻辑 |

W-TinyLFU 之所以成为现代内存缓存的标杆，在于它巧妙利用了 **Count-Min Sketch（CMS）** 作为准入门卫：Window LRU 淘汰出来的候选者想进入主缓存时，必须先和主缓存 Probation 区即将被逐出的 Victim 在 CMS 里比拼频次，只有 $Freq(\text{Candidate}) > Freq(\text{Victim})$ 才允许替换。并且 CMS 不需要存几百万的精确计数，每个计数器只占 **4 bits**（上限 15），单个 `uint64_t` 可以塞下 16 个计数器；每当采样次数达到阈值 $W$（通常为缓存容量的 10 倍），用一次位运算 `word = (word >> 1) & 0x7777777777777777ULL` 就能让整条字的频率同时折半衰减（Aging），低成本地清退历史旧热点。

### 3. Buffer Pool 与大模型前缀缓存中的特殊约束

把缓存放进存储引擎或 LLM 推理引擎时，淘汰逻辑还必须服从物理生命周期约束：

1. **Pin Count（引用保护）与异步刷盘解耦**：数据库算子正在扫描某个内存页时，绝不允许后台淘汰线程把它覆写。每个页框必须维护 `pin_count`，只有 `pin_count == 0` 的页才有资格进入可淘汰集合。此外，淘汰脏页（Dirty Page）前必须保证对应的 WAL 日志已经落盘（WAL-before-Data），且**严禁在持有 Buffer Pool 全局或分片锁的期间执行磁盘 `fsync`/写入**——正确做法是先把页状态标记为 `IO_IN_PROGRESS` 并摘出候选链，释放锁后再异步刷盘。
2. **受限拓扑下的叶到根淘汰（RadixAttention / Prefix Cache）**：在 LLM KV Cache 复用场景中，多轮对话或共享 System Prompt 组织成一棵前缀树，子节点的 KV Block 在语义和物理上强依赖父节点。此时**绝对不能只因为父节点久未直接访问就把它删掉**，节点可被驱逐的不变量收紧为 `ref_count == 0 && children.empty()`。工程上让侵入式 LRU 链表只挂载当前可驱逐的叶子节点；每当一个叶子被 `Evict()` 摘除时，顺便检查其父节点是否也变成了无锁且无子的空壳，若是则立即把父节点推入 LRU 队列，从而保持严格的 $O(1)$ 级联回收（`xlab` 仓库的 `cc/cclab/src/utils/prefix_cache_lru.h` 实现了这一机制）。

---

## 二、 并发队列：Ring Buffer、MPMC 与任务调度

多线程通信最直观的写法是 `std::mutex` 套 `std::queue`，但在微秒级任务频繁流转的 Pipeline 执行引擎里，每次入队/出队都要抢锁、触发内存节点分配，并发稍高就会把大量 CPU 周期浪费在上下文切换上。无锁环形队列（Ring Buffer）用预分配连续数组替代动态链表，但在并发模型上，**SPSC（单生产单消费）与 MPMC（多生产多消费）有着本质的复杂度鸿沟**。

### 1. SPSC 的极简优化 vs Vyukov MPMC 的槽位序号同步

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

### 2. 两级伪共享（False Sharing）与 `EventCount` 防丢唤醒

写无锁队列时最容易漏掉的是**两级伪共享**：
1. **控制头伪共享**：`enqueue_pos_`、`dequeue_pos_` 与只读的 `buffer_` 必须用 `alignas(64)`（或 `std::hardware_destructive_interference_size`）隔开，否则所有生产者和消费者会在同一条 64 字节缓存行上剧烈乒乓。
2. **数组相邻槽位伪共享**：如果 `sizeof(cell_t)` 只有 16 字节，那么逻辑上相邻的 `buffer_[0..3]` 会挤进同一条 Cache Line。4 个核心同时操作相邻槽位，依然会触发缓存行失效风暴！工程上要么直接把 `cell_t` 也做 `alignas(64)` 填充，要么采用 **Index Shuffling（缓存行步长重映射）**，让逻辑相邻的序号跨越到不同的物理缓存行中。

此外，无锁队列只解决非阻塞的 `try_enqueue` / `try_dequeue`，当队列为空时，消费者如果一直空转会烧光 CPU。为了避免退回重量级的 `std::mutex + condition_variable`，现代运行时普遍采用 **Two-Phase Wait + `EventCount`** 机制：消费者先短时间自旋吸收微秒级抖动；决定休眠前，先递增原子等待计数并施加 `seq_cst` 屏障，然后**必须再 Double-Check 一次 `queue.dequeue()`**，确认依然为空才调用 `futex_wait` / `std::atomic::wait` 进入内核休眠。生产者入队并 `release` 之后，同样先过屏障再检查等待计数，仅在有线程明确宣告休眠时才触发 `futex_wake`——既保证了正常吞吐路径零系统调用，又从数学上根除了唤醒信号在消费者入睡前擦肩而过的 **Lost Wakeup** 问题。

### 3. 从全局 MPMC 到 Work-Stealing Deque 与 Pipeline 背压

即便 MPMC 队列做了全套内存序与缓存行对齐，在 32 核以上的任务调度器（如向量化执行引擎或异步运行时）中，所有 Worker 线程共同竞争同一个全局 MPMC 队列的 `enqueue_pos_` 依然会撞上硬件扩展性瓶颈。

因此，高性能调度器（如 Tokio、Go Runtime、`labs/runtime/pool/`）普遍采用 **“每个 Worker 私有 Chase-Lev Work-Stealing Deque + 全局注入队列”** 的混合架构：
- **本地队列分离两端热点**：Worker 自己从队列底部（`bottom_`）以 **LIFO** 方式压入和弹出子任务，99% 的情况无任何跨核竞争（只有当队列仅剩最后 1 个元素时，才需要一次 CAS 与窃取者决胜负）；而空闲的 Thief 线程从其他核队列的顶部（`top_`）以 **FIFO** 方式窃取。这同时兼顾了**本地数据 Cache 热度（LIFO）**与**偷取大粒度父任务以减少窃取频次（FIFO）**两个目标。
- **有界队列驱动 Pipeline 背压（Backpressure）**：在 Push-based 执行引擎里，上游算子向 Exchange Ring Buffer 写入数据批时如果队列已满，**绝不能在 Worker 线程里阻塞或自旋等待**（否则如果所有线程都被上游算子占满，下游消费算子将永远拿不到 CPU 而发生死锁）。上游算子必须将自身状态置为 `BLOCKED_ON_SINK` 并立即让出线程去调度下游，直到下游消费者消费掉一定水位线后，再将上游重新推回就绪队列。

---

## 三、 排序与外部归并：PDQSort、Radix Sort 与败者树

如果问“现代列式数据库（DuckDB、Velox、ClickHouse）里的 `ORDER BY` 算子为什么比直接调用 `std::sort` 快出一个数量级”，答案往往藏在**分支预测**与**内存布局转换**里。

### 1. Introsort 的瓶颈与 PDQSort / Radix Sort 的突围

GCC 标准库的 `std::sort` 采用 Introsort（快排为主，递归深度超 $2\log_2 N$ 切堆排序兜底，小数组切插入排序）。它在工程中有两个显著短板：面对大量重复值（如低基数维度列）时二路分区依然产生过多无效交换；更重要的是，经典快排内层循环的 `if (a[i] < pivot)` 分支在随机数据下有一半概率预测失败，每一次失败都会清空现代 CPU 长达十余级的指令流水线。

- **PDQSort（Pattern-Defeating Quicksort）**：针对上述痛点引入了 **BlockQuicksort 无分支分区**——先用无条件算术逻辑批量比较 64 个元素并将需要交换的下标写进一个定长栈上偏移数组，随后再成对交换，彻底抹平了分支预测惩罚；同时在检测到连续相等 Pivot 时自动切换为三路分区，遇到全相等列直接线性时间收敛。
- **Radix Sort（基数排序）与保序编码（Order-Preserving Normalization）**：比较排序的理论下界是 $\Omega(N \log N)$，而按 8-bit 或 11-bit 分桶的 LSD/MSD Radix Sort 复杂度是 $O(W \cdot N)$。为了让任意类型都能走 Radix Sort，数据库内核会对 Key 做保序位变换：
  - 有符号整数 `int64_t` 将最高位（符号位）异或 `1`（`u ^ (1ULL << 63)`），负数即自然排在正数前；
  - IEEE-754 浮点数 `double` 若符号位为 `1`（负数）则按位全部取反，若符号位为 `0`（正数）则仅翻转最高位（并预先统一 `-0.0` 与 `NaN` 的位置），从而让任意浮点数可以直接按无符号整数甚至字节序直接比较！
  - 多列混合排序（如 `ORDER BY c1 ASC, c2 DESC NULLS LAST`）则根本不会拿行号数组 `perm[i]` 去各列里做随机 Gather 比较，而是先将排序键拼接编码为定长的 **Normalized Prefix Key**（带 `NULL` 标记位、大端序翻转整数以及字符串前 8~12 字节截断，降序字段整体按位取反），把复杂的多列比较直接降维成紧凑连续数组上的定长整数比较与 MSD 基数排序；仅当固定前缀完全相等时，才顺着尾随指针去比对完整字符串后缀。

在实现高基数 Radix Sort（例如 256 个桶同时散射写入）时，还有一个极易被忽视的硬件陷阱：**L1 dTLB 抖动**。256 个目标桶往往横跨 256 个不同的 4KB 内存页，远超 CPU L1 dTLB（通常仅 64 项）的容量。工程上通常在栈或连续内存里为每个桶配一个 **64 字节（1 条 Cache Line）的软件写合并缓冲区（Write-Combining Buffer）**，写满 64 字节再一次性整行刷入目标页，将 TLB Miss 压缩几十倍。

### 2. 外部归并排序（External Merge Sort）的极致压榨

当数据量超出内存预算、必须溢写磁盘归并多路有序文件（Runs）时，工程重心转向减少比较次数和掩盖磁盘 I/O 延迟：

1. **为什么 $K$-路归并用败者树（Loser Tree）不用小顶堆？**  
   在含 $K$ 路输入的 `std::priority_queue` 中，每次弹出最小值再塞入新元素做下滤（Sift-Down），每往下一层都要先比较**左右两个子节点谁更小**，再拿胜出者跟父节点比，共需 $2 \log_2 K$ 次比较。而**败者树**把每一轮的败者留在内部节点，胜者一路向上只与父节点保存的败者单挑，精确只需 $\log_2 K$ 次比较——当排序键是较长的字符串或复合结构时，比较次数直接减半，且树节点数组大小固定、Cache 局部性极佳。
2. **扇入比 $K$ 的 I/O 权衡与 Block-Max 异步预读**：  
   给定可用缓冲内存 $M$，若同时归并 $K$ 路，每路分到的 Read Buffer 只有 $M/K$。扇入过大导致每次读盘粒度太小、随机 I/O 退化；扇入太小则增加总归并趟数 $\lceil \log_K(N/M) \rceil$ 放大写磁盘量。在 NVMe SSD 上工程经验通常取 $K \in [64, 256]$ 以保证单次异步预读块在 $256\text{KB} \sim 1\text{MB}$ 以上。更进一步，预读调度器不应平均轮询 $K$ 路，而是直接比较这 $K$ 路当前内存块末尾元素的最大值（**Run Max-Key Prediction**）——谁当前块的最大键最小，谁的缓冲区就一定会最先被耗尽，优先对其发起双缓冲异步 `io_uring` 预读。

---

## 四、 二分查找与边界不变量：从无分支查找到谓词下推

二分查找看似基础，却在系统工程里贡献了最多的 Off-by-one Bug；而在硬件层面，教科书式二分又恰恰是对 CPU Cache 和分支预测最不友好的写法。

### 1. 用“单调布尔谓词 + 半开区间 `[L, R)`”消灭边界 Bug

把二分查找从“找等于 `target` 的下标”抽象为**寻找单调布尔序列 `[False, False, ..., True, True]` 的第一个 `True` 分界点**：
- 维护左闭右开区间 `[left, right)`，初始为 `[0, n]`。
- **循环不变量**：任何时刻，所有 `i < left` 都严格满足 `P(a[i]) == false`，所有 `i >= right` 都严格满足 `P(a[i]) == true`。
- 只要 `left < right`，取 `mid = left + ((right - left) >> 1)`（严格保证 `left <= mid < right`）。若 `P(a[mid])` 为真，则令 `right = mid`；否则令 `left = mid + 1`。循环终止时 `left == right` 即为第一个 `True` 位置（`lower_bound` 对应 `a[mid] >= target`，`upper_bound` 对应 `a[mid] > target`）。

### 2. 突破二分硬件瓶颈：Branchless、Eytzinger 布局与页内 SIMD

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

### 3. 谓词下推（Predicate Pushdown）与索引裁剪中的工程陷阱

在存储引擎用二分查找定位文件块（ZoneMap / SSTable Index Block）时，有三个经典边界极易翻车：
1. **寻址区间块边界**：对于有序块索引，查询 `col >= L` 不能二分找 `first_key >= L` 的块（因为 `L` 可能落在前一个块的 `[first_key, last_key]` 中间！），必须找**第一个满足 `last_key >= L` 的块**（或者等价地找第一个 `first_key > L` 的块再回退一格）。
2. **跨类型常量下推严禁直接强转**：例如列类型为 `int32_t`，查询条件是 `col <= 2.7` 或 `col >= 2.3`。不能把浮点数直接 `static_cast<int32_t>` 截断为 `2`！`col <= 2.7` 应向下取整为 `col <= 2`，而 `col >= 2.3` 必须向上取整为 `col >= 3`；若常量直接超出 `int32_t` 的表示极值，则应在计划期直接折叠为常量布尔值，避免整数溢出后做二分产生错漏。
3. **IEEE-754 全序破坏**：`NaN` 与任何值比较均返回 `false`，会直接破坏二分与排序依赖的严格弱序（Strict Weak Ordering）传递性；建索引与裁剪前必须显式规范化 `-0.0` 到 `+0.0`，并将 `NaN` 固定归位到序列末尾。

---

## 五、 位图结构：Roaring Bitmap、向量化选择与 Rank/Select

位图是列式分析引擎、倒排索引和集合运算的骨架，核心命题是如何在**稀疏内存开销**与**极速批量位运算**之间取得平衡。

### 1. Roaring Bitmap 的二级分桶与自适应容器

传统纯 `Bitset` 哪怕只存一个大整数 `100,000,000` 也要占用 12MB；而早期的 WAH/EWAH 游程压缩位图虽然省空间，却丧失了 $O(1)$ 随机定位能力，集合求交必须从头流式解码。

**Roaring Bitmap** 将 32 位无符号整数拆为高 16 位和低 16 位：高 16 位作为一级有序桶索引，每个桶覆盖 $[0, 65535]$ 的范围，并根据桶内实际元素基数 $C$ 动态切换三种底层容器：
- **`ArrayContainer`（$C \le 4096$）**：有序 `uint16_t` 数组。4096 这个阈值并非拍脑袋——$4096 \times 2\text{B} = 8\text{KB}$，恰好与完整覆盖 65536 个比特位的位图体积完全相等。
- **`BitmapContainer`（$C > 4096$）**：定长 `uint64_t[1024]`（恒占 8KB），再稠密也不会继续膨胀。
- **`RunContainer`（连续区间压缩）**：用 `{ uint16_t start, uint16_t len }` 记录连续 1 区间，适合自增主键或局部聚类列。

做集合求交（`AND`）时，先在一级高 16 位索引上做双指针或跳跃（Galloping）对齐，不匹配的 65536 范围直接整块跳过；桶内再按容器类型走异构快速路径——`Bitmap & Bitmap` 直接跑 AVX2 按位与并用硬件 `popcnt` 实时统计新基数（低于 4096 则自动降级为 `ArrayContainer`），`Array & Bitmap` 则只需 $O(|Array|)$ 次查表，完全无需解压。

### 2. 列存执行：Bitmap 转 Selection Vector 与 $O(1)$ Rank/Select

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

## 六、 概率数据结构：Blocked Bloom、Xor/Ribbon Filter 与 HyperLogLog

在海量数据过滤与统计中，允许极小的可控误差（如 1% 假阳性或 <1% 基数误差）往往能把内存和 I/O 开销压低一到两个数量级。

### 1. 从标准 Bloom Filter 到 Blocked Bloom 与 Ribbon Filter

标准 Bloom Filter 在 1% 假阳性率下需要 $m/n \approx 9.6\text{ bits/key}$ 与 $k=7$ 个哈希位置。工业界早已用 **Kirsch-Mitzenmacher 双哈希定理**（$g_i(x) = h_1 + i \cdot h_2 \pmod m$）把哈希计算降到了只算 1 次 128-bit 哈希，但真正的瓶颈依然存在：**这 7 个 bit 随机散布在几百 MB 位图中，每次查询触发 7 次独立的 DRAM 随机访存（7 次 L3 Cache Miss）**。

- **Blocked Bloom Filter（Split-Block Bloom Filter）**：Parquet、DuckDB 与 Velox 的标配方案。把位图切分为 **32 字节（256 bits，恰好一个 AVX2 寄存器）**或 **64 字节（一条 Cache Line）**的小块。查询时先用高 32 位哈希确定落在哪一个 Block（**整次查询最多只读 1 次内存**），再用低 32 位哈希在 AVX2 寄存器内乘以 8 个奇数常数一次生成 8 路探测掩码，单条 `_mm256_testc_si256` 指令即可一周期判定是否命中，吞吐量暴涨 5~10 倍。
- **Xor Filter 与 RocksDB Ribbon Filter**：Bloom Filter 距离信息论理论下界 $\log_2(1/p)$ 还有 44% 的空间冗余。当集合构建后只读不改（例如 SSTable 落盘或 Hash Join Build 完成）时，利用超图剥离（Xor Filter）或高斯消元求解稀疏线性方程组（Ribbon Filter），只需约 **7.0~7.7 bits/key** 就能达到同样的 1% 假阳性率，直接省下近三成内存，且每次查询固定只有 3 次访存。

在向量化引擎的 **Hash Join Runtime Filter** 中，Build 端顺手生成的 Blocked Bloom Filter 会被下推给大表的 Scan 算子提前剪枝，但工程上必须配合两道**自适应熔断**：如果 Build 端表太大导致 Filter 体积撑爆 L3 Cache，直接放弃生成；如果 Scan 端运行几个 Morsel 后统计发现 Filter 拦截掉的行数不足 10%，立即熔断关闭该 Filter，避免为几乎全命中的表白交哈希计算税。

### 2. HyperLogLog（HLL）基数估算的核心直觉

HyperLogLog 解决“极低内存估算亿级不同元素个数（NDV）”的问题。它的数学直觉非常优雅：均匀哈希后的二进制串里，观测到“连续 $k$ 个前导 `0`”的概率是 $2^{-(k+1)}$，因此最大前导零长度能反推数据规模。

为了防止单次撞大运出现极值把结果拉偏，HLL 用哈希高 $p$ 位（如 $p=14$）将数据随机分入 $m = 2^{14} = 16384$ 个桶中，每个桶仅需 **6 bits** 记录该桶见过的最大前导零位数（总内存恒为 **12KB**，标准误差 $\approx 1.04/\sqrt{m} \approx 0.81\%$）。汇总计算时，HLL **绝对不能用算术平均数**——因为 $2^{M[j]}$ 是重尾分布，个别偏大的桶会彻底拉高均值；采用**调和平均数** $E = \alpha_m m^2 (\sum 2^{-M[j]})^{-1}$ 取倒数后能天然压制异常大值的权重。而当真实基数很小、大量桶仍为 `0`（空桶数 $V$ 较高）时，则需无缝切换至基于泊松分布的 **Linear Counting** 公式 $m \ln(m/V)$ 来修正小基数偏差。

---

## 七、 存储引擎核心：B+ Tree 与 LSM Tree 的读写放大与并发演进

如果用 **RUM 猜想（Read / Update / Memory-Space Overhead 三者不可兼得）** 来统领存储引擎选型，B+ 树与 LSM 树恰好站在设计光谱的两端：

- **B+ 树的隐性写放大**：B+ 树面向**读优化**，点查稳定 $O(\log_B N)$。但它的原地更新（In-place Update）在随机写下极其沉重：哪怕只修改一行 100 字节的记录，也要把整张 16KB 数据页刷盘；为了防断电写半页损坏（Torn Page），还得先写 Redo Log 再写 16KB Doublewrite Buffer，实际随机写放大往往超过 300 倍，且页分裂使平均空间填充率停留在 $\ln 2 \approx 69\%$。
- **LSM 树的 Compaction 权衡**：LSM 树通过 `WAL + MemTable (SkipList)` 把所有随机写变成内存写与顺序追加，将代价后移给后台 Compaction。主流策略在读写空间放大之间做取舍：
  - **Leveled Compaction（RocksDB 默认）**：层内 SSTable 范围互不重叠，层间呈 $T=10$ 倍增长。点查每层至多看 1 个文件、空间放大极低（约 10%），但同一条数据在每层都要被反复重写归并，写放大较大（典型值 10~30 倍）。
  - **Size-Tiered / Universal Compaction**：同量级文件攒够就合并，每层只写 1 次，写放大极低（2~5 倍）；代价是同层文件范围大量重叠导致读放大飙升，且 Major Compaction 期间需要预留 **50%~100%** 的额外磁盘空间。

### 1. 高并发 B+ 树的并发控制演进：从 Crabbing 到 OLC

传统悲观锁蟹行（Latch Crabbing）在向下遍历时必须同时锁住父子节点，甚至连读操作都要在根节点上对 `RWLatch` 做原子引用计数修改（`fetch_add/sub`），在 64 核 CPU 上会把根节点的 Cache Line 彻底打瘫。现代内核的突破分为两条路：
- **B-link Tree（PostgreSQL 采用）**：每个节点增加 `right_sibling` 指针与最大边界 `high_key`，把“节点分裂”与“向父节点插入分隔键”解耦。并发读线程顺着旧指针跌进刚被分裂的节点时，只要发现 `search_key > high_key`，直接顺着右兄弟指针横向平移即可，彻底无需同时锁住父子两层。
- **Optimistic Lock Coupling（OLC，LeanStore / Umbra 标配）**：每个节点仅维护一个 64-bit `version_lock`。读线程遍历时**完全不执行任何原子写内存操作**——只读取父节点版本号 $v_p$，查到子节点指针并读子节点版本号 $v_c$ 后，回头校验父节点版本号是否依然等于 $v_p$（未变则安全放行，变了则乐观重试）。读路径零共享内存写入，让多核读扩展性呈完美线性。

### 2. 大 Value 下的 KV 分离（WiscKey / Integrated BlobDB）

当 Value 体积超过 1KB 时，让 LSM Compaction 反复搬运庞大的 Value 纯属带宽灾难。**WiscKey / RocksDB BlobDB** 的思路是**把 Key 和 Value 拆开**：大 Value 直接顺序追加写进独立的 Blob 文件，LSM 树内部只留 `Key -> {blob_file_id, offset, size}` 指针，把 LSM 体积缩减几十倍。随之而来的两个工程难点分别是：**Blob 垃圾回收（GC）**（RocksDB Integrated BlobDB 将 Blob 生命周期与 SSTable Level 绑定，在 Compaction 时按废弃率顺带流式重写，避免独立 GC 反向查询 LSM 的高昂开销）以及 **Range Scan 随机读退化**（借助 NVMe SSD 高队列深度，结合 `io_uring` 对后续一批相邻 Key 指向的离散 Blob Offset 发起批量异步并行预读）。

---

## 八、 字符串匹配：SIMD 预过滤、Z-Algorithm 与扁平化 AC 自动机

面试算法题常考 KMP 的 `next` 数组，但在 ClickHouse、Velox 或高性能 `grep` 源码里，几乎见不到用标准 KMP 来跑 SQL `LIKE '%pattern%'`。

### 1. 为什么工业界放弃纯 KMP，转向 SIMD Rare-Pair Filtering？

KMP 虽然拥有严格的 $O(N + M)$ 理论上界，但工程上必须**逐字节**检查主串，且内层失配回跳 `while (j > 0 && text[i] != pat[j]) j = next[j - 1];` 存在强数据依赖与频繁的条件跳转，彻底锁死了超标量 CPU 的指令级并行。

现代向量化引擎的标准范式是 **SIMD 稀有字符对预过滤**（如 `memchr::memmem` 与 Volnitsky 思想）：从模式串中挑选首尾字符（或英文语料中出现频率最低的两个字符及其相对间距 $\Delta$），将其广播填满两个 256-bit AVX2 寄存器。扫描主串时，每次步进 32 字节并行比较位置 `i` 和 `i + delta` 是否同时命中这两个字符：

```cpp
__m256i eq1 = _mm256_cmpeq_epi8(_mm256_loadu_si256((const __m256i*)(text + i)), v_first);
__m256i eq2 = _mm256_cmpeq_epi8(_mm256_loadu_si256((const __m256i*)(text + i + delta)), v_last);
uint32_t mask = _mm256_movemask_epi8(_mm256_and_si256(eq1, eq2));
```

99.9% 的情况下 `mask == 0`，主循环无任何内部分支直接 `i += 32` 飞跃前进；只有极少数情况 `mask != 0` 时，才用 `__builtin_ctz(mask)` 提取位点调一次 `memcmp` 确认中间字符，单核吞吐轻松达到十余 GB/s。

### 2. Z-Algorithm 的对称性与 AC 自动机工程重构

- **Z-Algorithm vs KMP**：KMP 的 $\pi[i]$ 定义为“以 $i$ **结尾**的最长匹配真前缀”；而 Z-Algorithm 的 $Z[i]$ 定义为“以 $i$ **开头**的后缀与原串的最长公共前缀（LCP）”。维护当前向右延伸最远的匹配窗口 $[L, R]$（Z-Box），当 $i \le R$ 时直接复用镜像 $Z[i - L]$ 跳过已知相等的字符，仅在超出 $R$ 时向右扩展，求字符串周期与所有后缀前缀关系时比 KMP 更直观。
- **Aho-Corasick（AC 自动机）的扁平化重构**：多模式匹配（如 `LIKE ANY` 关键词集合或敏感词过滤）如果按教科书写成 `struct Node { Node* next[256]; Node* fail; }`，单节点光指针就超过 2KB，直接撑爆缓存，且匹配时沿着 `fail` 链反复 `while` 回跳会产生极不稳定的 Pointer Chasing 延迟。工程化重构有两步：
  1. **补全为 DFA 状态转移表（Trie Graph）**：在 BFS 构建 `fail` 指针时，若当前节点缺失字符 `c` 的出边，直接将 `trie[u].next[c] = trie[trie[u].fail].next[c]` 固化进转移表。匹配主串的内层循环立刻退化为**零分支、零回跳的一行查表**：`state = dfa[state].next[byte]`。
  2. **字符集等价类压缩 + Double-Array Trie（双数组 DAT）**：先用 256 字节查找表把所有未在模式串中出现的无关字符全部折叠为同一个 `class 0`，将状态表列宽压缩数倍；再用紧凑的 `base[]` 与 `check[]` 两个一维 `int32_t` 数组替代二维稀疏矩阵，将整个自动机稳稳装进 L2/L3 Cache。

---

**整理基线说明**：本文沉淀的缓存管理（[lru.hpp](https://github.com/LiShuMing/xlab/blob/main/cc/cclab/src/utils/lru.hpp)、[prefix_cache_lru.h](https://github.com/LiShuMing/xlab/blob/main/cc/cclab/src/utils/prefix_cache_lru.h)）、无锁队列（[mpmc_queue.h](https://github.com/LiShuMing/xlab/blob/main/cc/cclab/src/common/queue/mpmc_queue.h)）与 Work-Stealing 线程池（[labs/runtime/pool/](https://github.com/LiShuMing/xlab/tree/main/labs/runtime/pool)）原型均可在 `xlab` 仓库对照翻阅；与算法母题及并发范式相关的补充可参见 [系统工程与算法面试核心范式]({{< relref "2026-11-18-system-and-algorithm-interview-patterns.md" >}})。
