"""
本文件创建 FastAPI 应用并挂载控制面板。

它属于 app 入口：初始化数据库、队列服务与可选 Worker，托管 web 静态资源。
不包含页面选择器或议价状态机细节。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.routes import router
from app.core.config import get_settings
from app.core.logging import setup_logging
from app.models import init_db
from app.repositories.queue import QueueRepository
from app.services.queue_service import QueueService
from app.worker.bargain_worker import BargainWorker, build_default_worker

WEB_DIR = Path(__file__).resolve().parents[1] / "web"
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    应用生命周期：初始化 DB/服务，恢复中断任务，必要时自动拉起 Worker。
    """

    setup_logging()
    settings = get_settings()
    session_factory = init_db(settings.database_url)
    with session_factory() as session:
        repo = QueueRepository(session)
        repo.ensure_settings(
            reply_timeout_seconds=settings.default_reply_timeout_seconds,
            max_rounds=settings.default_max_rounds,
            auto_send=settings.default_auto_send,
        )
        recovered = repo.recover_interrupted_active()
        if recovered:
            logger.warning("已将 %s 条中断的 active 任务重新入队", recovered)

    worker: BargainWorker | None = None
    try:
        worker = build_default_worker(session_factory)
    except Exception as error:  # noqa: BLE001
        app.state.worker_init_error = str(error)
        worker = None
        logger.exception("Worker 初始化失败，面板仍可入队：%s", error)
    else:
        app.state.worker_init_error = None

    def on_preempt() -> None:
        if worker is not None:
            worker.request_cancel_current()

    def on_stop_active() -> None:
        if worker is not None:
            worker.request_cancel_current()

    queue_service = QueueService(
        session_factory,
        on_preempt=on_preempt,
        on_stop_active=on_stop_active,
    )
    app.state.session_factory = session_factory
    app.state.queue_service = queue_service
    app.state.worker = worker

    # 数据库里若已打开 worker 开关，进程启动后自动继续跑，避免“显示运行中却不干活”。
    with session_factory() as session:
        enabled = QueueRepository(session).get_settings().worker_enabled
    if worker is not None and enabled:
        try:
            await worker.start()
            logger.info("已根据配置自动启动 Worker")
        except Exception:
            logger.exception("自动启动 Worker 失败")
            queue_service.set_worker_enabled(False)

    yield
    if worker is not None and worker.running:
        await worker.stop()


def create_app() -> FastAPI:
    """
    构造 FastAPI 应用实例。
    """

    app = FastAPI(title="闲鱼 AI 砍价本地工具", lifespan=lifespan)
    app.include_router(router)

    if WEB_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

        @app.get("/")
        def index() -> FileResponse:
            """返回控制面板首页。"""

            return FileResponse(WEB_DIR / "index.html")

    return app


app = create_app()


def main() -> None:
    """
    以 uvicorn 启动本地服务。
    """

    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=False,
    )


if __name__ == "__main__":
    main()
