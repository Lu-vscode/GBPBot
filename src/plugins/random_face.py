"""随机发送表情的插件。

用法：/rf（或 /randomface）[large|l] [emoji|e]
"""

import random

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
    "0 1 2 3 4 6 7 8 9 10 11 12 13 14 15 16 18 19 20 21 "
    "22 23 24 25 26 27 28 29 30 31 32 33 34 35 36 37 38 39 "
    "41 42 43 46 49 56 59 60 63 64 66 67 76 77 78 79 85 86 "
    "89 96 97 98 99 100 101 102 103 104 105 106 107 108 109 "
    "110 111 112 116 118 119 120 121 123 124 125 129 144 146 "
    "147 169 171 172 173 174 175 176 177 178 179 182 183 185 "
    "187 201 212"
)
_SMALL_FACE_IDS = [int(face_id) for face_id in _SMALL_FACE_ID_TEXT.split()]

# 大黄脸表情 ID：超级表情，单独发送一条消息时 QQ 客户端会自动放大渲染
_LARGE_FACE_ID_TEXT = (
    "5 53 74 75 137 311 312 314 317 318 319 320 324 325 326 "
    "333 337 338 339 341 342 343 344 345 346 349 350 351 "
    "395 424 425 426 427"
)
_LARGE_FACE_IDS = [int(face_id) for face_id in _LARGE_FACE_ID_TEXT.split()]

# emoji 表情：以纯文本消息段发送
_EMOJI_TEXT = (
    "😀 😄 😁 😂 🤣 😊 😇 🙂 😉 😍 "
    "🥰 😘 😋 😜 🤪 🤗 🤔 🤭 🥳 😎 "
    "🤓 🧐 😏 😒 😔 😢 😭 😤 😡 🤯 "
    "😱 😨 🥺 😴 🙄 😬 🥴 🤢 🤧 😷 "
    "👍 👎 👏 🙏 💪 ✌️ 👌 ❤️ 💔 💯 "
    "🎉 🎁 🔥 ⭐ 🍀 🌙 🐶 🐱 🌸 🍉"
)
_EMOJIS = _EMOJI_TEXT.split()

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
