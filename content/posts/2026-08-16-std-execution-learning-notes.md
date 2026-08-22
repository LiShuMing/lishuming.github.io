---
title: "异步执行的三种答案：从 Seastar、stdexec 与 brpc 源码理解调度"
slug: "seastar-stdexec-brpc-async-execution"
date: 2026-08-16T10:00:00+08:00
lastmod: 2026-08-22T21:10:00+08:00
draft: false
categories:
  - C++
  - Database
tags:
  - C++26
  - stdexec
  - Seastar
  - brpc
  - Async
  - Scheduler
  - Reactor
  - Sender Receiver
description: "结合 Seastar、NVIDIA stdexec 与 Apache brpc 源码，从接口、状态机、调度、资源所有权和工程边界理解异步执行，并讨论它们在数据库与高性能服务中的不同选择。"
summary: "stdexec、Seastar 和 brpc 并不是同一层的竞争方案：一个标准化异步组合协议，一个以 shard-per-core 重塑应用，一个用 M:N bthread 容纳同步 RPC 代码。本文从源码解释三者的调度与本质差异。"
toc: true
---

我第一次在大型工程里真正接触全链路异步编程，是在 Hologres 工作期间。系统以 Seastar 为基础，业务逻辑沿着 `future/promise` 继续执行，后来又加入 C++ coroutine，让代码从回调链逐渐变成接近同步的 `co_await`。

这段经历留下了一个一直没有完全回答的问题：Seastar 的性能究竟来自 Future、Reactor，还是 thread-per-core？如果 C++26 已经引入 `std::execution`，为什么还需要 Seastar？Rust 有 Tokio，brpc 又能让同步 RPC 写法获得很高并发，这些方案到底在解决同一个问题，还是只是看起来相似？

为此，我重新阅读了本地三份源码：

| 项目 | 本文源码快照 | 核心定位 |
| --- | --- | --- |
| [Seastar](https://github.com/scylladb/seastar) | `seastar-25.05.0-1193-g169810b9` | shard-per-core、shared-nothing 的完整异步应用框架 |
| [NVIDIA stdexec](https://github.com/NVIDIA/stdexec) | `nvhpc-26.05-244-ge92245d5` | C++26 `std::execution` 的实验性参考实现与扩展 |
| [Apache brpc](https://github.com/apache/brpc) | `1.16.0-47-g771de31e` | RPC 框架，以及支撑同步编程的 M:N bthread runtime |

读完后的结论可以先放在这里：

> Seastar、stdexec 与 brpc 不在同一个抽象层竞争。stdexec 定义异步工作的协议和组合方式；Seastar 通过数据分片、核绑定和 Reactor 规定整个应用如何运行；brpc 则用可迁移的栈式 bthread，把等待隐藏在同步接口背后。

真正的区别不是 `then`、`co_await` 还是 `join` 哪个写起来更漂亮，而是谁拥有数据、任务能否迁移、阻塞会伤害多大范围，以及系统如何在过载时继续保持边界。

## 先定义异步：它不等于“用户态调度”

异步最小的语义只有一件事：**发起操作与观察完成相互解耦**。

```text
issue operation
      |
      | 操作尚未完成，调用方可以处理其他工作
      v
readiness / completion event
      |
      v
resume continuation / coroutine / fiber / receiver
```

这里可能有用户态 scheduler，也可能根本没有。Linux AIO、`io_uring`、DMA 或 GPU stream 都可以让操作异步完成；一个最普通的线程池同样可以把阻塞调用包装成异步任务。反过来，存在用户态调度也不代表底层 I/O 是异步的：fiber 内调用阻塞 syscall，仍然会阻塞承载它的内核线程。

因此需要区分三个经常混用的词：

- **异步（asynchrony）**：发起与完成解耦，关注等待期间谁拥有控制权。
- **并发（concurrency）**：多个任务的生命周期重叠，关注系统能否共同推进。
- **并行（parallelism）**：多个任务在同一时刻使用不同执行资源，关注吞吐和算力利用。

一个单线程 Reactor 可以高度异步、并发，却没有 CPU 并行；四线程并行执行四个阻塞函数，可以并行，却不一定拥有异步 I/O。

从系统实现看，一套异步框架至少包含五个可以独立变化的层次：

```text
用户控制流       then / co_await / 同步函数 + join
完成协议         future / receiver / callback / wake fiber
运行期状态       continuation / operation state / coroutine frame / stack
调度与放置       pinned shard / work stealing / scheduler implementation
事件与资源       epoll / io_uring / timer / GPU / queue / backpressure
```

Seastar 同时实现了这五层；brpc 主要围绕 RPC 实现后四层，并用栈式 bthread 保留同步控制流；stdexec 重点标准化前三层与“调度器的接口”，但不提供一个唯一的 Reactor 或线程池策略。

## 三种架构先看全貌

| 维度 | Seastar | stdexec / `std::execution` | brpc / bthread |
| --- | --- | --- | --- |
| 本质 | 完整异步 runtime + 应用架构 | 异步协议、类型系统和组合算法 | 完整 RPC runtime + M:N 用户线程 |
| 默认执行单元 | 每核一个 Reactor thread 上的 continuation/task | 由 scheduler 决定 | M 个 stackful bthread 映射到 N 个 pthread |
| 放置策略 | shard affinity，默认不迁移 | 不规定；run loop、线程池、GPU 都可以 | local run queue + work stealing，可跨 worker 恢复 |
| 数据模型 | shard-local ownership，跨核显式消息 | 不规定 | 共享地址空间，用户负责同步 |
| 启动语义 | 异步 API 通常调用即发起，Future 表示进行中的结果 | Sender 通常是 lazy；`connect` 后 `start` 才启动 | 同步 RPC 阻塞 bthread；异步 RPC 调用即发起 |
| 等待状态 | Future continuation 或 coroutine frame | Operation State 树 | 独立 bthread stack + TaskMeta |
| 完成通道 | value 或 exception；取消多为 API 级协议 | `set_value / set_error / set_stopped` | RPC Controller、错误码、done callback、bthread stop/join |
| 阻塞容忍度 | 极低：阻塞一个 shard 就阻塞该核所有任务 | 取决于 scheduler | 较高：阻塞一个 worker 后其他 worker仍可推进，但不是无限 |
| 资源治理 | scheduling group、I/O queue、semaphore、SMP service group | 标准协议不规定公平性和背压 | worker 数、并发限制、tag、ExecutionQueue |
| 典型价值 | 尾延迟、缓存局部性、数据库/存储引擎 | 库边界、组合、异构执行、可替换后端 | RPC 服务、遗留同步代码、多核负载均衡 |

这张表中最值得注意的是：**stdexec 的 scheduler 与 Seastar/brpc 的 scheduler 不是同等重量的对象**。前者是一个轻量、值语义的协议入口；后两者背后则是已经做出线程、队列、I/O 和数据所有权选择的运行时。

## Seastar：调度之前，先决定数据属于哪个核

[Seastar 的 shared-nothing 设计](https://seastar.io/shared-nothing/) 不是简单的“每核一个线程”。它先把应用视为同一进程内的多个 shard：

```text
CPU 0: reactor + allocator + task queues + data shard 0 + I/O queues
CPU 1: reactor + allocator + task queues + data shard 1 + I/O queues
CPU 2: reactor + allocator + task queues + data shard 2 + I/O queues
...
```

每个 shard 的普通对象由本核拥有，本核 continuation 顺序访问；其他核不能把“共享一把锁”当成默认方案，而要通过 `smp::submit_to()` 把计算发送到数据所在的 shard。它用显式通信和所有权换掉数据路径上的锁、原子引用计数与 cache-line bouncing。

这也是数据库视角下最熟悉的一点：Seastar 不是只优化 Task Scheduler，而是把 **partitioning key 一直延伸到了 CPU core**。数据放置与计算放置被绑定之后，调度器不再需要随时寻找“哪个线程空闲”，而是先回答“这份状态归谁所有”。

### Reactor 主循环：任务与 I/O 在同一颗核上共同推进

在 [`src/core/reactor.cc`](https://github.com/scylladb/seastar/blob/master/src/core/reactor.cc) 中，`reactor::do_run()` 的主循环反复做两件事：

```cpp
while (true) {
    _cpu_sched.run_some_tasks();       // 从调度组的队列中执行 ready task

    if (_stopped) {
        break;                         // 收到停止信号后退出 Reactor
    }

    if (!poll_once() && !have_more_tasks()) {
        wait_and_process_events();     // 没有立即工作时进入事件等待
    }
}
```

代码细节比这个简化版本复杂得多：timer、网络、磁盘后端、跨核消息、idle handler 和 interrupt mode 都是 poller。但主干非常稳定：**完成事件把 continuation 放回 ready queue，Reactor 在同一线程上执行它，直到主动让出或到达可抢占检查点。**

Seastar 不是内核意义上的抢占式线程调度。`task_queue::run_tasks()` 每执行一个 task 后检查 `scheduler_need_preempt()`；默认 `task-quota-ms` 为 0.5ms，但它只能在框架设置的检查点生效。一个没有 `co_await`、没有 future 边界、也不调用 `maybe_yield()` 的长 CPU 循环，仍可以独占整个 shard。

所以 thread-per-core 的性能契约也很严格：

> 用户代码获得了无锁的 shard-local 世界，同时承诺不阻塞 Reactor，并主动把长计算切成可调度片段。

### Future：它是完成句柄，不是抽象 Scheduler

Seastar 的 [`future.hh`](https://github.com/scylladb/seastar/blob/master/include/seastar/core/future.hh) 允许写出：

```cpp
return read_request()
    .then([] (request req) {
        return query_local_shard(std::move(req));  // 上一步完成后继续
    })
    .then([] (result r) {
        return write_response(std::move(r));       // 继续返回另一个 future
    })
    .handle_exception([] (std::exception_ptr ep) {
        return make_error_response(ep);            // 异常沿 Future 链传播
    });
```

这里的 Future 通常代表已经发起或正在推进的操作。`then()` 把 continuation 注册到 state；如果上游已经 ready，release 构建中可能直接继续执行，直到调度器要求 preempt。它与 stdexec Sender 的“先描述、后启动”不同。

源码也纠正了一个常见误解：Seastar Future 链并不是天然零分配。`future::schedule(...)` 中会 `new continuation<...>`，再把 task 接到 promise 或 Reactor。Seastar 通过单线程假设、定制 allocator、ready-future fast path 和较轻的状态结构降低成本，但“高性能”不等于“从不分配”。

C++ coroutine 并没有替换 Reactor。[`coroutine.hh`](https://github.com/scylladb/seastar/blob/master/include/seastar/core/coroutine.hh) 中的 `promise_type` 本身继承 Seastar task，携带 scheduling group；`co_await future` 只是让编译器生成状态机，并在 future ready 时把 coroutine task 重新排入同一个调度体系。

换句话说：

```text
future.then(...)  ─┐
                   ├─> Seastar task -> scheduling group -> reactor
co_await future   ─┘
```

Future 与 coroutine 是控制流表达，Reactor 才是执行引擎。

### Scheduling Group：公平不是线程数，而是资源份额

一个核上同时存在 foreground query、compaction、streaming 和 maintenance。即使所有任务都不阻塞，没有资源隔离也会产生尾延迟。

Seastar 为每个 scheduling group 建立 task queue。在 `task_queue_group::run_tasks_impl()` 中，活跃实体依据 `vruntime` 选择；执行后按实际 CPU 时间和 shares 更新 accounting，再决定是否重新插入。这更接近单核上的 weighted fair scheduling，而不是一个简单 FIFO。

I/O 侧还有独立的 priority class 与 I/O queue。CPU shares 和 I/O shares 共同表达“谁可以消耗多少资源”。Semaphore、gate 与 `max_concurrent_for_each` 等工具则负责限制 in-flight 工作和生命周期。

这对数据库尤其重要：异步化只会让系统更容易制造并发，**不会自动产生背压**。如果入口无限创建 Future，内存、I/O queue 和下游服务仍会先被淹没。

### 跨核不是普通函数调用

[`smp.hh`](https://github.com/scylladb/seastar/blob/master/include/seastar/core/smp.hh) 中的 `smp_message_queue` 使用有界 SPSC queue 批量传递 work item；`smp::submit_to(cpu, fn)` 在目标 shard 执行函数，再把完成结果送回调用 shard。

```cpp
co_await seastar::smp::submit_to(owner_shard, [key] {
    return local_table.find(key);      // 在拥有数据的 shard 上访问
});
```

`smp_service_group` 进一步限制目标 shard 上的非本地请求并发。源码注释甚至要求嵌套 service group 调用形成 DAG，否则可能产生 ABBA 类死锁。

这说明 shared-nothing 并不是“完全没有共享”。底层仍使用同一物理内存和跨核队列；它真正做的是把共享从任意对象读写，收敛成少数明确、可计量的消息边界。

### Seastar 的收益与代价

收益是：

- 数据与计算核亲和，cache locality 更稳定；
- 普通 shard-local 路径不需要锁；
- 每核 Reactor、CPU shares 与 I/O shares 使资源消耗可观测；
- 很适合 ScyllaDB、Redpanda、Ceph Crimson 这类能够整体控制执行模型的系统。

代价同样是架构级的：

- 第三方阻塞库难以直接接入；
- 跨 shard 聚合、全局 metadata 和负载倾斜需要显式处理；
- 热 key 不会因为别的核空闲就自动迁移；
- 一个长任务或意外阻塞会放大成整个 shard 的尾延迟；
- 应用生命周期、数据结构和运维模型都会被 Seastar 塑形。

Seastar 的快，根本上来自约束，而不是 Future 语法本身。

## stdexec：标准化的是完成协议，不是某个线程池

[P2300R10](https://wg21.link/P2300R10) 已进入 C++26 工作草案，目标是为 C++ 建立一套可组合的异步执行词汇。NVIDIA stdexec 的 README 将自己定义为参考实现，同时明确提示项目仍是 experimental，并会跟随标准演进。

最小模型由五个对象组成：

```text
Sender                 描述将要发生的异步工作
Receiver               接收 value / error / stopped 三种终止信号
Environment            提供 scheduler、stop token、allocator、domain 等上下文
Operation State        connect 后形成的运行期状态与所有权树
Scheduler              生成一个“在该执行资源上完成”的 schedule sender
```

### 从源码看 Sender 的两阶段生命

stdexec 的中心不是 `then()`，而是：

```cpp
auto op = stdexec::connect(sender, receiver);  // 1. 物化 Operation State
stdexec::start(op);                            // 2. 真正启动
```

[`__connect.hpp`](https://github.com/NVIDIA/stdexec/blob/main/include/stdexec/__detail__/__connect.hpp) 会先根据 receiver environment 做 `transform_sender`，再调用 sender 的静态成员、成员函数或兼容 customization，返回 Operation State。

[`__operation_states.hpp`](https://github.com/NVIDIA/stdexec/blob/main/include/stdexec/__detail__/__operation_states.hpp) 对 `start` 的约束很关键：

- Operation State 必须以 lvalue 启动；
- `start()` 必须 `noexcept` 且返回 `void`；
- Operation State 必须一直存活到 completion；
- completion 可能在 `start()` 返回前内联发生，也可能很久以后发生。

这说明 Sender 不是 Future handle。它更像一份可变换的执行计划；Operation State 才是这次执行的实例。

```text
构图阶段                         运行阶段

just | then | when_all           connect(sender, receiver)
          |                                 |
          v                                 v
Sender expression tree          Operation State tree
                                            |
                                          start
                                            |
                           set_value / set_error / set_stopped
```

### Completion Signatures：把异步函数的效果写进类型

同步函数有返回类型和异常约定，传统 callback API 往往把这些信息拆散。Sender 用 completion signatures 描述所有合法终止：

```cpp
stdexec::completion_signatures<
    stdexec::set_value_t(int),                 // 成功并产生 int
    stdexec::set_error_t(std::error_code),     // 失败并产生 error_code
    stdexec::set_stopped_t()                   // 协作式停止
>;
```

[`__receivers.hpp`](https://github.com/NVIDIA/stdexec/blob/main/include/stdexec/__detail__/__receivers.hpp) 要求三个 completion 调用都为 `noexcept`，一次已启动的 operation 最终只能完成一次。`then`、`upon_error`、`when_all` 等算法会在编译期变换这些签名。

它的价值不只是“类型安全”。它建立了一套异步效果代数：

- `then`：只变换 value channel；
- `upon_error`：恢复或变换 error channel；
- `upon_stopped`：处理 stopped channel；
- `let_value`：根据上游值动态返回另一个 Sender，相当于异步 flat-map；
- `when_all`：组合多个 operation 的值、失败与停止关系。

### Scheduler：一个位置句柄，而不是调度策略本身

[`__schedulers.hpp`](https://github.com/NVIDIA/stdexec/blob/main/include/stdexec/__detail__/__schedulers.hpp) 对 Scheduler 的核心要求很小：它是可比较、可移动的值类型，并能通过 `schedule(scheduler)` 返回一个 Sender。这个 Sender 启动后，在目标执行上下文中调用 receiver。

因此下面的 pipeline 只表达“先切到线程池，再执行两个变换”：

```cpp
#include <stdexec/execution.hpp>
#include <exec/static_thread_pool.hpp>

exec::static_thread_pool pool{4};               // stdexec 扩展提供的运行时
auto scheduler = pool.get_scheduler();          // 轻量执行位置句柄

auto work =
    stdexec::schedule(scheduler)                 // 生成 lazy schedule sender
  | stdexec::then([] {
        return read_metadata();                  // 在线程池上下文执行
    })
  | stdexec::then([] (metadata m) {
        return build_index(std::move(m));        // value 沿 pipeline 传递
    });

auto result = stdexec::sync_wait(std::move(work)); // consumer 连接并启动
```

`stdexec::schedule` 不负责决定线程池是否 work stealing、是否绑核、是否有优先级。当前仓库的 `exec::static_thread_pool` 选择了自己的 work-stealing 与 NUMA 实现；`stdexec::run_loop` 是另一种队列；`exec::io_uring_context`、`nvexec::stream_context` 又分别连接 Linux I/O 与 CUDA stream。这些是具体 runtime 或扩展，不是 Scheduler concept 自动赠送的能力。

所以“stdexec 支持 GPU”更准确的说法是：

> Sender/Receiver 协议能够描述并组合异构执行，stdexec 仓库的 nvexec 证明了 CUDA 后端可以适配；C++26 核心协议本身并不实现 GPU runtime。

### starts_on 与 continues_on：不要把“在哪开始”和“在哪继续”混为一谈

- `starts_on(scheduler, sender)`：让一个 Sender 在指定 scheduler 上启动。
- `continues_on(sender, scheduler)`：把上游完成后的 continuation 转移到目标 scheduler。

```cpp
auto request =
    stdexec::starts_on(io_scheduler, read_page())
  | stdexec::continues_on(cpu_scheduler)
  | stdexec::then(parse_page);
```

执行位置是数据流的一部分，这正是 stdexec 比 `std::future` 更完整的地方。但 scheduler hop 也不是免费的：它通常意味着入队、同步和 cache locality 变化。接口可组合不代表任意切换执行上下文都合理。

### “零开销”需要更克制地理解

Sender expression 与 Operation State 通常可以静态展开，避免每个节点都使用虚函数、type erasure 或共享堆状态。这为 compiler fusion、内联和自定义存储留下了空间。

但 stdexec **不保证整个异步程序零堆分配**：

- type-erased Sender 需要存储与间接调用；
- `async_scope`、shared completion、dynamic fan-out 可能需要共享状态；
- coroutine frame 是否 heap allocate 取决于 promise、lifetime 与编译器优化；
- I/O runtime 自己仍需要队列、注册项与 buffer；
- Operation State 只是可以由调用方内嵌，不等于永远位于线程栈。

更准确的判断是：stdexec 不强制 `std::future` 式共享状态，并允许库作者把 allocation policy 暴露在 environment 和 operation lifetime 中。

### stdexec 缺少什么

它刻意不规定：

- 应用用 thread-per-core 还是 work stealing；
- socket 由哪个 Reactor poll；
- 数据应当 shard-local 还是共享；
- CPU 与 I/O 如何做公平调度；
- 过载时应该排队、拒绝、降级还是传播背压；
- tracing context、租户和 query priority 如何穿过业务图。

Environment、domain 和自定义 Scheduler 提供了承载这些策略的位置，但策略本身仍要由 runtime 和应用实现。stdexec 是一套“异步 ABI 以上、业务框架以下”的协议层，而不是另一个 Seastar。

## brpc：用可迁移的栈换回同步控制流

[brpc](https://brpc.apache.org/docs/overview/) 面对的是另一类工程现实：大量 RPC 服务存在同步接口、遗留库和不可预测的 handler，很难要求整条调用链都变成 non-blocking callback。

它的答案是 bthread。官方 [`bthread` 文档](https://github.com/apache/brpc/blob/master/docs/cn/bthread.md) 将其定义为 M:N 线程库：M 个用户态 bthread 映射到 N 个 pthread worker。

```text
RPC / timer / I/O completion
           |
           v
       runnable bthread
           |
     local run queue
       /         \
 pthread 0     pthread 1 ... pthread N
       \         /
        work stealing
```

### 从源码看 work stealing 与栈式切换

[`TaskGroup`](https://github.com/apache/brpc/blob/master/src/bthread/task_group.h) 是每个 worker 的线程本地调度组，内部有 `WorkStealingQueue` 和 remote queue；[`TaskControl`](https://github.com/apache/brpc/blob/master/src/bthread/task_control.h) 管理所有 worker，并提供 `steal_task()`。

`TaskGroup::sched()` 的核心路径是：

```cpp
if (!local_run_queue.pop(&next_tid)
    && !task_control.steal_task(&next_tid)) {
    next_tid = main_tid;               // 无任务时回到 worker 的 main task
}

sched_to(current_group, next_tid);     // 保存当前栈并切换到目标 bthread
```

新 bthread 的函数在独立 stack 上执行；调用 bthread-aware 的 mutex、butex、sleep 或 RPC wait 时，当前 bthread 挂起，pthread worker 转去运行另一个 ready bthread。恢复时，它不保证回到原 pthread，因此依赖 pthread-local session state 的代码可能出错。

与 Seastar 的 continuation 不同，bthread 保存的是一整段同步调用栈。它消耗更多栈空间与上下文切换成本，却让循环、异常路径、深层函数和 RAII 保持普通同步写法。

### I/O Reactor 与用户代码之间的关系

Linux 下 [`EventDispatcher`](https://github.com/apache/brpc/blob/master/src/brpc/event_dispatcher_epoll.cpp) 在 `epoll_wait` 中取得 edge-triggered 事件，再调用 input/output event callback。网络响应最终唤醒等待的 bthread，或触发异步 `done->Run()`。

brpc 文档强调它不采用传统“固定 I/O 线程收到事件，再总是转发到另一组 worker”的硬分层。事件处理与 bthread 调度尽量减少无必要的上下文跳转；每个请求通常由一个 bthread 承载。

用户可以保留同步代码：

```cpp
brpc::Controller cntl;
service_stub.Query(&cntl, &request, &response, nullptr);

// 当前 bthread 在等待 RPC 时挂起；
// 承载它的 pthread 可以继续运行其他 ready bthread。
if (cntl.Failed()) {
    handle_rpc_error(cntl.ErrorText());
}
```

也可以传入 closure 使用异步接口。brpc 的调用在 `CallMethod` 时已经发起，不像 Sender 还要等待 `start`。

### 阻塞兼容不是阻塞免费

如果 bthread 调用的是 bthread-aware 等待原语，只挂起当前 bthread。如果它直接进入阻塞 syscall 或 pthread primitive，则承载它的 pthread worker 也会被阻塞；其他 worker 可以偷走 ready task，但当所有 worker 都阻塞时，RPC 收发同样无法继续。

因此 bthread 是比单线程 Reactor 更宽容的模型，不是把阻塞成本消除。增加 worker 也可能只是增加同一把锁或同一下游上的等待者。brpc 提供最大并发限制，正是为了在过载时保护 worker 与下游。

官方 [`异步接口还是 bthread`](https://github.com/apache/brpc/blob/master/docs/cn/bthread_or_not.md) 提供了一个很实用的判断：`QPS × latency` 近似系统中的平均并发请求数。若它远大于 CPU 核数，等待占比高，异步调用能节省大量挂起栈；若与核数同量级，优先保留同步代码的可读性往往更合理。

### brpc 的设计取舍

brpc 选择：

- 允许 task 跨核迁移，以 work stealing 缓解不均衡；
- 允许同步 RPC 写法，以 stackful bthread 保存控制流；
- 共享地址空间与传统 C++ 对象模型，兼容现有工程；
- 将 timeout、retry、backup request、load balancing、circuit breaker 放进 RPC 语义。

代价是：

- shared mutable state 仍需锁或无锁结构；
- task 迁移会带来 cache 与 NUMA 成本；
- 每个挂起 bthread 仍需 stack 和 TaskMeta；
- pthread TLS 与 bthread migration 有语义陷阱；
- 它解决的是高性能 RPC 服务，不是通用异构执行协议。

## 调度的本质差异：局部性、均衡与通用性

把三者放在一起，最根本的不是 API，而是三个互相拉扯的目标。

### Seastar：优先局部性与可预测性

任务不随意迁移，数据属于固定 shard。负载均衡主要在请求分片、数据分区和 admission control 阶段完成。它愿意接受热点与跨 shard 编程复杂度，以换取 cache locality 和稳定尾延迟。

### brpc：优先动态均衡与兼容性

bthread 可以在不同 worker 上继续，空闲 worker 从其他 run queue 偷任务。它愿意承担共享同步、栈和迁移成本，以容纳复杂同步 handler 和不均匀请求。

### stdexec：优先协议通用性

Sender 不承诺运行在哪，Scheduler 也不承诺采用哪种队列。它把 placement policy 延迟到具体 backend：可以实现 pinned scheduler，也可以实现 work-stealing pool，还可以表示 GPU stream。

```text
                     stdexec
             统一“如何描述和完成”
                 /              \
                /                \
       Seastar scheduler      pool scheduler
       shard affinity         work stealing
              |                    |
       局部性 / 隔离          均衡 / 通用
```

因此不存在一个“stdexec 调度算法”可以直接和 Seastar 的 vruntime 或 brpc 的 work stealing 比性能。应该比较的是同一 workload 下的具体 Scheduler 实现、I/O backend、状态布局和资源治理。

## 接口差异：三种写法对应三种状态所有权

同一个“读取两份数据后合并”的逻辑，可以看到完全不同的状态表示。

### Seastar Future：结果驱动 continuation

```cpp
return seastar::when_all_succeed(read_left(), read_right())
    .then([] (left_value l, right_value r) {
        return merge(std::move(l), std::move(r));
    });
```

- `read_left()`、`read_right()` 通常已经启动；
- Future 链拥有 continuation；
- 结果或 exception 向下传播；
- shard affinity 由当前 Reactor 与 API 约定维持。

### stdexec Sender：先构图，再物化执行

```cpp
auto merged =
    stdexec::when_all(read_left_sender(), read_right_sender())
  | stdexec::then([] (left_value l, right_value r) {
        return merge(std::move(l), std::move(r));
    });

auto result = stdexec::sync_wait(std::move(merged));
```

- 构造 Sender 时通常没有启动 I/O；
- `connect` 生成持有所有子状态的 Operation State；
- `start` 后沿三种 completion channel 推进；
- 执行位置来自 Sender attributes、receiver environment 与 scheduler。

### brpc bthread：栈拥有控制流

```cpp
left_value left;
right_value right;

bthread_t left_task;
bthread_start_background(&left_task, nullptr, read_left, &left);

right = read_right_sync();             // 当前 bthread 做另一份工作
bthread_join(left_task, nullptr);      // 挂起当前 bthread，保留完整调用栈

return merge(std::move(left), std::move(right));
```

- 操作在函数调用时启动；
- 局部变量与控制流自然留在 bthread stack；
- join/wait 让出 worker；
- 共享结果需要明确同步和生命周期。

这三种接口都能表达异步，但成本中心不同：

| 模型 | 主要状态载体 | 优势 | 主要风险 |
| --- | --- | --- | --- |
| Seastar Future | Future state + continuation | 小状态、与 Reactor 深度融合 | continuation lifetime、意外阻塞 |
| stdexec Sender | 静态表达式 + Operation State | 可组合、可定制、效果类型化 | 类型复杂度、编译成本、runtime 仍需自选 |
| brpc bthread | TaskMeta + 独立 stack | 同步控制流、RAII、遗留兼容 | 栈成本、迁移/TLS、共享同步 |

## 取消、错误和生命周期：异步系统真正难的部分

“任务能被调度”只是开始。生产系统更关心请求超时后谁停止、资源何时释放、子任务是否变成孤儿。

### stdexec：协议级三通道

`set_value`、`set_error`、`set_stopped` 是互斥终止。Receiver environment 可提供 stop token，组合算法可以传播停止。`scope` 一类结构化并发工具要求父作用域等待子 operation 结束。

但 stop 仍然是协作式的：底层 operation 必须观察 token，并正确完成为 stopped。协议表达了取消，不代表所有硬件与 syscall 都可瞬间撤销。

### Seastar：组件化取消与显式排空

Seastar Future 原生主要是 value/exception。取消通常由 `abort_source`、timeout wrapper 或具体 I/O API 实现；`gate` 保证关闭服务时等待所有 in-flight operation，semaphore 控制并发资源。

这套方式很工程化，但取消语义不是每条 Future 类型中的统一第三通道。调用者必须知道所用 API 是否支持 abort、超时后底层工作是否仍在继续。

### brpc：RPC 生命周期优先

`brpc::Controller` 管理 deadline、错误、retry 与 cancellation，异步调用以 closure 完成；bthread 自身有 stop/interruption/join，但 stop 同样不是强制抢占任意用户代码。

三者共同说明：

> 取消不是“把线程杀掉”，而是从请求根节点传播意图，让每个资源拥有者停止产生新工作、结束已有操作，并最终完成生命周期汇合。

## 背压不是异步框架的副产品

异步接口很容易把“线程没有阻塞”误认为“系统仍然健康”。实际上，一百万个等待中的 Future、Operation State 或 bthread，只是用不同内存形式保存了一百万份未完成工作。

数据库系统至少要限制：

- 入口同时执行的 query/request 数；
- 每个 tenant 或 workload group 的 CPU shares；
- 跨 shard 请求数量；
- 磁盘队列深度与 buffer 占用；
- 下游 RPC in-flight 数；
- background compaction、flush 与 foreground query 的资源比例。

Seastar 将这类治理放在 scheduling group、I/O priority、semaphore 和 SMP service group；brpc 提供 server concurrency limiter、worker 控制和 RPC 策略；stdexec 则只提供组合这些机制的协议，背压策略需要 Scheduler、sequence sender 或业务层实现。

从这个角度看，异步化的终点不是“没有线程等待”，而是：

```text
admission -> bounded concurrency -> fair scheduling
          -> completion/cancellation -> resource reclamation
```

缺少其中任何一环，异步只会让过载来得更快。

## 能否让 Seastar 或 brpc 接入 stdexec

理论上完全可以，但不是给类加一个 `schedule()` 方法就结束。

### Seastar Scheduler 适配需要处理

- schedule sender 必须把 Operation State 安全投递到目标 shard；
- completion 要回到哪个 shard，必须有明确语义；
- Operation State 生命周期不能短于跨核消息；
- Seastar exception 与 `set_error`、abort 与 `set_stopped` 需要转换；
- scheduling group、allocator 与 shard id 应通过 environment 传播；
- ready sender 是否允许 inline completion，要遵守 Reactor 的 preemption 约定。

一个好的适配层能让通用 Sender algorithm 运行在 Seastar 上，但不会取消 shard ownership，也不会让阻塞算法突然变安全。

### brpc Sender 适配需要处理

- `CallMethod` 是 hot operation，必须推迟到 Operation State 的 `start()`；
- Controller、request、response 与 closure 必须由 Operation State 持有；
- timeout/cancel 要映射成 error 还是 stopped，需要稳定约定；
- completion 从 bthread 回调触发时要满足 receiver 的串行与 lifetime 契约；
- 如果下游 continuation 需要特定 Scheduler，不能假设回调线程就是目标执行上下文。

适配的价值在于让 brpc RPC 进入通用 pipeline，而不是把 brpc 重新实现成 stdexec runtime。

## 从数据库与大数据系统看如何选择

### 选择 Seastar：愿意让执行模型塑造整个引擎

适合：

- 数据天然可按 key、tablet、partition 或 shard 拆分；
- 网络、存储和 timer 都能走非阻塞链路；
- 关注高吞吐和可预测 P99；
- 团队可以长期维护 shard ownership、resource group 与异步栈。

它尤其适合 database/storage engine 的 data plane。但 metadata、全局事务、热点和跨 shard operator 仍需要额外设计，不能把 shared-nothing 当作免费线性扩展。

### 选择 brpc：RPC 边界和工程兼容更重要

适合：

- 大量同步业务代码、protobuf service 和现有阻塞库；
- 请求计算量与等待时间不均匀，需要 work stealing；
- 需要成熟的 timeout、retry、backup request、load balancer 与可观测性；
- 能接受 shared-state synchronization，并用并发限制保护 worker。

在很多大数据服务里，brpc 更像一个可靠的网络入口。引擎内部仍可能使用自己的 pipeline、线程池或 operator scheduler。

### 选择 stdexec：需要稳定的异步组件边界

适合：

- 希望算法与具体线程池、I/O loop 或 GPU stream 解耦；
- 构建可组合 library，而不是规定整个进程架构；
- 需要把 value/error/stopped 与 execution context 写进泛型接口；
- 愿意承担模板诊断、编译时间和生态仍在演进的成本。

stdexec 最有价值的位置可能不是“重写一个数据库 runtime”，而是成为 runtime 之间的公共异步词汇：存储算子返回 Sender，部署时再绑定 thread pool、io_uring、Seastar shard 或 GPU backend。

## 我现在对异步执行的理解

重新阅读三份源码后，我不再把异步简单理解为“用户态调度”或“用少量线程承载大量任务”。更完整的理解是：

1. **异步是一种完成协议。** 发起者不等待完成，结果通过 continuation、receiver、coroutine resume 或 fiber wakeup 交付。
2. **状态机是一种内存布局。** callback object、Operation State、coroutine frame 和 bthread stack，都在保存“暂停后如何继续”。
3. **调度是一种放置决策。** 它决定 ready work 在哪个核、哪个队列、哪个优先级和哪个资源域运行。
4. **数据所有权决定调度自由度。** Seastar 用固定 ownership 限制迁移；brpc 用共享地址空间允许 work stealing；stdexec 不替应用做这个决定。
5. **背压决定系统是否能活过峰值。** 能创建无限异步任务不是能力，而是风险。
6. **取消与生命周期决定系统是否正确。** 没有结构化收敛的后台任务，最终会变成资源泄漏和 shutdown race。

因此，Seastar 与 stdexec 最本质的区别可以压缩成一句话：

> stdexec 试图标准化“异步工作如何被描述和组合”，Seastar 则规定“整台机器上的数据和工作应当如何被放置和推进”。

brpc 则提供第三个答案：

> 当现实代码无法全链路 non-blocking 时，用 M:N 栈式线程保留同步思维，再通过 work stealing 和异步 I/O 提高整体并发。

这三条路线没有绝对胜负。它们分别优化协议复用、硬件局部性与工程可维护性。真正专业的选择，不是追逐某个最新异步语法，而是先确认 workload：等待发生在哪里、状态由谁拥有、任务是否允许迁移、过载如何被限制、取消如何收敛。

对数据库与大数据引擎而言，这些问题远比 `future.then`、`co_await` 或 Sender pipeline 的表面差异更接近系统本质。

## 参考资料

- [P2300R10：`std::execution`](https://wg21.link/P2300R10)
- [NVIDIA stdexec：C++ Sender/Receiver 参考实现](https://github.com/NVIDIA/stdexec)
- [stdexec `connect` 源码](https://github.com/NVIDIA/stdexec/blob/main/include/stdexec/__detail__/__connect.hpp)
- [stdexec Operation State 源码](https://github.com/NVIDIA/stdexec/blob/main/include/stdexec/__detail__/__operation_states.hpp)
- [stdexec Scheduler 源码](https://github.com/NVIDIA/stdexec/blob/main/include/stdexec/__detail__/__schedulers.hpp)
- [Seastar Shared-nothing Design](https://seastar.io/shared-nothing/)
- [Seastar 异步编程教程](https://docs.seastar.io/master/tutorial.html)
- [Seastar Reactor 源码](https://github.com/scylladb/seastar/blob/master/src/core/reactor.cc)
- [Seastar Future 源码](https://github.com/scylladb/seastar/blob/master/include/seastar/core/future.hh)
- [Seastar SMP 跨核队列源码](https://github.com/scylladb/seastar/blob/master/include/seastar/core/smp.hh)
- [Apache brpc Overview](https://brpc.apache.org/docs/overview/)
- [brpc 线程模型](https://github.com/apache/brpc/blob/master/docs/cn/threading_overview.md)
- [bthread 设计与使用边界](https://github.com/apache/brpc/blob/master/docs/cn/bthread.md)
- [brpc：异步接口还是 bthread](https://github.com/apache/brpc/blob/master/docs/cn/bthread_or_not.md)
- [brpc TaskGroup 源码](https://github.com/apache/brpc/blob/master/src/bthread/task_group.cpp)
