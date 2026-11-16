from __future__ import annotations

import contextlib
import subprocess
import threading
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

HttpResponder = Callable[[str, dict[str, str]], tuple[int, bytes, str]]


@contextlib.contextmanager
def local_http_server(respond: HttpResponder) -> Iterator[str]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            headers = {k.lower(): v for k, v in self.headers.items()}
            status, body, content_type = respond(self.path, headers)
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


def generate_signing_key(tmp: Path) -> tuple[Path, str]:
    key = tmp / "id_ed25519"
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
        check=True,
    )
    pub = key.with_suffix(".pub").read_text(encoding="utf-8").strip()
    return key, pub


def sign_script(key: Path, script: Path) -> bytes:
    subprocess.run(
        ["ssh-keygen", "-q", "-Y", "sign", "-f", str(key), "-n", "claude-env", str(script)],
        check=True,
    )
    return script.with_name("bootstrap.py.sig").read_bytes()
