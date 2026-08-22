---
title: "StarRocks 实现原理与源码深度分析：从 CBO、Pipeline 到湖仓一体"
date: 2026-08-09T00:00:00+08:00
categories:
  - 数据库
tags:
  - StarRocks
  - OLAP
  - 查询优化器
  - 向量化执行
  - 湖仓一体
description: "基于 StarRocks 源码，系统分析 FE/BE 架构、CBO、MPP Pipeline、主键表、数据湖、物化视图与存算分离的实现原理。"
draft: false
---

**StarRocks 是一款开源的高性能实时 OLAP 引擎**，主打毫秒级查询响应、高并发分析、实时数据更新，同时支持数据湖查询和湖仓一体架构。

本文不是 Feature 清单，而是一份从源码反推系统设计的技术分析：先建立 FE、BE/CN 与存储层的边界，再沿一条 SQL 的生命周期进入 CBO、Fragment 和 Pipeline，最后分析主键模型、Runtime Filter、数据湖、物化视图与 Shared-Data。希望读者看完后不仅知道 StarRocks “有什么”，也能回答这些能力“落在哪些模块、如何协作、代价是什么”。

## 一：StarRocks 概览

**极速**：采用 MPP 分布式执行框架，配置 CBO 优化器，采用列存数据格式，并实现全面的向量化；

**统一**：一套系统解决多维分析、高并发查询、预计算、实时分析查询等场景；使用 StarRocks 来统一数据湖和数据仓库，将高并发和实时要求性很高的业务放在 StarRocks 中分析，把数据湖上的分析使用 StarRocks 外表查询，统一使用 StarRocks 管理湖仓数据。

```text
实时写入：Kafka / Flink ─┐
                         ├──▶ StarRocks 实时湖仓 ──▶ Dashboard / BI 报表
批量写入：S3/HDFS/Spark ─┘              │            Ad-hoc / Data Apps
                                        │
                                        └── 联邦查询 ──▶ Hive / Iceberg / Hudi
                                                        MySQL / Elasticsearch / ...
```

### 分析结论先行

从源码结构看，StarRocks 的高性能不是来自某个孤立算子，而是以下几层共同作用：

1. **FE 把复杂性前置到规划期**：语义分析、规则改写、基于 Memo 的代价搜索、物化视图改写和 Fragment 构建都集中在 FE，BE/CN 收到的是可直接实例化的物理执行 DAG。
2. **BE/CN 以列式 `Chunk` 和 PipelineDriver 为执行核心**：算子按批处理数据，Driver 在用户态协作调度；阻塞、依赖和背压成为调度器可感知的状态，而不是简单地“一算子一线程”。
3. **数据模型直接进入存储实现**：Duplicate、Aggregate、Primary Key 并非只有 DDL 语义差异。尤其主键表需要 Primary Index、DelVector、版本发布和 Compaction 协同，写放大与读放大之间存在明确取舍。
4. **湖仓一体依赖统一优化器，而不是统一存储格式**：内表与外表共享逻辑计划、CBO 和大部分执行算子，但元数据枚举、文件裁剪、删除文件处理与 I/O 由 Connector 分层适配。
5. **Shared-Data 改写了状态管理方式**：对象存储中的版本化 Tablet Metadata 和 Txn Log 成为持久状态，本地缓存只负责加速；Warehouse 则把一份共享数据映射到相互隔离的计算资源。

### 源码分析基线与方法

本文保留原有主题和材料，并使用本地 StarRocks 源码逐项核对。分析基线为 [StarRocks 源码提交 0fd27fd](https://github.com/StarRocks/starrocks/tree/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1)（2026-03-30）。由于功能迭代很快，文中的类名和调用链以该快照为准；产品版本、商业信息和 Benchmark 结论则可能随时间变化。

阅读源码时采用三条主线：

| 主线 | FE 入口 | BE/CN 入口 | 要回答的问题 |
|------|---------|------------|----------------|
| 查询 | `StmtExecutor` → `StatementPlanner` → `QueryOptimizer` | `FragmentExecutor` → `FragmentContext` → `PipelineDriver` | SQL 如何变成分布式执行任务？ |
| 数据 | `OlapTable` → `Partition` → `MaterializedIndex` → `Tablet` | `Tablet` → `Rowset` → `Segment` → Page | 表模型怎样映射为物理文件和版本？ |
| 湖仓 | `ConnectorMetadata`、`MvRewritePreprocessor`、`WarehouseManager` | Connector DataSource、Lake `TabletManager` | 外部数据、MV 与 Shared-Data 如何复用执行引擎？ |



### 技术演进

StarRocks 的技术 lineage 可以追溯到 Google Mesa 系统：
```
Google Mesa 
    │
    ▼
Baidu Mesa (百度内部实现)
    │
    ▼
Baidu Palo (~-2017) — 百度内部 OLAP 引擎
    │
    ▼
Apache Doris (2018) — 百度开源，捐赠给 Apache 基金会
    │
    ▼
DorisDB (2019-2021) — 在 Doris 基础上增强的商业化产品，后因 Apache 项目商标与命名规范更名为 StarRocks
    │
    ▼
StarRocks (2021.06) — 从 Doris 分支，独立开源项目

```

**关键发展节点**：
- **2021年6月**：正式开源（Apache 2.0），GitHub Star 10k\+
- **2022年**：发布 V2.0，引入 CBO 优化器、Pipeline 引擎
- **2023年**：发布 V3.0，引入存算分离架构、异步物化视图
- **2024年**：发布 V3.1/V3.2/V3.3，完善存算分离完善及节约成本；
- **2025年**：发布V3.4/V3.5/V4.0，聚焦于AI变更、增量MV、商业化；



主要 Feature 对比：[StarRocks 功能特性对比](https://docs.starrocks.io/zh/docs/introduction/feature_difference/)

| 版本 | 发布时间 | 核心 Feature | 技术价值 |
|------|------------|--------------|------------|
| **V1.0** | 2021 H2 | 全向量化执行引擎、MPP 架构 | 奠定高性能基础，向量化覆盖所有核心算子 |
| **V2.0** | 2022 H2 | CBO 优化器、Pipeline 引擎、Primary Key 表 | 引入 Cascades 风格优化器，Pipeline 化执行 |
| **V3.0** | 2023 H2 | 存算分离架构、异步物化视图、湖仓一体 | 架构级重构，支持 Shared-Data 模式 |
| **V3.1~3.5** | 2024~2025 | Group Commit、File Bundling、Warehouse 管理 | 存算分离功能完善，云原生能力增强 |
| **V4.0** | 2025.12 | Incremental MV、Automatic tablet splitting、Vector Index、AI Native | 聚焦于商业化、数据的新鲜度、性能及AI Native |

发版节奏：
- 每4个月一个minor版本，3.1/3.2/3.3算是minor 版本：[StarRocks 版本生命周期与发布策略](https://docs.starrocks.io/docs/developers/versions/)
- Release版本的节奏2~3个星期发布周期；

### 商业化生态

| 实体                                                                                                      | 角色    | 说明                                |
| ------------------------------------------------------------------------------------------------------- | ----- | --------------------------------- |
| **StarRocks 开源社区**                                                                                      | 开源项目  | Apache 2.0 协议，GitHub star 数 11k\+ |
| <span style="color: rgb(44, 44, 43); background-color: rgb(255, 245, 184);">**CelerData（北美）**</span>    | 商业化公司 | StarRocks 创始团队创立，提供云服务，专注于北美业务    |
| <span style="color: rgb(44, 44, 43); background-color: rgb(255, 245, 184);">**MirrorShip（APAC）**</span> | 商业化公司 | 亚太地区商业化运营实体                       |
| **阿里云 EMR Serverless**                                                                                  | 云服务集成 | StarRocks 作为 EMR 可选组件，提供托管服务      |
| **火山引擎/腾讯云/华为云**                                                                                        | 云服务集成 | 其他三方商业化平台；                        |

---

## 二：架构解析

### 整体架构

StarRocks 采用经典的 FE \+ BE/CN 架构：
- 部署简单，只有两个类型的进程，不依赖其他三方系统；
- FE/BE常驻进程，

```text
Shared-Nothing（存算一体）              Shared-Data（存算分离）
┌─────────────────────┐               ┌─────────────────────┐
│ FE：Catalog / 调度   │               │ FE：Catalog / 调度   │
└──────────┬──────────┘               └──────────┬──────────┘
           │                                     │
┌──────────▼──────────┐               ┌──────────▼──────────┐
│ BE：执行 + 存储      │               │ CN：执行 + 本地缓存  │
│ 本地数据随节点分布   │               └──────────┬──────────┘
└─────────────────────┘                          │
                                     ┌──────────▼──────────┐
                                     │ S3 / OSS / HDFS     │
                                     └─────────────────────┘
```

从工程边界看，FE 更接近控制面，BE/CN 更接近数据面，但二者不是传统数据库中“SQL 层/存储层”的简单切分：

| 组件 | 核心职责 | 关键源码 |
|------|----------|----------|
| FE 会话与语句入口 | MySQL/HTTP 会话、语句分派、重试、结果返回、Profile 汇总 | [`StmtExecutor.java`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/fe/fe-core/src/main/java/com/starrocks/qe/StmtExecutor.java) |
| FE 元数据与控制面 | Catalog、Database、Table、节点、事务、Warehouse 等全局状态 | `GlobalStateMgr`、`MetadataMgr`、`WarehouseManager` |
| FE 查询规划 | AST 分析、逻辑计划、CBO、物理计划、Fragment 切分 | `StatementPlanner`、`QueryOptimizer`、`PlanFragmentBuilder` |
| FE 分布式协调 | 选择执行节点、生成 Fragment Instance、下发、收集状态 | [`DefaultCoordinator.java`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/fe/fe-core/src/main/java/com/starrocks/qe/DefaultCoordinator.java) |
| BE/CN Runtime | Query/Fragment 上下文、内存与资源组、Runtime Filter、Exchange | `be/src/runtime/`、`be/src/exec/pipeline/` |
| BE 本地存储 | Tablet、Rowset、Segment、索引、Compaction、主键更新 | `be/src/storage/` |
| CN Shared-Data | Lake Tablet、版本化元数据、Txn Log、对象存储与 Cache | `be/src/storage/lake/` |

这里有一个容易混淆的点：**FE 本身也能执行极少数可短路的查询，但正常 OLAP 查询仍由 BE/CN 执行。** `StmtExecutor.handleQueryStmt()` 会先判断 `canExecuteInFe`，否则构造 `DefaultCoordinator` 并部署 Fragment。也就是说，“FE 是纯元数据进程”是便于理解的近似，而不是绝对实现约束。

### **Shared-Nothing 架构（存算一体）**

**特点**：
- 每个 BE 节点拥有独立的计算和存储资源
- 数据分布在各个 BE 的本地磁盘上
- 查询通过 MPP（Massively Parallel Processing）方式并行执行

**优势**：
- 计算与存储本地化，减少网络开销
- 适合高吞吐、低延迟的 OLAP 查询

**劣势**：
- 扩缩容需要数据重分布（Rebalance）
- 存储容量受限于单机磁盘
- 无法独立扩展计算和存储

### **Shared-Data 架构（存算分离）**

**特点**：
- 计算节点（CN）无状态，可弹性伸缩
- 数据存储在对象存储（S3、OSS 等）
- 通过 Cache 层加速热数据访问（单机Local Cache，暂无Global Cache，演进中）
- 支持 Warehouse 管理（多计算集群隔离）

**优势**：
- 计算和存储独立扩缩容
- 存储容量理论上无限
- 多租户隔离（不同 Warehouse 独立资源）
- 更适合云原生部署

Shared-Data 下的 CN “无状态”同样是一种逻辑描述：CN 不拥有数据的唯一持久副本，但仍维护 Data Cache、Tablet Metadata Cache、主键索引缓存和正在执行的事务状态。节点可以被替换，是因为权威数据与版本信息能从共享存储恢复，而不是因为节点内存和磁盘上完全没有状态。

<span style="color: rgb(44, 44, 43); background-color: rgb(255, 245, 184);">**重要趋势**</span><span style="color: rgb(44, 44, 43); background-color: rgb(255, 245, 184);">：产品演进和云服务能力明显向存算分离倾斜，Warehouse、弹性与对象存储优化也是 Shared-Data 的重点，也是整个业界的整体发展趋势。</span>

## 三：数据模型与存储

### 数据组织层级
```text
FE 元数据层
Table（OlapTable）
  └── Partition（逻辑分区）
        └── PhysicalPartition（物理分区/子分区）
              └── MaterializedIndex（Base Index 或 Rollup Index）
                    └── Tablet（分桶后的数据分片）

BE/CN 存储层
Tablet
  └── Rowset（一次导入、发布或 Compaction 形成的版本数据集合）
        └── Segment（列式文件）
              └── Column
                    └── Page（编码、压缩和索引的基本 I/O 单元）
```

**Tablet** 是 StarRocks 最小的物理存储单元，每个 Tablet 推荐大小约 10GB，分布在不同的 BE 节点上。
- 表可以根据partition key进行水平分区， 支持list/range分区，每个分区维护单独的Tablets；
- 每个Partition指定bucket key，进行Hash/Random/<span style="color: rgb(44, 44, 43); background-color: rgb(255, 245, 184);">Range</span>打散分布，每个bucket成为tablet。

```text
Table
  └── Range / List Partition
        ├── Partition A ── Hash / Random / Range ──▶ Tablet 0 ──▶ Segment Files
        │                                            Tablet 1 ──▶ Segment Files
        └── Partition B ── Hash / Random / Range ──▶ Tablet 0 ──▶ Segment Files
                                                     Tablet 1 ──▶ Segment Files
```

这两个层级需要分开理解。FE 中 [`OlapTable`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/fe/fe-core/src/main/java/com/starrocks/catalog/OlapTable.java)、`Partition`、`PhysicalPartition`、`MaterializedIndex` 和抽象 `Tablet` 描述 Catalog 视角的布局；BE 中 [`Tablet`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/be/src/storage/tablet.h)、[`Rowset`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/be/src/storage/rowset/rowset.h) 和 [`Segment`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/be/src/storage/rowset/segment.h) 才负责真正的版本选择与数据读取。

查询时，`TabletReader` 根据可见版本选择 Rowset，Rowset 再创建 Segment Iterator；谓词会尽可能下推到 Zone Map、Bitmap、Bloom Filter 等索引，最终解码需要的 Column Page 并组装成列式 `Chunk`。因此“列存”不只是磁盘格式，也意味着从 Page 解码、表达式计算到算子交换都尽量保持列式批处理。

### 分区与分布策略

**Partition 策略**：
- **Range Partition**：按范围分区（如日期范围），适用于时间序列数据；支持\`date\_trunc/time\_slice\`等分区表达式
- **List Partition**：按离散值列表分区，适用于枚举值分区；支持多列、GeneratedColumn分区表达式

**Distribution 策略**：
- **Hash Distribution**：按 Hash 值分布到 Bucket，最常用，保证数据均匀分布
- **Random Distribution**：随机分布，无需指定分布列的场景
- <span style="color: rgb(44, 44, 43); background-color: rgb(255, 245, 184);">**Range Distribution**</span><span style="color: rgb(44, 44, 43); background-color: rgb(255, 245, 184);">：按范围分布，解决用户多租户场景下数据倾斜的问题，支持自动分裂、合并；</span>

### 表模型

StarRocks 支持三种表模型，分别适用于不同的场景：

#### 聚合表（Aggregate Table）

**设计理念**：Mesa中的Rollup表，类似 ClickHouse 的 AggregatingMergeTree，支持写入时自动聚合。

**支持的聚合函数**：
- 基础聚合：`SUM`、`MIN`、`MAX`、`COUNT`、`REPLACE`
- 高级聚合：`BITMAP_UNION`、`HLL_UNION`、`PERCENTILE_UNION`

**关键特性**：
- 不支持回撤数据；
- **Rollup Index**：预聚合的物化索引，类似物化视图，在写入时维护
- **Sync Materialized View**：同步物化视图，随基表更新自动更新
- 适用于指标预计算场景（如广告报表、用户画像）

#### 明细表（Duplicate Table）

**设计理念**：Append-only 模型，保留所有明细数据。

**关键特性**：
- 支持 Append 写入
- 也支持 DELETE，但 DELETE 只是标记删除（通过 Delete Predicate 标记），在查询时过滤
- 适用于日志分析、事件追踪等需要保留明细的场景

**DELETE 机制**：DELETE 操作不立即物理删除数据，而是生成 Delete Predicate 标记哪些行被删除。查询时通过 Delete Predicate 过滤，后台 Compaction 时真正删除。

#### 主键表（Primary Key Table）

**设计理念**：支持 Upsert 语义，类似传统数据库的主键表。

参考：[StarRocks 主键模型设计与实现](https://zhuanlan.zhihu.com/p/566219916)

**关键特性**：
- 支持部分列更新；
- 适用于实时数仓、CDC 同步场景

主键表是三种模型中实现代价最高的一种。以存算一体为例，核心链路可以概括为：

```text
新 Rowset 中的 Primary Keys
            │
            ▼
PrimaryIndex.upsert(pk → 新 rssid/rowid)
            │
            ├── 找到旧位置 ──▶ 为旧 Segment 生成/合并 DelVector
            │
            └── 写入新位置 ──▶ 发布新的 EditVersion
                                      │
                                      ▼
查询 = 可见 Rowsets - 对应版本的 DelVector
                                      │
                                      ▼
Compaction 重写数据并回收失效版本
```

关键实现分别位于 [`PrimaryIndex`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/be/src/storage/primary_index.h)、[`DelVector`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/be/src/storage/del_vector.h) 和 [`TabletUpdates`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/be/src/storage/tablet_updates.h)。`PrimaryIndex` 维护主键到物理行位置的映射；Upsert 返回被覆盖的旧行位置，这些位置写入版本化 DelVector；`TabletUpdates` 串联 Rowset Commit、异步 Apply、版本推进与 Compaction。

这个设计把随机更新转化为“追加新数据 + 版本化删除”，保留列存批写优势，但代价也很清楚：Primary Index 占用内存或持久索引空间，DelVector 增加读时过滤，后台 Compaction 必须及时清理历史版本。部分列更新还需要读取旧值或采用专门的 Column Mode 路径，不能简单等同于原地修改一行。

| 表模型 | 写入阶段 | 查询阶段 | 主要代价 |
|--------|----------|----------|----------|
| Duplicate | 追加 Rowset | 合并可见 Rowset，并应用 Delete Predicate | 查询可能读取较多明细 |
| Aggregate | 按 Key 聚合，Compaction 继续合并 | 读取已预聚合状态 | 写入/合并需执行聚合语义，不适合任意回撤 |
| Primary Key | Primary Index Upsert + DelVector | 读取最新版本并过滤旧行 | 索引、版本发布和 Compaction 成本更高 |

### **产品特点**
- [MPP 分布式执行框架](https://docs.starrocks.io/zh/docs/introduction/Features/#mpp-%E5%88%86%E5%B8%83%E5%BC%8F%E6%89%A7%E8%A1%8C%E6%A1%86%E6%9E%B6)
- [全面向量化执行引擎](https://docs.starrocks.io/zh/docs/introduction/Features/#%E5%85%A8%E9%9D%A2%E5%90%91%E9%87%8F%E5%8C%96%E6%89%A7%E8%A1%8C%E5%BC%95%E6%93%8E)
- [存算分离](https://docs.starrocks.io/zh/docs/introduction/Features/#%E5%AD%98%E7%AE%97%E5%88%86%E7%A6%BB)
- [CBO 优化器](https://docs.starrocks.io/zh/docs/introduction/Features/#cbo-%E4%BC%98%E5%8C%96%E5%99%A8)
- [可实时更新的列式存储引擎](https://docs.starrocks.io/zh/docs/introduction/Features/#%E5%8F%AF%E5%AE%9E%E6%97%B6%E6%9B%B4%E6%96%B0%E7%9A%84%E5%88%97%E5%BC%8F%E5%AD%98%E5%82%A8%E5%BC%95%E6%93%8E)
- [智能的物化视图](https://docs.starrocks.io/zh/docs/introduction/Features/#%E6%99%BA%E8%83%BD%E7%9A%84%E7%89%A9%E5%8C%96%E8%A7%86%E5%9B%BE)
- [数据湖分析](https://docs.starrocks.io/zh/docs/introduction/Features/#%E6%95%B0%E6%8D%AE%E6%B9%96%E5%88%86%E6%9E%90)

## 四：查询引擎

StarRocks 采用 MPP（Massively Parallel Processing）分布式执行框架。在MPP执行框架中，一条查询请求会被拆分成多个物理计算单元，在多机并行执行。
- 每个执行节点拥有独享的资源（CPU、内存）
- MPP执行框架能够使得单个查询请求可以充分利用所有执行节点的资源，所以单个查询的性能可以随着集群的水平扩展而不断提升。
- FE 生成物理 Plan 后，Fragment DAG 的拓扑通常不会像完整 AQE 那样重规划；但 Runtime Filter、自适应 DOP、Spill、预聚合模式和调度状态仍会根据运行时数据变化。Fragment Instance 是 FE 向节点部署的实例粒度，BE/CN 内部真正被调度器反复执行的是 PipelineDriver；

查询从 FE 规划到 BE/CN 执行的层级如下：

```text
SQL Text
   │
   ▼
AST ──▶ Logical Plan ──▶ Physical Plan
       FE：Parser / Analyzer / Transformer / CBO / Physical Rewrite
                                      │
                                      ▼
                               Plan Fragments
                                      │
                                      ▼
                              Fragment Instances
                                      │
                                      ▼
                              Pipeline Drivers
       BE/CN：准备 Fragment、执行 Pipeline、汇总 Profile
```

以 `SELECT count(*) FROM table GROUP BY id` 为例，Fragment 之间通过 Exchange 按 `id` Shuffle：

```text
下游 Fragment Instances                         上游 Fragment Instances
┌────────────────────────┐                    ┌────────────────────────┐
│ Scan ─▶ Local Agg ─▶ Sender ├── hash(id) ──▶│ Receiver ─▶ Final Agg  │
├────────────────────────┤                    ├────────────────────────┤
│ Scan ─▶ Local Agg ─▶ Sender ├── hash(id) ──▶│ Receiver ─▶ Final Agg  │
├────────────────────────┤                    ├────────────────────────┤
│ Scan ─▶ Local Agg ─▶ Sender ├── hash(id) ──▶│ Receiver ─▶ Final Agg  │
└────────────────────────┘                    └───────────┬────────────┘
                                                        ▼
                                                   Result Sender
```

### 一条 SQL 的源码调用链

下面这条调用链是理解 StarRocks 查询引擎最有效的入口：

```text
Client
  │
  ▼
StmtExecutor.execute()
  ├── generateExecPlan()
  │     └── StatementPlanner.plan()
  │            ├── analyzeStatement() / Authorizer.check()
  │            ├── RelationTransformer.transformWithSelectLimit()
  │            ├── QueryOptimizer.optimize()
  │            └── PlanFragmentBuilder.createPhysicalPlan()
  │
  └── handleQueryStmt()
         └── DefaultCoordinator.execWithQueryDeployExecutor()
                ├── 构建 ExecutionDAG / FragmentInstance
                └── RPC: exec_plan_fragment
                           │
                           ▼
BE/CN InternalService
  └── pipeline::FragmentExecutor
         ├── prepare()：QueryContext、RuntimeState、Plan、Pipeline
         └── execute()
                ├── FragmentContext.prepare_active_drivers()
                └── DriverExecutor.submit(PipelineDriver)
```

源码中的 `StatementPlanner.createQueryPlan()` 把规划阶段明确拆成 `Transformer`、`Optimizer` 和 `ExecPlanBuild` 三段，并通过 Tracer 记录耗时。[`PlanFragmentBuilder`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/fe/fe-core/src/main/java/com/starrocks/sql/plan/PlanFragmentBuilder.java) 将优化器的 `OptExpression` 转换为可序列化的 `PlanFragment`；[`DefaultCoordinator`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/fe/fe-core/src/main/java/com/starrocks/qe/DefaultCoordinator.java) 再完成实例放置、Runtime Filter 合并节点选择和 RPC 下发。

BE/CN 的 [`FragmentExecutor`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/be/src/exec/pipeline/fragment_executor.cpp) 在 `prepare()` 中恢复全局字典、RuntimeState、WorkGroup、算子与 Pipeline，在 `execute()` 中准备并提交 active drivers。这种分层的好处是：**计划语义由 FE 统一决定，节点内并发、依赖、背压和资源公平性由 BE/CN Runtime 决定。**



### **CBO 优化器**

StarRocks 参考 Cascades/Columbia Optimizer 思想，从零设计并实现了基于代价的优化器 CBO（Cost-Based Optimizer）。优化器针对全面向量化执行引擎做了深度定制，内部实现公共表达式复用、相关子查询重写、Lateral Join、Join Reorder、Join 分布式执行策略选择和低基数字典优化等能力。

#### CBO 的四阶段实现

[`QueryOptimizer.optimizeByCost()`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/fe/fe-core/src/main/java/com/starrocks/sql/optimizer/QueryOptimizer.java) 展示了比“基于代价选择 Join 顺序”更完整的流程：

| 阶段 | 关键动作 | 作用 |
|------|----------|------|
| RuleBaseOptimize | 子查询改写、谓词下推、列裁剪、分区裁剪、UK/FK 裁剪、MV 早期改写等 | 缩小搜索空间并规范逻辑树 |
| Memo 初始化 | 将逻辑表达式放入 `Memo` 的 Group/GroupExpression | 复用等价表达式，避免重复搜索 |
| CostBaseOptimize | `TaskScheduler` 驱动转换/实现规则，派生统计信息和物理属性 | 搜索 Join 顺序、分布方式和算子实现 |
| Physical/Dynamic Rewrite | 低基数字典、Runtime Filter、GLM 等物理树改写与最终校验 | 把跨算子优化落实到可执行计划 |

Memo 的核心价值是把“逻辑等价性”和“物理实现”分开：同一个 Group 可以同时保存不同 Join 顺序或等价表达式，再针对 `Distribution`、`Order` 等 `PhysicalPropertySet` 计算最低成本实现。最终 `extractBestPlan()` 从根 Group 提取满足所需属性的物理树。代价模型依赖行数、列统计和网络/内存估计，因此统计信息失真仍可能导致错误 Join 顺序；SPM 和 Query Feedback 正是在这个边界上提供稳定或纠偏能力。

#### **Colocate属性**

类似于hologres的`tablet_group`的概念。在建表的时候，可以指定`colocate_with`来确定该表的ColocateGroup，<span style="color: rgb(28, 30, 33);">，同一 CG 内的表需遵循相同的 Colocation Group Schema（CGS），即表对应的分桶副本具有一致的分桶键、副本数量和副本放置方式。如此可以保证同一 CG 内，所有表的数据分布在相同一组 BE 节点上。当 Join 列为分桶键时，计算节点只需做本地 Join，从而减少数据在节点间的传输耗时，提高查询性能。因此，Colocate Join，相对于其他 Join，例如 Shuffle Join 和 Broadcast Join，可以有效避免数据网络传输开销，提高查询性能。</span>

参考：[StarRocks Colocate Join](https://docs.starrocks.io/zh/docs/using_starrocks/Colocate_join/)

**Colocate Join**：
- 当两张表的 Distribution Bucket 相同（相同的 Distribution 列和 Bucket 数量）时
- Join 可以在本地执行，无需 Shuffle
- 大幅减少网络数据传输

**Bucket Shuffle Join**：
- 如果两张表 Join 的一侧已按 Join Key 分桶，只需将另一侧 Shuffle 到相同 Bucket 布局，即可实现 Bucket Shuffle Join；
- 减少了一侧数据的shuffle；

**Group Execution**（参考：[MPP Pipeline、Grouped Execution 与 Stage-by-Stage 对比](https://medium.com/starrocks-engineering/mpp-pipeline-vs-grouped-execution-vs-stage-by-stage-f4616052474a)）：
- 每个bucket 之间的数据是正交的，在pipeline调度的时候可以将多个 Bucket 分组执行，减低运行时内存压力，减少任务调度开销；
- 参考 Presto 的设计：[Presto Stage、Source Scheduler 与 Grouped Execution](https://github.com/prestodb/presto/wiki/Stage-and-Source-Scheduler-and-Grouped-Execution)

```text
各 Bucket（按 Custkey 正交划分）
  ├── Bucket 0 ─┐
  ├── Bucket 1 ─┼──▶ Group 1：Scan ─▶ Local Shuffle ─▶ Aggregate
  ├── Bucket 2 ─┘
  ├── Bucket 3 ─┐
  ├── Bucket 4 ─┼──▶ Group 2：Scan ─▶ Local Shuffle ─▶ Aggregate
  └── Bucket 5 ─┘

Classic MPP：所有 Group 同时执行，调度灵活，但峰值内存可能过高。
Grouped Execution：分组、分批调度 Bucket，以并发度换取可控内存和更低调度开销。
```

#### **全局字典编码**

全局字典是 StarRocks 针对低基数 VARCHAR 列的查询优化技术。

**核心思想**：利用存储文件中的字典编码，用整数运算替代字符串比较和聚合、Join，更好地利用FixedLengthInt Column的性能优势；

**支持的列类型**：varchar/array(varchar)/struct(varchar)等；

源码中这项优化横跨 FE 和 BE：FE 的 `CacheDictManager`/`IDictManager` 负责按表、列和版本获取字典；`DecodeCollector` 与 `DecodeRewriter` 判断表达式能否在字典码上执行，并在真正需要字符串的位置插入 `PhysicalDecodeOperator`。BE 的 `GlobalDictCodeColumnIterator` 则把 Segment 内局部字典码转换为查询级全局字典码。于是 Scan、Join、Aggregate、Exchange 可以尽可能传递定长整数，直到输出或不支持的表达式处才 Decode。

```text
Segment 局部字典码
       │  GlobalDictCodeColumnIterator
       ▼
查询级全局字典码（整数）
       ├── Filter / Join / Aggregate / Exchange
       │
       └── PhysicalDecodeOperator ──▶ VARCHAR 输出
```

这项优化的边界也值得注意：字典必须与数据版本兼容；高基数、频繁出现新值或不支持字典改写的表达式会降低收益甚至使字典失效。因此它是由优化器按列选择的物理优化，而不是把所有字符串永久编码成统一 ID。

#### **UK/FK 约束优化**

StarRocks 利用 UK（Unique Key）和 FK（Foreign Key）约束进行查询优化。

**核心思路**：
- 利用约束所蕴含的**基数保持关系**（Cardinality-Preserving Relation），在保持查询语义不变的前提下，裁剪冗余表、重排 Join 顺序、消除不必要的 group by keys/join keys。
- 在MV改写的时候，也可以用UK/FK关系支持Query-Delta的改写（Query的表比MV定义的表少的场景）

源码中的 `UKFKConstraintsCollector` 先自底向上收集唯一性、外键和 Join 属性，`PruneUKFKJoinRule`、`EliminateAggRule` 等规则再使用这些属性。例如，当查询只使用 FK 侧列、Join 不改变基数且过滤条件满足安全约束时，UK 侧可能被裁剪；唯一键也可能让某些 Group By 或 `DISTINCT` 变得冗余。

UK/FK 在这里首先是**优化器契约**。如果约束只声明但数据并不满足，基于约束的裁剪可能破坏语义，因此生产使用时必须保证数据质量，不能把它理解成数据库会自动替用户验证的强约束。

#### **Runtime Filter**

Runtime Filter 是 StarRocks 最重要的运行时优化机制之一，在没有AQE(多阶段Plan调整)调整的情况下，RuntimeFilter在RuntimeFilter阶段自适应地优化Query Plan。
- Local RuntimeFilter (Broadcast Join)
- Global Runtime Filter（Multi-Partition RF）：跨 Partition 的全局 Runtime Filter；可采用分区化布局降低假阳性并支持分布式合并；
- TopN Runtime Filter
- TopN算子产生， 下推到存储层减少IO扫描；
- 当 Aggregate \+ TopN 时，Group By Key 产生 TopN Runtime Filter；
- （TODO）将 Min/Max 谓词下推到 Scan Operator，利用 ZoneMap/Index 过滤减少扫描数据量
- BitSet Runtime Filter
    - 一种 Bloom Filter 的紧凑替代布局；当 Key 范围适合位图表示，且大小足以驻留 Cache、显著小于 Bloom Filter 时使用。

Runtime Filter 的完整链路包含“计划描述、局部合并、跨节点发布、Probe 下推”四步：FE 的 `RuntimeFilterDescription` 决定 Filter ID、Build/Probe 表达式、Join 模式和 Layout；每个 Hash Join Build Driver 生成局部 Filter，`PartialRuntimeFilterMerger` 先在实例内合并；Global Filter 再发送到 FE 选定的 Merge Node；Probe 侧 `RuntimeFilterProbeCollector` 等待并把 Filter 下推到 Scan 或中间算子。

```text
Hash Join Build Drivers
     └── Partial Filters
              │
              ▼
     Instance 内合并
              │
        ┌─────┴──────────┐
        │ Local RF       │ Global RF
        ▼                ▼
  本地 Probe/Scan   Merge Node ──广播──▶ 各 Probe Instance
```

[`RuntimeFilterLayout`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/fe/fe-core/src/main/java/com/starrocks/planner/RuntimeFilterLayout.java) 会根据 Broadcast、Shuffle、Bucket/Colocate 以及 Pipeline 多分区模式选择 Singleton、Pipeline Shuffle、Global 1-Level/2-Level 等布局。Filter 并非越大越好：构建、合并、网络广播和 Probe 计算都有成本，所以实现中还要处理等待超时、选择率更新、Always-True 降级等问题。

#### **全局延迟物化（GLM）**

**核心思想**：
- 在 Join/Sort 等操作中，延迟物化非必需列，只传递 RowID 和必要列，直到最终需要时才 Fetch 完整行数据。
- 通过统一的row\_id，支持了iceberg/内表的全局延迟物化；

**收益**：
- 减少中间结果的数据量（只传递 RowID \+ 必要列），减少网络 Shuffle 开销
- 特别适用于 `SELECT * ... LIMIT N` 场景

```text
Scan A：A.a + A.row_position ─┐
                              ├──▶ Join ──▶ A.row_position + B.row_position
Scan B：B.a + B.row_position ─┘                         │
                                                       ▼
                                      Fetch / Lookup：读取 A.b、B.b
                                                       │
                                                       ▼
                                                   Result Sink
```

[`GlobalLateMaterializationRewriter`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/fe/fe-core/src/main/java/com/starrocks/sql/optimizer/rule/tree/lazymaterialize/GlobalLateMaterializationRewriter.java) 的实现不是简单地“最后补一列”，而是一次全局物理树重写：

1. `SplitProjectionRewriter` 拆开 Projection，暴露可延迟列；
2. `ColumnCollector` 计算每个算子必须立即物化的列；
3. `mergeFetchPosition` 合并多个候选 Fetch 点，避免重复 Lookup；
4. 插入 `PhysicalFetchOperator`，并让 Scan 产生统一 RowID。

在 BE/CN 侧，内表 Lake Connector 与 Hive/Iceberg 路径分别根据 Thrift 中的 `enable_global_late_materialization` 开启 Lazy Read。GLM 的收益取决于“被延迟列宽度 × 中间结果行数”是否足以覆盖 RowID 传递和随机 Lookup 成本；宽表、Join 后强过滤、TopN/Limit 场景通常更有利，返回大比例数据时则未必占优。

#### **Prepared Statement**
```
PREPARE 阶段:
  SQL 文本 → Parser → AST(含 Parameter 节点) → Analyzer → 仅返回元数据(不生成执行计划)

EXECUTE 阶段:
  参数绑定 → 替换 Parameter 节点 → 判断是否 Point Query
    ├─ 是 Point Query:
    │   ├─ 首次执行: 完整规划流程 → 缓存 ExecPlan
    │   └─ 后续执行: 复用缓存计划 → 仅更新谓词和分区裁剪
    └─ 非 Point Query:
        └─ 每次完整规划(不缓存)

```

StarRocks支持行列混存（[StarRocks 行列混存表](https://docs.starrocks.io/zh/docs/table_design/hybrid_table/)），在行存场景下支持PreparedStatement，用于高性能点查。目前PreparedStatement主要用于点查场景，减少FE 优化器额外的开销，但其使用场景比较受限（这块应该不如hologres，hologres可以复用Postgres的[PostgreSQL PREPARE 命令](https://www.postgresql.org/docs/current/sql-prepare.html)）：

| 条件 | 说明 | 示例 |
|------|------|------|
| 单表查询 | 不支持 JOIN | `SELECT &#42; FROM t` |
| OLAP 表 | 仅支持 StarRocks 原生表 | 不支持 Hive/Iceberg 等 |
| 无 LIMIT/OFFSET | - | 不允许分页 |
| 无聚合/HAVING | - | 不允许 GROUP BY |
| 无 ORDER BY | - | 不允许排序 |
| 无 WITH 子句 | - | 不允许 CTE |
| 主键等值查询 | WHERE 条件必须是 `pk_col = ?` | `WHERE id = ?` |
| 完整主键 | 所有主键列都必须有等值条件 | 复合主键需全部匹配 |

实现上，`StmtExecutor.generateExecPlan()` 会从 `PrepareStmtContext` 取出已保存的计划，并检查表和分区版本是否仍允许复用；`ShortCircuitPlanner`/`ShortCircuitOptimizer` 为可支持的点查生成更短的规划路径。如果 Schema、分区或相关元数据变化导致缓存前提失效，系统必须重新规划。因此 Prepared Statement 的性能来源不是“跳过所有校验”，而是把稳定点查中的大部分优化器工作摊销到首次执行。

#### **SQL Plan Manager**

<span style="color: rgb(28, 30, 33);">SQL Plan Manager 允许用户将查询计划绑定到查询上，从而防止查询计划因系统状态变化（主要是数据更新和统计信息更新）而改变，从而稳定查询性能。其工作流程如下：</span>
1. <span style="color: rgb(28, 30, 33);">**创建 Baseline**</span><span style="color: rgb(28, 30, 33);">：使用</span>  `CREATE BASELINE`  <span style="color: rgb(28, 30, 33);">命令将查询计划绑定到指定的查询 SQL；</span>   
    1. <span style="color: rgb(28, 30, 33);">会物化该Query对应的digest pattern的一些Hints，通过Hint固化Join Order和Aggregate多阶段等信息；</span>
    2. <span style="color: rgb(28, 30, 33);">对于同一个Query Digest，根据执行性能更新该物化的base line以保证物化的是最好的plan。</span>
2. <span style="color: rgb(28, 30, 33);">**查询改写**</span><span style="color: rgb(28, 30, 33);">：提交到 StarRocks 的查询会自动与 SQL Plan Manager 中存储的 Baseline 进行匹配。如果匹配成功，则使用 Baseline 的查询计划执行查询。</span>

从调用顺序看，`StatementPlanner.plan()` 在语义分析前先调用 `SPMPlanner.plan()` 做 Baseline 匹配；匹配结果通过 Hint/占位参数影响后续正常优化，而不是绕过 Analyzer 和 CBO 直接执行一棵过期物理树。`SPMOptimizer` 仍参与优化流程，这使 Baseline 可以固定关键形态，同时继续适配当前元数据和执行环境。

参考：[StarRocks SQL Plan Manager](https://docs.starrocks.io/zh/docs/using_starrocks/SQL_plan_manager/)

<span style="color: rgb(28, 30, 33);">目前SQL PlanManager刚开始做，从未来规划上可以承载更多的功能：</span>
- <span style="color: rgb(28, 30, 33);">Plan Cache</span>
- <span style="color: rgb(28, 30, 33);">Plan的多版本管理</span>
- <span style="color: rgb(28, 30, 33);">固定查询计划的自动优化</span>

**查询反馈(HBO)**

目前做的比较轻量级，在FE进程运行期间针对完全相同的Query，通过runtime的stats信息优化Query Plan：
- Join Reorder
- Aggregate Phases

对应源码位于 `fe/.../qe/feedback/`：`SkeletonBuilder` 从物理计划构建稳定的算子骨架，`NodeExecStats` 挂载真实行数与执行统计，`JoinTuningAnalyzer` 和 `StreamingAggTuningAnalyzer` 识别估算偏差，最终由 `PlanTuningAdvisor` 缓存调优建议。它与完整 AQE 的区别是：反馈主要影响后续相同或可匹配查询，而不是在当前查询执行中途任意重写 Fragment DAG。

参考：[StarRocks 查询反馈](https://docs.starrocks.io/zh/docs/using_starrocks/query_feedback/)



### 执行引擎

#### **向量化执行引擎**

传统数据库采用火山模型（Volcano Model），逐行处理数据。向量化执行改为批量处理（Batch Processing），一次处理数千行，充分利用 CPU 缓存和 SIMD 指令。
- 向量化执行框架
- 存储向量化
- 表达式计算向量化

#### **Pipeline 执行引擎**

延伸阅读：[StarRocks Pipeline 执行引擎详解](https://zhuanlan.zhihu.com/p/573181686)

StarRocks V2.0 引入 Pipeline 执行引擎，替代原有的 Volcano 模型。Pipeline 引擎可以理解为具备用户态 `yield` 语义的协作式调度：传统模型依赖 OS 在线程之间切换，而 PipelineDriver 在算子阻塞、等待依赖、时间片耗尽或产生背压时主动归还执行权，从而降低高并发下的线程数和上下文切换成本。需要注意，当前主干实现的核心是 Driver 状态机与调度队列，并不是“每个 Driver 对应一个 C++ 语言协程”。

```text
传统线程调度                              Pipeline 协作式调度
Ready Queue ──▶ OS Scheduler              Ready Tasks ──▶ Worker Thread
                    │                                      │
                    ▼                                      ▼
               Thread 执行                            Pipeline Driver
                    │                                      │
        阻塞时进入内核态切换                    等待 I/O/依赖时主动 yield
                    │                                      │
                    ▼                                      ▼
               Wait Queue                         用户态切换到其他 Driver

传统模型依赖操作系统线程上下文切换；Pipeline 模型在用户态协作式让出执行权。
```

源码中的执行对象有清晰的层级：

```text
QueryContext
  └── FragmentContext
        ├── Pipeline 0
        │     ├── PipelineDriver 0：Source → Operator ... → Sink
        │     └── PipelineDriver 1：Source → Operator ... → Sink
        └── Pipeline 1 ...

GlobalDriverExecutor
  ├── ready queue：可运行 Driver
  ├── blocked/parked：等待 I/O、Runtime Filter、Exchange 或内存
  └── worker threads：每次执行 Driver 的一个时间片
```

`Pipeline` 保存一组 `OperatorFactory`，按 DOP 实例化多份 Operator 并组成 Driver；`FragmentContext.prepare_all_pipelines()` 建立依赖，`prepare_active_drivers()` 准备可运行 Driver，`GlobalDriverExecutor` 从队列取出 Driver 执行。Source/Sink 切断 Pipeline 的边界，Exchange、Hash Join Build、Aggregate Finalize 等阻塞点通过 Dependency/Event 显式表达，因此调度器能区分“没有数据”“下游满了”“依赖未就绪”和“执行结束”。



##### **Event-Driven Schedule**
- 在多核模式下，解决Poller线程CPU打满的问题；
- 每个 Pipeline 由多个 Operator 组成，按事件驱动调度

Event-Driven 的关键不是增加轮询线程，而是让 Pipeline 初始化和 Driver 唤醒依赖事件完成。例如 Hash Join Probe 要等待 Build 完成，自适应 DOP 的下游 Pipeline 要等待采样状态确定；事件完成后才创建或激活对应 Driver，避免未满足依赖的任务反复进入 ready queue。

##### **自适应 DOP（Degree of Parallelism）**
- 默认的`pipeline_dop`为机器核数的一半，但数据量比较少，在并发场景下pipeline本身的调度开销也比较大；
- 因此在不改变DAG pipeline调度的情况下，通过runtime的stats信息，自适应性的将上游数据路由到指定dop的下游算子；

[`CollectStatsContext`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/be/src/exec/pipeline/adaptive/collect_stats_context.h) 用三个状态实现运行时 DOP 选择：

| 状态 | 触发条件 | 行为 |
|------|----------|------|
| Block | 初始采样 | 暂存数据，统计总行数 |
| Passthrough | 达到 `max_block_rows_per_driver_seq × DOP` 且上游未结束 | 保持原 DOP，1:1 直通，避免大数据重分布 |
| RoundRobin | 上游已结束且数据量较小 | 将下游 DOP 降到不超过估算值的 2 的幂，并按 Driver 序号重分布 |

`adjusted_dop = clamp(floor_power_of_2(rows / rows_per_driver), 1, upstream_dop)`。这种设计不会改变 Fragment DAG，只是在 Pipeline 边界插入 CollectStats Sink/Source，并延迟下游 Driver 初始化；它优化的是小数据场景中过度并行产生的调度和内存开销。

#### **Join 优化**

StarRocks 的 Join 算法经历了两个主要版本的演进。

##### **Hash Table种类：**
- **v1: Bucket-Chain Hash Table: Balancing Vectorized Execution with Bandwidth-Optimized Storage**
    - 这是 StarRocks 初期的 Hash Join 实现，核心设计目标是在向量化执行和存储带宽之间取得平衡。
- **v2:  Linear-Chained Hash Table： Adaptive Factorization Using Linear-Chained Hash Tables**
    - 在跟Databricks **Photon** 进行性能对比时，**优化了Hash Join的实现算法**，主要通过Chain \+ Linear Hash Table解决Hash Join冲突较多时的问题；
- **v3: RangeHashMap:** 根据min/max 通过bitset压缩key范围，通过key\_bitset操作快速定位；
- **v4: DirectHashMap:** 每个桶最多一个 key， Probe 时直接计算 hash，定位到桶，比较 key， 无需链式查找，O(1) 时间复杂度；



自适应选择Hash Table:
- V1/V2 触发条件需要同时满足两个维度：数据量与桶深的组合，当数据量较大&桶深（每个bucket包含的keys）较大时选择V2版本；
- 根据数据的range范围及唯一性选择v3/v4;

源码把 Build 侧长期状态与 Probe 侧批次状态分开：`JoinHashTableItems` 保存 `build_chunk`、`first/next`、fingerprint、bitset 和 DenseGroup 等结构；`HashTableProbeState` 保存当前 Probe Chunk 的 hash、匹配位置、输出游标和协程句柄。`calculate_ht_info()` 根据 Build 行数、有效桶数、每桶 Key 数和 Probe 字节量判断是否存在严重 Cache Miss，再选择 Bucket-Chained、Linear 或更直接的布局。

这类“自适应”发生在 Hash Table 已获得真实 Build 数据之后，比只依据 FE 统计信息更可靠；但它仍局限在算子内部，不会重新选择 Join 顺序或把 Broadcast Join 改成 Shuffle Join。换言之，CBO 决定宏观物理计划，Join Runtime 根据真实 Key 分布选择微观数据结构。

##### **基于Fixed Length Hash处理String Join Key**

**问题**：字符串列作为 Join Key 时，每次比较都需要访问字符串内容，内存访问模式差，缓存命中率低。

**优化**：
- **Build 端**：序列化后的 Key 存储在连续的内存数组中（`Buffer<CppType>`），更有利于 Cache Line；
- **Probe 端**：序列化后的 Key 同样存储在连续数组中
- **比较**：直接进行整数比较（`==`），而不是字符串的 memcmp；

| 维度 | 传统 String Join | Fixed Length Hash Join |
|------|------------------|----------------------|
| **Key 存储** | `Slice`（16 字节指针\+长度）\+ 字符串数据 | 固定长度整数（4/8/16 字节） |
| **Key 比较** | memcmp（可能多次缓存未命中） | 单条整数比较指令 |
| **内存局部性** | 差（字符串分散在堆上） | 优（连续数组） |
| **缓存命中率** | 低（随机访问字符串内存） | 高（顺序访问整数数组） |
| **Build 内存** | `16 + 平均字符串长度` 字节/行 | `4/8/16` 字节/行（固定） |
| **Probe 开销** | 每次比较都需反序列化 | 预序列化，直接比较 |

`join_key_constructor.hpp` 在 Build 和 Probe 两侧使用同一套固定长度序列化逻辑，保证 Key 的字节布局一致。优化收益来自连续内存与整数比较，但只适用于序列化后能安全落入有限宽度的 Key 组合；超长字符串或复杂可变长组合仍需走 Slice/序列化 Key 路径。

##### **Partitioned Hash Join**

**问题**：当 Build 侧数据过大（超过内存限制）时，无法一次性构建 Hash Table，需要将数据分区处理。

**方案**：
- 将 Build 侧和 Probe 侧数据按相同的 Hash 函数分区，每个分区独立构建 Hash Table 并 Probe。
- 但这里Overhead就是增加了一次local shuffle的开销，所以需要一个精细化的buffer管理及自适应策略来判断是否使用该策略；

[`ChunksPartitioner`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/be/src/exec/partition/chunks_partitioner.h) 负责按分区表达式计算 Hash、缓存各 Partition 的 Chunk，并在达到批量阈值后交给消费者。Partitioned Hash Join 的本质是用一次本地重分区把“大而不可控的 Hash Table”拆成多个可独立 Build/Probe 的工作集；收益是限制峰值内存和支持 Spill，代价是额外 Hash、数据搬运以及分区倾斜风险。

##### **Coroutine Probe**

延伸阅读：[StarRocks Hash Join Coroutine Probe](https://zhuanlan.zhihu.com/p/666465496)

**问题**：传统的 Probe 过程是同步的，当 Probe 侧数据量大时，会长时间占用 CPU 且无法响应 Backpressure。同时，在 One-to-Many 场景下（一个 Probe 行匹配多个 Build 行），单次 Probe 可能产出大量结果，导致内存峰值。

这里的 Coroutine 是 Join Probe 内部使用的 C++20 协程，与前文 PipelineDriver 的协作式调度不是同一层机制。`HashTableProbeState` 保存多个 coroutine handle 和输出游标，让多个独立 Probe 流交错推进，以重叠随机内存访问延迟；当 One-to-Many 输出达到 Chunk 上限时，协程可以挂起，下次从同一链位置继续，而不需要一次物化全部匹配行。它改善的是 Hash Table 访问的内存级并行度和输出节流，不改变 Join 的逻辑语义。

#### **Aggregate**

StarRocks 在聚合算子上做了大量优化：

##### **自适应预聚合**
- 在 Partial Aggregate 阶段，自适应输出 Partial/Final 结果，根据Runtime数据聚合状态&系统内存状况动态调整聚合策略，是否pre-aggregate/force-aggregate/passthrough。

##### **基于 Meta信息优化Simple聚合函数函数**
- 利用列的 Min/Max或者count信息，优化count/min/max聚合函数；在Clickbench下有很好的性能收益；

##### **Aggregate \+ Limit 优化**
- Group By Keys 产生 Runtime Filter，下推到 Scan Operator
- 利用 Index 过滤减少扫描数据量

##### **Aggregate \+ TopN 优化**
- Step1: 当 Group By Key 与 Order By Key 相同时，将 TopN 下推到 Partial Aggregate，减少 Final Blocking Aggregate 再做 TopN 的开销
- Step2: TopN Runtime Filter 将 Min/Max 谓词下推到 Scan Operator，减少IO开销

#### **Sort 优化**

##### **German String（StringView / ColumnViewer）**
- <span style="color: rgb(33, 37, 41);">StarRocks 没有端到端地替换现有字符串 Column，而是在 Full Sort 等构建临时排序列的路径中使用 StringView 风格布局：短字符串直接内联，长字符串在 View 中保存长度、Prefix 和 Buffer 位置，利用内联 Prefix 减少比较时的随机内存访问和 Cache Miss。</span>

```text
传统 StringArray                        StringViewArray（每个 View 16 Bytes）
┌─────────────┐                         ┌──────────────────────────────┐
│ Offsets     │──┐                      │ length │ inline / reference  │
└─────────────┘  │                      └──────────────────────────────┘
                 ▼
┌──────────────────────────┐            长度 ≤ 12：字符串直接内联在 View 中
│ 连续字符串 Buffer        │            长度 > 12：length + prefix +
└──────────────────────────┘                       buffer index + offset
                                                        │
                                                        ▼
                                               外部字符串 Buffer
```

参考：[DataFusion：String View / German-style Strings](https://datafusion.apache.org/blog/2024/09/13/string-view-german-style-strings-part-1/)

优化效果：

| Query | 未使用 German String | 使用 German String | 加速比 |
|:-----:|---------------------:|--------------------:|-------:|
| Q1 | 9.300 | 6.109 | 1.52x |
| Q2 | 7.687 | 7.787 | 0.99x |
| Q3 | 26.613 | 17.547 | 1.52x |
| Q4 | 7.895 | 6.444 | 1.23x |
| Q5 | 8.601 | 10.120 | 0.85x |
| Q6 | 46.021 | 23.441 | 1.96x |
| Q7 | 8.266 | 9.707 | 0.85x |

原始材料没有标注耗时单位与完整测试配置，因此这组数据只能说明优化具有明显的 Query 相关性：Q1/Q3/Q6 收益较高，Q2 基本持平，Q5/Q7 反而回退。工程上应由代价或适用条件控制该表示，而不能把微基准中的最佳值外推为所有 Sort 的固定收益。

##### **Merge Path Parallel Merge Sort**
- 参考 DuckDB 的并行归并排序实现：[DuckDB：并行外部排序](https://duckdb.org/2021/08/27/external-sorting)
- 使用 Merge Path 算法实现高效并行归并，解决global sort时单点性能问题；

## 五：数据湖 & MV

**数据湖**

<span style="color: rgb(28, 30, 33);">StarRocks 不仅能高效的分析本地存储的数据，也可以作为计算引擎直接分析数据湖中的数据。用户可以通过 StarRocks 提供的 External Catalog，轻松查询存储在 Apache Hive、Apache Iceberg、Apache Hudi、Delta Lake 等数据湖上的数据，无需进行数据迁移。</span>

<span style="color: rgb(28, 30, 33);">在数据湖分析场景中，StarRocks 主要负责数据的计算分析，而数据湖则主要负责数据的存储、组织和维护。使用数据湖的优势在于可以使用开放的存储格式和灵活多变的 schema 定义方式，可以让 BI/AI/Adhoc/报表等业务有统一的 single source of truth。而 StarRocks 作为数据湖的计算引擎，可以充分发挥向量化引擎和 CBO 的优势，大大提升了数据湖分析的性能。</span>

```text
                         StarRocks
                ┌───────────┴───────────┐
                │                       │
        Internal Catalog        External Catalog 1..N
                │                       │
             Database                 Database
                │                       │
          StarRocks 内表        Hive / Iceberg / Hudi / Delta 表
                                        │
                                        ▼
                              外部数据湖中的开放格式数据

同一条 SQL 可以跨 Catalog 访问内表与外表，完成联邦分析。
```

### Connector 如何接入统一查询引擎

StarRocks 没有让每种外部数据源各自实现一套 SQL 引擎，而是把差异限制在元数据和 Scan 两个边界：

```text
FE
MetadataMgr
  └── ConnectorMetadata
        ├── 获取 Database / Table / Partition
        ├── 获取统计信息
        └── getRemoteFiles[Async]：文件、Split、Delete File
                              │
                              ▼
                    Hdfs/Iceberg ScanNode
                              │ Thrift Scan Ranges
                              ▼
BE/CN
Connector Scan Operator
  └── DataSource（Hive / Iceberg / Hudi / Paimon / ...）
        └── Reader → 列式 Chunk → 通用 Filter/Join/Aggregate
```

FE 的 [`ConnectorMetadata`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/fe/fe-core/src/main/java/com/starrocks/connector/ConnectorMetadata.java) 是 Catalog SPI，`MetadataMgr` 在内表 LocalMetastore 与外部 Connector 之间提供统一入口。优化器看到的仍是 `PhysicalScanOperator`，从而可以复用谓词下推、列裁剪、Join Reorder、Runtime Filter 和 MV 改写；直到 ScanNode 生成 Scan Range 时，文件格式、Snapshot 与 Delete File 等差异才具体化。

BE/CN 侧 Connector DataSource 把外部 Reader 适配为 `Chunk`。这解释了“无需搬迁数据”与“仍能复用向量化执行”如何同时成立：数据格式没有被 StarRocks 接管，但文件裁剪之后的批数据进入了同一套列式 Runtime。性能上限则同时受对象存储吞吐、元数据规模、文件布局、网络和 Cache 命中率影响，不能只看执行算子速度。



StarRocks 将 Iceberg 作为湖上能力的重点：从最初读取 Iceberg 数据，逐步扩展到写入、增量读取和更丰富的 Procedures，对 Apache Iceberg 的支持日益完善：

| 功能 | 说明 |
|------|------|
| **增量投递** | 支持 Iceberg 的增量读取，避免全表扫描（参考trino） |
| **分布式 Plan** | Iceberg Snapshot 的 Plan 阶段分布式执行，加速元数据收集 |
| **Variant 类型支持** | 支持 Iceberg 的 Variant 半结构化数据类型 |
| **V3 版本支持/Row Lineage (CDC)** | 基于 Row Lineage 的 CDC 能力，支持增量物化视图 |
| **Procedure 支持** | `expire_snapshots`、`rewrite_manifests`、`rewrite_data_files`、`register_table` 等维护操作 |

以 Iceberg 为例，`IcebergMetadata` 负责表与 Snapshot 语义，`IcebergRemoteSourceTrigger`/异步 RemoteFile Source 负责逐步产生文件任务，`IcebergScanNode` 再构造分布式 Scan Range。对于元数据量很大的表，异步或分布式规划的关键价值是避免 FE 必须一次性枚举并持有所有文件，而不是把 CBO 本身搬到 BE。Iceberg V2 的 Position/Equality Delete、V3 Row Lineage 和增量范围还会改变 Scan 输出列与删除合并逻辑，因此“支持 Iceberg”远不只是能读 Parquet。

### **MV**

MV在StarRocks承载了透明加速(通用index)、ETL Pipeline、数据建模等功能，同时是商业化时差异化的关键Feature之一。

StarRocks MV的主要特点：
    - MV在Lakehouse**湖仓一体**中也承载着很关键的性能优化的作用，MV支持多种湖上数据源自动刷新：[使用物化视图加速数据湖查询](https://docs.starrocks.io/zh/docs/using_starrocks/async_mv/use_cases/data_lake_query_acceleration_with_materialized_views/)；
    - 丰富的透明改写加速用户场景的Query，<span style="color: rgb(28, 30, 33);">在以下场景下特别有用：</span>指标预聚合、宽表 Join、湖仓加速。[使用物化视图进行透明查询改写](https://docs.starrocks.io/zh/docs/using_starrocks/async_mv/use_cases/query_rewrite_with_materialized_views/)；
    - 增量 MV 是 Data Freshness 的关键，MV 也承载着 StarRocks 迈向“流批一体”、拓展使用场景的核心功能；

```text
ODS：Hive / Hudi / Iceberg / Delta Lake
  │
  ├──▶ External-table MV ──▶ DWD ──▶ DWS ──▶ ADS
  │                              │       │       │
  │                              │       │       └── 固定报表
  │                              │       └────────── BI / OLAP
  │                              └────────────────── Ad-hoc
  │
  └── 基表 + 维表 ──▶ 物化视图（预计算）
                            ▲
                            └── 查询自动改写并命中 MV
```



```text
外表分区 A / B / C
       │
       └── MV Task ──▶ StarRocks MV 分区 A / B / C
                               │
                               └── MV Task ──▶ 下游嵌套 MV 分区 ...
                                                      ▲
SQL Query ── 自动按需改写 ──┬──────────────────────────┘
                            ├──▶ 命中中间 MV
                            └──▶ 无法改写时访问外表
```

| 能力 | 说明 |
|------|------|
| 物化视图构建 | 支持复杂查询、直接查询 MV、基于外表构建和嵌套 MV |
| 物化视图刷新 | 异步刷新不影响数据写入；支持周期、修改触发、分区级刷新及分区依赖推导 |
| 查询自动改写 | 支持 Scan、Filter、Aggregation、Join、Union、Cross Join 和 Delta Join |

### MV 的两条独立链路：刷新与查询改写

高质量理解 MV 的关键，是不要把“数据是否新鲜”和“查询能否等价改写”混为一谈：

```text
刷新链路
Base Table 版本/分区变化
  └── MV Refresh Task
        ├── 选择需要刷新的 MV Partition
        ├── 生成 INSERT/刷新执行计划
        └── 成功后更新 BaseTableVisibleVersionMap

改写链路
Query Logical Plan
  └── MvRewritePreprocessor：收集候选 MV + 新鲜度检查
        └── MaterializedViewRewriter：谓词/Join/Aggregate 补偿
              ├── 完全命中：直接 Scan MV
              ├── 部分新鲜：MV UNION Base Table 补偿
              └── 不满足等价性：保留原计划
```

刷新侧以 `MaterializedViewMgr`、`BaseMVRefreshProcessor`、`MVRefreshPartitionSelector` 和 `MVVersionManager` 为核心。分区级刷新会维护 MV 分区与基表分区的映射，并在成功后更新版本元数据；外表还要先刷新 Connector 元数据，否则“基表是否变化”的判断本身可能过期。

改写侧在 CBO 中由 [`MvRewritePreprocessor`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/fe/fe-core/src/main/java/com/starrocks/sql/optimizer/MvRewritePreprocessor.java) 准备候选集，再由单表/多表 MV 规则处理谓词包含关系、列映射、聚合 Roll-up、Join 等价和分区补偿。改写成功后还会重新执行谓词下推与分区裁剪，因为 `UNION` 补偿会生成新的 Scan 分支。

由此可见，MV 的主要风险不是“刷新慢”一个维度：候选 MV 过多会增加优化器搜索成本；分区映射错误会影响新鲜度；统计信息过期可能让命中 MV 的计划仍然不优；嵌套 MV 则需要限制改写层数，避免组合爆炸。



## 六：存算分离

存算分离（Shared-Data）是 StarRocks V3.0 的核心特性，后续云原生 Feature 的研发重心明显向这一架构倾斜；但具体功能是否同时支持 Shared-Nothing，应以对应版本文档和源码开关为准。

**追求目标**：Freshness + Cost

```text
                    ┌── Loading Warehouse：CN ... CN
                    │
共享对象存储 ───────┼── Ad-hoc Warehouse：CN ... CN
（同一份数据）      │
                    └── BI Report Warehouse：CN ... CN

Warehouse 之间：工作负载物理隔离
Warehouse 内部：根据负载独立、按需弹性扩缩容
目标：共享一份数据，在数据新鲜度（Freshness）与成本（Cost）之间取得平衡。
```

### Shared-Data 的版本发布模型

从源码看，Shared-Data 的关键并不是“Segment 放到 S3”这一件事，而是把 Tablet 的持久状态拆成不可变数据文件、版本化 Metadata 与事务日志：

```text
写入阶段
CN DeltaWriter ──▶ Segment / Delete File ──▶ 对象存储
       │
       └──────────▶ TxnLog(txn_id)：记录本次写入产生的文件

发布阶段
旧 TabletMetadata(version N) + TxnLog
       │
       └── publish ──▶ 新 TabletMetadata(version N+1)
                              │
查询阶段                      ├── Rowsets / Segments
CN ── LocationProvider ───────┤
     + Metadata/Data Cache    └── DelVector / Primary Index 元数据
```

[`lake::TabletManager`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/be/src/storage/lake/tablet_manager.h) 通过 `LocationProvider` 解析 Tablet Metadata、Txn Log 和数据文件位置，并为元数据提供本地缓存。普通表的 Publish 将 Txn Log 合并到新版本 Metadata；主键表则由 Lake `UpdateManager.publish_primary_key_tablet()` 同时更新 Primary Index 和 DelVector。版本文件不可变，使并发读可以固定在一个快照上，也让故障恢复能够从共享存储重建状态。

与 Shared-Nothing 相比，这种模型把“本地副本一致性”转化为“对象存储文件 + 版本发布一致性”，扩缩容更容易，但也引入新的成本：对象存储请求延迟、元数据文件数量、Cache 冷启动、Publish 冲突、孤儿文件回收和跨 CN Compaction 协调。

### Group Commit

**场景**：Streaming Load 中，结合 Flink 的特性，攒批提交。

**原理**：
- 多个小批量写入请求合并为一个 Group
- 减少 Tablet Meta 的更新频率
- 降低写入延迟

当前源码中相关能力以 **Merge Commit / Batch Write** 命名。FE 的 `BatchWriteMgr` 按 `BatchWriteId` 管理合并写任务，`CoordinatorBackendAssignerImpl` 为同构请求选择协调 BE/CN；BE 的 `BatchWriteMgr` 与 `IsomorphicBatchWrite` 复用 Stream Load Pipe 和执行计划，把相同表、参数和 Warehouse 的小请求汇入同一批次。批次由时间与数据量阈值共同关闭，然后只做一次事务提交。

因此 Group Commit 优化的不是单行编码速度，而是摊薄事务、计划、RPC、版本发布和小文件成本。代价是引入一个可控的等待窗口；低流量场景可能增加单请求延迟，参数过大又会放大单批内存和失败重试范围。

### File Bundling

**场景**：存算分离下减少 Tablet Meta 信息的维护开销。

**原理**：
- 将多个小 Segment 文件打包为一个 Bundle 文件
- 减少对象存储上的文件数量
- 降低 Meta 信息的管理开销
- 提高读取时的 IO 效率

实现上，[`BundleWritableFile`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/be/src/fs/bundle_file.h) 让多个逻辑 Segment 共享一个底层 `WritableFile`，每个 Segment 在 Rowset Metadata 中记录 `bundle_file_offset`。读取时 `BundleSeekableInputStream` 将逻辑文件的 offset/size 映射到 Bundle 中的一段范围，而上层 Segment Reader 仍把它当作独立文件处理。

这意味着 File Bundling 不是传统压缩包：Segment 的编码和逻辑边界仍然保留，只是共享对象存储 Object。收益是显著减少 PUT/GET 和对象数量；复杂性转移到 Offset 一致性、Vacuum 引用判断和共享文件生命周期——只要 Bundle 中仍有一个 Segment 被引用，就不能删除整个对象。

### 超大 Tablet 写入性能优化（进行中）

**场景**：单 Tablet 超过 100GB 时的写入性能优化。

**优化方向**：
- 单 Tablet 内的并行写入
- Compaction 策略优化（避免大 Tablet Compaction 阻塞）
- 内存管理优化（减少大 Tablet 写入时的内存峰值）

### Range Tablet （进行中）

**场景**：解决 Hash Tablet 数据分布倾斜的问题。

**原理**：
- 传统 Hash Tablet 按 Hash 值分布，可能导致某些 Tablet 数据量过大
- Range Tablet 按 Key 的范围分布，保证数据均匀
- 适用于有序 Key 的场景（如时间戳、自增 ID）

### AutoScaling

#### Multi Warehouse
- 支持多个 Warehouse（计算集群）
- 不同 Warehouse 独立资源，互不影响
- 提供面向`etl_mode`场景的warehouse参数配置（默认开启spill、支持全局队列），支持`etl`长任务；
- 适用于多租户场景

`WarehouseManager.acquireComputeResource()` 通过 `ComputeResourceProvider` 为查询选择 Warehouse 内的计算资源，Coordinator 后续只在该资源对应的 CN 集合中放置 Fragment Instance。因而 Warehouse 隔离主要发生在**计算与调度域**，底层对象数据仍可共享；这正是 Loading、Ad-hoc 和 BI 可以互不争抢 CPU/内存但读取同一份数据的基础。

#### CPU-Based/Queue-Based Auto Scaling
- 从基于 CPU 利用率的 AutoScaling 改为基于队列的Auto Scaling
- 队列深度更准确地反映实际负载
- 避免 CPU 利用率的滞后性

#### 多 AZ 部署
- 从单 Warehouse 多 CN 扩展到多 Cluster， 支持跨可用区（Availability Zone）部署
- 容错：单 AZ 故障不影响服务
- 减少跨 AZ 流量：优先本 AZ 读取，同时支持跨 Cluster 的数据共享；

---

## 七：可观测性与AI Native

### **可观测性**

StarRocks 在可观测性方面提供了完善的工具链：
- [StarRocks 查询规划与调优](https://docs.starrocks.io/docs/best_practices/query_tuning/query_planning/)

#### **Profile**
- 详细的算子级指标：执行时间、CPU 时间、内存使用、数据扫描量、Shuffle 数据量
- 每个算子内部的子指标都可以在文档中找到详细说明
- 支持 Web UI 可视化 Profile 分析

Profile 的展开层级如下：

```text
Profile
  ├── Fragment 0..N
  │     ├── Instance 0..N
  │     │     ├── Pipeline 0..N
  │     │     │     ├── PipelineDriver 0..N
  │     │     │     │     └── Operator 0..N
  │     │     │     └── ...
  │     │     └── ...
  │     └── ...
  └── ...
```

以 `AGGREGATION_NODE` 为例，Profile 会展示下列算子语义和指标：

| 指标 | 示例值 | 含义 |
|------|-------:|------|
| Active | 5s179ms | 算子 Active 时间；示例中 non-child 占比 0.01% |
| GroupingKeys | `ca_zip` | 分组键 |
| AggregateFunctions | `sum(sum(cs_sales_price))` | 聚合函数 |
| AggComputeTime | 113.465us | 构建 Hash 表并计算聚合函数的时间 |
| ExprComputeTime | 45.97us | 计算聚合函数内部标量表达式的时间 |
| ExprReleaseTime | 207.812us | 表达式相关内存释放时间 |
| GetResultsTime | 24.383us | 将 Hash 表数据转换成 Chunk 的时间 |
| HashTableSize | 19 | Hash 表中的分组数 |
| InputRowCount | 230 | 输入行数 |
| PassThroughRowCount | 0 | Streaming 聚合时未经过 Hash 表、直接输出的行数 |
| PeakMemoryUsage | 4.79 KB | 峰值内存使用量 |
| ResultAggAppendTime | 3.763us | 聚合结果追加耗时 |
| ResultGroupByAppendTime | 1.886us | Group By 结果追加耗时 |
| ResultIteratorTime | 13.829us | 结果迭代耗时 |
| RowsReturned | 19 | 返回行数 |
| RowsReturnedRate | 3 | 返回速率 |
| StreamingTime | 0ns | Streaming 聚合中聚合函数列格式转换耗时 |

Profile 的数据流同样对应前面的执行层级：每个 Operator 把 Counter 挂到 Driver Profile；BE/CN 的 `GlobalDriverExecutor.report_exec_state()` 可以先合并同构 Driver Profile，再通过 `report_exec_status` 返回 FE；`DefaultCoordinator.updateRuntimeProfile()` 汇入 Query Profile，最后由 `StmtExecutor` 加上 Summary、Planner Tracer 和 SQL 信息并交给 `ProfileManager`。

调优时不能只看总耗时，建议按以下顺序缩小范围：

1. **先看 Fragment/Operator 的 `Max` 与 `Avg` 差异**：差异大通常意味着数据倾斜、慢节点或 Cache 命中不均；合并后的平均值可能掩盖长尾。
2. **区分 CPU、等待和 I/O**：Operator Active Time 高不一定等于 CPU 高，还要看 InputEmpty、OutputFull、I/O、Runtime Filter Wait 等时间。
3. **核对估算行数与真实行数**：数量级偏差会解释错误 Join 顺序、Broadcast 选择或聚合阶段；这也是 Query Feedback 的输入。
4. **从 Scan 到 Exchange 看放大率**：读取行数、过滤后行数、Shuffle Bytes 和输出行数能定位谓词未下推、Runtime Filter 未命中或中间结果膨胀。



#### **Explain**
- `EXPLAIN`：展示规划后的物理 Fragment、算子、分布方式和估算信息，而非优化器内部完整逻辑树
- `EXPLAIN VERBOSE` / `EXPLAIN COSTS`：展示更详细的 Slot、表达式、统计信息与 CBO Cost
- `EXPLAIN ANALYZE`：真正执行查询，并把真实运行统计映射回物理计划，可用于定位算子瓶颈
- `TRACE LOGS MV` / `TRACE LOGS OPTIMIZER`：输出 MV 候选、改写过程或优化器规则 Trace

下面的示例应按“估算值”阅读：`Estimates.row` 是 CBO 输入，`cpu/memory/network/cost` 是代价模型结果，并非运行时实测。需要用 `EXPLAIN ANALYZE` 或 Query Profile 将其与 `RowsReturned`、`InputRowCount` 等真实指标对照。
```
- Output => [69:count]
    - TOP-100(FINAL)[69: count ASC NULLS FIRST]
            Estimates: {row: 1, cpu: 8.00, memory: 8.00, network: 8.00, cost: 68669801.20}
        - TOP-100(PARTIAL)[69: count ASC NULLS FIRST]
                Estimates: {row: 1, cpu: 8.00, memory: 8.00, network: 8.00, cost: 68669769.20}
            - AGGREGATE(GLOBAL) []
                    Estimates: {row: 1, cpu: 8.00, memory: 8.00, network: 0.00, cost: 68669737.20}
                    69:count := count(69:count)
                - EXCHANGE(GATHER)
                        Estimates: {row: 1, cpu: 8.00, memory: 0.00, network: 8.00, cost: 68669717.20}
                    - AGGREGATE(LOCAL) []
                            Estimates: {row: 1, cpu: 3141.35, memory: 0.80, network: 0.00, cost: 68669701.20}
                            69:count := count()
                        - HASH/INNER JOIN [9:ss_store_sk = 40:s_store_sk] => [71:auto_fill_col]
                                Estimates: {row: 3490, cpu: 111184.52, memory: 8.80, network: 0.00, cost: 68668128.93}
                                71:auto_fill_col := 1
                            - HASH/INNER JOIN [7:ss_hdemo_sk = 25:hd_demo_sk] => [9:ss_store_sk]
                                    Estimates: {row: 19940, cpu: 1841177.20, memory: 2880.00, network: 0.00, cost: 68612474.92}
                                - HASH/INNER JOIN [4:ss_sold_time_sk = 30:t_time_sk] => [7:ss_hdemo_sk, 9:ss_store_sk]
                                        Estimates: {row: 199876, cpu: 69221191.15, memory: 7077.97, network: 0.00, cost: 67671726.32}
                                    - SCAN [store_sales] => [4:ss_sold_time_sk, 7:ss_hdemo_sk, 9:ss_store_sk]
                                            Estimates: {row: 5501341, cpu: 66016092.00, memory: 0.00, network: 0.00, cost: 33008046.00}
                                            partitionRatio: 1/1, tabletRatio: 192/192
                                            predicate: 7:ss_hdemo_sk IS NOT NULL
                                    - EXCHANGE(BROADCAST)
                                            Estimates: {row: 1769, cpu: 7077.97, memory: 7077.97, network: 7077.97, cost: 38928.81}
                                        - SCAN [time_dim] => [30:t_time_sk]
                                                Estimates: {row: 1769, cpu: 21233.90, memory: 0.00, network: 0.00, cost: 10616.95}
                                                partitionRatio: 1/1, tabletRatio: 5/5
                                                predicate: 33:t_hour = 8 AND 34:t_minute >= 30
                                - EXCHANGE(BROADCAST)
                                        Estimates: {row: 720, cpu: 2880.00, memory: 2880.00, network: 2880.00, cost: 14400.00}
                                    - SCAN [household_demographics] => [25:hd_demo_sk]
                                            Estimates: {row: 720, cpu: 5760.00, memory: 0.00, network: 0.00, cost: 2880.00}
                                            partitionRatio: 1/1, tabletRatio: 1/1
                                            predicate: 28:hd_dep_count = 5
                            - EXCHANGE(BROADCAST)
                                    Estimates: {row: 2, cpu: 8.80, memory: 8.80, network: 8.80, cost: 44.15}
                                - SCAN [store] => [40:s_store_sk]
                                        Estimates: {row: 2, cpu: 17.90, memory: 0.00, network: 0.00, cost: 8.95}
                                        partitionRatio: 1/1, tabletRatio: 1/1
                                        predicate: 45:s_store_name = 'ese'
```

#### **运行时监控**
- 提供了运行时详尽的metrics信息，可以提供Prometheus监控报警：
    - 查询级别监控：QPS、延迟、错误率
    - 节点级别监控：CPU、内存、磁盘、网络

---

### **AI Native**

#### **AI-Agent**

StarRocks 已推出 [StarRocks AI Agent](https://ai-agent.starrocks.com/)，主要是协助DBA、RD排查问题：

| 功能 | 说明 |
|------|------|
| **Profile 分析** | AI 自动分析 Query Profile，定位性能瓶颈 |
| **Query 优化** | 根据 SQL 和 Profile 给出优化建议 |
| **文档搜索** | 基于 RAG 的文档检索 |
| **Table 优化** | 表结构设计建议（Partition、Distribution 等） |

#### **运维 Agent**
- 自动监控线上异常
- 计算节点 CPU/Mem/Disk 异常检测
- 自动告警和根因分析

## 八：性能 Benchmark & 竞对分析

瞄准一个好的对手，也是致胜的关键；
- 聚焦海外市场Customer Facing场景
- 极致性能、Data Freshness

### **单表场景**

##### **ClickBench**

ClickBench 是目前最权威的 OLAP 基准测试：[ClickBench 官网](https://benchmark.clickhouse.com/)

**StarRocks 的优化点**：
- **Count 走 Meta Scan**：`SELECT COUNT(*)` 直接读取 Tablet Meta，避免全表扫描
- **Single Node 优化**： 
    - MPP 架构即使针对单机也需要 Shuffle（数据拷贝），可以 Bypass 不走 Localhost 网络
    - 单机引擎（DuckDB/ClickHouse）通过并行 Hash Table 优化，StarRocks 也在追赶

##### **SSB / SSB Flat**

SSB（Star Schema Benchmark）和 SSB Flat 是经典的数仓基准测试。

StarRocks 在 SSB 上的优势主要来自：
- 向量化执行引擎
- Colocate Join（避免 Shuffle）
- Runtime Filter

### **TPC-H / TPC-DS**

**内表场景**：StarRocks 相比 Snowflake 有 ~3X 的性能优势，主要来自：
- 聚合下推到 Scan 层
- Runtime Filter 减少中间结果
- CTE Reuse 避免重复计算



**湖上场景**：
- StarRocks 只查湖上表相比 Trino 有 3X 性能提升。
- StarRocks Iceberg外表相比Databricks Delta Table有2X性能提升；

原图中的两组基准结果可以概括为：

| 测试场景 | 数据/资源配置 | 对比结论 |
|----------|---------------|----------|
| 直接查询数据湖 | Iceberg、TPC-H、4 个 `16C128G` 计算节点 | Q1～Q22 中 StarRocks 的柱状耗时整体显著低于 Trino，综合快约 3～5 倍 |
| 内表与数据湖 | Q01～Q13；`lake_no_cache`、`lake_with_cache`、`native` 三组 | 内表查询相比直接查湖约有 3 倍性能提升 |
| Local Cache | Q01～Q13，命中本地缓存 | `lake_with_cache` 的耗时与 `native` 基本持平；该对比尚未利用 StarRocks Colocate 等内表特性 |

| 竞品 | 定位 | 特点 | 与 StarRocks 对比 |
|------|------|------|--------------------|
| **ClickHouse** | 单机之王 | 可观测性生态好，迭代快（200\+ commits/天），存算分离/湖上数据/MV 快速发展 | StarRocks 在分布式场景更强，CH 在单机场景领先 |
| **Trino** | 湖上引擎 | 湖上生态完善，Failover 能力强 | StarRocks 湖上性能追赶中，内表优势明显 |
| **Snowflake** | 云数仓 | SaaS 模式，生态完善 | StarRocks 开源 \+ 性能优势，Snowflake 生态优势 |
| **Databricks** | 湖仓一体 | Delta Lake 生态，AI 集成 | StarRocks 在 Iceberg 生态追赶，Delta 支持较弱 |

### 如何正确解读这些 Benchmark

上述倍数来自原始分享材料，本文没有在相同硬件、数据版本和参数下独立复现，因此应当视为特定配置下的观察，而不是无条件产品结论。尤其是跨系统对比，至少需要公开以下变量才具备可复现性：

| 维度 | 必须控制或披露的变量 |
|------|----------------------|
| 数据 | Scale Factor、数据格式、压缩算法、文件大小、分区与排序方式 |
| 资源 | 节点数、CPU 型号与核数、内存、磁盘/对象存储、网络带宽 |
| 系统 | 版本/Commit、配置、并发、资源组、统计信息、Compaction 状态 |
| Cache | 冷启动/热启动、OS Page Cache、Data Cache、元数据缓存是否预热 |
| 结果 | 单 Query 延迟与吞吐分开报告，给出多轮 P50/P95 而非只取最佳值 |

对 StarRocks 而言，Benchmark 最有价值的用法不是证明“绝对更快”，而是把 Query 形态映射回源码机制：ClickBench 观察 Scan、表达式与单节点开销；SSB 观察 Colocate Join 和 Runtime Filter；TPC-H/TPC-DS 观察 CBO、统计信息、复杂 Join 与 Spill；湖上测试则额外观察文件裁剪、Connector、对象存储和 Cache。

### 关键源码阅读索引

以下路径都固定到本文分析基线，便于读者复核：

| 主题 | 建议入口 | 阅读重点 |
|------|----------|----------|
| SQL 生命周期 | [`StatementPlanner.java`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/fe/fe-core/src/main/java/com/starrocks/sql/StatementPlanner.java) | Analyze → Transform → Optimize → ExecPlanBuild |
| CBO | [`QueryOptimizer.java`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/fe/fe-core/src/main/java/com/starrocks/sql/optimizer/QueryOptimizer.java) | RBO、Memo、CBO、Physical Rewrite |
| 分布式调度 | [`DefaultCoordinator.java`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/fe/fe-core/src/main/java/com/starrocks/qe/DefaultCoordinator.java) | ExecutionDAG、实例放置、RPC、Profile |
| Pipeline | [`fragment_executor.cpp`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/be/src/exec/pipeline/fragment_executor.cpp) | Fragment 准备、Pipeline 构建、Driver 提交 |
| Driver 调度 | [`pipeline_driver_executor.cpp`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/be/src/exec/pipeline/pipeline_driver_executor.cpp) | 时间片、阻塞/唤醒、Profile 上报 |
| Hash Join | [`join_hash_table_descriptor.h`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/be/src/exec/join/join_hash_table_descriptor.h) | Chained/Linear、Probe State、Coroutine |
| 主键表 | [`tablet_updates.h`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/be/src/storage/tablet_updates.h) | EditVersion、Apply、Primary Index、DelVector |
| Runtime Filter | [`runtime_filter_probe.h`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/be/src/runtime/runtime_filter/runtime_filter_probe.h) | Probe 下推、等待、选择率与 Chunk 过滤 |
| MV | [`MvRewritePreprocessor.java`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/fe/fe-core/src/main/java/com/starrocks/sql/optimizer/MvRewritePreprocessor.java) | 候选准备、新鲜度、改写策略 |
| Connector | [`ConnectorMetadata.java`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/fe/fe-core/src/main/java/com/starrocks/connector/ConnectorMetadata.java) | Catalog SPI、分区与 Remote Files |
| Shared-Data | [`tablet_manager.h`](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/be/src/storage/lake/tablet_manager.h) | Metadata、Txn Log、Location、Cache、Compaction |

### 总结：StarRocks 的工程取舍

StarRocks 最值得学习的不是某一个 Benchmark 数字，而是它把 OLAP 系统的关键矛盾拆到不同层解决：CBO 负责全局搜索，Runtime Filter 和自适应数据结构吸收统计误差，PipelineDriver 管理节点内并发，主键索引用空间换实时更新，MV 用额外存储与刷新成本换透明加速，Shared-Data 再用版本化元数据和 Cache 换弹性。

这些机制也对应明确边界：CBO 依赖统计信息；Runtime Filter 无法替代 Join Reorder；Pipeline 降低调度开销但不能消除数据倾斜；主键表需要承担索引和 Compaction 成本；MV 必须同时保证等价性与新鲜度；对象存储提供弹性，却把本地 I/O 问题转化为元数据、请求次数和 Cache 问题。理解这些边界，比记住 Feature 名称更接近源码分析的真正价值。
