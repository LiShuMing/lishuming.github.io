---
title: "【Paper】数据库系统论文精读：从现代硬件到工业自治"
slug: "database-paper-reading-notes"
date: 2026-08-23T20:21:00+08:00
lastmod: 2026-08-29T00:00:00+08:00
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
  - 查询优化器
  - Learned Optimizer
  - History-Based Optimization
  - 分布式事务
  - Remote Shuffle
description: "持续更新的数据库系统论文精读笔记：从 Bf-Tree、HopsFS、PolarFS、Tectonic 到 Aurora Limitless、FuxiShuffle、LOAM 与 Ultron，理解现代硬件、云存储、分布式执行与反馈闭环如何重新定义系统边界。"
draft: false
toc: true
---

这是一篇持续更新的数据库论文精读笔记。

当前记录覆盖十四项看起来相距很远的研究与工业实践：Bf-Tree 讨论单机超内存索引，HopsFS 与 Tectonic 讨论文件系统元数据和资源池化，PolarFS 讨论云数据库共享存储；Aurora Limitless 讨论 PostgreSQL 兼容 OLTP 如何横向扩展，FuxiShuffle 讨论中间数据交换怎样服务化并获得自适应容错；Redshift、MaxCompute、Databricks、OceanBase、SQL Server、Databricks Lakehouse 与 Oracle 的八项工作，则讨论统计、学习模型、执行历史、索引、数据布局和计划稳定性如何形成可信的优化闭环。

它们的共同点很明确：现代 SSD、RDMA、多核 CPU、分布式数据库和海量执行历史不会自动转化成系统能力，软件必须重新选择缓存粒度、状态归属、I/O 路径、决策依据和安全验证边界。

## 阅读索引

| 方向 | 论文 | 会议 / 年份 | 状态 | 我关注的核心问题 |
|---|---|---:|---|---|
| 数据结构 / 范围索引 | [Bf-Tree](#bf-tree现代硬件上的超内存范围索引) | PVLDB 2024 | 精读 | 缓存页是否必须与磁盘页等大？ |
| 文件系统 / 元数据 | [HopsFS](#hopsfs用-newsql-扩展层级文件系统元数据) | FAST 2017 | 精读 | 能否把 NameNode 的内存状态变成可水平扩展的数据库事务？ |
| 工业架构 / 分布式存储 | [PolarFS](#polarfs面向云数据库的共享存储) | PVLDB 2018 | 精读 | 如何把共享存储的远程 I/O 延迟压到接近本地盘？ |
| 文件系统 / 资源池化 | [Tectonic](#tectonic从专用存储烟囱走向-eb-级统一底座) | FAST 2021 | 精读 | 如何让 Blob 与数仓共享同一个 EB 级存储池，同时保持隔离？ |
| 分布式 OLTP / PostgreSQL | [Aurora Limitless](#aurora-limitless让-postgresql-兼容-oltp-横向扩展) | SIGMOD Companion 2026 | 精读 | 如何把单 Writer Aurora 扩展成 Router + Shard，同时保留事务语义？ |
| 执行引擎 / Remote Shuffle | [FuxiShuffle](#fuxishuffle把数据交换变成自适应可恢复的服务) | SIGMOD Companion 2026 | 精读 | Shuffle 服务如何同时选择介质、调度时机、数据布局和恢复策略？ |
| 云数仓 / 自治基础设施 | [Redshift Re-invented](#redshift-re-invented先建立能够反馈的系统底座) | SIGMOD 2022 | 精读 | 存算分离、遥测与弹性基础设施为何是自治优化的前提？ |
| 优化器 / 增量统计 | [Redshift Incremental Stats](#redshift-增量统计把全表-analyze-变成可合并状态) | PVLDB 2026 | 精读 | 如何用可合并 Sketch 持续维护 CBO 统计？ |
| 优化器 / Learned Optimizer | [LOAM](#loam当统计缺失环境未知且不能在线试错) | SIGMOD 2026 Industry | 精读 | 缺少统计且环境变化时，如何安全比较候选计划？ |
| 优化器 / HBO | [Ultron](#ultron记住-aqe-的修正让下一次少走弯路) | PVLDB 2026 | 精读 | 如何把 AQE 的运行时修正变成下一次优化的历史？ |
| 优化器 / 参数化查询 | [ScalePQO](#scalepqo一个模板一个模型也是一种不可扩展) | PVLDB 2026 | 精读 | 模板与参数规模增长时，模型和 Plan Cache 如何共同演进？ |
| 物理设计 / LLM | [LLM Index Tuning](#llm-索引调优最好的一次回答不是可部署系统) | PVLDB 2026 | 精读 | LLM 适合生成索引候选，还是直接承担最终决策？ |
| 物理设计 / 自治聚簇 | [AutoLiquid](#autoliquid推荐聚簇键不难自动应用才难) | VLDB 2026 Industry | 公开设计预读 | 如何在改变数据布局前低成本验证收益？ |
| 优化器 / Plan Stability | [Oracle Real-Time SPM](#oracle-real-time-spm先让新计划发生再阻止它继续发生) | VLDB 2026 Industry | 公开设计预读 | 如何在计划演进与回退风险之间建立接受协议？ |

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

## 分布式 OLTP：在 PostgreSQL 语义上横向扩展

### Aurora Limitless：让 PostgreSQL 兼容 OLTP 横向扩展

#### 论文信息

- 论文：[Aurora PostgreSQL Limitless Database: Building a Highly Scalable OLTP Database](https://software.imdea.org/~gotsman/papers/limitless-sigmod26.pdf)
- 作者：Dmitry Arkhangelskiy、Saikiran Avula、Sachit Batra、Jin Chen、Radwan Deeb、Alexey Gotsman 等
- 出处：SIGMOD Companion 2026
- DOI：[10.1145/3788853.3803089](https://doi.org/10.1145/3788853.3803089)
- 产品文档：[Amazon Aurora PostgreSQL Limitless Database](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/limitless.html)

#### 为什么读这篇论文

经典 Aurora 已经把计算与分布式存储分离，但写入入口仍然是一台 Primary DB Instance。增加 Read Replica 可以扩展读，无法突破单 Writer 的 CPU、Buffer Pool、连接数和写吞吐上限。业务继续增长时，只能垂直扩容，或让应用自己维护多个数据库、路由规则与跨库事务。

Aurora Limitless 试图保留 PostgreSQL 驱动、SQL、事务和运维体验，同时把一个逻辑数据库拆成可以独立扩展的 Router Fleet 与 Shard Fleet。它真正要解决的不是“数据库能不能分片”，而是：

> **在没有中心化事务管理器的条件下，如何把分片、路由、跨分片事务、DDL、查询下推和弹性扩缩封装进一个 PostgreSQL 兼容服务？**

#### 三种表：分片并没有从数据建模中消失

Aurora Limitless 提供三种表类型：

| 表类型 | 物理放置 | 主要用途 | 代价 |
|---|---|---|---|
| Sharded Table | 按用户指定 Shard Key 哈希分布 | 大表写入与容量横向扩展 | 非 Shard Key 访问可能 Scatter-Gather |
| Reference Table | 在所有 Shard 完整复制 | 小字典表与 Sharded Table 本地 Join | 更新和存储随 Shard 数放大 |
| Standard Table | 完整放在一个指定 Shard | 不需要扩展的普通表、迁移过渡 | 容量和写入仍受单 Shard 限制 |

共享相同 Shard Key 的表可以 Collocate。例如 `customers` 与 `orders` 都按 `cust_id` 分布，同一客户的数据会落到同一个 Shard。以 `cust_id` 为边界的事务与 Join 因而可以退化成单 Shard 操作。

这里需要澄清“免应用分片”的含义：应用不再维护连接池、路由中间件、2PC 和分片拓扑，但用户仍要决定哪些表分片、选择哪个 Shard Key、哪些表复制。论文在 Future Work 中也承认，Shard Key 是接近 One-way Door 的决定，变更通常需要数据迁移。Limitless 消除的是分片的执行与运维泄漏，不是数据局部性本身。

#### Router 与 Shard：控制、协调和执行如何分工

```text
PostgreSQL Client
       |
       v
Router Fleet
  - Session endpoint / transaction coordinator
  - topology + schema + placement cache
  - distributed planner / result assembly
       |
       +----------+----------+
       |          |          |
       v          v          v
   Shard 1    Shard 2    Shard N
 PostgreSQL  PostgreSQL  PostgreSQL
 Aurora vol. Aurora vol. Aurora vol.
       \          |          /
        Aurora distributed storage across 3 AZs
```

Shard 持有数据分区并执行下推计划；Router 接收所有应用流量，维护 Shard Group 的 Schema 与 Placement 视图，选择 Snapshot、驱动跨 Shard Commit，并把客户端 Session 多路复用到 Router–Shard 连接上。一个连接在 Session 生命周期内固定到某个 Router，但 Router 到 Shard 的连接可在事务结束后复用。

Router 不是全部元数据的最终控制面。独立 Cluster-management Service 维护权威拓扑、节点生命周期和变更传播；Router 保存服务查询所需的状态，并对应用表现为统一端点。Router 故障可由 DNS 把新连接导向其他实例，Shard 则可配置跨 AZ Standby，因为某个 Shard 不可用会直接让其数据分区不可用。

#### 时间戳 MVCC 与非阻塞 2PC

社区 PostgreSQL 的 Snapshot 主要由 Transaction ID 集合描述。Aurora Limitless 为跨 Shard 可见性改造了事务内核，使用 Amazon Time Sync 提供的有界时钟误差，把 Snapshot 与 Commit Order 表达为标量时间戳。

跨 Shard 写仍使用 2PC，但不设置全局中心协调器：

```text
Router chooses snapshot timestamp
  -> execute on participating shards
  -> PREPARE on every participant
  -> choose one participant as lead shard
  -> commit decision becomes durable at lead
  -> other shards commit according to lead state
  -> wait out clock uncertainty when required
  -> return success
```

Router 失败后，Prepared Transaction 不依赖原 Router 内存才能恢复：若没有 Shard 持久化 Commit Decision，事务可以 Abort；若部分 Shard 已提交，Lead Shard 的状态成为权威，其余 Participant 查询 Lead 后收敛。把 Lead 放在参与事务的 Shard 中，避免了每笔事务都经过一个全局 Transaction Manager。

Commit Wait 保证 External Consistency：对外返回前，系统确保未来事务的 Snapshot 不会落到这个 Commit 之前。论文称 Amazon Time Sync 的误差界通常低于 1ms，且持久化写延迟往往已经覆盖这段等待；这不表示时钟同步是零成本，也不表示系统支持所有 PostgreSQL 隔离级别。当前设计扩展了 Read Committed 与 Repeatable Read，并明确没有提供 Serializable。

#### 混合扩缩：Serverless 与 Limitless 不是同义词

Aurora Serverless V2 解决单个 Router 或 Shard 的纵向弹性：根据负载调整 ACU、CPU、内存和 Buffer Cache。Limitless 在此基础上增加横向弹性：

- Router 压力上升时向 Router Fleet 增加实例；
- Shard 达到计算或存储阈值时执行 Shard Split，把细粒度 Table Slice 迁移到新 Shard；
- Router 与 Shard 使用不同扩容信号：Shard 更关注计算与 Buffer Cache，Router 更关注连接、协调和内存；
- 整个 Shard Group 有统一的最小/最大 ACU Budget，再根据各节点真实消耗动态分配。

所以 Serverless 回答“单个计算单元需要多少资源”，Limitless 还要回答“应该有多少计算单元、数据如何重新分布以及跨单元语义如何保持”。短时洪峰可以先 Scale-up，持续饱和再 Scale-out，二者组合才能同时控制迁移频率和长期容量。

#### 查询执行：Single-Shard 是 Sweet Spot

Router 使用 PostgreSQL Partition Pruning 判断查询能否只访问一个 Shard；若可以，就把整条查询下推，减少 Router–Shard Round Trip。跨 Shard 查询则拆成 Shard Subplan，在数据侧做 Filter、Partial Aggregate、Sort 或可下推 Join，再由 Router 汇总。

下推不是无条件的：

- Mutable、`STABLE/VOLATILE` 或 Definer Function 可能必须在 Router 执行；
- Collocated Sharded Table 与 Reference Table 更容易完成本地 Join；
- 某些 Outer Join、Anti Join 不能按相同方式下推；
- Standard Table 与其他表类型的 Join 当前可能回到 Router。

因此系统是否接近单机体验，很大程度取决于 Shard Key 是否覆盖高频事务边界。Router 隐藏了分布式执行，但无法消灭跨 Shard 数据移动。

#### 论文证据，以及数字的边界

论文使用修改后的 HammerDB/TPC-C 类负载，包含 12,000 个 Warehouse、1,000 个并发 Client、1,000 万次 Iteration，约 10% 的事务跨 Shard：

- 在相同 1,536 ACU Budget 下，从 2 Router / 4 Shard 扩到 4 Router / 8 Shard，NOPM 从 1,268,350 增至 2,012,763，提升 58.7%，平均 NEWORD 延迟从 29.70ms 降至 21.06ms；
- 在 8 Router / 16 Shard 下把 Budget 从 1,536 提到 3,072 ACU，NOPM 提升 41.6%，平均延迟从 16.42ms 降至 9.72ms；
- 论文写作时，生产最大规模达到 32 Router / 64 Shard，最常见配置为 4 Router / 8 Shard。

这些结果证明混合扩缩在论文构造的良好分片负载上有效，不等于任意 PostgreSQL 应用都能线性扩展。实验已主动把表按 ID 分片，并把可单 Shard 执行的 Function 标记为 Distributed；跨 Shard 比例约 10%。如果大部分事务跨分片、存在热点 Shard Key，或者需要大量 Router 端 Join，结果会明显不同。“Millions TPS / Petabytes”是系统目标和整体能力描述，不能替代每种业务 Schema 的容量与延迟验证。

#### 我的判断

Aurora Limitless 最值得学习的是一种渐进式分布式数据库路线：不是从零重写 PostgreSQL，而是保留 Planner、Executor、类型和生态，再系统改造 Snapshot、Commit、DDL、Partition Pruning 与连接管理。它证明 Shared-storage Aurora 与 Shared-nothing Sharding 并非互斥：每个 Shard 内继续使用 Aurora 分布式存储，Shard 之间再通过数据分区扩展写入。

复杂度也没有消失。过去由应用承担的 Shard Key、拓扑、跨库事务与扩容问题，被集中到了 Router、时间服务、2PC Recovery、Schema 传播和 Shard Split 状态机中。托管服务的价值正是让平台团队统一承担这部分复杂度，但数据库建模仍决定 Fast Path 的比例。

#### 待继续验证

- Read Committed 与 Repeatable Read 的 Snapshot Timestamp、Commit Timestamp 和 Clock-uncertainty Wait 的完整状态机；
- Router Failover、Lead Shard 决议和 In-doubt Transaction 回收如何避免错误提交；
- Shard Split 的 Snapshot Copy、增量追赶、切换锁和失败恢复路径；
- Global Unique Constraint、Secondary Index 与 Foreign Key 跨 Shard 时的真实边界；
- 与 Citus、Vitess、CockroachDB、Aurora DSQL 在兼容性、隔离级别和跨 Region 能力上的逐项比较。

---

## 执行引擎：Remote Shuffle 的自适应与容错

### FuxiShuffle：把数据交换变成自适应、可恢复的服务

#### 论文信息

- 论文：[FuxiShuffle: An Adaptive and Resilient Shuffle Service for Distributed Data Processing on Alibaba Cloud](https://arxiv.org/pdf/2602.22580)
- 作者：Yuhao Lin、Zhipeng Tang、Jiayan Tong、Junqing Xiao、Bin Lu、Yuhang Li、Chao Li 等
- 出处：SIGMOD Companion 2026
- DOI：[10.1145/3788853.3803085](https://doi.org/10.1145/3788853.3803085)

#### 为什么读这篇论文

Shuffle 是分布式执行中最容易被低估的算子边界。逻辑上它只是按 Key 重分区，物理上却同时涉及上游 Fan-out、下游 Fan-in、内存与磁盘选择、网络拥塞、碎片聚合、任务调度、倾斜统计、版本去重和失败重算。

固定策略很难适应多租户生产环境：全部 In-memory 容易挤爆内存，全部 On-disk 会浪费空闲 DRAM；Staged Scheduling 延长关键路径，Gang Scheduling 又让 Reader 长时间占着资源等数据；不给 Backup 会扩大失败重算，全部 Backup 则把正常路径拖慢。

FuxiShuffle 的核心问题因此不是“Push 比 Pull 快吗”，而是：

> **能否根据任务和实时资源，在每个 Shuffle 生命周期阶段选择不同模式，并让失败恢复不丢掉下游已经完成的工作？**

#### 四个角色与两种数据介质

```text
Compute Framework / Fuxi
          |
     Job Manager                 Shuffle Service Manager
  job-level policy/schedule <--> global resource/health control
          |
Writer -> Shuffle Proxy -> Shuffle Agent -> Reader
          batch/aggregate    memory or Pangu disk
```

- **Shuffle Proxy** 与 Writer 同机，合并小包并做初步 Partition Aggregation，降低网络碎片；
- **Shuffle Agent** 是数据面 Worker，接收多个 Writer 的数据，按 Partition 聚合为连续 Block，并放入内存或磁盘；
- **Job Manager** 根据任务优先级、预测时间、数据规模和资源决定 Shuffle Mode、Agent 分配与 Reader 启动时机；
- **Shuffle Service Manager** 管理全局 Agent 资源、分配和健康状态。

Shuffle Agent 既可以和 Compute Worker 同机，用共享内存承载 In-memory Shuffle；也可以部署在 Storage Node，使用盘古磁盘承载 On-disk Shuffle。这不是一套永远独立的 Remote Shuffle Cluster，而是一套允许计算侧内存和存储侧磁盘共同参与的数据交换服务。

Reader 不依赖中心调度器逐条告诉它数据位置。Primary Index 记录 Agent 聚合文件中的 Writer、`RetryIdx`、Offset 和 Length，Backup Index 记录 Writer Backup；Reader 合并元数据、按最大 `RetryIdx` 去重，自己构造读取路径。这让生产、消费和故障切换解耦，也把版本正确性放进了可验证的元数据协议。

#### 自适应一：选择 In-memory 还是 On-disk

短任务、小数据适合留在内存，长任务或大 Partition 更适合磁盘。系统用输入规模、Shuffle Operator、Key Cardinality 和历史画像预测 Task Runtime `t̂`，再用动态阈值决定模式：

```text
Mode = In-memory, if t̂ <= τmode
       On-disk,   otherwise

τ*mode = arg max y(τmode)
         subject to z(τmode) <= Zavailable
```

`y(τ)` 表示阈值下可以被内存加速的数据比例，`z(τ)` 表示每机平均内存需求，`Zavailable` 来自 Worker 实时上报。历史曲线可以按小时离线更新，后台 Daemon 再根据可用内存约每 30 秒重算阈值。

这不是一个静态“小于多少 GB 就进内存”的规则。它把 Admission Control 写成受实时容量约束的收益最大化：集群空闲时吸收更多 In-memory Shuffle，压力升高时主动退回 On-disk。

#### 自适应二：Progressive Scheduling 与 Pre-read

三种 Reader 调度方式代表不同取舍：

| 模式 | Reader 何时启动 | 优势 | 代价 |
|---|---|---|---|
| Staged | 所有 Writer 完成后 | Reader 不空等、资源成本低 | 无法重叠上下游，E2E 更长 |
| Gang | Writer 与 Reader 同时 | Pipeline 最早启动 | Reader 可能长期占资源等待 |
| Progressive | Writer 达到动态进度阈值后 | 接近 Gang 的延迟，减少无效等待 | 需要进度预测与增量可读协议 |

Job Manager 为每个下游 Stage 维护阈值 `λS`。Pre-start 在上游进度达到阈值时申请并启动 Reader；Pre-read 则让 Reader 轮询已经 Commit 的 Block，一边等待新数据，一边顺序消费当前可见数据。

系统不会机械启用 Pipeline：并发很低、可以一次拿齐资源的小 Job 会选择 Gang；遇到 Sort 或 Global Aggregate 这类 Blocking Edge 时，把 `λS` 设为 1，退回 Staged。Progressive Scheduling 的价值在于承认“更早启动”同时影响 E2E 和 CU Cost，阈值必须由算子依赖和资源状况共同决定。

Partition 级 Runtime Statistics 还服务 Dynamic Partition Insertion 与 Adaptive Skew Join：系统根据 Data Size、Record Count、Distinct Key 拆分热点 Partition，必要时拆左侧热点并复制对应右侧 Partition。Shuffle Service 因而不只是字节搬运层，也成为 AQE 的观测面。

#### 自适应三：不是所有 Chunk 都值得备份

FuxiShuffle 支持四类布局：

- **Shuffle Agent File**：聚合后的主读取路径；
- **Default Backup**：Writer 本地备份，Agent 读取失败时避免重跑；
- **Remote Backup**：Writer 与 Agent 同机时把备份放到远端，避免共同失效；
- **Backup Only**：超大 Block 跳过 Agent 聚合，直接写双副本，避免大流量占用 Agent 网络与 I/O。

是否备份由 Writer Runtime、Partition 总大小和单 Block 大小共同决定。长时间运行或重传代价高的 Writer 值得备份；很小、重算便宜的数据可以不备份；足够大的 Block 本身没有碎片读问题，则走 Backup Only，省去再转发和聚合。

这比“所有中间数据双写”更接近成本模型：Backup 的收益是减少失败重算，成本是前台写时延、磁盘空间和 I/O 竞争。系统按 Chunk 选择策略，让正常路径接近 NoBackup，异常路径接近 AllBackup。

#### Shuffle Agent Group、内存水位与多源读取

超大规模下，把所有 Writer 汇聚到一个 Agent 会形成 Incast。FuxiShuffle 先把 Writer 分组，让多个 Agent 分摊一个 Reader 所需的数据；每个 Agent Group 内提供逻辑 Replica，Writer 遇到网络、内存、磁盘或机器故障时切换到另一个 Agent。Reader 最后从整个 Group 聚合 Fragment。

In-memory Shuffle 与 Compute Worker 共机时，Agent 使用 Yellow/Red 双水位：

- 超过 Yellow Line，按优先级平滑 Spill 低优先级数据；
- 超过 Red Line，立即淘汰最低优先级且已有 Backup 的数据，防止 OOM；
- Worker 内存突然增长时动态下调水位，让 Shuffle 内存主动让路；
- Reader 优先走内存 Agent，失败或变慢后切到 Backup。

因此“内存级性能、磁盘级可靠性”不是因为内存数据天然可靠，而是布局规划、优先级、Spill、Backup 与 Reader Failover 共同建立的行为。

#### Incremental Recovery：恢复时不丢 Reader 进度

传统 Partial Re-execution 往往在某段 Shuffle 丢失后终止 Reader，重跑相关 Writer，再让 Reader 从头开始。反复故障会形成“下游丢进度—上游重算—再次失败”的串行循环。

FuxiShuffle 让 Reader 继续处理仍然有效的数据，同时重跑缺失 Writer；新版本到达后只补读缺口。正确性依赖双阶段校验：

1. 恢复前从 Job Manager 获得 Writer Version、`RetryIdx` 与 Checksum，确认缺失范围可定位、现有数据仍有效；
2. 重跑 Writer 产生新版本后，Reader 选择性拉取并验证聚合 Checksum；发现 Missing 或 Duplicate 就放弃增量路径，退回 Full Re-execution。

这个设计优化的不是“重算本身更快”，而是让上游重算与下游有效计算并行，并只丢弃真正不可信的部分。没有 Version 与 Checksum 约束的增量恢复，只会把性能问题变成静默错误。

#### 论文证据，以及数字的边界

论文测试集群包含 20 台计算节点（每台 96 Logical Core、412GB RAM）和 18 台存储节点（每台约 104TB Raw HDD），使用 1TB/10TB TPC-DS 与 TeraSort，对比生产定制的 Hadoop-like、Spark-like Baseline：

- 综合结果中，FuxiShuffle 相对 Baseline 平均降低 76.36% E2E Runtime 和 67.14% CU Cost；
- Progressive Scheduling 实验中，E2E 接近 Gang Scheduling，同时避免其 Reader 等待造成的高 CU Cost；
- 在一次故障实验中，Adaptive Backup 的正常路径接近 NoBackup，异常路径优于 NoBackup 与 AllBackup；
- 随机故障下注入短时网络断连，启用容错后 E2E 和 CU Cost 分别只增加约 9.6% 和 8.1%；关闭容错时 E2E 接近三倍、CU Cost 约翻倍；
- Shuffle Agent 绑定的专用 Core 约占单机 CPU 的 3%–5%。

这些数字来自阿里内部测试集群和定制 Baseline，不应直接等价为“比开源 Hadoop/Spark 固定快 76%”。其中同时叠加了 Agent 聚合、自适应介质、调度、布局和恢复，无法把全部收益归到某一个机制；生产环境的网络超卖、Pangu 配置、任务分布和既有优化程度都会改变结果。

#### 与 Remote Shuffle Service 的关系

Celeborn、Uniffle、Magnet、Riffle 等系统都在不同程度上把 Shuffle 从 Executor 本地盘中解耦。FuxiShuffle 的区别不宜简化成“Push-based RSS”：它同时支持计算节点内存与存储节点磁盘，关注的不只是服务化存储，还包括上游/下游调度解耦、每 Chunk 布局选择、Agent Group Failover 和不中断 Reader 的增量恢复。

Remote Shuffle 的普遍收益是让 Compute Worker 更容易释放和替换，代价则是多一层网络、共享服务容量治理和复杂元数据状态机。是否值得部署，取决于计算弹性、失败频率、网络成本和本地盘资源之间的权衡，不是所有规模都需要独立 Shuffle Service。

#### 我的判断

FuxiShuffle 最有价值的地方，是把“Shuffle 策略”从 Job 启动前的一次静态选择，改成贯穿写入、调度、布局、读取和恢复的连续控制过程。Mode、Reader Start、Backup 和 Recovery 各自都有局部策略，但它们共享 Runtime Telemetry 和元数据版本，最终形成一条闭环。

它也说明中间数据服务不能只追求吞吐。对生产 SQL 平台而言，真正目标是 E2E Runtime、CU Cost、失败放大和多租户隔离的组合。内存命中更高却让 Reader 长时间空等，或者备份更完整却拖慢所有正常 Job，都不是全局最优。

#### 待继续验证

- Job Manager、Shuffle Service Manager 与 Fuxi Scheduler 的状态归属及故障恢复；
- Primary/Backup Index 的持久化、垃圾回收与 `RetryIdx` 并发更新协议；
- Progressive Scheduling 阈值如何结合 Critical Path、Backpressure 与资源碎片动态调整；
- Backup Only 在热点 Partition、跨机网络和 Pangu I/O 之间的完整成本模型；
- Incremental Recovery 在多个 Writer 同时重跑、Partition Scheme 改变时的 Checksum 证明；
- 与 Celeborn、Uniffle 在开源可复现实验上的同硬件比较。

---

## 工业查询优化：从代价模型到反馈闭环

### 引言：优化器真正缺少的不是另一个模型

经典查询优化器建立在一个很优雅的抽象上：统计信息描述数据，代价模型预测执行成本，搜索器在候选计划中选择代价最低者。

```text
Statistics + Cost Model + Search Space
                  |
                  v
             Best Plan
```

这里需要先区分“优化器框架”和“反馈来源”。Volcano/Cascades 解决的是如何用规则、物理属性与 Memo 组织候选搜索：逻辑等价表达式归入同一 Group，Transformation Rule 扩展等价空间，Implementation Rule 产生物理算子，代价模型再做剪枝。后来的 Learned Optimizer、HBO 与自治调优通常没有抛弃这套搜索骨架，而是在基数、代价、候选排序、历史状态或验证环节补充新的信号。

这个抽象没有过时，但工业环境不断击穿它的边界：
- 统计信息会过期，数据湖中的列统计可能根本不存在；
- 共享集群的资源负载持续变化；
- 同一条参数化 SQL 在不同参数下需要完全不同的计划；
- 优化器选出的新计划即使平均更好，也可能让某次关键请求慢上数十倍。

更重要的是，现代数据平台已经积累了过去不曾拥有的资产：数十亿次查询的执行记录、算子级基数、Shuffle 与 Spill 指标、表版本、扫描遥测，以及能够低成本回放或影子验证的存算分离基础设施。于是问题从“能否把代价估得更准”逐渐变成：

> **能否把历史执行统计变成下一次决策的依据，并且让错误决策的代价始终可控？**

本节围绕八项工业数据库工作展开。它们分别来自 MaxCompute、Amazon Redshift、Databricks、OceanBase、Microsoft SQL Server 与 Oracle，看起来涉及学习型优化器、增量统计、参数化查询、索引、数据布局和计划稳定性，实际上都在回答同一个问题：如何构造一条可信的优化闭环。

本节不是摘要合集。阅读重点是四件事：系统观察到了什么，如何把观测表示成可复用状态，如何修改决策，以及如何验证修改不会造成不可接受的回退。

### 阅读地图与结论

#### 八项工作的定位

| 工作 | 公开版本 | 决策对象 | 反馈来源 | 核心方法 |
| --- | --- | --- | --- | --- |
| [Amazon Redshift Re-invented](https://doi.org/10.1145/3514221.3526045) | SIGMOD 2022 | 存储、计算与自治基础设施 | 全平台遥测 | RMS、弹性计算、代码生成与自治组件 |
| [Incremental Query Optimizer Statistics in Amazon Redshift](https://www.amazon.science/publications/incremental-query-optimizer-statistics-in-amazon-redshift) | PVLDB 2026 | CBO 单列统计 | 增量数据 | HLL++、Space Saving、Count Sketch、KLL |
| [Learned Query Optimizer in Alibaba MaxCompute](https://arxiv.org/abs/2602.07336) | SIGMOD 2026 Industry | 候选物理计划 | 历史默认计划与执行环境 | 无统计编码、环境建模、领域自适应 |
| [Ultron](https://doi.org/10.14778/3827998.3828038) | PVLDB 2026 | Join、Runtime Filter、分区数 | 子计划级执行历史 | Softstore、QuickPredict、单调优化应用 |
| [Towards Industrial-Scale Parametric Query Optimization](https://www.vldb.org/pvldb/vol19/p4303-mo.pdf) | PVLDB 2026 | 参数化 SQL 的计划缓存 | 参数—计划—延迟样本 | 模板聚类、分层训练、KL 漂移检测 |
| [LLM-Driven Index Tuning on Microsoft SQL Server](https://arxiv.org/abs/2603.09181) | PVLDB 2026 | 索引集合 | SQL、Schema、Showplan 与实测耗时 | GPT-5 与 DTA 的实证比较、规则蒸馏 |
| [AutoLiquid](https://vldb.org/2026/program.html) | VLDB 2026 Industry | Liquid Clustering Key | 扫描遥测与抽样验证 | 候选筛选、影子验证、验证后提交 |
| [Real-Time SQL Plan Management in Oracle](https://vldb.org/2026/program.html) | VLDB 2026 Industry | SQL Plan Baseline | 前台执行与历史参考计划 | 前台验证、计划接受与回退 |

截至 2026 年 8 月 28 日，AutoLiquid 与 Real-Time SPM 的公开入口主要是 VLDB 2026 会议摘要和产品文档，会议尚未召开。本节对其采用“公开设计预读”，不把摘要之外的实现猜测写成论文事实；其余六项工作均对照公开全文阅读。

#### 先说结论

读完这些工作，我得到九个判断：

1. **工业优化器的发展方向不是用机器学习、神经网络或 LLM 替换 CBO，而是把统计、历史、模型与验证器组合成闭环。** LOAM、Ultron 和 ScalePQO 都保留原生优化器，只在候选生成、代价比较或计划选择处介入。
2. **历史不是一个万能的 `plan_hash -> latency` 缓存，也不应只停留在整条 Plan 粒度。** 可复用历史必须绑定子计划、参数、表规模、Schema 或执行环境，否则“精确的旧答案”可能比粗糙的新估计更危险。
3. **统计信息仍是最便宜、最通用的知识。** Redshift 用可合并 Sketch 降低统计维护成本，并刻意把结果转换回已有 PostgreSQL 风格统计，避免修改优化器热路径。
4. **缺少统计信息时，模型只能学习代理信号。** LOAM 用表标识、访问分区、Join 列、谓词结构和历史成本间接推断数据特征；`statistics-free` 不等于模型不需要数据知识。
5. **不确定性来自两个方向：数据分布和运行环境。** 大多数优化器只处理前者；LOAM 明确指出，即使计划自身不变，共享集群的 CPU、I/O、内存和负载也会改变实际 CPU Cost。
6. **生产安全通常来自单调决策，而不是更复杂的预测器。** Ultron 只在有历史证据时把 SHJ 提升为 BHJ，Runtime Filter 只增不减；Oracle 用已接受计划做锚点；AutoLiquid 在真正改变布局前先验证收益。
7. **LLM 擅长提出候选，不擅长承担最终责任。** GPT-5 能发现 DTA 因代价误差而错过的索引，但同一输入的五次输出波动很大，直接把 LLM 候选塞回 DTA 也可能因为同一个错误代价模型而继续选错。
8. **规模问题最终会变成模型与状态的生命周期问题。** 十万个 Project、五千个 SQL 模板、数亿张表或数十亿次查询，使“每对象一个模型、每次决策一次远程查询”都不可行。
9. **优化的终点不是选出一次更快的计划，而是让系统在变化中稳定收敛。** 这也是本节最核心的主线。

### 一个统一视角：Observe、Represent、Decide、Verify

可以把八项工作统一成下面的控制闭环：

```text
                         +----------------------+
                         |  Workload / Data     |
                         +----------+-----------+
                                    |
                                    v
  +---------+   telemetry   +-------+--------+   features/history
  | Execute | ------------> |    Observe     | -------------------+
  +----+----+               +----------------+                    |
       ^                                                              v
       |                                                        +-----+------+
       |                                                        | Represent  |
       |                                                        +-----+------+
       |                                                              |
       |                                                   candidates / scores
       |                                                              v
  +----+----------------+    guardrail / validation             +-----+------+
  | Apply or Roll Back  | <------------------------------------ |   Decide   |
  +---------------------+                                      +------------+
```

四个阶段缺一不可：

- **Observe**：收集增量数据、算子实际行数、表大小、集群负载或 SQL 执行时间；
- **Represent**：把观测压缩为 Sketch、计划 Hash、Embedding、Plan Baseline 或候选布局；
- **Decide**：选择物理计划、Join 算法、Runtime Filter、分区数、索引或聚簇键；
- **Verify**：通过保守阈值、单调状态机、影子评估、实测执行或基线比较限制回退。

传统 CBO 主要覆盖 Represent 与 Decide。本节这些工作真正新增的是 Observe 与 Verify，并把四者连接成持续运行的系统。

### Redshift Re-invented：先建立能够反馈的系统底座

#### 论文信息

- 论文：[Amazon Redshift Re-invented](https://doi.org/10.1145/3514221.3526045)
- 出处：SIGMOD 2022 Industry
- 阅读状态：精读

#### 这篇论文为什么放在最前面

《Amazon Redshift Re-invented》并不是一篇学习型优化器论文，却解释了后续自治能力为什么可能成立。没有统一遥测、共享存储、弹性计算、异步服务与可重用编译缓存，所谓“持续学习”很容易沦为一个离线实验。

早期 Redshift 是典型 Shared-Nothing MPP：Leader 负责解析与优化，Compute Node 持有数据分片并执行。它的优势是数据本地性强，问题是数据与计算绑定：扩缩容要移动数据，存储与计算不能独立增长，并发负载也会争用同一集群。

重构后的逻辑结构可以简化为：

```text
                       Leader / Optimizer
                              |
             +----------------+----------------+
             |                                 |
       Compute Cluster                   Concurrency Cluster
       Memory + Local SSD                Memory + Local SSD
             |                                 |
             +---------------+-----------------+
                             |
                  Redshift Managed Storage
                    S3 as source of truth
```

#### RMS 不是“把磁盘换成 S3”

Redshift Managed Storage（RMS）的关键是重写状态归属：数据和事务元数据持久化到 S3，本地 SSD 与内存成为缓存层。RMS 依据 Block 的温度、年龄和访问模式预取与替换，并使用两级 Clock 策略区分首次访问的冷 Block 与重复访问的热 Block。

这个设计带来三项后果：

1. 计算节点损失本地盘不再意味着数据丢失，节点更接近可替换资源；
2. Elastic Resize 主要迁移元数据和重新建立缓存，而不必复制整份底层数据；
3. Concurrency Scaling 与 Data Sharing 可以让多个计算集群直接访问同一份已提交数据。

论文明确写的是 Redshift 列式数据以 **1 MiB Block** 存在 RMS 中。Block 越大，远程读取、压缩和元数据摊销通常越好，但随机访问与无效读取也越多；Block 越小，细粒度访问更灵活，却会增加元数据、请求调度和对象访问成本。这里的 1 MiB 是列式扫描、S3 吞吐与本地缓存粒度之间的工程折中，而不是适用于所有存储系统的常数。

#### 执行器优化仍然重要

存算分离并没有让单机执行效率变得不重要。Redshift 仍然大量使用代码生成，并针对 CPU Cache Miss 显式生成 Prefetch。其思路是让哈希表 Probe 或 Bloom Filter 访问提前发出内存预取，再用 L1 Cache 中的小型循环缓冲隐藏访存延迟。

Vectorized Execution 与 Code Generation 解决的是相邻但不同的问题：前者让一组固定 Kernel 按列式 Batch 工作，用摊薄解释器开销和更规则的数据访问换取稳定执行；后者根据当前表达式、数据类型与算子 Pipeline 生成专用机器码，减少虚函数分派、中间结果和无效分支，但会产生编译延迟与代码缓存压力。工业系统通常不是二选一：常见算子使用成熟向量化 Kernel，热点表达式或 Pipeline 再通过 Codegen 专门化，并用编译缓存摊薄冷启动。

编译本身又可能成为短查询的冷启动成本，因此 Redshift 把编译服务外置，使用 Cluster 外部 CodeCache 复用编译结果。

AQUA 则不是普通远程块存储接口，而是把过滤与聚合等计算靠近缓存数据执行的功能型接口。

这一层工程细节很重要：云原生不是“所有东西远程化”，而是在耐久状态、缓存状态与计算状态之间重新划分边界。

#### 自治组件为什么能够出现

论文还介绍了 Automatic Table Optimization、AutoWLM 与自动物化视图等能力：

- ATO 根据列访问、谓词选择率和 Join 图推荐 Distribution Key 与 Sort Key；
- AutoWLM 根据计划特征预测执行时间、内存与编译时间，动态控制并发；
- 自动物化视图综合查询收益与刷新成本，决定创建、刷新和重写。

这些功能的共同基础不是“用了 ML”，而是系统已经能稳定收集查询、数据和资源遥测，并把后台动作与前台查询隔离。后面的 LOAM、Ultron 与 AutoLiquid 都建立在同样的工程前提上。


### Redshift 增量统计：把全表 ANALYZE 变成可合并状态

#### 论文信息

- 论文：[Incremental Query Optimizer Statistics in Amazon Redshift](https://www.amazon.science/publications/incremental-query-optimizer-statistics-in-amazon-redshift)
- 出处：PVLDB 2026
- 阅读状态：精读

#### 问题不是统计不准，而是统计来得太晚

对十亿乃至百亿行表，重新扫描全表收集统计可能持续数小时。论文分析 Redshift Fleet 后发现，超过 100 亿行的大表，其 ANALYZE 时长 P90 超过 10 小时；涉及时间列谓词时，陈旧统计相较新鲜统计会带来 25 倍的 Q-Error。

传统 ANALYZE 会扫描全表或样本，构造 PostgreSQL 风格的单列统计：

- Row Count、Average Width、Null Fraction、Min/Max；
- NDV；
- Most Common Values（MCV）及频率；
- 去除 MCV 后的压缩等深直方图。

真正困难的是，优化器需要的不是一种 Sketch，而是一组语义不同的统计量。

#### 四种 Sketch 各司其职

| 统计目标 | 数据结构 | 为什么选择它 |
| --- | --- | --- |
| NDV | HLL++ | 常数级插入、可合并、已有工程基础 |
| MCV 候选 | Space Saving | 保证高频项进入候选集合 |
| MCV 频率 | Count Sketch | 对候选值提供更准确、近似无偏的频率估计 |
| 直方图分位点 | KLL | 有界空间、可反复合并，长期维护不会无限增长 |

这组组合很有工程意味。Space Saving 擅长“找谁可能是热点”，但频率估计不够精；Count Sketch 擅长“这个候选到底出现多少次”；KLL 负责 Quantile；HLL++ 独立承担 NDV。统一所有任务的数据结构理论上更漂亮，却可能更慢、更大。

论文给定参数下，四种 Sketch 的组合单线程吞吐约为每秒 1550 万次插入，占用约 90 KiB。这个数字说明在生产系统里，“算法渐进复杂度正确”远远不够，逐行 Hash、Cache 行为和序列化格式同样决定能否上线。

#### 层次化合并与 Delta ANALYZE

完整流程如下：

```text
Partition / Slice
  -> construct per-column local sketches
  -> merge on each Compute Node
  -> merge again on Leader Node
  -> convert sketches to existing optimizer statistics
  -> transactionally persist sketches and statistics
```

第一次运行仍需 Full-Scan Bootstrap。之后 Delta ANALYZE 只扫描上次分析后新插入、且对当前 MVCC Snapshot 可见的数据。每个分区先生成增量 Sketch，再逐层 Merge，最后与 Sketch Store 中的旧状态合并。

这里有一个很值得借鉴的决策：**Sketch 不直接进入优化器热路径。** 系统先把 Sketch 转换成已有 Catalog 中的 NDV、MCV 和 Histogram，优化器无需理解 KLL 或 Count Sketch，也不增加每次规划时的 Sketch 查询开销。它牺牲了一部分新算法表达能力，换取兼容性、可灰度性与故障隔离。

#### 直方图并不是简单查询 KLL

Redshift 使用去除 MCV 后的压缩直方图。KLL 中仍然包含 MCV，因此构造每个等深边界时，需要先查询一个 Rank，再累加落在当前边界之前的 MCV 频率，调整 Rank 后重新查询，直到边界稳定。

简化表达如下：

```text
target rank over non-MCV values
  -> query KLL
  -> count MCV mass before current boundary
  -> shift target rank
  -> repeat until boundary no longer changes
```

这段算法体现了从 Sketch 到优化器统计的真正难点：不同概要各自近似正确，并不意味着组合后的统计语义自动正确。

#### Sampling、Delete 与关键路径取舍

Full Sketch Bootstrap 的逐行成本高于旧的 Sample ANALYZE。系统最终只让 Space Saving 和 KLL 处理 25% 样本，HLL++ 与 Count Sketch 仍处理全量数据。原因是 NDV 对采样敏感，而 Count Sketch 可以复用 HLL 的 Hash 结果。

系统也没有强行让所有 Sketch 支持 Delete。Redshift 的目标负载以 Append 为主；当累计删除达到 10% 时，触发 Full-Scan 重建。论文给出的 Fleet 数据显示，86.6% 的表没有 Delete，发生 Delete 的表中约 85% 每天只删 10 行。对目标负载而言，复杂的可删除 Sketch 不值得其吞吐和空间代价。

另一个失败尝试更有启发：团队最初把 Sketch 更新放在 Insert Path，希望用 S3 延迟掩盖开销，但实测仍明显拖慢 INSERT、COPY 和 CTAS，于是退回异步 Automatic ANALYZE。**最及时的反馈不一定是最好的反馈；写入关键路径的预算比新鲜度更硬。**

#### 实验证据与边界

在从 100 亿行开始、每轮增长 1% 的 10 TB TPC-H `lineitem` 实验中：

- 25% Sampling 的首次 Sketch Bootstrap 约为旧 ANALYZE 的 2.8 倍；
- 后续 Delta ANALYZE 最高快 68.4 倍；
- 以 1% 增长触发增量统计、以 10% 增长触发旧 ANALYZE 时，累计成本在 34 轮后反超，同时统计新鲜度提高 10 倍；
- 在数千个生产集群上线后，大表每周统计收集总计算时间降低约 40%。

论文的目标是“以更低累计成本维持相同统计质量”，不是证明所有查询都变快。多列相关性仍然超出这些单列统计的表达范围；高频 Update/Delete 负载也会增加 Full Rebuild。它是面向追加型数仓的设计，不应直接外推到 OLTP。

### LOAM：当统计缺失、环境未知且不能在线试错

#### 论文信息

- 论文：[Learned Query Optimizer in Alibaba MaxCompute: Challenges, Analysis, and Solutions](https://arxiv.org/abs/2602.07336)
- 出处：SIGMOD 2026 Industry
- 阅读状态：精读

#### 四个生产约束

LOAM 的出发点不是“神经网络比 CBO 更聪明”，而是 MaxCompute 让许多学术 LQO 的默认假设失效：

1. 平均 5000 台机器的资源池动态调度，同一计划的执行环境不同；
2. 海量数据和频繁修改使 Histogram 等统计可能缺失或陈旧；
3. 候选计划代价很高，不能为训练而在线执行危险计划；
4. 超过 10 万个 Project 的收益差异巨大，无法每个 Project 都训练并部署模型。

因此 LOAM 的目标不是替代 MaxCompute 原生优化器，而是 Steering：原生优化器生成候选，学习模型负责比较候选，最终仍由成熟执行栈运行。

#### Plan Explorer：先限制搜索空间的风险

MaxCompute 暴露 75 个可调 Flag，涉及执行模式、Join、Shuffle、Spool、Filter 与并行度等。论文实验只选择其中 6 个相对安全、容易产生多样计划的 Flag；同时借鉴 Lero，对至少三个输入的子查询缩放估计基数，以影响计划结构。

这说明 30% 的最高收益不能理解为“模型任意生成了全新计划”。LOAM 的上限首先受 Plan Explorer 限制：候选集合中没有好计划，再准确的排序器也无能为力。

#### Statistics-Free 到底编码了什么

`Statistics-Free` 的准确含义是“不把维护好的 Histogram、NDV 等作为必要输入”，而不是完全不需要数据特征。LOAM 从算子语义与稳定标识中学习间接信号：

- TableScan：表标识、访问分区数、访问列数；
- Join：Join 类型、Join 列标识；
- Aggregate：聚合函数、聚合列与 Group-By 列；
- Filter：谓词中函数的 Multi-Hot、涉及列的 Hash 编码。

对于很深的谓词表达式树，LOAM 不编码完整 AST，而只保留函数集合与列标识。它承认这是粗粒度信息，再依靠重复工作负载的历史成本学习表、列与谓词的隐式数据分布。

#### Tree Convolution 与环境特征

计划树经过三层 Tree Convolution，通道数依次为 256、128、64；Pooling 后经过全连接层形成 32 维 Plan Embedding，再由一个简单 Cost Head 预测 CPU Cost。论文也比较了 Transformer、GCN 与 XGBoost，模型结构本身不是主要贡献。

LOAM 在每个 Stage 上编码四类运行环境指标：

```text
CPU_IDLE    IO_WAIT    LOAD5    MEM_USAGE
```

指标每 20 秒采样，并在 Stage 执行窗口及分配机器范围内求平均。训练时，模型同时看到计划结构与实际环境，从而尝试分离“计划内在成本”和“当时机器有多忙”。LOAM 预测 CPU Cost，而非更容易受排队和网络长尾影响的端到端时延。

线上规划时，未来机器尚未分配，环境当然不可知。论文证明：不知道未来环境的模型与知道真实环境的 Oracle 之间存在不可消除的期望差距。工程上，LOAM 把归一化环境特征设置为接近历史均值的代表值，以估计平均环境下的计划成本。这里不是“预测了未来”，而是选择了一个稳定的期望近似。

#### 领域自适应为什么不需要执行候选计划

训练数据主要是原生优化器产生并实际执行的默认计划，而线上要评分的是 Flag 与基数缩放产生的候选计划，二者存在 Covariate Shift。

LOAM 增加 Domain Classifier，判断某个 Embedding 来自默认计划还是候选计划；Plan Encoder 前使用 Gradient Reversal Layer：

```text
Cost loss:     encoder learns plan features useful for CPU prediction
Domain loss:   classifier learns to separate default / candidate plans
Reversed grad: encoder learns to make the two domains indistinguishable
```

候选计划只需生成结构，不需要执行得到真实 Cost。Domain Classifier 想分清两个域，Plan Encoder 反过来消除域特征，使默认计划上训练的 Cost Head 更可能泛化到候选计划。

这个方法降低了在线探索风险，但并不保证候选域与训练域完全对齐。若 Plan Explorer 未来加入新的算子或完全不同的计划形态，仍需要重新评估表示与分布漂移。

#### Project Selector：先决定哪里值得用 AI

LOAM 先用规则过滤训练数据不足、表生命周期太短或计划结构不合适的 Project，59.5% 的 Project 会被排除。剩余 Project 再由 XGBoost Ranker 根据默认计划结构、输入规模和历史 CPU Cost 估计“优化空间”，最终只部署到 Top-N。

论文对整体覆盖给出的是一个保守估算：40.5% 的 Project 通过规则；随机抽取的 30 个 Project 中，约 10% 获得不低于 10% 的收益，因此估计至少约 4% 的 Project 能获得不低于 10% 的提升。这个数字不是“LOAM 已覆盖全体 Project 的 4%”，而是基于样本和当前保守 Plan Explorer 的推断。

#### 如何理解“最高 30%”

论文在五个高潜 Project 的 Flighting 回放中报告最高约 30% 的 CPU Cost 节省；不同 Project 差异明显，部分查询也会变慢。作者把五个高潜 Project 的结果视为随机样本中的上界，并没有宣称所有工作负载平均提升 30%。

LOAM 最值得吸收的不是一个最高数字，而是三个生产化原则：**显式建模不可控环境、用领域自适应代替危险试跑、先筛选值得部署的租户。**

### Ultron：记住 AQE 的修正，让下一次少走弯路

#### 论文信息

- 论文：[Ultron: History-Based Query Optimization at Databricks](https://doi.org/10.14778/3827998.3828038)
- 出处：PVLDB 2026
- 阅读状态：精读

#### History-Based Optimization 与 Learned Optimizer 的差别

Databricks 每天处理数十亿查询，很多 Lakehouse 表缺少精细统计，但工作负载高度重复。论文统计：交互式 Runtime 中，前一周逐字重复的查询约占 45%～64%；DBSQL 为 67%～80%；约 85% 的 Join 会重复出现，这些 Join 占总执行时间的 73%。

Ultron 不训练通用神经网络预测未知计划，而是保存具体事实：某个子计划实际产生多少行、是否需要重分区、某个 Runtime Filter 是否有效。它的优势是决策有 Provenance，劣势是冷启动时几乎帮不上忙。

#### 三层架构：全局保存，本地决策

```text
Softstore
  distributed, tenant-isolated history cache
       |
       | push
       v
QuickPredict on cluster
  local hash lookup + drift awareness
       |
       v
Ultron Applications
  Join / Runtime Filter / Partition Count
```

Softstore 保存跨 Cluster 的历史，但不会让优化器同步远程读取。它主动把相关 Namespace 推到 Cluster 本地；网络抖动或服务故障时，优化器只是暂时不用历史，而不会阻塞规划。

QuickPredict 对子计划计算 Hash，支持三种精度：

- Literal-Sensitive：计划与参数常量完全一致；
- Literal-Insensitive：忽略常量，复用同模板历史；
- Plan-Shape：更模糊的结构匹配。

历史 Key 还包含由输入表规模计算的 Size Factor。表小幅变化时会探测相邻 Size Factor 与 Literal-Insensitive Key，避免刚跨边界就完全冷启动；表增长明显时则不再盲用远古历史。

出于隐私考虑，Softstore 按客户隔离，不保存 SQL 文本或 Literal，只保存不可逆的计划 Hash 与特定统计。这个选择同时服务性能、隔离与数据最小化。

#### 应用一：从 SHJ 单向提升到 BHJ

Broadcast Hash Join（BHJ）省去双侧 Shuffle，但 Build 侧过大时可能 OOM；Shuffle Hash Join（SHJ）更保守，但小 Build 表也要支付网络重分区成本。

Ultron 的策略不是在两者之间来回探索：

1. 无历史时沿用保守的 SHJ；
2. 执行后记录真实 Build Cardinality；
3. 下次只有在历史证明 Build 足够小时，才把 SHJ 提升成 BHJ；
4. Join 算法的改变不会反过来改变 Build Cardinality，因此一次修正即可收敛。

这是一种单向、可解释的优化。生产结果显示，Ultron 优化了全平台约 23% 的 Join，符合条件 Join 的中位时延降低约 25%，事后决策准确率约 96%。论文也明确存在错误决策，例如高倾斜表在逻辑大小可广播时仍可能导致问题。

#### 应用二：逐层加入 Runtime Filter

Runtime Filter 的收益依赖上游 Join 的实际选择率，而某个 Filter 加入后又会改变下游 Cardinality。这是典型的反馈依赖问题。

Ultron 利用单调性避免震荡：一个有效 Runtime Filter 只会减少或保持下游行数，不会把先前的 Filter 变得无效。系统每轮根据新历史多加入一层有收益的 Filter，直到没有候选达到阈值。

```text
iteration 1: observe first selective join
iteration 2: add RF-1, observe reduced downstream cardinality
iteration 3: add RF-2 if it now becomes profitable
...
converge: no additional filter passes the threshold
```

论文在 TPC-H 合成实验中显示，历史选择的 Runtime Filter 可对部分查询获得约 2 倍改善。但这一应用在论文撰写时尚未像 Join Selection 一样全面生产部署，不能把合成结果当作 Fleet 平均收益。

#### 应用三：记住 AQE 找到的分区数

AQE 可以发现分区过大，再把热点分区拆小；正确性和健壮性得到保障，但第一次执行已经支付重分区成本。Ultron 记录 AQE 最终采用的分区数，下一次直接从这个值开始。

我认为这是 Ultron 最凝练的设计：

> **HBO 可以被理解为 AQE 的长期记忆。AQE 修复当前查询，HBO 让相同修复不必再次发生。**

#### 边界：历史越精确，复用范围越窄

Ultron 的主要限制包括：

- Schema 变化目前会使相关历史整体失效，即使只是增加一个无关列；
- 新 Query、新客户与一次性 Ad-hoc Query 缺少历史；
- 跨租户泛化会引入隐私泄漏风险；
- 异步缓存使问题复现更难，需要计划决策 Provenance 与时间旅行调试；
- 更多 Ultron Application 可能让规划开销非线性增长。

论文报告符合条件查询的额外优化时间中位数低于 5%，但 P95 在峰值观测中可到 12.6%。系统规模不是“缓存查找 O(1)”就自动解决的，状态推送、TTL、容量预留、Schema 失效与调试工具同样重要。

### ScalePQO：一个模板一个模型，也是一种不可扩展

#### 论文信息

- 论文：[Towards Industrial-Scale Parametric Query Optimization](https://www.vldb.org/pvldb/vol19/p4303-mo.pdf)
- 出处：PVLDB 2026
- 实现：[ScalePQO / RankPQO Industry Artifact](https://github.com/songsong945/RankPQO-industry)
- 阅读状态：精读

#### 参数化查询的两个子问题

同一 SQL 模板使用不同参数时，数据选择率和最佳计划可能发生突变。PQO 通常拆成：

1. 离线产生一小组覆盖不同参数区域的 Candidate Plan；
2. 在线根据当前 Parameter Vector，从缓存中选出最佳计划。

OceanBase 自身会按参数选择率复用计划并在 Miss 时生成新计划。RankPQO 则同时编码 Parameter 与 Plan，通过 Learning-to-Rank 比较候选计划的相对性能，再选出有限的 Plan Cache。

#### 工业规模击穿两个极端

一模板一模型精度高，但 3300 个模板需要超过 3.4 GiB 模型存储与超过 1000 小时的数据收集；所有模板共享一个模型虽然便宜，Speedup 会从 33 个模板时的 2.34 倍下降到 3300 个模板时的 1.18 倍。

ScalePQO 选择中间路线：

```text
all templates
   -> train global ranking model
   -> derive performance-related template embeddings
   -> cluster similar templates
   -> fine-tune one specialized model per cluster
```

关键不是用 SQL Edit Distance 聚类，而是用全局模型学到的表示聚类。语法相似不代表计划性能曲线相似；Embedding 试图捕获 Parameter、Plan 与性能之间的关系。

#### 参数漂移要同时更新模型与 Plan Cache

ScalePQO 把到达的 Parameter Vector 按时间顺序划成 Slot，计算最近两个 Slot 分布之间的 KL Divergence。漂移越明显，后台 Fine-Tune 的强度越大，同时从新分布中补充 Candidate Plan。

Plan Cache 更新采用 Prepend：新计划放在前面，淘汰最旧计划。论文实验中它优于随机替换，说明时间局部性比无差别保留更适合演化负载。

这里有两个独立状态必须更新：

- Ranking Model 决定相同候选集里选谁；
- Candidate Set 决定系统有没有覆盖新参数区域的计划。

只 Fine-Tune 模型而不更新候选集，模型只能在过时选项中挑一个相对不差的；只换计划不更新模型，新计划也可能被错误排序。

#### 证据与边界

ScalePQO 集成在 OceanBase 上，在六组工作负载中相对原生优化器最高加速 1.62 倍，相对 RankPQO 最高 1.23 倍。5000 模板实验中，模型约 759 MiB；逐模板 RankPQO 约 5712 MiB。在线 Fine-Tune 与 Plan Update 在后台和 Query Execution 并行，不进入当前请求延迟。

但论文明确假设底层数据分布相对稳定，主要处理 Parameter Distribution Drift。数据本身发生倾斜或统计变化时，即使参数分布不变，计划相对性能也会改变。Embedding Mean Pooling 可能丢掉多峰分布，K-Means 也不一定是最佳聚类算法；最终质量仍受 Candidate Enumeration 上限约束。

### LLM 索引调优：最好的一次回答不是可部署系统

#### 论文信息

- 论文：[Evaluating the Practical Effectiveness of LLM-Driven Index Tuning on Microsoft SQL Server](https://arxiv.org/abs/2603.09181)
- 作者：Xiaoying Wang、Wentao Wu、Vivek Narasayya、Surajit Chaudhuri
- 出处：PVLDB 2026
- 阅读状态：精读

#### 这篇论文真正评测了什么

Microsoft 工作不是 SIGMOD 2024 论文，而是 2026 年 PVLDB 工作；作者为 Xiaoying Wang、Wentao Wu、Vivek Narasayya 与 Surajit Chaudhuri。论文最终报告基于 **GPT-5**，每个输入独立调用五次，并与确定性的 SQL Server DTA 比较。

输入包括 SQL、Schema、现有索引和 Showplan。数据集包含 TPC-H SF10 的 22 条查询，以及四组真实企业工作负载，共 127 条单查询 Case。评价使用真正的执行时间，而不是只看优化器 Estimated Cost。

#### Best-of-Five 揭示潜力，也隐藏选择器

五次回答中取最好结果时：

- 约 67% 的单查询 Case 与 DTA 相当或更好；
- 约 31% 比 DTA 至少快 20%；
- LLM 往往推荐更少的索引；
- LLM 的优势集中在 DTA 被错误 Cardinality / Cost Estimate 误导的查询。

但“取五次中最好”隐含了一个尚未解决的 Oracle：系统必须先知道哪次最好。最差结果常明显落后 DTA，甚至产生 Timeout 或接近 10 倍的回退。同一 Prompt 的波动不是附属问题，而是生产部署的核心问题。

#### LLM 与 DTA 的错误并不在同一个空间

DTA 的搜索由 What-If Cost 驱动，稳定、可约束，但会继承优化器的估计误差。GPT-5 根据 SQL 结构、Showplan 与训练语料中的 DBA 经验提出索引，不直接受这个 Cost Model 约束，因此偶尔能跳出 DTA 的局部错误。

然而，这不表示 LLM 会计算物理代价。它更像一个具有高方差的启发式候选生成器：能看到 Scan、Join、Filter 与 Covering Index 之间的语义关系，但无法可靠权衡索引维护成本、跨查询收益和存储预算。

#### 为什么“把 LLM 候选交给 DTA”仍可能失败

一个看似自然的混合方案是扩充 DTA Candidate Pool，再由 What-If Cost 选最终索引。论文发现，这种直接集成经常无收益甚至退化。

原因很直接：DTA 原本就是因为 Cost Estimate 错误而错过 LLM 的好索引；候选进入同一个错误评估器后，仍可能被拒绝。即使 DTA 选中 Estimated Cost 更低的新索引，真实执行也可能更慢。

```text
LLM finds a candidate outside DTA's original search space
                      |
                      v
same inaccurate What-If cost model evaluates it
                      |
          +-----------+-----------+
          |                       |
   good index rejected      bad index accepted
```

这个结果提醒我们：扩大 Search Space 不会自动修复 Objective Function。

#### 多查询为什么更难

多查询 Workload 要在索引数或空间预算下平衡共享收益。GPT-5 容易被大量上下文分散注意力，过度寻找跨查询共同模式，却忽略真正占据大部分执行时间的少数瓶颈查询。随着 Workload 从单条增加到四条，某个热点查询获得 DTA 同等或更好方案的概率可从约 50% 降到 5% 以下。

DTA 在多查询场景整体更稳定。LLM 偶尔仍能找到 DTA 没枚举到的共享索引，但这不足以构成可依赖的全局组合优化器。

#### 从 LLM 中蒸馏规则，比在线调用更可靠

作者观察 GPT-5 的 Reasoning，提炼出一个确定性 Rule-Based Tuner：优先处理大表上的高成本 Scan，根据谓词与 Join 构造 Key，补充 Covering Columns，忽略收益很小的小表索引。这个简单规则恢复了相当一部分 LLM 优势，且没有采样方差。

这是全文最有价值的结论：

> LLM 的知识不一定要以“每次在线询问 LLM”的方式部署。先用 LLM 发现启发式，再把启发式蒸馏成可测试、可审计的程序，可能更适合系统软件。

真正执行候选索引做验证当然能找出五次回答中最好的一个，但 Index Creation 与 Workload Execution 的成本通常显著高于调优本身。低成本、低扰动的验证机制，才是 LLM Index Tuning 走向生产的主要瓶颈。

### AutoLiquid：推荐聚簇键不难，自动应用才难

#### 论文信息

- 论文：[AutoLiquid](https://vldb.org/2026/program.html)
- 出处：VLDB 2026 Industry
- 产品文档：[Use Liquid Clustering for Tables](https://docs.databricks.com/aws/en/tables/clustering)
- 阅读状态：公开设计预读

#### 从“推荐”走向“自治”

Lakehouse 中，Clustering Key 决定文件 Min/Max 能否有效 Data Skipping。传统系统通常让用户人工选择 Key；当平台管理数亿张表时，人工方式无法扩展，而且 Workload 会变化。

AutoLiquid 只要求用户声明：

```sql
CLUSTER BY AUTO
```

公开摘要给出的闭环分三步：

1. 从 Scan Telemetry 中用轻量启发式生成候选 Clustering Key；
2. 在抽样数据上做快速 Shadow Verification，测量真实 Pruning Benefit；
3. 只有验证后的 Key 才会 Commit 并应用到 Liquid Clustering Table。

```text
scan telemetry
   -> candidate keys
   -> sampled shadow verification
   -> verified improvement?
          yes -> apply key
          no  -> keep current layout
```

#### 存算分离为什么是验证器的一部分

在传统存算一体系统中，验证新布局可能意味着复制整表或抢占线上节点。Lakehouse 的持久数据与计算资源解耦后，可以临时分配计算、读取相同 Snapshot 的抽样数据并评估候选，而不必先修改生产表。

这与 LOAM 的“不能在线执行危险计划”形成有趣对照：查询计划可能在一次执行中耗费巨大资源，因而 LOAM 用 Domain Adaptation 避免试跑；数据布局改变更慢，但可在独立计算上对样本做 Shadow Verification，AutoLiquid 因而选择“验证后提交”。验证策略取决于系统能否廉价隔离副作用。

#### 已公开证据与尚不能确认的细节

会议摘要报告：相较用户人工选择的 Key，AutoLiquid 在超过 95% 的评测表上达到相当或更好的性能，并已在 Databricks 生产管理数百万张表。

当前公开摘要没有足够信息证明早期草稿中的以下说法：一定使用 Hilbert Curve、具体 Overlap Ratio 阈值、文件冲突选择算法或写放大数字。因此本节不把它们作为既定实现。可以确认的是候选来自 Scan Telemetry，决策经过 Sampled Shadow Verification，且系统遵循 Verify-Before-Commit。

### Oracle Real-Time SPM：先让新计划发生，再阻止它继续发生

#### 论文信息

- 论文：[Real-Time SQL Plan Management in Oracle](https://vldb.org/2026/program.html)
- 出处：VLDB 2026 Industry
- 产品文档：[Overview of SQL Plan Management](https://docs.oracle.com/en/database/oracle/oracle-database/26/tgsql/overview-of-sql-plan-management.html)
- 阅读状态：公开设计预读

#### Plan Stability 与 Plan Evolution 的矛盾

Stored Outline 或固定 Plan 可以阻止升级、统计刷新、DML 与新索引引起的回退，但也会阻止真正更好的计划。Oracle 11g 引入 SQL Plan Management（SPM）：维护 Accepted Plan Set，新计划只有经过验证才能进入 Baseline。

传统验证主要由后台任务完成。在 Autonomous Cloud 中，自动升级、统计维护和索引变化更频繁，而后台资源有限，回退可能在被发现前持续很久。

Real-Time SPM 把验证移到 Foreground Session：Hard Parse 发现新计划时，从 Automatic SQL Tuning Set 中找历史 Reference Plan；新计划至少执行一次，执行结束后与 Reference 的真实表现比较，再创建或更新 SQL Plan Baseline。

#### 必须纠正“影子执行并中途熔断”的误读

早期草稿把 Real-Time SPM 描述为基线计划服务用户、候选计划后台影子执行，并在超过 `1.5 × baseline` 时强制中断。公开论文摘要与 Oracle 官方文档并不支持这个机制。

更准确的流程是：

```text
hard parse selects a previously unseen plan
        |
        v
foreground executes the new plan at least once
        |
        v
compare observed performance with a historical reference plan
        |
        +-- better -> accept into SQL plan baseline
        |
        +-- worse  -> reject / enforce better known plan later
```

所以 Real-Time SPM 能快速阻止**后续执行**继续使用退化计划，但不能保证第一次新计划完全没有用户可见的回退。它把检测窗口从后台任务的小时级缩短到前台执行周期，而不是创造零成本预知。

#### 状态与可观测性

SPM 的状态落在 SQL Management Base 中，包含 SQL Signature、Accepted / Unaccepted Plan 与 Baseline。`DBA_SQL_PLAN_BASELINES` 中的 `FOREGROUND_LAST_VERIFIED`、`ORIGIN` 和 `NOTES` 可用于识别前台验证。

相比 Ultron，Oracle SPM 更保守：它不需要泛化到相似子计划，而是围绕同一 SQL 的已知计划集合做接受控制；相比离线 Shadow Verification，它直接使用用户前台执行的真实结果，反馈最准，但第一次探索成本由真实请求承担。

截至本节合并时，VLDB 2026 摘要确认 Real-Time SPM 已部署在 Oracle 生产环境，Oracle 26ai 文档也描述了前台验证流程；公开信息尚不足以支持早期草稿中的“0 次重大回退”“监控开销低于 1%～2%”等数字，故不引用。

### 横向比较：八种系统到底在学习什么

#### 决策闭环对照

| 系统 | 观察值 | 持久状态 | 修改动作 | 安全机制 | 冷启动行为 |
| --- | --- | --- | --- | --- | --- |
| Redshift Incremental Stats | 新增行与 MVCC Delta | 合并后的列 Sketch | 更新 NDV / MCV / Histogram | 保留原 Catalog 接口，删除达阈值全量重建 | 首次全表 Bootstrap |
| LOAM | 默认计划 CPU Cost、Stage 环境 | 每 Project Cost Model | 候选计划排序 | 保守 Explorer、领域自适应、Project 筛选 | 依赖历史训练数据 |
| Ultron | 子计划行数、AQE 修正 | Plan Hash 对应的精确事实 | Join、RF、Partition Count | 单调应用、无历史则沿用原优化器 | 退化为原生 CBO / AQE |
| ScalePQO | 参数、候选计划、真实延迟 | Cluster Model + Plan Cache | 参数实例选计划 | 异步更新、有限候选集 | 使用初始训练模型 |
| LLM Index Tuning | SQL、Schema、Showplan | 候选索引与推理文本 | 建议索引集合 | 当前仍依赖昂贵实测验证 | 可直接生成，但方差高 |
| AutoLiquid | Scan Telemetry | 当前 Key 与候选收益 | 更新 Clustering Key | Sampled Shadow Verification | 保留当前布局 |
| Oracle Real-Time SPM | 新计划真实执行表现 | Accepted Plan Baseline | 接受或拒绝新计划 | Reference Plan 比较 | 首个新计划仍需执行 |

#### Learned、History-Based 与 Autonomic 不应混为一谈

三类方法的知识来源不同：

- **Learned Optimizer**：从许多 Plan-Cost 样本学习函数，能够在一定范围内泛化；
- **History-Based Optimizer**：复用某个精确 Query / Subplan 的实际历史，泛化少但 Provenance 强；
- **Autonomic Controller**：观察系统、提出动作、验证收益、持续调整，模型可能只是其中一个组件。

LOAM 更接近第一类，Ultron 是第二类，AutoLiquid 与 Real-Time SPM 更接近第三类。ScalePQO 位于 Learned 与 History-Based 之间：模型负责跨参数选择，Plan Cache 与漂移检测负责持续适应。

#### “无统计”与“用历史”并不是对立路线

Redshift 说明统计仍是通用 CBO 最便宜的输入；LOAM 说明统计无法及时维护时，可以从历史计划与算子语义学习代理表示；Ultron 则在重复负载上绕过估计，直接复用真实结果。

更完整的系统可能同时使用三层知识：

```text
fresh statistics available?
  yes -> CBO estimate
  no  -> learned proxy from plan semantics

exact matching history available?
  yes -> override selected high-confidence decisions

runtime sees unexpected reality?
  yes -> AQE repairs current execution and records feedback
```

问题不再是“CBO 还是 AI”，而是如何定义优先级、置信度、失效条件和回退路径。

### 从论文中抽象出的生产设计原则

#### 让知识靠近决策，但让收集远离关键路径

Ultron 把历史 Push 到 Cluster 本地，避免 Planner 远程查询；Redshift 放弃 Insert-Path Sketch，转用异步 ANALYZE；LOAM 在线只做候选评分，训练留在离线。

统一原则是：**读侧要近，写侧可异步。** 反馈晚一轮通常可以接受，阻塞每一条查询或写入则不可接受。

#### 先定义失效协议，再讨论预测精度

任何历史与模型都会过期：

- Redshift 用 Insert / Delete 比例触发刷新或重建；
- Ultron 用 Size Factor 与 Schema Change 失效历史；
- ScalePQO 用 KL Divergence 检测 Parameter Drift；
- LOAM 用稳定表比例筛除不适合训练的 Project；
- Oracle 用 Reference Plan 与 Accepted Baseline 管理演进。

一个没有 Invalidity Protocol 的高精度模型，只是一次性 Benchmark 结果。

#### 优先构造单调优化

如果每次应用优化都会改变下一轮观测，系统可能在多个决策之间 Flip-Flop。Ultron 的 BHJ Promotion、Runtime Filter Progressive Addition，Oracle 的 Accepted Plan Set，AutoLiquid 的 Verify-Before-Commit 都在限制状态只能向已验证方向移动。

单调性不是数学装饰，而是减少线上探索风险的核心手段。

#### 把最坏情况纳入目标函数

平均收益不能描述生产风险。更合理的目标至少包括：

```text
ExpectedGain
  - λ1 * RegressionProbability
  - λ2 * WorstCasePenalty
  - λ3 * OptimizationOverhead
  - λ4 * MaintenanceCost
```

LLM Index Tuning 的 Best-of-Five 很亮眼，但 Worst Response 决定能否自动执行；LOAM 的项目筛选关注 ROI；Ultron 只选择容易证明收敛的 Application。这些都说明工业优化器优化的不是单一 Latency，而是收益、风险和运营成本的组合。

#### 不要让模型同时承担候选生成、评分与安全验证

将三个角色拆开更容易审计：

- Candidate Generator 追求覆盖率；
- Ranker / Cost Model 追求相对选择质量；
- Validator / Guardrail 负责阻止灾难性结果。

LOAM 的 Explorer 与 Cost Predictor、ScalePQO 的 Candidate Set 与 Rank Model、AutoLiquid 的 Heuristic 与 Shadow Verification 都遵循这种分工。LLM 直接端到端输出最终索引之所以危险，正是把三个角色压在一个高方差模型上。

### Future ：优化器正在变成一个有记忆的控制系统

这些论文共同展示了一次重要迁移：优化器从“每次编译都从 Catalog 重新推理”的无状态组件，演进成持续吸收执行结果的有状态控制系统。

但“有记忆”会引入新的系统问题：

- 历史属于谁，Cluster、Tenant、Database 还是 Query Template？
- Schema、数据量、分布、硬件或引擎版本变化时，哪些历史还能复用？
- 一个决策如何解释到具体历史样本或模型版本？
- 状态服务故障时，优化器能否无阻塞地退回原生路径？
- 多租户之间能否共享知识而不泄漏计划结构与数据分布？
- 新模型带来的收益是否超过训练、存储、推理和验证成本？

因此，下一代优化器的关键接口可能不只是：

```text
optimize(logical_plan, catalog_statistics) -> physical_plan
```

而更接近：

```text
optimize(
    logical_plan,
    catalog_statistics,
    execution_history,
    environment_prior,
    safety_policy,
    state_version
) -> physical_plan + decision_provenance
```

这里最重要的新返回值是 `decision_provenance`：为什么使用这个历史、为什么相信这个预测、何时应当失效、如何回退。没有可追溯性，反馈闭环越复杂，线上问题越难复现。

最终，我并不认为这些工作宣告了传统 CBO 的终结。恰恰相反，它们在给 CBO 补上长期缺失的感知、记忆和安全机制：增量统计让 Catalog 更及时，Learned Model 补偿缺失信号，HBO 记住运行时纠错，Plan Baseline 和 Shadow Verification 管理探索风险。

> **工业查询优化的未来，不是找到一个永远正确的模型，而是构造一个即使模型会错，也能持续学习、稳定收敛并限制损失的系统。**

### 参考资料

- [Learned Query Optimizer in Alibaba MaxCompute: Challenges, Analysis, and Solutions](https://arxiv.org/abs/2602.07336)
- [SIGMOD 2026 Industry Papers: LOAM](https://2026.sigmod.org/sigmod_industry_papers.shtml)
- [Amazon Redshift Re-invented](https://www.amazon.science/publications/amazon-redshift-re-invented)
- [Incremental Query Optimizer Statistics in Amazon Redshift](https://www.amazon.science/publications/incremental-query-optimizer-statistics-in-amazon-redshift)
- [Ultron: History-Based Query Optimization at Databricks](https://doi.org/10.14778/3827998.3828038)
- [Towards Industrial-Scale Parametric Query Optimization](https://www.vldb.org/pvldb/vol19/p4303-mo.pdf)
- [ScalePQO / RankPQO Industry Artifact](https://github.com/songsong945/RankPQO-industry)
- [Evaluating the Practical Effectiveness of LLM-Driven Index Tuning on Microsoft SQL Server](https://arxiv.org/abs/2603.09181)
- [VLDB 2026 Program: AutoLiquid and Real-Time SQL Plan Management](https://vldb.org/2026/program.html)
- [Databricks: Use Liquid Clustering for Tables](https://docs.databricks.com/aws/en/tables/clustering)
- [Oracle: Overview of SQL Plan Management](https://docs.oracle.com/en/database/oracle/oracle-database/26/tgsql/overview-of-sql-plan-management.html)
- [Oracle Optimizer Blog: What Is Real-Time SQL Plan Management?](https://blogs.oracle.com/optimizer/what-is-realtime-spm)

---

## 四篇存储论文放在一起看

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

这篇笔记会沿着“数据结构、执行引擎、查询优化、存储系统、分布式协议、云原生架构与自治系统”逐步扩展。每增加一篇论文，都应该让已有问题得到一部分回答，或产生一个更准确的新问题。
