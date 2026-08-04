# 闲鱼 AI 砍价本地工具

本机 Playwright（可切换 CloakBrowser / Camoufox）+ Web 控制面板：粘贴商品链接入队，一家一家 AI 议价。

## 快速开始

```powershell
cd D:\Repo\xianyu-auto-reply
python -m pip install -e ".[dev]"
playwright install chromium
copy .env.example .env
# 编辑 .env：填写 DEEPSEEK_API_KEY；可选 XIANYU_BROWSER_BACKEND=chromium|camoufox|cloakbrowser
python scripts\login_xianyu.py
python scripts\print_account_fingerprint.py
# 把打印出的 SHA-256 写入 .env 的 XIANYU_EXPECTED_ACCOUNT_ID
python -m app.main
```

使用 CloakBrowser 时：

```powershell
python -m pip install -e ".[cloakbrowser]"
# .env 中设置 XIANYU_BROWSER_BACKEND=cloakbrowser
python scripts\login_xianyu.py
```

浏览器打开 `http://127.0.0.1:8787`。

## 行为摘要

1. 链接排队，默认一家一家处理
2. 发出议价后等待卖家（默认 180 秒，可在面板改）
3. 超时无回 → 暂挂 → 自动下一家
4. 有人回 → 锁定深聊，直到同意降价 / 明确不降 / 手动结束 / 达最大轮次
5. 新链接默认追加队尾；可用「优先插队」暂停当前并切过去
6. AI 浏览器后端由 `XIANYU_BROWSER_BACKEND` 控制；手动模式仍走 `XIANYU_CDP_ENDPOINT`

## 测试

```powershell
python -m pytest tests -q
```

## 文档

- [docs/architecture.md](docs/architecture.md)
- [docs/api.md](docs/api.md)
- [docs/data-model.md](docs/data-model.md)
- [docs/known-limitations.md](docs/known-limitations.md)
