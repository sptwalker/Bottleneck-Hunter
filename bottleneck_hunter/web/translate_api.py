"""翻译 API — 挂载于 /api/translate。新闻中英对照等按需翻译。"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from bottleneck_hunter.auth.dependencies import get_current_user
from bottleneck_hunter.web.translate import translate_texts

router = APIRouter(tags=["translate"])


class TranslateRequest(BaseModel):
    texts: list[str] = Field(default_factory=list)
    target: str = "zh"   # zh | en


@router.post("")
async def translate(req: TranslateRequest, user: dict = Depends(get_current_user)):
    """批量翻译文本，返回 {源文: 译文}（仅含成功翻译的；缺失由前端回退原文）。"""
    target = "en" if req.target == "en" else "zh"
    texts = [t for t in (req.texts or []) if t][:60]   # 单次上限，防滥用
    return {"translations": await translate_texts(texts, target)}
