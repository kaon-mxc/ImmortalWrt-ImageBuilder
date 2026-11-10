"""Run with real OpenWrt uci/jshn on PATH and OPENWRT_TEST_SUPPORT set.

OPENWRT_TEST_SUPPORT contains upstream lib_functions.sh,
lib_functions_uci-defaults.sh and lib_preinit_10_indicate_preinit.
JSHN_SHELL points to libubox's sh/jshn.sh. Only sysfs and ip are simulated;
all UCI changes, board JSON generation and preinit renaming use real tools.
"""

import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
OVERLAY = ROOT / "shell/x86-network"
PORTS = [
    ("wan0", "0000:02:00.0", "r8127", "eth3"),
    ("lan1", "0000:71:00.0", "igb", "eth0"),
    ("lan2", "0000:01:00.0", "mlx5_core", "eth1"),
    ("lan3", "0000:01:00.1", "mlx5_core", "eth2"),
    ("lan4", "0000:00:1f.6", "e1000e", "eth4"),
]


class NetworkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.config = self.base / "config"
        self.config.mkdir()
        (self.base / "delta").mkdir()
        self.marker = self.base / "preserved"
        self.sysfs = self.base / "sys"
        self.env = dict(os.environ, CUSTOM_X86_SYSFS=str(self.sysfs),
                        TEST_ROOT=str(self.base), LOGFILE=str(self.base / "log"),
                        enable_pppoe="no", pppoe_account="", pppoe_password="")
        helper = (OVERLAY / "lib/functions/custom-x86-network.sh").read_text()
        helper = helper.replace("/tmp/custom-x86-network-preserved", str(self.marker))
        helper = helper.replace("/etc/config/custom_router_ip.txt", str(self.config / "custom_router_ip.txt"))
        self.helper = self.base / "helper.sh"
        self.helper.write_text(helper)
        uci = shlex.quote(shutil.which("uci"))
        self.prelude = (
            f"uci() {{ {uci} -c {shlex.quote(str(self.config))} "
            f"-t {shlex.quote(str(self.base / 'delta'))} \"$@\"; }}\n"
            f". {shlex.quote(str(self.helper))}\n"
        )
        self.write_configs()

    def run_shell(self, script, check=True):
        return subprocess.run(["busybox", "ash", "-c", self.prelude + script],
                              env=self.env, text=True, capture_output=True, check=check)

    def get(self, key):
        return self.run_shell("uci -q get " + shlex.quote(key)).stdout.strip()

    def write_configs(self, named=False):
        section = " 'lan_bridge'" if named else ""
        (self.config / "network").write_text(f"""
config device{section}
 option name 'br-lan'
 option type 'bridge'
 list ports 'eth0'
 list ports 'eth1'
 list ports 'eth2'
 list ports 'eth3'
 list ports 'eth4'
config interface 'lan'
 option device 'br-lan'
 option proto 'static'
 option ipaddr '10.9.0.1'
 option netmask '255.255.255.0'
 option ifname 'eth0 eth1 eth2 eth4'
config interface 'wan'
 option device 'eth3'
 option ifname 'eth3'
 option proto 'pppoe'
 option username 'existing-user'
 option password 'existing-password'
 option mtu '1492'
config interface 'wan6'
 option device '@wan'
 option proto 'dhcpv6'
""")
        (self.config / "firewall").write_text("""
config zone 'guest'
 option name 'guest'
 option input 'DROP'
config zone
 option name 'lan'
 option input 'ACCEPT'
config zone 'external'
 option name 'wan'
 option input 'ACCEPT'
 option output 'ACCEPT'
 option forward 'REJECT'
 list network 'wan'
 list network 'wan6'
""")

    def assert_bindings(self):
        self.assertEqual(self.get("network.wan.device"), "wan0")
        self.assertEqual(self.get("network.lan.device"), "br-lan")
        section = self.run_shell("custom_x86_find_section network device br-lan").stdout.strip()
        self.assertEqual(self.get(section + ".ports"), "lan1 lan2 lan3 lan4")
        self.assertEqual(self.get("firewall.external.input"), "REJECT")
        self.assertEqual(self.get("firewall.@zone[1].input"), "ACCEPT")
        self.assertNotEqual(self.run_shell("uci -q get network.wan.ifname", False).returncode, 0)

    def test_fresh_defaults_and_idempotence(self):
        self.run_shell("custom_x86_configure_network")
        self.assert_bindings()
        self.assertEqual(self.get("network.lan.ipaddr"), "10.0.0.1")
        self.assertEqual(self.get("network.wan.proto"), "dhcp")
        self.assertEqual(self.get("network.wan6.device"), "wan0")
        before = self.run_shell("uci export network; uci export firewall").stdout
        self.run_shell("custom_x86_configure_network")
        self.assertEqual(before, self.run_shell("uci export network; uci export firewall").stdout)

    def test_fresh_pppoe_and_custom_ip(self):
        (self.config / "custom_router_ip.txt").write_text("10.0.8.1\n")
        self.env.update(enable_pppoe="yes", pppoe_account="new-user",
                        pppoe_password="test password '$()")
        self.run_shell("custom_x86_configure_network")
        self.assert_bindings()
        self.assertEqual(self.get("network.lan.ipaddr"), "10.0.8.1")
        self.assertEqual(self.get("network.wan.proto"), "pppoe")
        self.assertEqual(self.get("network.wan.password"), self.env["pppoe_password"])

    def test_upgrade_preserves_settings_with_named_bridge(self):
        self.write_configs(named=True)
        self.marker.touch()
        self.env.update(enable_pppoe="yes", pppoe_account="do-not-apply")
        self.run_shell("custom_x86_configure_network")
        self.assert_bindings()
        self.assertEqual(self.get("network.lan.ipaddr"), "10.9.0.1")
        self.assertEqual(self.get("network.wan.username"), "existing-user")
        self.assertEqual(self.get("network.wan.password"), "existing-password")
        self.assertEqual(self.get("network.wan.mtu"), "1492")
        self.assertEqual(self.get("network.wan6.device"), "@wan")

    def test_missing_bridge_is_created(self):
        text = (self.config / "network").read_text()
        (self.config / "network").write_text(text[text.index("config interface"):])
        self.run_shell("custom_x86_configure_network")
        self.assert_bindings()

    def test_missing_wan_zone_stops_configuration(self):
        self.run_shell("uci delete firewall.external; uci commit firewall")
        before = (self.config / "network").read_bytes()
        self.assertNotEqual(self.run_shell("custom_x86_configure_network", False).returncode, 0)
        self.assertEqual(before, (self.config / "network").read_bytes())

    def test_preserved_config_detection(self):
        script = (OVERLAY / "lib/preinit/81_custom_x86_network").read_text()
        script = script.replace("/etc/config/network", str(self.config / "network"))
        script = script.replace("/tmp/custom-x86-network-preserved", str(self.marker))
        self.run_shell("boot_hook_add() { :; };\n" + script + "\ncustom_x86_check_preserved_network")
        self.assertTrue(self.marker.exists())
        (self.config / "network").write_text("")
        self.run_shell("boot_hook_add() { :; };\n" + script + "\ncustom_x86_check_preserved_network")
        self.assertFalse(self.marker.exists())

    def hardware(self, names=None, missing_wan=False, wrong_driver=False):
        for name, pci, driver, original in PORTS:
            if missing_wan and name == "wan0":
                continue
            iface = names[name] if names else original
            device = self.sysfs / "devices/pci0000:00" / pci
            (device / "net" / iface).mkdir(parents=True)
            target = self.sysfs / "bus/pci/drivers" / ("igb" if wrong_driver and name == "wan0" else driver)
            target.mkdir(parents=True, exist_ok=True)
            (device / "driver").symlink_to(target)
            pci_dir = self.sysfs / "bus/pci/devices"
            pci_dir.mkdir(parents=True, exist_ok=True)
            (pci_dir / pci).symlink_to(device)
            classes = self.sysfs / "class/net"
            classes.mkdir(parents=True, exist_ok=True)
            (classes / iface).symlink_to(device / "net" / iface)
        (self.base / "net-dev").write_text("\n".join(f" {path.name}: 0" for path in (self.sysfs / "class/net").iterdir()))

    def board(self):
        support = Path(os.environ["OPENWRT_TEST_SUPPORT"])
        defaults = (support / "lib_functions_uci-defaults.sh").read_text()
        defaults = defaults.replace("/lib/functions.sh", str(support / "lib_functions.sh"))
        defaults = defaults.replace("/usr/share/libubox/jshn.sh", os.environ["JSHN_SHELL"])
        (self.base / "defaults.sh").write_text(defaults)
        board = (OVERLAY / "etc/board.d/99-zz-custom-x86-network").read_text()
        board = board.replace("/lib/functions/uci-defaults.sh", str(self.base / "defaults.sh"))
        board = board.replace("/lib/functions/custom-x86-network.sh", str(self.helper))
        cfg = self.base / "board.json"
        cfg.write_text(json.dumps({"model": {"id": "test"}, "network_device": {"eth9": {"path": "obsolete"}},
                                   "network": {"lan": {"ports": ["eth0", "eth3"]}}}))
        self.run_shell(f"CFG={shlex.quote(str(cfg))}\n" + board)
        return json.loads(cfg.read_text())

    def rename_with_upstream_preinit(self):
        support = Path(os.environ["OPENWRT_TEST_SUPPORT"])
        preinit = (support / "lib_preinit_10_indicate_preinit").read_text()
        preinit = preinit.replace("/bin/board_detect /tmp/board.json", ":")
        preinit = preinit.replace("/tmp/board.json", str(self.base / "board.json"))
        preinit = preinit.replace("/sys/", str(self.sysfs) + "/")
        preinit = preinit.replace("/proc/net/dev", str(self.base / "net-dev"))
        preinit = preinit.replace("/usr/share/libubox/jshn.sh", os.environ["JSHN_SHELL"])
        mock = self.base / "mock-ip.py"
        mock.write_text("""import os, pathlib, sys
args = sys.argv[1:]
if args[:2] != ['link', 'set']: raise SystemExit('unexpected ip call')
if 'name' not in args: raise SystemExit(0)
root = pathlib.Path(os.environ['CUSTOM_X86_SYSFS']) / 'class/net'
old, new = args[2], args[-1]
link = root / old
target = link.resolve()
if (root / new).exists(): raise SystemExit('name collision')
target.rename(target.with_name(new))
link.unlink()
(root / new).symlink_to(target.with_name(new))
""")
        self.run_shell("boot_hook_add() { :; };\n" + preinit +
                       f"\nip() {{ python3 {shlex.quote(str(mock))} \"$@\"; }}\n"
                       "preinit_ip_config() { :; }; preinit_config_board")

    def test_scrambled_names_and_repeated_boot(self):
        self.hardware(names={"wan0": "eth0", "lan1": "eth4", "lan2": "eth3", "lan3": "eth1", "lan4": "eth2"})
        for _ in range(2):
            board = self.board()
            self.assertEqual(set(board["network_device"]), {row[0] for row in PORTS})
            self.assertEqual(board["network"]["lan"]["ports"], ["lan1", "lan2", "lan3", "lan4"])
            self.assertEqual(board["network"]["wan"]["device"], "wan0")
            self.rename_with_upstream_preinit()
            for name, pci, _, _ in PORTS:
                self.assertEqual((self.sysfs / "class/net" / name).resolve().parent.parent.name, pci)

    def test_missing_wan_keeps_lan_separate(self):
        self.hardware(missing_wan=True)
        board = self.board()
        self.assertNotIn("wan0", board["network_device"])
        self.rename_with_upstream_preinit()
        self.assertFalse((self.sysfs / "class/net/wan0").exists())
        self.assertEqual(set(p.name for p in (self.sysfs / "class/net").iterdir()), {"lan1", "lan2", "lan3", "lan4"})

    def test_wrong_driver_is_not_assigned_to_wan(self):
        self.hardware(wrong_driver=True)
        self.assertNotIn("wan0", self.board()["network_device"])

    def test_ambiguous_pci_port_is_not_assigned_to_wan(self):
        self.hardware()
        (self.sysfs / "devices/pci0000:00/0000:02:00.0/net/eth99").mkdir()
        self.assertNotIn("wan0", self.board()["network_device"])


if __name__ == "__main__":
    for tool in ("uci", "jshn", "busybox"):
        if not shutil.which(tool):
            raise SystemExit(f"Missing test dependency: {tool}")
    for variable in ("OPENWRT_TEST_SUPPORT", "JSHN_SHELL"):
        if variable not in os.environ:
            raise SystemExit(f"Set {variable}; see this file's docstring")
    unittest.main(verbosity=2)
