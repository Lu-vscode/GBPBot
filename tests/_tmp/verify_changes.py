"""临时验证脚本：校验本轮技术债处理项（1-A、3-A、4、6、7、9、13）。

用法（项目根目录）：
    .venv/Scripts/python.exe tests/_tmp/verify_changes.py

脚本先切换到独立的临时目录再加载插件，数据文件写入临时目录，不会
读写项目真实的 data/ 数据；结束后删除临时目录。仅供本次改动验证，
验证通过后可删除本文件。
"""

# ruff: noqa: PLW0603, T201, PLR2004, FBT001

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

_checks = 0
_failures: list[str] = []


def check(condition: bool, message: str) -> None:
    """记录一条断言结果。"""
    global _checks
    _checks += 1
    if condition:
        print(f"  ok   {message}")
        return
    _failures.append(message)
    print(f"  FAIL {message}")


def section(title: str) -> None:
    print(f"\n== {title} ==")


def _load_plugins() -> bool:
    """初始化 nonebot 并加载 duel / item 插件，返回是否全部成功。"""
    import nonebot

    os.chdir(PROJECT_ROOT)
    nonebot.init(log_level="CRITICAL")
    work_dir = Path(tempfile.mkdtemp(prefix="gbpbot-verify-"))
    os.chdir(work_dir)
    print(f"临时数据目录：{work_dir}")

    nonebot.load_plugin("nonebot_plugin_localstore")
    nonebot.load_plugin("nonebot_plugin_apscheduler")
    duel_plugin = nonebot.load_plugin("src.plugins.duel")
    item_plugin = nonebot.load_plugin("src.plugins.item")
    check(duel_plugin is not None, "duel 插件加载成功")
    check(item_plugin is not None, "item 插件加载成功")
    return duel_plugin is not None and item_plugin is not None


def _test_jsonfile(work_dir: Path) -> None:
    section("1-A jsonfile：原子写与损坏保护")
    from src.plugins._shared import jsonfile

    target = work_dir / "atomic.json"
    check(
        jsonfile.save_json_atomic(target, {"中文": [1, 2]}, "测试数据") is True,
        "原子写返回成功",
    )
    check(
        json.loads(target.read_text(encoding="utf-8")) == {"中文": [1, 2]},
        "写入内容可完整读回（UTF-8）",
    )
    check(not list(work_dir.glob(".*.tmp")), "没有残留的临时文件")
    check(
        jsonfile.load_json_object(target, "测试数据") == {"中文": [1, 2]},
        "正常文件读取成功",
    )

    corrupt = work_dir / "corrupt.json"
    corrupt.write_text("{not json", encoding="utf-8")
    check(jsonfile.load_json_object(corrupt, "测试数据") == {}, "损坏文件返回空对象")
    check(not corrupt.exists(), "损坏文件已被移走（保留现场）")
    backups = list(work_dir.glob("corrupt.json.corrupt-*"))
    check(len(backups) == 1, "损坏文件备份存在且唯一")
    check(
        jsonfile.save_json_atomic(corrupt, {"a": 1}, "测试数据") is False,
        "读取失败后拒绝写入（不覆盖损坏现场）",
    )
    check(not corrupt.exists(), "拒绝写入后没有生成新文件")

    non_object = work_dir / "array.json"
    non_object.write_text("[1, 2]", encoding="utf-8")
    check(
        jsonfile.load_json_object(non_object, "测试数据") == {},
        "非对象 JSON 按损坏处理",
    )
    check(len(list(work_dir.glob("array.json.corrupt-*"))) == 1, "非对象文件已备份")


def _test_batch_inventory() -> None:
    section("3-A 批量库存 API")
    from src.plugins.item import _framework as fw

    group, user_a, user_b = 90001, 91001, 91002

    fw.add_items(group, user_a, ["111", "111", "222"])
    check(fw.get_item_count(group, user_a, "111") == 2, "add_items 处理重复编号")
    check(fw.get_item_count(group, user_a, "222") == 1, "add_items 逐件 +1")

    check(
        fw.remove_items(group, user_a, [("111", 3), ("222", 1)]) is False,
        "任一数量不足时整体失败",
    )
    check(fw.get_item_count(group, user_a, "111") == 2, "失败时不修改库存")
    check(
        fw.remove_items(group, user_a, [("111", 1), ("222", 1)]) is True,
        "批量扣减成功",
    )
    check(fw.get_item_count(group, user_a, "111") == 1, "批量扣减数量正确")
    check(fw.get_item_count(group, user_a, "222") == 0, "扣减到 0 的条目被删除")

    fw.add_items(group, user_b, ["333"])
    check(
        fw.exchange_items(group, user_a, "111", user_b, "333") is True,
        "道具交换成功",
    )
    check(fw.get_item_count(group, user_a, "333") == 1, "发起方获得对方道具")
    check(fw.get_item_count(group, user_b, "111") == 1, "对方获得发起方道具")
    check(
        fw.exchange_items(group, user_a, "999", user_b, "333") is False,
        "库存不足时不交换",
    )
    check(fw.get_item_count(group, user_a, "999") == 0, "交换失败时发起方库存不变")
    check(fw.get_item_count(group, user_b, "333") == 0, "交换失败时对方库存不变")


def _test_batch_states() -> None:
    section("3-A 批量状态 API 与状态版本号")
    from src.plugins.item import _framework as fw
    from src.plugins.item._storage import ItemState

    group, user = 90002, 92001
    version = fw.state_version()

    fw.add_states(
        group,
        user,
        states=[
            ItemState(key="test.a", item_id="111", data={"n": 1}),
            ItemState(key="test.b", item_id="111"),
        ],
        duration_seconds=3600,
    )
    check(fw.state_version() == version + 1, "add_states 只递增一次版本号")
    keys = {state.key for state in fw.list_states(group, user)}
    check(keys == {"test.a", "test.b"}, "add_states 一次登记多件状态")
    state_a = fw.get_state(group, user, "test.a")
    check(state_a is not None and state_a.data == {"n": 1}, "状态附加数据完整")
    check(
        state_a is not None
        and state_a.expires_at is not None
        and state_a.expires_at > 0,
        "整批共用一个过期时刻",
    )

    version = fw.state_version()
    check(fw.remove_states(group, user, ["test.a", "missing"]) == 1, "移除数量正确")
    check(fw.state_version() == version + 1, "remove_states 递增版本号")
    check(fw.remove_states(group, user, ["missing"]) == 0, "无移除时不递增版本号")
    check(fw.state_version() == version + 1, "无变化时版本号不变")

    version = fw.state_version()
    expired = fw.purge_expired_states()
    check(fw.state_version() == version + (1 if expired else 0), "清理按需递增版本号")
    check(fw.get_state(group, user, "test.b") is not None, "未过期状态保留")

    fw.remove_states(group, user, ["test.b"])


def _test_batch_scores() -> None:
    section("3-A 批量分数 API")
    from src.plugins.item import _framework as fw

    group = 90006
    fw.add_user_scores(group, [(96001, "甲", 5), (96002, "乙", -3), (96001, "甲", 2)])
    check(fw.get_user_score(group, 96001) == 7, "同一成员多条记录按顺序累加")
    check(fw.get_user_score(group, 96002) == -3, "负分正确写入")
    fw.add_user_scores(group, [])
    check(fw.get_user_score(group, 96001) == 7, "空序列不改变分数")


def _test_durability() -> None:
    section("9 耐久副本公共逻辑")
    from src.plugins.item import _framework as fw
    from src.plugins.item._storage import ItemState
    from src.plugins.item.items import flee, wrath

    def context(item_id: str, group: int, user: int) -> Any:
        from src.plugins.item._framework import ItemUseContext

        definition = fw.get_item(item_id)
        assert definition is not None
        return ItemUseContext(
            bot=None,  # type: ignore[arg-type]
            group_id=group,
            user_id=user,
            user_name="测试员",
            item=definition,
            args=(),
            reply_to=None,
        )

    group, user = 90003, 93001
    fw.add_item(group, user, "095", 1)
    check(wrath._detail_durability_king(group, user) == "4/1000000", "095 满耐久显示")
    check(wrath._transferable_count_kings(group, user) == 1, "095 全新副本可交换")
    check(
        wrath._handle_consume_king(context("095", group, user)) is True,
        "095 消耗成功",
    )
    check(
        fw.get_state(group, user, "095.durability") is not None
        and fw.get_state(group, user, "095.durability").data["remaining"] == [3],
        "095 首个副本耐久 4 -> 3",
    )
    check(
        wrath._detail_durability_king(group, user) == "3/1000000",
        "095 耗损后显示剩余耐久",
    )
    check(wrath._transferable_count_kings(group, user) == 0, "095 耗损副本不可交换")
    for _ in range(3):
        wrath._handle_consume_king(context("095", group, user))
    check(fw.get_item_count(group, user, "095") == 0, "095 耐久归零后道具消失")
    check(fw.get_state(group, user, "095.durability") is None, "095 耐久状态被清理")

    fw.add_item(group, user, "886", 2)
    check(flee._detail_durability(group, user) == "3/3", "886 满耐久显示")
    flee._handle_consume(context("886", group, user))
    flee._handle_consume(context("886", group, user))
    check(flee._detail_durability(group, user) == "1/3", "886 剩余耐久显示")
    check(flee._transferable_count(group, user) == 1, "886 耗损一件后仍有一件可交换")
    flee._handle_consume(context("886", group, user))
    check(fw.get_item_count(group, user, "886") == 1, "886 首件归零后移除一件库存")
    check(
        fw.get_state(group, user, "886.durability") is None,
        "886 无耗损副本时状态被清理",
    )

    # 数据异常的状态附加数据不影响读取
    fw.add_state(
        group,
        user,
        state=ItemState(
            key="886.durability",
            item_id="886",
            data={"remaining": ["bad", True, 0, -1, 2]},
        ),
    )
    from src.plugins.item.items import _durability

    check(
        _durability.instances(group, user, "886.durability") == [2],
        "异常耐久数据被过滤",
    )


def _test_worm_cache() -> None:
    section("4 worm 群昵称缓存")
    from src.plugins.item import _framework as fw
    from src.plugins.item._storage import ItemState
    from src.plugins.item.items import worm

    group = 90004
    check(worm._group_pattern(group) is None, "无持有者时返回 None")
    check(group in worm._group_patterns, "无持有者的空结果被缓存")
    cached = worm._group_patterns[group]
    check(cached[2] is None and cached[1] == float("inf"), "空结果缓存为永久命中")

    fw.add_state(
        group,
        94001,
        state=ItemState(
            key="curse.678",
            item_id="678",
            data={"names": ["阿区宝宝", "阿区"]},
        ),
        duration_seconds=3600,
    )
    pattern = worm._group_pattern(group)
    check(pattern is not None, "登记状态后缓存重建")
    check(worm._rewrite_text("阿区宝宝来了", pattern) == "区宝宝来了", "长名优先替换")
    check(worm._rewrite_text("阿区来了", pattern) == "区宝宝来了", "短名替换")
    check(worm._group_pattern(group) is pattern, "状态未变化时复用缓存对象")

    fw.remove_state(group, 94001, "curse.678")
    check(worm._group_pattern(group) is None, "移除状态后缓存失效为 None")

    # 到期时刻参与缓存：过期但未清理的状态不再参与替换
    import time

    fw.add_state(
        group,
        94002,
        state=ItemState(key="curse.678", item_id="678", data={"names": ["即将过期"]}),
        duration_seconds=0.01,
    )
    check(worm._group_pattern(group) is not None, "短时效状态登记后缓存重建")
    deadline = worm._group_patterns[group][1]
    check(deadline < time.time() + 1, "缓存记录了最早过期时刻")
    time.sleep(0.02)
    check(worm._group_pattern(group) is None, "状态过期后缓存失效")


def _test_quality_and_craft() -> None:
    section("6 品质夹取与黑色诅咒合成拦截")
    from src.plugins.item import _craft_error, _shift_quality
    from src.plugins.item._framework import Quality

    check(_shift_quality(Quality.GRAY, -1) is Quality.BLACK_CURSE, "灰色降级为黑色诅咒")
    check(_shift_quality(Quality.BLACK_CURSE, -1) is Quality.BLACK_CURSE, "下界夹取")
    check(_shift_quality(Quality.GOLD, 1) is Quality.GOLD, "上界夹取")
    check(_shift_quality(Quality.WHITE, 2) is Quality.BLUE, "正常升两级")
    check(_shift_quality(Quality.GRAY, 0) is Quality.GRAY, "零变化保持品质")

    from src.plugins.item import _framework as fw

    curse_a = fw.get_item("404")
    curse_b = fw.get_item("678")
    assert curse_a is not None and curse_b is not None
    check(
        _craft_error(90005, 95001, curse_a, curse_b) == "黑色诅咒无法作为合成材料。",
        "黑色诅咒作为材料被拦截",
    )


def _test_state_labels() -> None:
    section("13 state_labels 查表")
    from src.plugins.item._framework import state_label
    from src.plugins.item._storage import ItemState

    cases = [
        ("314.ouroboros", "314", "衔尾蛇"),
        ("121.gesture", "121", "石头"),
        ("122.gesture", "122", "剪刀"),
        ("123.gesture", "123", "布"),
        ("124.gesture", "124", "破平"),
        ("curse.678", "678", "区"),
        ("404.not_found", "404", "404"),
    ]
    for key, item_id, expected in cases:
        label = state_label(ItemState(key=key, item_id=item_id))
        check(label == expected, f"{key} -> {expected}（实际 {label}）")

    fallback = state_label(ItemState(key="000.zero", item_id="000"))
    check(fallback == "归零者", "未声明的状态回退为道具名称")
    unknown = state_label(ItemState(key="999.unknown", item_id="999"))
    check(unknown == "999.unknown", "道具未注册时回退为状态键")


def _test_shared_config() -> None:
    section("7 共享配置 NICKNAME_MAX_LENGTH")
    from src.plugins._shared.config import NICKNAME_MAX_LENGTH
    from src.plugins.duel import _config as duel_config

    check(
        NICKNAME_MAX_LENGTH == 12,
        f"共享配置读取到环境值（实际 {NICKNAME_MAX_LENGTH}）",
    )
    check(
        not hasattr(duel_config, "NICKNAME_MAX_LENGTH"),
        "duel 配置不再定义自己的截断长度",
    )
    check(
        not hasattr(duel_config.Config, "duel_nickname_max_length"),
        "duel 配置项 DUEL_NICKNAME_MAX_LENGTH 已移除",
    )
    from src.plugins.item.items import worm

    check(worm._NICKNAME_MAX_LENGTH == NICKNAME_MAX_LENGTH, "道具侧引用共享常量")


def main() -> int:
    work_dir = None
    try:
        if not _load_plugins():
            print("\n插件加载失败，终止验证")
            return 1
        work_dir = Path.cwd()
        _test_jsonfile(work_dir)
        _test_batch_inventory()
        _test_batch_states()
        _test_batch_scores()
        _test_durability()
        _test_worm_cache()
        _test_quality_and_craft()
        _test_state_labels()
        _test_shared_config()
    finally:
        os.chdir(PROJECT_ROOT)

    print(f"\n共 {_checks} 项检查，失败 {len(_failures)} 项")
    for failure in _failures:
        print(f"  - {failure}")
    if work_dir is not None:
        shutil.rmtree(work_dir, ignore_errors=True)
    return 0 if not _failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
