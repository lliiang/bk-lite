import logging
import os
import sys

from api import api, enterprise_api
from core.collection.application import initialize_collection_application
from core.collection.host_remote.runtime import register_host_remote_runtime
from core.config import YamlConfig
from core.infra.nats import initialize_nats
from core.infra.redis_client import register_redis_lifecycle
from core.infra.snmp_engine_pool import register_snmp_engine_lifecycle
from dotenv import load_dotenv
from sanic import Sanic

load_dotenv(".env")

yml_config = YamlConfig(path="./config.yml")
app = Sanic("Stargazer", config=yml_config)

LOG_FORMAT = "%(asctime)s [%(process)d] [%(levelname)s] %(message)s"
LOG_LEVEL_ENV = "STARGAZER_LOG_LEVEL"
_DEFAULT_LOG_LEVEL = "DEBUG"
# Sanic 自带 handler 的 logger，禁止再向 Python root 传播，避免每行打印两次。
_SANIC_LOGGERS = ("sanic.root", "sanic.error", "sanic.access")
# 第三方库的 DEBUG 会淹没采集目标日志，固定压回 INFO。
_NOISY_LOGGERS = ("asyncio", "httpx", "httpcore", "urllib3", "botocore", "aiormq", "nats", "redis")


def enable_debug_logging(app, *, level=None) -> str:
    """把 stargazer 进程日志整体开到 DEBUG（默认），便于跟踪每目标同步结果。

    Sanic 默认只给 ``sanic.root`` 挂 INFO 级 console handler，executor 里的
    ``event=collection_progress`` / ``event=target_collection_started``、
    插件里的 ``logger.debug`` 都会被 ``logging.lastResort`` 丢弃，这就是
    “只看到任务开始/汇总、看不到后面成功与失败”的原因。

    这里给 Python root 补一个 stdout handler 接管所有命名 logger，并把
    ``sanic.*`` 调到同一级别；可用 ``STARGAZER_LOG_LEVEL=INFO`` 恢复常规日志量。
    """

    resolved = str(level if level is not None else os.getenv(LOG_LEVEL_ENV) or _DEFAULT_LOG_LEVEL).strip().upper()
    if not isinstance(logging.getLevelName(resolved), int):
        resolved = _DEFAULT_LOG_LEVEL

    # 同步写回 Sanic 的 LOGGING，避免框架后续 dictConfig 把级别改回 INFO。
    logging_config = getattr(app.config, "LOGGING", None)
    if isinstance(logging_config, dict):
        loggers_config = logging_config.setdefault("loggers", {})
        for name in _SANIC_LOGGERS:
            logger_config = loggers_config.setdefault(name, {})
            logger_config["level"] = resolved
            logger_config["propagate"] = False

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(LOG_FORMAT))

    root_logger = logging.getLogger()
    root_logger.handlers[:] = [handler]
    root_logger.setLevel(resolved)

    for name in _SANIC_LOGGERS:
        sanic_logger = logging.getLogger(name)
        sanic_logger.setLevel(resolved)
        sanic_logger.propagate = False

    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.INFO)

    return resolved


enable_debug_logging(app)
app.blueprint(api)
if enterprise_api:
    app.blueprint(enterprise_api)

nats_instance_id = os.getenv("NATS_INSTANCE_ID", "default")
service_name = f"{nats_instance_id}_stargazer"
nats = initialize_nats(app, service_name=service_name)

register_redis_lifecycle(app)
register_snmp_engine_lifecycle(app)
initialize_collection_application(app)
register_host_remote_runtime(app)

# 导入 nats_server 模块，确保处理器被注册
from service import nats_server as _nats_server  # noqa: E402,F401


@app.before_server_start
async def show_banner(app, loop):
    with open("./asserts/banner.txt") as f:
        print(f.read())


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8083, workers=int(os.getenv("SANIC_WORKERS", "4")))
