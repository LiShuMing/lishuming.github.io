---
title: "StarRocks 内置函数实现原理：从函数解析到向量化执行"
slug: "starrocks内置函数分享"
date: 2023-04-06T00:00:00+08:00
lastmod: 2026-08-09T00:00:00+08:00
categories:
  - 数据库
tags:
  - StarRocks
  - 内置函数
  - 向量化执行
  - 源码分析
description: "结合 StarRocks 源码，系统分析标量函数、聚合函数、窗口函数和表函数从 FE 解析、函数注册到 BE 向量化执行的完整实现链路。"
draft: false
---

## 摘要

内置函数看起来只是 SQL 表达式中的一个局部能力，实际上却横跨了数据库的类型系统、语义分析、执行计划、向量化数据模型、分布式聚合和 Pipeline 执行框架。一个函数能否正确工作，不只取决于“计算公式是否正确”，还取决于以下契约是否一致：

- FE 能否根据函数名和参数类型找到唯一、合法的函数签名；
- FE 与 BE 是否对函数 ID、参数类型、返回类型和中间状态达成一致；
- 实现能否正确处理普通列、常量列、Nullable 列、空批次和复杂类型；
- 有状态函数能否在并行执行、分布式 Shuffle 和多阶段聚合中正确合并；
- 函数申请的资源能否在 Fragment 和执行线程两个作用域内安全初始化与释放；
- 实现是否保持 SQL 所要求的行数关系、NULL 语义和错误行为。

本文以本地 StarRocks 源码提交 [0fd27fd409f3a1ad8a4634d30baf8f22f48254f1](https://github.com/StarRocks/starrocks/tree/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1) 为分析基线，从四类函数的共同基础出发，逐步还原它们从 FE 到 BE 的完整调用链，并给出新增函数时需要关注的工程要点。

> 本文讨论的是实现模型，而不是某个版本的函数清单。函数数量、UDF 能力和源码目录可能继续演进，因此阅读其他版本源码时应以对应分支为准。

## 一、先建立统一认识：函数的四种执行模型

从 SQL 语义和输入、输出的行数关系看，StarRocks 中最常见的函数可以分为四类。

| 函数类型 | 典型函数 | 输入与输出的关系 | 是否维护状态 | 主要执行载体 |
| --- | --- | --- | --- | --- |
| 标量函数（Scalar Function） | `abs`、`lower`、`json_query` | 一行输入对应一行输出 | 通常无状态，也可维护只读辅助状态 | 表达式树 |
| 聚合函数（Aggregate Function） | `sum`、`avg`、`count` | 多行输入归约为一行或每组一行 | 是 | 聚合算子 |
| 窗口函数（Window Function） | `row_number`、`rank`、`lead` | 通常保持输入行数，每行产生一个结果 | 是 | Analytic/Window 算子 |
| 表函数（Table Function） | `unnest`、`json_each`、`generate_series` | 一行输入可展开为零行、一行或多行 | 可选 | Table Function 算子 |

原演讲稿曾提出一个问题：为什么窗口函数经常没有被单独放进“输入到输出”的三分法中？

原因在于，这里混合了两个分类维度：

1. 从 **SQL 语义** 看，窗口函数当然是一类独立函数。它不会像普通聚合那样减少结果集行数，而是在分区、排序和窗口范围上为每一行计算结果。
2. 从 **执行实现** 看，窗口函数和聚合函数都需要维护状态，因此 StarRocks 复用了 `AggregateFunction` 的部分状态接口，再由 Analytic 算子负责 Partition、Peer Group 和 Frame 的推进。

所以更准确的结论是：**窗口函数在语义上独立，在底层状态抽象上与聚合函数共享基础设施。**

UDF 则是另一条分类轴。Scalar、Aggregate、Window 和 Table 描述的是函数的执行语义；Built-in 与 UDF 描述的是函数由系统静态内置，还是由用户动态扩展。二者不能混为一谈。

## 二、贯穿所有函数的三层契约

无论函数属于哪一种执行模型，它都必须跨越 FE、Thrift 和 BE 三层。可以把总体链路概括为：

~~~text
SQL 文本
   |
   v
FE Parser -> FunctionCallExpr -> ExpressionAnalyzer
   |                              |
   |                              +-- 参数类型推导、隐式转换、重载选择
   |                              +-- 从 FunctionSet 查找函数签名
   v
TExpr / TFunction / TTypeDesc
   |
   v
BE 表达式或算子
   |
   +-- Scalar  -> VectorizedFunctionCallExpr -> ScalarFunction
   +-- Aggregate -> AggregateFunction + Aggregate Operator
   +-- Window  -> AggregateFunction/WindowFunction + Analytic Operator
   +-- Table   -> TableFunction + TableFunctionOperator
   |
   v
Column / Chunk
~~~

这条链路中存在三个必须同时成立的契约。

### 2.1 类型契约：同一个 SQL 类型在三层中的表示

| 层次 | 主要表示 | 作用 |
| --- | --- | --- |
| FE | `Type`、`ScalarType`、`ArrayType`、`MapType`、`StructType` 等 | SQL 语义检查、重载匹配、隐式转换和返回类型推导 |
| Thrift | `TTypeDesc`、`TTypeNode`、`TPrimitiveType` | 在 FE 与 BE 之间序列化类型信息 |
| BE | `TypeDescriptor`、`LogicalType`、`RunTimeTypeTraits` 等 | 选择物理 Column、模板实例和执行路径 |

FE 的类型系统位于 [fe/fe-type/src/main/java/com/starrocks/type](https://github.com/StarRocks/starrocks/tree/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/fe/fe-type/src/main/java/com/starrocks/type)，跨进程描述定义在 [gensrc/thrift/Types.thrift](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/gensrc/thrift/Types.thrift)，BE 的逻辑类型入口则是 [be/src/types/logical_type.h](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/be/src/types/logical_type.h)。

函数开发中常见的一类错误，是只在 BE 增加了模板实例，却没有同步 FE 签名；或者 FE 声明的返回类型与 BE 实际返回的 Column 不一致。这类问题未必在编译阶段暴露，可能直到计划下发、表达式初始化甚至查询运行时才失败。

### 2.2 数据契约：函数处理的是 Column，而不是单个值

StarRocks 的 BE 采用向量化执行。算子以 `Chunk` 为批次传递数据，`Chunk` 中的每个字段由一个 `Column` 表示。标量函数的核心签名不是传统意义上的：

~~~cpp
Value function(Value input);
~~~

而是：

~~~cpp
StatusOr<ColumnPtr> function(FunctionContext* context, const Columns& columns);
~~~

也就是说，函数一次处理一批值。常见 Column 类型包括：

| Column | 物理含义 | 函数实现需要关注的内容 |
| --- | --- | --- |
| `FixedLengthColumn<T>` | 定长数值、日期等连续数组 | 模板类型与 LogicalType 是否匹配 |
| `BinaryColumn` | 字符串或二进制数据 | offsets、bytes 生命周期和追加成本 |
| `NullableColumn` | 数据列加一份 NULL 位图 | NULL 传播、结果位图和数据列行数一致性 |
| `ConstColumn` | 一个物理值表示一批相同的逻辑值 | 不能假设底层数据列与逻辑行数相同 |
| `ArrayColumn`、`MapColumn`、`StructColumn` | 嵌套列 | 子列、offsets 和多层 Nullable 的一致性 |
| `JsonColumn`、`VariantColumn` | 半结构化数据 | 类型分派、解析成本和异常路径 |

相关实现可以从 [be/src/column/column.h](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/be/src/column/column.h) 和 [be/src/column/chunk.h](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/be/src/column/chunk.h) 开始阅读。

`ConstColumn` 与 `NullableColumn` 尤其容易被低估：

~~~text
ConstColumn
  logical size = N
  data column   = [value]

NullableColumn
  data column = [v0, v1, v2, ...]
  null bitmap = [ 0,  1,  0, ...]
~~~

函数不能直接把所有输入都强制转换成普通数据列。更稳妥的实现方式是使用 `ColumnViewer`、`ColumnBuilder`、`ColumnHelper` 或项目中已有的函数模板，将常量展开、NULL 检查和结果构造交给统一辅助设施。对于复杂类型，还要继续处理子列自身的 Nullable 和 offsets。

因此，一个标量函数至少应验证以下输入组合：

- 普通列；
- 常量列；
- Nullable 列；
- 常量 Nullable 列；
- 全 NULL；
- 空 Chunk；
- 边界值、非法值和超长输入；
- 如果支持复杂类型，还包括空数组、NULL 元素和嵌套 Nullable。

### 2.3 身份契约：FE 与 BE 必须找到同一个函数

FE 完成重载选择后，会把函数签名及函数标识序列化进执行计划。BE 收到计划后，再根据标识找到真正的执行实现。函数 ID 因而不是普通的内部序号，而是连接 FE 签名与 BE 实现的稳定身份。

如果函数名、参数类型和返回类型都正确，但 FE 与 BE 使用了不同的 ID，BE 仍会报“找不到函数实现”。反过来，重复使用一个 ID 也会导致生成阶段或运行时映射冲突。

## 三、标量函数：从 SQL 调用到向量化计算

标量函数是理解内置函数机制的最佳入口，因为它完整展示了函数解析、代码生成、执行生命周期和 Column 计算模型。

### 3.1 FE：函数解析与重载选择

以 `abs(col)` 为例，FE 大致经历以下过程：

1. Parser 构造 [FunctionCallExpr](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/fe/fe-core/src/main/java/com/starrocks/sql/ast/expression/FunctionCallExpr.java)。
2. [ExpressionAnalyzer](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/fe/fe-core/src/main/java/com/starrocks/sql/analyzer/ExpressionAnalyzer.java) 先分析子表达式，得到实际参数类型。
3. FE 在 `FunctionSet` 中按函数名、参数个数和参数类型查找候选签名。
4. 分析器结合精确匹配、隐式类型转换、可变参数、多态类型和 Decimal 特殊规则，确定最终重载。
5. 确定返回类型，并把函数信息写入 Thrift 表达式树。

`abs` 在注册表中有 DOUBLE、FLOAT、整数和多种 Decimal 重载。这里的重点是：BE 不负责重新猜测应该调用哪个 `abs`，重载决议主要在 FE 已经完成；BE 按计划中确定的函数描述执行。

### 3.2 一份元数据生成 FE 与 BE 注册表

普通向量化标量函数主要登记在 [gensrc/script/functions.py](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/gensrc/script/functions.py) 中。每条记录包含：

~~~text
function_id
function_name
exception_safe
check_overflow
return_type
argument_types
backend_symbol
[prepare_symbol]
[close_symbol]
~~~

例如，无参数函数 `pi()` 的登记项是：

~~~python
[10010, "pi", True, False, "DOUBLE", [], "MathFunctions::pi"]
~~~

[gen_functions.py](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/gensrc/script/gen_functions.py) 会读取这份元数据并生成两侧代码：

~~~text
functions.py
   |
   +-- FE: VectorizedBuiltinFunctions.java
   |       注册函数名、参数类型、返回类型和函数 ID
   |
   +-- BE: generated BuiltinFunctions registry
           将函数 ID 映射到 C++ 函数及 prepare/close 回调
~~~

FE 在 [FunctionSet.java](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/fe/fe-core/src/main/java/com/starrocks/catalog/FunctionSet.java) 中调用 `VectorizedBuiltinFunctions.initBuiltins(this)`。BE 则通过 [builtin_functions.h](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/be/src/exprs/builtin_functions.h) 中的 `find_builtin_function(fid)` 查询生成的注册表。

这种设计消除了手写两份标量函数签名的重复劳动，但它没有消除一致性要求：函数元数据、C++ 声明、C++ 实现和测试仍必须同时更新，生成文件本身不应直接修改。

### 3.3 BE：`VectorizedFunctionCallExpr` 的执行生命周期

BE 的入口是 [function_call_expr.cpp](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/be/src/exprs/function_call_expr.cpp) 中的 `VectorizedFunctionCallExpr`。它的核心流程可以简化为：

~~~text
prepare
  |
  +-- 准备子表达式
  +-- 根据 fid 查找 FunctionDescriptor
  +-- 创建 FunctionContext，记录参数与返回类型

open
  |
  +-- 计算并保存常量参数
  +-- 调用 FRAGMENT_LOCAL prepare
  +-- 调用 THREAD_LOCAL prepare

evaluate_checked
  |
  +-- 批量计算所有子表达式，得到 Columns
  +-- 调用 scalar_function(context, columns)
  +-- 检查 Status、容量限制和返回列结构
  +-- 调整无参数常量函数的逻辑行数

close
  |
  +-- 调用 THREAD_LOCAL close
  +-- 调用 FRAGMENT_LOCAL close
~~~

其中，`exception_safe` 决定调用实现时是否需要启用异常捕获保护，`check_overflow` 决定是否对返回 Column 做容量上限检查。这两个字段不是文档标签，而会影响真实的执行路径。

### 3.4 `FunctionContext`：函数状态与资源的边界

[function_context.h](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/be/src/exprs/function_context.h) 为函数提供类型信息、常量参数、错误与告警、内存管理，以及可选的函数状态。

| 状态作用域 | 生命周期与共享范围 | 适合保存的内容 |
| --- | --- | --- |
| `FRAGMENT_LOCAL` | Fragment 级别，同一 Fragment 的执行上下文可共享 | 由常量参数构造且可安全共享的只读对象 |
| `THREAD_LOCAL` | 每个执行线程或 Driver 私有 | 非线程安全的解析器、临时缓冲区、线程私有状态 |

例如，正则表达式、格式字符串或 JSON Path 如果是常量参数，可以在 `prepare` 中预编译，避免每一行重复解析。实现时需要明确对象是否线程安全：把可变对象放入 Fragment 共享状态，可能产生数据竞争；把大对象放入每个 Thread 状态，又可能放大内存消耗。

### 3.5 最小源码案例：`pi()`

`pi()` 的 C++ 实现位于 [math_functions.cpp](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/be/src/exprs/math_functions.cpp)：

~~~cpp
StatusOr<ColumnPtr> MathFunctions::pi(
        FunctionContext* context,
        const Columns& columns) {
    return ColumnHelper::create_const_column<TYPE_DOUBLE>(M_PI, 1);
}
~~~

这个实现虽然很短，却完整经过了以下链路：

~~~text
SQL pi()
  -> FE 在生成的签名表中解析到 fid=10010
  -> TFunction 将 fid 下发到 BE
  -> VectorizedFunctionCallExpr 按 fid 查找 MathFunctions::pi
  -> 函数返回 ConstColumn<double>
  -> 表达式框架将无参数常量结果调整为当前 Chunk 的逻辑行数
~~~

对应单测 [function_call_expr_test.cpp](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/be/test/exprs/function_call_expr_test.cpp) 直接构造 `TFunction`，设置 `fid=10010`，再经过表达式的 `prepare/open/evaluate/close` 生命周期，验证结果为常量列且值为 `M_PI`。

这个案例说明：测试一个内置函数，不能只测试计算公式，还应尽量覆盖真实表达式生命周期和注册映射。

## 四、聚合函数：可并行、可分布式合并的状态机

聚合函数的本质不是“循环调用一个累加函数”，而是定义一套可以在并行执行和分布式阶段之间迁移、合并的状态协议。

### 4.1 状态生命周期

以 `sum` 为例，单机直觉是把每一行加到同一个变量中；在分布式执行中，更接近下面的过程：

~~~text
输入 Chunk
   |
   +-- Driver 1: create -> update -> partial state A
   +-- Driver 2: create -> update -> partial state B
   +-- Driver 3: create -> update -> partial state C
                              |
                              v
                     serialize / shuffle
                              |
                              v
                     merge(A, B, C)
                              |
                              v
                          finalize
                              |
                              v
                          SQL 结果
~~~

[aggregate.h](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/be/src/exprs/agg/aggregate.h) 中的 `AggregateFunction` 抽象包含以下关键能力：

| 接口 | 作用 |
| --- | --- |
| `create` / `destroy` | 构造和销毁聚合状态 |
| `reset` | 将已有状态恢复为初始状态 |
| `update` | 把原始输入行更新到状态中 |
| `serialize_to_column` | 将本地状态编码为可传输的中间列 |
| `merge` | 把序列化的中间状态合并到当前状态 |
| `finalize_to_column` | 将状态转换为最终 SQL 结果 |
| `convert_to_serialize_format` | 将原始输入批量转换为中间格式，支持某些预聚合策略 |

`AggregateFunctionStateHelper<State>` 使用 placement new 在框架提供的状态内存中构造具体 `State`，并负责析构。这意味着聚合状态不是一个随意分配的普通对象：它的大小、对齐、构造、析构和序列化格式都是执行框架契约的一部分。

### 4.2 Intermediate Type 是分布式执行协议

聚合函数通常涉及三类类型：

~~~text
Input Type -> Intermediate Type -> Result Type
~~~

例如，`avg` 的状态至少要同时保存 sum 和 count，Intermediate Type 就不能简单等同于最终返回的 DOUBLE。对于 HLL、Bitmap、Percentile 等函数，中间状态还可能是专用对象或二进制编码。

Intermediate Type 的重要性在于：它会跨越本地聚合、Exchange、网络传输和最终合并阶段。FE 声明的中间类型、BE 实际序列化的 Column 和 `merge` 期待读取的布局必须完全一致。这里发生的不一致，比普通标量函数的返回类型错误更危险，因为它可能只在特定执行计划或数据规模下出现。

### 4.3 注册方式：FE 签名与 BE Resolver

聚合函数没有直接复用普通标量函数的 `functions.py` 注册流程。

- FE 在 [FunctionSet.java](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/fe/fe-core/src/main/java/com/starrocks/catalog/FunctionSet.java) 中注册聚合函数签名，描述函数名、输入类型、返回类型和中间类型。
- BE 的 [aggregate_factory.cpp](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/be/src/exprs/agg/factory/aggregate_factory.cpp) 构造 `AggregateFuncResolver`，再按 sum/count、avg、min/max、distinct、variance、window 等类别加载实现映射。
- [aggregate_resolver.hpp](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/be/src/exprs/agg/factory/aggregate_resolver.hpp) 提供普通、not-null 和 variadic 等注册辅助接口。

因此，新增聚合函数时必须同时检查两侧：FE 决定“SQL 是否能解析以及计划中使用什么类型”，BE 决定“具体状态机由哪一个模板实例执行”。

### 4.4 为什么 `convert_to_serialize_format` 值得关注

当聚合基数很高、局部预聚合收益较低时，执行引擎可能不希望为每个 key 都创建复杂状态。此时可以把原始输入转换为可供后续阶段合并的中间表示，再由下游继续处理。

`convert_to_serialize_format` 因此不是 `serialize_to_column` 的简单批量版本：

- `serialize_to_column` 面向已经构造并更新过的聚合状态；
- `convert_to_serialize_format` 面向原始输入列，直接生成中间格式。

理解这一区别，有助于分析高基数聚合、两阶段聚合和预聚合自适应策略。

## 五、窗口函数：复用状态接口，但不改变行数

窗口函数的调用形式是：

~~~sql
function(args) OVER (
    PARTITION BY ...
    ORDER BY ...
    ROWS | RANGE BETWEEN ... AND ...
)
~~~

可以用下面的 SQL 观察滑动窗口：

~~~sql
SELECT
    uid,
    category,
    event_date,
    price,
    SUM(price) OVER (
        PARTITION BY uid, category
        ORDER BY event_date
        ROWS BETWEEN 2 PRECEDING AND CURRENT ROW
    ) AS rolling_sum
FROM orders;
~~~

它包含三个不能混淆的概念：

| 概念 | 定义 | 在示例中的含义 |
| --- | --- | --- |
| Partition | `PARTITION BY` 划分的独立计算集合 | 相同 uid、category 的行 |
| Peer Group | `ORDER BY` 值相同的一组同级行 | event_date 相同的行 |
| Frame | 当前结果行实际可见的窗口边界 | 当前行及其前两行 |

`ROWS` 按物理行位置计算边界；`RANGE` 按排序键的值域和 Peer Group 语义计算边界。缺省 Frame 还会受到是否存在 `ORDER BY` 以及具体函数语义的影响，不能笼统理解为固定的“前 N 行”。

### 5.1 为什么窗口函数继承聚合接口

[window.h](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/be/src/exprs/agg/window.h) 中的 `WindowFunction` 复用了 `AggregateFunctionStateHelper`。这并不意味着窗口函数等同于聚合函数，而是因为二者都需要：

- 创建、重置和销毁状态；
- 将一段输入更新到状态；
- 从状态中产生结果；
- 在窗口移动时复用已有计算。

窗口独有的行为则体现在 [aggregate.h](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/be/src/exprs/agg/aggregate.h) 中的批量 Frame 更新、`get_values`、可移除累计状态和收缩重置等接口，以及 Pipeline 的 [analytic_sink_operator.cpp](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/be/src/exec/pipeline/analysis/analytic_sink_operator.cpp) 与 [analytic_source_operator.cpp](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/be/src/exec/pipeline/analysis/analytic_source_operator.cpp)。

### 5.2 滑动窗口为什么需要“可移除状态”

假设窗口从 `[0, 2]` 移动到 `[1, 3]`：

~~~text
旧 Frame: row0 row1 row2
新 Frame:      row1 row2 row3
               -row0 +row3
~~~

最直接的实现是清空状态，再扫描 row1、row2、row3。对于支持逆运算或可移除状态的函数，可以只移除 row0，再加入 row3，把每一行的重算成本降为增量更新。

并非所有聚合状态都支持高效移除。例如 sum/count 容易维护，某些 distinct 或复杂近似状态则很难直接撤销。因此执行框架同时保留了累计更新、状态收缩重置和重新计算等路径。分析窗口函数性能时，不能只看 SQL 中 Frame 的宽度，还要看具体函数能否使用增量维护。

### 5.3 普通聚合函数与窗口专用函数

`sum`、`avg` 等普通聚合函数也可以在 `OVER` 子句中使用；`row_number`、`rank`、`lead`、`lag` 等则是窗口专用函数。二者最终都由 Analytic 算子驱动，但状态输入和产出规则不同：

- 普通聚合窗口函数根据 Frame 中的值更新聚合状态；
- `rank`、`dense_rank` 依赖 Peer Group 边界；
- `lead`、`lag` 需要按当前位置访问偏移行；
- `row_number` 主要维护分区内行号。

这也是为什么“窗口函数只是聚合函数加一个 `OVER`”并不准确。

## 六、表函数：一行输入如何展开为多行

表函数改变的是行数关系。以 `unnest([10, 20, 30])` 为例，一行输入会生成三行输出。执行引擎不但要返回函数结果，还要知道每一段结果属于哪一行输入，以便复制左侧普通列。

### 6.1 FE 与 BE 的职责

FE 在 [TableFunction.java](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/fe/fe-core/src/main/java/com/starrocks/catalog/TableFunction.java) 中注册 `unnest`、`json_each`、`subdivide_bitmap`、`generate_series` 等内置表函数签名，并在规划阶段生成 Table Function 节点。

BE 的 [table_function.h](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/be/src/exprs/table_function/table_function.h) 定义以下生命周期：

| 接口 | 作用 |
| --- | --- |
| `init` | 创建具体的 `TableFunctionState` |
| `prepare` | 初始化与 RuntimeState 无关的资源 |
| `open` | 初始化运行期资源 |
| `process` | 消费一批参数列，返回结果列与 offsets |
| `close` | 释放状态和资源 |

具体实现由 [table_function_factory.cpp](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/be/src/exprs/table_function/table_function_factory.cpp) 解析，[table_function_operator.cpp](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/be/src/exec/pipeline/table_function_operator.cpp) 负责在 Pipeline 中执行。

### 6.2 offsets 是输入行与输出行的映射协议

`process` 返回 `pair<Columns, UInt32Column::Ptr>`。第一部分是表函数生成的结果列，第二部分是前缀 offsets。

假设三行输入分别生成 2、0、3 行：

~~~text
input row          0       1          2
generated count    2       0          3
offsets          [ 0,      2,         2,         5 ]

result rows         r0 r1                r2 r3 r4
belongs to input     0  0                 2  2  2
~~~

因此：

~~~text
offsets[i + 1] - offsets[i]
    = 第 i 行输入产生的输出行数
~~~

`TableFunctionOperator` 根据 offsets 复制外部列，并把函数结果拼成新的 Chunk。若一次展开的数据超过目标 Chunk 大小，`TableFunctionState` 中的 `offset`、`processed_rows` 以及 Operator 内部游标会记录消费进度，下次 `pull_chunk` 继续输出。

这套机制同时解决了三个问题：

- 一行展开多行后，如何保持普通列与函数结果对齐；
- 一个输入 Chunk 产生超大结果时，如何限制单个输出 Chunk 的内存；
- Pipeline 的 `push_chunk` / `pull_chunk` 背压模型如何与表函数配合。

需要特别区分：`process` 在这里是表函数的核心逻辑；窗口函数则由 Analytic 算子按 Partition 和 Frame 驱动，两者的输入输出关系并不相同。

## 七、内置函数与 UDF：扩展方式的边界

早期 StarRocks 的 UDF 能力主要围绕 Java 展开，当前分析基线已经包含多种扩展方式：

| 扩展方式 | 当前源码中的定位 | 适用场景 | 主要代价 |
| --- | --- | --- | --- |
| C++ 内置函数 | 编译进 BE，签名在 FE 注册 | 高频、通用、性能敏感的基础能力 | 需要修改源码、完整测试并随版本发布 |
| Java UDF | 支持标量、聚合、窗口和表函数形态 | 复用 Java 生态、动态交付业务逻辑 | JVM 调用、类型转换、对象与内存管理成本 |
| Python UDF | 实验性能力，当前主要为标量函数 | 算法验证、Python 生态集成 | Python 服务和跨进程/序列化开销 |
| SQL UDF | 用 SQL 表达式封装可复用逻辑 | 组合已有函数，降低重复 SQL | 能力受 SQL 表达式和优化器展开规则约束 |

Java UDF 的 BE 实现集中在 [be/src/udf/java](https://github.com/StarRocks/starrocks/tree/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/be/src/udf/java)，Python UDF 位于 [be/src/udf/python](https://github.com/StarRocks/starrocks/tree/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/be/src/udf/python)，SQL UDF 的 FE 表示可从 [SqlFunction.java](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/fe/fe-core/src/main/java/com/starrocks/catalog/SqlFunction.java) 阅读。

选择内置函数还是 UDF，可以按以下顺序判断：

1. 能否直接用已有 SQL 函数组合表达？如果可以，优先使用 SQL 或 SQL UDF。
2. 是否是组织内部、快速迭代的业务逻辑？如果是，优先考虑 UDF。
3. 是否需要成为所有用户可依赖的公共语义，并且位于高频性能路径？这时更适合内置函数。
4. 是否需要优化器识别特殊语义、下推谓词、使用索引或参与常量折叠？仅增加运行时实现通常不够，还可能需要扩展 FE 和优化器规则。

## 八、如何新增一个内置函数

新增函数前，最重要的工作不是写代码，而是先明确语义契约。

### 8.1 先写清楚函数契约

建议至少回答以下问题：

- 函数名、别名和重载分别是什么？
- 参数是否允许隐式转换？是否支持可变参数？
- 返回类型是固定类型，还是由输入类型、精度或 Scale 推导？
- NULL 是传播、忽略、报错，还是参与特定语义？
- 非法输入返回 NULL、返回错误还是截断？
- 常量参数能否在 `prepare` 阶段预处理？
- 函数是否确定性？能否常量折叠？
- 对聚合函数而言，中间状态能否序列化、合并和销毁？
- 对窗口函数而言，是否支持滑动移除？
- 对表函数而言，零行输出和超大展开如何处理？

如果这些问题没有先确定，FE、BE、文档和兼容性测试很容易各自形成不同解释。

### 8.2 新增标量函数

典型流程如下：

1. 在 `be/src/exprs` 下选择合适模块，声明并实现向量化 C++ 函数。
2. 优先复用 `ColumnViewer`、`ColumnBuilder`、`ColumnHelper` 或同类函数模板，处理 Const、Nullable 和类型分派。
3. 在 `gensrc/script/functions.py` 中分配唯一函数 ID，登记所有重载和可选的 prepare/close 回调。
4. 通过构建流程重新生成 FE 与 BE 注册代码，不直接编辑生成文件。
5. 增加 BE 函数测试和表达式生命周期测试；如涉及类型推导，再增加 FE Analyzer 或 Plan 测试。
6. 更新 SQL 文档，明确参数、返回类型、NULL、错误和边界行为。

如果函数有常量配置参数，例如正则表达式或格式字符串，应考虑在 `prepare` 中构建状态，在 `close` 中对称释放，而不是在每一行重复解析。

### 8.3 新增聚合函数

除计算逻辑外，还需要：

1. 定义 State 结构及其初始值；
2. 实现 create、destroy、reset 和 update；
3. 定义稳定的中间表示，实现 serialize 与 merge；
4. 实现 finalize，必要时实现 `convert_to_serialize_format`；
5. 在 FE `FunctionSet` 注册输入、中间和返回类型；
6. 在 BE Aggregate Resolver 注册具体模板实例；
7. 验证单阶段、两阶段、并行合并、空输入、全 NULL 和高基数场景。

聚合测试不能只验证“单个状态连续 update”的结果，还应验证：

~~~text
finalize(update(all rows))
    ==
finalize(merge(
    serialize(update(partition 1)),
    serialize(update(partition 2)),
    ...
))
~~~

这条等价关系是可分布式聚合正确性的核心。

### 8.4 新增窗口函数

需要先判断它属于哪一种：

- 已有聚合函数的窗口用法；
- 依赖 Peer Group 的排名函数；
- 依赖行位置的 offset/value 函数；
- 需要自定义 Frame 状态的新函数。

然后分别验证无 `ORDER BY`、重复排序键、空分区、单行分区、ROWS/RANGE、多种边界以及大分区。若实现可移除累计状态，还应对比增量结果与完整重算结果。

### 8.5 新增表函数

需要同时更新 FE 的 `TableFunction.initBuiltins` 和 BE 的 Table Function Resolver，并重点验证：

- 每行生成 0、1、多行时 offsets 是否正确；
- 多列返回值是否等长；
- LEFT JOIN 语义下零行结果如何补齐；
- 展开结果超过 `chunk_size` 时是否能分批恢复；
- Const、Nullable 和复杂参数列是否正确；
- 外部列复制后是否仍与结果列逐行对齐。

### 8.6 编译、测试与提交

在 StarRocks 仓库根目录可以按改动范围执行：

~~~bash
./build.sh --be
./build.sh --fe
./run-be-ut.sh --gtest_filter 'VectorizedFunctionCallExprTest.*'
~~~

测试名称应替换为新增函数对应的测试套件。函数改动还应尽量补充 SQL 层回归测试，以覆盖 FE 解析、计划下发和 BE 执行的完整链路。

提交开源项目 PR 时，建议保持以下顺序：

1. 先搜索现有 Issue、函数和进行中的 PR，避免重复实现；
2. 对新语义先创建 Issue 或设计讨论，明确与其他数据库的兼容边界；
3. Fork 仓库并在独立分支实现；
4. 在本地完成格式检查、目标编译和相关测试；
5. PR 描述中给出语义、实现路径、测试矩阵和兼容性说明；
6. 根据 CI 与 Review 意见补充测试，而不仅是修到“能够编译”。

StarRocks 社区早期曾通过年度“函数认领任务”集中推进能力建设。对于长期维护的技术文档，更稳妥的参与入口是当前 GitHub Issues、贡献指南和社区讨论，避免依赖已经结束的年度任务。

## 九、阅读源码时最值得追踪的入口

| 主题 | 源码入口 |
| --- | --- |
| FE 函数表达式 | [FunctionCallExpr.java](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/fe/fe-core/src/main/java/com/starrocks/sql/ast/expression/FunctionCallExpr.java) |
| FE 表达式分析 | [ExpressionAnalyzer.java](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/fe/fe-core/src/main/java/com/starrocks/sql/analyzer/ExpressionAnalyzer.java) |
| FE 内置函数集合 | [FunctionSet.java](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/fe/fe-core/src/main/java/com/starrocks/catalog/FunctionSet.java) |
| 标量函数元数据 | [functions.py](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/gensrc/script/functions.py) |
| 注册代码生成器 | [gen_functions.py](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/gensrc/script/gen_functions.py) |
| BE 标量表达式 | [function_call_expr.cpp](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/be/src/exprs/function_call_expr.cpp) |
| 函数上下文 | [function_context.h](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/be/src/exprs/function_context.h) |
| 向量化 Column | [be/src/column](https://github.com/StarRocks/starrocks/tree/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/be/src/column) |
| 聚合函数抽象 | [aggregate.h](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/be/src/exprs/agg/aggregate.h) |
| 聚合函数注册 | [be/src/exprs/agg/factory](https://github.com/StarRocks/starrocks/tree/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/be/src/exprs/agg/factory) |
| 窗口函数实现 | [window.h](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/be/src/exprs/agg/window.h) |
| 表函数抽象 | [table_function.h](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/be/src/exprs/table_function/table_function.h) |
| 表函数 Pipeline 算子 | [table_function_operator.cpp](https://github.com/StarRocks/starrocks/blob/0fd27fd409f3a1ad8a4634d30baf8f22f48254f1/be/src/exec/pipeline/table_function_operator.cpp) |

推荐的阅读顺序不是按目录从头浏览，而是选择一个具体函数做纵向追踪：

~~~text
SQL 文档
  -> FE 函数签名
  -> FE Analyzer 的类型决议
  -> Thrift 中的 TFunction
  -> BE Resolver 或函数 ID
  -> C++ 实现
  -> Column 辅助设施
  -> 单元测试与 SQL 回归测试
~~~

这条路径能够把“函数公式”还原为完整的数据库执行能力。

## 十、总结

StarRocks 内置函数的实现可以归纳为四种不同的核心模型：

- 标量函数是 **基于 Column 的批量表达式计算**；
- 聚合函数是 **可以序列化、合并和终结的分布式状态机**；
- 窗口函数是 **由 Partition、Peer Group 和 Frame 驱动的逐行状态计算**；
- 表函数是 **通过 offsets 维护输入行与展开结果映射的行数扩展算子**。

四者共享 FE 类型分析、Thrift 计划描述和 BE 向量化数据结构，但它们在行数关系、状态生命周期、注册方式和执行算子上有本质差异。

真正高质量的函数实现，需要同时满足语义正确、类型一致、NULL 与常量列安全、状态可管理、分布式结果可合并、内存可控以及测试可验证。理解这些约束后，新增内置函数就不再是“找一个 C++ 文件补一段逻辑”，而是在 StarRocks 的分析与执行体系中加入一份完整、可长期兼容的 SQL 能力。
