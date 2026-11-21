---
title: "AI Functions、LLM 数据管线与云数仓引擎内核横评：从语义算子到 Photon / Snowflake / StarRocks 取舍"
slug: "ai-functions-and-llm-data-pipelines"
date: 2026-11-20T20:55:00+08:00
lastmod: 2026-11-21T00:00:00+08:00
aliases:
  - "/2026/11/20/databricks-snowflake-starrocks-engine-and-aisql/"
categories:
  - AI Infra
  - 数据库
  - 分布式系统
tags:
  - AI Functions
  - AISQL
  - LLM
  - Ray Data
  - Daft
  - vLLM
  - SGLang
  - Databricks
  - Photon
  - Snowflake
  - StarRocks
  - 向量化执行
  - Runtime Filter
description: "梳理现代 LLM 数据管线的边界与六阶段标准漏斗，剖析 AI_FILTER、AI_CLASSIFY、AI_EXTRACT 与 AI_JOIN 等语义算子的查询优化与非 SQL 底层原语，并从云原生护城河、向量化、异步流水线与 Runtime Filter 四个维度深度横评 Databricks Photon、Snowflake 与 StarRocks，给出生产选型与 2026–2028 演进判断。"
draft: false
toc: true
math: true
---

过去两年，几乎所有数据团队都写过类似的一段脚本：从对象存储或数仓里拉出一百万条客服工单、合同 PDF 或商品评论，起几十个 Python 进程循环调用大模型 API 做抽取与分类，再把返回的 JSON 塞回数据库。

只要把数据规模再放大一个数量级，这套朴素做法会在四个地方同时失控：单行脏数据或 API 限流导致跑了六小时的夜间批作业整批崩溃；PDF 与高清视频批量解码瞬间把 Worker 内存打爆（OOM）；每条记录都喂给旗舰模型，一周烧掉上万美元 Token 费；更致命的是，自由生成偶尔多吐一个 Markdown 围栏或换个字段名，下游数仓表 Schema 当场断裂。

当同样的需求回到数据库内部，矛盾同样尖锐。把一条经典星型模型多表关联查询放进 **Databricks Photon**、**Snowflake** 和 **StarRocks**，三套引擎往往都能跑出亚秒到秒级的成绩；可一旦负载变成百 TB 级长链路 ETL、千 QPS 高并发报表，或在 `WHERE` 子句里塞进一句自然语言条件 `AI_FILTER('客户明确表达了退款意向', ticket_text)`，三套系统的延迟、账单和稳定性立刻会拉出数量级差距。

两条线的交汇点其实指向同一个底层事实：当单个算子的单行处理代价从 `10 ns` 暴涨到 `100 ms – 2 s` 且按 Token 实时计费时，无论是外部分布式 Python 管线还是内部云数仓执行引擎，都必须围绕异构算力、流式背压、语义代价优化与存储裁剪重新分工。

---

## 一、边界与标准流程：两类 LLM 数据管线与六阶段漏斗

强行用同一套工具同时服务“企业业务表增强”和“大模型预训练洗数”，是大量项目过度设计或成本爆炸的根源。当前生产落地中的 **LLM 数据管线（LLM Data Pipelines）** 天然分为两极：

- **企业非结构化增强与 RAG / Agent 业务管线（Enterprise AI-ETL）**：每日增量处理数万至数千万条客服工单、通话转写、合同 PDF 与业务日志，完成结构化抽取、多标签分类、语义过滤/连接与向量索引更新。核心约束在于**数据已在数仓内、强合规禁止 PII 出域、下游 BI 与应用强依赖稳定 Schema 与血缘治理**。
- **大模型预训练 / 后训练多模态语料清洗与合成管线（Foundation Model Curation）**：处理百 TB 至 PB 级网页（如 Common Crawl）、代码、图像、音视频与合成推理轨迹，执行解码抽帧、精确/模糊/语义去重、PII 脱敏、质量打分过滤与批量蒸馏推理。核心约束在于**极致压榨单 Token 与 Spot GPU 成本，以及解决 CPU 解码与 GPU 推理之间的异构背压失衡**。

![现代 LLM 数据管线六阶段漏斗与双轨技术栈协同架构](/images/diagrams/ai-llm-data-pipeline-and-aisql.svg)

无论上层采用数仓 AI SQL 还是自建分布式 Python 栈，生产级管线都遵循一条铁律——**凡是能用微秒级 CPU 算子裁剪掉的数据，绝不送进百毫秒级 LLM**。完整的端到端处理通常收敛为六个阶段：

1. **多模态接入与统一存储**：通过原生 `FILE` 类型（Snowflake）、`Object Table`（MaxCompute）、`Blob v2`（Lance v2.2）或 `.parquet + .blob`（Apache Paimon），将结构化标量与大体积音视频、文档纳入同一事务快照，避免表元数据与外部文件版本脱节。
2. **解析与流式切块（Parsing & Chunking）**：将 PDF、扫描件与音视频流式解码为文本或视觉切片。为防止单批拉取海量高清多媒体撑爆内存，解析层必须施加严格的**流式背压（Streaming Backpressure）**。
3. **廉价确定性裁剪与去重**：先下推时间区间、业务状态等确定性关系过滤谓词；在训练语料场景下并行执行规则清洗、精确哈希去重、MinHash 模糊去重与 PII 脱敏，提前淘汰 70%–90% 的无效样本。
4. **语义路由与分级推理（Workload–Router–Pool）**：不再把所有存活行发给同一个旗舰模型，而是经由轻量代理模型（Proxy，如向量距离、小分类器或 7B 量化模型）直接裁决高置信样本，仅将模糊灰区路由给昂贵的参考大模型（Oracle）；同时在投递给底层 **vLLM / SGLang** 推理池前按共享前缀聚簇微批，放大 KV Cache 复用。
5. **结构化约束与质量门禁**：用受限解码（Constrained Decoding / JSON Schema）锁死合法输出格式；配置单行死信隔离（如 `failOnError => false`）防止脏行拖垮整批任务；接入抽样评测（Eval-in-the-Loop）实时监控准确率与幻觉漂移。
6. **双轨落库与下游消费**：结构化抽取结果写回 Delta / Iceberg / Snowflake 表，同步刷新向量索引（HNSW / IVF / DiskANN）与全文倒排索引，供下游 BI、RAG 检索、Agent 长记忆或训练 DataLoader 消费。

---

## 二、语义算子内核：AI Functions、查询优化与非 SQL 等价实现

把大语言模型推理直接下沉为一等公民算子，会立刻击穿经典数据库理论的三条隐含前提：

1. **单行代价暴涨 7 个数量级**：经典算子评估一行只需约 `10 ns` 且无边际货币开销；一次 LLM 推理耗时 `100 ms – 2 s` 且按 Token 实时计费。
2. **优化目标变成三维帕累托前沿**：传统 CBO 只需在确定性语义下最小化耗时与资源；语义算子具备概率性，优化器必须在**延迟、Token 财务成本与输出语义准确率**三者间寻找平衡。
3. **数据类型跨越纯标量**：原生 `FILE`（PDF、高清图像、音频）与 `VARIANT`（半结构化 JSON Shredding）成为表内一等公民（底层存储格式的对应变革可见 [AI 时代的大数据列存基座]({{< relref "2026-11-17-columnar-formats-orc-parquet-lance-in-ai-era.md" >}})）。

### 2.1 典型 AI Functions 的 SQL 形态与语义查询优化

以 **Snowflake Cortex AISQL** 与 **Databricks AI Functions** 为例，主流语义算子已收敛为六种基础形态：`AI_FILTER`（语义布尔过滤）、`AI_CLASSIFY`（有限集单/多标签分类）、`AI_EXTRACT`（非结构化转结构化字段）、`AI_COMPLETE` / `ai_query`（带强 Schema 与容错的通用批推理）、`AI_JOIN`（跨表语义连接），以及 `AI_AGG` / `AI_PARSE_DOCUMENT`（跨行分组语义汇总与多模态解析）。

```sql
-- 示例 1（Snowflake Cortex AISQL）：先做廉价关系过滤，再执行语义过滤、分类与跨行分组聚合
SELECT
    product_line,
    AI_CLASSIFY(ticket_text, ['物流延迟', '账单异常', '功能故障', '其他']):labels[0]::STRING AS issue_type,
    AI_AGG(ticket_text, '总结本组客户最集中的前 3 个痛点，并给出改进建议') AS summary
FROM support_tickets
WHERE created_at >= '2026-01-01'                          -- 廉价关系谓词，优化器强制前置
  AND AI_FILTER('客户明确表达了退款或投诉意向', ticket_text) -- 昂贵语义谓词，延后执行
GROUP BY product_line, issue_type;

-- 示例 2（Databricks ai_query）：带 JSON Schema 强约束与单行容错的批量结构化抽取
SELECT
    doc_id,
    ai_query(
        'databricks-meta-llama-3-3-70b-instruct',
        CONCAT('从以下合同中抽取甲方、金额与到期日：', raw_text),
        responseFormat => '{
          "type": "json_schema",
          "json_schema": {
            "name": "contract_extract",
            "schema": {
              "type": "object",
              "properties": {
                "party_a": {"type": "string"},
                "amount":  {"type": "number"},
                "due_date":{"type": "string"}
              },
              "required": ["party_a", "amount", "due_date"]
            }
          }
        }',
        failOnError => false                              -- 单行异常落入 error 字段，不拖垮整批 ETL
    ) AS parsed_result
FROM raw_contracts
WHERE ingest_date = CURRENT_DATE();
```

为了不让这些查询烧穿预算，工业界（Snowflake Cortex AISQL、Databricks `ai_query`）与学术界（**LOTUS** PVLDB 2025、**Palimpzest** CIDR 2025）正在把三项核心机制写进**语义查询优化器（Semantic Query Optimization, SQO）**：

- **反直觉的谓词重排与上拉**：传统优化器总想把过滤条件下推到最底层扫描；但在混合关系/语义查询中，若把 `AI_FILTER` 排在 `created_at >= '2026-01-01'` 之前，或错误下推到高淘汰率 Join 之前，会白白多烧数百万次 LLM 调用。新代优化器同时在编译期与运行期监测选择率，强制先跑廉价确定性谓词，并按**预期总 LLM 调用次数**决定将 AI 谓词下推还是上拉越过 Join（Snowflake 披露其上下文重写与自适应路由可砍掉最高 **60%** 冗余 Token）。
- **自适应 Proxy → Oracle 模型级联**：清晰样本直接由轻量 Proxy 模型秒级裁决，仅将置信度落入灰区的模糊样本升级给旗舰 Oracle 模型，在保留 **90%–95%** 准确率的同时压低绝大部分推理开销。
- **异步微批调度与前缀缓存**：CPU 向量化线程绝不能同步死等 HTTP 推理返回，引擎在底层解耦出异步微批队列，并对共享 System Prompt 的请求开启 Prefix KV Caching。

### 2.2 拆解 `AI_JOIN`：如何把 \(O(N \times M)\) 的语义连接压到近线性

传统等值连接能从 \(O(N \times M)\) 降到 \(O(N + M)\)，靠的是等价哈希函数或全序排序；而 `JOIN ... ON AI_FILTER('该工单是否可由该知识库文章解决', t.desc, kb.doc)` 既无哈希也无全序，朴素嵌套循环在 \(10^4 \times 10^3\) 规模下就要发起千万次 LLM 调用。结合 Snowflake AISQL 与 LOTUS `sem_join` 的公开实现，引擎靠四步接力降维：

1. **按右表基数分流物理重写**：当 Build 侧（右表）基数较小（几十到几百条类别、规则或文档）时，引擎将 Build 侧整批打包进同一个共享 Prompt 上下文，把 `AI_JOIN` 重写为对 Probe 侧每行的**受限多标签分类 / 批选择**，直接将调用次数压至 \(O(N)\)（Snowflake 公开披露提速 **15x–70x**）；当左右表基数都很大时，退而采用 **Embedding Blocking**——为 Build 侧建向量索引（HNSW / IVF），对 Probe 侧每行仅召回 Top-\(K\) 近似候选对，把搜索空间缩至 \(O(N \cdot K)\)。针对否定条件或长文本含噪导致原文 Embedding 失真的问题，LOTUS 与 SemJoin 还会先用轻量投影把原文规整为关键特征或合取范式再做粗筛。
2. **小样本校准双阈值级联**：从 Blocking 后的候选对中随机抽样跑一遍 Oracle 大模型获得参考标签，结合 Proxy 分数分布统计校准出高置信通过阈值 \(\tau_{\text{high}}\) 与高置信拒绝阈值 \(\tau_{\text{low}}\)（给出相对全量 Oracle 的 Precision/Recall 统计保证），仅把落入 \((\tau_{\text{low}}, \tau_{\text{high}})\) 灰区的候选对送去精判。
3. **混合关系裁剪先行**：若连接条件同时包含等值键与语义条件（如 `a.region = b.region AND AI_FILTER(...)`），先走传统 Hash 分桶切碎搜索域，只在同桶内执行向量粗筛与语义精排。
4. **前缀聚簇微批投递**：把送往推理池的灰区候选对按共享 System Prompt 或固定右表文本分组拼包，放大底层推理引擎（vLLM、SGLang RadixAttention 或 Arctic Inference）的 Prefix KV Cache 命中率。

### 2.3 非 SQL 栈中的底层等价实现原理

如果在 **Ray Data / Daft + vLLM / SGLang** 这样的 Python 分布式栈中自建上述算子，绝不能停留在“调 Chat API + 正则解析”，每个算子背后都有对应的推理与系统原语：

1. **`AI_FILTER` & `AI_CLASSIFY` → 受限词表采样（Guided Choice）+ Logprobs 置信度级联**
   让模型吐出 50 个 Token 解释再写 `if "YES" in text`，不仅 Decode 耗时放大 50 倍，还极易格式漂移。工业级做法是向 **vLLM / SGLang** 传入 `guided_choice`（把合法输出锁死在给定标签词表）并指定 `max_tokens=1, logprobs=True`：模型仅需 **1 个 Decode 步**即可返回严格合法的标签，同时直接提取该 Token 的对数概率换算为置信度 \(p = \exp(\text{logprob})\)，驱动 Proxy → Oracle 的阈值级联。

```python
import math, ray
from vllm import LLM, SamplingParams

class GuidedClassifier:
    def __init__(self):
        self.llm = LLM(model="Qwen/Qwen2.5-7B-Instruct", quantization="fp8")
        self.params = SamplingParams(
            max_tokens=1, temperature=0.0, logprobs=2,
            guided_decoding={"choice": ["YES", "NO"]},
        )

    def __call__(self, batch: dict) -> dict:
        prompts = [f"判断是否存在退款或投诉意向，只回答YES或NO：\n{t}" for t in batch["ticket_text"]]
        outputs = self.llm.generate(prompts, self.params)
        labels, confs = [], []
        for out in outputs:
            tok = out.outputs[0]
            labels.append(tok.text)
            top_lp = next(iter(tok.logprobs[0].values())).logprob
            confs.append(math.exp(top_lp))
        batch["is_complaint"], batch["confidence"] = labels, confs
        return batch

ds = ray.data.read_parquet("s3://bucket/tickets/") \
    .filter(lambda r: r["created_at"] >= "2026-01-01") \
    .map_batches(GuidedClassifier, concurrency=4, num_gpus=1, batch_size=256)
# 后续仅将 confidence < 0.85 的灰区行路由至大模型 Actor 池复核
```

2. **`AI_EXTRACT` & `ai_query(responseFormat)` → FSM / XGrammar 受限解码 + 死信列隔离**
   底层由 vLLM / SGLang 在推理前将 Pydantic / JSON Schema 编译成**有限状态机（FSM）或下推自动机（XGrammar / Outlines）**。每一步自回归采样前动态计算当前语法状态允许的合法 Token 集合，将非法 Token 的 Logits 全部置为 \(-\infty\)，从数学上保证输出 100% 符合目标 Schema；批处理 Actor 内部对单行异常捕获并写入 `error` 死信列，复刻 `failOnError => false` 的作业韧性。
3. **`AI_JOIN` → Lance / FAISS Blocking + SGLang RadixAttention / LOTUS `sem_join`**
   直接落地上节的四步降维机制：小右表走共享前缀多标签分类配合 **SGLang RadixAttention** 复用前缀 KV Cache；大表双边走 Embedding + **Lance / FAISS（HNSW）** 召回 Top-\(K\) 候选对，再叠加 **LOTUS `sem_join`** 的校准双阈值级联。
4. **`AI_AGG` / `AI_SUMMARIZE_AGG` → Token 预算感知的分层树状归约（Hierarchical Map-Reduce）**
   单个 `GROUP BY` 分组内可能包含数千条评论，远超单次上下文窗口且易触发“中间遗忘（Lost in the Middle）”。非 SQL 管线借鉴分布式 SQL 的 `Partial Agg → Final Agg`：按固定 Token 预算（如每桶 8K–16K Token）对组内文本分桶，并发生成保留关键频次与原句锚点的局部摘要，再递归归约直至装入最终合成窗口；面对长合同等高复杂度文档聚合，则引入 **DocETL** 的 `split-gather` / `reduce` Agentic 计划重写。
5. **`AI_PARSE_DOCUMENT` & `AI_EMBED` → CPU/GPU 异构解耦与流式背压**
   在 **Daft**（Rust + Python 原生多模态引擎，执行层 *Swordfish/Flotilla*）或 **Ray Data** 中，将轻量 CPU Stage（S3 流式分片下载、`Docling`/`MinerU`/`PyMuPDF` 解码与切块）和 GPU Actor Stage（版面 OCR / VLM 与 Embedding 批推理）物理剥离，中间通过有界 Arrow Batch 队列施加背压，避免上游解码把内存撑爆，同时让下游 GPU 始终吃满批大小。

| 典型 AI Function | 核心语义 | 非 SQL 典型载体 | 底层核心原语 |
| :--- | :--- | :--- | :--- |
| **`AI_FILTER`** | 自然语言布尔过滤 | Ray Data / Daft + vLLM / LOTUS | `guided_choice` + `max_tokens=1` + Logprobs 级联 |
| **`AI_CLASSIFY`** | 有限标签集分类 | Ray Data / vLLM / 轻量分类器 | 词表掩码受限解码 + 低置信度升级路由 |
| **`AI_EXTRACT` / `ai_query`** | 结构化抽取与自定义推理 | Pydantic + vLLM / SGLang | FSM / XGrammar 逐 Token Logit Mask + 死信列隔离 |
| **`AI_JOIN`** | 跨表自然语言语义连接 | Lance / FAISS + SGLang / LOTUS | 共享前缀多标签（RadixAttention）或 ANN Blocking + 校准级联 |
| **`AI_AGG`** | 跨行分组语义汇总 | Ray / Daft GroupBy + DocETL | Token 预算感知的分层树状 Map-Reduce 归约 |
| **`AI_PARSE_DOCUMENT` / `AI_EMBED`** | 多模态解析与向量化 | Daft (`Swordfish`) / Ray + Docling | CPU 解码切块与 GPU 推理异构解耦 + 有界 Arrow 背压 |

---

## 三、执行引擎内核横评：Databricks Photon、Snowflake 与 StarRocks

回到数仓与 OLAP 阵营，语义算子并非空中楼阁——它跑在底层存储布局、向量化执行器、流水线调度器与动态过滤链路之上。**Databricks Photon**、**Snowflake** 与开源实时 OLAP 代表 **StarRocks** 在底层物理约束上作出了截然不同的下注，这直接决定了它们在传统分析与 AISQL 时代的上限与暗礁。

![Photon、Snowflake 与 StarRocks 执行内核与动态过滤链路对比](/images/diagrams/db-photon-snowflake-starrocks-kernels.svg)

### 3.1 两条云原生路径的竞争壁垒：Databricks vs. Snowflake

到 2026 年，Databricks 与 Snowflake 在产品形态上已高度趋同，但各自的护城河依然牢牢长在原始架构底座之上。

**Databricks** 的策略是**文件格式与开源生态极度开放，计算加速与全局运维深度闭环**：

- **Delta Lake 存储三件套定型**：通过 **Deletion Vectors**（删除向量）将行级更新/删除从整文件重写推迟为轻量 RoaringBitmap 读时合并（小比例 `MERGE` 提速约 10 倍）；通过 **Liquid Clustering**（液态聚类）取代刚性分区与全量重写的 `Z-ORDER`，以增量方式只重排未聚类文件并支持随查询轨迹自动选列（`CLUSTER BY AUTO`）；再借助 **UniForm + Apache Iceberg v3** 在二进制层面统一 Deletion Vectors 与 `VARIANT` 规范，用“一份 Parquet + 双份元数据”终结格式割裂。
- **Photon 执行引擎（SIGMOD 2022）**：以 C++ 重写核心向量化算子并通过 JNI 挂载在 Apache Spark 内部，既避开 JVM GC 抖动与全阶段代码生成（WSCG）的编译泥潭，又无损继承 Spark 庞大的数据处理与 ML 生态。
- **平台级闭环与版图延伸**：依托 **Unity Catalog** 对全平台查询模式的全局感知，**Predictive Optimization** 能自动触发统计采集、小文件合并与液态聚类——这是开源 Spark + Delta 用户无法复现的系统级红利。2025 年底随着收购 Neon 推出的 **Lakebase**（Serverless PostgreSQL）GA，Databricks 进一步把入口从下游分析向前延伸到在线事务库与 Agent 状态存储。

**Snowflake** 则摒弃了 Hadoop/JVM 历史包袱，用三层彻底解耦的洁净室纯 C++ 架构（SIGMOD 2016 / NSDI 2020）树立了云数仓工程标杆：

- **FoundationDB 驱动的全局服务层**：无状态 **Cloud Services Layer** 背后依托 FoundationDB 统一协调全局事务、元数据、裁剪索引与权限，使多个物理隔离的 **Virtual Warehouse** 可以在同一份远端对象存储上并发读写且互不干扰。
- **不可变微分区（Micro-Partition）与本地 SSD 缓存**：写入时自动按 50–500 MB（未压缩）切分微分区并维护丰富的列级 Min/Max 与 NDV 统计，配合一致性哈希路由的 **Ephemeral Local SSD Cache** 与 Multi-Cluster Warehouse 秒级扩缩，做到近乎零人工建索引/分区的“开箱即用”。
- **Gen2 Warehouse 与开放兼容**：2025–2026 年推出的 **Generation 2 Standard Warehouses** 结合新代硬件与内核重构，官方基准显示核心分析平均提速 **2.1x**、DML（`DELETE`/`UPDATE`/`MERGE`）最高提速 **4.4x**，并原生内建面向 Cortex AISQL 的容器化推理编排。生态边界上则通过 **Unistore Hybrid Tables** 补齐事务点查，通过托管与外部 **Apache Iceberg** 表回应开放湖仓诉求。

| 对比维度 | Databricks | Snowflake |
| :--- | :--- | :--- |
| **核心基因** | 开源计算与 ML 生态 + 闭源向量化加速与智能治理 | 纯自研 C++ 多租户数仓，极致零运维与隔离体验 |
| **最强壁垒** | Data+AI 全栈统一、Unity Catalog 预测优化、超大规模 ETL 韧性 | 多租户物理隔离、零调优微分区裁剪、极稳 BI 并发 |
| **存储与开放性** | Delta Lake 原生主场 + UniForm / Iceberg v3 双向兼容 | 自研 Micro-Partition 深度闭环 + Iceberg 原生支持 |
| **在线状态延伸** | **Lakebase**（Serverless PostgreSQL） | **Unistore Hybrid Tables**（行存事务 + 列存同步） |

### 3.2 向量化执行：Position List、微分区闭环与全局低基数字典

剥开商业叙事回到内核本身，三家在批处理表示、活跃行裁剪与编码态加速上走了三条不同路线：

| 内核维度 | Databricks Photon (SIGMOD 2022) | Snowflake (SIGMOD 2016 / Gen2) | StarRocks (Vectorized C++) |
| :--- | :--- | :--- | :--- |
| **运行态宿主** | C++ 引擎经 **JNI** 嵌入 Spark JVM | 全栈 C++ 从零构建，零 JVM 依赖 | 全栈 C++ 从零构建（BE / CN） |
| **批处理单元** | `ColumnBatch` 连续列内存块 | 紧耦合微服务/微分区的列块 | `Chunk`（默认 4096 行，可配 `vector_chunk_size`） |
| **过滤活跃行表示** | **Position List**（显式活跃行下标数组，免物理拷贝） | 向量化裁剪 + 微分区底层跳过 | 过滤位图 + 延迟物化 + 部分路径物理 Compaction |
| **编码态加速王牌** | 针对开放 Parquet 的 SIMD 批解码 | 闭源 Micro-Partition 端到端编码协同 | **低基数全局字典**：字符串转定长整型直算 Join/Agg/Sort |

- **Photon 的得与失**：SIGMOD 2022 论文验证了一个经典工程判断——在稀疏过滤场景下，维护显式活跃行下标数组（**Position List**）比稠密位图或反复物理拷贝压缩列块省下大量 CPU 周期，后续算子可直接跳过被过滤行。但作为 Spark JVM 的加速插件，一旦遇到未覆盖的表达式、UDF 或复杂嵌套类型就会触发 **Fallback 回退**，在 JNI 边界付出高昂的行列转换与堆内外拷贝税；面对开放 Parquet 也缺少跨文件强托管的全局字典红利。
- **Snowflake 的得与失**：全栈纯 C++ 彻底消灭了 Fallback 断崖，存储格式（Micro-Partition）与执行器高度一体化，Gen2 进一步强化了 DML 与复杂算子的硬件亲和。代价是这些极限优化与闭源内部微分区强绑定，查询外部 Iceberg 表时部分内部编码优势难以完整复现。
- **StarRocks 的得与失**：在字符串密集型 BI 与多维聚合中，StarRocks 的**低基数全局字典优化**（Low-Cardinality Optimization）杀伤力极强——CBO 在物理重写阶段直接把低基数字符串或数组列映射为定长整数 ID，让最耗 CPU 的字符串哈希探测、分组、排序与内存拷贝全部变成整型 SIMD 运算，全程免解码；配合 Native Parquet/ORC Reader 的 **Late Materialization**（先扫谓词列生成 `rowid bitmap` 再按需物化其余列）与 Page Index 裁剪，湖仓直查同样凶悍。短板在于部分算子路径在过滤后采用物理 Compaction 拷贝 Chunk，连续多过滤下内存搬移多于 Photon 的 Position List；在大规模 Iceberg Equality Delete（实现为 Left Anti-Join）与高频 Primary Key 表 Compaction 场景下，jemalloc 内存峰值与写放大仍是社区持续打磨的痛点。

### 3.3 流水线调度与 AISQL 异步适配：Pull + Stage 容错 vs. Push Work-Stealing vs. Morsel 状态机

执行调度模型不仅决定了传统 OLAP 的并发与容错特征，更直接决定了引擎吸收远程 LLM 推理算子的改造成本：

- **Photon（Pull-based 向量化 + Spark Stage / AQE）**：算子间保持 `next()` 向上拉取的控制流，寄生于 Spark 的 Stage 级落盘 Shuffle 与自适应执行（AQE）之上。这赋予它三者中最强的**百 TB 级长耗时批处理容错韧性**——运行两小时中途挂掉几台节点只需局部重算受损分区，且 AQE 可在 Stage 间隙合并碎分区或拆分倾斜桶；在批量调用 `ai_query` 时也天然受益于 Stage 级重试与单行容错。代价是 Stage 落盘屏障天生拉高小查询延迟，Pull 模型在深层算子树里的指令缓存局部性也逊于 Push 模型。
- **Snowflake（Push-based + 跨节点动态 Work-Stealing）**：生产者主动向下游推送列块，让热数据紧贴 CPU 寄存器与 L1/L2 Cache，天然适配 DAG 分叉计划；NSDI 2020 论文中极具巧思的**跨节点 Work-Stealing** 允许提前跑完本地微分区的计算节点动态窃取慢节点的剩余任务，在云端 S3 尾部抖动、数据倾斜乃至 LLM 响应长尾抖动时把 P99 延迟压得极稳，Gen2 又进一步内建了容器化推理编排。不过纯 MPP 内存流水线面对超长超大批 ETL 时的局部容错弹性仍弱于 Spark Stage 落盘模型。
- **StarRocks（Morsel-Driven Pipeline 状态机 + IO/CPU 彻底分离）**：将 Fragment 拆为 `Source → Operators → Sink` 的 Pipeline，由 **Driver 状态机**以 Morsel 微块为粒度在固定 CPU 线程池内协作抢占调度，告别“每查询开几十线程”的上下文切换风暴。更关键的是在 Shared-Data 存算分离模式下将 **CPU Task 与 Async IO Task 严格剥离**——当 `ScanOperator` 发起远端对象存储读取时提交异步 IO 任务并立刻切入 `PENDING_CHUNK` 释放 CPU 线程，下游满载时切入 `PENDING_FINISH`/`INPUT_EMPTY` 自然传导背压。这种架构不仅让 StarRocks 在**高并发亚秒级 BI 场景的吞吐与性价比上表现突出**，也使其天生适合吸收 AISQL：只需把远程 LLM/Embedding 推理抽象为新型异步微批算子，即可在不阻塞 CPU 线程池的前提下自然传导背压。其工程代价在于复杂的异步状态机叠加上 **Spill to Disk** 后边界错误路径陡增（社区曾出现 Hash Join Spill 异常状态未同步导致 Probe 侧死循环的典型竞态），且 FE 元数据层长期背负的 BDBJE 在超大规模 Multi-Warehouse 并发写入与千万级 Shard 调度上，明显不如 Snowflake 基于 FoundationDB 的云服务层从容。

### 3.4 Runtime Filter 与云存储 S3 I/O 裁剪的四道漏斗

在星型/雪花模型多表关联中，动态运行时过滤（Runtime Filter, RF）直接决定云存储网络带宽是省掉 90% 还是白拉全表。这里必须区分两个易混概念：**静态存储侧布隆过滤器**用于回答“单个明确值 `x` 在不在这个文件/RowGroup 里”；而 Join Build 侧临时构建的**动态 Runtime Bloom Filter** 因长度与哈希种子和存储侧不同，无法直接与静态 BF 按位求交，必须借由以下四道漏斗层层削减 S3 `GET` 请求：

1. **文件级跳过（零 S3 数据文件 `GET`）**：Snowflake 的 `JoinFilter` 将 Build 侧提取的过滤界限或集合直接下推到 Probe 侧的 **Micro-Partition Pruning**，在向 S3 发第一个字节请求前即可剪掉 95%+ 无关微分区（前提是微分区按相关键聚簇良好，否则 Min/Max 重叠会导致裁剪衰减）。**Photon + Delta DFP（Dynamic File Pruning）** 则将小维表产出的运行时键值/范围注入 `_delta_log` 文件级统计，直接剔除无关 Parquet 文件；针对高基数 UUID 等值点查，Delta 曾提供 `_delta_index` 旁路布隆索引文件，但在 DBR 12.2+ / Photon 时代因写放大较重，已逐步演进为 **Liquid Clustering + Predictive I/O**。
2. **Row Group 级跳过（KB 级 `Range GET` 跳过几十 MB 数据块）**：拉取 Parquet 尾部 Footer 后，静态等值谓词可直接拉取几十 KB 的 Parquet 原生 Split Block Bloom Filter 跳过整块 Row Group；而 **Runtime Bloom Filter 省 S3 I/O 的杀手锏在于字典页过滤**——当 Probe 侧 Join Key 列采用字典编码时，Reader 只需用一次极小的 S3 `Range GET` 拉取几 KB 到几十 KB 的 **Dictionary Page**，把字典内全部唯一值送入 Runtime Bloom Filter 探测，只要无一命中，该 Row Group 的全部 Data Pages 直接免下载。
3. **列级与 Page 级延迟物化（砍掉宽表其余 95% 列带宽）**：即使 Row Group 命中少量行，引擎也只先拉取 Join Key 单列并用 Runtime Bloom Filter 过滤（Photon 缩减 Position List，StarRocks 生成 `rowid bitmap`），再结合 Parquet **Column/Offset Index** 仅对包含存活行的少数 Page 按精确字节区间拉取宽表其余非 Key 列，往往直接削减 **80%–95%** 的下载流量。StarRocks 在这一层做到了极高的工程完备度：按 NDV 自动选用 **In-list / Bloom Filter / Min-Max**，通过 FE 协调跨 Fragment 分发 **Global Runtime Filter（GRF）** 并区分 Early RF 等待与 Late RF 防阻塞降级，还具备**运行期自适应裁撤**能力——实时监测到某个 RF 过滤率太差白耗 CPU 时自动关闭。
4. **对象存储请求合并（IO Coalescing）**：鉴于 S3 单次 HTTP `GET` 存在数十毫秒固定延迟且按请求次数计费，Photon、StarRocks 与 Arrow/Iceberg Reader 在算出零散命中区间后，都会把相距较近（如 `< 1 MB`）的 Range 合并成一次连续 `Range GET`，防止“省了带宽却打爆请求延迟”。

---

## 四、生产选型铁律、内核演进与 2026–2028 趋势

### 4.1 生产实测账本与技术栈选型原则

从 2025–2026 年公开的企业账单与基准数据来看，AI SQL 已在常规增量业务 ETL 中占据重要份额：在成熟的 Snowflake Cortex 生产环境里，**AISQL Token 函数（`AI_COMPLETE`、`AI_EXTRACT`、`AI_CLASSIFY`）约占平台 AI 总支出的 38%**，超过 Cortex Search（~28%）与 Analyst/Agents（~12%–18%）；伴随 Snowflake 将 AI Credits（约 \$2.00–\$2.20 / Credit）拆分重校，企业版及以上客户的 Token 密集型 AISQL 实际美元账单下降了 **40%–44%**；Databricks 的 Foundation Model API 批处理后端升级也将 `ai_query` 批吞吐拉高了 **10x 以上**。

架构师在落地时面临的核心分界线，归根结底是四笔硬账：

1. **什么时候选数仓 AI Functions（Snowflake AISQL / Databricks `ai_query`）？**
   - **合规与零出域红线**：金融、医疗与大型企业的敏感表禁止导出数仓安全域。AI SQL 直接继承 Unity Catalog / Snowflake RBAC、列级脱敏与血缘审计，省去给外部 Ray 集群重做 VPC/IAM/Ranger 鉴权与云存储出网费的麻烦。
   - **免写分布式推理胶水代码**：现有数仓团队（Analytics Engineers）无需手写分片切块、限流退避、单行容错与谓词重排，一句 SQL 即可在 dbt / Lakeflow / Dynamic Table 调度流中零新增运维上线。其中，重度 Data+AI/ML 与百 TB 级长链路批 ETL 优先 **Databricks**（Photon + Liquid Clustering + `ai_query`）；多部门共享数仓、追求极低运维心智与开箱即用 AISQL 优先 **Snowflake**（Gen2 + Micro-Partition + Cortex AISQL）。
2. **什么时候选 StarRocks 等开源实时 OLAP？**
   - 追求极致性价比的高并发实时 BI、亚秒级湖仓直查与自主可控开源内核时，**StarRocks**（Morsel Pipeline + 全局字典 + 自适应 GRF）依然不可替代。面向下一代竞争，开源 OLAP 内核的演进方向也十分清晰：一是依托现有 CPU/Async IO 分离流水线平滑接入异步微批语义算子与 Token 代价 CBO；二是在高基数文本与 `VARIANT` Shredding 削弱低基数全局字典优势的背景下，补齐类似 Photon 的 Position List 与对标 Liquid Clustering 的后台自动聚簇；三是用轻量分布式 KV 替代单点重型元数据存储（如 BDBJE），跨过超大规模 Multi-Warehouse 与千万级 Shard 调度的门槛。
3. **什么时候必须坚定转向 Ray Data / Daft + vLLM / SGLang（或 NVIDIA NeMo Curator）？**
   - **亿级语料的绝对单 Token 成本**：面对数亿条预训练/多模态语料，用 Ray Data 挂 AWS/GCP Spot GPU + 自编译 **vLLM**（PagedAttention + Continuous Batching + Chunked Prefill，将 HBM 利用率推升至 85%+）或 **SGLang**（RadixAttention 复用树状共享前缀 KV Cache）跑 FP8/INT4 量化开源模型，单位 Token 成本能比数仓托管 API 低数倍甚至一个量级；百 TB/PB 级预训练语料的精确、模糊与语义去重则直接交给基于 Ray 的 **NVIDIA NeMo Curator** 跑全链路 GPU 加速。
   - **复杂异构编排与防锁定**：当管线需要将音视频抽帧、奇异二进制解析、多模型动态条件分支交错编排，或数据散落在多云裸 S3/OSS 不愿绑定单一数仓计费体系时，Python 分布式引擎拥有无可替代的控制权。
4. **学术界前沿框架（LOTUS / Palimpzest / DocETL）的定位**：
   它们正在为工业界指明下一代优化器的内核方向——**LOTUS（PVLDB 2025）** 给出带统计学准确率保证的 Proxy–Oracle 级联；**Palimpzest（CIDR 2025）** 通过 Abacus 优化器在延迟—成本—质量三维帕累托前沿自动寻优；**DocETL（UC Berkeley）** 用 Agentic 查询重写（MOAR 框架）自动拆解长合同、病历等单条 Prompt 极易幻觉失效的复杂文档任务。

| 决策维度 | **云数仓 AI Functions** (Databricks / Snowflake) | **开源实时 OLAP** (StarRocks) | **Ray Data / Daft + vLLM / SGLang** | **NeMo Curator (on Ray)** |
| :--- | :--- | :--- | :--- | :--- |
| **最佳主战场** | 企业仓内千万级增量非结构化 ETL、RAG 索引、强合规分析与百 TB 批 ETL | 高并发实时 BI、亚秒级湖仓直查、自主可控低延迟分析 | 亿级多模态批推理、Spot GPU 极致降本、音视频流式预处理 | 百 TB/PB 级预训练与后训练语料 GPU 去重与质量清洗 |
| **核心优势** | 零数据出域、原生 RBAC/血缘、声明式 SQO、零 GPU 集群运维 | Morsel 异步流水线、全局字典、自适应 GRF、极高性价比 | 极致单 Token 成本、CPU/GPU 异步背压精细控制、跨云防锁定 | 全链路 GPU 加速 MinHash/语义去重与 PII 脱敏 |
| **存储搭档** | Delta Lake + Liquid Clustering / Snowflake Micro-Partition | StarRocks Shared-Data / Iceberg / Parquet / Paimon | S3/OSS + **Lance v2.2** / Parquet / Paimon `.blob` | 高速并行文件系统 / 对象存储 + Parquet / Lance |

### 4.2 2026–2028 六大演进趋势

1. **语义查询优化器（SQO）全面下沉为数仓标配**：LOTUS 的统计学阈值级联、Palimpzest 的三维帕累托代价模型与 Snowflake 的多标签 `AI_JOIN` 重写，正从独立 Python 框架加速沉淀进主流云数仓与开源 OLAP 的原生 CBO Pass。
2. **Workload–Router–Pool 取代模型名硬编码**：管线不再在 SQL 或代码里写死单一模型，而是普遍引入轻量**语义路由器（Semantic Router / AI Gateway）**，按单行长度、意图复杂度与隐私等级动态分流至缓存命中、轻量分类器、开源 8B–70B 池或旗舰推理模型。
3. **KV-Cache 感知的批排序调度（数据引擎与推理引擎协同）**：过去数据管线按物理分片顺序盲发请求，导致前缀缓存命中率极低；新一代执行层会在微批投递前按**相同文档 ID、相同租户 Prompt 或相同 Build 侧候选集做局部重排（Prefix-Aware Ordering）**，成倍放大 SGLang RadixAttention 与 vLLM Prefix Caching 的吞吐红利。
4. **多模态表格式的“逻辑一统、物理分流”**：结构化标量与大体积音视频、文档在事务快照层统一治理，但在物理存储与扫描路径上彻底分流（如 Lance Blob v2、Paimon `.blob`、Snowflake `FILE` 与 MaxCompute `Object Table`），互不污染缓存行与内存缓冲。
5. **从无状态批处理 DAG 演进为 Durable Agentic ETL**：面对长合同推理、跨文档事实验证等单次前向调用搞不定的复杂加工，管线正融合 **DocETL 式的 Agentic 拆解重写**与 **Temporal 式持久化状态机编排**，支持算子级动态分叉、工具校验与断点状态恢复。
6. **Eval-in-the-Loop 成为与 Schema 校验同级的强制门禁**：由于语义算子具备概率性与模型版本漂移风险，未来的 LLM 数据管线不再只看作业退出码，而是像 Snowflake `AI_FUNCTION_OPTIMIZATION` 与 Databricks MLflow 闭环一样，将金标准抽样评测、准确率置信区间、幻觉告警与单行 Token 预算熔断直接嵌入发布与运行流水线。

---

> **参考资料与公开信息基线**
> - **云数仓与 OLAP 内核**：Alexander Behm et al., *Photon: A Fast Query Engine for Lakehouse Systems* (SIGMOD 2022) · Benoit Dageville et al., *The Snowflake Elastic Data Warehouse* (SIGMOD 2016) · Midhul Vuppalapati et al., *Building An Elastic Query Engine on Disaggregated Storage* (NSDI 2020) · Databricks 官方文档（Delta Lake Deletion Vectors、Liquid Clustering、UniForm & Iceberg v3、Dynamic File Pruning、`ai_query`、Foundation Model APIs Batch Inference、Lakebase）· Snowflake 官方文档（Gen2 Standard Warehouses、Dynamic Join Pruning、Cortex AISQL、`AI_FUNCTION_OPTIMIZATION`、AI Credits）· StarRocks 官方架构文档（Pipeline Execution Engine、Global Runtime Filter、Low-Cardinality Optimization、Shared-Data Architecture）。
> - **分布式 LLM 管线与推理栈**：Ray Data 官方文档 · Daft（Eventual）Swordfish/Flotilla 多模态引擎文档 · NVIDIA NeMo Curator 文档 · vLLM（PagedAttention / Guided Decoding / Semantic Router）与 SGLang（RadixAttention）技术资料。
> - **学术界前沿文献**：Liana Patel et al., *Semantic Operators and Their Optimization: Enabling LLM-Based Data Processing with Accuracy Guarantees in LOTUS* (PVLDB 2025) · Chunwei Liu et al., *Palimpzest: Optimizing AI-Powered Analytics with Declarative Query Processing* (CIDR 2025) · Shreya Shankar et al., *DocETL: Agentic Query Rewriting and Evaluation for Complex Document Processing* (UC Berkeley, 2024–2025)。
