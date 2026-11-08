"""Cube 指标层客户端。

Cube 的查询是**异步**的：POST /load 可能返回 `{"error":"Continue wait"}`，
表示查询已入队；客户端必须**重发同一个请求**（Cube 按查询哈希去重），
直到拿到结果或真实错误。这里封装了这个重试循环。

失败语义：
    - 数据源不可达（如 connect ETIMEDOUT）会被原样抛出，不做"假装有数"的兜底
    - 超时、鉴权失败都给出明确原因码
"""
from __future__ import annotations

import logging
import time
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)


class CubeError(Exception):
    """Cube 调用失败。reason 是机器可读原因码。"""

    def __init__(self, reason: str, message: str) -> None:
        self.reason = reason
        self.message = message
        super().__init__(f"{reason}: {message}")


class CubeClient:
    def __init__(
        self,
        base_url: str | None = None,
        api_secret: str | None = None,
        timeout: float | None = None,
    ) -> None:
        self.base_url = (base_url or settings.CUBE_API_URL or "").rstrip("/")
        self.api_secret = api_secret or settings.CUBE_API_SECRET or ""
        self.timeout = timeout or settings.CUBE_TIMEOUT_SECONDS

    # ── 基础设施 ────────────────────────────────────────────
    def _headers(self) -> dict[str, str]:
        # Cube dev 模式不校验，但生产模式要 JWT；这里同时带上开发用 secret，
        # 兼容两种部署形态。
        return {
            "Authorization": self.api_secret or "unified-cube-local-dev-secret-change-me",
            "Content-Type": "application/json",
        }

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        import httpx

        if not self.base_url:
            raise CubeError("not_configured", "未配置 CUBE_API_URL")
        url = f"{self.base_url}{path}"
        try:
            resp = httpx.post(
                url, json=payload, headers=self._headers(), timeout=self.timeout,
            )
        except Exception as e:  # noqa: BLE001
            raise CubeError("unreachable", f"连接 Cube 失败：{e}") from None

        if resp.status_code == 401:
            raise CubeError("unauthorized", "Cube 鉴权失败，检查 CUBE_API_SECRET")
        if resp.status_code >= 500:
            raise CubeError("server_error", f"Cube 返回 {resp.status_code}")

        try:
            return resp.json()
        except Exception:  # noqa: BLE001
            raise CubeError("bad_response", f"Cube 返回非 JSON（{resp.status_code}）") from None

    def _get(self, path: str) -> dict[str, Any]:
        import httpx

        if not self.base_url:
            raise CubeError("not_configured", "未配置 CUBE_API_URL")
        try:
            resp = httpx.get(
                f"{self.base_url}{path}", headers=self._headers(), timeout=self.timeout,
            )
        except Exception as e:  # noqa: BLE001
            raise CubeError("unreachable", f"连接 Cube 失败：{e}") from None
        if resp.status_code == 401:
            raise CubeError("unauthorized", "Cube 鉴权失败，检查 CUBE_API_SECRET")
        try:
            return resp.json()
        except Exception:  # noqa: BLE001
            raise CubeError("bad_response", f"Cube 返回非 JSON（{resp.status_code}）") from None

    # ── 元数据 ──────────────────────────────────────────────
    def fetch_meta(self, *, include_datasets: bool = False) -> dict[str, Any]:
        """拉取全部 cube 的指标/维度定义。

        Cube 会为每个业务 cube 自动生成一个 `ds_<view>__<cube>` 数据集视图
        （dimensions 为空、只镜像 measures）。默认过滤掉它们，避免污染目录。
        """
        meta = self._get("/cubejs-api/v1/meta")
        cubes = meta.get("cubes") or []
        if not include_datasets:
            cubes = [c for c in cubes if not str(c.get("name", "")).startswith("ds_")]
        return {"cubes": cubes}

    def list_cubes(self, *, include_datasets: bool = False) -> list[dict[str, Any]]:
        meta = self.fetch_meta(include_datasets=include_datasets)
        out = []
        for c in meta.get("cubes") or []:
            out.append({
                "name": c.get("name"),
                "title": c.get("title") or c.get("name"),
                "description": c.get("description") or "",
                "measures": [
                    {
                        "name": m.get("name"),
                        "title": m.get("title") or m.get("name"),
                        "type": m.get("type"),
                        "description": m.get("description") or "",
                    }
                    for m in (c.get("measures") or [])
                ],
                "dimensions": [
                    {
                        "name": d.get("name"),
                        "title": d.get("title") or d.get("name"),
                        "type": d.get("type"),
                        "description": d.get("description") or "",
                    }
                    for d in (c.get("dimensions") or [])
                ],
            })
        return out

    # ── 查询 ────────────────────────────────────────────────
    def load(self, query: dict[str, Any]) -> dict[str, Any]:
        """执行一次 Cube 查询，处理 `Continue wait` 重试。

        返回 Cube 原始响应（含 data / annotation）。
        """
        deadline = time.monotonic() + settings.CUBE_QUERY_WAIT_SECONDS
        attempt = 0
        while True:
            attempt += 1
            payload = {"query": query}
            resp = self._post("/cubejs-api/v1/load", payload)

            err = resp.get("error")
            if err and str(err).strip().lower() == "continue wait":
                if time.monotonic() > deadline:
                    raise CubeError(
                        "query_timeout",
                        f"Cube 查询超过 {settings.CUBE_QUERY_WAIT_SECONDS}s 仍未完成",
                    )
                time.sleep(1.0)
                continue

            if err:
                # 数据源不可达、SQL 错误等都会走到这里。原样上报，不伪造结果。
                raise CubeError("query_failed", str(err))

            return resp
