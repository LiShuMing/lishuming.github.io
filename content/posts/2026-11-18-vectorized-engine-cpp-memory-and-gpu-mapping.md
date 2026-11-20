---
title: "向量化引擎的 C++ 内存底座与 CPU-GPU 异构映射机制"
slug: "vectorized-engine-cpp-memory-and-gpu-mapping"
date: 2026-11-18T11:00:00+08:00
lastmod: 2026-11-18T11:00:00+08:00
categories:
  - 数据库
  - 异构计算
tags:
  - C++
  - 向量化执行
  - Velox
  - DuckDB
  - Sirius
  - RAPIDS cuDF
  - 内存管理
description: "从数据面为何弃用 std::shared_ptr 转向侵入式智能指针 intrusive_ptr 出发，剖析现代 C++ 列式内核的内存池对齐与两阶段多态分发，并打通从 CPU SIMD 小批缓存驻留到 GPU SIMT 大批合并访存、RMM 流序显存池、NUMA 锁页内存与 PCIe 流水线掩盖的底层映射逻辑。"
draft: false
toc: true
math: true
---

翻开 **Meta Velox**、**ClickHouse** 或 **DuckDB** 的底层源码，熟悉现代标准 C++ 的工程师往往会在第一时间注意到几个略显“反教科书”的习惯：在高频流转的列存缓冲区（如 `velox::BufferPtr`、ClickHouse `ColumnPtr`）上，内核开发者几乎一致抛弃了标准库的 `std::shared_ptr`，转而自己维护一套侵入式智能指针（`intrusive_ptr`）；算子内部严禁直接调用系统的 `new` 或 `malloc`；到了向 GPU 卸载关系代数的 **Sirius** 与 **RAPIDS `libcudf`** 中，连最基础的 `cudaMalloc` 和主机普通堆内存也被列为性能禁区。

在上一篇 [CPU/GPU 异构计算中的 SQL/ETL 加速陷阱、算子设计与调度抉择]({{< relref "2026-11-17-cpu-gpu-heterogeneous-sql-etl-survey.md" >}}) 中，我们从宏观算术强度、算子算法与云数仓 TCO 账本讨论了何时该用 GPU；本文则将镜头拉回到更底层的系统编程切面，把 **C++ 内存所有权设计、CPU 向量化内核机制如何自然映射到 GPU SIMT 执行模型，以及跨越 PCIe 边界时的锁页内存与 NUMA 硬件细节** 逐一拆透。

![向量化引擎 C++ 侵入式内存底座与 CPU-GPU 三级异构流水线映射](/images/diagrams/db-cpp-intrusive-and-gpu-tiering.svg)

---

## 一、数据面内存句柄：为什么向量化内核弃用 `std::shared_ptr` 而选 `intrusive_ptr`？

在列式执行引擎中，切片（Slice）、字典包装（Dictionary Wrap）和跨算子投影每时每刻都在共享底层同一块不可变字节缓冲区，引用计数智能指针是实现零拷贝视图（Zero-Copy View）的刚需。

既然都是引用计数，`std::shared_ptr<Buffer>` 和 `boost::intrusive_ptr<Buffer>` 的分水岭究竟在哪？答案只有一句话：**引用计数器（RefCount）到底存放在对象外面，还是嵌在对象身体里面。**

### 1.1 胖指针与外部控制块的隐性税单

`std::shared_ptr<T>` 为了兼容任意无法修改源码的第三方类型乃至基础类型 `int`，采用**非侵入式（Non-intrusive）**设计。它无法往 `T` 体内塞字段，只能在对象外部单独开辟一块**控制块（Control Block）**，用来存放强引用计数 `strong_count`、弱引用计数 `weak_count`、虚表指针以及自定义删除器（Deleter）。因此，栈或容器里的每一个 `std::shared_ptr<T>` 变量实际上都是一个占 **16 字节的双指针胖结构（`{T* ptr, ControlBlock* ctrl}`）**。

而 `intrusive_ptr<T>` 采用**侵入式（Intrusive）**契约，要求类 `T` 自己继承引用计数基类，在成员变量里直接内嵌一个 `std::atomic<int32_t> referenceCount_`。这样一来，外部不需要任何控制块，`intrusive_ptr<T>` 自身退化为纯粹的 **8 字节单指针（`{T* ptr}`）**，物理尺寸与裸指针分毫不差。

这时很多人会立刻追问：**“C++11 早就提供了 `std::make_shared<T>()`，可以把控制块和对象本体合并进同一次连续内存分配里，为什么 Velox 的 `BufferPtr` 还是坚持不用？”**

只要亲手写一次列存 `Buffer` 的分配代码，就会立刻撞上 `std::make_shared` 跨不过去的五道硬墙：

1. **`make_shared` 无法支持“定长头部 + 变长连续数据区”的自定义内存池分配**：
   在向量化引擎里，一块 `64 KB` 的列缓冲区通常采用 **“头部 `Buffer` 元数据结构体 + 紧随其后的变长裸字节数组 Payload”** 紧凑布局——先向受统一配额管控的 `MemoryPool` 一次性切出 `sizeof(Buffer) + 64KB` 的连续对齐内存，再用 **Placement `new`** 在头部原地构造 `Buffer` 对象。
   这条路径彻底堵死了 `std::make_shared<Buffer>()`，因为 `make_shared` 只能分配编译期固定大小 `sizeof(Buffer)` 的内存，没法在对象尾部动态捎带 `64 KB` 数据区！而如果你退一步，先从 `MemoryPool` 拿到构造好的裸指针 `Buffer* raw`，再交给 `std::shared_ptr<Buffer>(raw, pool_deleter)` 托管——**`shared_ptr` 构造函数会背着你在后台偷偷调用全局 `operator new`，去系统堆上单独分配那 24–32 字节的 Control Block！** 这一下直接击穿了内核“数据面严禁触碰全局堆分配器、杜绝全局锁竞争与内存碎片”的铁律。反观 `intrusive_ptr`，计数器本就在 `Buffer` 头部结构体里，与尾随的 `64 KB` 数据在同一次 `MemoryPool` 切分中一步到位，真正做到 **0 次额外堆分配**。
2. **缓存行密度翻倍与寄存器传参 ABI 优势**：
   面对一张 200 列的大宽表，每个批次都要传递 `std::vector<BufferPtr>`。8 字节的 `intrusive_ptr` 让单个 `64B` Cache Line 能装下 **8 个**列句柄，而 16 字节的 `shared_ptr` 只能装 **4 个**。同时在跨函数传参时，体积减半的单指针显著减轻了栈拷贝与指令开销。
3. **归零后挂回空闲链表“原地复活”（Object Pool Recycling）**：
   当一个临时 `Buffer` 的引用计数减到 `0` 时，引擎往往不想销毁它，而是希望直接把它塞回 `MemoryPool` 的空闲块链表给下一个批次复用。在 `intrusive_ptr_release(Buffer* p)` 钩子里，计数归零时只需把 `p` 挂回空闲池，下次取出时把体内的 `referenceCount_` 重新置为 `1` 即可原地满血复活；而 `std::shared_ptr` 的外部控制块一旦强引用归零便不可逆地走向销毁，下次复用不得不重新分配新控制块。
4. **从裸指针 `this` 恢复智能指针的零成本安全**：
   计数器跟着对象本体走，意味着在任何成员函数或 C 接口回调里拿到裸指针 `Buffer*`，随时调用 `intrusive_ptr<Buffer>(this)` 都是绝对安全的；而在 `std::shared_ptr` 下直接包装裸指针会制造出双控制块并引发致命的 **Double Free**，若为了安全去继承 `std::enable_shared_from_this<T>`，又得在每个对象体内白白多塞进一个 16 字节的 `std::weak_ptr`。
5. **剔除无用的 `weak_count` 与定制原子内存序**：
   高频流转的数据缓冲从不需要弱引用观察，`std::shared_ptr` 每次析构时额外维护和原子递减 `weak_count` 纯属空耗。自研侵入式计数只需维护唯一的 `int32_t`，递增用最轻量的 `memory_order_relaxed`，递减用 `memory_order_acq_rel`，甚至在明确由单个 `PipelineDriver` 线程独占的局部结构上可以切换为非原子普通整数，彻底省去硬件 `lock xadd` 指令锁开销。

当然，这并不意味着内核里完全不用 `std::shared_ptr`——在低频创建的查询控制面（如 `PlanNode`、`QueryCtx`、异步任务树以及需要 `std::weak_ptr` 打破父子循环引用的 `MemoryPool` 层级节点）上，`std::shared_ptr` 依然是兼顾安全性与工程可读性的标准选择。**控制面重安全用 `shared_ptr`，数据面抠字节用 `intrusive_ptr` + 轻量 `span/string_view` 切片**，构成了现代 C++ 引擎清晰的分界线。

---

## 二、内存池对齐与“外虚内实”的两阶段多态

除了智能指针，C++ 基本功在向量化内核里还集中体现在两处高频设计上：

第一是**生命周期对齐的分配器与伪共享防御**。算子处理过程中产生的大量短命中间字符串与哈希节点，普遍交由绑定批次或查询生命周期的 **`Arena`（线性指针碰撞分配器，分配仅需一次指针加法，结束时整块一次性回退归零）** 与页级 **Slab / Buddy Pool** 接管，并在多线程并发写入的局部统计结构上严格标注 `alignas(64)`，防止相邻线程修改同一 `64B` 缓存行引发多核 MESI 协议失效风暴（False Sharing）。

第二是**平衡编译膨胀与运行效率的“外层虚函数 + 内层模板特化”架构**。数据库算子既要支持动态组合任意执行计划树，又要应对 `int8 / int32 / int64 / double / decimal / string` 等十几种物理类型的排列组合。如果每一行都调一次虚函数 `Value eval(Row)`，分支预测失败与虚表跳转会直接吃光 CPU；反过来，如果把整棵计划树全部用 C++ 模板拼死，二进制体积会膨胀到几百 MB 并撑爆 `32 KB` 的一级指令缓存（`L1i Cache`）。

向量化引擎给出的标准解法是按 **Batch 边界切开多态层次**：
- **算子间调度走虚函数**：`Operator::getOutput()` 保持清晰的面向对象虚接口——因为每次调用一口气吐出 `4,096` 行，虚函数跳转开销被摊薄了四千倍，几乎可以忽略不计。
- **算子内循环走编译期模板特化**：进入算子内部后，立刻通过一层 `TypeSwitch` 分发跳进针对具体物理类型实例化的紧凑模板函数 `evalTyped<int64_t>(const int64_t* __restrict__ in, ...)`。循环体内没有任何类型分支与虚调用，加上 `__restrict__` 消除指针别名顾虑，编译器可以顺畅地自动展开循环并生成满血的 AVX2 / AVX-512 或 ARM NEON 指令。

---

## 三、从 CPU SIMD 到 GPU SIMT：列式内核机制的自然映射

理解了上面这套 CPU 列式向量化底座，再去审视 **Sirius**（挂载于 DuckDB 之上的开源 GPU 原生引擎）或 **NVIDIA `libcudf`**，就会发现：**GPU 分析引擎并不是推翻重来的外星物种，而是把 CPU 列式向量化的核心设计沿着硬件并发度放大了一千倍后的自然延伸。**

只要抓住硬件微架构的四个核心差异，就能把熟悉的 CPU 算子精准映射到 GPU 上：

| 内核设计维度 | CPU 向量化内核（StarRocks / DuckDB / Velox） | GPU SIMT 内核（Sirius / `libcudf`） | 跨越硬件边界的底层原因 |
| :--- | :--- | :--- | :--- |
| **最佳批次规模（Batch Size）** | **`1,024 ~ 4,096` 行**（几十 KB 至几百 KB） | **`100,000 ~ 数百万` 行**（数十 MB 至数百 MB） | CPU 靠小批次常驻 **L1/L2 Cache** 保低延迟；GPU 靠海量活跃线程束切换掩盖 400+ 周期的 HBM 显存延迟 |
| **数据并行单元** | 单核 **SIMD 寄存器**（一条 AVX-512 指令算 16 个 `int32`） | **Warp 锁步执行**（32 个硬件线程锁步同跑一条指令） | 列存（SoA）在两端都是刚需：CPU 喂满连续向量寄存器，GPU 实现 `128B` 合并访存 |
| **过滤与条件裁剪（Filter）** | 维护 `SelectivityVector` 有效位图/下标延迟物化 | **无分支算位图 → 并行前缀和 → 流紧缩重排** | 规避 GPU **Warp 分支发散（Divergence）**，并保持后续算子的连续合并访存 |
| **哈希探测（Hash Join）** | `RowContainer` 紧凑行 + `__builtin_prefetch` 批量预取 | `cuco` 开放寻址 + **Cooperative Groups 协同探测** | CPU 靠软件预取榨取有限 MLP；GPU 靠数万并发在途请求喂饱 `2–3 TB/s` HBM 带宽 |

这四个映射里，最值得深挖的是**合并访存（Coalesced Memory Access）**与**前缀和流紧缩（Stream Compaction）**。

在 GPU 上，同一个 Warp 的 32 个线程在同一拍发出内存读取指令。如果数据按列连续存放（如 `cudf::column_view` 的 `int32_t*` 数组），线程 `0..31` 访问的恰好是紧挨在一起的 `128 字节` 对齐区间，显存控制器合并成 **1 次硬件内存事务**即可全员交付，带宽利用率 `100%`；倘若换成 `128 字节` 宽的行存（AoS），32 个线程各自隔着 128 字节跨步挑字段，硬件被迫拆成 **32 次独立内存事务**，高达 `3 TB/s` 的 HBM 有效吞吐当场暴跌至三十二分之一。这正是为什么 Arrow / DuckDB / Velox 的列式内存布局只需传递裸指针，就能与 GPU 算子实现天然零格式转换对接。

而在处理 `WHERE a > 10` 这类过滤算子时，CPU 引擎喜欢只给存活行套一层 `SelectionVector` 下标视图继续往下传；但在 GPU 上，如果让 Warp 里的 32 个线程直接进 `if (cond)` 分支，一旦一半线程为真、一半为假，锁步硬件只能把真假两条路径串行各跑一遍，且长期携带稀疏跳跃下标会彻底毁掉下游算子的合并访存。因此 GPU 引擎普遍采用经典的三步无分支流水线：先用一个极简 Kernel 并行算出全列 `0/1` 布尔掩码，再调用 `CUB::DeviceScan` 跑一遍**并行前缀和（Parallel Prefix Sum）**精准算出每个存活元素在新数组里的连续写入偏移，最后一步并行拷贝输出紧凑新列——用极其充裕的顺序显存带宽，换取下游算子零分支发散的满速运行。

---

## 四、跨越异构边界：RMM 流序显存池、锁页内存与 NUMA 亲和

算子跑通之后，真正的硬仗往往打在“显存不够装”和“PCIe 搬运太慢”这两道系统工程关卡上。哪怕在单机插满 `80 GB` 显存的高端卡上，面对几百 GB 的离线宽表关联，一旦内存与传输链路设计粗糙，系统不是直接 OOM 崩溃，就是被跨总线拷贝拖得比纯 CPU 还慢。

以 **Sirius** 结合 **RAPIDS Memory Manager（RMM）** 及分层框架 `cucascade` 的工程实践为例，一套抗压的异构内存底座必须扣紧以下四个环扣：

### 4.1 禁用原生 `cudaMalloc`，切入流序异步显存池

在 CUDA 编程中，原生的 `cudaMalloc` 与 `cudaFree` 属于**重量级设备同步操作**——每次调用不仅耗时数百微秒，还会隐式卡停当前 GPU 上的所有并发 Stream 流水线。对于每秒要分配释放上千次临时列缓冲区的查询引擎而言，直接调原生接口无异于自杀。

对应 CPU 内核里的 `Arena / Slab Pool`，GPU 侧必须通过 RMM 接管全部显存分配：启动时一次性划出大块显存池，并通过基于 CUDA Stream-ordered 分配器的 **`rmm::mr::cuda_async_memory_resource`** 将分配与释放动作直接绑定到具体的 `cudaStream_t` 队列上。前一个算子 Kernel 尚未执行完毕时，后续逻辑就可以把它的显存块异步排入同一条 Stream 的释放队列，紧接着的算子在同一条流上零同步锁、零等待地无缝复用同一片物理显存。

### 4.2 显存溢写（Spill）的两个硬件铁律：锁页内存与 NUMA 绑定

当活跃工作集突破 GPU HBM 容量水位时，引擎需要将冷数据块降级溢写到主机大内存（Host Tier），极端情况下再落向本地 NVMe 磁盘（Disk Tier）。在这条 `GPU HBM <-> Host RAM` 的溢写通道上，藏着两个极易被忽视的硬件陷阱：

1. **为什么主机缓冲池必须使用锁页内存（Pinned Memory / `cudaMallocHost`）？**
   C++ 普通 `malloc` 或 `new` 分配出来的是操作系统可随时换出或重映射物理页帧的可换页虚拟内存（Pageable Memory）。GPU 上的硬件 DMA 引擎要求物理地址在传输期间绝对锁定不变，因此**根本无法直接对普通堆内存发起 DMA 拷贝**！如果你拿普通堆指针去调 `cudaMemcpyAsync`，CUDA 驱动不得不在幕后偷偷干一件苦差事：先在驱动内部申请一块临时锁页缓冲区（Staging Buffer），**动用宝贵的 CPU 核心把你的数据用 `memcpy` 先拷进临时锁页区**，再通知 GPU DMA 来拉取。这不仅让 PCIe 实际吞吐直接腰斩、白白烧掉大量 CPU 周期，还会让所谓的“异步拷贝”在主机端退化成同步阻塞！因此，异构引擎的主机溢写池必须在初始化时显式分配锁页内存池。
2. **双路服务器下的 PCIe Root Complex 与 NUMA 亲和性**：
   在一台双路 CPU 插多张 GPU 的服务器上，`GPU 0` 物理挂载在 `CPU Socket 0` 的 PCIe 根复合体下，`GPU 1` 挂载在 `Socket 1` 下。如果 `GPU 0` 溢写时拿到的是分配在 `Socket 1` 远端内存控制器上的锁页内存，每一次 H2D / D2H 传输都必须横穿两颗 CPU 之间的互联总线（Intel UPI 或 AMD Infinity Fabric），不仅延迟陡增，还会挤占另一路 CPU 的访存带宽。因此，Sirius 在初始化主机内存层时，会严格按照拓扑为每张 GPU 单独绑定其所属本地 **NUMA 节点**的锁页内存池；在多卡协同洗牌时则优先走卡间高速 **NVLink P2P DMA**，彻底绕开主机内存。

### 4.3 折叠 PCIe 传输税：压缩态直传、多流重叠与防乒乓代价约束

面对 PCIe Gen4/Gen5 单向 `25–50 GB/s` 与 GPU 片上 HBM `2,000–3,350 GB/s` 之间近百倍的带宽鸿沟，除了依靠 **Substrait** 一次性卸载整棵连续子计划让中间结果全程驻留显存外，数据进出边界还依赖三项精细掩盖技术：

- **压缩页直传 + GPU 原位解压（`nvCOMP`）**：扫 Parquet 或 ORC 表时，绝不在 CPU 上先把 ZSTD / Snappy 数据解压膨胀 4 倍后再去挤狭窄的 PCIe 总线，而是直接把磁盘读上来的紧凑压缩字节流原样通过 DMA（或经由 **GPUDirect Storage `cuFile`** 绕过操作系统 PageCache 从 NVMe 直注显存）送上 GPU，利用 `nvCOMP` 在卡上以数百 GB/s 的吞吐并行解压，相当于把物理 PCIe 的有效传输带宽凭空拓宽了 3 到 5 倍。
- **Multi-Stream 三缓冲时间轴重叠**：将大表切分为适中的流式分块（如 `32–64 MB`），开辟多条独立 `cudaStream_t` 队列：当 `Stream 1` 正在通过 DMA 引擎搬运第 \(i+1\) 批输入（H2D）时，SM 计算核心正在 `Stream 2` 上全速处理第 \(i\) 批算子，而拷贝引擎的反向通道同时在 `Stream 3` 上回传第 \(i-1\) 批收敛结果（D2H），让总线搬运时间被计算窗口自然吞没。
- **杜绝单算子“乒乓回退（Ping-Pong Fallback）”**：一旦子计划中间夹杂了 GPU 不支持的长尾正则回溯或自定义函数，异构优化器在估算卸载收益时必须把边界插入的 `D2H + H2D` 往返时间与每次 `5–20 μs` 的 Kernel 启动开销一并计入总账。只要跨界拷贝惩罚吞噬了算子加速红利，就果断将整段子计划完整保留在 CPU 向量化管道内原地执行——守住这条代价红线，异构系统才能既有上限爆发力，又不失工程底线。

---

> **参考资料与源码基线**
> - **C++ 向量化内存与执行底座**：Meta Velox `velox/buffer/Buffer.h`（`boost::intrusive_ptr<Buffer>` 引用计数、池化回收与视图切片实现）· ClickHouse `COW<IColumn>::intrusive_ptr` 设计 · DuckDB `DataChunk` / `UnifiedVectorFormat` 与 `ArenaAllocator`。
> - **GPU 关系算子与三级显存架构**：**Sirius** (`sirius-db/sirius` 开源实现、Substrait 子计划拦截与基于 `cucascade` 的 GPU / NUMA-Local Pinned Host / Disk 三级内存管理，CIDR 2026) · NVIDIA **RAPIDS `libcudf`** (`cudf::column_view`, `apply_boolean_mask`)、**RMM** (`rmm::mr::cuda_async_memory_resource`)、**`cuCollections`** (`cuco::static_multimap` Cooperative Groups 探测) 与 **`nvCOMP` / GPUDirect Storage (`cuFile`)** 编程指南。
