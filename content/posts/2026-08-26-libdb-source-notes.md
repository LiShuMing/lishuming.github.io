---
title: "【源码】libdb：从 B-Tree 页、WAL 与 MVCC 理解嵌入式 OLTP 引擎"
slug: "libdb-source-notes"
date: 2026-08-26T00:00:00+08:00
lastmod: 2026-08-30T00:00:00+08:00
categories:
  - Database Engineering
tags:
  - Berkeley DB
  - libdb
  - OLTP
  - B-Tree
  - MVCC
  - SSI
  - WAL
  - CDC
description: "基于 libdb v5.3.34 源码，从一次 B-Tree put 出发，梳理页式行存、事务与 WAL、MVCC/SSI、并发控制、恢复复制与 CDC 边界，并对照 OLAP 系统理解两类引擎的不同取舍。"
draft: false
toc: true
math: false
---

虽然我一直在大数据和 OLAP 领域工作，也常自称 database boy，但过去对传统 OLTP 引擎的理解更多停留在术语层：行存适合点查，WAL 用来恢复，MVCC 让读写不互相阻塞。真正的问题是，这些概念在代码中究竟长什么样？一次 `put` 怎样同时碰到 B-Tree、buffer pool、锁和日志？事务提交的完成点在哪里？所谓 CDC 是否就是“读取 WAL”？

这次我选择从 [libdb](https://github.com/berkeleydb/libdb) 学习。它是 Berkeley DB 5.3.x 的社区分支，是一个链接进应用进程的嵌入式事务型 Key/Value 引擎，不是独立数据库 Server。本文基于本地 `master` 分支的 `v5.3.34`，源码提交为 [`c4811dc87`](https://github.com/berkeleydb/libdb/commit/c4811dc87)。这个边界很重要：当前分支已经把 `DB_TXN_SNAPSHOT` 改造成 Serializable Snapshot Isolation（SSI），还包含 cache cooling、B-Tree root snapshot 和异步 I/O 等社区改造，不能把所有行为都归到 Oracle 最后的 5.3.28。

我最终形成的核心认识是：

> OLTP 存储引擎不是“B-Tree 加一个 WAL”，而是一组围绕单条记录生命周期互相制约的协议：页格式决定更新粒度，锁决定谁能进入，MVCC 决定读哪个版本，WAL 决定何时能落数据页，事务提交决定何时对外承诺，恢复则证明这个承诺在进程崩溃后仍然成立。

## 1. 先确定 libdb 是什么

libdb 对外提供 `DB_ENV`、`DB`、`DBC` 和 `DB_TXN` 等 C API handle。应用自己创建线程、接收请求并调用这些 handle；一次读写通常就在调用者线程内同步完成。核心代码没有一个类似 MySQL `mysqld` 的常驻 SQL 请求调度层。

```text
application threads / processes
          │
          │ DB_ENV / DB / DBC / DB_TXN API
          ▼
┌─────────────────────────────────────────────────────┐
│                    libdb library                    │
│                                                     │
│  access methods     transaction       lock manager  │
│  Btree/Hash/Heap    begin/commit       wait/deadlock│
│  Queue/Recno             │                  │        │
│          └───────────────┼──────────────────┘        │
│                          ▼                           │
│                  mpool / MVCC cache                 │
│                          │                           │
│                     WAL / recovery                  │
│                          │                           │
│               OS file, fsync, mmap, AIO             │
└─────────────────────────────────────────────────────┘
```

`DB_ENV->open()` 的 flags 直接暴露了这组子系统的边界：`DB_INIT_MPOOL`、`DB_INIT_LOCK`、`DB_INIT_LOG`、`DB_INIT_TXN` 和 `DB_INIT_REP` 分别初始化缓存、锁、日志、事务和复制。它们不是为了代码目录整齐而拆分；事务正确性恰恰来自这些子系统按共同协议组合。

源码入口可以先记住这张表：

| 问题 | 主要源码 |
| --- | --- |
| 环境创建与恢复 | `src/env/env_open.c`、`src/env/env_recover.c` |
| 公共 KV API | `src/db/db_iface.c`、`src/db/db_am.c` |
| B-Tree 搜索、写入与分裂 | `src/btree/bt_search.c`、`bt_cursor.c`、`bt_put.c`、`bt_split.c` |
| 磁盘页格式 | `src/dbinc/db_page.h` |
| buffer pool 与 MVCC | `src/mp/mp_fget.c`、`mp_fput.c`、`mp_mvcc.c`、`src/dbinc/mp.h` |
| 锁与死锁检测 | `src/lock/lock.c`、`lock_deadlock.c`、`src/dbinc/lock.h` |
| 事务生命周期 | `src/txn/txn.c`、`txn_chkpt.c`、`txn_rec.c` |
| WAL 读写与恢复分发 | `src/log/log_put.c`、`log_get.c`、`src/db/db_dispatch.c` |
| 复制与日志补洞 | `src/rep/rep_record.c`、`rep_log.c`、`rep_util.c` |

### 1.1 三层对象：handle、shared region 与持久文件

libdb 是嵌入式 library，但“嵌入式”不等于只有一组进程内 C struct。它的运行状态可以分为三层：

```text
application process
┌─────────────────────────────────────────────────────────────────┐
│ process-local handles                                            │
│ DB_ENV -> DB -> DBC -> DB_TXN                                    │
│   │       │     │       │                                        │
│   │       │     │       └─ txnid / TXN_DETAIL offset / cursor list│
│   │       │     └───────── page/index/lock/search stack           │
│   │       └─────────────── access-method function table           │
│   └─────────────────────── subsystem handles / configuration      │
└──────────────────────────────┬──────────────────────────────────┘
                               │ region offset / shared mutex
┌──────────────────────────────▼──────────────────────────────────┐
│ environment shared regions                                      │
│ REGENV | LOCKREGION | TXNREGION | MPOOL | LOG | REP              │
│ lockers  lock objects  active txns  buffer hash/version chain    │
└──────────────────────────────┬──────────────────────────────────┘
                               │ pread/pwrite/fsync/mmap
┌──────────────────────────────▼──────────────────────────────────┐
│ persistent files                                                 │
│ database pages | log.* WAL | region/backing files | temp freezer │
└─────────────────────────────────────────────────────────────────┘
```

`DB_ENV/DB/DBC/DB_TXN` 是调用者持有的 handle；`TXN_DETAIL`、`DB_LOCKER`、buffer header 等可能位于 environment region，以 offset 而不是裸指针互相引用，从而支持多个进程 attach 同一个 environment。数据 page 与 WAL 则在进程退出后继续存在。

这一区分能避免两个常见误解：

- `DB_TXN->commit()` 销毁了进程内 transaction handle，不代表与它相关的所有 shared metadata 立刻消失；MVCC page version 或 SSI SIREAD marker 可能继续引用 `TXN_DETAIL`；
- `DB->put()` 在调用者线程执行，不代表没有跨进程并发；逻辑锁、transaction region 与 mpool region 正是在协调不同线程/进程。

### 1.2 `DB_ENV->open()` 为什么有严格初始化顺序

[`src/env/env_open.c`](https://github.com/berkeleydb/libdb/blob/c4811dc87/src/env/env_open.c) 先用 `__env_attach()` 创建或加入 environment。若是加入已有 environment，当前 handle 的 `DB_INIT_*` flags 会被底层 environment 实际配置覆盖，而不是任由进程以不一致的子系统集合 attach。

去掉错误处理后的初始化顺序如下：

```c
__env_attach(env, &init_flags, create_ok, retry_ok);
__mutex_open(env, create_ok);          /* region mutex 必须最先可用 */
__env_thread_init(env, create_ok);

if (DB_INIT_REP)   __rep_open(env);
if (DB_INIT_MPOOL) __memp_open(env, create_ok);
if (DB_INIT_LOG || DB_INIT_TXN)
                    __log_open(env);   /* transaction 隐含 logging */
if (DB_INIT_LOCK)  __lock_open(env);
if (DB_INIT_TXN) {
                    __txn_open(env);
                    __env_init_rec(env, log_version);
}
if (DB_RECOVER)    __db_apprec(...);
```

顺序不是代码风格问题：mpool 要先注册 page-in/page-out conversion；crypto 必须在 log recovery 前就绪；transaction recovery 依赖 log、mpool 与 access-method recovery dispatch table。源码还明确写着“transactions imply logging but do not imply locking”：单线程应用可以需要 atomicity/recovery 而不打开并发锁管理。

因此 environment flags 定义的是正确性模型，而不只是功能开关。打开 transaction 却漏掉所需 recovery/log 配置，或者多个进程以不同 region 配置加入同一 home，都不是可接受的降级模式。

### Query Engine 在哪里

如果把 Query Engine 理解为 parser、binder、optimizer 和 operator executor，那么它不在核心 libdb 中。核心 API 接收的已经是 key、data 和 cursor operation。可选 SQL 目录集成的是 SQLite-compatible API，但那是建立在 libdb 上方的另一层，不改变核心存储引擎的调用模型。

这反而让 libdb 很适合学习 OLTP Storage Engine：没有分布式调度和复杂 SQL plan 遮挡，一次点写怎样走到底层非常直接。

## 2. 一次 `DB->put` 如何走进 B-Tree

`src/db/db_method.c` 在创建 `DB` handle 时把 `dbp->put` 绑定到 `__db_put_pp`。调用链可以压缩成：

```text
DB->put
  -> __db_put_pp                 参数、环境、复制与 auto-commit 边界
       -> __txn_begin            没有显式事务时按配置创建本地事务
       -> __db_put
            -> __db_cursor       创建带 DB_WRITELOCK 的临时 cursor
            -> __dbc_put
                 -> __dbc_iput
                      -> dbc->am_put
                           -> __bamc_put   B-Tree access method
                      -> __bam_search
                      -> __bam_iitem
                      -> __bam_split + retry, if page is full
       -> commit / abort local transaction
```

### 2.1 public wrapper 负责的远不止参数检查

`DB` handle 在 [`__db_init()`](https://github.com/berkeleydb/libdb/blob/c4811dc87/src/db/db_method.c) 中安装 public method table：

```c
dbp->open = __db_open_pp;
dbp->get  = __db_get_pp;
dbp->put  = __db_put_pp;
dbp->del  = __db_del_pp;
```

`_pp` 可以理解为 public pre/post wrapper。[`__db_put_pp()`](https://github.com/berkeleydb/libdb/blob/c4811dc87/src/db/db_iface.c) 的等价骨架是：

```c
STRIP_AUTO_COMMIT(flags);
__db_put_arg(dbp, key, data, flags);           /* API contract */
ENV_ENTER(env, thread_info);                   /* thread/environment tracking */

__db_rep_enter(dbp, is_write=1, ...);          /* replication role/generation gate */
if (IS_DB_AUTO_COMMIT(dbp, txn)) {
    __txn_begin(env, thread_info, NULL, &txn, 0);
    txn_local = 1;
}
__db_check_txn(dbp, txn, ...);                 /* handle 与 txn 使用一致性 */
ret = __db_put(dbp, thread_info, txn, key, data, flags);

if (txn_local)
    __db_txn_auto_resolve(env, txn, 0, ret);    /* ret==0 commit，否则 abort */
__env_db_rep_exit(env);
ENV_LEAVE(env, thread_info);
```

这层定义了 API 的原子边界：auto-commit 不是 B-Tree 自己偷偷提交，而是 wrapper 创建一个正常 `DB_TXN`，底层失败码再决定 commit/abort。复制环境也在进入 access method 前阻止不允许的 write，而不是等 page 已经 dirty 后再判断角色。

[`__db_put()`](https://github.com/berkeleydb/libdb/blob/c4811dc87/src/db/db_am.c) 接下来创建带 `DB_WRITELOCK` 的 transient cursor。普通单条 put 最终调用 `__dbc_put()`；bulk、append、heap/queue/recno 则在这里分流。`__dbc_put()` 还会先维护 primary database 关联的 secondary indices，再由 `__dbc_iput()` 复制/定位 cursor，调用 access-method function pointer：

```c
/* B-Tree cursor 初始化时完成绑定。 */
dbc->am_put = __bamc_put;

/* 通用 cursor 层不知道内部是 B-Tree、Hash 还是 Heap。 */
ret = dbc_n->am_put(dbc_n, key, data, flags, &offpage_dup_pgno);
```

因此 `DB` public function table 与 `DBC` access-method table 是两级 dispatch：前者统一环境/事务语义，后者封装具体数据结构。

这个调用链有三个值得注意的地方。

第一，统一 API 通过 function pointer 分派到 access method。`DB_BTREE`、`DB_HASH`、`DB_HEAP`、`DB_QUEUE` 和 `DB_RECNO` 共用外层事务接口，但内部页组织和定位方式不同。

第二，`DB->put` 内部也借助 cursor。cursor 不只是给用户做 range scan 的便利封装，它保存 B-Tree 位置、页号、页内下标、lock、搜索栈和临时返回内存，是 access method 的执行上下文。

第三，写入不是“搜索一次然后原地塞入”。`__bamc_put()` 先通过 `__bam_search()` 找叶子，`__bam_iitem()` 返回 `DB_NEEDSPLIT` 时，代码会释放不再可靠的 pinned page 和短期锁，调用 `__bam_split()`，然后回到 `split:` 标签重新搜索、重试插入。结构修改后的旧路径不能被继续信任，这是并发 B-Tree 实现里非常具体的一条规则。

### 2.2 `__bamc_put()` 是一个可重试状态机

[`src/btree/bt_cursor.c`](https://github.com/berkeleydb/libdb/blob/c4811dc87/src/btree/bt_cursor.c) 中的核心控制流可以缩减为：

```c
split:
    __bamc_search(dbc, root_pgno, key, DB_KEYLAST, &exact);
    choose_insert_op(exact, duplicates, flags, &iiop);

    switch (__bam_iitem(dbc, key, data, iiop, 0)) {
    case 0:
        break;
    case DB_NEEDSPLIT:
        __bam_stkrel(dbc, STK_CLRDBC | STK_NOLOCK); /* 丢弃旧路径 */
        cp->pgno = PGNO_INVALID;
        __bam_split(dbc, key, &root_pgno);
        goto split;                                  /* 重新 search */
    }
```

`iiop` 不是简单 insert/replace boolean。它可能是 `DB_CURRENT/DB_BEFORE/DB_AFTER/DB_KEYFIRST`，取决于 exact match、duplicate policy、`DB_NOOVERWRITE` 与 cursor position。`__bam_iitem()` 先判断页空间、overflow/duplicate 情况；只有确认能够修改时才进入具体 page operation。

split 后必须释放 search stack 上的 page pin 和 lock，原因有两层：

1. split 会改变 parent separator 与 leaf sibling，旧 `(page, index)` 不再代表相同插入位置；
2. 若保留从 root 到 leaf 的整条 write stack 再重试，容易放大锁范围并和其他结构修改形成死锁。

`__bam_split()` 会返回一个可作为下一次搜索起点的 parent `root_pgno`，但启用 record numbering 时仍从真实 root 重走，因为沿途 subtree record count 都可能需要调整。这是 correctness 优先于 shortcut 的具体例子。

### 搜索为何区分 lock 与 latch

在 `__bam_search()` 中，逻辑 lock 与 buffer latch 同时存在，但生命周期不同：

- **lock** 保护事务语义，例如某个事务能否读写目标页，可能一直持有到 commit/abort；
- **latch/mutex** 保护内存中 `PAGE`/`BH` 结构的一次短操作，避免两个线程同时改坏字节布局；
- **pin/refcount** 保证 buffer frame 在访问期间不会被淘汰。

源码注释明确区分了“遍历内部节点的短期锁”和“数据项所在页需要随事务持有的锁”。B-Tree 向下走时还采用 lock coupling：拿到 child 的保护后才能释放 parent。把这三类机制都称为“锁”会很容易误读性能问题。

### 2.3 `__bam_search()` 同时维护路径、锁与 buffer pin

[`__bam_search()`](https://github.com/berkeleydb/libdb/blob/c4811dc87/src/btree/bt_search.c) 在每一层 page 上按 item 类型设置 binary-search stride：B-Tree leaf 的 key/data 占两个 slot，所以 `adjust=P_INDX`；internal page 每个 `BINTERNAL` 占一个 slot，所以使用 `O_INDX`。找到 internal separator 后取 child page number：

```c
adjust = TYPE(page) == P_LBTREE ? P_INDX : O_INDX;
DB_BINARY_SEARCH_FOR(base, limit, NUM_ENT(page), adjust) {
    __bam_cmp(dbc, key, page, index, compare, &cmp);
    /* 调整 base/limit */
}

child_pgno = GET_BINTERNAL(dbp, page, child_index)->pgno;
BT_STK_PUSH(env, cursor, page, child_index, lock, lock_mode, ret);
/* lock/fetch child 后再决定 parent 是否可以 release */
```

search stack 的 entry 保存 page、index、logical lock 与 lock mode；它服务于 split、delete、record-count adjustment，不只是调试信息。只读点查通常不需要保留完整 parent stack，结构修改则从某一层开始切换为 write lock 并保留路径。

当前分支还在这条函数前端加入 root-snapshot fast path：private root copy 先选 child，跳过 live root 的 pin/latch；获取 child 后再次比较 live root LSN。若 root 在窗口中发生变化，就释放 child、刷新 snapshot 并回退到真实 root：

```c
snap_child = __bam_rsnap_child(dbc, key, &snap_lsn);
start_pgno = snap_child;
__bam_get_root(dbc, start_pgno, ..., SR_SNAPSHOT, &stack);

if (LSN(live_root) != snap_lsn) {
    release_child();
    __bam_rsnap_refresh(dbc);
    start_pgno = PGNO_INVALID;
    goto retry;
}
```

这是一种“乐观选路、悲观验证”。它减少 hot root 上的共享 refcount/latch 写，却没有削弱结构正确性：任何无法证明 snapshot 仍有效的情况都退回原搜索路径。

## 3. 行存具体长什么样：一个从两端生长的 slotted page

libdb 不理解 SQL column，也没有固定 tuple schema。对核心引擎来说，key 和 data 都只是 `DBT` 中的一段 bytes；应用负责序列化“行”。但这些 bytes 落盘后不是简单连续 append，而是组织在页内。

[`src/dbinc/db_page.h`](https://github.com/berkeleydb/libdb/blob/c4811dc87/src/dbinc/db_page.h) 中的 `PAGE` 包含：

```c
typedef struct _db_page {
    DB_LSN    lsn;
    db_pgno_t pgno;
    db_pgno_t prev_pgno;
    db_pgno_t next_pgno;
    db_indx_t entries;
    db_indx_t hf_offset;
    u_int8_t  level;
    u_int8_t  type;
} PAGE;
```

页内布局是典型 slotted page：

```text
low address
┌──────────────────────────────────┐
│ PAGE: lsn, pgno, siblings, ...   │
├──────────────────────────────────┤
│ inp[0] inp[1] inp[2] ...         │  offset array grows ->
├──────────────────────────────────┤
│            free space            │
├──────────────────────────────────┤
│       item       item       item │  <- variable items grow
└──────────────────────────────────┘
high address
```

`P_INP()` 找到 offset array，`HOFFSET()` 标记高地址一侧第一个可用 byte，`P_ENTRY()` 用 offset 找到具体 item。这样移动变长记录时只需调整 offset，不需要让上层 cursor 保存裸指针。

### B-Tree leaf：key 和 value 是一对 slot

`P_LBTREE` 叶子页中 `inp[0]` 是 key、`inp[1]` 是 data，之后每两个 slot 表示下一条记录。普通短 key/value 使用 `BKEYDATA`：

```c
typedef struct _bkeydata {
    db_indx_t len;
    u_int8_t  type;
    u_int8_t  data[1];
} BKEYDATA;
```

大对象不会把一个 leaf page 撑爆，而是用 `BOVERFLOW` 保存 overflow page number 和总长度。internal page 则使用 `BINTERNAL`，保存 separator key、child page number 和可选 subtree record count。

这带来几种很传统、但在列存中不常以同样方式出现的优化：

- 点查在 internal page 对 separator key 做二分，最终只读一个 leaf page；
- 相邻 leaf 通过 `prev_pgno/next_pgno` 连接，cursor 可以顺序扫描；
- variable-length value 用 slot indirection 降低页内移动成本；
- 大 value 外置到 overflow pages，避免降低普通 leaf fanout；
- duplicate key 可以共享 key offset，或转成独立 duplicate tree；
- `__bam_ritem()` 更新 value 时只把变化的中段写进 `__bam_repl` 日志，共同 prefix/suffix 不重复记录。

`DB_HEAP` 也是行式页，但身份从 key 变成 `(pgno, indx)` 形式的 RID。`HEAPPG` 维护 offset table 与 free-space map；超大 record 可以由 `HEAPSPLITHDR` 串起多个 piece。它更接近传统 heap file，而 B-Tree 是“主数据就存放在索引叶子”的 clustered organization。

### 3.1 一次 replace 如何同时修改 WAL、page LSN 与 slot

理解 slotted page 不能只看 struct，还要看 mutation 顺序。[`__bam_iitem()`](https://github.com/berkeleydb/libdb/blob/c4811dc87/src/btree/bt_put.c) 先计算 key/data 是 inline `BKEYDATA` 还是 `BOVERFLOW`，再计算这次操作的净新增空间：

```c
bigkey  = key->size > cursor->ovflsize;
bigdata = data_size > cursor->ovflsize;
needed  = encoded_new_size - reclaimable_old_size;

if (P_FREESPACE(dbp, page) < needed)
    return DB_NEEDSPLIT;

__memp_dirty(mpf, &page, thread_info, txn, priority, 0);
```

最后一行很重要。对普通 page，它取得 exclusive mutation 权限；对 multiversion file，它还可能触发 page-level copy-on-write，返回的 `page` 指针不一定仍是传入的 buffer image。因此调用者会同步更新 cursor/search-stack 中保存的 page pointer。

以覆盖 value 的 [`__bam_ritem()`](https://github.com/berkeleydb/libdb/blob/c4811dc87/src/btree/bt_put.c) 为例，代码先寻找 old/new value 的共同 prefix/suffix，只记录变化中段：

```c
prefix = common_prefix(old_data, new_data);
suffix = common_suffix(old_data + prefix, new_data + prefix);

orig = old_data[prefix : old_len - suffix];
repl = new_data[prefix : new_len - suffix];

__bam_repl_log(dbp, txn,
    &LSN(page),                 /* 成功后写回新 page LSN */
    PGNO(page), old_page_lsn,
    index, orig, repl, prefix, suffix);

__bam_ritem_nolog(dbc, page, index, NULL, data, type);
```

`__bam_ritem_nolog()` 才真正移动 page bytes。若新 item 变长，它把高地址一侧的数据向低地址移动；若变短则反向移动，然后遍历 offset array，调整所有受影响 slot，最后更新 `HOFFSET(page)`。

这里形成了一条关键不变量：**先生成包含 old/new 差异与 old page LSN 的日志，再修改 page，并让 page header 指向新 log LSN**。日志此时可以只在 log buffer 中，不要求每次 item mutation 都 fsync；但 data page 真正刷盘前，mpool 必须确保它的 page LSN 对应 WAL 已经持久化。

### 3.2 overflow 不是另一种 value type，而是一条 page chain

当 key/value 超过 `ovflsize` 时，leaf slot 只保存 `BOVERFLOW {pgno, tlen}`。`__bam_ovput()` 分配一个或多个 overflow page，把大对象拆成链；更新/删除时 `__db_goff()`、`__db_doff()` 负责读取或回收链。

这会让一次逻辑 put 展开为多个物理动作：page allocation、overflow data、leaf pointer，必要时再加 B-Tree split。事务 undo 必须按 previous-LSN chain 逆序撤销这些动作，不能只恢复 leaf slot。也正因如此，后文的 logical CDC 无法把每条 WAL record 直接等价成一条业务 KV event。

### 行存的代价

行存并不是天然优于列存。读取一条记录需要的字段通常都在同一 value 附近，点查和小范围更新很合适；但扫描十亿行只计算一个 column 时，libdb 仍然要让应用解码每个 value，无法像 column store 那样只读所需列、使用 column encoding 和向量化批处理。

## 4. 事务不是一个对象，而是一条跨子系统协议

`__txn_begin()` 同时创建进程内的 `DB_TXN` handle 和共享 transaction region 中的 `TXN_DETAIL`。后者保存 `txnid`、`last_lsn`、`begin_lsn`、`read_lsn`、`visible_lsn`、状态、父事务以及 MVCC/SSI 引用计数。

一次更新事务的主线可以这样理解：

```text
txn_begin
   │ allocate txnid / TXN_DETAIL / locker
   ▼
search + acquire logical write lock
   ▼
fetch page from mpool (write => exclusive latch)
   ▼
write WAL record, link it through txn.last_lsn
   ▼
modify page bytes and page.lsn, mark buffer dirty
   ▼
commit record + durability policy
   ▼
publish MVCC visibility, release locks, free txn resources
```

### 4.1 `DB_TXN`、`TXN_DETAIL` 与 locker 各自负责什么

[`__txn_begin()`](https://github.com/berkeleydb/libdb/blob/c4811dc87/src/txn/txn.c) 先分配 process-local `DB_TXN`，设置 commit sync policy、isolation、parent/child list、cursor list；[`__txn_begin_int()`](https://github.com/berkeleydb/libdb/blob/c4811dc87/src/txn/txn.c) 再在 transaction region 的 system mutex 下分配 `TXN_DETAIL`：

```c
td->txnid = ++region->last_txnid;
ZERO_LSN(td->last_lsn);
ZERO_LSN(td->begin_lsn);
MAX_LSN(td->read_lsn);          /* snapshot 尚未真正开始 */
MAX_LSN(td->visible_lsn);       /* 未提交 page version 不对外可见 */
td->status = TXN_RUNNING;
atomic_init(&td->si_ref, 0);    /* SSI marker 引用数 */

SH_TAILQ_INSERT_HEAD(&region->active_txn, td, links, __txn_detail);
__lock_getlocker(lock_region, td->txnid, create=1, &txn->locker);
txn->locker->td_off = region_offset(td);
```

三者职责不同：

- `DB_TXN` 是调用者操作的生命周期 handle，拥有 children、events、cursor 与非持久 log list；
- `TXN_DETAIL` 是 shared transaction identity，保存 LSN、状态、MVCC/SSI 引用，其他进程可以通过 region offset 找到它；
- `DB_LOCKER` 是 lock manager identity，持有/等待哪些 logical lock，并通过 `td_off` 关联 SSI transaction state。

commit/abort 的清理顺序因此受引用关系约束。例如 `__txn_end()` 必须先处理 SIREAD、释放 locker，再从 active transaction list 移除/free `TXN_DETAIL`；反过来会让 lock cleanup 解引用已经释放的 detail。

### Atomicity：abort 沿事务日志链向后 undo

每条事务日志记录都通过 previous LSN 串到同一事务的上一条记录。`__txn_abort()` 调用 `__txn_undo()`，后者从 `last_lsn` 反向读取日志，再由 `__db_dispatch(..., DB_TXN_ABORT)` 分派到 access method 的 recovery function。例如 B-Tree 的 replace、split 和 page adjust 都有对应生成日志及 recover 代码。

`__txn_undo()` 的 loop 几乎就是这句话的可执行版本：

```c
key_lsn = txn->td->last_lsn;
__log_cursor(env, &logc);

while (!IS_ZERO_LSN(key_lsn)) {
    logc->get(logc, &key_lsn, &record, DB_SET);
    __txn_dispatch_undo(env, txn, &record,
        &key_lsn, txnlist);      /* 同时把 key_lsn 改成 record.prev_lsn */
}
```

`__db_dispatch()` 先从 record header 读 `rectype` 与 `txnid`，再用 `DB_DISTAB` 找到自动生成/注册的 recovery function，并把 operation mode 设成 `DB_TXN_ABORT`。access method 的 recover routine 同一份代码同时理解 REDO/UNDO，通过 page LSN 比较决定是否真正应用。

因此 abort 不是扔掉一块 private write set。非 MVCC 和部分结构修改已经可能进入共享 buffer，必须由日志把它们恢复到事务之前的状态。

### Durability：commit 的完成点由 sync policy 决定

默认事务被标记为 `TXN_SYNC`。顶层事务 commit 时，`__txn_commit()` 先关闭 cursor、处理 lock/event，再写 `__txn_regop_log(... TXN_COMMIT ...)`。日志 flags 来自事务的 durability 配置：

- `DB_TXN_SYNC`：commit record 与先前 WAL 被同步后才返回；
- `DB_TXN_WRITE_NOSYNC`：先把日志交给 OS，但不强制介质同步；
- `DB_TXN_NOSYNC`：进一步降低同步要求。

这解释了一个经常被笼统描述的点：WAL 只规定“脏数据页写回前，相应日志必须先持久化”，而客户端何时收到 commit success 还取决于 commit durability policy。两者相关，但不是同一句话。

更严格地说，三种策略对应不同的故障承诺，而不是三个纯性能档位：

| 策略 | Commit 返回前的关键要求 | 进程崩溃 | OS/机器掉电 |
| --- | --- | --- | --- |
| `DB_TXN_SYNC` | Commit WAL 完成同步 | 应可恢复已返回事务 | 取决于 OS、文件系统与设备是否兑现同步语义 |
| `DB_TXN_WRITE_NOSYNC` | WAL 已写入 OS 路径但不强制介质同步 | 通常可依赖 OS Cache | 最近事务可能丢失 |
| `DB_TXN_NOSYNC` | 允许更弱的日志刷新 | 故障窗口更大 | 不应宣称已返回事务具备同步持久性 |

因此 benchmark 不能只报告 TPS，还应说明 Sync Policy、文件系统、存储设备、是否启用写缓存以及故障注入类型。只做正常退出重启，只验证了 Close/Shutdown 路径；要验证 durability，至少需要区分进程 `SIGKILL`、Kernel/VM Power Cut 和存储设备重启，并检查已确认事务、未确认事务与 B-Tree 结构各自的恢复结果。

顶层 transaction 的成功路径可以整理为：

```c
if (txn->deadlocked) fail;
if (ssi_pivot(td)) return DB_SNAPSHOT_CONFLICT;

__txn_close_cursors(txn);
__txn_doevents(env, txn, TXN_COMMIT, preprocess=1);
__lock_vec(..., DB_LOCK_PUT_READ);              /* 普通 read lock 可先释放 */

__txn_regop_log(env, txn,
    &td->visible_lsn, LOG_FLAGS(txn), TXN_COMMIT, ...);
td->last_lsn = td->visible_lsn;

__txn_end(txn, is_commit=1);                    /* publish status/release rest */
```

commit record 的返回 LSN 同时写入 `visible_lsn`。对 MVCC page version 来说，这就是从“owner 私有”变成“read_lsn 足够新的 snapshot 可见”的时间点。若 transaction 没有任何 log record，则不必额外写空 commit record，但 `__txn_end()` 仍必须发布状态、处理 lock 和引用。

child transaction 的 commit 不等同于 durable top-level commit：它把 child log relationship 记到 parent，并把 in-memory non-durable logs 合并进 parent；parent 最终 abort 仍可逆序撤销 child。这是 nested transaction 与独立 transaction 的根本区别。

### Consistency：它一半来自引擎，一半来自应用

libdb 能保证 B-Tree 结构、页 LSN、事务原子性和隔离级别，但它不知道“余额不能为负”或“订单金额等于明细之和”。应用必须在同一 `DB_TXN` 内读取、验证并更新相关 key，或者借助 secondary database callback 维护派生索引。

### Recovery：redo committed history，undo loser transactions

环境以 `DB_RECOVER` 打开时，恢复代码读取 checkpoint 和 WAL，构造 transaction list，再通过 `__db_dispatch` 让各 access method 的 recovery routine 做 forward roll 或 backward roll。页上的 `PAGE.lsn` 用来判断某条日志是否已经体现在该页中，避免重复应用。

[`__db_apprec()`](https://github.com/berkeleydb/libdb/blob/c4811dc87/src/env/env_recover.c) 的源码注释把恢复拆成四个编号 pass（0 到 3）：

```text
Pass 0: 定位最近 checkpoint / point-in-time recovery 起点
Pass 1: 从起点向前扫，打开文件并收集 transaction 状态
Pass 2: 向后扫，UNDO 未完成或 recovery target 之后的 transaction
Pass 3: 再向前扫，REDO 已提交 transaction
```

恢复不是简单的“从 checkpoint 全部 redo”。首先要建立 fileid→database handle 与 transaction winner/loser 集合；backward pass 恢复 loser 的 before state；forward pass 才把 winner 的结果补齐。`__db_dispatch()` 根据当前 pass 传入 `DB_TXN_OPENFILES/BACKWARD_ROLL/FORWARD_ROLL` 等 mode，决定哪些 record 应调用 recovery function。

典型 recovery routine 会比较三个 LSN：当前日志 `lsnp`、page header LSN、record 中保存的 previous page LSN。简化判断是：

```c
if (REDO && page_lsn == record.prev_page_lsn) {
    apply_after_image_or_operation();
    page_lsn = record_lsn;
}
if (UNDO && page_lsn == record_lsn) {
    apply_before_image_or_inverse_operation();
    page_lsn = record.prev_page_lsn;
}
```

这让 recovery idempotent：重复运行不会把同一 change 应用两次，也不会 undo 一个 page 上已经不存在的版本。

可以把恢复理解为 ACID 的最终验收：如果 commit 已经对应用返回，崩溃后必须能 redo；如果事务没有完成，崩溃后必须能 undo。

## 5. MVCC 在 libdb 中是“页版本链”，不是行上的 begin/end timestamp

我过去接触较多的是把版本信息放在 row/segment 上的系统，因此 libdb 的 MVCC 实现很值得注意：它的主要版本单位是 buffer pool 中的 **page image**。

使用 snapshot read 需要两个开关：数据库以 `DB_MULTIVERSION` 打开，事务以 `DB_TXN_SNAPSHOT` 开始。当前社区分支中后者总是 SSI，不再提供 public plain-SI mode。

### 5.1 snapshot 不是在 `txn_begin()` 时立即拍下来的

snapshot transaction 第一次访问 multiversion page 时，[`__memp_fget()`](https://github.com/berkeleydb/libdb/blob/c4811dc87/src/mp/mp_fget.c) 才通过 `__log_current_lsn_int()` 初始化 `TXN_DETAIL.read_lsn`。这是 **lazy snapshot acquisition**，而不是在 `__txn_begin()` 中立即记录时间点。nested transaction 还会先向上找到 ultimate parent，共享同一 snapshot：

```c
/* src/mp/mp_fget.c，保留关键分支后的等价骨架。 */
if (mvcc && txn != NULL && txn->td != NULL) {
    while (txn->parent != NULL)
        txn = txn->parent;                  /* child 与 parent 共用可见性 */

    td = txn->td;
    if (F_ISSET(txn, TXN_SNAPSHOT)) {
        read_lsnp = &td->read_lsn;
        if (IS_MAX_LSN(*read_lsnp))
            __log_current_lsn_int(env, read_lsnp, NULL, NULL);
    }
}
```

这意味着“事务开始时间”和“snapshot 确立时间”可能不同。若事务 begin 后先做与 multiversion page 无关的工作，真正可见边界要到第一次相关 page access 才确定。

buffer header `BH` 保存：

- `td_off`：创建该版本的 transaction detail；
- `vc`：同一 `(file, pgno)` 的 version chain；
- owner transaction 的 `visible_lsn`：这个版本何时对其他 snapshot 可见。

读页时，hash bucket 先定位同一 `(file, pgno)` 的最新 `BH`，再沿 version chain 向旧版本走，直到满足：

```text
BH_OWNED_BY(version, txn)
    OR BH_VISIBLE(version, read_lsn)

BH_VISIBLE := version has no owner
           OR owner.visible_lsn <= transaction.read_lsn
```

[`src/dbinc/mp.h`](https://github.com/berkeleydb/libdb/blob/c4811dc87/src/dbinc/mp.h) 中的 `BH_VISIBLE` 会分两次读取 `visible_lsn.file/offset`。如果它恰好和 commit 并发，只读到一半的 LSN 会被视为“未来”，因此宁可暂时不可见，也不会错误读到尚未进入 snapshot 的版本。这是一个很小但非常关键的并发不变量。

### 5.2 写入为何是整页 copy-on-write

写事务 dirty 一个不属于自己的 multiversion buffer 时，`makecopy` 被置为 true。代码分配新 `BH`，复制完整 page image，用 `__memp_bh_settxn()` 记录 owner，然后把新版本插到 version chain 的 newest 端：

```c
makecopy = mvcc && dirty && !BH_OWNED_BY(env, bhp, txn);

if (makecopy) {
    alloc_bhp->pgno = bhp->pgno;
    alloc_bhp->mf_offset = bhp->mf_offset;
    __memp_bh_settxn(dbmp, mfp, alloc_bhp, td); /* td_off -> TXN_DETAIL */
    memcpy(alloc_bhp->buf, bhp->buf, mfp->pagesize);

    alloc_bhp->flags = BH_EXCLUSIVE | inherited_dirty_flags;
    SH_CHAIN_INSERT_AFTER(bhp, alloc_bhp, vc, __bh);
    bhp = alloc_bhp;                           /* writer 修改自己的版本 */
}
```

这就是 page-level copy-on-write。它的优势是 B-Tree page operation 不需要理解 row timestamp，拿到的仍是一张普通 `PAGE *`；代价是只改一个很小的 value 也可能复制整个 page，而且 hot page 上的并发 writer 会快速拉长版本链。

```text
oldest                                             newest
BH(T1, visible=L20) -> BH(T2, visible=L80) -> BH(T3, visible=∞)
                              ▲
                              └── snapshot read_lsn=L100 sees T2
```

commit 把 owner 的 `visible_lsn` 设置为 commit LSN；旧 snapshot 继续读旧版本，新事务可以读新版本。`BH_OBSOLETE` 用 oldest active snapshot 判断一个历史版本是否还有读者可能看见。

当 cache 需要空间、历史版本却还不能回收时，[`__memp_bh_freeze()`](https://github.com/berkeleydb/libdb/blob/c4811dc87/src/mp/mp_mvcc.c) 会把 page bytes 写入 `__db.freezer.<cache>.<bucket>.<pagesize>` 临时文件，只在内存保留 `BH_FROZEN` header。以后需要这个版本时再 thaw。于是长 snapshot 的成本是一条完整链路：

```text
old snapshot
  -> old BH cannot become obsolete
  -> buffer cache cannot reclaim its page image
  -> freeze page to temporary file under pressure
  -> later read may trigger thaw I/O
```

这不只是“占一点 transaction metadata”，而是会消耗 buffer header、临时磁盘与额外 I/O；应用层必须限制长事务，而不是期待 mpool 无限吸收版本。

### 5.3 MVCC 解决了什么，没有解决什么

page version chain 让 reader 不必用普通 read lock 阻塞 writer，并给同一事务稳定 snapshot。但 plain snapshot isolation 仍允许 write skew：两个事务读到相同旧状态，随后更新不同 key，彼此没有 write-write conflict，却共同破坏约束。

当前 fork 在 MVCC 上叠加了 SSI。

## 6. SSI：用 SIREAD 与 rw-antidependency 捕获 write skew

当前 [`RFC 0003`](https://github.com/berkeleydb/libdb/blob/c4811dc87/rfc/0003-ssi-serializable-snapshot-isolation.md) 实现的是 Cahill 的 Serializable Snapshot Isolation。核心不是给 snapshot read 重新加会阻塞 writer 的普通读锁，而是记录“我读过这里”的 `DB_LOCK_SIREAD` marker。

源码中有两条 conflict detection path：

1. **lock-table path**：writer 获取写锁时遇到另一个 snapshot transaction 留下的 SIREAD marker；
2. **MVCC version-chain path**：`mp_fget` 给 reader 返回旧版本，并发现它跳过了 concurrent writer 的新版本。

第一条路径发生在 [`__lock_get_internal()`](https://github.com/berkeleydb/libdb/blob/c4811dc87/src/lock/lock.c)。每个 `DB_LOCKOBJ` 除了 `holders`、`waiters`，还有一条不会阻塞 writer 的 `sireaders` list。writer 获取 `DB_LOCK_WRITE` 时扫描 marker；跳过自己的 marker，对仍在运行或在 writer snapshot 之后才提交的 reader 记录 `R --rw--> W`：

```c
for (siread = obj->sireaders; siread != NULL; siread = next) {
    if (writer == siread->holder)
        remove_own_siread();
    else if (reader_is_still_concurrent(siread, writer)) {
        TXN_SYSTEM_LOCK(env);                 /* 与 commit pivot check 串行化 */
        F_SET(writer_td, TXN_DTL_WCONF);      /* W 是这条 edge 的写入端 */
        F_SET(reader_td, TXN_DTL_RCONF);      /* R 是这条 edge 的读取端 */
        TXN_SYSTEM_UNLOCK(env);
    }
}
```

第二条路径发生在 [`__memp_si_rwconflict()`](https://github.com/berkeleydb/libdb/blob/c4811dc87/src/mp/mp_fget.c)。它不是重新检查 lock table，而是从已经选中的可见 `BH` 向 newer 方向遍历：若 newer owner 仍在运行，或在当前 `read_lsn` 之后提交，就说明 reader 为保持 snapshot 跳过了 concurrent write，同样建立一条 rw-antidependency。

发生 `R ->rw W` 时，lock/transaction layer 在相关 `TXN_DETAIL` 上设置：

- `TXN_DTL_WCONF`：处于 rw-conflict 的写入端；
- `TXN_DTL_RCONF`：处于 rw-conflict 的读取端。

如果某事务同时成为一条 rw edge 的读端和另一条 edge 的写端，它就是 dangerous structure 的 pivot。`__txn_commit()` 在 transaction-system mutex 下原子检查两个 flag，命中时返回 `DB_SNAPSHOT_CONFLICT`，应用必须 abort 并 retry。

```text
T1 --rw--> T2 --rw--> T3
              ▲
              └── T2 is the pivot; cannot safely commit
```

SIREAD marker 不能在 reader commit 时立即消失，因为 concurrent writer 可能稍后才抵达同一对象。于是 `si_ref` 会让已经提交的 reader detail 在一段时间内继续存活，GC 再根据最老 snapshot 清理 marker。这里展示了 MVCC 中很容易被忽略的一类问题：**正确性 metadata 的生命周期可能长于事务 handle 本身。**

这里的 mutex 范围也值得注意：源码只在读写两个 conflict flag 和 transaction status 时短暂拿 `TXN_SYSTEM_LOCK`，不会跨 object list 修改或 lock free。否则 lock partition mutex 与 transaction-region mutex 很容易形成新的锁序问题。SSI 不是额外加两个 boolean 就结束，它本身也必须满足并发原子性。

当前实现还有清楚的限制：conflict tracking 是 page granularity，热点页会产生 false positive 和更高 abort rate；SSI transaction 不能进入 `prepare()`/2PC；HA/replication qualification 仍在推进。因此“支持 serializable”不等于所有负载下都已经低成本。

### 6.1 普通锁管理：兼容矩阵、等待公平性与死锁图

SSI 的 SIREAD 不应和普通 logical lock 混在一起理解。[`DB_LOCKOBJ`](https://github.com/berkeleydb/libdb/blob/c4811dc87/src/dbinc/lock.h) 的三条队列代表三种不同状态：

```c
typedef struct __db_lockobj {
    SH_DBT lockobj;       /* page/record 等被锁对象的共享内存标识 */
    ... holders;          /* 已获得普通锁 */
    ... waiters;          /* 因不兼容而休眠的普通锁 */
    ... sireaders;        /* 只记录 SSI 读依赖，不阻塞 writer */
} DB_LOCKOBJ;
```

`__lock_get_internal()` 用 `CONFLICTS(...)` 查询 lock-mode compatibility matrix。一个新请求不仅要和 `holders` 兼容，默认还不能越过队列中更早的 conflicting waiter。否则不断到来的 reader 都能绕过 waiting writer，writer 会永久饥饿：

```text
request
  -> conflict with holder?  yes -> enqueue waiter
  -> no holder conflict
       -> conflict with earlier waiter? yes -> enqueue behind it
       -> no                         -> grant and append to holders
```

出现等待并不等于已经死锁。真正的 cycle 由 [`__lock_detect()`](https://github.com/berkeleydb/libdb/blob/c4811dc87/src/lock/lock_deadlock.c) 周期性或在配置要求时检测：

1. 只遍历 `LOCKREGION.dd_objs`，因为没有 waiter 的 object 不可能产生 waits-for edge；
2. 为 active lockers 编号，构造 `locker × locker` bitmap；
3. 对每个 object 汇总 holder bitmap，再 OR 到每个 waiter 对应的 row，得到 `waiter -> holders`；
4. 搜索 cycle，并按 `DB_LOCK_OLDEST/YOUNGEST/MINWRITE/MAXWRITE` 等策略选择 victim；
5. 将 victim 的 waiting lock 标为 abort，唤醒线程，由事务层返回可重试错误。

所以 storage engine 只负责打破环，不会替应用重放业务逻辑。应用收到 `DB_LOCK_DEADLOCK` 后必须 abort 整个 transaction，再以有界退避重试；只重试最后一个 `put` 会破坏原事务语义。

## 7. 并发场景分别要求 Query Engine 与 Storage Engine 做什么

libdb 没有核心 SQL Query Engine，但从它的调用边界反而能更清楚地区分两层责任。

### Query/Application 层必须承担的责任

1. **确定事务边界。** 哪些读写必须一起 commit，哪些失败码需要整体 retry；
2. **尽早缩小访问集合。** key lookup 与短 cursor range 可以减少持锁时间，长扫描会放大版本保留与 conflict；
3. **稳定访问顺序。** 多 key 更新尽可能按一致顺序获得资源，降低 deadlock 概率；
4. **处理可重试错误。** `DB_LOCK_DEADLOCK`、`DB_LOCK_NOTGRANTED`、`DB_SNAPSHOT_CONFLICT` 不是普通 fatal error；
5. **选择隔离与 durability。** read committed、snapshot/SSI、sync 或 nosync 都是业务语义，而不只是性能开关；
6. **做 admission control。** 嵌入式库不会替应用限制无限并发、超长事务或无限 retry storm。

完整 SQL 引擎还会利用 optimizer 选择 index/range、在 executor 中避免锁内做昂贵表达式，并维护 operator cancel/timeout。但 libdb 只看得到最终 key/cursor 操作，因此这些决策必须在上层完成。

### Storage Engine 必须承担的责任

1. **逻辑 lock 与物理 latch 分离**，避免事务长期持有 buffer mutex；
2. **定义 lock compatibility 与等待队列**，支持 NOWAIT、timeout 和 locker priority；
3. **发现环形等待。** `lock_deadlock.c` 构造 waits-for bitmap，找到 cycle 后按 oldest/youngest/minwrite 等策略选择 victim；
4. **控制共享热点。** lock table 与 mpool hash bucket 做 partition，但 root、hot leaf、buffer refcount 仍可能成为 cache-line 竞争点；
5. **保证结构修改可重试。** split 后释放旧 stack、重新 search，而不是持有整棵树的全局锁；
6. **提供 crash-safe 顺序。** page flush 前先 flush page LSN 对应 WAL；
7. **管理版本与背压。** 长 snapshot 导致 version chain 和 freezer I/O 增长时，不能假设内存无限。

当前 fork 的 B-Tree root snapshot 很能说明并发优化的方向：普通只读 lookup 可以从 `DB` handle 的 private root copy 选出第一个 child，并用 live root LSN 做前后验证，减少所有线程对 live root 的 pin/latch/refcount 写竞争；不满足条件或验证失败就回退到原路径。这是一种保守的 optimistic read，而不是把整个 B-Tree 改成 lock-free。

## 8. WAL、复制与 CDC：三者不能画等号

libdb 提供 `DB_ENV->log_cursor()`，返回的 `DB_LOGC->get()` 可以用 `DB_FIRST`、`DB_NEXT`、`DB_SET` 等方式按 LSN 遍历日志。复制层也确实以 log stream 为核心：master 发送 `REP_LOG`，client 的 `__rep_apply()` 检查 `ready_lsn`、处理 gap、暂存乱序记录并推进日志。

这很像 CDC 的底座，但还不是现代意义上开箱即用的 logical CDC。

### 8.1 一条 WAL record 如何被序列化

WAL record 由 access method 定义。例如：

- `__db_addrem` 记录 page、index、header 和 item bytes；
- `__bam_repl` 记录 value 变化部分及公共 prefix/suffix；
- `__bam_split` 记录左右页、parent entry 和 page image；
- `__txn_regop` 表示 transaction commit/abort；
- page alloc/free、overflow、cursor adjust 也各有 record type。

这些不同 record 最终都进入 [`__log_put_record_int()`](https://github.com/berkeleydb/libdb/blob/c4811dc87/src/log/log_put.c)。自动生成的 log function 传入一张 `DB_LOG_RECSPEC`，通用代码按 spec 依次编码字段。每条事务日志共享固定的前三项：

```text
┌──────────┬──────────┬──────────────────┬─────────────────────────┐
│ rectype  │ txnid    │ prev_lsn         │ access-method fields... │
│ u32      │ u32      │ DB_LSN           │ fileid/pgno/DBT/LSN/... │
└──────────┴──────────┴──────────────────┴─────────────────────────┘
```

源码骨架能直接看出 transaction chain 和 page chain 如何同时建立：

```c
DB_SET_TXN_LSNP(txnp, &record_lsnp, &prev_lsnp);

LOGCOPY_32(env, bp, &rectype);
LOGCOPY_32(env, bp, &txnp->txnid);
LOGCOPY_FROMLSN(env, bp, prev_lsnp);      /* 同一 txn 的上一条 WAL */

for (field = spec; field->type != LOGREC_Done; ++field) {
    encode_fileid_pgno_dbt_or_page_lsn(bp, field);
}

__log_put(env, record_lsnp, &logrec, flags | DB_LOG_NOCOPY);
*prev_lsnp = *record_lsnp;                /* 推进 txn->last_lsn */
*ret_lsnp = *record_lsnp;                 /* caller 可回填 PAGE.lsn */
```

`LOGREC_POINTER` 还会在编码 page previous LSN 前检查它没有指向 log 的未来；access-method caller 成功写 log 后再把返回 LSN 写回 `PAGE.lsn`。于是同一条 record 同时参与两种顺序：`prev_lsn` 串成 transaction undo chain，page LSN 参与 WAL-before-data 与 recovery idempotence。

如果 database/transaction 配置为 non-durable，record 可能不立刻进入持久 log，而是挂在 `txnp->logs`，并把返回 LSN 标为 `NOT_LOGGED`。这不是“仍然 durable 但少一次 fsync”，而是另一条明确的故障语义。

WAL 首先是一组让 recovery routine 能 redo/undo **物理页与访问方法操作** 的协议。一个业务 `put` 可能产生 data replace、overflow page、split 和 metadata update 多条日志；反过来，一条 split log 也不代表一条业务 row change。

### 8.2 物理复制如何处理乱序与缺口

复制接收路径不是收到 `REP_LOG` 就直接 append：

```text
REP_LOG / REP_LOG_MORE
  -> __rep_process_message_int()
       -> __rep_log()
            -> __rep_apply()
                 ├─ incoming_lsn == ready_lsn : apply expected record
                 ├─ incoming_lsn  > ready_lsn : queue in __db.rep.db
                 └─ incoming_lsn  < ready_lsn : duplicate, ignore
```

[`__rep_apply()`](https://github.com/berkeleydb/libdb/blob/c4811dc87/src/rep/rep_record.c) 维护两个容易混淆的 LSN：

- `ready_lsn` 是 client **下一条期望收到并落入本地 log** 的位置；
- `waiting_lsn` 是临时队列里最早的未来 record，说明 `[ready_lsn, waiting_lsn)` 仍有 gap。

等于 `ready_lsn` 的 record 经 `__rep_process_rec()` 处理后会推进 expected position。如果推进后恰好抵达 `waiting_lsn`，代码反复调用 `__rep_remfirst()` 从临时 `__db.rep.db` 取出后续 record，直到队列断开或清空。高于 `ready_lsn` 的 record 用自身 control header 作 key、以 `DB_NOOVERWRITE` 放入临时 DB，并由 `__rep_loggap_req()` 请求缺失区间；低于它的 record 计为 duplicate。

```text
ready=L40, receive L60  -> queue L60, waiting=L60, request [L40,L60)
receive L40             -> apply, ready advances
receive missing records -> ready reaches L60
                         -> drain queued L60 and later contiguous records
```

这说明物理复制依赖的是 **连续 WAL prefix**，不是“消息大致有序即可”。只有 gap 闭合后，乱序到达的 permanent record 才能成为本地已经处理的 durable prefix。它保护的是日志/页语义一致，而不是业务事件 schema。

### 8.3 如果要在它上面构建 CDC

一个可靠 consumer 至少需要：

```text
DB_LOGC from durable LSN
  -> verify log version / checksum
  -> decode record type
  -> map fileid to database identity
  -> group records by txnid and prev-LSN chain
  -> wait for TXN_COMMIT; discard/undo aborted txn
  -> reconstruct logical key/value change when possible
  -> persist consumer checkpoint LSN
  -> coordinate log_archive retention with the slowest consumer
```

这里最难的不是顺序读日志，而是建立稳定 logical contract：schema/serialization 在应用手里，page split 等结构日志要过滤，partial replace 要重建 before/after，大对象可能跨 overflow pages，版本升级还会改变 log record format。

因此更准确的结论是：

- **物理复制**直接消费 WAL，目标是重建相同存储状态；
- **逻辑 CDC**需要把 WAL/access-method operation 翻译成稳定业务事件；
- libdb 提供 log cursor 和 record dispatch 这类原语，但没有 Debezium/PostgreSQL logical decoding 那样完整的 slot、schema、logical event 与 consumer retention 协议。

对于新应用，如果强依赖业务级 CDC，我会优先考虑在同一 transaction 中写 outbox database，由应用按 key/value contract 消费；或者在调用 `DB->put/del` 的上层生成事件。直接解码物理 WAL 更适合复制、审计工具或对 libdb log format 有强控制的系统。

## 9. 从 OLAP/BigData 视角看，哪些能力以前不够显眼

下面的比较不是说所有 OLAP 都没有这些能力，而是说 analytical-first 系统通常不会把它们放在最核心、最频繁的单行路径上。

| OLTP/libdb 中的一等问题 | 许多 OLAP/BigData 系统的常见取舍 |
| --- | --- |
| 单条 key update 立即可见并可回滚 | 以 immutable file、batch/mini-batch publish 或 partition overwrite 为主 |
| 每个事务有 isolation、lock set、undo chain | 更关注 task retry、snapshot publish 和 job-level atomicity |
| lock wait、deadlock detection、victim retry | 通过单写、分区 ownership、乐观提交或 coarse metadata lock 避免细粒度等待图 |
| commit 可选择 sync/nosync durability | 写入通常跨网络、对象存储和副本，完成点由分布式 commit/publish 定义 |
| crash recovery 需要 redo winner、undo loser | immutable data 常只需选择已提交 manifest/version，较少原地 undo 数据页 |
| 长 cursor 与 concurrent update 共享同一页 | scan 多为 snapshot，写入生成新 file/segment，旧版本由 GC 回收 |
| XA/2PC、nested transaction、per-record lock | 更常见跨 stage DAG、checkpoint、exactly-once sink 或 catalog transaction |
| B-Tree leaf split 是前台正确性路径 | compaction/merge 多在后台批量重写文件，关注 write amplification 与 backlog |

libdb 让我看到，传统 OLTP 所谓“低延迟”并不只是 page cache hit。它要求在一次很短的 API 调用里完成 lock、WAL serialization、page mutation、可能的 split、commit flush 和错误分类，同时还要允许其他线程继续推进。

### 反过来，libdb 核心缺少哪些 OLAP 能力

libdb 也不应该被想象成一个小型通用数据库。核心层没有：

- SQL parser、cost-based optimizer 与 statistics-driven plan；
- vectorized/compiled expression execution；
- column projection、dictionary/RLE/bit-pack 等列式编码；
- zone map、partition pruning、runtime filter；
- distributed shuffle、exchange、elastic worker 与 fault-tolerant stage；
- 面向超大 scan 的 morsel scheduling、pipeline parallelism；
- LSM/SSTable compaction 是当前 RFC 中的 prospective design，不是现有主存储格式。

所以正确的比较不是“行存比列存快”或“嵌入式比 Server 简单”，而是两类系统优化了不同的单位：libdb 优化一条 key/value 与一个事务的完成路径，OLAP 系统优化一个 column batch、一个 fragment 或一个 distributed query 的总吞吐。

## 10. 当前源码中的性能方向

这个社区分支不只是在保存历史版本。源码和 RFC 能看到三条明确方向：

1. **减少 hot read path 的共享写。** cache cooling 避免每次访问都更新全局 LRU，root snapshot 减少 live root 的 pin/latch；
2. **把阻塞 I/O 从前台挪走。** `src/os/os_aio*.c` 已提供 io_uring、POSIX AIO、kqueue 和 IOCP 相关 backend，buffer pool 侧保留 WAL-before-data 的约束；
3. **探索新的 write path。** adaptive LSM 仍是 Draft RFC，不能当成已经落地的 access method。

我在本机做了最小构建验证：

```bash
cd build_unix
../dist/configure --disable-shared --disable-replication
make -j4 LIBS='-lrt -lpthread'
```

静态核心库和 `db_archive`、`db_printlog`、`db_recover`、`db_verify` 等工具构建成功。直接执行 `make -j4` 时，configure 虽然探测到 POSIX AIO 位于 `librt`，生成的 link command 却没有带 `-lrt`，导致 `aio_read/aio_write/aio_return` unresolved；显式传入 `LIBS` 后通过。这个结果只证明当前 Linux/GCC 环境下 core build 可完成，不等于 SSI、复制和并发回归套件已经全部验证。

## 11. 如何按架构路径继续读源码

如果从 `src/` 目录名逐个看，很容易得到许多局部知识，却不知道正确性在哪里闭环。我更推荐按下面六条路径阅读，每条都以一个可观察问题结束。

### 路径一：先走通一条非分裂写入

```text
src/db/db_method.c::__db_init
  -> src/db/db_iface.c::__db_put_pp
  -> src/db/db_am.c::__db_put / __dbc_iput
  -> src/btree/bt_cursor.c::__bamc_put
  -> src/btree/bt_search.c::__bam_search
  -> src/btree/bt_put.c::__bam_iitem
  -> src/db/db_rec.c / src/btree/bt_rec.c
```

阅读目标是回答：public contract 在哪里结束，B-Tree dispatch 在哪里开始；cursor 保存了什么；哪一步先 log、哪一步再改 page、哪一步更新 `PAGE.lsn`。

### 路径二：故意让 leaf 空间不足

从 `__bam_iitem()` 的 `DB_NEEDSPLIT` 返回，继续读 `__bam_split()`、search stack release 和 retry label。对照 `src/dbinc/db_page.h` 的 `NUM_ENT/P_INP/P_ENTRY` 宏，回答 split 后为什么不能继续使用旧 `(pgno, index)`，parent separator 和 sibling link 各由哪条日志保护。

### 路径三：让两个事务争用同一对象

```text
__db_lget
  -> __lock_get
  -> __lock_get_internal
       -> holders / waiters / CONFLICTS
       -> mutex wait / timeout
  -> __lock_detect
       -> dd_objs / waits-for bitmap / victim
```

这条路径要把“等待”和“死锁”分开：前者是 compatibility 的正常结果，后者是 waits-for graph 中的 cycle。再对照 `DB_LOCK_NOTGRANTED` 与 `DB_LOCK_DEADLOCK` 的返回位置，确认应用应在哪里 abort/retry。

### 路径四：让 snapshot 跳过一个新版本

从 `__memp_fget()` 设置 `read_lsn` 开始，跟 `BH_VISIBLE` 选择版本、`makecopy` 创建新 `BH`、`__memp_si_rwconflict()` 建 edge，最后到 `__txn_commit()` 的 pivot check。这条路径把 MVCC、SSI 和 transaction region 串成一个整体，比只读 RFC 更容易理解。

### 路径五：从一条 log 同时向两个方向走

向前看 `__log_put_record_int()` 如何编码 record、推进 transaction LSN、回填 page LSN；向后看 `__txn_undo()` 和 `__db_dispatch()` 如何按 `prev_lsn` 找 recovery function。然后从 `__db_apprec()` 看相同 recovery routine 如何在 backward/forward pass 中分别 UNDO/REDO。

### 路径六：把本地 WAL 换成网络乱序 WAL

从 `__rep_process_message_int()` 进入 `__rep_apply()`，分别模拟 `incoming < ready`、`== ready`、`> ready` 三个分支。读完应该能解释：复制为什么要临时 DB、何时更新 `waiting_lsn`、gap 闭合后怎样 drain，以及为什么这些机制仍不等于 logical CDC。

### 贯穿所有路径的十条不变量

最后不要只记函数名，可以用下面这些问题检查是否真正读懂：

1. **handle 不能越过 environment/transaction 边界乱用。** public wrapper 在进入 access method 前验证它们的一致性；
2. **没有 pin 就不能继续解引用 page，没有 latch 就不能改 buffer bytes。** logical lock 不能替代二者；
3. **B-Tree 结构变化后旧 search path 失效。** split 必须 release stack 并重新搜索；
4. **data page 不能先于其 page LSN 对应 WAL 持久化。** 这是 WAL-before-data，而不是 commit policy；
5. **transaction undo chain 必须严格向前一条 LSN 收敛。** 否则 abort/recovery 无法终止或会漏 undo；
6. **REDO 只从 expected previous page LSN 前进，UNDO 只从 current record LSN 后退。** 因此 recovery 可重复执行；
7. **snapshot 只能看到 owner 已提交且 `visible_lsn <= read_lsn` 的版本，或自己的版本。** 并发读取 LSN 时宁可保守不可见；
8. **MVCC version 只有在 oldest snapshot 不再可能看见时才可回收。** cache pressure 不能突破可见性；
9. **SSI conflict flag 的写入和 commit pivot 判断必须原子排序。** 否则真正的 dangerous structure 可能漏检；
10. **replica 只能推进连续的 WAL prefix。** future record 可以缓存，不能跳过 gap 宣称已应用。

这些不变量把目录之间的关系压缩成了系统设计：B-Tree 维护结构，mpool 维护 page identity/version，lock manager 维护等待关系，txn 维护生命周期，log/recovery 维护崩溃前后等价，replication 再把同一套 WAL 顺序延伸到另一台机器。

## 12. 这次源码学习后的理解

沿着 `DB->put` 向下读，比按目录逐个背模块有效得多。一个 API 把我带过了 cursor、B-Tree search、slotted page、overflow、lock/latch、WAL、commit 和 recovery，再从 snapshot read 延伸到 page version chain 与 SSI。

最终我会用下面几句话概括 libdb：

- 它是嵌入式 access-method library，应用线程就是它的执行线程；
- “行”是应用序列化的 bytes，存储层用 slotted page、B-Tree leaf 或 heap RID 组织；
- 普通事务以 lock + WAL + recovery 获得 ACID，MVCC 用 buffer page copy-on-write 提供 snapshot；
- 当前 fork 再用 SIREAD 和 rw-antidependency detection 把 snapshot 提升到 SSI；
- WAL 天然适合恢复与物理复制，但 logical CDC 还需要事务重组、身份、schema 和 retention contract；
- OLTP 与 OLAP 的差异不只在 row/column layout，更在提交单位、并发协议、恢复方式和资源治理边界。

下一步如果继续 dive，我会选两个可执行实验：第一，用两个 SSI transaction 复现 write skew 和 `DB_SNAPSHOT_CONFLICT`；第二，生成 insert/update/split/commit 日志，用 `db_printlog` 和 `DB_LOGC` 对照一次业务写入究竟展开成多少物理 record。只有把源码控制流和实际日志一一对应，才算真正理解了这套事务引擎。
