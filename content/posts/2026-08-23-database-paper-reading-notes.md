---
title: "【Paper】数据库系统论文精读：从现代硬件到工业自治"
slug: "database-paper-reading-notes"
date: 2026-08-23T20:21:00+08:00
lastmod: 2026-11-12T00:00:00+08:00
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
description: "从单机现代硬件上的范围索引（Bf-Tree），到云原生共享存储与 EB 级底座（HopsFS、PolarFS、Tectonic、Aurora/Socrates/AlloyDB），再到分布式 OLTP、弹性 Shuffle 与工业级查询优化反馈闭环（LOAM、Ultron、ScalePQO 等）的深度论文精读。"
draft: false
toc: true
math: true
---

> 本系列第 3/3 篇 · 论文雷达 · [上一篇]({{< relref "2026-08-09-sigmod-2026-industry-papers.md" >}}) · 对照：[Umbra]({{< relref "2026-09-04-dive-umbra.md" >}})

把近十年最具代表性的数据库系统论文摊在同一张桌面上——从单机超内存范围索引 **Bf-Tree**，到云原生文件系统与共享存储 **HopsFS、PolarFS、Tectonic、Aurora/Socrates/AlloyDB**，再到分布式横向扩展 OLTP **Aurora Limitless**、自适应远程数据交换 **FuxiShuffle**，以及来自 Redshift、MaxCompute、Databricks、OceanBase、SQL Server 与 Oracle 的八项工业级优化器自治实践——初看之下，它们分布在不同的会议年份与软件栈层级，像是一份跨度极大的阅读清单。

但如果退后一步，用第一性原理去审视这些系统试图拆解的物理约束，就会发现它们其实在回答**同一个底层问题**：当 NVMe SSD 把随机读写拉进数十微秒、RDMA 与对象存储重写了机房内的带宽层级、而云平台每天沉淀下数十亿条查询执行轨迹之后，传统数据库沿用了三四十年的静态边界——**“缓存页等于磁盘页”、“数据库自己刷全量脏页”、“单点协调器串行定序”、“靠一份过期直方图猜完整条执行计划”**——究竟在哪一环最先崩塌？

一旦硬件延迟比例变了，软件如果还死守旧的粒度与同步契约，多核 CPU 就会空转在无谓的整页搬运、锁排队和灾难性的慢计划上；而一旦动手把状态拆细、外置或交给模型动态调整，**空间碎片、跨网协调、故障恢复与计划回退风险**又会立刻在新的边界上冒出来。整部现代数据库系统的演进史，就是一部把**状态同步开销、I/O 读写放大与估计误差**放到更合适的硬件层级与生命周期阶段去支付的重构史。

| 系统层级 | 精读与对照工作 | 打破的旧默认假设 | 重新划定的系统边界 | 接盘的新工程代价 |
|---|---|---|---|---|
| **单机范围索引与缓冲** | Bf-Tree（对照 B+-Tree、ART、Wormhole、Learned、LeanStore） | 内存缓存页必须与 4KB 磁盘页严格等大 | 用变长 `mini-page` 仅驻留热记录、脏行与缺口 | 循环缓冲池碎片回收、快照与 WAL 合并恢复 |
| **云原生存储与元数据** | PolarFS、HopsFS、Tectonic（对照 Aurora、Socrates、AlloyDB、RMS） | 元数据常驻单机内存、计算节点直刷全量数据页 | 事务化/分层元数据分片 + 用户态块或“日志即数据库” | 跨层异步垃圾回收、乱序复制冲突检测与页重放延迟 |
| **分布式事务与 Shuffle** | Aurora Limitless、FuxiShuffle | 跨分片提交必经中心协调器、Shuffle 策略作业前静态锁死 | 有界时钟 + Lead-Shard 2PC；实时遥测驱动介质与增量恢复 | 分片键建模约束、双水位内存退避与版本校验协议 |
| **工业查询优化与自治** | Redshift 底座与增量统计、LOAM、Ultron、ScalePQO、LLM 调优、AutoLiquid、Real-Time SPM | 编译期单次统计 + 静态代价搜索即可通吃线上负载 | `Observe → Represent → Decide → Verify` 持续反馈闭环 | 历史失效协议、分布漂移检测、影子验证成本与溯源调试 |

下面我们就顺着**从单机内存与闪存交界处向上生长到分布式云底座与自治控制面**的脉络，逐层剖析这十四项核心工作背后的机制设计、真实收益与失效边界。

---

## 一、单机存储与范围索引：当 NVMe 追近内存，缓存页还必须等于磁盘页吗？

在机械硬盘主宰存储的年代，一次随机寻道动辄耗费 5～10 毫秒，而顺序多读几千字节几乎不花额外时间。因此，经典 B+-Tree 把磁盘持久化单位与内存 Buffer Pool 的管理单位死死绑在同一个固定尺寸（4KB 或 16KB 页）上是绝对理性的：既然磁头千辛万苦挪过去一趟，当然要把整页顺手搬回内存常驻。

但硬件天平已经彻底倾斜了。一方面，服务器单机 DRAM 容量受限于插槽功耗与采购成本，面对动辄数 TB 的业务表，**内存与磁盘容量比持续下滑**；另一方面，现代多队列 NVMe SSD 的 4KB 随机读延迟已压到几十微秒，主机 CPU Cache 未命中（指针追逐）和内存容量浪费反而成了大头。沿着这股张力回看过去十几年范围索引的演化，就能看清每一代结构究竟在省哪笔钱：

![现代硬件上的范围索引演进：从固定页 B+-Tree 到细粒度混合缓存](/images/diagrams/aug-dbpaper-range-index-evolution.svg)

在纯内存一侧，**[ART（Adaptive Radix Tree）](https://db.in.tum.de/~leis/papers/ART.pdf)** 用按字节动态伸缩的 4/16/48/256 扇出节点取代了 B+-Tree 节点内的二分比较，配合 SIMD 把单步查找压进了极少的 CPU Cache Line；遇到长 URL 或复合字符串键导致前缀树层数过深时，**[Wormhole](https://dl.acm.org/doi/10.1145/3302424.3303955)** 又在内部节点架起哈希表锚点，把寻路复杂度从键长线性相关压回 \(O(\log L)\)，同时保留有序叶链表支撑范围扫描。再激进一点的**学习型索引（Learned Index，如 ALEX、PGM-Index）**干脆用分段线性函数去拟合键的累积分布（CDF），把内部导航节点压缩成几个浮点斜率参数；只是每当线上遭遇高倾斜局部集中写入（Write Skew），预留间隙（Gapped Array）被迅速填满并触发局部数组重排与模型重训时，写入尾延迟就会剧烈抖动，而且这类模型很难直接接管磁盘页的持久化与崩溃恢复。

一旦数据规模越过内存容量墙（Larger-Than-Memory），传统引擎又退回了 **B+-Tree 与 LSM-Tree 的二选一困局**：B+-Tree 点查和范围扫直接，但哪怕只改页内一条 100 字节的小记录，缓存里也得占住整张 4KB 页，刷盘时还得整页写回；LSM-Tree 把随机写攒成顺序追加与后台归并（Compaction），却逃不掉跨层读放大、空间放大与周期性归并毛刺。

PVLDB 2024 的 **[Bf-Tree（A Modern Read-Write-Optimized Concurrent Larger-Than-Memory Range Index）](https://www.vldb.org/pvldb/vol17/p3442-hao.pdf)**（[源码 `microsoft/bf-tree`](https://github.com/microsoft/bf-tree)、[设计文档](https://github.com/XiangpengHao/bf-tree-docs)）恰恰跳出了“改树形状还是改追加顺序”的惯性，对着那个四十年没人动的默认前提开了一枪：**既然 4KB 页最适合做 NVMe 持久化和块 I/O 单位，谁规定内存里的缓存单位也必须跟着定死在 4KB？**

### 1.1 Bf-Tree 的细粒度解耦：把 Buffer Pool 变成索引感知的变长环

设想一张存有上亿条 100 字节用户配置记录的 B+-Tree，其中 5% 的活跃用户分散在不同的 4KB 叶子页里。在传统 Buffer Pool 中，每命中一条 100 字节的热记录，就得连带把同页另外 3,996 字节无人问津的冷邻居一起请进宝贵的 DRAM；内存很快被冷字节塞满，真实有效记录缓存率惨不忍睹。

Bf-Tree 保留了 B+-Tree 天然契合范围扫描的有序内部导航树，却把叶层的内存缓存替换成了一组可变长的 **`mini-page`**。每个 `mini-page` 只映射底层 4KB 磁盘页中真正活跃的一小撮局部记录，并同时扮演三重角色：

1. **热点记录级缓存**：点查从磁盘读回 4KB 页后，只把命中的目标记录提取提升（Promote）进 `mini-page`，其余冷字节读完即弃，不占内存坑位；
2. **近期写聚合缓冲**：单条更新或插入直接写进目标叶节点的 `mini-page`（仅百字节级别变脏），攒够多笔同页修改或触发淘汰/检查点时，再一次性合并写回底层 4KB 磁盘页，大幅摊薄每次逻辑更新的写放大；
3. **负查询区间缺口（Phantom/Gap）缓存**：明确记下某段键区间刚从磁盘查过且确无记录，后续落在该空缺区间内的不存在键点查直接由内存返回未命中，免去反复白跑磁盘。

这样一来，缓冲池就不再是一个对上层数据结构一无所知的通用固定页框数组，而演化成了 **Index-Aware Buffer Manager**。为了高效管理成千上万个尺寸随写入不断膨胀或收缩的 `mini-page`，Bf-Tree 开辟了一整块循环内存空间（Circular Buffer）：对象需要扩容时不在原地硬挤，而是借用近似 RCU / Copy-on-Access 的思路在环尾分配新槽位、拷入有效记录并原子切换映射，旧空间顺着近似 Second-Chance 淘汰指针回收。

| 索引与存储设计 | 核心优化靶点 | 内存与磁盘映射关系 | 写入落盘机制 | 转移出的核心工程代价 |
|---|---|---|---|---|
| **经典 B+-Tree** | 有序点查与范围扫描 | 固定缓存页 1:1 绑定磁盘页 | 原地修改整张脏页 | 冷数据挤占内存，小更新触发整页写放大 |
| **LSM-Tree（如 RocksDB）** | 高吞吐写入与顺序 I/O | MemTable + 多层不可变 SSTable | 顺序追加 + 后台多路归并 | 范围与点查读放大、Compaction 抢占前后台带宽 |
| **Bw-Tree** | 多核无锁内存并发 | Mapping Table + CAS Delta 链 | 原子挂载增量 Delta 节点 | 链过长拖慢读取，超内存换入换出与合并极繁琐 |
| **LeanStore** | 消除通用缓冲池哈希寻址税 | 固定页框 + Pointer Swizzling | 原地页更新 + 冷却态解绑 | 页面驱逐状态机与父子指针并发解引用复杂 |
| **Bf-Tree** | 超内存热记录驻留率与写合并 | 变长 `mini-page` 映射盘页局部子集 | `mini-page` 内存攒批后合并刷盘 | 变长循环池空间碎片治理、快照与 WAL 一致恢复 |

把 **LeanStore**（以及 Umbra 的 `vmcache`）跟 **Bf-Tree** 摆在一起对比特别有启发性：LeanStore 解决的是“当页面已经在内存里时，怎么省掉每次去全局页表做哈希查找的 CPU 开销”（靠指针混写）；Bf-Tree 解决的则是“当内存根本塞不下多少完整页时，怎么只把页里最值钱的那几行留住”。两者正交，完全没有互相替代的意思。

### 1.2 并发边界、恢复协议与真实收益账本

在工程实现上，有两处容易被误读的细节必须拎清。首先，**Bf-Tree 并非无锁（Lock-Free）结构**：内部节点采用经典的乐观锁耦合（Optimistic Latch Coupling），读线程靠校验版本号无阻塞穿行，写线程仍需获取排他锁；当前开源原型采用操作系统线程交错发起同步块 I/O，`io_uring` 用户态异步化尚在官方演进路线图中。其次，**缓存与磁盘页异构后，崩溃恢复比传统 B+-Tree 更重**：系统通过预写日志（WAL）保障持久性，做在线快照时必须先把脏 `mini-page` 归并回一致的磁盘页镜像，故障重启则遵循“加载快照 + 重放增量 WAL”的两阶段恢复。

在论文给定的约百字节小记录、现代 NVMe SSD 与超内存工作集基准测试下，Bf-Tree 交出了非常亮眼的成绩单：范围扫描吞吐达到 RocksDB 的 **2.5 倍**，写吞吐达到对比 B-Tree 的 **6 倍**，点查吞吐达到对比 B-Tree 与 LSM-Tree 的 **2 倍**。

顺着机制本身就能推演出它的适用禁区：如果单条记录本身就有两三千字节（接近半张 4KB 页），`mini-page` 省下的冷字节空间立刻所剩无几，反而白白背上了变长分配元数据税；如果底层挂的不是随机写强悍的现代本地 NVMe，而是随机小 IOPS 受限的廉价云盘，合并刷回 4KB 页时的原地覆盖依然会撞上盘内垃圾回收瓶颈。但 Bf-Tree 撕开的口子极具穿透力——无论是在对象存储列存缓存里只驻留热 Row Group 的局部列段，还是在磁盘向量索引里只把高频聚类中心周边子图留在 DRAM，**“持久化 I/O 粒度”与“内存驻留粒度”从此不必再穿同一条裤子。**

---

## 二、云原生存储与分布式元数据：从共享块、“日志即数据库”到 EB 级资源池

看完了单机内存与闪存的交界，我们把视野拉大到整座数据中心。一旦单台机器的磁盘容量或故障恢复速度撑不住业务增长，存储外置与存算分离（Storage–Compute Disaggregation）就成了必经之路。

可只要把原本走主板 PCIe 总线的本地落盘改成跨交换机的网络调用，两个尖锐矛盾马上浮出水面：

1. **数据库每次提交事务，到底要把什么东西发过网络？** 如果照搬单机引擎的老规矩，既发 WAL 日志、又隔三差五把整张 8KB/16KB 脏数据页（连同防半页撕裂的 Doublewrite Buffer）往远端共享盘刷，微秒级的 NVMe 经过内核文件系统、TCP/IP 和多副本复制层层扒皮，瞬间就会退化成毫秒级慢盘；
2. **当文件数膨胀到百亿级、容量突破 EB 级，目录树元数据归谁管？** 像早期 HDFS 那样把全部命名空间塞进单台 NameNode 的 JVM 堆内存里，本机查表固然飞快，但全局读写锁和单机内存上限迟早会把整座集群锁死。

过去十年间，工业界围绕这两个痛点演化出了泾渭分明却高度互补的技术谱系。

### 2.1 云数据库共享存储的两条路线：PolarFS 用户态块共享 vs “日志即数据库”

先看云原生关系型数据库怎么解决远程共享存储的延迟与写放大。业界主要走出了两条代表性路径：一条以阿里云 **[PolarFS（PVLDB 2018）](https://www.vldb.org/pvldb/vol11/p1849-cao.pdf)** 为代表，**保留类 POSIX 文件/块抽象，靠极致的用户态软硬协同把远程块读写压到逼近本地盘**；另一条则以 **AWS Aurora、微软 Socrates（Azure SQL Hyperscale）与 Google AlloyDB** 为代表，彻底重写数据库与存储的分工契约——**“日志即数据库（The Log is the Database）”**（亦可打开[交互式架构图](/diagrams/aug-dbpaper-shared-storage-log-is-db.html)分步切换前台写提交快路径、后台日志异步物化与只读副本补页链路）：

![云原生共享存储数据库核心范式：“日志即数据库”解耦架构](/images/diagrams/aug-dbpaper-shared-storage-log-is-db.svg)

在 **Aurora / Socrates / AlloyDB** 的范式里，计算层的主写节点（Primary Writer）彻底卸下了“刷脏数据页”和“双写缓冲”的历史包袱，前台事务提交唯一需要跨网发送的只有纤薄的 Redo 日志记录：

- **Aurora** 将持久性与页重放合二为一放在跨 3 个可用区的 6 副本存储节点上，写满 4/6 Quorum 即确认提交，存储节点自己在后台把 Redo 拼回物理数据页；
- **Socrates（即前文 SIGMOD 2026 Industry 追踪的 Azure SQL Hyperscale）** 发现当数据库涨到上百 TB 时，存储节点既接日志又重放页仍会互相干扰，于是再切一刀——拆出极致低延迟的独立日志层（Log Service / XLOG）专门保提交确认，由横向分片的无状态/软状态 **Page Server** 异步订阅日志流生成多版本页面，并把全量切片沉淀到廉价对象存储；
- **AlloyDB** 同样将日志处理服务（LPS）独立下沉，并配合块级多层超大容量缓存与内存列式引擎服务混合负载；而在纯分析数仓一侧，**[Amazon Redshift Re-invented（SIGMOD 2022）](https://doi.org/10.1145/3514221.3526045)** 的 **RMS（Redshift Managed Storage）** 则以 **1 MiB 列式块**为单位把真相源直接托付给 S3，将计算节点本地 SSD 降格为区分首访冷块与重访热块的两级 Clock 缓存，从而让扩缩容（Elastic Resize）与并发扩展集群（Concurrency Scaling）变成纯元数据切换与按需预热。

反观 **[PolarFS](https://doi.org/10.14778/3229863.3229872)**，它解决的是另一个非常现实的工程诉求：**如果不想把既有数据库引擎的 Buffer Pool 和页刷盘协议大卸八块，还能不能做出低延迟的一写多读共享存储？** PolarFS 的答案是绕开通用操作系统内核，用四件套重修一条数据库专用的用户态高速公路：

1. **控制面与数据面彻底剥离**：全局控制面 `PolarCtrl`（论文中基于 MySQL 存储集群元数据）绝不介入日常读写；数据库进程直接链接轻量用户态库 `libpfs`（暴露 `pfs_pread` / `pfs_pwrite` 与提前消除写路径元数据变更的 `pfs_fallocate`），通过共享内存 Ring Buffer 零系统调用交给本机路由守护进程 `PolarSwitch`，后者凭内存缓存的拓扑表直连远端存储节点 `ChunkServer`。
2. **多级空间粒度各司其职**：逻辑卷 `Volume`（10GB～100TB）按 **10GB `Chunk`** 做跨机架三副本放置与迁移（100TB 卷仅约一万条映射，轻松常驻 `PolarSwitch` 内存），而在 `Chunk` 内部再按 **64KB `Block`** 做细粒度空间按需分配（Thin Provisioning）。
3. **RDMA + SPDK 轮询链路**：跨机网络走单边/双边预注册内存 RDMA，`ChunkServer` 独占物理 CPU 核以用户态 SPDK 轮询驱动本地 NVMe，砍掉中断与内核块层拷贝（当然代价是空闲时也要烧掉常驻轮询核的电费与配额）。
4. **`ParallelRaft` 消除虚假的日志串行阻塞**：标准 Raft 要求日志按严格连续序号提交应用，一旦前一个请求遇到慢 I/O，后面几十个发往不同磁盘偏移的并发请求全得跟着队头阻塞（Head-of-Line Blocking）。`ParallelRaft` 点破了块存储的领域性质——**只要两次写入的逻辑块地址（LBA）互不重叠，它们在物理语义上就完全可交换！** 因此它允许副本乱序持久化、乱序确认，并借助记录前序若干条日志 LBA 摘要的 `Look-behind Buffer`（论文 RDMA 实测窗口取 2 即获显著收益）安全放行无冲突的空洞后写入，仅在 Leader 切换时进入 `Merge` 阶段对齐缺口。

在论文 2018 年的硬件基准上，PolarFS 把三副本远程 4KB 随机写平均延迟压到了约 **48 微秒**（同环境本地 Ext4 约 10 微秒，走内核栈的 CephFS 则高达约 760 微秒）；在队列深度 QD=32 下，标准 Raft 的延迟是 `ParallelRaft` 的 **2.5 倍**，IOPS 还不到后者一半。两条路线放在一起看格外清楚：“日志即数据库”从根源上砍掉了跨网传输脏页的字节总量，而 PolarFS 则在不侵入过多引擎内部状态的前提下，靠硬件直通与冲突感知共识榨干了远程块通道的微秒潜力。

### 2.2 分布式层级元数据与 EB 级资源池：HopsFS 强事务分片 vs Tectonic 弱耦合池化

当存储底座向下承接的不再只是单一数据库实例，而是整座公司的海量文件与异构负载时，元数据与资源孤岛就成了头号敌人。**[HopsFS（FAST 2017）](https://www.usenix.org/system/files/conference/fast17/fast17-niazi.pdf)**（[源码 `hopshadoop/hops`](https://github.com/hopshadoop/hops)）与 **[Facebook/Meta Tectonic（FAST 2021）](https://www.usenix.org/system/files/fast21-pan.pdf)**（[官方工程博客](https://engineering.fb.com/2021/06/21/data-infrastructure/tectonic-file-system/)）给出了两种截然相反的破局哲学。

**HopsFS 的切法是“把文件系统 NameNode 改造成无独占权威状态的 NewSQL 事务客户端”。** 它完整保留了 HDFS 的 DataNode 块数据面，把原本锁死在单台 Active NameNode 内存里的目录树拆成逐级关系表记录（例如 `/user/foo/data.parquet` 拆解为 `(parent_id=/, name=user)` → `(parent_id=user, name=foo)` → `(parent_id=foo, name=data.parquet)`），连同 `Block`、`Replica`、`Under-replicated` 状态表一并存入分布式内存数据库 **MySQL Cluster NDB**。

要把层级目录树塞进分片数据库而不引发全集群广播扫描，HopsFS 做了三处极其考究的领域映射：

- **按高频访问局部性设计分片键**：同一目录下的直接子项统一按**父目录 `parent_id`** 分片，确保高频 `ls /dir` 落入单个数据库分片做分区裁剪索引扫描（Partition-pruned Index Scan）；单个文件下辖的 Block 与副本记录则按**文件自身 `inode_id`** 分片，让读写元数据变成单分片短事务；唯独靠近根部的顶层目录改用缓存 + 可配置伪随机分片打散热点。
- **在应用层补齐串行隔离语义**：当时的 NDB 最高只提供 Read Committed 隔离级别，顶不住并发 `rename` 或路径删除冲突。HopsFS 干脆在多个并发 NameNode 内统一执行**从根到叶、多路径按左序深度优先（Left-ordered Depth-first）一次性加最强锁**的协议，事务内读入本地缓存、提交时统一落库，彻底杜绝死锁与中途锁升级。
- **百万级大目录拆解为 `Subtree Operations Protocol`**：递归删除百万文件的目录如果塞进一笔大事务，瞬间就会撑爆行锁表。HopsFS 先在子树根节点打上带 NameNode ID 的持久化排他意图标记，等存量短事务排空进入静默态（Quiescent）后，再拆成多批次小事务并行清理；哪怕中途该 NameNode 宕机，其他节点扫到残留标记也能无缝接力收尾。（顺带一提，因为表里存的是逐级 `parent_id` 而非全路径字符串，跨目录 `rename` 只需在隔离子树后修改根节点的 `parent_id`，真正重头戏在递归删除。）

在 Spotify 生产负载回放中，HopsFS 的元数据容量达到原版 HDFS 的 **37 倍**，混合负载吞吐达 **16 倍**，写密集负载达 **37 倍**，且单台 NameNode 故障不再触发漫长的 FSImage 重载停顿；代价则是空载单次操作比纯本机内存多了跨网查库开销，百万文件递归删除也因多轮远程事务明显慢于单机内存遍历（而同期 Apache Hadoop 社区则沿着兼容演进路线发展出了 [Router-based Federation](https://hadoop.apache.org/docs/stable/hadoop-project-dist/hadoop-hdfs-rbf/HDFSRouterFederation.html) 与分担读流量的 [Observer NameNode](https://hadoop.apache.org/docs/stable/hadoop-project-dist/hadoop-hdfs/ObserverNameNode.html)）。

如果说 HopsFS 还在执着地用跨行 ACID 事务保卫强一致目录语义，那么 **Facebook 的 Tectonic** 面对的则是更庞大的资源孤岛账本：早年热小图走 Haystack（卡在磁盘 IOPS，容量大量闲置）、温冷图走纠删码 f4（卡在容量，IOPS 空转）、数仓批处理走上百套独立 HDFS 集群（周期性打满顺序带宽）。Tectonic 决定把它们全部收编进**同一个 EB 级存储池**：一个典型生产集群即管理着 **1,590 PB 总容量（已用约 1,250 PB）、107 亿文件、150 亿 Block 和 4,208 台存储节点**，Blob 与数仓各占一半容量、错峰共享同一批机械盘与 SSD 热缓存，一举将数仓集群数量砍掉了 **10 倍**。

为了撑住 EB 级彻底打散，Tectonic 做出了三个比 HopsFS 更激进的决断：

1. **三层元数据彻底放弃跨分片事务**：把元数据拆成按目录 ID 分片的 `Name` 层、按文件 ID 分片的 `File` 层与按块 ID 分片的 `Block` 层，各自哈希打散到基于 RocksDB + Paxos 的线性一致 KV 库 **ZippyDB** 中。ZippyDB **不提供跨分片分布式事务**！Tectonic 靠“单文件只许单 Writer（通过写 Token 隔离旧写者）+ 厚客户端库（Client Library）严格按幂等顺序推进多层更新 + 后台垃圾回收（GC）异步清理异常中断孤儿块”来兜底正确性。
2. **像 LSM 一样把编码成本推迟到封闭（Seal）之后**：底层 `Chunk Store` 只认扁平 XFS 文件，不认目录。正在追加写入的小 Block 先用低延迟三副本承接，一旦写满封闭（Seal），后台再批量转码成省空间的 `RS(10,4)` 纠删码——完美避开了小追加每次都要同时读写 14 个数据与校验块的 IOPS 灾难。
3. **限流必须卡在真正饱和的物理瓶颈上**：小 Blob 吃随机寻道 IOPS，大数仓扫描吃顺序吞吐，单按请求个数或单按字节数做限流都会失真。Tectonic 统一折算为**磁盘忙碌时间（Disk Time）**，按 `Tenant → TrafficGroup → TrafficClass` 三级漏桶动态借用空闲配额。相应地，它也坦然接受了哈希打散的结构性代价：不再原生提供昂贵的跨分片递归 `ls`，`du` 容量统计改为周期性异步刷新，上层数仓也配合把串行 `rename` 改成了并发批量提交。直到 [Meta 2026 年公布的 AI 存储蓝图](https://engineering.fb.com/2026/07/01/data-infrastructure/metas-ai-storage-blueprint-at-scale/)中，Tectonic 依然作为最底层的横向扩展块底座稳稳托着上层对象与训练数据集服务——只是考核指标已从当年的“榨干硬盘利用率”演进成了“绝不让昂贵的训练 GPU 等 I/O 空转”。

---

## 三、分布式事务与中间数据交换：把扩展瓶颈从单点协调器里拆出来

当持久化存储与元数据具备了横向扩展能力，系统瓶颈很快就会顺着调用栈往上顶到计算层：在事务处理（OLTP）里，共享存储虽然能秒级拉起十几个只读副本，但**所有写入和锁协调依然卡在唯一那台 Primary 计算节点上**；而在大规模分析与批处理执行（OLAP / ETL）里，上下游算子重分区洗牌（Shuffle）一旦耦合在计算进程本地盘或静态内存阈值上，**网络碎片风暴与单节点挂掉引发的级联重算**就会把集群算力吞噬殆尽。

SIGMOD 2026 Industry 上的两篇重量级工业论文——AWS 的 **Aurora PostgreSQL Limitless** 与阿里云的 **FuxiShuffle**——恰好展示了如何在不引入脆弱全局中心单点的前提下，分别解开这两道枷锁。

### 3.1 Aurora PostgreSQL Limitless：无中心 2PC 与有界时钟上的分片进化

很多业务团队在单主 Aurora PostgreSQL 写满之后，极其抗拒迁移到全新分布式数据库，原因很实在：驱动行为微调、复杂存储过程失效、隔离级别细微差异都会让存量核心系统伤筋动骨；可如果留在应用层搞人工分库分表，跨库转账一致性与在线扩容重平衡又会变成研发噩梦。

**[Aurora PostgreSQL Limitless Database（SIGMOD Companion 2026）](https://software.imdea.org/~gotsman/papers/limitless-sigmod26.pdf)**（[DOI](https://doi.org/10.1145/3788853.3803089)、[官方文档](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/limitless.html)）走了一条非常务实的“内核渐进融合”路线：每个底层数据分片（Shard）依然是一套挂载着跨 3-AZ Aurora 共享存储的改造版 PostgreSQL 实例，上层则架设一组水平扩展的无持久提交状态路由层（Router Fleet），把单分片直推与跨分片分布式事务统一藏在标准 PostgreSQL 连接背后：

![Aurora Limitless 横向扩展 OLTP 架构：无状态路由、有界时钟与无中心 2PC](/images/diagrams/aug-dbpaper-aurora-limitless-2pc.svg)

要让这套两层架构既跑出百万级 TPS 又不被分布式协调拖死，内核必须在三个关键点上动手术：

首先是**诚实面对数据局部性**。Limitless 并没有宣称“随便建表就能零成本无限扩展”，而是提供三种显式表布局：按指定分片键哈希打散的 **Sharded Table**、在所有分片完整复制以支撑本地高速关联的小字典 **Reference Table**，以及留在单一分片承接遗留逻辑的 **Standard Table**。当 `customers` 与 `orders` 共用 `cust_id` 分片键共置（Collocate）时，Router 利用 PostgreSQL 原生分区裁剪识别出单分片事务，直接整句 SQL 下推到目标 Shard 一趟往返搞定（遇到带状态的 `VOLATILE` 函数或无法下推的复杂外连接则退回 Router 汇总）。分片键依然是一扇接近单向门（One-way Door）的建模决策，但平台彻底接管了过去泄漏给业务代码的连接池复用、路由映射与在线切片分裂（Shard Split）。

其次是**用有界时钟取代全局授时器，用参与分片自举取代中心化 2PC 协调器**。原生 PostgreSQL 靠活跃事务 ID 数组构造本地快照，跨机根本没法对齐；Limitless 接入前文硬件篇提到的 **Amazon Time Sync**（物理时钟误差界通常小于 1ms），把跨分片 MVCC 快照与提交序改造成了标量时间戳（当前支持扩展后的 Read Committed 与 Repeatable Read，暂未支持 Serializable）。更巧妙的是跨分片两阶段提交（2PC）：Router 驱动所有参与分片完成 `PREPARE` 后，**不设任何全局中心事务管理器，而是直接从本笔事务的参与分片中挑一个指定为 `Lead Shard`**！只要提交决议在 `Lead Shard` 落入 Aurora 共享存储即视为权威提交；哪怕驱动这笔事务的 Router 当场暴毙，其余处于悬决态（In-doubt）的参与分片只需去问一眼 `Lead Shard` 的持久状态就能安全收敛，最后配合常常已被存储落盘耗时自然掩盖的微秒级 `Commit Wait` 守住外部一致性。

最后是**纵向 Serverless 与横向 Split 的双层弹性**。突发流量毛刺先靠 Aurora Serverless V2 秒级拉高单节点 ACU 算力与缓冲池顶住，避免一有风吹草动就搬迁数据；持续高压再由集群控制面触发细粒度 `Table Slice` 在线拆分扩 Shard。在论文 12,000 个 Warehouse、约 10% 事务跨分片的改进版 HammerDB/TPC-C 实测中：保持总预算 **1,536 ACU** 不变，把拓扑从 `2 Router / 4 Shard` 横向拆细到 `4 Router / 8 Shard`，吞吐（NOPM）便从 1,268,350 跃升至 2,012,763（**+58.7%**），平均延迟从 29.70ms 降至 21.06ms；在 `8 Router / 16 Shard` 下将预算翻倍至 3,072 ACU，吞吐再涨 **41.6%**、延迟压至 9.72ms（论文发表时线上最大生产集群已达 `32 Router / 64 Shard`）。

### 3.2 FuxiShuffle：把静态数据洗牌改造成实时遥测闭环

如果说 OLTP 最怕协调器单点，那么超大规模分布式计算（如阿里云 MaxCompute / Fuxi）最怕的就是夹在两个 Stage 中间的 **Shuffle 算子**。上游成千上万个 Writer 往外扇出小碎片，下游成千上万个 Reader 汇聚拉取；策略一旦定死，怎么选都是错：全放内存容易把共机 Worker 挤到 OOM，全落机械盘又白白浪费空闲 DRAM；等所有 Writer 跑完再拉起 Reader（Staged）会拖长端到端工期，同时拉起（Gang）又让下游干占着昂贵 CPU 空等数据；不写备份怕单盘坏掉连环重跑上游，全量双写又把正常作业全拖慢一截。

**[FuxiShuffle（SIGMOD Companion 2026）](https://arxiv.org/pdf/2602.22580)**（[DOI](https://doi.org/10.1145/3788853.3803085)）的解法，是把 Shuffle 从一锤子买卖变成一套由 `Job Manager`、`Shuffle Service Manager`、Writer 同机合并碎片的 `Shuffle Proxy` 以及可共机（走内存）或驻留存储节点（走盘古磁盘）的 `Shuffle Agent Group` 组成的**连续自适应控制系统**：

1. **受实时容量约束的内存/磁盘动态准入**：系统根据输入规模、算子类型与历史画像预测任务耗时 \(\hat{t}\)，只有当 \(\hat{t} \le \tau_{\text{mode}}\) 时才走内存通道；而阈值 \(\tau_{\text{mode}}^*\) 并非拍脑袋写死的常数，而是后台每隔约 30 秒结合各节点实时上报的空闲内存 \(Z_{\text{available}}\) 动态求解的最优边界：
   $$\tau_{\text{mode}}^* = \arg\max_{\tau} y(\tau) \quad \text{s.t.} \quad z(\tau) \le Z_{\text{available}}$$
   其中 \(y(\tau)\) 为可被内存加速的数据比例，\(z(\tau)\) 为单机期望内存消耗。集群闲时自动吸纳更多短平快内存 Shuffle，吃紧时配合 Yellow/Red 双水位线平滑溢写（Spill）乃至果断丢弃已有备份的低优内存块，绝不挤爆计算任务。
2. **渐进式调度（Progressive Scheduling）与边产边读**：避开 Staged 与 Gang 的两极分化（排序等强阻塞边自动将阈值设为 1 退回 Staged，极小低并发作业直接走 Gang），`Job Manager` 在上游进度跨过动态阈值 \(\lambda_S\) 时触发 `Pre-start` 拉起下游 Reader，Reader 通过 `Pre-read` 轮询消费已提交的连续聚合块；与此同时，`Shuffle Agent` 收集的分区大小与基数统计直接反哺运行期动态拆倾斜分区（Adaptive Skew Join）。
3. **按重建代价挑备份，故障恢复绝不浪费下游进度**：小而重算极便宜的数据块不备份，长耗时高代价块挂载本地或跨机 `Remote Backup`，本身就足够大的巨型块干脆走 `Backup Only` 直写双副本跳过 Agent 二次转发。更漂亮的是 **Incremental Recovery（增量恢复）**：传统引擎一旦发现某段 Shuffle 块读取失败，往往一刀切杀掉下游 Reader 重跑；FuxiShuffle 则让 Reader 继续消费手头完好的其他分片，同时重拉缺失 Writer，最后靠 `Writer Version + RetryIdx + 聚合 Checksum` 双阶段校验只补读缺口数据（一旦校验发现缺失或重复残留立即安全退回全量重算）。

在 20 台 96 核计算节点 + 18 台存储节点的 1TB/10TB TPC-DS 与 TeraSort 基准对比中，FuxiShuffle 仅用单机约 3%～5% 的绑定 Agent 核心，便相对内部定制 Hadoop/Spark 基线平均压低了 **76.36% 的端到端耗时**与 **67.14% 的计算资源成本（CU Cost）**；在注入随机网络闪断时，开启容错后耗时与成本仅微增 9.6% 与 8.1%（关闭容错则耗时暴涨近三倍）。

---

## 四、工业查询优化与自治调优：从“把代价猜得更准”转向“让错误代价可控”

前面三层把索引、存储和分布式执行通道修得再宽，只要最顶上的查询优化器（Query Optimizer）在基数估计（Cardinality Estimation）上偏了三个数量级，挑出一个拿大表去广播或者漏掉关键索引的烂计划，几十倍的硬件加速红利一秒钟就能赔个精光。

经典的 Volcano/Cascades 优化器框架依赖一个非常干净的开环假设：**直方图与 NDV 描述数据分布，代价模型（Cost Model）折算算子开销，动态规划或 Memo 搜索挑出预估最便宜的物理计划。** 这套搜索骨架本身并没有过时，但真实云数仓与企业负载每天都在从四个方向击穿它的前提：百亿行大表的统计信息刷一次要十几个小时，数据湖外部表干脆连统计都没有；五千台机器共享资源池的瞬时拥塞让同一个计划上午跑 10 秒、下午跑 2 分钟；同一条参数化 SQL 模板绑上大客户 ID 和普通用户 ID 最优计划天差地别；更致命的是，就算新模型让 95% 的查询快了 20%，只要剩下 5% 里有一条核心交易报表慢了 50 倍，DBA 第二天就会毫不犹豫地关掉你的“智能优化开关”。

把近年来工业界真正成功上线的八项代表性优化与自治工作（来自 **Amazon Redshift、阿里巴巴 MaxCompute、Databricks、OceanBase、Microsoft SQL Server 与 Oracle**）放在一起对表，就会发现一个极其醒目的共识：**没有任何一家生产大厂试图用黑盒神经网络或大模型把原生 CBO 连根拔掉；大家真正在造的，是一套围绕原生优化器运转的 `Observe → Represent → Decide → Verify` 反馈控制闭环：**

![工业级查询优化反馈闭环：从静态基数失真到有界护栏收敛](/images/diagrams/aug-dbpaper-optimizer-feedback-closed-loop.svg)

| 系统与工作 | 出处 | 介入的控制对象 | 核心反馈与状态表示 | 防劣化安全护栏（Guardrail） |
|---|---|---|---|---|
| **[Redshift Re-invented](https://doi.org/10.1145/3514221.3526045)** | SIGMOD 2022 | 存储缓存、编译缓存与自治调度 | 全平台查询、扫描与资源遥测 | 存算分离隔离后台调优动作与前台 SLA |
| **[Redshift Incremental Stats](https://www.amazon.science/publications/incremental-query-optimizer-statistics-in-amazon-redshift)** | PVLDB 2026 | 单列 CBO 统计（NDV/MCV/直方图） | 四种可合并增量 Sketch 状态 | 转换回原 Catalog 格式免改热路径，删超 10% 全量重建 |
| **[MaxCompute LOAM](https://arxiv.org/abs/2602.07336)** | SIGMOD 2026 Ind. | 受限候选物理计划排序 | 无统计计划树编码 + Stage 环境指标 | 仅开 6 个安全旋钮、领域自适应免试跑、事前筛高 ROI 租户 |
| **[Databricks Ultron](https://doi.org/10.14778/3827998.3828038)** | PVLDB 2026 | Join 算法、Runtime Filter、分区数 | 软状态推送的子计划级精确行数与 AQE 修正 | 单向单调应用（如只许 SHJ 升 BHJ），无历史或变更即退回原生 |
| **[OceanBase ScalePQO](https://www.vldb.org/pvldb/vol19/p4303-mo.pdf)** | PVLDB 2026 | 参数化 SQL 的计划缓存路由 | 性能感知模板聚类模型 + 有限候选池 | KL 散度监测参数漂移，后台异步微调与 Prepend 换血 |
| **[SQL Server LLM Index Tuning](https://arxiv.org/abs/2603.09181)** | PVLDB 2026 | 工作负载推荐索引集合 | SQL、Schema、Showplan 与实测耗时 | 揭示单次高方差与错误代价器陷阱，蒸馏为确定性规则代码 |
| **[Databricks AutoLiquid](https://vldb.org/2026/program.html)** | VLDB 2026 Ind. | 表级数据聚簇键（Liquid Clustering） | 扫描裁剪遥测 + 独立计算抽样 | **Verify-Before-Commit**：抽样影子验证确认裁剪收益后才提交 |
| **[Oracle Real-Time SPM](https://vldb.org/2026/program.html)** | VLDB 2026 Ind. | SQL Plan Baseline 准入控制 | 前台新计划首跑实测 vs 历史参考基线 | 发现劣化立即封堵后续重复选用，强制锁回已知好计划 |

顺着这条闭环从“修补输入统计”一路走到“控制计划与物理布局风险”，每一步都藏着教科书里学不到的生产智慧。

### 4.1 先修最便宜的基石：Redshift 可合并增量统计（Sketch）

在跟风上深度学习之前，**[Incremental Query Optimizer Statistics in Amazon Redshift（PVLDB 2026）](https://www.amazon.science/publications/incremental-query-optimizer-statistics-in-amazon-redshift)** 先算了一笔非常清醒的账：传统 CBO 之所以频频估错，很多时候根本不是代价公式坏了，而是**统计信息来得太晚了**。论文统计 Redshift 线上集群发现，超过 100 亿行的大表跑一轮全表 `ANALYZE` 的 P90 耗时超过 **10 小时**；数据持续追加期间，陈旧统计在时间列过滤谓词上会酿成高达 **25 倍的 Q-Error**。

要把每次重扫全表的笨办法改成只扫新增可见行（MVCC Delta）的增量维护，难点在于 PostgreSQL/Redshift 优化器同时需要四类脾气完全不同的统计量。Redshift 没有强求用一个万能数据结构硬扛，而是组了一套单核每秒可吞吐 **1,550 万次插入**、总内存仅占约 **90 KiB** 的四件套组合拳：

- **基数（NDV）交由 `HLL++`**：扫 100% 增量行（采样对长尾唯一值极敏感），常数时间更新且天然支持跨节点无损合并；
- **高频值（MCV）拆成 `Space Saving` + `Count Sketch` 双打**：`Space Saving`（仅扫 25% 样本提速）擅长以极小空间锁定谁是高频候选，但频次计数偏粗糙；`Count Sketch`（扫 100% 行，直接白嫖复用 `HLL++` 算好的哈希值！）则专门为候选值提供近似无偏的高精度频率估计；
- **去除 MCV 后的等深直方图交由 `KLL` 分位数概要**（扫 25% 样本）：这里藏着一个极易踩坑的语义细节——`KLL` 内部依然混着高频 MCV 值，因此导出去除了 MCV 的直方图边界时，不能直接查一次分位点完事，而必须迭代执行“查当前目标 Rank → 统计落在该边界前的 MCV 累计权重 → 平移修正目标 Rank 再查 `KLL`”直至边界收敛。

整篇论文最值得内核开发者抄作业的有两处决断：第一，**Sketch 绝不进入优化器编译热路径**——分区级与节点级层层 Merge 完成后，系统直接把 Sketch 离线折算回 Catalog 原有的标准 NDV/MCV/直方图字段，优化器代码一行不用改，规划延迟零增加；第二，**坚决不在前台 `INSERT / COPY` 写路径里同步更新 Sketch**——团队起初试过用写 S3 的延迟去掩盖同步算 Sketch 的开销，结果实测依然拖慢了导入吞吐，果断退回后台异步自动 `Delta ANALYZE`；面对删除操作也不硬上昂贵的可删除 Sketch，而是仗着线上 86.6% 的表零删除、其余绝大多数每天仅删寥寥数行的真实分布，等累计删除满 10% 再兜底触发全量重建。在百亿行 `lineitem` 持续每轮增长 1% 的实测中，`Delta ANALYZE` 最高提速 **68.4 倍**，统计新鲜度提升 10 倍，上线数千生产集群后大表每周统计收集总算力直降约 **40%**。

### 4.2 当统计注定缺失、环境剧烈抖动：LOAM 与 ScalePQO 的受限学习

如果到了十万个业务项目混跑、外部湖表遍地开花、连 `ANALYZE` 都没法指望的共享离线/实时数仓里怎么办？阿里巴巴 MaxCompute 的 **[LOAM（SIGMOD 2026 Industry）](https://arxiv.org/abs/2602.07336)** 和 OceanBase 的 **[ScalePQO（PVLDB 2026）](https://www.vldb.org/pvldb/vol19/p4303-mo.pdf)**（[开源评测包](https://github.com/songsong945/RankPQO-industry)）分别攻克了**跨环境安全选计划**与**大规模参数化路由**两座大山。

**LOAM** 面对的是学术界学习型优化器（LQO）最头疼的真实战场：五千台机器动态混部导致同一计划端到端延迟乱跳，而且线上绝不允许为了攒训练数据去试跑高危野生计划。LOAM 的解法步步为营：

1. **用优化器当候选护栏（Steering）**：在 MaxCompute 75 个可调旋钮里只挑 **6 个安全且高区分度的 Flag** 配合多输入子查询基数缩放来诱导原生优化器吐出合法候选集——哪怕模型看走眼，最坏也只是退回另一个合法物理计划；
2. **`Statistics-Free` 代理特征 + 环境解耦**：不强求输入直方图，而是将表/连接列稳定标识、扫描分区数、谓词函数 Multi-Hot 喂给三层树卷积网络（通道 256→128→64 压至 32 维嵌入），同时在训练期拼入 Stage 窗口每 20 秒采样的四维集群负载特征（`CPU_IDLE`、`IO_WAIT`、`LOAD5`、`MEM_USAGE`），预测抗干扰更强的 **CPU Cost** 而非挂钟延迟，从而把“计划本身烂”与“恰好赶上宿主机繁忙”剥离开来（线上推理时因未来分配机器未知，填入历史均值代表向量做无偏近似）；
3. **梯度反转领域自适应（免试跑评分）**：训练样本全来自线上安全跑过的默认计划，待打分的却是旋钮变出来的候选计划。LOAM 在编码器后挂接判别器与梯度反转层（Gradient Reversal Layer），逼迫编码器抹平“默认计划域”与“未执行候选域”的分布偏移（Covariate Shift），一分钱试跑费不花就能给候选计划排座次；
4. **先算 ROI 再上线**：`Project Selector` 先用硬规则砍掉 59.5% 表命太短或样本稀疏的项目，再用 XGBoost 挑出优化潜力最大的头部项目定向部署，在高潜项目线上 Flighting 回放中最高省下约 **30% CPU 开销**（抽样推算全平台至少约 4% 的项目可稳拿 10% 以上净收益）。

而在高频 OLTP/HTAP 参数化查询场景下，**ScalePQO** 撞上的是另一堵规模墙：同一个 SQL 模板绑不同参数需要走不同索引或连接顺序；如果按早期做法“一个模板训一个专属模型”，3,300 个模板光收集样本就要上千小时、模型体积超 3.4 GiB；可如果偷懒“全库几千个模板共用唯一一个大模型”，加速比就会从 33 个模板时的 2.34× 暴跌到 3,300 个模板时的 1.18×。

ScalePQO 走通了一条漂亮的中间路线：先训一个全局排序模型，提取出能反映**“参数—计划—性能曲线相似性”**（而非表面 SQL 字符串编辑距离！）的模板嵌入向量，聚类后为每个簇微调一个轻量专精模型；线上按时间槽计算近期参数向量的 **KL 散度（KL Divergence）**，一旦捕捉到业务参数分布漂移，后台异步双管齐下——**既微调排序模型，又以 Prepend（新计划插队头、淘汰最老项）方式往候选池里补充新参数区间的计划**。在 OceanBase 5,000 模板基准上，模型体积从逐模板方案的 5,712 MiB 压到了 **759 MiB**，最高取得相对原生优化器 **1.62 倍**、相对前代 RankPQO **1.23 倍**的提速。

### 4.3 把运行时纠错存成单调长期记忆：Databricks Ultron

跟训练神经网络去泛化预测陌生查询相比，**[Ultron: History-Based Query Optimization at Databricks（PVLDB 2026）](https://doi.org/10.14778/3827998.3828038)** 抓住了生产数仓另一个黄金规律：**企业负载极其爱“炒冷饭”。** 论文统计 Databricks 线上发现，DBSQL 中前一周逐字重复的查询占 **67%～80%**，高达 **85% 的 Join 反复出现并吃掉了全平台 73% 的执行时间**！

在没有历史记忆之前，Spark/Databricks 的自适应查询执行（AQE）虽然能在跑起来之后发现“哎呀这张表过滤完其实很小，或者那个分区倾斜太大了”并临时拆分区补救，但**第一阶段的昂贵 Shuffle 税已经白交了，而且明天同一条报表跑过来时，失忆的优化器还会把同样的弯路原封不动再走一遍。**

Ultron 的定位用一句话就能说透：**HBO（基于历史的优化）就是 AQE 的跨查询长期记忆——AQE 负责救活今天这一把，Ultron 负责让明天一开始就选对。** 它的工程落地极其克制：

- **读侧零远程阻塞，租户隐私零明文**：跨集群分布式缓存 `Softstore` 主动把租户命名空间下的历史摘要异步推送（Push）到计算集群本地的 `QuickPredict` 内存中——万一历史服务挂了或网络抖动，优化器毫秒级跳过、绝不卡住编译；且缓存键只存不可逆的子计划结构 Hash（支持带常量精确匹配、去常量模板匹配及结构模糊匹配三级精度）与输入表规模分桶因子（`Size Factor`），不落一行原始 SQL 文本或敏感常量。
- **用数学上的“单调性（Monotonicity）”锁死震荡与回退**：很多反馈系统上线后死于左右横跳（今天改了计划导致观测变了，明天又改回去）。Ultron 专挑单调收敛的动作下手：
  1. **Join 单向晋升**：没把握时默认走保守的 Shuffle Hash Join（SHJ）；一旦首次执行测准了 Build 侧真实行数确实够小，下次直接单向晋升为免 Shuffle 的 Broadcast Hash Join（BHJ）——由于换连接算法绝不会改变上游输入的真实行数，一次晋升即永久稳定（该策略覆盖了全平台约 **23% 的 Join**，中位耗时降低约 **25%**，事后核验准确率达 **96%**）；
  2. **逐轮递增注入 Runtime Filter**：有效动态过滤只会减少或维持下游行数、绝不会让先前已生效的过滤失效，因此每跑一轮最多顺势多加一层达标的过滤器，直至稳态收敛；
  3. **直接复用 AQE 拆出的最优分区数**：省去每次运行期重分区的折腾。全套机制带来的中位额外规划耗时不到 **5%**（峰值 P95 为 12.6%），一旦遇到表 Schema 变更（哪怕只是加一列）则宁肯干脆失效清零，也绝不拿错位旧历史冒险。

### 4.4 物理调优与计划演进的终极防线：蒸馏规则、影子预验与前台熔断

当优化动作进一步延伸到开销高昂的**建索引、改表聚簇布局**或是**核心交易库升级后的新计划放行**时，犯错的代价就不再是慢几秒，而是刷爆磁盘或者引发生产 P0 故障。最后三项最新工作恰好把“怎么管住高风险变更”讲透了：

第一项是微软 SQL Server 团队在 PVLDB 2026 发表的实证研究 **[《Evaluating the Practical Effectiveness of LLM-Driven Index Tuning on Microsoft SQL Server》](https://arxiv.org/abs/2603.09181)**。作者拿 **GPT-5** 对每个案例（涵盖 TPC-H SF10 与四组真实企业负载共 127 个单查询案例，输入 SQL、Schema、存量索引与 Showplan）独立采样五次，跟打磨了二十多年的确定性调优器 **DTA（Database Engine Tuning Advisor）** 真刀真枪比实测执行耗时：

- 如果开了上帝视角挑五次回答里最快的那一次（Best-of-Five），GPT-5 在约 **67%** 的单查询案例中打平或击败了 DTA，其中约 **31%** 至少快出 20%，专治 DTA 被错误基数估计带偏的疑难杂症；
- **但生产环境没有上帝替你挑哪一次是对的！** 同一个 Prompt 的最差一次采样经常跑出超时或近 **10 倍的严重性能倒退**；更扎心的是，一旦把单条查询换成多查询组合工作负载，大模型注意力立刻被分散去凑表面公共列，漏掉真正占大头的热点瓶颈，打平或胜出率从约 50% 断崖跌到 **5% 以下**；
- 如果把 GPT-5 猜出来的候选索引喂回给 DTA 的 What-If 代价评估器兜底呢？**照样失灵！** 因为 DTA 当初之所以漏掉那个好索引，本来就是优化器自己的 What-If 代价模型算错了账，把好索引递回同一个算错账的裁判手里，不是被误杀就是把烂索引放了进来。
- 全篇最精彩的一步反转在于：**作者干脆人工剖析了 GPT-5 做对时的推理套路，将其提炼（Distill）成一套完全确定性的轻量启发式规则程序**（优先盯紧大表重度扫描、按过滤与连接列排键序、顺手补齐覆盖列、砍掉小表鸡肋索引）——结果这套零采样方差、零 Token 成本、完全可审计的白盒规则，稳稳拿回了大模型相当大一部分收益！这给所有系统软件提了个醒：**用大模型离线挖掘启发式灵感，再蒸馏成确定性代码上线，往往比每次在线抽盲盒靠谱得多。**

第二项与第三项（依据 VLDB 2026 Industry 官方公开设计与产品文档预读）则展示了存算分离与前台会话如何充当天然验证器：

- **Databricks [AutoLiquid](https://docs.databricks.com/aws/en/tables/clustering)** 把数亿张湖仓表的聚簇键选择简化为一句 `CLUSTER BY AUTO`。在传统存算一体库里，测试新聚簇键意味着要把整张大表重排一遍抢占线上 I/O；而依托 Lakehouse 存算解耦底座，AutoLiquid 先从扫描遥测里筛出候选键，接着拉起独立临时算力在只读快照的**抽样数据上跑快速影子验证（Sampled Shadow Verification）**，亲眼测出真实的数据块跳过（Data Skipping）收益达标后，才正式提交重写（生产管理数百万张表，超 95% 评测表持平或优于人工选键）。
- **Oracle [Real-Time SQL Plan Management（SPM）](https://docs.oracle.com/en/database/oracle/oracle-database/26/tgsql/overview-of-sql-plan-management.html)** 则直面云自治数据库频繁自动升级与刷统计引发的计划突变。早期草稿中常有人误传它是“后台影子跑新计划并在 1.5 倍耗时处半途熔断”，对照官方机制并非如此：当硬解析（Hard Parse）冒出一个前所未见的新计划时，Real-Time SPM 允许**前台会话真实执行这第一次新计划**，跑完立刻拿实测指标跟自动调优集（ASTS）里的历史参考计划对账（记录于 `DBA_SQL_PLAN_BASELINES` 的 `FOREGROUND_LAST_VERIFIED`）——变快了就纳进已接受基线（Accepted Baseline），一旦发现劣化立即将其拉黑，强制后续所有同类请求锁回已知好计划。它坦然承受了“首次探路请求可能变慢一次”的有限代价，却把计划退化的出血窗口从传统后台巡检的“几小时”压缩到了“单次执行周期”。

---

## 五、收束：一套贯穿索引、存储、分布式与优化器的系统设计守则

把这十四项横跨单机数据结构、EB 级存储、分布式事务与智能优化器的工作合拢到一起，四条跨越子领域的系统设计守则便水落石出：

1. **拆开读写与冷热的绑定粒度，把重活推迟到批处理窗口。** Bf-Tree 拆开了百字节热记录缓存与 4KB 磁盘落盘页；PolarFS 拆开了 10GB 放置迁移块与 64KB 空间分配块；Tectonic 先用三副本接小追加，封闭后再批转 `RS(10,4)` 纠删码；“日志即数据库”先靠纤薄 Redo 保微秒提交，再由后台 Page Server 异步拼页。**凡是试图用一个固定粒度同时讨好前台低延迟与后台高容量的设计，最后都在交双倍的放大税。**
2. **读侧知识必须贴身就近，写侧采集与训练一律异步旁路。** PolarSwitch 本地缓存卷拓扑、Ultron 把 `Softstore` 历史异步推到集群本地内存、Redshift 把增量 Sketch 离线转回常规 Catalog 字段并坚决撤出 `INSERT` 关键路径、ScalePQO 在后台线程异步微调模型——反馈晚生效一轮顶多少赚一点加速，要是把远程网络调用或重计算塞进每一笔前台请求，系统立马就会变成新的可用性单点。
3. **消除虚假的全局全序，只为真正的物理冲突排队。** `ParallelRaft` 放行互不重叠 LBA 的乱序持久化；HopsFS 用逐级 INode 细粒度锁取代单机全局锁；Aurora Limitless 靠有界误差时钟 + 参与分片自举的 `Lead Shard` 绕开中心化事务协调器；FuxiShuffle 在局部盘坏掉时保留下游已读完的好分片继续推进。真正释放并发吞吐的关键，在于精准识别哪些操作在语义上可交换（Commutative），把串行协调严格限制在真实数据冲突之内。
4. **在自治闭环里实行“候选生成、打分排序与安全护栏”三权分立。** 工业级系统真正优化的目标函数从来不是单纯的平均耗时，而是一笔把尾部灾难算进去的风险账：
   $$\text{Utility} = \mathbb{E}[\text{Gain}] - \lambda_1 P(\text{Regression}) - \lambda_2 \text{WorstCasePenalty} - \lambda_3 \text{ControlOverhead} - \lambda_4 \text{MaintenanceCost}$$
   用启发式或大模型蒸馏拓宽候选集，用领域自适应或聚类排序模型挑优，最后用**单调状态机（Ultron）、抽样影子预验（AutoLiquid）或前台实测基线熔断（Oracle SPM）**死守底线。如果把未来工业级优化器的契约写成伪代码，它早已不再是单纯的 `optimize(logical_plan, stats) -> physical_plan`，而是：

```sql
-- 现代工业级反馈优化器的概念契约（附带可审计的决策溯源）
optimize(
    logical_plan,
    catalog_statistics,   -- 增量 Sketch 维护的常规统计基石
    execution_history,    -- 绑定子计划 Hash 与表规模因子的真实轨迹
    environment_prior,    -- 多租户集群负载期望先验
    safety_policy,        -- 单调晋升规则 / 计划基线锚点
    state_version         -- Schema 与数据分布失效版本号
) -> (physical_plan, decision_provenance)
```

其中最不起眼却最救命的返回值正是 `decision_provenance`（决策溯源）：明确记录这次计划改动究竟采信了哪条历史样本、哪个模型版本、在什么漂移条件下必须自动作废。只有当每一次“变聪明”都附带着可解释的来龙去脉与一键退回原生路径的逃生门时，前沿论文里的精巧算法，才真正算长成了扛得住凌晨三点生产洪峰的数据库内核。

---

## 参考文献与范围说明

> **文献范围说明**：本文涵盖的十四项核心系统中，Bf-Tree、HopsFS、PolarFS、Tectonic、Aurora Limitless、FuxiShuffle、Redshift Re-invented、Redshift Incremental Statistics、LOAM、Ultron、ScalePQO 与 SQL Server LLM Index Tuning 均对照公开论文全文与实验章节精读整理；AutoLiquid 与 Oracle Real-Time SPM 截至初稿整理窗口（2026 年 8 月）主要依据 [VLDB 2026 Industry 官方议程摘要](https://vldb.org/2026/program.html)及对应官方产品技术文档做公开设计研读，未超出公开材料推测内部未披露阈值。文中引述的吞吐倍数与延迟数据均对应各原论文在特定硬件、数据集及定制基线下的实测报告。

1. **Bf-Tree** — Xiangpeng Hao, Badrish Chandramouli. *Bf-Tree: A Modern Read-Write-Optimized Concurrent Larger-Than-Memory Range Index*. [PVLDB 17(11), 2024](https://www.vldb.org/pvldb/vol17/p3442-hao.pdf) ([DOI](https://doi.org/10.14778/3681954.3682012), [GitHub](https://github.com/microsoft/bf-tree), [Design Docs](https://github.com/XiangpengHao/bf-tree-docs)). 对照范围索引文献：[ART (ICDE 2013)](https://db.in.tum.de/~leis/papers/ART.pdf)、[Wormhole (EuroSys 2019)](https://dl.acm.org/doi/10.1145/3302424.3303955)、[LeanStore (ICDE 2018)](https://db.in.tum.de/~leis/papers/leanstore.pdf).
2. **HopsFS** — Salman Niazi et al. *HopsFS: Scaling Hierarchical File System Metadata Using NewSQL Databases*. [FAST 2017](https://www.usenix.org/system/files/conference/fast17/fast17-niazi.pdf) ([Session](https://www.usenix.org/conference/fast17/technical-sessions/presentation/niazi), [GitHub](https://github.com/hopshadoop/hops)).
3. **PolarFS** — Wei Cao et al. *PolarFS: An Ultra-low Latency and Failure Resilient Distributed File System for Shared Storage Cloud Database*. [PVLDB 11(12), 2018](https://www.vldb.org/pvldb/vol11/p1849-cao.pdf) ([DOI](https://doi.org/10.14778/3229863.3229872)). 对照云原生“日志即数据库”架构：Amazon Aurora (SIGMOD 2017)、Microsoft Socrates / Hyperscale (SIGMOD 2019 & [SIGMOD 2026](https://doi.org/10.1145/3788853.3803083))、Google AlloyDB (VLDB 2024).
4. **Tectonic** — Satadru Pan et al. *Facebook's Tectonic Filesystem: Efficiency from Exascale*. [FAST 2021](https://www.usenix.org/system/files/fast21-pan.pdf) ([Meta Engineering Blog](https://engineering.fb.com/2021/06/21/data-infrastructure/tectonic-file-system/), [Meta 2026 AI Storage Blueprint](https://engineering.fb.com/2026/07/01/data-infrastructure/metas-ai-storage-blueprint-at-scale/)).
5. **Aurora Limitless** — Dmitry Arkhangelskiy et al. *Aurora PostgreSQL Limitless Database: Building a Highly Scalable OLTP Database*. [SIGMOD Companion 2026](https://software.imdea.org/~gotsman/papers/limitless-sigmod26.pdf) ([DOI](https://doi.org/10.1145/3788853.3803089), [AWS Docs](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/limitless.html)).
6. **FuxiShuffle** — Yuhao Lin et al. *FuxiShuffle: An Adaptive and Resilient Shuffle Service for Distributed Data Processing on Alibaba Cloud*. [SIGMOD Companion 2026](https://arxiv.org/pdf/2602.22580) ([DOI](https://doi.org/10.1145/3788853.3803085)).
7. **Redshift Re-invented** — Nikos Armenatzoglou et al. *Amazon Redshift Re-invented*. [SIGMOD 2022](https://doi.org/10.1145/3514221.3526045) ([Amazon Science](https://www.amazon.science/publications/amazon-redshift-re-invented)).
8. **Redshift Incremental Statistics** — *Incremental Query Optimizer Statistics in Amazon Redshift*. [PVLDB 2026](https://www.amazon.science/publications/incremental-query-optimizer-statistics-in-amazon-redshift).
9. **LOAM** — *Learned Query Optimizer in Alibaba MaxCompute: Challenges, Analysis, and Solutions*. [SIGMOD 2026 Industry](https://arxiv.org/abs/2602.07336) ([SIGMOD Industry Program](https://2026.sigmod.org/sigmod_industry_papers.shtml)).
10. **Ultron** — *Ultron: History-Based Query Optimization at Databricks*. [PVLDB 2026](https://doi.org/10.14778/3827998.3828038).
11. **ScalePQO** — *Towards Industrial-Scale Parametric Query Optimization*. [PVLDB 19, 2026](https://www.vldb.org/pvldb/vol19/p4303-mo.pdf) ([Industry Artifact](https://github.com/songsong945/RankPQO-industry)).
12. **LLM-Driven Index Tuning** — Xiaoying Wang, Wentao Wu, Vivek Narasayya, Surajit Chaudhuri. *Evaluating the Practical Effectiveness of LLM-Driven Index Tuning on Microsoft SQL Server*. [PVLDB 2026](https://arxiv.org/abs/2603.09181).
13. **AutoLiquid & Real-Time SPM** — [VLDB 2026 Program](https://vldb.org/2026/program.html)；[Databricks Liquid Clustering Docs](https://docs.databricks.com/aws/en/tables/clustering)；[Oracle 26ai SQL Plan Management Docs](https://docs.oracle.com/en/database/oracle/oracle-database/26/tgsql/overview-of-sql-plan-management.html) & [Oracle Optimizer Blog](https://blogs.oracle.com/optimizer/what-is-realtime-spm).
