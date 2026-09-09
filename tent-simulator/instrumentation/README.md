# P0/P1 构建与观测契约

## 执行入口

从 A10 仓库根目录执行，所有路径属于当前实验分支：

```bash
python3 tent-simulator/scripts/capture_environment.py
python3 tent-simulator/scripts/build.py
python3 tent-simulator/tests/acceptance.py
python3 tent-simulator/scripts/build.py --sanitize
python3 tent-simulator/tests/acceptance.py --sanitize --skip-overhead
```

`/root/mooncake-tent-multirdma-output/runs/p01/acceptance/summary.json` 是自动验收摘要。运行超时为每个子进程 90 秒，单 CPU 绑定仅对子进程生效。重跑前归档已有 `/root/mooncake-tent-multirdma-output/runs/p01`，否则同名编号文件会覆盖。

手动运行 smoke 和完整事件分析：

```bash
/root/mooncake-tent-multirdma-output/build/release/observed \
  tent-simulator/configs/p01-smoke.json \
  /root/mooncake-tent-multirdma-output/runs/p01/smoke.json
python3 tent-simulator/scripts/analyze.py \
  /root/mooncake-tent-multirdma-output/runs/p01/smoke.json \
  /root/mooncake-tent-multirdma-output/runs/p01/analysis --window-ns 1000000
```

## 真实代码与构建隔离

不修改生产 TENT 文件。`build.py` 读取当前仓库的 `quota.cpp`，产生三个实验可执行程序：

- `native`：原样编译 `quota.cpp`，使用原始时钟。
- `clock`：只将候选优先级轮转所用时钟替换为虚拟时间，并添加实验声明头。
- `observed`：在 clock 基础上，候选排序完成后调用只读回调。

两个插入锚点必须各唯一出现一次，否则构建失败，不静默适应其他版本。全部实际改动写入 `/root/mooncake-tent-multirdma-output/build/release/observer.patch`；构建命令、编译器、真实来源 SHA256 写入 `manifest.json`。拓扑、Status、GDR 可达性依赖也从当前仓库编译，未使用预装 wheel 或其他分支的库。无用函数由 linker garbage collection 裁剪，不伪造 RDMA/GPU backend。

随机数通过现有 `SimpleRandom` 的赋值操作初始化，继续使用生产 PRNG；每个测试启动独立进程，使线程局部轮询/百次探测计数从零开始。native 与 clock 只在 QoS 关闭时要求完整决策等价；QoS 打开时用 clock 与 observed 比较，避免拿墙钟时间与虚拟时间的不同阶段假装等价。

## 观测语义

- `candidates` 来自本次真实候选评分结果，记录 score、资格集合、归一化 p、虚拟时间和请求类别。
- allocate/post/complete 事件记录物理长度和原接口返回的 charged_bytes；释放严格采用 charged_bytes。
- 该旧版本尾片可能按 ceil 均分记账，不修正此行为；有效吞吐与守恒检查采用实际 payload 字节。
- 采样以整个请求为单位，保证同一个被采样请求的所有 Slice 事件都入缓冲；采样决定只计算一次并随 Slice 传播。
- trace 有容量上限，溢出计数公开。窗口分析只接受全量且无溢出的 trace，不能从低比例详细采样直接估算精确份额 TV。
- 事件使用预留容量的定长结构缓冲，热路径不构造 JSON，计时段结束后统一序列化。当前缓冲只用于单 Worker，不声称多线程并发安全。观测开销是仿真核心墙钟成本，明确不包含最后序列化/磁盘写入；不是实际 RDMA 的吞吐或 P99 开销。
- 聚合计数、请求时延、最终状态和轨迹摘要在开关观测两种运行中一致保留。候选权重不消费额外 PRNG、不修改选择结果。
- 另从同一只读状态计算固定 64 KiB、cpu:0、全可用 rank0 NIC 的参考权重，不调用生产候选构造、不消耗 PRNG、不推进探测计数。此参考明确排除 QoS/jitter，仅适用于当前单 Worker 合成拓扑；不能推广为通用混合业务或真实多 Worker 一致性快照。

## 模型边界

P1 从 `DeviceSelector::allocate/release` 接口驱动固定显式 Slice 分配，未执行 `RdmaTransport::submitTransferTasks` 的完整切片/阈值逻辑。默认 1 MiB/64 KiB 对应 16 片的聚合接口输入。阈值前后测试与 Worker 逐 Slice 路径属于后续实验 D 的接入项，不用近似重写冒充生产 transport。

每条模拟 Rail 为独立 FIFO 服务资源，按实际字节/容量耗时；QP credit 满时在软件队列等候，CQ poll 向上取整释放 credit。所有计量使用虚拟单调时间，独立 NIC 服务只计算一次。默认容量 12.5 GB/s 是合成参数，不是 A10 实测容量。固定基础延迟不消耗 NIC 带宽。此阶段未模型化共享瓶颈、远端接收拥塞、多 QP、多 Worker、数据载荷、provider/GPU 注册。

`deadline_exceeded` 仅计数超过期限的请求，模型仍排空全部已安排完成；不是生产取消/超时机制。故障释放、全 Rail 不可用与重路由仅检查真实 selector 接口的配额/状态，不声称覆盖 Workers 晚到 CQE、QP flush、peer restart 或真实请求永久挂起。无完成硬件故障归后续真实 transport 测试。

验收判断：正确性检查必须全部通过；约 1% 开销目标以低比例详细事件采样的配对中位数报告，同时公开置信区间。全量追踪用于仿真归因，可因观测不改变虚拟时序而保留；不能据此豁免未来实机观测开销测试。
