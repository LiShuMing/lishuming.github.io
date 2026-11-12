---
title: "【翻译】面向未来数据库的现代硬件：2026 回看"
date: 2026-08-09T00:00:00+08:00
lastmod: 2026-11-12T00:00:00+08:00
categories:
  - 数据库
tags:
  - 数据库
  - 硬件
  - LLM
  - 云计算
description: "翻译并回看《Modern Hardware for Future Databases》：从第一性原理拆解为什么云厂商内部大规模消化了 DPU、RDMA 与新型闪存，而公有云租户数据库依然围绕对象存储、NVMe 与 CPU/GPU 混合接口演进。"
draft: false
toc: true
---

**原文**: [Modern Hardware for Future Databases](https://transactional.blog/blog/2024-modern-database-hardware)

**发布日期**: 2024-11-19

**原文相关入口**: [另一份中文译本](https://pigsty.cc/zh/blog/db/future-hardware/)；[Reddit](https://www.reddit.com/search/?q=url%3Atransactional.blog/blog/2024-modern-database-hardware)、[Hacker News](https://hn.algolia.com/?query=transactional.blog/blog/2024-modern-database-hardware) 与 [Lobsters](https://lobste.rs/search?q=domain%3Atransactional.blog+title%3A%22Modern%20Hardware%20for%20Future%20Databases%22&what=stories) 上的讨论。

> We’re in an exciting era for databases, with hardware advances arriving across networking, storage, and compute. Each of these advances have the potential to reshape an optimal database architecture. Taken together, they make me hopeful that we’ll see meaningful architectural shifts in databases over the next decade, but also uncertain whether the necessary hardware will be accessible to most database builders.
{.quote-en lang="en"}

我们正处在数据库硬件快速演进的时期：网络、存储和计算都在变化，其中任何一项都可能改写数据库的最优架构。把这些变化放在一起看，未来十年很可能出现真正有意义的架构迁移；但这些硬件能否被大多数数据库开发者获得，仍然是一个疑问。

---

## 网络（NETWORKING）

> From a recent [talk by Stonebraker in HPTS 2024](https://muratbuffalo.blogspot.com/2024/09/hpts24-day-1-part-1.html), some benchmarking with VoltDB saw that ~60% of their server-side cycles went to the TCP/IP stack. VoltDB is already a database architecture whose goal was to remove as much not-query-processing work from serving requests as possible, so this is the extreme case. However, it still makes a valid point that the computational overhead of TCP is not small, and will become ever more noticeable as network bandwidth increases. This isn’t a new observation though, and there’s an escalating series of proposed solutions.
{.quote-en lang="en"}

根据 Stonebraker 在 [HPTS 2024 的演讲](https://muratbuffalo.blogspot.com/2024/09/hpts24-day-1-part-1.html)，VoltDB 的一项基准测试（Benchmark）显示，服务端约 60% 的 CPU 周期消耗在 TCP/IP 协议栈上。VoltDB 本就以尽量消除请求路径中与查询无关的工作为目标，因此这是一个极端案例；但它仍说明 TCP 的计算开销并不小，而且随着网络带宽增长会越来越显眼。对此，业界已经提出了一系列逐步激进的方案。

> One proposed solution is to replace TCP with another protocol that runs over UDP instead. QUIC is the frequently chosen example. However, this is misled.[1] [“It is a grossly inaccurate simplification, but at its simplest level, QUIC is simply TCP encapsulated and encrypted in a User Datagram Protocol (UDP) payload.”](https://blog.apnic.net/2022/11/03/comparing-tcp-and-quic/) The CPU overhead of [TCP and QUIC is also remarkably similar](https://www.fastly.com/blog/measuring-quic-vs-tcp-computational-efficiency). Diverging further from TCP and specializing in specific environments would be needed to materialize notable improvements, and there are papers like [Homa](https://networking.harshkapadia.me/files/homa/research-papers/its-time-to-replace-tcp-in-the-datacenter-v2.pdf) showing some improvements in datacenter environments. But even with a better protocol, the better optimization potential lies in reducing the overhead of the kernel networking stack.
{.quote-en lang="en"}

一种提议是用运行在 UDP 之上的协议替换 TCP，QUIC 是最常被提起的例子。不过，把 QUIC 当作低开销 TCP 替代品并不准确。[APNIC 的文章](https://blog.apnic.net/2022/11/03/comparing-tcp-and-quic/)用一种有意简化的说法指出，QUIC 仍然承担了类似 TCP 的可靠传输职责并额外提供加密；[Fastly 的测量](https://www.fastly.com/blog/measuring-quic-vs-tcp-computational-efficiency)也显示两者 CPU 开销很接近。要获得显著收益，需要像 [Homa](https://networking.harshkapadia.me/files/homa/research-papers/its-time-to-replace-tcp-in-the-datacenter-v2.pdf) 那样针对数据中心环境做更专门的设计。不过，即使更换协议，更大的优化空间通常仍在减少内核网络栈的成本。

> **原注 [1]：** QUIC 能改善连接迁移、握手和队头阻塞等问题，但不会天然改善稳态延迟与带宽；在某些场景下甚至可能更差。

> One way to reduce the amount of work the kernel has to do is by moving the computationally intensive but simple parts to the hardware. This has been happening incrementally over time with enhancements that [offload both segmentation and checksumming to the NIC](https://docs.kernel.org/networking/segmentation-offloads.html). A more recent enhancement of [KTLS](https://www.kernel.org/doc/html/v5.2/networking/tls-offload.html) allows offloading packet encryption in TLS to the NIC as well. Attempts at offloading all of TCP to the hardware, in the form of a [TCP Offload Engine (TOE)](https://wiki.linuxfoundation.org/networking/toe), have been systematically rejected by Linux maintainers. So these have been nice enhancements, but significant parts of the TCP stack still remain a responsibility of the kernel.
{.quote-en lang="en"}

减少内核工作量的一种方法，是把计算密集但逻辑相对简单的部分移到硬件。网卡已经逐步承担[分段与校验和卸载](https://docs.kernel.org/networking/segmentation-offloads.html)，[KTLS](https://www.kernel.org/doc/html/v5.2/networking/tls-offload.html) 还允许卸载部分 TLS 数据路径上的加密工作。相比之下，将整个 TCP 栈塞入硬件的 [TCP Offload Engine（TOE）](https://wiki.linuxfoundation.org/networking/toe)长期遭到 Linux 维护者的系统性拒绝。因此，渐进式卸载很有帮助，但 TCP 栈的相当一部分职责仍留在内核里。

> Thus another solution is to remove the kernel as the middleman between the NIC and the application. Frameworks such as [Data Plane Development Kit (DPDK)](https://www.dpdk.org/) permit userspace to poll the network card for packets, removing the overhead of interrupts, and keeping all the processing in userspace means no transitions into and out of the kernel. DPDK has also seen struggles in adoption, as it requires exclusive control of a NIC. One thus needs to have two NICs per host, one for DPDK and one for the OS and every other process. Marc Richards put together a nice [Linux Kernel vs DPDK benchmark](https://talawah.io/blog/linux-kernel-vs-dpdk-http-performance-showdown/), that ends with DPDK offering a 50% increase in throughput, followed by an enumeration of the slew of drawbacks one accepts to gain that 50%. It seems to be a tradeoff most databases aren’t interested in, and even ScyllaDB has mostly dropped its investment into it.
{.quote-en lang="en"}

另一条路是让内核退出网卡与应用之间的数据路径。[DPDK](https://www.dpdk.org/) 允许用户态（User Space）轮询网卡，省掉中断以及进出内核态（Kernel Space）的上下文切换开销。不过，DPDK 通常要求独占整块网卡，主机往往还得准备第二块网卡留给操作系统和其他进程。Marc Richards 的 [Linux 内核与 DPDK 基准测试](https://talawah.io/blog/linux-kernel-vs-dpdk-http-performance-showdown/)测得约 50% 的吞吐提升，随后也罗列了为换取这 50% 必须咽下的一长串工程代价。大多数数据库对这笔买卖兴趣不大，连早期重度投入的 ScyllaDB 后来也基本收缩了这条路线。

> Newer hardware presents an interesting new option: removing the CPU from the networking path. [RDMA (Remote Direct Memory Access)](https://www.naddod.com/blog/easily-understand-rdma-technology) offers verbs, a limited set of operations (essentially read, write, and 8-byte CAS) that can be performed entirely from within the NIC, with no CPU interaction. Cutting out the CPU means close to 1us of latency for a remote read, versus the >100us latency of TCP.
>
> As part of RDMA, the responsibility of packet loss and flow control is also pushed down entirely to the NIC.[2] Cutting out the CPU also means large volumes of data can be transferred without the CPU becoming the bottleneck. [TCP offload is a dumb idea whose time has come](https://scholar.google.com/scholar?cluster=4106138525527042387) is a fun read in this area. (From 2003!)
{.quote-en lang="en"}

更新的硬件提供了另一种选择：干脆把 CPU 请出网络数据路径。[RDMA（远程直接内存访问）](https://www.naddod.com/blog/easily-understand-rdma-technology)暴露了一组受限的 verbs 原语（主要是 `read`、`write` 和 8 字节 `CAS`），全部由网卡硬件直接完成，无需远端 CPU 介入。绕开 CPU 后，一次远程内存读取的延迟可逼近 1 微秒，而走 TCP 往往超过 100 微秒。同时，丢包检测与流控也一并下沉到了网卡硬件[2]，大批量数据传输不再轻易被 CPU 卡住。2003 年的老论文 [《TCP Offload Is a Dumb Idea Whose Time Has Come》](https://scholar.google.com/scholar?cluster=4106138525527042387)放在今天读来依然妙趣横生。

> **原注 [2]：** 为什么 RDMA 可以把丢包检测和流控下推到硬件，而 Linux 维护者拒绝为 TCP 做同样的事？关键在于 RDMA 暴露的是一套全新且受限得多的 API，网卡与主机软件之间的交互边界清晰可控得多。

> Having RDMA as a low-latency and high-throughput networking primitive changes how one can design databases. [The End of a Myth: Distributed Transactions Can Scale](https://www.vldb.org/pvldb/vol10/p685-zamanian.pdf/) shows that RDMA’s low latency lets the classic 2PL+2PC scale to large clusters. [Is Scalable OLTP in the Cloud a Solved Problem?](https://www.cidrdb.org/cidr2023/papers/p50-ziegler.pdf) pitches the idea of having shared writable page cache across nodes, because low latency means tighter coupling of components becomes feasible.
>
> RDMA isn’t just for OLTP databases either; BigQuery uses an [RDMA shuffle-based join](https://cloud.google.com/blog/products/bigquery/in-memory-query-execution-in-google-bigquery), because of the high throughput. Changing the basic numbers on latency and CPU utilization at a given throughput changes which design is the best, or unblocks new designs that previously weren’t considered feasible.[3]
{.quote-en lang="en"}

拥有 RDMA 这样低延迟、高吞吐的网络原语，会直接改写数据库架构的取舍边界。[《The End of a Myth: Distributed Transactions Can Scale》](https://www.vldb.org/pvldb/vol10/p685-zamanian.pdf/)证明，依托 RDMA 的极低延迟，经典的 `2PL + 2PC` 协议也能扩展到大规模集群；[《Is Scalable OLTP in the Cloud a Solved Problem?》](https://www.cidrdb.org/cidr2023/papers/p50-ziegler.pdf)则提出跨节点共享可写页面缓存（Shared Writable Page Cache），因为微秒级互连让紧密耦合的组件重新变得可行。RDMA 也不只属于 OLTP：BigQuery 同样利用高吞吐的[基于 RDMA Shuffle 的 Join](https://cloud.google.com/blog/products/bigquery/in-memory-query-execution-in-google-bigquery) 来加速分析执行。一旦延迟与单位吞吐 CPU 开销的基准常数发生数量级变化，架构优劣就会重新洗牌，过去被判“死刑”的设计也会重获新生[3]。

> **原注 [3]：** 编写 RDMA 程序时，建议采用 [libfabric](https://ofiwg.github.io/libfabric/) 来屏蔽不同网卡厂商与底层库的差异；[RDMAmojo](https://rdmamojo.com/) 是系统学习 RDMA 编程的绝佳博客。

> Lastly, there’s a class of even newer hardware that finishes the trend of placing even more computing power in the NIC itself, in the form of SmartNICs or Data Processing Units (DPUs). They permit arbitrary processing to be pushed down to the NIC, and potentially invoked in response to requests from other NICs.
>
> These are rather recent, and I’d suggest looking at [DPDPU: Data Processing with DPUs](https://scholar.google.com/scholar?cluster=14622696590036176289) for an overview, [DDS: DPU-Optimized Disaggregated Storage](https://scholar.google.com/scholar?cluster=12305794631120951674) for how to integrate them into a database, and [Azure Accelerated Networking: SmartNICs in the Public Cloud](https://www.microsoft.com/en-us/research/uploads/prod/2018/03/Azure_SmartNIC_NSDI_2018.pdf) for details about deploying them.
>
> In general, I expect SmartNICs to extend RDMA from simple reads and writes to general RPCs that fully bypass the CPU for requests that are computationally cheap to answer.
{.quote-en lang="en"}

沿着“把计算塞进网卡”的趋势走到头，便是 SmartNIC 与数据处理单元（DPU）。它们允许把任意处理逻辑下推到网卡上运行，甚至在收到远端网卡请求时直接触发执行。入门综述可看 [DPDPU: Data Processing with DPUs](https://scholar.google.com/scholar?cluster=14622696590036176289)，数据库存储解耦结合案例可看 [DDS: DPU-Optimized Disaggregated Storage](https://scholar.google.com/scholar?cluster=12305794631120951674)，大规模云端部署细节则可参考 [Azure Accelerated Networking](https://www.microsoft.com/en-us/research/uploads/prod/2018/03/Azure_SmartNIC_NSDI_2018.pdf)。总体而言，作者预计 SmartNIC 会将 RDMA 从简单的单边读写进一步延展为完全绕过宿主机 CPU 的轻量通用 RPC。

### 2026 回看：为什么网络卸载越彻底，离普通租户反而越远？

回看这条从内核 TCP 一路通往 SmartNIC/DPU 的演进链条，最值得先算清楚的是背后的物理账：**为什么在 VoltDB 这类系统里，网络栈会夸张到吃掉 60% 的 CPU？** 因为 VoltDB 把传统数据库里的缓冲池管理、重量级锁表和磁盘 I/O 全都砍掉了，在单核分区内存里触碰几行数据只需几百纳秒；可哪怕业务负载再轻，Linux 内核每收发一个包，照样要经历硬中断或 NAPI 软中断调度、分配并初始化两百多字节的 `sk_buff`、穿过 TCP 状态机与 `netfilter` 钩子，再做一次内核态到用户态的内存拷贝。当单向链路速率从 10GbE 跃升到 100GbE/400GbE，留给每个数据包的 CPU 时间预算被压缩到几十纳秒，协议栈的固定税自然成了头号瓶颈。

把原文讨论的五级方案放进同一张数据面演进图里，就能清楚看到每一次“请内核或 CPU 退场”所交换的工程代价：

![数据库网络数据路径卸载演进谱系：从内核协议栈到 SmartNIC/DPU](/images/diagrams/aug-dbpaper-hardware-offload-spectrum.svg)

站在 2026 年的节点核对，这五级阶梯在公有云上分化出了两条截然不同的命运线：

- **DPU 与 SmartNIC 确实全面普及了，但控制权留在了云厂商手里。** 无论是 [Azure Boost](https://learn.microsoft.com/en-us/azure/azure-boost/overview)、AWS Nitro 还是 [Google Titanium](https://cloud.google.com/titanium)，主流云平台都已默认用自研 DPU 接管虚拟网络转发、VPC 加密与远程块存储协议。原因很朴素：云厂商每从宿主机卸载一个核的虚拟化开销，就能直接多卖一个 vCPU 给客户；但一旦把网卡上的 ARM 核心或 P4 流水线开放给多租户随意烧写代码，故障隔离、固件热升级和虚拟机热迁移就会瞬间变成运维噩梦。因此，DPU 成了云底座的标配，却没有变成租户数据库可编程的协处理器。
- **高速低延迟网络供给大幅扩容，但优先绑定了 AI 加速器拓扑。** 大模型训练与分布式推理催生了海量的集体通信（Collective Communication）需求，像 GCP 的 A3 Ultra 等实例已公开交付 [RoCE 高速网络](https://cloud.google.com/compute/docs/accelerator-optimized-machines)，AWS EFA 也持续演进绕过内核的 [SRD（Secure Reliable Datagram）](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/efa.html)。但传统无损 RoCE 极度依赖精细的 PFC（优先级流控）与拥塞控制调优，稍有不慎就会引发跨交换机队头死锁，云厂商更愿意在物理拓扑受控的高单价 GPU/HPC 集群中开放它，而不是下沉给随时弹性漂移的通用 OLTP 虚拟机。
- **对绝大多数数据库内核而言，减少跨网往返与字节膨胀依然比换协议名字管用。** 正如原文预判的那样，把 TCP 换成 QUIC 并没有免掉加密和包处理的 CPU 税。只要跑在通用云实例上，利用 `io_uring` 攒批收发、列式字典与因子化压缩、客户端连接复用以及将过滤聚合推向存储侧，依然是投资回报率最高的工程路径。

---

## 存储（STORAGE）

> There are advances in storage devices that aim to improve the total cost of ownership for storage devices in specialized use cases. Manufacturers cleverly noted that one can read narrower stripes of magnetized HDD platters than the minimum stripe width than an HDD can write, and so one can overlap tracks to leave the minimal readable width.
>
> Thus, we gained [Shingled Magnetic Recording](https://www.storagereview.com/news/what-is-shingled-magnetic-recording-smr) HDDs, which introduced the concept of storage being split into zones that only support appends and erases. SMR HDDs are targeted at use cases like [object storage](https://dropbox.tech/infrastructure/four-years-of-smr-storage-what-we-love-and-whats-next) where access is infrequent but large volumes of data must be stored.
{.quote-en lang="en"}

一部分存储设备的演进瞄准了特定场景下的总拥有成本（TCO）。硬盘厂商巧妙地利用了一个物理事实：磁头读取所需的磁道宽度窄于写入磁头的最小宽度；只要让相邻磁道像屋顶瓦片一样部分重叠、仅留出最小可读宽度，就能显著提高盘片面密度。[叠瓦式磁记录（SMR）](https://www.storagereview.com/news/what-is-shingled-magnetic-recording-smr)机械盘由此诞生，并引入了只允许顺序追加（Append）与整区擦除的分区（Zone）概念，天然契合[对象存储](https://dropbox.tech/infrastructure/four-years-of-smr-storage-what-we-love-and-whats-next)这类低频访问、海量冷数据归档场景。

> Similar ideas have been applied to SSDs, and [Zonal SSDs](https://zonedstorage.io/docs/introduction/zoned-storage) also exist. Exposing zones within an SSD means that the drive doesn’t need to offer a Flash Translation Layer (FTL) or a complex garbage collection process. Similar to SMR, this reduces the cost of a ZNS SSD as compared to a “regular” SSD, but there’s an additional focus on application-driven[4] garbage collection being more efficient, thus decreasing total write amplification and increasing drive lifetime.
>
> Consider LSMs on SSDs, which already operate via incremental appending and large erase blocks. Removing the FTL between an LSM and the SSD opens [opportunity for optimizations](https://scholar.google.com/scholar?cluster=17379606248569225336).
{.quote-en lang="en"}

同样的思路搬到闪存上，就成了[分区命名空间 SSD（ZNS SSD）](https://zonedstorage.io/docs/introduction/zoned-storage)。把底层 Zone 暴露给主机后，SSD 内部就不必再维护庞大的闪存转换层（FTL）DRAM 映射表和黑盒垃圾回收（GC）。这不仅降低了盘体硬件成本，更让上层应用得以亲自接管数据放置与空间回收[4]，压低全链路写放大并延长闪存寿命。想想跑在 SSD 上的 LSM-Tree——它自己本来就在做增量追加和大块归并擦除；把夹在 LSM 与闪存颗粒之间信息不透明的 FTL 抽掉，立刻就打开了一大片[跨层协同优化的空间](https://scholar.google.com/scholar?cluster=17379606248569225336)。

> More recently, Google and Meta have collaborated on a proposal for [Flexible Data Placement (FDP)](https://www.micron.com/about/blog/storage/innovations/eliminating-the-io-blender-promise-of-flexible-data-placement), which acts more as a hint for grouping writes with related lifetimes than as a strict partitioning mechanism like ZNS. The goal is to enable an easier upgrade path where an SSD could ignore the FDP part of the write request and still be semantically correct, just with worse performance or write amplification.
{.quote-en lang="en"}

再往后，Google 与 Meta 联合提出了[灵活数据放置（FDP）](https://www.micron.com/about/blog/storage/innovations/eliminating-the-io-blender-promise-of-flexible-data-placement)。不同于 ZNS 强制要求严格顺序写的硬分区，FDP 更像是一个随写请求附带的“提示（Hint）”，告诉 SSD 哪些数据具有相近的生命周期、应该归拢到同一擦除块里。它的高明之处在于平滑的演进兼容性：即便遇到不支持 FDP 的旧盘直接忽略该提示，写入语义也丝毫不会出错，无非退回到原先较高的写放大水平。

> **原注 [4]：** 操作 SMR 机械盘可参考 [libzbc](https://github.com/westerndigitalcorporation/libzbc)，操作 ZNS SSD 可参考 [xNVMe](https://xnvme.io/api/c/nvme/xnvme_znd.html#functions)。

> Other improvements target not cost efficiency[5], but improving the set of features that storage devices support. Focusing on NVMe in particular, NVMe added a [copy command](https://www.snia.org/educational-library/towards-copy-offload-linux-nvme-2021), to remove the waste in reading and writing the same data. [Fused compare-and-write commands](https://files.futurememorystorage.com/proceedings/2013/20130812_PreConfD_Marks.pdf#page=46) allow a CAS operation to be pushed down to the drive itself, enabling crazy designs like pushing [Optimistic Lock Coupling](https://scholar.google.com/scholar?cluster=7804091931900436017) down into the drive itself.
>
> NVMe inherited the [Data Integrity Field (DIF)](https://lwn.net/Articles/548294/) / [Data Integrity Extensions (DIX)](https://oss.oracle.com/~mkp/docs/dix.pdf) support from SCSI, which allows pushing page checksums down into the drive. (Notably used by Oracle.) There are projects like [KV-SSD](https://blocksandfiles.com/2019/09/05/samsungs-potentially-groundbreaking-keyvalue-ssd/) that change the entire data model from storing blocks by index to storing objects by key, and head towards replacing software storage engines entirely. SSD manufacturers continue to make SSDs more capable of more operations over time.
{.quote-en lang="en"}

另一类改进不纯粹盯着成本效率[5]，而是不断丰富存储设备的指令语义。单看 NVMe 协议：新增的 [`Copy` 命令](https://www.snia.org/educational-library/towards-copy-offload-linux-nvme-2021)省去了做 Compaction 或搬移数据时“先读回主机内存再原样写回盘里”的无谓往返；[融合比较并写入（Fused Compare-and-Write）](https://files.futurememorystorage.com/proceedings/2013/20130812_PreConfD_Marks.pdf#page=46)允许把原子 `CAS` 直接下推到盘内，甚至催生了把[乐观锁耦合（Optimistic Lock Coupling）](https://scholar.google.com/scholar?cluster=7804091931900436017)塞进驱动器的激进设计。此外，NVMe 继承了 SCSI 时代的 [DIF](https://lwn.net/Articles/548294/) / [DIX](https://oss.oracle.com/~mkp/docs/dix.pdf)（Oracle 用得尤为深入），支持由盘体硬件直接校验数据页完整性；而像 [KV-SSD](https://blocksandfiles.com/2019/09/05/samsungs-potentially-groundbreaking-keyvalue-ssd/) 这样的尝试则干脆抛弃逻辑块寻址，直接按键存取对象，大有取代上层软件存储引擎之势。

> **原注 [5]：** 如果你原本盼着[持久内存（Persistent Memory）](https://pmem.io/pmdk/libpmem/)，Intel 已经砍掉了 [Optane / 3D XPoint](https://en.wikipedia.org/wiki/3D_XPoint) 产品线，这条路暂时断了。[Kioxia](https://americas.kioxia.com/en-ca/business/news/2021/memory-20210913-1.html) 与 [Everspin](https://investor.everspin.com/news-releases/news-release-details/everspin-technologies-unveils-persyst-simplifying-persistent) 等厂商仍在探索新型非易失介质，但作者撰文时还未看到成规模落地。

> As the penultimate step in SSD capabilities, SmartSSDs are coming into existence which allow for putting arbitrary compute into an SSD. [Query processing on SmartSSDs: Opportunities and challenges](http://pages.cs.wisc.edu/~yxy/cs764-f20/papers/SmartSSD.pdf) surveys their application to query processing tasks. Pushing filters to storage is always advantageous; I’ve regularly linked previous work like [PushdownDB](https://marcoserafini.github.io/assets/pdf/pushdown.pdf) leveraging S3 Select[6] as a great example on the analytics side.
>
> With SmartSSDs we get papers like [POLARDB Meets Computational Storage](https://www.usenix.org/conference/fast20/presentation/cao-wei). Even without specialized integration, there are arguments to be made that even transparent, in-drive compression can [close the gap between B+ trees and LSMs in write amplification](https://www.usenix.org/conference/fast22/presentation/qiao). Leveraging SmartSSDs is still a young field of research, but there’s incredible potential for impact.
{.quote-en lang="en"}

沿着 SSD 能力扩展再往前迈一步，就到了允许在盘内运行任意计算逻辑的 SmartSSD。[《Query Processing on SmartSSDs》](http://pages.cs.wisc.edu/~yxy/cs764-f20/papers/SmartSSD.pdf)系统梳理了它在查询处理中的潜力。把过滤谓词推向存储层向来稳赚不赔——分析领域里借助 S3 Select[6] 构建的 [PushdownDB](https://marcoserafini.github.io/assets/pdf/pushdown.pdf) 就是一个漂亮例证。到了 SmartSSD 硬件上，既有 [POLARDB Meets Computational Storage](https://www.usenix.org/conference/fast20/presentation/cao-wei) 这样的生产级协同，也有研究表明哪怕不做专门的引擎改造，单靠盘内硬件透明压缩就能[抹平 B+ 树与 LSM-Tree 之间的写放大鸿沟](https://www.usenix.org/conference/fast22/presentation/qiao)。

> **原注 [6]：** AWS 已于 2024-07-25 停止向新客户开放 [S3 Select](https://aws.amazon.com/blogs/storage/how-to-optimize-querying-your-data-in-amazon-s3/)，原作者推测其重心正转向更通用的 [S3 Object Lambda](https://aws.amazon.com/s3/features/object-lambda/)。

### 2026 回看：为什么“软提示 + 内存分层”跑赢了“激进改写设备语义”？

存储硬件这几年的分化，完美印证了一条系统铁律：**凡是要求上层软件推翻块设备契约才能用的硬件创新，普及速度一定远远慢于向下兼容的软提示（Hint）。**

最典型的对照正是 ZNS 与 FDP。ZNS 要求数据库彻底接管顺序写指针和显式 Zone Reset，一旦文件或 SSTable 大小与物理 Zone 容量没对齐，或者线上混跑了 WAL、元数据与多层 Compaction，主机侧就不得不自己再写半套 FTL。反观 FDP，[NVMe 2.1 规范](https://nvmexpress.org/wp-content/uploads/NVM-Express-Revision-Changes-2025.03.31.pdf)已将其正式纳入标准，目标直指[接近 1.0 的设备级写放大系数（WAF）](https://nvmexpress.org/nvmeflexible-data-placement-fdp-blog/)——数据库只需把热 WAL、短命 L0 文件与长命底层 SSTable 标记不同的放置句柄（Placement Handle），盘内控制器自然会把寿命相近的数据归入同一擦除块；即便迁移到不支持 FDP 的普通云盘上，代码一行不改也能照常运行。

与此同时，2024 年原文略显遗憾的“持久内存断代”，在 2026 年以另一种形态找到了出路：**CXL（Compute Express Link）内存池化与分层**。Optane 试图同时扮演“接近 DRAM 的延迟 + 断电不丢数据”，结果既贵又迫使引擎重写崩溃一致性协议；而 [CXL 规范](https://computeexpresslink.org/cxl-specification/)干脆放弃了字节级持久化执念，专注于打破单插槽内存容量墙。正如 Google 2025 年论文 [《Databases in the Era of Memory-Centric Computing》](https://research.google/pubs/databases-in-the-era-of-memory-centric-computing/)所剖析的，CXL 远端内存访问大约在百纳秒量级（高于本地 DRAM 的 ~80ns，却比 NVMe 快上百倍），拿它放高频争用的事务锁表并不划算，但用来承接温冷 Buffer Pool 页面、大型哈希/Shuffle 中间态（如 SIGMOD 2026 的 LakeMem）以及动辄数百 GB 的向量索引，性价比极高。

至于 SmartSSD 和 KV-SSD，它们至今没能成为通用数据库平台，卡脖子处同样不在芯片算力，而在**软件工程边界**：SSD 盘内那颗小型 ARM/FPGA 算力既敌不过宿主机多核 CPU，又缺乏统一的调试、限流、多租户隔离与热升级工具链。除了极少数软硬一体的专用系统，主流数据库在 LLM 与多模态浪潮下反而收敛到了更清晰的**三级存储分工**：用对象存储兜底廉价无限容量，用本地 NVMe 充当高带宽块/列段缓存，再用压缩与量化（Scalar/Product Quantization）在数据跨过 PCIe 总线前先把体积砍掉大半。

---

## 计算（COMPUTE）

> OLTP and OLAP spend their compute time on significantly different types of work, so we’ll address the potential advances for each separately.
{.quote-en lang="en"}

OLTP 与 OLAP 把计算时间花在了截然不同的指令模式上，因此需要拆开来看。

### 事务处理（TRANSACTION PROCESSING）

> In a recent VLDB, two powerhouses of database research put forth a position paper of [Cloud-Native Database Systems and Unikernels: Reimagining OS Abstractions for Modern Hardware](https://www.vldb.org/pvldb/vol17/p2115-leis.pdf), arguing that unikernels allow databases to specialize an OS for their exact needs.
>
> The early work on [VMCache](https://scholar.google.com/scholar?cluster=7903866005464261403) highlights the struggle in efficient database buffer management in particular, where one either accepts the complexity of [pointer swizzling](https://db.in.tum.de/~leis/papers/leanstore.pdf), or one hooks into the kernel and invokes `mmap()`-related syscalls frequently. Neither option is appealing, and unikernels instead offer direct access to virtual memory primitives.
>
> The effort required to develop unikernels is lowering as the area is getting more attention, and [Akira Kurogane](https://jp.linkedin.com/in/akira-kurogane) got [MongoDB running as a unikernel](https://www.linkedin.com/pulse/mongodb-booted-unikernel-os-akira-kurogane-vdf7c/) via [Unikraft](https://unikraft.org/) with little effort, and subsequent posts showed a bit of performance improvement without any MongoDB-internal changes.
>
> There’s been an endless joke that databases want to become the OS, as the desire for performance improvements would require more control over networking, filesystems, disk I/O, memory, etc., and unikernel databases offer exactly that as a tangible possibility.
{.quote-en lang="en"}

在近期一篇 VLDB 观点论文 [《Cloud-Native Database Systems and Unikernels》](https://www.vldb.org/pvldb/vol17/p2115-leis.pdf)中，两位数据库界重量级学者主张利用 Unikernel 让数据库按需定制专属的操作系统抽象。此前的 [VMCache](https://scholar.google.com/scholar?cluster=7903866005464261403) 就挑明了高效缓冲池管理的两难处境：要么硬啃 [Pointer Swizzling（指针混写）](https://db.in.tum.de/~leis/papers/leanstore.pdf)那套极其繁琐的状态机，要么频繁陷入内核调用 `mmap()` / `madvise()` 承受页表锁与 TLB Shootdown 开销。两条路都不痛快，而 Unikernel 允许数据库直接操纵硬件页表等底层虚拟内存原语。随着工具链日趋成熟，[Akira Kurogane](https://jp.linkedin.com/in/akira-kurogane) 已经借助 [Unikraft](https://unikraft.org/) 没费太大周折就让 [MongoDB 作为 Unikernel 启动运行](https://www.linkedin.com/pulse/mongodb-booted-unikernel-os-akira-kurogane-vdf7c/)，且在不改动一行内核代码的前提下拿到了一定性能收益。数据库“总想自己当操作系统”的老玩笑，第一次有了触手可及的落地路径。

> For data confidentiality beyond just TLS or disk encryption, secure enclaves allow execution of verifiably untampered code, where the data being operated on is protected even from a compromised operating system. Whereas a [Trusted Platform Module (TPM)](https://learn.microsoft.com/en-us/windows/security/hardware-security/tpm/tpm-fundamentals) allowed keys to be held securely within a machine, secure enclaves extend that protection to arbitrary code and data.
>
> This permits building databases that are tremendously more resilient to malicious compromise but with several constraints on their design. Microsoft has published on integrating [secure enclaves into Hekaton](https://blog.acolyer.org/2018/07/05/enclavedb-a-secure-database-using-sgx/), and has released the work as part of [SQL Server Always Encrypted](https://learn.microsoft.com/en-us/sql/relational-databases/security/encryption/always-encrypted-enclaves?view=sql-server-ver16). Alibaba has also published about their efforts in building [enclave-native storage engines](https://vldb.org/pvldb/vol14/p1019-sun.pdf) for enterprise customers worried about data confidentiality.
>
> Databases have a history of being able to sell security improvements through the vehicle of [regulatory compliance](https://www.fortanix.com/faq/confidential-computing/how-does-confidential-computing-help-with-regulatory-compliance-requirements), and secure enclaves are a meaningful improvement in data confidentiality.
{.quote-en lang="en"}

如果想要超越传输层 TLS 和静态落盘加密的数据机密性，安全飞地（Secure Enclave）允许在硬件隔离区中运行经远程证明未被篡改的代码——哪怕宿主操作系统内核本身已被攻破，飞地内正在处理的数据依然安全。相比只负责安全保管密钥的 [TPM](https://learn.microsoft.com/en-us/windows/security/hardware-security/tpm/tpm-fundamentals)，安全飞地把信任边界扩展到了任意代码与运行时内存。微软早已发表过[将安全飞地集成进内存引擎 Hekaton](https://blog.acolyer.org/2018/07/05/enclavedb-a-secure-database-using-sgx/) 的研究，并将其商业化为 [SQL Server Always Encrypted](https://learn.microsoft.com/en-us/sql/relational-databases/security/encryption/always-encrypted-enclaves?view=sql-server-ver16)；阿里云也发表过面向高敏政企客户的[飞地原生存储引擎](https://vldb.org/pvldb/vol14/p1019-sun.pdf)。数据库行业向来擅长借助[监管合规](https://www.fortanix.com/faq/confidential-computing/how-does-confidential-computing-help-with-regulatory-compliance-requirements)将安全升级转化为商业买单点，安全飞地正是这一脉络上的实质性跨越。

> After Spanner’s introduction of [TrueTime](https://sookocheff.com/post/time/truetime/), clock synchronization has become of notable interest for transaction ordering in geo-distributed databases. Each of the major cloud providers has an NTP offering that is tied to atomic clocks or GPS satellites ([AWS](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/set-time.html), [Azure](https://learn.microsoft.com/en-us/azure/virtual-machines/linux/time-sync#overview), [GCP](https://developers.google.com/time/faq#whatis)).
>
> This is of great utility to any similar design, like CockroachDB or Yugabyte, for which clock synchronization is vital for correctness, and conservatively wide margins of error degrade performance. AWS’s recent Aurora Limitless also [uses a TrueTime-like design](https://www.youtube.com/watch?v=a9FfjuVJ9d8&t=29m25s). This is the only mention of cloud-specific not-quite-hardware because it uniquely involves major cloud vendors exposing expensive hardware (atomic clocks) that users otherwise wouldn’t have considered buying for themselves.
{.quote-en lang="en"}

自打 Spanner 祭出 [TrueTime](https://sookocheff.com/post/time/truetime/) 以来，高精度时钟同步就成了跨地域分布式数据库事务定序的关键底座。如今三大公有云均已提供挂接原子钟或 GPS 卫星的时间同步服务（[AWS](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/set-time.html)、[Azure](https://learn.microsoft.com/en-us/azure/virtual-machines/linux/time-sync#overview)、[GCP](https://developers.google.com/time/faq#whatis)）。这对 CockroachDB、Yugabyte 一类架构意义重大：时钟误差界（Error Bound）不仅关乎外部一致性正确性，设得过于保守还会直接拉长 Commit Wait 等待延迟。AWS 推出的 Aurora Limitless 同样采用了[类 TrueTime 的有界时钟设计](https://www.youtube.com/watch?v=a9FfjuVJ9d8&t=29m25s)。这是全文唯一破例提及的“准硬件”云服务，因为它罕见地体现了云厂商把普通团队绝不会自掏腰包采购的昂贵硬件（原子钟）变成公共基础设施开放出来。

> Hardware transactional memory has had a rather ill-fated history. [Sun’s Rock processor](https://www.theregister.com/2007/08/21/sun_transactional_memory_rock/) featured hardware transactional memory right up until Sun was bought and Rock was shut down. Intel made two attempts at releasing it, and had to disable it both times.[7]
>
> There was some interesting work on the subject of [applying hardware transactional memory to in-memory databases](https://15721.courses.cs.cmu.edu/spring2019/papers/leis-icde2014.pdf), but other than finding some old CPUs for experimentation, we all must wait until a CPU manufacturer says they’re planning to make another attempt at it.
{.quote-en lang="en"}

硬件事务内存（HTM）的命途则颇为多舛。[Sun 的 Rock 处理器](https://www.theregister.com/2007/08/21/sun_transactional_memory_rock/)眼看就要把 HTM 带上舞台，却随着公司被收购而夭折；Intel 前后两次在量产 CPU 中推出 TSX，又两次被迫通过微码更新将其废掉[7]。学术界曾做过不少[将 HTM 用于内存数据库并发控制](https://15721.courses.cs.cmu.edu/spring2019/papers/leis-icde2014.pdf)的精彩探索，但眼下除了淘几颗老旧 CPU 跑跑实验，大家只能干等哪家芯片商愿意再赌一回。

> **原注 [7]：** Intel 第一次禁用 TSX 源于 [Haswell/早期 Broadwell 上的硬件勘误缺陷](http://techreport.com/news/26911/errata-prompts-intel-to-disable-tsx-in-haswell-early-broadwell-cpus)，第二次则栽在了[利用 TSX 计时侧信道绕过内核地址随机化（KASLR）的攻击](https://www.blackhat.com/docs/us-16/materials/us-16-Jang-Breaking-Kernel-Address-Space-Layout-Randomization-KASLR-With-Intel-TSX-wp.pdf)上；此外还有一段趣闻：有安全研究员[因为读错了 CTF 赛题，误打误撞挖出了另一处 Intel TSX 推测执行侧信道漏洞](https://blog.ret2.io/2019/06/26/attacking-intel-tsx/)。

#### 2026 回看：事务处理靠什么继续压榨单机与分布式延迟？

回看 OLTP 计算侧的四条线索，存活下来的全都是**不要求芯片推翻通用执行模型、且边界高度收敛**的能力：

- **Unikernel 指出的内核税完全真实，但工业界选择了“改良内核接口”而非“抛弃 Linux”。** 让每个数据库内核团队自己维护一套专属 Unikernel，意味着要一并接盘网卡驱动适配、安全补丁、BPF 火焰图剖析与容器编排生态的断层。现实中，Linux 的 `io_uring` 异步轮询、大页内存（HugePages）加上 LeanStore/Umbra 式的用户态指针混写（Swizzling），已经能用低得多的工程风险拿走八成收益。
- **有界时钟从“Spanner 独门秘籍”变成了云上分布式 OLTP 的关键加速器和分水岭。** 无论是 SIGMOD 2026 披露细节的 Aurora PostgreSQL Limitless，还是跨区域部署的 NewSQL 系统，能否拿到微秒级且带明确上下界保证的时钟（如 AWS `ClockBound`），直接决定了跨分片事务能不能省掉中心化授时器（TSO）的网络往返。
- **机密计算被企业级 AI/RAG 推上了前台，形态正从进程级飞地走向机密虚拟机（CVM）。** 企业把核心业务表和私有文档喂给云端检索与大模型时，“云管理员也不可见”成了硬性合规条款。相比早期受限于极小 EPC 内存和昂贵系统调用退出的 Intel SGX，AMD SEV-SNP、Intel TDX 与 NVIDIA H100 Confidential Computing 把隔离粒度扩大到了整台 VM 乃至 GPU 显存，大幅降低了已有数据库引擎的改造成本。
- **至于 HTM，数据库界早已彻底翻篇。** 与其指望硬件替软件兜底跨缓存行原子性并时刻提防推测执行侧信道，乐观锁耦合（OLC）、Epoch-based 内存回收与分区级确定性调度要可靠得多。

### 分析处理（ANALYTICAL PROCESSING）

> Companies are consistently founded to leverage specialized hardware to accelerate query processing and achieve better performance and cost efficiency than their CPU-only competitors. GPU-powered databases, like [Voltron](https://voltrondata.com/theseus.html), [HEAVY.ai](https://www.heavy.ai/), and [Brytlyt](https://brytlyt.io/), are the first step in this direction.
>
> I wouldn’t be overly surprised if Intel or AMD integrated graphics gained OpenCL support[8] sometime in the future, which would open the door to all databases being able to assume some amount of GPU capabilities on a much wider set of hardware configurations.
{.quote-en lang="en"}

每隔几年都会涌现一批创业公司，试图利用专用硬件加速分析查询，在性能和性价比上击败纯 CPU 引擎——诸如 [Voltron](https://voltrondata.com/theseus.html)、[HEAVY.ai](https://www.heavy.ai/) 与 [Brytlyt](https://brytlyt.io/) 等 GPU 数据库便是先行者。作者甚至猜想，未来某天如果 Intel 或 AMD 的核显普及了成熟的 OpenCL 支持[8]，所有数据库或许都能在更广泛的普通服务器配置上默认假定存在可用的 GPU 算力。

> **原注 [8]：** 原作者补充说，OpenGL Compute Shader 其实已经能在核显上跑较通用的可移植计算了，只是他还没见过正经拿它来写数据库查询引擎的论文。

> There are also opportunities for using even more power-efficient hardware. The newest Neural Processing Units/Tensor Processing Units have already been shown to be adaptable into query processing in work like [TCUDB: Accelerating Database with Tensor Processors](https://dl.acm.org/doi/pdf/10.1145/3514221.3517869). A few companies have attempted to utilize FPGAs. [Swarm64](https://dbdb.io/db/swarm64) tried (and failed?) at this market. AWS made its own effort as [Redshift AQUA](https://aws.amazon.com/blogs/aws/new-aqua-advanced-query-accelerator-for-amazon-redshift/).
>
> Going as far as ASICs seems to not be worth it for even the largest companies, as even Oracle [stopped their SPARC development in 2017](https://www.hpcwire.com/2017/09/07/oracle-layoffs-reportedly-hit-sparc-solaris-hard/). I’m not overly optimistic about FPGAs through ASICs as memory bandwidth will be the primary bottleneck at some point anyway, but [ADMS](https://adms-conf.org/) is the conference[9] to follow for papers in this overall area.
{.quote-en lang="en"}

能效比更高的专用芯片也激发了不少探索：例如 [TCUDB](https://dl.acm.org/doi/pdf/10.1145/3514221.3517869) 把关系查询算子映射到了张量处理器（NPU/TPU）上；[Swarm64](https://dbdb.io/db/swarm64) 曾闯荡过 FPGA 数据库加速市场（大概率已折戟），AWS 也曾自研过基于 FPGA/自定义芯片的缓存加速层 [Redshift AQUA](https://aws.amazon.com/blogs/aws/new-aqua-advanced-query-accelerator-for-amazon-redshift/)。至于一步到位造数据库专用 ASIC，哪怕强如行业巨头往往也算不平经济账——连一贯软硬通吃的 Oracle 都在 2017 年[关停了带 SQL 加速指令的 SPARC 芯片研发](https://www.hpcwire.com/2017/09/07/oracle-layoffs-reportedly-hit-sparc-solaris-hard/)。作者对 FPGA 乃至 ASIC 路线总体持保留态度，因为分析查询推到极致，终究要撞上内存带宽墙；对这个方向感兴趣的读者可以持续关注 [ADMS](https://adms-conf.org/) 上的前沿论文[9]。

> **原注 [9]：** 严格来讲，ADMS 是挂靠在顶会 VLDB 旗下的专题研讨会（Workshop）。

#### 2026 回看：分析处理与 LLM 时代的“内存带宽与 PCIe 铁律”

原文那句“内存带宽终将成为头号瓶颈”，在 2026 年被验证得淋漓尽致。LLM 浪潮让数据中心塞满了高算力 GPU，软件栈生态（围绕 CUDA、Triton 以及通过 Substrait 对接现有 SQL 前端的加速层，而非原文猜测的 OpenCL）也空前繁荣，但**纯 GPU 托管整座数仓依然不是主流，CPU + GPU 按阶段协同才是生产常态**：

- **算得快抵不过搬得慢。** 一块现代数据中心 GPU 拥有数 TB/s 的 HBM 显存带宽，可一旦工作集溢出几十到上百 GB 的昂贵显存、需要走主机 PCIe 总线按需换入列块，有效带宽立刻跌落近两个数量级。只要一条复杂查询里夹杂了长字符串正则、复杂窗口或未覆盖算子导致数据在主机内存与显存之间往返横跳（参见 CIDR 2026 的 Sirius 分析），Kernel 省下的几毫秒瞬间就会赔光。
- **最先跑通商业闭环的是计算密度极高的子任务卸载与向量检索。** 传统关系型扫描每读一个字节只做寥寥几次比较求和（算术强度极低），很难喂饱 GPU；但高维向量索引构建（如 HNSW / CAGRA 图构建）、大规模距离矩阵计算与量化编码，恰恰具备高并行度与高算术强度。因此，[NVIDIA cuVS](https://docs.nvidia.com/cuvs/home/) 与 [Amazon OpenSearch Service](https://docs.aws.amazon.com/opensearch-service/latest/developerguide/gpu-acceleration-vector-index.html) 都收敛到了同一种务实模式：**按需拉起 GPU 快速构建向量索引，落盘转换后交还廉价大内存 CPU 实例承担日常低延迟带过滤检索**；与此同时，[pgvector](https://github.com/pgvector/pgvector) 等扩展证明了向量类型、半精度/二值量化与标量混合过滤正在直接内化为常规数据库的标准算子，而不是逼每个业务都额外部署一套孤立的专用硬件库。

---

## 云端可用性（CLOUD AVAILABILITY）

> To finally address the depressing elephant in the room, none of these hardware advancements matter if they’re not accessible. For today’s systems, that means in the cloud, and the cloud doesn’t offer the forefront of hardware advancements to its customers.
{.quote-en lang="en"}

最后，我们不得不直面房间里那头令人沮丧的大象：**再惊艳的硬件突破，只要租不到、用不上，对绝大多数数据库系统来说就等于零。** 在今天，可用性意味着能在公有云上按需获取，而云厂商恰恰极少把最前沿的底层硬件原汁原味地开放给租户。

> For networking, the situation isn’t fantastic. DPDK is the most advanced networking technology that’s somewhat easily accessible, as most clouds allow some instance types to have more than one NIC. AWS offers pseudo-RDMA in the form of [Secure Reliable Datagrams](https://scholar.google.com/scholar?cluster=7115577907027624509), which [was benchmarked](https://scholar.google.com/scholar?cluster=9445549416525532418) to be about halfway between TCP and RDMA. Real RDMA is only available on the High Performance Computing instances within Azure, GCP, and OCI. Only Alibaba offers [RDMA on general-purpose compute instances](https://www.alibabacloud.com/help/en/ecs/user-guide/erdma-overview).[10] SmartNICs are not available anywhere publicly.
>
> Some of this is for good reason: Microsoft has published papers that [deploying RDMA is hard](https://scholar.google.com/scholar?cluster=12305794631120951674). In fact, it’s [really hard](https://scholar.google.com/scholar?cluster=2434531805096404846). Even their papers about [actually succeeding in using RDMA](https://scholar.google.com/scholar?cluster=6986943445603020796) emphasize that it’s really hard. We’re nearing a full decade after Microsoft started using RDMA internally and it’s still not available in their cloud. I have no guesses as to if or when it will be.
{.quote-en lang="en"}

网络侧的现实相当骨感。由于多数云支持给单个实例挂载多张弹性网卡，DPDK 算是唯一比较容易跑起来的进阶方案。AWS 提供了名为 [SRD（安全可靠数据报）](https://scholar.google.com/scholar?cluster=7115577907027624509)的“准 RDMA”，[第三方实测](https://scholar.google.com/scholar?cluster=9445549416525532418)显示其性能大约卡在传统 TCP 与真 RDMA 半途。在 Azure、GCP 与 OCI 上，真正的 RDMA 仅限昂贵的 HPC 专用实例；唯独阿里云是个异数，把 [eRDMA 铺到了通用计算实例上](https://www.alibabacloud.com/help/en/ecs/user-guide/erdma-overview)[10]。至于向租户开放可编程 SmartNIC，更是一家都没有。平心而论，云厂商的保守并非毫无道理：微软接连发过好几篇论文坦言在数据中心[部署 RDMA 很难](https://scholar.google.com/scholar?cluster=12305794631120951674)、[规模化运维难上加难](https://scholar.google.com/scholar?cluster=2434531805096404846)，哪怕是讲他们[最终成功驾驭 RDMA 的论文](https://scholar.google.com/scholar?cluster=6986943445603020796)，字里行间也写满了血泪。距离微软内部大规模启用 RDMA 已近十年，这项能力依旧没对普通云租户敞开大门。

> **原注 [10]：** 阿里云的 eRDMA 基于兼容以太网的 iWARP 协议栈演进，延迟表现可能类似 AWS SRD 一样存在一定折损；原作者当时尚未查到独立的第三方对比基准。

> For storage, the situation isn’t really any better. The few times that SMR HDDs did reach consumers, it was as a drive that still presented itself as supporting a block storage API, and [consumers hated it](https://arstechnica.com/gadgets/2020/04/caveat-emptor-smr-disks-are-being-submarined-into-unexpected-channels/). ZNS SSDs seem similarly locked behind enterprise-only purchasing agreements.
>
> One might think that Intel discontinuing Optane-branded persistent memory and SSDs would mean that they’re not accessible on the cloud, but Alibaba still offers [persistent memory optimized instances](https://www.alibabacloud.com/help/en/tair/product-overview/persistent-memory-optimized-instances). The wonderful folk at [Spare Cores](https://sparecores.com/) actually provided me with `nvme id-ctrl` output from each cloud vendor, and no NVMe device they pulled presents itself as supporting nearly any optional features: copy, fused compare and write, data integrity extensions, nor multi-block atomic writes.[11]
>
> Alibaba is also the only cloud vendor that has invested into SmartSSDs with their [collaboration with ScaleFlux on PolarDB](https://www.usenix.org/conference/fast20/presentation/cao-wei). This still means SmartSSDs are not accessible to the general public, but even the paper acknowledges it’s “the first real-world deployment of cloud-native databases with computational storage drives ever reported in the open literature”.
{.quote-en lang="en"}

存储侧同样乏善可陈。SMR 机械盘前些年偷偷混入零售市场时，因为继续伪装成普通随机写块设备而引发了[消费者的强烈声讨](https://arstechnica.com/gadgets/2020/04/caveat-emptor-smr-disks-are-being-submarined-into-unexpected-channels/)；ZNS SSD 也基本锁死在大厂定制采购协议里。有趣的是，尽管 Intel 停产了傲腾（Optane），阿里云当时依然挂牌售卖[持久内存优化型实例](https://www.alibabacloud.com/help/en/tair/product-overview/persistent-memory-optimized-instances)。热心的 [Spare Cores](https://sparecores.com/) 团队帮作者抓取了各大公有云本地 NVMe 盘的 `nvme id-ctrl` 控制器信息，结果全军覆没——没有任何一家云盘向租户暴露 `Copy`、融合 `Compare-and-Write`、DIF/DIX 完整性校验或多块原子写等高级可选指令[11]。阿里云联合 ScaleFlux 在 [PolarDB 上落地计算型存储 SmartSSD](https://www.usenix.org/conference/fast20/presentation/cao-wei) 也是独一份的创举；尽管普通租户同样摸不到裸卡，但连论文自己都直言这是“公开文献报道的首例云原生数据库与计算型存储真实生产部署”。

> **原注 [11]：** AWS 虽然没有直接透传底层 NVMe 原子写指令集，但已在部分实例存储与 EBS 上支持防撕裂写（[Torn-write Prevention](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/storage-twp.html)，即 16KB 双倍页原子落盘）；GCP 早年也有过类似特性说明。

> On the compute side, the state finally gets a bit better. The cloud fully permits unikernels, and TPMs are widely accessible, but only [AWS](https://aws.amazon.com/ec2/nitro/nitro-enclaves/) and [Azure](https://learn.microsoft.com/en-us/azure/confidential-computing/confidential-computing-enclaves) support secure enclaves as far as I can tell. NTP servers with atomic or GPS clocks are made available, but only [AWS makes efforts](https://aws.amazon.com/blogs/compute/its-about-time-microsecond-accurate-clocks-on-amazon-ec2-instances/) at [promising error bounds](https://github.com/aws/clock-bound). Without promised error bounds, it is hard to critically rely on these clocks.
>
> (Hardware transactional memory isn’t available, but it’s hard to blame the clouds on that one.)
{.quote-en lang="en"}

到了计算侧，脸色总算好看了一点。公有云虚拟机完全可以跑自定义 Unikernel 镜像，虚拟 TPM 也很普及；就作者当时所见，提供成熟安全飞地的主要是 [AWS Nitro Enclaves](https://aws.amazon.com/ec2/nitro/nitro-enclaves/) 和 [Azure Confidential Computing](https://learn.microsoft.com/en-us/azure/confidential-computing/confidential-computing-enclaves)。虽然各家云都有挂接原子钟或 GPS 的 NTP 时间源，但唯独 AWS 下功夫交付了[微秒级精度时钟](https://aws.amazon.com/blogs/compute/its-about-time-microsecond-accurate-clocks-on-amazon-ec2-instances/)并配套开源了可编程读取的[误差界库 ClockBound](https://github.com/aws/clock-bound)——要知道，拿不到明确承诺的误差上下界，分布式数据库根本不敢把时钟塞进事务正确性逻辑里。（至于租不到硬件事务内存，这口锅确实怪不到云厂商头上。）

> The explosion of AI means there’s good money behind making more efficient compute available. GPUs are available in all clouds. AWS,[12] Azure, IBM, and Alibaba offer FPGA instances. (GCP and OCI don’t.) The unfortunate reality is also that faster compute only matters when compute is the bottleneck. Both GPUs and FPGAs suffer from having limited memory, and so one cannot maintain the database in their local memory.
>
> Instead, one relies on streaming data in and out of them, which means being limited by PCIe speeds. All of this would encourage thoughtful motherboard layout and bus design in an on-premise appliance, but that’s not feasible in the cloud.
{.quote-en lang="en"}

AI 浪潮砸下了真金白银去扩充高能效算力：GPU 在所有云上随处可租，AWS[12]、Azure、IBM 与阿里云也上架了 FPGA 实例（GCP 和 OCI 除外）。但残酷的物理现实在于，只有当算力本身真的是瓶颈时，堆算力才管用。GPU 和 FPGA 的板载内存容量极其有限，根本塞不下整座数据库；一旦退而求其次把数据流式搬进搬出，咽喉就立刻被 PCIe 总线带宽扼住了。放在私有化一体机时代（如 Oracle Exadata 或 Netezza），硬件架构师可以精心定制主板走线与直连拓扑；但在公有云租户眼里，这些全是碰不到的黑盒。

> **原注 [12]：** 最理想的数据流当然是走 P2P DMA（点对点直接内存访问），让数据从 NVMe 盘不经主机内存直接灌进加速器；但至少按作者查阅的 [AWS F1 实例官方 FAQ](https://github.com/aws/aws-fpga/blob/master/FAQs.md#f1-instance-and-runtime-tools-faqs)，这条路在云上并不通。

> Thus we end with my bleak view on the next generation of databases: no one[13] can build databases that critically depend on new hardware advancements until they’re made available, but no cloud vendor wants to deploy hardware that can’t be immediately used. The next generation of databases is held back by the cyclic dependency that it doesn’t yet exist.
{.quote-en lang="en"}

写到这里，作者给出了一个略显灰暗的终局判断：在新硬件普及开放之前，没有任何独立团队[13]敢把下一代数据库的核心路径押在它上面；可反过来，在看到现成的大规模软件买单之前，云厂商也绝不会贸然向租户铺开新硬件。下一代数据库就这样被困在了“因为尚未诞生，所以拿不到赖以诞生的硬件”的死锁里。

> **原注 [13]：** **云厂商自营数据库团队除外！** 微软和 Google 早已在自家数据中心内部大规模部署 RDMA 来喂养自营核心存储与数据库服务，却迟迟不把同等原语开放给外部租户。原作者甚至一直手痒想专门写一篇《论 RDMA 如何构成云巨头的不对称竞争壁垒》（*The Competitive Advantage of RDMA for Cloud Vendors*）。

> Alibaba is shockingly great though. They’re consistently at the forefront of making hardware advances available for everything. I’m surprised I don’t see Alibaba being frequently used for benchmarking in academia and industry correspondingly.
{.quote-en lang="en"}

全篇唯一的惊喜来自阿里云：他们在把前沿硬件能力尽早产品化、通用化这件事上激进得令人刮目相看。也正因如此，作者颇感费解——为什么国际学术界与工业界的横向基准测试里，很少见到有人直接拿阿里云实例来做实验舞台。

### 2026 回看：解开“硬件死锁”的真正答案——不对称吸收与分层抽象

重读原文结尾那个著名的“先有鸡还是先有蛋”死锁，2026 年的云上数据库生态其实已经给出了清晰答案：**新硬件根本没有在仓库里吃灰，而是沿着“云平台内部消化底层异构原语，向租户交付高阶契约”的边界完成了大规模落地。**

把云厂商的商业账与运维账摊开看，这种内外分野几乎是必然结果：

![2026 云端数据库硬件现实：云厂商内部卸载底座与租户可见接口分野](/images/diagrams/aug-dbpaper-cloud-hardware-reality.svg)

为什么云巨头自己写 `Aurora`、`Azure SQL Hyperscale`、`PolarDB`、`AlloyDB` 和 `S3` 时拼命用 DPU、RDMA、FDP/ZNS 和定制硬件卸载，转身给 EC2 / VM 租户挂载的却是阉割掉几乎所有高级指令的“朴素”块设备与标准网卡？

第一是**虚拟化可迁移性与爆炸半径隔离**。公有云售卖弹性的前提，是宿主机硬件必须像标准集装箱一样随时可替换。一个依赖标准块读写和内核/SRD 网络的虚拟机，底层宿主机坏了可以无感热迁移到跨代机型上；可一旦租户数据库直接在盘内注册了专属 NVMe `Copy` 描述符、绑定了某款 SmartSSD 固件算子，或者向远端内存发起了裸单边 RDMA `CAS`，热迁移、限流计费和多租户拥塞隔离就会立刻失灵。唯一打破惯例透传出来的底层能力——比如 AWS 消除 InnoDB/PG 双写缓冲的 **16KB 防撕裂原子写（Torn-Write Prevention）**、带明确误差界的 **ClockBound 原子钟**，以及面向 AI 大集群打包出租的 **RoCE + HBM 专网实例**——全都具备同一个特征：**语义极窄、收益极直接、不暴露底层设备内部状态。**

第二是**自营数据库服务的不对称护城河**，也就是原注 [13] 一针见血点破的事实。当云厂商把 RDMA 骨干网、页级冷热分层（如阿里云 SIGMOD 2026 的 CloudJump III）和日志重放页服务（如 Aurora 与 Hyperscale）封装在托管数据库内部时，硬件红利直接转化成了自营 PaaS 对第三方自建数据库的毛利优势与尾延迟代差。

看清这层物理与商业现实后，今天构建数据库内核与数据基础设施的工程策略反而格外清爽：

1. **如果你在云厂商内部打造自营底座**，应当毫不犹豫地打穿软硬边界——让缓存淘汰直接感知 NVMe/云盘/对象存储的页寿命（FDP 与分层存储），用 DPU 和单边 RDMA 重构日志多数派复制与远程内存池（如 LakeMem 的共享/私有双路径缓存）。
2. **如果你在构建跨云、开源或运行在通用租户实例上的数据库**，千万别把事务正确性或核心架构押在“某天各家云都会开放裸 RDMA verbs 和可编程 SmartSSD”的幻想上。真正跨云通吃的现代硬件红利只有四样：**高并发多核与 SIMD、廉价海量对象存储 + 本地 NVMe 极速缓存（配 `io_uring` 与防撕裂写）、有界误差时钟，以及按需租用的 GPU 批处理算力**。先把引擎的数据表示做紧（因子化、列式压缩、量化）、把跨 PCIe 与跨网络的无效字节搬运砍到极致，无论底层云底座明天换成哪一代 DPU，你的系统都能稳稳吃到硬件演进的红利。

---

## 译注与回看说明

英文引文段落及 `原注 [1]`–`[13]` 完整译自 [transactional.blog](https://transactional.blog/blog/2024-modern-database-hardware) 于 2024-11-19 发表的《Modern Hardware for Future Databases》；各节末尾的“2026 回看”结合截至 2026 年公开的 NVMe 2.1 FDP 规范、CXL 内存分层研究、主流公有云 DPU/网络/时钟文档及 SIGMOD/VLDB/CIDR 2026 相关系统论文补充撰写，供对照演进脉络参考。
