---
title: "【源码】Ray：把对象当货币，从任务提交、对象生命周期到扩容预算"
slug: "ray-object-as-currency-source-notes"
date: 2026-11-10T00:00:00+08:00
lastmod: 2026-11-10T00:00:00+08:00
categories:
  - 分布式系统
tags:
  - Ray
  - 分布式计算
  - Ray Data
  - 调度
  - Object Store
  - GCS
  - 源码阅读
description: "基于本地 Ray 源码 d3febe73 与 13 个可复现实验，追踪一次任务提交在哪里停住、对象位置由谁维护、扩容预算与需求的先后关系，再把 Ray Data 放回 Spark、Dask、Daft 与 MPP 引擎的谱系里，说明哪些经验可以迁移、哪些不能。"
toc: true
math: false
---

我最初把 Ray 理解成"带状态管理的分布式函数调用"：`@ray.remote` 相当于一次远程过程调用（Remote Procedure Call, RPC），参数序列化过去，结果序列化回来，Actor 只是把函数换成对象。按这个模型，八个远程任务应该出现在八个进程里，五个 Actor 方法调用应该产生五份状态。

本地实验的第一批输出就把这个模型推翻了一半：

```text
tasks（8 次 square 调用）      pid = 158440, 158439, 158440, 158439, ...
actor_calls（5 次 add 调用）   pid = 158578, 158578, 158578, 158578, 158578
```

任务只落在两个进程上，Actor 的五次调用固定在同一个进程里。进程数既不由任务数决定，也不由调用数决定。我原本以为自己在观察"远程调用"，实际观察的是**一次资源租约换来的执行机会**：租约给了谁，谁就跑；能给几个，取决于调度账目而不是我提交了多少个。

顺着这个偏差继续追，Ray 的形状越来越不像 RPC 框架，而像一套以对象为计价单位的资源经济：任务不是"发出去执行"，而是"先在本地登记依赖、拿到队列位置、再向节点要租约"；对象不是"返回值的包装"，而是**所有权凭证加位置查询入口**；扩容不是"响应负载"，而是**先算预算，再在预算内安置需求**。

这篇文章的主问题就是：**Ray 用什么抽象把分布式执行变成可以局部推理的东西，这个抽象又在哪些地方失效？**

下文分三条源码链路展开——任务提交、对象所有权、扩容预算——每条链路都给出发起点、停住的位置和实测数据。然后把 Ray Data 放回 Spark、Dask、Daft 与大规模并行处理（Massively Parallel Processing, MPP）引擎的谱系里，说明它继承了谁、放弃了谁，最后交代哪些经验可以迁移到数据库引擎的工作、哪些必须重学。

## 实验环境与可复核手段

源码结论依赖版本，实验结论依赖机器形状，两者都要先钉住。

| 项目 | 状态 |
| --- | --- |
| Ray 源码 | `ray-project/ray` 本地检出，HEAD `d3febe73`（提交日期 2026-09-14） |
| Python | 3.11.4，独立虚拟环境，通过 `--system-site-packages` 继承已编译的 Ray 3.0.0.dev0 |
| Ray 导入来源 | 源码树内 `python/ray/__init__.py`，即仓库源码而非已安装轮子 |
| CPU（Central Processing Unit） | 96 核可见，实验默认只申请 4 核 |
| GPU（Graphics Processing Unit） | 无可见 NVIDIA 设备 |
| 对象存储 | 默认 256 MiB；13 号实验按节点设 160 MiB 上限 |
| `/dev/shm` | 64 MiB |

实验集中在本地 raylab 工程，共 13 个用例，编号 01 至 11、13（12 号预留给尚未实现的对象溢写曲线）。每个用例是一个独立进程，由同一入口调度，结果写入 `result.json`，同时记录退出码、是否超时和残留进程。这一点很重要：下文的数字都来自断言通过后落盘的文件，不是我回忆的日志片段。

有两条测量边界必须先声明，否则后面的数字会被误读。

第一，单机实验不走物理网卡。13 号实验在同一台机器上用本机网卡地址与回环地址（`127.0.0.2`）起了两个真实 Ray 节点，它走完整的网络栈，但两个节点共享同一宿主机。它能证明**位置语义**，不能证明跨机带宽和延迟。

第二，小数据上的秒数不是吞吐。10 号实验里 hash shuffle 用了 1.67 秒、无键的 all-to-all 用了 0.22 秒，这个差值主要是聚合器 Actor 的启动开销，不是 shuffle 算法本身的成本差异。

## 链路一：一次 SubmitTask 在哪里停住

问题来自一个具体现象：提交一百个耗时 1 秒的任务、把每类租约的在途请求上限压到 1，总耗时 25.7 秒。任务并没有变慢，是**派发节奏被压住了**。要解释这件事，得先知道一次提交路上有几道闸门。

从驱动侧进入 [CoreWorker::SubmitTask](https://github.com/ray-project/ray/blob/d3febe7397071c9993aafc1b33a0d5059ea2cc4c/src/ray/core_worker/core_worker.cc#L2056)，到本地节点真正收到 `RequestWorkerLease`，路径分成三段。

```text
A 段  driver 进程内（core_worker.cc）
     SubmitTask
       ├─ 订阅节点变更
       ├─ 校验调度策略
       ├─ 加入 Placement Group 约束
       ├─ BuildCommonTaskSpec      构造规格
       ├─ AddPendingTask           生成 ObjectRef，登记血缘
       └─ io_service_.post         转投事件循环，函数返回

B 段  driver 进程内（normal_task_submitter.cc）
     SubmitTask（提交器版本）
       ├─ ResolveDependencies      ← 主阻塞点
       └─ 回调：标记依赖完成
                ├─ 失败 → 重试或终止
                ├─ 已取消 → 丢弃
                ├─ 构造 SchedulingKey
                ├─ 首次构造 LeaseSpecification
                ├─ 入队
                ├─ 有空闲 worker → 直接复用
                └─ RequestNewWorkerIfNeeded

C 段  driver 进程内（RequestNewWorkerIfNeeded）
     四道闸门
       ├─ ① 该类在途租约已达上限 → 节流，直接返回
       ├─ ② 该类还有空闲 worker   → 不用再要
       ├─ ③ 队列为空               → 清理账目后返回
       ├─ ④ 队列长度 ≤ 在途租约数  → 不用再要
       └─ 通过 → GetBestNodeForLease → RequestWorkerLease（RPC 到 raylet）
```

A 段的代码值得逐句读，因为它**什么判断都没做**。[SubmitTask](https://github.com/ray-project/ray/blob/d3febe7397071c9993aafc1b33a0d5059ea2cc4c/src/ray/core_worker/core_worker.cc#L2126) 里没有任何"这个任务能不能跑"的检查，它只做构造、登记、投递。真正的等待发生在 B 段第一行 [ResolveDependencies](https://github.com/ray-project/ray/blob/d3febe7397071c9993aafc1b33a0d5059ea2cc4c/src/ray/core_worker/task_submission/normal_task_submitter.cc#L38)。这是第一个可以讲清楚的架构事实：

**Ray 的任务提交路径上没有全局调度决策，只有本地账目加一次向节点的租约请求。**

驱动不知道集群里有多少空闲核。它只知道"我自己这一类任务还欠几张租约"。集群资源视图属于 raylet，不属于驱动。

### 依赖解析为什么是主阻塞点

看 [dependency_resolver.cc](https://github.com/ray-project/ray/blob/d3febe7397071c9993aafc1b33a0d5059ea2cc4c/src/ray/core_worker/dependency_resolver.cc#L97) 的三条分支，能看出 Ray 把依赖分成三种处理方式：

| 依赖形态 | 处理路径 | 是否阻塞 |
| --- | --- | --- |
| 无依赖 | 同步调用回调 | 不阻塞，立即进入后续流程 |
| 普通对象 | 先查本地是否存在，不存在则异步等待 | 阻塞到对象可本地获取 |
| Actor 句柄 | 等待 Actor 注册完成 | 阻塞到 Actor 可用 |

零依赖任务走同步路径，这是热路径上的优化：常见的纯计算任务不会被塞进异步队列绕一圈。而对象依赖的"先查本地、再异步等"，直接决定了后面一条重要性质——**任务能不能进入队列，取决于它的输入对象在哪里，而不取决于集群里有没有空闲算力**。这解释了一个我在实际使用中反复遇到的困惑：任务一直显示等待，但 `ray.nodes()` 显示资源充足。此时去看看它等的那个引用，往往比去看资源更有效。

### 队列身份由依赖决定，不是由函数决定

`SchedulingKey` 的构造值得单独说。它包含调度类、运行时环境哈希，**还包含该任务未解析依赖的 ID 列表**：

```cpp
const SchedulingKey scheduling_key(task_spec.GetSchedulingClass(),
                                  task_spec.GetDependencies(),
                                  task_spec.GetRuntimeEnvHash());
```

这行在 [normal_task_submitter.cc](https://github.com/ray-project/ray/blob/d3febe7397071c9993aafc1b33a0d5059ea2cc4c/src/ray/core_worker/task_submission/normal_task_submitter.cc#L67) 附近。把依赖 ID 放进队列身份里，后果是同一个函数、同样的资源需求，只要输入不同，就会被分进不同的队列，各自独立计账。

这不是实现细节，它直接决定节流的作用范围。如果我认为"同一个函数的所有调用共享一个租约额度"，在依赖各不相同的负载上就会判断错。反过来说，这也在避免一类错误：把其中一个函数调用绑到某个节点的数据上，不该因此让另一个函数调用也失去机会。

### 四道闸门与节流的真实形态

`RequestNewWorkerIfNeeded` 的四道闸门在 [同一文件](https://github.com/ray-project/ray/blob/d3febe7397071c9993aafc1b33a0d5059ea2cc4c/src/ray/core_worker/task_submission/normal_task_submitter.cc#L260) 里。第一道是节流：当某类在途租约数达到 `RAY_max_pending_lease_requests_per_scheduling_category`，直接返回，不发起任何请求。日志用的是每十秒一条的限频告警，措辞很直白：

```text
Task submission is being throttled: pending lease requests (1) reached limit (1),
with 1 tasks waiting for lease requests.
This especially affects workloads submitting many tasks of the same type
to a cluster with few nodes but many cores per node.
```

配置名里那句 `few nodes but many cores` 是维护者对失效场景的判断。单机多核时租约授予是毫秒级，节流几乎没有代价；跨节点扩容时，每要一台机器都要走一次真正的申请，节流才会现形。

实测两组。第一组 100 个 0.05 秒任务、上限 1：只出现一条告警，在途 1、上限 1、积压 1，提交耗时 0.281 秒、总耗时 1.826 秒。第二组 100 个 1 秒任务、4 核、上限 1：三条告警，时间戳间隔约 10 秒，积压依次报 1、59、19，总耗时 25.7 秒，与"每秒约 4 个任务"的理论值接近。

积压峰值 59 出现在第二个采样点而不是最后一个，原因在限频宏本身：`RAY_LOG_EVERY_MS` 用函数内静态时间戳做采样，首条必打，之后每十秒一条。真实峰值可能落两个采样点之间而未被记录。这也是我最初的一个误判——我以为 19 是峰值，用 19 去反推队列行为，结论自然对不上；改成用总耗时反推才算自洽。

### 判断：调度是分散决策的

把三段串起来看，Ray 的调度是**分散决策**的：每个驱动各自维护自己那几类任务的欠账，每个节点（raylet）只对自己掌握的资源做授予或拒绝，没有任何进程持有全局最优解。

这个选择带来两件事。

好的那件是扩展性和异构性。因为不存在全局调度器，加节点不需要通知任何人，驱动也不需要理解集群拓扑；短任务、长驻 Actor、流式任务可以共存，因为它们各自走各自的账本。

坏的那件是没有全局优化。假设集群里有 10 个节点、驱动提交了三种任务，Ray 不会像 MPP 引擎那样把它们编排成一个阶段序列去最小化数据搬运。它只会一张一张地要租约。当数据局部性很重要时，用户必须自己用亲和性策略（如 `NodeAffinitySchedulingStrategy`）把选择压回去——这是把优化的责任交给了调用方。

对比经典的阶段式 MPP 批处理引擎（如 Spark、Trino 等）：在这类引擎里，调度单位是阶段（stage），协调者有全量视图，可以按分区数算并行度、按代价选连接顺序。Ray 的调度单位是**一次租约**，视图是局部的。这个差异决定了后面所有关于 Ray Data 的结论。

## 链路二：对象在哪里，谁说了算

第二个问题来自一个很朴素的观察：如果把对象想成分布式存储里的数据块，那么它应该有多副本、有位置元数据服务、有路由层。Ray 有对象存储（Object Store，基于 Plasma 的共享内存），有位置查询接口 `get_object_locations`，看起来对得上。

13 号实验就是为这个怀疑设计的：在 worker 节点上产生一个 8 MB 对象，不消费，然后逐级观察。

| 阶段 | 动作 | `node_ids` | 副本数 | 关键观测 |
| --- | --- | --- | --- | --- |
| 1 | 在 worker 上产生，不消费 | 只有 worker | 1 | 位置就在生产者节点 |
| 2 | 同节点 3 个并发任务消费同一对象 | 只有 worker | 1 | 消费进程只有 2 个，进程复用不改变副本数 |
| 3 | 驱动（在 head 上）取值 | head + worker | 2 | head 的 Plasma 从 7 MiB 涨到 15 MiB，增量 8 MiB |
| 4 | 删除引用并触发回收 | 无 | 0 | head 的 Plasma 从 15 MiB 回落到 0 MiB |

四行数据把三个机制钉住了：默认单副本、副本粒度是节点而不是任务、复制由消费方拉取触发。

第三行最能说明问题。驱动没有"请求复制"，它只是取值，取值本身引发了拉取。第四行说明了另一半：删除引用后不是"标记删除"，而是引用计数归零后各节点直接释放。

### 位置信息不来自全局控制存储

这是我最容易被误导的一处，也值得写清楚，因为很多二手材料会说"对象位置元数据存在 GCS"。

看 [ownership_object_directory.cc](https://github.com/ray-project/ray/blob/d3febe7397071c9993aafc1b33a0d5059ea2cc4c/src/ray/object_manager/ownership_object_directory.cc#L381)，发布 `WORKER_OBJECT_LOCATIONS_CHANNEL` 的是**对象所有者（owner）进程里的对象目录**，不是全局控制存储（Global Control Store, GCS）。GCS 那边发布的是 Actor、Job、节点信息、worker 增量、节点地址与存活这些通道。

这个分工在语义上说得通：对象的所有权属于创建它的 worker，位置随所有权走；GCS 只需要管"谁是谁、谁还活着"。如果反过来让 GCS 记录每个对象的位置，GCS 就会变成所有数据传输的热点，与它作为控制面的定位冲突。

GCS 自己的形状也支持这个判断。它是单进程、单实例、多线程的：内部按用途拆了 8 个独立的事件循环（`task`、`pubsub`、可观测性 `pubsub`、`ray_syncer`、`ray_event`、`internal_kv`、`node_manager`、资源负载拉取），见 [gcs_server_io_context_policy.h](https://github.com/ray-project/ray/blob/d3febe7397071c9993aafc1b33a0d5059ea2cc4c/src/ray/gcs/gcs_server_io_context_policy.h#L80)。其中只有 4 个参与健康检查，`task_io_context` 被显式排除在健康检查之外，代码注释写得很明白：

```text
task_io_context only runs GcsTaskManager, which ingests and serves
task-state events (observability) and drops events under load by design.
It is not on the GCS control plane, so a backlog here
(e.g. under a task-event flood) must not flip the server to NOT_SERVING.
```

这是一条值得记住的工程判断：**可观测性流量不得影响控制面可用性**。任务事件洪水是可预期的（大量短任务），如果它能把 GCS 打成就绪探针失败，KubeRay 会重启一个其实健康的进程，故障被放大。把这类通道从健康检查里摘出去，比给它扩容更省事。

存储后端也值得说一句。[gcs_server.h](https://github.com/ray-project/ray/blob/d3febe7397071c9993aafc1b33a0d5059ea2cc4c/src/ray/gcs/gcs_server.h#L130) 里 `StorageType` 有四档：未知、纯内存、Redis 持久、RocksDB 持久，默认是纯内存。含义很直接：**GCS 重启，元数据就没了**，除非显式配置持久后端。这决定了 Ray 的容错边界——它能容忍 worker 失败，不能默认可容忍控制面重启。

### 所有权、引用计数与失效路径

把链路二收束成一条因果链：

```text
worker A 创建对象 obj
  → A 成为 obj 的 owner，持有唯一权威的引用计数
  → A 的本地点对点目录向订阅者广播"obj 在哪里"
  → B 取值：B 向 owner 查询位置，然后从 A 拉一份副本（+1 份）
  → 所有持有者释放引用
  → owner 侧计数归零 → 通知各节点删除 → 数据消失
```

这条链上每个环节都有对应的失效模式，而它们的严重程度差别很大：

| 失效点 | 后果 | 可恢复性 |
| --- | --- | --- |
| 消费方死掉 | 副本减少 | 可从 owner 重新拉取 |
| 非 owner 节点死掉 | 该节点副本消失 | 可从 owner 重新拉取，代价是重传 |
| owner 死掉且对象仍有引用 | 所有权丢失 | 无 Task 可重算时数据不可恢复 |

第三行是 Ray 与 Spark 分野最深的地方。Spark 的分区丢失可以由血缘重算，因为分区是**从数据源确定性推导**出来的。Ray 的对象可以由任意副作用产生——一个加载了模型的 Actor 的中间结果、一个正在更新的参数的快照。这类对象没有可重放的推导过程，owner 一死就真的没了。

所以"Ray 是分布式存储"这个说法要拆开纠正：它有存储组件（Plasma），但语义是**缓存加所有权**，不是持久化副本。把状态放进去而不做检查点，是把自己暴露在不可恢复的失败上。05 号实验给了一个小尺度的对照：任务进程被杀死后重试，PID 从 109208 变成 109209，结果照常返回；Actor 则依赖持久化恢复，提交过（committed）的值 5 回来了，没提交的 7 永久丢失。

### 顺带纠正两个 API 层面的误解

第一个是 `get_object_locations` 的键。它返回的字典键是 `ObjectRef` 对象本身，不是 `ref.binary()` 也不是十六进制字符串。用错键会得到 `KeyError`，而错误信息不会告诉你"键的类型错了"。

第二个是观测对象释放的手段。Ray 3.0.0.dev0 里 `ray.internal.free` 与 `ray._private.internal_api.free` 都已移除，无法主动释放对象。13 号实验只能用"删除引用加触发回收，再比较 Plasma 用量"的间接方式观测释放。这也暴露了一个真实的观测缺口：**对象一旦释放就没有句柄了，位置查询和内存统计都失去锚点**。这是接口设计的必然结果，不是缺陷，但它意味着排查内存问题时必须提前留好观测点。

## 链路三：扩容预算先于扩容需求

前两条链路的结论都指向同一个方向：Ray 的运行时是分散的、局部的。但集群总得有人知道"要几台机器、什么形状"，这部分归自动扩缩容器（autoscaler）。第一版实现是脚本式的，第二版重写成了纯函数式的需求规划器，代码在 [scheduler.py](https://github.com/ray-project/ray/blob/d3febe7397071c9993aafc1b33a0d5059ea2cc4c/python/ray/autoscaler/v2/scheduler.py#L1181)。

它的一次决策就是一个函数 `schedule()`，内部按固定顺序跑八个阶段：

```text
 1. _terminate_outdated_nodes        清理过时节点
 2. _enforce_min_workers_per_type    每类型最小节点数
 3. _enforce_max_workers_per_type    每类型最大节点数
 4. _enforce_max_workers_global      全局最大节点数
 5. _enforce_resource_constraints    集群资源形状约束
 6. _sched_gang_resource_requests    成组需求（要么全给，要么全不给）
 7. _sched_resource_requests         普通需求（任务与 Actor）
 8. _enforce_idle_termination        回收空闲节点
 → 返回 to_launch / to_terminate / to_ippr / infeasible_*
```

这个顺序不是随手排的，它体现了三条不变量。

**第一，预算先于需求。** 最小/最大节点数、资源形状约束都在普通需求之前处理。当需求与约束冲突时，被牺牲的是需求——它会被记进 `infeasible_resource_requests`，而不是让约束让步。这是一个明确的立场选择：**集群形状是输入，不是结果**。

**第二，顺序本身有依赖，不能重排。** 每类节点的可用容量由"该类型最大节点数减去现有实例数"算出，而这个统计**并不过滤已标记终止的实例**。所以第 3 阶段必须跑在第 5 阶段之前，否则可用容量会算出负数，直接把后续阶段带崩。这类隐式时序依赖在代码里没有断言保护，是维护者靠顺序注释维持的契约。

**第三，gang 需求先于普通需求。** 成组需求（比如一组要一起启动的训练进程）必须整体满足，放在普通需求前面才不会被零散需求切碎容量。

### 容量为什么可以"提前兑现"

读 `SchedulingNode` 的构造会发现一件反直觉的事：同样叫"可用资源"，含义取决于节点状态。

| 节点状态 | 容量来源 | 语义 |
| --- | --- | --- |
| 已进入 RAY_RUNNING | GCS 上报的真实剩余资源 | 实际可放置 |
| REQUESTED / ALLOCATED / RAY_INSTALLING | 节点配置里的全量规格 | 尚未就绪但被提前兑现 |

对尚未启动的实例直接赋予全量容量，见 [scheduler.py 的 `from_node_config`](https://github.com/ray-project/ray/blob/d3febe7397071c9993aafc1b33a0d5059ea2cc4c/python/ray/autoscaler/v2/scheduler.py#L537) 与 `is_ray_running_reachable` 判断。这样做的动机是防止重复下单：如果规划器只看真实资源，就会在"机器已申请但还没启动"的窗口里为同一批需求再申请一批机器，形成雪崩式超配。

代价是规划结果只是**当前可执行性的下界**，不是最终状态的精确预测。一个浅显的推论：如果你在扩容爬坡期间反复读 `to_launch`，会看到它逐渐变小而不是接近某个稳定值——因为每轮都在把已申请但未就绪的容量兑现掉。

同一份代码里还有一处更细的隔离。每个节点维护两份可用量账本：

```python
self.available_resources_for_sched = {
    ResourceRequestSource.PENDING_DEMAND: dict(available_resources),   # 真实剩余
    ResourceRequestSource.CLUSTER_RESOURCE_CONSTRAINT: dict(total_resources),  # 全量
}
```

约束试算用后者，普通需求用前者。于是"试算集群形状"这个动作不会挤占真实需求的容量空间。这条不变量没有写在文档里，只能从两份账本的初始化看出来。

### 三段式放置与装箱偏好

单个需求的放置顺序在 `_try_schedule` 里，严格三段：

1. 先填**已有节点**的空余容量；
2. 其次对可原地扩容的节点做**IPPR**（原地 Pod 资源扩容）；
3. 最后才申请**新节点**。

打分函数 `_compute_score` 返回五元组：标签匹配优先级、是否 GPU 节点匹配、参与调度的资源类型数、**各资源类型利用率的最小值**、**平均值**。实现里对每个资源类型累加 `total × util³`，这个三次幂是刻意的：利用率从 50% 提到 90% 带来的分数增益远大于从 10% 提到 50%。

结果是装箱（binpack）而不是打散（spread）：**任务会主动集中到少数节点上**。看到集群里节点利用率呈"几个满、几个空"的形态，不要先当成故障，这是打分函数的偏好。它对批处理有利（空节点可以被整体回收），对延迟敏感服务不利（热点集中）。

### 实验 06 把"预算优先"量化为四行

06 号实验直接调用规划器，只在内存里模拟节点状态，不申请任何云主机。四组输入的结果：

| 场景 | 结果 | 说明 |
| --- | --- | --- |
| 三个各需 1 台的需求 | 申请 3 台，无不可行 | 需求都能满足时按需下单 |
| 需求落在已有节点容量内 | 申请 0 台，无不可行 | 先填已有节点 |
| 每类型上限 2、需求要 3 台 | 申请 2 台，1 个不可行 | 上限优先，需求被截断并记账 |
| 单请求要 2 GPU、节点只有 1 GPU | 申请 0 台，1 个不可行 | 结构性不可行，不浪费申请 |

第四行值得单独说。规划器识别出"单个请求的规格超过单节点规格"，直接判为不可行，而不是申请一堆单 GPU 节点然后让调度器永远等。这个判断必须发生在**规格层**，不能留给运行时——运行时只看到"资源不足"，会一直挂着。

实验还验证了九态状态机（QUEUED → REQUESTED → ALLOCATED → RAY_RUNNING → RAY_STOP_REQUESTED → RAY_STOPPING → RAY_STOPPED → TERMINATING → TERMINATED）会拒绝非法转移。规划器只在"规划可满足"这一层工作，容量真正兑现要靠状态机逐级推进——这也解释了为什么 `to_launch` 里只有状态为 TO_LAUNCH 的项，而 `to_ippr`、`to_terminate` 是并行的另外两路。

`infeasible_resource_requests` 这个字段本身有个使用陷阱：它把三种根因相反的失败混在同一个列表里——结构性不可行（单请求超单节点规格）、策略性受限（被最大节点数挡住）、外部受限（云配额不足）。看这个列表时必须回到日志分辨是哪一种，否则会把"配额不够"误诊成"需求不合理"。

## Ray Data：把 MPP 引擎的能力做成库，代价是什么

到这一步可以回答一个更实际的问题：拿 Ray Data 去写批处理，和拿 MPP 引擎去写，差别在哪里。

先说结论：**Ray Data 没有"阶段"这个调度单元**。MPP 引擎里阶段边界同时是并行度边界、故障重算边界和资源分配边界；Ray Data 只有算子，融合规则会把这三条边界撕开，各自由不同的机制决定。

### 计划分四段，但物理计划不做代价搜索

`explain_plan` 输出四段：逻辑计划、逻辑优化、物理计划、物理优化。逻辑到物理的转换是一张类型路由表，见 [planner.py](https://github.com/ray-project/ray/blob/d3febe7397071c9993aafc1b33a0d5059ea2cc4c/python/ray/data/_internal/planner/planner.py#L253-L273)：

```python
_DEFAULT_PLAN_FNS = {
    Read: plan_read_op,
    Filter: plan_filter_op,
    AbstractUDFMap: plan_udf_map_op,
    AbstractAllToAll: plan_all_to_all_op,
    Join: plan_join_op,
    Limit: plan_limit_op,
    ...
}
```

路由表的含义是：**给定逻辑算子类型，就决定物理算子类型，不看数据量、不看代价**。没有连接顺序搜索，没有实现选择，没有基于统计信息的决策。默认 shuffle 策略是哈希 shuffle，所以 `groupby().agg()` 直接走 `HashAggregateOperator`，根本不经过 sort-based 的 pull/push 调度器。

这解释了为什么 Ray Data 的计划看起来"很稳定"：同样的 API 链几乎总是翻译成同样的物理算子，因为决策点只有类型。

### 融合把三个边界解耦了

09 号实验实测，`read → filter → map` 三段被融合成一个物理算子：

```text
TaskPoolMapOperator[ReadRange->Filter(<lambda>)->Map(<lambda>)]
```

09 号实验还验证了 `repartition` 是硬边界：加进去之后融合立即断成两个算子。这条性质在 MPP 引擎里是不存在的——引擎的算子融合发生在阶段内部，阶段本身不会因为"多了一个重分区"而改变。

融合带来两个真实收益，都在 09 号实验里量化过：

| 观测 | 融合后 | 对照 |
| --- | --- | --- |
| 物理算子树中的算子数 | 1 | 3（加 repartition 后断开） |
| 下游 task 数随分区数变化 | 8 个输入块 → 下游 2 或 4 个 task | 上游 task 数保持 8 |
| 流水线时间区间重叠 | 6 对算子区间相交 | 串行下界 1.6 秒，实际 1.214 秒 |

第三行的读法要小心：1.214 秒小于 1.6 秒的串行下界，只能证明**单机上两个算子确实在同时跑**，不能推导出跨节点线性加速。

### 融合的边界是数据契约，不是性能模型

融合什么时候被拒绝？看 [operator_fusion.py](https://github.com/ray-project/ray/blob/d3febe7397071c9993aafc1b33a0d5059ea2cc4c/python/ray/data/_internal/logical/rules/operator_fusion.py#L838-L856) 的判据：

```python
if (
    upstream_op.can_modify_num_rows
    and not isinstance(upstream_op, MapBatches)
) and downstream_op.min_rows_per_bundled_input is not None:
    logger.debug(... "Skipping fusion")
    return False
```

判断依据是**行数契约**：上游会不会改变行数、下游有没有"每批至少多少行"的要求。如果上游会改行数而下游有批大小要求，融合就可能破坏下游对批内容的假设，于是拒绝。代码注释里还有一个例外——`MapBatches` 被显式豁免，理由是要保持"历史上一直被融合"的行为，避免回归。

这是我认为最值得学的一处设计：**融合决策的理由是语义正确性，不是性能**。没有代价模型说"融合能省 X 微秒"，只有"融合会不会让下游看到它不该看到的批次"。

### 我修正过一次关于 task 与 block 的理解

一开始我以为"一个 task 处理一个 block"，因为早期实验里 task 数正好等于 block 数。这个理解只在 `batch_size=None` 时成立。

起因是 03 号实验的统计对不上：进度条报 16 个 task，但落盘的 Parquet 里数出 32 次 UDF 调用。为了搞清这件事，写了一个探针，把 `TaskContext.task_idx` 打出来，在 1000 行、固定 10 个 block 的数据集上扫了一遍不同的 `batch_size`：

| batch_size | task 数 | UDF 调用数 | 每 task 消耗的 block 数 |
| --- | --- | --- | --- |
| None | 10 | 10 | 1 |
| 50 | 10 | 20 | 1（内部再切两批） |
| 150 | 5 | 10 | 2（切出 150 + 50） |
| 250 | 4 | 7 | 3（共 300 行） |
| 3000 | 1 | 1 | 10（全部） |

机制在 [dataset.py](https://github.com/ray-project/ray/blob/d3febe7397071c9993aafc1b33a0d5059ea2cc4c/python/ray/data/dataset.py#L967-L969)：`batch_size` 是整数时被当作 `min_rows_per_bundled_input` 传给执行层；打包队列为了凑够行数，会把所有待处理的 bundle 合并成一个，于是单个 task 的输入签名变成了 `*blocks: Block`。

结论要改成两句话：**一批可以跨 block，但绝不会跨 task**。批（batch）是在 task 内部生成的，看不见别的 task 的 block。而整数 `batch_size` 还有第二个副作用——它让下游的 `min_rows_per_bundled_input` 非空，于是按上一节的判据，`ReadRange → MapBatches` 的融合被打断，实测从 1 个物理算子变成 2 个，多了一次 block 写入对象存储再读回。

这是一个典型的"参数选了性能，却付了计划层代价"的例子。调 `batch_size` 时不能只看批大小对 UDF 的影响，还要看它是否改变了物理计划。

### 并行度是算子级的，不是阶段级的

09 号实验的对照最能说明差异：

| 场景 | 上游 task | 下游 task |
| --- | --- | --- |
| 8 个输入块、重分区成 2 份 | 8 | 2 |
| 8 个输入块、重分区成 4 份 | 8 | 4 |

上游按自己的块数并行，下游按分区数并行，两者互不约束。MPP 引擎里同一个阶段的所有算子共享一个并行度，因为阶段是一个调度单元。Ray Data 允许"上游 8 个、下游 2 个"这种不对称，收益是不需要为凑并行度而人为拆分数据，代价是没有全局的负载均衡视图。

09 号实验还留了另一组对照，回答一个更实用的问题：同一个 UDF 写成函数还是写成类，到底改变了什么。

| 写法 | 物理算子 | 进程数 | 单进程最多接的批 | 实例内计数器最大值 |
| --- | --- | --- | --- | --- |
| 函数 | `TaskPoolMapOperator` | 1 | 32 | 1 |
| 类 | `ActorPoolMapOperator` | 2 | 16 | 16 |

写成函数时，UDF 在每个任务里重新构造，所以计数器永远是 1——32 个批可能都复用同一个进程，但每次调用都是新对象。写成类时，类实例跟着 Actor 常驻，计数器累积到 16，状态真的跨批保留了下来。

这条差异的实际价值：模型加载、分词器初始化这类初始化贵、调用便宜的工作必须写成类；纯转换写成函数更省资源，因为没有常驻进程的占用。两种写法都叫 `map_batches`，但一个是有状态执行器，一个是无状态算子。

回到并行度这条线上。`ActorPoolStrategy(size=2)` 的两个 Actor 实例，实测一批负载出现 16:16 也出现 14:2 的分配。原因在 [select_actors](https://github.com/ray-project/ray/blob/d3febe7397071c9993aafc1b33a0d5059ea2cc4c/python/ray/data/_internal/execution/operators/actor_pool_map_operator.py#L1020)：它从小顶堆里挑当前任务数最少的 Actor（只窥视不弹出，由调用方在提交后更新计数）。单批在途上限为 1 时，分配顺序完全由完成顺序决定，先完成的 Actor 拿到下一个任务，形成正反馈。这个推断没有加日志追踪，属于源码级推导而非实测结论。

### 反压是可见的，方向有两个

04 号实验把反压做成了可观测的东西：快的生产者产生约 64 MiB 中间数据，慢消费者处理，把对象存储调度预算压到 16 MiB。

| 指标 | 数值 | 含义 |
| --- | --- | --- |
| 端到端反压时长 | 10.74 秒 | 生产被消费拖住的总时间 |
| ReadRange 的提交反压 | 4.48 秒 | 提交任务被预算挡住 |
| MapBatches 的提交反压 | 6.09 秒 | 同上，在执行算子侧 |
| MapBatches 的输出反压 | 1.98 秒 | 输出无处可放 |

指标定义在 [op_runtime_metrics.py](https://github.com/ray-project/ray/blob/d3febe7397071c9993aafc1b33a0d5059ea2cc4c/python/ray/data/_internal/execution/interfaces/op_runtime_metrics.py#L408-L437)。两个方向要分清：**提交反压**是"想提交但对象存储没预算"，**输出反压**是"算完了但下游不收"。看到反压时间大于零，说明预算或下游确实成了瓶颈；只看消费者睡得久，不构成反压证据。

### 五条 shuffle 路径与一个不能自愈的问题

10 号实验枚举了五条路径：

| 写法 | 物理算子 | 说明 |
| --- | --- | --- |
| 按 key 重分区 | `HashShuffleOperator` | 直接按哈希分区 |
| `groupby().agg()` | `HashAggregateOperator` | 哈希聚合，不走 sort 调度器 |
| 无 key 重分区 | `AllToAllOperator[Repartition]` | 统一 all-to-all 包装 |
| `sort` | `AllToAllOperator[Sort]` | 同上，无专属算子类 |
| 随机打散 | `AllToAllOperator[ReadRange->RandomShuffle]` | 融合进上游 |

两个反直觉事实值得记下。第一，`Sort`、`RandomShuffle` 和**不带 key** 的 `Repartition` 都没有专属物理算子类，全部是 `AllToAllOperator` 包一个转换函数——所以你在代码里搜 `SortOperator` 是搜不到的。第二，**分区数不等于输出块数**：8 个输入块重分区成 3 份，输出就是 3 个块；16 个块重分区成 3 份，输出还是 3 个块。分区数决定上限，空分区不产出。

同一个实验给了一个负面结论：倾斜不会自愈。同一份带热键的数据，4 个分区和 8 个分区下最大分区占比都是 0.957。增加分区数不会打散一个天然的热键，因为哈希把同一个键永远送到同一个分区。这与 MPP 引擎的行为一致，但和很多人对"加并行度就能解决倾斜"的直觉相反。

### pull 与 push：driver 侧引用的数量级差异

sort-based exchange 有两种调度器，真正的差异在**谁持有引用**。

pull-based 的实现在 [pull_based_shuffle_task_scheduler.py](https://github.com/ray-project/ray/blob/d3febe7397071c9993aafc1b33a0d5059ea2cc4c/python/ray/data/_internal/planner/exchange/pull_based_shuffle_task_scheduler.py#L54)：

```python
caller_memory_usage = (
    input_num_blocks * output_num_blocks * CALLER_MEMORY_USAGE_PER_OBJECT_REF
)
```

公式的形状就是结论：driver 需要持有 `N × R` 个引用，N 是输入块数、R 是输出分区数。每个 reduce 任务要自己知道去哪些 map 输出里取数，所以引用必须全部先造出来放在 driver 上。代码还带了自适应告警，超过阈值就建议改用 push-based。

push-based 用多轮 map + merge 的流水替代，merge 任务用 `NodeAffinitySchedulingStrategy(soft=True)` 绑定到节点，引用数量级降到 `N + N·N' + R`（N' 是每轮 merge 的输出数），默认 merge 因子是 2，见 [push_based_shuffle_task_scheduler.py](https://github.com/ray-project/ray/blob/d3febe7397071c9993aafc1b33a0d5059ea2cc4c/python/ray/data/_internal/planner/exchange/push_based_shuffle_task_scheduler.py#L456)。

代价也写在同一份代码里：pull-based 有一个"取完才能继续"的全局屏障，push-based 把它换成多轮局部屏障，换来的是多一次数据搬运。**这是拿带宽换 driver 内存的典型折中**，量级随 N×R 增长，所以小数据上 push 反而更慢。

顺带一个只有动手才会发现的代价：11 号实验里，48 条记录的多模态数据，宽表直接 shuffle 的输入是 130454 字节，先窄化到只剩文本列再 shuffle 是 1184 字节，差 110.2 倍。列裁剪在进 shuffle 之前做还是之后做，不是优化技巧，是数量级问题。

### 判断：并行度、故障与资源三个边界被解耦

把 Ray Data 与 MPP 引擎对位，差别集中在一张表里：

| 阶段 | 阶段式 MPP 引擎 | Ray Data |
| --- | --- | --- |
| 逻辑计划 | SQL 抽象语法树转逻辑算子 | Python API 链，`explain_plan` 第一段 |
| 规则优化 | 谓词下推、列裁剪、常量折叠 | 六条规则：Limit/Projection/Predicate 下推、CombineShuffles、CombineDownloads、PushdownCountFiles |
| 物理计划 | 按代价选连接顺序与算子实现 | 类型路由表，无代价搜索 |
| 物理优化 | 阶段内算子打包进同一实例 | 四条规则，`FuseOperators` 把连续 map 合并成一个 task 函数 |
| 并行度 | 由分区数决定整个阶段 | 算子级，每个算子按自己的块数并行 |
| 故障单位 | 阶段内的任务，可整体重算 | 单个算子任务，融合后重算范围变大 |

最后一行是我最在意的差异。融合把三个算子变成一个物理算子，意味着这个物理算子失败时，三个算子的逻辑要一起重跑。MPP 引擎的阶段边界天然是重算边界，Ray Data 的重算边界被融合规则动态决定了——计划看起来更紧凑，失败时的重算代价却不那么直观可算。

## 三个补充证据：资源记账、状态驻留与幂等重放

前面三条链路关注调度。还有三个实验的结论不属于任何一条链路，但恰好补上了三个常被忽略的机制。

### 资源声明是记账，不是隔离

02 号实验声明一个逻辑 GPU，然后启动三个各需半个 GPU 的 Actor。按“隔离”的直觉，三个里面有且只有两个能跑，且它们该拿到不同的设备。实测结果：

| 观察点 | 结果 |
| --- | --- |
| 前两个 Actor 的 `gpu_ids` | 都是 `[0]` |
| 前两个的 `CUDA_VISIBLE_DEVICES` | 都是 `0` |
| 第三个 Actor | 等待，直到有资源释放 |
| 释放后的新 Actor | 拿到 `gpu_ids=[0]`，PID 175102 |

前两行说明：`num_gpus=0.5` 在 Ray 里是**一份可重叠的记账额度**，不是物理设备切片。两个 Actor 都看到同一张卡，如果它们真去跑 CUDA，会互相抢占显存。Ray 只保证“总账不超卖”，不保证“物理上互不可见”。

这条结论对从引擎转过来的人尤其重要。引擎里的资源是预先划分的真实池子，声明什么就真拿到什么；Ray 里的声明是**调度契约**，它决定你能不能开始，不决定你开始以后能不能独占。真要隔离，得靠容器、靠 `CUDA_VISIBLE_DEVICES` 这类外部机制自己实现。

### Actor 的价值在于数据不动，只交换摘要

07 号实验用三个 Actor 各持三分之一的向量分片跑 KMeans：12000 行、8 维、4 簇、3 分片、2 轮收敛，结果与单进程串行实现完全一致（中心点和标签都一致）。真正值得看的是流量结构：

| 指标 | 数值 |
| --- | --- |
| 输入数值载荷 | 768000 字节 |
| 每轮汇总的逻辑载荷 | 1656 字节 |
| 比值 | 约 1/464 |

每轮只需把“部分和与计数”送回驱动，中心点更新后再发回去，原始分片始终留在原进程里。这是 Actor 最本质的用途：**把数据固定在进程里，让算法迁就数据位置**。任务模型做不到这一点，因为任务是无状态的，每次调用都重新传参。

同时要注意三份分片的实例 ID 跨两轮完全不变，说明 Actor 是进程级的状态容器。它不会因为一轮结束而被回收，这也是它比任务贵的原因——贵在占用一直存在。

### 重试能成立，靠的是幂等而不是重试本身

08 号实验把“确定性拼接 + 幂等写入”放在了失败路径上检验：先清洗 64 篇文档（保留 58 篇）、切成固定长度 64 的样本共 53 条 3239 个词元；然后故意杀掉写入分片的 Worker（PID 31800），观察重试进程 31810 能否把结果补齐。

| 校验项 | 结果 |
| --- | --- |
| 重放的分片数 | 4 |
| 与串行参照一致 | 是 |
| 逐词元无重复无遗漏 | 是 |

三行都是“是”，但原因不在重试机制。重试只是把失败的任务再跑一遍，能不能得到正确结果，取决于两件事：分片规则是确定的（同一份输入永远落到同一个分片），以及写入是幂等的（同一个分片重写一遍不会变成两份）。没有这两条，重试只会把错误忠实地重复一遍。

这也是从数据库世界带过来的经验里最值得强调的一条：**容错不是调度器的属性，是算子自身的属性**。引擎里我们叫做“可重入”或“幂等提交”，在 Ray 里换了个名词叫任务重试，底层要求完全一样。

## 把 Ray 放回系统谱系

如果说前面的分析都是"Ray 内部怎么运作"，这一节换个方向：把 Ray 与几个常被拿来比较的系统放在同一组问题上。

Ray 与 Spark 面对的不是同一类负载。Spark 的核心假设是"一次提交、一个有向无环图（Directed Acyclic Graph, DAG）、跑完就结束"，所以它可以把调度做得中心化、把 shuffle 落盘、把失败恢复交给血缘。Ray 的核心假设是"长期存活的进程 + 频繁的小步骤交互"——强化学习里一次训练要跑几千个短步骤、推理服务要常驻模型、Actor 要跨调用保存状态。这些负载在 Spark 的模型里很难表达，不是性能问题，是形状不匹配。

| 维度 | Ray | Spark | Dask | Daft | MPP 引擎 | Kubernetes |
| --- | --- | --- | --- | --- | --- | --- |
| 调度单元 | 一次资源租约 | 一个阶段的任务集 | 一个任务图节点 | 一个分区任务 | 一个阶段 | 一个 Pod |
| 并行度决定者 | 每个算子自己的块数 | 分区数 | 图节点与分区 | 分区与执行器容量 | 分区数 | 资源请求 |
| 数据交换 | 对象存储按需拉取 | shuffle 落盘 | 内存与磁盘 | 箭头格式分区 | shuffle 服务 | 不涉及 |
| 故障恢复 | 任务重试、Actor 重启、血缘重算 | 血缘重算 | 任务重试、图重算 | 任务重试 | 阶段重算 | 重建 Pod |
| 资源模型 | 逻辑记账，由 raylet 授予 | 执行器槽位 | 工作进程 | 执行器容量账本 | 配额与队列 | 请求与限制 |
| 全局优化器 | 无 | 有（阶段级） | 无 | 有（分布式计划） | 有（代价模型） | 不适用 |

这张表里最有信息量的一列是"全局优化器"。Ray 没有，而且不是没做完，是设计取舍：因为调度决策被下推给了每个驱动，没有任何进程有足够信息做全局优化。换来的是异构负载共存和任意长度的交互式执行。

我特意把 Kubernetes 也放进表里，因为"Daft 把任务交给 Ray Actor、Kubernetes 给 Pod 选节点"这个多层关系容易混淆。三个层次各自约束不同对象：Kubernetes 决定 Pod 去哪个节点，Ray 决定 Actor 去哪个 Ray 节点，Daft 决定任务去哪个已有 Actor。增加数据任务数量不会必然增加 Actor 或 Pod 数量——这是分层调度最容易被误读的地方。

## 能力转移：哪些经验可以搬，哪些必须重学

我原来是做数据库引擎的，把这次学习里真正能迁移的部分记下来，比记住 API 更有用。

**可以直接搬的：规则优化。** 谓词下推、列裁剪、常量折叠这些规则的形态与引擎无关。Ray Data 的六条逻辑规则和 MPP 引擎的规则集在结构上是同构的，只是作用对象从关系代数换成了 Python 算子链。11 号实验里列裁剪带来 96.6 倍体积差，这个数量级和列存引擎里的经验完全一致。

**需要改造的：并行度。** 引擎里并行度是阶段级的，改一个地方影响整个阶段；Ray Data 里每个算子各算各的。从引擎转过来的人第一反应是"为什么上下游并行度不一致"，第二反应是"这样会不会浪费"。答案是不浪费，但需要换一个思维模型：这里没有"阶段"这个整体，只有一串各自为政的算子。

**必须重学的：代价模型与全局视图。** 引擎里的经验是"有了统计信息和代价模型，就能选对连接顺序和算子实现"。Ray Data 没有这个层，它只有类型路由。这意味着在 Ray 上做优化，方向不是"让优化器选得更好"，而是"让数据形状更适合默认计划"——提前窄化、控制块大小、在合适的位置放重分区。

**反向也能搬的三件事。** 第一，把反压做成一等指标。Ray Data 的 `task_submission_backpressure_time` 和 `task_output_backpressure_time` 区分了两个方向，这个区分在引擎的算子监控里往往被合并成"等待时间"，排查时损失了信息。第二，按需拉取替代主动推送。引擎的 shuffle 默认是写出去，Ray 默认是消费方来拉，省掉了"写到哪里"的决策，代价是消费方要能承担拉取延迟。第三，幂等的声明式重算。autoscaler v2 每轮从完整状态重新算出结果，不维护增量状态；这种设计的好处是没有状态不一致，坏处是每轮都是全量计算——在节点数上千时这就是成本。

## 适用边界与尚未验证的部分

必须交代清楚这份材料能支撑什么、不能支撑什么。

已验证的：单机 4 核、无 GPU、对象存储 256 MiB 环境下的任务提交、依赖阻塞、租约节流、对象副本与释放、规划器的预算优先与三段式放置、Ray Data 的计划形状、融合边界、算子级并行度、反压方向、五条 shuffle 路径、列裁剪与窄化收益。

没有验证的，列出来以免被误引用：

- **跨节点性能**。13 号实验是同一台机器上的两个进程，只证明位置语义。任何关于网络带宽、跨节点延迟、spill 到远端对象存储的结论，这份材料都不支持。
- **扩容端到端**。06 号实验只在内存里模拟节点状态，没有调用云接口。真实扩容还要经过配额、镜像拉取、进程启动，规划器的输出只是其中第一步。
- **控制面故障恢复**。默认存储后端是纯内存，GCS 重启的后果是从源码推断的，没有做故障注入。
- **GPU 相关结论**。02 号实验只验证了逻辑 GPU 的记账与排队，没有 CUDA 执行，不能当作显存隔离的证据。
- **大规模下的调度行为**。所有实验的集群规模都不超过两个节点，节流、装箱、gang 调度在大规模下的表现都是源码级推导。

写作过程中我修正了两个判断，一并记下，因为它们代表了容易反复踩的两类坑。

第一类是**把参数当成独立旋钮**。`batch_size` 看起来只是控制批大小，实际上它同时改变了 task 数、改变了融合是否发生、改变了下游的最小行数契约。调一个参数要顺着计划看三处。

第二类是**把观测接口当成理所应当**。我原以为可以像引擎那样"主动释放对象然后观察回收"，实际上 Ray 3.0 已经移除了 `free` 这类接口，只能靠删除引用让计数自然归零。观测手段的形状会反过来限制你能提出的问题——这一点在引擎里也一样，只是引擎的监控更成熟，掩盖了这个事实。

## 源码索引

本文引用的位置都固定在 `d3febe7397071c9993aafc1b33a0d5059ea2cc4c`，换上你的本地分支后行号会漂移。

| 主题 | 位置 |
| --- | --- |
| 任务提交入口 | `src/ray/core_worker/core_worker.cc`，`SubmitTask`（L2056） |
| 任务规格构造 | 同上，`BuildCommonTaskSpec`（L1954） |
| 依赖解析与入队 | `src/ray/core_worker/task_submission/normal_task_submitter.cc`（L34 起） |
| 租约四道闸门 | 同上，`RequestNewWorkerIfNeeded`（L260 起） |
| 依赖解析器 | `src/ray/core_worker/dependency_resolver.cc`（L97 起） |
| 对象位置发布 | `src/ray/object_manager/ownership_object_directory.cc`（L381、L443） |
| GCS 存储后端 | `src/ray/gcs/gcs_server.h`，`StorageType`（L130） |
| GCS 事件循环拆分 | `src/ray/gcs/gcs_server_io_context_policy.h`（L80 起） |
| 扩容规划主流程 | `python/ray/autoscaler/v2/scheduler.py`，`schedule`（L1181 起） |
| 节点容量双语义 | 同上，`from_node_config`（L537）、`is_ray_running_reachable`（L531） |
| 放置打分 | 同上，`_compute_score`（L631 起） |
| 逻辑到物理路由 | `python/ray/data/_internal/planner/planner.py`（L253-L273） |
| 融合判据 | `python/ray/data/_internal/logical/rules/operator_fusion.py`（L838-L856） |
| 批大小到最小行数 | `python/ray/data/dataset.py`（L967-L969） |
| Actor 选择 | `python/ray/data/_internal/execution/operators/actor_pool_map_operator.py`（L1020 起） |
| 反压指标定义 | `python/ray/data/_internal/execution/interfaces/op_runtime_metrics.py`（L408-L437） |
| pull shuffle 引用估算 | `python/ray/data/_internal/planner/exchange/pull_based_shuffle_task_scheduler.py`（L54 起） |
| push shuffle 合并因子 | `python/ray/data/_internal/planner/exchange/push_based_shuffle_task_scheduler.py`（L456） |
| raylet 工作进程池 | `src/ray/raylet/worker_pool.h`，`PopWorker`（L217） |
| raylet 调度策略族 | `src/ray/raylet/scheduling/policy/`（hybrid / spread / node-affinity / bundle 等） |

如果只挑一处开始读，我建议从 `RequestNewWorkerIfNeeded` 的四道闸门入手。它把 Ray 调度的全部张力压在了四十行代码里：想跑得更快就多要租约，但账目、空闲 worker、队列长度和拓扑各有各的理由说不。理解这四道闸门为什么都必须存在，比读十个模块更能理解 Ray 为什么会呈现出现在这个形状。
