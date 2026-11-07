"""GBPBot 数据安全编辑工具。

用于在生产环境通过命令行安全地修改插件数据库（替代直接改 JSON 文件
或手工连库）：机器人服务运行中拒绝修改，修改前自动生成数据库快照，
修改在单个事务内完成，任何一步失败都会整体回滚。

模块与数据表对应关系（表名/列名与对应插件模型一致，插件侧表结构变更
时必须同步维护）：
- _scores.py：gbp_duel_score（对应 src/plugins/duel/_models.py）
- _inventory.py：gbp_item_inventory（对应 src/plugins/item/_models.py）
- _states.py：gbp_item_state（对应 src/plugins/item/_models.py）
- _signs.py：gbp_sign_record（对应 src/plugins/sign.py，只读）
"""
