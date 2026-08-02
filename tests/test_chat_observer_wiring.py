"""本文件验证新开聊天页的 WebSocket 监听会在页面创建时注册。"""

from __future__ import annotations

from app.crawler.chat_client import ChatBinding, XianyuChatClient, _ActiveSendObservation
from app.services.xianyu_account_guard import normalize_account_guard


class FakeWebSocket:
    """模拟 Playwright WebSocket，仅保存事件回调。"""

    url = "wss://im.goofish.com/socket"

    def __init__(self) -> None:
        """初始化空的事件回调表。"""

        self.handlers: dict[str, object] = {}

    def on(self, event: str, handler: object) -> None:
        """记录指定事件的监听器。"""

        self.handlers[event] = handler


class FakePage:
    """模拟页面和 BrowserContext 的最小事件接口。"""

    def __init__(self) -> None:
        """初始化页面与上下文的事件回调表。"""

        self.handlers: dict[str, object] = {}
        self.context = self

    def on(self, event: str, handler: object) -> None:
        """记录上下文或页面上的事件监听器。"""

        self.handlers[event] = handler


def test_new_chat_page_websocket_is_observed_before_send() -> None:
    """新聊天页创建后，其发送帧应能进入当前发送观察窗口。"""

    initial_page = FakePage()
    client = XianyuChatClient(
        initial_page,
        ChatBinding(source_item_id="1001", seller_id="seller1", account_id="a" * 64),
        normalize_account_guard(None),
    )
    new_chat_page = FakePage()
    page_handler = initial_page.handlers["page"]
    assert callable(page_handler)
    page_handler(new_chat_page)

    websocket = FakeWebSocket()
    websocket_handler = new_chat_page.handlers["websocket"]
    assert callable(websocket_handler)
    websocket_handler(websocket)

    client._active_send_observation = _ActiveSendObservation(draft_text="你好")
    frame_handler = websocket.handlers["framesent"]
    assert callable(frame_handler)
    frame_handler('{"content":"你好"}')
    assert client._active_send_observation.request_observed is True
    assert client._active_send_observation.transport == "websocket"
