"""分数查询指令：`/duel.score`。

默认查看自己在本群的决斗分数；超级用户可附加 sum/s 参数查看本群全部群
成员的分数总和（未在用户手册中提及）。
"""

from nonebot import on_command
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message
from nonebot.params import CommandArg
from nonebot.permission import SUPERUSER
from nonebot.rule import is_type

from src.plugins.duel.commands.args import sender_name
from src.plugins.duel.config import SCORE_SUM_DENIED, USAGE_SCORE
from src.plugins.duel.messaging import send_text
from src.plugins.duel.services.scores import score_service, total_score


def _parse_score_args(tokens: list[str]) -> str | None:
    """解析 /duel.score 命令参数，返回 "self"、"sum" 或 None（无法识别）。

    sum 参数仅超级用户可用，未在用户手册中提及。
    """
    if not tokens:
        return "self"
    if len(tokens) == 1 and tokens[0] in {"sum", "s"}:
        return "sum"
    return None


duel_score_cmd = on_command("duel.score", rule=is_type(GroupMessageEvent))


@duel_score_cmd.handle()
async def handle_duel_score(
    bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()
) -> None:
    """处理 /duel.score 命令：查看自己（或超级用户查看本群全部成员）的决斗分数。"""
    scope = _parse_score_args(args.extract_plain_text().lower().split())
    if scope is None:
        await send_text(bot, event.group_id, USAGE_SCORE, reply_to=event.message_id)
        return
    if scope == "sum":
        if not await SUPERUSER(bot, event):
            await send_text(
                bot, event.group_id, SCORE_SUM_DENIED, reply_to=event.message_id
            )
            return
        total = await total_score(event.group_id)
        await send_text(
            bot,
            event.group_id,
            f"本群全部群成员的决斗分数总和为 {total} 分。",
            reply_to=event.message_id,
        )
        return
    score = await score_service.get_score(event.group_id, event.user_id)
    name = sender_name(event)
    await send_text(
        bot,
        event.group_id,
        f"{name} 的决斗分数为 {score} 分。",
        reply_to=event.message_id,
    )
