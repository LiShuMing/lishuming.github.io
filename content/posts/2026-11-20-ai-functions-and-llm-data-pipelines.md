---
title: "AI Functions 与现代 LLM 数据管线：标准流程、非 SQL 实现原理与选型演进"
slug: "ai-functions-and-llm-data-pipelines"
date: 2026-11-20T20:55:00+08:00
lastmod: 2026-11-20T20:55:00+08:00
categories:
  - AI Infra
  - 数据库
tags:
  - AI Functions
  - AISQL
  - LLM
  - Ray Data
  - Daft
  - vLLM
  - SGLang
description: "拆解现代 LLM 数据管线的六阶段标准漏斗，结合生产实测数据对比 Snowflake Cortex AISQL、Databricks ai_query 与 Ray Data、Daft、NeMo Curator、LOTUS、DocETL 的技术选型逻辑，并逐一剖析典型 AI Functions 在非 SQL 栈中的受限解码、级联路由与树状归约实现原理。"
draft: false
toc: true
math: true
---

过去两年，几乎所有数据团队都写过类似的一段脚本：从对象存储或数仓里拉出一百万条客服工单、合同 PDF 或商品评论，起几十个 Python 进程循环调用大模型 API 做抽取与分类，再把返回的 JSON 塞回数据库。

只要把数据量再放大一个数量级，这套最朴素的做法会在四个地方同时失控：单行脏数据或 API 限流导致跑了六小时的夜间批作业整批崩溃；PDF/高清视频批量解码瞬间把 Worker 内存打爆（OOM）；每条记录都喂给旗舰大模型，一周烧掉上万美元 Token 费；更致命的是，大模型的自由生成偶尔多吐一个 Markdown 围栏或换个字段名，下游数仓表 Schema 当场断裂。

围绕这些真实工程痛点，工业界（Snowflake Cortex AISQL、Databricks `ai_query`、Ray Data、Daft、NVIDIA NeMo Curator、vLLM / SGLang）与学术界（LOTUS、Palimpzest、DocETL）在 2024–2026 年迅速收敛出一套分工清晰的现代 **LLM 数据管线（LLM Data Pipelines）** 架构。看懂这套架构的关键，在于先分清两类截然不同的业务场景，再顺着端到端漏斗拆透每个算子的底层原语。

![现代 LLM 数据管线六阶段漏斗与双轨技术栈协同架构](/images/diagrams/ai-llm-data-pipeline-and-aisql.svg)

---

## 一、先定边界：两类截然不同的 LLM 数据管线

强行用同一套工具同时服务“企业业务表增强”和“大模型预训练洗数”，是大量项目过度设计或成本爆炸的根源。当前生产落地中的 LLM 数据管线天然分为两极：

- **企业非结构化增强与 RAG / Agent 业务管线（Enterprise AI-ETL）**：每日增量处理数万至数千万条客服工单、通话转写、合同 PDF 与业务日志，完成结构化抽取、多标签分类、语义过滤/连接与向量索引更新。这类任务的核心约束在于**数据已经在数仓内、强合规禁止 PII 出域、下游 BI 与应用强依赖稳定 Schema 与血缘治理**。
- **大模型预训练 / 后训练多模态语料清洗与合成管线（Foundation Model Curation）**：处理百 TB 至 PB 级网页（如 Common Crawl）、代码、图像、音视频与合成推理轨迹，执行解码抽帧、精确/模糊/语义去重、PII 脱敏、质量打分过滤与批量蒸馏推理。这类任务的核心约束在于**极致压榨单 Token 与 Spot GPU 成本，以及解决 CPU 解码与 GPU 推理之间的异构背压失衡**。

---

## 二、端到端 LLM 数据管线的六阶段标准漏斗

无论上层采用数仓 AI SQL 还是自建分布式 Python 栈，生产级 LLM 数据管线都遵循一条铁律——**凡是能用微秒级 CPU 算子裁剪掉的数据，绝不送进百毫秒级 LLM**。完整的端到端处理通常拆为六个阶段：

1. **多模态接入与统一存储**：通过原生 `FILE` 类型（Snowflake）、`Object Table`（MaxCompute）、`Blob v2`（Lance v2.2）或 `.parquet + .blob`（Apache Paimon），将结构化标量与大体积音视频/文档纳入同一事务快照，避免表和文件版本脱节。
2. **解析与流式切块（Parsing & Chunking）**：将 PDF、扫描件、音视频流式解码为文本或视觉切片。为防止单批拉取海量高清多媒体撑爆内存，解析层必须施加严格的**流式背压（Streaming Backpressure）**。
3. **廉价确定性裁剪与去重**：先下推时间区间、业务状态等确定性关系过滤谓词；在训练语料场景下并行执行规则清洗、精确哈希去重、MinHash 模糊去重与 PII 脱敏，提前淘汰 70%–90% 的无效样本。
4. **语义路由与分级推理（Workload–Router–Pool）**：不再把所有存活行无脑发给同一个旗舰模型，而是经由轻量代理模型（Proxy，如向量距离、小分类器或 7B 量化模型）直接裁决高置信样本，仅将模糊灰区路由给昂贵的参考大模型（Oracle，即计算机理论中的“神谕/权威裁判”）；同时在投递给底层 **vLLM / SGLang** 推理池前按共享前缀聚簇微批，放大 KV Cache 复用。
5. **结构化约束与质量门禁**：用受限解码（Constrained Decoding / JSON Schema）锁死合法输出格式；配置单行死信隔离（如 `failOnError => false`）防止脏行拖垮整批任务；接入抽样评测（Eval-in-the-Loop）实时监控准确率与幻觉漂移。
6. **双轨落库与下游消费**：结构化抽取结果写回 Delta / Iceberg / Snowflake 表，同步刷新向量索引（HNSW / IVF / DiskANN）与全文倒排索引，供下游 BI、RAG 检索、Agent 长记忆或训练 DataLoader 消费。

---

## 三、典型 AI Functions 的用法、语义与非 SQL 等价实现原理

为了让“数仓 AI SQL”与“自建 Python / Ray 管线”之间的取舍落到代码级，下面先看六类最典型的 AI Functions 生产 SQL 写法，再逐一拆解它们脱离 SQL 后该如何用工业级非 SQL 原语实现。

### 3.1 典型 AI Functions 的 SQL 用法

以 **Snowflake Cortex AISQL** 与 **Databricks AI Functions** 为例，主流语义算子已收敛为六种基础形态：`AI_FILTER`（语义布尔过滤）、`AI_CLASSIFY`（有限集单/多标签分类）、`AI_EXTRACT`（非结构化转结构化字段）、`AI_COMPLETE` / `ai_query`（带强 Schema 与容错的通用批推理）、`AI_JOIN`（跨表语义连接，详细重写机制见姊妹篇 [Databricks、Snowflake 与 StarRocks 引擎内核横评]({{< relref "2026-11-20-databricks-snowflake-starrocks-engine-and-aisql.md" >}})），以及 `AI_AGG` / `AI_PARSE_DOCUMENT`（跨行分组语义汇总与多模态解析）。

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

### 3.2 非 SQL（Ray Data / Daft / vLLM / SGLang）中的等价实现与底层原理

如果在 Python 分布式栈中自建等价算子，绝不能停留在“调 Chat API + 正则解析”，每个算子背后都对应着一套扎实的推理与系统原语：

1. **`AI_FILTER` & `AI_CLASSIFY` \(\rightarrow\) 受限词表采样（Guided Choice）+ Logprobs 置信度级联**
   让模型吐出 50 个 Token 解释再写 `if "YES" in text`，不仅 Decode 耗时白白放大 50 倍，还极易漂移。工业级做法是向 **vLLM / SGLang** 传入 `guided_choice`（把合法输出锁死在给定标签词表）并指定 `max_tokens=1, logprobs=True`：模型仅需 **1 个 Decode 步**即可返回严格合法的标签，同时直接提取该 Token 的对数概率换算为置信度 \(p = \exp(\text{logprob})\)，用来驱动 Proxy 小模型 \(\rightarrow\) Oracle 大模型的阈值级联。

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

2. **`AI_EXTRACT` & `ai_query(responseFormat)` \(\rightarrow\) FSM / XGrammar 受限解码 + 死信列隔离**
   底层由 vLLM / SGLang 在推理前将 Pydantic / JSON Schema 编译成**有限状态机（FSM）或下推自动机（如 XGrammar / Outlines）**。每一步自回归采样前动态计算当前语法状态允许的合法 Token 集合，将非法 Token 的 Logits 全部置为 \(-\infty\)，从数学上保证输出 100% 符合目标 Schema；批处理 Actor 内部对单行异常捕获并写入 `error` 死信列，复刻 `failOnError => false` 的作业韧性。
3. **`AI_JOIN` \(\rightarrow\) 共享前缀多标签打包 / 向量 Blocking + 校准级联**
   右表较小时，把右表全部候选条目拼接进共享 System Prompt 前缀，对左表逐行跑受限多标签分类，借助 **SGLang RadixAttention** 复用前缀 KV Cache；双表都很大时，先用 Embedding + **Lance / FAISS（HNSW）** 召回 Top-\(K\) 候选对，再用小样本 Oracle 校准双阈值 \((\tau_{\text{low}}, \tau_{\text{high}})\)，让轻量 Proxy 过滤掉确定样本，仅将灰区送入大模型精判（即 **LOTUS `sem_join`** 范式）。
4. **`AI_AGG` / `AI_SUMMARIZE_AGG` \(\rightarrow\) Token 预算感知的分层树状归约（Hierarchical Map-Reduce）**
   单个 `GROUP BY` 分组内可能包含数千条评论，远超单次上下文窗口且易触发“中间遗忘（Lost in the Middle）”。非 SQL 管线借鉴分布式 SQL 的 `Partial Agg -> Final Agg`：按固定 Token 预算（如每桶 8K–16K Token）对组内文本分桶，并发生成保留关键频次与原句锚点的局部摘要，再递归归约直至装入最终合成窗口；面对高复杂度长合同聚合，则可引入 **DocETL** 的 `split-gather` / `reduce` Agentic 计划重写。
5. **`AI_PARSE_DOCUMENT` & `AI_EMBED` \(\rightarrow\) CPU/GPU 异构解耦与流式背压**
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

## 四、生产实测效果与技术选型背后的第一性原理

从 2025–2026 年公开的企业账单与基准数据来看，AI SQL 已在常规增量业务 ETL 中占据重要份额：在成熟的 Snowflake Cortex 生产环境里，**AISQL Token 函数（`AI_COMPLETE`、`AI_EXTRACT`、`AI_CLASSIFY`）约占平台 AI 总支出的 38%**，超过 Cortex Search（~28%）与 Analyst/Agents（~12%–18%）；伴随 Snowflake 将 AI Credits（约 \$2.00–\$2.20 / Credit）拆分重校，企业版及以上客户的 Token 密集型 AISQL 实际美元账单下降了 **40%–44%**；Databricks 的 Foundation Model API 批处理后端升级也将 `ai_query` 批吞吐拉高了 **10x 以上**。

这引出一个架构师常被问到的问题：**既然开源 Ray Data + vLLM 这么灵活，为什么大量企业依然在用数仓 AI SQL？反过来，什么时候又必须坚定放弃 AI SQL？**

核心分界线落在三笔硬账上：

1. **为什么选数仓 AI Functions（Snowflake AISQL / Databricks `ai_query`）？**
   - **合规与零出域红线**：金融、医疗与大型企业的敏感表禁止导出数仓安全域。AI SQL 直接继承 Unity Catalog / Snowflake RBAC、列级脱敏与血缘审计，省去了给外部 Ray 集群重做 VPC/IAM/Ranger 鉴权与云存储出网费的麻烦。
   - **免写分布式推理胶水代码**：开发者无需手写分片切块、限流退避、单行容错与谓词重排，一句 SQL 即可由现有数仓团队（Analytics Engineers）在 dbt / Lakeflow / Dynamic Table 调度流中零新增运维上线。
2. **为什么选 Ray Data / Daft + vLLM / SGLang（或 NVIDIA NeMo Curator）？**
   - **亿级语料的绝对单 Token 成本**：面对数亿条预训练/多模态语料，用 Ray Data 挂 AWS/GCP Spot GPU + 自编译 **vLLM**（PagedAttention + Continuous Batching + Chunked Prefill，将 HBM 利用率推升至 85%+）或 **SGLang**（RadixAttention 复用树状共享前缀 KV Cache）跑 FP8/INT4 量化开源模型，单位 Token 成本能比数仓托管 API 低数倍甚至一个量级（百 TB 级预训练语料的精确/模糊/语义去重则直接交给基于 Ray 的 **NVIDIA NeMo Curator** 跑全链路 GPU 加速）。
   - **复杂异构编排与防锁定**：当管线需要将音视频抽帧、奇异二进制解析、多模型动态条件分支交错编排，或数据散落在多云裸 S3/OSS 不愿绑定单一数仓计费体系时，Python 分布式引擎拥有无可替代的控制权。
3. **学术界新框架（LOTUS / Palimpzest / DocETL）的定位**：
   它们正在为工业界指明下一代优化器的内核方向——**LOTUS（PVLDB 2025）** 给出带统计学准确率保证的 Proxy–Oracle 级联；**Palimpzest（CIDR 2025）** 通过 Abacus 优化器在延迟—成本—质量三维帕累托前沿自动寻优；**DocETL（UC Berkeley）** 则用 Agentic 查询重写（MOAR 框架）自动拆解长合同、病历等单条 Prompt 极易幻觉失效的复杂文档任务。

| 决策维度 | **数仓 AI Functions** (`AISQL` / `ai_query`) | **Ray Data / Daft + vLLM / SGLang** | **NeMo Curator (on Ray)** |
| :--- | :--- | :--- | :--- |
| **最佳主战场** | 企业仓内千万级增量非结构化 ETL、RAG 索引构建、强合规分析 | 亿级多模态批推理、Spot GPU 极致降本、音视频流式预处理 | 百 TB/PB 级预训练与后训练语料 GPU 去重与质量清洗 |
| **核心优势** | 零数据出域、原生 RBAC/血缘、声明式优化、零 GPU 集群运维 | 极致单 Token 成本、CPU/GPU 异步背压精细控制、跨云防锁定 | 全链路 GPU 加速 MinHash/语义去重与 PII 脱敏 |
| **存储搭档** | Snowflake 内部表 / Delta Lake + Liquid Clustering | S3/OSS + **Lance v2.2** / Parquet / Paimon `.blob` | 高速并行文件系统 / 对象存储 + Parquet / Lance |

---

## 五、2026–2028 LLM 数据管线的六大演进趋势

1. **语义查询优化器（SQO）全面下沉为数仓标配**：LOTUS 的统计学阈值级联、Palimpzest 的三维帕累托代价模型与 Snowflake 的多标签 `AI_JOIN` 重写，正从独立 Python 框架加速沉淀进主流云数仓与开源 OLAP 的原生 CBO Pass。
2. **Workload–Router–Pool 取代模型名硬编码**：管线不再在 SQL 或代码里写死单一模型，而是普遍引入轻量**语义路由器（Semantic Router / AI Gateway）**，按单行长度、意图复杂度与隐私等级动态分流至缓存命中、轻量分类器、开源 8B–70B 池或旗舰推理模型。
3. **KV-Cache 感知的批排序调度（数据引擎与推理引擎协同）**：过去数据管线按物理分片顺序盲发请求，导致前缀缓存命中率极低；新一代执行层会在微批投递前按**相同文档 ID、相同租户 Prompt 或相同 Build 侧候选集做局部重排（Prefix-Aware Ordering）**，成倍放大 SGLang RadixAttention 与 vLLM Prefix Caching 的吞吐红利。
4. **多模态表格式的“逻辑一统、物理分流”**：结构化标量与大体积音视频/文档在事务快照层统一治理，但在物理存储与扫描路径上彻底分流（如 Lance Blob v2、Paimon `.blob`、Snowflake `FILE` 与 MaxCompute `Object Table`），互不污染缓存行与内存缓冲。
5. **从无状态批处理 DAG 演进为 Durable Agentic ETL**：面对长合同推理、跨文档事实验证等单次前向调用搞不定的复杂加工，管线正融合 **DocETL 式的 Agentic 拆解重写**与 **Temporal 式持久化状态机编排**，支持算子级动态分叉、工具校验与断点状态恢复。
6. **Eval-in-the-Loop 成为与 Schema 校验同级的强制门禁**：由于语义算子具备概率性与模型版本漂移风险，未来的 LLM 数据管线不再只看作业退出码，而是像 Snowflake `AI_FUNCTION_OPTIMIZATION` 与 Databricks MLflow 闭环一样，将金标准抽样评测、准确率置信区间、幻觉告警与单行 Token 预算熔断直接嵌入发布与运行流水线。

---

> **参考资料与公开信息基线**
> - **工业界平台与开源栈**：Snowflake Cortex AISQL、`AI_FUNCTION_OPTIMIZATION`、AI Credits 与 AI Gateway 官方文档 · Databricks `ai_query` / Foundation Model APIs Batch Inference / Lakeflow 官方文档 · Ray Data 官方文档 · Daft（Eventual）Swordfish/Flotilla 多模态引擎文档 · NVIDIA NeMo Curator 文档 · vLLM（PagedAttention / Guided Decoding / Semantic Router）与 SGLang（RadixAttention）技术资料。
> - **学术界前沿文献**：Liana Patel et al., *Semantic Operators and Their Optimization: Enabling LLM-Based Data Processing with Accuracy Guarantees in LOTUS* (PVLDB 2025) · Chunwei Liu et al., *Palimpzest: Optimizing AI-Powered Analytics with Declarative Query Processing* (CIDR 2025) · Shreya Shankar et al., *DocETL: Agentic Query Rewriting and Evaluation for Complex Document Processing* (UC Berkeley, 2024–2025)。
