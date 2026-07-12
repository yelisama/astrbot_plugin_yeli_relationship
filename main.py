"""astrbot_plugin_relationship - 关系本插件

在 AstrBot 上下文压缩/截断触发时，自动将 SQLite 中维护的人际关系表
注入为 system prompt；同时提供 LLM tool 与 Dashboard 页面供管理者维护。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from quart import jsonify, request

from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star, register
from astrbot.core.agent.context.manager import ContextManager
from astrbot.core.agent.message import Message
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.platform import AstrMessageEvent
from astrbot.core.provider.provider import Provider
from astrbot.core.star.filter.event_message_type import EventMessageType
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

try:
    import aiosqlite
except ImportError as exc:
    raise RuntimeError("astrbot_plugin_yeli_relationship requires aiosqlite>=0.19") from exc

PLUGIN_NAME = "astrbot_plugin_yeli_relationship"
PLUGIN_VERSION = "v1.2.0"
API_PREFIX = f"/{PLUGIN_NAME}"
REL_MARKER = "【关系本维护说明】"
NOTE_AUTO_MAX_LEN = 20
ACTIVE_UNKNOWN_LIMIT = 10
MAX_ACTIVITY_MSG_COUNT = 2000
HISTORY_SEEN_RETENTION_DAYS = 14
ACTIVE_RETENTION_DAYS = 14
HISTORY_CLEANUP_INTERVAL_SECONDS = 24 * 60 * 60
RECENT_CONTEXT_MAX_MESSAGES = 30
RECENT_CONTEXT_FOR_ANALYSIS = 15
DEFAULT_CONFIG = {
    "enabled": True,
    "mode": "auto",  # off / read_only / group_only / manual / auto
    "enable_group_whitelist": False,
    "group_whitelist": [],
    "enable_user_whitelist": False,
    "user_whitelist": [],
    "private_read_enabled": True,
    "private_stats_enabled": False,
    "inject_limit": 8,
    "scope_sharing_mode": "global_fallback",  # isolated / global_fallback / shared
    "inject_budget_mode": "balanced",  # compact / balanced / rich
    "inject_max_chars": 700,
    "inject_max_profiles": 3,
    "inject_active_profiles_enabled": False,
    "enable_relationship_self_awareness": True,
    "turn_context_cache_size": 100,
    "turn_context_ttl_seconds": 300,
    "alias_match_enabled": True,
    "alias_min_match_len": 2,
    "history_auto_scan_enabled": False,
    "history_auto_scan_interval_minutes": 360,
    "history_auto_scan_rounds": 5,
    "history_auto_scan_per_count": 20,
    "history_auto_scan_auto_profile": False,
    "history_auto_scan_group_delay_seconds": 5,
    "llm_provider_id": "",
    "llm_model": "",
    "llm_retry_times": 2,
    "webui_theme": "glass",
    "webui_dark_mode": "light",
    "auto_profile_on_activity": True,
    "relationship_auto_maintain_enabled": True,
    "relationship_auto_maintain_min_interval_seconds": 90,
    "relationship_auto_maintain_min_message_len": 6,
    "relationship_auto_maintain_confidence_threshold": 0.82,
    "relationship_auto_maintain_max_tasks": 3,
}

# ---------------------------------------------------------------------------
# 猴子补丁相关（模块级，仅安装一次）
# ---------------------------------------------------------------------------
_original_process = None
_patch_installed = False


def _is_relationship_message(msg: Message) -> bool:
    """判断一条消息是否是本插件注入的关系本 system 消息。"""
    if msg.role != "system":
        return False
    content = msg.content
    if isinstance(content, str):
        return content.startswith(REL_MARKER)
    # 也处理 list[TextPart] 的极端情况
    if isinstance(content, list) and content:
        first = content[0]
        if hasattr(first, "text") and isinstance(getattr(first, "text"), str):
            return getattr(first, "text").startswith(REL_MARKER)
    return False


async def _patched_process(self_obj, messages: list[Message], trusted_token_usage: int = 0):
    """拦截 ContextManager.process，在缓存 miss 时刷新关系本注入。"""
    plugin = RelationshipPlugin._instance

    # 判断是否需要注入
    force = plugin._pending_force_inject if plugin else False
    has_rel = any(_is_relationship_message(m) for m in messages)  # 检查原始messages

    len_before = len(messages)
    if _original_process is None:
        return messages
    result = await _original_process(self_obj, messages, trusted_token_usage)

    should_inject = force or len(result) != len_before or not has_rel
    if not should_inject:
        return result

    if plugin is None:
        return result

    # 1. 过滤掉旧的关系本 system 消息
    cleaned = [m for m in result if not _is_relationship_message(m)]

    # 2-4. 读取 DB 并组装新的关系本消息
    new_msg = await plugin.build_relationship_message(messages=cleaned)
    if new_msg is None:
        return cleaned

    # 5. 找到最后一条 system 消息，插入其后
    last_system_idx = -1
    for i, m in enumerate(cleaned):
        if m.role == "system":
            last_system_idx = i
    cleaned.insert(last_system_idx + 1, new_msg)
    plugin._pending_force_inject = False
    return cleaned


def _install_context_manager_patch() -> None:
    global _original_process, _patch_installed
    if _patch_installed:
        return
    _original_process = ContextManager.process
    ContextManager.process = _patched_process
    _patch_installed = True
    logger.info("[关系本] ContextManager.process 已打补丁")


def _uninstall_context_manager_patch() -> None:
    global _original_process, _patch_installed
    if not _patch_installed:
        return
    if ContextManager.process is _patched_process and _original_process is not None:
        ContextManager.process = _original_process
    _original_process = None
    _patch_installed = False
    logger.info("[关系本] ContextManager.process 补丁已恢复")


# ---------------------------------------------------------------------------
# 工具类
# ---------------------------------------------------------------------------
class UpdateRelationshipTool(FunctionTool):
    """update_relationship: AI 自助更新某个用户的关系字段。"""

    AI_WRITABLE_FIELDS = {"nickname", "aliases", "title_auto", "note_auto", "relation_type", "importance"}

    def __init__(self) -> None:
        super().__init__(
            name="update_relationship",
            description=(
                "更新人际关系表中的某个字段。可写字段：nickname（昵称）、aliases（别名）、"
                "title_auto（AI称呼）、note_auto（AI备注）、relation_type（关系类型）、importance（重要度）。"
                "若目标字段被管理员锁定，则无法写入。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "uid": {
                        "type": "string",
                        "description": "目标用户的 QQ 号",
                    },
                    "field": {
                        "type": "string",
                        "enum": ["nickname", "aliases", "title_auto", "note_auto", "relation_type", "importance"],
                        "description": "要更新的字段名",
                    },
                    "value": {
                        "type": "string",
                        "description": "新的值",
                    },
                    "scope_type": {
                        "type": "string",
                        "enum": ["global", "group", "private"],
                        "description": "可选作用域类型；不传则使用当前会话作用域",
                        "default": "",
                    },
                    "scope_id": {
                        "type": "string",
                        "description": "可选作用域ID；群聊为群号，私聊为用户QQ，不传则使用当前会话作用域",
                        "default": "",
                    },
                },
                "required": ["uid", "field", "value"],
            },
        )

    async def call(self, context, **kwargs) -> ToolExecResult:
        plugin = RelationshipPlugin._instance
        if plugin is None:
            return json.dumps({"success": False, "error": "插件未就绪"}, ensure_ascii=False)

        uid = str(kwargs.get("uid", "")).strip()
        field = str(kwargs.get("field", "")).strip()
        value = str(kwargs.get("value", "")).strip()

        if not uid:
            return json.dumps({"success": False, "error": "uid 不能为空"}, ensure_ascii=False)
        if field not in self.AI_WRITABLE_FIELDS:
            return json.dumps(
                {"success": False, "error": f"字段 {field} 不允许通过工具修改"},
                ensure_ascii=False,
            )
        if plugin._mode() == "manual":
            return json.dumps({"success": False, "error": "当前为 manual 模式，LLM 工具禁止写入"}, ensure_ascii=False)

        try:
            scope_type = str(kwargs.get("scope_type", "") or "").strip() or None
            scope_id = str(kwargs.get("scope_id", "") or "").strip() or None
            norm_scope_type, norm_scope_id = plugin._normalize_scope_pair(scope_type, scope_id)
            old_value = ""
            target_name = ""
            async with await plugin._get_db() as conn:
                async with conn.execute(
                    f"""
                    SELECT nickname, title_manual, title_auto, {field}
                    FROM relationship_profiles
                    WHERE scope_type = ? AND scope_id = ? AND qq_id = ?
                    """,
                    (norm_scope_type, norm_scope_id, uid),
                ) as cur:
                    old_row = await cur.fetchone()
            if old_row:
                target_name = str(old_row[0] or old_row[1] or old_row[2] or "")
                old_value = str(old_row[3] or "")
            ok, message = await plugin.update_field(
                uid,
                field,
                value,
                check_lock=True,
                scope_type=scope_type,
                scope_id=scope_id,
            )
            if ok and old_value != value:
                await plugin._record_op_log(
                    "edit", uid,
                    scope_type=norm_scope_type, scope_id=norm_scope_id,
                    target_name=target_name, field=field,
                    old_value=old_value, new_value=value,
                    actor="bot",
                )
            return json.dumps({"success": ok, "message": message}, ensure_ascii=False)
        except Exception as exc:
            logger.error(f"[关系本] update_relationship 失败: {exc}", exc_info=True)
            return json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False)


class AddUserTool(FunctionTool):
    """add_user: AI 自助添加新用户行。"""

    def __init__(self) -> None:
        super().__init__(
            name="add_user",
            description="在人际关系表中添加一条新用户记录。若该 QQ 号已存在则不会覆盖。",
            parameters={
                "type": "object",
                "properties": {
                    "uid": {
                        "type": "string",
                        "description": "新用户的 QQ 号",
                    },
                    "nickname": {
                        "type": "string",
                        "description": "用户昵称，可留空",
                        "default": "",
                    },
                    "scope_type": {
                        "type": "string",
                        "enum": ["global", "group", "private"],
                        "description": "可选作用域类型；不传则使用当前会话作用域",
                        "default": "",
                    },
                    "scope_id": {
                        "type": "string",
                        "description": "可选作用域ID；群聊为群号，私聊为用户QQ，不传则使用当前会话作用域",
                        "default": "",
                    },
                },
                "required": ["uid"],
            },
        )

    async def call(self, context, **kwargs) -> ToolExecResult:
        plugin = RelationshipPlugin._instance
        if plugin is None:
            return json.dumps({"success": False, "error": "插件未就绪"}, ensure_ascii=False)

        uid = str(kwargs.get("uid", "")).strip()
        nickname = str(kwargs.get("nickname", "")).strip()
        if not uid:
            return json.dumps({"success": False, "error": "uid 不能为空"}, ensure_ascii=False)
        if plugin._mode() == "manual":
            return json.dumps({"success": False, "error": "当前为 manual 模式，LLM 工具禁止写入"}, ensure_ascii=False)

        try:
            scope_type = str(kwargs.get("scope_type", "") or "").strip() or None
            scope_id = str(kwargs.get("scope_id", "") or "").strip() or None
            norm_scope_type, norm_scope_id = plugin._normalize_scope_pair(scope_type, scope_id)
            added = await plugin.add_user(uid, nickname, scope_type=scope_type, scope_id=scope_id)
            if added:
                await plugin._record_op_log(
                    "add", uid,
                    scope_type=norm_scope_type, scope_id=norm_scope_id,
                    target_name=nickname,
                    new_value=nickname,
                    actor="bot",
                )
            message = "添加成功" if added else "用户已存在，未覆盖"
            return json.dumps({"success": True, "added": added, "message": message}, ensure_ascii=False)
        except Exception as exc:
            logger.error(f"[关系本] add_user 失败: {exc}", exc_info=True)
            return json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False)


class QueryRelationshipTool(FunctionTool):
    """query_relationship: 查询单个用户或活跃未入册用户。"""

    def __init__(self) -> None:
        super().__init__(
            name="query_relationship",
            description=(
                "查询关系本中的用户信息，或查询最近活跃但尚未入册的用户。"
                "返回内容仅供内部参考，正常聊天不要直接展示备注。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "uid": {
                        "type": "string",
                        "description": "要查询的 QQ 号；留空时返回活跃未入册用户",
                        "default": "",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "uid 留空时返回的活跃用户数量上限",
                        "default": 10,
                    },
                },
                "required": [],
            },
        )

    async def call(self, context, **kwargs) -> ToolExecResult:
        plugin = RelationshipPlugin._instance
        if plugin is None:
            return json.dumps({"success": False, "error": "插件未就绪"}, ensure_ascii=False)

        uid = str(kwargs.get("uid", "")).strip()
        try:
            if uid:
                row = await plugin.get_user(uid)
                return json.dumps({"success": True, "user": row}, ensure_ascii=False)
            limit = int(kwargs.get("limit", 10) or 10)
            rows = await plugin.list_active_unknown(limit=limit)
            return json.dumps({"success": True, "active_unknown": rows}, ensure_ascii=False)
        except Exception as exc:
            logger.error(f"[关系本] query_relationship 失败: {exc}", exc_info=True)
            return json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False)


class ForceInjectTool(FunctionTool):
    """inject_relationship: 强制立即刷新关系本注入。"""

    def __init__(self) -> None:
        super().__init__(
            name="inject_relationship",
            description=(
                "强制立即刷新关系本注入。当你更新了关系本数据后，调用此工具可以让最新的关系本"
                "立即注入到对话上下文中，无需等待下一次上下文压缩。"
            ),
            parameters={
                "type": "object",
                "properties": {},
                "required": [],
            },
        )

    async def call(self, context, **kwargs) -> ToolExecResult:
        plugin = RelationshipPlugin._instance
        if plugin is None:
            return json.dumps({"success": False, "message": "插件未就绪"}, ensure_ascii=False)
        plugin._pending_force_inject = True
        return json.dumps(
            {"success": True, "message": "已标记强制注入，下次对话时将刷新关系本"},
            ensure_ascii=False,
        )


# ---------------------------------------------------------------------------
# 主插件类
# ---------------------------------------------------------------------------
@register(
    PLUGIN_NAME,
    "冰糖",
    "夜璃关系本：轻量联系人索引插件，支持作用域资料、别名命中、短卡注入与运行诊断。",
    PLUGIN_VERSION,
    "https://github.com/Mikachiyo/astrbot_plugin_Mikachiyo_relationship",
)
class RelationshipPlugin(Star):
    """关系本插件主类。"""

    _instance: RelationshipPlugin | None = None

    def __init__(self, context: Context, config: dict[str, Any] | None = None):
        super().__init__(context)
        self.context = context
        incoming_config = {k: v for k, v in (config or {}).items() if k in DEFAULT_CONFIG}
        self.config = {**DEFAULT_CONFIG, **incoming_config}
        self.pref_path = Path(get_astrbot_data_path()) / "plugin_data" / "relationship_webui_prefs.json"
        self._load_webui_prefs()
        self._last_scope: dict[str, str] = {
            "scope_type": "global",
            "scope_id": "global",
            "sender_id": "",
            "session_id": "",
        }
        self._last_mentions: set[str] = set()
        self._last_message_text = ""
        self._turn_context_cache: deque[dict[str, Any]] = deque()
        self._history_scan_bot: Any | None = None
        self._history_auto_scan_task: asyncio.Task | None = None
        self._history_cleanup_task: asyncio.Task | None = None
        self._recent_context: dict[str, deque[dict[str, str]]] = {}
        self._auto_maintain_tasks: set[asyncio.Task] = set()
        self._auto_maintain_last_run: dict[str, float] = {}

        # SQLite 数据库路径：data/plugin_data/relationship.db
        self.db_path = Path(get_astrbot_data_path()) / "plugin_data" / "relationship.db"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._pending_force_inject = False
        self._init_db()

        self._register_web_apis()
        self.context.add_llm_tools(UpdateRelationshipTool(), AddUserTool(), QueryRelationshipTool(), ForceInjectTool())

        _install_context_manager_patch()
        RelationshipPlugin._instance = self
        logger.info(f"[关系本] 插件已加载，数据库: {self.db_path}")

    def _load_webui_prefs(self) -> None:
        try:
            if self.pref_path.is_file():
                prefs = json.loads(self.pref_path.read_text(encoding="utf-8"))
                for k in ("webui_theme", "webui_dark_mode"):
                    if k in prefs:
                        self.config[k] = prefs[k]
        except Exception:
            pass

    def _save_webui_prefs(self) -> None:
        try:
            self.pref_path.parent.mkdir(parents=True, exist_ok=True)
            self.pref_path.write_text(
                json.dumps(
                    {k: self.config.get(k) for k in ("webui_theme", "webui_dark_mode")},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except Exception:
            pass

    # -----------------------------------------------------------------------
    # 数据库初始化与通用操作
    # -----------------------------------------------------------------------
    def _init_db(self) -> None:
        with sqlite3.connect(str(self.db_path)) as conn:
            cur = conn.cursor()
            cur.executescript(
                """
                CREATE TABLE IF NOT EXISTS active_seen_scoped (
                    scope_type TEXT NOT NULL DEFAULT 'global',
                    scope_id   TEXT NOT NULL DEFAULT 'global',
                    qq_id      TEXT NOT NULL,
                    nickname   TEXT DEFAULT '',
                    msg_count  INTEGER DEFAULT 0,
                    last_seen  TIMESTAMP,
                    PRIMARY KEY (scope_type, scope_id, qq_id)
                );

                CREATE TABLE IF NOT EXISTS active_seen_daily (
                    scope_type TEXT NOT NULL DEFAULT 'global',
                    scope_id   TEXT NOT NULL DEFAULT 'global',
                    qq_id      TEXT NOT NULL,
                    day        TEXT NOT NULL,
                    nickname   TEXT DEFAULT '',
                    msg_count  INTEGER DEFAULT 0,
                    last_seen  TIMESTAMP,
                    PRIMARY KEY (scope_type, scope_id, qq_id, day)
                );

                CREATE TABLE IF NOT EXISTS relationship_profiles (
                    scope_type    TEXT NOT NULL DEFAULT 'global',
                    scope_id      TEXT NOT NULL DEFAULT 'global',
                    qq_id         TEXT NOT NULL,
                    nickname      TEXT DEFAULT '',
                    aliases       TEXT DEFAULT '',
                    title_manual  TEXT DEFAULT '',
                    title_auto    TEXT DEFAULT '',
                    note_manual   TEXT DEFAULT '',
                    note_auto     TEXT DEFAULT '',
                    relation_type TEXT DEFAULT '',
                    importance    INTEGER DEFAULT 0,
                    msg_count     INTEGER DEFAULT 0,
                    last_seen     TIMESTAMP,
                    updated_at    TIMESTAMP,
                    source        TEXT DEFAULT 'manual',
                    confidence    REAL DEFAULT 1.0,
                    PRIMARY KEY (scope_type, scope_id, qq_id)
                );

                CREATE TABLE IF NOT EXISTS profile_locks (
                    scope_type TEXT NOT NULL DEFAULT 'global',
                    scope_id   TEXT NOT NULL DEFAULT 'global',
                    qq_id      TEXT NOT NULL,
                    field      TEXT NOT NULL,
                    locked     INTEGER DEFAULT 0,
                    updated_at TIMESTAMP,
                    PRIMARY KEY (scope_type, scope_id, qq_id, field)
                );

                CREATE TABLE IF NOT EXISTS relationship_aliases (
                    scope_type TEXT NOT NULL DEFAULT 'global',
                    scope_id   TEXT NOT NULL DEFAULT 'global',
                    qq_id      TEXT NOT NULL,
                    alias      TEXT NOT NULL,
                    source     TEXT DEFAULT 'rule',
                    confidence REAL DEFAULT 0.8,
                    hit_count  INTEGER DEFAULT 1,
                    updated_at TIMESTAMP,
                    PRIMARY KEY (scope_type, scope_id, qq_id, alias)
                );

                CREATE TABLE IF NOT EXISTS history_scan_cursor (
                    group_id        TEXT PRIMARY KEY,
                    last_message_id TEXT DEFAULT '',
                    last_scanned_at TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS history_seen_messages (
                    group_id   TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    seen_at    TIMESTAMP,
                    PRIMARY KEY (group_id, message_id)
                );

                CREATE TABLE IF NOT EXISTS relationship_op_logs (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    scope_type  TEXT NOT NULL DEFAULT 'global',
                    scope_id    TEXT NOT NULL DEFAULT 'global',
                    action      TEXT NOT NULL,
                    target_qq   TEXT NOT NULL,
                    target_name TEXT DEFAULT '',
                    field       TEXT DEFAULT '',
                    old_value   TEXT DEFAULT '',
                    new_value   TEXT DEFAULT '',
                    actor       TEXT DEFAULT 'webui',
                    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE INDEX IF NOT EXISTS idx_profiles_scope
                    ON relationship_profiles (scope_type, scope_id, importance DESC, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_profiles_qq
                    ON relationship_profiles (qq_id);
                CREATE INDEX IF NOT EXISTS idx_profile_locks_qf
                    ON profile_locks (scope_type, scope_id, qq_id, field);
                CREATE INDEX IF NOT EXISTS idx_alias_lookup
                    ON relationship_aliases (scope_type, scope_id, alias);

                CREATE INDEX IF NOT EXISTS idx_active_seen_scoped_count
                    ON active_seen_scoped (scope_type, scope_id, msg_count DESC, last_seen DESC);
                CREATE INDEX IF NOT EXISTS idx_active_seen_daily_scope_day
                    ON active_seen_daily (scope_type, scope_id, day DESC, msg_count DESC);
                CREATE INDEX IF NOT EXISTS idx_history_seen_group_seen_at
                    ON history_seen_messages (group_id, seen_at DESC);
                CREATE INDEX IF NOT EXISTS idx_relationship_op_logs_scope_time
                    ON relationship_op_logs (scope_type, scope_id, created_at DESC);
                """
            )
            self._ensure_column(cur, "profile_locks", "updated_at", "TIMESTAMP")
            conn.commit()

    async def _get_db(self):
        return aiosqlite.connect(str(self.db_path))

    @staticmethod
    def _ensure_column(cur: sqlite3.Cursor, table: str, column: str, ddl: str) -> None:
        cur.execute(f"PRAGMA table_info({table})")
        existing = {str(row[1]) for row in cur.fetchall()}
        if column not in existing:
            cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    @staticmethod
    def _normalize_id(value: Any) -> str:
        return str(value or "").strip()

    def _config_list(self, key: str) -> set[str]:
        value = self.config.get(key, [])
        if isinstance(value, str):
            value = [x.strip() for x in value.replace("，", ",").split(",")]
        if not isinstance(value, (list, tuple, set)):
            return set()
        return {str(x).strip() for x in value if str(x).strip()}

    def _mode(self) -> str:
        mode = str(self.config.get("mode", "auto") or "auto").strip().lower()
        return mode if mode in {"off", "read_only", "group_only", "manual", "auto"} else "auto"

    def _is_enabled(self) -> bool:
        return bool(self.config.get("enabled", True)) and self._mode() != "off"

    def get_llm_provider(self, *, umo: str | None = None) -> Provider:
        """获取关系本专用 LLM 提供商；未配置或不存在时回退当前会话 provider。"""
        provider_id = str(self.config.get("llm_provider_id", "") or "").strip()
        provider = None
        if provider_id and hasattr(self.context, "get_provider_by_id"):
            provider = self.context.get_provider_by_id(provider_id)
        if provider is None and hasattr(self.context, "get_using_provider"):
            try:
                provider = self.context.get_using_provider(umo=umo)
            except TypeError:
                provider = self.context.get_using_provider()
        if not isinstance(provider, Provider):
            raise RuntimeError("未配置用于关系本分析任务的 LLM 提供商")
        return provider

    def get_llm_model(self) -> str | None:
        """获取关系本专用模型名；空字符串表示使用 provider 默认模型。"""
        model = str(self.config.get("llm_model", "") or "").strip()
        return model or None

    async def llm_text_chat(self, *, system_prompt: str, prompt: str, umo: str | None = None):
        """统一的关系本 LLM 调用入口，后续自动分析都走这里。"""
        provider = self.get_llm_provider(umo=umo)
        retry_times = max(1, min(5, int(self.config.get("llm_retry_times", 2) or 2)))
        last_exc: Exception | None = None
        for _ in range(retry_times):
            try:
                kwargs = {"system_prompt": system_prompt, "prompt": prompt}
                if self.get_llm_model():
                    kwargs["model"] = self.get_llm_model()
                return await provider.text_chat(**kwargs)
            except Exception as exc:
                last_exc = exc
                await asyncio.sleep(0.2)
        raise RuntimeError(f"关系本 LLM 调用失败: {last_exc}")

    @staticmethod
    def _llm_response_text(response: Any) -> str:
        if isinstance(response, str):
            return response.strip()
        for attr in ("completion_text", "text", "content"):
            value = getattr(response, attr, None)
            if value:
                return str(value).strip()
        if isinstance(response, dict):
            for key in ("completion_text", "text", "content", "response"):
                if response.get(key):
                    return str(response[key]).strip()
        return str(response or "").strip()

    @staticmethod
    def _extract_json_object(text: str) -> dict[str, Any]:
        text = str(text or "").strip()
        if not text:
            return {}
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:].strip()
        try:
            data = json.loads(text)
            return data if isinstance(data, dict) else {}
        except Exception:
            pass
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            try:
                data = json.loads(text[start : end + 1])
                return data if isinstance(data, dict) else {}
            except Exception:
                return {}
        return {}

    def _auto_maintain_enabled(self) -> bool:
        return bool(self.config.get("relationship_auto_maintain_enabled", True)) and self._mode() == "auto"

    def _auto_maintain_int(self, key: str, default: int, *, min_value: int, max_value: int) -> int:
        try:
            value = int(self.config.get(key, default) or default)
        except Exception:
            value = default
        return max(min_value, min(max_value, value))

    def _auto_maintain_float(self, key: str, default: float, *, min_value: float, max_value: float) -> float:
        try:
            value = float(self.config.get(key, default) or default)
        except Exception:
            value = default
        return max(min_value, min(max_value, value))

    @staticmethod
    def _merge_alias_values(old_value: str, new_value: str) -> str:
        aliases: list[str] = []
        for raw in (old_value or "", new_value or ""):
            for item in str(raw).replace("，", ",").split(","):
                item = item.strip()
                if item and item not in aliases:
                    aliases.append(item)
        return ", ".join(aliases[:8])

    def _is_user_allowed(self, uid: str) -> bool:
        if not self.config.get("enable_user_whitelist", False):
            return True
        return uid in self._config_list("user_whitelist")

    def _is_group_allowed(self, group_id: str) -> bool:
        if not group_id:
            return False
        if not self.config.get("enable_group_whitelist", False):
            return True
        return group_id in self._config_list("group_whitelist")

    def _scope_from_event(self, event: AstrMessageEvent) -> dict[str, str]:
        uid = self._normalize_id(event.get_sender_id())
        session_id = str(getattr(event, "unified_msg_origin", "") or "").strip()
        is_private = False
        try:
            is_private = bool(event.is_private_chat())
        except Exception:
            is_private = False
        group_id = ""
        if not is_private:
            try:
                group_id = self._normalize_id(event.get_group_id())
            except Exception:
                group_id = ""
        if group_id:
            return {"scope_type": "group", "scope_id": group_id, "sender_id": uid, "session_id": session_id}
        return {"scope_type": "private", "scope_id": uid, "sender_id": uid, "session_id": session_id}

    def _scope_allowed(self, scope_type: str, scope_id: str, uid: str = "", *, for_write: bool = False) -> bool:
        if not self._is_enabled():
            return False
        mode = self._mode()
        if mode == "read_only" and for_write:
            return False
        if uid and not self._is_user_allowed(uid):
            return False
        if scope_type == "group":
            return self._is_group_allowed(scope_id)
        if scope_type == "private":
            if mode == "group_only":
                return False
            if for_write:
                return bool(self.config.get("private_read_enabled", True))
            return bool(self.config.get("private_read_enabled", True))
        return True

    def _set_last_scope(self, scope: dict[str, str]) -> None:
        self._last_scope = {
            "scope_type": scope.get("scope_type") or "global",
            "scope_id": scope.get("scope_id") or "global",
            "sender_id": scope.get("sender_id") or "",
            "session_id": scope.get("session_id") or "",
        }

    @staticmethod
    def _message_content_to_text(content: Any) -> str:
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if hasattr(item, "text"):
                    parts.append(str(getattr(item, "text") or ""))
                elif isinstance(item, dict) and item.get("text"):
                    parts.append(str(item.get("text") or ""))
            return " ".join(x.strip() for x in parts if x.strip())
        return str(content or "").strip()

    def _latest_user_message_text(self, messages: list[Message]) -> str:
        for msg in reversed(messages or []):
            if getattr(msg, "role", "") == "user":
                text = self._message_content_to_text(getattr(msg, "content", ""))
                if text:
                    return text
        return ""

    def _turn_context_int(self, key: str, default: int, *, min_value: int, max_value: int) -> int:
        try:
            value = int(self.config.get(key, default) or default)
        except Exception:
            value = default
        return max(min_value, min(max_value, value))

    def _turn_context_now(self) -> float:
        try:
            return asyncio.get_running_loop().time()
        except RuntimeError:
            return 0.0

    def _remember_turn_context(
        self,
        *,
        scope: dict[str, str],
        message_text: str,
        mentions: set[str],
    ) -> None:
        max_size = self._turn_context_int("turn_context_cache_size", 100, min_value=10, max_value=1000)
        item = {
            "scope": {
                "scope_type": scope.get("scope_type") or "global",
                "scope_id": scope.get("scope_id") or "global",
                "sender_id": scope.get("sender_id") or "",
                "session_id": scope.get("session_id") or "",
            },
            "message_text": str(message_text or ""),
            "mentions": {self._normalize_id(x) for x in mentions if self._normalize_id(x)},
            "created_at": self._turn_context_now(),
        }
        self._turn_context_cache.append(item)
        while len(self._turn_context_cache) > max_size:
            self._turn_context_cache.popleft()

    def _find_turn_context_for_messages(self, messages: list[Message]) -> dict[str, Any] | None:
        latest_text = self._latest_user_message_text(messages)
        now = self._turn_context_now()
        ttl = self._turn_context_int("turn_context_ttl_seconds", 300, min_value=30, max_value=3600)
        while self._turn_context_cache and now and now - float(self._turn_context_cache[0].get("created_at") or 0) > ttl:
            self._turn_context_cache.popleft()
        if latest_text:
            for item in reversed(self._turn_context_cache):
                if now and now - float(item.get("created_at") or 0) > ttl:
                    continue
                if str(item.get("message_text") or "") == latest_text:
                    return item
        return self._turn_context_cache[-1] if self._turn_context_cache else None

    def _fallback_turn_context(self) -> dict[str, Any]:
        return {
            "scope": self._last_scope or {"scope_type": "global", "scope_id": "global", "sender_id": "", "session_id": ""},
            "message_text": self._last_message_text,
            "mentions": set(self._last_mentions),
            "created_at": self._turn_context_now(),
        }

    def _extract_mentioned_ids(self, event: AstrMessageEvent) -> set[str]:
        mentioned: set[str] = set()
        try:
            message_obj = getattr(event, "message_obj", None)
            segments = getattr(message_obj, "message", []) if message_obj is not None else []
            for seg in segments or []:
                qq = getattr(seg, "qq", None)
                if qq is not None:
                    qq_text = self._normalize_id(qq)
                    if qq_text and qq_text != self._normalize_id(getattr(event, "get_self_id", lambda: "")()):
                        mentioned.add(qq_text)
        except Exception:
            pass
        text = str(getattr(event, "message_str", "") or "")
        for token in text.replace("，", " ").replace(",", " ").split():
            if token.isdigit() and 5 <= len(token) <= 12:
                mentioned.add(token)
        return mentioned

    @staticmethod
    def _shorten_text(text: str, limit: int) -> str:
        text = " ".join(str(text or "").split())
        if limit <= 0 or len(text) <= limit:
            return text
        return text[: max(1, limit - 1)] + "…"

    def _alias_min_match_len(self) -> int:
        try:
            value = int(self.config.get("alias_min_match_len", 2) or 2)
        except Exception:
            value = 2
        return max(2, min(12, value))

    def _alias_scope_where(self, target_ids_required: bool = False) -> tuple[str, list[Any]]:
        sharing_mode = self._scope_sharing_mode()
        conditions = ["(scope_type = ? AND scope_id = ?)"]
        params: list[Any] = []
        if sharing_mode in {"global_fallback", "shared"}:
            conditions.append("(scope_type = 'global' AND scope_id = 'global')")
        if sharing_mode == "shared" and target_ids_required:
            conditions.append("(scope_type IN ('group', 'private'))")
        return " OR ".join(conditions), params

    async def _alias_match_detail(self, scope_type: str, scope_id: str, text: str) -> dict[str, Any]:
        """按昵称/别名命中文本；冲突 token 会被跳过并写入诊断。"""
        text = str(text or "").strip()
        detail = {"matched": set(), "conflicts": [], "ignored_short": []}
        if not text or not self.config.get("alias_match_enabled", True):
            return detail
        min_len = self._alias_min_match_len()
        token_hits: dict[str, set[str]] = {}
        scope_conditions = ["(scope_type = ? AND scope_id = ?)"]
        params: list[Any] = [scope_type, scope_id]
        sharing_mode = self._scope_sharing_mode()
        if sharing_mode in {"global_fallback", "shared"}:
            scope_conditions.append("(scope_type = 'global' AND scope_id = 'global')")
        if sharing_mode == "shared":
            scope_conditions.append("(scope_type IN ('group', 'private'))")
        scope_where = " OR ".join(scope_conditions)
        async with await self._get_db() as conn:
            async with conn.execute(
                f"""
                SELECT qq_id, nickname, aliases
                FROM relationship_profiles
                WHERE {scope_where}
                """,
                tuple(params),
            ) as cur:
                rows = await cur.fetchall()
            for qq_id, nickname, aliases in rows:
                tokens = []
                if nickname:
                    tokens.append(str(nickname))
                if aliases:
                    tokens.extend(
                        x.strip()
                        for x in str(aliases).replace("，", ",").split(",")
                        if x.strip()
                    )
                for token in dict.fromkeys(tokens):
                    if len(token) < min_len:
                        if token and token in text:
                            detail["ignored_short"].append(token)
                        continue
                    if token in text:
                        token_hits.setdefault(token, set()).add(str(qq_id))
            async with conn.execute(
                f"""
                SELECT qq_id, alias
                FROM relationship_aliases
                WHERE {scope_where}
                """,
                tuple(params),
            ) as cur:
                alias_rows = await cur.fetchall()
            for qq_id, alias in alias_rows:
                alias_text = str(alias or "").strip()
                if len(alias_text) < min_len:
                    if alias_text and alias_text in text:
                        detail["ignored_short"].append(alias_text)
                    continue
                if alias_text in text:
                    token_hits.setdefault(alias_text, set()).add(str(qq_id))
        for token, ids in token_hits.items():
            if len(ids) == 1:
                detail["matched"].update(ids)
            else:
                detail["conflicts"].append({"token": token, "qq_ids": sorted(ids)})
        detail["ignored_short"] = sorted(set(detail["ignored_short"]))[:20]
        return detail

    async def find_profile_ids_by_text(self, scope_type: str, scope_id: str, text: str) -> set[str]:
        """按昵称/别名在当前文本中命中 profile，只读本插件数据库。"""
        detail = await self._alias_match_detail(scope_type, scope_id, text)
        return set(detail.get("matched") or set())

    async def sync_aliases_for_profile(
        self,
        uid: str,
        aliases: str,
        *,
        scope_type: str,
        scope_id: str,
        source: str = "tool",
    ) -> None:
        alias_items = [
            x.strip()
            for x in str(aliases or "").replace("，", ",").split(",")
            if x.strip()
        ]
        if not alias_items:
            return
        async with await self._get_db() as conn:
            for alias in dict.fromkeys(alias_items):
                await conn.execute(
                    """
                    INSERT INTO relationship_aliases (
                        scope_type, scope_id, qq_id, alias, source, confidence, hit_count, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 1.0, 1, CURRENT_TIMESTAMP)
                    ON CONFLICT(scope_type, scope_id, qq_id, alias) DO UPDATE SET
                        source = excluded.source,
                        confidence = excluded.confidence,
                        hit_count = relationship_aliases.hit_count + 1,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (scope_type, scope_id, uid, alias, source),
                )
            await conn.commit()

    async def list_profiles_admin(self, scope_type: str, scope_id: str) -> list[dict[str, Any]]:
        if scope_type not in {"global", "group", "private"}:
            scope_type = "global"
        if scope_type == "global":
            scope_id = "global"
        scope_id = str(scope_id or "global").strip() or "global"
        async with await self._get_db() as conn:
            async with conn.execute(
                """
                SELECT scope_type, scope_id, qq_id, nickname, aliases,
                       title_manual, title_auto, note_manual, note_auto,
                       relation_type, importance, msg_count, last_seen,
                       updated_at, source, confidence
                FROM relationship_profiles
                WHERE scope_type = ? AND scope_id = ?
                ORDER BY updated_at DESC, qq_id ASC
                """,
                (scope_type, scope_id),
            ) as cur:
                rows = await cur.fetchall()

            async with conn.execute(
                """
                SELECT qq_id, field, locked
                FROM profile_locks
                WHERE scope_type = ? AND scope_id = ?
                """,
                (scope_type, scope_id),
            ) as cur:
                lock_rows = await cur.fetchall()

        locks_map: dict[tuple[str, str], int] = {
            (str(q), str(f)): int(locked) for q, f, locked in lock_rows
        }
        fields = [
            "scope_type", "scope_id", "qq_id", "nickname", "aliases",
            "title_manual", "title_auto", "note_manual", "note_auto",
            "relation_type", "importance", "msg_count", "last_seen",
            "updated_at", "source", "confidence",
        ]
        editable_fields = [
            "nickname", "aliases", "title_manual", "title_auto", "note_manual",
            "note_auto", "relation_type", "importance", "qq_id",
        ]
        result = []
        for row in rows:
            item = dict(zip(fields, row))
            item["locks"] = {
                field: bool(locks_map.get((str(item["qq_id"]), field), 0))
                for field in editable_fields
            }
            result.append(item)
        return result

    def _normalize_scope_pair(self, scope_type: str | None, scope_id: str | None) -> tuple[str, str]:
        scope_type = str(scope_type or self._last_scope.get("scope_type") or "global").strip() or "global"
        scope_id = str(scope_id or self._last_scope.get("scope_id") or "global").strip() or "global"
        if scope_type not in {"global", "group", "private"}:
            scope_type = "global"
        if scope_type == "global":
            scope_id = "global"
        return scope_type, scope_id

    def _scope_label(self, scope_type: str, scope_id: str) -> str:
        scope_type = str(scope_type or "global")
        scope_id = str(scope_id or "global")
        if scope_type == "global":
            return "全局"
        if scope_type == "group":
            return f"群聊 {scope_id}"
        if scope_type == "private":
            return f"私聊 {scope_id}"
        return f"{scope_type} {scope_id}"

    async def _get_profile_label(self, uid: str, scope_type: str, scope_id: str) -> str:
        async with await self._get_db() as conn:
            async with conn.execute(
                """
                SELECT nickname, title_manual, title_auto
                FROM relationship_profiles
                WHERE scope_type = ? AND scope_id = ? AND qq_id = ?
                """,
                (scope_type, scope_id, uid),
            ) as cur:
                row = await cur.fetchone()
        if not row:
            return ""
        for value in row:
            value = str(value or "").strip()
            if value:
                return value
        return ""

    async def _record_op_log(
        self,
        action: str,
        uid: str,
        *,
        scope_type: str | None = None,
        scope_id: str | None = None,
        target_name: str = "",
        field: str = "",
        old_value: str = "",
        new_value: str = "",
        actor: str = "webui",
    ) -> None:
        try:
            scope_type, scope_id = self._normalize_scope_pair(scope_type, scope_id)
            uid = str(uid or "").strip()
            if not uid:
                return
            target_name = str(target_name or "").strip() or await self._get_profile_label(uid, scope_type, scope_id)
            created_at = datetime.now(timezone(timedelta(hours=8))).replace(microsecond=0).isoformat()
            async with await self._get_db() as conn:
                await conn.execute(
                    """
                    INSERT INTO relationship_op_logs (
                        scope_type, scope_id, action, target_qq, target_name,
                        field, old_value, new_value, actor, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        scope_type, scope_id, str(action or ""), uid, target_name,
                        str(field or ""), str(old_value or ""), str(new_value or ""), str(actor or "webui"), created_at,
                    ),
                )
                await conn.commit()
        except Exception as exc:
            logger.debug(f"[关系本] 写入操作日志失败: {exc}")

    async def list_op_logs_admin(
        self,
        scope_type: str,
        scope_id: str,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        scope_type = str(scope_type or "all").strip().lower()
        if scope_type != "all":
            scope_type, scope_id = self._normalize_scope_pair(scope_type, scope_id)
        try:
            limit = max(1, min(300, int(limit or 100)))
        except Exception:
            limit = 100
        async with await self._get_db() as conn:
            if scope_type == "all":
                async with conn.execute(
                    """
                    SELECT id, action, target_name, target_qq, field, old_value,
                           new_value, scope_type, scope_id, actor, created_at
                    FROM relationship_op_logs
                    ORDER BY created_at DESC, id DESC
                    LIMIT ?
                    """,
                    (limit,),
                ) as cur:
                    rows = await cur.fetchall()
            else:
                async with conn.execute(
                    """
                    SELECT id, action, target_name, target_qq, field, old_value,
                           new_value, scope_type, scope_id, actor, created_at
                    FROM relationship_op_logs
                    WHERE scope_type = ? AND scope_id = ?
                    ORDER BY created_at DESC, id DESC
                    LIMIT ?
                    """,
                    (scope_type, scope_id, limit),
                ) as cur:
                    rows = await cur.fetchall()
        return [
            {
                "id": int(row[0]),
                "action": str(row[1] or ""),
                "target_name": str(row[2] or ""),
                "target_qq": str(row[3] or ""),
                "field": str(row[4] or ""),
                "old_value": str(row[5] or ""),
                "new_value": str(row[6] or ""),
                "scope_type": str(row[7] or "global"),
                "scope_id": str(row[8] or "global"),
                "scope_label": self._scope_label(str(row[7] or "global"), str(row[8] or "global")),
                "actor": str(row[9] or "webui"),
                "timestamp": str(row[10] or ""),
            }
            for row in rows
        ]

    @staticmethod
    def _validate_field_value(field: str, value: str) -> tuple[bool, str]:
        value = str(value or "").strip()
        max_len = {
            "nickname": 32,
            "aliases": 120,
            "title_manual": 32,
            "title_auto": 32,
            "note_manual": 80,
            "note_auto": NOTE_AUTO_MAX_LEN,
            "relation_type": 24,
        }.get(field)
        if max_len and len(value) > max_len:
            if field == "note_auto":
                value = value[: NOTE_AUTO_MAX_LEN - 1] + "…"
            else:
                return False, f"{field} 长度不能超过 {max_len} 字符"
        if field in {"nickname", "title_manual", "title_auto", "relation_type"} and "\n" in value:
            return False, f"{field} 不能包含换行"
        if field == "aliases":
            aliases = [x.strip() for x in value.replace("，", ",").split(",") if x.strip()]
            if len(aliases) > 12:
                return False, "aliases 最多 12 个"
            if any(len(x) > 24 for x in aliases):
                return False, "单个 alias 不能超过 24 字符"
            value = ",".join(dict.fromkeys(aliases))
        return True, value

    async def update_field(
        self,
        uid: str,
        field: str,
        value: str,
        *,
        check_lock: bool = False,
        scope_type: str | None = None,
        scope_id: str | None = None,
    ) -> tuple[bool, str]:
        if field == "qq_id":
            return False, "QQ 号不可修改"

        allowed_dashboard_fields = {
            "nickname",
            "aliases",
            "title_manual",
            "title_auto",
            "note_manual",
            "note_auto",
            "relation_type",
            "importance",
        }
        if field not in allowed_dashboard_fields:
            return False, f"未知字段: {field}"

        scope_type = scope_type or self._last_scope.get("scope_type") or "global"
        scope_id = scope_id or self._last_scope.get("scope_id") or "global"
        if scope_type not in {"global", "group", "private"}:
            return False, "scope_type 必须是 global/group/private"
        if scope_type == "global":
            scope_id = "global"
        if not self._scope_allowed(scope_type, scope_id, uid, for_write=True):
            return False, "当前作用域未启用写入"

        async with await self._get_db() as conn:
            if check_lock:
                async with conn.execute(
                    """
                    SELECT locked FROM profile_locks
                    WHERE scope_type = ? AND scope_id = ? AND qq_id = ? AND field = ?
                    """,
                    (scope_type, scope_id, uid, field),
                ) as cur:
                    row = await cur.fetchone()
                    if row and row[0]:
                        return False, "该字段已被锁定"
            ok, value_or_error = self._validate_field_value(field, value)
            if not ok:
                return False, value_or_error
            value = value_or_error
            if field == "importance":
                try:
                    value = str(max(0, min(100, int(value))))
                except ValueError:
                    return False, "importance 必须是 0-100 的整数"

            await conn.execute(
                """
                INSERT OR IGNORE INTO relationship_profiles (
                    scope_type, scope_id, qq_id, updated_at, source, confidence
                ) VALUES (?, ?, ?, CURRENT_TIMESTAMP, 'tool', 1.0)
                """,
                (scope_type, scope_id, uid),
            )
            await conn.execute(
                f"""
                UPDATE relationship_profiles
                SET {field} = ?, updated_at = CURRENT_TIMESTAMP, source = 'tool', confidence = 1.0
                WHERE scope_type = ? AND scope_id = ? AND qq_id = ?
                """,
                (value, scope_type, scope_id, uid),
            )
            await conn.commit()
        if field == "aliases":
            await self.sync_aliases_for_profile(
                uid,
                value,
                scope_type=scope_type,
                scope_id=scope_id,
                source="tool",
            )
        return True, "更新成功"

    async def toggle_profile_lock(
        self,
        uid: str,
        field: str,
        *,
        scope_type: str | None = None,
        scope_id: str | None = None,
    ) -> tuple[bool, bool, str]:
        scope_type = scope_type or self._last_scope.get("scope_type") or "global"
        scope_id = scope_id or self._last_scope.get("scope_id") or "global"
        if scope_type not in {"global", "group", "private"}:
            scope_type = "global"
        if scope_type == "global":
            scope_id = "global"
        async with await self._get_db() as conn:
            async with conn.execute(
                """
                SELECT locked FROM profile_locks
                WHERE scope_type = ? AND scope_id = ? AND qq_id = ? AND field = ?
                """,
                (scope_type, scope_id, uid, field),
            ) as cur:
                row = await cur.fetchone()
            new_locked = 1
            if row is None:
                await conn.execute(
                    """
                    INSERT INTO profile_locks (scope_type, scope_id, qq_id, field, locked, updated_at)
                    VALUES (?, ?, ?, ?, 1, CURRENT_TIMESTAMP)
                    """,
                    (scope_type, scope_id, uid, field),
                )
            else:
                new_locked = 0 if row[0] else 1
                await conn.execute(
                    """
                    UPDATE profile_locks
                    SET locked = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE scope_type = ? AND scope_id = ? AND qq_id = ? AND field = ?
                    """,
                    (new_locked, scope_type, scope_id, uid, field),
                )
            await conn.commit()
        return True, bool(new_locked), "锁状态已切换"

    async def add_user(
        self,
        uid: str,
        nickname: str = "",
        *,
        scope_type: str | None = None,
        scope_id: str | None = None,
    ) -> bool:
        scope_type = scope_type or self._last_scope.get("scope_type") or "global"
        scope_id = scope_id or self._last_scope.get("scope_id") or "global"
        if scope_type not in {"global", "group", "private"}:
            scope_type = "global"
        if scope_type == "global":
            scope_id = "global"
        if not self._scope_allowed(scope_type, scope_id, uid, for_write=True):
            return False
        async with await self._get_db() as conn:
            cur = await conn.execute(
                """
                INSERT OR IGNORE INTO relationship_profiles (
                    scope_type, scope_id, qq_id, nickname, updated_at, source, confidence
                ) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, 'tool', 1.0)
                """,
                (scope_type, scope_id, uid, nickname),
            )
            added = getattr(cur, "rowcount", 0) == 1
            await conn.commit()
        return added

    async def get_user(self, uid: str) -> dict[str, Any] | None:
        scope_type = self._last_scope.get("scope_type") or "global"
        scope_id = self._last_scope.get("scope_id") or "global"
        async with await self._get_db() as conn:
            async with conn.execute(
                """
                SELECT scope_type, scope_id, qq_id, nickname, aliases,
                       title_manual, title_auto, note_manual, note_auto,
                       relation_type, importance, msg_count, last_seen, updated_at,
                       source, confidence
                FROM relationship_profiles
                WHERE qq_id = ? AND (
                    (scope_type = ? AND scope_id = ?)
                    OR (scope_type = 'global' AND scope_id = 'global')
                )
                ORDER BY CASE WHEN scope_type = ? AND scope_id = ? THEN 0 ELSE 1 END
                LIMIT 1
                """,
                (uid, scope_type, scope_id, scope_type, scope_id),
            ) as cur:
                row = await cur.fetchone()
        if row is None:
            return None
        fields = [
            "scope_type", "scope_id", "qq_id", "nickname", "aliases",
            "title_manual", "title_auto", "note_manual", "note_auto",
            "relation_type", "importance", "msg_count", "last_seen", "updated_at",
            "source", "confidence",
        ]
        return dict(zip(fields, row))

    async def list_active_unknown(self, limit: int = 10) -> list[dict[str, Any]]:
        limit = max(1, min(50, int(limit or 10)))
        scope_type = self._last_scope.get("scope_type") or "global"
        scope_id = self._last_scope.get("scope_id") or "global"
        if scope_type not in ("group", "private"):
            scope_type = "global"
            scope_id = "global"
        async with await self._get_db() as conn:
            async with conn.execute(
                """
                SELECT a.qq_id, a.nickname,
                       COALESCE(SUM(d.msg_count), a.msg_count) AS recent_count,
                       a.last_seen
                FROM active_seen_scoped a
                LEFT JOIN active_seen_daily d
                  ON d.scope_type = a.scope_type
                 AND d.scope_id = a.scope_id
                 AND d.qq_id = a.qq_id
                 AND d.day >= date('now', ?)
                LEFT JOIN relationship_profiles p
                  ON p.scope_type = a.scope_type
                 AND p.scope_id = a.scope_id
                 AND p.qq_id = a.qq_id
                WHERE a.scope_type = ? AND a.scope_id = ? AND p.qq_id IS NULL
                GROUP BY a.scope_type, a.scope_id, a.qq_id
                ORDER BY recent_count DESC, a.last_seen DESC
                LIMIT ?
                """,
                (f"-{ACTIVE_RETENTION_DAYS - 1} days", scope_type, scope_id, limit),
            ) as cur:
                rows = await cur.fetchall()
        return [
            {"qq_id": str(q), "nickname": n or "", "msg_count": int(c or 0), "last_seen": ls}
            for q, n, c, ls in rows
        ]

    async def list_active_candidates_scoped(
        self,
        scope_type: str,
        scope_id: str,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        if scope_type not in {"global", "group", "private"}:
            scope_type = "global"
        if scope_type == "global":
            scope_id = "global"
        scope_id = str(scope_id or "global").strip() or "global"
        limit = max(1, min(100, int(limit or 20)))
        async with await self._get_db() as conn:
            async with conn.execute(
                """
                SELECT a.qq_id, a.nickname,
                       COALESCE(SUM(d.msg_count), a.msg_count) AS recent_count,
                       a.last_seen
                FROM active_seen_scoped a
                LEFT JOIN active_seen_daily d
                  ON d.scope_type = a.scope_type
                 AND d.scope_id = a.scope_id
                 AND d.qq_id = a.qq_id
                 AND d.day >= date('now', ?)
                LEFT JOIN relationship_profiles p
                  ON p.scope_type = a.scope_type
                 AND p.scope_id = a.scope_id
                 AND p.qq_id = a.qq_id
                WHERE a.scope_type = ? AND a.scope_id = ? AND p.qq_id IS NULL
                GROUP BY a.scope_type, a.scope_id, a.qq_id
                ORDER BY recent_count DESC, a.last_seen DESC
                LIMIT ?
                """,
                (f"-{ACTIVE_RETENTION_DAYS - 1} days", scope_type, scope_id, limit),
            ) as cur:
                rows = await cur.fetchall()
        return [
            {"qq_id": str(q), "nickname": n or "", "msg_count": int(c or 0), "last_seen": ls}
            for q, n, c, ls in rows
        ]

    async def record_activity(
        self,
        uid: str,
        nickname: str = "",
        *,
        scope_type: str = "global",
        scope_id: str = "global",
        count: int = 1,
    ) -> None:
        uid = str(uid or "").strip()
        if not uid:
            return
        try:
            count = max(1, int(count))
        except Exception:
            count = 1
        nickname = str(nickname or "").strip()
        scope_type = scope_type if scope_type in {"global", "group", "private"} else "global"
        scope_id = "global" if scope_type == "global" else str(scope_id or "global").strip()
        async with await self._get_db() as conn:
            await conn.execute(
                """
                INSERT INTO active_seen_scoped (scope_type, scope_id, qq_id, nickname, msg_count, last_seen)
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(scope_type, scope_id, qq_id) DO UPDATE SET
                    nickname = excluded.nickname,
                    msg_count = MIN(?, COALESCE(active_seen_scoped.msg_count, 0) + excluded.msg_count),
                    last_seen = CURRENT_TIMESTAMP
                """,
                (scope_type, scope_id, uid, nickname, count, MAX_ACTIVITY_MSG_COUNT),
            )
            await conn.execute(
                """
                INSERT INTO active_seen_daily (scope_type, scope_id, qq_id, day, nickname, msg_count, last_seen)
                VALUES (?, ?, ?, date('now'), ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(scope_type, scope_id, qq_id, day) DO UPDATE SET
                    nickname = excluded.nickname,
                    msg_count = MIN(?, COALESCE(active_seen_daily.msg_count, 0) + excluded.msg_count),
                    last_seen = CURRENT_TIMESTAMP
                """,
                (scope_type, scope_id, uid, nickname, count, MAX_ACTIVITY_MSG_COUNT),
            )
            await conn.execute(
                """
                UPDATE relationship_profiles
                SET msg_count = MIN(?, COALESCE(msg_count, 0) + ?),
                    last_seen = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE scope_type = ? AND scope_id = ? AND qq_id = ?
                """,
                (MAX_ACTIVITY_MSG_COUNT, count, scope_type, scope_id, uid),
            )
            await conn.commit()

    async def delete_user(
        self,
        uid: str,
        *,
        scope_type: str | None = None,
        scope_id: str | None = None,
    ) -> tuple[bool, str]:
        scope_type = scope_type or self._last_scope.get("scope_type") or "global"
        scope_id = scope_id or self._last_scope.get("scope_id") or "global"
        async with await self._get_db() as conn:
            if scope_type not in {"global", "group", "private"}:
                scope_type = "global"
            if scope_type == "global":
                scope_id = "global"
            scope_id = str(scope_id or "global")
            async with conn.execute(
                """
                SELECT 1 FROM relationship_profiles
                WHERE scope_type = ? AND scope_id = ? AND qq_id = ?
                """,
                (scope_type, scope_id, uid),
            ) as cur:
                if await cur.fetchone() is None:
                    return False, "用户不存在"
            await conn.execute(
                "DELETE FROM relationship_profiles WHERE scope_type = ? AND scope_id = ? AND qq_id = ?",
                (scope_type, scope_id, uid),
            )
            await conn.execute(
                "DELETE FROM profile_locks WHERE scope_type = ? AND scope_id = ? AND qq_id = ?",
                (scope_type, scope_id, uid),
            )
            await conn.execute(
                "DELETE FROM relationship_aliases WHERE scope_type = ? AND scope_id = ? AND qq_id = ?",
                (scope_type, scope_id, uid),
            )
            await conn.commit()
        return True, "删除成功"

    # -----------------------------------------------------------------------
    # 关系本消息组装
    # -----------------------------------------------------------------------
    async def list_scoped_profiles(
        self,
        scope_type: str,
        scope_id: str,
        *,
        limit: int | None = None,
        target_ids: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        if not self._scope_allowed(scope_type, scope_id, for_write=False):
            return []
        limit = max(1, min(50, int(limit or self.config.get("inject_limit", 8) or 8)))
        target_ids = {self._normalize_id(x) for x in (target_ids or set()) if self._normalize_id(x)}
        sharing_mode = self._scope_sharing_mode()
        scope_conditions = ["(scope_type = ? AND scope_id = ?)"]
        params: list[Any] = [scope_type, scope_id]
        if sharing_mode in {"global_fallback", "shared"}:
            scope_conditions.append("(scope_type = 'global' AND scope_id = 'global')")
        if sharing_mode == "shared" and target_ids:
            scope_conditions.append("(scope_type IN ('group', 'private'))")
        where_target = ""
        if target_ids:
            placeholders = ",".join("?" for _ in target_ids)
            where_target = f" AND qq_id IN ({placeholders})"
            params.extend(sorted(target_ids))
        scope_where = " OR ".join(scope_conditions)
        params.extend([scope_type, scope_id, limit])
        async with await self._get_db() as conn:
            async with conn.execute(
                f"""
                SELECT scope_type, scope_id, qq_id, nickname, aliases,
                       title_manual, title_auto, note_manual, note_auto,
                       relation_type, importance, msg_count, last_seen,
                       updated_at, source, confidence
                FROM relationship_profiles
                WHERE ({scope_where})
                   {where_target}
                ORDER BY CASE
                         WHEN scope_type = ? AND scope_id = ? THEN 0
                         WHEN scope_type = 'global' AND scope_id = 'global' THEN 1
                         ELSE 2
                         END,
                         importance DESC, updated_at DESC, qq_id ASC
                LIMIT ?
                """,
                tuple(params),
            ) as cur:
                rows = await cur.fetchall()
        fields = [
            "scope_type", "scope_id", "qq_id", "nickname", "aliases",
            "title_manual", "title_auto", "note_manual", "note_auto",
            "relation_type", "importance", "msg_count", "last_seen",
            "updated_at", "source", "confidence",
        ]
        return [dict(zip(fields, row)) for row in rows]

    async def ensure_scoped_profile(
        self,
        uid: str,
        nickname: str = "",
        *,
        scope_type: str | None = None,
        scope_id: str | None = None,
        source: str = "rule",
    ) -> None:
        uid = self._normalize_id(uid)
        if not uid:
            return
        scope_type = scope_type or self._last_scope.get("scope_type") or "global"
        scope_id = scope_id or self._last_scope.get("scope_id") or "global"
        if not self._scope_allowed(scope_type, scope_id, uid, for_write=True):
            return
        nickname = str(nickname or "").strip()
        async with await self._get_db() as conn:
            await conn.execute(
                """
                INSERT INTO relationship_profiles (
                    scope_type, scope_id, qq_id, nickname, msg_count,
                    last_seen, updated_at, source, confidence
                ) VALUES (?, ?, ?, ?, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, ?, 0.8)
                ON CONFLICT(scope_type, scope_id, qq_id) DO UPDATE SET
                    nickname = CASE
                        WHEN excluded.nickname != '' THEN excluded.nickname
                        ELSE relationship_profiles.nickname
                    END,
                    msg_count = MIN(?, COALESCE(relationship_profiles.msg_count, 0) + 1),
                    last_seen = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (scope_type, scope_id, uid, nickname, source, MAX_ACTIVITY_MSG_COUNT),
            )
            await conn.commit()

    @staticmethod
    def _is_low_information_message(text: str) -> bool:
        text = str(text or "").strip()
        if not text:
            return True
        compact = "".join(ch for ch in text if not ch.isspace())
        if len(compact) <= 3:
            return True
        low_tokens = {
            "哈哈", "哈哈哈", "草", "艹", "啊", "嗯", "哦", "噢", "额", "呃",
            "在吗", "来了", "收到", "好的", "可以", "行", "6", "666", "？", "?",
        }
        if compact in low_tokens:
            return True
        if compact.startswith("&&") and compact.endswith("&&"):
            return True
        if len(set(compact)) <= 2 and len(compact) <= 8:
            return True
        return False

    def _context_key(self, scope_type: str, scope_id: str) -> str:
        return f"{scope_type}:{scope_id}"

    def _remember_recent_message(
        self,
        *,
        scope_type: str,
        scope_id: str,
        uid: str,
        nickname: str,
        message_text: str,
    ) -> None:
        text = str(message_text or "").strip()
        if not text:
            return
        key = self._context_key(scope_type, scope_id)
        bucket = self._recent_context.setdefault(key, deque(maxlen=RECENT_CONTEXT_MAX_MESSAGES))
        bucket.append({
            "uid": self._normalize_id(uid),
            "nickname": str(nickname or "").strip(),
            "text": text[:200],
        })

    def _recent_context_for_analysis(self, scope_type: str, scope_id: str, uid: str) -> list[dict[str, str]]:
        key = self._context_key(scope_type, scope_id)
        rows = list(self._recent_context.get(key) or [])[-RECENT_CONTEXT_FOR_ANALYSIS:]
        target_uid = self._normalize_id(uid)
        result: list[dict[str, str]] = []
        for item in rows:
            result.append({
                "speaker": item.get("nickname") or item.get("uid") or "",
                "is_target": str(item.get("uid") or "") == target_uid,
                "text": str(item.get("text") or "")[:200],
            })
        return result
    @staticmethod
    def _auto_maintain_value_allowed(field: str, value: str, current: dict[str, Any]) -> tuple[bool, str]:
        value = str(value or "").strip()
        if not value:
            return False, "空值跳过"
        if field == "nickname" and str(current.get("nickname") or "").strip():
            return False, "已有昵称，自动维护不覆盖"
        if field == "note_auto":
            banned_words = {
                "年龄", "学校", "大学", "高中", "初中", "小学", "公司", "职业", "工作", "住在", "地址",
                "身份证", "手机号", "电话", "真实姓名", "现实", "抑郁", "焦虑", "病", "家庭", "收入",
            }
            if any(word in value for word in banned_words):
                return False, "疑似现实身份或敏感信息"
        if field in {"nickname", "aliases", "title_auto", "relation_type"} and any(x in value for x in ("\n", "\r")):
            return False, "包含换行"
        return True, "允许写入"

    async def auto_maintain_relationship_from_message(
        self,
        *,
        scope_type: str,
        scope_id: str,
        uid: str,
        nickname: str,
        message_text: str,
        recent_context: list[dict[str, str]] | None = None,
        umo: str | None = None,
    ) -> None:
        if not self._auto_maintain_enabled():
            return
        uid = self._normalize_id(uid)
        message_text = str(message_text or "").strip()
        if not uid or not message_text:
            return
        if self._is_low_information_message(message_text):
            return
        min_len = self._auto_maintain_int("relationship_auto_maintain_min_message_len", 6, min_value=1, max_value=200)
        if len(message_text) < min_len:
            return
        if not self._scope_allowed(scope_type, scope_id, uid, for_write=True):
            return

        await self.ensure_scoped_profile(uid, nickname, scope_type=scope_type, scope_id=scope_id, source="activity")
        current = await self.get_user(uid) or {}
        current_view = {
            "qq_id": uid,
            "nickname": current.get("nickname") or nickname or "",
            "aliases": current.get("aliases") or "",
            "title_auto": current.get("title_auto") or "",
            "relation_type": current.get("relation_type") or "",
            "importance": current.get("importance") or 0,
            "note_auto": current.get("note_auto") or "",
        }
        truncated_message = message_text[:300]
        system_prompt = (
            "你是关系本维护器，只做证据型、保守的信息抽取。"
            "只能根据当前消息及少量最近上下文中明确出现的事实更新 bot 与发言人的关系资料。"
            "禁止把玩笑、反讽、称呼梗、临时情绪、猜测、他人转述当成稳定资料。"
            "除非发言人明确自称或管理员明确命名，否则不要更新 nickname/aliases/title_auto。"
            "不要记录真实身份、年龄、学校、职业、地区、联系方式、家庭、健康等现实隐私。"
            "note_auto 只能记录稳定事实，不能写性格推断、动机推断、关系脑补或剧情化总结。"
            "importance 只有出现明确长期关系、管理身份、项目职责时才可调整。"
            "没有明确新增信息，或只是寒暄/表情/调侃/询问/命令时，必须返回 update=false。"
            "只输出 JSON，不要输出解释。"
        )
        prompt = json.dumps(
            {
                "scope": {"type": scope_type, "id": scope_id},
                "speaker": {"qq_id": uid, "display_name": nickname or ""},
                "current_profile": current_view,
                "message": truncated_message,
                "recent_context": recent_context or [],
                "decision_rules": [
                    "需要当前消息或最近上下文里的直接证据；没有证据则 update=false",
                    "不要从单句语气推断性格、关系或重要度",
                    "不要把@、昵称玩笑、群友互怼写入资料",
                    "不要记录真实身份、年龄、学校、职业、地区、联系方式、家庭、健康等现实隐私",
                    "note_auto 必须短、事实化、可长期复用",
                    "如果只是 bot 被要求改资料，调用工具已有明确目标时才更新",
                ],
                "allowed_fields": ["nickname", "aliases", "title_auto", "note_auto", "relation_type", "importance"],
                "output_schema": {
                    "update": "bool",
                    "confidence": "0-1",
                    "fields": {
                        "nickname": "optional string",
                        "aliases": "optional comma separated string",
                        "title_auto": "optional string, how bot should address speaker",
                        "note_auto": "optional <=20 Chinese chars",
                        "relation_type": "optional short relationship label",
                        "importance": "optional int 0-100",
                    },
                    "reason": "short string",
                },
            },
            ensure_ascii=False,
        )
        try:
            response = await self.llm_text_chat(system_prompt=system_prompt, prompt=prompt, umo=umo)
            payload = self._extract_json_object(self._llm_response_text(response))
        except Exception as exc:
            logger.debug(f"[关系本] 主动维护分析跳过: {exc}")
            return

        if not payload.get("update"):
            return
        threshold = self._auto_maintain_float(
            "relationship_auto_maintain_confidence_threshold",
            0.82,
            min_value=0.0,
            max_value=1.0,
        )
        try:
            confidence = float(payload.get("confidence", 0) or 0)
        except Exception:
            confidence = 0.0
        if confidence < threshold:
            return
        fields = payload.get("fields") or {}
        if not isinstance(fields, dict):
            return
        allowed = {"nickname", "aliases", "title_auto", "note_auto", "relation_type", "importance"}
        changed: list[str] = []
        for field, raw_value in fields.items():
            if field not in allowed or raw_value is None:
                continue
            value = str(raw_value).strip()
            if not value:
                continue
            allowed_value, skip_reason = self._auto_maintain_value_allowed(field, value, current)
            if not allowed_value:
                logger.debug("[关系本] 主动维护字段跳过 uid=%s field=%s reason=%s", uid, field, skip_reason)
                continue
            if field == "aliases":
                value = self._merge_alias_values(str(current.get("aliases") or ""), value)
            ok, message = await self.update_field(
                uid,
                field,
                value,
                check_lock=True,
                scope_type=scope_type,
                scope_id=scope_id,
            )
            if ok:
                changed.append(field)
            else:
                logger.debug("[关系本] 主动维护写入跳过 uid=%s field=%s reason=%s", uid, field, message)
        if changed:
            logger.info("[关系本] 主动维护已更新 uid=%s scope=%s:%s fields=%s", uid, scope_type, scope_id, ",".join(changed))

    def _schedule_auto_maintain_relationship(
        self,
        *,
        scope: dict[str, str],
        uid: str,
        nickname: str,
        message_text: str,
    ) -> None:
        if not self._auto_maintain_enabled():
            return
        max_tasks = self._auto_maintain_int("relationship_auto_maintain_max_tasks", 3, min_value=1, max_value=10)
        self._auto_maintain_tasks = {task for task in self._auto_maintain_tasks if not task.done()}
        if len(self._auto_maintain_tasks) >= max_tasks:
            return
        key = f"{scope.get('scope_type')}:{scope.get('scope_id')}:{uid}"
        now = asyncio.get_running_loop().time()
        min_interval = self._auto_maintain_int(
            "relationship_auto_maintain_min_interval_seconds",
            90,
            min_value=10,
            max_value=86400,
        )
        if now - self._auto_maintain_last_run.get(key, 0) < min_interval:
            return
        self._auto_maintain_last_run[key] = now
        task = asyncio.create_task(
            self.auto_maintain_relationship_from_message(
                scope_type=scope.get("scope_type") or "global",
                scope_id=scope.get("scope_id") or "global",
                uid=uid,
                nickname=nickname,
                message_text=message_text,
                recent_context=self._recent_context_for_analysis(
                    scope.get("scope_type") or "global",
                    scope.get("scope_id") or "global",
                    uid,
                ),
                umo=scope.get("session_id") or None,
            )
        )
        self._auto_maintain_tasks.add(task)
        task.add_done_callback(lambda done: self._auto_maintain_tasks.discard(done))

    @staticmethod
    def _merge_title(manual: str | None, auto: str | None) -> str:
        manual = (manual or "").strip()
        auto = (auto or "").strip()
        if manual and auto:
            return f"{manual} / {auto}"
        return manual or auto or ""

    @staticmethod
    def _merge_note(manual: str | None, auto: str | None) -> str:
        manual = (manual or "").strip()
        auto = (auto or "").strip()
        if manual and auto:
            return f"{manual}；{auto}"
        return manual or auto or ""

    @staticmethod
    def _escape_md_cell(value: str) -> str:
        # 简单转义表格中的管道符与换行
        value = value.replace("|", "\\|").replace("\n", "<br>")
        return value

    def _scope_sharing_mode(self) -> str:
        mode = str(self.config.get("scope_sharing_mode", "global_fallback") or "global_fallback").strip().lower()
        return mode if mode in {"isolated", "global_fallback", "shared"} else "global_fallback"

    def _inject_budget_mode(self) -> str:
        mode = str(self.config.get("inject_budget_mode", "balanced") or "balanced").strip().lower()
        return mode if mode in {"compact", "balanced", "rich"} else "balanced"

    def _inject_profile_limit(self) -> int:
        defaults = {"compact": 1, "balanced": 3, "rich": 5}
        default_limit = defaults[self._inject_budget_mode()]
        try:
            explicit = int(self.config.get("inject_max_profiles", default_limit) or default_limit)
        except Exception:
            explicit = default_limit
        try:
            legacy = int(self.config.get("inject_limit", explicit) or explicit)
        except Exception:
            legacy = explicit
        return max(1, min(10, explicit, legacy))

    def _inject_max_chars(self) -> int:
        defaults = {"compact": 320, "balanced": 700, "rich": 1000}
        default_chars = defaults[self._inject_budget_mode()]
        try:
            value = int(self.config.get("inject_max_chars", default_chars) or default_chars)
        except Exception:
            value = default_chars
        return max(160, min(1600, value))

    @staticmethod
    def _clean_inline_text(value: Any, *, limit: int = 80) -> str:
        text = " ".join(str(value or "").replace("\n", " ").split())
        if len(text) > limit:
            return text[: max(1, limit - 1)] + "…"
        return text

    def _scope_source_label(self, row: dict[str, Any], current_scope_type: str, current_scope_id: str) -> str:
        row_scope_type = str(row.get("scope_type") or "global")
        row_scope_id = str(row.get("scope_id") or "global")
        if row_scope_type == current_scope_type and row_scope_id == current_scope_id:
            return "当前作用域"
        if row_scope_type == "global":
            return "全局资料"
        if row_scope_type == "group":
            return f"群聊 {row_scope_id}"
        if row_scope_type == "private":
            return "私聊资料"
        return f"{row_scope_type}:{row_scope_id}"

    def _relationship_row_priority(self, row: dict[str, Any], scope_type: str, scope_id: str) -> tuple[int, int, str]:
        row_scope_type = str(row.get("scope_type") or "global")
        row_scope_id = str(row.get("scope_id") or "global")
        if row_scope_type == scope_type and row_scope_id == scope_id:
            scope_rank = 0
        elif row_scope_type == "global":
            scope_rank = 1
        else:
            scope_rank = 2
        try:
            importance = int(row.get("importance") or 0)
        except Exception:
            importance = 0
        return (scope_rank, -importance, str(row.get("updated_at") or ""))

    def _dedupe_relationship_rows(
        self,
        rows: list[dict[str, Any]],
        scope_type: str,
        scope_id: str,
    ) -> list[dict[str, Any]]:
        selected: dict[str, dict[str, Any]] = {}
        for row in sorted(rows, key=lambda item: self._relationship_row_priority(item, scope_type, scope_id)):
            qq_id = str(row.get("qq_id") or "").strip()
            if qq_id and qq_id not in selected:
                selected[qq_id] = row
        return list(selected.values())

    def _relationship_card_line(
        self,
        row: dict[str, Any],
        *,
        role: str,
        scope_type: str,
        scope_id: str,
    ) -> str:
        qq_id = self._clean_inline_text(row.get("qq_id"), limit=24)
        nickname = self._clean_inline_text(row.get("nickname"), limit=24)
        aliases = self._clean_inline_text(row.get("aliases"), limit=40)
        title = self._clean_inline_text(self._merge_title(row.get("title_manual"), row.get("title_auto")), limit=32)
        relation = self._clean_inline_text(row.get("relation_type"), limit=24)
        note = self._clean_inline_text(self._merge_note(row.get("note_manual"), row.get("note_auto")), limit=48)
        source = self._scope_source_label(row, scope_type, scope_id)
        parts = [f"{role}：{qq_id}"]
        if nickname:
            parts.append(f"昵称：{nickname}")
        if aliases:
            parts.append(f"别名：{aliases}")
        if title:
            parts.append(f"称呼：{title}")
        if relation:
            parts.append(f"关系：{relation}")
        if note:
            parts.append(f"提示：{note}")
        if source != "当前作用域":
            parts.append(f"来源：{source}")
        return "- " + "｜".join(parts)

    def _apply_relationship_budget(self, lines: list[str], max_chars: int) -> tuple[str, bool]:
        kept: list[str] = []
        truncated = False
        for line in lines:
            candidate = "\n".join(kept + [line])
            if len(candidate) <= max_chars:
                kept.append(line)
                continue
            truncated = True
            break
        if truncated:
            suffix = "（已按预算省略部分关系资料）"
            candidate = "\n".join(kept + [suffix])
            if len(candidate) <= max_chars:
                kept.append(suffix)
            elif kept:
                kept[-1] = self._clean_inline_text(kept[-1], limit=max(20, max_chars - len("\n".join(kept[:-1])) - len(suffix) - 2))
                if len("\n".join(kept + [suffix])) <= max_chars:
                    kept.append(suffix)
        return "\n".join(kept), truncated

    def _render_relationship_injection(
        self,
        *,
        rows: list[dict[str, Any]],
        scope_type: str,
        scope_id: str,
        sender_id: str,
        max_chars: int,
    ) -> tuple[str, bool]:
        lines = [
            f"{REL_MARKER}",
            "【关系本-当前轮】",
        ]
        if self.config.get("enable_relationship_self_awareness", True):
            lines.append(
                "你有夜璃关系本辅助能力，可参考当前可见资料理解称呼、熟悉度和互动边界。"
            )
        lines.append("资料只作为聊天参考，不要主动解释资料来源；资料缺失时保持自然聊天，不编造具体过往或熟悉关系。")
        lines.append(f"当前作用域：{scope_type}:{scope_id}；互通策略：{self._scope_sharing_mode()}。")
        if rows:
            lines.append("")
            for row in rows:
                role = "当前对象" if sender_id and str(row.get("qq_id") or "") == sender_id else "提及对象"
                lines.append(self._relationship_card_line(row, role=role, scope_type=scope_type, scope_id=scope_id))
        else:
            lines.append("当前轮没有命中稳定关系资料；不要假装认识对方。")
        return self._apply_relationship_budget(lines, max_chars)

    async def _build_relationship_injection(
        self,
        *,
        scope: dict[str, str] | None = None,
        turn_context: dict[str, Any] | None = None,
        include_memory: bool = False,
    ) -> dict[str, Any]:
        if scope is None:
            turn_context = turn_context or self._fallback_turn_context()
            scope = dict(turn_context.get("scope") or {})
            message_text = str(turn_context.get("message_text") or "")
            mentioned = {self._normalize_id(x) for x in (turn_context.get("mentions") or set()) if self._normalize_id(x)}
        else:
            message_text = str(scope.get("message_text") or self._last_message_text or "")
            mentioned = {self._normalize_id(x) for x in (scope.get("mentions") or self._last_mentions or set()) if self._normalize_id(x)}
        scope_type = scope.get("scope_type") or "global"
        scope_id = scope.get("scope_id") or "global"
        sender_id = self._normalize_id(scope.get("sender_id") or "")
        reasons: list[str] = []
        alias_ids: set[str] = set()
        rows: list[dict[str, Any]] = []
        allowed_read = self._scope_allowed(scope_type, scope_id, sender_id, for_write=False)
        if not allowed_read:
            reasons.append("当前作用域读取被模式/白名单拦截")
        else:
            target_ids = set(mentioned)
            if sender_id:
                target_ids.add(sender_id)
            try:
                alias_detail = await self._alias_match_detail(scope_type, scope_id, message_text)
                alias_ids = set(alias_detail.get("matched") or set())
                alias_conflicts = list(alias_detail.get("conflicts") or [])
                alias_ignored_short = list(alias_detail.get("ignored_short") or [])
                target_ids.update(alias_ids)
                for item in alias_conflicts:
                    reasons.append(f"别名冲突已跳过：{item.get('token')} -> {','.join(item.get('qq_ids') or [])}")
                if alias_ignored_short:
                    reasons.append("短别名已忽略：" + "、".join(alias_ignored_short[:5]))
                profile_limit = self._inject_profile_limit()
                if target_ids:
                    rows = await self.list_scoped_profiles(scope_type, scope_id, target_ids=target_ids, limit=profile_limit * 3)
                if self.config.get("inject_active_profiles_enabled", False) and len(rows) < profile_limit:
                    top_active = await self.list_scoped_profiles(scope_type, scope_id, limit=profile_limit)
                    rows.extend(top_active)
                rows = self._dedupe_relationship_rows(rows, scope_type, scope_id)[:profile_limit]
            except Exception as exc:
                logger.error(f"[关系本] 读取关系本失败: {exc}", exc_info=True)
                reasons.append(f"读取关系资料失败: {exc}")
        if allowed_read and not rows:
            reasons.append("当前轮没有命中稳定关系资料")


        max_chars = self._inject_max_chars()
        content, truncated = self._render_relationship_injection(
            rows=rows,
            scope_type=scope_type,
            scope_id=scope_id,
            sender_id=sender_id,
            max_chars=max_chars,
        )
        return {
            "content": content,
            "rows": rows,
            "scope_type": scope_type,
            "scope_id": scope_id,
            "sender_id": sender_id,
            "allowed_read": allowed_read,
            "mentioned": mentioned,
            "alias_ids": alias_ids,
            "alias_conflicts": alias_conflicts,
            "alias_ignored_short": alias_ignored_short,
            "target_ids": ({sender_id} if sender_id else set()) | mentioned | alias_ids,
            "max_chars": max_chars,
            "char_count": len(content),
            "truncated": truncated,
            "would_inject": bool(allowed_read and (rows or self.config.get("enable_relationship_self_awareness", True))),
            "reasons": reasons,
        }

    async def build_relationship_message(self, messages: list[Message] | None = None) -> Message | None:
        turn_context = self._find_turn_context_for_messages(messages or []) if messages else None
        injection = await self._build_relationship_injection(turn_context=turn_context, include_memory=False)
        if not injection.get("would_inject"):
            logger.debug("[关系本] 跳过注入：%s", "；".join(injection.get("reasons") or []))
            return None
        return Message(role="system", content=str(injection.get("content") or ""))
    async def _call_group_history_by_bot(self, bot: Any, group_id: str, message_seq: int, count: int) -> dict[str, Any]:
        api = getattr(bot, "api", None)
        if api is not None and hasattr(api, "call_action"):
            return await api.call_action(
                "get_group_msg_history",
                group_id=group_id,
                message_seq=message_seq,
                count=count,
                reverseOrder=True,
            )
        if bot is not None and hasattr(bot, "call_action"):
            return await bot.call_action(
                "get_group_msg_history",
                group_id=group_id,
                message_seq=message_seq,
                count=count,
                reverseOrder=True,
            )
        raise RuntimeError("当前平台不支持 get_group_msg_history")

    async def _call_group_history(self, event: AstrMessageEvent, group_id: str, message_seq: int, count: int) -> dict[str, Any]:
        return await self._call_group_history_by_bot(
            getattr(event, "bot", None),
            group_id,
            message_seq,
            count,
        )

    @staticmethod
    def _history_message_text(msg: dict[str, Any]) -> str:
        parts: list[str] = []
        for seg in msg.get("message") or []:
            if not isinstance(seg, dict) or seg.get("type") != "text":
                continue
            data = seg.get("data") or {}
            text = str(data.get("text") or "").strip()
            if text:
                parts.append(text)
        return "".join(parts).strip()

    @staticmethod
    def _history_sender_info(msg: dict[str, Any]) -> tuple[str, str]:
        sender = msg.get("sender") or {}
        uid = str(sender.get("user_id") or "").strip()
        nickname = str(sender.get("card") or sender.get("nickname") or "").strip()
        return uid, nickname

    @staticmethod
    def _history_message_id(msg: dict[str, Any]) -> str:
        return str(msg.get("message_id") or msg.get("message_seq") or "").strip()

    async def _mark_history_messages_seen(self, group_id: str, message_ids: list[str]) -> set[str]:
        group_id = self._normalize_id(group_id)
        clean_ids = [str(x).strip() for x in message_ids if str(x).strip()]
        if not group_id or not clean_ids:
            return set()
        inserted: set[str] = set()
        async with await self._get_db() as conn:
            for message_id in clean_ids:
                cur = await conn.execute(
                    """
                    INSERT OR IGNORE INTO history_seen_messages (group_id, message_id, seen_at)
                    VALUES (?, ?, CURRENT_TIMESTAMP)
                    """,
                    (group_id, message_id),
                )
                if cur.rowcount:
                    inserted.add(message_id)
            await conn.commit()
        return inserted

    async def _get_history_scan_cursor(self, group_id: str) -> str:
        group_id = self._normalize_id(group_id)
        if not group_id:
            return ""
        async with await self._get_db() as conn:
            cur = await conn.execute(
                "SELECT last_message_id FROM history_scan_cursor WHERE group_id = ?",
                (group_id,),
            )
            row = await cur.fetchone()
        if not row:
            return ""
        return str(row[0] or "").strip()

    async def _update_history_scan_cursor(self, group_id: str, last_message_id: str) -> None:
        group_id = self._normalize_id(group_id)
        last_message_id = str(last_message_id or "").strip()
        if not group_id or not last_message_id:
            return
        async with await self._get_db() as conn:
            await conn.execute(
                """
                INSERT INTO history_scan_cursor (group_id, last_message_id, last_scanned_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(group_id) DO UPDATE SET
                    last_message_id = excluded.last_message_id,
                    last_scanned_at = CURRENT_TIMESTAMP
                """,
                (group_id, last_message_id),
            )
            await conn.commit()

    async def scan_group_history_activity_by_bot(
        self,
        bot: Any,
        group_id: str,
        *,
        operator_uid: str = "",
        self_id: str = "",
        max_rounds: int = 5,
        per_query_count: int = 20,
        auto_profile: bool = False,
    ) -> dict[str, Any]:
        group_id = self._normalize_id(group_id)
        if not group_id:
            return {"success": False, "error": "只能扫描有效群号"}
        operator_uid = self._normalize_id(operator_uid)
        if not self._scope_allowed("group", group_id, operator_uid, for_write=True):
            return {"success": False, "error": "当前群不在关系本白名单或无写入权限"}
        try:
            max_rounds = max(1, min(int(max_rounds), 20))
        except Exception:
            max_rounds = 5
        try:
            per_query_count = max(1, min(int(per_query_count), 50))
        except Exception:
            per_query_count = 20

        self_id = self._normalize_id(self_id)
        message_seq = 0
        scanned = 0
        duplicate_skipped = 0
        cursor_hit = False
        previous_cursor = await self._get_history_scan_cursor(group_id)
        newest_message_id = ""
        counted: dict[str, dict[str, Any]] = {}
        for _ in range(max_rounds):
            payload = await self._call_group_history_by_bot(bot, group_id, message_seq, per_query_count)
            messages = payload.get("messages") or []
            if not messages:
                break
            scanned += len(messages)
            page_ids = [self._history_message_id(msg) for msg in messages]
            if not newest_message_id:
                newest_message_id = next((x for x in page_ids if x), "")
            if previous_cursor and previous_cursor in page_ids:
                cursor_hit = True
            new_ids = await self._mark_history_messages_seen(group_id, page_ids)
            first_id = page_ids[0] if page_ids else ""
            try:
                message_seq = int(first_id or 0)
            except Exception:
                message_seq = 0
            for msg in messages:
                message_id = self._history_message_id(msg)
                if not message_id or message_id not in new_ids:
                    duplicate_skipped += 1
                    continue
                sender_id, nickname = self._history_sender_info(msg)
                if not sender_id or sender_id == self_id or not self._is_user_allowed(sender_id):
                    continue
                if not self._history_message_text(msg):
                    continue
                item = counted.setdefault(sender_id, {"nickname": nickname, "count": 0})
                if nickname:
                    item["nickname"] = nickname
                item["count"] += 1
            if cursor_hit:
                break

        await self._update_history_scan_cursor(group_id, newest_message_id)

        for sender_id, item in counted.items():
            nickname = str(item.get("nickname") or "")
            count = int(item.get("count") or 0)
            if count <= 0:
                continue
            await self.record_activity(
                sender_id,
                nickname,
                scope_type="group",
                scope_id=group_id,
                count=count,
            )
            if auto_profile:
                await self.ensure_scoped_profile(
                    sender_id,
                    nickname,
                    scope_type="group",
                    scope_id=group_id,
                    source="history_scan",
                )

        top = sorted(
            (
                {"qq_id": uid, "nickname": str(item.get("nickname") or ""), "count": int(item.get("count") or 0)}
                for uid, item in counted.items()
            ),
            key=lambda x: x["count"],
            reverse=True,
        )[:10]
        return {
            "success": True,
            "group_id": group_id,
            "scanned": scanned,
            "duplicates": duplicate_skipped,
            "cursor_hit": cursor_hit,
            "users": len(counted),
            "auto_profile": auto_profile,
            "top": top,
        }

    async def scan_group_history_activity(
        self,
        event: AstrMessageEvent,
        *,
        max_rounds: int = 5,
        per_query_count: int = 20,
        auto_profile: bool = False,
    ) -> dict[str, Any]:
        group_id = self._normalize_id(event.get_group_id())
        if not group_id:
            return {"success": False, "error": "只能在群聊中扫描"}
        self_id_getter = getattr(event, "get_self_id", None)
        self_id = self._normalize_id(self_id_getter() if callable(self_id_getter) else "")
        return await self.scan_group_history_activity_by_bot(
            getattr(event, "bot", None),
            group_id,
            operator_uid=self._normalize_id(event.get_sender_id()),
            self_id=self_id,
            max_rounds=max_rounds,
            per_query_count=per_query_count,
            auto_profile=auto_profile,
        )

    @filter.command("关系本扫描", alias={"关系本补录", "关系本扫群"})
    async def scan_relationship_history_command(self, event: AstrMessageEvent):
        """扫描当前群历史消息，补录活跃候选；加“入册”则同时创建关系本资料。"""
        try:
            is_admin = getattr(event, "is_admin", None)
            if callable(is_admin) and not is_admin():
                yield event.plain_result("只有管理员能扫描关系本历史记录。")
                return
        except Exception:
            logger.debug("[关系本] 扫描命令管理员权限检查失败，按兼容模式放行", exc_info=True)
        text = str(getattr(event, "message_str", "") or "")
        tokens = text.replace("，", " ").replace(",", " ").split()
        numbers = [int(x) for x in tokens if x.isdigit()]
        rounds = numbers[0] if numbers else 5
        per_count = numbers[1] if len(numbers) > 1 else 20
        auto_profile = any(x in text for x in ("入册", "建档", "profile"))
        try:
            result = await self.scan_group_history_activity(
                event,
                max_rounds=rounds,
                per_query_count=per_count,
                auto_profile=auto_profile,
            )
        except Exception as exc:
            logger.error(f"[关系本] 历史扫描失败: {exc}", exc_info=True)
            yield event.plain_result(f"关系本扫描失败：{exc}")
            return
        if not result.get("success"):
            yield event.plain_result(f"关系本扫描失败：{result.get('error', '未知错误')}")
            return
        top = result.get("top") or []
        top_text = "；".join(
            f"{x.get('nickname') or x.get('qq_id')}({x.get('count')})" for x in top[:5]
        ) or "无"
        mode_text = "并已入册" if result.get("auto_profile") else "已补录为活跃候选"
        yield event.plain_result(
            f"关系本扫描完成：扫描{result.get('scanned')}条，跳过重复{result.get('duplicates', 0)}条，命中{result.get('users')}人，{mode_text}。Top：{top_text}"
        )

    def _history_auto_scan_int(self, key: str, default: int, *, min_value: int, max_value: int) -> int:
        try:
            value = int(self.config.get(key, default) or default)
        except Exception:
            value = default
        return max(min_value, min(max_value, value))

    async def _history_auto_scan_once(self) -> None:
        if not self.config.get("history_auto_scan_enabled", False):
            return
        bot = self._history_scan_bot
        if bot is None:
            logger.debug("[关系本] 自动扫描等待可用 bot 实例")
            return
        if not self.config.get("enable_group_whitelist", False):
            logger.debug("[关系本] 自动扫描跳过：未启用群白名单")
            return
        group_ids = sorted(self._config_list("group_whitelist"))
        if not group_ids:
            logger.debug("[关系本] 自动扫描跳过：群白名单为空")
            return
        rounds = self._history_auto_scan_int("history_auto_scan_rounds", 5, min_value=1, max_value=20)
        per_count = self._history_auto_scan_int("history_auto_scan_per_count", 20, min_value=1, max_value=50)
        delay = self._history_auto_scan_int("history_auto_scan_group_delay_seconds", 5, min_value=0, max_value=300)
        auto_profile = bool(self.config.get("history_auto_scan_auto_profile", False))
        self_id = self._normalize_id(getattr(bot, "self_id", "") or getattr(bot, "uin", ""))
        for group_id in group_ids:
            try:
                result = await self.scan_group_history_activity_by_bot(
                    bot,
                    group_id,
                    self_id=self_id,
                    max_rounds=rounds,
                    per_query_count=per_count,
                    auto_profile=auto_profile,
                )
                if result.get("success"):
                    logger.info(
                        "[关系本] 自动扫描完成 group=%s scanned=%s duplicates=%s users=%s auto_profile=%s",
                        group_id,
                        result.get("scanned"),
                        result.get("duplicates", 0),
                        result.get("users"),
                        auto_profile,
                    )
                else:
                    logger.debug("[关系本] 自动扫描跳过 group=%s reason=%s", group_id, result.get("error"))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("[关系本] 自动扫描失败 group=%s err=%s", group_id, exc, exc_info=True)
            if delay > 0:
                await asyncio.sleep(delay)

    async def _history_auto_scan_loop(self) -> None:
        while True:
            try:
                if not self.config.get("history_auto_scan_enabled", False):
                    await asyncio.sleep(60)
                    continue
                if self._history_scan_bot is None:
                    await asyncio.sleep(60)
                    continue
                await self._history_auto_scan_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("[关系本] 自动扫描循环异常: %s", exc, exc_info=True)
            interval = self._history_auto_scan_int(
                "history_auto_scan_interval_minutes",
                360,
                min_value=5,
                max_value=10080,
            )
            await asyncio.sleep(interval * 60)

    async def cleanup_old_history_seen_messages(self) -> int:
        async with await self._get_db() as conn:
            cur = await conn.execute(
                """
                DELETE FROM history_seen_messages
                WHERE seen_at IS NOT NULL
                  AND seen_at < datetime('now', ?)
                """,
                (f"-{HISTORY_SEEN_RETENTION_DAYS} days",),
            )
            deleted = int(getattr(cur, "rowcount", 0) or 0)
            cur = await conn.execute(
                """
                DELETE FROM active_seen_daily
                WHERE day < date('now', ?)
                """,
                (f"-{ACTIVE_RETENTION_DAYS - 1} days",),
            )
            deleted_daily = int(getattr(cur, "rowcount", 0) or 0)
            await conn.commit()
        if deleted or deleted_daily:
            logger.info(
                "[关系本] 已清理历史去重 %s 条、滚动活跃日记录 %s 条",
                deleted,
                deleted_daily,
            )
        return deleted + deleted_daily

    async def _history_cleanup_loop(self) -> None:
        while True:
            try:
                await self.cleanup_old_history_seen_messages()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("[关系本] 历史消息清理失败: %s", exc, exc_info=True)
            await asyncio.sleep(HISTORY_CLEANUP_INTERVAL_SECONDS)

    def _start_history_auto_scan_task(self) -> None:
        if self._history_auto_scan_task and not self._history_auto_scan_task.done():
            return
        self._history_auto_scan_task = asyncio.create_task(self._history_auto_scan_loop())

    def _start_history_cleanup_task(self) -> None:
        if self._history_cleanup_task and not self._history_cleanup_task.done():
            return
        self._history_cleanup_task = asyncio.create_task(self._history_cleanup_loop())

    async def _stop_history_auto_scan_task(self) -> None:
        task = self._history_auto_scan_task
        self._history_auto_scan_task = None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _stop_history_cleanup_task(self) -> None:
        task = self._history_cleanup_task
        self._history_cleanup_task = None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _stop_auto_maintain_tasks(self) -> None:
        tasks = list(self._auto_maintain_tasks)
        self._auto_maintain_tasks.clear()
        for task in tasks:
            if not task.done():
                task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass

    @filter.event_message_type(EventMessageType.ALL, priority=100)
    async def track_activity(self, event: AstrMessageEvent):
        """轻量记录当前会话作用域；群聊按白名单统计，私聊默认不统计。"""
        try:
            bot = getattr(event, "bot", None)
            if bot is not None:
                self._history_scan_bot = bot
            scope = self._scope_from_event(event)
            self._set_last_scope(scope)
            self._last_message_text = str(getattr(event, "message_str", "") or "")
            self._last_mentions = self._extract_mentioned_ids(event)
            self._remember_turn_context(
                scope=scope,
                message_text=self._last_message_text,
                mentions=self._last_mentions,
            )
            uid = scope.get("sender_id", "")
            nickname = str(event.get_sender_name() or "").strip()
            scope_type = scope.get("scope_type", "global")
            scope_id = scope.get("scope_id", "global")
            if not self._scope_allowed(scope_type, scope_id, uid, for_write=True):
                logger.debug(f"[关系本] 跳过活跃统计：作用域不允许写入 {scope_type}:{scope_id} uid={uid}")
                return
            self._remember_recent_message(
                scope_type=scope_type,
                scope_id=scope_id,
                uid=uid,
                nickname=nickname,
                message_text=self._last_message_text,
            )
            if scope_type == "private" and not self.config.get("private_stats_enabled", False):
                logger.debug(f"[关系本] 跳过活跃统计：私聊自动统计关闭 uid={uid}")
                return
            if self.config.get("auto_profile_on_activity", True):
                await self.ensure_scoped_profile(
                    uid,
                    nickname,
                    scope_type=scope_type,
                    scope_id=scope_id,
                    source="activity",
                )
            if scope_type in {"group", "private"}:
                await self.record_activity(
                    uid,
                    nickname,
                    scope_type=scope_type,
                    scope_id=scope_id,
                )
            self._schedule_auto_maintain_relationship(
                scope=scope,
                uid=uid,
                nickname=nickname,
                message_text=self._last_message_text,
            )
        except Exception as exc:
            logger.debug(f"[关系本] 记录活跃失败: {exc}")

    # -----------------------------------------------------------------------
    # Web API 路由
    # -----------------------------------------------------------------------
    def _register_web_apis(self) -> None:
        if not hasattr(self.context, "register_web_api"):
            logger.warning("[关系本] 当前 AstrBot 版本不支持 register_web_api，跳过注册")
            return

        register = self.context.register_web_api
        routes = [
            ("relationship", self.api_get_all, ["GET", "POST"], "获取关系本全表"),
            ("relationship/update", self.api_update, ["POST"], "更新关系本字段"),
            ("relationship/clear_auto", self.api_clear_auto_fields, ["POST"], "清空自动维护字段"),
            ("relationship/lock", self.api_lock, ["POST"], "切换字段锁定"),
            ("relationship/add", self.api_add, ["POST"], "新增用户行"),
            ("relationship/delete", self.api_delete, ["POST"], "删除用户行"),
            ("relationship/config", self.api_config, ["GET", "POST"], "读取或更新关系本轻量配置"),
            ("relationship/active", self.api_active_candidates, ["POST"], "获取当前作用域活跃候选"),
            ("relationship/diagnose", self.api_diagnose, ["POST"], "诊断当前作用域关系本状态"),
            ("relationship/logs", self.api_logs, ["POST"], "获取关系本操作日志"),
            ("relationship/maintenance", self.api_maintenance, ["POST"], "获取关系本维护状态"),
            ("relationship/force_inject", self.api_force_inject, ["POST"], "手动注入"),
        ]
        for path, handler, methods, desc in routes:
            register(f"{API_PREFIX}/{path}", handler, methods, desc)

    async def api_get_all(self):
        try:
            data = {}
            if request.method == "POST":
                data = await request.get_json(silent=True) or {}
            scope_type = str(data.get("scope_type") or request.args.get("scope_type", "") or "").strip()
            scope_id = str(data.get("scope_id") or request.args.get("scope_id", "") or "").strip()
            rows = await self.list_profiles_admin(scope_type or "global", scope_id or "global")
            return jsonify({"success": True, "rows": rows})
        except Exception as exc:
            logger.error(f"[关系本] api_get_all 失败: {exc}", exc_info=True)
            return jsonify({"success": False, "error": str(exc)}), 500

    async def api_update(self):
        try:
            data = await request.get_json() or {}
            uid = str(data.get("qq_id", "")).strip()
            field = str(data.get("field", "")).strip()
            value = data.get("value", "")
            if value is not None:
                value = str(value)
            else:
                value = ""

            if not uid:
                return jsonify({"success": False, "error": "qq_id 不能为空"}), 400
            if not field:
                return jsonify({"success": False, "error": "field 不能为空"}), 400
            allowed_fields = {
                "nickname", "aliases", "title_manual", "title_auto",
                "note_manual", "note_auto", "relation_type", "importance",
            }
            if field not in allowed_fields:
                return jsonify({"success": False, "error": f"未知字段: {field}"}), 400

            scope_type = str(data.get("scope_type", "") or "").strip() or None
            scope_id = str(data.get("scope_id", "") or "").strip() or None
            norm_scope_type, norm_scope_id = self._normalize_scope_pair(scope_type, scope_id)
            old_value = ""
            target_name = ""
            async with await self._get_db() as conn:
                async with conn.execute(
                    f"""
                    SELECT nickname, title_manual, title_auto, {field}
                    FROM relationship_profiles
                    WHERE scope_type = ? AND scope_id = ? AND qq_id = ?
                    """,
                    (norm_scope_type, norm_scope_id, uid),
                ) as cur:
                    old_row = await cur.fetchone()
            if old_row:
                target_name = str(old_row[0] or old_row[1] or old_row[2] or "")
                old_value = str(old_row[3] or "")
            ok, message = await self.update_field(
                uid,
                field,
                value,
                check_lock=False,
                scope_type=scope_type,
                scope_id=scope_id,
            )
            if ok:
                await self._record_op_log(
                    "edit", uid,
                    scope_type=norm_scope_type, scope_id=norm_scope_id,
                    target_name=target_name, field=field,
                    old_value=old_value, new_value=value,
                )
            status = 200 if ok else 400
            return jsonify({"success": ok, "message": message}), status
        except Exception as exc:
            logger.error(f"[关系本] api_update 失败: {exc}", exc_info=True)
            return jsonify({"success": False, "error": str(exc)}), 500


    async def api_clear_auto_fields(self):
        try:
            data = await request.get_json() or {}
            uid = str(data.get("qq_id", "")).strip()
            if not uid:
                return jsonify({"success": False, "error": "qq_id 不能为空"}), 400

            scope_type = str(data.get("scope_type", "") or "").strip() or None
            scope_id = str(data.get("scope_id", "") or "").strip() or None
            norm_scope_type, norm_scope_id = self._normalize_scope_pair(scope_type, scope_id)
            if not self._scope_allowed(norm_scope_type, norm_scope_id, uid, for_write=True):
                return jsonify({"success": False, "error": "当前作用域未启用写入"}), 403

            async with await self._get_db() as conn:
                async with conn.execute(
                    """
                    SELECT nickname, title_manual, title_auto, note_manual, note_auto
                    FROM relationship_profiles
                    WHERE scope_type = ? AND scope_id = ? AND qq_id = ?
                    """,
                    (norm_scope_type, norm_scope_id, uid),
                ) as cur:
                    row = await cur.fetchone()
                if row is None:
                    return jsonify({"success": False, "error": "用户不存在"}), 404

                target_name = str(row[0] or row[1] or row[2] or "")
                old_values = {
                    "title_auto": str(row[2] or ""),
                    "note_auto": str(row[4] or ""),
                }
                changed = [field for field, value in old_values.items() if value]
                if changed:
                    await conn.execute(
                        """
                        UPDATE relationship_profiles
                        SET title_auto = '', note_auto = '', updated_at = CURRENT_TIMESTAMP
                        WHERE scope_type = ? AND scope_id = ? AND qq_id = ?
                        """,
                        (norm_scope_type, norm_scope_id, uid),
                    )
                    await conn.commit()

            if changed:
                await self._record_op_log(
                    "clear_auto", uid,
                    scope_type=norm_scope_type, scope_id=norm_scope_id,
                    target_name=target_name, field="title_auto,note_auto",
                    old_value=json.dumps(old_values, ensure_ascii=False), new_value="",
                )
            return jsonify({"success": True, "cleared_fields": changed, "message": "自动字段已清空" if changed else "没有可清空的自动字段"})
        except Exception as exc:
            logger.error(f"[关系本] api_clear_auto_fields 失败: {exc}", exc_info=True)
            return jsonify({"success": False, "error": str(exc)}), 500
    async def api_lock(self):
        try:
            data = await request.get_json() or {}
            uid = str(data.get("qq_id", "")).strip()
            field = str(data.get("field", "")).strip()
            if not uid:
                return jsonify({"success": False, "error": "qq_id 不能为空"}), 400
            if not field:
                return jsonify({"success": False, "error": "field 不能为空"}), 400

            scope_type = str(data.get("scope_type", "") or "").strip() or None
            scope_id = str(data.get("scope_id", "") or "").strip() or None
            norm_scope_type, norm_scope_id = self._normalize_scope_pair(scope_type, scope_id)
            ok, locked, message = await self.toggle_profile_lock(
                uid,
                field,
                scope_type=scope_type,
                scope_id=scope_id,
            )
            if ok:
                await self._record_op_log(
                    "lock" if locked else "unlock", uid,
                    scope_type=norm_scope_type, scope_id=norm_scope_id,
                    field=field,
                    new_value="已锁定" if locked else "已解锁",
                )
            return jsonify({"success": ok, "locked": locked, "message": message})
        except Exception as exc:
            logger.error(f"[关系本] api_lock 失败: {exc}", exc_info=True)
            return jsonify({"success": False, "error": str(exc)}), 500

    async def api_add(self):
        try:
            data = await request.get_json() or {}
            uid = str(data.get("qq_id", "")).strip()
            nickname = str(data.get("nickname", "")).strip()
            if not uid:
                return jsonify({"success": False, "error": "qq_id 不能为空"}), 400

            scope_type = str(data.get("scope_type", "") or "").strip() or None
            scope_id = str(data.get("scope_id", "") or "").strip() or None
            norm_scope_type, norm_scope_id = self._normalize_scope_pair(scope_type, scope_id)
            added = await self.add_user(uid, nickname, scope_type=scope_type, scope_id=scope_id)
            if added:
                await self._record_op_log(
                    "add", uid,
                    scope_type=norm_scope_type, scope_id=norm_scope_id,
                    target_name=nickname,
                    new_value=nickname,
                )
            message = "添加成功" if added else "用户已存在"
            return jsonify({"success": True, "added": added, "message": message})
        except Exception as exc:
            logger.error(f"[关系本] api_add 失败: {exc}", exc_info=True)
            return jsonify({"success": False, "error": str(exc)}), 500

    async def api_delete(self):
        try:
            data = await request.get_json() or {}
            uid = str(data.get("qq_id", "")).strip()
            if not uid:
                return jsonify({"success": False, "error": "qq_id 不能为空"}), 400

            scope_type = str(data.get("scope_type", "") or "").strip() or None
            scope_id = str(data.get("scope_id", "") or "").strip() or None
            norm_scope_type, norm_scope_id = self._normalize_scope_pair(scope_type, scope_id)
            target_name = await self._get_profile_label(uid, norm_scope_type, norm_scope_id)
            ok, message = await self.delete_user(uid, scope_type=scope_type, scope_id=scope_id)
            if ok:
                await self._record_op_log(
                    "delete", uid,
                    scope_type=norm_scope_type, scope_id=norm_scope_id,
                    target_name=target_name,
                )
            status = 200 if ok else 404
            return jsonify({"success": ok, "message": message}), status
        except Exception as exc:
            logger.error(f"[关系本] api_delete 失败: {exc}", exc_info=True)
            return jsonify({"success": False, "error": str(exc)}), 500

    async def api_logs(self):
        try:
            data = await request.get_json() or {}
            scope_type = str(data.get("scope_type", "global") or "global").strip()
            scope_id = str(data.get("scope_id", "global") or "global").strip()
            limit = int(data.get("limit", 120) or 120)
            rows = await self.list_op_logs_admin(scope_type, scope_id, limit=limit)
            return jsonify({"success": True, "rows": rows})
        except Exception as exc:
            logger.error(f"[关系本] api_logs 失败: {exc}", exc_info=True)
            return jsonify({"success": False, "error": str(exc)}), 500


    async def api_maintenance(self):
        try:
            data = await request.get_json() or {}
            action = str(data.get("action", "status") or "status").strip().lower()
            cleanup_deleted = 0
            if action == "cleanup":
                cleanup_deleted = await self.cleanup_old_history_seen_messages()

            async def count_rows(conn, table: str) -> int:
                async with conn.execute(f"SELECT COUNT(*) FROM {table}") as cur:
                    row = await cur.fetchone()
                return int(row[0] or 0) if row else 0

            async with await self._get_db() as conn:
                counts = {
                    "profiles": await count_rows(conn, "relationship_profiles"),
                    "active_seen": await count_rows(conn, "active_seen_scoped"),
                    "active_daily": await count_rows(conn, "active_seen_daily"),
                    "aliases": await count_rows(conn, "relationship_aliases"),
                    "locks": await count_rows(conn, "profile_locks"),
                    "op_logs": await count_rows(conn, "relationship_op_logs"),
                    "history_seen": await count_rows(conn, "history_seen_messages"),
                }
                async with conn.execute("SELECT MAX(created_at) FROM relationship_op_logs") as cur:
                    latest_log_row = await cur.fetchone()
                async with conn.execute("SELECT MAX(seen_at) FROM history_seen_messages") as cur:
                    latest_seen_row = await cur.fetchone()

            running_auto_tasks = sum(1 for task in self._auto_maintain_tasks if not task.done())
            db_size = self.db_path.stat().st_size if self.db_path.exists() else 0
            maintenance = {
                "plugin_version": PLUGIN_VERSION,
                "database_path": str(self.db_path),
                "database_bytes": db_size,
                "counts": counts,
                "cleanup_deleted": cleanup_deleted,
                "history_seen_retention_days": HISTORY_SEEN_RETENTION_DAYS,
                "active_retention_days": ACTIVE_RETENTION_DAYS,
                "history_cleanup_interval_seconds": HISTORY_CLEANUP_INTERVAL_SECONDS,
                "history_cleanup_task_running": bool(self._history_cleanup_task and not self._history_cleanup_task.done()),
                "history_auto_scan_task_running": bool(self._history_auto_scan_task and not self._history_auto_scan_task.done()),
                "auto_maintain_running_tasks": running_auto_tasks,
                "auto_maintain_task_limit": self._auto_maintain_int(
                    "relationship_auto_maintain_max_tasks",
                    3,
                    min_value=1,
                    max_value=10,
                ),
                "turn_context_cache_size": len(self._turn_context_cache),
                "turn_context_cache_limit": self._turn_context_int("turn_context_cache_size", 100, min_value=10, max_value=1000),
                "latest_log_at": str(latest_log_row[0] or "") if latest_log_row else "",
                "latest_history_seen_at": str(latest_seen_row[0] or "") if latest_seen_row else "",
            }
            return jsonify({"success": True, "maintenance": maintenance})
        except Exception as exc:
            logger.error(f"[relationship] api_maintenance failed: {exc}", exc_info=True)
            return jsonify({"success": False, "error": str(exc)}), 500

    async def api_config(self):
        try:
            if request.method == "GET":
                return jsonify({"success": True, "config": self.config, "version": PLUGIN_VERSION})
            data = await request.get_json() or {}
            allowed = set(DEFAULT_CONFIG.keys())
            for key, value in data.items():
                if key in allowed:
                    self.config[key] = value
            if any(k in data for k in ("webui_theme", "webui_dark_mode")):
                self._save_webui_prefs()
            return jsonify({"success": True, "config": self.config, "version": PLUGIN_VERSION})
        except Exception as exc:
            logger.error(f"[关系本] api_config 失败: {exc}", exc_info=True)
            return jsonify({"success": False, "error": str(exc)}), 500

    async def api_active_candidates(self):
        try:
            data = await request.get_json() or {}
            scope_type = str(data.get("scope_type", "global") or "global").strip()
            scope_id = str(data.get("scope_id", "global") or "global").strip()
            limit = int(data.get("limit", 20) or 20)
            rows = await self.list_active_candidates_scoped(scope_type, scope_id, limit=limit)
            return jsonify({"success": True, "rows": rows})
        except Exception as exc:
            logger.error(f"[关系本] api_active_candidates 失败: {exc}", exc_info=True)
            return jsonify({"success": False, "error": str(exc)}), 500

    async def api_diagnose(self):
        try:
            data = await request.get_json() or {}
            scope_type = str(data.get("scope_type", "") or self._last_scope.get("scope_type") or "global").strip()
            scope_id = str(data.get("scope_id", "") or self._last_scope.get("scope_id") or "global").strip()
            if scope_type not in {"global", "group", "private"}:
                scope_type = "global"
            if scope_type == "global":
                scope_id = "global"
            sender_id = self._normalize_id(data.get("sender_id", "") or self._last_scope.get("sender_id") or "")
            diagnose_scope = {
                "scope_type": scope_type,
                "scope_id": scope_id,
                "sender_id": sender_id,
                "session_id": self._last_scope.get("session_id") or "",
                "message_text": self._last_message_text,
                "mentions": set(self._last_mentions),
            }
            injection = await self._build_relationship_injection(scope=diagnose_scope, include_memory=False)
            allowed_read = bool(injection.get("allowed_read"))
            allowed_write = self._scope_allowed(scope_type, scope_id, sender_id, for_write=True)
            active_rows = await self.list_active_candidates_scoped(scope_type, scope_id, limit=20)
            mentioned = set(injection.get("mentioned") or set())
            alias_ids = set(injection.get("alias_ids") or set())
            alias_conflicts = list(injection.get("alias_conflicts") or [])
            alias_ignored_short = list(injection.get("alias_ignored_short") or [])
            target_ids = set(injection.get("target_ids") or set())
            matched_rows = list(injection.get("rows") or [])
            reasons = list(injection.get("reasons") or [])
            if not self._is_enabled():
                reasons.append("插件总开关关闭或 mode=off")
            if scope_type == "private" and not self.config.get("private_stats_enabled", False):
                reasons.append("私聊自动统计关闭")
            if allowed_read and active_rows and not matched_rows:
                reasons.append("只看到活跃候选，尚未转入关系资料")
            diagnosis = {
                "plugin_version": PLUGIN_VERSION,
                "enabled": self._is_enabled(),
                "mode": self._mode(),
                "scope_type": scope_type,
                "scope_id": scope_id,
                "sender_id": sender_id,
                "session_id": self._last_scope.get("session_id") or "",
                "allowed_read": allowed_read,
                "allowed_write": allowed_write,
                "sharing_mode": self._scope_sharing_mode(),
                "inject_budget_mode": self._inject_budget_mode(),
                "inject_max_chars": int(injection.get("max_chars") or self._inject_max_chars()),
                "inject_max_profiles": self._inject_profile_limit(),
                "inject_active_profiles_enabled": bool(self.config.get("inject_active_profiles_enabled", False)),
                "self_awareness_enabled": bool(self.config.get("enable_relationship_self_awareness", True)),
                "turn_context_cache_size": len(self._turn_context_cache),
                "turn_context_cache_limit": self._turn_context_int("turn_context_cache_size", 100, min_value=10, max_value=1000),
                "turn_context_ttl_seconds": self._turn_context_int("turn_context_ttl_seconds", 300, min_value=30, max_value=3600),
                "last_mentions": sorted(mentioned),
                "alias_hits": sorted(alias_ids),
                "alias_conflicts": alias_conflicts,
                "alias_ignored_short": alias_ignored_short,
                "alias_min_match_len": self._alias_min_match_len(),
                "target_ids": sorted(target_ids),
                "matched_count": len(matched_rows),
                "fallback_count": 0,
                "active_candidate_count": len(active_rows),
                "would_inject": bool(injection.get("would_inject")),
                "inject_char_count": int(injection.get("char_count") or 0),
                "inject_truncated": bool(injection.get("truncated")),
                "inject_preview": str(injection.get("content") or ""),
                "matched_profiles": [
                    {
                        "scope_type": str(row.get("scope_type") or ""),
                        "scope_id": str(row.get("scope_id") or ""),
                        "qq_id": str(row.get("qq_id") or ""),
                        "nickname": str(row.get("nickname") or ""),
                    }
                    for row in matched_rows
                ],
                "reasons": reasons or ["当前作用域状态正常"],
            }
            logger.debug(f"[关系本] 诊断结果: {diagnosis}")
            return jsonify({"success": True, "diagnosis": diagnosis, "active_rows": active_rows})
        except Exception as exc:
            logger.error(f"[关系本] api_diagnose 失败: {exc}", exc_info=True)
            return jsonify({"success": False, "error": str(exc)}), 500
    async def api_force_inject(self):
        try:
            self._pending_force_inject = True
            return jsonify({"success": True, "message": "已标记强制注入，下次对话生效"})
        except Exception as exc:
            logger.error(f"[关系本] api_force_inject 失败: {exc}", exc_info=True)
            return jsonify({"success": False, "error": str(exc)}), 500

    async def initialize(self) -> bool:
        self._start_history_auto_scan_task()
        self._start_history_cleanup_task()
        logger.info("[关系本] 插件初始化完成")
        return True

    async def terminate(self) -> None:
        await self._stop_history_auto_scan_task()
        await self._stop_history_cleanup_task()
        await self._stop_auto_maintain_tasks()
        if RelationshipPlugin._instance is self:
            RelationshipPlugin._instance = None
            _uninstall_context_manager_patch()
        logger.info("[关系本] 插件已停止")
