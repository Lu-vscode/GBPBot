"""决斗插件重构后的结构与行为契约测试（不依赖数据库）。

检查内容：

1. 结构契约：包入口规模、core.__all__ 完整可访问、core 与 config 不依赖
   上层模块、messaging 不依赖服务与指令层。
2. 手势解析：合法/非法手势值、rps 与 face 段、从消息与原始结构提取手势。
3. 判定引擎：基础猜拳规则、单组规则（覆盖/放弃/异常/优先级）、所需手势
   配额（提高/取最大/非法值/异常）、整局判定（默认判定、多手势聚合、
   效果改写、非参与者与异常）。
4. 内存存储：查询、记录与移除手势、就绪判定、快照、被挑衅与代发跳过。
5. 其它：发起限频滑动窗口、指令注册、跨插件服务注册结果。

判定表在测试开始时清空、结束时恢复（加载期道具注册的规则依赖数据库，
不参与纯逻辑断言）。

用法（项目根目录或任意目录下均可运行）：

    .venv/Scripts/python.exe tests/duel/test_duel_contracts.py

退出码：0 表示全部断言通过；1 表示存在失败断言。
"""

import asyncio
import shutil
import sys
import time
from pathlib import Path
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

DUEL_PACKAGE = "src.plugins.duel"
DUEL_ROOT = PROJECT_ROOT / "src" / "plugins" / "duel"

# 装配点规模上限：防止子模块逻辑重回包入口
MAX_ENTRY_LINES = 160

# core.__all__ 的规模下限
MIN_CORE_EXPORTS = 20


def _module(name: str) -> Any:
    """按包内相对名取已加载的模块（插件加载阶段已完成导入）。"""
    return cast("Any", sys.modules[f"{DUEL_PACKAGE}.{name}"])


def _package() -> Any:
    """取决斗插件包本身。"""
    return cast("Any", sys.modules[DUEL_PACKAGE])


def _imports_any(modules: set[str], bases: tuple[str, ...]) -> list[str]:
    """返回 modules 中属于 bases（含子模块）的模块名。"""
    return sorted(
        module
        for module in modules
        if any(module == base or module.startswith(f"{base}.") for base in bases)
    )


def _check_layout(rec: Recorder) -> None:
    """检查子包依赖方向（静态扫描源码，不导入模块）。"""
    low_level = tuple(
        f"{DUEL_PACKAGE}.{name}"
        for name in ("flow", "services", "commands", "runtime", "limit", "models")
    )
    violations = [
        f"{relative(path)} -> {module}"
        for path in source_files(DUEL_ROOT / "core", DUEL_ROOT / "config")
        for module in _imports_any(module_imports(path), low_level)
    ]
    rec.truthy(
        "core/ 与 config/ 不依赖上层模块",
        not violations,
        "反向依赖：" + "；".join(violations),
    )

    upper = tuple(
        f"{DUEL_PACKAGE}.{name}"
        for name in ("services", "commands", "runtime", "limit", "models")
    )
    messaging_violations = [
        f"{relative(path)} -> {module}"
        for path in source_files(DUEL_ROOT / "messaging")
        for module in _imports_any(module_imports(path), upper)
    ]
    rec.truthy(
        "messaging/ 不依赖服务与指令层",
        not messaging_violations,
        "反向依赖：" + "；".join(messaging_violations),
    )

    init_lines = len(
        (DUEL_ROOT / "__init__.py").read_text(encoding="utf-8").splitlines()
    )
    rec.truthy(
        f"duel/__init__.py 保持装配点规模（≤{MAX_ENTRY_LINES} 行）",
        init_lines <= MAX_ENTRY_LINES,
        f"实际 {init_lines} 行",
    )


def _check_exports(rec: Recorder, core: Any) -> None:
    """检查包入口与核心包的公开导出。"""
    rec.equal(
        "包入口 __all__ 仅包含装配的子模块",
        _package().__all__,
        ["commands", "models", "runtime"],
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
        f"core.__all__ 覆盖决斗核心能力（≥{MIN_CORE_EXPORTS} 项）",
        len(names) >= MIN_CORE_EXPORTS,
        f"实际 {len(names)} 项",
    )


def _check_gestures(rec: Recorder, gestures: Any) -> None:
    """检查手势解析与提取。"""
    valid = ((1, 1), (2, 2), (3, 3), ("2", 2), (" 3 ", 3))
    for value, expected in valid:
        rec.equal(f"合法手势值 {value!r}", gestures.parse_gesture(value), expected)
    invalid = (True, False, 0, 4, None, "x", 2.0)
    rec.truthy(
        "非法手势值返回 None",
        all(gestures.parse_gesture(value) is None for value in invalid),
    )

    rec.equal("rps 段按 result 解析", gestures.segment_gesture("rps", {"result": 2}), 2)
    rec.equal(
        "rps 段无效结果返回 None",
        gestures.segment_gesture("rps", {"result": 9}),
        None,
    )
    rec.equal(
        "face 段按 resultId 解析",
        gestures.segment_gesture("face", {"id": 359, "resultId": "3"}),
        3,
    )
    rec.equal(
        "非包剪锤 face 段被忽略",
        gestures.segment_gesture("face", {"id": 1, "resultId": 3}),
        None,
    )
    rec.equal(
        "其它消息段被忽略",
        gestures.segment_gesture("text", {"result": 2}),
        None,
    )

    message = Message(
        [
            MessageSegment.text("前"),
            MessageSegment("rps", {"result": 1}),
            MessageSegment("rps", {"result": 3}),
        ]
    )
    rec.equal("从消息中提取第一个手势", gestures.extract_gesture(message), 1)

    rec.equal(
        "从 CQ 码字符串提取手势",
        gestures.message_to_gesture("[CQ:rps,result=2]"),
        2,
    )
    rec.equal(
        "从消息段数组提取手势",
        gestures.message_to_gesture([{"type": "rps", "data": {"result": 1}}]),
        1,
    )
    rec.equal(
        "非法消息结构返回 None",
        gestures.message_to_gesture([{"type": "rps", "data": None}, "x"]),
        None,
    )


def _check_base_outcome(rec: Recorder, core: Any) -> None:
    """检查基础猜拳规则。"""
    matrix = {
        (1, 3): 1,
        (2, 1): 1,
        (3, 2): 1,
        (3, 1): -1,
        (1, 2): -1,
        (2, 3): -1,
        (1, 1): 0,
        (2, 2): 0,
        (3, 3): 0,
    }
    for (actor, opponent), expected in matrix.items():
        rec.equal(
            f"基础判定 {actor} 对 {opponent}",
            core.base_outcome(actor, opponent),
            expected,
        )
    rec.truthy(
        "基础判定反对称",
        all(
            core.base_outcome(actor, opponent) == -core.base_outcome(opponent, actor)
            for actor in (1, 2, 3)
            for opponent in (1, 2, 3)
        ),
    )


def _pair_context(services: Any, judgement: Any, actor: int, opponent: int) -> Any:
    """构造一组手势判定的上下文。"""
    return services.DuelGesturePairContext(
        group_id=1,
        actor_id=10,
        actor_name="甲",
        actor_gesture=actor,
        opponent_id=20,
        opponent_name="乙",
        opponent_gesture=opponent,
        base_outcome=judgement.base_outcome(actor, opponent),
    )


async def _check_pair_verdict(rec: Recorder, judgement: Any, services: Any) -> None:
    """检查单组判定规则的覆盖、放弃、异常与优先级。"""
    context = _pair_context(services, judgement, 3, 2)
    rec.equal(
        "无规则时回落基础判定（胜）",
        (await judgement.pair_verdict(context)).outcome,
        1,
    )
    rec.equal(
        "无规则时回落基础判定（平）",
        (
            await judgement.pair_verdict(_pair_context(services, judgement, 1, 1))
        ).outcome,
        0,
    )

    async def override(_context: Any) -> Any:
        return services.DuelGesturePairVerdict(outcome=-1, detail="测试规则")

    before = len(judgement._pair_rules)
    judgement.register_pair_rule(90, "高优先级规则", override)
    judgement.register_pair_rule(90, "高优先级规则", override)
    rec.equal(
        "重复注册同一规则被忽略",
        len(judgement._pair_rules),
        before + 1,
    )
    rec.equal(
        "规则可覆盖基础判定",
        (await judgement.pair_verdict(context)).outcome,
        -1,
    )

    async def abstain(_context: Any) -> Any:
        return None

    judgement.register_pair_rule(100, "放弃规则", abstain)
    rec.equal(
        "放弃的规则不影响低优先级裁决",
        (await judgement.pair_verdict(context)).outcome,
        -1,
    )

    async def broken(_context: Any) -> Any:
        message = "测试规则异常"
        raise RuntimeError(message)

    judgement.register_pair_rule(200, "异常规则", broken)
    rec.equal(
        "规则异常被忽略且不影响低优先级裁决",
        (await judgement.pair_verdict(context)).outcome,
        -1,
    )

    async def opposite(_context: Any) -> Any:
        return services.DuelGesturePairVerdict(outcome=1)

    judgement.register_pair_rule(150, "相反规则", opposite)
    rec.equal(
        "多条规则不一致时采用最高优先级",
        (await judgement.pair_verdict(context)).outcome,
        1,
    )


async def _check_required(rec: Recorder, judgement: Any) -> None:
    """检查所需手势数量的提高、取最大与非法值处理。"""
    rec.equal("默认需要 1 个手势", await judgement.required_gestures(1, 10, 20), 1)

    def never() -> Any:
        """返回一个新的放弃判定函数（注册表按函数身份去重）。"""

        async def skip(_context: Any) -> Any:
            return None

        return skip

    async def three(_group: int, _user: int, _opponent: int) -> int:
        return 3

    async def five(_group: int, _user: int, _opponent: int) -> int:
        return 5

    async def zero(_group: int, _user: int, _opponent: int) -> int:
        return 0

    async def boolean(_group: int, _user: int, _opponent: int) -> Any:
        return True

    async def broken(_group: int, _user: int, _opponent: int) -> int:
        message = "测试配额异常"
        raise RuntimeError(message)

    judgement.register_effect(50, "三手势", never(), required=three)
    rec.equal(
        "配额提供者提高所需数量",
        await judgement.required_gestures(1, 10, 20),
        3,
    )

    judgement.register_effect(60, "五手势", never(), required=five)
    rec.equal(
        "多个提供者取最大值",
        await judgement.required_gestures(1, 10, 20),
        5,
    )

    judgement.register_effect(70, "零手势", never(), required=zero)
    judgement.register_effect(80, "布尔配额", never(), required=boolean)
    judgement.register_effect(90, "异常配额", never(), required=broken)
    rec.equal(
        "非法或异常的配额被忽略",
        await judgement.required_gestures(1, 10, 20),
        5,
    )


def _side(services: Any, user_id: int, name: str, *gestures: int) -> Any:
    """构造决斗一方的手势记录。"""
    return services.DuelGestureSide(user_id=user_id, name=name, gestures=gestures)


async def _check_judge(rec: Recorder, core: Any, judgement: Any, services: Any) -> None:
    """检查整局判定：默认判定、聚合与效果改写。"""
    challenger = _side(services, 1, "甲", 3)
    opponent = _side(services, 2, "乙", 2)
    rec.equal(
        "单手势判定：挑战者胜",
        (await core.judge(1, challenger, opponent)).winner_id,
        1,
    )
    rec.equal(
        "单手势判定：对手胜",
        (
            await core.judge(
                1, _side(services, 1, "甲", 2), _side(services, 2, "乙", 3)
            )
        ).winner_id,
        2,
    )
    rec.equal(
        "单手势判定：平局",
        (
            await core.judge(
                1, _side(services, 1, "甲", 1), _side(services, 2, "乙", 1)
            )
        ).winner_id,
        None,
    )
    rec.equal(
        "多手势逐手势聚合：任一手势胜即胜",
        (
            await core.judge(
                1,
                _side(services, 1, "甲", 2, 3),
                _side(services, 2, "乙", 1),
            )
        ).winner_id,
        1,
    )
    rec.equal(
        "缺少手势按平局处理",
        (
            await core.judge(1, _side(services, 1, "甲"), _side(services, 2, "乙", 1))
        ).winner_id,
        None,
    )

    async def override(context: Any) -> Any:
        return services.DuelGestureVerdict(
            winner_id=context.opponent.user_id, detail="测试效果"
        )

    judgement.register_effect(100, "测试效果", override)
    verdict = await core.judge(1, challenger, opponent)
    rec.equal("整局效果可改写胜负", verdict.winner_id, 2)
    rec.equal("整局效果的说明随结果返回", verdict.detail, "测试效果")

    async def outsider(_context: Any) -> Any:
        return services.DuelGestureVerdict(winner_id=999)

    judgement.register_effect(200, "越界效果", outsider)
    rec.equal(
        "效果给出的胜者非参与者时按平局处理",
        (await core.judge(1, challenger, opponent)).winner_id,
        None,
    )

    async def broken(_context: Any) -> Any:
        message = "测试效果异常"
        raise RuntimeError(message)

    judgement.register_effect(300, "异常效果", broken)
    rec.equal(
        "效果异常被忽略且不影响低优先级结果",
        (await core.judge(1, challenger, opponent)).winner_id,
        None,
    )


def _check_limit(rec: Recorder, limit: Any) -> None:
    """检查发起限频的滑动窗口。"""
    original = list(limit._initiations)
    try:
        limit._initiations.clear()
        rec.truthy("无记录时未达上限", not limit.limit_reached())

        now = time.monotonic()
        for _ in range(limit.HOURLY_LIMIT):
            limit._initiations.append(now)
        rec.truthy("登记达到上限后预检拦截", limit.limit_reached())
        rec.truthy("登记达到上限后拒绝登记", not limit.record_initiation())
        delay = limit.retry_after_seconds()
        rec.truthy(
            "预计等待时间落在窗口内",
            0 < delay <= limit._WINDOW_SECONDS,
            f"实际 {delay}",
        )

        limit._initiations.clear()
        limit._initiations.append(now - limit._WINDOW_SECONDS - 1)
        rec.truthy("滑出窗口的记录被清理", not limit.limit_reached())
        rec.truthy("滑出窗口后可再次登记", limit.record_initiation())
    finally:
        limit._initiations.clear()
        limit._initiations.extend(original)


def _duel_factory(duel_module: Any) -> Any:
    """返回构造测试用进行中决斗的工厂函数。"""
    duel_cls = duel_module.Duel

    def make(
        *,
        challenger: int = 1,
        opponent: int = 2,
        accepted: bool = True,
    ) -> Any:
        return duel_cls(
            group_id=1,
            bot_self_id=999,
            challenger_id=challenger,
            challenger_name="甲",
            opponent_id=opponent,
            opponent_name="乙",
            multiplier=2,
            created_at=time.monotonic(),
            accepted=accepted,
        )

    return make


async def _check_store_lookup(rec: Recorder, core: Any, make: Any) -> None:
    """检查决斗的查询、手势记录与移除。"""
    core.duels.clear()
    duel = make()
    core.add_duel(duel)
    rec.equal("按群查询进行中决斗", core.group_duels(1), [duel])
    rec.truthy("双方查找不限方向", core.find_pair_duel(1, 2, 1) is duel)
    rec.truthy("其它群查找不命中", core.find_pair_duel(9, 1, 2) is None)
    rec.truthy("已接受的决斗不算等待接受", core.find_pending_duel(1, 1, 2) is None)

    pending = make(accepted=False)
    core.add_duel(pending)
    rec.truthy(
        "未接受的决斗可按发起方向查找",
        core.find_pending_duel(1, 1, 2) is pending,
    )
    pending.provoked_by_bot = True
    rec.truthy(
        "被挑衅决斗可按发起方向查找",
        core.find_provoked_duel(1, 1, 2) is pending,
    )

    rec.truthy("记录参与者手势", await core.record_gesture(duel, 1, 3))
    rec.truthy(
        "配额已满时拒绝继续记录",
        not await core.record_gesture(duel, 1, 1),
    )
    rec.truthy(
        "非参与者不能记录手势",
        not await core.record_gesture(duel, 9, 1),
    )

    rec.truthy("按身份移除决斗", core.remove_duel(duel))
    rec.truthy("重复移除返回 False", not core.remove_duel(duel))
    rec.truthy(
        "已移除的决斗不能记录手势",
        not await core.record_gesture(duel, 2, 2),
    )


async def _check_store_gestures(rec: Recorder, core: Any, make: Any) -> None:
    """检查按最旧决斗记录手势与各种跳过情形。"""
    core.duels.clear()
    first = make()
    second = make(opponent=3)
    core.add_duel(first)
    core.add_duel(second)
    taken = await core.take_gesture(1, 1, 2)
    rec.truthy("手势记录到最旧的等待决斗", taken is first)
    rec.equal("最旧决斗记录了手势", first.gestures.get(1), [2])
    rec.truthy("其它决斗未受影响", 1 not in second.gestures)

    core.duels.clear()
    waiting = make(accepted=False)
    core.add_duel(waiting)
    rec.truthy(
        "未接受的决斗不记录手势",
        await core.take_gesture(1, 1, 2) is None,
    )

    core.duels.clear()
    provoked = make(challenger=4, opponent=5)
    provoked.provoked_by_bot = True
    core.add_duel(provoked)
    rec.truthy(
        "被挑衅方的表情不再记录",
        await core.take_gesture(1, 5, 2) is None,
    )

    core.duels.clear()
    active = make(challenger=6, opponent=7)
    active.bot_rps_active = True
    core.add_duel(active)
    rec.truthy(
        "机器人代发期间不记录对方手势",
        await core.take_gesture(1, 7, 1) is None,
    )


async def _check_store_ready(rec: Recorder, core: Any, make: Any) -> None:
    """检查就绪判定与快照。"""
    core.duels.clear()
    ready = make(challenger=8, opponent=9)
    core.add_duel(ready)
    ready.gestures[8] = [1]
    ready.gestures[9] = [2]
    rec.truthy("双方出手完成后决斗就绪", await core.duel_ready(ready))

    snapshot = await core.snapshot(ready)
    rec.equal("快照记录双方已完成手势", snapshot.gesture_users, frozenset({8, 9}))
    rec.truthy("快照剩余时间非负", snapshot.remaining_seconds >= 0)
    rec.equal(
        "快照保留点数与接受状态",
        (snapshot.multiplier, snapshot.accepted),
        (2, True),
    )


async def _check_store(rec: Recorder, core: Any, duel_module: Any) -> None:
    """检查进行中决斗的内存存储原语。"""
    make = _duel_factory(duel_module)
    original = list(core.duels)
    try:
        await _check_store_lookup(rec, core, make)
        await _check_store_gestures(rec, core, make)
        await _check_store_ready(rec, core, make)
    finally:
        core.duels[:] = original


def _check_commands(rec: Recorder) -> None:
    """检查决斗指令的注册情况。"""
    commands = plugin_commands("duel")
    expected = {
        "duel",
        "duel.accept",
        "duel.reject",
        "duel.rank",
        "duel.score",
        "duel.status",
    }
    missing = sorted(expected - commands)
    rec.truthy("决斗指令全部注册", not missing, "缺失：" + "、".join(missing))


def _check_services(rec: Recorder, services: Any) -> None:
    """检查跨插件服务的注册结果。"""
    duel_protocols = (
        "DuelScoreService",
        "DuelScoreInterceptService",
        "DuelStateService",
        "DuelEventService",
        "DuelSettleService",
        "DuelBotAcceptService",
        "DuelProvokeService",
        "DuelMultiplierService",
        "DuelCancelService",
        "DuelForfeitService",
        "DuelGestureService",
    )
    missing = [
        name
        for name in duel_protocols
        if services.get_service(getattr(services, name)) is None
    ]
    rec.truthy("决斗对外服务全部注册", not missing, "缺失：" + "、".join(missing))

    item_protocols = ("ItemService", "SignScoreBonusService")
    missing_item = [
        name
        for name in item_protocols
        if services.get_service(getattr(services, name)) is None
    ]
    rec.truthy(
        "道具对外服务全部注册", not missing_item, "缺失：" + "、".join(missing_item)
    )


async def _run() -> int:
    """清空判定表后执行全部契约检查，最后恢复注册内容。"""
    rec = Recorder("决斗结构契约与行为契约")
    core = _module("core")
    judgement = _module("core.judgement")
    services = cast("Any", sys.modules["src.plugins._shared.services"])

    original_rules = list(judgement._pair_rules)
    original_effects = list(judgement._effects)
    judgement._pair_rules.clear()
    judgement._effects.clear()
    try:
        _check_layout(rec)
        _check_exports(rec, core)
        _check_gestures(rec, _module("core.gestures"))
        _check_base_outcome(rec, core)
        await _check_pair_verdict(rec, judgement, services)
        judgement._pair_rules.clear()
        await _check_required(rec, judgement)
        judgement._effects.clear()
        await _check_judge(rec, core, judgement, services)
        await _check_store(rec, core, _module("core.duel"))
        _check_limit(rec, _module("limit"))
        _check_commands(rec)
        _check_services(rec, services)
    finally:
        judgement._pair_rules[:] = original_rules
        judgement._effects[:] = original_effects
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
