#!/usr/bin/env python3
"""Cleanup action resolver and dry-run safety layer.

V1 UI must send action IDs, not paths. This module resolves an action ID from an
AnalysisResult and validates it against local safety rules before any real
cleanup implementation exists.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from typing import Callable

try:
    from tools import analyzer
except ModuleNotFoundError:
    import analyzer


class CleanupError(ValueError):
    pass


@dataclass(frozen=True)
class ResolvedAction:
    action_id: str
    kind: str
    item_id: str
    tier: str
    path: str
    path_display: str
    size_bytes: int


@dataclass
class CleanupResult:
    action_id: str
    kind: str
    status: str
    path: str
    path_display: str
    tier: str
    estimated_recovered_bytes: int
    moved_count: int = 0
    skipped_missing_count: int = 0
    skipped_locked_count: int = 0
    skipped_denied_count: int = 0
    message: str = ""

    def to_dict(self) -> dict:
        return {
            "action_id": self.action_id,
            "kind": self.kind,
            "status": self.status,
            "path": self.path,
            "path_display": self.path_display,
            "tier": self.tier,
            "estimated_recovered_bytes": self.estimated_recovered_bytes,
            "moved_count": self.moved_count,
            "skipped_missing_count": self.skipped_missing_count,
            "skipped_locked_count": self.skipped_locked_count,
            "skipped_denied_count": self.skipped_denied_count,
            "message": self.message,
        }


def iter_item_actions(analysis_result: dict):
    for item in analysis_result.get("items", []):
        for action in item.get("actions", []):
            yield item, action


def resolve_action(analysis_result: dict, action_id: str) -> ResolvedAction:
    for item, action in iter_item_actions(analysis_result):
        if action.get("id") != action_id:
            continue

        resolved = ResolvedAction(
            action_id=action_id,
            kind=action.get("kind", ""),
            item_id=item.get("id", ""),
            tier=item.get("tier", ""),
            path=item.get("path", ""),
            path_display=item.get("path_display", item.get("path", "")),
            size_bytes=int(item.get("size_bytes", 0) or 0),
        )
        validate_resolved_action(resolved)
        return resolved

    raise CleanupError(f"未知 action_id：{action_id}")


def validate_resolved_action(action: ResolvedAction) -> None:
    if not action.path:
        raise CleanupError("action 缺少路径")

    if action.kind == "open":
        validate_open_action(action)
        return

    if action.kind == "recycle_bin":
        validate_recycle_action(action)
        return

    raise CleanupError(f"不支持的 action 类型：{action.kind}")


def validate_open_action(action: ResolvedAction) -> None:
    if analyzer.is_never_delete(action.path) and action.path.rstrip("\\/") in {"C:", "D:", "E:"}:
        raise CleanupError("拒绝打开磁盘根目录 action")


def validate_recycle_action(action: ResolvedAction) -> None:
    if action.tier != "green":
        raise CleanupError("只有绿色项目可以移入回收站")
    if analyzer.is_never_delete(action.path):
        raise CleanupError("路径受 never-delete 规则保护")
    if not analyzer.is_cleanup_allowed(action.path):
        raise CleanupError("路径不在允许清理的根目录内")


def dry_run(action: ResolvedAction) -> dict:
    return CleanupResult(
        action_id=action.action_id,
        kind=action.kind,
        status="dry_run",
        path=action.path,
        path_display=action.path_display,
        tier=action.tier,
        estimated_recovered_bytes=action.size_bytes if action.kind == "recycle_bin" else 0,
        message=dry_run_message(action),
    ).to_dict()


def dry_run_message(action: ResolvedAction) -> str:
    if action.kind == "recycle_bin":
        return "试运行通过。这个绿色项目会被移入回收站。"
    if action.kind == "open":
        return "试运行通过。这个路径会在资源管理器中打开。"
    return "试运行通过。"


def is_locked_error(exc: OSError) -> bool:
    return getattr(exc, "winerror", None) == 32


def is_denied_error(exc: OSError) -> bool:
    return isinstance(exc, PermissionError) or getattr(exc, "winerror", None) == 5


def move_to_recycle_bin(path: str) -> None:
    if sys.platform.startswith("win"):
        move_to_recycle_bin_windows(path)
        return
    raise CleanupError("移入回收站当前仅支持 Windows")


def move_to_recycle_bin_windows(path: str) -> None:
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [
            ("hwnd", wintypes.HWND),
            ("wFunc", wintypes.UINT),
            ("pFrom", wintypes.LPCWSTR),
            ("pTo", wintypes.LPCWSTR),
            ("fFlags", ctypes.c_uint16),
            ("fAnyOperationsAborted", wintypes.BOOL),
            ("hNameMappings", ctypes.c_void_p),
            ("lpszProgressTitle", wintypes.LPCWSTR),
        ]

    fo_delete = 3
    fof_allowundo = 0x0040
    fof_noconfirmation = 0x0010
    fof_silent = 0x0004

    op = SHFILEOPSTRUCTW()
    op.wFunc = fo_delete
    op.pFrom = path + "\x00\x00"
    op.fFlags = fof_allowundo | fof_noconfirmation | fof_silent
    rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if rc != 0:
        raise OSError(rc, f"SHFileOperationW failed with code {rc}", path)
    if op.fAnyOperationsAborted:
        raise OSError("移入回收站操作已中止")


def open_in_explorer(path: str) -> None:
    if sys.platform.startswith("win"):
        if os.path.isdir(path):
            subprocess.Popen(["explorer", path])
        else:
            subprocess.Popen(["explorer", f"/select,{path}"])
        return
    raise CleanupError("在文件管理器中打开当前仅支持 Windows")


def execute(
    action: ResolvedAction,
    mover: Callable[[str], None] = move_to_recycle_bin,
    opener: Callable[[str], None] = open_in_explorer,
) -> dict:
    if action.kind == "open":
        opener(action.path)
        return CleanupResult(
            action_id=action.action_id,
            kind=action.kind,
            status="success",
            path=action.path,
            path_display=action.path_display,
            tier=action.tier,
            estimated_recovered_bytes=0,
            message="已在资源管理器中打开。",
        ).to_dict()

    if action.kind != "recycle_bin":
        raise CleanupError("execute 仅支持 open 和 recycle_bin action")

    if not os_path_exists(action.path):
        return CleanupResult(
            action_id=action.action_id,
            kind=action.kind,
            status="skipped",
            path=action.path,
            path_display=action.path_display,
            tier=action.tier,
            estimated_recovered_bytes=0,
            skipped_missing_count=1,
            message="路径已不存在，无需清理。",
        ).to_dict()

    try:
        mover(action.path)
    except OSError as exc:
        if is_locked_error(exc):
            return rejected_or_partial(action, "partial_success", locked=1)
        if is_denied_error(exc):
            return rejected_or_partial(action, "partial_success", denied=1)
        raise

    return CleanupResult(
        action_id=action.action_id,
        kind=action.kind,
        status="success",
        path=action.path,
        path_display=action.path_display,
        tier=action.tier,
        estimated_recovered_bytes=action.size_bytes,
        moved_count=1,
        message="已移入回收站。",
    ).to_dict()


def rejected_or_partial(action: ResolvedAction, status: str, locked: int = 0, denied: int = 0) -> dict:
    reason = "部分文件已跳过。"
    if locked:
        reason = "路径正在被占用。请关闭相关应用或重启后重试。"
    elif denied:
        reason = "权限被拒绝。请使用合适权限运行，或跳过此项目。"
    return CleanupResult(
        action_id=action.action_id,
        kind=action.kind,
        status=status,
        path=action.path,
        path_display=action.path_display,
        tier=action.tier,
        estimated_recovered_bytes=0,
        skipped_locked_count=locked,
        skipped_denied_count=denied,
        message=reason,
    ).to_dict()


def os_path_exists(path: str) -> bool:
    return os.path.exists(path)


def load_analysis(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Resolve and dry-run cleanup actions")
    parser.add_argument("analysis_json", help="Path to an AnalysisResult JSON file.")
    parser.add_argument("action_id", help="Action ID from AnalysisResult.items[].actions[].id")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="执行已解析的 action。open 会打开资源管理器；recycle_bin 会移入回收站。",
    )
    args = parser.parse_args(argv)

    try:
        analysis_result = load_analysis(args.analysis_json)
        action = resolve_action(analysis_result, args.action_id)
        result = execute(action) if args.execute else dry_run(action)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except CleanupError as exc:
        print(json.dumps({"status": "rejected", "error": str(exc)}, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
