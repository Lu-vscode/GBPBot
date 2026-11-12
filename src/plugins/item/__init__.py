"""道具插件（主模块）。

群成员通过签到获得随机道具，可查看（/item.list、/item.detail）、合成
（/item.craft）、交换（/item.exchange 系列）与使用（/item.use）道具；
测试群（ITEM_TEST_GROUPS）中的超级用户可通过 /item.get 获取测试道具；
道具与状态按群隔离并保存在数据库。

本插件经跨插件服务注册中心（见 _shared/services/）对外提供道具发放
服务（ItemService）：签到等插件调用 grant_random 发放道具，随后调用
handle_acquisition 完成黑色诅咒的展示与自动使用；同时提供签到分数
加成服务（SignScoreBonusService），签到插件据此计算道具带来的额外分数。

本插件为包子包结构（由原单文件 item.py 拆分而来），本模块只负责插件
元数据、配置注入、服务实例注册与启动日志：

- config/：配置模型与解析后的数值（model.py、resolve.py）；
- storage/：数据库模型与读写原语（models.py、lock.py、inventory.py、
  states.py），只做读写、不含业务语义；
- core/：道具定义与注册表（definition.py、registry.py）、库存与状态
  API、使用流程与钩子（use.py）、抽取与合成（draw.py、craft.py）、
  耐久型行为块（durability.py），以及决斗分数与签到加成的桥接；道具
  模块（items/）只从 core 包导入；
- commands/：查看、合成、使用与测试获取指令（list_detail.py、craft.py、
  use.py、get.py）及其用法文案（texts.py）；
- exchange/：交换请求的内存队列（queue.py）与三条交换指令
  （commands.py）；
- services.py：ItemService 与 SignScoreBonusService 的实现与注册；
- messaging.py：指令与交换共用的消息发送、@ 解析与群昵称截断；
- cleanup.py：过期道具状态与过期交换请求的定时清理；
- items/：各道具模块，导入时自动注册。
"""

from nonebot import logger
from nonebot.plugin import PluginMetadata

from src.plugins.item.config import CRAFT_ODDS, DRAW_WEIGHTS, TEST_GROUPS
from src.plugins.item.core import iter_items, set_craft_odds, set_draw_weights
from src.plugins.item.exchange.queue import EXCHANGE_DURATION_SECONDS

__plugin_meta__ = PluginMetadata(
    name="道具",
    description="群聊道具系统：签到获得随机道具，可查看、合成、交换与使用",
    usage=(
        "/item.list：查看自己拥有的道具\n"
        "/item.detail <道具编号>（别名 /item.info）：查看自己拥有道具的详细信息\n"
        "/item.craft <道具编号1> <道具编号2>：用两件同品质道具合成一件随机道具\n"
        "/item.exchange @成员 <自己的道具编号> <对方的道具编号>：发起道具交换\n"
        "/item.exchange.accept @成员：接受对方的道具交换\n"
        "/item.exchange.reject @成员：拒绝对方的道具交换\n"
        "/item.use <道具编号> [其它参数]：使用自己拥有的一件道具"
    ),
    type="application",
    supported_adapters={"~onebot.v11"},
)

# 注入解析后的配置：抽取权重与合成概率（须在导入道具模块之前完成）
set_draw_weights(DRAW_WEIGHTS)
set_craft_odds(CRAFT_ODDS)

# 导入道具模块（各模块在导入时调用 register_item 完成注册）与指令、交换、
# 服务、清理模块（各模块在导入时完成指令注册、服务注册与定时任务登记）
from src.plugins.item import cleanup, commands, exchange, items, services

__all__ = ["cleanup", "commands", "exchange", "items", "services"]

logger.info(
    f"道具系统已启用，共注册 {len(iter_items())} 件道具，"
    f"交换有效期为 {int(EXCHANGE_DURATION_SECONDS // 60)} 分钟"
)
if TEST_GROUPS:
    logger.info(f"道具测试获取已启用（/item.get），测试群：{sorted(TEST_GROUPS)}")
