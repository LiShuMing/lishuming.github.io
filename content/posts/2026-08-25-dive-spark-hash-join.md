---
title: "【源码】深入 Spark Hash Join：从 JoinSelection、HashedRelation 到 AQE 与 Spill 边界"
date: 2026-08-25T00:00:00+08:00
lastmod: 2026-10-18T00:00:00+08:00
slug: "dive-spark-hash-join"
categories:
  - 大数据
  - 数据库
tags:
  - Apache Spark
  - Hash Join
  - Sort Merge Join
  - AQE
  - Spill
  - Velox
description: "本文已收作 Hash Join 主文附录；本页保留跳转与原问题陈述。"
draft: false
---

> **本文已并入**：[深入 Hash Join]({{< relref "2026-08-26-dive-hash-join.md" >}}) 文末 [附录：Spark Hash Join 特有路径]({{< relref "2026-08-26-dive-hash-join.md" >}}#spark-hash-join)。请阅读主文附录；本页暂不 301。

## 原问题（一句）

Spark 如何在 JoinSelection、HashedRelation、AQE 与内存边界之间选择 Broadcast/Shuffled Hash Join 或退回 Sort-Merge，以及为何内置 Hash Join 在 Spill 问题上与 StarRocks/Doris/ClickHouse/DuckDB 的可恢复协议不同。

附录保留：JoinSelection 决策树、BHJ/SHJ 与 HashedRelation、Spill 失败边界、AQE/倾斜/Native Engine 要点。
