"""GBPBot 本地压力测试:高并发 + 频繁触发消息阈值下的异常行为检测。

测试方式（不使用任何虚拟插件）:
- 真实执行 nonebot.init()，加载项目 .env / .env.dev 真实配置；
- 真实注册 OneBot V11 适配器，从原始上报 JSON 解析事件（走适配器全部解析逻辑）；
- 真实调用 bot.handle_event() 驱动完整事件分发链（预处理、全部 8 个本地插件响应器、
  后处理），所有插件代码、频率限制、状态管理都是真实运行；并按生产配置加载内置
  插件 echo、single_session：同一会话（群_成员）的并发重复消息会被 single_session
  去重（日志 "is ignored"），属框架正常行为——并发场景按真实行为断言，需要验证
  插件自身守卫时改用串行分发（S5/S6）；
- 仅伪造 adapter._call_api（网络出口），把 QQ 服务器应答替换为本地记录与模拟响应；
- localstore 数据目录重定向到脚本目录下的 .stress_tmp_data/，不污染项目 data/。

场景:
  S0 插件加载与基础响应自检
  S1 消息阈值打满：阻断传播、按会话节流忙提示、同会话忙提示去重
  S2 滑动窗口恢复：等待窗口滚动后正常处理
  S3 全插件混合高频并发突发（实名群/命令/私聊/notice/lifecycle/react/duel）
  S4 昵称保护并发（group_card notice / lifecycle / 定时任务重叠）
  S5 决斗并发一致性（发起/接受/拒绝/猜拳结算/bot 自动接受/积分）
  S6 实名群成员冷却 + 错误注入（发送失败、get_msg 失败下的健壮性）

运行:  在仓库根目录执行 .venv\\Scripts\\python.exe tests\\stress\\_stress_test.py
报告:  与脚本同目录的 _stress_report.log（UTF-8，含 loguru 全量日志与检查结论）
"""

import asyncio
import copy
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
# 仓库根目录 = 向上第一个含 pyproject.toml 的目录（脚本位于 tests/stress/ 下）
REPO_ROOT = next(p for p in SCRIPT_DIR.parents if (p / "pyproject.toml").is_file())
os.chdir(REPO_ROOT)
# 仓库根目录加入 sys.path：插件按 src.plugins.* 模块名导入，脚本位于子目录时必需
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
# 临时数据目录（localstore 重定向目标）：每次运行前清空，保证插件状态干净
_TMP_DATA_DIR = SCRIPT_DIR / ".stress_tmp_data"
if _TMP_DATA_DIR.exists():
    shutil.rmtree(_TMP_DATA_DIR, ignore_errors=True)
os.environ["LOCALSTORE_DATA_DIR"] = str(_TMP_DATA_DIR)

import nonebot  # noqa: E402
from nonebot.adapters.onebot.v11 import Adapter as OneBotAdapter  # noqa: E402
from nonebot.adapters.onebot.v11 import Bot as OneBotBot  # noqa: E402
from nonebot.adapters.onebot.v11 import Message, MessageSegment  # noqa: E402
from nonebot.adapters.onebot.v11.exception import ActionFailed  # noqa: E402

nonebot.init()

from nonebot.log import logger  # noqa: E402

REPORT_PATH = SCRIPT_DIR / "_stress_report.log"
_report_file = open(REPORT_PATH, "w", encoding="utf-8")  # noqa: SIM115
logger.remove()
logger.add(
    _report_file,
    level="DEBUG",
    colorize=False,
    format="{time:HH:mm:ss.SSS} [{level}] {name} | {message}",
)
sys.stdout = _report_file
sys.stderr = _report_file

SELF_ID = "10001"
BOT_NICKNAME = "Q群管家Pro"

driver = nonebot.get_driver()
driver.register_adapter(OneBotAdapter)
adapter = driver._adapters[OneBotAdapter.get_name()]  # pyright: ignore[reportPrivateUsage]
bot = OneBotBot(adapter, self_id=SELF_ID)
adapter.bot_connect(bot)

T0 = time.monotonic()


def now() -> float:
    return time.monotonic()


# =====================================================================
# 伪造网络出口（仅替代 QQ 服务器应答）
# =====================================================================
SEND_APIS = frozenset(
    {
        "send_msg",
        "send_private_msg",
        "send_group_msg",
        "send_private_forward_msg",
        "send_group_forward_msg",
        "send_forward_msg",
    }
)


class FakeGateway:
    """记录所有 API 调用并返回本地模拟响应。"""

    def __init__(self) -> None:
        self.sends: list[dict[str, Any]] = []
        self.api_calls: list[tuple[float, str]] = []
        self._seq = 0
        self._messages: dict[int, Any] = {}
        self.reply_targets: dict[int, dict[str, Any]] = {}
        self.member_cards: dict[int, str] = {}
        self.bot_cards: dict[int, str] = {}
        self.group_list: list[int] = []
        self.bot_rps_gesture = 3  # 机器人猜拳表情的回查结果（3=布）
        self.fail_send_sessions: set[str] = set()  # 持续失败注入（键 group_{id}/user_{id}）
        self.fail_get_msg_ids: set[int] = set()  # get_msg 一次性失败注入
        self.send_failures: list[dict[str, Any]] = []  # 因注入失败而未发出的发送
        self.emoji_likes: list[dict[str, Any]] = []
        self.set_cards: list[dict[str, Any]] = []

    # ---- 便捷查询 ----
    def session_of(self, rec: dict[str, Any]) -> str:
        data = rec["data"]
        if data.get("group_id") is not None:
            return f"group_{data['group_id']}"
        return f"user_{data.get('user_id')}"

    def sends_in(self, start: int) -> list[dict[str, Any]]:
        return self.sends[start:]

    def text_of(self, rec: dict[str, Any]) -> str:
        return rec["text"]

    def is_busy(self, rec: dict[str, Any]) -> bool:
        return rec["text"] == BUSY_MESSAGE

    async def call(self, _bot: Any, api: str, **data: Any) -> Any:  # noqa: ANN401
        t = now()
        self.api_calls.append((t, api))

        if api in SEND_APIS:
            session = (
                f"group_{data['group_id']}"
                if data.get("group_id") is not None
                else f"user_{data.get('user_id')}"
            )
            if session in self.fail_send_sessions:
                self.send_failures.append({"t": t, "api": api, "data": data})
                raise ActionFailed(
                    retcode=100, message="注入的发送失败", wording=""
                )
            self._seq += 1
            mid = self._seq
            message = data.get("message")
            self._messages[mid] = copy.deepcopy(message)
            text = (
                message.extract_plain_text()
                if isinstance(message, Message)
                else str(message)
            )
            self.sends.append(
                {
                    "t": t,
                    "api": api,
                    "data": data,
                    "message_id": mid,
                    "text": text,
                    "group_id": data.get("group_id"),
                    "user_id": data.get("user_id"),
                }
            )
            return {"message_id": mid}

        if api == "get_msg":
            mid = int(data.get("message_id", 0))
            if mid in self.fail_get_msg_ids:
                self.fail_get_msg_ids.discard(mid)
                raise ActionFailed(
                    retcode=100, message="注入的查询失败", wording=""
                )
            if mid in self._messages:
                stored = self._messages[mid]
                if isinstance(stored, Message):
                    segments = []
                    for seg in stored:
                        if seg.type == "rps" and not seg.data.get("result"):
                            seg = MessageSegment(
                                "rps", {**seg.data, "result": self.bot_rps_gesture}
                            )
                        segments.append(seg)
                    stored = Message(segments)
                return {"message_id": mid, "message": stored}
            if mid in self.reply_targets:
                return self.reply_targets[mid]
            return {"message_id": mid, "message": []}

        if api == "get_group_member_info":
            uid = int(data.get("user_id", 0))
            gid = data.get("group_id")
            if str(uid) == SELF_ID:
                return {
                    "user_id": uid,
                    "group_id": gid,
                    "nickname": BOT_NICKNAME,
                    "card": self.bot_cards.get(gid, ""),
                }
            return {
                "user_id": uid,
                "group_id": gid,
                "nickname": f"成员{uid}",
                "card": self.member_cards.get(uid, ""),
            }

        if api == "get_login_info":
            return {"user_id": int(SELF_ID), "nickname": BOT_NICKNAME}

        if api == "get_group_list":
            return [{"group_id": gid, "group_name": f"群{gid}"} for gid in self.group_list]

        if api == "set_group_card":
            self.set_cards.append(
                {
                    "t": t,
                    "group_id": data.get("group_id"),
                    "user_id": data.get("user_id"),
                    "card": data.get("card"),
                }
            )
            if str(data.get("user_id")) == SELF_ID:
                self.bot_cards[data.get("group_id")] = data.get("card") or ""
            return {}

        if api == "set_msg_emoji_like":
            self.emoji_likes.append({"t": t, **data})
            return {}

        return {}


gateway = FakeGateway()
adapter._call_api = gateway.call  # pyright: ignore[reportAttributeAccessIssue]

# =====================================================================
# 真实加载全部本地插件
# =====================================================================
loaded = nonebot.load_plugins("src/plugins")
nonebot.load_builtin_plugins("echo", "single_session")
PLUGINS = {p.name: p for p in loaded}
M_SRL = PLUGINS["send_rate_limit"].module
M_RNG = PLUGINS["real_name_group"].module
M_DUEL = PLUGINS["duel"].module
M_NG = PLUGINS["nickname_guard"].module

BUSY_MESSAGE: str = M_SRL._busy_message  # pyright: ignore[reportPrivateUsage]
MAX_PER_MINUTE: int = M_SRL._max_per_minute  # pyright: ignore[reportPrivateUsage]
MIN_INTERVAL: float = M_SRL._min_interval  # pyright: ignore[reportPrivateUsage]
REAL_GROUP: int = M_RNG.group_ids[0]  # pyright: ignore[reportPrivateUsage]
SUPERUSER = next(iter(bot.config.superusers))

# =====================================================================
# 报告与断言
# =====================================================================
CHECKS: list[tuple[str, str, bool, str]] = []
SCENE_T0 = {"t": now(), "sends": 0}


def check(scene: str, name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((scene, name, bool(ok), detail))
    mark = "PASS" if ok else "FAIL"
    logger.info(f"[CHECK][{mark}] {scene} | {name} | {detail}")


def scene_begin(name: str) -> None:
    SCENE_T0["t"] = now()
    SCENE_T0["sends"] = len(gateway.sends)
    logger.info(f"===== 场景开始: {name} =====")


def scene_sends() -> list[dict[str, Any]]:
    return gateway.sends[SCENE_T0["sends"] :]


# =====================================================================
# 事件构造
# =====================================================================
class Payloads:
    def __init__(self) -> None:
        self._mid = 10_000

    def next_mid(self) -> int:
        self._mid += 1
        return self._mid


P = Payloads()


def seg_text(text: str) -> dict[str, Any]:
    return {"type": "text", "data": {"text": text}}


def seg_at(qq: int | str) -> dict[str, Any]:
    return {"type": "at", "data": {"qq": str(qq)}}


def seg_rps(result: int) -> dict[str, Any]:
    return {"type": "rps", "data": {"result": result}}


def seg_face(face_id: int) -> dict[str, Any]:
    return {"type": "face", "data": {"id": str(face_id)}}


def seg_reply(mid: int) -> dict[str, Any]:
    return {"type": "reply", "data": {"id": str(mid)}}


def has_seg(rec: dict[str, Any], seg_type: str) -> bool:
    """判断一次已发送消息中是否包含指定类型的消息段。"""
    message = rec["data"].get("message")
    return isinstance(message, Message) and any(seg.type == seg_type for seg in message)


def group_msg(
    group_id: int,
    user_id: int,
    segments: list[dict[str, Any]],
    *,
    card: str | None = None,
    nickname: str | None = None,
    role: str = "member",
) -> dict[str, Any]:
    return {
        "time": int(time.time()),
        "self_id": int(SELF_ID),
        "post_type": "message",
        "message_type": "group",
        "sub_type": "normal",
        "message_id": P.next_mid(),
        "group_id": group_id,
        "user_id": user_id,
        "message": segments,
        "raw_message": "",
        "font": 0,
        "sender": {
            "user_id": user_id,
            "nickname": nickname or f"用户{user_id}",
            "card": card,
            "role": role,
        },
    }


def private_msg(
    user_id: int,
    segments: list[dict[str, Any]],
    *,
    nickname: str | None = None,
) -> dict[str, Any]:
    return {
        "time": int(time.time()),
        "self_id": int(SELF_ID),
        "post_type": "message",
        "message_type": "private",
        "sub_type": "friend",
        "message_id": P.next_mid(),
        "user_id": user_id,
        "message": segments,
        "raw_message": "",
        "font": 0,
        "sender": {"user_id": user_id, "nickname": nickname or f"用户{user_id}"},
    }


def group_card_notice(group_id: int, card_new: str, card_old: str = "") -> dict[str, Any]:
    return {
        "time": int(time.time()),
        "self_id": int(SELF_ID),
        "post_type": "notice",
        "notice_type": "group_card",
        "user_id": int(SELF_ID),
        "group_id": group_id,
        "card_new": card_new,
        "card_old": card_old,
    }


def lifecycle_connect() -> dict[str, Any]:
    return {
        "time": int(time.time()),
        "self_id": int(SELF_ID),
        "post_type": "meta_event",
        "meta_event_type": "lifecycle",
        "sub_type": "connect",
    }


# =====================================================================
# 分发与等待
# =====================================================================
KEEP_ALIVE: list[Any] = []  # 持有事件引用，避免 id() 复用影响 default_reply 判定


async def dispatch(payloads: list[dict[str, Any]], timeout: float = 240.0) -> list[Any]:
    """并发分发事件（每个事件一个任务，与真实框架一致），返回异常列表。"""
    tasks = []
    for payload in payloads:
        event = OneBotAdapter.json_to_event(payload)
        if event is None:
            logger.error(f"[stress] 事件解析失败: {payload}")
            continue
        KEEP_ALIVE.append(event)
        tasks.append(asyncio.create_task(bot.handle_event(event)))
    try:
        results = await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True), timeout
        )
    except asyncio.TimeoutError:
        logger.error(f"[stress] 事件分发超时（{timeout}s）")
        return [asyncio.TimeoutError("dispatch timeout")]
    return [r for r in results if isinstance(r, BaseException)]


def normal_in_window() -> int:
    n = now()
    return sum(
        1
        for e in M_SRL._send_schedule  # pyright: ignore[reportPrivateUsage]
        if not e.is_busy and e.at > n - 60.0
    )


async def wait_for_capacity(need: int, timeout: float = 150.0) -> bool:
    """等待发送窗口可以容纳 need 条新普通消息（不含已在窗口内的）。"""
    deadline = now() + timeout
    while now() < deadline:
        if normal_in_window() <= MAX_PER_MINUTE - need:
            return True
        await asyncio.sleep(0.5)
    return False


async def wait_all_idle(timeout: float = 180.0) -> bool:
    """等待所有已排定的发送完成（排定表中不再有未来排定项）。"""
    deadline = now() + timeout
    while now() < deadline:
        if not any(
            entry.at > now()
            for entry in M_SRL._send_schedule  # pyright: ignore[reportPrivateUsage]
        ):
            return True
        await asyncio.sleep(0.3)
    return False


def window_compliance(sends: list[dict[str, Any]]) -> dict[str, Any]:
    """检查发送间隔与滑动窗口合规性(窗口比较留 0.1s 抖动容差)。"""
    normal = [s for s in sends if s["text"] != BUSY_MESSAGE]
    gaps = [b["t"] - a["t"] for a, b in zip(sends, sends[1:])]
    min_gap = min(gaps) if gaps else None
    worst = 0
    worst_at = None
    for i, s in enumerate(normal):
        count = sum(1 for o in normal[: i + 1] if o["t"] > s["t"] - 59.9)
        if count > worst:
            worst = count
            worst_at = s["t"] - T0
    return {
        "total": len(sends),
        "normal": len(normal),
        "busy": len(sends) - len(normal),
        "min_gap": min_gap,
        "worst_window_count": worst,
        "worst_window_at": worst_at,
    }


# =====================================================================
# 错误日志收集
# =====================================================================
ERROR_LOGS: list[str] = []
WARNING_LOGS: list[str] = []
IGNORED_LOGS: list[str] = []  # 被 single_session 去重（"is ignored"）的事件日志


def _log_sink(message: Any) -> None:  # noqa: ANN401
    record = message.record
    level = record["level"].no
    line = f"[{record['level'].name}] {record['name']} | {record['message']}"
    if level >= 40:
        ERROR_LOGS.append(line)
    elif level == 30:
        WARNING_LOGS.append(line)
    if (
        level == 20
        and record["name"] != "__main__"
        and "ignored" in record["message"]
    ):
        IGNORED_LOGS.append(record["message"])


logger.add(_log_sink, level="INFO")


def errors_since(marker: int) -> list[str]:
    return ERROR_LOGS[marker:]


# =====================================================================
# 场景
# =====================================================================
async def scene_s0_selfcheck() -> None:
    scene_begin("S0 插件加载与基础响应自检")
    expected = {
        "default_reply",
        "duel",
        "help",
        "nickname_guard",
        "random_face",
        "react",
        "real_name_group",
        "send_rate_limit",
    }
    check("S0", "8 个本地插件全部加载", set(PLUGINS) == expected, f"实际: {sorted(PLUGINS)}")
    check("S0", "机器人已连接（driver.bots）", list(nonebot.get_bots()) == [SELF_ID])
    check(
        "S0",
        "真实配置生效（限流 9/分、1/秒）",
        MAX_PER_MINUTE == 9 and MIN_INTERVAL == 1.0,
        f"max_per_minute={MAX_PER_MINUTE}, min_interval={MIN_INTERVAL}",
    )

    await dispatch([group_msg(900002, 20001, [seg_text("/help")])])
    sends = gateway.sends
    check("S0", "/help 命令得到回复", len(sends) == 1 and "用户手册" in sends[0]["text"])
    await wait_all_idle()


async def scene_s1_threshold() -> None:
    scene_begin("S1 消息阈值打满：阻断传播 + 忙提示节流")
    err_mark = len(ERROR_LOGS)
    await wait_for_capacity(9)

    # ---- 第一波：9 个会话的 @机器人消息，恰好打满每分钟限额 ----
    first = [
        group_msg(910000 + i, 22000 + i, [seg_at(SELF_ID), seg_text(" 你好")])
        for i in range(1, 10)
    ]
    errors = await dispatch(first)
    wave1 = scene_sends()
    check("S1", "第一波 9 条全部得到默认回复", len(wave1) == 9, f"实际 {len(wave1)}")
    check("S1", "第一波回复均为默认回复文案", all("不知道怎么使用" in s["text"] for s in wave1))
    check("S1", "第一波事件无异常", not errors, str(errors[:2]))

    # ---- 第二波：8 个新会话各 5 条普通消息（40 事件并发），额度已满 ----
    second = [
        group_msg(920000 + g, 23000 + g, [seg_text("随便聊聊")])
        for g in range(1, 9)
        for _ in range(5)
    ]
    mark2 = len(gateway.sends)
    ign2 = len(IGNORED_LOGS)
    errors2 = await dispatch(second)
    wave2 = gateway.sends[mark2:]
    busy2 = [s for s in wave2 if s["text"] == BUSY_MESSAGE]
    busy2_sessions = [gateway.session_of(s) for s in busy2]
    check("S1", "第二波 40 事件全部被阻断（无其他发送）", all(s["text"] == BUSY_MESSAGE for s in wave2), f"发送 {len(wave2)} 条")
    check("S1", "第二波忙提示每会话恰好 1 条", len(busy2) == 8 and len(set(busy2_sessions)) == 8, f"busy={len(busy2)} 会话={sorted(set(busy2_sessions))}")
    check("S1", "第二波同会话重复消息被 single_session 去重 32 条", len(IGNORED_LOGS) - ign2 == 32, f"ignored={len(IGNORED_LOGS) - ign2}")
    check("S1", "第二波事件无异常", not errors2, str(errors2[:2]))

    # ---- 第三波：已忙碌会话重复消息 + 1 个新会话 ----
    third = [
        group_msg(920001, 23001, [seg_text("再聊")]),
        group_msg(920001, 23002, [seg_text("再聊2")]),
        group_msg(930001, 23100, [seg_text("新会话")]),
    ]
    mark3 = len(gateway.sends)
    errors3 = await dispatch(third)
    wave3 = gateway.sends[mark3:]
    busy3 = [s for s in wave3 if s["text"] == BUSY_MESSAGE]
    check(
        "S1",
        "重复会话不再发忙提示，仅新会话收到 1 条",
        len(busy3) == 1 and gateway.session_of(busy3[0]) == "group_930001",
        f"busy={[(gateway.session_of(s), s['text'][:10]) for s in busy3]}",
    )
    check("S1", "第三波事件无异常", not errors3, str(errors3[:2]))

    await wait_all_idle()
    stats = window_compliance(scene_sends())
    check(
        "S1",
        "本场景发送间隔与限流合规",
        (stats["min_gap"] is None or stats["min_gap"] >= MIN_INTERVAL - 0.1)
        and stats["worst_window_count"] <= MAX_PER_MINUTE,
        f"min_gap={stats['min_gap']:.3f}s worst_window={stats['worst_window_count']} total={stats['total']} busy={stats['busy']}",
    )
    check("S1", "场景期间无 ERROR 日志", not errors_since(err_mark), str(errors_since(err_mark)[:2]))


async def scene_s2_recover() -> None:
    scene_begin("S2 滑动窗口恢复")
    await wait_for_capacity(9)
    err_mark = len(ERROR_LOGS)
    await dispatch(
        [
            group_msg(940001, 24000, [seg_at(SELF_ID), seg_text(" 你好")]),
            private_msg(23001, [seg_text("在吗")]),
        ]
    )
    sends = scene_sends()
    busy = [s for s in sends if s["text"] == BUSY_MESSAGE]
    check("S2", "窗口滚动后新事件正常处理（2 条回复）", len(sends) == 2, f"实际 {len(sends)}: {[s['text'][:12] for s in sends]}")
    check("S2", "恢复后无忙提示", not busy, f"busy={len(busy)}")
    check("S2", "场景期间无 ERROR 日志", not errors_since(err_mark), str(errors_since(err_mark)[:2]))


async def scene_s3_mixed_blast() -> None:
    scene_begin("S3 全插件混合高频并发突发")
    err_mark = len(ERROR_LOGS)
    await wait_for_capacity(9)

    cards = {
        24001: None,  # 空名片 -> not_set
        24002: "24-信科",  # 缺段
        24003: "24-信科-张三-京-多",  # 多余分段
        24004: "X-信科-张三-京",  # 年级不符
        24005: "24--张三-京",  # 院系为空
        24006: "24-信科-Ab-京",  # 姓名不符
        24007: "24-信科-张三-X",  # 生源地不符
        24008: "26-元培-张三-鄂",  # 合规（对照）
    }
    events: list[dict[str, Any]] = []
    for uid, card in cards.items():
        events.append(group_msg(REAL_GROUP, uid, [seg_text("签到")], card=card, nickname=f"昵称{uid}"))
    # 合规成员 @机器人 / 超管 /exempt
    events.append(group_msg(REAL_GROUP, 24101, [seg_at(SELF_ID), seg_text(" 你好")], card="26-元培-张三-京"))
    events.append(group_msg(REAL_GROUP, 24102, [seg_at(SELF_ID), seg_text(" 在")], card="26-元培-李四-鄂"))
    events.append(
        group_msg(
            REAL_GROUP,
            int(SUPERUSER),
            [seg_text("/exempt "), seg_at(24009), seg_text(" 测试理由")],
            card="26-元培-张三-京",
            role="admin",
        )
    )
    # 普通群各命令 / 消息
    g2 = 900002
    react_plain = group_msg(g2, 25005, [seg_text("/react 😄")], card="普通用户")
    react_reply = group_msg(
        g2, 25007, [seg_reply(12345), seg_text("/react 😄")], card="普通用户"
    )
    # 引用消息（id=12345）提供完整 Reply 记录：适配器 _check_reply 会通过 get_msg
    # 拉取引用信息，成功解析后会删除事件消息中的 reply 段；随后 react 插件会重新
    # get_msg 触发消息本身再次解析引用（模拟协议端原始消息保留引用段的真实行为）
    gateway.reply_targets[12345] = {
        "time": int(time.time()),
        "message_type": "group",
        "message_id": 12345,
        "real_id": 12345,
        "sender": {"user_id": 20099, "nickname": "引用者", "role": "member"},
        "message": [seg_text("被引用的消息")],
    }
    gateway.reply_targets[react_reply["message_id"]] = {
        "message_id": react_reply["message_id"],
        "message": [seg_reply(12345), seg_text("/react 😄")],
    }
    events += [
        group_msg(g2, 25001, [seg_text("/help")], card="普通用户"),
        group_msg(g2, 25002, [seg_text("/rf")], card="普通用户"),
        group_msg(g2, 25003, [seg_text("/rf large")], card="普通用户"),
        group_msg(g2, 25004, [seg_text("/rf 乱参数")], card="普通用户"),
        react_plain,
        group_msg(g2, 25006, [seg_text("/react")], card="普通用户"),
        react_reply,
        group_msg(g2, 25008, [seg_text("/duel.rank")], card="普通用户"),
        group_msg(g2, 25009, [seg_text("/duel")], card="普通用户"),
        group_msg(g2, 25010, [seg_text("/duel "), seg_at(25020)], card="普通用户"),
        group_msg(g2, 25011, [seg_rps(2)], card="普通用户"),
        group_msg(g2, 25012, [seg_rps(3)], card="普通用户"),
        group_msg(g2, 25013, [seg_at(SELF_ID), seg_text(" 你好")], card="普通用户"),
        group_msg(g2, 25014, [seg_at(SELF_ID), seg_text(" 在吗")], card="普通用户"),
    ]
    # 私聊
    events += [
        private_msg(23002, [seg_text("/help")]),
        private_msg(23003, [seg_text("在吗")]),
    ]
    # notice 与 lifecycle
    events.append(group_card_notice(960001, "恶意群名片"))
    events.append(lifecycle_connect())

    errors = await dispatch(events)
    await wait_all_idle()

    sends = scene_sends()
    texts = [s["text"] for s in sends]
    reminders = [s for s in sends if s["text"].startswith("@")]
    busy = [s for s in sends if s["text"] == BUSY_MESSAGE]
    check("S3", "并发事件无异常", not errors, str(errors[:2]))
    check(
        "S3",
        "实名群提醒恰好命中群窗口上限 5 条",
        len(reminders) == 5,
        f"实际 {len(reminders)} 条: {[t[:14] for t in [s['text'] for s in reminders]]}",
    )
    # 伪@提醒的目标为群内显示名：群名片为空时为 QQ 昵称，其余为群名片
    invalid_names = {
        "昵称24001",
        "24-信科",
        "24-信科-张三-京-多",
        "X-信科-张三-京",
        "24--张三-京",
        "24-信科-Ab-京",
        "24-信科-张三-X",
    }
    compliant_names = {"26-元培-张三-鄂", "26-元培-张三-京", "26-元培-李四-鄂"}
    reminder_targets = {s["text"][1:].split(" ", 1)[0] for s in reminders}
    check(
        "S3",
        "提醒目标均为不合规成员且不含合规对照成员",
        len(reminder_targets) == len(reminders)
        and reminder_targets <= invalid_names
        and not (reminder_targets & compliant_names),
        f"targets={sorted(reminder_targets)}",
    )
    default_replies = sum("不知道怎么使用" in t for t in texts)
    check("S3", "默认回复（4 条 @机器人 + 1 条私聊）", default_replies == 5, f"实际 {default_replies}")
    check("S3", "/help 回复 2 条（群+私聊）", sum("用户手册" in t for t in texts) == 2)
    face_sends = sum(has_seg(s, "face") for s in sends)
    check(
        "S3",
        "/rf 表情 2 条 + 参数错误 1 条",
        face_sends == 2 and sum("无法识别的参数" in t for t in texts) == 1,
        f"face={face_sends}",
    )
    check("S3", "/duel.rank 回复 1 条", sum("决斗积分" in t for t in texts) == 1)
    check(
        "S3",
        "/duel 用法与发起决斗回复",
        sum("请 @ 要发起决斗" in t for t in texts) == 1
        and sum("发起决斗" in t and "向" in t for t in texts) == 1,
    )
    check("S3", "/exempt 回复 1 条", sum("添加到免验证名单" in t for t in texts) == 1)
    check("S3", "忙碌提示为 0（未触及限额）", not busy, f"busy={len(busy)}")
    emoji_targets = sorted(
        int(e["message_id"])
        for e in gateway.emoji_likes
        if e.get("message_id") is not None
    )
    check(
        "S3",
        "/react 贴表情生效（普通与引用消息各 1 条）",
        emoji_targets == sorted([int(react_plain["message_id"]), 12345]),
        f"emoji_targets={emoji_targets}",
    )
    check("S3", "昵称保护已恢复通知改名的群名片", len(gateway.set_cards) >= 1)
    exempt_file = _TMP_DATA_DIR / "real_name_group" / "exemptions.json"
    loaded_exempt = False
    if exempt_file.exists():
        data = json.loads(exempt_file.read_text(encoding="utf-8"))
        loaded_exempt = any("24009" in members for members in data.values())
    check("S3", "免验证名单已写入本地存储", loaded_exempt)
    stats = window_compliance(sends)
    check(
        "S3",
        "突发期间发送间隔与限流合规",
        (stats["min_gap"] is None or stats["min_gap"] >= MIN_INTERVAL - 0.1)
        and stats["worst_window_count"] <= MAX_PER_MINUTE,
        f"min_gap={stats['min_gap']:.3f}s worst_window={stats['worst_window_count']} total={stats['total']} busy={stats['busy']}",
    )
    check("S3", "场景期间无 ERROR 日志", not errors_since(err_mark), str(errors_since(err_mark)[:2]))


async def scene_s4_nickname_guard() -> None:
    scene_start = len(gateway.set_cards)
    scene_begin("S4 昵称保护并发（无发送，不占限流窗口）")
    err_mark = len(ERROR_LOGS)

    for gid in range(950001, 950006):
        gateway.bot_cards[gid] = "被篡改的昵称"
    gateway.group_list = list(range(950001, 950006))

    events = [group_card_notice(960000 + i, "恶意群名片") for i in range(1, 7)]
    events += [lifecycle_connect(), lifecycle_connect()]
    tasks = [asyncio.create_task(dispatch(events))]
    tasks.append(asyncio.create_task(M_NG._check_all_group_cards()))  # pyright: ignore[reportPrivateUsage]
    tasks.append(asyncio.create_task(M_NG._check_all_group_cards()))  # pyright: ignore[reportPrivateUsage]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    errors = [r for r in results if isinstance(r, BaseException) or (isinstance(r, list) and r)]

    set_calls = gateway.set_cards[scene_start:]
    notice_groups = {960000 + i for i in range(1, 7)}
    notice_sets = [c for c in set_calls if c["group_id"] in notice_groups]
    life_sets = [c for c in set_calls if c["group_id"] in set(range(950001, 950006))]
    check("S4", "并发无异常", not errors, str(errors[:2]))
    check("S4", "群名片通知全部触发恢复（6 群）", len(notice_sets) == 6, f"实际 {len(notice_sets)}")
    check("S4", "lifecycle/定时任务恢复全部 5 群", len(life_sets) >= 5, f"实际 {len(life_sets)}")
    check(
        "S4",
        "恢复后机器人各群名片与昵称一致",
        all(gateway.bot_cards.get(g) == BOT_NICKNAME for g in range(950001, 950006)),
        f"{ {g: gateway.bot_cards.get(g) for g in range(950001, 950006)} }",
    )
    check("S4", "场景期间无 ERROR 日志", not errors_since(err_mark), str(errors_since(err_mark)[:2]))


async def scene_s5_duel() -> None:
    scene_begin("S5 决斗并发一致性")
    err_mark = len(ERROR_LOGS)
    # 每个子场景开始前等待发送窗口留出本轮发送量，避免事件被限额门阻断
    await wait_for_capacity(3)

    gid = 970001
    a, b, c, d = 21001, 21002, 21003, 21004
    card_a = "26-元培-张三-京"
    for uid, card in ((a, card_a), (b, "26-元培-李四-鄂"), (c, "26-元培-王五-沪"), (d, "26-元培-赵六-粤")):
        gateway.member_cards[uid] = card

    # ---- A: 并发发起 3 次决斗 -> single_session 去重后仅 1 次到达插件 ----
    ign_a = len(IGNORED_LOGS)
    duel_cmd = [seg_text("/duel "), seg_at(b)]
    await dispatch([group_msg(gid, a, duel_cmd, card=card_a) for _ in range(3)])
    s1 = scene_sends()
    created = [s for s in s1 if "发起决斗" in s["text"] and "向" in s["text"]]
    check("S5", "并发发起仅创建 1 场决斗", len(created) == 1, f"created={len(created)}")
    check("S5", "并发发起其余 2 条被 single_session 去重", len(IGNORED_LOGS) - ign_a == 2, f"ignored={len(IGNORED_LOGS) - ign_a}")

    # ---- A2: 串行重复发起同一对手 -> 插件守卫拦截重复决斗 ----
    await wait_for_capacity(1)
    mark = len(gateway.sends)
    await dispatch([group_msg(gid, a, duel_cmd, card=card_a)])
    s1b = gateway.sends[mark:]
    pair_active = [s for s in s1b if "已有一场进行中的决斗" in s["text"]]
    check("S5", "串行重复发起：插件守卫回复已有一场决斗", len(pair_active) == 1, f"实际 {len(pair_active)}")

    # ---- B: B 并发接受 3 次 -> single_session 去重后仅 1 次到达插件 ----
    await wait_for_capacity(3)
    mark = len(gateway.sends)
    ign_b = len(IGNORED_LOGS)
    accept = [seg_text("/duel.accept "), seg_at(a)]
    await dispatch([group_msg(gid, b, accept, card="26-元培-李四-鄂") for _ in range(3)])
    s2 = gateway.sends[mark:]
    accepted = [s for s in s2 if "已接受" in s["text"]]
    check("S5", "并发接受仅生效 1 次", len(accepted) == 1, f"accepted={len(accepted)}")
    check("S5", "并发接受其余 2 条被 single_session 去重", len(IGNORED_LOGS) - ign_b == 2, f"ignored={len(IGNORED_LOGS) - ign_b}")

    # ---- B2: 串行重复接受 -> 插件回复对方未发起决斗 ----
    await wait_for_capacity(1)
    mark = len(gateway.sends)
    await dispatch([group_msg(gid, b, accept, card="26-元培-李四-鄂")])
    s2b = gateway.sends[mark:]
    not_challenged = [s for s in s2b if "未向你发起决斗" in s["text"]]
    check("S5", "串行重复接受：插件回复对方未发起决斗", len(not_challenged) == 1, f"实际 {len(not_challenged)}")

    # ---- C: 双方各并发 3 条猜拳表情 -> 仅结算 1 次（平局）----
    await wait_for_capacity(1)
    mark = len(gateway.sends)
    gestures = [
        group_msg(gid, a, [seg_rps(1)], card=card_a),
        group_msg(gid, a, [seg_rps(1)], card=card_a),
        group_msg(gid, a, [seg_rps(1)], card=card_a),
        group_msg(gid, b, [seg_rps(1)], card="26-元培-李四-鄂"),
        group_msg(gid, b, [seg_rps(1)], card="26-元培-李四-鄂"),
        group_msg(gid, b, [seg_rps(1)], card="26-元培-李四-鄂"),
    ]
    await dispatch(gestures)
    s3 = gateway.sends[mark:]
    draws = [s for s in s3 if "平局" in s["text"]]
    wins = [s for s in s3 if "获胜" in s["text"]]
    check("S5", "并发猜拳仅结算 1 次（平局）", len(draws) == 1 and not wins, f"draws={len(draws)} wins={len(wins)}")

    # ---- D: 向机器人发起决斗 -> 自动接受 + 机器人回查手势 + 判定胜负 ----
    await wait_for_capacity(2)
    mark = len(gateway.sends)
    gateway.bot_rps_gesture = 3  # 布
    await dispatch([group_msg(gid, a, [seg_text("/duel "), seg_at(SELF_ID)], card=card_a)])
    await wait_all_idle()
    s4 = gateway.sends[mark:]
    auto_accepted = [s for s in s4 if "已接受" in s["text"] and BOT_NICKNAME in s["text"]]
    bot_rps = [s for s in s4 if has_seg(s, "rps")]
    await wait_for_capacity(1)
    mark = len(gateway.sends)
    await dispatch([group_msg(gid, a, [seg_rps(1)], card=card_a)])
    await wait_all_idle()
    s5 = gateway.sends[mark:]
    win_msgs = [s for s in s5 if "获胜" in s["text"]]
    check("S5", "向机器人发起决斗被自动接受", len(auto_accepted) == 1, f"auto_accepted={len(auto_accepted)}")
    check("S5", "机器人发送猜拳表情", len(bot_rps) == 1, f"bot_rps={len(bot_rps)}")
    check(
        "S5",
        "机器人查看手势后正确判定胜负（1 剪刀 vs 3 布 -> 挑战者胜）",
        len(win_msgs) == 1 and card_a in win_msgs[0]["text"],
        f"win={[s['text'][:40] for s in win_msgs]}",
    )

    # ---- E: 并发拒绝（1 条被去重）+ 串行重复拒绝（插件守卫）----
    await wait_for_capacity(3)
    mark = len(gateway.sends)
    await dispatch([group_msg(gid, c, [seg_text("/duel "), seg_at(d)], card="26-元培-王五-沪")])
    ign_e = len(IGNORED_LOGS)
    reject_cmd = [seg_text("/duel.reject "), seg_at(c)]
    await dispatch(
        [group_msg(gid, d, reject_cmd, card="26-元培-赵六-粤") for _ in range(2)]
    )
    await dispatch([group_msg(gid, d, reject_cmd, card="26-元培-赵六-粤")])
    s6 = gateway.sends[mark:]
    rejected = [s for s in s6 if "已拒绝" in s["text"]]
    nc2 = [s for s in s6 if "未向你发起决斗" in s["text"]]
    check("S5", "并发拒绝仅生效 1 次", len(rejected) == 1, f"rejected={len(rejected)}")
    check("S5", "并发拒绝 1 条被去重、串行重复拒绝被守卫拦截", len(IGNORED_LOGS) - ign_e == 1 and len(nc2) == 1, f"ignored={len(IGNORED_LOGS) - ign_e} nc={len(nc2)}")

    # ---- F: 排行榜与积分文件 ----
    await wait_for_capacity(1)
    mark = len(gateway.sends)
    await dispatch([group_msg(gid, a, [seg_text("/duel.rank")], card=card_a)])
    await wait_all_idle()
    s7 = gateway.sends[mark:]
    rank = [s for s in s7 if "决斗积分" in s["text"]]
    check("S5", "排行榜可用且包含胜者积分", len(rank) == 1 and card_a in rank[0]["text"] and "1 分" in rank[0]["text"], f"{rank[0]['text'][:60] if rank else ''}")
    score_file = _TMP_DATA_DIR / "duel" / "scores.json"
    scores_ok = False
    if score_file.exists():
        data = json.loads(score_file.read_text(encoding="utf-8"))
        group_scores = data.get(str(gid), {})
        a_rec = group_scores.get(str(a), {})
        bot_rec = group_scores.get(SELF_ID, {})
        scores_ok = a_rec.get("score") == 1 and bot_rec.get("score") == -1
    check("S5", "积分数据正确（胜者 +1、机器人 -1）", scores_ok)

    stats = window_compliance(scene_sends())
    check(
        "S5",
        "决斗场景发送间隔与限流合规",
        (stats["min_gap"] is None or stats["min_gap"] >= MIN_INTERVAL - 0.1)
        and stats["worst_window_count"] <= MAX_PER_MINUTE,
        f"min_gap={stats['min_gap']:.3f}s worst_window={stats['worst_window_count']} total={stats['total']}",
    )
    check("S5", "场景期间无 ERROR 日志", not errors_since(err_mark), str(errors_since(err_mark)[:2]))


async def scene_s6_realname_and_errors() -> None:
    scene_begin("S6 实名群成员冷却 + 错误注入")
    err_mark = len(ERROR_LOGS)
    emoji_mark = len(gateway.emoji_likes)
    ign_mark = len(IGNORED_LOGS)
    # 容量预留：1 条昵称提醒 + 2 条注入失败的默认回复
    await wait_for_capacity(3)

    # ---- 同一成员串行连发 10 条消息 -> 成员冷却（600s）内仅 1 条提醒 ----
    # 串行分发保证 10 条都真实到达插件，后续 9 条由插件成员冷却拦截，
    # 而非被框架 single_session 去重（避免假阳性）
    for _ in range(10):
        await dispatch(
            [
                group_msg(
                    REAL_GROUP,
                    24050,
                    [seg_text("冒泡")],
                    card="24-信科",
                    nickname="昵称24050",
                )
            ]
        )
    sends = scene_sends()
    remind = [s for s in sends if s["text"].startswith("@24-信科")]
    check("S6", "成员冷却生效：同成员连发 10 条仅 1 条提醒", len(remind) == 1, f"实际 {len(remind)}")
    check(
        "S6",
        "冷却拦截由插件完成（串行分发期间无事件被框架忽略）",
        len(IGNORED_LOGS) - ign_mark == 0,
        f"ignored={len(IGNORED_LOGS) - ign_mark}",
    )

    # ---- 错误注入①：910099 会话发送持续失败（默认回复路径）----
    gateway.fail_send_sessions.add("group_910099")
    events: list[dict[str, Any]] = [
        group_msg(910099, 26001, [seg_at(SELF_ID), seg_text(" 你好")]),
        group_msg(910099, 26002, [seg_at(SELF_ID), seg_text(" 在吗")]),
    ]
    # ---- 错误注入②：get_msg 失败（react 解析引用失败后回退到原消息自身）----
    react_evt = group_msg(900002, 26003, [seg_text("/react 😄")])
    gateway.fail_get_msg_ids.add(react_evt["message_id"])
    events.append(react_evt)

    errors = await dispatch(events)
    await wait_all_idle()
    sends = scene_sends()
    check("S6", "并发事件无异常传播", not errors, str(errors[:2]))
    failed = [f for f in gateway.send_failures if f["data"].get("group_id") == 910099]
    ok_sends = [s for s in sends if s["group_id"] == 910099]
    check(
        "S6",
        "发送失败会话：2 次注入失败且无成功发送记录",
        len(failed) == 2 and not ok_sends,
        f"failed={len(failed)} ok={len(ok_sends)}",
    )
    new_likes = [
        int(e["message_id"])
        for e in gateway.emoji_likes[emoji_mark:]
        if e.get("message_id") is not None
    ]
    check(
        "S6",
        "get_msg 失败后 /react 回退贴到原消息",
        new_likes == [int(react_evt["message_id"])],
        f"new_likes={new_likes}",
    )
    check("S6", "错误路径未产生 ERROR 日志", not errors_since(err_mark), str(errors_since(err_mark)[:3]))


# =====================================================================
# 主流程
# =====================================================================
async def main() -> None:
    logger.info(f"[stress] 测试开始 T0={T0:.3f} 报告文件={REPORT_PATH}")
    await scene_s0_selfcheck()
    await scene_s1_threshold()
    await scene_s2_recover()
    await scene_s3_mixed_blast()
    await scene_s4_nickname_guard()
    await scene_s5_duel()
    await scene_s6_realname_and_errors()

    # ---- 汇总 ----
    total = len(CHECKS)
    failed = [c for c in CHECKS if not c[2]]
    logger.info("========== 汇总 ==========")
    logger.info(f"检查项: {total} 项，通过 {total - len(failed)}，失败 {len(failed)}")
    for scene, name, ok, detail in CHECKS:
        logger.info(f"  [{'PASS' if ok else 'FAIL'}] {scene} | {name}" + (f" | {detail}" if detail else ""))
    logger.info("========== 发送统计 ==========")
    stats = window_compliance(gateway.sends)
    logger.info(f"总会话发送: {stats['total']}（普通 {stats['normal']}，忙提示 {stats['busy']}）")
    logger.info(f"最小发送间隔: {stats['min_gap']!r}s，最大 59.9s 窗口内普通消息数: {stats['worst_window_count']}（上限 {MAX_PER_MINUTE}）")
    logger.info(f"ERROR 日志: {len(ERROR_LOGS)} 条；WARNING 日志: {len(WARNING_LOGS)} 条")
    logger.info(f"被 single_session 去重（忽略）的事件: {len(IGNORED_LOGS)} 条")
    if ERROR_LOGS:
        for line in ERROR_LOGS[:20]:
            logger.info(f"  ERROR: {line}")
    logger.info(f"[stress] 测试结束，耗时 {now() - T0:.1f}s，失败检查项 {len(failed)}")
    sys.exit(0 if not failed else 2)


if __name__ == "__main__":
    asyncio.run(main())
