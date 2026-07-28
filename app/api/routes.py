"""
本文件定义砍价队列与 Worker 控制的 REST 路由。

它属于 api 模块，只做 HTTP 适配；业务在 QueueService，页面操作在 Worker。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from app.schemas.queue import (
    CurrentSessionResponse,
    EnqueueRequest,
    EnqueueResponse,
    QueueItemOut,
    QueueListResponse,
    SettingsOut,
    SettingsUpdateRequest,
    WorkerControlResponse,
)
from app.services.queue_service import QueueService, QueueServiceError

router = APIRouter(prefix="/api")


def _service(request: Request) -> QueueService:
    """从应用状态取出队列服务。"""

    return request.app.state.queue_service


def _worker_running(request: Request) -> bool:
    """返回进程内 Worker 是否真的在跑。"""

    worker = getattr(request.app.state, "worker", None)
    return bool(worker is not None and worker.running)


@router.post("/items", response_model=EnqueueResponse)
def enqueue_item(body: EnqueueRequest, request: Request) -> EnqueueResponse:
    """
    将商品链接追加到队尾。

    新链接不打断当前会话。
    """

    try:
        return _service(request).enqueue(body.url, body.title)
    except QueueServiceError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.get("/items", response_model=QueueListResponse)
def list_items(request: Request) -> QueueListResponse:
    """返回队列快照。"""

    return _service(request).list_queue(worker_running=_worker_running(request))


@router.post("/items/{item_id}/prioritize", response_model=QueueItemOut)
def prioritize_item(item_id: int, request: Request) -> QueueItemOut:
    """暂停当前会话，将指定项优先插队。"""

    try:
        return _service(request).prioritize(item_id)
    except QueueServiceError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.post("/items/{item_id}/retry", response_model=QueueItemOut)
def retry_item(item_id: int, request: Request) -> QueueItemOut:
    """将暂挂项重新入队。"""

    try:
        return _service(request).retry(item_id)
    except QueueServiceError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.post("/items/{item_id}/stop", response_model=QueueItemOut)
def stop_item(item_id: int, request: Request) -> QueueItemOut:
    """手动结束指定队列项。"""

    try:
        result = _service(request).stop(item_id)
    except QueueServiceError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    if result is None:
        raise HTTPException(status_code=404, detail="队列项不存在")
    return result


@router.post("/session/stop", response_model=QueueItemOut | None)
def stop_current(request: Request) -> QueueItemOut | None:
    """手动结束当前 active 会话。"""

    return _service(request).stop(None)


@router.get("/session/current", response_model=CurrentSessionResponse)
def current_session(request: Request) -> CurrentSessionResponse:
    """返回当前锁定会话消息。"""

    return _service(request).current_session()


@router.get("/settings", response_model=SettingsOut)
def get_settings(request: Request) -> SettingsOut:
    """读取运行时设置。"""

    return _service(request).get_settings()


@router.patch("/settings", response_model=SettingsOut)
def patch_settings(body: SettingsUpdateRequest, request: Request) -> SettingsOut:
    """更新超时、轮次与自动发送开关。"""

    return _service(request).update_settings(
        reply_timeout_seconds=body.reply_timeout_seconds,
        max_rounds=body.max_rounds,
        auto_send=body.auto_send,
    )


@router.post("/worker/start", response_model=WorkerControlResponse)
async def start_worker(request: Request) -> WorkerControlResponse:
    """启用 Worker 并启动后台任务。"""

    init_error = getattr(request.app.state, "worker_init_error", None)
    worker = request.app.state.worker
    if worker is None:
        detail = init_error or "Worker 未初始化（检查登录态与 DEEPSEEK_API_KEY）"
        raise HTTPException(status_code=500, detail=f"Worker 启动失败：{detail}")

    _service(request).set_worker_enabled(True)
    if not worker.running:
        try:
            await worker.start()
        except Exception as error:  # noqa: BLE001
            _service(request).set_worker_enabled(False)
            raise HTTPException(status_code=500, detail=f"Worker 启动失败：{error}") from error
    return WorkerControlResponse(
        worker_enabled=True,
        worker_running=True,
        message="Worker 已启动，正在处理队列",
    )


@router.post("/worker/stop", response_model=WorkerControlResponse)
async def stop_worker(request: Request) -> WorkerControlResponse:
    """停用 Worker 并停止后台任务。"""

    _service(request).set_worker_enabled(False)
    worker = request.app.state.worker
    if worker is not None and worker.running:
        await worker.stop()
    return WorkerControlResponse(
        worker_enabled=False,
        worker_running=False,
        message="Worker 已停止",
    )
