#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
POCO-TUI  -  Dashboard TUI + kontrol Mini Hotspot Gateway untuk Poco F1
(postmarketOS / distro Linux lain berbasis Alpine, Debian, dll).

Dikontrol sepenuhnya dengan 3 tombol fisik:
    VOL+  : naik / sebelumnya
    VOL-  : turun / berikutnya
    POWER : tekan singkat = pilih / OK,  tahan = kembali / aksi cepat

Tanpa dependensi pip - hanya Python 3 (curses) + hostapd, dnsmasq,
iptables atau nftables, iproute2.

Jalankan:   sudo python3 pocotui.py          (perangkat sebenarnya, perlu root)
            python3 pocotui.py --demo         (pratinjau di PC, data simulasi)
"""

import argparse
import collections
import curses
import fcntl
import glob
import ipaddress
import json
import locale
import math
import os
import queue
import random
import re
import secrets
import select
import shutil
import signal
import string
import struct
import subprocess
import sys
import threading
import time

VERSION = "1.0"
LONG_PRESS = 0.7                       # detik, POWER ditahan = "kembali"
EV_KEY = 1
KEY_POWER, KEY_VOLDOWN, KEY_VOLUP = 116, 114, 115
EVIOCGRAB = 0x40044590
EVENT = struct.Struct("llHHi")         # struct input_event (native)
MAC_RE = re.compile(r"^[0-9a-fA-F]{2}(:[0-9a-fA-F]{2}){5}$")

RUN = "/run/pocotui"                   # diatur ulang di main()


# ----------------------------------------------------------------------------
# Util
# ----------------------------------------------------------------------------
def which(x):
    return shutil.which(x)


def sh(cmd, timeout=6, inp=None):
    try:
        p = subprocess.run(cmd, shell=isinstance(cmd, str), input=inp,
                           capture_output=True, text=True, timeout=timeout)
        return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()
    except subprocess.TimeoutExpired:
        return 124, "timeout"
    except Exception as e:  # noqa
        return 127, str(e)


def rd(path, default=""):
    try:
        with open(path) as f:
            return f.read().strip()
    except Exception:
        return default


def rdi(path, default=None):
    try:
        return int(rd(path))
    except Exception:
        return default


def wr(path, val):
    try:
        with open(path, "w") as f:
            f.write(str(val))
        return True
    except Exception:
        return False


def fmt_bytes(n):
    n = float(n or 0)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return ("%.0f %s" % (n, u)) if u == "B" else ("%.1f %s" % (n, u))
        n /= 1024.0


def fmt_rate(n):
    return fmt_bytes(n) + "/s"


def fmt_dur(s):
    s = int(max(0, s))
    return "%02d:%02d:%02d" % (s // 3600, (s % 3600) // 60, s % 60)


def list_ifaces():
    try:
        return [n for n in sorted(os.listdir("/sys/class/net")) if n != "lo"]
    except OSError:
        return []


def is_wifi(n):
    return (os.path.isdir("/sys/class/net/%s/wireless" % n)
            or os.path.isdir("/sys/class/net/%s/phy80211" % n))


def default_route_dev(exclude=None):
    rc, out = sh("ip route show default", timeout=2)
    for line in out.splitlines():
        m = re.search(r"dev (\S+)", line)
        if m and m.group(1) != exclude:
            return m.group(1)
    return None


def net_counters():
    out = {}
    try:
        for line in open("/proc/net/dev").read().splitlines()[2:]:
            n, rest = line.split(":", 1)
            f = rest.split()
            out[n.strip()] = (int(f[0]), int(f[8]))
    except Exception:
        pass
    return out


# ----------------------------------------------------------------------------
# Konfigurasi
# ----------------------------------------------------------------------------
DEFAULTS = dict(
    ssid="PocoF1-Hotspot", psk=None, band="2.4", channel=6, country="ID",
    hidden=False, max_clients=8, wlan="auto", upstream="auto",
    dns=["1.1.1.1", "1.0.0.1"], subnet="192.168.50.0/24", nat=True,
    autostart=False, brightness=60, screen_timeout=60, theme="cyan",
    ascii=False, refresh=2, blocked=[],
    virtual_ap=True, ap_name="ap0",     # AP di interface virtual; wlan0 tetap bebas
)


class Config:
    def __init__(self, path):
        self.path = path
        self.d = dict(DEFAULTS)
        self.d["dns"] = list(DEFAULTS["dns"])
        self.d["blocked"] = []
        try:
            with open(path) as f:
                self.d.update(json.load(f))
        except Exception:
            pass
        if self.d.get("psk") is None:          # kali pertama: sandi acak
            al = string.ascii_letters + string.digits
            self.d["psk"] = "".join(secrets.choice(al) for _ in range(10))
            self.save()

    def __getitem__(self, k):
        return self.d[k]

    def __setitem__(self, k, v):
        self.d[k] = v

    def save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.d, f, indent=2)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except Exception:
            pass

    def reset(self):
        try:
            os.remove(self.path)
        except OSError:
            pass
        self.d = dict(DEFAULTS)
        self.d["dns"] = list(DEFAULTS["dns"])
        self.d["blocked"] = []
        self.d["psk"] = None
        al = string.ascii_letters + string.digits
        self.d["psk"] = "".join(secrets.choice(al) for _ in range(10))
        self.save()


# ----------------------------------------------------------------------------
# Tombol fisik (evdev)
# ----------------------------------------------------------------------------
def find_key_devices():
    """Cari /dev/input/event* yang punya POWER / VOL+ / VOL-."""
    res = []
    wbits = struct.calcsize("l") * 8
    for ev in sorted(glob.glob("/sys/class/input/event*")):
        caps = rd(ev + "/device/capabilities/key")
        if not caps:
            continue
        try:
            words = [int(w, 16) for w in caps.split()]
        except ValueError:
            continue
        bits = set()
        for i, w in enumerate(reversed(words)):
            for b in range(wbits):
                if (w >> b) & 1:
                    bits.add(i * wbits + b)
        if not bits & {KEY_POWER, KEY_VOLUP, KEY_VOLDOWN}:
            continue
        is_keyboard = 30 in bits               # punya tombol 'A'
        res.append(("/dev/input/" + os.path.basename(ev),
                    rd(ev + "/device/name"), not is_keyboard))
    return res


class Buttons(threading.Thread):
    def __init__(self, q, grab=True):
        super().__init__(daemon=True)
        self.q = q
        self.grab = grab
        self.stop_flag = False
        self.devices = []       # untuk info/debug
        self.error = ""

    def run(self):
        fds = {}
        try:
            for path, name, can_grab in find_key_devices():
                try:
                    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
                except OSError as e:
                    self.error = "%s: %s" % (path, e)
                    continue
                if self.grab and can_grab:
                    try:
                        fcntl.ioctl(fd, EVIOCGRAB, 1)
                    except OSError:
                        pass
                fds[fd] = path
                self.devices.append("%s (%s)" % (path, name))
        except Exception as e:  # noqa
            self.error = str(e)
        down_at, fired = None, False
        while not self.stop_flag:
            tout = 0.05 if down_at else 0.4
            try:
                r, _, _ = select.select(list(fds), [], [], tout)
            except (OSError, ValueError):
                break
            for fd in r:
                try:
                    data = os.read(fd, EVENT.size * 32)
                except BlockingIOError:
                    continue
                except OSError:
                    fds.pop(fd, None)
                    continue
                for off in range(0, len(data) - EVENT.size + 1, EVENT.size):
                    _, _, typ, code, val = EVENT.unpack_from(data, off)
                    if typ != EV_KEY:
                        continue
                    if code == KEY_VOLUP and val in (1, 2):
                        self.q.put("up")
                    elif code == KEY_VOLDOWN and val in (1, 2):
                        self.q.put("down")
                    elif code == KEY_POWER:
                        if val == 1:
                            down_at, fired = time.time(), False
                        elif val == 0:
                            if down_at and not fired:
                                self.q.put("ok")
                            down_at = None
            if down_at and not fired and time.time() - down_at >= LONG_PRESS:
                self.q.put("back")
                fired = True
        for fd in fds:                          # menutup fd = melepas grab
            try:
                os.close(fd)
            except OSError:
                pass


# ----------------------------------------------------------------------------
# Layar / backlight
# ----------------------------------------------------------------------------
class Display:
    def __init__(self, demo):
        self.demo = demo
        devs = sorted(glob.glob("/sys/class/backlight/*"))
        self.bl = devs[0] if devs else None
        self.max = rdi(self.bl + "/max_brightness", 255) if self.bl else None
        self.blanked = False

    def set_pct(self, pct):
        if self.demo or not self.bl:
            return
        wr(self.bl + "/brightness", max(1, int(self.max * pct / 100.0)))

    def blank(self, on, pct):
        self.blanked = on
        if self.demo:
            return
        if self.bl:
            wr(self.bl + "/brightness", 0 if on else max(1, int(self.max * pct / 100.0)))
        else:
            wr("/sys/class/graphics/fb0/blank", 1 if on else 0)


# ----------------------------------------------------------------------------
# Hotspot
# ----------------------------------------------------------------------------
class Hotspot:
    def __init__(self, app):
        self.app = app
        self.state = "off"       # off / starting / on / stopping / error
        self.error = ""
        self.started = 0.0
        self.ap = None
        self.eff = None          # (band, channel) yang benar-benar dipakai
        self.fwd_changed = False
        self.base = (0, 0)
        self.adopt()

    # -- bantu -------------------------------------------------------------
    @property
    def cfg(self):
        return self.app.cfg

    def pid(self, name):
        p = rdi("%s/%s.pid" % (RUN, name))
        return p if p and os.path.exists("/proc/%d" % p) else None

    def adopt(self):
        """Bila TUI di-restart saat hotspot masih hidup."""
        if self.app.demo:
            return
        if self.pid("hostapd") and self.pid("dnsmasq"):
            self.state = "on"
            self.ap = self.ap_iface()
            try:
                self.started = os.path.getmtime("%s/hostapd.pid" % RUN)
            except OSError:
                self.started = time.time()

    def phy_iface(self):
        """Interface WiFi fisik (induk) - biasanya wlan0, tetap dipakai sebagai klien."""
        w = self.cfg["wlan"]
        if w != "auto" and os.path.isdir("/sys/class/net/" + w):
            return w
        wifis = [n for n in list_ifaces() if is_wifi(n) and n != self.cfg["ap_name"]]
        if "wlan0" in wifis:
            return "wlan0"
        return wifis[0] if wifis else None

    def ap_iface(self):
        """Nama interface tempat AP berjalan (ap0 virtual, atau langsung wlan0)."""
        return self.cfg["ap_name"] if self.cfg["virtual_ap"] else self.phy_iface()

    def sta_freq(self, phy):
        rc, out = sh("iw dev %s link" % phy, timeout=3)
        m = re.search(r"freq:\s*(\d+)", out)
        return int(m.group(1)) if m else None

    def upstream_iface(self, ap):
        u = self.cfg["upstream"]
        if u != "auto":
            return u if os.path.isdir("/sys/class/net/" + u) else None
        dev = default_route_dev(exclude=ap)
        if dev:
            return dev
        for n in list_ifaces():
            if n == ap or n.startswith(("docker", "veth", "br-", "virbr", "tun", "tap")):
                continue
            if rd("/sys/class/net/%s/operstate" % n) in ("up", "unknown"):
                return n
        return None

    def net(self):
        return ipaddress.ip_network(self.cfg["subnet"], strict=False)

    def gw(self):
        return str(self.net().network_address + 1)

    def cli(self, args, timeout=3):
        ap = self.ap or self.ap_iface()
        return sh("hostapd_cli -p %s/hostapd -i %s %s" % (RUN, ap, args), timeout)

    # -- konfigurasi hostapd / dnsmasq -----------------------------------------
    def write_hostapd(self, ap):
        c = self.cfg
        band, chan = self.eff or (c["band"], int(c["channel"]))
        five = band == "5"
        lines = [
            "interface=%s" % ap, "driver=nl80211", "ssid=%s" % c["ssid"],
            "country_code=%s" % c["country"], "ieee80211d=1",
            "hw_mode=%s" % ("a" if five else "g"), "channel=%d" % int(chan),
            "ieee80211n=1", "wmm_enabled=1", "max_num_sta=%d" % int(c["max_clients"]),
            "ignore_broadcast_ssid=%d" % (1 if c["hidden"] else 0),
            "ctrl_interface=%s/hostapd" % RUN, "ctrl_interface_group=0",
            "macaddr_acl=0", "deny_mac_file=%s/hostapd.deny" % RUN,
            "logger_syslog=-1", "logger_syslog_level=2",
        ]
        if five:
            lines += ["ieee80211ac=1"]
        if c["psk"]:
            lines += ["auth_algs=1", "wpa=2", "wpa_key_mgmt=WPA-PSK",
                      "rsn_pairwise=CCMP", "wpa_passphrase=%s" % c["psk"]]
        with open("%s/hostapd.deny" % RUN, "w") as f:
            f.write("".join(m + "\n" for m in c["blocked"]))
        with open("%s/hostapd.conf" % RUN, "w") as f:
            f.write("\n".join(lines) + "\n")
        os.chmod("%s/hostapd.conf" % RUN, 0o600)

    def write_dnsmasq(self, ap):
        net = self.net()
        gw = self.gw()
        first = net.network_address + 10
        last = net.network_address + min(net.num_addresses - 2, 110)
        lines = [
            "interface=%s" % ap, "bind-dynamic", "listen-address=%s" % gw,
            "except-interface=lo",
            "dhcp-authoritative",
            "dhcp-broadcast",
            "dhcp-range=%s,%s,%s,12h" % (first, last, net.netmask),
            "dhcp-option=option:router,%s" % gw,
            "dhcp-option=option:dns-server,%s" % gw,
            "no-resolv", "domain-needed", "bogus-priv", "cache-size=500",
            "dhcp-leasefile=%s/dnsmasq.leases" % RUN,
            "pid-file=%s/dnsmasq.pid" % RUN,
            "log-facility=%s/dnsmasq.log" % RUN,
            "log-dhcp",
        ]
        lines += ["server=%s" % d for d in self.cfg["dns"]]
        with open("%s/dnsmasq.conf" % RUN, "w") as f:
            f.write("\n".join(lines) + "\n")

    # -- firewall ------------------------------------------------------------
    def fw_apply(self, ap, up):
        net = str(self.net())
        nat = self.cfg["nat"]
        if which("iptables"):
            o = "-o %s" % up if up else "! -o %s" % ap
            i = "-i %s" % up if up else "! -i %s" % ap
            cmds = [
                "iptables -N POCOTUI_IN", "iptables -N POCOTUI_FWD",
                "iptables -t nat -N POCOTUI",
                "iptables -A POCOTUI_IN -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT",
                "iptables -A POCOTUI_IN -i %s -p udp -m multiport --dports 53,67 -j ACCEPT" % ap,
                "iptables -A POCOTUI_IN -i %s -p tcp --dport 53 -j ACCEPT" % ap,
                "iptables -A POCOTUI_IN -i %s -p udp --dport 68 -j ACCEPT" % ap,
                "iptables -I INPUT -j POCOTUI_IN",
            ]
            if nat:
                cmds += [
                    "iptables -A POCOTUI_FWD -i %s %s -j ACCEPT" % (ap, o),
                    "iptables -A POCOTUI_FWD %s -o %s -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT" % (i, ap),
                    "iptables -t nat -A POCOTUI -s %s %s -j MASQUERADE" % (net, o),
                ]
            cmds += ["iptables -I FORWARD -j POCOTUI_FWD",
                     "iptables -t nat -I POSTROUTING -j POCOTUI"]
            for c in cmds:
                rc, out = sh(c)
                if rc != 0:
                    raise RuntimeError("iptables: %s" % out[-100:])
        elif which("nft"):
            oif = 'oifname "%s"' % up if up else 'oifname != "%s"' % ap
            iif = 'iifname "%s"' % up if up else 'iifname != "%s"' % ap
            script = ["table ip pocotui {",
                      " chain input { type filter hook input priority -1; policy accept;",
                      "  ct state established,related accept",
                      '  iifname "%s" udp dport { 53, 67, 68 } accept' % ap,
                      '  iifname "%s" tcp dport 53 accept }' % ap,
                      " chain forward { type filter hook forward priority -1; policy accept;"]
            if nat:
                script += ['  iifname "%s" %s accept' % (ap, oif),
                           "  %s oifname \"%s\" ct state established,related accept" % (iif, ap)]
            script += [" }",
                       " chain post { type nat hook postrouting priority 100; policy accept;"]
            if nat:
                script += ["  ip saddr %s %s masquerade" % (net, oif)]
            script += [" }", "}"]
            rc, out = sh("nft -f -", inp="\n".join(script) + "\n")
            if rc != 0:
                raise RuntimeError("nft: %s" % out[-100:])
            # Sistem modern (postmarketOS/Alpine) memakai nftables inet filter
            # dengan policy drop pada chain forward/input. Tabel pocotui terpisah
            # tidak cukup: aturan accept harus masuk ke tabel inet filter agar
            # trafik dari/ke AP (ap0) tidak di-drop. (bug: klien dapat IP tapi
            # tidak ada internet, termasuk dengan IP statis)
            for cmd in ("nft insert rule inet filter input iifname \"%s\" accept" % ap,
                        "nft insert rule inet filter forward iifname \"%s\" accept" % ap,
                        "nft insert rule inet filter forward oifname \"%s\" ct state established,related accept" % ap):
                sh(cmd)
        else:
            raise RuntimeError("iptables / nftables belum terpasang")

    def fw_clear(self):
        if which("iptables"):
            for c in ["iptables -D INPUT -j POCOTUI_IN", "iptables -F POCOTUI_IN",
                      "iptables -X POCOTUI_IN", "iptables -D FORWARD -j POCOTUI_FWD",
                      "iptables -F POCOTUI_FWD", "iptables -X POCOTUI_FWD",
                      "iptables -t nat -D POSTROUTING -j POCOTUI",
                      "iptables -t nat -F POCOTUI", "iptables -t nat -X POCOTUI"]:
                sh(c)
        if which("nft"):
            sh("nft delete table ip pocotui")

    # -- start / stop --------------------------------------------------------
    def start(self):
        app, c = self.app, self.cfg
        self.state, self.error = "starting", ""
        if app.demo:
            time.sleep(1.2)
            self.ap = c["ap_name"] if c["virtual_ap"] else "wlan0"
            self.eff = (c["band"], int(c["channel"]))
            self.state, self.started = "on", time.time()
            app.log("(demo) hotspot aktif")
            return
        try:
            if os.geteuid() != 0:
                raise RuntimeError("butuh root (jalankan dengan sudo/doas)")
            for b in ("hostapd", "dnsmasq", "ip"):
                if not which(b):
                    raise RuntimeError("%s belum terpasang" % b)
            phy = self.phy_iface()
            if not phy:
                raise RuntimeError("interface WiFi tidak ditemukan")
            virtual = bool(c["virtual_ap"])
            ap = c["ap_name"] if virtual else phy
            self.ap = ap
            self.eff = (c["band"], int(c["channel"]))
            self._cleanup(ap, quiet=True)
            os.makedirs(RUN, exist_ok=True)
            up = self.upstream_iface(ap) if c["upstream"] == "auto" else c["upstream"]
            up_rule = None if c["upstream"] == "auto" else c["upstream"]
            app.log("AP=%s upstream=%s" % (ap, up or "(belum ada)"))

            sh("rfkill unblock wifi")
            if virtual:
                # wlan0 TIDAK disentuh: AP dibuat di interface virtual terpisah
                if not which("iw"):
                    raise RuntimeError("iw belum terpasang (apk add iw)")
                rc, out = sh("iw dev %s interface add %s type __ap" % (phy, ap))
                if rc:
                    raise RuntimeError("gagal membuat %s di %s: %s (driver mungkin tak mendukung AP+STA)"
                                       % (ap, phy, out[-70:]))
                try:                          # MAC harus beda dari wlan0
                    o = [int(x, 16) for x in rd("/sys/class/net/%s/address" % phy).split(":")]
                    o[0] |= 0x02
                    o[5] = (o[5] + 1) & 0xFF
                    sh("ip link set %s address %s" % (ap, ":".join("%02x" % x for x in o)))
                except Exception:
                    pass
                if which("nmcli"):
                    sh("nmcli dev set %s managed no" % ap)
                f = self.sta_freq(phy)        # radio tunggal: channel AP harus = channel wlan0
                if f:
                    band = "2.4" if f < 3000 else "5"
                    ch = (f - 2407) // 5 if f < 3000 else (f - 5000) // 5
                    if f == 2484:
                        ch = 14
                    if (band, ch) != self.eff:
                        app.log("channel mengikuti %s: CH %d (%s GHz)" % (phy, ch, band))
                    self.eff = (band, ch)
            else:
                if which("nmcli"):
                    sh("nmcli dev set %s managed no" % ap)
                if which("wpa_cli"):
                    sh("wpa_cli -i %s terminate" % ap)
            net = self.net()
            sh("ip link set %s down" % ap)
            sh("ip addr flush dev %s" % ap)
            rc, out = sh("ip addr add %s/%d dev %s" % (self.gw(), net.prefixlen, ap))
            if rc:
                raise RuntimeError("ip addr: %s" % out[-80:])
            sh("ip link set %s up" % ap)

            self.write_hostapd(ap)
            self.write_dnsmasq(ap)
            for f in ("hostapd.log",):
                open("%s/%s" % (RUN, f), "w").close()
            rc, out = sh("hostapd -B -P %s/hostapd.pid -f %s/hostapd.log %s/hostapd.conf"
                         % (RUN, RUN, RUN), timeout=10)
            if rc:
                tail = rd("%s/hostapd.log" % RUN).splitlines()[-1:] or [out[-100:]]
                raise RuntimeError("hostapd gagal: %s" % tail[0][-90:])
            ok = False
            for _ in range(10):
                time.sleep(0.5)
                rc, out = self.cli("status")
                if "state=ENABLED" in out:
                    ok = True
                    break
                if not self.pid("hostapd"):
                    break
            if not ok:
                tail = rd("%s/hostapd.log" % RUN).splitlines()[-1:] or ["tidak ENABLED"]
                raise RuntimeError("AP tidak naik: %s" % tail[0][-90:])

            rc, out = sh("dnsmasq -C %s/dnsmasq.conf" % RUN)
            if rc:
                raise RuntimeError("dnsmasq: %s" % out[-100:])

            prev = rd("/proc/sys/net/ipv4/ip_forward")
            if prev != "1":
                wr("/proc/sys/net/ipv4/ip_forward", "1")
                self.fwd_changed = True
            self.fw_apply(ap, up_rule)
            cn = net_counters().get(ap, (0, 0))
            self.base = cn
            self.state, self.started = "on", time.time()
            app.log("hotspot aktif: %s" % c["ssid"])
            if not up:
                app.log("PERINGATAN: belum ada upstream/internet")
        except Exception as e:
            self.state, self.error = "error", str(e)
            app.log("ERROR start: %s" % e)
            try:
                self._cleanup(self.ap, quiet=True)
            except Exception:
                pass
            self.state, self.error = "error", str(e)
            raise

    def _cleanup(self, ap, quiet=False):
        for n in ("dnsmasq", "hostapd"):
            p = self.pid(n)
            if p:
                try:
                    os.kill(p, signal.SIGTERM)
                except OSError:
                    pass
        sh("pkill -f '%s/hostapd.conf'" % RUN)
        time.sleep(0.4)
        self.fw_clear()
        if self.fwd_changed:
            wr("/proc/sys/net/ipv4/ip_forward", "0")
            self.fwd_changed = False
        if ap and self.cfg["virtual_ap"] and ap == self.cfg["ap_name"]:
            sh("ip link set %s down" % ap)          # hanya ap0 yang dihapus; wlan0 aman
            sh("iw dev %s del" % ap)
        elif ap:
            sh("ip addr flush dev %s" % ap)
            if which("nmcli"):
                sh("nmcli dev set %s managed yes" % ap)
        for n in ("dnsmasq", "hostapd"):
            try:
                os.remove("%s/%s.pid" % (RUN, n))
            except OSError:
                pass

    def stop(self):
        self.state = "stopping"
        if self.app.demo:
            time.sleep(0.8)
            self.state = "off"
            self.app.log("(demo) hotspot dimatikan")
            return
        self._cleanup(self.ap or self.ap_iface())
        self.state, self.error = "off", ""
        self.app.log("hotspot dimatikan")


# ----------------------------------------------------------------------------
# Statistik sistem (thread terpisah)
# ----------------------------------------------------------------------------
class Stats(threading.Thread):
    def __init__(self, app):
        super().__init__(daemon=True)
        self.app = app
        self.d = {}
        self._cpu, self._net = {}, {}
        self.hd = collections.deque(maxlen=240)     # histori down ke klien
        self.hu = collections.deque(maxlen=240)     # histori up dari klien
        self.ping = (None, 0.0)
        self._t0 = time.time()

    def run(self):
        threading.Thread(target=self._pinger, daemon=True).start()
        while True:
            try:
                self.d = self.sample_demo() if self.app.demo else self.sample()
            except Exception as e:  # noqa
                self.app.log("stats err: %s" % e)
            time.sleep(max(1, self.app.cfg["refresh"]))

    def _pinger(self):
        while True:
            if self.app.demo:
                self.ping = (round(random.uniform(18, 42), 1), time.time())
            elif not self.app.display.blanked:
                rc, out = sh("ping -c 1 -W 2 1.1.1.1", timeout=4)
                m = re.search(r"time[=<]([\d.]+)", out)
                self.ping = (float(m.group(1)) if rc == 0 and m else None, time.time())
            time.sleep(10)

    # -- sampel nyata --------------------------------------------------------
    def sample(self):
        app, hs = self.app, self.app.hs
        now = time.time()
        d = {"ts": now}
        cores = []
        try:
            for line in open("/proc/stat"):
                if not line.startswith("cpu"):
                    break
                p = line.split()
                v = [int(x) for x in p[1:9]]
                idle, tot = v[3] + v[4], sum(v)
                pi, pt = self._cpu.get(p[0], (idle, tot))
                self._cpu[p[0]] = (idle, tot)
                pct = 100.0 * (1 - (idle - pi) / float(tot - pt)) if tot > pt else 0.0
                if p[0] == "cpu":
                    d["cpu"] = pct
                else:
                    cores.append(pct)
        except OSError:
            pass
        d["cores"] = cores
        fr = [rdi(p) for p in sorted(glob.glob("/sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_cur_freq"))]
        fr = [x / 1000.0 for x in fr if x]
        d["freq"] = max(fr) if fr else None
        try:
            mi = {}
            for line in open("/proc/meminfo"):
                k, v = line.split(":", 1)
                mi[k] = int(v.split()[0]) * 1024
            d["mem"] = (mi.get("MemTotal", 0), mi.get("MemAvailable", mi.get("MemFree", 0)))
            d["swap"] = (mi.get("SwapTotal", 0), mi.get("SwapFree", 0))
        except Exception:
            pass
        temps = []
        for z in glob.glob("/sys/class/thermal/thermal_zone*"):
            t = rdi(z + "/temp")
            if t is None:
                continue
            t = t / 1000.0 if abs(t) > 1000 else float(t)
            if -20 < t < 150:
                temps.append((rd(z + "/type"), t))
        cpu_t = [t for n, t in temps if re.search("cpu|tsens|soc|gpu", n)]
        d["temp"] = max(cpu_t) if cpu_t else (max(t for _, t in temps) if temps else None)
        bat, charger = {}, False
        for ps in glob.glob("/sys/class/power_supply/*"):
            typ = rd(ps + "/type")
            if typ == "Battery" and not bat:
                i, v = rdi(ps + "/current_now"), rdi(ps + "/voltage_now")
                bat = {"pct": rdi(ps + "/capacity"), "status": rd(ps + "/status"),
                       "w": (abs(i) * abs(v) / 1e12) if i and v else None}
            elif typ != "Battery" and rdi(ps + "/online"):
                charger = True
        d["bat"], d["charger"] = bat, charger
        try:
            d["load"] = os.getloadavg()
            d["uptime"] = float(rd("/proc/uptime").split()[0])
            du = shutil.disk_usage("/")
            d["disk"] = (du.total, du.used)
        except Exception:
            pass

        # jaringan
        cn = net_counters()
        ap = hs.ap or hs.ap_iface()
        up = hs.upstream_iface(ap) if ap else None

        def rate(iface):
            if not iface or iface not in cn:
                return (0.0, 0.0)
            rx, tx = cn[iface]
            pt, prx, ptx = self._net.get(iface, (now, rx, tx))
            self._net[iface] = (now, rx, tx)
            dt = max(0.2, now - pt)
            return (max(0.0, (rx - prx) / dt), max(0.0, (tx - ptx) / dt))

        a_rx, a_tx = rate(ap)
        u_rx, u_tx = rate(up)
        self.hd.append(a_tx)
        self.hu.append(a_rx)
        d["down"], d["up"] = a_tx, a_rx
        d["u_down"], d["u_up"] = u_rx, u_tx
        d["hd"], d["hu"] = list(self.hd), list(self.hu)
        if ap in cn:
            d["tot_down"] = max(0, cn[ap][1] - hs.base[1])
            d["tot_up"] = max(0, cn[ap][0] - hs.base[0])
        d["ap"], d["upif"] = ap, up
        ips = {}
        rc, out = sh("ip -4 -o addr show", timeout=2)
        for m in re.finditer(r"^\d+:\s+(\S+)\s+inet\s+([\d.]+)/(\d+)", out, re.M):
            ips[m.group(1)] = "%s/%s" % (m.group(2), m.group(3))
        d["ips"] = ips
        d["clients"] = self.get_clients() if hs.state == "on" else []
        d["ping"] = self.ping
        return d

    def get_clients(self):
        hs = self.app.hs
        stas = {}
        rc, out = hs.cli("all_sta", timeout=2)
        if rc == 0:
            cur = None
            for line in out.splitlines():
                line = line.strip()
                if MAC_RE.match(line):
                    cur = line.lower()
                    stas[cur] = {}
                elif cur and "=" in line:
                    k, v = line.split("=", 1)
                    stas[cur][k] = v
        leases = {}
        for line in rd("%s/dnsmasq.leases" % RUN).splitlines():
            p = line.split()
            if len(p) >= 4:
                leases[p[1].lower()] = (p[2], "" if p[3] == "*" else p[3])
        macs = list(stas) if rc == 0 else list(leases)
        res = []
        for m in macs:
            s = stas.get(m, {})
            ip, name = leases.get(m, ("-", ""))
            sig = s.get("signal")
            res.append({
                "mac": m, "ip": ip, "name": name,
                "sig": int(sig) if sig and re.match(r"-?\d+$", sig) else None,
                "rx": int(s.get("rx_bytes", 0) or 0), "tx": int(s.get("tx_bytes", 0) or 0),
                "t": int(s.get("connected_time", 0) or 0),
            })
        return res

    # -- sampel demo ---------------------------------------------------------
    def sample_demo(self):
        t = time.time() - self._t0
        on = self.app.hs.state == "on"
        d = {"ts": time.time()}
        d["cores"] = [max(2, min(98, 30 + 25 * math.sin(t / 3 + i) + random.uniform(-8, 8))) for i in range(8)]
        d["cpu"] = sum(d["cores"]) / 8
        d["freq"] = 1400 + 900 * (d["cpu"] / 100)
        d["temp"] = 41 + 8 * math.sin(t / 20) + (4 if on else 0)
        d["mem"] = (5.7 * 2 ** 30, (2.9 + 0.4 * math.sin(t / 15)) * 2 ** 30)
        d["swap"] = (2 * 2 ** 30, 1.8 * 2 ** 30)
        d["bat"] = {"pct": max(5, 83 - int(t / 40) % 60), "status": "Charging" if int(t / 30) % 2 else "Discharging", "w": 2.6 + random.random()}
        d["charger"] = True
        d["load"] = (0.4 + 0.3 * random.random(), 0.5, 0.45)
        d["uptime"] = 7300 + t
        d["disk"] = (56 * 2 ** 30, 21 * 2 ** 30)
        down = (1.4e6 * (1 + math.sin(t / 4)) + random.uniform(0, 3e5)) if on else 0
        up = (2.2e5 * (1 + math.sin(t / 5 + 1)) + random.uniform(0, 5e4)) if on else 0
        self.hd.append(down)
        self.hu.append(up)
        d.update(down=down, up=up, u_down=up, u_up=down, hd=list(self.hd), hu=list(self.hu),
                 tot_down=int(t * 4e5) if on else 0, tot_up=int(t * 6e4) if on else 0,
                 ap="ap0", upif="wlan0", ips={"ap0": "192.168.50.1/24", "wlan0": "10.42.0.14/24"})
        d["clients"] = [
            {"mac": "a4:5e:60:12:34:56", "ip": "192.168.50.23", "name": "iPhone-Rina", "sig": -48, "rx": 3_200_000, "tx": 41_000_000, "t": int(t) + 600},
            {"mac": "3c:22:fb:aa:bb:cc", "ip": "192.168.50.31", "name": "laptop-dimas", "sig": -61, "rx": 9_000_000, "tx": 120_000_000, "t": int(t) + 120},
            {"mac": "f0:9f:c2:77:88:99", "ip": "192.168.50.44", "name": "", "sig": -72, "rx": 400_000, "tx": 2_000_000, "t": int(t)},
        ] if on else []
        d["ping"] = self.ping
        return d


# ----------------------------------------------------------------------------
# Layar-layar UI
# ----------------------------------------------------------------------------
class Item:
    def __init__(self, label, action=None, value=None, danger=False, enabled=True):
        self.label, self.action, self.value = label, action, value
        self.danger, self.enabled = danger, enabled


class MenuScreen:
    hints = [("VOL+", "Naik"), ("VOL-", "Turun"), ("PWR", "Pilih"), ("PWR tahan", "Kembali")]

    def __init__(self, title, build):
        self.title, self.build, self.sel = title, build, 0

    def key(self, app, ev):
        items = self.build(app)
        n = len(items)
        if not n:
            if ev in ("ok", "back"):
                app.pop()
            return
        self.sel = min(self.sel, n - 1)
        if ev == "up":
            self.sel = (self.sel - 1) % n
        elif ev == "down":
            self.sel = (self.sel + 1) % n
        elif ev == "ok":
            it = items[self.sel]
            if it.enabled and it.action:
                it.action(app)
            elif not it.enabled:
                app.toast("Tidak tersedia saat ini", "warn")
        elif ev == "back":
            app.pop()

    def draw(self, app):
        h, w = app.size()
        items = self.build(app)
        app.header(" %s " % self.title)
        rows = h - 4                        # baris 1 .. h-4
        n = len(items)
        self.sel = min(self.sel, max(0, n - 1))
        top = max(0, min(self.sel - rows // 2, n - rows))
        S, c = app.sym, app.c
        for r in range(rows):
            idx = top + r
            if idx >= n:
                break
            it = items[idx]
            y = 1 + r
            sel = idx == self.sel
            label = it.label
            val = it.value(app) if callable(it.value) else (it.value or "")
            base = c["hl"] if sel else (c["bad"] if it.danger else (c["wh"] if it.enabled else c["dim"]))
            app.put(y, 0, " " * w, base)
            app.put(y, 1, (S["sel"] + " " if sel else "  ") + label, base)
            if val:
                vx = max(len(label) + 6, w - len(val) - 2)
                app.put(y, vx, val[:max(0, w - vx - 1)], base if sel else c["acc"])
        if top > 0:
            app.put(1, w - 2, S["up"], c["acc_b"])
        if top + rows < n:
            app.put(h - 4, w - 2, S["dn"], c["acc_b"])
        app.footer(self.hints)


class EditScreen:
    CHARS = (string.ascii_lowercase + string.digits + string.ascii_uppercase
             + "-_.@#!$%&*+=?/:, ")
    ACTIONS = ["[SIMPAN]", "[HAPUS]", "[KOSONGKAN]", "[BATAL]"]
    hints = [("VOL+/-", "Pilih karakter"), ("PWR", "Ketik"), ("PWR tahan", "Hapus 1")]

    def __init__(self, title, text, on_done, validate=None, maxlen=63, note=""):
        self.title, self.text, self.on_done = title, text, on_done
        self.validate, self.maxlen, self.note = validate, maxlen, note
        self.ring = list(self.CHARS) + self.ACTIONS
        self.i = 0

    def label(self, t):
        return "SPASI" if t == " " else t

    def key(self, app, ev):
        n = len(self.ring)
        if ev == "up":
            self.i = (self.i - 1) % n
        elif ev == "down":
            self.i = (self.i + 1) % n
        elif ev == "back":
            self.text = self.text[:-1]
        elif ev == "ok":
            t = self.ring[self.i]
            if t == "[SIMPAN]":
                err = self.validate(self.text) if self.validate else None
                if err:
                    app.toast(err, "bad", 4)
                else:
                    app.pop()
                    self.on_done(self.text)
            elif t == "[HAPUS]":
                self.text = self.text[:-1]
            elif t == "[KOSONGKAN]":
                self.text = ""
            elif t == "[BATAL]":
                app.pop()
            elif len(self.text) < self.maxlen:
                self.text += t

    def draw(self, app):
        h, w = app.size()
        c = app.c
        app.header(" %s " % self.title)
        y = 2
        if self.note:
            app.put(y, 2, self.note[:w - 4], c["dim"])
        y += 2
        app.box(y, 1, 5, w - 2, "Teks  %d/%d" % (len(self.text), self.maxlen))
        shown = self.text[-(w - 8):]
        app.put(y + 2, 3, shown, c["wh_b"])
        if int(time.time() * 2) % 2 == 0:
            app.put(y + 2, 3 + len(shown), "_", c["acc_b"])
        y += 7
        app.put(y, 2, "Pilih karakter / aksi (putar dengan VOL+ / VOL-):", c["dim"])
        y += 2
        n = len(self.ring)
        cur = " " + self.label(self.ring[self.i]) + " "

        def pad(t):
            return "%-3s" % t if len(t) < 3 else t
        for span in range(5, 0, -1):          # pakai tetangga sebanyak yang muat
            left = [pad(self.label(self.ring[(self.i - k) % n])) for k in range(span, 0, -1)]
            right = [pad(self.label(self.ring[(self.i + k) % n])) for k in range(1, span + 1)]
            total = sum(len(p) + 1 for p in left) + len(cur) + 2 + sum(len(p) + 1 for p in right)
            if total <= w - 2:
                break
        x = max(1, (w - total) // 2)
        for t in left:
            app.put(y, x, t, c["dim"])
            x += len(t) + 1
        app.put(y, x, "[", c["acc_b"])
        app.put(y, x + 1, cur, c["hl"])
        app.put(y, x + 1 + len(cur), "]", c["acc_b"])
        cx = x + 1 + len(cur) // 2
        x += len(cur) + 3
        for t in right:
            app.put(y, x, t, c["dim"])
            x += len(t) + 1
        app.put(y + 1, cx, app.sym["up"], c["acc_b"])
        app.footer(self.hints)


class TextView:
    def __init__(self, title, lines, follow=False, ok_label="Tutup", on_ok=None):
        self.title, self.lines, self.follow = title, lines, follow
        self.stick, self.pos = follow, 0
        self.ok_label, self.on_ok = ok_label, on_ok

    @property
    def hints(self):
        return [("VOL+", "Atas"), ("VOL-", "Bawah"), ("PWR", self.ok_label), ("PWR tahan", "Kembali")]

    def get(self, app):
        return self.lines(app) if callable(self.lines) else self.lines

    def key(self, app, ev):
        h, _ = app.size()
        rows = h - 3
        maxpos = max(0, len(self.get(app)) - rows)
        if ev == "up":
            self.pos = max(0, (maxpos if self.stick else self.pos) - 1)
            self.stick = False
        elif ev == "down":
            self.pos = min(maxpos, self.pos + 1)
            self.stick = self.follow and self.pos >= maxpos
        elif ev == "ok":
            (self.on_ok or (lambda a: a.pop()))(app)
        elif ev == "back":
            app.pop()

    def draw(self, app):
        h, w = app.size()
        lines = self.get(app)
        rows = h - 3
        maxpos = max(0, len(lines) - rows)
        if self.stick:
            self.pos = maxpos
        self.pos = min(self.pos, maxpos)
        app.header(" %s " % self.title, "%d/%d " % (min(len(lines), self.pos + rows), len(lines)))
        for r in range(rows):
            i = self.pos + r
            if i >= len(lines):
                break
            app.put(1 + r, 1, lines[i][:w - 2], app.c["wh"])
        app.footer(self.hints)


class DashScreen:
    TABS = ["Ringkasan", "Klien", "Trafik", "Log"]
    hints = [("VOL+", "Tab kiri"), ("VOL-", "Tab kanan"), ("PWR", "Menu"), ("PWR tahan", "Hotspot")]

    def __init__(self):
        self.tab = 0

    def key(self, app, ev):
        if ev == "up":
            self.tab = (self.tab - 1) % len(self.TABS)
        elif ev == "down":
            self.tab = (self.tab + 1) % len(self.TABS)
        elif ev == "ok":
            app.push(main_menu(app))
        elif ev == "back":
            toggle_hotspot(app)

    def draw(self, app):
        h, w = app.size()
        hs, S = app.hs, app.sym
        d = app.stats.d or {}
        state = {"on": "AKTIF", "off": "MATI", "starting": "MEMULAI", "stopping": "MENGHENTIKAN",
                 "error": "ERROR"}[hs.state]
        bat = d.get("bat") or {}
        btxt = ""
        if bat.get("pct") is not None:
            chg = "+" if (bat.get("status") or "").lower().startswith("charg") else ""
            btxt = "%d%%%s  " % (bat["pct"], chg)
        app.header(" %s POCO-TUI v%s " % (S["logo"], VERSION),
                   "%s %s  %s%s " % (S["on"] if hs.state == "on" else S["off"], state, btxt, time.strftime("%H:%M")))
        app.tabbar(1, self.TABS, self.tab)
        y0, y1 = 2, h - 2
        [self.ov, self.cl, self.tr, self.lg][self.tab](app, d, y0, y1, w)
        app.footer(self.hints)

    # -- tab: ringkasan ------------------------------------------------------------
    def ov(self, app, d, y0, y1, w):
        two = w >= 100
        cw = w // 2 if two else w
        lw = cw - 1 if two else w            # sisakan 1 kolom celah antar panel
        inner = lw - 4
        panels = [("HOTSPOT", self.rows_hotspot(app, d, inner)),
                  ("JARINGAN", self.rows_net(app, d, inner)),
                  ("SISTEM", self.rows_sys(app, d, (w - cw) - 4 if two else inner)),
                  ("LTE", self.rows_lte(app, d, (w - cw) - 4 if two else inner)),
                  ("GPS", self.rows_gps(app, d, (w - cw) - 4 if two else inner))]
        if two:
            y = y0
            for t, r in panels[:2]:
                y += app.panel(y, 0, lw, t, r, (y1 - y0) // 2)
            # right column: sistem then modem
            half = (y1 - y0) // 2
            y2 = y0
            y2 += app.panel(y2, cw, w - cw, panels[2][0], panels[2][1], half)
            app.panel(y2, cw, w - cw, panels[3][0], panels[3][1], y1 - y2)
        else:
            y = y0
            for t, r in panels:
                if y1 - y < 3:
                    break
                y += app.panel(y, 0, w, t, r, (y1 - y0) // 4)

    def rows_hotspot(self, app, d, inner):
        c, S, hs, cfg = app.c, app.sym, app.hs, app.cfg
        dim, wh = c["dim"], c["wh_b"]
        st = hs.state
        if st == "on":
            badge = [(S["on"] + " AKTIF", c["good_b"]), ("  " + fmt_dur(time.time() - hs.started), dim)]
        elif st in ("starting", "stopping"):
            badge = [(S["wait"] + " " + ("MEMULAI..." if st == "starting" else "MENGHENTIKAN..."), c["warn_b"])]
        elif st == "error":
            badge = [(S["bad"] + " ERROR", c["bad_b"])]
        else:
            badge = [(S["off"] + " MATI", dim)]
        rows = [[("Status   ", dim)] + badge]
        if st == "error":
            rows.append([("Sebab    ", dim), (hs.error[:inner - 9], c["bad"])])
        psk = cfg["psk"]
        rows.append([("SSID     ", dim), (cfg["ssid"], wh), ("  (tersembunyi)" if cfg["hidden"] else "", dim)])
        rows.append([("Sandi    ", dim), (S["mask"] * len(psk) if psk else "(terbuka)", c["acc"]),
                     ("  lihat: Menu > Hotspot" if psk else "", dim)])
        band, chan = (hs.eff if hs.state == "on" and hs.eff else (cfg["band"], cfg["channel"]))
        rows.append([("Radio    ", dim), ("%s  %s GHz  CH %s  %s" % (hs.ap if hs.state == "on" and hs.ap else hs.ap_iface(), band, chan, "WPA2" if psk else "OPEN"), c["wh"]),
                     ("  (ikut wlan0)" if hs.state == "on" and hs.eff and hs.eff != (cfg["band"], int(cfg["channel"])) else "", dim)])
        try:
            gw = "%s/%d" % (hs.gw(), hs.net().prefixlen)
        except Exception:
            gw = "?"
        rows.append([("Gateway  ", dim), (gw, c["wh"]), ("   DNS " + ",".join(cfg["dns"][:1]), dim)])
        up = d.get("upif")
        uip = (d.get("ips") or {}).get(up, "") if up else ""
        rows.append([("Upstream ", dim),
                     ((up or "(belum ada)") + ("  " + uip if uip else ""), c["wh"] if up else c["warn"]),
                     ("   NAT " + ("ON" if cfg["nat"] else "OFF"), dim)])
        ms, ts = d.get("ping", (None, 0))
        if not ts:
            inet = [("memeriksa...", dim)]
        elif ms is None:
            inet = [(S["off"] + " tidak ada", c["bad"])]
        else:
            inet = [(S["on"] + " online  %.0f ms" % ms, c["good"])]
        rows.append([("Internet ", dim)] + inet)
        n = len(d.get("clients") or [])
        rows.append([("Klien    ", dim), ("%d / %d" % (n, cfg["max_clients"]), c["acc_b"])])
        # checklist komponen
        hp = bool(hs.pid("hostapd"))
        dn = bool(hs.pid("dnsmasq"))
        fwd = rd("/proc/sys/net/ipv4/ip_forward") == "1"
        rows.append([("hostapd ", dim), ((S["on"] if hp else S["off"]) + (" aktif" if hp else " mati"), c["good"] if hp else c["bad"])])
        rows.append([("dnsmasq ", dim), ((S["on"] if dn else S["off"]) + (" aktif" if dn else " mati"), c["good"] if dn else c["bad"])])
        rows.append([("ip_fwd   ", dim), ((S["on"] if fwd else S["off"]) + (" 1" if fwd else " 0"), c["good"] if fwd else c["bad"])])
        rows.append([("NAT      ", dim), (("aktif" if hs.state == "on" and cfg["nat"] else "mati"), c["good"] if (hs.state == "on" and cfg["nat"]) else dim)])
        return rows

    def rows_net(self, app, d, inner):
        c, S = app.c, app.sym
        dim = c["dim"]
        hd, hu = d.get("hd") or [], d.get("hu") or []
        sw = max(6, inner - 9 - 12)
        vmax_d = max([10240] + hd[-sw:])
        vmax_u = max([10240] + hu[-sw:])
        rows = [
            [(S["dnl"] + " klien  ", dim), ("@spark", hd, sw, vmax_d, c["acc"]),
             (" " + fmt_rate(d.get("down", 0)).rjust(11), c["wh_b"])],
            [(S["upl"] + " klien  ", dim), ("@spark", hu, sw, vmax_u, c["good"]),
             (" " + fmt_rate(d.get("up", 0)).rjust(11), c["wh_b"])],
            [("Sesi     ", dim), ("%s %s   %s %s" % (S["dnl"], fmt_bytes(d.get("tot_down", 0)), S["upl"], fmt_bytes(d.get("tot_up", 0))), c["wh"])],
            [("Upstream ", dim), ("%s %s  %s %s" % (S["dnl"], fmt_rate(d.get("u_down", 0)), S["upl"], fmt_rate(d.get("u_up", 0))), c["wh"])],
        ]
        return rows

    def rows_sys(self, app, d, inner):
        c, S = app.c, app.sym
        dim = c["dim"]

        def barrow(label, pct, tail="", inv=False):
            tl = len(tail) + 1 if tail else 0
            bw = max(6, inner - 9 - 5 - tl)
            col = app.level_color(pct, inv)
            seg = [(label.ljust(9), dim), ("@bar", pct, bw, col), (" %3.0f%%" % pct, c["wh_b"])]
            if tail:
                seg.append((" " + tail, dim))
            return seg

        rows = [barrow("CPU", d.get("cpu", 0), "%.1fGHz" % (d["freq"] / 1000.0) if d.get("freq") else "")]
        cores = d.get("cores") or []
        tmp = d.get("temp")
        rows.append([("Core     ", dim), ("@spark", cores, len(cores), 100, c["acc"]),
                     ("  Suhu ", dim),
                     ("%.1f%sC" % (tmp, S["deg"]) if tmp is not None else "n/a",
                      app.level_color((tmp or 0) / 80.0 * 100, False) | curses.A_BOLD)])
        mt, ma = d.get("mem", (1, 1))
        rows.append(barrow("RAM", 100.0 * (mt - ma) / max(1, mt), "%s/%s" % (fmt_bytes(mt - ma), fmt_bytes(mt))))
        bat = d.get("bat") or {}
        if bat.get("pct") is not None:
            tail = {"charging": "Mengisi", "discharging": "Dipakai", "full": "Penuh",
                    "not charging": "Tdk mengisi"}.get((bat.get("status") or "").lower(), bat.get("status") or "")
            if bat.get("w"):
                tail += " %.1fW" % bat["w"]
            rows.append(barrow("Baterai", bat["pct"], tail, inv=True))
        dt, du = d.get("disk", (1, 0))
        rows.append(barrow("Disk", 100.0 * du / max(1, dt), "%s/%s" % (fmt_bytes(du), fmt_bytes(dt))))
        ld = d.get("load", (0, 0, 0))
        rows.append([("Load     ", dim), ("%.2f %.2f %.2f" % ld, c["wh"]),
                     ("   Aktif " + fmt_dur(d.get("uptime", 0)), dim)])
        return rows

    def rows_lte(self, app, d, inner):
        c = app.c
        dim = c["dim"]
        rows = []
        now = time.time()
        cache = getattr(app, "_lte_cache", None)
        if cache is None or now - cache[0] > 5:
            rc, out = sh("mmcli -m 0", timeout=3)
            rc2, out2 = sh("mmcli -m 0", timeout=4)
            rc3, out3 = sh("mmcli -m 0 --command='AT+GSN' 2>&1", timeout=5)
            app._lte_cache = (now, rc, out, rc2, out2, rc3, out3)
        _, rc, out, rc2, out2, rc3, out3 = app._lte_cache
        if rc == 0:
            for key in ("state:", "access tech:", "signal quality:", "power state:", "manufacturer:", "model:"):
                for l in out.splitlines():
                    if key in l:
                        rows.append([(key.replace(":", "").ljust(9), dim), (l.split(":", 1)[-1].strip(), c["wh"])])
            # operator name dari mmcli 3GPP (cached)
            for l in (out2 or "").splitlines():
                if "operator name" in l.lower():
                    rows.append([("Operator ", dim), (l.split(":", 1)[-1].strip(), c["acc"])])
                elif "operator id" in l.lower():
                    rows.append([("Op ID    ", dim), (l.split(":", 1)[-1].strip(), c["dim"])])
                elif "registration:" in l.lower():
                    rows.append([("Reg      ", dim), (l.split(":", 1)[-1].strip(), c["acc"])])
                elif "packet service state" in l.lower():
                    rows.append([("Pkt state", dim), (l.split(":", 1)[-1].strip(), c["dim"])])
            # imei (cached)
            if rc3 == 0 and out3:
                import re
                m = re.findall(r'\d{15}', out3)
                if m:
                    rows.append([("IMEI     ", dim), (m[0], c["wh"])])
            else:
                # from equipment id
                for l in out.splitlines():
                    if "equipment id" in l.lower():
                        rows.append([("IMEI     ", dim), (l.split(":", 1)[-1].strip(), c["wh"])])
        else:
            rows.append([("Modem    ", dim), ("tidak ditemukan", c["bad"])])
        return rows or [[("Modem    ", dim), ("n/a", c["dim"])]]

    def rows_gps(self, app, d, inner):
        c = app.c
        dim = c["dim"]
        rows = []
        now = time.time()
        cache = getattr(app, "_gps_cache", None)
        if cache is None or now - cache[0] > 5:
            sh("mmcli -m 0 --location-enable-gps-nmea 2>/dev/null", timeout=3)
            sh("mmcli -m 0 --location-enable-gps-raw 2>/dev/null", timeout=3)
            rc2, out2 = sh("mmcli -m 0 --location-get", timeout=4)
            app._gps_cache = (now, rc2, out2)
        _, rc2, out2 = app._gps_cache
        lat = lon = alt = sats = hdop = fix = speed = None
        if rc2 == 0:
            # parse NMEA GGA
            for l in (out2 or "").splitlines():
                l = l.strip()
                if "GGA" in l:
                    parts = l.split(",")
                    try:
                        # lat
                        if parts[2] and parts[3]:
                            la = float(parts[2]); lad = int(la/100); lam = la - lad*100
                            lat = lad + lam/60.0
                            if parts[3] == "S": lat = -lat
                        if parts[4] and parts[5]:
                            lo = float(parts[4]); lod = int(lo/100); lom = lo - lod*100
                            lon = lod + lom/60.0
                            if parts[5] == "W": lon = -lon
                        fix = parts[6]
                        sats = parts[7]
                        hdop = parts[8]
                        alt = parts[9]
                    except Exception:
                        pass
                if "RMC" in l:
                    parts = l.split(",")
                    try:
                        if parts[7]:
                            speed = float(parts[7]) * 1.852  # knots -> km/h
                    except Exception:
                        pass
        if lat is not None and lon is not None:
            rows.append([("Lat      ", dim), ("%.6f" % lat, c["wh"])])
            rows.append([("Lon      ", dim), ("%.6f" % lon, c["wh"])])
            rows.append([("Alt      ", dim), ("%s m" % alt if alt else "n/a", c["wh"])])
            rows.append([("Satelit  ", dim), (sats or "n/a", c["wh"])])
            rows.append([("HDOP     ", dim), (hdop or "n/a", c["wh"])])
            rows.append([("Fix      ", dim), ({"0":"no fix","1":"GPS","2":"DGPS","3":"PPS","4":"RTK","5":"FRTK","6":"est"}.get(fix, fix or "n/a"), c["good"] if fix not in (None,"0","") else c["bad"])])
            if speed is not None:
                rows.append([("Speed    ", dim), ("%.1f km/h" % speed, c["wh"])])
            # akurasi perkiraan dari hdop
            try:
                acc = float(hdop) * 5 if hdop else None
                if acc:
                    rows.append([("Akurasi  ", dim), ("~%.0f m" % acc, c["wh"])])
            except Exception:
                pass
            rows.append([("Mode     ", dim), ("3D" if (fix in ("1","2","3","4","5","6") and float(alt or 0) != 0) else "2D", c["wh"])])
        else:
            # fallback: tampilkan raw
            if rc2 == 0:
                for l in (out2 or "").splitlines():
                    if l.strip() and not l.strip().startswith("-"):
                        rows.append([("GPS      ", dim), (l.strip()[:inner - 10], c["wh"])])
            else:
                rows.append([("GPS      ", dim), ("tidak tersedia", c["dim"])])
        return rows or [[("GPS      ", dim), ("n/a", c["dim"])]]

    # -- tab: klien ----------------------------------------------------------------
    def cl(self, app, d, y0, y1, w):
        c, S, cfg = app.c, app.sym, app.cfg
        cl = d.get("clients") or []
        app.put(y0, 1, "Klien terhubung: ", c["dim"])
        app.put(y0, 18, "%d / %d" % (len(cl), cfg["max_clients"]), c["acc_b"])
        y = y0 + 2
        if not cl:
            msg = ["Belum ada klien." if app.hs.state == "on" else "Hotspot sedang mati.",
                   "Sambungkan perangkat ke WiFi:  %s" % cfg["ssid"] if app.hs.state == "on"
                   else "Nyalakan: tahan POWER, atau Menu > Hotspot."]
            for i, m in enumerate(msg):
                app.put(y + i, 2, m, c["dim"])
            return
        for k, x in enumerate(cl):
            if y + 2 > y1:
                app.put(y1 - 1, 1, "... +%d klien lagi" % (len(cl) - k), c["dim"])
                break
            name = x["name"] or "(tanpa nama)"
            sig = x["sig"]
            sc = c["dim"] if sig is None else (c["good"] if sig > -60 else c["warn"] if sig > -75 else c["bad"])
            app.put(y, 1, S["on"], sc)
            app.put(y, 3, name[:max(4, w - 22)], c["wh_b"])
            app.put(y, max(3 + len(name) + 2, w - 17), x["ip"], c["acc"])
            sub = "%s  %s  %s%s %s%s  %s" % (x["mac"], ("%d dBm" % sig) if sig is not None else "- dBm",
                                             S["dnl"], fmt_bytes(x["tx"]), S["upl"], fmt_bytes(x["rx"]),
                                             fmt_dur(x["t"]))
            app.put(y + 1, 3, sub[:w - 4], c["dim"])
            y += 3

    # -- tab: trafik ---------------------------------------------------------------
    def tr(self, app, d, y0, y1, w):
        c, S = app.c, app.sym
        hd, hu = d.get("hd") or [], d.get("hu") or []
        H = max(2, min(8, (y1 - y0 - 7) // 2))
        cw = w - 4
        y = y0
        for name, hist, col, cur in (("Ke klien (download)", hd, c["acc"], d.get("down", 0)),
                                     ("Dari klien (upload)", hu, c["good"], d.get("up", 0))):
            peak = max([1] + hist)
            app.put(y, 1, "%s %s" % (S["dnl"] if "Ke" in name else S["upl"], name), c["wh_b"])
            app.put(y, w - 30 if w > 60 else 28, ("sekarang %s" % fmt_rate(cur))[:29], c["acc_b"])
            app.put(y + 1, 1, ("puncak %s (%d dtk terakhir)" % (fmt_rate(peak), len(hist) * app.cfg["refresh"]))[:w - 2], c["dim"])
            app.chart(y + 2, 2, cw, H, hist, max(peak, 10240), col)
            y += H + 3
        app.put(y, 1, "Total sesi:  %s %s   %s %s" % (S["dnl"], fmt_bytes(d.get("tot_down", 0)),
                                                       S["upl"], fmt_bytes(d.get("tot_up", 0))), c["wh"])

    # -- tab: log ------------------------------------------------------------------
    def lg(self, app, d, y0, y1, w):
        lines = list(app.logbuf)[-(y1 - y0):]
        for i, ln in enumerate(lines):
            col = app.c["bad"] if "ERR" in ln else app.c["warn"] if "PERINGATAN" in ln else app.c["wh"]
            app.put(y0 + i, 1, ln[:w - 2], col)
        if not lines:
            app.put(y0, 1, "(log kosong)", app.c["dim"])


# ----------------------------------------------------------------------------
# Aksi-aksi
# ----------------------------------------------------------------------------
def confirm(app, text, on_yes):
    app.push(MenuScreen(text, lambda a: [
        Item("Batal", lambda a: a.pop()),
        Item("YA, lanjutkan", lambda a: (a.pop(), on_yes()), danger=True),
    ]))


def chooser(app, title, options, current, on_pick):
    def build(a):
        return [Item(lbl, (lambda a, v=val: (a.pop(), on_pick(v))), value=(app.sym["on"] if val == current else ""))
                for lbl, val in options]
    sc = MenuScreen(title, build)
    for i, (_, val) in enumerate(options):
        if val == current:
            sc.sel = i
    return sc


def set_cfg(app, key, val, note=True):
    app.cfg[key] = val
    app.cfg.save()
    if note and app.hs.state == "on":
        app.toast("Restart hotspot untuk menerapkan", "warn", 4)


def start_hotspot(app):
    app.job("Menyalakan hotspot", app.hs.start,
            then=lambda r: app.toast("Hotspot aktif: %s" % app.cfg["ssid"], "good"))


def stop_hotspot(app):
    app.job("Mematikan hotspot", app.hs.stop,
            then=lambda r: app.toast("Hotspot dimatikan", "info"))


def restart_hotspot(app):
    def fn():
        app.hs.stop()
        app.hs.start()
    app.job("Restart hotspot", fn, then=lambda r: app.toast("Hotspot di-restart", "good"))


def toggle_hotspot(app):
    st = app.hs.state
    if st == "on":
        confirm(app, "Matikan hotspot? Klien akan terputus.", lambda: stop_hotspot(app))
    elif st in ("off", "error"):
        confirm(app, "Nyalakan hotspot '%s'?" % app.cfg["ssid"], lambda: start_hotspot(app))
    else:
        app.toast("Sedang proses...", "warn")


def val_ssid(t):
    return None if 1 <= len(t) <= 32 else "SSID harus 1-32 karakter"


def val_psk(t):
    return None if t == "" or 8 <= len(t) <= 63 else "Sandi WPA2: 8-63 karakter (kosong = terbuka)"


def val_cc(t):
    return None if re.match(r"^[A-Za-z]{2}$", t) else "Kode negara: 2 huruf, mis. ID"


def edit(app, title, key, validate, maxlen=63, note="", transform=None):
    def done(v):
        set_cfg(app, key, transform(v) if transform else v)
    app.push(EditScreen(title, str(app.cfg[key]), done, validate, maxlen, note))


def toggle(app, key, note=True):
    set_cfg(app, key, not app.cfg[key], note)


def client_action(app, mac, what):
    ap = app.hs.ap or "wlan0"

    def fn():
        if app.demo:
            time.sleep(0.5)
            return
        if what == "block":
            if mac not in app.cfg["blocked"]:
                app.cfg["blocked"].append(mac)
                app.cfg.save()
            app.hs.cli("deny_acl ADD_MAC %s" % mac)
        elif what == "unblock":
            app.cfg["blocked"] = [m for m in app.cfg["blocked"] if m != mac]
            app.cfg.save()
            app.hs.cli("deny_acl DEL_MAC %s" % mac)
            return
        rc, out = app.hs.cli("deauthenticate %s" % mac)
        if rc != 0:
            raise RuntimeError(out[-80:] or "hostapd_cli gagal")
    labels = {"kick": "Memutuskan klien", "block": "Memblokir klien", "unblock": "Membuka blokir"}
    if app.demo and what in ("block", "unblock"):
        lst = app.cfg["blocked"]
        if what == "block" and mac not in lst:
            lst.append(mac)
        if what == "unblock" and mac in lst:
            lst.remove(mac)
    app.job(labels[what], fn, then=lambda r: app.toast("Selesai: %s" % labels[what].lower(), "good"))
    app.pop_to_root_menu()


def show_password(app):
    def lines(a):
        c = a.cfg
        return ["", "  SSID    : %s" % c["ssid"],
                "  Sandi   : %s" % (c["psk"] or "(terbuka, tanpa sandi)"),
                "  Keamanan: %s" % ("WPA2-PSK (CCMP)" if c["psk"] else "OPEN"),
                "", "  Ubah di Menu > Pengaturan Hotspot."]
    app.push(TextView("Kredensial WiFi", lines))


def show_qr(app):
    c = app.cfg

    def esc(s):
        return re.sub(r'([\\;,:"])', r"\\\1", s)
    payload = "WIFI:T:%s;S:%s;%sH:%s;;" % ("WPA" if c["psk"] else "nopass", esc(c["ssid"]),
                                           ("P:%s;" % esc(c["psk"])) if c["psk"] else "",
                                           "true" if c["hidden"] else "false")

    def lines(a):
        if not which("qrencode"):
            return ["", "  qrencode belum terpasang.", "  Pasang:  apk add libqrencode-tools"]
        rc, out = sh(["qrencode", "-t", "ASCII" if c["ascii"] else "UTF8", "-m", "1", payload])
        return ["  Scan dengan kamera HP untuk bergabung:", ""] + out.splitlines() if rc == 0 else ["  qrencode gagal: " + out[-60:]]
    app.push(TextView("Kode QR WiFi", lines))


def show_conn(app):
    def lines(a):
        d = a.stats.d or {}
        hs, c = a.hs, a.cfg
        up = d.get("upif")
        try:
            net = hs.net()
            rng = "%s - %s" % (net.network_address + 10, net.network_address + min(net.num_addresses - 2, 110))
            gw = hs.gw()
        except Exception:
            rng, gw = "?", "?"
        return ["", "  Interface AP : %s%s" % (d.get("ap") or "-",
                                             ("  (virtual di %s)" % hs.phy_iface()) if c["virtual_ap"] else ""),
                "  Gateway      : %s" % gw, "  Rentang DHCP : %s" % rng,
                "  DNS klien    : %s" % ", ".join(c["dns"]),
                "  Upstream     : %s  %s" % (up or "-", (d.get("ips") or {}).get(up, "")),
                "  NAT          : %s" % ("aktif" if c["nat"] else "mati (hanya LAN lokal)"),
                "  Diblokir     : %d perangkat" % len(c["blocked"])]
    app.push(TextView("Info Koneksi", lines))


def show_ifaces(app):
    def lines(a):
        d = a.stats.d or {}
        cn = net_counters()
        out = ["  %-12s %-8s %-18s %s" % ("IFACE", "STATUS", "IP", "RX / TX")]
        names = list_ifaces() if not a.demo else ["wlan0", "usb0", "wwan0"]
        for n in names:
            st = rd("/sys/class/net/%s/operstate" % n, "up")
            rx, tx = cn.get(n, (0, 0))
            out.append("  %-12s %-8s %-18s %s / %s" % (n, st, (d.get("ips") or {}).get(n, "-"), fmt_bytes(rx), fmt_bytes(tx)))
        return out
    app.push(TextView("Interface Jaringan", lines))


def ping_test(app):
    def fn():
        if app.demo:
            time.sleep(1.5)
            return "PING 1.1.1.1: 4 paket, 0% loss\nrtt min/avg/max = 19.2/24.8/33.1 ms (demo)"
        rc, out = sh("ping -c 4 -W 2 1.1.1.1", timeout=15)
        return out
    app.job("Tes ping", fn, then=lambda r: app.push(TextView("Hasil Ping", ["", ] + ["  " + l for l in r.splitlines()])))

def refresh_wlan0(app):
    def fn():
        if app.demo:
            time.sleep(0.8)
            return
        phy = app.hs.phy_iface() or "wlan0"
        sh("rfkill unblock wifi")
        sh("ip link set %s down" % phy)
        time.sleep(0.5)
        sh("ip link set %s up" % phy)
        if which("nmcli"):
            sh("nmcli dev wifi rescan ifname %s" % phy, timeout=8)
        elif which("iw"):
            sh("iw dev %s scan" % phy, timeout=8)
    app.job("Refresh wlan0", fn, then=lambda r: app.toast("wlan0 di-refresh", "good"))

def connect_wlan0(app):
    def ask_ssid():
        def on_ssid(ssid):
            if not ssid:
                app.toast("SSID kosong", "warn")
                return
            def on_psk(psk):
                def fn():
                    if app.demo:
                        time.sleep(1.0)
                        return
                    phy = app.hs.phy_iface() or "wlan0"
                    if which("nmcli"):
                        cmd = ["nmcli", "dev", "wifi", "connect", ssid]
                        if psk:
                            cmd += ["password", psk]
                        cmd += ["ifname", phy]
                        rc, out = sh(cmd, timeout=25)
                        if rc != 0:
                            raise RuntimeError(out[-90:] or "nmcli gagal")
                    elif which("wpa_cli"):
                        rc, out = sh("wpa_cli -i %s add_network" % phy, timeout=5)
                        # konfigurasi minimal via wpa_cli tidak lengkap; sarankan nmcli
                        raise RuntimeError("Gunakan nmcli untuk menyambung (wpa_cli manual tidak didukung)")
                    else:
                        raise RuntimeError("nmcli/wpa_cli belum terpasang")
                app.job("Sambungkan wlan0 ke %s" % ssid, fn,
                        then=lambda r: app.toast("wlan0 tersambung: %s" % ssid, "good"))
            app.push(EditScreen("Kata sandi WiFi (kosong=terbuka)", "", on_psk,
                                lambda t: None, 63, "WPA2/WPA3, kosongkan jika terbuka"))
        app.push(EditScreen("SSID WiFi tujuan", "", on_ssid, lambda t: None if 1 <= len(t) <= 32 else "SSID 1-32 karakter", 32))
    ask_ssid()
    def fn():
        if app.demo:
            time.sleep(1.5)
            return "PING 1.1.1.1: 4 paket, 0% loss\nrtt min/avg/max = 19.2/24.8/33.1 ms (demo)"
        rc, out = sh("ping -c 4 -W 2 1.1.1.1", timeout=15)
        return out
    app.job("Tes ping", fn, then=lambda r: app.push(TextView("Hasil Ping", ["", ] + ["  " + l for l in r.splitlines()])))


class LogScreen(TextView):
    SRC = ["Aplikasi", "hostapd", "dnsmasq", "dmesg"]

    def __init__(self):
        super().__init__("Log", self.fetch, follow=True, ok_label="Sumber lain", on_ok=self.next_src)
        self.src = 0

    def next_src(self, app):
        self.src = (self.src + 1) % len(self.SRC)
        self.stick = True
        self.title = "Log: %s" % self.SRC[self.src]

    def fetch(self, app):
        if self.src == 0:
            return list(app.logbuf) or ["(kosong)"]
        if self.src == 3:
            rc, out = sh("dmesg | tail -n 120", timeout=3)
            return out.splitlines() or ["(kosong)"]
        name = "hostapd" if self.src == 1 else "dnsmasq"
        t = rd("%s/%s.log" % (RUN, name))
        return t.splitlines()[-200:] if t else ["(belum ada log %s)" % name]


def device_info(app):
    def lines(a):
        u = os.uname()
        model = rd("/proc/device-tree/model").strip("\x00") or "(tidak diketahui)"
        d = a.stats.d or {}
        mt = (d.get("mem") or (0, 0))[0]
        btn = a.buttons.devices if a.buttons else []
        return ["", "  Model   : %s" % model, "  Kernel  : %s %s" % (u.sysname, u.release),
                "  Arsitek.: %s" % u.machine, "  RAM     : %s" % fmt_bytes(mt),
                "  Python  : %s" % sys.version.split()[0],
                "  Tombol  : %s" % ("; ".join(btn) if btn else "tidak terdeteksi (pakai keyboard)"),
                "  Config  : %s" % a.cfg.path, "  Runtime : %s" % RUN]
    app.push(TextView("Info Perangkat", lines))


HELP = [
    "",
    "  PETA TOMBOL",
    "    VOL+    naik / sebelumnya",
    "    VOL-    turun / berikutnya",
    "    POWER   tekan singkat = pilih / OK",
    "            tahan 0,7 dtk  = kembali",
    "",
    "  DASBOR",
    "    VOL+/-       ganti tab",
    "    POWER        buka menu",
    "    POWER tahan  nyalakan / matikan hotspot",
    "",
    "  MENGETIK (SSID / sandi)",
    "    VOL+/-       putar karakter (tahan = cepat)",
    "    POWER        ketik karakter terpilih",
    "    POWER tahan  hapus 1 karakter",
    "    Putar ke [SIMPAN] lalu tekan POWER",
    "",
    "  Layar mati otomatis menghemat baterai;",
    "  tombol apa pun menyalakan layar kembali.",
    "  Keyboard (SSH): panah/jk, Enter, Esc/q.",
]


def snort_menu(app):
    def find_cfg():
        for p in ("/etc/snort/snort.lua", "/etc/snort/snort.conf", "/etc/snort/snort.rules"):
            if os.path.exists(p):
                return p
        return None

    def find_log():
        for p in ("/var/log/snort/alert_fast.txt", "/var/log/snort/alert", "/var/log/snort/alert.log"):
            if os.path.exists(p):
                return p
        return "/var/log/snort/alert"

    def build(a):
        bin_path = which("snort")
        running = False
        if bin_path:
            rc, _ = sh("pgrep -f snort", timeout=2)
            running = rc == 0
        cfg = find_cfg()
        items = [
            Item("Binary", lambda a: None, value=lambda a: bin_path or "tidak ditemukan"),
            Item("Status", lambda a: None, value=lambda a: "berjalan" if (bin_path and sh("pgrep -f snort", timeout=2)[0] == 0) else "mati"),
            Item("Config", lambda a: None, value=lambda a: cfg or "tidak ditemukan"),
        ]
        if bin_path and cfg:
            if running:
                items.append(Item("Hentikan snort", lambda a: sh("pkill -f snort"), value=""))
            else:
                iface = app.hs.ap or app.hs.ap_iface() or "eth0"
                cmd = "snort -c %s -i %s -A alert_fast -l /var/log/snort &" % (cfg, iface)
                items.append(Item("Jalankan snort", lambda a: sh(cmd, timeout=3), value=""))
            items.append(Item("Lihat log", lambda a: a.push(TextView("Snort log", (rd(find_log()) or "(kosong)").splitlines()[-50:])), value=""))
        return items
    return MenuScreen("Snort", build)


def suricata_menu(app):
    def find_cfg():
        for p in ("/etc/suricata/suricata.yaml", "/etc/suricata/suricata.yml"):
            if os.path.exists(p):
                return p
        return None

    def find_log():
        for p in ("/var/log/suricata/fast.log", "/var/log/suricata/eve.json", "/var/log/suricata/suricata.log"):
            if os.path.exists(p):
                return p
        return "/var/log/suricata/fast.log"

    def build(a):
        bin_path = which("suricata")
        running = False
        if bin_path:
            rc, _ = sh("pgrep -f suricata", timeout=2)
            running = rc == 0
        cfg = find_cfg()
        items = [
            Item("Binary", lambda a: None, value=lambda a: bin_path or "tidak ditemukan"),
            Item("Status", lambda a: None, value=lambda a: "berjalan" if (bin_path and sh("pgrep -f suricata", timeout=2)[0] == 0) else "mati"),
            Item("Config", lambda a: None, value=lambda a: cfg or "tidak ditemukan"),
        ]
        if bin_path and cfg:
            if running:
                items.append(Item("Hentikan suricata", lambda a: sh("pkill -f suricata"), value=""))
            else:
                iface = app.hs.ap or app.hs.ap_iface() or "eth0"
                cmd = "suricata -c %s -i %s -l /var/log/suricata -D" % (cfg, iface)
                items.append(Item("Jalankan suricata", lambda a: sh(cmd, timeout=5), value=""))
            items.append(Item("Lihat log", lambda a: a.push(TextView("Suricata log", (rd(find_log()) or "(kosong)").splitlines()[-50:])), value=""))
        return items
    return MenuScreen("Suricata", build)

def lte_menu(app):
    def lte_info():
        rc, out = sh("mmcli -m 0", timeout=4)
        if rc != 0:
            return ["Modem tidak ditemukan"]
        lines = []
        for key in ("state:", "access tech:", "signal quality:", "power state:", "model:", "manufacturer:"):
            for l in out.splitlines():
                if key in l:
                    lines.append(l.strip())
        return lines or ["(tidak ada info)"]

    def build(a):
        items = [Item("LTE status", lambda a: a.push(TextView("LTE", lte_info())), value=lambda a: "info")]
        return items
    return MenuScreen("LTE", build)


def gps_menu(app):
    def gps_info():
        rc, out = sh("gpspipe -w -n 1 2>/dev/null", timeout=3)
        if rc != 0 or not out.strip():
            rc2, out2 = sh("mmcli -m 0 --location-get", timeout=4)
            if rc2 == 0:
                return out2.splitlines()[:10] or ["(tidak ada data GPS)"]
            return ["GPS tidak tersedia"]
        return out.splitlines()[:10]

    def build(a):
        items = [Item("GPS", lambda a: a.push(TextView("GPS", gps_info())), value=lambda a: "info")]
        return items
    return MenuScreen("GPS", build)

def dashboard_menu(app):
    def is_running():
        rc, _ = sh("pgrep -f dashboard_streamlit.py", timeout=2)
        if rc == 0:
            return True
        rc, _ = sh("pgrep -f serve_dashboard.py", timeout=2)
        if rc == 0:
            return True
        rc, _ = sh("pgrep -f app.py", timeout=2)
        if rc == 0:
            return True
        return False

    def build(a):
        running = is_running()
        items = [
            Item("Status", lambda a: None, value=lambda a: "berjalan" if running else "mati"),
            Item("Port", lambda a: None, value=lambda a: "7890"),
        ]
        if running:
            items.append(Item("Hentikan dashboard", lambda a: (sh("pkill -f dashboard_streamlit.py"), sh("pkill -f serve_dashboard.py"), sh("pkill -f app.py")), value=""))
        else:
            items.append(Item("Jalankan Streamlit", lambda a: sh("nohup streamlit run /root/pocotui/dashboard_streamlit.py --server.port 7890 --server.address 0.0.0.0 >/dev/null 2>&1 &", timeout=3), value=""))
        items.append(Item("URL", lambda a: None, value=lambda a: "http://<ip>:7890/"))
        return items
    return MenuScreen("Web Dashboard", build)

def main_menu(app):
    def build(a):
        n = len((a.stats.d or {}).get("clients") or [])
        st = a.hs.state
        return [
            Item("Hotspot", lambda a: a.push(hotspot_menu(a)), value=lambda a: {"on": "AKTIF", "off": "mati", "error": "ERROR"}.get(a.hs.state, "...")),
            # (nilai di atas tampil di kanan baris)
            Item("Klien", lambda a: a.push(clients_menu(a)), value=lambda a: "%d terhubung" % n),
            Item("Jaringan", lambda a: a.push(network_menu(a))),
            Item("Pengaturan Hotspot", lambda a: a.push(wifi_menu(a))),
            Item("Tampilan & Daya", lambda a: a.push(display_menu(a))),
            Item("Log", lambda a: a.push(LogScreen())),
            Item("Snort", lambda a: a.push(snort_menu(a))),
            Item("Suricata", lambda a: a.push(suricata_menu(a))),
            Item("LTE", lambda a: a.push(lte_menu(a))),
            Item("GPS", lambda a: a.push(gps_menu(a))),
            Item("Sistem", lambda a: a.push(system_menu(a))),
            Item("Bantuan / peta tombol", lambda a: a.push(TextView("Bantuan", HELP))),
            Item("Tutup menu", lambda a: a.pop()),
        ]
    return MenuScreen("Menu Utama", build)


def hotspot_menu(app):
    def build(a):
        on = a.hs.state == "on"
        busy = a.hs.state in ("starting", "stopping")
        return [
            Item("Matikan hotspot" if on else "Nyalakan hotspot", toggle_hotspot, enabled=not busy,
                 danger=on, value=lambda a: {"on": "AKTIF", "off": "mati", "error": "ERROR"}.get(a.hs.state, "...")),
            Item("Restart hotspot", lambda a: restart_hotspot(a), enabled=on),
            Item("Tampilkan kredensial", show_password),
            Item("Kode QR WiFi", show_qr),
            Item("Info koneksi", show_conn),
        ]
    return MenuScreen("Hotspot", build)


def clients_menu(app):
    def build(a):
        cl = (a.stats.d or {}).get("clients") or []
        items = []
        for x in cl:
            items.append(Item(x["name"] or x["ip"], (lambda a, x=x: a.push(client_menu(a, x))),
                              value="%s%s" % (x["ip"], ("  %ddBm" % x["sig"]) if x["sig"] is not None else "")))
        if not cl:
            items.append(Item("(belum ada klien)", None, enabled=False))
        items.append(Item("Daftar blokir", lambda a: a.push(blocked_menu(a)), value="%d" % len(a.cfg["blocked"])))
        return items
    return MenuScreen("Klien", build)


def client_menu(app, x):
    mac = x["mac"]
    return MenuScreen(x["name"] or x["ip"], lambda a: [
        Item("Detail", lambda a: a.push(TextView("Detail Klien", [
            "", "  Nama   : %s" % (x["name"] or "-"), "  IP     : %s" % x["ip"], "  MAC    : %s" % mac,
            "  Sinyal : %s" % (("%d dBm" % x["sig"]) if x["sig"] is not None else "-"),
            "  Unduh  : %s" % fmt_bytes(x["tx"]), "  Unggah : %s" % fmt_bytes(x["rx"]),
            "  Durasi : %s" % fmt_dur(x["t"])]))),
        Item("Putuskan (kick)", lambda a: client_action(a, mac, "kick")),
        Item("Blokir perangkat", lambda a: confirm(a, "Blokir %s?" % mac, lambda: client_action(a, mac, "block")), danger=True),
    ])


def blocked_menu(app):
    def build(a):
        items = [Item(m, (lambda a, m=m: confirm(a, "Buka blokir %s?" % m, lambda: client_action(a, m, "unblock"))))
                 for m in a.cfg["blocked"]]
        return items or [Item("(daftar kosong)", None, enabled=False)]
    return MenuScreen("Daftar Blokir", build)


DNS_PRESETS = [("Cloudflare 1.1.1.1", ["1.1.1.1", "1.0.0.1"]), ("Google 8.8.8.8", ["8.8.8.8", "8.8.4.4"]),
               ("Quad9 9.9.9.9", ["9.9.9.9", "149.112.112.112"]),
               ("AdGuard (blokir iklan)", ["94.140.14.14", "94.140.15.15"])]
SUBNETS = ["192.168.50.0/24", "10.42.0.0/24", "172.20.0.0/24", "192.168.100.0/24"]


def switch_upstream(app):
    cur = app.cfg["upstream"]
    if cur == "wlan0":
        new = "rmnet_ipa0"
    elif cur == "rmnet_ipa0":
        new = "wlan0"
    else:
        new = "wlan0"
    if not os.path.isdir("/sys/class/net/" + new):
        app.toast("Interface %s tidak ada" % new, "bad", 4)
        return
    # periksa IP & rute default pada interface target
    rc, out = sh("ip -4 -o addr show dev %s" % new, timeout=2)
    has_ip = bool(re.search(r"inet \d", out or ""))
    rc2, out2 = sh("ip route show default dev %s" % new, timeout=2)
    has_route = bool(out2.strip())
    app.cfg["upstream"] = new
    app.cfg.save()
    app.log("upstream switch: %s -> %s (ip=%s route=%s)" % (cur, new, has_ip, has_route))
    if app.hs.state == "on":
        restart_hotspot(app)
    else:
        app.toast("Upstream: %s (aktifkan hotspot untuk menerapkan)" % new, "info", 4)
    if not has_ip:
        app.toast("PERINGATAN: %s belum punya IP" % new, "warn", 5)
    if not has_route:
        app.toast("PERINGATAN: %s belum punya rute default" % new, "warn", 5)

def network_menu(app):
    def pick_dns(a):
        cur = tuple(a.cfg["dns"])
        opts = [(n, tuple(v)) for n, v in DNS_PRESETS]
        a.push(chooser(a, "DNS untuk klien", opts, cur, lambda v: set_cfg(a, "dns", list(v))))

    def pick_up(a):
        opts = [("auto (ikuti rute default)", "auto")] + [(n, n) for n in list_ifaces()]
        a.push(chooser(a, "Interface upstream", opts, a.cfg["upstream"], lambda v: set_cfg(a, "upstream", v)))

    def pick_sub(a):
        a.push(chooser(a, "Subnet hotspot", [(s, s) for s in SUBNETS], a.cfg["subnet"], lambda v: set_cfg(a, "subnet", v)))

    return MenuScreen("Jaringan", lambda a: [
        Item("Interface upstream", pick_up, value=lambda a: a.cfg["upstream"]),
        Item("Bagikan internet (NAT)", lambda a: toggle(a, "nat"), value=lambda a: "ON" if a.cfg["nat"] else "OFF"),
        Item("Pindah upstream wlan0 ↔ rmnet_ipa0", lambda a: switch_upstream(a), value=lambda a: a.cfg["upstream"]),
        Item("DNS klien", pick_dns, value=lambda a: a.cfg["dns"][0]),
        Item("Subnet hotspot", pick_sub, value=lambda a: a.cfg["subnet"]),
        Item("Tes ping internet", ping_test),
        Item("Refresh wlan0 (scan ulang)", refresh_wlan0),
        Item("Sambungkan wlan0 ke WiFi lain", connect_wlan0),
        Item("Daftar interface", show_ifaces),
    ])


def wifi_menu(app):
    def pick_band(a):
        def pick(v):
            set_cfg(a, "band", v)
            set_cfg(a, "channel", 36 if v == "5" else 6, note=False)
        a.push(chooser(a, "Band", [("2.4 GHz (jangkauan luas)", "2.4"), ("5 GHz (lebih cepat)", "5")], a.cfg["band"], pick))

    def pick_ch(a):
        chans = [36, 40, 44, 48, 149, 153, 157, 161] if a.cfg["band"] == "5" else list(range(1, 14))
        a.push(chooser(a, "Channel", [("Channel %d" % c, c) for c in chans], a.cfg["channel"], lambda v: set_cfg(a, "channel", v)))

    def pick_max(a):
        a.push(chooser(a, "Maks. klien", [("%d klien" % n, n) for n in (1, 2, 4, 8, 16, 32)], a.cfg["max_clients"], lambda v: set_cfg(a, "max_clients", v)))

    def pick_wlan(a):
        opts = [("auto", "auto")] + [(n, n) for n in list_ifaces() if is_wifi(n) or a.demo]
        a.push(chooser(a, "Interface WiFi", opts, a.cfg["wlan"], lambda v: set_cfg(a, "wlan", v)))

    return MenuScreen("Pengaturan Hotspot", lambda a: [
        Item("SSID (nama WiFi)", lambda a: edit(a, "Ubah SSID", "ssid", val_ssid, 32), value=lambda a: a.cfg["ssid"]),
        Item("Kata sandi", lambda a: edit(a, "Ubah Kata Sandi", "psk", val_psk, 63, "WPA2: 8-63 karakter. Kosong = WiFi terbuka."),
             value=lambda a: "*" * min(10, len(a.cfg["psk"])) if a.cfg["psk"] else "(terbuka)"),
        Item("Band", pick_band, value=lambda a: a.cfg["band"] + " GHz"),
        Item("Channel", pick_ch, value=lambda a: str(a.cfg["channel"])),
        Item("Sembunyikan SSID", lambda a: toggle(a, "hidden"), value=lambda a: "ON" if a.cfg["hidden"] else "OFF"),
        Item("Maks. klien", pick_max, value=lambda a: str(a.cfg["max_clients"])),
        Item("Kode negara", lambda a: edit(a, "Kode Negara", "country", val_cc, 2, "2 huruf, mis. ID", lambda v: v.upper()), value=lambda a: a.cfg["country"]),
        Item("Mode AP", lambda a: toggle(a, "virtual_ap"),
             value=lambda a: "virtual %s" % a.cfg["ap_name"] if a.cfg["virtual_ap"] else "wlan0 langsung"),
        Item("Interface WiFi induk", pick_wlan, value=lambda a: a.cfg["wlan"]),
        Item("Terapkan (restart hotspot)", lambda a: restart_hotspot(a), enabled=a.hs.state == "on"),
    ])


def display_menu(app):
    def pick_br(a):
        def pick(v):
            set_cfg(a, "brightness", v, note=False)
            a.display.set_pct(v)
        a.push(chooser(a, "Kecerahan", [("%d%%" % p, p) for p in (5, 10, 20, 40, 60, 80, 100)], a.cfg["brightness"], pick))

    def pick_to(a):
        opts = [("Tidak pernah", 0), ("15 detik", 15), ("30 detik", 30), ("1 menit", 60), ("2 menit", 120), ("5 menit", 300)]
        a.push(chooser(a, "Layar mati otomatis", opts, a.cfg["screen_timeout"], lambda v: set_cfg(a, "screen_timeout", v, False)))

    def pick_theme(a):
        a.push(chooser(a, "Tema warna", [(t, t) for t in THEMES], a.cfg["theme"], lambda v: (set_cfg(a, "theme", v, False), a.init_colors())))

    def pick_ref(a):
        a.push(chooser(a, "Interval data", [("1 detik", 1), ("2 detik", 2), ("5 detik", 5)], a.cfg["refresh"], lambda v: set_cfg(a, "refresh", v, False)))

    def tog_ascii(a):
        set_cfg(a, "ascii", not a.cfg["ascii"], False)
        a.set_symbols()

    def tmo(a):
        a.pop()
        a.blank_screen()

    return MenuScreen("Tampilan & Daya", lambda a: [
        Item("Kecerahan", pick_br, value=lambda a: "%d%%" % a.cfg["brightness"]),
        Item("Layar mati otomatis", pick_to, value=lambda a: "off" if not a.cfg["screen_timeout"] else "%ds" % a.cfg["screen_timeout"]),
        Item("Matikan layar sekarang", tmo),
        Item("Tema warna", pick_theme, value=lambda a: a.cfg["theme"]),
        Item("Karakter", tog_ascii, value=lambda a: "ASCII" if a.cfg["ascii"] else "Unicode"),
        Item("Interval data", pick_ref, value=lambda a: "%ds" % a.cfg["refresh"]),
    ])


def system_menu(app):
    def power(cmd, label):
        def go():
            if app.demo:
                app.toast("(demo) %s" % label, "info")
            else:
                sh(["sh", "-c", "sync; " + cmd])
        return go

    def quit_all(a):
        a.stop_on_exit = True
        a.running = False

    def quit_keep(a):
        a.running = False

    def reset(a):
        a.cfg.reset()
        a.init_colors()
        a.set_symbols()
        a.toast("Konfigurasi di-reset (sandi baru dibuat)", "warn", 5)
        a.pop()

    return MenuScreen("Sistem", lambda a: [
        Item("Hotspot otomatis saat start", lambda a: toggle(a, "autostart", False), value=lambda a: "ON" if a.cfg["autostart"] else "OFF"),
        Item("Info perangkat", device_info),
        Item("Keluar TUI (hotspot tetap jalan)", quit_keep),
        Item("Matikan hotspot lalu keluar", lambda a: confirm(a, "Matikan hotspot & keluar?", lambda: quit_all(a))),
        Item("Reboot", lambda a: confirm(a, "Reboot perangkat?", power("reboot", "reboot")), danger=True),
        Item("Matikan perangkat", lambda a: confirm(a, "Matikan perangkat?", power("poweroff", "poweroff")), danger=True),
        Item("Reset konfigurasi", lambda a: confirm(a, "Reset semua pengaturan?", lambda: reset(a)), danger=True),
    ])


# ----------------------------------------------------------------------------
# Aplikasi
# ----------------------------------------------------------------------------
THEMES = {  # nama: (idx 256-warna, warna 8-warna)
    "cyan": (45, curses.COLOR_CYAN), "green": (41, curses.COLOR_GREEN),
    "amber": (214, curses.COLOR_YELLOW), "magenta": (171, curses.COLOR_MAGENTA),
    "blue": (39, curses.COLOR_BLUE),
}

SYM_U = dict(tl="┌", tr="┐", bl="└", br="┘", h="─", v="│", full="█", empty="░",
             spark=" ▁▂▃▄▅▆▇█", on="●", off="○", wait="◌", bad="✖", up="▲", dn="▼",
             sel="▸", dnl="↓", upl="↑", deg="°", mask="•", logo="◉")
SYM_A = dict(tl="+", tr="+", bl="+", br="+", h="-", v="|", full="#", empty=".",
             spark=" .:-=+*#@", on="*", off="o", wait="~", bad="x", up="^", dn="v",
             sel=">", dnl="v", upl="^", deg=" ", mask="*", logo="#")


class App:
    def __init__(self, args):
        self.args = args
        self.demo = args.demo
        self.cfg = Config(args.config)
        if args.ascii:
            self.cfg["ascii"] = True
        self.q = queue.Queue()
        self.stack = []
        self.toast_msg = None
        self.busy = None
        self.running = True
        self.stop_on_exit = False
        self.logbuf = collections.deque(maxlen=300)
        self.display = Display(self.demo)
        self.hs = Hotspot(self)
        self.stats = Stats(self)
        self.buttons = None
        self.last_input = time.time()
        self.top_margin = 4
        self.scr = None
        self.set_symbols()

    # -- dasar -----------------------------------------------------------------------
    def log(self, msg):
        self.logbuf.append(time.strftime("%H:%M:%S ") + msg)

    def set_symbols(self):
        self.sym = SYM_A if self.cfg["ascii"] else SYM_U

    def size(self):
        return self.scr.getmaxyx()

    def push(self, s):
        self.stack.append(s)

    def pop(self):
        if len(self.stack) > 1:
            self.stack.pop()

    def pop_to_root_menu(self):
        while len(self.stack) > 2:
            self.stack.pop()

    def toast(self, msg, kind="info", secs=3.0):
        self.toast_msg = (msg, kind, time.time() + secs)

    def post(self, fn):
        self.q.put(fn)

    def job(self, label, fn, then=None):
        if self.busy:
            self.toast("Sedang sibuk: %s" % self.busy, "warn")
            return
        self.busy = label

        def run():
            err, res = None, None
            try:
                res = fn()
            except Exception as e:  # noqa
                err = e
                self.log("ERR %s: %s" % (label, e))

            def fin():
                self.busy = None
                if err:
                    self.toast("Gagal: %s" % err, "bad", 7)
                elif then:
                    then(res)
                else:
                    self.toast("Selesai: %s" % label, "good")
            self.post(fin)
        threading.Thread(target=run, daemon=True).start()

    def blank_screen(self):
        self.display.blank(True, self.cfg["brightness"])
        self.scr.erase()
        self.scr.refresh()

    def wake(self):
        self.last_input = time.time()
        self.display.blanked = False
        try:
            self.display.blank(False, self.cfg["brightness"])
            self.display.set_pct(self.cfg["brightness"])
        except Exception:
            pass
        # Kernel/console blanking (consoleblank/VT powersave) can blank deeper
        # than backlight; re-enable output on wake.
        try:
            sh("setterm -term linux -blank 0 -powersave off", timeout=2)
        except Exception:
            pass
        try:
            wr("/sys/class/graphics/fb0/blank", 0)
        except Exception:
            pass
        try:
            self.scr.erase()
            self.scr.refresh()
        except Exception:
            pass

    # -- warna -------------------------------------------------------------------------
    def init_colors(self):
        curses.start_color()
        try:
            curses.use_default_colors()
        except curses.error:
            pass
        rich = curses.COLORS >= 256
        acc = THEMES.get(self.cfg["theme"], THEMES["cyan"])[0 if rich else 1]
        g, y, r = (41, 220, 203) if rich else (curses.COLOR_GREEN, curses.COLOR_YELLOW, curses.COLOR_RED)
        dim = 245 if rich else curses.COLOR_WHITE
        bg = -1
        for i, (f, b) in enumerate([(acc, bg), (g, bg), (y, bg), (r, bg), (dim, bg),
                                    (curses.COLOR_BLACK, acc), (curses.COLOR_WHITE, bg)], 1):
            try:
                curses.init_pair(i, f, b)
            except curses.error:
                pass
        B = curses.A_BOLD
        self.c = dict(
            acc=curses.color_pair(1), acc_b=curses.color_pair(1) | B,
            good=curses.color_pair(2), good_b=curses.color_pair(2) | B,
            warn=curses.color_pair(3), warn_b=curses.color_pair(3) | B,
            bad=curses.color_pair(4), bad_b=curses.color_pair(4) | B,
            dim=curses.color_pair(5) | (0 if rich else curses.A_DIM),
            hl=curses.color_pair(6) | B, wh=curses.color_pair(7), wh_b=curses.color_pair(7) | B,
        )

    def level_color(self, pct, inv=False):
        if inv:
            return self.c["good"] if pct > 50 else self.c["warn"] if pct > 20 else self.c["bad"]
        return self.c["good"] if pct < 60 else self.c["warn"] if pct < 85 else self.c["bad"]

    # -- primitif gambar ----------------------------------------------------------------
    def put(self, y, x, s, attr=0):
        h, w = self.scr.getmaxyx()
        if y != h - 1:
            y += self.top_margin
        if y < 0 or y >= h or x >= w or not s:
            return
        if x < 0:
            s, x = s[-x:], 0
        s = s[:max(0, w - x)]
        if y == h - 1 and x + len(s) >= w:
            s = s[:w - x - 1]
        try:
            self.scr.addstr(y, x, s, attr)
        except curses.error:
            pass

    def box(self, y, x, h, w, title="", attr=None):
        if h < 2 or w < 4:
            return
        S = self.sym
        a = attr or self.c["dim"]
        self.put(y, x, S["tl"] + S["h"] * (w - 2) + S["tr"], a)
        for i in range(1, h - 1):
            self.put(y + i, x, S["v"], a)
            self.put(y + i, x + w - 1, S["v"], a)
        self.put(y + h - 1, x, S["bl"] + S["h"] * (w - 2) + S["br"], a)
        if title:
            self.put(y, x + 2, " %s " % title, self.c["acc_b"])

    def bar(self, y, x, w, pct, col):
        pct = max(0.0, min(100.0, pct))
        n = int(round(w * pct / 100.0))
        self.put(y, x, self.sym["full"] * n, col)
        self.put(y, x + n, self.sym["empty"] * (w - n), self.c["dim"])

    def spark_str(self, vals, w, vmax):
        sp = self.sym["spark"]
        vals = list(vals)[-w:]
        out = []
        for v in vals:
            idx = int(v / float(vmax) * (len(sp) - 1)) if vmax else 0
            if v > 0 and idx == 0:
                idx = 1
            out.append(sp[min(len(sp) - 1, idx)])
        return "".join(out).rjust(w)

    def chart(self, y, x, w, H, vals, vmax, col):
        vals = ([0.0] * w + list(vals))[-w:]
        S = self.sym
        blocks = [" ", "▁", "▂", "▃", "▄", "▅", "▆", "▇", "█"] if S is SYM_U else [" ", ".", ".", ":", ":", "=", "=", "#", "#"]
        for r in range(H):                    # r=0 baris paling atas
            line = []
            level = (H - 1 - r) * 8
            for v in vals:
                e = int(round(v / float(vmax) * H * 8)) if vmax else 0
                line.append(blocks[max(0, min(8, e - level))])
            self.put(y + r, x, "".join(line), col)

    def draw_row(self, y, x, segs, maxw):
        cx = x
        for sg in segs:
            if cx >= x + maxw:
                break
            if sg[0] == "@bar":
                _, pct, bw, col = sg
                self.bar(y, cx, min(bw, x + maxw - cx), pct, col)
                cx += bw
            elif sg[0] == "@spark":
                _, vals, wd, vmax, col = sg
                self.put(y, cx, self.spark_str(vals, wd, vmax), col)
                cx += wd
            else:
                t, a = sg
                self.put(y, cx, t[:max(0, x + maxw - cx)], a)
                cx += len(t)

    def panel(self, y, x, w, title, rows, maxh):
        """Gambar panel berbingkai; kembalikan tinggi yang dipakai."""
        h = min(len(rows) + 2, maxh)
        if h < 3:
            return maxh
        self.box(y, x, h, w, title)
        for i, segs in enumerate(rows[:h - 2]):
            self.draw_row(y + 1 + i, x + 2, segs, w - 4)
        return h

    def header(self, left, right=""):
        h, w = self.size()
        a = self.c["hl"]
        self.put(0, 0, " " * w, a)
        self.put(0, 0, left, a)
        if right:
            self.put(0, max(len(left) + 1, w - len(right)), right, a)

    def tabbar(self, y, tabs, active):
        h, w = self.size()
        self.put(y, 0, " " * w, self.c["dim"])
        x = 1
        for i, t in enumerate(tabs):
            lbl = " %s " % t
            self.put(y, x, lbl, self.c["hl"] if i == active else self.c["dim"])
            x += len(lbl) + 1

    def footer(self, hints):
        h, w = self.size()
        # baris toast / sibuk
        y = h - 2
        self.put(y, 0, " " * w, 0)
        if self.busy:
            sp = "|/-\\"[int(time.time() * 8) % 4]
            self.put(y, 1, "%s %s..." % (sp, self.busy), self.c["warn_b"])
        elif self.toast_msg:
            msg, kind, exp = self.toast_msg
            col = {"good": "good_b", "bad": "bad_b", "warn": "warn_b"}.get(kind, "acc_b")
            self.put(y, 1, msg[:w - 2], self.c[col])
        # baris tombol
        self.put(h - 1, 0, " " * (w - 1), self.c["dim"])
        x = 1
        for k, lbl in hints:
            if x >= w - 2:
                break
            self.put(h - 1, x, k, self.c["acc_b"])
            x += len(k) + 1
            self.put(h - 1, x, lbl, self.c["dim"])
            x += len(lbl) + 2

    # -- loop utama -----------------------------------------------------------------------
    def dispatch(self, ev):
        self.last_input = time.time()
        if self.display.blanked:
            self.wake()
            return
        try:
            self.stack[-1].key(self, ev)
        except Exception:
            pass

    def tick(self):
        now = time.time()
        if self.toast_msg and now > self.toast_msg[2]:
            self.toast_msg = None
        to = self.cfg["screen_timeout"]
        if to and not self.display.blanked and now - self.last_input > to and not self.busy:
            self.blank_screen()
        if self.hs.state == "on" and not self.demo and not (self.hs.pid("hostapd") and self.hs.pid("dnsmasq")):
            self.hs.state, self.hs.error = "error", "proses hostapd/dnsmasq berhenti"
            self.log("ERR hostapd/dnsmasq berhenti tiba-tiba")
            self.toast("Hotspot berhenti tiba-tiba!", "bad", 8)
            self.wake()

    def run(self, scr):
        self.scr = scr
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        scr.keypad(True)
        scr.timeout(60)
        self.init_colors()
        self.display.set_pct(self.cfg["brightness"])
        # Disable kernel console blanking/powersave so display can be woken.
        try:
            sh("setterm -term linux -blank 0 -powersave off", timeout=2)
        except Exception:
            pass
        try:
            wr("/sys/class/graphics/fb0/blank", 0)
        except Exception:
            pass
        self.stack = [DashScreen()]
        self.buttons = Buttons(self.q, grab=not self.args.no_grab)
        self.buttons.start()
        self.stats.start()
        self.log("%s v%s mulai%s" % ("POCO-TUI", VERSION, " (mode demo)" if self.demo else ""))
        if self.demo:
            self.log("Mode demo: tidak ada perubahan pada sistem")
        elif self.buttons and not find_key_devices():
            self.log("Tombol evdev tidak ditemukan - pakai keyboard")
        if self.cfg["autostart"] and self.hs.state == "off":
            start_hotspot(self)
        keymap = {curses.KEY_UP: "up", ord("k"): "up", curses.KEY_DOWN: "down", ord("j"): "down",
                  10: "ok", 13: "ok", curses.KEY_ENTER: "ok", ord(" "): "ok", ord("l"): "ok",
                  27: "back", curses.KEY_BACKSPACE: "back", 127: "back", ord("q"): "back",
                  curses.KEY_LEFT: "back", ord("h"): "back"}
        while self.running:
            try:
                while True:
                    ev = self.q.get_nowait()
                    if callable(ev):
                        ev()
                    else:
                        self.dispatch(ev)
            except queue.Empty:
                pass
            ch = scr.getch()
            if ch != -1 and self.display.blanked:
                self.last_input = time.time()
                self.wake()
                continue
            if ch in keymap:
                self.dispatch(keymap[ch])
            self.tick()
            if self.display.blanked:
                continue
            try:
                scr.erase()
                self.stack[-1].draw(self)
            except Exception as e:  # jangan sampai crash karena gambar
                self.log("ERR draw: %s" % e)
                self.put(0, 0, "draw error: %s" % e, self.c["bad"])
            scr.refresh()

    def draw_podman(self, scr):
        h, w = scr.getmaxyx()
        scr.erase()
        self.put(0, 0, "PODMAN INFO", self.c["acc_b"])
        out = self.sh("podman ps -a 2>&1 || docker ps -a 2>&1") if hasattr(self, "sh") else None
        if out is None:
            import subprocess
            r = subprocess.run("podman ps -a 2>&1 || docker ps -a 2>&1", shell=True, capture_output=True, text=True)
            out = (r.stdout or "") + (r.stderr or "")
        for i, line in enumerate((out or "(no output)").splitlines()[:h - 2]):
            self.put(2 + i, 2, line[:w - 4], self.c["dim"])
        scr.refresh()

    def shutdown(self):
        if self.buttons:
            self.buttons.stop_flag = True
            self.buttons.join(1.0)
        if self.display.blanked:
            self.display.blank(False, self.cfg["brightness"])
        self.cfg.save()
        if self.stop_on_exit and self.hs.state == "on":
            self.hs.stop()


def main():
    global RUN
    ap = argparse.ArgumentParser(description="POCO-TUI: dashboard + mini hotspot gateway (3 tombol)")
    ap.add_argument("--demo", action="store_true", help="mode simulasi, tidak mengubah sistem")
    ap.add_argument("--config", help="path file konfigurasi")
    ap.add_argument("--no-grab", action="store_true", help="jangan ambil alih tombol (EVIOCGRAB)")
    ap.add_argument("--ascii", action="store_true", help="paksa karakter ASCII")
    ap.add_argument("--version", action="version", version="POCO-TUI " + VERSION)
    args = ap.parse_args()

    if not args.config:
        args.config = ("/etc/pocotui/config.json" if os.geteuid() == 0 and not args.demo
                       else os.path.expanduser("~/.config/pocotui/config.json"))
    if args.demo or not os.access("/run", os.W_OK):
        RUN = "/tmp/pocotui-%d" % os.getuid()
    os.makedirs(RUN, exist_ok=True)

    for loc in ("", "C.UTF-8", "en_US.UTF-8"):
        try:
            locale.setlocale(locale.LC_ALL, loc)
            break
        except locale.Error:
            continue
    if "utf" not in (locale.getpreferredencoding(False) or "").lower().replace("-", ""):
        args.ascii = True
    os.environ.setdefault("ESCDELAY", "25")
    os.environ.setdefault("TERM", "linux")

    app = App(args)
    signal.signal(signal.SIGTERM, lambda *a: setattr(app, "running", False))
    try:
        curses.wrapper(app.run)
    except KeyboardInterrupt:
        pass
    finally:
        app.shutdown()
    if app.hs.state == "on" and not app.stop_on_exit:
        print("POCO-TUI selesai. Hotspot masih berjalan (jalankan lagi untuk mengontrol).")


if __name__ == "__main__":
    main()
