# 实验 D：离散分配、批内状态与周期探测

## 相对原计划的调整（运行前设计）

保持原 D 的阈值、Slice 数、候选数、尾片及第 99/100/101 次探测检查。
A 已复现恒定负载下逐请求往返；B 显示小权重差仍可对应 7/9 与 9/7，且高负载受在途量影响；C 显示学习周期不能代表决策周期，观测分辨率会影响 TV。因此增加：

1. 静态输入—输出映射：真实 allocate，固定候选带宽、无学习、无服务反馈，记录权重、片数和物理字节份额。独立验证取整跳变。
2. 闭环单因素对照：原算法、仅把余数给最大小数部分（每候选至多一片）、仅关闭每百次探测。评分、EWMA、floor、记账和传输模型保留。
3. 冻结带宽诊断：alpha=1，初值100G；仍释放在途配额。对照同样冻结的原算法与余数/探测干预。它只能说明此合成场景在无带宽学习时的行为，不是生产配置建议。
4. 批内快照诊断：增加 frozen_greedy/refreshed_greedy 配对。两者都是逐片 argmin、同样的 ceil 记账、同样的每百次 RR 探测、jitter=0；唯一差别是后续片是否重建候选。两者之间隔离批内预占可见性。**原算法与 refreshed_greedy 之间还改变了比例分配规则，不能将差异全部归因于快照。** 默认 jitter 的 refreshed_greedy 额外组仅是综合诊断。

所有改动在新 allocation 可执行文件中，生产 TENT、旧 driver.cpp、旧 build.py 不修改。不是替代算法交付；不包含真实 RDMA、GPU、Worker 并发或故障恢复实验。

## 矩阵和假设

- 静态：2/4/8 候选、2–64片（部分是直接 API 夹具，非生产切片范围）、接近均分的正负扰动；jitter=0。近均分输入不变时不能把静态整数跳跃称为时间震荡。
- 几何：实际 CPU WRITE 拆分函数，在15/16片阈值、25%尾片合并边界、32片上限附近列出长度、片数、块大小、零字节片数与实际记账。若生成零字节片，明确列出并排除于性能主矩阵，不擅自修复生产切片。
- 主闭环：等亲和双100G、1MiB、20/60/90%，四种诊断模式、五seed。
- 冻结带宽：同三负载；原算法、余数、无探测，五seed。
- 快照：60/90%，原算法/两种greedy，jitter=0，五seed。
- 大小：15片、阈值以上带尾片、16片尾片、17/32片和16MiB，60/90%，原算法/余数/无探测，五seed。固定总offered fraction，因大小变化导致事件率变化，所以只把同大小内配对差异用于归因。
- 多候选：4个模拟100G Rail、1/2MiB、60/90%，三种模式、五seed；这不代表A10有4条实测链路。
- 探测：固定/混合大小、把大请求对齐或错开探测、插入单片请求；1000请求的静态无学习序列。区分所有请求、multi调用数、探测请求字节占比；整次探测请求字节不是“相比基线新增的流量”。不将静态混合序列的计数结果作为混合流量P99证据。

预期：近等权重的小幅变化可能被floor和余数放大；最大余数可移动或减小某些跳变，但整数分配仍不连续，特别是奇数片。关闭探测可移除100调用周期的小扰动，但未必移除非探测的逐请求交替。冻结带宽后如仍往返，则带宽学习并非该场景往返的必要条件。逐片预占可见性可能降低批内集中，但代价和适应性尚须后续实机/动态验证。

## 固定口径

每闭环20,000请求、4,000预热，开环按字节/总容量/负载设置到达间隔，CQ=1μs、基础延迟1μs、每Rail QP credit=128；seed=1/2/19/12345/4294967295。复用P1/C服务模型。

主指标为逐请求首轮候选权重0.5×L1平均步长和逐请求**物理字节**份额0.5×L1平均步长。逐片刷新会产生更多候选构建，不能用所有候选决策的TV直接与批量算法比较。额外保留统一1ms参考权重窗口和原有全决策指标。吞吐CV以至少20请求的窗口汇总；P99波动按200请求群组，固定大小内对照。长期均衡采用已知合成容量归一化Jain指数。首片路径切换不是Slice迁移；实际迁移/重试未建模。

报告五seed中位数、配对差值和运行级bootstrap 95%区间；确定性模型导致退化区间不代表真实网络不确定性。吞吐按完整测量窗口，延迟按预热后到达请求并包含排空阶段。deadline只计数、不取消请求；有限正容量模型完成排空不证明生产永不挂起。

## 重跑

在实验分支仓库根目录运行，先归档已存在的D运行目录（脚本拒绝覆盖）：

```bash
python3 tent-simulator/scripts/capture_environment.py --output-dir /root/mooncake-tent-multirdma-output/runs/experiment-d/environment
python3 tent-simulator/scripts/build_experiment_d.py
python3 tent-simulator/scripts/run_experiment_d.py
python3 tent-simulator/scripts/build_experiment_d.py --sanitize
python3 tent-simulator/scripts/run_experiment_d.py --sanitize
python3 tent-simulator/tests/acceptance.py --skip-overhead --output-dir /root/mooncake-tent-multirdma-output/runs/experiment-d/p01-regression
python3 tent-simulator/scripts/audit_experiment_d.py
python3 tent-simulator/scripts/report_experiment_d.py
```

所有D原始配置/结果、stderr、case-manifest在runs/experiment-d。A原始数据存在时加验15组digest一致性；当前未找到旧原始JSON，材料审计改为核对已提交A汇总CSV的15组digest与P99。另有5组旧observed与D全关闭的自包含一致性检查。编译依赖沿用P1；绘图使用A已固定版本的matplotlib（见configs/experiment-a-plot-requirements.txt）。报告脚本自行加载build/plot-deps；无依赖时应先按A文档安装至该实验目录。
