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
2. 首句固定问“老板，这个还在吗？”，先确认商品是否仍在售
3. 明确无货 → 写入 `goods_available=false`、`over=true` 并结束；明确有货后才固定问“好的，那可以便宜一点吗？”
4. 议价超时无回 → 暂挂 → 自动下一家；暂挂、风控、发送不确定和模型异常保持 `over=false`
5. 有人回 → AI 结合完整聊天裁决继续、谈成或未谈成；谈成、谈不成、手动结束或达到最大轮次均写入 `over=true` 后处理下一家
6. 新链接默认追加队尾；可用「优先插队」暂停当前并切过去
7. AI 浏览器后端由 `XIANYU_BROWSER_BACKEND` 控制；手动模式仍走 `XIANYU_CDP_ENDPOINT`

## 测试

```powershell
python -m pytest tests -q
```

## 文档

- [docs/architecture.md](docs/architecture.md)
- [docs/api.md](docs/api.md)
- [docs/data-model.md](docs/data-model.md)
- [docs/known-limitations.md](docs/known-limitations.md)
