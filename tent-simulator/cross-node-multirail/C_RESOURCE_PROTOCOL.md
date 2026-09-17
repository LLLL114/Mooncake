# C：两端匹配的单lane资源对照

固定资源核心50点与此处的资源10点分开保存。原先“发送1、接收6”被TENT建连拒绝的记录保留为配置失败，不当性能结果，也不重复该配置。

## 配置

- 两端都设`transports/rdma/num_lanes=1`，其余选择器、库、NIC集合和发送NUMA0不变。
- 接收端运行从冻结`receiver_extended.py`生成的`receiver_lanes1.py`。它声明num_lanes及源文件SHA，发送端每次在数据传输前通过控制面核对；运行manifest再复核双方设置。
- 发送端调用原native_sender_extended，worker参数1；只有1个caller，Q128，1 MiB。
- num_lanes同时改变两端Worker/QP/CQ资源，不能称为纯Worker数消融或与核心实验相同QP预算。

## 顺序

1. `run_C_resources.sh --phase pilot`：8秒20%输入、1秒预热，验证配套配置的真实数据传输。
2. `--phase calibrate`：同Q的双轨饱和闭环10秒×3次，冻结单lane资源容量参考。它不是硬件峰值或单卡容量。
3. `--phase formal`：120秒测量、10秒预热，默认A参考的20%输入及饱和闭环各5次，分块随机顺序。20%约1.916Gbps，可与已有A的D0/1MiB/Q128/20%控制比较；饱和闭环Q128与A的同配置饱和控制比较。此处不是测试新算法。

9月16日正式运行前根据三次资源标定修订：单lane参考2.272Gbps（三次2.005/2.272/2.403），原默认参考60%/90%的5.748/8.623Gbps均远超此容量。直接沿用会主要测到不可持续输入和预热时序失效，不能作为稳态性能对照。因此将尚未执行的正式计划从60%/90%改为20%/饱和，保留10点；协议标识为C-matched-lanes1-formal-v2。20%默认参考相当于单lane标定约84%的输入，不是重新给它定义20%负载。跨日期既有A控制的环境变化仍是限制。

正式资源用例复用基线的严格终态/过载校验函数，目录中的phase字段因此为baseline，但它们的protocol=`C-matched-lanes1-formal-v2`（pilot/calibrate仍v1），独立的receiver_contract和输出根明确标识此实验；它们不是原A基线，也不改变A或C核心case身份。

数据位于服务器仓库外`cross-node-multirail/followups-c-resources/`。`progress-formal.json`的complete只表示收集完成，all_normal_success独立报告。预计过载、意外过载、测量未开始和未验证数据都不能冒充正常性能通过。未知错误或未排空请求停止诊断，不自动反复重试。

结束后先让发送端完成并排空，再通过本测试控制接口停止单lane receiver，恢复默认receiver_extended（lane6），验证后再进行D/E/F或batch API对照。不能在单lane接收端上直接运行原默认lane6的发送脚本。
