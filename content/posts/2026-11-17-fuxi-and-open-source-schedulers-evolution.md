---
title: "从 Fuxi 到现代开源调度器：增量资源协议与透明容错的演进对照"
slug: "fuxi-and-open-source-schedulers-evolution"
date: 2026-11-17T23:15:00+08:00
lastmod: 2026-11-17T23:15:00+08:00
categories:
  - 分布式系统
  - 数据库
tags:
  - Fuxi
  - YARN
  - Kubernetes
  - Flink
  - Spark
  - Ray
  - 资源调度
description: "以 VLDB 2014 Fuxi 论文中的增量资源管理协议、Hard/Soft State 分离与 JobMaster 透明容错为坐标，对照剖析 Apache YARN 3.x、Kubernetes（KEP-1287、Volcano、YuniKorn、Kueue、Koordinator）以及 Flink、Spark、Ray 引擎内调度器的演进路径与底层取舍。"
draft: false
toc: true
math: true
---

在一万台机器构成的共享大数据集群上，如果同时跑着几十万个并发 Task Instance，最朴素的调度系统会在两个地方同时崩盘。

第一个崩盘点在**资源装箱与通信开销**。假设中心调度器按固定步长分发“黑盒容器”（例如固定 4 核 16 GB，一旦启动生命周期内不可改配），又要求作业主控每次心跳都上报全量任务需求表，中心节点很快就会被序列化、锁竞争和资源碎片拖垮；而且作业运行到一半遇到数据倾斜需要加内存，如果容器不能原位变配，唯一的办法就是杀掉进程重来，把前面的中间计算全部作废。

第二个崩盘点在**控制面的故障恢复半径**。在万级节点集群上，机器掉线、进程 OOM 和网络抖动不是偶发事故，而是每分钟都在发生的常态。如果单台 Worker 挂掉导致整条作业重跑，或者作业主控（ApplicationMaster / Driver）重启一下就把几千个健康节点上正在跑的任务一并连坐杀光，长耗时作业的完成概率几乎归零。

早在 2014 年发表的 Fuxi 论文（*Fuxi: a Fault-Tolerant Resource Management and Job Scheduling System at Internet Scale*, PVLDB 2014）中，阿里巴巴就在论文里给出了非常清晰的解答：**中心化粗粒度装箱之所以受限，是因为缺少增量动态协商协议与轻量级容错收编机制**。而有意思的是，如果把时间轴拉到 2026 年，开源基础设施（YARN 3.x、Kubernetes KEP-1287 与批调度器家族）以及计算引擎内核（Apache Flink、Apache Spark、Ray）恰好各自走过了一段漫长的“分治演化”，用不同的架构拼出了对等解法，也暴露出了依然跨不过去的工程壁垒。

![Fuxi 增量协议与透明恢复 vs. 开源两级调度架构对应关系](/images/diagrams/sys-fuxi-vs-oss-schedulers.svg)

---

## 一、回到坐标原点：Fuxi（VLDB 2014）的两条内核机制

在建立对应关系之前，需要先把 Fuxi 原论文里的机制还原到具体协议与状态所有权上，避免把不同层面的容错混为一谈。

### 1.1 增量资源管理协议与动态运行时调整

Fuxi 的节点模型由中心主控 **FuxiMaster**（双活热备，通过飞天分布式锁互斥）、每作业主控 **JobMaster**（对应 YARN 的 ApplicationMaster）以及节点守护进程 **FuxiAgent** 组成。

为了支撑数十万并发实例，Fuxi 没有在 `JobMaster`、`FuxiAgent` 与 `FuxiMaster` 之间同步全量全局快照，而是设计了一套**增量资源管理协议（Incremental Resource Management Protocol）**：三方只交换资源变化量（Delta-based Updates），并在同一个协议框架内原生表达多维资源约束（CPU、内存、IO）以及三级数据本地性偏好（`Machine Queue -> Rack Queue -> Cluster Queue`）。在此基础上，Fuxi 体系后来又演进出 **DAG 2.0（Bubble 模式）**、**Fangorn（VLDB 2021）** 与 **IPA（VLDB 2022）**，让资源配置和并发粒度能够根据运行时统计在阶段边界动态重构。

### 1.2 两层故障域与 Hard/Soft State 剥离

Fuxi 论文所说的“用户透明容错（User-Transparent Failover）”，实际上由两套边界清晰、协同工作的机制共同构成：

1. **Worker / Agent 故障时的 Task 级恢复**：单台机器或 `FuxiAgent` 异常时，`JobMaster` 依托与 `FuxiMaster` 共享的**多级黑名单（Machine / Rack / Cluster Multi-level Blacklisting）** 迅速摘除故障域，仅在健康节点上对受损 Task Instance 发起局部重调度，作业与 `JobMaster` 自身毫发无损。
2. **Master / JobMaster 崩溃时的“活体收编（Live Adoption）”**：
   - `FuxiMaster` 绝不把瞬息万变的调度映射表写进持久化存储。它将状态严格切成两半——**Hard State**（作业描述、用户配置与机器黑名单，只在作业提交与结束时做轻量级检查点）与 **Soft State**（活跃资源分配与实例映射，完全驻留内存）。主备切换时，新 `FuxiMaster` 加载 Hard State 后，通过各 `JobMaster` 重传 `ScheduleUnit` 与偏好配置、各 `FuxiAgent` 反向汇报当前本机存活的实例列表，**在线拼回完整 Soft State 视图**。
   - 当 `JobMaster` 自身崩溃时，`FuxiMaster` 会拉起新的 `JobMaster` 实例，后者从轻量级检查点恢复已完成任务状态，并**直接重新接管健康节点上仍在运行的 Task Instance**，既不必重启整条作业，也不丢弃健康节点的计算进度。

---

## 二、开源调度生态的四层映射全景

如果去开源生态里找“Fuxi 的替代品”，会发现没有任何一个单一组件能直接画等号。开源界将 Fuxi 的能力拆进了**底层资源调度基座（YARN / Kubernetes + Batch 插件）**和**上层计算引擎主控（Flink / Spark / Ray）**两级架构之中：

| Fuxi 内核机制 | Apache YARN (2.x \(\rightarrow\) 3.x) | Kubernetes 批调度生态 | 计算引擎调度内核 (Flink / Spark / Ray) |
| :--- | :--- | :--- | :--- |
| **中心调度器 (`FuxiMaster`)** | `ResourceManager` (`CapacityScheduler` / `FairScheduler`) | `kube-scheduler` + `Volcano` / `YuniKorn` / `Kueue` | 依赖外部 RM，或由 **Ray GCS** + 节点 `Raylet` 协同 |
| **增量协商与动态调整** | 2.x 固定离散 `Container`；3.x 补齐 **Container Resizing** 与 **Opportunistic Containers** | Pod 长期不可变；**K8s 1.35 GA 的 KEP-1287** 实现容器原地垂直扩缩；**Koordinator** 做节点级超卖 | **Flink `DefaultDeclarativeSlotPool` + `AdaptiveScheduler`**；**Spark `ExecutorAllocationManager`**；**Ray `RequestWorkerLease`** |
| **Task 级局部恢复** | AM 内重调度容器；单 Container 内多 Task 仍会互相牵连 | `Volcano` 声明式 `RestartTask` / `RestartJob` 生命周期策略 | **Flink `RestartPipelinedRegionFailoverStrategy`**；**Spark `TaskSetManager` + Celeborn**；**Ray Lineage 重建** |
| **Master / AM 崩溃接管** | `WorkPreservingAMRestart` 保活容器进程，但应用 AM 难重建在途内存状态 | K8s Controller 重建 Pod，无内建应用态活体收编 | **Flink HA** 回滚至最近 Checkpoint；**Spark Driver** 崩溃通常触发在途 Stage 重算；**Ray Owner** 绑定局部任务 |

---

## 三、资源协商与动态调配：开源界如何打破“静态黑盒装箱”？

### 3.1 Apache YARN：从刚性 `ResourceRequest` 到 3.x 的艰难补课

早期 YARN 2.x 被批为“粗粒度静态分配”是有硬伤根源的。`ApplicationMaster` 通过 `AMRMClient` 周期性调用 `allocate(float progressIndicator)` 心跳向 `ResourceManager` 申请资源，所有配额都被离散化为集群配置的量子步长（`yarn.scheduler.minimum-allocation-mb` 与 `-vcores`）。一旦容器发放，生命周期内的内存与核数即被锁死；更麻烦的是，大数据引擎拿到 Container 后往往在里面拉起一个固定 `-Xmx` 堆大小的 JVM 进程，物理上也很难收缩。

**YARN 3.x** 实际上针对性补齐了非常类似 Fuxi 思想的两项能力：
- **Container Resizing**：允许 AM 通过 `AMRMClient` 发送动态扩缩容更新请求，由 RM 审批并通知 `NodeManager` 在不杀容器的前提下调大或调小资源配额；
- **Opportunistic Containers（机会型容器）**：在 `ContainerRequest` 中把执行类型区分为 `GUARANTEED` 与 `OPPORTUNISTIC`，允许在节点尚无稳定承诺配额时先将容器派发到 `NodeManager` 本地队列排队，榨干集群碎片算力。

但受制于两点现实约束，这两项特性在开源生产中始终曲高和寡：一方面，Java 引擎的 JVM Heap 在收缩时面临严重的 GC 与内存归还障碍；另一方面，由于 `AMRMClient.allocate()` 集心跳、资源申请、状态反馈与容器调整于一身，当 RM 发生主备切换时，在途的 Resize 或 ExecutionType 升级请求极易与客户端状态机错位，抛出 `InvalidApplicationMasterRequest` 异常并迫使 AM 丢弃追踪状态。

### 3.2 Kubernetes 批生态：用十年拆掉“Pod 不可变”的墙

相比 YARN Container，早期 **Kubernetes Pod** 的刚性甚至更强——Pod Spec 中的 `resources.requests/limits` 在调度绑定后属于不可变字段。在 2025–2026 年，云原生调度栈通过四个层次的合作，终于把动态调配拼图补全：

1. **KEP-1287（In-Place Pod Vertical Scaling）落地 GA**：历经多个大版本打磨，原位 Pod 垂直伸缩在 **Kubernetes v1.33 晋升 Beta**、在 **v1.35 正式进入 Stable（GA）**。通过新增 `/pods/<name>/resize` 子资源，并将实际生效配额反映在 `status.containerStatuses[*].resources` 中，`kubelet` 能够直接驱动底层 `containerd`/cgroup 在不重建 Pod 的情况下热调整 CPU 与内存。
2. **Volcano、YuniKorn 与 Kueue 的职责分工**：**Volcano** 用 `PodGroup` 解决了批/AI 作业的 Gang Scheduling（All-or-Nothing 防死锁）和队列抢占；**Apache YuniKorn** 将 YARN 式的层级弹性队列和公平性算法引入 K8s；**Kueue** 则把“队列准入控制（`ClusterQueue` / `Workload`）”剥离出 Pod 放置阶段，并在 KEP-1287 就绪后通过追踪容器实际资源状态变更实现配额实时回吐，免去因阶段性资源波动触发整组作业撕毁重建。
3. **Alibaba Koordinator 的节点混部实践**：面对在线微服务（Latency-Critical, LC）与离线批/AI 作业（Best-Effort, BE）的混部诉求，**Koordinator** 在节点侧部署 DaemonSet 代理 **`koordlet`**（角色高度对应 `FuxiAgent`），持续采样 CPI 与 PSI 等微架构干扰指标，将 LC 作业承诺但未实际消耗的资源动态超卖放大（Resource Amplification）给 BE 作业，一旦察觉延迟干扰立即在节点侧毫秒级收紧 BE 的 cgroup 配额。

### 3.3 计算引擎层的三种补偿哲学：Flink、Spark 与 Ray

在底层基础设施尚未完全做到细粒度弹性的漫长岁月里，上层计算引擎各自演进出了极具特色的资源协商模式：

- **Apache Flink（最接近 Fuxi 增量声明理念）**：早期 Flink 同样采用命令式槽位请求，槽位凑不齐就卡死。现代 Flink 转向了**声明式资源管理（Declarative Resource Management）**——`JobMaster` 内的 **`DefaultDeclarativeSlotPool`** 维护期望槽位池，通过 **`ResourceRequirements`** 持续向集群管理器申报当前执行图的理想并发与资源范围，并由 **`AdaptiveScheduler`** 根据实际到位的 Slot 数量动态伸缩作业并行度。这种“声明期望范围、按需异步增量收敛”的模式，在精神上最贴近 Fuxi 的增量协议。
- **Apache Spark（粗粒度 Executor 增减）**：Spark Driver 内的 **`ExecutorAllocationManager`** 盯紧 `TaskSetManager` 积压时间（`schedulerBacklogTimeout`）和空闲时间（`executorIdleTimeout`），以指数步长向 YARN/K8s 增减整颗 **Executor 容器**，并通过 Stage 级 **`ResourceProfile`** 允许不同阶段申请不同规格的隔离 Executor。它能解决作业级的潮汐伸缩，却无法在同一批进程内原地平滑变形。
- **Ray（微秒级去中心化租约 `RequestWorkerLease`）**：面向 AI 训练、强化学习与高频动态任务图，Ray 彻底跳出了“中心 Master 逐一装箱”的范式。每个节点上的 **`Raylet`** 兼具本地调度器与 Plasma 对象存储，Caller 直接向目标 `Raylet` 发送 **`RequestWorkerLease`** 请求（优先挑已持有上游数据对象的节点以换取局部性，本机无空余则 `Spillback` 溢出至其他节点），租约有效期内同一 Worker 进程可以零序列化开销连续吞吐大量微任务。

---

## 四、Failover 的真正分水岭：局部重启易得，无感“活体收编”难求

对照 `maxcompute-deep-dive.md` 原文最后一句——*“JobMaster 具备更强的 task-level failover 能力，无需 AM 重启”*——如果把这句拆开核验，会发现两层截然不同的工程现实。

### 4.1 节点/任务挂了：开源引擎同样无需重启 AM，但隔离粒度有别

在现代开源引擎中，单个 Worker 或 Task 崩溃早已不会导致 AM/Driver 重启，真正的差距在于**故障波及面与中间状态耦合度**：

- **Apache Spark 与 Remote Shuffle 的补课**：Spark 的 `TaskSetManager` 会在某个 Task 失败时自动重调度，Driver 稳如泰山。但传统 Spark 存在两个结构性耦合：其一，一个 Executor 容器内通常塞着多个并发 Task Slot，单个 Task 触发 JVM OOM 会连坐杀光同容器内的其余健康 Task；其二，Shuffle 中间文件落在计算节点本地盘，一旦 Executor 丢失就会触发上游 Stage 级联重算（Lineage Recomputation）。直到近年 **Apache Celeborn / Apache Uniffle** 将 Shuffle 彻底外置解耦，Spark 的任务级 Failover 才做到接近 MaxCompute/Fuxi 借助盘古或独立 Shuffle Service 的低恢复代价。
- **Apache Flink 的流水线区域恢复**：面对流计算长拓扑，Flink 通过 **`RestartPipelinedRegionFailoverStrategy`** 计算图内通过阻塞边（Blocking Edge）切开的最小流水线区域（Pipelined Region）。单一 Subtask 失败时，仅重启受影响的最小子图区域，无关分支继续稳态运行，同样无需惊动 `JobMaster` 进程。
- **Volcano 的策略表**：在 Kubernetes 控制面，Volcano 允许为作业声明精细的事件动作映射（例如遇到 `PodFailed` 时触发 **`RestartTask`** 而非 **`RestartJob`**），把原本只能靠应用层自己处理的局部恢复下沉到了编排器策略中。

### 4.2 主控（AM / JobMaster / Driver）挂了：开源为何很少敢做“活体收编”？

当故障直接砸在 **JobMaster / ApplicationMaster 自己头上**时，Fuxi 与开源界的技术分水岭才真正显现出来。

- **YARN `WorkPreservingAMRestart` 的“半程难题”**：YARN 其实从 2.6+ 起就支持工作保留式 AM 重启——当 AM 崩溃时，RM 不杀掉正在跑的底层 Container，并在新 AM 重新注册时把 `containersFromPreviousAttempt` 列表交还回去。问题在于，**YARN 保住了操作系统的进程壳，却保不住应用框架内存里的运行时图状态**。如果 Spark Driver 在 YARN 上崩溃，新启动的 Driver 拿回一堆活着却“失忆”的 Executor 进程，因为内存里丢失了当前 Stage 的 Partition 映射表、广播变量追踪与 Task Attempt 状态，往往仍不得不作废在途 Stage 重新来过（或依赖整作业级重试）。
- **Flink JobMaster HA 的语义优先取舍**：Flink 开启 Zookeeper / K8s ConfigMap 高可用后，新接管的 Standby JobMaster 会从**最近一次成功的 Checkpoint/Savepoint** 恢复状态图，并把所有 TaskManager 上的执行进度回滚重置到检查点屏障。对于追求严格 Exactly-Once 语义的无限流计算来说，回滚重置是数学上最干净、最安全的做法，只是为此放弃了在途任务的无损延续。
- **Ray 的去中心化代价**：Ray 把任务元数据与调度所有权（Ownership）下放给了发起调用的各个 `CoreWorker`，全局控制存储 GCS 仅维护 Actor 目录与 Placement Group 并支持重启而不打断运行中的 Worker 通信。但反过来，如果持有某批任务 Ownership 的 Driver/Caller 进程自身挂掉，它名下的任务子树依然会被连带清理——这是用单点透明接管换取极低调度延迟的刻意交换。

相比之下，Fuxi 之所以能把“JobMaster 崩溃后直接收编健康节点上仍在跑的 Task Instance”做进生产主干，归根结底是因为它**把调度与执行协议绑在了同一套受控生态之内**：`FuxiMaster` 只存不可变 Hard State、`FuxiAgent` 精准汇报当前存活的 `Instance` 语义（而不只是一个模糊的进程 PID）、`JobMaster` 围绕批处理确定性 DAG 做轻量任务级检查点——三者跨越了开源体系中“通用基础设施层不理解作业内部 Task 语义”的那道信息鸿沟。

---

## 五、给现代调度系统设计者的四条架构启示

回望 Fuxi 与开源调度栈十余年的演化合流，有四条经验在今天构建大规模 Lakehouse 与 AI 训练平台时依然极具指导意义：

1. **把控制面状态劈成 Hard State 与 Soft State**：不要把高频跳变的运行时调度映射塞进 Etcd 或 Raft 等强一致存储里，否则规模一上万台就会撞上共识写放大墙。静态配置与黑名单轻量落盘，瞬态分布由边缘 Agent 反向心跳实时拼回，是超大规模调度器兼顾高吞吐与秒级主备接管的必由之路。
2. **计算与 Shuffle/状态解耦，Failover 才有低成本**：无论是 MaxCompute 依托盘古/外部 Shuffle Service，还是 Spark 搭配 **Apache Celeborn**、Flink 依托分布式 Checkpoint，只要计算容器本地还背着不可替代的中间数据，任何增量协议和精细 Failover 都会在级联重算面前失效。
3. **基础设施原位弹性正在成熟，但应用运行时也要跟上**：Kubernetes **KEP-1287（v1.35 GA）** 终于让通用容器跨出了原地垂直扩缩的第一步，但上层运行时（尤其是自带静态内存池的 JVM 与早期 CUDA 显存分配器）如果不能安全释放与重分配内存，底层的 `resize` 原语就无法兑现为集群利用率，此时配合 **Koordinator** 式节点混部代理仍然是拉高算力水位的务实选择。
4. **接受分层调度，停止幻想单一全能调度器**：今天的生产环境早已不是单一的离线批处理，而是微秒级 AI Actor、分钟级迭代训练、秒级流式管道与百 TB 级大宽表 Shuffle 共存。最健康的模式是用 **Kueue / YuniKorn** 管住跨租户准入与公平配额，用 **Volcano** 守住 Gang 拓扑与硬件亲和，用 **Koordinator** 压实单机混部干扰，再把高频细粒度执行决策还给 **Flink `AdaptiveScheduler`** 或 **Raylet** 这样的引擎内调度内核。

---

> **参考资料与公开信息基线**
> - **Fuxi 与 MaxCompute 演进**：Zhuo Zhang et al., *Fuxi: a Fault-Tolerant Resource Management and Job Scheduling System at Internet Scale* (PVLDB 2014) · *Fangorn: Adaptive Execution Framework for Heterogeneous Workloads on Shared Clusters* (PVLDB 2021) · Chenghao Lyu et al., *Fine-Grained Modeling and Optimization for Intelligent Resource Configuration in Big Data Processing* (PVLDB 2022) · 本站 [maxcompute-deep-dive](https://github.com/LiShuMing/xlab/blob/main/docs/systems/maxcompute/maxcompute-deep-dive.md) 基础笔记。
> - **开源调度与计算内核官方文档及架构**：Apache Hadoop YARN (`AMRMClient` `allocate()` 协议、Container Resizing、Opportunistic Containers `GUARANTEED`/`OPPORTUNISTIC`、Work-Preserving AM Restart) · Kubernetes KEP-1287 (*In-Place Pod Vertical Scaling*, Beta in v1.33 / GA in v1.35) · Volcano (`PodGroup`、`RestartTask`/`RestartJob`) · Apache YuniKorn · Kueue (`ClusterQueue`、`Workload`) · Koordinator (`koordlet`、Resource Amplification) · Apache Flink (`DefaultDeclarativeSlotPool`、`ResourceRequirements`、`AdaptiveScheduler`、`RestartPipelinedRegionFailoverStrategy`) · Apache Spark (`TaskSetManager`、`ExecutorAllocationManager`、`ResourceProfile`) · Ray (`Raylet`、`RequestWorkerLease`、Ownership Model & GCS HA)。
