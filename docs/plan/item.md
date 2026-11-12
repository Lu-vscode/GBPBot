# 道具系统与 duel 重构 · 实现设计文档

> 依据：`docs/design/item.md`（已由用户明确要求按该文档开发）。
> 本文档由 agent 编写，是本次实现（item 插件包 + duel 深度重构）的完整设计记录；
> 未在 `docs/design/item.md` 中明确、由本次实现决定的细节均记录在此，供后续维护参考。

## 1. 背景与目标

- 新增道具插件包（`src/plugins/item/`）：群成员可通过签到获取道具，查看、合成、交换、使用道具；道具以"机制优先"设计，本次仅实现设计文档给出的两个示例道具（111「+1」、110「-1」）。
- 深度重构 `src/plugins/duel.py`：在决斗各环节（发起、接受、拒绝、出拳、结算、超时）暴露尽可能多的跨插件服务接口，供道具等插件灵活使用；**所有现有用户可见行为与文案保持不变**。
- 跨插件交互全部经 `src/plugins/_shared/`（服务注册中心 + 共享消息工具），不互相导入。

## 2. 已确认的关键决策

| # | 问题 | 决策 |
| --- | --- | --- |
| D1 | 抽中品质池为空（当前白色及以上池均为空）如何兜底 | **不兜底**：直接发送"抽取失败"消息，并在日志中记 warning。开发出足够道具前不更新生产环境，故生产不会遇到该状态 |
| D2 | 签到时获得道具如何提示 | **并入签到成功文案**：`SIGN_TEXT_SUCCESS` 增加 `{item}` 占位符（形如"111 +1"）；抽取失败时使用新的 `SIGN_TEXT_SUCCESS_NO_ITEM` 文案 |
| D3 | 交换"不能重复向同一人发起"的范围 | **双方之间仅一笔**：双方之间只要存在未完成交换（不论方向），都不能再发起新交换 |
| D4 | 合成抽中空池时材料是否消耗 | 不消耗：合成先抽结果、再消耗材料；抽不到任何结果时发送"合成失败"消息（材料保留）并记 warning |
| D5 | 交换接受时道具已不存在 | 发起方道具已失：交换取消并告知；接受方道具已失：保留交换（可再次尝试接受或拒绝） |
| D6 | 触发失败的日志级别 | 空池导致的抽取失败记 warning（抽取概率总和不为 1 时归一化，不存在"空抽"） |

## 3. 总体架构

```
src/plugins/
├── _shared/
│   ├── services/     # 跨插件服务契约与注册中心（本次大幅扩展）：registry.py 注册中心、
│   │                 #   duel/ 决斗契约子包（score/state/event/gesture/action）、
│   │                 #   item.py 道具发放、sign.py 签到加成
│   ├── config/       # 新增：跨插件共享配置（model/parse/bot_list/nickname/nickname_guard）
│   └── onebot/       # 新增：共享工具（messaging.py 群聊消息发送、naming.py 群昵称获取/截断）
├── duel/             # 重构（已拆包为包插件）：按职责分为 config（配置与文案）、
│                     #   core（值对象、存储、手势、判定）、flow（结算、机器人出手）、
│                     #   messaging（发送、延迟跟发）、services（对外服务）、
│                     #   commands（六个指令）与 runtime.py、limit.py、models.py
├── item/             # 新增：道具插件包（NoneBot 包插件，模块名 src.plugins.item）
│   ├── __init__.py   # 主模块：插件元数据、配置注入、服务实例注册、启动日志
│   ├── config/       # 配置：Config 模型（model.py）与解析后的抽取/合成概率（resolve.py）
│   ├── storage/      # 存储层：ORM 模型、群锁、库存与状态读写原语
│   ├── core/         # 道具核心：定义与注册表、库存/状态 API、使用流程、抽取、合成、耐久
│   ├── commands/     # 查看、合成、使用、测试获取指令与用法文案
│   ├── exchange/     # 交换：请求内存队列（queue.py）与三条交换指令（commands.py）
│   ├── services.py   # ItemService / SignScoreBonusService 的实现与注册
│   ├── messaging.py  # 指令与交换共用：文本发送、@ 解析、群昵称截断
│   ├── cleanup.py    # 过期状态与过期交换请求的定时清理
│   └── items/
│       ├── __init__.py  # 自动发现并导入本包下所有道具模块
│       ├── plus_one.py  # 道具 111「+1」
│       └── minus_one.py # 道具 110「-1」
└── sign.py           # 更新：签到发放道具、记录道具编号、文案变体
```

包内依赖方向单向：`config → core → commands/exchange/services/cleanup → __init__`；
`storage` 与 `core` 内部子模块互相导入具体模块、不经包入口转一手；道具模块
（`items/`）只从 `core` 包导入。

依赖关系（加载期 `require`）：`item` require `duel`；`sign` require `duel`、`item`。
子模块（`item.core` 等）由包内普通导入加载，不会被 NoneBot 当作独立插件
（扫描为非递归、仅顶层）；`_shared` 以下划线开头不会被加载。

## 4. 数据与存储

持久数据由 nonebot-plugin-orm 管理，全部落在 SQLite 数据库
`data/nonebot_plugin_orm/db.sqlite3`（路径由 `.env` 的 `SQLALCHEMY_DATABASE_URL` 指定）：
旧版 `data/item/inventory.json`、`data/item/states.json` 已废弃，由
`scripts/data/migrate_orm.py` 一次性迁移（旧文件改名归档为 `*.migrated-<时间戳>`）。
模型定义在 `item/storage/models.py`，表名统一加 `gbp_` 前缀：

- `gbp_item_inventory`：道具库存，主键 `(group_id, user_id, item_id)`
  - `item_id` 为三位数字字符串（`String(3)`）；`count` 为正整数，约束
    `CHECK (count > 0)` 下推数据库，数量归零即删除条目
- `gbp_item_state`：道具产生的状态（Buff/永久型等），主键 `(group_id, user_id, state_key)`
  - 同一成员同键状态重复添加时直接覆盖（UPSERT），不再需要“加载时校验结构、跳过无效条目”
  - `expires_at` 为 Unix 时间戳（墙钟，重启不丢失）的 `Float`，`NULL` 表示永久；
    过期清理依赖索引 `ix_gbp_item_state_expires`
  - `data` 为道具模块自定义的附加数据（JSON 列，须可 JSON 序列化）
- 读写全部收敛在 `item/storage/`（`inventory.py` 库存、`states.py` 状态、`lock.py` 群锁）：
  每个函数自行开事务
  （`async with get_session() as session, session.begin()`），复合操作（批量扣减、
  双方交换、整批写入状态）在单事务内完成，任一步不满足条件整体回滚；
  “先校验、后修改”的流程（使用、交换、合成）由 `group_lock(group_id)` 按群串行化，
  取代原先“校验与消耗同处同步段”的假设
- 过期状态在读取时跳过但不删除，统一由定时任务 `purge_expired_states()` 在事务内
  删除并返回到期列表，再分发到期效果（同一批状态不会被并发清理重复触发）

内存数据（做好内存管理）：

- 待处理交换 `_exchanges: list[_PendingExchange]`：有效期 10 分钟，**仅存内存**，过期静默删除；
  定时任务（30 秒）与指令入口双重清理
- 状态不再常驻内存：经 `storage/states.py` 按需读写数据库（热点是单成员单状态，开销可忽）

## 5. 道具框架（`item/core/`）

### 5.1 品质、分类与标签

- `Quality(IntEnum)`：`BLACK_CURSE=-1, GRAY=0, WHITE=1, GREEN=2, BLUE=3, PURPLE=4, GOLD=5`
  （数值用于合成 ±1/±2 运算与"品质由高到低"排序）
- 品质显示名：黑色诅咒 / 灰色垃圾 / 白色普通 / 绿色精良 / 蓝色稀有 / 紫色史诗 / 金色传说
- `ItemKind(Enum)`（展示分类，单值、互斥，用于详情与分类统计）：消耗型 / 诅咒型 / Buff型 /
  永久型 / 可选型 / 复合型 / 耐久型；诅咒型可由黑色诅咒品质派生，该类道具可省略 `kind`；
  耐久型专指效果仅为消耗耐久的道具，耐久与其它机制叠加的仍记为复合型
- `ItemTag(StrEnum)`（功能标签，可多值，只记录"具备什么机制"，用于 `has_tag`/`items_with_tag`
  查询）：复制型
- 分类与标签是两条互不相交的轴：分类回答"它是什么"，标签回答"它具备什么机制"，
  展示与统计只看分类，跨道具查询只看标签

### 5.2 道具定义与注册

```python
@dataclass(frozen=True)
class ItemBehavior:  # 行为钩子（可选；纯展示道具不需要）
    check: Callable[[ItemUseContext], Awaitable[str | None]] | None = None  # 返回错误文案即不可用
    use: Callable[[ItemUseContext], Awaitable[None]] | None = None
    consume: Callable[[ItemUseContext], Awaitable[bool]] | None = None  # 自定义消耗方式（耐久型等）
    transferable: Callable[[int, int, str], Awaitable[int]] | None = None  # 可交换副本数（群、成员、道具）
    expire: Callable[[ItemStateExpireContext], Awaitable[None]] | None = None  # 状态到期
    durability_text: Callable[[int, int, str], Awaitable[str]] | None = None  # 动态耐久（群、成员、道具）


@dataclass(frozen=True)
class ItemDefinition:
    item_id: str  # 三位数字字符串，如 "111"；注册时必须提供、不允许留空
    name: str  # 名称，如 "+1"
    quality: Quality
    description: str  # 介绍
    effect: str  # 功能
    kind: ItemKind | None = None  # 展示分类；None 表示由品质派生（仅黑色诅咒）
    tags: Collection[ItemTag] = ()  # 功能标签（注册时归一化为 frozenset）
    condition: str | None = None  # 使用条件（展示）；None 显示“无”
    timing: str | None = None  # 使用时机（展示）；None 显示“任意”
    note: str = ""  # 备注（空则不显示备注行）
    durability: str = "1/1"  # 耐久展示文本
    accepts_args: bool = False  # /item.use 是否接受额外参数
    labels: tuple[tuple[str, str], ...] = ()  # 本道具产生状态的显示名
    behavior: ItemBehavior | None = None  # 行为钩子
```

注册规则（`register_item(definition: ItemDefinition) -> ItemDefinition` 注册单件；
`register_items(*definitions)` 批量注册、等价于逐件注册。导入期执行，发现开发错误时抛 `ValueError`）：

- 编号必须为三位数字字符串（000~999）且不允许留空：编号由道具开发时指定，
  设计未指定时由开发 agent 在开发时从 100 起向上寻找可用编号并写入代码（000~099 保留）
- 编号留空/重复、黑/咒不一致（品质为黑色诅咒 ⟺ 分类为诅咒型；非黑色诅咒道具
  未声明 `kind` 时无法派生分类）→ 注册失败：由 items 加载器记录 warning 并忽略该道具，
  不影响其它道具与插件包加载
- 道具模块由 `items/__init__.py` 经 `pkgutil.iter_modules` 自动发现导入，新增道具文件即自动注册

### 5.3 使用上下文（`ItemUseContext`）

```python
@dataclass
class ItemUseContext:
    bot: Bot
    group_id: int
    user_id: int
    user_name: str           # 群昵称（已截断）
    item: ItemDefinition
    args: tuple[str, ...]    # /item.use 的额外参数（诅咒自动使用时为空）
    reply_to: int | None     # 触发指令的消息 ID（诅咒自动使用时为 None）

    async def send(self, text: str) -> None   # 向群聊发送纯文本消息
    async def add_score(self, delta: int) -> int    # 经决斗分数服务增减分数（写库），返回新分数
```

### 5.4 抽取

- `draw_random_item()`：按配置权重抽品质 → 该品质池等概率抽道具；品质池为空返回 `None`（warning）
- `draw_item_of_quality(quality)`：指定品质抽取（合成用），空池返回 `None`（warning）

### 5.5 状态 API（供道具模块使用，Buff/永久型道具的基础设施）

以下 API 均为异步（底层是数据库读写，函数返回时事务已提交）：

- `await add_state(group_id, user_id, *, state: ItemState, duration_seconds=None)` 新增/覆盖并写库
  （`state` 提供状态键、来源道具与附加数据，`expires_at` 由本函数按 `duration_seconds`
  计算（传入值不使用），`None` 表示永久）；`add_states` 支持一批状态共用同一过期时刻
- `await get_state` / `await remove_state` / `await list_states`：读取时跳过已过期状态但不删除，
  过期条目统一交由定时清理分发到期效果（见 `ItemBehavior.expire`）
- `await purge_expired_states()`：定时清理（事务内删除并返回到期列表）
- 状态按群隔离；道具模块可据此实现"状态期间的特殊效果/限定指令"（如经行内判定 `get_state`）

### 5.6 耐久型道具（`core/durability.py`）

- 数据模型：库存数量 + 耗损状态 `<道具编号>.durability`（附加数据 `remaining`
  为各副本剩余耐久列表，按开始耗损的先后排列）；未使用过的副本是全新的
- `durable_behavior(*, total, display_total=None, check=None, use=None)`：返回已接好
  消耗、可交易数量与详情耐久的行为块，道具模块只需一行；行为块不含具体道具，
  同组道具可共用一个实例（如保险 132~136）
- 消耗：优先对已耗损副本扣 1 点耐久（归零时该副本从库存移除），没有耗损副本时
  开始耗损一件全新副本；可交易数量只计全新副本（耗损副本不能交换/合成）
- `move_damaged_instance(group_id, from_user, to_user, item_id)`：把最早开始耗损的
  副本连同剩余耐久转给另一名成员（供「强塞」127 等转移类道具使用）

## 6. 指令与流程

所有指令仅限群聊（`is_type(GroupMessageEvent)`），错误提示引用触发消息，成功播报不带引用。
道具在消息中一律以"<编号> <名称>"（如"111 +1"）指代，编号必须三位、0 不省略。

### 6.1 查看：`/item.list`、`/item.detail <编号>`

- `/item.list`：按品质由高到低（同品质按编号升序）列出"编号 名称【品质】×数量"，
  首行为"<群昵称> 的道具："；种类数超过阈值（`ITEM_LIST_FORWARD_THRESHOLD`，默认 10）时
  整份清单以**合并转发**发送（单节点，节点名"道具清单"，不引用触发消息），
  不超过阈值时按普通文本回复并引用触发消息；
  无道具时提示"你还没有任何道具，签到可以获得随机道具。"
- `/item.detail <编号>`（别名 `/item.info`）：仅可查看自己拥有的道具；不存在/未拥有/参数错误分别提示；
  详情（编号/名称/品质/类型/介绍/功能/使用条件/使用时机/耐久/备注）以**合并转发**发送（单节点，节点名"道具详情"，uin 为机器人 QQ）

### 6.2 抽取（签到发放）：`ItemService.grant_random`

- 由 sign 在签到流程中调用（签到本身由 `gbp_sign_record` 的插入冲突判定，不依赖进程内同步段；
  本调用只做登记，不发送消息），返回 `GrantedItem(item_id, item_name) | None`
- 普通道具写入库存；黑色诅咒不写入库存（获得即自动使用）
- sign 发送签到文案后调用 `handle_acquisition`（异步）：

### 6.3 诅咒获得流程（`handle_acquisition`）

对黑色诅咒：先以合并转发发送一条消息展示完整信息（单节点，节点名"道具详情"，
前缀"你获得了黑色诅咒道具：110 -1！"），随后转交给道具的行为块 `use` 自动使用
（110 发"黑色诅咒生效，你的决斗分数-1。"）。
普通道具该方法为空操作（供 sign 无差别调用）。

### 6.4 合成：`/item.craft <编号1> <编号2>`

- 校验：两个编号均存在、品质相同、非黑色诅咒、非金色传说、数量足够（相同编号需 ≥2）
- 抽取结果品质：低一级 0.24 / 高一级 0.75 / 高两级 0.01（env 可配，见 §9）
  - 灰色垃圾"低一级"→ 黑色诅咒（抽取黑色诅咒池，获得后走诅咒获得流程）
  - 紫色史诗"高两级"→ **两件金色传说**（抽金色池两次）
  - 目标品质池为空 → 合成失败（D4：不消耗材料，发送失败消息并记 warning）
- 成功后先消耗材料并登记/处理结果（普通道具入库、诅咒走 6.3），按档位发送播报：
  低一级"合成失败"、高一级"合成成功"、高两级"合成大成功"，格式为
  "<前缀>，消耗了 111 +1、111 +1，获得了 113 xxx。"（结果多件时以顿号连接）

### 6.5 交换：`/item.exchange`、`/item.exchange.accept`、`/item.exchange.reject`

- `/item.exchange @成员 <自己编号> <对方编号>`：
  - 不能与自己、机器人自己（GBPBot）、机器人名单（共享配置 `BOT_LIST`）中的机器人交换
  - 校验双方均拥有对应道具；D3：双方之间存在任何未完成交换时禁止发起
  - 登记内存交换（10 分钟有效），发送请求消息（**仅含两件道具的编号与名称**，不含品质）：
    "<发起人> 想用 111 +1 交换 <对方> 的 123 xxx。<对方> 可在 10 分钟内使用
    /item.exchange.accept @<发起人> 接受，或 /item.exchange.reject @<发起人> 拒绝。"
- `/item.exchange.accept @成员`：无对应交换 → "X 未向你发起交换。"；
  接受时复查道具（D5）；成功后互换一件并发送完成播报
- `/item.exchange.reject @成员`：无对应交换 → 同上；成功移除并播报"X 已拒绝 Y 的交换请求。"
- 过期：静默删除（debug 日志），不发送任何消息

### 6.6 使用：`/item.use <编号> [参数]`

- 校验：参数格式（三位数字）、道具存在、自己拥有；非"可选型"道具附带额外参数时报用法错误
- 先调用行为块 `check`（返回错误文案则阻止，此时不消费），消耗一件库存后调用 `use`
  （成功消息由道具模块自行发送）
- 黑色诅咒无法拥有，正常不会出现在自己的道具中

## 7. duel 重构（对外接口）

### 7.1 `_shared/services/` 新增/扩展契约

- `DuelScoreService`（扩展）：`add_score(group_id, user_id, name, delta) -> int`、`get_score(group_id, user_id) -> int`
  （均为异步接口，直接读写 `gbp_duel_score`；`name` 随分数变化一并更新）
- `DuelStateService`（新增）：`list_duels(group_id) -> list[DuelSnapshot]`
  （快照含参与者、点数、是否已接受、已出手成员、剩余超时秒数；为只读副本）
- `DuelEventService`（新增）：`subscribe(handler)` / `unsubscribe(handler)`；
  发布 `DuelEvent`（含事件类型、群号、机器人、双方信息、点数等）：

| 事件 | 触发时机 | 可修改字段 |
| --- | --- | --- |
| `challenge` | 发起校验通过、登记决斗前 | `block_reason`（设置后取消发起并发送该文案） |
| `created` | 决斗登记后、发起播报发送后 | 无 |
| `accepting` | 对方接受决斗前 | `block_reason`（设置后取消本次接受并发送该文案，决斗保持待接受） |
| `accepted` | 对方接受（含机器人掷骰接受）后 | 无 |
| `rejected` | 拒绝（含机器人掷骰拒绝）后 | 无 |
| `gesture` | 记录任一方猜拳手势后 | 无 |
| `settling` | 判定完成、结算分数前 | `multiplier`（点数加成）、`result_prefix`（追加到默认结算消息前）；`winner_id`/`loser_id` 只读 |
| `settled` | 分数结算与结果播报完成后 | 无 |
| `timeout` | 决斗超时认领移除后 | 无 |

- 处理器按注册顺序逐个调用（异步处理器被 await）；单个处理器抛异常仅记录错误日志，
  不影响决斗流程与其它处理器（以常量元组统一捕获，规避 BLE001）
- `DuelSettleService`（新增）：`register_settler(priority, handler)`；settling 事件
  全部处理器完成后由决斗流程按优先级从高到低询问接管者，第一个返回 True 的
  接管本场结算（跳过默认分数结算与播报），其余接管者不再调用；全部返回 False
  时走默认结算。接管者只读取已判定出的胜负、不应再改写；抛异常按已接管处理，
  避免已产生的分数改变与默认结算叠加。720 魂迁（蓝色稀有 3）优先于 096
  布衣之怒（白色普通 1）
- `DuelGestureService`（新增）：`register_pair_rule(priority, name, rule)` /
  `register_effect(priority, name, judge, *, required=None)`；胜负判定集中在决斗
  插件的判定引擎（基础猜拳 → 单组规则 → 整局效果），道具只注册规则、不再
  在 settling 事件里改写胜负；整局效果同时声明所需手势数量（如「强运」）

### 7.2 行为保持

- 所有现有文案、判定（含胜负手势表）、超时延迟播报、并发防重逻辑**不变**；
  仅将 `_send_text`/群昵称获取、截断等改为复用 `_shared/onebot/`
- `settling` 处理器改写胜负会被还原并记 error（胜负只能经 `DuelGestureService`
  判定规则或结算接管改变）；点数负数取 0；结果播报使用判定结果

## 8. sign 集成

- 签发流程（同一事务 / 群锁内）：写入签到记录（唯一约束判定重复）→ 发放分数 → 调 `grant_random`
  获得道具（编号回填到签到记录的 `item` 字段，失败为 `""`，旧记录缺该字段按 `""` 兼容）
- 文案：成功且有道具用 `SIGN_TEXT_SUCCESS`（新增 `{item}` 占位）；未抽到用
  `SIGN_TEXT_SUCCESS_NO_ITEM`（默认"签到成功！决斗分数+{score}，道具抽取失败。"）
- 发送签到文案后调用 `handle_acquisition`（异步），由其处理诅咒信息与自动使用

## 9. 配置项（`.env.{environment}`，均可缺失）

```
ITEM_DRAW_BLACK=0.055   # 黑色诅咒
ITEM_DRAW_GRAY=0.55     # 灰色垃圾
ITEM_DRAW_WHITE=0.25    # 白色普通
ITEM_DRAW_GREEN=0.1     # 绿色精良
ITEM_DRAW_BLUE=0.03     # 蓝色稀有
ITEM_DRAW_PURPLE=0.01   # 紫色史诗
ITEM_DRAW_GOLD=0.005    # 金色传说
ITEM_CRAFT_DOWN=0.24    # 合成：低一级
ITEM_CRAFT_UP=0.75      # 合成：高一级
ITEM_CRAFT_UP2=0.01     # 合成：高两级
ITEM_LIST_FORWARD_THRESHOLD=10 # /item.list：道具种类超过该值时整份清单以合并转发发送
BOT_LIST=[111111, 222222] # 共享配置：机器人名单（所有群通用，见 _shared/config/bot_list.py）
```

解析规则（按设计文档）：缺失/为空用默认值；无法解析为数字报错并用默认值；
负数视为 0；抽取概率与合成概率的总和不为 1 时分别归一化；
总和为 0 时视为无效配置、报错并用默认值。
均带容差处理（默认值浮点相加为 1.0000000000000002，不做无谓归一化）。
`ITEM_LIST_FORWARD_THRESHOLD` 需为正整数，缺失、为空、非整数或不为正数时报错并用默认值 10。

道具功能文案（含失败、交换、使用等）全部内置，不经 env 配置。

## 10. 文档同步

- `README.md`：可用指令表新增 item 指令；签到说明提及道具；新增"道具"章节——
  仅介绍功能主体（获取方式、指令、品质、交换规则、诅咒规则），**不列出具体道具**
- `src/plugins/help_manual.txt`：与 README 的纯文本镜像同步更新
- `.env.dev` / `.env.prod`：新增 ITEM_* 配置注释与 SIGN 文案更新（两文件保持同步）

## 11. 测试与验证

- 开发期临时验证脚本（一次性，参考既有习惯、不参考 tests/stress）：
  通过 `SQLALCHEMY_DATABASE_URL` 与 `LOCALSTORE_*` 把数据库与数据目录重定向到临时位置，
  先 `upgrade head` 建表再加载插件，
  覆盖：品质抽取/合成概率解析（默认、归一化、负数、非法值）、空池失败、
  `/item.use`、签到发放（含诅咒消息顺序与记录字段）、合成（成功/失败/诅咒）、
  交换（发起/接受/拒绝/过期/限制）、状态 API、合并转发调用；
  增量验证：道具注册校验（编号留空/重复/品质不一致）、items 加载器忽略坏模块、
  共享配置 BOT_LIST 解析与各插件生效；验证后删除脚本与临时数据
- `ruff check`、`ruff format --check`、`pyright` 全绿

## 12. 边界与限制（记录备忘）

- 耐久型道具按"库存数量 + 耗损状态"跟踪副本：未使用过的副本可交换/合成，
  已耗损的副本不可（见 §5.6）
- Buff/永久型状态基础设施（§5.5）本次完成并测试，暂无示例道具使用
- 道具消息中的群昵称截断长度与决斗共用共享配置 NICKNAME_MAX_LENGTH（默认 12）
- 道具与道具间不互相 import：跨道具的功能分类走框架标签（`ItemTag`、`has_tag`/
  `items_with_tag`）；道具与 duel 的交互经 §7 服务，与其它插件经 `ItemService`
