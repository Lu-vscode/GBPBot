"""默认回复插件。

当群聊中被 @ 或私聊收到消息，且没有任何插件对该消息作出响应时，
回复默认消息。

“已响应”的判定：事件处理期间，任一插件调用发送消息、贴表情等响应类
接口（见 `_RESPONSE_APIS`）即视为已响应该消息。判定不依赖响应器的
优先级与事件传播中的 block 设置，因此其它插件即使以 block=False 运行
（项目惯例）并已作出响应，也不会再回复默认消息；反之，运行了但未发送
任何消息的插件（如频率限制门、昵称合规检查）不算响应。

实现方式：`Bot.on_calling_api` 钩子中通过 `current_event` 获取正在处理
的事件并标记；`event_postprocessor` 在所有响应器执行完毕后（不受
stop_propagation 影响）对未被标记且满足触发条件的事件发送默认消息。
"""

from typing import Any

from nonebot import get_plugin_config, logger
from nonebot.adapters import Bot as BaseBot
from nonebot.adapters.onebot.v11 import (
    Bot,
    Event,
    GroupMessageEvent,
    PrivateMessageEvent,
)
from nonebot.adapters.onebot.v11.exception import ActionFailed, NetworkError
from nonebot.matcher import current_event
from nonebot.message import event_postprocessor
from nonebot.plugin import PluginMetadata
from pydantic import BaseModel

__plugin_meta__ = PluginMetadata(
    name="默认回复",
    description="群聊中被 @ 或私聊收到消息且没有任何插件响应时，回复默认消息",
    usage="群聊中 @机器人 或私聊发送消息，且没有任何插件对该消息作出响应时触发",
    type="application",
    supported_adapters={"~onebot.v11"},
)


class Config(BaseModel):
    """默认回复插件配置。

    可在 `.env.{environment}` 文件中通过 `DEFAULT_REPLY` 配置。
    """

    default_reply: str = "不知道怎么使用 Bot ？发送 /help 即可查看帮助～"


plugin_config = get_plugin_config(Config)

# 视为“对消息作出响应”的 API：任一插件调用它们即认为已响应该消息。
# 若后续插件使用其它方式响应用户（如协议端扩展接口），请将接口名
# 一并加入本集合，否则该响应不会被识别。
_RESPONSE_APIS = frozenset(
    {
        "send_msg",
        "send_private_msg",
        "send_group_msg",
        "send_private_forward_msg",
        "send_group_forward_msg",
        "send_forward_msg",
        "set_msg_emoji_like",
    }
)

# 已被响应的事件集合（id(event)）：由 _mark_responded 写入，
# 由事件后处理读取并清理，不会跨事件残留
_responded_events: set[int] = set()


@Bot.on_calling_api
async def _mark_responded(_bot: BaseBot, api: str, _data: dict[str, Any]) -> None:
    """插件调用响应类接口时，把正在处理的事件标记为已响应。

    钩子在调用 API 的任务（各响应器的 handler 任务及其调用链）中执行，
    可从 current_event 取到正在处理的事件；非事件上下文中的调用
    （如启动检查、定时任务）取不到事件，直接忽略。
    """
    if api not in _RESPONSE_APIS:
        return
    event = current_event.get(None)
    if event is not None:
        _responded_events.add(id(event))


def _has_at_bot(event: GroupMessageEvent) -> bool:
    """判断消息原始内容中是否 @ 了机器人。

    只检查 original_message：适配器会剥离消息首尾的 @ 段
    （_check_at_me），且“回复机器人消息”也会被置为 to_me
    （_check_reply），二者都不能反映用户是否真的 @ 了机器人。
    """
    return any(
        segment.type == "at" and str(segment.data.get("qq", "")) == str(event.self_id)
        for segment in event.original_message
    )


def _should_reply(event: Event) -> bool:
    """判断事件是否满足默认回复的触发条件：群聊中被 @ 或私聊消息。"""
    if isinstance(event, PrivateMessageEvent):
        return True
    if isinstance(event, GroupMessageEvent):
        return _has_at_bot(event)
    return False


@event_postprocessor
async def _reply_if_unanswered(bot: Bot, event: Event) -> None:
    """事件全部处理完毕后，若无人响应该消息则回复默认消息。"""
    responded = id(event) in _responded_events
    # 无条件清理本事件的标记：非消息类事件同样可能因插件发送消息而被写入
    _responded_events.discard(id(event))

    if responded or not _should_reply(event):
        return

    try:
        await bot.send(event, plugin_config.default_reply)
    except (ActionFailed, NetworkError) as exc:
        logger.warning(f"发送默认回复失败：{exc}")
    finally:
        # 防止本次发送被误标记为响应，影响 id 复用后的新事件
        _responded_events.discard(id(event))
