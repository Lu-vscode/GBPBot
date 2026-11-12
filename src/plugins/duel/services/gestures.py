"""决斗手势服务。

实现 DuelGestureService：供道具等插件注册单组手势判定规则与整局判定
效果（可同时提高成员所需发送的猜拳表情数量），判定由 core/judgement
统一执行（如「特殊手势」与「强运」效果）。服务实例 gesture_service 由
插件主模块统一注册。
"""

from src.plugins._shared.services import (
    DuelGestureJudge,
    DuelGesturePairRule,
    DuelGestureQuotaProvider,
    DuelGestureService,
)
from src.plugins.duel.core.judgement import register_effect, register_pair_rule


class _DuelGestureService(DuelGestureService):
    """决斗手势服务的实现：供道具等插件注册判定规则与整局判定效果。"""

    def register_pair_rule(
        self, priority: int, name: str, rule: DuelGesturePairRule
    ) -> None:
        """注册单组手势判定规则（同一规则重复注册会被忽略）。"""
        register_pair_rule(priority, name, rule)

    def register_effect(
        self,
        priority: int,
        name: str,
        judge: DuelGestureJudge,
        *,
        required: DuelGestureQuotaProvider | None = None,
    ) -> None:
        """注册整局判定效果（同一判定重复注册会被忽略）。"""
        register_effect(priority, name, judge, required=required)


# 手势服务实例（由插件主模块统一注册）
gesture_service = _DuelGestureService()
