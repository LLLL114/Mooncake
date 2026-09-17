# TENT 跨机 eRDMA 连通性与实际带宽

本目录仅验证跨机 RDMA WRITE 的数据正确性和实际带宽；每次固定一个发送端 NIC，不运行多轨选路对比。脚本使用已安装的 `mooncake` Conda 环境，通过 `MC_USE_TENT=1` 启用 TENT。TCP 仅交换地址、控制命令及校验结果；传输数据使用 RDMA。

## 运行

先在 `erdma_test` 上启动接收端，看到 `RECEIVER_READY` 后启动发送端：

```bash
cd /root/mooncake/tent-simulator/connectivity
./run_receiver.sh --nic erdma_0 --bind-ip 10.0.1.251
```

在 `A10_8` 上依次执行，两张网卡分别测试：

```bash
cd /root/mooncake/tent-simulator/connectivity
./run_sender.sh --nic erdma_0 --peer 10.0.1.251
./run_sender.sh --nic erdma_1 --peer 10.0.1.251 --stop-receiver
```

只验证连通性时增加 `--check-only`。默认控制端口 19830、接收端 TENT RPC 19831、发送端 TENT RPC 19832。接收服务默认运行上限 1800 秒；整个进程另有 timeout 防护。若操作中断，重新启动接收端后再测。

## 方法和口径

- CPU DRAM，RDMA WRITE；混合 1 B、4 KiB、64 KiB、960 KiB、976 KiB、1 MiB、16 MiB 的三个传输轮次，每轮完成后验证完整 SHA256 和前后哨兵，确认地址、长度和覆盖写正确性。
- 带宽默认块大小 64 KiB / 1 MiB / 16 MiB，每批 32 个请求；每组预热 0.5 秒、测量至少 5 秒、重复三次。
- 主指标 `goodput_gbps = 成功完成的有效字节 × 8 / 测量墙钟时间`，包含发送循环中的标记更新、Python 与 API 开销，排除注册、预热、TCP 控制和 SHA256 校验。Gbps 为十进制；GiB/s 为二进制。
- 辅助指标 `api_goodput_gbps` 只用批量 API 调用累计时间。两者都是本测试配置下实测值，不能直接等同于硬件最大带宽。
- 测量阶段每个批次均等待完成，预热结束和每组测量结束做完整数据及哨兵校验；不会逐批校验测量期间被覆盖的所有历史内容。
- 记录发送端全部 eRDMA NIC 及接收端的硬件计数器增量，辅助确认实际流量路径。计数器属于整张设备，可能包含其他进程的流量。
- 两条路径分别测量的带宽不能相加作为多轨吞吐；本轮没有测试同时使用两条 Rail。

## 文件

所有结果默认写到仓库外：

```
/root/mooncake-tent-multirdma-output/connectivity/runs/<角色-NIC-时间-ID>/
```

`summary.json` 是汇总；`bandwidth.json` 包含逐组吞吐、延迟原始采样及硬件计数器；`correctness.json` / `receiver-checks.json` 包含校验；`environment.json`、`tent-config.json`、`topology.json` 记录环境与 TENT 配置。可以通过 `--output-root` 指定其他仓库外目录。

代码和 Shell 保留在本地工作区，未执行 git add 或 commit。

## 驱动前置条件

本次验证发现，`erdma_test` 的 eRDMA 驱动原始 `compat_mode=N`，只有非 IP 格式 GID；TENT 直接配置 QP 时，在 RTR → RTS 返回 EINVAL。`A10_8` 为 `compat_mode=Y`。该参数只读，变更需要重新加载驱动，不能靠设置 `MC_GID_INDEX=1` 解决（原接收端甚至没有 GID 1）。脚本对 N 模式会提前给出明确错误。驱动变更由管理员确认后单独执行，测试脚本不会自行卸载驱动。

本轮经授权已在 `erdma_test` 临时加载 `compat_mode=Y`，其余模块参数保持原值；未修改 modprobe 永久配置。系统重启/重新加载驱动后需重新检查该参数。原参数位于接收端仓库外的 `connectivity/preflight/receiver-driver-original.json`。

日志若显示 `link speed 0 Gbps ... assuming 400 Gbps`，这是 TENT 的默认估计，并非本次实测带宽。请以 `goodput_gbps` 为准。
