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
description: "当一行矩阵乘法跨过 PCIe 总线从主机内存进入 GPU 显存，底层约束发生了什么质变？用最小可复现的 CuPy 与 RawKernel 实验拆解显式数据搬运、10,003 元素线程边界与 CUDA Event 异步计时。附逐行中文注释 Notebook。"
draft: false
toc: true
math: false
---

在前面的[《NumPy 核心机制剖析》]({{< relref "2026-08-16-numpy-internals.md" >}})和[《手写多层感知机（MLP）实践》]({{< relref "2026-08-16-numpy-mlp-practice.md" >}})里，所有的张量计算都待在同一个温室里：CPU 核心透过各级高速缓存直接读写主机内存（Host DRAM），一行 `a @ a.T` 执行返回的那一刻，结果矩阵里的每一个浮点数都已经稳稳落在了内存地址上。

一旦把 `import numpy as np` 换成 `import cupy as cp`，代码长相几乎一模一样，但底层的物理世界已经彻底换了天地——数据不再躺在 CPU 手边，而是必须跨过 **PCIe 总线**搬进显卡独立的**显存（VRAM）**；算子不再是单线程或几个核上的循环，而是拆成上万个并发线程（Thread）映射到流式多处理器（SM）上；就连 Python 函数返回的那一瞬间，GPU 往往才刚刚从异步命令队列里接过活儿，计算还远未结束。

在直接扎进庞大的 PyTorch 训练框架之前，CuPy 恰好提供了一块极佳的透明试验田：它既保留了熟悉的数组接口，又允许直接内联原生 CUDA C++ 核函数（Kernel）。只要亲手跑通几个二十行以内的小实验，就能把 GPU 编程最关键的三次心智转换钉死在代码上：

1. **显式的设备内存边界**：数据不会自己瞬移过 PCIe 总线；把 3 个数字搬上 GPU 求和，花在总线往返与核函数发射上的开销远比计算本身昂贵得多。
2. **从多维视图退回裸指针与线程网格**：揭开高级算子外壳，底层只有一维连续指针、`blockIdx.x * blockDim.x + threadIdx.x` 全局编号，以及为了覆盖 `10,003` 个元素而启动 `10,240` 个线程时必不可少的尾部越界保护。
3. **异步发射与真实完成时间解耦**：在 Python 里直接用 `time.perf_counter()` 包住一次 GPU 矩阵乘法，测出来的其实只是往 CUDA 流（Stream）里塞一条指令的微秒级排队耗时，真正的设备耗时必须靠同步屏障或 CUDA 事件（`cudaEvent`）来卡点。

本文配套的完整交互式实验已整理为逐行中文注释版（由于需要直接调用宿主机 NVIDIA 驱动，请在本地或远端原生 Python CUDA 内核中打开，浏览器内的纯 WebAssembly 环境如 JupyterLite 无法访问物理显卡）：

- <a href="/code/cupy-cuda-basics/cupy-cuda-basics.ipynb" download>下载配套 Notebook：cupy-cuda-basics.ipynb</a>
- <a href="/code/cupy-cuda-basics/requirements.txt" download>下载本次直接依赖版本：requirements.txt</a>

---

## 一、环境分层与验证门禁：为什么 `import cupy` 成功不等于 GPU 可用？

很多人在 WSL（Windows Subsystem for Linux）下配 GPU 环境踩的第一个坑，就是看到 `import cupy` 没报错、甚至整份 Notebook 从头跑到尾都没抛异常，就以为自己已经跑通了 CUDA——殊不知代码可能早就顺着某个 `if not gpu_available:` 分支静默跳过了所有设备计算。

从软件栈自底向上看，WSL 里的 Python GPU 调用其实横跨了三层完全独立的组件：

1. **宿主机驱动层（Windows NVIDIA Driver）**：按照 [CUDA on WSL 官方指南](https://docs.nvidia.com/cuda/wsl-user-guide/index.html)，WSL 内部绝不能再装一套 Linux 显卡驱动，而是直接通过 `/usr/lib/wsl/lib` 映射调用 Windows 宿主机提供的底层驱动接口（`libcuda.so`）。
2. **Linux 开发运行库层（CUDA Toolkit）**：位于 WSL 内的 `/usr/local/cuda`，提供 `cuBLAS` 等数学库以及运行时编译（NVRTC）所需的头文件与动态库。注意 CuPy 的 `RawKernel` 默认走进程内 NVRTC 即时编译，而非每次调命令行 `nvcc`——所以终端敲 `nvcc --version` 有输出，不代表 Python 进程加载对了动态库路径。
3. **Python 虚拟环境层（Wheel 包）**：例如针对 CUDA 13 系列安装的 `cupy-cuda13x==14.2.0`（参考 [CuPy 14.2 安装说明](https://docs.cupy.dev/en/v14.2.0/install.html)，同一个虚拟环境里切勿混装多个 `cupy` 变体）。

正因为这三层随时可能脱节，我在诊断脚本 `check_cuda.py` 里把验证收紧成环环相扣的六道关卡：先确认 Python 包导入成功，接着调用 `cuInit` 完成 CUDA 驱动初始化与设备枚举，随后分配显存完成 `Host → Device` 数组上传，在设备侧跑通真实矩阵乘法与自定义核函数编译，再经 `Device → Host` 同步拷回结果，最后与 NumPy 基准严格比对浮点误差。

这份区分绝非多此一举。翻看这台机器早先留存的一份失败快照，当时 Python 包导入完全正常（`environment_ready=true`），但严格检查（`--require-gpu`）直接拦截报错：底层 Driver API 调用 `cuInit(0)` 返回 `CUDA_ERROR_NO_DEVICE`，CuPy 抛出 `cudaErrorInsufficientDriver`（`gpu_verified=false`）——这正是典型的宿主机外接显卡链路离线或驱动映射未就绪现场。待硬件枚举恢复后重新复查，无需重装任何驱动，同一套严格检查一次通过。配套 Notebook 也严格沿用了这一准则：无可用显卡时显式打印 `SKIPPED` 以区别于 `PASS`；一旦检测到显卡存在，后续任何编译或计算异常都直接令单元格报错中断，绝不含糊放行。

若在新目录从零复现实验，把下载的 Notebook 和 `requirements.txt` 放入同一目录后，按下面两步即可干净地固定虚拟环境与 WSL 库路径：

```bash
mkdir -p ~/work/cupy-cuda-basics
cd ~/work/cupy-cuda-basics
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt

.venv/bin/python -m ipykernel install --sys-prefix \
  --name cupy-cuda-basics --display-name "Python 3 (CuPy CUDA)"
```

当机器里并存多套 CUDA 目录时，启动 JupyterLab 时显式指定 `/usr/local/cuda` 与 WSL 驱动桥接目录（仅作用于当前进程，连接后选中 `Python 3 (CuPy CUDA)` 内核即可）：

```bash
CUDA_PATH=/usr/local/cuda \
PATH="/usr/local/cuda/bin:$PATH" \
LD_LIBRARY_PATH="/usr/local/cuda/lib64:/usr/lib/wsl/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" \
.venv/bin/python -m jupyterlab \
  --ip=127.0.0.1 --port=8891 --no-browser
```

---

## 二、跨过 PCIe 总线：从一次 3×4 矩阵乘法看显式数据搬运

配好环境后，我们先在 CPU 侧用最小的数据集建立一目了然的基准真值，顺手放进一张三行的 `pandas` 销售表，看看日常数据分析里的数据究竟在哪个瞬间才真正踏上显卡：

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

注意：哪怕虚拟环境里装好了 CuPy，`pandas` 的 `sales["quantity"] * sales["price"]` 和 `.to_numpy()` 依然百分之百运行在主机内存里。要让数据跨过 PCIe 总线进入显存，必须显式调用 `cp.asarray()`；算完之后要让 NumPy 拿到结果做校验，又必须显式调用 `cp.asnumpy()` 把数据拉回来：

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

再看后面那个把 3 个销售额（`20, 40, 45`）传上显卡算出 `105` 的例子——在工程实践里，这恰恰是一个**典型的性能反例**：为了加三个浮点数，先后付出了主机到设备拷贝、归约核函数发射、流同步等待以及设备到主机标量回传四笔固定买路钱，速度比 CPU 直接加慢上几个数量级。把它写在这里，就是要直观地破除一个迷思：**API 长得像 NumPy，绝不意味着跨设备搬运的物理延迟消失了；只有计算密集度足够高，或者数据上传一次后能在显存里连续跑完长串算子链路，GPU 的高带宽与高并发才能赚回票价。**（校验时用 `assert_allclose` 显式设好相对容差 `rtol=1e-5` 与绝对容差 `atol=1e-6`，则是为了把正常的浮点累加顺序舍入差异与真正的逻辑错误区分开。）

---

## 三、揭开算子黑盒：用 `RawKernel` 处理 10,003 个元素的线程边界

调 `a_gpu @ a_gpu.T` 时，cuBLAS 替我们包办了所有脏活。那如果我们自己写一个最简单的逐元素向量加法 `out[i] = x[i] + y[i]`，GPU 到底是怎么知道哪个线程该算哪一个下标的？

在 CUDA 的执行模型里，成千上万个线程被分装进一个个定长的**线程块（Thread Block）**，若干个线程块再排成一张**网格（Grid）**。假设我们规定每个 Block 固定装 `256` 个线程，而手头要处理的数组长度偏偏是一个除不尽的质数——**`n = 10003`**。

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
4. **裸指针看不懂视图步长（Stride）**：这是从 NumPy 转写 CUDA 内核最容易踩的大坑！在 NumPy 或高层 CuPy 算子里，转置视图 `x.T` 或隔行切片 `x[::2]` 只是改了元数据里的 `shape` 和 `strides`，算子会自动跳着地址读；但 [CuPy `RawKernel` 文档](https://docs.cupy.dev/en/v14.2.0/reference/generated/cupy.RawKernel.html)写得很清楚，传进 C++ 核函数的 `const float* x` 只是一根干巴巴的首地址裸指针！如果你把非连续切片直接塞进上面的 `vector_add`，核函数依然会傻乎乎地按连续偏移 `x[i]` 往下读，算出彻底错乱的结果——要么在调用前先调 `cp.ascontiguousarray()` 付出一次显存重排代价，要么把各维步长显式作为参数传进核函数自己算偏移。

---

## 四、异步流与计时陷阱：为什么不能直接用 `perf_counter` 测算子？

写完核函数，下一步自然想测测 GPU 到底有多快。但如果照搬测试纯 Python 代码的习惯，写出下面这样的计时逻辑：

```python
# 错误示范：缺少流同步的纯墙钟计时！
t0 = time.perf_counter()
res = timed_x @ timed_x.T
elapsed = time.perf_counter() - t0
```

你会测出一个快得离谱的微秒级数字，而且不管矩阵从 `1024×1024` 涨到 `8192×8192`，这个数字几乎纹丝不动！原因很简单：CPU 调用 `timed_x @ timed_x.T` 时，只是往当前 CUDA Stream 的命令队列里塞了一张任务描述符就立刻返回跑下一行 `perf_counter()` 了，此时显卡上的计算核心可能才刚刚开始热身。将 Python 主机线程、CuPy 运行时、PCIe DMA 总线与 GPU CUDA Stream 放在同一张时序图上对照，异步发射失真与真实同步卡点的差异便清晰可见：

![CuPy 异步流发射陷阱与同步计时时序：对比 cp.asarray 显式上传、裸 perf_counter 微秒级排队返回、CUDA Event 流内硬件卡点以及 cp.asnumpy 阻塞回传四段时序](/images/diagrams/gpu-agent-cupy-async-timing.svg)

顺着这张跨设备时序图往下看，要把 GPU 计时做准，必须先说清楚你到底想量哪一段的账，并用对应的同步机制把起点和终点钉死（参考 [CuPy 官方性能测量指南](https://docs.cupy.dev/en/v14.2.0/user_guide/performance.html)）。实验里我们把最常用的两种统计口径拆开实测。

### 口径一：数据已在显存内，用 `CUDA Event` 测连续计算耗时

当我们评估模型前向传播中间层算子（输入早已在显存里，输出也继续留在显存供下一层消费）时，标准做法是先跑几轮预热（把 cuBLAS 句柄初始化、内核懒加载和显存池首次分配的冷启动抖动排干净），然后在同一个 CUDA Stream 里插入起点与终点两个硬件时间戳事件，最后阻塞等待终点事件真正落定：

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

### 口径二：数据从 CPU 来、结果回 CPU 去，测单轮端到端墙钟时间

反过来，如果你的业务场景是“主程序在 CPU 内存里准备好一批特征，临时扔给 GPU 算一把矩阵乘法，立刻拿回 NumPy 数组往下走”，只报上面那个 `0.123 ms` 就是自欺欺人了。这时必须把跨 PCIe 上传和同步回传全包进墙钟计时窗里（同时把随机数据生成和事后正确性断言严格排除在计时窗外）：

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

把这两个数字放在一起对照，何时该上 GPU、怎么和 CPU 公平比快慢就再清楚不过了：

| 计时口径 | 纳入统计的物理过程 | 排除在外的干扰项 | 对应的真实工程场景 |
| --- | --- | --- | --- |
| **显存驻留 `CUDA Event` 计时** | 10 次矩阵乘法的流上执行与显存池缓冲分配 | PCIe 上传回传、首次库加载与主机侧构造 | 深度学习前向/反向传播、长链路显存驻留流水线 |
| **含传输端到端墙钟计时** | `Host → Device` 拷贝 + 算子执行 + `Device → Host` 同步回传 | 主机随机数生成、`assert_allclose` 校验 | CPU 主流程里临时卸载单个独立算子 |

以后看到任何“GPU 加速了 XX 倍”的报告，第一眼先核查两边是不是站在同一条起跑线上：相同的 `dtype`、相同的数据初始位置，以及同样等到计算真正落地的同步终点。

---

## 五、下一步：从单算子验证走向访存带宽与算子融合

跑通这组最小实验，等于在[《GPU 与大模型微调学习路线》]({{< relref "2026-08-15-gpu-to-llm-finetuning-roadmap.md" >}})的纯 NumPy 手写底座和上层深度学习框架之间，亲手搭起了一座看得见摸得着的桥。

顺着这份 Notebook 往下深挖，接下来最值得动手验证三个问题（每次只改一个自变量）：

1. **扫一遍矩阵规模拐点**：固定上述两种计时口径，把矩阵边长从 `32` 一路扫到 `8192`，亲眼看看在哪个临界规模以下，PCIe 传输与内核发射延迟会彻底吃掉 GPU 的算力优势；
2. **对比“反复搬运”与“显存常驻”**：测试把十步连续变换拆成十次 `asarray → 计算 → asnumpy` 往返，和一次上传后在显存里连算十步再拷回的耗时差距；
3. **体验算子融合（Kernel Fusion）**：哪怕数据全在显存里，连写五个逐元素算子也会触发五次显存读写往返；对比 `cp.ElementwiseKernel` 或自定义融合核函数把多次显存读写压成一次寄存器内计算后的带宽收益。

更有趣的是，把这次微观的百行脚本实验和前几天刚折腾完的[《Strata 本地分层推理实践》]({{< relref "2026-10-22-strata-windows-tiered-inference.md" >}})放在一起看，底层的物理法则竟然完全相通：CuPy 入门脚本在操心 `1024×1024` 矩阵跨 PCIe 搬运划不划算，而 117B 混合专家推理引擎同样在实测完 `7.2 GB/s` 的 OCuLink 带宽后，决定把八成热专家锁在显存、把未命中的冷专家留给 CPU 原地算而不是跨总线硬搬。不管系统规模放大多少倍，**搞清楚数据此刻躺在哪块物理介质上、跨界搬运要付多少买路钱、异步流水线在哪里同步等待**，永远是写好高性能 GPU 代码的第一课。

---

> **本机复查环境与基线说明**：本文实测结果复核于 **2026-10-28**。硬件与环境为 Windows WSL Ubuntu + NVIDIA RTX 5070 Ti（可见显存 `16,303 MiB`，宿主机驱动 `610.88`），WSL 本地工具链 `/usr/local/cuda` 为 CUDA `13.3`（`nvcc 13.3.73`），Python 环境为 `3.12.3`（`numpy 2.5.0`、`pandas 3.0.3`、`jupyterlab 4.6.1`、`cupy-cuda13x 14.2.0`）。原始学习记录位于本地仓库 `python/projects/py-numpy-pandas` 基线提交 `42f8853`。
