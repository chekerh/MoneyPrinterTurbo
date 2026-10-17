"""Application implementation - ASGI."""

import os
from contextlib import asynccontextmanager
from functools import lru_cache

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

from app.config import config
from app.controllers import base
from app.models.exception import HttpException
from app.router import root_api_router
from app.services.metrics import registry, start_metrics_collection, stop_metrics_collection
from app.utils import utils


@asynccontextmanager
async def application_lifespan(_: FastAPI):
    """集中处理 API 进程启动恢复和关闭日志。"""
    logger.info("startup event")

    configured_api_key = config.app.get("api_key", "")
    if configured_api_key in (None, ""):
        logger.warning(
            "API key authentication is disabled; keep the API on a trusted network"
        )
    elif isinstance(configured_api_key, str):
        # 只记录保护范围，不得输出 Key、长度或摘要，避免凭据进入日志系统。
        logger.info("API key authentication is enabled for /api/v1 and /tasks")
    else:
        logger.error(
            "API key authentication is misconfigured: app.api_key must be a string"
        )

    # 启动指标收集器（Prometheus /metrics）
    start_metrics_collection(interval=30.0)
    logger.info("metrics collector started")

    # 跨平台发布由当前进程线程池执行，不会在服务重启后恢复。启动时把 Redis
    # 中确认已失去执行进程的活动状态收敛为失败，避免任务永久无法删除。
    from app.services import task as task_service

    task_service.recover_interrupted_cross_posts()
    try:
        yield
    finally:
        stop_metrics_collection()
        logger.info("shutdown event")


def exception_handler(request: Request, e: HttpException):
    return JSONResponse(
        status_code=e.status_code,
        content=utils.get_response(e.status_code, e.data, e.message),
    )


def validation_exception_handler(request: Request, e: RequestValidationError):
    return JSONResponse(
        status_code=400,
        content=utils.get_response(
            status=400, data=e.errors(), message="field required"
        ),
    )


def get_application() -> FastAPI:
    """Initialize FastAPI application.

    Returns:
       FastAPI: Application object instance.

    """
    instance = FastAPI(
        title=config.project_name,
        description=config.project_description,
        version=config.project_version,
        debug=False,
        lifespan=application_lifespan,
    )
    instance.include_router(root_api_router)
    instance.add_exception_handler(HttpException, exception_handler)
    instance.add_exception_handler(RequestValidationError, validation_exception_handler)

    # Prometheus metrics endpoint
    @instance.get("/metrics", include_in_schema=False)
    async def metrics():
        return PlainTextResponse(registry.export_prometheus(), media_type="text/plain")

    return instance


app = get_application()


@app.middleware("http")
async def protect_generated_task_files(request: Request, call_next):
    """保护任务产物静态路由，防止绕过 API 鉴权直接下载。

    ``/tasks`` 由 StaticFiles 独立挂载，无法复用 APIRouter 的依赖，
    因此在中间件中调用同一个 verify_token。鉴权函数会在未配置
    api_key 时放行；OPTIONS 预检请求也保留给 CORS 中间件处理。
    """

    request_path = request.url.path
    is_task_file = request_path == "/tasks" or request_path.startswith("/tasks/")
    if is_task_file and request.method != "OPTIONS":
        try:
            base.verify_token(request)
        except HttpException as exception:
            return exception_handler(request, exception)

    return await call_next(request)


# Configures the CORS middleware for the FastAPI app
cors_allowed_origins_str = os.getenv("CORS_ALLOWED_ORIGINS", "")
origins = cors_allowed_origins_str.split(",") if cors_allowed_origins_str else ["*"]
# 鉴权用的是 ``x-api-key`` **请求头**，不是 Cookie，因此 credentials 没有任何用处。
# 而它恰恰是"通配来源 + 凭据"这个组合最危险的地方：Starlette 在 allow_origins=["*"]
# 且 allow_credentials=True 时会把请求方 Origin 原样回显进
# Access-Control-Allow-Origin，并附上 Allow-Credentials: true。于是任何一个网页都能
# 在用户开着本应用时对本机发起带凭据的跨域读取 —— 任何 origin 特权并不需要，
# 但留着这个组合就等于把它送出去。显式配置来源时才可以带凭据。
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials="*" not in origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

task_dir = utils.task_dir()
# html=False：不要目录列表。html=True 会把 /tasks/ 渲染成一个可点的目录树，于是
# "知道或猜到一个 task_id" 这道门槛消失，任何人都能枚举出全部历史任务。
# 任务目录里放着 script.json（完整脚本、搜索词、全部生成参数）、plan_meta.json
# 与成片本身。按确切路径取文件的能力保留（自动化与界面都只用确切路径），
# 去掉的是"列出所有任务"的能力。
app.mount("/tasks", StaticFiles(directory=task_dir, html=False), name="")


@app.get("/download", include_in_schema=False)
async def download_generated_file(request: Request, path: str, file_name: str | None = None):
    """按需流式下载成片，内存占用与文件大小无关。

    界面上的「下载」过去是把整条成片 ``read()`` 进内存再交给 Streamlit 的。
    一条 50 秒 1080p 大约 54 MB，而结果页一屏可能有 3 条——也就是说用户只是**看一眼**
    结果页，进程就多吃进 160 MB，而这 160 MB 在按钮被点之前一次都没被用到。

    ``FileResponse`` 直接以文件句柄边读边发，常驻内存只有分块缓冲。路径仍然要
    落在允许的根目录内：``path`` 来自浏览器，是不可信输入，不做约束就等于把整个
    磁盘开放下载。
    """
    try:
        base.verify_token(request)
    except HttpException as exception:
        return exception_handler(request, exception)

    layout = _download_allowed_roots()
    if not layout:
        raise HttpException(
            task_id="download", status_code=404, message="downloads unavailable"
        )

    try:
        # realpath 会把 ../ 与符号链接一并解析掉，之后的"必须在根内"判断才有意义。
        resolved = os.path.realpath(path)
        if not any(
            resolved == str(root) or resolved.startswith(str(root) + os.sep)
            for root in layout
        ):
            raise HttpException(
                task_id="download", status_code=403, message="path outside allowed roots"
            )
        if not os.path.isfile(resolved):
            raise HttpException(
                task_id="download", status_code=404, message="file not found"
            )
    except (OSError, ValueError):
        raise HttpException(
            task_id="download", status_code=400, message="invalid path"
        ) from None

    return FileResponse(
        resolved,
        filename=_safe_download_name(file_name) or os.path.basename(resolved),
        # 成片是不可变产物，直接给浏览器一个长缓存，避免重复传输 54 MB。
        headers={"Cache-Control": "private, max-age=3600"},
    )


def _safe_download_name(name: str | None) -> str:
    """把浏览器传来的建议文件名压成一个安全的 basename。

    它只出现在 ``Content-Disposition`` 里，**不参与定位**——真正的文件由 ``path``
    决定并已通过根目录校验。但它是不可信输入：带路径分隔符或换行的名字会污染
    响应头，空名字会让浏览器拿到一个无名文件。
    """
    if not name:
        return ""
    # 反斜杠在 Windows 上也是分隔符；CR/LF 能直接切断 HTTP 头。
    cleaned = name.replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = "".join(ch for ch in cleaned if ch.isprintable()).strip()
    return cleaned[:200]


@lru_cache(maxsize=1)
def _download_allowed_roots() -> tuple[str, ...]:
    """允许下载的根目录：作品集 outputs 与任务目录。"""
    roots: list[str] = []
    try:
        from app.services.project_layout import resolve_project_layout

        roots.append(str(resolve_project_layout().outputs_root))
    except Exception as exc:  # noqa: BLE001 - 布局不可用时只剩任务目录
        logger.warning(f"outputs root unavailable for downloads: {exc}")
    try:
        roots.append(os.path.realpath(utils.task_dir()))
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"task dir unavailable for downloads: {exc}")
    return tuple(roots)


# /tasks 在 api_key 为空时是**免认证**的（见 base.verify_token 的"本地免认证模式"）。
# 这个默认值本身是合理的——本应用只监听回环地址——但它必须被说出口，否则用户
# 不会知道自己正把脚本与成片暴露给本机上任何能发起 HTTP 请求的进程与网页。
if not config.app.get("api_key", ""):
    logger.warning(
        "任务产物目录 /tasks 当前无需认证即可访问：任何本机进程或你正在浏览的"
        "网页都可能读到全部任务的 script.json 与成片。"
        "在 config.toml 的 [app] 下设置 api_key 可关闭这一点。"
    )

public_dir = utils.public_dir()
app.mount("/", StaticFiles(directory=public_dir, html=True), name="")
