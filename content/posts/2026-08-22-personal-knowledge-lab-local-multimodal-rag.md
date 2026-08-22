---
title: "给自己的照片和文档建一套可解释的本地检索：Personal Knowledge Lab 阶段复盘"
slug: "personal-knowledge-lab-local-multimodal-rag"
date: 2026-08-22T19:30:00+08:00
lastmod: 2026-08-22T19:30:00+08:00
draft: false
categories:
  - AI Infra
tags:
  - RAG
  - Multimodal
  - Local AI
  - vLLM
  - CLIP
  - Qwen-VL
  - Personal Knowledge Management
summary: "从个人照片与文档出发，复盘一套由 Qwen2.5-VL、CLIP、文本检索、人脸聚类、SQLite 队列和 vLLM 组成的本地多模态 RAG；重点讨论数据建模、证据边界、检索失败与评测体系。"
description: "Personal Knowledge Lab 的架构、实现与阶段反思：本地优先的多模态索引、可解释检索、GPU 服务边界，以及为什么评测比再接一个模型更重要。"
toc: true
math: true
---

前一段时间，受朋友讨论社交与 AI 的影响，加上在播客里频繁听到“端侧模型”，我开始重新看待电脑里沉睡的个人数据：照片、视频、语音和零散文档过去只是等待被翻阅的文件；当多模态模型具备理解画面、文字和上下文的能力后，它们有机会成为一套可以检索、关联和重新认识自己的长期记忆。

这也是我尝试做 `personal-knowledge-lab` 的起点：用 Vibe Coding 搭建一个面向自己照片和文档的本地知识实验室。原始文件留在 Windows 目录并保持只读，索引、缩略图、任务状态和日志存放在 WSL；搜索能够组合中文描述、物体、OCR、时间、地点和人物，结果还要解释“为什么找到这张照片”。

项目目前没有达到我对搜索质量的预期，也因此暂时搁置。但这并不只是一次失败的 Demo。它把多模态 RAG、GPU 推理服务、增量索引、后台任务、数据版本和检索评测放进了同一个真实系统，使我更清楚地看到：

> 个人知识库首先是一个数据与证据系统，其次才是模型应用。模型决定能力上限，数据建模、索引版本和评测闭环决定它是否真的可用。

本文基于 `personal-knowledge-lab`、独立的 `pkl-vllm-service`、本地 SQLite 索引快照和运行日志完成。文中会明确区分已经落地的实现与仍处于设计阶段的能力，不把 roadmap 写成结果。

## 从本地优先开始，而不是从模型开始

“本地优先”并不等于把云端 API 地址改成 `localhost`。它更接近 [Ink & Switch 对 local-first software 的定义](https://www.inkandswitch.com/essay/local-first/)：用户拥有数据，应用在离线状态仍然可工作，网络或供应商不应成为访问个人资料的前置条件。

在这个项目里，我把它落实成四条约束：

1. Windows 中的照片和文档是只读事实源，扫描和索引不能改名、移动或删除原文件。
2. SQLite、向量、缩略图和日志都是可重建的派生数据；模型升级不能污染原始资料。
3. 视觉理解、embedding、人脸检测和检索在本机完成。环境里即使残留 `LLM_*` 配置，应用也不读取或转发其值。
4. 人脸模型只发现“相似的人脸”，真实姓名必须由用户确认，不能让 VLM 根据面孔猜身份。

从数据库的角度看，这套结构并不陌生：

| 数据库概念 | Personal Knowledge Lab 中的对应物 |
| --- | --- |
| Base table / source of truth | Windows 上的原始照片与文档 |
| Catalog | SQLite 中的 source、file、asset、模型与任务版本 |
| Materialized view | caption、OCR、缩略图、文档 chunk |
| Secondary index | CLIP、文本和人脸向量 |
| Index maintenance | 后台扫描、补索引、失败重试与版本失效 |
| Query planner | 意图识别、filter 下推和检索路由 |
| EXPLAIN | `match_reason`、namespace 分数和命中证据 |

这个类比也带来一个重要结论：caption 和 embedding 不是“内容本身”，只是可以被丢弃、重算和迁移的索引。更换模型、prompt、pooling 或向量维度，本质上都相当于一次索引格式升级。

## 当前架构：让 Qwen 负责采集，而不是在线搜索

早期最自然的想法，是每次查询都让视觉大模型逐张看图并回答“是否相关”。它的问题也很直接：延迟随照片数量线性增长，GPU 成本高，结果不可重复，并且难以说明究竟是哪条证据命中。

因此现在的 Qwen2.5-VL 被放在离线索引阶段，角色是 `context collector`：

```text
只读原始文件
  ├── 确定性解析
  │     ├── SHA-256 / 文件属性
  │     ├── EXIF / GPS / 拍摄时间
  │     └── 文档正文 / 页码 / chunk
  │
  ├── 原始视觉索引
  │     └── CLIP image embedding
  │
  └── 语义采集
        ├── Qwen2.5-VL -> 结构化 photo context
        ├── 文本 embedding -> 多个语义 namespace
        └── InsightFace -> face embedding / person cluster

查询
  -> 意图解析与过滤条件
  -> 多路召回
  -> Weighted RRF
  -> 结果、证据与 match_reason
```

[Qwen2.5-VL](https://arxiv.org/abs/2502.13923) 负责把像素转成可检索的结构化描述；[CLIP](https://arxiv.org/abs/2103.00020) 保留原始画面的跨模态召回能力；[InsightFace](https://github.com/deepinsight/insightface) 提供人脸检测和 embedding。在线查询只访问已经生成的索引，不再把整套相册交给 VLM 重看一遍。

这与经典 RAG 的 ingestion、indexing、retrieval、synthesis 分层类似，可以参考 [LlamaIndex 对 RAG 阶段的划分](https://github.com/run-llama/llama_index/blob/main/docs/src/content/docs/framework/understanding/rag/index.mdx)。但框架只能帮助编排组件，并不会自动解决数据归属、模型版本、幂等任务和评测问题，这些仍然是应用自己的系统责任。

## Context 不是一段更长的 caption

照片索引最危险的问题，不是模型漏掉一个物体，而是把“看起来合理的推断”写成事实。例如室内装饰可能像杭州某家餐厅，但画面本身不能证明地点；两个人站在一起也不能证明关系。

当前 `photo_context_v2` 把内容拆成三层：

```json
{
  "observations": {
    "caption": "两个人坐在桌前，桌面上有一台笔记本电脑",
    "objects": ["笔记本电脑", "咖啡杯"],
    "actions": ["交谈"],
    "visible_text": []
  },
  "inferences": [
    {
      "type": "event",
      "value": "技术讨论",
      "confidence": 0.63
    }
  ],
  "metadata": {
    "captured_at": "EXIF 中的拍摄时间",
    "gps": "EXIF 中的经纬度",
    "asset_type": "photo"
  }
}
```

- `observations` 只放画面中可以直接观察的对象、动作、场景、颜色和文字。
- `inferences` 保存地点、事件、时间或关系等推断，每项必须携带置信度。
- `metadata` 保存 EXIF、GPS、文件属性和用户确认的人物等确定性信息。

向量构造器只接收超过阈值的推断。姓名、时间和 GPS 更适合成为 filter 或结构化条件，而不是拼进 caption 后让 cosine similarity 碰运气。人脸 prompt 也明确禁止由面孔猜真实身份。

这里还保留了一个渐进迁移设计：索引失效版本仍叫 `photo_context`，payload schema 已升级为 `photo_context_v2`。前者控制重建批次，后者控制字段语义。这样不必在改 schema 的一刻让所有照片同时失效，但旧记录是否已经重建必须变得可观测。

## 多路检索：不要直接相加两个不同空间的分数

每张图片目前有一条 `clip:image` 向量，也有 caption、dense caption、object、activity、OCR、location、time、event、metadata 等文本向量。不同向量空间的 cosine score 不具备天然可比性：CLIP 的 0.31 与文本模型的 0.78，不能因为都是小数就直接相加。

当前实现让每个检索器先独立排序，再使用 [Reciprocal Rank Fusion（RRF）](https://cormack.uwaterloo.ca/cormack/cormacksigir09-rrf.pdf) 合并：

$$
\operatorname{RRF}(d)=\sum_{r\in R}\frac{w_r}{k+\operatorname{rank}_r(d)}
$$

其中 \(R\) 是召回通道，\(w_r\) 是通道权重，\(k\) 用于降低头部名次过强带来的抖动。RRF 使用排名而非原始分数，适合先解决多个模型分值尺度不一致的问题。

查询规划目前仍是可解释的规则系统：

- “去年”“2025 年”进入时间条件；
- 城市词与 GPS/地点字段建立候选；
- “写着”“报错”“函数”“白板”提高 OCR 通道权重；
- 已命名的人物别名先解析为 face cluster；
- 物体和场景描述同时进入 CLIP 与文本召回。

响应保留各 namespace 的分数、命中词和 `match_reason`。这类似数据库的 `EXPLAIN`：用户看到的不能只是“相关度 0.83”，而应该知道它来自画面相似、文字命中、人物聚类，还是时间与地点条件。

当前的 `confidence` 只是融合特征推导出的启发式值，并不是经过校准的概率。把它显示成“83% 正确”会制造并不存在的精确感。

## 人脸能力：聚类不是身份事实

人物链路目前是：

```text
照片
  -> InsightFace 检测与 face embedding
  -> 与 cluster centroid 比较
  -> person cluster
  -> 用户命名 / alias / ignore
  -> 重建受影响照片的 people context
```

cluster centroid 使用增量均值更新。用户把一个 cluster 命名为“乔乔”后，`refresh_person_vectors` 任务才会把名字和别名写入相关照片的检索 context。因此“找乔乔在地铁里的照片”由用户确认的 cluster、视觉场景和其他证据联合完成，不依赖模型凭脸猜人。

本地快照有 1,227 次人脸检测和 464 个 cluster，其中仍有 96 张图片等待人脸处理。这个数字首先暴露的是评测问题：阈值可能把同一个人拆得过细，也可能错误合并相似面孔。成熟的开源照片系统 [Immich 的人脸识别](https://immich.app/docs/features/facial-recognition) 同样把检测、embedding、聚类和用户命名分开，并提供合并、隐藏等人工修正路径。这里真正值得借鉴的不是“也有一个 People 页面”，而是允许用户持续纠正聚类结果。

## 后台队列：索引是一项长期的数据维护工作

FastAPI 启动后，`QueueController` 周期性扫描 SQLite 任务表，补入未索引照片、待处理人脸和到期的文档扫描任务。worker 通过条件更新 claim 任务，`ProcessingRun` 记录开始/结束时间、worker、模型 profile、输出统计和错误。

配置允许最大并发 4，但它只是上限。自动调度会读取 GPU 空闲显存：

- 空闲显存达到 24GB，视觉任务最多并发 2；
- 达到 8GB，最多并发 1；
- 更紧张时只保留轻量任务，或暂停视觉 worker。

目标机器是一张 16GB RTX 5070 Ti，实际策略通常只启动一个视觉 worker。这个结果没有“并发 4”好看，却更符合真实资源边界。让 Qwen、CLIP、文本 embedding 和 InsightFace 一起争抢显存，最终 OOM、模型反复加载或进入 swap，并不会提高吞吐。

SQLite 适合承担当前规模下的 catalog 和任务状态，但必须正视它的并发边界：[SQLite WAL](https://www.sqlite.org/wal.html) 可以让 reader 与 writer 并行，仍然同时只有一个 writer。当前连接配置没有显式启用 WAL，任务增加后应该将 journal mode、busy timeout、短事务和写入批次一起纳入压测，而不是只调 worker 数量。

## 为什么把 vLLM 从应用仓库拆出去

`personal-knowledge-lab` 负责业务状态，`pkl-vllm-service` 只负责模型运行时：

```text
React / FastAPI / SQLite worker
       |
       | OpenAI-compatible localhost request
       v
VisionServiceClient
       |
       | 127.0.0.2:8100/v1/chat/completions
       v
pkl-vllm-service
       |
       | Qwen2.5-VL-3B-Instruct + vLLM + CUDA
       v
结构化 JSON（没有应用数据库写权限）
```

应用把压缩后的图片编码为 data URL，与严格 JSON prompt 一起调用 `/v1/chat/completions`；温度为 0，输出上限 512 tokens。模型服务没有数据库权限，失败时由 worker 决定重试、降级或记录错误。

拆分之后有三个收益：

1. Web 应用环境不再被 Torch、CUDA 和 vLLM 的兼容矩阵绑死；
2. 模型的加载、显存、升级和 warmup 成为独立故障域；
3. 应用只依赖稳定的 endpoint 和模型别名，不把 Hugging Face snapshot 路径写进业务逻辑。

当前服务环境记录为 Python 3.12、vLLM 0.25.0、Torch 2.11.0+cu130、CUDA runtime 13.0；模型是 Qwen2.5-VL-3B-Instruct，服务名为 `qwen-vl-photo-context`。vLLM 通过连续批处理和 KV cache 管理提高吞吐，其核心思想可以追溯到 [PagedAttention 论文](https://arxiv.org/abs/2309.06180)，最新运行时能力则以 [vLLM 官方文档](https://vllm.ai/) 为准。

WSL 里还有一个很具体的工程教训：这台机器的 `127.0.0.1` TCP policy route 会导向异常 loopback 设备，进程虽然显示监听，客户端却收到连接拒绝。使用普通 HTTP server 复现后，服务改为绑定 `127.0.0.2:8100`，仍只暴露在本机 loopback。很多“模型服务故障”最终是网络、环境或进程边界问题，不能仅靠重装模型解决。

## 视觉 token、像素预算和基准数字

Qwen2.5-VL 不会直接把 JPEG 字节送入语言模型。图片先被处理成视觉 token，与 prompt 一起完成 prefill，随后才生成 JSON。像素越多，视觉 token 越多，prefill 计算与 KV cache 压力也随之增加。

服务端限制每个 prompt 一张图片，最大约 2,007,040 pixels；应用端还有更保守的 `768 × 768` 默认缩放。客户端上限控制常规照片的索引成本，服务端上限保护所有调用者。需要读取小字的图片，未来应该进入专门的 OCR 路由，而不是统一把每张生活照都放大。

当前 vLLM 配置为：

```text
max_model_len=4096
max_num_seqs=8
max_num_batched_tokens=4096
gpu_memory_utilization=0.82
dtype=bfloat16
limit_mm_per_prompt.image=1
```

在 1024px 合成图、客户端并发 8 的微基准里，`max_num_seqs=8` 达到约 21.95 req/s、P95 0.38s、31,220 tokens/s，且没有 preemption；`max_num_seqs=4` 的基线约为 13.01 req/s、P95 0.69s、18,501 tokens/s。

这个数字证明 continuous batching 有效，但不能翻译成“真实相册每张只需 0.38 秒”。真实 worker 日志中，每张照片通常需要 4.7～7.2 秒，少数超过 10 秒；其中还包括图片读取与缩放、请求传输、JSON 解析、CLIP/文本向量、人脸处理和数据库写入。微基准回答运行时容量，端到端日志回答用户实际等待时间，两者不能混用。

## 一次很关键的模型身份漂移

项目配置中请求的文本模型是 `BAAI/bge-m3`。[BGE-M3](https://arxiv.org/abs/2402.03216) 的价值在于多语言，以及 dense、lexical、multi-vector 等多种检索表示。但检查运行代码、模型缓存和数据库后，实际情况是：

1. 本机没有 BGE-M3 cache；
2. resolver 静默回退到 `BAAI/bge-small-zh-v1.5`；
3. SQLite 中所有文本向量都是 512 维，与这个回退模型一致。

也就是说，原来的“使用 BGE-M3”只是配置意图，不是运行事实。这比某次搜索结果不好更值得警惕：模型名、revision、维度、pooling、normalize 和 prompt 模板共同定义一个向量空间，任一项改变都意味着索引版本改变。静默 fallback 会让旧向量和新查询看似兼容，实际语义却已经漂移。

后续必须把两类配置分开记录：

```text
requested_model = BAAI/bge-m3
resolved_model  = BAAI/bge-small-zh-v1.5
revision        = ...
dimension       = 512
pooling         = mean
normalized      = true
index_version   = ...
```

worker 启动时应验证数据库中向量的模型指纹；不一致就拒绝混写并触发显式 reindex。这本质上不是 ML 细节，而是 schema compatibility。

## 为什么图片搜索没有达到预期

当前数据库快照包含：

| 项目 | 数量 |
| --- | ---: |
| source roots / files | 2 / 631 |
| documents / chunks | 102 / 2,293 |
| indexed images | 529 |
| photo vectors | 6,715 |
| processing tasks / runs | 4,627 / 4,646 |
| face detections / clusters | 1,227 / 464 |

所有 529 张照片已经生成 context 和 `clip:image`；数据库中可见的向量都是 512 维。代码测试也可以通过：

```bash
PYTHONDONTWRITEBYTECODE=1 \
python3 -m pytest -p no:cacheprovider backend/tests -q
# 10 passed
```

但“pipeline 全部成功”并不代表“搜索足够好”。结合代码与数据，我认为主要瓶颈来自以下几个层面。

### 1. 中文查询与英文 CLIP 的能力错位

当前视觉模型是 `openai/clip-vit-base-patch32`。它适合验证 text-to-image 的基本链路，但原始训练数据与中文查询并不匹配，ViT-B/32 对细粒度物体和小文字也有限。Immich 的 [搜索文档](https://docs.immich.app/features/searching/) 同样明确提醒：非英语查询应选择合适的多语言模型。

下一步不是随意换一个“更大 CLIP”，而是用真实中文 query 对比 multilingual CLIP/SigLIP 类模型，观察 Recall@10、显存、索引时间和向量维度的整体变化。

### 2. VLM context 是有损表示

caption 将高维画面压缩成有限文本。没被模型写出来的细节，后续文本 embedding 无法恢复；被模型猜错的内容则会变成新的噪声。observation/inference 分层可以降低污染，但不能消除信息损失。

因此原始视觉向量、OCR、结构化元数据和 VLM context 应当互补，不能让一段 dense caption 成为照片的唯一真相。

### 3. OCR 还不是独立的一等检索器

当前 visible text 主要来自 VLM 输出，数据库中只有 63 张照片拥有 OCR namespace。截图、白板、票据和报错信息需要专门 OCR、文本规范化和 lexical/BM25 检索。精确错误码或函数名交给 dense embedding，往往不如倒排索引可靠。

### 4. 检索仍是 Python 中的线性扫描

`photo_vectors` 以 JSON 存在 SQLite，查询时读入 Python 计算 cosine。在 529 张图上可以工作，也便于观察每条向量；继续扩展到几万张图后，内存、延迟和过滤顺序都会成为问题。

[Qdrant](https://qdrant.tech/documentation/manage-data/) 可以提供 ANN、payload filter 和更成熟的索引维护；其 [collection、named vectors 与 alias](https://qdrant.tech/documentation/manage-data/collections/) 也适合当前的多 namespace 与无停机迁移。但迁移向量数据库只解决规模和工程效率，不会自动修好中文 CLIP、坏 caption 或错误权重。

### 5. 权重和置信度还没有数据依据

当前意图规则、RRF 权重和 confidence 都来自工程经验。没有标注 query 集时，很容易对几条印象深刻的失败样例反复调参，最后得到只对这些样例有效的系统。

### 6. 文档检索仍是 baseline

照片已经有实验性的多路语义检索，但文档目前主要是本地 parser、chunk 与 TF-IDF，不是完整的 BGE-M3、sparse+dense、rerank pipeline。照片和文档也不是同一种资产：PDF 需要页码、版面、表格、代码和公式，照片需要人物、时空、OCR 与视觉场景。所谓统一检索，不应等同于把所有内容塞进一个 embedding。

## 与已有开源项目的关系

如果目标只是获得成熟的本地照片管理产品，[Immich](https://docs.immich.app/developer/architecture/) 已经提供移动端备份、PostgreSQL、机器学习服务、人物、OCR、地点和语义搜索。重新实现一套相册产品并没有明显价值。

Personal Knowledge Lab 的价值在另一个方向：

- 同时研究照片与文档，而不是只做照片备份；
- 保留每条结果的召回通道和证据，观察检索为何成功或失败；
- 把 prompt、模型、向量和任务都当成有版本的数据产品；
- 在单机 WSL 与有限 GPU 上研究调度、降级和可恢复性；
- 允许替换每个组件并做对照实验。

LlamaIndex、Haystack 等框架适合快速搭建 RAG orchestration；Immich 适合直接使用；Qdrant 适合向量规模增长之后的基础设施。这个项目更像一个个人 AI/数据库实验台。它不应该以“代码都是自己写的”为目标，而应该借鉴成熟项目的边界，再把精力放到可解释性、评测与跨资产检索上。

## 下一步：让评测成为 Vibe Coding 的规格

这次最深的反思，是 Vibe Coding 极大降低了生成 pipeline 和页面的成本，却没有自动生成产品判断。任务成功数、页面能打开、单元测试通过，只能证明系统在运行，不能证明用户找到了想要的内容。

下一阶段如果继续，我会先暂停堆组件，建立一个小而真实的离线评测集：

1. 从自己的使用方式收集 80～150 条 query，覆盖人物、时间、地点、物体、场景、OCR、文档和跨条件组合；
2. 为每条 query 标注相关资产与必要证据，并保留“没有正确结果”的样例；
3. 固定 train/dev/test 或至少固定回归集，避免每次只挑有利案例；
4. 记录 Recall@5/10、MRR、nDCG、zero-result rate、P50/P95 延迟和索引成本；
5. 分通道做 ablation：CLIP only、context only、OCR、metadata、RRF、rerank；
6. 把用户的“相关/不相关、人物合并、名称修正”变成下一轮标注，而不是只改当前页面。

在这个基线之上，优化顺序才清晰：

```text
模型与索引指纹可观测
  -> 中文/多语言视觉 encoder 对比
  -> 专用 OCR + BM25/sparse
  -> 人物、时间、地点的 filter 前推
  -> Qdrant named vectors 与可回滚 alias
  -> 只对小候选集做 cross-encoder/VLM rerank
  -> 用户反馈进入持续评测
```

这也可以借鉴数据库优化器的思路：先有 workload 和 profile，再调整索引、执行计划与代价模型。没有 workload 的“性能优化”容易沦为跑分，没有标注集的“RAG 优化”也容易沦为 Demo 调参。

## 阶段结论

这轮实践让我形成了几个更确定的判断。

第一，VLM 更适合离线提取候选事实，而不是成为每次查询的在线循环。这样才能控制延迟、显存和不可重复性。

第二，原始文件、确定性元数据、模型观察、模型推断和用户确认必须拥有不同的可信度边界。把它们全部揉成一段 caption，后面换任何 embedding 都无法恢复证据。

第三，模型、prompt 和 embedding 都是索引 schema 的一部分。`requested model` 不等于 `resolved model`，静默 fallback 是比一次 bad case 更严重的系统错误。

第四，队列、版本、日志和 `match_reason` 不是外围设施。它们决定系统能否从失败中恢复，能否解释结果，也决定一次升级能否安全重建。

最后，个人知识库真正困难的部分，不是“把 Qwen、CLIP、BGE、Qdrant 接起来”，而是持续回答这些问题：

> 它找到了什么？为什么找到？漏掉了什么？哪一条是事实，哪一条只是推断？模型变化后旧索引还可信吗？数据是否始终留在自己手中？

因此这个项目虽然暂时停下，但它已经完成了更重要的一步：从一个“本地多模态搜索 Demo”，转向一套可以被验证、被解释、被迁移的数据系统。下一次继续时，我不会先再接一个模型，而会先让评测成为系统的规格。
