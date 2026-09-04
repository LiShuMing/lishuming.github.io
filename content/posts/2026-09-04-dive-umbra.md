---
title: "【论文】Umbra 研究路线全景：从 SSD 数据库到编译器、优化器与云端 HTAP"
date: 2026-09-04T00:00:00+08:00
lastmod: 2026-09-04T00:00:00+08:00
slug: "dive-umbra"
categories:
  - 数据库
tags:
  - Umbra
  - HyPer
  - 查询编译
  - 查询优化器
  - HTAP
  - 云原生数据库
description: "沿 19 篇一手论文重建 Umbra 的研究谱系：它如何从 HyPer 的纯内存假设出发，以 SSD Buffer Manager 为系统底座，逐步补齐低延迟编译、事务、复杂 Join、云对象存储、流处理和库内机器学习，并在每次扩张中重新划定关系模型与编译器的边界。"
draft: false
---

如果只读 2020 年的系统论文，Umbra 很容易被概括成“加了 Buffer Manager 的 HyPer”；如果只看之后的单篇工作，它又像一个不断装入新功能的实验平台：今天做编译器，明天做 Join，后来又做流、数组、GPU 和云存储。这两种理解都低估了它。

Umbra 真正持续研究的是同一个问题：**在不放弃编译执行效率的前提下，一个现代数据库怎样摆脱“数据必须全部在内存、查询必须足够长、关系必须足够少、负载必须只是 SQL 分析”的理想化假设？**

沿着这个问题重读论文，会看到一条很清晰的演化路线：

```text
HyPer：内存常驻 + 编译执行
  │
  ├─ 数据可能大于内存 ───────→ Umbra Buffer Manager / Swip
  │                              └─ MVCC：把 OLTP 带回 SSD 架构
  │
  ├─ 编译可能比执行更慢 ─────→ Adaptive Execution
  │                              └─ Tidy Tuples / Umbra IR / Flying Start
  │                                  ├─ Dynamic Blocks：执行期适应
  │                                  └─ FireARM：迁移到 AArch64
  │
  ├─ 优化空间可能失控 ───────→ JOB 诊断 → Adaptive Large Join → LinDP++
  │                              └─ Indexed Algebra：降低代数优化复杂度
  │
  ├─ Join 可能在错误位置膨胀 → Unchained Hash Table
  │                              └─ Lookup / Expand / Diamond Hardened Join
  │
  ├─ 数据可能已经在对象存储 ─→ AnyBlob
  │                              └─ Colibri：热行存 + 冷列存的云端 HTAP
  │
  └─ 工作负载不再只是 SQL ───→ ArrayQL / Recursive SQL + GPU / LLVM AD
                                 ├─ Duck's Brain：反思关系物理表示
                                 └─ Relation-Based Streaming
```

这篇文章不是按发表时间逐篇摘录摘要，而是按系统问题组织 19 篇论文。每一节都会回答四件事：论文真正解决了什么、机制是什么、实验证明到哪里、它为什么自然地导向下一步。这样才能把 Umbra 看成一条研究路线，而不是论文标题的集合。

## 核心判断

先给出阅读全文后形成的五个判断。

1. **Umbra 的起点不是“SSD 很快”，而是让存储层在命中内存时接近不存在。** Variable-size page、Swip 和 versioned latch 都在压低 cached path 的固定税，同时保留 out-of-memory 的退路。
2. **查询编译不是一个开关，而是一条分层流水线。** 数据库负责关系语义和数据结构，Umbra IR 承接 imperative dataflow，Flying Start 负责尽快启动，LLVM 负责长期执行质量；Dynamic Blocks 再把运行时选择嵌回已编译代码。
3. **优化器问题被拆成了四层：估得准、搜得够、改得合法、维护得快。** Looking Glass、Adaptive Large Join、LinDP++、Indexed Algebra 分别击中了不同层次，不能互相替代。
4. **鲁棒性来自消除性能悬崖，而不只是优化最好情况。** 数据超内存、短查询、数千表 Join、n:m 倾斜、diamond 中间结果、对象存储延迟，都被当作必须平滑退化的常态。
5. **Relation 是强大的逻辑接口，但不是万能的物理表示。** ArrayQL 证明数组可以 relationalize，Duck's Brain 又证明“可以表达”不等于“应该用稀疏关系元组执行”。Umbra 后期越来越像一个保留领域语义的数据库编译与运行时平台。

## 一、先校准论文谱系：原始材料中的几处错位

在讨论技术之前，需要先把几个很容易传播的误读校正过来。

| 常见写法 | 论文中的真实含义 |
|---|---|
| `Making Compiling Query Engines Practical` 就是 Flying Start 论文 | 这是 2018 年 ICDE 工作扩展成的 2019 年 TKDE 论文，核心是 **LLVM IR 解释执行后再自适应编译**；Flying Start 属于 2021 年的 `Tidy Tuples and Flying Start` |
| `Indexed Algebra` 把物理索引提升为代数节点，并降低 Join 枚举复杂度 | 这里的“Indexed”是给**代数树上的路径查询与更新**建立动态索引，主要加速列集合分析，不是数据库 B-tree/index access path，也不负责 Join 枚举 |
| Robust Hash Table 是带 16-bit fingerprint 的 open addressing | 论文中的结构叫 **Unchained**，组合分区、adjacency array、流水化 probe、Bloom filter 和 software write-combining buffer |
| Diamond Hardened Join 靠计数器检测爆炸后在线换计划 | 核心是把二元 Join 分解成 **Lookup（收缩）与 Expand（扩张）**，分别重排，并用 `Expand3` 处理环；不是 in-flight re-optimization |
| Relation-Based Streaming 是 Differential Dataflow 的关系化实现 | 论文采用有限窗口的 ring-buffered relation，并尽量复用数据库执行器；它没有实现完整的分布式 checkpoint、watermark、exactly-once 和反压协议 |
| Colibri 以 S3 为唯一持久层，WAL 只在临时 NVMe | 论文明确区分 page server、log server 与 object storage；热页、日志和冷列文件承担不同持久化职责 |

还有一篇名为 `Instead of Rewriting Everything: A Case for Compiler-Driven Database Engines` 的所谓 CIDR 2023 论文，无法在 CIDR 论文集和作者发表列表中核验。本文不把它当作事实来源，而以真实发表的 [Bringing Compiling Databases to RISC Architectures](https://db.in.tum.de/people/sites/gruber/p791-gruber.pdf) 讨论 Umbra 编译架构的可移植性。

## 二、系统底座：从“数据必须在内存”到“冷热路径都可控”

### 2.1 Umbra 2020：让 Buffer Manager 的命中路径接近指针访问

[Umbra: A Disk-Based System with In-Memory Performance](https://cidrdb.org/cidr2020/papers/p29-neumann-cidr20.pdf)（CIDR 2020，Thomas Neumann、Michael Freitag）是系统路线的分水岭。HyPer 的很多设计默认数据常驻内存；Umbra 接受数据会超过 DRAM，却不愿退回传统 Buffer Pool 每次访问都查全局页表、加锁、pin/unpin 的路径。

论文的第一项关键设计是 **variable-size page**。页面从 64 KiB 起按 size class 指数增长，不同 size class 在进程虚拟地址空间中各自保留连续区域。小随机访问不必承担超大页的读放大，大对象和顺序数据也不必被强行切成大量固定小页。

第二项设计是 64-bit `Swip`（swizzled pointer）：

```text
cached page
┌───────────────────────────────────────────────────────────────┬─┐
│                       buffer-frame address                    │0│
└───────────────────────────────────────────────────────────────┴─┘

evicted page
┌─────────────────────────────────────────────┬────────────┬─────┐
│               57-bit page number            │ size class │  1  │
└─────────────────────────────────────────────┴────────────┴─────┘
                                                    6 bits   tag
```

最低位为 `0` 时它就是可直接解引用的内存地址；为 `1` 时携带 page number 与 size class，需要 Buffer Manager 载入并 swizzle。于是命中热页时没有中央哈希表查询。这个优化成立还有一个重要前提：持久页必须形成树，每个页面只有一个 owning Swip，Buffer Manager 才能安全地在父页中切换逻辑页号与物理指针。

并发控制使用 **versioned latch**：乐观读先读版本、访问页面、再检查版本是否变化；冲突或 I/O 路径再转向 shared/exclusive latch。它和 Swip 共同表达了 Umbra 的设计取向：常见热路径只做局部检查，稀有慢路径才支付协调成本。

关系本身组织为 B+-tree，叶页内部采用面向列的布局；短字符串（不超过 12 字节）可内联。系统还区分三类数据：

- persistent data 必须可恢复；
- transient data 属于数据库状态但不要求持久化；
- temporary data 仅服务某次查询。

这个区分很关键。Hash table、排序 runs、中间物化并不需要与用户表共享同一种恢复义务，统一交给 storage layer 管理也不等于统一持久化策略。

论文的实验边界同样值得保留。测试使用 64 GiB 内存和 Samsung 960 EVO，在 JOB 与 TPC-H 上比较缓存和冷数据路径。绕过 Buffer Manager 的平均收益不足 6%，说明热页管理税被压得很低；顺序冷扫描约 1.13 GiB/s，与 `mmap` 的约 1.15 GiB/s 接近。它证明的是**缓存工作集接近内存系统、缺页时可平滑使用 SSD**，不是“SSD 与 DRAM 等价”。

2020 版系统论文中的编译路径仍是自定义 IR 生成 bytecode，先解释、再视执行长度交给 LLVM。后来著名的 Flying Start 尚未出现在这里。把后续论文的实现倒灌回 2020 论文，会掩盖真正的演化。

### 2.2 Memory-Optimized MVCC：只解决分析还不算通用数据库

Buffer Manager 证明了分析负载可以越过内存边界，但 HTAP 还要求写事务、快照读和恢复。[Memory-Optimized Multi-Version Concurrency Control for Disk-Based Database Systems](https://www.vldb.org/pvldb/vol15/p2797-freitag.pdf)（PVLDB 2022）解决的是一个两难：

- 像 PostgreSQL 一样把历史版本放进持久页，会产生空间膨胀和写放大；
- 像纯内存 MVCC 一样用裸指针连接版本，页面一旦换出，指针就失去稳定锚点。

Umbra 的观察是，日常 OLTP 写事务通常很小，版本量相对现代服务器内存几乎可以忽略。于是数据库页只保留对象的最新状态，before-image 存在事务私有的内存 version buffer；每个出现版本对象的 resident page 配一张局部 mapping table，把稳定对象标识映射到内存版本链。访问热页时仍是局部映射，不引入全局版本索引。

当页面被换出时，版本数据并不随页持久化；恢复依赖原有 WAL，而非把可重建的历史副本写两遍。对于 bulk load 这类版本量可能超过内存的大写事务，论文另设轻量 fallback，在页上保留最少信息，使并发只读分析仍能隔离地进行。

实验报告事务吞吐相对传统磁盘系统最高达到一个数量级提升。准确的结论不是“MVCC 不需要持久化”，而是：**最新数据和 WAL 必须持久，绝大多数仅服务并发读的旧版本可以是可丢弃、可回收的内存状态。**这让 Umbra 的“memory-optimized disk-based”从 OLAP 扩展到了事务路径。

### 2.3 AnyBlob：先问对象存储本身到底慢在哪里

走向云端时，研究顺序很克制：先不设计完整云数据库，而是测量对象存储数据通路。[Exploiting Cloud Object Storage for High-Performance Analytics](https://www.vldb.org/pvldb/vol16/p2769-durner.pdf)（PVLDB 2023）把请求提交、TLS/网络栈、内存分配、响应解析和并发下载拆开分析，并实现 AnyBlob。

AnyBlob 使用 `io_uring` 和 CPU-efficient download manager，核心目标不是把单个 GET 变快，而是在足够并发下填满网络带宽，同时避免下载线程、复制和协议处理先吃光 CPU。论文在其云环境中表明：直接从对象存储读取、没有本地缓存的分析执行，可以接近依赖本地 SSD cache 的云数仓基线。

这个结论有明确边界：AnyBlob 是高性能 retrieval blueprint，不处理页面更新、日志、事务和恢复。它回答“冷数据能否直接扫”，没有回答“热写和冷分析怎样共存”。后者正是 Colibri 的问题。

### 2.4 Colibri：不是一份数据兼顾所有温度，而是让数据随温度变形

[Two Birds With One Stone: Designing a Hybrid Cloud Storage Engine for HTAP](https://www.vldb.org/pvldb/vol17/p3290-schmidt.pdf)（PVLDB 2024，系统名 Colibri）把 Umbra 的 B+-tree、页式更新和对象存储扫描接在一起：

```text
                         B+-tree key space
                               │
              ┌────────────────┴────────────────┐
              │                                 │
       hot / recently changed             cold / stable
       uncompressed PAX pages        immutable compressed column files
       point lookup + update          bandwidth-efficient async scan
              │                                 │
         page servers                       object storage
              └────────── log servers ──────────┘
```

新写入和频繁修改的数据留在未压缩 PAX 页中，获得行式点查和更新能力；冷却后的数据转成不可变、压缩、面向列的数据文件。若冷记录再次更新，系统把它解压并迁回热区，而不是在压缩文件上做细粒度原地修改。

提交协议也不是“对象存储是唯一真相”。压缩文件需要在相关事务提交前持久化，叶页中的引用变化进入日志；B+-tree、热页、log server 与 object store 各自承担职责。论文既给出 page/log server 的云部署，也讨论本地单机部署。测试环境中对象存储顺序带宽约 12 GB/s，低于本地 SSD 的 25～56 GB/s，因此它依靠压缩、预取和异步并发节省带宽，而不是假设网络和本地盘等速。

Colibri 是存储路线的阶段性闭环：Umbra 2020 让同一页在内存和 SSD 间切换，Colibri 2024 则让同一逻辑关系在**热行页与冷列文件**之间改变物理形态。前者统一地址空间，后者统一数据生命周期。

## 三、编译路线：从“先解释再编译”到分层 IR 与运行时适应

### 3.1 Making Compiling Query Engines Practical：先解决 JIT 的启动悬崖

[Making Compiling Query Engines Practical](https://db.in.tum.de/~leis/papers/adaptiveexecution.pdf)（TKDE 2019，源自 ICDE 2018 的 `Adaptive Execution of Compiled Queries`）讨论的是 HyPer/Umbra 编译路线的前史。问题非常实际：一个元数据查询可能执行不到 1 ms，LLVM 优化却需要几十毫秒；最大 TPC-DS 查询的编译甚至接近 1 秒。长查询靠编译获益，短查询却被 JIT 启动时间吞没。

论文没有维护“解释器算子”和“编译算子”两套语义，而是让同一份 LLVM IR 先由专门 bytecode interpreter 执行，同时跟踪代码路径进度；当预计剩余工作足够大时，再把相应路径切到 LLVM 机器码。切换粒度可以细到同一查询的不同代码路径。

这一步的价值是消除 `compile first, execute later` 的硬屏障，但解释阶段仍有解释开销，LLVM 后端仍然很重。它自然导向下一个问题：能否让第一份原生机器码本身就足够快地产生？

### 3.2 Tidy Tuples 与 Flying Start：把复杂度分层，而不是交给一个巨型后端

[Tidy Tuples and Flying Start: Fast Compilation and Fast Execution of Relational Queries in Umbra](https://link.springer.com/content/pdf/10.1007/s00778-020-00643-4.pdf)（VLDB Journal 2021）给出了 Umbra 编译内核的完整回答：

```text
physical query plan
        │
        ▼
Tidy Tuples：typed values / nullable values / tuple materialization
        │
        ▼
Umbra IR：SSA-like control flow + database-oriented data structures
        │
        ├─ Flying Start ─→ 很快产生 x86-64 机器码 ─→ 立即执行
        │
        └─ LLVM backend ─→ 更慢但更充分优化 ─────→ 长查询接管
```

`Tidy Tuples` 不是一个元组格式，而是一组分层代码生成抽象。上层表达 SQL 类型、NULL 语义、比较、哈希与物化，下层再落到 Umbra IR。它解决的是“编译器代码会不会把数据库语义散落成难以维护的字符串拼接”。

Umbra IR 则是为快速构造和遍历准备的图式中间表示。它保留 SSA、basic block、phi 等编译器概念，但数据结构和操作集合服务于数据库生成模式。这样，数据库可以在高层做 predicate、pipeline 与数据结构选择，把寄存器分配、指令选择留给后端。

Flying Start 是单遍、低延迟的原生代码生成器。论文的消融实验显示，寄存器分配是最有价值的快速优化之一，可使运行时间降低约 32%；更复杂的 linear-scan allocation 只再改善约 1% 执行时间，却增加约 14% 编译成本，因此没有成为默认选择。与优化 LLVM 相比，Flying Start 生成程序的中位数大约多执行 2.3 倍指令、消耗 1.6 倍 cycles，但 IPC 约高 1.4 倍，代码体积约 2.4 倍。它不是“和 LLVM 一样快”，而是在**编译时间和机器码质量之间选择了明确位置**。

论文还用代码规模反驳“编译执行必然难以工程化”：Tidy Tuples 约 2.2 万行、Umbra IR 约 3000 行、Flying Start 约 4200 行。行数不是复杂度证明，但分层至少让 SQL 语义、IR 与目标机器后端不必互相渗透。

后续 Umbra 把两条后端组合起来：Flying Start 让查询立刻跑，LLVM 在后台生成更优化的长跑代码，完成后切换。这和前一篇“bytecode 解释后再编译”目标相同，演化点是首层已经变成低延迟原生代码。

### 3.3 Dynamic Blocks：编译完成后，计划还可以改变吗

低延迟编译解决启动时间，却没有解决基数估计错误。传统 Adaptive Query Processing 可以在执行中换 Join 顺序或选择实现，但编译引擎若每次选择都重新生成和编译整段代码，适应成本可能比收益还大。

[Efficiently Compiling Dynamic Code for Adaptive Query Processing](https://db.in.tum.de/people/sites/schmidt/papers/dynamic-blocks.pdf)（ADMS 2022）提出 **Dynamic Blocks**：在生成机器码时，把有限候选的代码片段全部嵌入程序，并保留可改变的间接控制流。运行时根据真实测量选择片段或调整顺序，无需重新进入编译器。论文用自适应 selection 与 Join reorder 展示这种机制，在部分测试中获得超过 2 倍提升。

它的边界也很重要：Dynamic Blocks 只能在**编译前已经枚举并嵌入**的候选之间选择，不是执行中任意重建物理计划。它把“重新编译”变成“选择预编译路径”，用代码体积换适应延迟。

### 3.4 Bringing Compiling Databases to RISC：自研编译器是否会成为架构债务

自定义 x86 后端越快，一个新的问题越尖锐：换到 ARM 时，是否要重写全部指令选择和 ABI 适配？[Bringing Compiling Databases to RISC Architectures](https://db.in.tum.de/people/sites/gruber/p791-gruber.pdf)（PVLDB 2023）系统比较了标准语言/通用编译基础设施与数据库专用代码生成器，并为 AArch64 实现 FireARM。

论文得到的不是单向结论。通用编译设施降低移植门槛，对长查询能给出高质量代码；专用后端则能更严格地控制编译延迟，并更快支持新架构。FireARM 的意义在于验证 Umbra IR 的分层：上面的数据库语义和大部分优化可以保留，只替换目标后端。

因此 Umbra 的 compiler-driven 并不是“所有工作都交给 LLVM”，也不是“为每种 CPU 重写数据库”。更准确的边界是：

| 层次 | 应掌握的信息 | 适合的优化 |
|---|---|---|
| DB optimizer | 关系语义、基数、物理属性 | Join reorder、predicate pushdown、operator selection |
| Tidy Tuples / Umbra IR | NULL、布局、pipeline、数据结构 | materialization、控制流、domain-specific lowering |
| Flying Start / FireARM | 启动时延、目标 ISA | 快速指令选择、轻量寄存器分配 |
| LLVM | 完整 CFG 与机器模型 | CSE、DCE、强度削弱、深度寄存器与指令优化 |

## 四、优化器路线：估得准、搜得够、改得合法、维护得快

### 4.1 Looking Glass / JOB：先把“优化器差”拆成三个可验证问题

[Query Optimization Through the Looking Glass, and What We Found Running the Join Order Benchmark](https://db.in.tum.de/~leis/papers/lookingglass.pdf)（VLDB Journal 2018；早期版本是 PVLDB 2015 的 `How Good Are Query Optimizers, Really?`）使用 IMDb 的真实相关数据和 113 条多表 SQL，把优化器拆成 Cardinality Estimation、Cost Model 和 Plan Enumeration 分别做消融。

最有价值的实验不是比较数据库总分，而是把真实中间结果基数注入优化器。结果表明，多表和相关谓词下的估计误差会随 Join 层数迅速放大，并且更常见的是低估；在内存型执行中，用所有中间结果基数之和构成的简单 `C_mm`，已经能与复杂 PostgreSQL cost model 竞争。此时如果输入基数错了几个数量级，再精细的 CPU/I/O 系数也只是在精确计算错误前提。

但论文没有说枚举器不重要。给定相同估计，较完整的搜索仍通常优于过早启发式；丰富索引还会放大 access path 与 Join order 的组合空间。JOB 真正留下的方法论是：

```text
plan regression
   ├─ estimate error?   → 注入真实 cardinality
   ├─ cost error?       → 固定 cardinality，替换 cost model
   └─ search error?     → 固定前两者，扩大 enumerator
```

它诊断了“为什么会选错”，但 exhaustive search 本身会爆炸。于是下一篇把优化器的运行时间也纳入目标函数。

### 4.2 Adaptive Optimization of Very Large Join Queries：查询图难度比表数更重要

[Adaptive Optimization of Very Large Join Queries](https://db.in.tum.de/~radke/papers/hugejoins.pdf)（SIGMOD 2018）反对一个生硬阈值：少于 N 张表用 exact DP，多于 N 张表突然切 greedy。链状 50 表可能很容易，稠密 15 表却可能产生巨大搜索空间；真正的难度更接近连接图中 connected subgraph 的数量，也就是 DP 状态规模。

论文因此先估算搜索难度，再在不同层级选择策略：

- 小而简单的图运行 exact `DPHyp`；
- 中等图先得到一个线性顺序，再只对这条顺序上的区间做 DP；
- 极大图进一步结合 greedy、linearized DP 或 iterative DP，让搜索预算平滑下降。

线性化以后，候选子计划主要是连续区间，搜索从任意子集收缩成受限空间，却仍能通过区间 DP 修复初始顺序。论文展示了超过 4000 个关系的查询仍可优化，而常见查询继续保留 exact search。

更深的思想是：**优化器也是有截止时间的 anytime algorithm。**目标不是孤立地最小化执行代价，而是权衡：

```text
user-visible latency = optimization time + execution time
```

它的边界是计划质量仍依赖 CE 和 cost model，线性化也可能丢掉有价值的图拓扑。2018 论文主要处理 inner join；真实 SQL 的 outer/semi/anti join 还引入语义合法性约束。

### 4.3 LinDP++：搜索空间缩小后，不能把不等价的计划放进去

[LinDP++: Generalizing Linearized DP to Crossproducts and Non-Inner Joins](https://btw.informatik.uni-rostock.de/index.php/de/tagungsbaende/send/3-tagungsbaende/tagungsband.pdf)（BTW 2019，最佳论文）把 linearized DP 推向工业 SQL。

Inner join 可以在满足谓词的连接图上广泛交换、结合；left outer、semi、anti join 的 preserved side、NULL 扩展和 predicate dependency 会限制可重排区域。LinDP++ 在生成线性顺序和区间子问题时保留这些约束，从而先回答“等价吗”，再回答“便宜吗”。

论文还讨论了一个反直觉情况：cross product 不总是坏计划。若两个很小关系的笛卡尔积能够绕过一连串昂贵 Join，它可能降低总中间结果。穷举任意 cross product 会让搜索空间失控，因此 LinDP++ 只检查长度为 2 的相邻边路径，并在估计满足

```text
|u × w| < |u ⋈ v|  and  |u × w| < |v ⋈ w|
```

时保守加入人工 cross-product edge。论文示例中，最优代价从 1.84M 降到 0.94M，而不必开放所有笛卡尔积。作者也明确警告：如果基数把 10,000 错估成 1，cross product 会灾难性放大，因此只应在精确基数或可靠上界下采用。

LinDP++ 的贡献不是让任意 Join 都能自由重排，而是把 **search scalability** 与 **semantic legality** 放进同一枚举器。它与 Looking Glass 共同说明：估计不确定时，能做的变换本身也应更保守。

### 4.4 Indexed Algebra：当规则越来越多，分析一棵计划树也会成为瓶颈

[Asymptotically Better Query Optimization Using Indexed Algebra](https://www.vldb.org/pvldb/vol16/p3018-fent.pdf)（PVLDB 2023）解决的是另一类复杂度。许多优化都要反复问：某列在哪里产生、沿路径在哪里被使用、两个算子的最低公共祖先是谁、移动算子后哪些列集合改变。若每个算子都存整棵子树所需列集合，并在每次改写后重新向上传播，链状计划会产生二次工作量。

论文把代数树维护成 dynamic forest，用 link/cut tree 支持 `link`、`cut`、path query 和 path update。列的 producer/consumer 信息不再复制进沿途每个节点，而通过树路径聚合获得。这样，处理整棵 n 节点计划的相关分析从最坏 `O(n²)` 降为 `O(n log n)`。

这与数据库物理索引没有关系。它是在**给优化器自己的代数数据结构建索引**。实验中，即使 TPC-H/TPC-DS 这类并不极端的计划，总优化时间也可改善超过 1.8 倍。

四篇论文连起来，优化器路线就完整了：

| 问题 | 论文 | 回答 |
|---|---|---|
| 输入事实是否可信 | Looking Glass / JOB | 真实相关数据让 CE 成为主要误差源 |
| 候选是否搜得完 | Adaptive Large Join | 根据图难度平滑压缩枚举空间 |
| 改写是否语义等价 | LinDP++ | 在 non-inner join 约束下枚举，保守考虑 cross product |
| 大计划上的分析是否可维护 | Indexed Algebra | 用动态树索引降低列传播与路径查询复杂度 |

## 五、Join 路线：把数据结构鲁棒性与代数鲁棒性分开

### 5.1 Unchained：n:m 倾斜不能让 1:n 快路径付出高税

[Simple, Efficient and Robust Hash Tables for Join Processing](https://db.in.tum.de/~birler/papers/hashtable.pdf)（DaMoN 2024）从一个生产矛盾出发：open addressing 在主外键 `1:n` Join 上简单而快速，但 build side 有重复键或严重倾斜时，probe chain 和冲突会恶化；chaining 对 `n:m` 更稳，却增加指针追逐和分配成本。

论文提出 **Unchained**，并不是 tagged open addressing。其关键组合是：

1. build side 先分区，使相近哈希值连续落入局部区域；
2. 用 adjacency array 表示同一 bucket/键的匹配项，避免传统链表随机指针；
3. probe 侧流水化多个未决查找，利用 memory-level parallelism 隐藏访存延迟；
4. 用 Bloom filter 提前排除不命中；
5. 用 software write-combining buffer 合并分区写入。

设计目标不是某一数据分布下的峰值，而是让同一个结构覆盖 `1:n` 与带 skew 的 `n:m`。论文报告相对 relational open addressing 平均约 2 倍提升，在图工作负载上最高约 20 倍。数字不能外推为所有 Hash Join 的固定加速；它说明的是，把重复键表示、内存局部性和 probe 调度一起设计，比只调 load factor 更鲁棒。

### 5.2 Diamond Hardened Join：问题不只在 Hash Table，而在 Expand 出现得太早

[Robust Join Processing with Diamond Hardened Joins](https://www.vldb.org/pvldb/vol17/p3215-birler.pdf)（PVLDB 2024）处理另一种性能悬崖。所谓 diamond，不限于图形长得像菱形，而是多个分支共享键并再次汇聚时，传统二元 Join 顺序可能先产生远大于输入与最终结果的中间结果。

论文把 Join 语义拆成两种作用：

- `Lookup`：只判断或定位是否存在匹配，通常收缩候选；
- `Expand`：真正枚举一对多/多对多匹配，可能放大数据。

传统 binary Join 把二者绑在一起，优化器一旦过早选中高 fan-out 的分支，就必须把巨大中间结果送给后续过滤。Diamond Hardened Join 先重排 Lookup，尽量完成过滤、semi-join reduction、sideways information passing 和可提前的聚合，再安排 Expand；对于三角/环结构，引入 `Expand3` 同时检查第三条边，避免二元展开后才发现大部分组合无效。

论文在 CE 工作负载上最高达到约 500 倍提升，同时在 TPC-H/JOB 上只有很小回退。这不是一个“运行时发现爆炸后换计划”的故事，而是**在物理代数里把收缩和扩张显式化，让优化器有机会把危险动作推迟**。它也不是完全替代 worst-case optimal join，而是在现有 binary Join 编译执行框架中吸收多路 Join 的关键优势。

这两篇论文构成两个互补层次：Unchained 防止单次 Hash Join 因键分布崩坏；Lookup/Expand 防止整个 Join 拓扑因中间结果顺序崩坏。一个修数据结构，一个修代数表达能力。

## 六、工作负载扩张：Relation 能表达多少，物理层又应该保留什么

### 6.1 ArrayQL：先证明“数组语义可以进入关系优化器”

[ArrayQL Integration into Code-Generating Database Systems](https://db.in.tum.de/~schuele/data/arrayql.pdf)（EDBT 2022）没有给 Umbra 再塞一套独立数组执行器。它给出完整 ArrayQL grammar，并把 n 维坐标与 m 个值表示成 `n + m` 列的关系；维度列组成主键，bounding box 与 validity map 保存数组范围和有效区域。

九类数组操作在 semantic analysis 阶段 lowering 为关系代数。以矩阵乘法为例：对共享维度做 Join，乘积做 Projection，再按输出坐标 GroupBy/SUM。后续 optimizer、MVCC、codegen 都不需要知道前端来自 ArrayQL。接口既可独立暴露，也可嵌入 SQL/UDF。

实验显示过滤和聚合能够很好地复用关系执行器，但 index shift、反转等操作需要物化 table function，成本明显。论文证明的是 **logical reuse 可行**，没有证明 HashJoin + HashAggregate 是 dense GEMM 的最佳 physical implementation。

### 6.2 Recursive SQL + GPU：把训练拆成可编译的数据库阶段

[Recursive SQL and GPU-support for In-Database Machine Learning](https://link.springer.com/article/10.1007/s10619-022-07417-7)（Distributed and Parallel Databases 2022）进一步把 preprocessing、training 和 validation 放进一个查询。

recursive table 表达参数迭代：上一轮参数经过 loss 与梯度更新产生下一轮。论文的 derivation operator 接收 SQL lambda，在 code generation 阶段做 reverse-mode automatic differentiation，并缓存公共子表达式。gradient-descent operator 是 pipeline breaker：它物化 batch、管理迭代状态，并把适合的数值计算生成 NVPTX 交给 GPU。

```text
SQL preprocessing
       │
       ▼
recursive relation ── derivation operator ──→ gradient expression
       │
       ▼
gradient-descent pipeline breaker
       ├─ CPU relational pipelines
       └─ batched NVPTX kernels on GPU
```

论文覆盖多个 learner，并展示端到端性能可与外部框架竞争、GPU 路径最有优势。但 batch size 同时影响硬件吞吐和统计收敛，不能只最大化 GPU 利用率；结论也不能外推到 Transformer、Tensor Core、混合精度或分布式训练。它的研究价值是证明 DB compiler 能跨越“关系预处理—迭代状态—设备代码”三层，而不是宣称 SQL 已替代 PyTorch。

### 6.3 LLVM AD：通用编译器可以消掉多少领域生成代码

[LLVM Code Optimisation for Automatic Differentiation](https://db.in.tum.de/~schuele/data/forward.pdf)（DEEM 2022）只用 4 页回答一个很窄却重要的问题：理论上对多参数、单输出 loss 更合适的 reverse mode，与生成更多重复表达式的 forward mode，经过 LLVM 后差距还剩多少？

实验表明，在把别名信息明确为 `noalias` 后，LLVM 能识别并消除大量公共计算；测试模型上 forward/reverse 最终 PTX 和运行时间接近，主要差异转移到编译时间，且输入多于输出时 reverse mode 仍更有优势。测试里的 `fast-math` 没有产生额外收益。

这不等于“forward 与 reverse AD 普遍等价”。它只说明：当计算图小、静态且完全暴露给编译器时，算法层生成的冗余不能直接等同于最终机器码成本。数据库应在高层决定导数语义和数据布局，再让 LLVM 做 CSE/DCE；不要在 DB optimizer 中重写通用编译器已经擅长的工作。

### 6.4 The Duck's Brain：能用 Relation 表达，不代表应该用 Relation 存

[The Duck's Brain: Training and Inference of Neural Networks in Modern Database Engines](https://arxiv.org/pdf/2312.17355)（arXiv 2023，后发表于 Datenbank-Spektrum 2024）是前述路线难得的自我反思。作者分别用 SQL-92 的 relational coordinate representation，以及 Umbra 的 array datatype，在 Umbra/DuckDB 中实现神经网络训练和推理，并与 NumPy 比较。

关系表示当然能算，但代价很具体：坐标、Join key、GroupBy state 和 recursive CTE 物化会把密集矩阵拆成大量宽元组。论文展示的 Iris 设置中，每轮 relational 路径约使用 10/20 MiB，而 array 路径约 1/1.6 MiB；MNIST 的关系路径在部分设置中增长到约 25 GiB，数组路径低于 5 MiB。Umbra 在部分实验中是唯一完成全部数据库方案的系统，但 NumPy 仍更快；Umbra array inference 也仍约比 NumPy 慢一个数量级。

因此它给出了整条 workload-extension 路线最重要的否定性结论：

> **Logical unification 不等于 physical unification。**

SQL/Relation 可以作为组合和治理接口，但 dense tensor 需要保留 shape、stride、GEMM、fusion 与 device placement 等领域语义。把一切过早 lowering 成普通 tuple，会让后续编译器再也看不见这些机会。

### 6.5 Relation-Based Streaming：统一的是“可查询状态”，不是完整流系统协议

[Relation-Based In-Database Stream Processing](https://ceur-ws.org/Vol-3462/CDMS7.pdf)（CDMS @ VLDB 2023）从另一个方向扩张 Relation。传统架构把实时数据送入专用流引擎，把历史数据留在数据库；临时的 stream-table join 需要复制历史状态或跨系统访问。

论文把有限时间窗口实现成 ring-buffered relation：新数据追加，过期分区复用；从 optimizer 看，它仍是一张可扫描、Join、Aggregate 的关系，因此历史表与最近事件可以走同一个编译执行器。变化主要集中在 relation 的生命周期和扫描入口，而不是另建一套 streaming operator graph。论文用分析型流 workload 与 Spark/Flink 做比较，证明复用高性能 DBMS 内核是可行路线。

但它没有实现工业分布式流系统的完整控制面：checkpoint、exactly-once、watermark、late event、backpressure、弹性扩缩都不在证明范围。它更接近“数据库中一张持续滚动、支持 ad-hoc query 的近期关系”，而不是 Flink 的等价替代。

五篇论文连起来能看到一次观点收缩：

```text
ArrayQL：      新领域语义可以 lowering 到 Relation
Recursive ML：Relation + Compiler 可以穿过迭代与 GPU 边界
LLVM AD：      一部分低层冗余应交给通用编译器
Duck's Brain： Relation 不应垄断 dense numerical physical representation
Streaming：    Relation 适合统一可查询状态，但不自动提供分布式流协议
```

Umbra 的核心因此不是朴素的 “Everything is a Relation”，而更接近：**Everything may enter a common optimizer/compiler/runtime, but not everything should share one physical algebra.**

## 七、把 19 篇论文还原成一条核心演化路径

按年份重排后，每篇论文在路线中的位置如下。这里的“继承关系”不是引用次数，而是它从上一阶段接过的未解问题。

| 年份 | 论文 | 解决的瓶颈 | 留给下一步的问题 |
|---|---|---|---|
| 2018 | Looking Glass / JOB | 分离 CE、Cost、Enumeration 的责任 | 搜索本身如何扩展 |
| 2018 | Adaptive Very Large Join | 依据查询图难度平滑压缩搜索空间 | non-inner join 的合法性 |
| 2019 | LinDP++ | cross product 与 non-inner join 下的受限枚举 | 复杂计划分析的维护成本 |
| 2019 | Making Compiling Query Engines Practical | 同一 LLVM IR 先解释后编译，降低短查询延迟 | 能否直接快速产生原生码 |
| 2020 | Umbra | 用可变页、Swip、latch 跨越 DRAM/SSD | 事务与更低编译延迟 |
| 2021 | Tidy Tuples and Flying Start | 分层 codegen、Umbra IR、快速原生后端 | 运行时如何适应错误估计；如何移植 ISA |
| 2022 | Memory-Optimized MVCC | 内存版本链 + 页局部映射覆盖 OLTP | 远端云存储的数据布局 |
| 2022 | ArrayQL | 数组语义复用关系编译器 | 关系表示是否适合数值计算 |
| 2022 | Recursive SQL + GPU | 迭代、AD 与 GPU 纳入查询流水线 | 生成代码冗余和张量表示 |
| 2022 | LLVM AD | 划分 AD 与通用 LLVM 优化职责 | 真实大模型/大数组仍需专门物理算子 |
| 2022 | Dynamic Blocks | 预编译候选在运行时切换 | 候选范围仍受编译时枚举限制 |
| 2023 | Indexed Algebra | 动态树索引把代数分析降到 `O(n log n)` | 更丰富规则如何与运行时反馈结合 |
| 2023 | AnyBlob | 高并发、低 CPU 的对象存储读取 | 热写、日志、冷列扫如何统一 |
| 2023 | RISC / FireARM | 验证 Umbra IR 与目标后端分层 | 多 ISA 后端的长期维护成本 |
| 2023 | Relation-Based Streaming | 复用关系执行器查询近期流与历史表 | 完整流语义与增量维护 |
| 2023/24 | The Duck's Brain | 定量暴露 relational ML 的内存和物化代价 | 需要 heterogeneous physical algebra |
| 2024 | Unchained Hash Table | 同一 Join 结构覆盖 1:n 与倾斜 n:m | 跨多个 Join 的中间结果爆炸 |
| 2024 | Diamond Hardened Join | Lookup/Expand 分离，推迟放大 | 与 CE feedback/WCOJ 的更深结合 |
| 2024 | Colibri | 热 PAX 页与冷压缩列文件组成云 HTAP | 多节点写入、分层缓存与成本治理 |

从这张表可以抽象出三次核心演化。

### 第一次：从“所有数据在内存”到“常见路径在内存”

HyPer 的强假设是常驻；Umbra 的强假设改成**工作集有局部性**。页可以在 SSD，对象可以在远端，历史版本可以只在内存，热数据可以是 PAX、冷数据可以是压缩列文件。系统不再统一介质，而是统一地址、生命周期和访问接口。

### 第二次：从“编译查询”到“编译一个可适应的执行系统”

早期问题是 LLVM 启动慢，于是先解释后编译；Flying Start 把第一阶段也变成机器码；Dynamic Blocks 让机器码内含运行时选择；FireARM 证明同一个中层 IR 可以落到另一种 ISA。编译器不再只是把 plan 翻译成 code，而是承载了**分层优化、启动策略、运行时切换和硬件映射**。

### 第三次：从“Relation 是共同语言”到“共同语言必须保留物理差异”

Array、ML 与 Stream 都能进入关系前端，这是复用 optimizer/runtime 的巨大收益；Duck's Brain 又用实验说明，过早抹平 tensor/array 结构会付出难以挽回的内存和计算代价。后期路线不是退回多套烟囱，而是趋向：

```text
SQL / Stream / Array / ML
             │
             ▼
domain-aware logical IR
             │
             ▼
shared optimization framework
      ┌──────┼────────┐
      ▼      ▼        ▼
relational  array    streaming
physical    tensor   state/lifecycle
algebra     algebra  operators
      └──────┼────────┘
             ▼
       CPU / GPU / remote storage
```

共同的是编译框架、代价决策和状态管理；不应强行共同的是所有物理算子与数据布局。

## 八、从数据库工程视角看 Umbra：真正值得借鉴的不是某个技巧

### 8.1 先优化失败曲线，再优化峰值

Umbra 系列论文反复选择“悬崖”作为研究对象：

- 数据超过 DRAM，纯内存系统停止工作；
- 查询很短，LLVM 编译比执行更久；
- Join 图稍大，exact DP 突然超时；
- build side 倾斜，Hash table probe 突然退化；
- diamond 过早 expand，中间结果突然爆炸；
- 数据进入对象存储，线程和 CPU 开销先于带宽成为瓶颈。

每次给出的方案都不是保证所有情况最快，而是让系统从 fast path 进入 slow path 时**连续退化**。这是比 benchmark 峰值更可迁移的设计原则。

### 8.2 “零开销抽象”不是没有抽象，而是把检查放到局部

Swip、page-local version map、Tidy Tuples、Dynamic Blocks、link/cut tree、Lookup/Expand 看似来自不同领域，结构却相似：都拒绝每次操作访问全局中心状态，而把足够的信息放到指针 tag、页面、IR 节点或局部代码块旁边。

局部化并不等于无成本，它把成本变成可预测、可缓存、可在慢路径回收的形式。阅读 Umbra 时应关注的不是“是否有一层抽象”，而是：**这层抽象的判定发生在每个 tuple、每个 page、每个 pipeline，还是只在状态转换时发生？**

### 8.3 Optimizer 与 Runtime 不应互相替罪

Looking Glass 说明 CE 错误可能压倒 cost model；LinDP 说明再准的估计也救不了超时的枚举；LinDP++ 说明便宜计划必须先语义合法；Dynamic Blocks 和 Diamond Join 则说明某些不确定性应交给执行层吸收。

一个更健康的职责分工是：

- optimizer 用统计和预算找出高质量候选；
- algebra 暴露 Lookup/Expand、Array/Tensor 等真正影响代价的语义；
- runtime 用 measured behavior 在预留边界内适应；
- storage 保证工作集变化时不会把整个系统拖入不同数量级。

“把 CE 做到完美”与“运行时全部自适应”都是不现实的单点答案。Umbra 路线的价值恰恰在于同时推进四层。

## 九、仍未闭合的研究问题

Umbra 已经画出一张很完整的单机现代数据库蓝图，但论文边界也留下了清楚的空白。

1. **反馈如何跨查询持久化？** Dynamic Blocks 解决单次执行内有限候选的切换，尚未形成从 runtime measurement 回流 CE、cost 与 plan cache 的完整闭环。
2. **异构物理代数如何共享 Memo？** ArrayQL 与 Duck's Brain 已证明需要保留 array/tensor 表示，但 relational、tensor、stream operator 如何在同一搜索空间比较 cost 与 property，仍是开放问题。
3. **云端写扩展如何演化？** Colibri 重点是冷热布局与读写共存，不等于跨地域 multi-writer、serverless elasticity 和租户隔离已经解决。
4. **复杂 Join 的鲁棒性如何组合？** Unchained、Lookup/Expand、Dynamic Blocks、运行时 filter 与 WCOJ 各自处理不同风险，真正的 optimizer 需要知道何时组合它们，而不只是多注册几个算子。
5. **流与表的统一是否应进入增量代数？** ring relation 解决近期数据可查询，完整系统仍需要 changelog、watermark、checkpoint、增量视图维护与历史一致性。

## 结语

Umbra 最容易被记住的是 Swip、Flying Start 或某一张性能图，但这些只是沿途的器件。更重要的研究方法是：每当系统跨过一个边界，就重新检查原先被默认的东西。

- 从内存跨到 SSD，检查 Buffer Manager 是否必须有中央页表；
- 从长查询跨到短查询，检查 JIT 是否必须先暂停再执行；
- 从普通 Join 跨到千表和 diamond，检查枚举与二元代数是否足够；
- 从本地盘跨到对象存储，检查瓶颈究竟是延迟、带宽还是 CPU；
- 从 SQL 跨到数组、ML 和流，检查 Relation 是逻辑接口还是唯一物理世界。

因此，Umbra 的核心演化不是从“A 数据库”变成“功能更多的 A 数据库”，而是从一个高速编译型引擎，逐步变成一套围绕**介质无关、分层编译、预算化优化与异构物理表示**构建的系统研究框架。

## 参考论文

### 系统与存储

- [Umbra: A Disk-Based System with In-Memory Performance（CIDR 2020）](https://cidrdb.org/cidr2020/papers/p29-neumann-cidr20.pdf)
- [Memory-Optimized Multi-Version Concurrency Control for Disk-Based Database Systems（PVLDB 2022）](https://www.vldb.org/pvldb/vol15/p2797-freitag.pdf)
- [Exploiting Cloud Object Storage for High-Performance Analytics / AnyBlob（PVLDB 2023）](https://www.vldb.org/pvldb/vol16/p2769-durner.pdf)
- [Two Birds With One Stone: Designing a Hybrid Cloud Storage Engine for HTAP / Colibri（PVLDB 2024）](https://www.vldb.org/pvldb/vol17/p3290-schmidt.pdf)

### 查询编译

- [Making Compiling Query Engines Practical（TKDE 2019）](https://db.in.tum.de/~leis/papers/adaptiveexecution.pdf)
- [Tidy Tuples and Flying Start: Fast Compilation and Fast Execution of Relational Queries in Umbra（VLDBJ 2021）](https://link.springer.com/content/pdf/10.1007/s00778-020-00643-4.pdf)
- [Efficiently Compiling Dynamic Code for Adaptive Query Processing（ADMS 2022）](https://db.in.tum.de/people/sites/schmidt/papers/dynamic-blocks.pdf)
- [Bringing Compiling Databases to RISC Architectures（PVLDB 2023）](https://db.in.tum.de/people/sites/gruber/p791-gruber.pdf)

### 查询优化与 Join

- [Query Optimization Through the Looking Glass, and What We Found Running the Join Order Benchmark（VLDBJ 2018）](https://db.in.tum.de/~leis/papers/lookingglass.pdf)
- [Adaptive Optimization of Very Large Join Queries（SIGMOD 2018）](https://db.in.tum.de/~radke/papers/hugejoins.pdf)
- [LinDP++: Generalizing Linearized DP to Crossproducts and Non-Inner Joins（BTW 2019）](https://btw.informatik.uni-rostock.de/index.php/de/tagungsbaende/send/3-tagungsbaende/tagungsband.pdf)
- [Asymptotically Better Query Optimization Using Indexed Algebra（PVLDB 2023）](https://www.vldb.org/pvldb/vol16/p3018-fent.pdf)
- [Simple, Efficient and Robust Hash Tables for Join Processing（DaMoN 2024）](https://db.in.tum.de/~birler/papers/hashtable.pdf)
- [Robust Join Processing with Diamond Hardened Joins（PVLDB 2024）](https://www.vldb.org/pvldb/vol17/p3215-birler.pdf)

### 数组、机器学习与流

- [ArrayQL Integration into Code-Generating Database Systems（EDBT 2022）](https://db.in.tum.de/~schuele/data/arrayql.pdf)
- [Recursive SQL and GPU-support for In-Database Machine Learning（Distributed and Parallel Databases 2022）](https://link.springer.com/article/10.1007/s10619-022-07417-7)
- [LLVM Code Optimisation for Automatic Differentiation（DEEM 2022）](https://db.in.tum.de/~schuele/data/forward.pdf)
- [Relation-Based In-Database Stream Processing（CDMS @ VLDB 2023）](https://ceur-ws.org/Vol-3462/CDMS7.pdf)
- [The Duck's Brain: Training and Inference of Neural Networks in Modern Database Engines（arXiv 2023 / Datenbank-Spektrum 2024）](https://arxiv.org/pdf/2312.17355)
