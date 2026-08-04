# AGENTS.md

## 一、项目介绍

本项目是「闲鱼 AI 砍价本地工具」：在本机用 Playwright 打开闲鱼，按队列一家一家与卖家议价。

最小闭环：

```text
面板粘贴商品链接 → 入队
  → Worker 一次只处理一家
  → AI 发议价 → 等卖家回复（默认 180 秒可配）
  → 超时无回 → 暂挂 → 自动下一家
  → 有人回复 → 锁定当前会话深聊
  → 同意降价 / 明确不降 / 手动结束 → 处理下一家
  → 新链接默认追加队尾；可手动「暂停当前，优先插队」
```

本项目不承诺也不实现：自动购买、付款、填地址、确认收货、Chrome 扩展、商城订单绑定。

## 二、工程要求

- 模块职责分离：API / 业务 / Worker / Playwright / AI / 仓储不得堆进同一文件
- 新增源码文件顶部写中文文件头；函数与类写中文说明
- 敏感信息（密钥、Cookie、登录态）不得写入仓库或日志
- 改动行为必须同步更新 `docs/`

## 三、运行基线

- Python >= 3.11
- `pip install -e ".[dev]"` 后执行 `playwright install chromium`
- AI 浏览器后端由 `XIANYU_BROWSER_BACKEND` 控制（`chromium` / `camoufox` / `cloakbrowser`）；后两者需 `pip install -e ".[camoufox]"` 或 `".[cloakbrowser]"`
- 先 `python scripts/login_xianyu.py` 生成当前后端对应登录态
- `python -m app.main` 启动 API + 面板（默认 `http://127.0.0.1:8787`）
- 测试：`python -m pytest tests -q`
