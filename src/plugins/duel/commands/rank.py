"""分数排行榜指令：`/duel.rank (high|h (<条数>)) (low|l (<条数>))`。

未指定榜单时默认同时显示高分榜与低分榜；榜单以合并转发发送，每条榜单
一个节点。条数上限只影响展示，排名取自完整的正分/负分列表。
"""

from nonebot import on_command
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message
from nonebot.params import CommandArg
from nonebot.rule import is_type

from src.plugins.duel.config import (
    RANK_EMPTY,
    RANK_NODE_NAME,
    RANK_TITLE_HIGH,
    RANK_TITLE_LOW,
    USAGE_RANK,
)
from src.plugins.duel.messaging import send_forward, send_text
from src.plugins.duel.services.scores import rank_entries

# 排行榜的默认最大显示条数
_DEFAULT_RANK_LIMIT = 10


def _parse_rank_args(tokens: list[str]) -> tuple[bool, int, bool, int] | None:
    """解析排行榜参数，返回 (显示高分榜, 高分榜条数, 显示低分榜, 低分榜条数)。

    参数无法识别或条数不是正整数时返回 None。
    """
    show_high = False
    high_limit = _DEFAULT_RANK_LIMIT
    show_low = False
    low_limit = _DEFAULT_RANK_LIMIT
    index = 0
    while index < len(tokens):
        token = tokens[index].lower()
        if token in {"high", "h"}:
            show_high = True
            if index + 1 < len(tokens) and tokens[index + 1].isdigit():
                high_limit = int(tokens[index + 1])
                index += 1
        elif token in {"low", "l"}:
            show_low = True
            if index + 1 < len(tokens) and tokens[index + 1].isdigit():
                low_limit = int(tokens[index + 1])
                index += 1
        else:
            return None
        index += 1
    if high_limit < 1 or low_limit < 1:
        return None
    return show_high, high_limit, show_low, low_limit


def _rank_section(title: str, entries: list[tuple[int, str, int]], limit: int) -> str:
    """生成一个排行榜的文本：标题与最多 limit 条条目。"""
    lines = [f"{title}（前 {limit} 名）"]
    visible = entries[:limit]
    if not visible:
        lines.append(RANK_EMPTY)
        return "\n".join(lines)
    lines.extend(
        f"{index}. {name} {score} 分"
        for index, (_, name, score) in enumerate(visible, start=1)
    )
    return "\n".join(lines)


duel_rank_cmd = on_command("duel.rank", rule=is_type(GroupMessageEvent))


@duel_rank_cmd.handle()
async def handle_duel_rank(
    bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()
) -> None:
    """处理 /duel.rank 命令：查看本群分数排行榜。"""
    parsed = _parse_rank_args(args.extract_plain_text().lower().split())
    if parsed is None:
        await send_text(bot, event.group_id, USAGE_RANK, reply_to=event.message_id)
        return
    show_high, high_limit, show_low, low_limit = parsed
    if not show_high and not show_low:
        # 未指定榜单时默认同时显示高分榜与低分榜
        show_high = True
        show_low = True
    sections: list[str] = []
    if show_high:
        sections.append(
            _rank_section(
                RANK_TITLE_HIGH,
                await rank_entries(event.group_id, positive=True),
                high_limit,
            )
        )
    if show_low:
        sections.append(
            _rank_section(
                RANK_TITLE_LOW,
                await rank_entries(event.group_id, positive=False),
                low_limit,
            )
        )
    await send_forward(
        bot, event.group_id, sections, node_name=RANK_NODE_NAME, label="决斗排行榜"
    )
