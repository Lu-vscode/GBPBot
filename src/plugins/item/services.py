"""道具插件对外提供的跨插件服务实现（契约见 _shared/services/）。

- ItemService：签到等插件发放道具后调用 grant_random 抽取并登记，再调用
  handle_acquisition 完成黑色诅咒的展示与自动使用；
- SignScoreBonusService：签到插件据此汇总道具带来的额外分数（如「+1」
  状态按层数加成）。
"""

from nonebot import logger
from nonebot.adapters.onebot.v11 import Bot

from src.plugins._shared.services import (
    GrantedItem,
    ItemService,
    SignScoreBonusService,
    register_service,
)
from src.plugins.item.core import (
    Quality,
    acquire_curse,
    add_item,
    draw_random_item,
    get_item,
    quality_label,
    sign_bonus,
)


class _ItemService(ItemService):
    """道具发放服务的实现：其它插件经服务注册中心调用它发放道具。"""

    async def grant_random(
        self, group_id: int, user_id: int, name: str
    ) -> GrantedItem | None:
        """为成员随机发放并登记一件道具，返回发放结果；抽取失败时返回 None。

        普通道具写入库存；黑色诅咒不写入库存，由调用方在其后调用
        handle_acquisition 完成信息播报与自动使用。
        """
        definition = draw_random_item()
        if definition is None:
            return None
        if definition.quality != Quality.BLACK_CURSE:
            await add_item(group_id, user_id, definition.item_id)
        logger.info(
            f"群 {group_id} 成员 {user_id}（{name}）获得道具 {definition.label}"
            f"（{quality_label(definition.quality)}）"
        )
        return GrantedItem(item_id=definition.item_id, item_name=definition.name)

    async def handle_acquisition(
        self,
        bot: Bot,
        group_id: int,
        user_id: int,
        name: str,
        item: GrantedItem,
    ) -> None:
        """处理道具获得后的自动流程（黑色诅咒：先展示完整信息再自动使用）。"""
        definition = get_item(item.item_id)
        if definition is None or definition.quality != Quality.BLACK_CURSE:
            return
        await acquire_curse(bot, group_id, user_id, name, definition)


class _SignScoreBonusService(SignScoreBonusService):
    """签到分数加成服务的实现：汇总各道具登记的加成提供者。"""

    async def extra_score(self, group_id: int, user_id: int) -> int:
        """返回成员本次签到额外获得的决斗分数（无道具加成时为 0）。"""
        return await sign_bonus(group_id, user_id)


# 服务实例：/item.get 与签到等插件共用同一实例
item_service = _ItemService()

# 将道具发放与签到分数加成注册为跨插件服务供其它插件调用
register_service(ItemService, item_service)
register_service(SignScoreBonusService, _SignScoreBonusService())
