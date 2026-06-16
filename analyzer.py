#!/usr/bin/env python3
"""Rule-driven ScanResult -> AnalysisResult classifier.

V1 is deliberately model-free. This module decides cleanup permissions using
local rules only. The UI should consume action IDs from this output instead of
sending arbitrary file paths back to the backend.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path


Tier = str


@dataclass(frozen=True)
class Rule:
    id: str
    tier: Tier
    name: str
    patterns: tuple[str, ...]
    description: str
    close_apps_hint: tuple[str, ...] = ()
    manual_hint: str = ""


GREEN_RULES = (
    Rule(
        id="green_temp_children",
        tier="green",
        name="临时文件",
        patterns=("%TEMP%\\*",),
        description="应用和安装程序创建的临时文件。",
        close_apps_hint=("apps using temporary installers",),
    ),
    Rule(
        id="green_nvidia_dxcache",
        tier="green",
        name="NVIDIA DirectX 着色器缓存",
        patterns=("%LOCALAPPDATA%\\NVIDIA\\DXCache",),
        description="游戏和图形应用使用的着色器缓存，可由程序重新生成。",
        close_apps_hint=("games", "NVIDIA apps"),
    ),
    Rule(
        id="green_nvidia_glcache",
        tier="green",
        name="NVIDIA OpenGL 着色器缓存",
        patterns=("%LOCALAPPDATA%\\NVIDIA\\GLCache",),
        description="OpenGL 着色器缓存，可由程序重新生成。",
        close_apps_hint=("games", "NVIDIA apps"),
    ),
    Rule(
        id="green_pip_cache",
        tier="green",
        name="pip 下载缓存",
        patterns=("%LOCALAPPDATA%\\pip\\Cache",),
        description="Python 包下载缓存，需要时可以重新下载。",
        close_apps_hint=("python", "pip"),
    ),
    Rule(
        id="green_npm_cache",
        tier="green",
        name="npm 缓存",
        patterns=("%LOCALAPPDATA%\\npm-cache\\_cacache", "%LOCALAPPDATA%\\npm-cache\\_npx"),
        description="npm 和 npx 包缓存，内容可重新生成。",
        close_apps_hint=("node", "npm", "npx"),
    ),
    Rule(
        id="green_ms_playwright",
        tier="green",
        name="Playwright 浏览器运行时缓存",
        patterns=("%LOCALAPPDATA%\\ms-playwright",),
        description="Playwright 下载的浏览器运行时。",
        close_apps_hint=("node", "playwright"),
    ),
    Rule(
        id="green_vscode_cpptools_ipch",
        tier="green",
        name="VS Code C/C++ IntelliSense 缓存",
        patterns=("%LOCALAPPDATA%\\Microsoft\\vscode-cpptools\\ipch",),
        description="VS Code C/C++ 扩展生成的 IntelliSense 数据库缓存。",
        close_apps_hint=("Code",),
    ),
    Rule(
        id="green_edge_cache",
        tier="green",
        name="Microsoft Edge 缓存",
        patterns=(
            "%LOCALAPPDATA%\\Microsoft\\Edge\\User Data\\*\\Cache",
            "%LOCALAPPDATA%\\Microsoft\\Edge\\User Data\\*\\Code Cache",
        ),
        description="Edge 浏览器缓存，可由浏览器重新生成。",
        close_apps_hint=("msedge",),
    ),
    Rule(
        id="green_chrome_cache",
        tier="green",
        name="Google Chrome 缓存",
        patterns=(
            "%LOCALAPPDATA%\\Google\\Chrome\\User Data\\*\\Cache",
            "%LOCALAPPDATA%\\Google\\Chrome\\User Data\\*\\Code Cache",
        ),
        description="Chrome 浏览器缓存，可由浏览器重新生成。",
        close_apps_hint=("chrome",),
    ),
)


YELLOW_RULES = (
    Rule(
        id="yellow_desktop",
        tier="yellow",
        name="桌面文件",
        patterns=("%USERPROFILE%\\Desktop", "%USERPROFILE%\\Desktop\\*"),
        description="桌面上的用户文件。",
        manual_hint="请打开后人工检查。建议先把归档资料移动到其他磁盘，再删除不需要的文件。",
    ),
    Rule(
        id="yellow_downloads",
        tier="yellow",
        name="下载目录",
        patterns=("%USERPROFILE%\\Downloads", "%USERPROFILE%\\Downloads\\*"),
        description="下载的安装包、压缩包和用户文件。",
        manual_hint="请人工检查。旧安装包确认不再需要后再删除。",
    ),
    Rule(
        id="yellow_documents",
        tier="yellow",
        name="文档目录",
        patterns=("%USERPROFILE%\\Documents", "%USERPROFILE%\\Documents\\*"),
        description="用户文档和应用托管内容。",
        manual_hint="请人工检查。聊天文件、云文档和项目资料可能包含重要数据。",
    ),
    Rule(
        id="yellow_tencent",
        tier="yellow",
        name="腾讯 / 微信数据",
        patterns=(
            "%USERPROFILE%\\Documents\\Tencent Files",
            "%USERPROFILE%\\Documents\\Tencent Files\\*",
            "%USERPROFILE%\\Documents\\WeChat Files",
            "%USERPROFILE%\\Documents\\WeChat Files\\*",
            "%APPDATA%\\Tencent",
            "%APPDATA%\\Tencent\\*",
        ),
        description="聊天文件、接收媒体、缓存和登录数据。",
        manual_hint="优先从微信/QQ 设置中清理。不要直接删除账号数据库。",
    ),
    Rule(
        id="yellow_kingsoft",
        tier="yellow",
        name="WPS / 金山数据",
        patterns=("%APPDATA%\\kingsoft", "%APPDATA%\\kingsoft\\*"),
        description="WPS 账号、云文档缓存、模板和应用数据。",
        manual_hint="优先使用 WPS 内置清理，并确认云同步完成后再处理本地文件。",
    ),
    Rule(
        id="yellow_global_npm",
        tier="yellow",
        name="npm 全局包",
        patterns=("%APPDATA%\\npm\\node_modules", "%APPDATA%\\npm\\node_modules\\*"),
        description="全局安装的 Node 命令行包。",
        manual_hint="请使用 npm uninstall -g <package> 卸载，不要手动删除 node_modules。",
    ),
)


RED_RULES = (
    Rule(
        id="red_windows",
        tier="red",
        name="Windows 系统文件",
        patterns=("C:\\Windows", "C:\\Windows\\*",),
        description="Windows 系统组件。手动删除可能破坏系统。",
        manual_hint="请使用存储感知、磁盘清理或 DISM 清理。",
    ),
    Rule(
        id="red_program_files",
        tier="red",
        name="已安装应用",
        patterns=("C:\\Program Files", "C:\\Program Files\\*", "C:\\Program Files (x86)", "C:\\Program Files (x86)\\*"),
        description="已安装应用程序文件。",
        manual_hint="请使用 Windows 设置或应用自带卸载器处理。",
    ),
    Rule(
        id="red_program_data",
        tier="red",
        name="ProgramData 应用数据",
        patterns=("C:\\ProgramData", "C:\\ProgramData\\*"),
        description="全局应用数据、驱动、安装缓存和许可证数据。",
        manual_hint="请通过所属应用或卸载器清理。",
    ),
    Rule(
        id="red_system_files",
        tier="red",
        name="系统托管文件",
        patterns=("C:\\pagefile.sys", "C:\\hiberfil.sys", "C:\\System Volume Information", "C:\\System Volume Information\\*"),
        description="系统托管文件。",
        manual_hint="请通过 Windows 设置调整，不要手动删除。",
    ),
)


NEVER_DELETE_PATTERNS = tuple(pattern for rule in RED_RULES for pattern in rule.patterns) + (
    "C:\\",
    "D:\\",
    "E:\\",
)


def norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(os.path.expandvars(path)))


def expand_pattern(pattern: str) -> str:
    return norm(os.path.expandvars(pattern))


def path_matches(path: str, pattern: str) -> bool:
    return fnmatch.fnmatchcase(norm(path), expand_pattern(pattern))


def find_rule(path: str, rules: tuple[Rule, ...]) -> Rule | None:
    for rule in rules:
        if any(path_matches(path, pattern) for pattern in rule.patterns):
            return rule
    return None


def is_under(path: str, root: str) -> bool:
    candidate = norm(path)
    base = norm(root)
    return candidate == base or candidate.startswith(base.rstrip("\\/") + os.sep)


def allowed_cleanup_roots() -> list[str]:
    roots = []
    for key in ("USERPROFILE", "LOCALAPPDATA", "APPDATA", "TEMP"):
        value = os.environ.get(key)
        if value:
            roots.append(value)
    return roots


def is_never_delete(path: str) -> bool:
    return any(path_matches(path, pattern) for pattern in NEVER_DELETE_PATTERNS)


def is_cleanup_allowed(path: str) -> bool:
    if is_never_delete(path):
        return False
    return any(is_under(path, root) for root in allowed_cleanup_roots())


def alias_path(path: str) -> str:
    aliases = [
        ("USERPROFILE", os.environ.get("USERPROFILE")),
        ("LOCALAPPDATA", os.environ.get("LOCALAPPDATA")),
        ("APPDATA", os.environ.get("APPDATA")),
        ("TEMP", os.environ.get("TEMP")),
    ]
    aliases.sort(key=lambda pair: len(pair[1] or ""), reverse=True)
    normalized = norm(path)
    for name, value in aliases:
        if not value:
            continue
        base = norm(value)
        if normalized == base:
            return f"%{name}%"
        if normalized.startswith(base.rstrip("\\/") + os.sep):
            return f"%{name}%" + path[len(value):]
    return path


def action_id(kind: str, item_id: str) -> str:
    safe = re.sub(r"[^a-z0-9_]+", "_", item_id.lower()).strip("_")
    return f"{kind}_{safe}"


def classify_child(child: dict) -> dict:
    path = child["path"]
    rule = (
        find_rule(path, RED_RULES)
        or find_rule(path, GREEN_RULES)
        or find_rule(path, YELLOW_RULES)
    )

    if rule is None:
        rule = Rule(
            id="blue_other",
            tier="blue",
            name=child.get("name") or Path(path).name,
            patterns=(),
            description="已扫描，但没有自动清理决策。",
            manual_hint="只有在它异常占用空间时才需要人工检查。",
        )

    item_id = f"{rule.id}:{norm(path)}"
    actions = [{"id": action_id("open", item_id), "kind": "open"}]
    if rule.tier == "green" and is_cleanup_allowed(path):
        actions.insert(0, {"id": action_id("recycle", item_id), "kind": "recycle_bin"})

    return {
        "id": action_id("item", item_id),
        "tier": rule.tier,
        "rule_id": rule.id,
        "name": rule.name if rule.id != "blue_other" else child.get("name", rule.name),
        "path": path,
        "path_display": alias_path(path),
        "size_bytes": child.get("size_bytes", 0),
        "description": rule.description,
        "manual_hint": rule.manual_hint,
        "close_apps_hint": list(rule.close_apps_hint),
        "denied_count": child.get("denied_count", 0),
        "locked_count": child.get("locked_count", 0),
        "skipped_reparse_count": child.get("skipped_reparse_count", 0),
        "actions": actions,
    }


def classify_drive_child(child: dict) -> dict:
    item = classify_child(child)
    if item["tier"] != "blue":
        return item

    item_id = f"yellow_drive_item:{norm(child['path'])}"
    actions = [{"id": action_id("open", item_id), "kind": "open"}]
    return {
        "id": action_id("item", item_id),
        "tier": "yellow",
        "rule_id": "yellow_drive_item",
        "name": "磁盘文件",
        "path": child["path"],
        "path_display": alias_path(child["path"]),
        "size_bytes": child.get("size_bytes", 0),
        "description": "所选磁盘中的大文件或文件夹，可能包含个人资料、媒体、项目或备份。",
        "manual_hint": "请打开位置确认内容用途，再决定是否迁移、归档或删除。",
        "close_apps_hint": [],
        "denied_count": child.get("denied_count", 0),
        "locked_count": child.get("locked_count", 0),
        "skipped_reparse_count": child.get("skipped_reparse_count", 0),
        "actions": actions,
    }


def summarize(items: list[dict], system: dict) -> dict:
    tier_bytes = {
        "green": sum(item["size_bytes"] for item in items if item["tier"] == "green"),
        "reviewable": sum(item["size_bytes"] for item in items if item["tier"] == "reviewable"),
        "yellow": sum(item["size_bytes"] for item in items if item["tier"] == "yellow"),
        "red": sum(item["size_bytes"] for item in items if item["tier"] == "red"),
        "blue": sum(item["size_bytes"] for item in items if item["tier"] == "blue"),
    }
    free = None
    disk_name = system.get("disk_name")
    for drive in system.get("drives", []):
        if drive.get("name") == disk_name:
            free = drive.get("free_bytes")
            break

    overview = "规则分析完成。"
    if free is not None:
        overview = (
            f"{disk_name} 当前可用 {free} 字节。"
            f"规则确认可清理的缓存约 {tier_bytes['green']} 字节。"
        )

    return {
        "overview": overview,
        "green_bytes": tier_bytes["green"],
        "reviewable_bytes": tier_bytes["reviewable"],
        "yellow_bytes": tier_bytes["yellow"],
        "red_bytes": tier_bytes["red"],
        "blue_bytes": tier_bytes["blue"],
    }


def analyze(scan_result: dict) -> dict:
    items = []
    for group in scan_result.get("groups", []):
        is_drive_group = str(group.get("group", "")).startswith("drive_")
        for child in group.get("children", []):
            items.append(classify_drive_child(child) if is_drive_group else classify_child(child))
    items.sort(key=lambda item: item["size_bytes"], reverse=True)
    top_consumers = items[:10]

    return {
        "generated_at": scan_result.get("generated_at"),
        "source_scan_seconds": scan_result.get("scan_seconds"),
        "system": scan_result.get("system", {}),
        "summary": summarize(items, scan_result.get("system", {})),
        "top_consumers": top_consumers,
        "items": items,
        "action_policy": {
            "ui_sends_action_ids_only": True,
            "direct_delete_supported": False,
            "cleanup_action_source": "local_rules_only",
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Rule-driven scan analyzer")
    parser.add_argument("scan_json", help="Path to a ScanResult JSON file.")
    parser.add_argument("--output", help="Write AnalysisResult JSON to this file.")
    args = parser.parse_args(argv)

    with open(args.scan_json, "r", encoding="utf-8") as handle:
        scan_result = json.load(handle)

    result = analyze(scan_result)
    blob = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(blob + "\n", encoding="utf-8")
    else:
        print(blob)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
