#!/usr/bin/env python3
"""Native desktop UI for Local Disk Cleaner."""

from __future__ import annotations

import argparse
import json
import queue
import sys
import threading
from pathlib import Path
from typing import Any

try:
    import tkinter as tk
    from tkinter import messagebox, ttk
except ImportError:  # pragma: no cover - exercised by runtime environment
    tk = None
    ttk = None
    messagebox = None

try:
    from tools import ai_deep_scan, analyzer, cleanup, model_explainer, report, scanner
except ModuleNotFoundError:
    import ai_deep_scan
    import analyzer
    import cleanup
    import model_explainer
    import report
    import scanner


TIER_ORDER = ["green", "reviewable", "yellow", "red", "blue"]
TIER_LABELS = {
    "green": "可安全清理",
    "reviewable": "可清理但需确认",
    "yellow": "需要人工判断",
    "red": "谨慎处理",
    "blue": "其他占用",
}
TIER_DESCRIPTIONS = {
    "green": "已通过安全校验，可移入回收站。",
    "reviewable": "可能可以清理，但需要先确认内容是否还要保留。",
    "yellow": "通常包含用户文件或应用数据，需要人工查看。",
    "red": "不建议手动处理，请优先使用系统设置或应用卸载器。",
    "blue": "已统计但没有明确清理建议。",
}
TIER_ACCENTS = {
    "green": "#16a34a",
    "reviewable": "#0891b2",
    "yellow": "#d97706",
    "red": "#dc2626",
    "blue": "#2563eb",
}

APP_BG = "#f3f6fb"
SURFACE_BG = "#ffffff"
PANEL_BG = "#f8fafc"
BORDER_COLOR = "#d7dde8"
TEXT_COLOR = "#111827"
MUTED_COLOR = "#667085"
DETAIL_TEXT_HEIGHT = 5
ANALYSIS_TEXT_HEIGHT = 18


def format_bytes(value: int | float | None) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(value or 0)
    index = 0
    while size >= 1024 and index < len(units) - 1:
        size /= 1024
        index += 1
    if index == 0:
        return f"{int(size)} {units[index]}"
    return f"{size:.1f} {units[index]}"


def drive_options(drives: list[dict[str, Any]]) -> list[str]:
    return [
        f"{drive.get('name', '')}  可用 {format_bytes(drive.get('free_bytes'))} / 共 {format_bytes(drive.get('total_bytes'))}"
        for drive in drives
        if drive.get("name")
    ]


def drive_root_from_option(option: str) -> str:
    return scanner.normalize_drive_root(str(option).split()[0])


def scan_targets_for_selection(custom_targets: list[tuple[str, str]], selected_drive: str) -> list[tuple[str, str]]:
    if custom_targets:
        return custom_targets
    return scanner.targets_for_drive(selected_drive)


def initial_custom_targets(arg_targets: list[tuple[str, str]] | None) -> list[tuple[str, str]]:
    return list(arg_targets or [])


def group_items_by_tier(analysis_result: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    groups = {tier: [] for tier in TIER_ORDER}
    for item in analysis_result.get("items", []):
        tier = item.get("tier")
        if tier in groups:
            groups[tier].append(item)
    for items in groups.values():
        items.sort(key=lambda item: int(item.get("size_bytes") or 0), reverse=True)
    return groups


def tier_summary_rows(analysis_result: dict[str, Any]) -> list[tuple[str, str, str]]:
    summary = analysis_result.get("summary", {})
    counts = {tier: 0 for tier in TIER_ORDER}
    for item in analysis_result.get("items", []):
        tier = item.get("tier")
        if tier in counts:
            counts[tier] += 1
    return [
        (
            TIER_LABELS[tier],
            format_bytes(summary.get(f"{tier}_bytes", 0)),
            f"{counts[tier]} 项",
        )
        for tier in TIER_ORDER
    ]


def tier_summary_cards(analysis_result: dict[str, Any]) -> list[dict[str, str]]:
    rows = tier_summary_rows(analysis_result)
    return [
        {
            "tier": tier,
            "label": label,
            "size": size,
            "count": count,
            "accent": TIER_ACCENTS[tier],
            "description": TIER_DESCRIPTIONS[tier],
        }
        for tier, (label, size, count) in zip(TIER_ORDER, rows)
    ]


def find_action(item: dict[str, Any], kind: str) -> dict[str, Any] | None:
    return next((action for action in item.get("actions", []) if action.get("kind") == kind), None)


def action_label(kind: str) -> str:
    return {
        "open": "打开位置",
        "recycle_bin": "移入回收站",
    }.get(kind, "执行操作")


def format_item_detail(item: dict[str, Any]) -> tuple[str, str]:
    title = f"{item.get('name', '')} · {format_bytes(item.get('size_bytes'))}"
    insight = item.get("ai_insight") or {}
    confidence = insight.get("confidence")
    try:
        confidence_text = f"{float(confidence) * 100:.0f}%" if confidence not in (None, "") else ""
    except (TypeError, ValueError):
        confidence_text = ""
    detail = "\n".join(
        part
        for part in [
            f"分类：{TIER_LABELS.get(item.get('tier'), item.get('tier', ''))}",
            f"位置：{item.get('path_display') or item.get('path', '')}",
            f"说明：{item.get('description', '')}",
            f"处理建议：{item.get('manual_hint', '')}",
            f"操作前建议关闭：{', '.join(item.get('close_apps_hint', []))}",
            f"AI 判断：{insight.get('category', '')}",
            f"AI 风险：{insight.get('risk', '')}",
            f"置信度：{confidence_text}",
            f"判断依据：{insight.get('reason', '')}",
            f"建议：{insight.get('recommended_action', '')}",
            f"补充说明：{insight.get('note', '')}",
        ]
        if not part.endswith("：")
    )
    return title, detail


def item_display_name_by_id(analysis_result: dict[str, Any] | None) -> dict[str, str]:
    names: dict[str, str] = {}
    for item in (analysis_result or {}).get("items", []):
        item_id = item.get("id")
        if not item_id:
            continue
        name = item.get("name") or item_id
        path_display = item.get("path_display") or item.get("path", "")
        names[item_id] = f"{name}（{path_display}）" if path_display else name
    return names


def format_question_answer(question: str, answer: str) -> str:
    return "\n".join(
        [
            "追问回答",
            f"问题：{question.strip() or '请解释当前项目。'}",
            "AI 回答：",
            answer.strip() or "AI 未返回有效回答。",
        ]
    )


def format_explanation(explanation: dict[str, Any], analysis_result: dict[str, Any] | None = None) -> str:
    lines = ["智能分析建议", ""]
    display_names = item_display_name_by_id(analysis_result)
    summary = explanation.get("summary", "")
    if summary:
        lines.extend(["总览：", str(summary), ""])
    priority = explanation.get("priority", [])
    if priority:
        lines.append("重点建议：")
        lines.extend(f"- {item}" for item in priority)
        lines.append("")
    item_notes = explanation.get("item_notes", [])
    if item_notes:
        lines.append("项目补充：")
        for note in item_notes:
            item_label = display_names.get(note.get("item_id", ""), note.get("item_id", ""))
            lines.append(f"- {item_label}: {note.get('note', '')}")
        lines.append("")
    item_insights = explanation.get("item_insights", [])
    if item_insights:
        lines.append("项目洞察：")
        for insight in item_insights:
            try:
                confidence = f"{float(insight.get('confidence', 0)) * 100:.0f}%"
            except (TypeError, ValueError):
                confidence = "0%"
            detail = "；".join(
                part
                for part in [
                    f"风险 {insight.get('risk', '')}" if insight.get("risk") else "",
                    f"置信度 {confidence}",
                    f"依据：{insight.get('reason', '')}" if insight.get("reason") else "",
                    f"建议：{insight.get('recommended_action', '')}" if insight.get("recommended_action") else "",
                ]
                if part
            )
            item_label = display_names.get(insight.get("item_id", ""), insight.get("item_id", ""))
            lines.append(f"- {item_label}：{insight.get('category', '')}（{detail}）")
        lines.append("")
    long_term = explanation.get("long_term", [])
    if long_term:
        lines.append("长期优化：")
        lines.extend(f"- {item}" for item in long_term)
    return "\n".join(lines).strip()


def format_deep_scan_status(analysis_result: dict[str, Any], model_error: str | None = None) -> str:
    meta = analysis_result.get("ai_deep_scan", {})
    lines = [
        "智能分析结果已更新到列表。",
        (
            f"新增 {int(meta.get('candidate_count') or 0)} 个候选，"
            f"其中 {int(meta.get('verified_green_count') or 0)} 个通过安全校验，可直接清理，"
            f"预计 {format_bytes(meta.get('verified_green_bytes'))}。"
        ),
        (
            f"{int(meta.get('manual_candidate_count') or 0)} 个归入“可清理但需确认”，"
            f"预计 {format_bytes(meta.get('reviewable_bytes'))}。"
        ),
        "智能分析不会授予删除权限；清理按钮只来自本地安全校验。",
    ]
    if model_error:
        lines.extend(["", f"模型解释未生成：{model_error}"])
    return "\n".join(lines)


class LocalDiskCleanerWindow:
    def __init__(
        self,
        root,
        targets: list[tuple[str, str]],
        min_bytes: int,
        limit: int,
        workers: int,
        scan_output: str,
        analysis_output: str,
        report_output: str,
    ):
        self.root = root
        self.custom_targets = targets
        self.min_bytes = min_bytes
        self.limit = limit
        self.workers = workers
        self.scan_output = scan_output
        self.analysis_output = analysis_output
        self.report_output = report_output
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.analysis_result: dict[str, Any] | None = None
        self.selected_drive_root = scanner.system_drive_root()
        self.item_by_tree_id: dict[str, dict[str, Any]] = {}
        self.tree_id_by_item_id: dict[str, str] = {}
        self.selected_item: dict[str, Any] | None = None

        self.root.title("本地磁盘清理器")
        self.root.geometry("1220x820")
        self.root.minsize(1080, 700)
        self.root.configure(bg=APP_BG)

        self._build_ui()
        self.root.after(100, self._poll_events)

    def _build_ui(self) -> None:
        self._configure_styles()
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)

        header = ttk.Frame(self.root, padding=(22, 18, 22, 14), style="Header.TFrame")
        header.grid(row=0, column=0, sticky="ew", padx=14, pady=(14, 0))
        header.columnconfigure(1, weight=1)

        title = ttk.Label(header, text="本地磁盘清理器", style="Title.TLabel")
        title.grid(row=0, column=0, sticky="w")
        self.status_var = tk.StringVar(value="未扫描")
        ttk.Label(header, textvariable=self.status_var, style="Status.TLabel").grid(row=0, column=1, sticky="e")

        subtitle = ttk.Label(
            header,
            text="扫描本机空间占用，区分可安全清理、需确认项目和谨慎处理项目。清理操作默认移入回收站。",
            style="Muted.TLabel",
        )
        subtitle.grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))

        header_actions = ttk.Frame(header, style="Header.TFrame")
        header_actions.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(16, 0))
        header_actions.columnconfigure(1, weight=1)
        ttk.Label(header_actions, text="扫描磁盘", style="FieldLabel.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.drive_var = tk.StringVar()
        self.drive_combo = ttk.Combobox(header_actions, textvariable=self.drive_var, state="readonly", width=42)
        self.drive_combo.grid(row=0, column=1, sticky="ew", padx=(0, 10))
        options = drive_options(scanner.list_drives())
        if options:
            self.drive_combo.configure(values=options)
            system_root = scanner.system_drive_root()
            selected_index = next(
                (index for index, option in enumerate(options) if drive_root_from_option(option).lower() == system_root.lower()),
                0,
            )
            self.drive_combo.current(selected_index)
            self.selected_drive_root = drive_root_from_option(options[selected_index])
        else:
            self.drive_combo.configure(values=[self.selected_drive_root])
            self.drive_combo.current(0)
        if self.custom_targets:
            self.drive_combo.configure(state="disabled")

        self.scan_button = ttk.Button(header_actions, text="开始扫描", command=self.start_scan, style="Primary.TButton")
        self.scan_button.grid(row=0, column=2, sticky="w")
        self.explain_button = ttk.Button(
            header_actions,
            text="智能深度分析",
            command=self.explain_with_model,
            state="disabled",
            style="Accent.TButton",
        )
        self.explain_button.grid(row=0, column=3, sticky="w", padx=(10, 0))

        body = ttk.PanedWindow(self.root, orient=tk.HORIZONTAL)
        body.grid(row=1, column=0, sticky="nsew", padx=14, pady=14)

        left = ttk.Frame(body, padding=14, style="Panel.TFrame")
        right = ttk.Frame(body, padding=14, style="Panel.TFrame")
        body.add(left, weight=3)
        body.add(right, weight=2)

        left.rowconfigure(2, weight=1)
        left.columnconfigure(0, weight=1)

        self.summary_frame = ttk.Frame(left)
        self.summary_frame.grid(row=0, column=0, sticky="ew", pady=(0, 14))
        for col in range(len(TIER_ORDER)):
            self.summary_frame.columnconfigure(col, weight=1)

        list_header = ttk.Frame(left, style="Panel.TFrame")
        list_header.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        list_header.columnconfigure(1, weight=1)
        ttk.Label(list_header, text="清理项目", style="SectionTitle.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(list_header, text="按分类展开，先处理绿色和需确认项目", style="Muted.TLabel").grid(row=0, column=1, sticky="e")

        columns = ("size", "path")
        self.tree = ttk.Treeview(left, columns=columns, show="tree headings", selectmode="browse")
        self.tree.heading("#0", text="分类 / 项目")
        self.tree.heading("size", text="大小")
        self.tree.heading("path", text="位置")
        self.tree.column("#0", width=230, stretch=True)
        self.tree.column("size", width=90, anchor="e", stretch=False)
        self.tree.column("path", width=420, stretch=True)
        self.tree.grid(row=2, column=0, sticky="nsew")
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        scrollbar = ttk.Scrollbar(left, orient=tk.VERTICAL, command=self.tree.yview)
        scrollbar.grid(row=2, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scrollbar.set)
        for tier, accent in TIER_ACCENTS.items():
            self.tree.tag_configure(tier, foreground=accent)

        right.columnconfigure(0, weight=1)
        right.rowconfigure(2, weight=1)
        right.rowconfigure(6, weight=5)

        ttk.Label(right, text="项目详情", style="SectionTitle.TLabel").grid(row=0, column=0, sticky="w")
        self.detail_title = ttk.Label(right, text="未选择项目", wraplength=420, style="DetailTitle.TLabel")
        self.detail_title.grid(row=1, column=0, sticky="ew", pady=(8, 6))
        self.detail_text = tk.Text(
            right,
            height=DETAIL_TEXT_HEIGHT,
            wrap="word",
            relief="solid",
            borderwidth=1,
            padx=10,
            pady=8,
            font=("Microsoft YaHei UI", 9),
            bg=PANEL_BG,
            fg=TEXT_COLOR,
            insertbackground=TEXT_COLOR,
        )
        self.detail_text.grid(row=2, column=0, sticky="nsew")
        self.detail_text.configure(state="disabled")

        actions = ttk.Frame(right, style="Panel.TFrame")
        actions.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        self.open_button = ttk.Button(actions, text=action_label("open"), command=self.open_selected, state="disabled")
        self.open_button.grid(row=0, column=0, padx=(0, 8))
        self.clean_button = ttk.Button(actions, text=action_label("recycle_bin"), command=self.clean_selected, state="disabled", style="Danger.TButton")
        self.clean_button.grid(row=0, column=1)

        ask_frame = ttk.Frame(right, style="Panel.TFrame")
        ask_frame.grid(row=4, column=0, sticky="ew", pady=(10, 14))
        ask_frame.columnconfigure(0, weight=1)
        self.question_var = tk.StringVar()
        self.question_entry = ttk.Entry(ask_frame, textvariable=self.question_var)
        self.question_entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.question_entry.insert(0, "追问 AI：这个项目能不能清理？")
        self.ask_button = ttk.Button(ask_frame, text="追问 AI", command=self.ask_ai_question, state="disabled")
        self.ask_button.grid(row=0, column=1)

        ttk.Label(right, text="分析说明", style="SectionTitle.TLabel").grid(row=5, column=0, sticky="w")
        self.explanation_text = tk.Text(
            right,
            height=ANALYSIS_TEXT_HEIGHT,
            wrap="word",
            relief="solid",
            borderwidth=1,
            padx=12,
            pady=10,
            font=("Microsoft YaHei UI", 10),
            bg=PANEL_BG,
            fg=TEXT_COLOR,
            insertbackground=TEXT_COLOR,
        )
        self.explanation_text.grid(row=6, column=0, sticky="nsew", pady=(8, 0))
        self.explanation_text.configure(state="disabled")
        self._set_explanation("完成扫描后，可运行智能深度分析。这里会展示 AI 对大文件和目录的判断依据、优先检查顺序和处理建议。")

    def _configure_styles(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(".", font=("Microsoft YaHei UI", 9), background=APP_BG, foreground=TEXT_COLOR)
        style.configure("Header.TFrame", background=SURFACE_BG)
        style.configure("Panel.TFrame", background=SURFACE_BG)
        style.configure("TFrame", background=SURFACE_BG)
        style.configure("Title.TLabel", background=SURFACE_BG, foreground=TEXT_COLOR, font=("Microsoft YaHei UI", 18, "bold"))
        style.configure("SectionTitle.TLabel", background=SURFACE_BG, foreground=TEXT_COLOR, font=("Microsoft YaHei UI", 12, "bold"))
        style.configure("FieldLabel.TLabel", background=SURFACE_BG, foreground=TEXT_COLOR, font=("Microsoft YaHei UI", 9, "bold"))
        style.configure("Muted.TLabel", background=SURFACE_BG, foreground=MUTED_COLOR)
        style.configure("Status.TLabel", background=PANEL_BG, foreground="#344054", padding=(10, 5))
        style.configure("DetailTitle.TLabel", background=SURFACE_BG, foreground=TEXT_COLOR, font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("TButton", padding=(14, 7))
        style.configure("Primary.TButton", padding=(16, 7), font=("Microsoft YaHei UI", 9, "bold"))
        style.configure("Accent.TButton", padding=(16, 7), font=("Microsoft YaHei UI", 9, "bold"))
        style.configure("Danger.TButton", padding=(14, 7))
        style.configure("TCombobox", padding=(6, 4))
        style.configure(
            "Treeview",
            rowheight=30,
            fieldbackground=SURFACE_BG,
            background=SURFACE_BG,
            foreground=TEXT_COLOR,
            bordercolor=BORDER_COLOR,
        )
        style.configure("Treeview.Heading", padding=(8, 7), font=("Microsoft YaHei UI", 9, "bold"), background=PANEL_BG)
        style.map("Treeview", background=[("selected", "#dbeafe")], foreground=[("selected", TEXT_COLOR)])

    def start_scan(self) -> None:
        self.scan_button.configure(state="disabled")
        if not self.custom_targets:
            self.drive_combo.configure(state="disabled")
        self.selected_drive_root = self.current_drive_root()
        self.status_var.set(f"正在扫描 {self.selected_drive_root} ...")
        self._clear_report()
        thread = threading.Thread(target=self._run_scan, daemon=True)
        thread.start()

    def current_drive_root(self) -> str:
        if self.custom_targets:
            return self.selected_drive_root
        option = self.drive_var.get()
        return drive_root_from_option(option) if option else self.selected_drive_root

    def _run_scan(self) -> None:
        try:
            targets = scan_targets_for_selection(self.custom_targets, self.selected_drive_root)
            scan_result = scanner.scan(
                targets,
                min_bytes=self.min_bytes,
                limit=self.limit,
                workers=self.workers,
            )
            scan_result["selected_drive"] = self.selected_drive_root
            analysis_result = analyzer.analyze(scan_result)
            html = report.render_html(analysis_result, analysis_file=self.analysis_output)
            Path(self.scan_output).write_text(
                json.dumps(scan_result, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            Path(self.analysis_output).write_text(
                json.dumps(analysis_result, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            Path(self.report_output).write_text(html, encoding="utf-8")
            self.events.put(("completed", analysis_result))
        except Exception as exc:
            self.events.put(("failed", str(exc)))

    def _poll_events(self) -> None:
        try:
            event, payload = self.events.get_nowait()
        except queue.Empty:
            self.root.after(100, self._poll_events)
            return
        if event == "completed":
            self.analysis_result = payload
            self.status_var.set(f"{self.selected_drive_root} 扫描完成，可查看清理建议")
            self.scan_button.configure(state="normal")
            if not self.custom_targets:
                self.drive_combo.configure(state="readonly")
            self.explain_button.configure(state="normal")
            self.ask_button.configure(state="normal")
            self._render_report(payload)
        elif event == "failed":
            self.status_var.set("扫描失败")
            self.scan_button.configure(state="normal")
            if not self.custom_targets:
                self.drive_combo.configure(state="readonly")
            messagebox.showerror("扫描失败", payload)
        elif event == "explanation":
            self.explain_button.configure(state="normal")
            self._set_explanation(format_explanation(payload, self.analysis_result))
        elif event == "deep_scan_completed":
            analysis_result, explanation, model_error = payload
            self.analysis_result = analysis_result
            self.status_var.set("智能分析完成，结果已更新")
            self.explain_button.configure(state="normal")
            self.ask_button.configure(state="normal")
            self._render_report(analysis_result)
            text = format_deep_scan_status(analysis_result, model_error=model_error)
            if explanation:
                text = text + "\n\n" + format_explanation(explanation, analysis_result)
            self._set_explanation(text)
        elif event == "explanation_failed":
            self.explain_button.configure(state="normal")
            self.ask_button.configure(state="normal" if self.analysis_result else "disabled")
            self._set_explanation(
                "智能分析未完成。\n"
                "请确认本机 settings.json 中已配置 DeepSeek API Key，或检查网络连接。\n\n"
                f"错误：{payload}"
            )
        elif event == "question_answer":
            self.ask_button.configure(state="normal")
            self.status_var.set("AI 追问完成")
            answer = payload.get("answer", "")
            question = payload.get("question", "")
            current_text = self.explanation_text.get("1.0", "end").strip()
            self._set_explanation(current_text + "\n\n" + format_question_answer(question, answer), scroll_to_end=True)
        elif event == "question_failed":
            self.ask_button.configure(state="normal")
            messagebox.showerror("追问失败", payload)
        self.root.after(100, self._poll_events)

    def _clear_report(self) -> None:
        for child in self.summary_frame.winfo_children():
            child.destroy()
        for node in self.tree.get_children():
            self.tree.delete(node)
        self.item_by_tree_id.clear()
        self.tree_id_by_item_id.clear()
        self.selected_item = None
        self._set_detail("未选择项目", "")
        self.open_button.configure(state="disabled")
        self.clean_button.configure(state="disabled")
        self.explain_button.configure(state="disabled")
        self.ask_button.configure(state="disabled")
        self._set_explanation("")

    def _render_report(self, analysis_result: dict[str, Any]) -> None:
        for child in self.summary_frame.winfo_children():
            child.destroy()
        for node in self.tree.get_children():
            self.tree.delete(node)
        self.item_by_tree_id.clear()
        self.tree_id_by_item_id.clear()
        self.selected_item = None
        self._set_detail("未选择项目", "")
        self.open_button.configure(state="disabled")
        self.clean_button.configure(state="disabled")

        for col, card in enumerate(tier_summary_cards(analysis_result)):
            frame = tk.Frame(
                self.summary_frame,
                bg=SURFACE_BG,
                highlightbackground=BORDER_COLOR,
                highlightthickness=1,
                bd=0,
            )
            frame.grid(row=0, column=col, sticky="ew", padx=(0 if col == 0 else 8, 0))
            frame.columnconfigure(1, weight=1)
            tk.Frame(frame, bg=card["accent"], width=4, height=78).grid(row=0, column=0, rowspan=3, sticky="nsw")
            tk.Label(
                frame,
                text=card["label"],
                bg=SURFACE_BG,
                fg=MUTED_COLOR,
                font=("Microsoft YaHei UI", 9),
                anchor="w",
            ).grid(row=0, column=1, sticky="ew", padx=12, pady=(10, 0))
            tk.Label(
                frame,
                text=card["size"],
                bg=SURFACE_BG,
                fg=TEXT_COLOR,
                font=("Microsoft YaHei UI", 14, "bold"),
                anchor="w",
            ).grid(row=1, column=1, sticky="ew", padx=12, pady=(2, 0))
            tk.Label(
                frame,
                text=card["count"],
                bg=SURFACE_BG,
                fg=MUTED_COLOR,
                font=("Microsoft YaHei UI", 9),
                anchor="w",
            ).grid(row=2, column=1, sticky="ew", padx=12, pady=(0, 10))

        groups = group_items_by_tier(analysis_result)
        for tier in TIER_ORDER:
            parent = self.tree.insert(
                "",
                "end",
                text=TIER_LABELS[tier],
                values=("", TIER_DESCRIPTIONS[tier]),
                tags=(tier,),
                open=True,
            )
            for item in groups[tier]:
                node = self.tree.insert(
                    parent,
                    "end",
                    text=item.get("name", ""),
                    values=(format_bytes(item.get("size_bytes")), item.get("path_display") or item.get("path", "")),
                )
                self.item_by_tree_id[node] = item
                self.tree_id_by_item_id[item.get("id", "")] = node

    def _on_select(self, _event=None) -> None:
        selection = self.tree.selection()
        if not selection or selection[0] not in self.item_by_tree_id:
            self.selected_item = None
            self._set_detail("未选择项目", "")
            self.open_button.configure(state="disabled")
            self.clean_button.configure(state="disabled")
            return
        item = self.item_by_tree_id[selection[0]]
        self.selected_item = item
        title, detail = format_item_detail(item)
        self._set_detail(title, detail)
        self.open_button.configure(state="normal" if find_action(item, "open") else "disabled")
        self.clean_button.configure(state="normal" if find_action(item, "recycle_bin") else "disabled")
        self.ask_button.configure(state="normal" if self.analysis_result else "disabled")

    def _set_detail(self, title: str, detail: str) -> None:
        self.detail_title.configure(text=title)
        self.detail_text.configure(state="normal")
        self.detail_text.delete("1.0", "end")
        self.detail_text.insert("1.0", detail)
        self.detail_text.configure(state="disabled")

    def _set_explanation(self, detail: str, scroll_to_end: bool = False) -> None:
        self.explanation_text.configure(state="normal")
        self.explanation_text.delete("1.0", "end")
        self.explanation_text.insert("1.0", detail)
        if scroll_to_end:
            self.explanation_text.see("end")
        self.explanation_text.configure(state="disabled")

    def explain_with_model(self) -> None:
        if not self.analysis_result:
            return
        self.explain_button.configure(state="disabled")
        self.status_var.set(f"正在对 {self.selected_drive_root} 进行智能深度分析...")
        self._set_explanation("正在进一步扫描需要确认的目录，并生成处理建议。清理权限仍只由本地安全校验决定。")
        thread = threading.Thread(target=self._run_ai_deep_scan, daemon=True)
        thread.start()

    def _run_ai_deep_scan(self) -> None:
        try:
            base = self.analysis_result or {}
            deep_min_bytes = min(self.min_bytes, 20 * 1024 * 1024)
            analysis_result = ai_deep_scan.run_deep_scan(
                base,
                min_bytes=deep_min_bytes,
                limit_per_parent=max(self.limit, 30),
                workers=self.workers,
            )
            explanation = None
            model_error = None
            try:
                explanation = model_explainer.explain_analysis(analysis_result)
                analysis_result = model_explainer.apply_explanation_to_analysis(analysis_result, explanation)
            except Exception as exc:
                model_error = str(exc)

            html = report.render_html(analysis_result, analysis_file=self.analysis_output)
            Path(self.analysis_output).write_text(
                json.dumps(analysis_result, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            Path(self.report_output).write_text(html, encoding="utf-8")
            self.events.put(("deep_scan_completed", (analysis_result, explanation, model_error)))
        except Exception as exc:
            self.events.put(("explanation_failed", str(exc)))

    def ask_ai_question(self) -> None:
        if not self.analysis_result:
            return
        question = self.question_var.get().strip()
        if not question or question.startswith("追问 AI："):
            question = "请解释当前选中项目是否值得清理，以及应该如何安全判断。"
        self.ask_button.configure(state="disabled")
        self.status_var.set("正在向 AI 追问...")
        thread = threading.Thread(target=self._run_ai_question, args=(question, self.selected_item), daemon=True)
        thread.start()

    def _run_ai_question(self, question: str, selected_item: dict[str, Any] | None) -> None:
        try:
            answer = model_explainer.ask_question(
                self.analysis_result or {},
                question,
                selected_item=selected_item,
            )
            answer["question"] = question
            self.events.put(("question_answer", answer))
        except Exception as exc:
            self.events.put(("question_failed", str(exc)))

    def open_selected(self) -> None:
        self._run_selected_action("open", execute=True)

    def clean_selected(self) -> None:
        if not self.selected_item:
            return
        if not messagebox.askyesno("确认移入回收站", "确认将该项目移入回收站？清空回收站前仍可恢复。"):
            return
        self._run_selected_action("recycle_bin", execute=True)

    def _run_selected_action(self, kind: str, execute: bool) -> None:
        if not self.selected_item or not self.analysis_result:
            return
        action = find_action(self.selected_item, kind)
        if not action:
            return
        try:
            resolved = cleanup.resolve_action(self.analysis_result, action["id"])
            result = cleanup.execute(resolved) if execute else cleanup.dry_run(resolved)
            try:
                feedback_event = "recycled" if kind == "recycle_bin" and execute else "opened" if kind == "open" else kind
                model_explainer.record_feedback(self.selected_item, feedback_event)
            except Exception:
                pass
            messagebox.showinfo("操作结果", json.dumps(result, ensure_ascii=False, indent=2))
        except Exception as exc:
            messagebox.showerror("操作失败", str(exc))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Local Disk Cleaner native desktop app")
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
    return parser


def main(argv: list[str] | None = None) -> int:
    if tk is None:
        raise RuntimeError("tkinter is not available in this Python environment")
    args = build_parser().parse_args(argv)
    targets = initial_custom_targets(args.target)
    min_bytes = args.min_bytes if args.min_bytes is not None else args.min_mb * 1024 * 1024
    root = tk.Tk()
    LocalDiskCleanerWindow(
        root,
        targets=targets,
        min_bytes=min_bytes,
        limit=args.limit,
        workers=args.workers,
        scan_output=args.scan_output,
        analysis_output=args.analysis_output,
        report_output=args.report_output,
    )
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
