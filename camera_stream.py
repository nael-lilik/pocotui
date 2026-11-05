#!/usr/bin/env python3
"""On-demand V4L2 MJPEG camera stream for POCO-TUI."""

import base64
import hmac
import http.server
import ipaddress
import json
import os
import select
import shutil
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

import pocotui


pocotui.load_project_env()
CAPTURE_LOCK = threading.Lock()
BOUNDARY = "pocotui-frame"


def camera_config():
    try:
        width = int(os.environ.get("CAMERA_WIDTH", "640"))
        height = int(os.environ.get("CAMERA_HEIGHT", "480"))
        fps = int(os.environ.get("CAMERA_FPS", "8"))
        quality = int(os.environ.get("CAMERA_QUALITY", "8"))
    except ValueError as error:
        raise ValueError("konfigurasi ukuran, FPS, atau kualitas kamera harus berupa angka") from error
    if not 160 <= width <= 1920 or not 120 <= height <= 1080:
        raise ValueError("resolusi kamera harus antara 160x120 dan 1920x1080")
    if not 1 <= fps <= 15:
        raise ValueError("CAMERA_FPS harus antara 1-15 untuk membatasi beban perangkat")
    if not 2 <= quality <= 31:
        raise ValueError("CAMERA_QUALITY harus antara 2-31")
    return {
        "host": os.environ.get("CAMERA_HOST", "127.0.0.1"),
        "port": int(os.environ.get("CAMERA_PORT", "8090")),
        "device": os.environ.get("CAMERA_DEVICE", "/dev/video0"),
        "input_format": os.environ.get("CAMERA_INPUT_FORMAT", "").strip(),
        "width": width,
        "height": height,
        "fps": fps,
        "quality": quality,
        "password": os.environ.get("CAMERA_PASSWORD", ""),
        "cert": os.environ.get("CAMERA_TLS_CERT", ""),
        "key": os.environ.get("CAMERA_TLS_KEY", ""),
    }


def ffmpeg_command(config):
    executable = shutil.which("ffmpeg")
    if not executable:
        raise RuntimeError("ffmpeg tidak ditemukan")
    if not os.path.exists(config["device"]):
        raise RuntimeError("device kamera tidak ditemukan: %s" % config["device"])
    args = [
        executable, "-nostdin", "-hide_banner", "-loglevel", "error",
        "-f", "v4l2", "-framerate", str(config["fps"]),
    ]
    if config["input_format"]:
        args.extend(["-input_format", config["input_format"]])
    args.extend([
        "-video_size", "%dx%d" % (config["width"], config["height"]),
        "-i", config["device"],
        "-an", "-vf",
        "scale=%d:%d:force_original_aspect_ratio=decrease,pad=%d:%d:(ow-iw)/2:(oh-ih)/2"
        % (config["width"], config["height"], config["width"], config["height"]),
        "-q:v", str(config["quality"]), "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1",
    ])
    return args


def read_jpeg_frame(process, buffer, timeout):
    deadline = time.monotonic() + timeout
    fd = process.stdout.fileno()
    while time.monotonic() < deadline:
        start = buffer.find(b"\xff\xd8")
        if start >= 0:
            end = buffer.find(b"\xff\xd9", start + 2)
            if end >= 0:
                frame = bytes(buffer[start:end + 2])
                del buffer[:end + 2]
                return frame
            if start:
                del buffer[:start]
        else:
            buffer.clear()
        if process.poll() is not None:
            return None
        ready, _, _ = select.select([fd], [], [], min(0.5, max(0, deadline - time.monotonic())))
        if ready:
            chunk = os.read(fd, 65536)
            if not chunk:
                return None
            buffer.extend(chunk)
    return None


def stop_capture(process):
    if process and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)
    if process and process.stdout:
        process.stdout.close()


class CameraServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def handler_for(config):
    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "POCO-TUI-Camera"

        def log_message(self, fmt, *args):
            sys.stderr.write("%s - %s\n" % (self.log_date_time_string(), fmt % args))

        def authorized(self):
            password = config["password"]
            if not password:
                return True
            try:
                scheme, encoded = self.headers.get("Authorization", "").split(" ", 1)
                user, supplied = base64.b64decode(encoded, validate=True).decode().split(":", 1)
            except (ValueError, UnicodeDecodeError):
                scheme, user, supplied = "", "", ""
            if (scheme.lower() == "basic" and hmac.compare_digest(user, "camera")
                    and hmac.compare_digest(supplied, password)):
                return True
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="POCO-TUI Camera", charset="UTF-8"')
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return False

        def do_GET(self):
            path = urllib.parse.urlsplit(self.path).path
            if path == "/health":
                self.send_json({"ok": True})
                return
            if path not in ("/", "/stream.mjpg", "/snapshot.jpg"):
                self.send_error(404)
                return
            if not self.authorized():
                return
            if path == "/":
                self.send_html()
            elif path == "/snapshot.jpg":
                self.send_snapshot()
            else:
                self.send_stream()

        def send_json(self, payload):
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def send_html(self):
            title = "POCO-TUI Portable Camera"
            body = (
                "<!doctype html><html lang='id'><meta charset='utf-8'>"
                "<meta name='viewport' content='width=device-width,initial-scale=1'>"
                "<title>%s</title><style>body{margin:0;padding:16px;background:#07111c;"
                "color:#e6f0f7;font:16px system-ui}main{max-width:1000px;margin:auto}"
                "img{display:block;width:100%%;height:auto;border-radius:12px;background:#0c1926}"
                "a{color:#51e0d0}</style><main><h1>%s</h1><p>Live MJPEG · %dx%d · %d FPS</p>"
                "<img src='/stream.mjpg' alt='Live camera stream'>"
                "<p><a href='/snapshot.jpg'>Ambil snapshot</a> · "
                "Stream hanya aktif selama ada viewer; tidak merekam video ke disk.</p></main></html>"
            ) % (title, title, config["width"], config["height"], config["fps"])
            data = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def start_capture(self):
            try:
                command = ffmpeg_command(config)
                return subprocess.Popen(
                    command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                    stderr=None, close_fds=True, start_new_session=True,
                )
            except (OSError, RuntimeError) as error:
                sys.stderr.write("Camera capture failed: %s\n" % error)
                return None

        def send_snapshot(self):
            if not CAPTURE_LOCK.acquire(blocking=False):
                self.send_error(503, "Kamera sedang digunakan oleh viewer lain")
                return
            process = self.start_capture()
            try:
                if not process:
                    self.send_error(503, "Kamera tidak dapat dimulai")
                    return
                frame = read_jpeg_frame(process, bytearray(), 8)
                if not frame:
                    self.send_error(503, "Tidak ada frame; periksa driver dan media pipeline V4L2")
                    return
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(frame)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(frame)
            except (BrokenPipeError, ConnectionResetError):
                return
            finally:
                stop_capture(process)
                CAPTURE_LOCK.release()

        def send_stream(self):
            if not CAPTURE_LOCK.acquire(blocking=False):
                self.send_error(503, "Kamera hanya mendukung satu viewer aktif")
                return
            process = self.start_capture()
            try:
                if not process:
                    self.send_error(503, "Kamera tidak dapat dimulai")
                    return
                buffer = bytearray()
                frame = read_jpeg_frame(process, buffer, 8)
                if not frame:
                    self.send_error(503, "Tidak ada frame; periksa driver dan media pipeline V4L2")
                    return
                self.send_response(200)
                self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=%s" % BOUNDARY)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.end_headers()
                while frame:
                    self.wfile.write(
                        ("--%s\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n"
                         % (BOUNDARY, len(frame))).encode() + frame + b"\r\n"
                    )
                    self.wfile.flush()
                    frame = read_jpeg_frame(process, buffer, 10)
            except (BrokenPipeError, ConnectionResetError, OSError):
                return
            finally:
                stop_capture(process)
                CAPTURE_LOCK.release()

    return Handler


def main():
    config = camera_config()
    if not 1 <= config["port"] <= 65535:
        raise ValueError("CAMERA_PORT harus berada pada rentang 1-65535")
    try:
        remote = not ipaddress.ip_address(config["host"]).is_loopback
    except ValueError:
        remote = config["host"].lower() != "localhost"
    if remote and (not config["password"] or not config["cert"] or not config["key"]):
        raise ValueError("bind kamera non-localhost memerlukan CAMERA_PASSWORD dan sertifikat TLS")
    if bool(config["cert"]) != bool(config["key"]):
        raise ValueError("CAMERA_TLS_CERT dan CAMERA_TLS_KEY harus diatur bersama")

    server = CameraServer((config["host"], config["port"]), handler_for(config))
    if config["cert"]:
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(config["cert"], config["key"])
        server.socket = tls.wrap_socket(server.socket, server_side=True)
    pid_path = os.environ.get(
        "CAMERA_PID_FILE",
        os.path.join(tempfile.gettempdir(), "pocotui-camera-%d.pid" % os.getuid()),
    )
    os.makedirs(os.path.dirname(pid_path), exist_ok=True)
    with open(pid_path, "w", encoding="ascii") as pid_file:
        pid_file.write(str(os.getpid()))

    def stop_server(_signum, _frame):
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop_server)
    signal.signal(signal.SIGINT, stop_server)
    print("POCO-TUI camera listening on %s:%d" % (config["host"], config["port"]), flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        try:
            if open(pid_path, encoding="ascii").read().strip() == str(os.getpid()):
                os.remove(pid_path)
        except OSError:
            pass


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError) as error:
        print("POCO-TUI camera: %s" % error, file=sys.stderr)
        sys.exit(1)
