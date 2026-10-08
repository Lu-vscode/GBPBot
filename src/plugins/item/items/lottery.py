"""道具「抽奖券」组（编号 109、112、113、114）：由彩票衍生的道具组。

为减少风控，道具的中文名采用了更加保守的「抽奖券」。使用后按概率获得
或失去固定决斗分数；被诅咒的抽奖券在获得后自动使用：

- 109 低级抽奖券（灰色垃圾、消耗型）：50.5% 获得 100 决斗分数，
  49.5% 失去 100 决斗分数。
- 112 中级抽奖券（白色普通、消耗型）：55% 获得 100 决斗分数，
  45% 失去 100 决斗分数。
- 113 高级抽奖券（绿色精良、消耗型）：75% 获得 100 决斗分数，
  25% 失去 100 决斗分数。
- 114 被诅咒的抽奖券（黑色诅咒、诅咒型）：50% 失去 100 决斗分数，
  其余情况下决斗分数没有变化。
"""

import random

from src.plugins.item._framework import (
    ItemDefinition,
    ItemType,
    ItemUseContext,
    Quality,
    register_item,
)

# 单次获得或失去的决斗分数
_SCORE_DELTA = 100

# 消耗型抽奖券：编号、名称、品质、介绍、获得分数的概率、备注；
# 未命中获得概率时失去分数（两个方向变化的数值相同）
_LOTTERY_ITEMS = (
    ("109", "低级抽奖券", Quality.GRAY, "十分劣质的抽奖券", 0.505, "狗都不要"),
    ("112", "中级抽奖券", Quality.WHITE, "平平无奇的抽奖券", 0.55, "用来充数的道具"),
    (
        "113",
        "高级抽奖券",
        Quality.GREEN,
        "比较优秀的抽奖券",
        0.75,
        "没有更高级的抽奖券了",
    ),
)

# 各编号获得分数（而非失去分数）的概率：编号 -> 概率
_WIN_CHANCES = {
    item_id: win_chance for item_id, _, _, _, win_chance, _ in _LOTTERY_ITEMS
}

# 被诅咒的抽奖券：编号与失去分数的概率（未命中时无副作用）
_CURSE_ITEM_ID = "114"
_CURSE_LOSE_CHANCE = 0.5


def _percent_text(chance: float) -> str:
    """把概率渲染为百分比文案（整数百分比不保留小数部分）。"""
    return f"{chance * 100:.1f}".removesuffix(".0")


async def _handle_use(context: ItemUseContext) -> None:
    """使用效果：按概率获得或失去固定决斗分数。"""
    win_chance = _WIN_CHANCES[context.item.item_id]
    if random.random() < win_chance:
        context.add_score(_SCORE_DELTA)
        await context.send(f"你使用了 {context.item.label}，决斗分数+{_SCORE_DELTA}。")
        return
    context.add_score(-_SCORE_DELTA)
    await context.send(f"你使用了 {context.item.label}，决斗分数-{_SCORE_DELTA}。")


async def _handle_curse_use(context: ItemUseContext) -> None:
    """使用效果：按概率失去固定决斗分数，未命中时无副作用。"""
    if random.random() < _CURSE_LOSE_CHANCE:
        context.add_score(-_SCORE_DELTA)
        await context.send(f"黑色诅咒生效，祝你好运，你的决斗分数-{_SCORE_DELTA}。")
        return
    await context.send("黑色诅咒生效，幸运儿没有奖励，你的决斗分数没有变化。")


def _register_lottery_items() -> None:
    """注册本组的四件「抽奖券」道具。"""
    for item_id, name, quality, description, win_chance, note in _LOTTERY_ITEMS:
        register_item(
            ItemDefinition(
                item_id=item_id,
                name=name,
                quality=quality,
                types=(ItemType.CONSUMABLE,),
                description=description,
                effect=(
                    f"使用后有{_percent_text(win_chance)}%的概率"
                    f"获得{_SCORE_DELTA}决斗分数，"
                    f"{_percent_text(1 - win_chance)}%的概率"
                    f"失去{_SCORE_DELTA}决斗分数。"
                ),
                condition="无",
                timing="任意",
                note=note,
                handle_use=_handle_use,
            )
        )
    register_item(
        ItemDefinition(
            item_id=_CURSE_ITEM_ID,
            name="被诅咒的抽奖券",
            quality=Quality.BLACK_CURSE,
            types=(ItemType.CURSE,),
            description="祝你好运",
            effect=(
                f"使用后有{_percent_text(_CURSE_LOSE_CHANCE)}%的概率"
                f"失去{_SCORE_DELTA}决斗分数。"
            ),
            condition="无",
            timing="自动使用",
            note="幸运儿没有奖励",
            handle_use=_handle_curse_use,
        )
    )


_register_lottery_items()
