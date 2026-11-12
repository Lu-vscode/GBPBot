"""决斗流程（包入口）：结算与机器人代打。

- settle.py：胜负判定结果落地（settling 事件、结算接管、分数更新与播报）；
- botplay.py：机器人代替一方发送猜拳表情直至满足所需数量。
"""

from src.plugins.duel.flow.botplay import send_bot_rps
from src.plugins.duel.flow.settle import resolve_duel

__all__ = ["resolve_duel", "send_bot_rps"]
