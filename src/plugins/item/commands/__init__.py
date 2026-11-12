"""道具指令（包入口）：导入各指令模块以完成 matcher 注册。

- list_detail.py：/item.list（查看持有道具）与 /item.detail（查看详情）；
- craft.py：/item.craft（用两件同品质道具合成）；
- use.py：/item.use（使用自己拥有的一件道具）；
- get.py：/item.get（测试群超级用户获取测试道具）；
- texts.py：以上指令的用法提示文案。

交换指令自成一体，见 exchange/。
"""

from src.plugins.item.commands import craft, get, list_detail, use

__all__ = ["craft", "get", "list_detail", "use"]
