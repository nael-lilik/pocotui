# POCO-TUI — Mini Hotspot Gateway + Dashboard TUI (3 tombol)

Dashboard terminal interaktif untuk Poco F1 (postmarketOS) yang mengubah HP menjadi
**hotspot gateway kecil**: AP WiFi (hostapd) + DHCP/DNS (dnsmasq) + NAT (iptables/nftables).
Seluruh antarmuka dikontrol hanya dengan **VOL+, VOL−, POWER** — cocok untuk layar
sentuh yang tidak terbaca.

Tanpa dependensi pip: cukup Python 3 (modul `curses` bawaan).

## Kredensial bot Telegram (opsional)

Jika menggunakan `telegram_bot.py`, salin `.env.example` menjadi `.env`, lalu isi
`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, dan kredensial hotspot/LLM yang diperlukan.
`OLLAMA_URL`, `OLLAMA_MODEL`, `REMOTE_URL`, dan `LLM_MODEL` mengatur endpoint serta
model LLM; sesuaikan dengan layanan yang digunakan.
Jalankan bot dari direktori proyek; `.env` dibaca otomatis dan tidak dilacak Git.
Batasi akses file dengan `chmod 600 .env`. Jangan membagikan atau meng-commit file ini.
Jika token atau API key pernah tersimpan di Git, cabut/rotasi kredensial tersebut;
menghapusnya dari versi terbaru tidak menghapusnya dari riwayat Git.

## Pasang

```sh
sudo ./install.sh                 # paket + program
sudo ./install.sh --autostart     # + otomatis tampil di layar (tty1) saat boot
./pocotui.py --demo               # pratinjau di PC, data simulasi, tidak mengubah sistem
```

Jalankan di perangkat (butuh root): `sudo pocotui-tty` (di layar HP) atau `sudo pocotui` (via SSH;
tombol fisik tetap terbaca langsung dari `/dev/input`).

> Distro lain (mis. "Nura OS"): selama ada Linux standar (hostapd, dnsmasq, iproute2,
> iptables/nftables, Python 3) program ini jalan. `install.sh` mengenali apk (Alpine/pmOS)
> dan apt; selain itu pasang paketnya manual lalu salin `pocotui.py`.

## Kontrol tombol

| Tombol | Dasbor | Menu / daftar | Mengetik teks |
|---|---|---|---|
| **VOL+** | tab kiri | naik | karakter sebelumnya |
| **VOL−** | tab kanan | turun | karakter berikutnya |
| **POWER** (singkat) | buka menu | pilih / OK | ketik karakter terpilih |
| **POWER** (tahan 0,7 dtk) | nyalakan/matikan hotspot | kembali | hapus 1 karakter |

Menahan VOL+/VOL− mengulang otomatis (scroll cepat). Saat mengetik, putar sampai
`[SIMPAN]` lalu tekan POWER; ada juga `[HAPUS]`, `[KOSONGKAN]`, `[BATAL]`.
Layar mati otomatis (hemat baterai); tombol apa pun menyalakannya kembali
(tekanan pertama hanya membangunkan layar).
Lewat SSH/keyboard: panah atau `j/k`, Enter, Esc/`q`.

## Tampilan

- **Ringkasan** — status hotspot, SSID/radio/gateway/upstream, cek internet (ping), CPU
  (total + per-core + frekuensi), suhu, RAM, baterai (+ watt), disk, load, grafik mini trafik.
- **Klien** — perangkat terhubung: nama, IP, MAC, sinyal (dBm), unduh/unggah, durasi.
- **Trafik** — grafik download/upload ke klien, puncak, total sesi.
- **Log** — log aplikasi langsung; layar Log penuh di Menu juga membaca hostapd, dnsmasq, dmesg.

Layout otomatis menyesuaikan: satu kolom (portrait HP) atau dua kolom (layar lebar ≥100 kolom).

## Menu

`Hotspot` (nyala/mati/restart, kredensial, **QR WiFi**, info koneksi) · `Klien` (kick, blokir,
daftar blokir) · `Jaringan` (upstream, NAT, DNS, subnet, tes ping, daftar interface) ·
`Pengaturan Hotspot` (SSID, sandi, band 2.4/5 GHz, channel, SSID tersembunyi, maks. klien,
kode negara, interface WiFi) · `Tampilan & Daya` (kecerahan, layar mati otomatis, tema, ASCII/Unicode,
interval data) · `Log` · `Sistem` (autostart hotspot, info perangkat, keluar, reboot, matikan, reset) · `Bantuan`.

Perubahan pengaturan radio berlaku setelah *Restart hotspot* (ada pengingat di layar).

## Cara kerja hotspot

1. **`wlan0` tidak disentuh.** Dibuat interface virtual `ap0` (`iw dev wlan0 interface add ap0 type __ap`,
   MAC berbeda) yang diberi IP gateway (default `192.168.50.1/24`). Saat hotspot dimatikan hanya `ap0` yang dihapus.
   Mode lama (AP langsung di wlan0) masih ada: Menu > Pengaturan Hotspot > Mode AP.
2. `hostapd` membuat AP di `ap0` (WPA2-PSK CCMP, atau terbuka bila sandi kosong).
3. `dnsmasq` memberi DHCP (`.10–.110`) dan DNS (upstream: Cloudflare/Google/Quad9/AdGuard — pilih di menu).
4. `ip_forward=1` + aturan NAT di chain/tabel khusus `POCOTUI` / `pocotui` (mudah dibersihkan, tidak
   menyentuh aturan firewall lain). Dipakai `iptables` bila ada, jika tidak `nft`.
5. Saat dimatikan semuanya dibersihkan, `ip_forward` dikembalikan, wlan0 dikembalikan ke NetworkManager.

Upstream **auto** mengikuti rute default (mis. `usb0` USB/RNDIS, `rmnet_ipa0`/`wwan0` modem LTE, `eth0`).
Bisa dipaksa di Menu > Jaringan > Interface upstream.

Konfigurasi di `/etc/pocotui/config.json` (mode 600). **Sandi WPA2 acak 10 karakter dibuat otomatis pada
start pertama** — lihat/ubah di Menu. Jangan memakai sandi bawaan yang umum.

## Batasan `ap0` (AP + klien di satu chip)

- WiFi Poco F1 (WCN3990/ath10k) hanya punya **satu radio**: bila `wlan0` sedang tersambung ke WiFi lain,
  `ap0` **dipaksa memakai channel/band yang sama** dengan `wlan0`. TUI mendeteksinya otomatis dan menampilkan
  "(ikut wlan0)" di panel Radio; pengaturan Band/Channel hanya berlaku saat wlan0 tidak tersambung.
- Bila `wlan0` pindah channel (roaming) saat hotspot hidup, klien bisa terputus sebentar.
- Tergantung driver/firmware: jika `iw ... interface add` ditolak, hotspot gagal dengan pesan jelas;
  pilih Mode AP > "wlan0 langsung" sebagai cadangan (wlan0 akan terputus dari WiFi lain).
- `wlan0` bisa jadi upstream (repeater): Menu > Jaringan > Interface upstream (auto mengikuti rute default).

## Pemecahan masalah

- **Tombol tidak bereaksi** — lihat Menu > Sistem > Info perangkat (baris *Tombol*). Bila kosong, jalankan
  `sudo evtest` untuk melihat nama device; tombol power (pwrkey) dan volume (gpio-keys) harus muncul.
  Coba `pocotui --no-grab` bila ada konflik dengan layanan lain.
- **Power menekan mati perangkat** — TUI mengambil alih tombol (EVIOCGRAB) selama berjalan sehingga
  logind/acpid tidak menerima event. Sebelum TUI start / setelah keluar, perilaku normal kembali.
  Keluar lewat Menu > Sistem. (Tahan power ±10 dtk tetap *hard reset* PMIC.)
- **"AP tidak naik" / hostapd gagal** — lihat Log > hostapd. Penyebab umum: kode negara/channel tidak
  diizinkan (ganti channel 1/6/11 atau band 2.4), driver tidak mendukung mode AP bersamaan dengan
  mode klien (WiFi HP tidak boleh sedang tersambung ke AP lain), atau interface salah (Pengaturan > Interface WiFi).
- **dnsmasq gagal** — port 53 dipakai layanan lain (`systemd-resolved`, dnsmasq sistem). Installer sudah
  mematikan layanan bawaan.
- **Klien dapat IP tapi tidak ada internet** — cek baris *Upstream* & *Internet* di Ringkasan. Upstream
  harus punya rute default; koneksi USB dari PC biasanya muncul sebagai `usb0`. Data seluler (di Poco F1
  interface modem bernama `rmnet_ipa0`) baru bisa jadi upstream setelah modem benar-benar terhubung
  (ModemManager/qmicli, APN, rute default) — di luar cakupan program ini; TUI hanya memakai interface yang sudah punya rute.
- **Layar fisik kecil/besar** — `pocotui-tty` mencoba font Terminus 32px lewat `setfont`; ubah daftar font
  di skrip itu bila perlu. Karakter aneh? Menu > Tampilan > Karakter: ASCII (atau `--ascii`).
- **UI grafis (phosh/sxmo) bentrok** — TUI ini untuk konsol; pakai UI `console` atau matikan display manager.

## Catatan keamanan

Program mengubah jaringan dan firewall sebagai root. Hotspot dengan WPA2 + sandi acak aman untuk
penggunaan pribadi; jangan dibuka (tanpa sandi) kecuali sengaja. Blokir klien memakai ACL MAC hostapd
(bisa dipalsukan — bukan pengganti sandi yang kuat).
