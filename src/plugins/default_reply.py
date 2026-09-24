"""默认回复插件。

当群聊中被 @ 或私聊收到消息，且其他插件均未响应时，回复默认消息。
"""

from nonebot import on_message
from nonebot.plugin import PluginMetadata
from nonebot.rule import to_me

__plugin_meta__ = PluginMetadata(
    name="默认回复",
    description="群聊中被 @ 或私聊收到消息且其他插件均未响应时，回复默认消息",
    usage="群聊中 @机器人 或私聊发送消息且其他插件均未响应时触发",
    type="application",
    supported_adapters={"~onebot.v11"},
)

DEFAULT_REPLY = "嗨~，我是Q群管家Pro，暂时还不能和你对话哦。"

# 以远低于其他插件的优先级作为兜底：其他插件响应后事件会停止传播，
# 只有所有插件均未响应时才会轮到这里
default_reply = on_message(rule=to_me(), priority=999, block=True)


@default_reply.handle()
async def handle_default_reply() -> None:
    await default_reply.finish(DEFAULT_REPLY)
