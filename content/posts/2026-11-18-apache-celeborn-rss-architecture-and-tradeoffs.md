---
title: "重构分布式 Shuffle：Apache Celeborn 架构剖析与 Spark ESS 演进边界"
slug: "apache-celeborn-rss-architecture-and-tradeoffs"
date: 2026-11-18T10:45:00+08:00
lastmod: 2026-11-18T10:45:00+08:00
categories:
  - 分布式系统
  - 数据库
tags:
  - Apache Celeborn
  - Apache Spark
  - Shuffle
  - 存算分离
  - 云原生
  - Apache Gluten
description: "从 M×R 随机磁盘寻道与按 Map 组织文件的物理矛盾切入，拆解 Apache Celeborn 的控制面与数据面架构、PushMergedData 合包、8MB Chunk 零拷贝追加写、动态 PartitionSplit 软切分、双副本 Revive 无感容错与三级内存流控，并复盘 Spark AQE 倾斜读放大与控制面风暴等真实工程权衡。"
draft: false
toc: true
math: true
---

在一个拥有 \(M = 10,000\) 个上游 Map 任务与 \(R = 10,000\) 个下游 Reduce 任务的 Spark 批处理阶段里，总共需要传输 `5 TB` 洗牌数据。算一下平均值就会发现一个尴尬的数字：每个 Map 任务分给单个 Reduce 分区的数据块，平均只有区区 **`50 KB`**。

在原生 Apache Spark 的 `SortShuffleManager` 与外部洗牌服务（`ExternalShuffleService`，简称 ESS）下，这 `5 TB` 数据是如何落盘和读取的？

早期 Spark 1.x 的 `HashShuffleManager` 曾尝试让每个 Map 为每个下游分区单独建一个文件，结果全集群瞬间炸出 \(10^8\) 个碎文件，直接把操作系统的 inode 表与文件句柄池打穿。后来的 `SortShuffleManager` 为了把文件总数压回 \(M\) 个，改成了**按 Map 任务组织物理文件**：每个 Map 任务只产出一个 `.data` 数据文件和一个 `.index` 偏移索引文件，内部按 `partitionId` 排序，把发往 `P0, P1, ..., P9999` 的 `50 KB` 小切片首尾相连挤在同一个文件里。

文件句柄危机虽然解除了，下游读取端的物理灾难却原封不动地留了下来。当 `Reduce 500` 启动时，属于它的 `500 MB` 分区数据被横向撕裂成了 10,000 个 `50 KB` 的微末切片，嵌在全集群 10,000 个不同 `.data` 文件的正中间。于是全集群总共要跨越网络发起 **一亿次** 细粒度拉取请求，触发一亿次磁盘随机 `lseek` 寻道：

- 在机械硬盘（HDD）池上，磁头寻道耗时彻底淹没实际传输，磁盘 I/O Util 长期钉死在 `100%`；即便是 NVMe SSD，本地先 Spill 再归并的两次落盘加上高并发随机小读，也会造成严重的写放大与吞吐衰减。
- 海量一次性随机小读根本无法命中内核预读（Read-ahead），反而把 Linux PageCache 里的热页冲刷殆尽，拖累同机混部的其他任务出现剧烈延迟毛刺。
- 计算节点一旦挂载了单副本的本地 Shuffle 数据，Kubernetes 弹性缩容（Dynamic Allocation）与云上廉价竞价实例（Spot Instance）就被捆住了手脚——回收任意一台节点都会丢数据并触发 `FetchFailedException` 级联重算（关于 `FetchFailed` 与 `INDETERMINATE` 全图回滚的连锁反应，参见前文 [从 BSP 到 MPP 与列式卸载]({{< relref "2026-11-18-spark-bsp-gluten-velox-and-starrocks-execution.md" >}})）；遇到单个 `200 GB` 的倾斜大分区，还会直接把所在宿主机的单块本地盘写爆。

要从根源上拔除这五颗钉子，唯一的出路就是把物理文件的组织维度彻底翻转过来：**不再按上游 Mapper 攒文件，而是在数据离开计算节点的那一刻，直接按目标 Reducer Partition 在远端追加聚合成连续大文件。** 这正是 **Apache Celeborn** 等新一代远程洗牌服务（Remote Shuffle Service，RSS）立身的物理支点。

![原生 Spark 按 Map 聚合拉取与 Apache Celeborn 按 Partition 追加推送对比](/images/diagrams/db-celeborn-push-shuffle-arch.svg)

---

## 一、控制面与数据面解耦：Celeborn 的四层协同拓扑

作为一个独立于计算引擎部署的多租户存储服务，Celeborn 把“槽位调度与元数据一致性”和“高频字节流吞吐”拆进了四个各司其职的角色里：

1. **`Master` 集群（全局资源控制面）**：基于 Apache Ratis（Raft 共识协议）组成高可用集群。它不介入任何具体的数据包读写，只负责接收各 Worker 心跳上报的可用磁盘容量、近期刷盘吞吐与负载健康度，在作业申请洗牌资源时通过负载感知（Load-Aware）或轮询策略分配物理槽位（Slots）。
2. **`LifecycleManager`（作业级元数据中枢，驻留 Spark Driver）**：以单例形式运行在计算引擎的协调节点内，掌管当前应用全生命周期的 Shuffle 元数据。它维护着核心路由表 `(shuffleId, partitionId) -> List<PartitionLocation>`（每个位置带有单调递增的 `epoch` 代次号），统一编排注册槽位（`RegisterShuffle`）、故障切流（`Revive`）、倾斜动态分裂（`PartitionSplit`）以及阶段收尾时的文件封账（`CommitFiles`）。
3. **`ShuffleClient`（数据面客户端，驻留 Executor 进程单例）**：接管 `CelebornShuffleManager` 的实际读写通道。同一进程内的几十个并发 Task 共享这套 Netty 长连接池，在发送端管控飞行窗口（`maxBytesInFlight`）与批次合包，在读取端按连续块流式拉取数据。
4. **`Worker`（远程聚合存储节点）**：数据落地的终点。入站网络线程（`PushDataHandler`）收到字节流后，将其分发至对应分区的写入器（`ReducePartitionDataWriter`），先写入约 `256 KB` 的堆外内存接收缓冲，攒满后交由绑定物理磁盘挂载点的独立 `Flusher` 线程池异步顺序落盘，同时由全局 `MemoryManager` 实时监控堆外水位。

---

## 二、从随机抠块到顺序流：六项核心架构演进

围绕“按分区远端追加聚合”这一主轴，Celeborn 在客户端缓冲、网络协议、倾斜治理、多副本容错与流控上做了一整套环环相扣的工程改造。

### 2.1 `ReducePartition` 追加写与 `8MB Chunk` 零拷贝读取

在 Celeborn 中，来自上万个不同 Mapper、但同属 `Partition 0` 的数据块，会被统一推送到承载 `Partition 0` 的指定 Worker。Worker 端的 `ReducePartitionDataWriter` **不在服务端做任何耗费 CPU 的跨键归并排序**，而是把先后到达的同分区批次直接按流顺序追加（Append-only）写进同一个物理文件。

每当文件写入跨过默认 **`8 MB`** 边界，写入器就在内存索引中记下一个 `Chunk` 偏移坐标。等到下游 `Reduce 0` 启动读取时，面对的不再是一万台机器上的 `50 KB` 碎渣，而是单台（或少数几台）Worker 上整齐切好的 `8 MB` 连续巨块。客户端只需发起寥寥数次 `ChunkFetchRequest`，Worker 便通过 Netty 的 `DefaultFileRegion`（底层调用 Linux `sendfile` 系统调用）将 `8 MB` 磁盘页直接从内核态零拷贝送入网卡队列，把机械盘的顺序读写带宽彻底喂饱。

### 2.2 双模客户端写入器与 `PushMergedData` 合包降噪

把聚合点推向网络之后，客户端本身立刻面临两个新挑战：一是如果下游有 `50,000` 个分区，客户端怎么缓冲才不 OOM？二是每个分区每次只攒几 KB 就发包，岂不是把磁盘风暴变成了网络小包风暴？

Celeborn 用“双模 Writer + 目标节点级合包”化解了这对矛盾：
- **小中规模分区走 `HashBasedShuffleWriter`**：直接为每个分区分配独立的序列化内存缓冲（默认 `64 KB`），写满即异步推出，彻底省去 Map 端本地排序与本地落盘开销。
- **超大规模分区走 `SortBasedShuffleWriter`**：当分区数高达数万、按分区开缓冲会撑爆 Executor 内存时，自动切换为类似 Tungsten 的单块紧凑大内存加指针排序模式——所有记录追加进同一片内存池，按 `partitionId` 排完指针后再成批切块推送，使客户端内存占用保持恒定可控。
- **`PushMergedData` 跨分区合包**：假设某台 Worker 同时托管了当前作业的 200 个不同分区，而某个 Map 任务在这 200 个分区上各自只产出了 `8 KB` 数据。`ShuffleClient` 不会傻傻发出 200 次微型 `PushData` RPC，而是把路由目的地指向**同一台 Worker** 的不同分区小块打包塞进单个 `PushMergedData` 请求体里一次发走，由对端 Worker 在内存里解包分流给各自的 `FileWriter`，将全网 RPC 并发度直接压低一到两个数量级。

### 2.3 无阻塞动态软切分（Soft `PartitionSplit`）：免疫单盘热点

按分区聚合有一个显而易见的副作用：如果 `Partition 5` 遭遇严重数据倾斜、单分区膨胀到 `300 GB`，岂不是会把负责托管 `Partition 5` 的那台 Worker 磁盘直接写穿？

为此，Celeborn 引入了基于代次（`epoch`）轮转的 **Soft Split（无阻塞软切分）**：
1. Worker 的 `FileWriter` 持续监测活跃分区文件的物理字节数。一旦跨过切分阈值（`celeborn.worker.shuffle.partitionSplit.threshold`，默认 `1 GB`），Worker 并不拒绝当前写入，而是在返回给客户端的响应中捎带一个 `SOFT_SPLIT` 状态码。
2. 客户端收到信号后异步上报给 Driver 端的 `LifecycleManager`；**在控制面协商新位置的窗口期内，旧位置 `PartitionLocation(epoch=0)` 继续正常接收在途数据，整个发送流水线零停顿**。
3. `LifecycleManager` 在另一台磁盘充裕的 Worker 上开辟新槽位 `PartitionLocation(epoch=1)` 并广播路由更新，后续数据流平滑切往新机器。
4. 进入 Reduce 阶段时，`GetReducerFileGroup` 将 `epoch = 0, 1, ..., k` 的所有物理分片打包返回给客户端串联读取，把原本足以压垮单盘的百 GB 倾斜洪峰均匀摊平到了整个存储集群。

### 2.4 主从流水线复制与 `Revive`：把故障消灭在存储层内部

为了彻底根除单点坏盘引发的 Spark Stage 级回滚，Celeborn 支持主从双副本写入（`celeborn.client.push.replicate.enabled=true`），但并没有让计算节点傻傻向两台存储节点发双份数据，而是采用 **Pipeline Replication（链式流水线复制）**：Executor 只发一份数据给 `Primary Worker`，Primary 写入自身接收缓冲的同时，通过存储集群内部网口异步转发给 `Replica Worker`，省去计算节点一半的上行出口带宽。

在此之上，读写两端的节点故障均实现了对上层计算引擎透明的自治自愈：
- **写中途节点失联（`Revive` 切流）**：某台 Primary 或 Replica 宕机导致推送超时后，`ShuffleClient` 立刻向 `LifecycleManager` 发起 `Revive` 申请新一对 `(Primary', Replica', epoch+1)`。因为已落入旧 `epoch` 的历史块在存活的另一副本上完好无损，客户端只需将本地未收到 ACK 的少量飞行批次重推向新 `epoch` 即可，**正在跑的 Map Task 无需报错重来**。
- **读中途节点失联（副本无缝切换）**：Reducer 拉取某个 `Chunk` 超时后，客户端自动切向配对副本拉取相同块，全程不向 Spark 抛出 `FetchFailedException`。

### 2.5 三级硬水位反压与类 TCP 拥塞控制

共享存储集群最怕一个失控的万核大作业瞬间打爆所有 Worker 的堆外内存（Direct Memory），把同集群正常的线上小作业一起拖死。Celeborn 在 Worker 侧与客户端侧联合构筑了两道防线：
- **三级内存硬水位（`MemoryManager`）**：当单台 Worker 堆外内存占用触及 **`85%`**（暂停接收线）时，立即关闭入站客户端 Netty Channel 的 `AUTO_READ` 属性（停止从内核 Socket 缓冲区取包，借由 TCP 滑动窗口满载自然卡住远端发送方），同时唤醒全部刷盘线程全力泄洪；若继续冲高至 **`95%`**（暂停复制线），连内部副本同步通道一并挂起；待水位回落至 **`70%`**（恢复线）以下再重新开闸。
- **AIMD 慢启动与拥塞避免**：借鉴 TCP 拥塞控制思想，Worker 持续比对入站速率与底层磁盘刷盘速率；一旦积压抬头，主动在响应中向高频发送大户下发拥塞标记，客户端随即按“加性增、乘性减（AIMD）”规律主动收缩发送速率，实现细粒度的租户流量整形。

### 2.6 列存卸载直通与多级冷热分层

当上层 Spark 挂载了 **Apache Gluten + Velox** 向量化引擎时，原生 Shuffle 路径不得不先把列式 `RowVector` 转回行式 `UnsafeRow` 才能洗牌。Celeborn 原生提供了配套的列式洗牌写入器（`CelebornColumnarShuffleWriter`），直接在 C++ 层把列式批次按分区拆分成紧凑压缩列块推入 Celeborn，砍掉整段行列互转税；而在存储介质上，Celeborn 支持 `Memory -> Local SSD/HDD -> S3/OSS/HDFS` 三级分层，几十 MB 的短平快 Shuffle 全程驻留远端堆外内存，跑出近似 MPP 内存交换的低延迟，百 TB 级巨型洗牌则平滑溢写至廉价对象存储。

---

## 三、工程深水区的四个真实代价与避坑指南

把重排点从本地磁盘搬到远端网络并非免费的午餐。在大规模生产落地 Celeborn 时，有四笔工程账必须提前算清。

### 3.1 Spark AQE Skew Join 的“读放大”与 `FileSorter` 冲突

这是 Push-based RSS 与原生 Spark 优化器碰撞最激烈的一处细节。

当 Spark 的自适应执行（AQE）检测到某个 Reduce 分区发生数据倾斜时，会启动 `OptimizeSkewedJoin`，把这一个大 Reduce 任务拆成多个并行子任务。那么原生 Spark 是按什么维度切分这一个分区的数据呢？答案是**按上游 Mapper 编号区间 `[startMapIndex, endMapIndex)`**——比如子任务 A 只读 `Map 0..499` 产出的块，子任务 B 只读 `Map 500..999` 产出的块。在原生 ESS 里，文件本来就是按 Mapper 存的，指哪读哪毫不费力。

可到了 Celeborn 里，1,000 个 Mapper 推来的数据早已按照网络到达先后，**乱序交错地追加进了同一个 `ReducePartition` 物理文件**！每一个 `8 MB Chunk` 里面都混杂着来自 `Map 0` 到 `Map 999` 的记录。

如果在未经适配的情况下强行响应原生 Spark 的 `[startMapIndex, endMapIndex)` 切片请求，就会掉进两难陷阱：要么让每个倾斜子任务把整个几 GB 的分区文件全量拉回客户端、逐批检查包头丢掉不属于自己 `mapId` 范围的数据（造成数倍网络读放大）；要么强迫 Worker 在提供读取前临时启动 **`FileSorter`**，把磁盘上几个 GB 的大文件重新读出来按 `mapId` 排序构建二级偏移索引（造成严重的读前卡顿与额外磁盘 I/O）。

**根治方案**：启用 Celeborn 的自适应倾斜读取优化（`celeborn.client.adaptive.optimizeSkewedPartitionRead.enabled=true`，配合 Spark 3.5+ 的 `ShufflePartitionSpec` 扩展或对应引擎补丁），彻底抛弃按 `mapId` 切分的旧契约，直接顺应 Celeborn 的物理布局——**按 `PartitionLocation` 分片与 `8 MB Chunk` 偏移区间**将大分区均匀切给多个子任务，零排序、零读放大地跑通倾斜连接。

### 3.2 Driver 端 `LifecycleManager` 的控制面风暴

由于所有分区的 `PartitionSplit` 与 `Revive` 协调都汇总在 Spark Driver 内部的 `LifecycleManager`，当遇到 `50,000` 分区乘以百 TB 数据的巨无霸作业时，成千上万个分区可能在同一分钟内集体跨过 `1 GB` 阈值上报切分请求，瞬间塞爆 Driver 的 RPC 队列甚至引发 Driver OOM。面对此类超宽洗牌作业，工程上一方面要开启客户端批量合并变更请求（`celeborn.client.rpc.batchHandleChangePartition.enabled=true`）与细粒度分段锁，另一方面应将单文件软切分阈值适度调高至 `2–4 GB`，在磁盘均衡度与控制面信令开销之间取得平衡。

### 3.3 网络流量放大（\(2\times\text{–}3\times\)）与本地 NVMe 的盈亏线

算一下全链路网卡字节数就能看清物理边界：在原生本地盘 Shuffle 中，Map 写本地盘（`0` 网络），Reduce 跨机拉取（\(1\times\) 网络，同机分片还可走本地文件直读）。而在 Celeborn 双副本模式下，数据要经历 `Map -> Primary`（\(1\times\)）、`Primary -> Replica`（\(1\times\)）和 `Worker -> Reduce`（\(1\times\)），**总网络带宽消耗膨胀到了原来的 3 倍**（单副本也是 2 倍）。

因此，倘若你的集群本身规模不大、每台计算节点都插满了极高规格的本地 NVMe SSD、反而是宿主机网卡带宽捉襟见肘，且没有频繁弹性缩容的诉求，那么小规模作业跑原生本地 NVMe ESS 的端到端延迟反而可能比走一圈远程网络更快。Celeborn 真正释放巨大红利的主战场，是**存算分离 K8s 弹性池、HDD 大容量存储节点、重度倾斜的万级并发宽作业，以及挂载了 Gluten 的列式计算管线**。

### 3.4 推测执行（Speculative Execution）下的无锁幂等去重

当长尾慢节点触发 Spark 推测执行时，`Task Attempt 0` 和 `Task Attempt 1` 会同时往同一个远程 `ReducePartition` 文件里推送内容重复的数据块。Celeborn 不在 Worker 写入热路径上加锁拦截，而是要求客户端在每个批次头部写入 `BatchHeader(mapId, attemptId, batchId, size)`；等 Map Stage 结束、`LifecycleManager` 敲定每个 `mapId` 唯一胜出的合法 `attemptId` 后，下游客户端在流式读取时结合 `RoaringBitmap` 按 `(mapId, batchId)` 实时滤除落败 Attempt 混入的冗余批次，以极低开销守住精确一次（Exactly-Once）语义。

---

## 四、主流分布式 Shuffle 方案选型全景对照

把原生 ESS 与近几年工业界演进出的三类代表性方案放在同一张坐标系下，各自的适用边界一目了然：

| 方案体系 | 架构形态 | 核心数据流机制 | 突出优势 | 固有局限与边界 |
| :--- | :--- | :--- | :--- | :--- |
| **Spark Native ESS** | 计算节点本地守护进程 | Pull-based，按 Mapper 本地归并单文件 | 部署最轻、零额外网络放大，本地 NVMe 小作业延迟低 | \(M \times R\) 随机小读、污染 PageCache、阻碍无状态缩容、单副本易触发级联重算 |
| **Spark Magnet** *(LinkedIn, Spark 3.2+)* | 基于 ESS 的混合尽力推送 | Map 写完本地后尽力（Best-effort）推往远端 ESS 合并，失败则回退拉取原块 | 无需运维独立存储集群，平滑兼容存量 YARN 部署 | 仍未斩断对计算节点本地盘的强依赖，无法支撑无盘 Serverless 容器 |
| **Apache Uniffle** *(腾讯开源)* | 独立远程 Shuffle 服务 | 客户端按分区缓冲聚合推送，Worker 内存排序缓冲后落本地盘或 HDFS/对象存储 | 深度打通超大状态溢出 HDFS 的混合存储链路，客户端预聚合粒度大 | 超大分区数下对客户端缓冲内存水位较敏感，倾斜切分走不同演进路线 |
| **Apache Celeborn** *(阿里开源)* | 云原生多引擎统一 RSS | `PushMergedData` + Append-only `8MB Chunk` + 动态 Soft Split + 流水线双副本 | 抗单盘倾斜与坏机自愈能力极强，深度打通 Spark AQE、Flink Hybrid Shuffle 与 Gluten 列存 | 双副本带来 \(3\times\) 网络流量放大；极端超大规模分区下需调优 Driver 控制面合并 |

---

> **参考资料与公开信息基线**
> - **Apache Celeborn 架构与源码**：`apache/celeborn` 官方架构文档及核心模块 `Master` (Ratis HA)、`LifecycleManager`、`ShuffleClient`、`PushDataHandler`、`ReducePartitionDataWriter`、`MemoryManager` 与 `FileSorter` 实现 · `celeborn.client.adaptive.optimizeSkewedPartitionRead` 与 Spark 3.5+ `ShufflePartitionSpec` 倾斜优化补丁说明。
> - **横向对比项目与论文**：Shen et al., *Magnet: Push-based Shuffle Service for Large-scale Data Processing* (VLDB 2020, LinkedIn) · Apache Uniffle 官方架构文档 · Apache Gluten + Celeborn Columnar Shuffle 集成文档。
