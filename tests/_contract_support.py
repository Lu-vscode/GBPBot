"""契约测试脚本的共用支持：环境隔离、插件加载、源码扫描与断言记录。

供 tests/item 与 tests/duel 下的契约测试脚本导入，不属于插件运行时代码。
每个脚本独立运行（各自初始化一次 nonebot，互不共享进程状态）：

    .venv/Scripts/python.exe tests/item/test_item_contracts.py
    .venv/Scripts/python.exe tests/duel/test_duel_contracts.py
"""

import ast
import os
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import nonebot
from nonebot import logger
from nonebot.adapters.onebot.v11 import Adapter
from nonebot.plugin import get_loaded_plugins
from nonebot.rule import CommandRule

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def prepare_environment() -> Path:
    """切换工作目录到项目根并隔离数据目录，返回临时目录。

    LOCALSTORE_DATA_DIR 与 SQLALCHEMY_DATABASE_URL 都指向临时目录，
    契约测试不会触碰工作区的 data/ 生产数据。
    """
    os.chdir(PROJECT_ROOT)
    tmp_dir = Path(tempfile.mkdtemp(prefix="gbpbot_contract_test_"))
    os.environ["LOCALSTORE_DATA_DIR"] = str(tmp_dir / "localstore")
    os.environ["SQLALCHEMY_DATABASE_URL"] = (
        f"sqlite+aiosqlite:///{(tmp_dir / 'db.sqlite3').as_posix()}"
    )
    return tmp_dir


def init_nonebot() -> None:
    """初始化 nonebot 运行环境（不启动服务，仅用于加载插件）。"""
    nonebot.init(driver="~none")
    nonebot.get_driver().register_adapter(Adapter)


def load_plugins(names: Sequence[str]) -> bool:
    """按给定顺序加载插件，任一插件加载失败时返回 False。"""
    for name in names:
        if nonebot.load_plugin(name) is None:
            logger.error(f"插件 {name} 加载失败，无法继续检查")
            return False
    return True


def matcher_commands(matcher: object) -> set[str]:
    """读取 matcher 的规则中声明的全部命令名（含别名）。"""
    rule = getattr(matcher, "rule", None)
    commands: set[str] = set()
    for checker in getattr(rule, "checkers", set()):
        call = getattr(checker, "call", None)
        if not isinstance(call, CommandRule):
            continue
        for cmd in call.cmds:
            commands.add("".join(cmd) if isinstance(cmd, tuple) else cmd)
    return commands


def plugin_commands(name: str) -> set[str]:
    """返回已加载插件的全部指令名（含道具注册的子指令与别名）。"""
    commands: set[str] = set()
    for plugin in get_loaded_plugins():
        if plugin.name == name:
            for matcher in plugin.matcher:
                commands |= matcher_commands(matcher)
    return commands


def module_imports(path: Path) -> set[str]:
    """返回源文件 import 的全部模块名（import 与 from-import 两种写法）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def source_files(*roots: Path) -> list[Path]:
    """返回若干目录下全部 Python 源文件（不含 __pycache__）。"""
    files: list[Path] = []
    for root in roots:
        files.extend(
            path
            for path in sorted(root.rglob("*.py"))
            if "__pycache__" not in path.parts
        )
    return files


def relative(path: Path) -> str:
    """返回相对项目根的路径文本（用于断言信息）。"""
    try:
        return path.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


@dataclass
class Check:
    """一条断言结果。"""

    name: str
    """断言说明。"""

    ok: bool
    """是否通过。"""

    detail: str = ""
    """失败原因（通过时为空）。"""


@dataclass
class Recorder:
    """断言记录器：累积结果并输出带退出码的报告。"""

    title: str
    """报告标题（如“道具结构契约”）。"""

    checks: list[Check] = field(default_factory=list)
    """已记录的断言。"""

    def check(self, name: str, *, ok: bool, detail: str = "") -> bool:
        """记录一条断言结果并返回是否通过。"""
        self.checks.append(Check(name, ok, detail))
        return ok

    def truthy(self, name: str, value: object, detail: str = "") -> bool:
        """断言 value 为真（detail 为失败原因，省略时显示实际值）。"""
        return self.check(name, ok=bool(value), detail=detail or f"实际值 {value!r}")

    def equal(self, name: str, actual: object, expected: object) -> bool:
        """断言两个值相等。"""
        return self.check(
            name, ok=actual == expected, detail=f"期望 {expected!r}，实际 {actual!r}"
        )

    def error_contains(
        self, name: str, func: Callable[[], object], keyword: str
    ) -> bool:
        """断言调用 func 抛出 ValueError 且异常信息包含 keyword。"""
        try:
            func()
        except ValueError as exc:
            return self.check(name, ok=keyword in str(exc), detail=f"异常信息：{exc}")
        return self.check(name, ok=False, detail="未抛出 ValueError")

    def report(self) -> int:
        """输出报告：全部通过返回 0，存在失败断言返回 1。"""
        failed = 0
        for check in self.checks:
            if check.ok:
                logger.info(f"[PASS] {check.name}")
                continue
            failed += 1
            logger.error(f"[FAIL] {check.name} -> {check.detail}")
        logger.info(
            f"========== {self.title}：PASS {len(self.checks) - failed}，"
            f"FAIL {failed} =========="
        )
        return 0 if failed == 0 else 1
