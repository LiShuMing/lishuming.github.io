---
title: "【源码】OLAP 引擎里的 Hash Table：规模拐点、冲突演化、Rehash 消除与算子分化"
date: 2026-11-19T00:00:00+08:00
lastmod: 2026-11-19T00:00:00+08:00
slug: "olap-hash-table-design-and-tuning"
categories:
  - 数据库
  - 大数据
tags:
  - Hash Table
  - OLAP
  - ClickHouse
  - StarRocks
  - Velox
  - DuckDB
  - Apache Doris
  - DataFusion
  - 查询引擎
description: "结合 StarRocks、Velox、DuckDB、Apache Doris（ClickHouse 血统）、DataFusion 与 LingoDB 内核源码，从硬件存储层级拐点、冲突消解演化、Rehash 消除以及 Join/Aggregation/Window 算子分化四个维度，系统拆解 OLAP 查询引擎中哈希表的设计与调优方法。"
toc: true
math: true
---

教科书告诉我们，哈希表的期望查找时间复杂度是 \(O(1)\)。可如果你在现代 x86-64 服务器上跑一条 `GROUP BY` 或 `Hash Join`，把唯一键数量（NDV）从 **1,000** 拉到 **10 万**，再拉到 **1,000 万**，单次查表的平均延迟绝不会是一条水平直线——它会像踩了三段陡坡一样逐级暴跌：从 **1\~2 ns** 跳到 **15 ns**，再猛冲到 **80\~150 ns**，最后在某个毫不起眼的批次插入瞬间，整条流水线突然停顿几百毫秒，甚至直接触发内存超限（OOM）。

同样是 \(O(1)\)，为什么吞吐会差出几十倍？因为教科书的随机访问机（RAM）模型假设内存是一块延迟均匀的平地，而真实 CPU 的访存世界是一座极其陡峭的延迟金字塔：L1d 缓存（32\~48 KiB，\~1 ns）、L2 缓存（256 KiB\~1 MiB，\~4 ns）、L3 共享缓存（数十 MiB，\~15 ns）、主存 DRAM（\~70\~100 ns），旁边还横亘着同样稀缺的地址翻译缓冲区 TLB（典型 dTLB 仅 64 项、L2 STLB 约 1,536\~2,048 项，在 4 KiB 标准页下只能覆盖 **6\~8 MiB** 的随机工作集）。当哈希表跨过每一级缓存与 TLB 的物理天花板，单次查表的瓶颈就从“算一次哈希、比两个整数”彻底异化成“让 8 发射乱序执行 CPU 干等主存返回和硬件页表遍历（Page Walk）”。

更棘手的是，OLAP 查询引擎里并不存在一张通吃所有算子的“万能哈希表”。`Aggregation` 一边流式写入一边就地累加唯一键状态，`Join` 可以先攒齐整张构建表再进入高频只读探测且必须承接 1:N 重复键，`Window` 则带着分区归组与帧内有序的双重约束。如果直接把 C++ 标准库的 `std::unordered_map` 塞进列存引擎，每个节点一次 `malloc`、每条探测链几次指针跳转，不仅吞吐会比定制实现慢一个数量级，还会把 CPU 缓存、TLB、内存峰值和多核扩展性一并拖垮。

下面结合 **StarRocks、Meta Velox、DuckDB、Apache Doris（保留并演化了 ClickHouse 哈希表血统）、Apache DataFusion、LingoDB** 以及 **ClickHouse** 上游的内核设计，沿着“规模瓶颈拐点 → 冲突消解演化 → Rehash 消除与治理 → Join / Aggregation / Window 三类算子分化”的主线，把 OLAP 哈希表为什么必须这样设计讲透。

---

## 一、规模拐点：Hash Table 在什么数据量级会撞上瓶颈？

先算一笔硬件账。假设一台引擎里紧凑型哈希表的单槽开销在 **8\~16 字节**（例如 Velox 均摊每槽 8 字节，DuckDB `ht_entry_t` 每槽 8 字节，StarRocks `phmap` 存 8 字节键 + 8 字节聚合状态指针外加 1 字节控制字节约 17 字节），在 50%\~87.5% 的负载因子（Load Factor）下，不同唯一键规模对应的目录工作集大小会精准撞上四道物理墙：

![OLAP Hash Table 规模拐点与硬件层级跃迁](/images/diagrams/exec-olap-ht-scaling-cliffs.svg)

### 1.1 微型甜点区（< 4K \~ 64K 键，工作集 ≤ 512 KiB）：访存免费，算哈希反而最贵

当表完全装进 L1/L2 缓存时，随机读一个数组元素只需要 1\~4 ns，此时真正吃掉 CPU 周期的反而是哈希计算、位掩码取模、空槽判断和分支预测失败。在这个区间里，最极致的优化是**彻底消灭哈希本身**——只要键是小整数或值域紧凑的区间，就直接退化成数组下标寻址（Direct / Perfect Hash）：

- **StarRocks**：聚合算子对 `BOOLEAN`、`TINYINT`、`SMALLINT` 以及 1 字节位压缩键直接启用 `SmallFixedSizeHashMap`（[`fixed_hash_map.h:34-144`](https://github.com/StarRocks/starrocks/blob/f977b53c/be/src/base/container/fixed_hash_map.h#L34-L144)），为 `int16_t` 分配 65,536 槽位（约 512 KiB）外加 1 个尾部哨兵位，既无哈希计算也无冲突探测，扫描输出时靠哨兵位实现无分支跳过空槽。
- **DuckDB**：`PerfectAggregateHashTable` 在规划期检查所有分组整数列的值域位宽总和（[`plan_aggregate.cpp:131-247`](https://github.com/duckdb/duckdb/blob/2bb5316a/src/execution/physical_plan/plan_aggregate.cpp#L131-L247)），只要不超过默认 `perfect_ht_threshold = 12` 比特（即 **4,096 槽位**），就直接把多列 `(val - min + 1)` 位拼接成数组下标，甚至连分组键本身都不存，扫描吐出时靠位掩码加 `min_val` 算术还原；Join 侧的 `PerfectHashJoinExecutor` 则把单整数无重复构建表的直接映射上限放宽到 `MAX_BUILD_SIZE = 1,048,576`（[`perfect_hash_join_executor.cpp:73-195`](https://github.com/duckdb/duckdb/blob/2bb5316a/src/execution/operator/join/perfect_hash_join_executor.cpp#L73-L195)）。
- **Velox**：把直接数组寻址做成了运行时自适应的第一档 `HashMode::kArray`，上限 `kArrayHashMaxSize = 2 << 20`（**200 万槽位 / 16 MiB**，[`HashTable.h:146`](https://github.com/facebookincubator/velox/blob/303d7149/velox/exec/HashTable.h#L146)），探测阶段直接走 `simd::gather` 批量取指针。
- **DataFusion 与 Doris**：DataFusion 的 `ArrayMap`（[`array_map.rs:103-260`](https://github.com/apache/datafusion/blob/a4b8198c/datafusion/physical-plan/src/joins/array_map.rs#L103-L260)）在单整数值域跨度 `< 1,024` 或填充密度 `> 0.15` 时走直接数组；Doris 的 `MethodOneNumberDirect`（[`join_utils.h:190-191`](https://github.com/apache/doris/blob/7ac0eb2f/be/src/exec/common/join_utils.h#L190-L191)）把单数值键直接映射上限设在 \(2^{23} - 1\)。

不过直接映射有一条清晰的失效边界：一旦值域跨度极大而实际键很稀疏（例如键只有 0 和 1,500,000 两个值），数组里 99.9% 都是空槽，反而白白污染 L2/L3 缓存并在遍历输出时浪费扫描时间。正因如此，Velox 在局部聚合中专门加了稀疏降级保护（[`GroupingSet.cpp:981-985`](https://github.com/facebookincubator/velox/blob/303d7149/velox/exec/GroupingSet.cpp#L981-L985)）：一旦 `kArray` 占用超过预算的 \(1/16\) 且实际填充率低于 \(1/32\)，就永久禁用范围数组模式；StarRocks 则在 `RangeDirectMappingJoinHashMap` 之外额外造了 `DenseRangeDirectMappingJoinHashMap`（[`join_hash_map_method.hpp:784-955`](https://github.com/StarRocks/starrocks/blob/f977b53c/be/src/exec/join/join_hash_map_method.hpp#L784-L955)），用每 32 个键共用一个 64 位 `{start_index, bitset}` 结构配合 `popcnt` 指令把稀疏空洞压扁。

### 1.2 中型区间（数万 \~ 百万键，工作集数 MiB \~ L3 容量）：L2 / TLB 失手与指纹门控

一旦表大小突破 L2（约 256 KiB\~1 MiB）并开始触及 4 KiB 页下的 STLB 覆盖上限（约 6\~8 MiB），每次随机查表就会频繁掉到 L3 甚至偶发页表遍历。这个区间有两个极具代表性的工程门控阈值，恰好印证了硬件拐点的真实位置：

1. **DuckDB 的 `USE_SALT_THRESHOLD = 8192`**（[`join_hashtable.hpp:93-95`](https://github.com/duckdb/duckdb/blob/2bb5316a/src/include/duckdb/execution/join_hashtable.hpp#L93-L95)）：源码注释写得非常直白——当桶容量不超过 8,192 时（\(8192 \times 8\text{ B} = 64\text{ KiB}\)），整张目录和热行大概率驻留在 L1/L2 缓存里，直接解引用比对键的代价极低，此时额外比对高 16 位 Salt 反而多跑指令；只有跨过 8,192 槽位、表开始溢出高速缓存时，先比 16 位 Salt 再解引用行指针才能赚回收益。
2. **StarRocks 的 L2 预取门控**（[`agg_hash_set.h:41-54`](https://github.com/StarRocks/starrocks/blob/f977b53c/be/src/exec/aggregate/agg_hash_set.h#L41-L54)）：`agg_should_prefetch_table()` 动态计算 `bucket_count * (sizeof(value_type) + 1) >= L2_cache_size * agg_prefetch_l2_ratio`（默认系数 `1.0`）。表还装在 L2 里时，软件 `__builtin_prefetch` 只会白白占用发射端口和指令带宽；只有跨过 L2 边界，批预取才开启。

在这个区间里，**负载因子和槽位紧密度**决定了一张表能多在 L3 里撑多久。ClickHouse 传统线性探测表为了压制探测链长，不得不把负载因子锁在 **50%**；而 Apache Doris 后来在聚合算子里把数值与序列化键全面迁往 SwissTable（`phmap::flat_hash_map`，负载因子 **87.5%**），最直接的红利就是同样 100 万个分组键，目录内存直接缩水约 40%，让更多查询稳稳留在 L3 和 STLB 覆盖范围之内。

### 1.3 大型区间（百万 \~ 千万键，工作集超过 L3）：跌入 DRAM 泥潭后的两条生路

当唯一键突破数百万、目录加行存轻松超过几十 MiB 的 L3 容量后，几乎每一次哈希探针都是确定性的 LLC Miss + DRAM 随机读（\~100 ns），外加可能的 TLB Miss（开启 2 MiB 透明大页可将单颗核心的 TLB 覆盖面从 8 MiB 扩大到数 GiB，但无法消除 DRAM 带宽与延迟限制）。此时继续往单张扁平大表里塞数据，会同时引爆三场灾难：**缓存命中率归零、单次全表 Rehash 造成剧烈停顿与内存尖刺、多线程合流出现全局争用**。

各家引擎在这个拐点上不约而同地亮出了两组硬核门槛：

- **切分工作集，强行把热点压回 L2/L3**：StarRocks 聚合表的 `two_level_memory_threshold()`（[`aggregate_memory_threshold.h:27-33`](https://github.com/StarRocks/starrocks/blob/f977b53c/be/src/exprs/agg/aggregate_memory_threshold.h#L27-L33)）在 Release 构建下**直接读取硬件的 `CpuInfo::get_l3_cache_size()`**——表一超过 L3 大小立刻转换为 16 子表的 `TwoLevelHashMap`；ClickHouse 默认在 `group_by_two_level_threshold = 100,000` 行时切入 256 桶 `TwoLevelHashMap`；DuckDB 的 `RadixPartitionedHashTable` 更激进（[`radix_partitioned_hashtable.cpp:408-411`](https://github.com/duckdb/duckdb/blob/2bb5316a/src/execution/radix_partitioned_hashtable.cpp#L408-L411)）：只要并行线程数 \(> 2\)，每个线程的本地聚合表容量直接钉死在 **32,768 槽位**（约 256 KiB，稳坐 L2），一满就 `Abandon()` 刷进 Radix 分区，绝不允许本地表膨胀到冲垮 L3。
- **把串行等待变成并行在途访存（MLP）**：对于像 Hash Join 这样在 Probe 阶段避不开跨 L3 随机查表的场景，既然单次 DRAM 读必等 100 ns，那就同时往内存控制器塞进 8\~64 个互不依赖的读请求。Velox 用 64 行三阶段软件流水线预取，StarRocks 则在 `row_count > 2^18` 且探表工作集突破 16/32/64/128 MiB L3 档位（`cache_miss_serious`）时自动激活 **C++20 协程交错预取（`ProbeCoroutine`）**（[`join_hash_table_descriptor.h:189-192`](https://github.com/StarRocks/starrocks/blob/f977b53c/be/src/exec/join/join_hash_table_descriptor.h#L189-L192)）。

### 1.4 极限高基数与外存边界：什么时候“不建表”反而最快？

还有一种更极端的数据形态：输入 1 亿行数据做本地预聚合（Partial Aggregation），结果唯一键有 9,800 万个（聚合压缩率接近 1:1）。此时哈希表没有起到任何缩减网络 Shuffle 数据的作用，反而把 CPU 算力和 DRAM 带宽全烧在了查表、插表和反复 Rehash 上。

现代引擎的共识是**建立实时止损机制（Abandon Partial Aggregation）**：Velox 在读满 10 万行且去重后产出率 \(\ge 80\%\) 时直接销毁本地哈希表改走流式透传（[`HashAggregation.cpp:328-363`](https://github.com/facebookincubator/velox/blob/303d7149/velox/exec/HashAggregation.cpp#L328-L363)）；DuckDB 每线程灌入 1,048,576 行后用 HyperLogLog 估算唯一率，超过 **95%** 即调用 `SkipLookups()` 直接跳过查表（[`radix_partitioned_hashtable.cpp:534-573`](https://github.com/duckdb/duckdb/blob/2bb5316a/src/execution/radix_partitioned_hashtable.cpp#L534-L573)）；StarRocks 更是在流式聚合里内置了六态自适应状态机，在全量预聚合、选择性预聚合和直通旁路之间按数据分布变化动态切换。而当 Final Aggregation 或 Hash Join 真的装不下内存时，就必须越过内存哈希表边界，交由基于 Radix 哈希分区的对称外存 Spill 协议接管（关于多引擎 Spill 撤销与递归恢复协议，可延伸阅读[《深入 Hash Join：从内存哈希表到分区 Spill》]({{< relref "2026-08-26-dive-hash-join.md" >}})）。

| 规模区间 | 典型唯一键规模 | 对应物理墙 | 真正瓶颈 | 典型工程形态 |
| --- | --- | --- | --- | --- |
| **微型值域** | \(< 4\text{K} \sim 64\text{K}\)（或紧凑整数区间） | L1d / L2 内部 | 哈希计算与分支开销 | Direct / Perfect Hash（DuckDB 12-bit、StarRocks `SmallFixedSize`、Velox `kArray`） |
| **中型单表** | 数万 \(\sim\) 百万级 | 越过 L2 与 4 KiB STLB | 假阳性回表、负载因子与缓存容量 | SIMD Tag 分组（`phmap`/Velox）、16-bit Salt（DuckDB \(>8192\)）、L2 门控批预取 |
| **大型跨 L3** | 百万 \(\sim\) 千万级 | 越过 LLC，跌入 DRAM | 100 ns 随机读延迟、Rehash 尖刺、多核争用 | Two-Level（ClickHouse 10 万行 / StarRocks L3 阈值）、Radix 32K 本地 `Abandon`、协程/流水线 MLP |
| **高基数/超内存** | 压缩率 \(\to 1\) 或总内存超限 | DRAM 带宽与内存预算 | 无效建表与 OOM | Partial Agg Bypass/Abandon（Velox 80%、DuckDB HLL 95%）、Radix 外存 Spill |

---

## 二、冲突解决的工程演化：从线性探测到 SIMD 分组、指针嵌入指纹与延迟隐藏

把教科书翻开，哈希冲突的解法通常被粗糙地二分：拉链法（Separate Chaining）和开放寻址（Open Addressing）。可在 OLAP 引擎里，如果把冲突成本拆成三项——**探测步数（Probing Length）**、**假阳性回表解引用代价（False Positive Dereference）**、以及**哈希位相关性引发的聚集雪崩（Clustering）**——你会发现各家早已演化出了五种截然不同的现代工程形态：

![现代 OLAP 引擎五类 Hash Table 冲突消解与槽位布局](/images/diagrams/exec-olap-ht-collision-probing.svg)

### 2.1 纯开放寻址线性探测 + Zero 外置 + 短串分表（ClickHouse / Doris 血统）

ClickHouse 的 `HashTable.h`（被 Apache Doris 直接引入并演化在 [`hash_table.h`](https://github.com/apache/doris/blob/7ac0eb2f/be/src/exec/common/hash_table/hash_table.h#L394-L456)）代表了极简线性探测流派的巅峰。它不做任何额外的元字节数组，直接用“全零字节”表示空槽，同时用模板化的 `ZeroValueStorage` 把合法的零键（例如整数 `0` 或空串）单独外置在一个成员变量里，从而省掉每个槽位的状态位。

但纯线性探测有两处天生短板：一是**一次聚类（Primary Clustering）**——负载因子一旦超过 50%，探测链长度会急剧攀升，因此 `HashTableGrower` 在常规规模下必须把最大填充率压在 **50%**（空间翻倍；Doris 仅在表容量超过 `2^double_grow_degree` 后才放宽到 75%，[`hash_table.h:242-246`](https://github.com/apache/doris/blob/7ac0eb2f/be/src/exec/common/hash_table/hash_table.h#L242-L246)）；二是遇到变长字符串键时，每次线性探测都要解引用指针去 Arena 里做 `memcmp`，一次假冲突就是一次跨内存页的随机读。

为了治变长字符串的痛，ClickHouse 发明了极具想象力的 **`StringHashTable`**，把字符串按字节长度拆成多张独立子表，短串直接塞进定长无符号整数寄存器里比较，彻底消灭指针跳转和 Arena 拷贝。Doris 在 [`string_hash_table.h:30-60`](https://github.com/apache/doris/blob/7ac0eb2f/be/src/exec/common/hash_table/string_hash_table.h#L30-L60) 继承并微调了这套设计，拆成 **6 张子表**（相比 ClickHouse 原版的 0 / 1\~8 / 9\~16 / 17\~24 / >24 五分法，Doris 拆得更细以节省极短串的槽位宽度）：

- `m0`：空字符串（外置单槽 `StringHashTableEmpty`）
- `m1 ~ m4`：分别对应 `1..2B`（`UInt16`）、`3..4B`（`UInt32`）、`5..8B`（`UInt64`）、`9..16B`（`UInt128`），通过 `memcpy_small` 零填充进整数，输出时用一条 `__builtin_clzll` 硬件前导零指令反推真实长度（尾部带 `\0` 的字符串落入兜底表防歧义）
- `ms`：`>16B` 的长串走常规 `HashMapTable<StringRef, HashMapCellWithSavedHash>`，在槽内缓存完整 64 位哈希值先过滤再比对字节。

在批量插入阶段，Doris 的 `lazy_emplace_batch`（[`hash_map_context.h:617-689`](https://github.com/apache/doris/blob/7ac0eb2f/be/src/exec/common/hash_table/hash_map_context.h#L617-L689)）还会先把当前 Block 的行号按长度预分桶进 6 个索引数组，再逐张子表集中探测与预取（`HASH_MAP_PREFETCH_DIST = 16`），把逐行分支预测失败彻底抹平。有趣的是，Doris 后来虽然把数值和组合键聚合表从 ClickHouse `HashTable` 全面迁移到了 SwissTable（`PHHashMap`），却唯独保留了单列短字符串的 `StringHashMap`（[`agg_utils.h:51, 95-96`](https://github.com/apache/doris/blob/7ac0eb2f/be/src/exec/common/agg_utils.h#L51)），足见短串寄存器内联的威力。

### 2.2 SIMD 1-Byte Tag 分组探测（SwissTable / `phmap` / `hashbrown` / Velox）

为什么 StarRocks、Doris（数值/组合键聚合）和 DataFusion（Rust `hashbrown`）都倒向了 SwissTable，而 Velox 也自己手写了高度相似的 16 槽 SIMD 分组表？

核心洞察只有一句话：**别拿昂贵的完整 Key 比较去撞空槽和假冲突，先拿 1 个字节的哈希指纹（Tag）让 SIMD 一次并行审完 16 个槽位。**

在 `phmap::flat_hash_map`（StarRocks [`phmap.h:232-387`](https://github.com/StarRocks/starrocks/blob/f977b53c/be/src/base/phmap/phmap.h#L232-L387)）和 Rust `hashbrown` 里，这套思想被落成了四个严丝合缝的底层设计：

**第一，一次分配、元数据与载荷分离，并在控制数组末尾挂 16 字节哨兵与镜像区。** `phmap` 把控制字节数组 `ctrl_[]`（每槽 1 字节）和真实 `(Key, Value)` 数组 `slots_[]` 放在同一块连续内存的前后两段；容量 `capacity_` 恒为 \(2^k - 1\)（例如 `31`，对应掩码 `0b11111`）。关键在于 `ctrl_[]` 并非只有 `capacity_` 字节，而是分配 `capacity_ + 1 + 15` 字节：紧跟在 `ctrl_[0 .. capacity_-1]` 之后的 `ctrl_[capacity_]` 固定填入哨兵 `kSentinel`，再往后的 15 字节则是 `ctrl_[0 .. 14]` 的完整镜像拷贝。为什么要多出这 16 字节？因为探测起点 `offset = H1(hash) & capacity_` **并不对齐到 16 的倍数**，可能落在数组末尾附近（比如 `capacity_ = 31` 时的 `offset = 25`）。有了哨兵与镜像尾区，CPU 直接执行一条非对齐 128 位加载 `_mm_loadu_si128(ctrl_ + offset)`，就能**无分支、零越界地自然回绕到数组头部**，同时哨兵既不会被匹配也不会被写入，还顺手充当了迭代器的终止标志。（`hashbrown` 略作微调：不设哨兵、容量取整 \(2^k\)、镜像区 16 字节，且把 `slots` 倒序放在 `ctrl` 之前让表指针直接指向 `ctrl[0]`。）

**第二，为单条 SIMD 指令量身定做的有符号控制字节编码。** 每个控制字节都是有符号 `int8_t`，四类状态的取值专门卡在有符号比较的边界上：

| 槽位状态 | 二进制 | 十六进制 | 有符号值 | 设计意图 |
| --- | --- | --- | --- | --- |
| **已占用（Full）** | `0xxxxxxx` | `0x00 ~ 0x7F` | `0 ~ 127` | 最高位为 `0`，低 7 位存放哈希指纹 `H2` |
| **空槽（`kEmpty`）** | `10000000` | `0x80` | `-128` | 从未写入过，探测遇到它即可提前终止 |
| **已删除（`kDeleted`）** | `11111110` | `0xFE` | `-2` | 墓碑，查找需跳过，但新插入可原地复用 |
| **哨兵（`kSentinel`）** | `11111111` | `0xFF` | `-1` | 永久不可匹配、不可写入，兼作边界与迭代终点 |

这三个负数不是随便选的：判断“可插入（`kEmpty` 或 `kDeleted`）”只需做有符号比较 `c < kSentinel`（即 `c < -1`），一条 `_mm_cmpgt_epi8(_mm_set1_epi8(-1), ctrl)` 就能同时筛出空槽和墓碑，并自动避开哨兵（`-1`）与全部已占槽（`>= 0`）。更妙的是在支持 SSSE3 的 CPU 上，判“仅空槽”连 `-128` 常量都省了——对控制向量执行 `_mm_sign_epi8(ctrl, ctrl)` 后，正数与 `0` 符号位仍为 `0`，`-1` 取反成 `+1`、`-2` 取反成 `+2` 符号位也变成 `0`，唯独 `-128`（`0x80`）做 8 位有符号取反会溢出回 `-128`，从而只有空槽的最高位保持 `1`！

**第三，正交切分 `H1`/`H2` 与指令级分组探测。** 64 位哈希值 `h` 必须拆成**互不重叠**的两段：`phmap` 用高 57 位 `H1(h) = h >> 7` 定起始偏移、低 7 位 `H2(h) = h & 0x7F` 作指纹（`hashbrown` 则用低位定桶、高 7 位 `h >> 57` 作指纹）。只要两段比特正交，撞进同一位置的两个不同键碰巧共享同一个 7 位指纹的概率就严格压在 \(1/2^7 = 1/128 \approx 0.78\%\)。一次 `Find` 的核心热循环极其紧凑：

```cpp
// phmap::flat_hash_map / Abseil SwissTable 探测主循环示意（SSE2）
size_t offset = H1(hash) & capacity_;
size_t step   = 0;
uint8_t h2    = H2(hash); // 0x00 .. 0x7F

while (true) {
    // 1. 一次加载 16 个控制字节；末尾哨兵 + 15B 镜像区保证非对齐回绕不越界
    __m128i ctrl = _mm_loadu_si128(reinterpret_cast<const __m128i*>(ctrl_ + offset));

    // 2. 并行比对 16 个字节与 h2，将 16 个结果的最高位压缩成 16-bit 掩码
    uint32_t match_mask = _mm_movemask_epi8(_mm_cmpeq_epi8(ctrl, _mm_set1_epi8(h2)));

    // 3. 仅对指纹命中的候选槽（绝大多数时候 0 或 1 个）才解引用 slots_[] 比对真实 Key
    while (match_mask != 0) {
        size_t pos = (offset + __builtin_ctz(match_mask)) & capacity_;
        if (LIKELY(key_eq(slots_[pos].key, key))) {
            return &slots_[pos];
        }
        match_mask &= (match_mask - 1); // 清除最低位 1，继续下一候选
    }

    // 4. 若这 16 字节里存在任意 kEmpty，说明后续分组绝不可能存有该键，立即返回未命中
    if (LIKELY(_mm_movemask_epi8(_mm_cmpeq_epi8(ctrl, _mm_set1_epi8(kEmpty))) != 0)) {
        return nullptr;
    }

    // 5. 16 槽全满（或全满+墓碑），按 16 字节步进做三角数探测跳到下一组
    step += 16;
    offset = (offset + step) & capacity_;
}
```

这里还有三处细节把性能拧到了极致：
- **三角数跳组**：跨组采用 `+16, +32, +48, ...` 的三角数增量（第 \(i\) 步累计偏移 \(16 \cdot \frac{i(i+1)}{2} \bmod 2^k\)）。数论上，三角数模 \(2^{k-4}\) 恰好构成所有分组的完整排列，既能打散线性探测的一次聚类，又能保证遍历全部槽位绝不死循环。（ARM64 NEON 因缺少单条 `pmovmskb`，改用 `vceqq_s8` + `vshrn_n_u16` 将 16 槽压进 64 位每槽 4-bit 掩码；无 SIMD 平台则退化为 8 字节 `uint64_t` SWAR 位运算。）
- **无分支同步镜像尾区**：写入 `ctrl_[i]` 时若 `i < 15`，必须同步更新镜像区 `ctrl_[i + capacity_ + 1]`。`phmap` 用一行无分支位运算 `ctrl_[((i - 15) & capacity_) + (15 & capacity_)] = h2` 直接搞定——当 `i < 15` 时算出镜像区下标，当 `i >= 15` 时恰好算回 `i` 本身做幂等覆写，全程零分支。
- **删除尽量不留墓碑**：传统开放寻址删除必须写 `kDeleted` 防止截断探测链，而 SwissTable 删除槽 `pos` 时会检查 `pos` 前后各 16 字节的窗口——只要左侧最近 `kEmpty` 与右侧最近 `kEmpty` 间距 `< 16`，就说明任何覆盖 `pos` 的 16 字节探测窗口本来就包含空槽，绝无溢出链经过 `pos`，此时直接改写为 `kEmpty` 并归还 `growth_left_`；只有真正落在连续 16 槽全满的稠密区里才写 `kDeleted`，且墓碑攒多耗尽 `growth_left_` 但有效元素还少时，会原地执行 `DropDeletesWithoutResize` 把墓碑洗回空槽而不扩容。

**第四，为什么它能在 87.5%（\(7/8\)）负载因子下依然极快？** 算一笔直观的数学账：传统单槽线性探测在负载因子 \(\alpha = 7/8\) 时，未命中查找的期望探测步数高达 \(\frac{1}{2}\left(1 + \frac{1}{(1-\alpha)^2}\right) = 32.5\) 个槽，因此 ClickHouse 传统线性探测表被迫把上限卡在 50%（平均 2.5 槽）。而在 16 槽 SIMD 分组里，一组一次审完 16 槽，在 \(\alpha = 7/8\) 下整组 16 槽恰好全满（被迫跳向第二组）的概率只有 \((7/8)^{16} \approx 11.8\%\)——**约 88% 的查找在第一组（仅 1 次 SIMD 加载）就结束了**；组内平均有 \(16 \times 0.875 = 14\) 个已占槽，每次查找期望撞上假阳性指纹并无效回访 `slots_[]` 的次数仅 \(14/128 \approx 0.11\) 次，意味着**近 90% 的未命中请求连 `slots_[]` 所在的 Cache Line 都不会加载**。如果再激进推到 \(15/16\)，满组溢出概率会陡增到 \((15/16)^{16} \approx 35.6\%\)，因此 \(7/8\)（每组平均留 2 个空槽）正好踩在空间密度与探测深度的甜点上。

不过，把 `ctrl_[]` 和 `slots_[]` 拆成两段也有代价：它极度偏向**高未命中率**场景（不命中就不碰载荷），但一旦大表跨过 L3/STLB 且查询以**命中为主**，先读 `ctrl_[]` 再读相距甚远的 `slots_[]` 就会变成两次独立的 Cache/TLB Miss。Velox 的 `HashTable`（[`HashTable.h:897-931`](https://github.com/facebookincubator/velox/blob/303d7149/velox/exec/HashTable.h#L897-L931)）恰好沿着反方向做了更具硬件美感的“抠字节”融合改造——它把控制字节和指针直接焊进了**精确对齐两条 64 字节 Cache Line 的 128 字节 `Bucket`**：

```cpp
// velox/exec/HashTable.h (布局示意)
// sizeof(Bucket) == 128 (精确 2 条 Cache Line，容纳 16 个槽位，均摊 8 字节/槽)
struct Bucket {
  TagVector tags_;         // 16 bytes: 16 个 1B tag (MSB=1 为已占 + 7-bit hash>>38; 0x00=空, 0x7f=墓碑)
  char pointers_[16 * 6];  // 96 bytes: 16 个 48-bit (6 字节) 紧缩行指针!
  char padding_[16];       // 16 bytes: 尾部填充对齐到 128 字节
};
```

注意那个 `pointers_[16 * 6]`：现代 64 位 CPU 用户态只使用低 48 位虚拟地址，Velox 干脆把每个行指针压缩成 **6 字节**，让 16 个 Tag（16B）+ 16 个指针（96B）+ 对齐填充（16B）严丝合缝塞进 128 字节，既保证 Tag 和指针紧挨着同享极佳空间局部性，又把均摊槽位开销压到了惊人的 **8 字节/槽**（负载因子上限 `0.7`，跨桶采用 128B 步长的线性探测）。

### 2.3 指针高 16 位嵌入指纹：DuckDB Salt 与 LingoDB 桶级 Bloom Tag

既然 64 位指针的高 16 位在 x86-64 / ARM64 用户态（Android 除外）本来就全为 0，与其像 Velox 那样拆成 6 字节做非对齐读写，能不能直接把高 16 位利用起来？DuckDB 和 LingoDB 给出了两种精妙绝伦的用法：

1. **DuckDB `ht_entry_t`（16-bit Salt + 48-bit 指针）**（[`ht_entry.hpp:27-97`](https://github.com/duckdb/duckdb/blob/2bb5316a/src/include/duckdb/execution/ht_entry.hpp#L27-L97)）：
   每条 8 字节槽位的高 16 位存 `hash & 0xFFFF000000000000`，低 48 位存 `TupleDataCollection` 行指针。单次 8 字节对齐加载同时拿到指纹与地址，假阳性过滤精度高达 \(1 - 2^{-16} \approx 99.9985\%\)。更巧妙的是，DuckDB 在聚合表 `GroupedAggregateHashTable` 里还顺手利用 Salt 最高 5 位做**奇数步长双散列探测**（`SaltIncrementAndWrap`，[`aggregate_hashtable.cpp:334-340`](https://github.com/duckdb/duckdb/blob/2bb5316a/src/execution/aggregate_hashtable.cpp#L334-L340)）：
   ```cpp
   // 步长取高 5 位强制为奇数 -> {1, 3, 5, ..., 31}，必然与 2 的幂容量互质
   offset += (salt >> (64 - 5)) | 1;
   offset &= capacity_mask;
   ```
   只需两条位运算指令，就用 16 种不同步长把线性探测的一次聚类打散，同时保持较小的局部跨度以利硬件预取器工作。
2. **LingoDB / Umbra `HashIndexedView`（16-bit 桶级 Bloom Filter）**（[`helpers.h:325-346`](https://github.com/lingo-db/lingo-db/blob/fab813e8/include/lingodb/runtime/helpers.h#L325-L346)，[`LazyJoinHashtable.cpp:20-31`](https://github.com/lingo-db/lingo-db/blob/fab813e8/src/runtime/LazyJoinHashtable.cpp#L20-L31)）：
   拉链法在 Join 探测未命中时，如果该桶里恰好挂了其他碰撞键的链表，传统做法必须跳进链表逐一解引用比对。LingoDB 把 48 位指针左移 16 位、腾出低 16 位作为**整条桶链的微型 Bloom Filter**：构建时查 2048 项预生成表 `bloomMasks[hash >> 53]`（每个掩码置位固定数量比特），通过原子 CAS 把新元素的掩码按位或（`|`）进桶头指针里。Probe 时只需一条位运算 `!(tag & ~entry)`——如果桶里根本不含当前 Probe 键的指纹比特，**连链表头节点都不需要解引用，直接判定未命中**。

### 2.4 1:N 重复键的专属解法：开放寻址消异键 + 紧凑链挂同键

上面三种机制解决的都是“不同键撞到同一个桶”的冲突。但到了 Hash Join 场景，另一个问题浮出水面：**Build 侧本身存在大量 1:N 重复键**（例如订单明细表按 `product_id` 连接，同一个商品重复 500 次）。

如果把 500 个相同的键全塞进开放寻址数组里，它们会连成一条长达 500 槽的拥堵带，把周围所有其他键的探测路径全部堵死；可如果退回纯拉链法，不同键的碰撞和同键的重复又混在同一条链里，Probe 扫链时每个节点都不得不重新比对一次 Key。

StarRocks 的 **`LinearChainedJoinHashMap`**（[`join_hash_map_method.h:71-151`](https://github.com/StarRocks/starrocks/blob/f977b53c/be/src/exec/join/join_hash_map_method.h#L71-L151)）与 DuckDB 的 **`JoinHashTable`**（[`join_hashtable.cpp:755-790`](https://github.com/duckdb/duckdb/blob/2bb5316a/src/execution/join_hashtable.cpp#L755-L790)）不约而同地给出了最佳混合架构——**两类冲突分层治理**：

- **不同键的哈希碰撞**，在开放寻址目录里解决（StarRocks `first[]` 采用三角/二次探测 `bucket = (bucket + probe_times) & mask`，DuckDB 采用 `+1` 线性探测），并用指纹拦截假阳性（StarRocks 当桶数 \(\le 2^{24}-1 \approx 1677\) 万时，直接把 **`8-bit 指纹 << 24 | 24-bit 行号`** 压缩进 32 位 `uint32_t` 的 `first[bucket]` 里，空间比 64 位指针再省一半！）。
- **完全相同的重复键（1:N）**，全部串到该槽位挂出的单链表里（StarRocks 走 `uint32_t next[row_idx]` 数组，DuckDB 走元组末尾内嵌的 `next_ptr`）。

这种分层带来一个极其漂亮的副产品：**同一条 `next` 链上的所有行，其连接键必然完全相等**（StarRocks 在模板 traits 里直接标为 `AreKeysInChainIdentical = true`，[`join_hash_map_method.h:133`](https://github.com/StarRocks/starrocks/blob/f977b53c/be/src/exec/join/join_hash_map_method.h#L133)）。只要链头确认 Key 匹配，后续顺着 `next[]` 展开 500 行匹配结果时**连一次 Key 比较都不用做**！

### 2.5 隐蔽杀手：哈希位相关性与双阶段碰撞风暴

冲突率飙升有时并不怪探测算法，而是怪**哈希比特位被重复消费了**。设想一个分布式两阶段聚合：上游节点用 `CRC32C(key) % 64` 把数据 Shuffle 到 64 个下游节点，于是落到第 5 号节点的所有 Key，其哈希值低 6 位全都是 `000101`！如果下游本地建表继续用同一个 `CRC32C(key)` 并取低位掩码 `hash & (cap - 1)`，所有 Key 的低 6 位全部撞死在一起，整张哈希表瞬间退化成 64 倍退化的灾难性长链。

翻看各家源码，几乎所有成熟引擎都在哈希位隔离上布了重兵：

- **双阶段换种与混合（Re-seeding & Mixing）**：StarRocks 聚合表明确区分 `PhmapSeed1`（Phase 1 本地聚合）和 `PhmapSeed2`（Phase 2 Shuffle 后全局聚合），且对 `CRC32C` 的输出必须再过一遍 128 位乘法混合 `phmap_mix`（[`hash.h:68-89, 172-216`](https://github.com/StarRocks/starrocks/blob/f977b53c/be/src/base/hash/hash.h#L68-L89)），确保低 7 位 `H2` 指纹分布均匀；Doris 在二阶段聚合对 32/64 位整型键套上了 `HashMixWrapper`（[`agg_utils.h:53-56`](https://github.com/apache/doris/blob/7ac0eb2f/be/src/exec/common/agg_utils.h#L53-L56)）打破上游 Shuffle 的低位同余性。
- **64 位哈希的正交位切片**：DuckDB 对 64 位哈希的每一段比特都有严格宪章（[`radix_partitioning.hpp:23-65`](https://github.com/duckdb/duckdb/blob/2bb5316a/src/include/duckdb/common/radix_partitioning.hpp#L23-L65)）：
  - `[63..48]`（最高 16 位）专供 `ht_entry_t` 的 Salt；
  - `[47 .. 48 - radix_bits]`（紧接 Salt 下方最多 12 位）专供 Radix 分区号；
  - `[log2(cap)-1 .. 0]`（最低位）专供哈希表桶下标。
  三段比特绝不交叠，保证同一个 Radix 分区内的键，其 Salt 和桶下标依然完全随机独立。Velox 同样在 `HashTable::checkHashBitsOverlap()`（[`HashTable.cpp:1813-1822`](https://github.com/facebookincubator/velox/blob/303d7149/velox/exec/HashTable.cpp#L1813-L1822)）里硬性校验表的低位索引绝不能侵入 Spill 分区占用的高位 `[48, 51)`。

### 2.6 跨过 L3 后：用批预取与协程把冲突探测做成访存流水线

当表大到装不进 L3 时，哪怕每槽只有 1 次探测、0 次冲突，逐行顺序查表仍然只能跑出 \(\frac{1}{100\text{ ns}} = 10\text{M lookups/s}\) 的可怜吞吐，因为 CPU 乱序窗口（ROB）一次只能看到有限几条指令后的访存地址，绝大多数周期都在空转等 DRAM。

把向量化批量查表改造成**内存级并行（Memory-Level Parallelism, MLP）流水线**，是现代引擎在千万级哈希表上拉开 2~4 倍性能差距的胜负手：

1. **Velox 三阶段批预取流水线（`kPrefetchSize = 64`）**（[`HashTable.cpp:435, 492-517`](https://github.com/facebookincubator/velox/blob/303d7149/velox/exec/HashTable.cpp#L435)）：把每 64 行探针拆成三遍横扫——第一遍 `preProbe` 算出 64 个桶偏移并对 64 个 128B 桶同时发射 `__builtin_prefetch`；等这一轮跑完，第一批桶已经从 DRAM 飞回 L1；第二遍 `firstProbe` 做 16 路 SIMD Tag 比对并拿到首个候选行指针，立刻对 64 个 `RowContainer` 行的 `-8` 偏移（紧挨着行指针前方的 `normalizedKey`）再次发射预取；第三遍 `fullProbe` 才真正校对键值与处理冲突（并附带 `extraCheck` 防止同一 64 行窗口内前序行刚插入的新分组漏判）。在展开 Join 1:N 重复链时，Velox 还用 `listJoinResultsInterleaved`（[`HashTable.cpp:2149-2250`](https://github.com/facebookincubator/velox/blob/303d7149/velox/exec/HashTable.cpp#L2149-L2250)）同时交错推进 **8 条链表** 并预取下一跳。
2. **DuckDB 无分支预热循环**（[`aggregate_hashtable.cpp:871-882, 907-912`](https://github.com/duckdb/duckdb/blob/2bb5316a/src/execution/aggregate_hashtable.cpp#L871-L882)）：在进入带分支的探测循环前，先跑一段纯无分支紧密循环，把整批 2,048 行的桶偏移算出来并轻触 `entries[ht_offset].IsOccupied()` 累加到一个防优化掉的计数器里，借 CPU 乱序执行引擎自身把多个 DRAM 请求同时压进加载缓冲队列（LFB）。
3. **StarRocks C++20 协程交错探测（`ProbeCoroutine`）**（[`join_hash_map.hpp:649-714`](https://github.com/StarRocks/starrocks/blob/f977b53c/be/src/exec/join/join_hash_map.hpp#L649-L714)）：传统手写三阶段状态机在面对 Join 变长重复链时代码极难维护（因为不同行的链长不一致，很难同步推进）。StarRocks 直接用无栈 C++20 协程把单行探测写成直观的顺序代码，每当需要解引用 `build_data + build_index` 和 `next + build_index` 前，调用宏 `PREFETCH_AND_COWAIT` 发出预取并 `co_await std::suspend_always{}` 让出控制权，调度器在 `group_size` 个活跃协程间轮转——既保留了清晰的串行代码逻辑，又吃满了 DRAM 并发通道。

---

## 三、Rehash 的三重灾难与六大工业级解法

如果说冲突解决决定的是“稳态查询速度”，那么 **Rehash（动态扩容重散列）** 决定的是“高基数查询会不会在中途崩溃或严重长尾抖动”。

把一次朴素的哈希表倍增（`malloc(2N) → 遍历旧表逐个重算哈希插入新表 → free(N)`）放到 5,000 万行的聚合查询里，立刻会撞上三重灾难：

1. **3 倍峰值内存尖刺**：旧表占 1 GiB，申请 2 GiB 新表，搬迁期间新旧两表同时驻留，峰值瞬间冲上 **3 GiB**——哪怕最终表只占 2 GiB，查询也极容易在扩容瞬间被内存限额一刀斩杀。
2. **全局 STW 停顿与带宽打满**：把几千万条记录重新散列写入全新的随机内存页，会瞬间击穿 L3 与 TLB，不仅要重算千万次哈希，还可能搬运几十字节的聚合状态和变长字符串，造成数百毫秒乃至秒级卡顿。
3. **多核扩展性崩塌**：如果多个执行线程共享一张全局表，Rehash 期间所有线程都必须停在屏障前干等。

沿着“能不 Rehash 就不 Rehash → 必须 Rehash 时绝不搬大对象也不重算哈希 → 把全表扩容压成原地页表扩展或 1/256 局部扩容”的思路，开源 OLAP 引擎沉淀出了六大硬核解法：

![三类 OLAP 算子的哈希表契约与 Rehash 治理路径](/images/diagrams/exec-olap-ht-rehash-and-operators.svg)

### 3.1 解法一：Join 专属的“两阶段精确分配”，从根源彻底消灭 Rehash

Hash Join 与 Hash Aggregation 有一个根本区别：**Join 的 Build 侧无法像聚合那样边插边压缩行数，它天生必须把全部 Build 行留下来供 Probe 匹配。**

既然无论如何都要保留全量行，那为什么要在不知道总行数的时候急着建哈希索引、白白经历十几次倍增 Rehash？StarRocks、Doris、DuckDB、DataFusion、LingoDB 在 Join 算子里给出了完全一致的答案——**Build 阶段只攒数据、不建目录；等 EOF 拿到精确行数 `N` 之后，一次性按最终容量分配哈希目录，整个生命周期 0 次 Rehash**：

- **StarRocks / Doris**：Build 阶段把所有输入 Chunk 顺序追加到列式 `build_chunk` / `_build_side_mutable_block`，待上游全部到达后才调用 `JoinHashTable::build()` / `prepare_build()`，按 `calc_bucket_size(row_count)` 一次分配 `first[]` 和 `next[]`。
- **DuckDB**：把“存数据”和“建索引”彻底拆成了**正文区**与**目录区**两层。这里的**目录**（Pointer Table / `hash_map`）本质上是一张连续数组，**里面一个字节的 Key 或 Payload 都不存**，每个槽位只有 8 字节的 `ht_entry_t`（高 16 位 Salt + 低 48 位行指针）。以 100 万行、每行 100 字节（共 100 MiB）的 Build 表为例：
  1. **Sink 阶段只写正文、完全没有目录**：多线程把每行按 `[NullBitmap | Key 列 | Payload 列 | 8字节槽(暂存 64-bit hash_t)]` 紧凑追加进 `RadixPartitionedTupleData`（`sink_collection`），零空洞、零查表、零 Rehash；
  2. **Finalize 阶段拿到精确 `Count()` 后一次建目录**：调用 `AllocatePointerTable()` 按 `load_factor = 2.0`（内存紧张时降为 `1.5`）向上取 2 的幂一次分配 209 万槽（仅 **16 MiB**），连清零目录都拆成多线程任务并行 `memset`（`HashJoinTableInitEvent`，每任务至少 131,072 槽，[`physical_hash_join.cpp:1001-1041`](https://github.com/duckdb/duckdb/blob/2bb5316a/src/execution/operator/join/physical_hash_join.cpp#L1001-L1041)）；
  3. **扫正文填目录，并原地覆写 8 字节槽**：扫描正文直接读出每行末尾的 `hash_t` 插入目录（异键碰撞线性探测，同键重复挂链），完成后**顺手把这同一个 8 字节槽原地覆写成重复键链表的 `next_ptr`**（[`join_hashtable.cpp:762-786`](https://github.com/duckdb/duckdb/blob/2bb5316a/src/execution/join_hashtable.cpp#L762-L786)），一字节都没多花！

  这种“目录与正文解耦”还顺带换来了四重系统级红利：开放寻址为了低冲突留出的 50% 空槽只浪费 8 字节/槽（100 MiB 正文保持 100% 满载）；全程 0 次 Rehash；Probe 阶段先查紧凑目录并用高 16 位 Salt 挡掉 99.9985% 的假碰撞，绝大多数未命中根本不碰正文；更重要的是，当 Build 正文超出内存触发外存 Spill 时，Finalize 只需按批把能装进内存的若干 Radix 分区加载回来，**临时为这一批分区建一张小目录，Probe 扫完直接扔掉目录再换下一批**，目录成了一个极轻量的可重建索引层。
- **DataFusion / LingoDB**：DataFusion 拼完所有 Build `RecordBatch` 后调用 `HashTable::with_capacity(num_rows)` + `vec![0; num_rows]`；LingoDB 攒完 `GrowingBuffer` 后按 `nextPow2(len * 1.25)` 一次分配 `HashIndexedView`。

### 3.2 解法二：目录与载荷彻底分离 + 缓存哈希，让 Rehash 变成纯指针重排

Aggregation 必须边读边查表合并状态，无法像 Join 那样攒完再建。既然 Rehash 躲不掉，第一条铁律就是**绝不在 Rehash 时搬运聚合状态和变长键，也绝不重算昂贵的哈希函数**。

- **Velox `RowContainer` 纹丝不动**：所有分组键、聚合累加器、变长字符串都住在稳定的 `RowContainer` 页面里，哈希目录 `tableAllocation_` 只存 1 字节 Tag + 6 字节行指针。Rehash 时只需释放旧目录、分配新目录，按 1,024 行一批遍历 `RowContainer` 塞回指针（[`HashTable.cpp:1501-1556`](https://github.com/facebookincubator/velox/blob/303d7149/velox/exec/HashTable.cpp#L1501-L1556)）。如果表处于 `kNormalizedKey` 模式，甚至直接读行指针前 8 字节的 `normalizedKey` 做乘法混淆，连分组列都不用解包。
- **已知唯一性，Rehash 彻底跳过 Key 比较**：无论是 Velox 的 `insertForGroupBy`（[`HashTable.cpp:1287-1351`](https://github.com/facebookincubator/velox/blob/303d7149/velox/exec/HashTable.cpp#L1287-L1351)）还是 DuckDB 的 `ReinsertTuples`（[`aggregate_hashtable.cpp:311-369`](https://github.com/duckdb/duckdb/blob/2bb5316a/src/execution/aggregate_hashtable.cpp#L311-L369)），都牢牢抓住了聚合表的一个不变量——**已经在 `RowContainer` / `TupleDataCollection` 里的每一行，彼此之间的分组键必然互异**。因此 Rehash 往新表插槽时，根本不需要比对 Key，只找第一个空槽填进去就行！Velox 在 SSE2 下甚至省掉了 Tag 相等比较指令，直接抽 16 个 Tag 的最高位掩码（`~simd::toBitMask(...) & 0xffff`）一条指令定位空槽。
- **DataFusion 存下 `saved_hash`**：`GroupValuesPrimitive` 在 `HashTable<(usize, u64)>` 里冗余保存了 8 字节哈希值（[`primitive.rs:103-112`](https://github.com/apache/datafusion/blob/a4b8198c/datafusion/physical-plan/src/aggregates/group_values/single_group_by/primitive.rs#L103-L112)），也是为了高基数扩容时避免重算哈希与回访外部值数组。

这里值得专门厘清一个常见误区：**ClickHouse 真的把 `(Key, Value)` 全塞进哈希表了吗？它和 DuckDB / Velox 的“彻底分离”究竟差在哪？**

其实，ClickHouse 并没有把庞大的聚合状态或 Join 载荷列塞进哈希槽，它采取的是“**半分离（Key 内联 + 大载荷外置）**”：在 `Aggregator` 里，`HashTable::buf[]` 槽位中的 `Mapped` 是 8 字节的 `AggregateDataPtr`，真正的聚合累加器状态（`SUM`/`HLL`/`groupArray` 等）和变长长串正文全部外置在独立的 `Arena`（`aggregates_pool` / `keys_pool`）里；在 `HashJoin` 里，Build 侧的列式 `Blocks` 同样完全外置，槽位里只放指向外部列的 `RowRef` / `RowRefList`。**真正构成架构分水岭的，在于 `Key` 本身是否内联在哈希槽里**：

| 对比维度 | ClickHouse `HashTable`（**半分离：Key 内联 + 载荷指针**） | DuckDB / Velox（**全分离：Key 与载荷全外置，纯 8B 目录**） |
| --- | --- | --- |
| **哈希槽内存储内容** | 定长 `Key`（`UInt32/64/128/256` 或短串内联整型）/ `StringRef`（含 `saved_hash`）+ 外部指针（8B `AggregateDataPtr` / 16B `RowRef`） | **完全不存 Key**，恒为 **8 字节**（DuckDB：`16-bit Salt + 48-bit 指针`；Velox：128B 桶均摊 `1B Tag + 6B 指针`） |
| **单槽宽度** | 随键变宽：`int64` 为 16B、`UInt128` 为 24B、`UInt256` 为 40B、`StringRef` 为 32B | **恒定 8 字节**（与分组列数和类型无关） |
| **100 万键 50% 负载目录体积** | `UInt128` 需 48 MiB（空槽占 24 MiB）；`UInt256` 高达 80 MiB | 恒定 **16 MiB**（空槽仅占 8 MiB），Key 紧凑存于行存容器无空洞 |
| **定长键查表访存次数** | **1 次随机读（0 次指针跳转）**：读到槽位时 Key 就在眼前，直接寄存器比对 | **2 次访存**：先读 8B 目录，Salt/Tag 命中后再解引用行指针回表比对 Key |
| **聚合结束扫描输出** | 需遍历含 50% 空槽的稀疏 `buf[]`（Doris 后来专门加 `AggregateDataContainer` 绕开） | 直接顺序扫描紧凑无空洞的 `TupleDataCollection` / `RowContainer`，不碰目录 |

这两种设计各有鲜明赌注：ClickHouse 赌的是 OLAP 最高频的单列整数、双列定长键与短串查询——**宁愿在宽键时多交一点空槽学费并特化出几十套模板，也要换取热路径上“读槽即比 Key、少一次 100 ns 随机跳转”的极致延迟**；而 DuckDB / Velox 赌的是多列宽键、高基数下的目录极度瘦身，并用指纹拦住 99%+ 假碰撞，顺手拿到顺序输出、Join 零 Rehash 与按页 Spill 的统一架构。

### 3.3 解法三：Linux `mremap` 原地零拷贝页表扩容 + 2 的幂原地重插（ClickHouse / Doris）

正因为 ClickHouse 把定长 Key 与载荷指针直接内联在单块连续数组 `buf[]` 里，宽键大表动辄膨胀到数百 MiB 乃至数 GiB。如果按传统方式 `malloc(2N) + memcpy + free(N)` 扩容，3x 内存尖刺和全表拷贝代价极高——ClickHouse 在 `HashTable::resize()`（完整保留在 Doris [`hash_table.h:1031-1078`](https://github.com/apache/doris/blob/7ac0eb2f/be/src/exec/common/hash_table/hash_table.h#L1031-L1078)）里用一套操作系统与算法协同的技巧把它压了下去：

1. **前期 4x、大表 2x 的两段式生长**：表还小时（`size_degree < double_resize_threshold`，ClickHouse 与 Doris 默认 `23`，即 \(2^{23} \approx 838\) 万槽），每次扩容直接把容量 **翻 4 倍（`size_degree += 2`）**，把前期高频 Rehash 次数砍掉一半；一旦跨过 \(2^{23}\) 槽，自动切换为温和的 **翻 2 倍（`+= 1`）**，避免大表内存暴涨。
2. **`mremap(MREMAP_MAYMOVE)` 原地扩展虚拟地址**：当内存块超过 `MMAP_THRESHOLD`（ClickHouse 默认 64 MiB）时，定制 `Allocator::realloc` 不走用户态堆分配，而是直接调用 Linux 系统调用 `mremap`。内核只需修改进程页表（PTE），把原有物理页映射到扩大后虚拟地址的前半段 `[0, old_size)`，**零物理内存拷贝、峰值内存从 3x 直接降为 2x**；后半段 `[old_size, 2*old_size)` 则由内核挂上按需分配的匿名零页（Demand Zeroing），天然契合 ClickHouse 全零表示空槽的设计，连 `memset` 都省了。
3. **2 的幂原地重插与回绕尾链修补**：为什么敢在同一个 `buf[]` 里从左向右扫描 `[0, old_size)` 原地搬运而不踩坏尚未扫描的旧槽？因为容量翻倍后，哈希掩码只在第 \(k\) 位多暴露了 1 个比特——第 \(k\) 位为 `0` 的约 **50% 元素**理想位置不变（要么原地不动，要么左移进已经扫描腾空的槽位），第 \(k\) 位为 `1` 的另外 **50% 元素**则直接跳进全新全空的右半区 `[old_size, 2*old_size)`，绝不会覆盖右侧尚未扫描的旧槽！

这段代码最容易写错的角落隐藏在末尾第 1067~1077 行——**回绕尾链（Wrap-around Tail Chain）**：假设旧表里元素 `o` 原本哈希落在数组最末端，但因为末端已被元素 `x` 占住，线性探测回绕到了数组开头的槽位 `[0]`；扩容并扫完 `[0, old_size)` 后，`o` 先挪到了右半区紧挨着 `old_size` 的槽位，随后扫到末端的 `x` 时，`x` 自己搬去了右半区更远的新家，留下一道空洞把 `o` 孤零零地甩在了断裂链之后！因此 ClickHouse 在主循环结束后，必须紧跟着补扫右半区头部连续非空槽，把被遗落的尾链元素再 `reinsert` 归位：

```cpp
// 源自 ClickHouse HashTable.h / Doris be/src/exec/common/hash_table/hash_table.h:1060-1077
size_t i = 0;
for (; i < old_size; ++i) {
    if (!buf[i].is_zero(*this)) {
        reinsert(buf[i], buf[i].get_hash(*this));
    }
}
// 关键修补：处理旧表末尾因线性探测回绕到头部、在扩容搬迁后被甩在右半区起点的尾链元素
for (; !buf[i].is_zero(*this); ++i) {
    reinsert(buf[i], buf[i].get_hash(*this));
}
```

**那么，`phmap` 或 Velox / DuckDB / DataFusion 等其他引擎也会用 `mremap` 吗？答案是几乎都不会。** 一方面，`phmap` / `hashbrown` 在单块内存里把 `ctrl_[]` 和 `slots_[]` 前后拼接，拉长末尾根本无法在不踩烂 `slots_[]` 的前提下原地扩充 `ctrl_[]`，其三角数分组探测与哨兵镜像区也不满足单调原地重插不变量；另一方面，全分离 8 字节目录、Join 两阶段零 Rehash 以及 Two-Level/Radix 拆小表已经从架构上规避了巨型单表 Rehash。

更重要的是，`mremap` 原地扩容在现代多核高并发 OLAP 集群里存在**四重不容忽视的工程弊端**：

1. **内核 `mmap_lock` 写锁争用与多核 TLB Shootdown**：`mremap` 修改进程虚拟地址映射必须抢占进程级内核写锁 `mmap_lock`，且移动虚拟地址后，内核必须向同进程正在运行的所有 CPU 核心发送核间中断（IPI）强刷 TLB。在 64/128 核高并发查询场景下，多个查询同时触发 `mremap` 会互相抢锁并反复打断其他计算核心，引发剧烈的整机延迟抖动。
2. **绕过 `jemalloc` / `tcmalloc` 内存池复用**：`mmap` 申请的大块内存在查询结束后直接 `munmap` 归还给操作系统，下次查询重新 `mmap` 又要经历高频缺页异常（Page Fault）与内核页清零，短查询高并发下很容易把 `%sys` 内核态 CPU 打高。
3. **强绑定 Linux 且背负 50% 低负载因子稳态账**：`mremap(MREMAP_MAYMOVE)` 是 Linux 专属系统调用（macOS/Windows 只能退回普通拷贝）；为了维持原地重插不变量，不得不坚持全零空槽 + 2 的幂线性探测，负载因子锁死在 50%，稳态下反而比 87.5% 的 SwissTable 多占约 75% 槽位空间。
4. **在 Two-Level 架构下收益边际递减**：ClickHouse 自己在高基数聚合里默认 10 万行就会切入 256 桶的 `TwoLevelHashMap`，单张子表通常远小于 64 MiB 的 `MMAP_THRESHOLD`，实际上压根走不到 `mremap`——它更多是单层扁平大表在迫不得已长到几百 MiB 以上时的兜底防线。

### 3.4 解法四：化整为零——One-Level → Two-Level 动态切换与固定尺寸驱逐缓存

即便有 `mremap`，把一张 4 GiB 的单表瞬间扩成 8 GiB 依然会带来剧烈的内存抖动与多核合流瓶颈。治本之道是**把一张巨表拆成几十到几百张独立小表**：

- **ClickHouse / Doris / StarRocks 的 Two-Level 切换**：ClickHouse 在达到 10 万行（或内存阈值）时调用 `convertToTwoLevel()`，按哈希高 8 位拆成 **256 张子表**；StarRocks 在超过 L3 缓存大小时切换到 16 子表的 `phmap::parallel_flat_hash_map`（用 `((h>>8)^(h>>16)^(h>>24)) & 15` 均衡路由）；Doris 的 `BucketedAggSinkOperatorX` 则直接在算子层维护 **256 桶**独立哈希表（[`dependency.h:415-464`](https://github.com/apache/doris/blob/7ac0eb2f/be/src/exec/pipeline/dependency.h#L415-L464)）。拆成 256 桶之后，单次扩容从“4 GiB → 8 GiB（+4 GiB 尖刺）”变成“每次仅对触顶的那 1 张子表从 16 MiB → 32 MiB（+16 MiB 微幅波动）”，同时每张小表重回 L2/L3 缓存，最后的并行 Merge 阶段各个线程只需按桶号无锁领活合并即可！
- **DuckDB 的小表 `Abandon` + Radix 分区**：DuckDB 更进一步——在 \(> 2\) 线程并行聚合时，它干脆不让本地聚合表做大表 Rehash，把容量锁死在 32,768 槽（L2 友好），填满后直接清零目录（`Abandon()`），把已合并的局部状态留在 16\~256 个 Radix 分区里；最终 Source 阶段每个线程逐个认领一个 Radix 分区，在独立的小表里完成最终聚合与输出，全程零全局锁、零巨表 Rehash。
- **LingoDB 的零 Rehash 有损预聚合缓存（`PreAggregationHashtableFragment`）**：这是最激进也最巧妙的一招（[`PreAggregationHashtable.cpp:46-60`](https://github.com/lingo-db/lingo-db/blob/fab813e8/src/runtime/PreAggregationHashtable.cpp#L46-L60)）！每个线程只维护一张**极小、尺寸永不增长**的直映指针数组 `ht[hashtableSize]`。新键进来如果槽位冲突，**直接覆盖驱逐旧指针**，同时把新分配的状态节点追加到 `outputs[hash & 63]` 对应的 64 个分区缓冲里。结果是什么？**无论基数是 1 万还是 1 亿，局部预聚合全程 0 次 Rehash、永远死锁在 L1/L2 缓存里**；高频倾斜热点键依然能在 L1 缓存里直接拦截并就地聚合 99% 的重复行，冷键则无痛流入 64 个并行合并分区收口！

### 3.5 解法五：把 Rehash 升级为自适应数据结构状态机（Velox `HashMode`）

Velox 没有把 Rehash 仅仅当成“把数组变大”，而是把它设计成了**数据结构的动态升级状态机**（`HashTable::decideHashMode`，[`HashTable.cpp:1711-1799`](https://github.com/facebookincubator/velox/blob/303d7149/velox/exec/HashTable.cpp#L1711-L1799)）：

1. 起初，`VectorHasher` 跟踪每一列分组/连接键的极值范围（`range`）和不同值集合（`distinct`，上限 `kMaxDistinct = 100,000`，支持把 \(\le 7\) 字节短串直接编成 64 位整数），并预留 50% 增长余量（`reservePct`）减少频繁切换。
2. 当所有键的值域乘积 \(< 200\) 万（16 MiB）时，表运行在 **`HashMode::kArray`**（无哈希、无冲突、直接数组寻址）。
3. 当数据涌入导致值域乘积突破 200 万，但仍能打包进 64 位无符号整数时，在触发 `rehash()` 时无缝升级到 **`HashMode::kNormalizedKey`**——所有多列组合键被压成一个 64 位规范键（存在行前 `-8` 字节处），探测比较只需单条 64 位整数比较指令。
4. 只有连 64 位都装不下（如超长变长字符串或高基数多列组合），才最终退化到通用 **`HashMode::kHash`**，逐列回访 `RowContainer` 比较。

### 3.6 解法六：多核无锁并行建表与溢出收尾（Velox `parallelJoinBuild`）

当 Join 的 Build 侧达到千万行、需要 16 个核心并行写入同一张最终哈希表时，如果用细粒度桶锁会带来严重的缓存行弹跳（Cache Line Bouncing）。DuckDB 和 LingoDB 选择用无锁原子 CAS 往桶头挂链；而 Velox 对 128B SIMD 分组开放寻址表给出了一套极为优雅的**分段无锁并行构建协议**（[`HashTable.cpp:948-1239`](https://github.com/facebookincubator/velox/blob/303d7149/velox/exec/HashTable.cpp#L948-L1239)）：

1. 把整个哈希目录的字节空间 `[0, sizeMask + 1)` 按 128 字节 Bucket 边界切成 `P` 个互不重叠的连续区间。
2. **并行分区**：每个线程并行扫描自己的 `RowContainer`，算出每行的目标 `bucketOffset`，用 SIMD 确定它落入哪个区间号 `p`，把 1 字节区间号记在 `RowPartitions` 里。
3. **无锁并行插槽**：第 `p` 号线程独占区间 `[start_p, end_p)`，遍历所有 `RowContainer` 中标记为 `p` 的行直接写入，**全程无需任何锁或原子操作**！唯一例外是某条行在线性探测时撞到了区间右边界 `end_p`——线程绝不越界写别人的地盘，而是把这极少数跨界行连同算好的哈希值暂存进 `overflows` 与 `overflowHashes`。
4. **零重算串行收尾**：所有线程完成后，单线程把寥寥无几的 `overflows` 复用缓存哈希补插完毕。

---

## 四、面向算子的分化设计：Join、Aggregation、Window 为何长成“三种物种”？

把前面所有的底层技巧收拢起来，最值得引擎开发者反复咀嚼的问题是：**同样是哈希表，为什么放到 Join、Aggregation 和 Window 三个算子里，工程形态会分化得面目全非？**

因为决定数据结构长相的从来不是名字，而是算子的**访问契约**：

| 维度 | Hash Aggregation | Hash Join | Window / Analytic |
| --- | --- | --- | --- |
| **读写模式** | 边写边查，写密集，流式摄入即时聚合 | Build 阶段只写，Probe 阶段高频只读 | 通常需按 `PARTITION BY` 归组 + `ORDER BY` 排序 |
| **键唯一性** | 严格唯一键（1:1 对应聚合状态） | 天然存在 1:N 重复键（需挂链展开） | 同分区内多行必须全部保留且维持帧顺序 |
| **能否先知规模** | 不能（边插边压缩行数） | **能**（Build 全量缓存后精确已知行数） | 按分区切分后，单分区在排序前规模已知 |
| **Rehash 态度** | 必须正面优化（分离载荷/`mremap`/Two-Level/Abandon） | **彻底消灭（0 次 Rehash）** | 不涉及全局大表扩容，核心在控制单分区排序内存 |
| **输出方式** | 顺序扫描聚合状态 Arena，不扫稀疏哈希槽 | Probe 驱动 Gather 输出（延迟物化 / 哨兵补空） | 边界扫描 + 线段树 / 流式帧状态机 |

### 4.1 Aggregation：写密集、唯一键与状态生命周期分离

聚合算子最核心的矛盾是**热路径写入吞吐**与**最终顺序输出效率**之间的拉扯。

1. **哈希表只管索引，状态放进连续 Slab/Arena**：如果直接把几百字节的复杂聚合状态（如 `AVG`、`HLL`、`BITMAP`、`COUNT(DISTINCT)`）塞进开放寻址的 `Cell` 里，不仅会让每次探测跨越大量无用字节、严重稀释 Cache Line 利用率，在最后吐出结果时还得遍历包含大量空槽的稀疏目录。因此 StarRocks 用 `HashTableKeyAllocator` 按 1,024 槽批量分配 16 字节对齐的 `AggDataPtr`；Doris 专门引入了 `AggregateDataContainer`（每块 `SUB_CONTAINER_CAPACITY = 8192`，[`agg_utils.h:217-375`](https://github.com/apache/doris/blob/7ac0eb2f/be/src/exec/common/agg_utils.h#L217-L375)）把 `(key, agg_state)` 紧凑存在分块数组里，吐出阶段直接顺序扫描，彻底绕开稀疏目录；DataFusion 的 `GroupValues` 则更彻底——哈希表里只存密集自增的 `group_id`，分组键直接按列存成 Arrow `Vec` / `ArrowBytesMap`，输出阶段 `std::mem::take()` **零拷贝**交付给下游！
2. **单列 Nullable 外置与 `SELECT DISTINCT` 流式即时吐出**：单列分组键如果可空，StarRocks（`null_key_data`）、Doris（`DataWithNullKey`）和 DataFusion（`null_group: Option<usize>`）都会把 `NULL` 分组单独摘出来放进一个指针字段里，既避免给所有非空键增加 1 字节空值标志，又让整块无 `NULL` 的列走零判断快路径。对于没有聚合函数的 `SELECT DISTINCT`，Velox 更是利用“唯一键首次插入即可确定存在”的性质，在每次 `addInput()` 结束后直接把 `lookup.newGroups` 流式吐给下游（[`HashAggregation.cpp:434-496`](https://github.com/facebookincubator/velox/blob/303d7149/velox/exec/HashAggregation.cpp#L434-L496)），彻底打破聚合算子的全量阻塞屏障（一旦触发 Spill，再通过附加的 `emitted_` 布尔列去重防重吐）。
3. **高基数自适应退化**：如第一节所述，局部预聚合是一场以内存和 CPU 换网络带宽的赌博。StarRocks 的六态状态机（[`aggregate_streaming_sink_operator.cpp:224-357`](https://github.com/StarRocks/starrocks/blob/f977b53c/be/src/exec/pipeline/aggregate/aggregate_streaming_sink_operator.cpp#L224-L357)）把这场赌博算到了极致——命中率 \(\le 20\%\) 连续 5 个 Chunk 进入 `PASS_THROUGH` 全直通（每隔一段还会试探性 `FORCE_PREAGG` 3 个 Chunk 捕获新冒出来的热点键）；命中率落在 `20%~90%` 中间地带时进入 **`SELECTIVE_PREAGG`**（已命中的热键就地聚合进表，未命中的冷键不插表、直接流式透传给下游），既不让表膨胀爆内存，又把重尾分布里的热键压缩干净！

### 4.2 Join：零 Rehash、唯一快路径与列存/行存分歧

Join 算子的优化围绕“一次性精雕细琢 Build，换取亿级 Probe 的极致只读吞吐”展开：

1. **唯一键快路径（Unique-Key Fast Path）**：虽然 Join 必须支持 1:N 重复链，但现实数仓里大量星型模型连接是**事实表 Probe 维度表主键（N:1）**——Build 侧根本没有重复键！因此四大家全都在 Build 封板时顺手记录了“是否出现过重复键”：StarRocks 记录 `is_collision_free_and_unique`（`used_buckets == row_count`，[`join_hash_table_descriptor.h:196`](https://github.com/StarRocks/starrocks/blob/f977b53c/be/src/exec/join/join_hash_table_descriptor.h#L196)），DuckDB 维护原子标记 `chains_longer_than_one`，Velox 维护 `OneWayStatusFlag hasDuplicates_`，DataFusion 维护 `chained: bool`。只要确认无重复，Probe 热循环直接砍掉内层 `while (next != 0)` 扫链分支，甚至直接走 SIMD `gather` + `filter` 批产出。对于无残余条件的 `LEFT SEMI / ANTI` 连接，StarRocks `LinearChainedJoinHashSet` 与 DuckDB（`insert_duplicate_keys = false`）干脆在建表期就对重复键去重，直接释放掉整张 `next[]` 数组。
2. **Build 载荷的列存直引 vs 行存物化**：
   - **列存直引派（StarRocks / Doris / DataFusion）**：Build 侧数据保持原生的列式 `Chunk` / `RecordBatch`，哈希表只存 `uint32_t` 行号。好处是 Build 阶段**零行列转置拷贝**，且 StarRocks/Doris 在第 0 行塞入默认/NULL 哨兵行，让 `LEFT OUTER` 未命中行直接生成 `build_idx = 0` 走统一向量化 Gather（配合延迟物化，只 Gather 最终通过残余谓词的列）。代价是 Probe 侧校验多列键时，每列分布在独立内存数组里，随机访问会多消耗几条 Cache Line。
   - **行存物化派（DuckDB / Velox）**：Build 阶段把连接键、载荷列、状态位打包进连续的行式 `TupleDataCollection` / `RowContainer`。虽然 Build 多了一遍行列转置开销，但 Probe 命中解引用时，**一次内存读取就能把多列键和常用载荷整行拉进同一条 Cache Line**，而且当触发内存 Spill 时，整行直接按 Radix 分区连续写盘，无需像列存那样重新拆列重组。DuckDB 还对小 Build 表（载荷 \(\le 8\text{ MiB}\) 且 Probe/Build \(\ge 100\times\)）做了**词典直出优化**（[`join_hashtable.cpp:2769-2903`](https://github.com/duckdb/duckdb/blob/2bb5316a/src/execution/join_hashtable.cpp#L2769-L2903)），Probe 根本不拷贝 Build 载荷列，直接把行号包装成 Arrow/DuckDB 词典向量向上游传递。

### 4.3 Window：为什么窗口算子大多不用通用 Hash Table，但 Hash 藏在三条关键缝隙里？

很多工程师会直觉地问：既然 `GROUP BY a` 用 Hash Aggregation，那 `SUM(x) OVER (PARTITION BY a ORDER BY b ROWS BETWEEN 3 PRECEDING AND CURRENT ROW)` 能不能也用 Hash Table？

回到第一性原理看，答案非常清晰：**窗口函数的核心语义是“分区内有序滑动帧”，而通用哈希表天生是无序的。** 哪怕你先花一份内存把所有行按 `a` 插进哈希表归堆，落到每个分区之后，你依然逃不掉按 `b` 对分区内的行做排序（或者构造基于有序位置的线段树）来界定每一行的滑动窗口边界——哈希表不仅没省掉排序，反而平白多付了一遍建表内存与随机探测代价。

正因如此，翻遍 StarRocks `Analytor`、Doris `AnalyticSinkOperator`、Velox `SortWindowBuild`、DuckDB `PhysicalWindow` 和 DataFusion `WindowAggExec`，窗口算子的主干路径无一例外都是**分区 + 排序 + 连续边界扫描**（例如 StarRocks 和 Doris 在已序数据块上用二分搜索 `find_first_not_equal` 找分区与 Peer Group 边界，且当 `is_high_cardinality()` 检测到平均分区长度 `< 8` 时自动切回线性扫描防止二分退化；DuckDB 用 16 叉 `WindowSegmentTree` 把任意帧聚合压到 \(O(\log_{16} N)\)）。

甚至连最像该用哈希表的 **`COUNT(DISTINCT x) OVER (...)`**，DuckDB 的 `WindowDistinctAggregator`（[`window_distinct_aggregator.cpp:41-167`](https://github.com/duckdb/duckdb/blob/2bb5316a/src/function/window/window_distinct_aggregator.cpp#L41-L167)）在源码里留了一句耐人寻味的注释 `// TODO: Use a hash table?` 后，依然放弃了逐帧建哈希表——因为 100 万行滑动窗口如果每行重建或维护一套哈希集合，复杂度会恶化到 \(O(N \cdot W)\)。DuckDB 最终采用排序求每个元素上一次出现位置 `prev_idx` + 构造 `MergeSortTree` 的经典算法，把任意滑动帧的 `DISTINCT` 聚合压到了 \(O(\log^2 N)\)，只有最冷门的兜底路径 `WindowNaiveAggregator` 才逐帧临时使用 `std::unordered_set`。

但这绝不意味着哈希数据结构在 Window 算子里无用武之地——相反，在源码深处，**Hash 结构精准卡在了窗口算子的三条关键缝隙里**：

1. **缝隙一：用哈希切分子分区，把全局大排序拆小、把峰值内存压低（Velox & DuckDB）**
   - Velox 的 `SubPartitionedSortWindowBuild`（[`SubPartitionedSortWindowBuild.cpp:47-114`](https://github.com/facebookincubator/velox/blob/303d7149/velox/exec/window/SubPartitionedSortWindowBuild.cpp#L47-L114)）通过 `HashPartitionFunction`（底层复用 `VectorHasher`）按 `PARTITION BY` 键将输入行散列路由进 `numSubPartitions` 个独立的 `SortWindowBuild`。输入结束后，**一次只排序、消费并释放一个子分区**，既降低了 \(O(N \log N)\) 的对数项，又把排序峰值内存切小了 \(P\) 倍。
   - DuckDB 的 `HashedSort`（[`hashed_sort.cpp:105-152, 569-572`](https://github.com/duckdb/duckdb/blob/2bb5316a/src/common/sort/hashed_sort.cpp#L105-L152)）同样用 `RadixPartitionedTupleData` 动态把输入散列到 16\~256 个哈希桶里再各桶并行排序；更精妙的是它搭配了 **`PartitionKeyTracker`**，追踪每个 Radix 桶里出现过的不同分区键个数——如果窗口没有 `ORDER BY` 且某个 Radix 桶里恰好只有一个唯一分区键（`SINGLE_KEY`），`CanBypassSort(hash_bin)` 会**直接跳过排序**，把该桶零排序送入窗口求值！
2. **缝隙二：没有 `ORDER BY` 的窗口函数与 `PartitionTopN`，直接用哈希归组消灭排序（StarRocks）**
   - 当窗口表达式只有 `PARTITION BY` 而没有 `ORDER BY`（例如 `SUM(amount) OVER (PARTITION BY dept_id)`）时，分区内部根本不需要排序，只需要把相同 `dept_id` 的行凑成连续的 Chunk 块。StarRocks 在 `AnalyticNode::decompose_to_pipeline`（[`analytic_node.cpp:48-50, 96-118`](https://github.com/StarRocks/starrocks/blob/f977b53c/be/src/exec/analytic_node.cpp#L48-L50)）检测到该模式且开启 `use_hash_based_partition` 时，直接砍掉 Sort 算子，改插 `HashPartitionSink/Source`——底层用 **`PartitionHashMap`**（`phmap::flat_hash_map<Key, PartitionChunks*>`，[`partition_hash_map.h:57-85`](https://github.com/StarRocks/starrocks/blob/f977b53c/be/src/exec/partition/partition_hash_map.h#L57-L85)）在 \(O(N)\) 时间内把同分区行收拢成连续块喂给 `Analytor`。同一套 `PartitionHashMap` 还支撑了 `ROW_NUMBER()/RANK() <= K` 的 `LocalPartitionTopnContext` 下推。
3. **缝隙三：数据已按 `ORDER BY` 有序但未按 `PARTITION BY` 排序时的流式哈希窗口（DataFusion）**
   - 试想一张按时间戳 `ts` 天然有序的事件日志表，查询是 `SUM(v) OVER (PARTITION BY user_id ORDER BY ts ROWS BETWEEN ...)`。传统引擎必须为了 `PARTITION BY user_id` 把整张按 `ts` 排好序的表推倒重排；而 DataFusion 的 `BoundedWindowAggExec`（`InputOrderMode::Linear / PartiallySorted`，[`bounded_window_agg_exec.rs:717-920, 1182-1210`](https://github.com/apache/datafusion/blob/a4b8198c/datafusion/physical-plan/src/windows/bounded_window_agg_exec.rs#L717-L920)）反其道而行之：用保序哈希表 `IndexMap<PartitionKey, WindowState>` 为每个活跃用户维护独立的滑动窗口状态，并用两张 `hashbrown::HashTable`（`row_map_batch` 与 `row_map_out`）在每个输入 Batch 内把乱序交错的各用户行快速归堆、评估、再按原输入行序交错拼回输出——完全免去了全局重排序，还能借助单调递增的 `ts` 水位实时淘汰已完结分区！

---

## 五、给执行引擎工程师的一份 Hash Table 选型与调优 Checklist

把上面跨六套引擎的底层机制翻译成日常开发和线上排障可直接落地的判断依据，可以收束为五条原则：

1. **先看规模和值域，再决定要不要“哈希”**：单整数/短组合键且值域可控（如 \(< 64\text{K}\) 或满足高密度条件），直接走数组寻址或位图压缩；别让微型维度表白白承担哈希计算和探测开销，但务必像 Velox 那样加上**稀疏填充率防线**，防止两头极值把数组撑成空壳。
2. **目录设计先抠字节与指纹，后谈算法**：在现代 CPU 上，把单槽压进 8 字节（Velox 128B SIMD 桶、DuckDB 48+16 位 Salt 指针）或 4 字节（StarRocks 8+24 位混合槽），收益远大于纠结哈希函数本身快 1 个时钟周期。负载因子目标应定在 **70%\~87.5%**（借助 SIMD Tag 或高 16 位 Salt 挡掉 99%+ 的假阳性解引用），并用正交位切片或双阶段换种彻底封死上游 Shuffle 带来的低位碰撞风暴。
3. **跨过 L2/L3 拐点时，主动切换策略而非硬扛**：
   - 表小于 L2（\~256 KiB\~1 MiB）时，关掉软件预取与多余指纹校验；
   - 表跨过 L2 时，打开批量哈希预计算 + 固定步长/三阶段软件预取；
   - 聚合表跨过 L3（或 10 万级唯一键）时，果断切入 **Two-Level（16\~256 子表）** 或 **Radix 小表 `Abandon`**；
   - Join 探测表远超 L3 且存在重复链时，采用交错扫链或无栈协程隐藏 DRAM 延迟。
4. **能消灭 Rehash 绝不优化 Rehash，必须 Rehash 时锁死峰值与搬运量**：Join 坚持两阶段精确建表（0 次 Rehash）；Aggregation 坚持“目录与状态容器分离 + 缓存哈希/规范键 + 已知唯一免 Key 比较”；若必须原地扩扁平表，优先结合 Linux `mremap` + 2 的幂原地重插（切记补扫回绕尾链）；高基数局部预聚合必须配备实时压缩率监测（如 HLL 或滑动命中率状态机）及时旁路止损。
5. **线上 Profile 诊断速查**：
   - 若 `ResizeTime` / `RehashCount` 占比高且伴随内存尖刺：检查是否未开启 Two-Level 阈值（或阈值过高）、Join 是否误用了流式扩容表、以及初始容量是否过小导致前期频繁倍增；
   - 若 `ProbeTime` 高但指令数（Instructions）并不高、IPC 跌到 `< 0.5`：典型 DRAM + TLB Miss 瓶颈，优先核查负载因子是否过低导致目录膨胀、是否缺失批预取、是否开启透明大页（THP / HugePage）以缓解 STLB 抖动，以及是否触发了唯一键快路径；
   - 若聚合阶段 `HashTableBuildTime` 极高但输入输出行数几乎 1:1：本地预聚合正在白干活，检查并收紧 Partial Aggregation Abandon / Bypass 阈值；
   - 若 Two-Level / Radix 并行归并出现单核长尾：大概率是单个超级热点键倾斜（换哈希种子无解，需依赖上层倾斜打散或两阶段随机加盐聚合）。

---

## 六、源码基线与参考索引

本文所有数据结构布局、阈值常量与控制流均对照以下开源仓库核验：

| 项目 | 核验基线（短 Commit） | 核心检视源码入口 |
| --- | --- | --- |
| **StarRocks** | `f977b53c` | `be/src/base/container/fixed_hash_map.h`、`be/src/base/phmap/phmap.h`、`be/src/exec/aggregate/agg_hash_map.h`、`be/src/exec/pipeline/aggregate/aggregate_streaming_sink_operator.cpp`、`be/src/exec/join/join_hash_table.cpp`、`be/src/exec/join/join_hash_map_method.h`、`be/src/exec/join/join_hash_map.hpp`、`be/src/exec/analytic_node.cpp`、`be/src/exec/partition/partition_hash_map.h` |
| **Meta Velox** | `303d7149` | `velox/exec/HashTable.h`、`velox/exec/HashTable.cpp`、`velox/exec/VectorHasher.h`、`velox/exec/RowContainer.cpp`、`velox/exec/HashAggregation.cpp`、`velox/exec/GroupingSet.cpp`、`velox/exec/window/SubPartitionedSortWindowBuild.cpp` |
| **DuckDB** | `2bb5316a` | `src/include/duckdb/execution/ht_entry.hpp`、`src/include/duckdb/common/radix_partitioning.hpp`、`src/execution/aggregate_hashtable.cpp`、`src/execution/perfect_aggregate_hashtable.cpp`、`src/execution/radix_partitioned_hashtable.cpp`、`src/execution/join_hashtable.cpp`、`src/execution/operator/join/perfect_hash_join_executor.cpp`、`src/common/sort/hashed_sort.cpp`、`src/function/window/window_distinct_aggregator.cpp` |
| **Apache Doris**（含 ClickHouse 哈希表血统） | `7ac0eb2f` | `be/src/exec/common/hash_table/hash_table.h`（源自 ClickHouse `HashTable.h`）、`be/src/exec/common/hash_table/string_hash_table.h`、`be/src/exec/common/hash_table/ph_hash_map.h`、`be/src/exec/common/hash_table/join_hash_table.h`、`be/src/exec/pipeline/dependency.h`、`be/src/exec/operator/analytic_sink_operator.cpp` |
| **Apache DataFusion** | `a4b8198c` | `datafusion/physical-plan/src/aggregates/group_values/mod.rs`、`single_group_by/primitive.rs`、`multi_group_by/mod.rs`、`joins/join_hash_map.rs`、`joins/array_map.rs`、`windows/bounded_window_agg_exec.rs` |
| **LingoDB** | `fab813e8` | `src/runtime/PreAggregationHashtable.cpp`、`src/runtime/LazyJoinHashtable.cpp`、`include/lingodb/runtime/helpers.h` |
| **ClickHouse**（上游设计对照） | 公开上游与 Doris 继承源码对照 | `src/Common/HashTable/HashTable.h`、`TwoLevelHashTable.h`、`StringHashTable.h`、`FixedHashTable.h`、`src/Common/Allocator.h`（`MMAP_THRESHOLD` / `mremap`） |
