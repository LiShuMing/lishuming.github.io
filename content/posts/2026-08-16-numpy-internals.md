---
title: "【LLM】从 NumPy 内存模型、手写两层 MLP 到 CuPy/CUDA 设备边界与异步计时"
date: 2026-08-16T00:00:00+08:00
lastmod: 2026-11-21T00:00:00+08:00
slug: "numpy-mlp-to-cupy-cuda"
aliases:
  - "/2026/08/16/llmnumpy从-ndarray-内存模型到科学计算与张量生态/"
  - "/2026/08/16/llmnumpy实践从向量化到手写两层-mlp-与反向传播/"
  - "/2026/08/16/numpy-实践从向量化到手写两层-mlp-与反向传播/"
  - "/2026/08/16/numpy-项目实践从向量化到手写两层-mlp-与反向传播/"
  - "/2026/08/16/numpy从-ndarray-内存模型到科学计算与张量生态/"
  - "/2026/08/16/numpy实践从向量化到手写两层-mlp-与反向传播/"
  - "/2026/08/16/深入-numpy从-ndarray-内存模型到数据库向量化执行与-llm-推理/"
  - "/2026/08/16/深入-numpy从-ndarray-内存模型到科学计算与张量生态/"
  - "/2026/10/28/cupy-cuda-device-boundaries-and-timing/"
categories:
  - AI Infra
tags:
  - NumPy
  - Neural Network
  - Backpropagation
  - CuPy
  - CUDA
  - PyTorch
  - Jupyter
  - 性能分析
description: "沿着连续内存与执行边界这条物理主线，从 NumPy ndarray 步长、视图拷贝与 BLAS/LLM 原型出发，脱离框架手写两层 MLP、稳定 Softmax、反向传播与有限差分梯度检查，再跨过 PCIe 总线用 CuPy 与 RawKernel 验证显式数据搬运、10,003 元素线程越界守卫与 CUDA Event 异步计时。"
draft: false
toc: true
math: true
notebook: true
---

在纯 Python 里写一个 `for` 循环把两个长度为百万的 `list` 逐项相加，耗时往往比等效的 C 语言循环慢上 **50 到 100 倍**。很多人把原因笼统归结为“Python 是解释型语言”，但如果顺着内存地址往下看，真正的性能黑洞其实藏在每一个元素的存储与分派方式里。

在 CPython 中，一个包含一百万个浮点数的 `list` 并不是一段紧挨着存放的 8 字节 `double` 数组，而是一排指向堆内存各处的 `PyObject*` 指针。每次执行 `a[i] + b[i]`，解释器都要先后追着两个指针跳去不同的堆内存页（破坏 CPU 缓存空间局部性），读取各自的 `PyTypeObject` 类型头确认双方都是 `float`（动态类型分派），维护引用计数（`ob_refcnt` 增减），再向内存分配器申请一个全新的 24 字节 `PyFloatObject` 把相加结果装箱（Boxing）塞回去。真正花在浮点加法器指令上的时间还不到总耗时的零头。

要想让 Python 跑出逼近硬件内存带宽上限的速度，唯一的出路就是彻底砍掉逐元素的指针装箱与动态检查。NumPy 给出的解法极其洗练——用 **`ndarray`**（N-dimensional Array）把“物理存储”和“多维逻辑解释”一刀劈开：底层只保留一整块同质、定宽、紧凑排列的一维裸字节缓冲区（Contiguous Raw Buffer），上层则用一份轻量的元数据头（`dtype`、`shape` 与字节步长 `strides`）来描述怎么按偏移量切分和读取这块内存。

抓紧“数据躺在哪块内存、怎么按步长寻址、何时真正计算落定”这条物理主线，深度学习底层最核心的三级台阶就会自然连成一条严密的因果链：先在 CPU 主机内存里吃透 `ndarray` 步长、视图拷贝、BLAS 矩阵引擎与单层 Transformer 参考实现；接着撤掉自动求导黑盒，纯靠 `ndarray` 手写两层多层感知机（MLP）、数值稳定 Softmax、反向传播与有限差分梯度校验，亲手摸清训练显存与梯度流动的真实来路；最后把 `import numpy as np` 换成 `import cupy as cp`，跨过 PCIe 总线踏进 GPU 显存，用最小可复现实验钉死显式数据搬运、裸指针线程越界守卫以及 CUDA 异步流计时这三道硬件关卡。

---

## 一、`ndarray` 内存模型：连续裸缓冲、步长与视图拷贝边界

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

甚至连初学者觉得最神奇的 **广播机制**（[Broadcasting](https://numpy.org/doc/stable/user/basics.broadcasting.html)），在步长模型下也极其自然。当形状为 `(3, 1)` 的列向量与形状为 `(1, 4)` 的行向量相加时，NumPy 从尾部维度向前逐轴比对（维度相等或其中一方为 `1` 即兼容）；对于长度为 `1`、需要被逻辑“拉伸”到 `4` 的那一维，底层只需把该轴的 **步长设为 `0`**（`strides = (8, 0)`）。内层 C 循环沿该轴前进时，`i_1 * 0 = 0`，指针原地不动反复读取同一个值，根本不需要在内存里先把列向量复制成四列！日常写归约代码时配合 `keepdims=True`，正是为了让统计量保留长度为 `1` 的占位轴，严丝合缝地触发零步长广播：

```python
rng = np.random.default_rng(42)
x = rng.normal(size=(1000, 64)).astype(np.float32)

# 沿样本轴归约并保留 (1, 64) 形状，减法与除法沿第 0 轴以 stride=0 自动广播
mean = x.mean(axis=0, keepdims=True)  # (1, 64)
std = x.std(axis=0, keepdims=True)    # (1, 64)
x_norm = (x - mean) / std
```

当然，共享底层缓冲区也意味着 **原地修改视图会直接穿透回原数组**：

```python
x_arr = np.arange(6)
y_view = x_arr[1:4]
y_view[0] = 100
print(x_arr)  # [  0 100   2   3   4   5] —— 原数组第 1 号元素同步被改写！
```

需要独占数据所有权时必须显式调用 `.copy()`。此外还要拎清两个极易混淆的概念：**输入视图零拷贝，不代表算子表达式零分配**。`a + b` 在广播读取 `a` 和 `b` 时虽然不复制输入，但默认仍要按广播后的结果形状分配一整块全新的输出缓冲区（如果两个形状为 `(4000, 1)` 与 `(1, 4000)` 的小向量意外广播相加，瞬间就会物化出 `16,000,000` 个元素的巨型矩阵！）。同理，虽然 `np.lib.stride_tricks.as_strided` 允许手工改写任意步长，但一旦算错极易越界读到非法内存，工程代码构造滑窗应优先选用带边界校验的 `sliding_window_view`。

### 2. 视图（`View`）与副本（`Copy`）的分水岭，以及“零拷贝性能陷阱”

既然改元数据这么便宜，为什么有些操作还是非复制不可？判据只有一条：**目标元素序列在物理内存里的排布，还能不能写成具有固定步长的仿射公式 `offset + Σ(i_k · stride_k)`？**（详见官方 [复制与视图规则](https://numpy.org/doc/stable/user/basics.copies.html)。）

- **花式索引（Fancy Indexing）与布尔掩码**：例如 `a[[0, 2], [1, 3]]` 或 `a[a > 5]`，命中的元素散落在毫无规律的位置上，根本不存在常数步长，只能分配新缓冲区逐个收集（Gather）拷出。
- **非连续视图上的 `reshape`**：对刚才的 `3 × 4` 矩阵先转置成 `a.T`（形状 `(4, 3)`，`strides = (8, 32)`），再调用 `.reshape(-1)` 试图拉平为一维。按逻辑顺序遍历 `a.T` 的第 0 列元素 `0, 4, 8` 时，物理地址每次跳 `+32` 字节；可一旦跨到第 1 列首元素 `1`，物理地址却突然退回到基地址 `+8` 字节！一根一维轴只有一个 `stride[0]`，无论填什么整数都无法同时表达 `+32` 和回跳，因此 `a.T.reshape(-1)` 必定触发一次 `Copy`。编写代码时可通过 `flags` 与 `np.shares_memory` 随时验明正身：

```python
print(a.flags["C_CONTIGUOUS"])     # True：行主序紧凑连续
print(a.T.flags["F_CONTIGUOUS"])   # True：转置后变为列主序连续，C_CONTIGUOUS 为 False
print(np.shares_memory(a, a.T))    # True：零拷贝共享缓冲区
```

把索引/变换判定与下游 C 内核（`ufunc` 与 BLAS/LAPACK）的连续性契约连成一张完整执行流图，还会暴露出一个反直觉的工程真相——**零拷贝并不天然等于高性能**：

![NumPy 视图 vs 副本判定与底层 SIMD / BLAS 执行分派流：从步长仿射可表达性切分零拷贝 View 与新分配 Copy，并在进入 ufunc SIMD 或 BLAS GEMM 前执行连续性检查与必要的隐式重排](/images/diagrams/aug-ai-numpy-view-vs-copy.svg)

以 Transformer 多头自注意力里最经典的一行变换为例：把投影结果从 `(B, S, H, D)` 调用 `.transpose(0, 2, 1, 3)` 转成 `(B, H, S, D)`。这一步在 Python 层确实是零拷贝，耗时近乎为零；但转置后的张量在序列维 `S` 上变成了跨大步长跳跃，不再满足 `C_CONTIGUOUS`。如果紧接着只做一次简单的跨步读取还好，若后续要连续喂给高度依赖连续缓存行预取（Cache Line Prefetch）和 SIMD 向量化的重型矩阵库或 GPU 内核，跨大步长引发的 Cache Miss、TLB Miss 甚至底层包装器偷偷触发的 **隐式临时连续化拷贝**（Implicit Contiguous Copy），代价反而可能超过显式做一次线性重排！分析性能时绝不能只盯着源码里有没有写 `.copy()`，必须结合 `flags`、峰值内存与基准测试看清真正的物化发生点。

---

## 二、线性代数引擎与内存契约下的高频 API

### 1. BLAS 三级算术强度与 LAPACK 分解

NumPy 自身的 C 代码主要负责通用数组遍历、逐元素通用函数（`ufunc`）与基础归约；一旦遇到矩阵乘法（`matmul` / `@`）、线性方程组求解、特征分解或奇异值分解（SVD），它并不会用朴素三重循环硬算，而是校验完形状与数据类型后，直接把缓冲区指针移交给底层的 **BLAS**（Basic Linear Algebra Subprograms）与 **LAPACK**（Linear Algebra PACKage）标准接口。

这里要严格区分“接口规范”与“具体后端库”：BLAS/LAPACK 只规定函数签名与数学契约，真正榨干硬件算力的是链接在背后的 **OpenBLAS、Intel MKL、Apple Accelerate 或 BLIS**。可以通过以下两行命令直接查看当前环境挂载的加速后端、SIMD 指令集与线程配置：

```python
np.show_config()   # 查看构建期链接的 BLAS / LAPACK 实现库（如 OpenBLAS、Accelerate）
np.show_runtime()  # 查看运行时探测到的 CPU SIMD 特性（AVX2 / AVX-512 / NEON）与线程池后端
```

BLAS 按数据维度与复用率将基础线性代数划分为三个层级：

| BLAS 层级 | 代表运算与 NumPy 入口 | 浮点运算量 | 内存读写量级 | 硬件瓶颈特征 |
| --- | --- | ---: | ---: | --- |
| **Level 1** | 向量内积 / 加权和：`np.dot(x, y)` | \(O(n)\) | \(O(n)\) | 算术强度极低，完全卡在**内存带宽**与函数调用开销 |
| **Level 2** | 矩阵-向量乘：`A @ x`（`gemv`） | \(O(n^2)\) | \(O(n^2)\) | 矩阵元素仅用一次即丢弃，多数规模下仍受**访存带宽**制约 |
| **Level 3** | 矩阵-矩阵乘：`A @ B`（`gemm`） | \(O(n^3)\) | \(O(n^2)\) | 数据复用率随规模线性抬升，靠**分块打包**（Cache Blocking & Packing）逼近峰值算力 |

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

### 2. `dtype` 字节重解释、缓冲共享与高频算子避坑

如果不抓内存所有权，NumPy 上千个函数看起来就像一本枯燥的字典；但只要盯住“**元素占几字节、缓冲区归谁管、操作搬不搬字节、内核怎么批量扫内存**”这四个问题，整套 API 就能清晰归位（完整手册可查阅 [NumPy User Guide](https://numpy.org/doc/stable/user/)）。

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

在数组创建、跨生态共享与批量变换上，还有两组高频内存契约值得牢记：

- **是否新开内存与跨生态共享**：`np.array(obj)` 默认复制出独立缓冲区，`np.asarray(obj, dtype=np.float32)` 在输入已满足类型要求时零拷贝复用；浮点网格优先用含端点的 `np.linspace` 避免 `np.arange` 尾数漂移；`np.empty` 只划出裸内存而不清零，读取前必须确保每个槽位都被覆写；`np.frombuffer` 与 `np.memmap` 可把字节流或磁盘大文件直接映射为视图。数据跨出 NumPy 时（参考官方 [互操作指南](https://numpy.org/doc/stable/user/basics.interoperability.html)），PyTorch CPU Tensor 经 `torch.from_numpy()` 共享同一块主机内存，一旦上卡进入显存则需经 DLPack 在 CuPy、PyTorch CUDA 与 JAX 间交换设备指针；pandas 调 `.to_numpy()` 遇异构列或可空类型常退化为复制；Apache Arrow 数组也只有在定宽、无 `NULL` 位图且处于单一连续 Chunk 时才能零拷贝映射为只读视图。
- **高频算子的隐性成本**：
  1. **严防循环内 `concatenate` 的平方级复杂度陷阱**：`reshape`、`transpose`、`swapaxes`、`ravel` 都会优先尝试返回视图，而 `flatten`、`concatenate`、`stack`、`tile`、`repeat`、`pad` **永远分配新内存并复制数据**。如果在 `for` 循环里每步调用 `arr = np.concatenate([arr, new_row])`，历史数据会被一遍遍搬进新家，总拷贝量从 \(O(N)\) 恶化为 \(O(N^2)\)。正确做法是先用 `np.empty` 按最大容量预分配一整块数组，再用切片 `buf[pos:pos+step] = new_row` 原地写入——这正是下文 KV Cache 的标准写法。
  2. **善用 `out=` 复用缓冲与数值稳定函数**：逐元素数学函数支持传入目标缓冲（如 `np.exp(scores, out=scores)`），省掉中间临时数组分配；计算微小量对数与指数差时优先调用 `np.log1p(x)` 与 `np.expm1(x)` 抑制有效数字相消；校验浮点相等永远用 `np.allclose(actual, expected, rtol=1e-5, atol=1e-7)` 替代裸 `==`。
  3. **Top-K 选择不必全量排序**：只需要找出得分最高的前 \(k\) 个词元下标时，`np.argsort` 会付出完整的 \(O(N \log N)\) 排序成本，而 `np.argpartition(scores, -k)[-k:]` 基于快速选择算法只需 \(O(N)\) 平均时间即可把最大的 \(k\) 项归拢到末尾。
  4. **独立可复现随机流与张量缩并**：新代码一律使用局部实例 `rng = np.random.default_rng(42)` 取代污染全局状态的 `np.random.seed`，多进程数据加载可通过 [`rng.spawn(4)`](https://numpy.org/doc/stable/reference/random/generated/numpy.random.Generator.spawn.html) 派生统计独立的子流；复杂多维缩并可用 `np.einsum("bhsd,bhtd->bhst", q, k)` 清晰表达下标语义，但在性能敏感路径上仍需对比显式 `transpose + matmul` 是否能更直接地命中底层 BLAS `GEMM` 快路径。

---

## 三、用纯 NumPy 写透参考实现：泊松方程、单层 LLM 与张量生态

NumPy 在现代 AI 与科学计算工程里最不可替代的角色，就是充当 **绝对透明的参考实现**（Reference Implementation）：没有异步流延迟、没有编译器图改写，每一个中间数组的形状、步长与浮点误差都能直接摊在桌面上检验。下面用两个跨领域的完整实例把前面的机制全部串起来。

### 1. 一维泊松方程中心差分：看清稠密与稀疏的复杂度边界

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

其中 [RMSNorm](https://arxiv.org/abs/1910.07467) 省去了均值中心化，仅按最后一维均方根重缩放；位置信息通过前文介绍的复数视图 [RoPE](https://arxiv.org/abs/2104.09864) 注入 `Q` 和 `K`；门控前馈网络采用 [SwiGLU](https://arxiv.org/abs/2002.05202)。推理过程中最关键的状态管理在于 `KVCache`：

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

### 3. 张量生态全景：长得像 NumPy，底层改写了哪条契约？

理解了 `ndarray` 其实是“**CPU 主机内存 + 可变稠密缓冲 + 自由步长视图 + 同步立即执行**”这四条假设的组合体，再看整个 Python 数据与深度学习生态，就能一眼看穿所谓“NumPy 兼容（NumPy-Compatible）”背后的真实差异——每一个周边框架，都是为了突破某一个工程瓶颈，主动打破了其中至少一条假设：

| 数组/张量生态 | 在 `ndarray` 模型之上新增的核心能力 | 打破或改写的底层执行契约 |
| --- | --- | --- |
| **pandas** | 行列标签索引（`Index`）、异构表格列、原生缺失值与时间序列窗口 | `a + b` **先按标签对齐再运算**（而非纯按位置广播）；`to_numpy()` 遇异构列或缺失值常触发复制 |
| **SciPy** | 稀疏矩阵存储（CSR/CSC/DIA）、数值优化、信号滤波与科学求解器 | 稀疏数组只存非零值与坐标索引，不再支持任意 \(O(1)\) 步长切片或自由 `reshape` |
| **PyTorch** | GPU/加速器设备管理、动态自动微分图（Autograd）、神经网络与分布式通信 | `Tensor` 绑定 `device` 与梯度历史；`tensor.view()` 遇非连续布局会**直接报错**而非像 `reshape` 自动复制 |
| **JAX** | `jit` 编译融合、`grad`、`vmap` 批量化变换与 XLA 多设备并行 | 数组**严格不可变**（Immutable）；要求编译期静态形状（`nonzero`/`unique` 需定长约束），执行默认异步分派 |
| **CuPy** | 驻留 NVIDIA 显存的 GPU 数组、内联 CUDA 内核与高度兼容的算子接口 | 跨 PCIe 总线传输与异步流同步成为头号性能变量；裸指针内核不自动解析非连续 `strides` |
| **Apache Arrow** | 跨语言标准列式内存格式、有效性位图（`NULL`）、偏移量变长文本与嵌套结构 | 面向进程/系统间零拷贝交换的**不可变列缓冲**，本身不承担通用稠密矩阵计算引擎职责 |

看清这张对照表后，下面两个最关键的疑问自然浮现出来：第一，从纯前向推理进入神经网络训练，PyTorch 的 `loss.backward()` 究竟替我们藏起了哪些内存驻留与数值稳定性工作？第二，当数组跨出主机内存进入 CuPy/CUDA 显存，打破“主机内存 + 同步执行 + 自动解步长”这三条契约后，一线代码里到底会撞上什么真实陷阱？接下来我们分两步亲手把这两块盖板掀开。

---

## 四、脱离框架手写两层 MLP：数据构造、防泄漏与向量化热身

在 PyTorch 里训练一个分类器，核心循环往往浓缩成轻描淡写的三行：`logits = model(x)`、`loss = criterion(logits, y)`、`loss.backward()`。正因为自动求导引擎把脏活累活全包圆了，一旦后续在真实大模型上撞见显存随序列长度暴涨、半精度训练突然蹦出 `NaN`，或者模型死活不收敛，很多习惯了调框架的人一时间根本不知道该去掀哪一块盖板。

撤掉框架、只留 `ndarray` 从零写一个两层多层感知机（Multilayer Perceptron，MLP），`loss.backward()` 幕后的三件硬核工作就会立刻水落石出：

1. **为什么前向激活必须驻留内存？** 根据多元复合函数链式法则，反向传播计算第二层权重梯度 \(\partial L / \partial W_2 = A_1^T dZ_2\) 以及第一层激活门控 \(\partial L / \partial Z_1 = dA_1 \odot \mathbb{1}[Z_1 > 0]\) 时，手里必须攥着前向阶段算出来的隐藏层输出 \(A_1\)、预激活 \(Z_1\) 和原始输入 \(X\)。推理算完一层就能立刻复用内存，训练却非把沿途激活全部缓存下来不可——这就是训练显存远大于推理显存的物理根源。
2. **为什么教科书公式直接敲成代码会溢出崩溃？** 朴素的 \(\exp(z_i) / \sum_j \exp(z_j)\) 只要遇到一个稍大的未归一化分数（`float64` 下 \(z_i > 710\)，`float32` 下 \(z_i > 89\)），指数项就会瞬间冲出浮点表示上限变成 `inf`，紧接着 `inf / inf` 把整批梯度全部染成 `NaN`。利用平移不变性在求指数前先减去每行最大值，是保证训练不炸锅的生死线。
3. **怎么在不靠框架的前提下证明反向传播没写错？** 漏除 `batch_size`、偏置归约搞错轴，甚至原地修改了前向缓存的概率数组，随机梯度下降有时依然能跌跌撞撞把训练误差往下推，但收敛速度与超参数尺度早已失真。引入中心有限差分（Finite Difference）梯度检查，用 \((L(\theta+\varepsilon) - L(\theta-\varepsilon)) / 2\varepsilon\) 独立标定斜率，就能把“凭感觉看曲线”变成 `1.51e-11` 级别的确定性数学验证。

作为 [《面向 RTX 5070 Ti 的 LLM 实践学习路线》]({{< relref "2026-08-15-gpu-to-llm-finetuning-roadmap.md" >}}) 第一阶段的动手验收项目，下面不调用任何机器学习库，纯靠 NumPy 跑通二维双月牙非线性分类的全链路。配套交互式 Notebook 已内嵌在下方，可直接在浏览器中修改超参数并重新运行全部单元格（Matplotlib 仅用于最终绘制曲线与决策边界，不参与任何数值计算）：

{{< notebook src="/lab/notebooks/index.html?path=numpy-two-layer-mlp.ipynb" download="/notebooks/numpy-two-layer-mlp.ipynb" title="NumPy 两层 MLP：完整实验 Notebook" >}}

### 1. 构造双月牙数据与防泄漏预处理

之所以挑选二维双月牙（Two Moons）而非高维图像数据集作为练手靶子，是因为两弯交错的半圆在平面上 **严格线性不可分**——任何单层线性分类器都只能画出一条笔直的切线，模型必须借助隐藏层与 ReLU 非线性弯折空间才能分开两类样本；同时二维输入允许我们把 `48,400` 个密集网格点直接喂进网络，肉眼验收最终学出来的决策边界曲率。

不借道 `scikit-learn`，直接用三角函数、高斯噪声与排列索引拼出 `1,200` 条样本：

```python
def make_moons_numpy(n_samples=1200, noise=0.22, seed=42):
    rng = np.random.default_rng(seed)                 # ① 局部独立随机数生成器，隔离全局状态
    n_first = n_samples // 2                          # ② 第一类样本数量
    n_second = n_samples - n_first                    # ③ 第二类承接余数，兼容奇数样本总数

    first_angle = np.linspace(0.0, np.pi, n_first)    # ④ 在 [0, π] 等距采样上半圆角度
    second_angle = np.linspace(0.0, np.pi, n_second)  # ⑤ 下半圆角度序列
    first = np.c_[                                    # ⑥ 按列拼接 cos 与 sin，得到 (n_first, 2)
        np.cos(first_angle),
        np.sin(first_angle),
    ]
    second = np.c_[                                   # ⑦ 翻转并平移第二个半圆形成交错结构
        1.0 - np.cos(second_angle),
        0.5 - np.sin(second_angle),
    ]

    x = np.vstack([first, second])                    # ⑧ 沿第 0 轴纵向合并为 (N, 2)
    x += rng.normal(0.0, noise, size=x.shape)         # ⑨ 叠加同形状高斯噪声
    y = np.concatenate([                              # ⑩ 生成 int64 类别下标标签 (N,)
        np.zeros(n_first),
        np.ones(n_second),
    ]).astype(np.int64)

    order = rng.permutation(n_samples)                # ⑪ 生成单份随机置换下标
    return x[order], y[order]                         # ⑫ 用同一份 order 同步重排特征与标签
```

这里最值得留意的是 **①、⑩ 与 ⑪⑫**：局部 `default_rng(seed)` 确保后续改动数据噪声时不会意外扰动模型权重的初始化序列；标签 `y` 显式转为 `int64`，供后面交叉熵直接做二维整数高级索引；而 `x[order], y[order]` 共用同一个置换数组，杜绝了新手分别打乱特征和标签导致监督信号彻底错位却不报任何语法错误的隐蔽陷阱。

接着按类别分层切出 `900` 条训练样本与 `300` 条验证样本，保证两边正负样本比例严格一致：

```python
def stratified_split(x, y, val_ratio=0.25, seed=42):
    rng = np.random.default_rng(seed)                 # ① 切分随机源独立可控
    train_indices, val_indices = [], []               # ② 暂存各类别的训练/验证下标

    for label in np.unique(y):                        # ③ 逐个类别内部独立切分
        label_indices = np.flatnonzero(y == label)    # ④ 布尔掩码转一维命中下标
        rng.shuffle(label_indices)                    # ⑤ 仅打乱当前类别内部顺序
        val_count = int(label_indices.size * val_ratio)  # ⑥ 当前类划入验证集的样本数
        val_indices.append(label_indices[:val_count])    # ⑦ 前段归入验证集
        train_indices.append(label_indices[val_count:])  # ⑧ 剩余归入训练集

    train_indices = np.concatenate(train_indices)     # ⑨ 合并全部类别的训练下标
    val_indices = np.concatenate(val_indices)         # ⑩ 合并全部类别的验证下标
    rng.shuffle(train_indices)                        # ⑪ 二次打乱，消除按类别成块排列
    rng.shuffle(val_indices)
    return (                                           # ⑫ 高级索引一次性切出四份紧凑副本
        x[train_indices],
        y[train_indices],
        x[val_indices],
        y[val_indices],
    )
```

切分完成后得到的形状为 `train: (900, 2) / (900,)` 与 `val: (300, 2) / (300,)`。紧随其后的特征标准化藏着两条铁律：

```python
mean = x_train.mean(axis=0, keepdims=True)             # ① 沿样本轴求每列均值，保留 (1, 2)
std = x_train.std(axis=0, keepdims=True)               # ② 沿样本轴求每列标准差，保留 (1, 2)

x_train = ((x_train - mean) / std).astype(np.float64) # ③ (900, 2) 与 (1, 2) 零步长广播
x_val = ((x_val - mean) / std).astype(np.float64)     # ④ 验证集强制复用训练集的 mean 与 std！
```

`keepdims=True` 把两列特征的均值和标准差显式钉在 `(1, 2)` 形状上，语义上一眼就能看出是“一行按列统计的参数”；更重要的是第 **④** 行——验证集绝不允许重新调用 `.mean()` 和 `.std()` 拟合自身分布，否则就等于把测试期才该出现的统计信息提前泄漏进了预处理管线。

### 2. 向量化热身：先断言数值等价，再谈调度加速

正式写网络之前，先用一组对照实验量一量向量化的真实收益：计算 `10,000` 个 `128` 维特征点到单个查询向量 `query: (128,)` 的欧氏距离。纯 Python 循环写法每次只送一行进去：

```python
distance = np.empty(points.shape[0])                  # ① 预分配 (10000,) 连续结果数组
for i in range(points.shape[0]):                     # ② Python 解释器循环分派 10,000 次
    delta = points[i] - query                        # ③ 单样本 (128,) 减法
    distance[i] = np.sqrt(np.sum(delta ** 2))        # ④ 归约写入单槽位
```

而向量化写法依靠广播与指定轴归约，把一万条样本合并成单次底层 C 循环调用：

```python
delta = points - query                               # ⑤ (128,) 沿第 0 轴零步长广播至 (10000, 128)
squared = delta ** 2                                 # ⑥ 整块连续内存逐元素平方
sum_per_row = np.sum(squared, axis=1)                # ⑦ 沿特征轴 axis=1 归约为 (10000,)
distance = np.sqrt(sum_per_row)                      # ⑧ 批量开方得到全部距离
```

计时对比之前，先用 `np.testing.assert_allclose(loop_distance, vector_distance)` 锁死两套写法的浮点一致性——没有正确性兜底的“提速”，十有八九只是不小心改错了归约轴。在本机原生 CPython 实测中，Python 循环耗时约 `0.1010s`，向量化版本耗时约 `0.0196s`（快约 `5.1x`）。把成千上万次碎片化的解释器调度压成少数几次大块连续内存内核执行，这一原则从 CPU 上的 NumPy 一路贯通到后面的 PyTorch 与 CUDA 算子优化。

---

## 五、手写两层 MLP：前向缓存、稳定 Softmax 与反向传播

设微批次样本数为 \(B\)，输入特征维 \(D = 2\)，隐藏层宽度 \(H = 32\)，输出类别数 \(C = 2\)。两层 MLP 的前向映射由两步仿射变换夹一层逐元素 ReLU 激活与行归一化 Softmax 组成：

$$
Z_1 = X W_1 + b_1, \quad A_1 = \operatorname{ReLU}(Z_1), \qquad Z_2 = A_1 W_2 + b_2, \quad P = \operatorname{softmax}(Z_2)
$$

对 Softmax 与交叉熵损失 \(L = -\frac{1}{B}\sum_{i=1}^B \log P_{i, y_i}\) 联合求导，输出层未归一化分数 \(Z_2\) 处的梯度可以化简成一个极其干净的闭式解，随后顺着计算图逆向回传至每一组权重与偏置：

$$
dZ_2 = \frac{P - \operatorname{onehot}(y)}{B}, \qquad dW_2 = A_1^T dZ_2, \quad db_2 = \sum_{i=1}^{B} dZ_2^{(i)}
$$

$$
dA_1 = dZ_2 W_2^T, \qquad dZ_1 = dA_1 \odot \mathbb{1}[Z_1 > 0], \qquad dW_1 = X^T dZ_1, \quad db_1 = \sum_{i=1}^{B} dZ_1^{(i)}
$$

把正向的形状流转、中间横亘的 `ForwardCache` 状态驻留层，以及反向严格镜像对称的矩阵转置与按轴归约画在同一张架构图上，前向与反向之间的精确耦合关系便一目了然：

![两层 MLP 前向 Shape 变换、ForwardCache 缓存与反向梯度链式传播：上方左至右展示 (B,2)→(B,32)→(B,2) 前向变换，中部驻留 X、Z1、A1 与 P 四组缓存张量，下方右至左完成联合梯度回传、ReLU 门控截断与参数梯度归约](/images/diagrams/aug-ai-mlp-forward-backward.svg)

### 1. He 初始化、显式 `ForwardCache` 与数值稳定 Softmax

针对会截断负半轴信号的 ReLU 激活函数，权重采用按输入扇入维（`fan_in`）缩放的 He 正态初始化 \(\mathcal{N}(0, 2/\text{fan\_in})\)，避免层数加深时前向激活方差萎缩或爆炸：

```python
def init_params(input_dim, hidden_dim, output_dim, seed=42):
    rng = np.random.default_rng(seed)                    # ① 固定初始化种子，保证对照实验同起点
    w1 = rng.normal(                                     # ② 首层按输入维 D=2 缩放：(2, 32)
        loc=0.0,
        scale=np.sqrt(2.0 / input_dim),
        size=(input_dim, hidden_dim),
    )
    b1 = np.zeros(hidden_dim)                             # ③ 首层偏置置零：(32,)
    w2 = rng.normal(                                     # ④ 二层扇入已变为隐藏维 H=32：(32, 2)
        loc=0.0,
        scale=np.sqrt(2.0 / hidden_dim),
        size=(hidden_dim, output_dim),
    )
    b2 = np.zeros(output_dim)                             # ⑤ 输出层偏置置零：(2,)
    return MLPParams(w1, b1, w2, b2)                     # ⑥ 封装为具名 dataclass，杜绝传参错序
```

前向函数除算出预测概率外，还显式打包返回 `ForwardCache`（以 `B=8` 为例，`x:(8,2) → z1/a1:(8,32) → logits/probs:(8,2)`）：

```python
def forward(x, params):
    z1 = x @ params.w1 + params.b1       # ① (B,2) @ (2,32) + (32,) -> (B,32)
    a1 = relu(z1)                        # ② 逐元素 max(0, z1)，引入非线性弯折能力
    logits = a1 @ params.w2 + params.b2  # ③ (B,32) @ (32,2) + (2,) -> (B,2)
    probs = softmax(logits)              # ④ 沿类别维归一化为概率分布
    return ForwardCache(                 # ⑤ 显式留存反向链式求导所需的全部中间张量
        x=x,
        z1=z1,
        a1=a1,
        logits=logits,
        probs=probs,
    )
```

计算 Softmax 时，对同一行分数同时减去该行最大值 \(m_i = \max_j z_{i,j}\)，分子分母同乘 \(e^{-m_i}\) 严格约分，数学结果分毫不差，而传入 `np.exp` 的最大实参被死死摁在 `0`（即 \(\exp(0) = 1\)），彻底根除了指数上溢：

```python
def softmax(logits):
    row_max = np.max(logits, axis=1, keepdims=True)  # ① 求每条样本的最大分数：(B, 1)
    shifted = logits - row_max                       # ② 广播平移，确保最大元素为 0
    exp = np.exp(shifted)                            # ③ 安全求指数，值域落入 (0, 1]
    denominator = np.sum(exp, axis=1, keepdims=True) # ④ 沿类别轴求和并保持 (B, 1)
    return exp / denominator                         # ⑤ 广播相除，保证每行概率和等于 1.0
```

紧接着计算交叉熵损失时，无需浪费内存去构造一整张 `(B, C)` 的稀疏 One-Hot 矩阵，直接用行号数组配标签数组做二维高级索引即可精准抽出正确类别的预测概率：

```python
def cross_entropy_loss(probs, y):
    rows = np.arange(y.shape[0])                     # ① 构造样本行下标 [0, 1, ..., B-1]
    correct_probs = probs[rows, y]                   # ② 抽出每条样本在真实类别 y[i] 上的概率 (B,)
    safe_probs = correct_probs + 1e-12               # ③ 加极小量 1e-12 防止极端误判触发 log(0)
    return float(-np.mean(np.log(safe_probs)))       # ④ 求批次平均负对数似然
```

### 2. 逐行拆解 `backward`：三处最容易踩中的静音地雷

顺着前面的矩阵求导公式倒推，11 行代码就能写完整个两层网络的反向传播：

```python
def backward(cache, y, params):
    batch_size = y.shape[0]                           # ① 读取当前微批次大小 B

    dlogits = cache.probs.copy()                      # ② 必须深拷贝！严禁原地污染前向概率缓存
    dlogits[np.arange(batch_size), y] -= 1.0         # ③ 在真实类别列原地减 1，等价于 P - onehot(y)
    dlogits /= batch_size                             # ④ 匹配前向 np.mean，把梯度同步除以 B

    dw2 = cache.a1.T @ dlogits                        # ⑤ (32, B) @ (B, 2) -> (32, 2)，与 w2 同形
    db2 = np.sum(dlogits, axis=0)                     # ⑥ 沿样本轴求和归约 -> (2,)，与 b2 同形
    da1 = dlogits @ params.w2.T                       # ⑦ (B, 2) @ (2, 32) -> (B, 32)，传回隐藏层
    dz1 = da1 * (cache.z1 > 0.0)                     # ⑧ 乘布尔掩码，截断 Z1 <= 0 处的负半轴梯度
    dw1 = cache.x.T @ dz1                             # ⑨ (2, B) @ (B, 32) -> (2, 32)，与 w1 同形
    db1 = np.sum(dz1, axis=0)                        # ⑩ 沿样本轴求和归约 -> (32,)，与 b1 同形

    return MLPParams(dw1, db1, dw2, db2)             # ⑪ 返回与参数结构完全一致的梯度对象
```

这短短十几行里藏着三条写数值内核受用终身的经验：

- **前向广播对应反向求和**（⑥与⑩）：前向算 `x @ w1 + b1` 时，一维偏置 `b1:(32,)` 被零步长广播加到了全部 `B` 条样本上；根据多元全微分法则，一个变量分叉参与了 `B` 条路径，反向回传时就必须沿 `axis=0` 把这 `B` 条样本传回的偏导数全部加总起来，维度也恰好缩回 `(32,)`。
- **拒绝原地篡改缓存**（②）：如果贪图省事写成 `dlogits = cache.probs`，第 ③ 行的 `-= 1.0` 会直接把 `ForwardCache` 里的正确类别概率改成负数！一旦外部日志或评估逻辑再去读 `cache.probs`，就会看到荒诞的负概率。
- **损失归约口径严格对齐**（④）：既然前向损失用了 `np.mean` 除以 `B`，反向起点 `dlogits` 就必须除以 `B`。漏写这行代码程序照跑不误，但梯度模长会平白放大 `B` 倍——一旦后面调大 `batch_size`，原本稳定的学习率就会突然把模型训崩。

---

## 六、有限差分梯度检查、训练诊断与向 PyTorch 交棒

手写完 `backward`，绝不急着直接上大循环调学习率。我们先用只依赖前向损失函数定义的中心有限差分公式，给反向求导做一次严格的“数学体检”：

$$
\frac{\partial L}{\partial \theta_k} \approx \frac{L(\theta + \varepsilon e_k) - L(\theta - \varepsilon e_k)}{2\varepsilon}
$$

相比单侧差分 \((L(\theta+\varepsilon) - L(\theta)) / \varepsilon\) 的一阶截断误差 \(O(\varepsilon)\)，中心差分利用泰勒展开奇次项相消，将截断误差直接压到了二阶 \(O(\varepsilon^2)\)。将双路梯度核验门禁与后续受控 Mini-batch SGD 训练、学习率消融串起来，整套验证与实验闭环如下：

![中心有限差分梯度检查与受控训练闭环：先在 float64 微型网络上并行求解手写解析梯度与 ±1e-5 中心差分数值斜率，通过 1.51e-11 误差断言后再进入 400 轮 Mini-batch 训练与学习率单变量对照](/images/diagrams/aug-ai-mlp-gradcheck-loop.svg)

### 1. `1.51e-11` 级精度的 `gradient_check`

因为每抽查一个标量参数就要重跑两次前向传播，梯度检查专门在一个固定随机种子的 `float64` 微型网络（`5` 条 `3` 维样本，`3 → 4 → 3` 结构）上执行，覆盖全部四组参数张量：

```python
def gradient_check(seed=7):
    rng = np.random.default_rng(seed)                 # ① 固定微型检验问题
    tiny_x = rng.normal(size=(5, 3))                  # ② 构造 float64 双精度输入
    tiny_y = np.array([0, 1, 2, 1, 0], dtype=np.int64)
    params = init_params(3, 4, 3, seed)               # ③ 3→4→3 微型网络，避开 ReLU 零点奇异区
    grads = backward(forward(tiny_x, params), tiny_y, params)  # ④ 一次算出待测解析梯度
    epsilon = 1e-5                                    # ⑤ 平衡截断误差与双精度舍入误差的步长
    max_error = 0.0                                   # ⑥ 记录最坏绝对偏差

    checks = [                                        # ⑦ 四组权重与偏置逐一抽样覆盖
        ("w1", (0, 0)), ("w1", (2, 3)), ("b1", (1,)),
        ("w2", (0, 1)), ("w2", (3, 2)), ("b2", (2,)),
    ]
    for name, index in checks:
        parameter = getattr(params, name)             # ⑧ 取出目标参数 ndarray
        original = parameter[index]                   # ⑨ 备份原始标量值

        parameter[index] = original + epsilon         # ⑩ 施加 +ε 正向微扰
        loss_plus = cross_entropy_loss(forward(tiny_x, params).probs, tiny_y)
        parameter[index] = original - epsilon         # ⑪ 施加 -ε 负向微扰
        loss_minus = cross_entropy_loss(forward(tiny_x, params).probs, tiny_y)
        parameter[index] = original                   # ⑫ 立即恢复原值，严防污染下一个待检元素！

        numerical = (loss_plus - loss_minus) / (2.0 * epsilon)  # ⑬ 中心有限差分斜率
        analytical = getattr(grads, name)[index]      # ⑭ 同坐标的手写解析梯度
        max_error = max(max_error, abs(numerical - analytical)) # ⑮ 更新最坏误差
    return max_error
```

运行输出 `gradient check max abs error: 1.51e-11`！在双精度浮点与 \(\varepsilon = 10^{-5}\) 下拿到 \(10^{-11}\) 量级的吻合度，意味着转置方向、`axis=0` 求和与 `/ batch_size` 全部严丝合缝（若使用单精度 `float32`，加减 \(10^{-5}\) 极易被 23 位尾数截断吞没，且若扰动区间恰好跨过 \(Z_1 = 0\) 的 ReLU 折点也会引起折线斜率跳变）。

### 2. 400 轮 Mini-batch 训练、决策边界与单变量学习率消融

梯度验明无误后，用生成器按轮打乱下标切分微批次（支持末尾不足一整批的尾包），跑满 400 轮训练：

```python
def iterate_minibatches(x, y, batch_size, rng):
    indices = rng.permutation(x.shape[0])             # ① 每轮仅重排一维下标，不整表复制特征
    for start in range(0, x.shape[0], batch_size):   # ② 兼容样本数无法被 batch_size 整除的尾批
        batch_indices = indices[start:start + batch_size]
        yield x[batch_indices], y[batch_indices]      # ③ 按需切出当前批次的特征与标签
```

```python
params = init_params(...)                             # ① 训练开始前初始化一次权重
rng = np.random.default_rng(seed)                    # ② 独立管理微批次打乱随机流
history = {"train_loss": [], "train_acc": [], "val_loss": [], "val_acc": []}

for epoch in range(1, epochs + 1):
    for xb, yb in iterate_minibatches(x_train, y_train, batch_size, rng):
        cache = forward(xb, params)                   # ③ 前向计算并保存激活缓存
        grads = backward(cache, yb, params)           # ④ 反向链式求导
        update(params, grads, learning_rate)          # ⑤ 原地执行 params -= lr * grads

    train_loss, train_acc = evaluate(x_train, y_train, params)  # ⑥ 全量评估不更新参数
    val_loss, val_acc = evaluate(x_val, y_val, params)
    history["train_loss"].append(train_loss)
    history["train_acc"].append(train_acc)
    history["val_loss"].append(val_loss)
    history["val_acc"].append(val_acc)
```

第 400 轮收敛至 `train_loss=0.0945, train_acc=0.956 / val_loss=0.0782, val_acc=0.967`，训练与验证损失同步平滑下降，无过拟合分离迹象。为了直观看到隐藏层的 32 个 ReLU 神经元是如何通过折线拼接出平滑曲线边界的，在二维特征平面上铺开 `220 × 220 = 48,400` 个密集采样点，一次向量化前向推理画出类别概率等高线：

```python
xx, yy = np.meshgrid(np.linspace(x_min, x_max, 220), np.linspace(y_min, y_max, 220))
grid = np.c_[xx.ravel(), yy.ravel()]                 # 拼成 (48400, 2) 批量特征矩阵
class_one_probability = forward(grid, params).probs[:, 1].reshape(xx.shape)
axes[2].contourf(xx, yy, class_one_probability, levels=25, cmap="coolwarm", alpha=0.55)
```

接着锁死初始权重种子、数据集切分与批次打乱顺序，在固定 120 轮预算下仅改变 `learning_rate` 做单变量消融：

```python
learning_rates = [0.005, 0.08, 0.8, 5.0]             # 唯一自变量：跨越四个数量级的步长
lr_histories = {}
for learning_rate in learning_rates:
    _, lr_history = train(                            # 每次从完全相同的初始权重重新起跑！
        x_train, y_train, x_val, y_val,
        epochs=120, learning_rate=learning_rate, verbose=False,
    )
    lr_histories[learning_rate] = lr_history
```

| 学习率（`learning_rate`） | 120 轮训练损失 | 验证集准确率 | 损失曲面上的运动机制分析 |
| ---: | ---: | ---: | --- |
| `0.005` | `0.2902` | `86.7%` | 步幅过小，沿最速下降方向缓慢爬行，120 轮远未走完弯曲谷底 |
| `0.08` | `0.1096` | `97.0%` | 步长与当前标准化特征及 He 初始化曲率匹配，平滑稳定收敛 |
| `0.8` | `0.0965` | `97.3%` | 贴近当前浅层平滑损失面的稳定上限，前中期下降最快 |
| `5.0` | `0.2441` | `83.0%` | 步长跨过局部曲率稳定阈值，参数在峡谷两侧剧烈反弹震荡，边界严重变形 |

注意这里为什么 `lr = 0.8` 依然能稳住，而在深层 Transformer 里学习率通常要设到 \(10^{-4}\) 甚至 \(10^{-5}\)：本实验只有两层全连接、输入经过了严格的零均值单位方差标准化，损失曲面的局部曲率（Hessian 最大特征值）温和得多；一旦网络加深到几十层且叠加注意力点积，梯度范数与曲率尺度陡增，稳定步长区间就会随之向小数量级大幅收缩。

### 3. 从手写组件向 PyTorch 平滑交棒

亲手跑完这一轮，跨进 PyTorch 时框架里的每一个核心类都能在这份百行脚本里找到精准的原身：

| 纯 NumPy 手写组件 | PyTorch 对应抽象 | 框架接管的工作与仍需工程师把关的边界 |
| --- | --- | --- |
| `ndarray` + `MLPParams` | `torch.Tensor` + `nn.Parameter` | 增加 `device` 显存管理与梯度挂载；仍需人工核对广播维度与 `dtype` |
| 显式返回 `ForwardCache` | Autograd 动态计算图节点 | 自动驻留反向所需张量；推理评估漏加 `torch.no_grad()` 会白白撑爆显存 |
| 手写 `backward(cache, y, params)` | `loss.backward()` | 自动逆序链式求导；但梯度默认累加（需手动 `zero_grad()`） |
| `params -= lr * grads` | `optimizer.step()` | 扩展出动量与自适应二阶矩（AdamW）；学习率与权重衰减仍靠实测整定 |
| `iterate_minibatches` 生成器 | `Dataset` + `DataLoader` | 支持多进程预取与锁页内存；切分防泄漏仍归业务代码负责 |
| 中心有限差分 `gradient_check` | `torch.autograd.gradcheck` | 编写自定义 CUDA / Triton 算子 backward 时依然靠同一套双精度差分验真 |

---

## 七、跨过 PCIe 边界：用 CuPy 验证环境门禁与显式数据搬运

到这里为止，无论是单层 Transformer 推理还是两层 MLP 训练，所有的张量计算都待在同一个温室里：CPU 核心透过各级高速缓存直接读写主机内存（Host DRAM），一行 `a @ a.T` 执行返回的那一刻，结果矩阵里的每一个浮点数都已经稳稳落在了内存地址上。

一旦把 `import numpy as np` 换成 `import cupy as cp`，代码长相几乎一模一样，但底层的物理世界已经彻底换了天地——数据不再躺在 CPU 手边，而是必须跨过 **PCIe 总线**搬进显卡独立的 **显存**（VRAM）；算子不再是单线程或几个核上的循环，而是拆成上万个并发线程（Thread）映射到流式多处理器（SM）上；就连 Python 函数返回的那一瞬间，GPU 往往才刚刚从异步命令队列里接过活儿，计算还远未结束。

在直接扎进庞大的 PyTorch 分布式训练之前，CuPy 恰好提供了一块极佳的透明试验田：它既保留了熟悉的数组接口，又允许直接内联原生 CUDA C++ 核函数（Kernel）。只要跑通几个二十行以内的小实验，就能把 GPU 编程最关键的三次心智转换钉死在代码上：

1. **显式的设备内存边界**：数据不会自己瞬移过 PCIe 总线；把 3 个数字搬上 GPU 求和，花在总线往返与核函数发射上的开销远比计算本身昂贵得多。
2. **从多维视图退回裸指针与线程网格**：揭开高级算子外壳，底层只有一维连续指针、`blockIdx.x * blockDim.x + threadIdx.x` 全局编号，以及为了覆盖 `10,003` 个元素而启动 `10,240` 个线程时必不可少的尾部越界保护。
3. **异步发射与真实完成时间解耦**：在 Python 里直接用 `time.perf_counter()` 包住一次 GPU 矩阵乘法，测出来的其实只是往 CUDA 流（Stream）里塞一条指令的微秒级排队耗时，真正的设备耗时必须靠同步屏障或 CUDA 事件（`cudaEvent`）来卡点。

这部分实验需要直接调用宿主机 NVIDIA 驱动，因此请在本地或远端原生 Python CUDA 内核中运行（浏览器内的纯 WebAssembly 环境如 JupyterLite 无法访问物理显卡）。完整逐行中文注释版可在此下载：

- <a href="/code/cupy-cuda-basics/cupy-cuda-basics.ipynb" download>下载配套 Notebook：cupy-cuda-basics.ipynb</a>
- <a href="/code/cupy-cuda-basics/requirements.txt" download>下载本次直接依赖版本：requirements.txt</a>

### 1. 环境分层与验证门禁：为什么 `import cupy` 成功不等于 GPU 可用？

很多人在 WSL（Windows Subsystem for Linux）下配 GPU 环境踩的第一个坑，就是看到 `import cupy` 没报错、甚至整份 Notebook 从头跑到尾都没抛异常，就以为自己已经跑通了 CUDA——殊不知代码可能早就顺着某个 `if not gpu_available:` 分支静默跳过了所有设备计算。

从软件栈自底向上看，WSL 里的 Python GPU 调用其实横跨了三层完全独立的组件：

1. **宿主机驱动层**（Windows NVIDIA Driver）：按照 [CUDA on WSL 官方指南](https://docs.nvidia.com/cuda/wsl-user-guide/index.html)，WSL 内部绝不能再装一套 Linux 显卡驱动，而是直接通过 `/usr/lib/wsl/lib` 映射调用 Windows 宿主机提供的底层驱动接口（`libcuda.so`）。
2. **Linux 开发运行库层**（CUDA Toolkit）：位于 WSL 内的 `/usr/local/cuda`，提供 `cuBLAS` 等数学库以及运行时编译（NVRTC）所需的头文件与动态库。注意 CuPy 的 `RawKernel` 默认走进程内 NVRTC 即时编译，而非每次调命令行 `nvcc`——所以终端敲 `nvcc --version` 有输出，不代表 Python 进程加载对了动态库路径。
3. **Python 虚拟环境层**（Wheel 包）：例如针对 CUDA 13 系列安装的 `cupy-cuda13x==14.2.0`（参考 [CuPy 14.2 安装说明](https://docs.cupy.dev/en/v14.2.0/install.html)，同一个虚拟环境里切勿混装多个 `cupy` 变体）。

正因为这三层随时可能脱节，诊断脚本 `check_cuda.py` 把验证收紧成环环相扣的六道关卡：先确认 Python 包导入成功，接着调用 `cuInit` 完成 CUDA 驱动初始化与设备枚举，随后分配显存完成 `Host → Device` 数组上传，在设备侧跑通真实矩阵乘法与自定义核函数编译，再经 `Device → Host` 同步拷回结果，最后与 NumPy 基准严格比对浮点误差。

这份区分绝非多此一举。翻看本机早先留存的一份失败快照，当时 Python 包导入完全正常（`environment_ready=true`），但严格检查（`--require-gpu`）直接拦截报错：底层 Driver API 调用 `cuInit(0)` 返回 `CUDA_ERROR_NO_DEVICE`，CuPy 抛出 `cudaErrorInsufficientDriver`（`gpu_verified=false`）——这正是典型的宿主机外接显卡链路离线或驱动映射未就绪现场。待硬件枚举恢复后重新复查，无需重装任何驱动，同一套严格检查一次通过。配套 Notebook 也严格沿用了这一准则：无可用显卡时显式打印 `SKIPPED` 以区别于 `PASS`；一旦检测到显卡存在，后续任何编译或计算异常都直接令单元格报错中断，绝不含糊放行。

### 2. 从一次 `3×4` 矩阵乘法与三行销售表看显式数据搬运

先在 CPU 侧用最小的数据集建立一目了然的基准真值，顺手放进一张三行的 `pandas` 销售表，看看日常数据分析里的数据究竟在哪个瞬间才真正踏上显卡：

```python
a_cpu = np.arange(12, dtype=np.float32).reshape(3, 4)  # 创建 0..11，重排为 3 行 4 列。
expected = a_cpu @ a_cpu.T  # (3,4) @ (4,3) 得到 (3,3) 的 CPU 参照矩阵。
sales = pd.DataFrame({"quantity": [2, 5, 3], "price": [10.0, 8.0, 15.0]})  # 创建 CPU 表格。
sales["revenue"] = sales["quantity"] * sales["price"]  # 逐行相乘仍在 CPU 内存完成，不触碰 CUDA。
revenue_cpu = sales["revenue"].to_numpy(dtype=np.float32)  # 抽出底层连续数值列，明确指定 float32。
print("CPU 矩阵乘法：\n", expected)
print("收入合计：", revenue_cpu.sum())  # 20 + 40 + 45 = 105.0
```

手算就能核对 `(3, 4)` 与 `(4, 3)` 相乘得到的 `(3, 3)` 对称矩阵：

```text
[[ 14.  38.  62.]
 [ 38. 126. 214.]
 [ 62. 214. 366.]]
```

哪怕虚拟环境里装好了 CuPy，`pandas` 的 `sales["quantity"] * sales["price"]` 和 `.to_numpy()` 依然百分之百运行在主机内存里。要让数据跨过 PCIe 总线进入显存，必须显式调用 `cp.asarray()`；算完之后要让 NumPy 拿到结果做校验，又必须显式调用 `cp.asnumpy()` 把数据拉回来：

```python
if gpu_ready:  # 仅在设备枚举成功后执行真实 GPU 路径。
    a_gpu = cp.asarray(a_cpu, blocking=True)  # Host → Device：复制至当前 GPU 显存，并阻塞等待上传完毕。
    product_gpu = a_gpu @ a_gpu.T  # Device → Device：在 GPU 上提交 3×3 矩阵乘法。
    result_cpu = cp.asnumpy(product_gpu)  # Device → Host：默认 blocking=True，同步等待计算完成并拷回。
    np.testing.assert_allclose(result_cpu, expected, rtol=1e-5, atol=1e-6)
    revenue_gpu = cp.asarray(revenue_cpu, blocking=True)  # 仅将抽出的 float32 数组上传显存。
    total_gpu = revenue_gpu.sum()  # 在 GPU 上执行归约（Reduction），输出仍是 0 维设备数组。
    total_cpu = cp.asnumpy(total_gpu)  # 哪怕只取回一个标量，同样要走一次同步与跨总线传输！
    np.testing.assert_allclose(total_cpu, revenue_cpu.sum(), rtol=1e-5, atol=1e-6)
    gpu_verified = True
    print("PASS: GPU 矩阵乘法与归约结果均匹配 NumPy。")
else:
    print("SKIPPED:", gpu_reason)
```

短短十行代码里，藏着初学者最容易忽略的三处设备边界语义：

| 关键调用 | 物理数据流向 | 底层同步与内存语义 |
| --- | --- | --- |
| [`cp.asarray(a_cpu, blocking=True)`](https://docs.cupy.dev/en/v14.2.0/reference/generated/cupy.asarray.html) | **Host DRAM → GPU VRAM** | 若输入本就是兼容的 CuPy 数组则直接零拷贝复用；此处输入为 NumPy 数组，触发跨 PCIe 拷贝。CuPy 14.2 默认 `blocking=False`，此处显式传 `True` 确保返回时源主机缓冲已安全读完 |
| `a_gpu @ a_gpu.T` | **GPU VRAM → GPU VRAM** | 输入输出均驻留显存；Python 侧通常只把算子排入当前 CUDA Stream 即刻返回，不阻塞等结果 |
| [`cp.asnumpy(product_gpu)`](https://docs.cupy.dev/en/v14.2.0/reference/generated/cupy.asnumpy.html) | **GPU VRAM → Host DRAM** | 默认 `blocking=True`！它会隐式等待流上此前排队的核函数全部算完、数据完整拷回主机内存后才向 Python 返回 |

再看后面那个把 3 个销售额（`20, 40, 45`）传上显卡算出 `105` 的例子——在工程实践里，这恰恰是一个 **典型的性能反例**：为了加三个浮点数，先后付出了主机到设备拷贝、归约核函数发射、流同步等待以及设备到主机标量回传四笔固定买路钱，速度比 CPU 直接加慢上几个数量级。把它写在这里，就是要直观地破除一个迷思：**API 长得像 NumPy，绝不意味着跨设备搬运的物理延迟消失了；只有计算密集度足够高，或者数据上传一次后能在显存里连续跑完长串算子链路，GPU 的高带宽与高并发才能赚回票价。**（校验时用 `assert_allclose` 显式设好相对容差 `rtol=1e-5` 与绝对容差 `atol=1e-6`，则是为了把正常的浮点累加顺序舍入差异与真正的逻辑错误区分开。）

---

## 八、揭开算子黑盒：`RawKernel` 线程边界与异步流计时陷阱

### 1. 用 `RawKernel` 处理 `10,003` 个元素的线程越界守卫

调 `a_gpu @ a_gpu.T` 时，cuBLAS 替我们包办了所有脏活。那如果我们自己写一个最简单的逐元素向量加法 `out[i] = x[i] + y[i]`，GPU 到底是怎么知道哪个线程该算哪一个下标的？

在 CUDA 的执行模型里，成千上万个线程被分装进一个个定长的 **线程块**（Thread Block），若干个线程块再排成一张 **网格**（Grid）。假设规定每个 Block 固定装 `256` 个线程，而手头要处理的数组长度偏偏是一个除不尽的质数—— **`n = 10003`**。

如果向下取整只开 39 个 Block（`39 × 256 = 9,984` 个线程），末尾 19 个元素就会漏算；所以网格大小必须按整除向上取整：`blocks = (10003 + 256 - 1) // 256 = 40`。但一旦启动了 40 个 Block，硬件实际上拉起了 **`40 × 256 = 10,240` 个线程**——最后那 **237 个线程**（全局编号 `10003` 到 `10239`）根本没有对应的数组元素！把主机端网格划分、一维全局线程号拼接与第 39 号线程块内的边界分流摊开，就能直观看到守卫条件如何拦住尾部越界访问：

![CUDA RawKernel 线程网格映射与尾部越界守卫：40 个 Block 共启动 10,240 个并发线程，经 if (i < 10003) 将前 10,003 个有效读写线程与末尾 237 个空操作退出线程严格分流](/images/diagrams/gpu-agent-cuda-grid-tail-guard.svg)

用 `cp.RawKernel` 把这套映射与守卫直接写成 CUDA C++ 核函数，每一个细节都会水落石出：

```python
if gpu_verified:
    vector_add = cp.RawKernel(
        r"""
        extern "C" __global__  // 禁止 C++ 名称修饰（Name Mangling），声明为可从主机启动的 GPU 核函数。
        void vector_add(const float* x, const float* y, float* out, int n) {
            int i = blockIdx.x * blockDim.x + threadIdx.x;  // 拼出当前线程的一维全局索引。
            if (i < n) {  // 拦截末尾多出来的 237 个越界线程，严禁读写分配边界外的显存！
                out[i] = x[i] + y[i];
            }
        }
        """,
        "vector_add",
    )
    n = 10003  # 故意挑选无法被 256 整除的长度。
    threads = 256  # 每个 Thread Block 包含 256 个线程。
    blocks = (n + threads - 1) // threads  # 向上取整得到 40 个 Block。
    x_gpu = cp.arange(n, dtype=cp.float32)  # 直接在显存内生成连续一维输入。
    y_gpu = cp.ones(n, dtype=cp.float32)
    out_gpu = cp.empty_like(x_gpu)  # 仅分配显存而不清零，核函数必须写满全部 n 个有效槽位！
    vector_add((blocks,), (threads,), (x_gpu, y_gpu, out_gpu, np.int32(n)))  # 传入 Grid、Block 元组与实参。
    expected_add = np.arange(n, dtype=np.float32) + 1
    np.testing.assert_allclose(cp.asnumpy(out_gpu), expected_add)
    print("PASS: RawKernel 向量加法，40 个 Block、10240 个线程、10003 个有效元素。")
else:
    print("SKIPPED: RawKernel 需要先通过真实 GPU 校验。")
```

一旦从高级数组接口退回到 `RawKernel`，有四条平时被 NumPy / CuPy 悄悄兜底的底层铁律就会直接摆到台面上：

1. **尾部边界守卫（`if (i < n)`）不可省略**：没有这行判断，多出来的 237 个尾部线程就会直接往 `out[10003..10239]` 的未分配显存硬写，轻则踩坏相邻张量的数据，重则直接触发 `CUDA_ERROR_ILLEGAL_ADDRESS` 搞崩整个上下文。配套 Notebook 里还专门用 `0, 1, 255, 256, 257, 10003` 六组临界长度做了回归测试（长度为 `0` 时在主机端直接短路返回，避免向 CUDA 发射非法的零 Block 网格）。
2. **输出缓冲未初始化约束**：`cp.empty_like()` 为了省去一次无谓的显存写零开销，拿到的是一块残留着历史脏数据的裸显存。这意味着 `0 .. n-1` 里的每一个下标都必须被恰好一个有效线程覆盖写入；本例将输入 `x, y` 与输出 `out` 分离且按 `i` 一一映射，天然消除了跨线程写冲突。
3. **C ABI 标量位宽严格对齐**：Python 里的普通 `int` 是任意精度对象，传给核函数签名里的 32 位 `int n` 时，显式包一层 `np.int32(n)`（数组同样锁死 `cp.float32` 对应 `float*`），才能杜绝参数压栈错位。
4. **裸指针看不懂视图步长（`strides`）**：这是从 NumPy 转写 CUDA 内核最容易踩的大坑！前面在 NumPy 或高层 CuPy 算子里，转置视图 `x.T` 或隔行切片 `x[::2]` 只是改了元数据里的 `shape` 和 `strides`，算子会自动跳着地址读；但 [CuPy `RawKernel` 文档](https://docs.cupy.dev/en/v14.2.0/reference/generated/cupy.RawKernel.html) 写得很清楚，传进 C++ 核函数的 `const float* x` 只是一根干巴巴的首地址裸指针！如果把非连续切片直接塞进上面的 `vector_add`，核函数依然会傻乎乎地按连续偏移 `x[i]` 往下读，算出彻底错乱的结果——要么在调用前先调 `cp.ascontiguousarray()` 付出一次显存重排代价，要么把各维步长显式作为参数传进核函数自己算偏移。

### 2. 异步流与计时陷阱：为什么不能直接用 `perf_counter` 测算子？

写完核函数，下一步自然想测测 GPU 到底有多快。但如果照搬测试纯 Python 代码的习惯，写出下面这样的计时逻辑：

```python
# 错误示范：缺少流同步的纯墙钟计时！
t0 = time.perf_counter()
res = timed_x @ timed_x.T
elapsed = time.perf_counter() - t0
```

你会测出一个快得离谱的微秒级数字，而且不管矩阵从 `1024×1024` 涨到 `8192×8192`，这个数字几乎纹丝不动！原因很简单：CPU 调用 `timed_x @ timed_x.T` 时，只是往当前 CUDA Stream 的命令队列里塞了一张任务描述符就立刻返回跑下一行 `perf_counter()` 了，此时显卡上的计算核心可能才刚刚开始热身。将 Python 主机线程、CuPy 运行时、PCIe DMA 总线与 GPU CUDA Stream 放在同一张时序图上对照，异步发射失真与真实同步卡点的差异便清晰可见：

![CuPy 异步流发射陷阱与同步计时时序：对比 cp.asarray 显式上传、裸 perf_counter 微秒级排队返回、CUDA Event 流内硬件卡点以及 cp.asnumpy 阻塞回传四段时序](/images/diagrams/gpu-agent-cupy-async-timing.svg)

顺着这张跨设备时序图往下看，要把 GPU 计时做准，必须先说清楚你到底想量哪一段的账，并用对应的同步机制把起点和终点钉死（参考 [CuPy 官方性能测量指南](https://docs.cupy.dev/en/v14.2.0/user_guide/performance.html)）。实验里将最常用的两种统计口径拆开实测：

**口径一：数据已在显存内，用 `CUDA Event` 测连续计算耗时。** 评估模型前向传播中间层算子（输入早已在显存里，输出也继续留在显存供下一层消费）时，先跑几轮预热排掉 cuBLAS 句柄初始化、内核懒加载和显存池首次分配的冷启动抖动，然后在同一个 CUDA Stream 里插入起点与终点两个硬件时间戳事件，最后阻塞等待终点事件真正落定：

```python
if gpu_verified:
    size = 1024
    repeats = 10
    timed_x = cp.random.random((size, size), dtype=cp.float32)  # 输入直接生成在显存内。
    for _ in range(3):  # 预热 cuBLAS 上下文与 CuPy 显存池（Memory Pool）。
        timed_result = timed_x @ timed_x.T
    cp.cuda.get_current_stream().synchronize()  # 排空预热队列，站在干净起点上。

    start_event = cp.cuda.Event()
    end_event = cp.cuda.Event()
    start_event.record()  # 在当前流中打下设备侧起点标记。
    for _ in range(repeats):
        timed_result = timed_x @ timed_x.T
    end_event.record()  # 在 10 次矩阵乘法之后打下终点标记。
    end_event.synchronize()  # 阻塞主机线程，直到 GPU 真正跑完终点标记前的所有工作！

    mean_gpu_ms = cp.cuda.get_elapsed_time(start_event, end_event) / repeats
    print(f"GPU 驻留数据的 matmul 平均时间: {mean_gpu_ms:.3f} ms")
else:
    print("SKIPPED: 没有可用 GPU，不能报告 GPU 性能。")
```

在这台 RTX 5070 Ti 上，上述 `1024×1024` `float32` 矩阵乘法单轮 10 次实测均值为 **`0.123 ms`**（包含 CuPy 算子分派、cuBLAS 调用及显存池复用开销；若需继续下钻单个底层 SASS 内核与 Tensor Core 利用率，则需接入 Nsight Compute 做硬件剖析）。

**口径二：数据从 CPU 来、结果回 CPU 去，测单轮端到端墙钟时间。** 如果业务场景是“主程序在 CPU 内存里准备好一批特征，临时扔给 GPU 算一把矩阵乘法，立刻拿回 NumPy 数组往下走”，只报上面那个 `0.123 ms` 就是自欺欺人了。这时必须把跨 PCIe 上传和同步回传全包进墙钟计时窗里（同时把随机数据生成和事后正确性断言严格排除在计时窗外）：

```python
if gpu_verified:
    rng = np.random.default_rng(0)
    host_x = rng.random((size, size), dtype=np.float32)  # 计时前先在主机内存备好输入。
    cp.cuda.get_current_stream().synchronize()  # 确保前序 GPU 任务已彻底排空。

    wall_start = time.perf_counter()
    device_x = cp.asarray(host_x, blocking=True)  # 计入 Host → Device 跨总线拷贝。
    device_result = device_x @ device_x.T         # 计入 GPU 矩阵乘法。
    host_result = cp.asnumpy(device_result)       # 默认阻塞，计入等待完成与 Device → Host 拷回。
    end_to_end_ms = (time.perf_counter() - wall_start) * 1000

    np.testing.assert_allclose(host_result, host_x @ host_x.T, rtol=1e-4, atol=1e-4)
    print(f"上传 + matmul + 回传，本轮端到端时间: {end_to_end_ms:.3f} ms")
else:
    print("SKIPPED: 端到端 GPU 路径未执行。")
```

把这两个口径放在一起对照，何时该上 GPU、怎么和 CPU 公平比快慢就再清楚不过了：

| 计时口径 | 纳入统计的物理过程 | 排除在外的干扰项 | 对应的真实工程场景 |
| --- | --- | --- | --- |
| **显存驻留 `CUDA Event` 计时** | 10 次矩阵乘法的流上执行与显存池缓冲分配 | PCIe 上传回传、首次库加载与主机侧构造 | 深度学习前向/反向传播、长链路显存驻留流水线 |
| **含传输端到端墙钟计时** | `Host → Device` 拷贝 + 算子执行 + `Device → Host` 同步回传 | 主机随机数生成、`assert_allclose` 校验 | CPU 主流程里临时卸载单个独立算子 |

以后看到任何“GPU 加速了 XX 倍”的报告，第一眼先核查两边是不是站在同一条起跑线上：相同的 `dtype`、相同的数据初始位置，以及同样等到计算真正落地的同步终点。

### 3. 从单算子验证走向访存带宽、算子融合与分层推理

沿着这套实验继续往下深挖，接下来最值得动手验证三个单变量问题：

1. **扫一遍矩阵规模拐点**：固定上述两种计时口径，把矩阵边长从 `32` 一路扫到 `8192`，亲眼看看在哪个临界规模以下，PCIe 传输与内核发射延迟会彻底吃掉 GPU 的算力优势；
2. **对比“反复搬运”与“显存常驻”**：测试把十步连续变换拆成十次 `asarray → 计算 → asnumpy` 往返，和一次上传后在显存里连算十步再拷回的耗时差距；
3. **体验算子融合（Kernel Fusion）**：哪怕数据全在显存里，连写五个逐元素算子也会触发五次显存读写往返；对比 `cp.ElementwiseKernel` 或自定义融合核函数把多次显存读写压成一次寄存器内计算后的带宽收益。

更有意思的是，把微观的百行 CuPy 脚本和真实的大模型推理工程——例如 [《Strata 本地分层推理实践》]({{< relref "2026-10-22-strata-windows-tiered-inference.md" >}})——放在一起看，底层的物理法则竟然完全相通：入门脚本在操心 `1024×1024` 矩阵跨 PCIe 搬运划不划算，而 117B 混合专家推理引擎同样在实测完 `7.2 GB/s` 的 OCuLink 带宽后，决定把八成热专家锁在显存、把未命中的冷专家留给 CPU 原地算而不是跨总线硬搬。不管系统规模放大多少倍，始终追问“**数据此刻躺在哪块物理介质上、步长是否连续、跨界搬运要付多少买路钱、异步流水线在哪里同步落定**”，永远是写好高性能张量代码的第一课。

---

## 运行环境、Notebook 复现与参考资料

### 1. 两份实验 Notebook 复现命令

- **NumPy 两层 MLP（浏览器 / 本地 CPU）**：文中内嵌的 `numpy-two-layer-mlp.ipynb` 基于 Pyodide / WebAssembly 构建（由 `jupyter lite build` 输出至 `public/lab`），点击 `Restart Kernel and Run All Cells` 即可在浏览器沙箱内重跑全部数值验证与绘图；文中记录的 `0.1010s vs 0.0196s` 耗时采自本地原生 CPython 环境。下载后在本地复现只需三行：

```bash
python3 -m venv .venv && source .venv/bin/activate
python -m pip install numpy matplotlib jupyterlab
jupyter lab numpy-two-layer-mlp.ipynb
```

- **CuPy / CUDA 设备边界与异步计时（WSL 原生 NVIDIA GPU）**：将下载的 `cupy-cuda-basics.ipynb` 与 `requirements.txt` 放入同一目录后创建干净虚拟环境并注册内核：

```bash
mkdir -p ~/work/cupy-cuda-basics && cd ~/work/cupy-cuda-basics
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m ipykernel install --sys-prefix \
  --name cupy-cuda-basics --display-name "Python 3 (CuPy CUDA)"
```

当机器并存多套 CUDA 目录时，启动 JupyterLab 时显式指定 `/usr/local/cuda` 与 WSL 驱动桥接目录（连接后选中 `Python 3 (CuPy CUDA)` 内核即可）：

```bash
CUDA_PATH=/usr/local/cuda \
PATH="/usr/local/cuda/bin:$PATH" \
LD_LIBRARY_PATH="/usr/local/cuda/lib64:/usr/lib/wsl/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" \
.venv/bin/python -m jupyterlab \
  --ip=127.0.0.1 --port=8891 --no-browser
```

> **本机复查环境与基线说明**：GPU 实测结果复核于 **2026-10-28**。硬件与环境为 Windows WSL Ubuntu + NVIDIA RTX 5070 Ti（可见显存 `16,303 MiB`，宿主机驱动 `610.88`），WSL 本地工具链 `/usr/local/cuda` 为 CUDA `13.3`（`nvcc 13.3.73`），Python 环境为 `3.12.3`（`numpy 2.5.0`、`pandas 3.0.3`、`jupyterlab 4.6.1`、`cupy-cuda13x 14.2.0`）。CuPy 原始实验记录位于本地仓库 `python/projects/py-numpy-pandas` 基线提交 `42f8853`。

### 2. 参考资料

- [NumPy: The N-dimensional array (`ndarray`)](https://numpy.org/doc/stable/reference/arrays.ndarray.html) & [Copies and views](https://numpy.org/doc/stable/user/basics.copies.html)
- [NumPy: Broadcasting](https://numpy.org/doc/stable/user/basics.broadcasting.html) & [Interoperability with NumPy](https://numpy.org/doc/stable/user/basics.interoperability.html)
- [NumPy: Array API standard compatibility](https://numpy.org/doc/stable/reference/array_api.html) & [NEP 50 — Scalar promotion rules](https://numpy.org/neps/nep-0050-scalar-promotion.html)
- [NumPy: BLAS and LAPACK build configuration](https://numpy.org/doc/stable/building/blas_lapack.html)、[Netlib BLAS](https://netlib.org/blas/) & [LAPACK Users' Guide](https://www.netlib.org/lapack/lug/)
- [pandas: NumPy conversion](https://pandas.pydata.org/docs/user_guide/basics.html)、[SciPy: Sparse arrays](https://docs.scipy.org/doc/scipy/tutorial/sparse.html) & [Apache Arrow: Zero-copy conditions](https://arrow.apache.org/docs/python/pandas.html)
- [PyTorch: `torch.from_numpy`](https://docs.pytorch.org/docs/stable/generated/torch.from_numpy.html)、[JAX: Differences from NumPy](https://docs.jax.dev/en/latest/jax.numpy.html) & [CuPy: Differences between CuPy and NumPy](https://docs.cupy.dev/en/stable/user_guide/difference.html)
- [CUDA on WSL User Guide](https://docs.nvidia.com/cuda/wsl-user-guide/index.html)、[CuPy 14.2 Installation](https://docs.cupy.dev/en/v14.2.0/install.html)、[CuPy `RawKernel`](https://docs.cupy.dev/en/v14.2.0/reference/generated/cupy.RawKernel.html) & [CuPy Performance Best Practices](https://docs.cupy.dev/en/v14.2.0/user_guide/performance.html)
- [LLaMA 2](https://arxiv.org/abs/2307.09288)、[Qwen Technical Report](https://arxiv.org/abs/2309.16609)、[Mistral 7B](https://arxiv.org/abs/2310.06825)、[RMSNorm](https://arxiv.org/abs/1910.07467)、[RoFormer (RoPE)](https://arxiv.org/abs/2104.09864) & [SwiGLU](https://arxiv.org/abs/2002.05202)
