---
title: "从 BSP 到 MPP 与列式卸载：Spark、Gluten/Velox 与 StarRocks 的内核权衡"
slug: "spark-bsp-gluten-velox-and-starrocks-execution"
date: 2026-11-18T10:30:00+08:00
lastmod: 2026-11-18T10:30:00+08:00
categories:
  - 数据库
  - 分布式系统
tags:
  - Apache Spark
  - Velox
  - Apache Gluten
  - StarRocks
  - 向量化执行
  - 查询优化器
description: "从跨 Shuffle 的 LIMIT 短路实验与超长 ETL 容错边界出发，拆解 Spark BSP 与 StarRocks MPP Pipeline 的调度分野，深入剖析 UnsafeShuffleWriter 重定位、FetchFailed 与 INDETERMINATE 静默错数陷阱、LLC/TLB/L1i 缓存缺失、Substrait 列式卸载、Runtime Filter 阶段代价，以及纯列式矢量化在 BSP 模型下的真实物理边界。"
draft: false
toc: true
math: true
---

假设手头有两张百亿行的事实表 `big_a` 和 `big_b`，我们在禁用广播连接的前提下提交这样一条 SQL：

```sql
SELECT /*+ SHUFFLE_JOIN(a, b) */ a.id, b.val
FROM big_a a
JOIN big_b b ON a.id = b.id
LIMIT 10;
```

把同一条查询分别扔进 **StarRocks** 与 **Apache Spark**，会看到截然相反的运行轨迹。

在 StarRocks 中，查询启动的瞬间，所有扫描与连接分片（`PlanFragment`）在全集群同时就位并建好内存网络管道。`ScanNode` 刚读出前几千行推入 `ExchangeSink`，下游 `HashJoin` 瞬间凑齐 10 条匹配记录送达根节点；协调器（Coordinator）拿到这 10 行后立刻向全集群广播 `Cancel` 信号，百亿行大表查询在几十毫秒内提前收工。

而在原生 Spark 中，上游两个扫描阶段（`ShuffleMapStage`）根本不知道下游凑够 10 条就会停。引擎必须老老实实把两张百亿大表全量扫完、切分、排序、写满磁盘 Shuffle 文件，跨过全局同步屏障后才拉起下游 `Reduce Stage` 取出前 10 行。

反过来，如果把集群缩减到只有 100 个 CPU 核，却要硬啃一场持续 4 小时、洗牌 50 TB 数据的超宽 ETL 作业，并在跑到第 99 分钟时拔掉其中一台机器的网线——全内存流水的 MPP 引擎因为中间状态早已流过管道无法重放，只能整条 SQL 报错重头再来；而把作业切成 50,000 个小分片轮流调度的 Spark，却只需重算丢失的那几十个分片，稳稳跑完全程。

这两个极端场景直接划出了 **BSP（Bulk Synchronous Parallel，批量同步并行）** 与 **MPP（Massively Parallel Processing，大规模并行处理）** 在时间与空间调度上的底层分野：前者像“接力赛加驿站存包”，用磁盘屏障换取分时复用（Time-sharing）与局部容错；后者像“全自动工厂流水线”，靠全员同时在线（Gang Scheduling）与端到端内存反压（Backpressure）博取极致的低延迟与吞吐。

近几年，两条路线在湖仓一体架构下剧烈碰撞：Spark 社区借助 **Apache Gluten + Meta Velox** 把 Stage 内部改造成了微缩版 C++ 列式流水线，StarRocks 等 MPP 引擎则陆续补齐算子级落盘（Spill）与存算分离容错。顺着数据在内存、缓存、磁盘与网络中的真实流动路径把这三套内核放在一起对照，许多看似孤立的调优经验与线上事故都会水落石出。

![Spark BSP、Gluten/Velox 列式卸载与 StarRocks MPP 流水线执行架构对比](/images/diagrams/db-bsp-mpp-gluten-starrocks-arch.svg)

---

## 一、BSP 屏障背后的精密账本：Shuffle 写入器与 `INDETERMINATE` 容错深坑

既然 BSP 必须在宽依赖边界切断流水线并把数据落盘，Shuffle 自然成了 Spark 内核打磨最深、也最容易藏匿隐患的地带。

### 1.1 为什么 Spark SQL 永远不走 Shuffle 层的 `mapSideCombine`？

翻开 `SortShuffleManager` 源码，会看到三种写入分支：`BypassMergeSortShuffleWriter`、`UnsafeShuffleWriter`（Tungsten 指针排序）以及兜底的 `SortShuffleWriter`。许多教材会讲“需要 Map 端预聚合时走第三种 `SortShuffleWriter`”，但如果去抓任意一条 Spark SQL（哪怕是标准的 `SELECT k, SUM(v) FROM t GROUP BY k`）的物理计划，会发现底层的 `ShuffleDependency.mapSideCombine` 永远是 `false`。

原因在于 RDD 时代与 SQL 时代的职责剥离。早期 `rdd.reduceByKey` 没有独立聚合算子，只能把聚合函数塞进 Shuffle 框架，强迫 `ExternalSorter` 用堆内 Java 对象哈希表（`PartitionedAppendOnlyMap`）边收数据边合并，既慢又引发严重 GC。而在 Spark SQL 中，Catalyst 优化器直接把 `GROUP BY` 拆成算子层的两步：

```text
Scan -> HashAggregateExec(partial_sum) -> ShuffleExchangeExec -> HashAggregateExec(final_sum)
```

Map 端的局部预聚合已经在进入 Shuffle 之前，由 `HashAggregateExec` 利用堆外紧凑哈希表 `BytesToBytesMap` 配合代码生成极速完成了。等到数据流进 `ShuffleExchangeExec`，Shuffle 层只管纯粹地按分区搬运 `UnsafeRow` 字节块。因此，在整个 Spark SQL 体系内，第三种对象级 `SortShuffleWriter` 实际上已被架空，真正的较量只发生在 `BypassMergeSort`（默认分区数 \(\le 200\)）与 `UnsafeShuffleWriter`（分区数 \(> 200\)）之间。

### 1.2 “可重定位序列化对象”契约与 `Unsafe` 对 `Bypass` 的降维打击

要启用 `UnsafeShuffleWriter`，序列化器必须满足一个听起来颇为拗口的条件：`supportsRelocationOfSerializedObjects == true`（支持重定位序列化对象）。序列化之后不就是一串 `byte[]` 吗，难道剪下来换个位置贴上去还会坏掉？

拿 Java 原生的 `ObjectOutputStream`（`JavaSerializer`）走一遍两行写出就能看到问题所在。假设一个 Map 任务先后收到两条记录：第 1 条属于分区 1（`Person("Hangzhou")`），第 2 条属于分区 0（`Person("Shanghai")`）。`ObjectOutputStream` 为了节省体积，会在流开头写魔数 `0xACED0005`，并在内部维护一张跨对象的引用句柄表（Wire Handle Table）：

```text
[StreamHeader: 0xACED0005]
├── 记录 1 (Partition 1): [完整写入 Person 类结构元数据 ClassDesc，分配句柄 #1] + ["Hangzhou"]
└── 记录 2 (Partition 0): [仅写 5 字节反向引用标记: TC_REFERENCE #1] + ["Shanghai"]
```

如果排序器按分区号重排，把属于分区 0 的“记录 2”原始字节切片直接搬到前面单独发给下游 Reducer，Reducer 一上来读到的就是无头无尾的 `TC_REFERENCE #1`——而 `#1` 的类定义留在分区 1 的字节流里根本没发过来，当场就会抛出 `StreamCorruptedException`。反观 Spark SQL 的 `UnsafeRowSerializer`，每条记录都是 `[4B 长度 + Null 位图 + 8B 定长槽位区 + 变长数据区]` 的 100% 自包含字节块，不依赖任何上下文状态；`KryoSerializer` 在默认开启每次写完自动 `reset()` 清空引用表时同样满足自包含契约。

一旦满足了“字节块可自由剪贴”，`UnsafeShuffleWriter` 就能在生产常见的大分区场景（如 `partitions = 4000`）下展现出压倒 `BypassMergeSortShuffleWriter` 的四个物理优势：

1. **终结并发文件句柄与压缩缓冲区爆炸**：`Bypass` 会为每个分区同时打开一个临时文件。如果单个 Executor 并发跑 32 个 Task、分区数为 4,000，单机同时打开的文件描述符高达 \(32 \times 4000 = 128,000\) 个；更致命的是每个文件流外层套着的 LZ4/ZSTD 压缩器各自占用 `64–256 KB` 滑窗缓冲，光空缓冲区就会吃掉超过 `12 GB` 不受控内存。而 `UnsafeShuffleWriter` 无论下游是 201 个还是 100 万个分区，同一时刻最多只写 **1 个** Spill 文件，仅持有一份压缩流。
2. **内存充足时少写整整一遍磁盘**：`Bypass` 哪怕只处理 `10 MB` 数据、机器空着 `50 GB` 内存，也会死板地先写进 \(P\) 个临时小文件（第一次落盘），最后再全部读出来拷贝拼接成最终 `.data` 文件（第二次落盘）。而 `UnsafeShuffleWriter` 只要数据装得进 `TaskMemoryManager` 分配的内存页，全程零临时文件（Zero-Spill），在内存排完指针后直接一次性顺序写出最终文件，省掉一半磁盘写入与数千次 VFS 元数据系统调用。
3. **小块随机碎写变连续大块顺序写**：消除了几十个线程同时向几万个文件每次刷几十 KB 造成的磁盘磁头抖动与 SSD FTL 写放大。
4. **\(O(N)\) 基数排序让排序开销几乎白送**：`ShuffleInMemorySorter` 排的不是庞大的行内容，而是每条记录仅占 8 字节的紧凑长整型指针（`[24-bit partitionId | 13-bit pageNumber | 27-bit offset]`），且底层针对 24 位分区号手写了 **LSD Radix Sort（最低位优先基数排序）**，顺序扫描连续 `long[]` 数组，L1/L2 Cache 命中率极高，排一千万条记录仅需几十毫秒。

### 1.3 `FetchFailed` 状态机与 `INDETERMINATE` 静默丢数悬案

当 Shuffle 文件写完、下游 Reduce Task 通过 `ShuffleBlockFetcherIterator` 拉取块失败时，Executor 会抛出携带坏节点坐标的 `FetchFailedException`。与普通算子报错重试 4 次挂掉作业不同，`TaskSetManager` 遇到 `FetchFailed` 不会累加 Task 代码失败次数，而是立刻将当前 `TaskSet` 标为僵尸态（`isZombie = true`），交由 Driver 端的 `DAGScheduler.handleTaskCompletion` 处理：先去 `MapOutputTrackerMaster` 注销坏节点上的失效 Map 输出（整机失联时直接连坐注销该 Host 上的全部历史输出），再故意等待 `200 ms` 窗口把同一波宕机引发的几百次异常合并，最后调用 `resubmitFailedStages` 回头只补跑父 Stage 缺失的那几个分片。

这套局部补跑机制看似天衣无缝，却在社区引爆过著名的静默错数漏洞（SPARK-23207 / SPARK-23243 / SPARK-25341）。要看懂它，只需推演一条最常见的三阶段流水线：`Stage 0 -> Shuffle -> Stage 1 (执行 df.repartition(2)) -> Stage 2`。

Spark 为每个 RDD 定义了三种确定性级别：`DETERMINATE`（重算后内容和顺序完全一致）、`UNORDERED`（重算后集合相同但行到达顺序可变，所有读取多节点网络包的 `ShuffledRDD` 天然属于此类）和 `INDETERMINATE`（重算后连落入某分区的元素集合本身都会变）。当 `Stage 1` 从网络异步拉取 `Stage 0` 的数据块时，行到达顺序是 `UNORDERED`；紧接着它执行无参 `repartition(2)`（底层是按到达先后轮询发牌的 `RoundRobinPartitioning`）——**无序输入撞上位置敏感的轮询发牌，输出瞬间裂变成了 `INDETERMINATE`**！

看看局部重算在这里会酿成什么惨剧：
1. `Stage 1` 的 `Task 1.0` 第一次执行时，网络包到达顺序是 `[A, B]`，轮询发牌把 **`A` 分进分区 0，`B` 分进分区 1**。
2. 进入 `Stage 2`：读分区 0 的 `Reduce 2.0` 跑得飞快，顺利拿走 `A` 并成功落盘结束；此时 `Task 1.0` 所在机器突然掉线，读分区 1 的 `Reduce 2.1` 撞上 `FetchFailed`。
3. 如果 `DAGScheduler` 只回头补跑丢失的 `Task 1.0 (Attempt 1)`，这次网络到达顺序变成了 `[B, A]`，轮询发牌反转：**`B` 进了分区 0，`A` 进了分区 1**。
4. 随后补跑的 `Reduce 2.1` 去分区 1 读走了 **`A`**！最终结果里，`Reduce 2.0` 和 `Reduce 2.1` 各自输出了一份 `A`，而 **`B` 人间蒸发**，作业却绿灯显示 `SUCCEEDED`。

为了堵死这个正确性黑洞，现代 Spark 内核立下了严苛规矩：一旦失效的父 `ShuffleMapStage` 被判定为 `INDETERMINATE`，不仅父 Stage 所有分片必须全量作废重算，下游已经跑完 9,999 个任务的活跃子 Stage 也必须全部回滚重来；如果下游已经是正在往 HDFS/对象存储提交文件的最终 `ResultStage` 且部分任务已落盘，因外部副作用无法撤回，`DAGScheduler` 只能当场调用 `abortStage` 杀死整条作业（Spark 3.4+ 虽对确定性源上的 `RoundRobin` 增加了局部排序兜底，但只要链路混入 `rand()`、UUID 或非确定性 UDF，这道紧箍咒依然随时生效）。这也是为什么在大规模集群引入具备主从双副本与 `Revive` 机制的远程 Shuffle 服务（如 Apache Celeborn）后，把故障拦截在存储层不向上抛 `FetchFailed`，能够直接消灭掉大量整夜重算的生产事故。

---

## 二、跨越硬件缓存墙：Tungsten 编译执行与列式向量化的正面交锋

搞定了跨节点的 Stage 调度，再把镜头推进到单个 CPU 核心内部。Spark 2.x 引入堆外 `UnsafeRow` 与全阶段代码生成（Whole-Stage CodeGen）之后，已经消除了火山模型逐行 `Iterator.next()` 的虚函数跳转和 JVM 对象头开销；为什么换上 **Gluten + Velox** 或切到 **StarRocks**，在 CPU 算子上依然经常能跑出 2 到 5 倍的提速？

答案藏在现代处理器的多级缓存体系与乱序执行窗口里。

### 2.1 数据库内核视角下的六类硬件缓存缺失（Cache Miss）

用 `perf stat` 剖析一条慢查询时，笼统地说“Cache 不友好”并不能指导优化。内核研发通常将缓存缺失拆解为成因维度的 **4Cs 模型** 与硬件功能维度的两类特殊缺失：

| 缺失类别 | 物理成因 | 计算引擎典型触发点与内核解法 |
| :--- | :--- | :--- |
| **强制缺失（Compulsory / Cold Miss）** | 某个 `64B` Cache Line 首次被加载进缓存 | 顺序扫描冷列页；依靠连续内存布局（`FlatVector`）唤醒硬件流式预取器即可克制 |
| **容量缺失（Capacity Miss）** | 活跃工作集超出各级缓存物理容量（L1d 约 `32–48KB`，L2 约 `0.5–2MB`，共享 L3/LLC 约数十 MB） | 百 MB 至数 GB 级大表 `HashJoin` 探测；靠 Radix 分区切小块、Runtime Filter 削减构建侧、紧凑行容器压缩体积 |
| **冲突缺失（Conflict Miss）** | 组相联映射（Set-Associative）下，相隔 `2` 的幂次跨度的地址反复映射到同一组互相驱逐 | 列存数组按固定 `4096B` 对齐步长跨列跳跃读取；通过槽位 Padding 或错开哈希桶对齐规避 |
| **一致性缺失（Coherency Miss / 伪共享）** | 多核并发修改同一 `64B` 缓存行内的不同变量，MESI 协议频繁使对端核心缓存行失效 | 多线程共享统计计数器或相邻状态槽；采用线程局部预聚合，共享原子变量强制 `alignas(64)` 独占缓存行 |
| **指令缓存缺失（L1i Cache Miss）** | 机器码体积撑爆仅 `32–64KB` 的一级指令缓存，导致 CPU 前端取指停顿 | Whole-Stage CodeGen 融合超大宽表生成巨型函数；需拆分算子边界或改用向量化紧凑算子循环 |
| **页表缓存缺失（dTLB Miss）** | 大内存随机跳转超出默认 `4KB` 页的 TLB 覆盖半径，触发昂贵的多级页表遍历（Page Walk） | 数十 GB 哈希表随机探测；在内存池上启用 `2MB` **HugePages**（如 `madvise(MADV_HUGEPAGE)`） |

在这张表里，决定关联查询生死的是 **LLC Cache Miss（末级缓存缺失，通常即 L3 Miss）**。当 CPU 依次穿透 L1（`~1 ns`）、L2（`~4 ns`）和 L3/LLC（`~15 ns`）都未命中时，必须跨内存控制器访问主存 DRAM，延迟瞬间飙升至 **`70–100+ ns`（约 200 到 300 多个时钟周期）**。

### 2.2 为什么 `UnsafeRow` + CodeGen 扛不住 LLC Miss 与宽表退化？

Spark 的 Whole-Stage CodeGen 将一串窄依赖算子捏合成一个大 `for` 循环，逐行把字段装在寄存器里往下推。这套模式在简单过滤加窄表聚合上非常犀利，但一撞上复杂负载就会暴露三重物理短板：

1. **内存级并行度（MLP）不足，DRAM 延迟完全裸奔**：在逐行流水线里，当前行走到 `HashJoin` 探测大哈希表触发 LLC Miss 时，后续算子强依赖这一行的探测结果，CPU 乱序执行窗口很快被塞满并原地停顿 200 多个周期。而 Velox 与 StarRocks 以 `1,024–4,096` 行为一个 Batch，探测前先遍历数组批量发出几十条 `__builtin_prefetch` 软件预取指令，让内存控制器同时挂起多笔并发 DRAM 请求，把两百多个周期的等待时间彻底重叠掩盖。
2. **8 字节槽位对齐浪费与 SIMD 绝缘**：`UnsafeRow` 规定所有定长列一律占满 8 字节槽位（哪怕是 `boolean`、`int8`、`int32`），不仅让有效内存带宽缩水一半，而且同行不同列交错排列（AoS），即便 HotSpot 拥有 SuperWord 自动向量化，也几乎无法生成 AVX2/AVX-512 单指令多数据流代码。列式引擎把同一列的 `int32` 紧挨着铺满连续内存，一条 `512-bit` 指令即可同时算完 16 行过滤或哈希，还能直接在字典编码（`DictionaryVector`）和常量向量上跳过解压直接求值。
3. **Janino `64KB` 字节码天花板与 JIT 退化**：遇到几百列的大宽表或几十层 `CASE WHEN`，CodeGen 拼出的单个 Java 方法极易突破 HotSpot C2 编译器的内联与编译上限（`HugeMethodLimit = 8000B`），一旦退化回字节码解释执行（甚至突破 JVM `64KB` 方法硬限制被迫切碎），吞吐会瞬间暴跌一个数量级，同时伴随着严重的 `L1i` 指令缓存抖动。

### 2.3 Gluten + Velox 的卸载闭环：Substrait、统一内存桥与语义鸿沟

为了在保留 Spark BSP 容错壳的同时换掉底层 JVM 计算核，**Apache Gluten** 通过 Spark 的 `ColumnarRule` 物理计划扩展点切入：先对算子树做合法性校验（检查算子、数据类型、函数签名与配置模式），将连续可卸载的最大子树翻译成跨语言标准中间表示 **Substrait Protobuf**，经 JNI 下发给 C++ 侧组装为 `velox::core::PlanNode` 管道交由 `velox::exec::Driver` 执行。

这层“JVM 壳套 C++ 核”的架构要想在生产站稳，必须解决三道边界难题：

- **拒绝静态切分死水池的统一内存仲裁**：如果给 JVM 划一半内存、给 C++ 划一半内存，两侧极易出现“一边闲置、另一边 OOM”。Gluten 通过 `ReservationListener` 将 Velox 的 `MemoryPool` 桥接到 Spark 的 `TaskMemoryManager`（以 `8MB` 等步长按需向堆外执行内存池动态申领）；当配额见底时，回调触发 Velox 的 `SharedArbitrator`，唤醒同节点上的 `HashBuild`、`OrderBy`、`Aggregation` 等算子将内存状态溢写（Spill）到磁盘，实现单进程内统一水位核算。
- **警惕碎片化回退（Fallback）引发的转置灾难**：倘若一条十算子流水线正中间夹了一个未支持的 Hive UDF 或冷门窗口函数，物理计划就会被拦腰截断，被迫插入 `VeloxColumnarToRowExec` 与 `RowToColumnarExec`。几千万行数据在列存向量与 `UnsafeRow` 之间做矩阵转置、重算 Null 位图并跨 JNI 边界来回拷贝，付出的代价往往比两侧算子加速省下的时间还要高得多。
- **抹平非标准 SQL 的历史语义鸿沟**：Velox 脱胎于遵循严格 ANSI SQL 的 Meta Presto，而 Spark SQL 背负着大量宽松语义包袱。例如非 ANSI 模式下 Spark 规定 `1 / 0 = NULL`、`CAST('abc' AS INT) = NULL`，而 Presto 默认抛异常；更棘手的是浮点数语义——Spark 违反 IEEE 754 标准规定 `NaN == NaN` 为真且 `NaN > Double.PositiveInfinity`，导致 C++ 侧底层的 SIMD 浮点比较指令与哈希分桶逻辑无法直接照搬，必须在 Velox 内专门维护一套独立对齐的 Spark 函数注册表与特殊分支补丁。

---

## 三、优化器视野与运行时过滤：多跑两个 Stage 与“顺手捎带”的代差

换掉单机算子只能追平单核指令效率；当查询升级为 TPC-DS 级别的十几张表星型/雪花关联时，**Spark Catalyst** 与 **StarRocks Cascades CBO** 在全局规划与运行时反馈上的架构代差就会浮出水面。

Catalyst 的核心骨架是一套按批次顺序执行的规则重写引擎（Rule-based Batches）：逻辑优化阶段做谓词下推、列裁剪，虽然配有基于动态规划的 `CostBasedJoinReorder`，但强依赖外部手动触发的 `ANALYZE TABLE`，且 RBO 改写与 CBO 择优相互割裂，很难在同一个搜索空间里同时权衡“提前部分聚合 vs. 交换 Join 顺序 vs. 命中异步物化视图”。而 StarRocks 基于经典的 **Cascades / Memo** 框架构建全功能 CBO，利用紧凑的等价类森林（`Group` 与 `GroupExpression`）配合自动采集的多桶直方图（Histogram）及多列联合基数估算，在同一次自顶向下搜索中统一敲定最优连接树形、数据分布策略（Colocate 无网络本地连接、Bucket Shuffle 仅移动右表、Broadcast 或全局 Shuffle）以及 SPJG 透明物化视图改写。

两者差异最直观的缩影，莫过于 **Runtime Filter（运行时动态过滤）** 的底层实现。

很多人听说 Spark 3.3+（一直演进到如今的 Spark 3.5 / 4.0）也支持了基于布隆过滤器的 `InjectRuntimeFilter`（注入 `BloomFilterMightContain`），便以为它和 StarRocks 的 Runtime Filter 是一回事。但只要拆开物理执行图数一数 Stage 数量，就会看到 BSP 屏障带来的沉重包袱。

在 StarRocks 的 MPP 管道里，生成和投递 Runtime Filter **不需要增加任何额外 Stage，也绝不重扫小表**：主查询唯一的 `HashJoinBuild` 算子在消费右表构建内存哈希表的**同一个循环里**，顺手就把连接键塞进了 `In-Filter`、`MinMax` 或 `BloomFilter`，毫秒级异步直发同机左表扫描算子（或经协调器合并后广播给远端 `ScanNode`）。左表 `ScanNode` 不仅能拿它直接裁剪底层的分片索引、ZoneMap 与延迟物化字典页，还配有灵巧的**自适应等待超时机制**——右表构建快就稍等几十毫秒吃满过滤红利，右表构建慢就直接放行先扫，绝不死等。

而在 Spark 的 BSP 架构下，算子之间没有点对点内存通道，Driver 也无法伸手进 Executor 的 Join 算子内存里把刚建好的哈希表捞出来造过滤器。为了凑出这一个全局布隆过滤器，`InjectRuntimeFilter` 被迫在大表扫描之上硬塞进一棵完整的**聚合标量子查询（`ScalarSubquery` → `SubqueryExec`）**：

```text
SubqueryExec (将最终合并出的唯一 byte[] 拉回 Driver 再下发给大表任务)
 └─ ObjectHashAggregateExec (Final Merge：单点合并所有局部过滤器)
     └─ ShuffleExchangeExec (SinglePartition！强行把所有分区汇集到 1 个 Task)
         └─ ObjectHashAggregateExec (Partial Update：各分区构造局部 BloomFilter)
             └─ ReusedExchangeExec (若未命中 Exchange 复用，则直接把构建侧小表重扫一遍)
```

算一下这笔调度账：如果构建侧在主查询里恰好有可复用的 `ShuffleExchange`，命中 `ReuseExchange` 后仍需**额外多跑 1 个 `SinglePartition` 汇总 Stage**；如果构建侧在主查询里原本只是简单的 `Scan -> Filter`，无法复用交换节点，就得**额外多跑整整 2 个 Stage** 并把小表多读一遍。更关键的是，`SubqueryExec` 是一道硬阻塞屏障——必须等这 1 到 2 个额外 Stage 彻底跑完、把字节数组传回 Driver 之后，大表扫描的 Stage 才被允许下发启动，且过滤主要停留在扫描上方的 `FilterExec` 行级筛查（用于削减后续大表 Shuffle 体积），难以深入存储格式内部裁剪数据页。正因链路如此沉重，Spark 默认把构建侧注入阈值死死卡在 `10MB`（`creationSideThreshold`）以内，一旦统计信息失真导致误注入，多出来的 Stage 调度延迟反而会拖慢整条查询。

---

## 四、反常识追问：纯列式矢量化在 BSP 模型下一定最优吗？

看到这里，很容易得出一个线性推论：“既然 C++ 列式矢量化这么快，只要把所有算子和 Shuffle 全盘改成纯列存矢量化，就能通吃一切负载。”

然而，无论是慕尼黑工大（HyPer）与 CWI（VectorWise）在 VLDB 2018 联合发表的评测名篇 *《Everything You Always Wanted to Know About Compiled and Vectorized Queries But Were Afraid to Ask》*，还是 Meta Velox、Databricks Photon 与 Apache Gluten 在超大规模离线生产集群交出的昂贵学费，都指向同一个事实：**纯列式矢量化在 BSP 模型下绝非全场景最优；在特定物理边界上，果断退回紧凑行存或引入编译执行（CodeGen），恰恰是保住吞吐与内存稳定性的唯一解。**

### 4.1 万级分区宽表下的“列式 Shuffle 缓冲区矩阵爆炸”

StarRocks 的内存列式交换之所以轻快，是因为下游接收端只有几十个 BE 节点实例（扇出度 \(P \le 100\)）。可一旦进入离线 BSP 数仓，单个 Stage 动辄切分 \(R = 10,000\) 个分区，上游清洗宽表又常带有 \(C = 300\) 个特征列。

如果坚持在 Map 端按纯列存结构将 `RowVector` 拆往各个分区，每个分区、每一列都必须独立维护数据缓冲、空值位图（Validity Bitmap）以及变长偏移数组：

$$\text{单 Task 并发列缓冲数} \approx R \times C \times 2 = 10,000 \times 300 \times 2 = 6,000,000\text{ 个 Buffer}$$

哪怕每个缓冲区只预分配区区 `4 KB`，单个 Task 瞬间就会向操作系统索要 **`24 GB` 内存**，直接引爆容器 OOM！反之，如果为了保命把每个缓冲压扁到只存三五行，向量化批处理优势荡然无存，几百万次碎块 `realloc` 与极低压缩率的微型 LZ4 块会让 CPU 耗时反超原生 Spark。而在 `UnsafeRow` 行存模型下，300 列早已打包进连续字节切片，内存缓冲复杂度仅与行数 \(O(N)\) 线性相关，与 \(R \times C\) 彻底解耦。因此，现代 Velox 与 Gluten 在检测到高扇出宽表洗牌时，都会主动放弃纯列式切分，退回 `CompactRow` / `UnsafeRow` 紧凑行格式完成 Shuffle。

### 4.2 哈希连接与落盘内部的“列转行”妥协（`RowContainer`）

即便不看跨机洗牌，单看节点内部的 `HashJoin`、`HashAggregation` 与 `OrderBy`，纯列存也跨不过随机访存的物理铁律。

列存天生服务于顺序扫描；一旦进入哈希表冲突链探测或基于指针的归并排序，访存模式瞬间变成高度随机跳跃。倘若一张聚合表带了 20 个分组键与聚合状态列，纯列式哈希表每核对一行就要去 20 个天南海北的列数组里做随机偏移读取，实打实吃下 **20 次 CPU Cache Miss**。因此，Velox 专门在算子内核设计了混合行存结构 **`exec::RowContainer`**（定长槽位按行紧挨排列在一个 `64B` Cache Line 内，变长字符串追加进旁路 Arena，仅在向量化哈希探测入口保留列式提取接口），StarRocks 在处理多列复合键聚合与 Spill 落盘时同样会将多列打包序列化为紧凑行块——**扫描过滤用列存榨干 SIMD，状态驻留与随机探测切回行存守住 Cache Line**。

### 4.3 深层表达式物化税、多层字典退化与巨型字段稳定性

除此之外，在三类典型离线负载中，纯列式解释型矢量化还会撞上明显的软肋：

1. **超深表达式树与多路短路求值**：面对风控或特征工程里包含四五十个节点的复合算术表达式，解释型矢量化每算完一个子操作符都要分配并写回一个 4,096 行的临时 `FlatVector`，中间向量在 L1/L2 缓存里反复吞吐成了新瓶颈；遇到 20 层嵌套 `CASE WHEN` 且每个分支命中率仅 5% 时，纯矢量化层层构造 `SelectivityVector` 掩码再做散射合并（Scatter）的开销极重。此时，能够把中间变量钉死在寄存器里并天然支持 `if-else` 短路跳转的 **轻量级表达式 JIT 编译（LLVM 融合内核）** 明显更胜一筹。
2. **多层字典包装的间接寻址惩罚（Indirection Penalty）**：列式引擎常利用 `DictionaryVector(indices)` 包装过滤后的存活行以避免物理拷贝（延迟物化）。但如果历经多次过滤与展开后出现“字典套字典”，或者存活下标极度稀疏，下游算子每次通过 `indices[i]` 查表都会退化为昂贵的硬件随机收集指令（如 `vpgatherdd`），甚至比标量循环还慢。成熟内核必须根据存活率与唯一值占比动态判定何时果断调用 `flatten()` 物化回连续扁平列。
3. **巨型变长列（Giant JSON / Array）下的内存尖峰**：矢量化引擎习惯以固定 `4,096` 行为一批次流转。一旦业务日志里混入单行重达 `2 MB` 的原始 JSON 列，仅仅一个 `4,096` 行的 Batch 就会瞬间撑开 **`8 GB` 连续堆外内存**，在内存仲裁器来得及介入 Spill 之前直接击穿进程上限（必须引入按字节上限动态缩减行数的 Adaptive Batch Sizing 才能缓解）；反观 Spark 原生的逐行流式迭代器，同一时刻内存里只驻留当前正在处理的 1 行（`2 MB`），在脏数据横行的超长离线链路里反而拥有极强的抗爆能力。

### 4.4 协作式流水线调度的反压与死锁边界

最后，当我们将单机执行底座统一切向 StarRocks `PipelineDriver` 或 Velox `Driver` 这类“核数绑定线程池 + 用户态状态机协作调度”架构时，还必须在工程上守住三条并发底线：
- **严禁物理线程同步阻塞**：读对象存储、等网络回包、等 Runtime Filter 或等异步刷盘时，`Driver` 必须立即挂起状态机并绑定 `Future` 交出物理核，绝不能在算子内部做同步系统调用。
- **隔离计算池与刷盘回收池**：假设节点内存耗尽，所有计算线程都在等待释放内存，而负责执行异步 Spill 写盘释放内存的任务如果排在同一个工作队列末尾拿不到 CPU，整台机器就会陷入永久假死。Compute Pool、I/O Pool 与 Spill/Arbitration Pool 必须在物理线程上严格隔离。
- **破解“Spill 自身也需要内存”的悖论**：初始化 ZSTD 压缩器与多路归并输出缓冲本身就要新申请几十 MB 内存。如果在内存用到 `100%` 时才触发溢写，Spill 动作自己就会成为压死骆驼的最后一根稻草，因此必须在全局内存池中强制划出不可挪用的预留逃生水位（Reserved Spill Buffer）。

回看这场跨越十年的架构演进，BSP 与 MPP、编译执行与列式矢量化、行存与列存早已不再是非此即彼的阵营对立：外层用 BSP + 远程多副本 Shuffle 兜住万级分片的局部容错，Stage 内部在扫描过滤层跑满 SIMD 列式流水线，在复杂表达式层嵌入轻量 JIT 消除中间物化，在宽表关联、溢写与高扇出洗牌边界自适应切回紧凑行存——把每一项技术安放在它最顺应硬件物理规律的位置上，正是当代数据密集型内核设计的真正乐趣所在。

---

> **参考资料与源码基线**
> - **Apache Spark 内核源码**：`org.apache.spark.scheduler.DAGScheduler`（`handleTaskCompletion`、`resubmitFailedStages` 与 `DeterministicLevel.INDETERMINATE` 回滚检查，参见 SPARK-23207 / SPARK-23243 / SPARK-25341）· `org.apache.spark.shuffle.sort.UnsafeShuffleWriter` / `BypassMergeSortShuffleWriter` · `org.apache.spark.sql.catalyst.optimizer.InjectRuntimeFilter` 与 `BloomFilterAggregate`（Spark 3.3–4.0）。
> - **Apache Gluten & Meta Velox**：Pedreira et al., *Velox: Meta's Unified Execution Engine* (VLDB 2022) · `velox::exec::RowContainer`、`SharedArbitrator` 与 `CompactRow` Shuffle 实现 · Apache Gluten `ColumnarRule` 与 Substrait 计划转换架构文档。
> - **StarRocks 与工业界向量化引擎**：StarRocks FE Cascades Optimizer、BE `PipelineDriver` 调度器与全局 Runtime Filter 技术文档 · Behm et al., *Photon: A Fast Query Engine for Lakehouse Systems* (SIGMOD 2022)。
> - **编译执行与列式矢量化经典文献**：Kersten et al., *Everything You Always Wanted to Know About Compiled and Vectorized Queries But Were Afraid to Ask* (VLDB 2018) · Abadi et al., *Column-Stores vs. Row-Stores: How Different Are They Really?* (SIGMOD 2008)。
