"""插件共享代码包。

本包以 "_" 开头，不会被 NoneBot 当作插件加载（load_plugins 会跳过
以下划线开头的模块）；供各插件导入以共享代码，例如跨插件的服务
注册中心（见 services.py）。插件中通过完整模块路径导入，如：
`from src.plugins._shared.services import register_service`
"""
