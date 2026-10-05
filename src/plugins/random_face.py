"""随机发送表情的插件。

用法：/rf（或 /randomface）[large|l] [emoji|e]
"""

import random
import unicodedata

from nonebot import on_command
from nonebot.adapters.onebot.v11 import Message, MessageSegment
from nonebot.params import CommandArg
from nonebot.plugin import PluginMetadata

__plugin_meta__ = PluginMetadata(
    name="随机表情",
    description="随机发送 QQ 小黄脸、大黄脸（超级表情）或 emoji",
    usage=(
        "/rf：随机发送一个小黄脸\n"
        "/rf large（l）：随机发送一个大黄脸（超级表情）\n"
        "/rf emoji（e）：随机发送一个 emoji"
    ),
    type="application",
    supported_adapters={"~onebot.v11"},
)

# 小黄脸表情 ID：经典静态表情，单独发送时渲染为小尺寸
_SMALL_FACE_ID_TEXT = (
    "0 1 2 3 4 6 7 8 9 10 11 12 13 14 15 16 18 19 "
    "20 21 22 23 24 25 26 27 28 29 30 31 32 33 34 35 36 37 "
    "38 39 41 42 43 46 49 56 59 60 63 64 66 67 76 77 78 79 "
    "85 86 89 96 97 98 99 100 101 102 103 104 105 106 107 108 109 110 "
    "111 112 116 118 119 120 121 123 124 125 129 144 146 147 169 171 172 173 "
    "174 175 176 177 178 179 182 183 185 187 201 212 264 265 267 268 277 282 "
    "283 289 297 332 336 352 355 451 456 457 461 462 464 467 468 469 472 474 "
    "475 476 477 478 479 480 481 482 484"
)
_SMALL_FACE_IDS = [int(face_id) for face_id in _SMALL_FACE_ID_TEXT.split()]

# 大黄脸表情 ID：超级表情，单独发送一条消息时 QQ 客户端会自动放大渲染
_LARGE_FACE_ID_TEXT = (
    "5 53 74 75 137 311 312 314 317 318 319 320 324 325 326 333 337 338 "
    "339 341 342 343 344 345 346 349 350 351 366 368 369 372 373 375 378 380 "
    "381 384 385 386 387 395 398 399 402 404 411 413 424 425 426 427"
)
_LARGE_FACE_IDS = [int(face_id) for face_id in _LARGE_FACE_ID_TEXT.split()]

# emoji 候选码点范围（十六进制，"起-止"或单点）：由 emoji-regex（Emoji 15.1）
# 的单码点筛选结果压缩而来，覆盖象形文字主区与散落在通用区块的零星 emoji，
# 参考 react.py 的范围思路；肤色修饰符等只能跟随基础表情使用的组合字符已剔除
_EMOJI_RANGE_TEXT = (
    "a9 ae 203c 2049 2122 2139 2194-2199 21a9-21aa 231a-231b 2328 23cf 23e9-23f3 "
    "23f8-23fa 24c2 25aa-25ab 25b6 25c0 25fb-25fe 2600-2604 260e 2611 2614-2615 2618 "
    "261d 2620 2622-2623 2626 262a 262e-262f 2638-263a 2640 2642 2648-2653 265f-2660 "
    "2663 2665-2666 2668 267b 267e-267f 2692-2697 2699 269b-269c 26a0-26a1 26a7 "
    "26aa-26ab 26b0-26b1 26bd-26be 26c4-26c5 26c8 26ce-26cf 26d1 26d3-26d4 26e9-26ea "
    "26f0-26f5 26f7-26fa 26fd 2702 2705 2708-270d 270f 2712 2714 2716 271d 2721 2728 "
    "2733-2734 2744 2747 274c 274e 2753-2755 2757 2763-2764 2795-2797 27a1 27b0 27bf "
    "2934-2935 2b05-2b07 2b1b-2b1c 2b50 2b55 3030 303d 3297 3299 1f004 1f0cf "
    "1f170-1f171 1f17e-1f17f 1f18e 1f191-1f19a 1f201-1f202 1f21a 1f22f 1f232-1f23a "
    "1f250-1f251 1f300-1f321 1f324-1f393 1f396-1f397 1f399-1f39b 1f39e-1f3f0 "
    "1f3f3-1f3f5 1f3f7-1f3fa 1f400-1f4fd 1f4ff-1f53d 1f549-1f54e 1f550-1f567 "
    "1f56f-1f570 1f573-1f57a 1f587 1f58a-1f58d 1f590 1f595-1f596 1f5a4-1f5a5 1f5a8 "
    "1f5b1-1f5b2 1f5bc 1f5c2-1f5c4 1f5d1-1f5d3 1f5dc-1f5de 1f5e1 1f5e3 1f5e8 1f5ef "
    "1f5f3 1f5fa-1f64f 1f680-1f6c5 1f6cb-1f6d2 1f6d5-1f6d7 1f6dc-1f6e5 1f6e9 "
    "1f6eb-1f6ec 1f6f0 1f6f3-1f6fc 1f7e0-1f7eb 1f7f0 1f90c-1f93a 1f93c-1f945 "
    "1f947-1f9ff 1fa70-1fa7c 1fa80-1fa88 1fa90-1fabd 1fabf-1fac5 1face-1fadb "
    "1fae0-1fae8 1faf0-1faf8"
)

# 不宜出现在群聊中的个别表情：中指、18 禁标志
_EMOJI_EXCLUDED = {"\U0001f595", "\U0001f51e"}


def _build_emojis() -> list[str]:
    """把码点范围展开为可单独发送的 emoji 列表。"""
    emojis = []
    for token in _EMOJI_RANGE_TEXT.split():
        start_text, _, end_text = token.partition("-")
        start = int(start_text, 16)
        end = int(end_text, 16) if end_text else start
        for codepoint in range(start, end + 1):
            char = chr(codepoint)
            if char in _EMOJI_EXCLUDED:
                continue
            if unicodedata.category(char).startswith("C"):
                # 未分配或控制、格式类字符不可见，跳过
                continue
            emojis.append(char)
    return emojis


_EMOJIS = _build_emojis()

# 命令参数别名：large（l）与 emoji（e）互斥
_LARGE_ALIASES = {"large", "l"}
_EMOJI_ALIASES = {"emoji", "e"}

random_face = on_command("rf", aliases={"randomface"})


@random_face.handle()
async def handle_random_face(args: Message = CommandArg()) -> None:
    want_large = False
    want_emoji = False
    for token in args.extract_plain_text().lower().split():
        if token in _LARGE_ALIASES:
            want_large = True
        elif token in _EMOJI_ALIASES:
            want_emoji = True
        else:
            await random_face.finish("无法识别的参数，用法：/rf [large|l] [emoji|e]")

    if want_large and want_emoji:
        await random_face.finish("参数 large（l）与 emoji（e）互斥，只能选择其中一个")

    if want_emoji:
        await random_face.finish(MessageSegment.text(random.choice(_EMOJIS)))

    # 只发送单个表情段：超级表情（大黄脸）单独发送时才会被放大渲染
    face_ids = _LARGE_FACE_IDS if want_large else _SMALL_FACE_IDS
    await random_face.finish(MessageSegment.face(random.choice(face_ids)))
