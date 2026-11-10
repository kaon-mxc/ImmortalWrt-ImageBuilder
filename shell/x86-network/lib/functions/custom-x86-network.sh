#!/bin/sh
# 仅由 x86-64/build25.sh 打包。PCI 插槽改变后需要更新本表。
custom_x86_port_map() {
    cat <<'EOF'
wan0 0000:02:00.0 r8127
lan1 0000:71:00.0 igb
lan2 0000:01:00.0 mlx5_core
lan3 0000:01:00.1 mlx5_core
lan4 0000:00:1f.6 e1000e
EOF
}

# 返回 OpenWrt 原生 preinit 所用的 /sys/devices 相对路径。
# 同时校验驱动及端口数量，避免 PCI 拓扑变化时认错网卡。
custom_x86_device_path() {
    local pci="$1" driver="$2" path bound port count=0
    local sysfs="${CUSTOM_X86_SYSFS:-/sys}"
    path=$(readlink -f "$sysfs/bus/pci/devices/$pci") || return 1
    case "$path" in
        "$sysfs"/devices/*) ;;
        *) return 1 ;;
    esac
    bound=$(readlink -f "$path/driver") || return 1
    [ "${bound##*/}" = "$driver" ] || return 1
    for port in "$path"/net/*; do
        [ -d "$port" ] || continue
        count=$((count + 1))
    done
    [ "$count" -eq 1 ] || return 1
    printf '%s\n' "${path#"$sysfs"/devices/}"
}

# 同时支持匿名和命名 UCI section，不依赖 zone/device 的排列顺序。
custom_x86_find_section() {
    local package="$1" type="$2" name="$3" section
    for section in $(uci -q show "$package" | sed -n "s/^\($package\.[^.]*\)=$type$/\1/p"); do
        if [ "$(uci -q get "$section.name")" = "$name" ]; then
            printf '%s\n' "$section"
            return 0
        fi
    done
    return 1
}

custom_x86_configure_network() {
    local bridge zone port ip

    # 不依赖 @zone[1]；升级恢复的防火墙配置可能调整过 section 顺序。
    zone=$(custom_x86_find_section firewall zone wan) || {
        echo 'custom-x86-network: WAN firewall zone is missing; refusing to configure WAN.' >&2
        return 1
    }
    uci set "$zone.input=REJECT" || return 1
    uci commit firewall || return 1

    bridge=$(custom_x86_find_section network device br-lan)
    if [ -z "$bridge" ]; then
        bridge="network.$(uci add network device)" || return 1
        uci set "$bridge.name=br-lan" || return 1
    fi
    uci set "$bridge.type=bridge" || return 1
    uci -q delete "$bridge.ports"
    for port in lan1 lan2 lan3 lan4; do
        uci add_list "$bridge.ports=$port" || return 1
    done
    # 迁移旧 ethX 配置，只改物理端口绑定。
    uci set network.lan=interface || return 1
    uci -q delete network.lan.ifname
    uci -q delete network.lan.type
    uci set network.lan.device=br-lan || return 1
    uci set network.wan=interface || return 1
    uci -q delete network.wan.ifname
    uci -q delete network.wan.type
    uci set network.wan.device=wan0 || return 1
    uci set network.wan6=interface || return 1
    uci -q delete network.wan6.ifname
    # 保留 PPPoE 等场景下已有的逻辑接口引用。
    if [ "$(uci -q get network.wan6.device)" != '@wan' ]; then
        uci set network.wan6.device=wan0 || return 1
    fi

    # preinit 在恢复升级备份后、生成默认 network 之前记录是否已有配置。
    if [ ! -e /tmp/custom-x86-network-preserved ]; then
        ip=$(cat /etc/config/custom_router_ip.txt 2>/dev/null)
        uci set "network.lan.ipaddr=${ip:-10.0.0.1}" || return 1
        uci set network.lan.netmask=255.255.255.0 || return 1
        uci set network.lan.proto=static || return 1
        uci set network.wan.proto=dhcp || return 1
        uci set network.wan6.device=wan0 || return 1
        uci set network.wan6.proto=dhcpv6 || return 1
        if [ "$enable_pppoe" = yes ]; then
            uci set network.wan.proto=pppoe || return 1
            uci set "network.wan.username=$pppoe_account" || return 1
            uci set "network.wan.password=$pppoe_password" || return 1
            uci set network.wan.peerdns=1 || return 1
            uci set network.wan.auto=1 || return 1
            uci set network.wan6.proto=none || return 1
        fi
    fi

    uci commit network || return 1
    echo 'custom-x86-network: WAN=wan0; br-lan=lan1 lan2 lan3 lan4' >>"${LOGFILE:-/tmp/custom-x86-network.log}"
}
