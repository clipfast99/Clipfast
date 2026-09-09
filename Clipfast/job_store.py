import json
import os
from pathlib import Path
from threading import Lock

JOBS_DIR = Path("jobs")
JOBS_DIR.mkdir(exist_ok=True)
_lock = Lock()


def _path(job_id: str) -> Path:
    return JOBS_DIR / f"{job_id}.json"


def create(job) -> None:
    save(job)


def save(job) -> None:
    with _lock:
        with open(_path(job.id), "w") as f:
            json.dump(job.model_dump(), f)


def get(job_id: str):
    from models import JobStatus
    path = _path(job_id)
    if not path.exists():
        return None
    with open(path) as f:
        data = json.load(f)
    return JobStatus(**data)


def update(job_id: str, **fields) -> None:
    job = get(job_id)
    if job is None:
        return
    for k, v in fields.items():
        setattr(job, k, v)
    save(job)


def delete_old(max_age_hours: int = 24) -> int:
    import time
    cutoff = time.time() - (max_age_hours * 3600)
    removed = 0
    for f in JOBS_DIR.glob("*.json"):
        if f.stat().st_mtime < cutoff:
            f.unlink()
            removed += 1
    return removed
