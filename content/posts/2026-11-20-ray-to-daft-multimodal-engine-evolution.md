---
title: "【调研】从 Ray 到 Daft：AI 原生多模态数据引擎的架构演进，以及与 Spark、StarRocks 的深层分水岭"
date: 2026-11-20T00:00:00+08:00
lastmod: 2026-11-20T00:00:00+08:00
slug: "ray-to-daft-multimodal-engine-evolution"
categories:
  - 分布式系统
  - 数据库
  - 大数据
tags:
  - Ray
  - Ray Data
  - Daft
  - Apache Spark
  - StarRocks
  - 多模态
  - AI Infra
  - 调研
description: "基于 Ray、Daft、Spark 与 StarRocks 本地源码，系统拆解从 Ray Data 到 Daft（Swordfish + Flotilla + Arrow Flight Shuffle）的架构跃迁，横向对照四类引擎的底层分水岭，并梳理开源与商业多模态 AI 数据栈的四代演进脉络。"
toc: true
math: true
---

> 关联阅读：[Ray：把对象当货币]({{< relref "2026-11-10-ray-object-as-currency-source-notes.md" >}}) · [StarRocks 架构与源码]({{< relref "2026-08-09-starrocks-architecture-and-internals.md" >}}) · [查询并行度的三次决策]({{< relref "2026-09-12-dive-dop.md" >}}) · [深入 Hash Join（含 Spark 附录）]({{< relref "2026-08-26-dive-hash-join.md" >}})

设想一条如今在多模态大模型团队里再普通不过的数据流水线：从 S3 扫描 100 万条 Parquet 索引行，每行带着一个对象存储 URL；先并发拉取原始图片、音频或 PDF 文件，再做解码、缩放、抽帧或分词，接着送入 GPU 集群跑 Whisper 转写、ResNet/CLIP 打 Embedding 或 vLLM 批量生成标注，最后做一次 MinHash 去重、关联业务元数据表，写回 Iceberg 或 Lance。

如果你试图把这条流水线直接丢进运行了十余年的 Apache Spark，或者拿给以极致速度著称的 StarRocks，第一步不会是“跑得有点慢”，而是整个执行模型会在三个位置同时撞墙：

1. **内存膨胀方向彻底反转**：传统结构化分析里，数据经过扫描、过滤和聚合通常是“越算越小”（选择率 \(< 1\)）；多模态解码恰恰相反——一张 100 KiB 的压缩 JPEG 解成 RGB 张量瞬间膨胀约 **20 倍**，一段 10 MiB 的视频抽帧膨胀上百倍。一旦引擎沿用阶段级全量物化（先把 100 万张图全部解完落盘，再进下一个 Stage 喂给 GPU），内存和本地盘会瞬间被解码态张量撑爆。
2. **阶段内同构资源假设崩塌**：一条多模态流水线同时揉进了三种截然不同的硬件瓶颈——高并发异步网络 I/O（64~256 个并发连接拉 S3）、CPU 密集型解码与分词、以及稀缺昂贵的 GPU 矩阵推理。如果像 Spark 那样把一个 `Stage` 绑死在单一的 `ResourceProfile` 上，CPU 解码和 GPU 推理同阶段会逼着每条 CPU 任务空占一张 GPU；硬拆成两个 Stage 中间走 Shuffle 落盘，又彻底失去流式重叠（Overlap），还把膨胀 20 倍的张量往磁盘和网络里多倒腾一遍。
3. **跨语言序列化墙与控制面失速**：AI 生态（PyTorch、Transformers、vLLM、SGLang、Pydantic）的母语是 Python，但数据面 I/O、列式计算和分布式调度必须跑在原生系统级语言（Rust/C++）上。PySpark 要把 JVM 里的 `InternalRow` 编成 Arrow IPC 经 Socket 传给外部 `pyspark.worker` 进程再传回；反过来，完全用 Python 写调度与数据胶水层的早期分布式方案，又在千万级分块和 PB 级 Shuffle 下被单线程控制面与对象存储元数据拖垮。

在上一篇 [《Ray：把对象当货币，从任务提交、对象生命周期到扩容预算》]({{< relref "2026-11-10-ray-object-as-currency-source-notes.md" >}}) 中，我们拆解了 Ray Core 如何用资源租约和对象所有权接住异构 AI 负载。沿着这个脉络继续追问：既然 Ray 已经成了分布式 AI 底座，为什么在其上长出 **Ray Data** 之后，社区又演进出了把调度、执行与 Shuffle 全量用 Rust 重写的 **Daft（Swordfish + Flotilla）**？它们和 **Spark、StarRocks** 真正的架构分水岭在哪里？整个开源与商业 AI 数据栈又在朝哪个方向收敛？

---

## 一、Ray 与 Ray Data 的架构剖面：它解决了什么，又卡在了哪里？

Ray Core 的立身之本在于把“固定阶段 DAG”拆成了“无状态细粒度 Task + 有状态常驻 Actor + Plasma 共享内存对象存储”（核心提交、租约四闸门与所有权链路见 [《Ray：把对象当货币》]({{< relref "2026-11-10-ray-object-as-currency-source-notes.md" >}})）。建立在这层底座之上的 **Ray Data**，核心贡献是用**算子级流式执行**和**异构资源池**打通了数据预处理与 GPU 推理/训练的最后一公里。

### 1.1 Ray Data 的核心强项：算子级异构流水线与原生 `ray.data.llm`

与传统引擎“整段 Stage 共享同一套并行度和资源模板”不同，Ray Data 的 `StreamingExecutor` 允许上游读取算子、中游 CPU `TaskPoolMapOperator` 与下游 GPU `ActorPoolMapOperator` 各自独立配置并发数与资源配额，并通过 `ResourceManager`（[`resource_manager.py:650-1126`](https://github.com/ray-project/ray/blob/3fb778d3/python/ray/data/_internal/execution/resource_manager.py#L650-L1126)）统一管住 Plasma 对象存储配额（默认上限为 Plasma 总容量的 50%，`ReservationOpResourceAllocator` 为每个活跃算子预留输出配额防止死锁，并按“下游优先”分配剩余共享预算），再叠加并发上限、资源预算与下游容量三重反压策略，把流式解码的峰值内存压在可控水位内。

在最新代码基线（`3fb778d3`）里，Ray Data 把这套异构流水线的杀手级场景打磨到了极致——**`ray.data.llm` 大规模离线批量大模型推理**（入口 `build_processor`，[`llm.py:562-725`](https://github.com/ray-project/ray/blob/3fb778d3/python/ray/data/llm.py#L562-L725)）。当你配置 `vLLMEngineProcessorConfig` 或 `SGLangEngineProcessorConfig` 时，[`vllm_engine_proc.py:116-360`](https://github.com/ray-project/ray/blob/3fb778d3/python/ray/llm/_internal/batch/processor/vllm_engine_proc.py#L116-L360) 会自动把一次推理拆成前后解耦的异构流水线：

- **前置 CPU 阶段**（独立 `batch_size` / `concurrency` / `num_cpus`）：`PrepareMultimodalStage` → `ChatTemplateStage` → `TokenizerStage`；
- **核心 GPU 阶段**（`vLLMEngineStage` / `SGLangEngineStage`）：跑在 `ActorPoolStrategy` 常驻权重进程池上，启用 `zero_copy_batch=True` 与 `max_concurrent_batches` 让多个批次同时压进 vLLM/SGLang 内部的 Continuous Batching 队列，原生支持张量/流水线并行（TP/PP）`PlacementGroup`、Prefix Caching 与动态 LoRA；
- **后置 CPU 阶段**：`DetokenizeStage` 异步反分词输出。

对于“把海量样本流式喂进 Ray Train（PyTorch DDP/FSDP）”或“在百卡集群上跑 vLLM/SGLang 离线批推理”这两类与模型引擎紧耦合的负载，Ray Data 的生态贴合度目前几乎无人能出其右。

### 1.2 源码里清晰可见的三道结构性天花板

可一旦工作负载从“纯 Map + 模型推理”向**复杂多模态 ETL + 关系型查询（多表 Join、窗口聚合、大规模 Shuffle、超高吞吐对象存储扫描）**延伸，Ray Data 源码里的三道结构性瓶颈就会立刻暴露：

1. **单线程 Python 控制面 + 块级 Ray Core 调度开销**：
   `StreamingExecutor._scheduling_loop_step`（[`streaming_executor.py:504-594`](https://github.com/ray-project/ray/blob/3fb778d3/python/ray/data/_internal/execution/streaming_executor.py#L504-L594)）运行在 Driver 的单条 Python 线程中，每轮迭代要对全部活跃任务执行 `ray.wait(..., fetch_local=False)`，每派发一个任务就重新计算全图 `ResourceManager.update_usages()`。更关键的是，算子按块粒度直接向 Ray Core 提交远程任务，每个输出块默认产出 **2 个 `ObjectRef`**（数据块本体 + 元数据对象，由后台 `MetadataFetcher` 异步 `ray.wait` + `ray.get` 拉回 Driver，[`task_pool_map_operator.py:183-188`](https://github.com/ray-project/ray/blob/3fb778d3/python/ray/data/_internal/execution/operators/task_pool_map_operator.py#L183-L188)）。当数据规模膨胀到数十万个分块，头节点的引用跟踪、raylet RPC 与 Python 循环开销就会成为显著瓶颈。
2. **Plasma 共享内存穿越与 Shuffle RSS 膨胀**：
   只要算子链被融合规则打断（如修改 `batch_size` 或遇到 Shuffle），中间块就必须写入 Plasma 对象存储再由下游拉出。即便在最新的 HashShuffle V2（[`hash_shuffle_v2.py:11-15`](https://github.com/ray-project/ray/blob/3fb778d3/python/ray/data/_internal/execution/operators/hash_shuffle_v2.py#L11-L15)）中，开发团队也不得不为 Shuffle Map 任务专门设置 `_SHUFFLE_MAP_RUNTIME_ENV` 隔离 Worker 进程——注释明确指出原因在于 `combine_chunks` 产生的 Plasma `mmap` 内存页会在常驻 Worker 中不断累积，撑爆进程 RSS。
3. **薄弱的关系优化器与缺失原生系统内核**：
   - **优化器极简**：[`optimizers.py:31-88`](https://github.com/ray-project/ray/blob/3fb778d3/python/ray/data/_internal/logical/optimizers.py#L31-L88) 仅含 6 条核心逻辑规则（Limit/Projection/Predicate 下推、CombineShuffles、CombineDownloads、PushdownCountFiles）外加 CSE 与 4 条物理规则，无代价模型、无连接重排、无广播连接选择。
   - **关系算子短板明显**：源码中**完全没有 Window 窗口算子**；`JoinOperator`（[`join.py:169-314, 415-562`](https://github.com/ray-project/ray/blob/3fb778d3/python/ray/data/_internal/execution/operators/join.py#L415-L562)）对任意等值连接永远双侧 Hash Shuffle（不支持 Broadcast Join、Sort-Merge Join 或 ASOF Join），分区内直接委托给单线程 `pa.Table.join`（按 2 倍堆内存估算），且因为 PyArrow Join 不支持嵌套与扩展类型，遇到复杂列时不得不先剥离列、拼合成人工索引连接，再通过 `take + hstack` 重新拼回，且完全不支持按嵌套列连接。
   - **Worker 内缺融合执行核**：`_map_task` 在 Worker 里由 Python `MapTransformer` 逐块串联 PyArrow/Pandas/UDF，缺少一层把异步 S3 I/O、多模态解码和向量化 SQL 算子融为一体的 Rust/C++ 原生执行引擎。

理解了这三道天花板，你就能一眼看懂 **Daft 为什么会从 Ray 生态里长出来，又为什么在 2025~2026 年完成了“Swordfish + Flotilla”彻底的内核重构**。

---

## 二、Daft 架构深潜：把 Ray 降维成节点底座，把调度与执行全面收回 Rust

早期的 Daft 也曾把每个分区任务交给 Ray Core 调度，但很快撞上了和 Ray Data 一模一样的头节点对象压力、细粒度调度开销与 Plasma Shuffle 墙。Eventual 团队给出的破局方案极其彻底——**不再让 Ray Core 调度每一个数据微批，而是把 Ray 降维成纯粹的“集群节点与异构硬件容器（Cluster Substrate）”**，在其上用 Rust 打造了两层垂直整合的原生内核：单机流式执行核 **Swordfish** 与分布式调度层 **Flotilla**。

![Ray Data 与 Daft (Flotilla + Swordfish) 双轨分布式架构对比](/images/diagrams/exec-ray-vs-daft-architecture.svg)

### 2.1 单机内核 Swordfish：纯 Tokio 三池隔离与 `channel(1)` 零延迟背压

翻开 `src/daft-local-execution/` 会发现一个很有意思的设计事实：尽管 Cargo 工作区里声明了 `rayon`，但 **Swordfish 执行核完全没有使用 Rayon，而是彻底构建在三套隔离的 Tokio 异步运行时之上**（[`common/runtime/src/lib.rs:25-44`](https://github.com/Eventual-Inc/Daft/blob/1e7eef89/src/common/runtime/src/lib.rs#L25-L44)，[`run.rs:63-93`](https://github.com/Eventual-Inc/Daft/blob/1e7eef89/src/daft-local-execution/src/run.rs#L63-L93)）：

1. **`GLOBAL_RUNTIME`**：单线程事件循环，专门驱动 `run_execution_loop` 和各个 `PipelineNode` 的轻量协程协调器，避免多线程协调锁争用；
2. **`COMPUTE_RUNTIME`**：`NUM_CPUS` 条 `DAFTCPU` 线程，专跑哈希连接、分组聚合、排序、图像解码与分词等 CPU 密集型微批任务；
3. **`THREADED_IO_RUNTIME`**：`min(8, NUM_CPUS)` 条 `DAFTIO` 线程，专跑 S3/HTTP 异步拉取、Parquet 预取与 Flight Shuffle 读写，确保慢网络请求绝不会饿死 CPU 算子。

为了死守多模态解码的内存红线，Swordfish 在所有相邻 `PipelineNode` 之间全线采用 **容量仅为 1 的有界通道 `tokio::sync::mpsc::channel(1)`**（[`channel.rs:30-33`](https://github.com/Eventual-Inc/Daft/blob/1e7eef89/src/daft-local-execution/src/channel.rs#L30-L33)）。在统一事件循环 `next_event`（[`pipeline.rs:111-146`](https://github.com/Eventual-Inc/Daft/blob/1e7eef89/src/daft-local-execution/src/pipeline.rs#L111-L146)）里，节点只有在 `task_set.len() < max_concurrency` 时才会去上游 `recv()`；一旦下游 GPU 推理或写盘变慢，`output_sender.send().await` 立刻挂起，背压逐跳瞬间顶回 `SourceNode` 停止从 S3 拉取新文件。微批粒度 `MorselSizeRequirement`（默认 `default_morsel_size = 131,072` 行）则自顶向下传播，并支持 `LatencyConstrainedBatchingStrategy`（按 5s SLA 动态二分探测最佳批大小）。

更关键的是，Swordfish 对 **Python UDF 与 GIL 的治理**做到了丝滑分层：

- **异步协程 UDF（`async def`，调外部 LLM API 或异步服务）**：下沉为 `AsyncUdfSink`（[`async_udf.rs:147-410`](https://github.com/Eventual-Inc/Daft/blob/1e7eef89/src/daft-local-execution/src/streaming_sink/async_udf.rs#L147-L410)），单节点内部用 `JoinSet` 维持默认 64 路在途协程，通过 `pyo3_async_runtimes` 桥接后台 Python 事件循环，在 `.await` 等待网络返回期间**完全释放 GIL**；
- **同步 CPU/模型 UDF（`@daft.cls` 或 `use_process=True`）**：走 `UdfHandle::Process` 启动独立 `daft.execution.udf_worker` 子进程，通过 **Unix Domain Socket + POSIX `SharedMemoryTransport` 传递 Arrow IPC**（[`udf.rs:391-393`](https://github.com/Eventual-Inc/Daft/blob/1e7eef89/src/daft-local-execution/src/intermediate_ops/udf.rs#L391-L393)），绕开 Python GIL 且避免普通 Socket 全量字节拷贝；
- **完整的关系型本地算子**：Swordfish 原生内置了 `HashJoin`、`SortMergeJoin`、`AsofJoin`、`CrossJoin`、`TopNSink`、四种 `Window*Sink`（分区键哈希切分后并行排序评估），以及会在首微批动态采样基数比率（阈值 `0.8`）在 `AggThenPartition` 与 `PartitionThenAgg` 间自动切换的 `GroupedAggregateSink`（[`grouped_aggregate.rs:177-217`](https://github.com/Eventual-Inc/Daft/blob/1e7eef89/src/daft-local-execution/src/sinks/grouped_aggregate.rs#L177-L217)）。

### 2.2 分布式层 Flotilla：每节点单 Actor、深度优先调度与 Arrow Flight Shuffle

把 Swordfish 扩展到百节点集群的，是运行在头节点单例 Actor（`RemoteFlotillaRunner`）里的 Rust 调度层 **Flotilla**（`src/daft-distributed/` + [`daft/runners/flotilla.py`](https://github.com/Eventual-Inc/Daft/blob/1e7eef89/daft/runners/flotilla.py#L139-L290)）。它对 Ray 的用法堪称“反客为主”的经典样本：

1. **每节点只启一个常驻 `RaySwordfishActor`（`num_cpus=0`）**：
   `start_ray_workers`（[`flotilla.py:430-566`](https://github.com/Eventual-Inc/Daft/blob/1e7eef89/daft/runners/flotilla.py#L430-L566)）给集群里每台带 CPU/内存的 Ray 节点硬亲和绑定**唯一一个**异步 `RaySwordfishActor`。注意它故意不向 Ray Core 申请 CPU 槽位，而是直接把整机 `num_cpus` 交给节点内常驻的 Rust `NativeExecutor`（同时在节点上拉起内嵌的 Arrow Flight 服务 `ShuffleFlightServer`）。此后所有子计划都由 Flotilla 通过 `actor_handle.run_plan.remote(...)` 直接投递进 Swordfish，**彻底绕开 Ray Core 的逐任务调度与 Worker 进程拉起开销**；同一节点内相同 `plan_fingerprint` 的多条任务还会通过 `InputId` 多路复用同一条活的 Swordfish 流水线。
2. **深度优先调度 + 自动压低头节点 ObjectRef 压力**：
   Flotilla 的 Rust `SchedulerLoop`（[`scheduler_actor.rs:38-232`](https://github.com/Eventual-Inc/Daft/blob/1e7eef89/src/daft-distributed/src/scheduling/scheduler/scheduler_actor.rs#L38-L232)）每秒滴答并每 5 秒发现新节点，内部 `BinaryHeap` 的优先级排序规则非常有讲究：`(query_idx 升序, node_id 降序, task_id 升序)`（[`task.rs:203-237`](https://github.com/Eventual-Inc/Daft/blob/1e7eef89/src/daft-distributed/src/scheduling/task.rs#L203-L237)）。**`node_id` 越高（越靠近下游算子）优先级越高**——这种深度优先策略能优先把已产出的中间数据消化落盘、释放集群内存，避免上游把整个集群塞满。同时，非 Shuffle 产出在 Worker 侧会自动合并到 **`64 MiB`**（[`flotilla.py:245-275`](https://github.com/Eventual-Inc/Daft/blob/1e7eef89/daft/runners/flotilla.py#L245-L275)）才向头节点 yield 一个 `ObjectRef`，大幅削减头节点的引用表压力。
3. **双轨 Shuffle：小规模 Pre-Shuffle Merge，大规模 Arrow Flight 直传**：
   - 默认小规模走 Ray Object Store，并在 \(\sqrt{M \times N} > 200\) 时自动插入 `PreShuffleMergeNode` 按 1 GiB 预合并；
   - 当估算 Shuffle 超过 `10 GiB` 或 \(M \times N \ge 500{,}000\) 时提示并支持切换 **`flight_shuffle`**（[`translate_shuffle.rs:17-35`](https://github.com/Eventual-Inc/Daft/blob/1e7eef89/src/daft-distributed/src/pipeline_node/shuffles/translate_shuffle.rs#L17-L35)）：Map 端在 Tokio I/O 池一次性把所有输出分区写成本地 NVMe 上单个 LZ4 压缩 Arrow IPC 文件（4 MiB 块，[`oneshot_writer.rs:58-100`](https://github.com/Eventual-Inc/Daft/blob/1e7eef89/src/daft-shuffles/src/oneshot_writer.rs#L58-L100)），Reduce 端通过 `ShuffleFlightClient` 走 Arrow Flight gRPC 点对点直拉（**同节点读取直接调用 `get_partition_local` 零 gRPC 旁路**）。更妙的是元数据折叠（[`backends/flight.rs:42-85`](https://github.com/Eventual-Inc/Daft/blob/1e7eef89/src/daft-distributed/src/pipeline_node/shuffles/backends/flight.rs#L42-L85)）：头节点不再保存 \(O(M \times N)\) 矩阵，而是折叠为 \(O(M)\) 的 `BTreeMap<server_address, Vec<input_id>>`，Reduce 侧用 `(input_id << 32) | partition_idx` 自行拼出分区 ID！

### 2.3 优化器、多模态类型、高吞吐 I/O 与 Key 级断点续跑

除了执行层，Daft 还有四项直击多模态与 AI 工程痛点的差异化设计：

- **面向 AI 算子特化的逻辑优化器**：除了常规的谓词/投影/Limit/分区裁剪/计数下推与连接重排（默认 7 表以内 `BruteForceJoinOrderer`，可选 12 表以内 Moerkotte-Neumann `DpCcpJoinOrderer`，广播连接阈值 `10 MiB`），Daft 专门写了 **`SplitUDFs`** 与 **`SplitVLLM`**（[`optimizer.rs:124-240`](https://github.com/Eventual-Inc/Daft/blob/1e7eef89/src/daft-logical-plan/src/optimization/optimizer.rs#L124-L240)）：把混在同一个 `Project` 里的多个 UDF 或 `vllm-prefix-caching` 调用自动拆成独立的 `UDFProject` / `VLLMProject` 节点，从而为每个模型独立分配 Actor 池、GPU 份额与 Prefix Routing 负载均衡。
- **一等公民的多模态类型与原生 Rust 预处理库**：[`dtype.rs:124-152, 361-435`](https://github.com/Eventual-Inc/Daft/blob/1e7eef89/src/daft-schema/src/dtype.rs#L124-L152) 把 `Image`、`FixedShapeImage`、`Tensor`、`FixedShapeTensor`、`SparseTensor`、`Embedding` 和 `File(MediaType)`（延迟字节区间子文件引用 `{url, io_config, position, size}`）直接编码为可经 Arrow/Parquet 往返的物理类型（`daft.super_extension`）。同时在 Rust 层原生实现了图像编解码/缩放/裁剪（`daft-image`）、Common Crawl 流式 WARC 解析（`daft-warc`）、8 路 Portable SIMD 梅森素数 MinHash 去重（`daft-minhash`）、`tiktoken-rs` BPE 分词（`daft-functions-tokenize`）以及 Tokio `buffer_unordered` 高并发 `UrlDownload`（每核 32 并发）。
- **面向云对象存储极限调优的异步 I/O**：`RemoteChunkSource`（[`chunk_source.rs:520-660`](https://github.com/Eventual-Inc/Daft/blob/1e7eef89/src/daft-parquet/src/reader/chunk_source.rs#L520-L660)）在 Parquet Row Group 剪枝后，会把间隔 \(\le 1\text{ MiB}\) 的列字节区间自动合并（Range Coalescing），同时把超过 `24 MiB` 的大区间拆成 \(\le 16\text{ MiB}\) 的并行请求在 `THREADED_IO_RUNTIME` 上并发预取，并由 `ResumableByteStream`（[`object_io.rs:147-239`](https://github.com/Eventual-Inc/Daft/blob/1e7eef89/src/daft-io/src/object_io.rs#L147-L239)）自动记录已传输字节并在网络闪断时用 HTTP `Range` 续传。湖仓生态原生对接 Parquet、Iceberg、Delta Lake、Hudi、Paimon 与 **Lance**（支持标量索引、点查与列合并）。
- **面向长耗时 GPU 作业的 Key 级断点续跑（`daft-checkpoint`）**：在上一篇 [《Ray：把对象当货币》]({{< relref "2026-11-10-ray-object-as-currency-source-notes.md" >}}) 里我们得出一条结论——任务重试本身不能保证容错，真正的容错靠算子幂等。对于跑了 8 个小时 GPU 推理在第 90% 时节点被抢占的多模态作业，就算调度器支持血缘重算也烧不起重跑成本。Daft 直接内置了基于 S3/文件系统的三态 `CheckpointStore`（`Staged → Checkpointed → Committed`，[`store.rs:21-38, 54-154`](https://github.com/Eventual-Inc/Daft/blob/1e7eef89/src/daft-checkpoint/src/store.rs#L21-L38)）：每个分区完成写入后原子封存已处理的主键 Parquet 和文件清单；作业重启时，规划器自动注入 `BlobStoreCheckpointedKeysScanOperator` 做一次 **Key 级 Anti-Join**，直接跳过已经算完的样本！

---

## 三、横向深度对决：Ray Data vs Daft vs Apache Spark vs StarRocks

把四套开源引擎放进同一张解剖台，才能看清它们并不是简单的“谁替代谁”，而是**围绕完全不同的硬件瓶颈与业务契约演化出的四个物种**：

![Spark、StarRocks、Ray Data 与 Daft 四类引擎执行范式分水岭](/images/diagrams/exec-four-engines-paradigm-map.svg)

| 对比维度 | Apache Spark (`5d491f62`) | StarRocks (`f977b53c`) | Ray Data (`3fb778d3`) | Daft (`1e7eef89`) |
| --- | --- | --- | --- | --- |
| **核心定位** | 企业级 PB 结构化批处理/湖仓 ETL | 亚秒级高并发实时 OLAP / 湖仓分析 | 分布式 ML 数据喂入与批量 LLM 推理 | 多模态 AI 数据引擎（Python + SQL） |
| **执行内核** | JVM Tungsten（商业/扩展有 Photon/Gluten） | C++ 向量化 + Pipeline 协作式用户态调度 | Python `MapTransformer` + PyArrow + vLLM/SGLang | Rust Swordfish（Tokio 三池 + `mpsc(1)` 背压） |
| **分布式调度与并行度** | `DAGScheduler` 按 Shuffle 切 Stage；**单 Stage 同构 `ResourceProfile`** | FE 生成固定 Fragment + Pipeline DOP（详见[DOP 三次决策]({{< relref "2026-09-12-dive-dop.md" >}})） | Python `StreamingExecutor` 块级调度 Ray Task / ActorPool，**算子级并行** | Rust Flotilla 调度每节点单 `RaySwordfishActor`，**深度优先异构微批** |
| **优化器深度** | Catalyst + **AQE 运行期动态合桶/广播/倾斜拆分** | **完整 Cascades CBO**（列统计/直方图/属性 Enforcer/MV 改写） | 6 条基础规则下推 + 融合，**无 CBO / 无连接重排** | 完整规则下推 + `SplitUDFs/VLLM` + 轻量 `BruteForce/DP-ccp` 重排（尚无列级 NDV） |
| **关系算子完备度** | 极高（全套 SQL、Window、复杂 Join + AQE） | 极高（全套 SQL、物化视图、L3 感知两级哈希表与协程预取） | 弱（**无 Window**；Join 仅双侧 Hash Shuffle + 单线程 `pa.Table.join`） | 高（完整 SQL、Hash/SMJ/ASOF Join、4 种 Window、自适应聚合） |
| **多模态与 Python 融合** | 弱（`ArrowEvalPythonExec` 跨 OS 进程 Socket IPC 往返拷贝） | 极弱（纯 CPU SQL 引擎，非 Python/GPU 原生） | 强（Python 原生、常驻 `ActorPool`、原生 vLLM/SGLang 连续批） | 极强（原生多模态类型 + Rust 图像/WARC/MinHash/分词 + 异步/进程 GIL 旁路 + 原生 `VLLMProject`） |
| **Shuffle 与容错** | 磁盘 Shuffle / External Shuffle + 完整血缘重算 + 成熟 Spill | 内存流水交换 + 算子级 Radix Spill，面向低延迟查询重试 | Plasma 对象存储（`N×R` pull / push / V2）+ 任务重试 | 默认 Ray Object Store（Pre-Shuffle Merge）+ 大规模 **Arrow Flight NVMe Shuffle** + **Key 级 S3 断点续跑** |

把这张表翻过来追问四个最关键的架构边界问题，就能避免工程选型里最常见的误判：

### 3.1 为什么 Daft 与 Ray Data 取代不了 StarRocks？

在 [《StarRocks 实现原理与源码深度分析》]({{< relref "2026-08-09-starrocks-architecture-and-internals.md" >}})、[《深入 StarRocks Optimizer》]({{< relref "2026-09-01-dive-starrocks-optimizer.md" >}}) 和 [《OLAP 引擎里的 Hash Table》]({{< relref "2026-11-19-olap-hash-table-design-and-tuning.md" >}}) 中，我们拆解过 StarRocks 为了把十表星型关联压进亚秒级所做的一切：基于列级直方图与 NDV 的 Cascades CBO 全局搜索、物理属性 Enforcer 协商、物化视图透明改写、L3 缓存自适应的两级哈希表、C++20 协程交错预取 Join，以及 Shared-Data 架构下的本地盘数据缓存与千并发短查询调度。

反观 Daft：源码 [`stats.rs:23-24`](https://github.com/Eventual-Inc/Daft/blob/1e7eef89/src/daft-logical-plan/src/stats.rs#L23-L24) 坦承目前 `PlanStats` 只有表级行数和字节数，尚无列级 NDV 和直方图；Swordfish 的本地阻塞算子（Sort/HashJoin/Agg）也尚未实现通用的外存磁盘 Spill；Flotilla 架构基于 Ray Actor 通信，服务于长吞吐批处理而非千并发 50 ms 点查 BI。**只要核心战场是高并发交互式 SQL 分析与复杂星型/雪花数仓查询，StarRocks/ClickHouse/Doris 这类 C++ MPP OLAP 引擎的优势依然是代际性的。**

### 3.2 为什么 Spark 在结构化 PB 级 ETL 里依然难被全面淘汰？

我们把 Spark 源码（`5d491f62`）里最核心的两面摊开看，就会明白它的护城河和软肋同样鲜明：

- **软肋在硬件与语言边界**：
  - 在 [`Stage.scala:56-64`](https://github.com/apache/spark/blob/5d491f62/core/src/main/scala/org/apache/spark/scheduler/Stage.scala#L56-L64) 与 [`DAGScheduler.scala:591-650`](https://github.com/apache/spark/blob/5d491f62/core/src/main/scala/org/apache/spark/scheduler/DAGScheduler.scala#L591-L650) 里，每个 Stage 只能绑定一个 `resourceProfileId`；若同阶段内不同 RDD 请求了不同资源，Spark 要么直接抛 `IllegalArgumentException`，要么按逐项取 `max` 合并——导致高并发 CPU 预处理和 GPU 推理无法在同一个流式管线里各自按最优配比并行；
  - 在 [`ArrowEvalPythonExec.scala:77-220`](https://github.com/apache/spark/blob/5d491f62/sql/core/src/main/scala/org/apache/spark/sql/execution/python/ArrowEvalPythonExec.scala#L77-L220) 与 [`PythonWorkerFactory.scala:163-255`](https://github.com/apache/spark/blob/5d491f62/core/src/main/scala/org/apache/spark/api/python/PythonWorkerFactory.scala#L163-L255) 里，PySpark UDF 必须经由 Loopback TCP 或 Unix Domain Socket 与外部 Python 进程交互，每批数据都要经历 `InternalRow → ArrowWriter → IPC 序列化 → Socket 写入 → Python 反序列化 → 计算 → Socket 回传 → ArrowStreamReader → InternalRow` 的整套往返折腾。
- **护城河在运行期自愈与超大规模稳健性**：
  - [`AdaptiveSparkPlanExec.scala:69-384`](https://github.com/apache/spark/blob/5d491f62/sql/core/src/main/scala/org/apache/spark/sql/execution/adaptive/AdaptiveSparkPlanExec.scala#L69-L384) 依托 Shuffle 阶段屏障拿到的真实 `MapOutputStatistics`，在运行期自动完成小分区合并（`CoalesceShufflePartitions`）、倾斜连接自动切分复制（`OptimizeSkewedJoin`）以及 Sort-Merge Join 动态转 Broadcast Hash Join（`DynamicJoinSelection`，另见[《深入 Hash Join》Spark 附录]({{< relref "2026-08-26-dive-hash-join.md" >}})）。加上全算子磁盘 Spill、External Shuffle / Celeborn 解耦、以及长达十余年的企业级治理与安全生态，处理 PB 级强倾斜纯表格数仓 ETL 时，Spark（尤其配合 Photon 或 Gluten/Velox 原生加速后）依然是最稳妥的工业基石。

### 3.3 Ray Data vs Daft：站在同一片 AI 主场，分水岭在哪里？

如果已经确定业务落在“异构 CPU+GPU 多模态/AI 数据处理”，Ray Data 与 Daft 怎么选？其实边界非常清晰：

- **Ray Data 更像“面向训练与推理集群的流式数据管道”**：如果你的核心链路是 `读取样本 → 轻度预处理 → 喂给 Ray Train 做多机多卡训练`，或者跑大规模 **vLLM / SGLang 离线批量 LLM 推理**（需要成熟的 TP/PP 放置组、动态 LoRA、Continuous Batching 多批重叠与 Ray Serve 复用），Ray Data 与 Ray 全家桶的无缝耦合能省去大量胶水代码。
- **Daft 更像“长在 Ray 底座上的原生多模态查询引擎”**：一旦流水线里同时混杂了**海量云端小文件/WARC/Parquet 高速并发读取 + 图像解码/MinHash 去重/BPE 分词 + 多表 Join/Window/SQL 清洗 + 异步 API 或多模态 GPU 打标 + 断点续跑写回 Iceberg/Lance**，Daft 的 Rust Swordfish 核、Flotilla 调度、Arrow Flight Shuffle 和完整查询优化器会带来数量级的吞吐与内存稳定性优势——而且开发者在笔记本单机（Native Swordfish）与千核集群（Flotilla on Ray）上运行的是完全一致的 Rust 执行引擎，本地调通即可无痛上集群。

### 3.4 控制面扩展墙：Ray Driver/GCS 与 Spark Driver 的同构瓶颈、工程取舍与业界解法

把规模继续拉大到千级节点、十万级分块并发时，无论 Ray 还是 Spark 都会撞上一条共通的分布式铁律：**数据面容易水平扩展，但只要控制面存在“单一所有权（Single Owner）”或“全局一致视图（Global State）”，元数据扇入、内存膨胀与单点故障最终一定会先于算力触顶。**

#### 1. Ray 的双重控制面瓶颈：Driver（Owner）与 GCS

Ray 1.0 引入的 **Ownership（所有权）模型**把普通 Task 调度和 `ObjectRef` 追踪从中心化的 GCS 下放给了调用方，看似去中心化，但在真实大作业里反而形成了“Driver 扛高频任务、GCS 扛全局状态”的两道压力墙：

- **Ray Driver（默认 Owner）的四重过载**：绝大多数脚本由单个 Python Driver 循环提交 `.remote()`，导致该进程成了几十万任务与对象的唯一 Owner：
  1. **小对象内联（Inlining）撑爆堆内存**：Ray 默认把小于 **100 KB** 的返回值直接内联进 Owner（Driver）的堆内存而非分布式 Plasma 对象存储。一旦 Driver 扇出 100 万个各返回 50 KB 字典的任务，**50 GB 数据会直接灌进单机 Driver 触发 OOM**；
  2. **引用表、血缘与 gRPC 扇出瓶颈**：每个 `ObjectRef` 的位置表与容错 Lineage 都驻留 Driver 内存（如 1.2 节源码所见，Ray Data 每块默认产生 2 个 `ObjectRef`），高频提交毫秒级短任务会迅速打满 Python GIL、CoreWorker gRPC 线程与单机网卡；
  3. **`ray.get()` 汇聚单点带宽墙**；
  4. **Fate Sharing 单点故障（SPOF）**：Owner 一旦崩溃或 OOM，其名下所有对象与派生任务连带失效。
- **Ray GCS（Global Control Service）的三重上限**：运行在 Head Node 上的单进程 C++ 元数据中心，仍掌控着全集群所有全局可见状态：
  1. **海量 Actor 生命周期风暴**：与普通 Task 不同，**每个 Actor 的创建调度、名字注册/发现（`ray.get_actor`）、死亡通知与 Placement Group 分配全必须串行过 GCS**，数万 Actor 并发启停（RL Rollout / Agent 沙箱）极易拖垮 GCS RPC 队列；
  2. **千节点级资源视图广播（`O(N²)` 同步压力）**：各节点 `Raylet` 上报心跳与剩余资源、GCS 广播全集群资源视图，在千到万节点规模下极易打满 Head Node CPU 与网络带宽；
  3. **Task/Job 可观测性事件堆积**：Dashboard 与 State API 汇聚千万级任务事件时若淘汰不及时，会引发 Head Node OOM，导致整套集群控制面失联。

#### 2. Spark 的同构瓶颈：更中心化的 Driver 与 Cluster Manager

Spark 不仅存在高度同构的控制面瓶颈，甚至因为缺乏 Ownership 下放，**单作业 Driver 的中心化程度比 Ray 更高**：

- **Spark Driver（JVM）过载与 Full GC 雪崩**：整条作业哪怕十万级 Task 也全部由单个 JVM Driver 序列化闭包、下发并汇总状态；更致命的是 **`MapOutputTrackerMaster`** 在超大 Shuffle（例如 `10,000 Map × 10,000 Reduce`）中需要汇聚并广播 `M × R` 规模的 `MapStatus` 位置矩阵，极易撑爆 Driver 堆引发长达数十秒的 **Full GC Stop-the-World**，连带触发 Executor 心跳超时失联。此外，`collect()` / `toPandas()` 与估算失真的 Broadcast Join 同样是高频 Driver OOM 诱因；经典 Py4J 模式下用户 Python 脚本更直接与 Driver 共享宿主机内存。
- **Cluster Manager（类 GCS 角色）的集群上限**：在 Spark on YARN 中，**ResourceManager（RM）** 承担全局心跳与容器装箱，高并发短作业下易出现分配排队毛刺；在 Spark on K8s 中，**K8s API Server + etcd** 就是集群的 GCS，万级 Executor Pod 瞬时启停会直接把 API Server Watch 队列与 etcd 写入打垮。

#### 3. 核心工程取舍与业界的四条破局路径

面对控制面扩展墙，没有银弹，本质都是在**“全局视图/细粒度容错”**与**“控制面去中心化/低元数据开销”**之间做架构取舍。业界由此分化出四条清晰的解法：

| 破局思路 | 代表体系与核心机制 | 解决的核心瓶颈 | 付出的工程取舍（Trade-off） |
| :--- | :--- | :--- | :--- |
| **1. 控制面粒度粗化（节点级常驻内核架空底座）** | **Daft Flotilla**：每节点仅启 **1 个** `RaySwordfishActor`（压平 GCS Actor 数）；Worker 侧合并到 **64 MiB** 再上报 `ObjectRef`；Arrow Flight Shuffle 把 `O(M×N)` 矩阵折叠为 **`O(M)` 节点映射** | 同时给 Ray Driver（引用表/RPC）与 GCS（Actor/调度风暴）大幅减负，保留 Rust 层全局深度优先背压 | 放弃了 Ray Core 单 Task 粒度的细粒度弹性抢占与血缘重算，需额外自研 **Key 级 S3 Checkpoint** 补齐长作业容错 |
| **2. 层级所有权下放（Hierarchical Ownership）** | **Ray 生态最佳实践**：Driver 只拉起几十个顶层调度 Actor，将子任务提交与对象所有权分散到多层子 Actor，辅以 `ray.wait()` 限流；GCS 侧引入增量资源同步、广播节流、多线程化与多集群联邦 | 打破单进程 Driver 的内存内联与提交扇出上限，避免单集群突破千节点 GCS 同步红线 | 丧失了单点全局视图，跨多层 Actor 协同做全局内存背压与大规模 Shuffle 的复杂度陡增 |
| **3. 用户代码与重元数据外置卸载** | **Spark 现代栈**：**Spark Connect**（gRPC+Protobuf）剥离用户 Python 出 Driver；**AQE** 动态合并小分区压缩 `M×R`；**Apache Celeborn / Uniffle（RSS）** 将 Shuffle 元数据彻底移出 Driver；K8s 侧引入 **Volcano / YuniKorn** 与 Pod 池复用 | 根治用户脚本拖垮共享 Driver、Shuffle 元数据引发 Driver Full GC 与 K8s API Server 过载 | 平台组件链路变长，新增独立 RSS 集群与批调度插件的运维成本及跨跳网络开销 |
| **4. 无状态多前端水平切分（MPP 范式）** | **StarRocks 等 OLAP 引擎**：多 **FE（Follower/Observer）** 水平分担高并发短查询解析规划，**BE** 节点内用 C++ Pipeline 协作式用户态微调度 | 消除单点控制面 QPS 上限，支撑千级并发亚秒点查 | 放弃长耗时批处理的中间落盘与 Task 级局部重算，单节点故障直接整条查询秒级重试 |

回到本文主线就能恍然大悟：**Daft 之所以能从 Ray 生态里突围，不只是因为 Rust 比 Python 快，更是因为 Flotilla 用“每节点单 Actor + 64 MiB 块聚合 + `O(M)` Flight 元数据折叠”精准绕开了 Ray Driver 与 GCS 的控制面扩展墙**——这与 Spark 生态演化出 `Spark Connect + Celeborn` 的底层逻辑殊途同归，都是把最沉重的细粒度元数据与执行状态从脆弱的单点控制面剥离出去。

#### 4. Daft 究竟解决到什么程度？还剩哪三道硬边界？

不过必须客观区分：**Daft 绕开了 Ray 90% 的数据面调度痛点，但只要它仍运行在 Ray 底座上，就并未从物理根源消灭全部控制面天花板**：

1. **Flotilla 自身仍是单例 Rust 头调度器**：虽然 `RemoteFlotillaRunner` 基于 Rust + Tokio 实现，吞吐与内存紧凑度甩开 Ray Data 单线程 Python 控制面 1–2 个数量级，但它本质上依然是一个单例 Head Actor。在极端多租户高并发短查询场景下仍受单点控制面限制，且 Head Node 宕机当前查询会中断（依赖自研 S3 **Key 级 Checkpoint** 断点续跑兜底）。
2. **底层仍受限于 Ray GCS 的物理节点数上限**：只要 Flotilla 还依赖 Ray 做集群底座（定期通过 Ray 发现节点与资源），当底层集群本身膨胀到 5,000–10,000 台物理节点时，Raylet 心跳与 GCS 节点维护天花板依然属于 Ray 自身（除非未来演进出完全脱离 Ray、直连 Kubernetes 的原生 Flotilla 调度层）。
3. **有状态 GPU 模型池仍需注册为 Ray Actor**：运行 `@daft.cls` 或 `VLLMProject` 时仍会经由 GCS 注册 GPU Actor——不过其数量受限于物理 GPU 卡数（百到千级），远小于按数据分块爆炸的 Task 量级。

---

## 四、演进脉络与生态全景：从 Ray → Daft 看 AI 数据栈的四代演化

跳出单个仓库的代码细节，把视线拉长到 2015~2026 年整个开源与商业数据基础设施的演变，你会发现 **Ray → Daft 并不是一次孤立的框架更替，而是 AI 工作负载倒逼数据基础设施经历四代演化的必然切面**：

![AI 多模态数据引擎四代演进脉络与商业生态格局](/images/diagrams/exec-ai-data-engine-evolution.svg)

### 第一代：结构化大数据与 MPP 分析引擎（Spark / Hive / StarRocks / ClickHouse）
围绕“规整标量表 + CPU 集群 + 顺序扫描”建立起完整的理论和工程高峰——Catalyst/Cascades 优化器、阶段级 AQE、向量化列存与 C++ Pipeline。这一代引擎今天依然统治着企业 90% 的传统数仓与 BI 场景，但它们的资源模型与存储格式假设数据在内存中是紧凑且越算越小的。

### 第二代：打破阶段屏障的异构分布式底座（Ray Core / Ray Data / Dask）
深度强化学习、分布式训练与在线大模型服务率先击穿了 Spark 的静态 DAG 假设。UC Berkeley RISELab 孵化出的 **Ray（商业化主体 Anyscale）** 以“对象即货币、算子级租约、常驻 Actor”重构了分布式计算，并顺势长出 **Ray Data** 承接 ML 数据预处理与批量推理。它的历史意义在于证明了**异构 CPU+GPU 算子必须解耦并行并以流式反压串联**；但它的原生基因是通用分布式计算框架，而非数据库内核。

### 第三代：用 Rust/C++ 数据库内核重构多模态数据面（Daft / Polars / NeMo Curator）
当多模态预训练、合成数据清洗和大规模 Embedding 流水线进入百 TB 甚至 PB 规模，业界很快发现“纯 Python 调度 + 通用对象存储”扛不住数据面吞吐与复杂查询。于是第三代引擎出现了明显的**系统级内核回归**：

- **Daft（商业化主体 Eventual）** 直接把数据库内核技术（逻辑优化器、morsel-driven 流式管道、Arrow Flight Shuffle、Zone-Map 剪枝、Key 级 Checkpoint）用 Rust 重写，却聪明地**借用 Ray 作为跨节点集群与 GPU 资源底座**，做到“站在 Ray 的肩膀上，绕开 Ray 的数据面瓶颈”；
- **Polars / Polars Cloud** 从单机 Rust DataFrame 霸主向云端分布式延伸，切入轻量高吞吐表格分析与对象存储批处理；
- **NVIDIA NeMo Curator** 则站在 GPU 厂商视角，深度聚焦超大模型预训练语料的去重、过滤与多模态清洗（底层同样广泛运行在 Ray 集群之上）。

### 第四代：存算格式协同与商业平台的双峰分化
进入 2025~2026 年，计算引擎的演化已经反过来倒逼存储格式与商业云平台同步重构（相关硬件与 AI Infra 演进另见[《2026 AI Infra 与现代系统技术雷达》]({{< relref "2026-09-10-system-ai-infra-hardware-distributed-systems.md" >}}) 与 [《seekdb：AI-Native 数据库源码笔记》]({{< relref "2026-08-26-seekdb-ai-native-source-notes.md" >}})）：

- **存储格式突破 Parquet 局限**：Parquet 为顺序扫描标量列而生，面对千万级随机抽样（训练 DataLoader 随机访问）、单行数十 MB 的多模态 Blob 和高维向量索引力不从心。**Lance / LanceDB** 应运而生，以面向随机访问与向量检索的新一代列式格式补齐多模态湖仓的存储短板（Daft 已将其作为一等数据源深度整合）；应用层则出现了 **Pixeltable** 这类声明式多模态计算表，把模型推理与向量索引做成类似物化视图的增量计算列。
- **商业化版图清晰分化为“专精性能派”与“一体化云平台派”**：
  - **专精性能派（Anyscale / Eventual / Polars Cloud）**：服务 AI 基础设施团队、大模型研发与重度多模态工程场景。**Anyscale** 押注“Ray 全栈统一训练 + 推理 + 数据”，**Eventual（Daft）** 押注“多模态 ETL + 批处理极致吞吐与成本比”。这类平台提供最细粒度的 Python 自由度、自定义 CUDA/vLLM 编排与异构集群利用率。
  - **一体化云湖仓派（Databricks / Snowflake / BigQuery）**：服务看重统一安全、血缘审计与低运维门槛的企业客户。**Databricks** 通过 Spark + Photon 原生向量化 + Unity Catalog + **Databricks AI Runtime（AIR，支持 Serverless GPU、NeMo 与 Ray on Spark）** 把 AI 流水线拉回湖仓内部；**Snowflake** 则通过 **Snowflake Cortex AI**（如 `Cortex AI COMPLETE Multimodal`）和 Snowpark Container Services，让业务分析师用一条 SQL 就能直接分析表里的文本、图像与音频。

两条商业路线正在 **Apache Iceberg / Delta Lake / Lance 等开放表格式** 上交汇：企业不再被迫把所有数据锁死在单一引擎的私有存储里，而是同一份湖上数据，交互式报表走 StarRocks/Snowflake，传统数仓 ETL 走 Spark，多模态语料清洗与大模型批推理走 Daft / Ray Data。

### 生态博弈与大厂真实落地：Ray 会因为 Daft 失去用户吗？头部 AI 公司怎么选？

看完 Daft 对 Ray 数据面诸多痛点的降维打击，一个最容易被追问的问题是：**“既然 Daft 解决了 Ray Data 的这么多瓶颈，Ray 会不会因为 Daft 失去用户？大家都在往 Daft 迁移吗？”**

回答这个问题必须先剥离一个常见误区——**千万不要把“Ray”等同于“Ray Data”**：

- **Ray 的真正护城河是“AI 分布式操作系统”底座**：Ray 的核心壁垒在于 **Ray Core + Ray Train + Ray Serve + KubeRay + 强化学习后训练编排（如 veRL / OpenRLHF）**。Daft 根本不与这些层竞争——**Daft 自己就运行在 Ray 底座之上**。
- **真实发生的迁移路径是 `PySpark / Ray Data → Daft on Ray`**：业界确实有大量 AI 团队正在把重度多模态清洗、去重与湖仓 ETL 从容易 OOM 的 Ray Data（或笨重的 PySpark）迁移至 **Daft on Ray**，但他们**并没有离开 Ray**——而是继续保留 Ray / KubeRay 作为统一异构 GPU 集群底座，上游用 Daft 做多模态加工，下游依然在同一套 Ray 集群上跑 Ray Train / veRL / Ray Serve。因此，**Ray 不仅不会因 Daft 失去底座用户，反而会因 Daft 补齐了多模态 SQL/ETL 短板而更具生态黏性**；真正流失份额的是“拿纯 Ray Data 硬扛复杂多模态 ETL”以及“拿传统 JVM Spark 硬扛图片/视频解码”的存量场景。

把视角投向真实的大厂公开披露（严格基于公开演讲、开源项目与官方技术博客，不臆测未公开的内部闭源栈），可以看到前沿基模实验室与大型互联网/企业平台的清晰分化：

| 公司 / 阵营 | 对 Ray 的公开采用情况 | 对 Daft 的公开采用情况 | 基础设施路线特征 |
| :--- | :--- | :--- | :--- |
| **OpenAI** | **早期深度采用并公开背书**（Ray Summit 2022 公开分享在 ChatGPT 时代依托 Ray 编排分布式训练与大规模 RLHF 探索，并参与 Alpa 生态） | **无公开生产案例** | 演进至万卡级前沿模型后，核心预训练与内部编排高度闭源自研 |
| **Anthropic** | **无公开核心训练采用记录**（外部生态网关 / Ray Serve 广泛兼容 Anthropic API） | **无公开生产案例** | 高度闭源，围绕 AWS Trainium / Google TPU / NVIDIA GPU 自研定制编排 |
| **DeepSeek** | **未作为核心训练底座** | **无公开生产案例** | 极致贴近裸金属硬件的 C++/CUDA/PTX 全自研路线（开源了 3FS 分布式文件系统、DualPipe、DeepEP、FlashMLA 等底层基础设施） |
| **字节跳动（ByteDance）** | **重度深度采用**：开源顶流 LLM 后训练框架 **`veRL`** 基于 Ray 混合控制面构建；公开过用 **Ray + KubeRay** 跑 200 TB 级多模态 LLM 离线推理 | **公开评估与生产实践**：分享过在百万级图像分类与大规模多模态流水线上对比并应用 **Daft on Ray** | 典型的“统一 Ray / KubeRay 异构底座 + 多引擎混用（Ray Data / Daft / veRL）”大厂范式 |
| **Amazon / Essential AI / Together AI / CloudKitchens** | 多数同样依托 Ray / Kubernetes 集群底座运行 | **官方公开的生产标杆用户**（Eventual 披露用于 PB 级多模态清洗、基础模型训练语料构建与非结构化 ETL） | 开放云湖仓（S3 + Iceberg / Delta / Lance）上的 AI-Native 多模态栈 |
| **Uber / Pinterest / 蚂蚁集团 / Shopify** | **超大规模企业级 Ray 标杆**（统一异构调度、推荐特征、大规模 RL 与在线推理） | 处于评估或局部多模态管线试点阶段 | 存量大数据（Spark / Flink）与 AI（Ray）双轨并存的平台级架构 |

总体来看，**DeepSeek、Anthropic 与如今的 OpenAI** 这类超一线前沿实验室为了压榨万卡集群最后 5% 的硬件利用率（MFU），普遍选择绕过通用高层框架走裸金属全栈自研；而绝大多数头部互联网公司、AI 独角兽与企业级团队，则正在快速收敛到 **“KubeRay / Ray 统一异构底座 + Daft 承接重度多模态 ETL 与湖仓处理 + veRL / vLLM 承接后训练与推理”** 这套高性价比开源组合。

---

## 五、工程选型决策矩阵

面对真实业务需求，可以直接按以下维度对号入座：

| 业务场景 | 首选引擎 | 关键原因与避坑提示 |
| --- | --- | --- |
| **高并发亚秒级 BI / 复杂十表星型 SQL** | **StarRocks**（或 ClickHouse / Doris） | Cascades CBO + C++ 向量化 + 物化视图 + 低延迟并发队列；切忌拿 Ray Data / Daft 顶高并发点查 |
| **PB 级纯结构化数仓 ETL / 高倾斜宽表关联** | **Apache Spark**（配合 Photon / Gluten 更佳） | AQE 动态纠偏 + 全算子磁盘 Spill + 成熟企业治理；避免在热路径里写高开销逐行 Python UDF |
| **多模态数据清洗 + SQL 关联 + 特征提取写湖仓** | **Daft（Swordfish + Flotilla）** | Rust 原生图像/WARC/MinHash/分词 + 1 MiB 合并 S3 预取 + Flight Shuffle + Key 级 S3 断点续跑；大 Shuffle 记得开启 `flight_shuffle` |
| **百卡集群 vLLM / SGLang 离线批推理 / 喂数给 Ray Train** | **Ray Data**（`ray.data.llm`） | 原生连续批、TP/PP PlacementGroup、动态 LoRA 与 Ray Train 零缝隙衔接；避免把宽多模态列直接丢进 Shuffle（先做列裁剪） |
| **单机百 GB 以内快速探索与原型验证** | **Daft (Native)** 或 **Polars / DuckDB** | 零集群运维开销；若后续要无缝扩到 Ray 集群且含多模态/UDF，Daft 的单机与分布式语义最一致 |
| **强合规企业内的轻度文档/图片 AI 标注分析** | **Snowflake Cortex / Databricks AIR** | 零异构集群运维，直接在统一权限治理下用 SQL 调用托管模型，避免自建 GPU 调度团队 |

---

## 六、源码基线与核验索引

本文所有架构论断、调度策略、阈值常量与算子边界均对照以下本地开源仓库核验：

| 仓库 | 本地核验基线（短 Commit） | 核心源码索引 |
| --- | --- | --- |
| **Daft** (`Eventual-Inc/Daft`) | `1e7eef89` | `daft/runners/flotilla.py`（`RaySwordfishActor`、`64 MiB` 合并）、`src/daft-local-execution/src/{run.rs, pipeline.rs, channel.rs, intermediate_ops/udf.rs, streaming_sink/async_udf.rs, sinks/grouped_aggregate.rs}`、`src/daft-distributed/src/{scheduling/scheduler/default.rs, scheduling/scheduler/scheduler_actor.rs, pipeline_node/shuffles/translate_shuffle.rs, pipeline_node/vllm.rs}`、`src/daft-shuffles/src/{oneshot_writer.rs, shuffle_cache.rs}`、`src/daft-checkpoint/src/{store.rs, scan.rs}`、`src/daft-logical-plan/src/optimization/optimizer.rs`、`src/daft-schema/src/dtype.rs`、`src/daft-parquet/src/reader/chunk_source.rs` |
| **Ray** (`ray-project/ray`) | `3fb778d3` | `python/ray/data/llm.py`、`python/ray/llm/_internal/batch/processor/vllm_engine_proc.py`、`python/ray/data/_internal/execution/{streaming_executor.py, resource_manager.py, operators/join.py, operators/hash_shuffle_v2.py, operators/task_pool_map_operator.py}`、`python/ray/data/_internal/logical/optimizers.py` |
| **Apache Spark** (`apache/spark`) | `5d491f62` | `sql/core/src/main/scala/org/apache/spark/sql/execution/adaptive/{AdaptiveSparkPlanExec.scala, CoalesceShufflePartitions.scala, OptimizeSkewedJoin.scala, DynamicJoinSelection.scala}`、`sql/core/src/main/scala/org/apache/spark/sql/execution/python/ArrowEvalPythonExec.scala`、`core/src/main/scala/org/apache/spark/api/python/{PythonRunner.scala, PythonWorkerFactory.scala}`、`core/src/main/scala/org/apache/spark/scheduler/{Stage.scala, DAGScheduler.scala}` |
| **StarRocks** (`StarRocks/starrocks`) | `f977b53c` | 结合本站已核验系列：[架构与源码]({{< relref "2026-08-09-starrocks-architecture-and-internals.md" >}})、[CBO 优化器]({{< relref "2026-09-01-dive-starrocks-optimizer.md" >}})、[OLAP Hash Table 调优]({{< relref "2026-11-19-olap-hash-table-design-and-tuning.md" >}}) |
