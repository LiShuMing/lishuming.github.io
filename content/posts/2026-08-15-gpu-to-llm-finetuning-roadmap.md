---
title: "【LLM】 LLM学习：一条面向 RTX 5070 Ti 的实践学习路线"
date: 2026-08-15T00:00:00+08:00
lastmod: 2026-10-28T00:00:00+08:00
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
toc: true
---

第一次尝试在大模型上跑微调（Fine-Tuning）时，最诱人的捷径莫过于直接拷一份现成的 `AutoModelForCausalLM` 加 `peft` 脚本，喂进几百条 JSON 指令数据就开始盯终端进度条。顺风顺水时，十几分钟就能拿到一个看似能对话的权重包；可一旦遇到两类最常见的工程事故，纯靠调包立刻就会寸步难行。

第一类事故是**显存瞬间炸穿（CUDA Out Of Memory）**。手头这张 RTX 5070 Ti 拥有 16GB 显存（PyTorch 实际可见约 `15.92 GiB`）。如果按直觉估算：一个 7B 参数的模型用 FP16 加载不过 `7 × 2 = 14 GB`，推理想必绰绰有余，微调改小一点 `batch_size` 似乎也能塞下？真跑起来就会发现，连第 0 个训练步都迈不过去。原因在于推理只需驻留权重和随上下文增长的 KV Cache，而全参数训练在同一块 `15.92 GiB` 天花板下同时摆开了四本账：静态模型权重（`14 GB`）、同尺寸的参数梯度（`14 GB`）、AdamW 优化器为每个参数维护的 FP32 主权重备份与一阶动量 `m`、二阶方差 `v`（`7 × 12 = 84 GB`，即便省去主备份仅存动量与方差也要 `56 GB`），再加上 32 层网络前向传播必须暂存下来供反向链式求导使用的中间激活值（Activations，随 `batch_size × seq_len` 线性暴涨）。光静态状态就已经超出一张消费级显卡容量的四五倍。不搞清楚这四笔显存分别由谁产生、用什么机制裁剪，面对 OOM 就只能盲目乱改参数。

第二类事故是 **Loss 曲线诡异发散或“假收敛”**——训练损失一路跌到接近 0，拉起模型推理却满嘴乱码或复读停不下来。高层 Trainer 把分词对齐、因果掩码（Causal Mask）、混合精度缩放和梯度累积全部封在黑盒里：究竟是输入与标签漏掉了错位一位的移位（Shift-by-one），还是把 Prompt 模板算进了监督损失，抑或学习率与量化算子发生了数值下溢？

与其在庞大的高层脚本里猜谜，不如把整套系统自底向上拆成一条 **20–27 周、六个阶段的递进验证链**：从纯 CPU 上的 NumPy 矩阵与手写反向传播（Backpropagation）出发，逐步穿过 PyTorch 训练工程、GPU 异步计时与显存剖析、深度学习收敛诊断、手写小型 Decoder-only Transformer，最终在 16GB 显存约束下合拢出一套可复现、可证伪的 LoRA / QLoRA 微调项目。

---

## 一、核心能力目标：建立可重放、可证伪的训练闭环

整条路线聚焦于单卡环境下的模型微调与性能诊断，把每一阶段的阅读材料压缩到最少，将验收标准直接绑定到可执行代码与实验记录上：

1. **剥离自动求导黑盒**：只用 NumPy 写出小型神经网络的前向传播、数值稳定 Softmax、交叉熵损失与反向梯度，并用有限差分（Finite Difference）验证每一层参数梯度的形状与精度。
2. **搭建稳健的训练骨架**：用 PyTorch 独立组织数据切分、训练/验证切换、混合精度对照以及包含优化器与随机状态的完整断点恢复（Checkpoint）。
3. **建立硬件级显存与计时直觉**：定量拆解权重、梯度、优化器状态与激活值的显存占比；用流同步与硬件事件对训练步做可靠剖析（Profiling），告别只看 `nvidia-smi` 利用率百分比的粗放判断。
4. **掌握收敛异常归因**：在小型分类与序列任务上主动复现欠拟合、过拟合与步长震荡，凭借训练/验证曲线和错误样本分布指导下一轮单变量实验。
5. **吃透自回归语言模型机制**：从零拼出一个小型 Decoder-only Transformer，亲手验证词元（Token）错位预测、因果注意力掩码、预填充（Prefill）与解码（Decode）阶段的 KV Cache 增长规律。
6. **交付 16GB 单卡微调闭环**：完成指令数据清洗、模板固定、4-bit NF4 量化加载与 LoRA / QLoRA 训练，在独立评估集上同时报告微调前后的改进项与退化案例。

至于原生 CUDA C++ 核函数编写，在路线中定位为**建立底层物理直觉的辅助扩展**——理解线程块（Block）、网格（Grid）与内存层次极有价值，但微调主线优先依托 PyTorch 与 Hugging Face 打通训练与评测闭环，遇到真实算子瓶颈时再按需下沉到 CUDA 或 Triton。

---

## 二、六阶段递进学习地图

六个阶段环环相扣，前一个阶段亲手踩过的坑正是后一个阶段高层抽象试图解决的问题。推进时不追求把所有 API 背熟再往下走，只要跑通当期代码产物、通过可证伪门禁并留存问题记录，即可进入下一环：

![从 NumPy 手写梯度到 16GB 单卡 QLoRA 微调的六阶段验证路线：跨越 CPU 数值底座、GPU 显存与收敛诊断、自回归 Decoder 及低秩微调闭环，并配齐三道可证伪验收门禁](/images/diagrams/aug-ai-roadmap-six-stages.svg)

| 阶段 | 周期规划 | 核心工程问题 | 阶段交付产物 |
| --- | --- | --- | --- |
| **1. NumPy 与数值底座** | 2–3 周 | 矩阵乘法、广播与反向梯度在内存里到底发生了什么？ | 纯 NumPy 手写两层 MLP 与梯度检查 |
| **2. PyTorch 训练骨架** | 3–4 周 | 框架如何接管计算图状态、断点续训与混合精度？ | 可替换模型与数据集的通用训练模板 |
| **3. GPU 边界与性能剖析** | 3–4 周 | 跨总线搬运有多贵？为什么算力没满显存却先爆了？ | CUDA 同步计时与 16GB 显存拆解报告 |
| **4. 深度学习收敛诊断** | 4–5 周 | Loss 不降或验证集回升时，如何区分数据、容量与步长问题？ | 图像或文本分类诊断与消融实验 |
| **5. Decoder-only LLM** | 4–5 周 | 因果掩码如何阻止信息穿越？KV Cache 省掉了哪部分计算？ | 从零实现的微型自回归语言模型 |
| **6. 16GB LoRA / QLoRA** | 4–6 周 | 如何把 7B 级模型稳妥塞进 16GB 显存并证明微调真有效？ | 端到端指令微调与盲测对比项目 |

### 阶段一：NumPy、内存步长与手写反向传播（2–3 周）

第一阶段完全不碰 GPU，专心消除张量形状（Shape）和链式法则上的模糊感。

> **当前进度：已完成。**
> - **理论底座**：[《从 NumPy 内存模型、手写两层 MLP 到 CuPy/CUDA 设备边界与异步计时》]({{< relref "2026-08-16-numpy-internals.md" >}})第一部分已完整拆解连续缓冲区、字节步长（`strides`）、零步长广播、`ufunc` 向量化以及底层 BLAS/LAPACK 分派机制。
> - **动手验收**：同一长文[第二部分（手写两层 MLP 与反向传播）]({{< relref "2026-08-16-numpy-internals.md" >}})已交付可在浏览器直接运行的交互式 Notebook，完成了双月牙非线性分类、平移不变稳定 Softmax、手写 `backward`、中心有限差分梯度检查（最大绝对误差 `1.51e-11`）及单变量学习率对照。

三周的具体推进节奏按“内存布局 → 损失函数数值稳定性 → 完整两层网络”展开：第 1 周每次写矩阵运算前先在纸上推导输入输出形状，并用纯 Python 循环对比 NumPy 向量化计算欧氏距离的调度开销；第 2 周实现线性层、ReLU、减去行最大值的稳定 Softmax 与交叉熵，观察不同学习率下的收敛、震荡与溢出；第 3 周把 `forward → loss → backward → update` 串成闭环，逐个核对参数与梯度的维度契合关系。只有亲手写过一遍 `ForwardCache`，后面看框架显存占用时才会真正明白前向激活为什么不能随手丢掉。

### 阶段二：搭起可复用的 PyTorch 训练模板（3–4 周）

有了手写梯度的底子，第二阶段把那套显式维护的缓存和参数更新迁移到 PyTorch 的自动微分（Autograd）体系上。

前两周聚焦于 `Tensor` 设备属性、动态计算图、`nn.Module` 与优化器生命周期，不仅要熟写 `zero_grad() → forward → backward() → step()`，更要厘清 `model.train()`、`model.eval()` 与 `torch.no_grad()` 各自控制的是 Dropout / BatchNorm 行为还是计算图构建开关（关闭计算图正是省掉前向激活缓存的关键）。第 3 周补齐 `Dataset`、`DataLoader`、分层划分与工业级 Checkpoint——合格的断点文件除模型权重外，还需打包优化器动量状态、当前轮次（Epoch）以及随机数生成器状态，确保中断重启后的损失曲线能够无缝续接。

第 4 周在 RTX 5070 Ti 上实测 `fp32`、`fp16` 与 `bf16`：固定同一模型与批次大小，对比吞吐量（Samples/s）、峰值显存与收敛稳定性。Blackwell 架构原生支持 `bf16`，其指数位宽度与 `fp32` 一致，不易发生梯度下溢；但排查算子数值异常时，切回 `fp32` 建立黄金基线依然是最稳妥的手段。

### 阶段三：跨过 PCIe 总线，建立 GPU 性能与显存意识（3–4 周）

这一阶段直面三个核心问题：GPU 什么时候真快、什么时候反而更慢，以及 16GB 显存究竟是怎么被一步步吃光的。

> **前置实验进度：已完成。**
> - [《从 NumPy 内存模型、手写两层 MLP 到 CuPy/CUDA 设备边界与异步计时》第三部分（CuPy/CUDA 设备边界与异步计时）]({{< relref "2026-08-16-numpy-internals.md" >}})已在本机跑通最小闭环并附逐行中文注释 Notebook，用 `3×4` 跨总线搬运反例、`10,003` 元素非整除 `RawKernel` 尾部越界守卫以及 `CUDA Event` 硬件卡点，把 CPU 同步直觉平稳过渡到了 GPU 异步流模型。

在此基础上继续向 PyTorch 训练剖析推进：第 1 周对比 CPU 与 CUDA 在不同矩阵规模下的乘法耗时，强制执行预热（Warmup）并在计时边界调用 `torch.cuda.synchronize()`，同时对比“一万次小矩阵发射”与“单次合并大矩阵计算”，直观量化核函数发射开销（Launch Overhead）。第 2 周在 16GB 显存上逐步拉高 `batch_size` 与序列长度，开启混合精度、梯度累积（Gradient Accumulation）与激活重计算（Activation Checkpointing），记录每项开关省下的显存字节与付出的重算耗时。第 3 周借助 `torch.profiler` 定位数据加载、前向、反向与参数更新各段占比，调优 `num_workers` 与锁页内存（`pin_memory`）。

第 4 周可按兴趣选修配套仓库的原生 CUDA C++ 实验（向量加法与共享内存分块矩阵乘）：

```bash
cd ~/work/gpu-llm-roadmap
source ./scripts/cuda_env.sh
cd experiments/chapter03
make run
```

入门无需一上来硬啃复杂的汇编调优，先把 `int idx = blockIdx.x * blockDim.x + threadIdx.x;` 这行全局线程映射以及全局内存合并访存（Coalesced Access）、片上共享内存（Shared Memory）的带宽级差吃透，看上层框架算子就会通透得多。

### 阶段四：诊断欠拟合、过拟合与优化停滞（4–5 周）

代码跑通且不报 OOM，离模型真正学到规律还有很远。第四阶段专门训练“看曲线下药”的排障手感。

前两周在 MLP 与卷积网络（如 CIFAR-10 分类）上对照 SGD、Momentum 与 AdamW 在不同初始化尺度下的收敛轨迹，核对卷积特征图尺寸变化、批归一化（Batch Normalization）与数据增强的作用。第 3 周做破坏性对照实验：刻意砍掉八成训练样本诱发严重过拟合，再引入 Dropout 与权重衰减（Weight Decay）观察验证集缺口如何收拢，并输出混淆矩阵（Confusion Matrix）揪出高频误判类别。第 4 周过渡到文本分类，把分词 ID、词嵌入（Embedding）、变长填充（Padding）与注意力掩码串起来，为下一阶段进入序列生成铺平道路。

### 阶段五：从因果自注意力到小型 Decoder-only LLM（4–5 周）

调开源大模型之前，先亲手搭一座几十兆参数的“麻雀虽小五脏俱全”的自回归模型。

第 1 周从字符级分词器写起，构造下一词元预测（Next-Token Prediction）数据对，验证输入序列 `x[0..T-1]` 与监督标签 `y[1..T]` 为什么要严格错开一位。第 2 周手写单头与多头因果自注意力（Causal Self-Attention），打印下三角掩码矩阵，确认第 `t` 个位置绝不会偷看到 `t+1` 之后的未来信息。第 3 周将词嵌入、位置编码、残差连接（Residual Connection）、归一化层、前馈网络与输出头拼成完整的微型 Decoder-only Transformer，在小语料上跑通损失稳步下降并采样出结构连贯的文本片段。

第 4–5 周切入 Hugging Face 生态，用 `AutoTokenizer` 与 `AutoModelForCausalLM` 加载开源小模型，对比贪心解码（Greedy）、温度缩放（Temperature）与核采样（Top-p）的生成差异；随后固定模型，扫描不同提示词长度、生成长度与并发批次，实测预填充（Prefill，计算密集型）与逐词解码（Decode，访存带宽受限型）两个阶段的延迟特征与 KV Cache 显存增长斜率。

### 阶段六：在 16GB 显存红线内跑通 LoRA / QLoRA（4–6 周）

最后一关把前面积累的数据治理、混合精度、显存账本与 Transformer 结构全部汇合到真实微调项目里。

第 1 周准备 500–5,000 条指令样本，统一规范为 `instruction / input / output` 结构，剔除空值、重复项与异常超长噪声，在动工训练前先锁死 Prompt 模板与独立验证集切分。第 2 周用小模型跑几十步冒烟测试（Smoke Test），重点核查分词截断、Padding 方向以及只有回答部分参与 Loss 计算的标签掩码（`-100` 忽略位）是否严丝合缝。第 3 周接入 PEFT 完成标准 LoRA 训练，记录目标模块（`target_modules`）、秩（`r`）、缩放系数（`alpha`）及可训练参数占比，验证单独导出的轻量 Adapter 挂载回原始基座后推理结果完全一致。

第 4 周在 16GB 显存上攻坚 7B/8B 级别模型的 QLoRA：先用 1B–3B 模型跑稳全流程基线，再以 4-bit NF4 量化加载基座、开启 `bf16` 计算，将 `micro_batch_size` 设为 `1` 并靠梯度累积凑足目标有效批次；依据真实样本长度分位数设定上下文上限（而非盲目照搬模型宣称的 32K/128K 上限），显存吃紧时打开 Gradient Checkpointing 并记录吞吐折损。第 5–6 周用预先冻结的 20–50 道测试题对基座模型和微调后模型做同参数盲测，按完全正确、部分正确、格式破损与事实幻觉四档归类，排查训练集泄漏，最终打包成可一键复现的完整工程。

---

## 三、算清 16GB 显存账本：RTX 5070 Ti 实验与排障策略

贯穿整条路线最硬的物理约束，就是那块无法扩容的 `15.92 GiB` 显存。把 7B 规模模型在全参数训练下的显存爆炸路径与 QLoRA 四管齐下的压缩路径摆在一起对照，就能看清每一项配置开关究竟在省哪一块的钱：

![RTX 5070 Ti 16 GiB 训练显存账本与 QLoRA 压缩机制：对比全参训练超 56 GiB 触发 OOM 与 NF4 量化权重、LoRA 低秩优化器状态、激活重计算协同压入 15.9 GiB 红线的完整账目](/images/diagrams/aug-ai-vram-16gb-budget.svg)

围绕这本显存账，日常实验严格执行三条铁律：

1. **先立度量衡，再动超参数**：每次性能或显存对照实验都固定记录模型规格、`dtype`、`micro_batch_size`、序列长度、Checkpointing 开关、峰值显存（`max_memory_allocated`）以及真实有效吞吐（`tokens/s`）。绝不只盯 `nvidia-smi` 里的 GPU 利用率——CPU 数据预处理卡顿、频繁发射碎小核函数或过多同步屏障，同样会让利用率忽高忽低。
2. **严分推理能装下与训练能跑稳**：能流畅对话的 7B/8B 模型，在开启全量反向传播时加上多模块 LoRA 旁路和长上下文激活，依然随时可能冲破 16GB。切勿因为权重加载成功就盲目拉长 `max_seq_length`。
3. **遇到 CUDA OOM 按物理来源逐级拆弹**：
   - 第一步先把 `micro_batch_size` 降到 `1`，改用 `gradient_accumulation_steps` 维持数学等价的有效批次；
   - 第二步统计数据集 Token 长度分布，砍掉极少数未截断的异常长文本（自注意力与中间激活随序列长度剧增）；
   - 第三步核查基座是否已启用 4-bit NF4 量化与 `bf16` 计算路径，优化器是否切换为可在显存尖峰时借用主机内存分页的 `paged_adamw_8bit`；
   - 第四步打开 `gradient_checkpointing` 丢弃中间层前向激活，用反传时的局部重计算换回几 GB 宝贵空间；
   - 第五步清理验证循环里残留的计算图引用或过大的评估批次；若连静态权重加最小单样本工作区都超出 16GB，果断换更小尺寸的基座模型，不在物理上不可能装下的配置上空耗时间。

---

## 四、实验归档与最终交付标准

收藏十篇综述不如跑通一个能证伪假设的脚本。每周实验固定围绕四个维度留档：**可从干净环境重跑的脚本或 Notebook**、**有曲线或指标支撑的实验结论**、**报错现场与被证伪的初始猜想**，以及**下一周只改一个自变量的最小切入口**。每走完一个阶段，关掉参考代码做一次闭卷检验：能否从空白文件独立敲出核心训练循环？把批次翻倍或把序列拉长一倍前，能否大致算准显存和形状会怎么变？

参考资料同样奉行少而精，优先查阅一手官方文档与带完整代码实现的经典材料：

- **CUDA 与硬件执行模型**：[NVIDIA CUDA C++ Programming Guide](https://docs.nvidia.com/cuda/cuda-c-programming-guide/)、[NVIDIA CUDA Best Practices Guide](https://docs.nvidia.com/cuda/cuda-c-best-practices-guide/)
- **PyTorch 与设备语义**：[PyTorch Tutorials](https://pytorch.org/tutorials/)、[PyTorch CUDA Semantics](https://pytorch.org/docs/stable/notes/cuda.html)
- **深度学习与 Transformer 拆解**：[Dive into Deep Learning](https://d2l.ai/)、[The Annotated Transformer](https://nlp.seas.harvard.edu/annotated-transformer/)
- **大模型与参数高效微调**：[Hugging Face LLM Course](https://huggingface.co/learn/llm-course/)、[Hugging Face PEFT 文档](https://huggingface.co/docs/peft/)

整条路线的终点是一份经得起独立复现检验的微调工程：README 交代清楚环境锁定、数据清洗规则、训练与挂载推理命令；实验报告算清显存与吞吐取舍背后的账；独立评测集诚实摊开微调带来的提升与新引入的退化。打穿这条单卡主线之后，再根据真实痛点决定下一步是深挖高吞吐推理引擎（vLLM、`llama.cpp`、TensorRT-LLM）、对齐算法（DPO、ORPO），还是底层算子融合（Triton、CUDA C++）。

---

## 五、环境与工具链说明

本路线配套实验运行于单机 Windows + WSL2 环境，硬件与软件栈配置如下：

| 组件 | 配置版本 | 在路线中的角色 |
| --- | --- | --- |
| **操作系统** | WSL2 / Ubuntu 24.04.4 LTS | Linux 开发与训练宿主环境 |
| **GPU 硬件** | NVIDIA GeForce RTX 5070 Ti（Blackwell，`sm_120`） | 单卡训练、剖析与本地推理 |
| **物理显存** | 16GB（PyTorch 检测可用约 `15.92 GiB`） | 约束模型参数规模、序列长度与批次上限 |
| **Python / PyTorch** | Python `3.12.3` / PyTorch `2.10.0+cu128` | 深度学习与微调主线运行时 |
| **项目 CUDA Toolkit** | CUDA `12.8`（`nvcc 12.8.61`） | 与 PyTorch `cu128` 对齐，编译扩展与配套实验 |
| **系统独立 Toolkit** | CUDA `13.3`（`/usr/local/cuda-13.3`） / 驱动接口 `13.3` | WSL 驱动最高支持版本，供独立原生 CUDA 实验调用 |

初次在 WSL2 下排查环境时，常会看到 `nvidia-smi` 顶部显示的 CUDA 版本（`13.3`）、PyTorch 携带的运行时（`12.8`）与系统 `nvcc` 版本不一致。这三者本来就各司其职：WSL 桥接的宿主机驱动报告 `13.3` 代表驱动向下兼容的最高上限；PyTorch `cu128` 轮子自带所需的 CUDA 12.8 动态库；`nvcc` 则只在编译原生 CUDA C++ 或自定义算子扩展时登场。无需为了让三个数字长成一样而反复重装系统——日常跑 PyTorch 主线及编译扩展时固定指向 `CUDA 12.8` 避免 ABI 混用，独立测试新特性时显式调用 `/usr/local/cuda-13.3/bin/nvcc` 即可。

在配套实验仓库中可通过如下脚本一键完成环境变量挂载与 `4096 × 4096` 矩阵乘法连通性核验（若 WSL 终端未自动将 `/usr/lib/wsl/lib` 加入 `PATH`，可直接调用 `/usr/lib/wsl/lib/nvidia-smi` 查看设备状态）：

```bash
cd ~/work/gpu-llm-roadmap
source ./scripts/cuda_env.sh
./scripts/check_env.sh
```
