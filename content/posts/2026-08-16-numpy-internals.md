---
title: "【LLM】NumPy：从 ndarray 内存模型到科学计算与张量生态"
date: 2026-08-16T00:00:00+08:00
categories:
  - AI Infra
tags:
  - NumPy
  - pandas
  - SciPy
  - PyTorch
  - JAX
  - CuPy
  - Arrow
description: "从内存布局、strides、广播与 dtype 出发理解 NumPy，并比较 pandas、SciPy、PyTorch、JAX、CuPy、Arrow 等主要数组生态。"
draft: false
toc: true
math: true
---

NumPy 是 Python 数值计算生态的地基：pandas、SciPy、scikit-learn 构建在它之上，PyTorch、JAX 的张量语义也直接继承了它。即使日常主要使用 PyTorch、TensorRT 或 vLLM，理解 NumPy 仍然是在理解数值计算世界的“通用语言”。

NumPy 更重要的地位，是为 Python 数组生态提供了一套共同的心智模型。pandas 在数组之上增加标签、缺失值和异构列；SciPy 增加稀疏结构与科学算法；PyTorch 增加设备、自动微分和神经网络；JAX 增加函数变换、JIT 与多设备执行；CuPy 把大量 NumPy API 搬到 GPU；Arrow 则专注跨语言列式交换。它们共享 shape、dtype、广播和向量化语义，却在可变性、设备、执行时机与内存所有权上做出了不同选择。

因此，我更愿意把 NumPy 看成数组计算世界的“基准实现”：它足够简单，能让每次 view、copy、广播和类型提升都被看见；又足够接近硬件，可以作为理解更复杂张量框架和数据工具的起点。本文不只讲 API，也会沿着这条生态脉络回答：哪些 NumPy 经验可以迁移，哪些相似接口背后其实有不同执行语义。

PyTorch、TensorRT、vLLM 与 CUDA 算子的很多设计哲学，如连续性、广播、步长和 kernel 调度，本质上与 NumPy 的底层原理高度同构：`torch.Tensor.stride()` / `contiguous()` / `view()` 几乎就是 NumPy `strides` / `flags` / `reshape` 的另一套表达；GPU coalesced access 关注的仍是相邻线程能否访问连续地址；PagedAttention 解决的也是逻辑连续视图与物理分页之间的映射问题。

所以这篇文章不以罗列 API 为目标，而是试图回答三个问题：

1. **原理：** `ndarray` 到底是一块什么样的内存？为什么切片和转置通常不拷贝？
2. **接口：** NumPy 的类型系统、数组创建与操作函数，如何围绕这套内存模型设计？
3. **应用：** 用 NumPy 写有限差分求解器和 Transformer Decoder，在工程上说明了什么？

先给出一张贯穿全文的生态定位表：

| 生态 | 在 NumPy 数组模型上增加什么 | 与 NumPy 最关键的差异 |
|---|---|---|
| pandas | 标签索引、异构列、缺失值、表格操作 | 运算会按标签对齐，不只是按位置广播 |
| SciPy | 稀疏数组、优化、积分、信号、统计 | 算法与数据结构更专业，仍大量返回 `ndarray` |
| PyTorch | GPU、自动微分、神经网络与分布式训练 | Tensor 带 device、梯度历史和训练语义 |
| JAX | `jit`、`grad`、`vmap`、XLA 与多设备 | Array 不可变，执行可能编译且异步 |
| CuPy | CUDA 数组、GPU kernel 和 NumPy 兼容 API | 数据位于显存，kernel launch 与传输成本显著 |
| Arrow | 跨语言列式格式、NULL、变长与嵌套类型 | 面向交换的不可变 Buffer，不是通用数值计算引擎 |

---

## NumPy 核心

### 内存模型

参考 NumPy 官方文档：[The N-dimensional array](https://numpy.org/doc/stable/reference/arrays.ndarray.html)。

一个 `ndarray` 可以理解为两部分：一段一维数据缓冲区，以及描述这段缓冲区的元数据。

- **Data Buffer：** 原始内存，固定宽度元素通常紧凑排列。
- **Shape：** 每个维度的长度，例如 `(3, 4)`。
- **Dtype：** 单个元素的类型、字节宽度与解释方式。
- **Strides：** 沿每个维度前进一步，需要跨过多少字节。
- **Offset / data pointer：** 当前视图第一个元素相对底层缓冲区的位置。

例如：

```python
import numpy as np

# arange(stop, dtype) 生成 [0, stop) 的等差序列；reshape(3, 4) 将 12 个元素组织为 3 行 4 列。
# reshape 在 strides 兼容时返回视图，否则可能复制或报错。
a = np.arange(12, dtype=np.int64).reshape(3, 4)

print(a.shape)    # (3, 4)
print(a.strides)  # (32, 8)
```

`int64` 每个元素占 8 字节，因此逻辑下标 `(i, j)` 对应的物理偏移是：

```text
offset(i, j) = i * strides[0] + j * strides[1]
             = i * 32 + j * 8
```

这里没有 Python 对象逐个装箱，也没有哈希查找；定位一个元素只是整数乘加。连续区间上的循环还可以继续被 C 编译器、SIMD 或 BLAS 优化。这就是 NumPy 能接近内存带宽上限的基础。

```text
ndarray object
┌───────────────────────────┐
│ Python Object Header      │
├───────────────────────────┤
│ Array Metadata            │
│   dtype   : float32       │
│   shape   : (B,H,S,D)     │
│   strides : (s0,s1,s2,s3)│
│   data ptr / offset       │
└──────────────┬────────────┘
               │
               ▼
Data Buffer ──► [0.12 | -0.45 | 0.88 | 1.02 | ...]
```

#### 视图与零拷贝

理解 `strides` 后，很多“魔法”就只是修改元数据：

```python
a.T          # shape (4, 3), strides 从 (32, 8) 变为 (8, 32)
a[:, 1]      # shape (3,), strides (32,), 起点偏移 8 字节
a[::2]       # 第 0 维 stride 翻倍
a[::-1]      # 第 0 维使用负 stride
```

这些操作通常返回视图，与原数组共享缓冲区。修改视图可能反映到原数组：

```python
# arange(6) 生成整数 0 到 5；未显式 dtype 时使用平台默认整数类型。
x = np.arange(6)
y = x[1:4]
y[0] = 100
print(x)  # [  0 100   2   3   4   5]
```

需要独立所有权时应显式调用 `copy()`。更重要的是，不要把“API 看起来没复制”直接等同于“整个表达式没有分配”：视图可能零拷贝，但后续加法、乘法、类型转换仍然通常会生成输出数组。

`reshape` 也不是无条件零拷贝。数据布局与目标形状兼容时，它可以只修改元数据；不兼容时可能复制，或者直接报错。判断时可以检查：

```python
print(a.flags["C_CONTIGUOUS"])
print(a.flags["F_CONTIGUOUS"])
# shares_memory(a, b) 判断两个数组是否可能引用同一底层内存，而不是比较数值是否相等。
print(np.shares_memory(a, a.T))
```

`np.lib.stride_tricks.as_strided` 可以直接操纵 `strides`，构造滑动窗口等视图，但它绕过了很多安全检查。生产代码优先使用边界更清晰的 `sliding_window_view`。

#### 连续性与数据搬运

多头注意力经常执行：

```text
(B, S, H, D) -> transpose -> (B, H, S, D)
```

转置后的逻辑维度发生变化，底层数据却没有重排，因此数组往往不再 C-contiguous。NumPy 的通用算子可以按 stride 读取它，但底层矩阵库或 GPU kernel 可能要求连续输入，于是框架会显式或隐式地产生一次 contiguous copy。

这提示我们：**零拷贝不天然等于高性能。** 一个跨大步长访问的视图虽然没有复制，却可能带来更多 cache miss、TLB miss 和无效内存事务；有时先付出一次线性重排，反而能让后续多个算子更快。PyTorch/CuPy 的底层 kernel 同样偏好连续或规则布局；JAX 则可能在编译阶段消除中间转置，也可能为了目标设备生成完全不同的物理布局，因此不能把 NumPy 中的 view 成本机械套用到编译型框架。

NumPy 默认使用 C order，也支持 Fortran order。线性代数例程可能对接 BLAS/LAPACK；如果输入布局与底层库期望不匹配，就可能发生隐式转换。因此性能分析不能只看 Python 代码中有没有 `copy()`，还要结合 `flags`、峰值内存和 profiler 判断真正的物化位置。

#### 生态视角：相似的 Array，不同的执行契约

`ndarray` 是一个驻留在 CPU 内存、可变、支持任意 strides 的稠密数组。其他生态保留了相似接口，却改变了其中至少一个前提：

- pandas 的 Series/DataFrame 增加 Index、缺失值和多种 ExtensionArray，`to_numpy()` 可能因为类型统一或缺失值而复制；
- SciPy sparse 只存非零值和索引，节省稀疏数据的内存，但切片、reshape 与赋值不再像稠密数组一样自由；
- PyTorch Tensor 增加 device 与 autograd；CPU Tensor 可以和 NumPy 共享内存，GPU Tensor 则不能直接变成 NumPy view；
- JAX Array 不可变，`reshape`/`transpose` 的中间副本可能由 XLA 在 `jit` 后消除；
- CuPy ndarray 位于 GPU 显存，API 相似不代表 dtype 转换、越界索引和连续性细节完全一致；
- Arrow Array 由 values、validity bitmap、offsets 等 Buffer 组成，优先保证跨语言交换与不可变共享。

这也是阅读“NumPy-compatible”时最重要的判断：兼容的往往是表层 API 和数学语义，不一定包括可变性、同步行为、异常、内存位置和零拷贝条件。NumPy 官方的[互操作指南](https://numpy.org/doc/stable/user/basics.interoperability.html)把 buffer protocol、`__array__`、Array API 和 DLPack 串成了更完整的边界。

### 广播机制

如果说 `strides` 解释了“一个数组如何拥有不同视图”，那么 broadcasting 解释的就是“不同形状的数组如何参加同一个逐元素运算”。官方规则见 [Broadcasting](https://numpy.org/doc/stable/user/basics.broadcasting.html)：从尾部维度向前比较，两维相等或其中一维为 1 时兼容；缺失维度视为 1。

```text
a: (3, 1)          b: (1, 4)          a + b: (3, 4)
┌───┐              ┌───┬───┬───┬───┐  ┌───┬───┬───┬───┐
│ a │              │ b │ b │ b │ b │  │a+b│a+b│a+b│a+b│
├───┤              └───┴───┴───┴───┘  ├───┼───┼───┼───┤
│ a │                                  │a+b│a+b│a+b│a+b│
├───┤                                  ├───┼───┼───┼───┤
│ a │                                  │a+b│a+b│a+b│a+b│
└───┘                                  └───┴───┴───┴───┘
```

所谓“拉伸”是逻辑上的。实现上可以把长度为 1 的维度视为 stride 0，重复读取同一值，而不是先复制成完整数组。

```python
# default_rng(seed) 创建独立的 Generator；固定 seed=42 便于复现实验。
rng = np.random.default_rng(42)
# normal(size=...) 从标准正态分布采样；astype(float32) 把默认 float64 结果转换为 float32。
x = rng.normal(size=(1000, 64)).astype(np.float32)

# mean(axis=0) 沿样本维求每一列均值；keepdims=True 保留为 (1, 64)，便于广播。
mean = x.mean(axis=0, keepdims=True)  # (1, 64)
# std 的 axis/keepdims 语义与 mean 相同，返回每一列的总体标准差（默认 ddof=0）。
std = x.std(axis=0, keepdims=True)    # (1, 64)
x_norm = (x - mean) / std

# arange(4, dtype=float32) 生成 [0,1,2,3]；[:, None] 增加长度为 1 的轴，得到 (4, 1)。
row_weight = np.arange(4, dtype=np.float32)[:, None]  # (4, 1)
```

`keepdims=True` 是广播的最佳搭档：归约后保留长度为 1 的维度，结果就能直接与原数组运算。

广播需要警惕三类问题：

1. **形状正确但语义错误。** `(B, S, D)` 与 `(S,)` 有时能广播，有时直接报错；即使通过，也未必沿着预期维度计算。
2. **广播本身不复制，结果仍要物化。** `(B, H, S, D)` 的乘法输出仍然占完整大小。
3. **巨型中间结果。** 两个小数组可能广播成远大于输入的输出，内存与计算量由结果形状决定。

PyTorch、JAX 和 CuPy 大体沿用 NumPy 从尾部维度匹配的广播规则，所以 shape 推理经验可以迁移。但执行成本不能直接迁移：NumPy/CuPy 的逐个 eager 表达式通常各自产生输出或启动 kernel；JAX 在 `jit` 中可能把多步逐元素计算融合为一个程序。pandas 更特殊——Series/DataFrame 会先按标签对齐，再做逐元素运算；两个 shape 相同但 Index 不同的对象，也可能产生并集后的缺失值。接口看起来都是 `a + b`，真正决定结果的可能是 shape，也可能是 label、device 或编译边界。

### BLAS/LAPACK：NumPy 线性代数的底层引擎

NumPy 的数组遍历、ufunc 与部分归约由自身的 C 实现完成，但矩阵乘、线性方程、特征值和奇异值分解等重型线性代数通常不会从头实现，而是下沉到 BLAS/LAPACK。理解这层边界，才能解释为什么同一段 `A @ B` 在不同机器、不同 NumPy 安装方式下，性能可能相差明显。

```text
Python: np.matmul / np.linalg.solve / np.linalg.svd
                         │
                         ▼
NumPy C 层：校验 shape、dtype、广播、布局，准备输入输出 Buffer
                         │
                         ▼
BLAS/LAPACK 接口：GEMM、GESV、GESDD 等标准例程
                         │
                         ▼
具体实现：OpenBLAS / MKL / Accelerate / BLIS / Netlib ...
                         │
                         ▼
CPU kernel：SIMD、cache blocking、packing 与多线程
```

这里要区分“接口标准”和“实现库”：BLAS/LAPACK 定义函数做什么、参数如何传递；OpenBLAS、Intel MKL、Apple Accelerate 等负责针对具体 CPU、cache 和 SIMD 指令实现这些函数。NumPy wheel 通常已经链接了其中一个后端，用户调用的仍然是同一套 `np.linalg` API。

#### BLAS：把基础线性代数分成三个层级

BLAS（Basic Linear Algebra Subprograms）按运算粒度分为三个 Level：

| 层级 | 典型运算 | 计算量 | 常见瓶颈 |
|---|---|---:|---|
| Level 1 | vector-vector：dot、axpy、norm | \(O(n)\) | 内存带宽与调用开销 |
| Level 2 | matrix-vector：gemv | \(O(n^2)\) | 数据复用有限，通常仍偏带宽受限 |
| Level 3 | matrix-matrix：gemm | \(O(n^3)\) | 数据复用高，最容易利用 SIMD、多核和 cache blocking |

例如：

```python
import numpy as np

# arange(stop, dtype) 创建连续向量；这里显式使用 float64 与后续矩阵 dtype 保持一致。
x = np.arange(4, dtype=np.float64)
# 第二次 arange 使用相同 stop/dtype，得到与 x shape 和类型一致的向量。
y = np.arange(4, dtype=np.float64)
# dot(a, b) 对两个一维数组计算内积，对应 BLAS Level 1 的 dot 语义。
inner = np.dot(x, y)

# array(object, dtype) 创建 2×2 稠密矩阵；二维矩阵默认使用 C order。
A = np.array([[1.0, 2.0], [3.0, 4.0]], dtype=np.float64)
# array 为 B 创建同样的 2×2 float64 稠密矩阵，满足 matmul 的内维匹配要求。
B = np.array([[2.0, 0.0], [1.0, 2.0]], dtype=np.float64)
# matmul(A, B) 对二维输入执行矩阵乘，底层通常进入 BLAS Level 3 GEMM。
C = np.matmul(A, B)
# A @ x 是矩阵-向量乘，数学语义对应 BLAS Level 2 GEMV。
z = A @ x[:2]
```

为什么 GEMM 是高性能计算的核心？朴素矩阵乘中，每加载一块数据可以参与多次乘加；优化实现会先把矩阵分块和 packing，再让小块停留在 cache/寄存器中反复使用。相比之下，向量加法通常是“读两个值、写一个值、只做一次加法”，算术强度低，更容易被内存带宽限制。因此，“用了 BLAS”不等于每个线性代数操作都能达到峰值 FLOPS，问题规模和 BLAS Level 同样重要。

#### LAPACK：用分解组织完整的数值算法

LAPACK（Linear Algebra PACKage）建立在 BLAS 之上，提供线性方程、最小二乘、特征值、SVD 等更完整的算法。以：

```python
# array([1,0], dtype) 创建 float64 右端向量；linalg.solve(a, b) 求解 a @ x = b。
# a 必须是方阵，b 可以是一组或多组右端项。
solution = np.linalg.solve(A, np.array([1.0, 0.0], dtype=np.float64))
```

为例，`float64` 的一般方阵通常进入 LAPACK `dgesv` 家族，其核心不是直接计算逆矩阵，而是：

1. 使用带部分主元选择的 LU 分解，将 \(A\) 写成 \(P A = L U\)；
2. 先解下三角系统 \(L y = P b\)；
3. 再解上三角系统 \(U x = y\)。

例程名前缀也携带 dtype：`s`/`d` 通常表示 `float32`/`float64`，`c`/`z` 表示 `complex64`/`complex128`。NumPy 根据输入 dtype 选择对应路径，所以 dtype 不只决定精度和内存，也决定调用哪个底层 kernel。

| NumPy API | 典型数学路径 | LAPACK 例程家族示例 |
|---|---|---|
| `np.linalg.solve` | LU 分解 + 三角求解 | `*gesv` |
| `np.linalg.qr` | Householder QR | `*geqrf` + `*orgqr/*ungqr` |
| `np.linalg.svd` | 奇异值分解 | `*gesdd` |
| `np.linalg.eigh` | 对称/Hermitian 特征分解 | `*syevd/*heevd` |
| `np.linalg.cholesky` | Cholesky 分解 | `*potrf` |
| `np.linalg.lstsq` | 最小二乘/秩判定 | `*gelsd` 等 |

这也解释了一个重要实践：需要解 \(A x=b\) 时优先使用 `solve(A, b)`，不要先写 `inv(A) @ b`。显式求逆通常需要更多工作和中间结果，也更容易放大数值误差；专业求解器会直接利用分解完成目标任务。

#### Shape、strides 与隐式复制

经典 BLAS/LAPACK 源于 Fortran，天然偏好列主序；现代 C 接口可以通过 leading dimension 和 transpose flag 适配部分 C/F order 布局。但任意切片、负 stride、错位 dtype 或不满足例程约束的数组，仍可能在 NumPy 包装层被复制到临时连续 Buffer。

```python
# array(..., order="C") 创建行主序矩阵。
A_c = np.array([[1.0, 2.0], [3.0, 4.0]], order="C")
# asfortranarray(a) 在必要时复制为列主序；输入已是 F-contiguous 时直接复用。
A_f = np.asfortranarray(A_c)

print(A_c.flags["C_CONTIGUOUS"])  # True
print(A_f.flags["F_CONTIGUOUS"])  # True
```

不要因此机械地把所有输入都转成 Fortran order：转换本身也要读写整块内存，是否值得取决于后续会复用多少次、底层例程是否能直接处理当前布局。更稳妥的做法是结合 `flags`、峰值内存和基准测试，判断复制发生在哪里。

#### 后端、线程与可复现的性能测试

NumPy 构建系统会自动探测 BLAS/LAPACK，常见实现包括 OpenBLAS、MKL、Accelerate、FlexiBLAS、BLIS 和参考 Netlib 实现。可以从当前环境直接检查：

```python
# show_config() 打印 NumPy 构建期依赖，可查看链接的 BLAS/LAPACK 名称与编译信息。
np.show_config()
# show_runtime() 打印运行时 SIMD 与 BLAS 线程后端信息；具体字段随构建方式而异。
np.show_runtime()
```

做性能分析时还要注意：

1. **线程不由 NumPy 单独决定。** OpenBLAS/MKL/Accelerate 可能自行启动线程；如果外层 Python、joblib 或 PyTorch 也并行，容易出现线程过量竞争。
2. **小矩阵不一定受益。** 参数检查、函数分派、线程启动和 packing 都有固定成本，小问题可能由开销主导。
3. **布局与 dtype 必须一致。** 隐式转换和临时连续化可能比计算本身更贵。
4. **基准要固定后端和线程数。** 否则比较的可能是 OpenBLAS 与 MKL、单线程与多线程，而不是两种 NumPy 写法。
5. **批量小矩阵要单独评估。** `np.linalg` 支持在前导维度广播，但大量小矩阵与一个大 GEMM 的硬件利用率完全不同。

在其他生态中，PyTorch CPU 也常链接 BLAS/MKL，CuPy/PyTorch CUDA 通常依赖 cuBLAS/cuSOLVER，JAX/XLA 则可能把线性代数降到目标设备库并与周边算子融合。数学算法可以相近，dispatch、布局、融合和同步模型却不同。因此 NumPy 最适合建立算法与数值基线，跨框架性能结论仍需要在各自执行后端上重新测量。

---

## NumPy API

NumPy 的 API 很多，但可以沿着内存模型划分为四组：

1. **Data Type：** 每个元素如何解释、占多少字节。
2. **Array Creation：** 缓冲区从哪里来、由谁拥有。
3. **Array Manipulation：** 只改元数据，还是搬运数据。
4. **Functions：** 如何在连续或带 stride 的数组上批量执行 kernel。

完整入口可参考 [NumPy User Guide](https://numpy.org/doc/stable/user/)。下面不追求 API 穷举，而是抓住最影响工程判断的部分。

### Data Type

NumPy 的基础 dtype 接近 C 类型系统：同一个数组中的元素通常具有固定宽度，因此可在缓冲区中紧凑排列。

| 类别 | 常见 dtype | 字节数 | 典型用途 |
|---|---:|---:|---|
| 布尔 | `bool_` | 1 | mask；注意并非 bit-packed |
| 有符号整数 | `int8/16/32/64` | 1/2/4/8 | ID、计数、量化值 |
| 无符号整数 | `uint8/16/32/64` | 1/2/4/8 | 字节、位图、图像 |
| 浮点 | `float16/32/64` | 2/4/8 | 数值计算、模型权重 |
| 复数 | `complex64/128` | 8/16 | FFT、波动、RoPE 原型 |
| 时间 | `datetime64/timedelta64` | 8 | 时间点与时间差 |
| 字符串 | `S`、`U`、`StringDType` | 固定或变长 | 文本处理 |
| 对象 | `object_` | 指针宽度 | Python 对象；失去纯连续值语义 |

几个对工程实践影响很大的事实：

- Python `float` 构造数组时通常得到 `float64`；整数默认宽度取决于平台。跨库和跨节点协议应显式声明 dtype。
- NumPy 2.x 按 [NEP 50](https://numpy.org/neps/nep-0050-scalar-promotion.html) 调整了标量类型提升规则。不要把隐式 promotion 当成稳定的业务协议。
- 定宽整数会溢出。聚合、平方和或时间差运算要主动评估中间类型宽度。
- `float64` 精度高但占用两倍于 `float32` 的内存带宽；在许多分析和推理负载里，瓶颈首先是 bandwidth，而不是 FLOPS。
- `arr.nbytes == arr.size * arr.itemsize`，它是核对数组真实数据体积的第一工具。

生态之间的 dtype 也不是无损同构。pandas/Arrow 需要 nullable integer、字符串、时区和 decimal 等更丰富的逻辑类型；PyTorch/CuPy 更关注适合计算设备的数值 dtype；JAX 的类型提升和 64 位配置与 NumPy 并不完全相同。跨库传递数组时，至少要核对 dtype、device、nullable、字节序和写权限，不能只看 shape 与打印值。

#### `complex64` 与 RoPE

`complex64` 在内存中由两个相邻的 `float32` 组成。可以用视图在“实部/虚部交错”与复数之间切换：

```python
import numpy as np

# array(..., dtype=complex64) 从 Python 复数创建数组，每个元素由两个 float32 组成。
z = np.array([1.0 + 2.0j, 3.0 + 4.0j], dtype=np.complex64)

real_view = z.real               # stride 8
imag_view = z.imag               # stride 8，起点多 4 字节
# view(float32) 只重解释同一缓冲区，不做数值转换；每个 complex64 展开为两个 float32。
float_view = z.view(np.float32)  # [1., 2., 3., 4.]
```

RoPE 把相邻两个维度看成二维平面旋转，用复数表达非常自然：

```python
def apply_rotary_emb_complex(
    x: np.ndarray,
    base: float = 10000.0,
) -> np.ndarray:
    """x: (seq_len, head_dim), float32，且 head_dim 为偶数。"""
    # asarray(x, dtype) 尽量复用输入；仅当类型不匹配时转换为 float32。
    x = np.asarray(x, dtype=np.float32)
    seq_len, head_dim = x.shape

    if head_dim % 2 != 0:
        raise ValueError("head_dim must be even")
    if x.strides[-1] != x.itemsize:
        # ascontiguousarray 仅在布局不连续时复制，确保最后一维可按复数对读取。
        x = np.ascontiguousarray(x)

    # arange(start, stop, step, dtype) 取得偶数维索引 0,2,...，每两个实数维度组成一个复数维度。
    dim = np.arange(0, head_dim, 2, dtype=np.float32)
    inv_freq = 1.0 / (base ** (dim / head_dim))
    # arange(seq_len) 生成 token 位置；显式 float32 避免后续提升为 float64。
    positions = np.arange(seq_len, dtype=np.float32)
    # outer(a, b) 计算外积，得到 (seq_len, head_dim/2) 的旋转角矩阵。
    angles = np.outer(positions, inv_freq)
    # exp(1j*angles) 计算单位复数旋转因子；astype(complex64) 压缩到两个 float32。
    factors = np.exp(1j * angles).astype(np.complex64)

    # view(complex64) 将最后一维相邻两个 float32 零拷贝解释成一个复数。
    x_complex = x.view(np.complex64)
    rotated_complex = x_complex * factors
    # view(float32) 再把复数结果零拷贝展开回实数表示，输出 shape 与 x 相同。
    return rotated_complex.view(np.float32)
```

这里要精确区分三件事：

- `x.view(np.complex64)` 只是重解释已有字节，可以零拷贝；
- 复数乘法会创建 `rotated_complex`，不是零分配；
- 如果 `x` 的最后一维不连续，`ascontiguousarray` 会先复制。

这比简单地宣称“RoPE 是零拷贝”更接近真实系统。类型视图节省了重排代码，但算子输出、连续化和缓存仍然需要内存。

### Array Creation

数组创建函数可以按数据来源分成四类。

#### 从 Python 对象创建

```python
np.array([1, 2, 3], dtype=np.int32)  # array(object, dtype)：复制/转换 Python 序列为 int32 数组。
np.asarray(existing_array, dtype=np.float32)  # asarray：类型已匹配时尽量复用，否则转换为 float32。
np.fromiter((i * i for i in range(10)), dtype=np.int64)  # fromiter(iterable, dtype)：从迭代器单遍构造数组。
```

`np.array` 默认倾向创建独立数组；`np.asarray` 在输入已经满足 dtype/layout 时可以直接复用。接口选型本质上是在表达“我是否要求新的所有权”。

#### 按规则生成

```python
np.eye(3)  # eye(N)：创建 3×3 单位矩阵；可用 k 参数选择偏移对角线。
np.diag(np.array([1, 2, 3]))  # array 创建向量；diag(v) 把向量放到主对角线。
np.arange(0, 10, 2)  # arange(start, stop, step)：生成 [0,10) 内步长为 2 的整数。
np.linspace(2.0, 3.0, 5)  # linspace(start, stop, num)：含两端点地生成 5 个等距值。
np.logspace(2.0, 3.0, 5)  # logspace(start, stop, num)：生成 10**2 到 10**3 的 5 个对数等距值。
```

整数索引使用 `arange`，浮点区间采样优先使用 `linspace`，避免浮点步长累积误差和端点歧义。

#### 分配并填充

```python
np.zeros((1024, 64), dtype=np.float32)  # zeros(shape, dtype)：分配并以 0 初始化。
np.ones_like(x)  # ones_like(a)：创建与 x 的 shape、dtype 默认相同且全为 1 的数组。
np.full((4, 4), fill_value=-np.inf, dtype=np.float32)  # full：用 -inf 填满指定 shape。
np.empty((4096,), dtype=np.int64)  # empty：只分配、不初始化；使用前必须完整覆盖。
```

`empty` 只分配、不清零，适合随后必定完整覆盖的输出缓冲；绝不能依赖其中的初始值。PyTorch 和 CuPy 也提供同名接口，但返回 Buffer 所在设备不同；JAX 的函数式语义则更鼓励让编译器规划中间 Buffer，而不是在 Python 层手工复用可变数组。

#### 从缓冲区或文件创建

```python
raw = bytearray(4 * 1024)
# frombuffer(buffer, dtype) 直接把可缓冲对象解释为数组；默认 count=-1 表示读取全部内容。
view = np.frombuffer(raw, dtype=np.float32)

# memmap(filename, dtype, mode, shape) 建立文件映射数组；mode="r" 表示只读且不把全文件载入内存。
mapped = np.memmap(
    "vectors.bin",
    dtype=np.float32,
    mode="r",
    shape=(1000, 128),
)
```

`frombuffer` 与 `memmap` 是 NumPy 接入共享内存、文件格式和其他语言运行时的关键入口。它们可以避免一次用户态复制，但仍要处理生命周期、字节序、对齐与 schema。底层 owner 一旦释放或内容被修改，视图也会受到影响。

在主要生态之间，互操作大致有三条路径：pandas 通过 `to_numpy()`/`np.asarray()` 转换；PyTorch CPU Tensor 可通过 `torch.from_numpy()` 与 ndarray 共享内存；PyTorch、CuPy、JAX 等设备数组可使用 DLPack 交换 Buffer。Arrow 到 NumPy 只有在物理类型兼容、无 NULL、通常还是单一连续 chunk 时才可能零拷贝，而且得到的 view 往往不可写。真正的“零拷贝”不是某一个 API 的标签，而是 dtype、device、布局、所有权和生命周期共同满足约束。

### Array Manipulation

数组操作首先要问：**它只修改元数据，还是必须搬数据？**

#### 视图优先的操作

```python
a.reshape(3, 4)  # reshape(*shape)：改变逻辑形状；布局兼容时返回视图。
a.transpose(1, 0)  # transpose(*axes)：按给定轴顺序置换维度，通常只修改 strides。
a[None, ...]
a.swapaxes(-1, -2)  # swapaxes(axis1, axis2)：交换最后两个轴，负数轴从末尾计数。
a.view(np.float32)  # view(dtype)：重解释底层字节，不执行数值类型转换。
a.ravel()  # ravel(order="C")：拉平成一维并尽量返回视图。
```

这些操作不保证永远零拷贝，但都有机会复用原缓冲区。

#### 明确物化的操作

```python
np.concatenate([x, y], axis=0)  # concatenate：沿已有第 0 轴拼接，其他轴必须兼容；总会物化输出。
np.stack([x, y], axis=0)  # stack：新增第 0 轴后堆叠，输入 shape 必须完全一致。
np.tile(x, (2, 3))  # tile(a, reps)：沿两个维度分别重复 2、3 次，会真实复制数据。
np.repeat(x, 2, axis=0)  # repeat(a, repeats, axis)：把第 0 轴的每个元素重复两次。
np.pad(x, 1, mode="constant")  # pad(a, pad_width, mode)：每个轴两端各补 1 个常量值。
a.flatten()  # flatten(order="C")：拉平成一维且总是返回副本。
```

`concatenate` 是最容易被低估的操作：输出需要一块新的连续缓冲区，再把所有输入复制进去。在循环中不断追加数组会反复搬运历史数据，复杂度可能从线性退化成二次。这个问题在 PyTorch/CuPy 中还会叠加显存带宽与 kernel 调度成本；JAX 则通常要求提前确定 shape，让编译器一次规划结果 Buffer。

#### 索引与 selection

基本切片通常返回视图；整数数组索引和布尔索引通常返回副本：

```python
a[1:10:2]       # basic slicing，通常是 view
a[[0, 2, 5]]    # fancy indexing，通常是 copy
a[a > 0]        # boolean indexing，通常是 copy

a[a < 0] = 0
positive = np.where(a > 0, a, 0)  # where(condition, x, y)：逐元素按条件选择，支持广播。
indices = np.nonzero(a > 0)  # nonzero(condition)：返回每个维度的命中下标数组元组。
```

pandas 的布尔过滤会保留标签语义，NumPy/CuPy/PyTorch 的布尔索引更接近按位置 gather，并通常物化新数组。JAX 还需要面对编译期 shape：`nonzero`、`unique` 等数据相关输出在 `jit` 中往往需要显式给出静态 `size`。因此“同一个过滤表达式”跨生态迁移时，不仅要问 view 还是 copy，也要问结果 shape 能否在编译期确定。

#### 归约与张量表达式

```python
x.sum(axis=0, keepdims=True)  # sum(axis=0)：沿第 0 轴归约；keepdims 保留长度为 1 的轴。
x.argmax(axis=-1)  # argmax(axis=-1)：返回最后一维最大值的位置，而不是最大值本身。
np.cumsum(x, axis=0)  # cumsum(a, axis)：沿第 0 轴计算包含当前位置的累计和。

np.einsum("bij,bjk->bik", A, B)  # einsum：按下标规则执行批量矩阵乘，j 为归约维。
np.einsum("bi,bi->b", x, y)  # 对每个 batch 的 i 维做点积，输出 shape 为 (b,)。
np.einsum("bhsd,bhtd->bhst", q, k)  # 对 d 维缩并，生成注意力分数 (b,h,s,t)。
```

`einsum` 可以表达转置、外积、批量矩阵乘与缩并，但表达能力强不代表一定选择到最优 kernel。NumPy 会调用自身与 BLAS 路径，PyTorch/CuPy 面向 CPU/GPU kernel，JAX/XLA 还可能跨表达式融合和重写。声明式下标只是第一步，最终性能仍取决于 contraction 顺序、布局、设备和中间结果大小。

### Other Functions

NumPy 的 routines 大多是 ufunc 或建立在 ufunc 之上的批量算子。它们也是其他数组库最常复刻的一层 API，但“函数同名”只说明入口相似，不能保证边界行为、异步性和性能模型相同。

#### Bit-wise

位运算适合整数位图、特征开关、二值向量与布隆过滤器原型：

```python
# array(..., dtype=bool) 创建布尔 mask；普通 bool_ 每个元素占 1 字节，并非位压缩。
mask = np.array([True, False, True, True, False, True, False, False])
# packbits(a) 按每 8 个布尔值压成一个 uint8；默认沿扁平化数组打包。
packed = np.packbits(mask)
# unpackbits(a) 将字节展开为 bit；切片去掉最后一个字节中可能存在的补齐位。
restored = np.unpackbits(packed)[: mask.size]

# array(..., dtype=uint8) 明确用单字节无符号整数保存位模式。
a = np.array([0b11010100], dtype=np.uint8)
# 第二个 array 使用相同 dtype，保证 xor 两侧具有一致的字节宽度和位语义。
b = np.array([0b10110111], dtype=np.uint8)
# bitwise_xor 逐位异或；bitwise_count 再统计每个整数中置 1 的 bit 数，即汉明距离。
distance = np.bitwise_count(np.bitwise_xor(a, b))
```

`packbits` 可以把布尔值压缩 8 倍，但 NumPy 普通 `bool_` 数组本身仍是一字节一个值。Arrow BooleanArray 的 values 和 validity 则天然使用 bit-packed Buffer；pandas 的 nullable boolean 还需要同时表达 true、false 与缺失。相同的“布尔数组”，物理表示可能是一字节值、一个位图，或者 values + validity 两个位图。

#### String

NumPy 2.x 提供 `np.strings` 批量字符串函数：

```python
# array 从字符串序列推断字符串 dtype；显式 dtype 可控制固定宽度或使用 StringDType。
names = np.array(["  Alice ", "BOB", "Carol"])
# strings.lower 逐元素转小写；strings.strip 再移除每个字符串两端空白。
clean = np.strings.strip(np.strings.lower(names))
# strings.startswith(a, prefix) 逐元素判断是否以 "a" 开头，返回布尔数组。
starts = np.strings.startswith(clean, "a")
```

字符串也揭示了 ndarray 模型的边界：`U8` 以固定宽度存储，可能浪费空间；`StringDType` 是变长类型，数据不再简单地完全内联在主缓冲区。pandas 的 StringDtype 和 Arrow string 更强调缺失值、offsets + bytes 与跨系统交换；PyTorch/JAX/CuPy 则主要面向数值张量，并不把通用字符串计算作为核心能力。字符串生态的分界远比数值 dtype 明显。

#### Datetime

`datetime64` 与 `timedelta64` 本质上是带单位的 64 位整数：

```python
# arange(start, stop, dtype=datetime64[D]) 按天生成左闭右开的日期序列。
days = np.arange("2026-01-01", "2026-02-01", dtype="datetime64[D]")
weekly = days[::7]
delta = days - days[0]
# astype(datetime64[M]) 把日期转换到月粒度；这不是用固定秒数除法计算月份。
months = days.astype("datetime64[M]")
```

单位决定精度和范围，月份也不是固定秒数。pandas 在 `datetime64` 基础上增加 timezone-aware Timestamp、缺失时间和更完整的时间序列索引；Arrow 则把 timestamp unit 与 timezone 放入逻辑类型元数据。转成纯 NumPy 时，时区信息可能被转为对象，也可能统一到 UTC 后丢弃原时区表达。

#### Logic

```python
np.logical_and(a > 0, a < 10)  # logical_and(x1, x2)：逐元素逻辑与，两个条件可广播。
np.isfinite(scores)  # isfinite：标记既不是 NaN、也不是正负无穷的元素。
np.isnan(scores)  # isnan：逐元素检测 IEEE NaN，返回布尔数组。
np.allclose(actual, expected, rtol=1e-5, atol=1e-7)  # 判断所有元素是否满足绝对/相对误差阈值。
```

浮点近似判断应使用 `isclose/allclose`，而不是直接 `==`。同时要记住 NumPy NaN、pandas `NA`/`NaT`、Arrow validity bitmap 和 Python `None` 是不同的缺失语义；聚合是否跳过缺失值也因 API 而异。跨生态核对结果时，缺失值与容差策略往往比公式本身更容易出错。

#### Random

新代码应使用 Generator API：

```python
# default_rng(seed) 使用推荐的 Generator API；相同 seed 与调用顺序可复现。
rng = np.random.default_rng(42)
# sqrt(64) 给出缩放分母；normal(loc, scale, size) 采样 64×64 权重；astype 转成 float32。
w = rng.normal(0.0, 1.0 / np.sqrt(64), size=(64, 64)).astype(np.float32)
# choice(a, size, replace=False) 从 [0,a) 无放回抽取 128 个整数。
idx = rng.choice(50_257, size=128, replace=False)

# spawn(n) 从当前 Generator 派生 n 个统计独立的子随机流，适合并行 worker。
worker_rngs = rng.spawn(4)
```

NumPy 官方提供 [`Generator.spawn`](https://numpy.org/doc/stable/reference/random/generated/numpy.random.Generator.spawn.html) 为并行任务派生独立随机流。PyTorch 也使用显式 Generator 管理设备侧状态；JAX 则采用函数式 key，调用方必须显式 `split` 并传递子 key，更适合并行变换。只有 seed 还不够：调用顺序、设备、算法实现和子随机流分配都会影响能否复现。

#### Mathematical

```python
np.exp(x)  # exp：逐元素计算 e**x。
np.log1p(x)  # log1p：计算 log(1+x)，x 接近 0 时比直接写 log(1+x) 更稳定。
np.expm1(x)  # expm1：计算 exp(x)-1，小 x 时减少相消误差。
np.clip(x, lo, hi)  # clip(a, min, max)：把值截断到闭区间 [lo, hi]。
np.gradient(x)  # gradient：用有限差分估计梯度；默认样本间距为 1。

np.exp(x, out=x)  # out=x 指定写回原数组，避免额外输出分配；会覆盖旧值。
```

`out=` 允许调用方管理输出 Buffer，减少临时数组。PyTorch/CuPy 的许多数值算子也提供 `out` 或原地版本；JAX Array 不可变，通常由 XLA 在编译后进行 Buffer donation、融合和复用。两种路线的目标相同——控制中间分配——但一个把责任交给调用方，另一个更多交给编译器。

#### Set

```python
# unique(a, return_counts=True) 返回排序后的唯一值以及每个值的出现次数。
values, counts = np.unique(labels, return_counts=True)
# isin(element, test_elements) 逐元素判断 labels 是否属于 allowed_classes。
keep = np.isin(labels, allowed_classes)
clean = X[keep]
```

NumPy 的集合例程通常面向一维稠密数组并返回排序后的结果。pandas 的 Index/Series 集合操作还要保留标签和缺失值语义；JAX 的 `unique` 为了兼容 `jit` 增加了静态 `size` 等约束；CuPy 虽提供相似 API，成本则由 GPU 排序、同步和数据传输共同决定。当集合很大时，算法、设备与输出 shape 比函数名更重要。

#### Sorting

```python
order = np.argsort(scores)  # argsort：返回完整升序排列对应的原始下标。
topk = np.argpartition(scores, -k)[-k:]  # argpartition(kth=-k)：只保证最大的 k 项位于末尾，内部未排序。
bucket = np.searchsorted(edges, values)  # searchsorted：在有序 edges 中二分查找每个 value 的插入位置。
multi_key = np.lexsort((secondary_key, primary_key))  # lexsort(keys)：稳定多键排序，最后一个 key 是主键。
```

Top-K 不必完整排序，`argpartition` 可以先做选择。PyTorch 提供直接返回 values/indices 的 `topk`，JAX 有适合编译路径的 `lax.top_k`，CuPy 则在 GPU 上执行对应选择或排序。比较性能时要同时核对结果是否有序、稳定性、轴语义，以及 GPU 调用是否包含了同步时间。

#### Window

```python
# hanning(M) 生成长度 256 的 Hann 窗，端点趋近于 0，用于减轻频谱泄漏。
w = np.hanning(256)
# fft.rfft(a) 对实数输入做一维 FFT，只返回非负频率的 N/2+1 个复数结果。
spectrum = np.fft.rfft(signal[:256] * w)

# sliding_window_view(x, window_shape=5) 用 strides 创建长度 5 的重叠窗口视图。
windows = np.lib.stride_tricks.sliding_window_view(x, 5)
# mean(axis=1) 对每个窗口归约，得到移动平均；窗口视图本身不复制，结果会分配。
moving_avg = windows.mean(axis=1)
```

`sliding_window_view` 通过 strides 生成重叠视图，不复制窗口内容。但窗口数乘窗口宽度仍决定后续算子的逻辑工作量。pandas rolling 更适合带索引的时间窗口与缺失值处理；SciPy signal 提供卷积、滤波和频谱算法；PyTorch/JAX 则常把滑窗降为 convolution 或编译后的 gather。选择哪一层 API，取决于你需要的是数组视图、时间序列语义，还是设备侧高吞吐 kernel。

---

## NumPy 应用

NumPy 的价值不仅是“能算”，更在于把数学公式快速翻译成可执行、可验证的 reference implementation。它可以生成测试向量、验证 PyTorch/JAX/CuPy 实现、定义边界语义，也能在引入自动微分、JIT 或 GPU 前暴露 shape、dtype 与算法复杂度。

### 一维泊松方程的数值求解

考虑一维泊松方程：

$$
-u''(x) = f(x), \qquad u(0)=u(1)=0
$$

中心差分近似为：

$$
u''(x_i) \approx \frac{u_{i-1} - 2u_i + u_{i+1}}{h^2}
$$

令解析解为 \(u(x)=\sin(\pi x)\)，则 \(f(x)=\pi^2\sin(\pi x)\)。离散后得到三对角线性方程组：

```python
import numpy as np

N = 100
h = 1.0 / N
# linspace(start, stop, num) 在闭区间 [0,1] 生成 N+1 个等距网格点，包含两个边界。
x = np.linspace(0.0, 1.0, N + 1)
x_inner = x[1:-1]
n = N - 1

A = (
    # ones(n) 生成主对角线系数；diag(v, k=0) 把它放到主对角线。
    np.diag(2.0 * np.ones(n))
    # diag(v, k=1) 构造上偏移一格的对角线；ones(n-1) 与其长度匹配。
    + np.diag(-1.0 * np.ones(n - 1), k=1)
    # k=-1 表示下偏移一格的对角线，从而组成三对角矩阵。
    + np.diag(-1.0 * np.ones(n - 1), k=-1)
)

# sin 对 x_inner 逐元素计算正弦；np.pi 提供双精度圆周率常量。
f = (np.pi ** 2) * np.sin(np.pi * x_inner)
b = (h ** 2) * f

# linalg.solve(A, b) 求解 A @ u = b；要求 A 为方阵且与 b 维度兼容。
u_inner = np.linalg.solve(A, b)
# concatenate(seq, axis=0) 把左边界、内部解和右边界拼成完整解，会分配新数组。
u_numerical = np.concatenate(([0.0], u_inner, [0.0]))
# sin 逐元素计算解析解，用于和数值结果核对。
u_exact = np.sin(np.pi * x)

# abs 先计算逐元素绝对误差；max 再归约得到无穷范数误差。
max_error = np.max(np.abs(u_numerical - u_exact))
print(f"N={N}, L_inf error={max_error:.3e}")
```

`np.linalg.solve` 会调用底层 LAPACK `_gesv` 路径，而不是在 Python 中逐行做高斯消元。这个例子有三层价值：

1. **数学可验证。** `N` 增大时可以直接观察中心差分的二阶收敛。
2. **实现透明。** 每段代码都能对应到离散方程。
3. **复杂度暴露。** `np.diag` 构造稠密矩阵需要 \(O(N^2)\) 内存，通用稠密求解约为 \(O(N^3)\)；真正的大规模三对角系统应使用 \(O(N)\) 专用算法或稀疏矩阵库。

这也说明 NumPy 与 SciPy 的典型分工：NumPy 稠密实现便于直接对应公式、验证误差；规模扩大后，应改用 `scipy.sparse.diags` 表达三对角结构，并交给 `scipy.sparse.linalg` 的稀疏求解器。**NumPy reference code 负责定义正确性，SciPy 的专业数据结构与算法负责兑现复杂度。**

### LLM 应用

下面用 NumPy 实现一层简化 Transformer Decoder，覆盖 RMSNorm、RoPE、KV Cache、因果注意力与 SwiGLU。权重是随机的，它的目标不是生成有意义的文本，而是展示 shape、stride、广播和 buffer management 如何共同组成推理过程。

更准确地说，这段代码提取了 [LLaMA 2](https://arxiv.org/abs/2307.09288)、[Qwen](https://arxiv.org/abs/2309.16609) 和 [Mistral 7B](https://arxiv.org/abs/2310.06825) 等 Decoder-only LLM 共享的教学骨架，而不是对任一生产模型的逐参数、逐算子复刻。我们暂时拿掉 tokenizer、embedding、几十层堆叠、LM Head 和采样，把注意力集中在**一个 token 如何穿过一层 Decoder，以及推理状态如何被保存**。

#### 先把单层 Decoder 放回完整 LLM

一个 Decoder-only LLM 的推理主链路可以先压缩成：

```text
文本
  ↓ tokenizer
token ids
  ↓ embedding
隐藏状态 x: (B, S, D)
  ↓ Decoder Layer × N      ← 本节只展开其中一层
最终 RMSNorm
  ↓ LM Head
下一 token 的 logits
  ↓ sampling / argmax
新的 token
```

因此，下面的 `TransformerDecoderLayer.forward` 接收的已经不是 token id，而是 embedding 或上一层输出的隐藏状态 `x`；它输出相同的 `(B, S, D)` shape，以便继续送入下一层。代码中的随机输入和随机权重只能验证数学、shape 与缓存语义，不能生成有意义的语言。

这一层采用现代 LLM 常见的 **Pre-Norm + Residual** 结构：

$$
x_{mid} = x + \operatorname{Attention}(\operatorname{RMSNorm}(x))
$$

$$
x_{out} = x_{mid} + \operatorname{SwiGLU}(\operatorname{RMSNorm}(x_{mid}))
$$

两条残差路径让信息可以绕过注意力和 FFN 直接向后传播；归一化放在子层之前，则让每个子层看到尺度更稳定的输入。这里的 [RMSNorm](https://arxiv.org/abs/1910.07467) 只根据均方根缩放，不像 LayerNorm 那样先减均值：

$$
\operatorname{RMSNorm}(x) = \frac{x}{\sqrt{\operatorname{mean}(x^2)+\epsilon}} \odot w
$$

它的意义不是一句笼统的“更快”，而是在保留重缩放能力的同时省掉 re-centering；实际收益还取决于 dtype、张量大小与是否使用 fused kernel。

#### 一层里究竟发生了什么

可以把 `forward` 理解为两条数据通路：一条负责在 token 之间交换信息，另一条负责对每个 token 独立做非线性变换。

1. **RMSNorm**：在最后一个维度 `D` 上归一化每个 token。
2. **Q/K/V 投影**：把同一个隐藏状态投影为 Query、Key、Value，并把 `D` 拆成 `H × HD`。
3. **RoPE**：把位置信息编码成 Q、K 各二维分量上的旋转。代码用复数乘法直观表达 [RoPE](https://arxiv.org/abs/2104.09864)；生产 kernel 通常直接融合实数域的 `sin/cos` 计算，并不要求真的创建复数 Tensor。
4. **KV Cache**：把本次生成的 K、V 追加到历史缓存；Q 只属于当前查询，不需要跨步保存。
5. **因果注意力**：计算 $\operatorname{softmax}(QK^T/\sqrt{HD})V$，保证 token 只能读取自己及之前的位置。
6. **输出投影与第一条残差**：合并所有 head，映射回 `D` 后与输入相加。
7. **SwiGLU FFN 与第二条残差**：用一条分支产生门控值，另一条分支产生内容，再逐元素相乘并投影回 `D`。这种门控 FFN 来自 [GLU Variants Improve Transformer](https://arxiv.org/abs/2002.05202) 中讨论的 SwiGLU 变体。

用代码中的符号跟踪 shape，比只记公式更容易发现错误：

| 阶段 | shape | 含义 |
|---|---:|---|
| 输入隐藏状态 | `(B, S, D)` | batch、当前输入长度、模型维度 |
| 拆分多头后的 Q/K/V | `(B, H, S, HD)` | `D = H × HD` |
| 累积后的 K/V Cache | `(B, H, T, HD)` | `T` 是包含历史在内的总长度 |
| 注意力分数 | `(B, H, S, T)` | 当前 `S` 个 Query 分别读取 `T` 个位置 |
| 注意力输出 | `(B, H, S, HD)` | 每个 head 聚合自己的 Value |
| 合并多头 | `(B, S, D)` | 回到残差连接要求的 shape |

这里最值得盯住的是 `S` 与 `T`：Prefill 时通常 `S = T > 1`；逐 token Decode 时 `S = 1`，而 `T` 会随生成不断增长。

#### Prefill、Decode 与 KV Cache

同一个 `forward` 在推理中承担两种不同工作负载：

```text
Prefill（一次输入整个 prompt）
S = 5, T = 5     Q @ Kᵀ → (5 × 5)，需要因果 mask

Decode（追加一个新 token）
S = 1, T = 6     Q @ Kᵀ → (1 × 6)，读取已有 K/V
下一步
S = 1, T = 7     Q @ Kᵀ → (1 × 7)，继续复用历史 K/V
```

Prefill 有较大的矩阵乘，通常更容易利用并行算力；Decode 每步只有一个或少量 Query，却要反复读取模型权重和不断增长的 KV Cache，因此更容易受到内存带宽与调度延迟限制。这也是为什么真实推理系统会分别优化 prefill throughput 与 decode latency。

KV Cache 的核心收益是**不再为历史 token 重算 K、V，也不再让历史 token 重新穿过整层网络**。但它不会把全部注意力计算神奇地变成 $O(N)$：固定 hidden size 时，第 $t$ 个新 Query 仍需读取并匹配 $t$ 个历史 Key，因此带 Cache 的自回归注意力累计仍是 $O(N^2)$；没有 Cache、每一步都重跑完整前缀时，注意力部分累计可达到 $O(N^3)$。与此同时，缓存容量会线性增长：

$$
\text{KV bytes} = 2 \times L \times B \times H_{kv} \times T \times HD \times \text{bytes(dtype)}
$$

其中 `2` 代表 K 与 V，`L` 是层数。下面的教学代码只有一层，并令 `H_kv = H`。

#### 它与 LLaMA、Qwen、Mistral 的边界

这些模型共享 RMSNorm、RoPE、门控 FFN、因果自注意力等思想，但具体实现并不相同：

| 能力 | 本节 NumPy 实现 | 生产模型中的常见变化 |
|---|---|---|
| 注意力 head | Q/K/V 都有 `H` 个 head，即标准 MHA | 不少模型使用 GQA/MQA，让多个 Query head 共享较少的 K/V head |
| 注意力窗口 | 读取全部历史位置 | Mistral 7B 的论文重点之一是 GQA 与 Sliding Window Attention |
| RoPE | 固定 base、复数视图实现 | 不同模型可能改变 RoPE base、缩放策略、最大上下文和 kernel 实现 |
| KV Cache | 单层、单 batch、预分配连续数组 | 真实服务还要处理多层、多请求、分页、量化、淘汰与调度 |
| 数值与执行 | CPU `float32`、逐算子 eager 执行 | 常见 BF16/FP16/FP8/INT8/INT4、算子融合、GPU/NPU 与张量并行 |

因此，读这份代码时应把它当成 **reference implementation**：它足够小，可以检查每个数组；又足够完整，可以建立通往真实 LLM 推理引擎的概念地图。Tokenizer、embedding、多层堆叠、最终归一化、LM Head、采样、反向传播、批处理调度和量化并没有在这里实现。

推荐按下面的顺序阅读代码：

1. 先只看 `forward`，找到两次 RMSNorm、两条残差以及 Attention/FFN 两个子层。
2. 沿注释记录每一步 shape，特别检查 `transpose` 后的轴顺序和广播维度。
3. 运行一次 `PREFILL_LEN = 5`，确认输出仍是 `(1, 5, 64)`，Cache 长度变为 5。
4. 再跟踪三次 `S = 1` 的 Decode，观察 Cache 长度递增为 6、7、8，而当前输出始终是 `(1, 1, 64)`。
5. 最后再分别进入 RMSNorm、RoPE、稳定 softmax 与 SwiGLU，理解它们如何由基础 NumPy 操作拼起来。


```python
import numpy as np


def rms_norm(x: np.ndarray, weight: np.ndarray, eps: float = 1e-6) -> np.ndarray:
    # mean(..., axis=-1, keepdims=True) 计算每个 token 最后一维的均方，并保留归约轴用于广播。
    variance = np.mean(x * x, axis=-1, keepdims=True)
    # sqrt 逐元素开方；eps 防止方差为 0 时除零。
    return x * (1.0 / np.sqrt(variance + eps)) * weight


def silu(x: np.ndarray) -> np.ndarray:
    # exp(-x) 逐元素计算指数，组成 SiLU：x / (1 + exp(-x))。
    return x / (1.0 + np.exp(-x))


def precompute_freqs_cis(
    head_dim: int,
    max_seq_len: int,
    base: float = 10000.0,
) -> np.ndarray:
    # arange(start, stop, step, dtype) 取得偶数维索引；两个实数维组成一个复数旋转维。
    dim = np.arange(0, head_dim, 2, dtype=np.float32)
    inv_freq = 1.0 / (base ** (dim / head_dim))
    # arange(max_seq_len) 生成全部 token 位置，显式 float32 控制内存和运算类型。
    positions = np.arange(max_seq_len, dtype=np.float32)
    # outer(positions, inv_freq) 计算外积，输出 (max_seq_len, head_dim/2) 角度矩阵。
    angles = np.outer(positions, inv_freq)
    # exp(1j*angles) 生成单位复数；astype(complex64) 把实部、虚部限制为 float32 精度。
    return np.exp(1j * angles).astype(np.complex64)


def apply_rotary_emb(x: np.ndarray, freqs_cis: np.ndarray) -> np.ndarray:
    """x: (B, H, S, HD), float32，且最后一维连续。"""
    # asarray(x, dtype) 在输入已经是 float32 ndarray 时复用，否则执行类型转换。
    x = np.asarray(x, dtype=np.float32)
    if x.shape[-1] % 2 != 0:
        raise ValueError("head_dim must be even")
    if x.strides[-1] != x.itemsize:
        # ascontiguousarray 只在需要时复制，保证相邻两个 float32 在物理上连续。
        x = np.ascontiguousarray(x)

    # view(complex64) 将最后一维的 float32 两两零拷贝重解释为复数。
    x_complex = x.view(np.complex64)
    factors = freqs_cis[None, None, :, :]
    # 复数乘法完成旋转；view(float32) 再零拷贝展开为原来的实数 shape。
    return (x_complex * factors).view(np.float32)


class KVCache:
    def __init__(
        self,
        batch_size: int,
        num_heads: int,
        max_seq_len: int,
        head_dim: int,
    ) -> None:
        shape = (batch_size, num_heads, max_seq_len, head_dim)
        # empty(shape, dtype) 仅分配 K Cache，不初始化；后续 append 会覆盖有效区间。
        self.k = np.empty(shape, dtype=np.float32)
        # V Cache 与 K Cache 使用相同 shape 和 float32 物理类型。
        self.v = np.empty(shape, dtype=np.float32)
        self.length = 0

    def append(
        self,
        k: np.ndarray,
        v: np.ndarray,
    ) -> tuple[np.ndarray, np.ndarray]:
        step = k.shape[2]
        end = self.length + step
        if end > self.k.shape[2]:
            raise ValueError("KV cache capacity exceeded")

        self.k[:, :, self.length:end, :] = k
        self.v[:, :, self.length:end, :] = v
        self.length = end
        return self.k[:, :, :end, :], self.v[:, :, :end, :]


class TransformerDecoderLayer:
    def __init__(
        self,
        d_model: int,
        num_heads: int,
        d_ff: int,
        max_seq_len: int,
        rng: np.random.Generator,
    ) -> None:
        if d_model % num_heads != 0:
            raise ValueError("d_model must be divisible by num_heads")

        self.d_model = d_model
        self.num_heads = num_heads
        self.head_dim = d_model // num_heads
        self.freqs = precompute_freqs_cis(self.head_dim, max_seq_len)

        # sqrt(d_model) 计算 Xavier 风格缩放；float32(...) 把标量显式收窄，避免类型提升。
        scale = np.float32(1.0 / np.sqrt(d_model))

        def weight(*shape: int, s: np.float32 = scale) -> np.ndarray:
            # normal(loc, scale, size) 按指定 shape 采样权重；astype(float32) 转换默认 float64 输出。
            return rng.normal(0.0, float(s), size=shape).astype(np.float32)

        self.wq = weight(d_model, d_model)
        self.wk = weight(d_model, d_model)
        self.wv = weight(d_model, d_model)
        self.wo = weight(d_model, d_model)

        self.w_gate = weight(d_model, d_ff)
        self.w_up = weight(d_model, d_ff)
        # sqrt(d_ff) 生成 FFN 输出层缩放，float32 保持与权重 dtype 一致。
        self.w_down = weight(d_ff, d_model, s=np.float32(1.0 / np.sqrt(d_ff)))

        # ones(shape, dtype) 初始化 RMSNorm 权重为 1，使初始缩放保持恒等。
        self.attn_norm = np.ones(d_model, dtype=np.float32)
        # FFN 前的 RMSNorm 同样用 ones(d_model, float32) 初始化可学习缩放向量。
        self.ffn_norm = np.ones(d_model, dtype=np.float32)

    def forward(self, x: np.ndarray, cache: KVCache, start_pos: int) -> np.ndarray:
        B, S, D = x.shape
        H, HD = self.num_heads, self.head_dim

        residual = x
        h = rms_norm(x, self.attn_norm)

        # Q：reshape(B,S,H,HD) 拆分 head；transpose(0,2,1,3) 调整为 (B,H,S,HD)。
        q = (h @ self.wq).reshape(B, S, H, HD).transpose(0, 2, 1, 3)
        # K：使用相同的 reshape/transpose 参数，使序列维与 head 维交换位置。
        k = (h @ self.wk).reshape(B, S, H, HD).transpose(0, 2, 1, 3)
        # V：同样返回 (B,H,S,HD)，与后续注意力矩阵乘的维度约定一致。
        v = (h @ self.wv).reshape(B, S, H, HD).transpose(0, 2, 1, 3)

        current_freqs = self.freqs[start_pos : start_pos + S]
        q = apply_rotary_emb(q, current_freqs)
        k = apply_rotary_emb(k, current_freqs)

        keys, values = cache.append(k, v)
        total_s = keys.shape[2]

        # swapaxes(-1,-2) 交换 K 的最后两轴得到 K^T；sqrt(float32(HD)) 计算注意力缩放因子。
        scores = (q @ keys.swapaxes(-1, -2)) / np.sqrt(np.float32(HD))

        if S > 1:
            # full(shape, fill_value, dtype) 创建全为 -inf 的二维 mask。
            mask = np.full((S, total_s), -np.inf, dtype=np.float32)
            # triu(mask, k) 保留第 k 条及以上对角线，其余置 0，形成因果遮罩。
            mask = np.triu(mask, k=total_s - S + 1)
            scores += mask[None, None, :, :]

        # max(axis=-1, keepdims=True) 取每行最大值并保留轴，用于稳定 softmax。
        scores -= np.max(scores, axis=-1, keepdims=True)
        # exp(..., out=scores) 原地指数化，复用 scores 缓冲区。
        np.exp(scores, out=scores)
        # sum(axis=-1, keepdims=True) 计算每行归一化分母，保留轴以便广播除法。
        scores /= np.sum(scores, axis=-1, keepdims=True)

        attn = scores @ values
        # transpose 恢复 (B,S,H,HD)，reshape 再合并 H 与 HD 为模型维 D。
        attn = attn.transpose(0, 2, 1, 3).reshape(B, S, D)
        h = residual + attn @ self.wo

        residual = h
        h = rms_norm(h, self.ffn_norm)
        ffn = (silu(h @ self.w_gate) * (h @ self.w_up)) @ self.w_down
        return residual + ffn


# default_rng(42) 创建可复现、与全局随机状态隔离的 Generator。
rng = np.random.default_rng(42)

B, D, H, FF = 1, 64, 4, 128
MAX_SEQ_LEN = 32
PREFILL_LEN = 5

layer = TransformerDecoderLayer(D, H, FF, MAX_SEQ_LEN, rng)
cache = KVCache(B, H, MAX_SEQ_LEN, D // H)

# normal(size) 生成标准正态 prompt；astype(float32) 与模型权重类型保持一致。
prompt = rng.normal(size=(B, PREFILL_LEN, D)).astype(np.float32)
prefill_out = layer.forward(prompt, cache, start_pos=0)
print("prefill:", prefill_out.shape, "cache_len:", cache.length)

# size=(B,1,D) 模拟单 token decode 输入；astype 避免默认 float64 扩大带宽。
current = rng.normal(size=(B, 1, D)).astype(np.float32)
for step in range(3):
    current = layer.forward(current, cache, start_pos=cache.length)
    print("decode:", step, current.shape, "cache_len:", cache.length)
```

这段代码与前面的原理逐一对应：

- 多头切分通过 `reshape + transpose` 完成，逻辑视图不等于连续布局。
- RoPE 因子从 `(S, D/2)` 广播到 `(B, H, S, D/2)`。
- 因果 mask 从 `(S, Total_S)` 广播到整个 batch 和所有 head。
- softmax 使用 `keepdims=True` 保持可广播形状，并通过 `out=` 原地复用 scores 缓冲区。
- KV Cache 预分配后使用 slice 写入，每个 token 不再复制全部历史。

#### 从 NumPy 原型走向张量框架

这段 Decoder 能运行，但它更适合作为 reference implementation，而不是生产推理引擎：

1. **NumPy 的优势是透明。** eager、同步、CPU 执行让每个 shape、临时数组和数值步骤都容易检查，适合验证 RMSNorm、RoPE、mask 与 KV Cache 语义。
2. **PyTorch 增加训练与设备语义。** `torch.Tensor` 在相似数组接口上增加 autograd、CPU/GPU device、混合精度、模块系统和成熟 kernel；生产模型通常首先需要这些能力。
3. **JAX 增加可组合函数变换。** 同一数学函数可以被 `jit`、`grad`、`vmap` 和多设备变换，但代码必须适应 Array 不可变、静态 shape 与异步 dispatch。
4. **CuPy 是更直接的 GPU 数组迁移路径。** 把 `numpy` 替换为 `cupy` 就能迁移大量数组代码，但真正性能仍取决于 kernel fusion、host-device 传输和同步；它也不自动提供完整训练框架。
5. **框架性能来自执行方式，而不只是 API。** Prefill 偏向大矩阵吞吐，Decode 更受 KV Cache 与内存带宽影响；PyTorch compilation、JAX/XLA、CuPy kernel fusion 都试图减少 Python dispatch、临时 Buffer 和设备往返。

#### NumPy 与主要生态的边界

“像 NumPy”描述的是接口亲缘关系，不代表这些工具可以相互替代：

| 生态 | 最适合解决的问题 | 相比 NumPy 新增的能力与代价 |
|---|---|---|
| NumPy | CPU 稠密数组、数值原型、通用互操作 | eager 且透明；缺少 GPU、自动微分和 JIT |
| pandas | 带标签的表格与时间序列分析 | Index、异构列、缺失值；转换 ndarray 可能复制或统一 dtype |
| SciPy | 科学算法、稀疏线性代数、优化与信号处理 | 专业数据结构/算法；不是 NumPy API 的简单镜像 |
| PyTorch | 深度学习训练与推理 | autograd、device、模块与 kernel；状态和梯度生命周期更复杂 |
| JAX | 可微分科学计算、编译与多设备变换 | 纯函数、不可变 Array、JIT；需要静态 shape 思维并正确测量异步执行 |
| CuPy | 将 NumPy 风格数值代码迁移到 NVIDIA GPU | 高 API 相似度；受显存、传输、kernel launch 与部分边界差异约束 |
| Arrow | NumPy/pandas 与跨语言系统之间的数据交换 | NULL、变长、嵌套和不可变 Buffer；不负责通用数值计算 |

实际选择可以遵循一条简单路径：

- 先用 NumPy 写清数学语义、shape 契约与 reference result；
- 需要标签、缺失值和表格操作时进入 pandas；
- 需要稀疏结构或成熟科学算法时进入 SciPy；
- 需要训练、模型生态和通用 accelerator 支持时进入 PyTorch；
- 需要函数变换、XLA 编译和多设备 SPMD 时考虑 JAX；
- 已有 NumPy 数值代码、目标明确是 NVIDIA GPU 时评估 CuPy；
- 需要跨语言或跨进程传递数据时使用 Arrow/DLPack，并显式核对零拷贝条件。

---

## 总结

NumPy 的核心可以浓缩为三层：

1. 用 **buffer + shape + strides + dtype** 描述多维数组；
2. 用 **broadcasting + ufunc** 把循环下沉到高效内核；
3. 用 **BLAS/LAPACK 与可复用 buffer** 承接工业级计算。

围绕这三层核心，主要生态选择了不同扩展方向：pandas 增加标签和表格语义，SciPy 增加专业算法与稀疏结构，PyTorch 增加自动微分和设备，JAX 增加编译与函数变换，CuPy 增加 CUDA 执行，Arrow 增加跨语言 Buffer 协议。它们底层反复面对的仍是同一组问题：

- 数据在物理上如何排列？
- 什么时候可以只改视图，什么时候必须物化？
- 数据位于 CPU、GPU 还是其他设备？
- 算力和内存带宽谁才是瓶颈？
- 中间结果由谁分配、复用和回收？
- eager、lazy 还是 compiled execution 更适合当前负载？

掌握 NumPy，不只是学会一个 Python 库。它提供了一块足够小、又足够接近真实硬件的实验场：可以从一条 `strides` 出发，一路理解 pandas 的数据转换、SciPy 的稀疏结构、PyTorch/CuPy 的设备 Tensor、JAX 的编译语义，以及 Arrow/DLPack 的 Buffer 互操作。这条可迁移的数组思维，才是 NumPy 最持久的价值。

### 参考资料

- [NumPy: The N-dimensional array](https://numpy.org/doc/stable/reference/arrays.ndarray.html)
- [NumPy: Broadcasting](https://numpy.org/doc/stable/user/basics.broadcasting.html)
- [NumPy User Guide](https://numpy.org/doc/stable/user/)
- [NumPy Random Sampling](https://numpy.org/doc/stable/reference/random/)
- [NumPy: Interoperability with NumPy](https://numpy.org/doc/stable/user/basics.interoperability.html)
- [NumPy: Array API standard compatibility](https://numpy.org/doc/stable/reference/array_api.html)
- [NumPy: BLAS and LAPACK build configuration](https://numpy.org/doc/stable/building/blas_lapack.html)
- [Netlib: BLAS reference](https://netlib.org/blas/)
- [Netlib: LAPACK Users' Guide](https://www.netlib.org/lapack/lug/)
- [pandas: Essential basic functionality and NumPy conversion](https://pandas.pydata.org/docs/user_guide/basics.html)
- [SciPy: Sparse arrays](https://docs.scipy.org/doc/scipy/tutorial/sparse.html)
- [PyTorch: `torch.from_numpy`](https://docs.pytorch.org/docs/stable/generated/torch.from_numpy.html)
- [JAX: `jax.numpy` differences from NumPy](https://docs.jax.dev/en/latest/jax.numpy.html)
- [CuPy: Differences between CuPy and NumPy](https://docs.cupy.dev/en/stable/user_guide/difference.html)
- [Apache Arrow: pandas integration and zero-copy conditions](https://arrow.apache.org/docs/python/pandas.html)
- [LLaMA 2: Open Foundation and Fine-Tuned Chat Models](https://arxiv.org/abs/2307.09288)
- [Qwen Technical Report](https://arxiv.org/abs/2309.16609)
- [Mistral 7B](https://arxiv.org/abs/2310.06825)
- [Root Mean Square Layer Normalization](https://arxiv.org/abs/1910.07467)
- [RoFormer: Enhanced Transformer with Rotary Position Embedding](https://arxiv.org/abs/2104.09864)
- [GLU Variants Improve Transformer](https://arxiv.org/abs/2002.05202)
