---
title: "【Paper】事务隔离、千核并发与分布式存储精读：Adya 隔离模型、Abyss、Percolator、Megastore、HLC、PACELC 与 Anna"
date: 2026-10-22T13:20:00+08:00
lastmod: 2026-11-12T00:00:00+08:00
draft: false
slug: "db-papers-isolation-concurrency-and-distributed-transactions"
description: "从跨核缓存一致性抖动与跨洲广域网光速延迟两条物理约束出发，串联精读 Adya 广义隔离级别、千核并发控制评估、Percolator 跨行快照事务、Megastore 实体组 Paxos、混合逻辑时钟 HLC、PACELC 定理与 Anna 无协调格存储。"
categories: ["数据库"]
tags: ["论文精读", "事务隔离", "并发控制", "分布式事务", "一致性"]
series: ["数据库经典论文精读"]
toc: true
math: true
---

> 本系列第 9/10 篇 · [数据库经典论文精读总索引]({{< relref "2026-10-22-database-papers-source-reading.md" >}}) · [上一篇：存储结构与缓冲管理演进]({{< relref "2026-10-22-db-papers-lsm-rum-and-modern-buffer-managers.md" >}}) · [下一篇：分布式共识协议演进]({{< relref "2026-10-22-db-papers-paxos-raft-and-consensus-evolution.md" >}})

如果整个数据库永远只跑在一个单线程事件循环里，且所有数据都装在一块永不掉电的本地内存芯片上，并发控制与分布式一致性问题根本不会存在：事务按到达顺序逐个串行执行，前一个事务改完所有行，后一个事务再读，既没有脏读和幻读，也没有跨节点陈旧副本。

一旦真实业务负载突破单核吞吐上限或单机房容灾边界，最朴素的设计就会立刻撞上两道坚硬的物理高墙：

1. **片上多核的缓存一致性墙（Cache-Coherence Wall）**：当并发工作线程从 8 核扩展到 64 核乃至 1,000 核时，哪怕业务读写的数据行彼此完全不相交，只要引擎内部还保留着一个全局单调递增的时间戳计数器（64 字节缓存行上的原子 `fetch-and-add`），或者共用一张带桶闩锁（Latch）的全局锁表，片上互连网络就会被跨核缓存失效消息彻底淹没。协调元数据本身的硬件通信开销，反而会吃光全部新增算力。
2. **广域网的光速延迟墙（WAN Latency Wall）**：为了防止单个数据中心断电或海底光缆故障导致服务停摆，系统必须把同一份数据复制到相距数千公里的多个机房。此时无论网络有没有真正发生分区，光信号在跨洲光纤中往返一次（RTT）就要耗费 50–150 ms。如果每次读写都强求跨洋多数派同步握手，甚至为了对冲机器间几毫秒的物理时钟误差而额外停顿等待，交互式在线应用的尾延迟就会直接失控。

要想在这两道物理高墙之间腾挪，工程师首先得回答一个前置的数学问题：**到底哪些并发交错是绝对不能容忍的语义错误，哪些只是特定锁实现人为制造的假性冲突？** 如果连隔离级别的定义本身都绑死在悲观两阶段锁（2PL）上，多版本并发控制（MVCC）、乐观校验（OCC）乃至无锁异步合并连上场竞技的资格都会被标准误判剥夺。

## 从单核互斥到跨洲复制：七篇经典论文的协调税地图

在逐篇拆解之前，我们先在白板上走完两个只有两行数据的极简冲突切片，看清“语义契约”与“物理协调成本”是如何咬合在一起的。

### 切片一：两行排班表击穿锁时代隔离标准

假设医院值班表 `Duty(doctor, on_call)` 里只有两行：`('Alice', true)` 与 `('Bob', true)`，医院铁律要求“任何时刻至少一名医生在岗”。并发事务 \(T_1\) 与 \(T_2\) 同时发起请假申请：

```sql
-- 事务 T1（Alice 请假）                  -- 事务 T2（Bob 请假）
SELECT COUNT(*) FROM Duty               SELECT COUNT(*) FROM Duty
 WHERE on_call = true;  -- 读到 2         WHERE on_call = true;  -- 读到 2
UPDATE Duty SET on_call = false         UPDATE Duty SET on_call = false
 WHERE doctor = 'Alice';                 WHERE doctor = 'Bob';
COMMIT;                                 COMMIT;
```

在多版本快照隔离（Snapshot Isolation, SI）下，\(T_1\) 和 \(T_2\) 各自读取一致快照，发现都有 2 人在岗；随后 \(T_1\) 改 `Alice` 行，\(T_2\) 改 `Bob` 行，写集互不相交，双双顺利提交——最终表里在岗人数变成 `0`，业务不变量当场告破！诡异的是，你翻遍 ANSI SQL-92 标准定义的脏读（\(P_1\)）、不可重复读（\(P_2\)）与幻读（\(P_3\)）字面条文，这一写偏斜（Write Skew）竟然一条都不沾；反过来，如果某个乐观引擎让 \(T_1\) 和 \(T_2\) 先在私有缓冲里并发试写同一行、提交期再回滚败者，完全可串行化的执行却会被 Berenson 等人的脏写字面禁令 \(P_0\) 当场误杀。没有脱离锁实现的形式化图论，数据库连“什么是对的”都说不清楚。

### 切片二：一次跨行更新从缓存行抖动到跨洋 RTT 的代价跃迁

再看当我们要拦住上述异常时，硬件究竟付出了什么代价：
- 在**单机 1,000 核共享内存**里，为了给 \(T_1\)、\(T_2\) 分配事务时间戳，1,000 个核排队争抢同一个原子变量，单次跨核缓存失效往返约需 100 个时钟周期，1 GHz 下每秒最多发 1,000 万个戳；若改成每线程批发 100 个戳，一旦发生时间戳排序冲突重启，工作线程还会把手里整批过期时间戳逐个自杀耗尽。
- 在**单机房分布式表格**里，Alice 和 Bob 落在不同机器的分片上，底层存储只提供单行原子修改；要想让两行要么同时生效要么同时作废，又不想引入挂掉就锁死全场的外部协调节点，就必须把协调者状态编码进某一行自身的元数据列。
- 在**跨洲多机房部署**里，哪怕网络全年 99.9% 的时间健康畅通，只要系统承诺“机房掉电不丢数据且任意机房不读旧值”，每一次写操作还是不得不跨洋同步副本或逐一失效远端读缓存；除非我们用混合逻辑时钟（HLC）消掉中心授时等待，或者干脆像 Anna 那样将业务状态改造成满足结合律、交换律、幂等律的半格（Semilattice），让高冲突更新在本地核内直接合并消化。

本篇精读的 7 篇经典论文，正是沿着这条从形式化隔离语义、单机千核瓶颈、跨行/跨机房事务机制，一直走到广域时空时钟与零协调格存储的完整证据链渐次展开：

| 论文与年份 | 锚定问题域 | 协调粒度与核心抽象 | 稳态路径关键代价 | 语义与工程边界 |
| :--- | :--- | :--- | :--- | :--- |
| **Adya 等 (2000)** `#65` | 脱离锁实现定义隔离级别 | 对象全序版本链 \(\ll\) + 直接串行化图 `DSG(ww/wr/rw)` | 纯形式化历史判定，兼容乐观与多版本引擎 | `PL-3` 刻画已提交历史冲突可串行化，不含实时外部顺序与中止事务运行态不透明性 |
| **Staring into the Abyss (2014)** `#67` | 1,000 核共享内存并发可扩展性 | 7 类 CC 算法统一压测与 6 维耗时拆解 | 原子授时约 100 周期/次；锁表等待与校验复制随核数陡增 | 盲目批分配时间戳诱发 `TIMESTAMP` 连续重试空转；全分区锁畏惧跨区事务 |
| **Percolator (2010)** `#66` | 在单行原语上叠加跨行分布式事务 | Bigtable 三列布局 + `TSO` + `Primary Lock` 原子提交点 | 读接近原生（`0.94x`），写因多步落盘降至 `0.23x` | 提供快照隔离（防写写冲突，仍放行写偏斜 `G2-item`），吞吐受 `TSO` 授时制约 |
| **Megastore (2011)** `#54` | 跨洲高可用交互存储与本地低延迟读 | 细粒度 `Entity Group` 独立 Paxos + 本地 `Coordinator` 栅栏 | 同组写约 1 次广域网 RTT（100–400 ms），健康态强一致读纯本地（~20 ms） | 组内串行写入上限仅每秒数次；跨组退化为异步消息队列或高开销两阶段提交 |
| **HLC (2014)** `#37` | 无原子钟硬件下的全局因果快照授时 | 双分量 `(l, c)` 字典序时间戳（48 位物理上取整 + 16 位计数） | 零额外网络阻塞，误差严格受限于 NTP 偏置 \(\epsilon\)，99% 以上 `c <= 4` | 单向蕴含因果序（并发事件可平局）；跨带外信道的严格外部一致性仍需通知等待 |
| **PACELC (2012)** `#36` | 澄清 CAP 盲区，刻画常态复制权衡 | `If Partition (A vs C) Else (Latency vs Consistency)` 双分支分类 | 揭示 99.9% 无故障时间里“防灾复制”强加的读写延迟税 | \(R + W > N\) 异步重叠不等同于完整线性一致性；同一分类码下故障回滚细节各异 |
| **Anna (2018)** `#34` | 彻底消灭跨核锁与跨节点同步协调 | 单核私有内存 Actor + 可组合有界半格（`ACI Lattice`）异步交换 | 热键更新可在发送端按结合律就地折叠，高争用吞吐反超共享内存引擎数量级 | 仅覆盖无需同步仲裁的弱隔离模型；低争用过度复制时后台 gossip 占满 CPU |

---

## 隔离语义基线与单机千核并发墙

早期的商业数据库把“隔离级别”和“怎么加锁”死死绑在一起：读已提交就是读共享锁用完即放、写排他锁持到提交；可串行化就是读写锁加谓词范围锁（Predicate Lock）一律持到事务结束。这种定义在单核悲观锁时代看似省事，一旦放进多版本快照引擎或千核高并发内存数据库里，就会同时引发**理论误判**与**硬件雪崩**。

![从 Adya 依赖图与千核瓶颈到 Percolator / Megastore 分布式事务演进](/images/diagrams/paper-txn-adya-dsg-to-percolator.svg)

### Adya 等（2000）：广义隔离级别定义与直接串行化图（DSG） {#paper-65}

- **论文来源**：Atul Adya, Barbara Liskov, Patrick O'Neil, *Generalized Isolation Level Definitions*, ICDE 2000 · [原始 PDF（共 12 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/transaction/generalized-isolation-level-definitions.pdf)（ANSI 锁隔离对照见 `Figure 1` PDF p. 3；三类直接冲突与 `DSG` 图例见 `Figure 2–3` PDF p. 8；混合隔离图 `MSG` 见 PDF p. 11）。

Adya、Liskov 与 O'Neil 开篇直指 ANSI SQL-92 标准的根本病因：标准起草者试图用不提锁的通用语言重述 System R 的锁级别，却写出了既漏抓真实异常（如快照隔离写偏斜）、又把乐观并发与多版本引擎一棍子打死的事件序列禁令：

> "Phenomenon P0 can occur in optimistic implementations since there can be many uncommitted transactions modifying local copies of the same object concurrently; if necessary, some of them will be forced to abort so that serializability can be provided. Thus, disallowing P0 can rule out optimistic implementations." *(PDF p. 3)*

为了让悲观锁、乐观校验与多版本存储站在同一把数学标尺下接受检验，论文将数据库运行轨迹抽象为两部分：客户端可观测的读写事件偏序历史 \(H\)，以及存储引擎为每个对象在已提交版本集合上指派的**全序版本链（Version Order，\(\ll\)）**。注意这步解耦的威力：\(x_i \ll x_j\) 仅代表系统逻辑上认定版本 \(x_j\) 排在 \(x_i\) 之后，完全不必等同于物理写动作发生的墙钟顺序（例如时间戳并发控制可以根据事务开启时间将晚到达的写安插在早到达的写之前，PDF p. 5）。

为了把 `INSERT`、`DELETE` 和范围查询全部纳入统一模型而不留死角，作者补齐了三块精巧的拼图（PDF pp. 4–7）：
1. **对象全生命周期版本**：虚设初始事务 \(T_{\text{init}}\) 为宇宙间所有潜在对象预置不可见的 `unborn` 版本；`INSERT` 生成首个可见版本（删除后再插入同主键在逻辑上视作带独立唯一标识的新对象实例）；`DELETE` 则生成不可见的 `dead` 终结版本。
2. **谓词读版本集 `Vset(P)`**：任何谓词扫描（哪怕是不返回具体元组的 `SELECT COUNT(*) WHERE dept = 'Sales'`）在执行点都绑定了全库对象的一组逻辑快照 `Vset(P)`。只有当另一事务写入的新版本真正改变了谓词 \(P\) 的匹配元组集合时（例如将员工部门从 `'Eng'` 改为 `'Sales'`，而非仅仅更新与谓词无关的 `phone` 电话号码列，PDF p. 7），才构成对谓词读的实质冲突。
3. **三类直接冲突边与 `rw` 的关键非对称性**：在所有已提交事务集合上，直接串行化图 `DSG(H)` 包含三类有向边——
   - **直接写依赖（\(T_i \xrightarrow{ww} T_j\)）**：\(T_i\) 产出 \(x_i\)，\(T_j\) 产出同一对象在 \(\ll\) 链上**紧邻的下一版本** \(x_j\)；
   - **直接读依赖（\(T_i \xrightarrow{wr} T_j\)）**：\(T_i\) 写入 \(x_i\)，\(T_j\) 随后按项读到 \(x_i\)，或在谓词 `Vset(P)` 中依赖了改变匹配结果的 \(x_i\)；
   - **直接反依赖（\(T_i \xrightarrow{rw} T_j\)）**：\(T_i\) 先读旧态、\(T_j\) 随后生成新版本推翻该旧态。这里藏着全文最精妙的一处非对称条款：

> "For item-anti-depends, the overwriting transaction must produce the very next version of the read object, while for predicate-anti-depends it simply produces a later version that changes the matched tuples of the predicate." *(PDF p. 7)*

为什么按项 `rw` 只连“紧邻下一版本”，而谓词 `rw` 必须连“任意后续改变匹配集的版本”？看一个三事务链就豁然开朗：设员工对象 \(z\) 的版本序为 \(z_0(\text{Eng}) \ll z_1(\text{Eng, 仅改电话}) \ll z_2(\text{Sales})\)。若 \(T_1\) 按主键读取了 \(z_0\)，由于 \(T_a\) 产出紧邻的 \(z_1\)（\(T_1 \xrightarrow{rw} T_a\)），而 \(T_2\) 产出 \(z_2\)（\(T_a \xrightarrow{ww} T_2\)），传递路径 \(T_1 \to T_a \to T_2\) 已经连通，无需多余跨跳边；但若 \(T_1\) 执行的是谓词读 `WHERE dept = 'Sales'`，\(T_a\) 改电话根本没动销售部名单，因此不存在 \(T_1 \to T_a\) 的边！倘若谓词反依赖只看紧邻下一版本，真正制造幻影行的 \(T_2\) 就会漏网逃逸。

在这套严谨的图结构之上，杂乱的隔离异常化作了四级干净的图环禁令（PDF pp. 8–11）：

| 广义级别 | 新增禁止现象 | 图结构与历史约束精髓 | 典型放行或拦截案例 |
| :--- | :--- | :--- | :--- |
| **`PL-1`** | `G0`（脏写环） | `DSG` 中不含纯由 `ww` 边组成的有向环 | 防止 \(T_1, T_2\) 在对象 \(x, y\) 上交叉覆盖形成 \(x_1 \ll x_2\) 却 \(y_2 \ll y_1\)；允许并发写私有缓冲 |
| **`PL-2`** | `G1a`, `G1b`, `G1c` | 禁读中止版本（`G1a`）、禁读未提交中间态（`G1b`），且 `DSG` 不含仅由 `ww`/`wr` 构成的环（`G1c`） | 对应广义读已提交；允许乐观流水线提前读对方最终版本（只要写者先提交） |
| **`PL-2.99`** | `G2-item`（项反依赖环） | `DSG` 不含任何带有按项 `rw` 边的有向环 | 拦截不可重复读与双行写偏斜（\(T_1 \xrightarrow{rw} T_2 \xrightarrow{rw} T_1\)），但仍允许纯谓词 `rw` 幻读环 |
| **`PL-3`** | `G2`（任意反依赖环） | `DSG` 完全无环（禁止含按项或谓词 `rw` 边的任何环） | 等价于已提交事务冲突可串行化；混跑弱级别时可用 `MSG` 裁剪非必要冲突边 |

用这张表回看篇首的医生排班反例：\(T_1\) 读 `Bob=true` 被 \(T_2\) 改成 `false` 产生 \(T_1 \xrightarrow{rw} T_2\)，\(T_2\) 读 `Alice=true` 被 \(T_1\) 改成 `false` 产生 \(T_2 \xrightarrow{rw} T_1\)，两条 `rw` 边拼成 `G2-item` 二元环。快照隔离（SI）只拦写写冲突而不拦 `rw` 环，因此严格介于 `PL-2` 与 `PL-2.99`/`PL-3` 之间。

再对比 `PL-2.99` 与 `PL-3` 之间那 `0.01` 的差距——假设银行对账事务 \(T_{\text{audit}}\) 先执行 `SELECT SUM(bal) FROM Acct WHERE branch = 'NYC'`（谓词读），再读取汇总控制表 `Summary.nyc_total`（按项读）；并发开户事务 \(T_{\text{open}}\) 先 `INSERT` 一张余额 `\$100` 的纽约新账户 \(a_{\text{new}}\)（将该对象的 `unborn` 初始版本翻转为可见版本，改变谓词匹配集），再把 `Summary.nyc_total` 加上 `\$100` 并提交。

如果存储引擎只在已存在的物理元组上挂行级读写锁（对应 `PL-2.99`），\(a_{\text{new}}\) 在 \(T_{\text{audit}}\) 扫描时根本尚未落表，行锁无处可挂！于是 \(T_{\text{audit}}\) 前半段汇总没算进这 `\$100`，后半段读取控制表却读到了 \(T_{\text{open}}\) 提交后的新总额，当场报出账实不符的假警报。把它画进 `DSG(H)`，两条冲突边严丝合缝地闭合成一条双节点 `G2` 环：

$$T_{\text{audit}} \xrightarrow{\text{predicate-}rw\;(a_{\text{new}}:\;\text{unborn} \ll a_{\text{new},1})} T_{\text{open}} \xrightarrow{\text{item-}wr\;(\text{Summary})} T_{\text{audit}}$$

这行公式清晰表明：不看住谓词反依赖边的引擎永远止步于 `PL-2.99`，唯有连同谓词 `Vset(P)` 上的反依赖一并封堵，才能抵达真正的 `PL-3`。

在真实生产负载中，要求全库所有后台统计查询都跑在 `PL-3` 往往过于昂贵。论文第 5 节（PDF p. 11）为此专门定义了**混合串行化图（Mixed Serialization Graph, `MSG`）**，精确回答了“弱级别查询与强级别核心事务同库混跑时，到底需要保留哪些边”：

- **写依赖边（`ww`）**：所有事务间的 `ww` 边一律保留（守住最底线的 `PL-1` 版本次序一致性）；
- **读依赖边（`wr`）**：仅保留**射向** `PL-2` 或 `PL-3` 事务的入边（因为 `PL-1` 自身本就容忍脏读）；
- **反依赖边（`rw`）**：仅保留**从 `PL-3` 事务射出**的出边！

拿一个混合场景验证这条裁剪规则的妙处：假设核心转账事务 \(T_{\text{bank}}\) 声明为 `PL-3`，先写账户 A 再写账户 B；并发的监控大盘查询 \(T_{\text{dash}}\) 声明为 `PL-2`（读已提交），先读到了新 A，后读到了旧 B。从 \(T_{\text{bank}}\) 到 \(T_{\text{dash}}\) 有一条 `wr` 入边（A 上先写后读），而从 \(T_{\text{dash}}\) 到 \(T_{\text{bank}}\) 在 B 上虽然客观存在“先读旧 B 后被覆写”的反依赖，但由于 \(T_{\text{dash}}\) 只是 `PL-2`，**它的出向 `rw` 边不进入 `MSG`**！于是 `MSG` 中没有成环，\(T_{\text{dash}}\) 无需加长读锁阻塞 \(T_{\text{bank}}\)，而 \(T_{\text{bank}}\) 自身看到的世界依然严格等价于全局串行执行。同时也要读准 `PL-3` 的适用边界：它只要求**已提交事务子图**无环，既不约束中止事务在回滚前是否读到过不一致的中间快照（即运行态不透明性 Opacity），也不要求串行化次序遵循挂钟先后（即严格可串行化 Strict Serializability）。

### Yu 等（2014）：凝视千核深渊——1000 核并发控制全面评估 {#paper-67}

- **论文来源**：Xiangyao Yu, George Bezerra, Andrew Pavlo, Srinivas Devadas, Michael Stonebraker, *Staring into the Abyss: An Evaluation of Concurrency Control with One Thousand Cores*, PVLDB 2014 · [原始 PDF（共 13 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/transaction/staring-into-the-abyss%3A-an-evaluation-of-concurrency-control-with-one-thousand-cores.pdf)（Graphite 1,024 核瓦片模拟器与物理机校准见 `Figure 1–3` PDF pp. 4–5；时间戳分配与批处理死循环见 `Figure 6–7` PDF p. 7；YCSB 与 TPC-C 全谱系压测见 `Figure 8–17` PDF pp. 8–11）。

有了 Adya 的语义许可证，悲观锁、时间戳排序、多版本与乐观校验都可以在各自条件下实现 `PL-3`。但在 32 核服务器上跑得飞快的算法，一旦放进未来单芯片 1,000 核的共享内存环境里会发生什么？MIT 与 CMU 团队搭建了一个彻底剥离磁盘日志与网络栈噪声的内存测试台，在 1,024 核 2D-Mesh 片上网络模拟器 Graphite（经 Intel Xeon E7-4830 实机曲线校准）上对 7 种经典算法展开同场解剖：

> "With hundreds of threads running in parallel, the complexity of coordinating competing accesses to data will become a major bottleneck to scalability, and will likely dwindle the gains from increased core counts." *(PDF p. 2)*

模拟器把每个硬件线程的总执行时间 \(T_{\text{total}}\) 严格拆解为六本互斥的时钟周期分类账：

$$T_{\text{total}} = T_{\text{useful}} + T_{\text{abort}} + T_{\text{ts\_alloc}} + T_{\text{index}} + T_{\text{wait}} + T_{\text{manager}}$$

其中 \(T_{\text{useful}}\) 是真正读写业务字段的有效算力，其余五项全都是并发协调强加的系统税（`abort` 为冲突回滚清退、`ts_alloc` 为申请全局时间戳、`index` 为哈希桶探测、`wait` 为等锁或等待提交校验、`manager` 为锁表或私有读写集维护）。七大流派在千核压力下的死穴在实验镜头下一览无余：

| 并发控制方案 | 核心协调机制 | 只读 YCSB 表现 (`Fig. 8`) | 高争用热点表现 (`Fig. 10 / 16`) | 千核深渊下的头号崩溃机制 |
| :--- | :--- | :--- | :--- | :--- |
| **`DL_DETECT`** | 2PL + 中央有向等待图死锁检测 | 受锁表桶闩锁拖累 | 几百核后彻底归零 | 死锁检测线程与跨核锁队列等待（Lock Thrashing）双重卡死 |
| **`NO_WAIT`** | 2PL + 遇锁立即中止重启 | 同上，免除等待队列 | 吞吐断崖暴跌 | 热点行冲突触发海啸级 `abort` 回滚空转，白烧 CPU 周期 |
| **`WAIT_DIE`** | 2PL + 时间戳定序（老等新退） | 叠加强时间戳分配税 | 吞吐坍塌 | 开局必抢全局时间戳，叠加锁等待队列传导阻塞 |
| **`TIMESTAMP`** | 单版本基本时间戳排序（BTO） | **极差**（读亦写 `last_read_ts`） | 批分配下疯狂空转 | 纯读操作写共享元数据引发缓存行风暴；批授时导致连续重启失效 |
| **`MVCC`** | 多版本时间戳排序（MVTO） | **近线性扩展至 1000 核** | 热点写仍受挫但读稳健 | 历史版本链维护与中心化授时开销（批授时可显著缓解） |
| **`OCC`** | 私有读写集拷贝 + 提交期逐项校验 | **差**（私有拷贝 + 重扫校验） | 集中校验连环爆破 | 长读集拷贝耗内存带宽；提交期校验失败把已完成计算全额报废 |
| **`H-STORE`** | 按分区单线程独占 + 粗粒度分区锁 | **无跨区时线性扩展** | 4 仓库全卡在 4 个核上 | 倾斜热点无法借用空闲核；一旦跨分区事务占比上升，整区陪等远程核 |

这里有两处极具工程穿透力的微观发现值得单独拎出来细看。

第一处是**批量时间戳分配（Timestamp Batching）的冰火两重天**（`Figure 6–7`，PDF p. 7）。单点原子 `fetch-and-add` 因跨瓦片缓存一致性往返约需 100 周期，在 1 GHz 下卡死在 \(10^7\) 次/秒。若改为工作线程每次原子加 \(B\) 领走一段连续区间，\(B\) 越大，`MVCC` 吞吐越高；但 `TIMESTAMP`（BTO）一旦打开批分配，在中等争用下吞吐反而归零！原因就在于重启线程手里的存量批次成了催命符：

> "This is because when a transaction is restarted due to a conflict, it gets restarted in the same worker thread and is assigned the next timestamp in the last batch. But this new timestamp will also be less than the one for the other transaction that caused the abort, and thus it will continually restart until the thread fetches a new batch." *(PDF p. 7)*

换言之，线程 A 领了 `[1000, 1999]`，线程 B 领了 `[2000, 2999]` 并率先用 `2001` 写了热点元组；当线程 A 拿 `1000` 撞车中止后，原地重试依次摸出 `1001, 1002, ..., 1999`，每一次都小于 `2001`，硬生生连撞 1,000 次南墙才去领新号段！

第二处是**锁抖动（Lock Thrashing）与死锁的剥离实验**（`Figure 4`，PDF p. 6）。很多人以为 2PL 在高并发下慢是因为死锁多，作者特意让事务严格按主键升序加锁，从数学上 100% 消除了死锁，结果吞吐依然在数百核后雪崩——因为只要一个持有很多锁的事务在片上网络中被轻微延迟，所有碰触其中任意一把锁的后续线程就会像连环追尾一样排起长龙，持锁者不走，整片核心全部陪绑干等。

再把视角切到真实 OLTP 混合负载 TPC-C（`Figure 16–17`，PDF p. 11），千核硬件上的瓶颈漂移更加耐人寻味：

- **4 仓库极端热点态（`Figure 16`）**：全系统 1,000 个线程挤在 4 个 `Warehouse` 行上执行 `Payment` 累加 `W_YTD` 字段。此时 `H-STORE` 最多只能动用 4 个分区线程，其余 996 个核彻底闲置；悲观锁方案超过 90% 的周期消耗在 `wait` 与 `abort` 上；即便换成 `MVCC`，写写冲突也让有效工作占比跌至冰点。
- **1,024 仓库充足并行态（`Figure 17`）**：当每个核分到独立仓库后，`Payment` 热点瞬间蒸发，性能瓶颈立刻转移到访问 5–15 个商品项、频繁操作二级索引与历史链的 `NewOrder` 事务上。若所有商品均取自本地仓库，`H-STORE` 因零锁零时间戳开销一骑绝尘；但只要按标准 TPC-C 掺入 **10% 的跨仓库远程商品行**，`H-STORE` 粗粒度的分区级排他锁就会迫使协调分区干等远程核处理完毕，吞吐剧烈回落，反倒是经过时间戳批处理优化的 `MVCC` 与轻量无等待方案展现出更均衡的韧性。

这篇论文用详实的微架构周期账单宣告了传统中心化锁表与单点原子授时的终结，直接把学术界推向了去中心化周期授时（Silo）与按数据项动态计算提交时间戳（TicToc）的新赛道。

---

## 单行原语之上的跨行快照与实体组复制

单机共享内存受困于片上缓存一致性，而跨越数千台商用服务器的分布式存储则面临更严酷的起点：像 Bigtable 或 HBase 这样的底层宽表引擎为了弹性分裂与迁移，**原生只保证单行事务原子性**。当业务既要跨表更新多行网页索引，又要跨大洲容灾并提供低延迟在线读取时，`Percolator` 与 `Megastore` 分别在“跨行两阶段快照”与“细粒度实体组共识”两个维度立下了工业界标杆。

### Percolator（2010）：在单行 Bigtable 上构建跨行快照隔离与增量计算 {#paper-66}

- **论文来源**：Daniel Peng, Frank Dabek, *Large-scale Incremental Processing Using Distributed Transactions and Notifications*, OSDI 2010 · [原始 PDF（共 14 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/transaction/large-scale-incremental-processing-using-distributed-transactions-and-notifications.pdf)（快照写偏斜示例见 `Figure 3` PDF p. 3；Bigtable 三列布局与转账轨迹见 `Figure 4–5` PDF p. 4；核心事务伪代码见 `Figure 6` PDF p. 5；读写性能与故障恢复曲线见 `Figure 8–10` PDF pp. 10–12）。

为了把 Google 网页索引的构建模式从“每次重跑整套 MapReduce 批处理（延迟数天）”改造成“抓到一页就增量触发相关锚文本与重复页重算（延迟秒级）”，Percolator 需要在数千台机器的 Bigtable 之上提供每秒数万次的随机跨行 ACID 事务。没有全局锁管理器，客户端中途随时可能断电崩溃，怎样防止分布式两阶段提交（2PC）留下半死不活的悬挂锁？

Peng 与 Dabek 给出的解法干净利落：**把每个业务列 `c` 展开为同行内的三个物理列族——`c:data`（存真实值）、`c:lock`（存未决锁及主锁坐标）、`c:write`（存已提交可见时间戳到数据时间戳的映射），并将整个跨行事务的生死大权，全部系于客户端任选的一个主键锁（Primary Lock）所在行的单行原子修改之上！**

跟着论文 `Figure 5`（PDF p. 4）的“Bob 向 Joe 转账 7 美元”实例（初始时 Bob 在时间戳 `5` 有 `\$10`，Joe 在时间戳 `5` 有 `\$2`），逐步观察 Bigtable 底层三列状态如何严丝合缝地翻转：

| 执行阶段 | 目标行键 | `bal:data` 多版本列 | `bal:lock` 持久锁列 | `bal:write` 提交可见列 | 对外读取可见状态 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **0. 初始稳态** | `Bob` / `Joe` | `Bob: 5:$10`；`Joe: 5:$2` | 空 | `Bob: 5:data@5`；`Joe: 5:data@5` | 读任意 `ts >= 5` 见 `$10` 与 `$2` |
| **1. `Prewrite` 预写 (`start_ts=7`)** | `Bob` (选为 **Primary**)<br>`Joe` (Secondary) | 新增 `7:$3`<br>新增 `7:$17` | `7: I am primary`<br>`7: primary @ Bob.bal` | 仍为 `5:data@5`<br>仍为 `5:data@5` | `ts < 7` 读旧值；`ts >= 7` 撞见 `lock@7` 需等待或探主 |
| **2. `Commit` 主键翻牌 (`commit_ts=8`)** | 仅改 **`Bob`** 单行 | `7:$3`, `5:$10` | **原子擦除 `7` 号锁** | **原子写入 `8:data@7`** | **全局原子提交生效！** `ts >= 8` 读 Bob 顺指针拿 `$3` |
| **3. 异步清理次级锁** | `Joe` 单行 | `7:$17`, `5:$2` | 擦除 `7` 号锁 | 补写 `8:data@7` | Joe 行恢复免阻塞直读 `$17` |

结合 `Figure 6`（PDF p. 5）提炼出的客户端两阶段提交骨架代码，可以一眼看穿所有时间窗口的防御逻辑：

```cpp
bool Prewrite(Write w, Write primary) {
  Column c = w.col;
  bigtable::Txn T = bigtable::StartRowTransaction(w.row);
  // 1. 若在 (start_ts, ∞) 已有新提交，触发快照隔离 First-Committer-Wins 中止
  if (T.Read(w.row, c + ":write", [start_ts_ + 1, ∞])) return false;
  // 2. 若任意时间戳存在残留锁，放弃本次预写以避免锁重叠
  if (T.Read(w.row, c + ":lock", [0, ∞])) return false;
  T.Write(w.row, c + ":data", start_ts_, w.value);
  T.Write(w.row, c + ":lock", start_ts_, {primary.row, primary.col});
  return T.Commit(); // Bigtable 原生单行原子提交
}

bool Commit() {
  Write primary = writes_[0];
  for (Write w : writes_) if (!Prewrite(w, primary)) return false;
  int commit_ts = tso_.GetTimestamp(); // 严格满足 commit_ts > start_ts
  // 3. 单点原子提交锚点：仅当 Primary Lock 仍在时，擦锁并写 write@commit_ts
  bigtable::Txn P = bigtable::StartRowTransaction(primary.row);
  if (!P.Read(primary.row, primary.col + ":lock", [start_ts_, start_ts_]))
    return false; // 已被其他恢复事务回滚清退！
  P.Write(primary.row, primary.col + ":write", commit_ts, start_ts_);
  P.Erase(primary.row, primary.col + ":lock", start_ts_);
  if (!P.Commit()) return false;
  // 4. 跨过提交点后，异步把所有次级锁翻转为 write@commit_ts
  for (Write w : tail(writes_)) AsyncReplaceLockWithWrite(w, commit_ts, start_ts_);
  return true;
}
```

这里每一行检查都紧扣并发安全闭环：

> "The write record indicates to readers that committed data exists in this cell; it contains a pointer to the start timestamp where readers can find the actual data. Once the primary’s write is visible (line 58), the transaction must commit since it has made a write visible to readers." *(PDF p. 5)*

设想最凶险的故障时序：原客户端在 `Prewrite` 写完 Bob 和 Joe 的锁之后突然网络假死，另一个在 `ts = 9` 读取 Joe 余额的读者撞上了 `Joe` 行上超时的 `lock @ 7`。此时读者不靠猜测，直接按锁里记录的 `primary @ Bob.bal` 杀向 Bob 行探查真相：

> "A transaction that encounters a lock can distinguish between the two cases by inspecting the primary lock: if the primary lock has been replaced by a write record, the transaction which wrote the lock must have committed and the lock must be rolled forward, otherwise it should be rolled back" *(PDF p. 6)*

如果 Bob 行上已经躺着 `write @ 8 -> data @ 7`，读者就顺手替崩溃的客户端把 Joe 行的 `lock @ 7` 翻转为 `write @ 8`（**惰性前滚**）；如果 Bob 行上依然挂着 `lock @ 7`，读者就对 Bob 行发起单行原子删锁（**惰性回滚**）。即便此时假死的原客户端突然苏醒也冲过来提交 Bob 行，Bigtable 的单行串行化也会确保“删锁回滚”与“擦锁写 `write` 提交”二者必居其一且绝不并存！

再对照读取侧的 `Get()` 逻辑（`Figure 6` PDF p. 5），便能看清为何写侧必须先挂完所有锁才能拿 `commit_ts`：

```cpp
bool Get(Row r, Column c, string* value) {
  while (true) {
    bigtable::Txn T = bigtable::StartRowTransaction(r);
    // 1. 检查 [0, start_ts] 窗口内有无悬而未决的并发锁
    if (T.Read(r, c + ":lock", [0, start_ts_])) {
      BackoffAndMaybeCleanupLock(r, c); // 等待或顺藤摸瓜探查 Primary 清理死锁
      continue;
    }
    // 2. 取 <= start_ts 的最新已提交 write 记录，按其内部指针跳读对应 data
    Write w = T.ReadLatest(r, c + ":write", [0, start_ts_]);
    if (!w.Exists()) return false;
    *value = T.Read(r, c + ":data", w.data_timestamp());
    return true;
  }
}
```

不妨追问：倘若一个事务不检查 `[0, start_ts]` 里的 `lock` 直接读 `write` 会发生什么？设写事务 \(T_w\) 以 `start_ts = 5` 完成全部 `Prewrite` 挂锁，随后向 `TSO` 领到 `commit_ts = 8` 并刚刚翻完主键 Bob 行的牌、尚未清理次级 Joe 行的锁；此刻读事务 \(T_r\) 向同一个 `TSO` 领到更大的 **`start_ts = 9`**，先读到了 Bob 的新余额，转头去读 Joe 时如果无视 `Joe` 行上挂着的 `lock @ 5`（因 \(5 \le 9\) 落入检查窗口！），就会漏读 Joe 尚未翻牌的 `write @ 8` 而看到转账前旧余额，造成总金额平白缩水的脏快照撕裂！正是“先全员挂锁 \(\to\) 再领 `commit_ts` \(\to\) 读者必拦 `<= start_ts` 未决锁”这组时序闭环，严丝合缝地守住了跨行原子快照。

在这层事务引擎之上，Percolator 还用两列极轻量的辅助元数据（`notify` 与 `ack`）搭起了替代批处理 MapReduce 的增量计算框架 **Observer**（PDF pp. 7–8）：

1. **脏格标记与消息折叠（Message Collapsing）**：每当业务列写入提交，客户端顺带在同一单元格写下 `notify` 时间戳标记。即便某个热门网页在 1 秒内被上游连改 50 次，后台扫描线程发现 `notify` 后也只会触发**一次**下游观察者事务重算最新快照，把 49 次冗余中间态计算直接折叠清零。
2. **用 `ack` 写写冲突防并发重复触发**：倘若两个扫描工作线程同时扫到了同一行的 `notify` 并并发启动观察者事务，怎样防止下游重复发通知？每个观察者事务在启动时都会将自己的 `start_ts` 写入对应的 `ack` 列——由于两者修改同一个 `ack` 单元格，Percolator 自身的快照隔离写写冲突检测必然保证**至多只有一个观察者能成功提交**！
3. **热点扇入列的弱通知逃生阀（Weak Notification）**：对于数万反向链接同时指向的门户首页，若每次通知都强写 `ack` 锁会导致观察者频繁冲突回滚；Percolator 对此类仅通知列允许跳过 `ack` 互斥（容忍极少量幂等重算），从而打消跨站扇入热点。

代价账本同样透明：一次 Percolator 写需要多次跨网 Bigtable 单行读写，微基准写吞吐降至原生 Bigtable 的 `0.23x`（读为 `0.94x`，`Figure 8` PDF p. 10），但在 Google 生产网页库规模扩大 3 倍的前提下，它将单个网页从抓取到可检索的中位延迟缩减了 **100 倍以上**。

### Megastore（2011）：实体组 Paxos 分片与可见性栅栏的工程折中 {#paper-54}

- **论文来源**：Jason Baker et al., *Megastore: Providing Scalable, Highly Available Storage for Interactive Services*, CIDR 2011 · [原始 PDF（共 12 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/rdbms/megastore%3A-providing-scalable%2C-highly-available-storage-for-interactive-services.pdf)（实体组切分与跨组机制见 `Figure 1–2` PDF p. 3；跨机房架构与读写状态机见 `Figure 5` 及第 4.4–4.5 节 PDF pp. 7–8；生产可用性与延迟分布见 `Figure 9–10` PDF p. 10）。

Percolator 跑在单机房高速局域网内，而面向全球数十亿终端用户的在线服务（日均 200 亿次读、30 亿次写、1 PB 核心数据）必须直面机房级断电与断网：数据必须实时同步到跨地域多个数据中心。如果让整张大表跑一条全局广域网 Paxos 日志，每秒几次跨洋往返就会把全站并发写卡死在单点队尾；如果退回最终一致复制，应用层又要成天处理丢失邮件、乱序日历和悬空二级索引。

Megastore 的破局点在于**沿业务所有权自然边界切分“实体组（Entity Group）”**——例如同一个邮箱账号下的全部邮件与标签、同一张相册下的照片与评论同属一个实体组，物理上聚簇在底层 Bigtable 连续键前缀区间内，**每个实体组独立运行一条轻量级跨机房 Paxos 预写日志**（PDF p. 3）：

> "Boundaries that are too fine-grained force excessive cross-group operations, but placing too much unrelated data in a single group serializes unrelated writes, which degrades throughput." *(PDF p. 3)*

这样一来，千万个活跃用户的日常更新分流到了千万条互不阻塞的独立 Paxos 实例上，组内天然享有强一致可串行化 ACID 与实时本地索引；偶发的跨组协同（如用户 A 给用户 B 发邀请）则主要通过持久化在 Bigtable 里的事务性异步消息队列解耦（极少数强原子场景再调跨组 2PC）。同时为了节省存储成本，除保存完整数据的 `full replica` 外，系统还支持只存 Paxos 日志参与凑票仲裁而不存业务表数据的轻量见证副本（`witness replica`）。

Megastore 全文最精妙的协议设计，在于它怎样实现“**写走跨洲 Paxos 多数派，而强一致当前读（Current Read）在常态下 100% 走本机房本地直读、零跨洲 RTT**”！

仔细想一下这里的惊险矛盾：假设 3 个机房 A、B、C 组成 Paxos 组，一次写入在 A 和 B 两个机房赢下多数派投票后就已经持久提交；倘若此时直接给客户端返回成功，另一位连在机房 C 的用户立刻发起读取，而机房 C 还没收到这笔新日志，直接读 C 本地的 Bigtable 必然读到旧数据！为了堵死这道缝隙又不在每次读取时跨洋问询，Megastore 在每个机房驻留了一个内存状态守卫进程 `Coordinator`，专门维护“本机房哪些实体组此刻绝对追平了全局最新提交”，并把一次写入硬生生拆出了**持久提交点（Step 3）**与**对外可见点（Step 4）**两道水位（PDF p. 8）：

1. **Step 1–2（选最近主与打包日志）**：每次成功写入都会指定“离预期下次写入机房最近”的副本担任下一日志槽位的默认 Leader，提前省掉经典 Paxos 的第一阶段 Prepare 往返（预置 `proposal 0`）。
2. **Step 3（Paxos 多数派投票——持久提交点）**：并发向各机房发起 Accept 投票，一旦过半数副本落盘赞成，该事务在持久性意义上已不可撤销。但注意：**此刻严禁向客户端返回确认，也严禁刷入 Bigtable 暴露给读者！**
3. **Step 4（全量副本追平或失效守卫——可见性栅栏）**：写入驱动方继续确认所有持有数据的 `full replica` 是否均已收妥该日志；一旦某个远端机房 C 慢了或断联，写入方必须先发 RPC 将机房 C 本地 `Coordinator` 中该实体组的干净标记**显式置为失效（Invalidate）**（若连 `Coordinator` 进程都失联，则等待其在 Chubby 上的租约自然超时摘除）：

> "In our write algorithm, the commit point is after step 3 when the write has won the Paxos round, but the visibility point is after step 4. Only after all full replicas have accepted or had their coordinators invalidated can the write be acknowledged and the changes applied." *(PDF p. 8)*

只有跨过 Step 4，客户端才收到写入成功回执。此后任何落在健康机房的 `Current Read` 只需在本地内存瞄一眼 `Coordinator` 为真，便放心直读本机房 Bigtable。不妨用一个横跨美西（`US-West`）、美东（`US-East`）与欧洲（`EU`）的三机房场景追踪异常切换全过程：

- **常态快速写与本地读**：用户常驻 `US-West`，上次写入已将 `US-West` 指定为槽位 `n` 的默认 Leader。新写入发到 `US-West` 后直接发起 `Accept(slot=n, proposal=0)`，`US-West`、`US-East` 与 `EU` 并行落盘并返回确认，随后顺带刷入三地 Bigtable，全程仅需 **1 次跨洲广域网 RTT**；此后三地用户读该实体组均命中各自本地 `Coordinator`，零跨洋开销。
- **单机房抖动与 `Coordinator` 摘除**：若某次写入时 `EU` 海缆瞬断，`US-West` 与 `US-East` 组成 2/3 多数派在 Step 3 敲定提交；紧接着 Step 4 发现 `EU` 未响应，写入方立即向 `EU` 的 `Coordinator` 发送失效指令（若网络彻底不通则等待 `EU` 的 `Coordinator` 在 Chubby 锁服务上的失活租约超时自动放弃守卫权），随后才向 `US-West` 客户端返回成功。此时欧洲用户若发起强一致读，发现本地 `Coordinator` 标记缺失，便自动回退为跨洋多数派读取并触发日志补齐（Catchup），绝不暴露半点过期脏数据。
- **铁索连环限流（Chain Gang Throttling，PDF p. 9）**：生产运维中还暴露出一类微妙的次生灾害——当慢机房触发高频日志追赶读取时，如果追赶流量不加节制地打在健康多数派副本上，会把原本飞快的多数派磁盘也拖慢，整条实体组吞吐像栓在一根铁链上的囚徒一样向最慢节点看齐。Megastore 为此引入副本健康度路由与追赶限速，主动把病态慢副本踢出关键路径。

在 `Figure 10`（PDF p. 10）的生产监控中，这套“写时替读背负失效栅栏”的机制把百亿级强一致读的平均延迟死死压在 **~20 ms**，而写延迟保持在 100–400 ms 的单次跨洲 RTT 水平。正是对 Megastore“单实体组每秒仅能跑几次写、缺乏完整关系 SQL 与灵活跨组事务”痛点的反思，直接催生了 Google 下一代融合 Paxos 分片与原子钟的 Spanner。

---

## 广域网时空账本与彻底消除协调的格存储

Percolator 仰赖中心化 `TSO` 授时，后续的 Google Spanner 更是动用了昂贵的机房级 GPS 与原子钟（TrueTime API）并在每次事务提交时强行睡眠等待 \(2\epsilon\) 时钟不确定度窗口，才换来跨全球分片的外部一致性。对于绝大多数跑在普通公有云 NTP 时钟之上的数据库而言，有没有办法不买原子钟、不在提交时睡眠停顿，就能拿到贴近真实墙钟的因果一致快照？再进一步，常态跨机房复制到底有多少延迟是物理宿命，又能不能用代数结构把核间锁和跨节点等待一并清零？

![广域网一致性、混合逻辑时钟 HLC 与 Anna 无协调格合并谱系](/images/diagrams/paper-txn-pacelc-hlc-anna-spectrum.svg)

### Kulkarni 等（2014）：混合逻辑时钟（HLC）与免等待全局一致快照 {#paper-37}

- **论文来源**：Sandeep Kulkarni, Murat Demirbas, Deepak Madeppa, Bharath Avva, Marcelo Leone, *Logical Physical Clocks and Consistent Snapshots in Globally Distributed Databases*, 2014 · [原始 PDF（共 13 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/consistency/logical-physical-clocks-and-consistent-snapshots-in-globally-distributed-databases.pdf)（朴素时钟失控环路见 `Figure 3–4` PDF p. 4；`HLC` 四分支算法与恢复轨迹见 `Figure 5–6` PDF p. 5；`c` 分量分布与 64 位位宽编码见 `Figure 7–12` PDF pp. 9–11）。

如果不用中心化 `TSO`，只用普通服务器的 NTP 物理时钟 `pt` 给分布式多版本事务打时间戳，机器间几毫秒到几十毫秒的晶振偏置会导致因果倒置：\(T_2\) 明明读到了 \(T_1\) 的写入才更新，却因为所在机器时钟慢了 5 ms 而领到比 \(T_1\) 更小的时间戳。反过来，如果天真地套用 Lamport 逻辑时钟混合公式 `l = max(l + 1, pt)`，一旦集群里某台机器的时钟短暂往前跳了一下（比如真实时间 `2` 时跳到 `10`），哪怕它的 NTP 下一秒就修好了，其他两台高频互发消息的节点也会在每次收发时执行 `+1`，把逻辑时间推向 `11, 12, ... 50000`，从此彻底与物理墙钟脱钩（`Figure 4`，PDF p. 4）！

Kulkarni 等人提出的**混合逻辑时钟（Hybrid Logical Clock, HLC）**用一次漂亮的关注点分离根治了失控漂移：将单节点时间戳拆成按字典序比较的二元组 `(l, c)`——其中 **`l` 只负责记忆因果历史上见过的最大物理时钟读数 `pt`（绝不对其做 `+1` 自增！）**，而 **`c` 仅在物理时钟还没往前走动却连续发生因果事件时充当平局决胜计数器**：

```text
// 本地事件或发送消息（当前本地物理钟读数为 pt.j）
l'_j := l_j
l_j  := max(l'_j, pt.j)
if l_j == l'_j then c_j := c_j + 1 else c_j := 0

// 接收携带时间戳 (m_l, m_c) 的消息
l'_j := l_j
l_j  := max(l'_j, m_l, pt.j)
if      (l_j == l'_j) and (l_j == m_l) then c_j := max(c_j, m_c) + 1
else if (l_j == l'_j)                  then c_j := c_j + 1
else if (l_j == m_l)                   then c_j := m_c + 1
else                                        c_j := 0   // pt.j 独占最大，c 立即清零！
```

拿一段 5 步乒乓消息序列对比“朴素 `max(l+1, pt)`”与“`HLC (l, c)`”在遭遇节点 A 时钟瞬时尖峰（`pt = 10`）后的自愈表现，高下立判：

| 步骤与事件流向 | 真实墙钟 | 触发节点本地 `pt` | 朴素混合时钟 `l` | `HLC (l, c)` 状态 | 关键机制解读 |
| :--- | :---: | :---: | :---: | :---: | :--- |
| **1. 节点 A 本地写（时钟故障尖峰）** | `2` | `10` | `10` | `(10, 0)` | `pt=10` 刷新 `l`，`c` 归零 |
| **2. A 发消息至节点 B** | `3` | `3` | `11` | `(10, 1)` | B 的 `pt=3 < 10`，`l` 锁在 `10` 不涨，仅 `c` 增至 `1` |
| **3. B 回消息至节点 C** | `4` | `4` | `12` | `(10, 2)` | 继续靠 `c` 编码 happens-before 因果偏序 |
| **4. C 再发 100 条消息给 B** | `5..9` | `5..9` | `112`（彻底跑偏） | `(10, 102)` | 无论收发多少轮，物理估计分量 `l` 纹丝不动 |
| **5. 墙钟跨过 `11`，B 发生新事件** | `11` | `11` | `113`（永不归位） | **`(11, 0)`** | `pt=11 > 10` 命中 `else` 分支，污染瞬间清零自愈！ |

这一设计直接给出了两条硬核数学保证（PDF p. 5）：第一，严格保持单向因果单调性 \(e \;\text{hb}\; f \Rightarrow \text{HLC}(e) < \text{HLC}(f)\)；第二，任何时刻若 \(l.f > pt.f\)，因果前驱上必有某个已发生事件的真实物理读数恰好等于 \(l.f\)，从而只要全网 NTP 时钟误差有界于 \(\epsilon\)，\(|l.f - pt.f| \le \epsilon\) 恒成立：

> "Theorem 3. l.f denotes the maximum clock value that f is aware of. In other words, l.f > pt.f ⇒ (∃g : g hb f ∧ pt.g = l.f)" *(PDF p. 5)*

为了让 HLC 无缝塞进现有数据库定长 64 位时间戳字段，论文第 6 节（PDF p. 11）将 64 位 NTP 物理时间**向上取整（Rounding up）**至高 48 位赋给 `l`（保持微秒级物理分辨率），低 16 位赋给 `c`：

> "This scheme involves restricting l to track only the most significant 48 bits of pt in the HLC algorithm presented in Figure 5. Rounding up pt values to 48 bits l values still gives us microsecond granularity tracking of pt." *(PDF p. 11)*

向上取整而非简单右移截断，是为了防止截断后的 48 位数值小于真实 `pt` 而破坏 \(l \ge pt\) 的理论根基；在 AWS 跨区实测中，超过 99% 的事件 `c <= 4`，16 位空间绰绰有余。借助 HLC，全局快照读无需任何跨节点锁或提交等待，直接取各分片满足 \(\text{HLC} \le (T, 0)\) 的最新版本即可保证任意跨分片因果关系不撕裂（`Figure 12` PDF p. 10）。同时也要分清 HLC 与 TrueTime 的能力边界（PDF p. 11）：如果客户端甲写完分片 1 后通过数据库外部渠道（没有携带 HLC 消息头）通知客户端乙去写分片 2，而分片 2 所在机器物理钟恰好慢了 \(\epsilon\)，单靠 HLC 并不能自动感知这条带外因果链——因此 CockroachDB 等采用 HLC 的数据库在处理严格外部一致性读取时，仍会在遇到 `[read_ts, read_ts + max_offset]` 不确定窗口内的版本时触发重启重读。

### Abadi（2012）：PACELC——为什么 99.9% 的分布式延迟付在常态路径上 {#paper-36}

- **论文来源**：Daniel J. Abadi, *Consistency Tradeoffs in Modern Distributed Database System Design*, IEEE Computer, Feb 2012 · [原始 PDF（共 6 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/consistency/consistency-tradeoffs-in-modern-distributed-database-system-design.pdf)（CAP 常态误区剖析见 PDF pp. 1–2；故障发生 vs 故障可能性及三类复制拓扑见 PDF pp. 3–4；PACELC 主流系统分类见 PDF pp. 4–6）。

为什么在 NoSQL 与 NewSQL 混战年代，无数系统打着“CAP 定理证明了不能兼得，所以我们放弃强一致性”的旗号，却在机房网络明明毫发无损的日常时段里展现出天差地别的读写延迟？Abadi 在这篇仅 6 页的犀利宏文中指出了概念偷换的症结：**CAP 只刻画了网络真正断裂形成孤岛分区（Partition）那一刻的极端抉择，对平时 99.9% 网络连通时的系统能力没有任何数学限制！**

> "In reality, CAP only posits limitations in the face of certain types of failures, and does not constrain any system capabilities during normal operation." *(PDF p. 1)*

那日常运行中的高延迟究竟是谁逼出来的？不是“**故障已经发生（Failure Occurrence）**”，而是“**害怕故障可能发生（Failure Possibility）**”！只要业务不能接受单台机器或单个机房起火导致数据永久灭失，系统就不得不在风平浪静的每一秒钟把数据复制到远端节点；而一旦常态路径存在多份副本，每一次写与读就必须直面 **延迟（Latency, `L`）与一致性（Consistency, `C`）** 的常态二选一：

> "(Note the important difference between this tradeoff and the CAP tradeoffs: while the occurrence of a failure causes the CAP tradeoffs, the failure possibility itself results in this tradeoff.)" *(PDF p. 3)*

Abadi 将这一完整账本凝练为 **PACELC** 框架——`If there is a Partition (P), how does the system trade off Availability and Consistency (A and C); Else (E), when the system is running normally in the absence of partitions, how does the system trade off Latency and Consistency (L and C)?`——并在归类前先澄清了一个在 Dynamo 派生系统中极易踩中的认知误区（PDF pp. 3–4）：**光靠配置读写重叠 \(R + W > N\)，在没有预协调协议的情况下依然换不来严格线性一致性！**

不妨用 3 副本 \(\{A, B, C\}\)、配置 \(N = 3, W = 2, R = 2\)（满足 \(2 + 2 > 3\)）推演一次常态无分区交错（初始值均为 \(v_0\)）：

1. 写客户端并发向 \(A, B, C\) 发送新值 \(v_1\)（尚未凑齐 \(W = 2\) 返回）；
2. 消息率先抵达 \(A\) 落盘，发往 \(B, C\) 的网络包仍在传输途中；
3. 此时读者 \(R_1\) 抽中 \(\{A, B\}\) 读取，看到 \((v_1, v_0)\)，取时间戳较新的 **`v1`** 返回给用户；
4. 紧接着 1 毫秒后，读者 \(R_2\) 抽中 \(\{B, C\}\) 读取（且不带同步读修复写回），两节点此时都还是旧值，于是 \(R_2\) 理直气壮地返回 **`v0`**！

在完全没有网络分区的正常日子里，\(R_2\) 在实时顺序上明明晚于 \(R_1\) 启动，却读到了时光倒流的旧值 \(v_0\)！要想堵住这个窗口，要么由读修复先做同步回写（额外增加读延迟），要么在写端走真正意义上的两阶段或共识协调。看透这一层后，主流分布式存储的四象限坐标便清晰无比（PDF pp. 4–6）：

| PACELC 象限 | 分区发生时 (`P`) | 无故障常态下 (`E`) | 代表系统与底层机制 | 容易踩坑的工程细微处 |
| :--- | :--- | :--- | :--- | :--- |
| **`PA / EL`** | 选可用性 `A`（孤岛照常接单） | 选低延迟 `L`（异步复制立返） | Dynamo, Cassandra, Riak | 即便调成 \(R + W > N\)，并发写未落齐窗口内先后两次读仍可能先见新值后见旧值（非严格线性一致） |
| **`PC / EC`** | 选强一致 `C`（少数派拒服） | 选强一致 `C`（同步多数派/锁） | Bigtable/HBase, VoltDB, **Megastore** `#54`, Spanner | 常态写必须吞下跨节点或跨洲复制 RTT（Megastore 靠失效栅栏把 `EC` 读延迟救回本地） |
| **`PC / EL`** | 选强一致 `C`（防版本分叉） | 选低延迟 `L`（本地主写异步传） | Yahoo! PNUTS | 常态写交本地记录主立即返回，远端容忍读略旧时间线；主所在机房断联则宁停写也不许乱序抢主 |
| **`PA / EC`** | 偏向可用性 `A`（旧主曾签收可回滚） | 选强一致 `C`（单主收口无脏读） | 早期默认配置的 MongoDB | 常态读写全走单 Primary 无撕裂；但默认不等多数派落盘即返回，主切换重连时未同步写会被剥进 `rollback` 目录 |

最后一行对早期 MongoDB 回滚风险的精准点穴，恰好构成了下一篇 `#35` Ark 共识协议登场的直接导火索。

### Wu 等（2018）：Anna——以无共享 Actor 与 ACI 半格通吃任意规模 {#paper-34}

- **论文来源**：Chenggang Wu, Jose M. Faleiro, Yihan Lin, Joseph M. Hellerstein, *Anna: A KVS For Any Scale*, ICDE 2018（扩展技术报告版）· [原始 PDF（共 18 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/consistency/anna%3A-a-kvs-for-any-scale.pdf)（无协调架构与分类表见 Table I PDF p. 6；发送端结合律批合并与 C++ 格组合见 PDF pp. 8–11；安全删除 GC 与 32 核压测反转见 `Figure 6–7` PDF pp. 12–13）。

如果我们在 `PACELC` 谱系上果断迈向 `PA/EL` 一侧（业务无需跨行强串行化，只需因果一致性、读已提交或单调读），能不能顺手把 `#67` Abyss 揭示的单机多核缓存行争用也连根拔起？

UC Berkeley 团队打造的 **Anna** 把“零协调（Coordination-Free）”推向了极致：**单台 32 核服务器内部的 32 个线程之间不共享任何业务内存，连一把自旋锁、一个共享原子计数器或一次无锁 `CAS` 重试都不允许存在！** 每个核独占一个一致性哈希分片 Actor，核间交互与跨服务器交互完全同构，统统走异步消息多播：

> "each thread is always doing useful work (serving requests), and never waiting for other threads for reasons of consistency or semantics. To that end, coordination techniques such as locking, consensus protocols or even “lock-free” retries [7] need to be avoided." *(PDF p. 4)*

全异步乱序发消息，怎样保证不同副本合并并发写入时不丢数据、不分叉？Anna 将存储引擎底层的 Value 类型统一约束为数学上的**有界并半格（Bounded Join-Semilattice）** \((S, \sqcup, \bot)\)，要求合并函数 \(\sqcup\) 严格满足**结合律（Associativity）**、**交换律（Commutativity）**与**幂等律（Idempotence）**（简称 **ACI**）：

$$\forall a, b, c \in S:\quad (a \sqcup b) \sqcup c = a \sqcup (b \sqcup c), \quad a \sqcup b = b \sqcup a, \quad a \sqcup a = a$$

别小看这行简单的代数公理，它在单机多核高争用场景下直接改写了硬件扩展定律（`Figure 6`，PDF p. 13）。当 32 核压测跑在极端倾斜的 `Zipf = 4` 负载下（100 万次读写几乎全砸在 34 个超热键上），共享内存架构的王者 `Masstree` 与 `Intel TBB` 因 32 个核轮番修改同一批缓存行，L3 缓存未命中率飙升，吞吐暴跌；而 Anna 将热键复制到全部 32 个线程本地，每个线程在自己 L1/L2 缓存内零等待读写本地副本，并在异步广播前利用**结合律**把积攒的成百上千次更新就地压缩成一个格状态：

> "Hence batches of associative updates can be merged at a sending replica without affecting results; merging at the sender can dramatically reduce communication overhead for frequently-updated hot keys" *(PDF p. 8)*

热点越集中，本地批折叠率越高，核间消息越少，Anna 在最高争用点反而跑出了碾压共享内存无锁引擎数量级的吞吐！而在无热点的均匀负载（`Zipf = 0.5`，`Figure 7` PDF p. 13）下，论文也坦诚展示了盲目全核复制会让后台 gossip 消息处理吃掉 69%–92% 的 CPU，由此引出按访问热度动态伸缩副本数的选择性复制机制。

更令人拍案叫绝的是 Anna 如何用不到几百行 C++ 模板组合出跨越多级弱隔离的丰富语义（`Listing 1`，PDF pp. 9–12）。所有状态类型均继承统一的 `Lattice<T>` 基类，通过覆写纯虚函数 `do_merge` 将底层简单格逐级复合为高阶一致性容器：

```cpp
template <typename T> class Lattice {
 protected:
  T element_;
  virtual void do_merge(const T &e) = 0; // 子类仅需实现满足 ACI 的单调合并核
 public:
  void merge(const Lattice<T> &rhs) { do_merge(rhs.reveal()); }
};

// 1. 单调上界格：取 max(a, b)，用作时间戳与版本计数器
class MaxLattice : public Lattice<int> {
  void do_merge(const int &e) override { element_ = std::max(element_, e); }
};
// 2. 映射复合格：对交集键递归调用值类型自身的 merge()，一键拼出向量时钟！
template <typename K, typename V> // V 本身亦是某个 Lattice 子类
class MapLattice : public Lattice<std::unordered_map<K, V>> {
  void do_merge(const std::unordered_map<K, V> &m) override {
    for (const auto &[k, v] : m) element_[k].merge(v);
  }
};
```

靠这几块最小代数积木，原本需要重写整套并发控制器的隔离需求变成了服务端格类型与无状态客户端代理（Client Proxy）的协同拼装：

- **最终一致性（Last-Writer-Wins）**：用时间戳 `MaxLattice<TS>` 与值配对构成 `LWWPairLattice`，时间戳严格全序大者胜出；
- **多键因果一致性（Causal Consistency）**：用 `MapLattice<NodeId, MaxLattice<int>>` 拼出向量时钟，与并发值集合 `SetLattice<V>` 组合成因果对格，客户端代理在会话上下文中维护已观测因果历史，保证因果后继更新绝不会先于前驱暴露；
- **读已提交（Read Committed，对应 Adya `PL-2`）**：为了禁止脏读（`G1a`/`G1b`），客户端代理将未提交事务的所有 `PUT` 先暂存在本地私有缓冲区中，直到收到 `COMMIT` 指令才打成整批已提交格状态异步刷向存储节点；服务端存储的 `SetLattice` 只包含已提交事务产出的版本，从根源上消灭了未提交脏值外泄；
- **单键切面隔离与读己之所写（Item Cut Isolation / Read-Your-Writes）**：如果同一事务先后读两次键 \(K\)，怎样在不给服务端加读锁的前提下避免不可重复读？客户端代理只需在首次读取 \(K\) 时将其返回值钉入本地事务读缓存（Read Cache），同一事务后续再读 \(K\) 直接命中本地缓存（合并本地未提交写缓冲即自动获得 Read-Your-Writes），无需服务端感知任何活跃事务生命周期；
- **防诈尸物理删除回收（Watermark GC，PDF p. 12）**：在只增不减的单调半格里，如果某个副本收到 `DELETE(K)` 就把内存里的键直接抹掉（退回最小元 \(\bot\)），网络上任何一条迟到的旧更新 \(v_{\text{old}}\) 一旦随后抵达，执行 \(\bot \sqcup v_{\text{old}} = v_{\text{old}}\) 就会让已删数据当场“诈尸”复活！Anna 为此设计了优雅的双层水位回收：要求节点间走有序点对点信道，每台线程周期性广播自己已收齐的各节点发送序号向量，并用取逐维最小值的下界格算出“全集群所有副本均已收妥的安全水位线”；只有当删除墓碑的因果时间戳彻底沉入该安全水位线之下（意味着宇宙间再也不可能有比墓碑更老的在途乱序包），各线程才把墓碑连同历史数据物理擦除。

研读论文 `Listing 2`（PDF p. 10）给出的 `CausalPairLattice` 教学代码时，还可以亲手验证一个深刻的代数边界：若只按字面代码用单个合并后的向量时钟去吞并被偏序支配的旧对（偏序较小者直接丢弃，不可比并发者将向量取逐维 `max`、值集取并集），令 \(x = ((1,0), \{a\})\)、\(y = ((0,1), \{b\})\)、\(z = ((2,0), \{c\})\)：
- 先算左结合 \((x \sqcup y) \sqcup z\)：\(x, y\) 并发合并为 \(((1,1), \{a,b\})\)，再与 \(z((2,0), \{c\})\) 合并时两者仍不可比，得 **`((2,1), {a,b,c})`**；
- 再算右结合 \(x \sqcup (y \sqcup z)\)：\(y, z\) 并发合并为 \(((2,1), \{b,c\})\)，随后与 \(x((1,0), \{a\})\) 比较时因 \((2,1) > (1,0)\) 直接把 \(x\) 吞掉，得 **`((2,1), {b,c})`**！

结果相差了一个旧并发值 `a`！原因在于 \(z\) 虽然取代了同源的 \(x\)，但把 \(y\) 和 \(z\) 的向量粗暴取 `max` 后，伪造出了“新状态既支配 \(x\) 又包含 \(y\)”的假象。在真实工业级实现（如 Dotted Version Vectors）中，每个未消解值必须各自携带精确的产生点（Dot）与已观测因果集，才能让复合算子严格闭合结合律——这也从反面印证了 Anna 核心思想的分量：**一旦把分布式一致性交给代数定律兜底，每一行合并函数的结合律证明就是系统的生命线。**

---

## 从单机多核到全球分布的演进脉络与选型坐标

合上这七篇跨越近二十年的经典文献，现代分布式数据库的架构取舍不再是一团模糊的经验口诀，而是一套环环相扣的物理与数学推演：

1. **用 `DSG` 冲突图代替锁教条（`#65` Adya）**：评估任何存储引擎的隔离承诺，只需追问它允许哪类依赖边成环——只禁 `ww` 环是 `PL-1`，禁脏读与 `ww/wr` 环是 `PL-2`（读已提交），禁写写冲突却放行 `rw` 双边环是快照隔离（如 Percolator），连按项和谓词 `rw` 环一并斩断才是真正的 `PL-3` 可串行化。
2. **警惕千核下“看似只读或轻量”的全局共享状态（`#67` Abyss & `#34` Anna）**：单点原子授时器、锁表桶闩锁乃至元组上的 `last_read_ts` 标记，在百核以上都会退化为缓存一致性风暴。要么去中心化授时，要么像 Anna 那样在弱隔离场景切向核间无共享 Actor + 发送端结合律折叠，把高争用热点转化为本地压缩红利。
3. **跨分片与跨机房靠“单点原子翻牌 + 读写责任不对称转移”破局（`#66` Percolator & `#54` Megastore）**：Percolator 把跨行分布式提交浓缩在 `Primary Lock` 擦锁写 `write` 的单行原子跃迁上，让任意后来读者都能无状态自愈清理；Megastore 则按 `Entity Group` 拆碎广域网 Paxos 锁竞争，并用写路径 Step 4 的 `Coordinator` 失效栅栏换回读路径零跨洲通信。
4. **在 `PACELC` 常态税下用 `HLC` 锚定因果快照（`#36` Abadi & `#37` Kulkarni）**：防机房掉电的复制需求注定了 99.9% 的无故障时间都受制于 `Else: L vs C` 权衡；引入 64 位 `HLC (l, c)` 则让无原子钟的普通云集群免除了中心化 `TSO` 跨区取戳与提交停顿，在 NTP 误差界 \(\epsilon\) 内低成本截取全局因果一致快照。

将这套坐标系投射到今天的工业级分布式数据库中，主流系统的核心架构选型几乎就是上述论文组件的排列组合：

| 现代代表系统 | 隔离语义定位 (`#65`) | 跨行事务提交模型 (`#66`) | 分片复制与本地读优化 (`#54`) | 全局授时与 `PACELC` 坐标 (`#37`/`#36`) |
| :--- | :--- | :--- | :--- | :--- |
| **TiDB / TiKV** | 默认快照隔离 SI，可选悲观锁与可串行化校验 | 直接继承 **Percolator** `Primary Lock` + 异步锁清理与并行一阶段优化 | 按 Key Range 切分独立 Multi-Raft 复制组，支持 Follower Read | 高可用批分配中心 **`TSO`**（单区域低延迟，跨洲部署受限于取戳 RTT），属 **`PC/EC`** |
| **CockroachDB** | 默认 `PL-3` 可串行化（SSI 追踪读写时间戳防 `rw` 环） | 类 Percolator 事务记录（Transaction Record 充当单点原子提交锚点） | Range 级 Raft 复制 + Leaseholder 租约守卫本地强一致读 | **64 位 `HLC`** 免原子钟去中心化授时 + 不确定窗口重读，属 **`PC/EC`** |
| **Google Spanner** | 外部一致性（严格可串行化，超 `PL-3` 实时序） | 跨 Paxos 组两阶段锁 / 快照读 + 协调者两阶段提交 | 继承 **Megastore** 细粒度跨洲 Paxos 分片基因，升级全关系 SQL | GPS + 原子钟 **TrueTime** 配合 \(2\epsilon\) 提交等待，属 **`PC/EC`** |
| **YugabyteDB** | 支持快照隔离 SI 与 `PL-3` 可串行化两档 | 分布式事务状态表（单点原子翻转状态）+ 意图锁（Intents）写入 | Tablet 级 Raft 复制 + Leader 租约本地直读 | **`HLC` 混合逻辑时钟**驱动跨节点多版本快照，属 **`PC/EC`** |
| **Anna / Riak CRDT** | 因果一致、`PL-2` 读已提交、单键切面（弱于 SI） | 无两阶段提交，客户端代理本地缓存批量异步刷出 | 一致性哈希分片 + 核间无共享 Actor + 按热度弹性扩缩副本 | 向量时钟 / 点因果上下文 + **ACI 半格合并**，属极致 **`PA/EL`** |

当我们把分布式事务拆到最底层——无论是 Megastore 的实体组日志，还是 TiKV、CockroachDB 底层成千上万个数据分片复制组——最终都依赖一个能在丢包、乱序与节点随时宕机的异步网络中绝不脑裂的共识内核。下一篇（也是本十篇系列的收官篇），我们将潜入 Paxos 泛化定理、Raft 安全性反例与生产级拉式复制协议的腹地。

---

## 参考文献与阅读基线

本文精读的所有论文均对齐 `lonng/db-papers` 仓库基线（Commit `4ff1331`），原始 PDF 与核心图表索引汇总如下：

| 序号 | 论文标题与作者 | 发表年份与会议/期刊 | 仓库原始 PDF 与精读重点页码 |
| :--- | :--- | :--- | :--- |
| `#65` | *Generalized Isolation Level Definitions* — A. Adya, B. Liskov, P. O'Neil | ICDE 2000 | [原始 PDF（共 12 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/transaction/generalized-isolation-level-definitions.pdf) · Fig. 1 锁表 p. 3；版本序与谓词集 pp. 5–7；`DSG` 与 `PL-1..3` pp. 8–11 |
| `#67` | *Staring into the Abyss: An Evaluation of Concurrency Control with One Thousand Cores* — X. Yu et al. | PVLDB 2014 | [原始 PDF（共 13 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/transaction/staring-into-the-abyss%3A-an-evaluation-of-concurrency-control-with-one-thousand-cores.pdf) · Graphite 架构 pp. 4–5；Fig. 6–7 授时陷阱 p. 7；Fig. 8–17 YCSB/TPC-C pp. 8–11 |
| `#66` | *Large-scale Incremental Processing Using Distributed Transactions and Notifications (Percolator)* — D. Peng, F. Dabek | OSDI 2010 | [原始 PDF（共 14 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/transaction/large-scale-incremental-processing-using-distributed-transactions-and-notifications.pdf) · Fig. 4–6 三列布局与伪代码 pp. 4–5；惰性恢复 p. 6；Fig. 8 微基准 p. 10 |
| `#54` | *Megastore: Providing Scalable, Highly Available Storage for Interactive Services* — J. Baker et al. | CIDR 2011 | [原始 PDF（共 12 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/rdbms/megastore%3A-providing-scalable%2C-highly-available-storage-for-interactive-services.pdf) · Fig. 1–2 实体组 p. 3；Fig. 5 与读写栅栏 pp. 7–8；生产延迟 p. 10 |
| `#37` | *Logical Physical Clocks and Consistent Snapshots in Globally Distributed Databases (HLC)* — S. Kulkarni et al. | Tech Report / OPODIS 2014 | [原始 PDF（共 13 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/consistency/logical-physical-clocks-and-consistent-snapshots-in-globally-distributed-databases.pdf) · Fig. 3–6 算法与定理 pp. 4–5；Fig. 7 `c` 分布 p. 9；64 位编码与快照 pp. 10–11 |
| `#36` | *Consistency Tradeoffs in Modern Distributed Database System Design (PACELC)* — D. J. Abadi | IEEE Computer 2012 | [原始 PDF（共 6 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/consistency/consistency-tradeoffs-in-modern-distributed-database-system-design.pdf) · CAP 误区 pp. 1–2；故障可能性与三类路由 pp. 3–4；PACELC 矩阵 pp. 4–6 |
| `#34` | *Anna: A KVS For Any Scale* — C. Wu, J. M. Faleiro, Y. Lin, J. M. Hellerstein | ICDE 2018 | [原始 PDF（共 18 页）](https://github.com/lonng/db-papers/blob/4ff1331069c7cf52ac97dd769e5f484c1c35c200/papers/consistency/anna%3A-a-kvs-for-any-scale.pdf) · 无共享线程 p. 4；发送端结合律合并 p. 8；格组合与 GC pp. 9–12；Fig. 6–7 压测 p. 13 |
