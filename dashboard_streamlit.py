import streamlit as st
import subprocess, time

def sh(cmd, timeout=5):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return (r.stdout + r.stderr).strip()
    except Exception as e:
        return str(e)

st.set_page_config(page_title="POCO-TUI Dashboard", layout="wide", page_icon="📡")

# ---- Custom CSS ----
st.markdown("""
<style>
  .stApp { background: linear-gradient(135deg, #0a0f14 0%, #0d131a 100%); }
  .block-container { padding-top: 1rem; max-width: 1400px; }
  h1, h2, h3 { color: #4fd1c5 !important; }
  .metric-card {
    background: #111820; border: 1px solid #1f2a36; border-radius: 10px;
    padding: 16px 18px; margin: 6px 0;
  }
  .metric-card .label { color: #6b7a8a; font-size: 12px; letter-spacing: 1px; text-transform: uppercase; margin-bottom: 4px; }
  .metric-card .value { color: #cdd9e5; font-size: 22px; font-weight: 700; }
  .metric-card .value.good { color: #5fd38d; }
  .metric-card .value.warn { color: #f5c542; }
  .metric-card .value.bad  { color: #f36b6b; }
  .stTabs [data-baseweb="tab"] { color: #6b7a8a; font-size: 15px; }
  .stTabs [aria-selected="true"] { color: #4fd1c5 !important; border-bottom-color: #4fd1c5 !important; }
  pre { background: #080c10 !important; border: 1px solid #1f2a36; border-radius: 6px; font-size: 13px; line-height: 1.5; }
  .stButton>button { background: #111820; color: #4fd1c5; border: 1px solid #4fd1c5; border-radius: 6px; }
  .stButton>button:hover { background: #4fd1c5; color: #0a0f14; }
  .readable { font-size: 15px; line-height: 1.6; color: #cdd9e5; }
  .section-title { font-size: 18px; color: #4fd1c5; font-weight: 600; margin: 12px 0 6px; }
</style>
""", unsafe_allow_html=True)

st.title("📡 POCO-TUI Dashboard")
st.caption("Hotspot Gateway · POCO F1 · Nura Edge")

def card(label, value, cls=""):
    st.markdown(f'<div class="metric-card"><div class="label">{label}</div><div class="value {cls}">{value}</div></div>', unsafe_allow_html=True)

tabs = st.tabs(["🏠 Ringkasan", "👥 Klien", "🌐 Jaringan", "📶 LTE", "📍 GPS", "🛡️ Snort", "🔍 Suricata", "💻 Sistem"])

# ---- Ringkasan ----
with tabs[0]:
    c1, c2, c3 = st.columns(3)
    with c1:
        st.subheader("Hotspot")
        hs = "AKTIF" if sh("pgrep -f hostapd") else "MATI"
        card("Status", hs, "good" if hs=="AKTIF" else "bad")
        card("SSID", "PocoF1-Hotspot")
        card("Gateway", "192.168.50.1/24")
        n = sh("wc -l < /run/pocotui/dnsmasq.leases 2>/dev/null || echo 0")
        card("Klien", n.strip())
    with c2:
        st.subheader("Komponen")
        card("hostapd", "aktif" if sh("pgrep -f hostapd") else "mati", "good" if sh("pgrep -f hostapd") else "bad")
        card("dnsmasq", "aktif" if sh("pgrep -f dnsmasq") else "mati", "good" if sh("pgrep -f dnsmasq") else "bad")
        card("ip_fwd", sh("cat /proc/sys/net/ipv4/ip_forward").strip())
        card("NAT", "aktif")
    with c3:
        st.subheader("Gauge")
        cpu = sh("top -bn1 | grep 'Cpu(s)' | awk '{print $2}' | cut -d'%' -f1")
        try: cpuv = float(cpu)
        except: cpuv = 0
        st.metric("CPU", f"{cpuv:.1f}%")
        st.progress(min(cpuv/100,1.0))
        mem = sh("free | awk 'NR==2{printf \"%.0f\", $3/$2*100}'")
        try: memv = float(mem)
        except: memv = 0
        st.metric("RAM", f"{memv:.0f}%")
        st.progress(min(memv/100,1.0))
        bat = sh("cat /sys/class/power_supply/BAT0/capacity 2>/dev/null || echo 0")
        try: batv = float(bat)
        except: batv = 0
        st.metric("Baterai", f"{batv:.0f}%")
        st.progress(min(batv/100,1.0))
        ld = sh("cat /proc/loadavg | awk '{print $1,$2,$3}'")
        st.metric("Load (1/5/15)", ld)

# ---- Klien ----
with tabs[1]:
    st.subheader("Klien Terhubung")
    st.code(sh("cat /run/pocotui/dnsmasq.leases 2>/dev/null || echo '(tidak ada lease)'"), language="text")
    st.subheader("hostapd stations")
    st.code(sh("hostapd_cli -p /run/pocotui/hostapd -i ap0 all_sta 2>/dev/null | head -40 || echo '(n/a)'"), language="text")

# ---- Jaringan ----
with tabs[2]:
    c1, c2 = st.columns(2)
    with c1:
        st.subheader("Interface")
        st.code(sh("ip -br addr"), language="text")
    with c2:
        st.subheader("Routing")
        st.code(sh("ip route"), language="text")
    st.subheader("Counters")
    st.code(sh("cat /proc/net/dev"), language="text")

# ---- LTE ----
with tabs[3]:
    st.subheader("Modem / LTE")
    out = sh("mmcli -m 0")
    st.code(out, language="text")
    st.subheader("3GPP")
    st.code(sh("mmcli -m 0 | sed -n '/3GPP/,/---/p'"), language="text")

# ---- GPS ----
with tabs[4]:
    st.subheader("GPS")
    st.code(sh("mmcli -m 0 --location-get"), language="text")
    st.code(sh("gpspipe -w -n 3 2>/dev/null || echo 'gpsd tidak aktif'"), language="text")

# ---- Snort ----
with tabs[5]:
    st.subheader("Snort")
    card("Status", "berjalan" if sh("pgrep -f snort") else "mati", "good" if sh("pgrep -f snort") else "bad")
    st.code(sh("tail -20 /var/log/snort/alert 2>/dev/null || echo '(kosong)'"), language="text")

# ---- Suricata ----
with tabs[6]:
    st.subheader("Suricata")
    card("Status", "berjalan" if sh("pgrep -f suricata") else "mati", "good" if sh("pgrep -f suricata") else "bad")
    st.code(sh("tail -20 /var/log/suricata/fast.log 2>/dev/null || echo '(kosong)'"), language="text")

# ---- Sistem ----
with tabs[7]:
    st.subheader("Sistem")
    st.code(sh("cat /proc/cpuinfo | grep -m1 'model name'"), language="text")
    st.code(sh("free -h"), language="text")
    st.code(sh("df -h"), language="text")
    st.code(sh("uptime"), language="text")
    st.subheader("Log dnsmasq")
    st.code(sh("tail -30 /run/pocotui/dnsmasq.log 2>/dev/null || echo '(kosong)'"), language="text")

st.caption("POCO-TUI Web Dashboard · Streamlit · auto-refresh manual")
