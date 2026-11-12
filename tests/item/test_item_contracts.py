"""道具插件重构后的结构与行为契约测试（不依赖数据库）。

检查内容：

1. 结构契约：包入口只做装配、items/ 下道具模块只从 core 包导入、core 与
   storage 不反向依赖上层子包、旧单文件模块已删除且无残留引用、
   core.__all__ 完整可访问。
2. 注册表契约：编号格式、清单升序无重复、品质与分类一一对应、注册校验
   的五类开发错误、功能标签、状态显示名与状态变更序号。
3. 抽取与合成：各品质池非空、按品质/权重抽取、品质升降夹取、合成结果的
   三个概率分支、紫色大成功、合成材料校验（库存与可交易数量用桩替换）。
4. 消息与交换：@ 目标解析、道具显示名、交换队列的登记/查找/过期/移除、
   指令注册（基础指令、别名与 item.* 命名空间）。

用法（项目根目录或任意目录下均可运行）：

    .venv/Scripts/python.exe tests/item/test_item_contracts.py

退出码：0 表示全部断言通过；1 表示存在失败断言。
"""

import asyncio
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from nonebot.adapters.onebot.v11 import Message, MessageSegment

from tests._contract_support import (
    Recorder,
    init_nonebot,
    load_plugins,
    module_imports,
    plugin_commands,
    prepare_environment,
    relative,
    source_files,
)

ITEM_PACKAGE = "src.plugins.item"
ITEM_ROOT = PROJECT_ROOT / "src" / "plugins" / "item"

# 重构前的单文件模块，拆分后不应再出现
LEGACY_MODULES = ("_framework", "_craft", "_durability", "_models", "_store")

# 装配点规模与公开导出的下限：防止子模块重回包入口
MAX_ENTRY_LINES = 100
MIN_CORE_EXPORTS = 55
MIN_ITEMS = 60

# 紫色史诗大成功获得的道具件数
GREAT_SUCCESS_COUNT = 2

# core 与 storage 不得依赖的上层子包
UPPER_PACKAGES = (
    "commands",
    "exchange",
    "services",
    "cleanup",
    "messaging",
    "items",
    "config",
)


def _module(name: str) -> Any:
    """按包内相对名取已加载的模块（插件加载阶段已完成导入）。"""
    return cast("Any", sys.modules[f"{ITEM_PACKAGE}.{name}"])


def _package() -> Any:
    """取道具插件包本身。"""
    return cast("Any", sys.modules[ITEM_PACKAGE])


def _check_layout(rec: Recorder) -> None:
    """检查子包布局与依赖方向（静态扫描源码，不导入模块）。"""
    allowed = {f"{ITEM_PACKAGE}.core"}
    outside = [
        f"{relative(path)} -> {module}"
        for path in source_files(ITEM_ROOT / "items")
        for module in sorted(module_imports(path))
        if module.startswith(f"{ITEM_PACKAGE}.") and module not in allowed
    ]
    rec.truthy(
        "items/ 下道具模块只从 core 包导入",
        not outside,
        "越界导入：" + "；".join(outside),
    )

    upper = tuple(f"{ITEM_PACKAGE}.{name}" for name in UPPER_PACKAGES)
    back = [
        f"{relative(path)} -> {module}"
        for path in source_files(ITEM_ROOT / "core", ITEM_ROOT / "storage")
        for module in sorted(module_imports(path))
        if any(module == name or module.startswith(f"{name}.") for name in upper)
    ]
    rec.truthy(
        "core/ 与 storage/ 不反向依赖上层子包",
        not back,
        "反向依赖：" + "；".join(back),
    )

    existing = [name for name in LEGACY_MODULES if (ITEM_ROOT / f"{name}.py").exists()]
    rec.truthy(
        "旧的单文件模块已删除",
        not existing,
        "仍存在：" + "、".join(f"{name}.py" for name in existing),
    )

    leftovers = [
        f"{relative(path)} 含 {token}"
        for path in source_files(PROJECT_ROOT / "src", PROJECT_ROOT / "scripts")
        for token in (f"item.{name}" for name in LEGACY_MODULES)
        if token in path.read_text(encoding="utf-8")
    ]
    rec.truthy("源码中无旧模块引用", not leftovers, "；".join(leftovers))


def _check_exports(rec: Recorder, core: Any) -> None:
    """检查包入口的装配规模与公开导出。"""
    init_lines = len(
        (ITEM_ROOT / "__init__.py").read_text(encoding="utf-8").splitlines()
    )
    rec.truthy(
        f"item/__init__.py 保持装配点规模（≤{MAX_ENTRY_LINES} 行）",
        init_lines <= MAX_ENTRY_LINES,
        f"实际 {init_lines} 行",
    )
    rec.equal(
        "包入口 __all__ 仅包含装配的子模块",
        _package().__all__,
        ["cleanup", "commands", "exchange", "items", "services"],
    )

    names = core.__all__
    rec.truthy("core.__all__ 无重复", len(names) == len(set(names)))
    missing = [name for name in names if not hasattr(core, name)]
    rec.truthy(
        "core.__all__ 全部可从包访问",
        not missing,
        "缺失：" + "、".join(missing),
    )
    rec.truthy(
        f"core.__all__ 覆盖道具框架主要能力（≥{MIN_CORE_EXPORTS} 项）",
        len(names) >= MIN_CORE_EXPORTS,
        f"实际 {len(names)} 项",
    )


def _make_definition(
    definition: Any, item_id: str, quality: Any, kind: Any = None
) -> Any:
    """构造一件只用于注册校验的测试道具定义。"""
    return definition.ItemDefinition(
        item_id=item_id,
        name="契约测试道具",
        quality=quality,
        description="仅用于测试",
        effect="无",
        kind=kind,
    )


def _check_registry_basics(rec: Recorder, core: Any) -> None:
    """检查注册表的既有内容与查询行为。"""
    items = core.iter_items()
    rec.truthy(
        f"注册道具数量合理（≥{MIN_ITEMS} 件）",
        len(items) >= MIN_ITEMS,
        f"实际 {len(items)} 件",
    )

    bad_ids = [item.item_id for item in items if not core.is_item_id(item.item_id)]
    rec.truthy(
        "全部道具编号为三位数字",
        not bad_ids,
        "非法编号：" + "、".join(bad_ids),
    )

    ids = [item.item_id for item in items]
    rec.truthy(
        "道具清单按编号升序且无重复",
        ids == sorted(set(ids)),
        "编号序列：" + "、".join(ids),
    )

    mismatched = [
        item.label
        for item in items
        if (item.quality is core.Quality.BLACK_CURSE)
        != (item.resolved_kind is core.ItemKind.CURSE)
    ]
    rec.truthy(
        "黑色诅咒品质与诅咒型分类一一对应",
        not mismatched,
        "不一致：" + "、".join(mismatched),
    )

    first = items[0]
    rec.truthy("get_item 命中已注册道具", core.get_item(first.item_id) is first)
    rec.truthy("get_item 未注册编号返回 None", core.get_item("zzz") is None)

    valid = ("000", "099", "999")
    invalid = ("", "12", "1234", "12a", "\uff11\uff12\uff13", " 123")
    rec.truthy("合法编号判定", all(core.is_item_id(value) for value in valid))
    rec.truthy(
        "非法编号判定",
        not any(core.is_item_id(value) for value in invalid),
    )


def _check_registry_validation(
    rec: Recorder, core: Any, registry: Any, definition: Any
) -> None:
    """检查注册期的五类开发错误与合法注册。"""
    first = core.iter_items()[0]
    gray = core.Quality.GRAY
    consumable = core.ItemKind.CONSUMABLE

    rec.error_contains(
        "注册拒绝空编号",
        lambda: core.register_item(_make_definition(definition, "", gray, consumable)),
        "不能留空",
    )
    rec.error_contains(
        "注册拒绝非三位编号",
        lambda: core.register_item(
            _make_definition(definition, "12", gray, consumable)
        ),
        "三位数字",
    )
    rec.error_contains(
        "注册拒绝重复编号",
        lambda: core.register_item(
            _make_definition(definition, first.item_id, gray, consumable)
        ),
        "已被道具",
    )
    rec.error_contains(
        "注册拒绝非黑色诅咒却未声明分类",
        lambda: core.register_item(_make_definition(definition, "998", gray)),
        "无法由品质派生",
    )
    rec.error_contains(
        "注册拒绝黑色诅咒却声明非诅咒分类",
        lambda: core.register_item(
            _make_definition(definition, "998", core.Quality.BLACK_CURSE, consumable)
        ),
        "不一致",
    )

    temp = _make_definition(definition, "998", gray, consumable)
    try:
        registered = core.register_item(temp)
        rec.truthy("合法定义注册成功并返回同一对象", registered is temp)
        rec.truthy("注册后可按编号查询", core.get_item("998") is temp)
    finally:
        registry._ITEMS.pop("998", None)


def _check_registry_labels(rec: Recorder, core: Any, registry: Any) -> None:
    """检查功能标签与状态显示名。"""
    items = core.iter_items()
    tagged = core.items_with_tag(core.ItemTag.COPY)
    by_check = frozenset(
        item.item_id for item in items if core.has_tag(item.item_id, core.ItemTag.COPY)
    )
    rec.truthy("复制标签至少命中一件道具", bool(tagged))
    rec.equal("items_with_tag 与 has_tag 结果一致", tagged, by_check)
    rec.truthy(
        "未注册编号无功能标签",
        not core.has_tag("zzz", core.ItemTag.COPY),
    )

    state = core.ItemState
    rec.equal(
        "状态显示名：精确匹配",
        core.state_label(state(key="131.checkpoint", item_id="131")),
        "存档点",
    )
    rec.equal(
        "状态显示名：动态键按公共前缀匹配",
        core.state_label(state(key="314.ouroboros.layer2", item_id="314")),
        "衔尾蛇",
    )
    rec.equal(
        "状态显示名：未注册状态回退状态键",
        core.state_label(state(key="nope.key", item_id="not-registered")),
        "nope.key",
    )
    named = core.get_item("250")
    rec.truthy("用于显示名回退的道具已注册", named is not None)
    if named is not None:
        rec.equal(
            "状态显示名：无专用名称时回退道具名",
            core.state_label(state(key="unknown.state", item_id=named.item_id)),
            named.name,
        )

    before = core.state_version()
    registry.bump_state_version()
    rec.equal("状态变更序号递增", core.state_version(), before + 1)


def _check_draw(rec: Recorder, core: Any, registry: Any, draw: Any) -> None:
    """检查按品质抽取与按权重抽取。"""
    empty = [
        core.quality_label(quality)
        for quality in core.Quality
        if not any(item.quality is quality for item in core.iter_items())
    ]
    rec.truthy(
        "每个品质的道具池非空（抽取与合成前提）",
        not empty,
        "空池：" + "、".join(empty),
    )

    picked = core.draw_item_of_quality(core.Quality.GOLD)
    rec.truthy(
        "按品质抽取返回该品质道具",
        picked is not None and picked.quality is core.Quality.GOLD,
        f"实际 {picked!r}",
    )

    original_items = dict(registry._ITEMS)
    try:
        registry._ITEMS.clear()
        rec.truthy(
            "品质池为空时抽取返回 None",
            core.draw_item_of_quality(core.Quality.GRAY) is None,
        )
    finally:
        registry._ITEMS.clear()
        registry._ITEMS.update(original_items)

    original_weights = dict(draw._draw_weights)
    try:
        core.set_draw_weights({core.Quality.GRAY: 1.0})
        picked = core.draw_random_item()
        rec.truthy(
            "按权重抽取命中唯一品质",
            picked is not None and picked.quality is core.Quality.GRAY,
            f"实际 {picked!r}",
        )
    finally:
        core.set_draw_weights(original_weights)


def _check_shift_quality(rec: Recorder, core: Any) -> None:
    """检查品质升降的夹取语义。"""
    quality = core.Quality
    rec.equal(
        "灰色下调一档为黑色诅咒",
        core.shift_quality(quality.GRAY, -1),
        quality.BLACK_CURSE,
    )
    rec.equal(
        "黑色诅咒继续下调保持黑色诅咒",
        core.shift_quality(quality.BLACK_CURSE, -1),
        quality.BLACK_CURSE,
    )
    rec.equal(
        "金色上调两档夹取为金色传说",
        core.shift_quality(quality.GOLD, 2),
        quality.GOLD,
    )
    rec.equal(
        "蓝色上调一档为紫色史诗",
        core.shift_quality(quality.BLUE, 1),
        quality.PURPLE,
    )


def _check_craft_results(rec: Recorder, core: Any, craft: Any) -> None:
    """检查合成的三个概率分支与紫色大成功。"""
    cases = (
        (0.1, core.FAILED_PREFIX, core.Quality.GRAY),
        (0.5, core.SUCCESS_PREFIX, core.Quality.GREEN),
        (0.9, core.GREAT_SUCCESS_PREFIX, core.Quality.BLUE),
    )
    for roll, prefix, expected in cases:
        craft.random = SimpleNamespace(random=lambda value=roll: value)
        result = core.draw_craft_results(core.Quality.WHITE)
        rec.truthy(
            f"合成结果分支 roll={roll}（{prefix}）",
            result is not None
            and result[0] == prefix
            and all(item.quality is expected for item in result[1]),
            f"实际 {result!r}",
        )

    craft.random = SimpleNamespace(random=lambda: 0.9)
    result = core.draw_craft_results(core.Quality.PURPLE)
    rec.truthy(
        "紫色史诗大成功获得两件金色传说",
        result is not None
        and result[0] == core.GREAT_SUCCESS_PREFIX
        and len(result[1]) == GREAT_SUCCESS_COUNT
        and all(item.quality is core.Quality.GOLD for item in result[1]),
        f"实际 {result!r}",
    )


async def _check_craft_materials(rec: Recorder, core: Any, craft: Any) -> None:
    """检查合成材料校验（库存与可交易数量用桩替换，不访问数据库）。"""
    gray = [item for item in core.iter_items() if item.quality is core.Quality.GRAY]
    first, second = gray[0], gray[1]
    gold = next(item for item in core.iter_items() if item.quality is core.Quality.GOLD)
    black = next(
        item for item in core.iter_items() if item.quality is core.Quality.BLACK_CURSE
    )

    counts: dict[str, int] = {}
    transferable: dict[str, int] = {}

    async def count_stub(_group_id: int, _user_id: int, item_id: str) -> int:
        return counts.get(item_id, 0)

    async def transferable_stub(_group_id: int, _user_id: int, item_id: str) -> int:
        return transferable.get(item_id, counts.get(item_id, 0))

    original_count = craft.get_item_count
    original_transferable = craft.transferable_count
    try:
        craft.get_item_count = count_stub
        craft.transferable_count = transferable_stub

        rec.equal(
            "合成校验：品质不同",
            await core.craft_error(1, 1, first, gold),
            f"{first.label} 与 {gold.label} 的品质不同，无法合成。",
        )
        rec.equal(
            "合成校验：金色不可作为材料",
            await core.craft_error(1, 1, gold, gold),
            "金色传说道具无法合成。",
        )
        rec.equal(
            "合成校验：黑色诅咒不可作为材料",
            await core.craft_error(1, 1, black, black),
            "黑色诅咒无法作为合成材料。",
        )
        rec.equal(
            "合成校验：数量不足",
            await core.craft_error(1, 1, first, second),
            f"合成需要 {first.label}、{second.label} 各一件，你的数量不足。",
        )

        counts[first.item_id] = 1
        counts[second.item_id] = 1
        transferable[first.item_id] = 0
        rec.equal(
            "合成校验：数量满足但不可交易",
            await core.craft_error(1, 1, first, second),
            f"合成需要 {first.label}、{second.label} 各一件，"
            f"但 {first.label} 目前无法用于合成。",
        )

        transferable[first.item_id] = 1
        rec.equal(
            "合成校验：材料充足时通过",
            await core.craft_error(1, 1, first, second),
            None,
        )
        rec.equal(
            "合成校验：同种道具需要两件",
            await core.craft_error(1, 1, first, first),
            f"合成需要 {first.label}×2，你的数量不足。",
        )
    finally:
        craft.get_item_count = original_count
        craft.transferable_count = original_transferable


def _check_messaging(rec: Recorder, core: Any, messaging: Any) -> None:
    """检查 @ 目标解析与道具显示名。"""
    args = Message(
        [
            MessageSegment.at(123456),
            MessageSegment.at("all"),
            MessageSegment.text("随便"),
            MessageSegment.at("789012"),
        ]
    )
    rec.equal(
        "解析全部 @ 目标（忽略非数字 QQ）",
        messaging.parse_targets(args),
        (123456, 789012),
    )
    rec.equal("解析首个 @ 目标", messaging.parse_target(args), 123456)
    rec.equal(
        "无 @ 目标时返回空元组与 None",
        (
            messaging.parse_targets(Message("文本")),
            messaging.parse_target(Message("文本")),
        ),
        ((), None),
    )

    bot_id = 987654321
    stripped = SimpleNamespace(
        self_id=str(bot_id),
        original_message=Message(
            [MessageSegment.text("/item.use 711 "), MessageSegment.at(bot_id)]
        ),
    )
    rec.equal(
        "末尾 @机器人 被适配器剥离时补回为使用对象",
        messaging.parse_use_targets(Message("711"), stripped),
        (bot_id,),
    )
    rec.equal(
        "末尾 @机器人 与其它 @ 并存时补在最后",
        messaging.parse_use_targets(Message([MessageSegment.at(555)]), stripped),
        (555, bot_id),
    )
    rec.equal(
        "末尾 @机器人 未被剥离时不重复补回",
        messaging.parse_use_targets(Message([MessageSegment.at(bot_id)]), stripped),
        (bot_id,),
    )
    rec.equal(
        "未 @机器人 时不补回",
        messaging.parse_use_targets(
            Message("711"),
            SimpleNamespace(
                self_id=str(bot_id),
                original_message=Message(
                    [MessageSegment.text("/item.use 711 "), MessageSegment.at(555)]
                ),
            ),
        ),
        (),
    )

    known = core.iter_items()[0]
    rec.equal("已注册道具显示名", messaging.item_label(known.item_id), known.label)
    rec.equal("未注册道具显示名回退编号", messaging.item_label("zzz"), "zzz")


def _make_exchange(queue: Any, group_id: int, initiator: int, target: int) -> Any:
    """构造一笔测试用交换请求。"""
    return queue.PendingExchange(
        group_id=group_id,
        initiator_id=initiator,
        initiator_name="发起者",
        target_id=target,
        target_name="接收者",
        initiator_item_id="111",
        target_item_id="112",
        created_at=0.0,
    )


def _check_exchange(rec: Recorder, queue: Any) -> None:
    """检查交换队列的登记、查找、过期与移除。"""
    original = list(queue._exchanges)
    try:
        queue._exchanges.clear()
        exchange = _make_exchange(queue, 100, 1, 2)
        queue.append_exchange(exchange)
        rec.truthy(
            "登记的交换请求可按发起方向查找",
            queue.find_pending_exchange(100, 1, 2) is exchange,
        )
        rec.truthy(
            "反向查找不命中（只记录发起方向）",
            queue.find_pending_exchange(100, 2, 1) is None,
        )
        rec.truthy(
            "双方之间查找不限方向",
            queue.find_pair_exchange(100, 2, 1) is exchange,
        )
        rec.truthy(
            "其它群查找不命中",
            queue.find_pair_exchange(200, 1, 2) is None,
        )

        duration = queue.EXCHANGE_DURATION_SECONDS
        queue.expire_exchanges(duration - 0.1)
        rec.truthy(
            "有效期内请求保留",
            queue.find_pending_exchange(100, 1, 2) is exchange,
        )
        queue.expire_exchanges(duration)
        rec.truthy(
            "到达有效期后请求被丢弃",
            queue.find_pending_exchange(100, 1, 2) is None,
        )

        queue.append_exchange(exchange)
        rec.truthy(
            "按对象身份移除请求",
            queue.remove_exchange(exchange)
            and queue.find_pending_exchange(100, 1, 2) is None,
        )
        rec.truthy(
            "等值但不同对象的请求不可移除",
            not queue.remove_exchange(_make_exchange(queue, 100, 1, 2)),
        )
        rec.truthy(
            "移除不存在的请求返回 False",
            not queue.remove_exchange(exchange),
        )
        rec.truthy(
            "current_time 返回浮点时钟读数",
            isinstance(queue.current_time(), float),
        )
    finally:
        queue._exchanges[:] = original


def _check_commands(rec: Recorder) -> None:
    """检查道具指令的注册情况与命名空间。"""
    commands = plugin_commands("item")
    expected = {
        "item.list",
        "item.detail",
        "item.craft",
        "item.use",
        "item.get",
        "item.exchange",
        "item.exchange.accept",
        "item.exchange.reject",
    }
    missing = sorted(expected - commands)
    rec.truthy("道具基础指令全部注册", not missing, "缺失：" + "、".join(missing))
    rec.truthy("道具详情别名 item.info 可用", "item.info" in commands)
    outside = sorted(command for command in commands if not command.startswith("item."))
    rec.truthy(
        "道具指令统一使用 item.* 命名空间",
        not outside,
        "越界：" + "、".join(outside),
    )


async def _run() -> int:
    """执行全部契约检查并输出报告。"""
    rec = Recorder("道具结构契约与行为契约")
    core = _module("core")
    _check_layout(rec)
    _check_exports(rec, core)
    _check_registry_basics(rec, core)
    _check_registry_validation(
        rec, core, _module("core.registry"), _module("core.definition")
    )
    _check_registry_labels(rec, core, _module("core.registry"))
    _check_draw(rec, core, _module("core.registry"), _module("core.draw"))
    _check_shift_quality(rec, core)

    craft = _module("core.craft")
    original_odds = list(craft._craft_odds)
    original_random = craft.random
    try:
        core.set_craft_odds((0.2, 0.5, 0.3))
        _check_craft_results(rec, core, craft)
    finally:
        craft.random = original_random
        core.set_craft_odds(tuple(original_odds))
    await _check_craft_materials(rec, core, craft)

    _check_messaging(rec, core, _module("messaging"))
    _check_exchange(rec, _module("exchange.queue"))
    _check_commands(rec)
    return rec.report()


def main() -> int:
    """准备隔离环境、加载插件、执行检查并清理临时数据。"""
    tmp_dir = prepare_environment()
    try:
        init_nonebot()
        if not load_plugins(
            ["nonebot_plugin_orm", "src.plugins.duel", "src.plugins.item"]
        ):
            return 1
        return asyncio.run(_run())
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
