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
| conversation_phase | 对话阶段：`availability`（查库存）、`negotiation`（议价）、`finished`（已结束） |
| goods_available | 库存信号：`true` 有货、`false` 无货、`null` 尚未明确 |
| over | 对话是否结束的稳定信号；终态为 `true`，暂挂/风控/发送不确定/模型异常为 `false` |
| result_summary | 结果摘要 |
| fail_code | 稳定原因码 |
| send_diagnostic | 发送结果不确定时的脱敏诊断 JSON；不保存消息、Cookie、账号或卖家信息 |
| rounds_sent | 已发送轮次 |
| waiting_since | 开始等待卖家的时间 |

### status

- `queued` 排队  
- `active` 当前锁定  
- `parked` 超时暂挂  
- `done_unavailable` 已无货（`goods_available=false`、`over=true`）
- `done_agreed` 已谈成（明确降价、包邮、赠品或其他让利）
- `done_refused` 未谈成（AI 判定没有可继续协商的空间）
- `done_manual` 手动结束 / 被插队 / 达轮次上限  
- `failed` 风控、发送不确定、草稿拦截等  

明确有货后会写入 `goods_available=true` 并进入 `negotiation`。`done_agreed`、`done_refused`、
`done_manual` 和 `done_unavailable` 都会写入 `over=true`；其余中断路径保持 `over=false`，可重试或人工处理。

`send_diagnostic` 只会记录一次唯一点击的阶段、发送按钮中心是否被遮挡、是否在点击后检测到
风险页、是否确认到本人消息回显，以及粗粒度网络响应状态。诊断失败不会触发再次点击、Enter
回退或自动重试。

## `session_messages`

面板展示用消息快照：`queue_item_id`、`speaker`（`me`/`seller`）、`text`。

## `app_settings`

单行运行时设置：`reply_timeout_seconds`、`max_rounds`、`auto_send`、`worker_enabled`。
