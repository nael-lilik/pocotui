#!/usr/bin/env python3

import ipaddress
import os
import shutil
import sys

import pocotui


def main():
    pocotui.load_project_env()
    host = os.environ.get("DASHBOARD_HOST", "127.0.0.1")
    port = os.environ.get("DASHBOARD_PORT", "7890")
    try:
        address = ipaddress.ip_address(host)
    except ValueError as error:
        if host.lower() != "localhost":
            raise ValueError("DASHBOARD_HOST harus alamat IP atau localhost") from error
        local_only = True
    else:
        local_only = address.is_loopback
    try:
        port_number = int(port)
    except ValueError as error:
        raise ValueError("DASHBOARD_PORT harus berupa angka 1-65535") from error
    if not 1 <= port_number <= 65535:
        raise ValueError("DASHBOARD_PORT harus berupa angka 1-65535")

    password = os.environ.get("DASHBOARD_PASSWORD", "")
    cert_file = os.environ.get("DASHBOARD_TLS_CERT", "")
    key_file = os.environ.get("DASHBOARD_TLS_KEY", "")
    if not local_only and (not password or not cert_file or not key_file):
        raise ValueError(
            "akses non-localhost memerlukan DASHBOARD_PASSWORD, DASHBOARD_TLS_CERT, dan DASHBOARD_TLS_KEY"
        )
    if bool(cert_file) != bool(key_file):
        raise ValueError("DASHBOARD_TLS_CERT dan DASHBOARD_TLS_KEY harus diatur bersama")
    if cert_file and (not os.path.isfile(cert_file) or not os.path.isfile(key_file)):
        raise ValueError("file sertifikat atau private key dashboard tidak ditemukan")

    streamlit = os.environ.get("POCOTUI_STREAMLIT") or shutil.which("streamlit")
    if not streamlit:
        raise RuntimeError("Streamlit tidak ditemukan; pasang dependency dashboard terlebih dahulu")
    dashboard = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard_streamlit.py")
    arguments = [
        streamlit, "run", dashboard,
        "--server.address", host,
        "--server.port", str(port_number),
        "--server.headless", "true",
        "--browser.gatherUsageStats", "false",
    ]
    if cert_file:
        arguments.extend(["--server.sslCertFile", cert_file, "--server.sslKeyFile", key_file])
    os.execv(streamlit, arguments)


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError) as error:
        print("POCO-TUI dashboard: %s" % error, file=sys.stderr)
        sys.exit(1)
