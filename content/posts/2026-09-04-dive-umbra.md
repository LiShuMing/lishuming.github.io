---
title: "【论文】Umbra 研究路线全景：从 SSD 数据库到编译器、优化器与云端 HTAP"
date: 2026-09-04T00:00:00+08:00
lastmod: 2026-10-07T00:00:00+08:00
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
description: "从 HyPer 纯内存与全量 LLVM 编译撞上的两堵物理高墙出发，串联慕尼黑工业大学 19 篇核心论文，拆解 Umbra 如何用指针混写、German Strings、Flying Start 单遍编译、解关联大连接优化、菱形硬化连接与冷热变形存储一步步重塑现代数据库内核。"
draft: false
toc: true
math: true
---

2010 年代初，慕尼黑工业大学（TUM）的 HyPer 系统曾用一套极度纯粹的组合技震撼了数据库界：**假设全部数据常驻 DRAM，彻底砍掉传统缓冲池（Buffer Pool）与火山模型迭代器，用 LLVM 把整条 SQL 流水线直接编译成紧凑的原生机器码。** 在内存放得下的分析基准测试上，这套架构快得几乎摸到了单机硬件的天花板。

可一旦走出实验室、把这套纯粹设计推向真实的生产负载，它迎头就会撞上两堵结结实实的物理高墙：

1. **DRAM 容量与 NVMe 税之墙**：现代企业级 NVMe SSD 阵列已经能跑出每秒数千万次 IOPS 和几十 GB 的吞吐，单字节成本却只有 DRAM 的零头。一旦数据集涨破内存上限，系统就必须重新管起磁盘页；但如果你在编译执行引擎底下原样塞回一个传统数据库缓冲池——每次访问页面都要去全局哈希表里查页号、加锁、增减引用计数（`pin/unpin`），再按死板的 8 KiB 小页切碎大列块——CPU 光是在缓冲池记账上烧掉的指令周期，就足以把 JIT 编译省下来的算力红利吃得干干净净。
2. **LLVM 编译启动延迟之墙**：LLVM 全量优化后端（`-O3`）在跑十秒钟的大表扫描时威风八面，但它做一遍指令选择、循环展开和寄存器分配本身就要花掉几十毫秒，碰上几百行的复杂 TPC-DS 查询甚至逼近 1 秒。而线上大量点查、元数据查询或短事务真正的执行时间还不到 1 毫秒——“磨刀一秒钟，砍柴半毫秒”，编译本身反倒成了最大的响应延迟瓶颈。

顺着这两堵墙继续往下深挖，还会连带拔出另外三串连锁难题：当 TUM 招牌的通用子查询解关联（Subquery Unnesting）把层层嵌套的相关子查询统统拍平为连接图后，优化器动辄面对几十上百张表、夹杂外连接与半连接的搜索爆炸，以及多对多菱形连接（Diamond Join）引发的中间结果雪崩；当存储底座进一步延伸向云端对象存储（S3），客户端 CPU 往往在喂饱网卡带宽前就先被协议栈榨干；而当用户试图把数组、流处理乃至神经网络训练一并塞进 SQL 引擎时，“万物皆元组”的物理表示又会遭遇严酷的内存拷问。

把 TUM 团队在 2018 至 2024 年间发表的 **19 篇一手论文**拼回同一张工程蓝图，你会发现 **Umbra** 从来不是一堆零散顶会技巧的拼盘，而是围绕上述矛盾一环扣一环长出来的完整演化树：

```text
HyPer 起点：全量常驻 DRAM + 全量 LLVM 编译执行
  │
  ├─ 撞上内存容量墙 ────→ Umbra 缓冲池（可变长页 + Swip 指针混写 + German Strings）
  │                        ├─ Memory-Optimized MVCC：页局部内存版本链补齐磁盘态 OLTP
  │                        └─ AnyBlob → Colibri：打满对象存储带宽，热 PAX 页与冷列文件随温度变形
  │
  ├─ 撞上编译延迟墙 ────→ Adaptive Execution（先解释 LLVM IR 再按需切 JIT）
  │                        └─ Tidy Tuples + Umbra IR + Flying Start（毫秒级单遍原生码 + 后台 LLVM）
  │                             ├─ Dynamic Blocks：预埋多路机器码分支实现免重编自适应
  │                             └─ FireARM：验证分层 IR 向 AArch64 的低成本移植
  │
  ├─ 撞上连接搜索与膨胀 ─→ JOB 归因诊断 → Adaptive Very Large Join → LinDP++ 语义守门
  │                        ├─ Indexed Algebra：用动态树索引把计划树列分析压回 O(n log n)
  │                        └─ Unchained 哈希表 + Diamond Hardened Join（拆开 Lookup 与 Expand）
  │
  └─ 试探关系模型外延 ──→ ArrayQL / Recursive SQL + GPU / LLVM 自动微分优化
                           ├─ Relation-Based Streaming：环形缓冲关系复用编译执行器
                           └─ The Duck's Brain：用实测内存鸿沟证伪“物理表示大一统”
```

下面我们就顺着这五条主干脉络，看清每一项核心机制究竟被什么物理约束逼出来、靠什么数据结构落地，又在哪道边界上把接力棒交给了下一篇工作。

---

## 一、存储与事务底座：在 NVMe 与云对象存储上保住内存级热路径

想要支持超出内存的数据规模，同时守住接近纯内存系统的吞吐，核心铁律只有一条：**缓存命中（Cache Hit）这条占 99% 比例的快路径上，绝不允许出现任何全局哈希查表和重量级锁竞争。**

### 1.1 可变长页、Swip 指针混写与 German Strings（CIDR 2020）

作为整条路线的奠基之作，[《Umbra: A Disk-Based System with In-Memory Performance》](https://cidrdb.org/cidr2020/papers/p29-neumann-cidr20.pdf)（CIDR 2020）借鉴并扩展了 `vmcache` 虚拟内存缓冲思想，从三个维度重塑了存储底座。

第一刀砍向了延续几十年的**固定大小磁盘页**。传统 8 KiB 定长页在分析列存和大字符串面前左右为难：页太小，扫大列时 I/O 请求碎成一地且大对象被迫拆成跨页链表；页太大，随机点查改一行却要连坐读写几兆字节。Umbra 直接引入**按指数分档的可变长页（Variable-Size Pages）**，从 `64 KiB` 起步按 `2^k` 倍增，并在操作系统 64 位虚拟地址空间里为每一个尺寸档位（Size Class）预先划出独立的连续地址段——点查走小页免读放大，大列块与大对象直接住进大页保顺序吞吐。

第二刀是用 64 位**混写指针 `Swip`（Swizzled Pointer）**彻底消灭热页访问的全局查表税。由于内存页按字节对齐后最低位天然为 `0`，热页驻留内存时，`Swip` 直接存放 64 位缓冲帧虚拟内存地址（最低位 `tag = 0`）；冷页尚在磁盘时，最低位标为 `tag = 1`，高 57 位编码逻辑页号、中间 6 位记录尺寸档位。

CPU 沿着 B+-Tree 往下走时只需一条位测试指令：看到末位是 `0`，直接当原生 C++ 指针解引用冲过去，连缓冲池管家的面都不用见；只有看到末位是 `1`，才慢速陷入 Buffer Manager，将对应尺寸档位的页面从 SSD 读入预先 `mmap` 好的虚拟地址段，并把父节点里的 `Swip` 原地改写成 `tag = 0` 的虚拟内存指针（即 Pointer Swizzling）。等到内存吃紧需要替换冷却页面时，系统再通过原子 `CAS` 把父页 `Swip` 解混写（Unswizzle）回 `tag = 1`，最后调用 `madvise(MADV_DONTNEED)` 归还物理内存帧而保留虚拟地址空间：

![Umbra vmcache 可变长虚拟内存缓冲池与 Swip 指针混写生命周期](/images/diagrams/exec-umbra-vmcache-swip.svg)

注意这个精巧闭环成立的拓扑前提：**所有持久页必须构成一棵严格的单亲树（每个子页有且仅有一个父页持有它的 Owning Swip）**——否则当冷页被驱逐换出内存时，系统根本不知道该去哪里把散落在外的裸指针安全改回磁盘页号。配合**乐观版本闩锁（Versioned Latch）**（先读版本号、无锁读内容、末尾校验版本未变，遇到并发写干扰走乐观重试，仅在写操作或缺页 I/O 时升级锁）以及持久数据、瞬态状态、查询临时页的三级持久化职责剥离，Umbra 在 64 GiB 内存配 Samsung 960 EVO SSD 的实测中，把热页缓冲管理税压到了 **6% 以内**，冷数据顺序扫描跑出 `1.13 GiB/s`，几乎贴着操作系统裸 `mmap`（`1.15 GiB/s`）的极限飞驰。

第三处后来被 DuckDB、Velox、Polars 与 Apache Arrow `StringView` 竞相效仿的经典设计，是叶页列存里的 **16 字节字符串描述符（俗称 German Strings）**：当字符串长度 `≤ 12` 字节时，采用 `4B 字节长度 + 12B 完整内容` 全量内联在槽位里；当长度 `> 12` 字节时，槽位改存 `4B 字节长度 + 4B 首部前缀 + 8B 堆/数据块指针`。试想在做字符串等值探测或 `ORDER BY` 比较时，绝大多数业务编码、国家缩写、状态枚举压根不超过 12 字节，直接在 16 字节定长槽位里就能用寄存器一次比完；哪怕碰上几百字节的 URL 长文本，只要前 4 个字节的前缀（Prefix）对不上，CPU 当场就能判定不相等或比出大小，连一次跨页追逐外部指针的随机访存都不用付！

### 1.2 面向磁盘换出的内存态 MVCC（PVLDB 2022）

搞定了分析扫描，要做真正的 HTAP 还得过高频 OLTP 事务这一关。[《Memory-Optimized Multi-Version Concurrency Control for Disk-Based Database Systems》](https://www.vldb.org/pvldb/vol15/p2797-freitag.pdf)（PVLDB 2022）直面了一个经典两难：如果学 PostgreSQL 把旧版本直接追加进持久化数据页，不仅页面迅速膨胀还会引发严重的磁盘写放大；可如果学纯内存 HyPer 用裸内存指针串起 Undo 版本链，一旦数据页被换出到 SSD 再读回来，物理地址全变了，版本链当场断成野指针。

Umbra 抓住了一个关键的负载事实：**日常在线写事务同一时刻活跃的未提交/历史版本总量其实非常小，现代服务器内存完全装得下。** 于是它定下规矩：持久化数据页上永远只原地存放最新状态，修改前的旧镜像（Before-Image）留在事务私有的内存版本缓冲区里；只要某个常驻内存页上改出了多版本对象，就在该页旁挂一张轻量的**页局部映射表（Page-Local Mapping Table）**，用页内稳定的逻辑槽位号桥接到内存版本链。一旦页面冷却被驱逐落盘，无需把仅服务于并发快照读的历史旧版本写进数据文件（崩溃恢复本就有预写日志 WAL 兜底；遇到超大批量导入版本爆内存时，再退回页上极简标记位保证并发只读隔离）。靠着把持久恢复与并发读隔离拆开记账，其事务吞吐相比传统页内多版本磁盘引擎拉开了最高一个数量级的身位。

### 1.3 从 AnyBlob 到 Colibri：喂饱云端带宽与数据随温度变形（PVLDB 2023–2024）

当战场从本地 NVMe 转向云端对象存储（如 AWS S3），很多人直觉以为瓶颈全在“云存储延迟高”。[《Exploiting Cloud Object Storage for High-Performance Analytics》](https://www.vldb.org/pvldb/vol16/p2769-durner.pdf)（PVLDB 2023，系统名 **AnyBlob**）先把整条云端下载通路拆骨剖肉量了一遍，揪出了真凶：**在开足并发拉取分块后，网卡带宽还没满，数据库节点的客户端 CPU 就先被 TLS 加解密、HTTP 响应解析、内核态拷贝和线程切换吃光了！** AnyBlob 基于 `io_uring` 与低开销异步下载器重构了数据获取层，在无本地磁盘缓存的云主机上直接扫对象存储，跑出了逼近带本地 SSD 缓存云数仓的吞吐。

但 AnyBlob 只管高吞吐冷读，不管在线点查更新与事务日志。紧接着 PVLDB 2024 的 **[Colibri](https://www.vldb.org/pvldb/vol17/p3290-schmidt.pdf)**（*Two Birds With One Stone: Designing a Hybrid Cloud Storage Engine for HTAP*）把最后一块拼图合拢——既然没有任何一种单一物理格式能同时讨好高频原地更新与压缩列扫，那就**让同一张表的数据顺着生命周期温度自动变形**，并与前文的 16 字节 `German Strings` 前缀快筛协同挡掉绝大部分跨层随机解引用：

![German Strings 16B 寄存器快筛与 Colibri 冷热变形 HTAP 存储](/images/diagrams/exec-umbra-colibri-strings.svg)

新写入与热修改落在由 Page Server 托管的未压缩 PAX 页里并挂接内存版本链；数据冷却后后台紧缩打包成不可变的高压缩 Mini-Page 列存块沉入廉价对象存储（在论文测试的 `12 GB/s` 对象存储带宽下靠高压缩率与异步预取弥补与 `25–56 GB/s` 本地盘的差距），并在相关事务提交前确保持久化、将叶页引用变更记入 Log Server；万一某条冷数据突然又被业务更新，系统就把它解压迁回热页区，绝不在压缩列文件上硬做昂贵的原地修补。从 2020 年让页面跨越内存与 SSD，到 2024 年让物理布局跨越行式热页与云端冷列，Umbra 的存储底座走完了一个漂亮的闭环。

---

## 二、查询编译演进：跨过 LLVM 的启动延迟悬崖

按下存储墙之后，回头看开头提到的第二堵墙：一条执行只需 `0.5 ms` 的点查，凭什么要等 LLVM 花 `80 ms` 做重度编译优化？

### 2.1 从字节码自适应切换到 Flying Start 单遍原生后端（TKDE 2019 → VLDBJ 2021）

TUM 团队第一次出手拆这堵墙，是在 [《Making Compiling Query Engines Practical》](https://db.in.tum.de/~leis/papers/adaptiveexecution.pdf)（TKDE 2019，扩展自 ICDE 2018 的自适应执行工作；注：Umbra 2020 早期系统论文沿用的也是这套自定义字节码解释过渡方案）。为了避免同时维护“一套火山解释器算子 + 一套编译代码生成器”导致语义双轨分裂，他们让查询先生成统一的 IR，由专门编写的字节码解释器立刻开跑，同时后台盯紧各个代码分支的执行进度；一旦发现某个流水线剩余元组还很多，就在运行途中把该分支无缝热切换到后台刚编好的 LLVM 机器码上。

解释过渡虽然砍掉了启动硬屏障，但字节码解释器本身的指令派发开销依然偏高，而且庞大的 LLVM IR 构造起来也不算轻快。能不能干脆连解释器都省掉，**在几百微秒内直接吐出第一版像样的原生机器码**？

这就是 VLDB Journal 2021 名作 [《Tidy Tuples and Flying Start: Fast Compilation and Fast Execution of Relational Queries in Umbra》](https://link.springer.com/content/pdf/10.1007/s00778-020-00643-4.pdf) 的核心突破。它把原本一锅炖的代码生成切成了 `Tidy Tuples`（约 2.2 万行）、`Umbra IR`（约 3,000 行紧凑图式 SSA 与信息单元 `IU`）以及双层后端（约 4,200 行 `Flying Start` 单遍机器码生成器 + 后台异步 `LLVM -O3`），并在 Morsel 数据块边界上通过替换函数指针完成无锁热切换（可点击查看[交互式双层编译与热切换流程图](/diagrams/exec-umbra-flying-start.html)）：

![Umbra 双层自适应编译与 Morsel 函数指针热切换](/images/diagrams/exec-umbra-flying-start.svg)

这套分层里每一笔工程账都算得极其精细：

- **上层 `Tidy Tuples` 治乱**：早期编译型引擎最让人头疼的是 C++ 代码里到处拼接零碎的 IR 指令，改一个 `NULL` 语义像排雷。`Tidy Tuples` 用强类型接口把 SQL 值语义严密包裹起来，下层 IR 根本不用操心数据库业务逻辑。
- **底层 `Flying Start` 抢时间**：它模仿 WebAssembly 浏览器基线编译器的思路，只做单遍（Single-Pass）扫描直出 x86-64 机器码。论文消融实验给出了一组极具说服力的数字：加上轻量级寄存器分配能让生成代码的运行耗时立减 **32%**，性价比极高；但如果再往上堆复杂的线性扫描寄存器分配（Linear-Scan Allocation），编译耗时暴涨 **14%**，执行速度却只抠出来区区 **1%**——于是果断舍弃！最终 `Flying Start` 产出的机器码与满血版 LLVM 相比，虽然中位数多执行了约 `2.3` 倍指令、多花 `1.6` 倍时钟周期（因指令级并行度 IPC 高出约 `1.4` 倍而部分抵消，代码体积约 `2.4` 倍），但编译耗时直接压缩到了毫秒乃至亚毫秒级，短查询不等 LLVM 醒来就已经跑完收工了。

### 2.2 编译期预埋自适应分支与跨指令集移植（Dynamic Blocks & FireARM）

首层机器码飞快跑起来之后，紧接着会冒出两个灵魂拷问：

第一，**机器码一旦编死下发，运行中发现过滤率或连接顺序估错了怎么办？** 如果停下来重新调编译器生成一套新代码，刚省下来的编译延迟又全赔进去了。ADMS 2022 的 **[Dynamic Blocks](https://db.in.tum.de/people/sites/schmidt/papers/dynamic-blocks.pdf)** 想出了一招“空间换时间”：在初次生成机器码时，就把几个备选的过滤谓词次序或局部连接分支一并编进程序体里，中间用可动态改写目标地址的间接跳转连接。运行期根据前几批元组的实测选择率，直接拨动跳转指针切向最优代码段——全程零重编译，在论文自适应过滤与重排测试中拿下了超 2 倍加速（代价是只能切换编译期预先枚举埋好的有限分支，无法凭空变出全新拓扑）。

第二，**自己手搓 x86-64 后端这么爽，换到云厂商力推的 ARM（AArch64）服务器上岂不是要推倒重来？** PVLDB 2023 的 **[FireARM](https://db.in.tum.de/people/sites/gruber/p791-gruber.pdf)**（*Bringing Compiling Databases to RISC Architectures*）专门拿 AArch64 检验了这套分层架构的成色：上面的查询优化器、`Tidy Tuples` 和 `Umbra IR` 纹丝不动，只需重写最底下那几千行目标指令选择与调用约定（ABI）适配即可跑通。四层架构各管一摊的边界至此清晰无比：

| 编译栈层级 | 掌控的核心上下文 | 最拿手的优化活儿 |
| --- | --- | --- |
| **数据库优化器** | 关系代数等价性、基数统计、有趣物理属性 | 连接重排、谓词下推、子查询解关联、物理算子选型 |
| **Tidy Tuples / Umbra IR** | `NULL` 语义、内存元组布局、算子流水线边界 | 紧凑物化、指针混写解引用、领域特化控制流降级 |
| **Flying Start / FireARM** | 目标 CPU 寄存器个数与指令集（x86-64 / AArch64） | 单遍极速指令映射、贪心寄存器分配，压住首行响应延迟 |
| **LLVM 后端** | 完整控制流图（CFG）、别名信息与微架构流水线模型 | 公共子表达式消除（CSE）、死代码删除、循环向量化与深度调度 |

---

## 三、优化器四部曲：当解关联遇上超大连接图

为什么 TUM 团队一边抠底层机器码，另一边却在查询优化器上连发四篇重磅论文？这要从 HyPer/Umbra 的看家本领——**任意嵌套子查询通用解关联（General Subquery Unnesting）**说起。在很多传统数据库还在把复杂 `EXISTS` 或相关标量子查询退化成逐行循环探测（Nested-Loop）时，Umbra 会先把所有相关子查询彻底改写成显式的关系连接（包含大量外连接、半连接与反连接）。这一招威力无穷，但副作用也立竿见影：**哪怕业务只写了一条中等复杂的分析 SQL，解关联展开后的逻辑计划树动辄就有二三十张甚至上百张表相连！**

一旦进入这种深水区，基数估不准、搜索空间指数爆炸、外连接乱换顺序算错数、乃至遍历计划树本身的 CPU 开销，就会排着队找上门来。

### 3.1 JOB 归因诊断与查询图自适应枚举（VLDBJ 2018 & SIGMOD 2018）

面对选出烂计划的优化器，工程上最忌讳瞎改成本常数。[《Query Optimization Through the Looking Glass, and What We Found Running the Join Order Benchmark》](https://db.in.tum.de/~leis/papers/lookingglass.pdf)（VLDBJ 2018，扩展自 PVLDB 2015 经典 JOB 论文）拿真实世界高度偏斜且列间强相关的 IMDb 电影数据集（113 条多表查询）做了一次极其漂亮的对照控制实验：把**真实的中间结果精确基数**直接注入优化器，去隔离检验基数估计（CE）、成本模型（Cost Model）与计划枚举器（Enumerator）到底谁在背锅。

实验结果给所有迷信精细硬件系数的人泼了一盆冷水：随着连接层数加深，独立性假设导致的基数低估动辄偏离三四个数量级；在内存计算场景下，只要喂进准确的基数，哪怕用一个简陋到只把中间结果行数加起来的玩具成本公式 `C_mm`，选出来的计划照样能跟精雕细琢的 PostgreSQL 成本模型打得有来有回——反过来，如果输入基数错了十万倍，再精密的 CPU/I/O 权重也只是在极其认真地放大错误前提。不过在固定相同估计的前提下，完备的动态规划搜索依然显著优于过早剪枝的贪心启发式。

既然尽量搜完备更好，要是真撞上 BI 工具自动生成的 80 张表甚至 4,000 张表连接怎么办？传统优化器喜欢设一条一刀切红线：“不超过 12 张表跑精确动态规划 `DPHyp`，满 13 张表瞬间切贪心算法”。SIGMOD 2018 的 [《Adaptive Optimization of Very Large Join Queries》](https://db.in.tum.de/~radke/papers/hugejoins.pdf) 一针见血地指出：**决定搜索难度的根本不是表数，而是连接图的拓扑稠密度！** 一条首尾相接的 50 表链状图，连通子图（Connected Subgraphs）数量很少，精确搜索眨眼就完；而一张两两相连的 16 表星团稠密图，状态数早就爆表了。

因此，Umbra 把优化器改造成了紧盯“端到端用户等待时间 = 规划耗时 + 执行耗时”的平滑分档器：先预估连接图的连通子图规模——简单图跑精确 `DPHyp`；中等复杂度图先按启发式排出一条线性表序列，随后只在这条序列的**连续子区间**上跑线性化动态规划（Linearized DP，把任意子集的指数级空间压缩成区间级的二次/三次空间，同时保留局部翻盘纠错能力）；超巨型图再进一步结合贪心与迭代动态规划，稳稳撑起超过 4,000 个关系的极端查询。

### 3.2 LinDP++ 语义守门与 Indexed Algebra 动态树索引（BTW 2019 & PVLDB 2023）

2018 年的线性化 DP 主要对付可以自由交换结合的内连接（Inner Join），但前面说过，子查询解关联吐出来的满地都是 `LEFT OUTER`、`SEMI` 和 `ANTI JOIN`——乱换次序会直接把空值补位和存在性去重语义搞错。

BTW 2019 最佳论文 **[LinDP++](https://btw.informatik.uni-rostock.de/index.php/de/tagungsbaende/send/3-tagungsbaende/tagungsband.pdf)** 把外连接/半连接的保留侧约束严丝合缝地编进了线性化区间枚举里，确保“先等价，再比价”。不仅如此，它还顺手打破了“永远禁止笛卡尔积（Cross Product）”的教条：假设两张极小的维表 `u` 和 `w` 各自连着庞大的事实表 `v`，先算 `u × w` 虽然没有直接连接条件，却能合成复合键一次性过滤 `v`！为了防止放开任意笛卡尔积引爆搜索空间，`LinDP++` 只在连接图上试探长度为 2 的二跳邻居路径 `(u, v, w)`，且仅当满足严格基数不等式：

$$
|u \times w| < |u \bowtie v| \quad \text{且} \quad |u \times w| < |v \bowtie w|
$$

且基数具备可靠上界时，才保守补入这条人工笛卡尔边（论文实例中将最优计划代价从 `1.84M` 直接腰斩至 `0.94M`）。

当百表大查询和上百条代数改写规则同时运转时，团队又抓到了一个藏在优化器肚子里的隐形瓶颈（PVLDB 2023 [《Asymptotically Better Query Optimization Using Indexed Algebra》](https://www.vldb.org/pvldb/vol16/p3018-fent.pdf)）：优化规则每挪动一次算子，都要反复追问“某个字段是底下哪个算子产出的？沿途被谁消费了？两个节点的最近公共祖先在哪？”如果按传统做法在每个算子节点上维护整棵子树的自由变量列集合，每次局部改写都向上全链路刷新，在深链计划树上就会退化成 `O(n^2)` 的二次方复杂度！注意，这篇论文里的“Indexed”跟数据库的 B-Tree 物理索引毫无关系——它是用算法竞赛里经典的动态树结构 **Link-Cut Tree（动态森林）** 给优化器内存里的代数计划树本身建了索引，让树链切割、嫁接与路径列属性查询统统降到均摊 `O(log n)`，整棵树优化维护从 `O(n^2)` 压回 `O(n log n)`，连常规 TPC-H / TPC-DS 的总编译优化耗时都跟着缩短了超 `1.8` 倍。

---

## 四、鲁棒连接执行：一手抓哈希表局部性，一手推迟笛卡尔展开

就算优化器用最快的速度挑出了表面最优的连接树，一到真实数据上跑连接，还是有两颗地雷随时可能引爆：一颗埋在**单个 Hash Join 算子内部**（键分布严重偏斜），另一颗埋在**多表二元连接之间**（多对多菱形膨胀）。2024 年的两篇论文刚好一人拆了一颗。

### 4.1 Unchained 哈希表：消除倾斜键的指针追逐税（DaMoN 2024）

在编写内存哈希连接时，内核工程师常陷入两难：开放寻址法（Open Addressing）在无重复的主外键 `1:n` 连接上紧凑飞快，可一旦 Build 侧涌入大量相同键或哈希冲突，探测距离就会急剧拉长；拉链法（Chaining）虽然不怕重复键，但沿着链表逐节点跳指针又会制造满屏随机 Cache Miss。

DaMoN 2024 的 **[Unchained Hash Table](https://db.in.tum.de/~birler/papers/hashtable.pdf)**（*Simple, Efficient and Robust Hash Tables for Join Processing*）干脆把指针链彻底砍掉，换上了一套五合一的紧凑流水线：

1. **并行分区聚拢**：Build 阶段先用软件写合并缓冲区（Software Write-Combining Buffer）把元组按哈希前缀分桶归拢；
2. **连续邻接数组（Adjacency Array）**：把落入同一槽位的所有匹配元组紧挨着排在连续内存数组里，取代东一榔头西一棒子的链表节点指针；
3. **内嵌轻量级 Bloom Filter**：在目录槽位上顺手压入标签位过滤器，让绝大多数根本连不上的探测行在触碰数据区前就被当场挡回；
4. **流水化预取探测**：同时挂起多个未决探测请求，利用现代 CPU 的内存级并行度（MLP）掩盖 DRAM 延迟。

这套结构让主外键快路径与重度倾斜的多对多路径和谐共处，相比常规开放寻址哈希表平均提速约 2 倍，在充斥高重复度节点的图分析连接上更是拉开了最高约 20 倍的差距。

### 4.2 Diamond Hardened Join：拆开 Lookup 与 Expand（PVLDB 2024）

但有些连接灾难，换再快的哈希表也救不回来。设想在社交关系或商品标签上跑一条菱形（Diamond）或三角环路查询：`A` 连 `B`、`A` 连 `C`，最后 `B` 和 `C` 还要在下游汇合过滤。假设某个热点键在 `B` 里有 1,000 条匹配，在 `C` 里也有 1,000 条匹配，但合拢检验后最终存活下来的只有寥寥 2 条。在传统的二元连接（Binary Join）树里，只要第一步先连了高扇出的分支，算子就会老老实实先把 `1,000 × 1,000 = 1,000,000` 行中间笛卡尔组合全物化出来，再推给下一步去砍掉 99.999% 的废品！

PVLDB 2024 的 **[Diamond Hardened Joins](https://www.vldb.org/pvldb/vol17/p3215-birler.pdf)** 点破了病根：**传统二元连接把“验证能不能连上（Lookup，只会收缩行数）”和“把一对多匹配逐条复制吐出（Expand，会成倍放大行数）”死死绑在了同一个算子里！**

```text
传统二元连接（过早耦合扩张）：
  Input ──→ BinaryJoin(同时查表 + 展开 100 万行中间元组) ──→ 下游连接过滤剩 2 行

Diamond Hardened Join（物理代数显式拆解）：
  Input ──→ Lookup(B) ──→ Lookup(C) ──→ 尽早做完半连接裁剪与预聚合 ──→ Expand / Expand3 按需展开存活行
```

注意它并不是在运行中途发现行数爆炸后推倒重编计划，而是在物理代数层直接把一次连接拆成 `Lookup` 和 `Expand` 两个独立算子：优化器拼命把所有只起过滤定位作用的 `Lookup`、半连接削减和可提前聚合往前排，把真正引发数据膨胀的 `Expand` 死死摁到流水线最后；遇到三角闭合环路时，还祭出三路协同算子 `Expand3` 同时核验第三条边，无需推翻整个二元编译引擎重写全套最坏情况最优连接（WCOJ），就在基数估计基准测试的恶劣菱形查询上跑出了最高约 **500 倍**的惊人加速，且在常规 TPC-H 与 JOB 上几乎零回退。

---

## 五、关系模型的边界实验：数组、库内机器学习与流处理

当存储、编译、优化与连接内核全部成型后，TUM 团队顺理成章地把触角伸向了传统 SQL 之外的领地：科学计算数组、库内机器学习（In-DB ML）以及实时流处理。把这五篇探索论文连起来读，最有价值的恰恰不是“数据库无所不能”的凯歌，而是团队亲手用实验测出来的**诚实反思**。

起初三步走得顺风顺水：

- **数组前端降级（EDBT 2022 [ArrayQL](https://db.in.tum.de/~schuele/data/arrayql.pdf)）**：不另起炉灶写数组引擎，而是把 `n` 维数组坐标与 `m` 个数值映射成 `n + m` 列的关系表，在语义分析阶段直接把矩阵乘法等 9 类数组运算降级为标准的 `Join + Projection + GroupBy`，顺手复用现成的优化器、MVCC 与代码生成器（过滤聚合表现轻快，但坐标平移与翻转仍需物化表函数买单）。
- **递归 SQL + 自动微分 + GPU 内核生成（DAPD 2022 [Recursive SQL and GPU-support](https://link.springer.com/article/10.1007/s10619-022-07417-7)）**：用递归 CTE 表承载模型参数迭代，新增 `derivation` 算子在代码生成期对 SQL Lambda 表达式做反向模式自动微分（Reverse-Mode AD），再由充当流水线断点的梯度下降算子攒批物化，直接编译吐出 NVPTX 代码喂给 GPU 执行，把数据清洗、训练与验证揉进了同一条查询里。
- **编译器自动消冗余测试（DEEM 2022 [LLVM AD](https://db.in.tum.de/~schuele/data/forward.pdf)）**：对比前向自动微分（Forward AD，生成的导数冗余表达式更多）与反向自动微分时发现，只要在小规模静态计算图上明确标好指针无别名（`noalias`），LLVM 强大的公共子表达式消除（CSE）与死代码删除就能自动把大量冗余计算抹平，让两者的最终 PTX 核函数运行耗时相差无几（差距主要体现在编译耗时与多输入单输出场景上）。

沿着这条路再往前迈一步，岂不是连深度神经网络都能直接拆成关系元组来训练了？2023–2024 年的 **[The Duck's Brain](https://arxiv.org/pdf/2312.17355)**（*Training and Inference of Neural Networks in Modern Database Engines*）真的在 Umbra 和 DuckDB 里分别用“纯 SQL-92 坐标元组关系表示”与“原生数组（Array）类型表示”跑了一遍神经网络训练与推理，并拿 NumPy 当标尺对照。结果账单一出来，触目惊心：

- 在极小的 Iris 数据集上，关系元组路径每轮就要吃掉 `10–20 MiB` 内存，而数组路径只需 `1–1.6 MiB`；
- 一旦换到稍微大一点的 MNIST 手写数字数据集，把稠密权重矩阵拆成带坐标键的宽元组、挂进连接哈希表与分组聚合状态后，关系路径的内存消耗瞬间爆炸到了约 **`25 GiB`**，而保留连续张量布局的数组路径还不到 **`5 MiB`**——足足差出五千倍！哪怕用上 Umbra 原生数组，推理速度距高度调优的 NumPy 矩阵库仍有约一个数量级的差距。

几乎在同一时期，CDMS@VLDB 2023 的 **[Relation-Based Streaming](https://ceur-ws.org/Vol-3462/CDMS7.pdf)** 把近期实时事件流封装成固定窗口的**环形缓冲关系（Ring-Buffered Relation）**，让即席流表关联（Stream-Table Join）直接复用 Umbra 飞快的编译执行器，免去了跨系统倒腾历史维表的痛苦；但它统一的是“近期窗口状态的可查询性”，并没有替你变出分布式流引擎（如 Flink）里的全局 Checkpoint、Watermark 乱序水位线、端到端 Exactly-Once 与反压控制面协议。

这两盆清醒的冷水，反而淬炼出了整套研究路线最深刻的一句架构箴言：**逻辑接口的大一统（Logical Unification），绝不等于底层物理代数的大一统（Physical Unification）。**

```text
           SQL 查询  /  实时流窗口  /  多维数组  /  库内机器学习
                                 │
                                 ▼
                    保留领域语义的统一逻辑中间层
                                 │
                                 ▼
                  共享优化器骨架、存储生命周期与编译框架
                 ┌───────────────┼───────────────┐
                 ▼               ▼               ▼
          关系元组物理代数   稠密张量/算子融合代数  流式窗口生命周期原语
                 └───────────────┼───────────────┘
                                 ▼
                    多核 CPU  /  GPU  /  云对象存储
```

SQL 和关系模型是极佳的声明式胶水与数据治理入口，但绝不能在优化器一开头就把稠密矩阵的维度步长（Shape/Stride）过早拍扁成一盘散沙般的 `(row_id, col_id, val)` 元组——共享编译与事务底座，同时尊重不同负载原生的物理内存布局，才是库内多模计算的真正出路。

---

## 六、十九篇论文的全景总账与未竟问题

把这七年间的 19 篇论文按“解决了哪道悬崖 → 暴露出什么新矛盾”串成一张时间表，整条学术谱系的内在张力便一目了然：

| 年份 | 代表论文 | 攻克的核心系统瓶颈 | 自然引出的下一棒问题 |
| --- | --- | --- | --- |
| **2018** | Looking Glass / JOB | 用真实基数注入剥离 CE、成本与枚举器的误差责任 | 面对几十上百表连接图，完备搜索本身如何不超时？ |
| **2018** | Adaptive Very Large Join | 按连通子图复杂度从 `DPHyp` 平滑降级到线性化区间 DP | 外连接、半连接与受控笛卡尔积怎样保证语义合法？ |
| **2019** | LinDP++ | 在线性化 DP 中严守非内连接约束并安全引入二跳笛卡尔边 | 计划树与改写规则变庞大后，树属性分析本身变慢怎么办？ |
| **2019** | Making Compiling Engines Practical | 同一份 LLVM IR 先字节码解释再按进度热切 JIT | 解释器仍有开销，能不能亚毫秒级直接吐出原生机器码？ |
| **2020** | Umbra (CIDR) | 可变长页 + 64 位 `Swip` + 乐观锁 + German Strings 打通 SSD | 磁盘换页下如何跑高频 OLTP 多版本事务？编译能否更快？ |
| **2021** | Tidy Tuples & Flying Start | 三层代码生成 + 单遍原生后端把编译延迟压进毫秒内 | 机器码编好后撞上基数误判怎么改？换 ARM 芯片怎么移？ |
| **2022** | Memory-Optimized MVCC | 内存私有 Undo 链 + 页局部映射表兼顾换页与高吞吐事务 | 数据搬上云端对象存储后，冷热混合 HTAP 怎么存？ |
| **2022** | Dynamic Blocks | 编译期预埋多路机器码片段，运行期零重编拨动跳转指针 | 预埋分支仍受限于编译前能想到的有限候选集 |
| **2022** | ArrayQL | 数组语法降级为关系代数，验证编译器与事务层逻辑复用 | 关系坐标表示跑密集数值计算到底有多大物理代价？ |
| **2022** | Recursive SQL + GPU | 递归关系表 + 反向自动微分 + GPU NVPTX 核函数生成 | 自动微分吐出的大量冗余算式该由哪一层负责化简？ |
| **2022** | LLVM AD | 靠 `noalias` 别名标注借力 LLVM 消除前/反向微分冗余计算 | 真正的大规模张量运算靠标量编译器优化够用吗？ |
| **2023** | Indexed Algebra | 用 Link-Cut 动态树索引把计划树列传播从 `O(n^2)` 降到 `O(n log n)` | 静态代数维护快了，如何与运行期实测反馈闭环联动？ |
| **2023** | AnyBlob | `io_uring` 异步下载器解决客户端 CPU 先于云存储带宽饱和 | 只有高吞吐冷读还不够，在线热更新与日志如何协同？ |
| **2023** | FireARM (RISC) | 实证 `Umbra IR` 上层复用、仅替换薄后端即可迁往 AArch64 | 多指令集自研后端与通用编译器的长期维护边界在哪？ |
| **2023** | Relation-Based Streaming | 环形缓冲关系表让即席流表关联直接复用编译型执行内核 | 缺少分布式流处理的容错快照、乱序水位线与增量代数 |
| **2023/24** | The Duck's Brain | 实测揭露纯关系元组跑神经网络导致数千倍内存膨胀 | 多物理代数（关系/张量/流）如何共享同一个优化搜索器？ |
| **2024** | Unchained Hash Table | 分区邻接数组 + 内嵌 Bloom Filter + 流水探测免疫键值偏斜 | 单算子不退化了，多表菱形连接过早吐出天量中间行咋办？ |
| **2024** | Diamond Hardened Join | 拆开收缩型 `Lookup` 与膨胀型 `Expand`（含 `Expand3`）推迟展开 | 如何与运行期基数反馈及更多最坏情况最优算法深度融合？ |
| **2024** | Colibri | 热未压缩 PAX 页 + 冷压缩列文件 + 日志服务构成云端 HTAP | 跨可用区多写节点扩展、多租户隔离与云账单治理如何演进？ |

回看这张演进表，Umbra 给整个数据库系统界留下的最宝贵财富，不是某一个具体的哈希函数或位操作技巧，而是**“永远盯着性能悬崖做局部化拆解”**的工程哲学：无论是 `Swip` 指针里的最低标记位、页局部版本映射表、`German Strings` 里的 4 字节前缀，还是 `Dynamic Blocks` 里的间接跳转，它们都在做同一件事——**让最常见的快路径只看手边几个字节就当场拍板冲过去，把昂贵的全局协调、磁盘 I/O、LLVM 重度优化和笛卡尔展开统统关进稀有慢路径的笼子里。**

当然，作为一系列各自独立验证的学术原型（而非单一商业发行版的功能承诺），这条路线上依然留着五个极具吸引力的开放问题等待后来者接棒：**单次执行内的 `Dynamic Blocks` 测量如何沉淀为跨查询的统计与计划缓存闭环？关系元组、稠密张量与流窗口三套异构物理代数如何在同一个 Cascades Memo 里公平比价？`Colibri` 的云端冷热引擎如何迈向多写节点弹性扩展？`Unchained`、`Lookup/Expand` 与因子化执行如何由优化器自动编排组合？而环形缓冲流关系又该如何补齐增量视图维护（IVM）与精确一次语义？** 这每一个追问，几乎都是下一代数据库内核正在攻关的前沿阵地。

---

## 参考文献与谱系校准说明

> **文献核验与常见误读校准说明**：本文依据慕尼黑工业大学数据库组公开发表的 19 篇一手论文原文与实验章节整理（对照站内 [2026 数据库内核研究进展]({{< relref "2026-09-19-database-research-frontiers.md" >}}) 与 [经典数据库论文精读笔记]({{< relref "2026-08-23-database-paper-reading-notes.md" >}})）。阅读二手综述时需留意几处高频概念错位：其一，`Flying Start` 出自 2021 年 VLDBJ 论文，而 2019 年 TKDE 论文采用的是 LLVM IR 字节码解释过渡；其二，`Indexed Algebra` 是用 Link-Cut Tree 加速优化器内存计划树的路径属性分析，与数据库 B-Tree 物理索引无关；其三，`Unchained` 哈希表并非带标签的开放寻址法，而是分区构建加连续邻接数组；其四，`Diamond Hardened Join` 靠物理代数拆解 `Lookup` 与 `Expand` 在优化期推迟膨胀，并非运行中途推翻重编计划；其五，部分二手资料提及的所谓 CIDR 2023 *"Instead of Rewriting Everything: A Case for Compiler-Driven Database Engines"* 在 CIDR 官方论文集与作者主页均无此文，探讨编译引擎跨架构移植的一手文献实为 PVLDB 2023 的 *Bringing Compiling Databases to RISC Architectures*。

### 一、存储引擎、事务与云端 HTAP

1. Thomas Neumann, Michael J. Freitag, *Umbra: A Disk-Based System with In-Memory Performance*, [CIDR 2020](https://cidrdb.org/cidr2020/papers/p29-neumann-cidr20.pdf).
2. Michael J. Freitag et al., *Memory-Optimized Multi-Version Concurrency Control for Disk-Based Database Systems*, [PVLDB 15(11), 2022](https://www.vldb.org/pvldb/vol15/p2797-freitag.pdf).
3. Dominik Durner, Viktor Leis, Thomas Neumann, *Exploiting Cloud Object Storage for High-Performance Analytics (AnyBlob)*, [PVLDB 16(11), 2023](https://www.vldb.org/pvldb/vol16/p2769-durner.pdf).
4. Tobias Schmidt, Dominik Durner, Viktor Leis, Thomas Neumann, *Two Birds With One Stone: Designing a Hybrid Cloud Storage Engine for HTAP (Colibri)*, [PVLDB 17(11), 2024](https://www.vldb.org/pvldb/vol17/p3290-schmidt.pdf).

### 二、自适应查询编译与跨架构代码生成

5. André Kohn, Viktor Leis, Thomas Neumann, *Making Compiling Query Engines Practical*, [IEEE TKDE 2019](https://db.in.tum.de/~leis/papers/adaptiveexecution.pdf).
6. Timo Kersten, Viktor Leis, Thomas Neumann, *Tidy Tuples and Flying Start: Fast Compilation and Fast Execution of Relational Queries in Umbra*, [VLDB Journal 2021](https://link.springer.com/content/pdf/10.1007/s00778-020-00643-4.pdf).
7. Tobias Schmidt et al., *Efficiently Compiling Dynamic Code for Adaptive Query Processing (Dynamic Blocks)*, [ADMS @ VLDB 2022](https://db.in.tum.de/people/sites/schmidt/papers/dynamic-blocks.pdf).
8. Philipp Gruber et al., *Bringing Compiling Databases to RISC Architectures (FireARM)*, [PVLDB 16(4), 2023](https://db.in.tum.de/people/sites/gruber/p791-gruber.pdf).

### 三、查询优化器与鲁棒连接执行

9. Viktor Leis et al., *Query Optimization Through the Looking Glass, and What We Found Running the Join Order Benchmark*, [VLDB Journal 2018](https://db.in.tum.de/~leis/papers/lookingglass.pdf).
10. Bernhard Radke, Thomas Neumann, *Adaptive Optimization of Very Large Join Queries*, [SIGMOD 2018](https://db.in.tum.de/~radke/papers/hugejoins.pdf).
11. Bernhard Radke, Thomas Neumann, *LinDP++: Generalizing Linearized DP to Crossproducts and Non-Inner Joins*, [BTW 2019](https://btw.informatik.uni-rostock.de/index.php/de/tagungsbaende/send/3-tagungsbaende/tagungsband.pdf).
12. Philipp Fent, Thomas Neumann, *Asymptotically Better Query Optimization Using Indexed Algebra*, [PVLDB 16(11), 2023](https://www.vldb.org/pvldb/vol16/p3018-fent.pdf).
13. Altan Birler, Thomas Neumann, Viktor Leis, *Simple, Efficient and Robust Hash Tables for Join Processing (Unchained)*, [DaMoN 2024](https://db.in.tum.de/~birler/papers/hashtable.pdf).
14. Altan Birler, Alfons Kemper, Thomas Neumann, *Robust Join Processing with Diamond Hardened Joins*, [PVLDB 17(11), 2024](https://www.vldb.org/pvldb/vol17/p3215-birler.pdf).

### 四、多维数组、库内机器学习与流处理扩展

15. Maximilian E. Schüle et al., *ArrayQL Integration into Code-Generating Database Systems*, [EDBT 2022](https://db.in.tum.de/~schuele/data/arrayql.pdf).
16. Maximilian E. Schüle et al., *Recursive SQL and GPU-support for In-Database Machine Learning*, [Distributed and Parallel Databases 2022](https://link.springer.com/article/10.1007/s10619-022-07417-7).
17. Maximilian E. Schüle et al., *LLVM Code Optimisation for Automatic Differentiation*, [DEEM @ SIGMOD 2022](https://db.in.tum.de/~schuele/data/forward.pdf).
18. Maximilian E. Schüle et al., *Relation-Based In-Database Stream Processing*, [CDMS @ VLDB 2023](https://ceur-ws.org/Vol-3462/CDMS7.pdf).
19. Maximilian E. Schüle et al., *The Duck's Brain: Training and Inference of Neural Networks in Modern Database Engines*, [arXiv:2312.17355 / Datenbank-Spektrum 2024](https://arxiv.org/pdf/2312.17355).
