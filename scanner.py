#!/usr/bin/env python3
"""Read-only Windows hotspot scanner for Local Disk Cleaner V1.

This is a bootstrap implementation using only the Python standard library. The
shape is intentionally close to the V1 spec so the scanner can later move into a
Rust/Tauri backend without changing the product contract.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import string
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable


REPARSE_POINT_ATTRIBUTE = 0x400
DEFAULT_MIN_BYTES = 50 * 1024 * 1024
DEFAULT_CHILD_LIMIT = 80


@dataclass
class SizeStats:
    size_bytes: int = 0
    denied_count: int = 0
    locked_count: int = 0
    skipped_reparse_count: int = 0
    truncated_count: int = 0
    file_count: int = 0
    dir_count: int = 0

    def add(self, other: "SizeStats") -> None:
        self.size_bytes += other.size_bytes
        self.denied_count += other.denied_count
        self.locked_count += other.locked_count
        self.skipped_reparse_count += other.skipped_reparse_count
        self.truncated_count += other.truncated_count
        self.file_count += other.file_count
        self.dir_count += other.dir_count


def is_windows_locked(exc: OSError) -> bool:
    return getattr(exc, "winerror", None) == 32


def is_denied(exc: OSError) -> bool:
    return isinstance(exc, PermissionError) or getattr(exc, "winerror", None) == 5


def is_reparse_point(path: str) -> bool:
    try:
        st = os.stat(path, follow_symlinks=False)
    except OSError:
        return False
    return bool(getattr(st, "st_file_attributes", 0) & REPARSE_POINT_ATTRIBUTE)


def safe_scandir(path: str):
    try:
        return os.scandir(path), None
    except OSError as exc:
        return None, exc


def default_worker_count() -> int:
    return min(8, max(1, os.cpu_count() or 1))


def size_tree(path: str, max_depth: int | None = None, _depth: int = 0) -> SizeStats:
    stats = SizeStats()

    if os.path.islink(path) or is_reparse_point(path):
        stats.skipped_reparse_count += 1
        return stats

    try:
        if os.path.isfile(path):
            stats.size_bytes += os.path.getsize(path)
            stats.file_count += 1
            return stats
    except OSError as exc:
        if is_windows_locked(exc):
            stats.locked_count += 1
        elif is_denied(exc):
            stats.denied_count += 1
        else:
            stats.denied_count += 1
        return stats

    if max_depth is not None and _depth >= max_depth:
        stats.dir_count += 1
        stats.truncated_count += 1
        return stats

    entries, err = safe_scandir(path)
    if err is not None:
        if is_windows_locked(err):
            stats.locked_count += 1
        elif is_denied(err):
            stats.denied_count += 1
        else:
            stats.denied_count += 1
        return stats

    stats.dir_count += 1
    with entries:
        for entry in entries:
            try:
                if entry.is_symlink() or is_reparse_point(entry.path):
                    stats.skipped_reparse_count += 1
                    continue
                if entry.is_file(follow_symlinks=False):
                    stats.size_bytes += entry.stat(follow_symlinks=False).st_size
                    stats.file_count += 1
                elif entry.is_dir(follow_symlinks=False):
                    stats.add(size_tree(entry.path, max_depth=max_depth, _depth=_depth + 1))
            except OSError as exc:
                if is_windows_locked(exc):
                    stats.locked_count += 1
                elif is_denied(exc):
                    stats.denied_count += 1
                else:
                    stats.denied_count += 1

    return stats


def child_row(entry: os.DirEntry, max_depth: int | None = None) -> dict[str, Any] | None:
    try:
        if entry.is_symlink() or is_reparse_point(entry.path):
            return None
        child_stats = size_tree(entry.path, max_depth=max_depth)
    except OSError as exc:
        child_stats = SizeStats(
            denied_count=0 if is_windows_locked(exc) else 1,
            locked_count=1 if is_windows_locked(exc) else 0,
        )

    return {
        "name": entry.name,
        "path": entry.path,
        "size_bytes": child_stats.size_bytes,
        "denied_count": child_stats.denied_count,
        "locked_count": child_stats.locked_count,
        "skipped_reparse_count": child_stats.skipped_reparse_count,
        "truncated_count": child_stats.truncated_count,
        "file_count": child_stats.file_count,
        "dir_count": child_stats.dir_count,
    }


def scan_children(
    path: str,
    min_bytes: int,
    limit: int,
    workers: int = 1,
    max_depth: int | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> list[dict]:
    if not os.path.isdir(path):
        return []

    entries, err = safe_scandir(path)
    if err is not None:
        return [
            {
                "name": Path(path).name or path,
                "path": path,
                "size_bytes": 0,
                "denied_count": 0 if is_windows_locked(err) else 1,
                "locked_count": 1 if is_windows_locked(err) else 0,
                "skipped_reparse_count": 0,
                "truncated_count": 0,
                "file_count": 0,
                "dir_count": 0,
                "error": str(err),
            }
        ]

    rows: list[dict] = []
    with entries:
        child_entries = list(entries)

    total = len(child_entries)
    completed = 0

    def accept(row: dict[str, Any] | None) -> None:
        nonlocal completed
        completed += 1
        if progress_callback and row is not None:
            progress_callback(
                {
                    "path": row["path"],
                    "name": row["name"],
                    "index": completed,
                    "total": total,
                    "size_bytes": row["size_bytes"],
                }
            )
        if row is not None and row["size_bytes"] >= min_bytes:
            rows.append(row)

    if workers <= 1 or total <= 1:
        for entry in child_entries:
            accept(child_row(entry, max_depth=max_depth))
    else:
        pool_size = min(max(1, workers), total)
        with ThreadPoolExecutor(max_workers=pool_size) as executor:
            futures = [executor.submit(child_row, entry, max_depth) for entry in child_entries]
            for future in as_completed(futures):
                accept(future.result())

    rows.sort(key=lambda row: row["size_bytes"], reverse=True)
    return rows[:limit]


def list_drives() -> list[dict]:
    drives: list[dict] = []
    if os.name == "nt":
        roots = [f"{letter}:\\" for letter in string.ascii_uppercase]
    else:
        roots = ["/"]

    for root in roots:
        if not os.path.exists(root):
            continue
        try:
            total, used, free = shutil.disk_usage(root)
        except OSError:
            continue
        drives.append(
            {
                "name": root,
                "total_bytes": total,
                "used_bytes": used,
                "free_bytes": free,
            }
        )
    return drives


def default_targets() -> list[tuple[str, str]]:
    profile = os.environ.get("USERPROFILE") or str(Path.home())
    local = os.environ.get("LOCALAPPDATA") or str(Path(profile) / "AppData" / "Local")
    roaming = os.environ.get("APPDATA") or str(Path(profile) / "AppData" / "Roaming")
    temp = os.environ.get("TEMP") or str(Path(local) / "Temp")

    targets = [
        ("user_profile", profile),
        ("appdata_local", local),
        ("appdata_roaming", roaming),
        ("temp", temp),
        ("downloads", str(Path(profile) / "Downloads")),
        ("desktop", str(Path(profile) / "Desktop")),
        ("documents", str(Path(profile) / "Documents")),
    ]

    if os.name == "nt":
        targets.extend(
            [
                ("program_data", os.environ.get("ProgramData", r"C:\ProgramData")),
                ("program_files", os.environ.get("ProgramFiles", r"C:\Program Files")),
                (
                    "program_files_x86",
                    os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                ),
            ]
        )

    return targets


def normalize_drive_root(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("drive root cannot be empty")
    letter = raw[0].upper()
    if len(letter) != 1 or letter not in string.ascii_uppercase:
        raise ValueError(f"invalid Windows drive root: {value}")
    return f"{letter}:\\"


def system_drive_root() -> str:
    return normalize_drive_root(os.environ.get("SystemDrive", "C:")) if os.name == "nt" else "/"


def targets_for_drive(drive_root: str, system_drive: str | None = None) -> list[tuple[str, str]]:
    root = normalize_drive_root(drive_root) if os.name == "nt" else "/"
    system_root = normalize_drive_root(system_drive or system_drive_root()) if os.name == "nt" else "/"
    if root.lower() == system_root.lower():
        return default_targets()
    return [(f"drive_{root[0].lower()}", root)]


def parse_target(raw: str) -> tuple[str, str]:
    if "=" not in raw:
        raise argparse.ArgumentTypeError("--target must be formatted as name=path")
    name, path = raw.split("=", 1)
    if not name.strip():
        raise argparse.ArgumentTypeError("target name cannot be empty")
    return name.strip(), os.path.abspath(os.path.expandvars(os.path.expanduser(path.strip())))


def build_system_info() -> dict:
    drives = list_drives()
    system_drive = system_drive_root()
    primary = next((d for d in drives if d["name"].lower() == system_drive.lower()), None)

    return {
        "os": f"{platform.system()} {platform.release()}".strip(),
        "build": platform.version(),
        "arch": platform.machine(),
        "home": str(Path.home()),
        "disk_name": primary["name"] if primary else system_drive,
        "drives": drives,
    }


def scan(
    targets: Iterable[tuple[str, str]],
    min_bytes: int,
    limit: int,
    workers: int = 1,
    max_depth: int | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict:
    started = time.time()
    groups = []

    for name, path in targets:
        groups.append(
            {
                "group": name,
                "path": path,
                "exists": os.path.exists(path),
                "children": scan_children(
                    path,
                    min_bytes=min_bytes,
                    limit=limit,
                    workers=workers,
                    max_depth=max_depth,
                    progress_callback=progress_callback,
                ),
            }
        )

    return {
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "scan_seconds": round(time.time() - started, 2),
        "scan_options": {
            "min_bytes": min_bytes,
            "limit": limit,
            "workers": workers,
            "max_depth": max_depth,
        },
        "system": build_system_info(),
        "groups": groups,
    }


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def nonnegative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("value must be zero or positive")
    return parsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only Windows hotspot scanner")
    parser.add_argument(
        "--target",
        action="append",
        type=parse_target,
        help="Scan a custom target instead of defaults, formatted name=path. Can repeat.",
    )
    parser.add_argument("--min-mb", type=positive_int, default=50)
    parser.add_argument(
        "--min-bytes",
        type=nonnegative_int,
        help="Override --min-mb with a byte threshold. Useful for tests and tiny fixtures.",
    )
    parser.add_argument("--limit", type=positive_int, default=DEFAULT_CHILD_LIMIT)
    parser.add_argument("--workers", type=positive_int, default=default_worker_count())
    parser.add_argument("--max-depth", type=nonnegative_int)
    parser.add_argument("--output", help="Write JSON to this file instead of stdout.")
    args = parser.parse_args(argv)

    targets = args.target if args.target else default_targets()
    min_bytes = args.min_bytes if args.min_bytes is not None else args.min_mb * 1024 * 1024
    result = scan(
        targets,
        min_bytes=min_bytes,
        limit=args.limit,
        workers=args.workers,
        max_depth=args.max_depth,
    )
    blob = json.dumps(result, ensure_ascii=False, indent=2)

    if args.output:
        Path(args.output).write_text(blob + "\n", encoding="utf-8")
    else:
        print(blob)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
