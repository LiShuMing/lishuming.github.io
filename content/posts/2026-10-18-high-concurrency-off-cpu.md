---
title: "【排查】高并发查询为什么打不满 CPU：一次连续的 Off-CPU 排查"
date: 2026-10-18T00:00:00+08:00
lastmod: 2026-10-18T00:00:00+08:00
slug: "high-concurrency-query-off-cpu"
categories:
  - 工程排查
tags:
  - Off-CPU
  - 性能排查
  - Pipeline
  - StarRocks
  - eBPF
description: "历史案例：高并发小查询 CPU 打不满时，如何用 Profile、Off-CPU 堆栈与系统计数器连成证据链；标明版本与复现缺口，不做新实验。"
---

> **读完带走**
>
> - 并发高而 CPU 不忙，优先问「线程离开 CPU 后时间花在哪」，而不是先加线程。
> - Off-CPU 累计等待是**采样窗口内多线程合计**，不能直接当成单条查询的墙钟；宽栈是入口，不等于锁持有时间。
> - 本组历史记录里，热点依次落在对象生命周期、上下文二次查找 / 分片哈希、以及观测设施收尾——都在算子热路径外围。
> - 本文**不做新实验**；修复前后吞吐与尾延迟仍缺完整记录，结论止于「机制与排查方法」，不外推为当前版本缺陷。

> 相关阅读：[DOP 三次决策]({{< relref "2026-09-12-dive-dop.md" >}}) · [Hash Join]({{< relref "2026-08-26-dive-hash-join.md" >}})

## 案例边界（先读）

本文整理自一组 **StarRocks 历史排查笔记**（Notion 成稿，2026-10-14 整理；原始负载未在本稿复跑）。

| 项 | 本稿状态 |
|---|---|
| 场景数字（2048 并发、1 FE / 3 BE、每 BE 16 vCPU、CPU ≈30%） | 来自原笔记观测，**仅描述当时条件** |
| Off-CPU 采样（约 30s；某线程 `~RuntimeState` 路径累计约 25.36s） | 该次采样的等待证据，**不是**所有请求同时长阻塞 |
| 公开 PR [#13373](https://github.com/StarRocks/starrocks/pull/13373)、[#13593](https://github.com/StarRocks/starrocks/pull/13593)、[#13604](https://github.com/StarRocks/starrocks/pull/13604) | 补丁意图可核对；**未**对照当前主干再测 |
| 修复前后吞吐 / 尾延迟 / CPU | **缺失** → 见文末 TODO，**不编造** |
| 飞书原图（火焰图等） | 内网链接，本稿**不嵌入**；待替换为可公开证据 |
| StarRocks / Linux / BCC / perf 精确版本与采样机型 | **未写入原笔记成稿** → TODO |

**主张：** 低 CPU 利用率至少有两种解释——缺少可运行工作，或可运行工作被锁 / I/O / 调度挡住。Profile 缩查询内范围，Off-CPU 辨具体等待，系统计数器捕「短而频繁」的同步；每修一处热点，须回到同负载重采。  
**不适用：** 把历史案例当成当前产品缺陷；用单张火焰图消失代替收益评估；把 Off-CPU 累计秒数直接写成「查询变慢了 N 秒」。

---

查询慢、并发已经很高、CPU 却不忙，很容易把排查推向「再加线程」。增加参与竞争的请求，未必增加真正执行的工作。更有效的入口是：执行线程没有运行时，时间究竟花在哪里？

下文把查询 Profile、Off-CPU 堆栈与系统计数器连成一条证据链。数字一律来自原笔记；相关修改已对照公开 PR 的补丁意图。**没有重新执行原负载，也没有补造修复后的吞吐。**

## 1. 先分清「正在计算」和「等待运行」

On-CPU 看线程实际跑了什么；Off-CPU 看离开 CPU 之后过了多久——可能是锁、I/O、条件变量，也可能是可运行却轮不到的调度等待。宽大的等待栈只是定位入口，不能直接等同于锁的持有时间。

BCC 的 [offcputime](https://github.com/iovisor/bcc/blob/master/tools/offcputime.py) 在调度切换处记录并累计时间，不是只盯某一个阻塞系统调用。多线程累计等待可以超过采集窗口：多个线程同时等时，总量会叠上去。

因此低 CPU 至少有两种读法：线程缺少可运行工作，或可运行工作被资源与调度约束挡住。Profile 缩范围，系统工具辨原因。

## 2. 一个高并发小查询案例

原记录场景：**2048 并发、1 FE、3 BE，每 BE 16 vCPU**；查询只有一个 Fragment；**CPU 利用率约 30%**。Profile 里 ScheduleTime 与 PendingTime 占比较高。这些是当时观测，**不能推广为其他版本的固有行为**。

对一个 Pipeline 执行线程采样后，等待堆栈指向 `RuntimeState` 析构路径。原记录一次约 30 秒采集中，该路径累计约 **25.36 秒**——是该采样的等待证据，不是「每个请求都卡这么久」。

可以从进程级采集开始，再收窄到可疑线程（工具路径、权限与符号按环境确认）：

```bash
# 进程级折叠堆栈，采集 30 秒
sudo /usr/share/bcc/tools/offcputime -df -p <pid> 30 > offcpu.stacks

# 已定位到可疑执行线程后，观察其等待路径
sudo /usr/share/bcc/tools/offcputime -d -t <tid> 30
```

不要先追求漂亮的火焰图。先确认目标进程、线程类型、时间窗口和单位，再看最宽路径是否与业务症状一致。

> **TODO（图）：** 原飞书 Off-CPU 火焰图无法作为公开附件嵌入；定稿前需换成可公开的采样截图或折叠栈摘要。

## 3. 第一个瓶颈：对象生命周期碰到全局资源池

当时每个 Fragment Instance 持有 `RuntimeState`，初始化 / 析构需要注册或注销资源池。高并发小查询不断创建、销毁这些对象，使生命周期管理中的共享锁成为密集访问点。

[PR #13373](https://github.com/StarRocks/starrocks/pull/13373) 删除不再需要的 ResourcePool 机制；公开补丁可见 `RuntimeState` 注册 / 注销逻辑被移除。设计启示是：适合长任务的资源管理，在大量短任务下可能主要贡献管理成本。

这**不**等于应普遍删除资源管理。需要先证明被删机制已无必要职责，再确认替代路径仍能承担约束。减少一次锁竞争，和取消一种管理功能，是不同改动。

## 4. 下一个瓶颈：重复查找和分片分布

去掉第一处热点后，**重新采样**才看到 `ScanOperator` 首次取 `QueryContext` 的竞争：Fragment 准备阶段已经取得上下文，执行阶段再次访问全局管理器，带来可避免的查找与读锁。

[PR #13593](https://github.com/StarRocks/starrocks/pull/13593) 将上下文在准备阶段传给 `ScanOperator`，并修改 `QueryContextManager` 的分片哈希。PR 样本显示，原键与哈希低位没有把短时间产生的 ID 均匀散到槽位；补丁改变了哈希输入与混合方式。

两项改动层次不同：提前传递上下文减少热路径访问；改善分片分布降低剩余共享访问扎堆到同一槽位的概率。不能因为「已经分片」就假设竞争已被摊开。

原笔记中，消除 `query_ctx_mgr.get()` 读锁之后，还曾观察到 `PipelineDriver::finalize` 路径上的写锁竞争；与分片哈希问题一并在同一轮改动脉络中处理。堆栈摘要见文末附录。

## 5. 再看收尾：观测设施也可能制造竞争

排查继续进入 `RuntimeProfile` 销毁路径。原记录发现，删除计数器时会访问全局 Profile 状态；高并发下，小查询的收尾也会争用共享锁。

[PR #13604](https://github.com/StarRocks/starrocks/pull/13604) 删除未使用的全局 RuntimeProfile 机制，并为仍需要的吞吐计算改用其他计数方式。公开补丁支持「移除了相关同步与更新逻辑」，**没有**给出可独立复核的完整性能提升幅度。

可观测性也需要成本模型：创建、注册、更新、销毁指标，都可能进入请求生命周期。统计设施还在用吗？能否从已有计数派生？高并发下是否触碰全局状态？这些问题和算子自身优化同样值得查。

> **注意：** 原笔记中一处 `~RuntimeProfile` / `shared_ptr` 相关采样出现**异常偏大的累计计数**；成稿阶段已将其标为待核对，**本稿不引用该数字作定量结论**。

## 6. 为什么 Off-CPU 图还不够

原笔记另有一个**独立**案例：同一查询在两个版本中分别约 **1.5s** 与 **1.2s**，差异落在聚合附近。单看 CPU 热点与等待栈不易解释；系统计数显示上下文切换频率约每秒 **70 万** 与 **20 万** 次，记录将其归因于执行线程与 I/O 线程竞争任务队列。

该案例适合说明：短而频繁的同步，可能比一次长等待更难从 Off-CPU 宽栈里读出来。但切换次数只是线索，不能单独证明因果——应对齐线程数、负载和采样窗口，再定位具体队列与锁。

```bash
perf stat -p <pid> -- sleep 10
perf record -e cpu-clock -g -p <pid> -- sleep 10
```

同理，缓存状态、I/O 与机器基准也会干扰判断。原资料还记录过：查询耗时随 Page Cache 条件变化，以及机器基准差异导致「看起来像代码回退」。排查前先排除环境改变，否则代码修改可能只是碰巧换了测量条件。

> **边界：** 1.5s / 1.2s 案例与上文高并发小查询案例**条件不同**，不得合并成「同一组优化的收益数字」。

## 7. 把一次排查变成可以复用的方法

| 问题 | 证据入口 | 需要避免的误判 |
|---|---|---|
| 查询内哪里耗时 | Profile 与执行阶段 | 不同计时器可能重叠，不能机械相加 |
| 线程为什么离开 CPU | Off-CPU 堆栈与线程状态 | 累计等待 ≠ 单次查询耗时 |
| 短同步是否频繁发生 | 上下文切换与队列路径 | 计数相关 ≠ 已证明因果 |
| 修改是否有效 | 同负载的吞吐、尾延迟、CPU、等待 | 一张热点消失的图 ≠ 完整收益 |
| 结果能否外推 | 版本、机器、并发、缓存与 SQL | 历史案例 ≠ 当前缺陷 |

性能排查的关键是**重复测量**和**缩小假设**。每修复一个热点，就回到相同负载重新采样；保留被否定的解释，比只记下最终修改更能帮助后来者。

对本组历史资料，最可靠的结论是：低 CPU 利用率背后，可能存在对象管理、上下文查找和指标收尾的同步开销——它们往往落在计算算子外围，却决定小查询能否随并发扩展。**具体收益仍需要完整的前后实验记录（本稿没有）。**

## 成稿缺口（TODO）

- [ ] 补 StarRocks、Linux、BCC / perf 版本与采样条件（机型、是否同机复跑）。
- [ ] 补每一步修复前后的吞吐、尾延迟、CPU、上下文切换及波动范围（**禁止用估计值填空**）。
- [ ] 将飞书火焰图 / 关键栈转为可公开附件，或改为匿名折叠栈文本。
- [ ] 对异常偏大的 Off-CPU 计数值做工具 / 单位核对后，决定是否进入附录。
- [ ] 公开 PR 与当前主干行为再核对一次（笔记成稿时未重核）。

## 参考资料

- [BCC offcputime 源码](https://github.com/iovisor/bcc/blob/master/tools/offcputime.py)：采样与累计口径。
- [StarRocks #13373](https://github.com/StarRocks/starrocks/pull/13373)、[#13593](https://github.com/StarRocks/starrocks/pull/13593)、[#13604](https://github.com/StarRocks/starrocks/pull/13604)：案例对应的公开修改。
- Brendan Gregg：[Off-CPU Analysis](https://www.brendangregg.com/offcpuanalysis.html)。
- 原始材料：Notion《高并发查询为什么打不满 CPU：一次连续的 Off-CPU 排查》（项目调研；内含折叠保留的命令与堆栈）。

---

## 附录：原笔记中的关键堆栈摘要（供定位，非新采样）

以下摘自历史记录，符号与 tid 以当时环境为准；**单位为 offcputime 累计（微秒量级常见），勿直接换算成「查询延迟」**。

**A. `~RuntimeState`（资源池锁竞争阶段）**

```text
finish_task_switch
schedule
futex_wait_queue_me
futex_wait
do_futex
SyS_futex
system_call_fastpath
--
starrocks::RuntimeState::~RuntimeState()
... shared_ptr dispose / release ...
starrocks::pipeline::GlobalDriverExecutor::_worker_thread()
-                pip_executor (<tid>)
        25359323    # ≈25.36s 累计等待（该 30s 窗口、该线程）
```

**B. `ScanOperator::_trigger_next_scan` → `query_ctx_mgr.get()` 读锁**

```text
pthread_rwlock_rdlock
starrocks::pipeline::ScanOperator::_trigger_next_scan(...)
starrocks::pipeline::ScanOperator::pull_chunk(...)
starrocks::pipeline::PipelineDriver::process(...)
starrocks::pipeline::GlobalDriverExecutor::_worker_thread()
-                pip_executor (<tid>)
    12058256
```

**C. `PipelineDriver::finalize` 写锁（分片问题显露阶段）**

```text
pthread_rwlock_wrlock
starrocks::pipeline::PipelineDriver::finalize(...)
starrocks::pipeline::GlobalDriverExecutor::_worker_thread()
-                pip_executor (<tid>)
    20230688
```

原分片实现示意（已被 #13593 一类改动替换；**勿当作当前代码**）：

```cpp
size_t QueryContextManager::_slot_idx(const TUniqueId& query_id) {
    return std::hash<size_t>()(query_id.lo) & _slot_mask;
}
```

