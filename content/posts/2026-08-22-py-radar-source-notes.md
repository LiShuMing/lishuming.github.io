---
title: "【设计】Liminalis Radar：一个可降级的数据库信息雷达"
date: 2026-08-22T00:00:00+08:00
lastmod: 2026-08-23T21:00:00+08:00
categories:
  - Database Engineering
tags:
  - Python
  - FastAPI
  - PostgreSQL
  - React
  - LLM
  - Data Pipeline
  - Personal Knowledge Management
description: "只基于当前 Liminalis 实现，记录 Radar 的启动刷新、稳定身份、非破坏性写入、分层读取、后台摄取与前端消费设计。"
draft: false
toc: true
math: false
---

我现在对 Radar 的定位很明确：它不是一个独立新闻爬虫，也不是让 LLM 自由搜索互联网的 agent，而是 Liminalis 中负责管理数据库信息注意力的一个 domain。

它需要持续回答四个问题：

1. 最近有哪些值得关注的数据库新闻？
2. 同一个链接以不同形式出现时，是否仍能识别为同一条记录？
3. PostgreSQL、Redis 或外部 feed 不可用时，页面能否继续工作？
4. 自动采集与人工提交的高价值链接，是否应该走同一条处理链路？

当前实现给出的答案不是“所有事情都交给 LLM”，而是把确定性采集、数据身份、serving fallback 和 AI enrichment 分开。这也是我认为这套设计最重要的地方。

## 1. 当前架构

Radar 已经是 Liminalis FastAPI 后端中的一个完整业务域。它对外只有一个页面，但内部有三条不同的数据流。

```text
                              Liminalis FastAPI lifespan
                                         │
                                         ▼
                                startup refresh task
                                         │
          feeds.json -> Fetch -> Extract -> recent filter -> Normalize -> Rank
                                         │
                                         ▼
                                  canonical URL dedupe
                                         │
                          ┌──────────────┴──────────────┐
                          ▼                             ▼
             local latest.json artifact        PostgreSQL radar_items
                  always written                  when configured


React /radar -> GET /api/radar/items
                          │
             ┌────────────┴────────────┐
             ▼                         ▼
       PostgreSQL query       latest artifact + bundled snapshot
       primary read path              fallback read path


Admin drawer -> POST /api/admin/radar/links -> arq job
                                                │
                    URL safety -> Fetch -> Extract -> LLM analyze -> PG upsert
```

三条链路的职责不同：

- **启动刷新**追求低成本、确定性和快速完成，只处理配置的 feed，不调用 LLM。
- **查询链路**追求可用性，在业务数据库不可用或未配置时仍能返回数据。
- **管理员摄取**处理人工挑选的链接，允许使用 LLM 做标题、类型、摘要和标签增强。

核心代码边界如下：

| 职责 | 当前实现 |
| --- | --- |
| 应用生命周期 | `liminalis/backend/app.py` |
| 启动刷新编排 | `liminalis/backend/radar/refresh.py` |
| feed 配置 | `liminalis/backend/radar/data/feeds.json` |
| 抓取与抽取 | `fetcher.py`、`extractor.py` |
| 归一化与排序 | `normalize.py`、`ranker.py` |
| URL identity | `identity.py`、`backend/_shared/urls.py` |
| PostgreSQL model 与读写 | `db_models.py`、`radar/service.py` |
| 读取策略与降级 | `backend/services/radar_service.py` |
| HTTP API | `backend/routers/radar.py`、`backend/routers/admin.py` |
| 单链接后台摄取 | `backend/radar/tasks.py` |
| 页面 | `liminalis/src/pages/RadarPage.jsx` |

## 2. 启动刷新：新鲜度不能阻塞服务可用性

FastAPI 的 `lifespan()` 在应用启动时调用 `schedule_startup_radar_refresh()`。这里没有直接等待网络请求，而是创建名为 `radar-startup-refresh` 的异步任务，API 可以先完成启动。

默认参数是：

| 配置 | 默认值 | 含义 |
| --- | ---: | --- |
| `RADAR_REFRESH_ON_STARTUP` | `true` | 是否在服务启动后刷新 |
| `RADAR_REFRESH_STARTUP_DELAY` | `2` 秒 | 等 API 启动稳定后再抓取 |
| `RADAR_REFRESH_TIMEOUT` | `180` 秒 | 整次刷新上限 |
| `RADAR_REFRESH_MIN_INTERVAL_MINUTES` | `15` 分钟 | artifact 足够新时跳过重复刷新 |
| `RADAR_DAYS` | `7` 天 | 最近内容窗口 |
| `RADAR_MAX_ITEMS` | `80` | 单次最多保留的候选数 |

刷新任务有一组明确状态：

```text
idle -> scheduled -> running -> completed
         │             │
         ├-> throttled ├-> failed
         ├-> disabled  └-> cancelled
```

状态通过 `GET /health` 的 `radarRefresh` 返回。对我来说，这比只在日志里打印“开始抓取”更重要：页面是否有数据和后台是否成功更新，是两个不同问题，运维时需要分别观察。

### 2.1 当前 feed pipeline

启动刷新读取打包在代码中的 `feeds.json`。当前配置覆盖 PostgreSQL、ClickHouse、CockroachDB、Percona、ScyllaDB、PlanetScale 和 Supabase 七个来源。

实际处理顺序是：

```text
parse feeds
  -> Fetcher.fetch_feeds(use_cache=False)
  -> Extractor.extract_all
  -> filter recent extracted items
  -> Normalizer.normalize
  -> rank_items
  -> canonical URL deduplicate
  -> write artifact
  -> optional PostgreSQL bulk upsert
```

`Fetcher` 和 parser 都是同步实现，因此刷新使用 `asyncio.to_thread()` 把整段 CPU/网络工作移出事件循环。它不会把 FastAPI 的请求处理线程卡住，外层再由 `asyncio.wait_for()` 提供总超时。

只要至少一个 feed 成功，刷新就继续生成结果；只有所有 feed 都失败时才把任务标记为 `failed`。这符合信息聚合的容错模型：单一信息源失败不应该让整个 Radar 不可用。

### 2.2 为什么先过滤日期，再做相似标题去重

`Normalizer` 会用 `SequenceMatcher` 比较标题，相似标题去重最坏是 `O(n^2)`。某些 RSS 会返回多年历史，如果先对完整 feed 做相似度计算，再过滤最近七天，启动任务可能长时间占用 CPU。

当前实现把时间窗口提前到了 normalize 之前：

- 有发布日期的条目只保留最近 `RADAR_DAYS` 天；
- 没有日期的条目要求 `confidence > 0.7`；
- 无日期条目每个 product 最多保留 3 条。

这不是单纯的性能优化，而是在数据进入昂贵算子之前建立 cardinality bound。它和数据库执行计划中的 predicate pushdown 是同一种思路：过滤越早，后面的状态空间越小。

### 2.3 artifact 是可用性边界

每次成功抓取都会先写：

```text
~/.liminalis/artifacts/radar/latest.json
```

写入使用临时文件加原子 replace，避免 API 读到半个 JSON。artifact 保存展示所需字段，不保存 `rawContent`，因此它是 serving projection，不是原始语料库。

即使配置了 PostgreSQL，这个 artifact 也会更新。数据库是长期业务事实存储，artifact 则是本机快速恢复和降级读取的边界，两者没有必要互相替代。

## 3. Identity：先确定“同一条记录”，再谈智能处理

Radar 的唯一身份来自 canonical URL，而不是抓取时看到的原始字符串。

`normalize_url()` 当前执行这些规则：

- scheme 和 hostname 转小写；
- 去掉 hostname 的 `www.`；
- 去掉 HTTP/HTTPS 默认端口；
- 移除 fragment；
- 删除 `utm_*`、`fbclid`、`gclid`、`ref`、`source` 等追踪参数；
- 对剩余 query 参数稳定排序；
- 空 path 归一为 `/`。

之后，`radar_item_id()` 对 canonical URL 做 SHA-256，并取前 16 位作为稳定 ID。

```text
raw URL
  -> canonicalize_radar_url
  -> SHA-256[:16]
  -> RadarItem.id
```

这条规则同时用于启动刷新、管理员单链接摄取、PostgreSQL upsert 和静态数据合并。identity 只有成为全链路 contract 才有意义；如果每个模块各自 hash 原始 URL，任何去重都只在局部有效。

`deduplicate_radar_items()` 不只是删掉重复项，还会保留信息更丰富的版本：

- 标题、产品、类型优先补齐空字段；
- `summary` 和 `rawContent` 选择更长的值；
- `tags` 与 `sources` 做稳定集合合并。

PostgreSQL 的 upsert 使用同一语义。重复抓取可以更新事实字段，但空摘要不会覆盖已有摘要，短正文也不会覆盖长正文。这条“非破坏性 enrichment”约束，比选择哪一种摘要模型更基础。

## 4. 存储与读取：业务事实和页面可用性分开

当前读取策略由 `backend/services/radar_service.py` 集中管理。

### 4.1 PostgreSQL 已配置

`radar_items` 是主数据表，包含 canonical URL、标题、发布日期、product、content type、summary、tags、sources、raw content 和 sync batch。

查询支持：

- page/per_page 分页，单页最多 100 条；
- product 和 content type 过滤；
- PostgreSQL `to_tsvector` + `plainto_tsquery` 全文检索；
- 按 sync batch、发布日期、抓取时间倒序；
- product/type facet 聚合；
- 最新同步批次。

当数据库没有数据，或者发生允许降级的读取错误时，service 会转到本地 fallback。

### 4.2 PostgreSQL 未配置或读取降级

fallback 由两层数据合并而成：

```text
latest.json               本次运行环境抓到的最新窗口
    +
pyRadarFeed.js            随前端发布的稳定内置快照
    │
    ▼
canonical URL dedupe
    │
    ▼
recompute total/facets -> paginate/filter/search
```

合并时最新 artifact 放在前面，内置快照负责补齐历史窗口。合并后重新按 canonical URL 去重，并重新计算 `totalItems`、product facets 和 content type facets。因此页面上的 “All sources” 表示当前实际可浏览的唯一记录数，而不是某个数据文件曾经处理过的行数。

fallback 模式使用内存 substring search，PostgreSQL 模式使用全文检索；两者的召回语义并不完全相同，但 API response contract 保持一致。

这种设计选择的优先级是：

```text
read availability > refresh freshness > enrichment completeness
```

外部 RSS 失败，不影响已有内容；PostgreSQL 未配置，不影响页面打开；LLM 不可用，不影响自动 feed 刷新。

## 5. 自动刷新与人工摄取是两条不同成本的路径

启动刷新只使用确定性规则，summary 是正文清理后的前 1200 个字符。它的目标是快速获得“现在有什么新内容”，不是生成最终研究结论。

当我在 Radar 管理抽屉中提交一个链接时，才进入更重的处理链路：

```text
signed admin session
  -> create RadarIngestionJob
  -> enqueue arq ingest_link_task
  -> validate_public_url
  -> duplicate check
  -> fetch + extract
  -> canonical duplicate check
  -> LLM analyze
  -> PostgreSQL upsert
  -> completed / duplicate / failed
```

这里有几个关键设计：

1. 管理接口返回 `202 Accepted`，抓取和 LLM 不占用 HTTP 请求生命周期。
2. 页面每 1.6 秒查询 job 状态，完成后刷新 feed。
3. URL 在抓取前解析 DNS，并拒绝 localhost、loopback、private、link-local 和 reserved 地址，避免后台抓取成为 SSRF 入口。
4. 抓取前后各做一次重复检查。第一次处理提交 URL，第二次处理 redirect 或抽取出的 canonical URL。
5. `RadarIngestionJob` 与 `RadarItem` 分表，失败信息和处理状态不会污染内容记录。

我认可这种成本分层：机器自动发现的候选内容走便宜、可重放的流水线；人工明确表达兴趣的链接才获得 LLM token、完整正文和任务状态。模型预算由人的注意力触发，而不是由 feed 数量触发。

## 6. 前端只消费统一的 serving contract

`RadarPage.jsx` 不关心数据最终来自 PostgreSQL、artifact 还是内置快照。它始终请求：

```http
GET /api/radar/items?page=1&per_page=80&type=all&product=all&q=
```

API 返回：

```text
items
page / per_page
total_items / total_pages
has_prev / has_next
products
contentTypes
latestSyncBatch
```

页面负责搜索输入、类型和产品筛选、列表展示、管理员登录、链接提交与 job polling。请求失败时，React 自己还能直接回退到打包的 `pyRadarFeed.js`。

因此当前实际上有两层 read fallback：后端优先在服务端返回 artifact + snapshot；如果整个 API 都不可访问，前端再使用 bundled snapshot。这会带来少量重复逻辑，但换来的是 Radar 页面在本地开发、无数据库运行和后端故障时都能展示基本内容。

## 7. 一次当前环境的真实验证

2026-08-23，我在本机以未配置 PostgreSQL 的模式启动 Liminalis，并强制执行一次启动刷新。

结果如下：

| 检查项 | 结果 |
| --- | ---: |
| 配置 feed | 7 |
| 成功 feed | 6 |
| 刷新耗时 | 8.4 秒 |
| 最近 7 天新增 item | 13 |
| 新增内容日期范围 | 2026-08-17 至 2026-08-21 |
| 内置快照唯一 item | 160 |
| 合并后 `total_items` | 173 |
| 持久化模式 | `local-artifact` |

`GET /health` 返回：

```json
{
  "radarRefresh": {
    "state": "completed",
    "feeds": 7,
    "successfulFeeds": 6,
    "items": 13,
    "persisted": 0,
    "storage": "local-artifact"
  }
}
```

验证同时覆盖了 50 个 Radar identity、refresh、crawler、extractor、settings 与架构测试；Ruff、前端 Vite build 和 `git diff --check` 均通过。

这次实验最重要的不是多抓了 13 条新闻，而是验证了整个降级闭环：服务先启动、后台抓取完成、artifact 原子落盘、API 合并去重、页面总数从 160 更新为 173，期间不需要 PostgreSQL。

## 8. 当前设计的边界

这套实现已经能稳定支撑个人使用，但边界需要说清楚。

### 8.1 刷新调度是进程内的

刷新状态保存在 Python 进程内，artifact freshness 也只根据本机文件 mtime 判断。在当前单 API 进程部署中足够简单；如果改成多个 Uvicorn worker 或多实例，每个实例都可能尝试调度启动任务，15 分钟 throttle 不能替代跨进程锁。

### 8.2 只有启动刷新，没有周期调度

当前语义是“服务启动时获取一次最新内容”，不是常驻 scheduler。长期不重启的实例不会自动每小时刷新。如果产品目标变成持续雷达，应把同一个 `refresh_latest_radar_news()` 交给 arq cron 或外部 scheduler，而不是在 lifespan 里增加永久循环。

### 8.3 artifact 是 serving projection，不是审计日志

`latest.json` 只保留最近窗口，而且不包含 raw content。它不能回答一条记录经历过哪些版本，也不能用于完整重放。需要审计时，应依赖 PostgreSQL 中的业务记录，进一步的原文版本化则需要单独的数据模型。

### 8.4 自动摘要不是 LLM 摘要

启动刷新中的 summary 是最长 1200 字符的正文投影，速度快、失败面小，但不保证观点提炼。只有管理员提交路径才执行 LLM analysis。UI 不应把两者当成完全同质的内容质量。

### 8.5 两种搜索实现存在语义差异

PostgreSQL 使用 full-text search，fallback 使用小写 substring match。同一个 query 在两种模式下可能得到不同结果。API shape 一致不等于行为完全一致，测试需要分别覆盖两种运行模式。

## 9. 我对这套设计的理解

当前 Radar 最值得保留的不是某个 crawler 或 prompt，而是三个架构判断。

第一，**identity 先于 intelligence**。只有 canonical URL 和非破坏性 upsert 稳定，摘要、标签和 ranking 才能安全迭代。

第二，**读可用性和写新鲜度解耦**。启动抓取失败不应该让页面失败；数据库不可用也不应该让已有知识消失。artifact 与内置 snapshot 让失败变成“内容稍旧”，而不是“系统不可用”。

第三，**让成本跟随意图**。自动 feed 刷新使用确定性流水线，人工提交才触发 LLM。它既控制成本，也让系统行为更可解释。

从数据库工程角度看，Radar 更像一个很小的增量物化系统：外部 feed 是变化源，canonical URL 是 key，`radar_items` 是当前事实投影，`latest.json` 和内置 snapshot 是 serving materialization，React 页面是最终消费端。

我希望它持续保持这种克制。Radar 的价值不在于“替我读完互联网”，而在于以可恢复、可解释、可去重的方式，把有限注意力集中到真正值得继续阅读和实验的数据库变化上。
