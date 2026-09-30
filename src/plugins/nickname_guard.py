"""群昵称保护插件。

监听群名片（群昵称）变更事件，检测到机器人自己的群名片被修改后，
自动将群名片改回与机器人 QQ 昵称一致；机器人每次连接协议端（包括
掉线重连）、协议端重新登录 QQ 账号时，以及按固定间隔定时检查时，
也会检查机器人所在的全部群并恢复异常的群昵称。通过
NICKNAME_GUARD_DISABLED_GROUPS、NICKNAME_GUARD_CHECK_INTERVAL
环境变量可配置不启用本功能的群号列表与定时检查间隔。
"""

from typing import Any, Literal

from nonebot import (
    get_bots,
    get_driver,
    get_plugin_config,
    logger,
    on_metaevent,
    on_notice,
)
from nonebot.adapters.onebot.v11 import Bot, Event, LifecycleMetaEvent, NoticeEvent
from nonebot.adapters.onebot.v11.adapter import Adapter as OneBot11Adapter
from nonebot.adapters.onebot.v11.exception import ActionFailed, NetworkError
from nonebot.plugin import PluginMetadata
from nonebot_plugin_apscheduler import scheduler
from pydantic import BaseModel, field_validator

__plugin_meta__ = PluginMetadata(
    name="群昵称保护",
    description="机器人的群昵称被修改、机器人上线或协议端重新登录后，自动改回与昵称一致",
    usage=(
        "自动生效，无触发指令；可通过 NICKNAME_GUARD_DISABLED_GROUPS"
        " 与 NICKNAME_GUARD_CHECK_INTERVAL 配置"
    ),
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

    可在 `.env.{environment}` 文件中通过 NICKNAME_GUARD_* 变量配置，
    缺失或为空时使用默认行为（所有群均启用、每 120 秒定时检查一次）。
    """

    nickname_guard_disabled_groups: list[int] | None = None
    """不启用群昵称保护的群号列表（JSON 数组格式），缺失或为空时所有群均启用。"""

    nickname_guard_check_interval: int | None = None
    """定时检查各群群昵称的间隔秒数，0 表示禁用定时检查，缺失时默认 120。"""

    @field_validator(
        "nickname_guard_disabled_groups",
        "nickname_guard_check_interval",
        mode="before",
    )
    @classmethod
    def blank_as_none(cls, value: Any) -> Any:
        """将空字符串视为未配置，避免变量留空导致插件加载失败。"""
        if isinstance(value, str) and not value.strip():
            return None
        return value


plugin_config = get_plugin_config(Config)

# 定时检查的默认间隔（秒）。群名片通知事件可能延迟送达甚至漏发，
# 定时检查用于兜底，确保群昵称最终恢复一致
_DEFAULT_CHECK_INTERVAL = 120


def _check_interval_or_default(value: int | None) -> int:
    """返回定时检查的间隔秒数：0 表示禁用，未配置用默认值，负数回退默认值。"""
    if value is None:
        return _DEFAULT_CHECK_INTERVAL
    if value < 0:
        logger.warning(
            f"群昵称保护配置 NICKNAME_GUARD_CHECK_INTERVAL={value} 无效"
            f"（需为非负整数，0 表示禁用定时检查），已使用默认值"
            f" {_DEFAULT_CHECK_INTERVAL} 秒"
        )
        return _DEFAULT_CHECK_INTERVAL
    return value


_check_interval = _check_interval_or_default(
    plugin_config.nickname_guard_check_interval
)

# 不启用该功能的群号集合
_disabled_groups: frozenset[int] = frozenset(
    plugin_config.nickname_guard_disabled_groups or []
)

if _disabled_groups:
    logger.info(f"群昵称保护已启用，以下群不启用该功能：{sorted(_disabled_groups)}")
else:
    logger.info("群昵称保护已启用，所有群均受保护")

if _check_interval > 0:
    logger.info(f"群昵称保护每 {_check_interval} 秒定时检查一次各群的群昵称")
else:
    logger.info("群昵称保护的定时检查已禁用（NICKNAME_GUARD_CHECK_INTERVAL=0）")

nickname_guard = on_notice()


async def _is_lifecycle_connect(event: Event) -> bool:
    """仅匹配协议端（重新）登录 QQ 账号成功后的 lifecycle connect 元事件。"""
    return isinstance(event, LifecycleMetaEvent) and event.sub_type == "connect"


# 协议端（重新）登录 QQ 账号后触发一次全量检查；用 rule 过滤掉
# heartbeat 等其余元事件，避免每个心跳都进入响应器产生日志
nickname_guard_lifecycle = on_metaevent(rule=_is_lifecycle_connect)


async def _fetch_bot_nickname(bot: Bot) -> str:
    """获取机器人 QQ 昵称，获取失败或为空时返回空字符串。"""
    try:
        info = await bot.call_api("get_login_info")
    except (ActionFailed, NetworkError) as exc:
        logger.warning(f"获取机器人昵称失败：{exc}")
        return ""
    return str((info or {}).get("nickname") or "").strip()


# 协议端（NapCat）在群成员不存在时返回的错误码
_MEMBER_NOT_FOUND_RETCODE = 1200


def _is_member_missing(exc: ActionFailed) -> bool:
    """判断接口报错是否属于“群成员不存在”（机器人不在该群或协议端缓存异常）。"""
    if exc.info.get("retcode") == _MEMBER_NOT_FOUND_RETCODE:
        return True
    return "不存在" in str(exc.info.get("message") or "")


async def _restore_group_card(bot: Bot, group_id: int, nickname: str) -> bool:
    """检查指定群内机器人的群名片，异常时恢复为昵称，返回是否执行了恢复。"""
    self_id = int(bot.self_id)
    try:
        # no_cache=True 强制实时查询：协议端的成员缓存可能滞后，
        # 缓存中的旧名片会导致检测不到他人对群昵称的修改
        member = await bot.call_api(
            "get_group_member_info",
            group_id=group_id,
            user_id=self_id,
            no_cache=True,
        )
    except (ActionFailed, NetworkError) as exc:
        if isinstance(exc, ActionFailed) and _is_member_missing(exc):
            logger.debug(f"机器人不在群 {group_id} 中，跳过该群的群昵称检查")
        else:
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
        if isinstance(exc, ActionFailed) and _is_member_missing(exc):
            logger.debug(f"机器人不在群 {group_id} 中，无法恢复群昵称")
        else:
            logger.warning(f"恢复机器人在群 {group_id} 的群昵称失败：{exc}")
        return False
    logger.info(f"机器人在群 {group_id} 的群昵称（{card!r}）已恢复为 {nickname!r}")
    return True


async def _restore_all_group_cards(bot: Bot) -> None:
    """检查并恢复机器人在全部群（禁用群除外）的群昵称。"""
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
        logger.info(f"群昵称检查完成，已恢复 {restored} 个群的群昵称")
    else:
        logger.debug("群昵称检查完成，所有群的群昵称均正常")


async def _check_all_group_cards() -> None:
    """定时检查并恢复所有已连接机器人的群昵称（兜底，防通知事件延迟或漏收）。"""
    for bot in get_bots().values():
        if isinstance(bot, Bot):
            await _restore_all_group_cards(bot)


driver = get_driver()


@driver.on_bot_connect
async def _restore_cards_on_connect(bot: Bot) -> None:
    """与协议端建立连接（或重连）时，检查并恢复全部群的群昵称。"""
    logger.info("机器人已连接协议端，开始检查各群的群昵称")
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


@nickname_guard_lifecycle.handle()
async def handle_lifecycle_connect(bot: Bot) -> None:
    """协议端（重新）登录 QQ 账号后，检查并恢复机器人在全部群的群昵称。"""
    logger.info("协议端 QQ 账号已登录，开始检查各群的群昵称")
    await _restore_all_group_cards(bot)


if _check_interval > 0:
    scheduler.add_job(
        _check_all_group_cards,
        "interval",
        seconds=_check_interval,
        id="nickname_guard_check_all_group_cards",
        replace_existing=True,
        misfire_grace_time=30,
    )
