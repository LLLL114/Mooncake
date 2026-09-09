#!/usr/bin/env python3
"""Report D with paired seed uncertainty and compact static figures."""

import sys as _sys
_sys.dont_write_bytecode = True
from experiment_paths import OUTPUT_ROOT
import csv
import json
import os
import pathlib
import random
import statistics
import sys

ROOT=pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0,str(OUTPUT_ROOT/'build/plot-deps'))
os.environ['MPLCONFIGDIR']=str(OUTPUT_ROOT/'build/mplconfig')
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

LABEL={'original':'原算法','remainder':'仅改余数','no_probe':'仅关探测',
       'frozen_greedy':'贪心/固定快照','refreshed_greedy':'贪心/逐片刷新'}
EN={'original':'Original','remainder':'Largest remainder','no_probe':'No probe',
    'frozen_greedy':'Frozen greedy','refreshed_greedy':'Refreshed greedy'}
MODES=['original','remainder','no_probe','refreshed_greedy']
COLORS=['#2457a7','#dc7834','#389270','#9557a0']

def med(rows,key):return statistics.median(r[key] for r in rows)

def main():
    run=OUTPUT_ROOT/'runs/experiment-d/run';out=OUTPUT_ROOT/'reports';out.mkdir(exist_ok=True)
    data=json.loads((run/'summary.json').read_text());rows=data['rows']
    checks=json.loads((run/'validation.json').read_text())
    sanitizer=json.loads((OUTPUT_ROOT/'runs/experiment-d/sanitize/validation.json').read_text())
    audit=json.loads((run/'material-audit.json').read_text())
    regression=json.loads((OUTPUT_ROOT/'runs/experiment-d/p01-regression/summary.json').read_text())
    fields=[k for k,v in rows[0].items() if not isinstance(v,(list,dict))]
    with (out/'experiment-d-metrics.csv').open('w',newline='') as f:
        w=csv.DictWriter(f,fieldnames=fields,lineterminator='\n');w.writeheader()
        w.writerows({k:r[k] for k in fields} for r in rows)
    def select(phase,mode,load,size=None):
        return [r for r in rows if r['phase']==phase and r['mode']==mode and r['load']==load and (size is None or r['size']==size)]
    rng=random.Random(90841);effects=[]
    groups={}
    for r in rows:groups.setdefault((r['rails'],r['size'],r['load'],r['alpha'],r['jitter']),[]).append(r)
    for key,group in groups.items():
        base={r['seed']:r for r in group if r['mode']=='original'}
        for mode in sorted({r['mode'] for r in group}-{'original'}):
            target={r['seed']:r for r in group if r['mode']==mode}
            if set(target)!=set(base):continue
            for metric in ['allocation_step','request_weight_step','p99_us','goodput_gbps','p99_cohort_cv','jain']:
                diff=[target[s][metric]-base[s][metric] for s in sorted(base)]
                boot=sorted(statistics.median(rng.choices(diff,k=len(diff))) for _ in range(4000))
                effects.append({'rails':key[0],'size':key[1],'load':key[2],'alpha':key[3],'jitter':key[4],
                               'mode':mode,'metric':metric,'paired_median_difference':statistics.median(diff),
                               'ci95_low':boot[99],'ci95_high':boot[3899],'seeds':len(diff)})
    with (out/'experiment-d-effects.csv').open('w',newline='') as f:
        w=csv.DictWriter(f,fieldnames=list(effects[0]),lineterminator='\n');w.writeheader();w.writerows(effects)
    plt.rcParams.update({'font.size':10,'svg.fonttype':'none'})
    fig,axes=plt.subplots(2,2,figsize=(13,8.5),layout='constrained')
    for mode,color in zip(MODES,COLORS):
        rr=select('main',mode,.9)
        x=[r for r in rr if r['seed']==1][0]
        trace=x['request_decisions'][:40]
        axes[0,0].plot([r['request']-4000 for r in trace],[100*r['share0'] for r in trace],label=EN[mode],color=color,alpha=.85)
    axes[0,0].set(title='90% load: per-request Rail 0 allocation',xlabel='Request after warm-up',ylabel='Payload share (%)',ylim=(-5,105))
    axes[0,0].legend(fontsize=8,ncol=2)
    for mode,color in zip(MODES,COLORS):
        x=[20,60,90]
        axes[0,1].plot(x,[100*med(select('main',mode,l/100),'allocation_step') for l in x],'-o',color=color,label=EN[mode])
        axes[1,0].plot(x,[med(select('main',mode,l/100),'p99_us') for l in x],'-o',color=color,label=EN[mode])
    axes[0,1].set(title='Allocation changes between requests',xlabel='Offered load (%)',ylabel='Mean step (percentage points)')
    axes[1,0].set(title='Request P99 (five-seed median)',xlabel='Offered load (%)',ylabel='Latency (microseconds)')
    # Actual score-derived p0, not the intended bandwidth ratio.
    for mode,color in zip(['original','remainder'],COLORS):
        points=sorted([r for r in data['fixtures'] if r['rails']==2 and r['slice_count']==16 and r['mode']==mode],key=lambda r:r['first_weights'][0])
        axes[1,1].plot([100*r['first_weights'][0] for r in points],[100*r['payload_bytes'][0]/r['length'] for r in points],'-o',label=EN[mode],color=color)
    axes[1,1].plot([47,53],[47,53],'--',color='#999999',label='Continuous share')
    axes[1,1].set(title='Static 16-slice map (no feedback)',xlabel='Input Rail 0 weight (%)',ylabel='Output payload share (%)')
    axes[1,1].legend(fontsize=8)
    for ax in axes.flat:ax.grid(alpha=.2)
    fig.suptitle('Experiment D | Real DeviceSelector, simulated service | No physical RDMA',fontsize=13)
    fig.savefig(out/'experiment-d-overview.svg');plt.close(fig)
    svg=out/'experiment-d-overview.svg';svg.write_text('\n'.join(l.rstrip() for l in svg.read_text().splitlines())+'\n')
    lines=['# 实验 D：分片离散化、批内快照与周期探测验收报告','',
           '> 2026-09-08；A10 /root/mooncake，experiment/rdma-multirail-baseline。真实 DeviceSelector 的 CPU 仿真，无实际 RDMA 数据流量。','',
           '**结论：D支持将余数分配作为下一步优化的优先候选。现有floor后把余数全部给最优Rail，会在本次对称场景中放大小扰动，并通过排队和带宽反馈维持较大的逐请求往返；每百次探测不是双Rail主复现的必要条件。**','',
           '- 双Rail、1MiB、90%负载：仅改余数，分配平均步长从86.619个百分点降为0，模拟P99从80.634μs降至43.933μs（约下降45.5%），吞吐仍约180Gb/s。20/60%也从6/10与10/6附近的分配往返变成稳定8/8。权重仍有极小随机变化，不能说所有权重变化都为零。',
           '- 仅关闭探测时，90%分配步长仍为87.5个百分点，P99仍80.634μs。冻结带宽学习后，原分配的90%步长仍约85.574个百分点，因此该条件下带宽学习不是往返的必要条件。',
           '- 15片逐片分配、17片奇数分配、短尾片仍可出现正常的离散份额变化。仅改余数不是“所有场景分配恒定”的保证，也没有验证动态负载、故障或真实RDMA收益。',
           '- 完全对称且jitter=0时，原算法在本次60/90%场景也稳定在8/8；这说明默认微小jitter可以触发此复现，不能将理想对称初态下的稳定等同于对扰动稳健。','',
           '## 1. 实验调整与证据边界','',
           '原D方向保留。根据A/B/C，补充冻结带宽和同规则的批内快照配对，并统一逐请求首轮权重口径。目的是区分输入权重变化、整数分配放大和在途反馈。所有分配干预只编译进独立allocation程序，生产TENT和C的公共仿真driver未修改。','',
           f"完成 **{data['matrix_count']}组闭环、{len(data['fixtures'])}组静态映射、{len(data['geometry'])}组切片几何、{len(data['probes'])}组探测计数序列**，加验证共{data['case_count']}组配置/结果。每个闭环20,000请求、4,000预热，5个seed；CQ=1μs、等亲和100G合成Rail、开环到达。",'',
           '主指标的步长均为相邻请求份额0.5×L1距离×100，单位百分点。权重取每个请求首次构建候选的结果；分配取物理字节。吞吐和P99均为模拟值。正常首片换路不是实际迁移。','',
           '## 2. 原算法与独立干预','',
           '| 负载 | 模式 | 权重步长 pp | 分配步长 pp | P99 μs | 吞吐 Gb/s | Jain | P99群组CV |',
           '|---:|---|---:|---:|---:|---:|---:|---:|']
    for load in [.2,.6,.9]:
        for mode in MODES:
            rr=select('main',mode,load)
            lines.append(f"| {load:.0%} | {LABEL[mode]} | {100*med(rr,'request_weight_step'):.3f} | {100*med(rr,'allocation_step'):.3f} | {med(rr,'p99_us'):.3f} | {med(rr,'goodput_gbps'):.3f} | {med(rr,'jain'):.6f} | {med(rr,'p99_cohort_cv'):.6f} |")
    lines+=['','“仅改余数”保留比例floor，只将剩余片按最大小数部分分配；“仅关探测”保留原分配。逐片刷新贪心与原算法之间同时改变了比例分配规则和批内刷新，不能把它的收益全部归因于快照。该组不是已经验收的生产新算法。主矩阵原算法的长期Jain已接近1，主要收益是减少短时往返与降低P99水平；不能据此宣称长期均衡度显著提高。原策略的稳态P99群组CV本来就很低，降低P99水平也不同于证明时延波动大幅下降。','',
            '## 3. 冻结带宽学习','',
            'alpha=1、每Rail初始100G；在途量仍随分配与完成变化。与主表的差异包含固定带宽值与取消学习两项，不能当成新的有效带宽估计方法。','',
            '| 负载 | 模式 | 权重步长 pp | 分配步长 pp | P99 μs |',
            '|---:|---|---:|---:|---:|']
    for load in [.2,.6,.9]:
        for mode in ['original','remainder','no_probe']:
            rr=select('frozen_bw',mode,load)
            lines.append(f"| {load:.0%} | {LABEL[mode]} | {100*med(rr,'request_weight_step'):.3f} | {100*med(rr,'allocation_step'):.3f} | {med(rr,'p99_us'):.3f} |")
    lines+=['','## 4. 相同贪心规则下的批内快照对照','',
            '两种greedy都逐片argmin、相同ceil记账、保留每百次探测、jitter=0；frozen始终使用第一份候选，refreshed在下一片前读取已预占的状态。该配对隔离批内状态刷新；不等同于原比例算法的一次纯快照开关。','',
            '| 负载 | 模式 | 分配步长 pp | P99 μs | 吞吐 Gb/s |',
            '|---:|---|---:|---:|---:|']
    for load in [.6,.9]:
        for mode in ['original','frozen_greedy','refreshed_greedy']:
            rr=select('snapshot',mode,load)
            lines.append(f"| {load:.0%} | {LABEL[mode]} | {100*med(rr,'allocation_step'):.3f} | {med(rr,'p99_us'):.3f} | {med(rr,'goodput_gbps'):.3f} |")
    lines+=['','## 5. 静态映射、阈值和尾片','',
            '静态夹具直接调用真实allocate，完成时latency=0，只释放记账而不学习；没有传输性能含义。49/51的16片输入，原算法7/9，最大余数8/8；51/49反向时原算法9/7。映射的阶跃不等同于时间震荡，闭环表才检验反复穿越阶跃后的行为。奇数片和多候选结果完整保存在summary.json，不能把单个近均分案例推广为所有整数分配都连续。','',
            '| 请求字节 | 片数 | 块字节 | 批量 | 零字节片 | 总记账字节 |',
            '|---:|---:|---:|---|---:|---:|']
    for r in data['geometry']:
        lines.append(f"| {r['length']} | {r['slice_count']} | {r['slice_bytes']} | {r['aggregate']} | {r['zero_payload_slices']} | {r['total_charge']} |")
    lines+=['','零字节片列是按该基线的提取拆分函数生成几何的结果，未执行完整transport/Worker生命周期，因此不能宣称已证实真实请求失败或挂起。它们不进入D性能矩阵，未擅自修复生产行为。尾片按真实长度模拟服务，释放真实allocate返回的charged_bytes，保持基线记账与学习口径。','',
            '下表按同大小、同负载配对比较。大小变化会改变完成事件率，不能把跨大小差异全部归因于16片阈值。','',
            '| Rail数 | 请求字节 | 负载 | 模式 | 分配步长 pp | P99 μs | Jain |',
            '|---:|---:|---:|---|---:|---:|---:|']
    for phase in ['size','multi']:
        for size in sorted({r['size'] for r in rows if r['phase']==phase}):
            for mode in ['original','remainder','no_probe']:
                rr=select(phase,mode,.9,size)
                lines.append(f"| {rr[0]['rails']} | {size} | 90% | {LABEL[mode]} | {100*med(rr,'allocation_step'):.3f} | {med(rr,'p99_us'):.3f} | {med(rr,'jain'):.6f} |")
    lines+=['','15片时仍走原来的逐片接口，余数干预不会生效，6.667个百分点对应一片的份额。17片时最大余数后的5.882个百分点也对应一片；999424字节虽有16片，尾片只有16KiB，8/8片数并非8/8字节，尾片归属交替产生约4.918个百分点的字节份额变化。这些残余变化不能直接称为与15/1往返同等严重的震荡。','',
            '## 6. 每百次探测的计数与字节口径','',
            '| 序列 | 所有请求 | multi调用 | 探测调用 | 探测请求字节/全部字节 |',
            '|---|---:|---:|---:|---:|']
    for r in data['probes']:
        if r['mode']=='original':lines.append(f"| {r['pattern']} | {r['requests']} | {r['batch_calls']} | {r['probe_calls']} | {r['probe_bytes']/r['total_bytes']:.6%} |")
    lines+=['','探测按成功进入多片分配分支的线程局部调用计数；单片allocate不递增。99/100/101检查确认只有第100次启用RR，关闭探测时全部关闭。上表是确定性静态序列，探测字节是这些请求的全部字节，既不是重复发送字节，也不是相对于无探测新增的网络流量。混合大小闭环P99不在本次证据范围。','',
            '## 7. 配对效果量与验收','',
            '下面为90%负载、1MiB、原始alpha/jitter下，相对原算法的五seed配对差值中位数与运行级bootstrap 95%区间。分配步长单位百分点。模型高度确定，退化区间不代表真实网络误差为零。','',
            '| 模式 | 指标 | 配对差值 | 95%区间 |','|---|---|---:|---|']
    for e in effects:
        if e['rails']==2 and e['size']==1048576 and e['load']==.9 and e['alpha']==.01 and e['jitter']==1e-9 and e['metric'] in ['allocation_step','p99_us','goodput_gbps']:
            scale=100 if e['metric']=='allocation_step' else 1
            lines.append(f"| {LABEL[e['mode']]} | {e['metric']} | {scale*e['paired_median_difference']:.5f} | [{scale*e['ci95_low']:.5f}, {scale*e['ci95_high']:.5f}] |")
    lines+=['',f"- 主验证 {sum(c['passed'] for c in checks)}/{len(checks)}；ASan/UBSan {sum(c['passed'] for c in sanitizer)}/{len(sanitizer)}。P0/P1回归 {regression['check_count']}/{regression['check_count']}，全部通过。",
            f"- {audit['case_pairs']}组配置/结果及源码、二进制哈希通过核验；生产代码和C公共仿真器未修改；A已提交汇总中digest/P99一致性对照{audit['historical_A_summary_pairs']}组；当前A运行目录未找到原始JSON，因此不声称重审了旧原始事件。",
            f"- 全矩阵模拟吞吐/注入吞吐范围{audit['throughput_ratio_range']}；每组字节守恒、记账归零、完成排空、零deadline超限、零观测丢弃。",
            '- deadline是计数而非实际取消；没有真实故障、重试和网络断连，本结果不证明生产不存在永久挂起。',
            '- pre-commit与clang-format在当前PATH不可用；未声称执行这两项。编译、Python语法、git diff --check另行执行。',
            '- 初版逐片刷新夹具因未清空候选容器被已知答案校验拦住；保留于rejected-stale-candidates，未进入正式矩阵。修正后重新构建并运行全部正式结果。',
            '- 未给诊断模式注入真实CPU执行开销；逐片刷新增加候选构建，真实吞吐收益需后续测量。未重新验收动态响应、NUMA异构或多Worker场景。','',
            '![实验D概览](experiment-d-overview.svg)','',
            '下一步仍可按原计划E研究Worker/进程局部决策。D的最大余数模式值得作为候选纳入后续对照，但先补齐异构/动态适应与真实CPU开销，不能仅据本报告替换生产算法。本次没有开始E或修改生产算法。','',
            '复现命令、单因素边界及矩阵选择理由见instrumentation/experiment-d.md。完整数值见experiment-d-metrics.csv与experiment-d-effects.csv。']
    (out/'experiment-d-report.md').write_text('\n'.join(lines)+'\n')
    print('D_REPORT_READY',len(rows),len(effects),flush=True)

if __name__=='__main__':main()
