---
title: "【翻译】面向未来数据库的现代硬件：2026 回看"
date: 2026-08-09T00:00:00+08:00
categories:
  - 数据库
tags:
  - 数据库
  - 硬件
  - LLM
  - 云计算
description: "翻译并回看《Modern Hardware for Future Databases》：网络、存储、计算与云端可用性在 LLM 时代的发展、兑现与修正。"
draft: false
---

**原文**:  [Modern Hardware for Future Databases](https://transactional.blog/blog/2024-modern-database-hardware)

**发布日期**: 2024-11-19

**原文相关入口**: [另一份中文译本](https://pigsty.cc/zh/blog/db/future-hardware/)；[Reddit](https://www.reddit.com/search/?q=url%3Atransactional.blog/blog/2024-modern-database-hardware)、[Hacker News](https://hn.algolia.com/?query=transactional.blog/blog/2024-modern-database-hardware) 与 [Lobsters](https://lobste.rs/search?q=domain%3Atransactional.blog+title%3A%22Modern%20Hardware%20for%20Future%20Databases%22&what=stories) 上的讨论。

> We’re in an exciting era for databases, with hardware advances arriving across networking, storage, and compute. Each of these advances have the potential to reshape an optimal database architecture. Taken together, they make me hopeful that we’ll see meaningful architectural shifts in databases over the next decade, but also uncertain whether the necessary hardware will be accessible to most database builders.
{.quote-en lang="en"}

我们正处在数据库硬件快速演进的时期：网络、存储和计算都在变化，其中任何一项都可能改写数据库的最优架构。把这些变化放在一起看，未来十年很可能出现真正有意义的架构迁移；但这些硬件能否被大多数数据库开发者获得，仍然是一个疑问。

> **2026 回看：** LLM 并不是硬件趋势之外的另一个答案。更准确地说，生成式 AI 成了硬件演进的强力需求方：它推动 GPU/TPU、HBM、高速互联和存储带宽扩张，又通过 RAG、向量检索和智能体记忆把这些能力传导到数据库。原文的核心问题——“硬件能力能否成为普通数据库开发者可依赖的公共基础设施”——因此比 2024 年更加重要。

---

## 网络（NETWORKING）

> From a recent [talk by Stonebraker in HPTS 2024](https://muratbuffalo.blogspot.com/2024/09/hpts24-day-1-part-1.html), some benchmarking with VoltDB saw that ~60% of their server-side cycles went to the TCP/IP stack. VoltDB is already a database architecture whose goal was to remove as much not-query-processing work from serving requests as possible, so this is the extreme case. However, it still makes a valid point that the computational overhead of TCP is not small, and will become ever more noticeable as network bandwidth increases. This isn’t a new observation though, and there’s an escalating series of proposed solutions.
{.quote-en lang="en"}

根据 Stonebraker 在 [HPTS 2024 的演讲](https://muratbuffalo.blogspot.com/2024/09/hpts24-day-1-part-1.html)，VoltDB 的一项基准测试显示，服务端约 60% 的 CPU 周期消耗在 TCP/IP 协议栈上。VoltDB 本就以尽量消除请求路径中与查询无关的工作为目标，因此这是一个极端案例；但它仍说明 TCP 的计算开销并不小，而且随着网络带宽增长会越来越显眼。对此，业界已经提出了一系列逐步激进的方案。

对于存算分离系统，是否由网络主导开销要看负载：大范围扫描往往受网络带宽约束，小查询则更在意往返延迟、排队和协议栈的 CPU 成本。不能笼统地把所有 Scan I/O 都归结为网络，但数据搬运确实常是存算分离的关键成本。

> One proposed solution is to replace TCP with another protocol that runs over UDP instead. QUIC is the frequently chosen example. However, this is misled.[1] [“It is a grossly inaccurate simplification, but at its simplest level, QUIC is simply TCP encapsulated and encrypted in a User Datagram Protocol (UDP) payload.”](https://blog.apnic.net/2022/11/03/comparing-tcp-and-quic/) The CPU overhead of [TCP and QUIC is also remarkably similar](https://www.fastly.com/blog/measuring-quic-vs-tcp-computational-efficiency). Diverging further from TCP and specializing in specific environments would be needed to materialize notable improvements, and there are papers like [Homa](https://networking.harshkapadia.me/files/homa/research-papers/its-time-to-replace-tcp-in-the-datacenter-v2.pdf) showing some improvements in datacenter environments. But even with a better protocol, the better optimization potential lies in reducing the overhead of the kernel networking stack.
{.quote-en lang="en"}

一种提议是用运行在 UDP 之上的协议替换 TCP，QUIC 是最常被提起的例子。不过，把 QUIC 当作低开销 TCP 替代品并不准确。[APNIC 的文章](https://blog.apnic.net/2022/11/03/comparing-tcp-and-quic/)用一种有意简化的说法指出，QUIC 仍然承担了类似 TCP 的可靠传输职责并额外提供加密；[Fastly 的测量](https://www.fastly.com/blog/measuring-quic-vs-tcp-computational-efficiency)也显示两者 CPU 开销很接近。要获得显著收益，需要像 [Homa](https://networking.harshkapadia.me/files/homa/research-papers/its-time-to-replace-tcp-in-the-datacenter-v2.pdf) 那样针对数据中心环境做更专门的设计。不过，即使更换协议，更大的优化空间通常仍在减少内核网络栈的成本。

> **原注 [1]：** QUIC 能改善连接迁移、握手和队头阻塞等问题，但不会天然改善稳态延迟与带宽；在某些场景下甚至可能更差。讨论问题时需要区分协议语义、实现质量与内核路径成本。

> One way to reduce the amount of work the kernel has to do is by moving the computationally intensive but simple parts to the hardware. This has been happening incrementally over time with enhancements that [offload both segmentation and checksumming to the NIC](https://docs.kernel.org/networking/segmentation-offloads.html). A more recent enhancement of [KTLS](https://www.kernel.org/doc/html/v5.2/networking/tls-offload.html) allows offloading packet encryption in TLS to the NIC as well. Attempts at offloading all of TCP to the hardware, in the form of a [TCP Offload Engine (TOE)](https://wiki.linuxfoundation.org/networking/toe), have been systematically rejected by Linux maintainers. So these have been nice enhancements, but significant parts of the TCP stack still remain a responsibility of the kernel.
{.quote-en lang="en"}

减少内核工作量的一种方法，是把计算密集但逻辑相对简单的部分移到硬件。网卡已经逐步承担[分段与校验和卸载](https://docs.kernel.org/networking/segmentation-offloads.html)，[KTLS](https://www.kernel.org/doc/html/v5.2/networking/tls-offload.html) 还允许卸载部分 TLS 数据路径上的加密工作。相比之下，将整个 TCP 栈塞入硬件的 [TCP Offload Engine（TOE）](https://wiki.linuxfoundation.org/networking/toe)长期不受 Linux 维护者欢迎。因此，卸载在持续推进，但 TCP 栈的重要部分仍由内核负责。

> Thus another solution is to remove the kernel as the middleman between the NIC and the application. Frameworks such as [Data Plane Development Kit (DPDK)](https://www.dpdk.org/) permit userspace to poll the network card for packets, removing the overhead of interrupts, and keeping all the processing in userspace means no transitions into and out of the kernel. DPDK has also seen struggles in adoption, as it requires exclusive control of a NIC. One thus needs to have two NICs per host, one for DPDK and one for the OS and every other process. Marc Richards put together a nice [Linux Kernel vs DPDK benchmark](https://talawah.io/blog/linux-kernel-vs-dpdk-http-performance-showdown/), that ends with DPDK offering a 50% increase in throughput, followed by an enumeration of the slew of drawbacks one accepts to gain that 50%. It seems to be a tradeoff most databases aren’t interested in, and even ScyllaDB has mostly dropped its investment into it.
{.quote-en lang="en"}

另一条路是让内核退出网卡与应用之间的数据路径。[DPDK](https://www.dpdk.org/) 允许用户态轮询网卡，减少中断和用户态/内核态切换。不过，DPDK 通常要独占网卡，主机往往还要为操作系统和其他进程准备另一块网卡。Marc Richards 的 [Linux 内核与 DPDK 基准测试](https://talawah.io/blog/linux-kernel-vs-dpdk-http-performance-showdown/)测得约 50% 的吞吐提升，同时也列出了获得这部分性能所付出的复杂性。多数数据库并不愿接受这种权衡，ScyllaDB 后来也基本停止了在这一方向上的投入。

> Newer hardware presents an interesting new option: removing the CPU from the networking path. [RDMA (Remote Direct Memory Access)](https://www.naddod.com/blog/easily-understand-rdma-technology) offers verbs, a limited set of operations (essentially read, write, and 8-byte CAS) that can be performed entirely from within the NIC, with no CPU interaction. Cutting out the CPU means close to 1us of latency for a remote read, versus the >100us latency of TCP.
>
> As part of RDMA, the responsibility of packet loss and flow control is also pushed down entirely to the NIC.[2] Cutting out the CPU also means large volumes of data can be transferred without the CPU becoming the bottleneck. [TCP offload is a dumb idea whose time has come](https://scholar.google.com/scholar?cluster=4106138525527042387) is a fun read in this area. (From 2003!)
{.quote-en lang="en"}

更新的硬件提供了另一种选择：让 CPU 退出网络数据路径。[RDMA（远程直接内存访问）](https://www.naddod.com/blog/easily-understand-rdma-technology)提供一组受限的 verbs，主要包括 read、write 和 8 字节 CAS，可由网卡完成而无需远端 CPU 参与。原文给出的数量级是：远程读取接近 1 微秒，而 TCP 往往超过 100 微秒。RDMA 还把丢包处理和流控下放给网卡，使大规模数据传输不再轻易被 CPU 卡住。2003 年的论文 [《TCP Offload Is a Dumb Idea Whose Time Has Come》](https://scholar.google.com/scholar?cluster=4106138525527042387)至今仍值得一读。

> **原注 [2]：** 为什么 RDMA 可以把丢包检测和流控下推到硬件，而 Linux 维护者拒绝为 TCP 做同样的事？关键在于 RDMA 暴露的是一个不同且受限得多的 API，网卡与主机之间的复杂度更可控。

> Having RDMA as a low-latency and high-throughput networking primitive changes how one can design databases. [The End of a Myth: Distributed Transactions Can Scale](https://www.vldb.org/pvldb/vol10/p685-zamanian.pdf/) shows that RDMA’s low latency lets the classic 2PL+2PC scale to large clusters. [Is Scalable OLTP in the Cloud a Solved Problem?](https://www.cidrdb.org/cidr2023/papers/p50-ziegler.pdf) pitches the idea of having shared writable page cache across nodes, because low latency means tighter coupling of components becomes feasible.
>
> RDMA isn’t just for OLTP databases either; BigQuery uses an [RDMA shuffle-based join](https://cloud.google.com/blog/products/bigquery/in-memory-query-execution-in-google-bigquery), because of the high throughput. Changing the basic numbers on latency and CPU utilization at a given throughput changes which design is the best, or unblocks new designs that previously weren’t considered feasible.[3]
{.quote-en lang="en"}

RDMA 这种低延迟、高吞吐的网络原语会改变数据库的设计边界。[《The End of a Myth: Distributed Transactions Can Scale》](https://www.vldb.org/pvldb/vol10/p685-zamanian.pdf/)表明，RDMA 的低延迟可以让经典的 2PL+2PC 扩展到大型集群；[《Is Scalable OLTP in the Cloud a Solved Problem?》](https://www.cidrdb.org/cidr2023/papers/p50-ziegler.pdf)则提出跨节点共享可写页面缓存，因为低延迟让更紧密的组件耦合成为可能。RDMA 也不只服务于 OLTP：BigQuery 使用[基于 RDMA shuffle 的 join](https://cloud.google.com/blog/products/bigquery/in-memory-query-execution-in-google-bigquery)来获得高吞吐。当延迟和单位吞吐所需 CPU 的基础数字变化后，旧架构的取舍会被重新排序，原本不可行的设计也可能变得可行。

> **原注 [3]：** 使用 RDMA 时，作者建议采用 [libfabric](https://ofiwg.github.io/libfabric/) 来屏蔽不同厂商与库的差异；[RDMAmojo](https://rdmamojo.com/) 是系统学习 RDMA 的优秀资料。

> Lastly, there’s a class of even newer hardware that finishes the trend of placing even more computing power in the NIC itself, in the form of SmartNICs or Data Processing Units (DPUs). They permit arbitrary processing to be pushed down to the NIC, and potentially invoked in response to requests from other NICs.
>
> These are rather recent, and I’d suggest looking at [DPDPU: Data Processing with DPUs](https://scholar.google.com/scholar?cluster=14622696590036176289) for an overview, [DDS: DPU-Optimized Disaggregated Storage](https://scholar.google.com/scholar?cluster=12305794631120951674) for how to integrate them into a database, and [Azure Accelerated Networking: SmartNICs in the Public Cloud](https://www.microsoft.com/en-us/research/uploads/prod/2018/03/Azure_SmartNIC_NSDI_2018.pdf) for details about deploying them.
>
> In general, I expect SmartNICs to extend RDMA from simple reads and writes to general RPCs that fully bypass the CPU for requests that are computationally cheap to answer.
{.quote-en lang="en"}

最后是把更多计算能力直接放进网卡的 SmartNIC 和 DPU。它们允许把任意处理下推到网卡，甚至由其他网卡发来的请求触发。可分别参考综述 [DPDPU](https://scholar.google.com/scholar?cluster=14622696590036176289)、数据库集成案例 [DDS](https://scholar.google.com/scholar?cluster=12305794631120951674)，以及部署经验 [Azure Accelerated Networking](https://www.microsoft.com/en-us/research/uploads/prod/2018/03/Azure_SmartNIC_NSDI_2018.pdf)。作者预计，SmartNIC 会把 RDMA 从简单读写扩展到完全绕过 CPU 的通用轻量 RPC。

### 2026 回看：网络

- **已经兑现：基础设施卸载成为云厂商默认能力。** [Azure Boost](https://learn.microsoft.com/en-us/azure/azure-boost/overview) 和 [Google Titanium](https://cloud.google.com/titanium) 都把虚拟化、网络或存储处理移到专用硬件。这支持了原文关于 DPU/SmartNIC 价值的判断，但这些卡大多由云平台控制，并不是租户可以随意编程的数据库协处理器。
- **部分兑现：AI 集群扩大了低延迟网络的供给。** AWS EFA 仍以带 OS-bypass 的 [SRD](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/efa.html) 为核心；Google 的 A3 Ultra 等 AI 实例已经公开提供 [RoCE 网络](https://cloud.google.com/compute/docs/accelerator-optimized-machines)。也就是说，高速互联比 2024 年更容易租到，但通常与 GPU/HPC 实例、特定拓扑和调度系统绑定，并没有普遍下沉到普通数据库实例。
- **LLM 带来的修正：网络首先服务于加速器集群。** 训练与分布式推理让 RDMA/RoCE、集体通信和 GPUDirect 获得强商业驱动力；数据库能间接受益，但“任意 OLTP 服务可依赖单边 RDMA verbs”依然不是主流云的通用抽象。
- **仍然成立：数据搬运比协议名字更重要。** QUIC 没有消除 CPU 与内存拷贝成本。数据库优化的重点继续向批量化、零拷贝、异步 I/O、压缩，以及把计算放到数据所在位置迁移。

---

## 存储（STORAGE）

> There are advances in storage devices that aim to improve the total cost of ownership for storage devices in specialized use cases. Manufacturers cleverly noted that one can read narrower stripes of magnetized HDD platters than the minimum stripe width than an HDD can write, and so one can overlap tracks to leave the minimal readable width.
>
> Thus, we gained [Shingled Magnetic Recording](https://www.storagereview.com/news/what-is-shingled-magnetic-recording-smr) HDDs, which introduced the concept of storage being split into zones that only support appends and erases. SMR HDDs are targeted at use cases like [object storage](https://dropbox.tech/infrastructure/four-years-of-smr-storage-what-we-love-and-whats-next) where access is infrequent but large volumes of data must be stored.
{.quote-en lang="en"}

一部分存储创新以特定场景下的总拥有成本（TCO）为目标。由于 HDD 能读取的磁道宽度可以小于其最小写入宽度，制造商可以让磁道彼此重叠，只留下可读宽度，由此产生了[叠瓦式磁记录（SMR）](https://www.storagereview.com/news/what-is-shingled-magnetic-recording-smr)硬盘。SMR 把空间分成主要支持顺序追加与整区擦除的 zone，适合[对象存储](https://dropbox.tech/infrastructure/four-years-of-smr-storage-what-we-love-and-whats-next)这类低频访问、海量容量场景。

> Similar ideas have been applied to SSDs, and [Zonal SSDs](https://zonedstorage.io/docs/introduction/zoned-storage) also exist. Exposing zones within an SSD means that the drive doesn’t need to offer a Flash Translation Layer (FTL) or a complex garbage collection process. Similar to SMR, this reduces the cost of a ZNS SSD as compared to a “regular” SSD, but there’s an additional focus on application-driven[4] garbage collection being more efficient, thus decreasing total write amplification and increasing drive lifetime.
>
> Consider LSMs on SSDs, which already operate via incremental appending and large erase blocks. Removing the FTL between an LSM and the SSD opens [opportunity for optimizations](https://scholar.google.com/scholar?cluster=17379606248569225336).
{.quote-en lang="en"}

相似思路也被用于 SSD，形成了[分区命名空间 SSD（ZNS SSD）](https://zonedstorage.io/docs/introduction/zoned-storage)。向应用暴露 zone 后，设备可以简化闪存转换层（FTL）和垃圾回收。它不仅可能降低成本，还能让应用参与数据放置与回收，从而减少写放大并延长寿命。LSM 本来就采用增量追加并面对大擦除块，因此移除 LSM 与介质之间信息不透明的 FTL，提供了新的[跨层优化机会](https://scholar.google.com/scholar?cluster=17379606248569225336)。

> More recently, Google and Meta have collaborated on a proposal for [Flexible Data Placement (FDP)](https://www.micron.com/about/blog/storage/innovations/eliminating-the-io-blender-promise-of-flexible-data-placement), which acts more as a hint for grouping writes with related lifetimes than as a strict partitioning mechanism like ZNS. The goal is to enable an easier upgrade path where an SSD could ignore the FDP part of the write request and still be semantically correct, just with worse performance or write amplification.
{.quote-en lang="en"}

Google 与 Meta 后来合作提出了[灵活数据放置（FDP）](https://www.micron.com/about/blog/storage/innovations/eliminating-the-io-blender-promise-of-flexible-data-placement)。相比 ZNS 的严格分区，FDP 更像是把相近生命周期写入归组的提示。这样升级路径更平滑：SSD 即使忽略提示，语义仍然正确，只是性能或写放大更差。

> **原注 [4]：** 使用 SMR HDD 可参考 [libzbc](https://github.com/westerndigitalcorporation/libzbc)，使用 ZNS SSD 可参考 [xNVMe](https://xnvme.io/api/c/nvme/xnvme_znd.html#functions)。

> Other improvements target not cost efficiency[5], but improving the set of features that storage devices support. Focusing on NVMe in particular, NVMe added a [copy command](https://www.snia.org/educational-library/towards-copy-offload-linux-nvme-2021), to remove the waste in reading and writing the same data. [Fused compare-and-write commands](https://files.futurememorystorage.com/proceedings/2013/20130812_PreConfD_Marks.pdf#page=46) allow a CAS operation to be pushed down to the drive itself, enabling crazy designs like pushing [Optimistic Lock Coupling](https://scholar.google.com/scholar?cluster=7804091931900436017) down into the drive itself.
>
> NVMe inherited the [Data Integrity Field (DIF)](https://lwn.net/Articles/548294/) / [Data Integrity Extensions (DIX)](https://oss.oracle.com/~mkp/docs/dix.pdf) support from SCSI, which allows pushing page checksums down into the drive. (Notably used by Oracle.) There are projects like [KV-SSD](https://blocksandfiles.com/2019/09/05/samsungs-potentially-groundbreaking-keyvalue-ssd/) that change the entire data model from storing blocks by index to storing objects by key, and head towards replacing software storage engines entirely. SSD manufacturers continue to make SSDs more capable of more operations over time.
{.quote-en lang="en"}

另一些改进不以成本效率为首要目标，而是扩展设备语义。NVMe 的 [copy 命令](https://www.snia.org/educational-library/towards-copy-offload-linux-nvme-2021)可以避免数据经过主机做一次无意义的读回再写出；[fused compare-and-write](https://files.futurememorystorage.com/proceedings/2013/20130812_PreConfD_Marks.pdf#page=46) 把 CAS 下推到设备，甚至可能把[乐观锁耦合](https://scholar.google.com/scholar?cluster=7804091931900436017)放进盘内。NVMe 还继承了 SCSI 的 [DIF](https://lwn.net/Articles/548294/) / [DIX](https://oss.oracle.com/~mkp/docs/dix.pdf)，可把页校验的一部分职责交给设备；[KV-SSD](https://blocksandfiles.com/2019/09/05/samsungs-potentially-groundbreaking-keyvalue-ssd/)则尝试从按块寻址转向按键存取。总体趋势是 SSD 支持的操作持续增加。

> **原注 [5]：** 如果期待的是[持久内存](https://pmem.io/pmdk/libpmem/)，Intel 已终止 [Optane / 3D XPoint](https://en.wikipedia.org/wiki/3D_XPoint)，这条路线暂时中断。[Kioxia](https://americas.kioxia.com/en-ca/business/news/2021/memory-20210913-1.html) 和 [Everspin](https://investor.everspin.com/news-releases/news-release-details/everspin-technologies-unveils-persyst-simplifying-persistent) 等厂商仍在推进相关介质，但作者当时尚未看到广泛应用。

> As the penultimate step in SSD capabilities, SmartSSDs are coming into existence which allow for putting arbitrary compute into an SSD. [Query processing on SmartSSDs: Opportunities and challenges](http://pages.cs.wisc.edu/~yxy/cs764-f20/papers/SmartSSD.pdf) surveys their application to query processing tasks. Pushing filters to storage is always advantageous; I’ve regularly linked previous work like [PushdownDB](https://marcoserafini.github.io/assets/pdf/pushdown.pdf) leveraging S3 Select[6] as a great example on the analytics side.
>
> With SmartSSDs we get papers like [POLARDB Meets Computational Storage](https://www.usenix.org/conference/fast20/presentation/cao-wei). Even without specialized integration, there are arguments to be made that even transparent, in-drive compression can [close the gap between B+ trees and LSMs in write amplification](https://www.usenix.org/conference/fast22/presentation/qiao). Leveraging SmartSSDs is still a young field of research, but there’s incredible potential for impact.
{.quote-en lang="en"}

SmartSSD 进一步允许在 SSD 内执行任意计算。[《Query Processing on SmartSSDs》](http://pages.cs.wisc.edu/~yxy/cs764-f20/papers/SmartSSD.pdf)综述了查询处理机会；利用 S3 Select 的 [PushdownDB](https://marcoserafini.github.io/assets/pdf/pushdown.pdf)展示了分析场景中的计算下推；[POLARDB Meets Computational Storage](https://www.usenix.org/conference/fast20/presentation/cao-wei)则是云原生数据库与计算存储结合的实例。即使没有数据库专门适配，盘内透明压缩也可能[缩小 B+ 树与 LSM 的写放大差距](https://www.usenix.org/conference/fast22/presentation/qiao)。这是一个年轻但潜力很大的方向。

> **原注 [6]：** AWS 于 2024-07-25 起不再向新客户开放 [S3 Select](https://aws.amazon.com/blogs/storage/how-to-optimize-querying-your-data-in-amazon-s3/)，原作者推测其产品重心转向 [S3 Object Lambda](https://aws.amazon.com/s3/features/object-lambda/)。

### 2026 回看：存储

- **已经兑现：FDP 从提案走向标准。** [NVMe 2.1 的修订说明](https://nvmexpress.org/wp-content/uploads/NVM-Express-Revision-Changes-2025.03.31.pdf)已把 FDP 列为可选能力；[NVM Express 的说明](https://nvmexpress.org/nvmeflexible-data-placement-fdp-blog/)明确把目标定为接近 1.0 的写放大系数。相比要求应用彻底改写 I/O 模型的 ZNS，FDP 的兼容性路径更有现实吸引力。但“进入标准”不等于“普通云盘已经透传”，原文关于可获得性的担忧依旧成立。
- **需要补入的新主线：CXL 与内存中心架构。** Optane 消失后，“持久内存”没有按原路径复活，但 CXL 把问题改写为内存扩展、分层和池化。[CXL 规范](https://computeexpresslink.org/cxl-specification/)已覆盖内存池与设备共享；2025 年的论文 [Databases in the Era of Memory-Centric Computing](https://research.google/pubs/databases-in-the-era-of-memory-centric-computing/)也直接讨论了数据库如何使用解耦内存。它更可能先扩展冷数据、buffer pool 和大索引容量，而不是无代价替代本地 DRAM。
- **LLM 强化了“容量与带宽同等重要”。** embedding、向量索引、文档语料和模型检查点都扩大了存储量；而 RAG 的线上路径要求把相关数据快速送到 CPU/GPU。压缩、量化、冷热分层、对象存储与本地 NVMe 缓存因此比“单块盘的峰值 IOPS”更重要。
- **尚未兑现：SmartSSD 没有成为通用数据库平台。** 计算存储仍受编程模型、可观测性、升级、安全隔离和云端透传限制。更常见的落地方式仍是由云厂商在服务内部完成过滤、压缩或索引构建下推，而不是把可编程 SSD 直接交给租户。

---

## 计算（COMPUTE）

> OLTP and OLAP spend their compute time on significantly different types of work, so we’ll address the potential advances for each separately.
{.quote-en lang="en"}

OLTP 与 OLAP 的计算热点差异很大，因此需要分别讨论。

### 事务处理（TRANSACTION PROCESSING）

> In a recent VLDB, two powerhouses of database research put forth a position paper of [Cloud-Native Database Systems and Unikernels: Reimagining OS Abstractions for Modern Hardware](https://www.vldb.org/pvldb/vol17/p2115-leis.pdf), arguing that unikernels allow databases to specialize an OS for their exact needs.
>
> The early work on [VMCache](https://scholar.google.com/scholar?cluster=7903866005464261403) highlights the struggle in efficient database buffer management in particular, where one either accepts the complexity of [pointer swizzling](https://db.in.tum.de/~leis/papers/leanstore.pdf), or one hooks into the kernel and invokes `mmap()`-related syscalls frequently. Neither option is appealing, and unikernels instead offer direct access to virtual memory primitives.
>
> The effort required to develop unikernels is lowering as the area is getting more attention, and [Akira Kurogane](https://jp.linkedin.com/in/akira-kurogane) got [MongoDB running as a unikernel](https://www.linkedin.com/pulse/mongodb-booted-unikernel-os-akira-kurogane-vdf7c/) via [Unikraft](https://unikraft.org/) with little effort, and subsequent posts showed a bit of performance improvement without any MongoDB-internal changes.
>
> There’s been an endless joke that databases want to become the OS, as the desire for performance improvements would require more control over networking, filesystems, disk I/O, memory, etc., and unikernel databases offer exactly that as a tangible possibility.
{.quote-en lang="en"}

VLDB 论文 [《Cloud-Native Database Systems and Unikernels》](https://www.vldb.org/pvldb/vol17/p2115-leis.pdf)主张，Unikernel 允许数据库按自身需求定制操作系统抽象。[VMCache](https://scholar.google.com/scholar?cluster=7903866005464261403) 展示了高效 buffer management 的两难：要么接受[指针重定位](https://db.in.tum.de/~leis/papers/leanstore.pdf)的复杂性，要么频繁进入内核调用 `mmap()` 相关接口。Unikernel 则可以直接使用虚拟内存原语。随着工具成熟，[Akira Kurogane](https://jp.linkedin.com/in/akira-kurogane) 已通过 [Unikraft](https://unikraft.org/) 让 [MongoDB 运行在 Unikernel 上](https://www.linkedin.com/pulse/mongodb-booted-unikernel-os-akira-kurogane-vdf7c/)，而无需修改 MongoDB 内部代码。数据库“总想成为操作系统”的老笑话，由此变成了可实践的架构选择。

> For data confidentiality beyond just TLS or disk encryption, secure enclaves allow execution of verifiably untampered code, where the data being operated on is protected even from a compromised operating system. Whereas a [Trusted Platform Module (TPM)](https://learn.microsoft.com/en-us/windows/security/hardware-security/tpm/tpm-fundamentals) allowed keys to be held securely within a machine, secure enclaves extend that protection to arbitrary code and data.
>
> This permits building databases that are tremendously more resilient to malicious compromise but with several constraints on their design. Microsoft has published on integrating [secure enclaves into Hekaton](https://blog.acolyer.org/2018/07/05/enclavedb-a-secure-database-using-sgx/), and has released the work as part of [SQL Server Always Encrypted](https://learn.microsoft.com/en-us/sql/relational-databases/security/encryption/always-encrypted-enclaves?view=sql-server-ver16). Alibaba has also published about their efforts in building [enclave-native storage engines](https://vldb.org/pvldb/vol14/p1019-sun.pdf) for enterprise customers worried about data confidentiality.
>
> Databases have a history of being able to sell security improvements through the vehicle of [regulatory compliance](https://www.fortanix.com/faq/confidential-computing/how-does-confidential-computing-help-with-regulatory-compliance-requirements), and secure enclaves are a meaningful improvement in data confidentiality.
{.quote-en lang="en"}

安全飞地提供了超越 TLS 与磁盘加密的数据机密性：即使操作系统被攻破，经过验证的代码及其处理的数据仍可受到保护。[TPM](https://learn.microsoft.com/en-us/windows/security/hardware-security/tpm/tpm-fundamentals)主要保护密钥，飞地则把边界扩展到任意代码与数据。微软研究过[将飞地集成到 Hekaton](https://blog.acolyer.org/2018/07/05/enclavedb-a-secure-database-using-sgx/)，并将成果带入 [SQL Server Always Encrypted](https://learn.microsoft.com/en-us/sql/relational-databases/security/encryption/always-encrypted-enclaves?view=sql-server-ver16)；阿里也发表了[飞地原生存储引擎](https://vldb.org/pvldb/vol14/p1019-sun.pdf)。这类能力存在设计约束，但能通过[监管合规](https://www.fortanix.com/faq/confidential-computing/how-does-confidential-computing-help-with-regulatory-compliance-requirements)转化为明确的商业价值。

> After Spanner’s introduction of [TrueTime](https://sookocheff.com/post/time/truetime/), clock synchronization has become of notable interest for transaction ordering in geo-distributed databases. Each of the major cloud providers has an NTP offering that is tied to atomic clocks or GPS satellites ([AWS](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/set-time.html), [Azure](https://learn.microsoft.com/en-us/azure/virtual-machines/linux/time-sync#overview), [GCP](https://developers.google.com/time/faq#whatis)).
>
> This is of great utility to any similar design, like CockroachDB or Yugabyte, for which clock synchronization is vital for correctness, and conservatively wide margins of error degrade performance. AWS’s recent Aurora Limitless also [uses a TrueTime-like design](https://www.youtube.com/watch?v=a9FfjuVJ9d8&t=29m25s). This is the only mention of cloud-specific not-quite-hardware because it uniquely involves major cloud vendors exposing expensive hardware (atomic clocks) that users otherwise wouldn’t have considered buying for themselves.
{.quote-en lang="en"}

Spanner 引入 [TrueTime](https://sookocheff.com/post/time/truetime/)后，时钟同步成为地理分布式数据库事务排序的重要基础设施。主流云都提供与原子钟或 GPS 关联的时间服务（[AWS](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/set-time.html)、[Azure](https://learn.microsoft.com/en-us/azure/virtual-machines/linux/time-sync#overview)、[GCP](https://developers.google.com/time/faq#whatis)）。CockroachDB、Yugabyte 等系统既依赖时钟误差界保证正确性，又会因误差界过大付出性能代价。Aurora Limitless 也采用了[类似 TrueTime 的设计](https://www.youtube.com/watch?v=a9FfjuVJ9d8&t=29m25s)。这是云厂商把用户通常不会自行购买的原子钟基础设施间接开放出来的少见案例。

> Hardware transactional memory has had a rather ill-fated history. [Sun’s Rock processor](https://www.theregister.com/2007/08/21/sun_transactional_memory_rock/) featured hardware transactional memory right up until Sun was bought and Rock was shut down. Intel made two attempts at releasing it, and had to disable it both times.[7]
>
> There was some interesting work on the subject of [applying hardware transactional memory to in-memory databases](https://15721.courses.cs.cmu.edu/spring2019/papers/leis-icde2014.pdf), but other than finding some old CPUs for experimentation, we all must wait until a CPU manufacturer says they’re planning to make another attempt at it.
{.quote-en lang="en"}

硬件事务内存（HTM）的历史并不顺利。[Sun Rock](https://www.theregister.com/2007/08/21/sun_transactional_memory_rock/) 项目随公司收购而终止；Intel 两次推出、两次禁用。虽然已有[把 HTM 用于内存数据库](https://15721.courses.cs.cmu.edu/spring2019/papers/leis-icde2014.pdf)的研究，但现实选择仍是寻找旧 CPU 做实验，或等待芯片厂商再次尝试。

> **原注 [7]：** Intel 第一次禁用 TSX 是因为[硬件缺陷](http://techreport.com/news/26911/errata-prompts-intel-to-disable-tsx-in-haswell-early-broadwell-cpus)，第二次与[利用 TSX 绕过 KASLR 的侧信道攻击](https://www.blackhat.com/docs/us-16/materials/us-16-Jang-Breaking-Kernel-Address-Space-Layout-Randomization-KASLR-With-Intel-TSX-wp.pdf)有关；另有一次[因误解 CTF 题意而发现的推测执行时序攻击](https://blog.ret2.io/2019/06/26/attacking-intel-tsx/)。

#### 2026 回看：事务处理

- **Unikernel 的论点成立，产品形态却未成为主流。** 数据库确实需要更强的 I/O、内存和调度控制，但云上更常见的落地是轻量虚拟机、容器、用户态存储栈与云厂商 DPU，而不是让每个数据库团队维护一套专用 Unikernel。
- **机密计算的重要性上升。** LLM/RAG 把企业私有文档、提示词和检索结果送入推理链路，保护“使用中的数据”比 2024 年更迫切。不过飞地的内存限制、系统调用边界、证明链和调试成本仍会影响数据库设计。
- **精确时钟仍是小众但真实的竞争力。** 它对地理分布式事务有直接价值，却与 LLM 没有强因果关系；不应把所有硬件演进都解释为 AI 驱动。
- **HTM 仍未复兴。** 到目前为止，更值得数据库工程投入的是无锁算法、乐观并发控制、分区和批处理，而不是押注新的通用 HTM。

### 分析处理（ANALYTICAL PROCESSING）

> Companies are consistently founded to leverage specialized hardware to accelerate query processing and achieve better performance and cost efficiency than their CPU-only competitors. GPU-powered databases, like [Voltron](https://voltrondata.com/theseus.html), [HEAVY.ai](https://www.heavy.ai/), and [Brytlyt](https://brytlyt.io/), are the first step in this direction.
>
> I wouldn’t be overly surprised if Intel or AMD integrated graphics gained OpenCL support[8] sometime in the future, which would open the door to all databases being able to assume some amount of GPU capabilities on a much wider set of hardware configurations.
{.quote-en lang="en"}

不断有公司尝试用专用硬件加速查询，以获得优于纯 CPU 方案的性能或成本效率，例如 [Voltron](https://voltrondata.com/theseus.html)、[HEAVY.ai](https://www.heavy.ai/) 和 [Brytlyt](https://brytlyt.io/)。如果 Intel 或 AMD 的集成显卡形成更统一的通用计算支持，数据库就可能在更广泛的硬件上假设存在一定的 GPU 能力。

> **原注 [8]：** 原作者指出，OpenGL Compute Shader 是使用 GPU 做通用计算时较通用、可移植的形式，集成显卡已经支持；但他没有找到用它做数据库处理的相关论文。

> There are also opportunities for using even more power-efficient hardware. The newest Neural Processing Units/Tensor Processing Units have already been shown to be adaptable into query processing in work like [TCUDB: Accelerating Database with Tensor Processors](https://dl.acm.org/doi/pdf/10.1145/3514221.3517869). A few companies have attempted to utilize FPGAs. [Swarm64](https://dbdb.io/db/swarm64) tried (and failed?) at this market. AWS made its own effort as [Redshift AQUA](https://aws.amazon.com/blogs/aws/new-aqua-advanced-query-accelerator-for-amazon-redshift/).
>
> Going as far as ASICs seems to not be worth it for even the largest companies, as even Oracle [stopped their SPARC development in 2017](https://www.hpcwire.com/2017/09/07/oracle-layoffs-reportedly-hit-sparc-solaris-hard/). I’m not overly optimistic about FPGAs through ASICs as memory bandwidth will be the primary bottleneck at some point anyway, but [ADMS](https://adms-conf.org/) is the conference[9] to follow for papers in this overall area.
{.quote-en lang="en"}

能效更高的硬件也提供了机会。[TCUDB](https://dl.acm.org/doi/pdf/10.1145/3514221.3517869) 已展示用 NPU/TPU 做查询处理的可能性；[Swarm64](https://dbdb.io/db/swarm64) 尝试过 FPGA 数据库市场，AWS 则推出过 [Redshift AQUA](https://aws.amazon.com/blogs/aws/new-aqua-advanced-query-accelerator-for-amazon-redshift/)。走到 ASIC 往往连大厂都难以证明经济性，Oracle 也已在 2017 年[停止 SPARC 开发](https://www.hpcwire.com/2017/09/07/oracle-layoffs-reportedly-hit-sparc-solaris-hard/)。原作者对 FPGA/ASIC 并不乐观，因为内存带宽最终会成为瓶颈；相关论文可关注 [ADMS](https://adms-conf.org/)。

> **原注 [9]：** 严格来说，ADMS 是附属于 VLDB 的 workshop。

#### 2026 回看：分析处理与 LLM

- **GPU 路线已明显加速，但不是“所有查询都搬上 GPU”。** LLM 让 GPU 供给、软件栈和工程人才快速增长，数据库最先受益的是高度并行的扫描、聚合、解码、embedding 生成和向量索引构建。分支密集、更新频繁、延迟敏感的 OLTP 仍更适合 CPU。
- **向量检索成为数据库的标准工作负载之一。** [pgvector](https://github.com/pgvector/pgvector) 已支持 HNSW、IVFFlat、半精度向量、稀疏向量和量化等能力；这说明 LLM 带来的变化不只是“出现一种新数据库”，而是向量类型、ANN 索引和混合过滤进入现有数据库与搜索系统。
- **CPU/GPU 混合比 GPU 全托管更现实。** [NVIDIA cuVS](https://docs.nvidia.com/cuvs/home/) 的实践包括在 GPU 上构建索引、转换后在 CPU 上查询；[Amazon OpenSearch Service](https://docs.aws.amazon.com/opensearch-service/latest/developerguide/gpu-acceleration-vector-index.html)也采用按需把向量索引构建卸载到 GPU 的方式。这恰好印证了原文的限制：加速器本地内存昂贵且有限，数据搬运和利用率决定经济性。
- **原文对“内存带宽最终成为瓶颈”的判断更强了。** LLM 时代真正稀缺的不只是 FLOPS，还有 HBM 容量/带宽、互联带宽和能耗。对数据库而言，列式布局、压缩、低精度表示、批处理与算子融合的重要性继续上升。
- **需要修正的观点：集成 GPU 的关键不只是 OpenCL。** 实际生态更可能围绕 CUDA、ROCm、SYCL、Vulkan/计算着色器以及数据库自身的可移植执行层演进。硬件存在只是第一步，编译器、算子库、数据格式和查询优化器才决定数据库能否稳定使用它。

---

## 云端可用性（CLOUD AVAILABILITY）

> To finally address the depressing elephant in the room, none of these hardware advancements matter if they’re not accessible. For today’s systems, that means in the cloud, and the cloud doesn’t offer the forefront of hardware advancements to its customers.
{.quote-en lang="en"}

最后要面对房间里的大象：硬件如果不可获得，就无法影响普通数据库。今天的“可获得”主要意味着能够在云上租用，而云厂商并不一定会把最前沿能力直接暴露给客户。

> For networking, the situation isn’t fantastic. DPDK is the most advanced networking technology that’s somewhat easily accessible, as most clouds allow some instance types to have more than one NIC. AWS offers pseudo-RDMA in the form of [Secure Reliable Datagrams](https://scholar.google.com/scholar?cluster=7115577907027624509), which [was benchmarked](https://scholar.google.com/scholar?cluster=9445549416525532418) to be about halfway between TCP and RDMA. Real RDMA is only available on the High Performance Computing instances within Azure, GCP, and OCI. Only Alibaba offers [RDMA on general-purpose compute instances](https://www.alibabacloud.com/help/en/ecs/user-guide/erdma-overview).[10] SmartNICs are not available anywhere publicly.
>
> Some of this is for good reason: Microsoft has published papers that [deploying RDMA is hard](https://scholar.google.com/scholar?cluster=12305794631120951674). In fact, it’s [really hard](https://scholar.google.com/scholar?cluster=2434531805096404846). Even their papers about [actually succeeding in using RDMA](https://scholar.google.com/scholar?cluster=6986943445603020796) emphasize that it’s really hard. We’re nearing a full decade after Microsoft started using RDMA internally and it’s still not available in their cloud. I have no guesses as to if or when it will be.
{.quote-en lang="en"}

网络侧的情况并不理想。多网卡实例让 DPDK 勉强可用；AWS 通过 [SRD](https://scholar.google.com/scholar?cluster=7115577907027624509)提供一种接近 RDMA 的传输，[基准研究](https://scholar.google.com/scholar?cluster=9445549416525532418)显示它的表现介于 TCP 与 RDMA 之间。原文写作时，Azure、GCP 和 OCI 的真 RDMA 主要局限于 HPC 实例，只有阿里云把 [eRDMA](https://www.alibabacloud.com/help/en/ecs/user-guide/erdma-overview)扩展到通用实例。微软的研究反复说明 RDMA [难以部署](https://scholar.google.com/scholar?cluster=12305794631120951674)、[难以规模化](https://scholar.google.com/scholar?cluster=2434531805096404846)，即使[成功案例](https://scholar.google.com/scholar?cluster=6986943445603020796)也强调其复杂度。

> **原注 [10]：** 阿里云当时通过 iWARP 部署 eRDMA，延迟可能像 SRD 一样有所折损；原作者没有找到相应基准测试。

> For storage, the situation isn’t really any better. The few times that SMR HDDs did reach consumers, it was as a drive that still presented itself as supporting a block storage API, and [consumers hated it](https://arstechnica.com/gadgets/2020/04/caveat-emptor-smr-disks-are-being-submarined-into-unexpected-channels/). ZNS SSDs seem similarly locked behind enterprise-only purchasing agreements.
>
> One might think that Intel discontinuing Optane-branded persistent memory and SSDs would mean that they’re not accessible on the cloud, but Alibaba still offers [persistent memory optimized instances](https://www.alibabacloud.com/help/en/tair/product-overview/persistent-memory-optimized-instances). The wonderful folk at [Spare Cores](https://sparecores.com/) actually provided me with `nvme id-ctrl` output from each cloud vendor, and no NVMe device they pulled presents itself as supporting nearly any optional features: copy, fused compare and write, data integrity extensions, nor multi-block atomic writes.[11]
>
> Alibaba is also the only cloud vendor that has invested into SmartSSDs with their [collaboration with ScaleFlux on PolarDB](https://www.usenix.org/conference/fast20/presentation/cao-wei). This still means SmartSSDs are not accessible to the general public, but even the paper acknowledges it’s “the first real-world deployment of cloud-native databases with computational storage drives ever reported in the open literature”.
{.quote-en lang="en"}

存储侧也没有好多少。消费级 SMR 盘一度在继续伪装成普通块设备时引发[用户反感](https://arstechnica.com/gadgets/2020/04/caveat-emptor-smr-disks-are-being-submarined-into-unexpected-channels/)，ZNS SSD 也主要受限于企业采购。Intel 停产 Optane 后，阿里云当时仍提供[持久内存优化实例](https://www.alibabacloud.com/help/en/tair/product-overview/persistent-memory-optimized-instances)。[Spare Cores](https://sparecores.com/) 提供的各云厂商 `nvme id-ctrl` 输出显示，云 NVMe 几乎不暴露 copy、fused compare-and-write、数据完整性扩展或多块原子写等可选特性。阿里云与 ScaleFlux 的 [PolarDB/SmartSSD 合作](https://www.usenix.org/conference/fast20/presentation/cao-wei)是少见的真实部署，但也没有成为普通租户可依赖的通用设备。

> **原注 [11]：** AWS 虽未直接暴露相关 NVMe 原语，但支持 [torn-write prevention](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/storage-twp.html)；GCP 过去也有类似文档。

> On the compute side, the state finally gets a bit better. The cloud fully permits unikernels, and TPMs are widely accessible, but only [AWS](https://aws.amazon.com/ec2/nitro/nitro-enclaves/) and [Azure](https://learn.microsoft.com/en-us/azure/confidential-computing/confidential-computing-enclaves) support secure enclaves as far as I can tell. NTP servers with atomic or GPS clocks are made available, but only [AWS makes efforts](https://aws.amazon.com/blogs/compute/its-about-time-microsecond-accurate-clocks-on-amazon-ec2-instances/) at [promising error bounds](https://github.com/aws/clock-bound). Without promised error bounds, it is hard to critically rely on these clocks.
>
> (Hardware transactional memory isn’t available, but it’s hard to blame the clouds on that one.)
{.quote-en lang="en"}

计算侧稍好一些。云环境允许 Unikernel，TPM 也很常见；原文写作时，作者确认的安全飞地服务主要是 [AWS Nitro Enclaves](https://aws.amazon.com/ec2/nitro/nitro-enclaves/) 与 [Azure Confidential Computing](https://learn.microsoft.com/en-us/azure/confidential-computing/confidential-computing-enclaves)。各云都能提供原子钟或 GPS 支持的时间源，但只有 AWS 明确尝试提供[微秒级时钟服务](https://aws.amazon.com/blogs/compute/its-about-time-microsecond-accurate-clocks-on-amazon-ec2-instances/)及可编程使用的[误差界](https://github.com/aws/clock-bound)。没有误差界，数据库就很难把时钟用于正确性判断。

> The explosion of AI means there’s good money behind making more efficient compute available. GPUs are available in all clouds. AWS,[12] Azure, IBM, and Alibaba offer FPGA instances. (GCP and OCI don’t.) The unfortunate reality is also that faster compute only matters when compute is the bottleneck. Both GPUs and FPGAs suffer from having limited memory, and so one cannot maintain the database in their local memory.
>
> Instead, one relies on streaming data in and out of them, which means being limited by PCIe speeds. All of this would encourage thoughtful motherboard layout and bus design in an on-premise appliance, but that’s not feasible in the cloud.
{.quote-en lang="en"}

AI 的爆发为高效计算带来了充足预算，GPU 已经遍布公有云，部分云也提供 FPGA。但只有当计算本身是瓶颈时，更快的计算才有价值。GPU/FPGA 本地内存有限，数据库很难完全常驻其中；不断搬入搬出数据又会受 PCIe 限制。这会奖励精心设计的主板拓扑、总线和一体机，却不是普通云租户可以控制的变量。

> **原注 [12]：** 理想状态是支持 P2P DMA，让数据从磁盘直接进入 FPGA；至少原文所引的 [AWS F1 文档](https://github.com/aws/aws-fpga/blob/master/FAQs.md#f1-instance-and-runtime-tools-faqs)表明它做不到。

> Thus we end with my bleak view on the next generation of databases: no one[13] can build databases that critically depend on new hardware advancements until they’re made available, but no cloud vendor wants to deploy hardware that can’t be immediately used. The next generation of databases is held back by the cyclic dependency that it doesn’t yet exist.
{.quote-en lang="en"}

因此，原作者对下一代数据库给出了略显悲观的结论：在新硬件普及前，没有人能构建关键路径依赖它的数据库；但在没有现成应用时，云厂商也不愿部署它。下一代数据库被“因为尚不存在，所以无法获得赖以存在的硬件”这一循环依赖拖住了。

> **原注 [13]：** 云厂商自己除外。微软和 Google 已在内部广泛使用 RDMA 支撑数据库产品，却不一定把同样能力开放给租户。原作者因此一直想写一篇《The Competitive Advantage of RDMA for Cloud Vendors》。

> Alibaba is shockingly great though. They’re consistently at the forefront of making hardware advances available for everything. I’m surprised I don’t see Alibaba being frequently used for benchmarking in academia and industry correspondingly.
{.quote-en lang="en"}

不过，阿里云在原作者看来表现得格外积极：它持续尝试让新的硬件能力可用，因此阿里云很少出现在学术界和工业界的横向基准中，反而令人意外。

### 2026 回看：云端可用性与总判断

- **原文最重要的判断已经部分兑现：云厂商采用了新硬件，但优先把它封装成平台能力。** Azure Boost、Google Titanium、AWS Nitro/EFA，以及按需 GPU 向量索引构建，都说明专用硬件正在进入云；然而用户得到的通常是更快、更稳定的 VM 或托管服务，而不是可编程 DPU、完整 NVMe 指令集或任意 RDMA 原语。
- **LLM 打破了一部分“没有应用就不部署”的循环。** AI 训练和推理提供了足够大的确定性需求，推动高速网络、GPU、HBM 和高性能存储先行部署。数据库可搭便车，但这些资源经常以昂贵的 AI/HPC SKU 出现，未必改善普通 OLTP 的成本结构。
- **软件可移植性比裸硬件暴露更关键。** 真正扩散最快的不是某一块 SmartSSD 或 FPGA，而是能把异构硬件隐藏在 SQL、向量索引、对象存储 API、托管服务和通用库之后的软件层。数据库不应把正确性绑定到单一云厂商的私有硬件，适合把硬件加速做成可探测、可回退的执行路径。
- **截至 2026 年的结论：原文方向基本正确，但节奏不均匀。** DPU/SmartNIC、RDMA/RoCE、GPU 向量处理和 FDP 在前进；通用 SmartSSD、HTM、面向普通实例的完整 RDMA 与高级 NVMe 原语仍未普及。新增的关键变量是 CXL/内存池化，以及 LLM 把“数据库 + 检索 + 推理”变成一条端到端数据路径。

如果把 2024 年的文章压缩为一句今天仍适用的话：**数据库架构会随延迟、带宽、容量和每字节搬运成本的变化而重写；LLM 改变了这些硬件的投资优先级，却没有消除数据系统必须面对的物理约束。**
