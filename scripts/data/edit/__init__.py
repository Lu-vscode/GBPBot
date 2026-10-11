"""GBPBot 数据文件安全编辑工具。

用于在生产环境通过命令行安全地修改 localstore 数据文件（替代直接用
nano 等编辑器改 JSON）：机器人服务运行中拒绝修改，修改前自动备份原
文件，以临时文件 + 原子替换写盘，任何一步失败都不会触碰原文件。

模块与数据文件对应关系（数据格式与对应插件源码约定一致，插件侧格式
变更时必须同步维护）：
- _scores.py：data/duel/scores.json（对应 src/plugins/duel/_scores.py）
- _inventory.py：data/item/inventory.json（对应 src/plugins/item/_storage.py）
- _states.py：data/item/states.json（对应 src/plugins/item/_storage.py）
"""
