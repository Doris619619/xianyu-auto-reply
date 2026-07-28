# 数据模型

SQLite 默认路径：`./data/bargain.db`（可用 `DATABASE_URL` 覆盖）。

## `queue_items`

| 字段 | 说明 |
|------|------|
| id | 主键 |
| item_id | 闲鱼商品 ID |
| detail_url | 规范化详情页 URL |
| title | 可选标题 |
| status | 见状态枚举 |
| position | 队列排序键 |
| seller_id | 页面发现的卖家 ID |
| result_summary | 结果摘要 |
| fail_code | 稳定原因码 |
| rounds_sent | 已发送轮次 |
| waiting_since | 开始等待卖家的时间 |

### status

- `queued` 排队  
- `active` 当前锁定  
- `parked` 超时暂挂  
- `done_agreed` 同意降价  
- `done_refused` 明确不降  
- `done_manual` 手动结束 / 被插队 / 达轮次上限  
- `failed` 风控、发送不确定、草稿拦截等  

## `session_messages`

面板展示用消息快照：`queue_item_id`、`speaker`（`me`/`seller`）、`text`。

## `app_settings`

单行运行时设置：`reply_timeout_seconds`、`max_rounds`、`auto_send`、`worker_enabled`。
