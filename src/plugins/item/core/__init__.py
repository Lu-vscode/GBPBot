"""道具核心（包入口）：道具模块与插件内其它模块的统一导入点。

道具模块（items/ 下）只需从本包导入：注册与查询（register_item、get_item、
iter_items、has_tag 等）、库存与状态 API、使用流程与钩子、抽取与合成、
耐久行为块，以及品质与展示分类。各关注点的实现分别在：

- definition.py：品质（Quality）、展示分类（ItemKind）、功能标签（ItemTag）、
  行为钩子（ItemBehavior）与道具定义（ItemDefinition）；
- context.py：使用上下文（ItemUseContext）与状态到期上下文；
- use.py：使用流程（run_item_use）、使用后钩子、诅咒拦截器、详情文本与
  黑色诅咒获得流程；
- registry.py：注册表、定义校验、状态显示名与状态变更序号；
- inventory.py / states.py：库存与状态 API（组合 storage/ 的读写原语）；
- scores.py / sign_bonus.py：决斗分数服务的桥接与签到分数加成；
- draw.py / craft.py / durability.py：随机抽取、合成与耐久型行为。

群锁（group_lock）经本包转出：道具子指令需要在同一段互斥区内完成
多步写操作时使用（如「二阶堂希罗」回溯时的整群库存替换与分数恢复）。

本包不做跨插件交互：签到发放道具与诅咒自动处理经 _shared/services/ 的
ItemService（实现见 item/services.py）。
"""

from src.plugins.item.core.context import ItemStateExpireContext, ItemUseContext
from src.plugins.item.core.craft import (
    FAILED_PREFIX,
    GREAT_SUCCESS_PREFIX,
    SUCCESS_PREFIX,
    craft_error,
    draw_craft_results,
    set_craft_odds,
    shift_quality,
)
from src.plugins.item.core.definition import (
    ItemBehavior,
    ItemDefinition,
    ItemKind,
    ItemTag,
    Quality,
    quality_label,
)
from src.plugins.item.core.draw import (
    draw_item_of_quality,
    draw_random_item,
    set_draw_weights,
)
from src.plugins.item.core.durability import durable_behavior, move_damaged_instance
from src.plugins.item.core.inventory import (
    add_item,
    add_items,
    exchange_items,
    get_item_count,
    list_user_items,
    remove_item,
    remove_items,
    restore_group_inventory,
    snapshot_group_inventory,
    transferable_count,
)
from src.plugins.item.core.registry import (
    get_item,
    has_tag,
    is_item_id,
    items_with_tag,
    iter_items,
    register_item,
    register_items,
    state_label,
    state_version,
)
from src.plugins.item.core.scores import (
    add_user_score,
    add_user_scores,
    get_highest_score_user,
    get_lowest_score_user,
    get_rank_entries,
    get_user_score,
)
from src.plugins.item.core.sign_bonus import set_sign_bonus_provider, sign_bonus
from src.plugins.item.core.states import (
    add_state,
    add_states,
    get_state,
    list_state_holders,
    list_states,
    purge_expired_states,
    remove_state,
    remove_states,
    set_state_data,
)
from src.plugins.item.core.use import (
    acquire_curse,
    add_curse_interceptor,
    add_item_use_hook,
    detail_text,
    run_item_use,
)
from src.plugins.item.storage import group_lock
from src.plugins.item.storage.states import ItemState

__all__ = [
    "FAILED_PREFIX",
    "GREAT_SUCCESS_PREFIX",
    "SUCCESS_PREFIX",
    "ItemBehavior",
    "ItemDefinition",
    "ItemKind",
    "ItemState",
    "ItemStateExpireContext",
    "ItemTag",
    "ItemUseContext",
    "Quality",
    "acquire_curse",
    "add_curse_interceptor",
    "add_item",
    "add_item_use_hook",
    "add_items",
    "add_state",
    "add_states",
    "add_user_score",
    "add_user_scores",
    "craft_error",
    "detail_text",
    "draw_craft_results",
    "draw_item_of_quality",
    "draw_random_item",
    "durable_behavior",
    "exchange_items",
    "get_highest_score_user",
    "get_item",
    "get_item_count",
    "get_lowest_score_user",
    "get_rank_entries",
    "get_state",
    "get_user_score",
    "group_lock",
    "has_tag",
    "is_item_id",
    "items_with_tag",
    "iter_items",
    "list_state_holders",
    "list_states",
    "list_user_items",
    "move_damaged_instance",
    "purge_expired_states",
    "quality_label",
    "register_item",
    "register_items",
    "remove_item",
    "remove_items",
    "remove_state",
    "remove_states",
    "restore_group_inventory",
    "run_item_use",
    "set_craft_odds",
    "set_draw_weights",
    "set_sign_bonus_provider",
    "set_state_data",
    "shift_quality",
    "sign_bonus",
    "snapshot_group_inventory",
    "state_label",
    "state_version",
    "transferable_count",
]
