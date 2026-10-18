---
title: "【调研】数据库性能优化如何形成自主闭环：Agent、Benchmark 与可信实验"
date: 2026-09-30T12:00:00+02:00
lastmod: 2026-10-18T12:00:00+02:00
slug: "dive-autonomous-database-performance-loop"
categories:
  - 数据库
tags:
  - 性能工程
  - 查询优化器
  - 执行引擎
  - LLM Agent
  - Benchmark
  - 实验平台
description: "本文已并入优化研究平台与自主性能闭环合并稿；本页保留原问题陈述与跳转。"
toc: true
math: true
draft: false
---

> **本文已并入合并稿**：[数据库优化研究平台与自主性能闭环]({{< relref "2026-09-30-dive-database-optimization-research-platform.md" >}})（下篇 · 闭环与实验）。请阅读合并稿；本页仅保留原问题入口，暂不 301。

## 原问题（一句）

数据库性能优化如何把候选生成与结果验收分开，使 Agent、Benchmark 与实验平台形成可恢复、可审计、可证伪的闭环——而不是把工具 `exit code = 0` 误当成优化成立。

合并后的结构：上篇（情报与证据）来自研究平台文；下篇（闭环与实验）收录本文原论证与检查表。
