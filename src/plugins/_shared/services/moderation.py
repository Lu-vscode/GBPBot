"""内容审核服务（由内容审核插件提供，其它插件调用）。

当前只提供纯文本审核：把一段文本交给提供方判定是否放行。审核依赖大
模型，接口超时、鉴权失败或返回内容无法解析时抛出 ModerationUnavailableError，
由调用方决定按通过还是按拦截处理；服务本身不吞错，也不会替调用方
发送任何消息。
"""

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


class ModerationUnavailableError(RuntimeError):
    """审核服务不可用（接口超时、鉴权失败、返回内容无法解析等）。"""


@dataclass(frozen=True)
class ModerationResult:
    """一次纯文本审核的结果。"""

    allowed: bool
    """是否放行：True 通过，False 拦截。"""

    categories: tuple[str, ...] = ()
    """命中的违规类别（被拦截时通常非空，取值为审核标准中的类别名）。"""

    reason: str = ""
    """一句话判定理由，可能为空字符串。"""


@runtime_checkable
class TextModerationService(Protocol):
    """纯文本内容审核服务（由内容审核插件提供）。"""

    async def check_text(self, text: str, *, scene: str = "") -> ModerationResult:
        """审核一段纯文本，返回是否放行。

        判定标准为 QQ 平台的内容安全规范；同一段文本（含 scene）在缓存
        有效期内直接返回上次结果，不会重复请求模型。方法会发起网络请求，
        单次耗时可达数秒，调用方应避免在每条消息上无条件同步等待。

        参数:
            text: 待审核的纯文本（调用方应先自行剥离 @、表情等非文本段）。
            scene: 文本出现场景的简短说明（如"群聊正文""群昵称"），可为空。

        异常:
            ModerationUnavailableError: 审核不可用（超时、鉴权失败、返回
                无法解析），调用方需自行决定放行还是拦截。
        """
        ...
