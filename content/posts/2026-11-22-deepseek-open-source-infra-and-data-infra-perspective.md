---
title: "解剖 DeepSeek 开源技术栈：从训练推理软硬协同到 Data Infra 视角的利弊反思"
slug: "deepseek-open-source-infra-and-data-infra-perspective"
date: 2026-11-22T12:15:00+08:00
lastmod: 2026-11-22T12:15:00+08:00
categories:
  - AI 基础设施
  - 数据库
tags:
  - DeepSeek
  - 3FS
  - Smallpond
  - DeepEP
  - DeepGEMM
  - FlashMLA
  - DualPipe
  - Data Infra
  - LLM
description: "从硬件物理约束切入，系统拆解 DeepSeek 开源周全套训练、推理与存储数据栈（FlashMLA、DeepEP、DeepGEMM、DualPipe、EPLB、3FS、Smallpond），对比其与其他 AI 大厂及通用开源生态的差异，并从 Data Infra 视角深度剖析其锋利优势与落地短板。"
draft: false
toc: true
---

2025 年初的 **DeepSeek Open Source Week（开源周）** 在 AI 基础设施圈扔下了一连串深水炸弹：连续六天，DeepSeek 把支撑 671B 参数 DeepSeek-V3 / R1 训练与推理的核心底层组件——**FlashMLA、DeepEP、DeepGEMM、DualPipe、EPLB、3FS、Smallpond** 以及完整的在线推理系统架构——几乎毫无保留地摊开在了 GitHub 上。

如果只把它们看成几个“跑分很高的开源仓库”，其实错过了最核心的东西。**DeepSeek 技术栈真正的震撼之处，在于它是被极端硬件约束倒逼出来的“算法—内核—通信—存储”全栈垂直协同。** 在受限于 **NVIDIA H800**（机内 NVLink 带宽相比 H100 直接腰斩至 400 GB/s）且算力卡数远不如硅谷巨头的物理边界下，DeepSeek 无法靠堆十万张卡掩盖通用框架的抽象损耗，只能一路下潜到 CUDA、PTX、IBGDA（GPU 直连 RDMA）与 NVMe 裸盘，把每一块硅片的带宽和算力榨干。

这篇文章从第一性原理出发，依次拆解 DeepSeek 在**训练、推理、存储与数据预处理**四层的技术栈全貌，横向对比其与硅谷实验室及通用开源生态的差异，最后专门站在 **Data Infra（数据基础设施）** 的视角，冷静评估这套“极客裸金属路线”到底哪里值得抄作业、哪里又是普通企业绝对不能盲目照搬的深坑。

---

## 一、训练侧技术栈：在“半血 NVLink”的物理限制下压榨极限 MFU

DeepSeek-V3/R1 是一套典型的超大规模稀疏混合专家模型（**MoE**：总参 671B、单 Token 仅激活 37B）。训练这类模型在硬件上最大的敌人有三个：**FP8 低精度矩阵乘法的数值漂移、跨节点专家并行（EP）的 All-to-All 通信风暴，以及流水线气泡与热点专家负载倾斜**。DeepSeek 逐一造出了手术刀级别的专用组件：

### 1.1 `DeepGEMM`：300 行 JIT FP8 GEMM 破解细粒度缩放与累加漂移

在 LLM 训练与推理中，矩阵乘法（GEMM）吃掉了 80%–90% 的时间。为什么 DeepSeek 不直接调用现成的 cuBLAS？

因为 NVIDIA Hopper（H800/H100）的 FP8 Tensor Core（`WGMMA`）在硬件内部做长向量累加时，有效尾数精度偏低，直接用常规粗粒度张量级缩放（Tensor-wise Scaling）跑 FP8 训练极易出现梯度数值发散；同时 MoE 层需要大量形状不一的 **Grouped GEMM**。

**DeepGEMM** 给出的解法极其精炼（核心代码仅约 300 行）：
- **细粒度分块缩放（Fine-Grained Scaling）**：激活值按 `1×128`、权重按 `128×128` 维度的分块做独立缩放因子量化，稳住 FP8 数值精度；
- **定期升精度回写 CUDA Core**：每隔若干个 `WGMMA` 累加步，就将局部结果周期性提升到 FP32 CUDA Core 中做高精度累加，在不牺牲 Hopper Tensor Core 吞吐的前提下抹平累加漂移；
- **轻量 JIT 编译 + TMA 异步加载**：摒弃传统矩阵库庞大的预编译模板膨胀，按实际矩阵维度即时编译，并用 Hopper **TMA（Tensor Memory Accelerator）** 异步搬运数据，同时原生支持稠密 GEMM 与 MoE 连续/掩码分组 GEMM。

### 1.2 `DeepEP` + `DualPipe` + `EPLB`：把 MoE 跨节点通信藏进计算缝隙

在 H800 NVLink 带宽减半的条件下，如果像传统框架那样先做机内 Tensor Parallelism（TP）或串行等 All-to-All 专家路由完成再计算，GPU 一大半时间都会在空等网络。DeepSeek 用三件套把通信墙彻底拆解：

- **`DeepEP`（MoE 专家并行通信库）**：
  - 放弃通用集合通信库（NCCL）的厚重封装，针对 MoE 的 `Dispatch`（分发 Token 给专家）与 `Combine`（聚合专家输出）设计了**双轨 Kernel**：
    1. **高吞吐模式（服务训练与 Prefill）**：精细编排机内 NVLink 与机间 InfiniBand RDMA 的非对称转发，吃满双向带宽；
    2. **超低延迟模式（服务在线 Decode）**：基于 **IBGDA（InfiniBand GPUDirect Async） / NVSHMEM** 让 GPU 直接操控网卡发起微秒级纯 RDMA 传输，并创新性地采用**基于 Hook 的背景通信重叠机制**，在后台收发数据时**不占用 SM 计算核心**。
- **`DualPipe`（双向流水线并行）**：
  - 传统 1F1B 流水线并行存在明显的首尾气泡（Pipeline Bubble），且难以完全隐藏跨节点 All-to-All 通信。**DualPipe** 从流水线两端同时向中间双向喂入微批次，让一条微批次的 Forward 与另一条微批次的 Backward 严格对称重叠——**把整个 MoE 跨节点通信几乎 100% 掩盖在计算时间里**（核心工程代价是显存里需要持有双份模型参数权重，属于典型的“以空间换零通信暴露”）。
- **`EPLB`（Expert Parallelism Load Balancer）**：
  - 真实语料中不同专家的路由热度极度不均，一旦少数专家过热就会拖慢整组同步步调。EPLB 统计历史路由热度，通过分层/全局算法将热点专家动态复制到多张冗余 GPU 卡上分摊流量，消除 MoE 长尾慢节点。

---

## 二、推理侧技术栈：MLA 算法协同、超大跨节点 EP 与磁盘 KV Cache

如果说训练比拼的是 MFU（模型算力利用率），那么在线推理比拼的就是**显存容量、HBM 带宽与单位 Token 成本**。DeepSeek 在推理侧最精彩的地方在于**从模型结构设计之初就把系统硬件账算进去了**：

### 2.1 `MLA` + `FlashMLA`：先从数学上把 KV Cache 压薄，再贴着 Hopper 写内核

传统 MHA（多头注意力）甚至 GQA 在长上下文和高并发下，KV Cache 体积动辄占满几十 GB 显存，导致 Decode 阶段彻底卡死在 HBM 带宽墙上。

- **算法层（MLA, Multi-Head Latent Attention）**：将庞大的 Key/Value 投影压缩为一个极小的低秩潜在向量（Latent Vector）存入 KV Cache，把单 Token 显存占用压缩了一个数量级，使单卡能够容纳极大并发 Batch Size。
- **内核层（`FlashMLA`）**：专为 Hopper（H800/H100）MLA 解码打造的定制内核，支持 Paged KV Cache（Block Size 64）与 BF16/FP8，精细压榨片上共享内存（用到单个 SM 228 KB 中的约 224 KB），把 H800 的显存带宽利用率推向物理极限（实测可达 ~3,000 GB/s 级别）。

### 2.2 大规模跨节点 EP（EP32 / EP144+）+ PD 分离 + 双微批重叠

在官方公开的 DeepSeek-V3/R1 推理系统架构中，有三个颠覆常规部署习惯的设计：

1. **Prefill 与 Decode 物理分离（PD Disaggregation）**：将算力密集型（Compute-bound）的 Prefill 节点与显存带宽密集型（Memory-bound）的 Decode 节点彻底拆开，消除混部导致的队头阻塞（TTFT 与 TPOT 互相拖累）。
2. **激进的超大跨节点专家并行（Prefill EP32 / Decode EP144 甚至更高）**：
   - 常规小模型推理尽量把部署框在单机 8 卡内避免跨机通信；但对于 671B 总参的 MoE 模型，单机 8 卡光放权重就几乎挤满显存，根本塞不下大 Batch。
   - DeepSeek 利用 **DeepEP** 的低延迟跨机 RDMA，直接把 Decode 阶段的专家并行拉到 **EP144+**（配合 Attention 层的 DP 数据并行），每张卡只存极少数专家权重，省出海量显存用来堆积超大 Batch Size，强行提高 Decode 阶段的算术强度！
3. **双微批重叠（Two-Microbatch Overlap）**：在单个推理步内把 Batch 拆成两个微批交替推进——微批 A 在执行 Attention/MLP 计算时，微批 B 在后台执行 DeepEP 的跨机 Dispatch/Combine，彻底填满计算与网络间隙。
4. **基于 3FS 的磁盘 KV Cache 卸载**：由于 MLA 把 KV Cache 压缩得极小，直接把历史多轮对话与系统 Prompt 的前缀 KV 卸载到高速 SSD 存储池（3FS）成为可能，官方披露其线上系统通过磁盘前缀缓存获得了超过 **56%** 的缓存命中率，直接省掉一半以上的重复 Prefill 算力。

---

## 三、存储与数据侧：`3FS` 与 `Smallpond` 的反传统极简设计

在开源周的第 5 天，DeepSeek 亮出了它的存储与数据底座——**3FS（Fire-Flyer File System）** 与 **Smallpond**。这一层是整个技术栈里对 **Data Infra 工程师冲击最大** 的部分。

### 3.1 `3FS`：反直觉的“去客户端读缓存 + 强一致暴力随机读”

传统分布式文件系统（HDFS、Ceph、Lustre）诞生于机械硬盘与传统数仓时代，设计假设是“大块顺序读 + 客户端重度页缓存”。但大模型基础设施的 I/O 特征恰恰相反：
- **训练样本加载**：为了打乱样本分布，DataLoader 发起的是**海量跨文件完全随机读**，传统客户端读缓存命中率几乎为零，反而白白吃掉宿主机宝贵的内存和锁开销；
- **万卡 Checkpoint 保存**：每隔几十分钟，成千上万张 GPU 同时突发写入 TB 级权重文件，瞬间打爆元数据与存储吞吐；
- **在线 KV Cache 卸载**：需要亚毫秒到毫秒级的并发点查读取。

于是 **3FS** 做出了极其激进的硬件贴近取舍：
- **彻底砍掉客户端读缓存**，改用 Linux 异步 I/O（`io_uring` / AIO）+ **InfiniBand / RoCE RDMA** 网络（`hf3fs_usrbio` 用户态零拷贝接口），把全集群成百上千块本地 NVMe SSD 聚合成一个纯暴力高吞吐随机读存储池；
- 采用 **CRAQ（Chain Replication with Apportioned Queries）** 链式复制保障强一致性，允许任意副本承接读流量以榨干全集群 SSD 带宽；
- 官方实测在 180 个存储节点上跑出了 **6.6 TiB/s** 的聚合读吞吐，用一套存储统一扛下了 **训练数据加载、Checkpoint 读写、推理 KVCache 卸载以及离线数据 Shuffle** 四种截然不同的负载。

### 3.2 `Smallpond`：`DuckDB + 3FS + Ray Core` 的极简批处理范式

这里有一个非常有意思、也经常被误解的事实：**DeepSeek 虽然在核心 GPU 训练和在线推理层绝不用 Ray，但在离线数据预处理层（`Smallpond`）却极其务实地借用了 `Ray Core`！**

DeepSeek 不想为了洗 PB 级预训练语料去维护一套沉重的常驻 Hadoop/YARN/Spark 集群，于是造了 **Smallpond**：
- **单节点向量化执行核**：直接嵌入 **DuckDB**（以及部分场景结合 Polars）在分区内跑 Partial SQL 与高速列式转换，完全没有 JVM 与 GC 包袱；
- **无状态分布式编排**：用轻量的 **Ray Core** 将任务按分区弹性派发到计算节点，算完即退，零常驻服务运维；
- **超高速共享存算与 Shuffle**：彻底跳过传统复杂的本地磁盘 Shuffle 管理，直接把中间结果读写在 **3FS** 这张 200G/400G RDMA 高速共享存储网上。

在工业界经典的 **GraySort** 排序基准测试中，DeepSeek 仅用 **50 台计算节点（1×200Gb 网卡）+ 25 台 3FS 存储节点（2×400Gb 网卡）**，就在 **30 分 14 秒内排完了 110.5 TiB 数据，平均吞吐达 3.66 TiB/min**——正面证明了“轻量嵌入式向量化核 + 无状态任务调度 + 超高速 RDMA 存储”在特定批处理上的恐怖效率。

---

## 四、横向对比：DeepSeek 路线与其他大厂及开源生态的本质区别

把 DeepSeek 放进当前全球 AI Infra 的四股主力阵营中对比，就能看清它“锋利”背后的结构性定位：

| 对比维度 | DeepSeek 开源栈 | 硅谷前沿闭源实验室（OpenAI / Anthropic / Google） | 通用开源 AI 生态（Megatron + vLLM / SGLang + Ray） | 企业云湖仓派（Databricks / Snowflake / Spark / Daft） |
| :--- | :--- | :--- | :--- | :--- |
| **核心设计哲学** | **算法与硬件深度咬合**：为单一模型族（MLA+MoE）与特定硬件（Hopper+IB）做到极致 | 软硬一体或专用芯片协同（Google TPU+XLA、Anthropic 混用 Trainium/TPU/GPU） | **最大公约数通用性**：兼容 Dense/MoE/VLM、NVIDIA/AMD/昇腾多硬件与多业务场景 | **多租户治理与开放湖仓**：强 SQL CBO、ACID 事务、安全审计、运行在廉价公有云对象存储上 |
| **训练/推理通信与内核** | 自研极简 PTX/CUDA（`DeepGEMM`/`FlashMLA`/`DeepEP`/`DualPipe`） | 全栈闭源高度定制 | 社区快速吸收融合（**SGLang 与 vLLM 已在开源周后火速集成 DeepEP、DeepGEMM、FlashMLA 与 PD 分离**） | 侧重数据管线编排与模型托管调用（AI Functions / Ray on Spark） |
| **存储与数据基础设施** | **3FS（全闪 NVMe + IB RDMA）+ Smallpond（DuckDB + Ray）** | Google Colossus/Dremel；闭源高吞吐分布式对象/文件存储 | 对象存储（S3）+ Ray Data / Daft / Mooncake KV 池 | S3/OSS/GCS + Iceberg/Delta/Lance + Spark/StarRocks/Daft |
| **最大护城河** | 同等硬件下的极限 MFU、超低推理单价、零冗余抽象 | 万卡/十万卡超大规模容错与跨代专用芯片红利 | 生态兼容性极广、普通团队开箱即用、社区迭代极快 | 企业级数据安全、复杂多表 SQL 能力、云上弹性低 TCO |

一个非常值得注意的生态现象是：**DeepSeek 开源周并没有“杀死” vLLM 和 SGLang，反而成了它们最强的外挂燃料**。开源后短短几个月内，SGLang 和 vLLM 迅速把 `DeepEP`、`DeepGEMM`、`FlashMLA`、`DP-Attention` 与 PD 分离吸收进了主干代码，让没有自研团队的普通企业也能一键享受到 DeepSeek 级别的推理吞吐。

---

## 五、从 Data Infra 视角深度审视：DeepSeek 路线的利与弊

抛开对底层硬核代码的崇拜，如果站在**企业级数据基础设施（Data Infra）架构师**的立场冷静打分，DeepSeek 这套“3FS + Smallpond + 裸金属算子”组合呈现出极为鲜明的两面性。

### 5.1 锋利的优势：给整个 Data Infra 行业带来的三记重拳

1. **重新定义了 AI-Native 存储的第一性原理（3FS 的启示）**：
   过去数据湖默认存储介质就是“高延迟、低单价、面向顺序大块扫描的对象存储（S3/OSS）”。3FS 证明了在 AI 时代，热计算池的核心矛盾是**高并发随机读带宽与统一状态承载**——砍掉毫无意义的客户端读缓存，把训练 DataLoader、Checkpoint、推理 KVCache 与批处理 Shuffle 收口在同一层 RDMA 全闪存储上，彻底打破了“大数据存储”与“AI 推理缓存”的部门墙。
2. **验证了“嵌入式 C++ 向量化核 + 高速共享存储”对重型大数据栈的降维打击**：
   Smallpond 用几千行胶水把 DuckDB + Ray Core + 3FS 粘在一起就跑出了 3.66 TiB/min 的 GraySort，狠狠拷问了传统企业大数据平台：**当底层存储网络带宽足够高（200G/400G RDMA）且单机多核向量化引擎（DuckDB/Polars）足够强时，我们是否真的还需要为了离线批处理去养一套动辄几十万行 Java 代码、天天调 JVM GC 的常驻 Hadoop/Spark 集群？**
3. **把“算子前置减法”做到极致**：
   在系统工程里，最快的 I/O 是不发生的 I/O。DeepSeek 先在模型结构里用 **MLA** 把 KV Cache 压缩一个数量级，才让磁盘级 3FS 缓存成为现实——这与数据库里用字典编码/列裁剪减少扫描量的思想一脉相承。

### 5.2 致命的短板：为什么 95% 的企业 Data Infra 不能照搬 DeepSeek？

如果盲目把 3FS 和 Smallpond 搬进普通互联网或企业数仓团队，很快会撞上五堵硬墙：

1. **极度苛刻且昂贵的专属硬件前提（离开 IB RDMA + 全闪盘就失去魔力）**：
   3FS 的 6.6 TiB/s 与 Smallpond 的高速 Shuffle 建立在**自建全闪 NVMe 集群 + 200G/400G InfiniBand/RoCE RDMA 无损网络**之上。而在绝大多数企业所在的**公有云环境（S3 / OSS / GCS + 普通以太网）**里，你既拿不到廉价的 RDMA 裸金属互联，也承担不起把几十 PB 历史温冷数仓数据全部存放在全闪 NVMe 强一致（CRAQ）3FS 里的惊人 TCO。
2. **不是通用数仓/湖仓：缺失企业级 Data Infra 的核心骨架**：
   `Smallpond` 本质上是**面向预训练语料的轻量分区级批处理工具**（Partial SQL + 显式手动分区），它刻意放弃了企业级数据平台赖以生存的几乎所有上层基建：
   - **没有完整的全局代价优化器（Cascades / Catalyst CBO）**：不支持复杂十表星型 Join 重排、多列直方图估算与运行期 AQE 倾斜自愈；
   - **没有企业级数据治理与 ACID 事务表**：缺乏类似 Iceberg/Delta/Paimon 的时间旅行、Schema 演进、多租户资源配额隔离、行/列级权限控制（RBAC/RLS）与血缘审计（Unity Catalog / DLF）。
3. **多模态非结构化管线能力不如新一代专用引擎**：
   面对公有云对象存储上十亿级零散的视频、图片与 PDF，相比 **Daft**（原生 Rust 图像解码、S3 Range Coalescing 小文件合并、Tokio `channel(1)` 严格流式背压防 20× 解码 OOM、Key 级 S3 断点续跑），Smallpond（DuckDB + 3FS）仍偏向规整的 Parquet/文本离线批处理。
4. **极高的“硬件/模型强绑定”工程维护税**：
   DeepGEMM、FlashMLA、DeepEP 大量依赖 Hopper 专属的 `WGMMA`/TMA/PTX 与 IBGDA 底层细节。一旦未来集群换代到 Blackwell B200、AMD MI300X 或国产 AI 加速卡，或者模型架构发生演进，这种毫无中间抽象层的裸金属代码都需要一支顶级的 CUDA/RDMA 内核专家团队重写护航——对于非头部基模公司来说，这笔人力维护税远高于省下来的硬件费。
5. **高并发短查询与 BI 场景天然不适用**：
   无论是 Smallpond 还是基于 Ray 的无状态调度，都面向高吞吐离线批处理，完全无法替代 StarRocks / ClickHouse / Doris 这类支持数千并发、50 ms 亚秒响应的实时 OLAP 引擎。

---

## 六、总结：从 DeepSeek 技术栈里，我们该学什么、不该抄什么？

DeepSeek 开源技术栈像是一台**拆掉空调、座椅和隔音棉、为了赛道圈速调校到极致的 F1 赛车**——它在“有限 H800 算力 + 自建 IB 全闪集群 + 单一超大规模 MoE 模型”这条赛道上跑出了人类工程学的天花板；但如果你要的是每天拉几千名业务乘客跑城市道路的公交系统（企业多租户 Data Infra），直接照搬 F1 底盘反而会水土不服。

对绝大多数架构师与数据基础设施团队而言，最清醒的吸收策略是**“偷师设计哲学，借力通用生态”**：

- **值得坚决吸收的三条架构思想**：
  1. **针对真实工作负载做减法**：识别哪些传统组件是历史包袱（例如 AI 随机读下的客户端读缓存、重型常驻批处理调度守护进程），敢于用轻量向量化核（DuckDB / Polars / Rust）重构热路径；
  2. **算法、计算与存储协同设计**：在设计数据管线与推理系统时，同步考虑数据压缩比、前缀缓存复用（KV Cache 分层存储）与计算/通信重叠；
  3. **把数据面和推理缓存打通**：借鉴 3FS / Mooncake 的分层理念，用高速 NVMe/DRAM 池承接长上下文 KV Cache 与热点特征。
- **不必盲目自研照搬的工程路线**：
  - 在训练与推理层，直接复用已经深度吸收了 `DeepEP` / `FlashMLA` / `DeepGEMM` 的 **SGLang / vLLM / Megatron / veRL** 主干生态，把底层 PTX 与跨硬件适配的维护税交给开源社区；
  - 在企业 Data Infra 层，坚持 **对象存储（冷温事实源）+ 开放表格式（Iceberg / Delta / Lance）+ 场景化分层计算（StarRocks 管 BI、Spark 管重治理结构化 ETL、Daft / Ray Data 管多模态 AI 清洗）**，只有在拥有自建 IB 全闪集群且明确遭遇训练数据加载 / Checkpoint / KVCache 随机读瓶颈时，才将 **3FS** 作为热计算加速层精准引入。

---

> **参考与公开资料说明**
> - **DeepSeek 开源周官方仓库与技术报告**：[FlashMLA](https://github.com/deepseek-ai/FlashMLA)、[DeepEP](https://github.com/deepseek-ai/DeepEP)、[DeepGEMM](https://github.com/deepseek-ai/DeepGEMM)、[DualPipe](https://github.com/deepseek-ai/DualPipe)、[EPLB](https://github.com/deepseek-ai/EPLB)、[3FS](https://github.com/deepseek-ai/3FS)、[smallpond](https://github.com/deepseek-ai/smallpond) 以及 *DeepSeek-V3 Technical Report* 与 *DeepSeek-V3/R1 Inference System Overview*。
> - **社区复现与生态集成**：LMSYS / SGLang 团队关于 DeepSeek-V3/R1 大规模跨节点 EP（EP32/EP144）与 DeepEP/PD 分离复现的技术博客；vLLM 官方 DeepSeek-V3/R1 与 PD 分离架构文档。
