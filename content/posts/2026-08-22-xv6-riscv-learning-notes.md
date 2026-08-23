---
title: "【源码】RISC-V xv6 : 启动、调度、中断与分页源码学习笔记"
date: 2026-08-22T00:00:00+08:00
categories:
  - 操作系统
tags:
  - xv6
  - RISC-V
  - WSL
  - QEMU
  - GDB
  - 操作系统
description: "在 x86_64 WSL2 上构建和运行 xv6-riscv，结合本地开发环境改造、失败诊断与源码阅读，梳理启动、调度、中断和 Sv39 分页机制。"
draft: false
toc: true
math: false
---

最近我重新从源码学习操作系统，选择的入口是 MIT 的 `xv6-riscv`。这次没有先从概念章节开始，而是先在自己的 WSL 环境中把它编译、运行起来，并整理出可复现的调试路径，然后围绕运行中真正遇到的问题阅读源码：x86 CPU 为什么能运行 RISC-V 内核？QEMU 把内核放到了哪里？`entry.S`、`start.c` 和 `main.c` 分别完成什么？一个 `proc` 到底怎样获得 CPU？timer interrupt 如何迫使当前进程让出 CPU？虚拟地址最终又如何找到物理页？

这条路径让我逐渐形成了一个比“背模块功能”更稳定的认识：**操作系统内核首先是一组由硬件事件驱动的控制流，其次才是一组数据结构。** 进程、调度器、页表和中断处理程序并不是彼此孤立的模块，它们通过寄存器、栈、状态机和硬件约定连接在一起。

本文基于我本机的 `xv6-riscv` 仓库，当前分支为 `riscv`，源码基线提交为 `5474d4b`。这个版本已经包含 lazy `sbrk`/page fault 相关实现，部分细节可能与旧版 xv6 教材代码不同。工作树中还有我为阅读源码增加的环境检查、编辑器配置和文档，以及一处尚未整理完成的 Makefile 修改；后文会把“已经验证的实验”和“准备继续做的实验”明确区分。

---

## 1. 本机学习环境

我的宿主环境不是 RISC-V 机器，而是一台 x86_64 PC：

| 项目 | 本机环境 |
| --- | --- |
| 操作系统 | WSL2，Linux `6.6.87.2-microsoft-standard-WSL2` |
| 宿主架构 | x86_64 |
| CPU | AMD Ryzen AI 9 HX 370，12 核 24 线程 |
| RISC-V 编译器 | `riscv64-linux-gnu-gcc 13.3.0` |
| QEMU | `qemu-system-riscv64 8.2.2` |
| GDB | `gdb-multiarch 15.1` |

这里最先要澄清的是：`risc64` 通常是对 `RISC-V 64` 或 `riscv64` 的口头简写，并不是另一套架构。我的 CPU 只能原生执行 x86_64 指令，不能直接执行 xv6 内核中的 RV64 指令。整个运行过程实际分成两步：

```text
xv6 C/Assembly source
        │
        │ riscv64-linux-gnu-gcc
        ▼
RISC-V 64 ELF kernel
        │
        │ qemu-system-riscv64 translates/emulates instructions and devices
        ▼
x86_64 host CPU executes QEMU
```

交叉编译器解决“生成哪种机器码”，QEMU system emulator 解决“由谁执行这种机器码”。这也是 x86_64 主机能够学习 xv6-riscv 的根本原因。它不是把 RISC-V 程序当成 x86 程序运行，而是在 x86 上模拟出一台完整的 RISC-V 计算机。

我在仓库中增加了一个环境检查脚本：

```bash
./scripts/check-env.sh
```

本机检查结果是 `make`、`perl`、`bc`、RISC-V GCC、QEMU 和 GDB 均可用。WSL Ubuntu 上所需依赖可以这样安装：

```bash
sudo apt update
sudo apt install build-essential make perl bc qemu-system-misc gdb-multiarch \
  gcc-riscv64-linux-gnu binutils-riscv64-linux-gnu
```

### 编译、运行和退出

构建并启动：

```bash
make qemu
```

本次实际启动输出为：

```text
xv6 kernel is booting

hart 1 starting
hart 2 starting
init: starting sh
$
```

进入 shell 后实际执行：

```text
$ echo xv6-learning
xv6-learning
$ ls
README         2 2 2425
cat            2 3 35384
echo           2 4 34288
...
console        3 22 0
```

Makefile 默认传递 `-smp 3`，所以 QEMU 创建了 3 个 RISC-V hart。hart 是 RISC-V 对 hardware thread 的称呼，在 xv6 这套配置里可以近似理解为 3 个 CPU 执行上下文。

因为 QEMU 使用 `-nographic`，终端同时承担串口输入和 QEMU monitor escape。退出不是在 xv6 shell 中输入 `exit`，而是依次按：

```text
Ctrl-a
x
```

这里是先按 `Ctrl-a`，松开后再按 `x`，最终会看到 `QEMU: Terminated`。

我还遇到过一次很有代表性的启动失败：QEMU 报 `Could not open 'fs.img': Read-only file system`。这不是内核编译问题，而是 virtio block device 默认以可写方式打开 `fs.img`，当仓库或镜像位于只读挂载中时，内核甚至还没有开始执行。它提醒我在判断“系统为什么没启动”时，先分清失败发生在 host 工具、虚拟硬件、boot，还是 xv6 内核内部。

### GDB 调试路径，以及一次本地 Makefile 回归

上游 `5474d4b` 的 Makefile 原本保留了 DWARF 调试信息，并提供 `qemu-gdb`、`.gdbinit` 和 `print-gdbport`。正常路径是终端 1 执行：

```bash
make qemu-gdb
```

该目标在正常 QEMU 参数后增加了两个关键选项：

- `-S`：虚拟 CPU 启动后立即暂停；
- `-gdb tcp::26000`：在本机 26000 端口等待 GDB remote connection。

终端 2：

```bash
gdb-multiarch -x .gdbinit
```

生成的 `.gdbinit` 会加载 `kernel/kernel` 的符号，并连接本机动态计算的 GDB 端口。适合第一次源码跟踪的断点是：

```gdb
set pagination off
b _entry
b start
b main
b scheduler
b usertrap
b clockintr
c
```

几个常用观察命令：

```gdb
bt
info registers
info registers sp ra sepc scause stval satp
x/16gx $sp
disassemble /m swtch
p ticks
```

端口不应硬编码到通用文档中，应使用下面的命令确认：

```bash
make print-gdbport
```

按当前用户 ID 计算，本机通常会得到 `26000`。

不过，在写这篇笔记时，我重新执行 `make print-gdbport`，实际得到的是：

```text
make: *** No rule to make target 'print-gdbport'.  Stop.
```

对比 `git diff` 后发现，本地尚未提交的 Makefile 修改不仅删除了 `qemu-gdb`、`.gdbinit` 和 `print-gdbport`，还从 `CFLAGS` 中移除了 `-ggdb -gdwarf-2`。也就是说，上面的 GDB 流程描述的是上游基线能力，不是当前脏工作树已经验证通过的能力。

这个问题很有价值：开发辅助配置也属于代码的一部分，文档、VS Code task 和 Makefile 必须保持一致。当前 `.vscode` 里甚至只有 build/run/clean task，没有文档中声称存在的 `xv6: run qemu-gdb`。后续整理这组修改时，我需要恢复或重新实现 GDB 目标，再用真实断点记录完成下面的调试实验，而不是把“计划执行”写成“已经执行”。

---

## 2. 内核如何启动：从 QEMU 到 scheduler

运行 `make qemu` 时，真正执行的命令核心部分如下：

```text
qemu-system-riscv64 \
  -machine virt \
  -bios none \
  -kernel kernel/kernel \
  -m 128M \
  -smp 3 \
  -nographic \
  -drive file=fs.img,... \
  -device virtio-blk-device,...
```

这些参数建立了学习源码时需要记住的硬件边界：

- `virt` 是 QEMU 提供的 RISC-V 虚拟板卡；
- `-bios none` 表示没有 OpenSBI 等固件替内核完成早期初始化；
- `-kernel kernel/kernel` 把 ELF 内核装入内存；
- `-m 128M` 对应 xv6 使用的 128 MiB RAM；
- `-smp 3` 创建 3 个 hart；
- `fs.img` 通过 virtio block device 提供根文件系统。

整体启动控制流可以压缩成：

```text
QEMU reset
  └─> _entry                  kernel/entry.S, M-mode
       └─> start()            kernel/start.c, M-mode
            └─> mret
                 └─> main()   kernel/main.c, S-mode
                      ├─> global/per-hart initialization
                      └─> scheduler()
                           └─> forkret()
                                └─> /init
                                     └─> sh
```

### `entry.S`：先让 C 代码拥有可靠的栈

链接脚本 `kernel/kernel.ld` 把 `_entry` 放在 `0x80000000`，QEMU 加载内核后让每个 hart 从这里开始执行。`kernel/entry.S` 的核心只有几行：

```asm
la   sp, stack0
li   a0, 1024*4
csrr a1, mhartid
addi a1, a1, 1
mul  a0, a0, a1
add  sp, sp, a0
call start
```

此时分页尚未开启，使用的地址就是物理地址。`mhartid` 是硬件提供的 hart 编号，xv6 据此为每个 hart 选择一段独立的 4096 字节启动栈：

```text
hart 0: stack0 + 1 * 4096
hart 1: stack0 + 2 * 4096
hart 2: stack0 + 3 * 4096
```

栈向低地址增长，因此 `sp` 初始化为各段栈的高地址。直到 `sp` 可用之后，CPU 才能安全地执行遵循 C ABI 的 `start()`。`call start` 同时把返回地址写入 `ra`，这也是后面理解上下文切换的第一个线索。

### `start.c`：从 Machine mode 降到 Supervisor mode

QEMU 让内核从 Machine mode（M-mode）开始执行，但 xv6 的主体运行在 Supervisor mode（S-mode）。`start()` 的任务是设置 RISC-V 控制状态寄存器，然后通过 `mret` 完成特权级切换。

关键步骤如下：

1. 修改 `mstatus.MPP`，指定 `mret` 后进入 S-mode；
2. 把 `mepc` 设置为 `main`，指定 `mret` 后的 PC；
3. 将 `satp` 清零，早期启动阶段先关闭分页；
4. 通过 `medeleg`、`mideleg` 把异常和中断委托给 S-mode；
5. 配置 PMP，使 S-mode 能访问 xv6 使用的物理内存；
6. 初始化每个 hart 的 timer interrupt；
7. 把 `mhartid` 保存到 `tp`，供 `cpuid()` 使用；
8. 执行 `mret`，硬件根据 `mstatus` 和 `mepc` 进入 `main()`。

因此，`mret` 并不是普通函数返回。它让硬件原子地完成“恢复目标 PC + 切换特权级 + 更新中断状态”。可以把 `start()` 理解为 xv6 与 RISC-V 特权架构之间最集中的握手过程。

### `main.c`：全局初始化与每 hart 初始化

所有 hart 都会进入 `main()`，但共享对象只能由 hart 0 初始化一次：

```c
if(cpuid() == 0){
  consoleinit();
  printfinit();
  kinit();
  kvminit();
  kvminithart();
  procinit();
  trapinit();
  trapinithart();
  plicinit();
  plicinithart();
  binit();
  iinit();
  fileinit();
  virtio_disk_init();
  userinit();
  __sync_synchronize();
  started = 1;
}
```

其他 hart 等待 `started`，看到 hart 0 发布的初始化结果后，只执行页表、trap vector、PLIC 等 per-hart 初始化。`__sync_synchronize()` 是内存屏障，防止另一个 hart 看见 `started == 1` 时，前面的共享状态仍未对它可见。

最后，每个 hart 都调用自己的 `scheduler()`，并且不再从这个函数返回。

这里也解释了一个常见误会：系统中没有一个独立的“调度 CPU”。每个 CPU 都有自己的 scheduler context；一个 CPU 当前要么运行 scheduler，要么运行某个进程的用户/内核控制流。

---

## 3. `proc` 究竟如何在 CPU 上执行

我最初最难理解的问题是：scheduler 在进程表中找到一个 `RUNNABLE` 的 `proc` 后，这个 C 结构体怎样突然变成“正在 CPU 上运行的程序”？

关键是先去掉一种拟人化想象：**CPU 不认识 `struct proc`，也不知道进程是什么。CPU 只会按照 PC 取指，并使用寄存器和内存继续执行。** `proc` 是内核为了保存和组织这些执行状态而建立的软件抽象。

### scheduler 保存的不是程序，而是暂停点

`scheduler()` 扫描全局 `proc[]`：

```c
if(p->state == RUNNABLE) {
  p->state = RUNNING;
  c->proc = p;
  swtch(&c->context, &p->context);
  c->proc = 0;
}
```

`swtch.S` 保存旧 context，加载新 context：

```asm
sd ra, 0(a0)
sd sp, 8(a0)
sd s0, 16(a0)
...
sd s11, 104(a0)

ld ra, 0(a1)
ld sp, 8(a1)
ld s0, 16(a1)
...
ld s11, 104(a1)
ret
```

通常会把它概括成“调度的本质是切换 `sp` 和 `ra`”，这句话抓住了核心，但还需要两个补充：

1. `s0-s11` 也必须恢复，因为根据 RISC-V ABI，它们是 callee-saved registers；
2. 用户态的全部通用寄存器不保存在 `context`，而保存在 `trapframe`。

为什么恢复 `sp` 和 `ra` 后，CPU 就“自然进入”另一个进程？根本原因来自编译器、ABI 和指令语义共同建立的契约：

- `sp` 指向当前执行流的栈，栈中保存函数调用帧、局部变量和调用链；
- `ra` 保存函数返回后继续执行的地址；
- RISC-V 的 `ret` 本质上跳转到 `ra` 指向的地址；
- `s0-s11` 按 ABI 在函数调用前后应保持不变。

当 `swtch` 加载进程的 `sp` 时，CPU 后续的栈访问已经落到该进程自己的内核栈；加载它的 `ra` 后，最后一条 `ret` 又把 PC 设置为该执行流上次暂停的位置。CPU 没有执行“启动进程”这种特殊指令，它只是恢复了一组足够描述 C 函数执行现场的寄存器。

这与恢复一台虚拟机 snapshot 有相似之处：对象名称不是关键，关键是恢复后下一条指令、寄存器和内存是否彼此一致。

### 第一次运行为什么也能 `ret`

已经运行过的进程有真实的暂停位置，新进程却没有。xv6 在 `allocproc()` 中人为构造第一次 context：

```c
memset(&p->context, 0, sizeof(p->context));
p->context.ra = (uint64)forkret;
p->context.sp = p->kstack + PGSIZE;
```

所以 scheduler 第一次切换到该进程时，`swtch` 的 `ret` 会进入 `forkret()`。第一次 `forkret()` 初始化文件系统并 `kexec("/init", ...)`，随后通过 `prepare_return()` 和 trampoline 的 `userret` 恢复用户寄存器，最终由 `sret` 进入用户态 `/init`。

这里有两类不同的“返回”：

| 指令 | 恢复什么 | 用途 |
| --- | --- | --- |
| `ret` | 普通函数返回地址 `ra` | 在 scheduler context 与进程 kernel context 之间切换 |
| `sret` | `sepc`、`sstatus` 所描述的 trap 前状态 | 从 S-mode 返回 U-mode 或恢复被中断的 S-mode 代码 |

### scheduler 不是在后台同时运行

当一个 CPU 正在执行用户进程时，它在该 CPU 上的 scheduler 已经暂停在：

```c
swtch(&c->context, &p->context);
```

scheduler 的 `sp`、`ra`、`s0-s11` 保存在 `c->context` 中，当前硬件寄存器属于进程的执行流。直到该进程执行 `sleep()`、`yield()`、`exit()`，或者被 timer interrupt 抢占并进入 `yield()`，才会调用：

```c
swtch(&p->context, &mycpu()->context);
```

此时 scheduler 从原来那次 `swtch()` 调用中“返回”，继续执行 `c->proc = 0` 和下一轮扫描。它看起来像两条互相调用的执行流，实际上同一时刻一个 CPU 只有一套真实寄存器，只能运行其中一条。

---

## 4. Timer interrupt 如何产生抢占

如果进程从不主动调用 `yield()`，内核仍然需要收回 CPU。xv6 使用 RISC-V supervisor timer interrupt 实现时间片抢占。

### 第一次定时器由 `start.c` 设置

每个 hart 在 `timerinit()` 中：

```c
w_mie(r_mie() | MIE_STIE);
w_menvcfg(r_menvcfg() | (1L << 63));
w_mcounteren(r_mcounteren() | 2);
w_stimecmp(r_time() + 1000000);
```

硬件维护单调递增的 `time` 计数器。当 `time >= stimecmp` 且相关中断使能位允许时，硬件置起 supervisor timer interrupt。

这段代码中的 `r_time()`、`w_stimecmp()` 最终都是 CSR 访问指令。它们不是访问普通 C 变量，而是在直接配置 CPU 的计时和中断机制。

### 用户态发生 timer interrupt 后的完整路径

```text
user instruction
  │
  │ time >= stimecmp
  ▼
RISC-V hardware trap entry
  ├─ sepc   <- interrupted user PC
  ├─ scause <- supervisor timer interrupt
  ├─ sstatus records previous privilege/interrupt state
  ├─ privilege U -> S
  └─ pc <- stvec
       │
       ▼
trampoline.S:uservec
  ├─ save user registers to p->trapframe
  ├─ sp <- process kernel stack
  ├─ satp <- kernel page table
  └─ call usertrap()
       │
       ▼
devintr() -> clockintr() -> return 2
       │
       ▼
yield()
  ├─ p->state = RUNNABLE
  └─ sched() -> swtch(&p->context, &cpu->context)
       │
       ▼
scheduler resumes and chooses a RUNNABLE process
```

硬件只负责进入 trap：保存少量控制状态、切特权级、跳到 `stvec`。保存全部用户通用寄存器、切换内核栈和页表，是 `trampoline.S:uservec` 的软件职责。

`devintr()` 通过 `scause` 区分中断来源：

```c
if(scause == 0x8000000000000009L) {
  // supervisor external interrupt: UART/virtio via PLIC
} else if(scause == 0x8000000000000005L) {
  clockintr();
  return 2;
}
```

`clockintr()` 只让 hart 0 更新全局 `ticks` 并唤醒等待 `ticks` 的进程，但每个 hart 都会重新设置自己的下一次 timer：

```c
if(cpuid() == 0){
  acquire(&tickslock);
  ticks++;
  wakeup(&ticks);
  release(&tickslock);
}
w_stimecmp(r_time() + 1000000);
```

当前注释把 `1000000` 描述为约 0.1 秒。准确的墙钟时间取决于 QEMU `virt` 平台的 timebase，但调度机制并不依赖我们在源码中把它换算成某个固定毫秒数。

`usertrap()` 看到 `devintr()` 返回 2 后调用 `yield()`。如果 timer interrupt 发生在内核态，则入口是 `kernelvec.S -> kerneltrap()`；只要当前 CPU 关联了进程，`kerneltrap()` 同样调用 `yield()`。因此不是 scheduler 自己周期性 `yield`，而是正在运行的进程控制流在中断处理过程中被改成 `RUNNABLE`，再切回已暂停的 scheduler context。

### 哪些代码在直接操作 CPU 和设备

阅读 xv6 时，我把“普通内核逻辑”和“硬件边界”分开标记，源码会清晰很多：

| 交互方式 | 典型对象/指令 | 作用 |
| --- | --- | --- |
| RISC-V CSR | `mhartid`、`mstatus`、`mepc`、`satp` | hart 标识、特权级、返回 PC、页表根 |
| Trap CSR | `stvec`、`sepc`、`scause`、`stval`、`sstatus` | trap 入口、现场、原因和故障地址 |
| Timer CSR | `time`、`stimecmp`、`mie`、`menvcfg` | 产生和控制 timer interrupt |
| 特权指令 | `mret`、`sret`、`wfi`、`sfence.vma` | 特权返回、等待中断、刷新 TLB |
| MMIO | UART、virtio、PLIC 地址区间 | 控制串口、磁盘和外部中断控制器 |

`kernel/riscv.h` 把 CSR 指令包装成 `r_*`/`w_*` 内联函数；`uart.c`、`virtio_disk.c`、`plic.c` 则通过固定物理地址读写模拟设备寄存器。这些位置就是 xv6 与 QEMU 虚拟硬件真正接触的地方。

---

## 5. 内存管理：物理页、虚拟地址与页表

内存管理容易混淆，是因为“分配物理内存”和“建立虚拟地址映射”是两件不同的事。xv6 用两个相对独立的层次把它们连接起来：

```text
kalloc.c                       vm.c / hardware MMU
physical page allocator       virtual address translation

kalloc() -> physical page     walk()/mappages() -> PTE
             │                              │
             └──────── page table maps ─────┘
```

### 物理页分配器：一个以空闲页自身为节点的链表

QEMU 把 RAM 放在 `0x80000000` 开始的位置。Makefile 使用 `-m 128M`，源码中 `PHYSTOP` 也定义为 `KERNBASE + 128 MiB`。内核镜像末尾由链接符号 `end` 标记，所以可分配物理内存是：

```text
[PGROUNDUP(end), PHYSTOP)
```

`kinit()` 把这个区间按 4096 字节切成物理页，并逐页放入 freelist：

```c
struct run {
  struct run *next;
};

struct {
  struct spinlock lock;
  struct run *freelist;
} kmem;
```

空闲页不需要保存用户数据，因此 xv6 直接借用页首几个字节存放 `next` 指针。`kalloc()` 从链表头弹出一页，`kfree()` 把一页压回链表头，spinlock 负责多 hart 并发安全。

这套分配器不处理任意字节大小，也不做 slab、buddy 或 NUMA 优化。它只回答一个问题：给我一个空闲的 4 KiB 物理页。用户内存、内核栈、页表页和 pipe buffer 都建立在这个原语之上。

### Sv39：三层索引找到一个 PTE

RV64 的 xv6 使用 Sv39 分页。一个有效虚拟地址拆分为：

```text
 38        30 29        21 20        12 11         0
+------------+------------+------------+-------------+
| VPN[2] 9b  | VPN[1] 9b  | VPN[0] 9b  | offset 12b |
+------------+------------+------------+-------------+
```

每个页表页为 4096 字节，一个 PTE 为 8 字节，所以每层正好有 512 个 PTE，对应 9 位索引。`walk()` 从 level 2 走到 level 0：

```c
for(int level = 2; level > 0; level--) {
  pte_t *pte = &pagetable[PX(level, va)];
  if(*pte & PTE_V) {
    pagetable = (pagetable_t)PTE2PA(*pte);
  } else {
    pagetable = (pagetable_t)kalloc();
    memset(pagetable, 0, PGSIZE);
    *pte = PA2PTE(pagetable) | PTE_V;
  }
}
return &pagetable[PX(0, va)];
```

`walk()` 找到最低层 PTE 的地址，`mappages()` 再写入物理页号和 `PTE_V/R/W/X/U` 权限。真正执行用户指令时，完成这三级查找的是 CPU 的 MMU；软件 `walk()` 用于创建、修改或检查相同的数据结构。TLB 缓存近期翻译结果，修改或切换页表后用 `sfence.vma` 清理陈旧项。

### 内核页表与用户页表

`kvmmake()` 创建所有 hart 共享的 kernel page table：

- UART、virtio、PLIC 以 RW 权限直接映射 MMIO；
- kernel text 映射为 RX；
- kernel data 和物理 RAM 映射为 RW；
- trampoline 映射到最高虚拟页；
- 每个进程的 kernel stack 被映射，并在相邻位置保留 guard page。

大部分 kernel mapping 使用 `VA == PA` 的 direct map。`kvminithart()` 把根页表写入 `satp` 并执行 `sfence.vma`，此后 MMU 开始按 Sv39 翻译地址。

每个进程则有独立的 user page table。`proc_pagetable()` 除普通用户内存外，还建立两个特殊映射：

```text
MAXVA
  ┌────────────────────────┐
  │ TRAMPOLINE: RX, no U   │  同一段 trampoline 代码
  ├────────────────────────┤
  │ TRAPFRAME: RW, no U    │  每个进程各自的寄存器保存页
  ├────────────────────────┤
  │                        │
  │ user stack + guard     │
  │ heap                   │
  │ data                   │
  │ text                   │
  └────────────────────────┘
0
```

trampoline 在 user page table 和 kernel page table 中映射到相同虚拟地址。原因是 trap 刚发生时 CPU 仍在使用 user page table，而 `uservec` 执行过程中要切换 `satp`；相同 VA 映射保证切换页表前后的这几条指令能够连续执行。`TRAMPOLINE` 和 `TRAPFRAME` 都没有 `PTE_U`，用户态代码不能直接访问它们。

### `exec` 如何建立一个可运行的地址空间

`kexec()` 的逻辑可以看成一次带回滚能力的地址空间事务：

1. 打开并校验 ELF；
2. 创建新的 user page table；
3. 遍历 program header，用 `uvmalloc()` 分配物理页并映射；
4. 用 `loadseg()` 把 ELF segment 从 inode 读入物理页；
5. 分配用户栈，并用 `uvmclear()` 建立不可访问的 guard page；
6. 把 `argv` 字符串和指针数组压入新用户栈；
7. 设置 `trapframe->epc = elf.entry` 和 `trapframe->sp = sp`；
8. 最后才替换 `p->pagetable`，释放旧地址空间。

设置 `trapframe->epc` 和 `sp` 后，后面的 `userret -> sret` 就会让 CPU 从 ELF entry 和新用户栈开始执行。这与 scheduler 恢复 kernel context 是同一种思路：先构造硬件可理解的执行状态，再通过一条返回指令让控制流成立。

### 当前版本的 lazy `sbrk`

当前源码的 `sys_sbrk(int n, int t)` 同时支持 eager 和 lazy 两条路径。eager 模式立即调用 `growproc()` 分配和映射物理页；lazy 模式只增加 `p->sz`：

```c
if(t == SBRK_EAGER || n < 0) {
  growproc(n);
} else {
  myproc()->sz += n;
}
```

进程第一次读写尚未映射的地址时，硬件产生 load/store page fault，`scause` 为 13 或 15，`stval` 给出故障虚拟地址。`usertrap()` 调用 `vmfault()`：分配一页、清零、建立 `PTE_W | PTE_U | PTE_R` 映射，然后返回原用户指令重试。

这条路径把内存与中断章节真正连接起来了：page fault 不是“程序崩溃”的同义词，而是一种同步 exception；内核可以检查地址是否合法，并利用它按需完成虚拟内存策略。

---

## 6. 我目前形成的几个核心认识

### 进程是状态，不是 CPU 中的实体

一个可运行进程至少由三部分组成：

- 执行状态：kernel `context`、user `trapframe`；
- 地址空间：user page table 和它映射的物理页；
- 内核管理状态：PID、state、open files、cwd、parent、sleep channel 等。

CPU 只是暂时装载某个进程的寄存器并使用它的页表。所谓“进程在 CPU 上执行”，就是这套状态成为当前硬件状态。

### 调度器本身也是一段可暂停的控制流

scheduler 不是内核之外的裁判，也不是与用户程序并行运行的后台线程。它拥有自己的 kernel stack/context，在切到进程时暂停，在进程 `sched()` 回来后恢复。`swtch()` 的两个参数是对称的，这种对称性比“调度器调用进程”更接近真实机制。

### 中断把硬件时间变成软件状态机转换

timer 只通知 CPU“时间到了”。从 `RUNNING` 变成 `RUNNABLE`、保存 context、选择下一个进程，全是内核软件完成的：

```text
time comparator
  -> hardware trap
  -> trap handler
  -> yield
  -> proc state transition
  -> swtch
  -> scheduler policy
```

因此，中断机制与调度策略可以分开理解：前者提供抢占点，后者决定接下来运行谁。

### 页表也是硬件与软件共享的数据结构

`walk()` 和 `mappages()` 由内核维护页表，MMU 在每次内存访问时消费页表，`satp` 指向根，TLB 缓存结果。页表不是抽象意义上的“地址字典”，而是必须严格符合 RISC-V 位布局和权限语义的硬件协议数据。

---

## 7. 已完成的实验与下一步 develop

只读源码很容易产生“每个函数都看懂了，但系统仍然没有动起来”的感觉。对我更有效的方法是每次只追一条跨模块控制流，并用断点或小改动验证。

### 已完成：环境、构建与启动闭环

这次已经实际完成并复核了以下闭环：

1. 用 `scripts/check-env.sh` 检查 host 工具链；
2. 确认 `kernel/kernel` 和 `fs.img` 可以构建；
3. 用 QEMU 启动 3-hart 的 RISC-V `virt` 机器；
4. 看到 `/init` 拉起 `sh`，执行 `echo` 与 `ls`；
5. 用 `Ctrl-a x` 正常退出 QEMU；
6. 复现并识别只读 `fs.img` 属于 host/QEMU 层失败，而不是 xv6 kernel panic。

这个闭环看似基础，但它建立了后续所有源码实验的可信起点：每次改内核前先证明基线可运行，改动后再用同一条路径比较行为。

### 待完成实验一：逐指令观察启动和特权级切换

```gdb
b _entry
b start
b main
c
```

当前这项实验被本地 Makefile 的 GDB 回归阻断。恢复调试符号和 `qemu-gdb` 目标后，我会在 `_entry` 观察 `mhartid`、`sp`，在 `start` 前后观察 `mstatus`、`mepc`、`satp`，再用 `si` 单步跨过 `mret`。目标不是记住每个 CSR 位，而是亲眼确认：PC 从 `_entry` 到 `start`，再由 `mret` 到 `main`，同时 privilege mode 发生变化。

### 待完成实验二：捕获一次完整的时间片切换

```gdb
b clockintr
b yield
b sched
b swtch
c
```

命中断点后执行：

```gdb
bt
info registers sp ra sepc scause satp
x/16gx $sp
```

在第一次进入 `swtch` 前记录 `sp/ra`，单步执行保存和加载，再观察 `ret` 后的 backtrace。`swtch` 是高频路径，理解后应禁用断点，否则系统会几乎无法前进：

```gdb
disable
c
```

### 待完成实验三：观察 lazy allocation page fault

可以新增一个很小的用户程序：调用 lazy `sbrk` 扩大地址空间，先不访问，再写扩展区的第一个字节。GDB 在 `vmfault` 断下后观察：

```gdb
p/x $scause
p/x $stval
bt
```

然后跟踪 `kalloc -> mappages`，验证“增加逻辑地址空间”与“分配物理页”确实发生在两个不同时间点。

### 已完成：把阅读环境本身当成 develop

为了降低每次进入仓库的成本，我已经在本地增加了几项辅助设施：

- `scripts/check-env.sh`：检查 make、Perl、bc、QEMU、GDB 和 RISC-V GCC；
- `scripts/gentags`：为 `kernel/`、`user/`、`mkfs/` 生成 C/Assembly tags；
- `compile_flags.txt`：让 clangd 理解 RV64、freestanding 和 xv6 include path；
- `.vscode/tasks.json`：封装环境检查、构建、运行和清理；
- `.vscode/settings.json`：指定交叉编译器和 C/Assembly 文件关联；
- `docs/learning-env.md`：记录 WSL 安装、QEMU 运行和建议阅读顺序。

这些改动没有改变 xv6 内核语义，却直接改善了源码跳转、编译反馈和实验复现。与此同时，这次 Makefile/GDB 不一致也暴露出工程化的另一面：工具配置必须能被命令验证，文档不能只凭印象维护。下一步应该先把 GDB task、Makefile target 和文档修到同一状态，再开始更深的内核修改。

### 下一步：把源码理解变成内核 develop

我计划按下面的顺序修改 xv6，每一步只引入一个新机制：

1. 增加只读调试型 syscall，例如输出进程的页表或运行 tick，熟悉 user stub 到 syscall table 的完整链路；
2. 给 scheduler 增加每进程运行 tick 统计，验证 timer、锁和 proc 生命周期；
3. 实现可切换的调度策略，例如在现有 round-robin 之外增加 priority，并用 CPU-bound 程序比较结果；
4. 实现 copy-on-write fork，把 trap、PTE flag、引用计数和 TLB 刷新串起来；
5. 把全局物理页 freelist 改成 per-CPU freelist，分析多核锁竞争和跨 CPU 回收；
6. 继续进入 buffer cache、inode、logging 和 virtio 路径，追踪一次 `echo hello > file` 从 syscall 到磁盘中断的完整生命周期。

其中第 1、2 步适合作为起点，因为行为容易观察，出错范围有限；第 4、5 步才开始真正考验并发不变量和内存一致性。

---

## 8. 一条更适合源码阅读的路线

结合这次实践，我会按依赖关系而不是文件大小阅读：

```text
Boot
  entry.S -> start.c -> main.c

Execution
  proc.h -> proc.c -> swtch.S

Boundary
  trampoline.S -> trap.c -> syscall.c -> kernelvec.S

Memory
  memlayout.h -> riscv.h -> kalloc.c -> vm.c -> exec.c

Storage
  bio.c -> log.c -> fs.c -> file.c -> sysfile.c -> virtio_disk.c

User space
  init.c -> sh.c -> ulib.c -> usys.pl
```

每读一组文件，我会固定回答四个问题：

1. 谁调用它，它最终把控制权交给谁？
2. 进入和离开时，CPU 的 `pc/sp/privilege/satp` 是什么？
3. 哪些状态属于 CPU，哪些属于 proc，哪些是全局共享状态？
4. 哪几行是在操作普通内存，哪几行是在通过 CSR/MMIO 与硬件交互？

这四个问题能把启动、调度、中断和分页放进同一个坐标系。等到这些路径能够在脑中连起来，xv6 就不再是一组短小但陌生的 C 文件，而是一台可以逐指令解释、逐状态验证、也可以亲手改造的操作系统。

## 参考

- 本地源码：MIT `xv6-riscv`，branch `riscv`，commit `5474d4b`
- 仓库内 `README` 与 `docs/learning-env.md`
- `kernel/entry.S`、`kernel/start.c`、`kernel/main.c`
- `kernel/proc.c`、`kernel/swtch.S`
- `kernel/trampoline.S`、`kernel/kernelvec.S`、`kernel/trap.c`
- `kernel/kalloc.c`、`kernel/vm.c`、`kernel/exec.c`
