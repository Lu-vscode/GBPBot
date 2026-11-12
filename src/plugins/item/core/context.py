"""道具使用与状态到期的上下文对象（注入给道具模块的钩子）。

ItemUseContext 随 /item.use、诅咒自动使用与道具效果复用传递；状态到期
回调（ItemBehavior.expire）拿到 ItemStateExpireContext，到期时刻没有消息
事件，发送所需信息由道具模块在状态附加数据中自行保存。
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from src.plugins._shared.onebot import send_group_text
from src.plugins.item.core.definition import ItemDefinition
from src.plugins.item.core.scores import add_user_score
from src.plugins.item.storage.states import ItemState

if TYPE_CHECKING:
    from nonebot.adapters.onebot.v11 import Bot


@dataclass
class ItemUseContext:
    """一次道具使用的上下文（注入给道具模块的 check/use 等行为钩子）。"""

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

    async def add_score(self, delta: int) -> int:
        """经决斗分数服务为使用者增减决斗分数，返回更新后的分数。"""
        return await add_user_score(self.group_id, self.user_id, self.user_name, delta)


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
