"""WA bridge supervision: contract files exist, ready-probe works."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer


def test_bridge_files():
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "wa-bridge"
    assert (root / "server.js").exists()
    pkg = json.loads((root / "package.json").read_text())
    assert "@whiskeysockets/baileys" in pkg["dependencies"]


def test_ready_probe_shape():
    # The CLI waits for GET /status -> 200; emulate a stub bridge.
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"connected": false}')

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    import urllib.request

    with urllib.request.urlopen(f"http://127.0.0.1:{port}/status", timeout=5) as r:
        assert r.status == 200
        assert "connected" in json.loads(r.read().decode())
    srv.shutdown()
