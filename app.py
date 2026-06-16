#!/usr/bin/env python3
"""One-command Local Disk Cleaner bootstrap app.

Runs scan -> analyze -> report, then serves the interactive report locally.
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
import webbrowser
from pathlib import Path

try:
    from tools import analyzer, report, scanner, server
except ModuleNotFoundError:
    import analyzer
    import report
    import scanner
    import server


def build_outputs(args: argparse.Namespace) -> tuple[dict, dict, str]:
    targets = args.target if args.target else scanner.default_targets()
    min_bytes = args.min_bytes if args.min_bytes is not None else args.min_mb * 1024 * 1024
    scan_result = scanner.scan(targets, min_bytes=min_bytes, limit=args.limit, workers=args.workers)
    analysis_result = analyzer.analyze(scan_result)
    html = report.render_html(analysis_result, analysis_file=args.analysis_output)

    Path(args.scan_output).write_text(
        json.dumps(scan_result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    Path(args.analysis_output).write_text(
        json.dumps(analysis_result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    Path(args.report_output).write_text(html, encoding="utf-8")
    return scan_result, analysis_result, html


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Local Disk Cleaner bootstrap app")
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
    parser.add_argument("--no-serve", action="store_true", help="Generate files only; do not start local server.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _scan_result, analysis_result, _html = build_outputs(args)

    if args.no_serve:
        print(args.report_output)
        return 0

    token = secrets.token_urlsafe(24)
    app = server.ReportServer(analysis_result, args.analysis_output, token)
    httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(app))
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    print(url, flush=True)
    if not args.no_open:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
