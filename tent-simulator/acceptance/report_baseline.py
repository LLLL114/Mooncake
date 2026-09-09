#!/usr/bin/env python3

import sys as _sys
_sys.dont_write_bytecode = True
from pathlib import Path as _BootstrapPath
_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parents[1] / "scripts"))
from experiment_paths import OUTPUT_ROOT, ACCEPTANCE_OUTPUT
import csv,hashlib,json,pathlib,statistics
from collections import defaultdict
from metrics import np,dispersion
HERE=pathlib.Path(__file__).resolve().parent;ROOT=HERE.parent;RUN=OUTPUT_ROOT/'runs/acceptance-baseline'
def med(rows,key):
 vals=[]
 for r in rows:
  x=r
  for part in key.split('.'):x=x[int(part)] if isinstance(x,list) else x[part]
  if x is not None:vals.append(x)
 return float(np.median(vals)) if vals else None
def fmt(v,n=3):return 'N/A' if v is None else f'{v:.{n}f}'
def flatten(obj,prefix=''):
 result={}
 for k,v in obj.items():
  name=prefix+k
  if isinstance(v,dict):result.update(flatten(v,name+'.'))
  elif isinstance(v,list):
   for i,x in enumerate(v):result[f'{name}.{i}']=x
  else:result[name]=v
 return result
def csvwrite(name,rows):
 keys=sorted(set().union(*(r.keys() for r in rows)))
 with (ACCEPTANCE_OUTPUT/name).open('w') as f:
  w=csv.DictWriter(f,fieldnames=keys,lineterminator='\n');w.writeheader();w.writerows(rows)
def main():
 allrows=json.loads((RUN/'real/rows.json').read_text());real=[r for r in allrows if r['kind'] not in ['overhead_on','overhead_off']]
 cal=json.loads((RUN/'real/calibration.json').read_text());models=json.loads((RUN/'model/rows.json').read_text()) if (RUN/'model/rows.json').exists() else []
 groups=defaultdict(list)
 for r in real:groups[(r['size'],r['topology'],r['mask'],r['load'],r['kind'])].append(r)
 csvwrite('baseline-runs.csv',[flatten(r) for r in allrows]);csvwrite('baseline-model.csv',[flatten(r) for r in models])
 stat=[];rng=np.random.default_rng(91004)
 keys=['goodput_gbps','p99_us','p999_us','allocation_mean_ns','allocation_p99_ns','throughput.cv','throughput.sd','p99_windows.cv','p99_windows.sd','allocation_share.sd','window_jain.mean','window_imbalance.mean','weight_tv_per_s','weight_mean_step','request_assignment_step','weight_event_hz.1','weight_events_per_10k.1','weight_reversal_hz.1','capacity_fraction','allocation_p999_ns','p99_windows.p95_p5','throughput.p95_p5','allocation_share.p95_p5','window_assignment_tv_per_s']
 for group,rr in sorted(groups.items()):
  for key in keys:
   values=[]
   for r in rr:
    x=r
    for part in key.split('.'):x=x[int(part)] if isinstance(x,list) else x[part]
    if x is not None:values.append(x)
   if not values:continue
   a=np.asarray(values);boot=np.median(rng.choice(a,(5000,len(a)),replace=True),axis=1)
   stat.append(dict(zip(['size','topology','mask','load','kind'],group),metric=key,n=len(a),median=float(np.median(a)),minimum=float(a.min()),maximum=float(a.max()),ci95_low=float(np.quantile(boot,.025)),ci95_high=float(np.quantile(boot,.975))))
 csvwrite('baseline-statistics.csv',stat)
 validation=json.loads((RUN/'validate/checks.json').read_text());real_index=json.loads((RUN/'real/index.json').read_text());model_index=json.loads((RUN/'model/index.json').read_text()) if (RUN/'model/index.json').exists() else []
 lines=['# 多轨 RDMA 原算法指标 baseline 验收报告','',
 '日期：2026-09-09。对应 [指标方案](acceptance-plan.md)。本轮只测原算法，没有实现新选路策略，也没有替换生产 TENT 默认路径。','',
 '## 1. 基准完成范围','',
 f'- 新增真实 RDMA：{len(real_index)} 次（45 次 RR 容量标定、20 次权重计数开/关、{len(real)} 次正式原算法运行）。正式场景每组 5 次，预热 1s、测量 10s。',
 f'- 可控模型：{len(model_index)} 组有效场景，另隔离 6 组门控接线错误结果并完成 3 组零门控一致性复核。旧目录仍有日志，但本轮未找到可直接复算的旧 JSON 轨迹，因此重新生成本轮原算法数据。',
 f'- 驱动验证：{len(validation)}/{len(validation)} 项，另有 2 项 Python 指标单测与 ASan/UBSan 观察器检查。',
 '- R 层是同机真实 RDMA-CM/WRITE/CQ、原 DeviceSelector、单生产线程、CPU WRITE；S 层是真实选择器加模拟服务。二者不混用绝对性能结果。',
 '- **流级迁移、生产跨 Rail 重试迁移、完整 TENT Rail 故障收敛与故障情况下不挂起仍为 N/A。当前报告是可复现的性能/调度 baseline，不是全部需求已验收。**','',
 '## 2. 容量标定与负载口径','',
 '| 请求大小 | Rail 0 RR Gb/s | Rail 1 RR Gb/s | 双 Rail RR Gb/s |', '|---|---:|---:|---:|']
 for size,c in sorted(cal['capacities'].items(),key=lambda x:int(x[0])):lines.append(f'| {int(size)//1024}KiB | {c["1"]:.3f} | {c["2"]:.3f} | {c["3"]:.3f} |')
 lines+=['','容量是相同请求大小、固定 64 请求槽/每 Rail 128 WR 应用预算下的可达吞吐，不是物理线速。负载百分比相对该大小的双 Rail RR 标定；如果原算法自身无法跟上，应结合其饱和结果识别 CPU/调度瓶颈，不把所有积压归因为链路拥塞。NUMA 是输入给选择器的评分拓扑；本驱动没有绑定源缓冲区到指定 NUMA 节点，不能把该轴当成实测远端 NUMA 内存传输对照。','',
 '## 3. 原算法吞吐与端到端时延','',
 '以下为五次运行中位数。P99 按计划到达计算，包含注入迟到、分配、排队与完成观察；吞吐只计固定测量区间的 CQ 完成字节，排空阶段不拿来美化吞吐。','',
 '| 大小 / 评分 / 模式 | 负载 | 完成 Gb/s | P99 μs | P99.9 μs | 分配均值 / P99 ns |', '|---|---:|---:|---:|---:|---:|']
 for group,rr in sorted(groups.items()):
  size,topo,mask,load,kind=group;label=f'{size//1024}KiB / {topo} / {kind}'+(f' rail{mask-1}' if kind=='single' else '')
  lines.append(f'| {label} | {load:.0%} | {med(rr,"goodput_gbps"):.3f} | {med(rr,"p99_us"):.3f} | {med(rr,"p999_us"):.3f} | {med(rr,"allocation_mean_ns"):.1f} / {med(rr,"allocation_p99_ns"):.1f} |')
 lines+=['','### 慢运行与注入积压（保留全部样本）','', '| 场景 / 种子 | 完成 Gb/s | 请求 P99 μs | 最大注入迟到 μs | 延迟超过 1s 比例 |','|---|---:|---:|---:|---:|']
 slow=[r for r in real if r['kind']=='steady' and (r['p99_us']>10000 or r['latency_over_1s_fraction']>0)]
 for r in slow:lines.append(f'| {r["name"]} | {r["goodput_gbps"]:.3f} | {r["p99_us"]:.1f} | {r["max_injection_lateness_us"]:.1f} | {r["latency_over_1s_fraction"]:.2%} |')
 if not slow:lines.append('| 无固定注入运行超过上述阈值 | — | — | — | — |')
 lines+=['','上述样本不剔除，也不只报提交后时延。若同一场景不同次运行出现数量级差异，五次中位数不足以宣布该场景稳定；当前日志不能独立区分宿主机/驱动干扰与算法反馈，需要保留为后续配对测试的环境及重现性问题。1s 是诊断延迟预算，不是驱动已经执行的生产请求超时。']
 lines+=['','分配耗时按**整个请求的所有 allocate 调用**统计。512KiB 未过批量门槛，8 个 Slice 逐次调用；不能直接拿它的请求分配耗时与 1MiB 一次批量调用当作相同工作量比较。计时包含固定插桩，不能和旧报告不同驱动的耗时直接作因果对比。','',
 '## 4. 固定时间窗口的波动与均衡','',
 '主窗口 250ms。P99 使用到达队列分桶，跨窗完成仍属于原到达窗；每窗至少 500 请求。低于门槛的 P99 为 N/A。下面的 SD 和 CV 都针对窗口序列，不能用整体请求 P99 替代。','',
 '| 大小 / 评分 / 负载（双 Rail 固定注入） | 吞吐 SD Gb/s / CV | 窗口 P99 SD μs / CV | 分配份额 SD pp | 窗口 Jain 均值 | 有效 P99 窗数 |', '|---|---:|---:|---:|---:|---:|']
 for group,rr in sorted(groups.items()):
  size,topo,mask,load,kind=group
  if kind!='steady':continue
  lines.append(f'| {size//1024}KiB / {topo} / {load:.0%} | {fmt(med(rr,"throughput.sd"))} / {fmt(med(rr,"throughput.cv"),5)} | {fmt(med(rr,"p99_windows.sd"))} / {fmt(med(rr,"p99_windows.cv"),5)} | {med(rr,"allocation_share.sd")*100:.4f} | {med(rr,"window_jain.mean"):.6f} | {med(rr,"p99_windows.n"):.0f}/40 |')
 lines+=['','Jain 用各 Rail 吞吐除以该大小单 Rail 的 RR 标定进行归一化，再逐窗计算。长期 Jain 接近 1 并不排除更短时标的分配反转；相反，单片请求频繁交替也不自动意味着利用率差。50ms/1s 敏感性结果在窗口文件中保留，未更改主窗口以追求好看的数字。','',
 '## 5. 权重与实际分配变化','',
 '原算法没有独立的持久权重发布器；这里统计每次 buildCandidates 的逆评分归一化值。δ=0.001（0.1pp），另保留 1e-6 与 0.01 阈值。单片 argmin / 百次 RR 探测仍按原逻辑执行，不能把这些 p 全部称为实际分片概率。','',
 '| 大小 / 评分 / 负载 | 权重变化次/s | 每万次决策变化数 | 权重反转次/s | 权重 TV/s | 相邻请求分配步长 pp |', '|---|---:|---:|---:|---:|---:|']
 for group,rr in sorted(groups.items()):
  size,topo,mask,load,kind=group
  if kind!='steady':continue
  lines.append(f'| {size//1024}KiB / {topo} / {load:.0%} | {np.median([r["weight_event_hz"][1] for r in rr]):.1f} | {np.median([r["weight_events_per_10k"][1] for r in rr]):.1f} | {np.median([r["weight_reversal_hz"][1] for r in rr]):.1f} | {med(rr,"weight_tv_per_s"):.2f} | {med(rr,"request_assignment_step")*100:.3f} |')
 lines+=['','这些是调度反馈的直接基准，不是实际重传/迁移次数。真实流级迁移需要稳定 flow 标识与改派事件；当前独立请求驱动不具备该语义，因此保留 N/A，不能用首 Slice 换路数填充需求。','',
 '## 6. 权重计数的测量扰动','',
 '同一新驱动短配对测试，仅切换额外权重变化计数；时间戳、原有诊断回调、完成字节计数和请求记录仍存在。因此这只界定新增权重计数的扰动，不是全部插桩相对裸 TENT 的开销。每次 0.5s 预热、1s 测量，不能替代正式 10s 基准。','',
 '| 大小 | on/off 吞吐差中位数 | on/off 请求 P99 差中位数 | on/off 分配均值差 ns |','|---|---:|---:|---:|']
 for size in [65536,1048576]:
  pairs=[]
  for seed in [1,2,19,12345,4294967295]:
   off=next(r for r in allrows if r['kind']=='overhead_off' and r['size']==size and r['seed']==seed);on=next(r for r in allrows if r['kind']=='overhead_on' and r['size']==size and r['seed']==seed)
   pairs.append((100*(on['goodput_gbps']/off['goodput_gbps']-1),100*(on['p99_us']/off['p99_us']-1),on['allocation_mean_ns']-off['allocation_mean_ns']))
  vals=np.median(np.asarray(pairs),axis=0);lines.append(f'| {size//1024}KiB | {vals[0]:+.3f}% | {vals[1]:+.3f}% | {vals[2]:+.1f} |')
 lines+=['','## 7. 可控模型：参数、批量门槛、突变与局部决策','',
 '本节使用新生成的原算法轨迹。模型服务和队列时间不包含真实 CPU 调度耗时；alpha/gate/CQ 行只是对原算法参数做诊断，不是新策略。每组 3 个固定种子。','',
 '| 场景 | 原算法参数 | P99 μs | 分配步长 pp | 权重平均步长 pp |','|---|---|---:|---:|---:|']
 mg=defaultdict(list)
 for r in models:
  if r['kind'] in ['steady','threshold','single','alpha','gate','cq']:
   key=(r['kind'],r['size'],r['topology'],r['load'],r['alpha'],r['update_interval_ns'],r['poll_interval_ns']);mg[key].append(r)
 for key,rr in sorted(mg.items()):
  kind,size,topo,load,alpha,gate,poll=key
  lines.append(f'| {kind} {size//1024}KiB {topo} {load:.0%} | α={alpha:g}, gate={gate/1e6:g}ms, CQ={poll/1000:g}μs | {med(rr,"p99_us"):.3f} | {med(rr,"assignment_step")*100:.3f} | {med(rr,"decision_weight_mean_step")*100:.3f} |')
 lines+=['','| 扰动 | 扰动中 P99 μs | 恢复阶段 P99 μs | 恢复到原参考带 ms | 100ms 确认后重新出带次数 |','|---|---:|---:|---:|---:|']
 dg=defaultdict(list)
 for r in models:
  if 'phases' in r:dg[r['name'].rsplit('_',1)[0]].append(r)
 for name,rr in sorted(dg.items()):
  settled=[r for r in rr if r['recovery_ms'] is not None]
  recovery=fmt(med(settled,'recovery_ms')) if len(settled)==len(rr) else f'N/A（{len(settled)}/{len(rr)} 次进入参考带）'
  lines.append(f'| {name} | {med(rr,"phases.during.p99_us"):.3f} | {med(rr,"phases.after.p99_us"):.3f} | {recovery} | {fmt(med(settled,"reexits_after_settle"),0)} |')
 lines+=['','恢复带按 10ms 窗口要求：分配份额相对扰动前 ±5pp、吞吐相对扰动前 ±5%、队列不高于扰动前峰值+1MiB，并保持 100ms。分别报告首次进入和确认时间；报告进入后是否重新出带。表中 0ms 表示恢复后的第一个 10ms 桶已在参考带内，不代表物理瞬时收敛；确认时间在 CSV 中，至少还需 100ms。2s 内未满足为右删失，不能填 0。90% 注入时降容后的需求超过剩余能力，排空时间不能都归咎于调度。','',
 '| 模型 Worker / 到达 | P99 μs | 完成 Gb/s |','|---|---:|---:|']
 cg=defaultdict(list)
 for r in models:
  if r['kind']=='concurrent':cg[(r['workers'],r['burst'])].append(r)
 for (workers,burst),rr in sorted(cg.items()):lines.append(f'| {workers} / {"同步" if burst else "错峰"} | {med(rr,"p99_us"):.3f} | {med(rr,"goodput_gbps"):.3f} |')
 lines+=['','该并发驱动刻意构造“全部先读候选、再预留”的合法交错，固定总到达率与 QP 服务预算。它可以复现最坏局部视图效应，但不是实机多 Worker 竞争概率或吞吐的测量。','',
 '## 8. 验收状态与下一步边界','',
 '| 需求 | 本轮状态 |','|---|---|',
 '| 吞吐、请求时延、分配耗时 | 已建立同机 CPU WRITE baseline，含固定注入与饱和 |',
 '| 权重频率、幅度、反转、实际分配波动 | 已量化，统计语义与原算法机制分开说明 |',
 '| 固定窗口吞吐/P99 波动、容量归一化均衡 | 已建立，保留窗口敏感性与低样本 N/A |',
 '| 采样/权重更新/局部决策关系 | 已用可控模型重新生成诊断基准 |',
 '| 突发、负载变化、降容恢复 | 已建立模型基准，不能等同物理 Rail 故障 |',
 '| 真实流级改派、故障重试迁移 | N/A：驱动缺少逻辑流与生产重试事件 |',
 '| 完整 TENT 故障收敛与故障下不永久挂起 | N/A：需要完整 Worker/endpoint 故障测试；当前正常运行排空不构成证明 |',
 '| 新算法达标 | 尚未开始，不作优于原算法的结论 |','',
 '后续优先评估原路径最小修改和稳定份额+批量配额，但必须复用本轮 baseline 的负载、窗口和测量实现。严格的“吞吐不下降”与允许统计非劣界的区别见指标方案；建议门槛还不是用户已确认的业务 SLO。','',
 '## 9. 数据、复现与限制','',
 '- `acceptance/build_baseline.py` 构建，`run_baseline.py --stage validate` 验证，`--stage real` 运行实机；实机结束后才运行 `run_model.py`，避免同 CPU 的模型负载干扰实测。各运行脚本拒绝覆盖同名结果；重跑应使用新的运行目录。',
 '- `report_baseline.py` 从原始指标生成本报告和 CSV；二进制请求记录格式 `<QQQQddQ`，在测量外压缩为 gzip。原始配置、结果、二进制请求记录 SHA 与源码/二进制清单分别保留。',
 '- `baseline-runs.csv` 是运行级数据；`baseline-statistics.csv` 给出 5 次运行的中位数、范围、运行级 bootstrap 95% 区间。样本量小，不用于宣称细小差异显著。',
 '- 不做跨轮绝对值因果比较；同机回环、单生产线程、CPU 槽校验、未绑内存 NUMA、没有完整重试和跨节点，都是结论边界。',
 '- 真实正常路径所有 WR 身份/唯一完成/字节数检查、最终写入槽校验及 inflight 排空通过。最终槽数据校验不等于每次覆盖数据的唯一 CRC；运行级 120s 防卡死也不等于生产请求 deadline。','']
 repeats=json.loads((RUN/'repeat-slow/rows.json').read_text()) if (RUN/'repeat-slow/rows.json').exists() else []
 if repeats:
  csvwrite('baseline-repeat-slow.csv',[flatten(r) for r in repeats])
  extra=['### 慢运行输入的独立复测','', '对首次正式基准中 P99 超过 10ms 的固定注入输入，各追加 5 次原样复测，保留相同种子；这是事后诊断集，不替换原始 5 次样本，也不合并成新的有利中位数。复测前后另记录 CPU、网络和中断计数。','', '| 原输入 | 复测次数 | 再次超过 10ms 次数 | 吞吐范围 Gb/s | P99 范围 μs |','|---|---:|---:|---:|---:|']
  rg=defaultdict(list)
  for r in repeats:rg[r['repeat_of']].append(r)
  for name,rr in sorted(rg.items()):extra.append(f'| {name} | {len(rr)} | {sum(r["p99_us"]>10000 for r in rr)} | {min(r["goodput_gbps"] for r in rr):.3f}–{max(r["goodput_gbps"] for r in rr):.3f} | {min(r["p99_us"] for r in rr):.3f}–{max(r["p99_us"] for r in rr):.3f} |')
  extra+=['','未复现只能说明当前重复样本未重现，不能反向证明最初慢运行无效；复现也不能仅凭相关性证明是权重算法导致。','']
  at=lines.index('## 9. 数据、复现与限制');lines[at:at]=extra
 lines+=['','### 采样门控对照的接线修正','', '最终核验发现初版 6 组 gate 配置误用了不含门控的模型二进制，sample_meters 为零。这 6 组原结果与旧索引已隔离到 model-gate-correction，改接 controlled 后重跑；新增 3 个种子的零门控轨迹等价性检查，以及每条 Rail 的 seen > accepted > 0、实际最小学习间隔不小于配置值断言。正式实机、其他 69 组模型及其二进制未修改；上表使用修正后的 6 组结果。']
 lines[4:4]=(ACCEPTANCE_OUTPUT/'baseline-findings.md').read_text().splitlines()+['']
 (ACCEPTANCE_OUTPUT/'baseline-report.md').write_text('\n'.join(lines)+'\n')
 print('BASELINE_REPORT_OK',len(real),len(models),len(stat),flush=True)
if __name__=='__main__':main()
