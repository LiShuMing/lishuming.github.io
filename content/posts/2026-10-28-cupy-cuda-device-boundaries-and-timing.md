---
title: "从 NumPy 到 CUDA：用 CuPy 验证设备边界、线程索引与异步计时"
date: 2026-10-28T00:00:00+08:00
lastmod: 2026-10-28T00:00:00+08:00
slug: "cupy-cuda-device-boundaries-and-timing"
categories:
  - AI Infra
tags:
  - CuPy
  - CUDA
  - NumPy
  - Jupyter
  - 性能分析
description: "在 RTX 5070 Ti 与 WSL 上，把 NumPy 数组迁移到 CuPy，验证真实 GPU 运算、RawKernel 线程边界与 CUDA Event 计时，并保留旧失败记录和本次复查的适用范围。附中文注释 Notebook。"
draft: false
toc: true
---

在[NumPy 理论学习]({{< relref "2026-08-16-numpy-internals.md" >}})和[手写 MLP 实践]({{< relref "2026-08-16-numpy-mlp-practice.md" >}})之后，我想把学习推进到一个更具体的问题：同样是一行矩阵乘法，当数组从主机内存进入显存（VRAM），哪些成本和约束随之改变？

CuPy 保留了熟悉的数组接口，适合用很小的程序观察这个变化。这次实验依次做 CPU 参照、显式数据传输（Data Transfer）、GPU 矩阵乘法、自定义 CUDA 内核（CUDA Kernel）和同步计时。它不依赖 PyTorch，也不涉及模型训练；目标是把设备位置、线程索引（Thread Index）和完成时间这几个概念落实到能检查的代码上。

原始学习 Notebook 保存过一次 GPU 不可用的输出。这次整理时重新执行检查，设备已经可见，矩阵乘法、收入归约和向量加法均通过。两份记录不能合并成一句“CUDA 一直工作正常”，也不能仅凭旧错误文本认定驱动版本就是根因。

本文基于 2026 年 10 月 28 日的本机复查。下载版提供逐行中文注释，清除了本机路径和执行输出，适合按顺序重跑：

- <a href="/code/cupy-cuda-basics/cupy-cuda-basics.ipynb" download>下载配套 Notebook：cupy-cuda-basics.ipynb</a>
- <a href="/code/cupy-cuda-basics/requirements.txt" download>下载本次直接依赖版本：requirements.txt</a>

Notebook 需要连接本机或远程的 Python CUDA Kernel。本站已有的 JupyterLite 使用浏览器内的 Python 运行时（Python Runtime），不能替代访问 NVIDIA 驱动的原生进程；因此这份附件在本地 Jupyter 中执行。

## 检查环境时，要让返回值对应一个明确结论

原来的诊断脚本 `check_cuda.py` 把检查分成两种模式。普通模式确认 NumPy、pandas、JupyterLab 和 CuPy 能否导入；严格模式还要求在 GPU 上完成一次矩阵乘法，并与 NumPy 比较结果。

这两个返回值回答不同问题。Python 包导入成功，只说明解释器找到了依赖；设备发现成功，还不能证明后续库调用、运行时编译（Runtime Compilation）和实际运算可用。严格检查需要完成后面这些步骤：

```text
Python 包导入
  → CUDA 设备发现
  → 创建 GPU 数组
  → 执行矩阵乘法
  → 回传结果
  → 与 NumPy 参照比较
```

旧诊断记录中，`environment_ready=true`，但 `gpu_verified=false`；直接 Driver API 调用 `cuInit(0)` 返回 `CUDA_ERROR_NO_DEVICE`，CuPy 则报 `cudaErrorInsufficientDriver`。这些信息说明当时无法执行 GPU 路径，但不足以把问题收敛到某个驱动、Toolkit 或设备链路。

本次复查没有改动驱动或系统配置。普通检查和 `--require-gpu` 均通过，`cuInit(0)` 返回成功，并发现一张 RTX 5070 Ti。随后原 Notebook 全部代码单元执行完成，没有进入跳过分支。

| 层次 | 本次实际观察 | 这个结果支持什么 |
| --- | --- | --- |
| Python 依赖 | Python 3.12.3、NumPy 2.5.0、pandas 3.0.3、JupyterLab 4.6.1、CuPy 14.2.0 | 对应解释器可以加载这些包 |
| 设备与驱动 | RTX 5070 Ti、驱动 610.88、显存查询 16,303 MiB | 当前进程可以访问这张 GPU |
| 本地工具链 | `/usr/local/cuda` 指向 CUDA 13.3，`nvcc 13.3.73` | 明确独立 CUDA 工具链的位置 |
| 数值计算 | GPU 矩阵乘法与收入归约（Reduction）均匹配 CPU 参照 | 这两个样例的运算结果通过 |
| 自定义内核 | 10,003 个元素的向量加法通过 | 此内核、dtype 和长度组合可执行 |

这里记录的是本机组合，不是官方兼容矩阵。CuPy 14.2 安装文档的 NumPy 测试列表到 2.4，CUDA 列表到 13.2；表中更高的小版本只能按这次实际执行过的范围记录，不能由两个小样例推广到全部功能。[CuPy 14.2 安装说明](https://docs.cupy.dev/en/v14.2.0/install.html)

下载版 Notebook 沿用这个判断方式：缺少 CuPy 或设备不可见时，CPU 基线仍可执行，GPU 单元明确打印 `SKIPPED`；设备可见以后发生的运算错误会直接使单元失败。跳过与通过必须能够区分，否则自动执行 Notebook 时很容易得到一份“全部单元结束”，但没有任何 GPU 工作实际发生的报告。

## 先固定解释器，再选择 Toolkit

这次学习环境复用了已有 NumPy/pandas 虚拟环境（Virtual Environment），新增依赖为 `cupy-cuda13x==14.2.0`。只安装 CuPy 不会自动把 pandas 的普通列计算迁移到 GPU，也不需要为了执行这个实验额外安装训练框架。

若在一个新目录中使用下载附件，可以按以下方式建立环境。先把 Notebook 和 `requirements.txt` 保存到同一目录：

```bash
mkdir -p ~/work/cupy-cuda-basics
cd ~/work/cupy-cuda-basics
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt

.venv/bin/python -m ipykernel install --sys-prefix \
  --name cupy-cuda-basics --display-name "Python 3 (CuPy CUDA)"
```

依赖文件固定了本次观察到的 NumPy、pandas、JupyterLab 和 CuPy 版本，未锁定全部传递依赖（Transitive Dependency）。它用于复查这次实验，不等同于覆盖其他机器的环境保证。使用已有环境时，先确认 CUDA 大版本并选择对应的 CuPy wheel，避免把多个 `cupy` 变体装进同一个环境。

本机同时存在多套 CUDA 工具链，启动 Jupyter 时显式选择 WSL 内的 `/usr/local/cuda`：

```bash
CUDA_PATH=/usr/local/cuda \
PATH="/usr/local/cuda/bin:$PATH" \
LD_LIBRARY_PATH="/usr/local/cuda/lib64:/usr/lib/wsl/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" \
.venv/bin/python -m jupyterlab \
  --ip=127.0.0.1 --port=8891 --no-browser
```

这组变量只影响当前启动命令。Jupyter 保留默认的 Token 认证；打开它输出的带 Token 地址，然后选择 `Python 3 (CuPy CUDA)` 内核。

驱动、Toolkit 和 Python wheel 处于不同层。WSL 使用 Windows 提供的 NVIDIA 驱动接口，Linux Toolkit 提供开发与运行库。Windows 中安装的 Toolkit 不等于 WSL 内的 Toolkit；`nvidia-smi` 显示的 CUDA 能力也不能替代当前进程实际加载的库版本。NVIDIA 的 WSL 文档明确说明，WSL 内不需要另装 Linux 显示驱动。[CUDA on WSL 用户指南](https://docs.nvidia.com/cuda/wsl-user-guide/index.html)

后面的 `RawKernel` 默认通过 NVRTC 编译，而不是每次调用命令行 `nvcc`。因此检查 `nvcc --version` 能确定一个工具的位置，却不能单独证明 Python 路径使用了哪些库；最终仍要执行实际内核。

## 用一次矩阵乘法看清数据在哪里

先用很小的数据建立 CPU 参照。这里同时保留 pandas 的收入计算，目的是观察表格列何时才真正进入 GPU：

```python
a_cpu = np.arange(12, dtype=np.float32).reshape(3, 4)  # 创建 0..11，并改为 3 行 4 列。
expected = a_cpu @ a_cpu.T  # (3,4) @ (4,3) 得到 (3,3) 的 CPU 参照结果。
sales = pd.DataFrame({"quantity": [2, 5, 3], "price": [10.0, 8.0, 15.0]})  # 创建 CPU 表格。
sales["revenue"] = sales["quantity"] * sales["price"]  # 逐行计算收入，不会调用 CUDA。
revenue_cpu = sales["revenue"].to_numpy(dtype=np.float32)  # 取出数值列并明确为 float32。
print("CPU 矩阵乘法：\n", expected)  # 打印可以人工核对的矩阵。
print("收入合计：", revenue_cpu.sum())  # CPU 求和应为 105。
```

`a_cpu` 的 shape 是 `(3, 4)`，转置后为 `(4, 3)`，乘积为 `(3, 3)`：

```text
[[ 14.  38.  62.]
 [ 38. 126. 214.]
 [ 62. 214. 366.]]
```

`sales["revenue"]` 的乘法和 `to_numpy()` 都在 CPU 侧。只有显式调用 `cp.asarray()`，这份数值数组才被送入当前 GPU。下面摘自 Notebook 的设备计算单元，依赖环境检查单元定义的 `gpu_ready`：

```python
if gpu_ready:  # 只有设备发现成功才尝试真实 GPU 运算。
    a_gpu = cp.asarray(a_cpu, blocking=True)  # 把 CPU 数组复制到当前 GPU，并等待上传完成。
    product_gpu = a_gpu @ a_gpu.T  # 在 GPU 上计算 3×3 矩阵乘积。
    result_cpu = cp.asnumpy(product_gpu)  # 默认 blocking=True，回传并等待 CPU 结果可用。
    np.testing.assert_allclose(result_cpu, expected, rtol=1e-5, atol=1e-6)  # 对比绝对与相对误差。
    revenue_gpu = cp.asarray(revenue_cpu, blocking=True)  # 只上传选出的数值列。
    total_gpu = revenue_gpu.sum()  # 在 GPU 上归约得到标量数组。
    total_cpu = cp.asnumpy(total_gpu)  # 读取标量也需要同步与设备到主机的传输。
    np.testing.assert_allclose(total_cpu, revenue_cpu.sum(), rtol=1e-5, atol=1e-6)  # 校验求和。
    gpu_verified = True  # 只在两项真实 GPU 校验完成后记录通过。
    print("PASS: GPU 矩阵乘法与归约结果均匹配 NumPy。")
else:
    print("SKIPPED:", gpu_reason)  # 跳过只说明设备不可用，不是 GPU 测试通过。
```

这段代码的三个边界分别是：

| 调用 | 输入与输出位置 | 必须理解的语义 |
| --- | --- | --- |
| `cp.asarray(a_cpu, blocking=True)` | CPU → GPU | 本例需要复制；等待上传完成后才返回 |
| `a_gpu @ a_gpu.T` | GPU → GPU | 计算结果仍是设备数组，通常相对 CPU 异步执行 |
| `cp.asnumpy(product_gpu)` | GPU → CPU | 默认阻塞，返回时 CPU 结果已经可用 |

`asarray` 不是无条件复制：输入已经是兼容的 CuPy 数组时，它可以复用设备内存。本例使用 NumPy 输入，才需要主机到设备复制。CuPy 14.2 的 `asarray` 默认 `blocking=False`；这里显式使用 `True`，减少入门实验对主机缓冲区生命周期与并发修改的要求。[cupy.asarray API](https://docs.cupy.dev/en/v14.2.0/reference/generated/cupy.asarray.html)

相反，`asnumpy` 默认 `blocking=True`。验证发生在回传之后，所以 NumPy 看到的是已完成的输出，而不是仅仅已经排队的计算。[cupy.asnumpy API](https://docs.cupy.dev/en/v14.2.0/reference/generated/cupy.asnumpy.html)

`assert_allclose` 按 `abs(actual - expected) <= atol + rtol * abs(expected)` 判断误差。这里明确设置了绝对容差（Absolute Tolerance）与相对容差（Relative Tolerance），方便把数据错误和浮点舍入差异分开；它并不证明所有输入都正确。

三个收入值的总和是 105。在 GPU 上计算这三个数没有性能优势需要证明，反而会额外引入上传、内核提交与标量回传。这部分的作用是暴露边界：接口相似，并不会让数据移动成本消失。

## RawKernel 把数组接口还原成指针与线程

矩阵乘法隐藏了线程组织。要理解一个线程如何对应一个元素，可以先写最小的向量加法。Notebook 的这一单元只在前面的真实 GPU 校验通过后运行：

```python
if gpu_verified:  # 前一单元必须先证明真实运算可用。
    vector_add = cp.RawKernel(
        r"""
        extern "C" __global__  // 固定导出名称，并声明为 GPU kernel。
        void vector_add(const float* x, const float* y, float* out, int n) {
            int i = blockIdx.x * blockDim.x + threadIdx.x;  // 计算线程的一维全局编号。
            if (i < n) {  // 最后一组线程可能超过数组长度，必须检查边界。
                out[i] = x[i] + y[i];  // 每个有效线程只写自己负责的元素。
            }
        }
        """,
        "vector_add",  # 函数名必须与 CUDA 源码中的导出名称相同。
    )
    n = 10003  # 刻意选择不能被 256 整除的长度。
    threads = 256  # 每个 Block 的线程数。
    blocks = (n + threads - 1) // threads  # 向上取整，保证最后几个元素也被覆盖。
    x_gpu = cp.arange(n, dtype=cp.float32)  # 直接在 GPU 上创建连续的一维输入。
    y_gpu = cp.ones(n, dtype=cp.float32)  # 创建相同 dtype 的第二个输入。
    out_gpu = cp.empty_like(x_gpu)  # 分配输出但不清零，kernel 必须写完所有有效位置。
    vector_add((blocks,), (threads,), (x_gpu, y_gpu, out_gpu, np.int32(n)))  # grid、block、参数元组。
    expected_add = np.arange(n, dtype=np.float32) + 1  # CPU 端建立参照结果。
    np.testing.assert_allclose(cp.asnumpy(out_gpu), expected_add)  # 回传同时等待 kernel 完成。
    print("PASS: RawKernel 向量加法，40 个 Block、10240 个线程、10003 个有效元素。")
else:
    print("SKIPPED: RawKernel 需要先通过真实 GPU 校验。")
```

决定覆盖范围的是 `blocks = (n + threads - 1) // threads`。当 `n=10003`、`threads=256` 时，需要 40 个线程块（Thread Block），实际启动 10,240 个线程，最后 237 个没有对应元素。`if (i < n)` 正是为了让这些线程不读写数组边界之外的位置。

`grid` 的单位是 Block，`block` 的单位是线程，两者在这里都是一维元组（Tuple）。最后一个参数使用 `np.int32(n)`，与 CUDA 函数中的 `int n` 明确对应；数组使用连续的 `float32`，与 `const float*` 和 `float*` 对应。

`cp.empty_like()` 不清零。只要某个有效位置没有被 kernel 覆盖，读回的就可能是旧内容；完整覆盖输出是这段代码必须满足的条件。每个有效线程只写 `out[i]`，输入与输出分开，也避免了本例中的跨线程写冲突。

另一条容易遗漏的约束是步长（Stride）。普通 CuPy 数组运算可以理解视图的 shape 和 strides，自定义 CUDA 函数拿到的却只是指针。本内核按连续地址访问，不能直接把转置视图或 `x[::2]` 当作同样的输入。要支持它们，需要显式传入步长，或先生成连续副本并把复制成本计入比较。[RawKernel 接口与视图限制](https://docs.cupy.dev/en/v14.2.0/reference/generated/cupy.RawKernel.html)

下载版还用同一个 kernel 检查了长度 `0、1、255、256、257、10003`。空数组直接跳过启动，不构造零 Block 的 Grid；其他长度验证边界保护。它覆盖的是这一维度，不代表已经完成 dtype、别名、多维布局和多 CUDA 流（CUDA Stream）的通用测试。

## 异步计时必须先说明统计范围

GPU 工作通常相对 CPU 异步提交。在 Python 语句前后直接读取墙钟，可能只得到提交开销。即使结果最后是对的，计时边界也可能放错。

本次原 Notebook 对 `1024×1024` 的 `float32` 数组先预热 3 次，再连续执行 10 次 `x @ x.T`，使用 CUDA 事件（CUDA Event）记录时间，并等待终点事件完成。本次重跑的一轮平均值为 **0.123 ms**。

这个数值是单次复查样本：没有做跨进程分布、重复轮次置信区间或 CPU 对照，也没有启用统一的频率与功耗控制。它只说明当时这一条已经预热、输入已在 GPU 上的路径能被实际执行与计时。

下载版把计时步骤展开如下：

```python
if gpu_verified:  # 没有真实 GPU 时不生成计时数字。
    size = 1024  # 输入和输出均为 1024×1024。
    repeats = 10  # 统计连续 10 次矩阵乘法的平均设备时间。
    timed_x = cp.random.random((size, size), dtype=cp.float32)  # 数据直接生成在 GPU。
    for _ in range(3):  # 预热 cuBLAS、相关分配和执行路径。
        timed_result = timed_x @ timed_x.T
    cp.cuda.get_current_stream().synchronize()  # 在开始计时前等待预热完成。
    start_event = cp.cuda.Event()  # 创建 CUDA 起点事件。
    end_event = cp.cuda.Event()  # 创建 CUDA 终点事件。
    start_event.record()  # 把起点插入当前 CUDA Stream。
    for _ in range(repeats):  # 与预热保持相同运算和 dtype。
        timed_result = timed_x @ timed_x.T
    end_event.record()  # 把终点排在全部矩阵乘法之后。
    end_event.synchronize()  # 等待终点执行，避免只测到异步提交。
    mean_gpu_ms = cp.cuda.get_elapsed_time(start_event, end_event) / repeats  # 事件间毫秒除以次数。
    print(f"GPU 驻留数据的 matmul 平均时间: {mean_gpu_ms:.3f} ms")
else:
    print("SKIPPED: 没有可用 GPU，不能报告 GPU 性能。")
```

起点事件排在预热之后，终点事件排在十次运算之后；`synchronize()` 保证读到的时间对应已完成工作。CPU 的调用顺序与设备执行完成顺序由此建立起明确联系。CuPy 的 `cupyx.profiler.benchmark()` 也采用 CUDA Event 与预热来处理这个问题。[CuPy 性能计时说明](https://docs.cupy.dev/en/v14.2.0/user_guide/performance.html)

不过，事件间隔仍不是一个独立底层 kernel 的纯执行时间。`x @ x.T` 经过数组分派和库调用，循环还会创建结果数组，并受到内存池（Memory Pool）状态影响。这里没有用 profiler 拆分各项贡献，不能继续把 0.123 ms 解读成某个内核、Tensor Core 或编译选项的单独成绩。

如果数据来自 CPU，而且结果也要立刻回到 CPU，就应当测另一条路径：

```python
if gpu_verified:  # 与前一个计时单元使用相同设备。
    rng = np.random.default_rng(0)  # 固定 CPU 随机种子，便于复跑相同数据。
    host_x = rng.random((size, size), dtype=np.float32)  # 主机输入在计时前准备。
    cp.cuda.get_current_stream().synchronize()  # 排空前一单元的设备工作。
    wall_start = time.perf_counter()  # 从开始上传前记录墙钟时间。
    device_x = cp.asarray(host_x, blocking=True)  # 计入主机到设备的复制。
    device_result = device_x @ device_x.T  # 计入 GPU 矩阵乘法。
    host_result = cp.asnumpy(device_result)  # 默认阻塞，计入回传并等到结果可用。
    end_to_end_ms = (time.perf_counter() - wall_start) * 1000  # 得到本轮端到端毫秒。
    np.testing.assert_allclose(host_result, host_x @ host_x.T, rtol=1e-4, atol=1e-4)  # 校验不计入耗时。
    print(f"上传 + matmul + 回传，本轮端到端时间: {end_to_end_ms:.3f} ms")
else:
    print("SKIPPED: 端到端 GPU 路径未执行。")
```

这次墙钟从上传前开始，直到默认阻塞的回传完成才结束。CPU 随机数组准备和 NumPy 正确性校验都在计时范围之外。两个样本的统计范围如下：

| 口径 | 包含 | 排除 |
| --- | --- | --- |
| GPU 数据已驻留时的事件计时 | 十次矩阵乘法循环对应的设备时间 | 上传、回传、首次环境初始化 |
| 单轮端到端计时 | 上传、矩阵乘法、回传及调用等待 | 主机输入构造、结果的 CPU 校验 |

前者适合研究多个算子持续消费设备数组时的计算成本，后者接近“CPU 提交一份任务，最后取回结果”的使用方式。比较 CPU 和 GPU 时，应统一 dtype、数据位置、完成条件与统计范围；不能把一个不含传输的 GPU 时间除以一个包含输入准备的 CPU 时间，就宣称获得了某个加速倍数。

## 这一步接到后续学习的哪里

这次实践建立的是一条最小 CUDA 验证路径：解释器和依赖可追踪，数据位置显式可见，自定义 kernel 有长度与类型约束，计时等待实际完成。它适合放在[GPU 与 LLM 学习路线]({{< relref "2026-08-15-gpu-to-llm-finetuning-roadmap.md" >}})的 NumPy 基础之后，作为进入 GPU 性能实验的补充；PyTorch 训练、自动求导和微调阶段仍需独立完成。

下一轮可以先在同一个 Notebook 中改变矩阵规模，保留输入已驻留与含传输两种口径，观察小任务被提交和传输成本主导的范围。随后比较同一份数据反复上传与一次上传后连续计算，再进入内核融合（Kernel Fusion）和 profiler 分析。每轮只改变一个变量，比同时调整 dtype、矩阵大小和线程数更容易解释结果。

[Strata 的本地分层推理实践]({{< relref "2026-10-22-strata-windows-tiered-inference.md" >}})则提供了另一种尺度的观察：模型推理需要决定专家权重驻留在哪里，CuPy 实验需要决定数组何时跨设备移动。前者涉及缓存命中（Cache Hit）、CPU 协同和 SSD 后备，不能用这次单个矩阵乘法的时间预测其 token 吞吐；共同需要核对的是，计算是否已经被数据传输或等待覆盖。

本次原始实验来自本机 `python/projects/py-numpy-pandas`，基线为 `42f8853`，完整提交号保留在下载 Notebook 的元数据中。下载附件是这次整理后的中文注释版，增加了长度边界与端到端计时；原仓库保持不变。复查附件时，请保留设备、版本、预热次数、重复次数与跳过状态；如果这些条件不同，应把新结果记录成一次新实验。
