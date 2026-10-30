"""道具「存档」（编号 131）：紫色史诗、永久型。

使用后获得永久状态“存档点”，记录使用时的决斗分数；每个不同的分数各记
为一个独立的“存档点”，同时可以拥有多个。同一分数的“存档点”带有份数，
再次在该分数使用道具会使份数 +1：

- “/item.131.checkpoint”查看自己的全部“存档点”与份数；
- “/item.131.load <存档点分数>”把决斗分数恢复到该“存档点”记录的分数，
  并使该“存档点”的份数 -1，份数归零时该“存档点”消失。

两个指令仅限持有“存档点”状态的成员使用，未持有的成员使用时静默失败
（参数无法识别时照常提示用法）。
"""

from nonebot import on_command
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message
from nonebot.params import CommandArg
from nonebot.rule import is_type

from src.plugins._shared.config import NICKNAME_MAX_LENGTH
from src.plugins._shared.onebot import send_group_text, sender_display_name
from src.plugins.item._framework import (
    ItemDefinition,
    ItemState,
    ItemType,
    ItemUseContext,
    Quality,
    add_state,
    add_user_score,
    get_state,
    get_user_score,
    list_states,
    register_item,
    remove_state,
    set_state_data,
)

# 道具编号与状态键的公共前缀（状态键为“<前缀>.<存档点分数>”，一个分数
# 对应一个状态，因此多个不同的“存档点”可以同时存在；同一分数的份数记录
# 在该状态的附加数据中）
_ITEM_ID = "131"
_STATE_KEY_PREFIX = f"{_ITEM_ID}.checkpoint"

# 状态附加数据中的存档分数字段与份数字段
_SCORE_KEY = "score"
_COUNT_KEY = "count"

# 指令参数错误的提示文案
_USAGE_LOAD = f"用法：/item.{_ITEM_ID}.load <存档点分数>"

# 消息中群昵称的最大显示长度（字符数，共享配置 NICKNAME_MAX_LENGTH）
_NICKNAME_MAX_LENGTH = NICKNAME_MAX_LENGTH


def _state_key(score: int) -> str:
    """返回记录指定分数的“存档点”状态键。"""
    return f"{_STATE_KEY_PREFIX}.{score}"


def _state_score(state: ItemState) -> int | None:
    """返回“存档点”状态记录的分数；不是本道具的状态或数据异常时返回 None。"""
    if state.item_id != _ITEM_ID or not state.key.startswith(f"{_STATE_KEY_PREFIX}."):
        return None
    value = state.data.get(_SCORE_KEY)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _checkpoints(group_id: int, user_id: int) -> list[tuple[int, int]]:
    """返回成员全部“存档点”的 (分数, 份数)，按分数从高到低排列。"""
    entries = [
        (score, _state_count(state))
        for state in list_states(group_id, user_id)
        if (score := _state_score(state)) is not None
    ]
    entries.sort(key=lambda entry: -entry[0])
    return entries


def _state_count(state: ItemState) -> int:
    """返回“存档点”的份数（数据缺失或异常时按 1 份处理）。"""
    value = state.data.get(_COUNT_KEY)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return 1
    return value


def _parse_score(text: str) -> int | None:
    """把指令参数解析为存档点分数，不是整数时返回 None。"""
    try:
        score = int(text)
    except ValueError:
        return None
    return score


async def _handle_use(context: ItemUseContext) -> None:
    """使用效果：把当前决斗分数记为一个永久“存档点”并发送提示。

    该分数已有“存档点”时只把份数 +1：状态键按分数唯一，份数记在附加
    数据中，状态保持永久有效。
    """
    score = get_user_score(context.group_id, context.user_id)
    state_key = _state_key(score)
    existing = get_state(context.group_id, context.user_id, state_key)
    count = _state_count(existing) + 1 if existing is not None else 1
    if existing is None:
        add_state(
            context.group_id,
            context.user_id,
            state=ItemState(
                key=state_key,
                item_id=_ITEM_ID,
                data={_SCORE_KEY: score, _COUNT_KEY: count},
            ),
        )
    else:
        set_state_data(
            context.group_id,
            context.user_id,
            state_key,
            {_SCORE_KEY: score, _COUNT_KEY: count},
        )
    if existing is not None:
        await context.send(
            f"{context.user_name} 使用了 {context.item.label}，"
            f"又在决斗分数 {score} 处盖了一个存档点，现在共有 {count} 份。"
        )
        return
    await context.send(
        f"{context.user_name} 使用了 {context.item.label}，"
        f"把此刻的决斗分数 {score} 存了下来。\n"
        "“存档点”永久有效：\n"
        f"使用“/item.{_ITEM_ID}.checkpoint”查看自己的存档点，\n"
        f"使用“/item.{_ITEM_ID}.load <存档点分数>”"
        "把决斗分数恢复到该存档点，并使该存档点的份数-1。"
    )


checkpoint_cmd = on_command(
    f"item.{_ITEM_ID}.checkpoint", rule=is_type(GroupMessageEvent)
)


@checkpoint_cmd.handle()
async def handle_item_checkpoint(bot: Bot, event: GroupMessageEvent) -> None:
    """处理 /item.131.checkpoint 命令：查看自己的全部“存档点”。"""
    entries = _checkpoints(event.group_id, event.user_id)
    if not entries:
        # 不具有“存档点”状态：静默失败
        return
    name = sender_display_name(event, _NICKNAME_MAX_LENGTH)
    current = get_user_score(event.group_id, event.user_id)
    lines = [f"{name} 的存档点："]
    lines.extend(f"决斗分数 {score} ×{count}" for score, count in entries)
    lines.append(f"当前决斗分数：{current}")
    lines.append(f"使用“/item.{_ITEM_ID}.load <存档点分数>”读取存档点。")
    await send_group_text(
        bot, event.group_id, "\n".join(lines), event.message_id, label="道具消息"
    )


load_cmd = on_command(f"item.{_ITEM_ID}.load", rule=is_type(GroupMessageEvent))


@load_cmd.handle()
async def handle_item_load(
    bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()
) -> None:
    """处理 /item.131.load 命令：恢复决斗分数并使该“存档点”份数 -1。"""
    if not _checkpoints(event.group_id, event.user_id):
        # 不具有“存档点”状态：静默失败
        return
    tokens = args.extract_plain_text().split()
    score = _parse_score(tokens[0]) if len(tokens) == 1 else None
    if score is None:
        await send_group_text(
            bot, event.group_id, _USAGE_LOAD, event.message_id, label="道具消息"
        )
        return
    state_key = _state_key(score)
    state = get_state(event.group_id, event.user_id, state_key)
    if state is None:
        await send_group_text(
            bot,
            event.group_id,
            f"你没有决斗分数为 {score} 的存档点。",
            event.message_id,
            label="道具消息",
        )
        return
    name = sender_display_name(event, _NICKNAME_MAX_LENGTH)
    before = get_user_score(event.group_id, event.user_id)
    remaining = _state_count(state) - 1
    # 同步段：结算分数并把该存档点的份数 -1（归零时该存档点消失）
    add_user_score(event.group_id, event.user_id, name, score - before)
    if remaining > 0:
        set_state_data(
            event.group_id,
            event.user_id,
            state_key,
            {_SCORE_KEY: score, _COUNT_KEY: remaining},
        )
    else:
        remove_state(event.group_id, event.user_id, state_key)
    after = get_user_score(event.group_id, event.user_id)
    if after == before:
        detail = f"决斗分数没有变化（仍为 {before}）"
    else:
        detail = f"决斗分数从 {before} 变为 {after}"
    if remaining > 0:
        ending = f"该“存档点”还剩 {remaining} 份。"
    else:
        ending = "该“存档点”已用尽，失去了该状态。"
    await send_group_text(
        bot,
        event.group_id,
        f"{name} 读取了分数 {score} 的存档点，{detail}，{ending}",
        event.message_id,
        label="道具消息",
    )


register_item(
    ItemDefinition(
        item_id=_ITEM_ID,
        name="存档",
        quality=Quality.PURPLE,
        types=(ItemType.PERMANENT,),
        description="存档当前决斗分数",
        effect=(
            "使用后获得永久状态“存档点”。\n"
            f'使用"/item.{_ITEM_ID}.checkpoint"查看自己的存档点，\n'
            f'使用"/item.{_ITEM_ID}.load <存档点分数>"使自己的决斗分数恢复到'
            "指定“存档点”记录的分数，并使该“存档点”的份数-1，"
            "份数归零时该“存档点”消失。\n"
            "同一分数的“存档点”可以叠加份数。"
        ),
        condition="无",
        timing="任意",
        note="为什么是一次性的？",
        handle_use=_handle_use,
        state_labels=((_STATE_KEY_PREFIX, "存档点"),),
    )
)
