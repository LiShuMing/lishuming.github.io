---
title: "系统工程与算法面试核心范式：从单机并发、RadixCache 到高频解题母题"
slug: "system-and-algorithm-interview-patterns"
date: 2026-11-18T23:30:00+08:00
lastmod: 2026-11-19T09:45:00+08:00
categories:
  - 算法与数据结构
  - 系统工程
tags:
  - C++
  - Python
  - 面试总结
  - SingleFlight
  - RadixCache
  - PagedAttention
  - BloomFilter
  - 动态规划
  - 图论
description: "围绕系统与基础设施研发面试中的高频算法与工程设计题，从单调性与前缀和、SingleFlight 请求合并与重入防死锁、任务 DAG 与最短路边界、双堆事件调度、vLLM/SGLang RadixTree 带约束 LRU 到手写 BloomFilter 与 18 个核心解题范式做系统化沉淀。"
draft: false
toc: true
math: true
---

在数据库、分布式系统与 AI Infra 研发面试中，算法题与系统设计题很少是割裂的两张皮。面试官往往先抛出一道看似普通的数组、图或缓存题，紧接着把某个假设改掉——例如把“正数数组”改成“允许负数”，把“单机 LRU”改成“带父子依赖的显存块淘汰”，或者追问“如果同一任务递归请求自己的 Key 怎么防死锁”——以此观察候选人是否真正看穿了数据结构背后的物理约束与边界条件。

下面把近期系统梳理的并发控制、显存调度、图论最短路以及 18 个高频算法范式沉淀成一份可随时翻阅的实战笔记，重点写清**每一类问题在什么地方会踩坑、条件一变模型如何切换，以及工程代码落地时的关键细节**。

---

## 一、 窗口求和与单调性边界：为什么同样的题会有四种写法

先看一个极小的反例：`nums = [-5, 4]`，元素类型都是 `1`，要求子数组内最多包含 `m = 1` 种类型且和最大。

如果直接套用“右指针扩窗、超限缩左指针、合法就更新答案”的双指针写法，当右指针走到 `4` 时，窗口 `[0, 1]` 本身只有 `1` 种类型，完全合法，于是双指针会停在左边界 `0` 并算出 `-1`。但只要把前面的 `-5` 丢掉，只取 `[1, 1]`，就能得到更优的 `4`。

这个反例直接把窗口求和题劈成了四个完全不同的模型：

| 题型变体 | 核心约束 | 是否怕负数 | 正确做法 | 时间 / 空间 |
| :--- | :--- | :--- | :--- | :--- |
| **1. 定长 `k` 最大和** | 长度固定为 `k` | 不怕 | 定长滑窗：`sum += a[i] - a[i - k]` | $O(n)$ / $O(1)$ |
| **2. 定长 `k` 且种类 $\le m$** | 长度固定 + 种类上限 | 不怕 | 定长滑窗 + 类型计数表，仅合法时更新 | $O(n)$ / $O(\min(k, \text{种类数}))$ |
| **3. 变长且种类 $\le m$（非负）** | `a[i] >= 0` | **要求非负** | 同向双指针扩右缩左，直接取当前窗口和 | $O(n)$ / $O(\text{种类数})$ |
| **4. 变长且种类 $\le m$（含负数）** | 允许 `a[i] < 0` | 不怕 | 双指针定最左合法界 `l` + 前缀和 + 单调递增队列 | $O(n)$ / $O(n)$ |

之所以定长窗口不怕负数，是因为长度被锁死为 `k`，没有“缩短窗口丢掉负前缀”的自由度；而在变长窗口里，**种类数 `distinct <= m` 对窗口长度有单调性，但窗口和只有在元素非负时才对长度有单调性**。一旦允许负数，双指针只能用来求每个右端点 `r` 对应的最左合法边界 `l`——即起点 `j` 必须落在 `[l, r]` 内。子数组和写成 `prefix[r + 1] - prefix[j]` 后，问题就转化为在滑动区间 `[l, r]` 里找最小的 `prefix[j]`，用一个单调递增双端队列就能维持 $O(n)$。

同样受“负数破坏单调性”影响的经典题对比，还有 [209. 长度最小的子数组](https://leetcode.cn/problems/minimum-size-subarray-sum/)、[560. 和为 K 的子数组](https://leetcode.cn/problems/subarray-sum-equals-k/) 和 [862. 和至少为 K 的最短子数组](https://leetcode.cn/problems/shortest-subarray-with-sum-at-least-k/)：

- **209 题（全正数，和 $\ge K$ 最短）**：单调性成立，直接同向双指针 $O(1)$ 空间。
- **560 题（含负数，和 $== K$ 计数）**：单调性失效，用前缀和 + 哈希表查历史 `prefix - K` 出现次数，别忘了初始化 `prefix_count[0] = 1`。
- **862 题（含负数，和 $\ge K$ 最短）**：单调性失效，用前缀和 + 单调递增双端队列：队头一旦满足 `prefix[i] - prefix[dq.front()] >= K` 就结算并直接出队（后面只会更长，不可能更优）；队尾入队前把 `prefix[dq.back()] >= prefix[i]` 的点全部弹出（又靠左、起点前缀和又大，彻底被支配）。

实战写代码时还有三个高频边界：无解或全负数时不能把 `best` 初始化为 `0`（应使用 `None` / `std::optional` 或 `-inf`）；计数归零时要显式从哈希表删除或递减 `distinct`；当 `m >= 总种类数` 时，非负数组退化为全局和，含负数数组则退化为 Kadane 最大子数组和（[53. 最大子数组和](https://leetcode.cn/problems/maximum-subarray/)）。

---

## 二、 进程内请求合并（SingleFlight）：并发共享与递归自锁破局

进程内请求合并器（SingleFlight）的目标很明确：同一个 `key` 的并发请求只由第一个到达的 Leader 执行一次计算，其余 Waiter 共享结果或异常，完成后不保留缓存。

主干做法是用一把锁保护 `key -> Future/Call` 映射：Leader 注册条目后**立刻释放锁**去执行计算，Waiter 拿到共享句柄后也在锁外等待；计算结束无论成功还是抛异常，都唤醒所有 Waiter，再加锁**按指针或 Future 身份比对**清理映射（`if inflight.get(key) is fut: del inflight[key]`），防止误删后续新发起的同名请求。

### 面试必追问的三个深水区

1. **单个 Waiter 被取消或超时，不能连带取消共享任务**  
   在 Python `asyncio` 中，直接 `await asyncio.wait_for(fut, timeout)` 会在超时时把底层 `fut` 一起 `cancel()` 掉，导致 Leader 和其他 Waiter 全部陪葬。正确写法是套一层 **`asyncio.shield(fut)`** 隔离取消信号。而在 C++ 多线程实现中，Waiter 只是在 `std::condition_variable::wait_for`（或 `std::shared_future::wait_for`）上阻塞，超时退出不会修改共享 `Call` 的状态，天然具备 `shield` 语义。
2. **同一任务递归请求自己的 Key，如何避免永久等待？**  
   如果 Leader 在执行 `fn()` 期间又以相同 `key` 调回 `do()`，它会看到 `key` 仍在 `inflight` 中而等待自己尚未完成的 `Future`，形成自环死锁（间接环 `A -> B -> A` 同理）。
   - **Python `asyncio`**：使用 `contextvars.ContextVar` 维护当前调用链的活跃 `key` 集合（`frozenset`）。`ContextVar` 沿 `await` 链自动传播且 `create_task` 会自动拷贝上下文，不同并发请求天然隔离，入口命中即抛出 `ReentrantKeyError`（或按业务要求旁路直调）。
   - **C++ 多线程**：使用 `thread_local std::unordered_set<Key>` 配合 RAII Guard 在执行 `fn()` 前后登记/移除当前线程活跃 `key`。但如果 `fn()` 内部把子任务提交到线程池，C++ 没有语言级自动透传，需显式拷贝上下文或依赖 `wait_for` 超时兜底。
   - **C++20 协程 / 单线程 EventLoop（极易踩坑）**：在事件循环上**绝对不能用 `thread_local`**！因为同一线程上多个无关协程交替挂起与恢复，协程 A 挂起后协程 B 请求同一 `key` 会误判重入；必须把活跃 `key` 集合显式挂在随 `co_await` 链传递的 `RequestContext` 中。
3. **C++ 结果共享的所有权与锁热点**  
   如果结果类型 `T` 很大或不可拷贝，返回值建议设计为 `std::shared_ptr<const T>` 避免每个 Waiter 深拷贝一次；若高并发下全局 `mu_` 成为热点，可按 `hash(key) % N` 做分段锁（Striped Lock）。

### 核心代码骨架（Python `asyncio` 与 C++20）

```python
_ACTIVE_KEYS: ContextVar[frozenset[Any]] = ContextVar("active_keys", default=frozenset())

class AsyncSingleFlight:
    def __init__(self) -> None:
        self._inflight: dict[Any, asyncio.Future[Any]] = {}
        self._lock = asyncio.Lock()

    async def do(self, key: Any, fn: Callable[[], Awaitable[T]], timeout: float | None = None) -> T:
        active = _ACTIVE_KEYS.get()
        if key in active:
            raise ReentrantKeyError(f"Cycle/reentrant request for key: {key!r}")

        async with self._lock:
            fut = self._inflight.get(key)
            is_leader = fut is None
            if is_leader:
                fut = asyncio.get_running_loop().create_future()
                self._inflight[key] = fut

        if not is_leader:
            wait_coro = asyncio.shield(fut)
            return await (asyncio.wait_for(wait_coro, timeout) if timeout is not None else wait_coro)

        token = _ACTIVE_KEYS.set(active | {key})
        try:
            result = await fn()
            if not fut.done():
                fut.set_result(result)
            return result
        except BaseException as exc:
            if not fut.done():
                fut.set_exception(exc)
            raise
        finally:
            _ACTIVE_KEYS.reset(token)
            async with self._lock:
                if self._inflight.get(key) is fut:
                    del self._inflight[key]
```

C++20 多线程版用 `std::shared_ptr<Call>`（内含 `mutex`、`condition_variable`、`done`、`std::optional<T>`、`std::exception_ptr`）搭配 `try/catch (...)` 与 `ActiveKeyGuard` RAII 守卫，保证异常传播与按 `shared_ptr` 身份清理不遗漏。

---

## 三、 任务 DAG 与最短路：Kahn 拓扑、三色定位环与三类最短路边界

工程里的 ETL / 物化视图刷新任务、Agent 工具调用编排，底层都是有向图偏序调度：`u -> v` 表示 `u` 必须先于 `v` 完成。

### 1. Kahn 入度法 vs DFS 三色标记：为什么不能互相完全替代？

口述拓扑排序时很容易说出一句不严谨的话：“Kahn 结束后，入度还大于 0 的就是环上的点。”  
举个反例就能推翻它：如果 `b <-> c` 成环，同时 `c -> d`，那么 Kahn 结束后 `b`、`c`、`d` 的入度都大于 0，但 `d` 只是**被环阻塞的下游任务**，它自己并不在环上。因此，调度系统通常采用组合拳：
- **Kahn 入度队列**：负责产出执行序、按层统计并行度（[1136. 并行课程](https://leetcode.cn/problems/parallel-courses/)）；若需确定性输出则把普通队列换成最小堆；若任务耗时不等，则按拓扑序做关键路径 DP（`earliest_start[v] = max(earliest_start[u] + duration[u])`）。
- **DFS 三色标记（0 未访 / 1 在当前栈 / 2 已完成）**：一旦 Kahn 出队数小于总任务数，用三色 DFS 找到指向灰色节点（`1`）的回边，顺着 `parent` 回溯就能精确打印出 `b -> c -> b` 环路报错。
- **失败级联取消**：某个节点失败时，从该点出发沿正向出边做 BFS 求可达集，只取消下游依赖，旁路任务正常执行。

常见拓扑题的变形对照：

| 题号与名称 | 核心变形点 | 解法关键 |
| :--- | :--- | :--- |
| [207](https://leetcode.cn/problems/course-schedule/) / [210](https://leetcode.cn/problems/course-schedule-ii/) | 基础判环 / 出序 | Kahn 统计出队数，或三色 DFS 抓回边 |
| [2115. 做菜](https://leetcode.cn/problems/find-all-possible-recipes-from-given-supplies/) | 只有给定起点可用 | 仅将 `supplies` 作为初始队列，逐层解锁菜品 |
| [269. 火星词典](https://leetcode.cn/problems/alien-dictionary/) | 隐式字符建图 | 相邻词找首个不同字符连边；`"abc"` 在 `"ab"` 前直接判非法 |
| [1857. 最大颜色值](https://leetcode.cn/problems/largest-color-value-in-a-directed-graph/) | 拓扑序 + DAG DP | 出队 `u` 时向后继 `v` 传递 `dp[u][0..25]` 最大值 |
| [329. 矩阵最长递增路径](https://leetcode.cn/problems/longest-increasing-path-in-a-matrix/) | 隐式 DAG | 值严格递增天然无环，比自己小的邻居数即入度，按层剥离 |
| [802. 最终安全状态](https://leetcode.cn/problems/find-eventual-safe-states/) | 能走到环的不安全 | 建反图，从出度为 0 的安全终点反向跑 Kahn |
| [1203. 项目管理](https://leetcode.cn/problems/sort-items-by-groups-respecting-dependencies/) | 组内 + 组间双层约束 | 孤立项分配虚拟组，组间与组内分别拓扑再拼接 |
| [310. 最小高度树](https://leetcode.cn/problems/minimum-height-trees/) | 无向树拓扑剥叶子 | 度为 1 的叶子入队逐层向心剥离，剩下 $\le 2$ 个中心点 |

### 2. 三类最短路的边界：BFS、Dijkstra 与 Bellman-Ford

这三个算法之所以不能乱套，用 3 个节点的小反例就能一眼看穿：

1. **为什么不等权图不能用普通 BFS（[127. 单词接龙](https://leetcode.cn/problems/word-ladder/)）？**  
   图 `A -> C (10)`、`A -> B (1)`、`B -> C (1)`。BFS 只看步数，第 1 步先碰到 `C` 就误以为最短是 `10`。**无权图（或边权全等）才用 BFS**（状态多时用双向 BFS，每次只扩展较小集合）。
2. **为什么有负权边时 Dijkstra（[743. 网络延迟时间](https://leetcode.cn/problems/network-delay-time/)）会算错？**  
   图 `A -> C (2)`、`A -> B (5)`、`B -> C (-10)`。Dijkstra 假设“后面的路只会越走越贵”，先把最小的 `C(2)` 锁死，结果漏掉了 `5 + (-10) = -5` 这条路。
3. **为什么限制最多走 $k$ 步（[787. K 站中转内最便宜的航班](https://leetcode.cn/problems/cheapest-flights-within-k-stops/)）时标准 Dijkstra 也会错？**  
   限制最多走 2 条边从 `A` 到 `D`，图有 `A -> B(1) -> C(1) -> D(100)` 和 `A -> C(5)`。标准 Dijkstra 在 `C` 点看到 `A -> B -> C` 花费 `2 < 5`，就把直达 `A -> C(5)` 淘汰掉了，等走到 `D` 才发现 `A -> B -> C -> D` 走了 3 条边超步数！因此必须用 **Bellman-Ford 跑 `k + 1` 轮松弛**，且每轮开始前必须备份 `prev = dist`，防止同轮内串联松弛多走步数。

---

## 四、 事件驱动调度与优先级队列：双堆选机、反悔贪心与延迟删除

任务调度题在基础设施里非常常见，底层共同骨架是**按事件时间线跳跃推进 + 优先级堆维护候选池**（绝不能按秒空转）。

- **单机 SJF / EDF 调度（[1834](https://leetcode.cn/problems/single-threaded-cpu/)、[1353](https://leetcode.cn/problems/maximum-number-of-events-that-can-be-attended/)）**：堆空时时间直接快进到下一个到达时刻；1834 按耗时最短出堆（注意 `time` 用 `long long`），1353 每天清理过期会议后按最早截止时间（EDF）出堆。
- **双堆集群选机（[1882](https://leetcode.cn/problems/process-tasks-using-servers/)、[2402](https://leetcode.cn/problems/meeting-rooms-iii/)）**：维护 `free_heap`（按权重/编号选机器）与 `busy_heap`（按释放时间戳 `finish_time` 排序）。每次派发前先将 `busy_heap` 中 `finish_time <= current_time` 的机器全部归还 `free_heap`；若无空闲机器，时间直接跳到 `busy_heap.top().finish_time` 并批量归还同时释放的所有机器。
- **反悔贪心与配额瓶颈（[871](https://leetcode.cn/problems/minimum-number-of-refueling-stops/)、[1383](https://leetcode.cn/problems/maximum-performance-of-a-team/)）**：871 路过加油站先把油量存进大顶堆，油不够时从历史堆里挑最大的一桶补上；1383 先按效率降序固定瓶颈，再用小顶堆维护速度 Top-K。
- **冷却桶与区间贪心（[621](https://leetcode.cn/problems/task-scheduler/)、[452](https://leetcode.cn/problems/minimum-number-of-arrows-to-burst-balloons/)、[2054](https://leetcode.cn/problems/two-best-non-overlapping-events/)）**：621 利用最高频次 $M$ 与并列种类数 $C$ 构造桶公式 $\max(N, (M-1)(n+1)+C)$；452 按右端点排序射箭；2054 按开始时间扫描，用小顶堆弹出已结束活动并更新历史最大价值。
- **动态删任意元素——延迟删除对顶堆（[295](https://leetcode.cn/problems/find-median-from-data-stream/) $\to$ [480](https://leetcode.cn/problems/sliding-window-median/)）**：295 用“小的一半大顶堆 + 大的一半小顶堆”过一遍筛子维持平衡；480 滑窗移出任意元素时，堆不支持 $O(\log k)$ 随机删除，因此引入 `delayed` 哈希表记录待删次数，并**单独维护逻辑大小 `small_size_` / `large_size_`**，只在元素浮到堆顶时才通过 `prune()` 真正弹出。

---

## 五、 树状前缀缓存（RadixTree）与带约束 LRU：vLLM / SGLang 显存底座

在 LLM 推理引擎中，多轮对话、Few-shot 与 Agent 工作流存在大量公共前缀。RadixTree 的本质，是以 Token 序列为路径、以 GPU 物理 KV Block（固定大小，如 16 个 Token 一块）为映射目标的树状显存缓存管理器。

### 1. 显存布局与 PagedAttention 计算闭环

GPU 启动时将显存预分配为巨大的连续 Tensor Pool（如 `[num_blocks, block_size, num_kv_heads, head_dim]`）。对于序列第 $i$ 个 Token，逻辑块号为 `i / 16`、块内偏移为 `i % 16`，再通过请求私有的 `block_table` 页表查到物理 `block_id`。PagedAttention CUDA Kernel 接收 `block_table` 后，直接用指针算术就地跳块读取非连续物理 Block 算 Attention，**零拷贝**。

当尾部只填了部分槽位（比如 18 个 Token 占 2 块，第 2 块只用了 2 格，闲置 14 格）时：
- **计算时**：Kernel 依据传入的 `context_len` 对超出有效边界的槽位做掩码（置为 $-\infty$），绝不读出脏数据；
- **生成时**：Decode 阶段新生成的 Token 就地写入空槽位，填满 16 个才申请下一块；
- **跨请求共享时**：为避免并发写写冲突，**vLLM** 早期采用**块对齐截断**（只缓存填满 16 个的完整块，尾部零头宁可花几微秒重算）；**SGLang** 则支持未满块共享，并在分叉写入时触发 **Copy-on-Write（CoW）** 克隆新块。

### 2. vLLM V0 到 V1 架构演进动机

vLLM V0 早期采用平铺的级联块哈希（`H_i = Hash(H_{i-1}, Tokens_i)` 存入 `Dict[BlockHash, Block]`）。这种设计没有显式树拓扑，长上下文下每步在 Python 里切块算 Hash 带来毫秒级 CPU 调度瓶颈，更难以干净地统一 **Chunked Prefill（长 Prompt 分块跨步状态挂载）** 与 **推测解码的树状验证（Draft Token Tree 1:1 映射与瞬间剪枝）**。因此在 V1 重构中，vLLM 转向了显式分层前缀树模型。

### 3. 带约束的树状 LRU 淘汰机制

由于子节点存储的 KV Cache 在物理和语义上强依赖父节点前缀，**任何内部节点都不能在子节点存活时被直接驱逐**——淘汰必须严格**自叶向根（Leaf-to-Root）**。

```
可驱逐不变量：node->ref_count == 0 && node->children.empty()
```

核心结构采用**树节点 + 侵入式双向 LRU 链表**：
- **全局 LRU 链表**：仅挂载当前可驱逐的叶子节点（头最旧、尾最新），保证 $O(1)$ 摘除、尾插与淘汰；
- **`match_prefix`**：沿树向下匹配，更新 `last_access`，若节点在 LRU 链表中则 $O(1)$ 挪到链表尾；
- **`inc_ref` / `dec_ref`**：引用计数变化时，若节点状态在“完全无锁”与“有锁”之间翻转，沿 `parent` 向上更新 `locked_children`，遇到已处于相同锁定状态的祖先可提前终止；
- **`evict(B)`**：从 LRU 头部逐个淘汰叶子并从 `parent->children` 摘除；当某父节点的所有孩子都被逐空且自身 `ref_count == 0` 时，**晋升为新的可驱逐叶子**并保留原有 `last_access` 挂入 LRU（核心实现已沉淀于 `cc/cclab/src/utils/prefix_cache_lru.h` 与 `python/pylab/prefix_cache_lru.py`）。

---

## 六、 工程数据结构手写模板：Bloom Filter 与 LRU Cache

### 1. 面试手写 Bloom Filter 的四个加分细节

1. **先报参数公式与经验值**：给定预期元素数 $n$ 与误判率 $p$，位数组大小 $m = -\frac{n \ln p}{(\ln 2)^2}$，最优哈希数 $k = \frac{m}{n} \ln 2$（当 $p = 1\%$ 时，约 $9.6\text{ bits/elem}$、$k \approx 7$）。
2. **双哈希模拟 $k$ 个哈希（Kirsch-Mitzenmacher）**：工业界（RocksDB / Guava）只算两个 64 位哈希 $h_1, h_2$，用 $g_i(x) = (h_1(x) + i \cdot h_2(x)) \pmod m$ 模拟 $k$ 个独立哈希。
3. **底层位操作**：用 `std::vector<uint64_t>` 按 64 位对齐存储，`bits[idx / 64] |= (1ULL << (idx % 64))`。
4. **DB 内核级追问准备**：
   - *如何支持删除？* 改用 Counting Bloom Filter（4-bit 计数器，空间 $\times 4$）或 **Cuckoo Filter**（存指纹支持 $O(1)$ 删除）；
   - *标准 Bloom Filter 的硬件瓶颈？* 每次查询触发 $k$ 次随机内存访问导致 $k$ 次 L3 Cache Miss；工业界用 **Blocked Bloom Filter** 将 $k$ 个 bit 全塞进同一个 **64 字节 CPU Cache Line** 内配合 SIMD 检查，将 Cache Miss 降为至多 1 次；
   - *无锁并发写？* 使用 `std::atomic<uint64_t>::fetch_or(..., std::memory_order_relaxed)`。

```cpp
class BloomFilter {
public:
    BloomFilter(size_t expected_elements, double fp_rate = 0.01) {
        double ln2 = std::log(2.0);
        num_bits_ = static_cast<size_t>(
            std::ceil(-1.0 * expected_elements * std::log(fp_rate) / (ln2 * ln2)));
        num_bits_ = std::max<size_t>(64, (num_bits_ + 63) / 64 * 64);
        num_hashes_ = std::clamp<size_t>(
            static_cast<size_t>(std::round((double(num_bits_) / expected_elements) * ln2)), 1, 30);
        bits_.resize(num_bits_ / 64, 0);
    }

    void Add(std::string_view key) {
        auto [h1, h2] = Hash128(key);
        for (size_t i = 0; i < num_hashes_; ++i) {
            size_t idx = (h1 + i * h2) % num_bits_;
            bits_[idx / 64] |= (1ULL << (idx % 64));
        }
    }

    bool Contains(std::string_view key) const {
        auto [h1, h2] = Hash128(key);
        for (size_t i = 0; i < num_hashes_; ++i) {
            size_t idx = (h1 + i * h2) % num_bits_;
            if ((bits_[idx / 64] & (1ULL << (idx % 64))) == 0) return false;
        }
        return true;
    }

private:
    static uint64_t Mix64(uint64_t z) {
        z = (z ^ (z >> 30)) * 0xbf58476d1ce4e5b9ULL;
        z = (z ^ (z >> 27)) * 0x94d049bb133111ebULL;
        return z ^ (z >> 31);
    }
    static std::pair<uint64_t, uint64_t> Hash128(std::string_view s) {
        uint64_t h1 = 0xcbf29ce484222325ULL, h2 = 0x100000001b3ULL;
        for (char c : s) {
            h1 = (h1 ^ static_cast<uint8_t>(c)) * 0x100000001b3ULL;
            h2 = (h2 ^ static_cast<uint8_t>(c)) * 0xcbf29ce484222325ULL;
        }
        return {Mix64(h1), Mix64(h2 | 1ULL)};
    }
    size_t num_bits_{0}, num_hashes_{0};
    std::vector<uint64_t> bits_;
};
```

### 2. C++ 工程实现优化要点

- **LRU 缓存（146）**：`std::list<std::pair<int, int>>` 配合 `unordered_map`，命中时用 `cache_.splice(cache_.begin(), cache_, it->second)` 原地改指针移到表头，零内存重分配。
- **Trie（208 / 212）**：小写字母场景用 `std::array<std::unique_ptr<Node>, 26>` 替代哈希表；网格搜词（212）在终点节点直接存 `std::string word`，命中后置空防止重复收集。
- **二叉树序列化（297）**：`std::ostringstream` 写 `"# "` 占位符配合 `std::istringstream`（`in >> token` 自动跳过前导空白符并按空格切分）；追求极致零拷贝可用 `std::string_view` + `std::from_chars` 原地切分。

---

## 七、 18 个高频算法范式全景速查与切换开关

### 1. 一眼看懂 18 个解题范式

| 范式 | 识别信号 | 大白话核心思路 & 极简示例 | 代表题 |
| :--- | :--- | :--- | :--- |
| **1. 哈希与计数** | 查找配对、频率、去重 | 用小本本记下见过谁。`[2,7,11]` 找 `9`：到 `7` 时查表里有 `9-7=2` | [1](https://leetcode.com/problems/two-sum/), [49](https://leetcode.com/problems/group-anagrams/) |
| **2. 双指针** | 有序数组、两端夹逼 | 排序后两头往中间捏。`[-1,-1,0,1,2]`：和太小左进、太大右退，注意去重 | [15](https://leetcode.com/problems/3sum/), [42](https://leetcode.com/problems/trapping-rain-water/) |
| **3. 滑动窗口** | 连续子串/数组、非负极值 | 毛毛虫爬行：头往前伸凑条件，尾往回收砍多余（`valid` 计数器 $O(1)$ 判覆盖） | [3](https://leetcode.com/problems/longest-substring-without-repeating-characters/), [76](https://leetcode.com/problems/minimum-window-substring/), [209](https://leetcode.com/problems/minimum-size-subarray-sum/) |
| **4. 前缀和 / 差分** | 含负数区间和、批量范围改 | 先算总账，区间和等于两笔总账之差：`prefix[i] - prefix[j] == k` | [560](https://leetcode.com/problems/subarray-sum-equals-k/) |
| **5. 二分 / 答案二分** | 有序局部或单调判定谓词 | 不止能搜下标，还能猜答案区间 `[1, max]`，行就往小猜、不行往大猜 | [33](https://leetcode.com/problems/search-in-rotated-sorted-array/), [875](https://leetcode.com/problems/koko-eating-bananas/) |
| **6. 单调栈** | 下一个更大/小、矩形面积 | 栈内维持递增，来矮柱子就把高于它的栈顶踢出并结算左右边界 | [84](https://leetcode.com/problems/largest-rectangle-in-histogram/) |
| **7. 单调队列** | 滑动窗口极值、负数最短子数组 | 队里只留“又晚过期又更强”的候选，淘汰队尾弱者、踢掉过期队头 | [239](https://leetcode.com/problems/sliding-window-maximum/), [862](https://leetcode.com/problems/shortest-subarray-with-sum-at-least-k/) |
| **8. 堆 / Top-K** | 动态最值、中位数、多路归并 | 不排全名次，只用大小堆拎出尖子生；删任意元素配延迟删除表 | [215](https://leetcode.com/problems/kth-largest-element-in-an-array/), [295](https://leetcode.com/problems/find-median-from-data-stream/), [347](https://leetcode.com/problems/top-k-frequent-elements/), [480](https://leetcode.com/problems/sliding-window-median/) |
| **9. 排序 + 扫描线** | 区间重叠、会议室安排 | 按左端点排一排，从左扫过去能接就拉长、断开就另开一段 | [56](https://leetcode.com/problems/merge-intervals/), [2054](https://leetcode.com/problems/two-best-non-overlapping-events/) |
| **10. 贪心** | 最少次数、区间覆盖、资源调度 | 每步选当前最赚且经得起交换论证的选择；拿不准就先入堆再反悔 | [45](https://leetcode.com/problems/jump-game-ii/), [452](https://leetcode.com/problems/minimum-number-of-arrows-to-burst-balloons/), [871](https://leetcode.com/problems/minimum-number-of-refueling-stops/) |
| **11. DFS / BFS** | 连通块、层序、无权最短步数 | DFS 撞南墙淹没连通块，BFS 如水波纹扩散找首次撞线最短路 | [102](https://leetcode.com/problems/binary-tree-level-order-traversal/), [127](https://leetcode.com/problems/word-ladder/), [200](https://leetcode.com/problems/number-of-islands/) |
| **12. 回溯 / 剪枝** | 枚举全排列/组合、约束满足 | 做选择 $\to$ 下潜 $\to$ 撞墙悔棋撤回，死胡同提前砍掉 | [51](https://leetcode.com/problems/n-queens/), [212](https://leetcode.com/problems/word-search-ii/) |
| **13. 动态规划** | 重叠子问题、最值/方案数 | 拆子问题记表格（线性/背包/双序列），0-1 背包倒序、完全背包正序 | [72](https://leetcode.com/problems/edit-distance/), [198](https://leetcode.com/problems/house-robber/), [300](https://leetcode.com/problems/longest-increasing-subsequence/), [322](https://leetcode.com/problems/coin-change/), [416](https://leetcode.com/problems/partition-equal-subset-sum/) |
| **14. 分治 / 归并** | 逆序对、跨段点对统计 | 对半切开分别算完，趁两半各自有序用双指针扫跨界配对，再归并 | [493](https://leetcode.com/problems/reverse-pairs/), [23](https://leetcode.com/problems/merge-k-sorted-lists/) |
| **15. 并查集** | 动态加边连通、无向图判环 | 认老大看终极 Boss 是否同人，新边两端已同根则成环 | [547](https://leetcode.com/problems/number-of-provinces/), [684](https://leetcode.com/problems/redundant-connection/) |
| **16. 图最短路 / 拓扑** | 依赖序、带权路径、步数约束 | Kahn 剥入度 0、Dijkstra 非负权贪心、Bellman-Ford 抗负权/限步数 | [207](https://leetcode.com/problems/course-schedule/), [743](https://leetcode.com/problems/network-delay-time/), [787](https://leetcode.com/problems/cheapest-flights-within-k-stops/) |
| **17. Trie / 前缀树** | 共享前缀检索、词典剪枝 | 相同开头共用树枝，查词只需按字母往下走 | [208](https://leetcode.com/problems/implement-trie-prefix-tree/) |
| **18. 位运算 / 状态压缩** | 小规模集合状态枚举 | 用一个整数的二进制位代替 `bool used[]` 数组当状态 Key | [698](https://leetcode.com/problems/partition-to-k-equal-sum-subsets/) |

### 2. 高频「条件切换开关」

做题和面试时，重点盯住这五处条件变化：
1. **数组全正 $\to$ 允许负数**：滑动窗口（209）立刻切为前缀和 + 哈希（560）或前缀和 + 单调队列（862）。
2. **无权 $\to$ 正权 $\to$ 负权/限步数**：BFS（127） $\to$ Dijkstra（743） $\to$ Bellman-Ford（787）。
3. **静态全图 $\to$ 流式加边**：DFS/BFS（200） $\to$ 并查集（684）。
4. **普通 LRU $\to$ 树状前缀依赖 LRU**：`list + map`（146） $\to$ 叶子 LRU + 引用锁定（RadixCache）。
5. **静态 Top-K $\to$ 滑窗删除任意元素**：对顶堆（295） $\to$ 延迟删除对顶堆（480）。

---

## 八、 经典母题精讲：通用方法抽象与 C++ 模板

上面的 18 个范式如果只停留在概念表上，面试落笔时仍然容易在边界条件和循环不变量上出错。下面挑出 **12 个最具迁移价值的经典母题**——每一题都对应一类可复用的解题骨架，掌握这些骨架和它们的 C++ 实现，遇到同家族变体时只需替换判定条件或状态转移即可。

### 1. 滑动窗口通用骨架：[76. 最小覆盖子串](https://leetcode.cn/problems/minimum-window-substring/)

给定字符串 `s` 和 `t`，求 `s` 中涵盖 `t` 所有字符（含重复个数）的最小子串。如果每次窗口变动都把两个字符频次表完整比对一遍，时间会退化到 $O(|\Sigma| \cdot n)$。

把这个问题抽象成通用滑窗模板，关键在于引入一个增量计数器 `valid`：**只有当某个目标字符在窗口内的频次刚好追平需求量时，`valid` 才加 1；移出左端点导致该字符跌破需求量时，`valid` 才减 1**。这样就把“窗口是否合法”压缩成了一次 $O(1)$ 的整数比较 `valid == required_kinds`。所有“求最短合法区间”的滑窗题（3、209、438、567）都遵循同一套四步循环：右端入窗更新状态 $\to$ 满足条件时循环缩左 $\to$ 在合法态更新最优解 $\to$ 左端出窗撤销状态。

```cpp
#include <array>
#include <climits>
#include <string>

class Solution {
public:
    std::string minWindow(std::string s, std::string t) {
        std::array<int, 128> need{};
        std::array<int, 128> window{};
        int required_kinds = 0;
        for (char c : t) {
            if (need[static_cast<unsigned char>(c)]++ == 0) ++required_kinds;
        }

        int left = 0, valid = 0, start = 0, min_len = INT_MAX;
        for (int right = 0; right < static_cast<int>(s.size()); ++right) {
            unsigned char c = s[right];
            if (need[c] > 0 && ++window[c] == need[c]) ++valid;

            while (valid == required_kinds) {
                if (right - left + 1 < min_len) {
                    start = left;
                    min_len = right - left + 1;
                }
                unsigned char d = s[left++];
                if (need[d] > 0) {
                    if (window[d] == need[d]) --valid;
                    --window[d];
                }
            }
        }
        return min_len == INT_MAX ? "" : s.substr(start, min_len);
    }
};
```

### 2. 前缀和 + 哈希表：[560. 和为 K 的子数组](https://leetcode.cn/problems/subarray-sum-equals-k/)

给定可能包含负数的数组 `nums`，统计和恰好等于 `k` 的连续子数组个数。负数破坏了窗口和随长度递增的单调性，双指针无法判断该扩右还是缩左。

通用破局思路是把连续区间问题写成**前缀差分**：子数组 `[j, i]` 的和等于 `prefix[i] - prefix[j - 1] == k`，移项即 `prefix[j - 1] == prefix[i] - k`。问题瞬间退化成“遍历到当前前缀和时，历史哈希表里有多少个匹配的旧前缀和”。同理，只要把 `sum - k` 换成 `(sum % k + k) % k`（同余定理，如 523/974 题），或者把 `0` 看成 `-1` 求 `0/1` 数量相等的最长子数组（525 题），整套模板原封不动即可复用。

```cpp
#include <unordered_map>
#include <vector>

class Solution {
public:
    int subarraySum(std::vector<int>& nums, int k) {
        std::unordered_map<int, int> prefix_count;
        prefix_count[0] = 1; // 空前缀和为 0，覆盖从下标 0 开始直接命中 k 的情况

        int sum = 0, ans = 0;
        for (int x : nums) {
            sum += x;
            if (auto it = prefix_count.find(sum - k); it != prefix_count.end()) {
                ans += it->second;
            }
            ++prefix_count[sum];
        }
        return ans;
    }
};
```

### 3. 答案空间二分：[875. 爱吃香蕉的珂珂](https://leetcode.cn/problems/koko-eating-bananas/)

给定若干堆香蕉 `piles` 和时间上限 `h`，求每小时能吃完的最小速度 `k`。题目并没有给你一个排好序的数组，但答案本身具有单调性：如果速度 `k` 能在 `h` 小时内吃完，那么任何大于 `k` 的速度也一定吃得完。

这类“求最小可行值”或“最小化最大值”的问题（如 1011 运送包裹、410 分割数组的最大值）全部对应同一套**单调谓词二分模板**：确定答案上下界 `[left, right]`，写一个线性扫描判定函数 `check(mid)`；若 `check(mid)` 为真，说明最优解在左半边（含 `mid`），令 `right = mid`，否则 `left = mid + 1`，循环结束时 `left` 即为最小可行解。

```cpp
#include <algorithm>
#include <vector>

class Solution {
public:
    int minEatingSpeed(std::vector<int>& piles, int h) {
        int left = 1;
        int right = *std::max_element(piles.begin(), piles.end());

        auto canFinish = [&](int speed) -> bool {
            long long hours = 0;
            for (int p : piles) {
                hours += (p + speed - 1) / speed; // 向上取整
            }
            return hours <= h;
        };

        while (left < right) {
            int mid = left + (right - left) / 2;
            if (canFinish(mid)) {
                right = mid;
            } else {
                left = mid + 1;
            }
        }
        return left;
    }
};
```

### 4. 单调栈找左右边界：[84. 柱状图中最大的矩形](https://leetcode.cn/problems/largest-rectangle-in-histogram/)

给定柱子高度数组 `heights`，求最大矩形面积。若以第 `k` 根柱子为矩形高度，它向左向右最多能延伸到哪里？恰好停在**左边第一个严格比它矮的柱子**和**右边第一个严格比它矮的柱子**之间。

单调栈的通用抽象是：**栈内保存下标并维持对应值的严格单调性；每当新元素打破单调性时，被弹出的栈顶元素就同时锁定了它的右边界（当前下标 `i`）与左边界（弹出后的新栈顶）**。在循环末尾 `i == n` 处喂入一个高度 `0` 的虚拟哨兵，可以一次性把留在栈里的所有柱子结算干净，免去循环外写重复收尾代码。

```cpp
#include <algorithm>
#include <stack>
#include <vector>

class Solution {
public:
    int largestRectangleArea(std::vector<int>& heights) {
        int n = heights.size();
        std::stack<int> st;
        int max_area = 0;

        for (int i = 0; i <= n; ++i) {
            int cur_h = (i == n) ? 0 : heights[i]; // 末尾虚拟哨兵 0 强制清空栈
            while (!st.empty() && heights[st.top()] > cur_h) {
                int h = heights[st.top()];
                st.pop();
                int left_bound = st.empty() ? -1 : st.top();
                max_area = std::max(max_area, h * (i - left_bound - 1));
            }
            st.push(i);
        }
        return max_area;
    }
};
```

### 5. 前缀和 + 单调队列：[862. 和至少为 K 的最短子数组](https://leetcode.cn/problems/shortest-subarray-with-sum-at-least-k/)

给定含负数的数组 `nums`，求和至少为 `k` 的最短非空连续子数组。先构造前缀和 `prefix[0..n]`，目标变为对每个右端点 `i` 寻找最靠右的 `j < i` 满足 `prefix[i] - prefix[j] >= k`。

单调队列维护的是候选起点之间的**双维支配关系**：
- **队头结算即出队**：只要队头 `j` 已经满足 `prefix[i] - prefix[j] >= k`，更新最短长度后立刻弹出 `j`，因为后面更大的右端点再配对 `j` 只会更长，绝不可能刷新最优解；
- **队尾淘汰劣势点**：新下标 `i` 入队前，把队尾所有 `prefix[dq.back()] >= prefix[i]` 的点全部弹出，因为它们既更靠左、又需要更大的右端点才能凑够差值 `k`，在两个维度上都被 `i` 彻底碾压。

```cpp
#include <algorithm>
#include <climits>
#include <deque>
#include <vector>

class Solution {
public:
    int shortestSubarray(std::vector<int>& nums, int k) {
        int n = nums.size();
        std::vector<long long> prefix(n + 1, 0);
        for (int i = 0; i < n; ++i) prefix[i + 1] = prefix[i] + nums[i];

        std::deque<int> dq;
        int min_len = INT_MAX;

        for (int i = 0; i <= n; ++i) {
            while (!dq.empty() && prefix[i] - prefix[dq.front()] >= k) {
                min_len = std::min(min_len, i - dq.front());
                dq.pop_front();
            }
            while (!dq.empty() && prefix[dq.back()] >= prefix[i]) {
                dq.pop_back();
            }
            dq.push_back(i);
        }
        return min_len == INT_MAX ? -1 : min_len;
    }
};
```

### 6. 对顶堆 + 延迟删除：[480. 滑动窗口中位数](https://leetcode.cn/problems/sliding-window-median/)

固定长度为 `k` 的窗口在数组上滑动，每次返回窗口中位数。这道题把“动态维护中位数（295 题）”与“标准堆不支持随机删除”两个难点叠在了一起。

通用解法是**延迟删除（Lazy Deletion）框架**：用 `small_`（大顶堆）和 `large_`（小顶堆）各存一半元素，额外用 `delayed_` 哈希表记录“逻辑上已删除、物理上还留在堆里”的数值频次，并用 `small_size_`、`large_size_` 维护有效逻辑大小。只要每次平衡堆大小后立刻调用 `prune(heap)` 把浮到堆顶的过期元素清理干净，就能保证对外暴露的堆顶永远是有效值。

```cpp
#include <functional>
#include <queue>
#include <unordered_map>
#include <vector>

class DualHeap {
public:
    explicit DualHeap(int k) : k_(k) {}

    void insert(int num) {
        if (small_.empty() || num <= small_.top()) {
            small_.push(num);
            ++small_size_;
        } else {
            large_.push(num);
            ++large_size_;
        }
        makeBalance();
    }

    void erase(int num) {
        ++delayed_[num];
        if (num <= small_.top()) {
            --small_size_;
            if (num == small_.top()) prune(small_);
        } else {
            --large_size_;
            if (num == large_.top()) prune(large_);
        }
        makeBalance();
    }

    double getMedian() const {
        return (k_ & 1) ? static_cast<double>(small_.top())
                        : (static_cast<double>(small_.top()) + large_.top()) / 2.0;
    }

private:
    template <typename Heap>
    void prune(Heap& heap) {
        while (!heap.empty()) {
            int num = heap.top();
            auto it = delayed_.find(num);
            if (it == delayed_.end()) break;
            if (--(it->second) == 0) delayed_.erase(it);
            heap.pop();
        }
    }

    void makeBalance() {
        if (small_size_ > large_size_ + 1) {
            large_.push(small_.top());
            small_.pop();
            --small_size_;
            ++large_size_;
            prune(small_);
        } else if (small_size_ < large_size_) {
            small_.push(large_.top());
            large_.pop();
            ++small_size_;
            --large_size_;
            prune(large_);
        }
    }

    int k_, small_size_ = 0, large_size_ = 0;
    std::priority_queue<int> small_;
    std::priority_queue<int, std::vector<int>, std::greater<int>> large_;
    std::unordered_map<int, int> delayed_;
};
```

### 7. 反悔贪心：[871. 最低加油次数](https://leetcode.cn/problems/minimum-number-of-refueling-stops/)

初始油量 `startFuel`，沿途有若干加油站 `[pos, fuel]`，求到达 `target` 的最少加油次数。当场路过加油站时并不知道后面够不够开，如果立刻决定加不加，很容易陷入局部决策困境。

**反悔贪心（Regret Greedy）**的通用思想是把决策推迟到不得不做的时候：沿途路过的所有加油站先不加，统统把油量塞进一个大顶堆里当备用库；继续往前开，直到当前油量不足以撑到下一个目标时，再从大顶堆里掏出历史经过的最大一桶油加上（等价于“反悔”当初在那一站加了油）。同样套路可直接解 [630. 课程表 III](https://leetcode.cn/problems/course-schedule-iii/)（先按截止日排序硬选，超时就从大顶堆里把耗时最长的课踢掉）。

```cpp
#include <queue>
#include <vector>

class Solution {
public:
    int minRefuelStops(int target, int startFuel, std::vector<std::vector<int>>& stations) {
        std::priority_queue<int> max_heap;
        long long fuel = startFuel;
        int refuels = 0, idx = 0, n = stations.size();

        while (fuel < target) {
            while (idx < n && stations[idx][0] <= fuel) {
                max_heap.push(stations[idx++][1]);
            }
            if (max_heap.empty()) return -1;
            fuel += max_heap.top();
            max_heap.pop();
            ++refuels;
        }
        return refuels;
    }
};
```

### 8. 二叉树后序树形 DP：[124. 二叉树中的最大路径和](https://leetcode.cn/problems/binary-tree-maximum-path-sum/)

求二叉树中任意节点到任意节点的最大路径和（路径不必经过根）。树形 DP 最核心的通用思维，是把每个节点处计算的两个量彻底拆开：
- **以当前节点为最高拐点、连通左右子树的完整答案**：`node->val + left_gain + right_gain`，用来刷新全局 `max_sum_`；
- **向上返回给父节点的单边最大链贡献**：`node->val + max(left_gain, right_gain)`，因为路径往上延伸不能分叉。如果某侧子树贡献小于 0，直接截断取 `0`。二叉树的直径（543 题）、最长同值路径（687 题）都完全复用这一套后序汇总结构。

```cpp
#include <algorithm>
#include <climits>

class Solution {
private:
    int max_sum_ = INT_MIN;

    int maxGain(TreeNode* node) {
        if (!node) return 0;
        int left_gain = std::max(0, maxGain(node->left));
        int right_gain = std::max(0, maxGain(node->right));
        max_sum_ = std::max(max_sum_, node->val + left_gain + right_gain);
        return node->val + std::max(left_gain, right_gain);
    }

public:
    int maxPathSum(TreeNode* root) {
        maxGain(root);
        return max_sum_;
    }
};
```

### 9. 双向 BFS 状态搜索：[127. 单词接龙](https://leetcode.cn/problems/word-ladder/)

每次改一个字母从 `beginWord` 走到 `endWord`，求最短步数。在无权状态转移图中，单向 BFS 的搜索树按分支因子 $b$ 呈 $b^d$ 指数膨胀；而只要起点和终点都已知，**双向 BFS** 每次只扩展当前集合更小的那一端（`if (q1.size() > q2.size()) swap(q1, q2)`），把状态数压到 $2 \cdot b^{d/2}$。同时通过按位替换 26 个字母查哈希表生成邻居，省去了 $O(N^2)$ 建图开销。

```cpp
#include <string>
#include <unordered_set>
#include <utility>
#include <vector>

class Solution {
public:
    int ladderLength(std::string beginWord, std::string endWord, std::vector<std::string>& wordList) {
        std::unordered_set<std::string> dict(wordList.begin(), wordList.end());
        if (!dict.contains(endWord)) return 0;

        std::unordered_set<std::string> q1{beginWord}, q2{endWord};
        dict.erase(beginWord);
        dict.erase(endWord);

        int steps = 1;
        while (!q1.empty() && !q2.empty()) {
            if (q1.size() > q2.size()) std::swap(q1, q2);
            std::unordered_set<std::string> next_q;
            for (std::string word : q1) {
                for (size_t i = 0; i < word.size(); ++i) {
                    char orig = word[i];
                    for (char c = 'a'; c <= 'z'; ++c) {
                        if (c == orig) continue;
                        word[i] = c;
                        if (q2.contains(word)) return steps + 1;
                        if (dict.erase(word)) next_q.insert(word);
                    }
                    word[i] = orig;
                }
            }
            q1 = std::move(next_q);
            ++steps;
        }
        return 0;
    }
};
```

### 10. 线性 DP 降维与双序列 DP：[300. 最长递增子序列](https://leetcode.cn/problems/longest-increasing-subsequence/) & [72. 编辑距离](https://leetcode.cn/problems/edit-distance/)

动态规划最常见的两种形态，一是**利用贪心单调性把 $O(n^2)$ 线性 DP 降到 $O(n \log n)$**，二是**双序列二维状态对齐**。

- **300 题（LIS）**：定义 `tails[k]` 表示长度为 `k + 1` 的递增子序列中**末尾元素的最小值**。因为同长度下末尾越小越容易接后续元素，`tails` 天然严格递增，每次用 `std::lower_bound` 替换第一个 `>= x` 的位置即可。
- **72 题（编辑距离）**：定义 `dp[i][j]` 为 `word1[0..i)` 变成 `word2[0..j)` 的最少步数，末尾字符相等直接继承左上角 `dp[i - 1][j - 1]`，不等则在删（上）、插（左）、替（左上）三个方向取最小 `+ 1`（LCS 1143 题、正则匹配 10 题都是同一张二维表）。

```cpp
#include <algorithm>
#include <string>
#include <vector>

// 300. LIS: 贪心 + 二分 O(n log n)
int lengthOfLIS(const std::vector<int>& nums) {
    std::vector<int> tails;
    for (int x : nums) {
        auto it = std::lower_bound(tails.begin(), tails.end(), x);
        if (it == tails.end()) tails.push_back(x);
        else *it = x;
    }
    return tails.size();
}

// 72. 编辑距离: 双序列二维 DP O(m * n)
int minDistance(const std::string& w1, const std::string& w2) {
    int m = w1.size(), n = w2.size();
    std::vector<std::vector<int>> dp(m + 1, std::vector<int>(n + 1, 0));
    for (int i = 0; i <= m; ++i) dp[i][0] = i;
    for (int j = 0; j <= n; ++j) dp[0][j] = j;

    for (int i = 1; i <= m; ++i) {
        for (int j = 1; j <= n; ++j) {
            if (w1[i - 1] == w2[j - 1]) {
                dp[i][j] = dp[i - 1][j - 1];
            } else {
                dp[i][j] = 1 + std::min({dp[i - 1][j], dp[i][j - 1], dp[i - 1][j - 1]});
            }
        }
    }
    return dp[m][n];
}
```

### 11. 并查集与带权最短路：[684. 冗余连接](https://leetcode.cn/problems/redundant-connection/) & [743](https://leetcode.cn/problems/network-delay-time/) / [787](https://leetcode.cn/problems/cheapest-flights-within-k-stops/)

图论工程题里最常手写的三个底层模板：
- **并查集在线判环（684）**：带路径压缩的 `find`，合并边 `[u, v]` 前若发现 `find(u) == find(v)`，说明两点早已连通，当前边必成环。
- **非负权单源最短路（743 Dijkstra）**：小顶堆按距离从小到大弹出，遇到 `d > dist[u]` 的过时条目直接跳过。
- **步数受限最短路（787 Bellman-Ford）**：跑 `k + 1` 轮松弛，每轮开始先拷贝 `prev = dist` 隔离本轮更新，严防单轮串联松弛超过 1 条边。

```cpp
#include <algorithm>
#include <climits>
#include <numeric>
#include <queue>
#include <utility>
#include <vector>

// 684. 并查集在线判环
std::vector<int> findRedundantConnection(const std::vector<std::vector<int>>& edges) {
    int n = edges.size();
    std::vector<int> parent(n + 1);
    std::iota(parent.begin(), parent.end(), 0);
    auto find = [&](auto&& self, int x) -> int {
        return parent[x] == x ? x : (parent[x] = self(self, parent[x]));
    };
    for (const auto& e : edges) {
        int ru = find(find, e[0]), rv = find(find, e[1]);
        if (ru == rv) return e;
        parent[ru] = rv;
    }
    return {};
}

// 743. 堆优化 Dijkstra（非负权最短路）
int networkDelayTime(const std::vector<std::vector<int>>& times, int n, int k) {
    std::vector<std::vector<std::pair<int, int>>> adj(n + 1);
    for (const auto& t : times) adj[t[0]].emplace_back(t[1], t[2]);

    std::vector<int> dist(n + 1, INT_MAX);
    dist[k] = 0;
    using P = std::pair<int, int>;
    std::priority_queue<P, std::vector<P>, std::greater<P>> pq;
    pq.emplace(0, k);

    while (!pq.empty()) {
        auto [d, u] = pq.top();
        pq.pop();
        if (d > dist[u]) continue;
        for (const auto& [v, w] : adj[u]) {
            if (d + w < dist[v]) {
                dist[v] = d + w;
                pq.emplace(dist[v], v);
            }
        }
    }
    int ans = *std::max_element(dist.begin() + 1, dist.end());
    return ans == INT_MAX ? -1 : ans;
}

// 787. Bellman-Ford（最多 k 次中转 = k + 1 条边）
int findCheapestPrice(int n, const std::vector<std::vector<int>>& flights, int src, int dst, int k) {
    const int kInf = 1e9;
    std::vector<int> dist(n, kInf);
    dist[src] = 0;
    for (int step = 0; step <= k; ++step) {
        std::vector<int> prev = dist; // 备份上一轮，禁止同轮串联松弛
        for (const auto& f : flights) {
            if (prev[f[0]] != kInf) {
                dist[f[1]] = std::min(dist[f[1]], prev[f[0]] + f[2]);
            }
        }
    }
    return dist[dst] == kInf ? -1 : dist[dst];
}
```

### 12. 位运算状态压缩 DP：[698. 划分为 k 个相等的子集](https://leetcode.cn/problems/partition-to-k-equal-sum-subsets/)

给定最多 16 个整数的数组 `nums`，判断能否分成 `k` 个和相等的非空子集。如果直接写朴素回溯，不同排列顺序会反复访问完全相同的“已选元素集合”，导致严重重复搜索。

当元素规模 $n \le 20$ 时，通用优化手段是**位掩码状态压缩（Bitmask DP）**：用一个 $n$ 位整数 `mask` 的每一位表示第 `i` 个元素是否已被放入桶中，`dp[mask]` 记录当前正在填充的桶的剩余已占容量（满 `target` 自动取模归 `0`，表示开启下一个新桶；`-1` 表示该状态不可达）。将数组升序排序后，一旦 `dp[mask] + nums[i] > target` 就能直接 `break` 剪枝，把原本阶乘级的回溯压到了确定的 $O(n \cdot 2^n)$。

```cpp
#include <algorithm>
#include <numeric>
#include <vector>

class Solution {
public:
    bool canPartitionKSubsets(std::vector<int>& nums, int k) {
        int sum = std::accumulate(nums.begin(), nums.end(), 0);
        if (sum % k != 0) return false;
        int target = sum / k;

        std::sort(nums.begin(), nums.end());
        if (nums.back() > target) return false;

        int n = nums.size();
        int total = 1 << n;
        std::vector<int> dp(total, -1);
        dp[0] = 0;

        for (int mask = 0; mask < total; ++mask) {
            if (dp[mask] == -1) continue;
            for (int i = 0; i < n; ++i) {
                if ((mask & (1 << i)) != 0) continue;
                if (dp[mask] + nums[i] > target) break; // 已排序，后续更大直接剪枝
                int next_mask = mask | (1 << i);
                if (dp[next_mask] == -1) {
                    dp[next_mask] = (dp[mask] + nums[i]) % target;
                }
            }
        }
        return dp[total - 1] == 0;
    }
};
```

---

**整理基线说明**：本文由多轮系统与算法专题梳理沉淀而成，对应的 C++20 与 Python 可运行代码及单元测试已归档至 `xlab` 仓库的 [cc/cclab/src/utils/](https://github.com/LiShuMing/xlab)（`singleflight.h`、`window_sum.h`、`task_dag.h`、`prefix_cache_lru.h`）与 `python/pylab/`。
