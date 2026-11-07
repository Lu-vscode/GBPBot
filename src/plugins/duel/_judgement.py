"""决斗的手势判定引擎与手势效果注册。

胜负判定集中在本模块，分三层，下层为上层的基础：

1. 基础规则：布胜石头、剪刀胜布、石头胜剪刀，手势相同为平局（见
   WINNING_GESTURES）；
2. 单组判定规则（DuelGesturePairRule）：在基础规则之上判定一组手势，
   如「特殊手势」组的状态触发；
3. 整局判定效果（DuelGestureJudge）：在单组判定之上裁决整局胜负，并
   可同时提高成员需要发送的猜拳表情数量（如「强运」）。

道具经决斗手势服务（DuelGestureService）注册规则，不再各自在 settling
事件里改写胜负：两类注册都按优先级从高到低询问，取得出结果的最高优先级
者，多条来源给出不一致的结果时记录警告日志；未注册或全部放弃时回落到
下一层。单个规则抛出的异常只记录错误日志、其结果被忽略，不影响决斗流程
与其它规则。

所需手势数量由整局判定效果的 required 提供者决定（默认 1，多个效果取
最大值）；提高数量的效果必须同时注册整局判定，否则多手势的胜负会落到
兜底聚合判定并记录警告（属开发配置缺失）。
"""

from nonebot import logger

from src.plugins._shared.services import (
    DuelGestureContext,
    DuelGestureJudge,
    DuelGesturePairContext,
    DuelGesturePairOutcome,
    DuelGesturePairRule,
    DuelGesturePairVerdict,
    DuelGestureQuotaProvider,
    DuelGestureSide,
    DuelGestureVerdict,
)
from src.plugins.duel._gestures import WINNING_GESTURES

# 规则与效果的执行异常（单个来源出错时不影响决斗流程与其它来源）
_RULE_ERRORS = (Exception,)

# 已注册的单组判定规则：(优先级, 显示名, 规则)
_pair_rules: list[tuple[int, str, DuelGesturePairRule]] = []

# 已注册的整局判定效果：(优先级, 显示名, 判定, 所需手势提供者)
_effects: list[tuple[int, str, DuelGestureJudge, DuelGestureQuotaProvider | None]] = []


def register_pair_rule(priority: int, name: str, rule: DuelGesturePairRule) -> None:
    """注册单组手势判定规则（同一规则重复注册会被忽略）。"""
    if any(registered is rule for _, _, registered in _pair_rules):
        return
    _pair_rules.append((priority, name, rule))


def register_effect(
    priority: int,
    name: str,
    judge: DuelGestureJudge,
    *,
    required: DuelGestureQuotaProvider | None = None,
) -> None:
    """注册整局判定效果（同一判定重复注册会被忽略）。"""
    if any(registered is judge for _, _, registered, _ in _effects):
        return
    _effects.append((priority, name, judge, required))


def base_outcome(actor_gesture: int, opponent_gesture: int) -> int:
    """按基础猜拳规则判定一组手势：1 胜、0 平、-1 负（相对第一组手势）。"""
    if actor_gesture == opponent_gesture:
        return 0
    if (actor_gesture, opponent_gesture) in WINNING_GESTURES:
        return 1
    return -1


async def pair_verdict(context: DuelGesturePairContext) -> DuelGesturePairVerdict:
    """按已注册的单组判定规则判定一组手势（全部放弃时用基础规则）。"""
    verdicts: list[tuple[int, str, DuelGesturePairVerdict]] = []
    for priority, name, rule in sorted(_pair_rules, key=lambda entry: -entry[0]):
        try:
            verdict = await rule(context)
        except _RULE_ERRORS as exc:
            logger.error(f"单组手势判定规则执行出错（{name}）：{exc!r}")
            continue
        if verdict is not None:
            verdicts.append((priority, name, verdict))
    if not verdicts:
        return DuelGesturePairVerdict(outcome=context.base_outcome)
    if len({verdict.outcome for _, _, verdict in verdicts}) > 1:
        summary = "、".join(
            f"{name}={verdict.outcome}" for _, name, verdict in verdicts
        )
        logger.warning(
            f"多条单组手势判定规则给出不一致的结果（群 {context.group_id}，"
            f"成员 {context.actor_id} 对 {context.opponent_id}）：{summary}，"
            f"已采用优先级最高的 {verdicts[0][1]}"
        )
    return verdicts[0][2]


async def required_gestures(group_id: int, user_id: int, opponent_id: int) -> int:
    """返回成员在该场决斗中需要发送的猜拳表情数量（至少 1）。

    默认需要 1 个，注册整局判定效果后按其 required 提供者返回的数量提高
    （多个效果取最大值）；返回非正整数的提供者被忽略并记录警告，单个
    提供者抛出的异常只记录错误日志、其结果被忽略。
    """
    required = 1
    for _, name, _, provider in _effects:
        if provider is None:
            continue
        try:
            value = await provider(group_id, user_id, opponent_id)
        except _RULE_ERRORS as exc:
            logger.error(f"手势配额提供者执行出错（{name}）：{exc!r}")
            continue
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            logger.warning(
                f"手势配额提供者返回了无效的数量 {value!r}（{name}），已忽略"
            )
            continue
        required = max(required, value)
    return required


def _make_pair_outcome(
    group_id: int, challenger: DuelGestureSide, opponent: DuelGestureSide
) -> DuelGesturePairOutcome:
    """构造整局判定上下文使用的单组判定函数（按参与者 QQ 号取显示名）。"""
    names = {challenger.user_id: challenger.name, opponent.user_id: opponent.name}

    async def resolve(
        actor_id: int, actor_gesture: int, opponent_id: int, opponent_gesture: int
    ) -> DuelGesturePairVerdict:
        return await pair_verdict(
            DuelGesturePairContext(
                group_id=group_id,
                actor_id=actor_id,
                actor_name=names.get(actor_id, f"QQ {actor_id}"),
                actor_gesture=actor_gesture,
                opponent_id=opponent_id,
                opponent_name=names.get(opponent_id, f"QQ {opponent_id}"),
                opponent_gesture=opponent_gesture,
                base_outcome=base_outcome(actor_gesture, opponent_gesture),
            )
        )

    return resolve


async def _aggregate(
    context: DuelGestureContext, multi: DuelGestureSide, single: DuelGestureSide
) -> int:
    """把多手势方逐手势对对方首手势的判定聚合为整局结果。

    至少一个手势胜则该方胜；否则至少一个手势平则平局；否则该方负。
    返回 1 多手势方胜、0 平局、-1 多手势方负。
    """
    outcomes = [
        (
            await context.pair_outcome(
                multi.user_id, gesture, single.user_id, single.gestures[0]
            )
        ).outcome
        for gesture in multi.gestures
    ]
    if 1 in outcomes:
        return 1
    if 0 in outcomes:
        return 0
    return -1


async def _default_verdict(context: DuelGestureContext) -> DuelGestureVerdict:
    """没有整局判定效果裁决时的默认判定。

    双方各一个手势时按单组判定（含单组规则与基础规则）；任一方需要多个
    手势时按逐手势聚合兜底（每次只与对方首手势比对），正常流程下多手势
    必有对应的整局判定效果，走到兜底聚合说明效果注册缺失，记录警告。
    """
    challenger = context.challenger
    opponent = context.opponent
    if not challenger.gestures or not opponent.gestures:
        logger.error(
            f"群 {context.group_id} 的决斗缺少手势记录，判定为平局"
            f"（{challenger.user_id} 与 {opponent.user_id}）"
        )
        return DuelGestureVerdict(winner_id=None)
    if len(challenger.gestures) == 1 and len(opponent.gestures) == 1:
        verdict = await context.pair_outcome(
            challenger.user_id,
            challenger.gestures[0],
            opponent.user_id,
            opponent.gestures[0],
        )
        if verdict.outcome == 0:
            return DuelGestureVerdict(winner_id=None, detail=verdict.detail)
        winner_id = challenger.user_id if verdict.outcome > 0 else opponent.user_id
        return DuelGestureVerdict(winner_id=winner_id, detail=verdict.detail)
    if len(challenger.gestures) > 1:
        multi, single = challenger, opponent
    else:
        multi, single = opponent, challenger
    logger.warning(
        f"群 {context.group_id} 的决斗需要多个手势但没有整局判定效果裁决"
        f"（成员 {multi.user_id}），已按逐手势聚合兜底判定"
    )
    outcome = await _aggregate(context, multi, single)
    if outcome == 0:
        return DuelGestureVerdict(winner_id=None)
    return DuelGestureVerdict(
        winner_id=multi.user_id if outcome > 0 else single.user_id
    )


async def judge(
    group_id: int, challenger: DuelGestureSide, opponent: DuelGestureSide
) -> DuelGestureVerdict:
    """判定一场决斗的最终胜负：先询问整局判定效果，全部放弃时按默认判定。

    返回的 winner_id 为本场决斗的参与方或 None（平局）；detail 为整局
    判定效果给出的结算说明（如道具效果），由决斗插件拼在默认结算消息前。
    """
    context = DuelGestureContext(
        group_id=group_id,
        challenger=challenger,
        opponent=opponent,
        pair_outcome=_make_pair_outcome(group_id, challenger, opponent),
    )
    verdicts: list[tuple[int, str, DuelGestureVerdict]] = []
    for priority, name, effect_judge, _required in sorted(
        _effects, key=lambda entry: -entry[0]
    ):
        try:
            verdict = await effect_judge(context)
        except _RULE_ERRORS as exc:
            logger.error(f"整局判定效果执行出错（{name}）：{exc!r}")
            continue
        if verdict is not None:
            verdicts.append((priority, name, verdict))
    if not verdicts:
        return await _default_verdict(context)
    if len({verdict.winner_id for _, _, verdict in verdicts}) > 1:
        summary = "、".join(
            f"{name}={verdict.winner_id}" for _, name, verdict in verdicts
        )
        logger.warning(
            f"多条整局判定效果给出不一致的结果（群 {group_id}，"
            f"{challenger.user_id} 与 {opponent.user_id}）：{summary}，"
            f"已采用优先级最高的 {verdicts[0][1]}"
        )
    chosen = verdicts[0][2]
    if chosen.winner_id is not None and chosen.winner_id not in (
        challenger.user_id,
        opponent.user_id,
    ):
        logger.error(
            f"整局判定效果 {verdicts[0][1]} 给出的胜者 {chosen.winner_id} 不是本场"
            f"决斗的参与者（群 {group_id}），已按平局处理"
        )
        return DuelGestureVerdict(winner_id=None, detail=chosen.detail)
    return chosen
