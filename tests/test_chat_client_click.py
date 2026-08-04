"""
本文件离线验证发送按钮遮挡时的人工等待与唯一点击边界。

它不启动浏览器、不访问闲鱼；仅用内存替身验证按钮命中检测、等待和失败关闭行为。
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.crawler.chat_client import (
    ChatSafetyError,
    XianyuChatClient,
    _SendAttemptState,
)


class _FakeMouse:
    """记录可见鼠标操作，供发送边界测试断言。"""

    def __init__(self) -> None:
        self.events: list[str] = []

    async def move(self, *_args: object, **_kwargs: object) -> None:
        """记录移动事件。"""

        self.events.append("move")

    async def down(self) -> None:
        """记录按下事件。"""

        self.events.append("down")

    async def up(self) -> None:
        """记录松开事件。"""

        self.events.append("up")


class _FakePage:
    """提供最小页面时钟和鼠标接口。"""

    def __init__(self) -> None:
        self.mouse = _FakeMouse()
        self.waits: list[int] = []

    async def wait_for_timeout(self, milliseconds: int) -> None:
        """记录等待时长但不真实休眠。"""

        self.waits.append(milliseconds)


class _FakeSendButton:
    """按预设布尔序列模拟按钮中心命中结果。"""

    def __init__(self, hits: list[bool]) -> None:
        self._hits = iter(hits)
        self.evaluate_calls = 0

    async def scroll_into_view_if_needed(self, **_kwargs: object) -> None:
        """模拟已滚动到可见区域。"""

    async def bounding_box(self) -> dict[str, float]:
        """返回稳定的可点击边界。"""

        return {"x": 10.0, "y": 20.0, "width": 100.0, "height": 40.0}

    async def evaluate(self, *_args: object, **_kwargs: object) -> bool:
        """返回当前命中状态。"""

        self.evaluate_calls += 1
        return next(self._hits)


def _client_for_click_test(page: _FakePage) -> XianyuChatClient:
    """构造只用于私有点击方法的最小客户端实例。"""

    client = object.__new__(XianyuChatClient)
    client._page = page
    client._assert_safe = AsyncMock()
    return client


@pytest.mark.asyncio
async def test_click_uses_exposed_button_area_when_center_is_obscured() -> None:
    """按钮中心被遮挡但仍有露出区域时，应只在露出区域执行一次点击。"""

    page = _FakePage()
    button = _FakeSendButton([False, True])
    attempt = _SendAttemptState()

    await _client_for_click_test(page)._click_send_button_with_mouse(button, attempt)

    assert attempt.button_center_obscured is True
    assert attempt.click_attempted is True
    assert button.evaluate_calls == 2
    assert page.mouse.events == ["move", "down", "up"]
    assert 1_000 not in page.waits


@pytest.mark.asyncio
async def test_click_stops_without_mouse_action_when_button_is_fully_covered() -> None:
    """全部检查点均被遮挡时必须失败关闭且不能触发任何鼠标点击。"""

    page = _FakePage()
    button = _FakeSendButton([False] * 9)
    attempt = _SendAttemptState()

    with pytest.raises(ChatSafetyError, match="安全点击区域") as error:
        await _client_for_click_test(page)._click_send_button_with_mouse(button, attempt)

    assert error.value.code == "chat_send_button_obscured"
    assert attempt.phase == "button_obscured"
    assert attempt.button_center_obscured is True
    assert attempt.click_attempted is False
    assert page.mouse.events == []
