#!/usr/bin/env python3
"""AI-assisted deep scan merger with local permission verification.

The model may explain and rank findings, but cleanup permissions come only from
the local rules in this module plus analyzer/cleanup validation.
"""

from __future__ import annotations

import copy
import os
import re
from pathlib import Path
from typing import Any

try:
    from tools import analyzer, scanner
except ModuleNotFoundError:
    import analyzer
    import scanner


DEEP_SCAN_PARENT_TIERS = {"yellow"}
SAFE_CACHE_NAMES = {
    ".cache",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    ".nox",
}
SAFE_CACHE_SEGMENTS = {
    "cache",
    "caches",
    "code cache",
    "gpucache",
    "shadercache",
    "dxcache",
    "glcache",
    "temp",
    "tmp",
}
MANUAL_DOWNLOAD_SUFFIXES = {
    ".exe",
    ".msi",
    ".zip",
    ".rar",
    ".7z",
    ".iso",
    ".tar",
    ".gz",
    ".bz2",
    ".xz",
}


def collect_deep_candidates(
    analysis_result: dict[str, Any],
    min_bytes: int = 20 * 1024 * 1024,
    limit_per_parent: int = 30,
    workers: int = 1,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for item in analysis_result.get("items", []):
        if item.get("tier") not in DEEP_SCAN_PARENT_TIERS:
            continue
        path = item.get("path")
        if not path or not os.path.isdir(path):
            continue
        for child in scanner.scan_children(path, min_bytes=min_bytes, limit=limit_per_parent, workers=workers):
            child = dict(child)
            child["parent_item_id"] = item.get("id", "")
            child["parent_tier"] = item.get("tier", "")
            child["parent_name"] = item.get("name", "")
            candidates.append(child)
    candidates.sort(key=lambda row: int(row.get("size_bytes") or 0), reverse=True)
    return candidates


def classify_deep_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    path = candidate.get("path", "")
    if is_locally_verified_cache(path):
        return build_green_cache_item(candidate)
    return build_manual_candidate_item(candidate)


def is_locally_verified_cache(path: str) -> bool:
    if not path or analyzer.is_never_delete(path) or not analyzer.is_cleanup_allowed(path):
        return False

    normalized = os.path.normpath(path)
    parts = [part.lower() for part in Path(normalized).parts]
    name = parts[-1] if parts else ""
    if name in SAFE_CACHE_NAMES:
        return True
    if name in SAFE_CACHE_SEGMENTS:
        return True
    if len(parts) >= 2 and parts[-2] == "node_modules" and name == ".cache":
        return True
    return False


def is_manual_download_artifact(path: str) -> bool:
    suffixes = Path(path).suffixes
    if not suffixes:
        return False
    lower_suffixes = [suffix.lower() for suffix in suffixes]
    return any(suffix in MANUAL_DOWNLOAD_SUFFIXES for suffix in lower_suffixes)


def build_green_cache_item(candidate: dict[str, Any]) -> dict[str, Any]:
    path = candidate.get("path", "")
    item_id = deep_item_id("ai_verified_cache", path)
    return {
        "id": item_id,
        "tier": "green",
        "rule_id": "ai_verified_cache",
        "name": candidate.get("name") or Path(path).name or "AI 确认可清理缓存",
        "path": path,
        "path_display": analyzer.alias_path(path),
        "size_bytes": int(candidate.get("size_bytes") or 0),
        "description": "AI 深度扫描发现该子项；本地规则确认它属于缓存、临时或可再生目录。",
        "manual_hint": "可先关闭相关应用后移入回收站。清空回收站前仍可恢复。",
        "close_apps_hint": ["可能正在使用该缓存的应用"],
        "denied_count": candidate.get("denied_count", 0),
        "locked_count": candidate.get("locked_count", 0),
        "skipped_reparse_count": candidate.get("skipped_reparse_count", 0),
        "parent_item_id": candidate.get("parent_item_id", ""),
        "ai_deep_scan": {
            "source": "local_verified_ai_deep_scan",
            "model_granted_permission": False,
        },
        "actions": [
            {"id": analyzer.action_id("recycle", item_id), "kind": "recycle_bin"},
            {"id": analyzer.action_id("open", item_id), "kind": "open"},
        ],
    }


def build_manual_candidate_item(candidate: dict[str, Any]) -> dict[str, Any]:
    path = candidate.get("path", "")
    item_id = deep_item_id("ai_manual_candidate", path)
    artifact_note = "旧安装包或压缩包可能可删除，但需要确认是否仍要保留。" if is_manual_download_artifact(path) else ""
    return {
        "id": item_id,
        "tier": "reviewable",
        "rule_id": "ai_manual_candidate",
        "name": candidate.get("name") or Path(path).name or "AI 建议人工检查",
        "path": path,
        "path_display": analyzer.alias_path(path),
        "size_bytes": int(candidate.get("size_bytes") or 0),
        "description": "AI 深度扫描发现的可清理候选，但未通过本地可自动清理规则。",
        "manual_hint": artifact_note or "请打开后人工确认内容用途，再决定是否移动或删除。",
        "close_apps_hint": [],
        "denied_count": candidate.get("denied_count", 0),
        "locked_count": candidate.get("locked_count", 0),
        "skipped_reparse_count": candidate.get("skipped_reparse_count", 0),
        "parent_item_id": candidate.get("parent_item_id", ""),
        "ai_deep_scan": {
            "source": "ai_suggested_reviewable_cleanup",
            "model_granted_permission": False,
        },
        "actions": [{"id": analyzer.action_id("open", item_id), "kind": "open"}],
    }


def merge_deep_scan_items(analysis_result: dict[str, Any], deep_items: list[dict[str, Any]]) -> dict[str, Any]:
    merged = copy.deepcopy(analysis_result)
    if not deep_items:
        merged["ai_deep_scan"] = {
            "candidate_count": 0,
            "verified_green_count": 0,
            "verified_green_bytes": 0,
            "manual_candidate_count": 0,
            "reviewable_bytes": 0,
        }
        merged.setdefault("action_policy", {})["ai_can_grant_delete_permission"] = False
        return merged

    existing_ids = {item.get("id") for item in merged.get("items", [])}
    unique_deep_items = [item for item in deep_items if item.get("id") not in existing_ids]
    merged.setdefault("items", []).extend(unique_deep_items)
    merged["items"].sort(key=lambda item: int(item.get("size_bytes") or 0), reverse=True)

    summary = dict(merged.get("summary", {}))
    parent_tier_by_id = {item.get("id"): item.get("tier") for item in analysis_result.get("items", [])}
    moved_from_parent: dict[str, int] = {}
    verified_green_bytes = 0
    manual_count = 0
    reviewable_bytes = 0

    for item in unique_deep_items:
        size = int(item.get("size_bytes") or 0)
        tier = item.get("tier")
        parent_id = item.get("parent_item_id", "")
        if tier == "green":
            verified_green_bytes += size
            parent_tier = parent_tier_by_id.get(parent_id)
            if parent_tier and parent_tier != "green":
                moved_from_parent[parent_tier] = moved_from_parent.get(parent_tier, 0) + size
        elif tier == "reviewable":
            manual_count += 1
            reviewable_bytes += size
            parent_tier = parent_tier_by_id.get(parent_id)
            if parent_tier and parent_tier != "reviewable":
                moved_from_parent[parent_tier] = moved_from_parent.get(parent_tier, 0) + size

    summary["green_bytes"] = int(summary.get("green_bytes") or 0) + verified_green_bytes
    summary["reviewable_bytes"] = int(summary.get("reviewable_bytes") or 0) + reviewable_bytes
    for tier, size in moved_from_parent.items():
        key = f"{tier}_bytes"
        summary[key] = max(0, int(summary.get(key) or 0) - size)
    merged["summary"] = summary
    merged["top_consumers"] = merged["items"][:10]
    merged["ai_deep_scan"] = {
        "candidate_count": len(unique_deep_items),
        "verified_green_count": sum(1 for item in unique_deep_items if item.get("tier") == "green"),
        "verified_green_bytes": verified_green_bytes,
        "manual_candidate_count": manual_count,
        "reviewable_bytes": reviewable_bytes,
    }
    policy = dict(merged.get("action_policy", {}))
    policy["cleanup_action_source"] = "local_rules_with_ai_deep_scan"
    policy["ai_can_grant_delete_permission"] = False
    merged["action_policy"] = policy
    return merged


def run_deep_scan(
    analysis_result: dict[str, Any],
    min_bytes: int = 20 * 1024 * 1024,
    limit_per_parent: int = 30,
    workers: int = 1,
) -> dict[str, Any]:
    candidates = collect_deep_candidates(
        analysis_result,
        min_bytes=min_bytes,
        limit_per_parent=limit_per_parent,
        workers=workers,
    )
    items = [classify_deep_candidate(candidate) for candidate in candidates]
    return merge_deep_scan_items(analysis_result, items)


def deep_item_id(rule_id: str, path: str) -> str:
    safe = re.sub(r"[^a-z0-9_]+", "_", analyzer.norm(path).lower()).strip("_")
    return f"item_{rule_id}_{safe}"
