---
title: "【源码】Bubblewrap：五千行 C 代码如何构建 Linux 用户态沙箱"
date: 2026-08-24T00:00:00+08:00
lastmod: 2026-08-30T00:00:00+08:00
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
description: "结合 Bubblewrap 0.12.0 开发分支源码，分析声明式挂载操作、User/Mount/PID Namespace、双重 pivot_root、Capability 收敛、进程监管、Seccomp 与安全策略边界。"
draft: false
---

AI Agent 可以读写文件、执行 Shell、调用编译器，甚至运行来源不明的项目代码。模型能力越强，“如何限制一次代码执行能够看到什么、修改什么、访问什么”就越接近 AI Infra 的基础问题。

虚拟机提供较强隔离，但启动和资源成本较高；完整容器运行时提供镜像、网络、Cgroup 与生命周期管理，但对一次短命令执行可能过重。Bubblewrap（`bwrap`）选择了更窄的定位：组合 Linux Namespace、Mount、Capability、Seccomp 和进程监管机制，为调用方构造一个临时的文件系统与进程视图。

它不替调用方定义安全策略，也不是一个完整的 OCI Runtime。它更像一个低层沙箱执行器：

```text
调用方定义策略
  → Bubblewrap 把策略翻译成 Namespace、Mount 与权限操作
  → Linux Kernel 执行真正的隔离
  → Bubblewrap 监管进程并传播退出状态
```

本文基于 `~/xwork/bubblewrap` 的当前源码，重点回答：

1. Bubblewrap 与 Docker、runc、systemd-nspawn 的边界是什么？
2. 普通用户如何借助 User Namespace 获得“只在命名空间内有效”的管理能力？
3. 为什么文件系统构建需要两次 `pivot_root`？
4. 为什么简单的 Bind Mount 加只读标记，会扩展成数百行 `mountinfo` 处理？
5. 外部 Monitor、沙箱 PID 1 与目标程序为什么需要三个进程角色？
6. Bubblewrap 已经做了什么，安全策略调用方还必须补充什么？

## 核心结论

1. **Bubblewrap 是策略执行器，不是安全策略本身。** 它负责可靠地建立 Namespace、挂载树与权限边界；哪些目录、Socket、设备和网络应该暴露，完全由调用参数决定。
2. **User Namespace 是非特权容器能力的根。** 普通用户可以在新 User Namespace 中获得仅对该 Namespace 资源有效的 Capability，再用它创建 Mount、PID、Network 等隔离视图。
3. **安全性来自多层收敛，而不是单一 Chroot。** `PR_SET_NO_NEW_PRIVS`、User/Mount Namespace、`pivot_root`、`nosuid/nodev/ro`、Capability Bounding Set、Seccomp 和 FD 清理共同组成边界。
4. **两次 `pivot_root` 解决的是旧根可达性。** 第一次提供同时访问 `oldroot` 和 `newroot` 的搭建环境；第二次把 `/newroot` 提升为真正根目录，并彻底拆掉旧挂载树。
5. **SetupOp 是全项目最重要的中间表示。** CLI 参数先编译为有序操作链表，进入 Mount Namespace 后再执行，实现了解析、校验与特权操作的阶段分离。
6. **进程监管本身也是安全协议。** Monitor、沙箱 PID 1 与目标进程通过 `eventfd`、`signalfd`、Pipe 和 Credential Socket 传递状态，避免僵尸进程、退出码竞态和不可信 PID。
7. **默认应 Fail-Closed。** 当前新增的 `--not-a-security-boundary` 只适用于明确不把本次调用当作安全边界的场景；即便启用，Namespace、`pivot_root`、Capability Drop 等关键失败仍会终止。
8. **Namespace 不是虚拟机。** 沙箱仍共享宿主 Linux Kernel；D-Bus、Wayland/X11、设备、宿主目录和网络一旦被暴露，就会成为策略的一部分。

### 源码分析基线

| 项目属性 | 当前源码 |
|----------|----------|
| 本地目录 | `~/xwork/bubblewrap` |
| Commit | [`2f55bae`](https://github.com/containers/bubblewrap/tree/2f55bae38468d0c50cf5df87b1e481e882b63acb) |
| Git Describe | `v0.11.2-11-g2f55bae` |
| Meson Version | `0.12.0`，预发布开发分支 |
| Commit 日期 | 2026-06-02 |
| `.c` 实现规模 | 5,146 行 |
| 加上项目头文件 | 约 5,440 行 |
| 必选库依赖 | `libcap` |
| 可选库依赖 | `libselinux` |

| 文件 | 行数 | 主要职责 |
|------|------|----------|
| [`bubblewrap.c`](https://github.com/containers/bubblewrap/blob/2f55bae38468d0c50cf5df87b1e481e882b63acb/bubblewrap.c) | 3,255 | 参数解析、Namespace、挂载编排、权限与进程树 |
| [`utils.c`](https://github.com/containers/bubblewrap/blob/2f55bae38468d0c50cf5df87b1e481e882b63acb/utils.c) | 1,085 | FD、路径、内存、进程与通用系统调用封装 |
| [`bind-mount.c`](https://github.com/containers/bubblewrap/blob/2f55bae38468d0c50cf5df87b1e481e882b63acb/bind-mount.c) | 607 | Bind Mount、`mountinfo` 与递归 Remount |
| [`network.c`](https://github.com/containers/bubblewrap/blob/2f55bae38468d0c50cf5df87b1e481e882b63acb/network.c) | 199 | 最小 rtnetlink 客户端与 Loopback 配置 |

## 项目定位与历史

### 从 xdg-app helper 独立出来的低层组件

仓库的第一个 Commit 由 Alexander Larsson 于 2016 年 2 月提交。README 记录了更完整的代码血缘：

```text
linux-user-chroot
       ↓
xdg-app-helper
       ↓
Bubblewrap
       ↓
Flatpak 等上层调用方
```

Bubblewrap 最初继承了桌面应用沙箱中的低层能力，随后把应用策略、桌面集成与底层隔离拆开：Bubblewrap 只负责建立沙箱，上层框架负责决定暴露哪些资源。

名字也直接表达了这个定位：`bwrap` 作为目标应用的父进程“包裹”它，并在外部形成一层保护结构，类似气泡膜 Bubble Wrap。

### 它和容器运行时有什么不同

现代 Docker/runc 已经支持 Rootless 场景，因此不能简单地把两者区别归纳为“一个非特权、一个只能 Root”。更准确的区别是抽象层次：

| 能力 | Bubblewrap | OCI Runtime / 容器引擎 |
|------|------------|-------------------------|
| 核心定位 | 单进程沙箱构造器 | 标准容器创建与生命周期 |
| Rootfs | 由 CLI 逐项 Bind/Tmpfs/Overlay 构造 | 通常来自 OCI Bundle/Image |
| Namespace | 直接通过参数组合 | 由 OCI Spec 描述 |
| Cgroup | 不负责资源管理策略 | 通常支持 |
| 网络 | 可隔离到仅 Loopback，不负责完整网络栈 | 常结合 CNI/网络驱动 |
| 镜像 | 不管理 | 通常管理或消费 |
| 生命周期 | 监管一次命令 | 创建、启动、停止、删除容器 |
| 安全策略 | 调用方通过参数定义 | Spec、Runtime 与上层平台共同定义 |

systemd-nspawn 更接近“启动一个轻量系统容器”；Bubblewrap 更接近“为一个进程临时改写世界观”。

### 两种“安全边界”不能混淆

[`SECURITY.md`](https://github.com/containers/bubblewrap/blob/2f55bae38468d0c50cf5df87b1e481e882b63acb/SECURITY.md) 区分了两个问题。

第一，Bubblewrap 不是“当前用户与操作系统之间”的权限边界。普通用户能通过 Bubblewrap 完成的 Namespace 操作，理论上也可以编写自己的程序完成；它不应让用户获得宿主上原本没有的权限。

第二，Bubblewrap 可以成为“被沙箱进程与宿主资源之间”边界的一部分，但强度取决于参数：

```text
--ro-bind /usr /usr      只读暴露程序文件
--bind "$HOME" "$HOME"   同时也把用户数据写权限暴露进去
--share-net              保留宿主网络视图
--unshare-net            使用隔离网络命名空间
```

同一个二进制既能构建强约束沙箱，也能只用来调整文件系统布局。安全属性属于“Bubblewrap 能力 + 调用参数 + Kernel + 外部接口”的组合。

## 整体架构：一条声明式沙箱编译流水线

Bubblewrap 最终只生成一个 `bwrap` 可执行文件，入口是 [`main()`](https://github.com/containers/bubblewrap/blob/2f55bae38468d0c50cf5df87b1e481e882b63acb/bubblewrap.c)。整体可以理解为一条小型编译流水线：

```text
CLI / FD Arguments
      │
      ▼
parse_args_recurse()
      │
      ▼
SetupOp / LockFile / SeccompProgram
      │
      ▼
参数组合与安全前置校验
      │
      ▼
raw_clone(CLONE_NEWNS | ...)
      │
      ├── Parent：drop privileges → monitor_child()
      │
      └── Child：User/Net Namespace 初始化
                   │
                   ▼
            resolve_symlinks_in_ops()
                   │
                   ▼
          tmpfs + pivot_root #1
                   │
                   ▼
             setup_newroot()
                   │
                   ▼
             pivot_root #2
                   │
                   ▼
        UserNS/Capability 最终收敛
                   │
                   ▼
       PID 1 Reaper → Seccomp → execvp()
```

这套流程有两个重要边界：

- **解析阶段不执行挂载。** 用户意图先变成数据结构；
- **特权操作完成后不可回头。** 旧根被卸载、Capability 被丢弃、Seccomp 被应用，后续阶段只能继续收敛。

## SetupOp：把命令行编译为挂载计划

### 数据结构

几十个 CLI 选项最终被归一为 [`SetupOp`](https://github.com/containers/bubblewrap/blob/2f55bae38468d0c50cf5df87b1e481e882b63acb/bubblewrap.c)：

```c
typedef enum {
  SETUP_BIND_MOUNT,
  SETUP_RO_BIND_MOUNT,
  SETUP_DEV_BIND_MOUNT,
  SETUP_OVERLAY_MOUNT,
  SETUP_MOUNT_PROC,
  SETUP_MOUNT_DEV,
  SETUP_MOUNT_TMPFS,
  SETUP_MAKE_DIR,
  SETUP_MAKE_FILE,
  SETUP_MAKE_SYMLINK,
  SETUP_SET_HOSTNAME,
  SETUP_CHMOD,
} SetupOpType;

struct _SetupOp {
  SetupOpType type;
  const char *source;
  const char *dest;
  int fd;
  SetupOpFlag flags;
  int perms;
  size_t size;
  SetupOp *next;
};
```

它相当于沙箱文件系统的中间表示：

```text
--ro-bind /usr /usr
--proc /proc
--dev /dev
--tmpfs /tmp
--symlink usr/lib64 /lib64
        │
        ▼
[RO_BIND] → [PROC] → [DEV] → [TMPFS] → [SYMLINK]
```

`setup_newroot()` 在新的 Mount Namespace 中顺序执行链表。操作顺序是用户可观察语义：后一个 Mount 可以覆盖前一个路径，`--chmod` 也只能作用于已经存在的目标。

### 解析与执行分离的价值

这种设计带来四个收益：

1. 参数语法错误在进入复杂 Namespace 操作前就能失败；
2. Source Path 可以统一在切根前解析；
3. 特权阶段只消费结构化数据，减少分支与字符串语义；
4. 顺序语义天然保存在链表中，不需要额外依赖图。

这与数据库中的“SQL → Logical Plan → Physical Execution”非常相似：先把声明式输入变成中间表示，再在满足前置条件的环境中执行。

### 修饰符只对下一个操作生效

例如：

```bash
bwrap \
  --perms 0600 --file 3 /run/secret \
  --size 67108864 --tmpfs /tmp \
  ...
```

解析器使用 `next_perms`、`next_size_arg` 与 `next_overlay_src_count` 保存短暂状态。合法操作消费它们后立即复位；如果下一个参数不匹配，直接报错：

```text
--perms 必须跟随创建文件的操作
--size 必须跟随 --tmpfs
--overlay-src 必须跟随 Overlay 操作
```

这避免了修饰符作用域模糊，也阻止错误配置被静默接受。

### FD 优先于路径

`--args`、`--file`、`--bind-data` 与 `--seccomp` 都可以从 FD 获取输入。FD 有两个优势：

- 调用方可以在启动前完成打开、权限校验与内容准备；
- 沙箱构建过程中不必重新按路径查找，减少文件被替换的窗口。

`--args` 允许读取 NUL 分隔参数，并以 `MAX_ARGS = 9000` 限制递归展开规模，避免恶意输入导致无限解析或整数边界问题。

## 权限模型：只在必要阶段持有必要能力

### 入口先永久禁止 Exec 提权

`acquire_privs()` 首先拒绝历史 Setuid 与意外 File Capability 配置：

```text
real_uid != effective_uid
  → 拒绝 Setuid Bubblewrap

非 Root 用户却持有 Capability
  → 拒绝旧 Setcap 配置

Root 调用
  → 读取当前 Effective Capability，
    后续仍可通过 --cap-drop 收敛
```

随后 `main()` 立即执行：

```c
prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0);
```

`NO_NEW_PRIVS` 一旦设置就不能撤销。后续 `execve()` 即使遇到 Setuid 程序或 File Capability，也不能获得新的权限；非特权 Seccomp Filter 也依赖这一前提。

### User Namespace 中的 Root 不是宿主 Root

普通用户运行时，Bubblewrap 会自动启用新的 User Namespace。典型 UID 映射是：

```text
Sandbox UID 0  ──映射──> Host UID 1000
```

沙箱内进程看起来是 Root，并拥有这个 User Namespace 管辖资源上的 Capability；但它在父 User Namespace 仍然只是 UID 1000，不能借此修改宿主 Root 拥有的资源。

关键不是“伪造一个 UID 0”，而是 Linux 对权限检查的作用域：

```text
Capability
  + User Namespace 所有权
  + 被操作资源所属 Namespace
  → 是否允许 Mount/Namespace 管理操作
```

### UID/GID Map 的写入顺序

`write_uid_gid_map()` 依次处理：

```text
uid_map
  → setgroups = deny
  → gid_map
```

在无特权写入 `gid_map` 前必须禁止 `setgroups`。老内核不存在该接口时，源码只对 `ENOENT` 做兼容，并明确指出这意味着运行在仍受 CVE-2014-8989 影响的旧内核上。

当第一层 Namespace 需要把真实用户映射成 UID 0，同时又要保留目标 Sandbox UID 映射时，代码还读取 Kernel `overflowuid/overflowgid`，避免 Namespace 外部无映射文件的属主意外与 Sandbox Root 冲突。

### 两级 User Namespace 是条件性技巧

草率概括 Bubblewrap 时，经常会说它“总是创建两级 User Namespace”，这并不准确。第二级只在以下情况之一出现：

- `--dev` 需要挂载新的 devpts，而第一层必须先把真实 UID/GID 映射成 0；
- `--disable-userns` 需要在第一层把 `max_user_namespaces` 设为 1，再进入第二层固化限制；
- 请求的最终 UID/GID 与第一层临时映射不同。

典型 devpts 路径是：

```text
Host User
   │
   ▼
UserNS #1：real_uid → 0
   │         用 Namespace Root 完成 devpts 与 Mount Setup
   ▼
UserNS #2：0 → sandbox_uid
             回到最终目标身份
```

`--disable-userns` 更进一步：

```text
UserNS #1
  → 写 user.max_user_namespaces = 1
  → 创建 UserNS #2
  → 沙箱无法回到外层提高限制
  → 主动调用 unshare(CLONE_NEWUSER)
  → 如果居然成功，则整体失败
```

它不是“写完配置就相信”，而是运行时验证安全不变量。

### Capability 的三层收敛

Bubblewrap 同时处理：

| 层次 | 作用 |
|------|------|
| Bounding Set | 决定进程及后代未来最多可能获得哪些 Capability |
| Permitted/Effective/Inheritable | 决定当前进程可用与可传递的能力 |
| Ambient Set | 让明确保留的能力跨越非特权 `execve()` |

创建新 User Namespace 后，Kernel 会重新给予该 Namespace 范围内的 Bounding 能力，因此源码会再次调用 `drop_cap_bounding_set()`。最终 `drop_privs(true)` 只保留命令行明确请求的 Capability；普通非 Root 场景默认不保留能力。

## Mount Namespace：从空白根目录开始构造世界

### 第一步：阻断反向挂载传播

Bubblewrap 总会创建新的 Mount Namespace，并先执行：

```text
mount(NULL, "/", MS_SLAVE | MS_REC)
```

Slave Mount 可以接收上游传播，但当前 Namespace 内的新挂载与卸载不会反向污染宿主挂载树。随后在 `/tmp` 挂载一个 `nodev,nosuid` 的 Tmpfs，作为搭建新根的临时工作区。

选择 `/tmp` 不是为了把它暴露给沙箱，而是因为：

- 路径预期存在；
- 不应是低权限用户可替换的符号链接；
- 完成 `pivot_root` 后不再需要按原路径访问；
- Tmpfs 会在最后一个引用退出后自动清理。

### 两次 `pivot_root`

完整过程可以画成：

```text
宿主视图
/
└── tmp

Mount Namespace 内
tmpfs@/tmp
├── newroot
└── oldroot

        pivot_root #1
               │
               ▼
/
├── oldroot   ← 原宿主根，只在 Setup 阶段可见
└── newroot   ← 根据 SetupOp 构建的新根

        setup_newroot()
               │
        oldroot 设为 private
        umount2(oldroot, MNT_DETACH)
               │
        chdir("/newroot")
        pivot_root(".", ".")
               │
               ▼
/             ← 原 /newroot
旧搭建根被再次 MNT_DETACH
```

第一次 Pivot 的价值是同时提供两套稳定前缀：

```text
/oldroot/usr   → Bind Mount Source
/newroot/usr   → Sandbox Destination
```

第二次 Pivot 将 `/newroot` 提升为真正的 `/`。`pivot_root(".", ".")` 看似违反“旧根应位于新根之下”的直觉，但 Kernel 实际检查的是旧根是否能从新根访问；runc 与 LXC 也使用同类技巧。

### Source Path 为什么提前解析

`resolve_symlinks_in_ops()` 在 Chroot/Pivot 完成前、切换回真实 UID 后调用：

1. 切根前才能按宿主语义解析绝对 Symlink；
2. 使用真实 UID 才能正常访问部分 FUSE Mount；
3. 统一解析后，Setup 阶段不需要重新解释用户路径。

对 `--bind-fd`，源码还在 Mount 后比较 Source FD 与目标的 Device/Inode，检测“解析 `/proc/self/fd/N` 到真正 Mount”之间的替换竞态。

## `setup_newroot()`：顺序执行文件系统计划

### Bind、Tmpfs 与 Overlay

`setup_newroot()` 逐个消费 SetupOp：

```text
Bind / Read-only Bind / Device Bind
Overlay / Temporary Overlay / Read-only Overlay
Procfs / Devfs / Tmpfs / Mqueue
Directory / File / Bind Data
Symlink / Chmod / Hostname
```

目标父目录会按需要创建。创建敏感文件时，如果目标权限不允许 Group/Other 访问，父目录权限也会同步收紧，避免“文件不可读但父目录意外开放”的惊讶。

Overlay Mount 参数由 `StringBuilder` 拼装，Source 路径通过 `strappend_escape_for_mount_options()` 转义逗号、反斜线等控制字符，避免路径被解释成新的 Mount Option。遇到 `ELOOP` 时还会转换成“Overlay 目录不能重叠”的领域错误。

### `/proc`：共享 PID 与隔离 PID 走不同路径

```text
新 PID Namespace
  → 挂载新的 procfs

共享 PID Namespace
  → Bind 宿主 procfs
```

无论哪条路径，源码都会检查并只读遮盖：

```text
/proc/sys
/proc/sysrq-trigger
/proc/irq
/proc/bus
```

正常非特权用户本来不应拥有危险写权限，这一层属于纵深防御。

### `/dev`：只构建最小设备视图

`--dev /dev` 不会把整个宿主 `/dev` 暴露进去，而是：

1. 挂载新的 Tmpfs；
2. 单独 Bind `null`、`zero`、`full`、`random`、`urandom`、`tty`；
3. 创建 `/dev/stdin`、`stdout`、`stderr` 等 Symlink；
4. 使用 `newinstance` 挂载独立 devpts；
5. 创建 `/dev/ptmx` 与 `/dev/shm`；
6. 仅当外部 Stdout 已是 TTY 时映射对应 `/dev/console`。

这体现了 Allowlist 思路：应用需要哪些设备，就明确加入哪些设备。

### `--bind-data`：用匿名化 Mount 承载内容

数据从 FD 复制进 `mkstemp` 临时文件，随后 Bind Mount 到目标并立即 `unlink` 临时路径：

```text
FD Data
  → Temporary File
  → Bind Mount to Destination
  → unlink(Temporary Path)
```

文件内容仍由 Mount 引用，但沙箱无法从其他路径重新找到临时文件。这种做法把内容生命周期绑定到 Mount，而不是可见文件名。

## 为什么 Bind Mount 需要六百行代码

### Bind 时传入的安全标记不会自动递归生效

Linux Bind Mount 的历史行为是：

```text
mount(src, dest, MS_BIND | MS_REC)
```

可以复制挂载关系，却不会可靠地把 `MS_RDONLY`、`MS_NOSUID`、`MS_NODEV` 应用到所有子挂载点。Bubblewrap 因此采用：

```text
1. 创建 Recursive Bind
2. 读取 /proc/self/mountinfo
3. 找到目标及所有可见子 Mount
4. 保留现有标志
5. 添加 nosuid / nodev / readonly
6. 对每个 Mount 执行 MS_BIND | MS_REMOUNT
```

默认 Bind 强制 `nosuid`，非 Device Bind 还强制 `nodev`。只读 Bind 则额外增加 `MS_RDONLY`。

### `parse_mountinfo()` 是一个小型树解析器

[`parse_mountinfo()`](https://github.com/containers/bubblewrap/blob/2f55bae38468d0c50cf5df87b1e481e882b63acb/bind-mount.c) 的步骤包括：

1. 解析 Mount ID、Parent ID、Mount Point 与 VFS Option；
2. 反转义 `/proc/self/mountinfo` 的八进制路径；
3. 用 ID 索引重建 Parent/Child/Sibling 关系；
4. 删除被其他 Mount 覆盖、实际不可见的节点；
5. 深度优先收集需要 Remount 的 Mount Table。

覆盖检测很重要。Linux 允许多个 Mount 叠在同一路径上；对已经被上层 Mount 遮住的旧节点执行安全操作既没有意义，也可能导致错误判断。

### 与 Kernel 使用同一份路径字符串

大小写不敏感文件系统可能让 Kernel 在 `mountinfo` 中记录的路径大小写不同于调用参数。Bubblewrap 不直接用用户字符串匹配，而是：

```text
open(resolved_dest, O_PATH)
  → /proc/self/fd/N
  → readlink()
  → 使用 Kernel 返回的路径匹配 mountinfo
```

这是系统编程中非常重要的习惯：遇到 Kernel 视图与用户输入可能不一致时，以 Kernel 已确认的对象身份为准。

### `--not-a-security-boundary` 的精确边界

当前 HEAD 新增了：

```text
--not-a-security-boundary
```

它不是“关闭安全检查”的总开关。当前实现只把 `BIND_FAIL_OPEN` 传入 Bind Mount；递归子 Mount 因 Automounter 等原因无法 Remount 时，可以告警并继续。

以下关键失败依然 Fail-Closed：

```text
创建 Namespace 失败
pivot_root 失败
新根构建失败
Capability Drop 失败
Seccomp 安装失败
```

这个选项面向 xdg-dbus-proxy、Steam Runtime 等“只想调整文件系统布局，并不把本次调用视为隔离边界”的场景。如果调用方确实依赖 `ro/nodev/nosuid` 建立安全边界，就不能启用它。

## 进程树：Monitor、PID 1 与目标程序

启用 PID Namespace 且未使用 `--as-pid-1` 时，典型进程关系是：

```text
Caller
  │
  ▼
monitor_child()              宿主侧无特权监管进程
  │
  └── Sandbox PID 1
          │
          └── Target Process（通常是 PID 2）
```

三个角色职责不同：

| 角色 | 职责 |
|------|------|
| Monitor | 关闭多余 FD、等待状态、向调用方传播退出码 |
| Sandbox PID 1 | 回收所有后代，持有 Lock/Sync FD |
| Target | 应用最终 Capability 与 Seccomp 后 `execvp()` |

### `child_wait_fd`：父子启动栅栏

`raw_clone()` 之后，Child 先阻塞读取 `child_wait_fd`。Parent 完成 Namespace 信息读取、权限丢弃和状态输出后，写入 Eventfd 放行 Child。

这保证了：

- Parent 与 Child 对 UID/GID Map、状态 FD 的操作顺序确定；
- Child 不会在 Parent 尚未完成安全收敛时提前进入 Setup；
- Parent 失败时 Child 不会继续运行不完整沙箱。

### `event_fd`：把 PID 2 的真实退出码交给 Monitor

Sandbox PID 1 必须继续回收其他后代，因此 Target 退出时不能立即退出。它把：

```text
exit_status + 1
```

写入 Eventfd，Monitor 读取后减一并返回。加一避免 Eventfd 的零值语义产生歧义。

Monitor 每次 `poll()` 后先读 Eventfd，再处理 SIGCHLD。因为 Target 退出后 PID 1 也可能很快退出；如果先处理 PID 1 的 SIGCHLD，就可能丢失真正的应用退出码。

### `signalfd`：把信号纳入同步 IO

Monitor 阻塞 SIGCHLD，再通过 `signalfd` 将其转换为可 Poll 的 FD：

```text
poll(signalfd, eventfd)
```

相比异步 Signal Handler，这种设计无需考虑异步信号安全函数，也能在同一事件循环中处理状态通道与子进程退出。

### 外部 PID Namespace 中的可信 PID

指定 `--pidns` 时，进入目标 PID Namespace 需要额外 Fork，Monitor 最初拿到的 PID 可能只是中间进程。Bubblewrap 使用带 `SO_PASSCRED` 的 Unix Socketpair，通过 `SCM_CREDENTIALS` 把最终 PID 交回 Monitor。

PID 不是普通消息字段，而是 Kernel 随消息附带的 Credential。Monitor 信任 Kernel 认证结果，不信任 Child 自报的数据。

### `setup_finished_pipe`：区分 Setup、Exec 与正常退出

配合 `--json-status-fd`，Pipe 用字节数编码状态：

```text
0 字节：Setup 尚未完成或状态未知
1 字节：完成 Setup，已进入 Exec 边界
2 字节：Exec 尝试失败
```

成功 `execve()` 后，写端因为 `O_CLOEXEC` 自动关闭。这个小协议让调用方能够区分“应用正常退出”与“沙箱根本没有启动成功”。

## Network Namespace：只提供 Loopback

新的 Network Namespace 初始只有 Down 状态的 `lo`。[`network.c`](https://github.com/containers/bubblewrap/blob/2f55bae38468d0c50cf5df87b1e481e882b63acb/network.c) 没有引入 libnl，而是实现最小 rtnetlink 客户端：

```text
socket(PF_NETLINK, NETLINK_ROUTE)
  → RTM_NEWADDR：配置 127.0.0.1/8
  → RTM_NEWLINK：设置 IFF_UP
  → 检查 ACK、Sequence 与 Sender PID
```

Bubblewrap 不创建 Veth、不配置 NAT，也不管理 DNS。需要联网时，调用方要么共享宿主 Network Namespace，要么在外部准备好网络环境再让进程加入。

这再次体现项目边界：提供 Namespace 机制，不扩展成完整网络管理器。

## Seccomp 与执行前的最后收敛

### Seccomp Program 通过 FD 输入

`--seccomp` 与 `--add-seccomp-fd` 从 FD 读取 Classic BPF：

```text
读取全部字节
  → 长度必须是 8 的倍数
  → 转换为 struct sock_fprog
  → 关闭原 FD
```

FD 输入避免执行临界点再次按路径打开规则文件。多个 Seccomp Program 按链表顺序依次安装。

### 应用时机尽可能靠近 `execvp`

目标进程路径中：

```text
关闭内部 FD
  → 恢复 SIGCHLD
  → 设置 PDEATHSIG
  → 设置 Ambient Capability
  → seccomp_programs_apply()
  → execvp()
```

源码明确要求 Seccomp 成为 Exec 前最后的系统操作之一，这样调用方提供的 Filter 不必允许前面复杂的 Namespace 与 Mount 系统调用。

Sandbox PID 1 也会单独安装 Seccomp，再进入 `wait()` 循环，避免 Reaper 成为未受限制的旁路进程。

### TTY 是容易遗漏的外部接口

如果沙箱与调用者共享终端，恶意进程可能通过 `TIOCSTI` 向父终端注入字符。Bubblewrap 提供：

```text
--new-session → setsid()
```

README 要求：如果 Seccomp 没有禁止 `TIOCSTI`，通用沙箱应启用 `--new-session`。CVE-2017-5226 正是“Namespace 已经隔离，但终端控制面仍然共享”的典型教训。

### 生命周期绑定

`--die-with-parent` 使用：

```c
prctl(PR_SET_PDEATHSIG, SIGKILL);
```

让 Bubblewrap 或上层调用者退出时，目标进程不会变成失控孤儿。PID 1 持有的 Lock File 与 Sync FD 也利用“进程退出自动关闭 FD”的 Kernel 生命周期完成清理。

## C 语言工程实践

### 用 Cleanup Attribute 模拟 RAII

[`utils.h`](https://github.com/containers/bubblewrap/blob/2f55bae38468d0c50cf5df87b1e481e882b63acb/utils.h) 定义：

```c
#define cleanup_free __attribute__((cleanup(cleanup_freep)))
#define cleanup_fd   __attribute__((cleanup(cleanup_fdp)))
```

使用方式：

```c
cleanup_free char *path = NULL;
cleanup_fd int fd = -1;
```

离开作用域时自动 `free` 或 `close`。`steal_pointer()` 则显式转移所有权并把原指针置空。这让大量多分支系统调用代码不必维护 `goto cleanup` 阶梯。

### 错误是类型，不只是字符串

`bind_mount()` 返回 `bind_mount_result`，同时用 `failing_path` 标记具体失败位置。上层再由 `die_with_bind_result()` 统一决定：

- 是否应该附加 `errno`；
- 是 Source Mount 失败还是 Submount Remount 失败；
- 是否允许 `BIND_FAIL_OPEN`；
- 如何形成可诊断错误。

安全工具不能把“启动失败”都压缩成一个 `EPERM`。调用方需要知道是 Kernel 不支持 User Namespace、Mount Flag 无法落实，还是文件路径不存在。

### 默认 Fail-Closed，例外必须显式

大多数系统调用失败都直接 `die()`，避免在部分完成的安全状态中继续运行。真正允许降级的场景必须同时满足：

```text
调用方明确声明不依赖安全边界
  +
失败类型被实现显式列入可忽略范围
```

这比“遇到不支持就尽量继续”的兼容性策略更适合安全基础设施。

### 编译期警告也是安全边界

[`meson.build`](https://github.com/containers/bubblewrap/blob/2f55bae38468d0c50cf5df87b1e481e882b63acb/meson.build) 开启了大量严格警告：

```text
-Werror=shadow
-Werror=strict-prototypes
-Werror=implicit-function-declaration
-Werror=overflow
-Werror=int-conversion
-Werror=format-security
-Wswitch-enum
```

对 C 系统程序而言，整数转换、格式字符串、隐式声明和枚举漏处理都会直接触及安全边界，把它们升级成构建错误比依赖人工 Review 更可靠。

## Bubblewrap 没有替调用方解决什么

### 共享 Kernel

Namespace 隔离的是资源视图，不是 Kernel。Sandbox 仍然共享：

- Linux 系统调用实现；
- Kernel Driver；
- Filesystem 与网络协议栈；
- Kernel 漏洞攻击面。

需要对不可信 Native Code 建立更强边界时，还要评估虚拟机、MicroVM、用户态 Kernel 或远程隔离执行。

### 被 Bind 进去的资源就是能力

以下对象一旦暴露，可能绕过“文件系统看起来很干净”的直觉：

| 暴露对象 | 潜在能力 |
|----------|----------|
| Docker/Containerd Socket | 控制宿主容器 |
| D-Bus Socket | 调用桌面或 systemd 服务 |
| SSH Agent Socket | 使用宿主身份签名 |
| Wayland/X11 Socket | 与桌面会话交互 |
| Writable Home/Workspace | 修改用户代码、配置与凭据 |
| `/dev` 设备 | 进入 Driver 与硬件攻击面 |
| Host Network | 访问内网与本机服务 |

所以安全策略不能只列“禁止路径”，还要把 Socket、FD、Environment、Device 和 Network 当作 Capability。

### Seccomp 策略由调用方提供

Bubblewrap 能加载 Seccomp，却不内置一套适用于所有应用的系统调用 Allowlist。编译器、浏览器、数据库、AI Agent 所需系统调用差异很大；错误的通用策略要么无法运行，要么几乎没有限制。

### 资源限制不属于核心职责

Bubblewrap 不负责完整 Cgroup 策略。CPU、Memory、PID、IO 与执行时间上限需要由 systemd、容器平台或调用方补充。

### 先写 Threat Model，再拼命令行

Bubblewrap 官方 README 对安全边界的表述非常克制：它负责构造沙箱，保护强度由调用参数决定。换句话说，一条看起来很长的 `bwrap` 命令并不自动构成威胁模型。用于 AI Agent 时，至少要先写清四类信任关系：

| 对象 | 通常是否可信 | Bubblewrap 能解决什么 | 仍需谁负责 |
|------|--------------|----------------------|------------|
| 被执行代码与依赖 | 不可信 | 限制可见文件、Namespace、Capability 与系统调用 | 调用方定义 Mount、FD、网络和 Seccomp 策略 |
| 策略编译器与启动器 | 可信计算基 | 按参数执行机制 | 产品侧防止参数注入、路径竞态和策略绕过 |
| 宿主 Kernel | 必须信任 | 共享 Kernel，不提供内核隔离 | 及时升级 Kernel；高风险代码使用 MicroVM/VM |
| 同机资源与外部服务 | 部分可信 | 可隔离部分 Namespace | Cgroup、网络代理、凭据代理、配额和审计 |

还应把“机制不可用”与“策略允许降级”分开。某些发行版可能禁用非特权 User Namespace；此时安全执行服务应拒绝任务或切换到经过设计的 VM 后端，不能悄悄退化成普通子进程。`--not-a-security-boundary` 的名字正是在提醒调用方：只有明确不依赖隔离属性的兼容性场景，才可以接受部分 Fail-Open。

最后，`--disable-userns` 也不只是一个加固小选项。如果载荷能够继续创建嵌套 User Namespace，它就获得了更大的 Kernel Namespace 攻击面。是否允许嵌套 Namespace 应进入策略，而不是由被执行程序自行决定。这里的判断与项目的 [Sandbox security 说明](https://github.com/containers/bubblewrap#sandbox-security) 一致：Bubblewrap 是低层机制，安全模型属于调用它的上层系统。

## 面向 AI Agent Sandbox 的设计启示

### 策略层与机制层分开

```text
Agent Policy
  ├── Workspace 只读/可写范围
  ├── 网络域名或完全断网
  ├── Secret 与 Environment
  ├── 可执行程序
  ├── CPU/Memory/Time
  └── 审计与用户授权
          │
          ▼
Mechanism
  ├── Bubblewrap Namespace/Mount
  ├── Seccomp
  ├── Cgroup
  ├── Proxy
  └── VM/MicroVM
```

Bubblewrap 证明了机制层可以很小；真正复杂的是把用户意图转换成正确策略。

### 使用 FD 表达已授权资源

如果调用方已经完成路径解析和权限审批，优先传递打开的 FD，而不是让沙箱启动阶段重新解析字符串路径。FD 同时表达：

- 已经选择的 Kernel Object；
- 生命周期；
- 访问模式；
- 跨进程所有权。

Bubblewrap 的 `--bind-fd`、`--args`、`--seccomp`、Eventfd 与 Credential Socket 都体现了这一思路。

### 不变量必须运行时验证

适合验证的安全不变量包括：

```text
无法再创建 User Namespace
旧根已经不可达
目标 Bind 指向预期 Inode
Capability Bounding Set 已收敛
Producer PID 来自 Kernel Credential
安全 Mount Flag 已递归落实
```

只写配置、不验证结果，会把 Kernel 版本差异、Mount 传播和竞态问题留给生产环境。

### 可观测性不能破坏安全性

`--info-fd`、`--json-status-fd` 与退出码协议提供结构化状态，但不要求在沙箱内开放额外控制 Socket。Agent Sandbox 同样应优先使用单向 FD、事件流和宿主侧审计，而不是为了调试暴露高权限服务。

## 一次典型调用的完整时序

以下命令只用于展示组成方式，并不代表适用于任意不可信程序的完整安全策略：

```bash
bwrap \
  --unshare-user \
  --unshare-pid \
  --unshare-net \
  --ro-bind /usr /usr \
  --symlink usr/lib /lib \
  --symlink usr/lib64 /lib64 \
  --proc /proc \
  --dev /dev \
  --tmpfs /tmp \
  --new-session \
  --die-with-parent \
  -- \
  /usr/bin/bash
```

源码时序可以归纳为：

```text
main()
  │
  ├── acquire_privs()
  ├── PR_SET_NO_NEW_PRIVS
  ├── parse_args() → SetupOp
  ├── block(SIGCHLD)
  ├── eventfd / pipe / socketpair
  ├── raw_clone(NEWNS | NEWUSER | NEWPID | NEWNET)
  │
  ├── Parent
  │     ├── 读取 Namespace ID
  │     ├── drop_privs(false)
  │     ├── 输出 Child PID/Namespace
  │     ├── child_wait_fd 放行
  │     └── monitor_child()
  │
  └── Child
        ├── 等待 Parent 栅栏
        ├── UID/GID Map
        ├── Loopback Setup
        ├── resolve_symlinks_in_ops()
        ├── Mount Tree → Slave
        ├── Tmpfs@/tmp
        ├── pivot_root #1
        ├── setup_newroot()
        ├── Detach oldroot
        ├── pivot_root #2
        ├── 可选 UserNS #2
        ├── 验证无法继续创建 UserNS
        ├── drop_privs(true)
        ├── setsid()
        ├── fork()
        │     ├── PID 1 → do_init()
        │     └── Target
        ├── Ambient Capability
        ├── Seccomp
        └── execvp()
```

## 如何验证这类沙箱实现

Bubblewrap 的 [`tests/test-run.sh`](https://github.com/containers/bubblewrap/blob/2f55bae38468d0c50cf5df87b1e481e882b63acb/tests/test-run.sh) 不只验证“命令能运行”，还覆盖安全不变量：

| 测试方向 | 示例 |
|----------|------|
| Namespace | User/PID/Network Namespace 创建与复用 |
| UserNS 禁用 | 沙箱内递归 `unshare` 必须失败 |
| Capability | Add/Drop 与 PID 1 能力集合 |
| 生命周期 | `--die-with-parent` |
| 状态协议 | `--info-fd`、`--json-status-fd` |
| Mount | Bind、Readonly、设备、Overlay 与路径转义 |
| 数据传递 | `--file`、`--bind-data`、FD |
| Seccomp | Allowlist、Denylist、无效 BPF |
| 兼容降级 | `--not-a-security-boundary` |

安全回归至少应验证：

```text
Positive：允许的文件、设备与系统调用可用
Negative：未授权资源确实不可达
Invariant：旧根、Capability、Namespace 不能恢复
Lifecycle：父进程退出、Exec 失败、信号退出均能清理
Race：FD/Path 替换不能切换实际 Mount Source
Compatibility：Kernel 不支持时明确失败或按策略降级
```

仅比较 `bwrap` 退出码不足以证明隔离正确，还需要在沙箱内主动尝试越界操作，并从宿主侧确认没有留下 Mount、进程和 FD。

## 总结

Bubblewrap 最值得学习的不是某个冷门系统调用，而是它对边界的拆分：

```text
调用方负责 Security Policy
Bubblewrap 负责可靠落实 Policy
Linux Kernel 提供隔离原语
Monitor 负责生命周期与状态
测试负责证明关键不变量
```

五千余行 C 代码之所以能支撑 Flatpak 等复杂上层系统，依靠的不是功能堆叠，而是持续收窄职责：

1. 用 SetupOp 把参数编译成有序计划；
2. 用 User Namespace 获取局部管理能力；
3. 用双重 `pivot_root` 建立不可回退的新根；
4. 用递归 Remount 落实 `ro/nodev/nosuid`；
5. 用 Capability 与 `NO_NEW_PRIVS` 收敛权限；
6. 用 PID 1、Eventfd 与 Signalfd 管理进程生命周期；
7. 用 Seccomp 完成 Exec 前的最后限制；
8. 用 Fail-Closed 与运行时验证守住安全不变量。

对 AI Agent Sandbox 而言，Bubblewrap 是很好的机制层样本，但不能被误解为“一条命令自动获得安全”。真正的产品级边界还需要：

```text
Bubblewrap
  + 精确 Mount/FD/Socket 策略
  + Seccomp
  + Cgroup 与超时
  + 网络代理或隔离
  + Secret 管理
  + 审计与授权
  + 必要时更强的 VM 边界
```

安全不是某个选项，而是一组可以解释、可以验证、失败时默认收敛的系统不变量。

## 关键源码阅读索引

| 主题 | 源码入口 |
|------|----------|
| 项目定位与限制 | [`README.md`](https://github.com/containers/bubblewrap/blob/2f55bae38468d0c50cf5df87b1e481e882b63acb/README.md) |
| 安全模型 | [`SECURITY.md`](https://github.com/containers/bubblewrap/blob/2f55bae38468d0c50cf5df87b1e481e882b63acb/SECURITY.md) |
| 主流程与 SetupOp | [`bubblewrap.c`](https://github.com/containers/bubblewrap/blob/2f55bae38468d0c50cf5df87b1e481e882b63acb/bubblewrap.c) |
| Bind Mount 与 Mountinfo | [`bind-mount.c`](https://github.com/containers/bubblewrap/blob/2f55bae38468d0c50cf5df87b1e481e882b63acb/bind-mount.c) |
| Netlink 与 Loopback | [`network.c`](https://github.com/containers/bubblewrap/blob/2f55bae38468d0c50cf5df87b1e481e882b63acb/network.c) |
| FD、PID 与路径工具 | [`utils.c`](https://github.com/containers/bubblewrap/blob/2f55bae38468d0c50cf5df87b1e481e882b63acb/utils.c) |
| Cleanup/Ownership 宏 | [`utils.h`](https://github.com/containers/bubblewrap/blob/2f55bae38468d0c50cf5df87b1e481e882b63acb/utils.h) |
| CLI 语义 | [`bwrap.xml`](https://github.com/containers/bubblewrap/blob/2f55bae38468d0c50cf5df87b1e481e882b63acb/bwrap.xml) |
| 构建与依赖 | [`meson.build`](https://github.com/containers/bubblewrap/blob/2f55bae38468d0c50cf5df87b1e481e882b63acb/meson.build) |
| 主回归测试 | [`tests/test-run.sh`](https://github.com/containers/bubblewrap/blob/2f55bae38468d0c50cf5df87b1e481e882b63acb/tests/test-run.sh) |
| Seccomp 测试 | [`tests/test-seccomp.py`](https://github.com/containers/bubblewrap/blob/2f55bae38468d0c50cf5df87b1e481e882b63acb/tests/test-seccomp.py) |
