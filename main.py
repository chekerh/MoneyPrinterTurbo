import sys

import uvicorn
from loguru import logger

from app.config import config
from app.utils.access_policy import AccessPolicyError, resolve_bind_host

if __name__ == "__main__":
    # 绑定地址必须在 uvicorn 监听之前判定。放到 asgi 的 lifespan 里太晚：
    # 那时 socket 已经在接受连接了，"失败关闭"就变成了摆设。
    try:
        host = resolve_bind_host(
            config.listen_host,
            allow_non_loopback=bool(
                getattr(config, "allow_non_loopback_bind", False)
            ),
        )
    except AccessPolicyError as exc:
        logger.error(f"refusing to start: {exc}")
        sys.exit(1)

    logger.info(f"start server, docs: http://{host}:" + str(config.listen_port) + "/docs")
    # FFmpeg 探测已经移到 app/services/task.py 的共享任务流水线里，这样
    # API、CLI 和 WebUI 三条路径都能统一覆盖，这里不再单独检查。
    uvicorn.run(
        app="app.asgi:app",
        host=host,
        port=config.listen_port,
        reload=config.reload_debug,
        log_level="warning",
    )
