"""跨插件服务的注册与获取。

提供方插件在加载时把自身能力注册为"服务"，消费方插件按契约获取：
重复注册视为插件被重复加载（抛出 RuntimeError），实现不符合契约
（缺少契约要求的方法）时抛出 TypeError。各服务契约见同包的各子模块。
"""

from typing import Any

# 已注册的服务：契约类型 -> 提供方实现对象
_service_providers: dict[type[Any], Any] = {}


def register_service[T](interface: type[T], provider: T) -> None:
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


def get_service[T](interface: type[T]) -> T | None:
    """获取已注册的服务提供方，未注册时返回 None（供可选依赖使用）。"""
    return _service_providers.get(interface)


def require_service[T](interface: type[T]) -> T:
    """获取已注册的服务提供方，未注册时抛出异常（供必需依赖使用）。

    异常:
        RuntimeError: 服务尚未注册（提供方插件未加载或未注册服务）。
    """
    service = get_service(interface)
    if service is None:
        message = f"服务 {interface.__name__} 尚未注册"
        raise RuntimeError(message)
    return service
