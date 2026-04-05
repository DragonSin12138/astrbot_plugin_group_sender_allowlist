"""群聊内按 QQ 二次白名单：仅 ``group_rules`` 中的发送者可进入后续 Star/LLM，其余在本 Handler 被钳制。

行为与配置说明见 README；只处理群消息，私聊由 AstrBot 白名单负责。
"""

from __future__ import annotations

import re
from collections import defaultdict

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

_PLUGIN_ID = "astrbot_plugin_group_sender_allowlist"
_PLUGIN_DISPLAY_NAME = "群发送者白名单"
_RULE_CONFIG_KEY = "group_rules"


def _sender_id(event: AstrMessageEvent) -> str:
    sid = event.get_sender_id()
    if sid:
        return str(sid).strip()
    sender = getattr(event.message_obj, "sender", None)
    uid = getattr(sender, "user_id", None) if sender else None
    return str(uid).strip() if uid is not None else ""


def _group_id(event: AstrMessageEvent) -> str:
    gid = event.get_group_id()
    return str(gid).strip() if gid is not None else ""


def _parse_group_rules(items: list[str]) -> dict[str, set[str]]:
    out: defaultdict[str, set[str]] = defaultdict(set)
    for raw in items or []:
        line = str(raw).strip()
        if not line or ":" not in line:
            continue
        gid, rest = line.split(":", 1)
        gid = gid.strip()
        if not gid:
            continue
        parts = {p.strip() for p in re.split(r"[,，\s]+", rest) if p.strip()}
        out[gid].update(parts)
    return dict(out)


def _should_block(event: AstrMessageEvent, star: GroupSenderAllowlistStar) -> bool:
    if not star.enabled:
        return False
    if event.get_message_type() != MessageType.GROUP_MESSAGE:
        return False
    gid, sid = _group_id(event), _sender_id(event)
    if not gid or not sid:
        if star.log_blocked and gid and not sid:
            logger.warning(
                f"[{_PLUGIN_ID}] 群消息缺少可解析的发送者 ID，未拦截（避免误伤）："
                f"group_id={gid}",
            )
        return False
    allowed = star.group_to_senders.get(gid)
    if allowed is None or sid in allowed:
        return False
    if star.admin_bypass and event.role == "admin":
        if star.log_blocked:
            logger.info(
                f"[{_PLUGIN_ID}] 管理员豁免，未拦截："
                f"group_id={gid} sender_id={sid}",
            )
        return False
    return True


class GroupSenderBlockFilter(CustomFilter):
    """群在规则中且发送者不在允许列表时为 True → 执行拦截 Handler。"""

    def __init__(self, raise_error: bool = True) -> None:
        super().__init__(raise_error=raise_error)

    def filter(self, event: AstrMessageEvent, _cfg: AstrBotConfig) -> bool:
        inst = _plugin_instance
        return inst is not None and _should_block(event, inst)


class GroupSenderAllowlistStar(Star):
    """按群配置允许的发送者 QQ，未授权成员触发本插件 Handler 后被钳制。"""

    def __init__(self, context: Context, config: PluginAstrBotConfig) -> None:
        super().__init__(context)
        self._config = config
        global _plugin_instance
        _plugin_instance = self
        self._reload_rules()

    def _reload_rules(self) -> None:
        raw = self._config.get("group_rules", []) or []
        items = raw if isinstance(raw, list) else []
        self.group_to_senders = _parse_group_rules([str(x) for x in items])

    def _cfg_bool(self, key: str, *, default: bool) -> bool:
        v = self._config.get(key)
        return default if v is None else bool(v)

    @property
    def enabled(self) -> bool:
        return self._cfg_bool("enable", default=True)

    @property
    def admin_bypass(self) -> bool:
        return self._cfg_bool("admin_bypass", default=True)

    @property
    def log_blocked(self) -> bool:
        return self._cfg_bool("log_blocked", default=False)

    async def initialize(self) -> None:
        self._reload_rules()

    async def terminate(self) -> None:
        global _plugin_instance
        if _plugin_instance is self:
            _plugin_instance = None

    def _drop_following_handlers(self, event: AstrMessageEvent) -> None:
        hs: list | None = event.get_extra("activated_handlers")
        if not hs:
            return
        me = (
            f"{self.clamp_unauthorized_group_sender.__module__}"
            f"_{self.clamp_unauthorized_group_sender.__name__}"
        )
        for i, h in enumerate(hs):
            if h.handler_full_name == me:
                del hs[i + 1 :]
                return

    @filter.custom_filter(GroupSenderBlockFilter, False, priority=_HANDLER_PRIORITY)
    async def clamp_unauthorized_group_sender(self, event: AstrMessageEvent) -> None:
        """关唤醒、关默认 LLM 链，并删掉本 Handler 之后的 Star，避免其它插件仍处理本条。"""
        gid, sid = _group_id(event), _sender_id(event)
        allowed = self.group_to_senders.get(gid) or set()
        n_allowed = len(allowed)

        event.is_at_or_wake_command = False
        event.is_wake = False
        event.call_llm = True
        self._drop_following_handlers(event)

        logger.info(
            f"[{_PLUGIN_ID}] 已拦截：插件「{_PLUGIN_DISPLAY_NAME}」· "
            f"handler=GroupSenderAllowlistStar.clamp_unauthorized_group_sender · "
            f"规则=配置项「{_RULE_CONFIG_KEY}」中群号「{gid}」· "
            f"原因=发送者「{sid}」不在该群允许 QQ 列表（该群已配置 {n_allowed} 个允许 ID；"
            "0 表示仅写了「群号:」空列表，非管理员下全员拒）",
        )
