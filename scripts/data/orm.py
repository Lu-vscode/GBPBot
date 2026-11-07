"""nonebot-plugin-orm 迁移命令入口（等效 `nb orm`）。

服务器上不安装 nb-cli，本模块提供等价入口（在项目根目录执行）：

    .venv/bin/python -m scripts.data.orm upgrade head
    .venv/bin/python -m scripts.data.orm current
    .venv/bin/python -m scripts.data.orm history

参数与 `nb orm` 的子命令完全一致（init / revision / upgrade / downgrade /
check / current / history / stamp 等），只是先完成 NoneBot 初始化与
nonebot_plugin_localstore、nonebot_plugin_orm 两个插件的加载。

upgrade / downgrade / current / history 只依赖 `migrations/` 下的迁移脚本，
不需要模型；而 `revision`（默认即 autogenerate）与 `check` 必须能扫描到全部
Model 子类，因此这两个子命令会额外按 `pyproject.toml` 加载项目插件。
"""

import sys
from pathlib import Path

import nonebot
from sqlalchemy.engine import make_url

# 需要模型已注册的子命令（autogenerate 与模型对比都依赖 Model 元数据）
_MODEL_COMMANDS = frozenset({"revision", "check"})


def _ensure_database_dir() -> None:
    """确保 SQLite 数据库文件所在目录存在（首次部署时该目录往往还不存在）。"""
    url = getattr(nonebot.get_driver().config, "sqlalchemy_database_url", None)
    if not isinstance(url, str) or not url.startswith("sqlite"):
        return
    database = make_url(url).database
    if not database or database == ":memory:":
        return
    Path(database).expanduser().parent.mkdir(parents=True, exist_ok=True)


def _load_project_plugins() -> None:
    """按 pyproject.toml 加载项目插件，使迁移能扫描到全部 Model 子类。"""
    config_file = Path("pyproject.toml")
    if not config_file.is_file():
        print(f"未找到 {config_file.resolve()}")
        print("revision / check 需要模型已注册：请在项目根目录执行本命令。")
        return
    nonebot.load_from_toml(str(config_file))


def main() -> None:
    """初始化 NoneBot 后转交 ORM 的命令行入口。"""
    nonebot.init()
    if set(sys.argv[1:2]) & _MODEL_COMMANDS:
        # 模型需要已注册：按 pyproject.toml 加载全部插件（含 localstore / orm）
        _load_project_plugins()
    else:
        # 迁移命令只需 ORM 本身，不必导入业务插件
        nonebot.load_plugin("nonebot_plugin_localstore")
        nonebot.load_plugin("nonebot_plugin_orm")
    _ensure_database_dir()

    from nonebot_plugin_orm.__main__ import main as orm_main

    # ORM 入口是 click 命令组：参数需作为整个列表传入（而不是逐个位置参数）
    orm_main(sys.argv[1:] or None)


if __name__ == "__main__":
    main()
