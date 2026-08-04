# API 说明

基址：`http://127.0.0.1:8787`（默认）。

## 队列

### `POST /api/items`

请求：`{"url":"<商品链接或纯数字ID>","title":null}`

响应：`{"id", "item_id", "position_rank", "status", "message"}`  
新链接始终入队尾，不打断当前会话。

### `GET /api/items`

返回全部队列项、`worker_enabled`、`active_id`。

### `DELETE /api/items`

先关闭并等待 Worker 完整停止，再原子删除全部队列项及面板会话记录。该操作不可恢复；返回已删除的队列项数。

### `DELETE /api/items/{id}`

永久删除指定的一条队列记录及其面板会话内容。若该条正在处理，会立即请求 Worker 取消当前会话；不会自动重试发送。

### `POST /api/items/{id}/prioritize`

暂停当前 active（标为 `done_manual` / `preempted`），目标插队到队首。

### `POST /api/items/{id}/retry`

仅 `parked` 可重试，重新变为 `queued` 并排到队尾。

### `POST /api/items/{id}/resume-monitoring`

仅适用于已至少发送一轮、但因监听异常而 `failed` 的项。重新入队后只等待卖家新回复，
不会发送新的开场消息。

### `POST /api/items/{id}/stop`

手动结束指定项。

### `POST /api/session/stop`

手动结束当前 active。

### `GET /api/session/current`

当前锁定会话的消息列表。队列项中的 `send_diagnostic`（如存在）为脱敏发送诊断：只包含点击、
风险检查和回显确认阶段，不包含消息正文、Cookie 或账号信息。

响应额外包含 `manual_send_available`、当前会话的 `processing_reply_mode`，以及手动模式所需的本机浏览器 `browser` 状态。

### `POST /api/session/manual-reply`

仅当前 `active` 且处于 `manual` 模式的会话可调用。请求：`{"text":"<人工确认的回复>"}`。

接口会等待闲鱼聊天页面回读确认本人消息后才返回成功；发送结果不确定时会失败关闭，绝不自动重试。

### `GET /api/browser/connection`

返回手动回复所需 CDP 浏览器的 `configured`、`connected` 与非敏感提示文本。

## 设置

### `GET /api/settings` / `PATCH /api/settings`

字段：`reply_timeout_seconds`（30–900）、`max_rounds`（1–20）、`reply_mode`（`ai` 或 `manual`）、兼容保留的 `auto_send`、`worker_enabled`（只读于 GET；启停走 Worker 接口）。

`reply_mode` 是全局的下一会话设置；已经被 Worker 领取的会话会保留其 `processing_reply_mode`，直到结束或暂停。

## Worker

### `POST /api/worker/start`

置 `worker_enabled=true` 并启动后台任务（需已配置登录态与 DeepSeek）。

### `POST /api/worker/stop`

停止后台任务。
