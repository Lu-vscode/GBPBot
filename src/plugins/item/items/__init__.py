"""道具模块的自动发现与导入。

本包（items/）下的每个模块负责注册一件（或一组）道具：模块在导入时
调用 _framework.register_item 完成注册。新增道具只需在本目录下新建
模块文件（文件名建议使用道具的英文助记名），无需修改其它代码。

单个道具模块导入或注册失败时记录 warning 并忽略该道具，不影响其它
道具与整个插件包的加载。
"""

import importlib
import pkgutil

from nonebot import logger

# 道具模块加载失败的捕获集合：单个道具的注册错误不影响其它道具
_ITEM_MODULE_ERRORS = (Exception,)


def _import_item_modules() -> None:
    """导入本包下全部子模块（跳过下划线开头的模块）。

    单个模块导入或注册失败时记录 warning 并忽略该道具。
    """
    for module_info in pkgutil.iter_modules(__path__):
        if module_info.name.startswith("_"):
            continue
        try:
            importlib.import_module(f"{__name__}.{module_info.name}")
        except _ITEM_MODULE_ERRORS as exc:
            logger.warning(
                f"道具模块 {__name__}.{module_info.name} 加载失败，"
                f"已忽略该道具：{exc!r}"
            )
            continue
        logger.debug(f"已导入道具模块 {__name__}.{module_info.name}")


_import_item_modules()
