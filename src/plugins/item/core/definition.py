"""道具的身份与定义：品质、展示分类、功能标签与行为钩子。

道具分类有两个互不相交的轴：展示分类 kind（ItemKind，单值、互斥，用于
详情与统计）与功能标签 ItemTag（可多值，只记录“具备什么机制”，供其它
道具经 has_tag/items_with_tag 查询）。诅咒道具可省略 kind，由黑色诅咒
品质派生。
"""

from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass
from enum import Enum, IntEnum, StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.plugins.item.core.context import ItemStateExpireContext, ItemUseContext


class Quality(IntEnum):
    """道具品质，数值用于合成升降级运算与品质排序。"""

    BLACK_CURSE = -1
    GRAY = 0
    WHITE = 1
    GREEN = 2
    BLUE = 3
    PURPLE = 4
    GOLD = 5


# 品质的显示名
_QUALITY_LABELS = {
    Quality.BLACK_CURSE: "黑色诅咒",
    Quality.GRAY: "灰色垃圾",
    Quality.WHITE: "白色普通",
    Quality.GREEN: "绿色精良",
    Quality.BLUE: "蓝色稀有",
    Quality.PURPLE: "紫色史诗",
    Quality.GOLD: "金色传说",
}


def quality_label(quality: Quality) -> str:
    """返回品质的显示名。"""
    return _QUALITY_LABELS[quality]


class ItemKind(Enum):
    """道具分类：回答“这件道具是什么”，用于详情展示与统计（单值、互斥）。

    与功能标签（ItemTag）是两条互不相交的轴：分类是道具的身份，标签是
    道具具备的机制。黑色诅咒品质已能表达诅咒型分类，故诅咒道具可省略
    kind，由品质派生（见 ItemDefinition.resolved_kind）；需要新的展示
    分类时在此新增成员并同步 docs/count/item.md。

    耐久型专指效果仅为消耗耐久的道具（如 251 華為手錶）；耐久与其它
    机制叠加的道具仍记为复合型（如 886 逃之夭夭、132~136 保险）。
    """

    CONSUMABLE = "消耗型"
    CURSE = "诅咒型"
    BUFF = "Buff型"
    PERMANENT = "永久型"
    OPTIONAL = "可选型"
    COMPOSITE = "复合型"
    DURABLE = "耐久型"


class ItemTag(StrEnum):
    """道具的功能标签：只记录“具备什么机制”，与展示分类（ItemKind）不同轴。

    标签不参与详情展示与品质校验：展示分类回答“它是什么”（由 kind 表达），
    标签回答“它具备什么机制”；道具按需要声明多个标签，其它道具经
    has_tag/items_with_tag 查询，不依赖具体道具模块。
    """

    COPY = "复制型"
    """效果为复制道具的道具；以“非复制道具”为使用条件的道具据此排除
    （见 items/regain.py）。"""


# 状态到期回调的类型：过期状态被清理时由定时清理调用（参数见
# ItemStateExpireContext）；字符串前向引用以摆脱定义顺序约束
_StateExpireHandler = Callable[["ItemStateExpireContext"], Awaitable[None]]


@dataclass(frozen=True)
class ItemBehavior:
    """一件道具的行为钩子（与展示数据分离，纯展示道具不需要）。

    钩子均为可选：未提供时按框架默认处理（校验通过、消耗一件库存、
    无使用效果等）。行为块可在同组道具间共享（如一组道具使用同一对
    check/use），避免每件道具重复声明。
    """

    check: Callable[["ItemUseContext"], Awaitable[str | None]] | None = None
    """使用前校验：返回非空错误文案表示本次不可用（此时不消耗）。"""

    use: Callable[["ItemUseContext"], Awaitable[None]] | None = None
    """使用效果（成功提示消息由效果自身发送）。"""

    consume: Callable[["ItemUseContext"], Awaitable[bool]] | None = None
    """消耗道具的自定义方式（默认为消耗一件库存）；返回 False 表示未能
    消耗，本次使用中止且不执行效果。耐久型等道具在此自行扣减耐久或移除
    副本（由调用方在同一把群锁内 await，校验与消耗之间不会被同群的
    其它道具操作插入）。"""

    transferable: Callable[[int, int, str], Awaitable[int]] | None = None
    """返回成员可用于交换与合成的该道具副本数量（参数为群号、成员 QQ 号
    与道具编号；默认 None 表示全部库存均可参与）。交换与合成在库存数量
    满足要求后按该数量复核，如耐久耗损的副本可在此排除。"""

    expire: _StateExpireHandler | None = None
    """状态到期回调（由定时清理调用；未提供时过期状态只被静默删除）。"""

    durability_text: Callable[[int, int, str], Awaitable[str]] | None = None
    """详情消息中耐久条目的动态显示（参数为查看者所在群号、其 QQ 号与
    道具编号）；默认 None 表示使用 durability 的静态文本；供道具模块按
    成员状态动态展示（如显示耗损副本的剩余耐久）。"""


@dataclass(frozen=True)
class ItemDefinition:
    """一件道具的定义（由道具模块在导入期注册，注册后不可变）。

    字段分三类：身份（item_id/name/quality/kind/tags）、展示文案
    （description/effect/condition/timing/note/durability）与行为
    （behavior）。分类（kind）与标签（tags）是两条互不相交的轴，
    分别见 ItemKind 与 ItemTag。
    """

    item_id: str
    """道具编号：三位数字字符串（000~999），0 不可省略；
    必须唯一，且注册时不允许留空（编号由道具开发时指定）。"""

    name: str
    """道具名称（不要求唯一）。"""

    quality: Quality
    """品质。"""

    description: str
    """介绍。"""

    effect: str
    """功能。"""

    kind: ItemKind | None = None
    """展示分类（单值、互斥）：用于详情与分类统计。None 表示由品质派生，
    仅黑色诅咒道具可省略（派生为诅咒型）。"""

    tags: Collection[ItemTag] = ()
    """功能标签（可多个，注册时归一化为 frozenset）：只记录道具具备的
    机制（如复制型），供其它道具按功能查询；不参与详情展示与统计。"""

    condition: str | None = None
    """使用条件（展示文本）；None 表示“无”。"""

    timing: str | None = None
    """使用时机（展示文本）；None 表示“任意”。"""

    note: str = ""
    """备注（空字符串表示详情中不显示备注行）。"""

    durability: str = "1/1"
    """耐久（展示文本，一般为"剩余耐久/总耐久"）。"""

    accepts_args: bool = False
    """使用指令是否接受额外参数（可选型等道具为 True）。"""

    labels: tuple[tuple[str, str], ...] = ()
    """本道具产生的状态的显示名（状态键与显示名对，如
    ("314.ouroboros", "衔尾蛇")）；供其它道具在文案中显示状态名
    （见 state_label）。动态状态键（如按层编号的状态）可声明其公共
    前缀。同一状态键只应由产生该状态的道具声明。"""

    behavior: ItemBehavior | None = None
    """行为钩子（校验、消耗、使用效果、到期回调等）；纯展示道具可省略。"""

    def __post_init__(self) -> None:
        """把 tags 归一化为 frozenset，便于运行期查询与比较。"""
        object.__setattr__(self, "tags", frozenset(self.tags))

    @property
    def label(self) -> str:
        """道具的显示名（"<编号> <名称>"，如 "111 +1"）。"""
        return f"{self.item_id} {self.name}"

    @property
    def resolved_kind(self) -> ItemKind:
        """返回展示分类：未声明时由品质派生（黑色诅咒 → 诅咒型）。

        非黑色诅咒道具未声明 kind 属开发错误，注册时即抛出 ValueError；
        注册成功的定义不会再走到该异常分支。
        """
        if self.kind is not None:
            return self.kind
        if self.quality is Quality.BLACK_CURSE:
            return ItemKind.CURSE
        message = (
            f"道具 {self.item_id} 未声明分类（kind），且品质不是黑色诅咒，"
            "无法由品质派生"
        )
        raise ValueError(message)
