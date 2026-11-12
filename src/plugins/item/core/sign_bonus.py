"""签到分数加成的登记与汇总。

道具模块经 set_sign_bonus_provider 登记自己的加成提供者；签到插件发放
决斗分数奖励前经跨插件服务汇总加成（见 services.py 的
SignScoreBonusService）。
"""

from collections.abc import Awaitable, Callable

from nonebot import logger

# 签到分数加成提供者：接收 (群号, 成员 QQ 号)，返回本次签到额外增加的
# 决斗分数（非正整数或执行出错时按无加成处理）
SignBonusProvider = Callable[[int, int], Awaitable[int]]

# 已登记的加成提供者：来源道具编号 -> 提供者（同编号登记覆盖，重复
# 导入安全）
_sign_bonus_providers: dict[str, SignBonusProvider] = {}

# 加成提供者的处理异常（单个提供者出错时不影响签到与其它提供者）
_SIGN_BONUS_ERRORS = (Exception,)


def set_sign_bonus_provider(item_id: str, provider: SignBonusProvider) -> None:
    """登记某件道具的签到分数加成提供者（同编号重复登记时覆盖）。

    签到插件发放决斗分数奖励前经跨插件服务汇总加成（见
    _shared/services/ 的 SignScoreBonusService）；同一件道具重复登记
    （如模块被重新导入）只保留最后一次登记的提供者。
    """
    _sign_bonus_providers[item_id] = provider


async def sign_bonus(group_id: int, user_id: int) -> int:
    """返回成员本次签到额外获得的决斗分数（各提供者之和，无加成时为 0）。

    供签到分数加成服务调用；单个提供者抛出的异常或返回的非正整数
    只记录日志并跳过，不影响签到与其它提供者。
    """
    total = 0
    for item_id, provider in _sign_bonus_providers.items():
        try:
            value = await provider(group_id, user_id)
        except _SIGN_BONUS_ERRORS as exc:
            logger.error(
                f"签到分数加成提供者执行出错（道具 {item_id}，"
                f"群 {group_id}，成员 {user_id}）：{exc}"
            )
            continue
        if isinstance(value, bool) or not isinstance(value, int):
            logger.warning(f"签到分数加成提供者返回值无效（道具 {item_id}）：{value!r}")
            continue
        if value > 0:
            total += value
    return total
