---
title: "【源码】Bubblewrap：五千行 C 代码如何构建 Linux 用户态沙箱"
date: 2026-08-24T00:00:00+08:00
lastmod: 2026-10-28T00:00:00+08:00
slug: "dive-bubblewrap"
categories:
  - AI Infra
  - 操作系统
tags:
  - Bubblewrap
  - Linux
  - Sandbox
  - Namespace
  - Seccomp
  - AI Agent
description: "结合 Bubblewrap 0.13.x 源码，分析声明式挂载操作、User/Mount/PID Namespace、双重 pivot_root、mount_setattr 与 openat2、Capability 收敛、进程监管、Seccomp 与安全策略边界。"
draft: false
toc: true
---

今天当一个 AI Agent 在开发机或云端工作区里自动执行 Shell、运行测试脚本或编译未知仓库时，“怎样用几毫秒的开销把一次子进程执行关进笼子里”成了 AI 基础设施最迫切的基础问题。重量级虚拟机隔离最强，但每次跑一条百毫秒级的 `pytest` 或 `cargo check` 都拉起一台 VM 显然过于沉重；而完整的容器引擎（Docker / containerd）又背负着镜像分层、CNI 网络栈与常驻守护进程等冗余包袱。

于是很多工程师脑海里会冒出一个看似简单的念头：**既然 Linux 内核已经有 `chroot` 和 `unshare`，我自己写两百行代码调几个系统调用，不就能拼出一个轻量沙箱了吗？**

真正在生产环境里动手试一次就会发现，这种朴素沙箱会在五个致命缺口上瞬间漏成筛子：

1. **单纯 `chroot` 根本锁不住目录树**：`chroot` 只改了进程解析绝对路径的起点，原宿主根目录依然挂在内核挂载树上；只要进程手里还攥着一个切根前打开的目录文件描述符（FD），或者拥有局部权限再调一次 `chroot`，几行代码就能逃回宿主根目录。
2. **挂载传播反向污染宿主机**：现代 systemd 发行版默认把根挂载点设为共享传播（`MS_SHARED`）。如果 `unshare(CLONE_NEWNS)` 之后忘了第一时间切断传播关系，沙箱里挂载的临时目录或卸载动作会原封不动地同步回宿主机全局视图。
3. **“只读递归挂载”在子挂载点上悄悄失效**：Linux 传统的 `mount(src, dst, MS_BIND | MS_REC | MS_RDONLY)` 并不会把 `MS_RDONLY`、`MS_NOSUID`、`MS_NODEV` 自动压入底下嵌套的子挂载点，攻击者往往能在看似只读的目录树深处找到可写或可执行 Setuid 的突破口。
4. **共享 `/proc` 与终端控制面逃逸**：不隔离 PID 命名空间就直接挂 `/proc`，等于把宿主机所有进程信息摆上桌面；不脱离控制终端，恶意代码只需对标准输入调一次 `ioctl(TIOCSTI)`（如经典的 CVE-2017-5226），就能把恶意命令塞进宿主机父 Shell 的输入队列，等沙箱退出后由父 Shell 代为执行。
5. **临时特权残留与失控孤儿进程**：普通用户借助 User Namespace 拿到局部 `CAP_SYS_ADMIN` 完成挂载后，若不在 `execve` 前彻底清空能力集并封死嵌套命名空间，反而给内核漏洞敞开了大门；而一旦外层调用方意外崩溃，沙箱里派生出的后台孙进程就会脱离监管长驻宿主机。

从桌面端 Flatpak 底层演化而来的 **Bubblewrap（`bwrap`）**，正是专门用来堵死上述所有暗坑的“低层沙箱执行器”。它不管理镜像，不配跨机网络，也不替上层拍板安全策略，而是用约五千六百行极度克制的 C 代码（仅强依赖 `libcap`），把命令行声明的挂载与隔离意图严丝合缝地编译成一条**单向不可逆的内核收敛流水线**：

![Bubblewrap 的启动阶段与不可逆收敛点](/images/bubblewrap-lifecycle.svg)

上图标出了从参数解析到最终 `execvp()` 的四段主干与不可回退的安全闸门（亦可打开[交互式启动流程与权限收敛视图](/diagrams/bubblewrap-startup.html)逐节点检索源码依赖）。下面我们就沿着 [`bubblewrap.c`](https://github.com/containers/bubblewrap/blob/0f8073dd9b547e15e8ec9451371f9729830b083d/bubblewrap.c) 的真实执行轨迹，拆解它是如何把这五道缺口逐一焊死的。

---

## 一、解析与执行分离：把命令行编译成 `SetupOp` 与 FD 契约

跟 OCI 容器运行时（`runc` / `crun`）消费现成 Rootfs 镜像与 `config.json` 不同，Bubblewrap 允许调用方直接在命令行上像搭积木一样现场拼出一棵根文件系统：

```bash
bwrap \
  --ro-bind /usr /usr \
  --symlink usr/lib64 /lib64 \
  --proc /proc --dev /dev \
  --perms 0600 --file 3 /run/token \
  --size 67108864 --tmpfs /tmp \
  --unshare-all --new-session --die-with-parent \
  -- /usr/bin/python3 script.py
```

如果在解析参数的同时就去调 `mount()` 或 `mkdir()`，一旦半路遇到非法参数组合，进程就会卡在半成型的命名空间与挂载状态里进退两难。因此 [`main()`](https://github.com/containers/bubblewrap/blob/0f8073dd9b547e15e8ec9451371f9729830b083d/bubblewrap.c) 做的第一件事，是通过 `parse_args_recurse()` 把所有挂载和目录指令编译成一条单向链表中间表示 **`SetupOp`**：

```c
struct _SetupOp {
  SetupOpType type;      // SETUP_RO_BIND_MOUNT / SETUP_MOUNT_PROC / SETUP_MOUNT_DEV ...
  const char *source;
  const char *dest;
  int fd;
  SetupOpFlag flags;
  int perms;
  size_t size;
  SetupOp *next;
};
```

这套“先编译成操作计划，进命名空间后再由 `setup_newroot()` 顺序执行”的架构，顺手解决了三件麻烦事：

- **显式约束修饰符作用域**：像 `--perms 0600` 或 `--size 67108864` 这类前缀修饰符保存在解析器瞬态变量里，被紧随其后的唯一一条创建指令消费后立即归零；如果后面跟了不匹配的操作（例如给 `--ro-bind` 加 `--size`）或是传入了空路径字符串，在触碰任何内核命名空间之前就会直接报错退出。
- **切根前统一按宿主语义解析源路径**：进入新根之后，宿主上的绝对符号链接指向就会失真，部分 FUSE 挂载也只认真实用户 UID。源码在 `raw_clone()` 之后、切根之前专门调用 `resolve_symlinks_in_ops()`，把所有源路径一次性解析固定下来。
- **用已打开的 FD 取代易竞态的字符串路径**：无论是传入超长参数列表的 `--args FD`（上限 `MAX_ARGS = 9000`）、注入敏感凭据的 `--file FD` / `--bind-data FD`，还是加载系统调用过滤器的 `--seccomp FD`，Bubblewrap 一律鼓励上层调用方预先在可信环境里打开对象、校验完权限后把纯文件描述符传进来。对于 `--bind-fd`，源码甚至在完成挂载后再次核对源 FD 与挂载目标的 `st_dev / st_ino`（设备号与 Inode 号），严防从读取 `/proc/self/fd/N` 到真正完成挂载那几微秒之间发生路径偷换竞态（TOCTOU）。

---

## 二、文件系统彻底换血：双重 `pivot_root` 与七百行 Bind Mount 防线

拿到编译好的 `SetupOp` 链表并借 `raw_clone(CLONE_NEWNS | ...)` 进入独立挂载命名空间后，Bubblewrap 开始搭建沙箱的文件系统世界。整套操作环环相扣，一步顺序都不能乱：

![Bubblewrap 文件系统隔离流水线：从 MS_SLAVE 断连到双重 pivot_root](/images/diagrams/aug-sys-bwrap-mount-pivot.svg)

### 1. 第一铁律：`MS_SLAVE` 斩断反向传播与两次 `pivot_root`

子进程落入新 Mount Namespace 的第一行挂载代码不是建新根，而是立即执行：

```c
mount(NULL, "/", NULL, MS_SLAVE | MS_REC, NULL);
```

这行调用把整棵继承来的挂载树改成从属模式（Slave）：宿主机后续若有挂载变更仍可单向流入，但沙箱内部接下来所做的任何挂载与卸载，**绝不会反向泄漏回宿主机**。

紧接着，为什么不能直接在某个目录搭好新环境就 `chroot` 进去，非要折腾**两次 `pivot_root`**？追踪源码路径就能看清它的巧思：

1. **第一次 `pivot_root`（搭跳板工作台）**：代码先在 `/tmp` 上挂一块带 `nodev,nosuid` 的小型 `tmpfs`，在里面建好 `newroot` 和 `oldroot` 两个子目录，随即执行 `pivot_root("/tmp", "/tmp/oldroot")`。切完这一刀，当前命名空间的 `/` 变成了那块干净的 `tmpfs`——原宿主的所有文件整整齐齐退居在 `/oldroot` 前缀下，即将交付给沙箱的新世界则在 `/newroot` 前缀下施工，源和目标两套坐标泾渭分明；接着代码把 `/oldroot/proc` 临时递归绑定到 `/proc`，专门供施工阶段基于 `/proc/self/fd/N` 的句柄操作使用。
2. **按序施工与遮盖敏感内核接口**：`setup_newroot()` 沿着链表逐条兑现挂载。遇到 `--proc` 时，若开启了新 PID 命名空间就挂载全新的 `procfs`，否则绑定宿主 `proc`；但无论走哪条路，都会紧接着把 `/proc/sys`、`/proc/sysrq-trigger`、`/proc/irq` 和 `/proc/bus` 全部强制覆盖为只读，纵深封堵内核参数篡改通道。遇到 `--dev` 时绝不偷懒映射宿主 `/dev`，而是在全新 `tmpfs` 上仅按白名单逐个绑定 `null`、`zero`、`full`、`random`、`urandom`、`tty`，并用 `newinstance` 挂载隔离的伪终端 `devpts`。
3. **安全路径解析（`safe_openat`）**：在往 `/newroot` 里创建目标路径时，如果沙箱目录中含有一条指向外部的恶意符号链接怎么办？Bubblewrap 从 `crun` 引入了 [`safe_openat.c`](https://github.com/containers/bubblewrap/blob/0f8073dd9b547e15e8ec9451371f9729830b083d/safe_openat.c)，优先调用 Linux 5.6+ 的 `openat2(RESOLVE_IN_ROOT | RESOLVE_NO_MAGICLINKS)`，由内核在系统调用层强行把所有符号链接解析锁死在目标根目录之内；且每次重新打开根 FD，防止 `--bind / /` 覆盖顶层挂载后旧 `O_PATH` 句柄失效。
4. **第二次 `pivot_root`（拆跳板毁旧根）**：等 `/newroot` 全部拼装妥当，代码先把 `/oldroot` 设为 `MS_PRIVATE`，随即调用 `umount2("/oldroot", MNT_DETACH)` 将整棵原宿主挂载树彻底剥离；最后 `chdir("/newroot")` 并调用经典的无子目录技巧 `pivot_root(".", ".")`，把 `/newroot` 扶正为最终的 `/`，顺手连刚才那块垫脚石 `tmpfs` 也一并 `MNT_DETACH` 卸掉。至此，原宿主文件系统在当前命名空间里连一丝残影都不复存在。

### 2. 为什么区区一个只读 Bind Mount 要写七百多行代码？

打开 [`bind-mount.c`](https://github.com/containers/bubblewrap/blob/0f8073dd9b547e15e8ec9451371f9729830b083d/bind-mount.c)，整整 736 行代码几乎全在跟 Linux VFS 的历史包袱搏斗：**当你执行 `--ro-bind / /` 或 `--ro-bind /usr /usr` 时，怎样百分之百保证底下挂载的每一个子文件系统也真正变成只读且禁掉 `suid/dev`？**

在 0.13.x 源码中，`bind_mount_fd()` 给出了新旧两条严密路径：

- **现代内核快路径（Linux 5.12+ `mount_setattr`）**：完成 `mount(src, dest, MS_BIND | MS_REC)` 后，对目标目录打开 `O_PATH` 句柄，直接调一次 `mount_setattr(fd, "", AT_EMPTY_PATH | AT_RECURSIVE, ...)`，把 `MOUNT_ATTR_RDONLY | MOUNT_ATTR_NODEV | MOUNT_ATTR_NOSUID` 原子地刷遍整棵子挂载树。注意这里的错误处理极其严格：只有当内核明确返回 `ENOSYS`（系统调用不存在）时才允许走旧版回退路径；若是返回 `EPERM` 等权限错误（见修复提交 `9232fa0`），绝不当成“不支持”静默放过，而是立即报错熔断。
- **旧内核回退路径（重建 `mountinfo` 树逐个重挂）**：一旦退回没有 `mount_setattr` 的老内核，`parse_mountinfo()` 就得亲自读取 `/proc/self/mountinfo`，反转义八进制特殊字符，按 Mount ID 与 Parent ID 在内存里重建整棵父子挂载树，剔除那些被上层挂载完全遮蔽（Overmounted）的不可见节点，甚至为了应对大小写不敏感文件系统的路径偏差，特意通过 `readlink("/proc/self/fd/N")` 拿内核认可的规范路径去匹配，最后深度优先对每一个可见子挂载逐一补做 `MS_BIND | MS_REMOUNT | MS_RDONLY | MS_NOSUID | MS_NODEV`。

哪怕是专为非安全隔离场景准备的 `--not-a-security-boundary` 选项，源码也只允许在这条回退路径的子挂载重挂失败时打告警放行（`BIND_FAIL_OPEN`）；至于命名空间创建、切根、降权或 Seccomp 安装失败，任何时候都铁面无私地执行 Fail-Closed 直接退出。

---

## 三、单向权限收敛与三角色进程监管

文件系统换血之所以能由普通非特权用户完成，靠的是 Linux **User Namespace** 的精巧授权规则：把宿主机的普通用户（比如 `UID 1000`）映射为新建 User Namespace 里的 `UID 0` 后，该进程就在**这个新建命名空间所管辖的资源范围内**拥有了 `CAP_SYS_ADMIN` 挂载能力，但对宿主机原有的根文件系统依然只是个普通 `UID 1000`。

问题在于，搭建沙箱时需要这把“局部尚方宝剑”，而真正把控制权交给不可信的业务程序前，必须把宝剑彻底销毁。Bubblewrap 把这条不可逆的降权之路与三进程监管架构绑在了一起：

![Bubblewrap 单向权限收敛与三角色进程监管生命周期](/images/diagrams/aug-sys-bwrap-priv-drop.svg)

### 1. 五道不可逆权限闸门

顺着状态机从左往右看，权限在五个节点上层层收紧、有去无回：

1. **入口封死提权（`PR_SET_NO_NEW_PRIVS`）**：`acquire_privs()` 一上来先检查并拒绝历史上遗留的 Setuid 位或意外的 File Capability 配置，紧接着 `main()` 调 `prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0)`。这个标志一旦置位便终身跟随进程及所有后代，彻底断绝了通过 `execve` Setuid 程序抬升权限的可能，同时也为后续非特权加载 Seccomp BPF 铺平了道路。
2. **安全写入 UID/GID 映射**：`write_uid_gid_map()` 严格遵循先写 `uid_map`、再向 `/proc/self/setgroups` 写入 `"deny"`、最后写 `gid_map` 的内核安全时序（防止通过 `setgroups` 丢弃辅助组绕过组黑名单限制，即早年的 CVE-2014-8989）。若启用了 `--unshare-net`，此时顺手用 [`network.c`](https://github.com/containers/bubblewrap/blob/0f8073dd9b547e15e8ec9451371f9729830b083d/network.c) 里仅 199 行的原生 `rtnetlink` 客户端把隔离命名空间里的 `lo` 回环网卡配好 `127.0.0.1/8` 并拉起 `IFF_UP`。
3. **按需二级 UserNS 与防嵌套实测验证**：如果用户指定了 `--disable-userns` 防止恶意载荷在沙箱内继续套娃创建新命名空间（内核提权漏洞的重灾区），Bubblewrap 会在第一层 UserNS 里把 `user.max_user_namespaces` 设为 `1`，随即跳进第二层 UserNS 把自己锁死；更硬核的是，写完配置后它不盲信，而是当场试探调用一次 `unshare(CLONE_NEWUSER)`——如果居然成功了，说明内核未兑现限制，立即报错退出！
4. **清空三层 Capability 集合**：切完根之后，`drop_privs(true)` 将 Bounding Set、Permitted/Effective/Inheritable 以及 Ambient Set 统统洗白（非 Root 调用默认不留任何一项能力）。
5. **卡在 `execvp()` 咽喉处的 Seccomp 安装**：为什么 `--seccomp FD` 传入的 BPF 过滤器非要拖到关闭内部 FD、设好 `PDEATHSIG` 之后的最后一刻才调用 `seccomp_programs_apply()`？因为前面的 `mount`、`pivot_root`、`clone` 全是高危系统调用；只有卡在 `execvp()` 前一秒装上过滤器（并且沙箱内的 PID 1 回收进程在进入 `wait()` 前也同步装上同一份过滤器），上层调用方才能放心大胆地给业务载荷套上最严苛的系统调用白名单。

### 2. 为什么要拆成 Monitor、PID 1 与 Target 三个进程？

在启用了 PID Namespace 的默认模式下，一次 `bwrap` 调用在内核里会形成清晰的“祖孙三代”拓扑：宿主侧无特权的 **`monitor_child()`** → 沙箱内的 **`PID 1`（`do_init`）** → 真正执行业务命令的 **`Target`（`PID 2`）**。

少任何一层都会出乱子：

- **沙箱里必须有真正的 `PID 1` 收割孤儿**：业务代码（如编译器或 Shell 脚本）经常 `fork` 出后台子进程。如果没人扮演 `PID 1` 循环调 `wait()`，僵尸进程就会堆积，且锁文件生命周期无法与整个沙箱对齐。
- **宿主侧必须有 `Monitor` 安全透传真实退出码**：既然 `PID 1` 要等所有孙进程死光才能退，那么当业务主进程 `PID 2` 退出时，怎样避免它的真实退出码被后续退出的孙进程冲掉？Bubblewrap 设计了一个极精巧的双句柄循环：宿主侧 `Monitor` 屏蔽掉异步的 `SIGCHLD`，改用 `signalfd` 把信号转成同步可读事件，与 `event_fd` 一起放进 `poll()`。当 `PID 2` 结束时，`PID 1` 把 `exit_status + 1`（加 1 是为了避开 `eventfd` 写入 0 不触发可读的语义）写进 `event_fd`；`Monitor` 每次醒来**铁定先读 `event_fd` 再处理 `signalfd`**，确保哪怕 `PID 1` 紧跟着在同一毫秒退出，调用方拿到的也永远是业务主进程 `PID 2` 精确的退出状态。
- **终端隔离与同生共死绑定**：加上 `--new-session` 调用 `setsid()` 斩断 `TIOCSTI` 终端注入通道，加上 `--die-with-parent` 调用 `prctl(PR_SET_PDEATHSIG, SIGKILL)` 确保一旦上层 Agent 宿主或超时控制器杀掉 `bwrap`，整个沙箱进程树瞬间随之灰飞烟灭，绝不留失控孤儿。而在 C 代码工程细节上，[`utils.h`](https://github.com/containers/bubblewrap/blob/0f8073dd9b547e15e8ec9451371f9729830b083d/utils.h) 广泛借助编译器属性 `__attribute__((cleanup(...)))` 定义 `cleanup_free` 与 `cleanup_fd`，让海量早退分支自动释放内存与句柄，从源头减少句柄泄漏进沙箱的隐患。

---

## 四、安全边界的诚实账本：为 AI Agent 构建沙箱的启示

读透这五千六百行 C 代码，最让人佩服的不是某个炫技的系统调用，而是 Bubblewrap 在 [`SECURITY.md`](https://github.com/containers/bubblewrap/blob/0f8073dd9b547e15e8ec9451371f9729830b083d/SECURITY.md) 中对自己能力边界的绝对诚实：**机制再严密，也救不了放水或自相矛盾的调用策略。**

当我们基于 Bubblewrap（或同类 Linux 命名空间原语）为 AI Agent 打造代码执行沙箱时，必须先写清威胁模型，把以下四类 Bubblewrap 自身不替调用方兜底的边界逐项补齐：

| 风险维度 | 典型暴露面或底层限制 | 具体逃逸/破坏机制 | 调用方必须补齐的工程防线 |
| --- | --- | --- | --- |
| **高权限控制面句柄** | 误挂载 `docker.sock`、SSH Agent、D-Bus 或 X11 套接字 | 无需任何内核漏洞，直接顺着合法 IPC 协议接管宿主容器、盗用密钥签名或注入桌面按键 | 从空白根按最小白名单挂载，严禁透传宿主守护进程套接字与未过滤的继承 FD |
| **外发网络与内网探测** | 直接开启 `--share-net` 共享宿主机网卡 | 恶意脚本可直连云平台元数据地址（`169.254.169.254`）或扫描宿主机本机未鉴权端口 | 强制 `--unshare-net` 仅留回环口，通过受控 Unix Socket 桥接宿主侧域名白名单代理 |
| **资源耗尽（DoS）** | 未套外层 Cgroup 直接运行不可信编译或脚本 | Fork 炸弹耗尽宿主 PID 表、内存分配打爆整机触发 OOM Killer、死循环占满物理核 | 由外层 `systemd` Scope 或 Cgroup v2 联合限制 `pids.max`、`memory.max`、CPU 配额与墙钟超时 |
| **共享宿主内核攻击面** | 默认放开全部系统调用或允许嵌套 UserNS | 载荷在沙箱内再次 `unshare` 获取新命名空间能力，利用 Linux 内核驱动/网络栈提权漏洞穿透隔离 | 开启 `--disable-userns` 锁死嵌套，注入收敛的 `--seccomp` BPF 白名单；高危多租户负载外包 MicroVM |

---

> **源码基线与演进说明**：本文分析固定在官方仓库 `containers/bubblewrap` 提交 [`0f8073d`](https://github.com/containers/bubblewrap/tree/0f8073dd9b547e15e8ec9451371f9729830b083d)（`v0.13.0-4-g0f8073d`，共 5,640 行 `.c` 源码，核心文件包括 `bubblewrap.c` 3,568 行、`utils.c` 1,137 行、`bind-mount.c` 736 行、`safe_openat.c` 333 行与 `network.c` 199 行）。相较早期的 `0.11.x`（`2f55bae`），`0.13.x` 最关键的演进在于：Linux 5.12+ 优先采用 `mount_setattr(AT_RECURSIVE)` 原子设置子挂载安全标志（仅在 `ENOSYS` 时回退 `mountinfo` 逐项重挂，见 `9232fa0`）、引入基于 `openat2(RESOLVE_IN_ROOT)` 的 `safe_openat` 防路径逃逸、在首次 `pivot_root` 后临时挂载 `/proc` 支撑句柄挂载，以及严格拒绝空字符串路径参数。
