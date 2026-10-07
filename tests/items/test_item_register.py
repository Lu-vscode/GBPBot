"""道具注册通用测试脚本：检查 items/ 下的道具模块能否正常注册。

用法（项目根目录或任意目录下均可运行）：

    .venv/Scripts/python.exe tests/items/test_item_register.py

检查过程：

1. 按真实插件链路加载 duel 与 item 插件（item 的加载器会自动发现并导入
   items/ 下的全部道具模块，模块在导入期完成注册）；数据目录重定向到
   系统临时目录，不触碰工作区的 data/ 生产数据。
2. 逐个检查 items/ 下的道具模块：导入注册成功记 PASS；导入失败时重放
   导入并给出具体原因（编号留空、编号不是三位数字、编号重复、品质与
   类型不一致或模块代码异常等）；下划线开头的模块会被加载器跳过，记 SKIP。
3. 对导入成功的模块清空注册表后按序重导，建立「模块 -> 注册道具」的
   对应关系并恢复注册表；导入成功但未注册任何道具的模块记 WARN（通常是
   忘记调用 register_item）。归因重放会让注册日志出现两遍，属正常现象。
4. 输出全部已注册道具清单与统计结果。

退出码：0 表示全部道具模块注册正常；1 表示存在 FAIL 或 WARN 的模块。
"""

import importlib
import os
import pkgutil
import shutil
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import nonebot
from nonebot import logger
from nonebot.adapters.onebot.v11 import Adapter

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

ITEM_PACKAGE = "src.plugins.item"
ITEMS_PACKAGE = f"{ITEM_PACKAGE}.items"
FRAMEWORK_MODULE = f"{ITEM_PACKAGE}._framework"

# 模块导入失败的统一捕获集合（沿用项目约定：常量元组规避 BLE001）
_MODULE_ERRORS = (Exception,)

_STATUS_PASS = "PASS"
_STATUS_FAIL = "FAIL"
_STATUS_WARN = "WARN"
_STATUS_SKIP = "SKIP"


@dataclass
class ModuleCheck:
    """单个道具模块的检查结果。"""

    name: str
    """模块文件名（不含 .py 后缀）。"""

    status: str
    """检查状态：PASS / FAIL / WARN / SKIP。"""

    detail: str = ""
    """FAIL / WARN / SKIP 状态的原因或说明。"""

    labels: list[str] = field(default_factory=list)
    """本模块注册道具的显示名（含品质）列表（PASS 状态由归因填写）。"""


def _prepare_environment() -> Path:
    """切换工作目录到项目根并重定向数据目录，返回临时数据目录。

    重定向通过 LOCALSTORE_DATA_DIR 环境变量完成，须在 localstore 模块
    首次导入（加载插件）之前设置才会生效。
    """
    os.chdir(PROJECT_ROOT)
    tmp_dir = Path(tempfile.mkdtemp(prefix="gbpbot_item_register_test_"))
    os.environ["LOCALSTORE_DATA_DIR"] = str(tmp_dir)
    return tmp_dir


def _init_nonebot() -> None:
    """初始化 nonebot 运行环境（不启动服务，仅用于加载插件）。"""
    nonebot.init(driver="~none")
    nonebot.get_driver().register_adapter(Adapter)


def _load_plugins() -> bool:
    """按真实链路加载 duel 与 item 插件，返回是否加载成功。"""
    if nonebot.load_plugin("src.plugins.duel") is None:
        logger.error("duel 插件加载失败（item 依赖其服务），无法继续检查")
        return False
    if nonebot.load_plugin(ITEM_PACKAGE) is None:
        logger.error("item 插件包加载失败，请先修复插件本身的问题")
        return False
    return True


def _list_module_names(items_dir: Path) -> list[str]:
    """列出 items 包下的子模块名（与加载器使用同一种发现方式）。"""
    infos = pkgutil.iter_modules([str(items_dir)])
    return sorted(info.name for info in infos)


def _replay_import(module_name: str) -> str:
    """重放一次模块导入，返回异常描述（未报错时为空字符串）。"""
    try:
        importlib.import_module(module_name)
    except _MODULE_ERRORS as exc:
        return f"{type(exc).__name__}: {exc}"
    return ""


def _attribute_modules(framework: Any, names: list[str]) -> tuple[dict[str, Any], str]:
    """清空注册表后逐个重导模块，归因「模块 -> 注册道具」。

    返回 (归因结果, 保真说明)：归因结果的值为显示名列表，或重导异常描述；
    保真说明为空表示重建的注册表与真实加载一致。无论归因结果如何，注册表
    都会恢复为真实加载的内容。
    """
    original = dict(framework._ITEMS)
    attribution: dict[str, Any] = {}
    fidelity_note = ""
    framework._ITEMS.clear()
    try:
        for name in names:
            module_name = f"{ITEMS_PACKAGE}.{name}"
            sys.modules.pop(module_name, None)
            before = set(framework._ITEMS)
            try:
                importlib.import_module(module_name)
            except _MODULE_ERRORS as exc:
                attribution[name] = f"重导失败：{type(exc).__name__}: {exc}"
                continue
            attribution[name] = [
                f"{item.label}（{framework.quality_label(item.quality)}）"
                for item_id, item in sorted(framework._ITEMS.items())
                if item_id not in before
            ]
        if set(framework._ITEMS) != set(original):
            fidelity_note = "归因重导后的注册表与真实加载不一致，请检查模块导入的确定性"
    finally:
        framework._ITEMS.clear()
        framework._ITEMS.update(original)
    return attribution, fidelity_note


def _check_modules(framework: Any, items_dir: Path) -> list[ModuleCheck]:
    """逐个检查道具模块的导入与注册情况（含归因）。"""
    checks: list[ModuleCheck] = []
    passed_names: list[str] = []
    for name in _list_module_names(items_dir):
        if name.startswith("_"):
            checks.append(
                ModuleCheck(
                    name,
                    _STATUS_SKIP,
                    "以下划线开头的模块会被加载器跳过",
                )
            )
            continue
        module_name = f"{ITEMS_PACKAGE}.{name}"
        if module_name in sys.modules:
            checks.append(ModuleCheck(name, _STATUS_PASS))
            passed_names.append(name)
            continue
        reason = _replay_import(module_name)
        checks.append(
            ModuleCheck(
                name,
                _STATUS_FAIL,
                reason or "模块未被导入，且重放导入未报错（请重试）",
            )
        )
    attribution, fidelity_note = _attribute_modules(framework, passed_names)
    if fidelity_note:
        logger.warning(fidelity_note)
    for check in checks:
        if check.status != _STATUS_PASS:
            continue
        result = attribution.get(check.name)
        if isinstance(result, list) and result:
            check.labels = result
        elif isinstance(result, list):
            check.status = _STATUS_WARN
            check.detail = "导入成功但未注册任何道具，请确认模块中调用了 register_item"
        else:
            check.status = _STATUS_WARN
            check.detail = f"归因重导失败（不影响真实加载判定）：{result}"
    return checks


def _report(checks: list[ModuleCheck], framework: Any) -> int:
    """输出检查报告，返回退出码（存在异常模块时为 1）。"""
    logger.info("========== 检查结果 ==========")
    for check in checks:
        if check.status == _STATUS_PASS:
            logger.info(f"[PASS] {check.name} -> 注册 {'、'.join(check.labels)}")
        elif check.status == _STATUS_SKIP:
            logger.info(f"[SKIP] {check.name} -> {check.detail}")
        else:
            logger.warning(f"[{check.status}] {check.name} -> {check.detail}")
    counter = Counter(check.status for check in checks)
    logger.info(
        f"模块统计：PASS {counter[_STATUS_PASS]}，FAIL {counter[_STATUS_FAIL]}，"
        f"WARN {counter[_STATUS_WARN]}，SKIP {counter[_STATUS_SKIP]}"
    )
    logger.info(f"已注册道具（共 {len(framework._ITEMS)} 件）：")
    for item in framework.iter_items():
        type_text = "、".join(item_type.value for item_type in item.types)
        logger.info(
            f"  {item.label}（{framework.quality_label(item.quality)}、{type_text}）"
        )
    abnormal = counter[_STATUS_FAIL] + counter[_STATUS_WARN]
    if abnormal:
        logger.error(f"存在 {abnormal} 个异常模块，请修复后重新运行本脚本")
        return 1
    logger.info("全部道具模块注册正常")
    return 0


def _run() -> int:
    """初始化 nonebot 并执行完整的道具注册检查。"""
    _init_nonebot()
    logger.info("开始按真实链路加载插件（归因阶段的注册日志会重复出现，属正常现象）")
    if not _load_plugins():
        return 1
    framework = cast("Any", sys.modules[FRAMEWORK_MODULE])
    items_dir = Path(framework.__file__).resolve().parent / "items"
    checks = _check_modules(framework, items_dir)
    return _report(checks, framework)


def main() -> int:
    """脚本入口：准备隔离环境、执行检查、清理临时数据，返回退出码。"""
    tmp_dir = _prepare_environment()
    try:
        exit_code = _run()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
