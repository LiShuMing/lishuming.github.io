# Blog Writing Guidelines

## Core Philosophy: First Principles & Lucid Prose

Technical articles on this blog are written for curious engineers who want to truly understand *why* a system, algorithm, or bug behaves the way it does — not to read a defensive audit report or a directory listing.

1. **Reason from First Principles (从第一性原理出发)**
   - Before introducing internal class names, paper acronyms, or module trees, state the fundamental tension in plain language.
   - Ask: *If we built this in the simplest, most naive way, what would break first?* Identify the underlying physical, mathematical, or state-consistency constraint — memory/network hierarchy, combinatorial search explosion, concurrent state ownership across untrusted/async processes, or semantic edge cases (`NULL`, skew, partial failure).
   - Let the architecture, data structure, or algorithm emerge as the inevitable answer to that constraint, so the reader understands *why it has to be designed this way* rather than memorizing *what exists*.

2. **Concrete Before Abstract (先具体场景，后抽象概念)**
   - Walk through one concrete example — a 5-row join, a single IPC call interrupted mid-flight, a 114-character string mutating into 136 characters — before stating the general rule or invariant.
   - Unpack dense jargon. Instead of stacking four abstract nouns in one clause (e.g., "能力可见性、服务启动和关闭态读取"), explain the actual runtime behavior in clear, everyday engineering language.

3. **Fluent, Natural Chinese (语言通俗易懂、行文自然通畅)**
   - Write like a senior engineer explaining a hard problem clearly to a smart colleague at a whiteboard.
   - Connect paragraphs through genuine causal flow ("既然最朴素的做法会在 X 处卡住，最自然的想法是改成 Y；但一旦引入 Y，又会撞上 Z……"). Do not fragment arguments into isolated one-sentence paragraphs, bullet slogans, or 50+ micro-headings.
   - Never mechanically prepend a stiff one-line thesis summary under every `###` heading, and never leave raw template labels (`主张：/ 机制：/ 适用：/ 不适用：`) in published prose.
   - Avoid stock clichés (`不可否认`、`值得注意的是`、`双刃剑`、`注入新动能`) and overused rhetorical formulas (`真正重要的不是……而是……`、`本质上是一种……`).

4. **Technical Rigor Without Disclaimer Spam (严谨靠事实与机制，而非免责声明轰炸)**
   - **Do not** append defensive caveats (`这不代表……`、`不能据此推出……`、`更不能推广到……`) at the end of every other paragraph. Readers already know a single benchmark or PoC is not a universal law.
   - **Baseline at the end, short commit prefixes only:** Never block the opening hook with version tables, cutoff timestamps, or `源码基线说明` / `环境说明` callouts — place baseline/version/environment notes concisely at the **end** of the article. When referencing Git commits in prose or tables, use **short prefix fingerprints** (7–8 hex characters, e.g., `` `b1d73b1` ``) rather than 40-character full SHA-1 strings.
   - When discussing boundaries and trade-offs, explain the **concrete mechanism of failure or cost** (e.g., *why* splitting a join loses money when key degrees are uniform, or *why* materializing a CTE hurts when consumer predicates are disjoint) instead of repeating generic legalistic disclaimers.

---

## Adapting Structure to the Genre

Choose a structure that serves the article's core question rather than forcing a uniform template:

- **Source & Architecture Analysis (源码与架构剖析)**
  Start from the core engineering contradiction the software must solve. Trace one end-to-end lifecycle (an input message, a query plan, a memory allocation, a packet) across boundaries. Show *who owns which state* and *where failure recovery lines are drawn*, using pinned source links and focused snippets as evidence rather than cataloging every file in the repository.

- **Engineering Practice & Troubleshooting (工程实践与排障复盘)**
  Follow the real investigative chain: the Concrete Symptom → Initial Hypothesis (and why it failed) → Controlled Isolation Experiment → Root Cause & Fix → Remaining Open Risks. Keep real observations, hashes, commands, and unverified gaps honest; never invent fake detours or measurements.

- **Papers, Optimizers & Systems Research (论文精读与技术调研)**
  Organize around a unified cost or correctness model (e.g., *where unnecessary work is generated, paid for, or deferred*), not a conference reading log. Do not bury the opening under 40 lines of retrieval-window audit tables — hook the reader on the core technical shift first, and keep bibliographic scope notes concise. When investigating how to build a new system capability, lead the reader naturally from real workflow pain → why existing tools only solve part of it → architectural trade-offs → concrete design & verification.

- **Travel Writing & Personal Essays (游记与随笔)**
  Let concrete memories, sensory observations, and historical context carry the narrative in the author's natural voice. Never impose engineering templates or forced philosophical morals after every stop.

---

## Craft, Terminology & Validation

- **Headings, Tables, and Visual Flowcharts:** Use headings only for meaningful turns in the argument (typically 5–10 top-level sections, not dozens of fragmented subheadings). Use tables for multi-dimensional comparisons and **visual flowcharts / architecture / lifecycle / sequence / dataflow diagrams** (generated via the local `archify` skill into standalone light-theme SVGs under `static/images/diagrams/` and optional interactive views under `static/diagrams/`, rather than wall-of-text descriptions or brittle ASCII art) whenever tracing state transitions, query/execution pipelines, memory/network hierarchies, or cross-process call chains. Never add a table or diagram that merely repeats a trivial adjacent sentence.
- **Terminology:** On the first appearance of a specialized concept or proper name where the English term aids clarity, include it naturally in parentheses — e.g., `因子化执行（Factorized Execution）`, `谓词下推（Predicate Pushdown）`. Keep established English project names and code identifiers as-is; do not gloss ordinary words.
- **Factual & Link Integrity:** Preserve publication dates, URLs/slugs, series navigation links, pinned commit hashes, and real experimental conditions. Never fabricate benchmarks, code behavior, or personal experiences.
- **Build & Render Check:** After creating or revising articles, run `hugo` to verify clean compilation and inspect the rendered HTML of changed posts (title hierarchy, TOC/anchor resolution, math/table rendering, and static asset paths).
