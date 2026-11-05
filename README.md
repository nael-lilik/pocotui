# POCO-TUI — Mini Hotspot Gateway + Dashboard TUI (3 tombol)

Dashboard terminal interaktif untuk Poco F1 (postmarketOS) yang mengubah HP menjadi
**hotspot gateway kecil**: AP WiFi (hostapd) + DHCP/DNS (dnsmasq) + NAT (iptables/nftables).
Seluruh antarmuka dikontrol hanya dengan **VOL+, VOL−, POWER** — cocok untuk layar
sentuh yang tidak terbaca.

POCO-TUI terminal tidak memerlukan dependensi pip tambahan (menggunakan modul `curses` bawaan Python).
Web dashboard berjalan dengan Streamlit.

## Kredensial bot Telegram (opsional)

Jika menggunakan `telegram_bot.py`, salin `.env.example` menjadi `.env`, lalu isi
`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, dan kredensial hotspot/LLM yang diperlukan.
`OLLAMA_URL`, `OLLAMA_MODEL`, `REMOTE_URL`, dan `LLM_MODEL` mengatur endpoint serta
model LLM; sesuaikan dengan layanan yang digunakan.
`APN` menjadi nilai awal APN pada wizard profil LTE di POCO-TUI; kosongkan untuk
memasukkan APN secara manual saat wizard berjalan. POCO-TUI membaca `.env` di direktori
proyek, atau `/etc/pocotui/.env` untuk pemasangan sistem. Variabel `POCOTUI_ENV_FILE`
dapat digunakan untuk menentukan lokasi file lain.
`GPS_ENABLED=1` mengaktifkan pembacaan GPS ModemManager (default untuk menjaga perilaku lama);
atur `GPS_ENABLED=0` untuk mencegah TUI mengaktifkan atau membaca data lokasi. Opsi ini tidak
mematikan GNSS secara global jika sebelumnya sudah diaktifkan atau dipakai layanan lain.
Aktivasi sumber GPS dilakukan sekali per modem selama proses TUI berjalan. `GPS_MODEM=0`
memilih indeks modem yang dipakai, dan dapat disesuaikan jika indeks GPS ModemManager perangkat berbeda.
Pembacaan modem/GPS pada panel berjalan di latar belakang agar TUI tetap responsif saat ModemManager lambat.
Dengan `REDIS_URL=redis://127.0.0.1:6379/0`, POCO-TUI menyimpan seluruh respons mentah GPS dari ModemManager,
termasuk data saat belum ada fix, ke Redis setiap
`GPS_REDIS_INTERVAL` detik (default 60) dan menerapkan retensi `GPS_REDIS_RETENTION_DAYS` hari
(default 30). URL `rediss://` untuk koneksi TLS dan kredensial URL (`redis://user:password@host:port/db`)
didukung; kosongkan `REDIS_URL` untuk mematikan penyimpanan. Pastikan Redis telah dipasang dan berjalan
lokal, atau tentukan endpoint privat tepercaya. Data disimpan sebagai Redis Stream `pocotui:location`
(maksimum sekitar 50.000 record) serta key JSON `pocotui:location:latest`; tiap record menyimpan teks penuh
respons `mmcli --location-get` pada `raw_gps_data`, modem sumber, hasil parsing jika fix valid, dan konteks
baterai, charging, suhu, upstream, status hotspot, serta jumlah klien. Data lokasi sensitif: gunakan Redis
lokal/terproteksi, jangan mengekspos port Redis ke internet, dan atur retensi sesuai kebutuhan. Installer
standar memasang dan mengaktifkan Redis lokal; `--no-packages` melewati pemasangan tersebut. Gangguan Redis
tidak menghentikan TUI; status Log membedakan error pembacaan GPS dan koneksi Redis, mencatat pemulihan,
dan memakai jeda retry Redis bertingkat sampai maksimum 15 menit. Jika muncul `Connection refused`, pastikan
service Redis berjalan (`rc-service redis status` di Alpine atau `systemctl status redis-server` di Debian)
dan `REDIS_URL` menunjuk host/port yang benar.
Tab **Peta** menampilkan posisi dan sampai 500 titik track terakhir dari sesi saat ini pada grid. Menu
Monitoring & layanan > GPS > **Peta terminal (lintasan lokal)** juga menyediakan tampilan penuh dan zoom.
Ini peta jejak lokal, bukan peta jalan: tidak memakai tile, internet, atau layanan peta pihak ketiga.
Jalankan bot dari direktori proyek; `.env` dibaca otomatis dan tidak dilacak Git.
Batasi akses file dengan `chmod 600 .env`. Jangan membagikan atau meng-commit file ini.
Jika token atau API key pernah tersimpan di Git, cabut/rotasi kredensial tersebut;
menghapusnya dari versi terbaru tidak menghapusnya dari riwayat Git.

## Pasang

```sh
sudo ./install.sh                 # paket + program
sudo ./install.sh --autostart     # + otomatis tampilkan TUI di layar (tty1) saat boot
./pocotui.py --demo               # pratinjau di PC, data simulasi, tidak mengubah sistem
```

Jika Streamlit sudah terpasang, installer mengaktifkan service dashboard saat boot secara default
(`pocotui-dashboard` pada OpenRC atau `pocotui-dashboard.service` pada systemd). Dashboard bind hanya ke
`127.0.0.1:7890` secara default. Untuk mengakses dari perangkat lain, atur bind publik dengan autentikasi
dan TLS seperti dijelaskan di bawah, atau gunakan reverse proxy TLS.
Perintah `--autostart` hanya mengatur TUI terminal, bukan dashboard web. Jika Streamlit belum ada, installer
menampilkan peringatan dan melewati service dashboard; pasang Streamlit lalu jalankan kembali installer.
Service dapat dihentikan/dimulai dengan `rc-service pocotui-dashboard stop|start` atau
`systemctl stop|start pocotui-dashboard.service`.

### Webcam / CCTV portabel

POCO-TUI menyediakan server MJPEG on-demand dari kamera V4L2. Jalankan TUI, lalu pilih
**Monitoring & layanan > Webcam / CCTV > Jalankan camera stream**. Stream hanya menangkap
selama ada viewer, mendukung satu viewer, dan tidak merekam otomatis. Tutup viewer untuk
menghentikan capture; fitur ini dapat memakai baterai dan menaikkan suhu.

Installer memasang FFmpeg; driver kamera dan media pipeline kernel tetap harus mendukung
capture V4L2. Ketersediaan `/dev/video*` saja tidak menjamin kamera dapat menghasilkan frame.
Jika viewer menampilkan error, periksa `/run/pocotui/camera.log`; FFmpeg menuliskan kegagalan
capture di sana. Pada kernel POCO F1 yang diuji sebelumnya, `/dev/video0` gagal dengan
`VIDIOC_STREAMON: Broken pipe`; server MJPEG tidak memperbaiki pipeline kernel tersebut,
jadi streaming kamera nyata belum dapat dipastikan sampai masalah driver/media pipeline diatasi.

Default-nya server hanya bind ke `127.0.0.1`. Untuk akses dari jaringan, atur di `.env`:

```dotenv
CAMERA_HOST=0.0.0.0
CAMERA_PORT=8090
CAMERA_PASSWORD=ganti-dengan-password-kuat
CAMERA_TLS_CERT=/etc/pocotui/dashboard.crt
CAMERA_TLS_KEY=/etc/pocotui/dashboard.key
```

Nonaktifkan akses jaringan jika tidak memerlukannya. Bind non-localhost ditolak kecuali
password kamera dan kedua berkas sertifikat TLS tersedia. Password kamera terpisah dari
password dashboard; gunakan permission ketat untuk `.env` dan private key. Saat bind ke
`0.0.0.0`, dashboard memberi tautan viewer melalui alamat gateway hotspot (`192.168.50.1`);
klien di jaringan lain dapat memakai alamat IP perangkat yang sesuai. Uji jalur kamera
secara lokal terlebih dahulu sebelum mengandalkannya sebagai CCTV.

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
- **Jaringan** — mode upstream, jalur aktif beserta alamat IPv4, hasil probe Wi-Fi, status profil LTE
  backup, ping sistem, dan informasi modem.
- **Klien** — perangkat terhubung: nama, IP, MAC, sinyal (dBm), unduh/unggah, durasi.
- **Trafik** — grafik download/upload ke klien, puncak, total sesi.
- **Log** — log aplikasi langsung; layar Log penuh di Menu juga membaca hostapd, dnsmasq, dmesg.

Layout otomatis menyesuaikan: satu kolom (portrait HP) atau dua kolom (layar lebar ≥100 kolom).

## Menu

Menu utama diringkas menjadi `Hotspot` (nyala/mati/restart, kredensial, **QR WiFi**, info koneksi),
`Klien` (kick, blokir, daftar blokir), `Jaringan` (upstream, NAT, DNS, subnet, tes ping, interface),
`Pengaturan` (WiFi/AP: SSID, sandi, band/channel, negara, klien, kunci BSSID; tampilan/daya: kecerahan, timeout,
tema, ASCII/Unicode, interval), `Monitoring & layanan` (Snort, Suricata, LTE, GPS, web dashboard),
`Log`, `Sistem`, dan `Bantuan`. Menu Monitoring & layanan juga menyediakan **Webcam / CCTV**. Tekan POWER
tahan untuk kembali/menutup menu.
Dasbor memiliki tab **Peta** khusus; halaman Ringkasan juga menampilkan mini-map. Menu Monitoring & layanan
> GPS menyediakan **Peta terminal** penuh dengan zoom (VOL+ memperbesar, VOL− memperkecil). Semua peta
menampilkan posisi dan track GPS sesi berjalan pada grid karakter, bersifat lokal (tidak mengunduh peta
jalan/tile dan tidak mengirim koordinat ke penyedia peta).

Perubahan pengaturan radio berlaku setelah *Restart hotspot* (ada pengingat di layar).
Menu `Sistem` juga mengatur AP idle (default 30 menit tanpa klien; dapat dinonaktifkan) dan
shutdown baterai rendah (default aktif pada 15%). Di `Tampilan & daya` tersedia profil CPU:
**Eco** membatasi frekuensi maksimum tiap policy ke sekitar 60% kemampuan policy/cluster dan memilih governor
hemat daya bila tersedia; **Balanced** mengembalikan governor serta batas awal; **Performance** memilih governor `performance` bila
tersedia (fallback ke governor dinamis yang didukung) dan melepas pembatas maksimum ke batas kernel. Poco F1
memiliki cluster efisiensi/performa;
POCO-TUI mendeteksi policy/frekuensi dari sysfs dan tidak mengasumsikan semua kernel mengekspos delapan core
dengan cara yang sama. **Eco otomatis saat baterai <=20%** aktif secara default ketika baterai discharging,
lalu kembali ke profil pilihan saat charger tersambung atau kondisi pulih. Kontrol CPU memerlukan akses tulis
ke cpufreq (umumnya jalankan TUI sebagai root); jika salah satu policy tidak mendukung governor atau batas
frekuensi yang diminta, error dicatat dengan nama policy agar status aktual dapat diperiksa.

Web dashboard utama dijalankan dari Menu > Monitoring & layanan > Web dashboard menggunakan **Streamlit**.
Dashboard read-only menampilkan ringkasan perangkat, klien, route/interface dan throughput, LTE, GPS/map,
sumber daya, profil CPU, status service, ringkasan nftables, dan status webcam; refresh otomatis tiap 5 detik.
Dashboard menampilkan koordinat GPS serta data mentah terakhir di Redis, jadi lindungi aksesnya sesuai
sensitivitas data lokasi.

Default Streamlit bind ke `DASHBOARD_HOST=127.0.0.1` dan port `DASHBOARD_PORT=7890`. Untuk membuka port pada
semua interface, set `DASHBOARD_HOST=0.0.0.0`, `DASHBOARD_PASSWORD`, `DASHBOARD_TLS_CERT`, dan
`DASHBOARD_TLS_KEY` di `.env`, lalu restart `pocotui-dashboard`. App meminta password dan koneksi memakai HTTPS.
Gunakan password unik; sertifikat self-signed perlu dipercaya secara manual pada perangkat klien. Jangan membuat
aturan firewall yang membuka dashboard ke internet publik. Dashboard memuat koordinat GPS dan data perangkat.
Alternatifnya, gunakan reverse proxy TLS dengan autentikasi.

## Cara kerja hotspot

1. **Interface Wi-Fi induk tidak disentuh.** Dibuat interface virtual `ap0`
   (`iw dev <wifi> interface add ap0 type __ap`,
   MAC berbeda) yang diberi IP gateway (default `192.168.50.1/24`). Saat hotspot dimatikan hanya `ap0` yang dihapus.
   Mode lama (AP langsung di interface Wi-Fi induk) masih ada: Menu > Pengaturan > Hotspot/Wi-Fi > Mode AP.
2. `hostapd` membuat AP di `ap0` (WPA2-PSK CCMP, atau terbuka bila sandi kosong).
3. `dnsmasq` memberi DHCP (`.10–.110`) dan DNS (upstream: Cloudflare/Google/Quad9/AdGuard — pilih di menu).
4. `ip_forward=1` dan firewall **nftables diprioritaskan** (iptables hanya fallback bila `nft` tidak ada).
   Tabel POCO-TUI hanya memuat NAT `postrouting`: masquerade dibatasi ke subnet hotspot dan interface egress
   yang dipilih; mode auto memakai semua interface selain AP agar NAT tetap berlaku saat failover. Aturan
   filter bertanda `comment "pocotui"` ditambahkan ke chain `inet filter` yang ada; hanya aturan bertanda
   tersebut yang dihapus saat stop, sehingga aturan lain dipertahankan. Akses input dari klien hotspot ke
   perangkat diizinkan agar layanan/server lokal dapat dijangkau. Forward dibatasi ke upstream terpilih
   (atau semua interface non-AP pada mode auto); bila NAT dimatikan, forwarding dari klien dijatuhkan.
   Jika chain filter yang diperlukan tidak tersedia atau integrasi gagal, hotspot gagal start dengan error,
   bukan melanjutkan dalam kondisi firewall yang tidak terverifikasi.
5. Trafik dari subnet klien diarahkan memakai `ip rule` prioritas `100` ke tabel route khusus `202`, sebelum
   rule policy seperti Tailscale yang dapat mengambil alih trafik klien; monitor memperbarui default route
   tabel tersebut saat Wi-Fi/LTE failover. NAT dilakukan setelah keputusan routing pada hook `postrouting`.
   Tidak ada rule POCO-TUI di `prerouting` atau `output`: tidak digunakan DNAT atau packet marking untuk routing.
   Saat hotspot dimatikan hanya rule/rute POCO-TUI yang dihapus dan nilai `ip_forward` dikembalikan. Dalam
   mode virtual interface, Wi-Fi induk tetap tersambung seperti semula.

Upstream **auto** memprioritaskan interface Wi-Fi yang tersambung, memiliki default route, dan lolos probe internet
periodik, lalu interface LTE aktif, lalu rute lain. Probe memakai ping ke `1.1.1.1` setiap 20 detik; jika
`ping` tidak tersedia, pemilihan kembali berdasarkan koneksi dan rute saja. Trafik klien hotspot memakai policy
route khusus agar perpindahan upstream tidak mengubah rute layanan/server pada perangkat. Menu Jaringan
menyediakan wizard upstream Wi-Fi dan konfigurasi profil LTE backup. Wi-Fi dari wizard disimpan sebagai profil
NetworkManager dengan auto-connect dan metric lebih rendah; LTE yang dipilih dijalankan saat Wi-Fi upstream
tidak tersedia atau gagal probe, lalu dihentikan saat Wi-Fi pulih. Wi-Fi yang sudah tersimpan di NetworkManager
juga bisa tersambung otomatis sesuai pengaturan profilnya.
Wizard Wi-Fi menjeda AP terlebih dahulu (dengan konfirmasi), memindai SSID, menampilkan kekuatan sinyal dan
keamanan, lalu menyimpan jaringan yang dipilih sebagai profil NetworkManager. Sandi disimpan oleh NetworkManager,
bukan di konfigurasi POCO-TUI. Pilihan **Smart** mempertahankan koneksi saat ini bila trafik sedang tinggi
(≥128 KiB/s) atau sinyalnya masih dalam 15 poin dari sinyal terbaik; selain itu memilih sinyal terkuat.
Pemilihan kandidat baru hanya dilakukan di wizard (yang memang menjeda AP), bukan diam-diam saat klien sedang
menggunakan hotspot. Setelah tersambung, mode upstream otomatis diaktifkan. Opsi `Kunci BSSID upstream WiFi`
aktif secara default: mengunci profil ke access point yang dipilih agar roaming ke channel lain tidak memicu
perpindahan radio. Matikan opsi ini jika perlu roaming otomatis; kunci profil aktif juga dilepas saat opsi
dimatikan.

Saat upstream Wi-Fi berpindah channel ketika AP aktif, POCO-TUI **tidak me-restart AP otomatis** agar klien
tidak diputus oleh aplikasi. Perubahan dicatat di Log; satu radio tetap membatasi kombinasi channel AP/STA dan
dukungan mengikuti perilaku driver/firmware. Lock BSSID mengurangi roaming tak terduga, tetapi tidak dapat
mencegah perubahan channel yang dilakukan access point itu sendiri.

Shutdown baterai hanya berjalan pada perangkat nyata ketika sampel baterai masih baru, statusnya `Discharging`,
dan kapasitas ≤15% tanpa charger terdeteksi. Kondisi harus bertahan 30 detik lalu memberi countdown 30 detik;
charger terpasang atau kondisi baterai pulih membatalkan countdown. Hotspot idle dihitung hanya dari sampel
hostapd yang valid dan segar; jika tidak ada klien selama 30 menit, AP dimatikan (perangkat tetap menyala).
Kedua otomasi dapat diubah dari Menu > Sistem. Mode `--demo` tidak pernah menjalankan shutdown atau mematikan AP
otomatis.

Untuk aktivasi LTE otomatis, modem harus sudah dikenali NetworkManager/ModemManager, profil GSM dan APN valid,
serta modem memiliki layanan data. Wizard dapat memilih profil GSM yang ada atau membuat profil baru dengan APN.
Interface LTE berbeda antar-kernel/operator (`rmnet_data0`, `rmnet_ipa0`, `wwan0`, dll.); interface LTE yang
sudah mempunyai rute default tetap dapat dipakai otomatis meski tidak dikontrol NetworkManager.

Konfigurasi di `/etc/pocotui/config.json` (mode 600). **Sandi WPA2 acak 10 karakter dibuat otomatis pada
start pertama** — lihat/ubah di Menu. Jangan memakai sandi bawaan yang umum.

## Batasan `ap0` (AP + klien di satu chip)

- WiFi Poco F1 (WCN3990/ath10k) hanya punya **satu radio**: bila interface Wi-Fi induk sedang tersambung ke WiFi lain,
  `ap0` **dipaksa memakai channel/band yang sama** dengan `wlan0`. TUI mendeteksinya otomatis dan menampilkan
  "(ikut wlan0)" di panel Radio; pengaturan Band/Channel hanya berlaku saat interface induk tidak tersambung.
- Wizard Wi-Fi akan meminta izin menjeda hotspot sebelum scan; klien terputus selama jeda. Bila upstream Wi-Fi
  berpindah channel saat hotspot hidup, TUI hanya mencatat peringatan dan tidak me-restart AP otomatis.
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
- **Wi-Fi / `wlan0` tidak terdeteksi** — aplikasi mencari interface lewat sysfs dan `iw dev`, lalu mencoba
  membuka rfkill sebelum menunggu hingga 10 detik. Nama tidak harus `wlan0`; pilih interface yang terdeteksi
  di Pengaturan > Hotspot/Wi-Fi > Interface WiFi. Lihat Sistem > Info perangkat (Wi-Fi/rfkill) dan Log.
  Jika daftar kosong, periksa driver/firmware, airplane mode, `rfkill list wifi`, dan `iw dev`.
- **LTE tidak menjadi backup** — pastikan memakai upstream `auto`, buat/pilih profil GSM di Jaringan >
  Atur LTE sebagai backup, masukkan APN operator yang benar, dan pastikan ModemManager serta layanan data aktif.
  Failover hanya berjalan saat hotspot TUI hidup; status perpindahan tercatat di Log.
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
