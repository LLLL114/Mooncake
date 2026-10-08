# PD：请求关联的 post→CQ 长尾定位

2026-09-30；目标仍是改善P99且减少权重震荡。本轮只定位信号，不改选路算法、线程数、驱动、网络或轮询顺序。

A10双erdma → erdma_test单erdma，Conda mooncake，CPU内存/NUMA0/1MiB/Q128/单caller/默认6 lanes。基于冻结O库，仅在私有workers.cpp加观测。源码在tent-simulator/cross-node-multirail，数据/库在仓库外BASE/poll-diagnostic-20260930及BASE/build-poll-diagnostic-20260930。本机只编辑/传输；不用scp。

## 记录与安全边界

- 原测试payload首8字节已有唯一request_id，且在请求完成前不被复用。只在成功且仍PENDING的Slice、发布该Slice完成之前读取这个CPU缓冲区编号；不新增payload字段，不改传输内容。
- 每个成功Slice记录request_id、请求内偏移/字节数、原enqueue/submit时间、CQ调用开始/结束、同CQ上次poll、上次返回少于64个CQE的调用、worker本轮起点/进入poll阶段、该Slice成功处理结束时间。
- 复用原有时间戳；新增每CQ调用前一次时钟及每成功Slice处理结束一次时钟。TENT这些时间戳使用CLOCK_REALTIME，流量器使用CLOCK_MONOTONIC；保留原时钟不改变算法。启动/结束及每worker约每秒记录MONO→REAL→MONO夹逼样本，所有样本的偏移区间必须存在公共交集且宽度<=20µs，才能以交集中点映射；不符合则拒绝阶段归因，保留原始数据。不能直接跨时钟相减。
- 每worker独立预分配缓存，测量时不写文件、不加日志锁；溢出/错误使诊断无效，保留失败数据。缓存配置用于6 workers，分配在创建engine前完成，需约0.8GiB额外内存；这是诊断工具开销，不是候选算法的资源需求。
- 完整记录同一(worker,CQ)的>=1ms轮询服务间隔，包括idle。分析时只与Slice的post→CQ生命周期求交，不能把无在途请求的休眠误算成其延迟。
- 相邻poll间隔包含worker其它工作、调度及本次poll调用；CQ调用时间也可能包含线程被抢占。成功返回<64只表示该次没有取满预算，不提供硬件完成时间。
- Slice处理结束时间在完成发布之后，可能晚于调用者已观察到完成。只作完成观察延迟的区间界限，不直接当作精确发布时刻。

## 先验验证与实验顺序

1. SSH内C++结构/边界测试与ASan/UBSan；编译私有库，生产源与旧库不变。
2. 两次225rps短测（原O、诊断库），8s+1s预热。每请求需16个不重叠64KiB记录、编号/总字节闭合、时间顺序与实际NIC分配相符；失败不静默重跑。
3. 三对原O/诊断饱和对照，15s+3s预热。吞吐配对中位>=.95且P99中位比<=1.20才继续；逐次坏点仍完整报告，不能称“没有开销”。若未过先停止正式定位并调查。
4. 原O/诊断×225/675rps×3重复，每次30s+5s预热，共12次，顺序预先固定。再报告同负载观测影响；不能把诊断库结果直接与旧日期原算法比较。
5. 全部流量结束后分析，不在实测时编译/重分析。本轮不重跑TG算法矩阵，不据本轮声称优化算法达标。

## 分析口径

原有端到端P99继续用planned→finished，保留提交等待/drain。对每请求，选择CQ结束时间最晚的Slice，作可相加的时间链：planned→submitted→该Slice enqueue→submit标记→CQ结束→调用者finished。最后一段包含CQ处理和调用者观察，不等于纯调用者轮询。

对post→CQ>=10ms的Slice，统计其生命周期与>=1ms同CQ服务间隔的重叠比例，以及最后一次未取满CQ调用距完成的间隔/调用长度。密集轮询且近期未取满时仍迟来的completion，是“设备/传输/provider完成可见性”线索；长轮询服务间隔可解释观察滞后的可能上界，但不能证明completion早已到达。

不报告未经硬件时间戳证实的纯网络延迟。条件子组仅用于定位，不从主P99中删除慢请求。所有结果限定当前单caller、健康双轨和该观测开销，后续再决定对gain加入哪种信号。

## 2026-10-08 事后解释补充（不改已测数据）

源码核查发现RdmaCQ::poll()在cqe_now==0时直接返回0，未必调用ibv_poll_cq。本轮begin/end记录包住外层函数，原字段drained、类别dense_polling只能解释为“外层调用返回未取满预算/频繁短调用”，不能证明真实provider轮询或硬件CQ已排空。原始协议保留在source-snapshot，主指标不改。另用已有50ms CQ quota样本与>100ms Slice存活区间交叉核查，结果仅是采样证据，不替代逐次provider入口观测。
