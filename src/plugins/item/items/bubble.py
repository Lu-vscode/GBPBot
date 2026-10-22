"""道具「击鼓传花」（编号 924）：白色普通、复合型。

使用 /item.use 924 @传递对象 把花传出去：有 1.1^(-n) 的概率（n 为
自己的“泡沫”状态层数，没有该状态视为 0 层）传递成功，决斗分数增加
(n+1)×5、清空自己的“泡沫”状态，传递对象获得 n+1 层“泡沫”状态和
一个击鼓传花道具；否则传递失败，清空自己的“泡沫”状态，决斗分数减少
(n+1)×5。

“泡沫”状态（状态键 924.bubble，同键覆盖：层数累加并重置倒计时）：
获得后开始 1 小时倒计时，倒计时归零时使拥有者失去等同于层数×5 的
决斗分数并清空“泡沫”状态；到期结算与提示登记在过期状态被清理时完成。

到期提示不在到期时立即发出：先在队列保留 10 分钟，期间机器人向同群
发送其它消息时在其后延迟 1 秒跟随发出，未能跟随则静默丢弃（与决斗
超时提示的跟随发送机制同理，见 duel/_messages.py；本道具经
Bot.on_calling_api 钩子覆盖全部发送类 API，跟随不限消息来源）。

所有 @ 均为伪 @（"@昵称"为纯文本，非 at 消息段）。
"""

import asyncio
import random
import time
from dataclasses import dataclass
from typing import Any, cast

from nonebot import logger
from nonebot.adapters import Bot as BaseBot
from nonebot.adapters.onebot.v11 import Bot

from src.plugins._shared.config import NICKNAME_MAX_LENGTH, get_bot_list
from src.plugins._shared.onebot import (
    member_display_name,
    send_group_text,
    truncate_name,
)
from src.plugins.item._framework import (
    ItemDefinition,
    ItemState,
    ItemStateExpireContext,
    ItemType,
    ItemUseContext,
    Quality,
    add_item,
    add_state,
    add_user_score,
    get_state,
    register_item,
    remove_state,
)

# 道具编号与状态键
_ITEM_ID = "924"
_STATE_KEY = "924.bubble"

# “泡沫”状态的倒计时时长（秒）：1 小时，层数变化后重置
_STATE_SECONDS = 3600.0

# 传递成功的概率底数：1.1^(-n)（n 为“泡沫”状态层数）
_CHANCE_BASE = 1.1

# 决斗分数换算系数：每 1 层“泡沫”对应 5 点决斗分数（结算与文案均按此计算）
_SCORE_PER_LAYER = 5

# 消息中群昵称的最大显示长度（字符数，共享配置 NICKNAME_MAX_LENGTH）
_NICKNAME_MAX_LENGTH = NICKNAME_MAX_LENGTH

# 到期提示的保留与延迟：到期后在队列保留该时长（秒）等待跟随机器人
# 其它消息，跟随时在其后延迟该时长（秒）发出，保留期内未能发出则静默丢弃
_REMINDER_LINGER_SECONDS = 600.0
_REMINDER_DELAY_SECONDS = 1.0

# 会被视为“机器人发出的其它消息”的发送类 API：数据中带群号时尝试跟发
_GROUP_SEND_APIS = frozenset(
    {"send_msg", "send_group_msg", "send_group_forward_msg", "send_forward_msg"}
)

# 参数不合法的提示文案（传递对象须以 @ 形式给出）
_USAGE = f"请 @ 一位群成员，用法：/item.use {_ITEM_ID} @群成员"


def _layers_of(state: ItemState | None) -> int:
    """返回状态的“泡沫”层数（状态不存在或层数数据异常时视为 0 层）。"""
    if state is None:
        return 0
    value = state.data.get("layers")
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return 0
    return value


def _state_name(data: dict[str, Any], user_id: int) -> str:
    """从状态附加数据中取出成员显示名（缺失时回退为 QQ 号，并截断）。"""
    raw = str(data.get("user_name") or "").strip()
    if not raw:
        return f"QQ {user_id}"
    return truncate_name(raw, _NICKNAME_MAX_LENGTH)


def _can_use(context: ItemUseContext) -> str | None:
    """使用前校验：@ 传递对象且不为自己或机器人；返回错误文案时本次不消耗。"""
    target_id = context.target_id
    if context.args or target_id is None:
        return _USAGE
    if target_id == context.user_id:
        return f"不能对自己使用 {context.item.label}。"
    if target_id in get_bot_list() or context.bot.self_id == str(target_id):
        return "不能把花传递给机器人。"
    return None


async def _handle_use(context: ItemUseContext) -> None:
    """使用效果：按概率把花传给被 @ 的成员并结算双方状态与分数。"""
    target_id = context.target_id
    if target_id is None:
        # 不可达：can_use 已校验目标存在
        return
    layers = _layers_of(get_state(context.group_id, context.user_id, _STATE_KEY))
    target_name = await member_display_name(
        context.bot, context.group_id, target_id, _NICKNAME_MAX_LENGTH
    )
    gained = layers + 1
    score = gained * _SCORE_PER_LAYER
    # 同步段：判定并完成全部数据变更（状态、道具与分数），再发送播报
    if random.random() < _CHANCE_BASE ** (-layers):
        remove_state(context.group_id, context.user_id, _STATE_KEY)
        context.add_score(score)
        target_layers = _layers_of(get_state(context.group_id, target_id, _STATE_KEY))
        add_state(
            context.group_id,
            target_id,
            state=ItemState(
                key=_STATE_KEY,
                item_id=_ITEM_ID,
                data={"layers": target_layers + gained, "user_name": target_name},
            ),
            duration_seconds=_STATE_SECONDS,
        )
        add_item(context.group_id, target_id, _ITEM_ID)
        total = f"（共 {target_layers + gained} 层）" if target_layers else ""
        await context.send(
            f"{context.user_name} 使用了 {context.item.label}，花传了出去。\n"
            f"{target_name} 一把接住：获得 {gained} 层“泡沫”{total}"
            f"和一件 {context.item.label}。\n"
            f"{context.user_name} 决斗分数 +{score}。"
        )
        return
    remove_state(context.group_id, context.user_id, _STATE_KEY)
    context.add_score(-score)
    await context.send(
        f"{context.user_name} 使用了 {context.item.label}，鼓声一停，花掉了。\n"
        f"{context.user_name} 的 {layers} 层“泡沫”散了，决斗分数 -{score}。"
    )


@dataclass
class _DeferredReminder:
    """一条“泡沫”到期提示：先保留，跟随机器人在该群的其它消息延迟发出。"""

    group_id: int
    """提示所在群号。"""

    text: str
    """到期提示文案。"""

    deadline: float
    """保留截止时间（time.monotonic），超过后静默丢弃。"""


# 保留中的到期提示（到期后不立即发送，等待跟随机器人其它消息）
_deferred_reminders: list[_DeferredReminder] = []

# 正在延迟发送的群（防止同群重复调度）与延迟发送的任务集合
# （持有引用避免任务被垃圾回收，任务结束后自动移出）
_dispatching_groups: set[int] = set()
_reminder_tasks: set[asyncio.Task[None]] = set()


def defer_reminder(*, group_id: int, text: str) -> None:
    """把到期提示转入延迟发送队列（保留期内等待跟随机器人其它消息）。"""
    _deferred_reminders.append(
        _DeferredReminder(
            group_id=group_id,
            text=text,
            deadline=time.monotonic() + _REMINDER_LINGER_SECONDS,
        )
    )


def _drop_expired_reminders(now: float) -> None:
    """静默丢弃超过保留时间的到期提示。"""
    kept: list[_DeferredReminder] = []
    for reminder in _deferred_reminders:
        if reminder.deadline <= now:
            logger.debug(
                f"群 {reminder.group_id} 的“泡沫”到期提示超过保留时间，已静默丢弃"
            )
        else:
            kept.append(reminder)
    _deferred_reminders[:] = kept


def _on_reminder_task_done(task: asyncio.Task[None]) -> None:
    """延迟发送任务结束时移出任务集合，异常时记录日志避免静默失效。"""
    _reminder_tasks.discard(task)
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        logger.warning(f"“泡沫”到期提示的延迟发送任务异常：{error!r}")


def _schedule_reminder_send(bot: Bot, group_id: int) -> None:
    """机器人向群聊发送消息后，调度保留期内的到期提示延迟发出。

    该群已有等待中的延迟发送任务时不重复调度：跟随提示自身的发送也会
    触发本调度，由该判断拦住，避免自我循环。
    """
    if group_id in _dispatching_groups:
        return
    if not any(reminder.group_id == group_id for reminder in _deferred_reminders):
        return
    _dispatching_groups.add(group_id)
    task = asyncio.create_task(_send_reminders(bot, group_id))
    _reminder_tasks.add(task)
    task.add_done_callback(_on_reminder_task_done)


async def _send_reminders(bot: Bot, group_id: int) -> None:
    """延迟一段时间后发送群内保留期内的到期提示（同群提示合并为一条）。

    提示在发送成功后才移出队列；发送失败时保留，等待跟随下一次机器人
    消息重新尝试；超过保留期的提示由 _drop_expired_reminders 静默丢弃。
    """
    try:
        await asyncio.sleep(_REMINDER_DELAY_SECONDS)
        now = time.monotonic()
        _drop_expired_reminders(now)
        texts = [
            reminder.text
            for reminder in _deferred_reminders
            if reminder.group_id == group_id
        ]
        if not texts:
            return
        sent = await send_group_text(bot, group_id, "\n".join(texts), label="道具消息")
        if not sent:
            logger.debug(
                f"群 {group_id} 的“泡沫”到期提示发送失败，等待跟随下一次机器人消息"
            )
            return
        _deferred_reminders[:] = [
            reminder
            for reminder in _deferred_reminders
            if reminder.group_id != group_id
        ]
        logger.debug(f"群 {group_id} 的“泡沫”到期提示跟随机器人消息发出")
    finally:
        _dispatching_groups.discard(group_id)


def _as_group_id(value: Any) -> int | None:
    """把发送类 API 数据中的群号转换为 int，无效时返回 None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


@Bot.on_calling_api
async def _handle_group_send(bot: BaseBot, api: str, data: dict[str, Any]) -> None:
    """发送类 API 被调用时，调度该群保留中的“泡沫”到期提示延迟发出。"""
    if not _deferred_reminders or api not in _GROUP_SEND_APIS:
        return
    group_id = _as_group_id(data.get("group_id"))
    if group_id is None:
        return
    _drop_expired_reminders(time.monotonic())
    # 本项目仅注册 OneBot V11 适配器，钩子传入的机器人实例必为 V11 Bot
    _schedule_reminder_send(cast("Bot", bot), group_id)


async def _handle_state_expire(context: ItemStateExpireContext) -> None:
    """状态到期回调：扣除分数并登记跟随发送的到期提示。"""
    layers = _layers_of(context.state)
    if layers < 1:
        # 不可达：登记的“泡沫”状态层数至少为 1；数据异常时只做清理
        return
    name = _state_name(context.state.data, context.user_id)
    score = layers * _SCORE_PER_LAYER
    add_user_score(context.group_id, context.user_id, name, -score)
    defer_reminder(
        group_id=context.group_id,
        text=(
            f"@{name}\n鼓声停了，{name} 的 {layers} 层“泡沫”破了，决斗分数 -{score}。"
        ),
    )


register_item(
    ItemDefinition(
        item_id=_ITEM_ID,
        name="击鼓传花",
        quality=Quality.WHITE,
        types=(ItemType.COMPOSITE,),
        description=(
            "咚咚咚，鼓儿敲，红花一朵绕圈跑。你也捧，我也抛，"
            "越捧越高越热闹。鼓声一停风也小，花在谁手谁心焦。"
        ),
        effect=(
            f'使用"/item.use {_ITEM_ID} @传递对象"后，有 1.1^(-n) 的概率'
            "（n 指自己的“泡沫”状态层数）传递成功：决斗分数增加 (n+1)×5，"
            "清空自己的“泡沫”状态，传递对象获得 n+1 层“泡沫”状态和"
            "一个击鼓传花道具；否则传递失败：清空自己的“泡沫”状态，"
            "自己的决斗分数减少 (n+1)×5。\n"
            "“泡沫”状态：获得后开始 1 小时倒计时，层数可累加，层数变化后"
            "倒计时重置，倒计时归零时使拥有者失去<层数>×5 点决斗分数并"
            "清空“泡沫”状态。"
        ),
        condition="无",
        timing="任意",
        note="只是童年小游戏而已",
        accepts_args=True,
        can_use=_can_use,
        handle_use=_handle_use,
        handle_state_expire=_handle_state_expire,
    )
)
