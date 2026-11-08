"""对象索引的定时同步。

为什么需要：
    `object_index` 是对象实例的物化快照。源表数据变化后索引会过期，
    在此之前只能靠人手动调 `POST /studio/index/sync`，容易忘。

实现约束（重要）：
    - **索引同步是阻塞的**（源库网络 I/O + 本地 SQLite 写），必须 `asyncio.to_thread`
      丢线程。直接写在协程里会冻结事件循环 —— 监控采集器当初就是这么把整个
      服务拖垮的（表现为接口偶发卡 1-2 分钟）。
    - **不允许并发同步**：手动触发和定时触发共用一把锁，后到的直接跳过，
      避免同一时刻对源库连打两轮查询触发 rate_limit_qps。
    - 失败不抛到事件循环：单轮失败只记日志，下一轮继续。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.config import settings
from app.database import SessionLocal

logger = logging.getLogger(__name__)

TASK_NAME = "object-index-sync"


def _sync_once() -> dict[str, Any]:
    """在独立 session 里跑一轮同步（阻塞，只能在工作线程中调用）。"""
    from app.services.data_plane.object_index_service import ObjectIndexService

    db = SessionLocal()
    try:
        return ObjectIndexService(db).sync_all(skip_if_running=True)
    finally:
        db.close()


async def _loop() -> None:
    delay = max(0, settings.OBJECT_INDEX_SYNC_INITIAL_DELAY_SECONDS)
    interval = max(30, settings.OBJECT_INDEX_SYNC_INTERVAL_SECONDS)

    if delay:
        await asyncio.sleep(delay)

    while True:
        try:
            summary = await asyncio.to_thread(_sync_once)
            if summary.get("busy"):
                logger.info("对象索引同步跳过：已有一轮在执行中")
            else:
                logger.info(
                    "对象索引同步完成：ok=%s skipped=%s failed=%s",
                    summary.get("ok"), summary.get("skipped"), summary.get("failed"),
                )
        except Exception:
            logger.exception("对象索引定时同步失败（下一轮重试）")

        await asyncio.sleep(interval)


def start_object_index_scheduler() -> asyncio.Task | None:
    """启动定时同步任务；未启用时返回 None。"""
    if not settings.OBJECT_INDEX_SYNC_ENABLED:
        logger.info("对象索引定时同步已关闭（OBJECT_INDEX_SYNC_ENABLED=false）")
        return None

    task = asyncio.create_task(_loop(), name=TASK_NAME)
    logger.info(
        "对象索引定时同步已启动：每 %ss 一轮，首次延迟 %ss",
        settings.OBJECT_INDEX_SYNC_INTERVAL_SECONDS,
        settings.OBJECT_INDEX_SYNC_INITIAL_DELAY_SECONDS,
    )
    return task
