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
description: "从计算与先进封装、HBM 与 CXL、集合通信、NVMe-oF、Linux I/O 到 LLM Serving，建立一套连接芯片、机架、运行时与数据库执行引擎的 2026 系统技术雷达。"
math: true
draft: false
---

> 面向系统软件、数据库执行引擎与 AI Infra 工程师的一份长期技术雷达。本文关注的不是产品参数，而是：硬件边界变化以后，运行时应当怎样重新组织计算、数据与状态。

## 引言：系统的边界正在移动

异构设备与分层内存使数据位置直接参与性能决策，单看芯片算力无法判断端到端收益。本文选择 2026 年会议材料和公开系统文档，讨论计算、内存、网络与状态管理之间的约束；会议原型、厂商规格和生产部署经验分别标注，不把它们视为同等证据。

AI 工作负载把软件重新拉回到硬件约束最强的位置：HBM 容量决定模型并行与批处理空间，GPU 互连决定 MoE All-to-All 的上限，NIC 与交换网络决定 scale-out 的有效带宽，CXL 又在本地 DRAM 与网络远端内存之间插入一个新的时延、容量和故障域。与此同时，LLM 推理把 KV Cache、continuous batching、prefill/decode、抢占与迁移变成了新的“执行引擎问题”。

因此，今天更合适的系统抽象不再是一条整齐的纵向软件栈：

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

对上述负载，跨设备搬运与状态放置进入了执行路径，评估范围因而需要超出单核或单卡。是否扩展到机架级调度，仍取决于具体瓶颈与部署规模。

本文试图回答三个问题：

1. 2026 年最值得系统工程师持续跟踪的硬件与系统主线是什么？
2. 这些变化如何传导到 AI Serving、数据库和分布式运行时？
3. 如何建立一套可持续更新、又不被发布会参数淹没的技术雷达？

## 性能口径：时延域与跨层成本

### 比较口径

本轮比较以端到端数据路径为单位：记录数据在哪里、通过什么链路移动、哪些阶段可以重叠，以及状态失效后如何恢复。CXL、集合通信（Collective Communication）和 Serving 的共同约束是容量与供给，不是每个系统都需要同一套异构架构。

### 用“时延域”重新认识现代系统

现代机器并不存在一块同质的“内存”和一张透明的“网络”。更准确的理解方式，是把资源看成一组嵌套的时延域与带宽域。

```text
                    ┌────────── Node ──────────┐
                    │                          │
       ┌──── CPU NUMA Domain ────┐   ┌─ GPU Island ─┐
       │ L1/L2/LLC → Local DRAM  │   │ HBM ↔ NVLink │
       └─────────────┬────────────┘   └──────┬───────┘
                     │ PCIe / CXL             │
                     └──────────┬─────────────┘
                                │
                 ┌────────── Rack Fabric ──────────┐
                 │ CXL Pool / RDMA / RoCE / IB     │
                 └────────────────┬─────────────────┘
                                  │
                    NVMe-oF / Object Storage / WAN
```

同一份数据所处的位置，直接改变了算法的有效成本：

| 数据位置 | 主要优势 | 主要代价 | 运行时必须知道什么 |
| --- | --- | --- | --- |
| CPU cache | 极低时延 | 容量小、易被冲刷 | 访问局部性、分支和预取 |
| Local DRAM | 通用、容量较大 | NUMA 与带宽争用 | socket 归属、页放置、带宽 |
| GPU HBM | 高带宽、贴近计算 | 昂贵且容量受限 | batch、tensor/KV 生命周期 |
| Peer GPU memory | 可扩展显存 | 拓扑与传输成本 | NVLink/NVSwitch 路径、并行策略 |
| CXL memory | 容量扩展、可池化 | 高于本地 DRAM 的时延 | 热冷分层、共享与故障域 |
| RDMA remote memory | 跨机共享、CPU bypass | 网络抖动与拥塞 | 路由、权限、流控、重试 |
| Local NVMe | 容量大、持久化 | 微秒级 I/O 与软件开销 | queue depth、poll/interrupt |
| NVMe-oF / object store | 解耦与弹性 | 尾时延、带宽与故障放大 | locality、缓存、背压 |

这张表隐含了全文最重要的方法论：**不要问某项硬件“快不快”，而要问某个工作集放到该层级以后，端到端执行计划是否更优。**

为了检查遗漏项，可以用以下式子分解成本。各项必须先统一单位；这是分析账本而非可直接预测耗时的公式。计算与传输存在重叠时，端到端延迟还需按关键路径计算，不能简单相加：

\[
C_{op} = C_{compute} + C_{move} + C_{queue} + C_{sync} + C_{spill} + C_{recovery}
\]

其中 `C_move` 还要按照实际路径展开，而不是用一个统一的 memory cost 代替：

\[
C_{move} = \sum_{e \in path} \left(L_e + \frac{Bytes_e}{BW_e \cdot Util_e}\right)
\]

路径、利用率与并发量都会变化，这也是为什么静态峰值带宽很少能直接预测应用性能。

## 硬件路径：计算、内存、网络与存储

### 计算：从“更快的芯片”走向封装与系统协同

提高计算密度会同步增加供数、互连和散热需求，因此计算单元不能脱离封装与系统条件评价。

#### 算力增长的约束已经转移

芯片算力只有在内存供给、互连与功耗约束同时满足时才能兑现。评估对象因此需要包括：

```text
Compute Die
    +
HBM Stack
    +
I/O Die / Chiplet
    +
Interposer / Advanced Packaging
    +
Scale-up Fabric
    +
Power Delivery / Cooling
```

如果模型或中间状态无法装入 HBM，理论算力再高也可能被 PCIe 或网络传输吞噬；如果集合通信无法随 GPU 数量扩展，更多 GPU 只是制造更多等待；如果功耗和冷却无法支撑设计密度，芯片规格也无法转化为机架吞吐。

因此，评价一代 CPU/GPU/AI accelerator 时，至少要同时观察：

| 维度 | 关键问题 |
| --- | --- |
| 计算 | 支持哪些数据类型？真实 kernel 的利用率是多少？ |
| 内存 | HBM 容量、带宽与并发访存能力是否匹配？ |
| 封装 | chiplet 如何互连？封装是否限制带宽、良率和供给？ |
| Scale-up | 单机或单机架内多少设备能以高带宽互连？ |
| Scale-out | 跨机集合通信需要怎样的 NIC 和网络？ |
| 能源 | 每 token、每 query、每训练 step 的能耗是多少？ |
| 可编程性 | compiler、kernel 和 profiler 能否释放硬件能力？ |

#### Hot Chips 2026 释放的信号

会议议程能证明厂商正在讨论哪些架构问题，不能证明相应产品在目标工作负载中的收益。以下方向依据官方议程归纳，性能仍需独立实验。

[Hot Chips 2026 官方议程](https://hc2026.hotchips.org/)把 CPU、GPU、AI 加速、网络、存储和先进封装放在同一个体系结构会议里。议程中的 NVIDIA Vera CPU、NVIDIA Rubin GPU、AMD MI400 系列、Intel Diamond Rapids、Fujitsu MONAKA、Arm chiplet server SoC，以及面向 AI/HPC 的 NIC，呈现出几个比单项参数更稳定的方向：

- CPU 继续强化内存带宽、向量/矩阵能力和异构协同；
- GPU 的设计边界从芯片扩展到整机和机架；
- chiplet 与先进封装成为扩展带宽和产品组合的基础；
- 网络芯片不再只是 I/O 配件，而是 AI 系统架构的一部分；
- 推理优化开始围绕低精度、稀疏、KV Cache 和 agentic workload 展开。

这里需要避免一个常见误区：发布会上的 peak FLOPS、peak bandwidth 和最大集群规模属于能力上界，不等于应用可达到的吞吐。工程上真正要测量的是：

```text
Useful Throughput
  = min(
      compute useful work,
      memory supply,
      collective progress,
      scheduler efficiency,
      power/cooling envelope
    )
```

#### 对数据库执行引擎的启示

GPU 卸载只有在计算收益超过传输、启动和格式转换成本时才改善查询，尤其需要检查相邻算子的驻留位置。

传统执行引擎会关注 IPC、cache miss、branch miss 和 NUMA。异构系统还需要增加：

- CPU 与 GPU 的算子边界是否导致重复序列化；
- GPU kernel 启动和 JIT 编译能否被足够大的批量摊销；
- 中间结果是否跨 PCIe/NVLink 反复搬运；
- dictionary、selection vector 与数据布局是否适合目标设备；
- 某个算子变快以后，流水线是否被网络或下游算子重新限速。

换句话说，GPU offload 不是给算子贴一个 `GPU=true` 标签，而是重新划分整条 pipeline 的数据驻留边界。

### 内存：CXL 带来的不是容量，而是新的系统语义

CXL 扩展容量同时改变访问时延与共享边界，应用不能把新增容量等同于本地 DRAM。放置与迁移策略决定哪些工作集能受益。

#### CXL 应当被看成分层内存

CXL 通过 `CXL.io`、`CXL.cache` 和 `CXL.mem` 把设备、主机和内存连接起来。Linux 文档已经将 CXL memory device、decoder、region、DAX 等对象暴露为可管理的系统结构；[CXL 3.0 规范](https://computeexpresslink.org/wp-content/uploads/2024/02/CXL-3.0-Specification.pdf)进一步定义了 fabric 与 memory pooling 相关能力，[Linux CXL 驱动文档](https://docs.kernel.org/driver-api/cxl/linux/cxl-driver.html)则展示了这些能力如何落到操作系统设备模型。

但从软件视角看，CXL 的价值不能简化为“给机器插更多内存”。它实际上引入了四类新问题：

1. **放置：** 哪些页、对象或 cache block 可以承受更高时延？
2. **迁移：** 工作集热度变化时，谁负责 promotion/demotion？
3. **共享：** 多主机或多租户如何分配、隔离并回收池化容量？
4. **故障：** device、link、switch 或 fabric 失效时，状态如何恢复？

因此，应用如果完全通过通用 `malloc` 看待 CXL 内存，就丢失了最重要的物理属性（Physical Property）。更合理的接口应当逐步暴露：

```text
allocate(size,
         latency_class,
         bandwidth_class,
         sharing_scope,
         durability,
         migration_policy)
```

#### LiteSwitch：把 CXL stall 变成可调度资源

隐藏 CXL 访存停顿需要同时具备可运行线程和低成本切换机制，单纯增加内存容量不提供这一能力。

[LiteSwitch（OSDI 2026）](https://www.usenix.org/conference/osdi26/presentation/li-nanqinqin)指出，在其研究背景与平台上，CXL memory latency 通常达到本地内存的 3 倍或以上。论文没有试图把 CXL 伪装成本地 DRAM，而是通过软硬件协同识别 CXL-induced stall，并在 20ns 以内切换到另一个 ready thread；当每核有足够可运行线程时，实验最多恢复了 80% 因 CXL 时延损失的性能。

这项工作的价值不只在具体数字，而在于抽象方式：

```text
传统线程调度：
    runnable / blocked / preempted

LiteSwitch 增加的视角：
    当前线程是否正在等待某个特定内存层级？
    这段 stall 是否足够长，值得切换到其他工作？
```

它意味着未来 scheduler 可能不只感知 CPU 时间，还要感知 memory tier、device queue 和数据位置。对数据库而言，probe、aggregation 或 graph traversal 一旦访问 CXL 上的随机工作集，也可能通过更细粒度的并发隐藏时延。

但论文结论有明确边界：没有足够 ready work、切换破坏 cache locality、或者工作负载本身带宽饱和时，增加线程并不会自动带来同样收益。

#### Octopus：拓扑本身就是内存池设计

内存池共享程度与低延迟点对点通信存在拓扑取舍，Octopus 通过稀疏连接与 island 分组折中二者。

[Octopus（NSDI 2026）](https://www.usenix.org/conference/nsdi26/presentation/zhong)挑战了“所有服务器必须通过高端交换机完整连接所有 CXL pooling device”的默认假设。它用低端口数设备构建稀疏 CXL topology，以 island 组织低时延通信，同时在 pooling efficiency 与 server overlap 之间取舍。

论文构建了三服务器原型，并在测得的设备特性和物理约束下模拟到 96 台服务器。作者报告：原型上的 RPC 相比机架内 RDMA 快 3.2 倍、相比 CXL switch 路径快 2.4 倍；模拟中获得 3%—5.4% 的净服务器成本节省。这里的数字只代表论文配置，不能直接外推到任意机架，但它证明了一个重要事实：

> 当内存成为 fabric 上的资源时，“谁和谁直接相连”既是硬件拓扑，也是资源调度策略。

对上层 runtime 来说，pool capacity 不再是唯一输入，还必须知道：

- 对象位于哪个 pooling device；
- 哪些 compute node 与它有直接路径；
- 跨 island 访问需要多少 hop；
- 当前 link 的拥塞和故障状态；
- 迁移数据与迁移计算，哪一种更便宜。

#### 数据库和 AI 系统如何使用 CXL

访问热度和复用方式决定数据是否适合远端层级；错误迁移热状态可能用容量收益换来访存延迟。下表是待负载验证的放置假设。

较自然的使用方式不是把所有对象随机溢出到 CXL，而是按复用频率和访问模式分层：

| 对象 | 更适合的位置 | 原因 |
| --- | --- | --- |
| 高频 hash table / hot dimension | Local DRAM 或 HBM | 随机访问对时延敏感 |
| 冷分区 / 低频索引页 | CXL memory | 容量收益大，可容忍一定时延 |
| KV Cache 冷块 | CXL 或 host memory | 生命周期明确，可配合迁移/预取 |
| checkpoint / immutable segment | NVMe | 容量和持久化优先 |
| 跨节点共享 metadata | CXL/RDMA，视拓扑而定 | 要权衡一致性与通信开销 |

最终，buffer manager、KV cache manager 和 memory tier manager 会越来越像同一个问题的不同实例：维护状态、估计未来复用、选择驻留层级、触发迁移，并在压力下做 admission 与 eviction。

### 网络：集合通信已经进入执行计划

通信完成时间取决于流量分布与拓扑争用，而不只是总字节数。集合通信（Collective Communication）需要与计算阶段及路由条件共同安排。

#### 从“网络足够快”到“通信需要被编译”

总字节数除以带宽无法表达 incast 和链路共享，因此只能作为无争用近似。传统模型常写为：

传统数据系统经常把网络成本抽象为：

\[
C_{network} = latency + \frac{bytes}{bandwidth}
\]

这个模型适合表达基线，却不足以描述现代 AI cluster：物理拓扑可能是两级或三级，GPU/NIC 比例不一，链路异构，流量同时受到 ECMP、incast、queueing 和 collective schedule 影响。尤其对 MoE，token dispatch 与 combine 会产生结构化的 All-to-All(v) 流量，通信矩阵还会随路由结果动态倾斜。

因此，集合通信需要回答的不只是“发送多少字节”，还包括：

```text
Who talks to whom?
        +
Which path and NIC?
        +
In what order?
        +
At what concurrency?
        +
How to react to skew and congestion?
```

#### FAST：MoE All-to-All 的拓扑感知调度

MoE 路由倾斜会制造不均匀收发，静态通信模板可能让少数路径限制整个阶段。FAST 对服务器内再平衡与跨服务器调度分别处理。

[FAST（NSDI 2026 技术议程）](https://www.usenix.org/conference/nsdi26/technical-sessions)针对 MoE 的 All-to-All(v) 通信处理三类现实问题：token 分布倾斜、两级 fabric 以及 incast。其基本思路是先在 server 内进行 rebalancing，再把 scale-out 传输组织为更均衡的一对一调度，并在 H200 与 MI300X 集群上评估。

这类工作说明 collective library 正在从固定模板走向 workload-aware schedule synthesis：

- 同一个 `all_to_all`，其最优 schedule 取决于消息矩阵；
- 同一个模型，expert routing 变化会改变网络瓶颈；
- 单机 NVLink/NVSwitch 和跨机 RDMA 不能使用同一套成本；
- 通信与 kernel 的 overlap 必须放进统一时间线评估。

数据库的 exchange、shuffle 和 distributed join 面临同样的结构。区别只是数据单元从 token 变成 partition/batch，代价依旧来自 skew、incast、拓扑和背压。因此，未来 exchange operator 很可能也会包含更强的 topology-aware routing 与在线重平衡。

#### InfiniBand、RoCE 与 Ethernet：不存在脱离环境的赢家

选择网络技术时，不应只比较线速。需要把以下因素放入同一个工程模型：

| 维度 | 需要回答的问题 |
| --- | --- |
| 端到端时延 | 小消息、控制消息与大流分别表现如何？ |
| 有效吞吐 | 多流并发与 collective 下能达到多少？ |
| 拥塞控制 | incast、热点和长短流混合时是否稳定？ |
| Lossless 依赖 | PFC/ECN 配置错误会怎样放大故障？ |
| 拓扑 | oversubscription、rail 与多 NIC 如何布局？ |
| 运维 | 可观测性、故障定位与容量扩展成本如何？ |
| 软件生态 | RDMA、collective library、storage stack 是否成熟？ |

“IB 还是 Ethernet”不是一个只靠协议名称能回答的问题。真正决定结果的是 workload、拓扑、交换机缓冲、拥塞控制、NIC 能力和运维成熟度的组合。

### 存储与内核 I/O：设备变快以后，软件开销重新显形

设备延迟下降后，提交与完成路径的固定软件开销占比上升，优化对象需要从介质延伸到线程与队列管理。

#### 更快的 SSD 暴露了 I/O 路径成本

当 NVMe 设备时延持续下降，系统调用、中断、上下文切换、block layer、文件系统和 queue management 在总时延中的占比会上升。此时优化重点从“让设备更快”变成“让 CPU 以更低代价驱动设备”。

典型路径可以分为：

```text
Interrupt:
    submit → sleep → IRQ → wakeup → completion

Polling:
    submit → busy poll → completion

Hybrid:
    submit → short poll → sleep/interrupt → completion
```

polling 降低唤醒时延，却会持续消耗 CPU；interrupt 节省 CPU，却带来调度和尾时延成本。不存在对所有负载都最优的固定模式。

#### UnICom：把 I/O completion 策略变成动态决策

轮询与中断分别消耗 CPU 周期和唤醒开销，适用性随负载变化。UnICom 通过内核中的完成调度机制处理这种取舍。

[UnICom（FAST 2026）](https://www.usenix.org/conference/fast26/presentation/pan)观察到，在高性能 SSD 与 CXL SSD 场景中，I/O software overhead 最多可占其测试总时延的约 50%。论文通过 TagSched、TagPoll 和 SKIP 等机制，在 polling 与 interrupt 之间动态调度 completion，并与 ext4、BypassD、io_uring 等基线比较。

这项工作的系统意义在于：**completion mechanism 不应只是启动参数，而应成为随 CPU pressure、queue depth 和目标 SLO 变化的运行时策略。**

同样的思想也适用于数据库：

- 前台点查与后台 compaction 不应使用相同 I/O policy；
- tail-latency-sensitive 请求可能值得短暂 polling；
- CPU 饱和时继续 busy poll 反而损害查询吞吐；
- io_uring、direct I/O 和 userspace storage 必须按端到端路径评估，而非单测 syscall 数量。

#### NVMe-oF：存储解耦也是网络调度问题

存算解耦把存储队列与网络路径耦合起来，因此仅优化 SSD 不能消除链路干扰。该研究将路径选择、带宽分配和队列调度联合考虑。

[Co-Designing Traffic Control with NVMe-oF（NSDI 2026）](https://www.usenix.org/conference/nsdi26/presentation/wang-chendong)比较了 switched 与 switchless SAN。论文把 disaggregated storage 的流控分解为 path selection、bandwidth allocation 和 queue scheduling，并结合小规模实机原型与大规模模拟评估。

作者的实验表明，switchless SAN 可以通过多条 load-aware I/O path 缓解干扰，在其设置下达到与 switched SAN 相当的吞吐并降低时延，同时避免高 radix switch 的成本和 ToR 单点问题。

这里更重要的不是宣布某种拓扑“获胜”，而是理解：

```text
Disaggregated Storage
  = storage placement
  + network path selection
  + congestion control
  + queue scheduling
  + failure handling
```

当 compute 与 storage 解耦，query optimizer 如果仍然只估计扫描字节数，就遗漏了路径争用、并发 I/O、缓存命中与尾时延。remote scan 的物理属性至少应包含 storage node、network locality、replica choice 和 admission state。

## 运行时决策：AI Serving 与数据库

### AI Serving：一种状态密集型执行引擎

LLM 服务需要持续保存与迁移请求状态，调度决策不能只看 GPU kernel 的瞬时利用率。KV Cache 容量和请求级延迟目标共同限制可接纳并发。

#### 它为什么越来越像数据库

LLM Serving 并不只是“调用一次 GPU kernel”。一次请求会经历 admission、tokenization、prefix lookup、prefill、KV allocation、decode scheduling、sampling、streaming 和回收，且每一步都受到容量与 SLO 约束。

它和数据库执行引擎之间存在一组有用的对应关系：

| AI Serving | 数据库系统 | 共同问题 |
| --- | --- | --- |
| Request | Query | admission、优先级、取消 |
| Token batch | Vectorized batch | 批量大小与延迟平衡 |
| KV Cache | Buffer pool / state store | 放置、复用、淘汰、迁移 |
| Prefix cache | Materialized/intermediate cache | 等价性、命中收益、失效 |
| Prefill | Scan/build/compile-like stage | 计算密集、可批处理 |
| Decode | Stateful iterative execution | 依赖前序状态、长尾 |
| Continuous batching | Morsel/task scheduling | 动态组批与资源利用率 |
| Model parallelism | Distributed physical plan | 切分、通信与拓扑 |
| Preemption | Query suspension | 状态保存与恢复成本 |
| Goodput under SLO | Throughput under latency SLO | 不能只追求裸吞吐 |

但二者并不等价。decode 是自回归过程，每一步依赖前面的 token；KV Cache 会随序列增长；不同请求输出长度难以预知；GPU 的高效执行依赖 batch；TTFT（首 token 时延）和 ITL（token 间时延）又约束调度器不能只最大化吞吐。

#### PagedAttention：把 KV Cache 变成虚拟内存问题

按块分配 KV Cache 可以减少连续分配造成的浪费，但仍需维护逻辑块到物理块的映射与共享生命周期。

[PagedAttention 论文](https://arxiv.org/abs/2309.06180)借鉴操作系统 paging，将每个请求逻辑上连续的 KV Cache 切成固定大小 block，使物理显存可以非连续分配，并支持 block 粒度的共享与回收。

```text
Logical KV blocks of request A
    [A0] [A1] [A2] [A3]
      │    │    │    │
      ▼    ▼    ▼    ▼
Physical GPU blocks
    [A2] [B0] [A0] [free] [A3] [A1]
```

这一设计缓解了连续预留导致的内部/外部碎片，也让 copy-on-write、prefix sharing 等策略更自然。更深层的意义是：AI runtime 开始拥有一套类似操作系统和数据库 buffer manager 的地址映射、引用计数、eviction 与 sharing 机制。

需要注意，PagedAttention 解决的是 KV 内存管理的一部分，不等于完整的调度最优。block size、attention kernel、prefix 命中率、batch composition 和 offload 路径仍需联合优化。

#### KV Cache 已经形成多级存储层次

KV Cache 的管理正在从单 GPU 内存池扩展为层次化状态系统：

```text
GPU HBM
   ↓ eviction / migration
Peer GPU memory
   ↓
Host DRAM
   ↓
CXL memory
   ↓
Local NVMe
   ↓
Remote KV store / object storage
```

每一层都需要回答四个问题：

1. 复用概率是否高于迁移成本？
2. 加载是否能与计算 overlap？
3. 状态是否可以重算，重算是否反而更便宜？
4. 淘汰一个 block 对 TTFT、ITL 和整体 goodput 的影响是什么？

因此，简单的 LRU 往往不够。更有信息量的策略会同时考虑 prefix popularity、block size、remaining tokens、tenant priority、transfer bandwidth 和 recomputation cost。

#### Prefill/Decode 解耦：分阶段执行，而非免费加速

prefill 和 decode 的资源形态不同：prefill 通常具有更高的并行计算密度，decode 更受 KV 带宽、并发序列和逐 token 调度影响。把两者部署到独立 worker pool，可以分别扩缩容和选择并行策略。

[NVIDIA Dynamo 的 disaggregated serving 文档](https://docs.nvidia.com/dynamo/dev/kubernetes/disaggregated-serving/overview)展示了典型流程：prefill worker 生成 KV Cache，通过 NIXL 将其传给 decode worker，再由 decode 持续生成 token；同机可走 NVLink/CUDA IPC，跨机通常需要 RDMA 等高速 fabric。

```text
request
   │
   ▼
Prefill Router → Prefill Worker
                      │
                      │ KV transfer
                      ▼
Decode Router  → Decode Worker → token stream
```

解耦的收益来自独立扩缩容、减少长 prefill 对 decode 的干扰，以及为两个阶段选择不同硬件/并行度（Degree of Parallelism）；代价则是 KV transfer、额外路由、状态一致性和故障恢复。Dynamo 文档也明确指出：小模型、短 prompt、低并发或缺乏高速 KV transfer fabric 时，aggregated deployment 更简单且常常更快。

所以正确问题不是“要不要 P/D 分离”，而是：

\[
Benefit_{separation} > Cost_{KV\ transfer} + Cost_{routing} + Cost_{imbalance}
\]

#### 年的三个 Serving 信号

抢占、冷启动和跨模型复用针对不同成本，不能把它们的加速比分别相乘当成系统总收益。以下结果均限定于对应论文的实验。

2026 年 NSDI 的几项工作分别击中了调度、冷启动和跨模型状态复用：

- [FastServe](https://www.usenix.org/conference/nsdi26/presentation/wu-bingyang)把 preemption 下沉到 iteration/token 粒度，使用 skip-join MLFQ，并主动在 GPU 与 host memory 之间 offload/upload 中间状态。论文在其工作负载中报告最高 6.1 倍吞吐提升。它说明抢占是否有效，取决于状态切换能否与执行 overlap。
- [HydraServe](https://www.usenix.org/conference/nsdi26/presentation/lou)面向 serverless LLM 冷启动，提前把模型分布到多台 server、重叠 worker 内的启动阶段，并通过 GPU placement 避免多个冷启动实例争用网络。论文报告相对基线将冷启动降低 1.7—4.7 倍。
- [DroidSpeak](https://www.usenix.org/conference/nsdi26/presentation/liu-yuhan)研究同架构 fine-tuned model 之间的 KV 共享：选择性重算少量层，其余层复用，并流水化重算与加载。论文在评估中报告最高 4 倍吞吐和约 3.1 倍 prefill 加速，同时在所用质量指标上损失很小。

三项工作共同说明：Serving 的核心状态不只包括 model weight，还包括动态 KV、队列位置、执行进度和可复用中间结果；运行时优化的关键，是让这些状态可以被识别、迁移、共享和恢复。

### 数据库执行引擎需要补充哪些资源决策

跨设备执行会让数据驻留位置参与算法选择；若忽略传输，同一个算子的局部加速可能拖慢整条查询。

#### 从算子选择扩展到资源与拓扑选择

经典 optimizer 的物理决策主要是 join order、join algorithm、aggregation method、distribution 和 sort property。异构系统要求把 device、memory tier 与 topology 也纳入 physical property：

```text
PhysicalProperty {
  distribution,
  ordering,
  partitioning,
  device,
  memory_tier,
  data_residency,
  network_domain,
  fault_domain,
  latency_slo
}
```

例如，一个 GPU hash join 的算术阶段可能很快，但如果两边输入在远端存储、build table 超过 HBM、结果又立即返回 CPU，端到端计划可能不如 CPU radix join。相反，如果上游 scan/filter 和下游 aggregation 都已在 GPU，保持 pipeline resident 往往比单个算子峰值更重要。

本文与上一篇[《Hash、Sort，还是 Hybrid：现代执行引擎如何选择数据重组策略》]({{< relref "2026-09-10-dive-hash-sort-strategy.md" >}})的关系是：上一篇讨论算法选择，这一篇进一步说明，算法必须和实际资源层级一起选择。

#### 标量 cost 不足以表达现实约束

线性加权能给可行计划排序，却不能自动表达容量和服务等级的硬约束；例如内存超限需要改变执行路径，而不只是增加一个固定分数。常见的成本表达是：

\[
Cost = w_c C_{cpu} + w_i C_{io} + w_n C_{network}
\]

但在现代系统中，很多约束不能被线性权重安全替代：HBM 容量超限会 spill，NIC 饱和会产生非线性排队，TTFT 超过门槛意味着请求失去 goodput，故障域集中又会改变可用性。

一种研究表达是先约束可行性，再比较延迟、成本与能耗；下式不是现成求解器，带宽安全值与故障风险也必须有可测量定义：

\[
\begin{aligned}
\min\quad & (Latency_{p99},\ Cost,\ Energy) \\
\text{s.t.}\quad
& HBM(plan) \le HBM_{budget} \\
& BW_e(plan) \le BW_e^{safe},\ \forall e \\
& TTFT \le SLO_{ttft} \\
& ITL \le SLO_{itl} \\
& FaultDomain(plan) \le RiskBudget
\end{aligned}
\]

这种建模增加了搜索维度，也要求更新资源状态；当负载稳定、设备单一时，其维护成本可能超过收益。本文将它视为待验证的设计方向，不宣称已有优化器都采用这一形式。

#### Buffer Pool、KV Cache 与状态管理正在汇合

数据库 buffer pool 与 KV Cache 的对象语义不同，但管理问题高度相似：

- 容量有限，工作集远大于最快层级；
- 命中价值由未来访问决定；
- 淘汰、迁移和重算存在不同成本；
- 多租户之间需要配额和隔离；
- 状态位置会直接改变执行计划。

数据库领域长期积累的 admission control、cost model、cache replacement、spill 与 checkpoint 思想，可以迁移到 AI runtime；反过来，AI Serving 对 GPU block 管理、连续组批和跨层 KV transfer 的实践，也会推动传统执行引擎重新审视显存与异构内存管理。

#### VLDB 2026 展示的交叉方向

会议论文能提供具体问题和实验案例，但奖项本身不能证明方案适合某个生产系统。以下仅据官方奖项页面说明研究主题。

[VLDB 2026 Conference Awards](https://vldb.org/2026/conference-awards.html)中，最佳工业论文 OmniTable 面向 PB 级 LLM 数据整理与探索；最佳研究论文荣誉提名（Best Research Paper Honorable Mention）包括分析数据库标量函数的 GPU 加速，以及“How to Write to SSDs”；FastCompose 则关注 query execution 的 compilation cold start。

这些论文不能单独证明整个数据库领域已经转向 AI Infra，但它们提供了清晰的交叉信号：

- 数据库正在成为 AI 数据治理与探索基础设施；
- GPU kernel、编译成本和设备数据路径进入执行引擎核心；
- 存储介质行为依然会反向塑造数据结构和写入协议；
- 系统设计越来越难按“数据库、AI、操作系统、网络”分别优化。

## Linux 与系统软件：硬件能力最终要通过内核落地

发布会宣布的硬件能力，只有进入内核、驱动、runtime 和可观测工具以后，才真正成为可编程系统能力。2026 年值得持续追踪的 Linux 主题包括：

### 异构内存管理

放置策略依赖内核暴露的拓扑和迁移接口，runtime 无法仅凭容量统计判断访问成本。

- CXL device、decoder、region 与 DAX 的管理接口；
- NUMA balancing、page migration 与 tiering；
- DAMON 等内存访问观测机制；
- huge page、TLB 与大规模工作集；
- device memory 与 host memory 的一致性和 pinning。

阅读入口可从 [Linux CXL documentation](https://docs.kernel.org/driver-api/cxl/)和 [DAMON documentation](https://docs.kernel.org/mm/damon/)开始。关键不是记住 sysfs 名称，而是理解：内核知道哪些 locality，runtime 又能获取和控制到什么程度。

### 调度与隔离

共享硬件上的局部性能提升可能损害其他租户，因此应同时检查调度、配额和压力信号。

- CPU scheduler 如何处理异构 core、短任务和 latency-sensitive workload；
- cgroup v2 的 CPU、memory、I/O 与 device isolation；
- PSI 等 pressure signal 是否能进入 runtime admission；
- GPU、NIC 和 accelerator 是否拥有与 CPU 对等的资源治理能力。

### I/O 与数据路径

绕过部分内核路径不会自动消除系统管理成本，内存注册、权限和回收仍需由具体组件负责。

- io_uring 与传统 AIO 的适用边界；
- XDP/eBPF、DPDK 与 kernel networking 的取舍；
- RDMA、GPUDirect 与 userspace driver 的安全隔离；
- zero-copy 是否真的消除了 copy，还是把 pinning/ownership 成本转移到别处。

判断一项“kernel bypass”技术时，应检查完整生命周期：注册内存、建立连接、异常回退、资源回收、容器隔离和故障恢复。fast path 很短，不代表系统总成本很低。

## 评估方法：证据、优先级与部署边界

### 技术评估优先级与证据门槛

采用优先级应取决于目标负载和已有基础设施，不是对行业成熟度的统一排名。以下 `Adopt / Trial / Assess / Watch` 是本文的条件化建议：即使标为 Adopt，也需满足表中的适用前提。

| 层级 | 技术方向 | 当前判断 | 应验证的关键问题 |
| --- | --- | --- | --- |
| Adopt | HBM-aware batching 与容量规划 | 已是 AI runtime 基础能力 | OOM、碎片与 SLO 是否纳入统一监控？ |
| Adopt | Topology-aware collective | 通信占比较高且拓扑不均匀时优先验证 | 实际流量是否匹配 rail/NIC 布局？ |
| Adopt | NVMe async I/O 与分层缓存 | 数据系统成熟方向 | tail latency 和 CPU overhead 是否同时改善？ |
| Trial | Prefill/Decode disaggregation | 长 prompt、高并发场景有潜力 | KV transfer 是否抵消阶段解耦收益？ |
| Trial | KV-aware routing / prefix cache | agent/RAG 工作负载价值较高 | 命中率、租户隔离与一致性如何？ |
| Trial | GPU database operator pipeline | 适合算术密集且可驻留的 pipeline | 数据搬运和编译成本能否摊销？ |
| Assess | CXL memory tiering | 硬件与 OS 能力快速成熟 | 热度识别、迁移和故障模型是否可控？ |
| Assess | CXL memory pooling | 资源利用率潜力显著 | fabric 成本、拓扑和多租户隔离如何？ |
| Assess | DPU/SmartNIC offload | 网络、存储和安全路径逐渐下沉 | offload 后可观测性和可编程性是否足够？ |
| Assess | Switchless disaggregated storage | 研究显示有成本/路径潜力 | 布线、路由、故障和规模化运维如何？ |
| Watch | Optical interconnect | 可能改变 scale-up/scale-out 边界 | 成本、功耗、可靠性和量产节奏如何？ |
| Watch | Compute-in-memory / near-data compute | 有望减少数据搬运 | 编程模型、精度、通用性和一致性如何？ |

技术雷达不是采购清单。`Trial` 表示值得用真实 workload 做小规模实验，`Assess` 表示需要持续验证系统边界，`Watch` 则表示方向重要但尚不足以进入当前关键路径。

### 如何组织资料与验证证据

调研应保存问题、条件和验证记录，而不只保存链接；否则后续无法判断结论变化来自版本、硬件还是负载。

#### 先区分证据层级

规格上限、论文原型和实际部署回答不同问题，不能相互替代。资料应按可支持的结论分层：

| 层级 | 典型来源 | 适合回答什么 | 主要风险 |
| --- | --- | --- | --- |
| 规范与源码 | CXL、Linux、vLLM、Dynamo、硬件编程手册 | 能力和接口到底是什么 | 细节多，缺少全局评价 |
| 论文与会议 | OSDI、NSDI、FAST、MLSys、ASPLOS、VLDB、SIGMOD | 新问题、机制与实验边界 | 原型环境未必等于生产 |
| 工程博客 | Cloudflare、数据库/云厂商技术博客 | 真实故障、运维和规模经验 | 环境具有企业特异性 |
| 微架构分析 | Chips and Cheese、ServeTheHome | 实机行为、平台形态 | 结论依赖测试方法 |
| 产业分析 | SemiAnalysis、The Next Platform | 供给、封装、机架和市场联系 | 可能混入预测和商业判断 |
| arXiv / 社区讨论 | 新论文、RFC、mailing list | 提前发现方向 | 未经同行评审、噪声高 |

几个长期入口：

- [Chips and Cheese](https://chipsandcheese.com/)：从 microbenchmark 和反向分析理解 cache、TLB、interconnect 与执行单元；
- [ServeTheHome](https://www.servethehome.com/)：观察服务器、DPU、NIC、NVMe、CXL 和整机形态；
- [SemiAnalysis](https://semianalysis.com/)：连接制程、HBM、封装、GPU、机架、功耗与供应链；
- [The Next Platform](https://www.nextplatform.com/)：跟踪 HPC、AI、CPU/GPU、网络和系统架构；
- [Cloudflare Blog](https://blog.cloudflare.com/)：阅读大规模网络、Linux、eBPF、安全与故障工程；
- [LWN](https://lwn.net/)：理解 Linux patch、内核机制和社区演化；
- [USENIX Conferences](https://www.usenix.org/conferences)：跟踪 OSDI、NSDI、FAST 等系统论文；
- [MLSys](https://mlsys.org/)：连接模型、compiler、runtime 与硬件系统；
- [PVLDB](https://www.vldb.org/pvldb/)和 [SIGMOD](https://sigmod.org/)：跟踪数据库执行、优化、存储与 AI for data；
- [arXiv](https://arxiv.org/)：按问题订阅，而不是泛读全部更新。

#### 按问题阅读，不按产品阅读

围绕目标负载组织资料，才能比较不同产品对同一瓶颈的影响。可从以下可测量问题开始：

- 为什么同一个 hash table 在两代 CPU 上差异巨大？
- 为什么 SIMD filter 没有获得线性加速？
- 为什么增加 GPU 后 collective 反而成为瓶颈？
- 为什么 CXL 扩容后 p99 明显恶化？
- 为什么 NVMe 已经很快，CPU 利用率却继续升高？
- 为什么 P/D 分离降低了 decode 干扰，却恶化了 TTFT？

这些问题会把阅读自然连接到 cache topology、memory-level parallelism、TLB、queueing、data movement 和 scheduler，而不是停留在产品参数。

#### 建立固定节奏

阅读计划需要留下复现和归档时间，否则只能积累结论而不能核对适用条件。以下时长是个人安排示例，不是效率测量结果。

**每日 10—20 分钟：** 浏览论文 feed、Linux/项目更新和少量工程新闻，只做筛选，不追求读完。

**每周 1—2 小时：** 选择一个主题深读一篇论文或一组相关 patch，记录问题、机制、假设、实验和局限。

**每月半天：** 把材料更新到技术雷达；删除被证伪的判断，标记哪些结论只有单篇论文支持。

**每季度一次实验：** 用自己的 workload 复现一个关键判断，例如 HBM 容量曲线、CXL tiering、collective skew、NVMe polling 或 KV transfer break-even point。

真正的技术积累来自这条闭环：

```text
发现信号
   ↓
阅读一手材料
   ↓
写出机制与假设
   ↓
用 workload 验证
   ↓
更新 cost model / design / radar
```

### 做系统设计时应反复追问的十二个问题

方案只有在目标数据路径上改善瓶颈且满足故障与隔离要求，才具备采用依据。评审可以按以下问题收集证据：

1. 工作集真正受 compute、capacity、bandwidth 还是 latency 限制？
2. 数据当前在哪里，执行后又要去哪里？
3. 最贵的一次跨层搬运是什么，能否避免或 overlap？
4. 峰值性能对应什么数据类型、batch size 和并行规模？
5. p50 改善是否以 p99、goodput 或 CPU 消耗为代价？
6. 负载倾斜、短长请求混合与热点出现时会怎样？
7. 状态能否迁移、共享、重算和恢复？各自成本是多少？
8. 方案是否依赖足够多的并发工作来隐藏 stall？
9. 拓扑变化或链路拥塞后，静态计划是否仍然成立？
10. 多租户隔离、配额与安全边界是否进入 fast path？
11. 单个组件的收益在端到端 pipeline 中还剩多少？
12. 失败时是否有可观测、可回退、可恢复的 slow path？

这些问题适用于 CXL、GPU offload、DPU、disaggregated serving，也适用于数据库的 hash/sort/join、shuffle 和 remote scan。

### 结语：先测跨层成本，再扩大设计范围

异构与解耦方案只有在目标负载上证明净收益，才值得进入关键路径。评估应先记录数据驻留、跨层字节数、等待时间和状态恢复成本，再决定采用 GPU 卸载、CXL 分层或 Prefill/Decode 解耦。

本文的技术优先级不是采购建议，论文中的最高加速比也不是容量规划参数。短请求、低并发、低复用或故障隔离要求较高的系统，可能更适合较少层级、较少迁移的架构。上线前至少保留同硬件基线、尾延迟（Tail Latency）与资源成本对照，以及可观测的回退路径。

## 参考资料

### 规范、内核与开源项目

1. [Compute Express Link 3.0 Specification](https://computeexpresslink.org/wp-content/uploads/2024/02/CXL-3.0-Specification.pdf)
2. [Linux Kernel: CXL Driver Documentation](https://docs.kernel.org/driver-api/cxl/)
3. [Linux Kernel: DAMON Documentation](https://docs.kernel.org/mm/damon/)
4. [vLLM / PagedAttention Project Introduction](https://vllm-project.github.io/2023/06/20/vllm.html)
5. [NVIDIA Dynamo: Disaggregated Serving](https://docs.nvidia.com/dynamo/dev/kubernetes/disaggregated-serving/overview)

### 论文与会议材料

6. [Efficient Memory Management for Large Language Model Serving with PagedAttention](https://arxiv.org/abs/2309.06180)
7. [Hot Chips 2026 Program](https://hc2026.hotchips.org/)
8. [LiteSwitch: Harvesting Sub-Microsecond CXL Memory Stalls](https://www.usenix.org/conference/osdi26/presentation/li-nanqinqin)
9. [Octopus: Enhancing CXL Memory Pods via Sparse Topology](https://www.usenix.org/conference/nsdi26/presentation/zhong)
10. [NSDI 2026 Technical Sessions](https://www.usenix.org/conference/nsdi26/technical-sessions)
11. [FastServe: Iteration-Level Preemptive Scheduling for LLM Inference](https://www.usenix.org/conference/nsdi26/presentation/wu-bingyang)
12. [HydraServe: Minimizing Cold Start Latency for Serverless LLM Serving](https://www.usenix.org/conference/nsdi26/presentation/lou)
13. [DroidSpeak: KV Cache Sharing Across Fine-tuned Model Variants](https://www.usenix.org/conference/nsdi26/presentation/liu-yuhan)
14. [UnICom: Unified I/O Completion for Fast Storage](https://www.usenix.org/conference/fast26/presentation/pan)
15. [Co-Designing Traffic Control with NVMe-oF for Disaggregated Storage](https://www.usenix.org/conference/nsdi26/presentation/wang-chendong)
16. [VLDB 2026 Conference Awards](https://vldb.org/2026/conference-awards.html)

### 长期追踪入口

17. [Chips and Cheese](https://chipsandcheese.com/)
18. [ServeTheHome](https://www.servethehome.com/)
19. [SemiAnalysis](https://semianalysis.com/)
20. [The Next Platform](https://www.nextplatform.com/)
21. [Cloudflare Blog](https://blog.cloudflare.com/)
22. [LWN](https://lwn.net/)
23. [USENIX Conferences](https://www.usenix.org/conferences)
24. [MLSys](https://mlsys.org/)
25. [Proceedings of the VLDB Endowment](https://www.vldb.org/pvldb/)
26. [ACM SIGMOD](https://sigmod.org/)
