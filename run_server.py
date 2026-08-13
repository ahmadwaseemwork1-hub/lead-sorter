"""Start the Leads Sorter portal for the whole local network.

    python run_server.py

Reads server_config.json (host, port, passphrase, retention_hours,
max_upload_mb), prints the URLs colleagues should open, falls back to the
next free port if the configured one is taken, and purges old job files
in the background. Works fully offline — no CDN or cloud calls anywhere.
"""

import os
import socket
import threading
import time

from app import BASE, OUTPUT, UPLOADS, app, load_server_config


def lan_ip():
    """Best-effort LAN address (no packets are actually sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.168.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "127.0.0.1"
    finally:
        s.close()


def _port_busy(host, port):
    probe = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex((probe, port)) == 0  # something answered -> busy


def pick_port(host, wanted, tries=20):
    for port in range(wanted, wanted + tries):
        if not _port_busy(host, port):
            return port
    raise SystemExit(f"No free port found in {wanted}-{wanted + tries - 1}.")


def retention_sweeper(hours):
    """Delete job files older than the retention period, once an hour."""
    def sweep():
        while True:
            cutoff = time.time() - hours * 3600
            for folder in (UPLOADS, OUTPUT):
                if not os.path.isdir(folder):
                    continue
                for name in os.listdir(folder):
                    path = os.path.join(folder, name)
                    try:
                        if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                            os.remove(path)
                    except OSError:
                        pass
            time.sleep(3600)
    t = threading.Thread(target=sweep, daemon=True)
    t.start()


def main():
    cfg = load_server_config()
    host = cfg.get("host", "0.0.0.0")
    port = pick_port(host, int(cfg.get("port", 8080)))
    if port != int(cfg.get("port", 8080)):
        print(f"Port {cfg['port']} is busy - using {port} instead.")

    retention_sweeper(float(cfg.get("retention_hours", 24)))

    ip = lan_ip()
    print()
    print("=" * 58)
    print("  Leads Sorter is running.")
    print()
    print(f"  On this machine:      http://127.0.0.1:{port}")
    if host in ("0.0.0.0", "::", ip):
        print(f"  Colleagues (same network): http://{ip}:{port}")
    if cfg.get("passphrase"):
        print("  Passphrase protection: ON (see server_config.json)")
    print()
    print("  See SHARING.md for firewall/connection help.")
    print("  Press Ctrl+C to stop.")
    print("=" * 58)
    print()
    app.run(host=host, port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
