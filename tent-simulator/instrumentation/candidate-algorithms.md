# 三种候选多轨 RDMA 算法：实现与测试方法

## 代码和接入边界

`algorithms/rail_scheduler.h/.cpp`是可复用C++17模块，提供：

- `allocateLargestRemainder()`：无状态最大余数分配；分数严格排序，返回实际Slice字节。
- `Scheduler(Policy::EarliestFinish)`：带线程安全字节预占的预计完成时间调度。
- `Scheduler(Policy::ByteDeficit)`：慢目标份额、累计字节债务与拥塞覆盖调度。

统一调用是`Scheduler::allocate(candidates, slices, now_ns, traffic_class)`。调用方提供已通过健康、拓扑、优先级和mask过滤的候选；每条候选包含ID、标称带宽、NUMA惩罚和旧分数。实际post成功后调用`posted()`，每次终态调用`completed()`，明确区分是否posted及是否成功。未post取消也要归还预占。一个Scheduler是一个共享预占域；多线程可共用，不自动共享到其他OS进程。

测试适配器保留原DeviceSelector的候选过滤和外层配额，通过生成的源文件调用新模块；没有修改生产TENT源文件或默认策略，也没有把它接入完整Worker/endpoint运行时。`policy_adapter.h`中的注册表只服务于测试进程中的selector生命周期，不能直接当作生产热更新管理器。生产接入需把Scheduler生命周期与selector绑定，并在真实post/所有完成与取消路径上接好反馈。

非legacy策略要求`smart=true`；`smart=false`仅用于legacy的RR基线。

策略选择：`new_policy=legacy/largest_remainder/earliest_finish/byte_deficit`。另保留`new_policy=legacy, allocation_mode=remainder`作为D的旧余数诊断对照。回退通过排空后重建/重新启动为legacy；不声称支持带未完成请求的无缝热切换。

## 三个算法的具体行为

### 最大余数

先按旧分数倒数计算N×p，取floor，剩余片按小数部分分配，每候选至多一片，严格以分数/ID和余数/ID排序。多片调用仍保留每100次一次RR探测。它不修复旧评分的EWMA排队污染或并发快照。

与D不同，新版本按真实尾片字节预占和释放，且排序满足严格弱序，因此不是“仅改余数”对照的逐字复制。完整等长Slice是分析取整影响的主要配对；短尾片结果须同时考虑记账/采样变化。

### 预计完成时间

在同一互斥区内读取本模块预占并分配全部Slice，每片立即增加虚拟/真实逻辑预占。代价（ns）：

```text
(reserved_bytes + slice_bytes) / capacity × 1e9
+ min(2μs, (NUMA_penalty − 1) × slice_bytes / capacity × 1e9)
+ latency_residual
+ latency_residual × max(0, (posted/QP_limit − 0.75)/0.25)^2
```

初版v1把NUMA惩罚乘到全部排队时间，双NUMA仿真中虽减少分配变化，却使1MiB/90%的P99从旧算法约89μs升至205/263μs。完整v1源码、二进制及650组仿真、165组实机结果保存在runs/candidate-algorithms-v1。v2将亲和性改为最多2μs的新Slice偏好，避免扩大既有队列代价；其余控制参数保持不变。

reserved包含已分配未post的数据，不能再加一次相同的软件队列。QP项是资源压力保护项，不解释成精确硬件排队时延。所有QP满时仍返回逻辑分配，由调用方排队等待credit；函数自身不循环等待或丢弃请求。

容量以持续有posted工作的完成流估计：从第一个完成开始计时，达到20μs且完成256KiB后采样完成字节/时长；保留20ms时间常数、标称5%～100%边界和单样本0.5～2倍当前值的限制。v1仅以posted未归零判断持续工作，在有固定CQ延迟时可能把低注入率误当容量。v2额外要求完成样本属于10μs内连续提交的burst，或完成时确实有已预占但未post的软件积压；跨burst且无积压时重新开始采样。新增解析夹具用100G服务、50μs可见延迟、85μs请求间隔检查这一点。

空闲间隔不被当成低带宽，单片孤立完成不更新容量。失败使当前统计窗口失效。并发反馈的时间戳在统计窗口内单调化，避免无符号时间差溢出。

延迟项从忙碌期首个posted WR的完成时间减去估计串行化时间取得，非负并做有界平滑；不是直接叠加整个请求P99。它仍可能包含不可见外部排队与CQ开销，不能称为精确传播时延。测试中第二/第三方案不再执行旧逐完成带宽EWMA更新，实际估计保存在新模块，旧engine EWMA字段保持原先初值。

### 字节债务

目标份额按`capacity/NUMA_penalty`归一化，按traffic_class独立保存。目标最多每1ms更新一次，20ms时间常数，2个百分点滞回，单次TV最多10个百分点。每请求增加`p×request_bytes`的信用，逐片优先选择欠发字节最多的Rail。

若该Rail的预计完成代价超过最小值`max(10μs, 最小值×25%)`，覆盖为最小代价Rail。债务限制在±(2×请求字节+2×最大Slice字节)，候选集合改变时重置该类债务/份额，防止恢复后的无限补偿突发。

第二/第三方案使用1%输入字节预算、最多累计两个最大Slice的探测额度；每次最多强制探测首片，且只选择至少10ms未分配数据的健康候选。它不是固定1%请求探测，也不会制造额外payload。

## 不能隐去的局限

- 容量估计只利用本预占域的可见post/完成信息，无法区分真实链路容量和其他发送方/CPU/CQ形成的共享瓶颈。低负载单片样本不足时保留先验或上次估计，不声称能及时识别所有空闲路径容量变化。
- 预计完成时间方案没有跨请求公平债务；固定ID平局可能让低负载短尾片产生长期微小字节偏置。字节债务方案针对这一点，但拥塞保护可能偏离目标份额。
- 参数在本轮固定，不按场景调参。三方案是组合策略比较，不把收益全部归因于单个机制。
- 尚未完成跨节点、全TENT运行时、真实多进程、长时故障恢复验证。

## 测试矩阵和口径

1. 库单测：取整/尾片、候选数多于Slice数、记账、取消/失败、无候选、QP压力、持续完成流带宽解析值、空闲不降带宽、加权公平和8线程预占/完成。另验证无效完成不破坏已有记账。
2. 集成：5seed legacy与上一轮dynamic程序digest/全部请求延迟完全一致；三候选算法的单Rail、单片、16片和尾片全部排空。
3. 仿真：550组稳态（双Rail、equal/dual_numa、64KiB/999424B/1MiB/16MiB、20/60/90%筛选组合、5策略×5seed），另100组100/1000ms降容与恢复。保持原服务模型、QP预算和固定输入轨迹。
4. 并发：额外175组1/2/4/8真实线程的同步/错峰仿真，固定总180G offered及总QP预算。旧算法强制先读后预占，新调度器仍在同一不利交错下通过自身互斥区协调预占；未把锁耗时注入模拟时间，因此此项不是实机多Worker吞吐验收。计时计数器改为原子操作，旧策略新增5seed并发轨迹一致性检查。
5. 实机：先15组单Rail/双RailRR重新标定，再150组策略对照。1MiB、5策略、equal/dual_numa、标定能力20/60/90%、5seed，随机顺序。每组12288请求，延迟与分配份额剔除前4096请求；比上一轮4096/256运行更长，真正可归因的是本轮同条件的新旧配对，不直接跨轮比较绝对P99。
另增加50组闭环饱和测试，使用相同64槽/128 WR预算，检查极限吞吐是否下降，不能只凭90%固定注入能跟上便宣称吞吐上限无损。

6. 真实驱动仍为DeviceSelector+RDMA-CM/WRITE/CQ，64请求槽、每Rail 128 WR；实际提交和计划到达两套时延并列，记录生成器迟到。最终槽内容检查与每WR完成计数，不是每次覆盖前请求独立CRC。
旧d_*及reference_p字段保留作旧评分的影子量，不应当作新算法权重。新weights分别表示旧评分份额、首片代价倒数或慢目标份额；跨算法主要比较实际字节分配和传输结果。

7. 分配函数耗时为统一插桩的整段allocate墙钟耗时（包含候选/配额及算法调用，可受抢占影响），不是纯CPU周期。另列模块调用耗时。反馈处理成本通过实际吞吐/时延体现，不声称allocate计时包含全部反馈成本。
8. 仿真函数耗时含测试适配开销，不能代替实机成本。单片的正常Rail交替不作为坏震荡；长期Jain与短时分配变化必须一起看。

## 复现

在原实验分支仓库根目录，已有D/E/F基准构建产物后：

```bash
python3 tent-simulator/scripts/build_candidates.py
python3 tent-simulator/scripts/run_candidates.py --stage validate
python3 tent-simulator/scripts/build_candidates.py --sanitize
python3 tent-simulator/scripts/run_candidates.py --sanitize
python3 tent-simulator/scripts/run_candidates.py --stage sim
# 等仿真、编译、消毒器等自有任务全部结束后，单独执行实机组。
python3 tent-simulator/scripts/run_candidates.py --stage real
python3 tent-simulator/scripts/audit_candidates.py
python3 tent-simulator/scripts/report_candidates.py
```

脚本拒绝覆盖既有运行目录。所有配置、结果、stderr和哈希在`/root/mooncake-tent-multirdma-output/runs/candidate-algorithms/`；派生源文件/补丁在`/root/mooncake-tent-multirdma-output/build/`；生产文件不变。完整构建依赖与前置命令参见P1/D/E/F文档。

## 并发对照修正记录

图表复核发现E生成驱动未读取allocation_mode，最初old_remainder实际跑了legacy。修正后重跑全部175组并发场景，并断言5seed的旧余数同步分配确为8/8；另复核5seed旧策略并发轨迹。其他程序重新构建后的二进制哈希完全相同，不重写其测量。旧并发原始数据和证明在runs/candidate-algorithms/concurrent-control-correction。完整重跑时使用已修正build_candidates.py，不需要再次执行一次性的repair_concurrent_control.py。
