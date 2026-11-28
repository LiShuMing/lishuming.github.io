---
title: "当 SQL 引擎遇上大模型：从黑盒 UDF 到语义融合内核的架构推演"
slug: "sql-llm-fused-engine-architecture"
aliases:
  - "/2026/11/22/sql-llm-operator-fusion-and-seven-stage-ai-data-pipeline/"
date: 2026-11-22T12:20:00+08:00
lastmod: 2026-11-28
categories:
  - AI 基础设施
  - 数据库
tags:
  - SQL
  - LLM
  - 语义算子
  - 查询优化器
  - 多模态
  - 向量检索
  - KV Cache
  - Data Infra
description: "从千万级非结构化文档清洗与知识入库的真实瓶颈出发，沿着“类型与逻辑代数 → 语义代价优化与 PrefixLocality 物理属性 → 异构异步执行与推理协同 → 开放表格式增量闭环”的推演路径，拆解 SQL 引擎与 LLM 运行时深度融合的内核设计。"
draft: false
toc: true
math: true
---

假设你手里有 1,000 万份企业合同 PDF、扫描票据和原始业务日志，散落在对象存储的不同目录里。业务方提了一个在今天再普通不过的需求：把它们清洗成一份干净、无重复、不含敏感隐私（PII）、抽取出关键条款与风险等级的知识库，并且能同时支撑关键字与高维向量混合检索；之后每周新增几万份文件时，整套结果还要能低成本增量更新。

如果按过去最顺手的工程习惯去搭这套系统，大多数团队都会先后走过两条弯路——而这两条弯路撞上的墙，恰好把下一代 **SQL × LLM 融合引擎** 必须长成的样子逼了出来。

---

## 一、两条朴素路线的尽头：为什么“胶水拼接”与“黑盒 UDF”都会撞墙？

第一种做法是**按工序拼装独立工具**：写 Python 脚本从对象存储拉文件，交给文档解析库转成文本，落盘后再起 Spark 作业做词面去重，把向量抽出来丢进独立脚本跑聚类和成对比对，随后导入外部向量库，最后再起一批 Worker 调 vLLM 做结构化抽取。

管线跑在小样本上似乎一切正常，一旦数据量拉到千万级，四堵物理墙会同时压下来：
1. **跨系统序列化与反复落盘**：每跨过一道工具边界，整批数据就要经历一次编码、网络搬运和对象存储落盘，I/O 与序列化成本很快盖过有效计算；
2. **非结构化内存放大**：一份 20 MB 的扫描 PDF 在解码、渲染和切块后，内存中很容易膨胀到 300–400 MB，只要上游拉取速度和下游处理速度失配，Worker 瞬间 OOM；
3. **推理缓存被彻底打碎**：上游按存储分片乱序吐数据，共享同一长模板或同一主题背景的文档被拆散到不同批次、不同 GPU 节点，推理引擎的 Prefix KV-Cache 命中率跌至谷底，大量算力白烧在重复 Prefill 上；
4. **长作业容错成本失控**：GPU Spot 节点一旦被抢占，传统脚本或血缘重算机制会把几小时烧掉的 LLM Token 账单推倒重来。

为了消灭跨系统胶水，团队往往迅速转向第二种做法：**把解析、去重、向量化和模型调用全部包成 Python UDF，塞进现有的分布式 SQL 引擎里一锅端**。

表面上看，入口统一成了 SQL；可一到真实生产查询，优化器立刻变成“瞎子”。在传统关系数据库眼里，每个黑盒 UDF 都只是一段不透明的函数调用——它看不见 `file_path` 背后到底指着 10 KB 的纯文本还是 2 GB 的视频，看不见一次正则表达式只需 5 微秒、而一次 70B 大模型抽取要花 300 毫秒且按 Token 计费，也看不见相邻两个 UDF 其实都在反复读同一篇 10K Token 的长文档。结果引擎既不敢把廉价过滤提前、也做不了批处理重排，GPU 一边空等 I/O，一边为大量本该被挡在门外的脏数据支付昂贵算力。

走到这里，真正的核心问题才浮出水面：**传统数据库面对千变万化的业务需求，从来不是靠堆砌专用工具或黑盒函数，而是靠极少数正交的内核原语（数据表示 → 代数算子 → 代价优化 → 物理执行 → 事务存储），让系统自己把声明式意图编译成高效计划。** 如果沿着同一条数据库内核的演进思路反推，要让 SQL 引擎真正“看懂”并原生驾驭多模态与大模型负载，每一层究竟缺了哪块拼图？

---

## 二、第一块拼图：让引擎先“看见”大对象与语义计算

要让优化器睁开眼睛，第一步不是在 SQL 里堆语法糖，而是补齐**数据表示**与**逻辑代数**两处最基础的盲区：先让引擎理解“非结构化大对象”，再把看似五花八门的 AI 处理步骤压缩成极少数可优化的关系算子。

### 2.1 用惰性 `BlobRef` 打破“内联炸列存、裸路径丢治理”的死结

面对几十 MB 的 PDF、音频或视频，数据库一直卡在两难之间：如果直接当成 `BINARY` 列内联塞进 Parquet 或数仓数据页，常规标量列的压缩比、缓存行局部性与扫描吞吐会瞬间崩盘；可如果表里只留一串裸 URI 字符串（`'s3://bucket/a.pdf'`），SQL 就彻底失去了版本快照、行级权限控制，优化器更不知道这条路径背后藏着多大的文件。

破局点在于把大对象指针提升为类型系统里的一等公民——**惰性大对象引用类型 `BlobRef`**（工业界对应的落地形态包括 BigQuery `ObjectRef`、Snowflake `FILE`、Lance `Blob v2` 与 Apache Paimon `.blob`）。主表列存页里只紧致存放 `(uri, offset, length, etag, mime_type)` 等几十字节的轻量描述符和常规标量列，原始字节流则通过物理分流独立存放、保持惰性。

这一步看似只是多了一个类型，带来的连锁反应却直接改写了扫描层（`TableScan`）：
1. **元数据谓词零代价下推**：像 `WHERE size_bytes BETWEEN 1024 AND 52428800 AND updated_at >= ...` 这样的条件，在列存扫描阶段就靠 ZoneMap 和分区裁剪把绝大部分无效文件直接拦掉，连一次对象存储 GET 请求都不用发；
2. **字节加权分片与延迟物化**：只要后续算子没触碰文件内容，字节流就永远不被加载；一旦真正消费 `BlobRef`，调度器依据描述符里的 `length` 按**字节总量**而非行数切分 Split（避免单个 2 GB 视频把某个 Worker 拖成慢节点），底层的异步对象 I/O 池再自动执行 **Range Coalescing**（合并小于 1 MiB 的相邻碎区间以减少 HTTP 往返）与分片预取。

### 2.2 把纷繁的 AI 工序抽象为四类正交关系算子

解决了“数据怎么进表”，紧接着面对的第二个诱惑是：既然 AI 数据管线里既有 PDF 解析、敏感词脱敏，又有 MinHash 模糊去重、KMeans 向量聚类、成对语义比对、向量混合检索和 LLM 抽取，是不是该给每种算法都在内核里造一套独立阶段？

答案是否定的。业务算法会年年翻新，今天加一项版权检测，明天换一种聚类变体，如果把算法名写死在引擎骨架里，内核很快就会僵化。跳出具体算法名，从**数据形态与计算复杂度**切入，这堆看似迥异的操作其实全部收敛到四类正交的关系代数扩展算子，且每个算子都向优化器暴露统一的**六元组契约**（输入/输出强类型 Arrow Schema、算力域、单行 Cost、预估 Selectivity、语义精度约束）：

1. **异步语义变换与过滤：`AsyncMap` / `SemProject` / `SemFilter`**
   不管是 PDF 版面解析、SIMD 正则 + 轻量 NER 两级脱敏、Embedding 编码，还是大模型分类与字段抽取（`AI_CLASSIFY`、`AI_EXTRACT`、`AI_FILTER`），在逻辑计划眼里都是同一回事——把输入列异步映射成受 Schema 约束的输出列。唯一差异在于后端绑定的物理执行内核不同（Rust 原生算子、独立解码进程池、本地 GPU 模型或远端 vLLM 服务）。
2. **语义分区：`SemanticPartition`**
   传统分布式 SQL 靠 `HashPartition(key)` 把精确等值的数据路由到同一节点；到了非精确语义世界，MinHash 的 LSH Band 和 KMeans 的聚类质心干的其实是同一件事——**给每行打上语义分区键 `bucket_id` / `cluster_id`**，把全局 \(O(N^2)\) 的语义比较锁死在局部簇内。更妙的是，KMeans 产出的质心与簇归属能够“一鱼三吃”：下游同时复用它做簇内去重 Blocking、多样性分层采样（如 FineWeb-2 / D4 策略），以及直接充当 IVF 向量索引的底层倒排桶。
3. **近似相似度连接：`SimilarityJoin`**
   离线的大规模成对语义比对（SemDeDup 去重、实体消歧、RLHF 偏好对构造）与在线的高维向量检索（ANN Top-K），表面上分属离线批处理与在线查询两个世界，在关系代数里却是同一个算子：前者是表与自己的自连接，后者是 Query 表与 Base 表的索引连接。一旦在代数层统一，优化器就能像在 `Hash Join` 与 `Index Nested-Loop Join` 之间选型一样自主拍板：
   - 面对离线大表自连接，先借 `SemanticPartition` 做 Blocking（并靠 Balanced KMeans 或单簇上限 `m_max` 拦截超级热点簇），再在簇内把 \(m\) 条 L2 归一化向量拼成矩阵 \(E \in \mathbb{R}^{m \times d}\)，用单次 GPU FP16/BF16 矩乘 \(E \cdot E^T\) 替代双层循环，仅把落入模糊灰区的候选对送给 LLM 裁判；
   - 面对带结构化过滤（如 `tenant_id`、时间窗口）的在线召回，则根据过滤选择率在 **Pre-filtering**（低选择率先走倒排/B-Tree 再精确算距离）与 **Iterative Filtering**（高选择率边遍历 HNSW/DiskANN/IVF-PQ 图边验位图掩码）之间动态切换，并与 `BM25` 通过 RRF（倒数排名融合）在同一执行计划内合流。
4. **语义归约与迭代不动点：`SemAgg` / `IterativeFixpoint`**
   MinHash 分桶后需要把跨桶重复文档合并成连通分量——为避免单个大桶内 \(O(k^2)\) 建边爆炸，通常以桶内最小 `doc_id` 构造 \(O(k)\) 星型边，再跑交替大星-小星 WCC；这在关系代数里就是标准的迭代不动点算子。而面对超长上下文的跨行语义汇总（如 Snowflake `AI_AGG`），则是带窗口预算与 Tree-Reduce 分治归约的 `SemAgg`。

有了 `BlobRef` 和这四类算子，开篇那个需要七八套脚本硬拼的企业合同知识库构建任务，就能收束成一条完全声明式、且对优化器透明的 SQL：

```sql
WITH raw_docs AS (
  SELECT
    doc_id, tenant_id, file_ref,
    -- 统一建模为 AsyncMap：布局解析 + SIMD 正则与轻量 NER 分级脱敏
    PII_REDACT(AI_PARSE_DOCUMENT(file_ref, mode => 'LAYOUT')) AS clean_text
  FROM lakehouse.raw_pdfs_table
  WHERE size_bytes BETWEEN 1024 AND 52428800
    AND updated_at >= CURRENT_DATE - INTERVAL '30' DAY
),
dedup_and_clustered AS (
  -- 词面近似去重（SemanticPartition(LSH) + IterativeFixpoint WCC）
  -- 与语义聚类（AI_EMBED + SemanticPartition(KMeans)）统一为关系算子
  SELECT
    doc_id, tenant_id, clean_text, emb,
    SEMANTIC_PARTITION(emb, method => 'KMEANS', model => 'corpus_50k') AS cluster_id
  FROM (
    SELECT doc_id, tenant_id, clean_text, AI_EMBED(clean_text, model => 'bge-m3') AS emb
    FROM MINHASH_DEDUP(TABLE raw_docs, text_col => 'clean_text', threshold => 0.8)
  )
),
curated AS (
  -- SimilarityJoin：簇内 GPU GEMM 精确余弦去重，规避全局 O(N^2)
  SELECT doc_id, tenant_id, clean_text, emb, cluster_id
  FROM SIMILARITY_DEDUP(
    TABLE dedup_and_clustered,
    partition_by => 'cluster_id', vector_col => 'emb', cosine_threshold => 0.95
  )
)
INSERT INTO lakehouse.curated_knowledge_base
SELECT
  doc_id, tenant_id, clean_text, emb,
  -- 同一文档上的过滤与抽取可评估融合方案，质量与代价见第 3 节
  AI_EXTRACT(
    clean_text,
    schema => 'STRUCT<summary STRING, risk_level STRING, entities ARRAY<STRING>>',
    quality_target => 0.95
  ) AS structured_meta
FROM curated
WHERE AI_FILTER(clean_text, 'contains actionable compliance or contract obligations');
```

---

## 三、合同清洗管线里，哪些步骤可以换序？ {#三第二块拼图成本相差五个数量级优化器如何自动排兵布阵}

同一份合同先查文件大小，再查更新时间，调换这两步仍会留下相同的文件。脱敏和去重就不同了。假设两份合同只有签字人的姓名不同，脱敏把两个姓名都换成同一个占位符。按全文精确去重，先脱敏会把两份合同合成一份，先去重则会留下两份。业务要求在脱敏文本上判重，这个先后关系就已经写进了查询语义。优化器可以在其余合法的顺序里比较成本。

### 3.1 算一次过滤顺序的账 {#31-从三维帕累托代价里长出漏斗顺序}

假设有 100 份合同，大小检查 A 每份花 1 个成本单位，99 份通过。日期检查 B 每份花 5 个单位，10 份通过。两项检查只读同一份元数据，结果确定，逐份执行，成本固定。这组数字用来手算两种顺序。

| 执行顺序 | 第一遍检查 | 第二遍检查 | 总成本 |
| --- | ---: | ---: | ---: |
| A → B | 100 × 1 | 99 × 5 | 595 |
| B → A | 100 × 5 | 10 × 1 | 510 |

B 每查一份贵五倍，却能让 90 份合同跳过 A。把账摊回每份输入，两个顺序分别花 5.95 和 5.10 个单位。记每次检查的成本为 \(C_i\)，在这批输入上的通过率为 \(S_i\)。A 排在前面更省或持平的条件是

\[
C_A + S_A C_B \le C_B + S_B C_A
\quad\Longleftrightarrow\quad
C_A(1-S_B) \le C_B(1-S_A).
\]

两者都能淘汰一部分输入时，可以按 \(C_i/(1-S_i)\) 从小到大排。这个量是平均淘汰一行要付的检查成本。若一项检查让所有行通过，它排在前面就省不掉后续工作。比较这两个相邻过滤器，只需知道它们在**共同输入**上的通过率。前面若再加一道租户过滤，所选租户可能恰好全是近期合同，这时就得在该租户的数据上重估日期检查的通过率。[Hellerstein 与 Stonebraker，Predicate Migration](https://15721.courses.cs.cmu.edu/spring2019/papers/23-optimizer2/hellerstein-sigmod1993.pdf)

到了模型调用这一步，文档长短、批大小和前缀缓存都会改变单行成本，换模型还可能漏掉条款。Palimpzest 的房源查询里，地址和价格可以从文字里读到。先检查这两项，只有通过的房源才需要调用视觉模型判断房间的采光。作者在样本上收集计划统计量，用运行时间、费用和标注结果上的 F1 比较候选方案。迁移到合同管线，可以用同一批已标注合同估计通过率、记录推理账单，再检查每种方案漏掉了哪些条款。[Palimpzest，CIDR 2025，图 3](https://www.vldb.org/cidrdb/papers/2025/p12-liu.pdf)

### 3.2 换一种模型执行计划，怎样检查质量？ {#32-三条高杠杆逻辑改写经典-sql-优化的语义镜像}

开篇的 SQL 先用 `AI_FILTER` 判断合同里有没有可执行的义务，再用 `AI_EXTRACT` 从通过的合同里抽取字段。一种候选计划是让模型在同一次调用中返回判断和字段。通过的合同可以少读一遍正文，被拒绝的合同却可能多生成一段无用的答案。判断和抽取放进同一个提示词后，输出也可能改变。要比较这两种计划，可以在同一批合同上同时记录输入与输出 token 数、义务条款召回率和字段准确率。

另一种做法是保留过滤步骤，先让廉价代理给合同打分。LOTUS 的过滤级联用参考模型的判断校准接受和拒绝阈值，落在两个阈值之间的样本再交给参考模型。代理节省了多少次调用，取决于这批合同的分数分布。阈值控制的是相对于参考模型的精确率和召回率。要知道合同中的真实义务漏了多少，仍需拿人工标注核对。[LOTUS，语义过滤与级联](https://arxiv.org/abs/2407.11418)

如果错误集中在长合同尾部，例如模型读完正文却漏掉附录中的赔偿义务，可以尝试拆分文档、逐段抽取，再合并同一条款的结果。DocETL 将文档分块和任务分解作为候选改写，在样本上比较输出质量。对合同来说，一份条款正文在前一页、例外条件在后一页的样本，恰好可以检验分块是否切断了决定条款含义的上下文。[DocETL，改写规则与计划评估](https://arxiv.org/abs/2410.12189)

### 3.3 让共享前缀的请求在同一个推理节点相遇 {#33-把-prefixlocality-变成-cascades-的第三个一等物理属性}

即使每份合同只调一次模型，同一个节点仍可能反复计算相同的提示词前缀。设一批请求都以同一段抽取指令开头，随后拼入不同版本的合同模板和各自的正文。先发送使用同一模板的请求，可以让后来的请求在缓存被逐出前复用更长的前缀。已有工作在 Spark 和 vLLM 上研究了行顺序、行内字段顺序与 KV 缓存复用的关系。[Optimizing LLM Queries in Relational Workloads](https://arxiv.org/abs/2403.05821)

沿着这个思路，本文设想在物理计划中增加 `PrefixLocality` 属性。执行器根据最终提示词的共同 token 前缀给请求分组，必要时重排，并把同组请求路由到持有缓存的节点。两份合同即使属于同一主题，也可能从第一个正文 token 就不同。优化器要估计的是实际可复用的前缀长度，并比较省下的预填充时间与排序、跨节点传输和排队的开销。

---

## 四、第三块拼图：计划画干净了，怎么喂饱 GPU 又不爆内存？

优化器选出了一个满足质量目标的候选计划，但真把这套计划拉到几百张 GPU 和 PB 级非结构化文件上跑起来，物理执行层立刻会暴露出新的矛盾：**异步对象存储下载、CPU 密集型文档解码、稠密 GPU 矩阵乘法、以及有状态的 LLM 连续批推理，是四种资源画像截然冲突的负载**。把它们混在同一个线程模型里，轻则 GPU 饥饿空转，重则内存雪崩。

### 4.1 单一 Arrow 零拷贝数据面 + 四类隔离执行池

现代融合执行内核（如 Daft Swordfish 以及 Ray Data 的演进方向）给出的解法是**“数据面绝对统一，算力面彻底隔离”**：所有算子之间只传递统一的 **Apache Arrow `RecordBatch` + 惰性 `BlobRef` 句柄**（跨进程走 POSIX 共享内存 Arrow IPC 零拷贝），绝不反复序列化；但在物理调度上，把不同算子路由到四个互不干扰的执行池：

| 物理执行池 | 承载的典型物理算子 | 核心并发与内存机制 |
| --- | --- | --- |
| **Async Object-IO Pool** | 惰性 `BlobRef` 拉取、Range Coalescing、Checkpoint 落盘 | 事件循环驱动高并发异步 HTTP，支持断线 Range 自动续传 |
| **Native CPU SIMD Pool** | 列存扫描、SIMD 正则脱敏、MinHash 签名、`PrefixReorder`、RRF | 彻底绕开 Python GIL，用 Rust/C++ 向量化批处理吃满内存带宽 |
| **GPU Dense Compute Pool** | 本地 Embedding、Balanced KMeans、簇内 FP16 GEMM (`E·E^T`) | 批组装满载显存算力，与有状态 KV-Cache 推理池物理隔离防显存碎片 |
| **LLM Continuous-Batching Pool** | `AI_FILTER` / `AI_EXTRACT` / `AI_AGG` / 灰区裁判 | 常驻 vLLM / SGLang 连续批处理，输出端强制挂载 **Guided Decoding（JSON Schema / CFG 状态机）**，确保非结构化文本 100% 合法落回强类型 Arrow `STRUCT`/`ARRAY` 列 |

### 4.2 打破无状态 HTTP 黑洞：查询调度器与推理路由器的双向协同

目前绝大多数“SQL 调大模型”的实现，在查询引擎和 vLLM/SGLang 之间都只隔着一层无状态的 HTTP 客户端——SQL 引擎盲目按批发请求，推理集群盲目轮询调度。要让前面优化器费尽心思算出来的 `PrefixLocality` 真正变现，执行层必须在两个方向打通协同协议：

- **下行（前缀感知粘性路由）**：`AsyncLLMOperator` 不再轮询乱发微批，而是把优化器算出的前缀哈希随请求透传给推理路由层（如 SGLang Router、Mooncake 或 LMCache），确保共享同一长文档上下文的 SQL 行精准命中已经热缓存该 KV Block 的 GPU 节点。
- **上行（拥塞反馈 + 按字节计费的有界背压）**：`AsyncLLMOperator` 采用非阻塞微批挂起，持续监听推理池的 TTFT、KV-Cache 水位与排队深度，用类 TCP AIMD 算法动态调节在途并发度；同时，相邻算子间的有界通道全部**按字节配额（而非行数）计费**——一旦上游 PDF 解码出现 10×–20× 内存膨胀或下游推理变慢，背压逐跳反压直至暂停对象存储拉取，从根源上把多模态 OOM 堵死。

### 4.3 代价感知容错：让昂贵 GPU 作业免疫 Spot 抢占

传统分布式 SQL 引擎的容错哲学是**血缘重算（Lineage Recomputation）**：某个 Worker 挂了，按 DAG 血缘重跑上游分区就行。这套逻辑在 CPU 批处理里几乎免费，可一旦上游算子是耗时数小时的百万行 LLM 推理，节点被抢占一次就意味着几百美元的 Token 账单付诸东流。

因此，融合执行器的容错机制本身也必须是**代价感知的**：对单行 Cost 低于阈值的 CPU 算子继续沿用轻量血缘重算；对超过代价阈值的 LLM/GPU 算子，引擎自动在物理层挂载 **Row-Key 级幂等 Checkpoint Barrier**（类似 Daft `daft-checkpoint`），将已完成主键的结果异步持久化到对象存储。一旦便宜的 Spot GPU 节点被回收重启，底层自动对输入流执行 `Anti-Join` 跳过已落盘主键，上层 SQL 对此完全无感。

---

## 五、第四块拼图：跑完之后怎么用？把离线清洗与在线检索缝回同一张表

如果故事只停在单次离线清洗跑通，生产系统很快就会冒出最后两道顽疾：第一，每周新增几万份合同时，难道要把千万级存量全量重跑一遍？第二，把结果导进外部向量库后，只要业务主表里删掉一份涉密合同、或者收回某个租户的行级权限，外部向量库如果同步延迟，RAG 检索就会立刻吐出越权的“幽灵数据”。

要把这两道裂痕彻底缝合，存储层必须完成最后一步收口：

- **一表多索引与事务一致性**：基于 **Iceberg v3 / Paimon Blob / Lance** 等多模态开放表格式，把结构化标量列、大对象 `BlobRef`、全文倒排（BM25）、高维向量索引（IVF/HNSW/DiskANN，直接复用前面 `SemanticPartition` 产出的聚类质心）以及血缘审计列（`raw_uri, pii_audit, cluster_id, survivor_id, model_ver`）全部纳入同一套表快照事务与行级权限（RLS）边界，让权限撤回与数据删除在混合检索中原子生效。
- **管线即增量物化视图（Dynamic Table / IVM）**：整条 SQL × LLM 管线不再是周期全量重跑的离线脚本，而是声明在原始多模态表上的持续增量视图。当上游对象桶新增或删改文件时，只有 CDC 增量行穿过同一棵优化后的物理执行树，增量完成解析、脱敏、去重入簇、模型抽取与向量索引更新。

顺着上面推演出的四块拼图——**类型与惰性扫描、正交代数与语义 CBO、异构执行与推理协同、统一表格式与增量闭环**——回头审视 2025–2026 年工业界最主流的五类落地栈，就能一眼看清各家真正补齐了哪几块、又在哪些地方留了取舍：

| 落地栈 / 代表系统 | 类型与惰性扫描 | 正交代数与语义 CBO | 异构执行与推理协同 | 统一索引与增量闭环 | 最佳定位 |
| --- | --- | --- | --- | --- | --- |
| **Google BigQuery** | `Object Table` + 一等 `ObjectRef` | `ML.CREATE_MODEL(KMEANS)` + `VECTOR_SEARCH` / `AI.SEARCH` + `AI.IF` / `AI.GENERATE`（最接近全 Serverless SQL） | Dremel + Vertex AI 联邦推理；重型图/自定义解码回退 `bigframes` / Dataflow | 内置 ScaNN 向量索引 + 混合检索 | 强 Serverless SQL 一体化分析与轻中度多模态管线 |
| **Snowflake Cortex AISQL** | Stage + 原生 `FILE` 类型 | 语义代数最接近学术界 LOTUS（`AI_PARSE_DOCUMENT`、`AI_FILTER`、`AI_JOIN`、`AI_AGG`） | 托管 Cortex 推理；超重型聚类/定制模型退入 **SPCS** 托管 GPU 容器 | **Cortex Search**（BM25+向量）+ Dynamic Table 自动增量同步 | 强安全治理的企业知识库 ETL 与 BI × AI 融合 |
| **Databricks Mosaic AI** | Unity Catalog Volumes + Delta `VARIANT` / Binary | `ai_parse_document` / `ai_mask` / `ai_query` + Spark MLlib / GraphFrames | **Spark + Photon + Ray on Spark**，天然扛住 PB 级图 WCC 与 GPU 集群聚类 | Mosaic AI Vector Search + Lakeflow Delta Sync 增量维护 | 开放湖仓 + 超大规模自定义分布式算法与批处理 |
| **开放湖仓 AI-Native 栈**（Lance/Iceberg + Daft on Ray + vLLM/SGLang） | Lance Blob v2 / Iceberg + 零拷贝 Arrow | DataFrame / SQL 原语 + 原生多模态算子，正快速引入前缀重排与级联优化 | **四池解耦最彻底**：Rust 原生 SIMD + 有界流式背压 + 独立 vLLM 池 + Key 级 Checkpoint | Lance 一体化标量/Blob/IVF-PQ/BM25，离线批吞吐性价比最高 | 基模预训练/后训练 PB 级语料清洗与重度多模态加工 |
| **实时混合检索服务栈**（Hologres / StarRocks / Doris / PG） | 行列混存或列存 + 标量/向量共存 | 专精高并发 `IndexANNJoin`（Pre/Iterative-filter）+ BM25 RRF + 在线轻量拦截 | 低延迟 MPP/协程引擎 + 外联推理网关 | Dynamic Table 实时增量索引 + 完整 RLS 权限 | 千并发亚秒级 RAG 在线召回与 Agent 长期记忆服务层 |

---

## 六、回到架构原点：落地边界与未来演进

顺着开篇那个问题一路推演到底，一套 SQL × LLM 融合引擎其实并不神秘。落地到真实工程团队时，有五条边界清晰的原则值得守住：

1. **只沉淀正交原语，绝不把业务工序写死进内核**：业务需求会不停演变，内核只守住 `BlobRef` 扫描、四类正交代数算子、多目标语义 CBO、四池异步执行器和统一开放表格式这几个稳定扩展点。
2. **用字节背压与簇容量上限锁死两条 OOM 红线**：多模态链路最容易爆内存的永远是两处——文档/视频解码 20× 内存膨胀与 `SimilarityJoin` 局部 \(O(m^2)\) 展开。前者靠全链路按字节计费的有界背压治理，后者靠 `SemanticPartition` 均衡切分加 GPU 分块 GEMM 兜底。
3. **靠“代理级联 + 前缀重排 + Key 级 Checkpoint”守住 GPU 账单**：三项叠加，通常能把千万级文档的 LLM 批处理费用压低一个数量级，同时让团队敢于大规模使用廉价 Spot GPU。
4. **语义阈值靠评测集闭环，不靠拍脑袋**：无论是去重相似度阈值、聚类簇数还是代理级联的灰区边界，都应绑定固定 Golden Eval Set 或下游指标自动校准，并将血缘审计列随主表一起落盘。
5. **逻辑声明统一，物理执行允许专精分工**：PB 级离线多模态语料清洗与毫秒级高并发在线 RAG 在物理架构上短期内仍会各自专精，但它们正借由 **Apache Arrow 内存格式**与 **Iceberg / Paimon / Lance 开放表格式**在逻辑层彻底合流。

往后看两到三年，SQL 引擎与大模型融合的主线已经十分明朗：**一是**从今天仍需显式指定模型名与级联阈值，迈向只需声明“精度目标 + 预算上限”的**多目标帕累托语义 CBO**；**二是**打通查询优化器的 `PrefixLocality` 物理属性与分布式 KV-Cache 路由层（Mooncake / LMCache / SGLang Router）的**跨层协同协议**；**三是**在线**自蒸馏闭环**走向成熟——让数据库自动将模式稳定的 LLM 算子编译为高速确定性代码或小模型，仅在数据分布漂移时唤起旗舰大模型兜底，真正把大模型能力内化成关系引擎的一等基础原语。

---

> **参考与调研基线**
> - **工业界云数仓与 AI-Native 栈公开文档（2025–2026）**：Google Cloud BigQuery（`Object Table`/`ObjectRef`、BQML、`VECTOR_SEARCH`/`AI.SEARCH`、`AI.IF`/`AI.GENERATE`）；Snowflake Cortex AISQL（`FILE`、`AI_PARSE_DOCUMENT`、`AI_FILTER`/`AI_JOIN`/`AI_AGG`、Cortex Search、SPCS）；Databricks Mosaic AI（Unity Catalog Volumes、`ai_parse_document`/`ai_mask`/`ai_query`、Photon、Ray on Spark、Mosaic AI Vector Search）；Daft + Lance / Iceberg v3 / Paimon Blob 与 vLLM / SGLang / Mooncake 公开架构文档。
> - **多模态去重与向量聚类实证**：Meta AI *SemDeDup* & *D4* · Hugging Face *FineWeb / FineWeb-2* 技术报告 · NVIDIA NeMo Curator & RAPIDS cuML / FAISS GPU 官方文档。
