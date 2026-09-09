# 实验 A 执行与解释约定

## 范围

在 A10 的 `experiment/rdma-multirail-baseline` 分支、当前实验目录运行。生产 TENT 不修改；从源码提取 CPU WRITE 纯切片计算块后编译，并调用真实 DeviceSelector 的聚合或逐片接口。不是完整 transport/Worker/endpoint 或 GPU/eRDMA 数据路径测试。

新增窗口是只读观察，不推进生产随机数。输出同时包含生产 selector 的 inflight 与独立模型的 queue_bytes；RR 不维护前者，不能据此说 RR 没有排队。所有吞吐使用实际 payload 完成字节，不包含排空阶段。

## 重跑

以下命令在仓库根目录执行。重跑前归档 `/root/mooncake-tent-multirdma-output/runs/experiment-a/`，避免覆盖原始编号结果。默认矩阵为 320 组、单 CPU 8 顺序执行，每个子进程最多运行 90 秒。

```bash
python3 tent-simulator/scripts/capture_environment.py \
  --output-dir /root/mooncake-tent-multirdma-output/runs/experiment-a/environment
python3 tent-simulator/scripts/build.py
python3 tent-simulator/scripts/run_experiment_a.py
python3 tent-simulator/scripts/focus_experiment_a.py
python3 tent-simulator/tests/acceptance.py --skip-overhead \
  --output-dir /root/mooncake-tent-multirdma-output/runs/experiment-a/p01-regression
python3 tent-simulator/scripts/build.py --sanitize
python3 tent-simulator/scripts/run_experiment_a.py --sanitize --validate-only
python3 -m pip install --no-cache-dir \
  --target /root/mooncake-tent-multirdma-output/build/plot-deps \
  -r tent-simulator/configs/experiment-a-plot-requirements.txt
python3 tent-simulator/scripts/report_experiment_a.py
```

仅检查切片、模型和观测一致性时，用 `run_experiment_a.py --validate-only`。P0/P1 输出通过新 `--output-dir` 保存到独立位置，不覆盖旧验收材料。

## 参数与指标

- 2种拓扑：等亲和；双NUMA亲和（cpu内存NUMA0，本地NIC NUMA0，远端层级NIC NUMA1）。跨NUMA容量/延迟未实测标定，只研究配置中的层级惩罚。
- 请求：64KiB、512KiB、1MiB、16MiB，固定CPU WRITE，HIGH优先级。
- 负载：两条合成100G Rail总容量的20/60/90/110%。每组20000请求，4000请求预热；每次到达间隔按该负载固定。
- smart参数与原算法一致：alpha旧值权重0.01、NUMA惩罚1/5/10、jitter 1e-9、QoS开启、每100次多片分配探测。
- 5个seed控制PRNG，不冒充5次独立硬件测试。配对使用相同到达序列、容量、时钟及队列参数，策略顺序打乱。
- 均衡看长期完成份额与模型队列，切换看首片路径和真实argmin，二者都不是已post请求迁移。
- 流量统计窗至少约20次到达；快照固定1ms，1MiB另用10μs复查。不同请求大小的流量窗不同，不能直接比较跨大小的CV。
- P99按测量段到达请求统计，包含停止注入后完成的请求；波动采用每200请求群组，计量段共80组。超出deadline仅计数，不实现生产取消。
- 110%输入必然过载；双NUMA轮询只用本地NIC，超过总容量50%便超过其自身单轨容量。排队单调增长与持续控制震荡分开报告。

## 文件与来源

`/root/mooncake-tent-multirdma-output/runs/experiment-a/run/manifest.json` 固定真实源码、构建命令、二进制和实验驱动的SHA256。逐组config/result/stderr、320组CSV、fine快照和focus压缩trace保留在该目录。

`/root/mooncake-tent-multirdma-output/reports/experiment-a-report.md` 是结论，`experiment-a-metrics.csv` 保留全矩阵，`experiment-a-summary.json` 保留各条件中位数及seed范围。两张SVG使用标准Matplotlib生成，图示为仿真而非A10网络测量。

更改采样观察方式后已检查轨迹不变；本阶段未重新验收真实数据路径的观测开销，也不将P0/P1稀疏观测开销当作本阶段全量窗口的开销。
