---
title: "数据库论文精读：现代硬件、元数据与云存储"
slug: "database-paper-reading-notes"
date: 2026-08-23T20:21:00+08:00
lastmod: 2026-08-23T20:46:00+08:00
categories:
  - 数据库
tags:
  - 数据库
  - 论文
  - 存储引擎
  - 索引
  - 云原生
  - 分布式存储
  - 文件系统
description: "持续更新的数据库与存储系统论文精读笔记：从 Bf-Tree、HopsFS、PolarFS 到 Tectonic，理解现代硬件、分布式元数据与资源池化如何重新定义系统边界。"
draft: false
toc: true
---

这是一篇持续更新的数据库论文精读笔记。

我不准备把它写成论文摘要的集合。摘要只能回答“作者做了什么”，真正值得反复思考的是：**系统原来被什么约束，硬件变化后哪个旧假设失效了，作者移动了哪条软件边界，又为此引入了什么复杂度。**

当前记录覆盖四篇看起来相距很远的论文：Bf-Tree 讨论单机超内存索引，HopsFS 与 Tectonic 讨论文件系统元数据和资源池化，PolarFS 讨论云数据库共享存储。它们的共同点却很明确：现代 SSD、RDMA、多核 CPU 和分布式数据库不会自动转化成系统能力，软件必须重新选择缓存粒度、状态归属、I/O 路径和串行化边界。

## 阅读索引

| 方向 | 论文 | 会议 / 年份 | 状态 | 我关注的核心问题 |
|---|---|---:|---|---|
| 数据结构 / 范围索引 | [Bf-Tree](#bf-tree现代硬件上的超内存范围索引) | PVLDB 2024 | 精读 | 缓存页是否必须与磁盘页等大？ |
| 文件系统 / 元数据 | [HopsFS](#hopsfs用-newsql-扩展层级文件系统元数据) | FAST 2017 | 精读 | 能否把 NameNode 的内存状态变成可水平扩展的数据库事务？ |
| 工业架构 / 分布式存储 | [PolarFS](#polarfs面向云数据库的共享存储) | PVLDB 2018 | 精读 | 如何把共享存储的远程 I/O 延迟压到接近本地盘？ |
| 文件系统 / 资源池化 | [Tectonic](#tectonic从专用存储烟囱走向-eb-级统一底座) | FAST 2021 | 精读 | 如何让 Blob 与数仓共享同一个 EB 级存储池，同时保持隔离？ |

这里的阅读状态分为四级：**待读、粗读、精读、源码 / 复现**。后续增加论文时，先更新索引，再在对应分类下补充正文；对尚未确认的判断保留为问题，不把推测写成事实。

---

## 数据结构：现代硬件上的范围索引

### Bf-Tree：现代硬件上的超内存范围索引

#### 论文信息

- 论文：[Bf-Tree: A Modern Read-Write-Optimized Concurrent Larger-Than-Memory Range Index](https://www.vldb.org/pvldb/vol17/p3442-hao.pdf)
- 作者：Xiangpeng Hao（University of Wisconsin–Madison）、Badrish Chandramouli（Microsoft Research）
- 出处：PVLDB 17(11)，2024
- DOI：[10.14778/3681954.3682012](https://doi.org/10.14778/3681954.3682012)
- 实现：[microsoft/bf-tree](https://github.com/microsoft/bf-tree)
- 设计文档：[Bf-Tree Design Docs](https://github.com/XiangpengHao/bf-tree-docs)

#### 为什么读这篇论文

当数据超过内存容量时，B+ Tree 和 LSM-tree 代表了两种典型取舍：

- B+ Tree 原地更新，点查与范围扫描直接，但修改少量记录也可能产生整页 I/O；
- LSM-tree 把随机写转成顺序写和批量归并，但需要承担读放大、空间放大与后台 Compaction；
- 常规 Buffer Pool 以固定大小的页为缓存单位，即使应用只访问页内一条记录，也要把完整磁盘页留在内存中。

过去，磁盘随机 I/O 极其昂贵，“让缓存页与磁盘页保持同样大小”是合理选择。现代 NVMe SSD 缩短了随机与顺序访问之间的差距，CPU、内存带宽和缓存效率开始进入主要成本。Bf-Tree 真正追问的不是如何再造一种 B+ Tree，而是：

> **磁盘页仍然适合做持久化和 I/O 单位，但它是否还必须同时充当内存缓存单位？**

这是整篇论文最值得记住的问题。

#### 核心思想：磁盘页与缓存页不必一一对应

Bf-Tree 仍然保留 B+ Tree 适合范围查询的有序结构，但把内存中的缓存对象与磁盘页解耦。它在内存里使用可变长的 **mini-page**，只缓存当前真正需要的记录或区间。

一个 mini-page 可以承担三种角色：

1. **记录级缓存**：点查命中的少量记录不必占用完整磁盘页；
2. **近期写缓冲**：更新先进入 mini-page，多个更新可以在之后合并刷盘；
3. **区间缺口缓存**：记录某段范围已经从磁盘读过，避免对“不存在的键”重复发起 I/O。

因此，Bf-Tree 的本质并不是“在每层树节点上增加 Buffer”，也不是把 Bw-Tree 的 Delta Chain 搬到磁盘，而是改变 Buffer Pool 的基本粒度：

```text
传统 Buffer Pool
  一个缓存页  <------------>  一个磁盘页

Bf-Tree
  多个可变长 mini-page  ----->  一个或多个磁盘页中的局部记录 / 区间
```

这个变化同时影响缓存命中率、写合并、置换算法和并发控制，所以它不只是一个局部优化，而是一套新的索引与缓存协同设计。

#### 可变长 Buffer Pool 如何工作

Bf-Tree 的 Buffer Pool 是一段循环空间，mini-page 可以在其中增长、收缩和被回收。固定页缓存常用 Clock 或 LRU 近似算法，但可变长对象带来一个新问题：淘汰一个对象不一定能立即得到足够大的连续空间。

论文采用类似 Second-Chance 的置换策略，并通过近似 RCU 的方式更新 mini-page：分配新空间、复制有效内容，再把旧空间放回 Free List。它避免在原位置上复杂地扩容，却把空间管理和碎片控制变成实现中的关键部分。

我更倾向于把这理解为一种 **Index-aware Buffer Manager**：索引知道页内哪些记录是热点、哪些更新尚未合并，因而能做传统通用 Buffer Pool 无法完成的细粒度决策。代价是索引、缓存与持久化格式之间的耦合更深。

#### 读路径：先查 mini-page，必要时再读磁盘页

一次点查大致经历以下过程：

```text
Key
  -> 沿 Bf-Tree 定位目标节点
  -> 检查对应 mini-page
       -> 已缓存记录：直接返回
       -> 已缓存区间缺口：确认记录不存在
       -> 信息不足：读取磁盘页
  -> 将需要的记录或范围提升到 mini-page
```

这里的收益是缓存可以容纳更多“有效记录”，而不是更多“完整页”。它特别适合记录较小、访问存在局部热点、数据规模明显超过内存的负载。

范围扫描则需要在有序叶层上合并 mini-page 中的新版本与磁盘页中的稳定版本。Bf-Tree 没有丢掉 B+ Tree 的有序性，因此它比需要跨层归并的 LSM-tree 更自然地支持范围查询。

#### 写路径：细粒度变脏，批量落盘

更新首先进入目标节点对应的 mini-page。此时变脏的是一条记录或一小段区域，而不是整个 4KB 页。当缓存压力、更新量或检查点要求触发刷盘时，系统再把内存里的修改合并回磁盘页。

这并没有让“整页原地写”消失；它减少的是每次逻辑更新立刻触发整页写入的概率。多个落在同一页上的更新可以合并，一次磁盘写承担更多有效工作。

因此更准确的表达是：

> Bf-Tree 通过细粒度缓存和延迟合并降低写放大，而不是把 B+ Tree 变成了纯追加结构。

#### 并发、持久化与恢复

Bf-Tree **不是无锁索引**。内部节点使用 Optimistic Latch Coupling：读者通过版本号判断遍历期间节点是否发生变化，写者获取排他锁。它减少了读路径上的锁开销，但并不等于所有更新都只依赖 CAS，也不等于系统是 lock-free。

原型当前依赖操作系统线程交错 I/O，官方设计文档把更完善的用户态异步 I/O 列为后续方向。因此不能把 `io_uring` 或 `libaio` 描述成论文已有实现。

在持久化方面，系统提供 WAL、快照和恢复流程：

- 检查点可通过重放 WAL 构造离线快照；
- 在线快照需要把脏 mini-page 合并到持久化状态；
- 恢复时先加载快照，再重放快照之后的 WAL。

这说明细粒度缓存并没有绕开恢复问题。相反，缓存对象与磁盘页不再同构以后，如何构造一致快照、何时合并脏数据，会成为工程复杂度的重要来源。

#### 论文证据，以及数字的边界

在论文给定的硬件、数据集和约百字节小记录负载下，作者报告：

- 范围扫描吞吐约为 RocksDB 的 **2.5 倍**；
- 写吞吐约为对比 B-Tree 的 **6 倍**；
- 点查吞吐约为对比 B-Tree 与 LSM-tree 的 **2 倍**。

这些数字用于证明设计在目标负载上成立，不能直接外推成“Bf-Tree 对所有 B+ Tree 和 LSM-tree 都更快”。结果会受到记录大小、读写比例、热点分布、内存比例、SSD 随机写能力和后台任务设置影响。

论文与设计文档也明确暴露了边界：

- 对小记录优化最明显；大记录会稀释 mini-page 的收益；
- 它更依赖现代 SSD 较强的随机 4KB 写能力；
- 可变长 Buffer Pool 比固定页缓存更复杂；
- 原地覆盖磁盘页仍可能放大 SSD 内部垃圾回收；
- 当前的提升与淘汰策略较简单，未必能适应所有访问分布；
- 公开实现是研究原型，距离生产系统仍有恢复验证、监控、容量治理和长期稳定性工作。

#### 与 B+ Tree、LSM-tree、Bw-Tree 和 LeanStore 的关系

| 设计 | 主要优化对象 | 内存 / 磁盘映射 | 写入方式 | 典型代价 |
|---|---|---|---|---|
| B+ Tree | 有序查找与范围扫描 | 固定缓存页对应磁盘页 | 原地页更新 | 小更新可能造成整页 I/O |
| LSM-tree | 写吞吐与顺序 I/O | MemTable + 多层 SSTable | 追加并 Compaction | 读、空间与后台归并放大 |
| Bw-Tree | 内存并发访问 | Mapping Table + Delta Chain | CAS 安装 Delta | 链合并、间接寻址及超内存管理复杂 |
| LeanStore | 通用 Buffer Manager 的寻址成本 | Pointer Swizzling | B-Tree 风格更新 | 页面置换与指针状态管理复杂 |
| Bf-Tree | 缓存与变脏粒度 | 可变长 mini-page 映射局部记录 | mini-page 聚合后合并磁盘页 | 变长空间管理与恢复复杂 |

LeanStore 和 Bf-Tree 经常会被放在一起讨论，但它们并非简单替代关系。LeanStore 用 Pointer Swizzling 降低页表查找和间接寻址成本；Bf-Tree 则主要改变缓存对象的粒度。一个优化“如何找到已缓存页”，另一个优化“到底应该缓存多少页内数据”。

至于 RocksDB、TiKV 是否“吸收了 Bf-Tree 思想”，目前不应直接下结论。跳表、无锁读、Block Cache 和 Write Buffer Manager 在这些系统中有各自更早的演化历史。更有价值的研究方式是逐项比较机制、提交时间与解决的问题，而不是看到 CAS 或分层缓存就建立继承关系。

#### 我的判断

Bf-Tree 最有价值的地方，不是给出了一个宣称同时胜过 B+ Tree 和 LSM-tree 的最终答案，而是把一个长期默认的系统边界重新打开：**I/O 页、持久化页和缓存页不一定是同一个抽象。**

这种思路对数据库内核很有启发。我们经常围绕 Block Cache 命中率调参数，却很少追问“Block 是否就是正确的缓存单位”。在列存、向量索引、对象存储缓存中，同样存在类似问题：查询真正需要的是列段、Zone Map、Posting List 的局部，还是整个远程对象？

但它也提醒我：抽象越贴近 workload，效率通常越高，系统复杂度也越容易从通用层转移到专用层。判断一个新结构能否进入生产，不能只看前台吞吐，还要看快照、恢复、空间碎片、写入尾延迟、SSD 寿命和运维可观测性。

#### 待继续验证

- 对照源码梳理 mini-page 的状态机、淘汰路径与刷盘触发条件；
- 用相同硬件与数据分布比较 Bf-Tree、LeanStore 和 RocksDB，而不是只比较论文数字；
- 分析记录大小变化后，细粒度缓存收益与元数据开销的交叉点；
- 研究异步 I/O 加入后，线程模型、latch 持有时间和请求合并策略会怎样变化；
- 将这一思想映射到列存与向量检索：缓存粒度应由物理页决定，还是由查询语义决定？

---

## 工业架构：从元数据扩展到共享存储

### HopsFS：用 NewSQL 扩展层级文件系统元数据

#### 论文信息

- 论文：[HopsFS: Scaling Hierarchical File System Metadata Using NewSQL Databases](https://www.usenix.org/system/files/conference/fast17/fast17-niazi.pdf)
- 作者：Salman Niazi、Mahmoud Ismail、Seif Haridi、Jim Dowling、Steffen Grohsschmiedt、Mikael Ronström
- 出处：[FAST 2017](https://www.usenix.org/conference/fast17/technical-sessions/presentation/niazi)，89–104 页
- 实现：[hopshadoop/hops](https://github.com/hopshadoop/hops)

#### 为什么读这篇论文

HDFS 把完整命名空间、文件到 Block 的映射以及 DataNode 状态放在 Active NameNode 内存中。这给系统带来了两个很鲜明的特点：

- 路径解析与元数据操作是本机内存访问，单次请求很快；
- 元数据容量、RPC 吞吐和全局锁最终受一台 NameNode 的内存与 CPU 限制。

HDFS Federation 可以把命名空间静态拆到多个 NameNode，但每个子命名空间仍有自己的容量上限，跨命名空间操作和数据放置也更难统一管理。

HopsFS 提出了一个很“数据库化”的问题：

> **如果 NewSQL 已经能提供分片、复制、行锁和 ACID 事务，文件系统是否还需要在 NameNode 内重新维护一套单机元数据状态机？**

它保留 HDFS 的 DataNode 与 Block 数据面，将文件系统元数据搬进 MySQL Cluster NDB，让多个 NameNode 并发执行元数据事务。这里真正激进的地方，不只是把内存对象写进数据库，而是把 HDFS 的全局临界区重新表达成一组可分片的关系数据和细粒度事务。

#### 整体架构：NameNode 扩展计算，NDB 承载状态

```text
                       HDFS Clients
                            |
             +--------------+--------------+
             |              |              |
        NameNode 1      NameNode 2      NameNode N
        元数据逻辑       元数据逻辑       元数据逻辑
             \              |              /
              +-------------+-------------+
                            |
                  MySQL Cluster NDB
             分片元数据 + 行锁 + ACID 事务
                            |
                       HDFS DataNodes
                    实际文件 Block 数据
```

多个 NameNode 可以同时服务客户端，命名空间的权威状态位于 NDB。某个 NameNode 失败后，客户端把操作重试到其他实例，不需要先等待 Standby 加载完整 FSImage 和 EditLog。

不过“NameNode 无状态”需要更精确地理解：普通请求所需的持久化元数据不再由单个 NameNode 独占，但 NameNode 仍保留缓存、事务内快照以及正在执行的 Subtree Operation 等临时状态；Block Replication Manager 等后台职责也仍需要 Leader Election。这里消除的是**单点权威元数据状态**，不是让进程内部完全没有状态。

#### 把目录树映射成关系数据

HopsFS 不保存完整路径字符串，而是把路径拆成逐级 INode：

```text
/user/foo/data.parquet

INode(parent_id=/,    name=user)
INode(parent_id=user, name=foo)
INode(parent_id=foo,  name=data.parquet)
```

核心元数据被拆到多张表中：

- `INode` 保存文件或目录及其 `parent_id`；
- Block、Replica、Checksum 等表保存文件关联数据；
- Under-replicated、Corrupted、Pending Replication 等表表达 Block 生命周期状态。

这种模型的意义不只是“关系表容易查询”。文件系统操作最终会变成主键查询、批量主键查询和 Partition-pruned Index Scan，能否避免全分片扫描直接决定了系统是否可以扩展。

#### 分片策略：把常见事务尽量留在一个分片

HopsFS 主要使用两种局部性：

1. 同一目录的直接子项按父目录 INode ID 分片，使 `ls /dir` 可以在一个数据库分片上完成 Partition-pruned Scan；
2. 一个文件关联的 Block、Replica、Checksum 等元数据按文件 INode ID 分片，使常见文件操作尽量成为单分片事务。

如果严格把所有顶层目录都按根 INode 分片，根分片会立刻成为热点。HopsFS 因此缓存不可变的根 INode，并对靠近根的若干层使用可配置的伪随机分片。代价也很直接：分散得越彻底，顶层目录的 `ls` 和 `move` 越可能访问更多分片。

这与数据库 Sharding 的经验完全一致：**分片键不是静态数据建模问题，而是对未来事务边界的预测。** 父目录 ID 优化目录枚举，文件 INode ID 优化文件关联数据读取，但不存在同时让所有层级操作都局部化的完美键。

#### 普通 INode 事务：用统一锁顺序恢复 HDFS 语义

NDB 当时提供的最强隔离级别是 Read Committed，弱于 HDFS 在全局锁下表现出的串行语义。HopsFS 因此不能简单地把 SQL 发给数据库，而是在应用层实现并发控制：

- 路径解析从根到叶执行；
- 涉及多条路径时使用统一的 Left-ordered Depth-first 顺序；
- 事务开始时一次性以可能需要的最强模式取得锁，避免中途从读锁升级为写锁；
- 数据首先进入 Per-transaction Cache，逻辑操作完成后再统一提交。

这样，不冲突的文件或目录操作可以在不同 NameNode、不同数据库分片上并行；只有访问相同 INode 的事务才需要互相等待。

HopsFS 说明了一件经常被忽略的事：把状态交给数据库，不等于一致性问题就自动解决。数据库提供原子提交和行锁，**应用仍必须定义锁哪些行、按什么顺序锁、文件系统语义如何映射到事务冲突。**

#### 大目录操作：不能把百万行塞进一个事务

递归删除、移动非空目录、递归修改权限和 Quota 可能涉及数百万 INode。把整棵子树放进一个 NDB 事务既不现实，也会长时间持有大量行锁。

HopsFS 为此设计了 Subtree Operations Protocol：

1. 在子树根节点持久化一个带 NameNode ID 的排他标记；
2. 等待已有 INode 操作结束，使整棵子树进入 Quiescent 状态；
3. 把大操作拆成多个可重试的小事务，并行、分批执行；
4. NameNode 失败时，其他 NameNode 识别遗留标记并继续或清理操作。

这里需要修正一个常见误解：跨目录 `rename` 并不需要更新所有后代的完整路径，因为 HopsFS 保存的是逐级 INode。系统仍需扫描并隔离子树来保证并发安全，但最终移动可以通过修改子树根 INode 完成；真正逐批删除大量记录的是递归删除等操作。

这种协议很像数据库里的“长事务拆短事务”：为了避免一个庞大原子事务拖垮系统，先用应用级意图锁圈定作用域，再把工作增量化。它牺牲了实现简单性，换取大规模命名空间上的可恢复性。

#### 论文证据，以及数字的边界

论文在 HDFS 2.0.4 派生版本、MySQL Cluster NDB 和 Spotify 工作负载回放环境中报告：

- 元数据容量至少达到对比 HDFS 的 **37 倍**；
- Spotify 混合负载吞吐达到 HDFS 的 **16 倍**；
- 更偏写的负载达到 HDFS 的 **37 倍**；
- 多 NameNode 场景下，单个 NameNode 失败没有造成服务停顿，客户端继续在剩余节点上重试。

这些结果证明元数据层能够水平扩展，但不是说每次 HopsFS 操作都比 HDFS 更快。论文明确指出：空载 HDFS 的元数据已在本机内存中，单次操作延迟通常更低；HopsFS 的优势出现在大量并发客户端让 HDFS 全局锁和单机吞吐成为瓶颈时。

大目录操作尤其能体现代价。在论文的百万文件目录测试中，HopsFS 的移动与递归删除明显慢于 HDFS，因为它需要远程读取元数据、隔离子树并执行多个事务。论文认为此类操作在目标生产 Trace 中频率较低，因此接受这一取舍。

#### HDFS 后来怎样处理这些问题

HDFS 并没有因为 NewSQL 元数据方案出现而停止演化，也不应简单得出“大厂已经不再使用 HDFS”的结论。Apache Hadoop 后续沿着兼容原架构的路线扩展：

- Federation 用多个 Namespace / Block Pool 拆分单 NameNode 容量；
- [Router-based Federation](https://hadoop.apache.org/docs/stable/hadoop-project-dist/hadoop-hdfs-rbf/HDFSRouterFederation.html) 在多个子集群前提供统一路由与 Mount Table；
- [Observer NameNode](https://hadoop.apache.org/docs/stable/hadoop-project-dist/hadoop-hdfs/ObserverNameNode.html) 让只读流量由 Observer 分担，同时通过 Edit Log Tailing 保持客户端一致性。

这条路线强调渐进兼容和已有生态，HopsFS 则选择从根上把元数据事务数据库化。二者不是简单的先进与落后，而是“保留单分片状态机、扩展外围”与“把状态本身分片化”之间的工程选择。

#### 我的判断

HopsFS 最重要的启发不是“文件系统应该使用 MySQL”，而是：**元数据服务也可以被当作一个高并发 OLTP 系统来设计。** 路径是索引访问，文件操作是事务，目录热点是数据倾斜，Subtree Operation 是跨分片长事务。

它复用了数据库的复制、恢复和事务能力，却没有逃避领域语义。相反，真正困难的工作正是把目录树的层级锁、Block 状态机和故障恢复准确映射到数据库原语。

这种设计适合元数据规模和并发已经超过单机上限、团队又有能力维护 NewSQL 集群的场景。对规模较小的系统，远程数据库延迟和双重运维栈可能比单 NameNode 的问题更早到来。

#### 待继续验证

- HopsFS 当前实现中 NDB Data Access Layer、锁顺序和 Subtree Lock 的源码路径；
- 顶层伪随机分片面对超级目录和热点租户时的真实效果；
- NDB 故障、网络分区和长事务重试共同出现时的尾延迟；
- 将 NDB 替换成 FoundationDB、TiKV 或其他事务 KV 时，需要哪些隔离语义；
- HopsFS 与 Tectonic 的核心差异：前者强调跨行 ACID，后者为何接受分层元数据的非原子更新？

---

### PolarFS：面向云数据库的共享存储

#### 论文信息

- 论文：[PolarFS: An Ultra-low Latency and Failure Resilient Distributed File System for Shared Storage Cloud Database](https://www.vldb.org/pvldb/vol11/p1849-cao.pdf)
- 作者：Wei Cao、Yingqiang Zhang、Xingwang Yang 等
- 出处：PVLDB 11(12)，2018
- DOI：[10.14778/3229863.3229872](https://doi.org/10.14778/3229863.3229872)

#### 为什么数据库要重新实现文件系统

Shared-Storage 架构把计算节点与持久化存储解耦，允许多个数据库实例访问同一份数据。它有利于快速拉起只读节点、计算弹性和故障迁移，但远程 I/O 会经过文件系统、内核网络栈、协议处理、复制与一致性链路，很容易让微秒级 NVMe 重新变成毫秒级服务。

当底层硬件还是机械盘和低速网络时，内核路径的成本常被设备延迟掩盖。NVMe 与 RDMA 出现以后，软件栈本身开始主导尾延迟。PolarFS 的目标可以概括为：

> 为云数据库保留类似 POSIX 的文件接口，同时用数据库专用的用户态数据路径，把远程共享存储做得尽量接近本地 NVMe。

这是一种典型的数据库与存储协同设计：不让数据库直接管理裸块，也不把全部语义交给通用分布式文件系统，而是在二者之间重新划分职责。

#### 整体架构

PolarFS 由四个核心组件组成：

```text
+------------------------------------------------------------------+
| Database Compute Node                                            |
|                                                                  |
|  POLARDB                                                          |
|    -> libpfs：用户态文件系统库，提供 POSIX-like API              |
|    -> PolarSwitch：I/O 路由、Chunk 位置缓存与失败重试             |
+-------------------------------+----------------------------------+
                                | RDMA
                                v
+------------------------------------------------------------------+
| Storage Cluster                                                  |
|                                                                  |
|  ChunkServer leader  <---- ParallelRaft ---->  ChunkServer peers |
|    -> SPDK 用户态 NVMe 驱动                                     |
|    -> 本地 NVMe SSD                                              |
|                                                                  |
|  PolarCtrl：元数据、调度与集群控制（不在关键 I/O 路径）          |
+------------------------------------------------------------------+
```

组件职责如下：

- **libpfs**：链接进数据库进程的轻量级用户态文件系统库；挂载时加载目录、文件与块映射，常规读写通过 `pfs_pread`、`pfs_pwrite` 完成；
- **PolarSwitch**：部署在计算节点的路由进程，缓存 Chunk 位置，将请求转发到正确的 ChunkServer，并在 Leader 变化时重试；
- **ChunkServer**：部署在存储节点，每个实例管理独立 NVMe，并绑定专用 CPU Core，通过 SPDK 访问设备；
- **PolarCtrl**：控制面，负责元数据与集群管理，论文实现使用 MySQL 保存元数据，不进入常规数据 I/O 的关键路径。

`libpfs` 与 PolarSwitch 之间使用共享内存 Ring Buffer 和轮询通信，减少进程间系统调用与拷贝。PolarSwitch 缓存位置元数据，因此 PolarCtrl 短时不可用时，已有数据路径仍可继续工作。

#### 数据组织：Volume、Chunk 与 Block

PolarFS 需要同时解决“海量存储对象如何管理”和“小 I/O 如何执行”两个问题，因此使用不同层级的粒度：

- **Volume**：数据库看到的逻辑卷，论文中的容量范围为 10GB 到 100TB；
- **Chunk**：默认 10GB，是数据放置、复制和迁移的最小单位；默认有三个副本，并尽量分布在不同机架；
- **Block**：Chunk 内部按 64KB Block 管理，用于分配和实际数据映射，并支持 Thin Provisioning。

所以“Chunk 默认 10GB”并不表示每次 I/O 都读写 10GB。它是控制面和数据放置粒度，而 64KB Block 才是 Chunk 内更细的空间管理单位。

10GB 是一个工程折中。100TB Volume 只需约一万个 Chunk，PolarSwitch 可以把位置映射完整缓存在内存中，PolarCtrl 的元数据规模也更可控。代价是单个 Chunk 内部出现热点时不能继续拆分，只能依靠足够多的 Chunk、均匀分布和迁移来缓解。

文件系统层还提供预分配接口。数据库可通过 `pfs_fallocate` 提前分配空间，让常规写路径尽量避免同步修改文件系统元数据。这个设计看似普通，却很能体现数据库专用接口的价值：把可预测的慢路径提前做掉，使事务 I/O 路径保持稳定。

#### 用户态 I/O：绕过内核，但不要神化“零开销”

PolarFS 在关键数据路径使用两类硬件能力：

- 计算节点与存储节点、存储副本之间使用 RDMA；
- ChunkServer 使用 SPDK 在用户态以轮询方式驱动 NVMe。

RDMA 将数据传入预注册内存，SPDK 避免传统内核块设备路径中的中断与多次上下文切换。它们的共同目标不是让软件消失，而是让数据移动更直接、执行模型更可控。

“全链路零拷贝、零上下文切换”是过度概括。实际系统仍有请求描述符处理、队列操作、协议状态机、日志复制和必要的数据组织成本。更准确的说法是：**PolarFS 在关键数据路径中尽量减少内核穿越、上下文切换和冗余拷贝。**

轮询也不是免费午餐。它用持续占用 CPU Core 换取更稳定的低延迟。在云环境中，这进一步引出资源隔离、空闲功耗和高负载下 Poller 调度公平性问题。

#### 一次写请求如何完成

一次典型写请求可以拆成以下步骤：

```text
POLARDB
  -> libpfs 把请求放入共享内存 Ring Buffer
  -> PolarSwitch 根据本地缓存路由到 Chunk Leader
  -> Leader 通过 RDMA 接收数据到预注册 Buffer
  -> Leader 并行执行：
       1. 通过 SPDK 写本地 WAL
       2. 通过 RDMA 向 Followers 复制日志
  -> Followers 持久化并确认
  -> 多数副本完成持久化后提交
  -> Leader 将日志应用到数据块并返回结果
```

这条路径里，真正重要的并不是单独使用了 RDMA 或 SPDK，而是网络、持久化和复制协议围绕同一个异步执行模型组织起来。如果上层仍然要求每个请求严格串行等待，硬件多队列能力依旧发挥不出来。

#### ParallelRaft：放宽不必要的串行约束

标准 Raft 通过单调递增的日志序列建立清晰的复制顺序。这个模型易于推理，但若实现要求前一条日志完成后才能确认下一条，在高并发 NVMe 和 RDMA 环境中就会形成 Head-of-Line Blocking。

ParallelRaft 保留 Leader、Follower、多数派持久化和复制日志等 Raft 基本结构，同时允许日志 **乱序持久化、乱序确认和有条件地乱序应用**：

- 写入范围互不重叠时，可以并行执行和应用；
- LBA 范围发生冲突时，仍须保持原有顺序；
- 日志存在空洞时，系统使用 Look-behind Buffer 记录前面若干条日志的 LBA 摘要，判断当前操作是否能安全推进；
- Leader 切换后进入 Merge 阶段，补齐或重新确认缺失日志，再恢复正常复制。

论文在其 RDMA 环境中发现，回看前两条日志就能获得较好效果，但这不是协议层的普适常数。

ParallelRaft 的正确性也不能简单归因于“数据库上层有 WAL，可以出错后回滚”。关键仍然是存储层识别逻辑块地址冲突，并保证有依赖的写入顺序。数据库日志提供的是另一层事务恢复语义，不能代替底层块设备应有的一致性。

#### 论文证据，以及今天如何看这些数字

在论文 2018 年的测试环境中：

- 4KB 随机写平均延迟约为 **48 微秒**；
- 对比环境中的本地 Ext4 约为 **10 微秒**，CephFS 约为 **760 微秒**；
- Queue Depth 为 32 时，标准 Raft 的延迟约为 ParallelRaft 的 **2.5 倍**，IOPS 不到后者一半。

这些数字说明专用用户态数据路径在当时硬件上有效，但不能直接代表今天 PolarDB 产品、现代 Ceph 或新一代 RDMA/NVMe 平台的性能。论文数字应和测试时间、硬件型号、网络配置、队列深度及持久化语义一起阅读。

#### 从更高层看 PolarFS

PolarFS 不是简单地“用 RDMA + SPDK 写了一个更快的文件系统”。我认为它至少揭示了四条更重要的系统设计原则。

第一，**控制面与数据面必须分离**。元数据服务负责全局决策，但常规 I/O 依赖本地缓存而不是每次访问中心节点。这既降低延迟，也缩小控制面故障的影响范围。

第二，**不同问题需要不同粒度**。10GB Chunk 解决放置与复制，64KB Block 解决分配，数据库 Page 解决引擎内部一致性。试图用一个统一粒度解决所有层级的问题，通常会在元数据规模或热点治理上付出代价。

第三，**异步化的本质是消除虚假的顺序依赖**。RDMA 和 NVMe 提供并发能力，ParallelRaft 做的事情是辨认哪些操作真的冲突、哪些只是被日志序号意外串行化。这个原则同样适用于数据库执行器、Compaction Scheduler 和 RPC 框架。

第四，**专用系统不是放弃抽象，而是重新选择抽象边界**。PolarFS 仍给数据库提供类 POSIX 文件接口，但内部不再承担全部通用文件系统语义。相比 Aurora 把 Redo Log 语义进一步下推到存储层，PolarFS 保留了更清晰的文件 / 块边界。二者代表两种不同程度的数据库存储协同。

#### 我的判断

PolarFS 的价值既在性能，也在它证明了 Shared-Storage 数据库可以不把“远程”自动等同于“高延迟”。但它把复杂度转移到了另外几个位置：用户态资源调度、RDMA 内存管理、元数据 Fencing、副本修复、热点迁移以及数据库主节点与共享数据之间的正确性协议。

尤其需要警惕把 2018 年论文架构直接等同于今天的 PolarDB / PolarStore。生产系统会长期演进，组件边界、协议与硬件都可能变化。后续讨论产品现状时，应单独查阅当代资料，而不是从这篇论文外推。

#### 待继续验证

- 10GB Chunk 在热点写、容量倾斜和故障重建时的迁移成本；
- PolarCtrl、PolarSwitch 与 ChunkServer 之间如何完成版本校验和 Fencing；
- ParallelRaft 与今天 Multi-Raft、EPaxos 类协议以及存储设备多队列调度的关系；
- SPDK Polling 在低负载、多租户和节能场景下的 CPU 成本；
- PolarFS 之后的 PolarDB / PolarStore 在日志下推、压缩和计算存储分离方面发生了哪些变化。

---

### Tectonic：从专用存储烟囱走向 EB 级统一底座

#### 论文信息

- 论文：[Facebook's Tectonic Filesystem: Efficiency from Exascale](https://www.usenix.org/system/files/fast21-pan.pdf)
- 作者：Satadru Pan、Theano Stavrinos、Yunqiao Zhang、Atul Sikaria 等
- 出处：[FAST 2021](https://www.usenix.org/conference/fast21/presentation/pan)，217–231 页
- 官方介绍：[Consolidating Facebook storage infrastructure with Tectonic](https://engineering.fb.com/2021/06/21/data-infrastructure/tectonic-file-system/)

#### 为什么读这篇论文

Tectonic 之前，Facebook 为不同 workload 建立了多套专用存储：

- Haystack 服务热 Blob，主要受 HDD IOPS 限制；
- f4 使用纠删码保存温冷 Blob，更关注容量成本；
- 多个 HDFS 集群承载数仓批处理，主要消耗顺序带宽。

这种“一个 workload 一套系统”的方式能够做极致优化，却带来资源孤岛。Haystack 可能缺 IOPS 但空出容量，f4 可能缺容量但空出 IOPS，数仓则在大任务期间出现周期性带宽洪峰。硬件只能留在各自集群里，系统无法把不同 workload 的互补性转化为利用率。

Tectonic 的问题因此不只是如何造一个更大的文件系统，而是：

> **能否把 Blob 与数仓放进同一个 EB 级存储池，让容量、IOPS 和带宽跨租户复用，同时仍然提供接近专用系统的性能与隔离？**

这是一条从“为 workload 建系统”转向“让平台允许 workload 自定义策略”的路线。

#### 整体架构：客户端编排，元数据与数据面解耦

```text
Application / Tenant
        |
        v
Tectonic Client Library
  - 把文件 API 编排为元数据与 Chunk RPC
  - 选择副本、纠删码、Hedged Request 与 QoS 策略
        |
        +--------------------------+
        |                          |
        v                          v
Metadata Store                Chunk Store
  Stateless Services            Flat Chunk Namespace
  Name / File / Block layers    XFS files on storage nodes
  ZippyDB shards                HDD + SSD hot-chunk cache
        |                          |
        +-------------+------------+
                      |
       Repair / Rebalance / GC / Statistics
```

这里不宜把元数据服务直接称为 HDFS NameNode。Tectonic 的 Client Library 承担了大量文件系统编排逻辑，后端则拆成独立的元数据微服务和扁平 Chunk Store：

- Chunk Store 只理解 Chunk，不理解文件与目录；
- Metadata Store 保存命名空间以及 File → Block → Chunk 的映射；
- Client Library 直接访问元数据服务与存储节点，避免所有数据经过中心代理；
- 无状态后台服务负责修复、再平衡、垃圾回收和统计聚合。

这种 Client-driven 架构避免了额外数据转发节点，也允许不同租户选择不同策略。代价是客户端库进入大量应用进程：协议升级、Bug 扩散和版本兼容都比薄客户端更难治理。远程跨数据中心访问也不适合反复编排多个 RPC，因此 Tectonic 为远程请求使用靠近存储集群的无状态代理。

#### Chunk Store：扁平数据面与每个 Block 的持久化策略

Tectonic 的数据层级是：File 由 Block 构成，Block 再由一个或多个 Chunk 构成。Chunk 作为扁平对象保存在存储节点的本地 XFS 文件中，节点提供 `get`、`put`、`append`、`delete`、`list` 和 `scan` 等核心接口。

Block 才是向上封装持久性策略的逻辑单位。每个 Block 可以选择：

- 多副本，在小追加或延迟敏感时减少编码读写成本；
- Reed-Solomon 编码，在封闭后换取更好的容量效率；
- 不同故障域与 Copyset，控制副本相关性和故障重建压力。

论文给出的一个典型策略是：写入中的小 Block 先用三副本承接 Append，Block Seal 后再批量转换为 RS(10,4)。如果每次小追加都直接更新 14 个数据与校验 Chunk，IOPS 成本会非常高；Seal 后用大 I/O 编码，才能同时获得前台写效率和长期空间效率。

这和 LSM-tree 的思路有些相似：前台先选择适合增量更新的形态，状态稳定后再转成适合读取和容量的形态。系统没有消灭转换成本，而是把它移动到更适合批处理的时间点。

#### 分层元数据：Name、File、Block 各自扩展

Tectonic 把文件系统元数据拆为三个逻辑层：

| 元数据层 | 主要映射 | 典型分片键 |
|---|---|---|
| Name | Directory → Subdirectory / File | Directory ID |
| File | File → Block List | File ID |
| Block | Block → Disk / Chunk List | Block ID |

每层分别哈希分片到 ZippyDB。ZippyDB 是线性一致、容错的分片 KV，单节点使用 RocksDB 保存副本，分片通过 Paxos 复制。哈希分片让不同目录、文件和 Block 的流量更容易打散，也允许某一层单独扩容。

它与 HopsFS 的关键差异在这里出现：论文中的 ZippyDB **不提供跨分片事务**。一次文件操作可能跨 Name、File 和 Block 多层完成，失败时允许暂时出现可识别的不一致，再由分层 Garbage Collector 和后台服务修复。

这不是简单地降低一致性，而是在接口约束下重新分配正确性：

- 单文件只允许一个 Writer，通过写 Token Fencing 旧 Writer；
- Client Library 按协议顺序执行多步更新；
- 已确认的写必须已经更新可见的 Block 元数据；
- 失败遗留的孤儿对象或延迟删除由后台 GC 收敛。

HopsFS 更像把文件系统操作放进 NewSQL 事务，Tectonic 则把元数据拆得更细，依靠单写者、幂等步骤和异步修复跨越事务边界。后者牺牲了通用跨分片原子性，换取更彻底的哈希分片和 EB 级扩展。

#### 多租户隔离：调度的资源应当是瓶颈本身

把 Blob 与数仓合并后，隔离不能只看请求数。Blob 常见小 I/O，消耗 IOPS；数仓常见大扫描，消耗带宽；真正统一二者的是磁盘忙碌时间。

Tectonic 建立了分层资源模型：

```text
Tenant
  -> TrafficGroup：相似流量形态与延迟目标的一组应用
       -> TrafficClass：生产、普通、后台等优先级
```

客户端限流器使用修改后的 Leaky Bucket，根据近期需求与供给，依次寻找本 TrafficGroup、本 Tenant、其他 Tenant 的空闲能力，并遵循 TrafficClass 优先级。存储和元数据节点还需要在本地处理热点与公平共享。

我认为这里最值得借鉴的是资源度量。若系统只限制字节数，会低估小随机 I/O；只限制 IOPS，又会低估大块顺序读。数据库 Resource Group、Compaction Scheduler 和对象存储网关也面临同样问题：**隔离策略必须针对真正饱和的硬件资源，而不能只针对最容易计数的逻辑请求。**

#### 论文证据，以及数字的边界

论文展示的一个生产集群具有以下规模：

- 总容量约 **1590 PB**，其中已使用约 **1250 PB**；
- **107 亿个文件**、**150 亿个 Block**；
- **4208 个存储节点**；
- Blob 与数仓各占约一半已用空间，并共享磁盘时间处理数仓周期性洪峰。

论文还报告，将数仓迁到 Tectonic 后，数仓集群数量减少了 **10 倍**；统一系统在目标 workload 上提供了与原专用系统相当或更好的性能。

这些数据证明资源池化在 Facebook 当时的规模上有实际收益，但不能直接推出“统一集群总比专用集群便宜”。收益依赖 workload 互补性、足够大的资源池、成熟的隔离机制，以及能够长期运营 ZippyDB、Chunk Store 和后台修复体系的工程能力。

#### 论文主动暴露的代价

Tectonic 很有价值的一点，是论文没有把统一架构写成只有收益：

- 元数据需要经过一个或多个网络调用，单次延迟高于 HDFS 本机内存 NameNode；
- 数仓需要把原来串行执行的多个 Rename 改为并行，以隐藏额外元数据延迟；
- 哈希分片使递归目录枚举需要访问大量分片，Tectonic 没有直接提供 Recursive List；
- `du` 一类目录聚合统计改为周期性计算，结果可能陈旧；
- RS Reconstruction 一次读可能放大为多个磁盘 I/O，需要限流以避免故障时形成重建风暴；
- Client Library 变得很重，客户端版本与服务端协议需要共同治理。

这些不是实现瑕疵，而是 EB 级分片设计的结构性代价。局部内存树擅长层级遍历，哈希分片擅长打散热点；系统无法同时把两者做到极致，只能让高频路径优先，再为低频操作设计批量、并行或异步聚合。

#### 从 2026 年再看：Tectonic 与 AI 存储

Meta 在 2026 年的[官方 AI 存储架构回顾](https://engineering.fb.com/2026/07/01/data-infrastructure/metas-ai-storage-blueprint-at-scale/)中仍把 Tectonic 描述为横向扩展的基础 Block Layer，其上提供对象、文件和块接口。文中也提到，Llama 训练曾通过 NFS-like 文件接口直接使用 Tectonic，而现代训练栈正在逐步向其上的 Blob Storage 接口迁移。

这恰好说明基础存储层与面向 AI 的数据访问接口不是同一个问题。Tectonic 解决容量、持久性、放置与多租户资源池；上层 Blob / Dataset 服务还需要解决全球命名、样本组织、缓存、预取和 pMax 延迟。随着 GPU 成为昂贵的同步消费者，存储目标也从“最大化磁盘利用率”进一步变成“减少 GPU Stall，并在功耗约束下提供可预测尾延迟”。

#### 我的判断

Tectonic 并没有给出所谓“现代分布式文件系统的终极拓扑”。它更像一次极具说服力的架构收敛：当组织拥有足够多互补 workload 时，统一的扁平数据面和分层元数据能释放资源池化收益；专用差异不再通过维护多套后端实现，而是通过客户端策略、Block 编码和 QoS 表达。

但统一底座并不等于统一语义。越往下的系统越需要保持简单稳定，越往上的租户层越需要承担编排、缓存和适配。Tectonic 的复杂度没有消失，而是分布在 Client Library、ZippyDB、后台修复和多层隔离中。

#### 待继续验证

- Name / File / Block 多层更新的具体顺序、幂等条件与 GC 安全边界；
- ZippyDB 不支持跨分片事务时，哪些异常状态会短暂暴露给客户端；
- Copyset 数量、故障相关性与 Reconstruction Load 之间如何建模；
- TrafficGroup 的供给计算如何处理 SSD、HDD 与网络等多维资源；
- AI 训练从文件接口迁移到 Blob 接口后，数据加载、缓存和 Checkpoint 路径发生了什么变化。

---

## 四篇论文放在一起看

Bf-Tree、HopsFS、PolarFS 与 Tectonic 分别研究索引、元数据、共享存储和统一文件系统，却遵循相似的推理路径：

| 论文 | 硬件变化 | 旧的软件假设 | 重新选择的边界 | 新引入的复杂度 |
|---|---|---|---|---|
| Bf-Tree | SSD 随机 I/O 更快、内存更宝贵 | 缓存页必须等于磁盘页 | mini-page 作为细粒度缓存与变脏单位 | 可变长空间管理、合并与恢复 |
| HopsFS | NewSQL 可提供分片事务与内存级吞吐 | 文件系统元数据必须驻留在单 NameNode | NDB 成为权威元数据状态 | 数据库延迟、分片键与子树事务 |
| PolarFS | NVMe、RDMA、多队列并发 | 通用内核路径与严格日志串行足够高效 | 用户态数据路径与冲突感知的提交顺序 | Polling、元数据、复制协议与资源隔离 |
| Tectonic | EB 级容量与异构 workload 具有互补资源需求 | 每种 workload 都需要独立存储系统 | 分层元数据、扁平 Chunk Store 与租户策略 | 跨层修复、重客户端与多租户 QoS |

它们共同说明：

> **硬件只提供能力，软件决定能力是否会被旧抽象抵消。**

但“绕过一层”不会消灭复杂度。Bf-Tree 绕过固定页 Buffer Pool 后，需要自己管理可变长缓存；HopsFS 移除单 NameNode 权威状态后，需要把层级锁映射为数据库事务；PolarFS 绕过通用内核 I/O 后，需要自己管理队列、内存和故障；Tectonic 合并专用存储后，需要承担跨层修复与多租户隔离。系统设计真正困难的地方，不是找到一个更短的 Fast Path，而是同时建立一条正确、可恢复、可观测的 Slow Path。

这也是我后续阅读数据库论文时会持续追问的主线：

1. 论文优化的瓶颈是真实硬件瓶颈，还是基准测试构造出的瓶颈？
2. 它移除了什么串行点，又把一致性成本放到了哪里？
3. 前台吞吐之外，恢复、后台任务与尾延迟是否仍然成立？
4. 这个设计适合成为通用基础设施，还是只适合特定 workload 的专用层？

## 后续补充模板

新增论文时，在顶部阅读索引增加一行，并把下面模板复制到对应分类。精读正文优先记录可验证事实与自己的判断，不必追求把论文从头到尾重新翻译一遍。

```markdown
### Paper Title

#### 论文信息

- 论文：[Title](原文链接)
- 作者：
- 出处：
- DOI / 代码 / 设计文档：

#### 为什么读这篇论文

它试图解决什么问题？这个问题为什么现在值得重新讨论？

#### 核心机制

先给出一句话结论，再拆解架构、关键路径和正确性机制。

#### 论文证据，以及数字的边界

记录硬件、数据规模、负载、基线与关键结果；明确哪些结论不能外推。

#### 与相关系统的关系

比较它们解决的问题和机制，不只比较产品名称或性能数字。

#### 我的判断

写清收益来自哪里、复杂度转移到哪里、是否具备生产落地条件。

#### 待继续验证

- 源码路径或复现实验；
- 尚未确认的技术判断；
- 可以连接到下一篇论文的问题。
```

这篇笔记会沿着“数据结构、执行引擎、存储系统、分布式协议、云原生架构”逐步扩展。每增加一篇论文，都应该让已有问题得到一部分回答，或产生一个更准确的新问题。
