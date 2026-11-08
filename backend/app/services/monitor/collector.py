import asyncio
import logging
import shutil
import time

import psutil

from app.database import SessionLocal
from app.repositories.monitor_repo import MonitorRepository
from app.services.monitor.event_bus import event_bus
from app.services.monitor.ws_manager import ws_manager

logger = logging.getLogger(__name__)

# ── Service definitions ──

SERVICES = [
    {"name": "后端API", "check": "http", "url": "http://localhost:8001/api/health"},
    {"name": "数据库", "check": "db"},
    {"name": "规则引擎", "check": "memory"},
    {"name": "函数运行时", "check": "memory"},
    {"name": "Agent 服务", "check": "memory"},
    {"name": "本体引擎", "check": "memory"},
    {"name": "图数据库", "check": "neo4j"},
    {"name": "大模型网关", "check": "http", "url": "https://dashscope.aliyuncs.com"},
    {"name": "MinIO", "check": "minio"},
    {"name": "Redis", "check": "redis"},
]


async def _check_http(url: str, timeout: float = 3.0) -> tuple[str, float | None]:
    import httpx
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            start = time.monotonic()
            resp = await client.get(url)
            elapsed = (time.monotonic() - start) * 1000
            if resp.status_code < 400:
                return "healthy", round(elapsed, 1)
            return "warning", round(elapsed, 1)
    except Exception:
        return "unhealthy", None


def _check_db() -> tuple[str, float | None]:
    """同步阻塞检查 —— 必须由调用方用 asyncio.to_thread 包起来。"""
    from sqlalchemy import text
    db = SessionLocal()
    try:
        start = time.monotonic()
        db.execute(text("SELECT 1"))
        elapsed = (time.monotonic() - start) * 1000
        return "healthy", round(elapsed, 1)
    except Exception:
        return "unhealthy", None
    finally:
        db.close()


def _check_neo4j() -> tuple[str, float | None]:
    """同步阻塞检查 —— 必须由调用方用 asyncio.to_thread 包起来。

    显式给连接/取会话设短超时：Neo4j 没部署时默认超时会让这个检查挂几十秒。
    """
    try:
        from app.config import settings
        from neo4j import GraphDatabase
        driver = GraphDatabase.driver(
            settings.NEO4J_URI,
            auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD),
            connection_timeout=2,
            connection_acquisition_timeout=2,
        )
        start = time.monotonic()
        try:
            with driver.session() as session:
                session.run("RETURN 1")
        finally:
            driver.close()
        elapsed = (time.monotonic() - start) * 1000
        return "healthy", round(elapsed, 1)
    except Exception:
        return "unhealthy", None


def _check_minio() -> tuple[str, float | None]:
    """同步阻塞检查 —— 必须由调用方用 asyncio.to_thread 包起来。

    boto3 默认 connect/read 各 60s 且会重试 4 次；MinIO 不可达时单次调用能挂几分钟，
    所以这里显式压到 2s 且不重试。
    """
    try:
        import boto3
        from botocore.config import Config
        from app.config import settings
        client = boto3.client(
            "s3",
            endpoint_url=settings.MINIO_ENDPOINT,
            aws_access_key_id=settings.MINIO_ACCESS_KEY,
            aws_secret_access_key=settings.MINIO_SECRET_KEY,
            config=Config(
                connect_timeout=2, read_timeout=2,
                retries={"max_attempts": 1},
            ),
        )
        start = time.monotonic()
        client.head_bucket(Bucket=settings.MINIO_BUCKET)
        elapsed = (time.monotonic() - start) * 1000
        return "healthy", round(elapsed, 1)
    except Exception:
        return "unhealthy", None


def _check_redis() -> tuple[str, float | None]:
    """同步阻塞检查 —— 必须由调用方用 asyncio.to_thread 包起来。"""
    try:
        import redis
        from app.config import settings
        r = redis.from_url(
            settings.REDIS_URL, socket_timeout=2, socket_connect_timeout=2
        )
        start = time.monotonic()
        r.ping()
        elapsed = (time.monotonic() - start) * 1000
        return "healthy", round(elapsed, 1)
    except Exception:
        return "unhealthy", None


async def _check_service(svc: dict) -> tuple[str, str, float | None]:
    name = svc["name"]
    check = svc["check"]
    try:
        if check == "http":
            status, ms = await _check_http(svc["url"])
        elif check == "db":
            # 阻塞检查一律丢线程：跑在事件循环里会把整个服务冻结
            status, ms = await asyncio.to_thread(_check_db)
        elif check == "neo4j":
            status, ms = await asyncio.to_thread(_check_neo4j)
        elif check == "minio":
            status, ms = await asyncio.to_thread(_check_minio)
        elif check == "redis":
            status, ms = await asyncio.to_thread(_check_redis)
        else:
            status, ms = "healthy", None
    except Exception:
        status, ms = "unhealthy", None
    return name, status, ms


def _sample_host() -> tuple[float, float, float]:
    """采样主机 cpu/内存/磁盘 —— 同步阻塞，必须由 asyncio.to_thread 调用。

    `psutil.cpu_percent(interval=0.1)` 会真的 sleep 100ms；直接放在协程里
    等于每轮冻结事件循环 100ms。
    """
    cpu = psutil.cpu_percent(interval=0.1)
    mem = psutil.virtual_memory()
    disk = shutil.disk_usage("/")
    disk_pct = round(disk.used / disk.total * 100, 1) if disk.total else 0
    return cpu, mem.percent, disk_pct


async def collect_service_metrics():
    while True:
        try:
            # 阻塞采样丢线程，避免冻结事件循环
            cpu, mem_pct, disk_pct = await asyncio.to_thread(_sample_host)

            db = SessionLocal()
            repo = MonitorRepository(db)
            try:
                for svc in SERVICES:
                    name, status, ms = await _check_service(svc)
                    repo.save_metric(
                        service_name=name, status=status, response_ms=ms,
                        cpu_percent=cpu, memory_percent=mem_pct, disk_percent=disk_pct,
                    )
                logger.debug("Service metrics collected")
            finally:
                db.close()
        except Exception as e:
            logger.error("Metric collection error: %s", e, exc_info=True)

        await asyncio.sleep(30)


async def check_alerts():
    while True:
        try:
            db = SessionLocal()
            repo = MonitorRepository(db)
            try:
                metrics = repo.get_latest_metrics()
                for m in metrics:
                    if m.status == "unhealthy":
                        existing = repo.get_alerts(limit=100, resolved=False)
                        has_active = any(a.service_name == m.service_name and a.level == "critical" for a in existing)
                        if not has_active:
                            alert = repo.create_alert(
                                level="critical",
                                service_name=m.service_name,
                                message=f"{m.service_name} 服务不可达" if m.response_ms is None
                                        else f"{m.service_name} 响应超时 ({m.response_ms:.0f}ms)",
                            )
                            await event_bus.emit("alert", {
                                "id": alert.id, "level": alert.level,
                                "service_name": alert.service_name, "message": alert.message,
                                "created_at": alert.created_at.isoformat(),
                            })
                    elif m.status == "warning":
                        existing = repo.get_alerts(limit=100, resolved=False)
                        has_active = any(a.service_name == m.service_name and a.level == "warning" for a in existing)
                        if not has_active:
                            alert = repo.create_alert(
                                level="warning",
                                service_name=m.service_name,
                                message=f"{m.service_name} 响应较慢 ({m.response_ms:.0f}ms)",
                            )
                            await event_bus.emit("alert", {
                                "id": alert.id, "level": alert.level,
                                "service_name": alert.service_name, "message": alert.message,
                                "created_at": alert.created_at.isoformat(),
                            })
            finally:
                db.close()
        except Exception as e:
            logger.error(f"Alert check error: {e}")

        await asyncio.sleep(30)


async def cleanup_old_data():
    while True:
        await asyncio.sleep(3600)
        try:
            db = SessionLocal()
            repo = MonitorRepository(db)
            try:
                repo.cleanup_old_metrics(days=7)
                repo.cleanup_old_alerts(days=30)
                logger.debug("Old monitor data cleaned up")
            finally:
                db.close()
        except Exception as e:
            logger.error(f"Cleanup error: {e}")


async def start_collector():
    asyncio.create_task(collect_service_metrics())
    asyncio.create_task(check_alerts())
    asyncio.create_task(cleanup_old_data())
    ws_manager.start_heartbeat()
    logger.info("Monitor collector started")

    async def _on_alert(data: dict):
        await ws_manager.broadcast({"type": "alert", "data": data})

    event_bus.subscribe("alert", _on_alert)
