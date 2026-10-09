"""Stateless report downloads, separate from research and its failure state."""
import asyncio
import logging
import re
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.config import settings
from src.services.exporting import MIME_TYPES, export_report, safe_filename

router = APIRouter()
export_slots = asyncio.Semaphore(1)
logger = logging.getLogger(__name__)


class ExportRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    topic: str = Field(min_length=1, max_length=500)
    content: str = Field(min_length=1, max_length=200_000)
    format: Literal['md', 'txt', 'html', 'docx', 'pdf']

    @field_validator('topic', 'content')
    @classmethod
    def valid_text(cls, value):
        if not value.strip(): raise ValueError('导出内容不能为空')
        if re.search(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', value):
            raise ValueError('导出内容包含不支持的控制字符')
        return value


@router.post('/api/reports/export')
async def download_report(request: ExportRequest):
    try:
        async with export_slots:
            payload = await asyncio.to_thread(export_report, request.topic, request.content,
                                              request.format, settings.export_font_path)
    except Exception:
        logger.exception('Report export failed: %s', request.format)
        raise HTTPException(status_code=500, detail='报告导出失败，请重试或选择 Markdown 格式；原报告不受影响')
    filename = safe_filename(request.topic, request.format)
    return Response(payload, media_type=MIME_TYPES[request.format], headers={
        'Content-Disposition': f"attachment; filename=research-report.{request.format}; filename*=UTF-8''{quote(filename)}",
        'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
    })
