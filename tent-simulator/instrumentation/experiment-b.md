# 实验 B：执行约定

## 目的与范围

先验证真实 DeviceSelector 中 EWMA 的独立噪声过滤和阶跃响应，再仅改变旧值系数 alpha，测试1MiB闭环的权重、整数分配、吞吐/P99与动态响应。测试值为0.01、0.5、0.9、0.99、0.999。没有修改生产算法默认值或引入新分配策略。

## 运行

依赖实验A的原始基线文件；新环境请先按实验A约定执行A。

以下命令在仓库根目录执行，所有原始材料保存在实验分支的 `/root/mooncake-tent-multirdma-output/runs/experiment-b/`。先归档已有 `run/`、`sanitize/`，程序拒绝覆盖已有逐组配置。alpha含小数点的名称使用“追加后缀”，不能使用 `Path.with_suffix` 替换小数点之后的内容。

```bash
python3 tent-simulator/scripts/build.py
python3 tent-simulator/scripts/run_experiment_b.py
python3 tent-simulator/scripts/focus_experiment_b.py
python3 tent-simulator/tests/acceptance.py --skip-overhead \
  --output-dir /root/mooncake-tent-multirdma-output/runs/experiment-b/p01-regression
python3 tent-simulator/scripts/build.py --sanitize
python3 tent-simulator/scripts/run_experiment_b.py --sanitize --validate-only
python3 -m pip install --no-cache-dir \
  --target /root/mooncake-tent-multirdma-output/build/plot-deps \
  -r tent-simulator/configs/experiment-a-plot-requirements.txt
python3 tent-simulator/scripts/audit_experiment_b.py
python3 tent-simulator/scripts/report_experiment_b.py
```

`--validate-only` 只跑服务积分、轨迹一致性、EWMA阶跃与材料唯一性验证。报告依赖完整主实验、focus、sanitizer和P0/P1回归结果。

## 设计

- 独立估计器：25组；每组10000样本预热、1000000个4–6 GB/s均匀伪随机样本。以真实 charge/release/getAggregateEwmaBandwidth 接口喂入与读取，未复制一套估计器。
- 阶跃：10→5与5→10 GB/s；95%响应以误差不超过阶跃幅度5%定义，单位是完成事件样本数，不是毫秒。
- 稳态：75组，等亲和双100G合成Rail、1MiB/16 Slice、20/60/90%负载、5个alpha、5个seed；每组20000请求，4000请求预热。
- 慢轨目标标定：50组，在可持续的20/60%负载下将Rail0容量固定为6.25 GB/s；为各alpha建立其实际稳态流量份额目标。
- 动态：225组，外加75组匹配长度的无扰动对照。Rail0从12.5降至6.25 GB/s：1ms脉冲、持续降速、100ms后恢复。观察事件后300ms。
- 外部容量按绝对时间分段积分，已post或正在传输的Slice跨越变化点时也受影响；调度器只看到后续完成事件，标称带宽、初始EWMA和上下限不随模拟降速重置。

## 指标解释

连续权重变化与实际每请求分配变化均记录0.5×L1距离；固定1MiB聚合请求中每请求一次决策，可比较二者。分配直方图、8:8占比和首片切换率用于识别取整残留，首片切换不是已post请求迁移。

平均份额收敛使用1ms完整窗、同alpha独立稳态标定目标±5个百分点、连续保持10窗；报告首次合格窗结束时刻。它不表示逐请求振荡消失，也不表示队列清空。未达到条件保留为null，不填0。

队列恢复另以“相对匹配无扰动对照额外积压≤1MiB、保持10个1ms窗”判定。90%输入在半速单轨阶段需求180Gb/s高于150Gb/s容量，不能要求有限稳态收敛；恢复后才可观察清除积压。

短脉冲使用相对同一请求无扰动时延的最大增量，覆盖事件前仍可能在途的请求和事件后20ms。短时间内样本数有限，不将峰值冒称可靠P99。

## 来源与限制

每组配置、结果、stderr均有独立文件；`case-manifest.json` 保存逐组配置/结果SHA256，`summary.json` 保存核心源码、二进制与构建信息。首次文件命名缺陷产生的旧目录保留用于核对，不作为最终原始数据来源；以完整重跑的 `run/` 为准。

新增评分分解仅是当次状态的只读影子计算，不是替换队列项/带宽项的消融重跑。当前结果限于合成CPU模型，不证明真实网络、GPU注册、QP故障或永久挂起保护正确。无容量为0的故障事件，deadline只计数，不实现取消。
