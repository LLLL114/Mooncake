#!/usr/bin/env python3
"""Generate evidence-backed experiment A tables and standard scientific plots."""

import sys as _sys
_sys.dont_write_bytecode = True
from experiment_paths import OUTPUT_ROOT
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
    run=OUTPUT_ROOT/"runs/experiment-a/run"; reports=OUTPUT_ROOT/"reports"
    raw=json.loads((run/"metrics.json").read_text())
    focus=json.loads((run/"focus.json").read_text())
    fine=json.loads((run/"fine_examples.json").read_text())
    validation=json.loads((run/"validation.json").read_text())
    sanitizer=json.loads((OUTPUT_ROOT/"runs/experiment-a/sanitize/validation.json").read_text())
    regression=json.loads((OUTPUT_ROOT/"runs/experiment-a/p01-regression/summary.json").read_text())
    assert len(raw)==320 and all(c["passed"] for c in validation+sanitizer) and regression["all_correctness_passed"]
    def med(t,size,load,policy,key):
        vals=[r[key] for r in raw if (r["topology"],r["size"],r["load"],r["strategy"])==(t,size,load,policy)]
        assert len(vals)==5
        return statistics.median(vals) if vals[0] is not None else None
    steady=[r for r in raw if r["load"]<=.9 and not(r["topology"]=="dual_numa" and r["strategy"]=="rr" and r["load"]>.5)]
    max_flow_cv=max(r["throughput_cv"] for r in steady)*100
    max_tail_cv=max(r["p99_cohort_cv"] for r in steady)*100
    summaries=[]
    keys=sorted({k for r in raw for k,v in r.items() if isinstance(v,(int,float)) and k not in ["size","load","seed","digest"]})
    for topo in ["equal","dual_numa"]:
        for size in [65536,524288,1048576,16777216]:
            for load in [.2,.6,.9,1.1]:
                cell={"topology":topo,"size":size,"load":load}
                for policy in ["smart","rr"]:
                    cell[policy]={k:med(topo,size,load,policy,k) for k in keys}
                    points=[r for r in raw if (r["topology"],r["size"],r["load"],r["strategy"])==(topo,size,load,policy)]
                    cell[policy]["p99_seed_range_us"]=[min(r["p99_us"] for r in points),max(r["p99_us"] for r in points)]
                summaries.append(cell)
    focused=[]
    for f in focus:
        rows=f["rows"]; p=np.array([r["p0"] for r in rows]); inflight=np.array([r["inflight"] for r in rows])
        corr=float(np.corrcoef(p[:-1],p[1:])[0,1]) if np.std(p)>0 else None
        focused.append({"load":f["load"],"p0_min":float(p.min()),"p0_max":float(p.max()),
            "p0_lag1":corr,"empty_before_fraction":float(np.mean(np.sum(inflight,axis=1)==0)),
            "counts":f["allocation_counts"],"first12":rows[:12],"prefix_parity":f["prefix_latency_matches_matrix"]})
    result={"matrix_runs":320,"seeds":[1,2,19,12345,4294967295],"groups":summaries,
            "focus":focused,"validation_count":len(validation),"sanitizer_count":len(sanitizer),
            "p01_regression_count":regression["check_count"],
            "plot_versions":{"numpy":np.__version__,"matplotlib":matplotlib.__version__}}
    (reports/"experiment-a-summary.json").write_text(json.dumps(result,indent=2)+"\n")
    (reports/"experiment-a-metrics.csv").write_text((run/"metrics.csv").read_text())
    plt.rcParams.update({"svg.fonttype":"none","font.size":10})
    fig,axes=plt.subplots(1,2,figsize=(10,3.6),layout="constrained")
    sizes=[65536,524288,1048576,16777216]; x=np.arange(4)
    colors={"smart":"#007f86","rr":"#82909b"}
    for policy,offset in [("smart",-.18),("rr",.18)]:
        axes[0].bar(x+offset,[med("equal",s,.9,policy,"goodput_gbps") for s in sizes],width=.34,label=policy,color=colors[policy])
        axes[1].bar(x+offset,[med("equal",s,.9,policy,"p99_us") for s in sizes],width=.34,label=policy,color=colors[policy])
    for ax in axes:
        ax.set_xticks(x,["64 KiB","512 KiB","1 MiB","16 MiB"]);ax.spines[['top','right']].set_visible(False);ax.legend()
    axes[0].set_ylabel("Useful throughput (Gb/s)");axes[0].set_ylim(0,205)
    axes[1].set_ylabel("Request P99 (microseconds)");axes[1].set_yscale("log")
    fig.suptitle("Experiment A: equal-affinity, 90% load, 5-seed medians\nSynthetic CPU model, not A10 network measurements",fontsize=11)
    fig.savefig(reports/"experiment-a-overview.svg");plt.close(fig)
    fig,axes=plt.subplots(3,2,figsize=(10,7),layout="constrained")
    for index,f in enumerate(focus):
        rows=f["rows"]; xx=np.arange(100)
        axes[index,0].plot(xx,[r["p0"] for r in rows],lw=1,color=colors['smart'])
        axes[index,0].set_ylim(0,1);axes[index,0].set_ylabel(f"{f['load']}% load\nRail 0 weight")
        axes[index,1].step(xx,[r["assigned_slices"][0] for r in rows],where="mid",color=colors['smart'])
        axes[index,1].axhline(8,color=colors['rr'],ls="--",label="RR = 8/16")
        axes[index,1].set_ylim(0,16);axes[index,1].set_ylabel("Slices assigned to Rail 0")
        for ax in axes[index]:ax.spines[['top','right']].set_visible(False)
    axes[-1,0].set_xlabel("Request index (after warm-up)")
    axes[-1,1].set_xlabel("Consecutive request index");axes[0,1].legend()
    fig.suptitle("CPU simulation: 1 MiB decision weights and allocation (seed 1)\nFixed load and capacity; no failures injected",fontsize=11)
    fig.savefig(reports/"experiment-a-decisions.svg");plt.close(fig)
    for svg in reports.glob("experiment-a-*.svg"):
        svg.write_text("\n".join(line.rstrip() for line in svg.read_text().splitlines())+"\n")
    # Fine vs coarse snapshots are preserved; actual decision plots avoid snapshot aliasing.
    compact_fine=[]
    for e in fine:
        if not e["config"]["smart"]:continue
        rows=e["windows"]
        compact_fine.append({"name":e["name"],"interval_ns":10000,
            "reference_p0":[round(r["reference_p"][0],7) for r in rows]})
    (reports/"experiment-a-fine-reference.json").write_text(json.dumps(compact_fine)+"\n")
    lines=["# 实验 A：现有多轨 RDMA 算法稳态基线报告","","> 2026-09-08；A10 上实际执行的 CPU 仿真。不是跨节点 RDMA 性能测试。","",
        "## 1. 结论","",
        "在本模型的等亲和、1 MiB CPU WRITE 请求中，复现了持续的权重与 Slice 分配往返：即使外部到达率和 Rail 容量恒定，smart 仍几乎逐请求切换首片路径。吞吐和长期平均流量均衡基本保持不变，但请求 P99 水平升高。",
        "",
        "本次没有复现非过载稳态下明显的吞吐持续震荡或分窗 P99 大幅波动。不能把“尾时延变高”写成“P99 波动变大”，也不能把正常多轨分配叫作已提交请求迁移。110% 负载以及双 NUMA 轮询的部分场景为过载，必须单独解释。",
        "", "## 2. 环境、版本和方法","",
        "- 分支：`experiment/rdma-multirail-baseline`；实验前提交 `e73337e69`，生产 TENT 基线 `1c65ced88e509440e31ea88a76594660461d0eab`。生产算法未修改。",
        "- 320 组：2 种亲和拓扑 × 4 种请求大小 × 4 档负载 × smart/轮询 × 5 个 seed。",
        "- 每组 20,000 请求，前 4,000 请求预热；统计后 16,000 请求。CPU 8 单 Worker，固定间隔开环到达。不同 seed 只提供 PRNG 变化，不是独立物理环境重复。",
        "- 两条独立合成 Rail，每条 12.5 GB/s；基础完成延迟 1 μs，CQ poll 1 μs，每 Rail 模拟 QP 深度 128。未引入背景网络流量、GPU 或 provider 活动。",
        "- 原始 smart 参数：旧值 alpha=0.01、NUMA 惩罚[1,5,10]、jitter=1e-9、QoS 开启、HIGH 优先级。实际检查表明本配置下关闭 QoS 不改变轨迹，不应将它作为本次波动的根因。",
        "- 从固定源码提取纯切片计算块并编译，绑定 CPU WRITE 的 max_slice_count=32：64KiB→1片/逐片；512KiB→8片/逐片；1MiB→16片/聚合；16MiB→32片/聚合，每片512KiB。没有运行完整 transport/endpoint 数据路径。",
        "- 双 NUMA 使用A10式亲和层级：内存NUMA0，两个NIC分别在NUMA0/1；跨NUMA仍使用相同合成带宽/延迟，实际跨NUMA成本未标定。",
        "- 吞吐只统计预热结束至停止注入之间的完整时间窗，不计排空阶段。请求P99按到达归属统计，包含测量段到达但排空期才完成的请求。",
        "- 权重参考快照1ms，另外在1MiB场景以10μs复查且轨迹摘要一致。流量窗口至少覆盖约20次到达，避免大请求固有突发污染；窗口宽度在CSV中公开。P99波动使用每200请求一组的80个连续群组。",
        "- 统计表为5个seed的中位数，完整数据另有seed范围；不把seed范围或确定性重复解释为硬件置信区间。",
        "", f"已界定非过载案例中，吞吐窗口CV最大为{max_flow_cv:.2f}%，P99群组CV最大为{max_tail_cv:.2f}%。这表示波动较小，并非完全没有波动；窗口口径不同的指标不能直接互比。", "", "## 3. 等亲和拓扑：关键结果","",
        "| 请求 | 负载 | smart/RR 吞吐 Gb/s | smart/RR P99 μs | P99变化 | smart首片切换/万请求 | smart P99群组CV |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    cases=[(1048576,l) for l in [.2,.6,.9,1.1]]+[(s,.9) for s in [65536,524288,16777216]]
    labels={65536:"64 KiB",524288:"512 KiB",1048576:"1 MiB",16777216:"16 MiB"}
    for s,l in cases:
        get=lambda policy,key:med("equal",s,l,policy,key)
        change=(get("smart","p99_us")/get("rr","p99_us")-1)*100
        lines.append(f"| {labels[s]} | {l*100:.0f}% | {get('smart','goodput_gbps'):.3f} / {get('rr','goodput_gbps'):.3f} | {get('smart','p99_us'):.3f} / {get('rr','p99_us'):.3f} | {change:+.1f}% | {get('smart','first_slice_switches_per_10k_requests'):.1f} | {get('smart','p99_cohort_cv'):.6f} |")
    lines += ["", "110% 行是过载诊断，不属于可持续稳态。两种策略都出现排队累积；其 P99 群组趋势不能用来证明控制回路持续震荡。64KiB 的轮询本来就每请求交替路径，首片切换率为10000/万请求，因此单凭切换次数无法评价算法好坏。",
        "", "![等亲和90%负载吞吐与P99](experiment-a-overview.svg)","",
        "## 4. 逐请求复现证据","",
        "对1MiB、seed=1的20/60/90%负载分别保留完整前4100请求trace，并提取预热后的连续100请求。所有4100个请求时延与主矩阵对应前缀逐项相同，trace丢弃为0。",
        "", "| 负载 | Rail0真实决策权重范围 | 相邻请求权重相关系数 | 决策前两Rail队列均空的比例 | 100请求中的分片模式 |",
        "|---|---:|---:|---:|---|",
    ]
    for f in focused:
        lines.append(f"| {f['load']}% | {f['p0_min']:.4f}–{f['p0_max']:.4f} | {f['p0_lag1']:.4f} | {f['empty_before_fraction']*100:.1f}% | {f['counts']} |")
    lines += ["", "![连续请求的真实权重和分片结果](experiment-a-decisions.svg)","",
        "源码机制与现象一致：逐 Slice post→CQ 完成时间既受实际传输也受批内排队影响；默认EWMA接近直接采用新样本。较多分片进入某Rail后，后续样本可能降低其估计速率，下一请求又转向另一Rail。比例取整和余数交给最优路径可能放大变化。",
        "", "这是机制解释与定向复现，不是对各因素贡献的独立因果证明。尚未改变alpha、更新周期或余数策略；它们应在实验B/C/D中消融。1ms参考权重还会随单个请求的排队/排空自然变化，因此判定往返以真实决策trace为主，不能仅看周期快照。",
        "", "## 5. 失衡和双NUMA结果","",
        "等亲和1MiB在20/60/90%下，长期平均流量仍接近50:50，持续平均失衡未复现；瞬时分配/队列往返不能与长期均衡混为一谈。",
        "", "| 双NUMA，1MiB负载 | smart/RR吞吐 Gb/s | smart/RR P99 μs | smart平均流量失衡 | RR超过1s期限比例 |",
        "|---|---:|---:|---:|---:|",
    ]
    for l in [.2,.6,.9,1.1]:
        g=lambda policy,key:med("dual_numa",1048576,l,policy,key)
        lines.append(f"| {l*100:.0f}% | {g('smart','goodput_gbps'):.3f} / {g('rr','goodput_gbps'):.3f} | {g('smart','p99_us'):.3f} / {g('rr','p99_us'):.3f} | {g('smart','imbalance')*100:.2f}% | {g('rr','latency_deadline_fraction')*100:.2f}% |")
    lines += ["", "这里的失衡=abs(rail0吞吐−rail1吞吐)/总吞吐。RR仅使用首个可用亲和层级，所以双NUMA下只用本地NIC；当负载超过两Rail合计容量的50%时，RR已超过自身单Rail容量。这一差异来自候选策略，不能全部归因EWMA。smart允许跨层级使用NIC，但本模型未标定真实跨NUMA代价。",
        "", "## 6. 对目标的逐项判断","",
        "| 目标现象 | 本次判断 |", "|---|---|",
        "| 权重/分配持续往返 | 在等亲和1MiB等合成场景复现，有连续请求证据 |",
        "| 长期负载失衡 | 等亲和关键场景没有显著持续失衡；双NUMA有亲和策略造成的差异 |",
        "| 频繁迁移 | 观测到新请求路径切换；模型未实现对已post请求的搬迁，重试迁移为0 |",
        "| P99变高 | 复现，尤其1MiB/90%负载；不能扩展到所有大小与拓扑 |",
        "| 稳态吞吐/P99强烈波动 | 当前固定到达、固定容量场景未发现明显持续波动；过载趋势单列 |",
        "| 采样周期/alpha的因果贡献 | 尚未做干预；10μs复查仅验证观测与轨迹一致性 |",
        "", "## 7. 验证与可复现性","",
        f"- 实验A检查 {len(validation)}/{len(validation)} 通过；ASan/UBSan {len(sanitizer)}/{len(sanitizer)} 通过；P0/P1回归 {regression['check_count']}/{regression['check_count']} 通过。",
        "- 小/大请求生产切片、观察开关与快照频率不改轨迹、HIGH优先级QoS等价、字节守恒、最终配额排空、双NUMA RR资格范围均已检查。",
        "- RR的生产selector不维护inflight；本次另记独立的模型队列字节，避免把RR负载错误报告为零。",
        "- deadline_exceeded只表示完成时延超过阈值，模型仍排空请求；不等价于生产取消、超时恢复或永久挂起验证。无真实DMA载荷与故障注入。",
        "- 硬件信息为结束后快照，仿真容量是配置值；本阶段没有使用GPU/网络负载。源码、二进制SHA、配置与seed见run/manifest.json。",
        f"- 图表依赖隔离安装到build/plot-deps，NumPy {np.__version__}，Matplotlib {matplotlib.__version__}；未安装到共享Python环境。",
        "", "## 8. 文件与下一步","",
        "服务器目录：`/root/mooncake/tent-simulator/`。",
        "", "```bash", "python3 scripts/build.py", "python3 scripts/run_experiment_a.py", "python3 scripts/focus_experiment_a.py", "python3 scripts/report_experiment_a.py", "```", "",
        "以上命令在实验目录执行；生成报告前还需完成验收和安装隔离绘图库，详见instrumentation/experiment-a.md。",
        "", "- reports/experiment-a-metrics.csv：320组完整指标。", "- reports/experiment-a-summary.json：32个条件组及逐请求摘要。",
        "- runs/experiment-a/run/：逐组配置、原始结果、fine/reference与完整focus压缩trace。",
        "- runs/experiment-a/environment/：环境快照；p01-regression/与sanitize/：检查证据。",
        "", "建议下一步做实验B：在相同复现条件下仅改变alpha，先测试1MiB的20/60/90%负载，分别看权重往返、P99水平与收敛速度；不以吞吐变化作为唯一判断标准。", "",
    ]
    (reports/"experiment-a-report.md").write_text("\n".join(line.rstrip() for line in "\n".join(lines).splitlines())+"\n")
    print("REPORT_A_READY",json.dumps({"focus":[{k:v for k,v in f.items() if k!='first12'} for f in focused],
        "checks":[len(validation),len(sanitizer),regression["check_count"]]}),flush=True)


if __name__=="__main__":main()
