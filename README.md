# 夜璃关系本

夜璃关系本是一个 AstrBot 轻量联系人索引插件，用来把人格设定里固定的人际关系表拆出来，改为可维护、可分域、可按需注入的关系资料系统。

它不是长期记忆插件，也不和任何长期记忆插件做隐式联动。它只负责保存“这个人是谁、在当前群/私聊里该怎么称呼、哪些字段被锁定、是否需要在上下文里临时提醒”这类轻量信息。

## 主要特性

- 分域关系资料：支持全局、群聊、私聊三类作用域
- 轻量注入：只注入当前会话、发言人、被 @、QQ、昵称或别名命中的人
- 私聊保护：默认允许读取私聊资料，但不统计私聊活跃
- 白名单控制：可按群或用户限制插件生效范围
- 多运行模式：支持关闭、只读、仅群聊、手动维护、自动维护
- 主动关系维护：在 mode=auto 时后台低频分析聊天，保守更新称呼、别名、关系、重要度和 AI 备注
- LLM 工具维护：支持 add_user、update_relationship、query_relationship、inject_relationship
- Dashboard 分域管理：支持全局、群聊、私聊作用域切换
- 配置 API：支持读取和热更新插件配置

## 适合场景

- 人格设定里原本写了一整张关系表，导致 prompt 越来越长
- 群聊和私聊需要不同的称呼或关系备注
- 希望只在提到某个人时才注入对应资料
- 希望把“关系索引”和“长期记忆”分开维护

## 插件设置

插件支持在 AstrBot 插件设置里配置以下项目。

### enabled

类型：bool
默认：true
说明：插件总开关。关闭后不读取、不写入、不注入关系本。

### mode

类型：string
默认：auto
可选值：

- off：完全关闭
- read_only：只读取和注入，不自动写入
- group_only：只在群聊作用域启用
- manual：禁止 LLM 工具写入和自动维护，只允许后台/API 手动维护
- auto：允许规则自动维护

### llm_provider_id

类型：string
默认：空
说明：关系本专用 LLM 提供商 ID。留空或填写的提供商不存在时，回退当前正在使用的 LLM 提供商。

### llm_model

类型：string
默认：空
说明：关系本专用模型名。留空时使用 provider 默认模型；填写时会作为 `model` 参数传给 `provider.text_chat`。

### llm_retry_times

类型：int
默认：2
说明：关系本内部 LLM 调用失败时的重试次数，范围 1-5。

### relationship_auto_maintain_enabled

类型：bool
默认：true
说明：是否启用主动关系维护。开启后只在 `mode=auto` 时生效，插件会把当前消息和该用户少量已有资料交给 LLM 做保守判断。该任务在后台运行，不阻塞 bot 回复。

### relationship_auto_maintain_min_interval_seconds

类型：int
默认：90
说明：同一作用域同一用户两次主动分析之间的冷却时间。调大更省 token。

### relationship_auto_maintain_min_message_len

类型：int
默认：6
说明：短于该长度的消息不触发主动分析，避免寒暄、表情和短回复消耗 token。

### relationship_auto_maintain_confidence_threshold

类型：float
默认：0.82
说明：LLM 输出的置信度低于该值时不会写入。建议 0.8-0.9，越高越保守。自动维护会结合当前消息与少量内存上下文进行证据型抽取，并跳过低信息消息。

### relationship_auto_maintain_max_tasks

类型：int
默认：3
说明：后台主动关系分析最大并发数。低并发更稳，也能限制资源占用。

### enable_group_whitelist

类型：bool
默认：false
说明：是否启用群聊白名单。启用后，只有 group_whitelist 里的群会生效。

### group_whitelist

类型：list
默认：[]
说明：群聊白名单，填写群号字符串。留空时，如果 enable_group_whitelist 为 false，则所有群可用。

### enable_user_whitelist

类型：bool
默认：false
说明：是否启用用户白名单。启用后，只有 user_whitelist 里的用户会触发写入或读取。

### user_whitelist

类型：list
默认：[]
说明：用户白名单，填写 QQ 号字符串。

### private_read_enabled

类型：bool
默认：true
说明：是否允许私聊作用域读取资料。关闭后私聊不注入关系资料。

### private_stats_enabled

类型：bool
默认：false
说明：是否允许私聊自动统计活跃和自动创建 profile。默认关闭，避免私聊数据无意义膨胀。

### inject_limit

类型：int
默认：8
说明：单次注入的资料数量上限。建议保持 3-12 之间，越大越占 prompt。

### alias_match_enabled

类型：bool
默认：true
说明：开启后，消息文本提到昵称或别名时，会自动命中对应用户并注入资料。


## 运行模式建议

一般群聊使用：

```json
{
  "enabled": true,
  "mode": "auto",
  "private_read_enabled": true,
  "private_stats_enabled": false,
  "inject_limit": 8
}
```

只想手动维护，不想自动写资料：

```json
{
  "enabled": true,
  "mode": "manual"
}
```

只在指定群启用：

```json
{
  "enabled": true,
  "enable_group_whitelist": true,
  "group_whitelist": ["123456789"]
}
```

完全只读：

```json
{
  "enabled": true,
  "mode": "read_only"
}
```

## 数据结构

插件数据库位于 AstrBot 数据目录下的 plugin_data/relationship.db。

主要表：

- active_seen：旧版群聊活跃候选统计
- active_seen_scoped：新版分域累计活跃统计，带 2000 条封顶
- active_seen_daily：14 天滚动活跃日统计，用于候选排序
- relationship_profiles：新版分域关系资料表
- profile_locks：新版分域字段锁表
- relationship_aliases：别名索引表
- history_scan_cursor：每群历史扫描游标
- history_seen_messages：历史消息去重表，避免重复扫描累加 msg_count；超过 14 天自动清理

新版分域格式：

- global:global：全局资料
- group:<群号>：群聊资料
- private:<QQ号>：私聊资料

## 群历史补录命令

### 关系本扫描

在当前群扫描历史消息，只读取纯文本段，忽略图片、语音、文件等非文本消息。

用法：

- `/关系本扫描`：扫描默认 5 轮，每轮 20 条，补录为 active_seen 活跃候选
- `/关系本扫描 10 30`：扫描 10 轮，每轮 30 条
- `/关系本扫描 入册`：扫描后同时创建当前群的关系资料 profile

说明：该命令仍遵守群白名单与用户白名单；开启群白名单时，只有 group_whitelist 内的群可扫描。

## 白名单群历史自动扫描

可在配置中开启 `history_auto_scan_enabled`。开启后，插件会在后台按 `history_auto_scan_interval_minutes` 间隔遍历 `group_whitelist`，自动扫描白名单群历史消息并补录活跃候选。

相关配置：

- `history_auto_scan_enabled`：是否启用自动扫描，默认 false
- `history_auto_scan_interval_minutes`：扫描间隔分钟，默认 360
- `history_auto_scan_rounds`：每群扫描轮数，默认 5
- `history_auto_scan_per_count`：每轮拉取条数，默认 20
- `history_auto_scan_auto_profile`：是否自动入册，默认 false
- `history_auto_scan_group_delay_seconds`：多个群之间的扫描间隔秒数，默认 5

自动扫描只读取纯文本段，忽略图片、语音、文件等非文本消息；并且只有收到过一次消息拿到 bot 实例后才会开始后台扫描。

扫描会写入 `history_seen_messages` 做 message_id 去重；手动扫描和自动扫描共用去重表，已扫描过的消息不会再次累计 msg_count。插件会每天清理超过 14 天的去重记录与滚动活跃日记录。

## LLM 工具

### add_user

添加用户资料。支持可选 scope_type 和 scope_id。不传时使用当前会话作用域。

### update_relationship

更新指定用户字段。支持字段：nickname、aliases、title_auto、note_auto、relation_type、importance。
管理员锁定字段后，LLM 工具不能修改。

### query_relationship

查询当前作用域下的用户资料；查不到时会 fallback 到全局资料。

### inject_relationship

强制标记下次对话刷新关系本注入。

## Web API

配置接口：

GET /astrbot_plugin_yeli_relationship/relationship/config
POST /astrbot_plugin_yeli_relationship/relationship/config

用于读取或热更新插件轻量配置。relationship 查询、更新、锁定、新增、删除接口均支持 scope_type 和 scope_id。

## 使用注意

1. 启用本插件后，建议从 AstrBot 人格设定里删除固定的人际关系表，避免重复注入。
2. 备注字段只供内部维护和 summary 场景使用，正常聊天不要直接或间接引用。
3. Dashboard 支持全局、群聊、私聊作用域切换。
4. 插件内部只注册新版 API 前缀 astrbot_plugin_yeli_relationship。
5. repo 地址未修改，发新仓库时再改。
6. 修改 mode、白名单或注入数量后，建议观察一次日志和注入内容是否符合预期。

## 作者

冰糖
## v1.2 方向补充：短卡注入与可诊断运行

本版本开始将关系本定位为“群友画像小抄”：资料可以逐步丰富，但注入给模型的内容保持短、近、准。

### 当前轮短卡注入

关系本注入不再默认输出完整表格，而是生成当前轮相关对象的短卡。默认只注入当前发言人、被 @ 用户、QQ/昵称/别名明确命中的用户。未被明确提及的高活跃群友默认不注入，避免干扰回复注意力。

相关配置：

- `inject_budget_mode`：注入预算模式，支持 `compact`、`balanced`、`rich`。
- `inject_max_chars`：单次关系本注入字符上限。
- `inject_max_profiles`：单次最多注入人数。
- `inject_active_profiles_enabled`：是否追加高活跃群友，默认关闭。

### 资料互通策略

`scope_sharing_mode` 控制全局、群聊、私聊资料的读取关系：

- `isolated`：当前作用域完全隔离。
- `global_fallback`：当前作用域优先，允许全局资料兜底，推荐默认值。
- `shared`：允许同一 QQ 的群聊/私聊/全局资料互通，并在注入中标注来源。

### bot 自我认知

开启 `enable_relationship_self_awareness` 后，关系本会注入极短能力说明：bot 可以知道自己有夜璃关系本辅助能力，但只能依据当前可见资料回答是否认识用户；没有资料时必须说明不知道，不能编造。

### 会话快照缓存

为降低多群并发时串作用域的风险，插件会记录最近消息的轻量快照，并在注入时用当前上下文中的最后一条用户消息匹配对应快照。

相关配置：

- `turn_context_cache_size`：缓存最近多少条消息快照。
- `turn_context_ttl_seconds`：快照保留秒数。

状态检测页会显示快照缓存数量和注入预览，方便确认插件是否正在工作。

### 命中保护

昵称/别名命中现在会跳过冲突项：同一个文本别名如果命中多个 QQ，则不会自动注入任何一方，并会在诊断原因中提示。短于 `alias_min_match_len` 的昵称/别名也会被忽略，减少常见短词误命中。

### 主动维护边界

主动维护仍交给 bot 做保守判断，但插件会阻止明显不适合自动写入的内容：例如现实身份、年龄、学校、职业、地区、联系方式、家庭、健康等敏感资料。昵称默认只在原本为空时允许自动补全，不覆盖已有昵称。

## v1.2.1 补充：可纠错与长期运行

### 可纠错

- Dashboard 表格操作列新增“清”按钮，只清空自动称呼 `title_auto` 和自动备注 `note_auto`，不影响手动称呼、手动备注、昵称、别名和锁定状态。
- 后端接口：`POST /astrbot_plugin_yeli_relationship/relationship/clear_auto`，参数为 `qq_id`、`scope_type`、`scope_id`。
- 清理动作会写入 `relationship_op_logs`，便于回看误记修正记录。

### 长期运行

- 新增 `relationship/maintenance` 接口，返回资料数、活跃记录数、历史去重记录数、操作日志数、数据库大小、后台任务状态和缓存大小。
- Dashboard 新增“维护状态”面板，可查看关键运行指标，并手动清理过期历史去重和活跃日记录。
- 自动清理仍只处理过期辅助记录，不删除关系资料本体。