"""决斗状态查询服务。

实现对外提供的决斗状态服务（DuelStateService，实例 state_service 由插件
主模块统一注册），其它插件经服务注册中心查看某个群内进行中决斗的只读
快照。
"""

from src.plugins._shared.services import DuelSnapshot, DuelStateService
from src.plugins.duel.core.store import duels, snapshot


class _DuelStateService(DuelStateService):
    """决斗状态查询服务的实现：其它插件经服务注册中心查看进行中决斗。"""

    async def list_duels(self, group_id: int) -> list[DuelSnapshot]:
        """返回指定群内全部进行中决斗的只读快照（按创建顺序）。"""
        return [await snapshot(duel) for duel in duels if duel.group_id == group_id]


# 状态服务实例（由插件主模块统一注册）
state_service = _DuelStateService()
