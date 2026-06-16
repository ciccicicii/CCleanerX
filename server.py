#!/usr/bin/env python3
"""Local interactive report server.

Serves the HTML report and exposes a guarded /action endpoint. The endpoint only
accepts action IDs from the loaded AnalysisResult; it never accepts arbitrary
paths from the browser.
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    from tools import cleanup, report
except ModuleNotFoundError:
    import cleanup
    import report


class ReportServer:
    def __init__(self, analysis_result: dict, analysis_file: str, token: str):
        self.analysis_result = analysis_result
        self.analysis_file = analysis_file
        self.token = token

    def html(self) -> str:
        return report.render_html(
            self.analysis_result,
            analysis_file=self.analysis_file,
            server_config={"endpoint": "/action", "token": self.token},
        )

    def handle_action(self, payload: dict) -> tuple[int, dict]:
        if payload.get("token") != self.token:
            return 403, {"status": "rejected", "error": "token 校验失败"}

        action_id = payload.get("action_id")
        if not isinstance(action_id, str) or not action_id:
            return 400, {"status": "rejected", "error": "缺少 action_id"}

        execute = bool(payload.get("execute", False))
        try:
            action = cleanup.resolve_action(self.analysis_result, action_id)
            result = cleanup.execute(action) if execute else cleanup.dry_run(action)
            return 200, result
        except cleanup.CleanupError as exc:
            return 400, {"status": "rejected", "error": str(exc)}
        except OSError as exc:
            return 500, {"status": "error", "error": str(exc)}


def make_handler(app: ReportServer):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def send_bytes(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def send_json(self, status: int, payload: dict) -> None:
            self.send_bytes(
                status,
                json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                "application/json; charset=utf-8",
            )

        def valid_host(self) -> bool:
            host = (self.headers.get("Host") or "").split(":", 1)[0]
            return host in {"127.0.0.1", "localhost"}

        def do_GET(self):
            if not self.valid_host():
                self.send_json(403, {"status": "rejected", "error": "Host 不被允许"})
                return
            if self.path not in {"/", "/index.html"}:
                self.send_json(404, {"status": "not_found", "error": "页面不存在"})
                return
            self.send_bytes(200, app.html().encode("utf-8"), "text/html; charset=utf-8")

        def do_POST(self):
            if self.path != "/action":
                self.send_json(404, {"status": "not_found", "error": "接口不存在"})
                return
            if not self.valid_host():
                self.send_json(403, {"status": "rejected", "error": "Host 不被允许"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                self.send_json(400, {"status": "rejected", "error": "JSON 格式错误"})
                return
            status, result = app.handle_action(payload)
            self.send_json(status, result)

    return Handler


def load_analysis(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Serve interactive Local Disk Cleaner report")
    parser.add_argument("analysis_json")
    parser.add_argument("--no-open", action="store_true", help="Do not open the browser automatically.")
    args = parser.parse_args(argv)

    token = secrets.token_urlsafe(24)
    app = ReportServer(load_analysis(args.analysis_json), args.analysis_json, token)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(url, flush=True)
    if not args.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
