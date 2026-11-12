"""道具发放服务（由道具插件提供，签到插件调用）。"""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from nonebot.adapters.onebot.v11 import Bot


@dataclass(frozen=True)
class GrantedItem:
    """一次道具发放的结果（由 ItemService.grant_random 返回）。"""

    item_id: str
    """道具编号（三位数字字符串）。"""

    item_name: str
    """道具名称。"""


@runtime_checkable
class ItemService(Protocol):
    """道具发放服务（由道具插件提供）。"""

    async def grant_random(
        self, group_id: int, user_id: int, name: str
    ) -> GrantedItem | None:
        """为成员随机发放并登记一件道具，返回发放结果；抽取失败时返回 None。

        普通道具写入库存；黑色诅咒不写入库存，由调用方在其后调用
        handle_acquisition 完成信息播报与自动使用。

        参数:
            group_id: 群号。
            user_id: 成员 QQ 号。
            name: 成员在群内的显示名（提供方按自身显示规范处理）。
        """
        ...

    async def handle_acquisition(
        self,
        bot: "Bot",
        group_id: int,
        user_id: int,
        name: str,
        item: GrantedItem,
    ) -> None:
        """处理道具获得后的自动流程，应在调用方自己的提示消息之后调用。

        黑色诅咒会先发送完整信息再自动使用；其它道具为空操作。

        参数:
            bot: 发送道具消息的机器人实例。
            group_id: 群号。
            user_id: 成员 QQ 号。
            name: 成员在群内的显示名。
            item: grant_random 返回的道具发放结果。
        """
        ...
