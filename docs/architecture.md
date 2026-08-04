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
                      |
             BrowserSession（AI 或 CDP）
```

## 浏览器后端

AI Worker 通过环境变量 `XIANYU_BROWSER_BACKEND` 选择独立浏览器实现：

| 值 | 实现 | 依赖 |
|----|------|------|
| `chromium`（默认） | Playwright 官方 Chromium | `playwright` |
| `camoufox` | Camoufox（Firefox 系） | `pip install -e ".[camoufox]"` |
| `cloakbrowser` | CloakBrowser（自定义 Chromium） | `pip install -e ".[cloakbrowser]"` |

手动回复模式不走上述后端，而是连接 `XIANYU_CDP_ENDPOINT` 指向的本机可见浏览器。

实现位于 `app/crawler/browser_backends/`：

- `create_ai_browser_session(settings)`：按后端创建独立会话，忽略 CDP
- `create_login_browser_session(settings)`：按后端创建有头空白会话，供人工登录后保存状态
- `create_manual_cdp_session(settings)`：有 CDP 时创建附着会话
- `PersistentPlaywrightChatFactory` 只消费 `BrowserSession`，不再内部分支浏览器类型

登录态默认分文件保存，避免跨浏览器共用：

```text
data/browser_states/
├─ chromium_storage_state.json
├─ camoufox_storage_state.json
└─ cloakbrowser_storage_state.json
```

可用 `XIANYU_STORAGE_STATE_PATH` 覆盖。`chromium` 在新默认文件不存在且根目录旧 `storage_state.json` 存在时会兼容回退。

**重要**：切换浏览器后端只用于兼容性对比与排障，不等于规避闲鱼风控。遇到验证码或限制页仍立即失败关闭，不会自动轮换浏览器继续操作。

## 模块职责

| 目录 | 职责 | 不负责 |
|------|------|--------|
| `app/api` | HTTP 路由 | 页面操作、状态机细节 |
| `app/services` | 入队/插队/设置业务 | Playwright DOM |
| `app/worker` | 单飞议价状态机 | HTTP、前端 |
| `app/crawler` | 打开聊、读、发 | 队列策略 |
| `app/crawler/browser_backends` | 浏览器启动与登录态路径 | 聊天 DOM、议价 |
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
- 登录态与密钥仅本地 `.env` / 各后端 storage_state 文件，不入库、不进 Git
