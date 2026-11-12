---
title: "【源码】libdb：从 B-Tree 页、WAL 与 MVCC 理解嵌入式 OLTP 引擎"
slug: "libdb-source-notes"
date: 2026-08-26T00:00:00+08:00
lastmod: 2026-11-12T00:00:00+08:00
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
description: "从一次嵌入式 DB->put 面临的页内变长重排、崩溃半写入与并发写偏斜三道物理难题出发，结合 libdb v5.3.34 源码拆解 Slotted B-Tree、WAL 先行法则、页级 MVCC 与 SSI 反依赖环检测。"
draft: false
toc: true
math: false
---

长期在大数据与分析型（OLAP）引擎里打转的人，往往对列式编码、向量化流水线和分布式 Shuffle 如数家珍，却容易把传统单机 OLTP 存储引擎简化成几句口号：“行存适合点查，WAL 用来崩溃恢复，MVCC 让读写互不阻塞。”可一旦追问到底层字节与并发时序，真正的硬问题才会浮出来——如果让你写一个直接链接进业务进程的嵌入式事务引擎，执行一行看似最简单的 `DB->put("order:1042", payload)`，用最朴素的做法第一步会在哪里撞墙？

不妨设想最直接的实现：顺着磁盘 B-Tree 二分找到目标 4 KiB 叶子页，把旧值擦掉、拷入新值，内存不够时把脏页 `pwrite` 刷回文件。只要一上真实并发和故障场景，这套朴素方案立刻会撞碎在三道物理与状态一致性悬崖上：

1. **变长字节搬移与跨页分裂的并发失稳**：把一条 114 字节的订单状态改成 136 字节，页内后续数据就得整体挪位；如果并发游标直接捏着页内裸偏移，挪位当场就会让游标读串行。更糟的是，万一这多出来的 22 字节刚好撑爆当前叶子页、触发向父节点蔓延的分裂（Split），刚才从根一路记下来的搜索路径瞬间全部作废，死抱着整条路径的写锁不放又会与并发结构修改撞成死锁。
2. **脏页提前换出（Steal）与进程猝死的半写入残局**：事务还没提交，缓冲池就可能因为内存吃紧把改了一半的脏页刷回磁盘；反过来，事务提交时如果每次都强制把随机分布的 4 KiB 数据页 `fsync` 落盘，随机 I/O 又会把吞吐拖垮。一旦进程在半途遭遇 `SIGKILL` 或机器掉电，磁盘数据页上既有未提交事务留下的脏字节，又缺已提交事务尚在内存里的修改——重启后光看数据文件，根本分不清哪一页该留、哪一页该退。
3. **无锁快照读放行后的写偏斜（Write Skew）异象**：为了不让读请求阻塞写事务，引入多版本并发控制（MVCC）让读者顺着历史版本链读快照；可当两个并发事务在同一快照时刻读完彼此关联的两页数据、再各自修改不同页面时，由于没有写写冲突，普通快照隔离（SI）会放任双方双双提交，悄无声息地击穿业务一致性约束。

为了看清工业级引擎怎样在几万行 C 代码里把这三件事严丝合缝地闭环，我深入研读了 Berkeley DB 5.3 社区演进分支 [libdb](https://github.com/berkeleydb/libdb) 的源码。作为一个没有独立 Server 进程、没有 SQL 解析器与分布式调度器遮挡的纯粹嵌入式事务型 KV 引擎，libdb 把 B-Tree 页组织、预写日志（WAL）、锁管理、页级 MVCC 以及社区新增的可串行化快照隔离（Serializable Snapshot Isolation, SSI）赤裸而紧凑地摊开在了调用者线程面前。

---

## 一、没有独立 Server 进程，事务与并发状态住在哪里？

初次接触嵌入式引擎的人常有一个直觉误区：既然 libdb 只是一个被业务 `dlopen` 或静态链接进来的 C 库，没有类似 MySQL `mysqld` 的守护进程，那它的句柄是不是就等于几块进程私有的 `malloc` 结构体？

恰恰相反。libdb 支持**同一台机器上的多个独立操作系统进程同时挂载（Attach）同一个数据库环境（Environment）并安全跑读写事务**。为此，它把运行时状态严格切成了寿命与可见域截然不同的三层：

![libdb 嵌入式事务引擎分层架构与共享内存区边界](/images/diagrams/aug-store-libdb-page-architecture.svg)

最上层是调用方线程手里的 `DB_ENV`、`DB`、`DBC`（游标）和 `DB_TXN` 进程内句柄；中间层则是通过 `mmap` 或系统共享内存建立的 **Environment Shared Regions**（涵盖 `LOCKREGION`、`TXNREGION`、`MPOOL`、`LOG`、`REP`），里面的锁对象、活跃事务描述符 `TXN_DETAIL` 与缓冲帧头 `BH` 绝不保存进程私有的虚拟内存裸指针（因为不同进程映射同一块共享内存的基地址不同），而是统一采用**共享区相对偏移量（Region Offset）**互相寻址；最底层才是持久化数据页文件、`log.*` 预写日志以及内存吃紧时换出历史旧页的 `__db.freezer.*` 临时冻结文件。

把这三层边界立住，许多看似反直觉的源码现象就一下通透了：比如调用 `DB_TXN->commit()` 销毁了进程内的 `DB_TXN` 句柄，共享区里的 `TXN_DETAIL` 却可能因为被 MVCC 页版本链或 SSI 读痕迹引用而继续存活；又比如 `DB->put()` 明明就在业务线程自己的调用栈里同步执行，却依然能靠共享区里的互斥量与逻辑锁协调跨进程并发。

在 [`src/env/env_open.c`](https://github.com/berkeleydb/libdb/blob/c4811dc87/src/env/env_open.c) 中，`DB_ENV->open()` 通过 `DB_INIT_MPOOL`、`DB_INIT_LOCK`、`DB_INIT_LOG`、`DB_INIT_TXN`、`DB_INIT_REP` 一组标志按铁律顺序拉起各个子系统：

```c
__env_attach(env, &init_flags, create_ok, retry_ok);
__mutex_open(env, create_ok);          /* 共享区互斥量必须最先就绪 */
__env_thread_init(env, create_ok);

if (DB_INIT_REP)   __rep_open(env);
if (DB_INIT_MPOOL) __memp_open(env, create_ok);
if (DB_INIT_LOG || DB_INIT_TXN)
                    __log_open(env);   /* 开启事务隐含必须开启日志 */
if (DB_INIT_LOCK)  __lock_open(env);
if (DB_INIT_TXN) {
                    __txn_open(env);
                    __env_init_rec(env, log_version);
}
if (DB_RECOVER)    __db_apprec(...);
```

这段启动顺序直接编码了子系统之间的物理依赖：共享内存互斥量是一切并发结构的前提；`mpool` 必须先注册页面换入换出转换钩子，随后日志与事务恢复才能重放页面；源码注释更特意点明“事务隐含日志，但不隐含锁（transactions imply logging but do not imply locking）”——单线程嵌入式应用完全可以只开事务与 WAL 来换取崩溃原子性，省去并发锁表开销。若是新进程加入已存在的环境，`__env_attach()` 还会强行用底层共享区的真实配置覆盖当前句柄传入的标志，杜绝不同进程带着分裂的子系统集合混入同一个数据目录。

在深入单次写入前，可以先备好下面这张核心模块地图：

| 核心职责 | 关键源码位置 |
| --- | --- |
| 环境挂载与四遍崩溃恢复 | `src/env/env_open.c`、`src/env/env_recover.c` |
| 公共 KV 门面与访问方法分派 | `src/db/db_iface.c`、`src/db/db_am.c`、`src/db/db_method.c` |
| B-Tree 下探、页内修改与分裂 | `src/btree/bt_search.c`、`bt_cursor.c`、`bt_put.c`、`bt_split.c` |
| Slotted 磁盘页物理结构 | `src/dbinc/db_page.h` |
| 缓冲池、MVCC 版本链与页冻结 | `src/mp/mp_fget.c`、`mp_fput.c`、`mp_mvcc.c`、`src/dbinc/mp.h` |
| 逻辑锁表、SSI 冲突与死锁检测 | `src/lock/lock.c`、`lock_deadlock.c`、`src/dbinc/lock.h` |
| 事务生命周期与逆序回滚 | `src/txn/txn.c`、`txn_chkpt.c`、`txn_rec.c` |
| WAL 编码落盘与日志恢复分发 | `src/log/log_put.c`、`log_get.c`、`src/db/db_dispatch.c` |
| 物理日志复制与乱序补洞 | `src/rep/rep_record.c`、`rep_log.c`、`rep_util.c` |

---

## 二、追踪一次 `DB->put`：游标状态机、Slotted Page 与分裂重试

现在我们正式调用一行 `dbp->put(dbp, txn, &key, &data, 0)`，看这团二进制字节怎样一路穿过 API 门面、游标栈、页锁与缓冲池，落进 B-Tree 叶子页里：

![libdb DB->put 写入链路：Slotted 页重排、分裂重试与 WAL 先行刷盘](/images/diagrams/aug-store-libdb-put-wal-btree.svg)

### 2.1 两级函数表分派与外层原子门禁

创建 `DB` 句柄时，[`__db_init()`](https://github.com/berkeleydb/libdb/blob/c4811dc87/src/db/db_method.c) 把 `dbp->put` 绑定到了公共前置/后置包装器 [`__db_put_pp()`](https://github.com/berkeleydb/libdb/blob/c4811dc87/src/db/db_iface.c)。别小看这层 `_pp` 门面，它替底层数据结构守住了两道关键防线：

```c
STRIP_AUTO_COMMIT(flags);
__db_put_arg(dbp, key, data, flags);           /* 校验 API 参数契约 */
ENV_ENTER(env, thread_info);                   /* 登记线程与环境上下文 */

__db_rep_enter(dbp, is_write=1, ...);          /* 复制角色门禁：只读从库在此直接拦截 */
if (IS_DB_AUTO_COMMIT(dbp, txn)) {
    __txn_begin(env, thread_info, NULL, &txn, 0);
    txn_local = 1;
}
__db_check_txn(dbp, txn, ...);                 /* 校验 DB 句柄与事务归属一致性 */
ret = __db_put(dbp, thread_info, txn, key, data, flags);

if (txn_local)
    __db_txn_auto_resolve(env, txn, 0, ret);    /* ret==0 则 commit，失败则完整 abort */
__env_db_rep_exit(env);
ENV_LEAVE(env, thread_info);
```

第一，复制集群的只读备库拦截发生在触碰任何缓冲页之前，绝不等把内存页弄脏了才后悔；第二，所谓自动提交（Auto-Commit）根本不是 B-Tree 内部有什么免事务后门，而是包装器当场开了一个标准本地 `DB_TXN`，哪怕单次 `put` 中途分裂到一半空间耗尽报错，`__db_txn_auto_resolve()` 也会走标准逆序日志把半成品干净回滚。

随后 [`__db_put()`](https://github.com/berkeleydb/libdb/blob/c4811dc87/src/db/db_am.c) 创建一个带 `DB_WRITELOCK` 意向的临时游标（`DBC`），先维护关联的二级索引，再由 `__dbc_iput()` 通过第二级函数指针 `dbc->am_put`（对 B-Tree 而言即 `__bamc_put`）跳进具体的访问方法——`DB_BTREE`、`DB_HASH`、`DB_HEAP`、`DB_QUEUE` 正是靠这两级分派共享同一套外层事务协议。

### 2.2 锁耦合下探、三类并发原语分离与 Root Snapshot 快路

进入 [`src/btree/bt_cursor.c`](https://github.com/berkeleydb/libdb/blob/c4811dc87/src/btree/bt_cursor.c) 的 `__bamc_put()` 后，你会发现它并不是“找一次叶子、塞进去完事”，而是一个围绕 `DB_NEEDSPLIT` 循环构建的可重试状态机：

```c
split:
    __bamc_search(dbc, root_pgno, key, DB_KEYLAST, &exact);
    choose_insert_op(exact, duplicates, flags, &iiop);

    switch (__bam_iitem(dbc, key, data, iiop, 0)) {
    case 0:
        break;
    case DB_NEEDSPLIT:
        __bam_stkrel(dbc, STK_CLRDBC | STK_NOLOCK); /* 释放已失效的旧路径页帧与短期锁 */
        cp->pgno = PGNO_INVALID;
        __bam_split(dbc, key, &root_pgno);
        goto split;                                  /* 跳回起点重新搜索定位 */
    }
```

为什么发现页满不能原地劈开直接把数据塞进去，非要调 `__bam_stkrel()` 扔掉刚才千辛万苦攒下的搜索栈、跳回 `split:` 重新搜一遍？原因在于，[`__bam_search()`](https://github.com/berkeleydb/libdb/blob/c4811dc87/src/btree/bt_search.c) 普通下探时为了并发度，内部节点拿的只是短期锁；一旦触发 `__bam_split()` 改变了父节点分隔键与左右兄弟边界，原先搜索栈里记下的 `(pgno, index)` 物理坐标已经不再对应同一个键区间，且如果死攥着整条栈的锁再去申请跨页分裂锁，极易与自顶向下或自底向上的其他线程死锁。唯有释放旧栈、完成分裂后重新按新拓扑定位，才能既缩窄锁粒度又保证坐标有效（若启用了按子树计数的记录号功能，甚至不能偷懒从局部父页重搜，必须老老实实退回真根重算沿途计数）。

在读 [`__bam_search()`](https://github.com/berkeleydb/libdb/blob/c4811dc87/src/btree/bt_search.c) 时，最核心的基本功是把源码里同时出现的三种保护机制彻底分清：

- **逻辑锁（Logical Lock）**：归 `LOCKREGION` 管，保护“某个事务能否读写某页/某记录”的隔离语义，写锁一直持有到事务提交或回滚；
- **缓冲闩锁（Buffer Latch / Mutex）**：归 `MPOOL` 缓冲头 `BH` 管，仅保护内存中单张 `PAGE` 字节布局在一次二分或搬移期间不被并发线程改花，用完即放；
- **引用钉住（Pin / Refcount）**：保证调用者拿着 `PAGE *` 指针读字节的短短几微秒内，该缓冲帧不会被淘汰换出。

在内部页二分时，因为叶子页每条记录占“键 + 值”两个槽位（步长 `P_INDX = 2`），内部页每个 `BINTERNAL` 分隔项只占一个槽位（步长 `O_INDX = 1`），代码按页类型动态切换二分步长：

```c
adjust = TYPE(page) == P_LBTREE ? P_INDX : O_INDX;
DB_BINARY_SEARCH_FOR(base, limit, NUM_ENT(page), adjust) {
    __bam_cmp(dbc, key, page, index, compare, &cmp);
    /* 收缩 base/limit 二分窗口 */
}

child_pgno = GET_BINTERNAL(dbp, page, child_index)->pgno;
BT_STK_PUSH(env, cursor, page, child_index, lock, lock_mode, ret);
```

顺着树往下走时，libdb 执行经典的锁耦合（拿到子页保护后立即释放只读父页的短期保护）。但就像我们在 Bw-Tree 中分析过的那样，哪怕只是对共享根页做短暂的 `pin/latch/unpin`，多核高并发下根缓冲头的引用计数也会变成滚烫的缓存行热点。为此，当前社区分支特意在 `__bam_search()` 入口处加了一条精巧的 **Root Snapshot 乐观快路**：每个 `DB` 句柄私自缓存一份只读根页拷贝，查询直接在本地私有拷贝上二分选出第一层子页号 `snap_child`，完全跳过共享根页的 `pin` 与 `latch`；等拿到子页后，再回头瞄一眼共享真根当前的 `LSN(live_root)` 是否仍等于快照记录的 `snap_lsn`——只要根没分裂改写过，一次零争用下探就此成交；万一碰上极低概率的根分裂导致 LSN 变了，就释放子页、刷新本地根拷贝并平滑退回标准锁耦合路径。

### 2.3 Slotted Page 两端生长布局与前缀压缩差异日志

下探抵达叶子页后，我们终于见到了传统行存引擎最经典的物理载体——定义在 [`src/dbinc/db_page.h`](https://github.com/berkeleydb/libdb/blob/c4811dc87/src/dbinc/db_page.h) 中的**分槽页（Slotted Page）**：

```c
typedef struct _db_page {
    DB_LSN    lsn;        /* 最后一次修改本页的日志序号（崩溃恢复与刷脏门禁核心） */
    db_pgno_t pgno;       /* 当前逻辑页号 */
    db_pgno_t prev_pgno;  /* 左兄弟页号（支撑双向顺序扫描） */
    db_pgno_t next_pgno;  /* 右兄弟页号 */
    db_indx_t entries;    /* 当前槽位总数 */
    db_indx_t hf_offset;  /* 高地址侧空闲区上界偏移（High Free Offset） */
    u_int8_t  level;      /* 树层级（叶子为 1） */
    u_int8_t  type;       /* 页类型，如 P_LBTREE / P_IBTREE */
} PAGE;
```

为了优雅容纳长短不一的键值字节串，一张 4 KiB 或 8 KiB 的 `PAGE` 从首尾两端相向生长：低地址紧跟在固定 26 字节页头后面的，是向高地址方向顺序追加的 2 字节偏移槽数组 `inp[0], inp[1], ...`（在 `P_LBTREE` 叶子页里，偶数槽 `inp[2k]` 指键，奇数槽 `inp[2k+1]` 指值）；而真正的变长数据体（`BKEYDATA { len, type, data[] }`）则从页尾最高地址往低地址方向倒着堆放，由 `hf_offset` 时刻标出中间剩余连续空闲区的上沿。

看看这套经典布局一石三鸟化解了什么痛点：

- 当某条记录因更新变长需要在页内挪窝时，只需搬移高地址区的数据字节并原地修正 `inp[]` 数组里对应的 2 字节偏移量，外部游标持有的 `(pgno, index)` 逻辑槽号稳如泰山；
- 碰上几百 KB 的超长 JSON 或二进制块时，绝不硬塞进叶子页把树的扇出率（Fanout）降成链表——[`__bam_iitem()`](https://github.com/berkeleydb/libdb/blob/c4811dc87/src/btree/bt_put.c) 一旦测出长度越过 `ovflsize` 阈值，只在叶槽里留下一个极小的 `BOVERFLOW { pgno, tlen }` 描述符，正文由 `__bam_ovput()` 拆成独立的溢出页链表（Overflow Page Chain）外置存放；
- 在原地覆盖更新已有键值时，[`__bam_ritem()`](https://github.com/berkeleydb/libdb/blob/c4811dc87/src/btree/bt_put.c) 甚至不会傻乎乎地把整条新旧记录全抄进 WAL！它会先从头尾向中间夹逼，算出新旧字节串的公共前缀 `prefix` 与公共后缀 `suffix`，只把真正发生变化的中间切片写进差异日志：

```c
/* 先向缓冲池申请排他修改权（若开启 MVCC 且非本事务私有页，此处触发整页写时复制！） */
__memp_dirty(mpf, &page, thread_info, txn, priority, 0);

prefix = common_prefix(old_data, new_data);
suffix = common_suffix(old_data + prefix, new_data + prefix);
orig   = old_data[prefix : old_len - suffix];
repl   = new_data[prefix : new_len - suffix];

/* 铁律第一步：先生成含新旧差异与旧 PAGE.lsn 的日志，并将返回的新日志 LSN 写回页头 */
__bam_repl_log(dbp, txn, &LSN(page), PGNO(page), old_page_lsn,
               index, orig, repl, prefix, suffix);

/* 铁律第二步：日志入缓冲并回填 LSN 后，才真正搬移页内字节与偏移数组 */
__bam_ritem_nolog(dbc, page, index, NULL, data, type);
```

请盯紧最后这两行调用的先后次序——整座 OLTP 大厦崩溃不坏的秘密，就浓缩在这短短十行代码里。

---

## 三、崩溃原子性与持久性：双链 LSN、WAL 先行法则与四遍恢复

为什么 `__bam_repl_log()` 必须严格卡在 `__bam_ritem_nolog()` 改动页字节之前，而且还要把新生成的日志序号当场烙进页头的 `LSN(page)`？

让我们跟进 [`__log_put_record_int()`](https://github.com/berkeleydb/libdb/blob/c4811dc87/src/log/log_put.c) 的序列化现场。无论上层是页内增删 `__db_addrem`、中段替换 `__bam_repl`、节点分裂 `__bam_split` 还是事务提交 `__txn_regop`，落进日志缓冲区的每一条记录开头都雷打不动带着三个核心字段：`rectype`（记录类型）、`txnid`（所属事务 ID）与 `prev_lsn`（同一事务上一条日志的 LSN），后面再接访问方法特有的文件号、页号、旧页 `prev_page_lsn` 与差异载荷：

```c
DB_SET_TXN_LSNP(txnp, &record_lsnp, &prev_lsnp);

LOGCOPY_32(env, bp, &rectype);
LOGCOPY_32(env, bp, &txnp->txnid);
LOGCOPY_FROMLSN(env, bp, prev_lsnp);      /* 指向本事务前一条日志，串成单事务逆序回滚链 */

for (field = spec; field->type != LOGREC_Done; ++field) {
    encode_fileid_pgno_dbt_or_page_lsn(bp, field);
}

__log_put(env, record_lsnp, &logrec, flags | DB_LOG_NOCOPY);
*prev_lsnp = *record_lsnp;                /* 推进事务描述符 txn->td->last_lsn */
*ret_lsnp  = *record_lsnp;                /* 返回给调用方写入数据页头 PAGE.lsn */
```

看明白了吗？每一条 WAL 记录落地的瞬间，同时织起了两条正交的因果链：

1. **按事务串联的 `prev_lsn` 逆向链**：假如业务执行到一半显式调用 `DB_TXN->abort()`（或死锁被选为牺牲品），因为非 MVCC 修改或 B-Tree 分裂早已写进了共享缓冲池，绝不能简单释放内存了事。[`__txn_undo()`](https://github.com/berkeleydb/libdb/blob/c4811dc87/src/txn/txn.c) 会从 `txn->td->last_lsn` 出发，顺着每条记录头部的 `prev_lsn` 一路倒着往回抓日志，交给 `__db_dispatch(..., DB_TXN_ABORT)` 逐条逆向抵消（包括回收刚分配的溢出页、撤回叶子槽修改），直到 `ZERO_LSN` 为止。
2. **按物理页串联的 `PAGE.lsn` 水位锁**：生成日志时，日志还只躺在内存 log buffer 里，并不需要每次改一个槽位都去调昂贵的磁盘 `fsync`；但如果后台 Checkpoint 或缓冲池淘汰线程想把某张脏数据页 `P` 写回磁盘数据文件，Mpool 会先检查 `P` 页头的 `PAGE.lsn`——只要磁盘上已持久化的 WAL 末尾还没追上 `PAGE.lsn`，就必须先把日志缓冲区刷盘到该 LSN 之后，才放行脏数据页写盘！

这就是数据库教科书里常被混成一团的两条独立法则：**WAL 先行法则（Write-Ahead Rule，脏页落盘前其对应日志必先落盘）保的是“未提交脏页万一提前落盘，崩溃后一定有据可查、能被干净 UNDO”（即原子性 Atomicity）；而事务提交同步策略保的则是“一旦向客户端返回 Commit 成功，掉电后能不能把尚未落盘的数据页 REDO 回来”（即持久性 Durability）。**

在 [`__txn_commit()`](https://github.com/berkeleydb/libdb/blob/c4811dc87/src/txn/txn.c) 追加 `TXN_COMMIT` 记录时，libdb 把这笔持久性与延迟的取舍交给了三种明确的故障承诺档位：

| 提交策略标志 | `commit()` 返回客户端前的物理动作 | 仅业务进程 `SIGKILL` 猝死 | 整机内核崩溃或拔电源掉电 |
| --- | --- | --- | --- |
| `DB_TXN_SYNC`（默认） | 将含 `TXN_COMMIT` 在内的日志刷入存储介质并完成同步 | 已确认事务完整恢复 | 只要底层文件系统与磁盘兑现持久化刷盘语义即可完整恢复 |
| `DB_TXN_WRITE_NOSYNC` | 调用 `write()` 将日志移交操作系统页缓存，省去 `fsync` 阻塞 | 依赖存活的 OS Page Cache 仍可恢复 | 尚在内核缓冲区未落盘的最近几毫秒已确认事务可能丢失 |
| `DB_TXN_NOSYNC` | 提交日志仅留在用户态共享内存日志缓冲区，后台异步刷写 | 随共享内存生命周期而定 | 最近一批已确认事务直接丢失，换取极致内存级提交吞吐 |

当机器真从崩溃中重启、带着 `DB_RECOVER` 打开环境时，[`__db_apprec()`](https://github.com/berkeleydb/libdb/blob/c4811dc87/src/env/env_recover.c) 通过严整的**四遍扫描（4-Pass Recovery）**完成最终验收：

- **Pass 0 & Pass 1（定锚与点名）**：定位最近一次 Checkpoint 起点向前扫描日志，重建文件号到数据库的映射，并清点出哪些 `txnid` 见到了 `TXN_COMMIT`（赢家 Winner），哪些到日志末尾仍悬而未决（输家 Loser）；
- **Pass 2（逆向拔除输家脏写）**：从日志末尾向后倒扫，对所有输家事务执行 `DB_TXN_BACKWARD_ROLL`（UNDO）；
- **Pass 3（正向补齐赢家承诺）**：再从起点向前顺扫，对所有赢家事务执行 `DB_TXN_FORWARD_ROLL`（REDO）。

最绝的是，崩溃时可能有些数据页已经刷盘了、有些没刷，如果恢复程序本身跑到一半又断电重启了怎么办？libdb 的每条页面恢复函数只需核对日志记录里的两个 LSN 与磁盘当前页头的 `page_lsn`，天然就具备**严格幂等性（Idempotence）**：

```c
/* 正向 REDO：只有当磁盘页恰好停在本次修改前的旧水位时，才重放后像并推高 page_lsn */
if (REDO && page_lsn == record.prev_page_lsn) {
    apply_after_image_or_operation();
    page_lsn = record_lsn;
}
/* 逆向 UNDO：只有当磁盘页确实包含本次脏修改时，才回退前像并降回 prev_page_lsn */
if (UNDO && page_lsn == record_lsn) {
    apply_before_image_or_inverse_operation();
    page_lsn = record.prev_page_lsn;
}
```

哪怕恢复过程反复中断十次，已经重放过的赢家页（`page_lsn == record_lsn`）和从未沾染脏写的干净页（`page_lsn == prev_page_lsn`）都会被精准跳过，一分不多、一毫不少。

---

## 四、从页级 MVCC 到 SSI：不加阻塞读锁，怎样抓出写偏斜？

搞定了单写与崩溃恢复，接下来进入并发深水区。在默认锁模式下，读操作为了防止不可重复读而持有的读锁会挡住写者，写者的排他锁又会卡死读者。为此，libdb 提供了由 `DB_MULTIVERSION` 与 `DB_TXN_SNAPSHOT` 开启的多版本缓冲池；而在当前社区分支中，依照 [`RFC 0003`](https://github.com/berkeleydb/libdb/blob/c4811dc87/rfc/0003-ssi-serializable-snapshot-isolation.md)，`DB_TXN_SNAPSHOT` 已被直接升级为完整的 Cahill **可串行化快照隔离（Serializable Snapshot Isolation, SSI）**：

![libdb 页级 MVCC 快照可见性与 SSI 读写反依赖（rw-conflict）环检测](/images/diagrams/aug-store-libdb-mvcc-ssi.svg)

### 4.1 惰性快照获取、整页写时复制（COW）与 Freezer 冷冻换出

与 PostgreSQL 或 InnoDB 在每条行记录头部塞入隐藏事务号/回滚指针的做法不同，libdb 的底层核心只是一个不懂行 Schema 的通用页引擎。为了让上层 B-Tree 二分查找代码一行不改就能享受到多版本，libdb 选择在缓冲池 `MPOOL` 层面做**页级写时复制（Page-Level Copy-on-Write）**。

这里藏着三个极其考究的源码细节：

第一，**快照时间戳 `read_lsn` 采用惰性获取（Lazy Snapshot Acquisition）**。调用 `__txn_begin()` 时，`td->read_lsn` 和 `td->visible_lsn` 都被初始化为无穷大 `MAX_LSN`；直到该事务（或其嵌套子事务溯源找到的顶层父事务）第一次通过 [`__memp_fget()`](https://github.com/berkeleydb/libdb/blob/c4811dc87/src/mp/mp_fget.c) 真正触碰多版本页面时，才调 `__log_current_lsn_int()` 拍下那一刻的日志水位作为快照上界。这样一来，事务开启后若先做了一堆网络准备或非多版本操作，绝不会白白拿一个过老的快照去拖累历史版本回收。

第二，**整页拷贝与防撕裂可见性判定**。当写事务试图调 `__memp_dirty()` 修改一张不属于自己私有的多版本缓冲页时，`makecopy` 分支立刻分配一个新的缓冲头 `alloc_bhp`，把整张 4 KiB/8 KiB 页面原样 `memcpy` 过去，通过 `SH_CHAIN_INSERT_AFTER` 插到该 `(file, pgno)` 版本链 `vc` 的最新端，并将 `td_off` 指向当前写事务。等到写事务成功提交、拿到提交记录的 LSN 赋给 `td->visible_lsn` 后，该页版本才正式对后来者可见。并发读者顺着版本链从新往旧找第一个满足 `BH_OWNED_BY(本事务)` 或 `BH_VISIBLE(read_lsn)` 的缓冲帧时，[`src/dbinc/mp.h`](https://github.com/berkeleydb/libdb/blob/c4811dc87/src/dbinc/mp.h) 里的 `BH_VISIBLE` 宏甚至特意对 `visible_lsn` 的文件号与偏移量做了两次校验，确保即便撞上写者正在并发赋值的瞬间，读到半截的 LSN 也会被安全当作“未来不可见”处理。

第三，**长事务逼出的 Freezer 冷冻机制**。整页 COW 最大的软肋，在于改 10 个字节也要复制整张 8 KiB 页；假如有个粗心的慢查询捏着半小时前的 `read_lsn` 迟迟不结束，它身后的整串旧 `BH` 就统统不能被判定为 `BH_OBSOLETE` 回收，很快就会把宝贵的内存缓冲池塞满。为此，[`__memp_bh_freeze()`](https://github.com/berkeleydb/libdb/blob/c4811dc87/src/mp/mp_mvcc.c) 在内存承压时会把旧版本的页面字节刷进 `__db.freezer.<cache>.<bucket>.<pagesize>` 临时文件，内存里只留一个轻量的 `BH_FROZEN` 空壳头；万一那个慢读者真读到了这页，再按需解冻（Thaw）读回内存。这清晰揭示了工程代价的传导链：长快照在页级 MVCC 下不仅费内存，还会直接演化成额外的临时盘读写 I/O。

### 4.2 双路径捕获 `rw-antidependency`：在不阻塞读的前提下消灭写偏斜

光有页级 MVCC 快照读，还挡不住经典的**写偏斜（Write Skew）**。设想医院值班表规定“至少留一名医生在岗”，当前页 `P_A`（张医生在岗）和页 `P_B`（李医生在岗）最新可见版本都在 `L20`：事务 T1 和 T2 同时在 `read_lsn = L50` 快照下读完 `P_A` 和 `P_B`，都看到有两名医生在岗；于是 T1 安心改写 `P_B` 把李医生改成休假，T2 同时改写 `P_A` 把张医生改成休假。由于它俩改的是完全不同的物理页，写锁互不冲突，普通快照隔离下双双顺利提交，结果医院空无一人！

要在**绝不退回阻塞式共享读锁**的前提下抓出这类异象，Michael Cahill 提出的 SSI 定理给出了数学判据：任何违反可串行化的写偏斜，在并发依赖图里必然形成至少两条连续的**读写反依赖边（`T1 --rw--> T2 --rw--> T3`，其中 `T3` 可能就是 `T1` 自己）**，所谓 `R --rw--> W` 即“读者 `R` 读到了某个对象的旧版本，而并发写者 `W` 随后覆写了该对象的新版本”。处于中间既当别人写端、又当另一条边读端的 `T2`，就是危险结构的**枢轴（Pivot）**。

对照前文时序图，因为“先读后写”与“先写后读”到达现场的先后次序不同，libdb 必须在两个完全不同的子系统里布下天罗地网，才能一条不漏地逮住所有 `rw` 边：

1. **锁表路径（先读后写：`R` 先读留痕，`W` 后到拿写锁）**：[`DB_LOCKOBJ`](https://github.com/berkeleydb/libdb/blob/c4811dc87/src/dbinc/lock.h) 除了维护普通互斥锁的持有队列 `holders` 与等待队列 `waiters`（注：普通加锁请求在 [`__lock_get_internal()`](https://github.com/berkeleydb/libdb/blob/c4811dc87/src/lock/lock.c) 中既要与 `holders` 兼容，也不能插队越过更早排队的冲突 `waiter` 以防写饥饿，真出现环形等待则由 `lock_deadlock.c` 构造 `locker × locker` 位图检出环并挑牺牲品返回 `DB_LOCK_DEADLOCK`），还专门挂了第三条**绝不阻塞任何写者的 `sireaders` 链表**。快照读者每读一页，就悄悄挂上一枚 `DB_LOCK_SIREAD` 标记；稍后写者 `W` 来拿 `DB_LOCK_WRITE` 时，顺手扫一遍 `sireaders`：

```c
for (siread = obj->sireaders; siread != NULL; siread = next) {
    if (writer == siread->holder)
        remove_own_siread();
    else if (reader_is_still_concurrent(siread, writer)) {
        TXN_SYSTEM_LOCK(env);                 /* 与提交期枢轴判定严格串行化 */
        F_SET(writer_td, TXN_DTL_WCONF);      /* 标记 W 处于某条 rw 边的写入端 */
        F_SET(reader_td, TXN_DTL_RCONF);      /* 标记 R 处于某条 rw 边的读取端 */
        TXN_SYSTEM_UNLOCK(env);
    }
}
```

2. **版本链路径（先写后读：`W` 已写出新版本甚至已提交，旧快照 `R` 后到跳过它）**：在 [`__memp_si_rwconflict()`](https://github.com/berkeleydb/libdb/blob/c4811dc87/src/mp/mp_fget.c) 中，当快照读者在 `BH` 版本链上选定满足 `visible_lsn <= read_lsn` 的旧版本后，代码不会立刻返回，而是掉头沿着版本链向更新（Newer）的方向回扫一遍——只要发现有更新版本的拥有者仍在运行、或者是在本读者 `read_lsn` 之后才提交的，就说明本读者为了维持旧快照而“闭眼跳过了并发新写入”，当场补记一条 `R --rw--> W`（置 `R.RCONF` 与 `W.WCONF`）！

到了 [`__txn_commit()`](https://github.com/berkeleydb/libdb/blob/c4811dc87/src/txn/txn.c) 准备提交的临门一脚，引擎只需在 `TXN_SYSTEM_LOCK` 保护下检查当前事务是否同时亮起了 `TXN_DTL_RCONF` 和 `TXN_DTL_WCONF` 两个标志位：一旦双双命中，立刻判定为危险结构枢轴，拒绝提交并返回 `DB_SNAPSHOT_CONFLICT` 交由应用层整事务回滚重试。而且，为了防止读者 `T1` 提交释放句柄后写者 `T2` 才姗姗来迟漏记第一条边，挂有 `SIREAD` 标记的 `TXN_DETAIL` 会由原子引用计数 `si_ref` 续命，直到全局最老快照越界后才由后台 GC 回收。由于当前冲突粒度建立在物理页（Page Granularity）而非单键上，高并发争抢同一张热点小页时会出现假阳性（False Positive）连坐中止，这也是业务选型时必须纳入考量的吞吐代价。

---

## 五、物理复制、逻辑 CDC 的鸿沟，以及 OLTP 与 OLAP 的设计镜像

顺着 WAL 再往前迈一步，很多人看到 libdb 提供了按 LSN 顺序读取日志的游标 `DB_LOGC`，并且内置了一套高可用复制子系统（`src/rep/`），自然会问：**能不能直接拿 `DB_LOGC` 当现成的业务变更数据捕获（CDC）流来消费？**

翻开 [`__rep_apply()`](https://github.com/berkeleydb/libdb/blob/c4811dc87/src/rep/rep_record.c) 的复制状态机，就能看清“物理复制”与“逻辑 CDC”完全是两码事。主库发出的 `REP_LOG` 消息在网络上可能丢包或乱序到达，从库为此维护了两个水位：`ready_lsn`（下一条紧挨着本地持久前缀、期望落盘的日志位点）与 `waiting_lsn`（暂存队列里最早一条超前到达记录的位点）。一旦收到 `incoming_lsn > ready_lsn` 的超前日志，从库绝不敢跳着应用，而是以 `DB_NOOVERWRITE` 先塞进内部临时库 `__db.rep.db`，同时调 `__rep_loggap_req()` 向主库索要 `[ready_lsn, waiting_lsn)` 缺失段；只有等缺口严丝合缝补齐、`ready_lsn` 追平 `waiting_lsn` 后，才把暂存记录连续排空落盘。

为什么物理复制必须死守**连续字节级 WAL 前缀**？因为 libdb 的 WAL 本质上是面向物理页与访问方法的恢复指令，而非面向业务模型的行事件：

- 一次业务层面的 `DB->put(key, large_val)`，在 WAL 里会炸开成分配溢出页、写入多段溢出链、分裂 B-Tree 叶子、修改父页分隔键、前缀压缩替换叶槽等五六条物理记录；
- 一条单独的 `__bam_repl` 日志里只存了被截掉首尾公共部分的中段差值，脱离当时的旧页镜像或完整上下文根本拼不出业务要的完整新旧 JSON；
- 未提交事务的脏日志与最终回滚的日志全都混在同一条物理流里，没有类似 PostgreSQL Logical Decoding 的复制槽（Replication Slot）与 Schema 注册契约。因此，若要在业务里做可靠的事件驱动发布，最稳妥的做法是在同一个 `DB_TXN` 事务里顺手写一张 Outbox 队列表，而把底层物理 WAL 留给崩溃恢复与同构从库复制。

站在全篇终点回望，从 OLAP 与大数据系统跨界细读 libdb 源码，最震撼的莫过于看清两类引擎在每一个设计十字路口上截然相反的取舍：

| 核心工程矛盾 | OLTP 页式引擎（以 libdb 为代表） | 分析型 OLAP / 湖仓系统（列存与批处理） |
| --- | --- | --- |
| **物理存储与读写基本单位** | 4–8 KiB Slotted 行存页；极力保单键微秒级点查与原地局部修改 | 数 MB 不可变列存块（Parquet / SSTable）；靠字典/RLE 压缩与列裁剪榨干扫描带宽 |
| **并发与结构变更路径** | 前台精细区分逻辑页锁、短时缓冲 Latch、Pin 引用与页分裂栈重试 | 写入生成不可变新文件，靠 Catalog 原子切换快照 Manifest，后台异步做大粒度 Compaction |
| **故障原子性与崩溃恢复** | 严守 WAL 先行法则，靠 `prev_lsn` 与 `PAGE.lsn` 对原地脏页做四遍幂等 REDO/UNDO | 失败任务直接丢弃未发布临时文件，几乎无需原地逆向涂改已有数据块 |
| **隔离性与多版本成本** | 页级 COW 链 + `SIREAD` 反依赖环检测，长事务直接挤压缓冲池并触发 Freezer 换出 | 查询天然绑定不可变历史快照文件，多版本主要体现为对象存储上的空间保留成本 |

把散落在数十个 `.c` 文件里的机制收束成一句话，整套 libdb 内核其实就是靠十条环环相扣的不变量咬合运转的：

1. 公共 `_pp` 包装器守住环境、复制角色与事务归属边界，自动提交同样走完整本地事务；
2. 无 `pin` 严禁解引用页面内存，无排他 `latch` 严禁改动页面字节，长生命周期逻辑 `lock` 绝不替代前两者；
3. B-Tree 一旦触发跨页分裂，旧搜索栈物理坐标当即作废，必须释放旧栈回退重搜；
4. 任何脏数据页落盘前，其页头 `PAGE.lsn` 对应的 WAL 记录必须已先持久化落盘；
5. 单事务的所有日志通过 `prev_lsn` 严格单调前指，确保中途 `abort` 能逆序撤销每一处物理副作用；
6. 崩溃恢复仅在 `page_lsn == prev_page_lsn` 时正向 REDO、在 `page_lsn == record_lsn` 时逆向 UNDO，天然幂等；
7. 快照读仅可见本事务私有页或 `visible_lsn <= read_lsn` 的已提交页版本，并发读半截 LSN 保守判为不可见；
8. 历史多版本页只有在早于全局最老活跃快照后才允许回收，内存承压时只能冻结换出到 Freezer 而不能强拆可见性；
9. SSI 的 `RCONF / WCONF` 冲突标记写入与提交期枢轴检查受同一把系统锁串行化保护，且 `SIREAD` 痕迹随 `si_ref` 跨越句柄生命周期存活；
10. 物理复制从库只推进无缺口的连续 WAL 前缀，乱序超前记录必须暂存等待补洞，绝不跳号应用。

---

## 源码阅读路线、构建记录与基线说明

如果想亲手切入这份代码库验证上述闭环，推荐按由浅入深的六条小路径单步跟踪：先从 `__db_put_pp -> __bamc_put -> __bam_iitem` 走通一次不分裂的原地更新，核对差异日志与 `PAGE.lsn` 回填；再故意塞满一页观察 `DB_NEEDSPLIT` 释放搜索栈与重试；接着跟踪 `__lock_get_internal` 与 `__lock_detect` 区分正常锁排队与位图死锁破环；随后在 `__memp_fget` 与 `__memp_si_rwconflict` 中观察整页 COW 与 SSI 双路径冲突捕获；最后对照 `__log_put_record_int`、`__db_apprec` 与 `__rep_apply` 跑通恢复与乱序补洞。

在本地 Linux / GCC 环境下做静态库与配套诊断工具（`db_printlog`、`db_recover`、`db_verify`、`db_archive`）最小构建时，由于 `configure` 探测到 POSIX AIO 位于 `librt` 却未自动把 `-lrt` 写入最终链接参数，需显式传入 `LIBS` 完成编译：

```bash
cd build_unix
../dist/configure --disable-shared --disable-replication
make -j4 LIBS='-lrt -lpthread'
```

编译产出的 `db_printlog` 是下一步动手实验的最佳显微镜——后续可构造一组并发双事务写偏斜用例现场触发 `DB_SNAPSHOT_CONFLICT`，并对比一次含大对象溢出与分裂的 `DB->put` 在物理日志里展开的完整记录序列。

- **源码基线**：[berkeleydb/libdb](https://github.com/berkeleydb/libdb) 社区分支 `v5.3.34`（提交 [`c4811dc8`](https://github.com/berkeleydb/libdb/commit/c4811dc87)）。相较于早年 Oracle 官方停更的 5.3.28，该社区基线已合入 [`RFC 0003`](https://github.com/berkeleydb/libdb/blob/c4811dc87/rfc/0003-ssi-serializable-snapshot-isolation.md)（将 `DB_TXN_SNAPSHOT` 升级为 SSI，注意当前 SSI 尚不支持 XA/2PC `prepare()`）、B-Tree Root Snapshot 乐观快路、Cache Cooling 以及 `io_uring`/POSIX AIO 异步后端等关键演进。
