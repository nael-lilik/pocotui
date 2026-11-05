import hmac
import ipaddress
import json
import math
import os
import ssl
import time
import urllib.parse
import urllib.request

import streamlit as st

from serve_dashboard import collect_status


st.set_page_config(
    page_title="POCO-TUI · Mobile Gateway",
    page_icon="📡",
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.markdown(
    """
    <style>
    :root { color-scheme: dark; }
    .stApp { background: radial-gradient(ellipse at 12% 0%, #12334a 0, transparent 38%), #07111c; }
    .block-container { max-width: 1540px; padding-top: 1.4rem; }
    h1, h2, h3 { letter-spacing: -.035em; }
    [data-testid="stMetric"] {
      padding: 14px 16px; border: 1px solid #1d3448; border-radius: 14px;
      background: linear-gradient(145deg, #102133, #0a1723);
    }
    [data-testid="stMetricLabel"] { color: #91a8ba; }
    [data-testid="stMetricValue"] { color: #e6f0f7; }
    div[data-baseweb="tab-list"] { gap: 8px; }
    button[data-baseweb="tab"] { border-radius: 10px 10px 0 0; }
    div[data-testid="stDataFrame"] { border: 1px solid #1d3448; border-radius: 12px; }
    .status-card { padding: 16px; border: 1px solid #1d3448; border-radius: 14px; background: #0c1926; }
    .muted { color: #91a8ba; font-size: .9rem; }
    </style>
    """,
    unsafe_allow_html=True,
)


def display(value, fallback="—"):
    if value is None or value == "":
        return fallback
    return str(value)


def bytes_label(value):
    amount = float(value or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            return f"{amount:.1f} {unit}" if unit != "B" else f"{amount:.0f} B"
        amount /= 1024
    return "—"


def duration_label(value):
    seconds = max(0, int(value or 0))
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes = seconds // 60
    return f"{days}h {hours}j" if days else f"{hours}j {minutes}m" if hours else f"{minutes}m"


def gps_map_html(points):
    valid = []
    for point in points:
        try:
            latitude, longitude = float(point["latitude"]), float(point["longitude"])
        except (KeyError, TypeError, ValueError):
            continue
        if math.isfinite(latitude) and math.isfinite(longitude) and -90 <= latitude <= 90 and -180 <= longitude <= 180:
            valid.append((latitude, longitude))
    if not valid:
        return '<div class="muted">Belum ada posisi GPS valid tersimpan.</div>'

    lat0, lon0 = valid[-1]
    lon_scale = max(0.01, math.cos(math.radians(lat0)))
    projected = [((((lon - lon0 + 540) % 360) - 180) * lon_scale, -(lat - lat0))
                 for lat, lon in valid]
    min_x, max_x = min(p[0] for p in projected), max(p[0] for p in projected)
    min_y, max_y = min(p[1] for p in projected), max(p[1] for p in projected)
    width, height, pad = 800, 360, 42
    scale = min((width - 2 * pad) / max(1e-8, max_x - min_x),
                (height - 2 * pad) / max(1e-8, max_y - min_y))
    coords = [(width / 2 + (x - (min_x + max_x) / 2) * scale,
               height / 2 + (y - (min_y + max_y) / 2) * scale)
              for x, y in projected]
    lines = "".join(
        f'<line x1="{x}" y1="0" x2="{x}" y2="{height}"/><line x1="0" y1="{y}" x2="{width}" y2="{y}"/>'
        for i in range(1, 8)
        for x, y in [(i * width / 8, i * height / 8)]
    )
    path = " ".join(f"{x:.2f},{y:.2f}" for x, y in coords)
    dots = "".join(
        f'<circle cx="{x:.2f}" cy="{y:.2f}" r="3" class="track"/>'
        for i, (x, y) in enumerate(coords[:-1])
        if i % max(1, len(coords) // 30) == 0
    )
    current_x, current_y = coords[-1]
    return f"""
    <svg viewBox="0 0 {width} {height}" role="img" aria-label="Peta lintasan GPS lokal"
         style="width:100%;height:auto;min-height:260px;background:#08141f;border-radius:12px">
      <g class="grid">{lines}</g>
      {f'<polyline points="{path}" class="path"/>' if len(coords) > 1 else ""}
      {dots}<circle cx="{current_x:.2f}" cy="{current_y:.2f}" r="7" class="current"/>
      <style>
        .grid line{{stroke:#284157;stroke-width:1;opacity:.5}}
        .path{{fill:none;stroke:#51e0d0;stroke-width:3;stroke-linecap:round;stroke-linejoin:round}}
        .track{{fill:#63a8ff;stroke:#d4edff;stroke-width:1}}
        .current{{fill:#67e3a2;stroke:#e9fff4;stroke-width:3}}
      </style>
    </svg>
    """


def render_dashboard():
    try:
        data = collect_status(os.environ.get("POCOTUI_CONFIG", "/etc/pocotui/config.json"))
    except Exception as error:
        st.error(f"Gagal mengambil status perangkat: {error}")
        return

    hotspot, system = data["hotspot"], data["system"]
    network, gps = data["network"], data["gps"]
    power, firewall = data["power"], data["firewall"]
    services = data["services"]
    battery = system["battery"]
    active_interface = network["active_interface"]

    st.title("📡 POCO-TUI Gateway")
    st.caption(
        f"{data['hostname']} · diperbarui {time.strftime('%H:%M:%S', time.localtime(data['timestamp']))}"
        " · refresh otomatis setiap 5 detik"
    )

    tabs = st.tabs([
        "Ringkasan", "Klien", "Jaringan & LTE", "GPS & Peta",
        "Sistem & Daya", "Keamanan", "Webcam / CCTV",
    ])

    with tabs[0]:
        state_col, client_col, route_col, power_col = st.columns(4)
        state_col.metric("Hotspot", "AKTIF" if hotspot["active"] else "MATI", hotspot["ssid"])
        client_col.metric("Klien", f"{hotspot['client_count']} / {hotspot['max_clients']}")
        route_col.metric("Upstream", display(active_interface))
        power_col.metric("Profil CPU", power["profile"].upper(),
                         "Eco otomatis" if power["automatic_eco"] and power["profile"] == "eco" else None)

        left, right = st.columns([1.3, 1])
        with left:
            st.subheader("Throughput upstream")
            counters = network["counters"].get(active_interface, {})
            now = data["timestamp"]
            previous = st.session_state.get("network_sample")
            samples = st.session_state.setdefault("traffic_history", [])
            if previous and previous["interface"] == active_interface and now > previous["time"]:
                elapsed = max(0.5, now - previous["time"])
                samples.append({
                    "Waktu": time.strftime("%H:%M:%S", time.localtime(now)),
                    "RX KB/s": max(0, counters.get("rx_bytes", 0) - previous["rx"]) / elapsed / 1024,
                    "TX KB/s": max(0, counters.get("tx_bytes", 0) - previous["tx"]) / elapsed / 1024,
                })
                del samples[:-40]
                st.session_state["network_sample"] = {
                    "interface": active_interface, "time": now,
                    "rx": counters.get("rx_bytes", 0), "tx": counters.get("tx_bytes", 0),
                }
                if len(samples) >= 2:
                    st.line_chart({
                        "RX KB/s": [sample["RX KB/s"] for sample in samples],
                        "TX KB/s": [sample["TX KB/s"] for sample in samples],
                    }, height=260)
            else:
                st.info("Grafik throughput memerlukan dua sampel; pengukuran sedang dikumpulkan.")
        with right:
            st.subheader("Status perangkat")
            st.metric("Baterai", f"{battery['percent']}%" if battery["percent"] is not None else "—",
                      battery["status"])
            st.metric("CPU", f"{system['cpu_percent']}%" if system["cpu_percent"] is not None else "—")
            st.metric("Memori", f"{system['memory_percent']}%" if system["memory_percent"] is not None else "—")
            st.metric("Temperatur", f"{system['temperature_c']} °C" if system["temperature_c"] is not None else "—")
        st.subheader("Peta lintasan GPS lokal")
        st.markdown(gps_map_html(gps.get("track", [])), unsafe_allow_html=True)
        if gps.get("track"):
            st.caption("Peta lokal tanpa tile eksternal. Koordinat bersifat sensitif.")

    with tabs[1]:
        st.subheader("Klien hotspot")
        clients = hotspot.get("clients", [])
        if clients:
            st.dataframe(
                [{
                    "Perangkat": client.get("hostname") or "Perangkat tanpa nama",
                    "IP": client.get("ip"),
                    "MAC": client.get("mac"),
                    "Sinyal": f"{client['signal']} dBm" if client.get("signal") is not None else "—",
                    "Durasi": duration_label(client.get("connected_time")),
                    "RX": bytes_label(client.get("rx_bytes")),
                    "TX": bytes_label(client.get("tx_bytes")),
                } for client in clients],
                width="stretch", hide_index=True,
            )
        else:
            st.info("Belum ada klien yang terhubung.")
        st.caption(f"Interface AP: {hotspot['ap_interface']} · Gateway: {hotspot['gateway']}")

    with tabs[2]:
        st.subheader("Interface dan routing")
        interface_col, routes_col = st.columns([1.2, 1])
        with interface_col:
            st.dataframe(
                [{
                    "Interface": item["name"], "Status": item["state"],
                    "IPv4": ", ".join(item["addresses"]) or "—",
                    "RX": bytes_label(network["counters"].get(item["name"], {}).get("rx_bytes")),
                    "TX": bytes_label(network["counters"].get(item["name"], {}).get("tx_bytes")),
                } for item in network["interfaces"]],
                width="stretch", hide_index=True,
            )
        with routes_col:
            st.markdown("**Default routes**")
            if network["default_routes"]:
                st.dataframe(network["default_routes"], width="stretch", hide_index=True)
            else:
                st.warning("Default route tidak ditemukan.")
        st.subheader("Modem / LTE")
        lte = data["lte"]
        if lte["available"]:
            lte_cols = st.columns(4)
            for col, label, key in zip(
                lte_cols, ("Status", "Operator", "Teknologi", "Sinyal"),
                ("state", "operator", "technology", "signal"),
            ):
                col.metric(label, display(lte.get(key)))
            with st.expander("Informasi modem lengkap"):
                st.code(lte.get("raw", ""), language="text")
        else:
            st.info("ModemManager tidak tersedia atau modem belum terbaca.")

    with tabs[3]:
        st.subheader("Lokasi dan lintasan GPS")
        latest = gps.get("latest") or {}
        location = latest.get("parsed_location") or {}
        gps_col, map_col = st.columns([1, 1.5])
        with gps_col:
            st.metric("Status Redis", "Tersambung" if gps["available"] else "Tidak tersedia")
            if location.get("latitude") is not None and location.get("longitude") is not None:
                st.metric(
                    "Koordinat",
                    f"{float(location['latitude']):.6f}, {float(location['longitude']):.6f}",
                )
            else:
                st.info("Belum ada fix GPS tersimpan.")
            st.write(f"Satellites: {display(location.get('satellites'))}")
            st.write(f"HDOP: {display(location.get('hdop'))}")
            st.write(f"Altitude: {display(location.get('altitude_m'))} m")
            st.write(f"Kecepatan: {display(location.get('speed_kmh'))} km/j")
            st.write(f"Waktu: {display(latest.get('timestamp'))}")
            st.caption(f"Jumlah titik track: {len(gps.get('track', []))}")
            if not gps["available"]:
                st.warning(gps.get("reason", "Redis tidak tersedia"))
        with map_col:
            st.markdown(gps_map_html(gps.get("track", [])), unsafe_allow_html=True)
        if latest:
            with st.expander("Data GPS mentah terakhir di Redis"):
                st.code(latest.get("raw_gps_data", json.dumps(latest, ensure_ascii=False, indent=2))[:12000])

    with tabs[4]:
        st.subheader("Sistem dan sumber daya")
        metrics = st.columns(4)
        metrics[0].metric("CPU", f"{system['cpu_percent']}%" if system["cpu_percent"] is not None else "—")
        metrics[1].metric("RAM", f"{system['memory_percent']}%" if system["memory_percent"] is not None else "—")
        metrics[2].metric("Baterai", f"{battery['percent']}%" if battery["percent"] is not None else "—", battery["status"])
        metrics[3].metric("Uptime", duration_label(system["uptime_seconds"]))
        metrics = st.columns(4)
        metrics[0].metric("Temperatur", f"{system['temperature_c']} °C" if system["temperature_c"] is not None else "—")
        metrics[1].metric("Load 1 / 5 / 15m", " / ".join(f"{value:.2f}" for value in system["load"]) or "—")
        metrics[2].metric("Disk", f"{system['disk_percent']}%" if system["disk_percent"] is not None else "—")
        metrics[3].metric("Profil CPU", power["profile"].upper())
        st.markdown("**CPU policies**")
        if power["policies"]:
            st.dataframe(
                [{
                    "Policy": policy["name"], "Cluster": policy["cluster"],
                    "CPU": policy["cpus"], "Sekarang": f"{policy['current_mhz']} MHz" if policy["current_mhz"] else "—",
                    "Batas": f"{policy['max_mhz']} MHz" if policy["max_mhz"] else "—",
                    "Governor": policy["governor"],
                } for policy in power["policies"]],
                width="stretch", hide_index=True,
            )
        else:
            st.info("Kernel tidak menyediakan policy cpufreq.")
        st.caption(system["platform"])

    with tabs[5]:
        st.subheader("Status layanan dan firewall")
        services_cols = st.columns(3)
        service_items = [
            ("Hotspot / hostapd", services["hostapd"]),
            ("DHCP / DNS", services["dnsmasq"]),
            ("IP forwarding", services["ip_forward"]),
            ("Redis", services["redis"]),
            ("Suricata", services["suricata"]),
            ("nftables", services["nftables"]),
        ]
        for index, (label, active) in enumerate(service_items):
            services_cols[index % len(services_cols)].metric(label, "AKTIF" if active else "MATI")
        st.markdown("**nftables**")
        if firewall["available"]:
            st.write(
                f"Ruleset terbaca · {firewall['tables']} tables · "
                f"{firewall['chains']} chains · {firewall['rules']} rules"
            )
            if firewall["base_chains"]:
                st.dataframe(firewall["base_chains"], width="stretch", hide_index=True)
            else:
                st.info("Ruleset terbaca, tetapi tidak ada base chain.")
        else:
            st.warning(f"Ruleset nftables tidak tersedia: {firewall.get('reason', 'unknown')}")
        st.caption("Dashboard hanya untuk observasi; perubahan jaringan tetap dilakukan dari POCO-TUI.")

    with tabs[6]:
        st.subheader("Webcam / CCTV portabel")
        camera_host = os.environ.get("CAMERA_HOST", "127.0.0.1")
        camera_device = os.environ.get("CAMERA_DEVICE", "/dev/video0")
        camera_width = os.environ.get("CAMERA_WIDTH", "640")
        camera_height = os.environ.get("CAMERA_HEIGHT", "480")
        camera_fps = os.environ.get("CAMERA_FPS", "8")
        try:
            camera_port = int(os.environ.get("CAMERA_PORT", "8090"))
            if not 1 <= camera_port <= 65535:
                raise ValueError
        except ValueError:
            st.error("CAMERA_PORT harus berupa angka antara 1-65535.")
            return
        local_camera = camera_host.lower() in ("127.0.0.1", "::1", "localhost")
        camera_url_host = (
            hotspot.get("gateway", "192.168.50.1")
            if camera_host in ("0.0.0.0", "::", "")
            else camera_host
        )
        try:
            if ipaddress.ip_address(camera_url_host).version == 6:
                camera_url_host = f"[{camera_url_host}]"
        except ValueError:
            pass
        camera_scheme = "https" if os.environ.get("CAMERA_TLS_CERT") else "http"
        camera_url = urllib.parse.urlunsplit((
            camera_scheme, f"{camera_url_host}:{camera_port}", "/", "", "",
        ))
        probe_host = (
            "127.0.0.1" if camera_host in ("0.0.0.0", "")
            else "::1" if camera_host == "::"
            else camera_host
        )
        try:
            if ipaddress.ip_address(probe_host).version == 6:
                probe_host = f"[{probe_host}]"
        except ValueError:
            pass
        health_url = urllib.parse.urlunsplit((
            camera_scheme, f"{probe_host}:{camera_port}", "/health", "", "",
        ))
        cert_path = os.environ.get("CAMERA_TLS_CERT", "")
        camera_probe_error = None
        try:
            tls_context = (
                ssl.create_default_context(cafile=cert_path or None)
                if camera_scheme == "https" else None
            )
            with urllib.request.urlopen(health_url, timeout=0.5, context=tls_context) as response:
                health = json.load(response)
                camera_running = response.status == 200 and isinstance(health, dict) and health.get("ok") is True
                if not camera_running:
                    camera_probe_error = "endpoint /health mengembalikan respons tidak valid"
        except (OSError, ValueError) as error:
            camera_running = False
            camera_probe_error = str(error)

        camera_cols = st.columns(4)
        camera_cols[0].metric("Camera server", "AKTIF" if camera_running else "TIDAK TERDETEKSI")
        camera_cols[1].metric("Device V4L2", camera_device)
        camera_cols[2].metric("Resolusi", f"{camera_width}×{camera_height}")
        camera_cols[3].metric("Batas frame rate", f"{camera_fps} FPS")
        if os.path.exists(camera_device):
            st.success("Node device tersedia; dukungan capture driver/media pipeline perlu diuji dengan viewer.")
        else:
            st.warning(f"Device kamera tidak ditemukan: {camera_device}")
        if camera_running and not local_camera:
            st.link_button("Buka live camera viewer", camera_url)
        elif camera_running:
            st.info("Server hanya menerima koneksi lokal perangkat; atur CAMERA_HOST dan autentikasi TLS untuk viewer jaringan.")
        else:
            st.info("Server kamera tidak merespons health check; kamera tidak dipastikan sedang digunakan.")
            if camera_probe_error:
                st.caption(f"Detail health check: {camera_probe_error}")
        st.caption(
            "Stream MJPEG hanya aktif saat ada viewer; mendukung satu viewer dan tidak menyimpan rekaman. "
            "Aktivitas kamera memakai baterai dan dapat menaikkan suhu."
        )
        st.caption("Remote viewer perlu password + HTTPS; percayai sertifikat perangkat pada klien.")


@st.fragment(run_every="5s")
def live_dashboard():
    render_dashboard()


def authenticated():
    password = os.environ.get("DASHBOARD_PASSWORD", "")
    if not password or st.session_state.get("dashboard_authenticated"):
        return True

    st.title("🔐 POCO-TUI Gateway")
    st.caption("Masukkan password dashboard untuk melanjutkan.")
    with st.form("dashboard-login"):
        supplied = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Masuk")
    if submitted:
        if hmac.compare_digest(supplied, password):
            st.session_state["dashboard_authenticated"] = True
            st.rerun()
        st.error("Password salah.")
    return False


if authenticated():
    live_dashboard()
