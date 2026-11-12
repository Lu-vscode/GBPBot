"""查看、合成与使用指令的用法提示文案。

成功播报由各指令自由编写，不入 env 配置；交换指令的提示文案见
exchange/commands.py（交换语义自成一体）。
"""

USAGE_LIST = "参数无法识别，用法：/item.list"
USAGE_DETAIL = "请提供道具编号，用法：/item.detail <道具编号>"
USAGE_CRAFT = "请提供两个道具编号，用法：/item.craft <道具编号1> <道具编号2>"
USAGE_USE = "请提供道具编号，用法：/item.use <道具编号> [其它参数]"
