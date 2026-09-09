from pydantic import BaseModel
from typing import Optional, List


class Clip(BaseModel):
    clip_number: int
    start_time: str
    end_time: str
    duration_seconds: int
    title: str
    hook: str
    why_viral: str
    viral_score: int
    best_platform: str
    emotion: str
    clip_type: str
    suggested_caption: str
    file_url: Optional[str] = None


class JobStatus(BaseModel):
    id: str
    status: str
    progress: int
    message: str
    clips: List[dict] = []
