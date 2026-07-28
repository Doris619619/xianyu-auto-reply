# 架构说明

本文件描述闲鱼 AI 砍价本地工具的当前真实实现。

## 总体结构

```text
面板 (web/)  --REST-->  FastAPI (app/api)
                           |
                           v
                     QueueService / SQLite
                           |
                           v
                     BargainWorker（单飞）
                      /          \
             PersistentPlaywright   DeepSeek 草稿
             ChatFactory            + 同意/拒绝判定
```

## 模块职责

| 目录 | 职责 | 不负责 |
|------|------|--------|
| `app/api` | HTTP 路由 | 页面操作、状态机细节 |
| `app/services` | 入队/插队/设置业务 | Playwright DOM |
| `app/worker` | 单飞议价状态机 | HTTP、前端 |
| `app/crawler` | Playwright 打开聊、读、发 | 队列策略 |
| `app/seller_chat` | 单会话编排、黑名单、LLM、结果判定 | 多任务调度 |
| `app/repositories` | SQLite CRUD | 业务决策 |
| `web/` | 控制面板 | 直接操作浏览器 |

## Worker 不变量

1. 全局最多一个 `active` 队列项  
2. 首轮等待超时 → `parked`，立即领取下一家  
3. 卖家回复后保持锁定深聊  
4. 新链接只追加队尾  
5. 优先插队：当前 `active` → `done_manual(preempted)`，目标插到队首  
6. 浏览器 context 长驻，每个商品新开 page  

## 安全边界

- 允许议价；禁止外链、站外联系、支付、地址、验证码、拍下/付款承诺  
- 发送结果不确定 → `failed`，禁止自动重试  
- 登录态与密钥仅本地 `.env` / `storage_state.json`，不入库、不进 Git  
