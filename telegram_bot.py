#!/usr/bin/env python3
"""Nura Telegram bot — cyberpunk neon edition."""
import json, urllib.request, time, os, threading, sys, subprocess

TOKEN = "6272192596:AAFdeqNso0TZ-_uK1bWfVM4-JuGAgg7kXbo"
HOTSPOT_CONF = "/root/pocotui/hotspot.conf"
CHAT_ID = "141214455"
OLLAMA_URL = "http://localhost:11434/api/generate"
REMOTE_URL = "https://9r.nael.my.id/v1/chat/completions"
MODEL = "smart"
API_KEY = "sk-40f412ec220cd00c-7u1eh5-8e3fb54a"
OFFSET = 0

NEON = "⚡"


def hotspot_creds():
    ssid, pw = "NuraHotspot", "nura1234"
    try:
        with open(HOTSPOT_CONF) as f:
            for line in f:
                if "=" in line:
                    k, v = line.strip().split("=", 1)
                    if k == "ssid": ssid = v
                    elif k == "password": pw = v
    except FileNotFoundError:
        pass
    return ssid, pw


def set_hotspot_cred(key, val):
    ssid, pw = hotspot_creds()
    if key == "ssid": ssid = val
    else: pw = val
    with open(HOTSPOT_CONF, "w") as f:
        f.write(f"ssid={ssid}\npassword={pw}\n")


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
    return (f"{NEON} NURA STATUS {NEON}\n"
            f"🔋 Battery : {bat}%\n🌡 Temp    : {temp}\n"
            f"⚙️ Load    : {load}\n🧠 Mem     : {mem}\n"
            f"🌐 IP      : {ip}\n📡 Net     : {inet}")


def ask_llm(prompt):
    try:
        data = {"model": "llama3.2", "prompt": prompt, "stream": False}
        req = urllib.request.Request(OLLAMA_URL, data=json.dumps(data).encode(),
                                    headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read()).get("response", "")
    except Exception:
        pass
    try:
        data = {"model": MODEL, "messages": [{"role": "user", "content": prompt}], "stream": False}
        req = urllib.request.Request(REMOTE_URL, data=json.dumps(data).encode(),
                                    headers={"Content-Type": "application/json",
                                             "Authorization": f"Bearer {API_KEY}",
                                             "User-Agent": "curl/8.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())["choices"][0]["message"]["content"]
    except Exception as e:
        return f"LLM error: {e}"


def hotspot(on):
    active = run("nmcli connection show --active 2>/dev/null").strip()
    if on:
        if "Hotspot" in active:
            return "🔥 Hotspot already ON"
        src = run("ip route get 1.1.1.1 2>/dev/null | head -1").strip()
        iface = src.split("dev", 1)[1].split()[0] if "dev" in src else "?"
        saved = run("nmcli -t -f NAME,DEVICE connection show --active 2>/dev/null").strip()
        wifi_conn = ""
        for line in saved.splitlines():
            parts = line.split(":")
            if len(parts) >= 2 and parts[1].startswith("wlan"):
                wifi_conn = parts[0]; break
        if wifi_conn:
            run(f'printf "%s" "{wifi_conn}" > /tmp/nura_wifi_saved 2>/dev/null || true')
        run("iw dev wlan0 interface add ap0 type __ap 2>/dev/null || true")
        run("ip link set ap0 up 2>/dev/null || true")
        ssid, pw = hotspot_creds()
        run(f'nmcli dev wifi hotspot ifname ap0 ssid "{ssid}" password "{pw}" 2>&1 || true')
        run("iptables -t nat -A POSTROUTING -o wlan0 -j MASQUERADE 2>/dev/null || true")
        run("iptables -A FORWARD -i ap0 -o wlan0 -j ACCEPT 2>/dev/null || true")
        run("iptables -A FORWARD -i wlan0 -o ap0 -m state --state RELATED,ESTABLISHED -j ACCEPT 2>/dev/null || true")
        run("sysctl -w net.ipv4.ip_forward=1 2>/dev/null || true")
        return f"🔥 Hotspot ON ({iface})\nSSID: {ssid}\nPW: {pw}"
    else:
        if "Hotspot" not in active:
            return "❄️ Hotspot already OFF"
        run("nmcli connection down Hotspot 2>&1 || true")
        run("iw dev ap0 del 2>/dev/null || true")
        saved = run("cat /tmp/nura_wifi_saved 2>/dev/null").strip()
        if saved:
            run(f'nmcli connection up "{saved}" 2>&1 || true')
            run("rm -f /tmp/nura_wifi_saved")
        return "❄️ Hotspot OFF — WiFi restored"


def docker_cmd(cmd):
    out = run(f"docker {cmd} 2>&1 || podman {cmd} 2>&1")
    return out.strip() or "(no output)"


def handle(text):
    t = text.lower().strip()
    if t in ("/status", "status"):
        return system_status()
    if t in ("/hotspot on", "hotspot on"):
        return hotspot(True)
    if t in ("/hotspot off", "hotspot off"):
        return hotspot(False)
    if t.startswith("/hotspot ssid "):
        val = text.split(" ", 2)[2]
        set_hotspot_cred("ssid", val)
        return f"✅ SSID → {val}"
    if t.startswith("/hotspot password "):
        val = text.split(" ", 2)[2]
        set_hotspot_cred("password", val)
        return f"✅ Password → {val}"
    if t in ("/hotspot creds", "hotspot creds"):
        ssid, pw = hotspot_creds()
        return f"📶 SSID: {ssid}\n🔑 PW: {pw}"
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
                "/hotspot on | off | creds | clients\n"
                "/hotspot ssid <name>\n"
                "/hotspot password <pw>\n"
                "/wifiscan\n/btscan\n/blescan\n"
                "/docker <cmd>\n/containers\n/sms\n/lte\n/ask <q>\n/help")
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
    tg("sendMessage", {"chat_id": CHAT_ID, "text": f"{NEON} Nura bot online {NEON}"})
    threading.Thread(target=self_check, daemon=True).start()
    while True:
        res = tg("getUpdates", {"offset": OFFSET, "timeout": 25})
        if res.get("ok"):
            for u in res.get("result", []):
                OFFSET = u["update_id"] + 1
                msg = u.get("message", {})
                chat = str(msg.get("chat", {}).get("id", ""))
                if chat != CHAT_ID:
                    continue
                text = msg.get("text", "")
                if not text:
                    continue
                reply = handle(text)
                tg("sendMessage", {"chat_id": CHAT_ID, "text": reply[:4000]})
        time.sleep(1)


if __name__ == "__main__":
    main()
