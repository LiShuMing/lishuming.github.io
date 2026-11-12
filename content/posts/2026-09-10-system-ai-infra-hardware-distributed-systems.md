---
title: "【调研】从芯片到数据中心：2026 AI Infra 与现代系统技术雷达"
date: 2026-09-10T00:00:00+08:00
lastmod: 2026-10-07T00:00:00+08:00
slug: "system-ai-infra-hardware-distributed-systems-2026"
categories:
  - 系统
tags:
  - AI Infra
  - 系统架构
  - CXL
  - GPU
  - 分布式系统
  - LLM Serving
  - 数据库
description: "从芯片算力、HBM 带宽、机架互连与供电散热的增速剪刀差出发，串联 2026 年先进封装、CXL 池化、集合通信编译、微秒级存储 I/O、LLM Serving 与异构数据库执行引擎的底层演进脉络。"
draft: false
toc: true
math: true
---

过去十年里，底层硬件演进拉开了一条越来越陡峭的物理剪刀差：**芯片张量算力（FLOPS）翻了几个数量级，片上高带宽显存（HBM）的容量与带宽增速远远落后，跨卡 Scale-up（NVLink/PCIe/CXL）与跨机 Scale-out（RDMA/Ethernet/IB）互连带宽再慢一截，而机架级供电与液冷散热更是撞上了硬性的物理天花板。**

拿今天最典型的一次混合专家模型（MoE）在线推理或分布式分析查询来说：单颗加速卡上的矩阵单元每毫秒能吞吐数万亿次低精度乘加，可在真实的墙钟时间（Wall-Clock Time）里，核心却常常在干等——自回归解码（Decode）阶段等 HBM 把历史 KV Cache 喂进寄存器；跨机专家路由（`All-to-All`）阶段等拥塞在网卡队列里的长尾分片；一旦显存水位告急触发换出（Spill），又要等慢上好几个数量级的 PCIe、CXL 或远端 NVMe-oF 把状态搬回来。

当“算得有多快”彻底让位于“数据能不能按时喂到嘴边”，过去那种“上层软件只管逻辑算法、把硬件当成均质黑盒”的分层抽象就失灵了。现代系统软件正在被迫重组成一张围绕**数据位置与搬运半径**协同调度的横纵网络：

```text
Application / Agent / SQL
          │
          ├───────────────┬──────────────────┐
          │               │                  │
     Query Engine     AI Runtime       Data Runtime
          └───────────────┴──────────────────┘
                          │
          Scheduler / Compiler / Memory Manager
                          │
        ┌─────────┬───────┼────────┬──────────┐
        │         │       │        │          │
       CPU       GPU    DPU/NIC   CXL       NVMe
        │         │       │        │          │
        └── PCIe / NVLink / Ethernet / IB ───┘
                          │
                   Rack / Cluster
```

站在 2026 年的时间节点回头看，无论是芯片侧的先进封装、内存侧的 CXL 分层池化、网络侧的拓扑感知集合通信，还是运行时侧的 Prefill/Decode 分离、动态存储轮询与异构数据库计划器，本质上都是在不同的物理半径上修补同一组**带宽与延迟比值**（Bandwidth-Latency Ratio）约束。下面我们就沿着“芯片与封装 → 内存层次 → 互连网络 → 存储与内核 → AI 与数据库运行时 → 技术评估闭环”这条主线，把这幅跨层技术拼图完整串起来。

---

## 一、物理瓶颈总账：从先进封装到机架的时延域与供数上限

现代数据中心里早就没有一块均质的“内存”和一张透明的“网络”，取而代之的是一层层嵌套的**时延域（Latency Domain）与带宽墙**。从片上数十 TB/s 的寄存器与 SRAM 往外走，每跨出一道封装或机箱边界——经过板载 `HBM3e`（约 3–8 TB/s）、机内 `NVLink` 与 `CXL` 内存池（百 GB/s 至 ~900 GB/s）、跨机 `RDMA RoCEv2 / InfiniBand`（约 50–100 GB/s），一直退到微秒级本地 `NVMe` 与远端解耦存储——供给带宽逐级陡降，负载也在 Roofline 模型上依次跨过算力受限（Compute-Bound）、显存带宽受限（Memory-Bound）与跨网受限（Network-Bound）拐点：

![从芯片片上 SRAM 到数据中心存储的带宽与时延层级](/images/diagrams/sys-ai-infra-bandwidth-hierarchy.svg)

同样一份中间状态（无论是数据库的 Hash Table 分区，还是大模型的 KV Cache 块），落在上面哪一级台阶，直接决定了算法在物理上能不能跑得通：

| 数据驻留位置 | 核心物理优势 | 核心物理代价 | 上层运行时必须感知的属性 |
| --- | --- | --- | --- |
| **CPU Cache（L1/L2/LLC）** | 纳秒级超低时延 | 容量极小、并发流极易冲刷 | 缓存行局部性、分支可预测性与硬件预取 |
| **Local DRAM** | 百 GB 至 TB 级通用容量 | 跨 Socket NUMA 惩罚与通道争用 | NUMA 归属、大页（Hugepage）放置与并发访存带宽 |
| **GPU HBM** | TB/s 级超高带宽、紧贴算力核 | 昂贵且单卡容量受限 | Batch 大小、权重精度与 Tensor / KV 生命周期 |
| **Peer GPU Memory** | 借高速互连扩展可用显存池 | 受限于跨卡拓扑跳数与集合争用 | NVLink / NVSwitch 物理路径与张量/专家并行切分 |
| **CXL Memory** | 突破单机内存插槽上限、可池化 | 时延约为本地 DRAM 的数倍 | 冷热页识别、跨主机共享边界与链路故障域 |
| **RDMA Remote Memory** | 跨机内存直通、绕过远端 CPU | 微秒级基线时延与网络拥塞抖动 | 路由轨道（Rail）、内存注册（Pinning）、流控与重试 |
| **Local NVMe** | TB 级低成本持久化容量 | 微秒级介质时延与内核栈固定开销 | 队列深度、轮询（Polling）与中断动态切换 |
| **NVMe-oF / 对象存储** | 存算彻底解耦、弹性扩缩容 | 尾延迟放大、API/链路争用 | 数据局部性缓存、攒批粒度与端到端反压（Backpressure） |

把这张层级表写成端到端执行账单，任何算子或推理步骤的开销都由六笔账共同构成（当计算与通信流水线重叠时，总耗时取决于未掩盖的关键路径）：

$$C_{\text{op}} = C_{\text{compute}} + C_{\text{move}} + C_{\text{queue}} + C_{\text{sync}} + C_{\text{spill}} + C_{\text{recovery}}$$

其中跨边界数据搬运项 \(C_{\text{move}}\) 绝不能用一个静态平均值糊弄过去，必须顺着真实经过的每一段物理链路 \(e\) 累加固定时延 \(L_e\) 与有效带宽耗时：

$$C_{\text{move}} = \sum_{e \in \text{path}} \left(L_e + \frac{\text{Bytes}_e}{\text{BW}_e \cdot \text{Util}_e}\right)$$

这两条式子解释了为什么近两年芯片架构的设计边界已经从“单颗裸片（Compute Die）”外扩到了**整机乃至整机架协同**。翻开 [Hot Chips 2026 官方议程](https://hc2026.hotchips.org/)——从 NVIDIA Vera CPU 与 Rubin GPU、AMD MI400 系列、Intel Diamond Rapids、Fujitsu MONAKA、Arm Chiplet 服务器 SoC，到专为 AI/HPC 定制的智能网卡——硬件厂商交出的答卷高度一致：单靠缩微晶体管已经喂不饱算力单元，现代加速器实质上是一个由 **`Compute Die + HBM 堆叠 + I/O Die + 硅中介层先进封装 + Scale-up 高速域 + 供电液冷包络`** 捆绑交付的机架级系统。

因此，当我们在架构评审中拿到新一代 CPU/GPU 的规格表时，千万别被发布PPT上的峰值算力（Peak FLOPS）迷惑；系统真正能兑现的有效吞吐受制于最短的那块木板：

```text
Useful Throughput = min(算子有效计算吞吐, HBM 供数带宽, 集合通信推进速率, 调度器组批效率, 机架供电与散热上限)
```

只有把下面七个问题挨个问透——支持哪些低精度格式与真实 Kernel 利用率？HBM 容量与带宽比能否装下目标上下文？Chiplet 封装是否切碎了访存一致性域？单机架高带宽 Scale-up 域能囊括多少张卡？跨机 Scale-out 需要几张网卡配比？每生成百万 Token 或跑完每条复杂查询烧多少焦耳电能？编译器与 Profiler 能否真正把硬件特性暴露出来？——纸面算力才算真正落到了工程地面上。

---

## 二、内存墙与 CXL 语义：当容量池化改变访存时延、线程调度与机架拓扑

算力暴涨撞上的第一堵墙就是内存容量与成本：GPU 上的 HBM 带宽极高但寸土寸金，而通用服务器里的本地 DDR5 DRAM 又常常因为整机插槽上限或机型配比僵化而出现“有的节点内存撑爆 OOM、隔壁节点空着一半内存用不上”的孤岛浪费。

Compute Express Link（CXL）正是为了打破这道单机容量围墙而生的。通过复用物理层构建 `CXL.io`、`CXL.cache` 与 `CXL.mem` 三套子协议，[CXL 3.0 规范](https://computeexpresslink.org/wp-content/uploads/2024/02/CXL-3.0-Specification.pdf)不仅支持单机挂载内存扩展卡，还进一步引入了跨交换机（Switch）的多机内存池化（Memory Pooling）与硬件一致性共享；[Linux CXL 驱动子系统](https://docs.kernel.org/driver-api/cxl/linux/cxl-driver.html)也已将 CXL Memory Device、Decoder、Region 与 DAX 设备纳入内核管理模型。

但如果上层系统把 CXL 内存简单当成“插上就能通过普通 `malloc` 无感使用的廉价本地 DRAM”，马上就会在生产环境吃大亏——因为 CXL 跨过了 PCIe 物理层与控制器队列，**它的随机访存延迟通常是本地 DRAM 的 3 倍甚至更高**，同时还引入了跨主机容量借用隔离以及交换链路断连后的爆炸半径问题。一套清醒的内存分配接口必须把底层物理契约显式暴露给运行时：

```text
allocate(size, latency_class, bandwidth_class, sharing_scope, durability, migration_policy)
```

围绕“怎样吃下 CXL 的容量红利、同时驯服它的高时延与交换机成本”，2026 年两篇系统顶会工作给出了极具启发性的破局方向：

第一条路是从**微秒/纳秒级线程调度**入手掩盖访存停顿。[LiteSwitch（OSDI 2026）](https://www.usenix.org/conference/osdi26/presentation/li-nanqinqin)直面“CXL 访存延迟高达本地内存 3 倍以上”的现实：既然单线程硬等缓存行返回会让 CPU 流水线白白空转几百纳秒，不如通过软硬件协同实时识别由 CXL 远端访存触发的停顿（Stall），在 **20 纳秒以内**极速切换到同核另一个就绪线程去干活！实验表明，当单核拥有充足的可运行并发任务时，这套机制最多能挽回 **80%** 因 CXL 额外延迟损失的吞吐。这刷新了我们对操作系统和协程调度器的认知：未来的调度器不能只懂“时间片用完”或“I/O 阻塞”，还要能看懂“**当前线程正卡在哪一层内存墙上**”（当然，如果业务本身缺乏并发就绪任务、频繁切换冲刷了 L1 Cache，或者链路带宽本身已经打满，光靠切线程就救不回来了）。

第二条路是从**机架物理连线拓扑**入手省掉昂贵的全互连交换机。[Octopus（NSDI 2026）](https://www.usenix.org/conference/nsdi26/presentation/zhong)挑战了“做 CXL 内存池就必须买昂贵的高端口 CXL Switch 把所有服务器全连接起来”的默认假设：它改用低端口数的 CXL 内存设备直接跨服务器连成稀疏拓扑（Sparse Topology），把紧耦合节点聚合成低时延通信岛（Island），在池化共享率与直连低延迟之间取最优折中。在其三节点硬件原型与扩展至 96 台服务器的模拟评估中，基于该拓扑的机架内 RPC 比同机架 RDMA 快 **3.2 倍**、比经过 CXL Switch 转发快 **2.4 倍**，并测算出 **3%–5.4%** 的整机群净成本节省。这揭示了一条深刻的架构规律：**一旦内存变成了跨节点的网络化资源，“哪台机器和哪块内存池直连、跨岛要跳几跳”就不再只是硬件布线图，而成了运行时放置状态与迁移计算的核心输入。**

把 HBM、本地 DRAM、CXL 与 NVMe 摆在一起，现代数据库 Buffer Manager 与大模型 KV Cache Manager 其实正在收敛为同一套**热度与复用驱动的状态分层状态机**：

| 核心状态对象 | 契合的物理层级 | 背后的带宽与时延权衡机制 |
| --- | --- | --- |
| **高频探测 Hash 表 / 活跃 Attention 热块** | GPU HBM 或 Local DRAM | 随机与高并发访存极度敏感，3 倍延迟惩罚会直接击穿关键路径 |
| **温冷数据库分区 / 低频二级索引页** | CXL Memory | 容量需求大、命中频次低，省下的本地 DRAM 可让位给热算子 |
| **长会话历史 KV Cache 温冷块** | Host DRAM 或 CXL Memory | 随多轮对话或 Agent 思考步骤呈阶段性唤醒，可提前异步预取回 HBM |
| **训练 Checkpoint / 不可变列存段（Parquet/SST）** | Local NVMe / NVMe-oF | 顺序大块吞吐与断电持久性优先，无需占用字节寻址内存总线 |
| **跨机共享协调元数据 / 分布式锁表** | CXL 共享岛或 RDMA 内存 | 视共享主机跨度与硬件一致性开销在直连 CXL 与 RDMA 间权衡 |

---

## 三、互连墙与集合通信编译：从静态带宽公式到倾斜与拓扑协同

跨出单卡和单机内存池，系统立刻撞上第二堵墙：**多机多卡之间的互连带宽落差与流量倾斜**。

过去写分布式数据系统成本模型时，大家习惯把网络传输开销写成一条极简线性公式：

$$C_{\text{network}} = \text{latency} + \frac{\text{bytes}}{\text{bandwidth}}$$

在今天的 AI 训练/推理集群与大规模并行分析引擎里，这条静态公式几乎处处失真。原因在于现代集群是典型的**异构分层拓扑**：机内 8 张（或机架内数十张）GPU 之间走数百 GB/s 的 NVLink / NVSwitch 全互连高速域，一旦跨出节点边界进入 RoCE 或 InfiniBand 网络，单卡配属的 NIC 出口带宽往往骤降一个数量级；更致命的是，叠加在两级拓扑之上的流量绝非均匀广播——在 MoE 模型的专家分发（Dispatch）与合并（Combine）阶段，或者分布式数据库的倾斜大表 `Shuffle / Exchange` 阶段，动态路由会让少数热门专家或热点分区的网卡队列瞬间遭遇多对一打满（Incast），引发等价多路径路由（ECMP）哈希碰撞甚至无损网络优先流控（PFC）反压蔓延，让几百张算完的 GPU 集体停下来干等最慢的那条链路。

因此，集合通信（Collective Communication）已经没法再靠几套写死的 Ring 或 Tree 静态模板通吃天下，而是正在演变成一门**结合实时消息矩阵与物理拓扑的“通信编译”技术**。

[FAST（NSDI 2026）](https://www.usenix.org/conference/nsdi26/technical-sessions) 在 H200 与 MI300X 集群上针对 MoE `All-to-All(v)` 通信的优化就是绝佳例证：面对严重的动态 Token 路由倾斜、两级网络带宽差与跨机 Incast，它没有让各个 GPU 盲目直发跨机网卡，而是**先利用机内充裕的高速互连做一轮本地再平衡（Intra-server Rebalancing），把跨机出口流量削峰填谷整理成均衡的一对一传输调度**，从而把跨机长尾等待大幅压平。你会发现，这和分布式数据库执行引擎在线重平衡倾斜 `Exchange` 分区的思路如出一辙——区别仅仅是搬运的包裹名叫 Token 还是 Tuple Batch。

放到千卡级分布式训练中，经典的 3D 并行切分与故障恢复同样严格受制于这两级物理拓扑：逐层高频同步的张量并行（TP `AllReduce`）必须收敛在机内 NVLink 高速岛内，跨节点链路仅承载流水线并行（PP）微批次边界的点对点传递，以及能与反向计算重叠的数据并行/分片优化器（DP / ZeRO / FSDP `ReduceScatter` 与 `AllGather`）；而在容错侧，为了避免单卡掉线迫使全集群停摆等待冷存储读取，训练快照先以毫秒级异步拷贝落入主机 DRAM/CXL 再后台刷盘，故障接管时直接从存活对等节点的内存回灌状态：

![分布式 3D 并行通信拓扑映射与异步内存容错恢复](/images/diagrams/sys-distributed-parallelism-faults.svg)

这也决定了工程团队在做集群网络选型（InfiniBand、RoCEv2 还是新一代增强型 AI 以太网）时，绝不能只拿交换机端口线速比大小，而必须把整条数据路径放进下面七个维度联合拷问：

| 评估维度 | 必须用目标负载实测回答的工程问题 |
| --- | --- |
| **端到端时延分布** | 几十字节的控制信令、几十 KB 的稀疏专家包与上百 MB 的全量同步各自延迟如何？ |
| **多流并发有效吞吐** | 在真实 `AllReduce / All-to-All` 并发冲击下，有效带宽利用率（Goodput）能剩多少？ |
| **拥塞控制与抗倾斜** | 遭遇突发多对一 Incast、热点路由与长短流混跑时，尾部完成时间会不会雪崩？ |
| **无损依赖与故障爆炸半径** | 若依赖 PFC / ECN 无损配置，单张故障网卡死锁或参数失配会不会引发全网头阻塞（HoL Blocking）？ |
| **轨道（Rail）与收敛比** | GPU 与 NIC 比例如何配对？叶脊网络（Leaf-Spine）超额订阅比（Oversubscription）如何影响跨轨任务？ |
| **运维可观测性** | 出现微突发丢包或链路降速时，能否在秒级定位到具体队列、光模块与受影响的通信组？ |
| **上层生态耦合度** | 主流集合通信库（NCCL/RCCL）、RDMA 动词接口与分布式存储栈能否开箱跑满？ |

---

## 四、存储供数与 Linux 内核落地：当微秒级介质撞上软件栈与网络流控

沿着数据供给链再往外退一步到持久化存储与操作系统层，同样的剧本再次上演：**当底层 NVMe SSD 乃至 CXL SSD 把硬件介质访问延迟压进个位数微秒后，以前藏在水面下的 Linux 内核软件栈开销与存储网络争用，突然浮出水面成了大头。**

先看单机内核 I/O 完成路径的两难困局：

```text
传统中断模式（Interrupt）：提交请求 → 线程休眠让出 CPU → 硬件完成后发硬中断 → 唤醒调度 → 完成处理
纯忙轮询模式（Polling）  ：提交请求 → 线程独占 CPU 死循环轮询完成队列 → 零唤醒延迟立刻返回
混合自适应模式（Hybrid） ：提交请求 → 先短暂休眠或短轮询 → 结合负载压力动态切换中断与轮询
```

用中断省了 CPU，但一次睡眠唤醒加中断上下文切换动辄花掉好几微秒，把超快 SSD 的低延迟优势吃掉大半；改成纯忙轮询压低了尾延迟，一旦机器 CPU 利用率升高，轮询线程又会跟前台计算任务疯抢核心。[UnICom（FAST 2026）](https://www.usenix.org/conference/fast26/presentation/pan) 的实测一针见血地指出，在高性能 SSD 与 CXL SSD 场景下，内核 I/O 软件栈开销最高能占到总延迟的 **50% 左右**！为此，论文提出了 `TagSched`、`TagPoll` 与 `SKIP` 机制，把“用中断还是用轮询完成 I/O”从僵化的启动参数变成了感知实时 CPU 压力、队列深度与延迟目标（SLO）的动态调度策略。放到数据库与 AI 数据加载里道理完全相通：前台在线点查值得用短暂轮询保 P99，后台大批量的 LSM Compaction 或 Checkpoint 刷盘则理应退回低 CPU 侵占的中断批处理。

一旦存储跨过网络做成存算分离的 **NVMe-oF（NVMe over Fabrics）**，存储队列就跟网络流控彻底绑在了一起。[Co-Designing Traffic Control with NVMe-oF（NSDI 2026）](https://www.usenix.org/conference/nsdi26/presentation/wang-chendong) 对比了有交换机（Switched SAN）与无交换机直连（Switchless SAN）架构，将解耦存储的流量控制拆解为**多路径选择、带宽分配与队列调度**三件事联合设计，证明了即便省掉昂贵的高端口存储交换机，只要配合负载感知的多路径 I/O 调度，同样能消解读写干扰、拿到媲美交换网的吞吐与更低的延迟。

说到底，任何新硬件特性要走出实验室变成工程师手里可控的系统能力，最终都得穿过 **Linux 内核与系统软件层** 的三道关卡：

1. **异构内存拓扑与热度感知**：紧盯 [Linux CXL 驱动框架](https://docs.kernel.org/driver-api/cxl/)与数据访问监控工具 [DAMON](https://docs.kernel.org/mm/damon/) 的演进——重点看内核暴露了哪些物理距离（Locality）信息，页冷热晋升/降级（Page Promotion/Demotion）、大页拆分以及设备显存与主机内存的一致性锁页（Pinning）到底由内核自动兜底还是交由用户态接管。
2. **多租户隔离与压力反压信号**：在混部环境下，除了 `cgroup v2` 对 CPU、内存与块 I/O 的配额限制，更要看压力失速信息（PSI，Pressure Stall Information）能否实时喂给上层服务的准入控制器（Admission Control），以及 GPU、RDMA 网卡与 CXL 通道何时能补齐与 CPU 对等的细粒度隔离原语。
3. **内核旁路（Kernel Bypass）的全生命周期总账**：评估 `io_uring`、XDP/DPDK、RDMA 或 GPUDirect Storage 时，绝不能只跑一个热循环微基准测试报喜；内存提前注册与锁页耗时、容器安全边界、网卡重置时的异常回退慢路径以及进程崩溃后的物理资源回收，才是决定生产可用性的隐性大账。

---

## 五、LLM Serving：受限于显存与带宽比的“状态密集型执行引擎”

如果把前面四节讲的算力/显存剪刀差、多级内存墙、跨机互连倾斜与内核调度放在同一个放大镜下观察，**2026 年最典型的试验场莫过于大模型在线推理引擎（LLM Serving）**。

很多人最初以为推理服务无非是“包一层 HTTP 接口调一次 GPU 矩阵乘”；真正上线抗高并发流量后才发现，一次请求要走完准入控制、分词、前缀缓存匹配、计算密集的预填充（Prefill）、KV 显存块分配、受限于显存带宽的逐词自回归解码（Decode）、采样流式返回乃至超限抢占换出——**它从头到尾就是一台受严格尾延迟 SLO（首字延迟 TTFT 与字间延迟 ITL）约束的状态密集型数据库执行引擎**：

| LLM Serving 推理运行时 | 现代数据库执行引擎 | 底层同构的系统调度难题 |
| --- | --- | --- |
| 在线推理请求（Request） | SQL 查询（Query） | 准入控制（Admission Control）、多租户配额与超时取消 |
| 动态 Token 组批（Batch） | 向量化批处理（VectorBatch） | 攒大批量提硬件利用率 vs 控制排队等待保延迟 |
| **KV Cache 显存池** | **Buffer Pool / 算子状态存储** | 容量封顶下的块映射、命中复用、冷热淘汰与跨层换出 |
| 共享前缀缓存（Prefix Cache） | 物化视图 / 公共子表达式缓存 | 跨请求等价匹配、命中省算力收益 vs 占用宝贵缓存配额 |
| **Prefill（预填充阶段）** | 全表扫描 / Hash Build / JIT 编译 | 算力密集（Compute-Bound），高并行但易阻塞短任务 |
| **Decode（自回归解码阶段）** | 依赖前序状态的流式迭代算子 | **显存带宽受限（Memory-Bandwidth-Bound）**，输出长度事先未知 |
| Continuous Batching | Morsel-Driven 动态任务窃取 | 消除短请求干等长请求的填充气泡，让计算单元持续满载 |
| 张量 / 流水线 / 专家并行 | 分布式物理执行计划（MPP Plan） | 算子切分收益 vs 跨卡/跨机集合通信与倾斜同步代价 |
| 请求抢占换出（Preemption） | 大查询挂起落盘（Spill / Suspend） | 显存撑爆时如何以最低代价保全已算出的中间进度 |
| SLO 约束下的有效吞吐（Goodput） | P99 延迟红线下的最大 QPS | 违背 TTFT/ITL 红线的废吞吐毫无商业价值 |

顺着“HBM 容量太贵、Decode 阶段显存带宽卡脖子”这条第一性原理往下推，就能看懂推理架构连迈的三大步：

第一步是**把连续显存切碎做虚拟化寻址**。由于每个请求最终会吐出多少个 Token 事先根本不知道，早期按最大上下文长度提前预留连续显存的做法，会造成严重的内部留白浪费与外部碎片。vLLM 提出的 [PagedAttention](https://arxiv.org/abs/2309.06180)（及[项目架构解析](https://vllm-project.github.io/2023/06/20/vllm.html)）直接借用了操作系统页表的设计，将每个请求逻辑上连续的 KV 序列切成固定大小的块（Block），散落映射到任意空闲的物理显存块上：

```text
请求 A 的逻辑 KV 块：  [A0] [A1] [A2] [A3]
                        │    │    │    │
                        ▼    ▼    ▼    ▼
GPU 物理显存块池：     [A2] [B0] [A0] [空闲] [A3] [A1]
```

消灭连续预留碎片只是第一步；一旦有了块级页表映射，多轮对话的公共系统提示词、Agent 反复读取的代码库上下文就能自然通过写时复制（Copy-on-Write）零拷贝共享物理块。随着多轮长上下文与 Agent 工作负载普及，单卡 HBM 依然装不下海量历史会话，KV Cache 自然外溢成了一条横跨六层的存储瀑布：**`GPU HBM → 同机/同轨对等 GPU 显存 → Host DRAM → CXL 内存池 → 本地 NVMe → 远端分布式 KV / 对象存储`**。此时淘汰策略再也不能靠简单的 LRU 拍脑袋，因为每一层都算着一笔三方比价账：**把旧 KV 块从远端搬回 HBM 的耗时，跟干脆重新跑一遍 Prefill 重算出来的耗时相比，到底哪个更便宜？搬运过程能不能跟上一层的矩阵计算完美重叠（Overlap）？**

第二步是**拆开性格迥异的 Prefill 与 Decode 阶段（P/D 分离）**。既然长 Prompt 的 Prefill 一下子吃满算力核、会直接卡住同卡正在平稳吐字的几十个 Decode 请求（打爆字间延迟 ITL），最自然的架构演进就是把两者拆到独立的计算池里各自扩缩容、各自挑选最优并行度。如 [NVIDIA Dynamo 解耦推理架构文档](https://docs.nvidia.com/dynamo/dev/kubernetes/disaggregated-serving/overview)所示，Prefill 节点算完首轮后，通过零拷贝高速传输层（机内 NVLink/CUDA IPC，跨机 RDMA）将生成的 KV Cache 推入 Decode 节点的 HBM 块表，并与下层 `Host DRAM / CXL → 本地 NVMe → 远端共享 KV 池` 的全局前缀命中与冷热换出连成一体：

![Prefill / Decode 解耦推理与多级 KV Cache 状态瀑布](/images/diagrams/sys-pd-disaggregation-kvcache.svg)

注意，P/D 分离绝不是无脑开启的免费午餐，它受制于一条硬核的收支不等式：

$$\text{Benefit}_{\text{阶段解耦消除干扰与异构扩缩容}} > \text{Cost}_{\text{KV 跨网传输}} + \text{Cost}_{\text{二级路由开销}} + \text{Cost}_{\text{双池负载失衡空转}}$$

在短 Prompt、小模型、低并发或者缺乏 RDMA 高速互连的环境里，搬运整坨 KV Cache 的网络耗时和双池木桶失衡代价很容易反超解耦收益，老老实实单池混跑（Aggregated Serving）反而更快更省。

第三步是**更细粒度的运行时抢占、冷启动供数与跨微调模型状态共享**。2026 年 NSDI 上的三项代表工作恰好分别攻克了这三个死角：

- **[FastServe（NSDI 2026）](https://www.usenix.org/conference/nsdi26/presentation/wu-bingyang)** 解决了长请求霸占队列导致短请求排死的问题：它将抢占粒度下沉到单步迭代（Token 级），采用跳级多级反馈队列（Skip-Join MLFQ），并主动把挂起请求的 KV 状态在 GPU 与主机内存之间异步流水换出/换入，用计算掩盖 PCIe 搬运，在论文测试负载中拿到最高 **6.1 倍**的有效吞吐提升。
- **[HydraServe（NSDI 2026）](https://www.usenix.org/conference/nsdi26/presentation/lou)** 瞄准了 Serverless 大模型实例拉起时的“供数风暴”：通过提前将模型权重分片分布到多台服务器、流水重叠 Worker 内部初始化阶段，并做网络感知的 GPU 放置避免多实例同抢一张网卡带宽，把冷启动耗时压低了 **1.7–4.7 倍**。
- **[DroidSpeak（NSDI 2026）](https://www.usenix.org/conference/nsdi26/presentation/liu-yuhan)** 则探索了同底座微调（Fine-tuned）模型变体之间的 KV 复用：当多个专精 Agent 模型共享同一套基础架构与上下文时，无需各自从头重算全部 KV，只需选择性重算敏感的少数几层、其余层直接跨模型复用并流水化加载，在保持生成质量的同时实现了最高 **4 倍**吞吐与约 **3.1 倍**的 Prefill 加速。

---

## 六、反哺数据库执行引擎：驻留边界、非线性硬约束与状态汇合

看完 AI Infra 如何被硬件边界倒逼重构，再回头审视我们熟悉的分析型与事务型数据库内核，就会发现传统优化器的假设同样到了必须刷新升级的时刻。

在之前的[《Hash 还是 Sort：从优化器决策到运行时自适应》]({{< relref "2026-09-10-dive-hash-sort-strategy.md" >}})一文中，我们剖析了单机与常规分布式环境下如何权衡数据有序性与分区属性；而一旦数据库引入异构硬件（如 GPU 协处理、CXL 扩展内存、NVMe-oF 存算分离），查询优化器维护的**物理属性**（Physical Property）就必须从单纯的分布与排序，扩容为包含设备驻留和故障边界的多维向量：

```text
PhysicalProperty {
  distribution,      // 哈希分片 / 范围分片 / 广播
  ordering,          // 局部或全局排序键
  device,            // CPU / GPU / SmartNIC
  memory_tier,       // HBM / Local DRAM / CXL / NVMe
  data_residency,    // 编码格式（字典/Selection Vector）是否已就绪驻留于目标设备
  network_domain,    // 同 NUMA / 同 NVLink 岛 / 跨机架 RDMA
  fault_domain,      // 池化设备或远端节点失效时的重算边界
  latency_slo        // 在线交互查询 vs 后台批处理报表
}
```

举个最直观的翻车现场：单测一个 GPU Hash Join 算子，内核跑得比 CPU 快十倍；可放到真实复杂 SQL 里，上游字符串正则过滤不支持 GPU，下游自定义窗口聚合也不支持 GPU——结果数据先走 PCIe 拷进显存建表探测，算完立刻又走 PCIe 拷回主存并重新解码字典列，来回两趟总线搬运和 Kernel 启动开销把算子省下的几毫秒吃得干干净净，端到端耗时甚至不如留在 CPU 本地跑一套紧凑的向量化 Radix Join！因此，异构加速真正的胜负手从来不是给单个算子贴上 `GPU=true` 标签，而是**尽可能拉长数据在同一高带宽域内连续驻留（Pipeline Residency）的算子链条**。

与此同时，优化器沿用了几十年的**线性加权标量代价模型**（\(Cost = w_c C_{\text{cpu}} + w_i C_{\text{io}} + w_n C_{\text{network}}\)）在现代硬件的“悬崖效应”面前也越来越力不从心：HBM 差 10 MB 没装下触发 Spill、网卡利用率冲过拐点引发排队雪崩、或者首包响应跨过 SLO 红线，代价都不是平滑线性增加一点分数，而是执行模式直接跳变。更贴合现代系统的规划范式，是**先用硬容量与安全带宽卡死可行域，再在可行计划里做延迟、成本与能耗的多目标寻优**：

$$
\begin{aligned}
\min\quad & (\text{Latency}_{p99},\ \text{Cost},\ \text{Energy}) \\
\text{s.t.}\quad
& \text{HBM}(\text{plan}) \le \text{HBM}_{\text{budget}} \\
& \text{BW}_e(\text{plan}) \le \text{BW}_e^{\text{safe}},\quad \forall e \in \text{path} \\
& \text{TTFT} \le \text{SLO}_{\text{ttft}},\quad \text{ITL} \le \text{SLO}_{\text{itl}} \\
& \text{FaultDomain}(\text{plan}) \le \text{RiskBudget}
\end{aligned}
$$

翻看 [VLDB 2026 官方获奖论文名单](https://vldb.org/2026/conference-awards.html)，数据库与 AI 系统双向奔赴的趋势已经跃然纸上：最佳工业界论文 `OmniTable` 聚焦 PB 级大模型预训练数据的治理与探索底座；最佳研究论文荣誉提名同时涵盖了“分析型数据库标量函数的 GPU 加速”与深入闪存物理写特性的 *How to Write to SSDs*；`FastCompose` 则直击编译型查询引擎的 JIT 编译冷启动痛点。数据库沉淀了几十年的代价估算、内存准入、落盘降级与事务恢复正在源源不断输入给 AI Runtime；而 AI Infra 在显存块虚拟化、跨层异步预取和拓扑感知通信上的实战经验，也在倒逼下一代数据库执行引擎重塑底层架构。

---

## 七、2026 技术评估雷达、十二问检查单与证据闭环

面对铺天盖地的顶会新名词与硬件发布会参数，工程团队最怕两件事：要么盲目追新把尚未成熟的研究原型塞进核心生产链路，要么固守旧经验对已经发生移动的硬件边界视而不见。结合当前软硬件生态成熟度与落地前提，我们可以把文中梳理的十二个关键方向归入四级条件化行动雷达（`Adopt / Trial / Assess / Watch`）：

| 评估象限 | 技术方向 | 2026 工程成熟度判断 | 引入前必须实测验证的前提问题 |
| --- | --- | --- | --- |
| **Adopt（优先采用）** | HBM 容量感知组批与显存预算治理 | 已成现代推理与异构计算底座标配 | 碎片率、突发长序列 OOM 与尾延迟 SLO 是否已纳入统一准入闭环？ |
| **Adopt（优先采用）** | 拓扑感知的集合通信调度 | 多机多卡且存在跨层带宽差时收益确定 | 实际通信矩阵是否与机内高速域 / 跨机多网卡轨道（Rail）严格对齐？ |
| **Adopt（优先采用）** | NVMe 异步 I/O 与多级热冷缓存 | 云原生数据库与存储引擎成熟主力路径 | 在压低 P99 尾延迟的同时，内核提交/完成路径的 CPU 占比是否可控？ |
| **Trial（小规模试点）** | Prefill / Decode 分离部署 | 在长上下文、高并发在线推理场景潜力突出 | 跨网搬运 KV Cache 的延迟与双池木桶失衡成本，是否小于解耦收益？ |
| **Trial（小规模试点）** | 前缀缓存（Prefix Cache）与亲和路由 | 多轮对话、RAG 与 Agent 工具循环命中率高 | 带状态路由引入的负载倾斜、多租户缓存隔离与失效一致性如何兜底？ |
| **Trial（小规模试点）** | GPU 驻留型数据库分析算子链 | 契合高算术强度且能连续驻留显存的查询子图 | PCIe 往返搬运、冷启动编译与未覆盖算子回退 CPU 的开销能否摊平？ |
| **Assess（原型评估）** | CXL 本机内存冷热分层（Tiering） | 硬件与 Linux 内核支持快速成型 | 冷热页识别准确度、后台页迁移带宽开销与尾延迟毛刺是否可控？ |
| **Assess（原型评估）** | CXL 跨节点内存池化（Pooling） | 消除单机内存孤岛、提升机架利用率潜力显著 | 交换机/稀疏拓扑硬件成本、跨机故障隔离爆炸半径与恢复协议如何闭合？ |
| **Assess（原型评估）** | DPU / SmartNIC 数据面卸载 | 虚拟网络、存储转发与安全加密持续下沉 | 卸载进固件/硬件后，线上排障可观测性、热升级与研发调试成本如何？ |
| **Assess（原型评估）** | 无交换机多路径解耦存储（Switchless SAN） | 兼顾省除高端存储交换机与多路径低延迟 | 机架物理布线复杂度、多跳路由算法与节点扩缩容运维难度如何？ |
| **Watch（长期跟踪）** | 光互连（Optical Interconnect / CPO） | 有望重构机架间高带宽 Scale-up 物理半径 | 光电转换功耗、模块失效率（MTBF）、成本曲线与大规模量产节奏如何？ |
| **Watch（长期跟踪）** | 存内计算 / 近数据处理（PIM / Near-Data） | 从物理根源削减跨总线字节搬运 | 编程模型碎片化、地址一致性、低精度局限与多租户复用难题如何解？ |

以后无论在架构评审会上评估上述哪一项技术，只要拿出下面这份**十二问检查单**逐条过堂，就能迅速剥离宣传水分，算清真实的系统总账：

1. 当前工作集到底卡在**算力、容量、带宽，还是单次往返时延**上？
2. 输入数据此刻驻留在哪一层，算完之后下一步又要送去哪里？
3. 整条链路上最昂贵的一次跨层搬运是什么？能不能提前过滤掉，或者与计算流水重叠？
4. 宣传的峰值吞吐绑死了什么数据精度、多大的 Batch Size 和怎样的并行度？
5. P50 平均耗时变好看，是不是拿恶化 P99 尾延迟、牺牲有效吞吐（Goodput）或烧满额外 CPU 换来的？
6. 一旦遇到连接键倾斜、MoE 专家热点或长短请求混跑，静态计划会不会瞬间退化？
7. 中间状态能不能低成本迁移、跨请求共享或干脆丢弃重算？各自代价几何？
8. 该方案是不是暗中假设“单核永远有足够多的并发就绪任务来掩盖访存停顿”？
9. 当底层物理链路拥塞、降速或交换机局部故障时，系统能否在线感知并动态绕行？
10. 多租户配额、权限校验与安全隔离放进快路径后，吃掉了多少性能红利？
11. 把局部算子加速 10 倍之后，按阿姆达尔定律折算回端到端全链路还剩百分之几的收益？
12. 快路径一旦踩空（显存满、硬件表满、远端断连），有没有经过实战演练的降级回退与恢复慢路径？

想要长期维护这样一套不被营销口号带偏的技术判断力，关键在于区分不同一手材料的证据边界，并形成固定的验证闭环：

| 证据层级 | 代表性信息源 | 最适合用来回答什么问题 | 阅读时需警惕的局限 |
| --- | --- | --- | --- |
| **硬件规范与内核/项目源码** | CXL 规范、Linux 文档、vLLM / Dynamo 源码 | 硬件与系统接口到底承诺了什么精确语义 | 细节浩瀚，缺乏跨方案横向性价比视角 |
| **系统与数据库顶会论文** | OSDI、NSDI、FAST、MLSys、VLDB、SIGMOD | 瓶颈成因拆解、新机制设计与受控实验边界 | 原型简化假设较多，未必覆盖全量生产长尾 |
| **一线大规模工程博客** | Cloudflare、主流云厂商与数据库内核团队博客 | 真实规模下的诡异故障、运维踩坑与折中取舍 | 高度绑定特定业务形态与历史基础设施包袱 |
| **微架构实测与整机拆解** | Chips and Cheese、ServeTheHome | 真实的缓存/TLB/互连微基准表现与服务器物理形态 | 结论强依赖微基准构造方式 |
| **半导体与供应链深度分析** | SemiAnalysis、The Next Platform | 晶圆制程、先进封装产能、机架供电与商业成本账 | 需自行剥离市场预测与确定性工程事实 |

与其每天被碎片资讯淹没，不如围绕具体瓶颈问题（例如“为什么同一张 Hash 表换代后性能迥异”“为什么开了 P/D 分离 TTFT 反而变慢”）建立“**日常筛选信号 → 每周精读一手源码/论文 → 提炼成本公式与失效边界 → 每季度用自有负载跑一次最小复现实验**”的闭环。毕竟，再漂亮的雷达图也替代不了在自己的机器上亲手量出的那条延迟与带宽曲线。

### 延伸资料与长期追踪索引

- **规范、内核与开源运行时**：[CXL 3.0 Specification](https://computeexpresslink.org/wp-content/uploads/2024/02/CXL-3.0-Specification.pdf) · [Linux Kernel CXL Documentation](https://docs.kernel.org/driver-api/cxl/) · [Linux DAMON Documentation](https://docs.kernel.org/mm/damon/) · [vLLM & PagedAttention Introduction](https://vllm-project.github.io/2023/06/20/vllm.html) · [NVIDIA Dynamo Disaggregated Serving](https://docs.nvidia.com/dynamo/dev/kubernetes/disaggregated-serving/overview)
- **核心论文与顶会议程**：[PagedAttention (SOSP 2023 / arXiv)](https://arxiv.org/abs/2309.06180) · [Hot Chips 2026 Program](https://hc2026.hotchips.org/) · [LiteSwitch (OSDI 2026)](https://www.usenix.org/conference/osdi26/presentation/li-nanqinqin) · [Octopus (NSDI 2026)](https://www.usenix.org/conference/nsdi26/presentation/zhong) · [NSDI 2026 Technical Sessions (FAST MoE Collective)](https://www.usenix.org/conference/nsdi26/technical-sessions) · [FastServe (NSDI 2026)](https://www.usenix.org/conference/nsdi26/presentation/wu-bingyang) · [HydraServe (NSDI 2026)](https://www.usenix.org/conference/nsdi26/presentation/lou) · [DroidSpeak (NSDI 2026)](https://www.usenix.org/conference/nsdi26/presentation/liu-yuhan) · [UnICom (FAST 2026)](https://www.usenix.org/conference/fast26/presentation/pan) · [Co-Designing Traffic Control with NVMe-oF (NSDI 2026)](https://www.usenix.org/conference/nsdi26/presentation/wang-chendong) · [VLDB 2026 Conference Awards](https://vldb.org/2026/conference-awards.html)
- **长期技术追踪站点**：[Chips and Cheese](https://chipsandcheese.com/) · [ServeTheHome](https://www.servethehome.com/) · [SemiAnalysis](https://semianalysis.com/) · [The Next Platform](https://www.nextplatform.com/) · [Cloudflare Blog](https://blog.cloudflare.com/) · [LWN.net](https://lwn.net/) · [USENIX Conferences](https://www.usenix.org/conferences) · [MLSys](https://mlsys.org/) · [PVLDB](https://www.vldb.org/pvldb/) · [ACM SIGMOD](https://sigmod.org/) · [arXiv](https://arxiv.org/)

---

> **文献与检索基线说明**：本文初稿整理于 2026-09-10，并于 2026-10-07 完成增补核对，检索范围覆盖 Hot Chips 2026、OSDI 2026、NSDI 2026、FAST 2026、VLDB 2026 官方议程与获奖目录，以及 CXL 3.0、Linux Kernel、vLLM 和 NVIDIA Dynamo 公开文档；文中引用的加速比与成本比例均对应原论文实验配置与负载条件，落地选型时需结合目标集群拓扑与真实业务流量独立定标。
