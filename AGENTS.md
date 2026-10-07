# Blog Writing Guidelines

## Technical Articles

Apply these rules when writing, researching, analyzing source code for, or revising technical articles intended for professional engineers.

### Structure

- Build a progressive argument: establish the problem and constraints, explain the mechanisms, then discuss outcomes, trade-offs, and applicability. Each section must advance the argument rather than assemble disconnected terms, systems, or source summaries.
- State each major section's technical or business judgment early and support it with facts or a causal explanation. A subsection may begin with a concrete observation, counterexample, or code snippet when that is the clearest route to the judgment; explain its significance promptly. Do not mechanically prepend a generic thesis sentence to every heading.
- Organize around one main question and a chain of dependencies. Use headings for meaningful advances in the argument, not for every term or implementation detail. Merge neighboring sections when removing their headings produces a coherent paragraph without losing a distinct argument.
- Adapt the structure to the article rather than forcing one template on every post:
  - Source analysis: follow a request, operation, or state transition; explain control flow, state ownership, invariants, and failure paths.
  - Comparative research: establish common questions and constraints before comparing systems; compare mechanisms and cost allocation rather than assembling product summaries.
  - Paper reading: explain the assumption that fails, the proposed mechanism, the supporting experiments, and the limits of the evidence.
  - Practice notes: preserve the actual problem, failed attempts, corrections, and verification. Do not turn a partial experiment into a comprehensive theory.

### Language and Style

- Avoid stock phrases such as “不可否认” (undeniably), “值得注意的是” (it is worth noting), “双刃剑” (a double-edged sword), and “注入新动能” (inject new momentum).
- Avoid empty lyrical parallelism. Prefer concrete nouns and verbs; use fewer adjectives and adverbs.
- Develop reasoning in coherent paragraphs. Do not fragment an argument into isolated short sentences, slogans, or excessive subheadings.
- Use transitions to explain why the next mechanism or question follows. Do not replace a fragmented list with a longer list of generic introductory claims.
- Avoid repeatedly using contrast formulas such as “真正重要的不是……而是……” or “本质上是一种……”. State the mechanism or constraint directly when the contrast adds no information.
- Give the introduction, analysis, and conclusion different jobs: establish the question and scope, develop the evidence, then state decision conditions or remaining verification work. Do not repeat the same list of conclusions at all three points.
- Use tables for comparisons with stable dimensions and diagrams for relationships or state transitions. Use prose for causal reasoning. Avoid introducing a table or diagram merely to restate the adjacent paragraph.
- End with explicit applicability limits or actionable recommendations. Do not add unsupported predictions, generic outlooks, or rhetorical uplift.

### Evidence and Applicability

- Support every claim with a concrete industry example, engineering parameters, or an explicit logical derivation.
- For source-code analysis, identify the relevant version, code locations, call paths, and mechanisms. Link to traceable sources for papers and public materials.
- Preserve the experimental conditions, workloads, and version assumptions needed to interpret engineering parameters, performance figures, and conclusions. Distinguish verified facts, deductions, and assumptions. Never invent examples, parameters, or measurements.
- Explain each proposed approach's trade-offs and scenarios where it does not apply, including the mechanisms behind those limits. Do not list only advantages.
- Treat code as evidence: explain why a snippet matters, identify the decisive branch or state transition, and state what it does and does not establish. Keep complete structures, long call inventories, and repetitive field descriptions in a reference section when they interrupt the argument; preserve useful technical detail rather than deleting it to achieve brevity.
- Preserve the author's reasoning process when it is documented: an initial assumption, the observation that challenged it, and the resulting change in judgment. Never invent experiments, mistakes, personal experiences, or epiphanies to make a narrative more engaging.
- Distinguish evidence levels without labeling every paragraph mechanically: source-confirmed behavior, a paper's reported result, the author's deduction, and an unvalidated design proposal are not interchangeable.

## Proper Names and Technical Terms

Apply these naming conventions to technical articles, travel writing, and personal essays.

- At the first occurrence of a Chinese proper name or specialized term in an article, append its established English name in parentheses: `中文名称（English Name）`.
- Cover people, places, institutions, projects, named algorithms, and specialized concepts where the English name helps identification. Examples: `普拉多博物馆（Prado Museum）` and `谓词下推（Predicate Pushdown）`.
- Use official or widely accepted English names and preserve their spelling and capitalization. If no established English name exists, use the original-language name and make that distinction clear rather than inventing a translation.
- After the first annotation, use the Chinese name consistently without repeating the parenthetical at every mention. Keep established English project names and code identifiers unchanged; do not add redundant English annotations to them.
- Annotate complete terms rather than substrings of compound words. Do not add English glosses to ordinary verbs or common words when they do not identify a specialized concept, and do not alter quoted text or code identifiers to insert annotations.

## Travel Writing and Personal Essays

- Do not mechanically apply technical-article requirements for technical judgments, engineering parameters, or solution trade-offs to travel writing or personal essays.
- Preserve the author's concrete memories, wording, and line of thought. Expand verifiable history, context, and biographical information; do not invent experiences, feelings, or epiphanies on the author's behalf.
- Let the itinerary and actual observations carry the narrative. Connect related memories into paragraphs rather than manufacturing lyrical transitions or a lesson after every stop.
- A philosophical quotation may introduce a question when the author wants it, but do not repeatedly echo it throughout the article or force the ending to return to it.

## Editorial Workflow and Validation

- Before editing, identify the article's main question, genre, intended reader, evidence boundaries, and repeated material. Revise the outline before polishing individual sentences when the problem is structural.
- For style experiments, prefer a small, explicitly scoped set of representative recent posts before applying the same changes broadly. Preserve publication dates, URLs, useful source material, and unrelated working-tree changes unless requested otherwise.
- Separate automated checks from editorial judgment:
  - Automated checks: front matter, code fences, tables, bold and math rendering, local links and anchors, unresolved TODOs, duplicate passages, terminology consistency, and moving-branch source links. Flag findings for review rather than treating every match as an error.
  - Editorial review: does this section advance the question, does the citation support the claim, are performance conditions preserved, are proposals distinguished from implementation, and would removing a paragraph lose necessary information?
- Do not use an “AI-writing score” as an acceptance criterion. Evaluate evidence, coherence, precision, and fidelity to the author's voice instead.
- Build the site and inspect the changed articles' rendered output in proportion to the changes. Report checks actually performed and separate dependencies that were not executed; a successful site build is not a reproduction of the articles' experiments.
