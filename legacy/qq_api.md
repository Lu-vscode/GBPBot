# QQ官方机器人接口适配

## 目标
重构此项目，使其同时支持Napcat+Onebot v11与QQ官方机器人api，使用单进程双适配器的模式，
允许两个机器人同时运行，官方机器人仅实现部分功能。

## 官方机器人已有插件功能取舍
触发官方机器人的指令需要额外加上qq.的前缀（如/qq.duel.rank）,onebot端不执行带qq.前缀的指令，以防重复触发两个机器人
### duel
仅支持/qq.duel.score,/qq.duel.rank，合并转发改为拼接并直接发送消息

### item
主体功能支持，合并转发改为拼接并直接发送消息，具体道具的功能取舍请向我提问。

### default_reply
不支持

### help
变为发送“请参阅 GBPBot 用户手册：https://github.com/Lu-vscode/GBPBot”

### nickname_guard
不支持

### random_face
仅支持emoji表情

### react
不支持

### send_rate_limit
不支持

### sign
仅在绑定QQ号与openid后支持

## 消息接收模式
默认全量模式，非全量模式不支持所有涉及@成员的功能

## openid处理
未绑定时群号/QQ号与openid两套数据并行存储，即(group_id/群openid,user_id/群成员openid)。
当官方机器人指令以未绑定成员为目标时，数据记录在成员的openid下；当在未绑定的群聊中使用官方机器人指令时，数据记录在群openid下。

### 新增bind插件

存储群号与群Openid的映射关系、各个群聊中群成员QQ号与群成员Openid的映射关系。

在env中配置超级用户的QQ号与其私聊openid的映射关系，超级用户可在与官方机器人的私聊中使用“/qq.bind.superuser.key”生成自己QQ号的验证密钥（需要加密,有效期1min），
在群聊中使用"/qq.bind.superuser <自己的QQ号> <验证密钥>"将自己在此群的openid与自己的QQ号绑定（绑定后密钥作废）

超级用户可在群聊中使用"/qq.bind.group <群号>"将此群号与该群的openid绑定，每个群仅可绑定一次。

在群号已被绑定的群聊中，超级用户可使用“/qq.bind.member <QQ号> @群成员”将该成员的QQ号与openid绑定（作用域仅限该群）

在群号已被绑定的群聊中，当两个机器人同时处于该群聊且均在线时，普通群成员可以使用“/qq.bind.self <需要绑定的QQ号>”获取一次性票据（由官方端生成，有效期5min）,再使用"/bind.self.verify <票据>"将自己的QQ号与openid绑定(由onebot端确认，需要指令触发者的QQ号与要绑定的QQ号一致)(作用域仅限本群)

QQ号绑定成功后除了记录映射关系外，还需要将此群聊中自己记录在Openid下的数据合并到QQ号下，其中决斗分数相加、道具合并、状态合并（相同状态根据其重复获得的处理逻辑合并）

群号绑定成功后也需要将原本记录在群openid下的数据合并到群号下

当存在openid到QQ号的映射关系时，可以用于需要用到QQ号的场合，如BOT_LIST、SUPERUSERS等

## 其它
官方端需要使用群昵称而无法获取时，使用昵称，昵称也无法获取时，使用截断的openid