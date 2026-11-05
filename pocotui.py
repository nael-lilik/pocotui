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
import shlex
import shutil
import signal
import socket
import ssl
import string
import struct
import subprocess
import sys
import threading
import time
import urllib.parse

VERSION = "1.0"
LONG_PRESS = 0.7                       # detik, POWER ditahan = "kembali"
EV_KEY = 1
KEY_POWER, KEY_VOLDOWN, KEY_VOLUP = 116, 114, 115
EVIOCGRAB = 0x40044590
EVENT = struct.Struct("llHHi")         # struct input_event (native)
MAC_RE = re.compile(r"^[0-9a-fA-F]{2}(:[0-9a-fA-F]{2}){5}$")

RUN = "/run/pocotui"                   # diatur ulang di main()
UPSTREAM_TABLE = "202"
UPSTREAM_RULE_PRIORITY = "100"
LEGACY_UPSTREAM_RULE_PRIORITIES = ("14200",)
ENV_FILES = (
    os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),
    "/etc/pocotui/.env",
    os.path.join(os.getcwd(), ".env"),
)


def load_project_env():
    paths = []
    configured_path = os.environ.get("POCOTUI_ENV_FILE")
    if configured_path:
        paths.append(configured_path)
    paths.extend(ENV_FILES)
    for path in dict.fromkeys(paths):
        try:
            with open(path) as env_file:
                for number, line in enumerate(env_file, 1):
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if line.startswith("export "):
                        line = line[7:].lstrip()
                    if "=" not in line:
                        raise ValueError("%s:%d: expected KEY=VALUE" % (path, number))
                    key, raw_value = line.split("=", 1)
                    key = key.strip()
                    if not key or not (key[0].isalpha() or key[0] == "_") or not all(
                            char.isalnum() or char == "_" for char in key):
                        raise ValueError("%s:%d: invalid environment variable name" % (path, number))
                    try:
                        values = shlex.split(raw_value, comments=True)
                    except ValueError as error:
                        raise ValueError("%s:%d: %s" % (path, number, error)) from error
                    if len(values) > 1:
                        raise ValueError("%s:%d: quote values containing spaces" % (path, number))
                    os.environ.setdefault(key, values[0] if values else "")
            return path
        except FileNotFoundError:
            continue
    return None


load_project_env()
APN = os.environ.get("APN", "")
GPS_ENABLED_VALUE = os.environ.get("GPS_ENABLED", "1").strip().lower()
if GPS_ENABLED_VALUE not in ("1", "true", "yes", "on", "0", "false", "no", "off"):
    raise ValueError("GPS_ENABLED harus berupa true/false atau 1/0")
GPS_ENABLED = GPS_ENABLED_VALUE in ("1", "true", "yes", "on")
GPS_MODEM = os.environ.get("GPS_MODEM", "0").strip()
if not re.fullmatch(r"\d+", GPS_MODEM):
    raise ValueError("GPS_MODEM harus berupa indeks modem numerik")
REDIS_URL = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0").strip()
try:
    GPS_REDIS_INTERVAL = int(os.environ.get("GPS_REDIS_INTERVAL", "60"))
    GPS_REDIS_RETENTION_DAYS = int(os.environ.get("GPS_REDIS_RETENTION_DAYS", "30"))
except ValueError as error:
    raise ValueError("GPS_REDIS_INTERVAL dan GPS_REDIS_RETENTION_DAYS harus berupa angka") from error
if GPS_REDIS_INTERVAL < 5 or GPS_REDIS_RETENTION_DAYS < 1:
    raise ValueError("GPS_REDIS_INTERVAL minimal 5 detik dan retensi minimal 1 hari")
GPS_ENABLE_LOCK = threading.Lock()


def parse_nmea_coordinate(value, hemisphere, degree_digits):
    if not value or hemisphere not in ("N", "S", "E", "W"):
        return None
    try:
        raw = float(value)
        degrees = int(raw // 100)
        minutes = raw - degrees * 100
        if minutes >= 60:
            return None
        result = degrees + minutes / 60.0
        limit = 90 if degree_digits == 2 else 180
        if result > limit or (result == limit and minutes != 0):
            return None
        return -result if hemisphere in ("S", "W") else result
    except (ValueError, TypeError):
        return None


def parse_gps_nmea(output):
    fix = {"latitude": None, "longitude": None, "altitude_m": None,
           "speed_kmh": None, "satellites": None, "hdop": None, "fix": 0}
    for line in (output or "").splitlines():
        line = line.strip()
        if not line.startswith("$") and "$" in line:
            line = line[line.index("$"):]
        if not line.startswith("$") or "*" in line and not re.search(r"\*[0-9A-Fa-f]{2}$", line):
            continue
        fields = line.split("*", 1)[0].split(",")
        kind = fields[0][-3:]
        if kind == "GGA" and len(fields) >= 10:
            try:
                fix["fix"] = int(fields[6] or 0)
                fix["satellites"] = int(fields[7]) if fields[7] else None
                fix["hdop"] = float(fields[8]) if fields[8] else None
                fix["altitude_m"] = float(fields[9]) if fields[9] else None
            except ValueError:
                continue
            fix["latitude"] = parse_nmea_coordinate(fields[2], fields[3], 2)
            fix["longitude"] = parse_nmea_coordinate(fields[4], fields[5], 3)
        elif kind == "RMC" and len(fields) >= 8:
            if fields[2] == "A":
                fix["latitude"] = parse_nmea_coordinate(fields[3], fields[4], 2)
                fix["longitude"] = parse_nmea_coordinate(fields[5], fields[6], 3)
            try:
                fix["speed_kmh"] = float(fields[7]) * 1.852 if fields[7] else None
            except ValueError:
                pass
    if fix["fix"] <= 0 or fix["latitude"] is None or fix["longitude"] is None:
        return None
    return fix


def terminal_track_map(points, width, height, zoom=1.0):
    width, height = max(1, width), max(1, height)
    grid = [[" " for _ in range(width)] for _ in range(height)]
    if not points:
        return ["".join(row) for row in grid]
    reference_lat, reference_lon = points[-1]["latitude"], points[-1]["longitude"]
    longitude_scale = max(0.01, math.cos(math.radians(reference_lat)))
    projected = [
        (((point["longitude"] - reference_lon + 180) % 360 - 180) * 111320 * longitude_scale,
         (point["latitude"] - reference_lat) * 111320)
        for point in points
    ]
    half_width = max(1, (width - 1) / 2.0)
    half_height = max(1, (height - 1) / 4.0)
    scale = max(
        max((abs(east) for east, _ in projected), default=0) / half_width,
        max((abs(north) for _, north in projected), default=0) / half_height,
        1.0,
    ) / max(0.25, min(8.0, zoom))
    center_x, center_y = (width - 1) / 2.0, (height - 1) / 2.0
    coords = [
        (int(round(center_x + east / scale)),
         int(round(center_y - north / (scale * 2))))
        for east, north in projected
    ]

    def line(x0, y0, x1, y1):
        dx, dy = abs(x1 - x0), -abs(y1 - y0)
        sx, sy = (1 if x0 < x1 else -1), (1 if y0 < y1 else -1)
        error = dx + dy
        while True:
            if 0 <= x0 < width and 0 <= y0 < height:
                grid[y0][x0] = "+"
            if x0 == x1 and y0 == y1:
                break
            doubled = 2 * error
            if doubled >= dy:
                error += dy
                x0 += sx
            if doubled <= dx:
                error += dx
                y0 += sy

    for previous, current in zip(coords, coords[1:]):
        line(previous[0], previous[1], current[0], current[1])
    for x, y in coords:
        if 0 <= x < width and 0 <= y < height:
            grid[y][x] = "."
    x, y = coords[-1]
    if 0 <= x < width and 0 <= y < height:
        grid[y][x] = "@"
    return ["".join(row) for row in grid]


def redis_read_reply(sock):
    marker = sock.recv(1)
    if not marker:
        raise ConnectionError("Redis menutup koneksi")
    line = bytearray()
    while not line.endswith(b"\r\n"):
        part = sock.recv(1)
        if not part:
            raise ConnectionError("respons Redis tidak lengkap")
        line.extend(part)
    value = bytes(line[:-2])
    if marker == b"+":
        return value.decode("utf-8", "replace")
    if marker == b"-":
        raise RuntimeError("Redis: %s" % value.decode("utf-8", "replace"))
    if marker == b":":
        return int(value)
    if marker == b"$":
        length = int(value)
        if length == -1:
            return None
        data = bytearray()
        while len(data) < length + 2:
            part = sock.recv(length + 2 - len(data))
            if not part:
                raise ConnectionError("respons Redis terpotong")
            data.extend(part)
        return bytes(data[:-2])
    raise RuntimeError("jenis respons Redis tidak dikenal")


def redis_command(sock, *args):
    encoded = [str(arg).encode("utf-8") for arg in args]
    request = [b"*" + str(len(encoded)).encode() + b"\r\n"]
    for value in encoded:
        request.extend((b"$" + str(len(value)).encode() + b"\r\n", value, b"\r\n"))
    sock.sendall(b"".join(request))
    return redis_read_reply(sock)


def save_gps_record(url, record, retention_days=30):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("redis", "rediss") or not parsed.hostname:
        raise ValueError("REDIS_URL harus menggunakan redis:// atau rediss://")
    try:
        database = int(parsed.path.strip("/") or "0")
    except ValueError as error:
        raise ValueError("nomor database Redis tidak valid") from error
    if not 0 <= database <= 15:
        raise ValueError("nomor database Redis harus 0-15")
    port = parsed.port or 6379
    user = urllib.parse.unquote(parsed.username) if parsed.username else None
    password = urllib.parse.unquote(parsed.password) if parsed.password else None
    key = "pocotui:location"
    payload = json.dumps(record, separators=(",", ":"), ensure_ascii=True)
    context = ssl.create_default_context() if parsed.scheme == "rediss" else None
    with socket.create_connection((parsed.hostname, port), timeout=3) as raw:
        sock = context.wrap_socket(raw, server_hostname=parsed.hostname) if context else raw
        sock.settimeout(3)
        if password:
            if user:
                redis_command(sock, "AUTH", user, password)
            else:
                redis_command(sock, "AUTH", password)
        if database:
            redis_command(sock, "SELECT", database)
        redis_command(sock, "XADD", key, "MAXLEN", "~", "50000", "*", "record", payload)
        redis_command(sock, "SET", key + ":latest", payload, "EX", retention_days * 86400)
        redis_command(sock, "EXPIRE", key, retention_days * 86400)


def enable_modem_gps(app):
    with GPS_ENABLE_LOCK:
        if getattr(app, "_gps_enabled_modem", None) == GPS_MODEM:
            return True, ""
        errors = []
        enabled = False
        for source in ("gps-nmea", "gps-raw"):
            rc, output = sh(["mmcli", "-m", GPS_MODEM, "--location-enable-" + source], timeout=4)
            if rc:
                errors.append(output or "ModemManager menolak sumber %s" % source)
            else:
                enabled = True
        if not enabled:
            return False, "; ".join(errors)
        app._gps_enabled_modem = GPS_MODEM
        return True, ""


def request_cached_data(app, cache_name, fetch, max_age=5):
    cache = getattr(app, cache_name, None)
    if cache is None or time.monotonic() - cache[0] >= max_age:
        fetching_name = cache_name + "_fetching"
        if not getattr(app, fetching_name, False):
            setattr(app, fetching_name, True)

            def update():
                try:
                    result = fetch()
                except Exception as error:
                    result = (1, str(error))
                setattr(app, cache_name, (time.monotonic(),) + result)
                setattr(app, fetching_name, False)

            threading.Thread(target=update, daemon=True, name=cache_name.lstrip("_")).start()
    return getattr(app, cache_name, None)


def fetch_modem_gps(app):
    enabled, error = enable_modem_gps(app)
    if not enabled:
        return 1, "Gagal mengaktifkan GPS: %s" % error
    return sh(["mmcli", "-m", GPS_MODEM, "--location-get"], timeout=4)


def fetch_gps_menu(app):
    rc, output = sh(["gpspipe", "-w", "-n", "1"], timeout=3) if which("gpspipe") else (127, "")
    return (rc, output) if rc == 0 and output.strip() else fetch_modem_gps(app)


class GPSRecorder(threading.Thread):
    def __init__(self, app):
        super().__init__(daemon=True, name="gps-redis-recorder")
        self.app = app
        self.stop_event = threading.Event()
        self.last_error_log = -300.0
        self.last_saved = None
        self.state = "menunggu fix GPS"
        self.track = collections.deque(maxlen=500)
        self.track_lock = threading.Lock()

    def run(self):
        while not self.stop_event.is_set():
            try:
                rc, output = fetch_modem_gps(self.app)
                if rc:
                    self.state = "GPS tidak tersedia"
                    now = time.monotonic()
                    if now - self.last_error_log >= 300:
                        self.app.log("PERINGATAN pembacaan GPS: %s" % (output or "ModemManager gagal"))
                        self.last_error_log = now
                    self.stop_event.wait(GPS_REDIS_INTERVAL)
                    continue
                if not output.strip():
                    self.state = "menunggu data GPS"
                    self.stop_event.wait(GPS_REDIS_INTERVAL)
                    continue
                location = parse_gps_nmea(output)
                stats = dict(self.app.stats.d or {})
                battery = stats.get("bat") or {}
                timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                if location:
                    point = {"latitude": location["latitude"], "longitude": location["longitude"],
                             "timestamp": timestamp}
                    with self.track_lock:
                        self.track.append(point)
                record = {
                    "timestamp": timestamp,
                    "source": "ModemManager",
                    "modem": GPS_MODEM,
                    "raw_gps_data": output,
                    "parsed_location": location,
                    "battery_percent": battery.get("pct"),
                    "battery_status": battery.get("status"),
                    "charging": bool(stats.get("charger", False)),
                    "temperature_c": stats.get("temp"),
                    "upstream_interface": stats.get("upif"),
                    "hotspot_active": self.app.hs.state == "on",
                    "client_count": len(stats.get("clients") or []),
                }
                if REDIS_URL:
                    save_gps_record(REDIS_URL, record, GPS_REDIS_RETENTION_DAYS)
                    self.last_saved = time.time()
                    self.state = ("tersimpan di Redis" if location else "data mentah tersimpan; menunggu fix")
                else:
                    self.state = "track lokal" if location else "menunggu fix GPS"
            except Exception as error:
                self.state = "Redis/GPS tidak tersedia"
                now = time.monotonic()
                if now - self.last_error_log >= 300:
                    self.app.log("PERINGATAN penyimpanan GPS Redis: %s" % error)
                    self.last_error_log = now
            self.stop_event.wait(GPS_REDIS_INTERVAL)


def fetch_lte_data():
    return sh(["mmcli", "-m", "0"], timeout=4)


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


def wifi_ifaces():
    """Return Wi-Fi interfaces reported by sysfs or the wireless driver."""
    names = {
        n for n in list_ifaces()
        if os.path.isdir("/sys/class/net/%s/wireless" % n)
        or os.path.exists("/sys/class/net/%s/phy80211" % n)
    }
    if which("iw"):
        _, out = sh(["iw", "dev"], timeout=3)
        names.update(re.findall(r"^\s*Interface\s+(\S+)\s*$", out, re.M))
    return sorted(names)


def is_wifi(n):
    return n in wifi_ifaces()


def default_routes(exclude=None):
    _, out = sh(["ip", "-4", "route", "show", "table", "all"], timeout=3)
    routes = []
    ignored = ("docker", "veth", "br-", "virbr", "tun", "tap")
    for line in out.splitlines():
        if not line.startswith("default "):
            continue
        dev = re.search(r"\bdev\s+(\S+)", line)
        table = re.search(r"\btable\s+(\S+)", line)
        if (not dev or dev.group(1) == exclude or dev.group(1).startswith(ignored)
                or table and table.group(1) == UPSTREAM_TABLE):
            continue
        metric = re.search(r"\bmetric\s+(\d+)", line)
        routes.append((dev.group(1), int(metric.group(1)) if metric else 0, line))
    return routes


def split_nmcli_row(line):
    fields, field = [], []
    escaped = False
    for char in line:
        if escaped:
            field.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == ":":
            fields.append("".join(field))
            field = []
        else:
            field.append(char)
    if escaped:
        field.append("\\")
    fields.append("".join(field))
    return fields


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
    lte_profile="",
    wifi_lock_bssid=True, idle_hotspot_timeout=1800, low_battery_shutdown=True,
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
        self.active_upstream = None
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
            self.active_upstream = self.upstream_iface(self.ap)
            ok, error = self.set_client_route(self.active_upstream, self.ap)
            if not ok:
                self.app.log("ERR memulihkan jalur upstream: %s" % error)
            try:
                self.started = os.path.getmtime("%s/hostapd.pid" % RUN)
            except OSError:
                self.started = time.time()

    def phy_iface(self):
        """Interface WiFi fisik (induk) - biasanya wlan0, tetap dipakai sebagai klien."""
        wifis = [n for n in wifi_ifaces() if n != self.cfg["ap_name"]]
        w = self.cfg["wlan"]
        if w != "auto" and w in wifis:
            return w
        if "wlan0" in wifis:
            return "wlan0"
        return wifis[0] if wifis else None

    def wait_for_phy(self, timeout=10):
        deadline = time.monotonic() + timeout
        while True:
            phy = self.phy_iface()
            if phy:
                return phy
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.5)

    def wifi_diagnostics(self):
        names = ", ".join(wifi_ifaces()) or "(tidak ada)"
        _, rfkill = sh(["rfkill", "list", "wifi"], timeout=3) if which("rfkill") else (127, "rfkill tidak terpasang")
        _, iw = sh(["iw", "dev"], timeout=3) if which("iw") else (127, "iw tidak terpasang")
        return "Wi-Fi=%s; rfkill=%s; iw=%s" % (
            names, (rfkill or "-").replace("\n", " ")[:100], (iw or "-").replace("\n", " ")[:100])

    def ap_iface(self):
        """Nama interface tempat AP berjalan (ap0 virtual, atau langsung wlan0)."""
        return self.cfg["ap_name"] if self.cfg["virtual_ap"] else self.phy_iface()

    def sta_freq(self, phy):
        rc, out = sh(["iw", "dev", phy, "link"], timeout=3)
        m = re.search(r"freq:\s*(\d+)", out)
        return int(m.group(1)) if m else None

    def upstream_iface(self, ap):
        u = self.cfg["upstream"]
        if u != "auto":
            return u if os.path.isdir("/sys/class/net/" + u) else None
        return self.select_auto_upstream(ap)

    def select_auto_upstream(self, ap, prefer_wifi=True, exclude_ifaces=()):
        routes = [route for route in default_routes(exclude=ap) if route[0] not in exclude_ifaces]
        if not routes:
            return None
        route_ifaces = {dev for dev, _, _ in routes}
        wifi = self.select_wifi_upstream(ap, route_ifaces) if prefer_wifi else None
        if wifi:
            return wifi
        cellular = re.compile(r"^(rmnet|wwan|ppp|ccmni|pdp|cell|lte)", re.I)
        mobile = sorted((r for r in routes if cellular.search(r[0])), key=lambda r: r[1])
        if mobile:
            return mobile[0][0]
        return min(routes, key=lambda r: r[1])[0]

    def select_wifi_upstream(self, ap, route_ifaces=None):
        if route_ifaces is None:
            route_ifaces = {dev for dev, _, _ in default_routes(exclude=ap)}
        for phy in wifi_ifaces():
            if phy == ap or phy not in route_ifaces:
                continue
            _, link = sh(["iw", "dev", phy, "link"], timeout=3)
            if "Connected to " in link:
                return phy
        return None

    def radio_for_frequency(self, freq):
        if freq < 3000:
            channel = 14 if freq == 2484 else (freq - 2407) // 5
            return "2.4", channel
        return "5", (freq - 5000) // 5

    def set_client_route(self, upstream, ap):
        subnet = str(self.net())
        route_args = ["ip", "-4", "route", "replace", subnet, "dev", ap,
                      "table", UPSTREAM_TABLE]
        rc, out = sh(route_args, timeout=3)
        if rc:
            return False, "route subnet: %s" % out[-100:]

        if not upstream:
            for priority in (UPSTREAM_RULE_PRIORITY,) + LEGACY_UPSTREAM_RULE_PRIORITIES:
                sh(["ip", "-4", "rule", "del", "from", subnet, "table", UPSTREAM_TABLE,
                    "priority", priority], timeout=3)
            rc, out = sh(["ip", "-4", "route", "del", "default", "table", UPSTREAM_TABLE], timeout=3)
            if rc and "No such process" not in out:
                return False, "route default: %s" % out[-100:]
            return True, ""

        _, route_out = sh(["ip", "-4", "route", "show", "default", "dev", upstream, "table", "all"], timeout=3)
        default = next((line for line in route_out.splitlines() if line.startswith("default ")), "")
        via = re.search(r"\bvia\s+(\S+)", default)
        default_args = ["ip", "-4", "route", "replace", "default"]
        if via:
            default_args += ["via", via.group(1)]
        default_args += ["dev", upstream, "table", UPSTREAM_TABLE]
        if via:
            default_args.insert(-2, "onlink")
        rc, out = sh(default_args, timeout=3)
        if rc:
            return False, "route upstream %s: %s" % (upstream, out[-100:])
        for priority in LEGACY_UPSTREAM_RULE_PRIORITIES:
            sh(["ip", "-4", "rule", "del", "from", subnet, "table", UPSTREAM_TABLE,
                "priority", priority], timeout=3)
        rc, rules = sh(["ip", "-4", "rule", "show"], timeout=3)
        if rc:
            return False, "gagal membaca policy rules: %s" % rules[-100:]
        priority_in_use = any(
            re.match(r"^\s*%s:\s+" % re.escape(UPSTREAM_RULE_PRIORITY), line)
            and not (re.search(r"\bfrom\s+%s\b" % re.escape(subnet), line)
                     and re.search(r"\b(?:lookup|table)\s+%s\b" % re.escape(UPSTREAM_TABLE), line))
            for line in rules.splitlines())
        if priority_in_use:
            return False, "prioritas ip rule %s sudah dipakai; pilih prioritas lain" % UPSTREAM_RULE_PRIORITY
        already_present = any(
            re.match(r"^\s*%s:\s+" % re.escape(UPSTREAM_RULE_PRIORITY), line)
            and re.search(r"\bfrom\s+%s\b" % re.escape(subnet), line)
            and re.search(r"\b(?:lookup|table)\s+%s\b" % re.escape(UPSTREAM_TABLE), line)
            for line in rules.splitlines())
        if not already_present:
            rc, out = sh(["ip", "-4", "rule", "add", "priority", UPSTREAM_RULE_PRIORITY,
                          "from", subnet, "table", UPSTREAM_TABLE], timeout=3)
            if rc:
                return False, "policy rule: %s" % out[-100:]
        return True, ""

    def avoid_upstream_overlap(self, ap):
        _, output = sh(["ip", "-4", "-o", "addr", "show"], timeout=3)
        occupied = []
        for line in output.splitlines():
            iface = re.search(r"^\d+:\s+(\S+)", line)
            address = re.search(r"\binet\s+([\d.]+/\d+)", line)
            if not iface or iface.group(1).split("@", 1)[0] == ap or not address:
                continue
            try:
                occupied.append((iface.group(1), ipaddress.ip_network(address.group(1), strict=False)))
            except ValueError:
                continue
        current = self.net()
        conflict = next(((name, net) for name, net in occupied if current.overlaps(net)), None)
        if not conflict:
            return
        candidates = ("192.168.50.0/24", "10.42.0.0/24", "172.20.0.0/24",
                      "192.168.100.0/24", "10.200.0.0/24", "172.31.0.0/24",
                      "192.168.200.0/24", "10.240.0.0/24")
        for candidate in candidates:
            network = ipaddress.ip_network(candidate)
            if all(not network.overlaps(active) for _, active in occupied):
                self.cfg["subnet"] = candidate
                self.cfg.save()
                self.app.log("subnet hotspot otomatis %s (bentrok dengan %s %s)"
                             % (candidate, conflict[0], conflict[1]))
                return
        raise RuntimeError("subnet hotspot %s bentrok dengan %s %s; tidak ada subnet cadangan yang aman"
                           % (current, conflict[0], conflict[1]))

    def clear_client_route(self, ap):
        subnet = str(self.net())
        for priority in (UPSTREAM_RULE_PRIORITY,) + LEGACY_UPSTREAM_RULE_PRIORITIES:
            sh(["ip", "-4", "rule", "del", "from", subnet, "table", UPSTREAM_TABLE,
                "priority", priority], timeout=3)
        sh(["ip", "-4", "route", "flush", "table", UPSTREAM_TABLE], timeout=3)

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
        if which("nft"):
            out_if = 'oifname "%s"' % up if up else 'oifname != "%s"' % ap
            script = ["table ip pocotui {",
                      " chain postrouting { type nat hook postrouting priority srcnat; policy accept;"]
            if nat:
                script.append('  ip saddr %s %s counter masquerade comment "pocotui"' % (net, out_if))
            script += [" }", "}"]
            rc, output = sh(["nft", "-f", "-"], inp="\n".join(script) + "\n")
            if rc:
                raise RuntimeError("nft NAT: %s" % output[-120:])

            if nat:
                forward_rules = [
                    ["iifname", ap, "oifname", up, "counter", "accept", "comment", "pocotui"]
                    if up else
                    ["iifname", ap, "oifname", "!=", ap, "counter", "accept", "comment", "pocotui"],
                    (["iifname", up] if up else ["iifname", "!=", ap])
                    + ["oifname", ap, "ct", "state", "established,related",
                       "counter", "accept", "comment", "pocotui"],
                ]
            else:
                forward_rules = [["iifname", ap, "counter", "drop", "comment", "pocotui"]]
            integration_rules = [
                ["nft", "insert", "rule", "inet", "filter", "input",
                 "iifname", ap, "counter", "accept", "comment", "pocotui"],
            ] + [
                ["nft", "insert", "rule", "inet", "filter", "forward"] + rule
                for rule in forward_rules
            ]
            for command in integration_rules:
                rc, output = sh(command)
                if rc:
                    raise RuntimeError(
                        "nft filter integration gagal (%s): %s" % (" ".join(command[-5:]), output[-120:]))
            nat_rc, nat_rules = sh(["nft", "-a", "list", "table", "ip", "pocotui"])
            input_rc, input_rules = sh(["nft", "-a", "list", "chain", "inet", "filter", "input"])
            forward_rc, forward_output = sh(["nft", "-a", "list", "chain", "inet", "filter", "forward"])
            if nat_rc or input_rc or forward_rc:
                raise RuntimeError("gagal membaca ulang ruleset nft setelah instalasi")

            def has_rule(rules, *parts):
                return any('comment "pocotui"' in line and all(part in line for part in parts)
                           for line in rules.splitlines())

            nat_oif = 'oifname "%s"' % up if up else 'oifname != "%s"' % ap
            expected_nat = has_rule(nat_rules, "ip saddr %s" % net, nat_oif, "masquerade")
            if bool(nat) != expected_nat:
                raise RuntimeError("verifikasi nft NAT gagal (subnet/interface masquerade tidak cocok)")
            if not has_rule(input_rules, 'iifname "%s"' % ap, "accept"):
                raise RuntimeError("verifikasi nft input gagal (akses klien hotspot ke perangkat)")
            if nat:
                out_match = 'oifname "%s"' % up if up else 'oifname != "%s"' % ap
                in_match = 'iifname "%s"' % up if up else 'iifname != "%s"' % ap
                if not has_rule(forward_output, 'iifname "%s"' % ap, out_match, "accept"):
                    raise RuntimeError("verifikasi nft forward gagal (jalur upstream keluar)")
                if not has_rule(forward_output, in_match, 'oifname "%s"' % ap,
                                "ct state established,related", "accept"):
                    raise RuntimeError("verifikasi nft forward gagal (paket balasan)")
            elif not has_rule(forward_output, 'iifname "%s"' % ap, "drop"):
                raise RuntimeError("verifikasi nft gagal: forwarding masih diizinkan saat NAT mati")
        elif which("iptables"):
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
        else:
            raise RuntimeError("iptables / nftables belum terpasang")

    def fw_clear(self, ap=None):
        if which("iptables"):
            for c in ["iptables -D INPUT -j POCOTUI_IN", "iptables -F POCOTUI_IN",
                      "iptables -X POCOTUI_IN", "iptables -D FORWARD -j POCOTUI_FWD",
                      "iptables -F POCOTUI_FWD", "iptables -X POCOTUI_FWD",
                      "iptables -t nat -D POSTROUTING -j POCOTUI",
                      "iptables -t nat -F POCOTUI", "iptables -t nat -X POCOTUI"]:
                sh(c)
        if which("nft"):
            if ap:
                for chain in ("input", "forward"):
                    _, output = sh(["nft", "-a", "list", "chain", "inet", "filter", chain])
                    for line in output.splitlines():
                        if 'comment "pocotui"' not in line:
                            continue
                        handle = re.search(r"# handle (\d+)", line)
                        if handle:
                            sh(["nft", "delete", "rule", "inet", "filter", chain,
                                "handle", handle.group(1)])
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
            for b in ("hostapd", "dnsmasq", "ip", "iw"):
                if not which(b):
                    raise RuntimeError("%s belum terpasang" % b)
            if which("rfkill"):
                rc, out = sh(["rfkill", "unblock", "wifi"], timeout=3)
                if rc:
                    app.log("PERINGATAN: rfkill unblock gagal: %s" % out[-100:])
            phy = self.wait_for_phy()
            if not phy:
                raise RuntimeError("interface Wi-Fi tidak ditemukan setelah menunggu 10 detik. "
                                   "Periksa driver/firmware, airplane mode, dan rfkill. %s"
                                   % self.wifi_diagnostics())
            virtual = bool(c["virtual_ap"])
            ap = c["ap_name"] if virtual else phy
            self.ap = ap
            self.eff = (c["band"], int(c["channel"]))
            self._cleanup(ap, quiet=True)
            os.makedirs(RUN, exist_ok=True)
            up = self.upstream_iface(ap) if c["upstream"] == "auto" else c["upstream"]
            up_rule = None if c["upstream"] == "auto" else c["upstream"]
            self.avoid_upstream_overlap(ap)
            app.log("AP=%s upstream=%s" % (ap, up or "(belum ada)"))

            if virtual:
                # wlan0 TIDAK disentuh: AP dibuat di interface virtual terpisah
                rc, out = sh(["iw", "dev", phy, "interface", "add", ap, "type", "__ap"])
                if rc:
                    raise RuntimeError("gagal membuat %s di %s: %s (driver mungkin tak mendukung AP+STA)"
                                       % (ap, phy, out[-70:]))
                try:                          # MAC harus beda dari wlan0
                    o = [int(x, 16) for x in rd("/sys/class/net/%s/address" % phy).split(":")]
                    o[0] |= 0x02
                    o[5] = (o[5] + 1) & 0xFF
                    sh(["ip", "link", "set", ap, "address", ":".join("%02x" % x for x in o)])
                except Exception:
                    pass
                if which("nmcli"):
                    sh(["nmcli", "dev", "set", ap, "managed", "no"])
                f = self.sta_freq(phy)        # radio tunggal: channel AP harus = channel wlan0
                if f:
                    band, ch = self.radio_for_frequency(f)
                    if (band, ch) != self.eff:
                        app.log("channel mengikuti %s: CH %d (%s GHz)" % (phy, ch, band))
                    self.eff = (band, ch)
            else:
                if which("nmcli"):
                    sh(["nmcli", "dev", "set", ap, "managed", "no"])
                if which("wpa_cli"):
                    sh(["wpa_cli", "-i", ap, "terminate"])
            net = self.net()
            sh(["ip", "link", "set", ap, "down"])
            sh(["ip", "addr", "flush", "dev", ap])
            rc, out = sh(["ip", "addr", "add", "%s/%d" % (self.gw(), net.prefixlen), "dev", ap])
            if rc:
                raise RuntimeError("ip addr: %s" % out[-80:])
            rc, out = sh(["ip", "link", "set", ap, "up"])
            if rc:
                raise RuntimeError("gagal mengaktifkan interface %s: %s" % (ap, out[-80:]))

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
            ok, route_error = self.set_client_route(up, ap)
            if not ok:
                raise RuntimeError("gagal mengatur jalur upstream klien: %s" % route_error)
            self.fw_apply(ap, up_rule)
            self.active_upstream = up
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
        self.fw_clear(ap)
        self.clear_client_route(ap)
        self.active_upstream = None
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


class UpstreamMonitor(threading.Thread):
    def __init__(self, app):
        super().__init__(daemon=True)
        self.app = app
        self.stop_event = threading.Event()
        self.last_lte_action = 0.0
        self.last_lte_target = None
        self.last_wifi_probe = 0.0
        self.wifi_probe_iface = None
        self.wifi_probe_result = None
        self.last_wifi_state = None
        self.lte_active = None
        self.last_route_error = None
        self.last_radio_warning = None
        self.last_lte_error = None

    def run(self):
        while not self.stop_event.wait(5):
            hs = self.app.hs
            if self.app.demo or hs.state != "on":
                continue
            try:
                wifi = self.usable_wifi(hs)
                self.sync_lte(hs, wifi)
                self.sync_upstream(hs, wifi)
                self.sync_radio(hs)
            except Exception as e:
                self.app.log("ERR monitor upstream: %s" % e)

    def sync_lte(self, hs, wifi):
        profile = hs.cfg["lte_profile"]
        if hs.cfg["upstream"] != "auto" or not profile or not which("nmcli"):
            self.lte_active = None
            return
        desired_active = not bool(wifi)
        rc, output = sh(["nmcli", "-t", "--escape", "yes", "-f", "UUID",
                         "connection", "show", "--active"], timeout=5)
        if rc:
            self.lte_active = None
            message = "PERINGATAN: gagal memeriksa profil LTE: %s" % output[-100:]
            if message != self.last_lte_error:
                self.app.log(message)
                self.last_lte_error = message
            return
        self.last_lte_error = None
        active = profile in {line.strip() for line in output.splitlines()}
        self.lte_active = active
        if active == desired_active:
            self.last_lte_target = desired_active
            return
        if (self.last_lte_target == desired_active
                and time.monotonic() - self.last_lte_action < 30):
            return
        command = ["nmcli", "connection", "down" if wifi else "up", "uuid", profile]
        self.last_lte_target = desired_active
        rc, output = sh(command, timeout=25)
        self.last_lte_action = time.monotonic()
        if rc:
            self.app.log("PERINGATAN: LTE %s gagal: %s" % ("standby" if wifi else "backup", output[-100:]))
        else:
            self.last_lte_target = desired_active
            self.app.log("LTE %s" % ("standby (Wi-Fi upstream aktif)" if wifi else "backup diaktifkan"))

    def usable_wifi(self, hs):
        iface = hs.select_wifi_upstream(hs.ap)
        if not iface:
            if self.last_wifi_state is not None:
                self.app.log("Wi-Fi upstream terputus")
            self.last_wifi_state = None
            self.wifi_probe_iface = None
            self.wifi_probe_result = None
            return None
        if not which("ping"):
            return iface
        now = time.monotonic()
        if iface != self.wifi_probe_iface or now - self.last_wifi_probe >= 20:
            rc, _ = sh(["ping", "-I", iface, "-c", "1", "-W", "2", "1.1.1.1"], timeout=4)
            self.wifi_probe_iface = iface
            self.wifi_probe_result = rc == 0
            self.last_wifi_probe = now
        online = bool(self.wifi_probe_result)
        if online != self.last_wifi_state:
            self.app.log("Wi-Fi upstream %s: %s" % ("online" if online else "tanpa internet", iface))
            self.last_wifi_state = online
        return iface if online else None

    def sync_upstream(self, hs, wifi):
        if hs.cfg["upstream"] != "auto":
            return
        excluded = set() if wifi else set(wifi_ifaces())
        if not wifi and self.wifi_probe_iface:
            excluded.add(self.wifi_probe_iface)
        upstream = hs.select_auto_upstream(hs.ap, prefer_wifi=bool(wifi), exclude_ifaces=excluded)
        if upstream == hs.active_upstream:
            return
        ok, error = hs.set_client_route(upstream, hs.ap)
        if not ok:
            if error != self.last_route_error:
                self.app.log("ERR pindah upstream %s: %s" % (upstream or "(none)", error))
                self.last_route_error = error
            return
        self.last_route_error = None
        old = hs.active_upstream
        hs.active_upstream = upstream
        self.app.log("upstream otomatis: %s -> %s" % (old or "(none)", upstream or "(none)"))

    def sync_radio(self, hs):
        phy = hs.phy_iface()
        if not phy:
            return
        freq = hs.sta_freq(phy)
        if not freq:
            return
        radio = hs.radio_for_frequency(freq)
        if radio == hs.eff:
            self.last_radio_warning = None
            return
        if radio != self.last_radio_warning:
            self.last_radio_warning = radio
            self.app.log("PERINGATAN: channel upstream berubah ke CH %d (%s GHz); AP tidak di-restart otomatis"
                         % (radio[1], radio[0]))


# ----------------------------------------------------------------------------
# Statistik sistem (thread terpisah)
# ----------------------------------------------------------------------------
class Stats(threading.Thread):
    def __init__(self, app):
        super().__init__(daemon=True)
        self.app = app
        self.d = {}
        self.clients_sample_valid = False
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
        d["clients_valid"] = bool(hs.state == "on" and self.clients_sample_valid)
        d["ping"] = self.ping
        return d

    def get_clients(self):
        hs = self.app.hs
        stas = {}
        rc, out = hs.cli("all_sta", timeout=2)
        self.clients_sample_valid = rc == 0
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
        d["clients_valid"] = True
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

    def __init__(self, title, build, on_back=None):
        self.title, self.build, self.sel = title, build, 0
        self.on_back = on_back

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
            if self.on_back:
                self.on_back()

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

    def __init__(self, title, text, on_done, validate=None, maxlen=63, note="", on_cancel=None):
        self.title, self.text, self.on_done = title, text, on_done
        self.validate, self.maxlen, self.note = validate, maxlen, note
        self.on_cancel = on_cancel
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
                if self.on_cancel:
                    self.on_cancel()
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


class TerminalMapScreen:
    hints = [("VOL+", "Zoom +"), ("VOL-", "Zoom -"), ("PWR tahan", "Kembali")]

    def __init__(self):
        self.zoom = 1.0

    def key(self, app, ev):
        if ev == "up":
            self.zoom = min(8.0, self.zoom * 1.5)
        elif ev == "down":
            self.zoom = max(0.25, self.zoom / 1.5)
        elif ev in ("ok", "back"):
            app.pop()

    def draw(self, app):
        h, w = app.size()
        app.header(" Peta GPS Lokal ", "VOL+/-: zoom ")
        if not GPS_ENABLED:
            app.put(2, 2, "GPS dinonaktifkan oleh GPS_ENABLED di .env", app.c["warn"])
            app.footer(self.hints)
            return

        cache = request_cached_data(app, "_gps_cache", lambda: fetch_modem_gps(app))
        current = None
        if cache and cache[1] == 0:
            current = parse_gps_nmea(cache[2])
        recorder = getattr(app, "gps_recorder", None)
        points = []
        if recorder:
            with recorder.track_lock:
                points = list(recorder.track)
        if current:
            if not points or (points[-1]["latitude"], points[-1]["longitude"]) != (
                    current["latitude"], current["longitude"]):
                points.append(current)
        if not points:
            status = recorder.state if recorder else "GPS tidak tersedia"
            app.put(2, 2, "Belum ada posisi GPS valid.", app.c["warn"])
            app.put(3, 2, status, app.c["dim"])
            app.put(5, 2, "Peta ini menggambar lintasan lokal; tidak mengunduh tile peta.", app.c["dim"])
            app.footer(self.hints)
            return

        map_y, map_x = 2, 1
        map_h = max(3, h - app.top_margin - 6)
        map_w = max(1, w - 2)
        lines = terminal_track_map(points, map_w, map_h, self.zoom)
        for row, text in enumerate(lines):
            app.put(map_y + row, map_x, text, app.c["dim"])
        latest = points[-1]
        app.put(map_y + map_h, 1,
                "@ posisi terkini  . lintasan  N ^  |  %.6f, %.6f"
                % (latest["latitude"], latest["longitude"]), app.c["acc_b"])
        fix_details = []
        if current:
            fix_details.append("%d sat" % (current.get("satellites") or 0))
            if current.get("altitude_m") is not None:
                fix_details.append("%.0f m" % current["altitude_m"])
            if current.get("speed_kmh") is not None:
                fix_details.append("%.1f km/j" % current["speed_kmh"])
        else:
            fix_details.append("GPS menunggu fix")
        app.put(map_y + map_h + 1, 1,
                "%d titik sesi  |  zoom %.1fx  |  %s  |  %s"
                % (len(points), self.zoom, ", ".join(fix_details),
                   recorder.state if recorder else "GPS aktif"),
                app.c["dim"])
        app.footer(self.hints)


class DashScreen:
    TABS = ["Ringkasan", "Jaringan", "Klien", "Trafik", "Log"]
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
        [self.ov, self.nw, self.cl, self.tr, self.lg][self.tab](app, d, y0, y1, w)
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
        up = hs.active_upstream if st == "on" else d.get("upif")
        uip = (d.get("ips") or {}).get(up, "") if up else ""
        mode = "AUTO" if cfg["upstream"] == "auto" else "MANUAL"
        rows.append([("Upstream ", dim),
                     (mode + "  " + (up or "(belum ada)") + ("  " + uip if uip else ""),
                      c["wh"] if up else c["warn"]),
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

    def nw(self, app, d, y0, y1, w):
        c, hs = app.c, app.hs
        monitor = app.upstream_monitor
        dim = c["dim"]
        up = hs.active_upstream if hs.state == "on" else d.get("upif")
        uip = (d.get("ips") or {}).get(up, "") if up else ""
        wifi_iface = monitor.wifi_probe_iface
        if wifi_iface and monitor.wifi_probe_result is not None:
            wifi_status = "online" if monitor.wifi_probe_result else "tersambung, internet gagal"
            wifi_color = c["good"] if monitor.wifi_probe_result else c["bad"]
        else:
            wifi_status, wifi_color = "tidak terhubung / belum diuji", dim
        profile = app.cfg["lte_profile"]
        if app.cfg["upstream"] != "auto":
            lte_status, lte_color = "nonaktif (mode manual)", dim
        elif not profile:
            lte_status, lte_color = "profil backup belum dipilih", dim
        elif monitor.lte_active is True:
            lte_status, lte_color = "aktif", c["good"]
        elif monitor.lte_active is False:
            lte_status, lte_color = "standby", c["warn"]
        else:
            lte_status, lte_color = "status belum diketahui", dim
        route_kind = "Wi-Fi" if up and (
            up == wifi_iface or re.match(r"^(wlan|wlp|wlx|wifi)", up, re.I)) else (
                "LTE" if up and re.match(r"^(rmnet|wwan|ppp|ccmni|pdp|cell|lte)", up, re.I) else "lainnya")
        rows = [
            [("Mode       ", dim), ("otomatis" if app.cfg["upstream"] == "auto" else "manual", c["acc"])],
            [("Jalur aktif", dim), ((route_kind + " / " + up) if up else "belum ada upstream",
                                    c["good"] if up else c["bad"])],
            [("Alamat IPv4", dim), (uip or "-", c["wh"])],
            [("Wi-Fi      ", dim), ((wifi_iface + ": " if wifi_iface else "") + wifi_status, wifi_color)],
            [("LTE backup ", dim), (lte_status, lte_color)],
            [("Ping sistem", dim), ("online %.0f ms" % d["ping"][0] if d.get("ping", (None, 0))[0] is not None
                                    else "tidak terjangkau", c["good"] if d.get("ping", (None, 0))[0] is not None else c["warn"])],
        ]
        y = y0
        if w >= 100:
            left_w = w // 2
            app.panel(y, 0, left_w - 1, "UPSTREAM", rows, y1 - y)
            app.panel(y, left_w, w - left_w, "MODEM LTE", self.rows_lte(app, d, w - left_w - 4),
                      y1 - y)
            return
        used = app.panel(y, 0, w, "UPSTREAM", rows, max(3, (y1 - y0) // 2))
        y += used
        if y < y1:
            app.panel(y, 0, w, "MODEM LTE", self.rows_lte(app, d, w - 4), y1 - y)

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
        cache = request_cached_data(app, "_lte_cache", fetch_lte_data)
        if cache is None:
            return [[("Modem    ", dim), ("membaca status...", dim)]]
        _, rc, out = cache
        if rc == 0:
            for key in ("state:", "access tech:", "signal quality:", "power state:", "manufacturer:", "model:"):
                for l in out.splitlines():
                    if key in l:
                        rows.append([(key.replace(":", "").ljust(9), dim), (l.split(":", 1)[-1].strip(), c["wh"])])
            # operator name dari mmcli 3GPP (cached)
            for l in out.splitlines():
                if "operator name" in l.lower():
                    rows.append([("Operator ", dim), (l.split(":", 1)[-1].strip(), c["acc"])])
                elif "operator id" in l.lower():
                    rows.append([("Op ID    ", dim), (l.split(":", 1)[-1].strip(), c["dim"])])
                elif "registration:" in l.lower():
                    rows.append([("Reg      ", dim), (l.split(":", 1)[-1].strip(), c["acc"])])
                elif "packet service state" in l.lower():
                    rows.append([("Pkt state", dim), (l.split(":", 1)[-1].strip(), c["dim"])])
            for l in out.splitlines():
                if "equipment id" in l.lower():
                    rows.append([("IMEI     ", dim), (l.split(":", 1)[-1].strip(), c["wh"])])
        else:
            message = (out.splitlines() or ["tidak ditemukan"])[0]
            rows.append([("Modem    ", dim), (message[:max(1, inner - 10)], c["bad"])])
        return rows or [[("Modem    ", dim), ("n/a", c["dim"])]]

    def rows_gps(self, app, d, inner):
        c = app.c
        dim = c["dim"]
        if not GPS_ENABLED:
            return [[("GPS      ", dim), ("dinonaktifkan (.env)", dim)]]
        rows = []
        cache = request_cached_data(app, "_gps_cache", lambda: fetch_modem_gps(app))
        if cache is None:
            return [[("GPS      ", dim), ("mengambil data...", dim)]]
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
                    except (ValueError, IndexError, TypeError):
                        pass
                if "RMC" in l:
                    parts = l.split(",")
                    try:
                        if parts[7]:
                            speed = float(parts[7]) * 1.852  # knots -> km/h
                    except (ValueError, IndexError, TypeError):
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
            try:
                has_altitude = float(alt or 0) != 0
            except (TypeError, ValueError):
                has_altitude = False
            rows.append([("Mode     ", dim), ("3D" if fix in ("1","2","3","4","5","6") and has_altitude else "2D", c["wh"])])
        else:
            # fallback: tampilkan raw
            if rc2 == 0:
                for l in (out2 or "").splitlines():
                    if l.strip() and not l.strip().startswith("-"):
                        rows.append([("GPS      ", dim), (l.strip()[:inner - 10], c["wh"])])
            else:
                rows.append([("GPS      ", dim), ((out2 or "tidak tersedia")[:inner - 10], c["warn"])])
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


def chooser(app, title, options, current, on_pick, on_back=None):
    def build(a):
        return [Item(lbl, (lambda a, v=val: (a.pop(), on_pick(v))), value=(app.sym["on"] if val == current else ""))
                for lbl, val in options]
    sc = MenuScreen(title, build, on_back=on_back)
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
                "  Mode upstream: %s" % ("otomatis" if c["upstream"] == "auto" else "manual"),
                "  Radio AP     : %s" % (("%s GHz CH %s" % hs.eff) if hs.eff else "-"),
                "  NAT          : %s" % ("aktif" if c["nat"] else "mati (hanya LAN lokal)"),
                "  Diblokir     : %d perangkat" % len(c["blocked"])]
    app.push(TextView("Info Koneksi", lines))


def show_ifaces(app):
    def lines(a):
        d = a.stats.d or {}
        cn = net_counters()
        out = ["  %-12s %-8s %-6s %-18s %s" % ("IFACE", "STATUS", "TYPE", "IP", "RX / TX")]
        names = list_ifaces() if not a.demo else ["wlan0", "usb0", "wwan0"]
        wifis = set(wifi_ifaces()) if not a.demo else {"wlan0"}
        for n in names:
            st = rd("/sys/class/net/%s/operstate" % n, "up")
            rx, tx = cn.get(n, (0, 0))
            kind = "Wi-Fi" if n in wifis else ""
            out.append("  %-12s %-8s %-6s %-18s %s / %s" % (n, st, kind, (d.get("ips") or {}).get(n, "-"), fmt_bytes(rx), fmt_bytes(tx)))
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

def refresh_wifi(app):
    def fn():
        if app.demo:
            time.sleep(0.8)
            return
        if which("rfkill"):
            sh(["rfkill", "unblock", "wifi"], timeout=3)
        phy = app.hs.wait_for_phy(timeout=2)
        if not phy:
            raise RuntimeError("interface Wi-Fi tidak ditemukan. %s" % app.hs.wifi_diagnostics())
        if which("nmcli"):
            rc, out = sh(["nmcli", "dev", "wifi", "rescan", "ifname", phy], timeout=10)
            if rc == 0:
                return
            app.log("nmcli rescan gagal (%s), mencoba iw" % out[-80:])
        if which("iw"):
            rc, out = sh(["iw", "dev", phy, "scan"], timeout=12)
            if rc == 0:
                return
            raise RuntimeError("scan %s gagal: %s" % (phy, out[-100:]))
        raise RuntimeError("nmcli/iw belum terpasang untuk scan Wi-Fi")
    app.job("Scan ulang Wi-Fi", fn, then=lambda r: app.toast("Wi-Fi di-refresh", "good"))


def toggle_wifi_bssid_lock(app):
    enabled = not app.cfg["wifi_lock_bssid"]

    def apply():
        if not app.demo:
            if not which("nmcli"):
                raise RuntimeError("mengubah kunci BSSID memerlukan NetworkManager/nmcli")
            phy = app.hs.phy_iface()
            if phy:
                rc, link = sh(["iw", "dev", phy, "link"], timeout=4)
                bssid = re.search(r"Connected to\s+([0-9a-fA-F:]{17})", link)
                if bssid:
                    uuid_rc, uuid = sh(["nmcli", "-g", "GENERAL.CON-UUID", "device", "show", phy], timeout=5)
                    if uuid_rc or not re.fullmatch(r"[0-9a-fA-F-]{36}", uuid.strip()):
                        raise RuntimeError("profil Wi-Fi aktif tidak ditemukan")
                    value = bssid.group(1).lower() if enabled else ""
                    rc, output = sh(["nmcli", "connection", "modify", "uuid", uuid.strip(),
                                     "802-11-wireless.bssid", value], timeout=8)
                    if rc:
                        raise RuntimeError(output[-100:] or "gagal mengubah kunci BSSID")
                elif enabled and "Not connected." not in link:
                    raise RuntimeError("BSSID aktif tidak dapat dibaca; pengaturan tidak diubah")
        app.cfg["wifi_lock_bssid"] = enabled
        app.cfg.save()

    app.job("Mengubah kunci BSSID", apply,
            then=lambda _: app.toast("Kunci BSSID %s" % ("aktif" if enabled else "dilepas"), "good"))


def smart_wifi_choice(networks, traffic_rate):
    if not networks:
        return None, None
    strongest = max(networks, key=lambda record: (record[2], record[0].lower()))
    current = next((record for record in networks if record[4]), None)
    if current and traffic_rate >= 128 * 1024:
        return current, "traffic aktif"
    if current and current[2] >= strongest[2] - 15:
        return current, "sinyal masih dalam ambang stabil"
    return strongest, None


def connect_wifi(app):
    resume_hotspot = app.hs.state == "on"
    stats = app.stats.d or {}
    traffic_at_start = float(stats.get("down") or 0) + float(stats.get("up") or 0)

    def restore_hotspot():
        if resume_hotspot and app.hs.state == "off":
            app.job("Menyalakan kembali hotspot", app.hs.start,
                    then=lambda _: app.toast("Hotspot kembali aktif", "good"))

    def scan():
        if app.demo:
            return "wlan0", [("Demo-WiFi", "WPA2", 87, "02:00:00:00:00:01", True),
                            ("Guest", "--", 42, "02:00:00:00:00:02", False)]
        if not which("nmcli"):
            raise RuntimeError("Wizard Wi-Fi memerlukan NetworkManager/nmcli")
        if which("rfkill"):
            sh(["rfkill", "unblock", "wifi"], timeout=3)
        phy = app.hs.wait_for_phy(timeout=2)
        if not phy:
            raise RuntimeError("interface Wi-Fi tidak ditemukan. %s" % app.hs.wifi_diagnostics())
        sh(["nmcli", "dev", "wifi", "rescan", "ifname", phy], timeout=15)
        rc, out = sh(["nmcli", "-t", "--escape", "yes", "-f", "IN-USE,SSID,SIGNAL,SECURITY,BSSID",
                      "dev", "wifi", "list", "ifname", phy, "--rescan", "no"], timeout=15)
        if rc:
            raise RuntimeError(out[-120:] or "gagal memindai jaringan Wi-Fi")
        by_ssid = {}
        for line in out.splitlines():
            fields = split_nmcli_row(line)
            if len(fields) < 5 or not fields[1]:
                continue
            try:
                signal_pct = int(fields[2])
            except ValueError:
                signal_pct = 0
            record = (fields[1], fields[3] or "--", signal_pct, fields[4], fields[0] == "*")
            previous = by_ssid.get(fields[1])
            if (previous is None or (record[4] and not previous[4])
                    or (record[4] == previous[4] and signal_pct > previous[2])):
                by_ssid[fields[1]] = record
        return phy, sorted(by_ssid.values(), key=lambda row: (-row[2], row[0].lower()))

    def connect_selected(phy, ssid, security, password="", hidden=False, bssid=""):
        target_bssid = bssid

        def fn():
            try:
                if app.demo:
                    time.sleep(0.5)
                else:
                    cmd = ["nmcli", "dev", "wifi", "connect", ssid]
                    if password:
                        cmd += ["password", password]
                    cmd += ["ifname", phy]
                    if hidden:
                        cmd += ["hidden", "yes"]
                    if target_bssid:
                        cmd += ["bssid", target_bssid]
                    rc, out = sh(cmd, timeout=35)
                    if rc:
                        raise RuntimeError(out[-120:] or "gagal menyambungkan Wi-Fi")
                    uuid_rc, uuid = sh(["nmcli", "-g", "GENERAL.CON-UUID", "device", "show", phy], timeout=5)
                    if uuid_rc or not re.fullmatch(r"[0-9a-fA-F-]{36}", uuid.strip()):
                        raise RuntimeError("Wi-Fi tersambung, tetapi profil NetworkManager tidak ditemukan")
                    profile_args = ["nmcli", "connection", "modify", "uuid", uuid.strip(),
                                    "connection.autoconnect", "yes", "connection.autoconnect-priority", "100",
                                    "ipv4.route-metric", "100", "ipv6.route-metric", "100"]
                    if app.cfg["wifi_lock_bssid"]:
                        _, link = sh(["iw", "dev", phy, "link"], timeout=4)
                        active_bssid = re.search(r"Connected to\s+([0-9a-fA-F:]{17})", link)
                        if active_bssid:
                            profile_args += ["802-11-wireless.bssid", active_bssid.group(1).lower()]
                        else:
                            app.log("PERINGATAN: Wi-Fi tersambung, tetapi BSSID aktif tidak dapat dikunci")
                    else:
                        profile_args += ["802-11-wireless.bssid", ""]
                    rc, out = sh(profile_args, timeout=8)
                    if rc:
                        raise RuntimeError("Wi-Fi tersambung, gagal menyimpan auto-connect: %s" % out[-100:])
                    app.cfg["upstream"] = "auto"
                    app.cfg.save()
                if resume_hotspot:
                    app.hs.start()
            except Exception:
                if resume_hotspot and app.hs.state == "off":
                    app.hs.start()
                raise

        app.job("Menyambungkan Wi-Fi", fn,
                then=lambda _: app.toast("Wi-Fi %s tersimpan; upstream otomatis aktif" % ssid, "good", 5))

    def choose_network(result):
        phy, networks = result
        options = []
        if networks:
            options.append(("Smart pilih: tahan koneksi sehat / sinyal terbaik", "__auto_best__"))
        for record in networks:
            ssid, security, signal_pct, _, in_use = record
            label = "%s  %d%%  %s%s" % (
                ssid, signal_pct, security, "  [terhubung]" if in_use else "")
            options.append((label, record))
        options += [("Jaringan tersembunyi / masukkan SSID", "__hidden__"), ("Batal", "__cancel__")]

        def keep_current(record, reason):
            ssid = record[0]
            app.cfg["upstream"] = "auto"
            app.cfg.save()
            app.log("Smart Wi-Fi mempertahankan %s (%s)" % (ssid, reason))
            if resume_hotspot and app.hs.state == "off":
                app.job("Melanjutkan hotspot", app.hs.start,
                        then=lambda _: app.toast("Upstream %s dipertahankan; hotspot aktif" % ssid, "good", 5))
            else:
                app.toast("Upstream %s dipertahankan" % ssid, "good", 5)

        def selected(value):
            if value == "__cancel__":
                restore_hotspot()
            elif value == "__hidden__":
                app.push(EditScreen("SSID Wi-Fi", "", lambda ssid: choose_security(phy, ssid, "WPA2", True),
                                    lambda text: None if 1 <= len(text.encode("utf-8")) <= 32 else "SSID harus 1-32 byte",
                                    32, on_cancel=restore_hotspot))
            elif value == "__auto_best__":
                choice, reason = smart_wifi_choice(networks, traffic_at_start)
                if reason:
                    keep_current(choice, reason)
                else:
                    choose_security(phy, choice[0], choice[1], bssid=choice[3])
            else:
                choose_security(phy, value[0], value[1], bssid=value[3])

        if not networks:
            app.toast("Tidak ada jaringan terdeteksi; pilih SSID tersembunyi", "warn", 5)
        app.push(chooser(app, "Pilih upstream Wi-Fi", options, None, selected,
                         on_back=restore_hotspot))

    def choose_security(phy, ssid, security, hidden=False, bssid=""):
        if not ssid:
            app.toast("SSID tidak boleh kosong", "warn")
            return
        if security in ("", "--", "OPEN"):
            connect_selected(phy, ssid, security, hidden=hidden, bssid=bssid)
            return
        app.push(EditScreen("Sandi Wi-Fi: %s" % ssid, "",
                            lambda password: connect_selected(phy, ssid, security, password, hidden, bssid),
                            lambda text: None if not text or 8 <= len(text) <= 63 else "Sandi WPA harus 8-63 karakter",
                            63, "Sandi disimpan di profil NetworkManager. Profil diatur auto-connect.",
                            on_cancel=restore_hotspot))

    def start_scan():
        def scan_and_restore():
            try:
                return scan()
            except Exception:
                if resume_hotspot and app.hs.state == "off":
                    app.hs.start()
                raise

        app.job("Memindai jaringan Wi-Fi", scan_and_restore, then=choose_network)

    if resume_hotspot:
        confirm(app, "Wi-Fi satu radio: scan perlu menjeda hotspot dan memutus klien. Lanjutkan?",
                lambda: app.job("Menjeda hotspot", app.hs.stop, then=lambda _: start_scan()))
    else:
        start_scan()


def configure_lte_backup(app):
    def list_profiles():
        if not which("nmcli"):
            raise RuntimeError("Profil LTE otomatis memerlukan NetworkManager/nmcli")
        rc, output = sh(["nmcli", "-t", "--escape", "yes", "-f", "UUID,NAME,TYPE",
                         "connection", "show"], timeout=8)
        if rc:
            raise RuntimeError(output[-120:] or "gagal membaca profil NetworkManager")
        profiles = []
        for line in output.splitlines():
            fields = split_nmcli_row(line)
            if len(fields) >= 3 and fields[2] in ("gsm", "cdma"):
                profiles.append((fields[0], fields[1], fields[2]))
        return profiles

    def activate_profile(uuid, name):
        def fn():
            rc, output = sh(["nmcli", "connection", "modify", "uuid", uuid,
                             "connection.autoconnect", "no", "connection.autoconnect-priority", "10",
                             "ipv4.route-metric", "700", "ipv6.route-metric", "700"], timeout=8)
            if rc:
                raise RuntimeError(output[-120:] or "gagal mengatur profil LTE")
            app.cfg["lte_profile"] = uuid
            app.cfg.save()

        app.job("Mengatur profil LTE backup", fn,
                then=lambda _: app.toast("LTE aktif otomatis saat Wi-Fi upstream terputus: %s" % name, "good", 6))

    def create_profile(apn):
        def fn():
            rc, output = sh(["nmcli", "connection", "add", "type", "gsm", "ifname", "*",
                             "con-name", "POCO-TUI LTE", "gsm.apn", apn], timeout=15)
            if rc:
                raise RuntimeError(output[-120:] or "gagal membuat profil LTE")
            match = re.search(r"\(([0-9a-fA-F-]{36})\)", output)
            if not match:
                raise RuntimeError("NetworkManager membuat profil, tetapi UUID tidak ditemukan")
            uuid = match.group(1)
            rc, output = sh(["nmcli", "connection", "modify", "uuid", uuid,
                             "connection.autoconnect", "no", "connection.autoconnect-priority", "10",
                             "ipv4.route-metric", "700", "ipv6.route-metric", "700"], timeout=8)
            if rc:
                raise RuntimeError("Profil dibuat, tetapi pengaturan backup gagal: %s" % output[-100:])
            app.cfg["lte_profile"] = uuid
            app.cfg.save()

        app.job("Membuat profil LTE backup", fn,
                then=lambda _: app.toast("Profil LTE tersimpan; modem akan aktif saat Wi-Fi upstream terputus", "good", 6))

    def show_profiles(profiles):
        options = [("%s (%s)" % (name, kind), uuid) for uuid, name, kind in profiles]
        options.append(("Buat profil LTE baru (masukkan APN)", "__create__"))

        def selected(value):
            if value == "__create__":
                app.push(EditScreen("APN operator", APN, create_profile,
                                    lambda text: None if 1 <= len(text) <= 100 else "APN harus 1-100 karakter",
                                    100, "APN dari .env (dapat diedit); cek nilai tepat ke operator seluler."))
            else:
                name = next((name for uuid, name, _ in profiles if uuid == value), value)
                activate_profile(value, name)

        app.push(chooser(app, "Pilih profil LTE backup", options, app.cfg["lte_profile"], selected))

    app.job("Membaca profil LTE", list_profiles, then=show_profiles)


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
        wifis = wifi_ifaces()
        rfkill = sh(["rfkill", "list", "wifi"], timeout=3)[1] if which("rfkill") else "rfkill tidak terpasang"
        rfkill_state = ", ".join(line.strip() for line in rfkill.splitlines()
                                 if "blocked:" in line.lower()) or (rfkill.splitlines()[0] if rfkill else "tidak ada output")
        return ["", "  Model   : %s" % model, "  Kernel  : %s %s" % (u.sysname, u.release),
                "  Arsitek.: %s" % u.machine, "  RAM     : %s" % fmt_bytes(mt),
                "  Python  : %s" % sys.version.split()[0],
                "  Wi-Fi   : %s" % (", ".join(wifis) if wifis else "tidak terdeteksi"),
                "  rfkill  : %s" % rfkill_state[:70],
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
    def lte_info(a):
        cache = request_cached_data(a, "_lte_cache", fetch_lte_data)
        if cache is None:
            return ["Membaca status modem..."]
        _, rc, out = cache
        if rc != 0:
            return ["Status modem gagal:", out] if out else ["Modem tidak ditemukan"]
        lines = []
        for key in ("state:", "access tech:", "signal quality:", "power state:", "model:", "manufacturer:"):
            for l in out.splitlines():
                if key in l:
                    lines.append(l.strip())
        return lines or ["(tidak ada info)"]

    def build(a):
        items = [Item("LTE status", lambda a: a.push(TextView("LTE", lte_info)), value=lambda a: "info")]
        return items
    return MenuScreen("LTE", build)


def gps_menu(app):
    def gps_info(a):
        if not GPS_ENABLED:
            return ["GPS dinonaktifkan oleh GPS_ENABLED di .env"]
        cache = request_cached_data(a, "_gps_menu_cache", lambda: fetch_gps_menu(a))
        if cache is None:
            lines = ["Mengambil data GPS..."]
        else:
            _, rc, output = cache
            if rc == 0:
                lines = output.splitlines()[:10] or ["(tidak ada data GPS)"]
            else:
                lines = ["GPS tidak tersedia", output] if output else ["GPS tidak tersedia"]
        recorder = getattr(a, "gps_recorder", None)
        if recorder:
            lines += ["", "Penyimpanan Redis: %s" % recorder.state]
            if recorder.last_saved:
                lines.append("Terakhir disimpan: %s" % time.strftime("%H:%M:%S", time.localtime(recorder.last_saved)))
        elif not REDIS_URL:
            lines += ["", "Penyimpanan Redis: dinonaktifkan (.env)"]
        return lines

    def build(a):
        items = [
            Item("GPS", lambda a: a.push(TextView("GPS", gps_info)), value=lambda a: "info"),
            Item("Peta terminal (lintasan lokal)", lambda a: a.push(TerminalMapScreen()),
                 value=lambda a: "aktif" if GPS_ENABLED else "GPS OFF"),
        ]
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


def settings_menu(app):
    return MenuScreen("Pengaturan", lambda a: [
        Item("Hotspot / WiFi", lambda a: a.push(wifi_menu(a))),
        Item("Tampilan & daya", lambda a: a.push(display_menu(a))),
    ])


def services_menu(app):
    return MenuScreen("Monitoring & layanan", lambda a: [
        Item("Snort", lambda a: a.push(snort_menu(a))),
        Item("Suricata", lambda a: a.push(suricata_menu(a))),
        Item("LTE", lambda a: a.push(lte_menu(a))),
        Item("GPS", lambda a: a.push(gps_menu(a))),
        Item("Web dashboard", lambda a: a.push(dashboard_menu(a))),
    ])


def main_menu(app):
    def build(a):
        n = len((a.stats.d or {}).get("clients") or [])
        return [
            Item("Hotspot", lambda a: a.push(hotspot_menu(a)), value=lambda a: {"on": "AKTIF", "off": "mati", "error": "ERROR"}.get(a.hs.state, "...")),
            Item("Klien", lambda a: a.push(clients_menu(a)), value=lambda a: "%d terhubung" % n),
            Item("Jaringan", lambda a: a.push(network_menu(a))),
            Item("Pengaturan", lambda a: a.push(settings_menu(a))),
            Item("Monitoring & layanan", lambda a: a.push(services_menu(a))),
            Item("Log", lambda a: a.push(LogScreen())),
            Item("Sistem", lambda a: a.push(system_menu(a))),
            Item("Bantuan / peta tombol", lambda a: a.push(TextView("Bantuan", HELP))),
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
SUBNETS = ["192.168.50.0/24", "10.42.0.0/24", "172.20.0.0/24", "192.168.100.0/24",
           "10.200.0.0/24", "172.31.0.0/24", "192.168.200.0/24", "10.240.0.0/24"]


def switch_upstream(app):
    ap = app.hs.ap or app.hs.ap_iface()
    devices = []
    for name in list_ifaces():
        if name == ap or name.startswith(("docker", "veth", "br-", "virbr", "tun", "tap")):
            continue
        if rd("/sys/class/net/%s/operstate" % name) not in ("up", "unknown"):
            continue
        rc, out = sh(["ip", "-4", "-o", "addr", "show", "dev", name], timeout=2)
        if rc == 0 and re.search(r"\binet\s+\d", out):
            devices.append(name)
    if not devices:
        app.toast("Tidak ada interface upstream aktif dengan alamat IPv4", "bad", 4)
        return
    options = ["auto"] + devices
    cur = app.cfg["upstream"]
    if cur == "auto":
        cur = app.hs.upstream_iface(ap) or "auto"
    if cur not in options:
        cur = "auto"
    new = options[(options.index(cur) + 1) % len(options)]
    app.cfg["upstream"] = new
    app.cfg.save()
    app.log("upstream switch: %s -> %s" % (cur, new))
    if app.hs.state == "on":
        restart_hotspot(app)
    else:
        app.toast("Upstream: %s (aktifkan hotspot untuk menerapkan)" % new, "info", 4)

def network_menu(app):
    def pick_dns(a):
        cur = tuple(a.cfg["dns"])
        opts = [(n, tuple(v)) for n, v in DNS_PRESETS]
        a.push(chooser(a, "DNS untuk klien", opts, cur, lambda v: set_cfg(a, "dns", list(v))))

    def pick_up(a):
        ap = a.hs.ap or a.hs.ap_iface()
        excluded = ("docker", "veth", "br-", "virbr", "tun", "tap")
        opts = [("auto (ikuti rute kernel/LTE)", "auto")] + [
            (n, n) for n in list_ifaces() if n != ap and not n.startswith(excluded)]
        a.push(chooser(a, "Interface upstream", opts, a.cfg["upstream"], lambda v: set_cfg(a, "upstream", v)))

    def pick_sub(a):
        a.push(chooser(a, "Subnet hotspot", [(s, s) for s in SUBNETS], a.cfg["subnet"], lambda v: set_cfg(a, "subnet", v)))

    return MenuScreen("Jaringan", lambda a: [
        Item("Interface upstream", pick_up, value=lambda a: a.cfg["upstream"]),
        Item("Bagikan internet (NAT)", lambda a: toggle(a, "nat"), value=lambda a: "ON" if a.cfg["nat"] else "OFF"),
        Item("Pindah upstream berikutnya", lambda a: switch_upstream(a), value=lambda a: a.cfg["upstream"]),
        Item("DNS klien", pick_dns, value=lambda a: a.cfg["dns"][0]),
        Item("Subnet hotspot", pick_sub, value=lambda a: a.cfg["subnet"]),
        Item("Tes ping internet", ping_test),
        Item("Scan ulang Wi-Fi", refresh_wifi),
        Item("Tambah upstream Wi-Fi (wizard)", connect_wifi),
        Item("Atur LTE sebagai backup", configure_lte_backup),
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
        opts = [("auto", "auto")] + [(n, n) for n in (["wlan0"] if a.demo else wifi_ifaces())]
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
        Item("Kunci BSSID upstream WiFi", toggle_wifi_bssid_lock,
             value=lambda a: "ON" if a.cfg["wifi_lock_bssid"] else "OFF"),
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
    def pick_idle_timeout(a):
        options = [("Tidak pernah", 0), ("30 menit tanpa klien", 1800)]
        a.push(chooser(a, "Hotspot idle", options, a.cfg["idle_hotspot_timeout"],
                       lambda value: set_cfg(a, "idle_hotspot_timeout", value, False)))

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
        Item("Matikan AP tanpa klien", pick_idle_timeout,
             value=lambda a: "OFF" if not a.cfg["idle_hotspot_timeout"] else "30 menit"),
        Item("Shutdown baterai <=15%", lambda a: toggle(a, "low_battery_shutdown", False),
             value=lambda a: "ON" if a.cfg["low_battery_shutdown"] else "OFF"),
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
        self.upstream_monitor = UpstreamMonitor(self)
        self.gps_recorder = GPSRecorder(self) if GPS_ENABLED and not self.demo else None
        self.buttons = None
        self.last_input = time.time()
        self.battery_low_since = None
        self.battery_shutdown_deadline = None
        self.battery_shutdown_started = False
        self.battery_last_notice = None
        self.idle_hotspot_since = None
        self.idle_stop_pending = False
        self.idle_stop_logged = False
        self.idle_stop_attempted = False
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
        if (to and not self.display.blanked and now - self.last_input > to
                and not self.busy and self.battery_low_since is None):
            self.blank_screen()
        if self.hs.state == "on" and not self.demo and not (self.hs.pid("hostapd") and self.hs.pid("dnsmasq")):
            self.hs.state, self.hs.error = "error", "proses hostapd/dnsmasq berhenti"
            self.log("ERR hostapd/dnsmasq berhenti tiba-tiba")
            self.toast("Hotspot berhenti tiba-tiba!", "bad", 8)
            self.wake()
        self.update_safety_automations()

    def update_safety_automations(self):
        if self.demo:
            return
        monotonic_now = time.monotonic()
        data = self.stats.d or {}
        sample_time = data.get("ts")
        sample_fresh = (isinstance(sample_time, (int, float))
                        and 0 <= time.time() - sample_time <= max(15, self.cfg["refresh"] * 4))

        battery = data.get("bat") or {}
        low_battery = (
            self.cfg["low_battery_shutdown"] and sample_fresh
            and isinstance(battery.get("pct"), (int, float))
            and battery["pct"] <= 15
            and str(battery.get("status", "")).strip().lower() == "discharging"
            and not data.get("charger", False)
        )
        if low_battery:
            if self.battery_low_since is None:
                self.battery_low_since = monotonic_now
                self.log("baterai <=15% saat discharging; shutdown dibatalkan bila charger terpasang")
                self.toast("Baterai <=15%; sambungkan charger untuk mencegah shutdown otomatis", "warn", 35)
                if getattr(getattr(self, "display", None), "blanked", False):
                    self.wake()
            elif (self.battery_shutdown_deadline is None
                  and monotonic_now - self.battery_low_since >= 30):
                self.battery_shutdown_deadline = monotonic_now + 30
                self.battery_last_notice = None
                self.log("shutdown baterai dijadwalkan dalam 30 detik")
            if self.battery_shutdown_deadline is not None:
                remaining = max(0, int(self.battery_shutdown_deadline - monotonic_now + 0.999))
                notice_bucket = remaining // 10
                if notice_bucket != self.battery_last_notice:
                    self.battery_last_notice = notice_bucket
                    self.toast("Shutdown baterai dalam %d dtk; sambungkan charger untuk membatalkan"
                               % remaining, "bad", 11)
                if remaining == 0 and not self.battery_shutdown_started and not self.busy:
                    self.battery_shutdown_started = True

                    def poweroff():
                        if os.geteuid() != 0:
                            raise RuntimeError("shutdown otomatis memerlukan hak root")
                        if self.hs.state == "on":
                            self.hs.stop()
                        command = which("poweroff")
                        args = [command] if command else ([which("shutdown"), "-h", "now"] if which("shutdown") else None)
                        if not args:
                            raise RuntimeError("perintah poweroff/shutdown tidak ditemukan")
                        rc, output = sh(args, timeout=15)
                        if rc:
                            raise RuntimeError(output or "perintah shutdown gagal")

                    self.log("shutdown perangkat dimulai karena baterai <=15%")
                    self.job("Shutdown baterai rendah", poweroff,
                             then=lambda _: setattr(self, "running", False))
        else:
            if self.battery_low_since is not None or self.battery_shutdown_deadline is not None:
                self.log("shutdown baterai dibatalkan: kondisi baterai aman/charger terpasang")
                if self.battery_shutdown_deadline is not None:
                    self.toast("Shutdown baterai dibatalkan", "good", 5)
            self.battery_low_since = None
            self.battery_shutdown_deadline = None
            self.battery_shutdown_started = False
            self.battery_last_notice = None

        idle_timeout = self.cfg["idle_hotspot_timeout"]
        clients_valid = (
            sample_fresh and data.get("clients_valid") is True
            and self.hs.state == "on"
        )
        if not idle_timeout or self.hs.state != "on":
            self.idle_hotspot_since = None
            self.idle_stop_pending = False
            self.idle_stop_logged = False
            self.idle_stop_attempted = False
        elif clients_valid:
            if data.get("clients"):
                self.idle_hotspot_since = None
                self.idle_stop_logged = False
                self.idle_stop_attempted = False
            else:
                if self.idle_hotspot_since is None:
                    self.idle_hotspot_since = monotonic_now
                    self.log("hotspot idle: mulai hitung 30 menit tanpa klien")
                elif (monotonic_now - self.idle_hotspot_since >= idle_timeout
                      and not self.idle_stop_pending and not self.idle_stop_attempted):
                    if not self.idle_stop_logged:
                        self.log("hotspot idle 30 menit; mematikan AP")
                        self.idle_stop_logged = True
                    if not self.busy:
                        self.idle_stop_pending = True
                        self.idle_stop_attempted = True

                        def stop_idle_hotspot():
                            try:
                                return self.hs.stop()
                            finally:
                                self.idle_stop_pending = False

                        self.job("Mematikan hotspot idle", stop_idle_hotspot,
                                 then=lambda _: self.toast("Hotspot dimatikan setelah 30 menit tanpa klien", "info", 6))
        else:
            # Missing/stale client samples cannot be treated as an empty AP.
            self.idle_hotspot_since = None
            self.idle_stop_logged = False

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
        self.upstream_monitor.start()
        if self.gps_recorder:
            self.gps_recorder.start()
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
            self.tick()
            if ch != -1 and self.display.blanked:
                self.last_input = time.time()
                self.wake()
                continue
            if ch in keymap:
                self.dispatch(keymap[ch])
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
        self.upstream_monitor.stop_event.set()
        if self.upstream_monitor.is_alive():
            self.upstream_monitor.join(1.0)
        if self.gps_recorder:
            self.gps_recorder.stop_event.set()
            if self.gps_recorder.is_alive():
                self.gps_recorder.join(4.0)
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
