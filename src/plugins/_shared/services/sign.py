"""签到相关跨插件服务契约。

- SignScoreBonusService：由道具插件提供、签到插件调用（道具带来的签到
  额外分数）；
- SignMakeUpService：由签到插件提供、道具插件调用（按指定日期补上签到
  并发放签到奖励，如「补签卡」）。
"""

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from src.plugins._shared.services.item import GrantedItem


@runtime_checkable
class SignScoreBonusService(Protocol):
    """签到分数加成服务（由道具插件提供）。"""

    async def extra_score(self, group_id: int, user_id: int) -> int:
        """返回成员本次签到额外获得的决斗分数（无加成时返回 0）。

        由签到插件在发放签到奖励的同一流程内调用（不得落盘或发送
        消息），返回值与签到基础分数相加后计入本次签到获得的决斗
        分数；因此实现应按签到时刻的成员状态即时计算（如道具状态
        层数），并保持快捷。

        参数:
            group_id: 群号。
            user_id: 成员 QQ 号。
        """
        ...


@dataclass(frozen=True)
class MakeUpResult:
    """一次补签的结果（由 SignMakeUpService.make_up 返回）。"""

    signed: bool
    """是否补签成功（该日期已有签到记录时返回 False，此时不发放奖励）。"""

    score_gain: int = 0
    """本次补签计入的决斗分数（签到基础分数与道具加成之和；未补签时为 0）。"""

    granted: GrantedItem | None = None
    """本次补签获得的随机道具（抽取失败或未补签时为 None；黑色诅咒不在库存内）。"""


@runtime_checkable
class SignMakeUpService(Protocol):
    """补签服务（由签到插件提供）。"""

    async def has_record(self, group_id: int, user_id: int, date: str) -> bool:
        """判断成员是否已有指定日期的签到记录。

        供道具在消耗前校验（已签到的日期不能补签），不得修改任何数据
        或发送消息。

        参数:
            group_id: 群号。
            user_id: 成员 QQ 号。
            date: 补签日期（本地时区 YYYY-MM-DD）。
        """
        ...

    async def make_up(
        self, group_id: int, user_id: int, name: str, date: str
    ) -> MakeUpResult:
        """为成员补上指定日期的签到，并发放签到奖励。

        记录写入与奖励发放复用签到本身的流程（基础分数与道具加成、
        随机道具抽取与登记），补签获得的奖励与签到相同的判定为准；
        该日期已有签到记录时不发放任何奖励并返回 signed=False。普通
        道具写入库存（诅咒不在库存内），黑色诅咒的播报与自动使用由
        调用方在自己的提示消息之后经 ItemService.handle_acquisition
        完成。

        参数:
            group_id: 群号。
            user_id: 成员 QQ 号。
            name: 成员在群内的显示名（提供方按自身显示规范处理）。
            date: 补签日期（本地时区 YYYY-MM-DD）。
        """
        ...
