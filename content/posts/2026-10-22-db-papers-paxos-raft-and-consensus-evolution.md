---
title: "【Paper】分布式共识协议演进精读：Paxos 泛化与 Quorum 弱化、Generalized Consensus、Raft 证明边界与 Ark 工程实践"
date: 2026-10-22T13:30:00+08:00
lastmod: 2026-11-12T00:00:00+08:00
slug: "db-papers-paxos-raft-and-consensus-evolution"
description: "从 FLP 不可能定理与异步网络脑裂防线出发，串联精读 Heidi Howard 非对称 Quorum 与 Paxos 泛化、单次写寄存器统一共识与无主可交换命令、Raft 强领导者与 Figure 8 幽灵回滚反例、Ongaro 博士论文成员变更与线性化读边界，以及 MongoDB 前身 Ark 的拉式复制确认栅栏。"
categories: ["数据库"]
tags: ["论文精读", "分布式共识", "Paxos", "Raft"]
series: ["数据库经典论文精读"]
toc: true
math: true
---

> 本系列第 10/10 篇 · [数据库经典论文精读总索引]({{< relref "2026-10-22-database-papers-source-reading.md" >}}) · [上一篇：事务隔离与分布式存储]({{< relref "2026-10-22-db-papers-isolation-concurrency-and-distributed-transactions.md" >}})

设想机架上只摆着两台数据库服务器 `N1` 和 `N2`，中间隔着一台随时可能丢包或把数据包卡住 10 秒的普通以太网交换机。此时客户端甲连上 `N1` 请求写入 `x = 1`，客户端乙连上 `N2` 请求写入 `x = 2`。在没有全局共享时钟的异步网络中，`N1` 收不到 `N2` 的回音时，永远无法区分“**`N2` 已经拔电烧毁**”与“**`N2` 活得好好的，只是回包堵在路由器队列里**”这两种状态：

- 如果允许 `N1` 和 `N2` 在失联时各自独立拍板提交，两台机器立刻分裂出 `x = 1` 与 `x = 2` 两个平行宇宙（脑裂，Split-Brain），账本彻底报废；
- 如果规定必须同时拿到 `N1` 和 `N2` 两张赞成票才能提交，那么只要其中任意一台机器重启维护，整个数据库就当场停摆，连单节点故障都扛不住；
- 更冷酷的数学天花板来自 1985 年的 **FLP 不可能定理**（Fischer-Lynch-Paterson）：在完全异步、允许哪怕 1 个节点崩溃的网络模型中，没有任何确定性算法能够同时保证**安全性（Safety，绝不达成冲突决议）**与**活性（Liveness，有限步内必然终止）**——因为敌手调度器总能在系统即将跨过决策临界点的那一瞬间，精准延迟关键节点的一条消息，把集群永远困在左右摇摆的双价态（Bivalent State）里。

既然纯异步确定性共识在数学上无解，现代分布式数据库脚下的共识引擎又是靠什么安身立命的？答案是一条铁血的工程切割线：**把安全性与物理时间彻底解耦，让正确性 100% 建立在“单调递增的逻辑纪元（Epoch / Ballot / Term）+ 必然相交的法定人数集合（Intersecting Quorums）”之上；而把活性交给部分同步假设下的随机化超时或稳定主节点去争取。** 哪怕全网时钟错乱、消息延迟三天三夜，系统顶多暂时选不出主而停写，却绝不会吐出半字节自相矛盾的脏决议。

## 走出教科书的两个思维定式：对称多数派与“过半即安全”

一旦引入“至少 3 节点组成多数派（\(\lfloor N/2 \rfloor + 1\)）+ 选出一个 Leader 串行复制日志”的经典范式，绝大多数工程师对分布式共识的理解往往就此定格。然而把镜头推近到真实广域网与生产级存储引擎内部，两条流传最广的教科书直觉其实都是错的：

1. **定式一：“Paxos 必须处处使用 \(\lfloor N/2 \rfloor + 1\) 的对称多数派，且必须靠单一主节点给所有命令排全序。”**
   错。在单值共识中，同一个纪元内部之所以不会写出两个冲突值，靠的是“每个纪元唯一归属一个提议者”，根本没用到法定人数相交！法定人数相交唯一派上用场的时刻，是**新纪元的第一阶段（读取历史）去探测旧纪元的第二阶段（已写状态）**。一旦看破这层窗户纸，稳态写完全可以只等极少数节点（例如 5 节点只等 2 节点），无相互干扰的并发写命令甚至无需主节点排序就能在 1 次网络往返（1 RTT）内直接落盘。
2. **定式二：“只要一条日志已经落盘到 \(\lfloor N/2 \rfloor + 1\) 个多数派节点上，它就铁定提交、绝无可能回滚。”**
   大错特错！无论是在强领导者推式复制的 **Raft**（著名的 `Figure 8` 反例），还是在异步拉式链状复制的 **MongoDB / Ark**（`Problem 3.1` 缺陷）中，一条明明已经躺在 5 台机器里 3 台磁盘上的旧任期日志，依然可能在下一秒被持有更高任期凭证的新主合法抹平！

作为本十篇系列的收官之作，本篇串联精读 `lonng/db-papers` 共识专题下的 5 篇深度文献（含两部合计逾 400 页的剑桥与斯坦福博士学位论文），从 Quorum 集合论泛化一路推演到工业界每一行状态机护栏的诞生始末：

| 论文与年份 | 切入维度 | 打破的隐含假设或工程盲区 | 核心数学不变式 / 协议原语 | 现实系统落点与代价边界 |
| :--- | :--- | :--- | :--- | :--- |
| **Howard (2018/19)** `#32` | Paxos 理论重构与非对称 Quorum | 推翻“同阶段 Quorum 必须相交”与“选值必取最高纪元” | 跨阶段定向交集 \(\forall f < e:\; Q_1^e \cap Q_2^f \neq \emptyset\)（计数特例 \(q_1 + q_2 > N\)）与排除不可能旧多数派的 `V_dec` | 稳态复制可缩减 \(Q_2\) 降延迟，代价是新主选举恢复需凑齐更大的 \(Q_1\) |
| **Howard & Mortier (2019)** `#30` | 共识底层抽象统一与 1-RTT 快路径 | 剥离选主/角色术语，统一经典、Flexible、Fast 与主备重配 | 无限单次写持久寄存器序列 \(\{R_0, R_1, \dots\}\) + 四条写规则 + 共享纪元三元交集 \(A \cap B \cap C \neq \emptyset\) | 与 Lamport 可交换命令图（`c-structs`）互补，无冲突 1 RTT 直写，冲突退回协调 |
| **Raft Extended (2014)** `#33` | 可理解性强领导者状态机复制 | 消除乱序日志空洞，揭示“按副本数提交旧任期日志”的覆写灾难 | `(lastLogTerm, lastLogIndex)` 选举门禁 + **仅对 `currentTerm` 日志计数提交**并借匹配性质间接封存前缀 | 日志严格单向流动极易实现，但跨任期提交依赖当前任期新项（或上任 `no-op`）锚定 |
| **Ongaro PhD (2014)** `#31` | 共识从论文原型走向生产全栈闭环 | 补齐动态扩缩容脑裂防护、免写盘强一致读、快照与形式化证明边界 | 单步增删节点 \(C_{\text{old}} \to C_{\text{new}}\)、`Pre-Vote` 租约护栏、`ReadIndex` 心跳栅栏与 400 行 TLA+ 规约 | 诚实划定 TLA+ 仅覆盖固定成员核心安全性；成员变更与快照耦合处仍需严防工程越界 |
| **Ark (2014)** `#35` | 生产级拉式链状复制与动态写关注 | 揪出副本投赞成票后继续向旧主回传复制 `Ack` 导致 `majority` 写回滚的漏洞 | 词典序 `GTID` + 持久化 `maxVotedTermId` + **投赞成票后禁向低任期旧主发 `Ack` 栅栏** | 完美适配异步拉取与可逆回滚存储，直接奠定 MongoDB `pv1` 共识升级蓝图 |

---

## 打破同构多数派迷思：Paxos 泛化、寄存器公理与 1-RTT 快速共识

Leslie Lamport 的经典 Paxos 之所以令几代系统工程师望而生畏，一方面源于古希腊城邦议会的隐喻包装，另一方面源于它把太多本可正交拆解的设计决策——对称多数派、两阶段对称交集、单主独占槽位与按最高纪元机械搬运旧值——拧成了一个密不透风的整体。剑桥大学 Heidi Howard 的系列研究像一把精准的手术刀，把这些耦合层层剥开。

![Paxos 共识泛化谱系：从对称多数派到跨阶段非对称 Quorum 与 1-RTT 冲突恢复](/images/diagrams/paper-cons-flexible-generalized-quorums.svg)

### Howard（2018/2019）：Distributed Consensus Revised——拆解 Paxos 的三重隐含冗余 {#paper-32}

- **论文来源**：Heidi Howard, *Distributed Consensus Revised*, University of Cambridge PhD Dissertation (Tech Report UCAM-CL-TR-935), 2018/2019 · [原始 PDF（共 151 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/consensus/distributed-consensus-revised.pdf)（经典 Paxos 持久化顺序引理见 Lemma 7–8 PDF p. 40；跨阶段非对称 Quorum 见 Revision A PDF p. 76；纪元定向交集与历史复用见 Revision B–C PDF pp. 81–94；`V_dec` 五节点算例见 PDF p. 105；共享纪元三元交集见 PDF pp. 122–138）。

在动刀改造 Paxos 之前，Howard 先在第二章把单值经典 Paxos 的底层存储假设抠到了字节级（Lemma 7–8，PDF p. 40）：每个 Acceptor 持久维护两个变量——已承诺不再响应更低编号的纪元上界 `e_pro`，以及已接受提案的纪元与值 `(e_acc, val)`。为什么磁盘落盘顺序必须死守恒等式 \(e_{\text{pro}} \ge e_{\text{acc}}\)？不妨推演一次反顺序刷盘引发的断电事故：

1. 3 节点集群 `{A1, A2, A3}` 中，`A1` 和 `A2` 先收到纪元 `5` 的提案 `(5, x)`；假设工程师为了省一次 `fsync`，在 `A2` 上先把 `(e_acc=5, val=x)` 刷入数据文件，还没来得及把元数据文件里的 `e_pro` 从 `3` 更新到 `5` 就遭遇机房瞬时断电；
2. 此时 `(5, x)` 已在 `{A1, A2}` 达成多数派决议并告知客户端成功，随后 `A1` 网络隔离；
3. `A2` 重启恢复后，磁盘状态变成荒谬的 **`e_pro = 3, e_acc = 5`**！此时迟到的旧提议者拿着纪元 **`4`** 发来 `Propose(4, y)`——由于 `4 >= e_pro(3)`，`A2` 竟然合法放行并用 `(4, y)` **倒退覆盖**了 `(5, x)`，协同 `A3` 把已提交的 `x` 篡改成了 `y`！

唯有每次接受更高纪元提案时将 `e_pro` 与 `(e_acc, val)` 置于同一原子落盘记录（或严格先刷 `e_pro` 后刷 `e_acc`），才能从存储根基上封死纪元倒退。夯实这层地基后，论文沿着“**安全性到底在哪一步真正用到了集合相交**”展开了三次层层递进的理论松绑：

#### 第一重松绑（Revision A）：同阶段互不相交与 Flexible Paxos（PDF p. 76）

回想经典 Paxos 要求所有第一阶段（Prepare/Promise）法定人数集合与第二阶段（Propose/Accept）法定人数集合统统取严格多数派 \(\lfloor N/2 \rfloor + 1\)，从而任意两个集合必有交集。但仔细追问：
- 两个不同纪元的第一阶段集合 \(Q_1, Q'_1\) 需要相交吗？不需要，Phase 1 只是探路读状态，本身不拍板任何决议；
- 两个不同纪元的第二阶段集合 \(Q_2, Q'_2\) 需要直接相交吗？**同样不需要！** 同一个纪元内只有一个独占提议者，天然不会自相矛盾；而跨纪元 \(f < e\) 的安全性，完全是由**高纪元 \(e\) 的第一阶段集合 \(Q_1\) 撞见低纪元 \(f\) 的第二阶段集合 \(Q_2\) 中至少一个知情节点**来传递的！

> "There is no need to require that phase one quorums intersect with each other nor that phase two quorums intersect with each other. Since no intersection is required within phases, quorums within each phase of Classic Paxos can be disjoint." *(PDF p. 76)*

将这一洞察写成数学条件，经典同构多数派瞬间泛化为 **Flexible Paxos** 的跨阶段交集公理：

$$\forall Q_1 \in \mathcal{Q}_1,\; \forall Q_2 \in \mathcal{Q}_2:\quad Q_1 \cap Q_2 \neq \emptyset \qquad (\text{按节点计数特例：}\; q_1 + q_2 > N)$$

对比几种典型法定人数几何在真实集群中的延迟与容错账本，立刻能看清这颗旋钮的工程爆发力：

| 法定人数构造方案 | 节点总数 \(N\) | 选主探测集 \(Q_1\) | 稳态复制集 \(Q_2\) | 稳态复制等待代价 | 稳态可容忍宕机数 | 选主恢复可容忍宕机数 |
| :--- | :---: | :---: | :---: | :--- | :---: | :---: |
| **经典对称多数派** | `5` | 任意 `3` 节点 | 任意 `3` 节点 | 需等跨机房第 3 快节点 | `2` 台 | `2` 台 |
| **非对称写加速（Flexible）** | `5` | 任意 `4` 节点 | 任意 **`2`** 节点 | **主 + 本地最近 1 从即提交！** | **`3` 台** | `1` 台（\(5 - 4 = 1\)） |
| **大规模非对称写加速** | `9` | 任意 `7` 节点 | 任意 **`3`** 节点 | 9 副本仅等最近 3 节点 | **`6` 台** | `2` 台（\(9 - 7 = 2\)） |
| **二维网格 Quorum（Grid）** | `9` (`3×3`) | 任意 `1` 整列（`3` 台） | 任意 `1` 整行（`3` 台） | 行列必交于 1 点，**双阶段均仅需 3 台！** | 最坏 `2` 台（整行挂） | 最坏 `2` 台（整列挂） |
| **共享纪元快路径（Fast）** | `5` | 任意 `4` 节点 | 任意 **`4`** 节点 | 无主 1 RTT 直写，需三元交集 \(4+8>10\) | `1` 台（退慢路径可抗 `2` 台） | `1` 台（若 \(q_1=3\) 则快写需 `4` 台） |

看第二行 \(N = 5, q_2 = 2, q_1 = 4\) 的非对称配置：主节点在稳态下成千上万次执行的都是 Phase 2 日志复制，第一阶段选主只在主挂掉时偶发执行一次。把 \(q_2\) 压到 `2` 后，主节点只需和同机架或同城可用区的 1 台从节点握手就能返回客户端，直接砍掉了跨大洲 RTT，代价仅仅是万一主节点自身宕机，重选新主时必须凑齐 4 台存活节点。

#### 第二重松绑（Revision B & C）：纪元定向交集与排除“不可能旧多数派”的 `V_dec`（PDF pp. 81–105）

顺着时间箭头再往前推一步：纪元 \(e\) 的第一阶段甚至不需要跟未来更高纪元的 \(Q_2\) 相交，它**只需要与所有严格小于它的历史纪元 \(f < e\) 的第二阶段集合相交**：

> "We require only that a phase one quorum of the epoch e (Qe1) intersects with the phase two quorums (Qf2) for all smaller epochs, {f ∈ E|f < e}" *(PDF p. 81)*

这意味着最小初始纪元 \(e_{\min}\) 左侧空无一人（\(\{f \mid f < e_{\min}\} = \emptyset\)），若该纪元已归属唯一默认主节点，它天生就能合法跳过第一阶段！更妙的是中间历史复用（Revision C，PDF pp. 92–94）：一旦新提议者在纪元 \(e\) 的第一阶段收到了某个节点汇报的已接受记录 `(f, v)`，由于提议者 \(f\) 当年在提出 `v` 前已经替我们检查并封存了所有 `< f` 的更老纪元，当前提议者需要亲自确保交集覆盖的区间瞬间从 \([0, e)\) 收窄为 \((f_{\max}, e)\)！

紧接着在 PDF p. 105，Howard 给出了一个颠覆“Paxos 第一阶段必须机械采纳最高已接受纪元值（Highest-Epoch Rule）”的精妙 5 节点算例。设 5 个 Acceptor \(\{A_1, A_2, A_3, A_4, A_5\}\) 采用常规 3 节点多数派（\(q_2 = 3\)），纪元 `4` 的提议者在第一阶段收到了前 4 个节点的 Promise 回执（\(A_5\) 网络超时未回）：

| 节点 | 承诺纪元 `e_pro` | 汇报已接受 `(e_acc, val)` | 对各历史纪元可能投票情况的反推证据 |
| :---: | :---: | :---: | :--- |
| `A1` | `4` | `(3, b)` | 在纪元 `3` 接受了 `b`；**同时在被 `(3, b)` 覆盖前，它也可能曾在纪元 `1` 或 `2` 投过票！** |
| `A2` | `4` | `(2, a)` | 在纪元 `2` 接受了 `a`；因 `e_acc = 2 < 3`，**铁定从未在纪元 `3` 投过赞成票！** |
| `A3` | `4` | `(1, a)` | 在纪元 `1` 接受了 `a`；因 `e_acc = 1 < 2`，**铁定从未在纪元 `2` 或 `3` 投过赞成票！** |
| `A4` | `4` | `nil` | **在纪元 `1, 2, 3` 从未接受过任何提案！** |
| `A5` | （未响应） | `?` | 未知，最坏保守假设它在纪元 `1, 2, 3` 全都投过赞成票 |

按照经典 Paxos 的最高纪元规则，提议者看到 `A1` 汇报了最高纪元 `(3, b)`，必须无脑重提旧值 **`b`**。但让我们用集合论逐个纪元验算“当年到底哪个值真正有可能凑够过 3 票”——对每个历史纪元 \(f \in \{1, 2, 3\}\)，统计所有**可能曾在纪元 \(f\) 接受过提案**的节点集合（即汇报等于 \(f\)、汇报大于 \(f\) 可能覆盖了旧记录、或未响应的节点）：

- **检验纪元 `3` 的值 `b`**：可能投过赞成票的节点只有 **`{A1, A5}`**（共 2 台 < 3 台，因为 `A2, A3, A4` 均已作证自己在纪元 `3` 未接受任何值）——**`b` 绝对不可能在纪元 `3` 达成过决议**！
- **检验纪元 `2` 的值 `a`**：可能投过赞成票的节点包含 `A2`（现存 `(2, a)`）、`A1`（现存 `(3, b)`，可能覆盖了当年的 `(2, a)`）以及沉默的 `A5`，恰好拼成 **`{A1, A2, A5}`** 满员 3 台多数派——因此 **`a` 才是潜在可能已决的候选值（\(V_{\text{dec}} = \{a\}\)）**！
- 且慢——为什么经典单主 Paxos 当年选 `b` 也没出安全事故？因为在经典规则下，纪元 `3` 的提议者当年若发现 `a` 已决，它自己就只会被迫提 `a` 而写不出 `b`。但一旦我们进入多提议者共享纪元或允许跳过冗余约束的泛化协议，盲选最高纪元的 `b` 就会当场酿成覆写事故！反过来，倘若上例中 `A2` 汇报的也是 `nil`，那么连纪元 `2` 和 `1` 也凑不够 3 台，**\(V_{\text{dec}} = \emptyset\)**——纪元 `4` 的提议者立刻获得完全自由，直接写入客户端新请求而无需替失败的前任搬运废案 `b`！

#### 第三重松绑：共享纪元与 1-RTT 冲突恢复三元交集（PDF pp. 122–138）

既然初始纪元可以跳过 Phase 1，能不能干脆让所有客户端共享同一个快速纪元 \(f\)，不经主节点转发、直接 1 RTT 把各自的值并发写向 Acceptor？一旦放开同纪元并发直写，\(A_1, A_2\) 可能收到客户端甲的 `a`，而 \(A_3, A_4\) 收到客户端乙的 `b`。当后续恢复纪元 \(e > f\) 介入收拾残局时，普通二元交集 \(Q_1(e) \cap Q_2(f) \neq \emptyset\) 不够用了——因为恢复者必须分辨到底是 `a` 还是 `b` 有可能凑齐了快速法定人数！论文证明充要条件升级为**三元交集（Triple Intersection）**：

$$\forall Q_1 \in \mathcal{Q}_1(e),\; \forall Q_{2a}, Q_{2b} \in \mathcal{Q}_2(f):\quad Q_1 \cap Q_{2a} \cap Q_{2b} \neq \emptyset$$

在对称计数场景下，若恢复读取集大小为 \(q_1\)、快速写入集大小为 \(q_2\)，用一次极简的容斥原理（鸽巢下界）即可直接推导出节点数不等式：

$$|Q_1 \cap Q_{2a} \cap Q_{2b}| \;\ge\; |Q_1| + |Q_{2a}| + |Q_{2b}| - 2N \;=\; q_1 + 2q_2 - 2N$$

要令交集至少包含 **1 个公共见证节点**，充要计数条件即为：

$$q_1 + 2q_2 > 2N \quad\Longleftrightarrow\quad q_2 > N - \frac{q_1}{2}$$

代入两组经典工程参数立即验明正身：
- 若系统要求容忍 \(f\) 台机器永久宕机后仍能完成恢复选主，则恢复集至多只能取 \(q_1 = N - f\)；代入上式得 \(q_2 > N - \frac{N - f}{2} = \frac{N + f}{2}\)。再结合 \(N \ge 2f + 1\)（或最紧容错比 \(N = 3f + 1\) 时 \(q_1 = 2f + 1, q_2 = 2f + 1\)），当取经典多数派恢复 \(q_1 = \lfloor N/2 \rfloor + 1\) 时，快速写入集下界便落在著名的 **\(\lceil 3N/4 \rceil\)** 附近；
- 若读写两阶段采用相同规模 \(q_1 = q_2 = k\)，则 \(3k > 2N \implies k = \lfloor 2N/3 \rfloor + 1\)（如 \(N = 5\) 时快写与恢复均取 `4` 台节点）。此外，论文还在第 3.3 节指出了一项实用的**第二阶段旁路优化（Phase-Two Bypass，PDF p. 55）**：如果提议者在 Phase 1 意外发现已有足够多节点在同一个历史纪元接受了同一个值 `v`（证明 `v` 事实上早已达成决议），提议者甚至无需再跑一遍 Phase 2 广播，可直接宣布 `v` 已决！

### Howard & Mortier（2019）与广义共识双翼：单次写寄存器公理 vs 可交换命令图 {#paper-30}

- **论文来源**：Heidi Howard, Richard Mortier, *A Generalised Solution to Distributed Consensus*, arXiv:1902.06776, 2019 · [原始 PDF（共 20 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/consensus/a-generalised-solution-to-distributed-consensus.pdf)（持久单次写寄存器与四条正确性规则见 `Figure 1–3` PDF p. 3；客户端状态判定表见 `Figure 5` PDF p. 5；快速冲突与提前终止算例见 `Figure 13–14` PDF pp. 9–10；主备直写零停顿重配见第 6 节 PDF pp. 10–11）。

如果把 `#32` 的剥茧抽丝推向极简主义终点，能不能连“Proposer、Acceptor、Promise、Accept”这些延续了三十年的术语也一并扔掉？Howard 与 Mortier 在这篇 20 页浓缩之作中给出了令人耳目一新的统一模型：**分布式共识无非是一组客户端在若干台服务器的无限单次写持久寄存器数组 \(\{R_0, R_1, R_2, \dots\}\) 上按四条铁律读写填表的过程！**

> "each has a infinite series of write-once, persistent registers, {R0, R1, . . . }. Clients read and write registers on servers" *(PDF p. 2)*

每台服务器的第 \(i\) 号寄存器 \(R_i\) 一生只能从 `Unwritten` 单向跃迁一次，要么被封死为特殊哨兵值 `Written(nil)`（等价于“我承诺废掉第 \(i\) 行”），要么被写入某个业务提案值 `Written(v)`。所有同名编号 \(i\) 横跨各服务器的寄存器组成第 \(i\) 个**寄存器集（Register Set）**，配备各自的法定人数簇 \(\mathcal{Q}(i)\)，并分为两类模式：**客户端独占型（Client-restricted，仅允许单一指定客户端写非空值）**与**全员开放型（Quorum-intersecting，允许任意客户端并发直写 1-RTT 快速值）**。

整个共识宇宙的安全性，被彻底收敛进 `Figure 3`（PDF p. 3）的四条写规则与 `Figure 5`（PDF p. 5）的四态判定表中：
1. **Rule 1（单次写持久性）**：任何已写寄存器绝不可覆写篡改（若已写其它值则返回当前已存值拒绝修改）；
2. **Rule 2（独占写资格）**：向独占型寄存器集写非空值 `v` 前，必须是被分配该编号的唯一合法客户端；
3. **Rule 3（同集值唯一性）**：在独占型集内天然满足；在开放型快速集内，客户端直接盲写自己的请求值，交由更强的三元交集规则兜底；
4. **Rule 4（跨前缀历史继承——最核心公理）**：任何客户端想要往第 \(i\) 号寄存器集写入非空值 `v`（开放型盲写除外），**必须先用 `Written(nil)` 封死从 `0` 到 `i-1` 每一行的至少一个有效法定人数（阻止旧行再凑齐新决议），并逐行对照客户端判定表检验历史**。

把服务端单次写寄存器接口与客户端历史回溯算法写成精简伪代码（对照 `Figure 3 & 5`，PDF pp. 3–5），整套广义共识内核不过二十行：

```python
# 服务端原语：每个槽位 r 仅允许从 Unwritten 单向写入一次（持久化落盘）
def server_write_once(r: int, val_or_nil) -> Value:
    if R[r] == Unwritten:
        R[r] = Written(val_or_nil)  # Rule 1: 一旦写入永不更改
        persist(R[r])
    return R[r]  # 返回该寄存器最终锁定的真实值（可能是先前别人写入的值）

# 客户端在独占寄存器集 i 提案新值 client_val 前的历史封锁与判定（Rule 4）
def client_propose_at_row(i: int, client_val):
    # 1. 向前缀 0 .. i-1 每一行的读取封锁集 Q1(i) 广播 server_write_once(r, nil)
    prefix_views = seal_and_read_prefix(rows=range(0, i), quorum=Q1(i))
    # 2. 自顶向下（从 i-1 倒查回 0）对照判定表寻找可能已决的历史值
    chosen = client_val
    for r in reversed(range(0, i)):
        status = evaluate_decision_table(r, prefix_views[r], Q2_family=Q2(r))
        if status == Decided(v):
            return v  # 历史行已达成完整法定人数决议，直接返回结果！
        if status == Maybe(v):
            chosen = v  # 发现最早需要保护的潜在决议，强制继承！
            break       # 更高行的 Maybe 已包含更早行的继承链，无需继续下探
    # 3. 安全向第 i 行的写入法定人数 Q2(i) 写入最终选定的 chosen
    return write_quorum(row=i, val=chosen, quorum=Q2(i))
```

对照论文 `Figure 13–14`（PDF pp. 9–10），在一个 4 服务器 \(\{S_0, S_1, S_2, S_3\}\) 集群上（设开放型快速行 \(R_0\) 的法定人数大小为 `3`，独占恢复行 \(R_1\) 的读取封锁集大小为 `3`，满足三元交集 \(3 + 3 + 3 > 2 \times 4\)），观察客户端如何仅凭填表规则优雅化解快速路径冲突与慢节点阻塞：

| 场景切片 | `S0` 的 `R0` | `S1` 的 `R0` | `S2` 的 `R0` | `S3` 的 `R0` | 恢复客户端写 `nil` 探测结果与 `Figure 5` 判定 |
| :--- | :---: | :---: | :---: | :---: | :--- |
| **场景 A：提前判定空行 (`Fig. 13`)** | `Written(a)` | **`Written(nil)`** | **`Written(nil)`** | （慢节点未回） | 无需等 `S3`！因已有 `2` 个 `nil`，存活非 `nil` 节点至多剩 `{S0, S3}` 共 2 台 < 3 台，当场判定 **`None`**，自由提案新值！ |
| **场景 B：并发双写冲突 (`Fig. 14`)** | `Written(a)` | `Written(a)` | `Written(b)` | （慢节点未回） | 封锁集 `{S0, S1, S2}` 看到两个 `a`、一个 `b`：即使 `S3` 写了 `b` 也只有 `{S2, S3}` 2 台 < 3 台；而若 `S3` 写了 `a` 则 `{S0, S1, S3}` 已满 3 台！唯一判定 **`Maybe a`**，必须在 `R1` 继承写 `a`！ |

站在这套寄存器矩阵上俯瞰，过往各自为政的共识变种瞬间退化为配置表里的几行参数（PDF pp. 7–11）：

> "quorum intersection is required only between the phase one quorum for register set r and the phase two quorums of register sets 0 to r − 1." *(PDF p. 8)*

更令人拍案叫绝的是第 6 节展示的**零停顿主备重配（Reconfigurable Primary-Backup，PDF pp. 10–11）**。在传统教科书里，“双机全同步主备复制（写满 `{S0, S1}` 才算提交）”和“三节点多数派 Paxos”仿佛是两个物种，前者一旦主库 `S0` 宕机就得搬出外部协调服务（如 ZooKeeper）做故障切换。而在单次写寄存器矩阵里，只需预编排上下两段异构寄存器集就能原生自举切换：

| 寄存器集区段 | 独占写入客户端 | 写入法定人数 \(\mathcal{Q}_2(r)\) | 后续行封锁集 \(\mathcal{Q}_1\) 对本行的交集要求 | 实际运行物理表现 |
| :--- | :---: | :---: | :--- | :--- |
| **`r = 0 .. k-1`（日常主备段）** | 主节点 `S0` | **`{{S0, S1}}`**（双全集） | 只需命中 `S0` **或** `S1` 任意 1 台即可封锁！ | `S0` 直接 1 RTT 写 `{S0, S1}`，闲置冷备 `S2` 零负载旁观 |
| **`r = k`（`S0` 故障接管行）** | 备节点 `S1` | **`{{S1, S2}}`**（切换新双集） | `S1` 仅读**自己本地磁盘**的 `0..k-1` 行写 `nil`！ | 因 `{S1} ∩ {S0, S1} = {S1} ≠ ∅`，**`S1` 零网络读即满足 Rule 4**，把未决状态安全搬入 `{S1, S2}`！ |
| **`r >= k + 1`（新稳态或多数派）** | 新主 / 任意节点 | `{{S1, S2}}` 或三节点多数派 | 与前序所有行的 \(\mathcal{Q}_2\) 逐行保持非空交集 | 彻底废除旧主 `S0` 写权限，传统主备复制与 Paxos 共识在同一张数学表格里融为一体！ |

在结束理论泛化部分之前，我们还要补齐“Generalized Consensus”在多命令状态机维度上的另一只著名翅膀——**Leslie Lamport（2005）提出的命令历史结构（`c-structs`）以及后继的 Egalitarian Paxos（EPaxos）**（在本文下一节 `#31` Ongaro 博士论文第 11.5 节 PDF p. 184 亦有深度对比）。把这两个正交的泛化维度并列对照，便能看清现代低延迟广域共识的全部秘密：

| 泛化维度 | 单槽位寄存器与 Quorum 几何泛化 (`#32` / `#30`) | 多命令交换律与偏序图泛化 (Lamport `c-structs` / EPaxos) |
| :--- | :--- | :--- |
| **消除的冗余税** | 消除同阶段 Quorum 相交冗余与无冲突时的 Phase 1 选主 RTT | 消除**互不干扰命令**被迫排在单主后面串行领号的单点与跨洲转发瓶颈 |
| **核心代数工具** | 单次写持久寄存器集 + 跨阶段二元/三元集合交集 | 命令等价商集（若 \(C_1 \circ C_2 \equiv C_2 \circ C_1\)，则两顺序映射为同一个抽象 `c-struct`） |
| **1-RTT 快路径条件** | 客户端并发直写开放型寄存器集，无并发碰撞时 1 RTT 落盘 | 并发客户端各自修改不相交键（如 `k1` 与 `k2`），即使乱序抵达不同副本也因**可交换**而零冲突 1 RTT 提交 |
| **冲突恢复慢路径** | 同集撞车写出 \(v_a \neq v_b\) 时，升入更高寄存器集靠三元交集仲裁 | 仅当并发命令真正读写同一键（不可交换）时，才触发协调轮敲定二者偏序依赖边 |

两条路线殊途同归：凡是不影响状态机外部观测一致性的顺序分歧与节点等待，统统可以从稳态关键路径上剥离出去。值得特别品味的是，“命令是否可交换”不仅取决于访问的键是否相交，还取决于**操作本身的代数语义**——例如在同一行库存计数器 `x = 10` 上：
- 若两个客户端并发执行盲覆盖 `SET(x, 5)` 与 `SET(x, 3)`，先 `5` 后 `3` 结果是 `3`，先 `3` 后 `5` 结果是 `5`（\(C_1 \circ C_2 \neq C_2 \circ C_1\)，不可交换），快速通道撞车后必须走慢路径排定全序；
- 但若两个客户端发来的是无返回值增量扣减 `ADD(x, -2)` 与 `ADD(x, -3)`，无论某副本先算 `-2` 再算 `-3` 还是反过来，最终库存都是 `5`（满足代数交换律 \(C_1 \circ C_2 \equiv C_2 \circ C_1\)）！在 `c-struct` 广义共识下，即便聚焦同一热点键，纯可交换算子依然能免除单主排序、直接走 1-RTT 快速通道收敛。

---

## 强领导者范式与生产深水区：Raft 安全反例、博士论文扩展与 Ark 拉式复制

理论泛化把协议的自由度放到了最大，但在工程实现的一线战场上，过大的自由度往往是幽灵 Bug 的温床：Multi-Paxos 允许不同日志槽位并发乱序达成决议，日志中间随时可能出现空洞（Log Holes），选出新主后还得逐槽补洞、小心翼翼防止状态机越过空洞乱序回放。2014 年问世的 **Raft** 与工业界同期演进的 **Ark** 选择了一条截然相反的哲学：**主动收紧协议不变量，把一切复杂性关进单向流动的强领导者管道与清晰的任期栅栏里。**

![Raft 提交与成员变更安全边界及 Ark 拉式复制确认栅栏](/images/diagrams/paper-cons-raft-fig8-and-membership.svg)

### Ongaro & Ousterhout（2014）：Raft 强领导者解耦与 Figure 8 幽灵覆写反例 {#paper-33}

- **论文来源**：Diego Ongaro, John Ousterhout, *In Search of an Understandable Consensus Algorithm (Extended Version)*, USENIX ATC 2014 扩展技术报告 · [原始 PDF（共 18 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/consensus/in-search-of-an-understandable-consensus-algorithm-%28extended-version%29.pdf)（浓缩协议全集见 `Figure 2` PDF p. 4；旧任期多数派回滚反例见 `Figure 8` PDF pp. 8–9；联合共识成员变更见 `Figure 10–11` PDF p. 11；快照与随机化选举超时压测见 `Figure 12–16` PDF pp. 12–15）。

Ongaro 与 Ousterhout 为 Raft 立下的头号架构公理是**强领导者（Strong Leader）单向数据流**：日志条目永远只能从 Leader 流向 Follower，Leader 绝不覆盖或删除自己已有的日志，Follower 的冲突后缀则无条件被 Leader 截断覆写。

但这一句话立刻引出一个致命质问：既然新当选的 Leader 绝不向 Follower 拉取补齐缺失日志（省掉了 Paxos 复杂的历史合并），**凭什么保证新选出来的 Leader 手里一定已经攥着之前所有已提交的日志条目（Leader Completeness）？**

Raft 把这道安检关直接前置到了拉票阶段（`RequestVote` RPC，PDF p. 8）：投票者一旦发现候选人的日志没有自己“新（up-to-date）”，当场投反对票！而评判两份日志谁更新的标尺，是**先比最后一条日志的任期号，任期相同再比日志总长度**的字典序：

> "If the logs have last entries with different terms, then the log with the later term is more up-to-date. If the logs end with the same term, then whichever log is longer is more up-to-date." *(PDF p. 8)*

初看之下，既然已提交日志已经写到了多数派节点上，而候选人又必须拿到多数派选票，交集里的那个公共节点似乎总能拦住落后的候选人。然而在 `Figure 8`（PDF pp. 8–9）中，作者构造出了分布式系统史上最著名的一幕五节点交错反例，证明**如果新主沿用直观思维“只要看到旧任期日志凑够了 3/5 多数派就标记提交”，已提交数据依然会被合法抹杀**！

让我们在 5 台服务器 `{S1, S2, S3, S4, S5}` 上逐帧回放这场惊心动魄的消息时序（假定索引 `1` 处全员均已有 `term 1` 的初始日志）：

| 时刻与动作切片 | `S1` 日志 | `S2` 日志 | `S3` 日志 | `S4` 日志 | `S5` 日志 | 关键因果与危险信号剖析 |
| :--- | :---: | :---: | :---: | :---: | :---: | :--- |
| **`(a)` `S1` 任 `term 2` 主** | `[1, 2]` | `[1, 2]` | `[1]` | `[1]` | `[1]` | `S1` 将 `(idx=2, term=2)` 复制给 `S2` 后断联（仅 `2/5` 副本） |
| **`(b)` `S5` 赢 `term 3` 主** | `[1, 2]` | `[1, 2]` | `[1]` | `[1]` | **`[1, 3]`** | `S5` 获 `{S3, S4, S5}` 3 票当选（尾日志同为 `[1]`），本地写 `(idx=2, term=3)` 后立刻断联 |
| **`(c)` `S1` 重启赢 `term 4` 主** | `[1, 2]` | `[1, 2]` | **`[1, 2]`** | `[1]` | `[1, 3]` | `S1` 凭尾任期 `2 > 1` 获 `{S1, S2, S3, S4}` 选票当选，接着把旧项 `(idx=2, term=2)` 补推给 `S3`！ |
| **`(d)` 致命陷阱分支** | 崩溃 | **`[1, 3]`** | **`[1, 3]`** | **`[1, 3]`** | **`[1, 3]`** | 在 `(c)` 末尾 `(idx=2, term=2)` **已占满 `{S1, S2, S3}` 过半多数派**！若此时算作已提交且 `S1` 崩溃，`S5` 随后在 `term 5` 拉票：因 `S5` 尾任期 **`3 > 2`**，`S2` 与 `S3` **必须合法投票给 `S5`**！`S5` 上台将全网 `idx=2` 覆写为 `term 3`，已提交数据灰飞烟灭！ |
| **`(e)` Raft 正确锚定分支** | `[1, 2, 4]` | `[1, 2, 4]` | `[1, 2, 4]` | `[1]` | `[1, 3]` | `S1` 在 `term 4` 绝不直接提交旧项 `2`，而是追加一条当前任期新项 `(idx=3, term=4)` 并复制到 `{S1, S2, S3}`；此刻三节点尾任期升至 **`4 > 3`**，`S5` 彻底丧失当选资格，旧项 `2` 被顺带永久锁死！ |

看清 `(c) -> (d)` 那一步的杀机了吗？`S3` 虽然存下了 `(idx=2, term=2)`，但它的结尾任期 `2` 依然敌不过 `S5` 手里那条从未复制出去的孤本 `(idx=2, term=3)`！字典序新鲜度比较偏袒更高任期，使得“旧任期条目占领空间多数派”不再等价于“锁定未来选票多数派”（阅读原文 p. 9 时顺带留意一处排版小笔误：正文第 9 行写成了 `as in 8(d)`，实际指代的是正确提交分支 `8(e)`）。为此，Raft 立下了铁板一块的提交定律：

> "Only log entries from the leader’s current term are committed by counting replicas; once an entry from the current term has been committed in this way, then all prior entries are committed indirectly because of the Log Matching Property." *(PDF p. 9)*

为什么加上“仅对当前任期计数提交”这一条限制后，后续任意更高任期的新主就绝对不可能丢失已提交日志？论文在 `Figure 9`（PDF p. 9）给出了一段极其优美的**最小反例反证法**：

1. 假设任期 \(T\) 的领导者 \(\text{leader}_T\) 提交了一条当前任期日志 \(e\)，但后来某个更高任期的领导者手里竟然没有 \(e\)；取其中**任期编号最小**的那位失忆领导者，记为 \(\text{leader}_U\)（\(U > T\)）。
2. 由于 \(e\) 在任期 \(T\) 提交时已复制到某个多数派，而 \(\text{leader}_U\) 当选时又拿到了某个多数派选票，两个多数派的交集中必然存在至少一个公共选民节点 **`voter`**——且由于在 \(\text{leader}_U\) 上台前没有任何领导者缺过 \(e\)，`voter` 在给 \(\text{leader}_U\) 投赞成票之前**必定已经收存了日志 \(e\)**（否则它后来也无处收得，违背其在任期 \(T\) 复制多数派内的身份）。
3. 既然 `voter` 投票时手里已经有 `(index(e), term = T)`，而它居然把赞成票投给了不含 \(e\) 的 \(\text{leader}_U\)，根据选举新鲜度字典序规则，\(\text{leader}_U\) 的最后一条日志要么**结尾任期严格大于 `voter` 的结尾任期**（即 \(\ge T + 1 > T\)），要么**结尾任期等于 `voter` 的结尾任期且长度不短于 `voter`**！
4. 先看第二种可能：若结尾任期同为 \(T\) 且长度不短于含 \(e\) 的 `voter`，根据日志匹配性质（Log Matching Property），\(\text{leader}_U\) 的日志里必然早已一字不差地装着 \(e\)，矛盾！再看第一种可能：若 \(\text{leader}_U\) 有一条任期 \(T' \in (T, U)\) 的尾日志，那么制造这条日志的前任领导者 \(\text{leader}_{T'}\)（因 \(T' < U\)，按最小反例假设 \(\text{leader}_{T'}\) 必定持有 \(e\)！）在追加该日志时早就把前缀里的 \(e\) 一并带给了 \(\text{leader}_U\)，再次矛盾！证毕。

对照论文 `Figure 2`（PDF p. 4）浓缩的两页核心状态机规约，工程实现 Raft 时最容易写出高危线上 Bug 的地方恰恰集中在以下两处护栏条件上：

```go
// 护栏 1（Follower 处理 AppendEntries 第 3 步）：仅在同位置任期冲突时截断！
// 严禁写成“无条件截断 prevLogIndex 之后的全部本地日志”——否则乱序迟到的旧心跳包
// 会把 Follower 已经收妥甚至已提交的更长日志后缀当场误删！
for i, entry := range req.Entries {
    idx := req.PrevLogIndex + 1 + i
    if idx <= len(log) && log[idx].Term != entry.Term {
        log = log[:idx] // 发现真实同下标异任期冲突，才截断冲突后缀
    }
    if idx > len(log) {
        log = append(log, entry)
    }
}

// 护栏 2（Leader 推进 commitIndex 规则）：必须严验 log[N].Term == currentTerm！
// 漏掉右边这个等号，就会在生产环境一模一样地复现 Figure 8(d) 的已提交数据丢失事故！
for N := len(log); N > commitIndex; N-- {
    if countReplicated(N) > len(peers)/2 && log[N].Term == currentTerm {
        commitIndex = N // 借日志匹配性质一次性间接封存 1..N 全部前缀
        break
    }
}
```

在教学可理解性对照实验（43 名高年级学生同堂测试，Raft 平均得分 `25.7` 显著高于 Paxos 的 `20.8`，`Figure 14` PDF p. 14）之外，论文第 5.6 节（PDF p. 10）还给出了保障系统活性的定量时序不等式——虽然 Raft 的**安全性**完全不依赖物理时间，但要在生产网络里选得出主、稳得住主，广播往返耗时 `broadcastTime`、选举超时窗口 `electionTimeout` 与单机平均无故障时间 `MTBF` 必须拉开至少一个数量级的阶梯：

$$\text{broadcastTime} \;\ll\; \text{electionTimeout} \;\ll\; \text{MTBF}$$

典型数据中心内 `broadcastTime` 约为 `0.5–20 ms`，将 `electionTimeout` 设在 **`150–300 ms`** 均匀随机区间内，既保证正常心跳绝不会误触发无谓重选，又在 `Figure 16`（PDF p. 15）的 5 节点宕机压测中证明：不加随机抖动时选票平分活锁超过 `10 s` 无法收敛，而仅注入 `12–24 ms` 随机宽度即可把平均故障接管耗时压至 **`35 ms`**，让 Raft 迅速成为现代云原生基础设施的事实标准。

### Ongaro 博士论文（2014）：单步成员变更、ReadIndex 线性化读与 TLA+ 证明边界 {#paper-31}

- **论文来源**：Diego Ongaro, *Consensus: Bridging Theory and Practice*, Stanford University PhD Dissertation, Aug 2014 · [原始 PDF（共 258 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/consensus/consensus%3A-bridging-theory-and-practice.pdf)（时间与安全性分离原则见第 3 章 PDF p. 45；单节点成员变更与 `Pre-Vote` 扰动防御见第 4 章及第 9.6 节 PDF pp. 51–62, 155；日志快照与数组越界教训见第 5 章 PDF pp. 69–75；`ReadIndex` 线性化读与会话语义见第 6 章 PDF pp. 87–92；TLA+ 规约证明边界见第 8 章 PDF p. 134；与 Multi-Paxos \(\alpha\) 窗口及 EPaxos 对比见第 11 章 PDF pp. 175, 184）。

18 页的会议论文让全世界读懂了 Raft 的主干，而真正决定工业级实现（如 etcd、TiKV、Consul、LogCabin）生死的魔鬼细节，全都藏在 Ongaro 这部 258 页的博士论文里。全书开宗明义确立了贯穿每一章的铁律——物理时钟与超时只能影响快慢，绝不能触碰对错：

> "One of our requirements for Raft is that safety must not depend on timing: the system must not produce incorrect results just because some event happens more quickly or slowly than expected. However, availability (the ability of the system to respond to clients in a timely manner) must inevitably depend on timing." *(PDF p. 45)*

围绕这条铁律，博士论文补齐了四个在生产落地中绕不开的深水区模块：

#### 1. 在线成员变更脑裂陷阱、单步增删与 `Pre-Vote` 孤岛免疫（第 4 章 & 第 9.6 节）

为什么集群不能直接发一条广播把 3 节点配置 \(C_{\text{old}} = \{S_1, S_2, S_3\}\) 一步切换成 5 节点配置 \(C_{\text{new}} = \{S_1..S_5\}\)？因为消息不可能在所有机器上同一纳秒生效！设某一瞬间 \(S_1, S_2\) 还在跑 \(C_{\text{old}}\)，而 \(S_3, S_4, S_5\) 已经收到了 \(C_{\text{new}}\)：此时 \(S_1\) 只需拿到 \(\{S_1, S_2\}\)（`2/3` 旧多数派）就能当选旧主，而同一毫秒 \(S_3\) 拿到 \(\{S_3, S_4, S_5\}\)（`3/5` 新多数派）也能合法当选新主——**双主脑裂当场引爆**！

对比会议版的双阶段联合共识（\(C_{\text{old,new}}\) 过渡态同时要求两套配置各自过半）与博士论文第 4.1 节主力推崇的**单节点变更（Single-Server Membership Changes）**：
- 若规定每次配置变更**至多只增加或删除 1 台机器**（例如先走 \(3 \to 4\) 提交，再走 \(4 \to 5\) 提交），那么相邻两代配置的多数派在算术上必然相交（例如 \(N=3\) 的多数派 `2` 与 \(N=4\) 的多数派 `3` 之和为 \(5 > 4\)；\(N=4\) 的多数派 `3` 与 \(N=5\) 的多数派 `3` 之和为 \(6 > 5\)），根本无需引入复杂的双多数派联合中间态！唯一铁律是：**上一条成员变更日志尚未正式提交前，Leader 绝不允许开启下一条成员变更。**
- 与此同时，被移出集群的旧节点（或单向断网的孤岛节点）因收不到心跳会反复超时自增 `currentTerm`，重连时一封高任期拉票就会把健康真主打回 Follower（`Figure 4.7` PDF p. 61）。论文第 4.2.3 节与第 9.6 节（PDF p. 155）指出，必须双管齐下：**（a）最小选举超时租约屏蔽**：Follower 只要在最近一个最小选举超时窗口内刚收到过合法 Leader 心跳，直接丢弃任何 `RequestVote` 不涨任期；**（b）`Pre-Vote` 预投票阶段**：候选人在真正自增持久 `currentTerm` 前先发试探请求，确认自己能从过半节点拿到准许票后再正式升任期参选。

#### 2. 免写盘强一致线性化读 `ReadIndex` 与客户端会话幂等（第 6 章 pp. 87–92）

如果每次 `GET` 查询也塞进 Raft 日志走一轮多数派落盘，读吞吐会惨遭腰斩；可如果让 Leader 不问青红皂白直接读本地内存返回，万一该 Leader 刚刚被网络分区隔离、外界早已选出了新主并改写了数据，客户端就会读到违背线性一致性的过期旧值！不靠有晶振漂移风险的物理时钟租约，怎样零写盘完成严格线性化读？论文给出了工业界全面采纳的四步 **`ReadIndex` 协议**（PDF p. 90）：
1. **新主 `no-op` 锚定检查**：Leader 必须已经在**当前任期**至少提交过一条日志（为此 Leader 当选后第一件事就是立即写入一条空白 `no-op` 日志！因为在 `no-op` 提交前，受制于 `Figure 8` 规则，新主甚至无法确知全局最新的 `commitIndex` 到底推进到了哪里）；
2. **锁定读水位**：将此刻的 `commitIndex` 记为本次读批次的 `readIndex`；
3. **纯内存心跳验主**：向全集群发一轮不写磁盘的轻量心跳（多个并发读请求可共享同一轮心跳摊薄开销），集齐过半数节点确认“在记录 `readIndex` 之后这一刻，尚未有任何更高任期新主产生”；
4. **等待状态机追平**：待本地状态机 `lastApplied >= readIndex` 后直接读内存返回！配合随快照持久化的客户端序列号去重表（Session Table），重试写请求同样不会发生二次重复扣款。

把 `ReadIndex` 与第 5 章 `InstallSnapshot` 最容易写错的边界条件落成核心伪代码，可以清晰看到工业级实现如何严防陈旧主脏读与乱序快照误删日志：

```go
// Leader 处理线性化只读查询（免写 Raft 磁盘日志，支持成百上千并发读共享单轮心跳）
func (r *Raft) LinearizableRead(ctx Context, key Key) (Value, error) {
    if !r.hasCommittedEntryInCurrentTerm() {
        r.waitUntilInitialNoOpCommitted(ctx) // 必须等上任首条 no-op 锚定最新 commitIndex
    }
    readIdx := r.commitIndex
    // 发起纯内存轻量心跳，证明记录 readIdx 这一刻之后本节点依然握有多数派领导权
    if !r.broadcastHeartbeatQuorumAck(ctx) {
        return nil, ErrNotLeader
    }
    r.stateMachine.WaitUntilApplied(readIdx) // 等本地状态机追至 >= readIdx
    return r.stateMachine.Get(key), nil
}

// Follower 接收 InstallSnapshot 护栏（博士论文第 5.1 节 PDF p. 71）
func (r *Raft) HandleInstallSnapshot(snap Snapshot) {
    if snap.Term < r.currentTerm || snap.LastIndex <= r.commitIndex {
        return // 忽略网络中乱序迟到的过期快照！
    }
    // 关键护栏：若本地在快照边界处拥有同任期日志且后缀更长，必须保留后续日志条目！
    if r.log.HasEntry(snap.LastIndex, snap.LastTerm) {
        r.log.TruncatePrefixUpTo(snap.LastIndex) // 仅裁剪已入快照的前缀，绝不清空后缀
    } else {
        r.log.ResetEmptyAt(snap.LastIndex, snap.LastTerm)
        r.stateMachine.RestoreFrom(snap.Data)
    }
}
```

在端到端线性化闭环中，第 6.3 节（PDF pp. 88–89）还指出了一个常被业务研发忽略的内存边界：状态机里的客户端会话去重表（Session Table）不可能无限膨胀，必须按容量或超时淘汰久不活跃的客户端条目。一旦某个失联许久的客户端重发旧序列号写请求时发现其 Session 已被驱逐：
- 系统**绝不能静默当作新请求再执行一遍**（否则非幂等转账会被扣款两次）；
- 必须向客户端明确返回确定性的 **`SESSION_EXPIRED` 错误**，把不确定性诚实地交还上层业务做对账恢复，而幂等只读请求则完全无需登记 Session 槽位。

#### 3. 并行刷盘网络流水线、独立快照陷阱与形式化证明的诚实边界（第 5、8、10、11 章）

在工程吞吐优化上，博士论文第 10 章（PDF p. 162）拆解了参考系统 LogCabin 如何把单轮复制延迟压到极致：Leader 收到客户端请求后，**并不傻等自己本地磁盘 `fsync` 完成才发网络 RPC**，而是将“Leader 本地异步落盘”与“向全体 Follower 并发推送 `AppendEntries` 并在远端落盘”两条高延迟路径完全并行重叠，配合请求批处理（Batching）与乱序流水线（Pipelining，动态维护每个 Follower 的 `nextIndex` 与单调确认水位 `matchIndex`），在 3 节点万兆网、100 并发线程写入 1 KB 数据负载下跑出了约 **19,500 ops/s** 的饱和吞吐。

在日志压缩快照（第 5 章 pp. 69–75）中，每个节点独立生成携带 `(lastIncludedIndex, lastIncludedTerm, 最新成员配置, 客户端会话表)` 的快照文件；作者专门记录了早期 LogCabin 实现因隐式假设日志数组从下标 `1` 连续驻留内存而在快照截断后频发越界崩溃的工程教训（PDF p. 74），并在第 11 章点破了 Multi-Paxos 并发窗口参数 \(\alpha\) 存入状态机自身时因前序空洞引发的死锁循环（PDF p. 175）。

更具方法论价值的是第 8 章（PDF p. 134）对约 400 行 TLA+ 形式化规约保护范围的坦诚界定：

> "We leave specifying and proving cluster membership, log compaction, and client interaction to future work, along with liveness and availability properties" *(PDF p. 134)*

机械化证明锁死的是**固定成员集合下的核心日志安全性**，而动态成员变更、快照截断、客户端会话与活性均不在该证明覆盖范围内。果不其然，论文发表次年 Ongaro 就在社区复盘了一处细微缺陷：若某实现把单节点变更顺序写反，允许尚未在当前任期提交 `no-op` 的新主直接追加第二条单步变更，两步变更叠加仍会撬开交集缺口——这再次提醒每一位内核工程师：**形式化证明只担保模型边界以内的代码，任何跨模块拼接都必须重新审视交集不变式。**

### Kasheff & Walsh（2014）：Ark——异步拉式 Oplog 复制下的确认栅栏与工程闭环 {#paper-35}

- **论文来源**：Zardosht Kasheff, Leif Walsh, *Ark: A Real-World Consensus Implementation*, arXiv:1407.4765, 2014 · [原始 PDF（共 12 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/consistency/ark%3A-a-real-world-consensus-implementation.pdf)（拉式链状复制与动态写关注背景见 PDF pp. 1–3；`Problem 3.1` 多数派确认回滚漏洞见 PDF pp. 4–5；`GTID`、持久化投票上界与禁 `Ack` 栅栏见 PDF pp. 6–8；三机房否决权扩展见 PDF p. 10）。

把视线拉回上一篇 `#36` Abadi 在 PACELC 中点名批评的早期 MongoDB（以及同源分支 TokuMX）：为什么在 Raft 论文已经公开发表的 2014 年，这类成熟文档数据库不能直接把旧复制层推倒换成标准 Raft？

Tokutek 工程师 Kasheff 与 Walsh 在 **Ark** 论文开篇摆出了三道无法回避的存量工程约束（PDF pp. 1–3）：
1. **拉式链状复制（Pull-based Chained Replication）**：与 Raft 由 Leader 主动推送日志相反，MongoDB/TokuMX 的从节点（Secondary）自己挑选上游数据源（可以是 Primary，也可以是另一个离自己更近的 Secondary！）拉取操作日志 `oplog`，像贪吃蛇一样组成多跳复制链以分担主库跨机房带宽；
2. **先乐观入库、失主时反向回滚（Optimistic Apply + Rollback）**：从节点拉到 `oplog` 条目后不等全局提交就立即应用到本地引擎供弱一致从库读使用；一旦切换新主发现自己多出了未提交尾巴，就通过反向补偿操作把多出来的事务回滚剥离；
3. **逐请求动态写关注（Per-Operation Write Concern）**：同一个集群上，写日志埋点的请求传 `w: 1`（主库落盘即回，追求极速），而扣款改密请求传 `w: majority`（要求过半副本同步完并回传复制 `Ack` 后才向客户端返回成功，承诺绝不回滚）。

早期协议的设计者以为：“只要选新主需要过半数投票，而 `w: majority` 写也需要过半数节点落盘确认，两个多数派必有交集，怎么可能回滚已经返回成功的 `w: majority` 写呢？”

论文第 3.1 节（`Problem 3.1`，PDF pp. 4–5）在 5 节点集群 `{A, B, C, D, E}` 上当场引爆了这个看似天衣无缝的直觉——**因为“投票选新主”和“拉取确认旧主日志”在异步拉式架构里是两条互不通气的独立通道！** 对照下表逐帧追踪旧版协议如何弄丢一笔 `w: majority` 扣款，以及 Ark 如何一招锁死它：

| 时序步骤 | 事件与消息交错 (`5` 节点 `{A,B,C,D,E}`) | 旧版无栅栏复制表现 (`Problem 3.1`) | Ark 禁 `Ack` 栅栏表现 (`PDF p. 7`) |
| :---: | :--- | :--- | :--- |
| **1** | `A`（`term 1` 旧主）心跳偶发丢包，`B` 发起 `term 2` 选举，获 **`{B, C, D}`** 3 票当选新主（日志均停在 `op 100`） | `C, D` 投完赞成票，但后台拉取线程仍连在 `A` 上且无任期约束 | `B, C, D` 持久化 `maxVotedTermId = 2`，**立即关闭对 `< 2` 旧主的 `Ack` 资格！** |
| **2** | 客户端向尚不知情的旧主 `A` 发送关键写 `op 101`（指定 `w: majority`） | `A` 本地写入 `(1, 101)`（计 1 票），等待另外 2 台从节点拉取确认 | `A` 本地写入 `(1, 101)`（计 1 票），等待另外 2 台从节点拉取确认 |
| **3** | 从节点 `C, D, E` 的后台拉取链路从 `A` 拉走 `(1, 101)` 并试探应用到本地引擎 | `C` 与 `D` 顺着拉取链路向 `A` 回传 **`Ack(101)`**！`A` 凑齐 `{A,C,D}` 3 票，**向客户端返回 `majority` 成功！** | `C, D` 虽拉取试探应用了 `(1, 101)`，但因 `1 < maxVotedTermId(2)` **扣住 `Ack` 拒不回传**！仅 `E` 回 `Ack`，`A` 卡在 2 票超时报错！ |
| **4** | 网络恢复，`C, D, E` 切向真主 `B`（已写 `(2, 101')`），回滚旧分叉 `(1, 101)` | **严重事故**：客户端已拿到 `w: majority` 成功回执的扣款被静默回滚抹除！ | **安全闭环**：客户端从未收到虚假成功承诺，试探脏尾被干净回滚，持久契约毫发无损！ |

明明凑够了选票多数派，也凑够了写入多数派，交集 `{C, D}` 甚至高达两个节点，旧版为什么还是丢了数据？因为 `C` 和 `D` **违背了时间先后契约**——它们先在选举通道里发誓拥立新王 `B`（当时的知识快照里并没有 `op 101`），转头却在复制通道里替旧王 `A` 的新遗诏盖章背书！

找准病根后，Ark 的三项核心改造显得格外犀利而克制（PDF pp. 6–8）：

1. **全局词典序 `GTID = (electionTermId, opid)` 与持久化 `maxVotedTermId`**：严禁对 `<= maxVotedTermId` 投赞成票（单任期单主），并对日志落后于自身的候选人投否决票 `veto`（兼顾 3 机房 9 副本下的跨机房写关注安全，PDF p. 10）。
2. **解耦“拉取试探应用”与“持久确认签收”的禁 `Ack` 栅栏（PDF p. 7）**：从节点在更高任期投出 `"yes"` 后，立即斩断对一切旧任期主节点的 `Ack` 回执，却允许拉取数据流照常运转以兼容流产选举：

> "To solve Problem 3.1, we make one more simple change: a replica never acknowledges an update from a primary after it votes “yes” in a later election." *(PDF p. 7)*

> "Note that a replica may still copy and apply oplog entries from any member of the set, knowing that it may roll them back at any point in the future." *(PDF p. 7)*

3. **防流产选举饿死的 `maxKnownTermId` 心跳逼退机制（PDF p. 8）**：假如 `B` 在任期 `5` 发起的选举中途流产（只拿到 `{B, C}` 两票未过半），此时旧主 `A`（任期 `4`）仍是唯一主节点，但 `B` 和 `C` 已永久丧失对任期 `4` 的确认权；若 `E` 此刻宕机，`A` 上的 `w: majority` 写就会因凑不够 3 票而无限期卡死！为此 Ark 在每 2 秒一次的全员心跳中广播 `maxKnownTermId`（全网已知获得过至少 1 张赞成票的最高任期）：一旦旧主 `A` 发现 `maxKnownTermId (5) > A.term (4)`，`A` 立即主动退位（全链路有界约 12 秒），触发任期 `6` 的干净重选，一举恢复全集群确认活性。

论文第 4 节（PDF p. 10）还展示了为什么 Ark 在普通选票之外特意保留了**否决票（`veto`）**来支撑企业级多数据中心定制写关注（Custom Write Concern）：

- 设某金融集群横跨 3 个机房 `DC1, DC2, DC3`，每个机房部署 3 台服务器，共 **9 个副本**（普通选举多数派为 `5/9`）。
- 某笔核心转账指定了自定义跨机房防灾规则——**“只需覆盖 `DC1` 全部节点或跨两机房指定组合”**；如果仅靠 `5/9` 简单多数派选主，候选人有可能绕开持有最新数据的局部子集当选。
- 在 Ark 中，只要拉票请求触达范围内哪怕 **1 台**持有字典序更新 `GTID` 的节点，它投出的单张 **`veto` 否决票**就能一票叫停落后候选人，迫使最新数据所在节点接管选举。

同时也要看清 Ark 在客户端视角下的两条精确语义边界：
- **零假阳性（Zero False Positives），但允许网络丢包假阴性**：凡是客户端明确收到 `w: majority` 成功回执的写入，100% 永不回滚；但若写入其实已在多数派落盘、偏偏回给客户端的最后一跳响应断网超时，该写依然可能随新主继承而最终生效（与 Raft/Paxos 一致，需配合客户端请求序列号幂等去重）；
- **从节点本地读取仍为试探态**：由于从节点允许在全局提交前先行应用拉到的 `oplog`，直接连接 Secondary 读取默认看到的是可能随切主回滚的乐观快照，唯有升级到后续的 `readConcern: majority` 提交点快照才能彻底屏蔽未决尾巴。这套兼顾严密安全性与存量拉式引擎兼容性的方案，最终化作了 MongoDB 3.2 复制集协议 `pv1` 的核心骨架。

---

## 共识协议设计空间全景：从理论旋钮到主流工业实现

跳出每篇论文各自的术语外壳，这五篇文献其实都在回答同一个核心状态机方程的四个子问题：

1. **谁有资格在当前纪元写新值？（纪元归属与选主门禁）**
   - **Paxos / Howard 路线（`#32`、`#30`）**：允许任意节点拿到任意粗糙日志参选 Phase 1，靠选上之后在第一阶段“向旧历史拉取并继承潜在已决值（Rule 4 / `V_dec`）”来补齐记忆；甚至在开放型快速寄存器集中允许所有客户端共享同一纪元并发盲写。
   - **Raft / Ark 路线（`#33`、`#31`、`#35`）**：反过来把历史完整性检查前置到拉票瞬间——日志不够新（`(lastTerm, lastIndex)` 或 `GTID` 落后）的候选人连赢下选举的资格都没有！代价是必须像 `Figure 8` 那样，严禁直接按副本数提交旧任期遗留日志，必须靠当前任期新项间接锁死前缀。

2. **稳态写一笔数据到底要等几台机器？（Quorum 几何解耦）**
   - 经典实现把选主集 \(Q_1\) 与稳态写集 \(Q_2\) 绑死在 \(\lfloor N/2 \rfloor + 1\)；Flexible Paxos 则揭示只需 \(Q_1 \cap Q_2 \neq \emptyset\)，允许用更大的偶发恢复集 \(Q_1\) 换取更小的日常复制集 \(Q_2\)，或用二维网格将两者同时压降至 \(\sqrt{N}\)；若升级到无主 1-RTT 快速通道，则需满足三元交集 \(Q_1 \cap Q_{2a} \cap Q_{2b} \neq \emptyset\) 以消解并发写碰撞。

3. **推式管道还是拉式拓扑？（数据流与控制流分离）**
   - Raft 用强领导者推式连续数组消灭了日志空洞与乱序应用难题；而当存量数据库必须保留多跳链状拉取（Pull-based Oplog）以节省跨机房主库出口带宽时，Ark 证明只要把“乐观拉取回放数据流”与“受 `maxVotedTermId` 严格约束的 `Ack` 确认控制流”解耦，拉式引擎同样能获得滴水不漏的共识保证。

4. **怎样在不写盘的前提下防止陈旧主脏读？（线性化读栅栏）**
   - 无论是上一篇 Megastore Step 4 的 `Coordinator` 失效通知，还是本篇 Ongaro 博士论文第 6 章的“上任 `no-op` + `ReadIndex` 轻量心跳验主 + `lastApplied` 水位等待”，本质都是在**不触碰物理时钟测不准风险**的前提下，用一次廉价的内存控制面握手证明“此刻尚无更高纪元新主篡位”。

把这四组旋钮与现代主流分布式存储的共识底座对齐，工业界的架构取舍便一目了然：

| 工业系统 / 引擎 | 共识家族谱系 | 日志流向与乱序模型 | 强一致读路径机制 | 成员变更与特色工程护栏 |
| :--- | :--- | :--- | :--- | :--- |
| **etcd / Consul** | 标准 **Raft** (`#33`/`#31`) | Leader 单向推式连续日志，无空洞 | **`ReadIndex`** 心跳验主（支持批合并） | 单节点步进增删 + `Pre-Vote` + Learner 无投票追赶节点 |
| **TiKV / CockroachDB** | Multi-**Raft** 分片组 | 成千上万 Range 独立跑连续 Raft 日志 | **`ReadIndex`** 或基于物理时钟误差界的 **Leader Lease** 本地读 | 联合共识 / 单步变更 + 快照流控，上层叠 Percolator / HLC 事务 |
| **MongoDB (`pv1`)** | **Ark** + Raft 任期融合 (`#35`) | 从节点**拉式链状 `oplog` 复制** + 乐观回放与反向回滚 | `readConcern: linearizable`（多数派确认屏障）或本地快照读 | **投票后禁旧主 `Ack` 栅栏** + `maxKnownTermId` 心跳退位 + 动态 `w` 写关注 |
| **Google Spanner / Chubby** | Multi-**Paxos** | 单主租约稳态跳过 Phase 1，支持受控流水线并发槽位 | **TrueTime** 有界主租约直接本地读，免跨网心跳 | 细粒度 Paxos 组分裂迁移，严守 `e_pro >= e_acc` 落盘次序 |
| **LogDevice / 广域定制存储** | **Flexible Paxos** 思想 (`#32`/`#30`) | 缩减稳态写集 \(Q_2\)（同可用区极速落盘），放大恢复读集 \(Q_1\) | 依配置走恢复读取交集或状态机水位栅栏 | 利用跨阶段非对称交集 \(Q_1 \cap Q_2 \neq \emptyset\) 削平稳态尾延迟 |

透过这张对照表不难发现：没有任何一套工业级共识实现是照搬单一论文伪代码的产物——生产系统总是在 Raft 的**连续无洞日志与清晰任期规则**之上，嫁接了 Flexible Paxos 的**法定人数几何弹性**、Ongaro 博士论文的 **`ReadIndex` / `Pre-Vote` 工程护栏**，以及 Ark 式的**控制流与数据流确认隔离**。

---

## 十篇经典数据库论文精读收官结语

回望从第 1 篇关系代数地基、第 2–5 篇 Cascades 查询优化器与基数估计、第 6–7 篇向量化/编译执行与多核网络硬件协同、第 8 篇 LSM 读写放大与现代缓冲池，直到第 9–10 篇千核事务隔离与分布式共识的整整 **67 篇经典论文**，一条贯穿半个世纪的数据库系统第一性原理清晰浮现：

数据库内核的一切精妙演进，从来都不是概念名词的堆砌，而是围绕**代数等价律（交换律、结合律、单调性、集合交集）**与**物理硬件层级（CPU 寄存器/SIMD、片上缓存行、NVMe 页、跨洲光速 RTT）**展开的精密博弈。看懂每一项经典设计背后“最朴素做法究竟在哪一步撞墙”，我们就能在面对下一代新硬件与新架构浪潮时，一眼认出那些历久弥新的不动点。

---

## 参考文献与阅读基线

本文精读的所有论文均对齐 `lonng/db-papers` 仓库基线（Commit `4ff1331`），原始 PDF 与核心图表索引汇总如下：

| 序号 | 论文标题与作者 | 发表年份与出处 | 仓库原始 PDF 与精读重点页码 |
| :--- | :--- | :--- | :--- |
| `#32` | *Distributed Consensus Revised* — Heidi Howard | Cambridge PhD Tech Report 2018/2019 | [原始 PDF（共 151 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/consensus/distributed-consensus-revised.pdf) · 持久化顺序 p. 40；Revision A–C pp. 76–94；`V_dec` 算例 p. 105；三元交集 pp. 122–138 |
| `#30` | *A Generalised Solution to Distributed Consensus* — H. Howard, R. Mortier | arXiv:1902.06776 (2019) | [原始 PDF（共 20 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/consensus/a-generalised-solution-to-distributed-consensus.pdf) · 单次写寄存器与四规则 p. 3；判定表 p. 5；快速冲突与重配 pp. 9–11 |
| `#33` | *In Search of an Understandable Consensus Algorithm (Extended Version)* — D. Ongaro, J. Ousterhout | USENIX ATC 2014 Tech Report | [原始 PDF（共 18 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/consensus/in-search-of-an-understandable-consensus-algorithm-%28extended-version%29.pdf) · Fig. 2 全集 p. 4；Fig. 8 覆写反例 pp. 8–9；联合共识 p. 11；压测 pp. 14–15 |
| `#31` | *Consensus: Bridging Theory and Practice* — Diego Ongaro | Stanford PhD Dissertation 2014 | [原始 PDF（共 258 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/consensus/consensus%3A-bridging-theory-and-practice.pdf) · 单步变更与 `Pre-Vote` pp. 51–62, 155；快照 pp. 69–75；`ReadIndex` pp. 87–92；TLA+ 边界 p. 134 |
| `#35` | *Ark: A Real-World Consensus Implementation* — Z. Kasheff, L. Walsh | arXiv:1407.4765 (2014) | [原始 PDF（共 12 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/consistency/ark%3A-a-real-world-consensus-implementation.pdf) · 拉式背景 pp. 1–3；Problem 3.1 漏洞 pp. 4–5；禁 `Ack` 栅栏与心跳退位 pp. 6–8 |
