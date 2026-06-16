#!/usr/bin/env python3
"""LLM explanation layer for Local Disk Cleaner.

The model can explain and suggest. It cannot create cleanup permissions.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
import copy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

try:
    from tools import secret_store
except ModuleNotFoundError:
    import secret_store


DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-v4-flash"
DEFAULT_TIMEOUT_SECONDS = 45
CONFIG_ENV = "LOCAL_DISK_CLEANER_CONFIG"
FEEDBACK_ENV = "LOCAL_DISK_CLEANER_FEEDBACK"


class ModelExplainerError(RuntimeError):
    pass


Transport = Callable[[str, dict[str, str], dict[str, Any], int], dict[str, Any]]


def _alias_path(path: str, home: str = "") -> str:
    normalized = str(path or "")
    if home and normalized.lower().startswith(home.lower()):
        return "%USERPROFILE%" + normalized[len(home) :]
    return normalized


def _path_segments(path_display: str) -> list[str]:
    return [part for part in str(path_display or "").replace("/", "\\").split("\\") if part]


def _source_hint(path_display: str, name: str = "") -> str:
    text = f"{path_display}\\{name}".lower()
    if any(token in text for token in ("wegameapps", "steamapps", "epic games", "riot games", "rail_apps")):
        return "game_library"
    if any(token in text for token in ("wechat files", "tencent files", "\\tencent\\", "wxwork")):
        return "chat_app_data"
    if any(token in text for token in ("downloads", "download")):
        return "download_artifact"
    if any(token in text for token in ("cache", "tmp", "temp", "__pycache__", ".cache", "dxcache", "glcache")):
        return "cache_or_temp"
    if any(token in text for token in ("desktop", "documents", "pictures", "videos")):
        return "user_file_area"
    if any(token in text for token in ("program files", "windows", "programdata")):
        return "system_or_app_area"
    return "unknown"


def item_context_features(item: dict[str, Any], home: str = "") -> dict[str, Any]:
    path = item.get("path_display") or _alias_path(item.get("path", ""), home)
    segments = _path_segments(path)
    suffixes = Path(str(item.get("path") or item.get("path_display") or item.get("name") or "")).suffixes
    extension = suffixes[-1].lower() if suffixes else ""
    drive = ""
    if segments and segments[0].endswith(":"):
        drive = segments[0]
    return {
        "drive": drive,
        "extension": extension,
        "path_segments": segments[-6:],
        "source_hint": _source_hint(path, item.get("name", "")),
        "parent_item_id": item.get("parent_item_id", ""),
        "rule_id": item.get("rule_id", ""),
        "ai_deep_scan_source": (item.get("ai_deep_scan") or {}).get("source", ""),
        "file_count": item.get("file_count", 0),
        "dir_count": item.get("dir_count", 0),
        "skipped_reparse_count": item.get("skipped_reparse_count", 0),
    }


def build_model_context(analysis_result: dict[str, Any], max_items: int = 30) -> dict[str, Any]:
    system = analysis_result.get("system", {})
    home = system.get("home", "")
    summary = analysis_result.get("summary", {})
    items = []
    for item in analysis_result.get("items", [])[:max_items]:
        items.append(
            {
                "id": item.get("id", ""),
                "tier": item.get("tier", ""),
                "name": item.get("name", ""),
                "path_display": item.get("path_display") or _alias_path(item.get("path", ""), home),
                "size_bytes": item.get("size_bytes", 0),
                "description": item.get("description", ""),
                "manual_hint": item.get("manual_hint", ""),
                "close_apps_hint": item.get("close_apps_hint", []),
                "denied_count": item.get("denied_count", 0),
                "locked_count": item.get("locked_count", 0),
                "features": item_context_features(item, home),
            }
        )
    return {
        "system": {
            "os": system.get("os", ""),
            "disk_name": system.get("disk_name", ""),
            "drives": system.get("drives", []),
        },
        "summary": {
            "overview": summary.get("overview", ""),
            "green_bytes": summary.get("green_bytes", 0),
            "reviewable_bytes": summary.get("reviewable_bytes", 0),
            "yellow_bytes": summary.get("yellow_bytes", 0),
            "red_bytes": summary.get("red_bytes", 0),
            "blue_bytes": summary.get("blue_bytes", 0),
        },
        "ai_deep_scan": analysis_result.get("ai_deep_scan", {}),
        "local_feedback": load_feedback().get("events", [])[-20:],
        "items": items,
        "policy": {
            "model_role": "explanation_only",
            "model_must_not_grant_delete_permission": True,
            "cleanup_permissions_source": analysis_result.get("action_policy", {}).get(
                "cleanup_action_source",
                "local_rules_only",
            ),
        },
    }


def build_messages(context: dict[str, Any]) -> list[dict[str, str]]:
    system_prompt = (
        "你是本地磁盘清理软件的 AI 深度分析解释层。你只能解释扫描结果、解释已整合进主列表的深扫候选、补充人工判断建议、"
        "指出风险和建议检查路径。你绝不能决定、扩大或授予删除权限；"
        "所有清理权限只来自本地规则引擎。不要输出命令，不要建议直接删除系统目录。"
        "你的输出必须是语法完全合法的 JSON 对象，不能包含 Markdown、注释或多余前后缀。"
        "可以根据 features.source_hint、扩展名、路径片段、父目录、历史反馈来判断文件来源和建议优先级。"
    )
    user_prompt = (
        "请基于下面 JSON 生成中文解释，输出严格 JSON，不要 Markdown。"
        "如果 ai_deep_scan 存在，优先说明新增候选中哪些已经通过安全校验，哪些归入“可清理但需确认”。"
        "如果不确定某项怎么解释，把自然语言放进 summary 或 item_notes.note，仍然必须保持 JSON 合法。"
        "Schema: {"
        '"summary": "一句总览洞察", '
        '"priority": ["2到5条优先建议"], '
        '"item_notes": [{"item_id": "原 item id", "note": "对该项的解释或人工判断建议"}], '
        '"item_insights": [{"item_id": "原 item id", "category": "来源分类", "confidence": 0.0, "risk": "低/中/高", "reason": "判断依据", "recommended_action": "人工处理建议"}], '
        '"long_term": ["长期优化建议"]'
        "}。输入：\n"
        + json.dumps(context, ensure_ascii=False)
    )
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]


def _strip_code_fence(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
    return cleaned


def parse_model_response(raw: str) -> dict[str, Any]:
    cleaned = _strip_code_fence(raw)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        return {
            "summary": f"模型返回的结构化 JSON 无法解析，已降级显示原始解释文本。\n\n{cleaned}",
            "priority": [],
            "item_notes": [],
            "long_term": [],
            "policy": {
                "role": "explanation_only",
                "cleanup_permissions_source": "local_rules_only",
                "parse_warning": str(exc),
            },
        }

    item_notes = []
    for note in parsed.get("item_notes", []):
        if not isinstance(note, dict):
            continue
        item_notes.append(
            {
                "item_id": str(note.get("item_id", "")),
                "note": str(note.get("note", "")),
            }
        )

    item_insights = []
    for insight in parsed.get("item_insights", []):
        if not isinstance(insight, dict):
            continue
        try:
            confidence = float(insight.get("confidence", 0) or 0)
        except (TypeError, ValueError):
            confidence = 0.0
        item_insights.append(
            {
                "item_id": str(insight.get("item_id", "")),
                "category": str(insight.get("category", "")),
                "confidence": max(0.0, min(1.0, confidence)),
                "risk": str(insight.get("risk", "")),
                "reason": str(insight.get("reason", "")),
                "recommended_action": str(insight.get("recommended_action", "")),
            }
        )

    return {
        "summary": str(parsed.get("summary", "")),
        "priority": [str(item) for item in parsed.get("priority", []) if str(item).strip()],
        "item_notes": item_notes,
        "item_insights": item_insights,
        "long_term": [str(item) for item in parsed.get("long_term", []) if str(item).strip()],
        "policy": {
            "role": "explanation_only",
            "cleanup_permissions_source": "local_rules_only",
        },
    }


def apply_explanation_to_analysis(analysis_result: dict[str, Any], explanation: dict[str, Any] | None) -> dict[str, Any]:
    enriched = copy.deepcopy(analysis_result)
    insights = {
        insight.get("item_id"): {
            "category": insight.get("category", ""),
            "confidence": insight.get("confidence", 0),
            "risk": insight.get("risk", ""),
            "reason": insight.get("reason", ""),
            "recommended_action": insight.get("recommended_action", ""),
        }
        for insight in (explanation or {}).get("item_insights", [])
        if insight.get("item_id")
    }
    notes = {
        note.get("item_id"): note.get("note", "")
        for note in (explanation or {}).get("item_notes", [])
        if note.get("item_id")
    }
    for item in enriched.get("items", []):
        item_id = item.get("id")
        if item_id in insights:
            item["ai_insight"] = insights[item_id]
        if item_id in notes:
            item.setdefault("ai_insight", {})["note"] = notes[item_id]
    policy = dict(enriched.get("action_policy", {}))
    policy["ai_can_grant_delete_permission"] = False
    enriched["action_policy"] = policy
    return enriched


def default_feedback_path() -> Path:
    try:
        home = str(Path.home())
    except RuntimeError:
        home = str(Path.cwd())
    base = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA") or home
    return Path(base) / "LocalDiskCleaner" / "feedback.json"


def load_feedback(path: str | Path | None = None) -> dict[str, Any]:
    feedback_path = Path(path or os.environ.get(FEEDBACK_ENV) or default_feedback_path())
    if not feedback_path.exists():
        return {"events": []}
    try:
        with open(feedback_path, "r", encoding="utf-8-sig") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {"events": []}
    if not isinstance(data, dict):
        return {"events": []}
    events = data.get("events", [])
    return {"events": events if isinstance(events, list) else []}


def record_feedback(item: dict[str, Any], event: str, path: str | Path | None = None) -> None:
    feedback_path = Path(path or os.environ.get(FEEDBACK_ENV) or default_feedback_path())
    feedback = load_feedback(feedback_path)
    feedback["events"].append(
        {
            "at": datetime.now(timezone.utc).isoformat(),
            "event": str(event),
            "item_id": item.get("id", ""),
            "tier": item.get("tier", ""),
            "name": item.get("name", ""),
            "path_display": item.get("path_display") or item.get("path", ""),
            "size_bytes": item.get("size_bytes", 0),
            "source_hint": item_context_features(item).get("source_hint", ""),
        }
    )
    feedback["events"] = feedback["events"][-200:]
    feedback_path.parent.mkdir(parents=True, exist_ok=True)
    feedback_path.write_text(json.dumps(feedback, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def default_config_path() -> Path:
    try:
        home = str(Path.home())
    except RuntimeError:
        home = str(Path.cwd())
    base = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA") or home
    return Path(base) / "LocalDiskCleaner" / "settings.json"


def load_local_config(path: str | Path | None = None) -> dict[str, Any]:
    config_path = Path(path or os.environ.get(CONFIG_ENV) or default_config_path())
    if not config_path.exists():
        return {}
    try:
        with open(config_path, "r", encoding="utf-8-sig") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ModelExplainerError(f"本地模型配置读取失败: {exc}") from exc
    return data if isinstance(data, dict) else {}


def resolve_api_key(api_key: str | None = None) -> str:
    if api_key:
        return api_key
    env_key = os.environ.get("DEEPSEEK_API_KEY")
    if env_key:
        return env_key
    config_key = load_local_config().get("deepseek_api_key")
    if isinstance(config_key, str) and config_key.strip():
        return config_key.strip()
    bundled_key = resolve_bundled_api_key()
    if bundled_key:
        return bundled_key
    raise ModelExplainerError("缺少 DeepSeek API Key。请在本地配置文件或 DEEPSEEK_API_KEY 中设置。")


def resolve_bundled_api_key() -> str | None:
    return secret_store.load_bundled_secret()


def http_transport(url: str, headers: dict[str, str], payload: dict[str, Any], timeout: int) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise ModelExplainerError(f"DeepSeek API HTTP {exc.code}: {detail}") from exc
    except OSError as exc:
        raise ModelExplainerError(f"DeepSeek API 请求失败: {exc}") from exc


def explain_analysis(
    analysis_result: dict[str, Any],
    *,
    transport: Transport = http_transport,
    api_key: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    key = resolve_api_key(api_key)

    context = build_model_context(analysis_result)
    payload = {
        "model": model or os.environ.get("DEEPSEEK_MODEL") or DEFAULT_MODEL,
        "messages": build_messages(context),
        "stream": False,
        "response_format": {"type": "json_object"},
        "temperature": 0.2,
    }
    endpoint = (base_url or os.environ.get("DEEPSEEK_BASE_URL") or DEFAULT_BASE_URL).rstrip("/") + "/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {key}",
    }
    response = transport(endpoint, headers, payload, timeout)
    try:
        content = response["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ModelExplainerError("DeepSeek API 返回格式无法解析") from exc
    return parse_model_response(content)


def ask_question(
    analysis_result: dict[str, Any],
    question: str,
    *,
    selected_item: dict[str, Any] | None = None,
    transport: Transport = http_transport,
    api_key: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    key = resolve_api_key(api_key)
    context = build_model_context(analysis_result, max_items=12)
    home = analysis_result.get("system", {}).get("home", "")
    selected_context = None
    if selected_item:
        selected_context = {
            "id": selected_item.get("id", ""),
            "tier": selected_item.get("tier", ""),
            "name": selected_item.get("name", ""),
            "path_display": selected_item.get("path_display") or _alias_path(selected_item.get("path", ""), home),
            "size_bytes": selected_item.get("size_bytes", 0),
            "description": selected_item.get("description", ""),
            "manual_hint": selected_item.get("manual_hint", ""),
            "ai_insight": selected_item.get("ai_insight", {}),
            "features": item_context_features(selected_item, home),
        }
    messages = [
        {
            "role": "system",
            "content": (
                "你是本地磁盘清理软件的追问解释助手。你只能解释、比较风险、建议人工检查步骤，"
                "不能授予删除权限，不能创建 action_id，不能要求用户直接删除系统目录。"
                "回答要简洁、中文、面向普通用户。"
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "question": question,
                    "selected_item": selected_context,
                    "analysis_context": context,
                    "policy": {
                        "role": "qa_explanation_only",
                        "cleanup_permissions_source": "local_rules_only",
                    },
                },
                ensure_ascii=False,
            ),
        },
    ]
    payload = {
        "model": model or os.environ.get("DEEPSEEK_MODEL") or DEFAULT_MODEL,
        "messages": messages,
        "stream": False,
        "temperature": 0.2,
    }
    endpoint = (base_url or os.environ.get("DEEPSEEK_BASE_URL") or DEFAULT_BASE_URL).rstrip("/") + "/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {key}",
    }
    response = transport(endpoint, headers, payload, timeout)
    try:
        content = response["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ModelExplainerError("DeepSeek API 返回格式无法解析") from exc
    return {
        "answer": str(content).strip(),
        "policy": {
            "role": "qa_explanation_only",
            "cleanup_permissions_source": "local_rules_only",
        },
    }
