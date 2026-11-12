---
title: "【原理】Column Encoding：从信息表示到 Encoding-aware Execution"
date: 2026-08-25T00:00:00+08:00
lastmod: 2026-10-07T00:00:00+08:00
slug: "dive-encoding"
categories:
  - 数据库
  - 大数据
tags:
  - Column Encoding
  - Compression
  - Parquet
  - Apache Arrow
  - Velox
  - DuckDB
description: "从信息冗余与 CPU 内存带宽墙出发，拆解 Dictionary、RLE/REE、Delta-BitPacking、FSST、ALP 与 Roaring Bitmap 的物理机制，剖析编码感知执行、延迟物化与跨算子物理属性代数。"
draft: false
toc: true
math: true
---

1948 年，Claude Shannon 在《[A Mathematical Theory of Communication](https://doi.org/10.1002/j.1538-7305.1948.tb00917.x)》中做了一次影响深远的剥离：把消息的“业务意义”与承载消息的“物理编码”彻底分开，用信息熵刻画统计冗余允许我们把表示压到多短。七十多年后，同样的张力每天都在现代分析型数据库（OLAP）的 CPU 流水线里重演——只不过这一次，瓶颈早已不只是磁盘容量，而是悬殊的**内存带宽墙与算子解压税**。

如果我们用最朴素的“先解压、再计算”方式跑一条最常见的电商看板查询，会发生什么？设想扫描一张 100 万行的行为明细表：

```sql
SELECT country, sum(amount)
FROM events
WHERE event_type = 'purchase'
GROUP BY country;
```

其中 `event_type` 只有 8 种枚举字符串（`'purchase'` 仅占 4%），`country` 只有 200 个国家代码。在磁盘上的 Parquet 文件里，这两列早已被字典编码（Dictionary Encoding）加位打包（Bit-Packing）压成了不到 1.5 MiB 的紧凑整数流。可一旦交给传统的朴素读取器：

1. **Scan 出口立刻内存爆炸**：读取器老老实实把 100 万个 `event_type` 字典编号和 100 万个 `country` 字典编号全部还原成 UTF-8 变长字符串切片（`16B StringView + 字符缓冲区`），原本塞得进 L3 缓存的 1.5 MiB 码流瞬间膨胀到三十多兆字节，一路击穿末级缓存，把 DRAM 内存通道塞得水泄不通。
2. **96% 的解码纯属无用功**：紧接着 `WHERE event_type = 'purchase'` 逐行跑了 100 万次字节比较（`memcmp`），筛完只剩 4 万行——也就是说，前面为 `country` 和 `amount` 提前解压、分配内存的那 96 万行数据，连下游聚合算子的面都没见着就被原地丢弃了。
3. **聚合阶段二次交税**：幸存下来的 4 万个 `'US'`、`'CN'` 字符串进入 `GROUP BY`，算子又得对变长字节重算一遍字符串哈希、探测哈希表、在 Arena 里拷贝字符串键。事实上，只要保留那张只有 200 项的字典，分组聚合根本连哈希表都不用建，开一个长度 200 的定长数组按整数编号直接累加 `sum[code] += amount` 就结束了！

在这条朴素链路上，**超过 80% 的 CPU 指令周期与内存带宽都被消耗在了“把紧凑结构拆散成冗余字节，再从冗余字节里费力找回结构”的折返跑上**。Apache Arrow 团队在[毫秒级查询 Parquet 的实测报告](https://arrow.apache.org/blog/2022/12/26/querying-parquet-with-millisecond-latency/)中曾展示过这笔账的真实量级：仅仅在 Parquet → Arrow 读取边界保住 `DictionaryArray` 而不提前展开成 UTF-8 字符串，端到端吞吐就能拉出超过 60 倍的差距。

这也揭示了现代查询引擎看待列式编码（Column Encoding）的第一性原理：数据库追求的从来不是单一的 `min(磁盘字节数)`，而是端到端总代价的全局最小化：

$$
C_{\text{total}} = C_{\text{io}} + C_{\text{decode}} + C_{\text{mem\_bw}} + C_{\text{operator}} + C_{\text{shuffle\_serde}} + C_{\text{rewrite}}
$$

下文顺着这条成本方程，从语义编码与黑盒压缩的分界出发，逐一拆解六大核心编码家族的物理构造，再深入编码感知执行（Encoding-Aware Execution）、跨算子转换边界与十余套主流工业系统的架构取舍。

---

## 一、语义编码 vs 字节流压缩：为什么高压缩比未必跑得快？

在存储引擎的代码里，**列式编码（Column Encoding）**与**通用页压缩（Page Compression）**虽然前后串联，扮演的却是截然不同的角色。以标准 Parquet 或 Apache Doris 的写入管线为例，一列逻辑数据落盘前会依次穿过两道工序：先按列值分布应用 Dictionary、RLE、Delta 或 Byte Stream Split 等**语义编码**，再把编码产出的字节缓冲区交给 LZ4、Snappy 或 ZSTD 做**通用字节块压缩**。

两者的根本分界在于**数据结构对计算引擎是否透明**：

- **通用压缩（LZ4 / ZSTD / Gzip）是字节级黑盒**：它基于滑动窗口重复串（LZ77）与熵编码（Huffman / ANS）消除任意字节流冗余，对上层算子完全不暴露元素边界。哪怕查询只想读页面里的第 37 行，或者判断哪个值大于 `100`，也必须先把整块压缩页全量解压回连续内存。在云原生数据湖场景下，随着 NVMe SSD 带宽突破数 GB/s、对象存储并发吞吐水涨船高，SIGMOD 2023 的 [BtrBlocks](https://www.cs.cit.tum.de/fileadmin/w00cfj/dis/papers/btrblocks.pdf) 实测指出，重度通用解压极易反过来把原本 I/O 受限的扫描顶成 **CPU 解压瓶颈**。
- **列式编码（Dictionary / RLE / FOR / FSST / ALP）是代数同态映射**：它利用同一列相邻元素同类型、同分布的统计规律，把宽重或变长的逻辑域映射成**定长、稠密、保结构**的整数或位流域。只要映射满足等值保持（`a == b <=> code(a) == code(b)`）或序保持（`a < b <=> code(a) < code(b)`），过滤、哈希、分组乃至连接算子就能直接下沉到编码域里闭环运算，甚至把单次操作复杂度从 `O(行数)` 直接降维到 `O(唯一值数)` 或 `O(游程数)`。

这一理念最早由 C-Store（VLDB 2005）、MonetDB/X100（CIDR 2005）以及 Abadi 等人在 SIGMOD 2006 发表的经典论文 [《Integrating Compression and Execution in Column-Oriented Database Systems》](https://www.cs.umd.edu/~abadi/papers/abadisigmod06.pdf) 奠定地基：列存的真正红利绝不只是“少读没用到的列”，而是**物理排序（Sort Key / Clustering）、轻量语义编码与 SIMD 向量化批处理三者的乘积效应**。同一列 `country` 如果随机混排写入，`['US', 'CN', 'JP', 'US', 'CN']` 只能靠字典把字符串压成整数；一旦表按 `(country, event_date)` 聚簇排序，物理存储瞬间变成连续十万个 `'CN'` 紧接二十万个 `'US'`，此时叠加游程编码（RLE），几十万行数据在算子眼里就坍缩成了两条轻量记录。

---

## 二、六大列编码家族：数据分布如何决定物理表示？

没有任何一种编码能通吃所有数据分布。成熟的存储与执行内核会根据**基数（NDV）、连续重复度、数值跨度、子串相似性与稀疏度**，将列数据精准路由到对应的轻量编码结构中：

![从数据分布冗余特征到列式编码及编码域执行原语的映射谱系](/images/diagrams/aug-exec-encoding-taxonomy.svg)

### 2.1 常量与游程家族：Constant、RLE 与 Arrow Run-End Encoding（REE）

当一个微分区（Micro-partition）或数据页内的所有值完全相同时（例如按天分区的 `dt = '2026-08-25'` 或刚新增的默认列），**Constant Encoding** 只存 `1` 个字面值加总行数 `N`（对应 DuckDB 的 Constant 压缩段与 Velox 的 `ConstantVector`）。任何作用在常量向量上的确定性谓词或表达式只需在单值上算一次，结果为真则整批 4,096 行全量放行，结果为假则整批瞬间短路清零。

当数据按维度列排好序、呈现出大段连续重复（如 `['A', 'A', 'A', 'A', 'B', 'B', 'B', 'C', 'C']`）时，传统的 **RLE（Run-Length Encoding）** 将其记作 `[(A, 4), (B, 3), (C, 2)]`。Parquet 用 RLE / Bit-Packing 混合编码紧凑存储字典索引与嵌套层级，ORC RLEv2 则进一步细分出定长、直接、打补丁（Patched Base）与差分子模式。不过传统存“长度”的 RLE 有一个随机访问痛点：想查第 `k` 行是什么值，必须从头累加前缀和。为此，[Apache Arrow 的 Run-End Encoding（REE）](https://arrow.apache.org/docs/format/Columnar.html#run-end-encoded-layout) 改存累积结束下标 `values = [A, B, C], run_ends = [4, 7, 9]`——既保住了按段聚合（`count[A] += 4`）的 `O(runs)` 优势，又能直接对 `run_ends` 数组做二分查找实现 `O(log(runs))` 的点查定位。

### 2.2 字典编码（Dictionary / `LowCardinality`）：变长域向整数域的降维桥

面对无序但低基数的字符串或枚举列（国家、设备型号、HTTP 状态码），**Dictionary Encoding** 抽出唯一值构建字典表 `0 -> "click", 1 -> "view", 2 -> "buy"`，将原列替换为紧凑整数数组 `[0, 1, 0, 2]`。Parquet 默认优先为列块（Column Chunk）构建字典页，一旦唯一值撑破阈值再自动回退 `PLAIN`；ClickHouse 则将其提炼成显式类型 `LowCardinality(T)`（经验上唯一值低于万级时收益极佳）；Velox 与 Trino 的 `DictionaryVector` / `DictionaryBlock` 更把它升格为内存里的通用切片容器。

字典编码真正迷人的地方在于它同时打通了存储与计算：不仅磁盘与内存体积骤降，字符串判等 `strcmp(s, "buy")` 变成了单指令整数比较 `code == 2`，分组与连接哈希从逐字节滚动计算变成了定长整数寻址。唯一的暗礁是**字典作用域（Dictionary Locality）**——如果文件 A 里 `0` 代表 `"US"`，文件 B 里 `0` 代表 `"CN"`，跨文件算子就绝不能直接拿局部 `code` 判等，这也是下文要重点剖析的转换边界。

### 2.3 差分、基准帧与位打包：Delta + FOR + SIMD Bit-Packing

遇到高基数的单调时间戳、自增主键或局部波动很小的整数指标时，字典会因唯一值过多而失效，但数值本身的**高位比特几乎全是重复的**。

以一组毫秒时间戳 `[1700001001, 1700001002, 1700001004, 1700001005]` 为例：
- **基准帧（Frame of Reference, FOR）** 先扣除块内最小值 `base = 1700001000`，将 64 位大整数变成极小的非负偏移 `[1, 2, 4, 5]`；若是等间隔递增序列，**Delta / DoubleDelta**（如 `[1000, 1010, 1020, 1030]` 一阶差分全为 `10`，二阶差分全为 `0`）压得更狠；偶发出现一两个离群极大值时，**PFOR（Patched FOR）** 将异常值单独打补丁存放，防止一颗老鼠屎撑大整块的位宽。
- 紧接着 **Bit-Packing** 发现偏移最大值不超过 `7`，于是抛弃 64 位字长，每个数只用 `3 bits` 紧挨着塞进内存！原本一个 64 字节 CPU 缓存行（Cache Line）只能装 8 个 `int64`，现在足足能塞进 **170 个逻辑整数**，有效内存带宽凭空放大了 20 倍以上。

更进一步，[BitWeaving](https://pages.cs.wisc.edu/~jignesh/publ/bitweaving.pdf)、ByteSlice 与近年大热的 [FastLanes](https://github.com/cwida/FastLanes) 重新编排了位打包在寄存器内的交错顺序（Transposed Layout），使单条 AVX-512 指令无需串行移位就能一口气并行解包或直接完成范围谓词比较（每列值耗时低于 0.5 个 CPU 周期）。这就解释了 DuckDB 官方文档里那个反直觉的现象：**开启轻量压缩的落盘表，全表扫描往往比完全未压缩的纯内存表跑得还快**——省下的 DRAM 带宽等待时间，远远盖过了寄存器内那几纳秒的 SIMD 解包指令。

### 2.4 高基数字符串破局：FSST 与前缀差分编码

如果遇到 URL、日志路径、JSON 片段或 UUID 这类基数高达几千万的字符串列，传统字典彻底失灵，而直接上 LZ4 块压缩又会丧失单行随机访问能力。

PVLDB 2020 提出的 **[FSST（Fast Static Symbol Table）](https://vldb.org/pvldb/vol13/p2649-boncz.pdf)**（已被 DuckDB 与 CedarDB 纳入核心存储）给出了极其精巧的解法：它在数据块上采样统计最高频的 `1 ~ 8` 字节短子串（例如 `"https://"`、`".com/api"`、`"status":`），构建一张最多 255 项的静态符号表，把命中的多字节子串全部替换成 1 字节单码（保留 `0xFF` 作为未命中原字符的转义前缀）。因为符号表固定只有约 2 KiB、常驻 L1 缓存，解码时无需回溯滑动窗口，每次查表取出一个 8 字节整数写入目标缓冲再按实际长度推进指针即可；更重要的是，**任意单条字符串都能独立解压，完全不依赖前后相邻行**，完美契合延迟物化按行号随机拉取的需求。而若高基数字符串本身已按字典序排好（如排序主键上的文件路径），Parquet 的 `DELTA_BYTE_ARRAY`（前缀压缩 Front Coding）通过复用相邻字符串的公共前缀长度，同样能大幅砍掉重复字节。

### 2.5 业务浮点与稀疏位集：Gorilla / Chimp / ALP 与 Roaring Bitmap

长期以来，IEEE-754 双精度浮点数（`double`）都是列式压缩的硬骨头。像 `19.99`、`20.05` 这样在十进制下极其规整的业务金额，转换成二进制科学计数法后尾数末尾全是无限循环截断比特，直接套整数差分毫无规律。时序数据库早期普遍采用 Facebook Gorilla 或 EDBT/PVLDB 后续改进的 **[Chimp](https://www.vldb.org/pvldb/vol15/p3058-liakos.pdf) / Patas**，对相邻浮点数做按位异或（`XOR`），只编码非零有效尾数区间。而 SIGMOD 2024 的 **[ALP（Adaptive Lossless floating-Point Compression）](https://github.com/cwida/ALP)** 戳破了窗户纸：既然真实世界里绝大多数浮点数原本就是十进制小数，只需在块内自适应探测最佳的 \(10^e\) 缩放因子将其无损乘回精确整数（极少数无法精确还原的二进制浮点走异常补丁通道），瞬间就能无缝复用前面超高速的 FOR + FastLanes SIMD 位打包管线！

与数值列并肩作战的还有贯通索引与执行层的 **[Roaring Bitmap](https://roaringbitmap.org/)**。无论是扫描过滤产出的存活行号集合（Selection Set）、倒排索引行号表还是稀疏空值掩码，纯位图在极稀疏时浪费空间，纯整数数组在稠密时又慢又占内存。Roaring 将 32 位整数按高 16 位分桶（每桶覆盖 `65,536` 个值），在桶内根据基数动态切换三种物理容器：少于 4,096 个元素用有序 `uint16 Array`，稠密区间用固定 8 KiB 的 `64-bit Bitmap`，连续大段命中则用 `Run` 游程容器。多条件 `AND / OR` 过滤直接退化为两个容器在 AVX2 寄存器里的位交并运算，算完再将存活行号精准喂给下游列读取器。

### 2.6 半结构化嵌套列：Dremel Definition / Repetition Levels

面对日志与湖仓中遍地开花的 `STRUCT`、`ARRAY` 与 `MAP`，Parquet 与 Google BigQuery（Capacitor）沿用了 Dremel 的经典拆解：把嵌套树拍平成独立的叶子物理列，每列附带紧凑 RLE 编码的 **Definition Level**（路径上第几层开始出现 `NULL`）与 **Repetition Level**（当前值是在哪一层重复数组上新开的元素）。这样查询 `SELECT user.profile.city FROM logs` 时，引擎只需扫描 `city` 这一列叶子数据外加两个微型整数层级流，就能在完全不触碰旁边几百个兄弟 JSON 字段的前提下，毫秒级向量化重建出完整的嵌套结构。

把上述机制汇总成日常存储选型速查表，不同数据特征与查询模式的最优解便清晰浮现：

| 业务列典型场景 | 核心统计特征 | 首选物理编码组合 | 对应的执行期加速原语 |
| --- | --- | --- | --- |
| **分区键 / 默认填充列** | 段内全部值恒等（`NDV = 1`） | `Constant` | 谓词单次求值，整批短路放行或裁剪 |
| **聚簇排序后的维度列** | 大段连续相同值（高平均游程） | `RLE` / Arrow `REE` | `O(runs)` 分段过滤、`count/sum` 按游程长度加权累加 |
| **无序低基数字符串/枚举** | `NDV < 10^4 ~ 10^5`，全局或块内重复 | `Dictionary` / `LowCardinality` | 谓词改写为整数比对，按整数 Code 做 Hash / GroupBy / Join |
| **时间戳 / 自增 ID / 窄整数** | 单调递增或局部 `max - min` 窄 | `Delta / DoubleDelta` + `FOR` + `Bit-Pack` | 单缓存行高密装载，FastLanes / AVX2 并行解包与比较 |
| **URL / 日志 / 高基数短文本** | `NDV` 极高，但共享大量高频短子串 | `FSST`（无序）/ `Delta Byte Array`（有序） | 保持单行随机解压能力，配合 StringView 仅还原存活行 |
| **金融金额 / 监控时序浮点** | 十进制小数精度固定，或相邻读数平缓 | `ALP`（通用十进制浮点）/ `Chimp`（时序） | 无损转回窄位宽整数后走 SIMD 向量化流水线 |
| **多条件过滤行号集 / 稀疏位** | 局部稀疏与局部稠密交替混合 | `Roaring Bitmap` + Validity RLE | 容器级无分支位交并，直接驱动延迟物化按需跳读 |

---

## 三、编码感知执行（Encoding-Aware Execution）：把计算留在压缩域

看懂了单列怎么压，真正的重头戏在于执行引擎怎么算。回到文章开头那条 100 万行的过滤聚合查询，把传统“先全量解码再逐行计算”与现代向量化引擎的“编码域直算 + 延迟物化”并排对比，就能看清每一处性能红利从何而来：

![先解压后计算 vs 编码域直算：过滤与分组聚合管线对比](/images/diagrams/aug-exec-encoding-simd-pushdown.svg)

在下半区的编码感知管线中，执行器靠四项环环相扣的技术把无谓的数据搬运压到了理论下限：

1. **编码域谓词改写（Predicate Rewriting on Codes）**：面对 `event_type = 'purchase'` 或 `country IN ('US', 'CN', 'JP')`，算子绝不解开 100 万行数据，而是先去扫那张只有几百到几千项的字典表，把字符串条件翻译成整数 `code == 3` 或 `code IN (2, 5, 7)`（若字典本身按字节序构建，连 `country BETWEEN 'A' AND 'M'` 都能直接翻译成整数区间 `code BETWEEN low AND high`）。随后一条 SIMD 比较指令扫过紧凑位打包码流，几微秒就能吐出命中的行号位图。
2. **延迟物化（Late Materialization / `LazyVector`）**：既然 96% 的行都会死在 `event_type` 过滤上，扫描器在读取 `country` 和 `amount` 时压根不触碰磁盘页或解压逻辑，只向上游交出一个包裹着数据源句柄的 `LazyVector`。直到过滤算子把 4% 的存活行号集合敲定，`LazyVector` 才按行号精准跳过去跳过整段无用页，仅拉取并还原那 4 万行有效载荷。
3. **跨算子字典与游程传播（Dictionary & RLE Propagation）**：拉取出来的 `country` 依然保持 `DictionaryVector` 形态进入 `HashAggregate`。聚合算子看到输入是单字典包裹的整数码流，直接跳过字符串哈希，按整数 `code` 建桶累加；如果上游是 RLE/REE 编码的连续重复块（如 `'CN'` 连续重复 50,000 次），聚合器更是一步跨过五万行，直接执行 `sum[CN] += 50000 * val`。一直等到整个查询即将把最终那 200 行汇总结果发给客户端前，Sink 算子才查一次字典把整数编号翻译回可读的国家名称。
4. **`DecodedVector` 统一抽象避免算子代码爆炸**：如果引擎里有 8 种编码、15 种数据类型、100 个内置函数，两两手写特化会产生上千套代码分支。Meta Velox 为此设计了精妙的 [`DecodedVector`](https://facebookincubator.github.io/velox/develop/vectors.html) 视图：对上层通用表达式暴露统一的 `baseVector + indices[] + nulls` 逻辑平坦接口——无论底层进来的是 `FlatVector`、`ConstantVector`（所有下标指向槽位 `0`）还是多层嵌套的 `DictionaryVector`（自动折叠索引映射链），在无需深拷贝一个字节的前提下就能复用同一套算子代码；同时只为最高频的等值过滤、哈希探测和简单聚合单独开辟极速特化通道，优雅平衡了极致性能与内核维护成本。

---

## 四、编码生命周期与转换边界：什么时候必须果断解码？

既然保持编码态这么快，是不是所有向量都应该一路捂着编码直到查询结束？绝非如此！在实际的复杂执行计划里，**编码既是加速器，也是一份带严格作用域的契约**——甚至很多时候，编码本身还是算子在运行期动态制造出来的“零拷贝中间表示”。

![编码向量跨算子生命周期：零拷贝传播、重映射与物化边界](/images/diagrams/aug-exec-encoding-transcode-boundary.svg)

沿着上图的状态流转，我们需要看清“零拷贝升维复用”与“果断解码止损”的两面：

一方面，**`DictionaryVector` 早已超越存储格式，成为内存里的零拷贝关系代数原语**。当 `Filter` 算子从 4,096 行宽表里筛出 300 行时，它根本不用把几十列宽字符串搬到新缓冲区，只需给原向量套上一层 `indices = [2, 19, 45, ...]` 的 `DictionaryVector` 外壳；当 `Hash Join` 一行左表匹配到 5 行右表时，同样只需在输出端生成重复下标 `[0, 0, 0, 0, 0]` 指向原行；最经典的莫过于 [Trino 对 `UNNEST` 数组展开算子的改造](https://trino.io/blog/2019/08/23/unnest-operator-performance-enhancements.html)——过去把包含十个元素的数组行展开成十行，必须把同行伴随的所有标量列深拷贝十遍；改用 `DictionaryBlock` 只生成重复索引指向原底表后，线上实测最高获得了约 **9 倍延迟提速与 13 倍 CPU 削减**。

另一方面，一旦撞上下列三条物理或语义边界，死守编码不仅算不对结果，还会引发严重的间接寻址倒挂：

1. **跨文件局部字典冲突（Local vs Global Scope）**：Parquet 的字典默认局部于单个 Row Group 的 Column Chunk。文件 A 里 `"US" -> 0, "CN" -> 1`，文件 B 里 `"CN" -> 0, "US" -> 1`。当两个文件汇入同一个 `Hash Join` 或跨节点 `Shuffle Exchange` 时，直接比较整数 `0 == 0` 会把美国和中国当成同一个国家连起来！此时引擎只有三条出路：要么像 StarRocks 全局低基数字典优化那样，在元数据或扫描期统一维护**表级/分区级全局字典（Global Dictionary）**，让整条分布式流水线直接拿全局 `int32` 做 Shuffle 与 Join；要么在算子入口插入轻量级的字典归一化算子（`RemapDictionary`），把局部编号翻译成统一编号；倘若拼接后发现唯一值暴涨到百万级、重映射表比原数据还贵，就应当立刻展开（Flatten）回原始字符串。
2. **多层索引包裹与高选择率间接寻址失速**：如果一个向量连续经过 Filter、Project、Join 套上了三四层 `DictionaryVector`，或者原本 4,096 行的字典底座经过强过滤后只剩 3 行存活却还拖着一整块 10 MiB 的底层字符串池不释放，不仅每次读值都要连跳多次 `indices[indices[i]]` 引发缓存缺失，还会拖垮算子内存水位（例如即将写盘 Spill 或塞入网络序列化缓冲前）。此时主动调用一次紧凑物化（Compact / Flatten），只把存活的那几行拷出来释放大底座，反而是更划算的买卖。
3. **非保序算术、复杂字符串函数与兼容回滚协议**：对 `substr(url, 5, 10)` 或正则匹配，在几千项字典底座上算一遍再套回原 `indices` 依然大赚（但必须注意除零报错或非确定性函数如 `rand()` 绝不能在未被选中的死字典项上提前触发！）；而遇到跨多列复杂算术或外部黑盒 C++/Python UDF 时，老老实实解码回 `FlatVector` 才是正道。同样，在开放湖仓写入端启用新编码（如 Parquet `BYTE_STREAM_SPLIT` 或 `DELTA_LENGTH_BYTE_ARRAY`）前，还必须核对下游全部消费引擎（Spark、Trino、Flink、DuckDB 旧版本）的 **Reader 兼容性矩阵**——对生产数据湖而言，省下 15% 存储却让下游某个 ETL 读取器直接抛 `Unsupported encoding` 挂掉，首先是一起可用性故障。

---

## 五、主流存储格式与计算引擎的工程分野

把开放文件格式、内存交换标准与主流 MPP / 云数仓 / GPU 引擎放在同一张全景坐标系里横向审视，可以看到工业界围绕“谁来拍板选编码、编码能穿透几层算子”走出了清晰的路线差异：

| 系统 / 格式 | 核心编码武器库 | 选型策略（自动 vs 显式） | 执行层穿透深度与标志性取舍 |
| --- | --- | --- | --- |
| **Apache Parquet / ORC** | Dict、RLE/BitPack Hybrid、Delta 系列、Byte Stream Split / ORC RLEv2 | Writer 按类型与字典体积阈值自动降级 | 开放湖仓事实标准；局部字典作用域限于 RowGroup/Stripe，易被朴素 Reader 过早解码 |
| **Apache Arrow** | `DictionaryArray`、`RunEndEncoded`（REE）、Validity Bitmap | 上游转换或算子显式构造 | 跨语言零拷贝内存总线；设计重心是 CPU 随机直访而非极限磁盘压缩率 |
| **Meta Velox / Trino** | `Flat`、`Constant`、`Dictionary`、`Sequence/RLE`、`LazyVector` | 连接器与算子运行期动态包裹 | 将编码做成执行层一等公民；靠 `DecodedVector` 与 `DictionaryBlock` 贯穿过滤、投影与 `UNNEST` |
| **DuckDB** | Constant、RLE、FOR、BitPack、Dict、`FSST`、`ALP`、`Chimp`、`Patas` | Checkpoint 采样块统计全自动竞价选型 | 新型轻量编码学术转化标杆；证明了高带宽 SIMD 解码可令压缩落盘表跑赢未压缩内存表 |
| **ClickHouse** | `LowCardinality(T)`、`Delta`、`DoubleDelta`、`Gorilla`、`T64` + LZ4/ZSTD | 暴露给工程师按列 Schema 显式精调 | 极致扫描吞吐；专家可控性最强，但把分布误判（如高基数列误套 `LowCardinality`）的风险留给用户 |
| **StarRocks / Doris** | 列级 Dict、RLE、Bit-Shuffle、FOR + 页级 LZ4/ZSTD | 存储层自动选型 + CBO 优化器全局字典改写 | StarRocks CBO 可将低基数字符串列的 `JOIN / GROUP BY / 投影` 整条改写为全局字典整数 ID 执行 |
| **Snowflake / BigQuery / Redshift** | 微分区列式自动压缩、Capacitor Def/Rep 层级、Redshift `ENCODE AUTO` | 云端黑盒自动管理（Redshift 兼顾手动与采样建议） | 用户只管 Clustering / Sort Key；Redshift 特意抑制对 Sort Key 列的过重压缩以保护范围定位延迟 |
| **SingleStore / RAPIDS cuDF** | Seekable Dict/RLE/LZ4（SingleStore）；GPU 定长/字典/ByteStreamSplit | 面向点查或 GPU Warp 并行度定制 | SingleStore 改造块编码支持单点随机直达以满足 HTAP；GPU 极度排斥串行熵解码与变长指针追逐 |

仔细品味这张对照表，有两处工程折中格外值得做系统设计的同行留意：

第一是**“顺序扫描吞吐”与“点查定位延迟（Seekability）”的天然拉扯**。Amazon Redshift 的自动压缩器有一个极富经验的例外规则：绝不对排序键（Sort Key）列施加最激进的重度编码压缩，否则做窄区间范围扫描（Range-Restricted Scan）时花在定位解压块头上的代价会吃掉索引裁剪红利；同样，HTAP 引擎 SingleStore 专门给列存里的 Dictionary、RLE 乃至 LZ4 补齐了块内跳转索引（Seekable Encoding），就是为了防止一次高并发主键点查被迫解压几万行陪跑数据。

第二是**更新、合并（Merge-on-Read）与 Compaction 对编码秩序的侵蚀与重建**。所有惊艳的编码压缩率，几乎都建立在“不可变段（Immutable Segment）且排好序”的前提之上。一旦线上业务开始高频追加乱序数据、写入删除向量（Deletion Vector）或局部更新，原本绵延几万行的 RLE 游程会被打得粉碎，微分区 `Min/Max` 边界互相重叠，块内 FOR 差值跨度暴涨。因此，现代列存引擎里的后台 Compaction 从来不只是为了“合并小文件、清理死行”——它同时是一次**重排聚簇顺序、清洗膨胀字典、按最新统计分布重新锻造高效列编码的物理重整工序**。

---

## 六、把 Encoding 纳为物理计划属性：优化器的下一块拼图

回看过去二十年查询优化器（如 Cascades 框架）的演进，我们已经习惯了把**数据分布（Distribution）**和**排序顺序（Ordering）**建模成严格的物理属性（Physical Property），通过 `Exchange` 和 `Sort` 强制器在全局计划树上算清复利（详见[《深入 Calcite VolcanoPlanner》]({{< relref "2026-09-02-dive-calcite-cost-and-traits.md" >}})与[《Hash 还是 Sort》]({{< relref "2026-09-10-dive-hash-sort-strategy.md" >}})）。沿着同样的第一性原理往前推一步：**列编码完全应该成为继分布与顺序之后的第三类一等物理属性（`ColumnEncodingProperty`）。**

一份能够安全指导优化器做跨算子决策的编码属性签名，不能只挂一个笼统的 `"Dictionary"` 字符串标签，而必须精确刻画五个契约维度：

1. **身份与作用域（Identity & Scope）**：这份字典或基准帧绑定的是哪一个唯一字典 ID？它的有效范围是单页、单文件 Row Group，还是整张表可跨节点复用的全局字典？
2. **代数保持性（Algebraic Semantics）**：该编码只保持等值（无序字典、FSST），还是同时保持全序（保序字典、同基准 FOR）？`NULL`、浮点数 `+0.0 / -0.0` 与 `NaN` 是否满足 SQL 标准比较契约？
3. **算子能力集（Operator Capabilities）**：当前编码是否支持 `O(1)` 随机下标访问、按游程跳跃聚合，或在不解包前提下直接算哈希？
4. **状态转换算子（Enforcers / Transitions）**：正如无序变有序要插 `Sort`，当局部字典撞上跨文件 `Hash Join` 时，优化器可以在计划树里显式比较插入 `RemapDictionary`（重映射为全局码）与插入 `DecodeToFlat`（提前物化成原值）两条路径的真实 CPU 与网络字节账单，挑最便宜的位置完成转换。
5. **渐进落地路径**：在真实引擎研发中，完全不必上来就把一百个算子重写一遍。最稳健的工程节奏是：先让 Scan 彻底戒掉“出口无条件转 FlatVector”的坏习惯，立住类似 `DecodedVector` 的零拷贝兜底视图与 `LazyVector` 延迟物化机制；随后优先打通收益最高、语义最清晰的**低基数字典等值过滤 / Group By / Join 改写**与 **FOR Bit-Packing SIMD 扫描**，并以平坦路径作为语义黄金基准覆盖空值、字典越界与 Spill 边界回归验证。

当存储页、内存向量、SIMD 寄存器、网络 Shuffle 与优化器属性在同一套编码代数下咬合成闭环时，数据才真正摆脱了笨重的字面外壳，以最贴合硅片物理规律的紧凑形态在算子间高速流淌。

---

## 参考资料与延伸阅读

> **资料与范围说明**：文中涉及的开源格式规范与引擎机制分别核对自 Apache Parquet Encoding / Versioning 规范、ORC v1 规范、Apache Arrow Columnar Format（含 Run-End Encoding）、Meta Velox Vector 文档、DuckDB Storage Internals、ClickHouse Compression / `LowCardinality` 文档、StarRocks CBO 低基数字典优化文档及 Apache Doris 列存文档（截至 2026 年 10 月复核状态）。第六节关于统一 `ColumnEncodingProperty` 物理属性代数的建模属于面向执行引擎架构的演进推演。

### 经典论文脉络

1. Claude E. Shannon, *A Mathematical Theory of Communication*, [Bell System Technical Journal 1948](https://doi.org/10.1002/j.1538-7305.1948.tb00917.x).
2. Mike Stonebraker et al., *C-Store: A Column-oriented DBMS*, [VLDB 2005](https://www.vldb.org/archives/website/2005/program/paper/thu/p553-stonebraker.pdf).
3. Peter Boncz, Marcin Zukowski, Niels Nes, *MonetDB/X100: Hyper-Pipelining Query Execution*, [CIDR 2005](https://www.cidrdb.org/cidr2005/papers/P19.pdf).
4. Daniel J. Abadi, Samuel Madden, Miguel Ferreira, *Integrating Compression and Execution in Column-Oriented Database Systems*, [SIGMOD 2006](https://www.cs.umd.edu/~abadi/papers/abadisigmod06.pdf).
5. Yinan Li, Jignesh M. Patel, *BitWeaving: Fast Scans for Main Memory Data Processing*, [SIGMOD 2013](https://pages.cs.wisc.edu/~jignesh/publ/bitweaving.pdf).
6. Samy Chambi, Daniel Lemire et al., *Better bitmap performance with Roaring bitmaps*, [Software: Practice and Experience 2016](https://roaringbitmap.org/).
7. Peter Boncz, Thomas Neumann, Viktor Leis, *FSST: Fast Random Access String Compression*, [PVLDB 2020](https://vldb.org/pvldb/vol13/p2649-boncz.pdf).
8. Panagiotis Liakos et al., *Chimp: Efficient Lossless Floating Point Compression for Time Series Databases*, [PVLDB 2022](https://www.vldb.org/pvldb/vol15/p3058-liakos.pdf).
9. Maximilian Kuschewski et al., *BtrBlocks: Efficient Columnar Compression for Data Lakes*, [SIGMOD 2023](https://www.cs.cit.tum.de/fileadmin/w00cfj/dis/papers/btrblocks.pdf).
10. Azim Afroozeh, Peter Boncz, *The FastLanes Compression Layout: Decoding > 100 Billion Integers per Second with Scalar Code*, [PVLDB 2023](https://github.com/cwida/FastLanes).
11. Azim Afroozeh, Leonardo X. Kuffo, Peter Boncz, *ALP: Adaptive Lossless floating-Point Compression*, [SIGMOD 2024](https://github.com/cwida/ALP).

### 工业系统文档与工程案例

- **格式与内存标准**：[Parquet Encodings](https://parquet.apache.org/docs/file-format/data-pages/encodings/) & [Format Versioning](https://parquet.apache.org/docs/file-format/versions/) · [ORC v1 Specification](https://orc.apache.org/specification/ORCv1/) · [Apache Arrow Columnar Format](https://arrow.apache.org/docs/format/Columnar.html) & [Querying Parquet with Millisecond Latency](https://arrow.apache.org/blog/2022/12/26/querying-parquet-with-millisecond-latency/)
- **计算引擎与存储实现**：[Meta Velox Vectors](https://facebookincubator.github.io/velox/develop/vectors.html) · [Trino UNNEST DictionaryBlock Optimization](https://trino.io/blog/2019/08/23/unnest-operator-performance-enhancements.html) · [DuckDB Compression Internals](https://duckdb.org/docs/stable/internals/storage.html) · [ClickHouse Database Compression](https://clickhouse.com/resources/engineering/database-compression) · [StarRocks Low-Cardinality Join Optimization](https://docs.starrocks.io/docs/sql-reference/System_variable/#cbo_enable_low_cardinality_optimize_for_join) · [Apache Doris Columnar Storage](https://doris.apache.org/docs/dev/key-features/columnar-storage/) · [SingleStore Seekable Columnstore](https://docs.singlestore.com/db/v9.0/create-a-database/columnstore/) · [RAPIDS cuDF Parquet Guide](https://docs.rapids.ai/api/cudf/stable/user_guide/api_docs/api/cudf.dataframe.to_parquet/)
