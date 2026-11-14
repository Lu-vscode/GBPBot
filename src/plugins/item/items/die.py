"""道具「二阶堂希罗」（编号 659）：金色传说、Buff型。

使用后把本群此刻的决斗分数与全部成员的库存记入持续 3 天的“死亡回溯”
状态；使用“/item.659.die”让本群的决斗分数与库存回到记录的那一刻，并
失去该状态。

快照规则：

- 决斗分数只记录非零的成员：0 分与没有分数记录在分数服务中等价，恢复
  时快照中没有的成员按 0 分处理；
- 库存按使用后的现状整群记录，因此不包含本次被消耗的这一件 659，使用
  者手上其余的副本照常记录；
- 状态到期后静默消失，不改变决斗分数与库存。

快照保存在状态的附加数据（ItemState.data）中：

- scores：[[QQ 号, 昵称, 分数], ...]，只含快照时的非零分数成员；
- items：{QQ 号字符串: {道具编号: 数量}}，本群全部成员在快照时的库存。
"""

from typing import Any

from nonebot import on_command
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent
from nonebot.rule import is_type

from src.plugins._shared.onebot import send_group_text, sender_display_name
from src.plugins.item.core import (
    ItemBehavior,
    ItemDefinition,
    ItemKind,
    ItemState,
    ItemUseContext,
    Quality,
    add_state,
    add_user_scores,
    get_rank_entries,
    get_state,
    group_lock,
    register_item,
    remove_state,
    restore_group_inventory,
    snapshot_group_inventory,
)

# 道具编号、状态键与状态持续时长（秒）：3 天
_ITEM_ID = "659"
_STATE_KEY = f"{_ITEM_ID}.die"
_STATE_LABEL = "死亡回溯"
_DURATION_SECONDS = 3 * 86400.0

# 状态附加数据中的快照键
_SCORES_KEY = "scores"
_ITEMS_KEY = "items"

# 分数快照中单条记录的字段数（QQ 号、昵称、分数）
_SCORE_ENTRY_SIZE = 3

# 快照：分数为 {QQ 号: (昵称, 分数)}，库存为 {QQ 号: {道具编号: 数量}}
type _ScoreSnapshot = dict[int, tuple[str, int]]
type _ItemSnapshot = dict[int, dict[str, int]]


def _parse_scores(raw: Any) -> _ScoreSnapshot | None:
    """解析状态附加数据中的分数快照（格式异常时返回 None）。"""
    if not isinstance(raw, list):
        return None
    snapshot: _ScoreSnapshot = {}
    for entry in raw:
        if not isinstance(entry, list) or len(entry) != _SCORE_ENTRY_SIZE:
            return None
        user_id, name, score = entry
        if not isinstance(user_id, int) or isinstance(user_id, bool):
            return None
        if not isinstance(name, str):
            return None
        if not isinstance(score, int) or isinstance(score, bool):
            return None
        snapshot[user_id] = (name, score)
    return snapshot


def _parse_items(raw: Any) -> _ItemSnapshot | None:
    """解析状态附加数据中的库存快照（格式异常时返回 None）。"""
    if not isinstance(raw, dict):
        return None
    snapshot: _ItemSnapshot = {}
    for user_key, items in raw.items():
        if not isinstance(user_key, str) or not user_key.isdigit():
            return None
        if not isinstance(items, dict):
            return None
        counts: dict[str, int] = {}
        for item_id, count in items.items():
            if not isinstance(item_id, str):
                return None
            if not isinstance(count, int) or isinstance(count, bool) or count <= 0:
                return None
            counts[item_id] = count
        if counts:
            snapshot[int(user_key)] = counts
    return snapshot


def _encode_items(inventory: dict[int, dict[str, int]]) -> dict[str, dict[str, int]]:
    """把库存快照编码为可 JSON 序列化的形式（QQ 号作为字符串键）。"""
    return {str(user_id): dict(items) for user_id, items in inventory.items() if items}


def _count_item_differences(
    current: dict[int, dict[str, int]], snapshot: dict[int, dict[str, int]]
) -> int:
    """返回两份库存之间的差异件数（各成员各道具数量之差的绝对值之和）。"""
    difference = 0
    for user_id in current.keys() | snapshot.keys():
        current_items = current.get(user_id, {})
        snapshot_items = snapshot.get(user_id, {})
        for item_id in current_items.keys() | snapshot_items.keys():
            difference += abs(
                snapshot_items.get(item_id, 0) - current_items.get(item_id, 0)
            )
    return difference


async def _check(context: ItemUseContext) -> str | None:
    """使用条件：自己身上没有“死亡回溯”状态。"""
    if await get_state(context.group_id, context.user_id, _STATE_KEY) is not None:
        return f"你正处于“{_STATE_LABEL}”之中，无法再次使用 {context.item.label}。"
    return None


async def _handle_use(context: ItemUseContext) -> None:
    """使用效果：记录本群此刻的决斗分数与库存，并登记持续 3 天的状态。"""
    scores = await get_rank_entries(context.group_id)
    inventory = await snapshot_group_inventory(context.group_id)
    await add_state(
        context.group_id,
        context.user_id,
        state=ItemState(
            key=_STATE_KEY,
            item_id=_ITEM_ID,
            data={
                _SCORES_KEY: [
                    [user_id, name, score] for user_id, name, score in scores
                ],
                _ITEMS_KEY: _encode_items(inventory),
            },
        ),
        duration_seconds=_DURATION_SECONDS,
    )
    item_total = sum(count for items in inventory.values() for count in items.values())
    await context.send(
        f"{context.user_name} 使用了 {context.item.label}。\n"
        f"囚徒编号659的魔法开始刻录：本群 {len(scores)} 名决斗者的分数、"
        f"{item_total} 件道具被记了下来。\n"
        f"“{_STATE_LABEL}”状态持续3天：\n"
        f"使用“/item.{_ITEM_ID}.die”让本群的决斗分数与道具回到这一刻，"
        f"并失去“{_STATE_LABEL}”状态。"
    )


async def _restore_scores(group_id: int, snapshot: _ScoreSnapshot) -> int:
    """把本群决斗分数恢复为快照，返回分数被调整的成员数。

    快照中的成员补上与快照分数的差值；快照之后才获得分数的成员减去
    全部分数（回到快照时的 0 分）。变化量经决斗分数服务批量写入，仍会
    经过分数改变拦截器链（如被「无懈可击」抵消），实际生效的变化量
    可能与回溯目标不同。
    """
    current = {
        user_id: (name, score)
        for user_id, name, score in await get_rank_entries(group_id)
    }
    changes: list[tuple[int, str, int]] = []
    for user_id, (name, target) in snapshot.items():
        current_name, current_score = current.get(user_id, (name, 0))
        if target != current_score:
            changes.append((user_id, current_name or name, target - current_score))
    for user_id, (name, score) in current.items():
        if user_id not in snapshot:
            changes.append((user_id, name, -score))
    await add_user_scores(group_id, changes)
    return len(changes)


def _backtrack_text(name: str, changed_members: int, changed_items: int) -> str:
    """生成回溯结果的提示文案。"""
    lines = [f"{name} 发动了“{_STATE_LABEL}”。"]
    if changed_members or changed_items:
        lines.append(
            "本群的决斗分数与道具回到了被记住的那一刻："
            f"{changed_members} 名成员的决斗分数被改写，"
            f"{changed_items} 件道具被复原。"
        )
    else:
        lines.append("这一刻与记忆中的模样别无二致，什么也没有改变。")
    lines.append(f"{name} 失去了“{_STATE_LABEL}”状态。")
    return "\n".join(lines)


die_cmd = on_command(f"item.{_ITEM_ID}.die", rule=is_type(GroupMessageEvent))


@die_cmd.handle()
async def handle_item_die(bot: Bot, event: GroupMessageEvent) -> None:
    """处理 /item.659.die 命令：本群决斗分数与道具回到记录的那一刻。

    仅限持有“死亡回溯”状态的成员使用，其它成员使用时静默失败。
    """
    if await get_state(event.group_id, event.user_id, _STATE_KEY) is None:
        # 不具有“死亡回溯”状态：静默失败
        return
    name = sender_display_name(event)
    # 群锁：库存整群替换与分数回溯需要在同一段内完成，避免被同群的
    # 其它道具操作插入（也与 /item.use 等指令互斥）
    async with group_lock(event.group_id):
        state = await get_state(event.group_id, event.user_id, _STATE_KEY)
        if state is None:
            # 不可达：并发的回溯指令已先一步消耗掉状态
            return
        scores = _parse_scores(state.data.get(_SCORES_KEY))
        items = _parse_items(state.data.get(_ITEMS_KEY))
        if scores is None or items is None:
            await remove_state(event.group_id, event.user_id, _STATE_KEY)
            text = (
                f"{name} 的“{_STATE_LABEL}”记录已损坏，无法回溯，"
                f"失去了“{_STATE_LABEL}”状态。"
            )
        else:
            changed_items = _count_item_differences(
                await snapshot_group_inventory(event.group_id), items
            )
            await restore_group_inventory(event.group_id, items)
            changed_members = await _restore_scores(event.group_id, scores)
            await remove_state(event.group_id, event.user_id, _STATE_KEY)
            text = _backtrack_text(name, changed_members, changed_items)
    await send_group_text(bot, event.group_id, text, event.message_id, label="道具消息")


register_item(
    ItemDefinition(
        item_id=_ITEM_ID,
        name="二阶堂希罗",
        quality=Quality.GOLD,
        kind=ItemKind.BUFF,
        description="囚徒编号659二阶堂希罗，拥有【死亡回溯】的魔法",
        effect=(
            "使用后记录本群的决斗分数与道具，获得持续3天的“死亡回溯”状态：\n"
            f'使用"/item.{_ITEM_ID}.die"让本群的决斗分数与道具回到记录'
            "的那一刻，并失去“死亡回溯”状态。"
        ),
        condition="自己没有“死亡回溯”状态",
        timing="任意",
        note="囚徒编号659二阶堂希罗，拥有【死亡回溯】的魔法",
        labels=((_STATE_KEY, _STATE_LABEL),),
        behavior=ItemBehavior(
            check=_check,
            use=_handle_use,
        ),
    )
)
