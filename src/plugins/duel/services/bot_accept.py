"""机器人接受概率函数服务。

实现 DuelBotAcceptService：供道具等插件校验并改写机器人接受决斗的概率
函数表达式（如「篡天」类道具改写 DUEL_BOT_ACCEPT_FUNC）。服务实例
bot_accept_service 由插件主模块统一注册。
"""

from src.plugins._shared.services import DuelBotAcceptService
from src.plugins.duel.config import accept_expr_error


class _DuelBotAcceptService(DuelBotAcceptService):
    """机器人接受概率函数服务的实现：供道具等插件校验表达式。"""

    def validate_accept_func(self, expr: str) -> str | None:
        """校验概率函数表达式；额外禁止连续下划线（防外部输入逃逸）。"""
        text = expr.strip()
        if not text:
            return "表达式不能为空。"
        if "__" in text:
            return "表达式不允许包含连续的下划线“__”。"
        return accept_expr_error(text)


# 服务实例（由插件主模块统一注册）
bot_accept_service = _DuelBotAcceptService()
