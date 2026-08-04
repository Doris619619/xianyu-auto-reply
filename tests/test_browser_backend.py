"""
本文件验证浏览器后端配置、登录态路径解析与会话工厂分发。

它属于 tests，不启动真实浏览器、不访问闲鱼、不读取真实登录态内容。
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.crawler.browser_backends.camoufox import CamoufoxBrowserSession
from app.crawler.browser_backends.cdp import EdgeCdpBrowserSession
from app.crawler.browser_backends.chromium import ChromiumBrowserSession
from app.crawler.browser_backends.cloakbrowser import CloakBrowserSession
from app.crawler.browser_backends.factory import (
    create_ai_browser_session,
    create_login_browser_session,
    create_manual_cdp_session,
)
from app.crawler.browser_backends.storage import (
    LEGACY_CHROMIUM_STORAGE_STATE,
    default_storage_state_path,
    resolve_storage_state_path,
)


def _workdir() -> Path:
    """在仓库内创建可写临时目录，避开本机 pytest 默认 Temp 权限问题。"""

    base = Path(__file__).resolve().parents[1] / ".pytest_tmp"
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"browser-backend-{uuid.uuid4().hex}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def test_accepts_supported_browser_backends() -> None:
    """chromium / camoufox / cloakbrowser 均可作为合法后端。"""

    assert Settings(xianyu_browser_backend="chromium").xianyu_browser_backend == "chromium"
    assert Settings(xianyu_browser_backend="Camoufox").xianyu_browser_backend == "camoufox"
    assert (
        Settings(xianyu_browser_backend="cloakbrowser").xianyu_browser_backend == "cloakbrowser"
    )


def test_rejects_unknown_browser_backend() -> None:
    """非法后端名称必须在配置阶段被拒绝。"""

    with pytest.raises(ValidationError, match="XIANYU_BROWSER_BACKEND"):
        Settings(xianyu_browser_backend="chrome")


def test_explicit_storage_path_overrides_backend_default() -> None:
    """显式配置登录态路径时不再按后端默认文件名。"""

    configured = _workdir() / "custom_state.json"
    path = resolve_storage_state_path(
        backend="cloakbrowser",
        configured_path=str(configured),
    )
    assert path == configured


def test_default_storage_path_is_backend_specific() -> None:
    """未配置路径时使用 data/browser_states/{backend}_storage_state.json。"""

    assert resolve_storage_state_path(
        backend="cloakbrowser",
        configured_path=None,
    ) == default_storage_state_path("cloakbrowser")
    assert resolve_storage_state_path(
        backend="camoufox",
        configured_path="",
    ) == default_storage_state_path("camoufox")


def test_chromium_falls_back_to_legacy_storage_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """chromium 在新默认路径不存在且旧 storage_state.json 存在时回退旧路径。"""

    workdir = _workdir()
    monkeypatch.chdir(workdir)
    legacy = LEGACY_CHROMIUM_STORAGE_STATE
    legacy.write_text("{}", encoding="utf-8")
    resolved = resolve_storage_state_path(backend="chromium", configured_path=None)
    assert resolved == legacy


def test_chromium_prefers_new_default_when_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """chromium 新默认登录态存在时不再回退旧路径。"""

    workdir = _workdir()
    monkeypatch.chdir(workdir)
    legacy = LEGACY_CHROMIUM_STORAGE_STATE
    legacy.write_text("{}", encoding="utf-8")
    modern = default_storage_state_path("chromium")
    modern.parent.mkdir(parents=True, exist_ok=True)
    modern.write_text("{}", encoding="utf-8")
    resolved = resolve_storage_state_path(backend="chromium", configured_path=None)
    assert resolved == modern


def test_settings_resolved_storage_state_path_uses_backend() -> None:
    """Settings.resolved_storage_state_path 跟随后端默认分文件。"""

    settings = Settings(
        xianyu_browser_backend="cloakbrowser",
        xianyu_storage_state_path=None,
    )
    assert settings.resolved_storage_state_path() == default_storage_state_path(
        "cloakbrowser"
    )


def test_create_ai_browser_session_dispatches_by_backend() -> None:
    """AI 会话工厂按后端返回对应实现，且忽略 CDP。"""

    chromium = create_ai_browser_session(
        Settings(
            xianyu_browser_backend="chromium",
            xianyu_cdp_endpoint="http://127.0.0.1:9222",
        )
    )
    camoufox = create_ai_browser_session(Settings(xianyu_browser_backend="camoufox"))
    cloak = create_ai_browser_session(Settings(xianyu_browser_backend="cloakbrowser"))

    assert isinstance(chromium, ChromiumBrowserSession)
    assert isinstance(camoufox, CamoufoxBrowserSession)
    assert isinstance(cloak, CloakBrowserSession)


@pytest.mark.parametrize(
    ("backend", "expected_type"),
    [
        ("chromium", ChromiumBrowserSession),
        ("camoufox", CamoufoxBrowserSession),
        ("cloakbrowser", CloakBrowserSession),
    ],
)
def test_create_login_browser_session_is_headed_and_ignores_cdp(
    backend: str,
    expected_type: type[object],
) -> None:
    """人工登录会话不读取存量登录态，固定有头且不连接 CDP。"""

    session = create_login_browser_session(
        Settings(
            xianyu_browser_backend=backend,
            xianyu_headless=True,
            xianyu_cdp_endpoint="http://127.0.0.1:9222",
        )
    )

    assert isinstance(session, expected_type)
    assert session._storage_state_path is None  # type: ignore[attr-defined]
    assert session._headless is False  # type: ignore[attr-defined]


def test_create_manual_cdp_session_only_when_configured() -> None:
    """手动 CDP 会话仅在配置端点时创建，与 AI 后端无关。"""

    assert create_manual_cdp_session(Settings(xianyu_cdp_endpoint=None)) is None
    session = create_manual_cdp_session(
        Settings(
            xianyu_browser_backend="cloakbrowser",
            xianyu_cdp_endpoint="http://127.0.0.1:9222",
        )
    )
    assert isinstance(session, EdgeCdpBrowserSession)


def test_cloakbrowser_missing_dependency_has_install_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """缺少 cloakbrowser 包时错误信息必须提示可选依赖安装命令。"""

    import builtins

    from app.crawler.browser_backends import cloakbrowser as cloak_mod

    real_import = builtins.__import__

    def fake_import(name: str, *args: object, **kwargs: object):
        if name == "cloakbrowser" or name.startswith("cloakbrowser."):
            raise ImportError("simulated missing cloakbrowser")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(RuntimeError, match=r'pip install -e "\.\[cloakbrowser\]"'):
        cloak_mod._import_cloakbrowser_launch()


def test_camoufox_missing_dependency_has_install_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """缺少 camoufox 包时错误信息必须提示可选依赖安装命令。"""

    import builtins

    from app.crawler.browser_backends import camoufox as camoufox_mod

    real_import = builtins.__import__

    def fake_import(name: str, *args: object, **kwargs: object):
        if name == "camoufox" or name.startswith("camoufox."):
            raise ImportError("simulated missing camoufox")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(RuntimeError, match=r'pip install -e "\.\[camoufox\]"'):
        camoufox_mod._import_async_camoufox()


class _FakeContext:
    """测试浏览器 context 的最小替身。"""

    async def close(self) -> None:
        """记录关闭动作；不访问外部资源。"""


class _FailingBrowser:
    """在创建 context 时失败、但可观察关闭动作的浏览器替身。"""

    def __init__(self) -> None:
        """初始化观测字段；无外部副作用。"""

        self.closed = False
        self.context_options: dict[str, Any] | None = None

    async def new_context(self, **kwargs: Any) -> _FakeContext:
        """记录空白 context 参数后模拟创建失败。"""

        self.context_options = kwargs
        raise RuntimeError("simulated context failure")

    async def close(self) -> None:
        """记录浏览器关闭；无外部资源。"""

        self.closed = True


@pytest.mark.asyncio
async def test_chromium_login_context_failure_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Chromium 空白登录 context 创建失败时关闭浏览器和 Playwright。"""

    from app.crawler.browser_backends import chromium as chromium_mod

    browser = _FailingBrowser()

    class FakePlaywright:
        """提供 Chromium 启动入口与 stop 观测的 Playwright 替身。"""

        def __init__(self) -> None:
            self.stopped = False
            self.chromium = self

        async def launch(self, *, headless: bool) -> _FailingBrowser:
            assert headless is False
            return browser

        async def stop(self) -> None:
            self.stopped = True

    playwright = FakePlaywright()

    class FakeStarter:
        """模拟 async_playwright().start()。"""

        async def start(self) -> FakePlaywright:
            return playwright

    monkeypatch.setattr(chromium_mod, "async_playwright", lambda: FakeStarter())
    with pytest.raises(RuntimeError, match="simulated context failure"):
        await ChromiumBrowserSession(storage_state_path=None, headless=False).start()

    assert browser.context_options == {}
    assert browser.closed is True
    assert playwright.stopped is True


@pytest.mark.asyncio
async def test_cloakbrowser_login_context_failure_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CloakBrowser 空白登录 context 创建失败时关闭浏览器。"""

    from app.crawler.browser_backends import cloakbrowser as cloak_mod

    browser = _FailingBrowser()

    async def launch(*, headless: bool) -> _FailingBrowser:
        assert headless is False
        return browser

    monkeypatch.setattr(cloak_mod, "_import_cloakbrowser_launch", lambda: launch)
    with pytest.raises(RuntimeError, match="simulated context failure"):
        await CloakBrowserSession(storage_state_path=None, headless=False).start()

    assert browser.context_options == {}
    assert browser.closed is True


@pytest.mark.asyncio
async def test_camoufox_login_context_failure_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Camoufox 空白登录 context 创建失败时退出 launcher。"""

    from app.crawler.browser_backends import camoufox as camoufox_mod

    browser = _FailingBrowser()

    class FakeLauncher:
        """提供 async context manager 生命周期观测的 Camoufox 替身。"""

        exited = False

        def __init__(self, *, headless: bool) -> None:
            assert headless is False

        async def __aenter__(self) -> _FailingBrowser:
            return browser

        async def __aexit__(
            self,
            exc_type: object,
            exc_value: object,
            traceback: object,
        ) -> None:
            type(self).exited = True

    monkeypatch.setattr(camoufox_mod, "_import_async_camoufox", lambda: FakeLauncher)
    with pytest.raises(RuntimeError, match="simulated context failure"):
        await CamoufoxBrowserSession(storage_state_path=None, headless=False).start()

    assert browser.context_options == {}
    assert FakeLauncher.exited is True
