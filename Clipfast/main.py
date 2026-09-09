from fastapi import FastAPI, UploadFile, File, Form, BackgroundTasks, HTTPException, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
import uvicorn, uuid, os
from pathlib import Path

from pipeline import process_video
from captions import burn_captions, STYLES
from reframe import reframe_video, trim_clip, get_video_info
from models import JobStatus
import job_store
import user_store
import stripe_payments
from clerk_auth import get_current_user, get_optional_user

app = FastAPI(title="ClipFast API")

BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

os.makedirs("uploads", exist_ok=True)
os.makedirs("outputs", exist_ok=True)
app.mount("/outputs", StaticFiles(directory="outputs"), name="outputs")


@app.on_event("startup")
async def on_startup():
    removed = job_store.delete_old(max_age_hours=24)
    if removed:
        print(f"Cleaned up {removed} old jobs.")


@app.get("/")
async def root():
    html_path = Path(__file__).parent / "index.html"
    if html_path.exists():
        return FileResponse(html_path)
    return {"status": "ClipFast API running"}


# ── User routes ────────────────────────────────────────────────────────────────

@app.get("/api/me")
async def get_me(user: dict = Depends(get_current_user)):
    status = user_store.get_status(user["clerk_user_id"])
    return {
        "clerk_user_id": user["clerk_user_id"],
        "email": user.get("email", ""),
        "name": user.get("name", ""),
        **status,
    }


# ── Stripe routes ──────────────────────────────────────────────────────────────

class CheckoutRequest(BaseModel):
    plan: str

@app.post("/api/checkout")
async def create_checkout(req: CheckoutRequest, user: dict = Depends(get_current_user)):
    url = stripe_payments.create_checkout_session(
        plan_key=req.plan,
        user_email=user.get("email", ""),
        success_url=f"{BASE_URL}?payment=success",
        cancel_url=f"{BASE_URL}?payment=cancelled",
    )
    return {"checkout_url": url}


@app.post("/api/billing-portal")
async def billing_portal(user: dict = Depends(get_current_user)):
    customer_id = user.get("stripe_customer_id")
    if not customer_id:
        raise HTTPException(400, "No active subscription found.")
    url = stripe_payments.create_billing_portal_session(customer_id, f"{BASE_URL}")
    return {"portal_url": url}


@app.post("/api/webhook")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")
    try:
        event = stripe_payments.handle_webhook(payload, sig_header)
    except HTTPException as e:
        raise e

    action = event.get("action")
    if action == "subscription_activated":
        u = user_store.get_by_stripe_customer(event["customer_id"])
        if not u:
            # Find by email
            for path in Path("users").glob("*.json"):
                import json
                with open(path) as f:
                    d = json.load(f)
                if d.get("email") == event.get("email"):
                    u = d
                    break
        if u:
            user_store.upgrade(u["clerk_user_id"], event["plan"], event["customer_id"])
    elif action in ("subscription_cancelled", "payment_failed"):
        u = user_store.get_by_stripe_customer(event["customer_id"])
        if u:
            user_store.downgrade(u["clerk_user_id"])

    return {"received": True}


# ── Video processing routes ────────────────────────────────────────────────────

@app.post("/api/process-url")
async def process_url(
    background_tasks: BackgroundTasks,
    youtube_url: str = Form(...),
    video_title: str = Form(default="Untitled Video"),
    user: dict = Depends(get_current_user),
):
    allowed, reason = user_store.can_process(user["clerk_user_id"])
    if not allowed:
        raise HTTPException(403, reason)

    job_id = str(uuid.uuid4())
    job_store.create(JobStatus(id=job_id, status="queued", progress=0, message="Job queued..."))
    background_tasks.add_task(run_pipeline, job_id, youtube_url, video_title, None, user["clerk_user_id"])
    return {"job_id": job_id}


@app.post("/api/process-file")
async def process_file(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    video_title: str = Form(default="Untitled Video"),
    user: dict = Depends(get_current_user),
):
    allowed, reason = user_store.can_process(user["clerk_user_id"])
    if not allowed:
        raise HTTPException(403, reason)

    if file.size and file.size > 500 * 1024 * 1024:
        raise HTTPException(400, "File too large. Max 500MB.")

    job_id = str(uuid.uuid4())
    ext = Path(file.filename).suffix or ".mp4"
    upload_path = f"uploads/{job_id}{ext}"
    with open(upload_path, "wb") as f:
        f.write(await file.read())

    job_store.create(JobStatus(id=job_id, status="queued", progress=0, message="File uploaded..."))
    background_tasks.add_task(run_pipeline, job_id, None, video_title, upload_path, user["clerk_user_id"])
    return {"job_id": job_id}


@app.get("/api/status/{job_id}")
async def get_status(job_id: str, user: dict = Depends(get_current_user)):
    job = job_store.get(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    return job


# ── Post-processing routes ─────────────────────────────────────────────────────

class CaptionRequest(BaseModel):
    clip_url: str
    style: str = "tiktok_white"

@app.post("/api/add-captions")
async def add_captions(req: CaptionRequest, background_tasks: BackgroundTasks, user: dict = Depends(get_current_user)):
    rel = req.clip_url.lstrip("/")
    if not os.path.exists(rel):
        raise HTTPException(404, "Clip not found")
    if req.style not in STYLES:
        raise HTTPException(400, f"Style must be one of: {list(STYLES.keys())}")

    is_free = user_store.get_status(user["clerk_user_id"])["plan"] == "free"
    stem = rel.replace(".mp4", "")
    output_path = f"{stem}_cap.mp4"
    cap_job_id = str(uuid.uuid4())
    job_store.create(JobStatus(id=cap_job_id, status="processing", progress=10, message="Generating captions..."))

    async def run_captions():
        try:
            job_store.update(cap_job_id, message="Burning captions...", progress=40)
            burn_captions(rel, output_path, req.style, watermark=is_free)
            job_store.update(cap_job_id, status="done", progress=100, message="Captions added!", clips=[{"file_url": f"/{output_path}"}])
        except Exception as e:
            job_store.update(cap_job_id, status="error", message=str(e))

    background_tasks.add_task(run_captions)
    return {"job_id": cap_job_id}


class ReframeRequest(BaseModel):
    clip_url: str
    aspect: str = "9:16"

@app.post("/api/reframe")
async def reframe(req: ReframeRequest, user: dict = Depends(get_current_user)):
    rel = req.clip_url.lstrip("/")
    if not os.path.exists(rel):
        raise HTTPException(404, "Clip not found")
    stem = rel.replace(".mp4", "")
    output_path = f"{stem}_{req.aspect.replace(':', 'x')}.mp4"
    reframe_video(rel, output_path, req.aspect)
    return {"file_url": f"/{output_path}"}


class TrimRequest(BaseModel):
    clip_url: str
    start_seconds: float
    end_seconds: float

@app.post("/api/trim")
async def trim(req: TrimRequest, user: dict = Depends(get_current_user)):
    rel = req.clip_url.lstrip("/")
    if not os.path.exists(rel):
        raise HTTPException(404, "Clip not found")
    if req.end_seconds <= req.start_seconds:
        raise HTTPException(400, "end must be after start")
    stem = rel.replace(".mp4", "")
    output_path = f"{stem}_trimmed.mp4"
    trim_clip(rel, output_path, req.start_seconds, req.end_seconds)
    return {"file_url": f"/{output_path}"}


# ── Pipeline runner ────────────────────────────────────────────────────────────

async def run_pipeline(job_id, url, title, file_path, clerk_user_id):
    def update(status, progress, message):
        job_store.update(job_id, status=status, progress=progress, message=message)
    try:
        result = await process_video(youtube_url=url, file_path=file_path, title=title, update_fn=update, job_id=job_id)
        job_store.update(job_id, status="done", progress=100, message=f"{len(result)} clips ready!", clips=result)
        total_sec = sum(c.get("duration_seconds", 0) for c in result)
        user_store.record_usage(clerk_user_id, total_sec / 60)
    except Exception as e:
        job_store.update(job_id, status="error", message=str(e))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
