"""
本文件用于开发者在本机有头浏览器中人工登录闲鱼并保存状态。

它按 ``XIANYU_BROWSER_BACKEND`` 启动对应浏览器；用户自行操作后保存 Playwright storage state。
不收集账号密码或验证码。
"""

from __future__ import annotations

import asyncio

from app.core.config import get_settings
from app.crawler.browser_backends.factory import create_login_browser_session


async def login() -> None:
    """
    打开有头闲鱼页面，等待用户确认后保存本地登录态。

    无输入输出；浏览器错误向上抛出；副作用为人工登录和写入被忽略的状态文件。
    """

    settings = get_settings()
    state_path = settings.resolved_storage_state_path()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    backend = settings.xianyu_browser_backend
    print(f"当前浏览器后端：{backend}")
    print(f"登录态将保存到：{state_path}")

    browser_session = create_login_browser_session(settings)
    context = await browser_session.start()
    try:
        page = await context.new_page()
        await page.goto("https://www.goofish.com/", wait_until="domcontentloaded")
        print("请在打开的浏览器中自行完成登录，并确认可正常搜索。")
        await asyncio.get_running_loop().run_in_executor(
            None, lambda: input("确认登录完成后按 Enter 保存本地状态：")
        )
        await context.storage_state(path=str(state_path))
        print(f"登录状态已保存到：{state_path}")
    finally:
        await browser_session.stop()


def main() -> None:
    """
    运行人工登录异步入口。

    无输入输出；失败向命令行抛出；副作用由 `login` 描述。
    """

    asyncio.run(login())


if __name__ == "__main__":
    main()
