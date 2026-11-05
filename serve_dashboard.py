#!/usr/bin/env python3
"""Local, read-only POCO-TUI web dashboard with a small JSON status API."""

import argparse
import base64
import hmac
import http.server
import ipaddress
import json
import os
import platform
import signal
import socket
import socketserver
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse

from pocotui import (
    REDIS_URL,
    cpu_policy_status,
    redis_command,
)


ROOT = os.path.dirname(os.path.abspath(__file__))
HTML_PATH = os.path.join(ROOT, "dashboard.html")
STATUS_CACHE = {"timestamp": 0.0, "data": None}
STATUS_LOCK = threading.Lock()


def command(args, timeout=3):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
        return result.returncode, (result.stdout or "") + (result.stderr or "")
    except (OSError, subprocess.TimeoutExpired) as error:
        return 127, str(error)


def read_text(path, default=""):
    try:
        with open(path, encoding="utf-8", errors="replace") as stream:
            return stream.read().strip()
    except OSError:
        return default


def read_json(path, default):
    try:
        with open(path, encoding="utf-8") as stream:
            return json.load(stream)
    except (OSError, ValueError):
        return default


def process_running(name):
    return command(["pgrep", "-x", name], timeout=1)[0] == 0


def battery_status():
    result = {"percent": None, "status": "unknown", "charging": False}
    for path in sorted(os.listdir("/sys/class/power_supply")) if os.path.isdir("/sys/class/power_supply") else []:
        root = os.path.join("/sys/class/power_supply", path)
        if read_text(os.path.join(root, "type")) == "Battery":
            try:
                result["percent"] = int(read_text(os.path.join(root, "capacity")))
            except ValueError:
                pass
            result["status"] = read_text(os.path.join(root, "status"), "unknown")
        elif read_text(os.path.join(root, "type")) != "Battery":
            result["charging"] = result["charging"] or read_text(os.path.join(root, "online")) == "1"
    return result


def system_status():
    result = {"cpu_percent": None, "memory_percent": None, "memory_used": None,
              "memory_total": None, "temperature_c": None, "uptime_seconds": None,
              "load": [], "battery": battery_status(), "disk_percent": None,
              "platform": "%s %s · %s" % (platform.system(), platform.release(), platform.machine())}
    try:
        fields = read_text("/proc/stat").splitlines()[0].split()[1:]
        current = [int(value) for value in fields]
        previous = STATUS_CACHE.get("cpu")
        STATUS_CACHE["cpu"] = current
        if previous:
            total = sum(current) - sum(previous)
            idle = current[3] - previous[3]
            if total > 0:
                result["cpu_percent"] = round(100 * (1 - idle / total), 1)
    except (IndexError, ValueError):
        pass
    try:
        memory = {}
        for line in read_text("/proc/meminfo").splitlines():
            key, value = line.split(":", 1)
            memory[key] = int(value.split()[0]) * 1024
        total, available = memory.get("MemTotal", 0), memory.get("MemAvailable", 0)
        if total:
            result["memory_total"] = total
            result["memory_used"] = total - available
            result["memory_percent"] = round(100 * (total - available) / total, 1)
    except (ValueError, IndexError):
        pass
    try:
        result["load"] = [float(value) for value in read_text("/proc/loadavg").split()[:3]]
        result["uptime_seconds"] = int(float(read_text("/proc/uptime").split()[0]))
    except (ValueError, IndexError):
        pass
    try:
        import shutil
        usage = shutil.disk_usage("/")
        result["disk_percent"] = round(100 * usage.used / usage.total, 1)
    except OSError:
        pass
    temperatures = []
    thermal = "/sys/class/thermal"
    for zone in sorted(os.listdir(thermal)) if os.path.isdir(thermal) else []:
        if not zone.startswith("thermal_zone"):
            continue
        try:
            value = int(read_text(os.path.join(thermal, zone, "temp")))
            degrees = value / 1000 if abs(value) > 1000 else float(value)
            if -20 < degrees < 150:
                temperatures.append(degrees)
        except ValueError:
            pass
    if temperatures:
        result["temperature_c"] = round(max(temperatures), 1)
    return result


def network_status():
    rc, output = command(["ip", "-j", "-4", "addr", "show"])
    interfaces = []
    if not rc:
        try:
            for item in json.loads(output):
                addresses = [addr["local"] for addr in item.get("addr_info", [])
                             if addr.get("family") == "inet"]
                interfaces.append({"name": item["ifname"], "state": item.get("operstate", "UNKNOWN"),
                                   "addresses": addresses})
        except (ValueError, KeyError, TypeError):
            pass
    rc, output = command(["ip", "-j", "-4", "route", "show", "default"])
    routes = []
    if not rc:
        try:
            routes = [{"interface": route.get("dev"), "gateway": route.get("gateway"),
                       "metric": route.get("metric", 0)} for route in json.loads(output)]
        except (ValueError, TypeError):
            pass
    counters = {}
    for line in read_text("/proc/net/dev").splitlines()[2:]:
        try:
            name, values = line.split(":", 1)
            fields = values.split()
            counters[name.strip()] = {"rx_bytes": int(fields[0]), "tx_bytes": int(fields[8])}
        except (ValueError, IndexError):
            continue
    active = routes[0]["interface"] if routes else None
    wifi = next((item["name"] for item in interfaces if item["name"].startswith(("wlan", "wlp", "wlx"))), None)
    return {"interfaces": interfaces, "default_routes": routes, "active_interface": active,
            "wifi_interface": wifi, "counters": counters}


def hotspot_status(config_path):
    config = read_json(config_path, {})
    ap_name = config.get("ap_name", "ap0")
    running = process_running("hostapd")
    leases = []
    for line in read_text("/run/pocotui/dnsmasq.leases").splitlines():
        fields = line.split()
        if len(fields) >= 5:
            leases.append({"expiry": fields[0], "mac": fields[1], "ip": fields[2],
                           "hostname": "" if fields[3] == "*" else fields[3], "client_id": fields[4]})
    rc, stations_text = command(["hostapd_cli", "-p", "/run/pocotui/hostapd",
                                 "-i", ap_name, "all_sta"])
    stations = []
    current = None
    if rc == 0:
        for line in stations_text.splitlines():
            line = line.strip()
            if len(line) == 17 and line.count(":") == 5:
                current = {"mac": line.lower()}
                stations.append(current)
            elif current and "=" in line:
                key, value = line.split("=", 1)
                if key in ("signal", "connected_time", "rx_bytes", "tx_bytes"):
                    try:
                        current[key] = int(value)
                    except ValueError:
                        pass
    by_mac = {client["mac"].lower(): client for client in stations}
    for lease in leases:
        station = by_mac.get(lease["mac"].lower())
        if station:
            station.update({"ip": lease["ip"], "hostname": lease["hostname"]})
        else:
            lease["signal"] = None
            stations.append(lease)
    return {"active": running, "ssid": config.get("ssid", "PocoF1-Hotspot"),
            "gateway": "192.168.50.1", "ap_interface": ap_name, "clients": stations,
            "client_count": len(stations), "max_clients": config.get("max_clients", 8),
            "nat": config.get("nat", True)}


def firewall_status():
    rc, output = command(["nft", "-j", "list", "ruleset"], timeout=4)
    if rc:
        return {"available": False, "active": False, "reason": output.strip()[:200],
                "tables": 0, "chains": 0, "rules": 0, "base_chains": []}
    try:
        entries = json.loads(output).get("nftables", [])
    except (ValueError, AttributeError):
        return {"available": False, "active": False, "reason": "Respons nftables tidak valid",
                "tables": 0, "chains": 0, "rules": 0, "base_chains": []}
    tables, chains, rules, base_chains = 0, 0, 0, []
    for entry in entries:
        if "table" in entry:
            tables += 1
        elif "chain" in entry:
            chains += 1
            chain = entry["chain"]
            if chain.get("hook"):
                base_chains.append({
                    "table": chain.get("table", ""),
                    "name": chain.get("name", ""),
                    "hook": chain["hook"],
                    "policy": chain.get("policy"),
                })
        elif "rule" in entry:
            rules += 1
    return {"available": True, "active": bool(chains), "reason": "",
            "tables": tables, "chains": chains, "rules": rules,
            "base_chains": base_chains}


def lte_status():
    rc, output = command(["mmcli", "-m", "0"], timeout=4)
    data = {"available": rc == 0, "raw": output[:12000]}
    if rc:
        return data
    mappings = {
        "state": "state", "access tech": "technology", "signal quality": "signal",
        "power state": "power", "manufacturer": "manufacturer", "model": "model",
        "operator name": "operator", "registration": "registration",
    }
    for line in output.splitlines():
        lowered = line.strip().lower()
        for label, key in mappings.items():
            if lowered.startswith(label + ":"):
                data[key] = line.split(":", 1)[-1].strip()
    return data


def redis_gps():
    if not REDIS_URL:
        return {"available": False, "reason": "Redis dinonaktifkan"}
    parsed = urllib.parse.urlsplit(REDIS_URL)
    try:
        port = parsed.port or 6379
        database = int(parsed.path.strip("/") or "0")
        password = urllib.parse.unquote(parsed.password) if parsed.password else None
        username = urllib.parse.unquote(parsed.username) if parsed.username else None
        context = ssl.create_default_context() if parsed.scheme == "rediss" else None
        with socket.create_connection((parsed.hostname, port), timeout=2) as raw:
            sock = context.wrap_socket(raw, server_hostname=parsed.hostname) if context else raw
            sock.settimeout(2)
            if password:
                if username:
                    redis_command(sock, "AUTH", username, password)
                else:
                    redis_command(sock, "AUTH", password)
            if database:
                redis_command(sock, "SELECT", database)
            latest = redis_command(sock, "GET", "pocotui:location:latest")
            entries = redis_command(sock, "XREVRANGE", "pocotui:location", "+", "-", "COUNT", 500)
        points = []
        for entry in entries or []:
            if not isinstance(entry, list) or len(entry) < 2:
                continue
            values = entry[1]
            fields = {values[index].decode(): values[index + 1].decode()
                      for index in range(0, len(values) - 1, 2)}
            try:
                record = json.loads(fields["record"])
                position = record.get("parsed_location")
                if position and position.get("latitude") is not None and position.get("longitude") is not None:
                    points.append({"latitude": position["latitude"], "longitude": position["longitude"],
                                   "timestamp": record.get("timestamp")})
            except (KeyError, ValueError, TypeError, UnicodeDecodeError):
                continue
        latest_record = json.loads(latest) if latest else None
        return {"available": True, "latest": latest_record, "track": list(reversed(points))}
    except Exception as error:
        return {"available": False, "reason": str(error)[:200]}


def collect_status(config_path):
    config = read_json(config_path, {})
    hotspot = hotspot_status(config_path)
    network = network_status()
    system = system_status()
    battery = system["battery"]
    gps = redis_gps()
    firewall = firewall_status()
    auto_eco = config.get("auto_eco_battery", True)
    eco_active = (auto_eco and battery.get("percent") is not None
                  and battery["percent"] <= 20
                  and battery.get("status", "").lower() == "discharging"
                  and not battery.get("charging"))
    return {
        "timestamp": time.time(),
        "hostname": socket.gethostname(),
        "hotspot": hotspot,
        "system": system,
        "network": network,
        "lte": lte_status(),
        "gps": gps,
        "firewall": firewall,
        "power": {
            "profile": "eco" if eco_active else config.get("cpu_profile", "balanced"),
            "configured_profile": config.get("cpu_profile", "balanced"),
            "automatic_eco": auto_eco,
            "policies": cpu_policy_status(),
        },
        "services": {
            "hostapd": hotspot["active"],
            "dnsmasq": process_running("dnsmasq"),
            "ip_forward": read_text("/proc/sys/net/ipv4/ip_forward") == "1",
            "redis": gps["available"],
            "nftables": firewall["active"],
            "snort": process_running("snort"),
            "suricata": process_running("suricata"),
        },
    }


class DashboardServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def handler_for(config_path, username, password):
    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "POCO-TUI-Dashboard"

        def log_message(self, fmt, *args):
            sys.stderr.write("%s - %s\n" % (self.log_date_time_string(), fmt % args))

        def authorized(self):
            if not password:
                return True
            header = self.headers.get("Authorization", "")
            try:
                scheme, encoded = header.split(" ", 1)
                supplied = base64.b64decode(encoded, validate=True).decode("utf-8")
                supplied_user, supplied_password = supplied.split(":", 1)
            except (ValueError, UnicodeDecodeError):
                supplied_user, supplied_password, scheme = "", "", ""
            valid = (scheme.lower() == "basic"
                     and hmac.compare_digest(supplied_user, username)
                     and hmac.compare_digest(supplied_password, password))
            if not valid:
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="POCO-TUI", charset="UTF-8"')
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
            return valid

        def do_GET(self):
            path = urllib.parse.urlsplit(self.path).path
            if path not in ("/", "/index.html", "/api/status", "/health"):
                self.send_error(404)
                return
            if not self.authorized():
                return
            if path == "/health":
                self.send_json({"ok": True})
            elif path == "/api/status":
                now = time.monotonic()
                with STATUS_LOCK:
                    if STATUS_CACHE["data"] is None or now - STATUS_CACHE["timestamp"] >= 3:
                        try:
                            STATUS_CACHE["data"] = collect_status(config_path)
                        except Exception as error:
                            sys.stderr.write("Dashboard status collection failed: %s\n" % error)
                            self.send_error(503, "Status perangkat sementara tidak tersedia")
                            return
                        STATUS_CACHE["timestamp"] = now
                    payload = STATUS_CACHE["data"]
                self.send_json(payload)
            else:
                try:
                    with open(HTML_PATH, "rb") as html:
                        body = html.read()
                except OSError:
                    self.send_error(500, "Dashboard UI tidak ditemukan")
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

        def send_json(self, payload):
            body = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

    return Handler


def main():
    parser = argparse.ArgumentParser(description="POCO-TUI web dashboard")
    parser.add_argument("--host", default=os.environ.get("DASHBOARD_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("DASHBOARD_PORT", "7890")))
    parser.add_argument("--config", default=os.environ.get("POCOTUI_CONFIG", "/etc/pocotui/config.json"))
    parser.add_argument("--pid-file", default=os.path.join(
        tempfile.gettempdir(), "pocotui-web-dashboard-%d.pid" % os.getuid()))
    args = parser.parse_args()
    username = os.environ.get("DASHBOARD_USER", "admin")
    password = os.environ.get("DASHBOARD_PASSWORD", "")
    cert_file = os.environ.get("DASHBOARD_TLS_CERT", "")
    key_file = os.environ.get("DASHBOARD_TLS_KEY", "")
    try:
        address = ipaddress.ip_address(args.host)
        remote = not address.is_loopback
    except ValueError:
        remote = args.host.lower() not in ("localhost",)
    if remote and not password:
        parser.error("DASHBOARD_PASSWORD wajib diatur sebelum bind ke interface non-loopback")
    if remote and (not cert_file or not key_file):
        parser.error("DASHBOARD_TLS_CERT dan DASHBOARD_TLS_KEY wajib untuk akses non-loopback")
    if not 1 <= args.port <= 65535:
        parser.error("port harus 1-65535")
    os.makedirs(os.path.dirname(args.pid_file), exist_ok=True)
    server = DashboardServer((args.host, args.port), handler_for(args.config, username, password))
    if cert_file or key_file:
        if not cert_file or not key_file:
            parser.error("DASHBOARD_TLS_CERT dan DASHBOARD_TLS_KEY harus diatur bersama")
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(certfile=cert_file, keyfile=key_file)
        server.socket = tls.wrap_socket(server.socket, server_side=True)
    with open(args.pid_file, "w", encoding="ascii") as pid:
        pid.write(str(os.getpid()))

    def stop_server(_signum, _frame):
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop_server)
    signal.signal(signal.SIGINT, stop_server)
    print("POCO-TUI dashboard listening on %s:%d" % (args.host, args.port), flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        try:
            if read_pid(args.pid_file) == os.getpid():
                os.remove(args.pid_file)
        except OSError:
            pass


def read_pid(path):
    try:
        return int(read_text(path))
    except ValueError:
        return None


if __name__ == "__main__":
    main()
