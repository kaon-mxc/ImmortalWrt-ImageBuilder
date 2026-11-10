# 本分支的 x86 25.12 网口布局

`build25.sh` 将 `shell/x86-network/` 复制到固件。普通 EFI 镜像和使用同一脚本的 ISO 构建均采用以下专用布局；其他架构及 24.x 构建不启用它。

| 固定名称 | PCI 地址 | 驱动 | 用途 |
| --- | --- | --- | --- |
| wan0 | 0000:02:00.0 | r8127 | WAN / WAN6 |
| lan1 | 0000:71:00.0 | igb | br-lan |
| lan2 | 0000:01:00.0 | mlx5_core | br-lan |
| lan3 | 0000:01:00.1 | mlx5_core | br-lan |
| lan4 | 0000:00:1f.6 | e1000e | br-lan |

每次启动由 OpenWrt 原生 preinit 的 `network_device.path` 机制重命名网口，初始 `ethX` 顺序不参与配置。五个口的驱动均支持早期启动加载；mlx5 和 RTL8127 已显式加入软件包清单，igb/e1000e 为 x86 默认驱动。匹配时校验 PCI 地址、驱动和单端口条件；缺失或不匹配的口不回退到其他接口，WAN 缺失时其余 LAN 仍可用于管理。

首次安装或恢复出厂时，LAN 使用 Actions 输入的管理 IP 和 `/24` 掩码（无输入文件时为 `10.0.0.1`），WAN 使用 DHCP 或 Actions 指定的 PPPoE。保留配置升级时，会迁移 WAN 和 LAN 的物理绑定，保留已有 IP、拨号参数及 `wan6` 的 `@wan` 引用。WAN 防火墙区域按名称查找，入站策略设为 `REJECT`。

网络配置由首次启动脚本写入，日常重启只执行固定命名。普通接口角色修改可在 LuCI 中进行；重新刷入本固件会再次应用上述默认物理绑定。PCI 插槽、BIOS 拓扑或直通地址变化时，需要更新 `custom_x86_port_map`，不能把此固件的专用布局直接用于其他机器。

刷机后可以用 `ethtool -i wan0` 确认 `r8127` / `0000:02:00.0`，用 `uci show network` 和 `bridge link` 检查四个 LAN 成员。当前映射基于已提供的硬件信息，仍需在实机上验证。

## 回归测试

`tests/test_x86_network.py` 使用实际的 OpenWrt `uci`、`jshn` 和 BusyBox ash，仅模拟 sysfs 与 `ip link` 操作。运行环境需将这三个工具放入 `PATH`，将 `JSHN_SHELL` 指向 libubox 的 `sh/jshn.sh`，并设置 `OPENWRT_TEST_SUPPORT` 目录；其中放置 ImmortalWrt `openwrt-25.12` 分支 `package/base-files/files/` 下的三个原始文件：

- `lib/functions.sh`，保存为 `lib_functions.sh`
- `lib/functions/uci-defaults.sh`，保存为 `lib_functions_uci-defaults.sh`
- `lib/preinit/10_indicate_preinit`，保存为 `lib_preinit_10_indicate_preinit`

然后运行 `python3 tests/test_x86_network.py`。测试覆盖接口乱序及重复启动、缺失/不匹配/多端口的 WAN 设备、旧桥配置迁移、首次安装、保留配置升级和防火墙区域重排。
