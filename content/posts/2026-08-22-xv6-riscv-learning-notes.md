---
title: "【源码】RISC-V xv6 : 启动、调度、中断与分页源码学习笔记"
date: 2026-08-22T00:00:00+08:00
lastmod: 2026-10-07T00:00:00+08:00
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

按下电源复位键的那一瞬间，CPU 里既没有“进程”，也没有“虚拟内存”，甚至连调用一个最普通 C 函数的资格都没有——程序计数器（PC）孤零零地指着物理内存里的某个起始地址，全套寄存器处于最高权限的机器态（Machine Mode），栈指针 `sp` 里的数值毫无意义。

如果把操作系统教材里的抽象名词先统统忘掉，直面裸机硬件，任何内核源码其实都必须先回答三个最根本的物理问题：

1. **第一行 C 代码是怎么跑起来的？** 只有物理地址和机器态寄存器时，谁来给 C 编译器生成的栈帧腾地方，又怎样从至高无上的机器态降权到受保护的用户态？
2. **用户态写下 `while(1);` 死循环时，内核凭什么强行夺回控制权？** 当 CPU 正在全速执行用户指令时，内核代码明明不在运行，它是靠什么硬件机关打断当前执行流、换上另一套栈和页表的？
3. **同一个虚拟地址为什么在不同进程眼里指向不同物理内存，而在切换页表的刹那又不会让 CPU 取指踩空崩溃？**

带着这三个问题重读 MIT 的 `xv6-riscv` 源码，不再按文件目录逐个背模块，而是顺着一条真实的硬件控制流——从 QEMU 复位地址 `0x80000000` 出发，一路追踪寄存器、栈指针、控制状态寄存器（CSR）与三级页表的每一次易手——整座内核的骨架顿时变得无比通透。

---

## 一、从 `0x80000000` 到首个 Shell：两次“伪造返回”驱动的冷启动

执行 `make qemu` 拉起 xv6 时，QEMU 模拟出一块带 3 个硬件线程（`hart`，`-smp 3`）和 128 MiB 物理内存的 RISC-V `virt` 开发板，加上 `-bios none` 参数跳过外部固件，直接按链接脚本 `kernel/kernel.ld` 的约定把内核镜像装入物理地址 `0x80000000`，并让所有 `hart` 同时从这里开始取指。

从裸机复位到屏幕上打出第一个 `$ ` 提示符，控制流跨越了机器态（M-mode）、监管态（S-mode）和用户态（U-mode）三道特权级边界：

![xv6-riscv 冷启动特权级跃迁：从 0x80000000 裸机复位到用户态 /init](/images/diagrams/aug-sys-xv6-boot-modes.svg)

### 1. `entry.S`：给每个物理核划出第一块 4 KiB 栈

为什么内核的第一行代码必须是汇编而不是 C？因为 C 语言编译器一进函数体就会生成向栈上保存返回地址和局部变量的指令，没有合法的 `sp`，第一条访存就会踩坏未知物理内存。`kernel/entry.S` 全文核心只有寥寥数行：

```asm
la   sp, stack0
li   a0, 1024*4
csrr a1, mhartid
addi a1, a1, 1
mul  a0, a0, a1
add  sp, sp, a0
call start
```

此时分页尚未开启，所有地址都是实打实的物理地址。由于 3 个 `hart` 同时执行这段代码，如果共用同一个栈就会互相覆盖栈帧。代码通过 `csrr` 读取硬件只读寄存器 `mhartid`（编号 `0, 1, 2`），在全局字节数组 `stack0` 里为每个核切出一段互不重叠的 4096 字节空间；又因为栈向低地址生长，`sp` 必须指向各自区间的顶部 `stack0 + (mhartid + 1) * 4096`。一旦 `sp` 站稳脚跟，一条 `call start` 把返回地址写入 `ra`，C 语言世界的大门才算正式打开。

### 2. `start.c`：借 `mret` 实施第一次“伪造返回”降权

QEMU 复位后处于无所不能的 M-mode，但现代内核为了利用硬件分页保护与中断委托机制，主体逻辑都运行在次一级的 S-mode。有趣的是，RISC-V 并没有一条叫作“向下跳转进监管态”的普通跳转指令；要想体面地降权，标准做法是**伪造一次从异常返回的现场，再执行机器态异常返回指令 `mret`**。

在 `kernel/start.c` 的 `start()` 函数里，每个 `hart` 依次完成这组硬件握手：

- 修改 `mstatus` 寄存器的 `MPP`（Machine Previous Privilege）字段为 `S-mode`，欺骗硬件“刚才我是从监管态陷入进来的”；
- 把 C 函数 `main` 的入口地址写进机器态异常程序计数器 `mepc`，指定“返回后跳去哪里继续取指”；
- 将页表根寄存器 `satp` 清零暂时关闭分页，配置物理内存保护（PMP）允许 S-mode 访问全部物理空间，并通过 `medeleg` 和 `mideleg` 把绝大多数同步异常与中断直接委托给 S-mode 处理；
- 在 `timerinit()` 中写 `stimecmp = r_time() + 1000000` 并打开 `MIE_STIE`，为日后的时间片抢占埋下第一颗定时种子，随后把 `mhartid` 存入 `tp` 寄存器供内核随时调用 `cpuid()` 识别身份。

最后执行一句内联汇编 `asm volatile("mret")`，硬件便原子地把特权级降为 S-mode，同时将 PC 拨向 `main()`。

### 3. `main.c` 到 `/init`：多核屏障与第二次“伪造返回”

所有核一脚踏进 `kernel/main.c` 后，全局共享的数据结构（物理页空闲链表 `kinit`、内核页表 `kvminit`、进程数组 `procinit`、磁盘与块缓存）绝不能初始化三次，因此只由 `hart 0` 一人包揽：

```c
if(cpuid() == 0){
  consoleinit();
  printfinit();
  kinit();            // 回收内核末尾 end 到 PHYSTOP 的空闲物理页
  kvminit();          // 构建内核直接映射页表
  kvminithart();      // 写 satp 并执行 sfence.vma，正式开启 Sv39 分页！
  procinit();
  trapinit();
  trapinithart();     // 将 stvec 指向 kernelvec
  plicinit();
  plicinithart();
  binit();
  iinit();
  fileinit();
  virtio_disk_init();
  userinit();         // 捏造出系统中第一个 RUNNABLE 进程
  __sync_synchronize();
  started = 1;
}
```

注意倒数第二行的内存屏障 `__sync_synchronize()`：在弱内存序架构下，如果没有这道屏障，`hart 1` 和 `hart 2` 可能先在缓存里看到 `started == 1` 而冲出等待循环，此时 `hart 0` 对内核页表或 `proc[]` 数组的写入却还没对它们可见。穿过屏障后，其余核只需调用 `kvminithart()`、`trapinithart()` 和 `plicinithart()` 装载各自的硬件寄存器，随后三颗核殊途同归，一头扎进永不返回的 `scheduler()`。

那么 `userinit()` 造出来的第一个进程，从来没有在 CPU 上跑过，它是怎么启动的？答案依然是**伪造返回现场**。`allocproc()` 在分配进程槽位时故意写下两行关键赋值：

```c
memset(&p->context, 0, sizeof(p->context));
p->context.ra = (uint64)forkret;
p->context.sp = p->kstack + PGSIZE;
```

当某颗核上的 `scheduler()` 扫到这个处于 `RUNNABLE` 状态的初始进程、调用 `swtch` 换上它的 `context` 并执行末尾的 `ret` 指令时，CPU 顺着预设的 `ra` 第一次跳进内核函数 `forkret()`（完成依赖进程睡眠机制的文件系统初始化与根程序加载），紧接着走 `prepare_return() -> userret` 恢复预先填在 `trapframe` 里的 ELF 入口地址与用户栈，最后伴随一条监管态返回指令 `sret` 降权进入 U-mode，跑起 `/init` 并拉起交互式 `sh`。

---

## 二、强行夺回 CPU：跳板页、双现场与 `swtch` 对称切栈

现在 `/init` 派生的用户进程已经在 U-mode 下跑起来了。假设某个用户程序写了一个纯粹的算术死循环 `while(1);`，既不调系统调用，也不主动睡眠，早已把执行权交出去的内核靠什么把它拉下马？

答案藏在硬件定时器与两套完全独立的寄存器保存区（`trapframe` 与 `context`）的精密接力之中。下面这张全链路控制流图完整串起了从用户态陷入、内核分发到跨栈切换的每一步（推荐点开[交互式陷阱与上下文切换路径视图](/diagrams/aug-sys-xv6-trap-swtch.html)，在右上角切换“时钟抢占切换路径”与“系统调用/缺页直返路径”对照观察）：

![xv6-riscv 跨态陷阱、时钟抢占与双栈上下文切换全链路](/images/diagrams/aug-sys-xv6-trap-swtch.svg)

### 1. 硬件只做最小动作，剩下全靠 `trampoline.S` 走钢丝

在 RISC-V 硬件里，单调递增的 `time` 计数器一旦追平 `start.c` 设下的 `stimecmp` 阈值，无论当前用户进程愿不愿意，CPU 都会在两条指令之间强制触发一次监管态时钟中断（Supervisor Timer Interrupt）。

但 RISC-V 硬件在陷入瞬间做得非常克制——它**不会**替你把 32 个通用寄存器压栈，也不会自动切换页表 `satp` 或栈指针 `sp`（因为在硬件看来，强行在每次陷阱里写内存既慢又缺乏灵活性）。硬件只原子地干四件事：

1. 把被打断的那条用户指令地址存进 `sepc`；
2. 把中断原因写入 `scause`（时钟中断对应 `0x8000000000000005L`）；
3. 在 `sstatus` 里记下陷入前的特权级（U-mode）并关闭 S-mode 中断；
4. 将当前特权级切到 S-mode，然后把 `pc` 直接设为 `stvec` 指向的 `trampoline.S:uservec`。

请仔细体会 `uservec` 第一条指令面临的惊险处境：**此时特权级虽然已经是 S-mode，但 `sp` 里装的还是不可信的用户栈地址，31 个通用寄存器里全是用户态现场（连一个空闲临时寄存器都没有！），更要命的是 `satp` 寄存器里装的依然是用户页表！**

xv6 是怎么在钢丝上完成切换的？

- 首先，进用户态前 `prepare_return()` 早就把该进程 `trapframe` 的虚拟地址预先藏在了专用交换寄存器 `sscratch` 里。`uservec` 第一条指令执行 `csrrw a0, sscratch, a0`，毫发无损地把用户的 `a0` 暂存进 `sscratch`，同时让 `a0` 拿到 `trapframe` 基址，一口气把全部 32 个用户寄存器妥妥当当地存进 `p->trapframe`；
- 接着，从 `trapframe` 预设槽位里读出该进程专属的内核栈顶赋给 `sp`、读出当前物理核编号赋给 `tp`，再读出内核根页表值写入 `satp` 并执行 `sfence.vma` 刷新快表；
- **最精妙的一步**：为什么 `csrw satp` 改掉页表的下一纳秒，CPU 去取下一条指令不会触发页错误崩溃？因为 xv6 在构建每一个用户页表和全局内核页表时，都把同一页物理 `trampoline` 代码映射到了**完全相同的最高虚拟地址 `TRAMPOLINE`**（且不置 `PTE_U`，严防用户态偷跑或篡改）。页表在脚下瞬间抽换，但新旧两张地图上 `PC + 4` 这个虚拟坐标指向同一块物理机器码，控制流得以丝滑跨入 C 函数 `usertrap()`。

### 2. 为什么需要 `trapframe` 和 `context` 两套现场？

初读 `kernel/proc.h` 时，最容易犯迷糊的就是“既然 `p->trapframe` 已经保存了整整 32 个寄存器，为什么 `struct proc` 里还要再塞一个 14 寄存器的 `p->context`？”

把一次时钟抢占的后半程走完就彻底明白了：

`usertrap()` 认出 `devintr() == 2`（来自 `clockintr()` 时钟中断，且已在里面将下一次闹钟拨到了 `r_time() + 1000000`），便调用 `yield()`。`yield()` 锁住进程状态将其改为 `RUNNABLE`，接着调用 `sched()`，最终落到汇编函数 [`kernel/swtch.S`](https://github.com/mit-pdos/xv6-riscv/blob/riscv/kernel/swtch.S)：

```c
swtch(&p->context, &mycpu()->context);
```

看清 `swtch.S` 里的 29 行汇编：

```asm
swtch:
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

原来，进程在内核态执行 `usertrap -> yield -> sched -> swtch` 时，自己也是一段标准的 C 函数调用流，跑在属于它自己的内核栈 `p->kstack` 上。按照 RISC-V 函数调用约定（ABI），调用者保存寄存器（Caller-Saved，如 `t0-t6`、`a0-a7`）早就由编译器按需压在了 `p->kstack` 的调用帧里；`swtch` 作为一个被正常 `call` 进来的函数，只需要替当前内核执行流存下返回地址 `ra`、栈顶 `sp` 以及被调用者保存寄存器 `s0-s11`（共计 14 个 64 位寄存器），就足以完整冻结住这个内核暂停点！

这也顺手打破了另一个常见错觉：**内核里根本不存在一个“在后台和用户进程并行运转”的独立调度线程**。每颗物理核（`struct cpu`）都有一套专属的调度栈和 `cpu->context`：

- 当核 0 正在跑进程 A 时，核 0 的 `scheduler()` 其实正静静地冻结在上一次 `swtch(&c->context, &pA->context)` 的那行代码里；
- 当进程 A 被时钟中断打进内核、调用 `swtch(&pA->context, &c->context)` 时，硬件寄存器切回核 0 的调度栈，`scheduler()` 就像刚从普通函数调用里返回一样醒过来，执行下一行 `c->proc = 0`，继续扫描 `proc[]` 数组寻找下一个 `RUNNABLE` 的进程 B；
- 一旦选中进程 B 并执行 `swtch(&c->context, &pB->context)`，CPU 就跳回进程 B 上次在内核里睡着的地方，顺着 `sched -> yield -> usertrap -> usertrapret` 一路倒退回 `trampoline:userret`，换上进程 B 的用户页表，把进程 B 的 `trapframe` 装回 32 个通用寄存器，最终执行 `sret` 回到进程 B 被打断的那条用户指令。

一句话分清二者：**`trapframe` 守在用户态与内核态的城门口，保存跨特权级边界时的完整用户现场；`context` 守在内核态进程栈与调度器栈的交接处，保存两次 `swtch` 协程切换之间的内核函数暂停点。** 把整条链路里反复出现的三种“返回指令”放在一起对照，xv6 的硬件控制流边界就彻底串通了：

| 指令 | 特权级变化 | 状态恢复来源 | 在 xv6 中的核心使命 |
| --- | --- | --- | --- |
| `mret` | M-mode → S-mode | `mepc`（目标 PC）+ `mstatus.MPP` | 冷启动阶段从 `start()` 首次降权跳入内核 `main()` |
| `ret` | S-mode 内部平级跳转 | 通用寄存器 `ra`（配合新 `sp` 与 `s0-s11`） | 在 `swtch.S` 中完成进程内核栈与每核 `scheduler()` 栈的对称切换 |
| `sret` | S-mode → U-mode（或回 S-mode） | `sepc`（目标 PC）+ `sstatus.SPP` | 在 `trampoline:userret` 末尾完成跨态返回，恢复用户指令执行 |

---

## 三、物理页与虚拟地址解耦：Sv39 三级页表与按需缺页

搞懂了控制流切换，再看 `kalloc.c` 与 `vm.c` 里的内存管理，核心线索就是把**“批一块 4 KiB 物理内存”**和**“在地址空间里画一条虚拟映射”**拆成两件正交的事。

### 1. 借用空闲页自身做节点的物理分配器

xv6 管理的物理 RAM 区间从内核镜像末尾符号 `end`（向上页对齐）一直延伸到 `PHYSTOP`（`0x80000000 + 128 MiB`）。既然空闲的物理页里还没有装任何用户有效数据，`kernel/kalloc.c` 干脆连额外的元数据数组都不分配，直接把每个空闲 4 KiB 物理页开头的 8 个字节强转成单向链表指针 `struct run *next`：

```c
struct run {
  struct run *next;
};
```

`kalloc()` 加自旋锁从表头摘下一页（并在调试语义下填满垃圾字节 `0x05` 以便尽早暴露未初始化读取），`kfree()` 填满 `0x01` 后压回表头。用户代码段、进程内核栈、管道缓冲区乃至构成页表本身的中间目录页，全都是从这个最朴素的页池里一页页领出来的。

### 2. Sv39 硬件查表流水线与 Lazy `sbrk` 缺页闭环

在 64 位 RISC-V 上，xv6 启用的是 **Sv39 分页方案**：64 位虚拟地址只使用低 39 位（xv6 进一步将最高虚拟地址 `MAXVA` 限制在 `1L << 38` 即 256 GiB，省去高位符号扩展的麻烦）。为什么页表偏偏设计成三级、每级索引偏偏是 9 比特？算一笔整除账就一目了然：一页物理内存 4096 字节，每条页表项（PTE）占 8 字节，一页恰好放下 `4096 / 8 = 512`（即 `2^9`）个条目。低 12 位留作 4 KiB 页内偏移（Offset），剩下的 27 位虚拟页号自然均分成 `VPN[2]`、`VPN[1]`、`VPN[0]` 三段 9 比特索引：

![xv6-riscv Sv39 三级页表地址翻译与高地址特殊页布局](/images/diagrams/aug-sys-xv6-sv39-pagetable.svg)

内核里的 `walk(pagetable, va, alloc)` 函数几乎就是 CPU 硬件内存管理单元（MMU）查表电路的 C 语言镜像：从 `satp` 指向的 L2 根页表出发，依次用 `PX(2, va)`、`PX(1, va)` 取出 PTE；若有效位 `PTE_V` 为 1，就通过 `PTE2PA(*pte)` 提取 44 位物理页号拼成下一级页表的物理基址（因为内核页表对物理 RAM 做了 `VA == PA` 恒等直接映射，内核可以直接解引用这个物理地址！）；若途中发现目录页尚未建立且 `alloc == 1`，便当场调 `kalloc()` 补齐并清零，最终返回 L0 叶子 PTE 的指针供 `mappages()` 填入物理页地址与 `PTE_R/W/X/U` 权限位。

把这套页表机制与第二章的陷阱机制合龙，就能看懂仓库基线（`5474d4b`）中自带的 **Lazy `sbrk`（按需内存分配）** 与 **`exec` 栈保护页**：

- **用缺页异常推迟物理分配**：当用户程序调用默认模式的 `sbrk(n)` 申请大块堆内存时，`sys_sbrk()` 并不急着调 `growproc(n)` 去搜刮物理页，而是只把进程逻辑大小 `myproc()->sz += n` 往上涨了事。等到用户指令真正第一次去读写那片新扩展的地址时，MMU 查表撞上 `PTE_V == 0`，当场触发同步页错误异常（Load/Store Page Fault，`scause` 为 13 或 15，故障虚拟地址记在 `stval` 中）。`usertrap()` 捕获该异常后转交 `vmfault()`，核实 `stval` 落在合法堆区间内，这才调用 `kalloc()` 拿出一页物理内存、清零并挂入用户页表，最后经 `sret` 回到刚才报错的那条用户指令重试——在用户程序毫无察觉的情况下完成了按需兑现。
- **用清除 `PTE_U` 打造栈溢出护栏**：`kexec()` 在加载 ELF 镜像并分配两页空间时，只把高地址页设为合法的用户栈，紧贴其下的低地址页故意调 `uvmclear()` 抹掉 `PTE_U` 权限位充当 Guard Page。一旦用户递归过深把栈指针踩进护栏页，硬件立刻触发页异常拦截，绝不会让它静默踩坏下方的堆数据。

---

## 四、跨架构实验与排障实录：QEMU 边界、GDB 断点与演进路线

在 x86_64 宿主机（WSL2）上折腾 `xv6-riscv` 时，最容易掉进的坑就是分不清问题出在宿主机文件系统、交叉工具链、QEMU 虚拟板卡还是 xv6 内核代码里。首先在概念上要钉死一件事：宿主机的 AMD x86_64 处理器根本不认识半条 RISC-V 64 指令；我们是用 `riscv64-linux-gnu-gcc` 交叉编译出纯粹的 RV64 ELF 内核镜像，再由全系统模拟器 `qemu-system-riscv64` 在软件层把整块 `virt` 主板（3 颗 RV64 核、UART 串口、PLIC 中断控制器、VirtIO 磁盘）连同指令集一起模拟出来。

### 1. 已跑通的构建启动闭环与一次只读镜像排障

安装齐交叉工具链与 QEMU 依赖后，执行 `make qemu` 即可看到 3 颗 `hart` 启动并拉起 Shell（由于启用了 `-nographic` 将串口复用在当前终端，退出虚拟机不能在 xv6 的 `sh` 里敲 `exit`，而要依次按下 `Ctrl-a` 松开后再按 `x`）：

```text
xv6 kernel is booting

hart 1 starting
hart 2 starting
init: starting sh
$ echo xv6-learning
xv6-learning
```

在验证过程中我曾撞见过一次很典型的启动报错：`Could not open 'fs.img': Read-only file system`。当时第一反应以为是文件系统构建脚本 `mkfs` 出了问题，细查 QEMU 启动参数才意识到：VirtIO 块设备驱动默认要求以读写模式打开根磁盘镜像 `fs.img`，而当时工作目录恰好挂在只读路径下——**连第一条 `_entry` 汇编都还没开始跑，QEMU 在挂载虚拟外设阶段就直接退出了**。这类现象提醒我们排障时先划清宿主/模拟器层与客户机内核层的故障边界。

### 2. 本地 Makefile 的 GDB 回归与三组待执行单步实验

上游基线原本提供了开箱即用的双终端远程调试链路——终端一跑 `make qemu-gdb`（附带 `-S` 让虚拟 CPU 在复位点暂停，并监听动态计算的 TCP 端口），终端二跑 `gdb-multiarch -x .gdbinit` 即可挂载符号表。但我在复核本地工作树时执行 `make print-gdbport`，却收到了 `No rule to make target 'print-gdbport'`。检查 `git diff` 才发现，此前整理本地辅助脚本（如环境自检 `scripts/check-env.sh`、`clangd` 交叉头文件路径 `compile_flags.txt` 与 `.vscode/tasks.json`）时，一份未收尾的本地 `Makefile` 改动误删了 `qemu-gdb` 目标以及 `CFLAGS` 里的 `-ggdb -gdwarf-2`。

工程配置同样是源码真相的一部分，绝不能把“文档里写的命令”当成“当前工作树已验证的状态”。修复恢复 `Makefile` 的 DWARF 调试符号与 `qemu-gdb` 目标后，接下来最值得亲手单步验证的三组跨边界实验已经列好清单：

1. **单步跨过 `mret` 验证特权级降级**：在 `_entry`、`start`、`main` 下断点，观察各核 `mhartid` 与初始 `sp` 落点，在 `mret` 指令前后用 `info registers mstatus mepc satp` 搭配 `si` 单步跨界，亲眼确认硬件自动完成 PC 跳转与监管态切换。
2. **捕获一次完整的时钟抢占切栈**：依次断在 `clockintr -> yield -> sched -> swtch`，在进入 `swtch` 前打印当前栈顶 `sp` 与 `ra`，单步穿过 14 个寄存器的保存与恢复后再次执行 `bt`，观察调用栈如何瞬间从进程内核栈变成 `scheduler()`（验证完需及时 `disable` 高频的 `swtch` 断点，否则虚拟机寸步难行）。
3. **抓拍 Lazy `sbrk` 缺页兑现现场**：写一个 10 行的小用户程序调用延迟版 `sbrk` 扩充地址空间但不读写，随后向新区间首字节写入数据，在 `vmfault` 处断下核对 `$scause`（应为 `0xf` Store Page Fault）与 `$stval` 故障地址，确认逻辑扩容与物理 `kalloc` 发生在两个完全分离的时刻。

### 3. 从读懂源码到动手改内核的演进阶梯

顺着依赖拓扑往深处走，阅读与动手改造 `xv6-riscv` 最顺畅的顺序是先打通跨态边界，再挑战多核锁竞争与崩溃一致性：

- **第一阶（打通跨态链路与统计）**：新增一个只读观测型系统调用（如打印当前进程三级页表有效 PTE 或累计运行 tick 数），完整串起 `usys.pl` 用户态桩代码、`ecall`、`syscall.c` 分发表与 `copyout`；
- **第二阶（整合陷阱、页表与引用计数）**：实现写时复制（Copy-on-Write `fork`），让父子进程共享只读物理页并利用自定义软件保留位标记 COW，在写保护触发 `vmfault` 时按需拷贝新页并刷 `sfence.vma`；
- **第三阶（多核并发与持久化事务）**：将 `kalloc.c` 那把所有核争抢的全局自旋锁拆成 Per-CPU 空闲链表加跨核窃取，随后深入 `bio.c -> log.c -> fs.c -> virtio_disk.c`，追踪一行 `echo hello > file` 如何穿过预写日志（WAL）事务落入磁盘中断。

每翻开一组新文件，只要死死盯住四个不变量——**谁交出控制权又由谁接手、进出时 `pc/sp/privilege/satp` 各自是谁、状态归属物理核还是归属进程、哪几行在读写普通内存而哪几行在拨动 CSR/MMIO 硬件开关**——哪怕面对体量百倍于 xv6 的生产级内核，也不会在代码森林里迷失方向。

---

> **学习环境与源码基线**：本文源码基于 MIT `xv6-riscv` 仓库 `riscv` 分支提交 `5474d4b`（已内置 Lazy `sbrk` / `vmfault` 按需分配路径）。本地编译与运行实验在 x86_64 主机（AMD Ryzen AI 9 HX 370，WSL2 Linux `6.6.87.2`）上完成，工具链版本为 `riscv64-linux-gnu-gcc 13.3.0`、`qemu-system-riscv64 8.2.2` 与 `gdb-multiarch 15.1`。
