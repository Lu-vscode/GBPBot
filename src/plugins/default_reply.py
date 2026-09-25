"""默认回复插件。

当群聊中被 @ 或私聊收到消息，且其他插件均未响应时，回复默认消息。
"""

from nonebot import get_plugin_config, on_message
from nonebot.plugin import PluginMetadata
from nonebot.rule import to_me
from pydantic import BaseModel

__plugin_meta__ = PluginMetadata(
    name="默认回复",
    description="群聊中被 @ 或私聊收到消息且其他插件均未响应时，回复默认消息",
    usage="群聊中 @机器人 或私聊发送消息且其他插件均未响应时触发",
    type="application",
    supported_adapters={"~onebot.v11"},
)


class Config(BaseModel):
    """默认回复插件配置。

    可在 `.env.{environment}` 文件中通过 `DEFAULT_REPLY` 配置。
    """

    default_reply: str = "不知道怎么使用 Bot ？发送 /help 即可查看帮助～"


plugin_config = get_plugin_config(Config)

# 以远低于其他插件的优先级作为兜底：其他插件响应后事件会停止传播，
# 只有所有插件均未响应时才会轮到这里
default_reply = on_message(rule=to_me(), priority=999, block=True)


@default_reply.handle()
async def handle_default_reply() -> None:
    await default_reply.finish(plugin_config.default_reply)
