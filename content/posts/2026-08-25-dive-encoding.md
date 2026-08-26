---
title: "【原理】Column Encoding：从信息表示到 Encoding-aware Execution"
date: 2026-08-25T00:00:00+08:00
lastmod: 2026-08-30T00:00:00+08:00
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
description: "从信息表示、列式存储和向量化执行出发，系统分析 Dictionary、RLE、Delta、Bit-packing、FSST 与浮点编码，讨论 Encoding-aware Execution、跨算子传播和物理计划建模。"
draft: false
---

## 引言：计算从来不是作用在“抽象数据”上

1948 年，Claude Shannon 在《[A Mathematical Theory of Communication](https://doi.org/10.1002/j.1538-7305.1948.tb00917.x)》中把通信的基本问题概括为：如何在一个位置精确地或近似地复现另一个位置所选择的消息。信息论由此把“意义”和“表示”暂时分开，开始研究消息如何编码、传输，以及统计结构允许我们压缩到什么程度。

这个问题同样存在于数据库内部。用户看到的是字符串、时间戳、金额和嵌套对象；处理器真正读取的却是 Bit、Byte、Offset、Dictionary ID 和连续的 Vector。一个逻辑值可以拥有多种物理表示，而不同表示会唤起完全不同的算法：

```text
同一列逻辑值
    │
    ├─ Flat values ─────► 逐值比较、Hash、聚合
    ├─ Dictionary IDs ──► 整数比较、字典传播、无复制重映射
    ├─ RLE / REE ───────► 按 Run 过滤和聚合
    ├─ Delta / FOR ─────► Bit-packed SIMD Decode
    └─ Constant ────────► 元数据级判断，不必逐行执行
```

“程序 = 数据结构 + 算法”之所以重要，是因为数据结构并不是算法之前的静态容器。表示方式决定了哪些操作便宜、哪些操作昂贵，也决定了 CPU Cache、Memory Bandwidth、SIMD、网络和存储系统如何参与计算。编码因此不是数据落盘时的一个附属选项，而是数据分布、执行算法与硬件之间的契约。

熵给出了无损编码在统计意义上的理论边界，但查询引擎追求的并不只是最小字节数。压得更小的格式可能解码更慢，顺序扫描友好的格式可能不适合随机访问，局部字典可能节省 I/O 却无法直接用于跨文件 Join。数据库真正优化的是一个更复杂的目标：

```text
总成本 = 存储与读取成本
       + 解码与物化成本
       + 内存带宽与缓存成本
       + 算子执行成本
       + Shuffle 与 SerDe 成本
       + 更新、合并和演化成本
```

因此，本文关注的不是“哪一种压缩算法压缩率最高”，而是三个更接近计算本质的问题：

1. 逻辑数据在不同 Encoding 与 Compression 下呈现什么物理形态？
2. Filter、Join、Aggregate 和 Shuffle 如何直接利用这些形态？
3. Parquet、Arrow、Velox、DuckDB 等系统如何把 Encoding 从存储层贯穿到执行层？

贯穿全文的核心思考是：**高性能计算不是尽快把数据恢复成统一形态，而是在正确性允许的范围内，尽可能晚地放弃已经存在的结构。**

## 0. 核心结论

Column encoding 已经不是“存储压缩技巧”，而是现代计算引擎的核心执行机制之一。它贯穿了四层：

```text
Storage Format Encoding
  Parquet / ORC / Capacitor / Micro-partition / DuckDB storage / ClickHouse parts

In-memory Column Encoding
  Arrow DictionaryArray / Run-End Encoding
  Velox DictionaryVector / ConstantVector / LazyVector
  Trino/Presto DictionaryBlock / RLEBlock

Encoding-aware Execution
  在 encoded data 上直接 filter / join / aggregate / project
  late materialization
  dictionary propagation
  RLE-aware aggregation
  SIMD bit-packed scan

Serialization / Deserialization Optimization
  Parquet → Arrow 保留 dictionary
  Arrow zero-copy IPC / Flight
  避免字符串、重复值、嵌套结构过早 materialize
```

最重要的判断是：

**未来高性能计算引擎不会把 encoding 当成 scan 之前的“解压步骤”，而会把 encoding 当成物理执行计划的一部分。**

也就是说，优化器和执行器要知道：

```text
这个 column 是 dictionary encoded
这个 batch 是 RLE encoded
这个 vector 是 constant
这个 string column 可以用 global dictionary 做 join
这个 timestamp column 可以用 delta/double-delta
这个 float column 适合 ALP/Gorilla/Chimp
这个 Parquet page 可以不解码直接过滤
```

这和 Query Engine 的本质高度一致：**利用数据分布、物理布局和硬件特性，降低执行代价。**

---

## 1. Column Encoding 与 Compression 的区别

很多系统文档会混用 encoding 和 compression，但在工程上二者应该区分。

### 1.1 Encoding 是语义级转换

Encoding 利用列值的结构性特征，把逻辑值变成更适合存储和执行的表示。

例如：

```text
["US", "CN", "US", "JP", "US"]

dictionary encoding:

dict:
  0 -> "US"
  1 -> "CN"
  2 -> "JP"

codes:
  [0, 1, 0, 2, 0]
```

这不是普通字节压缩，而是把 string domain 映射成 integer domain。

再比如：

```text
timestamp:
1000, 1001, 1002, 1003

delta:
1000, +1, +1, +1

delta-of-delta:
1000, +1, 0, 0
```

这里利用的是时间序列单调递增和间隔稳定的分布特性。

### 1.2 Compression 是字节级压缩

Compression 更接近通用压缩算法：

```text
LZ4
ZSTD
Snappy
Gzip
Brotli
Zlib
```

它们作用在 encoded bytes 上，进一步减少字节数。PyArrow 文档明确说，Parquet column data page 会先经过 dictionary、RLE 等 encoding，再进行 Snappy、Brotli、Gzip、ZSTD、LZ4 等 compression。

所以一个典型 Parquet 写入链路是：

```text
Logical Column Values
  ↓
Column Encoding
  dictionary / RLE / delta / bit-packing / byte-stream-split
  ↓
Page Compression
  Snappy / ZSTD / LZ4
  ↓
Column Chunk
  ↓
Row Group
  ↓
File
```

工程上要记住一句话：

**Encoding 决定数据是否适合被计算，Compression 决定数据是否更少占用 I/O 和存储。**

---

## 2. 学术脉络：Column Encoding 为什么会成为计算引擎核心能力？

### 2.1 Column Store 让数据天然更容易 encoding

经典的 Column Store 论文指出，列式存储把同一属性的值连续存放，这会显著增加相邻值的相似性，从而创造更好的压缩机会；同时，一次压缩多个相邻 tuple 能降低 per-tuple 的 CPU 和空间开销。Abadi、Madden、Ferreira 在 SIGMOD 2006 的 [Integrating Compression and Execution in Column-Oriented Database Systems](https://www.cs.umd.edu/~abadi/papers/abadisigmod06.pdf) 中进一步提出，Column Store 不应该只是“压缩后再解压执行”，而应该研究如何在压缩数据上执行查询。

这篇论文的核心观点到今天仍然成立：

```text
Column Store 的优势不是：
  少读列

而是：
  少读列
  + 更高压缩率
  + 更低内存带宽
  + 可以直接在 compressed / encoded representation 上执行
```

### 2.2 C-Store / Vertica：排序、投影和 encoding 是一体的

[C-Store](https://www.vldb.org/archives/website/2005/program/paper/thu/p553-stonebraker.pdf) 把列式系统定义成 read-optimized DBMS，核心设计包括 projections、按不同顺序存储、read-optimized store 和 write store。

这里有一个很深的点：

**排序顺序本身会改变 encoding 效果。**

例如：

```text
如果按 country 排序：
  US, US, US, US, CN, CN, JP, JP

RLE / dictionary / bitmap 都会更有效。

如果随机顺序：
  US, CN, US, JP, US, CN, JP

RLE 几乎没收益。
```

这也是为什么 Vertica、ClickHouse、Snowflake clustering、BigQuery clustering、StarRocks sort key、Doris sort key 都和 encoding/compression 有强关联。

### 2.3 MonetDB/X100：vectorized execution 让 encoding 可以进入 CPU pipeline

[MonetDB/X100](https://www.cidrdb.org/cidr2005/papers/P19.pdf) 提出以 vector processing 为核心的 query execution，使执行引擎能更好利用 CPU cache、SIMD 和 pipeline。X100 论文明确指出，它像 Volcano-style engine，但关键区别是所有执行都基于 vector processing，从而显著提高 CPU efficiency。

Column encoding 和 vectorized execution 天然契合：

```text
一个 vector batch:
  1024 / 2048 / 4096 rows

每个 column vector:
  可以是 flat
  可以是 dictionary
  可以是 RLE
  可以是 constant
  可以是 bit-packed

operator:
  对整个 vector 做 branch-light / SIMD-friendly 执行
```

这就是现代引擎 Velox、DuckDB、ClickHouse、Photon、DataFusion、Trino 的共同方向。

### 2.4 BitWeaving / ByteSlice：encoding 可以直接服务 predicate scan

BitWeaving 的目标是让 main-memory scan 接近 processor speed，通过 bit-level parallelism 让 predicate evaluation 更接近 bare metal speed。

ByteSlice 则提出 byte-level columnar layout，同时支持高效 scan 和 lookup，并利用 SIMD data parallelism；论文摘要中报告 scan speed 小于 0.5 processor cycle per column value。

这两类研究说明：

**encoding 不只是为了省空间，而是可以改变 scan operator 的计算复杂度和 CPU 指令形态。**

传统 scan：

```text
for v in values:
    if v > 100:
        emit
```

encoding-aware scan：

```text
bit-sliced / byte-sliced representation
  ↓
SIMD compare many values at once
  ↓
produce selection mask
```

这和数据库向量化执行、selection vector、SIMD filter 是同一个方向。

---

## 3. Column Encoding 的主要类型与适用分布

下面从数据分布特性出发，整理主流 encoding。

---

### 3.1 Constant Encoding

适用分布：

```text
一个 segment / page / vector 中所有值相同
```

示例：

```text
partition_date = '2026-06-15'
```

存储方式：

```text
只存一个 value
+ row count
```

收益：

```text
存储空间极低
filter 可以直接根据单值判断
count / group by 可以直接利用 row count
vector execution 可使用 ConstantVector
```

[DuckDB Storage Internals](https://duckdb.org/docs/stable/internals/storage.html) 把 Constant Encoding 列为支持的 compression algorithm；如果一个 column segment 中每个值都相同，就只需要存储该单个值。

执行层也广泛使用 constant encoding。[Velox Vector 文档](https://facebookincubator.github.io/velox/develop/vectors.html)说明，Vector 有 type、encoding 和 size，flat、constant、dictionary 等 encoding 可与任意类型组合。

工程意义：

```text
Constant encoding 是最简单但最重要的 encoding。
很多 query optimizer 的 constant folding、partition pruning、metadata aggregation
都可以和它结合。
```

---

### 3.2 Run-Length Encoding / Run-End Encoding

适用分布：

```text
连续重复值
排序后重复值
低 cardinality 且有局部聚集
大量连续 null
```

示例：

```text
A, A, A, A, B, B, B, C, C

RLE:
(A, 4), (B, 3), (C, 2)
```

收益：

```text
存储空间低
filter 可以一次处理一段 run
count/group by 可以用 run length 加权
aggregation 可以跳过逐行处理
```

[Parquet Encoding 文档](https://parquet.apache.org/docs/file-format/data-pages/encodings/)说明，RLE/Bit-Packing Hybrid encoding 用于更高效地存储重复值；在 Parquet 中，RLE 用于 repetition/definition levels、dictionary indices，以及 boolean values。

[ORC v1 规范](https://orc.apache.org/specification/ORCv1/)中，RLEv2 提供更好的压缩和 fixed bit width encoding，并包含多种 sub-encoding。

[Arrow Columnar Format](https://arrow.apache.org/docs/format/Columnar.html#run-end-encoded-layout) 的 Run-End Encoding 是 RLE 的一种变体，每个 run 存储实际值以及该 run 结束位置，适合包含连续相同值的数据。

工程意义：

```text
RLE 的效果高度依赖排序/聚簇。
```

如果数据是随机分布，RLE 效果差；如果按某些维度排序，RLE 会非常强。

---

### 3.3 Dictionary Encoding

适用分布：

```text
低基数字符串
枚举值
状态码
国家、省份、渠道、设备类型
重复出现的长字符串
```

示例：

```text
["click", "view", "click", "buy"]

dict:
0 -> click
1 -> view
2 -> buy

codes:
[0, 1, 0, 2]
```

收益：

```text
string → integer
减少存储
减少内存
减少 hash / compare 成本
group by / join 可在 int code 上执行
shuffle / exchange 可传 code
```

Parquet 官方文档说明，dictionary encoding 会为 column chunk 建 dictionary page，数据值以整数形式存储，并使用 RLE/Bit-Packing Hybrid 编码；如果 dictionary 太大，会 fallback 到 plain encoding。

ClickHouse 的 `LowCardinality(T)` 是非常典型的工业化 dictionary encoding。官方文档说明，LowCardinality 会改变数据存储方式和处理规则，ClickHouse 对 LowCardinality 列应用 dictionary coding，并且对很多应用可以显著提升 SELECT 性能。 ClickHouse 最佳实践建议，对于少于约 10,000 个 unique values 的列使用 LowCardinality，以通过 dictionary encoding 显著减少存储。

Velox 也大量使用 Dictionary encoding。Velox 文档明确说，Dictionary encoding 用于紧凑表示重复值，也用于不复制数据地表示 row subset；DictionaryVector 由 nulls buffer、indices buffer 和 base vector 组成。

工程意义：

```text
Dictionary encoding 是目前最重要的 column encoding。
它连接了 storage、memory、execution、shuffle、join、group by。
```

真正高级的系统不会在 scan 时把 dictionary code 立刻解成 string，而是尽量把 code 传播到后续算子。

---

### 3.4 Bit-Packing / Frame of Reference / PFOR

适用分布：

```text
整数范围小
dictionary id 小
timestamp delta 小
某个 block 内 min/max 差距小
```

Frame of Reference：

```text
原始值:
1001, 1002, 1004, 1005

base = 1000
offset:
1, 2, 4, 5
```

Bit-packing：

```text
如果 offset 最大值 < 8
只需要 3 bits 表示
```

收益：

```text
极大减少整数列存储
decode 可 SIMD
更少 memory bandwidth
```

DuckDB 支持 Bit Packing 和 Frame of Reference。

Parquet 的 RLE/Bit-Packing Hybrid 则常用于 dictionary indices、definition/repetition levels 等小整数流。

BtrBlocks、FastLanes 等新研究也把 bit-packing、FOR、dictionary、delta 等轻量压缩组合成更适合 data lake 和现代硬件的列格式。BtrBlocks 指出，云上 data lake 使用 Parquet 等开放格式，但远程对象存储和高速网络下，低效 decompression 会让 scan 变成 CPU-bound；BtrBlocks 使用一组 lightweight encoding schemes 来优化这种场景。 FastLanes 则强调对常见 LWC schemes 加速解码，并在真实数据上相比 Parquet 提升压缩率和 decompression 速度。

工程意义：

```text
Bit-packing/FOR 是 integer-heavy analytics 的基础设施。
它们对 SIMD、cache、memory bandwidth 极其友好。
```

---

### 3.5 Delta / DoubleDelta Encoding

适用分布：

```text
单调递增数字
timestamp
sequence id
event time
log offset
指标采样时间
```

Delta：

```text
1000, 1010, 1020, 1030
→ 1000, 10, 10, 10
```

DoubleDelta：

```text
1000, 1010, 1020, 1030
delta:
10, 10, 10
delta-of-delta:
0, 0
```

ClickHouse 官方 compression docs 建议，对于 delta 较小的序列，Delta codec 可以有效；对于时间序列数据，DoubleDelta 经常更有效；Gorilla 对 gauge 类 floating point data 有效，T64 对 sparse data 或 block 内 range 小的数据有效。

Parquet 也支持 DELTA_BINARY_PACKED、DELTA_LENGTH_BYTE_ARRAY、DELTA_BYTE_ARRAY 等 encoding。Parquet 官方 encoding 文档列出了 delta binary packed、delta length byte array 和 delta byte array 等 encoding。

工程意义：

```text
时间序列、日志、监控、CDC、append-only fact table
天然适合 delta/double-delta。
```

---

### 3.6 Float Encoding：Gorilla / Chimp / Patas / ALP / Byte Stream Split

浮点数很特殊。

整数可以 delta、FOR、bit-pack，但 IEEE 754 float/double 的二进制表示并不总是和数值分布保持简单关系。很多真实业务浮点数是：

```text
价格
温度
指标
延迟
比率
金额
传感器读数
embedding component
```

这些值可能在十进制上很有规律，但在二进制浮点上不容易压缩。

Gorilla 系列利用相邻 floating-point values 的 XOR 特性，适合 time-series metrics。Chimp 是面向 floating point time series 的 lossless compression algorithm，论文公开了代码和复现实验。 Patas 和 Chimp 的 EDBT 2024 论文也强调，浮点列通常相似但传统算法 savings modest，因此提出 Chimp 和 Patas 以提高压缩率，并讨论其对 DuckDB 的影响。

ALP 是 SIGMOD 2024 的 Adaptive Lossless floating-Point Compression。它基于真实浮点数据中的模式自适应选择策略，并把浮点数转换成更可压缩的表示；DuckDB 文档也把 ALP 列为其支持的 compression algorithm。

Parquet 的 Byte Stream Split 则适合 fixed-width numeric types，特别是和通用压缩算法组合时可以让相同字节位聚集，提高压缩效果。RAPIDS cuDF 文档也列出了 BYTE_STREAM_SPLIT、DELTA_BYTE_ARRAY、DICTIONARY 等 Parquet encoding 类型。

工程意义：

```text
Float encoding 正在变成新热点。
原因是 AI / observability / metrics / vector workloads 使 float 列暴涨。
```

不过要注意：

```text
用于向量检索的 embedding 压缩通常还涉及 quantization / product quantization，
这和数据库 column encoding 有交集，但不是完全同一件事。
```

---

### 3.7 String Encoding：Dictionary / FSST / Delta Byte Array / Front Coding

字符串是 OLAP 系统最难处理的数据类型之一：

```text
变长
比较慢
hash 慢
cache locality 差
占内存大
序列化成本高
```

低基数字符串适合 dictionary encoding；但高基数字符串、URL、JSON、路径、日志 message 等不一定适合传统 dictionary。

FSST 是近几年非常重要的 string compression 研究。FSST 论文指出，字符串在真实数据集中普遍存在，常占据大量数据且处理缓慢；FSST 是 lightweight string compression，目标是提供类似 fast compression method 的速度，同时有更好的压缩因子。 FSST GitHub 项目说明，FSST 支持对 compressed data 的随机访问，单个字符串可以不用解压周围 block 就被解压；相比 LZ4 这类 block-based 压缩，FSST 在保持类似速度的同时有更好的压缩率。

NVIDIA RAPIDS 的 Parquet string data guide 给出了很实用的经验：默认 dictionary encoding 对 distinct values 少于约 100K 的 string data 效果好；distinct 更多时，delta 和 delta length encoding 往往能产生更小文件，尤其是短字符串。

工程意义：

```text
string encoding 的关键目标不是只省空间，
而是把 string operation 变成 integer operation，
或至少避免重复 materialization。
```

这对 join、group by、distinct、sort、shuffle 都非常关键。

---

### 3.8 Nested Data Encoding：Definition / Repetition Levels

Parquet 和 BigQuery/Dremel 体系对嵌套数据使用 definition level 和 repetition level 表示嵌套结构。

BigQuery Capacitor 博客说明，BigQuery 使用支持 semi-structured data、nested/repeated fields 的 columnar storage；每个 column 除了 value，还存 definition 和 repetition levels，从而只读请求列也能重建完整或部分结构。

这类 encoding 对现代数据 lakehouse 非常重要，因为实际数据越来越多是：

```text
JSON
ARRAY
MAP
STRUCT
PROTOBUF
日志事件
半结构化业务属性
```

但 nested encoding 的代价也明显：

```text
definition/repetition level decode 成本
nested vector materialization 成本
filter pushdown 更复杂
schema evolution 更复杂
```

Spark 文档显示，Spark 支持 Parquet vectorized decoding，并且 nested column vectorized reader 默认启用。 这说明主流引擎正在把 nested decode 也纳入 vectorized reader。

---

## 4. 工业界主流系统与产品现状

### 4.1 Parquet：开放数据湖事实标准，encoding 非常丰富

Parquet 是当前 lakehouse 和大数据系统最核心的开放列式格式之一。

核心 encoding 包括：

```text
PLAIN
RLE / Bit-Packing Hybrid
Dictionary
Delta Binary Packed
Delta Length Byte Array
Delta Byte Array
Byte Stream Split
```

Parquet 官方 encoding 文档明确说明 dictionary page、dictionary indices、fallback 到 plain encoding 等机制。

Parquet 的优势：

```text
开放生态强
Spark / Trino / Flink / DuckDB / DataFusion / ClickHouse / Doris / StarRocks / BigQuery external / Snowflake external 均支持
行组 + 列块 + 页结构适合分布式扫描
encoding + compression 组合成熟
```

不足：

```text
很多 encoding 在 reader 中会被过早 decode
dictionary 通常是 row group / column chunk 局部的，不利于跨文件 join/group by
page-level encoding metadata 对 optimizer 暴露不足
nested decode 复杂
面向对象存储和高速网络的 decode CPU 成本越来越突出
```

这也是 BtrBlocks、FastLanes、global dictionary research 继续活跃的原因。

---

### 4.2 ORC：Hadoop/Hive 生态中更强的内建索引与 encoding

ORC 和 Parquet 一样是主流列式格式。ORC v1 引入 RLEv2，提供更好的 compression 和 fixed bit width encoding，并根据数据使用多种 sub-encoding。

ORC 的特点：

```text
stripe / row group / stream 组织
内建 column statistics
bloom filter
dictionary/direct encoding
RLEv2 对整数流优化较强
Hive/Spark/Trino 生态成熟
```

ORC 的工业价值在于：

```text
读路径非常适合 predicate pushdown
与 Hive/Trino/Spark 的老生态结合深
```

Trino 官方博客 **Even Faster ORC** 中提到，ORC reader 对 all-null case 返回 RLE block，并在 null handling 上拆分 no-null loop 和 mixed-null loop，以提升性能。 这说明 ORC reader 不只是“读文件”，而是直接产出更适合执行层的 encoded block。

---

### 4.3 Apache Arrow：内存格式与 SerDe 的核心标准

Arrow 是 in-memory columnar format。DataFusion 文档说，Arrow 定义标准化列式内存表示，使不同系统和语言可以 zero-copy 共享数据，避免 serialization overhead，并支持 vectorized execution。

Arrow 的 encoding 包括：

```text
DictionaryArray
Run-End Encoded Array
Validity bitmap
Offset buffer for variable length values
```

Arrow FAQ 也说明，Arrow 通常不是强压缩格式，而是面向 CPU 直接访问；但它提供 dictionary encoding、run-end encoding 和 buffer compression 等有限空间效率选项。

最关键的是：

**Parquet → Arrow 时保留 dictionary encoding，可以显著减少内存和反序列化开销。**

[Apache Arrow 的 Parquet Reader 案例](https://arrow.apache.org/blog/2022/12/26/querying-parquet-with-millisecond-latency/)显示，保留 Dictionary Encoding 在特定数据和 Reader 实现中可获得超过 60 倍的提升，并显著减少内存使用。这个数字不是所有 Workload 的普遍收益，但它清楚展示了过早物化重复字符串的代价。

工程意义：

```text
如果 Parquet reader 把 dictionary encoded string 全部 decode 成 UTF-8 strings，
就丢掉了最重要的性能收益。

正确做法：
  Parquet dictionary page
    ↓
  Arrow DictionaryArray
    ↓
  execution engine dictionary-aware operators
```

---

### 4.4 Velox：把 encoding 做成执行层一等公民

Velox 是 Meta 开源的 unified execution engine。Meta 官方介绍说，Velox 的 Vector 模块是 Arrow-compatible columnar memory layout，支持 flat、dictionary、constant、sequence/RLE、frame of reference、lazy materialization 等 encoding。

Velox 文档中，Vector 由 type、encoding、size 组成，flat、constant、dictionary encodings 可与任意 type 组合。

Velox 论文还提到 DecodedVector 抽象：它把任意 encoded Vector 转成逻辑上一致的 flat vector + indices API；对于 flat、constant 和 single-level dictionary inputs，DecodedVector 可以 zero-copy。

这点非常重要。

执行器有两个选择：

```text
方式 A：
  每个 operator 都理解所有 encoding
  性能高，但代码复杂

方式 B：
  scan 后统一 decode 成 flat
  代码简单，但性能差

Velox 选择：
  通过 Vector/DecodedVector 抽象平衡性能和工程复杂度
```

这代表现代计算引擎的方向：

**encoding 不再隐藏在 storage reader 内，而是贯穿 expression、operator、connector、memory manager。**

---

### 4.5 Trino / Presto：DictionaryBlock 与 RLEBlock 贯穿执行

Presto/Trino 的论文 **Presto: SQL on Everything** 说明，Presto 可以使用 dictionary 和 RLE blocks；多个 pages 可以共享 dictionary，从而改善内存效率。

Trino 的 UNNEST operator 优化是 encoding-aware execution 的经典工业案例。[Trino 官方博客](https://trino.io/blog/2019/08/23/unnest-operator-performance-enhancements.html)展示了如何用 DictionaryBlock 创建指向输入元素的输出，避免深拷贝并显著降低 CPU 与内存分配；具体收益取决于列类型和数据分布。

这个案例说明：

```text
Dictionary encoding 不只是 storage compression。
它也可以表示：
  projection
  filtering result
  unnest result
  row subset
  repeated output
```

也就是说，dictionary vector 本质上是一种：

```text
base vector + selection/remapping indices
```

这和 Query Engine 中的 selection vector、late materialization、zero-copy projection 是同一类思想。

---

### 4.6 DuckDB：轻量压缩与新型 encoding 的试验田

DuckDB 是当前 column encoding 创新非常活跃的开源系统之一。官方 storage docs 列出支持的 compression algorithms：

```text
Constant Encoding
Run-Length Encoding
Bit Packing
Frame of Reference
Dictionary Encoding
FSST
ALP
Chimp
Patas
Zstd
```

DuckDB tuning 文档还指出，DuckDB 默认只对 persistent on-disk database 应用 lightweight compression；有些情况下，on-disk tables 反而可能比 in-memory tables 查询更快，因为 persistent table 使用了 lightweight compression。

这句话非常值得品味：

**压缩数据不一定更慢。**

原因是：

```text
压缩后：
  读的 bytes 更少
  cache miss 更少
  memory bandwidth 更少
  vectorized decoder 很快

未压缩：
  decode 成本没有
  但 memory bandwidth 更高
  cache locality 更差
```

在现代 CPU 上，memory bandwidth 经常比 ALU 更宝贵。

DuckDB 也很快吸收学术成果：FSST、ALP、Chimp、Patas 都已经进入其 compression algorithm 列表。

---

### 4.7 ClickHouse：用户可显式选择 encoding/codecs，LowCardinality 工业化成熟

ClickHouse 的 encoding/codecs 体系很工程化。

常见选择：

```text
LowCardinality
Delta
DoubleDelta
Gorilla
T64
LZ4
ZSTD
```

[ClickHouse 的压缩说明](https://clickhouse.com/resources/engineering/database-compression)把 Encoding 与通用 Compression 区分为两个阶段；`LowCardinality(String)` 使用字典表示重复字符串，Delta、DoubleDelta、Gorilla 和 T64 则针对不同数值分布。

ClickHouse compression docs 对 codec 选择也有明确建议：

```text
Delta:
  适合 delta 较小的数据

DoubleDelta:
  适合时间序列

Gorilla:
  适合 floating point gauge readings

T64:
  适合 sparse data 或 block 内 range 小的数据
```

ClickHouse 的特点是：

```text
把 encoding 选择暴露给用户
用户可以基于数据分布和 workload 调整
系统保持非常高的扫描吞吐
```

缺点是：

```text
需要用户理解数据分布
错误 codec 可能负优化
LowCardinality 过高 cardinality 时可能变差
```

---

### 4.8 StarRocks / Doris：低基数字符串与字典执行优化

StarRocks 文档说明其 internal tables 使用 columnar storage，物理上 column 被分成 data blocks，encoded、compressed 后持久化存储。

StarRocks 还有面向 low-cardinality dictionary encoded string columns 的 optimizer rewrite。[系统变量文档](https://docs.starrocks.io/docs/sql-reference/System_variable/#cbo_enable_low_cardinality_optimize_for_join)说明，优化器可以重写 Join ON Predicate、Join Predicate 和 Projection，以利用低基数字符串列的 Dictionary Encoding。

这说明 StarRocks 不满足于 storage 层字典压缩，而是在执行计划里把 string join 改成 dictionary id join。

[Doris Columnar Storage 文档](https://doris.apache.org/docs/dev/key-features/columnar-storage/)也明确区分逐列 Encoding 与逐页 Compression：不同列可采用 Plain、Dictionary、RLE、Bit-shuffle 或 FOR，编码后的 Page 再由 LZ4、ZSTD 等算法压缩。

这代表国内 OLAP 系统的一个重要方向：

```text
storage encoding
  ↓
vectorized scan
  ↓
dictionary-aware expression
  ↓
join/group by rewrite
```

---

### 4.9 Spark / Databricks Photon：从 row execution 到 columnar batch

Spark 对 Parquet 的 vectorized reader 已经非常成熟。Spark 文档中 `spark.sql.parquet.enableVectorizedReader` 默认启用；`spark.sql.parquet.columnarReaderBatchSize` 控制 vectorized reader batch rows；nested column vectorized reader 也默认启用。

Databricks Photon 则是原生 vectorized query engine。Databricks 文档说 Photon 以 columnar batches 处理数据，相比传统 row-based execution 带来显著性能提升，并兼容 Spark APIs。 Photon 论文也把它定义为面向 Lakehouse 的 vectorized query engine，支持直接处理 Parquet 等原始数据格式。

Spark/Photon 路线说明：

```text
大数据系统的性能瓶颈不只是 shuffle 和调度，
scan/deserialize/decode/expression 也必须 columnar + vectorized。
```

---

### 4.10 BigQuery / Snowflake：闭源产品也以 columnar compressed storage 为核心

BigQuery 官方文档说明，BigQuery 以 columnar format 存储 table data，即每列单独存储；列式数据库特别适合扫描整个数据集中的单列。 BigQuery Capacitor 博客进一步说明，BigQuery 使用支持 nested/repeated fields 的 columnar storage，并通过 definition/repetition levels 重建结构。

Snowflake 官方文档说明，所有表数据自动划分为 micro-partitions，每个 micro-partition 包含 50MB–500MB 未压缩数据，实际大小更小，因为数据总是压缩存储；micro-partitions 以 columnar 方式组织。 Snowflake 还说明，它管理数据存储的 organization、file size、structure、compression、metadata 和 statistics。

这些系统不公开具体 encoding 细节，但它们的方向非常清楚：

```text
自动列式布局
自动压缩
自动 micro-partition / block metadata
自动 pruning
自动 statistics
```

闭源云数仓的策略通常是：

```text
不暴露 encoding knob
让系统自动选择
用户只关心 clustering / partitioning / query pattern
```

---

### 4.11 Redshift / Vertica / SingleStore：传统 MPP/HTAP 产品中的 encoding 工程

Amazon Redshift 把 column compression encoding 作为用户可见能力。官方文档说明，ENCODE AUTO 是默认选项，由 Redshift 自动管理所有列的 compression encoding。 `ANALYZE COMPRESSION` 会基于表内容 sample 给出列 encoding 建议，并估计相比 RAW 的磁盘空间节省。

Redshift 文档还提醒，automatic compression 对 sort key columns 会谨慎处理，因为 range-restricted scans 在 sort key columns 被压得过重时可能表现不好，因此自动 compression 会对 sort key 采用特殊策略。

这个点很重要：

**最小存储空间不一定等于最快查询性能。**

Vertica 的 encoding 类型也非常丰富，包括 delta、dictionary、RLE 等不同 scheme。

SingleStore 则强调 columnstore string data 的 seekable encoding。其文档说，Dictionary Encoding、RLE、LZ4 Encoding 被扩展为 seekable 并支持 point-access，以同时获得低查询延迟和存储节省。

这说明：

```text
scan-friendly encoding
和
point-access-friendly encoding

并不总是一致。
```

HTAP 系统尤其需要 seekable encoding，否则 point lookup 会因为 block-level decompression 付出太高延迟。

---

### 4.12 RAPIDS cuDF / GPU 方向：encoding 也必须适配 GPU

RAPIDS cuDF 的 Parquet writer 选项中，`use_dictionary=True` 会优先使用 dictionary encoding，但受 `max_dictionary_size` 限制。 NVIDIA 的 Parquet string guide 强调，string encoding/compression 的效果高度依赖数据本身，cardinality 和 string length 会主导结果；对于少于约 100K distinct values 的字符串列，默认 dictionary encoding 效果好。

GPU 对 encoding 的要求和 CPU 不一样：

```text
适合 GPU:
  fixed-width
  branch-light
  warp-friendly
  coalesced memory access
  bit/byte parallelism

不适合 GPU:
  大量变长分支
  指针追逐
  串行 entropy decoding
  不规则 dictionary lookup
```

所以 GPU-accelerated query engine 很可能推动新的 tabular encoding 设计。

---

## 5. Column Encoding 如何带来性能收益？

### 5.1 存储空间收益

最直接：

```text
少存 bytes
降低磁盘成本
降低对象存储成本
降低 cache footprint
降低 replica / backup / snapshot 成本
```

对于云数据仓库和 lakehouse，存储空间收益会直接转成成本收益。

BigQuery 采用 columnar storage；Snowflake micro-partitions 以 compressed columnar 形式组织；ClickHouse、DuckDB、Doris、Redshift 都强调压缩减少存储并改善 I/O。

### 5.2 Scan I/O 收益

列存本身让 query 只读需要的列。

Encoding/compression 进一步让需要读的列更小。

```text
SELECT sum(revenue)
FROM fact
WHERE event_date >= '2026-01-01'

只读：
  revenue
  event_date

不读：
  user_agent
  json_payload
  comment
  device_info
```

Column pruning + encoding + compression 组合后，scan bytes 可以显著下降。

### 5.3 Memory Bandwidth 收益

现代 CPU 上，很多 OLAP query 不是 ALU bound，而是 memory bandwidth bound。

Encoding 后：

```text
int64 → bit-packed 12-bit
string → dictionary int32
timestamp → delta bit-packed
boolean → bitmap / RLE
```

这样每个 cache line 包含更多 logical values。

结果：

```text
更少 cache miss
更少 DRAM bandwidth
更高 SIMD lane utilization
更少 TLB pressure
```

这解释了为什么 DuckDB 文档会提到 on-disk compressed tables 有时比 in-memory uncompressed tables 更快。

### 5.4 CPU 执行收益

Encoding 可以把复杂操作变简单：

```text
string equality:
  strcmp("California", "California")

变成：

integer equality:
  code == 17
```

Group by：

```text
hash(string)
```

变成：

```text
hash(int32)
```

Join：

```text
probe string key
```

变成：

```text
probe dictionary id
```

StarRocks 的 low-cardinality dictionary rewrite 就是这种方向：优化器可以改写 join predicates 和 projections，以利用 dictionary-encoded string columns。

### 5.5 Operator Zero-copy 收益

Trino UNNEST 的例子最典型。

传统 UNNEST：

```text
复制输入元素
生成展开后的新 block
```

DictionaryBlock UNNEST：

```text
base vector 不动
只生成 indices
```

[Trino 的生产数据实验](https://trino.io/blog/2019/08/23/unnest-operator-performance-enhancements.html)报告查询最高约 9 倍加速、CPU 使用最多降低约 13 倍。具体收益依赖复制列宽度、嵌套结构和展开基数，不能直接外推到其他算子。

这说明 encoding 还可以作为中间结果表示，而不只是文件格式。

### 5.6 SerDe 收益

SerDe 是大数据系统的长期瓶颈。

典型浪费路径：

```text
Parquet dictionary encoded string
  ↓
decode 成 Java String / UTF-8
  ↓
再转 Arrow / UnsafeRow / ColumnVector
  ↓
shuffle 时再 serialize
  ↓
另一端再 deserialize
```

优化路径：

```text
Parquet dictionary encoded page
  ↓
Arrow DictionaryArray
  ↓
execution uses dictionary ids
  ↓
shuffle/exchange keeps encoded representation where possible
```

这里的收益并非来自更复杂的计算，而是来自少做了重复字符串分配、复制和解码；前文 Arrow Reader 案例给出了这种差异的量级。

DataFusion 文档也强调 Arrow 支持不同系统和语言之间 zero-copy interchange，避免 serialization overhead。

---

## 6. Encoding-aware Execution 的关键技术

### 6.1 Late Decoding / Late Materialization

核心原则：

```text
能不 decode 就不 decode
能晚 decode 就晚 decode
```

例如：

```text
SELECT count(*)
FROM t
WHERE country = 'US'
```

如果 `country` 是 dictionary encoded：

```text
dict:
0 -> CN
1 -> JP
2 -> US

predicate:
country = 'US'
  ↓
code = 2

scan codes:
[2, 0, 2, 1, 2]
```

无需把每个 code 解成 string。

Late decoding 对 string 列尤其重要。

### 6.2 Predicate Rewriting on Encoded Domain

对于 equality predicate：

```text
col = 'abc'
```

dictionary-aware rewrite：

```text
code = dict_lookup('abc')
```

对于 IN predicate：

```text
col IN ('US', 'CN', 'JP')
```

变成：

```text
code IN (2, 5, 7)
```

对于 sorted dictionary，如果 dictionary 保序，range predicate 也可以改写：

```text
col BETWEEN 'A' AND 'M'
```

变成：

```text
code BETWEEN low_code AND high_code
```

但大多数普通 dictionary 不保证 order-preserving，因此 range predicate 更复杂。

### 6.3 Dictionary Propagation through Join / Group By

如果两个表共享 global dictionary：

```text
fact.country_code_id
dim.country_code_id
```

join 可以直接在 int id 上做。

如果 dictionary 是 per-page/per-file local dictionary，则需要：

```text
local code → global code remap
```

这就是为什么 global dictionary 是很多系统的优化方向。

StarRocks 的 global low-cardinality dictionary optimization、ClickHouse LowCardinality global dictionary RFC、Parquet global dictionary research 都指向这个问题。StarRocks 文档已经显示其优化器能够利用 low-cardinality dictionary-encoded string columns 改写 join。

### 6.4 RLE-aware Aggregation

对于：

```text
values:
A x 1,000,000
B x 500,000
```

普通聚合：

```text
for row in rows:
    count[row.value] += 1
```

RLE-aware：

```text
count[A] += 1,000,000
count[B] += 500,000
```

这对 count、sum、min/max、group by 都可能有效。

### 6.5 ConstantVector Optimization

如果一个 vector 是 constant：

```text
col = 5 repeated 4096 rows
```

filter：

```text
col > 3
```

可以直接得出：

```text
全部通过
```

filter：

```text
col > 10
```

可以直接得出：

```text
全部不通过
```

ConstantVector 在 Velox、Presto/Trino、Arrow-like systems 中都非常常见。Velox 文档把 constant encoding 作为基本 vector encoding。

### 6.6 LazyVector / Lazy Materialization

Velox 支持 lazy materialization pattern。Meta 的 Velox 介绍明确说其 Vector 模块支持 lazy materialization。

LazyVector 的意义：

```text
先只保留数据源引用和 row ids
真正需要时再 load/decode
```

这对：

```text
filter 之后只剩少量 rows
join probe 之后只需要部分 payload
project 中某些 column 未被使用
```

非常有用。

### 6.7 DecodedVector 抽象

Velox 的 DecodedVector 很值得借鉴。

它让 operator 可以看到逻辑 flat view，但对 flat、constant、single-level dictionary inputs 保持 zero-copy。

这是工程上非常漂亮的折中：

```text
性能：
  避免不必要 decode/copy

工程复杂度：
  operator 不必手写所有 encoding path
```

---

## 7. Encoding Selection：如何根据数据分布选择？

### 7.1 选择 encoding 需要观察哪些统计信息？

一个成熟 encoding selector 至少需要以下统计：

```text
cardinality
run length distribution
min / max
delta distribution
null ratio
string length distribution
prefix/suffix similarity
sortedness / clustering degree
value skew
outlier ratio
update frequency
query pattern
random access vs scan
```

简单表：

| 数据分布 | 推荐 encoding |
| --- | --- |
| 全部值相同 | Constant |
| 连续重复值 | RLE / Run-End Encoding |
| 低基数字符串 | Dictionary / LowCardinality |
| 小整数范围 | Bit-packing / FOR |
| 单调整数 / timestamp | Delta / DoubleDelta |
| 时间序列 float | Gorilla / Chimp / Patas |
| 真实业务 double | ALP |
| 高基数字符串但子串重复 | FSST |
| prefix-heavy string | Delta Byte Array / front coding / FSST+ 类方向 |
| 大量 null | validity bitmap + RLE/REE |
| nested data | definition/repetition level encoding |

### 7.2 自动选择 vs 手动选择

工业系统大致分两派。

#### 自动选择派

代表：

```text
Snowflake
BigQuery
Redshift ENCODE AUTO
DuckDB auto compression
Doris writer auto picks encoding
```

Redshift 官方文档说明，ENCODE AUTO 默认自动管理列 compression encoding；`ANALYZE COMPRESSION` 可以基于 sample 给出建议。 Doris 文档也显示 writer 会根据列类型和值分布选择 encoding。

优点：

```text
用户负担低
适合云服务
避免错误配置
```

缺点：

```text
对特殊 workload 可能不最优
encoding choice 不透明
难以跨系统保持语义
```

#### 手动调优派

代表：

```text
ClickHouse
Redshift manual ENCODE
Vertica projection encoding
SingleStore 部分 schema-level encoding
```

ClickHouse 允许用户显式选择 codecs，并通过 LowCardinality 类型指定 dictionary-like storage。

优点：

```text
专家用户可极致优化
适合固定 workload
适合可控数据分布
```

缺点：

```text
需要理解分布
错误选择可能负优化
schema 演化后可能过期
```

### 7.3 为什么最优 encoding 不只取决于压缩率？

假设两个方案：

```text
A:
  压缩率 10:1
  decode 速度 1 GB/s

B:
  压缩率 5:1
  decode 速度 10 GB/s
```

如果 query 是 CPU-bound，B 可能更快。

如果 query 是 object-store I/O-bound，A 可能更省钱。

如果 column 是 sort key，过度压缩可能影响 range scan。Redshift 文档就特别说明，自动 compression 对 sort key columns 会避免过强压缩，因为 range-restricted scans 可能因此变差。

所以 encoding selector 的目标函数应该是：

```text
minimize:
  scan_time
  + decode_time
  + memory_bandwidth
  + network_shuffle_time
  + storage_cost
  + write_amplification
  + maintenance_cost
```

而不是只看：

```text
compressed_size
```

---

## 8. Column Encoding 在 SerDe / 网络 / Shuffle 中的价值

### 8.1 Parquet → Arrow：避免重复字符串 materialization

这是最典型的 SerDe 优化。

糟糕路径：

```text
Parquet dictionary:
  dict + codes

reader:
  decode into strings

execution:
  compare/hash strings
```

优化路径：

```text
Parquet dictionary:
  dict + codes

reader:
  Arrow DictionaryArray

execution:
  compare/hash integer codes
```

因此，SerDe 层如果丢失 Encoding，会让上游存储格式已经识别出的数据结构重新退化成重复值。

### 8.2 Exchange / Shuffle 中保留 dictionary/RLE

分布式引擎中，shuffle 是典型瓶颈：

```text
serialize
network transfer
deserialize
hash
partition
```

如果 string key 能变成 dictionary code：

```text
network bytes 少
hash 更快
deserialize 更快
CPU cache 更好
```

但问题是：

```text
不同 partition / page / file 的 dictionary code 不一定一致
```

所以需要：

```text
global dictionary
dictionary remapping
dictionary normalization
```

这也是 StarRocks、ClickHouse、Parquet global dictionaries 等方向的核心。

### 8.3 Intermediate Representation Encoding

Trino/Presto 的 DictionaryBlock 和 Velox 的 DictionaryVector 说明，encoding 还可以表示中间执行结果：

```text
filter result:
  base vector + selected indices

join output:
  probe/base vector + repeated indices

unnest result:
  base nested vector + expanded indices

projection:
  no-copy dictionary remap
```

这比单纯“压缩存储”更高级。

---

## 9. 当前主要挑战

### 9.1 Encoding-aware Operator 代码复杂

如果一个 operator 要支持：

```text
flat
constant
dictionary
RLE
nested dictionary
lazy
sequence
```

代码复杂度会显著增加。

Velox 的 DecodedVector 是一个解决方案：给 operator 一个统一逻辑视图，同时尽量保持 zero-copy。

但这仍然不是免费午餐：

```text
DecodedVector 自身有开销
多层 dictionary 可能需要 flatten
null semantics 更复杂
nested types 更复杂
```

### 9.2 Dictionary Locality 问题

Parquet dictionary 通常是 per column chunk / page 级别。

这对 scan 有利，但对 join/group by 不够好。

问题：

```text
file A:
  US -> 0
  CN -> 1

file B:
  CN -> 0
  US -> 1
```

如果不统一 dictionary，不能直接在 code 上 join/group by。

解决方向：

```text
global dictionary
per-partition dictionary normalization
runtime dictionary remapping
dictionary-aware hash table
```

### 9.3 高 cardinality 字符串

Dictionary encoding 在高 cardinality 下可能失败或 fallback。Parquet 官方文档说明，如果 dictionary 太大，会 fallback 到 plain encoding。

这时可以考虑：

```text
FSST
Delta Byte Array
Delta Length Byte Array
front coding
prefix compression
general compression + string view
```

NVIDIA 的指南也说明，distinct values 高时，delta 和 delta length encoding 对短字符串可能更优。

### 9.4 随机访问与 Seekability

很多 compression/encoding 对顺序 scan 友好，但对 point lookup 不友好。

例如：

```text
block-level LZ4:
  要读第 1 条，也可能要解整个 block

RLE:
  要定位第 N 行，需要 run index

dictionary:
  code 可随机访问，但变长 dict value 可能仍需 offset lookup
```

SingleStore 文档强调，它把 Dictionary/RLE/LZ4 等 string encoding 扩展为 seekable，以支持 point-access。

这对 HTAP、serving、point query 很重要。

### 9.5 Heavy Compression 可能让 Query CPU-bound

在对象存储和高速网络时代，瓶颈可能从 I/O 变成 decompression CPU。

BtrBlocks 论文直接指出，数据湖中开放格式如 Parquet 在远程访问和高吞吐网络下，低效 decompression 会让 scan CPU-bound，从而增加 query time 和 cost。

所以未来不是“压得越小越好”，而是：

```text
压缩率
decode speed
SIMD friendliness
random access
operator pushdown
object-store read amplification
```

一起优化。

### 9.6 更新、删除、Compaction 会破坏 encoding 效果

Column encoding 通常对 immutable segment / sorted block 最有效。

但真实系统有：

```text
append
update
delete
merge-on-read
compaction
schema evolution
late-arriving data
CDC
```

这些会导致：

```text
run 被打碎
min/max 变宽
dictionary 膨胀
delta 变大
clustering 变差
```

所以现代 OLAP 系统必须把 encoding 与 compaction、clustering、sort key、row group rewrite 结合。

### 9.7 Encoding 还是兼容性协议

Writer 能生成某种 Encoding，不等于所有 Reader 都能正确读取。Parquet 官方的[格式版本说明](https://parquet.apache.org/docs/file-format/versions/)特别区分两类演进：新增 Bloom 等可忽略元数据时，旧 Reader 仍可读取但性能退化；新增 `DELTA_*`、`BYTE_STREAM_SPLIT`、`RLE_DICTIONARY` 或 Data Page V2 等物理表示时，不支持它们的旧 Reader 可能直接无法解码。

因此开放格式中的 Encoding Selection 还要受 Compatibility Matrix 约束：

```text
candidate encoding
  ├─ writer 是否实现且稳定？
  ├─ 所有生产 reader 是否支持？
  ├─ reader 遇到未知 encoding 是报错还是错误 fallback？
  ├─ schema / logical type annotation 是否保持同一语义？
  └─ 回滚到旧版本时，已写文件是否仍可读？
```

这类问题不能只靠 `format_version=2` 推断。Parquet 文档明确指出，文件元数据中的版本字段历史上与实际 Feature 并非严格一一对应；可靠做法是按 Reader 实现和 Feature 建立能力清单，灰度写入新 Encoding，并在升级完成前保留可回滚的 Writer 策略。对 Lakehouse 而言，“压缩率更高但部分 Reader 不认识”首先是可用性事故，而不是性能优化。

---

## 10. 最新研究与发展趋势

### 10.1 Data Lake 格式正在重新思考 encoding

Parquet/ORC 是十多年前为 Hadoop 生态设计的。现在硬件变了：

```text
NVMe 更快
网络更快
对象存储成为主流
CPU decompression 成为瓶颈
SIMD 更强
GPU 参与分析
```

BtrBlocks、FastLanes 等研究都在重新审视 columnar storage format。BtrBlocks 认为现有开放格式不够适合 remote data lakes 和高吞吐网络；FastLanes 则试图对常见轻量压缩方案统一加速解码，并改善 Parquet 这类格式的压缩率和解码速度。

趋势：

```text
新格式不是只追求更高压缩率，
而是追求：
  vectorized decode
  SIMD-friendly
  random access
  nested data support
  open interoperability
  object-store efficient
```

### 10.2 String encoding 进入新阶段

FSST 已经从论文进入 DuckDB/CedarDB 等工程系统。DuckDB 支持 FSST，CedarDB 也公开介绍其使用 FSST 压缩 text columns，以减少存储并提升查询。

趋势：

```text
低基数字符串：
  dictionary

高基数字符串：
  FSST / prefix-aware / delta byte array / string view

执行层：
  尽量避免 materialize full string
```

### 10.3 Float encoding 变得越来越重要

ALP、Chimp、Patas 等进入数据库系统，说明浮点压缩已从 time-series 专用算法变成 general columnar engine 关注点。DuckDB 已经支持 ALP、Chimp、Patas。

未来原因：

```text
observability metrics
IoT time series
ML feature columns
embedding metadata
向量检索辅助属性
scientific data
```

都会让 float/double 列变多。

### 10.4 Encoding-aware Optimizer

未来 optimizer 不仅要知道：

```text
row count
NDV
min/max
histogram
```

还要知道：

```text
encoding type
decode cost
compressed size
dictionary size
run length distribution
bit width
selectivity after dictionary predicate
whether predicate can be evaluated on encoded domain
whether join keys share dictionary
```

可能出现新的 physical property：

```text
ColumnEncodingProperty:
  Flat
  Dictionary(local/global, ordered/unordered)
  RLE
  Constant
  FOR(bit_width)
  Delta
  FSST
```

这会影响：

```text
scan cost
filter cost
join key cost
group by cost
shuffle size
materialization decision
```

### 10.5 Global Dictionary 会在 Lakehouse 中重新变热

当前 Parquet dictionary 通常局部于 column chunk，不适合跨文件执行。

Lakehouse 场景中，事实表和维表可能存成大量 Parquet files。如果可以维护 global dictionary：

```text
customer_country:
  global code

product_category:
  global code

event_type:
  global code
```

则 join/group by/shuffle 都可以显著加速。

问题是：

```text
dictionary evolution
schema evolution
late-arriving values
multi-writer consistency
跨文件/跨分区 remap
delete/update
兼容 Parquet/Arrow/engine runtime
```

这是很有价值的研究和工程方向。

### 10.6 GPU-friendly Column Encoding

GPU 需要：

```text
coalesced memory access
fixed-width values
branchless decoding
massively parallel decompression
minimal pointer chasing
```

传统 LZ 类算法不一定适合 GPU。NVIDIA RAPIDS 在 Parquet encoding/compression 上已经提供可配置 dictionary、byte stream split、delta 等选项。

未来如果 GPU query engine 更普及，可能需要：

```text
GPU-native string encoding
GPU-native float encoding
warp-friendly dictionary lookup
GPU decompression + decode fusion
GPUDirect Storage + encoded column scan
```

### 10.7 Encoding 与 AI 数据系统结合

AI 时代新的数据类型正在进入数据库：

```text
embedding
token sequence
KV cache metadata
agent trace
prompt/response text
document chunks
JSON memory
observability metrics
```

这里的 encoding 机会非常多：

```text
embedding:
  float encoding / quantization / byte stream split / vector compression

token sequence:
  integer bit-packing / RLE / dictionary

agent trace:
  dictionary for tool names/status/model names
  FSST for prompt text
  RLE/REE for repeated states

observability:
  Gorilla / Chimp / ALP / Delta
```

Column encoding 会从传统 OLAP 扩展到 AI-native workload。

---

## 11. 对计算引擎设计的建议

### 11.1 把 encoding 作为 Physical Plan Property

建议在 Query Engine 中显式建模：

```text
ColumnBatch:
  type
  logical_type
  encoding
  null_encoding
  dictionary_info
  compressed_size
  decoded_size
  can_random_access
  can_filter_encoded
```

这样 optimizer 可以做：

```text
filter pushdown on dictionary
aggregation on RLE
join on global dictionary
late decode
decode placement decision
```

### 11.2 Scan Operator 不应总是输出 FlatVector

Scan 的输出应该允许：

```text
FlatVector
DictionaryVector
ConstantVector
RLEVector
LazyVector
RunEndVector
```

否则所有 encoding 的收益会在 scan 边界消失。

Velox、Trino、Arrow 的经验都说明，这是现代 vectorized engine 的关键。

### 11.3 Expression Engine 要支持 encoded fast path

例如：

```text
equals(dictionary_col, literal)
in(dictionary_col, literals)
is_null(RLE_nulls)
and/or on selection vectors
cast on dictionary base values
```

对于不支持的表达式，再 fallback 到 decode。

### 11.4 Join / Aggregate 应支持 dictionary id path

尤其是字符串 key：

```text
GROUP BY country
JOIN ON user_segment
COUNT DISTINCT event_type
```

如果能在 dictionary id 上执行，收益通常很大。

StarRocks 对 low-cardinality dictionary string join 的 rewrite 是一个很好的工业信号。

### 11.5 Encoding Selector 应同时考虑 workload

不要只基于数据分布选 encoding，还要基于查询：

```text
频繁 equality filter:
  dictionary 很好

频繁 range filter:
  ordered dictionary / delta / raw + minmax 可能更好

频繁 point lookup:
  seekable encoding 更重要

频繁 scan aggregate:
  RLE / FOR / bitpacking 更好

频繁 update:
  过度压缩会增加 rewrite 成本
```

### 11.6 SerDe 边界要保留 encoding

在以下边界都要尽量保留 encoding：

```text
Parquet → Arrow
Arrow → execution batch
operator → operator
shuffle send → receive
cache → scan
spill → restore
UDF boundary
Flight / IPC
```

否则会出现：

```text
storage encoded
  ↓
scan decode
  ↓
operator 再编码
  ↓
shuffle decode/encode
  ↓
receiver 再 decode
```

这种反复转换会吞掉很多收益。

---

## 12. 一个实用的 Encoding 选择矩阵

| 场景 | 数据特征 | 推荐 encoding | 执行优化 |
| --- | --- | --- | --- |
| 低基数字符串维度 | country、status、channel | Dictionary / LowCardinality | filter/join/group by on int code |
| 高基数字符串但有重复子串 | URL、路径、日志、JSON text | FSST / Delta Byte Array | late materialization，string view |
| 时间戳 | 单调、固定间隔 | Delta / DoubleDelta / bit-pack | range filter + min/max + SIMD decode |
| 指标 float | time-series gauge | Gorilla / Chimp / Patas | batch decode，predicate pushdown |
| 真实业务 double | 金额、价格、比率 | ALP | float → integer-like encoding |
| 小范围整数 | age、small id、bucket | FOR / bitpacking / T64 | SIMD compare |
| 连续重复值 | 排序后维度列 | RLE / REE | run-level filter/agg |
| 全部相同 | partition column / constant projection | Constant | metadata-only execution |
| 大量 null | sparse columns | validity bitmap + RLE/REE | null fast path |
| 嵌套数据 | array/map/struct | def/rep levels | nested vectorized decode |
| point lookup | serving/HTAP | seekable dictionary/RLE/LZ4 | random access fast path |

---

## 13. 最值得精读的论文与系统

### 学术基础

| 论文 | 价值 |
| --- | --- |
| [C-Store: A Column-oriented DBMS](https://www.vldb.org/archives/website/2005/program/paper/thu/p553-stonebraker.pdf) | 列存、Projection、排序和 Read-optimized Architecture 的基础。 |
| [Integrating Compression and Execution in Column-Oriented Database Systems](https://www.cs.umd.edu/~abadi/papers/abadisigmod06.pdf) | 压缩数据上执行查询的经典论文。 |
| [MonetDB/X100](https://www.cidrdb.org/cidr2005/papers/P19.pdf) | Vectorized Execution 与 CPU-aware Query Processing 的经典。 |
| [The Design and Implementation of Modern Column-Oriented Database Systems](https://stratos.seas.harvard.edu/files/stratos/files/columnstoresfntdbs.pdf) | Column Store 系统设计综述。 |
| [BitWeaving](https://pages.cs.wisc.edu/~jignesh/publ/bitweaving.pdf) | Bit-level Parallel Scan。 |
| [ByteSlice](https://www.cs.columbia.edu/~orestis/publications.html) | Byte-level SIMD-friendly Layout。 |
| [FSST](https://vldb.org/pvldb/vol13/p2649-boncz.pdf) | 轻量级字符串压缩与随机访问。 |
| [ALP](https://github.com/cwida/ALP) | 自适应无损浮点压缩。 |
| [Chimp](https://www.vldb.org/pvldb/vol15/p3058-liakos.pdf) / [Patas](https://github.com/andybaran/chimp) | 面向浮点时间序列的压缩。 |
| [BtrBlocks](https://www.cs.cit.tum.de/fileadmin/w00cfj/dis/papers/btrblocks.pdf) / [FastLanes](https://github.com/cwida/FastLanes) | Data Lake 时代的列式轻量压缩。 |

### 工业系统

| 系统 | 值得关注点 |
| --- | --- |
| [Parquet](https://parquet.apache.org/docs/file-format/data-pages/encodings/) | Encoding + Compression 的开放标准，Dictionary、RLE、Delta 和 Byte Stream Split。 |
| [ORC](https://orc.apache.org/specification/ORCv1/) | RLEv2、Stripe Statistics、Bloom Filter 和 Hive/Trino 生态。 |
| [Arrow](https://arrow.apache.org/docs/format/Columnar.html) | Zero-copy 内存列式格式、DictionaryArray 和 Run-End Encoding。 |
| [Velox](https://facebookincubator.github.io/velox/develop/vectors.html) | Vector Encoding 一等公民与 DecodedVector 抽象。 |
| [Trino/Presto](https://trino.io/blog/2019/08/23/unnest-operator-performance-enhancements.html) | DictionaryBlock/RLEBlock 贯穿执行以及 UNNEST 无复制优化。 |
| [DuckDB](https://duckdb.org/docs/stable/internals/storage.html) | 快速吸收 FSST、ALP、Chimp、Patas 等 Encoding 研究。 |
| [ClickHouse](https://clickhouse.com/resources/engineering/database-compression) | LowCardinality、Delta、DoubleDelta、Gorilla 和 T64 可配置。 |
| [StarRocks](https://docs.starrocks.io/docs/sql-reference/System_variable/#cbo_enable_low_cardinality_optimize_for_join) / [Doris](https://doris.apache.org/docs/dev/key-features/columnar-storage/) | 字典编码、Optimizer Rewrite 与基于分布的 Encoding。 |
| [Spark](https://spark.apache.org/docs/latest/sql-data-sources-parquet.html) / [Photon](https://docs.databricks.com/aws/en/compute/photon) | Vectorized Parquet Reader、Columnar Batch 与 Lakehouse 执行引擎。 |
| [BigQuery](https://cloud.google.com/bigquery/docs/storage_overview) / [Snowflake](https://docs.snowflake.com/en/user-guide/tables-clustering-micropartitions) | 自动列式压缩存储与 Metadata Pruning。 |
| [Redshift](https://docs.aws.amazon.com/redshift/latest/dg/c_Compression_encodings.html) / Vertica / [SingleStore](https://docs.singlestore.com/db/v9.0/create-a-database/columnstore/) | 自动或手动列编码、Seekable Encoding 和经典 MPP 工程经验。 |
| [RAPIDS cuDF](https://docs.rapids.ai/api/cudf/stable/user_guide/api_docs/api/cudf.dataframe.to_parquet/) | GPU Parquet Encoding/Compression，包括 Dictionary 与 Byte Stream Split。 |

---

## 14. 总结：Column Encoding 的本质

Column encoding 的本质不是“把数据压小”。

它真正解决的是：

```text
如何利用数据分布，
把逻辑数据转换成更接近硬件、更接近执行器、更接近网络传输的物理表示。
```

在现代计算引擎里，它同时影响：

```text
存储成本
I/O 成本
CPU decode 成本
memory bandwidth
cache locality
SIMD utilization
predicate evaluation
join/group by cost
shuffle bytes
serialization/deserialization
operator intermediate representation
```

如果用一句话总结：

**Column encoding 是现代 Query Engine 的物理代数之一。**

过去我们在 optimizer 里讨论：

```text
Scan
Filter
Project
Join
Aggregate
Exchange
Sort
```

未来还要讨论：

```text
Encode
Decode
RemapDictionary
PropagateDictionary
RunLengthAggregate
BitPackedFilter
LateMaterialize
GlobalDictJoin
```

### 14.1 我的思考：Encoding 是物理代数，也是正确性契约

把 Encoding 引入执行计划，不能只记录一个 `Dictionary` 或 `RLE` 标签。表示只有在特定作用域和语义下才成立：两个 Dictionary ID 相等，不代表它们来自不同字典时对应的逻辑值相等；有序字典可以支持范围比较，无序字典通常只能安全地做等值映射；浮点编码还必须保持 NaN、正负零和排序语义；嵌套类型则要同时维护 Null、Offset 与父子层级。

因此，一个真正可用的 Encoding Property 至少需要回答：

```text
Identity:
  这份 dictionary / base vector 是谁？

Scope:
  它在 page、row group、partition、file 还是 table 内有效？

Semantics:
  是否保序？是否保持 SQL equality、NULL 和 float 语义？

Capabilities:
  能否直接 filter、hash、group、join、seek 或 random access？

Transition:
  经过 Project、Filter、Exchange、Spill 和 UDF 后如何传播或失效？

Cost:
  保留、Remap、Decode 和重新 Encode 分别需要多少代价？
```

这使 Encoding 更像一种物理代数：算子不仅消费和产生 Row，也消费和产生带有表示属性的 Column。Optimizer 的职责不再只是选择 Join 顺序，还要选择何时保持结构、何时转换结构，以及转换发生在哪个成本最低的边界。

我认为下一代计算引擎最值得投入的方向，不是让所有算子理解所有 Encoding，而是建立三层能力：公共的逻辑访问抽象、少数高收益的 Encoded Fast Path，以及随时可以回退的正确 Decode Path。这样既不会因为追求统一而过早物化，也不会让算子代码陷入 Encoding 组合爆炸。

最终，这篇文章想强调的不是“压缩数据也能计算”，而是一个更一般的判断：**数据在进入系统时已经携带结构，好的执行引擎应尽可能利用并传播这些结构，而不是先把它们抹平，再付出代价重新发现。**

这也是下一代计算引擎值得深入投入的方向：**把 Encoding 从 Storage 层的隐式优化，提升为 Optimizer 和 Execution Engine 都能理解的一等物理属性。**
