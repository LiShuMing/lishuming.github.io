---
title: "【源码】深入 Bw-Tree：Mapping Table、Delta Chain 与无锁结构变更"
date: 2026-08-24T00:00:00+08:00
lastmod: 2026-08-30T00:00:00+08:00
slug: "dive-bwtree"
categories:
  - 数据库
tags:
  - Bw-Tree
  - B+Tree
  - Lock-Free
  - Index
  - Concurrency
description: "结合 Open BwTree 源码，分析 Mapping Table、Delta Chain、CAS 线性化点、节点整合、分裂合并、Epoch GC 与工程边界。"
draft: false
---

## 1. 背景：为什么还需要一种 B+Tree

B+Tree 是数据库索引最经典的数据结构之一，但“树结构适合索引”和“传统 B+Tree 的并发实现没有代价”是两回事。

传统 B+Tree 通常原地修改页面，并通过 page latch 保护页内数据和结构变更。随着处理器核数增加，热点根节点、内部节点和缓冲池元数据上的 latch 可能成为共享瓶颈；一次节点分裂还会跨越子节点、父节点乃至根节点，锁顺序、死锁规避和恢复逻辑都会随之复杂化。

微软研究院在 2013 年发表的 [The Bw-Tree: A B-tree for New Hardware](https://www.microsoft.com/research/publication/the-bw-tree-a-b-tree-for-new-hardware/) 提出了一条不同路线：

> 不再原地修改树节点，也不让父节点保存子节点的物理地址；所有更新都先变成不可变 Delta，并通过 Mapping Table 上的一次 CAS 发布。

本文结合 [Open BwTree](https://github.com/wangziqi2013/BwTree/tree/09b7354d419513ac5648980e875e1a3246be396a) 的源码回答以下问题：

1. Bw-Tree 试图解决传统 B+Tree 的什么问题？
2. Mapping Table 为什么是整个设计的枢纽？
3. Delta Chain 如何把“原地修改”变成“追加并发布”？
4. 一次插入、删除、分裂和合并如何完成？
5. 不持有 latch 后，旧节点何时才能安全释放？
6. 论文中的 Bw-Tree 与这个开源实现之间有哪些重要边界？

本文研究的源码基线为提交 [`09b7354`](https://github.com/wangziqi2013/BwTree/commit/09b7354d419513ac5648980e875e1a3246be396a)。这是一个适合学习算法与并发协议的研究型实现，而不是可以直接嵌入生产数据库的完整存储引擎。

## 2. 先说结论

Bw-Tree 的关键不只是“无锁 B+Tree”，而是把树节点拆成了三个层次：

- **逻辑身份**：稳定的 `NodeID`；
- **物理版本**：Mapping Table 当前指向的 Delta Chain 头部；
- **可见内容**：读取 Base Node 并重放 Delta 后得到的逻辑页面。

由此产生四个核心结论：

1. **Mapping Table 是地址间接层，也是更新的序列化点。** 父子节点只记录 `NodeID`，页面换地址、整合或结构变化时不需要同步修正所有入边。
2. **Delta Chain 是页级版本日志。** 单条记录的更新先构造不可变 Delta，再以 CAS 发布；失败者丢弃自己的候选版本并重试。
3. **结构变更是可被帮助完成的多阶段协议。** Split Delta 可以先让新边界立即可见，再异步把分隔键补进父节点。
4. **消除 latch 不等于消除复杂性。** 复杂性转移到了链重放、冲突重试、结构变更状态机和内存回收。

还必须补充两个实现边界：

- 论文还讨论了面向闪存的日志结构存储管理器；本地 Open BwTree 仓库实现的是**内存索引数据结构**，没有 WAL、恢复和持久化页面管理，不能把论文完整系统的能力直接归于这份代码。
- 该实现为处理合并竞态引入 `InnerAbortNode`。仓库 README 明确指出，如果发布 ABORT 的线程永久停顿，其他线程可能无法继续，因此这个具体实现的进展保证需要谨慎表述。它的普通更新路径是 latch-free/CAS 驱动的，但不能无条件宣称所有路径都满足严格 lock-free 或 wait-free。

## 3. 设计坐标：Bw-Tree 与哪些方案竞争

Bw-Tree 不是对所有索引结构的单向替代。它选择的是“有序索引 + 高并发更新 + 间接寻址”这一组权衡。

| 结构 | 更新方式 | 并发与结构变更 | 范围查询 | 主要代价 |
| --- | --- | --- | --- | --- |
| 传统 B+Tree | 原地修改页面 | page latch，分裂时修改父节点 | 强 | 热点 latch、锁顺序和跨页协议 |
| B-link Tree | 原地修改并维护右兄弟和 high key | latch 或乐观协议 | 强 | 仍需管理原地并发写入 |
| Bw-Tree | Delta 追加，Mapping Table CAS | 分阶段 SMO，可帮助完成 | 强 | Delta 读放大、回收和状态机复杂 |
| Lock-free Skip List | CAS 修改多级链表 | 算法相对直接 | 较强 | 指针开销大，缓存局部性通常弱于页式树 |
| ART / Masstree | Trie 或混合树，强调缓存效率 | 取决于具体实现 | 强 | 节点类型和并发协议复杂 |
| LSM-Tree | 写入 MemTable，后台全局分层合并 | 前台写优化 | 强 | 读放大、空间放大和 compaction |
| Hash Index | 桶级更新 | 容易做细粒度并发 | 弱 | 不支持天然有序扫描 |

Bw-Tree 的 Delta Chain 容易让人联想到 Kudu Delta Store 或 LSM-Tree，但二者不应混为一谈：

- Bw-Tree 的 Delta 是**单个逻辑节点内部**的短版本链；
- Consolidation 把这条链折叠回一个 Base Node；
- LSM compaction 则在多个有序 run、层级和存储文件之间重写数据。

两者都用“先追加、后合并”换取写路径简化，但作用域和系统目标不同。

## 4. 总体架构：逻辑节点与物理版本分离

Open BwTree 中，根节点由原子的 `root_id` 标识，父节点和兄弟节点保存的也是 `NodeID`。真正的内存地址由 Mapping Table 解析。

```text
                    root_id (atomic<NodeID>)
                              │
                              ▼
                    ┌───────────────────┐
                    │   Mapping Table   │
                    │ NodeID -> pointer │
                    ├───────────────────┤
                    │  1 -> chain head ─┼──► Inner Delta ... ► Inner Base
                    │  2 -> chain head ─┼──► Leaf  Delta ... ► Leaf  Base
                    │  3 -> chain head ─┼──► Leaf  Delta ... ► Leaf  Base
                    └───────────────────┘
                              ▲
                              │ CAS(NodeID, old head, new head)
                         并发更新发布点
```

源码中的核心成员如下：

```cpp
std::atomic<NodeID> root_id;
NodeID first_leaf_id;

std::atomic<NodeID> next_unused_node_id;
std::array<std::atomic<const BaseNode *>, MAPPING_TABLE_SIZE>
    mapping_table;
```

相关定义位于 [`src/bwtree.h`](https://github.com/wangziqi2013/BwTree/blob/09b7354d419513ac5648980e875e1a3246be396a/src/bwtree.h)。

### 4.1 Mapping Table 解决了什么

如果父节点直接保存子节点地址，那么子节点被重写、搬迁或合并时，父节点也必须跟着更新。Mapping Table 增加一次间接寻址，却换来一个重要性质：

> `NodeID` 的逻辑身份可以保持稳定，而它对应的物理版本可以独立切换。

因此，下列操作都可以归一化为“替换某个 Mapping Table 表项”：

- 在旧链头上发布 Insert/Delete Delta；
- 把长 Delta Chain 整合成新 Base Node；
- 发布 Split/Remove 等结构 Delta；
- 让并发读者继续沿旧版本完成，再延迟回收旧内存。

发布函数只有一次 `compare_exchange_strong`：

```cpp
inline bool InstallNodeToReplace(NodeID node_id,
                                 const BaseNode *node_p,
                                 const BaseNode *prev_p) {
  assert(node_id != INVALID_NODE_ID);
  assert(node_id < MAPPING_TABLE_SIZE);
  return mapping_table[node_id].compare_exchange_strong(prev_p, node_p);
}
```

这里没有显式指定 memory order，因此使用 C++ 原子操作默认的顺序一致性。对一次成功更新而言，这个 CAS 就是新版本对其他线程生效的线性化点。

### 4.2 固定表带来的容量边界

这份实现把 Mapping Table 固定为：

```cpp
#define MAPPING_TABLE_SIZE ((size_t)(1 << 20))
```

也就是最多约 104 万个表项。按 64 位指针粗略估算，数组本身约占 8 MiB，尚未计算原子对象和其他元数据。

代码虽然定义了 `free_node_id_list`，但当前提交的 `InvalidateNodeID()` 中，重新压回空闲栈的语句被注释：

```cpp
inline void InvalidateNodeID(NodeID node_id) {
  mapping_table[node_id] = nullptr;
  // free_node_id_list.SingleThreadPush(node_id);
}
```

因此，当前快照实际上不会完整复用被删除节点的 ID。长期运行时，`next_unused_node_id` 会持续增长，最终触碰固定 Mapping Table 的上限。这不是算法必然限制，而是这个开源实现需要生产化改造的地方。

## 5. 节点模型：Base Node 加不可变 Delta

每个逻辑节点最终落在一个 Base Node 上，其上可以叠加多个 Delta Node。Delta 保存本次变化和指向旧链头的 `child_node_p`：

```text
mapping_table[node_id]
          │
          ▼
  ┌──────────────────┐
  │ LeafInsert Delta │  新：插入 (k3, v3)
  └────────┬─────────┘
           ▼
  ┌──────────────────┐
  │ LeafDelete Delta │      删除 (k1, v1)
  └────────┬─────────┘
           ▼
  ┌──────────────────┐
  │    Leaf Base     │  旧：有序 KV 数组
  └──────────────────┘
```

读取并不是简单访问链头，而是解释整条链：较新的 Delta 覆盖较旧的状态，最后以 Base Node 作为底座还原逻辑内容。

Open BwTree 分别为叶子节点和内部节点定义了不同 Delta：

| 层级 | 数据更新 | 结构更新 |
| --- | --- | --- |
| Leaf | `LeafInsertNode`、`LeafDeleteNode` | `LeafSplitNode`、`LeafRemoveNode`、`LeafMergeNode` |
| Inner | `InnerInsertNode`、`InnerDeleteNode` | `InnerSplitNode`、`InnerRemoveNode`、`InnerMergeNode`、`InnerAbortNode` |

这种设计避免了多线程同时改写一个 Base Node，但读路径需要付出 Delta 重放成本。源码把叶子和内部节点的链长阈值都设为 8，达到阈值后尝试 Consolidation：

```cpp
#define INNER_DELTA_CHAIN_LENGTH_THRESHOLD 8
#define LEAF_DELTA_CHAIN_LENGTH_THRESHOLD 8
```

阈值不是越小越好：

- 阈值小，读放大低，但更新更频繁地触发整页重建；
- 阈值大，前台更新便宜，但查找、扫描和缓存局部性变差。

这正是 Bw-Tree 最核心的读写权衡。

## 6. 查找：读取的不是节点，而是节点的逻辑状态

查找从 `root_id` 开始。`Context` 保存搜索键和一条由 `NodeSnapshot(NodeID, node_p)` 组成的路径。每下探一层，都通过 NodeID 重新读取 Mapping Table 的当前链头。

```text
root_id
   │
   ▼
MappingTable[root]
   │  解释 Inner Delta Chain
   ▼
选择 child NodeID
   │
   ▼
MappingTable[child]
   │  必要时右移、帮助 SMO、整合长链
   ▼
目标 Leaf Delta Chain
```

内部节点查找不能只对 Base Node 二分。它需要按从新到旧的顺序处理：

- `InnerInsertNode` 新增的分隔键；
- `InnerDeleteNode` 删除的分隔键；
- `InnerSplitNode` 更新后的 high key 和右兄弟；
- `InnerRemoveNode` 指向的替代节点；
- 最终 Base Node 中的有序分隔键。

### 6.1 high key 与右兄弟为什么仍然需要

节点分裂不是一次原子地同时更新子节点和父节点。线程可能看到“子节点已经分裂，但父节点还没有新分隔键”的中间状态。

Bw-Tree 借用了 B-link Tree 的关键思想：节点维护 high key 和右兄弟 NodeID。如果目标键超过当前节点边界，查找可以向右跳转，而不必等待父节点修复：

```text
父节点仍指向旧范围
          │
          ▼
  [left: key < K] ──next──► [right: key >= K]
          high key = K
```

这使“父节点稍旧”仍然可以被导航信息容忍，也是分阶段 Split 能成立的基础。

### 6.2 读者也可能帮助完成结构变更

普通遍历加载一个 NodeID 后会调用：

1. `FinishPartialSMO()`：完成未结束的 split/merge；
2. `TryConsolidateNode()`：链过长时尝试整合；
3. `AdjustNodeSize()`：节点过大或过小时尝试分裂/合并。

因此，Bw-Tree 的读写边界并不绝对：访问者发现未完成结构变更时，可以帮助推进系统状态。帮助机制避免了必须由原线程独占完成整个 SMO，但也让遍历协议明显复杂于普通 B+Tree。

## 7. 插入与删除：CAS 是提交点

### 7.1 插入流程

`Insert(key, value)` 的主流程可以抽象为：

```text
进入 Epoch
   │
   ▼
从根遍历到目标叶子，检查 (key, value) 是否已存在
   │
   ▼
读取 NodeID 对应的当前链头 old_head
   │
   ▼
构造 LeafInsertNode(key, value, child = old_head)
   │
   ▼
CAS MappingTable[NodeID]: old_head -> insert_delta
   ├── 成功：插入提交
   └── 失败：释放候选 Delta，从根重试
   │
   ▼
离开 Epoch
```

候选 Delta 在 CAS 前只对当前线程可见，因此可以完整构造后一次发布。CAS 成功意味着该 Delta 成为链头；CAS 失败说明链头已被其他线程替换，本线程不能把 Delta 接在过期版本上，只能重试。

这个实现允许一个 key 对应多个 value，但相同 `(key, value)` 不会重复插入。它保证的是单次索引操作的原子性，不是事务隔离。

### 7.2 删除流程

删除与插入对称：先确认目标存在，再构造 `LeafDeleteNode`，最后 CAS 发布。它不会立刻重写 Base Node，也不会同步释放被覆盖的旧链。

```text
Base:             (k1,v1) (k2,v2)
Delete Delta:     delete (k1,v1)
逻辑读取结果:              (k2,v2)
```

将修改表达为不可变 Delta 有三个直接好处：

- 失败的并发更新不会污染共享页面；
- 发布动作缩小为一个机器字 CAS；
- 旧读者仍可沿旧链安全完成。

代价则是：删除并不立刻回收空间，后续必须依赖 Consolidation 和 Epoch GC。

## 8. Consolidation：把版本链折叠回 Base Node

当链深达到 8，`TryConsolidateNode()` 调用 `ConsolidateNode()`。其本质不是“修改原 Base Node”，而是离线构造一个全新的 Base Node：

```text
旧状态
  Delta N -> Delta N-1 -> ... -> Base A

重放 Delta，生成有序逻辑内容
                  │
                  ▼
              New Base B

CAS MappingTable[id]: old chain head -> Base B
  ├── 成功：旧链进入 Epoch 回收队列
  └── 失败：其他线程先更新，丢弃 Base B；原链继续有效
```

叶子节点通过 `CollectAllValuesOnLeaf()` 重放插入和删除，内部节点通过 `CollectAllSepsOnInner()` 收集有效分隔键。源码使用小型有序集合、哈希集合和 Bloom Filter 辅助判重与过滤。

Consolidation 有几个值得注意的性质：

1. **它是机会性的。** CAS 失败不影响索引正确性，只说明有人先发布了更新。
2. **它缩短读路径。** 多次指针追逐和 Delta 判断被折叠成连续有序数组。
3. **它放大写入。** 少量记录的变化最终可能触发整个逻辑节点的复制。
4. **它创造大量短命对象。** 旧链不能立即释放，内存回收性能会直接影响系统稳定性。

源码还把内部节点和叶子节点的上下限设置为 128/32：

```cpp
#define INNER_NODE_SIZE_UPPER_THRESHOLD 128
#define INNER_NODE_SIZE_LOWER_THRESHOLD 32
#define LEAF_NODE_SIZE_UPPER_THRESHOLD 128
#define LEAF_NODE_SIZE_LOWER_THRESHOLD 32
```

整合得到 Base Node 后，代码根据逻辑大小判断是否继续触发 split 或 remove/merge。

## 9. Split：先让新边界可见，再修父节点

传统 B+Tree 分裂常常需要同时锁住子页和父页。Bw-Tree 把分裂拆成可观察、可恢复的多个阶段。

以叶子节点为例：

1. 整合当前节点，确定分裂键；
2. 构造右半节点，为它分配新 `NodeID`；
3. 把右节点安装进 Mapping Table 的空表项；
4. 在左节点上 CAS 发布 `LeafSplitNode`，更新 high key 和右兄弟；
5. 在父节点上发布 `InnerInsertNode`，补充分隔键；
6. 如果是根分裂，则构造新根并 CAS 切换 `root_id`。

```text
阶段 1：父节点尚未更新

 Parent: ... -> Left(old range)
                  │
                  ▼
       Left(new range) ──next──► Right
         high key = K             low key = K

阶段 2：帮助完成父节点更新

 Parent: ... -> Left | K -> Right
```

阶段 1 已经足以保证导航正确：落入右半区间的线程会依据 high key 向右跳转。父节点更新因此可以稍后由当前线程或其他观察到该 Split Delta 的线程完成。

这体现了 Bw-Tree SMO 的核心思想：

> 不要求一个线程在一个临界区内原子完成全部结构修改，而是让每个中间状态都可解释，并允许其他线程帮助收敛。

根节点是例外，因为根没有父节点。实现通过 `root_id.compare_exchange_strong(old_root, new_root)` 切换逻辑根；失败说明另一个线程已率先改变根，当前线程需要根据新状态重试或帮助完成。

## 10. Merge 与 Remove：最难的并发路径

节点低于下限后，代码可能把它移除并合并到左兄弟。相比 split，merge 更难处理：

- Split 即使父节点暂时缺少新分隔键，high key 和右链仍能找到新节点；
- Merge 若基于过期父快照判断“分隔键已经不存在”，可能把尚未完成的合并误判为已完成；
- 被移除 NodeID 还可能被旧读者持有，不能立即复用。

Open BwTree 为此引入了论文之外值得重点关注的 `InnerAbortNode` 协议。

### 10.1 ABORT 协议

合并路径大致如下：

```text
1. 在父节点 CAS 发布 InnerAbortNode
               │
               ▼
2. 阻止基于旧父快照的访问/更新继续提交
               │
               ▼
3. 在待删除节点发布 LeafRemoveNode / InnerRemoveNode
               │
               ▼
4. 从父节点移除 ABORT，恢复访问
               │
               ▼
5. 把内容合并到左兄弟，并在父节点发布 InnerDeleteNode
```

`PostAbortOnParent()` 的关键代码是：

```cpp
InnerAbortNode *abort_node_p = new InnerAbortNode{parent_node_p};

bool ret = InstallNodeToReplace(parent_node_id,
                                abort_node_p,
                                parent_node_p);
```

ABORT 的目的不是表示用户事务回滚，而是暂时冻结某个父节点的可提交快照，避免 remove 与父节点并发 split/merge 交错后产生错误判断。

### 10.2 进展保证的代价

这个协议修补了正确性窗口，却引入了新的活性问题：如果线程成功发布 ABORT 后永久停顿，其他线程可能持续遇到 ABORT，无法让父节点恢复正常状态。仓库 [README](https://github.com/wangziqi2013/BwTree/blob/09b7354d419513ac5648980e875e1a3246be396a/README.md) 将此列为已知问题。

因此，对该实现更准确的描述是：

- 普通数据更新不依赖互斥 latch，以 CAS 竞争发布；
- 多数未完成 SMO 可以由其他线程帮助推进；
- 但 ABORT 的撤销依赖发布者，当前实现并不满足 wait-free，且在发布者失效时还会削弱严格的 lock-free 进展性质。

这也是阅读并发算法源码时必须区分的三个概念：

| 概念 | 含义 |
| --- | --- |
| Latch-free | 实现没有使用传统页面 latch；更多是工程结构描述 |
| Lock-free | 系统整体持续有操作取得进展，不保证每个线程成功 |
| Wait-free | 每个操作都能在有限步骤内完成，保证最强 |

“代码里没有 mutex”不足以自动证明后两者。

## 11. Epoch GC：旧版本何时才能释放

CAS 替换 Mapping Table 表项，只意味着旧链不再接受新访问，不意味着旧链已经无人引用。一个读线程可能在 CAS 前取得旧指针，此时立刻 `delete` 会造成 use-after-free。

Bw-Tree 因而需要两阶段删除：

```text
逻辑删除：CAS 让旧链从 Mapping Table 不可达
                         │
                         ▼
延迟回收：等待所有可能持有旧指针的线程跨过安全 Epoch
                         │
                         ▼
                    释放旧链内存
```

### 11.1 当前启用的是哪套 Epoch 机制

源码保留了新旧两套 Epoch 实现，但 `USE_OLD_EPOCH` 默认被注释。当前路径由 `BwTreeBase` 维护全局 epoch 和每线程 GC 元数据：

- 工作线程调用 `RegisterThread()` 获得 `gc_id`；
- `JoinEpoch()` 与 `LeaveEpoch()` 更新该线程的 `last_active_epoch`；
- 被替换的链加入当前线程的垃圾链表，并记录删除 epoch；
- 某线程积累超过 1024 个垃圾节点时，`PerformGC()` 计算所有线程的最小活跃 epoch；
- 删除 epoch 小于安全边界的对象才可以释放。

源码注释明确要求线程在 BwTree 实例创建前完成注册，并指出这种预分配元数据的方式更适合启动时线程数固定的线程池：

```cpp
static void RegisterThread() {
  gc_id = total_thread_num.fetch_add(1);
}
```

每线程 GC 上下文还做了 cache-line 对齐，以减少不同线程更新活跃 epoch 时的 false sharing。

### 11.2 Epoch 的工程约束

Epoch 回收没有逐对象引用计数，读路径较轻，但使用者必须遵守严格协议：

- 未注册线程不能随意访问树；
- 线程退出或长期停顿时要正确更新状态；
- 所有从共享结构取出的裸指针都只能在受保护区间内使用；
- 一个迟迟不推进活跃 epoch 的线程会延迟全局回收，造成垃圾链堆积。

`LeafRemoveNode` 和 `InnerRemoveNode` 还携带 `removed_id`，目的是等安全 epoch 后再回收 NodeID。不过，如前文所述，当前 `InvalidateNodeID()` 并未真正把 ID 压回空闲栈，这条复用链路并不完整。

## 12. Iterator：逻辑叶子快照，不是事务快照

Open BwTree 提供 `Begin()`、`Begin(start_key)` 和双向移动的 `ForwardIterator`。迭代器不能长期保存一个可能被回收的叶子链指针，因此它在 Epoch 内把当前逻辑叶子的内容物化到 `IteratorContext` 中，再退出受保护区间。

跨越页面边界时，迭代器使用 high key 和 `LowerBound` 重新定位，而不是假设旧物理页面和兄弟指针永远有效。这也用于处理并发 merge 可能造成的边界重复。

需要强调：

> 叶子内容被物化，只保证迭代器当前局部状态的内存安全，不等于整个扫描具有一致性快照。

如果扫描过程中其他线程继续更新或分裂页面，迭代器跨页时可能观察到更新后的状态。数据库若需要 repeatable read、snapshot isolation 或 serializable，必须在 Bw-Tree 之上结合 MVCC、时间戳或事务层实现。Open BwTree 自己只承诺索引操作原子性，不承担数据库隔离职责。

## 13. 从一条写入看完整生命周期

把前面的机制串起来，一条记录从插入到旧版本释放会经历：

```text
Insert(k, v)
   │
   ├─ 1. Traverse：找到叶子 NodeID 和当前链头
   │
   ├─ 2. Build：构造不可变 LeafInsertNode
   │
   ├─ 3. Publish：CAS Mapping Table 表项
   │          ├─ fail -> 丢弃候选对象，从根重试
   │          └─ success -> 操作线性化
   │
   ├─ 4. Consolidate：链深达到 8 时重建 Base Node
   │
   ├─ 5. Resize：超过 128 时发布 Split Delta
   │
   ├─ 6. Help Along：把新分隔键补进父节点
   │
   └─ 7. Reclaim：旧链跨过安全 Epoch 后释放
```

这条链路展示了 Bw-Tree 的真正设计中心：它不是取消了同步，而是把同步集中到**版本发布**；不是取消了页面重写，而是把重写延迟到**整合**；不是让结构变更一步完成，而是把它改写成**可解释、可帮助的状态机**。

## 14. 工程实现中的其他细节

### 14.1 Header-only 的泛型索引

主体实现集中在近万行的 [`src/bwtree.h`](https://github.com/wangziqi2013/BwTree/blob/09b7354d419513ac5648980e875e1a3246be396a/src/bwtree.h)，通过模板参数接收：

- `KeyType` / `ValueType`；
- key comparator 与 equality checker；
- key/value hash function。

这让算法可以适配自定义键值类型，但比较、相等与哈希语义必须一致，否则 Delta 判重、集合过滤和树顺序可能产生不一致。

### 14.2 辅助数据结构

仓库还包含：

- [`atomic_stack.h`](https://github.com/wangziqi2013/BwTree/blob/09b7354d419513ac5648980e875e1a3246be396a/src/atomic_stack.h)：NodeID 空闲栈；
- [`bloom_filter.h`](https://github.com/wangziqi2013/BwTree/blob/09b7354d419513ac5648980e875e1a3246be396a/src/bloom_filter.h)：整合过程中的快速过滤；
- [`sorted_small_set.h`](https://github.com/wangziqi2013/BwTree/blob/09b7354d419513ac5648980e875e1a3246be396a/src/sorted_small_set.h)：针对小集合的有序操作。

这些组件说明，Delta Chain 的成本不只来自指针跳转，还包括重放时的去重、删除遮蔽和临时集合构造。评估 Bw-Tree 时不能只测 CAS 吞吐，还应测不同链长下的点查、范围扫描、整合频率和内存峰值。

### 14.3 测试与构建基线偏旧

仓库提供基本功能、混合高并发、压力、迭代器和 benchmark 测试，这是理解协议的重要入口。但构建文件仍硬编码 `g++-5`，使用 C++11、PAPI、jemalloc 和特定平台参数。

这说明它更像 2018 年前后的研究/实验代码快照。迁移到现代 Linux 或 macOS 时，至少需要重新处理：

- 编译器版本和原子指令支持；
- PAPI 与 jemalloc 可选依赖；
- sanitizer 和现代线程检测工具；
- 构建系统、持续集成与跨平台配置。

## 15. 正确性应如何理解

分析 Bw-Tree 不能只记住几个类名，还要抓住三个不变量。

### 15.1 发布不变量

一个新版本只有在 Mapping Table CAS 成功后才可见。CAS 前构造过程不修改共享旧版本；CAS 失败者不能继续提交基于旧链头的结果。

### 15.2 导航不变量

即使父节点尚未反映子节点 split，high key 和右兄弟仍必须把搜索引导到正确键范围。中间 SMO 状态必须能够被遍历代码识别并帮助完成。

### 15.3 生命周期不变量

从 Mapping Table 移除不等于立即释放。任何可能被旧快照引用的链，都必须等所有相关读者越过安全 epoch 后才能析构。

### 15.4 四种“正确”不能合并成一句无锁

Bw-Tree 讨论中最常见的概念滑移，是从“Mapping Table CAS 成功”直接跳到“数据库事务已经安全提交”。实际上至少有四层相互独立的证明义务：

| 层次 | 需要证明什么 | Open BwTree 当前覆盖 |
|------|--------------|----------------------|
| 操作线性化 | 一次 Insert/Delete 在哪个瞬间对并发读者生效 | 主要由 Mapping Table CAS 提供 |
| 结构可导航 | Split/Merge 中间态下，搜索仍能到达正确 Key Range | 依赖 high key、右兄弟与 help-along |
| 内存安全 | 旧链仍被读者持有时不会释放或复用 | 依赖 Epoch 注册、退出与延迟回收 |
| 事务与持久化 | 多键原子性、隔离、WAL 顺序与 Crash Recovery | 这份内存实现不提供 |

因此，CAS 是单个索引操作的候选线性化点，却不是事务 Commit Record，也不证明多个索引更新能够原子提交。Epoch 只回答“何时可以释放旧对象”，不回答“事务应该看到哪个版本”。原始论文还包含 Log-Structured Storage Manager，而 Open BwTree 代码聚焦内存索引；把两者分开，才能避免将论文系统的持久化能力错误归因于当前仓库。

生产接入时，数据库事务层还必须决定：唯一性冲突在 CAS 前还是后判定、Abort 如何撤销已发布 Delta、Checkpoint 如何固定 Mapping Table 与 NodeID、恢复时如何重新建立物理地址，以及 Secondary Index 与 Base Table 如何保持提交一致。这些问题并不会因为索引内部 latch-free 而消失。

如果要形式化验证这个实现，CAS 本身反而是最简单的部分。更难的是证明：

- 每种 Delta 的重放优先级一致；
- split/merge 的所有中间状态都可导航；
- 帮助完成不会重复应用或遗漏父分隔键；
- Remove Delta 的重定向在 NodeID 回收前一直有效；
- Epoch 注册、退出和对象析构覆盖所有裸指针使用区间。

## 16. 局限性与生产化清单

这份 Open BwTree 源码非常适合学习，但若要进入生产系统，需要正视以下问题。

### 16.1 当前实现已知限制

| 问题 | 影响 |
| --- | --- |
| Mapping Table 固定为 `1 << 20` | 索引规模存在硬上限 |
| NodeID 复用代码未启用 | 长期 churn 会持续消耗 ID 空间 |
| ABORT 依赖发布线程撤销 | 线程永久停顿可能影响系统进展 |
| 线程须提前注册 | 不适合随意创建/销毁的动态线程模型 |
| Epoch 被慢线程拖延 | 旧链堆积，内存峰值上升 |
| 无 WAL、恢复和持久化页面层 | 不是完整数据库存储引擎 |
| 构建依赖和工具链较旧 | 需要现代化迁移与重新验证 |
| 只提供操作原子性 | 事务隔离需要上层系统实现 |

### 16.2 生产化时应补充什么

1. **容量管理**：动态扩展或分段 Mapping Table，可靠复用 NodeID，并防止 ABA。
2. **内存治理**：限制 Delta 链、提供 GC backpressure，监控各线程 epoch 和待回收字节数。
3. **故障活性**：重新设计 ABORT owner 失效后的帮助/接管协议。
4. **事务集成**：明确 key/value 的版本语义，与 MVCC、唯一性检查和锁管理协作。
5. **持久化协议**：定义 WAL 顺序、page image、崩溃恢复和 checkpoint；不能只把内存指针写盘。
6. **可观测性**：统计 CAS 冲突、重试次数、链长分布、整合耗时、SMO 帮助次数和 GC 滞后。
7. **系统化验证**：加入 ASan、UBSan、TSan、长时间随机并发测试和故障注入。

## 17. 如何阅读这份源码

建议按“正常数据路径 → 维护路径 → 生命周期”的顺序阅读，而不是从近万行头文件第一行顺序向下：

1. 先看 `NodeType`、`BaseNode`、`DeltaNode`，建立节点类型图；
2. 再看 `InstallNodeToReplace()`，确认统一发布点；
3. 跟踪 `Insert()`、`Delete()` 和 `GetValue()`，理解正常路径；
4. 阅读 `Traverse()` 与内部/叶子链解释逻辑；
5. 阅读 `TryConsolidateNode()` 和两个 Collect 函数；
6. 进入 `AdjustNodeSize()`、split、remove、merge 与 `FinishPartialSMO()`；
7. 最后阅读 `EpochManager`、`AddGarbageNode()`、`PerformGC()` 和 Iterator。

调试时建议重点记录以下状态，而不是只打印 key：

```text
NodeID
当前 Mapping Table 链头地址
Delta 类型与 depth
low key / high key / next NodeID
CAS expected / actual
Context 中的父子快照
线程 gc_id / global epoch / last_active_epoch
```

## 18. 总结

Bw-Tree 的价值不只在于提供一种“更快的 B+Tree”，而在于展示了一种通用并发设计方法：

1. 用稳定逻辑 ID 隔离对象身份与物理地址；
2. 用不可变 Delta 表达更新，避免原地写共享页面；
3. 用单点 CAS 发布版本，把冲突变成可检测的失败；
4. 用 high key、右兄弟和 help-along 把结构变更拆成可恢复阶段；
5. 用 Epoch 把逻辑删除与物理释放解耦。

但它并没有让复杂性消失。读放大、Consolidation、SMO 状态机、ABORT 活性问题、NodeID 生命周期和 Epoch GC，共同构成了实现难度。

从 Open BwTree 源码得到的最重要结论是：

> Mapping Table + Delta Chain 让 B+Tree 的共享原地修改转化为版本发布问题；真正决定实现能否落地的，则是结构变更与内存生命周期能否在所有并发交错下保持正确和持续进展。

## 参考资料

- [The Bw-Tree: A B-tree for New Hardware](https://www.microsoft.com/research/publication/the-bw-tree-a-b-tree-for-new-hardware/)
- [Open BwTree 源码，commit 09b7354](https://github.com/wangziqi2013/BwTree/tree/09b7354d419513ac5648980e875e1a3246be396a)
- [Open BwTree README](https://github.com/wangziqi2013/BwTree/blob/09b7354d419513ac5648980e875e1a3246be396a/README.md)
- [BwTree 核心实现：src/bwtree.h](https://github.com/wangziqi2013/BwTree/blob/09b7354d419513ac5648980e875e1a3246be396a/src/bwtree.h)
