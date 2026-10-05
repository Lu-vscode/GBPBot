"""跨插件服务注册中心。

NoneBot 的插件之间不应互相导入模块（提前导入会导致插件无法由框架
正常加载，见 PluginLoader 对已导入模块的检查）；本模块提供一套解耦
的跨插件交互方式：提供方插件把自身能力注册为"服务"，消费方插件按
契约定向获取并调用。

约定：
1. 服务契约（@runtime_checkable 的 Protocol，仅含方法）与注册、获取
   接口都定义在本共享模块中，交互双方共同依赖契约，不互相导入。
2. 提供方插件在加载时注册服务（注册早于任何事件处理）：
   `register_service(DuelScoreService, _DuelScoreService())`
3. 消费方插件在加载时先用 `nonebot.plugin.require` 声明对提供方插件
   的依赖（确保其已加载、服务已注册），再获取服务：
   `require("duel")`
   `service = get_service(DuelScoreService)`      # 可选依赖
   `service = require_service(DuelScoreService)`  # 必需依赖
4. 本模块所在包以 "_" 开头，不会被 NoneBot 当作插件加载；插件中通过
   `from src.plugins._shared.services import ...` 导入（插件模块名形如
   src.plugins.xxx，与运行时命名一致）。
"""

from typing import Any, Protocol, TypeVar, runtime_checkable

T = TypeVar("T")


@runtime_checkable
class DuelScoreService(Protocol):
    """决斗分数服务（由猜拳决斗插件提供）。"""

    def add_score(self, group_id: int, user_id: int, name: str, delta: int) -> int:
        """更新指定群内成员的决斗分数并写入本地存储，返回更新后的分数。

        参数:
            group_id: 群号。
            user_id: 成员 QQ 号。
            name: 成员在群内的显示名（提供方按自身的显示规范处理）。
            delta: 分数变化量，可为负数。
        """
        ...


# 已注册的服务：契约类型 -> 提供方实现对象
_service_providers: dict[type[Any], Any] = {}


def register_service(interface: type[T], provider: T) -> None:
    """注册服务提供方。

    参数:
        interface: 服务契约（@runtime_checkable 的 Protocol 类型）。
        provider: 实现该契约的对象。

    异常:
        TypeError: 实现对象不符合契约（缺少契约要求的方法）。
        RuntimeError: 该契约已注册过服务（通常意味着插件被重复加载，
            属于需要暴露的错误，不做静默覆盖）。
    """
    if interface in _service_providers:
        message = f"服务 {interface.__name__} 已注册，不能重复注册"
        raise RuntimeError(message)
    if not isinstance(provider, interface):
        message = f"服务实现不符合契约 {interface.__name__} 的约定"
        raise TypeError(message)
    _service_providers[interface] = provider


def get_service(interface: type[T]) -> T | None:
    """获取已注册的服务提供方，未注册时返回 None（供可选依赖使用）。"""
    return _service_providers.get(interface)


def require_service(interface: type[T]) -> T:
    """获取已注册的服务提供方，未注册时抛出异常（供必需依赖使用）。

    异常:
        RuntimeError: 服务尚未注册（提供方插件未加载或未注册服务）。
    """
    service = get_service(interface)
    if service is None:
        message = f"服务 {interface.__name__} 尚未注册"
        raise RuntimeError(message)
    return service
