---
title: "【源码】DPDK：从 PCIe、DMA 与 Descriptor 到 PMD 快路径"
slug: "dpdk-source-notes"
date: 2026-09-09T00:00:00+08:00
lastmod: 2026-09-09T00:00:00+08:00
categories:
  - Systems
tags:
  - DPDK
  - Linux
  - PCIe
  - DMA
  - VFIO
  - NUMA
  - PMD
  - Network
description: "基于 DPDK main 源码，从 PCIe、DMA、IOVA 与 VFIO 出发，逐层追踪 EAL、mbuf、mempool、ring、ethdev、ixgbe RX/TX、RSS 和 rte_flow，建立 packet 从 NIC 到用户态 fast path 的完整心智模型。"
draft: false
toc: true
math: false
---

第一次接触 DPDK 时，我记住的是一串 API：`rte_eal_init`、`rte_pktmbuf_pool_create`、`rte_eth_rx_burst`。但会调用这些函数，并不等于理解 DPDK。只要继续追问几个问题，API 记忆很快就不够用了：网卡究竟把包 DMA 到哪里？CPU 为什么能用虚拟地址访问同一块数据？descriptor、mbuf 和 packet buffer 分别归谁所有？为什么一个普通函数调用最后会落到特定网卡的 SIMD 收包实现？

这次我按 `dive-dpdk.md` 的路线，从硬件契约向上阅读源码，而不是从 API 向下背文档。本文基于官方 DPDK 仓库 `main` 分支提交 [`d55ccd4e6d`](https://github.com/DPDK/dpdk/commit/d55ccd4e6de64e3f797f60de9e81f1d60f849775)。`git describe` 为 `v26.07-8-gd55ccd4e6d`，源码中的 `VERSION` 是 `26.11.0-rc0`。因此它是 **v26.07 之后 8 个提交、已经进入 26.11 开发周期的快照**，不是正式的 26.11 release。

读完以后，我对 DPDK 的核心认识变成了：

> DPDK 是一个把网卡 queue、DMA memory 和专用 CPU worker 交给用户态程序管理的数据面运行时。它的性能不是来自某一个“零拷贝 API”，而是来自一组互相配合的不变量：固定 ownership、批处理、每核私有状态、NUMA locality、短控制路径，以及 descriptor 可见性与 doorbell 顺序。

## 1. 先画清边界：DPDK 不是更快的 socket

传统 Linux 网络栈必须服务通用目标：不同协议、进程隔离、调度、公平性、动态路由和安全策略。一次收包通常经过 IRQ/NAPI、内核 driver、`sk_buff`、协议栈、socket queue，再由系统调用交给应用。DPDK 选择了另一组约束：由用户态 PMD 直接轮询 NIC queue，把 worker 固定在 CPU 上，以 busy polling 换掉中断和调度，以 burst 换掉 per-packet 固定开销。

```text
Linux:
NIC → DMA → RX descriptor → IRQ/NAPI → kernel driver
    → sk_buff → TCP/IP → socket queue → syscall → application

DPDK:
NIC → DMA → RX descriptor ← PMD polling → rte_mbuf * → application
                                              │
application → PMD → TX descriptor → DMA ──────┘
```

这不意味着 Linux 路径“设计得慢”，而是两个系统承担的职责不同。DPDK 应用需要自己处理 CPU 隔离、queue 映射、内存生命周期、协议功能、可观测性和故障恢复。它适合 NFV、负载均衡、vSwitch、存储网络和 packet processing；并不自动适合所有普通网络服务。

## 2. 最底层契约：PCIe、MMIO、DMA 与 descriptor

理解 DPDK，首先要把“控制设备”和“搬运数据”分开。

- **PCIe 配置空间**描述设备身份、能力和 BAR。
- **BAR/MMIO**让 CPU 通过内存读写语义访问网卡寄存器。
- **DMA**让网卡绕过 CPU copy，直接读写主存。
- **descriptor ring**是 CPU 和 NIC 共享的工作队列，descriptor 中保存 buffer 的 DMA 地址、长度、状态和 offload 信息。
- **doorbell**通常是一个 MMIO tail register。软件准备完一批 descriptor 后写 tail，通知 NIC 有新工作。

以接收队列为例，软件先给每个 RX descriptor 填入空 buffer 的 IOVA，再把队列交给 NIC。包到达后，NIC 按 RSS 选择 RX queue，把数据 DMA 到 buffer，写回长度、校验和、VLAN、RSS hash 等字段，最后设置完成位。PMD 轮询完成位，取走收到的 mbuf，同时放入新的空 mbuf 补洞。

```text
                CPU / PMD                         NIC
                    │                              │
allocate mbuf       │                              │
write buffer IOVA ──┼──→ RX descriptor ring        │
write RDT doorbell ─┼─────────────────────────────→│
                    │                         packet arrives
packet buffer       │←────────────── DMA packet data
descriptor status   │←────────────── DMA write-back
poll DD bit         │                              │
```

这里有两个经常混在一起的对象：descriptor ring 是硬件 ABI 的一部分；`rte_mbuf` 是 DPDK 软件元数据。网卡不认识 `rte_mbuf *`，只认识 descriptor 中的 DMA 地址。

## 3. VA、PA、IOVA、IOMMU 与 VFIO

同一块内存同时面对两个地址空间：CPU 用虚拟地址，设备发 DMA 时用 I/O 虚拟地址。

```text
CPU load/store:  VA   ── MMU ──→ PA
NIC DMA:         IOVA ─ IOMMU ─→ PA
```

- VA 是进程看到的 virtual address。
- PA 是物理内存地址。
- IOVA 是设备放进 DMA transaction 的地址。
- IOMMU 将 IOVA 翻译到 PA，同时限制设备能访问的物理页。
- VFIO 向用户态提供受隔离的设备访问、interrupt 和 DMA mapping 接口。

[`rte_memseg`](https://github.com/DPDK/dpdk/blob/d55ccd4e6de64e3f797f60de9e81f1d60f849775/lib/eal/include/rte_memory.h) 同时记录 `addr`、`iova`、`len`、`hugepage_sz` 和 `socket_id`；[`rte_memzone`](https://github.com/DPDK/dpdk/blob/d55ccd4e6de64e3f797f60de9e81f1d60f849775/lib/eal/include/rte_memzone.h) 在其上提供命名的连续内存区域。CPU 设置 descriptor 时，从软件对象拿到 IOVA；CPU 真正读 packet bytes 时，仍通过 VA。

Linux EAL 的 VFIO 路径最终用 `VFIO_GROUP_SET_CONTAINER` 把 group 加入 container，使用 `VFIO_SET_IOMMU` 选择 IOMMU backend，再通过 `VFIO_IOMMU_MAP_DMA` 提交 `vaddr/iova/size` 映射。对应实现在 [`eal_vfio.c`](https://github.com/DPDK/dpdk/blob/d55ccd4e6de64e3f797f60de9e81f1d60f849775/lib/eal/linux/eal_vfio.c) 和 PCI VFIO glue [`pci_vfio.c`](https://github.com/DPDK/dpdk/blob/d55ccd4e6de64e3f797f60de9e81f1d60f849775/drivers/bus/pci/linux/pci_vfio.c)。

IOVA 有两种常见模式：

- IOVA-as-VA：IOVA 数值可与进程 VA 相同，由 IOMMU 再映射到 PA。
- IOVA-as-PA：设备使用物理地址语义。

`rte_eal_init()` 会综合 bus 要求、构建配置、物理地址是否可用和 IOMMU 状态选择模式。这里最需要避免的误解是：**IOVA-as-VA 只说明数值可相同，不表示 VA 等于 PA。**

### Hugepage 真正解决什么

Hugepage 首先扩大 TLB reach。假设工作集为 1 GiB，4 KiB page 需要 262144 个页映射，2 MiB hugepage 只需 512 个。它还给 EAL 提供了更可控的 pinned/DMA memory、memseg 组织和 NUMA placement。是否物理连续取决于 page size、分配方式和 IOMMU 映射，不能把 hugepage 简化成“为了拿一整块连续物理内存”。

## 4. `rte_eal_init()`：把进程变成数据面 runtime

[`rte_eal_init()`](https://github.com/DPDK/dpdk/blob/d55ccd4e6de64e3f797f60de9e81f1d60f849775/lib/eal/linux/eal.c) 不只是解析 `-l` 和 `-n`。按当前 Linux 实现，它大致完成：

```text
arguments / logging
  → CPU、MMU、lcore topology
  → plugin、trace、shared configuration
  → interrupt、alarm、multi-process channel
  → rte_bus_scan()
  → IOVA mode selection、hugepage info、VFIO
  → memzone / rte_eal_memory_init() / malloc heaps
  → tailq、timer
  → pin main lcore
  → create + pin worker pthreads
  → service cores
  → rte_bus_probe()
  → telemetry
```

这个顺序说明 EAL 同时扮演 hardware discovery、memory manager、thread runtime 和 process coordination layer。先 scan 是为了收集 bus/driver 对 IOVA 的约束；先建好 memory/VFIO，之后 probe PMD 时才能映射 BAR 和 DMA memory。

### lcore 不是另一种硬件线程

`lcore` 是 EAL 对逻辑执行资源的抽象。Linux 实现仍创建 pthread，再把它 pin 到配置的 CPU set，并把 socket/core/role/state 保存在 `lcore_config[]`。

worker 的运行机制也不是每次 launch 新建线程。[`rte_eal_remote_launch()`](https://github.com/DPDK/dpdk/blob/d55ccd4e6de64e3f797f60de9e81f1d60f849775/lib/eal/common/eal_common_launch.c) 把函数和参数发布给已有 worker，唤醒它的 pipe；worker 在 [`eal_thread_loop()`](https://github.com/DPDK/dpdk/blob/d55ccd4e6de64e3f797f60de9e81f1d60f849775/lib/eal/common/eal_common_thread.c) 中取函数执行，再回到 WAIT 状态。函数指针发布使用 release/acquire ordering，保证 worker 醒来后看见完整任务参数。

### primary/secondary 的边界

EAL 可以让 secondary process 重新映射 primary 的 shared configuration 和 hugepage memory，并通过 Unix socket multi-process channel 同步 hotplug、malloc 等操作。但它们不是两份独立内存；地址映射和 DPDK 版本必须兼容。`--in-memory` 会关闭基于共享文件的 secondary 支持。对初学者而言，先掌握单进程多 lcore；多进程是部署与隔离能力，不是 RX/TX 快路径的必要条件。

## 5. 从 PCI device 到 ixgbe PMD

EAL 初始化中的 `rte_bus_scan()` 和 `rte_bus_probe()` 分工明确。PCI scan 枚举设备；probe 遍历 driver 的 PCI ID 表，检查 IOVA 要求，需要时映射 PCI resource，然后调用匹配 driver 的 probe callback。

ixgbe 在 [`ixgbe_ethdev.c`](https://github.com/DPDK/dpdk/blob/d55ccd4e6de64e3f797f60de9e81f1d60f849775/drivers/net/intel/ixgbe/ixgbe_ethdev.c) 中注册：

```c
static struct rte_pci_driver rte_ixgbe_pmd = {
    .id_table = pci_id_ixgbe_map,
    .drv_flags = RTE_PCI_DRV_NEED_MAPPING | RTE_PCI_DRV_INTR_LSC,
    .probe = eth_ixgbe_pci_probe,
    .remove = eth_ixgbe_pci_remove,
};

RTE_PMD_REGISTER_PCI(net_ixgbe, rte_ixgbe_pmd);
```

因此完整控制路径是：

```text
rte_bus_scan
  → enumerate PCI devices
rte_bus_probe
  → vendor/device ID match
  → validate IOVA mode
  → allocate interrupt handles
  → rte_pci_map_device
  → pci_driver.probe
  → eth_ixgbe_pci_probe
  → create rte_eth_dev + install dev_ops / burst callbacks
```

ethdev 不是另一个真正搬包的 driver。它提供统一配置 API 和 fast-path dispatch；真正读写硬件 descriptor 的仍是 PMD。

## 6. `rte_mbuf`：packet 的软件控制块

[`struct rte_mbuf`](https://github.com/DPDK/dpdk/blob/d55ccd4e6de64e3f797f60de9e81f1d60f849775/lib/mbuf/rte_mbuf_core.h) 最值得按 cache line 阅读。关键字段的关系如下：

```text
rte_mbuf metadata
├── buf_addr ──────────────→ packet buffer CPU VA
├── buf_iova ──────────────→ packet buffer DMA address（依构建配置）
├── data_off ──────────────→ valid data 相对 buf_addr 的偏移
├── data_len                当前 segment 长度
├── pkt_len                 整条 packet chain 总长度
├── nb_segs / next          multi-segment chain
├── port / packet_type
├── ol_flags                RX result / TX offload request
├── hash.rss                RSS hash
├── refcnt                  shared ownership
└── pool                    归还目标 mempool
```

`buf_addr + data_off` 才是 CPU 看到的包头。`pkt_len` 不一定等于 `data_len`；scatter receive、jumbo frame 或 chained TX 都可能让一个 packet 跨多个 mbuf。`ol_flags` 也有双重含义：RX 时 PMD 写入 checksum/VLAN/RSS 等结果，TX 时应用用它请求 checksum、TSO 等 offload。

源码把 RX 热字段集中在前部，尽量让常见解析少触碰 cache line。所谓“zero-copy”也应准确理解：DPDK 避免在内核和应用之间再复制 packet bytes；CPU 仍要读取 header，转发时 NIC 仍通过 DMA 读取 packet data，multi-stage pipeline 还可能产生 cache-line ownership transfer。

## 7. `rte_mempool`：固定对象、每核缓存与所有权

[`rte_pktmbuf_pool_create()`](https://github.com/DPDK/dpdk/blob/d55ccd4e6de64e3f797f60de9e81f1d60f849775/lib/mbuf/rte_mbuf.c) 不是逐个 `malloc` mbuf。它创建 fixed-size object pool，初始化 pool-private mbuf layout，再对每个对象调用 mbuf initializer。backend 常由 ring 或其他 mempool ops 管理，而前面还有 per-lcore cache。

当前 [`rte_mempool_generic_get/put`](https://github.com/DPDK/dpdk/blob/d55ccd4e6de64e3f797f60de9e81f1d60f849775/lib/mempool/rte_mempool.h) 的策略可以概括为：

```text
get:
  local cache 足够     → 本地 pop
  local cache 不足     → 向 backend 批量 refill，再返回对象
  请求本身过大         → 直接访问 backend

put:
  local cache 有空间   → 本地 append
  cache 将溢出         → 批量 flush 较冷的一部分到 backend
  返回批次过大         → 直接放回 backend
```

收益不是“分配算法更聪明”，而是绝大多数 get/put 不碰共享 head/tail，减少 atomic、cache-line bouncing 和 coherence traffic。代价也很真实：对象可能滞留在其他 lcore 的 cache 中，所以某个 core 从 backend 得到 `-ENOENT`，不等于全系统所有 cache 都为空。

NUMA 参数不是装饰。`rte_pktmbuf_pool_create(..., socket_id)` 决定对象尽量从哪个 socket 的 memory heap 分配。理想布局是：

```text
NIC PCIe root on Node 0
  → RX queue polled by Node 0 core
  → descriptor/mempool allocated on Node 0
  → application state also local to Node 0
```

只 pin core，却把 mbuf pool 放在远端 NUMA node，仍会把每包路径变成跨 socket memory traffic。

## 8. `rte_ring`：无锁不等于没有顺序

[`struct rte_ring`](https://github.com/DPDK/dpdk/blob/d55ccd4e6de64e3f797f60de9e81f1d60f849775/lib/ring/rte_ring_core.h) 把 producer 和 consumer 状态放在不同的 cache-aligned 区域，避免双方频繁写同一 cache line。逻辑 index 以 `uint32_t` 单调回绕，仅访问数组时用 mask 映射到容量范围。

默认 MP/MC enqueue 的核心不是一句 CAS，而是三阶段协议：

```text
1. reserve producer head       多 producer 用 CAS 竞争区间
2. copy objects into slots     写入真正 payload pointer
3. publish producer tail       release-store，按前序完成顺序推进
```

consumer 必须在 tail 可见以后才读取 slot；acquire/release ordering 建立了“先写元素、后发布可见性”的 happens-before。后来的 producer 即使先写完，也不能跳过仍未完成的前序 producer 直接推进公共 tail，否则 consumer 会读到未初始化的洞。

ring 还支持 SP/SC、MP/MC、RTS、HTS，以及 fixed bulk 和 variable burst。若拓扑已保证唯一 producer/consumer，SP/SC 可以消除不必要竞争；若猜错了并发模型，得到的不是一点性能损失，而是正确性问题。

## 9. 最小程序如何落到 PMD

[`examples/skeleton/basicfwd.c`](https://github.com/DPDK/dpdk/blob/d55ccd4e6de64e3f797f60de9e81f1d60f849775/examples/skeleton/basicfwd.c) 是最合适的入口。当前例子使用 1024 个 RX/TX descriptor、8191 个 mbuf、250 个 mempool cache 和 32 包 burst：

```text
rte_eal_init
  → rte_pktmbuf_pool_create
  → rte_eth_dev_configure
  → rte_eth_rx_queue_setup
  → rte_eth_tx_queue_setup
  → rte_eth_dev_start
  → loop:
       rte_eth_rx_burst
       rte_eth_tx_burst
       free packets not accepted by TX
```

例子还会检查 NIC socket 与 polling lcore socket 是否一致。这条 warning 揭示了 DPDK API 的风格：它允许不理想配置继续工作，把 topology policy 留给应用。

### ethdev fast-path dispatch

[`rte_eth_rx_burst()`](https://github.com/DPDK/dpdk/blob/d55ccd4e6de64e3f797f60de9e81f1d60f849775/lib/ethdev/rte_ethdev.h) 是 inline wrapper，核心逻辑近似：

```c
p = &rte_eth_fp_ops[port_id];
qd = p->rxq.data[queue_id];
nb_rx = p->rx_pkt_burst(qd, rx_pkts, nb_pkts);
```

[`eth_dev_fp_ops_setup()`](https://github.com/DPDK/dpdk/blob/d55ccd4e6de64e3f797f60de9e81f1d60f849775/lib/ethdev/ethdev_private.c) 在设备配置阶段，把 PMD callback 和每个 queue 的 private pointer 复制到 cache-aligned `rte_eth_fp_ops`。所以快路径不必层层查询 generic device object：给定 port/queue 后，直接取 queue data，调用已选择的 PMD 函数。

这也是为什么 PMD 可以同时提供 scalar、bulk、SSE、AVX2、AVX-512 等实现：控制面只在配置或启动时选择一次，packet loop 中不需要反复判断硬件能力。

## 10. ixgbe RX：从 DD bit 到可消费 mbuf

以 [`ixgbe_recv_pkts()`](https://github.com/DPDK/dpdk/blob/d55ccd4e6de64e3f797f60de9e81f1d60f849775/drivers/net/intel/ixgbe/ixgbe_rxtx.c) 的通用 scalar path 为例，一次收包实际做了：

```text
read descriptor status/DD
  → acquire fence
  → copy completed descriptor fields
  → allocate replacement mbuf
  → swap software ring entry
  → write replacement mbuf IOVA to descriptor
  → initialize returned mbuf data_len/pkt_len/port...
  → decode RSS/checksum/VLAN/offload metadata
  → append mbuf to rx_pkts[]
  → periodically write RDT doorbell
```

第一条系统不变量是：

> PMD 必须先观察到 descriptor 的 DD completion，再消费 NIC 写回的其他字段。

NIC 和 CPU 是两个并发主体。没有正确 memory ordering，CPU 可能看到状态完成，却读到旧 length 或旧 metadata。

第二条不变量是补洞顺序。PMD 不能先把 descriptor 还给 NIC，再发现没有 replacement buffer。当前 scalar path 若 raw mbuf allocation 失败，会停止处理当前 descriptor并记录 alloc failure；这样已经收到的 packet 仍留在 software-owned buffer 中，但持续缺内存最终会让 RX ring 无可用 descriptor 并丢包。

RX queue setup 在 [`ixgbe_dev_rx_queue_setup()`](https://github.com/DPDK/dpdk/blob/d55ccd4e6de64e3f797f60de9e81f1d60f849775/drivers/net/intel/ixgbe/ixgbe_rxtx.c) 中同时建立两类结构：CPU 使用的 queue/software ring，以及通过 DMA zone 分配的 hardware descriptor ring。前者保存 mbuf 指针和索引；后者的 `addr` 给 CPU、`iova` 给 NIC；`qrx_tail` 则指向 MMIO RDT register。

[`ixgbe_set_rx_function()`](https://github.com/DPDK/dpdk/blob/d55ccd4e6de64e3f797f60de9e81f1d60f849775/drivers/net/intel/ixgbe/ixgbe_rxtx.c) 会依据 scatter、LRO、offload、queue threshold、CPU SIMD width 等条件选择 scattered、bulk、vector 或 scalar 路径。“调用 `rte_eth_rx_burst`”因此不是承诺某个固定实现，而是进入当前设备配置允许的最短实现。

## 11. ixgbe TX：ownership 在什么时候转移

[`ixgbe_xmit_pkts()`](https://github.com/DPDK/dpdk/blob/d55ccd4e6de64e3f797f60de9e81f1d60f849775/drivers/net/intel/ixgbe/ixgbe_rxtx.c) 做的是反向过程：

```text
if free descriptors low:
    clean descriptors completed by NIC
for each mbuf chain:
    count data descriptors + optional context descriptor
    check ring capacity
    encode checksum/TSO/VLAN context
    write each segment IOVA + length
    set EOP on final descriptor
    set RS according to cleanup threshold
rte_wmb()
write TDT MMIO doorbell
```

`rte_wmb()` 位于 doorbell 之前，是另一条关键不变量：NIC 被通知之前，descriptor 内容必须已经对设备可见。否则 NIC 可能沿新 tail 读取尚未完整写好的 descriptor。

TX ownership 的边界以返回值为准。`rte_eth_tx_burst()` 接受的 mbuf 从此由 PMD 持有，直到 NIC completion 后由 cleanup 回收；没有被接受的尾部 mbuf 仍归应用，`basicfwd` 必须显式 free。应用若忽略部分发送，会造成 mbuf leak；应用若立即修改已接受 mbuf，则会与 NIC DMA 并发。

`ixgbe_set_tx_function()` 同样根据 offload、multi-segment 假设和 SIMD 能力选择 vector/simple/full-featured 路径。offload 不是无条件免费：复杂功能可能需要 context descriptor，也可能把队列从最简 SIMD path 推回功能更完整但更长的实现。

## 12. RSS：网卡完成的 hash partition

多 queue 不会自动带来扩展性，还需要把 flow 稳定地映射到 queue。RSS 通常对选定 header 字段计算 Toeplitz hash，再用 redirection table（RETA）把 hash bucket 映射到 RX queue：

```text
packet headers
  → configured RSS fields + hash key
  → Toeplitz hash
  → RETA[hash bits]
  → RX queue
  → polling lcore
```

ixgbe 的 [`ixgbe_rss_configure()`](https://github.com/DPDK/dpdk/blob/d55ccd4e6de64e3f797f60de9e81f1d60f849775/drivers/net/intel/ixgbe/ixgbe_rxtx.c) 在应用未更新 RETA 时，按 `0..nb_rx_queues-1` 轮转填硬件表；随后写 RSS key 和参与 hash 的 IPv4/IPv6/TCP/UDP 字段。[`ixgbe_dev_rss_reta_update()`](https://github.com/DPDK/dpdk/blob/d55ccd4e6de64e3f797f60de9e81f1d60f849775/drivers/net/intel/ixgbe/ixgbe_ethdev.c) 则按 mask 局部修改 RETA register。

RSS 解决的是并行分区，不保证均衡。少数 elephant flow、输入 key 倾斜或 RETA 配置不佳，都会使某些 queue 过载。它也不替代应用层同步：如果同一 flow 的 state 被多个阶段共享，跨核 handoff 仍可能发生。

从数据库角度看，RSS 很像 NIC 执行的 hash shuffle：hash key 决定 partition，RETA 类似可调的 bucket-to-worker map，queue 是 partition buffer，lcore 是消费 worker。

## 13. Run-to-Completion 还是 Pipeline

[`l2fwd`](https://github.com/DPDK/dpdk/blob/d55ccd4e6de64e3f797f60de9e81f1d60f849775/examples/l2fwd/main.c) 的核心结构是 Run-to-Completion（RTC）：一个 lcore 轮询分配给自己的 port/queue，在本核完成改包和发送。它的优势是 packet ownership 很少跨核，cache locality 好，延迟路径短。

```text
RXQ0 → Core0: RX → parse → lookup → update → TX
RXQ1 → Core1: RX → parse → lookup → update → TX
```

Pipeline 会用 `rte_ring` 把阶段拆到不同 core：

```text
RX Core → ring → Parse/Lookup Workers → ring → TX Core
```

它适合阶段成本悬殊、需要独立伸缩，或必须把慢阶段隔离的场景；代价是 enqueue/dequeue、跨核 cache-line transfer、额外排队和更复杂的 backpressure。`examples/distributor`、`packet_ordering` 等例子能看到 ring burst handoff。

选择标准不是“pipeline 更并行”：

- 每包处理短、state 可按 flow 分区时，优先 RTC。
- 某阶段很重、动态性强或资源类型不同，才考虑 pipeline。
- pipeline 必须明确 ring 满时谁 drop、谁 retry、谁负责 free mbuf。
- 任何拓扑都应让 queue、core、mempool 和 state 尽量位于同一 NUMA node。

## 14. Offload 与 `rte_flow`：控制面下推，快路径绕开 CPU

checksum、VLAN、TSO、RSS 多为 fixed-function offload。应用通过 port/queue configuration 或 mbuf `ol_flags` 声明需求，PMD 转成 descriptor bits 和 hardware registers。

[`rte_flow`](https://github.com/DPDK/dpdk/blob/d55ccd4e6de64e3f797f60de9e81f1d60f849775/lib/ethdev/rte_flow.c) 更接近统一的 match/action 控制面：pattern 描述 Ethernet/VLAN/IP/TCP/隧道等匹配，action 可以 queue、RSS、drop、mark、count 等。通用层的调用关系是：

```text
rte_flow_validate/create(port, attr, pattern, actions)
  → rte_flow_ops_get(port)
  → ethdev dev_ops.flow_ops_get
  → PMD-specific rte_flow_ops.validate/create
  → translate to NIC rule / firmware object
```

源码显示 `rte_flow_validate()` 和 `rte_flow_create()` 最终都调用 PMD callback；driver 没有 `flow_ops_get` 或 callback 就返回不支持。因此 `rte_flow` 的可移植性是语法和能力发现层面的，不代表每块 NIC 支持相同 pattern、action、优先级、规则数量和更新语义。

它与数据库 predicate pushdown 很像：规则安装是控制面成本，一旦成功，未命中流量可以在进入 CPU 前被 drop 或 steer。但真实评估不能只问“能否下推”，还要测 rule setup latency、capacity、counter consistency、更新原子性、fallback 和设备重置后的恢复。

SmartNIC/DPU 和 GPUDirect 继续移动这条边界：前者把 classify/crypto/virtual switching 等更多逻辑移入设备，后者试图减少 `NIC → host DRAM → CPU → GPU` 的 staging。它们改变的是 ownership、failure domain 和 data movement，不是把软件复杂度消掉。

## 15. DPDK 与 XDP/AF_XDP：不是简单替代关系

XDP 在 Linux driver 的早期 RX 点运行 eBPF，可在构造完整 `sk_buff` 前完成 pass/drop/redirect；AF_XDP 再用 UMEM 和 RX/TX/FILL/COMPLETION rings 把 packet 交给用户态。DPDK 自己甚至包含 [`net_af_xdp` PMD](https://github.com/DPDK/dpdk/blob/d55ccd4e6de64e3f797f60de9e81f1d60f849775/drivers/net/af_xdp/rte_eth_af_xdp.c)，说明两者并非互斥阵营。

从该 PMD 可以看到两种 RX 路径：copy mode 从 UMEM packet 拷入 mbuf；zero-copy mode 让可映射的 mbuf/mempool memory 直接成为 UMEM。zero-copy 是否成立取决于 kernel、NIC driver、queue 和 memory layout，不能仅凭使用 AF_XDP 就宣称零拷贝。

| 维度 | 原生 PCI PMD 的 DPDK | XDP / AF_XDP |
|---|---|---|
| 设备控制 | 用户态 PMD 直接管理 queue | Linux driver 管设备，XDP/AF_XDP 接入 |
| kernel integration | 弱，需要另建控制/协议能力 | 强，可与 Linux network stack/eBPF 配合 |
| zero-copy | DMA buffer 是核心模型 | AF_XDP ZC 依赖 driver 与配置，也有 copy mode |
| dedicated core | 常见 | 视 busy-poll 和负载而定 |
| 可编程位置 | 用户态 packet loop / NIC flow | driver early path 的 eBPF + 用户态 |
| 典型取舍 | 极致、可控的数据面 | Linux-native 部署、容器、安全和渐进接入 |

如果系统必须保留 Linux routing、namespace、cgroup 与 eBPF 运维体系，XDP/AF_XDP 往往更自然；若应用愿意接管设备并围绕专用 queue/core 构建完整 dataplane，原生 PMD 的控制更直接。

## 16. 为什么 burst 快，但不是越大越好

把一次 burst 的固定成本记为 `C_f`，每包实际处理记为 `C_p`，批次为 `N`，近似单包成本是：

```text
C_packet ≈ C_f / N + C_p
```

固定成本包括函数进入、head/tail 读取、doorbell、mempool backend 操作和部分 memory barrier。增大 N 能摊薄这些成本，也给 compiler/SIMD/prefetch 更多空间。但 N 太大又会提高排队时延、扩大工作集、延迟 TX flush，并让某个繁忙 queue 长时间独占 core。

所以 `BURST_SIZE=32` 是常见折中，不是常数真理。真实实验至少应固定并交叉以下变量：

| 维度 | 建议值 | 主要观察 |
|---|---|---|
| packet size | 64 / 128 / 512 / 1500 B | 小包 pps 与大包 bandwidth 边界 |
| burst | 1 / 8 / 16 / 32 / 64 | cycles/packet、尾延迟、drop |
| queue/core | 1 / 2 / 4 / 8 | scaling、RSS skew、contention |
| NUMA | core local/remote × memory local/remote | LLC miss、UPI/IF traffic、bandwidth |
| offload | checksum/RSS/TSO on/off | PMD path 与 CPU cost 变化 |

指标不能只有 Gbps。64-byte packet 应重点看 Mpps、cycles/packet、RX missed、mempool alloc failure；还应配合 `perf stat/record/c2c`、`numastat` 和 NIC queue counters。大包更容易先撞到 link/PCIe/memory bandwidth，小包更容易暴露 descriptor 与控制路径成本。

`testpmd` 是执行这套矩阵的实验室，而不是最终应用模板。它在 `app/test-pmd` 中提供 `io`、`mac`、`rxonly`、`txonly`、`csum` 等 forwarding engine，并能查看 queue stats、RSS、offload 与 flow rule。正确用法是一次只改变一个变量，保留 EAL 参数、port topology、packet generator 和运行时长。

## 17. 把 DPDK 映射回数据库执行引擎

DPDK 和向量化数据库共享的不是表面上的“batch API”，而是对固定开销、数据局部性和 ownership 的相同态度。

| DPDK | 数据库执行引擎 | 共同问题 |
|---|---|---|
| packet | tuple/row | 最小逻辑数据单元 |
| burst | vectorized batch | 摊薄调用和分派成本 |
| RX queue | input partition | 单 worker 局部消费 |
| RSS + RETA | hash partition/shuffle | key 到 partition/worker 的映射 |
| mbuf | tuple/block descriptor | metadata 与 payload 分离 |
| mempool cache | arena/object pool/thread cache | 避免通用 allocator 与共享争用 |
| rte_ring | exchange buffer | pipeline stage 间 ownership transfer |
| PMD callback | specialized operator/kernel | 控制面选实现，数据面走短路径 |
| core pinning | worker affinity | 保留 cache/NUMA locality |
| rte_flow | filter/routing pushdown | 在更靠近数据源的位置减少流量 |

数据库里一次 `next()` 返回一行，很像 burst=1：virtual dispatch、边界检查和状态机成本无法摊薄。vector-at-a-time 很像 RX burst：一次取得一批 descriptor/row id，在紧凑循环内运行 filter、hash 和 projection。Pipeline exchange 和 `rte_ring` 一样，能拆阶段，但也制造同步、排队和 cache ownership transfer。

更深的一层是 fast/slow path 分离。DPDK 在设备启动时选择 PMD callback，把能力检查留在控制面；数据库也应在 plan/codegen 阶段解决 type、nullability、encoding 和 implementation selection，让 hot loop 少做重复判断。

## 18. 七天源码学习与实验路线

这套路线不是每天“读一个库”，而是每天闭合一条因果链。

### Day 1：硬件路径

执行 `lspci -nn/-vv`、`numactl --hardware`，画出 NIC BDF、PCIe root 和 NUMA node。阅读网卡 descriptor 定义和 queue register，回答“谁写 descriptor、谁推进 head/tail、哪一步是 MMIO”。远程机器不要解绑承载 SSH/管理面的 NIC。

### Day 2：EAL、Hugepage 与 VFIO

检查 `/proc/meminfo`、IOMMU group 和 `dpdk-devbind.py --status`，跟一次 `rte_eal_init()`。画出 `VA → PA` 与 `IOVA → PA` 两条翻译，确认 memory 是在什么 socket 分配、何时映射给设备。

### Day 3：最小 dataplane

精读并运行 `basicfwd`，从 `rte_eth_rx_burst` 跟到 PMD callback。自己实现 `RX → 修改 MAC → TX`，专门处理 partial TX 和 port/queue validation。

### Day 4：mbuf、mempool、ring

打印 mbuf 核心字段，构造 multi-segment packet；改变 burst 和 mempool cache size。阅读 ring 的 head reservation、slot copy、tail publish，解释 SP/SC 为什么可以更短。

### Day 5：RSS、多核与 NUMA

运行 l2fwd/l3fwd，记录 queue-to-lcore mapping 和每 queue counters。改变 RETA、core affinity 和 mempool socket，做 local/remote 2×2 对照；再实现一个 ring-based pipeline，与 RTC 比较 cycles 和延迟。

### Day 6：testpmd 与 offload

固定 packet/burst/queue 矩阵，分别运行 `io/mac/csum` forwarding mode，观察 checksum、RSS、VLAN、TSO 对 PMD selection 和吞吐的影响；安装简单 `rte_flow` queue/drop/count rule 并验证硬件计数器。

### Day 7：完整追踪一个 PMD

按实际硬件选择 `mlx5`、`ice` 或 `ixgbe`，只追：

```text
PCI probe
  → ethdev creation
  → rx/tx queue setup
  → burst function selection
  → descriptor loop
  → mbuf ownership
  → doorbell / completion cleanup
```

不要试图线性读完整 driver。最终用 flame graph 和 cycles-per-packet breakdown 验证自己认为的热点，而不是把源码行数当作重要性。

## 19. 阅读源码时应抓住的系统不变量

到这里，DPDK 可以浓缩成十条检查表：

1. NIC descriptor 中是 IOVA，不是 `rte_mbuf *`。
2. CPU 通过 VA 访问 buffer；IOMMU 负责设备 IOVA 到 PA 的翻译和隔离。
3. RX 必须先观察 completion，再读取 descriptor 其余 write-back 字段。
4. RX descriptor 交还 NIC 前，replacement buffer 必须准备好。
5. TX doorbell 前，descriptor writes 必须对设备可见。
6. TX burst 接受之后 ownership 归 PMD；未接受的 mbuf 仍归应用。
7. ring 必须先写 slot 再发布 tail，MP producer 不能越过前序 reservation。
8. queue 最好单 lcore 消费，mempool 和 state 与 NIC/core NUMA-local。
9. offload capability、configuration 和实际选中的 PMD path 是三件不同的事。
10. 吞吐提升必须同时检查 drop、alloc failure、queue skew、尾延迟和 CPU cost。

这些不变量比 API 列表更耐版本变化。换成 ice、mlx5，descriptor layout 和 doorbell 细节会变；换成 AF_XDP，设备管理边界会变；但 DMA visibility、ownership、batching、locality 与 backpressure 仍然是问题核心。

## 20. 源码阅读地图与最终理解

推荐顺序是从最短闭环进入，再逐步下钻：

```text
examples/skeleton/basicfwd.c
  → lib/ethdev/rte_ethdev.h + ethdev_private.c
  → lib/mbuf + lib/mempool + lib/ring
  → drivers/net/<one PMD>/rx + tx
  → drivers/bus/pci
  → lib/eal/linux/eal.c + eal_memory.c + eal_vfio.c
  → RSS / rte_flow / testpmd / multi-process
```

如果反过来从 `lib/eal` 第一行一路读到底，很容易陷在平台兼容和初始化分支里，看不到 packet 的主线。以 `basicfwd` 为锚，每向下一层都回答“这个抽象替谁保存状态、消除什么成本、在哪一步移交 ownership”，源码会清楚很多。

最终，DPDK 的“快”可以拆成一条可验证的架构路径：

```text
PCI/VFIO 让用户态安全接管设备
  → hugepage/memseg 建立可 DMA、NUMA-aware 的 memory substrate
  → mempool/mbuf 固定对象和 packet ownership
  → RSS 把流量分区到 hardware queues
  → pinned lcore 批量轮询 queue
  → ethdev 直接分派到配置期选定的 PMD callback
  → PMD 按 memory ordering 协议消费/生产 descriptors
  → MMIO doorbell 把一批工作交给 NIC
```

这条路径没有魔法，也没有一个单独的“性能开关”。任何一环破坏 locality、引入共享写、错误处理 ownership、过早敲 doorbell，都会重新把 fixed cost 放回每一个 packet。理解 DPDK 的标志，不是背出多少 `rte_*` 函数，而是看到吞吐或延迟变化时，能沿 queue、descriptor、DMA、cache line、NUMA、PCIe 和 CPU cycles 一层层提出可验证的问题。

## 参考资料

- [DPDK 源码，本次阅读提交](https://github.com/DPDK/dpdk/tree/d55ccd4e6de64e3f797f60de9e81f1d60f849775)
- [DPDK Programmer's Guide](https://doc.dpdk.org/guides/prog_guide/)
- [DPDK Linux Getting Started Guide](https://doc.dpdk.org/guides/linux_gsg/)
- [DPDK Sample Applications User Guides](https://doc.dpdk.org/guides/sample_app_ug/)
- [testpmd Application User Guide](https://doc.dpdk.org/guides/testpmd_app_ug/)
- [Linux AF_XDP documentation](https://docs.kernel.org/networking/af_xdp.html)
