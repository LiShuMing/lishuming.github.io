---
title: "Code Review（一）：原则与证据——review 到底在优化什么"
date: 2026-10-24T21:01:00+08:00
slug: "code-review-principles-and-evidence"
categories:
  - 工程实践
tags:
  - Code Review
  - 工程文化
description: "用 Google eng-practices、MCR 实证研究和 Apache/LLVM/PostgreSQL 规范，重新定义 review 的目标、标准、粒度与节奏。"
---

> 系列索引见 [Code Review 系列](/2026/10/24/code-review-series-index/)。

## 面试主线：如何给出可辩护的合入判断

面向 Senior / Staff / Tech Lead 数据库内核岗位，本篇训练的是合入判断：如何把“不放心”转成可复现的风险，把“应该更好”转成可以延期的建议。公开规范提供决策语言，研究解释评审可能产生什么价值；它们不能替某个优化器（Optimizer）规则或存储协议证明正确性。回答时先给技术依据，再说明如何协调交付与责任。

## 1. 标准：improve code health，而非 perfect

【事实】Google 的 [The Standard of Code Review](https://google.github.io/eng-practices/review/reviewer/standard.html) 写明：只要 CL（changelist，即一次提交单元）确定能改善系统整体代码健康度，reviewer 就应该批准，哪怕它不完美；可有可无的意见加 `Nit:` 前缀，不阻塞合并。同一文档还规定技术事实和数据压过个人偏好，风格问题以 style guide 为准。

【推断】这条标准在内核团队里最容易走偏，常见的是两个极端：committer 拿自己的偏好卡新人，或者因为作者资深就直接放行。xlab 原稿的“权威效应”“确认偏误”两条陷阱说的就是这两种情况。比较稳的做法是让每条阻塞意见都写明理由，比如违反了哪个不变量（Invariant）、会触发哪类故障，并允许作者拿数据反驳。

## 2. 证据：review 真正产出了什么

- 【事实】Bacchelli & Bird（ICSE 2013，[Expectations, Outcomes, and Challenges of Modern Code Review](https://ieeexplore.ieee.org/document/6606617)）研究的是 Microsoft 的 MCR（Modern Code Review，基于工具的轻量异步评审）。开发者把“找缺陷”列为首要动机，但实际评论里缺陷类占比不高，更多产出是知识传递、团队意识、替代方案，以及对“理解变更”的需求。
- 【事实】Sadowski 等人（ICSE-SEIP 2018，*Modern Code Review: A Case Study at Google*，[DOI 10.1145/3183519.3183525](https://doi.org/10.1145/3183519.3183525)）的数据：Google 的大多数变更很小，通常只有一名 reviewer，中位响应时延在小时级。教育作用和维护规范被列为核心价值。
- 【事实】Google [Small CLs](https://google.github.io/eng-practices/review/developer/small-cls.html) 给的经验值是 100 行左右算合理，1000 行通常过大；改动分散在多个文件时还要更小。[Speed of Code Reviews](https://google.github.io/eng-practices/review/reviewer/speed.html) 要求最迟一个工作日内回应。

【推断】xlab 原稿建议 200–500 行，比 Google 宽松。内核 PR 经常带测试和生成代码，不能据此设统一合入门槛，应分开看两类：纯重构/机械改动可以大，语义改动（optimizer 规则、事务、存储格式）应该尽量小。

## 3. 开源社区的 review 机制

- **Apache**：【事实】[Apache Voting](https://www.apache.org/foundation/voting.html) 规定，对代码修改投 −1 是 veto（否决），必须附技术理由，没有理由的 veto 无效。StarRocks 不是 ASF 项目，它的流程以 [CONTRIBUTING](https://github.com/StarRocks/starrocks/blob/main/CONTRIBUTING.md) 为准。
- **LLVM**：【事实】[LLVM Code Review Policy](https://llvm.org/docs/CodeReview.html) 区分 pre-commit 和 post-commit review。小而明显的改动可以先提交再评审，有争议的改动必须先评审；reviewer 的“LGTM”默认代表他对所评审部分的认可。
- **PostgreSQL**：【事实】PostgreSQL 用 CommitFest 周期管理补丁，[Reviewing a Patch](https://wiki.postgresql.org/wiki/Reviewing_a_Patch) 把 review 拆成几层：补丁能否应用、功能是否符合需求、性能、代码规范、文档。社区期望提交补丁的人也去 review 别人的补丁。

【推断】这些材料可用于比较异议如何提出、认可覆盖哪些代码、谁有最终提交权限，不能据此给社区贴上效率或质量标签。数据库内核团队应明确模块审阅、跨模块契约确认和最终合入的责任，社区名称不能替代这些细节。

## 4. 设计评审先于代码评审

【事实】Malte Ubl 的 [Design Docs at Google](https://www.industrialempathy.com/posts/design-docs-at-google/) 认为，design doc 的价值在于实现前就暴露分歧，记录 trade-off 和被否决的方案；小改动不必写。【推断】内核里的存储格式、协议、公开语义（SQL 行为、默认值）一旦合入就很难回退，这类改动应该先过 RFC/设计评审，代码评审仍需检查设计假设是否被实现和测试支持。原稿缺这一层，本系列把它补上了。

## 5. 节奏

原稿提到的固定时段 review、单次不超过 60 分钟、复杂 PR 分多轮看不同维度，都与上面的研究方向一致。我的补充【推断】：分多轮看时先看 PR 描述和测试，再看接口，最后看实现；第一轮就发现方向错了，应该立刻停下来同步讨论，不要继续写行级评论。

## 6. 面试问答：将规范转成内核决策

以下是【推断】性质的备考推演，不是研究报告中的面试结论。

**问题：Google 允许“不完美”合入，错误结果也能留到以后修吗？**

**精简强答**：先确定变更契约。不完美可以是非关键命名或可独立偿还的结构问题；若规则在合法 SQL 上改变结果，已经违反语义契约，不能用整体代码更整洁来抵消。对风险的争论要收敛到触发条件、影响和验证，而非“零风险”口号。

**追问**：只发现一个可疑分支，没有反例呢？答：指出尚未证明的条件，尝试构造最小输入；不能把怀疑说成已确认缺陷。若涉及持久格式且失败后不可逆，应在关键疑点澄清前暂停合入；可隔离的实验路径则说明开关、默认状态和退出条件。

**差答**：“正确性 P0，其他都不阻塞。”严重的设计复杂度、无法控制的资源增长和有条件可复现的性能退化也可能阻塞；优先级需要结合后果，不能只按问题名称分档。

**问题：一个跨优化器、执行器（Executor）、存储的大 PR 怎么拆？**

**精简强答**：按可验证的契约拆，先建立接口和兼容读取能力，再接入新路径，最后切换默认行为与清理旧路径；每步必须说明可编译、可测试、是否可部署，以及中间版本支持什么。若新格式先写出而旧节点无法读取，代码上的小 PR 仍可能构成部署上的大风险。

**追问**：为什么不能只按文件或行数拆？如何防止中间提交改变行为？答：模块边界不等于语义边界；需要逐步写出行为差异、开关与测试，必要时将不可独立成立的步骤放在同一评审单元。拆不动也可能是协议原子切换的约束，不能直接推断作者设计能力不足。

**差答**：“所有 PR 控制在固定行数以内。”行数不能表达规则等价证明的难度、恢复状态数或测试生成代码的占比，研究中的规模观察也不是团队质量阈值。

**问题：Staff/TL 如何判断 review 机制改进有效？**

**精简强答**：先说明瓶颈是排队、上下文理解还是返工，再选择首次响应、等待时长、返工原因及漏出缺陷等材料。按变更类型和风险看分布，检查是否把成本转嫁给作者或发布阶段；评论数量和通过速度都不能单独代表质量。

**追问**：时延下降但事故变多，是新机制导致的吗？答：先核对变更量、类型、发布节奏和事故归因；前后相关不足以证明因果。没有对照证据时，只能报告观察和待验证解释。

**差答**：“review 越严越好”“评论越多说明越认真”。二者都缺乏与缺陷、维护成本及交付目标之间的因果链。

## 7. 用评审记录证明判断，而非背社区规范

准备一条真实阻塞意见及其上下文：基于哪个提交版本、涉及哪个不变量、如何复现、作者怎样修订、最后由谁确认。再准备一条自己撤回的意见；若确有这类记录，解释是遗漏了调用方保证，还是新数据改变了判断。没有就说明没有，不补写转折。

面试展示时，评论、补丁和验证结果应能相互对应。公开论文证明的是所研究环境中的观察，不证明自己的团队采用同一措施必然获益。最终建议应受变更风险、发布能力和验证资源约束；对尚无证据的改进，先提出可核验的试行目标。
