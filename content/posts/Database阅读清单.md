---
title: "Database阅读清单"
date: 2025-08-09T00:00:00+08:00
categories:
  - 数据库
tags:
  - 数据库
  - 论文
description: ""
draft: false
---

## 最新关注（Cloud Storage & Recent）

| 论文 | 简介 |
|------|------|
| **Here, There and Everywhere: The Past, the Present and the Future of Local Storage in Cloud** | 阿里最佳论文，关于云本地存储的演进 |
| **ACOS: Apple's Geo-Distributed Object Store at Exabyte Scale** | 苹果的对象存储演进，极少大厂对象存储文章 |
| **Cost-efficient Archive Cloud Storage with Tape: Design and Deployment** | 2025年存储涨价后，磁带存储成为新主流 |
| **An Efficient Cloud Storage Model with Compacted Metadata Management for Performance Monitoring Timeseries Systems** | 时序系统文章，将维度数据分离的组织格式放到对象存储 |
| **"Range as a Key" is the Key! Fast and Compact Cloud Block Store Index with RASK** | EBS出品，必属精品 |
| **Holistic and Automated Task Scheduling for Distributed LSM-tree-based Storage** | 分布式LSM树调度，与分布式Compact模块面临的问题类似 |
| **PolarStore: High-Performance Data Compression for Large-Scale Cloud-Native Databases** | VLDB最佳论文提名 |

---

## 学者推荐

| 学者 | 主页 | 简介 |
|------|------|------|
| **Thomas Neumann** | [Google Scholar](https://scholar.google.com/citations?view_op=list_works&hl=en&hl=en&user=xSDfDpsAAAAJ) | 德国人，Hyper（内存OLTP/OLAP数据库）主要作者，Join Reorder专家 |
| **Goetz Graefe** | [DBLP](https://dblp.org/pid/g/GoetzGraefe.html) | SQL Server优化器大拿，目前在Google，Optimizer领域很多文章，Mr. BTree；[SIGMOD采访](https://sigmodrecord.org/publications/sigmodRecord/2009/pdfs/05_Profiles_Graefe.pdf) |
| **Benoit Dageville** | [DBLP](https://dblp.org/pid/59/847.html) | Oracle大拿，Snowflake创始人 |
| **Marcin Zukowski** | [DBLP](https://dblp.org/pid/48/1954.html) | 女，优化器，IBM研究院；System R系统 |
| **Mohamed Soliman** | [DBLP](https://dblp.org/pid/07/1330.html) | Orca论文作者，之前在Pivotal工作，目前在Apple |
| **Maximilian Schüle** | [DBLP](https://dblp.org/pid/15/10872.html) | 德国博士生，Hyrise项目，现在在Snowflake |
| **阿里云-遥凌** | [阿里云](https://developer.aliyun.com/profile/expert/jipp3rk57eobi/article_1) | 阿里云数据库专家 |
| **微软Spark实践** | [VLDB'21](https://vldb.org/pvldb/vol14/p3122-roy.pdf) | 关于计算复用、View Feedback、workload调优等 |

---

## AI for DB

### 资源
- [CWI MSc Projects](https://homepages.cwi.nl/~boncz/msc/)
- [PingCap Awesome Database Learning](https://github.com/pingcap/awesome-database-learning)
- [Tsinghua AIDB](https://github.com/TsinghuaDatabaseGroup/AIDB)

### Consensus

- [The Part-Time Parliament](https://dl.acm.org/doi/pdf/10.1145/3335772.3335939) (1998) - Lamport, Leslie
- [Paxos Made Simple](https://www.microsoft.com/en-us/research/publication/2016/12/paxos-simple-Copy.pdf) (2001) - Lamport, Leslie
- [Consensus: Bridging theory and practice](https://github.com/lonng/db-papers/blob/main/papers/consensus/consensus:-bridging-theory-and-practice.pdf) (2014) - Ongaro, Diego
- [In search of an understandable consensus algorithm (extended version)](https://github.com/lonng/db-papers/blob/main/papers/consensus/in-search-of-an-understandable-consensus-algorithm-(extended-version).pdf) (2014) - Ongaro, Diego, and John Ousterhout
- [Distributed consensus revised](https://github.com/lonng/db-papers/blob/main/papers/consensus/distributed-consensus-revised.pdf) (2019) - Howard, Heidi
- [A Generalised Solution to Distributed Consensus](https://github.com/lonng/db-papers/blob/main/papers/consensus/a-generalised-solution-to-distributed-consensus.pdf) (2019) - Howard, Heidi, and Richard Mortier
- [Paxos vs Raft: Have we reached consensus on distributed consensus?](https://dl.acm.org/doi/pdf/10.1145/3380787.3393681) (2020) - Howard, Heidi, and Richard Mortier

### Consistency

- [Consistency Tradeoffs in Modern Distributed Database System Design](https://github.com/lonng/db-papers/blob/main/papers/consistency/consistency-tradeoffs-in-modern-distributed-database-system-design.pdf) (2012) - Abadi, Daniel
- [Logical physical clocks and consistent snapshots in globally distributed databases](https://github.com/lonng/db-papers/blob/main/papers/consistency/logical-physical-clocks-and-consistent-snapshots-in-globally-distributed-databases.pdf) (2014) - Kulkarni S S, et al.
- [Ark: A Real-World Consensus Implementation](https://github.com/lonng/db-papers/blob/main/papers/consistency/ark:-a-real-world-consensus-implementation.pdf) (2014) - Kasheff, Zardosht, and Leif Walsh
- [PolarFS: an ultra-low latency and failure resilient distributed file system for shared storage cloud database](https://dl.acm.org/doi/pdf/10.14778/3229863.3229872) (2018) - Cao, Wei, et al.
- [Anna: A kvs for any scale](https://github.com/lonng/db-papers/blob/main/papers/consistency/anna:-a-kvs-for-any-scale.pdf) (2018) - Wu, Chenggang, et al.
- [Strong and efficient consistency with consistency-aware durability](https://dl.acm.org/doi/pdf/10.1145/3423138) (2021) - Ganesan, Aishwarya, et al.

---

## System Design

### RDBMS

- [System R: Relational Approach to Database Management](https://dl.acm.org/doi/pdf/10.1145/320455.320457) (1976) - Astrahan, Morton M., et al.
- [The design and implementation of INGRES](https://dl.acm.org/doi/10.1145/320473.320476) (1976) - Stonebraker, Michael, et al.
- [The design of Postgres](https://dl.acm.org/doi/pdf/10.1145/16856.16888) (1986) - Stonebraker, Michael, and Lawrence A. Rowe
- [Query Processing in Main Memory Database Management Systems](https://dl.acm.org/doi/pdf/10.1145/16894.16878) (1986) - Lehman, Tobin J., and Michael J. Carey
- [Megastore: Providing Scalable, Highly Available Storage for Interactive Services](https://github.com/lonng/db-papers/blob/main/papers/rdbms/megastore:-providing-scalable,-highly-available-storage-for-interactive-services.pdf) (2011) - Baker J, et al.
- [Spanner: Google's globally distributed database](https://dl.acm.org/doi/pdf/10.1145/2491245) (2013) - Corbett, James C., et al.
- [Online, Asynchronous Schema Change in F1](https://dl.acm.org/doi/pdf/10.14778/2536222.2536230) (2013) - Rae, Ian, et al.
- [Amazon aurora: Design considerations for high throughput cloud-native relational databases](https://dl.acm.org/doi/pdf/10.1145/3035918.3056101) (2017) - Verbitski, Alexandre, et al.
- [Looking Back at Postgres](https://github.com/lonng/db-papers/blob/main/papers/rdbms/looking-back-at-postgres.pdf) (2019) - Hellerstein, Joseph M.
- [CockroachDB: The Resilient Geo-Distributed SQL Database](https://dl.acm.org/doi/pdf/10.1145/3318464.3386134) (2020) - Taft, Rebecca, et al.
- [F1 Lightning: HTAP as a Service](https://dl.acm.org/doi/pdf/10.14778/3415478.3415553) (2020) - Yang, Jiacheng, et al.
- [TiDB: a Raft-based HTAP database](https://dl.acm.org/doi/pdf/10.14778/3415478.3415535) (2020) - Huang, Dongxu, et al.
- [PolarDB Serverless: A Cloud Native Database for Disaggregated Data Centers](https://dl.acm.org/doi/pdf/10.1145/3448016.3457560) (2021) - Cao, Wei, et al.

### NoSQL

- [Bigtable: A Distributed Storage System for Structured Data](https://dl.acm.org/doi/pdf/10.1145/1365815.1365816) (2006) - Chang, Fay, et al.
- [Dynamo: Amazon's Highly Available Key-value Store](https://dl.acm.org/doi/pdf/10.1145/1323293.1294281) (2007) - DeCandia, Giuseppe, et al.
- [PNUTS: Yahoo!'s Hosted Data Serving Platform](https://dl.acm.org/doi/pdf/10.14778/1454159.1454167) (2008) - Cooper, Brian F., et al.
- [Cassandra - A Decentralized Structured Storage System](https://dl.acm.org/doi/pdf/10.1145/1773912.1773922) (2010) - Lakshman, Avinash, and Prashant Malik
- [Windows azure storage: a highly available cloud storage service with strong consistency](https://dl.acm.org/doi/pdf/10.1145/2043556.2043571) (2011) - Calder, Brad, et al.
- [Azure data lake store: a hyperscale distributed file service for big data analytics](https://dl.acm.org/doi/pdf/10.1145/3035918.3056100) (2017) - Ramakrishnan, Raghu, et al.
- [PNUTS to Sherpa: Lessons from Yahoo!'s Cloud Database](https://dl.acm.org/doi/pdf/10.14778/3352063.3352146) (2019) - Cooper, Brian F., et al.

---

## 优化器

### Essentials

- [A relational model of data for large shared data banks](https://dl.acm.org/doi/pdf/10.1145/362384.362685) (1970) - Codd, Edgar F.
- [SEQUEL: A structured English query language](https://dl.acm.org/doi/pdf/10.1145/800296.811515) (1974) - Chamberlin, Donald D., and Raymond F. Boyce
- [INGRES: a relational data base system](https://dl.acm.org/doi/pdf/10.1145/1499949.1500029) (1975) - Held, G. D., M. R. Stonebraker, and Eugene Wong
- [Extending the database relational model to capture more meaning](https://dl.acm.org/doi/pdf/10.1145/320107.320109) (1979) - Codd, Edgar F.
- [A critique of the SQL database language](https://dl.acm.org/doi/pdf/10.1145/984549.984551) (1984) - Date, C. J.

### Planner Framework

- [Access Path Selection in a Relational Database Management System](https://dl.acm.org/doi/pdf/10.1145/582095.582099) (1979) - Selinger, P. Griffiths, et al. ⭐ 经典必读
- [Query Optimization by Simulated Annealing](https://dl.acm.org/doi/pdf/10.1145/38713.38722) (1987) - Ioannidis, Yannis E., and Eugene Wong
- [The EXODUS Optimizer Generator](https://dl.acm.org/doi/pdf/10.1145/38713.38734) (1987) - Graefe, Goetz, and David J. DeWitt
- [Extensible/Rule Based Query Rewrite Optimization in Starburst](https://dl.acm.org/doi/pdf/10.1145/141484.130294) (1992) - Pirahesh, Hamid, et al.
- [The Volcano Optimizer Generator- Extensibility and Efficient Search](https://github.com/lonng/db-papers/blob/main/papers/optimizer-framework/the-volcano-optimizer-generator--extensibility-and-efficient-search.pdf) (1993) - Graefe, Goetz, and William J. McKenna
- [The Cascades Framework for Query Optimization](https://github.com/lonng/db-papers/blob/main/papers/optimizer-framework/the-cascades-framework-for-query-optimization.pdf) (1995) - Graefe, Goetz ⭐ 经典必读
- [An Overview of Query Optimization in Relational Systems](https://dl.acm.org/doi/pdf/10.1145/275487.275492) (1998) - Chaudhuri, Surajit
- [Robust Query Processing through Progressive Optimization](https://dl.acm.org/doi/pdf/10.1145/1007568.1007642) (2004) - Markl, Volker, et al.
- [Orca: A Modular Query Optimizer Architecture for Big Data](https://dl.acm.org/doi/pdf/10.1145/2588555.2595637) (2014) - Soliman, Mohamed A., et al. ⭐ 现代优化器
- [Parallelizing Query Optimization on Shared-Nothing Architectures](https://github.com/lonng/db-papers/blob/main/papers/optimizer-framework/parallelizing-query-optimization-on-shared-nothing-architectures.pdf) (2015) - Trummer, Immanuel, and Christoph Koch
- [The MemSQL Query Optimizer: A modern optimizer for real-time analytics in a distributed database](https://dl.acm.org/doi/pdf/10.14778/3007263.3007277) (2016) - Chen, Jack, et al.

### Transformation

- [Processing queries with quantifiers a horticultural approach](https://dl.acm.org/doi/pdf/10.1145/588058.588075) (1983) - Dayal, Umeshwar
- [Translating SQL into relational algebra: Optimization, semantics, and equivalence of SQL queries](https://www.academia.edu/download/50687636/tse.1985.23222320161202-29901-8u86ef.pdf) (1985) - Ceri, Stefano, and Georg Gottlob
- [Grammar-like Functional Rules for Representing Query Optimization Alternatives](https://dl.acm.org/doi/pdf/10.1145/971701.50204) (1988) - Lohman, Guy M.
- [Query Optimization by Predicate Move-Around](https://www.researchgate.net/profile/Inderpal-Mumick/publication/2754592_Query_Optimization_by_Predicate_Move-Around/links/0f317534d437e49755000000/Query-Optimization-by-Predicate-Move-Around.pdf) (1994) - Levy, Alon Y., et al.
- [Eager Aggregation and Lazy Aggregation](https://www.researchgate.net/profile/Per-Ake-Larson/publication/2733082_Eager_Aggregation_and_Lazy_Aggregation/links/02bfe50ce6de3dad7c000000/Eager-Aggregation-and-Lazy-Aggregation.pdf) (1995) - Yan, Weipeng P., and Per-Bike Larson
- [Parameterized Queries and Nesting Equivalences](https://www.microsoft.com/en-us/research/wp-content/uploads/2016/02/tr-2000-31.pdf) (2000) - Galindo-Legaria, C. A.
- [Cost-based query transformation in Oracle](https://www.researchgate.net/profile/Rafi-Ahmed-2/publication/221311318_Cost-Based_Query_Transformation_in_Oracle/links/572bbc5e08aef7c7e2c6b829/Cost-Based-Query-Transformation-in-Oracle.pdf) (2006) - Ahmed, Rafi, et al.

### Nested Query

- [Using semi-joins to solve relational queries](https://dl.acm.org/doi/pdf/10.1145/322234.322238) (1981) - Bernstein, Philip A., and Dah-Ming W. Chiu
- [On optimizing an SQL-like nested query](https://dl.acm.org/doi/pdf/10.1145/319732.319745) (1982) - Kim, Won
- [Optimization of nested queries in a distributed relational database](https://github.com/lonng/db-papers/blob/main/papers/nested-query/optimization-of-nested-queries-in-a-distributed-relational-database.pdf) (1984) - Lohman, Guy M., et al.
- [SQL-like and Quel-like correlation queries with aggregates revisited](https://github.com/lonng/db-papers/blob/main/papers/nested-query/sql-like-and-quel-like-correlation-queries-with-aggregates-revisited.pdf) (1984) - Kiessling, Werner
- [Optimization of nested SQL queries revisited](https://dl.acm.org/doi/pdf/10.1145/38714.38723) (1987) - Ganski, Richard A., and Harry KT Wong
- [A Unitied Approach to Processing Queries That Contain Nested Subqueries, Aggregates, and Quantifiers](https://github.com/lonng/db-papers/blob/main/papers/nested-query/a-unitied-approach-to-processing-queries-that-contain-nested-subqueries,-aggregates,-and-quantifiers.pdf) (1987) - Dayal, Umeshwar
- [Optimization of correlated SQL queries in a relational database management system](https://github.com/lonng/db-papers/blob/main/papers/nested-query/optimization-of-correlated-sql-queries-in-a-relational-database-management-system.pdf) (1998) - Jou, Michelle M., et al.
- [Orthogonal Optimization of Subqueries and Aggregation](https://dl.acm.org/doi/pdf/10.1145/376284.375748) (2001) - Galindo-Legaria, César, and Milind Joshi
- [WinMagic: Subquery Elimination Using Window Aggregation](https://dl.acm.org/doi/pdf/10.1145/872757.872840) (2003) - Zuzarte, Calisto, et al.
- [Execution strategies for SQL subqueries](https://dl.acm.org/doi/pdf/10.1145/1247480.1247598) (2007) - Elhemali, Mostafa, et al.
- [Enhanced subquery optimizations in Oracle](https://dl.acm.org/doi/pdf/10.14778/1687553.1687563) (2009) - Bellamkonda, Srikanth, et al.
- [Unnesting Arbitrary Queries](https://github.com/lonng/db-papers/blob/main/papers/nested-query/unnesting-arbitrary-queries.pdf) (2015) - Neumann, Thomas, and Alfons Kemper

### Join Order Optimization

- [Access paths in the "Abe" statistical query facility](https://dl.acm.org/doi/pdf/10.1145/582353.582382) (1982) - Klug, Anthony
- [Extending the Algebraic Framework of Query Processing to Handle Outerjoins](https://github.com/lonng/db-papers/blob/main/papers/join-order/extending-the-algebraic-framework-of-query-processing-to-handle-outerjoins.pdf) (1984) - Rosenthal, A., and D. Reiner
- [Analysis of Two Existing and One New Dynamic Programming Algorithm for the Generation of Optimal Bushy Join Trees without Cross Products](https://www.researchgate.net/profile/Thomas_Neumann2/publication/47861835_Analysis_of_Two_Existing_and_One_New_Dynamic_Programming_Algorithm_for_the_Generation_of_Optimal_Bushy_Join_Trees_without_Cross_Products/links/0912f506d90ad19031000000.pdff) (2006) - Moerkotte, Guido, and Thomas Neumann
- [Dynamic programming strikes back](https://dl.acm.org/doi/pdf/10.1145/1376616.1376672) (2008) - Moerkotte, Guido, and Thomas Neumann
- [On the Correct and Complete Enumeration of the Core Search Space](https://dl.acm.org/doi/pdf/10.1145/2463676.2465314) (2013) - Moerkotte, Guido, Pit Fender, and Marius Eich
- [How Good Are Query Optimizers, Really?](https://dl.acm.org/doi/pdf/10.14778/2850583.2850594) (2015) - Leis, Viktor, et al. ⭐ 必读
- [The Complete Story of Joins](https://github.com/lonng/db-papers/blob/main/papers/join-order/the-complete-story-of-joins.pdf) (2017) - Neumann, Thomas, Viktor Leis, and Alfons Kemper
- [Improving Join Reorderability with Compensation Operators](https://dl.acm.org/doi/pdf/10.1145/3183713.3183731) (2018) - Wang, TaiNing, and Chee-Yong Chan
- [Adaptive Optimization of Very Large Join Queries](https://dl.acm.org/doi/pdf/10.1145/3183713.3183733) (2018) - Neumann, Thomas, and Bernhard Radke

### Functional Dependency & Physical Properties

- [Fundamental Techniques for Order Optimization](https://dl.acm.org/doi/pdf/10.1145/233269.233320) (1996) - Simmen, David, et al.
- [Exploiting Functional Dependence in Query Optimization (Thesis)](https://github.com/lonng/db-papers/blob/main/papers/functional-dependencies/%5Bthesis%5D-exploiting-functional-dependence-in-query-optimization.pdf) (2000) - Paulley, Glenn Norman
- [An Efficient Framework for Order Optimization](https://github.com/lonng/db-papers/blob/main/papers/functional-dependencies/an-efficient-framework-for-order-optimization.pdf) (2004) - Neumann, Thomas, and Guido Moerkotte
- [Incorporating Partitioning and Parallel Plans into the SCOPE Optimizer](https://github.com/lonng/db-papers/blob/main/papers/functional-dependencies/incorporating-partitioning-and-parallel-plans-into-the-scope-optimizer.pdf) (2010) - Zhou, Jingren, Per-Ake Larson, and Ronnie Chaiken
- [Accelerating Queries with GroupBy and Join by Group join](https://dl.acm.org/doi/pdf/10.14778/3402707.3402723) (2011) - Moerkotte, Guido, and Thomas Neumann

### Cost Model

- [Modelling Costs for a MM-DBMS](https://www.semanticscholar.org/paper/Modelling-Costs-for-a-MM-DBMS-Listgarten-Neimat/42b88445cfb28fbe4b6539c97674a8fa9815e635) (1996) - Listgarten, Sherry, and Marie-Anne Neimat
- [SEEKing the truth about ad hoc join costs](https://github.com/lonng/db-papers/blob/main/papers/cost-model/seeking-the-truth-about-ad-hoc-join-costs.pdf) (1997) - Haas, Laura M., et al.
- [Approximation Schemes for Many-Objective Query Optimization](https://dl.acm.org/doi/pdf/10.1145/2588555.2610527) (2014) - Trummer, Immanuel, and Christoph Koch
- [Multi-Objective Parametric Query Optimization](https://dl.acm.org/doi/pdf/10.1145/3068612) (2015) - Trummer, Immanuel, and Christoph Koch

### Execution Engine

- [Query Evaluation Techniques for Large Databases](https://dl.acm.org/doi/pdf/10.1145/152610.152611) (1993) - Graefe G.
- [Volcano - An Extensible and Parallel Query Evaluation System](https://github.com/lonng/db-papers/blob/main/papers/execution-engine/volcano---an-extensible-and-parallel-query-evaluation-system.pdf) (1994) - Graefe, Goetz
- [MonetDB/X100: Hyper-Pipelining Query Execution](https://www.researchgate.net/profile/Niels-Nes/publication/45338800_MonetDBX100_Hyper-Pipelining_Query_Execution/links/0deec520cd1e8a3607000000/MonetDB-X100-Hyper-Pipelining-Query-Execution.pdf) (2005) - Boncz, Peter A., et al.
- [Efficiently Compiling Efficient Query Plans for Modern Hardware](https://dl.acm.org/doi/pdf/10.14778/2002938.2002940) (2011) - Neumann, Thomas
- [Multi-Core, Main-Memory Joins: Sort vs. Hash Revisited](https://dl.acm.org/doi/pdf/10.14778/2732219.2732227) (2013) - Balkesen, Cagri, et al.
- [Morsel-Driven Parallelism: A NUMA-Aware Query Evaluation Framework for the Many-Core Age](https://dl.acm.org/doi/pdf/10.1145/2588555.2610507) (2014) - Leis, Viktor, et al.
- [Relaxed Operator Fusion for In-Memory Databases: Making Compilation, Vectorization, and Prefetching Work Together At Last](https://dl.acm.org/doi/pdf/10.14778/3151113.3151114) (2017) - Menon, Prashanth, et al.
- [Looking Ahead Makes Query Plans Robust](https://dl.acm.org/doi/pdf/10.14778/3090163.3090167) (2017) - Zhu, Jianqiao, et al.
- [Everything You Always Wanted to Know About Compiled and Vectorized Queries But Were Afraid to Ask](https://dl.acm.org/doi/pdf/10.14778/3275366.3284966) (2018) - Kersten, Timo, et al. ⭐ 必读
- [SuRF: Practical Range Query Filtering with Fast Succinct Tries](https://dl.acm.org/doi/pdf/10.1145/3183713.3196931) (2018) - Zhang, Huanchen, et al.
- [Adaptive Execution of Compiled Queries](https://15721.courses.cs.cmu.edu/spring2019/papers/19-compilation/kohn-icde2018.pdff) (2018) - Kohn, André, et al.

### MPP Optimizations

- [DB2 Parallel Edition](https://github.com/lonng/db-papers/blob/main/papers/mpp-optimizations/db2-parallel-edition.pdf) (1995) - Baru, Chaitanya K., et al.
- [Parallel SQL execution in Oracle 10g](https://dl.acm.org/doi/pdf/10.1145/1007568.1007666) (2004) - Cruanes, Thierry, et al.
- [Query Optimization in Microsoft SQL Server PDW](https://dl.acm.org/doi/pdf/10.1145/2213836.2213953) (2012) - Shankar, Srinath, et al.
- [Adaptive and big data scale parallel execution in Oracle](https://dl.acm.org/doi/pdf/10.14778/2536222.2536235) (2013) - Bellamkonda, Srikanth, et al.
- [Optimizing Queries over Partitioned Tables in MPP Systems](https://dl.acm.org/doi/pdf/10.1145/2588555.2595640) (2014) - Antova, Lyublena, et al.

---

## Storage Engine

### Storage Structure

- [The Ubiquitous B-Tree](https://dl.acm.org/doi/pdf/10.1145/356770.356776) (1979) - Comer, Douglas
- [The 5 Minute Rule for Trading Memory for Disc Accesses and the 5 Byte Rule for Trading Memory for CPU Time](https://dl.acm.org/doi/pdf/10.1145/38713.38755) (1987) - Gray, Jim, and Franco Putzolu ⭐ 经典必读
- [The Log-Structured Merge-Tree (LSM-Tree)](https://github.com/lonng/db-papers/blob/main/papers/storage-structure/the-log-structured-merge-tree-(lsm-tree).pdf) (1996) - O'Neil, Patrick, et al. ⭐ 经典必读
- [The five-minute rule ten years later, and other computer storage rules of thumb](https://dl.acm.org/doi/pdf/10.1145/271074.271094) (1997) - Gray, Jim, and Goetz Graefe
- [The Five Minute Rule 20 Years Later and How Flash Memory Changes the Rules](https://dl.acm.org/doi/pdf/10.1145/1363189.1363198) (2008) - Graefe, Goetz
- [A Comparison of Fractal Trees to Log-Structured Merge (LSM) Trees](https://github.com/lonng/db-papers/blob/main/papers/storage-structure/a-comparison-of-fractal-trees-to-log-structured-merge-(lsm)-trees.pdf) (2014) - Kuszmaul, Bradley C.
- [Design Tradeoffs of Data Access Methods](https://dl.acm.org/doi/pdf/10.1145/2882903.2912569) (2016) - Athanassoulis, Manos, and Stratos Idreos
- [Designing Access Methods: The RUM Conjecture](https://github.com/lonng/db-papers/blob/main/papers/storage-structure/designing-access-methods:-the-rum-conjecture.pdf) (2016) - Athanassoulis, Manos, et al.
- [The five minute rule thirty years later and its impact on the storage hierarchy](https://infoscience.epfl.ch/record/230398/files/adms-talk.pdf) (2017) - Appuswamy, Raja, et al.
- [WiscKey: Separating Keys from Values in SSD-conscious Storage](https://dl.acm.org/doi/pdf/10.1145/3033273) (2017) - Lu, Lanyue, et al.
- [Managing Non-Volatile Memory in Database Systems](https://dl.acm.org/doi/pdf/10.1145/3183713.3196897) (2018) - van Renen, Alexander, et al.
- [LeanStore: In-Memory Data Management Beyond Main Memory](https://github.com/lonng/db-papers/blob/main/papers/storage-structure/leanstore:-in-memory-data-management-beyond-main-memory.pdf) (2018) - Leis, Viktor, et al.
- [The Case for Learned Index Structures](https://dl.acm.org/doi/pdf/10.1145/3183713.3196909) (2018) - Kraska, Tim, et al. ⭐ 必读
- [LSM-based Storage Techniques: A Survey](https://github.com/lonng/db-papers/blob/main/papers/storage-structure/lsm-based-storage-techniques:-a-survey.pdf) (2019) - Luo, Chen, and Michael J. Carey
- [Learning Multi-dimensional Indexes](https://dl.acm.org/doi/pdf/10.1145/3318464.3380579) (2019) - Nathan, Vikram, et al.
- [Umbra: A Disk-Based System with In-Memory Performance](https://github.com/lonng/db-papers/blob/main/papers/storage-structure/umbra:-a-disk-based-system-with-in-memory-performance.pdf) (2020) - Neumann, Thomas, and Michael J. Freitag
- [XIndex: A Scalable Learned Index for Multicore Data Storage](https://dl.acm.org/doi/pdf/10.1145/3332466.3374547) (2020) - Tang, Chuzhe, et al.
- [The PGM-index: a fully-dynamic compressed learned index with provable worst-case bounds](https://dl.acm.org/doi/pdf/10.14778/3389133.3389135) (2020) - Ferragina, Paolo, and Giorgio Vinciguerra
- [From WiscKey to Bourbon: A Learned Index for Log-Structured Merge Trees](https://github.com/lonng/db-papers/blob/main/papers/storage-structure/from-wisckey-to-bourbon:-a-learned-index-for-log-structured-merge-trees.pdf) (2020) - Dai, Yifan, et al.
- [CaaS-LSM: Compaction-as-a-Service for LSM-based Key-Value Stores in Storage Disaggregated Infrastructure](https://github.com/lonng/db-papers/blob/main/papers/storage-structure/caas-lsm:-compaction-as-a-service-for-lsm-based-key-value-stores-in-storage-disaggregated-infrastructure.pdf) (2024) - Yu, Qiaolin et al.

### Transaction

- [The Notions of Consistency and Predicate Locks in a Database System](https://dl.acm.org/doi/pdf/10.1145/360363.360369) (1976) - Eswaran, Kapali P., et al.
- [Concurrency Control in Distributed Database Systems](https://dl.acm.org/doi/pdf/10.1145/356842.356846) (1981) - Bernstein, Philip A., and Nathan Goodman
- [On Optimistic Methods for Concurrency Control](https://dl.acm.org/doi/pdf/10.1145/319566.319567) (1981) - Kung, Hsiang-Tsung, and John T. Robinson
- [Principles of transaction-oriented database recovery](https://dl.acm.org/doi/10.1145/289.291) (1983) - Haerder, Theo, and Andreas Reuter
- [Multiversion Concurrency Control - Theory and Algorithms](https://dl.acm.org/doi/pdf/10.1145/319996.319998) (1983) - Bernstein, Philip A., and Nathan Goodman
- [ARIES: A transaction recovery method supporting fine-granularity locking and partial rollbacks using write-ahead logging](https://dl.acm.org/doi/pdf/10.1145/128765.128770) (1992) - Mohan C, et al. ⭐ 经典必读
- [A Critique of ANSI SQL Isolation Levels](https://dl.acm.org/doi/pdf/10.1145/568271.223785) (1995) - Berenson, Hal, et al.
- [Generalized Isolation Level Definitions](https://github.com/lonng/db-papers/blob/main/papers/transaction/generalized-isolation-level-definitions.pdf) (2000) - Adya, Atul, et al.
- [Serializable Snapshot Isolation in PostgreSQL](https://arxiv.org/pdf/1208.4179.pdf) (2012) - Ports, Dan RK, and Kevin Grittner
- [Calvin: Fast Distributed Transactions for Partitioned Database Systems](https://dl.acm.org/doi/pdf/10.1145/2213836.2213838) (2012) - Thomson, Alexander, et al.
- [MaaT: effective and scalable coordination of distributed transactions in the cloud](https://dl.acm.org/doi/pdf/10.14778/2732269.2732270) (2014) - Mahmoud, Hatem A., et al.
- [Staring into the Abyss: An Evaluation of Concurrency Control with One Thousand Cores](https://github.com/lonng/db-papers/blob/main/papers/transaction/staring-into-the-abyss:-an-evaluation-of-concurrency-control-with-one-thousand-cores.pdf) (2014) - Yu, Xiangyao, et al.
- [An Evaluation of the Advantages and Disadvantages of Deterministic Database Systems](https://dl.acm.org/doi/pdf/10.14778/2732951.2732955) (2014) - Ren, Kun, et al.
- [Fast Serializable Multi-Version Concurrency Control for Main-Memory Database Systems](https://dl.acm.org/doi/pdf/10.1145/2723372.2749436) (2015) - Neumann, Thomas, et al.
- [An Empirical Evaluation of In-Memory Multi-Version Concurrency Control](https://dl.acm.org/doi/pdf/10.14778/3067421.3067427) (2017) - Wu, Yingjun, et al.
- [An Evaluation of Distributed Concurrency Control](https://dl.acm.org/doi/pdf/10.14778/3055540.3055548) (2017) - Harding, Rachael, et al.
- [Scalable Garbage Collection for In-Memory MVCC Systems](https://dl.acm.org/doi/pdf/10.14778/3364324.3364328) (2019) - Böttcher, Jan, et al.

### Scheduling

- [Automated Demand-driven Resource Scaling in Relational Database-as-a-Service](https://dl.acm.org/doi/pdf/10.1145/2882903.2903733) (2016) - Das, Sudipto, et al.
- [Autoscaling Tiered Cloud Storage in Anna](https://dl.acm.org/doi/pdf/10.14778/3311880.3311881) (2019) - Wu, Chenggang, et al.
- [Adaptive HTAP through Elastic Resource Scheduling](https://dl.acm.org/doi/pdf/10.1145/3318464.3389783) (2020) - Raza, Aunn, et al.
- [MorphoSys: Automatic Physical Design Metamorphosis for Distributed Database Systems](https://dl.acm.org/doi/pdf/10.14778/3424573.3424578) (2020) - Abebe, Michael, et al.

---

## Query Feedback Loop

- [LEO – DB2's LEarning Optimizer](https://github.com/lonng/db-papers/blob/main/papers/probabilistic-counting/leo-%E2%80%93-db2%E2%80%99s-learning-optimizer.pdf) (2001) - Stillger, Michael, et al.
- [Robust Query Processing through Progressive Optimization](https://dl.acm.org/doi/pdf/10.1145/1007568.1007642) (2004) - Markl, Volker, et al.
- [Automated Statistics Collection in DB2 UDB](https://github.com/lonng/db-papers/blob/main/papers/statistics/automated-statistics-collection-in-db2-udb.pdf) (2004) - Aboulnaga, Ashraf, et al.
- [Automatic SQL Tuning in Oracle 10g](https://github.com/lonng/db-papers/blob/main/papers/diagnosis-and-tuning/automatic-sql-tuning-in-oracle-10g.pdf) (2004) - Dageville B, et al.
- [Automatic Performance Diagnosis and Tuning in Oracle](https://github.com/lonng/db-papers/blob/main/papers/diagnosis-and-tuning/automatic-performance-diagnosis-and-tuning-in-oracle.pdf) (2005) - Dias K, et al.
- [Adaptive Query Processing in the Looking Glass](https://github.com/lonng/db-papers/blob/main/papers/statistics/adaptive-query-processing-in-the-looking-glass.pdf) (2005) - Babu, Shivnath, and Pedro Bizarro
- [Optimizer plan change management: improved stability and performance in Oracle 11g](https://dl.acm.org/doi/pdf/10.14778/1454159.1454175) (2008) - Ziauddin, Mohamed, et al.

### 工业实现

| 系统 | 链接 |
|------|------|
| **Presto** | [History-Based Optimization](https://prestodb.io/docs/current/optimizer/history-based-optimization.html), [GitHub Issue](https://github.com/prestodb/presto/issues/20355) |
| **BigQuery** | [History-Based Optimizations](https://cloud.google.com/bigquery/docs/history-based-optimizations) |
| **PolarDB** | [MySQL.taobao.org](http://mysql.taobao.org/monthly/2023/12/03/) |

---

## Statistics

### Histogram

- [Accurate Estimation of the Number of Tuples Satisfying a Condition](https://dl.acm.org/doi/pdf/10.1145/971697.602294) (1984) - Piatetsky-Shapiro, Gregory, and Charles Connell
- [Optimal Histograms for Limiting Worst-Case Error Propagation in the Size of Join Results](https://dl.acm.org/doi/pdf/10.1145/169725.169708) (1993) - Ioannidis, Yannis E., and Stavros Christodoulakis
- [Universality of Serial Histograms](https://github.com/lonng/db-papers/blob/main/papers/statistics/universality-of-serial-histograms.pdf) (1993) - Ioannidis, Yannis E.
- [Balancing Histogram Optimality and Practicality for Query Result Size Estimation](https://dl.acm.org/doi/pdf/10.1145/568271.223841) (1995) - Ioannidis, Yannis E., and Viswanath Poosala
- [Improved Histograms for Selectivity Estimation of Range Predicates](https://dl.acm.org/doi/pdf/10.1145/235968.233342) (1996) - Poosala, Viswanath, et al.
- [The History of Histograms](https://github.com/lonng/db-papers/blob/main/papers/statistics/the-history-of-histograms.pdf) (2003) - Ioannidis, Yannis
- [Automated Statistics Collection in DB2 UDB](https://github.com/lonng/db-papers/blob/main/papers/statistics/automated-statistics-collection-in-db2-udb.pdf) (2004) - Aboulnaga, Ashraf, et al.
- [Adaptive Query Processing in the Looking Glass](https://github.com/lonng/db-papers/blob/main/papers/statistics/adaptive-query-processing-in-the-looking-glass.pdf) (2005) - Babu, Shivnath, and Pedro Bizarro
- [Optimizer plan change management: improved stability and performance in Oracle 11g](https://dl.acm.org/doi/pdf/10.14778/1454159.1454175) (2008) - Ziauddin, Mohamed, et al.
- [Histograms Reloaded: The Merits of Bucket Diversity](https://dl.acm.org/doi/pdf/10.1145/1807167.1807239) (2010) - Kanne, Carl-Christian, and Guido Moerkotte
- [Synopses for Massive Data: Samples, Histograms, Wavelets, Sketches](https://github.com/lonng/db-papers/blob/main/papers/statistics/synopses-for-massive-data:-samples,-histograms,-wavelets,-sketches.pdf) (2011) - Cormode, Graham, et al.
- [Exploiting Ordered Dictionaries to Efficiently Construct Histograms with Q-Error Guarantees in SAP HANA](https://dl.acm.org/doi/pdf/10.1145/2588555.2595629) (2014) - Moerkotte, Guido, et al.
- [Adaptive Statistics in Oracle 12c](https://dl.acm.org/doi/pdf/10.14778/3137765.3137785) (2017) - Chakkappen, Sunil, et al.

### Probabilistic Counting

- [Towards Estimation Error Guarantees for Distinct Values](https://dl.acm.org/doi/pdf/10.1145/335168.335230) (2000) - Charikar, Moses, et al.
- [Distinct Sampling for Highly-Accurate Answers to Distinct Values Queries and Event Reports](https://github.com/lonng/db-papers/blob/main/papers/probabilistic-counting/distinct-sampling-for-highly-accurate-answers-to-distinct-values-queries-and-event-reports.pdf) (2001) - Gibbons, Phillip B.
- [An Improved Data Stream Summary: The Count-Min Sketch and its Applications](https://github.com/lonng/db-papers/blob/main/papers/probabilistic-counting/an-improved-data-stream-summary:-the-count-min-sketch-and-its-applications,-journal-of-algorithms.pdf) (2005) - Cormode, Graham, and Shan Muthukrishnan
- [New Estimation Algorithms for Streaming Data: Count-min Can Do More](https://www.academia.edu/download/31052190/cmm.pdf) (2007) - Deng, Fan, and Davood Rafiei
- [Preventing Bad Plans by Bounding the Impact of Cardinality Estimation Errors](https://dl.acm.org/doi/pdf/10.14778/1687627.1687738) (2009) - Moerkotte, Guido, Thomas Neumann, and Gabriele Steidl
- [Pessimistic Cardinality Estimation: Tighter Upper Bounds for Intermediate Join Cardinalities](https://dl.acm.org/doi/pdf/10.1145/3299869.3319894) (2019) - Cai, Walter, Magdalena Balazinska, and Dan Suciu
- [Deep Unsupervised Cardinality Estimation](https://github.com/lonng/db-papers/blob/main/papers/probabilistic-counting/deep-unsupervised-cardinality-estimation.pdf) (2019) - Yang, Zongheng, et al.
- [NeuroCard: One Cardinality Estimator for All Tables](https://github.com/lonng/db-papers/blob/main/papers/probabilistic-counting/neurocard:-one-cardinality-estimator-for-all-tables.pdf) (2020) - Yang, Zongheng, et al.

---

## Materialized View

### Incremental Processing

- [DBSP: Automatic Incremental View Maintenance for Rich Query Languages](https://arxiv.org/abs/2203.16684) (VLDB 2023 Best Paper) - By introducing Integration (I) and Differentiation (D) operators, DBSP allows the composition of complex incremental SQL queries
- [Keep Your Distributed Data Warehouse Consistent at a Minimal Cost](https://dl.acm.org/doi/10.1145/3589770) (PACMMOD 2023) - Implemented at YouTube Data Warehouse, cut update requests by 25%
- [What's the Difference? Incremental Processing with Change Queries in Snowflake](https://dl.acm.org/doi/10.1145/3589776) (ACM Management of Data 2023) - Snowflake introduces CHANGE queries and STREAM table objects
- [dbt: Incremental Models](https://docs.getdbt.com/docs/build/incremental-models) - dbt supports simple incremental processing with conditional switch of SQL queries

### Incremental Processing with Materialized Views

- [DBToaster: Higher-order Delta Processing for Dynamic, Frequently Fresh Views](http://vldb.org/pvldb/vol5/p968_yanifahmad_vldb2012.pdf) (VLDB 2012) - Efficient way for finding the delta of delta queries; [Demo Code](https://github.com/dbtoaster)
- [How to Win a Hot Dog Eating Contest: Distributed Incremental View Maintenance with Batch Updates](https://www.cs.ox.ac.uk/files/9133/sigmod2016-dbtoaster-batching_divm.pdf) (SIGMOD 2016) - Extending DBToaster for batch and distributed setting
- [Generalized Scale Independence Through Incremental Precomputation](http://dl.acm.org/citation.cfm?id=2465333) (SIGMOD 2013) - An approach for guaranteeing the response time of queries by preparing materialized views
- [Comet: batched stream processing for data intensive distributed computing](https://www.microsoft.com/en-us/research/publication/comet-batched-stream-processing-for-data-intensive-distributed-computing/) (SoCC 2010)
- [Continuous queries over append-only databases](http://www.cs.brandeis.edu/~cs227b/papers/pubsub/TGNO92-Continuous.pdf) (SIGMOD 1992)
- [Selecting Subexpressions to Materialize at Datacenter Scale](http://www.vldb.org/pvldb/vol11/p800-jindal.pdf) (PVLDB 2018) - Microsoft SCOPE - Automatically finding common sub-expressions
- [Tempura: A General Cost-Based Optimizer Framework for Incremental Data Processing](https://arxiv.org/abs/2009.13631) (VLDB 2020) - A cost-based optimizer for choosing the right incremental processing methods
- [Napa: Powering Scalable Data Warehousing with Robust Query Performance at Google](http://vldb.org/pvldb/vol14/p2986-sankaranarayanan.pdf) (VLDB 2021) - Control the timing of eager materialization based on user's requirements
- [Amazon Redshift: Automatic Query Rewriting with Materialized Views](https://docs.aws.amazon.com/redshift/latest/dg/materialized-view-auto-rewrite.html)
- [BigQuery: Smart Tuning](https://cloud.google.com/bigquery/docs/materialized-views-use) - BigQuery automatically rewrites queries to use materialized views

---

## Hash Join

- [In-Memory Hash Join Algorithms](https://15721.courses.cs.cmu.edu/spring2018/papers/20-sortmergejoins/kim-vldb2009.pdf) (VLDB 2009)
- [A Vectorized Hash Join](http://groups.csail.mit.edu/cag/6.893-f2000/vectorhashjoin.pdf)
- [Radix Sort and Hash-Join for Vector Computers](http://groups.csail.mit.edu/cag/6.893-f2000/readslides/Vector_Sort_Hash.pdf)
- [Hash Join Implement In OceanBase](https://zhuanlan.zhihu.com/p/35040231)
- [X100: MonetDB/X100](http://cidrdb.org/cidr2005/papers/P19.pdf)
- [Cuckoo Hash](https://web.stanford.edu/class/archive/cs/cs166/cs166.1146/lectures/13/Small13.pdf)
- [Hash Join Prefetch](http://www.cs.cmu.edu/~chensm/papers/hashjoin_icde04.pdf)
- [CockroachDB Vectorized Hash Joiner](https://www.cockroachlabs.com/blog/vectorized-hash-joiner/)
- [Architecture Conscious Hashing](https://www.cs.cmu.edu/~damon2006/pdf/zukowski06archconscioushashing.pdf)

---

## Spill

- [Velox Spilling](https://facebookincubator.github.io/velox/develop/spilling.html)
- [CockroachDB Disk Spilling Vectorized](https://www.cockroachlabs.com/blog/disk-spilling-vectorized/)

---

## Benchmark

- [TPC-H Analyzed: Hidden Messages and Lessons Learned from an Influential Benchmark](https://www.researchgate.net/profile/Peter-Boncz/publication/291257517_TPC-H_Analyzed_Hidden_Messages_and_Lessons_Learned_from_an_Influential_Benchmark/links/5852dbf708ae95fd8e1d749b/TPC-H-Analyzed-Hidden-Messages-and-Lessons-Learned-from-an-Influential-Benchmark.pdff) (2013) - Boncz, Peter, Thomas Neumann, and Orri Erling
- [Quantifying TPCH Choke Points and Their Optimizations](https://dl.acm.org/doi/pdf/10.14778/3389133.3389138) (2020) - Dreseler, Markus, et al.

---

## Miscellaneous

### Workload

- [TPC-H Analyzed: Hidden Messages and Lessons Learned from an Influential Benchmark](https://www.researchgate.net/profile/Peter-Boncz/publication/291257517_TPC-H_Analyzed_Hidden_Messages_and_Lessons_Learned_from_an_Influential_Benchmark/links/5852dbf708ae95fd8e1d749b/TPC-H-Analyzed-Hidden-Messages-and-Lessons-Learned-from-an-Influential-Benchmark.pdff) (2013) - Boncz, Peter, et al.
- [Quantifying TPCH Choke Points and Their Optimizations](https://dl.acm.org/doi/pdf/10.14778/3389133.3389138) (2020) - Dreseler, Markus, et al.

### Network

- [The End of Slow Networks: It's Time for a Redesign](https://github.com/lonng/db-papers/blob/main/papers/network/the-end-of-slow-networks:-it's-time-for-a-redesign.pdf) (2015) - Binnig, Carsten, et al.
- [Accelerating Relational Databases by Leveraging Remote Memory and RDMA](https://dl.acm.org/doi/pdf/10.1145/2882903.2882949) (2016) - Li, Feng, et al.
- [Don't Hold My Data Hostage: A Case for Client Protocol Redesign](https://dl.acm.org/doi/pdf/10.14778/3115404.3115408) (2017) - Raasveldt, Mark, and Hannes Mühleisen

### Quality

- [Testing the Accuracy of Query Optimizers](https://dl.acm.org/doi/pdf/10.1145/2304510.2304525) (2012) - Gu, Zhongxian, et al.

### Diagnosis and Tuning

- [Automatic SQL Tuning in Oracle 10g](https://github.com/lonng/db-papers/blob/main/papers/diagnosis-and-tuning/automatic-sql-tuning-in-oracle-10g.pdf) (2004) - Dageville B, et al.
- [Automatic Performance Diagnosis and Tuning in Oracle](https://github.com/lonng/db-papers/blob/main/papers/diagnosis-and-tuning/automatic-performance-diagnosis-and-tuning-in-oracle.pdf) (2005) - Dias K, et al.

---

## 基础必读（Rxin 精选）

> 来自 [Rxin/db-readings](https://github.com/rxin/db-readings) 的经典必读论文列表

### Basics and Algorithms

- [The Five-Minute Rule Ten Years Later, and Other Computer Storage Rules of Thumb](https://github.com/rxin/db-readings/blob/master/papers/5_min_rule_sigmod.pdf) (1997) - Jim Gray。关于数据页是否应缓存在内存中的定量公式。原论文发表于10年前，本文是对其的回顾。
- [AlphaSort: A Cache-Sensitive Parallel External Sort](https://github.com/rxin/db-readings/blob/master/papers/alphasortsigmod.pdf) (1995) - 排序是数据库中最重要的算法之一。本文介绍了快速排序实现使用的所有技巧。
- [Patience is a Virtue: Revisiting Merge and Sort on Modern Processors](https://github.com/rxin/db-readings/blob/master/papers/patsort-sigmod14.pdf) (2014) - 重新审视排序算法，也是实践排序算法及其权衡的很好综述。

### Essentials of Relational Databases

- [Architecture of a Database System](https://github.com/rxin/db-readings/blob/master/papers/fntdb07-architecture.pdf) (2007) - Joe Hellerstein 的 RDBMS 概述，介绍关系数据库系统的所有基本组件。
- [A Relational Model of Data for Large Shared Data Banks](https://github.com/rxin/db-readings/blob/master/papers/codd.pdf) (1970) - Codd 关于数据独立性的开创性论文，是关系数据库的基本概念。
- [ARIES: A Transaction Recovery Method Supporting Fine-Granularity Locking and Partial Rollbacks Using Write-Ahead Logging](https://github.com/rxin/db-readings/blob/master/papers/aries.pdf) (1992) - 第一个真正工作的算法：支持事务的并发执行，即使在失败情况下也不会丢失数据。这篇论文很难读，因为将许多底层细节混合在高层算法的解释中。
- [Efficient Locking for Concurrent Operations on B-Trees](https://github.com/rxin/db-readings/blob/master/papers/btree.pdf) (1981) 和 [R*-tree: An Efficient and Robust Access Method for Points and Rectangles](https://github.com/rxin/db-readings/blob/master/papers/rstar-tree.pdf) (1990) - B-Tree 是数据库的核心数据结构。R-tree 是 B-tree 的扩展，支持多维数据的查找。
- [Improved Query Performance with Variant Indexes](https://github.com/rxin/db-readings/blob/master/papers/variant-index.pdf) (1997) - 分析数据库和 OLTP 数据库需要不同的权衡。这篇论文讨论了更适合分析数据库的索引数据结构。
- [On Optimistic Methods for Concurrency Control](https://github.com/rxin/db-readings/blob/master/papers/occ.pdf) (1981) - 解释乐观并发控制作为锁的替代方案。
- [Access Path Selection in a Relational Database Management System](https://github.com/rxin/db-readings/blob/master/papers/systemr-optimizer.pdf) (1979) - 查询优化的基础。SQL 是声明式的，数据库系统检查多个计划并选择最优的（尽力而为）。这篇论文解释了成本模型和动态规划算法。
- [Eddies: Continuously Adaptive Query Processing](https://github.com/rxin/db-readings/blob/master/papers/eddies.pdf) (2000) - 传统查询优化是静态的。这篇论文提出了一套动态优化查询执行的技术。

### Classic System Design

- [A History and Evaluation of System R](https://github.com/rxin/db-readings/blob/master/papers/systemr.pdf) (1981) - IBM 的 System R 证明了关系数据库的可行性。令人印象深刻的是，2012年的关系数据库内部看起来很像1981年的 System R。
- [The Google File System](https://github.com/rxin/db-readings/blob/master/papers/gfs.pdf) (2003) 和 [Bigtable: A Distributed Storage System for Structured Data](https://github.com/rxin/db-readings/blob/master/papers/bigtable.pdf) (2006) - Google 数据基础设施的两个核心组件。GFS 是为大数据顺序读取优化的追加式分布式文件系统。BigTable 是在 GFS 上构建的高性能分布式数据存储。
- [Chord: A Scalable Peer-to-peer Lookup Service for Internet Applications](https://github.com/rxin/db-readings/blob/master/papers/chord.pdf) (2001) 和 [Dynamo: Amazon's Highly Available Key-value Store](https://github.com/rxin/db-readings/blob/master/papers/dynamo.pdf) (2007) - Chord 是在分布式哈希表热潮时期诞生的。Dynamo 解释了如何使用 Chord 构建分布式键值存储。

### Columnar Databases

> 列式存储和列式查询引擎对分析工作负载（如 OLAP）至关重要。

- [C-Store: A Column-oriented DBMS](https://github.com/rxin/db-readings/blob/master/papers/cstore.pdf) (2005) 和 [The Vertica Analytic Database: C-Store 7 Years Later](https://github.com/rxin/db-readings/blob/master/papers/vertica-7-years.pdf) (2012) - C-Store 是有影响力的学术系统，Vertica 是 C-Store 的商业化身。
- [Column-Stores vs. Row-Stores: How Different Are They Really?](https://github.com/rxin/db-readings/blob/master/papers/column-vs-row.pdf) (2012) - 讨论列式存储和查询引擎的重要性。
- [Dremel: Interactive Analysis of Web-Scale Datasets](https://github.com/rxin/db-readings/blob/master/papers/dremel.pdf) (2010) - Google 的交互式分析数据库，用于即席查询，在数千个节点上运行，可在数秒内处理 TB 级数据。

### Data-Parallel Computation

- [MapReduce: Simplified Data Processing on Large Clusters](https://github.com/rxin/db-readings/blob/master/papers/mapreduce.pdf) (2004) - MapReduce 既是编程模型（借用函数式编程），也是 Google 的分布式数据密集型计算系统。
- [Resilient Distributed Datasets: A Fault-Tolerant Abstraction for In-Memory Cluster Computing](https://github.com/rxin/db-readings/blob/master/papers/spark.pdf) (2012) - Spark 的研究论文，引入了 RDD（弹性分布式数据集）抽象。
- [Shark: SQL and Rich Analytics at Scale](https://github.com/rxin/db-readings/blob/master/papers/shark.pdf) (2013) - 构建在 Spark 之上的 SQL 引擎。

### Consensus and Consistency

- [Paxos Made Simple](https://github.com/rxin/db-readings/blob/master/papers/paxos.pdf) (2001) - Paxos 是容错分布式共识协议。想法很简单，但 notoriously 难理解。
- [The Raft Consensus Algorithm](https://github.com/rxin/db-readings/blob/master/papers/raft.pdf) (2014) - Raft 是作为 Paxos 替代方案设计的共识算法，更易于理解。
- [CAP Twelve Years Later: How the "Rules" Have Changed](https://github.com/rxin/db-readings/blob/master/papers/cap.pdf) (2012) - Eric Brewer 对 CAP 定理的回顾。

### Trends (Cloud Computing, Warehouse-scale Computing, New Hardware)

- [A View of Cloud Computing](https://github.com/rxin/db-readings/blob/master/papers/cloud-computing.pdf) (2010) - 关于云计算的论文。讨论云计算（指资源的弹性）的经济性和技术障碍。
- [The Datacenter as a Computer: An Introduction to the Design of Warehouse-Scale Machines](https://github.com/rxin/db-readings/blob/master/papers/data-center-computer.pdf) - Google 的 Luiz André Barroso 和 Urs Hölzle 解释数据中心硬件和软件的基础知识。关键思想包括长尾延迟和资源的解耦。

### 其他经典

- [Reflections on Trusting Trust](https://github.com/rxin/db-readings/blob/master/papers/trusting-trust.pdf) (1984) - Ken Thompson 的图灵奖演讲，描述黑盒后门问题。
- [What Goes Around Comes Around](https://github.com/rxin/db-readings/blob/master/papers/goes-around.pdf) - Michael Stonebraker 和 Joseph M. Hellerstein 总结35年的数据模型提案，分为9个不同时代。
- [Readings in Database Systems (The Red Book)](http://www.redbook.io/) - Peter Bailis, Joseph Hellerstein 和 Michael Stonebraker 编写的关于数据管理系统历史和最先进研究的紧凑而有信息量的书。

### 外部阅读列表

- [Berkeley PhD prelim exam reading list](https://docs.google.com/spreadsheets/d/1QOf6UmSo87hELSZiDZZ3rSZngsh4CBcef06x-JkvCnI/edit?gid=652684559) 和 [CS286 grad database class reading list](http://www.cs286.net/home/reading-list)
- [Brown CSCI 2270 Advanced Topics in Database Management](https://www.francosolleza.com/CS227/)
- [Stanford PhD qualifying exam](http://infolab.stanford.edu/db_pages/infoqual.html)
- [MIT Database Systems 6.830](http://ocw.mit.edu/courses/electrical-engineering-and-computer-science/6-830-database-systems-fall-2010/readings/)
- [Wisconsin Database Qualifying Exam Reading List (2014)](https://cs.wiscweb.wisc.edu/wp-content/uploads/sites/871/2018/12/Database-systems-qual_Summer-2014.pdf)
- [CMU 15-721 Database Systems Reading List (Spring 2016)](http://15721.courses.cs.cmu.edu/spring2016/schedule.html)


# Readings in Stream Processing

- https://github.com/xerial/streamdb-readings/blob/master/README.md
- [streamdb-readings](https://prod-files-secure.s3.us-west-2.amazonaws.com/1cd5f27a-1dba-488c-85f1-dbeaf7ae25bf/d4af7ee5-e51c-499b-8d8d-d9d174fc3c18/streamdb-readings)


A list of essential articles to understand stream processing, including continuously querying and maintaining time-series data.

## Books

- [Designing Data Intensive Applications. The Big Ideas Behind Reliable, Scalable, and Maintainable Systems. Martine Kleppmann](https://www.oreilly.com/library/view/designing-data-intensive-applications/9781491903063/) This is a book we've been waiting for 10 years. A definitive guide for entering the field of distirbuted systems and stream data processing. This book covers fundamental concepts, techniques, and challenges in keep processing large volumes of data continously.
    - Japanese translation of the book 「[データ指向アプリケーションデザイン](https://amzn.to/3I40Pkk)」 is also available
- [Streaming Systems: The What, Where, When, and How of Large-Scale Data Processing](https://www.amazon.co.jp/Streaming-Systems-Large-Scale-Processing-English-ebook/dp/B07FMDY5CC/) A book by the authors of Streaming 101, Spark Streaming. This book introduces how we can unify batch processing and stream processing within a single system and covers the basic ideas of Stream SQL.

## Programming Models for Stream Processing

- [One SQL to Rule Them All. Edmon Begoli, Tyler Akidau, Fabian Hueske, Julian Hyde, Kathryn Knight, Kenneth Knowles SIGMOD 2019](https://arxiv.org/abs/1905.12133) A proposal to extend the current SQL semantics to support both batch and stream processing by adding time-varying relations (TVR), event time, and materialization control.
- [DryadLINQ: A System for General-Purpose Distributed Data-Parallel Computing Using a High-Level Language. Y. Yu, et al. OSDI08](https://www.usenix.org/legacy/event/osdi08/tech/full_papers/yu_y/yu_y.pdf) The origin of declarative data processing operators (e.g., map, filter, groupBy, etc.) in modern programming languages.
- [OpenMessaging:Common Use Cases](https://github.com/openmessaging/specification/blob/master/usecase.md) Illustrations of typical stream processing patterns from OpenMessaging.
- [The world beyond batch: Streaming 101](https://www.oreilly.com/ideas/the-world-beyond-batch-streaming-101). An article written by the author of Google Dataflow. Streaming is actually a superset of batch processing for unbounded data. This article explains what is unbounded data and how to manage late coming data. You can also learn what streaming systems can do and can't do, and the differences of event times and processing times, and varieties of time-window based processing.
- [Structured Streaming: A Declarative API for Real-Time Applications in Apache Spark. SIGMOD2018](https://cs.stanford.edu/~matei/papers/2018/sigmod_structured_streaming.pdf). A good summary of challenges around continous stream processing and unifying APIs for batch and stream processing.
    - [Discretized Streams: Fault-Tolerant Streaming Computation at Scale. NSDI 2013](https://people.csail.mit.edu/matei/papers/2013/sosp_spark_streaming.pdf) An approacy for applying micro-batch style stream processing in Spark. This model has been redesigned in Spark 2.0 as [Structured Streaming](https://spark-summit.org/2017/events/easy-scalable-fault-tolerant-stream-processing-with-structured-streaming-in-apache-spark/).
    - [Continuous Applications: Evolving Streaming in Apache Spark 2.0](https://databricks.com/blog/2016/07/28/continuous-applications-evolving-streaming-in-apache-spark-2-0.html)
- [Drizzle: Fast and Adaptable Stream Processing at Scale. SOSP 2017](http://shivaram.org/publications/drizzle-sosp17.pdf) An approach for reducing the overhead of the coordination between stream processing tasks.
- [Dataflow/Beam & Spark: A Programming Model Comparison](https://cloud.google.com/dataflow/blog/dataflow-beam-and-spark-comparison)
- [ReactiveX](http://reactivex.io/). Stream processing patterns for functional programming.
- [Akka Stream](https://doc.akka.io/docs/akka/2.5/stream/index.html) Stream processing DSL for Akka
- [A Practical Guide to Selecting A Stream Processing Technology](http://www.slideshare.net/ConfluentInc/a-practical-guide-to-selecting-a-stream-processing-technology). A good explanation of stream processing
- [Trill: A High-Performance Incremental Query Processor for Diverse Analytics. B. Chandramouli, et al. VLDB 2014](https://www.microsoft.com/en-us/research/publication/trill-a-high-performance-incremental-query-processor-for-diverse-analytics/)

## Table Catalog for Stream Processing

- [Delta Lake: High-Performance ACID Table Storage over Cloud Object Stores](https://www.databricks.com/jp/wp-content/uploads/2020/08/p975-armbrust.pdf) Utilizing scalable object strage on the cloud (e.g., S3), [Delta Lake](https://github.com/delta-io/delta) provides a single table format with versioning (time-travel) and transaction support.
- [Iceberg](https://iceberg.apache.org/)
- [Big Metadata: When Metadata is Big Data. (VLDB 2021)](http://vldb.org/pvldb/vol14/p3083-edara.pdf) Columnar table catalog with partition statistics used in Google's BigQuery.
- [Apache Hudi](https://hudi.apache.org/) Apach Hudi provides [a file layout](https://hudi.apache.org/tech-specs) for placing both streaming and batch data processing with a transaction support. You can merge fragmented partition data or use it as is for faster real-time data processing. Previously, it was called [Uber Hoodie](https://github.com/uber/hoodie)

## Watermark Management for Stream Processing

- [The Dataflow Model: A Practical Approach to Balancing Correctness, Latency, and Cost in Massive-Scale, Unbounded, Out-of-Order Data Processing. Akidau et al., (Google) VLDB 2015](http://www.vldb.org/pvldb/vol8/p1792-Akidau.pdf) The original paper of [Google Cloud Dataflow](https://cloud.google.com/dataflow/), which describes how we can cope with the delay of data arrival (late-coming data) and periodical data processing in a unified API for batch and stream processing. You can also find a summary of this paper at [the morning paper](https://blog.acolyer.org/2015/08/18/the-dataflow-model-a-practical-approach-to-balancing-correctness-latency-and-cost-in-massive-scale-unbounded-out-of-order-data-processing/)
- [Watermarks in Stream Processing Systems: Semantics and Comparative Analysis of Apache Flink and Google Cloud Dataflow. VLDB 2021](http://vldb.org/pvldb/vol14/p3135-begoli.pdf). Describes basic definitions of watermarks and shows challenges and trade-offs in managing watermarks.
- [Watermarking in stream processing | Course in Spark Structured Streaming 3.0 | Lesson 7](https://www.youtube.com/watch?v=XjlKGvUt2dY) A good tutorial explaining the notions of stream processing and watermark management.

## Workload Optimization

- [Towards a Learning Optimizer for Shared Clouds (VLDB 2019)](http://www.vldb.org/pvldb/vol12/p210-wu.pdf). Estimate cardinality models from the previous job executions in order to optimize the overall workloads. This work uses the multi-layer perceptron (MLP) neural network for learning models from query exeuction features (e.g., job name, input cardinality, average row length, input dataset names, etc.)
- [CrocodileDB: Efficient Database Execution through Intelligent Deferment (CIDR 2020)](http://cidrdb.org/cidr2020/papers/p14-shang-cidr20.pdf) This paper introduces Intermittent Query Processing (IQP) approach for utilizing the knowledge about new data, query semantics, and users' expectation together to reduce the overall processing cost. It uses Deep Q-Materialization (DQM) to make a tradeoff under a certain resource constraint (e.g., memory, CPUs, storage) to decide how much data will be cached, pre-computed, pre-loaded, etc.
- [Peregrine: Workload Optimization for Cloud Query Engines (SOCC 2019)](https://dl.acm.org/doi/10.1145/3357223.3362726) Analyzing the workload of historical queries and optimize recurrring queries, similar queries, and coordinating queries by extracing common subexpressions that can be materialized. To support various query engines including Spark, Microsoft has creaetd a common intermediate representation (IR) of workloads.
- [Computation reuse via fusion in Amazon Athena (ICDE 2022)](https://www.amazon.science/publications/computation-reuse-via-fusion-in-amazon-athena) Query-rewrite optimization to merge multiple table scans between subqueries into a single table scan, which is available in Amazon Athena. Support for Trino is also coming [trino#15690](https://github.com/trinodb/trino/issues/15690)

## Iterative Data Processing

- [Olston, C. et al. 2011. Nova: continuous Pig/Hadoop workflows. (Jun. 2011)](http://infolab.stanford.edu/~olston/publications/sigmod11.pdf)
- [Naiad: A Timely Dataflow system (SOSP13 best paper)](https://www.microsoft.com/en-us/research/wp-content/uploads/2013/11/naiad_sosp2013.pdf) Differential data processing developed in Microsoft. [Niad Project Page](https://www.microsoft.com/en-us/research/project/naiad/) The proposed approach is now implemented in Rust [Timely Dataflow](https://github.com/timelydataflow/timely-dataflow)
- [Apache Flink: Spinning Fast Iterative Data Flows. PVLDB 2012](http://stratosphere.eu/assets/papers/spinningFastIterativeDataFlows_12.pdf)

## Incremental Processing

- [DBSP: Automatic Incremental View Maintenance for Rich Query Languages (VLDB 2023 Best Paper)](https://arxiv.org/abs/2203.16684) By introducing Integration (I) and Differentiation (D) operators, DBSP allows the composition of complex incremental SQL queries, including joins, aggregations, recursive loops, etc.
- [Keep Your Distributed Data Warehouse Consistent at a Minimal Cost (PACMMOD 2023)](https://dl.acm.org/doi/10.1145/3589770) Proposes a method for maintaining consistency in large data warehouses while minimizing updates. It solves a dynamic programming problem to balance computational cost and data freshness. Implemented at the YouTube Data Warehouse, the method has cut update requests by 25% by removing non-trivial duplicates, significantly saving computing resources.
- [What’s the Difference? Incremental Processing with Change Queries in Snowflake (ACM Management of Data 2023)](https://dl.acm.org/doi/10.1145/3589776) Snowflake introduces CHANGE queries and STREAM table objects to subscribe changes in the table.
- [dbt: Incremental Models](https://docs.getdbt.com/docs/build/incremental-models) dbt, a tool for compiling a sequence of queries from SQL templates, supports a simple incremental processing with conditional switch of SQL queries.

### Incremental Processing with Materialized Views

- [DBToaster: Higher-order Delta Processing for Dynamic, Frequently Fresh Views (VLDB 2012)](http://vldb.org/pvldb/vol5/p968_yanifahmad_vldb2012.pdf). An efficient way for finding the delta of delta queries (higher-order delta) for computing materialized views as we receive a new update record. A demo source code written in OCaml and Scala can be found in https://github.com/dbtoaster
- [How to Win a Hot Dog Eating Contest: Distributed Incremental View Maintenance with Batch Updates M. Nicolik et al. (SIGMOD 2016)](https://www.cs.ox.ac.uk/files/9133/sigmod2016-dbtoaster-batching_divm.pdf) Extending DBToaster for batch and distributed incremental view maintenance setting.
- [Generalized Scale Independence Through Incremental Precomputation. M. Armbrust, et al. SIGMOD 2013](http://dl.acm.org/citation.cfm?id=2465333) An approach for guaranteeing the response time of queries by classifying query types and preparing materialized views if necessary.
- [Comet: batched stream processing for data intensive distributed computing (SoCC 10)](https://www.microsoft.com/en-us/research/publication/comet-batched-stream-processing-for-data-intensive-distributed-computing/) A basic style of incremental processing
- [Continuous queries over append-only databases. SIGMOD 1992](http://www.cs.brandeis.edu/~cs227b/papers/pubsub/TGNO92-Continuous.pdf)
- [Selecting Subexpressions to Materialize at Datacenter Scale. PVLDB 2018](http://www.vldb.org/pvldb/vol11/p800-jindal.pdf) Microsoftr SCOPE - Automatically finding common sub-expressions among queries and materializing their results for reducing the overhead of recurrent queries.
- [Tempura: A General Cost-Based Optimizer Framework for Incremental Data Processing (VLDB 2020)](https://arxiv.org/abs/2009.13631) A cost-based optimizer for choosing the right incremental processing methods. A demo [source code](https://github.com/alibaba/cost-based-incremental-optimizer) extending Apache Calcite is available.
- [Napa: Powering Scalable Data Warehousing with Robust Query Performance at Google. VLDB 2021](http://vldb.org/pvldb/vol14/p2986-sankaranarayanan.pdf) Control the timing of eager materialization of queries based on the user's requirements (Favor freshness or performance)
- [Amazon Redshift: Automatic Query Rewriting with Materialized Views](https://docs.aws.amazon.com/redshift/latest/dg/materialized-view-auto-rewrite.html)
- [Big Query: Smart tuning](https://cloud.google.com/bigquery/docs/materialized-views-use) BigQuery automatically rewrites queries to use materialized views whenever possible. Automatic rewriting improves query performance and cost, and does not change query results.
- [OpenIVM: a SQL-to-SQL Compiler for Incremental Computations (SIGMOD-Companion 2024)](https://dl.acm.org/doi/10.1145/3626246.3654743) Implements incremental view maintenance (IVM) at the SQL level, implemented as a DuckDB extension ([source code](https://github.com/ila/duckdb/tree/rdda/extension/openivm))
- [Incremental Refresh for Materialized Views (Databricks)](https://docs.databricks.com/en/optimizations/incremental-refresh.html)

## Stream Log Collection Systems

- [Fluentd](https://www.fluentd.org/) A unified logging layer from various data sources.
- [Kafka](https://kafka.apache.org/) is often used for providing *replayable* streaming data sources.
- [Apache Pulsar](https://pulsar.incubator.apache.org/) A distributed pub-sub messaging system originally created at Yahoo!
- [OpenMessaging](https://github.com/openmessaging) Cloud-oriented, simple, flexible, vendor-neutral and language-independent standards for messaging
- [Uber Hoodie](https://github.com/uber/hoodie) Hybrid storage: Avro for streaming import, Parquet for analysis. This project has been moved to [Apache Hudi](https://hudi.apache.org/)
- [MQTT](http://mqtt.org/) A machine-to-machine (M2M)/"Internet of Things" connectivity protocol.
- [Robust, Scalable, Real-Time Event Time Series Aggregation at Twitter. SIGMOD2018](https://cs.uwaterloo.ca/~jimmylin/publications/Yang_etal_SIGMOD2018.pdf)
    - [Twitter Heron: Stream Processing at Scale. SIGMOD 2015](http://dl.acm.org/citation.cfm?id=2742788)
    - [The Unified Logging Infrastructure for Data Analytics at Twitter. VLDB 2012](http://vldb.org/pvldb/vol5/p1771_georgelee_vldb2012.pdf)
- [Questioning the Lambda Architecture](https://www.oreilly.com/ideas/questioning-the-lambda-architecture) A commonly used architecture for managing recent data and archived data. However combining two types of systems for batch and streaming is still painful because analysts need to understand both systems (e.g, Hadoop + Storm, Spark + Spark Streaming, Kafka + other data store)

## Real-Time Stream Processing

Real-time stream processing usually means ultra-low latency applications to satisfy SLAs for returning results in a few seconds.

- [The Stratosphere Platform for Big Data Analytics](http://stratosphere.eu/assets/papers/2014-VLDBJ_Stratosphere_Overview.pdf). Stratospher is a former name of [Apache Flink](https://flink.apache.org/).
- [The 8 Requirements of Real-Time Stream Processing. M. Stonebraker, et al. SIGMOD Record 2005](http://cs.brown.edu/~ugur/8rulesSigRec.pdf). A summary is also available in [the morning paper](https://blog.acolyer.org/2014/12/03/the-8-requirements-of-real-time-stream-processing/)
- [MacroBase: Prioritizing Attention in Fast Data. P. Bailis, et al. SIGMOD 2017](http://www.bailis.org/papers/macrobase-sigmod2017.pdf). A data analytics engine that prioritizes end-user attention in high-volume fast data streams.
    - A prototype implementation on [GitHub](https://github.com/stanford-futuredata/macrobase)

## Stream SQL

- [Foundations of Streaming SQL](http://s.apache.org/streaming-sql-strata-nyc) by Tyler Akidau. Good illustrations for understanding how regular table-based SQL and streaming SQL are different.
- [Microsoft Azure Stream Analytics](https://azure.microsoft.com/en-us/services/stream-analytics/)
- [Norikra](http://norikra.github.io/) Schema-less Stream Processing with SQL
- [Esper](http://www.espertech.com/esper/)
- [KSQL](https://github.com/confluentinc/ksql) A Streaming SQL Engine for Apache Kafka

## GitHub Projects

- [OpenMessaging](https://github.com/openmessaging)
- [Apache Beam](https://github.com/apache/beam) A unified model for defining both batch and streaming data-parallel processing pipelines.
    - It was [Google Dataflow Open source API for Java](https://github.com/GoogleCloudPlatform/DataflowJavaSDK)
- [spotify/scio: A Scala API for Google Cloud Dataflow](https://github.com/spotify/scio)
- [twitter/heron](https://github.com/twitter/heron)
- [Microsoft Naiad](https://github.com/MicrosoftResearch/Naiad)
- [Norikra](http://norikra.github.io/)
- [KSQL](https://github.com/confluentinc/ksql)
- [Apache Apex](https://apex.apache.org/) Unified stream and batch processing engine.

## Commercial Services

### Stream Ingestion

- [Azure Event Hubs](https://docs.microsoft.com/en-us/azure/event-hubs/event-hubs-what-is-event-hubs)
- [AWS Kinesis Streams](https://aws.amazon.com/kinesis/data-streams/)
- [Google Cloud Pub/Sub](https://cloud.google.com/pubsub/)

## External Lists

- List of projects related to stream-processing: https://github.com/manuzhang/awesome-streaming


大概用了9个月的时间，研读了优化器、执行器经典论文，论文主要来源于[梁辰：优化器论文列表](https://zhuanlan.zhihu.com/p/363997416)和[https://15721.courses.cs.cmu.edu](https://link.zhihu.com/?target=https%3A//15721.courses.cs.cmu.edu/spring2018/schedule.html%23warning)，辅助性来自一些国外明显的topic。研读期间，学习了[梁辰](https://www.zhihu.com/people/yaoling-lc)、[https://](https://link.zhihu.com/?target=https%3A//loopjump.com/)loopjump.com/对优化器、执行器的论文解读，收益很大。

## **简介**

大概用了9个月的时间，研读了优化器、执行器经典论文，论文主要来源于[梁辰：优化器论文列表](https://zhuanlan.zhihu.com/p/363997416)和[https://15721.courses.cs.cmu.edu](https://link.zhihu.com/?target=https%3A//15721.courses.cs.cmu.edu/spring2018/schedule.html%23warning)，辅助性来自一些国外明显的topic。研读期间，学习了

[梁辰](https://www.zhihu.com/people/yaoling-lc)、[https://](https://link.zhihu.com/?target=https%3A//loopjump.com/)loopjump.com/对优化器、执行器的论文解读，收益很大。

同时，在研读论文过程中，收集整理了论文的发表单位，以及论文的落地产品。个人理解，论文更多的是体现出一个idea以及这个idea的实现思路，如果想融汇贯通、运用自如，还是需要进一步研究分析相关的开源数据库源码、闭源数据库的功能特性。

由于个人能力有限，对论文的解读、按数据库进行整理的内容可能存在一定偏差或错误，期待能够和感兴趣的同学学习交流、共同进步。

## **Oracle**

[[SIGMOD 1979] Access Path Selection in a Relational Database Management System 论文阅读](https://zhuanlan.zhihu.com/p/590776990)

本文虽然发表于1979年，但在System R这个关系型数据库研究项目中，提出的制定SQL查询计划思想（自下而上+启发式+基于代价评估）至今仍被主流数据库所使用，例如Oracle、DB2、PostgreSQL等。

本文解决的主要问题是，用于在不了解数据库存储细节的情况下，执行了一个SQL查询，数据库优化器如何将SQL转换为最优的执行路径，即最高效的执行。

[[VLDB 06]Cost-based query transformation in Oracle 论文学习](https://zhuanlan.zhihu.com/p/595947967)

本篇论文虽是2006年发表，但其中的启发式规则、代价评估规则仍被主流数据库使用。在Heuristic Transformations转换规则中，Join Elimination、Filter Predicate Move Around、Group Pruning规则必然能获得更加的查询计划，但Subquery Unnesting则需要依赖表中具体的数据行数决定。代价评估规则中，给出了8中转换规则，并结合实例说明如何使用。论文也提出了一个代价评估框架，包括框架的组件、state搜索算法、transformation规则的执行方式以及框架的性能优化方法。

[[VLDB 2017]Adaptive Statistics In Oracle 12c --学习笔记](https://zhuanlan.zhihu.com/p/622026398)

论文介绍了查询执行过程中，中间结果统计信息错误导致的问题，并给出了Oracle 12c的解决方案。该解决方案包括使用表的数据和通过自动创建辅助统计信息来验证已生成的统计信息的准确性。

[[SIGMOD 2004]Parallel SQL Execution in Oracle 10g --学习笔记](https://zhuanlan.zhihu.com/p/621200670)

论文介绍了Oracle10g在并行执行方面的新架构和优化。自Oracle7开始，Oracle就基于share-disk架构实现了sql并行执行框架。在Oracle10g对并行执行引擎进行了重构，通过使用全局并行计划模型，使得并行执行引擎更高效、更易于动态扩展、更易于管理。论文不是很好理解，有些内容只是简单的说了下what，没有解释how，另外也涉及到了许多Oracle的相关名词术语。

[[VLDB 2015]Query Optimization in Oracle 12c Database In-Memory--学习笔记](https://zhuanlan.zhihu.com/p/620535500)

Oracle 12c内存数据库是业界第一个 dual-format（同时支持行存、列存）的数据库。在IO层，仍然使用行存格式。在内存中，同时支持行存和列存。数据存储方式的变化，使得数据查询处理算法也要进行扩展，这就要求查询优化器同步做扩展和适配，以便在支持列存的存储、查询计算的情况下，能够产生最佳的查询计划。论文给出了引入Database In-Memory后，查询优化器的扩展，具体包括统： statistics, cost model, query transformation, access path 和 join 优化, parallelism, 以及 cluster-awareness优化。

[[VLDB 2013]Adaptive and Big Data Scale Parallel Execution in Oracle--学习笔记](https://zhuanlan.zhihu.com/p/619664281)

论文介绍了Oracle数据库在执行join、groupby、窗口函数方面引入的新的并行+自适应方法。这套技术的创新点是采用了多阶段并发模型（multi-stage parallelization models），能够在运行时根据实时统计信息，将由于优化器统计信息不准导致的次优plan，动态调整为更优的plan，主要策略是：动态扩展并行执行进程的数量，以及调整数据的重分布策略，避免data skew。论文还讨论了Oracle原有的串行化执行的算子（如top-N）的并行化执行的方法，Oracle从这些自适应并行化技术中实现了巨大的性能提高。

[[VLDB 2009]Enhanced Subquery Optimizations in Oracle-Oracle去相关子查询论文学习](https://zhuanlan.zhihu.com/p/617059579)

paper从优化器、执行器两方面提出了子查询优化的方法：

- 优化器：子查询优化方法，包括子查询合并、基于窗口函数的子查询删除、在groupby查询中删除view，目标是删除冗余子查询块，并将子查询转换为更容易优化的形式。
- 执行器：提出了一种并行执行技术，具有普遍适用性和可扩展性，并可以同经历了某些transform的查询共同使用

另外，paper针对antijoin处理null值的问题，提出了一个anntijoin算子的变种Null-Aware Antijoin，用于优化包含可能为null的column的子查询。

[[SIGMOD 2015]Rethinking SIMD Vectorization for In-Memory Databases--学习笔记](https://zhuanlan.zhihu.com/p/640818697)

论文作者来自哥伦比亚大学和Oracle。论文基于先进的SIMD指令，例如gathers和scatters，提出了一种新的向量化执行引擎，并给出了selection scans、hash tables、partition的向量化实现代码，在这些基础上，实现了sort和join等算子。论文将提出的向量化实现技术与最先进的scalar和vectorized技术进行了对比，通过在Xeon Phi以及主流CPU的实测结果，证实论文提出的技术的高性能。

## DB2

[[SIGMOD '03] WinMagic Subquery Elimination Using Window Aggregation--DB2去相关子查询论文学习](https://zhuanlan.zhihu.com/p/618026383)

paper介绍了IBM DB2中利用窗口函数针对agg场景下去相关子查询的方法，该方法虽然有些约束限制，但如果满足的话，还是很高效的。目前，PolarDB MySQL、OceanBase都实现了这个算法。据了解，Oracle也实现了这个算法。

## **SQL Server**

[[IEEE Data engineering Bulletin 1995] The Cascades Framework for Query Optimization论文阅读](https://zhuanlan.zhihu.com/p/590793684)

本篇论文已经在SQL Server中落地，论文对Volcano Optimizer Generator论文的后续，作者提出了Cascades优化器框架，对Volcano Optimizer Generator在功能、易用性和健壮性方面的实质性改进，主要的不同点在于volcano是先等价替换出所有的逻辑计划，然后计算物理计划。而cascade是没有区分逻辑计划和物理计划，统一处理。

本论文也是抽象化的提出了一种Cascades优化器框架，给出了一些工程化实现的说明。与Volcano优化器相比，Cascades性能更加。因为Volcano采用了穷举方法，在父节点对子节点优化过程中，采用了广度优先算法，先等价替换出所有的逻辑计划，然后计算物理计划。Cascades是没有区分逻辑计划和物理计划，统一处理，采用了类似深度优先的算法。并且使用了指导规则，一些搜索工作是可以避免的，Cascades的搜索策略似乎更好。

[[ICDE 2010]Incorporating Partitioning and Parallel Plans into the SCOPE optimizer--学习笔记](https://zhuanlan.zhihu.com/p/642248107)

在大规模集群中进行大量数据的分析为查询优化带来了新的机会和挑战。这种场景下，使用数据分区是提升性能的关键。然而，数据重分布的代价非常高，因此，在查询计划中，如果可以在算子中最大程度的减少数据重分布操作，那么可以显著的提升性能。在这个存在分区的环境下。查询优化器需要能够利用数据分区的位置、排序、分组等元数据信息，制定更高效的查询计划。

论文讨论的是微软的Cosmos。Cosmos是微软的一个运行在大规模服务器集群上的分布式技术平台，专门用来存储和分析Massive Data，Scope是运行在Cosmos环境下的数据处理工具

- Scope is the Query Language for Cosmos（Scope是在Cosmos平台下运行的Query语言）
- Scope Is Not SQL （Scope像SQL 但不同于SQL）
- Scope的优化器负责生成最优的执行计划
- scope优化器能够利用数据分区信息能够在代价评估中考虑并行计划、串行计划、二者混合的计划，选出最优解

这篇论文虽是基于scope的，但已经在SQL Server中落地。

[[SIGMOD '00]Counting, enumerating, and sampling of execution plans in a cost-based query optimizer学习](https://zhuanlan.zhihu.com/p/626372143)

在对优化器的测试中，一项重要的测试内容是，对比分析优化器选出的plan的执行时间与优化器中其他候选plans的执行时间，进而判断优化器从候选plans中选择较佳的plan的能力，那么问题就来了，如何才能知道优化器中，其他候选plans的执行时间？

这篇论文给出了一个很好的方法，就是在优化器制定query的执行计划过程中，建立好数字和候选plans的对应关系，在SQL语句执行时，用户可以输入数字，指定执行plan。这个技术已经应用到SQL Server的优化器测试验证中了。

由于SQL Server采用了Cascade框架，因此，整个实现是围绕MEMO结构进行的。

[[Data Engineering Bulletin '08]Testing SQL Server's Query Optimizer--学习笔记](https://zhuanlan.zhihu.com/p/626015214)

查询优化本身就很复杂，因此，验证优化器的正确性和有效性是一项非常复杂的工作。正确性是说生成的查询计划对标原sql，能不能保证结果集的正确性；有效性是说，对原sql执行RBO+CBO变换后，得到的执行计划是不是高效的。

这篇论文描述了测试查询优化器时遇到的一些独特问题，概述了验证微软SQL Server的查询优化器的测试技术，给出了优化器测试过程中的成功经验和教训，最后讨论了测试器测试中持续存在的挑战。

论文更多的是从测试工程的角度说明优化器测试问题，以及微软SQL Server是如何测试优化器的，给出的方法相对要宏观一些，或者说更像是一种方向性的指引。很遗憾，可能是出于商业保密的原因，论文并没有公开优化器测试的实现机制。论文[DBTest 2012] Testing the Accuracy of Query Optimizers，给出了优化器测试的数学模型，但没有从系统工程的角度说明如何进行优化器的测试。这两篇论文有很强的互补性，都是很值得学习的。

[[Microsoft Research 2000] Parameterized queries and nesting equivalences--SQL Server去相关子查询](https://zhuanlan.zhihu.com/p/601494429)

本文为解决SQL关联子查询问题，提出了Apply算子，用于描述SQL子查询，并给出了Apply算子向join转换的恒等式，基于这些恒等式，可以实现SQL子查询去关联。Apply算子已经应用到SQL Server7.0。

[[SIGMOD '01] Orthogonal Optimization of Subqueries and Aggregation --SQL Server去相关子查询](https://zhuanlan.zhihu.com/p/601449775)

论文提出了一种小的、独立的Correlation removal和有效处理outer join、group by的源语（rule），通过正交组成丰富的执行计划，高效解决子查询问题。

[[SIGMOD 07] Execution strategies for SQL subqueries --SQL Server去相关子查询](https://zhuanlan.zhihu.com/p/601499565)

本文描述了SQL Server 2005中用于高效计算包含子查询的查询的大量策略。论文从必不可少的先决条件开始，例如子查询的相关性检测和删除。然后确定了一系列基本的子查询执行策略，例如正向查找和反向查找，以及set-based的方法，解释了在SQL Server中实现的子查询的不同执行策略，并将它们与当前的技术状态联系起来。实验评价对论文进行了补充。它量化了不同方法的性能特征，并表明在不同的情况下确实需要不同的执行策略，这使得基于成本的查询优化器对于提升查询性能必不可少。

[[SIGMOD 2012]Query optimization in Microsoft SQL Server PDW--学习笔记](https://zhuanlan.zhihu.com/p/618780370)

PDW QO是paper描述了SQL Server MPP查询优化器的设计与实现，Paralled Data Warehouse产品的查询优化器（PDW QO）。PDW QO是基于SQL Server单机Cascade优化器技术实现的针对分布式查询的CBO优化器。PDW QO在控制节点采用两阶段方式制定DPlan（分布式执行计划）：

1. 1. 基于单机Cascade优化器架构，使用全局统计信息，产生若干个潜在winner的memo结构
2. 2. 根据数据的分布信息，对这些候选plans进行枚举和剪枝，选择最佳，生成分布式的执行计划。并将这个分布式执行计划树转换为DSQL语句（扩展SQL），分发到各计算节点执行。

在控制节点制定分布式执行计划时，会根据数据重分布的操作将整棵查询执行树切割成不同的子树。每个子树对应查询计划的一个阶段，就是GreenPlum中的slice。每个slice都被转换成DSQL语句，在各个计算节点的sql server上执行，互相之间通过网络，发送到远端时要全物化，同时会收集物化的中间结果的统计信息，然后计算节点执行当前slice对应的dsql语句时，会基于这些统计信息重新进行查询优化，可能会有本地更优的计划。

与基于成本的transform相关的一个基本问题是，这些transform是否会导致需要评估的替代方案的组合数量爆炸，以及它们是否会在查询优化的成本和SQL执行的成本之间提供权衡。PDW优化器在这方面有一个非常重要的搜索优化，即timeout机制+从候选plans选取“seed”plans。

SQL Server优化器的timeout机制，不会生成所有可能的plans。在MEMO中基于全局统计信息的可能winner的plans对所考虑的Search space有很大的影响。为了提升PDW查询优化的效率，尽量不穷举所有的备选plans，而是从备选plans中考虑table的数据分布信息以高效配置DMS算子，选择部分"优质plans"，即“seed”plans。这个考虑数据分布的方式和interesting order类似，从上到下提出require distribution，从下向上提出provide distribution，进行匹配，不满足增加exchange，这个选种子plans时，尽量选择不需要增加exchange的候选plans。

## **CockroachDB**

目前，没有见到CockroachDB（简称CRDB）公开发表的优化器、执行器方面的论文，但其源码或设计文档中，明确告知了优化器执行器依赖的论文，具体如下：

### **（1）去相关子查询，主要实现了SQL Server的apply算子**

[[Microsoft Research 2000] Parameterized queries and nesting equivalences--SQL Server去相关子查询](https://zhuanlan.zhihu.com/p/601494429)

[[SIGMOD '01] Orthogonal Optimization of Subqueries and Aggregation --SQL Server去相关子查询](https://zhuanlan.zhihu.com/p/601449775)

[[SIGMOD 07] Execution strategies for SQL subqueries --SQL Server去相关子查询](https://zhuanlan.zhihu.com/p/601499565)

此外，去相关子查询中，实现了[[VLDB 06]Cost-based query transformation in Oracle 论文学习](https://zhuanlan.zhihu.com/p/595947967)中提出的规则。

### **（2）Outerjoin Simplification**

[[ACM Trans'97] Outerjoin Simplification and Reordering for Query Optimization--论文学习](https://zhuanlan.zhihu.com/p/601454148)

论文的主要贡献如下：

1. 1. Outerjoin Simplification，不改变join次序，提出了算法A （基于null reject的outerjoin Simplification，见2.1）
2.
    1. 1. selection算子下推
    2. 2. leftouter join 或 rightouter join 转 inner join
3. 1. join reorder
4.
    1. 1. 提出广义outerjoin算法**（Generalized Outerjoin）**，
    2. 2. join reorder变换的恒等式

这里重点介绍Outerjoin Simplification（outerjoin 化简）算法，因为这个算法至今仍在主流数据库优化器中使用。

简单说明join reorder算法**（Generalized Outerjoin）**，GOJ。因为这个算法在使用中存在很多局限性，例如仅适用于**simple outerjoin**，基于query graph进行实现，实现难度大。

对于join reorder算法感兴趣的同学可以阅读《[SIGMOD 13]On the correct and complete enumeration of the core search space》，论文主体算法已经应用于TiDB、PolarDB、CockroachDB等多款主流数据库中。

### **（3）Join Reorder**

[[VLDB 2006]Analysis of Two Existing and One New Dynamic Programming--论文学习](https://zhuanlan.zhihu.com/p/632872022)

论文描述了现有文献中的两种构造join order tree的动态规划算法的方法，DPSize算法、DPsub算法，通过分析和实验表明，这两种算法对于不同的query graph表现出非常不同的效果。更具体地说，对于chain （链型）Join graph查询，DPsize优于DPsub，对于clique（集团型）Join graph查询，DPsub优于DPsize。另外，这两种算法都不能很好的处理star（星型）查询。为解决这些问题，论文提出了一种优于DPSize和DPsub的算法，即DPCpp算法，该算法适用于所有类型的query graph。

CRDB主要实现了DPSube算法，其最初的来源为DPsub算法。

[[SIGMOD 13]On the correct and complete enumeration of the core search space--学习](https://zhuanlan.zhihu.com/p/595870481)

本文针对多表join，基于交换律、结合律、左结合律、右结合律，进行等价转换，产生正确且完整的plan集合(core search space)，论文主体算法已经应用于TiDB、PolarDB、CockroachDB等多款主流数据库中。

[再读[SIGMOD 13]On the correct and complete enumeration of the core search space](https://zhuanlan.zhihu.com/p/632565261)

本文针对当前主流两种基于动态规划的plan生成器（DB-based plan generator）算法，即NEL/EEL方法算法、SES/TES方法等会在Join Reorder中产生无效plans的问题，论文提出了三种Join Reorder过程中的冲突检测算法（CD-A、CD-B、CD-C），其中CD-C最为完备，能够用于生成完整的core search space（搜索空间），即根据初始的plan，基于Join Reorder算法，生成所有合法的候选plans的集合。

### **（4）统计**

[[ACM,2013]HyperLogLog in Practice Algorithmic Engineering of a State of The Art 学习笔记](https://zhuanlan.zhihu.com/p/632565939)

基数估计（Cardinality Estimation），也称为 count-distinct problem，是为了估算在一批数据中，它的不重复元素的个数，distinct value(DV)，具有广泛的应用前景，在数据库系统中尤为重要。虽然基数可以很容易地使用基数中的线性差值来计算，但对于许多应用程序，这是完全不切实际的，并且消耗大量内存。因此，许多近似基数而使用较少资源的算法已经被开发出来。这些算法在网络监控系统、数据挖掘应用程序以及数据库系统中发挥着重要的作用，HyperLogLog算法就是其中之一。

目前，HyperLogLog算法已经应用于Redis、PolarDB PostgreSQL、OpenGauss、CockroachDB、AnalyticDB for PostgreSQL、PostgreSQL、Spark、Flink、Presto等。

在本文中，作者提出了对该算法的一系列改进，以减少了其内存需求，并显著提高了其对一个重要的基数范围的精度。作者已经为谷歌的一个系统实现了这个新提出的算法，并对其进行了经验评估，并与改进前的HyperLogLog算法进行了比较。与改进前的HyperLogLog一样，作者改进的算法可以完美地并行化执行，并在single-pass中完了基数估计的计算。

### **（5）基础设施框架**

[University of Waterloo 2000] Exploiting Functional Dependence in Query Optimization

ANSI SQL关系模型中已经包括函数依赖分析，但函数依赖分析具有复杂性，因为存在如下情况：

- 空值
- 三值逻辑（three-valued logic），即TRUE、FALSE、UNKNOWN
- outer joins
- 重复的rows

那么如何简化函数依赖分析，并充分利用函数依赖分析进行查询优化呢？

本文是滑铁卢大学2000年的一篇博士论文，主要解决了这方面的问题。论文虽然有些老，但学完之后还是感觉收益匪浅，特别是通过研读本文提供的函数依赖定义、定理、推理等，有助于理解函数依赖在如下SQL优化中的作用：

- selectivity estimation
- estimation of (intermediate) result sizes
- order optimization(in particular sort avoidance)
- cost estimation
- various problems in the area of semantic query optimization

[不会游泳的鱼：[University of Waterloo 2000] Exploiting Functional Dependence in Query Optimization--学习笔记上-基本认知](https://zhuanlan.zhihu.com/p/603358443)

[不会游泳的鱼：[University of Waterloo 2000] Exploiting Functional Dependence in Query Optimization--学习笔记中-数学定义](https://zhuanlan.zhihu.com/p/603362330)

[不会游泳的鱼：[University of Waterloo 2000] Exploiting Functional Dependence in Query Optimization--学习笔记下-函数依赖生成算法](https://zhuanlan.zhihu.com/p/603364178)

[[SIGMOD 1996] Fundamental Techniques for Order Optimization 论文学习](https://zhuanlan.zhihu.com/p/595949653)

这是DB2 96年的一篇关于join中order（排序）优化论文，提出了具体的优化方法，并概述了DB2的工程实现。这篇论文虽然比较老，但至今仍有很大的指导作用。

在数据库优化器中，排序是一个非常消耗资源的算子，如果能够利用数据中的索引或排序的属性，降低对排序算子的使用，可以大幅提升SQL执行效率。具体来说，在Group By、Order By、Distinct子句中，父节点都会对子节点提出 Interesting Order ,优化器如果能够利用底层已有的序（例如，主键、索引），满足 Interesting Order，就不需要对底层扫描的行做排序，还可以消除 ORDER BY。

论文提出了一种在join中下推sort算子的方法，以减少按照columns对数据的排序，基于谓词、索引、主键信息，在等效情况下，消除排序算子。

为了便于对论文的理解，这里对Interesting Order、Physical Property进行解释。

- Interesting Order:（也许叫Interesting Property更合适，因为多数Interesting的都是排序属性。）由父节点传给子节点，要求其返回满足特定Physical Property的最优计划，例如升序、降序。另一个能够解释“Interesting Order”名称的是**Join Order**。在多表互Join时，表的Join顺序（Join Order）排列组合数量巨大，影响查询性能；父节点需选择哪些Join Order应被进一步探索（Join Enumeration）。它们成了“Interesting Order”。
- Physical Property：Operator返回的结果带有的属性，最典型的例子是排序与否；还例如结果是否已计算Hash；结果在单服务器上，还是分散在多服务器（需要额外GatherMerge）。

在开始做CBO之前，优化器会top-down的遍历QGM，针对不同算子可能的有序性需求（join/group by/distinct/order by），建立算子的interesting order，这个过程称为对QGM的**order scan**。

### **（5）优化器框架**

[[IEEE Data engineering Bulletin 1995] The Cascades Framework for Query Optimization论文阅读](https://zhuanlan.zhihu.com/p/590793684)，CRDB采用了Cascade架构。

[[SIGMOD 2014] Orca: A Modular Query Optimizer Architecture for Big Data 论文阅读](https://zhuanlan.zhihu.com/p/590795795)，Cascade架构实现类似ORCA。

### **（6）SQL System**

[[SIGMOD 2017]Spanner Becoming a SQL System--学习笔记](https://zhuanlan.zhihu.com/p/625805991)

Spanner是一个面向全球的分布式DBMS，为谷歌数百项关键服务提供数据支撑。谷歌在OSDI‘12上发表了第一篇Spanner的论文，那个时候的Spanner可以理解分布式KV数据库，支持动态扩缩容、自动分区、多版本、全球部署、基于Paxos的多副本机制、支持外部一致性的分布式事务（SSI），详见[OSDI '12] Spanner Google’s Globally-Distributed Database。本文讲的是Spanner后续实现的分布式SQL引擎，包括分布式查询执行、临时故障时查询重启、如果根据查询数据的范围将请求路由到数据所在的range（数据分片）、索引的查询机制以及在存储方面的优化。

目前，两个著名的分布式NewSQL数据库tidb和cockroach，就是inspire by Spanner 和 F1。

### **（7）向量化执行引擎**

CRDB向量化执行引擎inspire by MonetDB-X100，但未使用SIMD。

[[CIDR 2005]MonetDB-X100 Hyper-Pipelining Query Execution--学习笔记](https://zhuanlan.zhihu.com/p/640004245)

[Balancing Vectorized Query Execution with Bandwidth-Optimized Storage-第四章MonetDB/X100 overview--学习笔记](https://zhuanlan.zhihu.com/p/640267685)

[Balancing Vectorized Query Execution with Bandwidth-Optimized Storage-第五章MonetDB/X100向量化执行引擎](https://zhuanlan.zhihu.com/p/640518615)

## **Hyper/DuckDB**

Hyper提出了很多经典算法，但未开源，不过这些算法基本都在DuckDB中实现了。

[Unnesting Arbitrary Queries--Hyper去相关子查询论文学习](https://zhuanlan.zhihu.com/p/613010202)

SQL 99中允许子查询出现在SELECT、FROM和WHERE子句中，从用户使用的角度看，嵌套子查询能够极大地简化复杂查询的形式。但是，在相关子查询中，优化器不加处理，直接传递给执行器计算，其算法复杂度为N方，数据量大的情况下，性能极差。paper提出来了一种去相关子查询的通用且高效算法。

[[BTW 2017]The Complete Story of Joins (in HyPer)--HyPer去相关子查询论文学习](https://zhuanlan.zhihu.com/p/614953165)

这篇论文是《[BTW 2015] Unnesting Arbitrary Queries》的延续，在去相关子查询方面，提出了两个新的Join扩展算子mark-join和single-join，并给出了逻辑上的等价变换规则，物理上的实现算法。

[[SIGMOD 2008]Dynamic Programming Strikes Back--读书笔记](https://zhuanlan.zhihu.com/p/632565843)

DPCCP算法与DPsize和DPsub算法相比优势在于能够高效获取csg-cmp-pairs：

- DPCCP算法
- 连通子图，在query graph中，依次以base tables为种子，步长为1，采用广度优先的方法，对每个种子节点求邻近节点集合，邻近节点的编号必须大于当前节点，这样保证去重，然后基于种子节点和第1级邻近节点集合中的所有非空子集构成种子节点步长为1的所有连通子图，然后对于其中的每个连通子图，再采用类似的方法，步长为1，扩展获得下一级所有的连通子图，以此类推，就可以获取query graph中的所有连通子图
- 连通子图的补图，对于任意的连通子图，以其邻居节点集合中的每个节点为种子节点，采用了和连通子图扩展相似的方式，获取所有的连通子图，其中，扩展时要排除掉原连通子图，并且仅选择比种子节点编号大的节点
- DPCCP通过上述方法，直接获取了csg-cmp-pairs，高效先进
- DPsize和DPsub
- 采用了枚举方式，获取节点集合对，(S1,S2)，再判断S1和S2各自是否是连通的，是否存在交集，S1和S2之间是否连通，这样才能得到csg-cmp-pairs，效率要低

DPCCP算法存在如下两个缺陷，使其不能直接应用于数据库实践中，可以理解为是一种学术性的创新。

1. 1. 仅能处理inner join，不支持full join，left join，right join，semi join，anti join等
2. 2. 仅能处理简单的join谓词，即join的left input或right input仅包含1个base table，无法处理这种join两个是多个base tables的复杂join谓词，t1.a = t2.b + t3.d

本文是DPCCP算法的延续，开发了一种新的算法DPhyp，能够保留DPCCP在寻找csg-cmp-pairs方面的优势，并解决DPCCP的2个缺陷。

MySQL 8.0中已经实现了DPhyp算法。CockroachDB采用了DPSube算法查找best join order，但将DPhyp列为了TODO。

[[VLDB 2015] How Good Are Query Optimizers, Really](https://zhuanlan.zhihu.com/p/628138716)

论文的作者是大名鼎鼎的CWI实验室Peter Boncz教授、慕尼黑工业大学数据库研究组长Thomas Neumann教授和Alfons Kemper博士，其中，Peter Boncz教授是MonetDB的创始人之一，Thomas Neumann是HyPer的创始人之一。

论文认为，Join orders对查询的性能非常重要。优化器中，cardinality estimator（基数估计）组件、cost model(代价模型)、 plan enumeration（plan的枚举技术）是查找好的Join order的核心组件。论文设计并实施了Join Order Benchmark (JOB)，即Join Order基准测试，实验结果表明：

- cardinality estimation（基数估计）组件：往往产生较大的误差
- cost model(代价模型)：其对性能的影响要比cardinality estimator（基数估计）小的多
- plan enumeration（plan的枚举技术）：对比了穷举动态规划和启发式算法，发现在次优的基数估计情况下，穷举方法能够改善查询性能

最终，论文得出结论，过度依赖cardinality estimation（基数估计）组件、cost model(代价模型)、plan enumeration（plan的枚举技术），不能得到让人满意的性能。

[[VLDB 2011]Efficiently Compiling Efficient Query Plans for Modern Hardware --学习笔记](https://zhuanlan.zhihu.com/p/623906536)

这篇Paper的作者是著名的Thomas Neumann，慕尼黑工业大学（TMU）的数据库研究组组长，HyPer数据库的初始人之一。论文讲述了如何在执行器中，编译执行查询计划，CodeGen概念就是出自这篇论文。如果对PostgreSQL、Opengauss等数据库的JIT技术感兴趣的话，推荐阅读原文。

论文认为，随着内存硬件的进步，查询性能越来越取决于查询处理本身的原始CPU成本。经典的火山迭代器模型虽然技术非常简单和灵活，但由于缺乏局部性（locality）和频繁的指令错误预测，在现代cpu上的性能较差。过去已经提出了一些技术，如面向批处理的处理或向量化tuples处理策略来改善这种情况，但即使是这些技术也经常被hand-written计划执行。

这篇论文提出了一种新的编译策略，它使用LLVM编译器框架将查询转换为紧凑而高效的机器代码。通过针对良好的代码和数据局部性和可预测的分支布局，生成的代码经常可以与手写的C++代码相媲美。将这些技术已经集成到HyPer数据库系统中，获得了很好的查询性能，而只需要增加适度的编译时间。

## **Calcite**

[[SIGMOD 2018] Apache Calcite 论文学习笔记](https://zhuanlan.zhihu.com/p/624607251)

Apache Calcite是一个基础的软件框架，提供SQL 解析、查询优化、数据虚拟化、数据联合查询 和 物化视图重写等功能，目前已经应用于多款开源数据库或大数据处理系统中，例如

- Apache Hive，Apache Storm，Apache Flink，Druid 和 MapD
- Polar-X、Doris、StarRocks、SelectDB

Calcite框架组成如下：

- 查询优化器，内置几百种优化规则，模块化且可扩展
- 查询处理器，支持多种查询语言
- 适配器模式的体系结构，支持多种异构数据模型（关系结构格式、半结构化格式、流格式、地理信息格式）

Calcite框架具有灵活性、可嵌入性、可扩展性等特点，因此，对于涉及多源异构类型的大数据框架来说，很具吸引力。Apache Calcite项目非常活跃，持续支持新型数据源、查询语言、查询处理以及查询优化方法。

[[ICDE 1993] The Volcano Optimiz](https://zhuanlan.zhihu.com/p/590785118)er Generator- Extensibility and Efficient Search （Calcite优化器架构）

Volcano模型旨在解决扩展和性能问题。EXODUS是同一个作者在Volcano的一个优化器设计的版本，EXODUS虽然已经证明了优化器生成器范式的可行性和有效性，但很难构建高效的、满足生产环境质量的优化器。Volcano也是在EXODUS的基础之上优化而来。Volvano在EXODUS上面的做的一些优化： 前者(EXODUS)没有区分逻辑表达式和物理表达式，也没有物理Properties的概念，没有一个通用的Cost函数等等。Volcano优化器要满足如下目标：

1. 1. 这个新的优化器生成器必须能够使用Volcano Executor，也可以在其他项目中作为独立的工具
2. 2. 必须更加高效，比较低的时间消耗和内存消耗
3. 3. 必须提供有效的，高效的，和可扩展的物理属性支持，如sort order、compression
4. 4. 必须允许使用启发式和数据模型语义来指导搜索并删除搜索空间中无用的部分（剪枝）
5. 5. 它必须支持灵活的代价模型，对于查询中的子项能够产生动态查询计划

## **GreenPlum**

[[SIGMOD 2014] Orca: A Modular Query Optimizer Architecture for Big Data 论文阅读](https://zhuanlan.zhihu.com/p/590795795)

Orca是Pivotal公司基于Cascade优化器框架开发的SQL优化器服务，以独立的Service形态单独存在，并不依附于特定的数据库产品，对外是标准化的接口和协议，调用者通过标准化的输入，由Orca负责根据输入指定SQL执行计划，由调用者将这个基于Orca的SQL执行计划转换成自身数据库的SQL执行计划，这样理论上可以被集成到任何数据库系统中。已经应用于Greenplum和基于Hadoop的HAWQ中。是Volcano/Cascades的论文思想的一个业界著名的工程化实现。

[[IEEE Data engineering Bulletin 1995] The Cascades Framework for Query Optimization论文阅读](https://zhuanlan.zhihu.com/p/590793684)，支持Cascade架构

[[SIGMOD 1979] Access Path Selection in a Relational Database Management System 论文阅读](https://zhuanlan.zhihu.com/p/590776990)，支持System R架构

## **MemSQL**

[[VLDB 2016] The MemSQL Query Optimizer，HTAP优化器论文阅读](https://zhuanlan.zhihu.com/p/594101717)

当前主流优化器有2种，[System R](https://zhuanlan.zhihu.com/p/590776990)（bottom-up）和[Volcano/Cascades](https://zhuanlan.zhihu.com/p/590793684)（top-down），二者各有千秋。 但对于MemSQL来说，皆无法满足需求。

这是因为，MemSQL是一个高性能、分布式HTAP数据库，需要解决OLAP实时负载分析问题，要求SQL优化时间非常有限，现有优化器框架不能满足这个需求，集成已有优化器框架还意味着继承框架的所有缺点和集成挑战**。因此，MemSQL从头开始构建，采用了创新的工程，如内存优化锁(lock - free)和能够运行实时流分析的列存储引擎(columnstore engine)，开发了一个功能丰富的查询优化器，能够跨各种复杂的企业工作负载生成高质量的查询执行计划。**

## **MonetDB/X100**

[[CIDR 2005]MonetDB-X100 Hyper-Pipelining Query Execution--学习笔记](https://zhuanlan.zhihu.com/p/640004245)，简介见上文

[Balancing Vectorized Query Execution with Bandwidth-Optimized Storage-第四章MonetDB/X100 overview--学习笔记](https://zhuanlan.zhihu.com/p/640267685)，简介见上文

[Balancing Vectorized Query Execution with Bandwidth-Optimized Storage-第五章MonetDB/X100向量化执行引擎](https://zhuanlan.zhihu.com/p/640518615)，简介见上文

[[DaMoN 2011]Vectorization vs. Compilation in Query Execution--学习笔记](https://zhuanlan.zhihu.com/p/641268363)

在关系数据库中，查询执行一般有三种策略，解释型执行、编译执行和向量化执行。在OLAP领域，编译执行和向量化执行要优于解释执行的方式。但编译执行和向量化执行哪种更佳？如何基于现代CPU，为向量化执行引擎配置好编译执行的策略，获取最佳性能？围绕这个问题，这篇论文就行了研究分析。作者针对Project、Select、Hash join算子深入调研分析了VectorWise（MonetDB X100的商业化版本）数据库的向量化执行引擎和编译策略。调研发现

- 对于block-wise 查询执行引擎来说，最好使用编译执行的方式
- 找到了三种场景下，“loop-compilation”策略不如向量化执行引擎
- 如果能较好的将编译执行和向量化执行结合，可以获取更好的性能
-
    - either by incorporating vectorized execution principles into compiled query plans
    - using query compilation to create building blocks for vectorized processing

## **优化器测试论文**

[[DBTest 2012] Testing the Accuracy of Query Optimizers论文阅读](https://zhuanlan.zhihu.com/p/590798843)

查询优化器的准确性与数据库系统性能及其操作成本密切相关:优化器的成本模型越准确，得到的执行计划就越好。数据库应用程序程序员和其他从业者长期以来提供了一些轶事证据，表明数据库系统在优化器的质量方面存在很大差异，但迄今为止，数据库用户还没有正式的方法来评估或反驳这种说法。

在本文中，我们开发了一个TAQO 框架来量化给定工作负载下优化器的准确性。我们利用优化器公开开关或hint，让用户控制查询计划，并生成除默认计划之外的计划。使用这些实现，我们为每个测试用例强制生成多个替代计划，记录所有替代计划的执行时间，并根据它们的有效成本对执行计划进行排序。我们将这个排名与估计成本的排名进行比较，并为优化器的准确性计算一个分数。

我们给出了对几个主要商业数据库系统进行匿名比较的初步结果，表明系统之间实际上存在实质性差异。我们还建议将这些知识纳入商用数据库开发过程。

[[CIKM 2016] OptMark- A Toolkit for Benchmarking Query Optimizers--优化器评价论文学习](https://zhuanlan.zhihu.com/p/626262614)

如何评估数据库优化器的质量，这个问题非常有挑战性。目前，现有数据库的benchmarks测试（如TPC benchmarks）是对整体的query的性能评估，并不是针对优化器。这篇论文描述了OptMark，一个用于评估优化器质量的toolkit，可惜没有开源。论文从Effectiveness（有效性） 和Efficiency（效率）两个维度，评价优化器的质量，并给出了具体的评价公式。

[[SIGMOD '00]Counting, enumerating, and sampling of execution plans in a cost-based query optimizer学习](https://zhuanlan.zhihu.com/p/626372143)

在对优化器的测试中，一项重要的测试内容是，对比分析优化器选出的plan的执行时间与优化器中其他候选plans的执行时间，进而判断优化器从候选plans中选择较佳的plan的能力，那么问题就来了，如何才能知道优化器中，其他候选plans的执行时间？

这篇论文给出了一个很好的方法，就是在优化器制定query的执行计划过程中，建立好数字和候选plans的对应关系，在SQL语句执行时，用户可以输入数字，指定执行plan。这个技术已经应用到SQL Server的优化器测试验证中了。

由于SQL Server采用了Cascade框架，因此，整个实现是围绕MEMO结构进行的。

[[Data Engineering Bulletin '08]Testing SQL Server's Query Optimizer--学习笔记](https://zhuanlan.zhihu.com/p/626015214)

查询优化本身就很复杂，因此，验证优化器的正确性和有效性是一项非常复杂的工作。正确性是说生成的查询计划对标原sql，能不能保证结果集的正确性；有效性是说，对原sql执行RBO+CBO变换后，得到的执行计划是不是高效的。

这篇论文描述了测试查询优化器时遇到的一些独特问题，概述了验证微软SQL Server的查询优化器的测试技术，给出了优化器测试过程中的成功经验和教训，最后讨论了测试器测试中持续存在的挑战。

论文更多的是从测试工程的角度说明优化器测试问题，以及微软SQL Server是如何测试优化器的，给出的方法相对要宏观一些，或者说更像是一种方向性的指引。很遗憾，可能是出于商业保密的原因，论文并没有公开优化器测试的实现机制。论文[DBTest 2012] Testing the Accuracy of Query Optimizers，给出了优化器测试的数学模型，但没有从系统工程的角度说明如何进行优化器的测试。这两篇论文有很强的互补性，都是很值得学习的。
