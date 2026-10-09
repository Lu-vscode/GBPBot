"""道具插件的核心框架：道具定义与注册表、品质与类型、使用上下文、
库存与状态 API、随机抽取代数。

道具模块（items/ 下）在导入期调用 register_item 注册道具；新增道具文件
由 items/__init__.py 自动发现导入，无需修改本文件。库存与状态的加载、
保存经 _storage.py 完成，本模块维护内存数据并提供操作 API。

本模块仅由道具插件包内（含道具模块）导入使用；跨插件交互（签到发放
道具、诅咒自动处理）经 _shared/services.py 的 ItemService 进行。
"""

import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum, IntEnum
from typing import TYPE_CHECKING

from nonebot import logger
from nonebot.plugin import require

from src.plugins._shared.onebot import send_group_text
from src.plugins._shared.services import DuelScoreService, require_service
from src.plugins.item._storage import (
    ItemState,
    load_inventory,
    load_states,
    save_inventory,
    save_states,
)

if TYPE_CHECKING:
    from nonebot.adapters.onebot.v11 import Bot

# 加载时依赖声明：道具效果可经决斗分数服务增减分数或查询低分榜第一名
# （见 _shared/services.py）。require 保证决斗插件先完成加载、分数服务已
# 注册；服务缺失时插件加载失败并给出明确日志
require("duel")
_score_service = require_service(DuelScoreService)


class Quality(IntEnum):
    """道具品质，数值用于合成升降级运算与品质排序。"""

    BLACK_CURSE = -1
    GRAY = 0
    WHITE = 1
    GREEN = 2
    BLUE = 3
    PURPLE = 4
    GOLD = 5


# 品质的显示名
_QUALITY_LABELS = {
    Quality.BLACK_CURSE: "黑色诅咒",
    Quality.GRAY: "灰色垃圾",
    Quality.WHITE: "白色普通",
    Quality.GREEN: "绿色精良",
    Quality.BLUE: "蓝色稀有",
    Quality.PURPLE: "紫色史诗",
    Quality.GOLD: "金色传说",
}


def quality_label(quality: Quality) -> str:
    """返回品质的显示名。"""
    return _QUALITY_LABELS[quality]


class ItemType(Enum):
    """道具类型。"""

    CONSUMABLE = "消耗型"
    CURSE = "诅咒型"
    BUFF = "Buff型"
    PERMANENT = "永久型"
    DURABLE = "耐久型"
    OPTIONAL = "可选型"
    COMPOSITE = "复合型"
    SPECIAL = "特殊型"


# 状态到期回调的类型：过期状态被清理时由主模块调用（参数见
# ItemStateExpireContext）；字符串前向引用以摆脱定义顺序约束
_StateExpireHandler = Callable[["ItemStateExpireContext"], Awaitable[None]]


@dataclass(frozen=True)
class ItemDefinition:
    """一件道具的定义（由道具模块在导入期注册，注册后不可变）。"""

    item_id: str
    """道具编号：三位数字字符串（000~999），0 不可省略；
    必须唯一，且注册时不允许留空（编号由道具开发时指定）。"""

    name: str
    """道具名称（不要求唯一）。"""

    quality: Quality
    """品质。"""

    types: tuple[ItemType, ...]
    """类型（可为多种类型的复合）。"""

    description: str
    """介绍。"""

    effect: str
    """功能。"""

    condition: str
    """使用条件（展示文本）。"""

    timing: str
    """使用时机（展示文本）。"""

    durability: str = "1/1"
    """耐久（展示文本，一般为"剩余耐久/总耐久"）。"""

    detail_durability: Callable[[int, int], str] | None = None
    """详情消息中耐久条目的动态显示（参数为查看者所在群号与其 QQ 号）；
    默认 None 表示使用 durability 的静态文本；供道具模块按成员状态
    动态展示（如显示耗损副本的剩余耐久）。"""

    note: str = ""
    """备注。"""

    accepts_args: bool = False
    """使用指令是否接受额外参数（可选型等道具为 True）。"""

    can_use: Callable[["ItemUseContext"], str | None] | None = None
    """使用前校验：返回非空错误文案表示本次不可用（此时不消耗）。"""

    handle_use: Callable[["ItemUseContext"], Awaitable[None]] | None = None
    """使用效果（成功提示消息由效果自身发送）。"""

    handle_consume: Callable[["ItemUseContext"], bool] | None = None
    """消耗道具的自定义方式（默认为消耗一件库存）；返回 False 表示未能
    消耗，本次使用中止且不执行效果。耐久型等道具在此自行扣减耐久或移除
    副本（须为同步函数，在校验与消耗的同步段内执行）。"""

    transferable_count: Callable[[int, int], int] | None = None
    """返回成员可用于交换与合成的该道具副本数量（参数为群号与成员 QQ 号；
    默认 None 表示全部库存均可参与）。交换与合成在库存数量满足要求后按
    该数量复核，如耐久耗损的副本可在此排除。"""

    handle_state_expire: _StateExpireHandler | None = None
    """状态到期回调（由定时清理调用；未提供时过期状态只被静默删除）。"""

    @property
    def label(self) -> str:
        """道具的显示名（"<编号> <名称>"，如 "111 +1"）。"""
        return f"{self.item_id} {self.name}"


@dataclass
class ItemUseContext:
    """一次道具使用的上下文（注入给道具模块的 can_use/handle_use）。"""

    bot: "Bot"
    """发送消息的机器人实例。"""

    group_id: int
    """使用道具所在群号。"""

    user_id: int
    """使用者 QQ 号。"""

    user_name: str
    """使用者在群内的显示名（已截断）。"""

    item: ItemDefinition
    """被使用的道具定义。"""

    args: tuple[str, ...]
    """使用指令的额外参数（诅咒自动使用时为空）。"""

    reply_to: int | None
    """触发使用指令的消息 ID（诅咒自动使用时为 None）。"""

    target_id: int | None = None
    """使用指令中 @ 的群成员 QQ 号（未 @ 时为 None）。"""

    async def send(self, text: str) -> None:
        """向群聊发送一条道具消息（有触发消息时附带引用）。"""
        await send_group_text(
            self.bot, self.group_id, text, self.reply_to, label="道具消息"
        )

    def add_score(self, delta: int) -> int:
        """经决斗分数服务为使用者增减决斗分数，返回更新后的分数。"""
        return _score_service.add_score(
            self.group_id, self.user_id, self.user_name, delta
        )


@dataclass
class ItemStateExpireContext:
    """一条过期状态被清理时的上下文（注入给道具模块的到期回调）。

    到期时刻没有消息事件，机器人实例等发送所需信息由道具模块在
    状态附加数据（ItemState.data）中自行保存。
    """

    group_id: int
    """状态所在群号。"""

    user_id: int
    """状态所属成员 QQ 号。"""

    state: ItemState
    """被清理的过期状态（附加数据可用于恢复业务信息）。"""


def get_user_score(group_id: int, user_id: int) -> int:
    """查询成员在指定群的决斗分数（供无使用上下文的场景调用）。"""
    return _score_service.get_score(group_id, user_id)


def get_lowest_score_user(group_id: int) -> int | None:
    """查询指定群决斗低分榜第一名的成员 QQ 号（无负分成员时返回 None）。

    低分榜的判定与决斗插件的 /duel.rank 低分榜一致；供道具模块校验
    "位列决斗低分榜第一名"等使用条件。
    """
    return _score_service.lowest_member(group_id)


def get_highest_score_user(group_id: int) -> int | None:
    """查询指定群决斗高分榜第一名的成员 QQ 号（无正分成员时返回 None）。

    高分榜的判定与决斗插件的 /duel.rank 高分榜一致；供道具模块校验
    "位列决斗高分榜第一名"等使用条件。
    """
    return _score_service.highest_member(group_id)


def get_rank_entries(group_id: int) -> list[tuple[int, str, int]]:
    """查询指定群决斗分数总榜的全部条目 (QQ 号, 昵称, 分数)。

    总榜为决斗高分榜与反转的决斗低分榜的拼接：全部非零分数的成员
    按分数从高到低排列、同分按 QQ 号升序排列；0 分或没有分数记录的
    成员不在榜上；供道具模块按榜上相邻位置处理（如交换分数）。
    """
    return _score_service.rank_entries(group_id)


def add_user_score(group_id: int, user_id: int, user_name: str, delta: int) -> int:
    """经决斗分数服务为成员增减决斗分数，返回更新后的分数。

    供状态到期回调等没有 ItemUseContext 的场景使用。
    """
    return _score_service.add_score(group_id, user_id, user_name, delta)


# 已注册的道具：编号 -> 定义（注册顺序即道具模块导入顺序）
_ITEMS: dict[str, ItemDefinition] = {}

# 道具编号的位数（三位数字字符串）
_ITEM_ID_LENGTH = 3


def is_item_id(value: str) -> bool:
    """判断是否为合法的道具编号（三位 ASCII 数字字符串）。"""
    return len(value) == _ITEM_ID_LENGTH and value.isascii() and value.isdigit()


def register_item(definition: ItemDefinition) -> ItemDefinition:
    """注册一件道具并返回注册后的定义。

    注册在道具模块导入期执行，发现开发错误时抛出 ValueError：由
    items 加载器记录 warning 并忽略该道具，不影响其它道具与整个
    插件包的加载。校验规则：

    - 编号必须为三位数字字符串（000~999）且不允许留空；编号由道具
      开发时指定（设计未指定时应从 100 起向上寻找可用编号并写入代码，
      000~099 保留给强大/特殊的道具）
    - 编号不得与已注册道具重复
    - 品质为黑色诅咒与类型含诅咒型必须同时成立（互为充要条件）
    """
    if not definition.item_id:
        message = "道具编号不能留空，请在道具开发时指定编号"
        raise ValueError(message)
    if not is_item_id(definition.item_id):
        message = (
            f"道具编号必须为三位数字字符串（000~999），收到：{definition.item_id!r}"
        )
        raise ValueError(message)
    if definition.item_id in _ITEMS:
        message = (
            f"道具编号 {definition.item_id} 已被道具"
            f"「{_ITEMS[definition.item_id].name}」占用"
        )
        raise ValueError(message)
    cursed = ItemType.CURSE in definition.types
    if (definition.quality is Quality.BLACK_CURSE) != cursed:
        message = (
            f"道具 {definition.item_id} 的品质与类型不一致：黑色诅咒品质与诅咒型"
            "类型必须同时出现"
        )
        raise ValueError(message)
    _ITEMS[definition.item_id] = definition
    logger.info(
        f"道具已注册：{definition.label}（{quality_label(definition.quality)}、"
        f"{'、'.join(item_type.value for item_type in definition.types)}）"
    )
    return definition


def get_item(item_id: str) -> ItemDefinition | None:
    """按编号查询道具定义，未注册时返回 None。"""
    return _ITEMS.get(item_id)


def iter_items() -> list[ItemDefinition]:
    """返回全部已注册道具（按编号升序）。"""
    return [_ITEMS[item_id] for item_id in sorted(_ITEMS)]


# 道具库存内存数据：群号 -> 成员 QQ 号 -> {道具编号 -> 数量}
_inventory: dict[int, dict[int, dict[str, int]]] = load_inventory()
# 道具状态内存数据：群号 -> 成员 QQ 号 -> 状态列表
_states: dict[int, dict[int, list[ItemState]]] = load_states()

if _inventory:
    _holder_count = sum(len(users) for users in _inventory.values())
    logger.info(f"已加载道具库存，共 {len(_inventory)} 个群、{_holder_count} 名成员")
if _states:
    _state_holder_count = sum(len(users) for users in _states.values())
    logger.info(f"已加载道具状态，共 {len(_states)} 个群、{_state_holder_count} 名成员")


def get_item_count(group_id: int, user_id: int, item_id: str) -> int:
    """返回成员在指定群拥有的道具数量（无记录时为 0）。"""
    return _inventory.get(group_id, {}).get(user_id, {}).get(item_id, 0)


def add_item(group_id: int, user_id: int, item_id: str, count: int = 1) -> None:
    """为成员增加道具并落盘。"""
    items = _inventory.setdefault(group_id, {}).setdefault(user_id, {})
    items[item_id] = items.get(item_id, 0) + count
    save_inventory(_inventory)


def _drop_empty_inventory_levels(group_id: int, user_id: int) -> None:
    """库存清空后删除成员与群的空层级。"""
    users = _inventory.get(group_id)
    if users is None or users.get(user_id):
        return
    users.pop(user_id, None)
    if not users:
        _inventory.pop(group_id, None)


def remove_item(group_id: int, user_id: int, item_id: str, count: int = 1) -> bool:
    """为成员移除道具并落盘；数量不足时不做任何事并返回 False。"""
    items = _inventory.get(group_id, {}).get(user_id)
    if items is None:
        return False
    current = items.get(item_id, 0)
    if current < count:
        return False
    if current == count:
        del items[item_id]
    else:
        items[item_id] = current - count
    _drop_empty_inventory_levels(group_id, user_id)
    save_inventory(_inventory)
    return True


def list_user_items(group_id: int, user_id: int) -> list[tuple[ItemDefinition, int]]:
    """返回成员拥有的全部道具（定义与数量），按品质由高到低、
    同品质按编号升序排列；库存中未注册的编号跳过并记录 warning。"""
    items = _inventory.get(group_id, {}).get(user_id, {})
    result: list[tuple[ItemDefinition, int]] = []
    for item_id, count in items.items():
        definition = _ITEMS.get(item_id)
        if definition is None:
            logger.warning(f"库存中的道具编号 {item_id} 未注册（可能已移除），已跳过")
            continue
        result.append((definition, count))
    result.sort(key=lambda entry: (-entry[0].quality.value, entry[0].item_id))
    return result


def _state_active(state: ItemState, now: float) -> bool:
    """判断状态在指定时刻是否仍然有效。"""
    return state.expires_at is None or state.expires_at > now


def _drop_empty_state_levels(group_id: int, user_id: int) -> None:
    """状态清空后删除成员与群的空层级。"""
    users = _states.get(group_id)
    if users is None or users.get(user_id):
        return
    users.pop(user_id, None)
    if not users:
        _states.pop(group_id, None)


def add_state(
    group_id: int,
    user_id: int,
    *,
    state: ItemState,
    duration_seconds: float | None = None,
) -> None:
    """为成员新增道具状态并落盘（同键旧状态被覆盖）。

    state 提供状态键、来源道具与附加数据（expires_at 由本函数按下述
    时长计算，传入值不使用）；duration_seconds 为 None 时状态永久
    有效，否则自当前时刻起持续指定秒数。过期状态不在此处清理，
    统一由定时清理分发到期效果（见 purge_expired_states）。
    """
    now = time.time()
    user_states = _states.setdefault(group_id, {}).setdefault(user_id, [])
    user_states[:] = [existing for existing in user_states if existing.key != state.key]
    user_states.append(
        ItemState(
            key=state.key,
            item_id=state.item_id,
            expires_at=None if duration_seconds is None else now + duration_seconds,
            data=dict(state.data),
        )
    )
    save_states(_states)


def get_state(group_id: int, user_id: int, key: str) -> ItemState | None:
    """查询成员身上指定键的有效状态，未找到或已过期时返回 None。

    已过期的状态不在此处删除：等待定时清理统一分发到期效果
    （见 purge_expired_states）。
    """
    user_states = _states.get(group_id, {}).get(user_id)
    if not user_states:
        return None
    now = time.time()
    for state in user_states:
        if state.key == key and _state_active(state, now):
            return state
    return None


def remove_state(group_id: int, user_id: int, key: str) -> bool:
    """移除成员身上指定键的状态并落盘，返回是否存在该状态。"""
    user_states = _states.get(group_id, {}).get(user_id)
    if not user_states:
        return False
    kept = [state for state in user_states if state.key != key]
    if len(kept) == len(user_states):
        return False
    user_states[:] = kept
    if not kept:
        _drop_empty_state_levels(group_id, user_id)
    save_states(_states)
    return True


def list_states(group_id: int, user_id: int) -> list[ItemState]:
    """返回成员身上的全部有效状态（不做清理，过期条目等待定时清理）。"""
    user_states = _states.get(group_id, {}).get(user_id)
    if not user_states:
        return []
    now = time.time()
    return [state for state in user_states if _state_active(state, now)]


def list_state_holders(group_id: int, key: str) -> list[tuple[int, ItemState]]:
    """返回群内持有指定键有效状态的成员 (QQ 号, 状态)，按 QQ 号升序。

    供道具模块在无使用上下文的场景查询状态持有者（如发送消息时按
    持有者显示名改写文本）；已过期但未清理的状态不计入。
    """
    users = _states.get(group_id)
    if not users:
        return []
    now = time.time()
    holders: list[tuple[int, ItemState]] = []
    for user_id in sorted(users):
        for state in users[user_id]:
            if state.key == key and _state_active(state, now):
                holders.append((user_id, state))
                break
    return holders


def purge_expired_states() -> list[ItemStateExpireContext]:
    """清理全部已过期的状态并落盘，返回到期条目列表（无清理时不落盘）。

    返回的条目由主模块按来源道具分发到期回调（见
    ItemDefinition.handle_state_expire）；未提供回调或处理失败时
    状态仍已被删除。
    """
    now = time.time()
    expired: list[ItemStateExpireContext] = []
    for group_id in list(_states):
        users = _states[group_id]
        for user_id in list(users):
            user_states = users[user_id]
            kept: list[ItemState] = []
            for state in user_states:
                if _state_active(state, now):
                    kept.append(state)
                else:
                    expired.append(
                        ItemStateExpireContext(
                            group_id=group_id, user_id=user_id, state=state
                        )
                    )
            if len(kept) == len(user_states):
                continue
            if kept:
                user_states[:] = kept
            else:
                users.pop(user_id, None)
        if not users:
            _states.pop(group_id, None)
    if expired:
        save_states(_states)
    return expired


# 抽取品质的权重（由主模块解析 ITEM_DRAW_* 配置后注入）：品质 -> 权重
_draw_weights: dict[Quality, float] = {}


def set_draw_weights(weights: dict[Quality, float]) -> None:
    """设置抽取品质的权重（主模块解析配置后调用）。"""
    _draw_weights.clear()
    _draw_weights.update(weights)


def _draw_quality() -> Quality | None:
    """按权重抽取品质；权重未设置或浮点误差导致未命中时返回 None。"""
    roll = random.random()
    for quality, weight in _draw_weights.items():
        roll -= weight
        if roll < 0:
            return quality
    return None


def draw_item_of_quality(quality: Quality) -> ItemDefinition | None:
    """从指定品质的道具池中等概率抽取一件，池为空时返回 None。"""
    pool = [item for item in _ITEMS.values() if item.quality == quality]
    if not pool:
        logger.warning(
            f"抽取失败：{quality_label(quality)}（品质 {quality.value}）的道具池为空"
        )
        return None
    return random.choice(pool)


def draw_random_item() -> ItemDefinition | None:
    """随机抽取一件道具：先按配置权重抽品质、再从该品质池等概率抽取。

    权重未设置（异常情况）或品质池为空时返回 None。
    """
    quality = _draw_quality()
    if quality is None:
        logger.warning("道具抽取未命中任何品质（抽取权重未设置），本次未抽到道具")
        return None
    return draw_item_of_quality(quality)
