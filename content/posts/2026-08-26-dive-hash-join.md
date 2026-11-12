---
title: "【源码】深入 Hash Join：从内存哈希表到分区 Spill"
date: 2026-08-26T00:00:00+08:00
lastmod: 2026-10-28T00:00:00+08:00
slug: "dive-hash-join"
categories:
  - 数据库
  - 大数据
tags:
  - Hash Join
  - StarRocks
  - Apache Doris
  - ClickHouse
  - DuckDB
  - Spill
description: "从连接状态机与分区不变量出发，结合 StarRocks、Apache Doris、ClickHouse、DuckDB 与 Velox 源码剖析内存哈希表布局、三值逻辑与分区 Spill 恢复协议，并在附录拆解 Spark Hash Join 边界。"
draft: false
toc: true
math: true
---

> 本系列第 1/3 篇 · Join 执行 · [下一篇]({{< relref "2026-09-10-dive-hash-sort-strategy.md" >}}) · 相关：[DOP 三次决策]({{< relref "2026-09-12-dive-dop.md" >}})

算法教科书里的 Hash Join 只有两句话：挑较小的一侧建哈希表（Build），再拿较大的一侧逐行查表（Probe），平均时间复杂度 `O(N + M)`。如果照着这个模型，直接拿 `std::unordered_map` 写一个算子放进列式分析引擎，第一步会在哪里撞墙？

不妨拿一组只有 5 行数据的迷你查询在脑海里推演一遍。假设右表 `customers` 有两行 `c_id = 42`、一行 `c_id = 7`、一行 `c_id = NULL`；左表 `orders` 带着 `c_id = 42`、非等值残余条件 `o.amount > c.credit_limit` 做 `FULL OUTER JOIN` 或 `NOT IN`（即 Null-Aware Anti Join），并且在建表建到一半时撞上了算子内存上限：

1. **输出膨胀与跨批断点**：一个 `c_id = 42` 命中多条右表记录，总输出规模是 \(\sum_k \text{count}_L(k) \times \text{count}_R(k)\)。向量化引擎每次最多向下游吐出 4,096 行（一个 Chunk/Block），一旦某条左表行的重复链把当前输出块塞满，算子绝不能丢掉剩下的匹配项，而必须把 `(probe_row_idx, build_chain_ptr)` 冻结在状态机里，等下游拉取下一批时接着扫链。
2. **未命中标记与 `NULL` 三值陷阱**：做 `RIGHT OUTER` 或 `FULL OUTER` 时，右表那行 `c_id = NULL` 虽然在 `=` 下永远配不上任何左表行，却绝不能在 Build 入口顺手过滤掉——它必须留到全部左表探测结束后原样补空输出；反过来做 `NOT IN` 时，右表只要出现这一个 `NULL`，所有左表未命中行的三值逻辑结果瞬间从 `TRUE` 翻转成 `UNKNOWN`，整个算子应当直接返回空集。
3. **半途落盘破坏全局不变量**：如果内存吃紧时天真地把“装不下的那一半右表”写到磁盘，先把内存里这一半和左表连完，灾难立刻发生——右表某行在内存轮次没配对成功，你怎么知道它在落盘的左表分片里也没配对？`FULL OUTER` 会吐出成堆假未命中行；更隐蔽的是，基于残缺内存表构建的 Runtime Bloom Filter 会把本该命中落盘分区的左表行提前在 Scan 层误杀，造成静默丢数。

把这三个故障现场串起来就会发现：**工业级 Hash Join 从来不是一次无状态的键值查找，而是一台横跨“连接键编码、重复链游标、命中位图（Matched Flags）与对称分区落盘（Spill）不变量”的有状态机器。** Build 阶段向 Probe 阶段交付的不是一个孤立容器，而是一份严密的状态契约——键怎么序列化、哈希种子与桶掩码如何对齐、`NULL` 走哪条旁路、未命中位记在哪里，以及一旦发生内存撤销（Revoke），两侧如何用完全一致的分区函数 `P(hash(k), level)` 在外存重新相遇。

下文结合 **StarRocks、Apache Doris、ClickHouse、DuckDB**（并对照 **Velox** 与文末 **[Apache Spark 附录](#spark-hash-join)**）的内核源码，顺着“内存布局与探测快路径 → 三值语义与并行构建 → 外存分区与递归恢复协议 → 数据倾斜边界与线上排障”的主线，拆解现代执行引擎如何把这台状态机压榨到极致。

---

## 一、内存哈希表布局：为什么不能只算原始表大小？

在深入各家源码前，先算一笔经常让线上作业意外 OOM 的内存账。许多工程师看到右表 Parquet 文件解压后只有 2 GiB，就以为给算子留 2.5 GiB 内存绰绰有余。实际上，Build 侧的工作集由七部分叠加而成：

$$
M_{\text{build}} \approx M_{\text{key}} + M_{\text{payload}} + M_{\text{directory}} + M_{\text{chain}} + M_{\text{flags}} + M_{\text{rf}} + M_{\text{arena\_frag}}
$$

为了把开放寻址或桶链的冲突率压低，目录数组（`M_directory`）的装载因子（Load Factor）通常控制在 `50% ~ 75%`，且容量向上取整到 2 的幂——仅一次跨阈值扩容就可能瞬间翻倍桶空间；每行还要附带 `4 ~ 8` 字节的冲突链偏移或元组指针（`M_chain`）、外连接的命中标记位（`M_flags`）、变长字符串内存池对齐碎片（`M_arena_frag`），以及构建期并存的 Runtime Filter（`M_rf`）。原始 2 GiB 的窄表，建完哈希表占到 `5 ~ 8 GiB` 司空见惯。

为了让单次探测尽可能停留在 CPU L1/L2 缓存内、减少随机 DRAM 跳转，现代列存引擎在“键怎么编码”和“桶与载荷怎么摆放”上演化出了清晰的流派分野。

### 1.1 连接键特化：把变长多列压进定长寄存器

通用数据库如果把所有连接键都包装成虚函数对象逐字段比对，CPU 流水线会被分支预测失败和间接寻址彻底拖垮。几乎所有高性能引擎都会在 Build 入口根据列类型与值域做**键归一化（Key Normalization）**：

| 连接键物理形态 | 典型编码表示 | 访存与指令收益 | 退化边界与代价 |
| --- | --- | --- | --- |
| **小整数或窄区间单键** | 原值下标或 `key - min_key` | 零哈希计算、零冲突，退化为数组直接寻址 | 值域跨度大但数据稀疏时会造成严重的空槽浪费 |
| **多列定长组合键** | 拼装为 `64 / 128 / 256-bit` 无符号整数 | 一次寄存器比较或 `memcmp` 即可判定整组键相等 | 需把各列 `NULL` 位及 `NULL`-safe 标志一并编入定长位宽 |
| **单列短字符串** | 内联定长整数或 `(len, prefix, ptr)` 引用 | 前缀不符立即短路，省去堆上拷贝 | 尾部 `\0` 填充可能引发编码歧义，且底层字符串块生命周期须锁死 |
| **任意变长多列键** | Arena 紧凑序列化或 128-bit 宽哈希 + 回表校验 | 兜底覆盖任意复杂类型与十进制/嵌套键 | 序列化 CPU、字符串池占用与碰撞回表 Gather 成本最高 |

StarRocks 在 [`join_hash_table.cpp`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/be/src/exec/join/join_hash_table.cpp#L154) 的 `JoinHashMapSelector` 中干脆把这件事拆成正交的两级决策：先由 `JoinKeyConstructorType` 决定怎么从输入列提取键（单键直取、定长位打包 `SERIALIZED_FIXED_SIZE`、或通用变长序列化），再由 `JoinHashMapMethodType` 结合 L2/L3 缓存容量与 Join 类型挑选索引结构（直接映射、稠密范围映射、线性链或桶链）。早期版本（`0fd27fd`）靠 `std::visit` 在编译期暴力展开约 80 种组合，每个实例要花掉约 8 秒编译时间；到了 `f977b53`，内核改用静态注册工厂表 `find_join_hash_map_factory()` 按 Chunk 级虚调用进入具体模板 `JoinHashMap<LT, CT, MT>`，既砍掉了编译膨胀，又保住了逐行热循环里的零分支内联。

ClickHouse 的 [`chooseMethod()`](https://github.com/ClickHouse/ClickHouse/blob/c631f591cef3ecc0fcbba8fff30d16cc7a67ec23/src/Interpreters/HashJoin/HashJoin.cpp#L304) 走了相似的精细特化路线：单数值键直走 `key8 ~ key64`，总宽不超过 16 或 32 字节的多列定长键压进 `keys128` / `keys256`，单字符串走专用池，其余复杂组合键计算无歧义序列化的 `UInt128` 宽哈希；同时按连接严格度把 Value 拆成单行 `RowRef`、多行链 `RowRefList` 与区间检索 `AsofRowRefs`（见 [`HashJoin.h`](https://github.com/ClickHouse/ClickHouse/blob/c631f591cef3ecc0fcbba8fff30d16cc7a67ec23/src/Interpreters/HashJoin/HashJoin.h#L357)），一旦构建期发现 `ALL` 连接右侧实际无重复键（`all_values_unique`），当场降级为单行快路径。

### 1.2 三大哈希表内存组织与 SIMD 预筛

键编码确定之后，Build 侧的目录与行数据究竟按行存还是按列存？把 DuckDB、Velox/Umbra 与 StarRocks/Doris/ClickHouse 的内存结构摊开对比，可以看到三种截然不同的局部性取舍：

![现代列存引擎三类内存哈希表布局与向量化探测路径](/images/diagrams/aug-exec-hash-join-memory-layout.svg)

- **开放寻址指针表 + 行式元组块（DuckDB）**：DuckDB 的 [`JoinHashTable`](https://github.com/duckdb/duckdb/blob/1c4ecd8138ae0c63c73957a411e65484301bb300/src/include/duckdb/execution/join_hashtable.hpp) 把连接键、残余条件列、输出载荷列、可选 `found` 布尔位以及 64 位哈希值统一打包进按行连续存放的 `TupleDataCollection`。构建未封板（Finalize）前，每行元组末尾槽存的是 Hash 值，元组块随时可以搬迁或落盘；进入 Finalize 后，开放寻址目录 `ht_entry_t` 的高 16 位存哈希盐值（Salt）、低 48 位存元组指针，若遇到同键重复行，就把元组末尾原来的 Hash 槽原地覆写成旧链头指针（Next Pointer）。这样一来，**不同键的哈希碰撞靠紧凑目录里的线性探测 + Salt 快速过滤（Salt 不等根本不去解引用随机元组指针），相同键的多行重复则沿元组内部指针串链**，各司其职。
- **SIMD 1-Byte Tag 组内探测与无链连续布局（Velox / Unchained）**：Meta Velox 的 `HashBuild` 将数据存入混合布局的 `RowContainer`（定长连接键与哈希列紧邻行头，变长字段进独立 Arena），上层目录借鉴 F14 思想，把哈希桶按缓存行对齐分组，提取哈希值的 1 字节指纹（Tag）集中摆放，探测时用一条 AVX2/NEON 指令并行比对 16 个槽位的 Tag，命中后才去核对 `RowContainer` 里的完整键；而 DaMoN 2024 提出的 Unchained Hash Table 更进一步，利用前缀计数把同桶所有元组紧凑排进连续数组，配合内嵌 Bloom Filter 彻底消灭冲突链指针跳转。
- **原生列存 Chunk + 稠密 `uint32` 行号链（StarRocks / Doris）**：StarRocks 与 Doris（[`join_hash_table.h`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/be/src/exec/common/hash_table/join_hash_table.h)）选择不做整表行列转置，直接保留列式 `build_chunk` / `Block`，额外分配两个紧凑的 `uint32_t` 数组 `first[bucket]` 和 `next[row_id]` 来表达桶头与同桶下一行。更巧妙的是**第 0 号哨兵行约定**：构建时先在每个 Build 列第 0 号槽塞入默认值/`NULL`，真实数据从行号 `1` 开始编号。这样 `0` 既代表冲突链终止符，又能在 `LEFT / FULL OUTER` 左表未命中时直接生成 `(probe_row, build_row = 0)` 索引对，让下游列式 Gather 算子走完全统一的向量化拷贝逻辑，热循环里连一句判断“是否补空”的 `if` 分支都不用写。

---

## 二、语义状态机与并行管线：NULL、残余谓词与并发合流

只要查询停留在无重复键的 `INNER JOIN`，上面任何一种内存结构写出来都干净利落。真正把内核代码逼成复杂状态机的，是 SQL 的多样连接语义与多线程并行屏障。

### 2.1 三值逻辑、残余谓词与连接状态演化

不同连接类型要求哈希表维护截然不同的附加状态：

| 连接类型 | Probe 阶段动作 | Build 侧附加状态与收尾要求 |
| --- | --- | --- |
| `INNER JOIN` | 沿重复链展开全部匹配对，满块挂起续扫 | 维护同键重复链；无尾扫阶段 |
| `LEFT OUTER` | 未命中行配对 `build_row = 0`（哨兵 `NULL` 行） | 无需全表尾扫；若有残余条件须整链皆假才补空 |
| `RIGHT / FULL OUTER` | 命中时幂等置位 `visited[build_idx] = 1` | 保留右表 `NULL` 键行；Probe 耗尽后转入 `POST_PROBE` 扫 `visited == 0` |
| `LEFT SEMI / ANTI` | 首条真命中即产出（Semi）或淘汰（Anti） | 若无残余谓词可去重存成 `Set`，大幅削减重复键内存 |
| `NULL-Aware ANTI` / `MARK` | 输出左表存活行或 `TRUE / FALSE / NULL` 三值标记列 | 显式追踪 `has_null_in_build_side` 与关联子查询分组计数 |

沿着一条探测批次穿过这些状态门控，可以看到每一处看似不起眼的顺序约束背后都藏着正确性红线：

![NULL 三值逻辑与 Outer / Semi / Anti 连接的状态机演进](/images/diagrams/aug-exec-hash-join-null-outer-semantics.svg)

第一道红线是 **`NULL` 键的不对称生命周期**。在普通 `=` 条件下，`NULL = NULL` 的求值结果是 `UNKNOWN` 而非 `TRUE`，所以 `INNER` 和 `LEFT OUTER` 在 Build 入口直接把右表空键丢弃毫无问题；但在 `RIGHT OUTER` 或 `FULL OUTER` 中，这些永远配不上对的右表 `NULL` 行恰恰是最终必须输出的未匹配记录——Doris 为此在 `first` 数组末尾专门多开第 `bucket_size` 号槽挂接空键链，ClickHouse 则用独立的 `NullmapList` 把它们攒到最后。而一旦 SQL 写成 `a <=> b`（`IS NOT DISTINCT FROM`），`NULL` 又变成了可以彼此相等的一等公民，StarRocks 在定长键打包时必须为每列多塞 1 个空值标志字节，防止 `NULL` 与整数 `0` 撞成同一个二进制键。至于 `WHERE x NOT IN (SELECT y FROM r)` 翻译出的 Null-Aware Left Anti Join，Doris 在 `ProcessHashTableBuild` 中专门埋了 `short_circuit_for_null` 快路径：只要确认无残余条件且右表扫到了 `NULL`，当场终止构建返回空集，连哈希表都不用建完。

第二道红线是**重复链与非等值残余谓词（Residual Predicate）叠加时的选择向量更新顺序**。设想 `LEFT ANTI JOIN ... ON l.k = r.k AND l.v < r.v`，左表第 3 行在右表撞上了两条 `k` 相等的记录，第一条算出来 `l.v < r.v` 为假，第二条算出来为真。此时左表第 3 行到底算命中还是未命中？显然只要重复链上有一条为真就算命中！DuckDB 在 2026 年修复过一个极具代表性的内核 Bug（`duckdb#23952`）：旧版 `ScanStructure::ApplyResidualPredicate()` 在遍历重复链时，先原地压缩了 `match_sel` 选择向量，再去推导 `no_match_sel`，结果在“等值键有重复 + 带残余谓词”的 `SEMI / ANTI` 场景下算错了未命中下标；修复方案正是把 `no_match_sel` 的差集计算严格挪到 `match_sel` 压缩之前。这类 Bug 单靠主键唯一的基准测试永远测不出来。

### 2.2 并行 Build 屏障与延迟物化探测

在单机多核 Pipeline 执行模型里，Hash Join 天生横亘着一道 **Build Barrier**——在右表尚未收齐封板前，左表一个字节都不能开始探测。为了让几十个物理线程在屏障两侧都不闲置，工业界演化出两类并行构建机制：

1. **线程本地无锁收集 + Finalize 并行建索引（DuckDB）**：在 `PhysicalHashJoin::Sink()` 阶段，每个工作线程只往私有的 `RadixPartitionedTupleData` 里做高吞吐纯追加，完全不碰全局指针表；等到 `Combine()` 汇总后，若内存够用，再由多线程通过 CAS 原子抢占空槽与链头（`InsertHashesLoop`）并行构建全局 `Pointer Table`。这把重数据搬运和易冲突的随机写索引拆在了两个阶段。
2. **双层哈希表按桶分片构建 + 零拷贝移交共享探测（ClickHouse `ConcurrentHashJoin`）**：如果每个线程各建一张独立哈希表，Probe 阶段要么每批左表数据都得先按哈希分片路由（Scatter），要么每次查表都得轮询 `N` 张小表。ClickHouse 的 [`ConcurrentHashJoin`](https://github.com/ClickHouse/ClickHouse/blob/c631f591cef3ecc0fcbba8fff30d16cc7a67ec23/src/Interpreters/ConcurrentHashJoin.cpp) 采用 256 桶双层哈希表（Two-Level Map）：Build 期按桶号把输入路由给最多 256 个 Slot，第 `i` 号线程只写 `i, i+S, i+2S...` 号子表，全程无锁；Build 收尾时（`onBuildPhaseFinish`），只需把各线程持有的子表指针 `std::move` 拼回同一张逻辑总表，并归并 `Right / Full` 连接的 `NullMap` 与命中位。随后所有 Probe 线程直接并发只读同一张完整哈希表，省掉了探测侧的分片洗牌开销。

顺带需要区分一个常被误当成落盘分区的概念——StarRocks 的 [`AdaptivePartitionHashJoinBuilder`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/be/src/exec/hash_join_components.cpp#L512)。它的目标纯粹是对抗 CPU 缓存失效：当右表大到溢出 L3 Cache（源码通过 `CpuInfo::get_l3_cache_size()` 动态探测，未上报时回退 L2 或 32 MiB）却又没大到需要写盘时，先缓冲估算每行触碰的 `8B(first+next) + Key + Payload` 字节数，只有确认“切成 16 张驻留缓存的子表所省下的 Cache Miss”大于“把左表按 CRC32 分发成 16 份的 Shuffle 成本”时才保持分区，一旦中途每 8 个 Chunk 重算发现不划算，立刻调用 `_convert_to_single_partition()` 退回单表。

到了 Probe 热路径上，StarRocks 与 Doris 均采用**行号对先行 + 延迟物化（Lazy Materialization）**：探测循环不搬运任何宽列字节，只往 `probe_indexs[]` 和 `build_indexs[]` 塞入命中的行号对；先拉取残余谓词必需的少数列完成过滤，最后才对真正存活下来的行号批量 Gather 宽字符串或 JSON 输出列——如果某批左表行号恰好连续且无重复链悬挂，Doris 甚至直接 `std::move` 移交左表列指针所有权，连一次内存拷贝都省了。

---

## 三、外存协议：为什么哈希表不能直接写盘，必须回到逻辑分区？

当 Build 侧体积终于顶穿内存配额时，为什么没有任何一家主流数据库选择“把建好的内存哈希表直接 `fwrite` 到临时文件，要用时再 `mmap` 或 `fread` 回来”？

回头看一眼第一节的内存结构就明白了：无论是 DuckDB 的 48 位元组裸指针、ClickHouse `Arena` 里的变长字符串地址，还是根据当前全局行数算出来的 2 的幂桶掩码，全都死死绑在当前进程虚拟地址空间与全局容量上。就算费尽周折把指针全部重定位成文件偏移，在磁盘文件上做随机哈希桶探测也会把顺序带宽打碎成每秒几万次 4 KiB 随机读，吞吐瞬间跌去两个数量级。

因此，所有外存 Hash Join（Grace / Hybrid Hash Join）都遵循同一条铁律：**落盘的永远是剥离了地址指针的逻辑行或列块，靠对称分区函数把全局大连接拆成若干互不相交的局部小连接。**

![外存 Hash Join 的分区与逐分区恢复协议](/images/hash-join-external-protocol.svg)

要让这套拆解在任意连接语义下滴水不漏，必须同时守住三条物理不变量：

1. **双侧分区映射严格同构**：设第 `level` 层的分区函数为 `pid = P(hash(key), level)`。Build 侧与 Probe 侧必须使用完全相同的键编码、哈希种子与分区位数（例如高比特 Radix 位或不同种子的重哈希），确保任何可能相等的左右记录必然落进同一个 `pid`，不同分区之间绝无交叉匹配。
2. **Hybrid 热分区留存与配对恢复**：内存预算允许时，保留部分 Build 分区常驻内存（Hybrid 模式），流经这些分区的左表行当场完成连接吐出，省去双侧写盘；其余冷分区的左右数据成对顺序追加到各自 Spill 文件。当上游全部流完后，执行器按剩余可用内存一次装入一个或多个配对 Build 分区，在内存中重建局部哈希表，流式扫完同号 Probe 文件并跑完该分区的 `POST_PROBE` 未命中扫描，随后整块销毁局部表。
3. **Runtime Filter 的完整性断路**：一旦 Build 切换进部分落盘或全量落盘模式，内存里只剩残缺子集。除非引擎额外维护了跨分区无损合并的全局布隆过滤器并在全部 Build 流完后才发布，否则必须果断停用或发布空 Runtime Filter（StarRocks 与 Doris 均在触发 Spill 时显式走这条保护分支），严禁用半张哈希表的过滤器去裁剪上游 Scan。

---

## 四、四套引擎（及 Velox）如何实现撤销、分区与递归恢复？

虽然底层数学协议都是 Hybrid / Recursive Grace Hash Join，但放进各自的内存管理与 Pipeline 调度框架里，StarRocks、Apache Doris、ClickHouse、DuckDB 和 Velox 走出了五种极具辨识度的状态机设计：

![四引擎并行构建与分区/基数 Spill 恢复状态管线](/images/diagrams/aug-exec-hash-join-four-engines-spill.svg)

> 想按执行阶段逐步跟踪内存驻留快路径与撤销递归重切分支的读者，可打开 [交互式视图：四引擎并行构建与分区 Spill 恢复管线](/diagrams/aug-exec-hash-join-four-engines-spill.html) 切换焦点路径查看。

### 4.1 StarRocks：按需拆表转列与可用内存多分区装箱

StarRocks（[`spillable_hash_join_build_operator.cpp`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/be/src/exec/pipeline/hashjoin/spillable_hash_join_build_operator.cpp) 与 [`spillable_hash_join_probe_operator.cpp`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/be/src/exec/pipeline/hashjoin/spillable_hash_join_probe_operator.cpp)）的哲学是**不为没发生的落盘提前交税**。查询启动时默认走纯粹的内存 `JoinHashTable`，零分区洗牌开销；只有收到算子内存管理器的撤销（Revoke）指令时，才一次性切入 `SPILL_ALL` 模式：根据当前哈希表占用估算分区数，调用 `_convert_hash_map_to_chunk()` 把已建好的内存表拆回列式逻辑块（跳过第 0 号哨兵行），追加分区哈希列后与后续输入一并按列压缩写盘。

![StarRocks 可 Spill Hash Join 的模式切换与分区恢复](/images/hash-join-starrocks-spill.svg)

进入恢复阶段后，`_acquire_next_partitions()` 并不是呆板地每次只读回一个分区，而是参考 `min(compute_available_memory_bytes, spill_hash_join_probe_op_max_bytes / DOP)` 做**贪心装箱**：如果当前剩余 800 MiB 内存，而落盘分区 1、7、9 分别只有 420 MiB、180 MiB 和 90 MiB，就把这三个分区同时拉起并行重建局部哈希表，一次流过 Probe 数据处理掉一批小分区，大幅削减磁盘调度轮次。在 `f977b53` 的演进中，团队还专门加固了异步落盘与查询取消（Cancel）交错时的生命周期防线：拆表前先用 `shared_ptr` 快照锁住底层 `build_chunk` 再同步 `reset()` 哈希表，防止后台 I/O 线程解引用已被 `close()` 销毁的对象；恢复读取前强制校验 `_probe_spiller->task_status()`，杜绝把半途取消的残缺文件当成合法分区读回。

### 4.2 Apache Doris：外层包装器与恢复中途的二次递归重切

Apache Doris（[`partitioned_hash_join_sink_operator.cpp`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/be/src/exec/operator/partitioned_hash_join_sink_operator.cpp) 与 [`partitioned_hash_join_probe_operator.cpp`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/be/src/exec/operator/partitioned_hash_join_probe_operator.cpp)）采用了干净的**内外双层解耦架构**：内层始终是原封不动的内存算子 `HashJoinBuildSinkOperatorX` / `HashJoinProbeOperatorX`，外层套上 `PartitionedHashJoin*OperatorX` 专职管理 `(build_file, probe_file, level)` 工作队列。这样做最大的好处是：10 余种 `JoinOp`（含 `RIGHT_ANTI`、`NULL_AWARE_LEFT_ANTI`、带残余谓词的外连接）只写一套内层模板代码，恢复某个分区时只需实例化一套局部内部算子喂数据即可，彻底消灭“内存跑得对、一落盘就出语义偏差”的双轨维护噩梦。

Doris 状态机里最精彩的一笔是**恢复中途遭遇二次撤销的断点续切**。`_pull_from_spill_queue()` 在读回某个 `build_file` 时每攒够 `spill_buffer_size` 就主动 `yield` 让出 Pipeline 线程，并且在真正建表前把 `first[] + next[] + visited[] + 序列化键` 的完整峰值算进内存预留。万一文件读到一半内存又被别的查询抢占，或者单分区体积超出预期，`repartition_current_partition()` 会以 `level + 1` 的新哈希种子同步拆分两侧：对 Build 侧，它先把已经读进内存 `_recovered_build_block` 的那半截行散列到子分区文件，再让 `SpillRepartitioner` **紧接着 `_current_build_reader` 的当前文件游标往后读完剩余半截**——既不漏行，也绝不从文件头重读引发重复行！整个递归深度受 `spill_repartition_max_depth` 硬约束，防止死循环耗尽磁盘句柄。

### 4.3 ClickHouse：显式 `GraceHashJoin` 与 2 的幂动态裂变

与 MPP 引擎透明撤销不同，ClickHouse 在 [`PlannerJoins.cpp`](https://github.com/ClickHouse/ClickHouse/blob/c631f591cef3ecc0fcbba8fff30d16cc7a67ec23/src/Planner/PlannerJoins.cpp#L1259) 中把内存假设直接交由 `IJoin` 策略类显式承载：`HashJoin`、`ConcurrentHashJoin` 与 [`GraceHashJoin`](https://github.com/ClickHouse/ClickHouse/blob/c631f591cef3ecc0fcbba8fff30d16cc7a67ec23/src/Interpreters/GraceHashJoin.cpp)。

在 `GraceHashJoin` 中，桶数量始终保持 2 的幂。初始阶段可以只开少量 `FileBucket`（避免小查询一上来在磁盘上铺满几百个空文件），第 0 号当前桶直接写入内存 `HashJoin` 实例，其余桶写各自压缩临时流。一旦当前内存桶突破预算，`rehashBuckets()` 将总桶数翻倍（受 `max_num_buckets` 封顶），调用 `releaseJoinedBlocks()` 从内存表里取回原始逻辑右表 `Block`，多取 1 位哈希比特重新分发——属于新当前桶的留下来重建内存表，分出去的落入新桶文件。由于多线程可能持有扩容前的桶数快照，探测与延迟回放入口（`getDelayedBlocks`）每次都会校验记录所属桶号是否已随翻倍漂移，必要时二次路由，确保左右两侧最终严丝合缝地按桶号汇合。

### 4.4 DuckDB 与 Velox：预分区元组、工作集预留与仲裁协同

嵌入式引擎 DuckDB（[`physical_hash_join.cpp`](https://github.com/duckdb/duckdb/blob/1c4ecd8138ae0c63c73957a411e65484301bb300/src/execution/operator/join/physical_hash_join.cpp)）从 `Sink` 第一行起就把元组放进 `RadixPartitionedTupleData`。到了 `PrepareFinalize()` 关卡，它向全局 `TemporaryMemoryManager` 申报预留额度时，算的是一笔极其严谨的全链路账：

$$
M_{\text{round}} = \sum_{p \in S} \left( \text{TupleBytes}(p) + \text{PointerTableBytes}(p) \right) + N_{\text{threads}} \times B_{\text{probe\_radix\_buf}}
$$

只要拿到的 `reservation` 小于全量内存表需求，算子自动转入 External 模式：先把开放寻址装载因子收紧（有时仅靠缩小空槽就能免于落盘）；若最大分区加上探测缓冲仍超预算，`SetRepartitionRadixBits()` 会增加基数位，故意把单分区目标体积压到预算的 `1/4` 左右留足安全垫；随后每轮按近似大小排序挑出一组能塞进预算的 `current_partitions` 建表，未选中分区的左表行写进 `ProbeSpill`，等上游流尽后切入 `Source` 角色按 `BUILD → PROBE → SCAN_HT` 循环扫完剩余轮次。

Meta Velox 的原生执行层（[`HashBuild.h`](https://github.com/facebookincubator/velox/blob/main/velox/exec/HashBuild.h) / [`HashProbe.h`](https://github.com/facebookincubator/velox/blob/main/velox/exec/HashProbe.h)）则通过 `HashJoinBridge` 在多个并发 Driver 线程间同步跨阶段分区契约，并与统一内存仲裁器（Memory Arbitrator）深度联动：当同节点其他查询突发申请内存时，仲裁器随时可挂起当前 Join 驱动组，挑出若干哈希分区顺序刷盘腾出配额，恢复时同样支持递归子分区拆分。

把这几套工业实现的底层取舍放在同一张坐标表里，差异一目了然：

| 核心维度 | StarRocks (`f977b53`) | Apache Doris (`7ac0eb2`) | ClickHouse (`c631f59`) | DuckDB (`1c4ecd8`) |
| --- | --- | --- | --- | --- |
| **Build 数据形态** | 列式 `build_chunk` + 哨兵行 0 | 列式 `Block` + 哨兵行 0 | `ScatteredColumnsList` + `Arena` | 行式 `RadixPartitionedTupleData` |
| **目录与碰撞机制** | 静态工厂注册 Direct / Linear / Bucket 链 | 稠密 `first[] + next[]` 行号链 | Typed Map → `RowRef` / `RowRefList` | 开放寻址 `Pointer Table` + 16-bit Salt |
| **Spill 切入时机** | 内存快路径优先，撤销时拆表转列落盘 | 内存快路径优先，撤销时切换外层包装器 | 显式启用 `grace_hash`，超限动态翻倍桶 | 构建期即保 Radix 分区，Finalize 按预留切换 |
| **单轮恢复粒度** | 按可用内存贪心合批加载多个分区 | 队列逐对恢复，读盘超阈值主动 `yield` | 逐个非空 `FileBucket` 恢复并探测 | 每轮装箱一组不超预留额度的分区集 |
| **超大单区应对** | 分区预算控制与多级写出 | 断点续读 + `level + 1` 显式递归重分区 | `bucket_count *= 2` 取回逻辑块重散列 | 追加 Radix 位压至 `1/4` 预算；识别极度倾斜 |

---

## 五、算法特化、热点倾斜死局与线上排障闭环

看懂了常规哈希与分区恢复，再来看两个决定长尾查询生死的极值边界：**分布极度规整时如何绕开哈希？分布极度倾斜时为什么递归分区会彻底失灵？**

### 5.1 值域直接映射（Direct / Perfect Hash）：消灭哈希本身

当连接键是单个整数（如日期键 `20260101 ~ 20261231` 或连续编码的维表主键），且统计信息证明 `max_key - min_key` 跨度可控时，算哈希和跑冲突链纯属多此一举。直接用 `slot = key - min_key` 做数组下标寻址，就能把探测指令压到“一次范围检查 + 一次偏移读取”。

DuckDB 的 [`PerfectHashJoinExecutor`](https://github.com/duckdb/duckdb/blob/1c4ecd8138ae0c63c73957a411e65484301bb300/src/execution/operator/join/perfect_hash_join_executor.cpp) 在单等值条件、整数值域跨度低于阈值且 Build 侧键唯一时自动接管探测；StarRocks 的 `RANGE_DIRECT_MAPPING` 与 `DENSE_RANGE_DIRECT_MAPPING` 更进一步，把重复键场景也纳了进来——即使值域略稀疏，先用每槽约 `2-bit` 位图标记哪些偏移存在真实键，再紧凑映射到行号链数组，兼顾了 CPU 缓存命中率与多对一连接。不过这类特化极其依赖精确的 `Min/Max` 边界：一旦遇到异常脏数据（如一个 `99999999` 哨兵值把跨度撑大一亿倍），必须立刻回退常规哈希表，否则直接按值域开数组会当场撑爆内存。

### 5.2 单个超级热点键：为什么换一百次哈希种子也没用？

递归 Grace Hash Join 有一个无法靠哈希数学绕过去的阿喀琉斯之踵：**哈希函数是确定性映射，只要两行数据的连接键值完全相同（例如十亿条日志里占了 60% 的默认账户 `user_id = 0` 或空串 `""`），无论你换什么哈希种子、取哪几位 Radix 比特，`hash(hot_key)` 算出来永远是同一个常数！**

这意味着，那 60% 的热点行在 `level = 0` 时挤爆 3 号分区，重切成 `level = 1` 时原封不动挤爆 `3.7` 号子分区，一路切到 `max_depth` 依然一字节都拆不开，白白把同一坨数据在磁盘上反复读写了好几遍后报错退出。DuckDB 在 `Finalize` 源码里专门写下了 `very_very_skewed` 判定：一旦监测到单个分区吞掉了全表 `80%` 以上的体积，当场放弃无效的增加 Radix 位尝试，甚至主动把并行构建收敛回单线程，免得几十个核围着同一根冲突链做 CAS 自旋把总线锁死。

在工程上要真正化解单键倾斜，必须跳出“单算子死磕哈希位”的局限：

- **语义削减**：若是 `LEFT SEMI / ANTI` 且无残余非等值依赖，在 Build 入口开启按键去重（如 StarRocks `*_SET` 变体、Spark `ignoreDuplicatedKey`），把一千万条相同 `user_id = 0` 当场压成 1 条，倾斜瞬间消失；
- **交换 Build/Probe 方向**：若热点集中在右表而左表相对均匀（且连接语义允许翻转，如 `INNER` 或 `LEFT ↔ RIGHT` 对偶改写），把无热点的一侧拿来 Build，让热点侧流式穿过探测；
- **分区级算法降级或加盐裂变（Skew Salting）**：像 CockroachDB 外部哈希连接那样，仅把切不小的那一个倾斜分区单独转交外排 + Sort-Merge Join（详见[下一篇《Hash 还是 Sort》]({{< relref "2026-09-10-dive-hash-sort-strategy.md" >}})）；或者在分布式规划层识别热点值，给热点侧随机打上 `0..K-1` 盐值拆散，同时把另一侧对应的同键记录精确复制 `K` 份配对连接（注意外连接复制侧须谨慎处理未命中去重）。

### 5.3 线上慢查询与 OOM 排障定位表

当线上监控报出某个 Hash Join 节点卡死、耗时飙升或触发内存上限时，只盯“总落盘字节数（Spill Bytes）”往往抓不到病根。最高效的排查顺序是沿指标链逐层剥离：

| 线上观测症状 | 底层真实物理根因 | 针对性核查与治理动作 |
| --- | --- | --- |
| **Build 阶段未落盘即迅速 OOM** | 大小表估反（拿百亿行事实表做了 Build）、未开启可撤销 Spill，或宽列/低装载因子放大 | 核对 `EXPLAIN` 估算行数与真实行数；收集统计信息翻转 Build 方向，或切入可 Spill 算子管线 |
| **Spill 总量不大，但递归层级打满报错** | 存在超高频单一热点键（如默认值 `-1` / 空串），多轮重哈希完全无法拆小最大分区 | 对比 `MaxPartitionBytes` 与 `AvgPartitionBytes`；拆分过滤异常默认键、预聚合去重或启用倾斜优化 |
| **Probe CPU 占满，但最终输出行数极少** | 大量根本连不上的左表行穿过了上游，或因 Build 发生 Spill 导致 Runtime Filter 被停用 | 检查 Runtime Filter 是否生效下推到 Scan；尽量给 Build 留足免 Spill 内存以保住完整 Bloom Filter |
| **哈希表很小、内存平稳，但算子长期挂起** | 两侧同键多对多笛卡尔放大，单输入块反复触发 `has_remain` 挂起吐出百倍结果块 | 对比 Probe 输入行数与 Join 输出行数；排查业务连接键是否漏写租户/分区条件导致非预期发散 |
| **磁盘带宽与 CPU 利用率双低，恢复极慢** | 初始分区切得过碎产生上千个小文件，或每次仅串行恢复单个微小分区导致 I/O 空转 | 调小初始分区扇出，确认引擎开启按可用内存多分区合批恢复（Batch Restore）与压缩块读写 |

---

## 附录：Spark Hash Join 特有路径 {#spark-hash-join}

看完了四款原生 MPP / 列存引擎的可恢复分区状态机，很多习惯了大数据栈的读者会产生一个疑问：**为什么在 Apache Spark SQL 里，只要两张表大到不能广播，默认执行计划几乎清一色落到 `SortMergeJoin`（SMJ），而不是 `ShuffledHashJoin`（SHJ）？**

翻开 Spark 源码（基线 `786bb3d`），答案不在于“JVM 算哈希慢”，而在于**内置 `HashedRelation` 压根没有实现分区级落盘与恢复协议**。

### A.1 `JoinSelection` 准入漏斗与二进制相等契约

在 [`SparkStrategies.JoinSelection`](https://github.com/apache/spark/blob/786bb3d9751fc6c4993997c088345ceba1b7a8d5/sql/core/src/main/scala/org/apache/spark/sql/execution/SparkStrategies.scala#L181) 与 [`JoinSelectionHelper`](https://github.com/apache/spark/blob/786bb3d9751fc6c4993997c088345ceba1b7a8d5/sql/catalyst/src/main/scala/org/apache/spark/sql/catalyst/optimizer/joins.scala#L290) 中，无 Hint 的等值连接按固定优先级依次尝试：

```scala
createBroadcastHashJoin(false)
  .orElse(createShuffleHashJoin(false))
  .orElse(createSortMergeJoin())
  .orElse(createCartesianProduct())
  .getOrElse(createBroadcastNestedLoopJoin())
```

要想挤进前两档 Hash Join，首先必须过两道硬性语义关：一是连接键全部满足 `UnsafeRowUtils.isBinaryStable(e.dataType)`——如果字符串列带了大小写不敏感或口音折叠排序规则（非二进制 Collation），两个语义相等的字符串在 `UnsafeRow` 里字节序列不同、算出的哈希值也不同，Spark 会直接拒绝 Hash Join；二是 Build 侧方向必须合法——例如 `BroadcastHashJoinExec`（BHJ）绝不支持 `FULL OUTER JOIN`，因为每个 Executor 都持有一份全量广播表的副本，却只看到本地那一份 Stream 分片，谁都没资格判断某条广播行在全局是不是“真未命中”。

跨过语义门槛后，BHJ 只需估算体积不超过 `spark.sql.autoBroadcastJoinThreshold`（默认 `10MB`），由 Driver 收集全表构建 [`HashedRelation`](https://github.com/apache/spark/blob/786bb3d9751fc6c4993997c088345ceba1b7a8d5/sql/core/src/main/scala/org/apache/spark/sql/execution/joins/HashedRelation.scala)（单非空 `Long` 键走支持 `key - minKey` 稠密数组优化的 `LongHashedRelation`，通用键走 `UnsafeHashedRelation`）并通过 Torrent 协议分发给各 Executor（[`BroadcastExchangeExec`](https://github.com/apache/spark/blob/786bb3d9751fc6c4993997c088345ceba1b7a8d5/sql/core/src/main/scala/org/apache/spark/sql/execution/exchange/BroadcastExchangeExec.scala#L121)）。而 [`ShuffledHashJoinExec`](https://github.com/apache/spark/blob/786bb3d9751fc6c4993997c088345ceba1b7a8d5/sql/core/src/main/scala/org/apache/spark/sql/execution/joins/ShuffledHashJoinExec.scala) 在静态规划期默认直接被 `spark.sql.join.preferSortMergeJoin = true` 拦在门外；即便手动关掉这个开关，还必须同时满足 `BuildSize < autoBroadcastJoinThreshold * numShufflePartitions` 且 `BuildSize * 3 <= StreamSize`（`shuffledHashJoinFactor = 3`）。

### A.2 为什么 `BytesToBytesMap.spill()` 救不了 Spark Hash Join？

Spark 之所以对 SHJ 如此保守，根源就在 [`BytesToBytesMap.java`](https://github.com/apache/spark/blob/786bb3d9751fc6c4993997c088345ceba1b7a8d5/core/src/main/java/org/apache/spark/unsafe/map/BytesToBytesMap.java) 的内存回收实现里。很多开发者看到这个类继承了 `MemoryConsumer` 并带有 `spill()` 方法，误以为它能自动落盘：

```java
public long spill(long size, MemoryConsumer trigger) throws IOException {
  if (trigger != this && destructiveIterator != null) {
    return destructiveIterator.spill(size);
  }
  return 0L;
}
```

看清那个前置条件 `destructiveIterator != null`！这个破坏性迭代器只在 Hash Aggregate 退化为外部排序聚合（交给 `UnsafeKVExternalSorter` 接管）时才会创建；而在 `ShuffledHashJoin` 构建 `UnsafeHashedRelation` 的全过程中，`destructiveIterator` 永远是 `null`（`LongToUnsafeRowMap.spill()` 更是直白地硬编码返回 `0L`）。

这就造成了单 Task 内极其凶险的不对称内存博弈：当 SHJ 在某个倾斜分区上不断 `append` 撑满执行内存时，它可以逼迫同 Task 上游的 Sort 或 Shuffle Reader 把缓冲刷盘给自己腾地方；可一旦同 Task 里没有别的可驱逐算子，或者单个分区的 Build 数据（外加开放寻址空槽、70% 扩容峰值以及 `Full Outer` 的 `BitSet` / `OpenHashSet` 命中集）硬生生超过了单 Task 内存上限，它没有任何“按哈希位再切子分区写盘”的退路，只能当场抛出 `cannotAcquireMemoryToBuildUnsafeHashedRelationError` 让作业挂掉。

### A.3 AQE 动态纠偏与原生引擎（Velox / Photon）的补位

正因为静态统计用“总字节数除以分区数”根本算不出单分区倾斜，Spark 把翻盘希望寄托在了 **AQE（Adaptive Query Execution）** 的 Stage 边界重规划（[`AdaptiveSparkPlanExec.reOptimize()`](https://github.com/apache/spark/blob/786bb3d9751fc6c4993997c088345ceba1b7a8d5/sql/core/src/main/scala/org/apache/spark/sql/execution/adaptive/AdaptiveSparkPlanExec.scala#L793)）上：

- **拿到真实 Shuffle 体积后升级算子**：当上游 Map Stage 物化完毕，`ShuffleQueryStageExec.mapStats.bytesByPartitionId` 精确暴露了每一个分区的真实压缩字节数。如果某侧过滤后大幅缩水，AQE 可将其升级为 BHJ，并借 [`OptimizeShuffleWithLocalRead`](https://github.com/apache/spark/blob/786bb3d9751fc6c4993997c088345ceba1b7a8d5/sql/core/src/main/scala/org/apache/spark/sql/execution/adaptive/OptimizeShuffleWithLocalRead.scala) 免掉另一侧的跨机重洗牌（注意：已经完成广播的 `BroadcastQueryStageExec` 受 [`LogicalQueryStageStrategy`](https://github.com/apache/spark/blob/786bb3d9751fc6c4993997c088345ceba1b7a8d5/sql/core/src/main/scala/org/apache/spark/sql/execution/adaptive/LogicalQueryStageStrategy.scala) 沉没成本保护，不会反向退回 Shuffle）；同时 [`DynamicJoinSelection`](https://github.com/apache/spark/blob/786bb3d9751fc6c4993997c088345ceba1b7a8d5/sql/core/src/main/scala/org/apache/spark/sql/execution/adaptive/DynamicJoinSelection.scala) 在确认**每一个分区**都不超过 `spark.sql.adaptive.maxShuffledHashJoinLocalMapThreshold` 时可注入 `PREFER_SHUFFLE_HASH` 把 SMJ 改写成 SHJ——不过为了安全，该阈值在开源默认配置里依然设为 `0`（关闭）。
- **倾斜拆分的缓解与局限**：[`OptimizeSkewedJoin`](https://github.com/apache/spark/blob/786bb3d9751fc6c4993997c088345ceba1b7a8d5/sql/core/src/main/scala/org/apache/spark/sql/execution/adaptive/OptimizeSkewedJoin.scala) 能把超大分区按 Map 端块范围切成多份并复制配对侧，摊薄单 Task 输入，但它依然无法赋予 `HashedRelation` 落盘恢复能力。
- **Native 插件彻底补齐状态机**：这也解释了为什么 Apache Gluten + Velox 等原生向量化插件接入 Spark 后，敢于大胆把大表 `SortMergeJoin` 批量改写成 `ShuffledHashJoin`——真正给底气的不只是 C++ SIMD 指令，而是用第四节剖析的 **Velox 可递归分区落盘 `HashBuild / HashProbe` 状态机**替换掉了不可落盘的 JVM `HashedRelation`，终于把单分区估算失误的代价从“Task 直接 OOM 崩溃”拉回了“受控的局部磁盘读写退化”。

---

## 参考资料与源码基线

> **源码基线说明**：主文四套引擎源码基于以下提交核对——StarRocks `0fd27fd`（并在 `f977b53` 上复核了哈希表静态工厂注册表、L3 Cache 动态探测及异步 Spill 取消保护演进）、Apache Doris `5202d06`（复核至 `7ac0eb2`，分区 Spill 状态机一致）、ClickHouse `c631f59`、DuckDB `1c4ecd8`（复核至 `2bb5316`，含 `duckdb#23952` 残余谓词与重复链选择向量修复）；Spark 附录基于 Apache Spark `786bb3d`。

### 核心源码入口索引

- **StarRocks (`0fd27fd` / `f977b53`)**：[键构造与哈希表选型 `join_hash_table.cpp`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/be/src/exec/join/join_hash_table.cpp) · [四阶段控制器 `hash_joiner.h`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/be/src/exec/hash_joiner.h) · [Cache 自适应分区 `hash_join_components.cpp`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/be/src/exec/hash_join_components.cpp#L512) · [可撤销落盘与多分区恢复 `spillable_hash_join_build_operator.cpp`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/be/src/exec/pipeline/hashjoin/spillable_hash_join_build_operator.cpp) / [`spillable_hash_join_probe_operator.cpp`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/be/src/exec/pipeline/hashjoin/spillable_hash_join_probe_operator.cpp)
- **Apache Doris (`5202d06`)**：[`first/next/visited` 数组结构 `join_hash_table.h`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/be/src/exec/common/hash_table/join_hash_table.h) · [构建与空值短路 `hashjoin_build_sink.cpp`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/be/src/exec/operator/hashjoin_build_sink.cpp#L586) · [行号向量化探测 `process_hash_table_probe.h`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/be/src/exec/operator/join/process_hash_table_probe.h) · [递归分区恢复状态机 `partitioned_hash_join_probe_operator.cpp`](https://github.com/apache/doris/blob/5202d06dd8feb3390ff32839227eeee89c345b57/be/src/exec/operator/partitioned_hash_join_probe_operator.cpp)
- **ClickHouse (`c631f59`)**：[算法策略选择 `PlannerJoins.cpp`](https://github.com/ClickHouse/ClickHouse/blob/c631f591cef3ecc0fcbba8fff30d16cc7a67ec23/src/Planner/PlannerJoins.cpp#L1259) · [多形态键与 `RightTableData` `HashJoin.cpp`](https://github.com/ClickHouse/ClickHouse/blob/c631f591cef3ecc0fcbba8fff30d16cc7a67ec23/src/Interpreters/HashJoin/HashJoin.cpp) · [Two-Level Map 并行构建 `ConcurrentHashJoin.cpp`](https://github.com/ClickHouse/ClickHouse/blob/c631f591cef3ecc0fcbba8fff30d16cc7a67ec23/src/Interpreters/ConcurrentHashJoin.cpp) · [动态倍增外存桶 `GraceHashJoin.cpp`](https://github.com/ClickHouse/ClickHouse/blob/c631f591cef3ecc0fcbba8fff30d16cc7a67ec23/src/Interpreters/GraceHashJoin.cpp)
- **DuckDB (`1c4ecd8`)**：[开放寻址 Salt 与元组复用槽 `join_hashtable.cpp`](https://github.com/duckdb/duckdb/blob/1c4ecd8138ae0c63c73957a411e65484301bb300/src/execution/join_hashtable.cpp) · [Radix 预留与多轮 External 状态机 `physical_hash_join.cpp`](https://github.com/duckdb/duckdb/blob/1c4ecd8138ae0c63c73957a411e65484301bb300/src/execution/operator/join/physical_hash_join.cpp) · [稠密值域完美哈希 `perfect_hash_join_executor.cpp`](https://github.com/duckdb/duckdb/blob/1c4ecd8138ae0c63c73957a411e65484301bb300/src/execution/operator/join/perfect_hash_join_executor.cpp)
- **Velox & Apache Spark (`786bb3d`)**：[Velox `HashBuild.h`](https://github.com/facebookincubator/velox/blob/main/velox/exec/HashBuild.h) / [`HashProbe.h`](https://github.com/facebookincubator/velox/blob/main/velox/exec/HashProbe.h) · [Spark `JoinSelection`](https://github.com/apache/spark/blob/786bb3d9751fc6c4993997c088345ceba1b7a8d5/sql/core/src/main/scala/org/apache/spark/sql/execution/SparkStrategies.scala#L181) · [`HashedRelation.scala`](https://github.com/apache/spark/blob/786bb3d9751fc6c4993997c088345ceba1b7a8d5/sql/core/src/main/scala/org/apache/spark/sql/execution/joins/HashedRelation.scala) · [`BytesToBytesMap.java`](https://github.com/apache/spark/blob/786bb3d9751fc6c4993997c088345ceba1b7a8d5/core/src/main/java/org/apache/spark/unsafe/map/BytesToBytesMap.java) · [`DynamicJoinSelection.scala`](https://github.com/apache/spark/blob/786bb3d9751fc6c4993997c088345ceba1b7a8d5/sql/core/src/main/scala/org/apache/spark/sql/execution/adaptive/DynamicJoinSelection.scala)
