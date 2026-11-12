---
title: "【LLM】NumPy：从 ndarray 内存模型到科学计算与张量生态"
date: 2026-08-16T00:00:00+08:00
lastmod: 2026-10-07T00:00:00+08:00
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

在纯 Python 里写一个 `for` 循环把两个长度为百万的 `list` 逐项相加，耗时往往比等效的 C 语言循环慢上 **50 到 100 倍**。很多人把原因笼统归结为“Python 是解释型语言”，但如果顺着内存地址往下看，真正的性能黑洞其实藏在每一个元素的存储与分派方式里。

在 CPython 中，一个包含一百万个浮点数的 `list` 并不是一段紧挨着存放的 8 字节 `double` 数组，而是一排指向堆内存各处的 `PyObject*` 指针。每次执行 `a[i] + b[i]`，解释器都要先后追着两个指针跳去不同的堆内存页（破坏 CPU 缓存空间局部性），读取各自的 `PyTypeObject` 类型头确认双方都是 `float`（动态类型分派），维护引用计数（`ob_refcnt` 增减），再向内存分配器申请一个全新的 24 字节 `PyFloatObject` 把相加结果装箱（Boxing）塞回去。真正花在浮点加法器指令上的时间还不到总耗时的零头。

要想让 Python 跑出逼近硬件内存带宽上限的速度，唯一的出路就是彻底砍掉逐元素的指针装箱与动态检查。NumPy 给出的解法极其洗练——用 **`ndarray`（N-dimensional Array）** 把“物理存储”和“多维逻辑解释”一刀劈开：底层只保留一整块同质、定宽、紧凑排列的一维裸字节缓冲区（Contiguous Raw Buffer），上层则用一份轻量的元数据头（`dtype`、`shape` 与字节步长 `strides`）来描述怎么按偏移量切分和读取这块内存。

吃透这一个核心抽象，原本零散的 NumPy 知识点就会瞬间串成一条严密的因果链：为什么矩阵转置、隔行切片和维度广播能做到零字节拷贝（Zero-Copy `View`），为什么花式索引和非连续重排必定触发物理分配（`Copy`），底层 `ufunc` SIMD 循环与 BLAS/LAPACK 矩阵引擎何时会被跨步视图拖慢，以及当这套数组模型延伸到 pandas、SciPy、PyTorch、JAX、CuPy 和 Apache Arrow 时，哪些契约被继承了下来、哪些前提已经悄然改写。

---

## 一、核心底座：一块连续裸内存如何变出任意多维视图？

### 1. 字节步长（`strides`）与仿射寻址公式

先看一个最具体的例子：分配 12 个 64 位整数（`int64`，每个元素固定占 `8` 字节），并将它重排成 `3` 行 `4` 列的二维矩阵（参考 [NumPy `ndarray` 内存模型文档](https://numpy.org/doc/stable/reference/arrays.ndarray.html)）：

```python
import numpy as np

# 生成 0..11 共 12 个 int64 整数（共 12 × 8 = 96 字节），重排为 3 行 4 列矩阵
a = np.arange(12, dtype=np.int64).reshape(3, 4)

print(a.shape)    # (3, 4)
print(a.strides)  # (32, 8)
```

在物理内存里，根本不存在什么“行”和“列”，只有从 `0` 到 `11` 紧挨着排成一排的 `96` 个字节。`ndarray` 之所以能把它当成 `(3, 4)` 矩阵来索引，靠的是元数据里的字节步长 `strides = (32, 8)`：沿第 0 维（向下跨一行）需要跳过 4 个元素即 `32` 字节；沿第 1 维（向右跨一列）只需跨过 1 个元素即 `8` 字节。任意多维逻辑下标 \((i_0, i_1, \dots, i_{d-1})\) 对应的物理字节偏移，全部归结为一条纯整数乘加公式：

$$
\text{byte\_offset}(i_0, \dots, i_{d-1}) = \text{offset} + \sum_{k=0}^{d-1} i_k \times \text{strides}[k]
$$

在上面的 `(3, 4)` 矩阵里，访问 `a[2, 1]` 只需计算 `0 + 2 * 32 + 1 * 8 = 72` 字节偏移，直接从基地址 `data_ptr + 72` 读出 8 个字节即可——没有指针追逐、没有哈希查找、也没有任何 Python 对象开销。

一旦看懂了这条寻址公式，切片、转置、翻转乃至广播（Broadcasting）为什么不需要复制半个字节就一目了然了——它们全都只是在复用同一块 `96` 字节缓冲区的前提下，换了一套 `offset`、`shape` 和 `strides` 元数据头：

![NumPy ndarray 连续内存缓冲与多维步长零拷贝视图机制：同一块 96 字节连续 int64 缓冲区通过改写 shape、offset 与 strides，零字节拷贝派生出行主序矩阵、转置视图、隔列切片以及 stride=0 广播拉伸视图](/images/diagrams/aug-ai-numpy-ndarray-strides.svg)

顺着图中的四个派生视图逐个核对步长账本：

```python
a.T          # shape=(4, 3), strides=(8, 32), offset=0   —— 仅对调两个轴的步长！
a[:, 1::2]   # shape=(3, 2), strides=(32, 16), offset=8  —— 首地址右移 1 列(8B)，列步长翻倍为 16B
a[::2]       # shape=(2, 4), strides=(64, 8), offset=0   —— 行步长翻倍为 64B
a[::-1]      # shape=(3, 4), strides=(-32, 8), offset=64 —— 起点移至末行，行步长取负值倒着走
```

甚至连初学者觉得最神奇的**广播机制（[Broadcasting](https://numpy.org/doc/stable/user/basics.broadcasting.html)）**，在步长模型下也极其自然。当形状为 `(3, 1)` 的列向量与形状为 `(1, 4)` 的行向量相加时，NumPy 从尾部维度向前逐轴比对（维度相等或其中一方为 `1` 即兼容）；对于长度为 `1`、需要被逻辑“拉伸”到 `4` 的那一维，底层只需把该轴的 **步长设为 `0`**（`strides = (8, 0)`）。内层 C 循环沿该轴前进时，`i_1 * 0 = 0`，指针原地不动反复读取同一个值，根本不需要在内存里先把列向量复制成四列！日常写归约代码时配合 `keepdims=True`，正是为了让统计量保留长度为 `1` 的占位轴，严丝合缝地触发零步长广播：

```python
rng = np.random.default_rng(42)
x = rng.normal(size=(1000, 64)).astype(np.float32)

# 沿样本轴归约并保留 (1, 64) 形状，减法与除法沿第 0 轴以 stride=0 自动广播
mean = x.mean(axis=0, keepdims=True)  # (1, 64)
std = x.std(axis=0, keepdims=True)    # (1, 64)
x_norm = (x - mean) / std
```

当然，共享底层缓冲区也意味着**原地修改视图会直接穿透回原数组**：

```python
x_arr = np.arange(6)
y_view = x_arr[1:4]
y_view[0] = 100
print(x_arr)  # [  0 100   2   3   4   5] —— 原数组第 1 号元素同步被改写！
```

需要独占数据所有权时必须显式调用 `.copy()`。此外还要拎清两个极易混淆的概念：**输入视图零拷贝，不代表算子表达式零分配**。`a + b` 在广播读取 `a` 和 `b` 时虽然不复制输入，但默认仍要按广播后的结果形状分配一整块全新的输出缓冲区（如果两个形状为 `(4000, 1)` 与 `(1, 4000)` 的小向量意外广播相加，瞬间就会物化出 `16,000,000` 个元素的巨型矩阵！）。同理，虽然 `np.lib.stride_tricks.as_strided` 允许手工改写任意步长，但一旦算错极易越界读到非法内存，工程代码构造滑窗应优先选用带边界校验的 `sliding_window_view`。

### 2. 视图（`View`）与副本（`Copy`）的分水岭，以及“零拷贝性能陷阱”

既然改元数据这么便宜，为什么有些操作还是非复制不可？判据只有一条：**目标元素序列在物理内存里的排布，还能不能写成具有固定步长的仿射公式 `offset + Σ(i_k · stride_k)`？**（详见官方[复制与视图规则](https://numpy.org/doc/stable/user/basics.copies.html)。）

- **花式索引（Fancy Indexing）与布尔掩码**：例如 `a[[0, 2], [1, 3]]` 或 `a[a > 5]`，命中的元素散落在毫无规律的位置上，根本不存在常数步长，只能分配新缓冲区逐个收集（Gather）拷出。
- **非连续视图上的 `reshape`**：对刚才的 `3 × 4` 矩阵先转置成 `a.T`（形状 `(4, 3)`，`strides = (8, 32)`），再调用 `.reshape(-1)` 试图拉平为一维。按逻辑顺序遍历 `a.T` 的第 0 列元素 `0, 4, 8` 时，物理地址每次跳 `+32` 字节；可一旦跨到第 1 列首元素 `1`，物理地址却突然退回到基地址 `+8` 字节！一根一维轴只有一个 `stride[0]`，无论填什么整数都无法同时表达 `+32` 和回跳，因此 `a.T.reshape(-1)` 必定触发一次 `Copy`。编写代码时可通过 `flags` 与 `np.shares_memory` 随时验明正身：

```python
print(a.flags["C_CONTIGUOUS"])     # True：行主序紧凑连续
print(a.T.flags["F_CONTIGUOUS"])   # True：转置后变为列主序连续，C_CONTIGUOUS 为 False
print(np.shares_memory(a, a.T))    # True：零拷贝共享缓冲区
```

把索引/变换判定与下游 C 内核（`ufunc` 与 BLAS/LAPACK）的连续性契约连成一张完整执行流图，还会暴露出一个反直觉的工程真相——**零拷贝并不天然等于高性能**：

![NumPy 视图 vs 副本判定与底层 SIMD / BLAS 执行分派流：从步长仿射可表达性切分零拷贝 View 与新分配 Copy，并在进入 ufunc SIMD 或 BLAS GEMM 前执行连续性检查与必要的隐式重排](/images/diagrams/aug-ai-numpy-view-vs-copy.svg)

以 Transformer 多头自注意力里最经典的一行变换为例：把投影结果从 `(B, S, H, D)` 调用 `.transpose(0, 2, 1, 3)` 转成 `(B, H, S, D)`。这一步在 Python 层确实是零拷贝，耗时近乎为零；但转置后的张量在序列维 `S` 上变成了跨大步长跳跃，不再满足 `C_CONTIGUOUS`。如果紧接着只做一次简单的跨步读取还好，若后续要连续喂给高度依赖连续缓存行预取（Cache Line Prefetch）和 SIMD 向量化的重型矩阵库或 GPU 内核，跨大步长引发的 Cache Miss、TLB Miss 甚至底层包装器偷偷触发的**隐式临时连续化拷贝（Implicit Contiguous Copy）**，代价反而可能超过显式做一次线性重排！分析性能时绝不能只盯着源码里有没有写 `.copy()`，必须结合 `flags`、峰值内存与基准测试看清真正的物化发生点。

---

## 二、线性代数引擎：为什么同一行 `A @ B` 在不同机器上快慢悬殊？

NumPy 自身的 C 代码主要负责通用数组遍历、逐元素通用函数（`ufunc`）与基础归约；一旦遇到矩阵乘法（`matmul` / `@`）、线性方程组求解、特征分解或奇异值分解（SVD），它并不会用朴素三重循环硬算，而是校验完形状与数据类型后，直接把缓冲区指针移交给底层的 **BLAS（Basic Linear Algebra Subprograms）** 与 **LAPACK（Linear Algebra PACKage）** 标准接口。

这里要严格区分“接口规范”与“具体后端库”：BLAS/LAPACK 只规定函数签名与数学契约，真正榨干硬件算力的是链接在背后的 **OpenBLAS、Intel MKL、Apple Accelerate 或 BLIS**。可以通过以下两行命令直接查看当前环境挂载的加速后端、SIMD 指令集与线程配置：

```python
np.show_config()   # 查看构建期链接的 BLAS / LAPACK 实现库（如 OpenBLAS、Accelerate）
np.show_runtime()  # 查看运行时探测到的 CPU SIMD 特性（AVX2 / AVX-512 / NEON）与线程池后端
```

### 1. BLAS 三级算术强度：为什么只有 GEMM 能跑满峰值算力？

BLAS 按数据维度与复用率将基础线性代数划分为三个层级：

| BLAS 层级 | 代表运算与 NumPy 入口 | 浮点运算量 | 内存读写量级 | 硬件瓶颈特征 |
| --- | --- | ---: | ---: | --- |
| **Level 1** | 向量内积 / 加权和：`np.dot(x, y)` | \(O(n)\) | \(O(n)\) | 算术强度极低，完全卡在**内存带宽**与函数调用开销 |
| **Level 2** | 矩阵-向量乘：`A @ x`（`gemv`） | \(O(n^2)\) | \(O(n^2)\) | 矩阵元素仅用一次即丢弃，多数规模下仍受**访存带宽**制约 |
| **Level 3** | 矩阵-矩阵乘：`A @ B`（`gemm`） | \(O(n^3)\) | \(O(n^2)\) | 数据复用率随规模线性抬升，靠**分块打包（Cache Blocking & Packing）**逼近峰值算力 |

```python
x = np.arange(4, dtype=np.float64)
y = np.arange(4, dtype=np.float64)
inner = np.dot(x, y)  # BLAS Level 1: 读 2n 个数，只做 n 次乘加

A = np.array([[1.0, 2.0], [3.0, 4.0]], dtype=np.float64)
B = np.array([[2.0, 0.0], [1.0, 2.0]], dtype=np.float64)
z = A @ x[:2]         # BLAS Level 2: GEMV
C = np.matmul(A, B)   # BLAS Level 3: GEMM
```

这段对比直接解释了后续大模型推理里 **Prefill 与 Decode 的性能分化**：一次性输入长 Prompt 的 Prefill 阶段把整批 Token 拼成大矩阵做 `GEMM`（Level 3），加载进 L1/L2 Cache 和寄存器的权重块能被成百上千个 Token 反复乘加；而逐词生成的 Decode 阶段每次只算 1 个新 Token，退化成了贴近 `GEMV`（Level 2）的瘦长运算——哪怕底层同样调了 BLAS（或 GPU 上的 cuBLAS），算力利用率也会断崖式下跌，彻底沦为搬运权重的内存带宽游戏。

### 2. LAPACK 矩阵分解：解方程永远优先写 `solve(A, b)` 而非 `inv(A) @ b`

建立在 BLAS Level 3 分块内核之上的 LAPACK，专门负责完整的数值矩阵分解。例如求解线性方程组 \(Ax = b\)：

```python
solution = np.linalg.solve(A, np.array([1.0, 0.0], dtype=np.float64))
```

对于双精度一般方阵，`np.linalg.solve` 底层走的是 LAPACK `dgesv` 例程（首字母前缀 `s / d / c / z` 分别对应 `float32 / float64 / complex64 / complex128`）：先用带部分主元选择的 LU 分解将矩阵拆为 \(PA = LU\)，再依次快速回代求解下三角系统 \(Ly = Pb\) 与上三角系统 \(Ux = y\)。相比先显式求逆 `np.linalg.inv(A)` 再右乘 `b`，直接调用 `solve` 不仅减少了约三分之二的浮点工作量与临时矩阵分配，还能避免病态矩阵显式求逆带来的灾难性舍入误差放大。

| NumPy 线性代数接口 | 底层核心数学路径 | 对应 LAPACK 例程家族 |
| --- | --- | --- |
| `np.linalg.solve` | 列主元 LU 分解 + 三角回代 | `*gesv` |
| `np.linalg.cholesky` | 对称正定矩阵 Cholesky 分解 | `*potrf` |
| `np.linalg.qr` | Householder 反射 QR 分解 | `*geqrf` + `*orgqr / *ungqr` |
| `np.linalg.eigh` | 实对称 / Hermitian 特征值分解 | `*syevd / *heevd` |
| `np.linalg.svd` | 分治法奇异值分解 | `*gesdd` |
| `np.linalg.lstsq` | 基于 SVD 的最小二乘与有效秩判定 | `*gelsd` |

做跨机器或跨写法基准测试时，还有三处底层细节极易干扰结论：一是传统 Fortran LAPACK 接口偏好列主序（`F_CONTIGUOUS`），尽管现代 CBLAS 能靠透传转置标志消化常规行主序矩阵，但隔行切片或负步长视图仍会在包装层触发隐式连续拷贝；二是 OpenBLAS / MKL 默认会在内部自建多线程池，若外层 Python 又套了 `multiprocessing` 或 `joblib` 多进程并行，几十上百个线程争抢物理核反而会令吞吐暴跌（可通过 `OMP_NUM_THREADS=1` 或 `threadpoolctl` 锁死）；三是成千上万个 `4×4` 微小矩阵做批量乘法的分派开销与单次 `4096×4096` 大矩阵完全不在一个频道上。

---

## 三、顺着内存契约重梳高频 NumPy API

如果不抓内存所有权，NumPy 上千个函数看起来就像一本枯燥的字典；但只要盯住**“元素占几字节、缓冲区归谁管、操作搬不搬字节、内核怎么批量扫内存”**这四个问题，整套 API 就能清晰归位（完整手册可查阅 [NumPy User Guide](https://numpy.org/doc/stable/user/)）。

### 1. 数据类型（`dtype`）与字节重解释：以 `complex64` 实现 RoPE 为例

同质定宽的 `dtype` 是紧凑寻址的根基：

| 类型族 | 常用 `dtype` | 单元素字节数 | 工程关键语义与踩坑点 |
| --- | --- | ---: | --- |
| **布尔型** | `bool_` | `1` | **并非按 bit 压缩**！每个布尔值占满 1 字节；位压缩需显式调 `np.packbits` |
| **定宽整数** | `int8/16/32/64`、`uint8..64` | `1 / 2 / 4 / 8` | 无任意精度保护，累加或平方和超界会静默环绕溢出；索引与计数前先核对上限 |
| **IEEE 浮点** | `float16 / float32 / float64` | `2 / 4 / 8` | Python 原生 `float` 默认转为 `float64`（带宽开销翻倍），模型与跨库协议需显式锁死 `float32` |
| **复数** | `complex64 / complex128` | `8 / 16` | 物理上由两个相邻 `float32` 或 `float64`（实部紧挨虚部）拼成，可零拷贝互转视图 |
| **时间与文本** | `datetime64`、`U`、`StringDType` | `8` 或定长/变长 | `datetime64` 本质是带单位元数据的 `int64`；NumPy 2.x 变长 `StringDType` 脱离纯内联定宽语义 |
| **对象引用** | `object_` | `8`（64 位指针） | 退化回 Python 指针数组，彻底丧失紧凑内存布局与 C 级 SIMD 加速能力 |

平时核查内存体积，第一反应永远是看 `arr.nbytes == arr.size * arr.itemsize`；同时在 NumPy 2.x 环境下，标量与数组的混合类型提升规则已按 [NEP 50](https://numpy.org/neps/nep-0050-scalar-promotion.html) 收紧，工程接口务必显式声明 `dtype`。

利用 `view(dtype)` 在不改动任何底层字节的前提下重解释数据类型，可以写出非常漂亮的旋转位置编码（RoPE）。RoPE 将特征向量的相邻两维 \((x_{2m}, x_{2m+1})\) 视作二维平面向量旋转角度 \(p \cdot \theta_m\)，映射到复平面上恰好就是复数乘法 \((x_{2m} + i x_{2m+1}) e^{i p \theta_m}\)：

```python
def apply_rotary_emb_complex(x: np.ndarray, base: float = 10000.0) -> np.ndarray:
    """对形状为 (seq_len, head_dim) 的 float32 张量施加复数域 RoPE（要求 head_dim 为偶数）。"""
    x = np.asarray(x, dtype=np.float32)
    seq_len, head_dim = x.shape
    if head_dim % 2 != 0:
        raise ValueError("head_dim 必须为偶数")
    if x.strides[-1] != x.itemsize:
        # 若最后一维被跨步切片打断连续性，相邻两个实数在物理上不再紧挨，必须先连续化！
        x = np.ascontiguousarray(x)

    dim = np.arange(0, head_dim, 2, dtype=np.float32)
    inv_freq = 1.0 / (base ** (dim / head_dim))
    positions = np.arange(seq_len, dtype=np.float32)
    factors = np.exp(1j * np.outer(positions, inv_freq)).astype(np.complex64)  # (seq_len, head_dim // 2)

    # 零拷贝把最后一维相邻两个 float32 重解释为一个 complex64，旋转后再零拷贝展回 float32
    return (x.view(np.complex64) * factors).view(np.float32)
```

这段代码把“零拷贝”和“内存分配”的边界划分得一清二楚：前后的 `.view(np.complex64)` 与 `.view(np.float32)` 确实只是改了元数据头，零字节搬运；但中间的复数乘法 `*` 依然分配了保存旋转结果的新数组，且一旦输入最后一维不连续（`x.strides[-1] != 4`），入口处的 `ascontiguousarray` 就必须先付出一笔复制开销。

### 2. 数组创建与跨生态共享缓冲区

创建数组时最核心的抉择是**要不要新开内存**：

- **从 Python 序列转换**：`np.array(obj)` 默认复制出独立缓冲区；而 `np.asarray(obj, dtype=np.float32)` 在输入已经是满足 `dtype` 要求的 `ndarray` 时直接零拷贝复用。浮点网格采样优先用含端点的 `np.linspace(start, stop, num)`，避免 `np.arange` 累加浮点步长带来的尾数漂移。
- **预分配缓冲**：`np.zeros`、`np.ones_like`、`np.full` 会写满初始值；`np.empty` 则只向操作系统或分配器划出裸内存而不清零，速度最快，但读取前必须确保每一个槽位都被后续计算完整覆写。
- **挂载外部内存与文件映射**：`np.frombuffer(raw_bytes, dtype=np.float32)` 与 `np.memmap("vectors.bin", dtype=np.float32, mode="r", shape=(1000, 128))` 允许直接把字节流或磁盘大文件映射成数组视图，省去用户态全量拷贝，但调用方必须自行保证底层对象生命周期与字节序（Endianness）有效。

当数据跨出 NumPy 流向周边生态（参考官方[互操作指南](https://numpy.org/doc/stable/user/basics.interoperability.html)）时，零拷贝同样是有严格前提的契约：PyTorch CPU Tensor 可经 `torch.from_numpy()` 共享同一块主机内存（原地修改一方另一方立刻跟着变），但一旦上卡进入 GPU 显存，就只能走 DLPack 在 CuPy、PyTorch CUDA 与 JAX 之间交换设备指针；pandas 调 `.to_numpy()` 若遇到异构列合并或可空类型（Nullable Dtype）会自动退化为复制；Apache Arrow 数组也只有在类型定宽、无 `NULL` 有效位图（Validity Bitmap）且位于单一连续 Chunk 时，才能零拷贝映射成只读的 NumPy 视图。

### 3. 变形、拼接、归约与常用批量算子

日常做数组变换和计算时，牢记下面四组高频模式的成本差异，能避开绝大多数隐性性能退化：

1. **严防循环内 `concatenate` 的平方级复杂度陷阱**：`reshape`、`transpose`、`swapaxes`、`ravel` 都会优先尝试返回视图，而 `flatten`、`concatenate`、`stack`、`tile`、`repeat`、`pad` **永远分配新内存并复制数据**。如果在 `for` 循环里每步调用 `arr = np.concatenate([arr, new_row])`，历史数据会被一遍遍搬进新家，总拷贝量从 \(O(N)\) 恶化为 \(O(N^2)\)。正确做法是先用 `np.empty` 按最大容量预分配一整块数组，再用切片 `buf[pos:pos+step] = new_row` 原地写入——这正是下文 KV Cache 的标准写法。
2. **善用 `out=` 复用缓冲与数值稳定函数**：逐元素数学函数支持传入目标缓冲（如 `np.exp(scores, out=scores)`），省掉中间临时数组分配；计算微小量对数与指数差时优先调用 `np.log1p(x)` 与 `np.expm1(x)` 抑制有效数字相消；校验浮点相等永远用 `np.allclose(actual, expected, rtol=1e-5, atol=1e-7)` 替代裸 `==`。
3. **Top-K 选择不必全量排序**：只需要找出得分最高的前 \(k\) 个词元下标时，`np.argsort` 会付出完整的 \(O(N \log N)\) 排序成本，而 `np.argpartition(scores, -k)[-k:]` 基于快速选择算法只需 \(O(N)\) 平均时间即可把最大的 \(k\) 项归拢到末尾。
4. **独立可复现随机流与张量缩并**：新代码一律使用局部实例 `rng = np.random.default_rng(42)` 取代污染全局状态的 `np.random.seed`，多进程数据加载可通过 [`rng.spawn(4)`](https://numpy.org/doc/stable/reference/random/generated/numpy.random.Generator.spawn.html) 派生统计独立的子流；复杂多维缩并可用 `np.einsum("bhsd,bhtd->bhst", q, k)` 清晰表达下标语义，但在性能敏感路径上仍需对比显式 `transpose + matmul` 是否能更直接地命中底层 BLAS `GEMM` 快路径。

---

## 四、从微分方程到单层 LLM：用 NumPy 写透参考实现

NumPy 在现代 AI 与科学计算工程里最不可替代的角色，就是充当**绝对透明的参考实现（Reference Implementation）**：没有异步流延迟、没有编译器图改写，每一个中间数组的形状、步长与浮点误差都能直接摊在桌面上检验。下面用两个跨领域的完整实例把前面的机制全部串起来。

### 1. 一维泊松方程中心差分求解：看清稠密与稀疏的复杂度边界

求解经典边值问题 \(-u''(x) = f(x)\)（边界条件 \(u(0) = u(1) = 0\)）。取解析真值 \(u(x) = \sin(\pi x)\)，右端源项即为 \(f(x) = \pi^2 \sin(\pi x)\)。在 \([0, 1]\) 区间等距划分 \(N\) 段（步长 \(h = 1/N\)），代入二阶中心差分近似 \(u''(x_i) \approx (u_{i-1} - 2u_i + u_{i+1}) / h^2\)，内部 \(N-1\) 个网格点便化为一个对称正定三对角线性方程组：

```python
import numpy as np

N = 100
h = 1.0 / N
x = np.linspace(0.0, 1.0, N + 1)
x_inner = x[1:-1]
n = N - 1

# 用三条偏移对角线拼出 (99, 99) 稠密三对角刚度矩阵 A
A = (
    np.diag(2.0 * np.ones(n))
    + np.diag(-1.0 * np.ones(n - 1), k=1)
    + np.diag(-1.0 * np.ones(n - 1), k=-1)
)
b = (h ** 2) * (np.pi ** 2) * np.sin(np.pi * x_inner)

# 调用底层 LAPACK dgesv 求解，拼回两端零边界后核对无穷范数误差
u_inner = np.linalg.solve(A, b)
u_numerical = np.concatenate(([0.0], u_inner, [0.0]))
max_error = np.max(np.abs(u_numerical - np.sin(np.pi * x)))
print(f"N={N}, L_inf error={max_error:.3e}")
```

只要把 `N` 从 `100` 翻倍到 `200`，就能亲眼看到最大误差缩减为原来的四分之一，干净利落地验证二阶收敛率。同时，这段二十行的原型也一眼暴露了稠密数组模型的规模天花板：`np.diag` 把绝大多数为零的元素全存成了 `float64`，占用 \(O(N^2)\) 内存，通用 LU 分解耗时 \(O(N^3)\)；一旦网格加密到十万级，就该保持公式不变，无缝切入 `scipy.sparse.diags` 与三对角专用 \(O(N)\) 追赶法求解器——**用 NumPy 稠密原型锁死数学正确性，再交给 SciPy 专业数据结构兑现渐近复杂度**。

### 2. 手写单层 Transformer Decoder：把 RoPE、因果掩码与 KV Cache 钉在代码上

再看当下最核心的 Decoder-only 大模型推理（以 [LLaMA 2](https://arxiv.org/abs/2307.09288)、[Qwen](https://arxiv.org/abs/2309.16609) 与 [Mistral 7B](https://arxiv.org/abs/2310.06825) 共享的现代架构骨架为准）。剥去分词器、词表嵌入和几十层循环堆叠，一个隐藏状态张量 `x: (B, S, D)` 穿过单层 Decoder 时，走的是一条清晰的 **Pre-Norm + 双残差** 通路：

$$
x_{\text{mid}} = x + \operatorname{CausalAttention}(\operatorname{RMSNorm}(x)), \qquad x_{\text{out}} = x_{\text{mid}} + \operatorname{SwiGLU}(\operatorname{RMSNorm}(x_{\text{mid}}))
$$

其中 [RMSNorm](https://arxiv.org/abs/1910.07467) 省去了均值中心化，仅按最后一维均方根重缩放；位置信息通过前文介绍的复数视图 [RoPE](https://arxiv.org/abs/2104.09864) 注入 `Q` 和 `K`；门控前馈网络采用 [SwiGLU](https://arxiv.org/abs/2002.05202)。推理过程中最关键的状态管理在于 **`KVCache`**：

| 推理阶段 | 当前输入长度 \(S\) | 缓存累计总长 \(T\) | 注意力分数 \(QK^T\) 形状 | 硬件负载特征与算子形态 |
| --- | ---: | ---: | ---: | --- |
| **Prefill（首轮提示词）** | \(S = 5\) | \(T = 5\) | `(B, H, 5, 5)`（需上三角 `-inf` 遮罩） | 多 Token 矩阵乘，贴近 BLAS Level 3 计算密集型 |
| **Decode Step 1** | \(S = 1\) | \(T = 6\) | `(B, H, 1, 6)`（单查询读全部历史） | 省去历史 K/V 重算，但退化为带宽受限的瘦长矩阵乘 |
| **Decode Step 2** | \(S = 1\) | \(T = 7\) | `(B, H, 1, 7)`（切片追加，零历史搬运） | KV 显存占用按 \(2 \times L \times B \times H_{kv} \times T \times HD \times \text{itemsize}\) 线性增长 |

下面用不到 180 行纯 NumPy 代码把整层推理与增量 KV 缓存完整跑通：

```python
import numpy as np


def rms_norm(x: np.ndarray, weight: np.ndarray, eps: float = 1e-6) -> np.ndarray:
    variance = np.mean(x * x, axis=-1, keepdims=True)
    return x * (1.0 / np.sqrt(variance + eps)) * weight


def silu(x: np.ndarray) -> np.ndarray:
    return x / (1.0 + np.exp(-x))


def precompute_freqs_cis(head_dim: int, max_seq_len: int, base: float = 10000.0) -> np.ndarray:
    dim = np.arange(0, head_dim, 2, dtype=np.float32)
    inv_freq = 1.0 / (base ** (dim / head_dim))
    positions = np.arange(max_seq_len, dtype=np.float32)
    angles = np.outer(positions, inv_freq)
    return np.exp(1j * angles).astype(np.complex64)


def apply_rotary_emb(x: np.ndarray, freqs_cis: np.ndarray) -> np.ndarray:
    """对形状为 (B, H, S, HD) 的张量施加旋转位置编码。"""
    x = np.asarray(x, dtype=np.float32)
    if x.shape[-1] % 2 != 0:
        raise ValueError("head_dim 必须为偶数")
    if x.strides[-1] != x.itemsize:
        x = np.ascontiguousarray(x)

    x_complex = x.view(np.complex64)
    factors = freqs_cis[None, None, :, :]  # 从 (S, HD/2) 零步长广播至 (B, H, S, HD/2)
    return (x_complex * factors).view(np.float32)


class KVCache:
    def __init__(self, batch_size: int, num_heads: int, max_seq_len: int, head_dim: int) -> None:
        shape = (batch_size, num_heads, max_seq_len, head_dim)
        # 预分配固定容量连续缓冲，避免 Decode 循环里调 concatenate 引发二次方拷贝！
        self.k = np.empty(shape, dtype=np.float32)
        self.v = np.empty(shape, dtype=np.float32)
        self.length = 0

    def append(self, k: np.ndarray, v: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        step = k.shape[2]
        end = self.length + step
        if end > self.k.shape[2]:
            raise ValueError("超出 KV Cache 预分配容量上限")
        self.k[:, :, self.length:end, :] = k
        self.v[:, :, self.length:end, :] = v
        self.length = end
        # 返回有效前缀切片视图 (B, H, end, HD)，零字节复制历史数据
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
            raise ValueError("d_model 必须能被 num_heads 整除")
        self.d_model = d_model
        self.num_heads = num_heads
        self.head_dim = d_model // num_heads
        self.freqs = precompute_freqs_cis(self.head_dim, max_seq_len)

        scale = np.float32(1.0 / np.sqrt(d_model))

        def weight(*shape: int, s: np.float32 = scale) -> np.ndarray:
            return rng.normal(0.0, float(s), size=shape).astype(np.float32)

        self.wq = weight(d_model, d_model)
        self.wk = weight(d_model, d_model)
        self.wv = weight(d_model, d_model)
        self.wo = weight(d_model, d_model)

        self.w_gate = weight(d_model, d_ff)
        self.w_up = weight(d_model, d_ff)
        self.w_down = weight(d_ff, d_model, s=np.float32(1.0 / np.sqrt(d_ff)))

        self.attn_norm = np.ones(d_model, dtype=np.float32)
        self.ffn_norm = np.ones(d_model, dtype=np.float32)

    def forward(self, x: np.ndarray, cache: KVCache, start_pos: int) -> np.ndarray:
        B, S, D = x.shape
        H, HD = self.num_heads, self.head_dim

        residual = x
        h = rms_norm(x, self.attn_norm)

        # 拆分多头并交换序列维与头维：(B, S, H, HD) -> (B, H, S, HD)
        q = (h @ self.wq).reshape(B, S, H, HD).transpose(0, 2, 1, 3)
        k = (h @ self.wk).reshape(B, S, H, HD).transpose(0, 2, 1, 3)
        v = (h @ self.wv).reshape(B, S, H, HD).transpose(0, 2, 1, 3)

        current_freqs = self.freqs[start_pos : start_pos + S]
        q = apply_rotary_emb(q, current_freqs)
        k = apply_rotary_emb(k, current_freqs)

        keys, values = cache.append(k, v)
        total_s = keys.shape[2]

        scores = (q @ keys.swapaxes(-1, -2)) / np.sqrt(np.float32(HD))
        if S > 1:
            # Prefill 阶段叠加因果上三角遮罩，禁止当前位置读取未来 Token
            mask = np.triu(np.full((S, total_s), -np.inf, dtype=np.float32), k=total_s - S + 1)
            scores += mask[None, None, :, :]

        # 原地减去行最大值并复用 scores 缓冲完成稳定 Softmax
        scores -= np.max(scores, axis=-1, keepdims=True)
        np.exp(scores, out=scores)
        scores /= np.sum(scores, axis=-1, keepdims=True)

        # 注意力聚合后经 transpose 变非连续，reshape(B, S, D) 合并多头时会自动触发一次连续化整理
        attn = (scores @ values).transpose(0, 2, 1, 3).reshape(B, S, D)
        h = residual + attn @ self.wo

        residual = h
        h = rms_norm(h, self.ffn_norm)
        ffn = (silu(h @ self.w_gate) * (h @ self.w_up)) @ self.w_down
        return residual + ffn


rng = np.random.default_rng(42)
B, D, H, FF, MAX_SEQ_LEN, PREFILL_LEN = 1, 64, 4, 128, 32, 5
layer = TransformerDecoderLayer(D, H, FF, MAX_SEQ_LEN, rng)
cache = KVCache(B, H, MAX_SEQ_LEN, D // H)

prompt = rng.normal(size=(B, PREFILL_LEN, D)).astype(np.float32)
prefill_out = layer.forward(prompt, cache, start_pos=0)
print("prefill:", prefill_out.shape, "cache_len:", cache.length)  # (1, 5, 64), cache_len: 5

current = rng.normal(size=(B, 1, D)).astype(np.float32)
for step in range(3):
    current = layer.forward(current, cache, start_pos=cache.length)
    print("decode:", step, current.shape, "cache_len:", cache.length)  # (1, 1, 64), cache_len: 6, 7, 8
```

前面聊过的所有底层机制在这段代码里严丝合缝地咬合在了一起：`KVCache` 用 `np.empty` 预分配加切片视图规避了循环拼接拷贝；`apply_rotary_emb` 用 `.view(np.complex64)` 省去奇偶维手工重排；稳定 Softmax 靠 `keepdims=True` 广播配合 `out=scores` 原地覆写省掉两块 `(B, H, S, T)` 临时矩阵；而合并多头时的 `.transpose(0, 2, 1, 3).reshape(B, S, D)` 正是前文剖析过的“跨步视图无法单步拉平、由 `reshape` 自动重排为连续内存”的鲜活现场。再往生产级推理引擎迈进一步，无非就是把标准多头（MHA）换成共享键值头的 GQA/MQA、把单请求预分配连续数组换成多请求分页管理的 PagedAttention，并把逐算子 Eager 调用融合成驻留 GPU SRAM 的 FlashAttention 内核。

---

## 五、张量生态全景：长得像 NumPy，底层改写了哪条契约？

理解了 `ndarray` 其实是“**CPU 主机内存 + 可变稠密缓冲 + 自由步长视图 + 同步立即执行**”这四条假设的组合体，再看整个 Python 数据与深度学习生态，就能一眼看穿所谓“NumPy 兼容（NumPy-Compatible）”背后的真实差异——每一个周边框架，都是为了突破某一个工程瓶颈，主动打破了其中至少一条假设：

| 数组/张量生态 | 在 `ndarray` 模型之上新增的核心能力 | 打破或改写的底层执行契约 |
| --- | --- | --- |
| **pandas** | 行列标签索引（`Index`）、异构表格列、原生缺失值与时间序列窗口 | `a + b` **先按标签对齐再运算**（而非纯按位置广播）；`to_numpy()` 遇异构列或缺失值常触发复制 |
| **SciPy** | 稀疏矩阵存储（CSR/CSC/DIA）、数值优化、信号滤波与科学求解器 | 稀疏数组只存非零值与坐标索引，不再支持任意 \(O(1)\) 步长切片或自由 `reshape` |
| **PyTorch** | GPU/加速器设备管理、动态自动微分图（Autograd）、神经网络与分布式通信 | `Tensor` 绑定 `device` 与梯度历史；`tensor.view()` 遇非连续布局会**直接报错**而非像 `reshape` 自动复制 |
| **JAX** | `jit` 编译融合、`grad`、`vmap` 批量化变换与 XLA 多设备并行 | 数组**严格不可变（Immutable）**；要求编译期静态形状（`nonzero`/`unique` 需定长约束），执行默认异步分派 |
| **CuPy** | 驻留 NVIDIA 显存的 GPU 数组、内联 CUDA 内核与高度兼容的算子接口 | 跨 PCIe 总线传输与异步流同步成为头号性能变量；裸指针内核不自动解析非连续 `strides` |
| **Apache Arrow** | 跨语言标准列式内存格式、有效性位图（`NULL`）、偏移量变长文本与嵌套结构 | 面向进程/系统间零拷贝交换的**不可变列缓冲**，本身不承担通用稠密矩阵计算引擎职责 |

工程选型与跨框架迁移时，顺着这条边界清单逐项核查即可：先用 NumPy 写清小规模数学原型与黄金参考真值；涉及表格对齐与缺失值治理进 pandas；遇到大规模稀疏结构找 SciPy；上 GPU 训练与部署神经网络切入 PyTorch（或用 CuPy 快速加速既有数组流水线，用 JAX 做函数式编译变换）；跨进程或跨语言搬运列式数据则走 Arrow / DLPack 并显式核验零拷贝前置条件。只要始终追问一句“**数据此刻躺在哪块内存、步长是否连续、算子何时真正同步落定**”，无论换哪套张量外壳都不会迷失方向。

---

## 参考资料

- [NumPy: The N-dimensional array (`ndarray`)](https://numpy.org/doc/stable/reference/arrays.ndarray.html) & [Copies and views](https://numpy.org/doc/stable/user/basics.copies.html)
- [NumPy: Broadcasting](https://numpy.org/doc/stable/user/basics.broadcasting.html) & [Interoperability with NumPy](https://numpy.org/doc/stable/user/basics.interoperability.html)
- [NumPy: Array API standard compatibility](https://numpy.org/doc/stable/reference/array_api.html) & [NEP 50 — Scalar promotion rules](https://numpy.org/neps/nep-0050-scalar-promotion.html)
- [NumPy: BLAS and LAPACK build configuration](https://numpy.org/doc/stable/building/blas_lapack.html)、[Netlib BLAS](https://netlib.org/blas/) & [LAPACK Users' Guide](https://www.netlib.org/lapack/lug/)
- [pandas: NumPy conversion](https://pandas.pydata.org/docs/user_guide/basics.html)、[SciPy: Sparse arrays](https://docs.scipy.org/doc/scipy/tutorial/sparse.html) & [Apache Arrow: Zero-copy conditions](https://arrow.apache.org/docs/python/pandas.html)
- [PyTorch: `torch.from_numpy`](https://docs.pytorch.org/docs/stable/generated/torch.from_numpy.html)、[JAX: Differences from NumPy](https://docs.jax.dev/en/latest/jax.numpy.html) & [CuPy: Differences between CuPy and NumPy](https://docs.cupy.dev/en/stable/user_guide/difference.html)
- [LLaMA 2](https://arxiv.org/abs/2307.09288)、[Qwen Technical Report](https://arxiv.org/abs/2309.16609)、[Mistral 7B](https://arxiv.org/abs/2310.06825)、[RMSNorm](https://arxiv.org/abs/1910.07467)、[RoFormer (RoPE)](https://arxiv.org/abs/2104.09864) & [SwiGLU](https://arxiv.org/abs/2002.05202)
