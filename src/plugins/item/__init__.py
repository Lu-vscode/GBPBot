"""道具插件（主模块）。

群成员通过签到获得随机道具，可查看（/item.list、/item.detail）、合成
（/item.craft）、交换（/item.exchange 系列）与使用（/item.use）道具；
测试群（ITEM_TEST_GROUPS）中的超级用户可通过 /item.get 获取测试道具；
道具与状态按群隔离并保存在本地（见 _storage.py），道具定义与框架见
_framework.py，各道具模块位于 items/ 下并在导入时自动注册。

本插件经跨插件服务注册中心（见 _shared/services.py）对外提供道具发放
服务（ItemService）：签到等插件调用 grant_random 发放道具，随后调用
handle_acquisition 完成黑色诅咒的展示与自动使用。
"""

import math
import random
import time
from dataclasses import dataclass
from typing import Any

from nonebot import get_plugin_config, logger, on_command
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message
from nonebot.params import CommandArg
from nonebot.permission import SUPERUSER
from nonebot.plugin import PluginMetadata
from nonebot.rule import is_type
from nonebot_plugin_apscheduler import scheduler
from pydantic import BaseModel

from src.plugins._shared.config import get_bot_list
from src.plugins._shared.onebot import (
    member_display_name,
    send_group_forward,
    send_group_text,
    sender_display_name,
)
from src.plugins._shared.services import (
    GrantedItem,
    ItemService,
    register_service,
)
from src.plugins.item._framework import (
    ItemDefinition,
    ItemUseContext,
    Quality,
    add_item,
    draw_item_of_quality,
    draw_random_item,
    get_item,
    get_item_count,
    is_item_id,
    iter_items,
    list_user_items,
    purge_expired_states,
    quality_label,
    remove_item,
    set_draw_weights,
)

__plugin_meta__ = PluginMetadata(
    name="道具",
    description="群聊道具系统：签到获得随机道具，可查看、合成、交换与使用",
    usage=(
        "/item.list：查看自己拥有的道具\n"
        "/item.detail <道具编号>：查看自己拥有道具的详细信息\n"
        "/item.craft <道具编号1> <道具编号2>：用两件同品质道具合成一件随机道具\n"
        "/item.exchange @成员 <自己的道具编号> <对方的道具编号>：发起道具交换\n"
        "/item.exchange.accept @成员：接受对方的道具交换\n"
        "/item.exchange.reject @成员：拒绝对方的道具交换\n"
        "/item.use <道具编号> [其它参数]：使用自己拥有的一件道具"
    ),
    type="application",
    supported_adapters={"~onebot.v11"},
)

# 道具消息中群昵称的最大显示长度（字符数）
_NICKNAME_MAX_LENGTH = 12

# 合成与交换指令需要提供的道具编号数量
_ITEM_ARG_COUNT = 2

# 交换请求的有效期（秒）：期间不能向同一人重复发起；过期静默删除
_EXCHANGE_DURATION_SECONDS = 600.0

# 过期状态与交换请求的定时清理间隔（秒）
_CLEANUP_INTERVAL_SECONDS = 30

# 过期状态到期处理时捕获的异常集合（单条处理失败不影响其它条目）
_CLEANUP_ERRORS = (Exception,)

# 各指令的用法与错误提示文案（成功播报由各处自由编写，不入 env 配置）
_USAGE_LIST = "参数无法识别，用法：/item.list"
_USAGE_DETAIL = "请提供道具编号，用法：/item.detail <道具编号>"
_USAGE_CRAFT = "请提供两个道具编号，用法：/item.craft <道具编号1> <道具编号2>"
_USAGE_EXCHANGE = (
    "请 @ 群成员并提供两个道具编号，"
    "用法：/item.exchange @群成员 <自己的道具编号> <对方的道具编号>"
)
_USAGE_EXCHANGE_ACCEPT = "请 @ 发起交换的群成员，用法：/item.exchange.accept @群成员"
_USAGE_EXCHANGE_REJECT = "请 @ 发起交换的群成员，用法：/item.exchange.reject @群成员"
_USAGE_USE = "请提供道具编号，用法：/item.use <道具编号> [其它参数]"
_EXCHANGE_PAIR_ACTIVE = (
    "你们之间已有一笔进行中的交换，请等待其完成、被拒绝或过期后再发起。"
)

# 抽取品质的默认概率（键与 ITEM_DRAW_* 配置一一对应）
_DEFAULT_DRAW = {
    Quality.BLACK_CURSE: 0.055,
    Quality.GRAY: 0.550,
    Quality.WHITE: 0.250,
    Quality.GREEN: 0.100,
    Quality.BLUE: 0.030,
    Quality.PURPLE: 0.010,
    Quality.GOLD: 0.005,
}

# 品质对应的 ITEM_DRAW_* 配置后缀
_DRAW_KEYS = {
    Quality.BLACK_CURSE: "black",
    Quality.GRAY: "gray",
    Quality.WHITE: "white",
    Quality.GREEN: "green",
    Quality.BLUE: "blue",
    Quality.PURPLE: "purple",
    Quality.GOLD: "gold",
}

# 合成概率的默认值：品质低一级 / 高一级 / 高两级
_DEFAULT_CRAFT_DOWN = 0.24
_DEFAULT_CRAFT_UP = 0.75
_DEFAULT_CRAFT_UP2 = 0.01

# 概率比较的容差（默认值浮点相加可能有微小误差，不做无谓归一化）
_PROBABILITY_TOLERANCE = 1e-9


class Config(BaseModel):
    """道具插件配置。

    可在 `.env.{environment}` 文件中通过 `ITEM_*` 系列变量配置，
    缺失或为空时使用默认行为（默认抽取/合成概率）。
    """

    item_draw_black: Any = None
    """签到抽取黑色诅咒道具的概率，默认 0.055。"""

    item_draw_gray: Any = None
    """签到抽取灰色垃圾道具的概率，默认 0.55。"""

    item_draw_white: Any = None
    """签到抽取白色普通道具的概率，默认 0.25。"""

    item_draw_green: Any = None
    """签到抽取绿色精良道具的概率，默认 0.1。"""

    item_draw_blue: Any = None
    """签到抽取蓝色稀有道具的概率，默认 0.03。"""

    item_draw_purple: Any = None
    """签到抽取紫色史诗道具的概率，默认 0.01。"""

    item_draw_gold: Any = None
    """签到抽取金色传说道具的概率，默认 0.005。"""

    item_craft_down: Any = None
    """合成获得低一级品质道具的概率，默认 0.24。"""

    item_craft_up: Any = None
    """合成获得高一级品质道具的概率，默认 0.75。"""

    item_craft_up2: Any = None
    """合成获得高两级品质道具的概率，默认 0.01。"""

    item_test_groups: Any = None
    """测试群群号列表（JSON 数组）：仅这些群中的超级用户可使用 /item.get
    获取测试道具，缺失或为空时功能禁用。"""


plugin_config = get_plugin_config(Config)


def _parse_probability(value: Any, default: float, name: str) -> float:
    """解析单个概率配置。

    缺失或为空时使用默认值；无法解析为有限数字时报错并使用默认值；
    负数视为 0。
    """
    if value is None:
        return default
    text = str(value).strip()
    if not text:
        return default
    try:
        number = float(text)
    except ValueError:
        logger.error(
            f"道具配置 {name}={value!r} 无法解析为数字，已使用默认值 {default}"
        )
        return default
    if not math.isfinite(number):
        logger.error(f"道具配置 {name}={value!r} 不是有限数字，已使用默认值 {default}")
        return default
    if number < 0:
        logger.warning(f"道具配置 {name}={value!r} 为负数，已视为 0")
        return 0.0
    return number


def _resolve_draw_weights() -> dict[Quality, float]:
    """解析抽取品质的权重：总概率不为 1 时归一化，总概率为 0 时用默认值。"""
    weights = {
        quality: _parse_probability(
            getattr(plugin_config, f"item_draw_{key}"),
            _DEFAULT_DRAW[quality],
            f"ITEM_DRAW_{key.upper()}",
        )
        for quality, key in _DRAW_KEYS.items()
    }
    total = sum(weights.values())
    if total <= 0:
        logger.error("道具配置 ITEM_DRAW_* 的总概率不大于 0，已使用默认抽取概率")
        return dict(_DEFAULT_DRAW)
    if not math.isclose(
        total, 1.0, rel_tol=_PROBABILITY_TOLERANCE, abs_tol=_PROBABILITY_TOLERANCE
    ):
        logger.warning(f"道具配置 ITEM_DRAW_* 的总概率为 {total}（不为 1），已归一化")
        return {quality: weight / total for quality, weight in weights.items()}
    return weights


def _resolve_craft_odds() -> tuple[float, float, float]:
    """解析合成概率（低一级、高一级、高两级）：总概率不为 1 时归一化。"""
    down = _parse_probability(
        plugin_config.item_craft_down, _DEFAULT_CRAFT_DOWN, "ITEM_CRAFT_DOWN"
    )
    up = _parse_probability(
        plugin_config.item_craft_up, _DEFAULT_CRAFT_UP, "ITEM_CRAFT_UP"
    )
    up2 = _parse_probability(
        plugin_config.item_craft_up2, _DEFAULT_CRAFT_UP2, "ITEM_CRAFT_UP2"
    )
    total = down + up + up2
    if total <= 0:
        logger.error("道具配置 ITEM_CRAFT_* 的总概率不大于 0，已使用默认合成概率")
        return _DEFAULT_CRAFT_DOWN, _DEFAULT_CRAFT_UP, _DEFAULT_CRAFT_UP2
    if not math.isclose(
        total, 1.0, rel_tol=_PROBABILITY_TOLERANCE, abs_tol=_PROBABILITY_TOLERANCE
    ):
        logger.warning(f"道具配置 ITEM_CRAFT_* 的总概率为 {total}（不为 1），已归一化")
        return down / total, up / total, up2 / total
    return down, up, up2


def _as_group_id(value: Any) -> int | None:
    """把配置项转换为群号，无效时返回 None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _resolve_test_groups() -> frozenset[int]:
    """解析测试群群号列表（JSON 数组），无效时视为空（功能禁用）。"""
    value = plugin_config.item_test_groups
    if value is None:
        return frozenset()
    if not isinstance(value, list):
        logger.warning("道具配置 ITEM_TEST_GROUPS 格式无效（应为群号列表），已视为空")
        return frozenset()
    return frozenset(
        group_id for item in value if (group_id := _as_group_id(item)) is not None
    )


# 抽取品质的权重（注入框架）与合成概率
set_draw_weights(_resolve_draw_weights())
_craft_down, _craft_up, _craft_up2 = _resolve_craft_odds()

# 测试群群号（JSON 数组配置，缺失或无效时为空）：仅这些群中的超级用户
# 可使用 /item.get 获取测试道具
_test_groups = _resolve_test_groups()

# 导入道具模块子包：各模块在导入时调用 register_item 完成注册
from src.plugins.item import items  # noqa: F401

# 机器人名单（共享配置 BOT_LIST，见 _shared/config.py），
# 用于交换校验（不能与名单中的机器人交换）
_bot_list = get_bot_list()


async def _send_text(
    bot: Bot, group_id: int, text: str, reply_to: int | None = None
) -> None:
    """向群聊发送道具文本消息（发送失败仅记录日志）。"""
    await send_group_text(bot, group_id, text, reply_to, label="道具消息")


def _detail_text(definition: ItemDefinition, group_id: int, user_id: int) -> str:
    """生成道具的完整信息文本（用于详情与黑色诅咒获得消息）。

    耐久条目默认使用定义中的静态文本；道具模块提供 detail_durability
    钩子时改用其动态结果（按查看者状态计算）。
    """
    durability = definition.durability
    if definition.detail_durability is not None:
        durability = definition.detail_durability(group_id, user_id)
    lines = [
        f"编号：{definition.item_id}",
        f"名称：{definition.name}",
        f"品质：{quality_label(definition.quality)}",
        f"类型：{'、'.join(item_type.value for item_type in definition.types)}",
        f"介绍：{definition.description}",
        f"功能：{definition.effect}",
        f"使用条件：{definition.condition}",
        f"使用时机：{definition.timing}",
        f"耐久：{durability}",
    ]
    if definition.note:
        lines.append(f"备注：{definition.note}")
    return "\n".join(lines)


def _item_label(item_id: str) -> str:
    """返回道具的显示名（"<编号> <名称>"）；编号未注册时回退为编号本身。"""
    definition = get_item(item_id)
    if definition is None:
        return item_id
    return definition.label


def _transferable_count(group_id: int, user_id: int, item_id: str) -> int:
    """返回成员可用于交换/合成的道具数量。

    道具模块可经 transferable_count 钩子排除不可交易的副本（如耐久
    耗损的副本）；未注册编号按库存数量处理。
    """
    definition = get_item(item_id)
    if definition is not None and definition.transferable_count is not None:
        return definition.transferable_count(group_id, user_id)
    return get_item_count(group_id, user_id, item_id)


def _parse_target(args: Message) -> int | None:
    """从命令参数中解析被 @ 的成员 QQ 号，无有效 @ 段时返回 None。"""
    for segment in args:
        if segment.type != "at":
            continue
        qq = str(segment.data.get("qq", ""))
        if qq.isdigit():
            return int(qq)
    return None


async def _run_item_use(context: ItemUseContext, *, consume: bool) -> None:
    """执行一次道具使用：使用条件校验（未通过时不消耗也不执行效果）、
    可选地消耗道具（默认为消耗一件库存，道具模块可自定义消耗方式）、
    调用道具模块的使用效果。"""
    if context.item.can_use is not None:
        error = context.item.can_use(context)
        if error:
            await context.send(error)
            return
    if consume:
        consumed = (
            context.item.handle_consume(context)
            if context.item.handle_consume is not None
            else remove_item(context.group_id, context.user_id, context.item.item_id)
        )
        if not consumed:
            # 不可达：校验与消耗同处同步段，道具状态不会在期间改变
            logger.error(
                f"道具 {context.item.label} 消耗失败（群 {context.group_id}，"
                f"成员 {context.user_id}）：库存或状态不一致"
            )
            await context.send("道具使用失败：道具状态异常，本次未消耗。")
            return
    if context.item.handle_use is not None:
        await context.item.handle_use(context)


async def _auto_use_black_curse(
    bot: Bot,
    group_id: int,
    user_id: int,
    user_name: str,
    definition: ItemDefinition,
) -> None:
    """黑色诅咒获得的自动流程：先以合并转发发送完整信息，再转交道具模块自动使用。"""
    await send_group_forward(
        bot,
        group_id,
        [
            f"你获得了黑色诅咒道具：{definition.label}！\n"
            f"{_detail_text(definition, group_id, user_id)}"
        ],
        node_name="道具详情",
        label="诅咒道具信息",
    )
    context = ItemUseContext(
        bot=bot,
        group_id=group_id,
        user_id=user_id,
        user_name=user_name,
        item=definition,
        args=(),
        reply_to=None,
    )
    await _run_item_use(context, consume=False)


class _ItemService(ItemService):
    """道具发放服务的实现：其它插件经服务注册中心调用它发放道具。"""

    def grant_random(
        self, group_id: int, user_id: int, name: str
    ) -> GrantedItem | None:
        """为成员随机发放并登记一件道具，返回发放结果；抽取失败时返回 None。

        普通道具写入库存；黑色诅咒不写入库存，由调用方在其后调用
        handle_acquisition 完成信息播报与自动使用。
        """
        definition = draw_random_item()
        if definition is None:
            return None
        if definition.quality != Quality.BLACK_CURSE:
            add_item(group_id, user_id, definition.item_id)
        logger.info(
            f"群 {group_id} 成员 {user_id}（{name}）获得道具 {definition.label}"
            f"（{quality_label(definition.quality)}）"
        )
        return GrantedItem(item_id=definition.item_id, item_name=definition.name)

    async def handle_acquisition(
        self,
        bot: Bot,
        group_id: int,
        user_id: int,
        name: str,
        item: GrantedItem,
    ) -> None:
        """处理道具获得后的自动流程（黑色诅咒：先展示完整信息再自动使用）。"""
        definition = get_item(item.item_id)
        if definition is None or definition.quality != Quality.BLACK_CURSE:
            return
        await _auto_use_black_curse(bot, group_id, user_id, name, definition)


# 将道具发放能力注册为跨插件服务（契约见 _shared/services.py），
# 供签到等插件发放道具时调用；/item.get 命令也复用同一实例
_item_service = _ItemService()
register_service(ItemService, _item_service)


@dataclass
class _PendingExchange:
    """一笔等待对方处理的交换请求（仅存内存，有效期 10 分钟）。"""

    group_id: int
    """交换所在群号。"""

    initiator_id: int
    """发起方的成员 QQ 号。"""

    initiator_name: str
    """发起方的群昵称（已截断）。"""

    target_id: int
    """被发起方的成员 QQ 号。"""

    target_name: str
    """被发起方的群昵称（已截断）。"""

    initiator_item_id: str
    """发起方提供的道具编号。"""

    target_item_id: str
    """发起方希望获得（对方拥有）的道具编号。"""

    created_at: float
    """创建时间（time.monotonic，用于过期判断）。"""


# 等待处理的交换请求（按创建顺序）
_exchanges: list[_PendingExchange] = []


def _drop_expired_exchanges(now: float) -> None:
    """静默丢弃超过有效期的交换请求。"""
    kept: list[_PendingExchange] = []
    for exchange in _exchanges:
        if now - exchange.created_at >= _EXCHANGE_DURATION_SECONDS:
            logger.debug(
                f"群 {exchange.group_id} 中 {exchange.initiator_id} 向 "
                f"{exchange.target_id} 的交换请求已过期，已静默删除"
            )
        else:
            kept.append(exchange)
    _exchanges[:] = kept


def _find_pair_exchange(
    group_id: int, user_a: int, user_b: int
) -> _PendingExchange | None:
    """查找双方之间（不限方向）未完成的交换请求。"""
    for exchange in _exchanges:
        if exchange.group_id != group_id:
            continue
        if {exchange.initiator_id, exchange.target_id} == {user_a, user_b}:
            return exchange
    return None


def _find_pending_exchange(
    group_id: int, initiator: int, target: int
) -> _PendingExchange | None:
    """查找指定成员向对方发起、等待处理的交换请求。"""
    for exchange in _exchanges:
        if (
            exchange.group_id == group_id
            and exchange.initiator_id == initiator
            and exchange.target_id == target
        ):
            return exchange
    return None


def _remove_exchange(exchange: _PendingExchange) -> bool:
    """从交换列表中移除指定请求（按对象身份判断）。"""
    for index, item in enumerate(_exchanges):
        if item is exchange:
            del _exchanges[index]
            return True
    return False


def _draw_craft_results(quality: Quality) -> list[ItemDefinition] | None:
    """抽取合成结果（获得的道具列表）；目标品质的道具池为空时返回 None。

    紫色史诗"高两级"超出最高品质时改为获得两件金色传说。
    """
    roll = random.random()
    if roll < _craft_down:
        target = Quality(quality - 1)
    elif roll < _craft_down + _craft_up:
        target = Quality(quality + 1)
    elif quality is Quality.PURPLE:
        first = draw_item_of_quality(Quality.GOLD)
        second = draw_item_of_quality(Quality.GOLD)
        if first is None or second is None:
            return None
        return [first, second]
    else:
        target = Quality(quality + 2)
    item = draw_item_of_quality(target)
    if item is None:
        return None
    return [item]


item_list_cmd = on_command("item.list", rule=is_type(GroupMessageEvent))


@item_list_cmd.handle()
async def handle_item_list(
    bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()
) -> None:
    """处理 /item.list 命令：查看自己拥有的道具（按品质由高到低）。"""
    if args.extract_plain_text().strip():
        await _send_text(bot, event.group_id, _USAGE_LIST, reply_to=event.message_id)
        return
    entries = list_user_items(event.group_id, event.user_id)
    if not entries:
        await _send_text(
            bot, event.group_id, "你还没有任何道具，签到可以获得随机道具。"
        )
        return
    lines = ["你的道具："]
    lines.extend(
        f"{definition.label}（{quality_label(definition.quality)}）×{count}"
        for definition, count in entries
    )
    await _send_text(bot, event.group_id, "\n".join(lines))


item_detail_cmd = on_command("item.detail", rule=is_type(GroupMessageEvent))


@item_detail_cmd.handle()
async def handle_item_detail(
    bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()
) -> None:
    """处理 /item.detail 命令：查看自己拥有道具的详细信息（合并转发）。"""
    tokens = args.extract_plain_text().split()
    if len(tokens) != 1 or not is_item_id(tokens[0]):
        await _send_text(bot, event.group_id, _USAGE_DETAIL, reply_to=event.message_id)
        return
    item_id = tokens[0]
    definition = get_item(item_id)
    if definition is None:
        await _send_text(
            bot, event.group_id, f"道具 {item_id} 不存在。", reply_to=event.message_id
        )
        return
    if get_item_count(event.group_id, event.user_id, item_id) < 1:
        await _send_text(
            bot,
            event.group_id,
            f"你未拥有道具 {definition.label}。",
            reply_to=event.message_id,
        )
        return
    await send_group_forward(
        bot,
        event.group_id,
        [_detail_text(definition, event.group_id, event.user_id)],
        node_name="道具详情",
        label="道具详情",
    )


item_craft_cmd = on_command("item.craft", rule=is_type(GroupMessageEvent))


def _craft_error(
    group_id: int, user_id: int, first: ItemDefinition, second: ItemDefinition
) -> str | None:
    """校验两件合成材料（品质相同、非金色、数量足够且可交易），
    返回错误文案或 None。"""
    if first.quality != second.quality:
        return f"{first.label} 与 {second.label} 的品质不同，无法合成。"
    if first.quality is Quality.GOLD:
        return "金色传说道具无法合成。"
    needed = 2 if first.item_id == second.item_id else 1
    if first.item_id == second.item_id:
        need_text = f"{first.label}×2"
    else:
        need_text = f"{first.label}、{second.label} 各一件"
    if (
        get_item_count(group_id, user_id, first.item_id) < needed
        or get_item_count(group_id, user_id, second.item_id) < needed
    ):
        return f"合成需要 {need_text}，你的数量不足。"
    unavailable = next(
        (
            definition
            for definition in (first, second)
            if _transferable_count(group_id, user_id, definition.item_id) < needed
        ),
        None,
    )
    if unavailable is not None:
        return f"合成需要 {need_text}，但 {unavailable.label} 目前无法用于合成。"
    return None


@item_craft_cmd.handle()
async def handle_item_craft(
    bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()
) -> None:
    """处理 /item.craft 命令：用两件同品质道具合成一件随机品质道具。"""
    tokens = args.extract_plain_text().split()
    if len(tokens) != _ITEM_ARG_COUNT or not all(is_item_id(token) for token in tokens):
        await _send_text(bot, event.group_id, _USAGE_CRAFT, reply_to=event.message_id)
        return
    item_id_a, item_id_b = tokens
    definition_a = get_item(item_id_a)
    definition_b = get_item(item_id_b)
    if definition_a is None or definition_b is None:
        missing_id = item_id_a if definition_a is None else item_id_b
        await _send_text(
            bot,
            event.group_id,
            f"道具 {missing_id} 不存在。",
            reply_to=event.message_id,
        )
        return
    # 同步段：复查数量、抽取结果、消耗材料并登记，避免并发指令重复消耗
    error = _craft_error(event.group_id, event.user_id, definition_a, definition_b)
    if error is not None:
        await _send_text(bot, event.group_id, error, reply_to=event.message_id)
        return
    results = _draw_craft_results(definition_a.quality)
    if results is None:
        await _send_text(
            bot,
            event.group_id,
            "合成失败：目标品质的道具池为空。",
            reply_to=event.message_id,
        )
        return
    remove_item(event.group_id, event.user_id, item_id_a)
    remove_item(event.group_id, event.user_id, item_id_b)
    for result in results:
        if result.quality != Quality.BLACK_CURSE:
            add_item(event.group_id, event.user_id, result.item_id)
    result_labels = "、".join(result.label for result in results)
    await _send_text(
        bot,
        event.group_id,
        f"合成完成：消耗了 {definition_a.label}、{definition_b.label}，"
        f"获得道具 {result_labels}。",
    )
    user_name = sender_display_name(event, _NICKNAME_MAX_LENGTH)
    for result in results:
        if result.quality is Quality.BLACK_CURSE:
            await _auto_use_black_curse(
                bot, event.group_id, event.user_id, user_name, result
            )


async def _start_exchange(
    bot: Bot,
    event: GroupMessageEvent,
    target: int,
    first: ItemDefinition,
    second: ItemDefinition,
) -> _PendingExchange | str:
    """复查并登记一笔交换，返回交换对象或错误文案（发送由调用方完成）。

    先复查发起方道具（拥有且可交易），再获取对方显示名并复查对方道具
    （拥有且可交易）与双方之间进行中的交换；全部通过后在同步段登记，
    避免并发指令重复发起。
    """
    if get_item_count(event.group_id, event.user_id, first.item_id) < 1:
        return f"你未拥有道具 {first.label}。"
    if _transferable_count(event.group_id, event.user_id, first.item_id) < 1:
        return f"你的道具 {first.label} 目前无法用于交换。"
    target_name = await member_display_name(
        bot, event.group_id, target, _NICKNAME_MAX_LENGTH
    )
    if get_item_count(event.group_id, target, second.item_id) < 1:
        return f"{target_name} 未拥有道具 {second.label}。"
    if _transferable_count(event.group_id, target, second.item_id) < 1:
        return f"{target_name} 的道具 {second.label} 目前无法用于交换。"
    if _find_pair_exchange(event.group_id, event.user_id, target) is not None:
        return _EXCHANGE_PAIR_ACTIVE
    exchange = _PendingExchange(
        group_id=event.group_id,
        initiator_id=event.user_id,
        initiator_name=sender_display_name(event, _NICKNAME_MAX_LENGTH),
        target_id=target,
        target_name=target_name,
        initiator_item_id=first.item_id,
        target_item_id=second.item_id,
        created_at=time.monotonic(),
    )
    _exchanges.append(exchange)
    return exchange


item_exchange_cmd = on_command("item.exchange", rule=is_type(GroupMessageEvent))


@item_exchange_cmd.handle()
async def handle_item_exchange(
    bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()
) -> None:
    """处理 /item.exchange 命令：向群成员发起道具交换。"""
    _drop_expired_exchanges(time.monotonic())
    target = _parse_target(args)
    tokens = args.extract_plain_text().split()
    if (
        target is None
        or len(tokens) != _ITEM_ARG_COUNT
        or not all(is_item_id(token) for token in tokens)
    ):
        await _send_text(
            bot, event.group_id, _USAGE_EXCHANGE, reply_to=event.message_id
        )
        return
    item_id_a, item_id_b = tokens
    if target == event.user_id:
        await _send_text(
            bot, event.group_id, "不能与自己交换道具。", reply_to=event.message_id
        )
        return
    if target == int(event.self_id) or target in _bot_list:
        await _send_text(
            bot, event.group_id, "不能与机器人交换道具。", reply_to=event.message_id
        )
        return
    definition_a = get_item(item_id_a)
    definition_b = get_item(item_id_b)
    if definition_a is None or definition_b is None:
        missing_id = item_id_a if definition_a is None else item_id_b
        await _send_text(
            bot,
            event.group_id,
            f"道具 {missing_id} 不存在。",
            reply_to=event.message_id,
        )
        return
    # 同步段：复查双方道具与进行中的交换并登记，避免并发指令重复发起
    result = await _start_exchange(bot, event, target, definition_a, definition_b)
    if isinstance(result, str):
        await _send_text(bot, event.group_id, result, reply_to=event.message_id)
        return
    minutes = int(_EXCHANGE_DURATION_SECONDS // 60)
    await _send_text(
        bot,
        event.group_id,
        f"{result.initiator_name} 想用 {definition_a.label} 交换 "
        f"{result.target_name} 的 {definition_b.label}。"
        f"{result.target_name} 可在 {minutes} 分钟内使用 "
        f"/item.exchange.accept @{result.initiator_name} 接受，"
        f"或 /item.exchange.reject @{result.initiator_name} 拒绝。",
    )


item_exchange_accept_cmd = on_command(
    "item.exchange.accept", rule=is_type(GroupMessageEvent)
)


@item_exchange_accept_cmd.handle()
async def handle_item_exchange_accept(
    bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()
) -> None:
    """处理 /item.exchange.accept 命令：接受对方的道具交换。"""
    _drop_expired_exchanges(time.monotonic())
    target = _parse_target(args)
    if target is None:
        await _send_text(
            bot, event.group_id, _USAGE_EXCHANGE_ACCEPT, reply_to=event.message_id
        )
        return
    exchange = _find_pending_exchange(
        event.group_id, initiator=target, target=event.user_id
    )
    if exchange is None:
        initiator_name = await member_display_name(
            bot, event.group_id, target, _NICKNAME_MAX_LENGTH
        )
        await _send_text(
            bot,
            event.group_id,
            f"{initiator_name} 未向你发起交换。",
            reply_to=event.message_id,
        )
        return
    # 同步段：复查双方道具并完成互换，避免并发指令重复处理
    if (
        get_item_count(
            event.group_id, exchange.initiator_id, exchange.initiator_item_id
        )
        < 1
    ):
        # 发起方道具已失去：交换取消并告知
        _remove_exchange(exchange)
        await _send_text(
            bot,
            event.group_id,
            f"交换已取消：{exchange.initiator_name} 已不再拥有道具 "
            f"{_item_label(exchange.initiator_item_id)}。",
        )
        return
    if (
        _transferable_count(
            event.group_id, exchange.initiator_id, exchange.initiator_item_id
        )
        < 1
    ):
        # 发起方道具已不可交易（如耐久耗损）：交换取消并告知
        _remove_exchange(exchange)
        await _send_text(
            bot,
            event.group_id,
            f"交换已取消：{exchange.initiator_name} 的道具 "
            f"{_item_label(exchange.initiator_item_id)} 目前无法用于交换。",
        )
        return
    if get_item_count(event.group_id, event.user_id, exchange.target_item_id) < 1:
        # 接受方道具已失去：保留交换（可再次尝试接受或拒绝）
        await _send_text(
            bot,
            event.group_id,
            f"你已不再拥有道具 {_item_label(exchange.target_item_id)}，交换无法完成。",
            reply_to=event.message_id,
        )
        return
    if _transferable_count(event.group_id, event.user_id, exchange.target_item_id) < 1:
        # 接受方道具已不可交易：保留交换（可再次尝试接受或拒绝）
        await _send_text(
            bot,
            event.group_id,
            f"你的道具 {_item_label(exchange.target_item_id)} 目前无法用于交换，"
            "交换无法完成。",
            reply_to=event.message_id,
        )
        return
    remove_item(event.group_id, exchange.initiator_id, exchange.initiator_item_id)
    add_item(event.group_id, exchange.target_id, exchange.initiator_item_id)
    remove_item(event.group_id, event.user_id, exchange.target_item_id)
    add_item(event.group_id, exchange.initiator_id, exchange.target_item_id)
    _remove_exchange(exchange)
    await _send_text(
        bot,
        event.group_id,
        f"交换完成：{exchange.initiator_name} 用 "
        f"{_item_label(exchange.initiator_item_id)} 交换了 {exchange.target_name} 的 "
        f"{_item_label(exchange.target_item_id)}。",
    )


item_exchange_reject_cmd = on_command(
    "item.exchange.reject", rule=is_type(GroupMessageEvent)
)


@item_exchange_reject_cmd.handle()
async def handle_item_exchange_reject(
    bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()
) -> None:
    """处理 /item.exchange.reject 命令：拒绝对方的道具交换。"""
    _drop_expired_exchanges(time.monotonic())
    target = _parse_target(args)
    if target is None:
        await _send_text(
            bot, event.group_id, _USAGE_EXCHANGE_REJECT, reply_to=event.message_id
        )
        return
    exchange = _find_pending_exchange(
        event.group_id, initiator=target, target=event.user_id
    )
    if exchange is None:
        initiator_name = await member_display_name(
            bot, event.group_id, target, _NICKNAME_MAX_LENGTH
        )
        await _send_text(
            bot,
            event.group_id,
            f"{initiator_name} 未向你发起交换。",
            reply_to=event.message_id,
        )
        return
    _remove_exchange(exchange)
    await _send_text(
        bot,
        event.group_id,
        f"{exchange.target_name} 已拒绝 {exchange.initiator_name} 的交换请求。",
    )


item_use_cmd = on_command("item.use", rule=is_type(GroupMessageEvent))


@item_use_cmd.handle()
async def handle_item_use(
    bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()
) -> None:
    """处理 /item.use 命令：使用自己拥有的一件道具。"""
    tokens = args.extract_plain_text().split()
    if not tokens or not is_item_id(tokens[0]):
        await _send_text(bot, event.group_id, _USAGE_USE, reply_to=event.message_id)
        return
    item_id = tokens[0]
    definition = get_item(item_id)
    if definition is None:
        await _send_text(
            bot, event.group_id, f"道具 {item_id} 不存在。", reply_to=event.message_id
        )
        return
    use_args = tuple(tokens[1:])
    if use_args and not definition.accepts_args:
        await _send_text(bot, event.group_id, _USAGE_USE, reply_to=event.message_id)
        return
    # 同步段：校验拥有并消耗，避免并发指令重复使用同一件道具
    if get_item_count(event.group_id, event.user_id, item_id) < 1:
        await _send_text(
            bot,
            event.group_id,
            f"你未拥有道具 {definition.label}。",
            reply_to=event.message_id,
        )
        return
    user_name = sender_display_name(event, _NICKNAME_MAX_LENGTH)
    context = ItemUseContext(
        bot=bot,
        group_id=event.group_id,
        user_id=event.user_id,
        user_name=user_name,
        item=definition,
        args=use_args,
        reply_to=event.message_id,
        target_id=_parse_target(args),
    )
    await _run_item_use(context, consume=True)


# /item.get 命令：仅测试群中的超级用户可用（条件不符时不作响应）
item_get_cmd = on_command("item.get", rule=is_type(GroupMessageEvent))


@item_get_cmd.handle()
async def handle_item_get(bot: Bot, event: GroupMessageEvent) -> None:
    """处理 /item.get 命令：测试群中的超级用户获取一件随机道具。

    仅测试群（ITEM_TEST_GROUPS）中的超级用户可用，每次获取一件、无次数
    限制，抽取概率与签到一致；群号或权限不符时不作响应，也不干预事件
    传播。
    """
    if event.group_id not in _test_groups or not await SUPERUSER(bot, event):
        return
    name = sender_display_name(event, _NICKNAME_MAX_LENGTH)
    granted = _item_service.grant_random(event.group_id, event.user_id, name)
    if granted is None:
        await _send_text(
            bot, event.group_id, "道具抽取失败。", reply_to=event.message_id
        )
        return
    await _send_text(
        bot,
        event.group_id,
        f"获得道具 {granted.item_id} {granted.item_name}。",
        reply_to=event.message_id,
    )
    # 与签到一致：黑色诅咒由道具服务在获取文案之后完成播报与自动使用
    await _item_service.handle_acquisition(
        bot, event.group_id, event.user_id, name, granted
    )


async def _cleanup_items() -> None:
    """定时清理过期的道具状态与交换请求，并分发出状态到期效果。

    过期状态先按来源道具分发到期回调（如黑色诅咒的到期结算），
    回调失败只记录错误；交换请求为静默清理。
    """
    expired = purge_expired_states()
    if expired:
        logger.debug(f"已清理 {len(expired)} 条过期的道具状态")
    for entry in expired:
        definition = get_item(entry.state.item_id)
        if definition is None or definition.handle_state_expire is None:
            continue
        try:
            await definition.handle_state_expire(entry)
        except _CLEANUP_ERRORS as exc:
            logger.error(
                f"处理道具 {entry.state.item_id} 的状态到期时出错"
                f"（群 {entry.group_id}，成员 {entry.user_id}）：{exc}"
            )
    _drop_expired_exchanges(time.monotonic())


logger.info(
    f"道具系统已启用，共注册 {len(iter_items())} 件道具，"
    f"交换有效期为 {int(_EXCHANGE_DURATION_SECONDS // 60)} 分钟"
)
if _test_groups:
    logger.info(f"道具测试获取已启用（/item.get），测试群：{sorted(_test_groups)}")

scheduler.add_job(
    _cleanup_items,
    "interval",
    seconds=_CLEANUP_INTERVAL_SECONDS,
    id="item_cleanup",
    replace_existing=True,
    misfire_grace_time=30,
)
