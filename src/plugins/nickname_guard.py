"""群昵称保护插件。

监听群名片（群昵称）变更事件，检测到机器人自己的群名片被修改后，
自动将群名片改回与机器人 QQ 昵称一致；与协议端建立连接或重连时，
也会检查机器人所在的全部群并恢复异常的群昵称。通过
NICKNAME_GUARD_DISABLED_GROUPS 环境变量可配置不启用本功能的群号列表。
"""

from typing import Any, Literal

from nonebot import get_driver, get_plugin_config, logger, on_notice
from nonebot.adapters.onebot.v11 import Bot, NoticeEvent
from nonebot.adapters.onebot.v11.adapter import Adapter as OneBot11Adapter
from nonebot.adapters.onebot.v11.exception import ActionFailed, NetworkError
from nonebot.plugin import PluginMetadata
from pydantic import BaseModel, field_validator

__plugin_meta__ = PluginMetadata(
    name="群昵称保护",
    description="机器人的群昵称被他人修改或重连时，自动改回与自己的昵称一致",
    usage="自动生效，无触发指令；可通过 NICKNAME_GUARD_DISABLED_GROUPS 配置不启用的群",
    type="application",
    supported_adapters={"~onebot.v11"},
)


class GroupCardNoticeEvent(NoticeEvent):
    """群名片（群昵称）变更通知事件。

    OneBot v11 标准事件（notice_type 为 group_card），当前版本的适配器
    事件模型中未内置该事件，需在此自定义并注册后才会被解析为该类型。
    """

    notice_type: Literal["group_card"]
    user_id: int
    group_id: int
    card_new: str
    card_old: str = ""

    def get_user_id(self) -> str:
        return str(self.user_id)

    def get_session_id(self) -> str:
        return f"group_{self.group_id}_{self.user_id}"


OneBot11Adapter.add_custom_model(GroupCardNoticeEvent)


class Config(BaseModel):
    """群昵称保护插件配置。

    可在 `.env.{environment}` 文件中通过 NICKNAME_GUARD_DISABLED_GROUPS 配置，
    缺失或为空时不对任何群禁用，即所有群均启用保护。
    """

    nickname_guard_disabled_groups: list[int] | None = None
    """不启用群昵称保护的群号列表（JSON 数组格式），缺失或为空时所有群均启用。"""

    @field_validator("nickname_guard_disabled_groups", mode="before")
    @classmethod
    def blank_as_none(cls, value: Any) -> Any:
        """将空字符串视为未配置，避免变量留空导致插件加载失败。"""
        if isinstance(value, str) and not value.strip():
            return None
        return value


plugin_config = get_plugin_config(Config)

# 不启用该功能的群号集合
_disabled_groups: frozenset[int] = frozenset(
    plugin_config.nickname_guard_disabled_groups or []
)

if _disabled_groups:
    logger.info(f"群昵称保护已启用，以下群不启用该功能：{sorted(_disabled_groups)}")
else:
    logger.info("群昵称保护已启用，所有群均受保护")

nickname_guard = on_notice()


async def _fetch_bot_nickname(bot: Bot) -> str:
    """获取机器人 QQ 昵称，获取失败或为空时返回空字符串。"""
    try:
        info = await bot.call_api("get_login_info")
    except (ActionFailed, NetworkError) as exc:
        logger.warning(f"获取机器人昵称失败：{exc}")
        return ""
    return str((info or {}).get("nickname") or "").strip()


async def _restore_group_card(bot: Bot, group_id: int, nickname: str) -> bool:
    """检查指定群内机器人的群名片，异常时恢复为昵称，返回是否执行了恢复。"""
    self_id = int(bot.self_id)
    try:
        member = await bot.call_api(
            "get_group_member_info", group_id=group_id, user_id=self_id
        )
    except (ActionFailed, NetworkError) as exc:
        logger.warning(f"获取机器人在群 {group_id} 的群成员信息失败：{exc}")
        return False
    card = str((member or {}).get("card") or "")
    if not card or card == nickname:
        return False
    try:
        await bot.call_api(
            "set_group_card", group_id=group_id, user_id=self_id, card=nickname
        )
    except (ActionFailed, NetworkError) as exc:
        logger.warning(f"恢复机器人在群 {group_id} 的群昵称失败：{exc}")
        return False
    logger.info(f"机器人在群 {group_id} 的群昵称（{card!r}）已恢复为 {nickname!r}")
    return True


async def _restore_all_group_cards(bot: Bot) -> None:
    """连接建立（或重连）后，检查并恢复机器人在全部群的群昵称。"""
    nickname = await _fetch_bot_nickname(bot)
    if not nickname:
        return
    try:
        groups = await bot.call_api("get_group_list")
    except (ActionFailed, NetworkError) as exc:
        logger.warning(f"获取群列表失败：{exc}")
        return
    restored = 0
    for group in groups or []:
        group_id = group.get("group_id")
        if not isinstance(group_id, int) or group_id in _disabled_groups:
            continue
        if await _restore_group_card(bot, group_id, nickname):
            restored += 1
    if restored:
        logger.info(f"连接检查完成，已恢复 {restored} 个群的群昵称")
    else:
        logger.info("连接检查完成，所有群的群昵称均正常")


driver = get_driver()


@driver.on_bot_connect
async def _restore_cards_on_connect(bot: Bot) -> None:
    """与协议端建立连接（或重连）时，检查并恢复全部群的群昵称。"""
    await _restore_all_group_cards(bot)


@nickname_guard.handle()
async def handle_group_card_notice(bot: Bot, event: NoticeEvent) -> None:
    """机器人的群名片被修改时，改回与 QQ 昵称一致。"""
    if not isinstance(event, GroupCardNoticeEvent):
        return
    if str(event.user_id) != bot.self_id or event.group_id in _disabled_groups:
        return
    if not event.card_new:
        # 群名片为空时群内显示的就是 QQ 昵称，无需处理
        return

    nickname = await _fetch_bot_nickname(bot)
    if not nickname or event.card_new == nickname:
        return

    try:
        await bot.call_api(
            "set_group_card",
            group_id=event.group_id,
            user_id=event.user_id,
            card=nickname,
        )
    except (ActionFailed, NetworkError) as exc:
        logger.warning(f"恢复机器人在群 {event.group_id} 的群昵称失败：{exc}")
        return
    logger.info(
        f"机器人在群 {event.group_id} 的群昵称被修改"
        f"（{event.card_old!r} -> {event.card_new!r}），已恢复为 {nickname!r}"
    )
