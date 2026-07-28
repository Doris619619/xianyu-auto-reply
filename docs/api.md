# API 说明

基址：`http://127.0.0.1:8787`（默认）。

## 队列

### `POST /api/items`

请求：`{"url":"<商品链接或纯数字ID>","title":null}`

响应：`{"id", "item_id", "position_rank", "status", "message"}`  
新链接始终入队尾，不打断当前会话。

### `GET /api/items`

返回全部队列项、`worker_enabled`、`active_id`。

### `POST /api/items/{id}/prioritize`

暂停当前 active（标为 `done_manual` / `preempted`），目标插队到队首。

### `POST /api/items/{id}/retry`

仅 `parked` 可重试，重新变为 `queued` 并排到队尾。

### `POST /api/items/{id}/stop`

手动结束指定项。

### `POST /api/session/stop`

手动结束当前 active。

### `GET /api/session/current`

当前锁定会话的消息列表。

## 设置

### `GET /api/settings` / `PATCH /api/settings`

字段：`reply_timeout_seconds`（30–900）、`max_rounds`（1–20）、`auto_send`、`worker_enabled`（只读于 GET；启停走 Worker 接口）。

## Worker

### `POST /api/worker/start`

置 `worker_enabled=true` 并启动后台任务（需已配置登录态与 DeepSeek）。

### `POST /api/worker/stop`

停止后台任务。
