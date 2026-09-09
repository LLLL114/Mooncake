#!/usr/bin/env python3
"""Experiment B report: distinguish estimator smoothing from closed-loop stability."""

import sys as _sys
_sys.dont_write_bytecode = True
from experiment_paths import OUTPUT_ROOT
import csv
import json
import os
import pathlib
import statistics
import sys

ROOT=pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0,str(OUTPUT_ROOT/"build/plot-deps"))
os.environ.setdefault("MPLCONFIGDIR",str(OUTPUT_ROOT/"build/mplconfig"))
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


def main():
    run=OUTPUT_ROOT/"runs/experiment-b/run"; reports=OUTPUT_ROOT/"reports"
    data=json.loads((run/"summary.json").read_text())
    focus=json.loads((run/"focus.json").read_text())
    san=json.loads((OUTPUT_ROOT/"runs/experiment-b/sanitize/validation.json").read_text())
    regression=json.loads((OUTPUT_ROOT/"runs/experiment-b/p01-regression/summary.json").read_text())
    assert all(c["passed"] for c in data["checks"]+san) and regression["all_correctness_passed"]
    for d in data["dynamic"]:
        d["queue_recovery_ms"]=None
        if d["scenario"]=="held_drop": continue
        changed=json.loads((run/(d["name"]+".json")).read_text())
        stem=d["name"][len(d["scenario"])+1:]
        control=json.loads((run/("control_"+stem+".json")).read_text())
        lookup={w["start_ns"]:w for w in control["windows"]}
        at=d["event_ns"]+(1000000 if d["scenario"]=="pulse1ms" else 100000000)
        bins=[w for w in changed["windows"] if w["start_ns"]>=at]
        for j in range(len(bins)-9):
            if all(sum(w["queue_bytes"])<=sum(lookup[w["start_ns"]]["queue_bytes"])+1048576 for w in bins[j:j+10]):
                d["queue_recovery_ms"]=(bins[j]["start_ns"]+1000000-at)/1e6;break
    audit=json.loads((run/"material-audit.json").read_text())
    assert audit["verified_case_pairs"]==463 and all(audit["aggregate_reproduction"].values())
    refs=[x for x in audit["reference_self_checks"] if x["alpha"]==.5 and x["load"]==.2 and x["scenario"]=="drop100ms_recover"]
    reference_note='α=0.5、20%负载这一行需特别解释：5个seed的无扰动对照也无法满足相同的连续10窗门限（仅约28.14%的窗口落在目标带内）。因此“未达到”反映该策略本身的平均份额波动，不能读成降速后无法恢复或永久挂起；此条件下不能用该判据给出可辨识的恢复时间。' if len(refs)==5 and not any(x["unperturbed_reference_qualifies"] for x in refs) else ""
    alphas=data["alphas"];loads=data["loads"]
    def steady(a,l):return [r for r in data["steady"] if r["alpha"]==a and r["load"]==l]
    def dynamic(a,l,scenario):return [r for r in data["dynamic"] if r["alpha"]==a and r["load"]==l and r["scenario"]==scenario]
    def median(rows,key):return statistics.median(r[key] for r in rows)
    def settling(rows,key):
        vals=[r[key] for r in rows if r[key] is not None]
        return {"median_ms":statistics.median(vals) if vals else None,"attained":len(vals),"total":len(rows)}
    def pretty(value):
        return f"{value['median_ms']:.2f} ({value['attained']}/{value['total']})" if value['median_ms'] is not None else f"未达到 ({value['attained']}/{value['total']})"
    grouped=[];est=[];dyn=[]
    for a in alphas:
        e=[r for r in data["estimator"] if r["alpha"]==a]
        est.append({"alpha":a,"theory":e[0]["theory_ratio"],"measured":median(e,"variance_ratio"),
                    "min":min(r["variance_ratio"] for r in e),"max":max(r["variance_ratio"] for r in e),
                    "step95_samples":e[0]["up_step95_samples"],"clamped_samples":sum(r["clamped_samples"] for r in e)})
        for l in loads:
            rows=steady(a,l)
            grouped.append({"alpha":a,"load":l,**{k:median(rows,k) for k in ["goodput_gbps","p99_us","decision_weight_step","allocation_step","allocation_std","balanced_fraction","first_slice_switches_per_10k_requests"]},
                "histogram_seed1":next(r["allocation_histogram"] for r in rows if r["seed"]==1),
                "p99_range":[min(r["p99_us"] for r in rows),max(r["p99_us"] for r in rows)]})
            for scenario in ["pulse1ms","held_drop","drop100ms_recover"]:
                rows=dynamic(a,l,scenario)
                dyn.append({"alpha":a,"load":l,"scenario":scenario,
                    **{k:median(rows,k) for k in ["extra_peak_latency_us","flow_share_excursion","last_100ms_goodput_gbps","post_event_p99_us"]},
                    "down":settling(rows,"down_settle_ms"),"recovery":settling(rows,"recovery_settle_ms"),
                    "overloaded":l>.75,"queue_recovery":settling(rows,"queue_recovery_ms")})
    decomposition=[]
    for f in focus:
        rows=f["rows"]
        decomposition.append({"load":f["load"],"alpha":f["alpha"],"prefix_equal":f["prefix_equal"],
            "actual_range":[min(r["p0"] for r in rows),max(r["p0"] for r in rows)],
            "bandwidth_only_range":[min(r["zero_queue_shadow_p0"] for r in rows),max(r["zero_queue_shadow_p0"] for r in rows)],
            "queue_only_mae":statistics.mean(abs(r["p0"]-r["equal_bandwidth_shadow_p0"]) for r in rows),
            "first10":rows[:10]})
    summary={"estimator":est,"steady":grouped,"dynamic":dyn,"decomposition":decomposition,
             "validation":[len(data["checks"]),len(san),regression["check_count"]],
             "process_runs":data["process_runs"],"closed_loop_runs":data["closed_loop_runs"],
             "settling_definition":data["settling_definition"],"binary_sha256":data["binary_sha256"]}
    (reports/"experiment-b-summary.json").write_text(json.dumps(summary,indent=2)+"\n")
    for name,rows in [("steady",data["steady"]),("dynamic",data["dynamic"]),("estimator",data["estimator"])]:
        keys=[k for k,v in rows[0].items() if not isinstance(v,(dict,list))]
        with (reports/f"experiment-b-{name}.csv").open("w",newline="") as stream:
            writer=csv.DictWriter(stream,fieldnames=keys,lineterminator="\n");writer.writeheader()
            writer.writerows({k:r[k] for k in keys} for r in rows)
    plt.rcParams.update({"svg.fonttype":"none","font.size":10})
    fig,axes=plt.subplots(2,2,figsize=(10,7),layout="constrained");x=np.arange(5)
    axes[0,0].plot(x,[e["theory"] for e in est],"--",color="#687785",label="White-noise theory")
    axes[0,0].plot(x,[e["measured"] for e in est],"o",color="#007f86",label="Real estimator")
    axes[0,0].set_yscale("log");axes[0,0].set_ylabel("EWMA variance / input variance");axes[0,0].set_title("Isolated estimator");axes[0,0].legend()
    for ax,l in [(axes[0,1],.6),(axes[1,0],.9)]:
        ax.plot(x,[median(steady(a,l),"decision_weight_step") for a in alphas],"o-",label="Weight step",color="#007f86")
        ax.plot(x,[median(steady(a,l),"allocation_step") for a in alphas],"s-",label="Allocation share step",color="#d67b35")
        if l==.6: ax.set_yscale("log")
        else: ax.set_ylim(0,1)
        ax.set_title(f"Closed loop: {l*100:.0f}% load");ax.set_ylabel("Mean total-variation step");ax.legend()
    for l,color in zip(loads,["#87939e","#007f86","#d67b35"]):
        axes[1,1].plot(x,[median(steady(a,l),"p99_us") for a in alphas],"o-",label=f"{l*100:.0f}% load",color=color)
    axes[1,1].set_ylabel("Request P99 (microseconds)");axes[1,1].set_title("Performance constraint");axes[1,1].legend()
    for ax in axes.flat:
        ax.set_xticks(x,[str(a) for a in alphas]);ax.set_xlabel("Old-value alpha (tested settings)");ax.spines[['top','right']].set_visible(False)
    fig.suptitle("Experiment B: smoothing is not sufficient for scheduling stability\nSynthetic CPU model, real DeviceSelector, 1 MiB requests",fontsize=11)
    path=reports/"experiment-b-overview.svg";fig.savefig(path);plt.close(fig)
    path.write_text("\n".join(line.rstrip() for line in path.read_text().splitlines())+"\n")
    lines=["# 实验 B：EWMA 灵敏度与调度稳定性报告","","> 2026-09-08；A10 上执行的真实 DeviceSelector CPU 仿真。真实 RDMA 数据路径尚未验证。","",
        "## 1. 结论","",
        "在本次测试的五个取值中，增强 EWMA 平滑抑制了估计器噪声，但未能同时消除各负载下的调度震荡。低/中负载时，较大的alpha大幅减小连续权重变化，整数Slice分配仍近乎逐请求往返；90%负载时，即使alpha=0.999，权重和分配的大幅往返仍存在。alpha=0.5在20%/60%负载下甚至恶化了闭环指标。",
        "", "因此，实验B支持“短期估计敏感是影响因素之一”，不支持“只调大alpha即可完成稳定性优化”。吞吐、P99、平均份额收敛与逐请求振荡必须分别看。",
        "", "## 2. 实验与控制变量","",
        "- 分支：experiment/rdma-multirail-baseline；运行前提交0b5dba76894dcbaae6c63441a678e065b756b635。生产TENT源码及默认参数不修改，仅实验配置改变alpha。",
        "- alpha是旧值系数：B_new = alpha × B_old + (1-alpha) × B_sample；测试0.01、0.5、0.9、0.99、0.999。",
        "- 独立估计器：每alpha使用5个seed，输入4–6 GB/s的伪随机均匀噪声，均值5 GB/s；预热10000样本，测量1000000样本。没有触发带宽裁剪。另测10→5、5→10 GB/s阶跃的95%响应样本数。",
        "- 稳态闭环：等亲和双Rail、1MiB CPU WRITE、16×64KiB Slice；20%/60%/90%负载×5个alpha×5个seed=75组。每组20000请求，4000请求预热。其余切片、QP、CQ、NUMA、优先级、探测和到达规则与A一致。",
        "- 降速标定：低/中负载下，单独运行50组持续慢轨稳态，给每个alpha/负载/seed建立其实际稳态份额目标，不假定1:2一定是当前算法的目标。",
        "- 动态闭环：225组扰动，加75组相同长度的无扰动对照。扰动为Rail0从12.5降至6.25 GB/s：1ms后恢复、持续到结束、100ms后恢复；观察到事件后300ms。",
        "- 服务时间按分段容量积分，已在传/已post的Slice跨越变化点也受影响；选择器不知道未来事件，网卡标称带宽和EWMA上下限保持不变。模拟的是有效服务能力降低，不是端口重新协商速率。",
        "- 主数据共425组闭环运行、25组独立估计器；另有13个验证进程，总计463个主流水线子进程。5个seed只代表PRNG变化，不能当成独立硬件重复。",
        "", "## 3. 独立估计器：理论关系成立","",
        "在独立同分布、无裁剪、稳态条件下，Var(EWMA)/Var(sample)=(1-alpha)/(1+alpha)。95%阶跃响应样本数为ceil(log(0.05)/log(alpha))。",
        "", "| alpha | 理论方差比 | 实测方差比（5 seed中位数） | 95%下降/上升响应样本数 |", "|---:|---:|---:|---:|",
    ]
    for e in est:lines.append(f"| {e['alpha']} | {e['theory']:.7f} | {e['measured']:.7f} | {e['step95_samples']} / {e['step95_samples']} |")
    lines += ["", "这是方差比，不是振幅比。样本数也不是毫秒：现有算法逐完成事件更新，实际时间响应还取决于每条Rail的完成事件率。单独估计器变慢不意味着完整调度闭环必然按相同比例变慢，因为队列项也会推动选路。闭环中的B_sample由此前分配、排队和CQ完成产生，并非与alpha无关的独立噪声；不能直接套用线性滤波方差公式推算调度稳定性。",
        "", "## 4. 稳态闭环：权重平滑与分配稳定并不等价","",
        "平均权重变动和平均分配变动都用相邻请求归一化份额的0.5×L1差计算，本表乘100后为百分点。这里每个1MiB请求只做一次聚合选路，因此两个步长可在相同请求尺度比较。",
        "", "| alpha | 负载 | 权重平均变动 pp | 分配平均变动 pp | 8:8请求比例 | 首片切换/万请求 | P99 μs | 吞吐 Gb/s |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in grouped:
        lines.append(f"| {r['alpha']} | {r['load']*100:.0f}% | {r['decision_weight_step']*100:.4f} | {r['allocation_step']*100:.4f} | {r['balanced_fraction']*100:.2f}% | {r['first_slice_switches_per_10k_requests']:.1f} | {r['p99_us']:.3f} | {r['goodput_gbps']:.3f} |")
    lines += ["", "20%/60%时，alpha≥0.9仍主要在7:9和9:7间往返，均分8:8约占1%，大体来自周期探测。alpha=0.999虽然把60%的权重平均变动压到约0.03个百分点，分配平均变动仍约12.38个百分点。连续权重只要落在0.5两侧，向下取整再把余数给第一名，就可能制造显著的整数分配跳变。",
        "", "90%时，alpha=0.99/0.999仍有约86.63个百分点的逐请求分配变动，P99约80.634μs；相对alpha=0.01没有明显改善。所有稳态组的有效吞吐基本保持对应的40/120/180Gb/s。不能只根据估计器曲线变平就宣布调度稳定。",
        "", "![实验B概览](experiment-b-overview.svg)","",
        "## 5. 高平滑下为何仍往返：只读评分分解","",
        "在alpha=0.999、seed=1的60%和90%条件下，重放前4100请求并提取预热后的连续100次真实决策。时延前缀与主矩阵一致、trace无丢弃。另计算当次状态下的两个影子分数：忽略队列只看带宽；假设带宽相同只看队列。它们不参与选路，不是改算法重跑的消融。",
        "", "| 负载 | 实际Rail0权重范围 | 只看带宽的权重范围 | 只看队列相对实际权重的平均绝对误差 |",
        "|---:|---:|---:|---:|",
    ]
    for f in decomposition:
        lines.append(f"| {f['load']}% | {f['actual_range'][0]:.6f}–{f['actual_range'][1]:.6f} | {f['bandwidth_only_range'][0]:.6f}–{f['bandwidth_only_range'][1]:.6f} | {f['queue_only_mae']:.6f} |")
    lines += ["", "这与评分分子中的inflight项、聚合分配只使用一次状态快照的机制一致：即使带宽估计较平稳，队列压力仍可能推动大量流量换轨。低负载残留的小权重变化又会被取整放大。各机制贡献尚需C/D中的独立干预，不能把影子分解等同因果证明。",
        "", "## 6. 短期扰动与持续降速","",
        "平均流量份额收敛定义：1ms完整时间窗，份额距离另行标定的同alpha稳态目标不超过5个百分点，连续保持10个窗口；报告第一个合格窗口结束时刻相对事件的时间。该指标不等于逐请求振荡消失，也不是理论最优份额收敛。表中括号为达到条件的seed数/5。",
        "", "| alpha | 负载 | 1ms脉冲额外峰值时延 μs | 持续降速平均份额收敛 ms | 100ms降速后平均份额恢复 ms |",
        "|---:|---:|---:|---:|---:|",
    ]
    for a in alphas:
        for l in [.2,.6]:
            pulse=dynamic(a,l,"pulse1ms");held=dynamic(a,l,"held_drop");recovery=dynamic(a,l,"drop100ms_recover")
            lines.append(f"| {a} | {l*100:.0f}% | {median(pulse,'extra_peak_latency_us'):.3f} | {pretty(settling(held,'down_settle_ms'))} | {pretty(settling(recovery,'recovery_settle_ms'))} |")
    lines += ["", reference_note, "", "额外峰值是同一请求相对无扰动对照增加的最大时延，观察区间覆盖事件前仍可能在途的请求及事件后20ms。短脉冲请求数有限，因此不把这个峰值叫作可靠P99。",
        "", "60%负载下，多数组合的平均份额收敛约1.38ms，接近当前1ms分箱可分辨下界，不能断言它们的亚毫秒响应完全相同。alpha=0.999在1ms脉冲下额外峰值约29μs，低于默认约84μs；但经历100ms降速后，平均份额恢复约11.38ms，默认约1.38ms。平滑带来的抑制瞬态与恢复迟缓需要同时评估，结果不是简单单调关系。",
        "", "90%输入在一条轨降速一半后，需求180Gb/s高于总容量150Gb/s，持续降速阶段不定义有限稳态收敛时间；队列增长属于容量不足。该组的完整诊断结果仍保留在dynamic.csv/原始数据中，不能用它证明alpha响应失败。",
        "", "队列恢复另以“相对无扰动对照的总队列额外量不超过1MiB，连续保持10个1ms窗口”判定，结果保存在summary.json/dynamic.csv。它与平均份额收敛不同；90%输入经历100ms降速后，平均份额恢复约21.59ms，而额外积压清除约150.59ms，五个alpha基本一致。后者受清空积压所需的物理时间限制。", "", "## 7. 验证、来源与限制","",
        f"- 主验证 {len(data['checks'])}/{len(data['checks'])} 通过；ASan/UBSan {len(san)}/{len(san)} 通过；P0/P1回归 {regression['check_count']}/{regression['check_count']} 通过。",
        "- 默认alpha的15组稳态轨迹与实验A完全一致；容量不变事件不改轨迹；静态慢轨与t=0降速一致；跨变化点服务积分与解析时延10486ns/6743ns一致。",
        "- 225组扰动中，变化发生前已完成请求的时延与无扰动对照一致。所有闭环运行检查字节守恒、最终配额排空。",
        "- 首片切换不是已post请求迁移。未传输真实载荷、未运行真实QP/CQ/peer故障；deadline只计数，不模拟生产取消。",
        "- 仅测试当前等亲和、固定1MiB、单Worker模型及指定干预。均值/P99受模型和工作负载约束，不能直接当作生产参数推荐。",
        "- 核心源码和二进制SHA、构建命令、每组配置/seed、完整trace与事件数据均在runs/experiment-b/run/保留。图表为标准Matplotlib生成，依赖隔离在build/plot-deps。",
        "- 首次保存时发现小数alpha文件名被with_suffix截断，已改为追加后缀并完整重跑；最终以run/及case-manifest.json为准，包含逐组文件唯一性和SHA检查。463组配置/结果文件哈希均通过，新旧估计器、稳态、标定与动态汇总逐项一致；旧目录不作为最终原始数据来源。",
        "", "## 8. 交付与下一步","",
        "服务器目录：`/root/mooncake/tent-simulator/`。重跑命令和指标约定见instrumentation/experiment-b.md。", "",
        "- scripts/run_experiment_b.py：估计器、稳态、标定、动态和验证。",
        "- scripts/focus_experiment_b.py：只读评分分解。",
        "- scripts/report_experiment_b.py：表格、图和报告。",
        "- reports/experiment-b-steady.csv、dynamic.csv、estimator.csv：完整组级指标；experiment-b-summary.json：汇总。",
        "", "下一步按原计划C区分更新周期与CQ采样批量，再用D验证取整和单次批量分配的反馈。B已说明调alpha有局部收益，但不足以满足权重和流量分配整体稳定的目标。", "",
    ]
    path=reports/"experiment-b-report.md"
    path.write_text("\n".join(line.rstrip() for line in "\n".join(lines).splitlines())+"\n")
    print("B_REPORT_READY",json.dumps({"checks":summary["validation"],"focus":decomposition}),flush=True)


if __name__=="__main__":main()
