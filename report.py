#!/usr/bin/env python3
"""Generate a local interactive HTML report from AnalysisResult."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


HTML_TEMPLATE = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>本地磁盘清理报告</title>
  <style>
    :root {
      --bg: #f6f7f9;
      --card: #ffffff;
      --ink: #20242c;
      --muted: #68707d;
      --line: #dde1e7;
      --green: #14883e;
      --reviewable: #0f766e;
      --yellow: #b86b00;
      --red: #c9342f;
      --blue: #2563eb;
      --radius: 10px;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      padding: 28px;
      background: var(--bg);
      color: var(--ink);
      font: 14px/1.55 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    main { max-width: 1100px; margin: 0 auto; }
    header { margin-bottom: 20px; }
    h1 { margin: 0 0 4px; font-size: 26px; letter-spacing: 0; }
    h2 { margin: 26px 0 12px; font-size: 18px; }
    .muted { color: var(--muted); }
    .grid { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 12px; }
    .section-head {
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      gap: 12px;
      margin: 26px 0 12px;
    }
    .section-head h2 { margin: 0; }
    .card {
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: var(--radius);
      padding: 16px;
    }
    .stat-label { color: var(--muted); font-size: 12px; }
    .stat-value { font-size: 22px; font-weight: 700; margin-top: 4px; }
    .bar {
      height: 16px;
      border-radius: 999px;
      overflow: hidden;
      display: flex;
      background: #e8ebf0;
      border: 1px solid var(--line);
    }
    .seg.green { background: var(--green); }
    .seg.reviewable { background: var(--reviewable); }
    .seg.yellow { background: var(--yellow); }
    .seg.red { background: var(--red); }
    .seg.blue { background: var(--blue); }
    .item {
      border-left: 4px solid var(--blue);
      margin-bottom: 10px;
    }
    .item.green { border-left-color: var(--green); }
    .item.reviewable { border-left-color: var(--reviewable); }
    .item.yellow { border-left-color: var(--yellow); }
    .item.red { border-left-color: var(--red); }
    .item.blue { border-left-color: var(--blue); }
    .item-head {
      display: flex;
      gap: 12px;
      align-items: baseline;
      justify-content: space-between;
      cursor: pointer;
    }
    .item-title { font-weight: 700; }
    .pill {
      display: inline-block;
      padding: 2px 8px;
      border-radius: 999px;
      background: #eef1f5;
      color: var(--muted);
      font-size: 12px;
    }
    .empty { margin-bottom: 10px; }
    .path {
      margin: 10px 0;
      padding: 8px 10px;
      background: #f1f3f6;
      border-radius: 6px;
      font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
      word-break: break-all;
      color: #3d4450;
    }
    .actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 10px; }
    button {
      border: 1px solid var(--line);
      border-radius: 7px;
      background: #fff;
      padding: 7px 10px;
      cursor: pointer;
      color: var(--ink);
    }
    button.primary { background: #e8f1ff; border-color: #bdd3ff; color: #174ea6; }
    button.danger { background: #ffecea; border-color: #ffc5bf; color: var(--red); }
    pre {
      margin: 10px 0 0;
      padding: 10px;
      border-radius: 7px;
      overflow: auto;
      background: #20242c;
      color: #f3f6fb;
      font-size: 12px;
    }
    details > summary { list-style: none; }
    @media (max-width: 800px) {
      body { padding: 16px; }
      .grid { grid-template-columns: 1fr 1fr; }
    }
  </style>
</head>
<body>
<main>
  <header>
    <h1>本地磁盘清理报告</h1>
    <div class="muted" id="meta"></div>
  </header>
  <section class="card">
    <p id="overview"></p>
    <div class="bar" id="tierBar"></div>
  </section>
  <section class="grid" id="stats"></section>
  <div id="tierSections"></div>
</main>
<script>
const DATA = __REPORT_DATA__;
const analysisFile = __ANALYSIS_FILE__;
const SERVER = __SERVER_CONFIG__;
const TIER_ORDER = ["green", "reviewable", "yellow", "red", "blue"];
const TIER_LABELS = {
  green: "可安全清理",
  reviewable: "可清理但需确认",
  yellow: "需要人工判断",
  red: "谨慎处理",
  blue: "其他占用",
};

function bytes(n) {
  const units = ["B", "KB", "MB", "GB", "TB"];
  let v = Number(n || 0);
  let i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return i === 0 ? `${v} ${units[i]}` : `${v.toFixed(1)} ${units[i]}`;
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

function command(actionId) {
  return `python tools\\\\cleanup.py ${analysisFile} ${actionId}`;
}

function tierLabel(tier) {
  return TIER_LABELS[tier] || tier;
}

function renderStats() {
  const s = DATA.summary || {};
  const entries = [
    ["可安全清理", s.green_bytes, "green"],
    ["可清理但需确认", s.reviewable_bytes, "reviewable"],
    ["需要人工判断", s.yellow_bytes, "yellow"],
    ["谨慎处理", s.red_bytes, "red"],
    ["其他占用", s.blue_bytes, "blue"],
  ];
  document.getElementById("stats").innerHTML = entries.map(([label, value, tier]) => `
      <div class="card">
      <div class="stat-label">${esc(label)}</div>
      <div class="stat-value">${bytes(value)}</div>
      <span class="pill">${esc(tierLabel(tier))}</span>
    </div>
  `).join("");
  const total = entries.reduce((sum, [, value]) => sum + Number(value || 0), 0) || 1;
  document.getElementById("tierBar").innerHTML = entries.map(([, value, tier]) => {
    const width = Math.max(0, Number(value || 0) / total * 100);
    return `<div class="seg ${tier}" title="${esc(tierLabel(tier))}: ${bytes(value)}" style="width:${width}%"></div>`;
  }).join("");
}

function renderItem(item) {
  const actions = (item.actions || []).map(action => {
    const cmd = command(action.id);
    const klass = action.kind === "recycle_bin" ? "danger" : "primary";
    const label = action.kind === "recycle_bin" ? "移入回收站" : "打开位置";
    const execute = action.kind === "recycle_bin"
      ? `<button class="danger" onclick="runAction(this, '${esc(action.id)}', true)">移入回收站</button>`
      : `<button class="primary" onclick="runAction(this, '${esc(action.id)}', true)">打开位置</button>`;
    if (SERVER) {
      return `<button class="${klass}" onclick="runAction(this, '${esc(action.id)}', false)">${label}</button>${execute}`;
    }
    return `<button class="${klass}" onclick="showCommand(this, '${esc(cmd)}')">复制${label}命令</button>`;
  }).join("");
  return `
    <details class="card item ${esc(item.tier)}">
      <summary class="item-head">
        <span><span class="item-title">${esc(item.name)}</span> <span class="pill">${esc(tierLabel(item.tier))}</span></span>
        <strong>${bytes(item.size_bytes)}</strong>
      </summary>
      <div class="path">${esc(item.path_display || item.path)}</div>
      <p>${esc(item.description)}</p>
      ${item.manual_hint ? `<p class="muted">${esc(item.manual_hint)}</p>` : ""}
      ${(item.close_apps_hint || []).length ? `<p class="muted">操作前建议关闭：${esc(item.close_apps_hint.join(", "))}</p>` : ""}
      <div class="actions">${actions}</div>
    </details>
  `;
}

function itemsForTier(tier) {
  return (DATA.items || [])
    .filter(item => item.tier === tier)
    .sort((a, b) => Number(b.size_bytes || 0) - Number(a.size_bytes || 0));
}

function totalBytes(items) {
  return items.reduce((sum, item) => sum + Number(item.size_bytes || 0), 0);
}

function renderTierSection(tier) {
  const items = itemsForTier(tier);
  const body = items.length
    ? items.map(renderItem).join("")
    : `<div class="card empty muted">暂无项目</div>`;
  return `
    <section>
      <div class="section-head">
        <h2>${esc(tierLabel(tier))}</h2>
        <span class="muted">${items.length} 项 · ${bytes(totalBytes(items))}</span>
      </div>
      ${body}
    </section>
  `;
}

function showCommand(button, cmd) {
  const parent = button.closest(".item");
  let pre = parent.querySelector("pre");
  if (!pre) {
    pre = document.createElement("pre");
    parent.appendChild(pre);
  }
  pre.textContent = cmd;
  navigator.clipboard?.writeText(cmd).catch(() => {});
}

function showResult(button, result) {
  const parent = button.closest(".item");
  let pre = parent.querySelector("pre");
  if (!pre) {
    pre = document.createElement("pre");
    parent.appendChild(pre);
  }
  pre.textContent = typeof result === "string" ? result : JSON.stringify(result, null, 2);
}

async function runAction(button, actionId, execute) {
  if (!SERVER) return;
  if (execute && !confirm("现在执行这个本地操作？移入回收站在清空回收站前可恢复。")) {
    return;
  }
  button.disabled = true;
  try {
    const response = await fetch(SERVER.endpoint, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({token: SERVER.token, action_id: actionId, execute})
    });
    const result = await response.json();
    showResult(button, result);
  } catch (error) {
    showResult(button, {status: "error", error: String(error)});
  } finally {
    button.disabled = false;
  }
}

document.getElementById("meta").textContent = `生成时间 ${DATA.generated_at || ""}`;
document.getElementById("overview").textContent = DATA.summary?.overview || "分析完成。";
renderStats();
document.getElementById("tierSections").innerHTML = TIER_ORDER.map(renderTierSection).join("");
</script>
</body>
</html>
"""


def render_html(
    analysis_result: dict,
    analysis_file: str = "analysis_result.json",
    server_config: dict | None = None,
) -> str:
    return HTML_TEMPLATE.replace(
        "__REPORT_DATA__", json.dumps(analysis_result, ensure_ascii=False)
    ).replace("__ANALYSIS_FILE__", json.dumps(analysis_file)).replace(
        "__SERVER_CONFIG__", json.dumps(server_config, ensure_ascii=False)
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate Local Disk Cleaner HTML report")
    parser.add_argument("analysis_json", help="Path to AnalysisResult JSON.")
    parser.add_argument("--output", default="report.html", help="Output HTML path.")
    args = parser.parse_args(argv)

    with open(args.analysis_json, "r", encoding="utf-8") as handle:
        analysis_result = json.load(handle)

    html = render_html(analysis_result, analysis_file=args.analysis_json)
    Path(args.output).write_text(html, encoding="utf-8")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
