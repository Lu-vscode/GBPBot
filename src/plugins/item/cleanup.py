"""定时清理：过期的道具状态与过期的交换请求。

过期状态先按来源道具分发到期回调（如黑色诅咒的到期结算），回调失败
只记录错误；交换请求为静默清理。清理由 APScheduler 按固定间隔调用。
"""

from nonebot import logger
from nonebot_plugin_apscheduler import scheduler

from src.plugins.item.core import get_item, purge_expired_states
from src.plugins.item.exchange.queue import current_time, expire_exchanges

# 过期状态与交换请求的定时清理间隔（秒）
CLEANUP_INTERVAL_SECONDS = 30

# 状态到期处理时捕获的异常集合（单条处理失败不影响其它条目）
_CLEANUP_ERRORS = (Exception,)


async def cleanup_items() -> None:
    """清理过期的道具状态与交换请求，并分发出状态到期效果。

    过期状态先按来源道具分发到期回调（如黑色诅咒的到期结算），回调
    失败只记录错误；交换请求为静默清理。
    """
    expired = await purge_expired_states()
    if expired:
        logger.debug(f"已清理 {len(expired)} 条过期的道具状态")
    for entry in expired:
        definition = get_item(entry.state.item_id)
        behavior = definition.behavior if definition is not None else None
        if behavior is None or behavior.expire is None:
            continue
        try:
            await behavior.expire(entry)
        except _CLEANUP_ERRORS as exc:
            logger.error(
                f"处理道具 {entry.state.item_id} 的状态到期时出错"
                f"（群 {entry.group_id}，成员 {entry.user_id}）：{exc}"
            )
    expire_exchanges(current_time())


scheduler.add_job(
    cleanup_items,
    "interval",
    seconds=CLEANUP_INTERVAL_SECONDS,
    id="item_cleanup",
    replace_existing=True,
    misfire_grace_time=30,
)
