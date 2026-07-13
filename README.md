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
