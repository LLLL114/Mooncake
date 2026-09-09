#!/usr/bin/env python3
"""Report sampling cadence, CQ effects and observation aliasing separately."""

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
    run=OUTPUT_ROOT/"runs/experiment-c/run";reports=OUTPUT_ROOT/"reports"
    data=json.loads((run/"summary.json").read_text())
    san=json.loads((OUTPUT_ROOT/"runs/experiment-c/sanitize/validation.json").read_text())
    regression=json.loads((OUTPUT_ROOT/"runs/experiment-c/p01-regression/summary.json").read_text())
    audit=json.loads((run/"material-audit.json").read_text())
    assert audit["verified_case_pairs"]==580 and all(x["passed"] for x in data["checks"]+san) and regression["all_correctness_passed"]
    def selection(g,t,p,l,pos="head"):
        r=[x for x in data["rows"] if (x["update_interval_ns"],x["time_constant_ns"],x["poll_interval_ns"],x["load"],x["sample_position"])==(g,t,p,l,pos)]
        assert len(r)==5;return r
    def med(g,t,p,l,key,pos="head"):return statistics.median(r[key] for r in selection(g,t,p,l,pos))
    groups=[]
    for r in data["rows"]:
        key=(r["update_interval_ns"],r["time_constant_ns"],r["poll_interval_ns"],r["load"],r["sample_position"])
        if any(x["key"]==key for x in groups):continue
        keys=["goodput_gbps","p99_us","p99_cohort_cv","decision_weight_step","allocation_step","balanced_fraction","mean_actual_update_us","selection_bias_ratio","cq_batch_mean","chosen_sample_mean_GBps","raw_sample_mean_GBps"]
        values={k:med(*key[:4],k,key[4]) for k in keys}
        groups.append({"key":key,**values})
    scalar={x["name"]:x for x in data["temporal"]}
    obs=[x for x in data["observations"] if x["update_interval_ns"]==0 and x["time_constant_ns"]==0]
    obs.sort(key=lambda x:x["observation_ns"])
    ratio=obs[0]["reference_weight_tv_per_second"]/obs[-1]["reference_weight_tv_per_second"]
    min_throughput=min(r["goodput_gbps"]/(200*r["load"]) for r in data["rows"])
    max_throughput=max(r["goodput_gbps"]/(200*r["load"]) for r in data["rows"])
    result={"groups":groups,"temporal":data["temporal"],"observations":data["observations"],"representative":data["representative"],
            "validation":[len(data["checks"]),len(san),regression["check_count"]],"cases":data["case_count"],
            "throughput_to_offered_range":[min_throughput,max_throughput],"observation_tv_ratio":ratio,"audit":audit}
    (reports/"experiment-c-summary.json").write_text(json.dumps(result,indent=2)+"\n")
    keys=[k for k,v in data["rows"][0].items() if not isinstance(v,(dict,list))]
    with (reports/"experiment-c-metrics.csv").open("w",newline="") as f:
        w=csv.DictWriter(f,fieldnames=keys,lineterminator="\n");w.writeheader();w.writerows({k:r[k] for k in keys} for r in data["rows"])
    plt.rcParams.update({"svg.fonttype":"none","font.size":10})
    fig,axes=plt.subplots(2,2,figsize=(10,7),layout="constrained")
    x=np.array([10,100,1000])
    axes[0,0].plot(x,[scalar[f"rate_p{p}_t0"]["t95_ns"]/1e6 for p in [10000,100000,1000000]],"o-",label="Fixed alpha=0.9",color="#d67b35")
    axes[0,0].plot(x,[scalar[f"rate_p{p}_t50000000"]["t95_ns"]/1e6 for p in [10000,100000,1000000]],"o-",label="Time constant=50ms",color="#007f86")
    axes[0,0].set_xscale("log");axes[0,0].set_yscale("log");axes[0,0].set_xlabel("Input event interval (microseconds)");axes[0,0].set_ylabel("Step95 response (ms)");axes[0,0].legend();axes[0,0].set_title("Isolated time scale")
    gs=[0,100000,1000000,5000000,10000000];ix=np.arange(5)
    for t,label,color in [(0,"Fixed alpha=0.01","#d67b35"),(50000000,"Time constant=50ms","#007f86")]:
        axes[0,1].plot(ix,[med(g,t,1000,.9,"p99_us") for g in gs],"o-",label=label,color=color)
    axes[0,1].set_xticks(ix,["event","0.1","1","5","10"]);axes[0,1].set_xlabel("Minimum update interval (ms)");axes[0,1].set_ylabel("Request P99 (microseconds)");axes[0,1].legend();axes[0,1].set_title("90% load; CQ period 1us")
    ps=[10000,50000,100000]
    for pos,label,color in [("head","First eligible sample","#87939e"),("tail","Last sample of CQ group","#007f86")]:
        axes[1,0].plot(np.array(ps)/1000,[med(1000000,0,p,.9,"p99_us",pos) for p in ps],"o-",label=label,color=color)
    axes[1,0].set_xlabel("CQ poll period (microseconds)");axes[1,0].set_ylabel("Request P99 (microseconds)");axes[1,0].legend();axes[1,0].set_title("Update 1ms; fixed alpha=0.01")
    axes[1,1].plot([o["observation_ns"]/1000 for o in obs],[o["reference_weight_tv_per_second"] for o in obs],"o-",color="#007f86")
    axes[1,1].set_xscale("log");axes[1,1].set_yscale("log");axes[1,1].set_xlabel("Observation interval (microseconds)");axes[1,1].set_ylabel("Sampled weight TV / second");axes[1,1].set_title("Same policy, same trajectory and P99")
    for ax in axes.flat:ax.spines[['top','right']].set_visible(False)
    fig.suptitle("Experiment C: update rate, CQ and observation are different controls\nSynthetic CPU model; real DeviceSelector; 1 MiB requests",fontsize=11)
    svg=reports/"experiment-c-overview.svg";fig.savefig(svg);plt.close(fig)
    svg.write_text("\n".join(x.rstrip() for x in svg.read_text().splitlines())+"\n")
    lines=["# 实验 C：采样周期、更新节奏与 CQ 的影响","","> 2026-09-08；A10 上的真实 DeviceSelector CPU 仿真，未进行真实 RDMA 数据路径验证。","",
        "## 1. 结论","",
        "实验C表明，更新频率确实影响算法，但不能把采样周期看成一个独立的稳定性旋钮。固定alpha时改变更新节奏会改变时间记忆；门控还会改变被选样本，CQ周期又同时影响完成延迟、在途计数和样本值。固定时间常数可以统一恒定信号的响应尺度，但未自动消除闭环分配往返。",
        "",f"另外，只改变观测周期就能让记录到的权重总变差相差约{ratio:.0f}倍，而调度轨迹与P99完全不变。不能把粗粒度日志的平滑当成算法改善。","",
        "## 2. 方法与数据范围","",
        "- 分支：experiment/rdma-multirail-baseline；运行前版本e3f9b6e72f4605cc34124adbbf9c16b0e303e6b3。生产TENT不修改，生成独立controlled可执行程序做诊断干预。",
        "- 525组闭环：300组更新间隔/时间常数主矩阵、135组CQ对照、90组CQ末样本诊断；另有26组时间尺度、12组仅观测对照、17个验证运行，共580组配置/结果。",
        "- 主矩阵：等亲和双100G合成Rail、固定1MiB CPU WRITE/16个64KiB Slice、20/60/90%负载、5个seed，单Worker。统一至少1秒预热、至少1秒测量，覆盖最大100ms时间常数的10倍预热。",
        "- 原始策略每个成功完成事件更新带宽EWMA，旧值alpha=0.01。门控按NIC独立执行，在上次接受更新后达到最小间隔时接受首个成功样本；间隔为0.1/1/5/10ms。没有新样本时不更新，因此不是无条件的墙钟定时器。",
        "- 固定alpha组仍为0.01。固定时间常数组使用exp(-实际经过时间/tau)，tau=20/50/100ms；同时间戳不重复计入时间衰减。名义间隔和实际间隔都记录。",
        "- CQ周期1/10/50/100μs。head选择首个满足门槛的样本，tail等待同NIC同时间戳组的最后一个样本；不改变CQ完成消费顺序。",
        "- 门控只跳过带宽学习，始终先释放真实在途记账。派生权重仍根据最新在途量逐请求重算，并不是每个周期才发布一次或冻结权重。沿用原EWMA公式、带宽上下限、拓扑/QoS/探测与Slice分配。",
        f"- 全矩阵有效吞吐与注入吞吐之比范围约[{min_throughput:.5f}, {max_throughput:.5f}]；差异需要结合完整窗口边界看，不能用单个窗口抖动代替有效吞吐。",
        "", "## 3. 完成事件率与时间记忆","",
        "独立估计器输入从10阶跃至5GB/s，不触发裁剪。固定alpha的95%响应需要ceil(log(0.05)/log(alpha))个样本，墙钟时间随事件间隔变化；固定tau在此恒定输入下约为3tau。",
        "", "| 成功样本间隔 | 固定alpha=0.9的95%响应 | tau=50ms的95%响应 |", "|---:|---:|---:|",
    ]
    for p in [10000,100000,1000000]:
        lines.append(f"| {p/1000:.0f}μs | {scalar[f'rate_p{p}_t0']['t95_ns']/1e6:.3f}ms | {scalar[f'rate_p{p}_t50000000']['t95_ns']/1e6:.3f}ms |")
    lines += ["", "| 更新门槛 | 固定alpha=0.01 | tau=20ms | tau=50ms | tau=100ms |", "|---|---:|---:|---:|---:|"]
    for g in gs:
        vals=[scalar[f"cadence_g{g}_t{t}"]["t95_ns"]/1e6 for t in [0,20000000,50000000,100000000]]
        lines.append("| "+("逐事件" if g==0 else f"{g/1e6:g}ms")+" | "+" | ".join(f"{v:.3f}ms" for v in vals)+" |")
    lines += ["", "上表输入事件间隔固定10μs，故最小更新门槛可准确落在样本时钟上；实际闭环有空闲和CQ合批，接受更新的时刻不一定等于名义周期。不规则样本测试也确认用实际Δt计算alpha，而不是盲用名义间隔。",
        "", "## 4. 固定alpha与固定时间常数的闭环对照","",
        "下表是5个seed的中位数，CQ周期1μs；权重和分配的平均变化为相邻请求份额0.5×L1距离乘100，单位为百分点。固定tau表中选择50ms，其余20/100ms数据在CSV中保留。",
        "", "| 负载 | 学习模式 | 最小更新间隔 | 实际平均间隔 μs | 权重步长 pp | 分配步长 pp | P99 μs | P99群组CV | 选中/全部样本均值 |", "|---:|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for l in [.6,.9]:
        for t in [0,50000000]:
            for g in gs:
                vals=[med(g,t,1000,l,k) for k in ["mean_actual_update_us","decision_weight_step","allocation_step","p99_us","p99_cohort_cv","selection_bias_ratio"]]
                lines.append(f"| {l*100:.0f}% | {'alpha=0.01' if t==0 else 'tau=50ms'} | {'逐事件' if g==0 else str(g/1e6)+'ms'} | {vals[0]:.2f} | {vals[1]*100:.3f} | {vals[2]*100:.3f} | {vals[3]:.3f} | {vals[4]:.5f} | {vals[5]:.3f} |")
    lines += ["", "关键点：固定alpha时，90%负载的1ms/10ms门控使P99从约80.63μs升至104.98/112.62μs。固定tau=50ms避免了这部分额外抬升，但高负载分配往返仍很大。固定alpha的10ms门槛还把90%负载P99群组CV从约0.002%增至13.0%；不能只看动作变少而忽略尾时延波动。60%负载下部分门控/时间常数组合显著降低分配步长，不能直接推广到90%或突变响应；完整CSV同时保留平均失衡与Jain，动作更少也不自动意味着均衡更好。",
        "", "固定tau只统一时间尺度，不让不同样本序列变成同一输入。样本值由此前分配、排队和CQ完成产生；采用实际Δt也改变每个样本的增益。表中的样本均值比为未裁剪样本的简单均值比，不等于最终EWMA，也不代表全部增益加权偏差。",
        "", "## 5. CQ批量与代表样本","",
        "固定人工样本回放：每100μs提供同时间戳的2/4/6/8GB/s四个样本，更新门槛1ms、alpha=0.01。head与tail接受相同数量、相同时刻的更新，但最终估计分别为2与8GB/s；全部样本均值是5GB/s。该回放只验证选样规则，不宣称来自真实CQ。",
        "", "真实调度代码闭环对照（90%负载、1ms门控）：", "", "| CQ周期 | 模式 | 平均CQ组大小 | head/tail样本均值比 | head/tail P99 μs |", "|---:|---|---:|---:|---:|",
    ]
    for p in ps:
        for t in [0,50000000]:
            lines.append(f"| {p/1000:.0f}μs | {'alpha=0.01' if t==0 else 'tau=50ms'} | {med(1000000,t,p,.9,'cq_batch_mean'):.2f} | {med(1000000,t,p,.9,'selection_bias_ratio'):.3f} / {med(1000000,t,p,.9,'selection_bias_ratio','tail'):.3f} | {med(1000000,t,p,.9,'p99_us'):.3f} / {med(1000000,t,p,.9,'p99_us','tail'):.3f} |")
    lines += ["", "表中均值比各自以该运行全部成功样本均值为分母。CQ周期变大直接增加完成可见延迟，也改变在途计数与post→CQ样本，不能将P99差异全部归因于EWMA更新频率。固定alpha时首末选样差异明显；固定tau缓和了部分差异，但这不是“末样本总是更好”的证据。",
        "", "## 6. 仅改变观测：不能把日志变平当作优化","",
        "三种策略各用10/100μs、1/10ms观测窗口重跑，共12组，全部轨迹摘要一致。下表展示原策略，比较公共区间[1.01s,1.99s)，消除了不同窗口边界范围的影响。",
        "", "| 观测间隔 | 参考权重标准差 | 采样权重TV/s | P99 μs |", "|---:|---:|---:|---:|",
    ]
    for o in obs:lines.append(f"| {o['observation_ns']/1000:g}μs | {o['reference_weight_std']:.6f} | {o['reference_weight_tv_per_second']:.3f} | {o['p99_us']:.3f} |")
    lines += ["", f"标准差仍约0.226，P99始终80.634μs，轨迹未变；总变差却相差约{ratio:.0f}倍，说明跨分辨率比较TV会严重误导。10μs本身也不是无限精度的真值，只是本次较细的观测。",
        "", "![实验C概览](experiment-c-overview.svg)", "", "## 7. 验证、限制与下一步","",
        f"- 主验证 {len(data['checks'])}/{len(data['checks'])} 通过；ASan/UBSan {len(san)}/{len(san)} 通过；P0/P1回归 {regression['check_count']}/{regression['check_count']} 通过。580组配置/结果与源码/二进制SHA均已核验。",
        "- 零门控与原策略相同；被跳过学习的完成仍归还配额；CQ末项 bookkeeping 不改变原策略；门控间隔与时间常数阶跃符合解析值。",
        "- 固定1MiB、单Worker、等亲和、固定容量和到达率的CPU模型；未验证真实RDMA、GPU、跨节点拥塞、多Worker局部决策或故障恢复。",
        "- 本阶段没有对新门控策略复做容量突变的完整B矩阵，低负载稳态收益不能当作动态适应性已通过。也未声称全量观测开销符合P0/P1稀疏观测开销。",
        "- 原始日志、每组参数、实际接受间隔、CQ重复时间戳、样本均值、case-manifest与运行SHA均在runs/experiment-c/run；完整指标在reports/experiment-c-metrics.csv。",
        "", "下一步按原计划实验D，独立检查整数取整、余数归属、一次快照批量分配与周期探测。C已说明仅调时间尺度不能可靠解决高负载往返，且实验必须固定观测口径并记录选样偏差。",
        "", "重跑命令与门控语义见instrumentation/experiment-c.md；所有代码及报告均在实验分支，生产TENT未修改。", "",
    ]
    (reports/"experiment-c-report.md").write_text("\n".join(x.rstrip() for x in "\n".join(lines).splitlines())+"\n")
    print("C_REPORT_READY",json.dumps({"checks":result["validation"],"cases":data["case_count"],"throughput_range":[min_throughput,max_throughput],"observation_tv_ratio":ratio}),flush=True)


if __name__=="__main__":main()
