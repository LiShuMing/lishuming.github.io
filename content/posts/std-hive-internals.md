---
title: std::hive 实现原理深度分析
date: 2025-07-17T00:00:00+08:00
categories:
  - 技术分享-summary
tags:
  - C++
  - 2025
---

# std::hive 实现原理深度分析 —— 基于 plf::hive 源码

**分析版本**：Matt Bentley 的 [`plf::hive`](https://github.com/mattreecebentley/plf_hive)，本地源码 commit [`085899f`](https://github.com/mattreecebentley/plf_hive/commit/085899f55591e77d49ed168be4594200aa0f0c3a)（2026-07-31）。

**标准依据**：[`P0447R28`](https://www.open-std.org/jtc1/sc22/wg21/docs/papers/2024/p0447r28.html) 与当前 C++ 工作草案的 [`[hive]`](https://eel.is/c++draft/hive) 章节。

**目标读者**：对 C++ 容器实现有兴趣的系统、数据库内核和高性能计算工程师。

---

## 1. 背景：为什么还需要一种容器

在使用 C++ 时，经常要在两类场景之间做选择：一类是低频更新、频繁读取和遍历；另一类是频繁插入、删除，且对象之间还存在长期引用。

静态或低更新场景通常选择固定大小或连续内存布局，例如 `std::array`、`std::vector`（配合 `reserve`）以及 Folly 的 `small_vector`。频繁更新的随机访问场景则经常选择 `std::list`。但在性能敏感场景中，我们又希望数据尽可能连续，以便更好地管理内存并利用 CPU cache。

这种偏好在 query engine 中尤其明显：hash table 通常使用基于数组的线性探测；aggregate 算子会用 vector 保存 blocking columns；`std::list` 或以 `std::deque` 为底层的 `std::queue`，则更适合需要稳定地从两端组织数据的场景，例如 shuffle 中的缓冲队列。

### 1.1 常见结构的静态/动态适用性

| 数据结构 | 更适合的更新模式 | 原因与代价 |
| --- | --- | --- |
| `std::vector` | 非常适合静态或尾部追加 | 连续内存、cache locality 好、遍历快；中间插入和删除需要移动后续元素，扩容还会使引用和迭代器失效 |
| `std::array` | 最静态 | 大小在编译期固定，没有扩容成本，布局最稳定 |
| `std::span` | 只读视图或静态访问 | 不拥有数据，非常适合对既有连续内存做轻量访问；生命周期由外部负责 |
| 排序后的 `vector` + `binary_search` | 非常适合静态查找 | 构建一次后查询为 `O(log N)`，通常比 tree/hash 更 cache-friendly |
| `flat_map` / `flat_set` | 静态或低频更新 | 本质是排序数组，查询快、内存紧凑，但插入和删除为 `O(N)` |
| `std::map` / `std::set` | 动态更新 | 红黑树使插入和删除为 `O(log N)`，但 pointer chasing 多、cache locality 差 |
| `std::unordered_map` / `std::unordered_set` | 动态或混合 | 平均查询 `O(1)`，更新方便；具体局部性取决于拉链法或开放地址法，内存开销通常较大 |
| B-tree 类容器 | 中低频更新 | 比红黑树的 locality 好，适合大规模有序索引 |
| Trie | 取决于实现 | 静态 Trie 可以高度压缩；动态 Trie 的更新成本和内存占用通常更高 |
| Perfect Hash | 极静态 | key 集合固定后可做到接近 `O(1)`、低冲突，但很难动态更新 |
| Bloom Filter | 偏静态 | 构建后查询极快，适合作为只读过滤器；存在假阳性，不保存原始集合 |
| Bitmap / Bitset | 非常适合静态集合 | 紧凑且对 SIMD/位运算友好，但要求 key domain 可以映射到位下标 |
| CSR 图（Compressed Sparse Row） | 典型静态结构 | 图构建后连续存储，遍历很快；增删边昂贵 |
| 邻接表（`vector<vector<...>>`） | 中等动态 | 比 CSR 更容易更新，但 locality 和压缩性略差 |
| `std::list` | 频繁插入和删除 | 节点位置稳定，插入和删除为 `O(1)`；每个元素至少承担两个链接指针，遍历时 pointer chasing 严重 |
| `std::deque` | 两端频繁更新 | 分段数组避免整体搬迁，随机访问为 `O(1)`；内存并非全局连续 |
| `std::hive` | 频繁无序插入、删除并频繁遍历 | 分段连续，其他元素地址稳定，删除槽位可复用；不支持 `operator[]`，插入位置未指定 |

### 1.2 从底层内存组织看容器

大多数 C++ 数据结构最终建立在两种基本组织方式之上：连续内存（contiguous memory）和节点存储（node-based storage）。不同容器之间真正的区别不只是逻辑数据结构是 Tree、Hash 还是 Heap，更重要的是内存布局和访问策略。

典型的连续内存结构包括：

- `array`
- `vector`
- `string`
- `flat_map`
- `flat_set`
- heap（通常由 `priority_queue` 包装）
- sorted vector + binary search

典型的节点存储结构包括：

- `list`
- `forward_list`
- `map`
- `set`
- `multimap`
- `multiset`

那么 `hash_map`、`queue` 和 `priority_queue` 属于哪一种？

- `hash_map` 只是逻辑数据结构。底层既可以采用拉链法，此时元素是 node-based；也可以采用开放地址法，此时数据主要位于数组中。必须看具体实现。
- `priority_queue` 是容器适配器，逻辑上维护 heap；默认底层容器是 `vector`，因此通常是连续数组。
- `queue` 也是容器适配器，默认底层容器是 `deque`，而不是 `vector` 或 `list`。`deque` 通常由 map of blocks（索引块加多个固定大小数据块）实现：两端插入只需在端部 block 写入，必要时新增 block，不需要像 `vector` 扩容那样搬迁所有元素；与 `list` 相比，它又避免了每个元素单独分配和严重的 pointer chasing。它是在整体连续性与两端更新成本之间的折中。

### 1.3 `vector` 和 `list` 之间的空白

`deque` 解决的是两端扩展问题，但不能同时提供“任意位置删除后其他元素地址稳定”和“遍历时较好的局部性”。我们真正需要的是：

1. 单元素插入和删除为常数复杂度；
2. 插入、删除不移动其他元素；
3. 迭代器、指针和引用在对应元素未被删除时保持有效；
4. 一个内存块中容纳多个元素，迭代时尽量顺序读取；
5. 删除产生的空洞能够被后续插入复用。

这就是 `std::hive` 的设计空间。它与 Apache Hive 无关；hive 在这里取“蜂巢”的含义。Daniel Lemire 的文章 [How fast is C++26 `std::hive`?](https://lemire.me/blog/2026/08/02/how-fast-is-c26s-stdhive/) 用一句话概括了其内部结构：

> Internally, a hive is a linked list of blocks. Each block carries a skipfield: a small integer per slot that tells the iterator how many erased slots to jump over.

需要先修正两个容易产生误解的说法：

- `std::hive` **不是一整块连续内存**，而是多个内部连续的 block/group 组成的链；准确描述是“分段连续”。
- 截至本文分析的标准草案，`std::hive` 已进入 C++26 工作草案，而不再只是“标准候选”。`plf::hive` 是与标准提案接口保持一致的参考实现之一，但标准只规定行为和复杂度，不强制实现必须逐行复刻它。

在游戏引擎、实时仿真、事件系统和粒子系统中，大量对象会被持续创建和销毁，又被其他结构通过指针或迭代器引用。`std::list` 的碎片和 cache 不友好会成为瓶颈，`vector` 的搬迁又破坏引用稳定性。数据库内核中的运行期对象池、执行器状态、动态 row batch 也有类似矛盾。

---

## 2. 一张图理解 `plf::hive`

先建立整体心智模型：一个 hive 不是只有一条 group 链，而是维护三套不同用途的链表。

```text
active group list（双向，决定迭代顺序）

begin_iterator
      │
      ▼
┌─────────┐   next    ┌─────────┐   next    ┌─────────┐
│ group 0 │ ────────► │ group 1 │ ────────► │ group 2 │
│ elements│ ◄──────── │ elements│ ◄──────── │ elements│
│ skipfield│ previous │ skipfield│ previous │ skipfield│
└─────────┘           └─────────┘           └─────────┘
                                               ▲
                                               │
                                          end_iterator

groups-with-erasures list（双向，只串有空洞的 active group）

erasure_groups_head ──► group 1 ⇄ group 2

unused-groups list（单向，保存 reserve/erase/clear 留下的预留块）

unused_groups_head ──► reserved group ──► reserved group ──► null
```

三条链分别回答三个问题：

| 链表 | 入口 | 作用 | 为什么需要单独维护 |
| --- | --- | --- | --- |
| 活跃 group 双向链 | `begin_iterator.group_pointer` | 定义正向、反向迭代顺序 | 迭代器跨 group 时只需追踪前后指针 |
| 含空洞 group 双向链 | `erasure_groups_head` | 找到至少有一个可复用槽位的 group | 插入无需扫描所有 group 或 skipfield |
| 预留 group 单向链 | `unused_groups_head` | 保存已分配但暂未参与迭代的 group | `reserve`、清空尾块和再次增长可以复用内存 |

容器级成员如下（源码 [`plf_hive.h:369-380`](https://github.com/mattreecebentley/plf_hive/blob/085899f55591e77d49ed168be4594200aa0f0c3a/plf_hive.h#L369-L380)）：

```cpp
// Hive member variables:
iterator begin_iterator;
iterator end_iterator;

group_pointer_type erasure_groups_head;
group_pointer_type unused_groups_head;

size_type total_size;
size_type total_capacity;
skipfield_type min_block_capacity;
skipfield_type max_block_capacity;

group_allocator_type group_allocator;
aligned_struct_allocator_type aligned_struct_allocator;
skipfield_allocator_type skipfield_allocator;
tuple_allocator_type tuple_allocator;
```

这里有几个贯穿整个实现的不变量：

- `total_size` 是所有 active group 中有效元素数之和。
- `total_capacity` 同时包括 active group 和 reserved group 的容量。
- `begin_iterator` 指向全局第一个有效元素，而不一定指向首个 group 的 `elements[0]`。
- `end_iterator` 指向最后一个 active group 中最后构造位置的后一位；它不必等于该 group 的物理尾部。
- active group 不能是“完全删除但仍留在迭代链中”的空块；当一个 group 变空时，代码会释放它或转入 unused list。
- `erasure_groups_head == nullptr` 只表示 active groups 没有可复用的删除槽位，不表示最后一个 group 没有尚未构造的尾部容量。

---

## 3. 核心数据结构：`group`、元素区与 skipfield

### 3.1 `group` 元数据

`plf::hive` 的核心是 `group`。它不是传统哈希表意义上的 bucket，而是一个自包含的逻辑块：一份元数据，加上一块单独动态分配、但内部连续的“元素区 + skipfield 区”。

下面按语义对源码 [`plf_hive.h:322-365`](https://github.com/mattreecebentley/plf_hive/blob/085899f55591e77d49ed168be4594200aa0f0c3a/plf_hive.h#L322-L365) 对齐排版：

```cpp
struct group
{
    // 迭代热路径使用的字段放在前面。
    skipfield_pointer_type skipfield;
    group_pointer_type next_group;

    // elements 与 skipfield 实际来自同一块动态分配。
    const aligned_struct_pointer_type elements;
    group_pointer_type previous_group;

    // group 内部 free list 的头，以及容量/有效元素计数。
    skipfield_type free_list_head;
    const skipfield_type capacity;
    skipfield_type size;

    // 含删除槽位的 group 组成另一条侵入式双向链。
    group_pointer_type erasures_list_next_group;
    group_pointer_type erasures_list_previous_group;

    // 支持跨 group 的迭代器顺序比较。
    size_type group_number;

    group(
        aligned_struct_allocator_type &aligned_struct_allocator,
        const skipfield_type elements_per_group,
        const group_pointer_type previous)
        : next_group(nullptr),
          elements(std::allocator_traits<aligned_struct_allocator_type>::allocate(
              aligned_struct_allocator,
              get_aligned_block_capacity(elements_per_group),
              (previous == nullptr) ? 0 : previous->elements)),
          previous_group(previous),
          free_list_head(std::numeric_limits<skipfield_type>::max()),
          capacity(elements_per_group),
          size(1),
          erasures_list_next_group(nullptr),
          erasures_list_previous_group(nullptr),
          group_number((previous == nullptr) ? 0 : previous->group_number + 1u)
    {
        skipfield = pointer_cast<skipfield_pointer_type>(
            to_aligned_pointer(elements) + elements_per_group);
        std::memset(
            std::to_address(skipfield),
            0,
            sizeof(skipfield_type) *
                (static_cast<size_type>(elements_per_group) + 1u));
    }
};
```

字段顺序也属于优化的一部分：`skipfield` 和 `next_group` 是 `operator++` 的高频访问字段，因此被放在结构体开头；`capacity` 虽然可以通过 `skipfield - elements` 计算，但源码选择直接缓存，利用结构体 padding 换取更短的热路径。

### 3.2 一次分配中的内存布局

```text
低地址
┌──────────────────────────────┐  ◄── group->elements
│ aligned_element_struct[0]    │
│ aligned_element_struct[1]    │
│ ...                          │
│ aligned_element_struct[N-1]  │
├──────────────────────────────┤  ◄── group->skipfield
│ skipfield[0]                 │
│ skipfield[1]                 │
│ ...                          │
│ skipfield[N-1]               │
│ skipfield[N]                 │  额外哨兵，始终为 0
└──────────────────────────────┘
高地址
```

`elements` 与 skipfield 数组来自一次 allocation，而 `group` 元数据本身是另一次 allocation。更精确地说：

```text
group 元数据 allocation ──指向──► [elements | skipfield | sentinel] allocation
```

因此“elements 和 skipfield 合并为一次分配”是正确的，但不能进一步推导为“group 元数据也在同一次分配里”。[`allocate_new_group()`](https://github.com/mattreecebentley/plf_hive/blob/085899f55591e77d49ed168be4594200aa0f0c3a/plf_hive.h#L826-L856) 先分配一个 `group`，再由 `group` 构造函数分配元素/skipfield block；异常时回滚 `total_capacity` 并释放 group。

```cpp
group_pointer_type allocate_new_group(
    const skipfield_type elements_per_group,
    const group_pointer_type previous = nullptr)
{
    if (max_size() - total_capacity < elements_per_group)
    {
        throw std::length_error(
            "New block allocation would create capacity greater than max_size()");
    }

    const group_pointer_type new_group =
        std::allocator_traits<group_allocator_type>::allocate(
            group_allocator, 1, previous);
    total_capacity += elements_per_group;

    try
    {
        std::allocator_traits<group_allocator_type>::construct(
            group_allocator,
            new_group,
            aligned_struct_allocator,
            elements_per_group,
            previous);
    }
    catch (...)
    {
        std::allocator_traits<group_allocator_type>::deallocate(
            group_allocator, new_group, 1);
        total_capacity -= elements_per_group;
        throw;
    }

    return new_group;
}
```

### 3.3 为什么元素槽位要人为加宽

删除后的元素内存还要保存 free-list 的两个索引，因此单个槽位至少要容纳 `2 * sizeof(skipfield_type)`。源码定义了 [`aligned_element_struct`](https://github.com/mattreecebentley/plf_hive/blob/085899f55591e77d49ed168be4594200aa0f0c3a/plf_hive.h#L231-L243)：

```cpp
struct alignas(alignof(element_type)) aligned_element_struct
{
    char data[
        (sizeof(element_type) < (sizeof(skipfield_type) * 2))
            ? (((sizeof(skipfield_type) * 2) < alignof(element_type))
                   ? alignof(element_type)
                   : (sizeof(skipfield_type) * 2))
            : ((sizeof(element_type) < alignof(element_type))
                   ? alignof(element_type)
                   : sizeof(element_type))];
};
```

它表达的核心公式是：

```text
slot_size = max(sizeof(T), alignof(T), 2 * sizeof(skipfield_type))
slot_alignment = alignof(T)
```

这只会明显影响极小元素类型。例如 `char` 的真实对象只占 1 字节，但槽位至少需要 2 字节，才能在删除后存入两个 8-bit free-list index。

### 3.4 `skipfield_type` 的自适应选择

源码 [`plf_hive.h:202`](https://github.com/mattreecebentley/plf_hive/blob/085899f55591e77d49ed168be4594200aa0f0c3a/plf_hive.h#L202) 根据元素大小和对齐选择 skipfield 宽度：

```cpp
typedef std::conditional_t<
    (sizeof(element_type) > 10 || alignof(element_type) > 10),
    uint_least16_t,
    uint_least8_t>
    skipfield_type;
```

- 当 `sizeof(T) > 10` 或 `alignof(T) > 10` 时，使用至少 16 bit 的整数。
- 否则使用至少 8 bit 的整数。
- `numeric_limits<skipfield_type>::max()` 被拿来表示 free-list 空端，因此它既是哨兵，也约束默认 block 的最大可寻址容量。

标准没有要求实现必须采用这个 10 字节阈值；这是 `plf::hive` 在元素开销、block 上限和迭代性能之间做出的实现选择。

---

## 4. Skipfield：如何用一次加法跳过整段空洞

### 4.1 值语义与 skipblock

skipfield 遵循 **low-complexity jump-counting pattern**：

- `skipfield[i] == 0`：该位置对迭代器而言不是已删除元素。它可能已经构造，也可能位于 `end()` 之后、尚未构造。
- `skipfield[i] != 0`：该位置属于已删除区间。
- 一段连续删除位置称为 skipblock；首、尾 skipfield 值都等于 skipblock 长度。
- 中间节点只需保持非零，不负责给 `++` 或 `--` 提供跳距；当前实现通常写入 `1`，以便 `get_iterator()` 判断该槽位不是活跃对象。

例如：

```text
slot index:   0  1  2  3  4  5  6  7  8
skipfield:   [0, 0, 1, 0, 0, 3, 1, 3, 0]
state:        A  A  E  A  A  E  E  E  A
```

- 位置 `0、1、3、4、8` 是 active element；
- 位置 `2` 是长度为 1 的 skipblock；
- 位置 `5、6、7` 是长度为 3 的 skipblock，首尾的 `3` 才是双向跳转边界；
- 中间的 `1` 不是“剩余长度”，只是非零占位。

### 4.2 为什么 skipfield 多一个哨兵

每个 group 都分配 `capacity + 1` 个 skipfield 节点，最后一个节点始终为 0。这样 `erase()` 可以无条件读取当前节点后一位，`operator++` 也能先递增 skipfield 指针再读取，而无需在热路径加入额外的数组越界分支。

注意，哨兵解决的是 skipfield 读取安全性，不是说元素区也多了一个可写槽位。元素指针到达 `group->skipfield` 转换后的地址时，已经位于元素区 one-past-end。

### 4.3 迭代器为什么保存三个指针

`hive_iterator` 保存 group、元素槽位和对应 skipfield 三个位置：

```cpp
template <bool is_const>
class hive_iterator
{
    group_pointer_type group_pointer{nullptr};
    aligned_pointer_type element_pointer{nullptr};
    skipfield_pointer_type skipfield_pointer{nullptr};
};
```

理论上可以只存 group 加 slot index，但每次解引用和递增都要重新计算地址。`plf::hive` 用更大的迭代器换取更短的热路径。

### 4.4 `operator++` 的完整状态转移

源码 [`plf_hive.h:3793-3826`](https://github.com/mattreecebentley/plf_hive/blob/085899f55591e77d49ed168be4594200aa0f0c3a/plf_hive.h#L3793-L3826) 的非 MSVC 分支如下：

```cpp
hive_iterator &operator++()
{
    assert(group_pointer != nullptr);

    // 先移动到“下一物理槽位”的 skipfield。
    skipfield_type skip = *(++skipfield_pointer);

    // element 先跨过一个槽位，再一次跨过整个 skipblock。
    if ((element_pointer += static_cast<size_type>(skip) + 1u) ==
            to_aligned_pointer(group_pointer->skipfield) &&
        group_pointer->next_group != nullptr)
    {
        // 到达当前 group 尾部后，进入下一个 active group。
        group_pointer = group_pointer->next_group;
        const aligned_pointer_type elements =
            to_aligned_pointer(group_pointer->elements);
        const skipfield_pointer_type skipfield = group_pointer->skipfield;

        // 下一个 group 的开头也可能是 skipblock。
        skip = *skipfield;
        element_pointer = elements + skip;
        skipfield_pointer = skipfield;
    }

    skipfield_pointer += skip;
    return *this;
}
```

这里不是逐槽扫描：无论连续删除了 1 个还是 200 个元素，迭代器只读取 skipblock 首节点的长度并做一次指针加法。因此单次 `++` 保持常数复杂度，遍历复杂度以 active elements 为主，而不是退化成对每个已删除槽位逐个判断。

反向迭代利用 skipblock 尾节点保存的同一长度，逻辑完全对称：

```cpp
hive_iterator &operator--()
{
    assert(group_pointer != nullptr);

    if (--skipfield_pointer >= group_pointer->skipfield)
    {
        element_pointer -=
            static_cast<size_type>(*skipfield_pointer) + 1u;
        if ((skipfield_pointer -= *skipfield_pointer) >=
            group_pointer->skipfield)
        {
            return *this;
        }
    }

    group_pointer = group_pointer->previous_group;
    const skipfield_pointer_type skipfield =
        group_pointer->skipfield + group_pointer->capacity - 1;
    const skipfield_type skip = *skipfield;
    element_pointer =
        (to_aligned_pointer(group_pointer->skipfield) - 1) - skip;
    skipfield_pointer = skipfield - skip;

    return *this;
}
```

### 4.5 迭代器稳定性的准确边界

元素存储在固定 group block 中。插入只会在删除槽位原地构造、在尾部未构造槽位构造，或新增/复用另一个 group；删除只销毁目标对象并更新元数据。因此，普通插入和单元素删除都不会移动其他元素。

但“任何迭代器永远不失效”并不严谨。准确边界是：

| 操作 | 对元素指针、引用和迭代器的影响 |
| --- | --- |
| `insert` / `emplace` | 已有元素的指针、引用和迭代器保持有效；past-the-end iterator 会失效 |
| `erase(it)` | 只使被删除元素的指针、引用和迭代器失效；删除尾元素还会使 past-the-end iterator 失效 |
| `reserve` / `trim_capacity` | 已有元素位置保持有效 |
| `splice` | 被转移元素地址保持不变，迭代器改为属于目标 hive；两端的 past-the-end iterator 失效 |
| `reshape` / `shrink_to_fit` | 如果触发元素重分配，会使所有元素引用和迭代器失效 |
| `sort` | 当前标准允许实现重排或重分配，引用、指针和迭代器都可能失效 |

`begin()` 也可能在最前方元素被删除，或更早的空洞被新插入复用时改变；旧的、指向仍存活元素的迭代器有效，不等于它仍然等于新的 `begin()`。

---

## 5. Free list：如何在常数时间定位可复用空洞

skipfield 回答“迭代时如何跳过空洞”，free list 回答“插入时如何直接找到空洞”。两者解决的是不同方向的问题。

### 5.1 两级索引结构

`plf::hive` 使用两级结构定位空洞：

1. `erasure_groups_head` 指向一条“含空洞 group”的双向链；
2. 每个 group 的 `free_list_head` 指向该 group 内某个 skipblock 的首槽位。

group 内 free list 是侵入式、基于 index 的双向链。每个 skipblock 只占一个 free-list node，这个 node 就存放在 skipblock 首个已删除元素的内存中：

```text
erasure_groups_head
        │
        ▼
┌────────────────── group ──────────────────┐
│ free_list_head = 5                        │
│                                           │
│ erased slot[2]: [prev = max][next = 5]    │
│ erased slot[5]: [prev = 2  ][next = max]  │
│                   ▲                       │
│                   └── 当前 free-list head │
└───────────────────────────────────────────┘
```

这里的 `prev` / `next` 是 group 内 slot index，不是机器指针。使用 `skipfield_type` 宽度的 index 有两个收益：

- 一个删除槽位只需容纳两个很小的整数，而不是两个 64-bit 指针；
- 链接只在 group 内解释，block 被整体 splice 时无需修正每个内部链接。

`numeric_limits<skipfield_type>::max()` 同时作为链首/链尾和“没有 free list”的哨兵。

源码通过 allocator-aware 的构造/销毁在已结束生命周期的元素存储区中写入 index：

```cpp
void edit_free_list_prev(
    const aligned_pointer_type location,
    const skipfield_type value) noexcept
{
    edit_free_list(pointer_cast<skipfield_pointer_type>(location), value);
}

void edit_free_list_next(
    const aligned_pointer_type location,
    const skipfield_type value) noexcept
{
    edit_free_list(
        pointer_cast<skipfield_pointer_type>(location) + 1,
        value);
}

void edit_free_list_head(
    const aligned_pointer_type location,
    const skipfield_type value) noexcept
{
    const skipfield_pointer_type converted_location =
        pointer_cast<skipfield_pointer_type>(location);
    edit_free_list(converted_location, value);
    edit_free_list(
        converted_location + 1,
        std::numeric_limits<skipfield_type>::max());
}
```

### 5.2 删除一个元素时的四种 skipblock 合并

`erase()` 在销毁对象、递减 `total_size` 与 `group->size` 后，只需看当前节点左侧 skipfield 值和右侧 skipfield 值。源码使用按位 `&` / `|` 组合布尔条件，目的是帮助编译器生成更紧凑、分支特征更稳定的代码。

| 左侧已有 skipblock | 右侧已有 skipblock | 操作 | free-list 变化 |
| --- | --- | --- | --- |
| 否 | 否 | 新建长度为 1 的 skipblock | 新增一个 node，并把它设为 `free_list_head` |
| 是 | 否 | 把左侧 skipblock 长度加 1 | node 仍位于原左侧 skipblock 首槽，无需改链 |
| 否 | 是 | 当前槽成为右侧 skipblock 的新首槽 | 把原 node 的前后链接迁移到当前槽，并修正相邻 node |
| 是 | 是 | 合并左右 skipblock 与当前槽 | 保留左侧 node，删除右侧 node，并连接其前后邻居 |

核心分支来自 [`plf_hive.h:1865-1939`](https://github.com/mattreecebentley/plf_hive/blob/085899f55591e77d49ed168be4594200aa0f0c3a/plf_hive.h#L1865-L1939)：

```cpp
const skipfield_type prev_skipfield = *(
    it.skipfield_pointer -
    (it.skipfield_pointer != it.group_pointer->skipfield));
const skipfield_type after_skipfield = *(it.skipfield_pointer + 1);

if ((prev_skipfield == 0) & (after_skipfield == 0))
{
    // 左右都不是删除槽：创建长度为 1 的新 skipblock。
    *it.skipfield_pointer = 1;
    const skipfield_type index = static_cast<skipfield_type>(
        it.element_pointer -
        to_aligned_pointer(it.group_pointer->elements));

    if (it.group_pointer->free_list_head !=
        std::numeric_limits<skipfield_type>::max())
    {
        edit_free_list_next(
            to_aligned_pointer(it.group_pointer->elements) +
                it.group_pointer->free_list_head,
            index);
    }
    else
    {
        add_to_groups_with_erasures_list(it.group_pointer);
    }

    edit_free_list_head(
        it.element_pointer,
        it.group_pointer->free_list_head);
    it.group_pointer->free_list_head = index;
}
else if ((prev_skipfield != 0) & (after_skipfield == 0))
{
    // 只和左侧相连：扩大左侧 skipblock。
    *(it.skipfield_pointer - prev_skipfield) =
        *it.skipfield_pointer =
            static_cast<skipfield_type>(prev_skipfield + 1);
}
else if ((prev_skipfield == 0) & (after_skipfield != 0))
{
    // 只和右侧相连：当前槽变成 skipblock 新起点。
    *(it.skipfield_pointer + after_skipfield) =
        *it.skipfield_pointer = after_skipfield + 1;

    const skipfield_type following_previous = *(
        pointer_cast<skipfield_pointer_type>(it.element_pointer + 1));
    const skipfield_type following_next = *(
        pointer_cast<skipfield_pointer_type>(it.element_pointer + 1) + 1);

    edit_free_list_prev(it.element_pointer, following_previous);
    edit_free_list_next(it.element_pointer, following_next);

    const skipfield_type index = static_cast<skipfield_type>(
        it.element_pointer -
        to_aligned_pointer(it.group_pointer->elements));

    if (following_previous !=
        std::numeric_limits<skipfield_type>::max())
    {
        edit_free_list_next(
            to_aligned_pointer(it.group_pointer->elements) +
                following_previous,
            index);
    }

    if (following_next != std::numeric_limits<skipfield_type>::max())
    {
        edit_free_list_prev(
            to_aligned_pointer(it.group_pointer->elements) + following_next,
            index);
    }
    else
    {
        it.group_pointer->free_list_head = index;
    }
}
else
{
    // 左右都有 skipblock：合并两段，并移除右侧 free-list node。
    *it.skipfield_pointer = 1;
    *(it.skipfield_pointer - prev_skipfield) =
        *(it.skipfield_pointer + after_skipfield) =
            static_cast<skipfield_type>(
                prev_skipfield + after_skipfield + 1);

    const skipfield_type following_previous = *(
        pointer_cast<skipfield_pointer_type>(it.element_pointer + 1));
    const skipfield_type following_next = *(
        pointer_cast<skipfield_pointer_type>(it.element_pointer + 1) + 1);

    if (following_previous !=
        std::numeric_limits<skipfield_type>::max())
    {
        edit_free_list_next(
            to_aligned_pointer(it.group_pointer->elements) +
                following_previous,
            following_next);
    }

    if (following_next != std::numeric_limits<skipfield_type>::max())
    {
        edit_free_list_prev(
            to_aligned_pointer(it.group_pointer->elements) + following_next,
            following_previous);
    }
    else
    {
        it.group_pointer->free_list_head = following_previous;
    }
}
```

### 5.3 删除后返回哪个迭代器

`erase(it)` 需要返回删除位置之后的第一个 active element。由于删除前已读取 `after_skipfield`，代码可以直接跳过右侧 skipblock；若刚好到达 group 末尾，则进入下一个 group 并跳过它开头的 skipblock：

```cpp
iterator return_iterator(
    it.group_pointer,
    it.element_pointer + after_skipfield + 1,
    it.skipfield_pointer + after_skipfield + 1);

if (return_iterator.element_pointer ==
        to_aligned_pointer(it.group_pointer->skipfield) &&
    it.group_pointer != end_iterator.group_pointer)
{
    return_iterator.group_pointer = it.group_pointer->next_group;
    const aligned_pointer_type elements =
        to_aligned_pointer(return_iterator.group_pointer->elements);
    const skipfield_pointer_type skipfield =
        return_iterator.group_pointer->skipfield;
    return_iterator.element_pointer = elements + *skipfield;
    return_iterator.skipfield_pointer = skipfield + *skipfield;
}

if (it.element_pointer == begin_iterator.element_pointer)
{
    begin_iterator = return_iterator;
}

return return_iterator;
```

### 5.4 当整个 group 变空

如果 `--group->size == 0`，空 group 不能继续留在 active 链中，否则跨 group 迭代会遇到没有 active element 的 block。源码按位置处理：

| 空 group 的位置 | 处理 |
| --- | --- |
| 唯一 group | 不释放，重置 skipfield 和 free list，复用为容器的空初始块 |
| 首 group | 从 active 链摘除并释放，更新 `begin_iterator` |
| 中间 group | 从 active 链摘除；通常释放，邻近尾 group 的特定情况可进入 unused list |
| 尾 group | 从 active 链摘除并加入 unused list，`end_iterator` 回退到前一个 group 的尾部 |

这解释了为什么删除尾元素可能改变 `end()`，也解释了 `total_capacity` 不一定随 `size()` 下降：尾部空 group 可以保留为 reserved capacity。

### 5.5 范围删除为什么不是循环调用单元素删除

[`erase(first, last)`](https://github.com/mattreecebentley/plf_hive/blob/085899f55591e77d49ed168be4594200aa0f0c3a/plf_hive.h#L2139-L2270) 分成三段：

1. 若起点在首 group 中间，调用 `partially_erase_group()` 处理首段；
2. 对完全落入删除范围的中间 group，直接批量析构并摘链，不逐个维护 skipfield/free list；
3. 对末 group 做部分删除或整体删除。

`partially_erase_group()` 还会把区间内已有的多个 skipblock 从 free list 移除，再一次性生成合并后的 skipblock。长度大于 2 时，中间 skipfield 用 `memset(..., 1, ...)` 置为非零，保证 `get_iterator()` 能识别删除槽。这个批量路径避免了对长范围重复执行四分支合并逻辑。

---

## 6. 插入路径：先复用空洞，再使用尾部，再扩 group

### 6.1 `emplace_implementation()` 的四条路径

单元素插入有四种状态：

| 优先级 | 条件 | 动作 |
| --- | --- | --- |
| 1 | 容器已有删除槽位 | 在 `erasure_groups_head->free_list_head` 指向的 skipblock 首槽原地构造 |
| 2 | 没有删除槽，尾 group 仍有未构造容量 | 在 `end_iterator` 处构造并递增 end |
| 3 | 尾 group 已满 | 优先从 `unused_groups_head` 取预留 group，否则分配新 group |
| 4 | 容器从未初始化 | 以 `min_block_capacity` 初始化首 group，并构造第一个元素 |

源码把“尾部追加”放在 `erasure_groups_head == nullptr` 的分支中，而有空洞时优先复用空洞。这一点意味着 hive 的插入位置未指定：新元素可能出现在迭代序列中间甚至 `begin()` 之前，而不是总在末尾。

### 6.2 空洞复用如何截短 skipblock

下面是 [`emplace_implementation()`](https://github.com/mattreecebentley/plf_hive/blob/085899f55591e77d49ed168be4594200aa0f0c3a/plf_hive.h#L1066-L1204) 中最关键的空洞复用路径：

```cpp
const iterator new_location(
    erasure_groups_head,
    to_aligned_pointer(erasure_groups_head->elements) +
        erasure_groups_head->free_list_head,
    erasure_groups_head->skipfield +
        erasure_groups_head->free_list_head);

const skipfield_type prev_free_list_index = *(
    pointer_cast<skipfield_pointer_type>(new_location.element_pointer));

// 先构造；若构造抛异常，skipfield 和 free list 还没有修改。
construct_element(
    new_location.element_pointer,
    std::forward<arguments>(parameters)...);

const skipfield_type new_value = static_cast<skipfield_type>(
    *new_location.skipfield_pointer - 1);

if (new_value != 0)
{
    // 原 skipblock 长度大于 1：消费首槽，剩余部分从下一槽开始。
    *(new_location.skipfield_pointer + new_value) =
        *(new_location.skipfield_pointer + 1) = new_value;

    ++erasure_groups_head->free_list_head;

    if (prev_free_list_index !=
        std::numeric_limits<skipfield_type>::max())
    {
        edit_free_list_next(
            to_aligned_pointer(new_location.group_pointer->elements) +
                prev_free_list_index,
            erasure_groups_head->free_list_head);
    }

    // 把 free-list node 从旧首槽迁移到下一槽。
    edit_free_list_head(
        new_location.element_pointer + 1,
        prev_free_list_index);
}
else
{
    // 长度为 1：消费后整个 skipblock 消失。
    erasure_groups_head->free_list_head = prev_free_list_index;

    if (prev_free_list_index !=
        std::numeric_limits<skipfield_type>::max())
    {
        edit_free_list_next(
            to_aligned_pointer(new_location.group_pointer->elements) +
                prev_free_list_index,
            std::numeric_limits<skipfield_type>::max());
    }
    else
    {
        // 这个 group 已无任何空洞，从 groups-with-erasures 链移除。
        erasure_groups_head =
            erasure_groups_head->erasures_list_next_group;
    }
}

*new_location.skipfield_pointer = 0;
++new_location.group_pointer->size;

if (new_location.group_pointer == begin_iterator.group_pointer &&
    new_location.element_pointer < begin_iterator.element_pointer)
{
    begin_iterator = new_location;
}

++total_size;
return new_location;
```

这个算法总是消费 skipblock 的**首槽**，而不是任意槽位。原因是首槽同时保存 skipblock 长度和 free-list node；从首部截短只需把 node 向右迁移一格，更新成本稳定。

### 6.3 尾 group 满时如何增长

没有空洞且尾 group 已满时，源码先尝试复用 `unused_groups_head`。如果没有 reserved group，才调用：

```cpp
next_group = allocate_new_group(
    static_cast<skipfield_type>(
        std::min(
            total_size,
            static_cast<size_type>(max_block_capacity))),
    end_iterator.group_pointer);
```

因此新 group 容量为 `min(total_size, max_block_capacity)`。由于第一个 group 至少已有 `min_block_capacity`，后续 group 近似按容器规模增长，直到上限：

```text
示意：8 → 8 → 16 → 32 → 64 → ... → max_block_capacity
```

这不是标准强制的固定 2 倍增长公式，而是当前源码通过“新容量等于当前 size（受最大值限制）”形成的增长效果。`splice`、自定义 block limits 和 reserved groups 会让实际 group 序列偏离这条理想曲线。

### 6.4 插入的异常安全

源码刻意在元素构造成功后才修改 skipfield/free list。空洞复用时，如果 `T` 的构造函数抛异常，原 skipblock 仍完整；新 group 路径中，如果首元素构造失败，会释放刚分配的 group 并回滚 capacity。首次初始化路径失败则调用内部 `reset()` 回到空状态。

这种顺序使单元素 `emplace` 能提供“构造失败无效果”的强保证，而不是留下一个 skipfield 已标活跃、但对象未成功构造的槽位。

---

## 7. 容量管理：active、reserved 与 consolidation

### 7.1 自适应的默认 block limits

默认最小容量来自 [`block_capacity_default_min()`](https://github.com/mattreecebentley/plf_hive/blob/085899f55591e77d49ed168be4594200aa0f0c3a/plf_hive.h#L391-L397)：

```cpp
static constexpr skipfield_type block_capacity_default_min() noexcept
{
    const skipfield_type adaptive_size = static_cast<skipfield_type>(
        ((sizeof(hive) + sizeof(group)) * 2) /
        sizeof(aligned_element_struct));
    const skipfield_type max_block_capacity =
        block_capacity_default_max();
    return std::max(
        static_cast<skipfield_type>(8),
        std::min(adaptive_size, max_block_capacity));
}
```

默认最大容量来自：

```cpp
static constexpr skipfield_type block_capacity_default_max() noexcept
{
    return static_cast<skipfield_type>(
        std::min(
            std::min(
                static_cast<size_t>(
                    std::numeric_limits<skipfield_type>::max()),
                static_cast<size_t>(8192u)),
            max_size_static()));
}
```

可以概括为：

- 最小容量至少为 8；
- 最小容量会考虑 `sizeof(hive) + sizeof(group)` 相对单槽位大小，避免元数据占比过高；
- 最大容量受 8192、skipfield index 上限和 allocator `max_size()` 三者约束；
- 对 8-bit skipfield，最大值通常首先受 255 限制；对 16-bit skipfield，源码主动把默认上限压到 8192。

### 7.2 `reserve()` 分配的是 reserved groups

`reserve(n)` 不会建立一个巨大的连续区，也不会移动 active elements。它计算还差多少容量，再分成若干满足 `[min_block_capacity, max_block_capacity]` 的 group，挂到 `unused_groups_head`。

当余数小于最小 block 容量时，源码先把余数提升到 minimum，再把多出来的差额分摊到后续 max-capacity blocks，避免制造非法小块。它还可能把一个较小的 unused group 合并进新的余数 group，以减少 group 数量、改善后续迭代局部性。

因此：

```text
size()     = active element 数量
capacity() = active groups 容量 + reserved groups 容量
```

`reserve()` 只改变 capacity 和 unused list，不改变 active group 的元素地址。

### 7.3 `trim_capacity()` 与 `shrink_to_fit()` 不等价

- `trim_capacity()` 只释放 reserved groups，不搬迁 active elements，因此元素引用和迭代器保持有效。
- `shrink_to_fit()` 可以通过 consolidation 把元素搬到更紧凑的新 group 中；一旦搬迁，所有元素地址和迭代器都会失效。

这是使用 hive 时很重要的 API 区分：前者回收“完全未使用的块”，后者还可能回收 active groups 内部的碎片。

### 7.4 `reshape()` 什么时候会搬迁元素

`reshape(hive_limits)` 修改允许的 group 容量范围：

1. 如果现有 active group 全都落在新范围内，只需更新 limits，并清理不合规的 reserved groups；
2. 如果某个 active group 不合规，调用 `consolidate(new_min, new_max)`；
3. `consolidate()` 建立临时 hive，`reserve(total_size)`，再 move/copy 所有 active elements，最后 move-assign 回当前对象。

```cpp
void consolidate(
    const skipfield_type new_min,
    const skipfield_type new_max)
{
    hive temp(plf::hive_limits(new_min, new_max));
    temp.reserve(total_size);
    temp.end_iterator.group_pointer->next_group = temp.unused_groups_head;

    if constexpr (
        !std::is_trivially_copyable<element_type>::value &&
        std::is_nothrow_move_constructible<element_type>::value)
    {
        temp.range_fill_unused_groups(
            total_size,
            std::make_move_iterator(begin_iterator),
            0,
            nullptr,
            temp.begin_iterator.group_pointer);
    }
    else
    {
        temp.range_fill_unused_groups(
            total_size,
            begin_iterator,
            0,
            nullptr,
            temp.begin_iterator.group_pointer);
    }

    *this = std::move(temp);
}
```

所以“hive 的元素地址永不变化”只适用于普通插入、删除、reserve、trim 和兼容的 splice；显式压缩或改变 block limits 时不能依赖它。

---

## 8. `splice()`：不搬元素地拼接两条 group 链

`splice()` 的目标是把 source 的 active groups 整体接到 destination，保留 source 元素的物理地址。实现比 `list::splice` 复杂，因为 destination 尾 group 可能还有尚未构造的槽位。

### 8.1 为什么要把尾部未构造槽标成 erased

假设 destination 的 `end_iterator` 位于尾 group 中间。拼接 source 后，这些“原本位于 end 后面”的未构造槽位会变成两个 group 之间的内部区域；如果仍保持 skipfield 为 0，迭代器会把它们误认为 active element。

因此源码把 destination 尾 group 从旧 `end()` 到物理尾部的槽位转成一个 skipblock，并加入 free list：

```text
splice 前：

destination tail: [A A A U U U] end() 在第一个 U
source head:      [A A E A]

splice 后：

destination tail: [A A A E E E] ──► source head: [A A E A]
                              ^
                   U 必须转成可跳过的 E
```

这也解释了 group 构造注释中的特殊情况：通常“尚未构造”与 active slot 一样使用 skipfield 0，因为它们位于 `end()` 之后；只有 splice 会把这种尾部空间显式翻转为 erased 状态。

### 8.2 `splice()` 的源码流程

[`splice()`](https://github.com/mattreecebentley/plf_hive/blob/085899f55591e77d49ed168be4594200aa0f0c3a/plf_hive.h#L3189-L3368) 依次执行：

1. 检查 source active groups 的 capacity 是否符合 destination limits；
2. 比较双方尾 group 的未使用槽位，必要时交换容器，以减少需要转成 skipblock 的尾部空间；
3. 合并双方的 groups-with-erasures 链；
4. 把 destination 尾部未构造槽编码为 skipblock；
5. 拼接 active group 双向链；
6. 必要时重编号 `group_number`；
7. 更新 `end_iterator`、`total_size`、`total_capacity`；
8. source 清空，但 source 自己的 reserved groups 不转移到 destination。

其中真正连接 group 链的部分很短：

```cpp
end_iterator.group_pointer->next_group =
    source.begin_iterator.group_pointer;
source.begin_iterator.group_pointer->previous_group =
    end_iterator.group_pointer;

if (source.begin_iterator.group_pointer->group_number <=
    end_iterator.group_pointer->group_number)
{
    update_subsequent_group_numbers(
        end_iterator.group_pointer->group_number + 1u,
        source.begin_iterator.group_pointer);
}

end_iterator = source.end_iterator;
total_size += source.total_size;
total_capacity += source.total_capacity;
```

元素对象从未 move/copy，因此 source 中元素的地址保持不变；对应迭代器之后属于 destination。不过 `splice` 需要处理 group lists、capacity compatibility 和 group numbering，所以其复杂度与两边 group 数量相关，不是无条件 `O(1)`。

### 8.3 本地 commit 中的 rvalue overload 异常

深入阅读时还需要区分“设计原理”和“这个 commit 的具体代码质量”。本地分析版本 [`085899f` 的 `splice(hive&&)`](https://github.com/mattreecebentley/plf_hive/blob/085899f55591e77d49ed168be4594200aa0f0c3a/plf_hive.h#L3372-L3375) 是：

```cpp
void splice(hive &&source)
{
    splice(std::move(source));
}
```

这里 `std::move(source)` 仍是 xvalue，重载解析会再次选择 `splice(hive&&)`，而不是上面的 `splice(hive&)`，从而形成无终止递归。使用下面的最小调用可以在该 commit 复现进程因递归耗尽资源而终止：

```cpp
plf::hive<int> destination;
plf::hive<int> source;

destination.insert(1);
source.insert(2);
destination.splice(std::move(source)); // 085899f：递归进入 rvalue overload
```

若该 overload 的本意只是转发给已实现完整逻辑的 lvalue overload，调用应当是 `splice(source)`。在上游修复前，使用此源码版本时应调用 `destination.splice(source)`；这里不影响前面对 block 拼接算法的分析，但它说明参考实现也必须锁定 commit、测试具体 API，不能仅凭“参考实现”身份假定每个 overload 都正确。

---

## 9. `sort()`、`unique()` 与 `get_iterator()`

### 9.1 为什么 hive 需要成员 `sort()`

hive iterator 是 bidirectional iterator，不满足 `std::sort` 所需的 random-access iterator。因此容器提供成员 `sort()`。

当前源码按元素大小选择两条路径：

- 当 `T` 可平凡复制或可 move-assign，且 `sizeof(T) <= 2 * sizeof(pointer)` 时，把所有值复制/移动到临时连续数组中排序，再按 hive 的迭代顺序写回。
- 对较大元素，构建 `(element pointer, original index)` tuple 数组，按指针所指的值排序，再按 permutation cycle 原地 move 元素，避免建立一份完整的大对象数组。

第二条路径的核心如下：

```cpp
for (iterator current_element = begin_iterator;
     current_element != end_iterator;
     ++current_element, ++tuple_pointer, ++index)
{
    std::allocator_traits<tuple_allocator_type>::construct(
        tuple_allocator,
        tuple_pointer,
        &*current_element,
        index);
}

std::sort(
    sort_array,
    tuple_pointer,
    sort_dereferencer<comparison_function>(compare));

for (tuple_pointer_type current_tuple = sort_array;
     current_tuple != tuple_pointer;
     ++current_tuple, ++index)
{
    if (current_tuple->original_index != index)
    {
        element_type end_value =
            std::move(*(current_tuple->original_location));
        size_type destination_index = index;
        size_type source_index = current_tuple->original_index;

        do
        {
            *(sort_array[destination_index].original_location) =
                std::move(*(sort_array[source_index].original_location));
            destination_index = source_index;
            source_index = sort_array[destination_index].original_index;
            sort_array[destination_index].original_index = destination_index;
        } while (source_index != index);

        *(sort_array[destination_index].original_location) =
            std::move(end_value);
    }
}
```

重要语义是：地址稳定不代表“该地址上的对象身份不变”。当前实现主要在槽位之间 move-assign 值；标准又允许其他优化策略，所以调用 `sort()` 后不能继续把旧指针理解为“仍指向排序前的同一个逻辑对象”。标准明确允许 `sort()` 使引用、指针和迭代器失效。

### 9.2 `unique()` 只删除连续重复项

`unique()` 的语义与 `std::list::unique()` 类似：只移除迭代顺序中连续的等价元素。因此常见用法是先 `sort()`，再 `unique()`。实现发现连续重复区间后优先调用优化过的 range erase，而不是逐元素删除：

```cpp
if (count != original_count)
{
    current = erase(current, last);
}
else
{
    current = erase(current);
}
```

### 9.3 从元素指针恢复迭代器

`get_iterator(pointer)` 遍历 active groups，判断指针是否落在某个 group 的元素地址范围内，再计算对应 skipfield 位置。当前实现复杂度是 `O(active group count)`，不是 `O(1)`：

```cpp
const skipfield_pointer_type skipfield_pointer =
    current_group->skipfield +
    (aligned_element_pointer -
     to_aligned_pointer(current_group->elements));

return (*skipfield_pointer == 0)
    ? hive_iterator<is_const>(
          current_group,
          aligned_element_pointer,
          skipfield_pointer)
    : end_iterator;
```

标准接口对参数有前置条件：指针必须指向当前 hive 中的有效元素。不要把已删除对象的旧指针交给它来“探测对象是否还活着”；对象生命周期结束后，这种用法本身就不可靠。

---

## 10. 复杂度、局部性与内存开销

### 10.1 关键操作复杂度

| 操作 | 复杂度 | 实现依据 |
| --- | --- | --- |
| 单元素 `emplace` / `insert` | `O(1)` | 直接使用 erasure group + free-list head，或尾部构造/新增 group |
| 单元素 `erase` | `O(1)` | 只检查相邻 skipfield 并做常数次链表更新；空 group 摘链也是常数操作 |
| `operator++` / `operator--` | `O(1)` | jump-counting 一次跨过整个 skipblock |
| 完整遍历 | `O(size + group transitions)` | 每个 active element 一次递增，外加跨 group 的少量工作 |
| 范围 `erase` | 与删除元素数及被移除 group 数线性相关 | 首尾局部处理，中间完整 group 批量移除 |
| `reserve` | 与新分配 reserved groups 数量线性相关 | 不搬 active elements |
| `splice` | 与两边 active groups 数量之和线性相关 | 检查 capacity、合并 erasure list、可能重编号 |
| `get_iterator(pointer)` | `O(active group count)` | 逐 group 判断指针范围 |
| `sort` | `O(N log N)` comparisons | 临时数组排序后写回或 permutation cycle |
| `unique` | `O(N)` predicate applications | 连续扫描，重复区间使用 range erase |

这里的 `O(1)` 不等于相同的常数。一次普通连续槽位 `++` 很接近数组指针递增；跨 skipblock 需要读取 skipfield；跨 group 还要追踪 `next_group`。hive 的优势是把 list 的“每个元素一次 pointer chasing”降为“每个 group 一次”，同时避免 vector 删除时的大规模移动。

### 10.2 skipfield 与槽位开销

以常见 ABI 为例：

| 元素类型 | 典型 `sizeof(T)` / `alignof(T)` | `skipfield_type` | 槽位大小 | 每槽额外 skipfield |
| --- | --- | --- | --- | --- |
| `char` | 1 / 1 | `uint_least8_t` | 至少 2 字节 | 1 字节，另有槽位加宽 |
| `int` | 4 / 4 | `uint_least8_t` | 4 字节 | 1 字节 |
| `double` | 8 / 8 | `uint_least8_t` | 8 字节 | 1 字节 |
| `std::string` | 常见为 32 / 8 | `uint_least16_t` | 常见为 32 字节 | 2 字节 |

以 `plf::hive<int>` 为例：

- `skipfield_type` 通常是 1 字节；
- `aligned_element_struct` 为 4 字节；
- 元素区每槽 4 字节，skipfield 区每槽再加 1 字节；
- 每个 group 还有一个额外 skipfield sentinel、allocation 对齐 padding 和 group metadata。

以常见 64-bit ABI 下的 `plf::hive<std::string>` 为例：

- `sizeof(std::string)` 常见为 32，但这不是标准保证；
- `skipfield_type` 为 2 字节，因为 `sizeof(T) > 10`；
- 单槽元素区常见为 32 字节，skipfield 再增加 2 字节。

与 `std::list<int>` 相比，list node 通常至少需要前后两个指针，再加元素和 allocator padding；hive 把链接开销摊到整个 group，并让每个元素只承担很小的 skipfield 成本。不过不能只比较“1 字节 vs 16 字节”：hive 还存在 group metadata、reserved capacity、删除空洞和对齐开销，应使用真实 workload 的 `size/capacity` 与 profiler 评估。

### 10.3 Cache locality 的准确表述

hive 的 locality 介于 vector/deque 与 list 之间：

```text
vector:  [A A A A A A A A]              全局连续
deque:   [A A A A] -> [A A A A]          分段连续，通常可随机访问
hive:    [A E E A] ⇄ [A A E A]           分段连续，夹杂删除洞
list:    A -> A -> A -> A                 每元素独立节点
```

删除密度越高，hive 在 active elements 之间跨过的物理距离越大，cache 和预取效果会下降；但 jump-counting 避免了对每个洞执行分支。若 workload 会产生大量洞且很少再次插入，可以在允许引用失效的维护窗口使用 `shrink_to_fit()` 或重建容器恢复紧凑性。

---

## 11. 使用边界：什么时候适合，什么时候不适合

### 11.1 适合 hive 的场景

- 对象被频繁创建、删除，同时其他对象持有它们的指针或迭代器；
- 业务主要做全量或大范围遍历，要求比 node-based container 更好的 locality；
- 元素迭代顺序不重要，允许新元素复用任意删除槽；
- 不需要按 index 随机访问；
- 希望把 object pool/bucket array 的常见实现收敛到标准容器接口。

游戏引擎 entity/actor、事件系统、实时仿真、粒子系统都是典型用例。数据库内核中可类比到：

- 执行器内生命周期不同的 operator/runtime state；
- 需要稳定句柄的 in-memory task 或 request 对象池；
- 动态 row/tuple 对象集合，读侧以 scan 为主、写侧频繁回收；
- buffer pool 或资源表中的活跃项管理。

不过 buffer pool 往往还需要固定 frame id、并发 pin/unpin、锁和 NUMA 策略，不能因为 free-list 思想类似就直接用 hive 替代完整的 buffer manager。

### 11.2 不适合 hive 的场景

- 需要全局连续内存、SIMD 批处理、直接交给 C API 的场景：优先 `vector`。
- 需要 `operator[]` 和真正的 random-access iterator：优先 `vector` / `deque`。
- 插入顺序必须严格等于迭代顺序：hive 会优先复用空洞，不满足这一假设。
- key lookup 是主操作：使用合适的 ordered/unordered associative container。
- 需要多个线程无同步地并发修改：`std::hive` 本身不是并发容器。
- 必须把对象 identity 与排序前地址永久绑定：`sort`、`reshape`、`shrink_to_fit` 可能破坏这种绑定。

### 11.3 与稳定句柄系统的区别

hive 保证的是特定操作下的元素地址稳定，不自动解决 ABA 问题：

1. 元素 A 被删除；
2. 旧指针仍保存着 A 的地址，但已经悬空；
3. 插入 B 恰好复用同一槽位；
4. 地址数值相同，不代表它仍是 A。

如果外部引用可能晚于对象生命周期，需要额外使用 generation counter、ID table、slot map 或显式所有权协议。hive 不能仅凭稳定地址替代生命周期管理。

---

## 12. 总结：三套结构、两个方向、一个核心权衡

`plf::hive` 的实现可以压缩成三层协作：

1. **active group 双向链 + 分段连续元素区**：避免整体扩容搬迁，把 pointer chasing 从每元素一次降为每 group 一次；
2. **jump-counting skipfield**：在迭代方向上，以常数时间跨过任意长度的连续删除区；
3. **groups-with-erasures 链 + group 内 free list**：在插入方向上，以常数时间定位并复用一个删除槽位。

围绕这三层，源码又加入：

- 自适应 block limits，平衡小容器浪费和大容器分配次数；
- unused group list，让 `reserve` 和空尾块可以不搬元素地复用；
- 对单元素 erase、range erase、splice、sort 分别设计的专用路径；
- allocator-aware 的构造、销毁与异常回滚；
- `group_number` 和三指针 iterator，换取跨 group 比较与短迭代热路径。

它的核心价值不是“比所有容器都快”，而是在一个明确的权衡点上提供标准化解法：牺牲全局连续性、随机访问和确定插入位置，换取频繁插入/删除下的元素地址稳定与较好的顺序遍历性能。

对于数据库和系统内核工程，最值得借鉴的不只是直接使用 `std::hive`，还有它把同一批删除槽位同时投影成两种索引的思路：skipfield 服务读路径，free list 服务写路径；两者共享状态但各自为热路径优化。

---

## 参考资料

- [P0447R28: Introduction of `std::hive` to the standard library](https://www.open-std.org/jtc1/sc22/wg21/docs/papers/2024/p0447r28.html)
- [C++ working draft: `[hive]`](https://eel.is/c++draft/hive)
- [`plf::hive` repository](https://github.com/mattreecebentley/plf_hive)
- [`plf_hive.h` at commit `085899f`](https://github.com/mattreecebentley/plf_hive/blob/085899f55591e77d49ed168be4594200aa0f0c3a/plf_hive.h)
- [How fast is C++26 `std::hive`?](https://lemire.me/blog/2026/08/02/how-fast-is-c26s-stdhive/)
- [The low-complexity jump-counting pattern](https://archive.org/details/matt_bentley_-_the_low_complexity_jump-counting_pattern)
