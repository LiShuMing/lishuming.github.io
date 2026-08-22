---
title: "从 GPU 到 LLM 微调：一条面向 RTX 5070 Ti 的实践学习路线"
date: 2026-08-15T00:00:00+08:00
categories:
  - AI Infra
tags:
  - CUDA
  - PyTorch
  - Transformer
  - LLM
  - LoRA
description: "基于 RTX 5070 Ti 16GB、WSL2、CUDA 与 PyTorch 环境设计的一条 20-27 周实践路线，从 NumPy、GPU 性能分析逐步走到 Transformer 和 LoRA/QLoRA 微调。"
draft: false
---

当前LLM/大模型的新闻铺天盖地，日常开发工作显得老旧又无聊。同时老人们（包括我自己）都很矛盾，一方面嘴巴上、思想上都觉得我们要拥抱AI，自己再一次能够走在时代的前言（本身互联网的定义就意味着创新和前沿）；但另外一方面又有点固守以前的思维，AI写的代码、写的文档怎么可能有我手写的好呢？（呵呵，早就有了）。所以还是要主动地学习啊！

希望能够开始系统整理一条从 GPU 基础走向 LLM 微调的学习路线(结合AI的辅助加成)。目标不是先成为 CUDA 底层专家，也不是直接套用一个微调脚本得到若干看似不错的输出，而是补齐：数值计算、自动求导、训练循环、GPU 性能意识、深度学习训练诊断、Transformer 原理，以及受限显存下的微调与评估。

AI帮我制定了如下路线， 以实践为主，每周至少产出一个可运行脚本或 Notebook，每个阶段都要留下实验数据和复盘。最终能够在一张 RTX 5070 Ti(我的电脑) 上完成一个可复现的 LoRA 或 QLoRA 项目，并能解释训练过程中每一项关键选择。

## 一、目标：应用微调，而不是堆工具

LLM 学习很容易陷入两种极端：一种是一直补数学和理论，迟迟不开始训练；另一种是直接运行 Hugging Face 示例，只知道修改 batch size，遇到 OOM 或效果退化时却无从分析。

应用微调能力目标：

1. 用 NumPy 实现小型神经网络的前向与反向传播。
2. 用 PyTorch 独立完成数据加载、训练、验证、保存与恢复。
3. 解释参数、梯度、优化器状态和 activation 分别如何占用显存。
4. 对训练 step 做可靠计时和 profiling，而不是只观察 GPU 利用率。
5. 从零实现一个小型 decoder-only Transformer，理解 causal mask 和 next-token prediction。
6. 准备指令数据，完成 LoRA/QLoRA 微调，并对比微调前后的效果。

CUDA C++ 是路线中的可选扩展。理解 thread、block、grid 和内存层次很有价值，但写复杂 kernel 并不是微调 LLM 的前置条件。主线仍然是先通过 PyTorch 和 Hugging Face 建立完整的训练与评估能力，再根据性能问题深入 CUDA 或 Triton。

## 二、当前环境

实验环境是一台 Windows + WSL2 机器，核心配置如下：

| 组件 | 当前配置 | 用途 |
|------|----------|------|
| 操作系统 | WSL2 / Ubuntu 24.04.4 LTS | 开发与训练环境 |
| GPU | NVIDIA GeForce RTX 5070 Ti | 单卡训练和推理 |
| 显存 | 16GB（PyTorch 检测约 15.92 GiB） | 约束模型、序列长度与 batch size |
| Compute Capability | 12.0 | Blackwell 架构 CUDA 能力 |
| Python | 3.12.3 | 实验运行时 |
| PyTorch | 2.10.0+cu128 | 深度学习主线 |
| PyTorch CUDA Runtime | 12.8 | PyTorch 自带并使用的 CUDA 运行时 |
| 项目 CUDA Toolkit | 12.8 / nvcc 12.8.61 | 编译配套 CUDA C++ 实验 |
| 系统 CUDA Toolkit | 13.3 | `/usr/local/cuda-13.3` 中的独立工具链 |
| GPU 驱动接口 | CUDA 13.3 | WSL 驱动报告的最高 CUDA 能力 |

这里有一个值得单独说明的地方：驱动、PyTorch CUDA Runtime 和 `nvcc` 不一定显示同一个版本。

- WSL 中的 NVIDIA 驱动报告 CUDA 13.3，表示当前驱动可以支持到该 CUDA 版本。
- PyTorch wheel 是 `cu128` 构建，运行 PyTorch 时主要使用它随包携带的 CUDA 12.8 运行库。
- `nvcc` 只在编译 CUDA C++ 或 PyTorch CUDA Extension 时参与工作。
- 当前项目显式固定到用户目录下的 CUDA 12.8，使它和 PyTorch `cu128` 保持一致；系统安装的 13.3 可以保留给独立 CUDA 实验。

因此，不需要为了让三个版本号看起来一致而反复重装。真正需要检查的是：驱动是否支持当前 Runtime、PyTorch 是否能识别 GPU、目标架构是否受支持，以及实际 kernel 能否运行。

项目环境可以这样检查：

```bash
cd ~/work/gpu-llm-roadmap
source ./scripts/cuda_env.sh
./scripts/check_env.sh
```

当前环境已经通过一个 `4096 x 4096` 的 CUDA 矩阵乘法测试。WSL 中如果直接执行 `nvidia-smi` 提示找不到命令，也可以使用：

```bash
/usr/lib/wsl/lib/nvidia-smi
```

## 三、学习地图

整条路线分成六个阶段。阶段之间有明确依赖，但不用等到“完全掌握”再继续；达到验收标准、留下问题记录，就可以进入下一阶段。

| 阶段 | 时间 | 核心问题 | 阶段产出 |
|------|------|----------|----------|
| 1. NumPy 与数学基础 | 2-3 周 | 模型训练中的矩阵和梯度到底是什么 | NumPy 手写两层 MLP |
| 2. PyTorch 基础 | 3-4 周 | 框架如何组织自动求导和训练过程 | 可复用训练模板 |
| 3. GPU 与性能意识 | 3-4 周 | 为什么快、为什么慢、为什么 OOM | CUDA 性能实验报告 |
| 4. 深度学习核心 | 4-5 周 | 如何判断模型没有学好 | 图像或文本分类项目 |
| 5. Transformer 与 LLM | 4-5 周 | decoder-only LM 如何训练和生成 | 小型语言模型 |
| 6. LLM 微调 | 4-6 周 | 如何在 16GB 显存内有效微调 | LoRA/QLoRA 最终项目 |

### 阶段一：NumPy、矩阵计算与反向传播

第一阶段不用 GPU，重点是消除 shape 和梯度上的模糊感。

**理论基础进度：已完成。** [《深入 NumPy：从 ndarray 内存模型到科学计算与张量生态》]({{< relref "2026-08-16-numpy-internals.md" >}}) 已系统梳理 ndarray 内存模型、strides、广播、dtype、向量化以及 BLAS/LAPACK 等核心内容，接下来进入矩阵计算、反向传播和小型神经网络的实践阶段。

**实践项目进度：已完成。** [《NumPy 项目实践：从向量化到手写两层 MLP 与反向传播》]({{< relref "2026-08-16-numpy-mlp-practice.md" >}}) 提供了可在浏览器运行的 Notebook，并以向量化、稳定 softmax、手写 backward、有限差分梯度检查和学习率对照完成了阶段验收。

第 1 周练习 `ndarray`、dtype、axis、reshape、transpose、broadcasting 和向量化。每次矩阵运算先在纸上推导 shape，再运行代码验证。还要分别用 Python 循环和 NumPy 向量化实现欧氏距离，观察实现方式对性能的影响。

第 2 周实现线性模型、sigmoid、ReLU、softmax、MSE 和 cross entropy。softmax 必须包含减去最大值的数值稳定处理，并通过不同学习率下的 loss 曲线观察收敛、震荡和发散。

第 3 周只用 NumPy 完成两层 MLP 的 forward、loss、backward 和 update。除了最终 accuracy，更重要的是能说明每个参数及其梯度的 shape。

这一阶段的验收不是“看完 NumPy 教程”，而是不依赖 autograd 训练出一个小型分类器，并能解释深度学习框架为什么需要自动求导。

### 阶段二：形成可复用的 PyTorch 训练模板

第二阶段把手写训练过程迁移到 PyTorch。

前两周覆盖 Tensor、device、dtype、autograd、`nn.Module`、loss 和 optimizer。训练循环要能独立写出 `zero_grad`、forward、loss、backward 和 `step`，并清楚 `model.train()`、`model.eval()` 与 `torch.no_grad()` 分别影响什么。

第 3 周引入 Dataset、DataLoader、训练/验证/测试集划分和 checkpoint。一个合格的 checkpoint 不应只保存模型参数，还应包含 optimizer、epoch、随机种子或恢复训练所需的其他状态。

第 4 周对比 fp32、fp16 和 bf16。RTX 5070 Ti 可以承担这部分真实实验：在同一个模型上记录吞吐、峰值显存和最终指标，而不是笼统地认为“混合精度一定更快”。排查数值问题时，仍应先回到 fp32 建立基线。

阶段产出是一套可替换模型和数据集的训练模板，支持 GPU 自动选择、train/eval、checkpoint 恢复、指标日志和随机种子设置。

### 阶段三：建立 GPU 性能意识

这一阶段回答三个问题：GPU 为什么快，GPU 为什么有时不快，以及显存为什么会耗尽。

首先比较 NumPy CPU、PyTorch CPU 和 PyTorch CUDA 的矩阵乘法。测试必须包含 warmup，并在计时边界调用 `torch.cuda.synchronize()`，否则测到的可能只是异步 kernel launch 时间。还要比较大量小矩阵计算和一次大矩阵计算，理解 launch overhead 与并行度。

随后拆解训练显存：

```text
训练显存 ≈ 参数 + 梯度 + 优化器状态 + activation + 临时工作区
```

在 RTX 5070 Ti 的 16GB 显存上逐步增加 batch size 和 sequence length，记录峰值显存；再分别启用混合精度、gradient accumulation 和 activation checkpointing，比较每种方法节省了什么、付出了什么。

第 3 周使用 `torch.profiler` 分析 data loading、forward、backward 和 optimizer 的耗时，调整 `num_workers`、pin memory 和 batch size。最终报告必须包含环境、测试代码、CPU/GPU 对比、batch size 与显存关系，以及至少一个由数据支持的优化结论。

第 4 周是 CUDA C++ 扩展。配套实验包含 vector add 和 shared memory tiled matrix multiplication，可以用下面的方式运行：

```bash
cd ~/work/gpu-llm-roadmap
source ./scripts/cuda_env.sh
cd experiments/chapter03
make run
```

CUDA 入门最重要的一行索引是：

```cpp
int idx = blockIdx.x * blockDim.x + threadIdx.x;
```

理解它如何把线程映射到数组元素，再逐步进入 global memory、shared memory、coalesced access 和性能分析，会比一开始抄复杂矩阵乘法 kernel 更有效。

### 阶段四：学习诊断训练问题

模型能运行不等于模型学得好。第四阶段集中处理欠拟合、过拟合、学习率、正则化、数据质量和指标选择。

先在 MLP 上对比 SGD、Momentum 和 AdamW，观察不同初始化与学习率的曲线；再通过 CIFAR-10 或类似数据集完成 CNN 图像分类，理解 convolution 的输出 shape、batch normalization 和 data augmentation。

第 3 周主动制造问题：减少训练数据观察过拟合，增加 dropout 或 weight decay 后重新对比；输出 confusion matrix，分析模型到底在哪些类别上出错。第 4 周转向文本分类，建立 token、id、embedding、padding 和 mask 的基本概念。

阶段项目可以选择图像或文本分类，但必须包含数据加载、模型定义、训练与验证、checkpoint、指标曲线和错误样本分析。最终需要能根据 train/validation 曲线提出下一轮实验，而不是只继续增加 epoch。

### 阶段五：从 Attention 到 decoder-only LLM

第五阶段先实现原理，再使用成熟模型。

第 1 周从 character tokenizer 开始，构造 next-token prediction 样本，确认训练输入与 label 为什么错开一位。第 2 周用 PyTorch 手写 single-head causal self-attention，打印 attention score 和 causal mask，再扩展到 multi-head attention。

第 3 周把 token embedding、position embedding、attention、MLP、residual connection、layer norm 和 logits head 组合成一个小型 decoder-only Transformer。训练目标不是生成高质量长文，而是让 loss 稳定下降，并生成能够证明数据分布被学习到的样例。

第 4 周使用 `AutoTokenizer`、`AutoModelForCausalLM` 和 `generate()` 加载小型开源模型，对比 greedy、temperature 和 top-p。第 5 周测试不同 prompt 长度、生成长度和 batch size，理解 prefill、decode、KV cache 与上下文显存之间的关系。

这一阶段结束时，应该能解释 causal mask、attention shape、logits、sampling 和 KV cache，而不只是会调用 `generate()`。

### 阶段六：在 16GB 显存内完成 LoRA/QLoRA

最后一个阶段把前面的数据、训练、显存和 Transformer 知识合并起来。

第 1 周准备 500-5000 条指令数据，统一为 `instruction`、`input`、`output` 字段，并检查空值、重复、超长和低质量样本。先固定 prompt template 和 validation split，再开始训练。

第 2 周只用小模型、小数据训练几十个 step，完成 smoke test。此时目标是验证 tokenize、padding、label、数据 collator 和 loss 都正确，而不是追求最终效果。

第 3 周使用 PEFT 完成 LoRA，记录 target modules、rank、alpha、dropout、可训练参数量和总参数量。adapter 要单独保存，并验证 base model + adapter 可以重新加载推理。

第 4 周进入 QLoRA。针对 16GB 显存，默认实验顺序是：

1. 从 1B-3B 模型建立稳定基线，再尝试更大的 7B/8B 级模型。
2. 使用 4-bit NF4 加载 base model，并优先采用 bf16 计算。
3. 将 micro batch size 从 1 开始，通过 gradient accumulation 获得目标有效 batch。
4. 限制 sequence length，并用样本长度分布指导选择，而不是直接设成模型上限。
5. 显存仍不足时启用 gradient checkpointing，并记录吞吐下降。

“7B/8B 能加载”不代表“能以任意序列长度稳定训练”。模型结构、target modules、optimizer、量化实现和上下文长度都会影响峰值显存，因此最终配置必须由实测得出。

第 5 周固定 20-50 条评估问题，对 base model 和微调模型使用一致的生成参数。输出按正确、部分正确、格式错误和幻觉分类，同时检查是否存在训练集泄漏。loss 下降只能说明优化目标发生了变化，不能单独证明微调成功。

第 6 周整理最终项目，至少保留数据集说明、环境锁定、训练命令、显存与耗时、loss 曲线、adapter、推理脚本、微调前后对比和已知问题。

## 四、RTX 5070 Ti 上的实验策略

16GB 显存足以完成这条路线，但需要把“显存约束”当作实验变量，而不是机器缺陷。

### 1. 先测量，再调参

每次性能实验至少记录：模型、dtype、batch size、sequence length、是否开启 checkpointing、峰值显存、tokens/s 或 samples/s。只记录 GPU utilization 很难定位瓶颈，因为等待数据、频繁小 kernel 和同步也可能表现为利用率不高。

### 2. 区分训练显存与推理显存

推理主要保存参数和 KV cache；训练还需要梯度、优化器状态和大量 activation。一个能够顺利推理的模型，未必能在相同精度和上下文长度下全参数训练。LoRA 减少的是可训练参数及相关状态，QLoRA 则进一步压缩 base model 的权重。

### 3. OOM 时按来源处理

遇到 CUDA OOM，可以按下面的顺序排查：

- 降低 micro batch size。
- 降低 sequence length，并检查是否有异常长样本。
- 使用 bf16/fp16 或 4-bit 量化。
- 使用 gradient accumulation 保持有效 batch size。
- 启用 activation checkpointing。
- 减少不必要的缓存和评估 batch，确认进程中没有残留模型。
- 最后才考虑缩小模型；先确认显存到底花在哪里。

### 4. 保留 12.8 与 13.3 两套 Toolkit 的边界

PyTorch 主线继续使用项目固定的 CUDA 12.8，因为当前 PyTorch 本身是 `cu128` 构建；独立 CUDA 13.3 实验则可以显式调用 `/usr/local/cuda-13.3/bin/nvcc`。编译 PyTorch CUDA Extension 时，应优先让 `CUDA_HOME` 与 PyTorch 的 CUDA 版本一致，减少 ABI、头文件和工具链混用带来的问题。

## 五、如何记录学习，而不是只收藏资料

每周结束时只记录“看了哪些课程”没有太大价值。我的进度记录会围绕四项内容展开：

```text
本周产出：可以重新运行的脚本、Notebook 或报告
实验结论：由指标、曲线或输出支持的认识
失败记录：错误信息、错误假设和解决过程
下周入口：尚未解决的问题和下一项最小任务
```

每个阶段结束后再做一次验收：关闭参考代码，能否从空文件写出核心流程；更换数据或参数后，能否预判 shape、显存和行为变化；实验失败时，能否提出可以证伪的原因。

资料方面保持精简，优先使用官方文档和能够落到代码的材料：

- [NVIDIA CUDA C++ Programming Guide](https://docs.nvidia.com/cuda/cuda-c-programming-guide/)
- [NVIDIA CUDA Best Practices Guide](https://docs.nvidia.com/cuda/cuda-c-best-practices-guide/)
- [PyTorch Tutorials](https://pytorch.org/tutorials/)
- [PyTorch CUDA Semantics](https://pytorch.org/docs/stable/notes/cuda.html)
- [Dive into Deep Learning](https://d2l.ai/)
- [The Annotated Transformer](https://nlp.seas.harvard.edu/annotated-transformer/)
- [Hugging Face LLM Course](https://huggingface.co/learn/llm-course/)
- [Hugging Face PEFT](https://huggingface.co/docs/peft/)

## 六、最终验收

这条路线的终点不是“学完 CUDA、PyTorch 和 Transformer”，而是交付一个别人可以复现的微调项目。README 应该能让人从环境准备开始，复现数据处理、训练和推理；实验报告能够解释显存和性能选择；评估集能够展示模型的改善与退化。

完成以后，下一步可以沿三条方向继续深入：使用 vLLM、llama.cpp 或 TensorRT-LLM 学习推理部署；使用 DPO、ORPO 等方法学习偏好优化；或者进入 Triton、CUDA C++ 与算子优化。到那时，选择方向的依据会来自真实实验中遇到的问题，而不是工具名称本身。

对我而言，这条学习路线真正要建立的是一套稳定的方法：从原理形成假设，用代码构造实验，用指标验证结论，再把失败过程保留下来。硬件只是边界，持续可复现的实践才是主线。
