"""签到分数加成服务（由道具插件提供，签到插件调用）。"""

from typing import Protocol, runtime_checkable


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
