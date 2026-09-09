#!/usr/bin/env python3

import sys as _sys
_sys.dont_write_bytecode = True
from experiment_paths import OUTPUT_ROOT
import pathlib,json,csv,statistics,random,sys,os
ROOT=pathlib.Path(__file__).resolve().parents[1];run=OUTPUT_ROOT/'runs/candidate-algorithms';out=OUTPUT_ROOT/'reports'
sys.path.insert(0,str(OUTPUT_ROOT/'build/plot-deps'));os.environ['MPLCONFIGDIR']=str(OUTPUT_ROOT/'build/mplconfig')
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
POLICIES=['legacy','old_remainder','largest_remainder','earliest_finish','byte_deficit']
LABEL={'legacy':'旧算法','old_remainder':'D旧余数版','largest_remainder':'新最大余数','earliest_finish':'预计完成时间','byte_deficit':'字节债务'}
SHORT=['Legacy','D remainder','New remainder','Earliest finish','Byte deficit']
def med(rr,key):return statistics.median(r[key] for r in rr)
def write_csv(name,rows):
 fields=list(dict.fromkeys(k for r in rows for k in r))
 with (out/name).open('w',newline='') as f:
  w=csv.DictWriter(f,fieldnames=fields,lineterminator='\n');w.writeheader();w.writerows(rows)
def main():
 sim=json.loads((run/'sim/summary.json').read_text())['rows'];real=json.loads((run/'real/summary.json').read_text())['rows'];audit=json.loads((run/'audit.json').read_text())
 old=json.loads((OUTPUT_ROOT/'runs/candidate-algorithms-v1/sim/summary.json').read_text())['rows']
 validation=json.loads((run/'validate/checks.json').read_text());sanitize=json.loads((run/'sanitize/checks.json').read_text())
 unit_count=sum(x.startswith('PASS ') for x in (run/'validate/unit.log').read_text().splitlines())
 old_rate=float((OUTPUT_ROOT/'runs/candidate-algorithms-v1/overlap-result.txt').read_text().split()[0]);new_rate=float((run/'validate/capacity-overlap.log').read_text().split()[0])
 def select(data,kind,topo,load,policy,size=1048576):return [r for r in data if r['kind']==kind and r['topology']==topo and r['load']==load and r['policy']==policy and r['size']==size]
 # Add actual per-rail balance and fixed-window completion variability.
 caps={mask:med([r for r in real if r['kind']=='calibration' and r['mask']==mask],'goodput_gbps') for mask in [1,2]}
 for r in real:
  d=json.loads((run/'real'/(r['name']+'.json')).read_text());rates=[x*8/d['elapsed_ns'] for x in d['completion_bytes']];u=[rates[0]/caps[1],rates[1]/caps[2]]
  r['jain']=sum(u)**2/(2*sum(x*x for x in u));r['rail0_share']=d['completion_bytes'][0]/sum(d['completion_bytes']);warm=d['config']['warmup_requests']
  lo=(d['submitted_ns'][warm]+9999999)//10000000;hi=max(d['finished_ns'])//10000000;bins=[0]*(hi-lo)
  for t in d['finished_ns']:
   k=t//10000000
   if lo<=k<hi:bins[k-lo]+=1048576
  r['goodput_cv_10ms']=statistics.pstdev(bins)/statistics.mean(bins) if len(bins)>=20 else None
 srows=[]
 for r in sim:
  row={k:v for k,v in r.items() if k not in ['stats','phases']}
  if 'phases' in r:
   for phase,values in r['phases'].items():srows.append({**row,'phase':phase,**values})
  else:srows.append(row)
 write_csv('candidate-sim-metrics.csv',srows);write_csv('candidate-real-metrics.csv',[{k:v for k,v in r.items() if k!='stats'} for r in real])
 effects=[];rng=random.Random(9101)
 for kind in ['real','saturated']:
  for topo in ['equal','dual_numa']:
   for load in ([.2,.6,.9] if kind=='real' else [1.0]):
    baseline={r['seed']:r for r in select(real,kind,topo,load,'legacy')}
    for policy in POLICIES[1:]:
     other={r['seed']:r for r in select(real,kind,topo,load,policy)}
     for metric in ['p99_us','goodput_gbps','allocation_step','allocation_wall_ns','p99_cv']:
      values=[other[s][metric]-baseline[s][metric] for s in baseline];boot=sorted(statistics.median(rng.choices(values,k=5)) for _ in range(4000))
      effects.append({'kind':kind,'topology':topo,'load':load,'policy':policy,'metric':metric,'paired_median_delta':statistics.median(values),'ci95_low':boot[99],'ci95_high':boot[3899]})
 write_csv('candidate-real-effects.csv',effects)
 plt.rcParams.update({'svg.fonttype':'none','font.size':10});fig,axes=plt.subplots(2,2,figsize=(13,8.5),layout='constrained');colors=['#607080','#80a0aa','#cc833d','#2875b2','#30966b']
 for policy,label,color in zip(POLICIES,SHORT,colors):
  axes[0,0].plot([20,60,90],[med(select(real,'real','dual_numa',l,policy),'p99_us') for l in [.2,.6,.9]],'-o',label=label,color=color)
 axes[0,0].set(title='Same-host RDMA, NUMA scoring',xlabel='Offered fraction (%)',ylabel='Scheduled P99 (us)',yscale='log');axes[0,0].legend(fontsize=8)
 for j,topo in enumerate(['equal','dual_numa']):
  axes[0,1].bar([i+(j-.5)*.35 for i in range(5)],[med(select(real,'saturated',topo,1.0,p),'goodput_gbps') for p in POLICIES],width=.35,label=topo)
 axes[0,1].set_xticks(range(5),SHORT,rotation=20,ha='right');axes[0,1].set(title='Saturated: fixed 64 request slots',ylabel='Goodput (Gb/s)');axes[0,1].legend(fontsize=8)
 axes[1,0].bar(range(5),[med(select(real,'real','dual_numa',.9,p),'allocation_wall_ns')/1000 for p in POLICIES],color=colors);axes[1,0].set_xticks(range(5),SHORT,rotation=20,ha='right');axes[1,0].set(title='Full allocate wall time, real NUMA / 90%',ylabel='Microseconds (instrumented)')
 rr=[r for r in sim if r['kind']=='concurrent' and r['workers']==8 and r['burst']]
 axes[1,1].bar(range(5),[med([r for r in rr if r['policy']==p],'p99_us') for p in POLICIES],color=colors);axes[1,1].set_xticks(range(5),SHORT,rotation=20,ha='right');axes[1,1].set(title='8 synchronized workers, simulated service',ylabel='Request P99 (us)')
 for ax in axes.flat:ax.grid(axis='y',alpha=.2)
 fig.suptitle('Candidate RDMA schedulers | Fixed parameters | Five paired seeds/runs')
 fig.savefig(out/'candidate-algorithms-overview.svg');plt.close(fig);p=out/'candidate-algorithms-overview.svg';p.write_text('\n'.join(x.rstrip() for x in p.read_text().splitlines())+'\n')
 high_base=med(select(real,'real','dual_numa',.9,'legacy'),'p99_us')
 best=min(POLICIES[2:],key=lambda p:med(select(real,'real','dual_numa',.9,p),'p99_us'))
 high_new=med(select(real,'real','dual_numa',.9,best),'p99_us')
 mid_best=min(POLICIES[2:],key=lambda p:med(select(real,'real','dual_numa',.6,p),'p99_us'))
 lines=['# 新多轨 RDMA 算法：实现与性能对比报告','',
 '> A10 `/root/mooncake`，实验分支 `experiment/rdma-multirail-baseline`。保留旧算法默认路径；新增可复用C++函数，通过测试适配器接入真实DeviceSelector和RDMA数据路径。','',
 f"**本轮1MiB、双NUMA、90%注入下，新方案中P99最低的是{LABEL[best]}：旧算法{high_base:.3f}μs，新方案{high_new:.3f}μs，变化{(high_new/high_base-1)*100:+.1f}%。中负载下{LABEL[mid_best]}更低。**",
 '饱和吞吐约保持在163Gb/s量级，但函数分配耗时增加，P99相对波动也没有在所有场景同时下降。应按业务场景选择候选，不能据一个高负载点替换全部默认策略。','',
 '## 1. 交付内容与比较原则','',
 '- `algorithms/rail_scheduler.h/.cpp`：最大余数、预计完成时间、字节债务三种算法。模块不依赖verbs，可独立编译；第二/第三种以一个Scheduler作为线程安全预占域。',
 '- 最大余数是可直接调用的`allocateLargestRemainder()`；另外两种通过`Scheduler(Policy::EarliestFinish/ByteDeficit).allocate()`调用，并由posted/completed反馈维护状态。',
 '- 旧算法legacy和D旧余数版old_remainder作为同期对照；不把新模块的额外计算成本排除在实机结果外。',
 '- 最终825组仿真（550稳态、100降容/恢复、175并发）、215组实机（15标定、150固定注入、50饱和）。所有候选使用同一组固定参数，没有按场景调参。',
 '- 实机每组12288个1MiB请求，前4096个用于预热；比之前4096/256的短测更长。本文真正可归因的是本轮同条件新旧配对，不能直接把跨轮绝对P99差异当作收益。','',
 '生产TENT文件未修改，默认算法未替换；本轮交付的是实现完整、已接入测试驱动的候选模块。完整Worker/endpoint的生产反馈接线、多peer/真实多进程、GPU性能、跨节点及故障恢复验收尚未完成。','',
 '## 2. 实现不是机械照搬初稿：保留v1并修正两处问题','',
 'v1完整源码、二进制、650组仿真和165组实机结果保存在`runs/candidate-algorithms-v1`，没有删除不利结果。','',
 '| 1MiB / 双NUMA / 90%仿真 | v1 P99 μs | v2 P99 μs |','|---|---:|---:|']
 for policy in POLICIES:lines.append(f"| {LABEL[policy]} | {med(select(old,'steady','dual_numa',.9,policy),'p99_us'):.3f} | {med(select(sim,'steady','dual_numa',.9,policy),'p99_us'):.3f} |")
 lines+=['','第一处问题：把NUMA惩罚乘到全部排队工作上，会以更大的排队代价维持亲和偏好。v2以物理预计完成时间为主，只给当前Slice增加最多2μs的亲和偏好；不再将原有队列乘以5/10倍。','',
 f'第二处问题：posted未归零不等于链路一直有待发送工作。100G服务、50μs完成可见延迟、85μs请求间隔的解析测试中，v1误估约{old_rate*8/1e9:.3f}Gb/s，v2约{new_rate*8/1e9:.3f}Gb/s。v2要求样本来自10μs内提交的burst，或完成时确有未post的软件积压；否则重新开始容量采样。','',
 '这两处修正之后完整重跑，而非只补跑有利场景。另将适配器计时计数改为原子操作以支持并发测试。','',
 '## 3. 算法与默认参数','',
 '| 策略 | 实际行为 |','|---|---|',
 '| legacy | 原始评分、逐完成alpha=.01 EWMA、floor后余数给最优Rail、每百次多片RR |',
 '| old_remainder | D的旧诊断版本，只改余数；仍沿用旧ceil记账 |',
 '| largest_remainder | 独立最大余数函数、严格排序、按实际Slice字节记账；保留旧评分/EWMA和每百次多片RR |',
 '| earliest_finish | 在互斥区中逐片选择预计完成代价最小的Rail，并立即预占字节；容量/延迟独立估计 |',
 '| byte_deficit | 以capacity/NUMA产生慢目标，累计字节债务，超过拥塞保护阈值时选择更快Rail；实际字节补偿 |','',
 '后两种使用20ms容量时间常数、至少20μs/256KiB有效窗口；慢目标每1ms检查，20ms时间常数、2pp滞回、单步TV上限10pp；债务限幅。QP项是posted WR对应用预算128的压力代理，不是完整QP池状态。探测预算为输入字节的1%，每次最多首片，间隔至少10ms，不制造额外payload。','',
 '```text\nfinish_cost_ns = (reserved_bytes + slice_bytes) / capacity × 1e9\n  + min(2000, (NUMA_penalty−1) × slice_bytes / capacity × 1e9)\n  + latency_residual_ns + QP_pressure_cost\n```','',
 'reserved已包含软件队列，不能重复加同一队列。延迟项来自忙碌期首WR的完成时间减去串行化时间；容量观测不足时保持先验/上次值，不宣称总能识别空闲路径的真实容量。详情和调用约束见`instrumentation/candidate-algorithms.md`。','',
 '## 4. 同机真实 RDMA：固定注入','',
 '两卡各自RDMA-CM回环，真实WRITE/CQ；同一源缓冲区注册到两NIC，独立目标缓冲区。保留64请求槽、每Rail128 posted WR。每WR完成身份/状态/字节与最终被写槽内容均检查；不是每次覆盖前请求的独立CRC校验。','',
 '| 评分亲和 | 注入 | 策略 | goodput Gb/s | 计划到达P99 μs | 提交后P99 μs | 分配步长 pp | P99群组CV |',
 '|---|---:|---|---:|---:|---:|---:|---:|']
 for topo in ['equal','dual_numa']:
  for load in [.2,.6,.9]:
   for policy in POLICIES:
    rr=select(real,'real',topo,load,policy);lines.append(f"| {topo} | {load:.0%} | {LABEL[policy]} | {med(rr,'goodput_gbps'):.3f} | {med(rr,'p99_us'):.3f} | {med(rr,'service_p99_us'):.3f} | {100*med(rr,'allocation_step'):.3f} | {med(rr,'p99_cv'):.4f} |")
 lines+=['','所有值为5次运行中位数。计划到达时延包括生成器和环形槽背压导致的迟到；不能只报更好看的提交后时延。固定注入能跟上，只证明该注入点，不证明吞吐上限没有下降。','',
 '### 配对差值与区间：双NUMA / 90%','',
 '| 策略相对legacy | P99差 μs（95%区间） | 分配步长差 pp（95%区间） |','|---|---:|---:|']
 for policy in POLICIES[1:]:
  dd={e['metric']:e for e in effects if e['kind']=='real' and e['topology']=='dual_numa' and e['load']==.9 and e['policy']==policy};a=dd['p99_us'];b=dd['allocation_step']
  lines.append(f"| {LABEL[policy]} | {a['paired_median_delta']:.3f} [{a['ci95_low']:.3f}, {a['ci95_high']:.3f}] | {100*b['paired_median_delta']:.3f} [{100*b['ci95_low']:.3f}, {100*b['ci95_high']:.3f}] |")
 lines+=['','区间用5个运行级配对差值bootstrap，不把大量相关Slice当独立样本。共享主机、同机回环和当前驱动边界仍限制外推。','',
 '## 5. 饱和吞吐与分配成本','',
 '| 评分亲和 | 策略 | 饱和goodput Gb/s | 相对legacy | 分配墙钟耗时 μs |','|---|---|---:|---:|---:|']
 for topo in ['equal','dual_numa']:
  base=med(select(real,'saturated',topo,1.0,'legacy'),'goodput_gbps')
  for policy in POLICIES:
   rr=select(real,'saturated',topo,1.0,policy);rate=med(rr,'goodput_gbps');lines.append(f"| {topo} | {LABEL[policy]} | {rate:.3f} | {(rate/base-1)*100:+.2f}% | {med(rr,'allocation_wall_ns')/1000:.3f} |")
 lines+=['','饱和指该驱动固定64槽/128 WR预算下的闭环，不是裸网卡线速。allocate耗时为统一插桩的完整调用墙钟时间，包含候选构建、算法与配额操作，可能受抢占影响；不是纯CPU周期，也不包含全部完成反馈处理成本。实际吞吐已经包含这些反馈成本。不能因新分配更平稳而忽略饱和吞吐回退。','',
 '## 6. 仿真交叉检查：尾片、NUMA、动态与并发','',
 '| 大小 | 评分亲和 | 策略 | 90% P99 μs | 分配步长 pp | Jain |','|---:|---|---|---:|---:|---:|']
 for size in [65536,999424,1048576]:
  for topo in ['equal','dual_numa']:
   for policy in POLICIES:
    rr=select(sim,'steady',topo,.9,policy,size);lines.append(f"| {size} | {topo} | {LABEL[policy]} | {med(rr,'p99_us'):.3f} | {100*med(rr,'allocation_step'):.3f} | {med(rr,'jain'):.5f} |")
 lines+=['','单片请求的100%相邻请求换路可以是正常均衡，不等于坏震荡；尾片还要同时看字节公平。新最大余数与D旧余数版在尾片记账、带宽样本与排序上有差异，不能把尾片结果全部归因于余数处理。','',
 '| 降容场景 | 策略 | 扰动中P99 μs | 恢复阶段P99 μs | 峰值队列 MiB |','|---|---|---:|---:|---:|']
 for load,duration in [(.6,100),(.9,1000)]:
  for policy in POLICIES:
   rr=[r for r in sim if r['kind']=='drop' and r['load']==load and r['duration_ms']==duration and r['policy']==policy]
   lines.append(f"| {load:.0%}/{duration}ms | {LABEL[policy]} | {statistics.median(r['phases']['during']['p99_us'] for r in rr):.3f} | {statistics.median(r['phases']['after']['p99_us'] for r in rr):.3f} | {statistics.median(r['phases']['during']['queue_peak'] for r in rr)/1048576:.3f} |")
 lines+=['','90%降容后180G需求超过150G剩余能力，积压不可由选路算法消除；这个场景检验代价与恢复，不以“队列不增长”作为不可能的通过条件。','',
 '| Worker/到达 | 策略 | 仿真P99 μs | goodput Gb/s |','|---|---|---:|---:|']
 for workers,burst in [(1,True),(8,True),(8,False)]:
  for policy in POLICIES:
   rr=[r for r in sim if r['kind']=='concurrent' and r['workers']==workers and r['burst']==burst and r['policy']==policy]
   lines.append(f"| {workers}/{'同步' if burst else '错峰'} | {LABEL[policy]} | {med(rr,'p99_us'):.3f} | {med(rr,'goodput_gbps'):.3f} |")
 lines+=['','并发使用真实OS线程、固定总负载和共享QP服务预算，旧路径构造先读后预占交错；新调度器以自己的预占锁协调。模拟服务不计入锁等待CPU时间，因此不将此表当作实机多Worker吞吐结论。模块只协调共享同一Scheduler的线程，不自动解决跨进程不可见负载。','',
 '并发图表曾发现一个对照接线错误：E驱动未读取allocation_mode，D旧余数标签最初实际运行legacy。所有175组并发数据已隔离并完整重跑，另增加5个旧余数均分断言和5seed旧策略轨迹复核。稳态、降容、实机、库单测程序重建前后二进制完全一致，因此保留这些不受影响的结果；证明与旧数据在concurrent-control-correction。','',
 '在本轮等亲和同步突发中，正确接入的D旧余数版本也能消除放大，不能把并发图的全部收益归因于互斥预占。新调度器协调的正确性另由同时预占的单片线程测试验证。','',
 '## 7. 使用建议、回退与验证','',
 f'- 本轮1MiB高负载可优先继续评估{LABEL[best]}；双NUMA中负载可优先评估{LABEL[mid_best]}。这是候选选择建议，不是已完成生产默认替换或带未完成请求的自动策略切换。',
 '- 先看目标拓扑/大小/负载对应行，再看饱和吞吐和分配成本；没有用一张等亲和图宣布所有场景最优。',
 '- 最大余数适合作为较小策略变化的候选，但通用模块/适配器有额外开销。预计完成时间侧重消除排队代价，字节债务侧重可控目标与长期公平；是否默认采用应由实机表及业务约束决定。',
 '- `new_policy`选择三种新方案或legacy；非legacy要求`smart=true`。回退采用排空后重建/重启为legacy，不声称有未完成请求时可无缝热切换。',
 '- 生产接入必须将Scheduler生命周期和selector绑定，完整接入post成功、CQ成功/失败、未post取消以及重试释放。当前注册表/生成源码是测试适配层，不是生产热更新实现。',
 '- 尚未覆盖跨节点、真实多进程、多peer、GPU性能与完整TENT错误恢复。真实程序有运行deadline，库有记账校验；这些不构成生产永不挂起的证明。','',
 f"验证：库内{unit_count}项断言、容量重叠解析检查、{len(validation)}/{len(validation)}集成检查、ASan/UBSan {len(sanitize)}/{len(sanitize)}通过；所有最终配置/结果及源码/二进制哈希核验通过。对数：{audit['case_pairs']}。",
 '所有文件在原实验分支`experiments/rdma-multirail-baseline`。原始数据在`runs/candidate-algorithms`，v1另行封存。完整CSV、配对区间与构建方法一并保留。','',
 '![候选算法性能概览](candidate-algorithms-overview.svg)']
 (out/'candidate-algorithms-report.md').write_text('\n'.join(lines)+'\n')
 concise={'sim_cases':len(sim),'real_cases':len(real),'unit_assertions':unit_count,'integration_checks':len(validation),'real_90':[],'saturation':[]}
 for topo in ['equal','dual_numa']:
  for policy in POLICIES:
   rr=select(real,'real',topo,.9,policy);concise['real_90'].append({'topology':topo,'policy':policy,**{k:med(rr,k) for k in ['p99_us','goodput_gbps','allocation_step','allocation_wall_ns']}})
   rr=select(real,'saturated',topo,1.0,policy);concise['saturation'].append({'topology':topo,'policy':policy,'goodput_gbps':med(rr,'goodput_gbps')})
 (run/'conclusions.json').write_text(json.dumps(concise,indent=2)+'\n');print('CANDIDATE_REPORT_READY',flush=True)
if __name__=='__main__':main()
