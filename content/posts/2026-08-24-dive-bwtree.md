---
title: "【源码】深入 Bw-Tree：Mapping Table、Delta Chain 与无锁结构变更"
date: 2026-08-24T00:00:00+08:00
lastmod: 2026-11-12T00:00:00+08:00
slug: "dive-bwtree"
categories:
  - 数据库
tags:
  - Bw-Tree
  - B+Tree
  - Lock-Free
  - Index
  - Concurrency
description: "从多核 B+Tree 闩锁耦合的缓存行乒乓与单字 CAS 跨页原子性困局出发，结合 Open BwTree 源码拆解 Mapping Table、Delta Chain、两阶段协作分裂、Epoch 回收，以及它为何在现代 CPU 上被乐观锁耦合反超。"
draft: false
toc: true
---

把一棵教科书里的内存 B+-Tree 放到 64 核 NUMA 服务器上跑高并发负载，如果用最朴素的做法，第一步会在哪里撞墙？

按照经典并发协议，线程从根节点向叶子节点下探时，需要执行**闩锁耦合（Latch Crabbing）**：先给父页加读闩锁（Shared Latch），拿到子页读闩锁后，再释放父页读闩锁。表面上看，读闩锁之间互不阻塞，纯只读工作负载应该能随核数线性扩展；可在现代多核 CPU 的 MESI 缓存一致性协议下，“加读闩锁”本质上仍是一次原子写操作——无论是递增 `reader_count` 引用计数还是修改锁字状态，都会执行 `LOCK XADD` 或 `CAS` 指令，强行夺取该缓存行（Cache Line）的独占所有权（Modified/Exclusive 状态），把其他几十个核心 L1/L2 缓存里的同一行瞬间作废。由于所有查询都必经根节点与上层内部节点，哪怕全表没有一条写入，光是并发读者争抢根页锁字引发的**缓存行乒乓（Cache-Line Ping-Pong）**，就足以让多核吞吐早早撞上天花板。

既然原地加锁会打爆缓存一致性总线，最自然的想法是彻底扔掉页面闩锁，改用硬件原生的无锁原子指令 `CAS`（Compare-And-Swap）。但只要动手写十行代码，你立刻就会撞上第二堵物理高墙：**现代 64 位 CPU 只支持对单个 8 字节机器字做原子 `CAS`，而 B+-Tree 的页内插入需要搬移几十上百字节的有序数组，一次节点分裂更必须同时修改子页、右兄弟页和父节点三个完全不同的内存地址。** 单字 `CAS` 根本无法横跨多个物理地址完成原子提交。

2013 年微软研究院（Microsoft Research）发表的经典论文 [《The Bw-Tree: A B-tree for New Hardware》](https://www.microsoft.com/research/publication/the-bw-tree-a-b-tree-for-new-hardware/)，正是为了同时绕开这两堵墙而设计的一套精巧机制。顺着上述物理约束往下推演，你会发现 Bw-Tree 的四大核心组件几乎是环环相扣被逼出来的必然产物：

1. 要把跨页指针牵连缩减为单字原子切换，就必须切断父子节点间的裸物理地址引用，引入一层将逻辑页号（`NodeID`）翻译为物理内存指针的**映射表（Mapping Table）**；
2. 要避开多线程原地搬移有序数组的竞争，所有页内增删就只能先封装成不可变的**增量记录（Delta Record）**，以单链表形式头插（Prepend）到旧版本前方，靠映射表槽位上的一次单字 `CAS` 完成原子发布；
3. 版本链越挂越长会拖垮查找性能，因此链深触及阈值时必须离线重放并折叠回紧凑的**基节点（Base Node）**，即节点整合（Consolidation）；
4. 节点分裂无法一步改完父子两层，就借鉴 B-link Tree 的右兄弟与上界键（High Key），把结构变更（SMO）拆成**子层先切分可见、读者顺路协助补齐父层**的两阶段协作状态机，并配齐**基于全局代次的延迟垃圾回收（Epoch-Based GC）**防止悬垂指针。

下文我们将贴着 [Open BwTree](https://github.com/wangziqi2013/BwTree/tree/09b7354d419513ac5648980e875e1a3246be396a) 的近万行 C++ 核心实现追踪一条键值从插入、整合、分裂到内存回收的完整生命周期，并在最后回答一个极具现实启发的问题：**为什么这套设计如此优雅的无锁索引，在随后十年的现代内存与 NVMe 数据库竞赛中，反而输给了看似更“保守”的乐观锁耦合（Optimistic Lock Coupling, OLC）？**

---

## 一、间接寻址与不可变增量：把任意页修改压进一次单字 CAS

在传统 B+-Tree 里，一旦某个叶子节点因为重整或扩容换了内存地址，持有它裸指针的父节点与左右兄弟节点就必须同步加锁改写指针，跨页锁序与死锁规避随之接管整个引擎。Bw-Tree 的破局点，是把“树节点的逻辑身份”与“它在内存里的物理字节”彻底拆开。

在 [`src/bwtree.h`](https://github.com/wangziqi2013/BwTree/blob/09b7354d419513ac5648980e875e1a3246be396a/src/bwtree.h) 中，整棵树的根、父节点分隔项以及叶间双向链表，保存的全是稳定的 64 位整数 `NodeID`；真正的物理地址统一收归一张全局定长原子指针数组 `mapping_table` 管辖：

```cpp
std::atomic<NodeID> root_id;
NodeID first_leaf_id;

std::atomic<NodeID> next_unused_node_id;
std::array<std::atomic<const BaseNode *>, MAPPING_TABLE_SIZE>
    mapping_table;
```

有了这层间接寻址，每个逻辑节点不再是一块被多线程反复涂改的共享缓冲区，而是一条由不可变对象串成的**单节点局部版本链**：最底部是存放连续有序键值对数组的只读底座 `BaseNode`，上方叠加零到多个记录单次变更的 `DeltaNode`，每个 Delta 通过 `child_node_p` 指向发布前的旧链头：

![Bw-Tree 逻辑页号间接寻址、CAS Delta 链与节点整合架构](/images/diagrams/aug-store-bwtree-mapping-delta.svg)

拿一次具体的插入操作来走一遍：假设逻辑叶节点 `NodeID = 2` 当前底座存有 `(k1, v1), (k2, v2)`，上一轮刚被人追加了一条遮蔽删除 `LeafDeleteNode(k1, v1)`。现在线程 T1 要插入 `(k3, v3)`：

1. T1 先进入当前 Epoch 保护区，从 `root_id` 一路查 `mapping_table` 定位到叶子 `NodeID = 2`，读出此时的链头指针 `old_head`（即那条 `LeafDeleteNode`），并在链上确认 `(k3, v3)` 尚未存在；
2. T1 在线程私有堆内存里完整构造好一个新的 `LeafInsertNode(k3, v3, child = old_head)`——此时没有任何其他线程能看见这个新对象，自然不需要任何锁；
3. T1 调用统一的发布原语 `InstallNodeToReplace()`，对 `mapping_table[2]` 发起单字 `CAS`：

```cpp
inline bool InstallNodeToReplace(NodeID node_id,
                                 const BaseNode *node_p,
                                 const BaseNode *prev_p) {
  assert(node_id != INVALID_NODE_ID);
  assert(node_id < MAPPING_TABLE_SIZE);
  return mapping_table[node_id].compare_exchange_strong(prev_p, node_p);
}
```

这里的 `compare_exchange_strong` 采用 C++ 默认的顺序一致内存序（`memory_order_seq_cst`），它就是单次索引修改对外生效的**唯一线性化点（Linearization Point）**。如果返回 `true`，`(k3, v3)` 当即成为逻辑叶 `2` 的最新状态，而父节点里存的 `NodeID = 2` 连一个比特都不用动；如果返回 `false`，说明在 T1 构造对象的短短几十纳秒内，另一个线程 T2 已经抢先把 `mapping_table[2]` 改成了别的链头——此时 T1 绝不能把基于过期状态构造的 Delta 硬接到新链上，只能直接 `delete` 掉尚未对外暴露的候选 `LeafInsertNode`，重新从根遍历再来一遍。

删除操作（`Delete(k1, v1)`）走的是完全对称的路径：确认键值存在后，离线构造 `LeafDeleteNode` 并 `CAS` 推上链头。它既不原地擦除 `BaseNode` 里的字节，也不立即回收被遮蔽的记录。Open BwTree 按层级区分了数据更新与结构更新两类不可变 Delta：

| 层级 | 数据更新 Delta | 结构变更（SMO）Delta |
| --- | --- | --- |
| 叶子层（Leaf） | `LeafInsertNode`、`LeafDeleteNode` | `LeafSplitNode`、`LeafRemoveNode`、`LeafMergeNode` |
| 内部层（Inner） | `InnerInsertNode`、`InnerDeleteNode` | `InnerSplitNode`、`InnerRemoveNode`、`InnerMergeNode`、`InnerAbortNode` |

大家很容易把 Bw-Tree 的 Delta Chain 与 LSM-Tree 或 Kudu 的 Delta Store 混为一谈，但两者的物理作用域截然不同：LSM-Tree 的 Compaction 是在横跨全键空间的多个有序 SSTable 文件之间做全局归并重写；而 Bw-Tree 的 Delta Chain 严格限制在**单个逻辑页内部**。

正因为读取一个逻辑页不再是对着连续数组做一次干净的二分查找，而是必须顺着指针从最新 Delta 一跳一跳往旧端解释（新插入优先命中、遇到 Delete Delta 当场遮蔽旧值、最后才落回 `BaseNode` 二分），链越长，读放大与缓存缺失就越惨烈。Open BwTree 将内部节点与叶子节点的链长阈值统一设为 `8`：

```cpp
#define INNER_DELTA_CHAIN_LENGTH_THRESHOLD 8
#define LEAF_DELTA_CHAIN_LENGTH_THRESHOLD 8
```

一旦某次遍历发现链深达到 `8`，`TryConsolidateNode()` 就会离线分配一块全新的 `BaseNode`：叶子层调用 `CollectAllValuesOnLeaf()`（辅以小型有序集 `sorted_small_set.h` 与布隆过滤器 `bloom_filter.h` 快速判重、剔除被遮蔽项），内部层调用 `CollectAllSepsOnInner()`，把整条链折叠回一块紧凑的有序数组，再用同一次 `InstallNodeToReplace()` 把 `mapping_table[id]` 从旧链头原子切向新 `BaseNode`。这同样是个**机会性操作**——万一整合期间别的写线程先一步追加了新 Delta 导致 `CAS` 失败，当前线程只需丢弃刚建好的新 `BaseNode` 即可，索引正确性毫发无损，被成功替换下来的旧链则移交后续 Epoch 回收队列。

---

## 二、两阶段无闩锁结构变更：B-link 容错导航与 ABORT 合并边界

单点页内更新靠 `Mapping Table + CAS` 轻松化解了，但当整合后的 `BaseNode` 逻辑项数涨破上限 `128`（`LEAF_NODE_SIZE_UPPER_THRESHOLD`）或跌破下限 `32`（`LEAF_NODE_SIZE_LOWER_THRESHOLD`）时，真正的硬骨头才浮出水面：**不加跨页锁，怎么安全地把一个节点劈成两半，或者把两个节点合二为一？**

### 2.1 两阶段分裂与读者 Help-Along 协作推进

试想叶子节点 `L` 存满了 `[10, 90)` 的记录，需要以 `K = 50` 为界拆出右兄弟 `R`（承载 `[50, 90)`）。由于单字 `CAS` 无法同时修改 `mapping_table[L]` 和 `mapping_table[Parent]`，如果在两步之间哪怕隔着几纳秒的窗口，并发查找 `key = 65` 的读线程顺着尚未更新的 `Parent` 摸进 `L`，岂不是会误判 `65` 不存在？

Bw-Tree 借用了 Lehman-Yao **B-link Tree** 的经典不变量，把跨页分裂拆成两个任何时刻都可独立解释的原子阶段（可点击查看[交互式两阶段无闩锁分裂与协作推进流程图](/diagrams/aug-store-bwtree-cooperative-smo.html)）：

![Bw-Tree 两阶段无闩锁分裂（Split SMO）与读者协作推进协议](/images/diagrams/aug-store-bwtree-cooperative-smo.svg)

沿着上图追踪一次具体的分裂与并发偶遇：

1. **阶段一（子层切分并暴露 B-link 导航桥）**：分裂线程先在离线内存里建好承载 `[50, 90)` 的右半节点 `Base R`，申请新逻辑号 `NodeID = R` 安装进 `mapping_table[R]`（此时没有任何路径指向 `R`，绝对安全）；紧接着在左节点 `L` 上 `CAS` 发布一条 `LeafSplitNode`，将 `L` 的有效上界收缩为 `high_key = 50`，并把右兄弟指针设为 `next_node_id = R`。这一步 `CAS` 一旦成功，分裂在数据层就已经生效了！哪怕父节点此刻还完全不知道 `R` 的存在，并发查 `65` 的读线程落入 `L` 后，对比发现 `65 >= high_key(50)`，立刻就能顺着右兄弟 `NodeID` 横向跳进 `R` 拿到正确结果。
2. **阶段二（协作补齐父节点分隔键）**：接下来需要把新分隔项 `(50, R)` 作为 `InnerInsertNode` 发布到 `Parent` 的链头上。精妙之处在于，Bw-Tree **不要求必须由原分裂线程独自跑完阶段二**。如果原分裂线程刚做完阶段一就被操作系统调度挂起了，任何后续路过 `L` 的普通读线程或写线程，在加载节点后都会例行调用 `FinishPartialSMO()`；一旦看到链上挂着尚未在父节点收敛的 `SplitDelta`，路人线程就会主动替它构造父级 `InnerInsertNode(50, R)` 并发起 `CAS`（即 **Help-Along 协作推进协议**）。即便原线程醒来后也去发了一次 `CAS`，败者只需根据父节点最新状态丢弃重复候选即可。唯一的特例是根节点分裂：因为根没有父节点，代码会构造一个指向左右子页的新根，再通过 `root_id.compare_exchange_strong(old_root, new_root)` 原子切换全局根号。

### 2.2 为什么节点合并难得多：Open BwTree 的 `InnerAbortNode` 妥协

相比顺水推舟的分裂，节点移除与合并（Merge / Remove）在并发无锁环境里要凶险得多。分裂时父节点就算慢半拍，左页的 `high_key` 与右链总能把迷路的请求导过去；可合并要把待删右节点 `R` 的残余数据并入左兄弟 `L`、再从父节点删掉指向 `R` 的分隔键——如果父节点在此期间正巧并发发生了另一场分裂或合并，辅助线程仅凭手头过期的父快照看到“分隔键不在了”，根本分不清是“别人已经帮我合完了”还是“父节点自己搬家了”。

为了堵住这个致命的竞态窗口，Open BwTree 在原始论文之外引入了一个专门的父节点快照冻结标记 `InnerAbortNode`：

```cpp
InnerAbortNode *abort_node_p = new InnerAbortNode{parent_node_p};

bool ret = InstallNodeToReplace(parent_node_id,
                                abort_node_p,
                                parent_node_p);
```

合并线程必须先在父节点链头 `CAS` 挂上一枚 `InnerAbortNode`，强行拦住所有基于旧父快照试图提交的并发修改；随后在待删子节点发布 `LeafRemoveNode` / `InnerRemoveNode`（把迟到的访问重定向往左兄弟），摘除父节点的 `ABORT` 标记，最后才将数据并入左兄弟并向父节点发布 `InnerDeleteNode`。

这恰恰暴露了阅读并发源码时最值得警惕的工程现实：代码里没有 `std::mutex`，绝不自动等于算法满足理论上的严格无锁。我们必须把三个常被混为一谈的进展保证（Progress Guarantee）掰开看清：

| 进展级别 | 核心判定标准 | Open BwTree (`09b7354`) 的真实表现 |
| --- | --- | --- |
| **无闩锁（Latch-Free）** | 工程实现上不依赖传统的阻塞式页读写互斥锁 | **满足**：全路径采用不可变节点与原子 `CAS` 驱动 |
| **无锁（Lock-Free）** | 即使任意线程在任意指令处永久停顿，系统整体仍不断有其他线程完成操作 | **部分削弱**：仓库 [README](https://github.com/wangziqi2013/BwTree/blob/09b7354d419513ac5648980e875e1a3246be396a/README.md) 坦承，`InnerAbortNode` 依赖原发布线程亲自撤销；若该线程刚挂上 `ABORT` 就崩溃或死循环，撞上同一父节点的其他线程将无法继续推进 |
| **无等待（Wait-Free）** | 每一个线程都能在有限步内完成自身操作，绝不饥饿 | **不满足**：热点键上的高频 `CAS` 败者与 SMO 重试均可能反复退避 |

---

## 三、Epoch 延迟内存回收与局部物化迭代器

搞定了无闩锁读写与结构变更，还剩下一个足以让 C++ 程序当场段错误（Segmentation Fault）的悬崖：当整合线程用 `CAS` 把 `mapping_table[2]` 从旧链头切向新 `BaseNode` 后，**那条被摘下来的旧 Delta 链什么时候才能 `delete`？**

绝对不能当场 `delete`！因为读线程全程不加页闩锁，完全可能有一个读线程 T_read 在 `CAS` 发生前 1 纳秒刚刚从 `mapping_table[2]` 读出了旧链头地址，此刻正悬在半空中准备解引用 `old_head->child_node_p`。如果给每个 Delta 节点都加上 `std::shared_ptr` 原子引用计数，那每次指针追逐又会退化回摧毁缓存行的原子写争用。

Bw-Tree 采用的是**基于全局代次的内存回收（Epoch-Based Reclamation）**。在当前默认启用的 `BwTreeBase` GC 实现中，内存销毁被拆成“逻辑摘除”与“跨代安全释放”两个阶段：

![Bw-Tree 基于全局 Epoch 的无锁内存延迟回收生命周期](/images/diagrams/aug-store-bwtree-epoch-gc.svg)

这套协议在源码里靠四条硬规矩运转：

1. **线程预注册与缓存行隔离**：所有访问树的线程必须在实例工作前调用 `RegisterThread()` 领取独立的 `gc_id`（`gc_id = total_thread_num.fetch_add(1)`），每线程的 GC 元数据结构体做了严格的 Cache-Line 对齐，彻底杜绝线程各自更新活跃代次时的伪共享（False Sharing）；
2. **受保护临界区**：每次读写操作入口调用 `JoinEpoch()` 把全局当前代次记入本线程的 `last_active_epoch`，操作结束调用 `LeaveEpoch()` 清除活跃标记——任何从 `mapping_table` 取出的裸指针，其生命周期严禁逃出这对括号；
3. **按代次挂入垃圾链**：被整合或 SMO 替换下来的旧链，连同删除时刻的全局代次 `E_del` 一起挂进当前线程私有的垃圾链表（对于移除节点的 `LeafRemoveNode` / `InnerRemoveNode` 还一并携带 `removed_id`）；
4. **安全水位批量清扫**：当某线程攒够 `1024` 个待回收节点时，触发 `PerformGC()` 扫描所有已注册线程，算出最小的 `last_active_epoch` 作为安全边界。只要垃圾对象的 `E_del < min_active_epoch`，就证明当时可能见过它的所有在途读者百分之百已经离开了现场，可以放心物理析构。

这套机制直接决定了范围迭代器 `ForwardIterator`（`Begin()` / `Begin(start_key)`）的写法：既然调用方可能在两次 `++iter` 之间停顿几百毫秒，迭代器就绝不能长期攥着一个裸叶子链指针不放（否则要么违背协议惨遭释放，要么长期不调 `LeaveEpoch()` 一人拖死全局内存回收，导致短命 Delta 链无限堆积撑爆内存）。因此，Open BwTree 的迭代器在进入 Epoch 后，会一口气把当前逻辑叶子的有序键值对**完整拷贝物化**到私有的 `IteratorContext` 数组中，随即立刻退出 Epoch；等扫完当前页需要跨页时，再凭保存的 `high_key` 重新下探定位下一张逻辑页（顺带规避并发合并可能带来的边界重复）。

这也引出了存储引擎与上层事务系统之间的一道关键分界线：**叶子级物化只保障迭代器自身的 C++ 内存安全，不提供跨页一致性快照（Snapshot Isolation）。** 假如扫描完第 1 页去抓第 5 页的途中，别的线程改写了第 5 页的数据，迭代器照样会看到新写入的值。如果业务需要可重复读或可串行化隔离，必须在 Bw-Tree 上方额外叠加 MVCC 版本可见性或锁管理器。

---

## 四、把单次写入串成闭环：四层正确性与现代硬件的残酷反超

现在我们可以把前面拆开的零件重新组装起来，看清一次高并发 `Insert(k, v)` 究竟靠哪三条核心不变量守住底线：

- **发布不变量**：任何新 Delta 或新 `BaseNode` 在 `mapping_table` 单字 `CAS` 成功前纯属线程私有，绝不污染共享状态；`CAS` 败者必须丢弃候选重来，禁止将过期链头硬接上网。
- **导航不变量**：在子节点已挂出 `SplitDelta` 而父节点尚未补齐分隔键的窗口期内，`high_key` 与右兄弟 `NodeID` 保证任意键值查找仍能横向抵达正确半区，且沿途线程可幂等协助收敛。
- **回收不变量**：从 `mapping_table` 摘除只代表“新读者不可达”，物理析构必须等到全局最小活跃 Epoch 越过删除代次之后。

但在把这类无锁索引接入真实数据库内核时，最容易踩中的大坑就是把“单次 `CAS` 线性化”直接脑补成“数据库 ACID 事务已提交”。实际上，从底层数据结构到生产级存储引擎横亘着四层完全独立的证明义务：

| 正确性层次 | 核心解决的问题 | Open BwTree 当前覆盖度 |
| --- | --- | --- |
| **1. 单操作线性化** | 单次 `Insert/Delete` 在哪个瞬间对并发线程原子生效？ | 由 `mapping_table[id]` 上的 `CAS` 保证 |
| **2. 结构中间态可导航** | 分裂/合并拆成多步后，并发查找会不会漏键或走丢？ | 靠 `high_key`、右链与 `FinishPartialSMO` 保障（`ABORT` 有停顿阻塞风险） |
| **3. 并发内存安全** | 无闩锁读者遍历旧链途中，内存会不会被提前释放复用？ | 靠预注册线程的 Epoch 延迟回收保障 |
| **4. 多键事务与崩溃持久性** | 多键原子回滚、快照隔离、WAL 顺序与掉电恢复怎么保证？ | **完全未覆盖**（仅实现纯内存索引，不含论文后半部分的日志结构闪存层） |

更进一步看，即便只谈纯内存高并发索引本身，Bw-Tree 发表十余年后的今天，工业界与学术界也经历了一场极具戏剧性的认知反转——当年跟风采用 Open BwTree 作为底层索引的 CMU 内存数据库 Peloton，后来在实测复盘（SIGMOD 2018 *Building a Bw-Tree Takes More Than Just Buzz Words*）并演进为 NoisePage 时，最终却用传统原地修改加乐观锁的 B+-Tree 换掉了 Bw-Tree。**为什么干掉了根页闩锁争用的 Bw-Tree，在现代多核 CPU 上反而跑不过改良后的传统 B+-Tree？**

答案就藏在现代处理器的缓存层次结构里：

| 索引架构 | 写路径并发机制 | 读路径访存特征（64B Cache Line 视角） | 核心硬件瓶颈与工程代价 |
| --- | --- | --- | --- |
| **传统悲观闩锁 B+-Tree** | 页级读写互斥锁（Latch Crabbing） | 页内连续数组二分查找，局部性好；但每层下探都要原子改写锁字 | 高核数下根与上层内部页锁字发生剧烈**缓存行乒乓** |
| **Bw-Tree（Delta + 映射表）** | 追加不可变 Delta，`Mapping Table` 单字 `CAS` | 先查映射表，再沿多级动态分配的 `DeltaNode` **跨地址追逐指针**，最后才落回 `BaseNode` | 每次指针跳转大概率触发 **L2/L3 缓存未命中（Cache Miss）** 与分支预测失败；频繁短命对象分配、整合拷贝与 Epoch 积压开销沉重 |
| **乐观锁耦合 B+-Tree（OLC / Umbra `Versioned Latch`）** | 写者短暂加页排他锁原地改数组并递增版本号 | 读者**零内存写入**：先读 64 位 `version`（纯共享态缓存命中），直接在连续紧凑页内二分，末尾复核 `version` 未变即放行 | 既彻底消除了只读路径的缓存行失效，又完整保住了连续数组二分的极致 CPU 缓存局部性，冲突时仅局部重试 |

回看这场对决，道理其实非常通透：Bw-Tree 为了绕开“读者加锁写内存”这一个痛点，不惜放弃原地连续存储，引入了映射表间接跳转与链式 Delta 指针追逐；而在现代 CPU 上，一次跨缓存行的随机指针追逐要付出一百多个时钟周期的 DRAM 访存延迟，连续跳 5 层 Delta 烧掉的时间，比在紧凑数组里做完整个二分查找还要贵得多！后来 Viktor Leis 与 Thomas Neumann 等人推广的**乐观锁耦合（Optimistic Lock Coupling, OLC）**一针见血地指出：读者根本不需要往锁字里写任何东西，只要先读一眼版本号、无锁读连续页面、出门前再核对一眼版本号没变且未上写锁，就能在**零缓存行乒乓**的前提下白拿原地有序数组的硬件预取红利。

这丝毫不掩盖 Bw-Tree 作为无锁系统设计教科书的价值——它在“逻辑身份与物理指针解耦”“把多地址修改拆成可协作收敛的单字 CAS 状态机”以及“面向闪存擦写特性的日志结构页追加”上的思想，至今仍在云原生分离式存储与无锁数据结构中反复回响。

---

## 源码阅读路径与基线说明

若想深入研读这份近万行的 [`src/bwtree.h`](https://github.com/wangziqi2013/BwTree/blob/09b7354d419513ac5648980e875e1a3246be396a/src/bwtree.h) 泛型实现，建议不要从第一行顺序硬啃，而是顺着本文展开的三条闭环按图索骥：先看 `NodeType`、`BaseNode`、`DeltaNode` 与 `InstallNodeToReplace()` 建立版本链心智模型；接着跟通 `Insert()`、`GetValue()`、`Traverse()` 以及利用 [`bloom_filter.h`](https://github.com/wangziqi2013/BwTree/blob/09b7354d419513ac5648980e875e1a3246be396a/src/bloom_filter.h)、[`sorted_small_set.h`](https://github.com/wangziqi2013/BwTree/blob/09b7354d419513ac5648980e875e1a3246be396a/src/sorted_small_set.h) 实现的 `ConsolidateNode()`；最后再攻克 `AdjustNodeSize()`、`FinishPartialSMO()`、`PostAbortOnParent()` 与 `EpochManager::PerformGC()`。

还需要留意当前开源仓库在工程落地上的几处硬边界：其一，`MAPPING_TABLE_SIZE` 被硬编码为定长 `1 << 20`（约 104 万槽位，裸指针数组约占 8 MiB），且 `InvalidateNodeID()` 中将废弃 ID 压回 [`atomic_stack.h`](https://github.com/wangziqi2013/BwTree/blob/09b7354d419513ac5648980e875e1a3246be396a/src/atomic_stack.h) 空闲栈的代码处于注释关闭状态（`// free_node_id_list.SingleThreadPush(node_id);`），长期高频分裂合并会单调耗尽 `next_unused_node_id`；其二，线程池必须在建树前静态完成 `RegisterThread()`，动态临时起停线程会破坏 Epoch 水位数组契约；其三，仓库构建脚本仍绑定早期的 `g++-5`、PAPI 与 jemalloc 参数，迁移至现代编译器时需重配工具链并挂载 ASan/TSan 做长稳并发压测。

- **源码基线**：[wangziqi2013/BwTree](https://github.com/wangziqi2013/BwTree/tree/09b7354d419513ac5648980e875e1a3246be396a)（主干提交 `09b7354`）
- **核心参考文献**：Justin J. Levandoski, David B. Lomet, Sudipta Sengupta, *The Bw-Tree: A B-tree for New Hardware*, [ICDE 2013](https://www.microsoft.com/research/publication/the-bw-tree-a-b-tree-for-new-hardware/)；Ziqi Wang et al., *Building a Bw-Tree Takes More Than Just Buzz Words*, [SIGMOD 2018](https://dl.acm.org/doi/10.1145/3183713.3196895).
