"""帮助插件。

用法：/help
"""

from nonebot import on_command
from nonebot.plugin import PluginMetadata

__plugin_meta__ = PluginMetadata(
    name="帮助",
    description="回复 GBPBot 用户手册地址，引导用户获取使用帮助",
    usage="/help：回复 GBPBot 用户手册地址",
    type="application",
    supported_adapters={"~onebot.v11"},
)

help_cmd = on_command("help")


@help_cmd.handle()
async def handle_help() -> None:
    await help_cmd.finish("请参阅 GBPBot 用户手册：https://github.com/Lu-vscode/GBPBot")
