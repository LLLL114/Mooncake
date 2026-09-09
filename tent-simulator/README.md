# TENT 多轨 RDMA 仿真与测试

源码位于 /root/mooncake/tent-simulator/。
默认生成目录是 /root/mooncake-tent-multirdma-output/，不在 Git 仓库中。

## 目录

~~text
tent-simulator/
  algorithms/       候选算法源码
  simulator/        事件模型、RDMA 驱动和插桩
  scripts/          构建、运行、分析与统一路径定义
  tests/            功能、算法及输出路径测试
  acceptance/       指标基准的代码与实验方案
  configs/          手写配置样例、基线合同和依赖版本
  instrumentation/  实验方法说明
  EXPERIMENT_PLAN.md 原实验设计

/root/mooncake-tent-multirdma-output/
  build/            生成代码、二进制、依赖及构建清单
  runs/             运行配置、原始数据、轨迹和日志
  reports/          A–F 与候选算法的报告、CSV、图表
  acceptance/       指标基准报告、CSV、证据包、校验清单
  migration/        原暂存区备份、原始源码快照和迁移核验
~~

configs 中的 JSON 是手写测试输入，不是运行结果。生成的数据、报告、缓存和构建产物均放在输出目录。

## 输出目录和构建

路径由 scripts/experiment_paths.py 管理。可为一次独立复测选择新的外部输出目录，避免覆盖历史数据：

~~bash
export MOONCAKE_TENT_OUTPUT=/root/mooncake-tent-multirdma-output/retest-01
~~

如果输出目录落在 Mooncake 仓库内部（包括经符号链接指向仓库），程序会拒绝启动。Python 入口关闭源码目录的字节码缓存；建议使用 python3 -B。

从 Mooncake 仓库根目录按以下顺序构建：

~~bash
python3 -B tent-simulator/tests/test_output_paths.py
python3 -B tent-simulator/scripts/build_experiment_d.py
python3 -B tent-simulator/scripts/build_experiment_ef.py
python3 -B tent-simulator/scripts/build_real_probe.py
python3 -B tent-simulator/scripts/build_candidates.py
python3 -B tent-simulator/acceptance/build_baseline.py
python3 -B tent-simulator/tests/acceptance.py
python3 -B tent-simulator/acceptance/test_metrics.py
python3 -B tent-simulator/acceptance/run_baseline.py --stage validate
~~

依赖沿用原环境：C++17、RDMA/NUMA/glog 开发库、NumPy；绘图需要 Matplotlib，GPU 探测需要 CUDA。绘图依赖版本见 configs/experiment-a-plot-requirements.txt。使用新的输出根目录时，需要为其 build/plot-deps 准备依赖，或使用已经安装依赖的 Python。

validate 阶段包含一次真实 RDMA 冒烟测试，需要匹配驱动中的网卡地址。路径测试和纯算法测试不需要 RDMA 连通。

## 运行与报告

~~bash
python3 -B tent-simulator/acceptance/run_baseline.py --stage real
python3 -B tent-simulator/acceptance/run_model.py
python3 -B tent-simulator/acceptance/report_baseline.py
~~

实机性能测试与模型负载应串行运行。脚本拒绝覆盖已有同名结果。报告生成需要相应的数据；手工审核的 baseline-findings.md 属于输出材料，保存在外部 acceptance 目录，需要与该批数据对应。

日志重定向也应使用外部目录：

~~bash
mkdir -p /root/mooncake-tent-multirdma-output/logs
python3 -B tent-simulator/tests/test_output_paths.py \
  > /root/mooncake-tent-multirdma-output/logs/path-check.log 2>&1
~~

## 历史数据

历史报告、清单和数据按原字节保存，未为迁移改写实验数值或哈希。旧清单中的绝对路径是当时的来源信息，可通过 migration 下的映射查找新位置。新构建和新运行会记录新路径；混合源码与产物的清单用 @output/ 标识外部文件。

部分历史审计固定了旧提交的脚手架哈希。重排后的脚本不等于原字节快照，不能冒充旧源码通过检查；原始源码保存在 migration/source-before。缺失的历史原始数据也不能由报告替代。

repair_concurrent_control.py 和 repair_gate_control.py 是历史修复工具，常规新测试不需要重新执行。本目录尚未替换生产 TENT 默认算法；直接 RDMA 驱动不代表完整 Worker/endpoint 测试。
