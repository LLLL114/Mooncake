# E/F 与单机真实 RDMA：方法与复现

## 范围

在原实验分支5f383e8e之后增加E/F，不修改生产TENT、P1/C公共driver和D算法源码。build_experiment_ef.py根据精确锚点生成两个新程序：concurrency和dynamic。新的真实程序通过RDMA-CM建立端点，直接使用真实DeviceSelector和ibv_post_send/ibv_poll_cq，不运行完整TENT Worker/endpoint生命周期。

E：560组闭环，1/2/4/8个真实OS线程Worker；1/2/4个逻辑进程状态域。进程隔离以独立DeviceSelector实例模拟，不是独立OS进程或真实IPC。P×W组合为(1,1)/(1,2)/(1,4)/(1,8)/(2,1)/(4,1)/(2,4)。同进程共用selector；共享视图干预让全部逻辑进程共用selector，包括在途计数和EWMA，不能归因成只改在途可见性。

- 同步突发与均匀错峰；同总offered load 60/90%；64KiB单片、1MiB批量；5seed；4096请求/1024预热。
- 所有状态域共用两条100G模拟链路、每Rail总QP credit=128、FIFO服务和1μs CQ，不随Worker/进程增加总资源。
- 并发组在真实buildCandidates结束处设置屏障：全部活动线程读完候选才允许任何线程预占。是可重复的合法不利交错，不是自然发生频率采样。
- 串行组按客户端编号执行完整allocate；模拟时间不在决策期间推进，所以比较固定到达轨迹和固定注入速率，不包含实际锁/线程调度CPU成本。
- 同步突发一次注入P×W个请求，总间隔同步扩大；错峰每次仅一个请求。同形状内的串行/并发比较用于归因；不能把突发本身的物理排队全部归因于锁。
- 权重/分配变化按每个客户端自身的连续请求计算，不能与A/D全局相邻请求指标直接对比。共享QP模型不含独立QP公平调度、进程调度成本和跨节点拥塞。

F：380组闭环，原算法、alpha=.99、alpha=.999、D最大余数诊断，5seed。

- 30%→90%→30%负载脉冲；单Rail容量100G→50G→100G，原负载20/60/90%。持续1/10/100/1000ms；额外持续降容参考组。
- 扰动始于0.5s，预热0.2s，恢复后观察2s。最大alpha=.999的事件记忆通过实际完成驱动；不是固定墙钟tau。
- 到达时间显式写入配置；策略变化重新产生传输和完成事件，不重放旧完成时间。降容作用于已开始传输WR剩余字节，沿用B/C的积分模型。
- 恢复份额：1ms窗口，进入独立扰动前参考份额±5个百分点，维持10个窗口；入带时间记第一个完整窗口结束，最小1ms。另检查扰动前自身是否可满足此条件；不满足时不把NA解读为未恢复。
- 队列恢复：总队列≤扰动前最大队列+1MiB，保持10×1ms。与份额恢复分开。held组不报告恢复到原状态；高负载降容后180G>150G，不存在有限队列稳态。
- P99按阶段到达请求统计，包含排空完成；短阶段不足1000请求的经验分位数仅作描述，重点看队列峰值/峰值延迟。原deadline只计数、不取消；不证明完整生产请求永不挂起。

## 真实数据路径

1. verbs_probe在两卡尝试手工RC QP配置，RTR返回EINVAL。这是该配置失败，不能据此判断RDMA不可用。
2. cm_probe经RDMA-CM检查两卡各自回环及交叉，共4组64KiB DRAM WRITE及接收内容校验。仅绑定A10的10.0.1.243/244；不修改网卡、路由、驱动或SSH管理链路。
3. real_rdma使用上述真实端点及DeviceSelector，64个请求环形槽，两张卡注册同一源缓冲区，独立目标缓冲区；每Rail最多128个posted WR。每WR signaled并核对身份、状态、计数及字节；测量结束验证所有曾写入槽片的最终内容。不是每个覆盖前请求的独立CRC校验。
4. 单卡/双卡各5次closed-loop短时标定（4096×1MiB），RR+等评分亲和允许使用指定全部候选。双卡中位数作为固定注入率基准；它是该主机/CPU/环回实现的端到端能力，不是网卡标称/跨节点可用带宽。
5. 等评分亲和与实际双NUMA软惩罚分别测20/60/90%注入、原算法/最大余数、5次配对。每组4096请求，延迟剔除前256请求；goodput按整段实际传输时长。记录预定到达与实际提交两套延迟、最大注入迟到；环形槽满时等待，迟到不能被隐藏。
6. CPU固定8；短时运行、共享主机、不注入故障，不能称为原计划跨节点3–5分钟稳定性验收。没有SHM/CUDA IPC数据路径。descriptor/remote-address在同进程共享用于建连元数据，payload明确经RDMA WRITE传输。
7. GPU补充：8GPU×2NIC，64KiB host→GPU WRITE，CUDA读回验证。不是GPU→host、长时GPUDirect性能或全TENT GPU流程测试。

## 重跑

在仓库根目录，需已有P1/D的build/release和build/sanitize基准构建（可先运行build_experiment_d.py及其--sanitize）。运行目录拒绝覆盖，应先归档本次结果。

```bash
python3 tent-simulator/scripts/build_experiment_ef.py
python3 tent-simulator/scripts/run_experiment_ef.py --stage validation
python3 tent-simulator/scripts/run_experiment_ef.py --stage E
python3 tent-simulator/scripts/run_experiment_ef.py --stage F
python3 tent-simulator/scripts/build_experiment_ef.py --sanitize
python3 tent-simulator/scripts/run_experiment_ef.py --sanitize
# 编译cm_probe.cpp需-lrdmacm -libverbs；verbs_probe.cpp需-libibverbs。
python3 tent-simulator/scripts/run_hardware_probe.py
python3 tent-simulator/scripts/build_real_probe.py
python3 tent-simulator/scripts/run_real_probe.py
python3 tent-simulator/scripts/build_gpu_probe.py
python3 tent-simulator/scripts/run_gpu_probe.py
python3 tent-simulator/scripts/calibrate_replay.py
python3 tent-simulator/tests/acceptance.py --skip-overhead --output-dir /root/mooncake-tent-multirdma-output/runs/experiment-ef/p01-regression
python3 tent-simulator/scripts/check_candidate_order.py
python3 tent-simulator/scripts/audit_experiment_ef.py
python3 tent-simulator/scripts/report_experiment_ef.py
```

源码与生成补丁在simulator/scripts及build；原始结果在runs/experiment-ef。真实网络程序有进程级deadline，某探测失败保留返回码/日志，不能当作算法发生永久挂起。跨节点与多发送方仍需用户提供额外端点；不能用本机两进程代替跨节点。
