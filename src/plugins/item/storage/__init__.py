"""道具存储层（包入口）：数据库模型与读写原语。

- models.py：`gbp_item_inventory` / `gbp_item_state` 两张表的模型；
- lock.py：按群号串行化的群锁 group_lock；
- inventory.py：库存读写原语（读取、增加、扣减、互换）；
- states.py：状态值对象 ItemState 与状态读写原语。

本层只负责数据库读写，不包含校验与业务语义；业务 API 见 core/。
"""

from src.plugins.item.storage.inventory import (
    add_to_inventory,
    load_item_counts,
    set_item_count,
    swap_items,
    take_from_inventory,
)
from src.plugins.item.storage.lock import group_lock
from src.plugins.item.storage.models import ItemInventory, ItemStateRow
from src.plugins.item.storage.states import (
    ItemState,
    delete_states,
    fetch_state,
    load_states,
    select_state_holders,
    take_expired_states,
    update_state_data,
    upsert_states,
)

__all__ = [
    "ItemInventory",
    "ItemState",
    "ItemStateRow",
    "add_to_inventory",
    "delete_states",
    "fetch_state",
    "group_lock",
    "load_item_counts",
    "load_states",
    "select_state_holders",
    "set_item_count",
    "swap_items",
    "take_expired_states",
    "take_from_inventory",
    "update_state_data",
    "upsert_states",
]
