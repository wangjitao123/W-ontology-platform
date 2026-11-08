"""MinIO 文档读取服务 — 列出并解析 milvus bucket 中的文档"""
from __future__ import annotations

import io
import logging
from typing import Any

import boto3
from botocore.config import Config

from app.config import settings

logger = logging.getLogger(__name__)

_client = None


class StorageUnavailable(Exception):
    """对象存储不可达（MinIO 未部署 / 网络不通 / 凭证错）。"""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


def _get_client():
    global _client
    if _client is None:
        _client = boto3.client(
            "s3",
            endpoint_url=settings.MINIO_ENDPOINT,
            aws_access_key_id=settings.MINIO_ACCESS_KEY,
            aws_secret_access_key=settings.MINIO_SECRET_KEY,
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
                # boto3 默认 connect/read 各 60s 且重试 4 次：MinIO 不可达时
                # 单次调用能挂几分钟，把 FastAPI 的线程池拖死。
                # 这里显式压到 2s 且不重试——对象存储只用来列文档，
                # 挂了就该立刻如实报不可用，而不是让页面转圈。
                connect_timeout=2,
                read_timeout=2,
                retries={"max_attempts": 1},
            ),
            region_name="us-east-1",
        )
    return _client


def _wrap_unreachable(exc: Exception) -> StorageUnavailable:
    return StorageUnavailable(
        f"对象存储不可达（{settings.MINIO_ENDPOINT}）：{type(exc).__name__}: {exc}"
    )


def list_documents(prefix: str = "", limit: int = 200) -> list[dict]:
    client = _get_client()
    try:
        resp = client.list_objects_v2(
            Bucket=settings.MINIO_BUCKET, Prefix=prefix, MaxKeys=limit,
        )
    except Exception as e:  # noqa: BLE001
        # 不返回空列表：那会让 UI 显示"没有文档"，而真相是"存储连不上"。
        raise _wrap_unreachable(e) from None
    items = []
    for obj in resp.get("Contents", []) or []:
        key = obj["Key"]
        if not key.lower().endswith((".docx", ".doc", ".pdf", ".txt", ".xlsx")):
            continue
        items.append({
            "key": key,
            "title": key.rsplit("/", 1)[-1],
            "size": obj.get("Size", 0),
            "last_modified": obj.get("LastModified").isoformat() if obj.get("LastModified") else None,
        })
    return items


def get_document_content(key: str) -> str:
    client = _get_client()
    try:
        resp = client.get_object(Bucket=settings.MINIO_BUCKET, Key=key)
        body = resp["Body"].read()
    except Exception as e:  # noqa: BLE001
        raise _wrap_unreachable(e) from None

    if key.lower().endswith(".docx"):
        return _parse_docx(body)
    elif key.lower().endswith(".txt"):
        return body.decode("utf-8", errors="replace")
    elif key.lower().endswith(".pdf"):
        return _parse_pdf(body)
    else:
        return f"[Unsupported format: {key}]"


def _parse_docx(data: bytes) -> str:
    try:
        from docx import Document
        doc = Document(io.BytesIO(data))
        paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
        return "\n".join(paragraphs)
    except ImportError:
        return "[python-docx not installed]"
    except Exception as e:
        logger.warning(f"Failed to parse docx: {e}")
        return f"[Parse error: {e}]"


def _parse_pdf(data: bytes) -> str:
    try:
        import fitz  # PyMuPDF
        doc = fitz.open(stream=data, filetype="pdf")
        text_parts = []
        for page in doc:
            text_parts.append(page.get_text())
        return "\n".join(text_parts)
    except ImportError:
        return "[PyMuPDF not installed]"
    except Exception as e:
        logger.warning(f"Failed to parse pdf: {e}")
        return f"[Parse error: {e}]"
