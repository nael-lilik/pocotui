#!/bin/sh
# POCO-TUI installer - postmarketOS / Alpine (OpenRC) dan Debian/systemd.
#
#   ./install.sh                  pasang paket + program
#   ./install.sh --autostart      ...dan jalankan otomatis di layar (tty1) saat boot
#   ./install.sh --uninstall      hapus program & kembalikan autostart
#   ./install.sh --no-packages    lewati pemasangan paket (hanya salin file)
#
# Variabel: PREFIX=/usr/local (lokasi pasang)
set -e

PREFIX="${PREFIX:-/usr/local}"
DIR="$(cd "$(dirname "$0")" && pwd)"
AUTOSTART=0; UNINSTALL=0; PACKAGES=1
for a in "$@"; do
  case "$a" in
    --autostart) AUTOSTART=1 ;;
    --uninstall) UNINSTALL=1 ;;
    --no-packages) PACKAGES=0 ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "Opsi tidak dikenal: $a"; exit 1 ;;
  esac
done

[ "$(id -u)" = 0 ] || { echo "Harus dijalankan sebagai root (sudo/doas)."; exit 1; }

INITTAB=/etc/inittab
UNIT=/etc/systemd/system/pocotui.service

if [ "$UNINSTALL" = 1 ]; then
  echo "[*] Menghapus POCO-TUI"
  rm -f "$PREFIX/bin/pocotui" "$PREFIX/bin/pocotui-tty"
  if [ -f "$UNIT" ]; then
    systemctl disable --now pocotui.service 2>/dev/null || true
    rm -f "$UNIT"; systemctl daemon-reload 2>/dev/null || true
  fi
  if [ -f "$INITTAB.pocotui.bak" ]; then
    cp "$INITTAB.pocotui.bak" "$INITTAB" && rm -f "$INITTAB.pocotui.bak"
    echo "    inittab dipulihkan"
  fi
  echo "    (konfigurasi di /etc/pocotui dibiarkan; hapus manual bila perlu)"
  exit 0
fi

# ---------------------------------------------------------------- paket
if [ "$PACKAGES" = 1 ]; then
  echo "[1/3] Memasang paket"
  if command -v apk >/dev/null 2>&1; then
    for p in python3 hostapd dnsmasq iptables iproute2 iw rfkill redis libqrencode-tools \
             ncurses-terminfo-base kbd terminus-font; do
      apk add --no-cache "$p" >/dev/null 2>&1 && echo "    + $p" || echo "    ! lewati $p (tidak ada/gagal)"
    done
    rc-service redis start >/dev/null 2>&1 || true
    rc-update add redis default >/dev/null 2>&1 || true
    # layanan bawaan jangan ikut jalan sendiri (rebutan port 53 / wlan0)
    for s in dnsmasq hostapd; do rc-service "$s" stop >/dev/null 2>&1 || true; rc-update del "$s" >/dev/null 2>&1 || true; done
  elif command -v apt-get >/dev/null 2>&1; then
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3 hostapd dnsmasq iptables iproute2 iw rfkill redis-server qrencode kbd fonts-terminus
    systemctl unmask hostapd 2>/dev/null || true
    systemctl disable --now hostapd dnsmasq 2>/dev/null || true
    systemctl enable --now redis-server
  else
    echo "    Manajer paket tidak dikenal. Pasang manual: python3 hostapd dnsmasq iptables(atau nftables) iproute2 [qrencode]"
  fi
else
  echo "[1/3] Lewati paket (--no-packages)"
fi

# ---------------------------------------------------------------- berkas
echo "[2/3] Memasang program ke $PREFIX/bin"
mkdir -p "$PREFIX/bin" /etc/pocotui
install -m 755 "$DIR/pocotui.py" "$PREFIX/bin/pocotui"
cat > "$PREFIX/bin/pocotui-tty" <<EOF
#!/bin/sh
# Peluncur untuk layar fisik: font besar (layar HP beresolusi tinggi) + terminfo linux
export TERM=linux LANG=C.UTF-8
if command -v setfont >/dev/null 2>&1; then
  for f in /usr/share/consolefonts/ter-132n.psf.gz /usr/share/consolefonts/ter-v32n.psf.gz \\
           /usr/share/consolefonts/ter-132n.psf /usr/share/consolefonts/ter-v28n.psf.gz; do
    [ -f "\$f" ] && setfont "\$f" 2>/dev/null && break
  done
fi
exec $PREFIX/bin/pocotui "\$@"
EOF
chmod 755 "$PREFIX/bin/pocotui-tty"

# ---------------------------------------------------------------- autostart
echo "[3/3] Autostart"
if [ "$AUTOSTART" = 1 ]; then
  if [ -d /run/systemd/system ] || command -v systemctl >/dev/null 2>&1 && [ ! -f "$INITTAB" ]; then
    cat > "$UNIT" <<EOF
[Unit]
Description=POCO-TUI hotspot gateway dashboard
Conflicts=getty@tty1.service
After=systemd-user-sessions.service

[Service]
ExecStart=$PREFIX/bin/pocotui-tty
StandardInput=tty
StandardOutput=tty
TTYPath=/dev/tty1
TTYReset=yes
TTYVHangup=yes
TTYVTDisallocate=yes
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable pocotui.service
    echo "    systemd: pocotui.service aktif di tty1 (mulai: systemctl start pocotui)"
  elif [ -f "$INITTAB" ]; then
    [ -f "$INITTAB.pocotui.bak" ] || cp "$INITTAB" "$INITTAB.pocotui.bak"
    if ! grep -q 'pocotui-tty' "$INITTAB"; then
      sed -i 's|^\(tty1::.*\)$|#\1|' "$INITTAB"
      echo "tty1::respawn:$PREFIX/bin/pocotui-tty" >> "$INITTAB"
    fi
    echo "    inittab: tty1 sekarang menjalankan POCO-TUI (efektif setelah reboot / 'kill -HUP 1')"
    echo "    PERINGATAN: jika UI grafis (phosh/sxmo/tinydm) aktif, matikan dulu agar tidak rebutan layar:"
    echo "                rc-update del tinydm   (atau pilih UI 'console' di pmbootstrap)"
  else
    echo "    Sistem init tidak dikenali - jalankan manual: pocotui-tty"
  fi
else
  echo "    dilewati (tambahkan --autostart untuk jalan otomatis di layar)"
fi

echo
echo "Selesai. Coba dulu pratinjau:   pocotui --demo"
echo "Perangkat sebenarnya:           pocotui-tty        (sebagai root)"
