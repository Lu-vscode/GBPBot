"""道具「挑戰數學有趣的難題」（编号 250）：黑色诅咒、诅咒型。

获得后自动使用：随机出一道高等数学难题（题库见 _PROBLEMS，答案为
整数、公式为 LaTeX 源码），限时 10 分钟（_SOLVE_SECONDS），用
/item.250.answer <答案> 回答。答对即解除诅咒；答错、参数不合法或
到期未答则把决斗分数归零（到期结算在过期状态被清理时立即尝试
发送，机器人不在线或发送失败时只记录日志、不补发）。所有 @ 均为
伪 @（"@昵称"为纯文本，非 at 消息段）。
"""

import random
import re
from typing import Any

from nonebot import get_bots, logger, on_command
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message
from nonebot.params import CommandArg
from nonebot.rule import is_type

from src.plugins._shared.onebot import send_group_text, truncate_name
from src.plugins.item._framework import (
    ItemDefinition,
    ItemState,
    ItemStateExpireContext,
    ItemType,
    ItemUseContext,
    Quality,
    add_state,
    add_user_score,
    get_state,
    get_user_score,
    register_item,
    remove_state,
)

# 道具编号与状态键
_ITEM_ID = "250"
_STATE_KEY = "curse.250"

# 解题限时（秒）：自出题时刻起共 10 分钟
_SOLVE_SECONDS = 600.0

# 消息中群昵称的最大显示长度（字符数，与主模块保持一致）
_NICKNAME_MAX_LENGTH = 12

# 题库：题面（纯文本，公式为 LaTeX 源码）与答案（均为整数）
_PROBLEMS: tuple[tuple[str, int], ...] = (
    (
        r"求级数 $\sum_{n=0}^{\infty}\frac{n^2}{2^n}$ 的值。",
        6,
    ),
    (
        r"求无穷根式 $\sqrt{1+2\sqrt{1+3\sqrt{1+4\sqrt{1+\cdots}}}}$ 的值。",
        3,
    ),
    (
        r"计算定积分 $\int_0^{\pi/2}\ln(\tan x)\,\mathrm{d}x$ 的值。",
        0,
    ),
    (
        r"求级数 $\sum_{n=1}^{\infty}\frac{F_n}{2^n}$ 的值，"
        r"其中斐波那契数列满足 $F_1=F_2=1$。",
        2,
    ),
)


def _parse_answer(text: str) -> int | None:
    """把回答文本解析为整数，参数不合法（含空参数）时返回 None。"""
    if not re.fullmatch(r"[+-]?\d+", text):
        return None
    return int(text)


def _state_name(data: dict[str, Any], user_id: int) -> str:
    """从状态附加数据中取出成员显示名（缺失时回退为 QQ 号，并截断）。"""
    raw = str(data.get("user_name") or "").strip()
    if not raw:
        return f"QQ {user_id}"
    return truncate_name(raw, _NICKNAME_MAX_LENGTH)


def _zero_score(group_id: int, user_id: int, name: str) -> None:
    """把成员的决斗分数严格归零（正负分都设为 0，已是 0 时不动）。"""
    current = get_user_score(group_id, user_id)
    if current != 0:
        add_user_score(group_id, user_id, name, -current)


async def _handle_use(context: ItemUseContext) -> None:
    """使用效果：随机出一道题并登记限时状态，再发送题面消息。

    先登记状态再发送题面：题面经发送节流排队期间状态已就绪，
    用户马上作答也能被正常受理。
    """
    statement, answer = random.choice(_PROBLEMS)
    add_state(
        context.group_id,
        context.user_id,
        state=ItemState(
            key=_STATE_KEY,
            item_id=_ITEM_ID,
            data={
                "answer": answer,
                "user_name": context.user_name,
                "bot_self_id": context.bot.self_id,
            },
        ),
        duration_seconds=_SOLVE_SECONDS,
    )
    minutes = int(_SOLVE_SECONDS // 60)
    await context.send(
        f"@{context.user_name}\n"
        "请解答以下数学难题，答案一定是整数：\n"
        f"{statement}\n\n"
        f"限时 {minutes} 分钟，在规定时间内使用 "
        f"/item.{_ITEM_ID}.answer <答案> 回答。"
    )


answer_cmd = on_command(f"item.{_ITEM_ID}.answer", rule=is_type(GroupMessageEvent))


@answer_cmd.handle()
async def handle_item_math_answer(
    bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()
) -> None:
    """处理 /item.250.answer 命令：回答进行中的数学难题。"""
    state = get_state(event.group_id, event.user_id, _STATE_KEY)
    if state is None:
        # 没有进行中的挑战：静默忽略
        return
    data = state.data
    name = _state_name(data, event.user_id)
    # 同步段：先删除状态再判定与结算，避免并发指令重复回答
    remove_state(event.group_id, event.user_id, _STATE_KEY)
    answer = data.get("answer")
    if (
        isinstance(answer, int)
        and not isinstance(answer, bool)
        and _parse_answer(args.extract_plain_text().strip()) == answer
    ):
        await send_group_text(
            bot,
            event.group_id,
            f"@{name}\n獎一塊華為手錶",
            event.message_id,
            label="道具消息",
        )
        return
    _zero_score(event.group_id, event.user_id, name)
    await send_group_text(
        bot,
        event.group_id,
        f"@{name}\n羞也不羞",
        event.message_id,
        label="道具消息",
    )


async def _handle_state_expire(context: ItemStateExpireContext) -> None:
    """状态到期回调：分数归零并立即尝试发送到期提示。

    机器人不在线或发送失败时只记录日志、不补发（发送失败已由
    send_group_text 内部记录 warning）。
    """
    data = context.state.data
    name = _state_name(data, context.user_id)
    _zero_score(context.group_id, context.user_id, name)
    bot_id = str(data.get("bot_self_id") or "")
    bot = get_bots().get(bot_id)
    if not isinstance(bot, Bot):
        logger.warning(
            f"数学难题到期提示未发送：机器人 {bot_id or '未知'} 不在线"
            f"（群 {context.group_id}，成员 {context.user_id}）"
        )
        return
    await send_group_text(
        bot, context.group_id, f"@{name}\n不會受到任何處分", label="道具消息"
    )


register_item(
    ItemDefinition(
        item_id=_ITEM_ID,
        name="挑戰數學有趣的難題",
        quality=Quality.BLACK_CURSE,
        types=(ItemType.CURSE,),
        description="花點時間挑戰數學有趣的難題",
        effect=(
            "Bot会发送随机高等数学难题，答案一定是整数，限时10min。"
            '在规定时间内使用"/item.250.answer <答案>"回答，'
            "回答正确即可解除诅咒，回答错误或超时决斗分数直接归零。"
        ),
        condition="无",
        timing="自动使用",
        note="發自我的手機",
        handle_use=_handle_use,
        handle_state_expire=_handle_state_expire,
    )
)
