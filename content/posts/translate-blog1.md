**原文链接：** [https://transactional.blog/blog/2024-modern-database-hardware](https://transactional.blog/blog/2024-modern-database-hardware)
**发布日期：** 2024-11-19
**阅读时长：** 15 分钟


> We’re in an exciting era for databases, with hardware advances arriving across networking, storage, and compute. Each of these advances have the potential to reshape an optimal database architecture. Taken together, they make me hopeful that we’ll see meaningful architectural shifts in databases over the next decade, but also uncertain whether the necessary hardware will be accessible to most database builders.

我们正处于数据库的一个激动人心的时代，各大资源领域都在取得进展，每一个进步都有潜力塑造理想的数据库架构。综合来看，我希望在未来十年能看到数据库架构出现一些有趣的转变，但我并不确定必要的硬件是否能够普及。

> reshape an optimal database architecture，现在再回过头看，LLM则是最大的影响因素，而不是硬件。


---

## 网络 (NETWORKING)

> From a recent talk by Stonebraker in HPTS 2024, some benchmarking with VoltDB saw that ~60% of their server-side cycles went to the TCP/IP stack. VoltDB is already a database architecture whose goal was to remove as much not-query-processing work from serving requests as possible, so this is the extreme case. However, it still makes a valid point that the computational overhead of TCP is not small, and will become ever more noticeable as network bandwidth increases. This isn’t a new observation though, and there’s an escalating series of proposed solutions.

根据 Stonebraker 在 HPTS 2024 上的[最新演讲](https://muratbuffalo.blogspot.com/2024/09/hpts24-day-1-part-1.html)，对 VoltDB 的基准测试显示，其服务器端约 60% 的 CPU 周期消耗在 TCP/IP 协议栈上。VoltDB 的架构目标本就是尽可能消除处理请求中的非查询工作，因此这是一个极端案例，但它依然证明了一个事实：TCP 的计算开销不容小觑，且随着网络带宽的增加，这种开销将变得愈发显著。这并非新发现，业内也已提出了一系列解决方案。
> 对于存算分离场景中（当然也分具体的业务场景），大部分场景Scan IO仍然是是大头，而Scan IO的大头则是networking。

> One proposed solution is to replace TCP with another protocol that runs over UDP instead. QUIC is the frequently chosen example. However, this is misled[1]. ["It is a grossly inaccurate simplification, but at its simplest level, QUIC is simply TCP encapsulated and encrypted in a User Datagram Protocol (UDP) payload."](https://blog.apnic.net/2022/11/03/comparing-tcp-and-quic/) The CPU overhead of TCP and QUIC is also remarkably similar. Diverging further from TCP and specializing in specific environments would be needed to materialize notable improvements, and there are papers like Homa showing some improvements in datacenter environments. But even with a better protocol, the better optimization potential lies in reducing the overhead of the kernel networking stack.

一种提议是用运行在 UDP 之上的其他协议替换 TCP，QUIC 是最常被提及的例子。然而，这是一种误导[1]。“虽然是极度不准确的简化，但从最简单的层面来看，QUIC 只是封装在 UDP 负载中并经过加密的 TCP。”TCP 和 QUIC 的 CPU 开销也非常相似。要实现显著改进，需要进一步偏离 TCP 并针对特定环境进行专业化，诸如 Homa 等论文显示了在数据中心环境中的一些改进。但即使有了更好的协议，更大的优化潜力仍在于减少内核网络栈的开销。

*[1]: 如果你奇怪为什么 QUIC 在这里“躺枪”，是因为我曾多次陷入这样的争论：当 TCP 或 TLS 被归咎于某些问题时，转向 QUIC 总被作为一个建议的结果。QUIC 确实能解决一些问题，但有些问题它无法改善，甚至可能使之恶化。需要理解的是，稳态延迟和带宽属于后者。*
> WHY???

> One way to reduce the amount of work the kernel has to do is by moving the computationally intensive but simple parts to the hardware. This has been happening incrementally over time with enhancements that offload both segmentation and checksumming to the NIC. A more recent enhancement of KTLS allows offloading packet encryption in TLS to the NIC as well. Attempts at offloading all of TCP to the hardware, in the form of a TCP Offload Engine (TOE), have been systematically rejected by Linux maintainers. So these have been nice enhancements, but significant parts of the TCP stack still remain a responsibility of the kernel.

减少内核工作量的一种方法是将计算密集但简单的部分移至硬件。随着将分段（Segmentation）和校验和（Checksumming）卸载到网卡（NIC）的增强，这种情况一直在逐步发生。最近 KTLS 的增强甚至允许将 TLS 中的数据包加密也卸载到网卡。而试图将整个 TCP 栈卸载到硬件（即 TCP 卸载引擎 TOE）的尝试，一直遭到 Linux 维护者的系统性拒绝。因此，这些虽是很好的增强，但 TCP 栈的重要部分仍是内核的责任。

> Thus another solution is to remove the kernel as the middleman between the NIC and the application. Frameworks such as Data Plane Development Kit (DPDK) permit userspace to poll the network card for packets, removing the overhead of interrupts, and keeping all the processing in userspace means no transitions into and out of the kernel. DPDK has also seen struggles in adoption, as it requires exclusive control of a NIC. One thus needs to have two NICs per host, one for DPDK and one for the OS and every other process. Marc Richards put together a nice Linux Kernel vs DPDK benchmark, that ends with DPDK offering a 50% increase in throughput, followed by an enumeration of the slew of drawbacks one accepts to gain that 50%. It seems to be a tradeoff most databases aren’t interested in, and even ScyllaDB has mostly dropped its investment into it.

因此，另一种解决方案是将内核从网卡与应用之间剥离。诸如 DPDK（数据平面开发工具包）之类的框架允许用户态轮询网卡数据包，从而消除了中断开销，并且所有处理都在用户态进行，意味着没有内核态切换。DPDK 的采用也面临困难，因为它需要对网卡的独占控制。这意味着每台主机需要两块网卡：一块给 DPDK，一块给操作系统及其他进程。Marc Richards 做了一个 Linux 内核 vs DPDK 的基准测试，结果显示 DPDK 提供了 50% 的吞吐量提升，但随后列举了一系列为了这 50% 提升而必须接受的缺点。这似乎是大多数数据库不愿接受的权衡，甚至 ScyllaDB 也基本放弃了对其的投入。

> Thus another solution is to remove the kernel as the middleman between the NIC and the application. Frameworks such as Data Plane Development Kit (DPDK) permit userspace to poll the network card for packets, removing the overhead of interrupts, and keeping all the processing in userspace means no transitions into and out of the kernel. DPDK has also seen struggles in adoption, as it requires exclusive control of a NIC. One thus needs to have two NICs per host, one for DPDK and one for the OS and every other process. Marc Richards put together a nice Linux Kernel vs DPDK benchmark, that ends with DPDK offering a 50% increase in throughput, followed by an enumeration of the slew of drawbacks one accepts to gain that 50%. It seems to be a tradeoff most databases aren’t interested in, and even ScyllaDB has mostly dropped its investment into it.

更新的硬件提供了一个有趣的新选项：将 CPU 从网络路径中完全移除。RDMA（远程直接内存访问）提供了谓词（verbs），这是一组可以在网卡内完全执行的操作（主要是 read, write 和 8 字节 CAS），无需 CPU 参与。绕过 CPU 意味着远程读取的延迟接近 1 微秒，而 TCP 的延迟则超过 100 微秒。作为 RDMA 的一部分，丢包处理和流量控制的责任也完全下放给了网卡[2]。绕过 CPU 还意味着可以在不让 CPU 成为瓶颈的情况下传输海量数据。

*[2]: 为什么 RDMA 可以将丢失检测和流控推向硬件，而 Linux 维护者却拒绝为 TCP 这样做？因为这是一个完全不同且更为受限的 API，降低了网卡与主机之间的复杂度。“TCP 卸载是一个注定会到来的笨主意”是该领域一篇有趣的读物（写于 2003 年！）。*

> Having RDMA as a low-latency and high-throughput networking primitive changes how one can design databases. The End of a Myth: Distributed Transactions Can Scale shows that RDMA’s low latency lets the classic 2PL+2PC scale to large clusters. Is Scalable OLTP in the Cloud a Solved Problem? pitches the idea of having shared writable page cache across nodes, because low latency means tighter coupling of components becomes feasible. RDMA isn’t just for OLTP databases either; BigQuery uses an RDMA shuffle-based join, because of the high throughput. Changing the basic numbers on latency and CPU utilization at a given throughput changes which design is the best, or unblocks new designs that previously weren’t considered feasible.[3]

拥有 RDMA 这种低延迟、高吞吐的网络原语改变了数据库的设计方式。《神话终结：分布式事务可以扩展》(The End of a Myth: Distributed Transactions Can Scale) 展示了 RDMA 的低延迟让经典的 2PL+2PC 能够扩展到大型集群。《云端可扩展 OLTP 是已解决的问题吗？》(Is Scalable OLTP in the Cloud a Solved Problem?) 提出了跨节点共享可写页面缓存的想法，因为低延迟使组件间的紧耦合变得可行。RDMA 不仅适用于 OLTP 数据库；BigQuery 因其高吞吐量而使用基于 RDMA shuffle 的连接。改变给定吞吐量下的延迟和 CPU 利用率基准数，会改变最优设计，或者解锁此前认为不可行的全新设计[3]。

*[3]: 对于使用 RDMA，我强烈建议使用 libfabric，因为它抽象了所有不同的 RDMA 厂商和库。RDMAmojo 博客拥有多年关于 RDMA 特定内容的积累，是学习 RDMA 各个方面的最佳场所之一。*

> Lastly, there’s a class of even newer hardware that finishes the trend of placing even more computing power in the NIC itself, in the form of SmartNICs or Data Processing Units (DPUs). They permit arbitrary processing to be pushed down to the NIC, and potentially invoked in response to requests from other NICs. These are rather recent, and I’d suggest looking at DPDPU: Data Processing with DPUs for an overview, DDS: DPU-Optimized Disaggregated Storage for how to integrate them into a database, and Azure Accelerated Networking: SmartNICs in the Public Cloud for details about deploying them. In general, I expect SmartNICs to extend RDMA from simple reads and writes to general RPCs that fully bypass the CPU for requests that are computationally cheap to answer.

最后，还有一类更新的硬件进一步强化了在网卡本身放置更多计算能力的趋势，即智能网卡（SmartNICs）或数据处理单元（DPUs）。它们允许将任意处理下推到网卡，并可能响应来自其他网卡的请求。这些技术较新，我建议参考《DPDPU: 使用 DPU 进行数据处理》获取概览，《DDS: DPU 优化的解耦存储》了解如何将其集成到数据库，以及《Azure 加速网络：公共云中的智能网卡》获取部署细节。总的来说，我预计智能网卡将扩展 RDMA，从简单的读写进化到允许绕过 CPU 的通用 RPC（针对计算廉价的请求）。

---

## 存储 (STORAGE)

> There are advances in storage devices that aim to improve the total cost of ownership for storage devices in specialized use cases. Manufacturers cleverly noted that one can read narrower stripes of magnetized HDD platters than the minimum stripe width than an HDD can write, and so one can overlap tracks to leave the minimal readable width. Thus, we gained Shingled Magnetic Recording HDDs, which introduced the concept of storage being split into zones that only support appends and erases. SMR HDDs are targeted at use cases like object storage where access is infrequent but large volumes of data must be stored.

存储设备的进步旨在提高特定用例下的总拥有成本（TCO）。制造商敏锐地注意到，读取 HDD 磁碟上磁化条带的宽度可以比写入产生的条带宽度更小，因此可以重叠磁道以留出最小宽度。于是我们有了叠瓦式磁记录（SMR）硬盘，它引入了将存储划分为仅支持追加或擦除的“分区”（zones）的概念。SMR 硬盘针对的是对象存储等访问频率低但存储容量大的用例。

> Similar ideas have been applied to SSDs, and Zonal SSDs also exist. Exposing zones within an SSD means that the drive doesn’t need to offer a Flash Translation Layer (FTL) or a complex garbage collection process. Similar to SMR, this reduces the cost of a ZNS SSD as compared to a "regular" SSD, but there’s an additional focus on application-driven[4] garbage collection being more efficient, thus decreasing total write amplification and increasing drive lifetime. Consider LSMs on SSDs, which already operate via incremental appending and large erase blocks. Removing the FTL between an LSM and the SSD opens opportunity for optimizations.

类似的想法也被应用于 SSD，于是有了分区 SSD（Zonal SSDs）。在 SSD 中暴露分区意味着驱动器不需要提供闪存转换层（FTL）或复杂的垃圾回收过程。与 SMR 类似，这降低了 ZNS SSD 相比“常规”SSD 的成本，但其另一个焦点在于让应用驱动[4]的垃圾回收更高效，从而减少总写入放大并延长驱动器寿命。考虑 SSD 上的 LSM 树，它们本就通过增量追加和大块擦除运行。移除 LSM 和 SSD 之间的 FTL 为优化打开了大门。

*[4]: SMR HDD 的使用可参考 libzbc，ZNS SSD 的使用可参考 xNVMe。*


> More recently, Google and Meta have collaborated on a proposal for Flexible Data Placement (FDP), which acts more as a hint for grouping writes with related lifetimes than as a strict partitioning mechanism like ZNS. The goal is to enable an easier upgrade path where an SSD could ignore the FDP part of the write request and still be semantically correct, just with worse performance or write amplification.

最近，Google 和 Meta 合作提出了灵活数据放置（FDP），它更像是一种对相关生命周期写入进行分组的提示，而不是像 ZNS 那样严格强制分区。其目标是实现更平滑的升级路径：SSD 可以忽略写请求中的 FDP 部分，依然能保证语义正确，只是性能或写入放大略差。

> Other improvements target not cost efficiency[5], but improving the set of features that storage devices support. Focusing on NVMe in particular, NVMe added a copy command, to remove the waste in reading and writing the same data. Fused compare-and-write commands allow a CAS operation to be pushed down to the drive itself, enabling crazy designs like pushing Optimistic Lock Coupling down into the drive itself. NVMe inherited the Data Integrity Field (DIF) / Data Integrity Extensions (DIX) support from SCSI, which allows pushing page checksums down into the drive. (Notably used by Oracle.) There are projects like KV-SSD that change the entire data model from storing blocks by index to storing objects by key, and head towards replacing software storage engines entirely. SSD manufacturers continue to make SSDs more capable of more operations over time.

其他改进的目标不是成本效率[5]，而是提升存储设备支持的功能集。特别关注 NVMe，它增加了 copy 命令，以消除读写相同数据造成的浪费。融合比较并写入（fused compare-and-write）命令允许将 CAS 操作下推到驱动器本身，从而支持一些疯狂的设计，如将乐观锁耦合（Optimistic Lock Coupling）下推到驱动器中。NVMe 继承了 SCSI 的数据完整性字段（DIF）/ 数据完整性扩展（DIX）支持，允许将页面校验和下推到驱动器（Oracle 显著使用了这一技术）。还有 KV-SSD 等项目，将整个数据模型从按索引存储块改为按键存储对象，朝着完全取代软件存储引擎的方向发展。SSD 制造商不断让 SSD 随时间推移支持更多操作。

*[5]: 如果你期待讨论持久内存，Intel 已经终结了 Optane（傲腾），所以目前那是一条死胡同。似乎还有 Kioxia（铠侠）或 Everspin 等几家公司在坚持，但我还没听说过关于它们的大规模使用。*

> As the penultimate step in SSD capabilities, SmartSSDs are coming into existence which allow for putting arbitrary compute into an SSD. Query processing on SmartSSDs: Opportunities and challenges surveys their application to query processing tasks. Pushing filters to storage is always advantageous; I’ve regularly linked previous work like PushdownDB leveraging S3 Select[6] as a great example on the analytics side. With SmartSSDs we get papers like POLARDB Meets Computational Storage. Even without specialized integration, there are arguments to be made that even transparent, in-drive compression can close the gap between B+ trees and LSMs in write amplification. Leveraging SmartSSDs is still a young field of research, but there’s incredible potential for impact.

作为 SSD 能力的倒数第二步，智能 SSD（SmartSSDs）正在出现，允许在 SSD 中放入任意计算。《智能 SSD 上的查询处理：机遇与挑战》调查了它们在查询处理任务中的应用。将过滤下推到存储总是更有利的；我经常引用此前的研究如利用 S3 Select[6] 的 PushdownDB，将其作为分析侧的一个极佳范例。有了 SmartSSDs，我们看到了《POLARDB 遇见计算存储》等论文。即使没有专门的集成，也有论点认为，即使是透明的驱动器内压缩，也能在写入放大方面缩小 B+ 树与 LSM 树之间的差距。利用 SmartSSDs 仍是一个年轻的研究领域，但具有巨大的影响力潜力。

*[6]: 截至 2024 年 7 月 25 日，AWS 已停止 S3 Select 服务的更新/推广，推测是为了支持 S3 Object Lambda。*

---

## 计算 (COMPUTE)

> OLTP and OLAP spend their compute time on significantly different types of work, so we’ll address the potential advances for each separately.


### 事务处理 (TRANSACTION PROCESSING)

> In a recent VLDB, two powerhouses of database research put forth a position paper of Cloud-Native Database Systems and Unikernels: Reimagining OS Abstractions for Modern Hardware, arguing that unikernels allow databases to specialize an OS for their exact needs. The early work on VMCache highlights the struggle in efficient database buffer management in particular, where one either accepts the complexity of pointer swizzling, or one hooks into the kernel and invokes mmap()-related syscalls frequently. Neither option is appealing, and unikernels instead offer direct access to virtual memory primitives. The effort required to develop unikernels is lowering as the area is getting more attention, and Akira Kurogane got MongoDB running as a unikernel via Unikraft with little effort, and subsequent posts showed a bit of performance improvement without any MongoDB-internal changes. There’s been an endless joke that databases want to become the OS, as the desire for performance improvements would require more control over networking, filesystems, disk I/O, memory, etc., and unikernel databases offer exactly that as a tangible possibility.

在最近的一次 VLDB 上，数据库研究领域的两位巨头提交了一篇立场论文《云原生数据库系统与独核 (Unikernels)：重新想象现代硬件的操作系统抽象》，认为 Unikernels 允许数据库针对其确切需求对操作系统进行专业化定制。关于 VMCache 的早期工作特别强调了在高效数据库缓冲管理中的挣扎：要么接受指针重定位（pointer swizzling）的复杂性，要么挂钩内核并频繁调用 mmap() 相关的系统调用。这两个选项都不理想，而 Unikernels 提供了对虚拟内存原语的直接访问。随着该领域获得更多关注，开发 Unikernels 的工作量正在降低，Akira Kurogane 通过 Unikraft 没费多少力气就让 MongoDB 在 Unikernel 上运行了起来，随后的博文显示，在不对 MongoDB 内部做任何改动的情况下，性能就有了一定提升。业内一直有个笑话：数据库总想变身成操作系统，因为追求性能提升需要对网络、文件系统、磁盘 I/O、内存等拥有更多控制权，而 Unikernel 数据库恰好将这种控制权变为了现实。

> For data confidentiality beyond just TLS or disk encryption, secure enclaves allow execution of verifiably untampered code, where the data being operated on is protected even from a compromised operating system. Whereas a Trusted Platform Module (TPM) allowed keys to be held securely within a machine, secure enclaves extend that protection to arbitrary code and data. This permits building databases that are tremendously more resilient to malicious compromise but with several constraints on their design. Microsoft has published on integrating secure enclaves into Hekaton, and has released the work as part of SQL Server Always Encrypted. Alibaba has also published about their efforts in building enclave-native storage engines for enterprise customers worried about data confidentiality. Databases have a history of being able to sell security improvements through the vehicle of regulatory compliance, and secure enclaves are a meaningful improvement in data confidentiality.

为了实现除 TLS 或磁盘加密之外的数据机密性，安全飞地（secure enclaves）允许执行经验证且未被篡改的代码，使得操作的数据即便是在操作系统被入侵的情况下也能得到保护。受信任平台模块（TPM）允许机器安全地保存密钥，而安全飞地则扩展到了任意代码和数据。这允许构建对恶意破坏具有极强抵御能力的数据库，但在设计上存在一些约束。微软发表过关于将安全飞地集成到 Hekaton 的文章，并已将其作为 SQL Server Always Encrypted 的一部分发布。阿里巴巴也发表了他们为担心数据机密性的企业客户构建飞地原生存储引擎的努力。数据库在通过合规手段销售安全改进方面有着悠久的历史，安全飞地是数据机密性方面的重大改进。

> After Spanner’s introduction of TrueTime, clock synchronization has become of notable interest for transaction ordering in geo-distributed databases. Each of the major cloud providers has an NTP offering that is tied to atomic clocks or GPS satellites (AWS, Azure, GCP). This is of great utility to any similar design, like CockroachDB or Yugabyte, for which clock synchronization is vital for correctness, and conservatively wide margins of error degrade performance. AWS’s recent Aurora Limitless also uses a TrueTime-like design. This is the only mention of cloud-specific not-quite-hardware because it uniquely involves major cloud vendors exposing expensive hardware (atomic clocks) that users otherwise wouldn’t have considered buying for themselves.

在 Spanner 引入 TrueTime 之后，时钟同步对于地理分布式数据库中的事务排序引起了显著关注。每个主流云供应商都有一个绑定到原子钟或 GPS 卫星的 NTP 服务（AWS, Azure, GCP）。这对于任何类似设计（如 CockroachDB 或 Yugabyte）都具有巨大的实用价值，对这些设计而言，时钟同步对正确性至关重要，保守的误差幅度会降低性能。AWS 最近的 Aurora Limitless 也使用了类似 TrueTime 的设计。这是本文唯一提到且并非完全属于硬件的云服务，因为它是主流云厂商将用户通常不会考虑购买的昂贵硬件（原子钟）暴露出来的案例。


> Hardware transactional memory has had a rather ill-fated history. Sun’s Rock processor featured hardware transactional memory right up until Sun was bought and Rock was shut down. Intel made two attempts at releasing it, and had to disable it both times[7]. There was some interesting work on the subject of applying hardware transactional memory to in-memory databases, but other than finding some old CPUs for experimentation, we all must wait until a CPU manufacturer says they’re planning to make another attempt at it.

硬件事务内存（HTM）的历史相当坎坷。Sun 的 Rock 处理器具备 HTM，直到 Sun 被收购且 Rock 项目被关闭。Intel 曾两次尝试发布它，但两次都不得不禁用它[7]。关于将 HTM 应用于内存数据库有一些有趣的研究，但除了找一些旧 CPU 进行实验外，我们都只能等待 CPU 制造商表示他们计划再次尝试。

*[7]: 第一次是因为 bug，第二次是因为一个破坏 KASLR 的侧信道攻击。还有一个因误解 CTF 挑战意图而发现的推测执行定时攻击。*

### 分析处理 (Analytical PROCESSING)

> Companies are consistently founded to leverage specialized hardware to accelerate query processing and achieve better performance and cost efficiency than their CPU-only competitors. GPU-powered databases, like Voltron, HEAVY.ai, and Brytlyt, are the first step in this direction. I wouldn’t be overly surprised if Intel or AMD integrated graphics gained OpenCL support[8] sometime in the future, which would open the door to all databases being able to assume some amount of GPU capabilities on a much wider set of hardware configurations.

一直有公司致力于利用专用硬件来加速查询处理，以期获得比纯 CPU 竞争对手更好的性能和成本效率。由 GPU 驱动的数据库，如 Voltron, HEAVY.ai 和 Brytlyt，是这一方向的第一步。如果 Intel 或 AMD 的集成显卡在未来获得 OpenCL 支持[8]，我不会感到意外，这将为所有数据库开启在更广泛的硬件配置上假设拥有一定 GPU 能力的大门。

*[8]: OpenGL 计算着色器（Compute Shaders）是利用 GPU 进行任意计算的最通用且可移植的形式，且集成显卡芯片组已经支持。但我还没找到任何研究利用它们进行数据库处理的论文？*

> There are also opportunities for using even more power-efficient hardware. The newest Neural Processing Units/Tensor Processing Units have already been shown to be adaptable into query processing in work like TCUDB: Accelerating Database with Tensor Processors. A few companies have attempted to utilize FPGAs. Swarm64 tried (and failed?) at this market. AWS made its own effort as Redshift AQUA. Going as far as ASICs seems to not be worth it for even the largest companies, as even Oracle stopped their SPARC development in 2017. I’m not overly optimistic about FPGAs through ASICs as memory bandwidth will be the primary bottleneck at some point anyway, but ADMS is the conference[9] to follow for papers in this overall area.

利用能效更高的硬件也有机会。最新的神经处理单元（NPUs）/ 张量处理单元（TPUs）已被证明可以适应查询处理，如《TCUDB: 利用张量处理器加速数据库》等工作。一些公司曾尝试利用 FPGA，Swarm64 在这个市场上尝试过（并失败了？）。AWS 通过 Redshift AQUA 进行了自己的尝试。走到 ASIC（专用集成电路）这一步似乎即便对大公司来说也不划算，甚至 Oracle 也在 2017 年停止了其 SPARC 开发。我对从 FPGA 到 ASIC 的路径并不乐观，因为内存带宽终究会在某一点成为主要瓶颈，但 ADMS 是关注这一领域论文的必看会议[9]。

*[9]: 好吧，技术上讲 ADMS 是附属于 VLDB 的一个研讨会（workshop），但我不知道有什么词可以概括会议、期刊和研讨会。*

---

## 云端可用性 (CLOUD AVAILABILITY)

> To finally address the depressing elephant in the room, none of these hardware advancements matter if they’re not accessible. For today’s systems, that means in the cloud, and the cloud doesn’t offer the forefront of hardware advancements to its customers.

最后，来谈谈房间里那个令人沮丧的大象：如果这些硬件进步无法触达用户，那么它们就毫无意义。对于今天的系统来说，这意味着在云端，而云服务商并没有向客户提供最前沿的硬件进步。

> For networking, the situation isn’t fantastic. DPDK is the most advanced networking technology that’s somewhat easily accessible, as most clouds allow some instance types to have more than one NIC. AWS offers pseudo-RDMA in the form of Secure Reliable Datagrams, which was benchmarked to be about halfway between TCP and RDMA. Real RDMA is only available on the High Performance Computing instances within Azure, GCP, and OCI. Only Alibaba offers RDMA on general-purpose compute instances[10]. SmartNICs are not available anywhere publicly. Some of this is for good reason: Microsoft has published papers that deploying RDMA is hard. In fact, it’s really hard. Even their papers about actually succeeding in using RDMA emphasize that it’s really hard. We’re nearing a full decade after Microsoft started using RDMA internally and it’s still not available in their cloud. I have no guesses as to if or when it will be.

在网络方面，情况不容乐观。DPDK 是目前比较容易获得的最高级网络技术，因为大多数云允许某些类型的实例拥有多个网卡。AWS 以安全可靠数据报（SRD）的形式提供伪 RDMA，基准测试显示其性能大约介于 TCP 和 RDMA 之间。真正的 RDMA 仅在 Azure, GCP 和 OCI 的高性能计算（HPC）实例中提供。只有阿里巴巴在通用计算实例上提供 RDMA[10]。智能网卡在任何地方都不公开提供。这其中有一些充分的理由：微软发表过论文称部署 RDMA 很难。事实上，非常难。甚至他们关于成功使用 RDMA 的论文也强调这真的很难。自微软开始在内部使用 RDMA 以来，已经快过去十年了，它在云端依然不可用。我无法预测它是否或何时会普及。

*[10]: 尽管可能会像 SRD 那样有一定的延迟损失。阿里巴巴通过 iWARP 部署 RDMA，速度应该会慢一点，但我还没看到相关的基准测试。*

> For storage, the situation isn’t really any better. The few times that SMR HDDs did reach consumers, it was as a drive that still presented itself as supporting a block storage API, and consumers hated it. ZNS SSDs seem similarly locked behind enterprise-only purchasing agreements. One might think that Intel discontinuing Optane-branded persistent memory and SSDs would mean that they’re not accessible on the cloud, but Alibaba still offers persistent memory optimized instances. The wonderful folk at Spare Cores actually provided me with nvme id-ctrl output from each cloud vendor, and no NVMe device they pulled presents itself as supporting nearly any optional features: copy, fused compare and write, data integrity extensions, nor multi-block atomic writes[11]. Alibaba is also the only cloud vendor that has invested into SmartSSDs with their collaboration with ScaleFlux on PolarDB. This still means SmartSSDs are not accessible to the general public, but even the paper acknowledges it’s "the first real-world deployment of cloud-native databases with computational storage drives ever reported in the open literature".

在存储方面，情况也没好到哪儿去。SMR 硬盘少数几次接触到消费者时，其驱动器仍伪装成支持块存储 API，消费者对此深恶痛绝。ZNS SSD 似乎同样被锁死在企业专属采购协议之后。你可能认为 Intel 停产 Optane 品牌持久内存和 SSD 意味着它们在云端不可用，但阿里巴巴依然提供持久内存优化实例。Spare Cores 的朋友们实际上为我提供了每个云厂商的 nvme id-ctrl 输出，他们拉取的 NVMe 设备都没有显示支持几乎任何可选功能：copy, fused compare and write, 数据完整性扩展，以及多块原子写入[12]。阿里巴巴也是唯一一家通过与 ScaleFlux 在 PolarDB 上的合作对智能 SSD 进行投入的云厂商。这依然意味着智能 SSD 对普通大众不可用，但即便该论文也承认这是“公开文献中首次报道的云原生数据库与计算存储驱动器的真实世界部署”。

*[11]: 尽管 AWS 支持防止断裂写入（torn write prevention），而 GCP 过去也有类似的文档。*

> On the compute side, the state finally gets a bit better. The cloud fully permits unikernels, and TPMs are widely accessible, but only AWS and Azure support secure enclaves as far as I can tell. NTP servers with atomic or GPS clocks are made available, but only AWS makes efforts at promising error bounds. Without promised error bounds, it is hard to critically rely on these clocks. (Hardware transactional memory isn’t available, but it’s hard to blame the clouds on that one.)

在计算方面，情况终于好了一点。云端完全允许 Unikernels，TPM 广泛可用，但据我所知只有 AWS 和 Azure 支持安全飞地。带有原子钟或 GPS 时钟的 NTP 服务器已提供，但只有 AWS 努力承诺了误差界限。（硬件事务内存不可用，但这很难怪罪到云厂商头上。）

*[12]: 理想情况下需要对等 DMA（P2P DMA）支持，以便能够直接从磁盘读取到 FPGA，而至少 AWS 的 F1 实例无法做到。*
k
> The explosion of AI means there’s good money behind making more efficient compute available. GPUs are available in all clouds. AWS[12], Azure, IBM, and Alibaba offer FPGA instances. (GCP and OCI don’t.) The unfortunate reality is also that faster compute only matters when compute is the bottleneck. Both GPUs and FPGAs suffer from having limited memory, and so one cannot maintain the database in their local memory. Instead, one relies on streaming data in and out of them, which means being limited by PCIe speeds. All of this would encourage thoughtful motherboard layout and bus design in an on-premise appliance, but that’s not feasible in the cloud.

AI 的爆发意味着有充足的资金投入到提供更高效的计算中。GPU 在所有云中都可用。AWS[12], Azure, IBM 和阿里巴巴提供 FPGA 实例（GCP 和 OCI 则没有）。不幸的现实是，只有在计算是瓶颈时，更快的计算才有意义。GPU 和 FPGA 都受限于内存容量，因此无法在其本地内存中维护数据库。相反，必须依赖数据的流进流出，这意味着受限于 PCIe 速度。所有这些都将鼓励在本地设备中进行深思熟虑的主板布局和总线设计，但在云端这是不可行的。

> Thus we end with my bleak view on the next generation of databases: no one[13] can build databases that critically depend on new hardware advancements until they’re made available, but no cloud vendor wants to deploy hardware that can’t be immediately used. The next generation of databases is held back by the cyclic dependency that it doesn’t yet exist.

因此，我对下一代数据库的看法有些暗淡：在新的硬件进步普及之前，没有人[13]能够构建批判性依赖于这些进步的数据库，但没有云厂商愿意部署无法立即投入使用的硬件。下一代数据库正被这种“因尚未存在而无法部署”的循环依赖所绑架。

*[13]: 云厂商自己除外。最显著的是，微软和 Google 已经在内部拥有 RDMA 并广泛利用于其数据库产品中，同时却不允许公众使用。我的草稿箱里有一篇构思了很久的帖子，标题是《RDMA 对云厂商的竞争优势》。*

> Alibaba is shockingly great though. They’re consistently at the forefront of making hardware advances available for everything. I’m surprised I don’t see Alibaba being frequently used for benchmarking in academia and industry correspondingly.

不过阿里巴巴表现得惊人地出色。他们始终走在让一切硬件进步可用的最前沿。我很惊讶在学术界和工业界的基准测试中，阿里巴巴并没有被相应地频繁使用。
