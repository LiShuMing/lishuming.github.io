---
title: "【草稿】Iceberg 规格笔记：V3 Deletion Vector 边界与 V4 进行中变更"
date: 2026-10-18T00:00:00+08:00
lastmod: 2026-10-20T00:00:00+08:00
slug: "iceberg-v3-dv-and-v4-wip"
categories:
  - 数据库
tags:
  - Iceberg
  - 湖仓
  - Spec
  - Deletion Vector
description: "草稿：V3 equality/position→DV 全景与并发边界，以及 V4 已合入/投票中/仅讨论三栏；FILE 规格投票已通过且 #17918 已合（仍非 adoption）；反例标 spec-only。未正式发表。"
draft: true
---

> **草稿边界**：本文从 cygnus `research/iceberg/`（2026-10-14 / 10-18）抽成博客向文稿，**不做新实验、不宣称 format v4 已采纳**。  
> 官方口径（社区公开材料）：**V1–V3 已采纳；V4 仍在开发、未 formal adopt**。  
> 已上线的精简版见 frontiers 附录 A.1 / A.2；DuckDB spill×Bloom（A.3）属执行线，本文不展开。  
> V3 全景原料：`01-v3-status.md`（cygnus / drafts 包内；已并入 §1.0）；并发专章仍见 cygnus `2026-10-14-v3-dv-row-level-concurrency.md`。

## 读完带走

1. **V2 用两类 delete file 做行级删**（position / equality）；**V3 把按位置删换成 DV**（每数据文件每快照至多一个），禁止新写 position delete，equality **仍可写**。V4 规格文字方向是禁止**新写** equality（≠ formal adopt）。
2. **V3 DV 让删除变便宜，也把冲突粒度钉在「单文件一个 DV」上**——不相交行的并发行级写，今天仍可能在文件/分区级失败；社区提案要把校验细化到行重叠（#18020 / #17754，**仍 open**）。
3. **V4 有两条已经「进主干」但含义不同的线**：（a）同分支快照时间戳必须严格大于父——**语义约束，已合 spec+实现**；（b）AMT 基线规格 PR 合进 main——**方便并行改规格，不是 format adoption**。
4. **FILE 类型**规格投票已通过且 #17918 已合（仍非 format adoption）；**DV 行级并发、REST/KMS、promotable types** 多数仍 open——适合进跟踪清单，不适合写成「湖仓已默认如此」。
5. **仍缺两块**：完整 V1→V2 读图、引擎实现对照。V3 equality/position→DV 全景 **已齐**（§1.0）。

## 案例 / 证据边界

| 项 | 本文做法 |
|----|----------|
| 笔记状态 | 原料均为 `needs-review`；文中保留该标记 |
| 投票 / merge | 只写笔记已核对的宣布与 PR 状态；**不臆测**未宣布结果 |
| 「已合入」 | 仅用于已 merge 的 spec/实现 PR，或 frontiers 已发表的同事实 |
| 「合仓 ≠ adoption」 | AMT #16025 合入 = 基线规格进仓；**不是** v4 formal adopt |
| 教学二级来源 | AMT 概念段整理自 Alex Merced 2026-08 文；文中已声明当时 v4 为提案/原型 |
| 反例 / SQL | 无新跑数；并发与 delete 语义反例以「应构造」列出，标 `spec-only` |
| 与 frontiers | A.1 / A.2 事实对齐；细节展开不抬会议同级 |

---

## 一、V3：删除语义（equality / position → DV）与行级并发

### 1.0 全景：三种删除形态与读写契约

> 压缩自 `01-v3-status.md`（cygnus / drafts 包内）。博客抽稿以本节为自洽底稿；细节与待核清单仍以主笔记为准。

#### 1.0.1 动机（V2 负担 → V3 DV）

V2 用 delete files 做行级删，不必重写整份数据文件：

| 类型 | 语义 | 典型成本 |
|------|------|----------|
| **Position delete** | `(file_path, pos)` | 写便宜；多文件时读端合并负担上升 |
| **Equality delete** | 列上等值谓词（可含 `IS NULL`） | 写可不先读旧行；读端谓词探测 + 序列号/分区规则耦合 |

方向性痛点（规格硬约束见下表；工程叙述见 [#11122](https://github.com/apache/iceberg/issues/11122)，已关 / 已进 Spec）：delete 文件粒度两难、内存与磁盘表示不一致、性能依赖 compaction。V3 回答：**按位置删走单文件位图（DV）**，并规定每数据文件每快照至多一个 DV、写时同步合并（[Delete Formats](https://iceberg.apache.org/spec/)、[Appendix E · Version 3](https://iceberg.apache.org/spec/)）。

#### 1.0.2 对照表

| | **Equality delete** | **Position delete** | **Deletion Vector (V3+)** |
|--|---------------------|---------------------|---------------------------|
| **标识** | `equality_ids` 列上等值（多列 = AND；null ≡ `IS NULL`） | `(file_path, pos)`，pos 从 0 | 单文件 bitmap：位 P 置位 ⇒ 行 P 已删 |
| **存哪** | Delete manifest · equality 文件 | Delete manifest · position 文件 | Delete manifest · DV 元数据 + Puffin blob |
| **每数据文件数量** | 可多份（按 seq 叠加） | 可多份 | **至多一个**（同快照） |
| **V2** | 可写 | 可写 | 无 |
| **V3** | **可写** | **禁止新写**；升级遗留有效 | **主路径**；新按位置删必须走 DV |
| **V4（规格文字）** | **禁止新写**；遗留须继续应用 | （V3 已禁新写） | 新 DV 与 data **同条目 colocate**（v4 manifest）；旧 delete-manifest DV 仍有效 |
| **读端序列号** | 数据 seq **严格小于** delete seq | 数据 seq **≤** delete seq（含同提交） | 同 position；且路径/分区匹配 |
| **分区** | 同分区分区相等，**或** delete 未分区（**全局**） | 分区相等；`referenced_data_file` 非空时还要路径匹配 | 同 position；`referenced_data_file` **必填** |

来源：[Row-level Deletes](https://iceberg.apache.org/spec/)、[Scan Planning](https://iceberg.apache.org/spec/)、[Delete Formats](https://iceberg.apache.org/spec/)、[Appendix E](https://iceberg.apache.org/spec/)。

#### 1.0.3 Writer / Reader 不变量（摘要）

**Writer**

- **Equality（V2/V3）**：`content = EQUALITY DELETES`，`equality_ids` 必填；删列后该列若曾是 delete 列，应用时仍须使用。
- **Position（仅 V2 新写；V3 遗留）**：按 `file_path` 再 `pos` 排序。V3 **不得再添加**新 position delete；对某文件更新按位置删时须写 DV，并把既有 position deletes **合并进该 DV**。
- **DV（V3+）**：快照内每数据文件 ≤ 1 个 DV；新删与旧 DV / 旧 position deletes **同步合并**。写出 DV 后，读者可忽略该文件上匹配的 position delete files。`referenced_data_file` 必填；offset/length 须与 Puffin footer 一致。

**Reader**（[Scan Planning](https://iceberg.apache.org/spec/) 摘要）

1. **DV**：路径 = `referenced_data_file`，数据 seq ≤ DV seq，分区相等 → 应用。
2. **Position delete**：同上（路径钉死可选），**且**该数据文件上没有必须应用的 DV。
3. **Equality**：数据 seq **<** delete seq，分区相等 **或** delete 未分区（全局）。
4. 特例：position/DV 允许 **同 seq**；equality **不允许**同 seq 作用到同 commit 数据。

#### 1.0.4 Equality-delete 正确性边界

| 边界 | 规格事实 | 引擎含义（教学级） |
|------|----------|-------------------|
| **作用窗口** | 只作用于 **更老** 的数据文件（seq 严格更小） | 同 commit 的 equality **不会**删同 commit 新行；与 position/DV 不同 |
| **全局 delete** | 未分区 equality → 对所有分区按 seq 规则应用 | 误写 unpartitioned equality = 全表谓词删 |
| **Null 语义** | delete 列上 null 匹配数据 null（`IS NULL`） | 与 SQL `=` 三值逻辑不同；测试须覆盖 null |
| **Schema 演进** | 已用作 delete 的列后来被 drop → **仍须参与**匹配；后加列作 delete 列 → 旧文件投影为 null | Reader 不能按当前 schema 丢掉旧 delete 列 |
| **Row lineage** | Equality 路径上的更新 **不保留** 原 `_row_id`（删旧 + 加新） | CDC 不能假设 equality upsert 保持 lineage |
| **V3 仍可写** | Equality 可在 v2 **与 v3** 表创建 | 「上了 V3/DV」≠「equality 已消失」 |
| **V4 禁新写** | Writers must not add equality delete to v4；升级不必重写旧 equality；读者必须继续应用遗留 | 读路径长期双轨；**V4 未 formal adopt** |

动机层（为何 V4 禁新写 equality 的 design 讨论）→ **待核**，勿写成规格定理。各引擎默认写 equality vs DV → **待核**（篇 3 实现对照）。

#### 1.0.5 最小反例（spec-only · 未本机跑表）

1. **Equality 同 seq 不删同 commit 行**：同一 commit 写 data `f1`（seq=10）与 equality `id=1`（seq=10）→ equality 要求 data seq 严格小于 10 → **不**删。对照：position/DV 且 seq=10 → **会**删。
2. **写出 DV 后 position delete 被遮蔽**：v2 遗留 position P 引用 `f1`；v3 writer 为 `f1` 写 DV（已合并 P）→ 读者对 `f1` **只应用 DV**。合并遗漏 → 静默少删（正确性 bug）。
3. **未分区 equality 变全局**：unpartitioned equality `id=1` → 对**所有分区**中更老数据文件应用；分区表上误用 = 跨分区删行。

#### 1.0.6 对引擎的含义（短）

1. **读路径**：V3 表仍可能同时存在 equality + 遗留 position + DV；测试矩阵按三列做，不是「只有 DV」。
2. **写路径**：V3 新按位置删 → 只写 DV 并合并；Equality 仍可用，但要接受 lineage 与同-seq 语义差异。
3. **升级**：v2→v3 不必立刻重写旧 position deletes；对某文件**第一次**写 DV 时必须吸收它们。
4. **并发**：单 DV/文件 ⇒ 文件级冲突 → §1.1–1.3。
5. **V4 前瞻**：新 writer 停写 equality；读旧 equality 路径要保留到清理完毕。勿承诺「升级即无 equality」。

**证据等级：`已采纳规格（V3）` · needs-review（对照 spec.md main 再扫）；V4 禁写 equality = 规格文字已写 · format 未 formal adopt**

---

### 1.1 并发问题（整理自 DV 并发笔记）

V3 表上，每个数据文件通常对应 **一个** deletion vector。两个并发行级写即使删除的是 **不相交行**，也可能在文件或分区粒度上失败：

- 核心路径 `validateAddedDVs`：同一数据文件上并发新增 DV → 失败；
- Spark `validateNoConflictingDeleteFiles`：UPDATE/MERGE 上按分区与列度量剪枝后仍残留的并发 delete → 失败，**不比较具体行位**。

开源 Spark 路径历史上更偏分区级锁；社区希望在 OSS 路径上也能把冲突粒度推到行（对照一些引擎在 row lineage + DV 上的做法时，注意：**实现归属引擎 vs 格式**）。

> 全景与 equality 边界见 §1.0；本节只谈冲突粒度。细节交叉引用 cygnus `2026-10-14-v3-dv-row-level-concurrency.md`。

### 1.2 提案要点（#18020，在 #17754 之上）

状态（笔记截 2026-10-14）：**#18020 open**（找设计反馈 / reviewer）；**#17754 open**（纯 DELETE 路径合并并发 DV；CI 绿，待第二位 committer）。前史 #14613 已 stale 关闭。

整理结论（非 Issue 全文）：

1. 把 `validateNoConflictingDeleteFiles` **细化到行**：仅当新删位置与本操作触及行 **重叠** 才冲突；可讨论 write-set（bitmap）与 read-set（谓词）两种精度。
2. 把 DV merge **扩展到 UPDATE/MERGE**：仅当并发新删与自身不相交时并集，否则失败；overwrite 产生的 DV 排除（与 #17754 一致）。
3. 配置挂到现有 `write.{delete,update,merge}.isolation-level`，不另开平行开关。
4. **非目标**：CoW 重写、ZORDER 跨文件重排等。

### 1.3 对引擎 / 教学的含义（并发）

- 讲清权衡：**DV 降低删除成本** ↔ **单 DV/文件把冲突钉在文件上**——适合作为并发课案例。
- 博客措辞：这是 **进行中设计**，不是「V3 已具备通用行级并发」。
- 待核：是否已有正式 `dev@` 帖、#17754 第二 committer、与 Spark isolation-level 的最终交互。

**证据等级：`open/讨论` · needs-review**

---

## 二、V4：相对 V3 的进行中变更（三栏）

官方公开材料口径：V4 **仍在开发**。下表只汇总 cygnus 已有四条；其它 V4 主线（如相对路径、data+DV 合并条目、禁写新 equality delete 等）**本包未另建专条笔记的，不编造展开行**——禁写 equality 的规范性语句已收进 §1.0.4，不在此重复抬级。

| 条目 | 已合入 | 投票中 / 已通过但非 adoption | 仅讨论 / 草案 |
|------|--------|------------------------------|----------------|
| **单调 snapshot 时间戳** | Spec #16294、Impl #16293 **merged**；同分支新快照 `timestamp-ms` 严格大于父；根不受；v1–v3 不变 | VOTE 已通过（笔记：9 binding +1 / 11 non-binding +1） | — |
| **`_last_updated_timestamp_ms`** | — | — | Spec #17636 **draft**；依赖元数据路径大致打通后 plumbing |
| **AMT 基线规格** | PR #16025 **MERGED**（2026-10-14；entry structures 等） | 合仓 VOTE：**18+1**（9 binding），**明确不是 format adoption** | 加密、列文件更新、去掉 partition tuple 等可后续迭代 |
| **AMT 机制本身（inline/flush）** | （基线文字进仓，≠ 全量实现门控） | — | 教学向推导见 Merced 文；内联剪枝、迁移、并发合并仍开放 |
| **FILE 类型** | Spec #17918 **MERGED**（2026-10-20 07:07 Asia/Shanghai；基线规格文字） | RESULT：**通过**（8 binding +1 / 19 non-binding +1 / 1 non-binding +0 / 0 −1）；**仍非 format adoption** | Issue #17919 挂 API / 引擎后续 |

### 2.1 单调快照时间戳（已合 · 与 frontiers A.1 对齐）

**问题**：v1–v3 **不保证**同分支子快照 `timestamp-ms` 严格大于父。时钟回拨、并发提交、或「用业务时间当 snapshot timestamp」会让 time travel 与快照链顺序不一致，增量/CDC/审计变得不可靠。

**约束（已进规格）**：

- 对 **v4 表**、**同一分支**：新快照时间戳必须 **严格大于** 父快照；
- 根快照不受约束；v1–v3 行为不变；升级前历史快照不回溯改写；
- 规格只陈述要求，不规定 writer 算法。

**参考实现（#16293）**：有父快照时 `max(clock.millis(), parent.timestampMillis() + 1)`（Lamport 式 fast-forward）。

**引擎含义**：

- 自研 writer 若乱填事件时间，v4 下可能直接 **commit 失败**；
- 旧表升级后 **新提交** 才强制单调；历史乱序窗口要写进 runbook；
- Part2 行级 `_last_updated_timestamp_ms` 仍草案——rewrite/expire 后的继承链验收，等规格落地再写。

**证据等级：Part1 已合入 · needs-review；Part2 草案**

> frontiers A.1 已写「约束已进主干」；本文补教学与运维边界，不抬级。

### 2.2 AMT：概念 + 基线合仓（合仓 ≠ adoption · 与 frontiers A.2 对齐）

**一句话**：今天一次小 append，常重写与表规模成正比的 manifest list / metadata.json；AMT 要把 **提交成本拉回与本次变更成正比**——根上 inline 近期变更，攒够再 flush 成带摘要的叶子。

口诀：**元数据树 = 索引**（摘要剪枝；inline 是有界的「未整理索引债」）。

**推导结构（教学摘要，二级来源）**：root 携带对叶子的引用 + inline 新条目 → 指针仍只指根（完整可达）→ flush 把 inline 打成带摘要 leaf。深度随表自适应。开放问题包括：inline 如何剪枝、条目带什么摘要字段、v3→v4 混跑、多 writer 的 inline 合并与 flush 交错。

**合仓投票（事实核对）**：

- 目标：把基线 V4 AMT 规格 PR #16025 **merge 进 main**，降低交叉 rebase 成本；
- 发起帖写明：**这不是格式采纳投票**；
- 结果（笔记 2026-10-15 核对）：**18 +1**（9 binding / 9 non-binding），无 0/-1；#16025 已 merge（commit `fd3a81b…`）。

**引擎含义（现在）**：

- 不要写死依赖 v4 API；
- 课程/博客可用「完整层级重写 vs 根 inline+flush」对照讲 commit 经济学；
- 运维上把 **inline 集合大小** 当作与 snapshot 速率同级的前瞻指标（flush 停滞 → 规划退向线性扫）。

**证据等级：合仓投票通过 + PR 已合入 · needs-review（未做实现级技术核验）；概念段为教学摘要**

### 2.3 FILE 类型（投票通过 + 规格 PR 已合 · 仍非 adoption）

**问题**：多模态对象用路径字符串或缺校验的巨大 binary，语义与扫描成本都不理想。提案在 v4 引入一等 `file` 类型（内联字节或外部对象/字节区间引用；值语义跟 Parquet FILE）。

**规格要点（整理自投票帖 / #17918）**：隐式子字段 `uri/offset/size/content_type/checksum/inline`；相对 URI 相对表 location；整值相等/排序/哈希未定义；不能当 identifier、分区或排序源；不能类型提升进出。

**状态（2026-10-20 核对，Asia/Shanghai）**：
- RESULT 邮件（Talat Uyarer，UTC 2026-10-19 21:44）：**通过** — 8 binding +1、19 non-binding +1、1 non-binding +0、0 −1。
- Spec PR [#17918](https://github.com/apache/iceberg/pull/17918)：**MERGED**（2026-10-19 23:07 UTC ≈ 次日 07:07 上海；merged_by danielcweeks）。
- 边界与 AMT 合仓同：**规格文字进仓 ≠ v4 format adoption**；引擎/API 仍看 [#17919](https://github.com/apache/iceberg/issues/17919)。

**证据等级：投票通过 + 规格 PR 已合入 · needs-review（未核最终 diff 措辞相对投票稿是否有改；未做引擎实现对照）**

---

## 三、附录：跟踪清单（不抬成主主张）

下列全部 **open / unverified**，只供雷达与 runbook，不进「读完带走」的硬结论。

### A. 读端必须接受 promotable types（#18404 open）

类型提升允许字段演进而不重写文件（如 `DECIMAL(8,0)` → `DECIMAL(25,0)`）。读者必须随时接受比当前 schema 更窄、且可提升到声明类型的物理类型——不能只依赖 metadata 里是否留下演进痕迹。

### B. REST `X-Iceberg-Access-Delegation` 无法满足时（#18480 open）

客户端要了委托机制、catalog 一个都给不出时，实践与讨论偏向 **fail-fast 400**（而非 200 空凭据）。#18480 为允许值增加 `none`，让客户端明示「不需要服务器委托」。Polaris 文档侧已有相关讨论闭环；Iceberg 规格 PR 仍 open。

### C. REST KMS credential vending（#17155 / #13225 open）

表加密需要短期、scoped 的 KMS 凭证与存储凭证一并 vend。规格 PR 截 10-14 **仍缺回复** → 笔记标 `claim_type: unverified`。**本文不当事实主张。**

---

## 四、显式缺口（下一轮补笔记，不补脑）

1. **无 V1→V2 完整读图**：快照、Manifest 树、delete files 的动机与契约——建议一页前缀，不单独发长文。  
2. ~~**无 V3「equality / position → DV」总览**~~ → 已并入 §1.0；主笔记 `01-v3-status.md`（cygnus / drafts 包内）。  
3. **无实现对照**：Iceberg Java/Py、Spark/Flink/Trino（及 cygnus 侧）各自落到哪一版语义。  
4. **V4 其它官方主线**未进本包专条笔记：勿从群聊口径直接写成表格新行。  
5. **FILE 投票结果、#17754 merge、#18404/#18480 等**：以后续宣布为准更新 `00-reading-map.md` 状态列。

---

## 五、建议成文拆分（发表侧）

| 对外篇 | 内容 | 依赖 |
|--------|------|------|
| 《V3 现状：删除语义与 DV 边界》 | §1.0 全景 + §1.1–1.3 并发 | 主笔记已齐；可抽 |
| 《V4 进行中：元数据树与时间语义》 | §2 三栏；文末链 frontiers A.1/A.2 | FILE 结果更新后改一格 |
| 附录页 | §3 跟踪清单 | 每周雷达改状态 |

知乎/专栏：先发「V3 现状」再发「V4 相对 V3 的 diff」；博客可留本合并草稿为完整对照，`draft: true` 直至缺口 1、3 缩小（或先拆两篇 draft）。

> 相关阅读：[论文雷达 · frontiers 附录 A.1 / A.2]({{< relref "2026-09-19-database-research-frontiers.md" >}})

## 原料索引

- `cygnus/research/iceberg/2026-10-18-v3-equality-position-dv-overview.md`（= 本目录 `01-v3-status.md`）
- `cygnus/research/iceberg/2026-10-14-v3-dv-row-level-concurrency.md`
- `…/2026-10-14-v4-adaptive-metadata-tree.md`
- `…/2026-10-14-v4-amt-baseline-vote.md`
- `…/2026-10-14-v4-monotonic-snapshot-timestamps.md`
- `…/2026-10-14-v4-file-type-vote.md`
- `…/2026-10-14-promotable-types-readers.md`
- `…/2026-10-14-rest-access-delegation-unsatisfied.md`
- `…/2026-10-14-rest-kms-credential-vending.md`
- 线上对照：https://lishuming.github.io/2026/09/19/database-research-frontiers-2026/ 附录 A.1–A.2
