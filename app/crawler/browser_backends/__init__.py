"""
本包负责按配置创建可切换的浏览器会话实现。

它属于 crawler 模块的浏览器后端层，供 PersistentPlaywrightChatFactory 与登录脚本使用。
不负责聊天打开、议价草稿或队列业务；也不在风控后自动轮换浏览器。

请从具体子模块导入，例如::

    from app.crawler.browser_backends.factory import create_ai_browser_session
    from app.crawler.browser_backends.storage import resolve_storage_state_path

避免在本包 ``__init__`` 中急切导入 factory，以免与 ``app.core.config`` 形成循环依赖。
"""
