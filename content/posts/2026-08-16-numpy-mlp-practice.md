---
title: "NumPy 项目实践：从向量化到手写两层 MLP 与反向传播"
date: 2026-08-16T00:00:00+08:00
categories:
  - AI Infra
tags:
  - NumPy
  - Neural Network
  - Backpropagation
  - Jupyter
description: "只用 NumPy 完成双月牙数据生成、向量化、两层 MLP、稳定 softmax、手写反向传播、梯度检查与训练诊断，并提供可在浏览器运行的 JupyterLite Notebook。"
draft: false
toc: true
math: true
notebook: true
---

在[《从 GPU 到 LLM 微调：一条面向 RTX 5070 Ti 的实践学习路线》]({{< relref "2026-08-15-gpu-to-llm-finetuning-roadmap.md" >}})中，除了基本的numpy理论学习之外，还希望能够完成：

1. 能判断 shape、broadcasting 和矩阵乘是否合法；
2. 能把逐样本 Python 循环改写为向量化计算；
3. 能实现稳定的 softmax、cross-entropy 和两层 MLP；
4. 不依赖 autograd 写出 `forward → loss → backward → update`；
5. 能用梯度检查和训练曲线证明实现是正确的。

[《深入 NumPy：从 ndarray 内存模型到科学计算与张量生态》]({{< relref "2026-08-16-numpy-internals.md" >}})已经完成了 ndarray、strides、广播、dtype、向量化与 BLAS/LAPACK 的理论铺垫。本文不再重复 API，而是把这些概念放进一个可以运行、可以失败、也可以验证的项目：**只用 NumPy 训练一个两层 MLP，对双月牙数据进行分类。**

本文对应的 Notebook 已保存执行结果，也可以直接在浏览器里修改参数、重启 Kernel 和重新运行。浏览器版本基于 Pyodide/WASM，适合验证算法和数值结果；文中的耗时只代表本次本地 CPU 执行，不应拿来评价本机 OpenBLAS、MKL 或 GPU 性能。

<!--more-->

{{< notebook src="/lab/notebooks/index.html?path=numpy-two-layer-mlp.ipynb" download="/notebooks/numpy-two-layer-mlp.ipynb" title="NumPy 两层 MLP：完整实验 Notebook" >}}

下文代码均直接对应 Notebook 中的 Cell，不是为了讲解重新编造的伪代码。代码里的 `①②③……` 与紧随其后的说明一一对应；Notebook 本身则使用更适合实际阅读的逐行中文注释，解释每个参数、`axis`、广播和 shape 变化。

## 一、项目设计：把框架自动完成的工作全部显式展开

选择二维双月牙数据(数据在坐标系中的分布，类似双月牙)，是因为它不是线性可分问题。一条直线无法把两类样本正确分开，模型必须通过隐藏层和非线性激活学习弯曲的决策边界。相比直接使用 MNIST，这个数据集的输入只有两个维度，数据、错误样本和决策边界都可以直接画出来。

整个实验的数据流是：

```text
双月牙数据
  → 分层切分 train / validation
  → 仅用训练集统计量做标准化
  → Linear(2, 32)
  → ReLU
  → Linear(32, 2)
  → Stable Softmax + Cross Entropy
  → 手写反向传播
  → Mini-batch SGD
```

这里有两个刻意的约束：

- 数据生成、标准化、前向、反向和参数更新全部由 NumPy 完成；
- Matplotlib 只负责绘图，不参与任何模型计算。

为了让 Notebook 能独立运行，也为了把随机数、索引、广播和统计量真正串起来，本实现基于纯 NumPy 实现。

## 二、数据准备：标准化本身也是一次 shape 练习

### 逐行构造双月牙

Notebook 没有调用 `sklearn.datasets.make_moons`，而是把两个半圆的坐标构造完整展开：

```python
def make_moons_numpy(n_samples=1200, noise=0.22, seed=42):
    rng = np.random.default_rng(seed)                 # ① 局部、可复现的随机数生成器
    n_first = n_samples // 2                          # ② 第一类样本数
    n_second = n_samples - n_first                    # ③ 第二类接收余数

    first_angle = np.linspace(0.0, np.pi, n_first)    # ④ 第一段半圆角度
    second_angle = np.linspace(0.0, np.pi, n_second)  # ⑤ 第二段半圆角度
    first = np.c_[                                    # ⑥ 两列拼成 (n_first, 2)
        np.cos(first_angle),
        np.sin(first_angle),
    ]
    second = np.c_[                                   # ⑦ 翻转并平移第二个半圆
        1.0 - np.cos(second_angle),
        0.5 - np.sin(second_angle),
    ]

    x = np.vstack([first, second])                    # ⑧ 沿样本轴合并
    x += rng.normal(0.0, noise, size=x.shape)         # ⑨ 加入同 shape 高斯噪声
    y = np.concatenate([                              # ⑩ 构造整数类别索引
        np.zeros(n_first),
        np.ones(n_second),
    ]).astype(np.int64)

    order = rng.permutation(n_samples)                # ⑪ 生成随机样本排列
    return x[order], y[order]                         # ⑫ 特征与标签同步打乱
```

逐行看这段代码：

1. **①** `default_rng(seed)` 比直接调用全局 `np.random.seed` 更容易控制作用域；数据随机性不会意外影响参数初始化。
2. **②③** 分开计算两类数量，使 `n_samples` 为奇数时仍不会丢样本。
3. **④⑤** `np.linspace(start, stop, num)` 在 `[0,π]` 上生成等距角度，输出是一维数组。
4. **⑥⑦** `np.c_` 按列拼接两个一维数组。`cos` 成为第 0 列，`sin` 成为第 1 列，因此每一行是一条二维样本。第二类通过平移与翻转形成交错月牙。
5. **⑧** `vstack` 在第 0 轴拼接两类，feature shape 从两个 `(N/2,2)` 变成 `(N,2)`。
6. **⑨** `size=x.shape` 保证噪声与每个坐标一一对应；原地加法不改变 shape。
7. **⑩** 分类标签必须是整数索引，因为后续 `probs[rows, y]` 会直接用标签选择正确类别概率。
8. **⑪⑫** 同一个 `order` 同时索引 `x` 和 `y`。如果分别打乱，代码仍能运行，但标签已经与样本错位，是一种不会立即报错的数据错误。

### 逐类切分，避免类别比例漂移

```python
def stratified_split(x, y, val_ratio=0.25, seed=42):
    rng = np.random.default_rng(seed)                 # ① 切分随机性独立可复现
    train_indices, val_indices = [], []               # ② 分类别暂存索引

    for label in np.unique(y):                        # ③ 逐个类别处理
        label_indices = np.flatnonzero(y == label)    # ④ 布尔条件转一维索引
        rng.shuffle(label_indices)                    # ⑤ 只打乱当前类别
        val_count = int(label_indices.size * val_ratio)  # ⑥ 当前类验证数量
        val_indices.append(label_indices[:val_count])    # ⑦ 前段进入验证集
        train_indices.append(label_indices[val_count:])  # ⑧ 余下进入训练集

    train_indices = np.concatenate(train_indices)     # ⑨ 合并各类训练索引
    val_indices = np.concatenate(val_indices)         # ⑩ 合并各类验证索引
    rng.shuffle(train_indices)                        # ⑪ 消除合并后的类别分块
    rng.shuffle(val_indices)
    return (                                           # ⑫ 同步索引特征与标签
        x[train_indices],
        y[train_indices],
        x[val_indices],
        y[val_indices],
    )
```

**③到⑧** 是“分层”的核心：不是先打乱全部数据再切一刀，而是在每个类别内部按相同比例切分。**⑨到⑪** 再把各类别索引合并并打乱，避免训练 batch 先全部看到类别 0、再看到类别 1。**⑫** 使用 NumPy 高级索引一次产出四个数组，四者第 0 轴分别保持训练或验证样本数一致。

数据生成后，先按类别做分层切分，得到 900 条训练样本和 300 条验证样本：

```text
train: (900, 2) (900,)
val:   (300, 2) (300,)
```

标准化只允许使用训练集统计量：

```python
mean = x_train.mean(axis=0, keepdims=True)             # ① 每列均值：(1, 2)
std = x_train.std(axis=0, keepdims=True)               # ② 每列标准差：(1, 2)

x_train = ((x_train - mean) / std).astype(np.float64) # ③ 广播并固定精度
x_val = ((x_val - mean) / std).astype(np.float64)     # ④ 复用训练统计量
```

**①②** 中 `axis=0` 消掉样本轴，只为两列特征分别计算统计量；`keepdims=True` 把结果保留成 `(1,2)`。**③** 中 `(900,2)-(1,2)` 从尾部维度对齐，首维 `1` 广播到 `900`，所以每条样本都减去同一组训练均值。**④** 只能复用训练集的 `mean/std`，不能在验证集重新估计。

`keepdims=True` 并非必需，`(2,)` 同样能广播。但保留二维形式更明确地表达“这是按列统计得到的一行参数”，也能减少扩展到更高维数据时的 shape 歧义。

本次运行结果为：

```text
train mean: [ 0. -0.]
train std:  [1. 1.]
```

验证集必须复用训练集的 `mean` 和 `std`。如果对验证集单独 `fit`，就把验证集分布提前泄漏进了预处理过程。

## 三、向量化：先证明结果相同，再讨论速度

阶段一要求分别用循环和 NumPy 计算 10,000 个 128 维样本到一个查询向量的欧氏距离。逐样本版本是：

```python
distance = np.empty(points.shape[0])                  # ① 预分配 (10000,)
for i in range(points.shape[0]):                     # ② Python 调度 10000 次
    delta = points[i] - query                        # ③ (128,) - (128,)
    distance[i] = np.sqrt(np.sum(delta ** 2))        # ④ 特征归约成标量
```

向量化版本利用 `query: (128,)` 向 `points: (10000, 128)` 广播：

```python
delta = points - query                               # ⑤ (128,) 广播到 (10000,128)
squared = delta ** 2                                 # ⑥ 逐元素平方，shape 不变
sum_per_row = np.sum(squared, axis=1)                # ⑦ 特征轴归约为 (10000,)
distance = np.sqrt(sum_per_row)                      # ⑧ 逐元素开平方
```

**①** 预分配输出避免在循环中不断扩展 Python list；**②到④** 每次只处理一个样本，因此算术由 NumPy 完成，但循环调度仍发生在 Python。**⑤** 是关键：NumPy 将 `query` 看成 `(1,128)` 并沿样本轴扩展；**⑥⑦⑧** 分别对应平方、沿 feature 归约和开平方。Notebook 为了少创建命名变量，把 ⑤到⑧ 合并成一条表达式，执行顺序并没有变化。

性能比较之前先执行 `np.testing.assert_allclose(loop_distance, vector_distance)`，否则“更快”可能只是悄悄改变了计算语义。本次重新执行 Notebook 时，循环耗时约 `0.1010s`，向量化耗时约 `0.0196s`，约为 `5.1x`。

这个数字不是 NumPy 的固定加速比，甚至同一台机器重复执行也会波动：数组规模、CPU cache、系统负载、NumPy 构建方式以及是否产生临时数组都会影响结果。真正重要的是执行边界发生了变化：Python 不再调度 10,000 次逐样本循环，而是把整块数组交给 NumPy 的底层循环处理。进入 PyTorch 和 CUDA 后，减少 Python dispatch、增加单次 kernel 的工作量，仍然是同一条原则。

## 四、前向传播：每条公式都先写出 shape

设 batch size 为 \(B\)，输入维度为 \(D=2\)，隐藏维度为 \(H=32\)，类别数为 \(C=2\)。两层 MLP 的前向传播为：

$$
Z_1 = XW_1 + b_1,\qquad A_1 = \operatorname{ReLU}(Z_1)
$$

$$
Z_2 = A_1W_2 + b_2,\qquad P = \operatorname{softmax}(Z_2)
$$

### 参数初始化逐行拆解

```python
def init_params(input_dim, hidden_dim, output_dim, seed=42):
    rng = np.random.default_rng(seed)                    # ① 初始化可复现
    w1 = rng.normal(                                     # ② 第一层 He 初始化
        loc=0.0,
        scale=np.sqrt(2.0 / input_dim),
        size=(input_dim, hidden_dim),
    )
    b1 = np.zeros(hidden_dim)                             # ③ (H,)
    w2 = rng.normal(                                     # ④ 第二层按 fan-in 缩放
        loc=0.0,
        scale=np.sqrt(2.0 / hidden_dim),
        size=(hidden_dim, output_dim),
    )
    b2 = np.zeros(output_dim)                             # ⑤ (C,)
    return MLPParams(w1, b1, w2, b2)                     # ⑥ 具名返回
```

**①** 让相同 seed 产生相同初始权重，学习率对照才能只改变一个变量。**②** 中 `loc` 是正态分布均值，`scale` 是标准差，`size` 明确参数 shape；ReLU 会截断一部分激活，He 初始化用 `sqrt(2/fan_in)` 控制前向方差。**③⑤** 使用一维 bias，后续加法会沿 batch 轴广播。**④** 的 `fan_in` 已变成隐藏维度 `H`。**⑥** 用 dataclass 按名称组织参数，防止把四个匿名数组的位置顺序传错。

### forward 的六行分别做什么

```python
def forward(x, params):
    z1 = x @ params.w1 + params.b1       # ① (B,D)@(D,H)+(H,) -> (B,H)
    a1 = relu(z1)                        # ② 逐元素非线性，shape 不变
    logits = a1 @ params.w2 + params.b2  # ③ (B,H)@(H,C)+(C,) -> (B,C)
    probs = softmax(logits)              # ④ 每行转换成类别概率
    return ForwardCache(                 # ⑤ 保存 backward 所需状态
        x=x,
        z1=z1,
        a1=a1,
        logits=logits,
        probs=probs,
    )
```

**①** 是第一层仿射变换，`b1` 由 `(H,)` 广播到 `(B,H)`；**②** 如果省略 ReLU，两层矩阵乘仍可合并成一个线性变换，无法学习弯曲边界；**③** 把隐藏特征投影为每个类别的未归一化分数；**④** 只在类别轴归一化；**⑤** 说明手写反向传播需要主动保存输入和中间激活，而 autograd 会替框架用户维护这部分状态。

对应的 shape 是：

| 张量 | Shape | 含义 |
|---|---:|---|
| \(X\) | \((B,D)\) | 一批输入 |
| \(W_1\) / \(b_1\) | \((D,H)\) / \((H,)\) | 第一层参数 |
| \(Z_1\) / \(A_1\) | \((B,H)\) | 隐藏层线性输出与激活 |
| \(W_2\) / \(b_2\) | \((H,C)\) / \((C,)\) | 第二层参数 |
| \(Z_2\) / \(P\) | \((B,C)\) | logits 与类别概率 |

两个 bias 都依赖广播：`b1: (H,)` 被加到 `Z1` 的每一行，`b2: (C,)` 被加到每条样本的 logits。Notebook 对 8 条样本实际打印：

```text
x       (8, 2)
w1      (2, 32)
b1      (32,)
z1      (8, 32)
a1      (8, 32)
w2      (32, 2)
b2      (2,)
logits  (8, 2)
probs   (8, 2)
```

### 稳定 softmax 不是可选优化

直接计算 \(e^{z_i}\) 会在 logits 很大时溢出。softmax 对所有 logits 同时平移一个常数不会改变结果，所以实现时先减去每行最大值：

```python
def softmax(logits):
    row_max = np.max(logits, axis=1, keepdims=True)  # ① 每条样本的最大分数：(B,1)
    shifted = logits - row_max                       # ② 广播回 (B,C)
    exp = np.exp(shifted)                            # ③ 最大指数固定为 exp(0)=1
    denominator = np.sum(exp, axis=1, keepdims=True) # ④ 每行归一化因子：(B,1)
    return exp / denominator                         # ⑤ 广播除法，输出 (B,C)
```

**①②** 利用 softmax 的平移不变性，把每行最大值移到 0；**③** 因而不会计算大于 1 的指数；**④** 中 `axis=1` 表示类别维，`keepdims=True` 保证分母是 `(B,1)`；**⑤** 再沿类别维广播，使输出每行和为 1。

cross-entropy 没有构造完整 one-hot 矩阵，而是直接索引正确类别：

```python
def cross_entropy_loss(probs, y):
    rows = np.arange(y.shape[0])                     # ① [0,1,...,B-1]
    correct_probs = probs[rows, y]                   # ② 每行选择标签对应概率
    safe_probs = correct_probs + 1e-12               # ③ 防止 log(0)
    return float(-np.mean(np.log(safe_probs)))       # ④ batch 平均负对数似然
```

**②** 是二维高级索引：`rows` 决定行，`y` 决定该行的类别列，输出 shape 为 `(B,)`。**③** 是数值保护，不改变正常概率的有效精度。**④** 先逐样本取负对数，再对 batch 求均值，所以 backward 中也必须除以 `B`。

## 五、反向传播：autograd 到底替我们做了什么

对 softmax 和 cross-entropy 联合求导，可以得到：

$$
dZ_2 = \frac{P-\operatorname{onehot}(y)}{B}
$$

然后沿计算图反向传播：

$$
dW_2=A_1^TdZ_2,\qquad db_2=\sum_{i=1}^{B}dZ_2^{(i)}
$$

$$
dA_1=dZ_2W_2^T,\qquad dZ_1=dA_1\odot \mathbb{1}[Z_1>0]
$$

$$
dW_1=X^TdZ_1,\qquad db_1=\sum_{i=1}^{B}dZ_1^{(i)}
$$

Notebook 中的 `backward` 沿着上面公式的逆序逐行执行。最值得核对的不是变量名，而是每一行输入输出的 shape：

```python
def backward(cache, y, params):
    batch_size = y.shape[0]                           # ① B

    dlogits = cache.probs.copy()                      # ② 不修改前向概率
    dlogits[np.arange(batch_size), y] -= 1.0         # ③ P-one_hot(y)
    dlogits /= batch_size                             # ④ 对应 mean loss

    dw2 = cache.a1.T @ dlogits                        # ⑤ (H,B)@(B,C)
    db2 = np.sum(dlogits, axis=0)                     # ⑥ 样本轴归约
    da1 = dlogits @ params.w2.T                       # ⑦ (B,C)@(C,H)
    dz1 = da1 * (cache.z1 > 0.0)                     # ⑧ ReLU 局部梯度
    dw1 = cache.x.T @ dz1                             # ⑨ (D,B)@(B,H)
    db1 = np.sum(dz1, axis=0)                        # ⑩ 样本轴归约

    return MLPParams(dw1, db1, dw2, db2)             # ⑪ 与参数同结构
```

逐行对应关系是：

1. **①** 从标签数量取得 `B`，不依赖调用者额外传入。
2. **②** 必须 `copy`；如果直接写 `dlogits = cache.probs`，③会原地破坏 forward 结果，后续 loss、accuracy 或调试输出都可能出错。
3. **③** 同一条高级索引语句只修改每条样本的正确类别，等价于减去 one-hot 标签，却不需要分配 `(B,C)` 的 one-hot 数组。
4. **④** forward 的 loss 使用 `mean`，所以梯度也要除以 batch size。漏掉这一行不会改变梯度方向，却会让有效学习率随 batch size 改变。
5. **⑤⑨** 权重梯度都由“层输入转置 @ 层输出梯度”得到；转置既来自矩阵微分，也保证输出与权重同形。
6. **⑥⑩** bias 被广播到 batch 中每条样本，因此反向要把所有样本对 bias 的贡献沿 `axis=0` 求和。
7. **⑦** 通过 `W2.T` 把类别空间梯度传回隐藏空间。
8. **⑧** 比较表达式生成布尔 mask；NumPy 在乘法中将 `True/False` 当作 `1/0`，负激活位置的梯度被截断。
9. **⑪** 返回与参数相同的 dataclass，让更新函数可以逐字段操作，并直接检查参数/梯度 shape 是否一致。

最终每个梯度必须和对应参数同形：

```text
w1 (2, 32) (2, 32)
b1 (32,)   (32,)
w2 (32, 2) (32, 2)
b2 (2,)    (2,)
```

这也解释了为什么框架需要 autograd：真实 Transformer 的计算图包含大量矩阵乘、残差、归一化、reshape 和广播，手工保存 cache、按逆序应用链式法则，既繁琐又容易错。autograd 消除的是机械工作，不是 shape、数值稳定性和优化问题。

## 六、梯度检查：让 backward 具备可证伪性

代码能运行、loss 能下降，都不能严格证明梯度实现正确。有限差分用参数两侧的 loss 估算数值梯度：

$$
\frac{\partial L}{\partial \theta}
\approx
\frac{L(\theta+\varepsilon)-L(\theta-\varepsilon)}{2\varepsilon}
$$

Notebook 的实现只抽查少量参数元素：

```python
def gradient_check(seed=7):
    rng = np.random.default_rng(seed)                 # ① 固定微型问题
    tiny_x = rng.normal(size=(5, 3))                  # ② 5 条 3 维输入
    tiny_y = np.array([0, 1, 2, 1, 0], dtype=np.int64)
    params = init_params(3, 4, 3, seed)               # ③ 3→4→3 网络
    grads = backward(forward(tiny_x, params), tiny_y, params)  # ④ 解析梯度
    epsilon = 1e-5                                    # ⑤ 中心差分步长
    max_error = 0.0                                   # ⑥ 聚合最大误差

    checks = [                                        # ⑦ 权重和偏置都覆盖
        ("w1", (0, 0)), ("w1", (2, 3)), ("b1", (1,)),
        ("w2", (0, 1)), ("w2", (3, 2)), ("b2", (2,)),
    ]
    for name, index in checks:
        parameter = getattr(params, name)             # ⑧ 按名称取得 ndarray
        original = parameter[index]                   # ⑨ 保存原值

        parameter[index] = original + epsilon         # ⑩ 正向扰动
        loss_plus = cross_entropy_loss(
            forward(tiny_x, params).probs,
            tiny_y,
        )
        parameter[index] = original - epsilon         # ⑪ 负向扰动
        loss_minus = cross_entropy_loss(
            forward(tiny_x, params).probs,
            tiny_y,
        )
        parameter[index] = original                   # ⑫ 必须恢复

        numerical = (loss_plus - loss_minus) / (2.0 * epsilon)  # ⑬
        analytical = getattr(grads, name)[index]      # ⑭ 同位置解析梯度
        max_error = max(                              # ⑮ 保留最坏情况
            max_error,
            abs(numerical - analytical),
        )
    return max_error
```

**①到③** 刻意构造小型 `float64` 问题，避免完整训练模型让检查变得昂贵；**④** 只计算一次手写解析梯度；**⑤** 在截断误差与浮点舍入误差之间取折中；**⑧到⑫** 对同一个参数元素先加后减，并在每次 forward 后恢复原值；**⑬** 得到中心差分；**⑭⑮** 比较同一位置的解析梯度并记录最大误差。⑫如果遗漏，后面的检查就会在已经被扰动的模型上进行。

Notebook 从 `W1`、`b1`、`W2`、`b2` 中抽取若干元素，把数值梯度和手写 backward 的解析梯度逐个比较。在 `\varepsilon=10^{-5}` 时：

```text
gradient check max abs error: 1.51e-11
```

这个结果说明被抽查的梯度与有限差分在浮点误差范围内一致。梯度检查通常只在小模型上运行；应优先使用 `float64`，还要避开 ReLU 在 0 附近的不可导点。抽样通过不是数学证明，但能高效发现大多数符号、转置和归约轴错误。

## 七、训练循环与实验结果

训练循环之前，Notebook 先把“打乱并切成 batch”封装成生成器：

```python
def iterate_minibatches(x, y, batch_size, rng):
    indices = rng.permutation(x.shape[0])             # ① 每轮重新打乱
    for start in range(0, x.shape[0], batch_size):   # ② 最后一个 batch 可更小
        batch_indices = indices[start:start + batch_size]  # ③ 当前窗口索引
        yield x[batch_indices], y[batch_indices]      # ④ 特征标签同步返回
```

**①** 只打乱索引，不复制完整特征矩阵；**②** `range` 不要求样本数整除 batch size；**③** 最后一片会自然停在数组尾部；**④** 使用 `yield` 延迟生成 batch，不需要预先把所有 batch 保存为列表。

核心训练循环没有隐藏步骤：

```python
params = init_params(...)                             # ① 初始化一次
rng = np.random.default_rng(seed)                    # ② batch 随机性
history = {                                          # ③ 四条指标曲线
    "train_loss": [],
    "train_acc": [],
    "val_loss": [],
    "val_acc": [],
}

for epoch in range(1, epochs + 1):
    for xb, yb in iterate_minibatches(x_train, y_train, batch_size, rng):
        cache = forward(xb, params)                   # ④ 当前参数做前向
        grads = backward(cache, yb, params)           # ⑤ 当前 batch 梯度
        update(params, grads, learning_rate)          # ⑥ 立即执行 SGD

    train_loss, train_acc = evaluate(x_train, y_train, params)  # ⑦
    val_loss, val_acc = evaluate(x_val, y_val, params)          # ⑧
    history["train_loss"].append(train_loss)           # ⑨ 记录而不更新
    history["train_acc"].append(train_acc)
    history["val_loss"].append(val_loss)
    history["val_acc"].append(val_acc)
```

**①** 参数只能在训练开始时初始化一次，放进 epoch 循环会让模型每轮“失忆”；**②** 单独维护 batch RNG，使数据生成和参数初始化不受 batch 调度影响；**③** 同时记录 train/validation 的 loss 与 accuracy，才能区分优化速度和泛化表现；**④⑤⑥** 严格对应 forward、backward、update；**⑦⑧** 只评估，不产生梯度；**⑨** 历史指标不参与参数更新，只作为诊断证据。

固定随机种子后，400 epoch 的最终结果是：

```text
epoch=0400 train_loss=0.0945 train_acc=0.956
           val_loss=0.0782   val_acc=0.967
```

验证准确率略高于训练准确率并不反常：当前验证集更小，也可能恰好稍容易。重要的是同时观察 loss、accuracy 和决策边界：loss 稳定下降、训练/验证曲线没有持续分离，边界也确实弯曲并贴合双月牙结构。

决策边界并不是模型额外输出的对象，而是对密集二维网格批量推理后画出的概率等高线：

```python
xx, yy = np.meshgrid(                                # ① 二维坐标网格
    np.linspace(x_min, x_max, 220),
    np.linspace(y_min, y_max, 220),
)
grid = np.c_[xx.ravel(), yy.ravel()]                 # ② (48400,2)
class_one_probability = forward(                     # ③ 一次批量推理
    grid,
    params,
).probs[:, 1].reshape(xx.shape)                     # ④ 恢复 (220,220)
axes[2].contourf(                                    # ⑤ 概率填色
    xx,
    yy,
    class_one_probability,
    levels=25,
    cmap="coolwarm",
    alpha=0.55,
)
```

**①** `meshgrid` 把两个一维坐标轴展开为所有二维组合；**②** `ravel` 后按列拼接，正好得到模型接受的二维 feature；**③** 展示了向量化的另一个用途——一次预测 48,400 个网格点；**④** 为绘图恢复网格结构；**⑤** 将类别 1 概率画成连续色块，`0.5` 附近就是分类边界。

### 学习率实验：更快的单步不等于更快收敛

保持初始化、数据和 batch 顺序不变，训练 120 epoch：

```python
learning_rates = [0.005, 0.08, 0.8, 5.0]             # ① 唯一自变量
lr_histories = {}                                     # ② 保存完整曲线

for learning_rate in learning_rates:
    _, lr_history = train(                            # ③ 重新从同一 seed 初始化
        x_train,
        y_train,
        x_val,
        y_val,
        epochs=120,                                   # ④ 固定训练预算
        learning_rate=learning_rate,                  # ⑤ 只替换学习率
        verbose=False,
    )
    lr_histories[learning_rate] = lr_history          # ⑥ 按学习率索引结果
```

**①** 同时覆盖过小、合适和过大的区间；**②⑥** 不只保存最终 accuracy，还保留每一轮曲线；**③** 每次 `train` 都使用相同 seed，因此初始参数和 batch 顺序一致；**④⑤** 固定 epoch，只改变学习率，实验才具有可比性。如果复用上一次训练后的 `params`，比较的就不再是学习率，而是不同起点和不同训练时长的混合影响。

| Learning rate | 最终训练 loss | 验证 accuracy | 观察 |
|---:|---:|---:|---|
| 0.005 | 0.2902 | 86.7% | 下降稳定，但明显偏慢 |
| 0.08 | 0.1096 | 97.0% | 稳定收敛 |
| 0.8 | 0.0965 | 97.3% | 本实验中收敛较快 |
| 5.0 | 0.2441 | 83.0% | 更新过大，结果明显退化 |

不能由此推出“`0.8` 是两层 MLP 的最佳学习率”。这个结论只在当前初始化、数据尺度、batch size 和 epoch 数下成立。换成未标准化输入、更深网络或不同初始化，稳定区间都会变化。正确做法是记录完整实验条件，再解释曲线，而不是孤立保存一个最佳数字。

## 八、从 NumPy 迁移到 PyTorch

完成这个项目后，PyTorch 的核心对象不再神秘：

| NumPy 项目中的显式工作 | PyTorch 中的对应机制 |
|---|---|
| `ndarray` 与参数 dataclass | `Tensor` 与 `nn.Parameter` |
| 手工保存 `ForwardCache` | autograd graph 保存反向所需状态 |
| `backward(cache, y, params)` | `loss.backward()` |
| `params -= lr * grads` | `optimizer.step()` |
| 手写 mini-batch 索引 | `Dataset` / `DataLoader` |
| 有限差分 gradient check | `torch.autograd.gradcheck` |

真正需要带入下一阶段的是三种习惯：

1. **先推 shape，再运行。** 广播成功不代表语义正确，很多错误不会抛异常。
2. **先构造可证伪检查，再调参。** 梯度检查、概率行和、数值一致性断言都比观察“好像在下降”可靠。
3. **把实验条件和结果一起记录。** 随机种子、数据切分、dtype、学习率、batch size 缺一不可。

## 九、如何复现实验

点击文首的“在文章内加载 Notebook”，或者在新窗口打开。首次启动需要下载浏览器 Python 运行时；之后可以执行 `Restart Kernel and Run All Cells`。浏览器版适合验证算法，不适合判断本机 OpenBLAS/MKL、SIMD 和线程性能。

也可以下载 `numpy-two-layer-mlp.ipynb` 后在本地运行：

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install numpy matplotlib jupyterlab
jupyter lab numpy-two-layer-mlp.ipynb
```

Notebook 的模型实现不依赖 scikit-learn。建议第一次选择 `Restart Kernel and Run All Cells`，确认所有 Cell 在干净状态下可以顺序执行。

## 十、阶段一复盘

到这里，阶段一的理论和项目实践形成了闭环：

- ndarray、内存布局、广播和 dtype：由理论文章建立心智模型；
- shape、标准化和向量化：由数据与距离计算实验验证；
- softmax、cross-entropy 和数值稳定性：由前向传播验证；
- 矩阵梯度和链式法则：由手写 backward 验证；
- 实现正确性：由有限差分梯度检查验证；
- 训练行为：由 loss、accuracy、学习率对照和决策边界验证。

这还不是“学会深度学习”，但已经足以进入 PyTorch 阶段。下一步不是忘掉这些细节，而是观察框架如何把参数注册、计算图、梯度累积、优化器和设备迁移系统化，并继续追问：哪些机制只是减少样板代码，哪些机制真正改变了执行方式与性能边界。
