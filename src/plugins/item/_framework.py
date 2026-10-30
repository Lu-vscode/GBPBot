"""道具插件的核心框架：道具定义与注册表、品质与类型、使用上下文与
使用流程、库存与状态 API、随机抽取代数。

道具模块（items/ 下）在导入期调用 register_item 注册道具；新增道具文件
由 items/__init__.py 自动发现导入，无需修改本文件。库存与状态的加载、
保存经 _storage.py 完成，本模块维护内存数据并提供操作 API。

本模块仅由道具插件包内（含道具模块）导入使用；跨插件交互（签到发放
道具、诅咒自动处理）经 _shared/services.py 的 ItemService 进行。
"""

import random
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from enum import Enum, IntEnum
from typing import TYPE_CHECKING, Any

from nonebot import logger
from nonebot.plugin import require

from src.plugins._shared.onebot import send_group_forward, send_group_text
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

    state_labels: tuple[tuple[str, str], ...] = ()
    """本道具产生的状态的显示名（状态键与显示名对，如
    ("314.ouroboros", "衔尾蛇")）；供其它道具在文案中显示状态名
    （见 state_label）。动态状态键（如按层编号的状态）可声明其公共
    前缀。同一状态键只应由产生该状态的道具声明。"""

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

    target_ids: tuple[int, ...] = ()
    """使用指令中 @ 的全部群成员 QQ 号（按出现顺序；未 @ 时为空元组，
    target_id 为其中第一个）。供道具模块取用道具编号之后的 @ 作为被
    使用道具自身的 @ 目标（如「夺心魄」代替目标成员使用道具）。"""

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


async def run_item_use(context: ItemUseContext, *, consume: bool = True) -> bool:
    """执行一次道具使用：使用条件校验（未通过时不消耗也不执行效果）、
    可选地消耗道具（默认为消耗一件库存，道具模块可自定义消耗方式）、
    调用道具模块的使用效果。

    返回是否成功执行（使用条件未通过或消耗失败时返回 False，失败文案
    已由本函数发送）。供道具模块以其他成员的身份复用 /item.use 的完整
    使用流程（如「夺心魄」代替目标成员使用其道具）。

    道具效果执行成功后依次调用使用后钩子（见 add_item_use_hook），
    供「复得之轮」等道具在成员使用其它道具后附加效果。
    """
    if context.item.can_use is not None:
        error = context.item.can_use(context)
        if error:
            await context.send(error)
            return False
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
            return False
    if context.item.handle_use is not None:
        await context.item.handle_use(context)
    await run_item_use_hooks(context)
    return True


# 道具使用后的附加效果钩子：在道具效果成功执行后按注册顺序调用（如
# 「复得之轮」在成员使用其它道具后按概率发放道具）
ItemUseHook = Callable[["ItemUseContext"], Awaitable[None]]

# 已注册的使用后钩子（按注册顺序）
_item_use_hooks: list[ItemUseHook] = []

# 钩子处理异常（单个钩子出错时不影响本次使用结果与其它钩子）
_ITEM_USE_HOOK_ERRORS = (Exception,)


def add_item_use_hook(hook: ItemUseHook) -> None:
    """注册道具使用后的附加效果钩子（同一钩子重复注册会被忽略）。

    钩子在道具自身的校验、消耗与效果全部成功后按注册顺序调用；拿到
    的是本次使用的完整上下文（道具、使用者、所在群与参数）。钩子自行
    判断是否生效（如按使用者身上的状态、道具品质或编号），抛出的异常
    只记录错误日志，不影响道具模块已发送的消息与其它钩子。
    """
    if hook not in _item_use_hooks:
        _item_use_hooks.append(hook)


async def run_item_use_hooks(context: ItemUseContext) -> None:
    """依次调用道具使用后的附加效果钩子（由 run_item_use 调用）。"""
    for hook in _item_use_hooks:
        try:
            await hook(context)
        except _ITEM_USE_HOOK_ERRORS as exc:
            logger.error(
                f"道具使用后钩子执行出错（{context.item.label}，"
                f"群 {context.group_id}，成员 {context.user_id}）：{exc}"
            )


# 黑色诅咒自动使用拦截器：在诅咒的详细信息消息发送后、效果执行前
# 依次调用；返回 True 表示本次自动使用被拦截（诅咒失去、效果不执行）
CurseInterceptor = Callable[[ItemUseContext], Awaitable[bool]]

# 已注册的拦截器（按注册顺序）
_curse_interceptors: list[CurseInterceptor] = []

# 拦截器处理异常（单个拦截器出错时视为未拦截，不影响诅咒执行）
_CURSE_INTERCEPTOR_ERRORS = (Exception,)


def add_curse_interceptor(interceptor: CurseInterceptor) -> None:
    """注册黑色诅咒自动使用拦截器（同一拦截器重复注册会被忽略）。

    黑色诅咒道具获得后自动使用前（详细信息消息已发送）按注册顺序
    询问拦截器：第一个返回 True 的拦截器抵消本次自动使用（诅咒道具
    失去、诅咒效果不执行），其余拦截器不再被调用；全部返回 False 时
    按正常流程自动使用。拦截器可在此时消耗自己的状态并发送提示消息；
    单个拦截器抛出的异常只记录错误日志，视为未拦截。
    """
    if interceptor not in _curse_interceptors:
        _curse_interceptors.append(interceptor)


async def run_curse_interceptors(context: ItemUseContext) -> bool:
    """依次询问黑色诅咒自动使用拦截器，返回本次自动使用是否被拦截。

    供诅咒获得流程（见 acquire_curse）在诅咒的详细信息消息发送后、
    执行诅咒效果前调用。
    """
    for interceptor in _curse_interceptors:
        try:
            if await interceptor(context):
                return True
        except _CURSE_INTERCEPTOR_ERRORS as exc:
            logger.error(
                f"黑色诅咒拦截器执行出错（{context.item.label}，"
                f"群 {context.group_id}，成员 {context.user_id}）：{exc}"
            )
    return False


def detail_text(definition: ItemDefinition, group_id: int, user_id: int) -> str:
    """生成道具的完整信息文本（用于详情与黑色诅咒获得消息）。

    耐久条目默认使用定义中的静态文本；道具模块提供 detail_durability
    钩子时改用其动态结果（按查看者所在群与 QQ 号计算）。
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


async def acquire_curse(
    bot: "Bot",
    group_id: int,
    user_id: int,
    user_name: str,
    definition: ItemDefinition,
) -> None:
    """让成员获得一件黑色诅咒并完成完整的获得流程。

    诅咒不写入库存：先以合并转发发送完整信息（节点名"道具详情"），
    再询问黑色诅咒拦截器（如「坚如磐石」的护盾）；未被拦截时按诅咒
    自身的 handle_use 自动使用（不消耗库存）。

    签到、合成与道具效果转赠诅咒统一经本函数处理，保证信息播报、
    拦截与自动使用行为一致。definition 不是黑色诅咒时抛出 ValueError
    （开发错误，正常流程不会出现）。
    """
    if definition.quality is not Quality.BLACK_CURSE:
        message = f"道具 {definition.label} 不是黑色诅咒，不能按诅咒获得流程处理"
        raise ValueError(message)
    await send_group_forward(
        bot,
        group_id,
        [
            f"你获得了黑色诅咒道具：{definition.label}！\n"
            f"{detail_text(definition, group_id, user_id)}"
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
    if await run_curse_interceptors(context):
        return
    await run_item_use(context, consume=False)


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
    成员不在榜上；供道具模块遍历全部非零分数成员（如全体加 1 分、
    随机抽选）或按榜上相邻位置处理（如交换分数）。
    """
    return _score_service.rank_entries(group_id)


def add_user_score(group_id: int, user_id: int, user_name: str, delta: int) -> int:
    """经决斗分数服务为成员增减决斗分数，返回更新后的分数。

    供状态到期回调等没有 ItemUseContext 的场景使用。
    """
    return _score_service.add_score(group_id, user_id, user_name, delta)


def add_user_scores(group_id: int, entries: Sequence[tuple[int, str, int]]) -> None:
    """批量增减多名成员的决斗分数，只落盘一次。

    entries 为 (成员 QQ 号, 显示名, 分数变化量) 序列，按顺序应用；
    各成员的变化量仍会依次经过分数改变拦截器链。供「五谷丰登」等
    一次影响全群成员的道具效果使用。
    """
    if entries:
        _score_service.add_scores(group_id, entries)


# 已注册的道具：编号 -> 定义（注册顺序即道具模块导入顺序）
_ITEMS: dict[str, ItemDefinition] = {}

# 状态键 -> 状态显示名（由各道具的 state_labels 在注册时汇总）
_STATE_LABELS: dict[str, str] = {}

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
    _STATE_LABELS.update(dict(definition.state_labels))
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


def state_label(state: ItemState) -> str:
    """返回状态在文案中的显示名：优先产生该状态的道具声明的专用名称
    （精确匹配优先，动态状态键其次匹配已声明键的公共前缀），再次为该
    道具的名称，道具未注册时回退为状态键。"""
    label = _STATE_LABELS.get(state.key)
    if label is None:
        prefix = max(
            (key for key in _STATE_LABELS if state.key.startswith(key)),
            key=len,
            default=None,
        )
        if prefix is not None:
            label = _STATE_LABELS[prefix]
    if label is not None:
        return label
    definition = _ITEMS.get(state.item_id)
    if definition is not None:
        return definition.name
    return state.key


# 道具库存内存数据：群号 -> 成员 QQ 号 -> {道具编号 -> 数量}
_inventory: dict[int, dict[int, dict[str, int]]] = load_inventory()
# 道具状态内存数据：群号 -> 成员 QQ 号 -> 状态列表
_states: dict[int, dict[int, list[ItemState]]] = load_states()

# 状态数据的变更序号（单元素列表以便在函数内递增）：每次状态集合
# 变化时递增，供道具模块缓存由状态派生的数据（见 state_version）
_states_version = [0]

if _inventory:
    _holder_count = sum(len(users) for users in _inventory.values())
    logger.info(f"已加载道具库存，共 {len(_inventory)} 个群、{_holder_count} 名成员")
if _states:
    _state_holder_count = sum(len(users) for users in _states.values())
    logger.info(f"已加载道具状态，共 {len(_states)} 个群、{_state_holder_count} 名成员")


def state_version() -> int:
    """返回状态数据的变更序号（状态新增、移除或过期清理后递增）。

    道具模块缓存由状态派生的数据时可记录构建时的序号，序号未变且
    缓存内容仍在有效期内即可复用（见 items/worm.py）。
    """
    return _states_version[0]


def _bump_state_version() -> None:
    """标记状态数据已变更（内部使用）。"""
    _states_version[0] += 1


def get_item_count(group_id: int, user_id: int, item_id: str) -> int:
    """返回成员在指定群拥有的道具数量（无记录时为 0）。"""
    return _inventory.get(group_id, {}).get(user_id, {}).get(item_id, 0)


def add_item(group_id: int, user_id: int, item_id: str, count: int = 1) -> None:
    """为成员增加道具并落盘。"""
    items = _inventory.setdefault(group_id, {}).setdefault(user_id, {})
    items[item_id] = items.get(item_id, 0) + count
    save_inventory(_inventory)


def add_items(group_id: int, user_id: int, item_ids: Sequence[str]) -> None:
    """批量增加道具（每件 +1，允许重复编号）并只落盘一次。"""
    if not item_ids:
        return
    items = _inventory.setdefault(group_id, {}).setdefault(user_id, {})
    for item_id in item_ids:
        items[item_id] = items.get(item_id, 0) + 1
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


def remove_items(
    group_id: int, user_id: int, entries: Sequence[tuple[str, int]]
) -> bool:
    """批量移除道具并只落盘一次；任一数量不足时不做任何修改并返回 False。

    entries 为 (道具编号, 数量) 序列；同一编号在序列中重复出现时，
    各项数量均按库存总量校验，按顺序扣减。
    """
    items = _inventory.get(group_id, {}).get(user_id)
    if items is None:
        return False
    for item_id, count in entries:
        if items.get(item_id, 0) < count:
            return False
    for item_id, count in entries:
        current = items[item_id]
        if current == count:
            del items[item_id]
        else:
            items[item_id] = current - count
    _drop_empty_inventory_levels(group_id, user_id)
    save_inventory(_inventory)
    return True


def _take_one(items: dict[str, int], item_id: str) -> None:
    """从库存字典中扣减一件道具（数量为 0 时删除条目）。"""
    if items[item_id] == 1:
        del items[item_id]
    else:
        items[item_id] -= 1


def exchange_items(
    group_id: int,
    initiator_id: int,
    initiator_item_id: str,
    target_id: int,
    target_item_id: str,
) -> bool:
    """把双方各一件道具互换并只落盘一次，返回是否交换成功。

    任一方库存不足时不做任何修改并返回 False；双方为同一成员的
    情况由调用方排除。
    """
    initiator_items = _inventory.get(group_id, {}).get(initiator_id)
    target_items = _inventory.get(group_id, {}).get(target_id)
    if initiator_items is None or target_items is None:
        return False
    if initiator_items.get(initiator_item_id, 0) < 1:
        return False
    if target_items.get(target_item_id, 0) < 1:
        return False
    _take_one(initiator_items, initiator_item_id)
    _take_one(target_items, target_item_id)
    target_items[initiator_item_id] = target_items.get(initiator_item_id, 0) + 1
    initiator_items[target_item_id] = initiator_items.get(target_item_id, 0) + 1
    _drop_empty_inventory_levels(group_id, initiator_id)
    _drop_empty_inventory_levels(group_id, target_id)
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
    """为成员新增一件道具状态并落盘（同键旧状态被覆盖）。

    state 提供状态键、来源道具与附加数据（expires_at 由本函数按下述
    时长计算，传入值不使用）；duration_seconds 为 None 时状态永久
    有效，否则自当前时刻起持续指定秒数。过期状态不在此处清理，
    统一由定时清理分发到期效果（见 purge_expired_states）。

    一次新增多件状态请用 add_states（只落盘一次）。
    """
    add_states(group_id, user_id, states=[state], duration_seconds=duration_seconds)


def add_states(
    group_id: int,
    user_id: int,
    *,
    states: Sequence[ItemState],
    duration_seconds: float | None = None,
) -> None:
    """批量新增道具状态并只落盘一次（同键旧状态按顺序覆盖）。

    时长语义与 add_state 一致（整批共用一个过期时刻）；空序列不落盘。
    """
    if not states:
        return
    now = time.time()
    expires_at = None if duration_seconds is None else now + duration_seconds
    user_states = _states.setdefault(group_id, {}).setdefault(user_id, [])
    for state in states:
        user_states[:] = [
            existing for existing in user_states if existing.key != state.key
        ]
        user_states.append(
            ItemState(
                key=state.key,
                item_id=state.item_id,
                expires_at=expires_at,
                data=dict(state.data),
            )
        )
    save_states(_states)
    _bump_state_version()


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
    return remove_states(group_id, user_id, [key]) > 0


def set_state_data(group_id: int, user_id: int, key: str, data: dict[str, Any]) -> bool:
    """覆盖成员身上指定键有效状态的附加数据并落盘（过期时刻保持不变）。

    供道具模块在状态有效期内更新附加记录（如刷新冷却时刻）；状态不
    存在或已过期时不做任何事并返回 False。
    """
    state = get_state(group_id, user_id, key)
    if state is None:
        return False
    state.data = dict(data)
    save_states(_states)
    _bump_state_version()
    return True


def remove_states(group_id: int, user_id: int, keys: Sequence[str]) -> int:
    """批量移除指定键的状态并只落盘一次，返回实际移除的状态数。"""
    user_states = _states.get(group_id, {}).get(user_id)
    if not user_states:
        return 0
    removing = set(keys)
    kept = [state for state in user_states if state.key not in removing]
    removed = len(user_states) - len(kept)
    if not removed:
        return 0
    user_states[:] = kept
    if not kept:
        _drop_empty_state_levels(group_id, user_id)
    save_states(_states)
    _bump_state_version()
    return removed


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
        _bump_state_version()
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
