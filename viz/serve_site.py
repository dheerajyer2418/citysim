"""Serve the pre-built public/ site locally and open it in your browser.

Static preview of exactly what gets deployed to Vercel. Needs only the Python
standard library (no Java, no venv packages). Stop with Ctrl+C.

Usage:
    python viz/serve_site.py
"""

from __future__ import annotations

import functools
import http.server
import socket
import socketserver
import threading
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PUBLIC = ROOT / "public"


def _free_port(start: int = 8000) -> int:
    for port in range(start, start + 50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            if probe.connect_ex(("127.0.0.1", port)) != 0:
                return port
    return start


def main() -> None:
    if not (PUBLIC / "index.html").exists():
        raise SystemExit("public/ is not built yet. Run: python viz/build_site.py")
    port = _free_port(8000)
    url = f"http://127.0.0.1:{port}/"
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(PUBLIC))
    print(f"CitySim website: {url}  (press Ctrl+C to stop)")
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    with socketserver.TCPServer(("127.0.0.1", port), handler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nstopped")


if __name__ == "__main__":
    main()
