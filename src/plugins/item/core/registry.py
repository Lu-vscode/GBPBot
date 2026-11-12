"""道具注册表：定义校验、注册、查询与状态显示名。

道具模块在导入期调用 register_item 注册道具；新增道具文件由 items/__init__.py
自动发现导入，无需修改本模块。发现开发错误时抛出 ValueError，由加载器
记录 warning 并忽略该道具，不影响其它道具与整个插件包的加载。
"""

from nonebot import logger

from src.plugins.item.core.definition import (
    ItemDefinition,
    ItemKind,
    ItemTag,
    Quality,
    quality_label,
)
from src.plugins.item.storage.states import ItemState

# 已注册的道具：编号 -> 定义（注册顺序即道具模块导入顺序）
_ITEMS: dict[str, ItemDefinition] = {}

# 状态键 -> 状态显示名（由各道具的 labels 在注册时汇总）
_STATE_LABELS: dict[str, str] = {}

# 道具编号的位数（三位数字字符串）
_ITEM_ID_LENGTH = 3


def is_item_id(value: str) -> bool:
    """判断是否为合法的道具编号（三位 ASCII 数字字符串）。"""
    return len(value) == _ITEM_ID_LENGTH and value.isascii() and value.isdigit()


def register_item(definition: ItemDefinition) -> ItemDefinition:
    """注册一件道具并返回注册后的定义。

    注册在道具模块导入期执行，发现开发错误时抛出 ValueError：由
    items 加载器记录 warning 并忽略该道具，不影响其它道具与整个
    插件包的加载。校验规则：

    - 编号必须为三位数字字符串（000~999）且不允许留空；编号由道具
      开发时指定（设计未指定时应从 100 起向上寻找可用编号并写入代码，
      000~099 保留给强大/特殊的道具）
    - 编号不得与已注册道具重复
    - 品质为黑色诅咒与分类为诅咒型必须同时成立（互为充要条件）；
      非黑色诅咒道具未声明 kind 时无法派生分类，同样视为开发错误
    """
    if not definition.item_id:
        message = "道具编号不能留空，请在道具开发时指定编号"
        raise ValueError(message)
    if not is_item_id(definition.item_id):
        message = (
            f"道具编号必须为三位数字字符串（000~999），收到：{definition.item_id!r}"
        )
        raise ValueError(message)
    if definition.item_id in _ITEMS:
        message = (
            f"道具编号 {definition.item_id} 已被道具"
            f"「{_ITEMS[definition.item_id].name}」占用"
        )
        raise ValueError(message)
    kind = definition.resolved_kind
    if (definition.quality is Quality.BLACK_CURSE) != (kind is ItemKind.CURSE):
        message = (
            f"道具 {definition.item_id} 的品质与分类不一致：黑色诅咒品质与诅咒型"
            "分类必须同时出现"
        )
        raise ValueError(message)
    _ITEMS[definition.item_id] = definition
    _STATE_LABELS.update(dict(definition.labels))
    logger.info(
        f"道具已注册：{definition.label}（{quality_label(definition.quality)}、"
        f"{kind.value}）"
    )
    return definition


def register_items(*definitions: ItemDefinition) -> None:
    """按顺序批量注册多件道具（等价于逐件调用 register_item）。

    供成组道具（同一模块内的多件相似道具）用一条语句注册；任一件
    注册失败时抛出 ValueError，由 items 加载器记 warning 并忽略整个
    模块（与逐件注册的失败语义一致）。
    """
    for definition in definitions:
        register_item(definition)


def get_item(item_id: str) -> ItemDefinition | None:
    """按编号查询道具定义，未注册时返回 None。"""
    return _ITEMS.get(item_id)


def iter_items() -> list[ItemDefinition]:
    """返回全部已注册道具（按编号升序）。"""
    return [_ITEMS[item_id] for item_id in sorted(_ITEMS)]


def has_tag(item_id: str, tag: ItemTag) -> bool:
    """判断道具是否带指定功能标签（未注册的编号返回 False）。"""
    definition = _ITEMS.get(item_id)
    return definition is not None and tag in definition.tags


def items_with_tag(tag: ItemTag) -> frozenset[str]:
    """返回带指定功能标签的全部道具编号（未声明该标签时为空集）。

    由注册表实时汇总，新增道具只需在定义中声明标签即可被其它道具看见；
    道具数量有限，查询开销可忽略。
    """
    return frozenset(
        item_id for item_id, definition in _ITEMS.items() if tag in definition.tags
    )


def state_label(state: ItemState) -> str:
    """返回状态在文案中的显示名：优先产生该状态的道具声明的专用名称
    （精确匹配优先，动态状态键其次匹配已声明键的公共前缀），再次为该
    道具的名称，道具未注册时回退为状态键。"""
    label = _STATE_LABELS.get(state.key)
    if label is None:
        prefix = max(
            (key for key in _STATE_LABELS if state.key.startswith(key)),
            key=len,
            default=None,
        )
        if prefix is not None:
            label = _STATE_LABELS[prefix]
    if label is not None:
        return label
    definition = _ITEMS.get(state.item_id)
    if definition is not None:
        return definition.name
    return state.key


# 状态数据的变更序号（单元素列表以便在函数内递增）：每次状态集合
# 变化时递增，供道具模块缓存由状态派生的数据（见 state_version）
_states_version = [0]


def state_version() -> int:
    """返回状态数据的变更序号（状态新增、移除或过期清理后递增）。

    道具模块缓存由状态派生的数据时可记录构建时的序号，序号未变且
    缓存内容仍在有效期内即可复用（见 items/worm.py）。
    """
    return _states_version[0]


def bump_state_version() -> None:
    """标记状态数据已变更（由 core/states.py 的状态 API 调用）。"""
    _states_version[0] += 1
