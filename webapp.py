#!/usr/bin/env python3
"""Local Web App shell for Local Disk Cleaner."""

from __future__ import annotations

import argparse
import json
import secrets
import sys
import threading
import traceback
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

try:
    from tools import analyzer, cleanup, report, scanner
except ModuleNotFoundError:
    import analyzer
    import cleanup
    import report
    import scanner


APP_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>本地磁盘清理器</title>
  <style>
    :root {
      --bg: #f6f7f9;
      --card: #ffffff;
      --ink: #20242c;
      --muted: #68707d;
      --line: #dde1e7;
      --blue: #2563eb;
      --green: #14883e;
      --red: #c9342f;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      background: var(--bg);
      color: var(--ink);
      font: 14px/1.55 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    main { max-width: 960px; margin: 0 auto; padding: 36px 24px; }
    h1 { margin: 0 0 8px; font-size: 30px; letter-spacing: 0; }
    p { margin: 0; }
    .muted { color: var(--muted); }
    .card {
      margin-top: 22px;
      padding: 20px;
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: 10px;
    }
    .row { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; }
    button, a.button {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 38px;
      border: 1px solid #bdd3ff;
      border-radius: 7px;
      background: #e8f1ff;
      color: #174ea6;
      padding: 8px 12px;
      cursor: pointer;
      text-decoration: none;
      font: inherit;
    }
    button:disabled { cursor: wait; opacity: .65; }
    .status {
      display: inline-block;
      padding: 3px 9px;
      border-radius: 999px;
      background: #eef1f5;
      color: var(--muted);
      font-size: 12px;
    }
    .status.completed { background: #e7f7ee; color: var(--green); }
    .status.failed { background: #ffecea; color: var(--red); }
    pre {
      margin: 14px 0 0;
      white-space: pre-wrap;
      word-break: break-word;
      color: #3d4450;
    }
  </style>
</head>
<body>
<main>
  <h1>本地磁盘清理器</h1>
  <p class="muted">扫描本机磁盘占用，按安全等级生成交互报告。清理动作只使用本地规则生成的 action_id。</p>

  <section class="card">
    <div class="row">
      <button id="scanButton">开始扫描</button>
      <a class="button" href="/report" id="reportLink" hidden>查看报告</a>
      <span class="status" id="state">idle</span>
    </div>
    <pre id="detail">等待开始扫描。</pre>
  </section>

  <section class="card">
    <strong>安全边界</strong>
    <p class="muted">扫描过程只读。只有绿色可安全清理项能移入回收站；黄色、红色和其他项目需要人工判断或只提供打开目录。</p>
  </section>
</main>
<script>
const stateEl = document.getElementById("state");
const detailEl = document.getElementById("detail");
const button = document.getElementById("scanButton");
const reportLink = document.getElementById("reportLink");
let timer = null;

function renderStatus(payload) {
  stateEl.textContent = payload.state;
  stateEl.className = `status ${payload.state}`;
  button.disabled = payload.state === "running";
  reportLink.hidden = !payload.has_report;
  if (payload.state === "completed") {
    const summary = payload.summary || {};
    detailEl.textContent = `扫描完成。共 ${summary.total_items || 0} 个项目，可安全清理 ${summary.green_bytes || 0} 字节。`;
  } else if (payload.state === "failed") {
    detailEl.textContent = payload.error || "扫描失败。";
  } else if (payload.state === "running") {
    detailEl.textContent = payload.message || "正在扫描和分析，请稍候。";
  } else {
    detailEl.textContent = "等待开始扫描。";
  }
}

async function refreshStatus() {
  const response = await fetch("/api/status");
  renderStatus(await response.json());
}

async function startScan() {
  button.disabled = true;
  await fetch("/api/scan", {method: "POST", headers: {"Content-Type": "application/json"}, body: "{}"});
  await refreshStatus();
  clearInterval(timer);
  timer = setInterval(refreshStatus, 1000);
}

button.addEventListener("click", startScan);
refreshStatus();
</script>
</body>
</html>
"""


@dataclass
class ScanJob:
    state: str = "idle"
    message: str = "等待开始扫描。"
    error: str = ""
    scan_result: dict[str, Any] | None = None
    analysis_result: dict[str, Any] | None = None
    html: str = ""
    lock: threading.Lock = field(default_factory=threading.Lock)
    thread: threading.Thread | None = None


class LocalDiskWebApp:
    def __init__(
        self,
        targets: list[scanner.ScanTarget],
        min_bytes: int,
        limit: int,
        scan_output: str,
        analysis_output: str,
        report_output: str,
        auto_open: bool = True,
        workers: int = 1,
    ):
        self.targets = targets
        self.min_bytes = min_bytes
        self.limit = limit
        self.workers = workers
        self.scan_output = scan_output
        self.analysis_output = analysis_output
        self.report_output = report_output
        self.auto_open = auto_open
        self.token = secrets.token_urlsafe(24)
        self.job = ScanJob()

    def status(self) -> dict[str, Any]:
        with self.job.lock:
            return self._status_unlocked()

    def start_scan(self) -> dict[str, Any]:
        with self.job.lock:
            if self.job.state == "running":
                return self._status_unlocked()
            self.job.state = "running"
            self.job.message = "正在扫描和分析，请稍候。"
            self.job.error = ""
            self.job.thread = threading.Thread(target=self._run_scan, daemon=True)
            self.job.thread.start()
            return self._status_unlocked()

    def _status_unlocked(self) -> dict[str, Any]:
        summary = None
        if self.job.analysis_result:
            data = self.job.analysis_result.get("summary", {})
            summary = {
                "green_bytes": data.get("green_bytes", 0),
                "yellow_bytes": data.get("yellow_bytes", 0),
                "red_bytes": data.get("red_bytes", 0),
                "blue_bytes": data.get("blue_bytes", 0),
                "total_items": len(self.job.analysis_result.get("items", [])),
            }
        return {
            "state": self.job.state,
            "message": self.job.message,
            "error": self.job.error,
            "has_report": bool(self.job.html),
            "summary": summary,
        }

    def _run_scan(self) -> None:
        try:
            scan_result = scanner.scan(
                self.targets,
                min_bytes=self.min_bytes,
                limit=self.limit,
                workers=self.workers,
            )
            analysis_result = analyzer.analyze(scan_result)
            html = report.render_html(
                analysis_result,
                analysis_file=self.analysis_output,
                server_config={"endpoint": "/api/action", "token": self.token},
            )
            Path(self.scan_output).write_text(
                json.dumps(scan_result, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            Path(self.analysis_output).write_text(
                json.dumps(analysis_result, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            Path(self.report_output).write_text(html, encoding="utf-8")
            with self.job.lock:
                self.job.scan_result = scan_result
                self.job.analysis_result = analysis_result
                self.job.html = html
                self.job.state = "completed"
                self.job.message = "扫描完成。"
        except Exception as exc:
            with self.job.lock:
                self.job.state = "failed"
                self.job.error = f"{exc}\n{traceback.format_exc()}"
                self.job.message = "扫描失败。"

    def report_html(self) -> str:
        with self.job.lock:
            return self.job.html

    def handle_action(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        if payload.get("token") != self.token:
            return 403, {"status": "rejected", "error": "token 校验失败"}
        with self.job.lock:
            analysis_result = self.job.analysis_result
        if not analysis_result:
            return 409, {"status": "rejected", "error": "请先完成扫描"}
        action_id = payload.get("action_id")
        if not isinstance(action_id, str) or not action_id:
            return 400, {"status": "rejected", "error": "缺少 action_id"}
        execute = bool(payload.get("execute", False))
        try:
            action = cleanup.resolve_action(analysis_result, action_id)
            result = cleanup.execute(action) if execute else cleanup.dry_run(action)
            return 200, result
        except cleanup.CleanupError as exc:
            return 400, {"status": "rejected", "error": str(exc)}
        except OSError as exc:
            return 500, {"status": "error", "error": str(exc)}


def make_handler(app: LocalDiskWebApp):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def valid_host(self) -> bool:
            host = (self.headers.get("Host") or "").split(":", 1)[0]
            return host in {"127.0.0.1", "localhost"}

        def send_bytes(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def send_json(self, status: int, payload: dict[str, Any]) -> None:
            self.send_bytes(
                status,
                json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                "application/json; charset=utf-8",
            )

        def read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0"))
            return json.loads(self.rfile.read(length) or b"{}")

        def do_GET(self):
            if not self.valid_host():
                self.send_json(403, {"status": "rejected", "error": "Host 不被允许"})
                return
            if self.path in {"/", "/index.html"}:
                self.send_bytes(200, APP_HTML.encode("utf-8"), "text/html; charset=utf-8")
                return
            if self.path == "/api/status":
                self.send_json(200, app.status())
                return
            if self.path == "/report":
                html = app.report_html()
                if not html:
                    self.send_json(404, {"status": "not_found", "error": "报告尚未生成"})
                    return
                self.send_bytes(200, html.encode("utf-8"), "text/html; charset=utf-8")
                return
            self.send_json(404, {"status": "not_found", "error": "页面不存在"})

        def do_POST(self):
            if not self.valid_host():
                self.send_json(403, {"status": "rejected", "error": "Host 不被允许"})
                return
            try:
                payload = self.read_json()
            except Exception:
                self.send_json(400, {"status": "rejected", "error": "JSON 格式错误"})
                return
            if self.path == "/api/scan":
                self.send_json(202, app.start_scan())
                return
            if self.path == "/api/action":
                status, result = app.handle_action(payload)
                self.send_json(status, result)
                return
            self.send_json(404, {"status": "not_found", "error": "接口不存在"})

    return Handler


def create_server(app: LocalDiskWebApp) -> ThreadingHTTPServer:
    return ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Local Disk Cleaner local Web App")
    parser.add_argument(
        "--target",
        action="append",
        type=scanner.parse_target,
        help="Scan a custom target instead of defaults, formatted name=path. Can repeat.",
    )
    parser.add_argument("--min-mb", type=scanner.positive_int, default=50)
    parser.add_argument("--min-bytes", type=scanner.nonnegative_int)
    parser.add_argument("--limit", type=scanner.positive_int, default=scanner.DEFAULT_CHILD_LIMIT)
    parser.add_argument("--workers", type=scanner.positive_int, default=scanner.default_worker_count())
    parser.add_argument("--scan-output", default="scan_result.json")
    parser.add_argument("--analysis-output", default="analysis_result.json")
    parser.add_argument("--report-output", default="report.html")
    parser.add_argument("--no-open", action="store_true", help="Do not open the browser automatically.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    targets = args.target if args.target else scanner.default_targets()
    min_bytes = args.min_bytes if args.min_bytes is not None else args.min_mb * 1024 * 1024
    app = LocalDiskWebApp(
        targets=targets,
        min_bytes=min_bytes,
        limit=args.limit,
        workers=args.workers,
        scan_output=args.scan_output,
        analysis_output=args.analysis_output,
        report_output=args.report_output,
        auto_open=not args.no_open,
    )
    server = create_server(app)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(url, flush=True)
    if app.auto_open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
