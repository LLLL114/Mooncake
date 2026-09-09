# 实验 C：采样、更新和 CQ 时间尺度

## 范围与规则

实验仅在现有实验分支内运行真实 DeviceSelector 的 CPU 仿真。生产文件不修改。新 controlled 二进制在生成的 quota.cpp 中增加学习门控：先执行原有在途释放，再决定是否学习，保留原始 EWMA 公式和裁剪。native/clock/observed 不启用该干预。

- 原策略：每个成功完成事件更新带宽EWMA，旧值 alpha=0.01。派生权重仍随最新在途量逐请求重算，门控不是周期性冻结或发布权重。
- 门控：每个NIC从上次被接受更新起，经过最小间隔后接受首个成功完成样本；间隔为0.1/1/5/10ms。它不是无条件触发的墙钟定时器，空闲期间没有新样本就不更新。
- 固定alpha组维持0.01。固定时间常数组使用 exp(-实际经过时间/tau)，tau为20/50/100ms；同时间戳的重复样本不会产生额外时间衰减。
- head是首个符合门槛的样本；tail等待同NIC同CQ时间戳组的最后一个样本。完成消费仍然FIFO，不倒序CQ，不变更服务时间。
- 门控改变被选样本分布；实际更新间隔、原始/选中样本均值与批量大小必须一起记录，不能把所有效果归因于更新次数。

## 重跑

依赖实验A的原始基线文件。先归档已有 /root/mooncake-tent-multirdma-output/runs/experiment-c/run 和 sanitize，程序拒绝覆盖逐组配置。以下命令在仓库根目录执行。

```bash
python3 tent-simulator/scripts/capture_environment.py \
  --output-dir /root/mooncake-tent-multirdma-output/runs/experiment-c/environment
python3 tent-simulator/scripts/build.py
python3 tent-simulator/scripts/run_experiment_c.py
python3 tent-simulator/tests/acceptance.py --skip-overhead \
  --output-dir /root/mooncake-tent-multirdma-output/runs/experiment-c/p01-regression
python3 tent-simulator/scripts/build.py --sanitize
python3 tent-simulator/scripts/run_experiment_c.py --sanitize --validate-only
python3 tent-simulator/scripts/audit_experiment_c.py
python3 tent-simulator/scripts/report_experiment_c.py
```

图表使用隔离的 /root/mooncake-tent-multirdma-output/build/plot-deps，若不存在，按 experiment-a-plot-requirements.txt 安装到该目录。只做验证可使用 --validate-only。

## 矩阵

- 300组主闭环：5个门控间隔（含0）、固定alpha或3个tau、20/60/90%负载、5个seed；CQ周期1μs。
- 135组CQ对照：CQ周期10/50/100μs，原策略、1ms固定alpha、1ms/tau50ms三种策略，3个负载和5个seed。
- 90组tail诊断：CQ周期10/50/100μs，1ms固定alpha或tau50ms，3个负载和5个seed。
- 所有闭环为等亲和双100G合成Rail、1MiB CPU WRITE、16×64KiB、单Worker。统一预热至少1s，测量至少1s，覆盖最大tau的10倍预热。
- 独立估计器：改变成功样本事件周期、门控间隔和tau，使用恒定阶跃检验95%响应。另用同时间戳2/4/6/8 GB/s固定样本验证首末选样偏差；该回放不宣称来自真实CQ。
- 仅观测对照：相同3种策略，观测周期10/100μs、1/10ms，共12组。比较公共区间[1.01s,1.99s)，轨迹摘要必须不变。

## 指标边界

每请求权重/分配的0.5×L1平均变化用于比较策略。固定参考权重快照的总变差会随观测分辨率改变，跨分辨率不得当作策略改善。

吞吐按有效完成字节统计；P99按测量段到达请求统计，含排空后完成者。CQ周期变大直接增加完成可见延迟，并改变在途计数和带宽样本，不能将P99变化全部归因于EWMA。

固定tau在恒定输入下统一时间响应，不保证复杂完成样本下策略完全等价；选样分布、裁剪和队列反馈仍存在。所有结果限于当前合成模型，未测试真实RDMA或多Worker竞争。
