# 持久存储迁移 nonebot-plugin-orm · 详细实施方案

> 依据：用户要求"将 src 中各插件/插件包的持久存储方案重构为基于 nonebot-plugin-orm"。
> 本文档是本次实现的完整设计记录，供审核后执行；未在需求中明确、由本方案决定的细节
> 均记录在此，供后续维护参考。
>
> 注：本文记录的是 ORM 迁移当时的文件布局。此后 duel 与 item 已按职责拆分子包
> （见 `docs/plan/item.md` §3）：文中 `duel/_models.py` → `duel/models.py`，
> `item/_models.py` → `item/storage/models.py`，`item/_store.py` → `item/storage/`，
> `item/_framework.py` → `item/core/`；测试脚本目录此后亦由 `tests/items/` 更名为 `tests/item/`。

## 1. 背景与目标

当前 `src/plugins` 下有三处持久存储，全部为"全量读入内存 + 每次变更全量重写 JSON"：

| 数据 | 文件 | 实现位置 | 写入方式 |
| --- | --- | --- | --- |
| 决斗分数 | `data/duel/scores.json` | `duel/_scores.py` | 原子写（`_shared/jsonfile.py`） |
| 道具库存 | `data/item/inventory.json` | `item/_storage.py` → `item/_framework.py` | 原子写 |
| 道具状态 | `data/item/states.json` | `item/_storage.py` → `item/_framework.py` | 原子写 |
| 签到记录 | `data/sign/sign_ins.json` | `sign.py` | 裸 `write_text`（无原子写、无 fsync） |

外加运维 CLI `scripts/data/edit`（生产别名 `gbpedit`）直接读写上述 JSON。

已知硬伤：签到写盘不可靠且存在"记录丢失后重复签到刷分"的窗口；无跨实体事务（签到＝写
签到 + 加分数 + 发道具，三件事无法原子化）；每次变更全量重写，随数据增长阻塞事件循环；
格式演进只能靠"加载时静默丢弃"。

目标：迁移到 `nonebot-plugin-orm`（SQLAlchemy 2 + Alembic）＋ SQLite（aiosqlite），以
"表 + 事务 + 约束"取代"JSON 全量重写"，并同步改造迁移脚本、运维 CLI 与文档。

## 2. 已确认的关键决策

| # | 问题 | 决策 |
| --- | --- | --- |
| D1 | 数据访问层形态 | **全面异步化**：库存/状态/分数/签到 API 全部改 `async`，同步钩子（check/consume/transferable/durability_text）一并改异步；不保留内存全量缓存 |
| D2 | 落地节奏 | **一次性全部改完**：依赖与建表、duel、item、sign、迁移脚本、运维 CLI、文档同一批完成 |
| D3 | 迁移后原 JSON 处置 | **改名归档**：迁移成功后原文件改名 `*.migrated-<时间戳>`，机器人只读库 |
| D4 | 运维 CLI（`gbpedit`） | **同步改造为操作 DB**：`scores/inventory/states` 子命令改 SQL，`verify`、`backup list/restore` 保留 |
| D5 | 数据库文件位置 | 显式配置 `SQLALCHEMY_DATABASE_URL=sqlite+aiosqlite:///data/nonebot_plugin_orm/db.sqlite3`（相对 cwd，与 `LOCALSTORE_USE_CWD=true` 的既有约定一致），恰好与 ORM 默认位置一致。**目录名必须用下划线**：连字符的 `data/nonebot-plugin-orm` 是 ORM 的弃用路径，导入时会自动改名，配成它会因目录消失而报 `unable to open database file` |
| D6 | 表名 | 统一加 `gbp_` 前缀（`gbp_duel_score` 等），避免与将来共用默认 bind 的第三方插件表冲突 |
| D7 | 签到记录的"当日唯一" | 由 `UNIQUE(group_id, user_id, date)` 保证；重复签到靠插入冲突判定，不再依赖 Python 同步段 |
| D8 | 复合操作的并发安全 | 引入**按群号**的 `asyncio.Lock`（`group_lock(group_id)`），包住"校验 → 消耗 → 效果登记"关键段；不按成员分桶（跨成员操作需双锁排序，易错） |
| D9 | 模型文件位置 | 模型定义在各自插件内（`duel/_models.py`、`item/_models.py`、`sign.py` 顶部），由插件包导入保证启动前注册；`sign` 保持单文件不拆包 |
| D10 | 幂等与回滚 | 迁移脚本幂等（upsert/ignore）；回滚方式＝改回 JSON 名 + 弃用 DB 文件 + 回退代码，写入运维文档 |

## 3. 目标架构

```
src/plugins/
├── _shared/
│   ├── services/        # 契约改为 async（破坏性变更，见 §5.4）
│   └── jsonfile.py      # 本次删除（迁移后无 JSON 写入）
├── duel/
│   ├── _models.py       # 新增：gbp_duel_score
│   ├── _scores.py       # 重写：异步 SQL 读写，删除内存 _scores
│   └── _flow.py         # 结算改为一次批量写入
├── item/
│   ├── _models.py       # 新增：gbp_item_inventory / gbp_item_state
│   ├── _store.py        # 新增：异步读写原语、事务辅助、group_lock
│   ├── _framework.py    # API 全异步化，新增 async 钩子类型
│   ├── _durability.py   # 耐久逻辑异步化
│   ├── _storage.py      # 删除
│   └── items/*.py       # 50 个道具模块：调用处加 await、钩子改 async
└── sign.py              # 顶部定义 gbp_sign_record；签到落库走唯一约束

scripts/data/
├── edit/                # 运维 CLI：改为操作 DB
└── migrate_orm.py       # 新增：一次性 JSON → DB 迁移（标准库 sqlite3）

migrations/              # 新增：Alembic 迁移目录（随代码分发；不建 alembic.ini）
scripts/data/orm.py      # 新增：ORM 命令入口（升级 / 新建 revision），转调 ORM 的 Python API
```

### 3.1 依赖与插件声明

`pyproject.toml`：

- `[project].dependencies` 增加 `nonebot-plugin-orm[sqlite]>=0.8.4`（`[sqlite]` 扩展提供 aiosqlite 后端）
- `[tool.nonebot.plugins]` 增加 `nonebot-plugin-orm = ["nonebot_plugin_orm"]`
- 各插件在模块顶部 `require("nonebot_plugin_orm")` 后再导入 `Model` / `get_session`

`nonebot-plugin-orm` 内部已 `require("nonebot_plugin_localstore")`，localstore 依赖保留。

### 3.2 环境配置

| 变量 | `.env` / `.env.dev` | `.env.prod` |
| --- | --- | --- |
| `SQLALCHEMY_DATABASE_URL` | `sqlite+aiosqlite:///data/nonebot_plugin_orm/db.sqlite3`（写在 `.env`，`.env.dev` / `.env.prod` 继承） | 同左 |
| `ALEMBIC_STARTUP_CHECK` | `.env.dev` 设 `false`（启动自动同步表结构，免手动迁移）；`.env` 不设 | `true`（默认值，未更新则启动失败，强制先迁移） |

生产必须显式迁移的原因：`ALEMBIC_STARTUP_CHECK=true` 且库未升级时，ORM 会 `click.confirm`
询问是否升级；systemd 无 TTY 环境下该交互会失败并中止启动。因此部署/升级流程必须包含
一次显式迁移命令（见 §8、§9）。

### 3.3 表结构

```sql
CREATE TABLE gbp_duel_score (
    group_id INTEGER NOT NULL,
    user_id  INTEGER NOT NULL,
    name     VARCHAR(64) NOT NULL DEFAULT '',
    score    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (group_id, user_id)
);

CREATE TABLE gbp_item_inventory (
    group_id INTEGER NOT NULL,
    user_id  INTEGER NOT NULL,
    item_id  VARCHAR(3) NOT NULL,
    count    INTEGER NOT NULL,
    PRIMARY KEY (group_id, user_id, item_id),
    CONSTRAINT ck_gbp_item_inventory_count CHECK (count > 0)
);

CREATE TABLE gbp_item_state (
    group_id   INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    state_key  VARCHAR(64) NOT NULL,
    item_id    VARCHAR(3) NOT NULL,
    expires_at FLOAT,                    -- NULL 表示永久
    data       JSON NOT NULL,            -- 状态附加数据
    PRIMARY KEY (group_id, user_id, state_key)
);
CREATE INDEX ix_gbp_item_state_expires ON gbp_item_state (expires_at);

CREATE TABLE gbp_sign_record (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL,
    user_id  INTEGER NOT NULL,
    date     VARCHAR(10) NOT NULL,       -- YYYY-MM-DD（本地时区）
    item_id  VARCHAR(3) NOT NULL DEFAULT '',
    CONSTRAINT uq_gbp_sign_record_day UNIQUE (group_id, user_id, date)
);
```

设计要点：

- 状态以 `(群, 成员, 状态键)` 为主键，天然实现"同键覆盖"，取代原来的列表内去重逻辑
- 库存数量约束 `count > 0` 下推数据库，删除"数量为 0 时删条目"的多处手工判空
- 签到不存 `name`/`count`：昵称不参与展示，累计次数由 `COUNT(*)` 得出（语义与现状一致）
- 模型类名：`DuelScore`、`ItemInventory`、`ItemStateRow`、`SignRecord`。**`ItemStateRow` 不得写作
  `ItemState`**——后者是道具框架的值对象，50 个道具模块依赖该名字

### 3.4 Alembic 迁移（实际落地形态）

- 不建 `alembic.ini`：`nonebot-plugin-orm` 在内存中构造 `AlembicConfig`，脚本目录固定为仓库根的
  `migrations/`（`migrations/{env.py,script.py.mako,versions/}`，随代码包分发并纳入版本控制）
- 统一入口 `scripts/data/orm.py`（替代 `python -m alembic`）：先 `nonebot.init()` 并加载
  localstore / orm 插件，再转调 `nonebot_plugin_orm.__main__.main`；首次运行会先创建数据库
  文件所在目录（否则首次部署报 `unable to open database file`）
  - 建表 / 升级：`python -m scripts.data.orm upgrade head`
  - 查看版本：`python -m scripts.data.orm current`（应为 `68117ac7adf9 (head)`）
  - 比对模型与库：`python -m scripts.data.orm check`（无差异时输出"没有检测到新的升级操作"）
  - 新增 revision：`python -m scripts.data.orm revision -m "说明"`（默认即 autogenerate，
    没有 `--autogenerate` 开关）；`check` / `revision` 会先按 `pyproject.toml` 加载全部
    项目插件（autogenerate 必须扫描 Model 子类），其余子命令只加载 localstore / orm
- 首个 revision 建上述 4 张表，文件为 `migrations/versions/68117ac7adf9_init_schema.py`
  （revision id 与文件名统一用 ASCII，避免跨平台工具问题）；此后模型变更必须新增 revision

## 4. 数据访问形态（D1 的落地约束）

改造后**不存在**内存全量缓存，任何读写都是数据库调用，因此：

1. 所有相关 API 为 `async def`，调用方必须 `await`
2. 单次写操作 = 单事务（`async with get_session() as session, session.begin(): ...`）
3. 复合操作 = 一个事务内完成多步，失败整体回滚：
   - `exchange_items`（双方各扣一件、各得一件）
   - `remove_items`（批量扣减，任一不足则不修改）
   - `add_states`（整批共用一个过期时刻）
   - `update_scores`（决斗双方结算一次写入）
4. 需要"先校验后修改"的流程由 `group_lock(group_id)` 串行化，取代原来的
   "校验与消耗同处同步段"假设

## 5. 代码改造清单

### 5.1 `duel`

`_models.py`（新增）：`DuelScore` 模型。

`_scores.py`（重写）：

- 删除 `_load_scores` / `_save_scores` / `_scores` / 启动统计日志 / 旧格式兼容（旧格式由迁移脚本处理）
- `update_score(group_id, user_id, name, delta) -> None` → `async def`：
  先经拦截器链改写 `delta`，再以 upsert 写入（`INSERT ... ON CONFLICT (group_id, user_id)
  DO UPDATE SET score = score + excluded_delta, name = excluded_name`），末态分数用同事务
  `SELECT` 读回供 `add_score` 返回
- `update_scores(group_id, entries)` → `async def`，单事务内按顺序应用（每条仍逐个过拦截器）
- `rank_entries(group_id, *, positive)` → `async def`，改 `ORDER BY score DESC, user_id ASC`
  （低分榜 `ORDER BY score ASC, user_id ASC`）；非零过滤下沉到 SQL
- `total_score(group_id)` → `async def`，`SELECT COALESCE(SUM(score), 0)`
- `_apply_interceptors` → `async def`；`ScoreInterceptor` 类型改为 `Awaitable[int]`；
  单个拦截器异常仍只记日志、沿用上一步结果
- `_DuelScoreService` 各方法改 `async`（`add_score` 返回写后分数，`rank_entries` 拼接逻辑不变）

`_flow.py`：结算处两次 `update_score` 合并为一次 `await update_scores(group_id, [...])`
（顺带消除"赢家加分成功、输家扣分失败"的中间态）。

`__init__.py`：排行榜与分数查询（`rank_entries`/`total_score`/`get_score`）加 `await`。

### 5.2 `item`

`_models.py`（新增）：`ItemInventory`、`ItemStateRow`。

`_store.py`（新增，道具包内部）：

- `group_lock(group_id)`：按群号缓存的 `asyncio.Lock`（`async with` 上下文管理器）
- 库存原语：`load_item_counts`、`set_item_count`、`add_to_inventory`、`take_from_inventory`、
  `read_inventory_items`（供 `/item.list`）
- 状态原语：`load_states`、`upsert_state`、`delete_states`、`select_expired_states`、
  `select_state_holders`
- 所有函数以"调用方已开启事务"或"自开事务"两种形态二选一并写清 docstring，禁止半开半闭

`_framework.py`（全面异步化）：

| 变更 | 函数 |
| --- | --- |
| 改 `async` | `get_item_count`、`transferable_count`、`add_item`、`add_items`、`remove_item`、`remove_items`、`exchange_items`、`list_user_items` |
| 改 `async` | `add_state`、`add_states`、`get_state`、`remove_state`、`remove_states`、`list_states`、`list_state_holders`、`set_state_data`、`purge_expired_states` |
| 改 `async` | `detail_text`、`get_user_score`、`get_lowest_score_user`、`get_highest_score_user`、`get_rank_entries`、`add_user_score`、`add_user_scores`、`sign_bonus` |
| 钩子类型改 `Awaitable` | `ItemBehavior.check`（`-> Awaitable[str \| None]`）、`consume`（`-> Awaitable[bool]`）、`transferable`（`-> Awaitable[int]`）、`durability_text`（`-> Awaitable[str]`） |
| 方法改 `async` | `ItemUseContext.add_score` |
| 类型改 `Awaitable[int]` | `SignBonusProvider` |

- `run_item_use`：`error = await behavior.check(context)`、`consumed = await behavior.consume(context)`、
  默认分支 `await remove_item(...)`；删除"不可达：校验与消耗同处同步段"的过时注释
- `ItemState` 数据类与 `state_label`、注册表相关函数保持同步（纯内存操作，无 IO）
- `_states_version` 计数保留（`worm.py` 依赖），在写操作后递增
- 删除导入期的 `load_inventory()` / `load_states()` 与启动统计日志（改由 CLI `verify` 提供统计）

`_durability.py`：`_remaining_durabilities`、`_store`、`_consume`、`_transferable_count`、
`_durability_text`、`move_damaged_instance` 全部改 `async`；`durable_behavior` 签名不变
（`partial` 包装 async 函数即可）。

`items/*.py`（50 个模块，机械改造）：所有库存/状态/分数调用加 `await`；`check` / `consume` /
`transferable` / `durability_text` 钩子声明改为 `async def`。改造以 `ruff`（F 规则）与
`pyright` 兜底：漏改 `await` 会报 "coroutine was never awaited" 类类型错误。

`__init__.py`：

- `_ItemService.grant_random` → `async def`（`await add_item(...)`）
- `_SignScoreBonusService.extra_score` → `async def`
- `/item.use`、`/item.craft`、`/item.exchange*`、`/item.get` 的流程内"校验 → 消耗 → 效果"全部
  包在 `async with group_lock(group_id):` 内；消息发送放在锁外或锁内均可（本次放锁内，
  与现状"同步段内完成判定"一致）
- `_cleanup_items`：`expired = await purge_expired_states()`

### 5.3 `sign`

保持单文件，顶部新增 `class SignRecord(Model)`（表 `gbp_sign_record`）。

- 删除 `_SIGN_FILE`、`_normalize_entry/_normalize_member/_normalize_group/_load_signs/_save_signs`、
  `_signs`、`_migrated` 及旧格式迁移逻辑（旧格式由 `scripts/data/migrate_orm.py` 处理）
- `_try_sign` → `async def`，顺序固定为：
  1. 插入签到记录（`item_id=""`），唯一冲突 → 返回"今天已签到"
  2. `await _score_bonus_service.extra_score(...)`
  3. `await _score_service.add_score(...)`
  4. `await _item_service.grant_random(...)`
  5. 回填本次记录的 `item_id`
- 日志中的"累计 N 次"改为 `SELECT COUNT(*)` 查询结果
- 中途失败时保留"当天已签到但未发奖"的状态（比重复领奖安全），异常只记错误日志
- 依赖声明不变：`require("duel")`、`require("item")`，另加 `require("nonebot_plugin_orm")`

### 5.4 `_shared/services/`（破坏性契约变更）

| 契约 | 变更 |
| --- | --- |
| `DuelScoreService.add_score` / `get_score` / `add_scores` / `lowest_member` / `highest_member` / `rank_entries` | 全部 `async def` |
| `ScoreInterceptor` | `Callable[[int, int, str, int], Awaitable[int]]` |
| `SignScoreBonusService.extra_score` | `async def`（docstring 中的"同步段内调用"改为"在签到同一流程内调用，不得落盘或发消息"） |
| `ItemService.grant_random` | `async def` |

`DuelScoreInterceptService.add_interceptor` 保持同步（仅登记，无 IO），但其 docstring 需说明
拦截器本身为异步回调。

### 5.5 删除

- `src/plugins/item/_storage.py`
- `src/plugins/_shared/jsonfile.py`（迁移后无 JSON 写入方；迁移脚本用标准库 `json` +
  `scripts/data/edit/_pipeline.load_json_if_exists`）

## 6. 数据迁移脚本 `scripts/data/migrate_orm.py`

一次性、幂等、可预演：

```
用法（项目根目录）：.venv/bin/python -m scripts.data.migrate_orm [--data-dir PATH] [--db PATH] [--dry-run] [-y]
```

流程：

1. 复用 `scripts/data/edit/_pipeline` 的 `service_state` / `enforce_service_gate`（运行中拒绝）、
   `confirm`、`data_lock`、`make_backup_set`（先备份 4 个 JSON）
2. 用标准库 `sqlite3` 连接目标库；**先校验 4 张表存在**，否则提示先执行迁移
   （`python -m scripts.data.orm upgrade head`）后退出（退出码 2）
3. 读取并校验 JSON（复用 `_scores.normalize` / `_inventory.normalize` / `_states.normalize`；
   `sign_ins.json` 的 normalize 在脚本内实现，含旧格式 → 仅保留每人最新一条）
4. 单个事务写入：
   - 分数/库存/状态 `INSERT ... ON CONFLICT (...) DO UPDATE SET ...`
   - 签到 `INSERT OR IGNORE`
5. 行数核对（每表写入数与 JSON 记录数一致），打印摘要
6. 成功且非 `--dry-run` 时把 4 个 JSON 改名为 `<原名>.migrated-<YYYYmmdd-HHMMSS>`
7. 输出回滚提示（`mv` 改名 + 删除 db 文件 + 回退代码）

脚本不 import nonebot / 插件代码（与现有 CLI 约定一致），表结构变更时必须同步维护 DDL 常量。

## 7. 运维 CLI 改造（`scripts/data/edit`）

`_pipeline.py`：

- 新增 `db_path(ctx)`（`ctx.data_dir / "nonebot_plugin_orm" / "db.sqlite3"`）与
  `connect_db(ctx)`（`sqlite3.connect`，`row_factory=sqlite3.Row`，`PRAGMA foreign_keys=ON`）
- `ChangePlan`（整文件 JSON）→ `DbChangePlan`：`label` + `changes` + `apply(conn)`，由
  `execute_change` 在 `BEGIN IMMEDIATE` 事务内执行，异常整体回滚
- 备份改为数据库快照：`VACUUM INTO '<备份集>/nonebot_plugin_orm/db.sqlite3'`（服务已停止，
  仍需避开 `-wal`/`-shm` 残留）；`backup list/restore` 以**整库文件**为粒度
- 删除 `write_json_atomic`、`load_json_if_exists` 之外的 JSON 写入路径（读取函数保留给迁移脚本）

数据模块：

| 模块 | 改造 |
| --- | --- |
| `_scores.py` | `normalize` → `validate(conn)`；`get/list/set/add/del` 改 SQL；`summary` 用 `COUNT(*)` |
| `_inventory.py` | 同上；`set/add/del` 按 `(group, user, item_id)` 定位，`count<=0` 视为删除 |
| `_states.py` | 同上；状态以 `(group, user, state_key)` 定位，`data` 为 JSON 文本 |
| `_signs.py`（新增） | 只读：`verify` + `list -g 群号`（成员、签到天数、最近签到日），不提供改写 |
| `__main__.py` | `_SUPPORTED_FILES` → `_SUPPORTED_TABLES`；`backup restore` 去掉 `--file` 参数 |

命令行为保持兼容：`gbpedit scores get/list/set/add/del`、`inventory ...`、`states ...` 的参数与
输出格式不变；退出码 0/1/2/3 语义不变。

## 8. 文档与配置更新

| 文件 | 更新内容 |
| --- | --- |
| `docs/Help/operations.md` | §4 备份与恢复改为 DB 文件快照；§4.1 命令示例按新 CLI 重写；新增"切换 ORM 后首次迁移"步骤与回滚步骤 |
| `docs/Help/deployment.md` | 依赖列表增加 `"nonebot-plugin-orm[sqlite]>=0.8.4"`；§6 新增"首次部署执行 `upgrade head`"；§10.3 打包补 `migrations`、升级流程改"解包 → 同步依赖 → 停服 → `upgrade head` → 启服"；§10.1/10.6/10.8/11 同步改库 |
| `docs/plan/item.md` | §4 数据与存储改写为 DB 表；§5.2/§5.5 的钩子与状态 API 签名改 `async`；§6 中"同步段"措辞改为"群锁 / 事务内" |
| `.env`、`.env.dev`、`.env.prod` | `.env` 增加 `SQLALCHEMY_DATABASE_URL`（下划线路径）；`.env.dev` 加 `ALEMBIC_STARTUP_CHECK=false`、`.env.prod` 加 `ALEMBIC_STARTUP_CHECK=true` |
| `.gitignore` | 增加 `*.db`、`*.db-wal`、`*.db-shm`、`*.migrated-*`（`data/` 整体已忽略，此处为防误提交） |
| `tests/items/test_item_register.py` | 加载顺序改为先 `nonebot.load_plugin("nonebot_plugin_orm")` 再加载 duel/item |

## 9. 验证计划

1. **静态检查**：`ruff check`、`ruff format --check`、`pyright`（异步化漏改由这两项兜底）
2. **临时验证脚本** `tests/_tmp/orm_probe.py`（按开发规范独立编写、验证后删除）：
   - 在 `import nonebot` 之前设 `LOCALSTORE_DATA_DIR` 到 `tempfile.mkdtemp()`，结束精确清理；
     对 `data/` 做前后快照确认未写入（含 `data/prod` 真实数据）
   - 建表：脚本内跑 `upgrade head`（等价于 `python -m scripts.data.orm upgrade head`），
     或依赖开发模式 `ALEMBIC_STARTUP_CHECK=false` 自动同步
   - 用例：库存增删/批量扣减失败不修改/交换原子性；状态新增覆盖/查询/附加数据/过期清理；
     分数 upsert、榜单排序、批量结算、拦截器抵消；签到同日两次只成功一次且 `COUNT(*)=1`；
     `group_lock` 下并发使用道具不超发
   - 迁移脚本：构造 4 个 JSON（含 sign 旧格式与脏记录）→ 迁移 → 行数核对 → 归档 →
     二次运行幂等；`--dry-run` 不写库不归档
   - CLI：`verify`、`scores get/list/set/add/del`、`inventory`、`states` 各命令冒烟
3. **不执行**：压力测试（`legacy/stress`，按开发规范冻结）；`tests/items/test_item_register.py`
   之外的仓库测试
4. **生产验收**（由用户在服务器执行，开发期不触碰 `data/prod`）：
   停服 → 打包上传 → `upgrade head` → `python -m scripts.data.migrate_orm`（预演 + 执行）→
   启服 → 抽查分数/背包/签到

## 10. 风险与应对

| 风险 | 影响 | 应对 |
| --- | --- | --- |
| 迁移命令在服务器（无 nb-cli）不可用 | 生产无法升级库 | 已采用 `scripts/data/orm.py` 包装 ORM 的 Python API（不依赖 nb-cli 与 alembic.ini），已在本地实测升级、查看版本 |
| Python 3.14.7 与 sqlalchemy/aiosqlite 兼容性未知 | 无法安装或运行 | 执行第一步就安装并跑通最小用例；不通过则报告并与你确认（不擅自降级 venv） |
| 240 处调用点的异步化遗漏 | 运行时协程未等待、逻辑失效 | `pyright` + `ruff` 全量检查；道具模块机械改造后逐文件核对 |
| 并发语义变化（校验与消耗不再处于同步段） | 同成员并发使用可超发 | `group_lock` 串行化复合流程；写操作单事务 |
| 迁移中途失败造成"半迁移" | 数据不一致 | 单事务写入 + 迁移前 JSON 备份集 + 行数核对；失败不改名归档，可重跑 |
| Alembic 迁移目录未随包分发 | 服务器无表结构 | `migrations/` 纳入仓库与部署包，部署文档列出 |

## 11. 执行顺序

1. 安装依赖，验证 Python 3.14 兼容性；落地 `migrations/`（无 `alembic.ini`）与 `scripts/data/orm.py`，生成首个 revision；
   实测迁移命令形态
2. `pyproject.toml`、`.env*`、`.gitignore` 配置落地
3. `duel`：`_models.py` + `_scores.py` 重写 + `_flow.py`/`__init__.py` 调用点
4. `_shared/services/` 契约改异步（此步起 item/sign 编译期即暴露待改点）
5. `item`：`_models.py` + `_store.py` + `_framework.py` + `_durability.py` + `__init__.py`，
   再逐个改 `items/*.py`
6. `sign.py`：模型 + 唯一约束签到流程
7. 删除 `item/_storage.py`、`_shared/jsonfile.py`
8. `scripts/data/migrate_orm.py` 与 CLI 改造
9. 临时验证脚本跑通 + 静态检查全绿
10. 文档更新（operations / deployment / plan/item）
11. 交付：报告改动清单与生产迁移步骤，等你在服务器执行
