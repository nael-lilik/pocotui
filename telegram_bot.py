#!/usr/bin/env python3
"""POCO-TUI Telegram bot for status and read-only device checks."""
import argparse
import json
import os
import shlex
import shutil
import subprocess
import threading
import time
import urllib.request

PROJECT_ENV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
ENV_FILE = os.environ.get("POCOTUI_ENV_FILE") or (
    PROJECT_ENV_FILE if os.path.isfile(PROJECT_ENV_FILE) else "/etc/pocotui/.env"
)


def read_env_file():
    values = {}
    try:
        with open(ENV_FILE) as f:
            for number, line in enumerate(f, 1):
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[7:].lstrip()
                if "=" not in line:
                    raise ValueError("%s:%d: expected KEY=VALUE" % (ENV_FILE, number))
                key, raw_value = line.split("=", 1)
                key = key.strip()
                if not key or not (key[0].isalpha() or key[0] == "_") or not all(
                        c.isalnum() or c == "_" for c in key):
                    raise ValueError("%s:%d: invalid environment variable name" % (ENV_FILE, number))
                try:
                    parts = shlex.split(raw_value, comments=True)
                except ValueError as e:
                    raise ValueError("%s:%d: %s" % (ENV_FILE, number, e)) from e
                if len(parts) > 1:
                    raise ValueError("%s:%d: quote values containing spaces" % (ENV_FILE, number))
                values[key] = parts[0] if parts else ""
    except FileNotFoundError:
        return values
    return values


def load_env():
    for key, value in read_env_file().items():
        os.environ.setdefault(key, value)


load_env()

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
ALLOWED_USER_ID = os.environ.get("TELEGRAM_ALLOWED_USER_ID", "")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "")
REMOTE_URL = os.environ.get("REMOTE_URL", "")
MODEL = os.environ.get("LLM_MODEL", "")
OFFSET = 0

NEON = "⚡"


def tg(method, data=None):
    url = f"https://api.telegram.org/bot{TOKEN}/{method}"
    req = urllib.request.Request(url, data=json.dumps(data).encode(),
                                headers={"Content-Type": "application/json"}) if data else urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except Exception as e:
        return {"ok": False, "error": str(e)}


def run(cmd):
    try:
        r = subprocess.run(cmd, shell=isinstance(cmd, str), capture_output=True, text=True, timeout=10)
        return (r.stdout or "") + (r.stderr or "")
    except Exception as e:
        return str(e)


def system_status():
    bat = run("cat /sys/class/power_supply/*/capacity 2>/dev/null | head -1").strip()
    temp = run("cat /sys/class/thermal/thermal_zone0/temp 2>/dev/null").strip()
    temp = f"{int(temp)/1000:.1f}°C" if temp.isdigit() else "?"
    load = run("awk '{print $1}' /proc/loadavg").strip()
    mem = run("free -h | awk '/^Mem:/{print $3\"/\"$2}'").strip()
    ip = run("ip -4 addr show 2>/dev/null | grep -oP 'inet \\K[\\d.]+' | head -1").strip()
    inet = "🟢 ONLINE" if "1 received" in run("ping -c1 -W2 8.8.8.8 2>/dev/null") else "🔴 OFFLINE"
    return (f"{NEON} POCO-TUI STATUS {NEON}\n"
            f"🔋 Battery : {bat}%\n🌡 Temp    : {temp}\n"
            f"⚙️ Load    : {load}\n🧠 Mem     : {mem}\n"
            f"🌐 IP      : {ip}\n📡 Net     : {inet}")


def ask_llm(prompt):
    if OLLAMA_URL and OLLAMA_MODEL:
        try:
            data = {"model": OLLAMA_MODEL, "prompt": prompt, "stream": False}
            req = urllib.request.Request(OLLAMA_URL, data=json.dumps(data).encode(),
                                        headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read()).get("response", "")
        except Exception:
            pass
    if not REMOTE_URL or not MODEL:
        return "LLM error: configure REMOTE_URL and LLM_MODEL in .env"
    if not os.environ.get("LLM_API_KEY"):
        return "LLM error: LLM_API_KEY is not configured in .env"
    try:
        data = {"model": MODEL, "messages": [{"role": "user", "content": prompt}], "stream": False}
        req = urllib.request.Request(REMOTE_URL, data=json.dumps(data).encode(),
                                    headers={"Content-Type": "application/json",
                                             "Authorization": f"Bearer {os.environ.get('LLM_API_KEY', '')}",
                                             "User-Agent": "curl/8.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())["choices"][0]["message"]["content"]
    except Exception as e:
        return f"LLM error: {e}"


def docker_cmd(command):
    try:
        args = tuple(shlex.split(command))
    except ValueError:
        return "Perintah container tidak valid."
    allowed = {
        ("ps",): ["ps"],
        ("ps", "-a"): ["ps", "-a"],
        ("images",): ["images"],
        ("stats", "--no-stream"): ["stats", "--no-stream"],
    }
    if args not in allowed:
        return "Hanya perintah baca-saja yang diizinkan: ps [-a], images, stats --no-stream."
    for runtime in ("docker", "podman"):
        executable = shutil.which(runtime)
        if not executable:
            continue
        try:
            result = subprocess.run(
                [executable, *allowed[args]], capture_output=True, text=True, timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            return "Gagal menjalankan %s: %s" % (runtime, error)
        if result.returncode == 0:
            return (result.stdout or "").strip() or "(tidak ada output)"
    return "Docker/Podman tidak tersedia atau perintah gagal."


def is_authorized_message(message):
    chat = message.get("chat", {})
    sender = message.get("from", {})
    chat_id = str(chat.get("id", ""))
    sender_id = str(sender.get("id", ""))
    if chat_id != CHAT_ID:
        return False
    if ALLOWED_USER_ID:
        return sender_id == ALLOWED_USER_ID
    return chat.get("type") == "private" and sender_id == CHAT_ID


def hotspot_control_disabled():
    return (
        "Kontrol hotspot lewat Telegram dinonaktifkan agar tidak melewati konfigurasi "
        "nftables, failover, dan fail-safe POCO-TUI. Gunakan menu Hotspot di POCO-TUI."
    )


def handle(text):
    t = text.lower().strip()
    if t in ("/status", "status"):
        return system_status()
    if t in ("/hotspot on", "hotspot on", "/hotspot off", "hotspot off",
             "/hotspot creds", "hotspot creds") or t.startswith(
                 ("/hotspot ssid ", "/hotspot password ")
             ):
        return hotspot_control_disabled()
    if t.startswith("/ask ") or t.startswith("ask "):
        q = text.split(" ", 1)[1]
        return ask_llm(q)
    if t.startswith("/docker ") or t.startswith("/podman "):
        cmd = text.split(" ", 1)[1]
        return docker_cmd(cmd)[:4000]
    if t in ("/containers", "containers"):
        return docker_cmd("ps -a")[:4000]
    if t in ("/sms", "sms"):
        out = run("mmcli -m 0 --sms-list 2>/dev/null; mmcli -m 0 --messaging-list-sms 2>/dev/null")
        return out.strip()[:4000] or "(no sms)"
    if t in ("/lte", "lte"):
        out = run("mmcli -m 0 2>/dev/null")
        info = [s.strip() for s in out.splitlines() if any(k in s for k in ("state:", "access tech:", "signal quality:", "operator name:", "lock:"))]
        return "\n".join(info) or "(no modem)"
    if t in ("/wifiscan", "wifiscan", "/wifi scan", "wifi scan"):
        return run("nmcli dev wifi list 2>&1 || true").strip()[:4000] or "(no output)"
    if t in ("/btscan", "btscan", "/bt scan", "bt scan"):
        return run("bluetoothctl scan on 2>/dev/null; sleep 8; bluetoothctl devices 2>&1; bluetoothctl scan off 2>/dev/null").strip()[:4000] or "(no output)"
    if t in ("/blescan", "blescan", "/ble scan", "ble scan"):
        return run("bluetoothctl scan on 2>/dev/null; sleep 8; bluetoothctl devices 2>&1 | grep -i 'Device' ; bluetoothctl scan off 2>/dev/null").strip()[:4000] or "(no output)"
    if t in ("/hotspot clients", "hotspot clients", "/hotspotclients"):
        return run("ip neigh show dev ap0 2>/dev/null; echo '---'; cat /proc/net/arp 2>/dev/null | grep ap0").strip()[:4000] or "(no clients)"
    if t in ("/help", "help"):
        return ("⚡ NURA COMMANDS ⚡\n"
                "/status\n"
                "/hotspot clients\n"
                "/wifiscan\n/btscan\n/blescan\n"
                "/containers\n/docker ps [-a] | images | stats --no-stream\n"
                "/sms\n/lte\n/ask <q>\n/help")
    ctx = system_status()
    return ask_llm(f"Context:\n{ctx}\n\nUser: {text}")


def self_check():
    last_inet = "OK"
    while True:
        try:
            inet = "OK" if "1 received" in run("ping -c1 -W3 8.8.8.8 2>/dev/null") else "FAIL"
            if inet == "FAIL" and last_inet == "OK":
                tg("sendMessage", {"chat_id": CHAT_ID, "text": "⚠️ Internet DOWN — bot offline mode"})
            elif inet == "OK" and last_inet == "FAIL":
                tg("sendMessage", {"chat_id": CHAT_ID, "text": "✅ Internet restored"})
            bat = run("cat /sys/class/power_supply/*/capacity 2>/dev/null | head -1").strip()
            if bat.isdigit() and int(bat) <= 15:
                tg("sendMessage", {"chat_id": CHAT_ID, "text": f"🔋 LOW BATTERY: {bat}%"})
            last_inet = inet
        except Exception:
            pass
        time.sleep(60)


def main():
    global OFFSET
    parser = argparse.ArgumentParser(description="POCO-TUI Telegram status bot")
    parser.parse_args()
    missing = [key for key, value in (("TELEGRAM_BOT_TOKEN", TOKEN), ("TELEGRAM_CHAT_ID", CHAT_ID)) if not value]
    if missing:
        raise SystemExit("Missing required .env setting(s): %s" % ", ".join(missing))
    tg("sendMessage", {"chat_id": CHAT_ID, "text": f"{NEON} POCO-TUI bot online {NEON}"})
    threading.Thread(target=self_check, daemon=True).start()
    while True:
        res = tg("getUpdates", {"offset": OFFSET, "timeout": 25})
        if res.get("ok"):
            for u in res.get("result", []):
                OFFSET = u["update_id"] + 1
                msg = u.get("message", {})
                if not is_authorized_message(msg):
                    continue
                text = msg.get("text", "")
                if not text:
                    continue
                reply = handle(text)
                tg("sendMessage", {"chat_id": CHAT_ID, "text": reply[:4000]})
        time.sleep(1)


if __name__ == "__main__":
    main()
