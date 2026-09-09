#!/usr/bin/env python3

import sys as _sys
_sys.dont_write_bytecode = True
from experiment_paths import OUTPUT_ROOT
import pathlib,json,csv,statistics,sys,os,math,random
ROOT=pathlib.Path(__file__).resolve().parents[1];run=OUTPUT_ROOT/'runs/experiment-ef';out=OUTPUT_ROOT/'reports'
sys.path.insert(0,str(OUTPUT_ROOT/'build/plot-deps'));os.environ['MPLCONFIGDIR']=str(OUTPUT_ROOT/'build/mplconfig')
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

def med(rr,key):return statistics.median(r[key] for r in rr)
def csvout(name,rows):
 with (out/name).open('w',newline='') as f:
  w=csv.DictWriter(f,fieldnames=list(rows[0]),lineterminator='\n');w.writeheader();w.writerows(rows)
def main():
 e=json.loads((run/'E/summary.json').read_text())['rows'];f=json.loads((run/'F/summary.json').read_text())['rows'];real=json.loads((run/'real/summary.json').read_text());replay=json.loads((run/'replay/summary.json').read_text());audit=json.loads((run/'audit.json').read_text());gpu=json.loads((run/'gpu/summary.json').read_text())
 # Derive measured per-rail balance and request-completion throughput windows.
 caps={mask:med([r for r in real['rows'] if r['policy']=='calibration' and r['mask']==mask],'goodput_gbps') for mask in [1,2]}
 for r in real['rows']:
  d=json.loads((run/'real'/(r['name']+'.json')).read_text());rate=[x*8/d['elapsed_ns'] for x in d['completion_bytes']];u=[rate[0]/caps[1],rate[1]/caps[2]]
  r['rail0_gbps']=rate[0];r['rail1_gbps']=rate[1];r['capacity_normalized_jain']=sum(u)**2/(2*sum(x*x for x in u))
  start=math.ceil(d['submitted_ns'][256]/10000000);stop=max(d['finished_ns'])//10000000;bins=[0]*(stop-start)
  for t in d['finished_ns']:
   k=t//10000000
   if start<=k<stop:bins[k-start]+=1048576
  r['completion_goodput_cv_10ms']=statistics.pstdev(bins)/statistics.mean(bins) if len(bins)>=20 and statistics.mean(bins)>0 else None
 csvout('experiment-e-metrics.csv',e)
 ff=[]
 for r in f:
  for phase,values in r['phases'].items():ff.append({**{k:v for k,v in r.items() if k!='phases'},'phase':phase,**values})
 csvout('experiment-f-metrics.csv',ff);csvout('experiment-real-metrics.csv',[{k:v for k,v in r.items() if k!='devices'} for r in real['rows']]);csvout('experiment-replay-metrics.csv',replay['rows'])
 # Independent sustained-drop reference for finite-load pulses. No completion
 # timestamps are fed back into a policy; this is post-run analysis only.
 settling=[]
 held={(r['load'],r['policy'],r['seed']):r for r in f if r['kind']=='held'}
 for r in f:
  if r['kind']!='capacity' or r['load']>=.75:continue
  target=held[(r['load'],r['policy'],r['seed'])]['phases']['after']['mean_share0'];raw=json.loads((run/'F'/(r['name']+'.json')).read_text());on=raw['config']['phase_on_ns'];off=raw['config']['phase_off_ns'];ww=[w for w in raw['windows'] if on<=w['start_ns']<off]
  fractions=[w['assigned'][0]/sum(w['assigned']) for w in ww if sum(w['assigned'])];good=[abs(x-target)<=.05 for x in fractions];first=next((i+1 for i in range(len(good)-9) if all(good[i:i+10])),None)
  direction=1 if target>r['phases']['before']['mean_share0'] else -1
  settling.append({'name':r['name'],'load':r['load'],'policy':r['policy'],'seed':r['seed'],'duration_ms':r['duration_ms'],'drop_reference_share0':target,'drop_settle_ms':first,'directional_overshoot_pp':max([0]+[(x-target)*direction*100 for x in fractions]),'recovery_ms':r['mean_share_recovery_ms'],'baseline_band_qualifies':r['baseline_recovery_band_qualifies']})
 csvout('experiment-f-settling.csv',settling)
 # Matched hardware run-level differences, not individual-WR bootstrap.
 effects=[];rng=random.Random(9095)
 for topo in ['equal','dual_numa']:
  for load in [.2,.6,.9]:
   rr=[r for r in real['rows'] if r['policy']!='calibration' and r['topology']==topo and r['offered_fraction']==load]
   for metric in ['goodput_gbps','scheduled_p99_us','service_p99_us','allocation_step']:
    a={r['seed']:r for r in rr if r['policy']=='original'};b={r['seed']:r for r in rr if r['policy']=='remainder'}
    values=[b[s][metric]-a[s][metric] for s in a];boot=sorted(statistics.median(rng.choices(values,k=5)) for _ in range(4000))
    effects.append({'topology':topo,'load':load,'metric':metric,'paired_median_delta':statistics.median(values),'ci95_low':boot[99],'ci95_high':boot[3899]})
 csvout('experiment-real-effects.csv',effects)
 plt.rcParams.update({'svg.fonttype':'none','font.size':10});fig,axes=plt.subplots(2,2,figsize=(13,8.5),layout='constrained')
 for burst,serial,label in [(True,False,'Synchronous / concurrent'),(True,True,'Synchronous / serialized'),(False,True,'Staggered')]:
  xx=[];yy=[]
  for w in [1,2,4,8]:
   rr=[r for r in e if r['processes']==1 and r['workers']==w and r['size']==1048576 and r['load']==.9 and r['burst']==(True if w==1 else burst) and r['serialize']==(False if w==1 else serial)]
   xx.append(w);yy.append(med(rr,'p99_us'))
  axes[0,0].plot(xx,yy,'-o',label=label)
 axes[0,0].set(title='E: same total load, 1 MiB, 90%',xlabel='Workers (one state domain)',ylabel='Simulated P99 (us)');axes[0,0].legend(fontsize=8)
 for policy in ['original','alpha99','alpha999','remainder']:
  rr=[r for r in f if r['kind']=='capacity' and r['load']==.6 and r['policy']==policy]
  xx=[1,10,100,1000];yy=[statistics.median(r['phases']['during']['p99_us'] for r in rr if r['duration_ms']==x) for x in xx]
  axes[0,1].plot(xx,yy,'-o',label=policy)
 axes[0,1].set(xscale='log',title='F: one rail halves, 60% offered load',xlabel='Drop duration (ms)',ylabel='During-phase empirical P99 (us)');axes[0,1].legend(fontsize=8)
 for policy in ['original','remainder']:
  xx=[20,60,90];yy=[med([r for r in real['rows'] if r['topology']=='dual_numa' and r['policy']==policy and r['offered_fraction']==x/100],'scheduled_p99_us') for x in xx]
  axes[1,0].plot(xx,yy,'-o',label=policy)
 axes[1,0].set(title='Same-host real RDMA, NUMA scoring',xlabel='Fraction of calibrated injection rate (%)',ylabel='Scheduled-arrival P99 (us)',yscale='log');axes[1,0].legend(fontsize=8)
 rp=[r for r in replay['rows'] if r['topology']=='dual_numa'];labels=[f"{r['policy']} {int(r['load']*100)}%" for r in rp]
 axes[1,1].bar(range(len(rp)),[r['ratio_to_scheduled'] for r in rp]);axes[1,1].axhline(1,color='grey',linestyle='--');axes[1,1].set_xticks(range(len(rp)),labels,rotation=30,ha='right');axes[1,1].set(title='Calibrated model / real scheduled P99',ylabel='Ratio (seed 1 replay)')
 for ax in axes.flat:ax.grid(alpha=.2)
 fig.suptitle('E/F and same-host RDMA | Different evidence levels, not interchangeable')
 fig.savefig(out/'experiment-ef-overview.svg');plt.close(fig);p=out/'experiment-ef-overview.svg';p.write_text('\n'.join(x.rstrip() for x in p.read_text().splitlines())+'\n')
 lines=['# 多轨 RDMA 基线与归因：总体总结及优化算法建议','',
 '> 2026-09-08；A10 `/root/mooncake`，分支 `experiment/rdma-multirail-baseline`。生产算法基线 `1c65ced8`，A～D后补充E/F、同机真实RDMA与回放。','',
 '## 结论与完成范围','',
 '**现有算法在固定外部条件下可出现持续、有界的权重和分片分配往返。问题不是仅由EWMA更新过快引起：整数余数处理、批量快照、并发预占可见性、CQ反馈及负载变化共同影响结果。单改学习率、单关探测、单共享状态都不能作为通用修复。**','',
 '- D的等容量、等亲和1MiB仿真中，仅改变余数分配就消除了15/1与1/15附近的大幅往返，90%负载P99从80.634降至43.933μs，吞吐仍约180Gb/s。这是强归因证据，不是生产算法全面验收。',
 '- E表明“共享状态”和“读状态—选择—预占的协调”是两件事。真实线程的不利交错能放大尾时延；共享视图而不协调，部分场景反而更差。',
 '- F区分了短期敏感性与真实容量不足：90%负载在一条Rail减半后超过剩余容量，恢复后约1.5秒的积压消退符合容量关系，不能全部归咎于调度震荡。',
 f"- 同机短时实测的收益明显小于理想仿真：等评分亲和90%下，原算法分配步长约{100*med([r for r in real['rows'] if r['topology']=='equal' and r['offered_fraction']==.9 and r['policy']=='original'],'allocation_step'):.2f}pp，最大余数约{100*med([r for r in real['rows'] if r['topology']=='equal' and r['offered_fraction']==.9 and r['policy']=='remainder'],'allocation_step'):.2f}pp；计划到达P99约{med([r for r in real['rows'] if r['topology']=='equal' and r['offered_fraction']==.9 and r['policy']=='original'],'scheduled_p99_us'):.0f}→{med([r for r in real['rows'] if r['topology']=='equal' and r['offered_fraction']==.9 and r['policy']=='remainder'],'scheduled_p99_us'):.0f}μs。它没有消除实际高负载排队。真实均值和运行级区间见后文，不能直接宣称取得仿真的45.5%收益。",
 '- 本次首次加入实际RDMA数据路径：4组DRAM CM连接/WRITE校验、16组NIC×GPU写入及CUDA读回、75组同机短时标定与策略对照、10组低负载延迟标定、12组计划到达轨迹回放。真实结果与理想模型分开呈现，不能从仿真直接推断实机收益。','',
 '**完成的是当前单台A10可执行的基线与归因工作。跨节点、多发送方真实网络、完整TENT Worker/endpoint集成、长时间稳定性验收仍未完成。已询问可用对端，当前未提供；不能将同机两端点冒充跨节点。**','',
 '| 阶段 | 本次状态与证据 |','|---|---|',
 '| P0/P1 | 既有代码/硬件冻结、观测与仿真一致性、记账/终态校验；稀疏观测开销结论只适用于既有CPU测试 |',
 '| A/B/C/D | 已完成；原始稳态、EWMA增益、学习/CQ周期、离散分配与探测 |',
 '| E | 560组闭环；真实OS线程调用算法，逻辑进程视图与传输服务为模拟 |',
 '| F | 380组闭环；负载脉冲、降容脉冲、持续降容参考及恢复 |',
 '| P5 | 同机真实WRITE/完成/内容校验，短时性能与描述性模型校准；未运行完整TENT运行时 |',
 '| P6 | 缺少额外RDMA对端，跨节点与三节点多发送方实验未执行 |','',
 '## 1. 测的是哪层算法','',
 '真实DeviceSelector按照本地NIC状态选路，本文“Rail权重”指该层的归一化派生份额，不是端到端(local NIC, remote NIC)映射的持久全局权重。RailMonitor、失败重试和远端映射没有在A～F闭环中执行。','',
 '```text\nscore_i = (inflight_bytes_i + slice_bytes) / EWMA_BW_i × NUMA_penalty_i + jitter\np_i ∝ 1 / (score_i + epsilon)\nBW_new = alpha × BW_old + (1−alpha) × (charged_bytes / post_to_CQ_time)\n```','',
 '默认旧值alpha=0.01，新样本占99%；带宽有标称值0.1～10倍的裁剪。inflight在分配时预占、完成时释放，包含尚未post的已分配工作。不能再把同一软件排队字节简单重复加一次。','',
 'CPU WRITE最大32片，实际片数达到16才预先批量分配；较小请求逐Slice选择。尾片合并也影响门槛。CPU READ及完整CUDA业务路径未覆盖，不能将本报告推广为所有opcode/内存类型的验收。批量路径采用比例floor，余数全部给当前最优NIC，每第100次多片调用RR探测。','',
 '## 2. A～D建立的证据链','',
 '| 因素 | 已验证关系 | 不能推出的结论 |','|---|---|---|',
 '| 恒定外部条件 | 双Rail、1MiB：20/60%常见6/10↔10/6；90%常见15/1↔1/15；长期均值仍接近50/50 | 不是队列无界发散，也不是所有路径切换有害 |',
 '| EWMA增益 B | IID无裁剪输入方差比吻合(1−alpha)/(1+alpha)；闭环alpha=.5在部分低负载场景更差，高alpha也未消除高负载往返 | 平滑估计器不等于平滑分配 |',
 '| 学习频率 C | 固定alpha降低更新频率改变时间记忆和样本选择，90%下1ms/10ms门控P99可升至约104.98/112.62μs | 不能把更新周期当成独立稳定旋钮 |',
 '| 观测频率 C | 相同轨迹仅换10μs/10ms观测窗口，TV/s相差约950倍，P99不变 | 日志变平不等于算法改善 |',
 '| 余数 D | 49/51权重、16片产生7/9；反向变为9/7，2pp输入变化放大为12.5pp输出变化；仅改余数可移除主复现 | 整数/奇数/尾片分配不可能普遍连续，8/8片数也未必是8/8字节 |',
 '| 探测 D | 关闭后双Rail主复现仍存在；1%调用可对应13.91%或0.063%的请求字节，取决于大小相位 | 探测不是固定1%字节流量，也不是额外重传流量 |',
 '| 固定带宽 D | alpha=1时，90%原分配仍有约85.57pp平均步长；完全对称且jitter=0时又可保持8/8 | BW学习不是该复现必要条件；理想对称初态稳定不代表抗扰稳健 |','',
 '必须区分三种时钟：指标导出窗口只改变观测；带宽学习随成功完成/CQ事件发生；权重在请求或Slice决策时重算，并立即读取最新在途量。C只节流带宽学习，并没有低频发布或冻结权重。固定alpha的墙钟记忆近似tau=−1/(完成事件率×ln(alpha))；闭环中事件率本身又受分配影响。IID方差关系只用于独立估计器校验，不直接预测闭环P99。','',
 '补充静态检查确认：buildCandidates中的比较器把score差小于jitter时按dev_id排序，可能产生a<b、b<c但a<c不成立。用原比较器及10μs、10μs+0.75ns、10μs+1.5ns的三个构造分数已复现严格弱序违反。未证明既有四Rail轨迹实际遇到了这些分数；双NIC主实验不涉及这个三候选反例。扩展到多候选前，应改为严格的排序键，把滞回/近似等价放在排序之外。','',
 '对需求中的“负载失衡”要分清时间尺度。A的等容量双Rail长期Jain已经接近1；主要问题是短时分配往返及请求P99水平升高。A双NUMA中RR只用首个亲和层级，不能把其单Rail容量限制全部算作EWMA问题。','',
 '## 3. E：局部决策与并发','',
 '每组固定总负载、总双Rail容量和每Rail 128个QP credit。一个进程内共享selector；不同逻辑进程使用独立selector，共享视图对照改为同一selector。真实线程在候选计算之后设置屏障，构造“全部先读、然后预占”的合法不利交错；这是风险复现，不是实际发生频率测量。模型不在锁/决策时推进传输时间，所以没有把CPU开销降低注入速率当成收益。','',
 '| 逻辑进程×Worker | 视图 | 到达/执行 | 1MiB、90% P99 μs | 客户端内分配步长 pp | 吞吐 Gb/s |',
 '|---|---|---|---:|---:|---:|']
 for pp,ww in [(1,1),(1,8),(4,1),(2,4)]:
  for shared in ([False] if pp==1 else [False,True]):
   for burst,serial in ([(True,False)] if pp*ww==1 else [(True,False),(True,True),(False,True)]):
    rr=[r for r in e if r['processes']==pp and r['workers']==ww and r['shared']==shared and r['burst']==burst and r['serialize']==serial and r['size']==1048576 and r['load']==.9]
    lines.append(f"| {pp}×{ww} | {'全局共享' if shared else '进程内共享'} | {'同步/串行' if burst and serial else '同步/并发' if burst else '错峰'} | {med(rr,'p99_us'):.3f} | {100*med(rr,'allocation_step'):.3f} | {med(rr,'goodput_gbps'):.3f} |")
 lines+=['','1进程8Worker的同形状同步突发，串行预占将P99从约673降至343μs；错峰约81μs。8个1MiB请求本身就需要约336μs的双100G服务时间，所以不能把全部突发延迟归因于协调缺陷。4逻辑进程同步时，共享视图但不协调可比私有视图更差；共享也改变EWMA汇聚与反馈相位，不能把变化全部归因于单一在途计数。','',
 '64KiB单片也出现对应现象：1进程8Worker、90%同步并发P99约43.864μs，串行约22.936μs，错峰约11.272μs。E的步长按每个客户端自身连续请求统计，与A/D的全局相邻请求口径不同，不直接横比数值。','',
 '## 4. F：真实变化与短期扰动','',
 '负载30%→90%→30%，或一Rail100G→50G→100G；扰动1/10/100/1000ms，5seed，并有独立持续降容参考。改变策略后重新生成CQ完成，旧策略完成时间从未作为输入。','',
 '| 场景 | 策略 | 扰动前P99 μs | 扰动中P99 μs | 恢复阶段P99 μs | 队列恢复 ms |',
 '|---|---|---:|---:|---:|---:|']
 for kind,load,dt in [('load',.3,1000),('capacity',.6,100),('capacity',.9,1000)]:
  for policy in ['original','alpha99','alpha999','remainder']:
   rr=[r for r in f if r['kind']==kind and r['load']==load and r['duration_ms']==dt and r['policy']==policy]
   vals=[statistics.median(r['phases'][p]['p99_us'] for r in rr) for p in ['before','during','after']];q=[r['queue_recovery_ms'] for r in rr if r['queue_recovery_ms'] is not None]
   lines.append(f"| {kind}/{load:.0%}/{dt}ms | {policy} | {vals[0]:.3f} | {vals[1]:.3f} | {vals[2]:.3f} | {statistics.median(q) if q else 'NA'} |")
 lines+=['','队列恢复定义为总队列回到扰动前最大值+1MiB以内并保持10个1ms窗口；份额恢复另按±5个百分点/10窗口判定，最小可报告1ms，且检查参考本身能否满足判据。NA不自动代表挂起。短脉冲的样本不足1000时，经验P99不作为精确尾分布估计，原始CSV保留样本数及峰值。','',
 '90%负载为180G，降容后的合计能力150G。持续1秒累积约30Gb，恢复后剩余处理能力200−180=20G，物理排空量级为30/20=1.5秒，与约1501ms结果一致。此条件不存在有限队列的降容稳态；不能以强平滑或权重冻结解决容量不足。','',
 '最大余数的稳态收益不保证所有动态场景最佳：60%降容100ms时，其扰动中P99约138μs，低于原算法约148μs，但高于两种强平滑约127～128μs。90%过载降容时几种策略均出现大积压，最大余数也未消除此代价。','',
 '## 5. 同机真实RDMA、GPU与模型误差','',
 '手工verbs QP配置在RTR阶段返回EINVAL；随后RDMA-CM正确建连成功，说明不能把前者当作RDMA不支持。erdma_0/1各自回环及交叉四组DRAM WRITE全部通过。8张A10×两NIC的16组host→GPU 64KiB WRITE及CUDA读回全部通过；这不覆盖GPU→host性能、长时故障或完整TENT CUDA业务。','',
 '真实性能程序直接调用DeviceSelector、RDMA-CM、ibv_post_send/ibv_poll_cq。两卡注册同一源缓冲区；请求64槽，每Rail最多128个posted WR，每WR有成功CQ与字节检查，最后验证所有曾写入槽片的最终内容。完整TENT运行时未执行，payload不走SHM或CUDA IPC。','',
 '| 标定 | 短时goodput中位数 Gb/s |','|---|---:|']
 for mask in [1,2,3]:lines.append(f"| {'erdma_0' if mask==1 else 'erdma_1' if mask==2 else '双Rail RR'} | {med([r for r in real['rows'] if r['policy']=='calibration' and r['mask']==mask],'goodput_gbps'):.3f} |")
 lines+=['',f"以双Rail标定中位数{real['calibrated_dual_Gbps']:.3f}Gb/s的20/60/90%设定固定计划到达。该标定包含CPU8、同机回环、软件提交与当前共享主机的限制，不是两张100G端口跨节点能力的证明。每组4096个1MiB请求，延迟剔除前256请求，5次重复，运行顺序固定种子打乱。",'',
 '| 评分亲和 | 注入比例 | 策略 | 实际goodput Gb/s | 计划到达P99 μs | 实际提交P99 μs | 分配步长 pp |',
 '|---|---:|---|---:|---:|---:|---:|']
 for topo in ['equal','dual_numa']:
  for load in [.2,.6,.9]:
   for policy in ['original','remainder']:
    rr=[r for r in real['rows'] if r['topology']==topo and r['offered_fraction']==load and r['policy']==policy]
    lines.append(f"| {topo} | {load:.0%} | {policy} | {med(rr,'goodput_gbps'):.3f} | {med(rr,'scheduled_p99_us'):.3f} | {med(rr,'service_p99_us'):.3f} | {100*med(rr,'allocation_step'):.3f} |")
 lines+=['','| 评分亲和/注入/策略 | 归一化Jain | 请求完成吞吐CV（10ms） | P99群组CV |','|---|---:|---:|---:|']
 for topo in ['equal','dual_numa']:
  for load in [.2,.6,.9]:
   for policy in ['original','remainder']:
    rr=[r for r in real['rows'] if r['topology']==topo and r['offered_fraction']==load and r['policy']==policy];vv=[r['completion_goodput_cv_10ms'] for r in rr if r['completion_goodput_cv_10ms'] is not None]
    lines.append(f"| {topo}/{load:.0%}/{policy} | {med(rr,'capacity_normalized_jain'):.5f} | {statistics.median(vv) if vv else '样本窗口不足20'} | {med(rr,'p99_cohort_cv'):.5f} |")
 lines+=['','| 评分亲和/注入 | 余数−原算法：计划P99差 μs（95%区间） | 分配步长差 pp（95%区间） |','|---|---:|---:|']
 for topo in ['equal','dual_numa']:
  for load in [.2,.6,.9]:
   ee={r['metric']:r for r in effects if r['topology']==topo and r['load']==load};a=ee['scheduled_p99_us'];b=ee['allocation_step']
   lines.append(f"| {topo}/{load:.0%} | {a['paired_median_delta']:.3f} [{a['ci95_low']:.3f}, {a['ci95_high']:.3f}] | {100*b['paired_median_delta']:.3f} [{100*b['ci95_low']:.3f}, {100*b['ci95_high']:.3f}] |")
 lines+=['','Jain按独立单Rail短时标定归一化，只作相对均衡参考，不能证明两个Rail没有共享瓶颈。吞吐CV按请求完成统计的10ms完整窗口；尾时延CV按200请求群组。NUMA亲和有意造成的长期份额不均不能直接判为算法缺陷。','',
 'equal是人为设置相同评分亲和的对照；dual_numa使用该CPU源位置对应的NUMA软惩罚。不能把物理两NUMA描述为真正等距离链路。计划到达P99包含生成器/环形槽背压造成的迟到；实际提交P99不包含这段迟到。两者差异大时，必须同时看注入迟到，不能只报更好看的提交后时延。','',
 '每WR完成和最终槽内容均校验，但槽会复用，因此不是逐请求独立CRC的完整端到端验收。每组为短时测量，共享主机而非独占硬件；5次运行级配对差值及bootstrap区间保存在experiment-real-effects.csv。首次性能运行可能与GPU探测重叠，已整体归档至real-overlap-gpu；正式75组在自有探测结束后完整重跑，未挑选单次有利结果。','',
 '描述性校准使用独立单Rail/双Rail标定和低负载64KiB延迟样本，按合计能力缩放模拟容量，再回放seed=1的12组计划到达轨迹。模拟输入不包含实测完成时间；这不是对任意共享瓶颈的准确模型。','',
 '| 评分亲和/注入/策略 | 校准模型P99 μs | 实际计划到达P99 μs | 模型/实测 |',
 '|---|---:|---:|---:|']
 for r in replay['rows']:lines.append(f"| {r['topology']}/{r['load']:.0%}/{r['policy']} | {r['model_p99_us']:.3f} | {r['real_scheduled_p99_us']:.3f} | {r['ratio_to_scheduled']:.3f} |")
 lines+=['','模型不包含完整CPU执行、真实CQ批次、硬件队列公平性、共享带宽瓶颈和主机抖动；校准后的误差用于限定模型用途。适合做可重复归因与筛选，不能作为真实P99的直接预测器。','',
 '## 6. 对需求第一部分的验收判断','',
 '| 要求 | 当前证据 | 判断 |','|---|---|---|',
 '| 基线与可复现 | 真实源码冻结、配置/seed/哈希、A～F和同机实际数据 | 单机范围完成 |',
 '| 权重/流量震荡 | A/D持续有界往返；B/C灵敏度与观测口径；E并发交错 | 已复现并量化，不能等同系统发散 |',
 '| 负载失衡 | 短时集中明显；长期Jain常已接近1；双NUMA策略候选范围差异明确 | 短时与长期分开，未普遍复现长期严重失衡 |',
 '| 频繁迁移 | 首片换路/分配变化已计数；当前场景无故障重试改派 | 未复现实际Slice频繁迁移，不把正常喷洒当迁移 |',
 '| 尾时延波动 | A主要是P99水平升高；C门控可增加P99群组CV；E/F和真实短时数据另有量化 | 不能用P99水平代替P99波动 |',
 '| 采样/权重/局部决策关系 | 独立噪声、节流、观测、余数、探测、冻结BW、并发预占等配对 | 支持多因素闭环关系，未证明单一根因覆盖全部场景 |',
 '| 数据与终态 | 模型守恒/排空，真实WRITE CQ和最终内容检查 | 仅限已执行路径，不证明完整生产无永久挂起 |',
 '| 跨节点适用性 | 当前未有额外端点 | 未完成，不能总体盖章为全场景验收通过 |','',
 '## 7. 三种优化算法建议','',
 '### 方案一：最大余数分配，并逐步改为字节配额','',
 '保留候选、评分和EWMA。先计算q_i=N×p_i，分配floor(q_i)，剩余片按q_i的小数部分由大到小各给一片；不要把全部余数都给最优NIC。尾片以实际字节评估误差，下一阶段再决定是否引入跨请求余量。','',
 '- 证据：D和F中的最大余数模式已实现并测试；真实同机对照见上表。它是最小改动候选，不等于已验证所有拓扑/动态场景都更优。',
 '- 优点：改动集中、回退简单，直接针对已证实的离散放大。',
 '- 限制：不修复估计偏差、批间并发快照或跨进程不可见性；奇数片仍会变化，长期公平性可能需要累计字节债务。',
 '- 下一步验收：真实长时配对、异构容量、混合大小、CPU READ、尾片、QoS/故障过滤；同时检查吞吐、P99与CPU成本。','',
 '### 方案二：基于预计完成时间的逐片预占调度','',
 '对每个候选维护可解释的有效服务能力C_i、已预占字节R_i、基础/历史完成延迟L_i和QP占用Q_i。选择预计完成代价最小的Rail，每分配一片立即更新本批虚拟预占；提交时保证读取与预占协调。','',
 '```text\nT_i = NUMA_penalty_i × (R_i + slice_bytes) / C_i\n      + L_i + qp_pressure_cost(Q_i)\nchoose argmin(T_i)\nR_chosen += actual_slice_bytes\n```','',
 'C_i优先由持续有待发送工作时的完成字节/有效忙碌时间窗口估计；低负载时不要把低注入率当链路退化。用有界鲁棒平滑处理样本，完成延迟与容量估计分开，避免post→CQ的排队污染同时被带宽和R_i重复放大。不能再次把R_i已经包含的软件排队字节重复相加。','',
 '- 证据：D逐片刷新/固定快照对照和E协调对照支持批内/批间预占的重要性；上述完整C_i估计器与QP代价尚未实现或验证。',
 '- 实现选择：短临界区完成“读—选择—预占”，或版本校验后提交；必须测竞争成本。单个atomic加减并不保证整个决策过程原子。',
 '- 风险：逐片重算成本、锁竞争、历史容量在突变时滞后；健康状态失败应即时剔除，不能等待平滑收敛。','',
 '### 方案三：带字节债务和拥塞覆盖规则的稳定加权调度','',
 '慢层由拓扑、可信容量、历史完成延迟生成目标份额p_i；快层维护累计字节债务d_i。每个请求到达时d_i+=p_i×request_bytes，依次把Slice分给债务最大的健康候选并扣除实际字节。这样通过多个请求补偿整数误差，不在每次请求上丢掉小数。','',
 '```text\nd_i += p_i × request_bytes\nfor each slice:\n    candidates = healthy rails satisfying congestion guard\n    i = argmax(d_i among candidates)\n    assign slice to i; d_i -= actual_slice_bytes\n```','',
 '慢层使用时间常数平滑、滞回及份额变化预算；快层在排空时间/QP压力超过阈值时覆盖目标份额，防止稳定目标持续向拥塞路径注入。债务必须限幅，失效Rail清零或冻结，恢复时限制补偿突发。探测采用字节/时间预算，避免“每百次请求”被请求大小相位放大。','',
 '- 证据：D支持保留整数余量的方向，C支持区分慢学习与快决策；完整债务/滞回组合尚未测试。',
 '- 优点：长期按字节公平，可限制目标份额的动作频率；小请求正常轮换无需被惩罚。',
 '- 风险：债务累积、目标滞后和拥塞覆盖冲突；必须防止为了曲线平滑牺牲吞吐、排空时间或故障响应。','',
 '三者都应保留原策略和稳定RR/固定容量策略作为快速回退，但回退的候选范围必须一致且只包含健康Rail，不能因切换到单NUMA RR而悄悄丢掉一半可用容量。不将已post Slice做正常迁移；失败重试另行记账与限次。','',
 '建议先评估方案一的真实收益与代价，再用方案二作为较完整的队列感知候选、方案三作为稳定份额/长期公平候选。不要只调alpha便宣布解决；也不要一次叠加所有机制而失去归因。','',
 '## 8. 验证、复现与待补项','',
 f"- E/F共有940组正式模拟场景。模拟功能检查15/15、ASan/UBSan15/15、P0/P1回归30/30；E/F运行各包含同一组20个验证进程。配置/结果校验对数：{audit['case_pairs']}。",
 '- 已核验源码和二进制哈希、模型字节守恒/记账排空、真实CQ/字节和内容检查；生产TENT及公共P1/D driver未修改。',
 '- 新文件均在experiments/rdma-multirail-baseline；原始数据在runs/experiment-ef，完整指标CSV与图表在reports。详细复现见instrumentation/experiment-ef.md。',
 '- 下一步真实验收需要另一台双RDMA对端；多发送方需至少三个节点。之后运行完整TENT传输、长时3–5分钟配对、真实队列/QP压力和故障重试，才能关闭当前外部验证缺口。','',
 '![E/F及同机真实RDMA概览](experiment-ef-overview.svg)','',
 '已有详细报告：experiment-a-report.md、experiment-b-report.md、experiment-c-report.md、experiment-d-report.md。本文覆盖其结论与新增证据，不将任何模拟值冒充网卡实测值。']
 (out/'rdma-multirail-baseline-summary.md').write_text('\n'.join(lines)+'\n')
 compact={'E_matrix':560,'F_matrix':380,'real_cases':75,'real_capacity':real['calibrated_dual_Gbps'],'hardware_pairs':4,'gpu_pairs':16,'replay_cases':replay['cases'],'audit':audit,'real_grouped':[]}
 for topo in ['equal','dual_numa']:
  for load in [.2,.6,.9]:
   for policy in ['original','remainder']:
    rr=[r for r in real['rows'] if r['topology']==topo and r['offered_fraction']==load and r['policy']==policy];compact['real_grouped'].append({'topology':topo,'load':load,'policy':policy,**{k:med(rr,k) for k in ['goodput_gbps','scheduled_p99_us','service_p99_us','allocation_step']}})
 (run/'conclusions.json').write_text(json.dumps(compact,indent=2)+'\n');print('EF_REPORT_READY',len(lines),flush=True)
if __name__=='__main__':main()
