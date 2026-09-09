#!/usr/bin/env python3
"""Produce the P0/P1 acceptance report from actual run artifacts."""

import sys as _sys
_sys.dont_write_bytecode = True
from experiment_paths import OUTPUT_ROOT
import json
import pathlib
import shutil

ROOT=pathlib.Path(__file__).resolve().parents[1]


def main():
    runs=OUTPUT_ROOT/"runs/p01"
    reports=OUTPUT_ROOT/"reports"
    reports.mkdir(exist_ok=True)
    normal=json.loads((runs/"acceptance/summary.json").read_text())
    sanitizer=json.loads((runs/"sanitize/summary.json").read_text())
    env=json.loads((runs/"environment.json").read_text())
    manifest=json.loads((runs/"run_manifest.json").read_text())
    overhead=normal["overhead"]
    core_ok=normal["all_correctness_passed"] and sanitizer["all_correctness_passed"]
    assert core_ok, "cannot issue a passing report for failed correctness checks"
    baseline_ok=env["production_diff"]==""
    assert baseline_ok, "production TENT differs from declared baseline"
    passed=overhead["target_met"]
    median=overhead["median_relative"]*100
    low,high=[x*100 for x in overhead["median_ci95"]]
    status="P0/P1 在本报告声明的单 Worker、真实 DeviceSelector CPU 仿真范围内通过验收。" if passed else "P0 与 P1 正确性验收通过；观测开销未达到约 1% 的数值目标，P1 保留此未通过项。"
    lines=["# TENT 多轨 RDMA：P0/P1 验收报告", "", "> 日期：2026-09-08。来源：A10 服务器实际构建与测试结果；不是实验 A 的性能结论。", "", "## 1. 验收结论", "", status, "",
        "- 分支：`experiment/rdma-multirail-baseline`。",
        "- 生产基线：`1c65ced88e509440e31ea88a76594660461d0eab`。",
        "- 仓库：`/root/mooncake`；交付目录：`tent-simulator/`。",
        f"- Release：{normal['check_count']}/{normal['check_count']} 检查通过，运行 {normal['runs']} 个独立测试子进程。",
        f"- ASan/UBSan：{sanitizer['check_count']}/{sanitizer['check_count']} 检查通过；启用内存泄漏检测与遇错退出。",
        f"- 观测开销配对中位数：**{median:.2f}%**；中位数 bootstrap 95% 区间 **[{low:.2f}%, {high:.2f}%]**。",
        "- 生产 TENT 相对基线无改动；虚拟时钟和只读观测只进入实验生成文件。",
        "", "## 2. P0：环境与版本冻结", "",
        "环境快照已保存 GPU/NIC/NUMA、驱动、编译器、CPU 与 GPU 占用、Git 状态及源码哈希。未执行网卡断链、GPU 压测或系统参数修改。",
        "", "```text", env["commands"]["gpu_state"].get("stdout","未获取").strip(),
        env["commands"]["rdma_link"].get("stdout","未获取").strip(), "```", "",
        "RDMA sysfs 亲和性：", "", "| 设备 | NUMA |", "|---|---|",
    ]
    for name,row in env["rdma_sysfs"].items():
        lines.append(f"| {name} | {row['numa']} |")
    lines += ["", "上述设备信息不证明双 Rail 可叠加容量或 GPU RDMA 可达性。P1 使用每 Rail 12.5 GB/s 的合成容量；默认 fixture 关闭 QoS，全部 NIC 位于同一亲和层级，不能把 smoke 当作原始 B0 生产性能。",
        "", "## 3. P1：实现与证据", "", "| 项目 | 已完成内容 |", "|---|---|",
        "| 真实算法 | 从本分支编译 quota.cpp、Topology、Status、GDR 依赖，未抄写算法 |",
        "| 对照构建 | native 原始源码、clock 仅虚拟时钟、observed 增加只读回调 |",
        "| 一致性 | 5 个种子比较 native/clock/observed 的全部决策与完成摘要、请求时延；QoS 开启时另比较 clock/observed |",
        "| 模型 | 独立 Rail FIFO 服务、软件等待队列、QP credit、CQ poll 合批、开环到达 |",
        "| 观测 | 真实候选分数/权重、固定参考权重、分配/post/完成、实际与记账字节、原始速率、EWMA/在途量 |",
        "| 完整性 | 整请求采样、有界缓冲、溢出可见；不完整事件拒绝用于精确窗口分析 |",
        "| 指标 | 固定窗口分配/完成份额、利用率/Jain、权重与份额 TV、请求 P99（窗内不足100请求留空） |",
        "| 重现材料 | 配置、种子、构建命令、SHA256、source_snapshot、原始日志、机器可读摘要 |",
        "", "生产逐片/批量分配中的 ceil 记账行为保持不变；释放使用真实接口返回的 charged_bytes，有效流量用实际 payload。未在建立基线时顺带修复算法。",
        "", "## 4. 正确性与模型校验", "", "| 检查 | 结果 |", "|---|---|",
    ]
    for check in normal["checks"]:
        lines.append(f"| `{check['name']}` | {'PASS' if check['passed'] else 'FAIL'} |")
    lines += ["", "单 Rail 无排队的解析用例：64 KiB、12.5 GB/s、1 μs 基础延迟、1 ns poll 粒度，期望完成时延 `ceil(65536/12.5e9*1e9)+1000 = 6243 ns`，P50/P99 与解析值一致。",
        "", "16片取整案例使用真实分配函数验证 49%/51% -> 7/9；第100次多片调用进入 8/8 探测。尾片、错误释放、不可用/恢复、换路记账、QP credit 和事件先后均有独立用例。",
        "", "Smoke 仅检查工具完整性：", "", "```json", json.dumps(normal["smoke"],indent=2), "```",
        "", "这里的 P99 为合成模型输出，不代表 A10 网络 P99，也不构成现有算法已经发生震荡的证据。",
        "", "## 5. 观测开销验收", "",
        f"最终运行固定 CPU {normal['cpu']}，每组 300000 请求、每请求 16 Slice；详细事件按每 16384 个请求采样一次。先预热，再随机交错进行 9 次配对，所有配对的决策/完成摘要完全一致。",
        "", "| 配对 | 关闭观测（秒） | 开启观测（秒） | 相对变化 |", "|---|---:|---:|---:|",
    ]
    for i,pair in enumerate(overhead["pairs"],1):
        lines.append(f"| {i} | {pair['off_seconds']:.6f} | {pair['on_seconds']:.6f} | {pair['relative']*100:.2f}% |")
    lines += ["", f"中位数 {median:.2f}%，95% 区间 [{low:.2f}%, {high:.2f}%]；约1%中位数目标：{'满足' if passed else '未满足'}。区间上界是否超过1%需与中位数判据分开理解，不能宣称所有运行都小于1%。",
        "", "前三轮分别约5.25%（每4096请求）、1.31%（每8192请求）、3.39%（每16384请求，加入参考权重后）。最终定长记录与第三轮保持相同采样配置，降低热路径JSON构造成本。原始运行均保留，不将采样率变化混为算法收益。",
        "", "计时仅覆盖仿真核心，不含最后 JSON 序列化和落盘。全量 trace 用于虚拟时间归因；低比例 trace 用于成本测量，不能直接计算精确流量 TV。真实 RDMA 的吞吐/P99 观测开销尚未验证。",
        "", "## 6. 未覆盖项与下一阶段入口", "",
        "- 未运行实验 A 的稳态负载矩阵，未声称复现或量化了真实网络震荡。",
        "- 当前从真实 DeviceSelector 接口输入显式切片；未接入 RdmaTransport 完整切片/阈值和 Worker 的逐片路径。",
        "- 虚拟时钟/确定性验收仅覆盖单 Worker。共享瓶颈、多进程、多 QP 和远端拥塞模型未实现。",
        "- deadline_exceeded 仅计数；真实取消、无 CQE、晚到 CQE、QP flush 和永久挂起防护尚未通过本测试验证。",
        "- 未传输真实载荷，未验证 GPU 注册、eRDMA 同机支持、跨节点链路和 DMA 数据完整性。",
        "- 服务器未提供 pre-commit/clang-format 可执行文件；已做构建、Python语法、文件格式及 git diff 检查。",
        "", "可以开始 P2 的单 Worker、显式16片聚合接口仿真基线；接入真实 transport 或混合请求之前，先补足对应切片/候选口径。实机性能结论仍需单独校准和跨节点测试。",
        "", "## 7. 重跑与文件", "", "见 `../instrumentation/README.md` 的完整命令。", "",
        "- `../scripts/build.py`：隔离构建；`../tests/acceptance.py`：验收。",
        "- `../configs/`：基线契约、显式 smoke 参数。",
        "- `../scripts/analyze.py`：全量事件窗口 JSON/CSV 分析。",
        "- `../runs/p01/environment.json`、`run_manifest.json`、`source_snapshot/`：环境与来源。",
        "- `../runs/p01/acceptance/`、`sanitize/`：每次配置、原始结果与 stderr。",
        "- `p01-acceptance.json`、`p01-sanitizer.json`：本报告对应的机器可读摘要。",
        "- Git 忽略 `runs/`、`build/` 和 Python 缓存；测试源码与配置在实验分支提交。",
        "", f"验收运行起始 Git HEAD：`{manifest['head']}`；运行时未提交文件已用 source_snapshot 与 SHA256 固定，最终提交号以交付消息及该分支 Git 日志为准。", "",
    ]
    (reports/"p01-acceptance.md").write_text("\n".join(line.rstrip() for line in "\n".join(lines).splitlines()) + "\n")
    shutil.copyfile(runs/"acceptance/summary.json",reports/"p01-acceptance.json")
    shutil.copyfile(runs/"sanitize/summary.json",reports/"p01-sanitizer.json")
    print("REPORT_WRITTEN",reports/"p01-acceptance.md")


if __name__=="__main__":
    main()
