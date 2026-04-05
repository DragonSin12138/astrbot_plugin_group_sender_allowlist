"""群内发送者白名单（README 模式 1～3 与本插件关系见下）。

- 模式 1（会话隔离）：主要由 AstrBot id_whitelist + unique_session 完成按人会话；若你在白名单里
  仍写了裸 group_id 导致全员过闸，可借助本插件按群配置 group_rules 做二次过滤。本插件不改会话 ID。
- 模式 2（关隔离、仅白名单）：未在 group_rules 中出现的群，本插件不介入（见 README「一、易混场景」）。
- 模式 3 / 多群（示例 D）：关 unique_session，id_whitelist 放行各群；group_rules 每群一行
  「群号:QQ,...」，群内允许列表中的成员共享该群会话上下文，未列出成员被拦截。

仅处理 MessageType.GROUP_MESSAGE；私聊完全交给 AstrBot 白名单。
"""

from __future__ import annotations

import re

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.config import AstrBotConfig
from astrbot.core.config.astrbot_config import AstrBotConfig as PluginAstrBotConfig
from astrbot.core.platform.message_type import MessageType
from astrbot.core.star.filter.custom_filter import CustomFilter

_plugin_instance: GroupSenderAllowlistStar | None = None

# 需高于常见第三方插件，否则拦截前其它 Star 已跑完
_HANDLER_PRIORITY = 50_000_000

# 与 metadata.yaml 一致，便于在 AstrBot 日志中 grep 与排查
_PLUGIN_ID = "astrbot_plugin_group_sender_allowlist"
_PLUGIN_DISPLAY_NAME = "群发送者白名单"
_RULE_CONFIG_KEY = "group_rules"


def _effective_sender_id(event: AstrMessageEvent) -> str:
    """AstrBot get_sender_id 仅在 user_id 为 str 时返回值，int 时会变成空串，这里补一层兼容。"""
    sid = event.get_sender_id()
    if sid:
        return str(sid).strip()
    sender = getattr(event.message_obj, "sender", None)
    uid = getattr(sender, "user_id", None) if sender else None
    if uid is not None:
        return str(uid).strip()
    return ""


def _effective_group_id(event: AstrMessageEvent) -> str:
    gid = event.get_group_id()
    return str(gid).strip() if gid is not None else ""


def _parse_group_rules(items: list[str]) -> dict[str, set[str]]:
    """解析 '群号:qq1,qq2' -> {群号: {qq}}"""
    out: dict[str, set[str]] = {}
    for raw in items or []:
        line = str(raw).strip()
        if not line or ":" not in line:
            continue
        gid, rest = line.split(":", 1)
        gid = gid.strip()
        if not gid:
            continue
        ids = {p.strip() for p in re.split(r"[,，\s]+", rest) if p.strip()}
        if gid not in out:
            out[gid] = set()
        out[gid].update(ids)
    return out


class GroupSenderBlockFilter(CustomFilter):
    """群聊 + 群在规则中 + 发送者不在允许列表 → True（执行拦截）。"""

    def __init__(self, raise_error: bool = True) -> None:
        super().__init__(raise_error=raise_error)

    def filter(self, event: AstrMessageEvent, _cfg: AstrBotConfig) -> bool:
        inst = _plugin_instance
        if inst is None or not inst.raw_enable:
            return False
        if event.get_message_type() != MessageType.GROUP_MESSAGE:
            return False
        gid = _effective_group_id(event)
        sid = _effective_sender_id(event)
        if not gid or not sid:
            if inst.log_blocked and gid and not sid:
                logger.warning(
                    f"[{_PLUGIN_ID}] 群消息缺少可解析的发送者 ID，未拦截（避免误伤）："
                    f"group_id={gid}",
                )
            return False
        allowed = inst.group_to_senders.get(gid)
        if allowed is None:
            return False

        if sid in allowed:
            return False
        if inst.admin_bypass_enabled and event.role == "admin":
            if inst.log_blocked:
                logger.info(
                    f"[{_PLUGIN_ID}] 管理员豁免，未拦截："
                    f"group_id={gid} sender_id={sid}",
                )
            return False
        return True


class GroupSenderAllowlistStar(Star):
    """群成员白名单（发送者维度）。"""

    def __init__(self, context: Context, config: PluginAstrBotConfig):
        super().__init__(context)
        self._config = config
        global _plugin_instance
        _plugin_instance = self
        self._reload_rules()

    def _reload_rules(self) -> None:
        items = self._config.get("group_rules", []) or []
        if not isinstance(items, list):
            items = []
        self.group_to_senders = _parse_group_rules([str(x) for x in items])

    @property
    def raw_enable(self) -> bool:
        v = self._config.get("enable")
        return True if v is None else bool(v)

    @property
    def admin_bypass_enabled(self) -> bool:
        v = self._config.get("admin_bypass")
        return True if v is None else bool(v)

    @property
    def log_blocked(self) -> bool:
        return bool(self._config.get("log_blocked"))

    async def initialize(self) -> None:
        self._reload_rules()

    async def terminate(self) -> None:
        global _plugin_instance
        if _plugin_instance is self:
            _plugin_instance = None

    @filter.custom_filter(GroupSenderBlockFilter, False, priority=_HANDLER_PRIORITY)
    async def clamp_unauthorized_group_sender(self, event: AstrMessageEvent) -> None:
        """关闭唤醒、跳过默认 LLM，并裁掉后续 Star，避免其它插件仍处理本条消息。"""
        gid = _effective_group_id(event)
        sid = _effective_sender_id(event)
        allowed = self.group_to_senders.get(gid) or set()
        n_allowed = len(allowed)

        event.is_at_or_wake_command = False
        event.is_wake = False
        event.call_llm = True

        hs: list | None = event.get_extra("activated_handlers")
        if hs:
            me = (
                f"{self.clamp_unauthorized_group_sender.__module__}"
                f"_{self.clamp_unauthorized_group_sender.__name__}"
            )
            try:
                idx = next(i for i, h in enumerate(hs) if h.handler_full_name == me)
            except StopIteration:
                idx = -1
            if idx >= 0:
                del hs[idx + 1 :]

        logger.info(
            f"[{_PLUGIN_ID}] 已拦截：插件「{_PLUGIN_DISPLAY_NAME}」· "
            f"handler=GroupSenderAllowlistStar.clamp_unauthorized_group_sender · "
            f"规则=配置项「{_RULE_CONFIG_KEY}」中群号「{gid}」· "
            f"原因=发送者「{sid}」不在该群允许 QQ 列表（该群已配置 {n_allowed} 个允许 ID；"
            "0 表示仅写了「群号:」空列表，非管理员下全员拒）",
        )
