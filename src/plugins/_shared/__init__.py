"""插件共享代码包。

本包以 "_" 开头，不会被 NoneBot 当作插件加载（load_plugins 会跳过
以下划线开头的模块）；供各插件导入以共享代码。插件中通过完整模块
路径导入，如 `from src.plugins._shared.services import register_service`。

本包按职责分为三个子包，各自只依赖更底层的子包，不反向依赖：

- config/：跨插件共享配置（BOT_LIST、NICKNAME_MAX_LENGTH、
  NICKNAME_GUARD_DISABLED_GROUPS）的字段声明、解析与读取；
- onebot/：与具体业务无关的 OneBot v11 群聊工具（群成员显示名与
  截断、群文本消息与合并转发消息发送），显示名默认按 config/ 的
  NICKNAME_MAX_LENGTH 截断；
- services/：跨插件服务注册中心与服务契约（决斗、道具、签到），
  插件间功能调用一律经此包，不互相导入。
"""
