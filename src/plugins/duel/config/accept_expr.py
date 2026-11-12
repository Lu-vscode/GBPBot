"""机器人接受决斗的概率函数：表达式沙箱校验与求值。

配置项 DUEL_BOT_ACCEPT_FUNC 为 Python 表达式（变量 x 为点数，可使用四则
运算、幂运算与 math 库的常量、函数）。本模块在静态白名单内解析表达式，
并在全部可选点数上求值校验结果落在 0~1 之间；不符合限制、无法求值或
耗时过长的表达式一律拒绝并回退默认函数（1/点数），不会因配置错误导致
插件加载失败。

沙箱同时限制表达式长度、语法节点数、嵌套深度、总求值耗时、幂运算的
指数形式与整数常量位数，防止过于复杂的表达式拖垮服务器。
"""

import ast
import math
import time
from collections.abc import Callable
from types import CodeType
from typing import Any

from nonebot import logger

from src.plugins.duel.config.model import plugin_config
from src.plugins.duel.config.values import MAX_MULTIPLIER


def _default_bot_accept_probability(multiplier: int) -> float:
    """默认的机器人接受决斗概率函数：接受概率为 1/点数。"""
    return 1 / multiplier


# 表达式求值阶段的异常捕获集合（编译阶段另行捕获 SyntaxError/ValueError）
_ACCEPT_EXPR_ERRORS = (
    ArithmeticError,
    AttributeError,
    NameError,
    TypeError,
    ValueError,
)

# 表达式资源限制：长度、语法节点数、嵌套深度、全部点数校验的总耗时
# （秒）、幂运算的指数上限与整数常量的位数上限；超出限制的表达式
# 一律拒绝，防止过于复杂的表达式拖垮服务器
_MAX_EXPR_LENGTH = 200
_MAX_EXPR_NODES = 64
_MAX_EXPR_DEPTH = 16
_EXPR_TIME_BUDGET_SECONDS = 0.5
_MAX_POW_EXPONENT = 1024
_MAX_INT_LITERAL_BITS = 64

# 概率函数表达式允许使用的 math 常量与函数（均为 C 实现的快速函数）
_MATH_ALLOWED_CONSTANTS = frozenset({"pi", "e", "tau", "inf", "nan"})
_MATH_ALLOWED_FUNCTIONS = frozenset(
    {
        "sin",
        "cos",
        "tan",
        "asin",
        "acos",
        "atan",
        "atan2",
        "sinh",
        "cosh",
        "tanh",
        "asinh",
        "acosh",
        "atanh",
        "exp",
        "expm1",
        "log",
        "log1p",
        "log2",
        "log10",
        "sqrt",
        "cbrt",
        "fabs",
        "floor",
        "ceil",
        "trunc",
        "copysign",
        "fmod",
        "pow",
        "hypot",
        "dist",
        "degrees",
        "radians",
        "isfinite",
        "isinf",
        "isnan",
    }
)
_MATH_ALLOWED_NAMES = _MATH_ALLOWED_CONSTANTS | _MATH_ALLOWED_FUNCTIONS

# 表达式求值的命名空间：禁用内置函数，仅提供 math 库与变量 x
_ACCEPT_EVAL_GLOBALS: dict[str, Any] = {"__builtins__": {}, "math": math}

# 表达式静态检查允许的二元/一元运算符与语法节点
_ALLOWED_BINOPS = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow)
_ALLOWED_UNARYOPS = (ast.UAdd, ast.USub)
_ALLOWED_NODE_TYPES = (
    ast.Expression,
    ast.BinOp,
    ast.UnaryOp,
    ast.Constant,
    ast.Name,
    ast.Attribute,
    ast.Call,
    ast.keyword,
    ast.Load,
    *_ALLOWED_BINOPS,
    *_ALLOWED_UNARYOPS,
)

# 表达式静态检查允许引用的名称
_ALLOWED_NAMES = frozenset({"x", "math"})


def _constant_error(node: ast.Constant) -> str | None:
    """检查常量节点：仅支持有限的数值常量。"""
    value = node.value
    if isinstance(value, bool):
        return "表达式不支持布尔值。"
    if isinstance(value, int):
        if value.bit_length() > _MAX_INT_LITERAL_BITS:
            return "表达式中的整数常量过大。"
        return None
    if isinstance(value, float):
        return None
    return "表达式不支持字符串等非数值常量。"


def _name_error(node: ast.Name) -> str | None:
    """检查名称引用：仅允许 x 与 math。"""
    if node.id not in _ALLOWED_NAMES:
        return f"表达式不支持名称 {node.id}（仅支持 x 与 math）。"
    return None


def _attribute_error(node: ast.Attribute) -> str | None:
    """检查属性访问：仅允许 math 库的常用常量与函数。"""
    if not (isinstance(node.value, ast.Name) and node.value.id == "math"):
        return "表达式仅允许访问 math 库的常量与函数。"
    if node.attr not in _MATH_ALLOWED_NAMES:
        return f"表达式不支持 math.{node.attr}（仅支持 math 库的常用函数与常量）。"
    return None


def _call_error(node: ast.Call) -> str | None:
    """检查函数调用：仅允许调用 math 库的常用函数。"""
    func = node.func
    if not (
        isinstance(func, ast.Attribute)
        and isinstance(func.value, ast.Name)
        and func.value.id == "math"
    ):
        return "表达式仅允许调用 math 库的函数。"
    if func.attr not in _MATH_ALLOWED_FUNCTIONS:
        return f"表达式不支持 math.{func.attr}（仅支持 math 库的常用函数）。"
    return None


def _pow_exponent_ok(node: ast.expr) -> bool:
    """判断幂运算的指数是否为允许的简单形式（常量、x 或 -x）。"""
    if isinstance(node, ast.Constant) and not isinstance(node.value, bool):
        if isinstance(node.value, (int, float)):
            return abs(node.value) <= _MAX_POW_EXPONENT
        return False
    if isinstance(node, ast.Name):
        return node.id == "x"
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        return _pow_exponent_ok(node.operand)
    return False


def _pow_error(node: ast.BinOp) -> str | None:
    """检查幂运算：限制指数形式，禁止幂运算嵌套在底数中。"""
    if not _pow_exponent_ok(node.right):
        return (
            "幂运算（**）的指数只能是绝对值不超过 "
            f"{_MAX_POW_EXPONENT} 的数字常量、x 或 -x。"
        )
    if any(isinstance(child, ast.Pow) for child in ast.walk(node.left)):
        return "幂运算（**）不能嵌套使用。"
    return None


def _binop_error(node: ast.BinOp) -> str | None:
    """检查二元运算：运算符白名单与幂运算规则。"""
    if not isinstance(node.op, _ALLOWED_BINOPS):
        return "表达式包含不支持的运算符。"
    if isinstance(node.op, ast.Pow):
        return _pow_error(node)
    return None


def _unaryop_error(node: ast.UnaryOp) -> str | None:
    """检查一元运算：仅允许正负号。"""
    if not isinstance(node.op, _ALLOWED_UNARYOPS):
        return "表达式包含不支持的运算符。"
    return None


def _node_error(node: ast.AST) -> str | None:
    """检查单个语法节点（节点类型、白名单与运算规则）。"""
    error: str | None = None
    if not isinstance(node, _ALLOWED_NODE_TYPES):
        error = (
            "表达式包含不支持的结构（仅支持四则运算、幂运算与 math 库的常量、函数）。"
        )
    elif isinstance(node, ast.Constant):
        error = _constant_error(node)
    elif isinstance(node, ast.Name):
        error = _name_error(node)
    elif isinstance(node, ast.Attribute):
        error = _attribute_error(node)
    elif isinstance(node, ast.Call):
        error = _call_error(node)
    elif isinstance(node, ast.BinOp):
        error = _binop_error(node)
    elif isinstance(node, ast.UnaryOp):
        error = _unaryop_error(node)
    return error


def _expr_structure_error(tree: ast.Expression) -> str | None:
    """静态检查表达式结构与规模：拒绝白名单外的语法与过大、过深的表达式。"""
    count = 0
    stack: list[tuple[ast.AST, int]] = [(tree, 0)]
    while stack:
        node, depth = stack.pop()
        count += 1
        if count > _MAX_EXPR_NODES:
            return f"表达式过于复杂（语法节点超过 {_MAX_EXPR_NODES} 个）。"
        if depth > _MAX_EXPR_DEPTH:
            return f"表达式嵌套过深（超过 {_MAX_EXPR_DEPTH} 层）。"
        error = _node_error(node)
        if error is not None:
            return error
        stack.extend((child, depth + 1) for child in ast.iter_child_nodes(node))
    return None


def _parse_expr(expr: str) -> ast.Expression | str:
    """解析表达式源码；长度超限或解析失败时返回面向用户的错误文案。"""
    if len(expr) > _MAX_EXPR_LENGTH:
        return f"表达式过长（最多 {_MAX_EXPR_LENGTH} 个字符）。"
    try:
        return ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        location = f"（第 {exc.offset} 个字符附近）" if exc.offset else ""
        return f"表达式无法解析：{exc.msg}{location}"
    except ValueError as exc:
        return f"表达式无法解析：{exc}"


def _eval_probabilities_error(code: CodeType) -> str | None:
    """在全部可选点数上求值并检查概率范围；超时或越界时返回错误文案。"""
    deadline = time.monotonic() + _EXPR_TIME_BUDGET_SECONDS
    for x in range(1, MAX_MULTIPLIER + 1):
        if time.monotonic() > deadline:
            return "表达式校验超时（运算过于复杂）。"
        try:
            probability = float(eval(code, _ACCEPT_EVAL_GLOBALS, {"x": x}))
        except _ACCEPT_EXPR_ERRORS as exc:
            return f"表达式在点数 {x} 时求值失败：{exc}"
        if not 0.0 <= probability <= 1.0:
            return f"表达式在点数 {x} 时的值为 {probability}，不是 0~1 之间的概率"
    if time.monotonic() > deadline:
        return "表达式校验超时（运算过于复杂）。"
    return None


def accept_expr_error(expr: str) -> str | None:
    """校验接受概率表达式在全部可选点数上都能求值为 0~1 的浮点数。

    表达式为 Python 表达式（变量 x 为决斗点数，可使用 math 库的常量与
    函数），在受限命名空间中求值；静态检查限定语法结构与规模，逐点
    求值设有总耗时预算，拒绝过于复杂或耗时的表达式。返回面向用户的
    错误文案，合法时返回 None。
    """
    tree = _parse_expr(expr)
    if isinstance(tree, str):
        return tree
    structure_error = _expr_structure_error(tree)
    if structure_error is not None:
        return structure_error
    code = compile(tree, "<DUEL_BOT_ACCEPT_FUNC>", "eval")
    return _eval_probabilities_error(code)


def _bot_accept_func_or_default(expr: str | None) -> Callable[[int], float]:
    """解析机器人接受决斗的概率函数，无效时报错并使用默认函数。

    配置为 Python 表达式（变量 x 为点数，可使用四则运算、幂运算与
    math 库的常量、函数）；当表达式不符合静态白名单、超出长度/规模/
    耗时限制、无法求值，或在点数取值范围 1..上限 内存在小于 0 或大于
    1 的概率时，记录错误并回退默认函数 1/点数。
    """
    text_value = (expr or "").strip()
    if not text_value:
        return _default_bot_accept_probability
    error = accept_expr_error(text_value)
    if error is not None:
        logger.error(
            f"猜拳决斗配置 DUEL_BOT_ACCEPT_FUNC={text_value!r} 校验未通过"
            f"（{error}），已使用默认函数 1/点数"
        )
        return _default_bot_accept_probability
    code = compile(text_value, "<DUEL_BOT_ACCEPT_FUNC>", "eval")

    def accept_probability(multiplier: int) -> float:
        return float(eval(code, _ACCEPT_EVAL_GLOBALS, {"x": multiplier}))

    logger.info(f"机器人接受决斗的概率函数已配置：{text_value}")
    return accept_probability


# 机器人接受决斗的概率函数：点数 -> 接受概率
BOT_ACCEPT_PROBABILITY = _bot_accept_func_or_default(plugin_config.duel_bot_accept_func)
