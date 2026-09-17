# TENT 原算法观测：主代理构建与容量契约

仅生成构建目录中的源码副本，不修改生产 repo、header ABI、pilot 或 summarize_requests.py。基准 HEAD 参考为 `89c3835`；生成器实际以 SHA256 和逐个锚点匹配作为准入条件。此次容量修改没有在本地执行、编译或验证；所有验证由主代理在服务器完成。

## 文件与生成器 CLI

本说明所在目录包含四个交付文件：`observer.h`、`observer.cpp`、`instrument_tent.py`、`OBSERVER.md`。

```text
python3 /path/to/instrument_tent.py --source-root /path/to/a10-source --builddir /path/to/observer-build
python3 /path/to/instrument_tent.py --source-root /path/to/a10-source --builddir /path/to/observer-build --check-only
python3 /path/to/instrument_tent.py --help
```

全部选项：

| 参数 | 语义 |
| --- | --- |
| `--source-root PATH` | 必填。仓库根目录，其下必须有 `mooncake-transfer-engine/tent/src/transport/rdma/`；使用与服务器一致的 a10-source，不能用新版 Mooncake。 |
| `--builddir PATH` | 必填。必须位于 source-root 外，且不能是 observer 原文件所在目录。相对路径按运行生成器时的工作目录解析。 |
| `--check-only` | 只校验全部输入哈希、锚点及目标路径，不创建目录、不写文件。仍需提供 builddir。 |
| `-h` / `--help` | argparse 帮助。 |

输入相对于 source-root：

```text
mooncake-transfer-engine/tent/src/transport/rdma/quota.cpp
mooncake-transfer-engine/tent/src/transport/rdma/workers.cpp
mooncake-transfer-engine/tent/src/transport/rdma/rdma_transport.cpp
mooncake-transfer-engine/tent/src/transport/rdma/endpoint.cpp
```

`endpoint.cpp` 只读校验，不生成修改副本：workers 的已接受 post 统计依赖其 `bad_wr` 后缀被标记为 failed 的语义。

输出相对于 builddir（平铺，不复制原目录层级）：

```text
quota.cpp
workers.cpp
rdma_transport.cpp
observer.h
observer.cpp
observer-manifest.json
```

生成器从自身所在目录读取 observer.h/.cpp，调用前应将三个实现文件放在一起。输出 manifest 记录三个源码输入/输出哈希、锚点计数、endpoint 语义依赖哈希及 observer 头/实现哈希。校验失败非零退出。输出目录中同名普通文件会被覆盖；符号链接和硬链接目标被拒绝。

输入 SHA256：

| 文件 | SHA256 |
| --- | --- |
| quota.cpp | `9d064e873b0e060e30a373b039df253499529eaab2e7df6a555773ebe2efc913` |
| workers.cpp | `2c30639eb9b433ca35ec476383781e96b4e48309d866c0c9b05194378ade8af0` |
| rdma_transport.cpp | `9f37e499581ff6c1f4f3bf7b32fd98573b65eaf657256a183ce3c0478e627302` |
| endpoint.cpp | `1907d9248b97d857ff394e978a740c255b46ed1b2e5f60acbee78954d477a7c7` |

## 编译与 C 接口

在服务器使用现有 build-tent flags，添加 builddir 到 include 搜索路径；分别编译 builddir 的三个修改 cpp 和独立 observer.cpp，将三个原对象替换为修改对象，额外链接 observer 对象，重新链接独立观察库。observer 使用 C++17 和标准库，不依赖 nlohmann，不构造完整 JSON DOM。既有生产头保持不变。导出接口显式具有 default visibility：

```cpp
extern "C" void tent_obs_configure(int enabled, uint64_t start_ns, uint64_t end_ns) noexcept;
extern "C" void tent_obs_dump(const char* path) noexcept;
extern "C" int tent_obs_last_error() noexcept;
```

控制接口由主驱动串行调用，不能在观测回调内部调用。默认关闭。`configure(1,start,end)` 先关闭并等待正在执行的观测退出，再清空聚合数据，开始新 epoch；要求 end > start。清空约 386 MiB arena 的耗时应置于正式测量前：选未来的 start，待配置完成再按计划注入。所有时间为 CLOCK_MONOTONIC 纳秒，使用 `[start,end)`，每 50ms 相对 start 分桶。

`configure(0,0,0)` 关闭并排空正在执行的观测，保留本轮数据。`dump(path)` 同样先关闭并排空，再写 JSON；工作线程可以继续运行关闭的观测点。调用后读取 last_error，0 为成功，非零为 errno 风格错误。控制函数之间不能并发。dump 覆盖给定路径，父目录须已存在。

allocate 按入口时刻归桶，入口在区间内的调用保留至退出，可能在 end 后完成；其候选 hook 使用该调用的活动状态，关闭时不会截断已进入调用。release/post/CQ 按各自 hook 时刻归桶，因此窗口边界处三类字节不要求守恒。阶段突变若不应参与 TV/reversal，须关闭、导出并开始新 epoch。

## 容量：60/120 秒设计包络，512 MiB 预算

当前编译期配置：16 个 lifetime 线程槽、每线程 64 个上下文、131072 个聚合行、每次决策最多 16 个候选设备；哈希线性探测上限 512 步。6 worker + 8 caller 使用 14 槽，余 2 槽。线程槽不随 configure 重用、线程退出也不回收；主驱动应复用固定线程池。若不停销毁重建 caller，累计超过 16 个线程会失败，即使同时在线线程不足 16。

常见 64 位 ABI 下 Row 为 192 字节，行池为 `16 × 131072 × 192 = 384 MiB`；上下文、scratch、采样器及计数器约另占 1.5 MiB。`arena_bytes` 报告编译器实际 sizeof；static_assert 强制 arena 小于 512 MiB。JSON 流式逐行输出，不额外复制整份 arena；预算不包含 TENT 本身、驱动或 sanitizer 的内存。

容量由不同 `(thread,context,bin,device,kind)` 数量决定，**不随同一个键下的事件速率增加**。所有权重决策、事件/reversal、分配/post/CQ 字节仍全速聚合；只有队列读取每个键每 50ms 最多采样一次。没有通过稀疏采样权重决策来降低容量。

以固定请求大小、peer/location/priority/mask、双轨为正式 baseline 的包络估算：

| 单线程流量形态 | 每 50ms 行数估算 |
| --- | ---: |
| 一个 allocator 模式：candidate×2、weight×2、allocation×2、weight_tv、allocation_tv、allocate_call | 9 |
| 同线程同时出现单片、正常多片、probe 三个模式 | 27 |
| transport 门槛，三个固定请求上下文 | 3 |
| worker 双轨 release：bw/bounds/sample/interval/no_learning | 10 |
| worker 双轨九类 post/CQ/retry（包含全部错误类的保守估算） | 18 |
| worker 快照×1、双轨 CQ 快照×2 | 3 |
| 典型 worker 保守合计（三种 allocator 模式也都计入） | 58 |

普通健康单大小 baseline 往往少于上述行数。使用更保守的 **64 行/50ms/线程** 计算：60s 为 76800 行/线程，120s 为 153600 行/线程，后者超出单线程容量，**不能宣称支持这个更宽包络**。针对实际角色分离的包络，caller 三模式加门槛最多 30 行/50ms；worker 三模式全错误包络 58 行仍超出 120s 单线程容量。因此正式 120s 的准入条件必须检查实际上下文与模式数：固定大小健康 worker 通常只含单片 allocator（9）或没有 allocator，再加 release（成功路径 8）、post/CQ（4）、队列（3），即至多 24 行/50ms。

| 正式固定大小健康 baseline | 60s / 1200 bins | 120s / 2400 bins | 每线程容量 |
| --- | ---: | ---: | ---: |
| caller：正常多片+probe 两模式及门槛，按 19 行/bin | 22800 | 45600 | 131072 |
| worker：按上述 24 行/bin | 28800 | 57600 | 131072 |
| 8 caller + 6 worker，总使用行数 | 355200 | 710400 | 总物理容量 2097152 |

这为正式 60/120s 固定大小健康 baseline 留出余量，但不是任意上下文/故障组合的无限容量保证。不同大小、peer、location、priority、mask、候选集合、allocator 模式会创建额外上下文；变化频繁时也可能先耗尽 64 个上下文。主代理仍须在服务器用真实 1/8 caller + 6 worker 配置运行 60s/120s 容量验收，检查所有失败标记及哈希探测碰撞。此次修改没有执行该验收。

600s 有 12000 bins，按 caller 19 行/bin 为 228000 行、worker 24 行/bin 为 288000 行，均超过 131072。**当前配置不支持声称 600s 全量采集**；必须采用更大预算并重新估算/验证，或在正式方案中明确分段导出。分段会重置相邻决策状态、产生控制开销，段边界不计算 TV/reversal，也不能伪装为连续全量。

## 失败标记与字段语义

JSON 顶层 `incomplete=true` 表示存在任何线程拒绝、聚合行/上下文/候选容量丢失、非法值、嵌套分配、时钟错误、过长 location、分配未完整覆盖或队列采样表溢出。保留所有 `threads[].drops` 明细和 `rejected_threads_lifetime`；线程拒绝为进程 lifetime 计数，不随 epoch 清零。行溢出包含达到探测上限的碰撞，即使表未物理填满也不能隐瞒。任何溢出都应使正式全量观测验收失败，不用已有行外推丢失事件。

原生产分片器可能提交零长度 descriptor。例如 999423B 请求的 15 个 descriptor 中可有 7 个 length=0，另 8 个 descriptor 完整覆盖有效字节。成功的零长度 allocate 在候选存在且返回 descriptor 数等于请求 slices 数时，不再计入 `drops.missing_allocation`。正长度调用的成功、有效字节覆盖及候选检查保持原样；零长度调用失败或返回数量不足仍计缺失。collector 不修改生产分片、charge、post 或 CQ 行为。

`allocate_call.zero_length_allocations`（同时为该行 flags[0]）统计 total_bytes=0 的调用；`allocation.zero_length_slices`（同时为该行 flags[0]）统计还原有效长度为 0 的返回 slice，包含正长度调用中的零长度尾部 descriptor。它们的 n 正常累计，零长度工作的 bytes 增量为 0，aux 仍保留原有成功次数/charge 口径。total_bytes=0 的上下文不输出 allocation_tv 或 weight_tv 样本、不进行零分母份额归一化，`allocation_share_tv_semantics` 和 `weight_tv_semantics` 显式标记 `N/A: zero total bytes`；真实候选 score 和可用的 weight 观测仍保留。比较次数为 0 不能解释成零长度流量的 TV=0。

`rows` 无时间排序保证，按 bin 合并；实际桶起点为 `start_ns + bin × 50000000`。每行 n 为观测次数，values 的每项均含 sum/mean/min/max/last；合并窗口均值需用 sum 与 n，不可直接平均 mean。线程与上下文先分别保留，不能跨线程拼接相邻决策。

| kind | bytes / aux | values[0..2] / flags |
| --- | --- | --- |
| candidate | 算法读到的 inflight 字节累计 / 0 | 真实 score、当时 EWMA B/s、rank penalty |
| weight | 0 / 0 | 归一化逆评分权重、0、0 |
| weight_tv | 0 / 0 | TV、0、0；flags[0..2] 为 TV > .001/.01/.05 事件数；flags[3..5] 为相应 reversal 数 |
| allocation | 计划有效字节 / 原算法 charge 字节 | n 为分配 slice 数；有效字节按返回设备顺序和调用的 slice_bytes/total 还原；zero_length_slices / flags[0] 为零长度 slice 数 |
| allocation_tv | 0 / 0 | 同上下文相邻分配有效字节份额 TV |
| allocate_call | 入参 total 字节 / 成功调用数 | 入口至退出 ns、请求 slice 数、输出设备数；zero_length_allocations / flags[0] 为零长度调用数；上下文另有 log2 ns 直方图，不能冒充精确 P99 |
| release_bw | release length / 0 | current_ewma、observed_bw、new_ewma，单位 B/s；flags 为 new==min、new==max、unclamped<min、unclamped>max，后两项保留 0 |
| release_bounds | 0 / 0 | theoretical_bw、min_bw、max_bw，单位 B/s |
| release_sample | 0 / 0 | latency 秒、alpha、裁剪前 new_ewma B/s |
| release_interval | 0 / 0 | 同线程/selector/NIC 连续更新间隔 ns，不是全局跨线程更新时间顺序 |
| release_no_learning | 释放字节 / 0 | latency 秒、0、0；不伪造带宽样本 |
| batch_threshold | 请求字节 / 达到门槛次数 | num_slices、max_slice_count/2、block_size；达到门槛不等于 allocate 成功 |
| post/cq/retry 各类 | slice length 累计 / retry_count>0 次数 | retry_count、WC status（只在 CQ 类有效）、0 |
| worker_queue | 0 / 0 | worker.inflight_slices、inflight_slice_set.size、requeue_overflow.size；分别是软件计数，不称为硬件队列深度 |
| cq_queue | 0 / 0 | CQ reserved quota、max CQE、0；不代表 NIC 硬件队列字节 |

mode：0 单片 argmin、1 正常多片、2 实际 probe RR、3 baseline RR、4 非分配遥测。只有 mode 1 的 weight 是原分配实际使用的权重；mode 0/2 从已捕获 score 得到的逆评分归一化仅为诊断，JSON 标记 `diagnostic_inverse_score_only`，不能作为实际随机选卡概率。reversal 定义为相邻超过对应 TV 阈值的变化向量点积为负；双轨时等价于有符号变化反向。上下文、模式、候选集合变化不会直接拼接为同一序列。

post 指 submitSlices 返回前缀中未 failed 的已接受工作；CQ 错误字节表示相关 WR 的 payload 长度，不能称为成功传输字节。已终态 slice 的 WC 进入 cq_late_success/error，不混入普通 CQ。retry_endpoint/post/cq 是真实 retry_count 增量，不能推导唯一跨轨重发字节；稳定 flow ID、可靠跨轨 attempt 关联、硬件队列字节、持久目标发布均为 N/A。请求 goodput 和端到端尾时延由主 CAPI 驱动提供。

## 队列空轮询开销对照

当前源码已加入 `observer-queue-stride.apply_patch` 的队列门控，另建 v2 观察库验证，不覆盖旧库。默认 `TENT_OBS_QUEUE_POLL_STRIDE=1` 保持每次轮询检查；显式设置 `256` 时按每个 thread/worker/dev/cq 独立计数，仅首次及每256次轮询进入原有时钟/50ms采样检查。权重、分配、学习、post和CQ事件计数仍全量。

JSON 的 `queue_sampling` 与各线程 `queue_sampling_coverage` 披露配置及实际采样桶数。队列未采样窗口是未观测，不能填零；低轮询频率时可能跨多个50ms桶，不能将这种队列快照当作连续时间队列统计。门控表满时回退原采样器，任何汇总溢出均单独标记。环境仅接受1和256，配置于测量开始前读取。

这只是测量工具的可选降开销设置，尚需服务器的开销、覆盖与计数验证。详细候选补丁仍保留在同目录供审阅。

## 远程待验证

1. 运行生成器 check-only 和生成，核对 manifest；使用现有 flags 编译三修改对象及 observer 并链接，确认导出 C 接口可加载。
2. 验证关闭、未来/过期窗口、区间末尾活动 allocate、关闭排空和新 epoch 清空语义；确认窗口外无采集。
3. 验证双轨 TV/阈值/reversal、单片/多片/probe 分类、真实尾片字节及 clamp 上下界命中。零长度局部补丁须在服务器另建新观察库验证 999423B/15 descriptor 案例：7 次零长度单片 allocate 不增加 missing_allocation，保留次数/bytes=0，份额与 TV 为 N/A；同时验证正长度真实缺失仍被标记。不要覆盖正在运行 overhead 的既有库。
4. 验证容量边界失败标记，实测 60s/120s 的 1/8 caller + 6 worker 配置零溢出，保留 arena_bytes 和各线程行/上下文数量。
5. 同一观察库关闭/开启配对测量吞吐与尾时延开销；检查源码随机调用、分配/学习表达式保持原样。不把短 mock 或静态容量估算作为长时实测通过证据。
