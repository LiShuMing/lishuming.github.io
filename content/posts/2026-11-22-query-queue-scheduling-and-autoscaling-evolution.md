---
title: "查询队列、资源调度与 AutoScaling 深度推演：如何驯服突发大 Query 并跨越 CPU/GPU 异构边界"
slug: "query-queue-scheduling-and-autoscaling-evolution"
date: 2026-11-22T17:55:00+08:00
lastmod: 2026-11-22T17:55:00+08:00
categories:
  - 分布式系统
  - 数据库
  - AI 基础设施
tags:
  - Query Queue
  - AutoScaling
  - 资源调度
  - StarRocks
  - ClickHouse
  - Snowflake
  - BigQuery
  - Databricks
  - Kubernetes
  - Kueue
  - Volcano
  - Ray
  - Daft
description: "从生产集群中突发大 Query 拖垮高并发小查询的典型故障出发，系统剖析 StarRocks V2、ClickHouse、Trino/Velox、Snowflake QAS、BigQuery 无损抢占、Databricks IWM 以及 Ray/Daft/K8s Kueue+Volcano 的资源调度与弹性架构，归纳应对混合负载的四道纵深防线与异构演进路径。"
draft: false
toc: true
math: true
---

周一早上九点半，一套共享分析集群正平稳扛着每秒 2,000 次、平均耗时 30 毫秒的 BI 看板查询。突然，某位分析师敲下了一条忘了带分区过滤条件的五表关联查询，瞬间扫向 10 TB 历史明细表。

接下来不到十秒钟，几乎所有缺乏深度资源治理的数据平台都会沿着同一条多米诺骨牌倒下：最轻的后果是队头阻塞（Head-of-Line Blocking），2,000 QPS 的小查询全部卡死在大查询身后，P99 延迟飙升 50 倍；更糟的情况是大查询迅速吃光共享内存池，触发节点 OOM，把同机无辜运行的几十条在线查询一并带走；而监控告警触发的 AutoScaling 忙活了两三分钟才把新机器拉起来，此时高峰已经过去，新扩的冷节点既没帮上忙，反而留下一笔白花花的账单。

要真正解开“资源调度、突发大 Query 隔离与弹性伸缩”这道连环题，不能停留在某个产品的配置参数上，必须先看清底层究竟卡着哪三道物理与数学矛盾。

---

## 一、第一性原理：为什么单靠“排队”永远治不好突发大 Query？

如果我们按最朴素的直觉去修上面那次故障——给集群加一个“最大并发数队列”加上“CPU > 70% 就自动扩容”——上线第一周就会发现它依然频频失效。根源在于三道绕不开的系统矛盾：

1. **静态估算与运行时现实的鸿沟（Planning vs. Runtime Gap）**
   准入队列必须在查询**开始执行之前**决定放不放行、扣多少配额。但优化器（CBO）手里只有静态统计信息——一旦表统计过期、连接键出现极端数据倾斜，或者下游是多模态 PDF/视频解码（内存可能放大 10–20 倍），一条估算只占 1 个槽位的“小查询”跑起来照样吃掉 80% 内存。**只做事前静态准入，必然漏放黑天鹅。**
2. **高并发扩容与单查扩容的方向冲突**
   扛住 5,000 QPS 的短查询，靠的是**水平加独立副本（Scale-out）**——把单条查询锁死在小规模节点组内闭环执行，避免跨节点 Shuffle 扇出随机器数平方级恶化；但消化一条扫描 10 TB 的突发大查询，需要的却是**更大单次内存池与更高并行度（Scale-up 或临时借算力）**。单一维度的弹性永远顾此失彼。
3. **资源隔离与集群利用率的天然悖论（且在 AI 集群里方向还会反转）**
   给每个业务线切死独立物理集群当然互不干扰，但波谷期 60% 以上的算力白白闲置；全部扔进共享池利用率高，可只要缺少细粒度抢占与降级手段，一颗大雷就炸全盘。更棘手的是，当工作负载从纯 SQL 延伸到多卡 GPU 训练与多模态数据管线时，连“谁饿死谁”的方向都发生了反转——不仅大任务会打爆内存，**源源不断的小作业还会见缝插针占满机器碎片，把需要连续 64 张 GPU 拓扑的大作业活活饿死**。

把这三道矛盾摆到桌面上，2025–2026 年无论是开源 OLAP、闭源云数仓还是云原生 AI 调度器，演进方向几乎全部收束到了**四道纵深防线**：

![现代数据引擎应对突发大 Query 与高并发混合负载的四道纵深防线](/images/diagrams/sys-query-queue-four-layer-defense.svg)

下面沿着这四道防线，横向拆解三大技术阵营各自是怎么出招的。

---

## 二、开源 OLAP 阵营：前端全局准入与执行内核仲裁的两条路线

在开源与 BYOC 存算分离世界里，计算集群通常由用户自主运维或托管在云 VPC 内，缺乏公有云那样无限大的多租户共享池，因此核心功夫下在**“前端全局准入队列”**与**“执行引擎内部精细仲裁”**两条互补路线上。

### 2.1 StarRocks Query Queue V2：用 `LogicalSlot` + 全局 `SlotManager` 根治惊群

早期 StarRocks（V1）的查询队列存在两个典型痛点：一是排队逻辑分散在各个 FE 节点本地，仅靠后端 BE/CN 定期心跳上报的 CPU/内存水位和单 FE 并发数做判断——心跳存在秒级滞后，高并发突发流量同时打到多个 Follower FE 时，各 FE 都误以为后端还有余量而同时放行，瞬间引发**惊群效应（Thundering Herd）**把后端打 OOM；二是按简单“并发数”一刀切，10 毫秒点查和 10 TB 扫描在队列里等价，大查询极易堵死小查询。

从 v3.3 演进至 v3.5 的 **Query Queue V2** 把这套控制面彻底重构为 **“标准化槽位抽象 + Leader 全局强一致收口”**：

1. **资源模型抽象：`LogicalSlot`**
   每个 Warehouse（CNGroup）的算力被标准化为固定总量的逻辑槽位（`MAX_SLOTS` / `REMAIN_SLOTS`）。每条 SQL 在规划阶段依据估算代价、扫描量与所属 Resource Group 属性换算出需要申请的 `LogicalSlot` 数，重查询多占槽位、轻查询少占槽位，彻底打破“按条数计数”的盲区。
2. **Leader-Coordinator 全局仲裁架构**
   全局唯一的 `SlotManager` 驻留在 **Leader FE**，按 Warehouse / CNGroup 维度统一掌管已分配槽位和全局 `Pending Queue`。任意 Follower FE 完成解析与优化后，通过异步 RPC 向 Leader 申请槽位：有余量则立即放行下发 Fragment；不足则进入全局队列等待，唤醒策略支持 **SWRR（平滑加权轮询，保障多租户公平）** 与 **SJF + Aging（短查询优先并随等待时间老化提权，既解队头阻塞又防大查询永久饿死）**。
3. **软状态设计与防泄漏工程细节**
   Slot 分配与释放属于高频微秒/毫秒级变更，绝不能写入 BDBJE/Raft 持久化日志引发元数据写放大，因此被设计为纯内存**软状态（Soft State）**：
   - **Follower 宕机防泄漏**：Leader 为各 Follower 持有的 Slot 维护租约与心跳活性检查，一旦某 Follower 失联或查询超时，自动 GC 回收其占用的全部 Slot，避免资源永久卡死；
   - **Leader 切主收敛**：新 Leader 上位后重置软状态，在极短时间内随在途查询心跳与释放请求自然收敛，切换窗口期由 CN 端硬内存水位兜底。
4. **配合 Resource Group 与 Spill 的大查询闭环**
   前端队列之上，StarRocks 通过 **Resource Group** 施加 CPU 硬限制与 Big Query 门禁（对单查扫描行数、CPU 时间和内存设阈值拦截）；放行后如果遇上估算偏差导致内存冲高，再由执行层的 **Operator Spill**（Hash Join / Agg / Sort 落盘）与 **Load Spill** 兜底保命。

当然，**集中式 Leader 全局队列也有清晰的架构代价**：当集群承接数万 QPS 的 5 毫秒极轻点查时，每条查询都跨网络找 Leader 做一次 `Acquire` + `Release`（两次 RPC + 锁竞争），会显著抬高 P99 延迟。工程上成熟的破局手段是**轻重分离（Fast-path）+ Follower 本地批量 Slot Lease 预借**——极轻点查走本地预借配额或无锁快通道，余额耗尽才向 Leader 批量续租，仅重查询与高水位状态严格走 Leader 全局串行仲裁。

### 2.2 ClickHouse：把资源仲裁沉入执行内核的树形调度器

与 StarRocks 重点打造“前端 FE 全局准入队列”不同，ClickHouse 在开源内核里走了一条极硬核的**执行层内核调度路线**，直接把操作系统级的资源仲裁器搬进了数据库引擎内部：

1. **原生 SQL 声明式资源与层级工作负载树（`CREATE RESOURCE` + `CREATE WORKLOAD`）**
   管理员可以直接用 SQL 定义底层物理资源（Master/Worker CPU 线程、指定磁盘的 Read/Write I/O 带宽），再把不同业务编排成树形层级并赋予优先级和权重：
   ```sql
   CREATE RESOURCE cpu (MASTER THREAD, WORKER THREAD);
   CREATE RESOURCE io (READ DISK `default`, WRITE DISK `default`);

   CREATE WORKLOAD all SETTINGS max_speed = 1073741824; -- 全局磁盘上限 1 GB/s
   CREATE WORKLOAD interactive IN all SETTINGS priority = 0, weight = 80;
   CREATE WORKLOAD batch       IN all SETTINGS priority = 10, weight = 20;
   ```
   执行期调度器（可通过 `system.scheduler` 实时审计）对 CPU 线程和磁盘吞吐严格实施 **Max-Min Fair Sharing + 优先级抢占**。空闲时 `batch` 大查询可以吃满 100% 机器核数和磁盘带宽；一旦 `interactive` 小查询进入，内核瞬间按权重把 80% 算力与 I/O 抢给高优交互查询，**从执行层直接打断大查询的垄断**。
2. **优雅过载保护（`MemoryOvercommitTracker`）**
   当并发内存逼近节点上限时，ClickHouse 不再粗暴让先触线的小查询随机 OOM，而是由 `MemoryOvercommitTracker` 介入，按优先级与内存占用动态限流、暂停，必要时精准牺牲低优先级或最吃内存的大查询，保住整机与高优业务稳定。
3. **ClickHouse Cloud 的弹性三件套**
   在云端托管版中，ClickHouse 进一步补齐了三个很具启发性的弹性能力：**Two-Window Lookback Recommender**（短窗口激进快扩 + 双窗口果断缩容，解决传统缩容过慢浪费云成本又防震荡）、**Scheduled Scaling**（可预测潮汐定时预扩），以及专门针对偶发巨型查询的 **On-Demand Compute**——遇到突发超大 ad-hoc 查询时，临时从托管资源池租借额外 Worker 节点挂给当前服务，算完即刻归还，不拉高常驻规格。

### 2.3 Trino 与 Velox 补齐的两块拼图

在开源体系里，还有两套机制非常值得纳入完整拼图：
- **Trino / Presto Resource Groups**：除了树形多租户并发与排队上限，它引入了**滑动时间窗内的累计 CPU/内存软硬配额**。很多时候打垮集群的不是 1 条大查，而是某个脚本每分钟发 1,000 条中等查询“蚂蚁搬家”；时间窗累计配额配合动态降权，专门治理这类持续性资源刷屏。
- **Meta Velox `Memory Arbitrator`**：在执行引擎底层建立跨 Query 的统一内存仲裁器。当高优查询申请内存受阻时，仲裁器主动跨 Query 选中低优或大内存占用的查询，强制触发其 Hash/Agg 算子 **Spill-to-Disk 并精准回收（Reclaim）内存**——把过去动辄报错重试的 OOM 事故，平滑降解为“大查询落盘变慢、小查询毫秒无感”。

---

## 三、闭源云数仓三巨头：如何从架构上“消解”离群大 Query？

相比自建开源集群，Snowflake、BigQuery 与 Databricks Serverless 手握庞大的公有云多租户算力底座，它们在**离群大查询隔离**与**秒级弹性**上已经演化出了三种风格迥异却极具代表性的范式。

### 3.1 Snowflake：双轴解耦 → QAS 离群卸载 → Adaptive Warehouses

Snowflake 早期最成功的设计决策，是**把“扛并发”和“扛单查复杂度”彻底拆开**：
- 高并发 BI 靠 **Multi-Cluster Warehouse** 水平增加同构独立小 Cluster（并提供 **Standard** 排队即扩保延迟、**Economy** 攒够负载再扩省成本两套策略）；
- 单条重查询靠改虚拟仓 T-shirt 规格（XS 到 6XL）做垂直扩容。

但这套双轴模型一直有一个经典痛点：如果一台 Small 仓 95% 的时间都在跑 200ms 报表，偶尔混进来 2 条扫描 10 TB 的离群大查询（Outlier Query），为了这 5% 的查询把整座仓库升级成 2XL 实在太烧钱，不升级又会把同仓报表全堵死。

Snowflake 给出的王牌解法是 **Query Acceleration Service（QAS）**——并且从 2026 年起在新建 Gen2 与多集群仓中**默认开启**：当优化器识别出具备大表扫描或重 DML（`MERGE`/`UPDATE`/`DELETE`）特征的离群查询时，**既不改动主仓规格，也不让它霸占主仓阻塞小查询**，而是将其扫描与过滤切片动态卸载到后台共享的 Serverless 算力池并行吃完（官方聚合实测对合格负载平均提速超 40%）。

更进一步，Snowflake 推出的 **Adaptive Compute / Adaptive Warehouses** 正在把“人工选 T-shirt 规格 + 配多集群上限 + 调 QAS 系数”全部收进黑盒：单一自适应算力池根据每条查询的复杂度与并发压力自动智能路由和伸缩，并逐步过渡到按查询算力计费。

### 3.2 Google BigQuery：打破虚拟仓边界的动态公平切分与“无损抢占”

与 Snowflake 的“虚拟仓 + 独立小集群”哲学完全相反，BigQuery 依托底层 Borg 与 Dremel **计算与持久化内存 Shuffle 彻底解耦** 的架构，取消了租户可见的节点与集群边界，把算力抽象为全局池化的 **Slot**：

- **动态 DAG 公平切分（Fair Scheduling）**：BigQuery 不会让大查询像石头一样卡在 FIFO 队头。在同一 Reservation 内，调度器随着每个查询所处 DAG Stage 的动态形态变化，实时按比例重分可用 Slot——空闲时一条大查询瞬间吃满数千 Slot 极速完结；只要几十条小查询并发进来，调度器立刻切出份额给小查询。
- **闲置借用与“无损抢占”（Idle Slot Sharing & Preemption）**：业务线 A 购买的 Baseline Slot 在波谷期会自动免费借给繁忙的业务线 B 跑重型批处理；一旦业务线 A 的交互查询唤醒，BigQuery **毫秒级把借出去的 Slot 抢占回来**。最精妙之处在于：**因为大查询的中间 Shuffle 数据稳稳躺在独立持久化内存 Shuffle 层里，失去 Slot 后大查询绝不会失败重算，只是平滑缩减并行度慢一点跑完**。
- **账单护栏**：通过 `Baseline Commitments + Fluid Autoscaling（秒级按需计费）+ Max Reservation Cap` 组合，既承接突发大查询，又用 Max Cap 锁死失控 SQL 的云账单上限。

### 3.3 Databricks Serverless SQL：用 ML 预测击穿估算盲区，用热池打穿冷启动

Databricks Serverless SQL 瞄准了传统调度器最痛的两处软肋——“静态规则估不准”和“云机拉起要几分钟”：

- **Intelligent Workload Management（IWM）**：引入机器学习模型在查询入站瞬间预测其真实的内存、CPU 与 I/O 消耗画像，取代滞后的 CPU 水位阈值和死板并发上限。凭借实时预测，IWM 能提前识别高内存大查询，将其与轻查询集群合理分流或动态排入智能队列，并结合 Photon 引擎的内存管理与自动磁盘 Spill 守住稳定底线。
- **Warm Pool 秒级弹性**：底层维护跨租户共享的预热计算池，将 Serverless SQL Warehouse 的启动与水平扩 Cluster 时间从 Classic 仓的约 4 分钟压缩至 **2–6 秒**，让突发 BI 高峰几乎在队列刚刚冒头时就被新集群消化掉。

---

## 四、第四道防线：生产级 AutoScaling 的四个隐蔽工程坑

很多团队以为“接上 K8s HPA 或云厂商 ASG 就有了 AutoScaling”，但真正把弹性跑进生产 OLAP 集群，90% 的故障都栽在下面四个细节里：

### 1. 触发信号陷阱：为什么绝对不能只看 CPU 利用率？
在数据库负载里，**CPU 利用率是最具欺骗性的指标**。一条大查询可能已经占满 85% 内存或耗尽了所有 `LogicalSlot`，导致队列里积压了 60 条等待查询，但因为正卡在对象存储 I/O 或锁等待上，节点 CPU 监控曲线只有 30%——只盯 CPU 会彻底漏扩。

生产级扩缩容必须坚持**“队列背压为主信号、资源水位为辅信号、不对称双窗口防抖”**：
- **扩容要快（保尾延迟）**：主信号紧盯全局队列的 **`Pending Depth` 与 `Queue Wait Time`**（例如排队等待超过 2 秒或排队数持续大于 0 达 15–30 秒立即触发扩容），辅信号叠加 `Slot 使用率 > 80%`、`MemUsedPct > 80%` 或 `Spill 率陡增`；
- **缩容要稳且不浪费**：必须同时满足 `Pending Queue == 0`、活跃资源持续低于低水位（如 `< 20%`）并跨过合理冷却期（Cooldown），借鉴 ClickHouse 的 **Two-Window Recommender** 思路——既防止周期性小波峰引发频繁启停震荡（Thrashing），又避免固定超长冷却窗口白烧云费用；
- **三层组合削峰**：1 分钟以内的毛刺由 **Query Queue 直接吸收**不盲目扩容；1–5 分钟突发靠 **Warm Pool** 秒级承接；早高峰报表等可预测潮汐走 **Scheduled Scaling** 提前预热。

### 2. 拓扑陷阱：为什么高并发不能靠把单个 Cluster 从 8 节点扩到 64 节点？
对于高并发短查询，把同一个计算组从 8 节点拉大到 64 节点往往适得其反：单查询 Fragment 调度开销增加、跨节点 Shuffle 通信呈 \(O(N^2)\) 膨胀、每次增删节点还会大面积扰动本地缓存映射。因此，**面向高并发的弹性物理形态应当是水平增减独立的同构副本（如 StarRocks CNGroup / Snowflake Multi-Cluster）**，让每条查询在单个小集群内部闭环完成；只有面向重型大 ETL 需要更大单次内存池时，才对单组做节点数扩容（Scale-up）。

### 3. 冷启动与缓存击穿陷阱：新节点上线瞬间把对象存储打崩
存算分离架构下，新扩出来的计算节点本地 SSD/内存缓存一片空白。如果刚一注册就立刻切入 100% 流量，不仅首查延迟暴涨，并发回源甚至会直接打爆 S3/OSS 的前缀 QPS 限流。标准解法是两件套：
- **一致性哈希（Consistent Hashing）**：缩扩容节点时最小化分片/Tablet 归属迁移范围，最大限度保住存量节点上的热缓存；
- **Slow Start 渐进放量**：新节点或新 CNGroup 上线后按权重渐进接流（例如 `10% → 50% → 100%`），同时后台异步预热热点表元数据与字典。

### 4. 缩容杀查陷阱：Graceful Drain 闭环
在 BYOC 或云原生 K8s 环境下，缩容绝不能直接对计算节点发 `SIGKILL`，必须严格执行四步优雅排空：
1. **标记 `Draining`**：控制面通知调度器将目标节点/CNGroup 置为排空态；
2. **摘除新流**：全局 `SlotManager` 立即停止向其分配新查询槽位；
3. **等待排空 + 僵尸查超时保护**：等待在途 Fragment 自然完结，同时设置最大 Drain 超时上限，防止个别死循环或僵尸长查询永久锁死节点回收；
4. **回收算力**：确认活跃查询归零后，归还至 Warm Pool 或调用底层云 API 销毁实例。

---

## 五、跨越异构边界：从 SQL 调度跃迁到 CPU/GPU AI 集群

过去两年，数据平台最大的变化在于：**同一套底座不再只跑结构化 SQL，还要同时承载文档解析、视频抽帧、Embedding 编码、VLM 批量推理与分布式模型训练。** 当我们试图把上面打磨成熟的 SQL 调度与 AutoScaling 体系迁移到 **CPU/GPU 异构多模态集群** 时，会立刻撞上四个结构性差异：

![从传统 SQL 一维槽位调度到 AI 时代多维 CPU/GPU 异构调度的范式跃迁](/images/diagrams/sys-sql-to-ai-heterogeneous-scheduling.svg)

正是为了解这四道新题，专有分布式执行引擎与云原生批调度栈演化出了截然不同的组件分工：

### 1. 集群层多租户调度：Kubernetes + Kueue / Volcano + Karpenter
- **Kueue（非侵入式准入队列层）**：卡在标准 `kube-scheduler` 之前，未拿到配额的 `Job` / `PyTorchJob` / `RayJob` 全部保持 `Suspended` 不产生碎片 Pod。通过 **`ClusterQueue` 与 `Cohort`** 实现跨团队公平共享与配额借用（并用 `borrowingLimit / lendingLimit` 防止激进小作业团队长期霸占大作业预留资源）。面对“碎片小作业饿死多卡大作业”的难题，Kueue 引入了**拓扑感知碎片整理抢占**——当大作业急需连续 NVLink/机架拓扑时，主动驱逐占住关键空隙的碎片小作业。
- **Volcano（批调度内核替换）**：原生内置 **DRF（Dominant Resource Fairness，主导资源公平）**——在 `[CPU, Mem, GPU, VRAM]` 多维强耦合资源下，按每个作业占比最高的那一维“主导资源”计算公平份额，彻底解决一维 Slot 无法描述异构资源失衡的困境；同时提供 **`PodGroup` Gang Scheduling（全有或全无原子准入）**，防止两个 32-GPU 作业各占 16 张卡互相死锁。
- **Karpenter（基础设施弹性层）**：通过 `ProvisioningRequest` 与 Kueue 联动，直接读取 Pending 作业的精确卡型（如 H100/B200）、高速互联（InfiniBand/EFA）和 Spot 策略，绕过笨重的传统节点组模板秒级拉起最优规格节点。

### 2. 管线内异构流控：Ray Data 与 Daft
K8s 管的是 Pod/Job 级的秒~分钟级调度，而深入单条多模态管线内部，真正防止“快上游把慢 GPU 撑爆 OOM”的，是执行引擎级的细粒度流式背压（更深入的内核对比见 [从 Ray 到 Daft：AI 原生多模态数据引擎的架构演进](/2026/11/20/ray-to-daft-multimodal-engine-evolution/) 与 [LLM 时代的非结构化数据基座：从 Object Table 到 BlobRef](/2026/11/22/object-table-and-blobref-in-llm-era/)）：
- **Ray Data**：显式为每个算子维护四维资源预算（`data_cpu_budget`、`data_gpu_budget`、`data_memory_budget`、`data_object_store_memory_budget`），通过细粒度 Generator Backpressure RPC 控流，防止上游对象存储拉取和 CPU 预处理把 Ray 共享内存 Object Store 写爆。
- **Daft（Swordfish + Flotilla）**：用每节点单个常驻 Worker Actor 压平分布式控制面风暴，节点内部借 Rust Push-based 执行器实施**深度优先（Depth-First）流式调度 + 严格字节上限 + Out-of-Core Spill**，确保单条重型多模态任务不会同时展开数百个解码后的巨型中间分区挤死同节点其他查询。

### 3. 从 SQL 弹性迁移到 AI 异构弹性的四项必须重塑
如果要在同一平台上统一承接 OLAP 与多模态 AI 管线，AutoScaling 机制必须完成四项升级：
1. **算力池解耦独立伸缩**：绝不能把 CPU 解码清洗池和 GPU 推理池绑在同一组同构节点里等比扩缩，必须按上游字节积压队列与 GPU SM/KV-Cache 水位分别独立弹性，动态对齐吞吐比；
2. **模型权重热池化**：拉起 GPU 节点再下载加载几十 GB 多模态模型权重动辄数分钟，远慢于无状态 SQL 节点；必须配套 **GPU Warm Pool + 权重 P2P/RDMA 局域网缓存**，并将高频基础模型（Embedding / OCR / 常用 VLM）**公共服务化**（共享常驻显存 + Continuous Batching）；
3. **字节级背压取代行数队列**：多模态解码存在 10–20× 内存膨胀，调度与流控必须全程以字节配额为准绳；
4. **从 Graceful Drain 升级为 Checkpoint 抢占续跑**：潮汐/Spot GPU 回收往往只有几十秒通知窗口，无法像短 SQL 那样慢慢等 10 分钟排空；昂贵 GPU 算子必须内置 **Row-Key 级异步幂等 Checkpoint**，被抢占后秒级换卡走 `Anti-Join` 续跑，不重复燃烧 Token 与 GPU 账单。

---

## 六、全景横向对比矩阵与四条架构设计铁律

把上面调研的 9 类代表系统收进同一张横向坐标系，各家在不同防线上的核心武器一目了然：

| 系统 | 准入与排队机制 | 突发大 Query / 异构隔离王牌 | 执行期防爆兜底 | AutoScaling 与冷启动治理 |
| :--- | :--- | :--- | :--- | :--- |
| **StarRocks (V2)** | Leader FE 全局 `SlotManager` + `LogicalSlot`（SWRR / SJF+Aging） | Resource Group CPU 硬限 + Big Query 阈值拦截 + 队列隔离 | Operator Spill + Load Spill + 内存水位熔断 | 队列驱动 CNGroup 水平副本 + Graceful Drain + 一致性哈希 |
| **ClickHouse** | 节点内 `CREATE WORKLOAD` 树形 Fair Sharing（CPU+IO 带宽） | 高低优先级权重抢占 + Cloud **On-Demand Compute 临时借节点** | `MemoryOvercommitTracker` 按优先级限流/牺牲大查 | **Two-Window Recommender**（快扩果断缩）+ 定时扩容 |
| **Snowflake** | 虚拟仓内部并发队列 + Adaptive 自动路由 | **QAS 离群大查旁路卸载至共享 Serverless 池**（Gen2 默认开） | 自动 Spill 至本地 SSD 再至远端对象存储 | Multi-Cluster（Standard/Economy）→ **Adaptive Warehouses** |
| **BigQuery** | 无 FIFO 死队列，**动态 DAG Stage 公平切分 Slot** | **Idle Slot Sharing + 毫秒级无损抢占（大查降速不失败）** | 独立持久化 Shuffle 内存层 + Spill + Max Slot Cap | Baseline + **Fluid Autoscaling** 秒级按需计费 |
| **Databricks Serverless** | **IWM（ML 实时预测内存/CPU/IO）** 智能准入与分流 | 预测分流 + 独立 Cluster 水平隔离 | Photon 内存管理 + 自动磁盘 Spill | **Warm Pool（2–6s 秒级拉起）** + 预测式扩缩 |
| **Trino / Presto** | 树形层级 Resource Groups（并发 + 滑动时间窗累计配额） | 按时间窗动态降权，防小查刷屏与大查霸占 | Memory Pool + Spill + 杀最大内存查询 | 依赖外部 K8s/HPA 联动，缺少原生秒级无损缩容 |
| **Velox (执行内核)** | 上层引擎负责 | 跨 Query 统一内存优先级仲裁 | **Memory Arbitrator 主动抢占 + Spill Reclaim** | 属于单机执行内核原语 |
| **Ray Data / Daft** | 四维预算（CPU/GPU/Mem/ObjStore）+ 深度优先流控 | 节点内有界流式背压 + 算力池物理隔离 | Out-of-Core Spill + Row-Key Checkpoint 续跑 | 依赖 Ray Autoscaler / K8s |
| **K8s (Kueue/Volcano/Karpenter)** | `ClusterQueue/Cohort` 借用 + **DRF** + **Gang 原子准入** | **拓扑碎片整理抢占** + `borrowing/lendingLimit` 防小作业饿死大作业 | Pod OOMKill / Eviction（粗粒度） | **Karpenter + ProvisioningRequest** 按精准卡型拉机 |

回望整条演进脉络，设计一套真正扛得住生产高压的查询调度与弹性系统，只需死守四条底层铁律：

1. **永远不要把稳定性押注在单一防线上**：事前准入只能防住 80% 的常规过载，剩下 20% 的统计估偏与突发黑天鹅，必须靠**执行期 CPU/IO 权重树 + 内存 Spill 仲裁 + 离群大查旁路卸载**联手兜底——把毁灭性的 OOM 降级为局部可控的减速。
2. **用队列背压驱动弹性，用双窗口与热池驯服延迟和账单**：CPU 利用率永远晚一步，排队深度与等待时长才是扩容的真北星；同时必须把“高并发加独立副本（Scale-out）”和“离群大查借算力（QAS/On-Demand）”彻底解耦。
3. **把控制面做成轻重分离的软状态**：全局强一致队列消灭惊群，但绝不让它成为万级点查的 RTT 瓶颈，也绝不把高频 Slot 状态塞进持久化共识日志；靠 Fast-path 预借解吞吐，靠租约心跳与后端硬水位解容错。
4. **跨入 AI 时代，调度器必须从“单维槽位”升维为“多维拓扑 + 字节背压 + 抢占续跑”**：在 CPU/GPU 异构世界里，调度既要防大任务打爆内存，更要防小碎片饿死多卡大作业；既要让计算池独立弹性，更要让昂贵的 GPU 计算具备随时被抢占、秒级换卡无损续跑的能力。

---

> **系列关联阅读**
> - 分布式调度器演进：[伏羲（Fuxi）与开源分布式调度器的架构演进](/2026/11/17/fuxi-and-open-source-schedulers-evolution/)
> - 多模态执行引擎：[从 Ray 到 Daft：AI 原生多模态数据引擎的架构演进，以及与 Spark、StarRocks 的深层分水岭](/2026/11/20/ray-to-daft-multimodal-engine-evolution/)
> - 云数仓内核与语义算子：[AI Functions、LLM 数据管线与云数仓引擎内核横评](/2026/11/20/ai-functions-and-llm-data-pipelines/)
> - SQL × LLM 融合架构：[当 SQL 引擎遇上大模型：从黑盒 UDF 到语义融合内核的架构推演](/2026/11/22/sql-llm-fused-engine-architecture/)
> - 非结构化数据基座：[LLM 时代的非结构化数据基座：从 Object Table 到 BlobRef 的查询流转、成本权衡与生态融合](/2026/11/22/object-table-and-blobref-in-llm-era/)
>
> **参考与调研基线**
> - **开源 OLAP 与执行内核公开文档（2025–2026）**：StarRocks 官方文档（Query Queue V2 `SlotManager`/`LogicalSlot`、Resource Group、Operator/Load Spill 与 Shared-Data Warehouse 架构）；ClickHouse 官方文档（`CREATE RESOURCE` / `CREATE WORKLOAD` 调度树、`system.scheduler`、`MemoryOvercommitTracker` 与 ClickHouse Cloud Two-Window Autoscaling / On-Demand Compute 工程博客）；Trino Resource Groups 与 Meta Velox Memory Management / Arbitrator 设计文档。
> - **闭源云数仓公开资料（2025–2026）**：Snowflake（Multi-Cluster Warehouses、Query Acceleration Service 2026 默认开启公告、Adaptive Compute / Adaptive Warehouses）；Google Cloud BigQuery（Editions、Slot Autoscaler、Fair Scheduling、Idle Slot Sharing 与 Preemption 文档）；Databricks（Serverless SQL Warehouses 与 Intelligent Workload Management 架构博客）。
> - **云原生与 AI 异构调度规范**：Kubernetes SIG Scheduling **Kueue**（`ClusterQueue`、`Cohort`、Fair Sharing 与拓扑抢占）与 **Karpenter**（`ProvisioningRequest` / DRA）；CNCF **Volcano**（DRF 与 `PodGroup` Gang Scheduling）；Ray Data 流式执行器与 Daft `Swordfish`/`Flotilla` 公开架构资料。
