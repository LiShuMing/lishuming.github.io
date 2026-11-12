---
title: "【源码】Go Runtime：从内存分配、goroutine 调度到 GC"
slug: "go-runtime-gc-goroutine-source-notes"
date: 2026-10-22T00:00:00+08:00
categories:
  - Programming Languages
tags:
  - Go
  - Runtime
  - GC
  - Goroutine
  - Linux
  - Source Code
description: "从高并发短命任务的资源矛盾出发，基于 Go 1.26.2 源码拆解逻辑处理器 P 如何串起无锁 mcache 分配、GMP 调度与阻塞 syscall 交接、Green Tea GC 局部性扫描与页归还，并用三组本机实验逐一验证。"
draft: false
toc: true
math: true
---

设想一个典型的高并发服务端场景：几十万条网络连接同时在线，每秒钟有上百万个生命周期只有几毫秒的任务被创建出来——每个任务分配十几个 32 字节或 64 字节的小对象，在管道或套接字上等一次 I/O，拼完响应后立刻销毁。

如果我们用最朴素的方式去搭这套运行时——**每个任务对应一个操作系统线程 + 传统全局 `malloc/free` 堆 + 全量暂停（Stop-The-World，STW）的标记清扫回收器**——系统马上会在三个地方撞墙：

1. **线程栈与内核切换爆炸**：操作系统线程默认预留数 MB 的虚拟栈空间，每次阻塞和唤醒都要陷入内核改写调度队列。几十万个处于等待态的任务还没开始干活，光线程上下文和内核调度开销就会把机器拖垮。
2. **分配器的本地缓存无处安放**：成熟的 C 分配器（如 `tcmalloc`、`jemalloc`）同样不会每次 `malloc` 都进内核调 `mmap`，而是靠每线程缓存（Thread-Local Cache）避开全局堆锁。但在 Go 里，缓存挂给谁？如果挂给每个 goroutine（`G`），一百万个挂起等待的 `G` 会把空闲内存碎片死死扣在自己兜里；如果挂给每个系统线程（`M`），一旦几百个线程因为阻塞系统调用或 Cgo 卡在内核里，它们持有的本地缓存同样会被白白冻结。
3. **并发修改与尾延迟的冲突**：为了保住接口尾延迟，垃圾回收（Garbage Collection，GC）绝不能长时间停住所有业务线程去扫几 GB 的对象图；可一旦允许业务代码与 GC 并发运行，goroutine 一边高速分配新对象、一边改写已有指针，随时可能把存活对象藏进扫描盲区，或者让分配速度彻底甩开后台回收进度。

把这三个矛盾放在一起看，就能明白为什么 Go Runtime 没有把分配器、调度器和 GC 做成三个彼此独立的模块，而是提炼出了一个贯穿全局的枢纽——**逻辑处理器 `P`（Logical Processor）**。

`GOMAXPROCS` 限制的既不是 goroutine 总数，也不是系统线程总数，而是同时执行普通 Go 代码的 `P` 的数量。Runtime 把小对象无锁缓存 `mcache`、本地可运行队列 `runq`、GC 标记工作区 `gcWork` 以及写屏障缓冲区 `wbBuf` 全部绑在 `P` 上：

![Go runtime 中执行、分配、标记、清扫和页回收的关系](/images/go-runtime/runtime-memory-cycle.svg)

顺着这张图看过去，原本分散在源码各处的机制立刻咬合成了一条闭环：goroutine 只要拿到 `P` 就能免锁分配小对象；一旦底层线程 `M` 陷入慢系统调用，`P` 可以带着整套本地缓存和队列金蝉脱壳交给其他 `M`；并发标记（Concurrent Marking）直接作为调度任务在 `P` 上穿插取用 CPU，阶段切换靠短暂的 STW 同步写屏障状态，最后再由并发清扫（Concurrent Sweeping）与后台页回收器（Scavenger）分步把空间退回堆和操作系统。下面我们就沿着一个对象从诞生、跨阻塞存活到最终回收的完整生命周期，走进 `go1.26.2` 源码把这笔账算清楚。

---

## 一、堆分配快路径：为什么本地缓存必须挂在 P 上

一行 `new(T)` 或字面量取地址会不会触发堆分配，第一道关口在编译器。逃逸分析（Escape Analysis）先判定对象生命周期是否跨出了当前栈帧，配合函数内联还可以进一步做栈上分配或消除；只有确认逃逸到堆上的对象，才会在编译期改写成 [`walk/builtin.go`](https://github.com/golang/go/blob/go1.26.2/src/cmd/compile/internal/walk/builtin.go) 中的运行时调用，经 [`runtime.newobject`](https://github.com/golang/go/blob/go1.26.2/src/runtime/malloc.go) 或 [`runtime.makeslice`](https://github.com/golang/go/blob/go1.26.2/src/runtime/slice.go) 汇入核心入口 `mallocgc`。

在 [`malloc.go`](https://github.com/golang/go/blob/go1.26.2/src/runtime/malloc.go)、[`mcache.go`](https://github.com/golang/go/blob/go1.26.2/src/runtime/mcache.go) 与 [`mcentral.go`](https://github.com/golang/go/blob/go1.26.2/src/runtime/mcentral.go) 里，`mallocgc` 按**对象尺寸**和**内部是否包含指针**把分配请求拆成四条路径：

```text
逃逸到堆的对象 → mallocgc（检查大小、类型指针位图、是否需要清零）
  ├─ 微对象（< 16B 且无指针）：当前 P 的 mcache tiny allocator 拼装共享槽位
  ├─ 小对象（≤ 32 KiB，分 scan / noscan 两类 Size Class）：
  │     当前 P 的 mcache 对应 mspan 空闲 slot
  │       └─ 本地 span 耗尽 → mcentral.cacheSpan（必要时触发按需清扫）→ mheap
  └─ 大对象（> 32 KiB）：直接向全局 mheap 申请连续页 → 不足时经 mmap 向 Linux 扩容
```

这里有两个直接影响后续 GC 成本的设计细节：

第一是 **`scan` 与 `noscan` 在分配瞬间就彻底分家**。Go 把小对象按大小类别（Size Class）切进固定槽位尺寸的连续页段 `mspan` 中，同时为每个尺寸维护含指针（`scan`）和不含指针（`noscan`）两套 span class。标记为 `noscan` 的对象同样受 GC 管理、不可达时照常回收；但因为编译器已经证明它内部没有任何需要继续追踪的 Go 指针（比如纯字节切片 `[]byte` 的底层数组），GC 标记遇到它时只需点亮存活位，根本不用翻开对象内存逐字扫描。

第二是 **快路径免锁靠的是 `P` 的独占所有权**。当系统线程 `M` 执行普通 Go 代码时，它必然独占绑定一个 `P`，且分配器在读写本地缓存的关键窗口内会禁止抢占。既然同一时刻绝不会有两个 `M` 并发争抢同一个 `P` 的 `mcache`，绝大多数小对象分配就退化成了本核缓存里的位图查找与指针递增，无需触碰任何全局互斥锁；只有当本地 `mspan` 填满需要去 `mcentral` 换新块、或者向 `mheap` 申请页与触发 GC 协助时，才会踏入加锁慢路径。由于活跃 `P` 的数量被 `GOMAXPROCS` 严格封顶（通常等于 CPU 核数），本地缓存的总膨胀上限天然可控，不会随着 goroutine 暴涨到百万级而失控。

---

## 二、GMP 调度与阻塞交接：当线程卡进内核，P 怎样金蝉脱壳

对象分配好之后，持有它的 goroutine 开始参与并发调度。翻开 [`runtime2.go`](https://github.com/golang/go/blob/go1.26.2/src/runtime/runtime2.go)，`G`、`M`、`P` 三个核心结构体各自掌管着不同生命周期的状态：

| 核心角色 | 持有的关键状态 | 生命周期与职责边界 |
| --- | --- | --- |
| **`G`（Goroutine）** | 用户栈、保存的寄存器现场（`gobuf`）、运行状态、等待原因、`gcAssistBytes` 债务 | 轻量任务实体；挂起时依然持有栈上堆指针，高速分配时需偿还标记债务 |
| **`M`（Machine）** | 底层 OS 线程、调度栈 `g0`、当前运行的 `G`、绑定的 `P` 或暂存的 `oldp` | 真正上 CPU 跑指令的执行体；一旦调阻塞内核接口会连人带线程卡住 |
| **`P`（Processor）** | 本地 `runq`/`runnext`、`mcache`、定时器堆、`gcWork`、写屏障缓冲 `wbBuf` | 逻辑执行许可；把无锁分配、任务调度与并发 GC 的本地工作区拧成一体 |

当代码执行 `go f()` 时，[`proc.go`](https://github.com/golang/go/blob/go1.26.2/src/runtime/proc.go) 中的 `newproc/newproc1` 会优先从空闲列表复用（或新建）一个 `G`，初始化小栈与返回出口后将其置为 `_Grunnable` 推入当前 `P` 的本地队列。调度循环找到它后，`execute` 建立 `G` 与 `M` 的双向关联、把状态翻转为 `_Grunning`，最后由汇编入口 `gogo` 切到用户栈开跑。

运行途中一旦遇到“等数据”，Go Runtime 会根据**阻塞发生在用户态还是内核态**走两条截然不同的路：

第一条路是**用户态主动让出（`gopark`）**，典型代表是 Channel 收发和网络 I/O。在 [`chan.go`](https://github.com/golang/go/blob/go1.26.2/src/runtime/chan.go) 里，未就绪的 goroutine 会包装成 `sudog` 挂进 `sendq` 或 `recvq`（`select` 则允许同一个 `G` 同时挂入多个等待队列），随后经 `gopark → park_m → schedule` 把 `G` 置为 `_Gwaiting` 并与当前 `M` 解绑；Linux 上的非阻塞套接字同样如此，[`netpoll.go`](https://github.com/golang/go/blob/go1.26.2/src/runtime/netpoll.go) 与 [`netpoll_epoll.go`](https://github.com/golang/go/blob/go1.26.2/src/runtime/netpoll_epoll.go) 在读写遇到 `EAGAIN` 时把 `G` 挂到网络轮询器（Network Poller）上，等 `epoll` 事件就绪再调 `goready` 把它重新排回可运行队列。在这条路径上，**`M` 根本没有阻塞在内核里**，它手里攥着原来的 `P` 立刻进入 `findRunnable` 挑下一个活干。阅读 `findRunnable` 的分支顺序还能看清一个关键事实：调度器在查本地 `runnext`/`runq`、做全局队列公平抽样、轮询网络事件和跨 `P` 工作窃取（Work Stealing）之前，就会优先处理 STW 同步、定时器并尝试唤醒后台 **GC Worker**——也就是说，GC 标记协程本来就是通过同一个调度器从 `P` 手里分走 CPU 的。

第二条路则是棘手得多的**内核态线程阻塞（`syscall`）**。如果代码直接调用阻塞式文件读写或管道 `read`，底层 OS 线程 `M` 会实打实地睡在内核态。标准库 [`syscall.Syscall`](https://github.com/golang/go/blob/go1.26.2/src/syscall/syscall_linux.go) 为此专门在内核指令前后包了一层调度钩子（而纯内核封装 `RawSyscall` 不发通知；至于 runtime 自用的 [`cgo_mmap.go`](https://github.com/golang/go/blob/go1.26.2/src/runtime/cgo_mmap.go)、[`sys_linux_amd64.s`](https://github.com/golang/go/blob/go1.26.2/src/runtime/sys_linux_amd64.s)、`futex` 和信号处理，往往运行在无 `P`、持内部锁或禁止栈增长的 `g0` 上下文里，因此必须各自按调用约束单独封装）：

```text
runtime.entersyscall
  → RawSyscall6 → internal/runtime/syscall/linux.Syscall6 → SYSCALL 指令陷入内核
runtime.exitsyscall
```

如果在进入 `entersyscall` 的一瞬间就无条件把 `P` 剥离交给别的线程，那像 `clock_gettime` 或命中页缓存的极短系统调用每次往返都要付出昂贵的跨线程交接成本。因此 `go1.26.2` 的 `reentersyscall` 采用了**乐观滞留 + 异步夺回**的两段式协议（仅当明确预知会长时间阻塞、调用 `entersyscallblock` 时才当场交出 `P`）：

1. **进入时暂留 `P`**：`reentersyscall` 保存用户栈 `PC/SP/BP` 供 GC 安全扫栈，记录 `m.oldp`，把 `G` 从 `_Grunning` 改为 `_Gsyscall`。此时如果全局没有待处理的 STW，原线程允许暂时保留 `P` 的关联，以便微秒级短调用返回时直接在 `exitsyscall` 快路径继续跑（因此在这一版源码里，监控侧完全可能看到 `P` 仍处 `_Prunning` 而其上的 `G` 已进入 `_Gsyscall`，不能套用老版本“进 syscall 必改 `_Psyscall`”的旧印象）。
2. **超时由 `sysmon` 夺回并转交**：如果内核调用迟迟不归，后台监控线程 `sysmon` 在 `retake` 巡检时会结合监控 tick、队列待办任务与空闲资源做出判断，通过 `setBlockOnExitSyscall` 锁定状态，调用 `thread.takeP()` 正式剥离原线程的 `P`，再由 `handoffp` 把这个 `P`（连同它上面的 `mcache` 和待运行任务）转交给其他空闲或新建的 `M`。
3. **返回时重抢或排队**：当原线程终于从内核醒来进入 `exitsyscall`，如果幸运地拿回了 `oldp` 或其他空闲 `P`，就恢复 `_Grunning` 接着跑；如果系统里所有 `P` 都在忙，它只能把 `G` 放回可运行队列，自己退回休眠等待下次调度。

![阻塞 syscall 期间 P 从 reader 线程交给其他线程的示意](/images/go-runtime/syscall-p-handoff.svg)

### 本机验证一：单 P 下的阻塞管道交接

为了亲眼验证“唯一一个 `P` 不会被死锁在阻塞线程里”，我们在实验程序 [`main.go`](/code/go-runtime-lab/main.go) 中把 `GOMAXPROCS` 压到 `1`，用 `syscall.Pipe2` 创建裸阻塞管道描述符并直接调 `syscall.Read`（绕开 `os.File` 自动接入的 `netpoll` 路径）。让 `reader` 协程调 `LockOSThread` 把自己焊死在当前系统线程上阻塞读，另一个 `writer` 协程睡 100 ms 后写入字节 `42`：

```bash
GO=/home/lism/work/go/bin/go
LAB=/home/lism/work/lishuming.github.io/static/code/go-runtime-lab
"$GO" build -o /tmp/go-runtime-lab "$LAB/main.go"
GOMAXPROCS=1 GOGC=100 GOMEMLIMIT=off /tmp/go-runtime-lab \
  -mode syscall -trace /tmp/go-runtime-syscall.trace
"$GO" tool trace -d=parsed /tmp/go-runtime-syscall.trace
```

运行输出（原始记录见 [`syscall.txt`](/code/go-runtime-lab/results/syscall.txt)）清晰展示了两个不同内核线程 ID 的接力：

```text
reader tid=13860 gomaxprocs=1
writer tid=13858 after=103ms
reader resumed tid=13860 after=103ms byte=42
```

进一步用 `go tool trace -d=parsed` 解析出的[状态迁移摘录](/code/go-runtime-lab/results/syscall-transitions.txt)完整还原了幕后交接：`reader` 在线程 `13860` 上发生 `Running → Syscall` 后，唯一的 `P0` 被成功摘走并挂到线程 `13858` 上驱动 `writer` 完成写入；等到管道可读、`reader` 从内核返回时，依次经历 `Syscall → Runnable → Running`，重新拿回 `P0` 并在它锁定的原线程 `13860` 上读出了字节 `42`。

---

## 三、并发标记、混合写屏障与 Green Tea GC 的局部性重构

理解了 `P` 如何承载分配缓存与协程切换，接下来看 GC 如何在不搬移堆对象的前提下回收内存。[`mgc.go`](https://github.com/golang/go/blob/go1.26.2/src/runtime/mgc.go) 中的收集器依然采用**并发、非分代（Non-Generational）、非压缩（Non-Compacting）的标记—清扫**（Mark-Sweep）架构（注意：堆对象不会为了整理碎片而移动，但 goroutine 的栈在扩容时会发生复制搬迁）。

沿着 `gcStart → gcMarkDone → gcMarkTermination` 走完一轮 GC 周期，核心是两次极短的 STW 边界与中间的并发标记协作：

| 阶段 | 核心动作 | 解决的正确性边界 |
| --- | --- | --- |
| **Sweep Termination（STW）** | 清完上一轮残留的未扫 span，把所有 `P` 推进到安全点 | 防止上一周期残留的标记位与新一轮标记混淆 |
| **Mark Start（STW）** | 切入 `_GCmark`，开启写屏障与 Mutator Assist，排队根扫描任务 | 保证所有 `P` 在并发扫描启动前一致看到写屏障已打开 |
| **Concurrent Mark（并发）** | 后台 Worker 与业务分配协程协作扫描根集合与可达对象图 | 应用照常改写指针时，靠写屏障守住三色不变式 |
| **Mark Termination（STW）** | 汇总冲刷所有 `P` 的本地队列，切入 `_GCmarktermination` 完成统计 | 确保任何 `P` 的 `gcWork` 和 `wbBuf` 都不留漏网对象 |
| **Concurrent Sweep（`_GCoff`）** | 关闭写屏障，后台或按需清扫各 `mspan` | 将不可达槽位变回空闲可分配状态，空 span 归还 `mheap` |

这里有两处非常精巧的并发协议值得细品。

第一是**安全点（Safe Point）与完成判定**。GC 要扫某个 `G` 的栈，必须先停住它或拿到栈独占权；[`preempt.go`](https://github.com/golang/go/blob/go1.26.2/src/runtime/preempt.go) 结合了函数序言处的同步栈检查与 Linux 信号驱动的异步抢占（常规安全点依靠编译器生成的精确栈指针位图扫描，而异步信号停住的特殊指令现场则会对寄存器和局部区域做保守处理）。到了标记尾声，`gcMarkDone` 绝不能看一眼全局队列空了就草率收工——它必须跨所有 `P` 冲刷本地写屏障缓冲与 `gcWork`，甚至在进入 STW 后若发现冲刷出了新灰色对象，还会退回并发标记继续扫，本质上是一套分布式终止检测协议。

第二是**混合写屏障（Hybrid Write Barrier）如何堵住并发漏标**。在三色标记模型（白：未发现；灰：已发现待扫出边；黑：已扫完且不直指白对象）下，设想业务代码在并发标记期间干了这样两步：先把指向白色对象 `X` 的指针写进一个**已经扫完的黑色对象 `A`**（`A.f = X`），紧接着把**尚未扫描的灰色栈或对象 `B`** 里原有的 `B.f = X` 抹成 `nil`。如果 GC 毫无察觉，黑色 `A` 不会重扫，`B` 扫到时链接已断，仍在使用的 `X` 就会被误判为白色垃圾直接回收！

[`mbarrier.go`](https://github.com/golang/go/blob/go1.26.2/src/runtime/mbarrier.go) 用混合写屏障干净地封死了这条路径：每当堆上发生指针覆写时，屏障同时把**被覆盖的旧目标**和**新写入的目标**记录下来（当前实现为了省去运行期逐次判断栈扫描状态的同步分支，对堆写涉及的新旧指针统一做保守着色记录），攒进当前 `P` 的 [`mwbbuf.go`](https://github.com/golang/go/blob/go1.26.2/src/runtime/mwbbuf.go) 缓冲区再批量刷入标记队列；同时，标记期间新分配的对象一律直接涂黑（最多容忍本轮刚创建又立刻死掉的对象留到下一轮再收）。这样既保住了并发可达性，又彻底省去了早期版本在 Mark Termination 阶段重扫大量活跃栈的 STW 开销。

### Green Tea GC：从“追着指针乱跳”改成“攒满一页顺着扫”

读 `go1.26.2` 源码时最容易踩的一个认知误区，是看到文件头带有 `goexperiment.greenteagc` 构建标签、又看到 `go env GOEXPERIMENT` 输出为空，就以为跑的还是传统标记器。实际上翻开 `src/internal/buildcfg/exp.go` 就能看到，这套自 Go 1.25 引入的算法在基线配置里已写死 `GreenTeaGC: true`——**Go 1.26.2 默认运行的就是 Green Tea GC**（详见 [Go 1.26 发布说明](https://go.dev/doc/go1.26#runtime) 与 [Green Tea 官方设计文章](https://go.dev/blog/greenteagc)）。

Green Tea GC 到底在修补什么硬件痛点？回想一下传统三色标记的工作方式：每发现一个灰色指针，就把它塞进 [`mgcwork.go`](https://github.com/golang/go/blob/go1.26.2/src/runtime/mgcwork.go) 的对象队列；Worker 弹出一个对象地址，去内存里读它的字段，再弹出下一个。在经历过多次分配与回收的真实堆上，逻辑上相邻的链表节点或结构体实例往往散落在天南海北的物理页里——**每次从队列弹出一个小对象，CPU 几乎都要吃一次末级缓存（LLC）甚至 TLB 未命中**，大量时钟周期全烧在了等内存总线上。

[`mgcmark_greenteagc.go`](https://github.com/golang/go/blob/go1.26.2/src/runtime/mgcmark_greenteagc.go) 的破局思路正是顺着第一节 `mallocgc` 铺好的分级页段底座往下走：既然微对象（`< 16B noscan`）已在 `mcache.tiny` 拼槽免扫、大对象（`> 32 KiB`）直走 `mheap`，而海量的含指针小对象（`≤ 32 KiB`）早已按 68 级规格整齐码放在 `mcache → mcentral → mheap` 切出的连续页段 `mspan` 里，那么**对象图的可达语义一点不用改，只需把小对象的排队粒度从“单个对象”提升到“所属的 `mspan`”**，就能把跨页随机追指针扭转为顺着缓存行的页内批量扫描：

![Go mallocgc 分级堆分配快路径与 Green Tea GC 局部性批扫](/images/diagrams/sys-go-mallocgc-greentea.svg)

具体到代码里，`spanInlineMarkBits` 在原有分配位图 `mspan.allocBits` 之外，并列维护了 `marks`（已发现可达）和 `scans`（已完成内部指针扫描）两套位图。当 `scanSpan` 接手一个待处理的 span 时，直接通过位运算 `marks &^ scans` 找出积攒的所有待扫小对象，顺着连续虚拟地址一口气扫过去（如果恰好只有一个待扫对象，源码还留了单对象快速通道；而大对象和常规 `gcWork` 队列依然并存兜底），最后在清扫前把标记状态合并回周期账本。用极小的位图空间，换来了实打实的 CPU 缓存局部性（Cache Locality）。

---

## 四、Pacer 预算与 Mutator Assist：为什么 STW 很短还会拖慢请求

搞懂了标记怎么扫，下一个工程问题是：**GC 什么时候启动？如果业务分配太猛，后台标记追不上怎么办？** 这笔账由 [`mgcpacer.go`](https://github.com/golang/go/blob/go1.26.2/src/runtime/mgcpacer.go) 里的节奏控制器（Pacer）统管。

很多资料喜欢把 `GOGC=100` 简化成一句话：“上次存活堆 4 MiB，那涨到 8 MiB 就启动 GC。”对照 `mgcpacer.go` 的 `commit` 函数和 [Go GC Guide](https://go.dev/doc/gc-guide#GOGC) 就会发现，这句话同时漏掉了两个关键机制：

$$\text{gcPercentHeapGoal} = \text{heapMarked} + (\text{heapMarked} + \text{lastStackScan} + \text{globalsScan}) \times \frac{\text{GOGC}}{100}$$

1. **根扫描成本也在基数里**：增长比例乘上的不只是上一轮标活的堆容量 `heapMarked`，还加上了上一轮实际扫描的活跃栈 `lastStackScan` 和全局变量指针区 `globalsScan`（如果协程栈极多，目标水位也会相应抬高）。
2. **目标水位（Goal）不是启动阈值（Trigger）**：公式算出的 `Goal` 是希望**本轮并发标记结束那一刻**堆大小刚好停住的目标线；为了给并发标记留出跑道（Runway），Pacer 会综合历史扫描速率与分配速率，在远低于 `Goal` 的 `Trigger` 水位就提前吹哨启动 GC（此外还受最小堆、清扫距离以及软内存上限 `GOMEMLIMIT`——近似对应 `MemStats.Sys - HeapReleased`——的共同约束）。

即便提前吹哨，如果线上突然涌入一波分配洪峰，仅靠后台占 25% 左右 CPU 的 GC Worker 仍然可能在堆冲破 `Goal` 前扫不完。这时 **分配者协助（Mutator Assist）** 就会强制介入：[`malloc.go`](https://github.com/golang/go/blob/go1.26.2/src/runtime/malloc.go) 的 `deductAssistCredit` 按本次分配字节数扣减当前 `G` 的信用额度 `gcAssistBytes`；一旦扣成负数，业务协程当场拐进 [`mgcmark.go`](https://github.com/golang/go/blob/go1.26.2/src/runtime/mgcmark.go) 的 `gcAssistAlloc`，自己动手帮 GC 扫完定额的灰色对象才准拿内存返回！这就是为什么线上服务明明监控显示 **STW 只有零点几毫秒，业务请求的 P99 尾延迟依然会因为“边分配边替 GC 打工”而剧烈抖动**。

### 本机验证二：同等保留容量下的指针扫描账单与 GOGC 权衡

为了直观看到“占多少内存”和“让 GC 干多少活”根本不是一回事，实验程序构造了两组总容量几乎一致的负载：共循环 64 轮、每轮生成约 2 MiB 数据（累计分配约 128 MiB），用长度 8 的环形窗口保留最近约 16 MiB（配合 `runtime.KeepAlive` 锚定存活区间）：

- **`bytes` 布局**：每轮分配一整块不含指针的 `[]byte`（落入 `noscan` 路径）；
- **`nodes` 布局**：每轮分配 32768 个 64 字节链表节点，头部含一个 `next *node` 指针和 56 字节载荷（落入 `scan` 路径）。

```bash
GOMAXPROCS=2 GOGC=100 GOMEMLIMIT=off GODEBUG=gctrace=1 \
  /tmp/go-runtime-lab -mode gc -kind bytes
GOMAXPROCS=2 GOGC=100 GOMEMLIMIT=off GODEBUG=gctrace=1 \
  /tmp/go-runtime-lab -mode gc -kind nodes
```

读取 [`runtime/metrics/description.go`](https://github.com/golang/go/blob/go1.26.2/src/runtime/metrics/description.go) 定义的运行期指标，两组单次实验在保留窗口稳定后的快照对比非常鲜明：

| 数据布局 | 保留 `HeapAlloc` / MiB | 可扫描堆（`/gc/scan/heap:bytes`）/ MiB | 自动 GC 轮数 | 累计 Mark Assist CPU / ms |
| --- | ---: | ---: | ---: | ---: |
| `bytes`（无指针切片） | 16.090 | 0.061 | 9 | 0.308 |
| `nodes`（64B 指针链表） | 16.090 | 2.061 | 8 | 16.833 |

两边保留的 `HeapAlloc` 同为 `16.090 MiB`，但因为 64 字节节点只有起始指针域需要追踪，`nodes` 的可扫描堆为 `2.061 MiB`（`bytes` 仅剩外层切片头与运行时自身的 `0.061 MiB`）；反映到业务协程被迫垫付的 Mark Assist CPU 时间上，`nodes` 直接拉高到了 `16.833 ms`（而 `bytes` 因单次大块分配推进极快，自动周期反而触发了 9 轮，Assist 却只有 `0.308 ms`）。

保持 `nodes` 指针链表负载不变，再把 `GOGC` 依次调为 `50`、`100`、`200`，就能看清内存水位预算对 GC 频率与 Assist 抢占的杠杆效应：

| `GOGC` | 自动 GC 轮数 | 稳定态 `NextGC` / MiB | 累计 Mark Assist CPU / ms |
| --- | ---: | ---: | ---: |
| 50 | 17 | 24.213 | 78.181 |
| 100 | 8 | 32.335 | 16.833 |
| 200 | 4 | 48.580 | 5.106 |

当 `GOGC` 压低到 `50`，允许堆增长的跑道缩窄，GC 轮数翻倍到 17 轮，业务协程帮扫指针消耗的 Assist CPU 飙升到 `78.181 ms`；放宽到 `200` 后，用更高的 `NextGC` 目标水位换取了仅 4 轮回收与 `5.106 ms` 的协助开销。从 [`nodes-gogc100.gctrace`](/code/go-runtime-lab/results/nodes-gogc100.gctrace) 摘出一轮真实日志，正好可以把前面讲的阶段耗时一一对号入座：

```text
gc 4 @0.027s 16%: 0.080+14+0.12 ms clock, 0.16+1.7/8.2/9.7+0.24 ms cpu,
15->19->19 MB, 18 MB goal, 0 MB stacks, 0 MB globals, 2 P
```

墙钟时间 `0.080+14+0.12 ms` 分别对应 Sweep Termination STW（0.080 ms）、**与业务并发运行的标记阶段**（14 ms，绝非暂停 14 ms！）以及 Mark Termination STW（0.12 ms）；中间的 CPU 拆解 `1.7/8.2/9.7 ms` 则把业务协程垫付的 Assist CPU、专用后台 Worker CPU 和空闲 Idle 标记 CPU 账目列得清清楚楚，`15->19->19 MB` 记录了启动水位、标记结束堆大小与存活量（`18 MB goal` 是软性目标而非硬截断线）。

---

## 五、清扫、页归还与栈根保留：对象不可达后内存去哪了

很多工程师在排查线上容器内存时都会纳闷：“我已经把大缓存 map 清空了，GC 也跑过了，为什么监控看到的进程内存迟迟不往下掉？”这是因为从“Go 对象失去引用”到“Linux 收回物理内存页”，中间隔着两道完全异步的闸门。

第一道闸门是**代数驱动的并发清扫（[`mgcsweep.go`](https://github.com/golang/go/blob/go1.26.2/src/runtime/mgcsweep.go)）**。标记结束只产出了存活位图，并未逐个擦除死对象。Runtime 通过全局递增的清扫代数 `sweepgen` 来无锁协调所有权（详见 [`mheap.go`](https://github.com/golang/go/blob/go1.26.2/src/runtime/mheap.go) 注释：`h.sweepgen - 2` 代表待清扫，`- 1` 代表正在清扫，等于 `h.sweepgen` 代表已清扫完毕，进入缓存的 span 还会叠加 `+1`/`+3` 状态编码），由后台清扫协程或分配慢路径上的 `object reclaimer`（找空闲槽位）与 `span reclaimer`（找全空页段）推进。需要注意非压缩回收的一个天然物理边界：**只要一个 `mspan` 里还钉着哪怕 1 个长期存活的小对象，整块 span 就不能作为全空页段退回 `mheap`**——里面的空槽位只能留给后续同尺寸类别的小对象复用。

第二道闸门是**物理页归还（[`mgcscavenge.go`](https://github.com/golang/go/blob/go1.26.2/src/runtime/mgcscavenge.go)）**。即使整块 span 空出来退回了 `mheap`，它首先也只是变成 Runtime 手里随时待命的空闲页（`HeapIdle`）。后台 Scavenger 会按自己的步调挑选长期闲置的页，在 Linux 上通过 [`mem_linux.go`](https://github.com/golang/go/blob/go1.26.2/src/runtime/mem_linux.go) 的 `sysUnusedOS` 发起 `madvise` 系统调用，通知内核解除物理页帧映射，同时保留虚拟地址区间以便日后免 `mmap` 直接复用。

接着看刚才 `nodes`（`GOGC=100`）实验后半程的三次连续快照，两道闸门的分工一目了然（注：`HeapReleased` 本身是 `HeapIdle` 中已通知 OS 释放的子集，两者不可相加）：

| 生命周期阶段 | `HeapAlloc` / MiB | `HeapIdle` / MiB | `HeapReleased` / MiB |
| --- | ---: | ---: | ---: |
| 保留窗口稳定态（`retained`） | 16.090 | 34.828 | 3.945 |
| 清空引用并强制 GC 后 | 0.091 | 51.352 | 7.773 |
| 调用 `debug.FreeOSMemory` 主动归还后 | 0.091 | 51.352 | 51.156 |

清空引用跑完 GC 后，活跃对象空间瞬间从 `16.090 MiB` 跌到 `0.091 MiB`，腾出的页悉数转入 `HeapIdle`（`51.352 MiB`），但此时交还给操作系统的 `HeapReleased` 只有 `7.773 MiB`；直到显式调用 `debug.FreeOSMemory` 强制跑完页回收（仅用于实验放大观察，生产环境交由后台 Scavenger 按节奏推进即可），`HeapReleased` 才跃升至 `51.156 MiB`。

### 本机验证三：挂起 goroutine 如何锁住堆内存

最后，把调度器与 GC 合龙，回答文章最开头那个问题：**如果一个 goroutine 阻塞挂起了，它对内存和 GC 意味着什么？**

被 `gopark` 挂起的 `G` 虽然不占 `M` 也不耗 `P`，但它的用户栈依然是 GC 根集合（Root Set）堂堂正正的一员。第三组实验启动 2000 个 goroutine，每个分配一块 16 KiB 的 `[]byte` 后阻塞在同一个 Channel 接收上，并在接收语句之后放置 `KeepAlive(payload)` 模拟“醒来后还要继续用这块缓冲区”的真实业务引用：

```bash
GOMAXPROCS=2 GOGC=100 GOMEMLIMIT=off \
  /tmp/go-runtime-lab -mode roots -goroutines 2000 -payload 16384
```

采样快照（原始数据见 [`roots.jsonl`](/code/go-runtime-lab/results/roots.jsonl)）如下：

| 阶段 | 存活 Goroutine 数 | `HeapAlloc` / MiB | 上轮实际扫栈（`/gc/scan/stack:bytes`）/ MiB |
| --- | ---: | ---: | ---: |
| 初始基线（`baseline`） | 1 | 0.080 | 0.002 |
| 2000 个协程阻塞等待中 | 2001 | 32.487 | 0.734 |
| 关闭 Channel 全部退出并 GC | 1 | 1.046 | 0.002 |

2000 份 16 KiB 载荷本身占 `31.25 MiB`，叠加上 2000 个 `G` 结构体与 `sudog` 等待节点，`HeapAlloc` 停在 `32.487 MiB` 一分都收不走，同时每轮 GC 被迫多扫 `0.734 MiB` 的活跃栈空间（这笔根扫描开销还会进一步写进前面讲的 Pacer 公式里）；直到 Channel 关闭、所有等待任务正常退出，GC 才能把这批堆内存彻底释放（余下约 `1 MiB` 为运行时保留复用的内部结构）。这也点破了线上 **Goroutine 泄漏（Goroutine Leak）** 真正的杀伤力：泄漏的不只是几 KB 的协程栈，而是那几千个死等栈帧顺藤摸瓜拽住的整片业务堆对象图。

---

## 六、工程排障顺序与源码走读索引

把分配、调度和回收串成一张网之后，再遇到线上 Go 服务的内存或延迟异常，排查思路就有了清晰的先后次序：

1. **先分清涨的是哪一层账本**：看存活 Goroutine 数（是不是退出信号缺失导致栈根死拽着堆）、看 `HeapAlloc` 与 `/gc/scan/heap:bytes`（是纯字节缓冲变大了，还是含指针复杂结构膨胀了），再看 `HeapIdle - HeapReleased`（只是空闲页暂未经过 Scavenger 归还 OS，还是真发生了对象泄漏或跨长短命对象的 span 碎片）。
2. **排查延迟抖动时把调度与 Assist 对齐到同一时间线**：别只盯着毫秒级的 STW，同时采集 Mutator Assist 耗时与 `Runnable` 排队延迟；盲目调大 `GOGC` 或手工刷 GC 往往只是在内存上限与 CPU 协助抢占之间拆东墙补西墙，减少热路径上的逃逸分配和不必要的指针嵌套才是治本之策。
3. **修改或验证 Runtime 核心协议时守住不变量**：如果要动手改 Runtime 源码，必须紧盯对应的状态机契约——动 syscall 交接先核对 `_Gsyscall`、`m.oldp` 与 `retake` 退出握手；动 Green Tea 标记先核对 `marks/scans` 位图与 span 并发所有权；动清扫先核对 `sweepgen` 奇偶代数转换，并用匹配的源码树工具链跑通针对性单测与并发压力测试：

```bash
cd /home/lism/work/go/src
../bin/go test runtime -run 'Test.*(GC|Syscall|Chan)' -count=1
../bin/go test runtime/metrics -count=1
```

如果你想顺着本文的脉络亲自翻一遍 `go1.26.2` 源码，下面这张索引表就是最短路径；文中所用的完整复现材料（[实验主程序 `main.go`](/code/go-runtime-lab/main.go)、[构建说明与哈希记录](/code/go-runtime-lab/README.md)、[`bytes` 采样](/code/go-runtime-lab/results/bytes-gogc100.jsonl)、[`nodes` GOGC=50](/code/go-runtime-lab/results/nodes-gogc50.jsonl) / [100](/code/go-runtime-lab/results/nodes-gogc100.jsonl) / [200](/code/go-runtime-lab/results/nodes-gogc200.jsonl)、[栈根保留采样](/code/go-runtime-lab/results/roots.jsonl) 与 [syscall 轨迹](/code/go-runtime-lab/results/syscall.txt)）也已随文归档：

| 核心问题 | `go1.26.2` 源码入口 |
| --- | --- |
| 小对象分级分配与 Assist 扣账 | [`src/runtime/malloc.go`](https://github.com/golang/go/blob/go1.26.2/src/runtime/malloc.go)：`mallocgc`、`deductAssistCredit` |
| Per-P 本地缓存与跨层补货 | [`src/runtime/mcache.go`](https://github.com/golang/go/blob/go1.26.2/src/runtime/mcache.go)：`refill`；[`src/runtime/mcentral.go`](https://github.com/golang/go/blob/go1.26.2/src/runtime/mcentral.go)：`cacheSpan` |
| GMP 调度与阻塞 `syscall` 夺回 | [`src/runtime/proc.go`](https://github.com/golang/go/blob/go1.26.2/src/runtime/proc.go)：`findRunnable`、`reentersyscall`、`retake`、`exitsyscall` |
| GC 周期推进与分布式完成判定 | [`src/runtime/mgc.go`](https://github.com/golang/go/blob/go1.26.2/src/runtime/mgc.go)：`gcStart`、`gcMarkDone`、`gcMarkTermination` |
| 栈根扫描与分配者协助打工 | [`src/runtime/mgcmark.go`](https://github.com/golang/go/blob/go1.26.2/src/runtime/mgcmark.go)：`markroot`、`scanstack`、`gcAssistAlloc` |
| Green Tea Span 级局部性扫描 | [`src/runtime/mgcmark_greenteagc.go`](https://github.com/golang/go/blob/go1.26.2/src/runtime/mgcmark_greenteagc.go)：`spanInlineMarkBits`、`scanSpan` |
| Pacer 目标水位、跑道与软内存上限 | [`src/runtime/mgcpacer.go`](https://github.com/golang/go/blob/go1.26.2/src/runtime/mgcpacer.go)：`commit`、`trigger`、`memoryLimitHeapGoal` |
| 代数并发清扫与物理页 `madvise` | [`src/runtime/mgcsweep.go`](https://github.com/golang/go/blob/go1.26.2/src/runtime/mgcsweep.go)、[`src/runtime/mgcscavenge.go`](https://github.com/golang/go/blob/go1.26.2/src/runtime/mgcscavenge.go) |

---

> **源码与实验基线说明**：本文源码链接固定在上游标签 `go1.26.2`（`VERSION` 构建时间戳 `2026-03-27T21:58:29Z`，基线默认开启 `GreenTeaGC: true`）。本机验证运行于 WSL2（Linux `6.6.87.2-microsoft-standard-WSL2` `linux/amd64`，`GOROOT=/home/lism/work/go`，`GOEXPERIMENT` 为空），其中 GC 与栈根实验采用 `GOMAXPROCS=2`、`GOMEMLIMIT=off`，阻塞管道实验采用 `GOMAXPROCS=1` 配合 `LockOSThread`；本地源码树中 `proc.go`、`mgc.go`、`mgcpacer.go`、`mgcmark_greenteagc.go` 的 SHA-256 校验值均与上游 `go1.26.2` 完全一致，完整指纹与单次运行原始数据见 [`/code/go-runtime-lab/README.md`](/code/go-runtime-lab/README.md)。
