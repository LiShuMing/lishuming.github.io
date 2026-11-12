---
title: "【LLM】NumPy实践：从向量化到手写两层 MLP 与反向传播"
date: 2026-08-16T00:00:00+08:00
lastmod: 2026-10-07T00:00:00+08:00
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

在 PyTorch 里训练一个分类器，核心循环往往浓缩成轻描淡写的三行：`logits = model(x)`、`loss = criterion(logits, y)`、`loss.backward()`。正因为自动求导引擎把脏活累活全包圆了，一旦后续在真实大模型上撞见显存随序列长度暴涨、半精度训练突然蹦出 `NaN`，或者模型死活不收敛，很多习惯了调框架的人一时间根本不知道该去掀哪一块盖板。

如果我们把深度学习框架彻底拿掉，只留上一篇[《NumPy 内存模型与张量生态》]({{< relref "2026-08-16-numpy-internals.md" >}})里的 `ndarray`，从零手写一个两层多层感知机（Multilayer Perceptron，MLP），`loss.backward()` 幕后藏着的三件硬核工作就会立刻水落石出：

1. **为什么前向激活必须驻留内存？** 根据多元复合函数链式法则，反向传播计算第二层权重梯度 $\partial L / \partial W_2 = A_1^T dZ_2$ 以及第一层激活门控 $\partial L / \partial Z_1 = dA_1 \odot \mathbb{1}[Z_1 > 0]$ 时，手里必须攥着前向阶段算出来的隐藏层输出 $A_1$、预激活 $Z_1$ 和原始输入 $X$。推理算完一层就能立刻复用内存，训练却非把沿途激活全部缓存下来不可——这就是训练显存远大于推理显存的物理根源。
2. **为什么教科书公式直接敲成代码会溢出崩溃？** 朴素的 $\exp(z_i) / \sum_j \exp(z_j)$ 只要遇到一个稍大的未归一化分数（`float64` 下 $z_i > 710$，`float32` 下 $z_i > 89$），指数项就会瞬间冲出浮点表示上限变成 `inf`，紧接着 `inf / inf` 把整批梯度全部染成 `NaN`。利用平移不变性在求指数前先减去每行最大值，是保证训练不炸锅的生死线。
3. **怎么在不靠框架的前提下证明反向传播没写错？** 漏除 `batch_size`、偏置归约搞错轴，甚至原地修改了前向缓存的概率数组，随机梯度下降有时依然能跌跌撞撞把训练误差往下推，但收敛速度与超参数尺度早已失真。引入中心有限差分（Finite Difference）梯度检查，用 $(L(\theta+\varepsilon) - L(\theta-\varepsilon)) / 2\varepsilon$ 独立标定斜率，就能把“凭感觉看曲线”变成 `1.51e-11` 级别的确定性数学验证。

本文作为[《面向 RTX 5070 Ti 的 LLM 实践学习路线》]({{< relref "2026-08-15-gpu-to-llm-finetuning-roadmap.md" >}})第一阶段的动手验收项目，不调用任何机器学习库，纯靠 NumPy 跑通二维双月牙非线性分类的全链路。配套交互式 Notebook 已内嵌在下方，可直接在浏览器中修改超参数并重新运行全部单元格：

<!--more-->

{{< notebook src="/lab/notebooks/index.html?path=numpy-two-layer-mlp.ipynb" download="/notebooks/numpy-two-layer-mlp.ipynb" title="NumPy 两层 MLP：完整实验 Notebook" >}}

下文所有代码片段均与 Notebook 单元格严格对应（Matplotlib 仅用于最终绘制曲线与决策边界，不参与任何数值计算），代码右侧的 `①②③` 标号与紧随其后的维度及机制拆解一一呼应。

---

## 一、构造双月牙数据与防泄漏预处理

之所以挑选二维双月牙（Two Moons）而非高维图像数据集作为练手靶子，是因为两弯交错的半圆在平面上**严格线性不可分**——任何单层线性分类器都只能画出一条笔直的切线，模型必须借助隐藏层与 ReLU 非线性弯折空间才能分开两类样本；同时二维输入允许我们把 `48,400` 个密集网格点直接喂进网络，肉眼验收最终学出来的决策边界曲率。

### 1. 纯 NumPy 生成交错半圆与同步打乱

我们不借道 `scikit-learn`，直接用三角函数、高斯噪声与排列索引拼出 `1,200` 条样本：

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

### 2. 分层切分与“仅用训练集统计量”标准化

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

---

## 二、向量化热身：先断言数值等价，再谈调度加速

在正式写网络之前，Notebook 先做了一组对照实验：计算 `10,000` 个 `128` 维特征点到单个查询向量 `query: (128,)` 的欧氏距离。纯 Python 循环写法每次只送一行进去：

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

计时对比之前，先用 `np.testing.assert_allclose(loop_distance, vector_distance)` 锁死两套写法的浮点一致性——没有正确性兜底的“提速”，十有八九只是不小心改错了归约轴。在本机实测中，Python 循环耗时约 `0.1010s`，向量化版本耗时约 `0.0196s`（快约 `5.1x`）。把成千上万次碎片化的解释器调度压成少数几次大块连续内存内核执行，这一原则从 CPU 上的 NumPy 一路贯通到后续 PyTorch 与 CUDA 算子优化。

---

## 三、前向传播、显式状态缓存与手写反向求导

设微批次样本数为 $B$，输入特征维 $D = 2$，隐藏层宽度 $H = 32$，输出类别数 $C = 2$。两层 MLP 的前向映射由两步仿射变换夹一层逐元素 ReLU 激活与行归一化 Softmax 组成：

$$
Z_1 = X W_1 + b_1, \quad A_1 = \operatorname{ReLU}(Z_1), \qquad Z_2 = A_1 W_2 + b_2, \quad P = \operatorname{softmax}(Z_2)
$$

对 Softmax 与交叉熵损失 $L = -\frac{1}{B}\sum_{i=1}^B \log P_{i, y_i}$ 联合求导，输出层未归一化分数 $Z_2$ 处的梯度可以化简成一个极其干净的闭式解，随后顺着计算图逆向回传至每一组权重与偏置：

$$
dZ_2 = \frac{P - \operatorname{onehot}(y)}{B}, \qquad dW_2 = A_1^T dZ_2, \quad db_2 = \sum_{i=1}^{B} dZ_2^{(i)}
$$

$$
dA_1 = dZ_2 W_2^T, \qquad dZ_1 = dA_1 \odot \mathbb{1}[Z_1 > 0], \qquad dW_1 = X^T dZ_1, \quad db_1 = \sum_{i=1}^{B} dZ_1^{(i)}
$$

把正向的形状流转、中间横亘的 `ForwardCache` 状态驻留层，以及反向严格镜像对称的矩阵转置与按轴归约画在同一张架构图上，前向与反向之间的精确耦合关系便一目了然：

![两层 MLP 前向 Shape 变换、ForwardCache 缓存与反向梯度链式传播：上方左至右展示 (B,2)→(B,32)→(B,2) 前向变换，中部驻留 X、Z1、A1 与 P 四组缓存张量，下方右至左完成联合梯度回传、ReLU 门控截断与参数梯度归约](/images/diagrams/aug-ai-mlp-forward-backward.svg)

### 1. He 初始化与主动保存 `ForwardCache`

针对会截断负半轴信号的 ReLU 激活函数，权重采用按输入扇入维（`fan_in`）缩放的 He 正态初始化 $\mathcal{N}(0, 2/\text{fan\_in})$，避免层数加深时前向激活方差萎缩或爆炸：

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

### 2. 平移不变稳定 Softmax 与免 One-Hot 交叉熵

对同一行分数同时减去该行最大值 $m_i = \max_j z_{i,j}$，分子分母同乘 $e^{-m_i}$ 严格约分，数学结果分毫不差，而传入 `np.exp` 的最大实参被死死摁在 `0`（即 $\exp(0) = 1$），彻底根除了指数上溢：

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

### 3. 逐行拆解 `backward`：三处最容易踩中的静音地雷

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

- **前向广播对应反向求和（⑥与⑩）**：前向算 `x @ w1 + b1` 时，一维偏置 `b1:(32,)` 被零步长广播加到了全部 `B` 条样本上；根据多元全微分法则，一个变量分叉参与了 `B` 条路径，反向回传时就必须沿 `axis=0` 把这 `B` 条样本传回的偏导数全部加总起来，维度也恰好缩回 `(32,)`。
- **拒绝原地篡改缓存（②）**：如果贪图省事写成 `dlogits = cache.probs`，第 ③ 行的 `-= 1.0` 会直接把 `ForwardCache` 里的正确类别概率改成负数！一旦外部日志或评估逻辑再去读 `cache.probs`，就会看到荒诞的负概率。
- **损失归约口径严格对齐（④）**：既然前向损失用了 `np.mean` 除以 `B`，反向起点 `dlogits` 就必须除以 `B`。漏写这行代码程序照跑不误，但梯度模长会平白放大 `B` 倍——一旦你后面调大 `batch_size`，原本稳定的学习率就会突然把模型训崩。

---

## 四、有限差分梯度检查与单变量训练诊断

手写完 `backward`，绝不急着直接上大循环调学习率。我们先用只依赖前向损失函数定义的中心有限差分公式，给反向求导做一次严格的“数学体检”：

$$
\frac{\partial L}{\partial \theta_k} \approx \frac{L(\theta + \varepsilon e_k) - L(\theta - \varepsilon e_k)}{2\varepsilon}
$$

相比单侧差分 $(L(\theta+\varepsilon) - L(\theta)) / \varepsilon$ 的一阶截断误差 $O(\varepsilon)$，中心差分利用泰勒展开奇次项相消，将截断误差直接压到了二阶 $O(\varepsilon^2)$。将双路梯度核验门禁与后续受控 Mini-batch SGD 训练、学习率消融串起来，整套验证与实验闭环如下：

![中心有限差分梯度检查与受控训练闭环：先在 float64 微型网络上并行求解手写解析梯度与 ±1e-5 中心差分数值斜率，通过 1.51e-11 误差断言后再进入 400 轮 Mini-batch 训练与学习率单变量对照](/images/diagrams/aug-ai-mlp-gradcheck-loop.svg)

### 1. `1.51e-11` 级精度的 `gradient_check` 实现

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

运行输出 `gradient check max abs error: 1.51e-11`！在双精度浮点与 $\varepsilon = 10^{-5}$ 下拿到 $10^{-11}$ 量级的吻合度，意味着转置方向、`axis=0` 求和与 `/ batch_size` 全部严丝合缝（若使用单精度 `float32`，加减 $10^{-5}$ 极易被 23 位尾数截断吞没，且若扰动区间恰好跨过 $Z_1 = 0$ 的 ReLU 折点也会引起折线斜率跳变）。

### 2. 400 轮 Mini-batch 训练与 48,400 点决策边界可视化

梯度验明无误后，我们用生成器按轮打乱下标切分微批次（支持末尾不足一整批的尾包），跑满 400 轮训练：

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

第 400 轮收敛至 `train_loss=0.0945, train_acc=0.956 / val_loss=0.0782, val_acc=0.967`，训练与验证损失同步平滑下降，无过拟合分离迹象。为了直观看到隐藏层的 32 个 ReLU 神经元是如何通过折线拼接出平滑曲线边界的，我们在二维特征平面上铺开 `220 × 220 = 48,400` 个密集采样点，一次向量化前向推理画出类别概率等高线：

```python
xx, yy = np.meshgrid(np.linspace(x_min, x_max, 220), np.linspace(y_min, y_max, 220))
grid = np.c_[xx.ravel(), yy.ravel()]                 # 拼成 (48400, 2) 批量特征矩阵
class_one_probability = forward(grid, params).probs[:, 1].reshape(xx.shape)
axes[2].contourf(xx, yy, class_one_probability, levels=25, cmap="coolwarm", alpha=0.55)
```

### 3. 单变量学习率消融：步长尺度如何塑造优化轨迹？

最后锁死初始权重种子、数据集切分与批次打乱顺序，在固定 120 轮预算下仅改变 `learning_rate`：

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

注意这里为什么 `lr = 0.8` 依然能稳住，而在深层 Transformer 里学习率通常要设到 $10^{-4}$ 甚至 $10^{-5}$：本实验只有两层全连接、输入经过了严格的零均值单位方差标准化，损失曲面的局部曲率（Hessian 最大特征值）温和得多；一旦网络加深到几十层且叠加注意力点积，梯度范数与曲率尺度陡增，稳定步长区间就会随之向小数量级大幅收缩。

---

## 五、向 PyTorch 平滑交棒与本地复现指南

亲手跑完这一轮，下一阶段跨进 PyTorch 时你会发现框架里的每一个核心类都能在这份百行脚本里找到精准的原身：

| 纯 NumPy 手写组件 | PyTorch 对应抽象 | 框架接管的工作与仍需工程师把关的边界 |
| --- | --- | --- |
| `ndarray` + `MLPParams` | `torch.Tensor` + `nn.Parameter` | 增加 `device` 显存管理与梯度挂载；仍需人工核对广播维度与 `dtype` |
| 显式返回 `ForwardCache` | Autograd 动态计算图节点 | 自动驻留反向所需张量；推理评估漏加 `torch.no_grad()` 会白白撑爆显存 |
| 手写 `backward(cache, y, params)` | `loss.backward()` | 自动逆序链式求导；但梯度默认累加（需手动 `zero_grad()`） |
| `params -= lr * grads` | `optimizer.step()` | 扩展出动量与自适应二阶矩（AdamW）；学习率与权重衰减仍靠实测整定 |
| `iterate_minibatches` 生成器 | `Dataset` + `DataLoader` | 支持多进程预取与锁页内存；切分防泄漏仍归业务代码负责 |
| 中心有限差分 `gradient_check` | `torch.autograd.gradcheck` | 编写自定义 CUDA / Triton 算子 backward 时依然靠同一套双精度差分验真 |

> **运行环境与复现说明**：
> - **浏览器交互运行**：文首内嵌的 Notebook 基于 Pyodide / WebAssembly 构建（仓库部署时通过 `jupyter lite build` 输出至 `public/lab`），打开后点击 `Restart Kernel and Run All Cells` 即可在浏览器沙箱内重跑全部数值验证与绘图；文中记录的 `0.1010s vs 0.0196s` 耗时采自本地原生 CPython 环境，浏览器 WASM 环境仅用于核验算法与数值结果。
> - **本地原生环境复现**：点击文首按钮下载 `numpy-two-layer-mlp.ipynb` 后，三行命令即可在干净虚拟环境中启动：
>   ```bash
>   python3 -m venv .venv && source .venv/bin/activate
>   python -m pip install numpy matplotlib jupyterlab
>   jupyter lab numpy-two-layer-mlp.ipynb
>   ```
