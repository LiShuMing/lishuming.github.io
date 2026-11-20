---
title: "AI 时代的大数据列存基座：ORC、Parquet 与 Lance 的架构分野及演进趋势"
slug: "columnar-formats-orc-parquet-lance-in-ai-era"
date: 2026-11-17T23:20:00+08:00
lastmod: 2026-11-17T23:20:00+08:00
categories:
  - 数据库
  - AI Infra
tags:
  - Parquet
  - ORC
  - Lance
  - Vortex
  - 列式存储
  - Lakehouse
description: "从 200GbE 云网络与 GPUDirect Storage 带来的物理瓶颈倒挂出发，剖析 Apache ORC 2.2 与 Apache Parquet 2.11（Variant Shredding、CDC、FlatBuffers Footer 提案）的演进边界，深入拆解 Lance v2.2 的 MiniBlock/FullZip 与 Blob v2 内核，并研判 Vortex、Nimble、CMU F3 与 Paimon Blob 所代表的下一代格式趋势。"
draft: false
toc: true
math: true
---

把一台挂着 8 张 GPU、通过 **GPUDirect Storage（GDS）** 直连 NVMe 阵列的训练服务器接入数据湖时，硬件账面上可以跑出 `40–120+ GB/s` 的 DMA 显存直注带宽；即便是纯云端无本地缓存的场景，搭载 `io_uring` 下载器（如慕尼黑工大的 **AnyBlob**）的现代实例也能直接把 `100–200 Gbps` 对象存储网络打满。

然而，只要数据以默认参数写的 **Apache Parquet** 或 **ORC** 存放在下面，两类再普通不过的 AI/ML 作业就会立刻把整套昂贵硬件卡死在起跑线上：

- **场景 A（千亿级训练语料的随机采样与回表）**：推荐或表格深度学习任务需要从 10 亿行表里按 `row_id` 随机抽取 1,024 条样本组成一个 Mini-batch，或者在向量检索召回 Top-100 之后回表取原始文本与图片字节。由于 Parquet 默认把上万行乃至百万行压在一个个连续 Data Page 里，随机捞一行往往迫使客户端拉取并解压整个几十 KB 到几 MB 的压缩页，顺着 Dremel 层级逐行计数跳过前置记录，硬生生制造出 **10 倍到 100 倍的 I/O 与 CPU 解码放大**。
- **场景 B（上万列超宽特征表与热卡喂数）**：现代特征平台动辄维护 `5,000` 到 `20,000+` 列宽表。读取哪怕 20 列特征之前，单是反序列化几十 MB 的 Apache Thrift Footer 元数据，就要耗掉大量 CPU 周期；数据真正读上来之后，Snappy/ZSTD 字节流解压、变长偏移解析与字典查表把 CPU 核全部占满，本该直接走 PCIe DMA 灌进 GPU 显存的数据流，因为 GPU 根本没法直接消费嵌套压缩页而退化回缓慢的 CPU 中转链路。

十年前（2012–2014），**ORC** 与 **Parquet** 在“磁盘 `100 MB/s`、网络 `1 Gbps`、CPU 相对过剩、查询以顺序全列聚合为主”的物理假设下达到了工程巅峰（关于经典列存扫描解码墙的早期学术实验，亦可对照 [多核并行、主存连接与数据表示精读]({{< relref "2026-10-22-db-papers-parallel-joins-formats-and-networks.md" >}})）。今天，**物理瓶颈已经从 I/O 带宽 wall 彻底倒挂成了 CPU 解码 wall 与随机定位 wall**，文件格式本身也迎来了十年未见的剧烈重构。

![Parquet、Lance 与下一代列存格式的物理布局与硬件通路分野](/images/diagrams/db-columnar-formats-ai-era.svg)

---

## 一、传统双雄的分化与自救：ORC 2.2 与 Parquet 2.11+

在探讨新兴格式之前，先看清楚传统两强截至 2026 年在各自生态位上的真实演变。尽管两者同源列存时代，近三年的命运与发力方向已经大幅分化。

### 1.1 Apache ORC：精良的“专精守成派”

脱胎于 Apache Hive 生态的 **ORC**，凭借 Stripe 划分、内置每万行一组轻量级 Row-Index 索引与原生 Bloom Filter，在纯 Java 离线批处理时代一度拥有不输甚至优于 Parquet 的扫描裁剪与压缩率；它也是阿里云 MaxCompute **AliORC**（叠加 C++ Arrow 直读、异步预读与自适应字典编码改造）的技术母本。

在 **v2.0 到 v2.2.0** 的演进中，ORC 团队依然保持着扎实的内核打磨：
- **空间数据类型落地（v2.2.0）**：在 Java 与 C++ 双端库中正式加入原生 `Geometry` 与 `Geography` 空间类型规范及读写实现；
- **C++ 核心库现代化**：在 `SortedStringDictionary` 中用 Google `dense_hash_map` 取代 `std::unordered_map` 加速字典写入，开放无需加载全量 Row-Group 索引的 Stripe 级统计直读 API，并将构建链升级至 CMake 3.25+ 与 Meson（Java 侧同步对齐 Java 21/25）；
- **压缩精细化**：开放更细粒度的 ZSTD 压缩策略开关（`orc.compression.zstd.strategy`）。

但从生态结果看，**ORC 赢下了早期 Hive 工程细节，却彻底退出了开放湖仓与 AI 基座的主战场**。在 Apache Iceberg、Delta Lake、Apache Hudi 与 Apache Paimon 四大表格式，以及 PyArrow、DuckDB、Polars、DataFusion 等新兴 Python/Rust 执行生态中，Parquet 已经确立了事实上的默认标准。ORC 目前主要服务于存量 Hadoop/Hive 遗产平滑纳管以及特定云厂商的闭源定制链路，新建绿地系统很少再将其选为默认底层格式。

### 1.2 Apache Parquet：湖仓霸主的“补丁式自我革新”

作为今天大数据生态的绝对公约数，Parquet 在 2024–2026 年迎来了规范演化最密集的周期（以 **`parquet-format` 2.11.0** 及周边 Arrow/Java/Rust 实现升级为代表），集中回应了半结构化数据、浮点向量压缩、版本化去重与宽表元数据四类压力：

1. **`VARIANT` 逻辑类型与 Variant Shredding（半结构化性能革命）**：
   过去把 Agent 轨迹日志、API JSON 塞进数仓只有两条痛苦的路——存成原始 `STRING`（每次解析开销巨大且无法裁剪），或强制拆成刚性 `STRUCT`（上游多一个字段整张表 Schema 跟着炸）。`parquet-format` 2.11.0 正式将 **`VARIANT`** 纳入核心规范，并与 **Apache Iceberg v3**（通过 Table Format v3 协议原生声明）、Delta Lake 与 Spark 4.0+ 拉齐（低版本旧 Reader 碰到时可安全降级为底层 `BYTE_ARRAY` 读取）。
   `VARIANT` 采用分离 metadata 与 value 的紧凑二进制直查布局，而真正让查询提速一个数量级的是上层协同的 **Variant Shredding（碎裂列化）**：引擎把高频过滤或聚合的 JSON 字段自动抽离成强类型物理子列（`typed_value`、`object`、`array`），余下动态长尾字段留在残余二进制载荷里。于是 JSON 数据同时吃到了标准列存的 Min/Max 跳数裁剪、字典压缩和局部列投影（免读整条 JSON）。
2. **原生空间类型（`GEOMETRY` / `GEOGRAPHY`）**：在 2.11.0 同步升格为一等公民，不再依赖应用层手工打包 WKB 字节流。
3. **浮点编码进化（`BYTE_STREAM_SPLIT` 与 ALP 推进）**：传统字典编码与 RLE 对高熵大模型 Embedding（FP32/FP16/BF16）几乎零压缩效果。**`BYTE_STREAM_SPLIT`** 将浮点数的符号位、指数位和尾数位拆成独立字节流后再交给 ZSTD/Snappy，显著榨出高指数相似度下的压缩比；同时社区也在积极吸收 CWI 提出的 **ALP（Adaptive Lossless Floating-Point）** 等兼具极高解压速率与随机访问潜力的无损浮点编码。
4. **Content-Defined Chunking（CDC，内容定义分块）**：已经在 PyArrow / DataFusion 等写入器中落地，并在 **Hugging Face Xet** 等版本化 AI 数据存储中发挥核心作用。按固定行数切分 Parquet Data Page 时，只要在语料集开头插入几条新样本，后面所有 Page 的偏移边界全部整体错位，导致版本存储的块级去重彻底失效；CDC 改为根据列数据内容本身的滚动哈希特征动态锚定 Page 边界，局部增删之后后续 Page 边界能迅速重对齐，大幅削减多版本数据集的存储膨胀与拉取带宽。
5. **FlatBuffers Footer 重构提案**：针对万列超宽特征表，社区正积极推动用支持零拷贝按需偏移寻址的 **FlatBuffers** 替换或补充历史遗留的 Apache Thrift Footer，避免读取少量列前先花几十毫秒反序列化整棵元数据树。

### 1.3 “打补丁”仍跨不过去的三道结构墙

尽管 Parquet 的补丁生态极其繁荣，它的物理格式根基——**“大 Row Group + 整块黑盒压缩 Page + 页内交织的 Dremel R/D Level”**——决定了三件事它几乎无法在不破坏兼容性的前提下彻底解决：
- 只要随机读取单行，就必须承担 Page 级读取和解压放大；
- 把几 MB 的图片、音视频切片硬塞进 `BYTE_ARRAY` 列，会撑爆 Row Group 内存缓冲且无法像文件系统那样按字节偏移做部分 Seek；
- CPU 导向的页内嵌套解压流水线与 GPUDirect Storage（NVMe 直通显存）存在根本性的硬件阻抗失配。

---

## 二、当代 AI 原生格式代表：Lance（v2.0 \(\rightarrow\) v2.2）内核拆解

正是瞄准了 Parquet 修修补补也跨不过去的这三道墙，**Lance**（由 LanceDB 团队主导的开源列存湖仓格式）在 v2.0 至 **v2.2（`data_storage_version="2.2"`）** 的演化中，针对“随机采样 + 全表扫描 + 多模态 Blob + 向量/标量/倒排索引”的混合负载，对底层物理页做了彻底重设计。

### 2.1 模块化结构编码：`MiniBlock` 与 `FullZip` 的双模权衡

在传统 Parquet 中，“这一列是否可空、嵌套有几层”（结构信息）和“整数怎么位打包、字符串怎么压缩”（值压缩信息）被捆死在同一个 Page 编码流水线里。Lance v2.x 做了一次彻底的正交拆分——将 **Structural Encoding（结构编码）** 独立出来作为可插拔的物理映射层，用来规定 Arrow 数组的 Validity Bitmap、List Offsets 和 Repetition/Definition Levels 如何落盘，并据此提供两套互补布局：

- **`MiniBlock`（面向小数据类型与高压缩比）**：
  用于整型、浮点、布尔与短字符串。它将一页切分成一系列极小的自包含 MiniBlock（每个块包含 2 的幂次条记录，压缩体积严格压在 **\(32\text{ KiB}\) 以内**），并在页头存放每块仅占 2 字节的轻量长度/行数查找表（初始化时常驻内存查找缓存）。这样既能保留高效率的通用块压缩，又把随机单行定位时的读放大与 CPU 解压惩罚死死锁在不到 32 KiB 的微窗口里。
- **`FullZip`（面向变长高维载荷与零读放大）**：
  用于超过阈值（如 \(\ge 32\text{ KiB}\)）的长文本、高维稠密向量与大型二进制对象。`FullZip` 采用**逐值（Per-Value）紧致编码与打包**——每个值独立编码、前置控制字并配有精确偏移定位指针。随机访问第 \(i\) 行时，Reader 直接按偏移跳过去只解码第 \(i\) 个值，**完全不会被迫解压前后相邻的第 \(i-1\) 或 \(i+1\) 行**。

### 2.2 Repetition Index：把嵌套随机取行压到 1–2 次 IOPS

在 Parquet 中处理 `List<Struct<...>>` 等嵌套列时，Dremel R/D Level 被压缩进页内字节流，想精准找到第 10,000 条逻辑记录的起始偏移，必须从页头开始一路解码计数过去。Lance 则把位打包后的 R/D Level 与控制字分离出来，构建专用的 **Repetition Index（重复索引）**。无论嵌套深度有多深，给定行号（或向量索引检索返回的 Row Address），Reader 都能独立于嵌套层级、稳定在 **1 到 2 次 IOPS** 内直达目标物理字节区间。

早期版本的 Lance 为了换取这种 \(O(1)\) 随机读取能力，在纯文本列上的压缩率曾经明显落后于 Parquet；**Lance v2.2** 引入了更激进的压缩编码器组合并完善了嵌套字段演化与原生 Map 支持。根据 LanceDB 官方发布的 v2.2 基准对照：它在文本密集型数据集上可将体积压缩至同等 Parquet 的约一半，全表顺序扫描追平乃至反超 Parquet，而**随机行读取与多模态 Blob 检索则维持比 Parquet 快 68 倍到 100 倍的优势**。

### 2.3 Blob v2：把多模态大对象变成湖仓的一等公民

多模态大模型预处理团队最怕的一件事，是用“Parquet 存 S3 路径字符串 + 对象存储散放几千万张小图”：每次列目录被对象存储限流，版本回滚时又因为元数据和大文件分离而对不上账。

Lance v2.2 引入的 **Blob v2** 直接把非结构化大对象纳为列存表内的一等公民，依据实际字节分布自适应四种存储语义——**Inline**（极小载荷内联）、**Packed**（中等图片/音频自动聚合成大块，消灭 S3 小文件灾难）、**Dedicated**（独立大对象区）和 **External**（外部挂载引用）。同时提供两条读取路径：分布式批处理喂数走高吞吐 **`read_blobs`**；视频抽帧、大模型权重按切片拉取等局部读场景则走 **`take_blobs`**，返回支持 `seek()` 的懒加载 **`BlobFile`** 句柄，避免把几 GB 的视频整条拖进主机内存。

---

## 三、2025–2026 下一代格式浪潮：五大不可逆转的技术趋势

如果把视野从 Lance 再放大一圈，结合过去两年 SIGMOD、VLDB 与 CIDR 上最活跃的系统工程成果——**Vortex**、**Meta Nimble**、**BtrBlocks / FastLanes**、**CMU F3** 以及湖格式 **Apache Paimon Blob**——可以清晰地看到大数据基座 File Format 正在向五个方向集体迁徙：

| 格式 / 项目 | 主导方 / 学术出处 | 瞄准的核心痛点 | 关键技术解法 |
| :--- | :--- | :--- | :--- |
| **BtrBlocks & FastLanes/ALP** | TUM (SIGMOD 2023) & CWI (VLDB 2023 / SIGMOD 2024) | 200GbE 云网络下 ZSTD/Snappy 解压耗尽 CPU | 放弃黑盒通用压缩，改用采样驱动的**级联轻量编码**；SIMD 转置布局实现超千亿整数/秒解码与 ALP 浮点压缩 |
| **Vortex** | SpiralDB \(\rightarrow\) LF AI & Data 孵化 | S3-to-GPU 带宽浪费与编码扩展封闭 | 定位“文件格式界的 LLVM”；**压缩态直接过滤/投影计算（Late Decompression）**；实测 60 Gbps S3 直注 GPU |
| **Nimble（原 Alpha）** | Meta 开源 | ML 推荐系统 10,000+ 列超宽表与稀疏 Map | FlatBuffers 零拷贝元数据 + SIMD/GPU 友好的轻量块流式布局 |
| **F3** | CMU + 清华 + Wes McKinney (CIDR 2025 / SIGMOD 2025–2026) | 新编码落地需等全生态升级 3–5 年的“格式僵化症” | FlatBuffers 元数据内直接嵌**WebAssembly (Wasm) 解码器模块**，老引擎沙箱安全自举读取未知新编码 |
| **Paimon Blob Storage** | Apache Paimon (Data Evolution) | 既想保留 Spark/Trino/Parquet 生态，又想纳管多模态大对象 | **合表分文件**：标量列存 `.parquet`/`.orc`，`BLOB` 列分流至随机读优化的 `.blob` 文件 + DiskANN 向量索引 |

把这几股力量放在一起看，未来两到三年的演进逻辑其实非常集中：

### 趋势一：告别黑盒字节流压缩，转向“级联轻量编码 + 压缩态直接算”
当网络和 SSD 带宽突破数十 GB/s，先把数据用 ZSTD 压紧、读出来再花巨量 CPU 核全部解压成原始数组，已经成了算力负资产。以 **BtrBlocks**、**FastLanes** 和 **Vortex** 为代表的新代格式普遍通过对样本数据采样，动态组合 `Dictionary -> Frame-of-Reference -> Bit-Packing` 或 `ALP` 等轻量级编码链。因为数据保持着结构化的编码语义而非一团黑盒字节流，执行引擎可以直接在未解压状态做谓词过滤和位图裁剪（Late Decompression），甚至让硬件通道直接把轻编码块流式推进 GPU 显存。

### 趋势二：元数据彻底零拷贝化（Thrift \(\rightarrow\) FlatBuffers）
无论是 Meta **Nimble**、**CMU F3**，还是 **Parquet** 社区正在推动的新 Footer 提案，共识高度一致：**超宽表时代绝不能再全量反序列化堆对象**。FlatBuffers 支持在内存映射或单次 Range Read 之后，按指针偏移在 \(O(1)\) 时间内精准抓出查询真正投影的那几列元数据，让万列表的首字节延迟从数十毫秒压回亚毫秒。

### 趋势三：用 Wasm 打破“格式升级死锁”（F3 的自描述思路）
传统文件格式之所以进化缓慢，是因为任何一次新编码提案都要经历漫长的规范投票、Java/C++/Rust/Python 四套库重写和下游数仓升级窗口。**F3** 把 Wasm 字节码作为便携式兜底解码器嵌入文件元数据，相当于给数据格式装上了“自带驱动的即插即用机制”——常用编码走本地原生 SIMD 编译核跑满性能，长尾创新编码走 Wasm 沙箱确保 100% 可读性，把格式创新周期从“以五年计”拉短到“随时发布”。

### 趋势四：逻辑一统、物理分流——结构化与 Blob 的“合表分文件”
从 **Lance Blob v2** 到 **Apache Paimon Blob Storage**，再到 **MaxCompute Object Table**，整个行业已经达成共识：**用同一个事务快照管住标量和多模态大对象（实现 ACID 回滚与血缘一致），但在物理文件层坚决分道扬镳**。窄小标量继续享受极致的向量化列存编码，而几 MB 到几 GB 的图/音/视频则交给独立打包、支持偏移 `seek()` 的 Blob 存储布局，互不拖累彼此的缓存行与缓冲区。

### 趋势五：Table Format 与 File Format 从“二选一”走向“可插拔混编”
许多技术选型讨论容易把 **Iceberg / Paimon** 和 **Parquet / Lance / Vortex** 放在对立面上。实际上，**Table Format（表格式）**解决的是跨多文件的事务快照、Time Travel、Deletion Vectors（用行级删除位图免去全文件重写）与多引擎 Catalog 治理；**File Format（文件格式）**解决的是单文件内部的物理编码、硬件亲和与局部寻址。2025–2026 年最值得关注的架构动向，正是 Iceberg、Paimon 等开放表格式开始接纳可插拔底层物理载体——在同一套资产元数据控制面下，分析型温冷分区继续使用 Parquet，极热 GPU 训练和特征随机回表分区则无缝挂载 Vortex 或 Blob/Lance 文件。

---

## 四、资深专家实战选型矩阵

回到实际生产落地，没有任何一种格式可以在所有维度上通吃。结合 2026 年的生态成熟度与工程代价，选型边界可以划得非常明确：

1. **企业通用 BI、多引擎 OLAP 湖仓共享（Spark / Trino / StarRocks / Doris / Snowflake / DuckDB）**
   **首选 Apache Parquet**。尽量开启 **Iceberg v3 + Parquet 2.11+ `VARIANT` Shredding** 接管半结构化日志与 Agent Trace；对向量/浮点列启用 **`BYTE_STREAM_SPLIT`**；配合 Paimon Deletion Vectors 或 Iceberg 删除向量避免小更新引发的大文件重写；版本化高频微改语料可评估开启 Arrow CDC 分块。
2. **存量 Hadoop/Hive 资产或深度绑定单一闭源数仓内部表**
   存量 **ORC / AliORC** 保持原地纳管即可，不必为换格式付出高昂洗数开销；面向跨引擎开放的新增数仓表应统一转向 Parquet。
3. **多模态大模型语料库、自动驾驶/具身智能非结构化流水线、向量检索与随机特征回表**
   **首选 Lance（`data_storage_version="2.2"`）**，或者采用 **Apache Paimon（标准列走 Parquet + 非结构化列走 Paimon `.blob` + DiskANN）**。前者在随机取行（1–2 IOPS）、Blob 懒加载与 PyTorch/Ray/向量混合检索一体化的开箱体验上最具优势；后者适合已经重度使用 Flink 流批一体入湖的团队。
4. **S3 直喂 GPU 集群训练（GPUDirect Storage / 200GbE 带宽墙）或万列级超宽稀疏特征表**
   值得重点测试引入 **Vortex**（追求压缩态直接过滤计算与高吞吐 S3-to-GPU 流式传输）或 **Meta Nimble**（超宽表元数据优化）。若受限于合规要求必须保留标准 Parquet，务必调小 Row Group、强制生成 Page Index 并结合本地 NVMe 缓存层兜底，同时密切跟进社区 FlatBuffers Footer 与 ALP 的合入进展。

---

> **参考资料与公开信息基线**
> - **Apache Parquet & Iceberg 演进**：`apache/parquet-format` 2.11.0 规范（`VARIANT` 逻辑类型与 Shredding、`GEOMETRY`/`GEOGRAPHY`、`BYTE_STREAM_SPLIT`）· Apache Arrow PyArrow Content-Defined Chunking (CDC) 文档与 Hugging Face Xet 技术博客 · Parquet 社区 FlatBuffers Footer 讨论提案 · Apache Iceberg v3 规范（Variant 支持与 Deletion Vectors）。
> - **Apache ORC 演进**：`apache/orc` 2.0 / 2.1 / 2.2.0 发布说明（`Geometry`/`Geography` 支持、C++ `SortedStringDictionary` `dense_hash_map` 优化、Stripe Statistics API 与 ZSTD 策略）。
> - **Lance 与多模态湖格式**：Lance 官方格式规范与 LanceDB 工程发布（*Lance File Format v2 / v2.1 / v2.2*、`MiniBlock` / `FullZip` 结构编码、Repetition Index、Blob v2 语义及与 Parquet 基准对比）· Apache Paimon 官方 Blob Storage 与 Data Evolution 文档。
> - **下一代列存前沿研究与项目**：*BtrBlocks: Efficient Columnar Compression for Data Lakes* (SIGMOD 2023, TUM) · *FastLanes* & *ALP: Adaptive Lossless Floating-Point Compression* (VLDB 2023 / SIGMOD 2024, CWI) · *AnyBlob: High-Throughput Cloud Object Storage Downloading* (TUM) · *F3: The Open-Source Data File Format for the Future* & *Towards Functional Decomposition of Storage Formats* (CIDR 2025 / SIGMOD 2025–2026, CMU / 清华大学) · **Vortex** (LF AI & Data / SpiralDB) · **Nimble** (Meta)。
