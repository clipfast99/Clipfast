import os, uuid, json, subprocess, asyncio
from pathlib import Path
from fastapi import FastAPI, UploadFile, File, Form, BackgroundTasks, HTTPException, Request, Depends, Security
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import anthropic
import assemblyai as aai
import yt_dlp
import stripe
import jwt as pyjwt
import httpx
import base64

# ── Setup ──────────────────────────────────────────────────────────────────────
app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

for d in ["uploads","outputs","jobs","users"]:
    os.makedirs(d, exist_ok=True)

anthropic_client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
aai.settings.api_key = os.environ["ASSEMBLYAI_API_KEY"]
stripe.api_key = os.environ.get("STRIPE_SECRET_KEY","")
BASE_URL = os.environ.get("BASE_URL","http://localhost:8000")

app.mount("/outputs", StaticFiles(directory="outputs"), name="outputs")

# ── Frontend ───────────────────────────────────────────────────────────────────
@app.get("/")
async def root():
    p = Path(__file__).parent / "index.html"
    return FileResponse(str(p)) if p.exists() else {"status":"ClipFast running"}

# ── Clerk Auth ─────────────────────────────────────────────────────────────────
_bearer = HTTPBearer(auto_error=False)
_jwks_cache = {}

def _clerk_fapi():
    pk = os.environ.get("CLERK_PUBLISHABLE_KEY","")
    try:
        part = pk.split("_")[2]
        decoded = base64.b64decode(part + "==").decode()
        return decoded.rstrip("$")
    except:
        return ""

async def _get_jwks():
    fapi = _clerk_fapi()
    if not fapi: return {}
    if fapi in _jwks_cache: return _jwks_cache[fapi]
    try:
        async with httpx.AsyncClient() as c:
            r = await c.get(f"https://{fapi}/.well-known/jwks.json", timeout=10)
            _jwks_cache[fapi] = r.json()
        return _jwks_cache[fapi]
    except:
        return {}

async def get_user(creds: HTTPAuthorizationCredentials = Security(_bearer)) -> dict:
    if not creds:
        raise HTTPException(401, "Not authenticated")
    token = creds.credentials
    try:
        header = pyjwt.get_unverified_header(token)
        jwks = await _get_jwks()
        key_data = next((k for k in jwks.get("keys",[]) if k.get("kid") == header.get("kid")), None)
        if not key_data:
            raise HTTPException(401, "Invalid token key")
        pub = pyjwt.algorithms.RSAAlgorithm.from_jwk(key_data)
        payload = pyjwt.decode(token, pub, algorithms=["RS256"], options={"verify_aud":False})
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(401, f"Token error: {str(e)}")

    uid = payload.get("sub","")
    upath = Path(f"users/{uid}.json")
    if upath.exists():
        user = json.loads(upath.read_text())
    else:
        user = {
            "id": uid,
            "email": payload.get("email",""),
            "plan": "free",
            "videos_used": 0,
            "stripe_customer_id": None
        }
        upath.write_text(json.dumps(user, indent=2))
    return user

def save_user(user: dict):
    Path(f"users/{user['id']}.json").write_text(json.dumps(user, indent=2))

# ── Job helpers ────────────────────────────────────────────────────────────────
def job_get(jid: str) -> dict | None:
    p = Path(f"jobs/{jid}.json")
    return json.loads(p.read_text()) if p.exists() else None

def job_set(jid: str, **kw):
    p = Path(f"jobs/{jid}.json")
    data = json.loads(p.read_text()) if p.exists() else {"id": jid}
    data.update(kw)
    p.write_text(json.dumps(data))

# ── Pipeline ───────────────────────────────────────────────────────────────────
SYSTEM = """You are a viral content strategist. Find 3-7 clips with highest viral potential for TikTok, Reels, Shorts. Respond ONLY with valid JSON array, no markdown."""

def get_duration(path: str) -> float:
    r = subprocess.run(
        ["ffprobe","-v","quiet","-print_format","json","-show_format", path],
        capture_output=True, text=True
    )
    return float(json.loads(r.stdout)["format"]["duration"])

def ts_to_sec(ts: str) -> float:
    p = ts.split(":")
    return int(p[0])*3600 + int(p[1])*60 + float(p[2])

async def run_pipeline(job_id: str, url, fpath, title: str, uid: str):
    def upd(status, pct, msg):
        job_set(job_id, status=status, progress=pct, message=msg)
    try:
        # Download
        if url:
            upd("downloading", 10, "Downloading video...")
            vpath = f"uploads/{job_id}.mp4"
            with yt_dlp.YoutubeDL({
                "format":"bestvideo[ext=mp4][height<=1080]+bestaudio[ext=m4a]/best",
                "outtmpl": vpath, "quiet": True, "merge_output_format":"mp4"
            }) as ydl:
                await asyncio.to_thread(ydl.download, [url])
        else:
            vpath = fpath

        # Transcribe
        upd("transcribing", 30, "Transcribing...")
        apath = vpath.rsplit(".",1)[0] + ".mp3"
        subprocess.run(
            ["ffmpeg","-y","-i",vpath,"-ac","1","-ar","16000", apath],
            capture_output=True
        )
        cfg = aai.TranscriptionConfig(speaker_labels=True, punctuate=True)
        tr = await asyncio.to_thread(aai.Transcriber(config=cfg).transcribe, apath)
        if tr.status == aai.TranscriptStatus.error:
            raise RuntimeError(tr.error)

        def fmt(ms):
            s = ms/1000
            return f"{int(s//3600):02d}:{int(s%3600//60):02d}:{int(s%60):02d}"

        transcript = "\n".join(
            f"[{fmt(u.start)}] Speaker {u.speaker}: {u.text}"
            for u in (tr.utterances or [])
        ) or (tr.text or "")

        dur = int(get_duration(vpath) // 60)

        # Claude
        upd("analyzing", 60, "Finding viral moments...")
        msg = anthropic_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            system=SYSTEM,
            messages=[{"role":"user","content":
                f'Transcript of {dur}-min video "{title}":\n\n{transcript}\n\n'
                'Return JSON array with: clip_number, start_time(HH:MM:SS), end_time(HH:MM:SS), '
                'duration_seconds(30-90), title, hook, why_viral, viral_score(60-100), '
                'best_platform(TikTok/Reels/Shorts/All), clip_type(hot_take/story/insight/funny/tutorial), '
                'suggested_caption. Order by viral_score desc.'
            }]
        )
        raw = msg.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        clips = sorted(json.loads(raw.strip()), key=lambda x: x["viral_score"], reverse=True)

        # Cut
        upd("cutting", 75, f"Cutting {len(clips)} clips...")
        os.makedirs(f"outputs/{job_id}", exist_ok=True)
        total_dur = get_duration(vpath)
        result = []

        for i, clip in enumerate(clips):
            out = f"outputs/{job_id}/clip_{clip['clip_number']}.mp4"
            s = min(ts_to_sec(clip["start_time"]), total_dur - 1)
            e = min(ts_to_sec(clip["end_time"]), total_dur)
            if e <= s: e = min(s + 30, total_dur)
            subprocess.run([
                "ffmpeg","-y","-i",vpath,
                "-ss",str(s),"-to",str(e),
                "-c:v","libx264","-preset","fast","-c:a","aac",
                "-vf","scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black",
                out
            ], capture_output=True)
            result.append({**clip, "file_url": f"/outputs/{job_id}/clip_{clip['clip_number']}.mp4"})
            upd("cutting", 75 + int((i+1)/len(clips)*20), f"Cut {i+1}/{len(clips)}...")

        # Record
        upath = Path(f"users/{uid}.json")
        if upath.exists():
            u = json.loads(upath.read_text())
            u["videos_used"] = u.get("videos_used",0) + 1
            upath.write_text(json.dumps(u, indent=2))

        job_set(job_id, status="done", progress=100, message=f"{len(result)} clips ready!", clips=result)

    except Exception as e:
        job_set(job_id, status="error", message=str(e))

# ── Routes ─────────────────────────────────────────────────────────────────────
@app.get("/api/me")
async def me(user: dict = Depends(get_user)):
    plan = user.get("plan","free")
    videos_used = user.get("videos_used",0)
    return {
        **user,
        "videos_left": max(0, 1 - videos_used) if plan == "free" else 9999,
        "minutes_remaining": 200 if plan == "creator" else 600 if plan == "pro" else None,
    }

@app.post("/api/process-url")
async def process_url(
    bg: BackgroundTasks,
    youtube_url: str = Form(...),
    video_title: str = Form(default="My Video"),
    user: dict = Depends(get_user)
):
    if user.get("plan","free") == "free" and user.get("videos_used",0) >= 1:
        raise HTTPException(403, "Your free video has been used. Upgrade to process more.")
    jid = str(uuid.uuid4())
    job_set(jid, status="queued", progress=0, message="Queued...", clips=[])
    bg.add_task(run_pipeline, jid, youtube_url, None, video_title, user["id"])
    return {"job_id": jid}

@app.post("/api/process-file")
async def process_file(
    bg: BackgroundTasks,
    file: UploadFile = File(...),
    video_title: str = Form(default="My Video"),
    user: dict = Depends(get_user)
):
    if user.get("plan","free") == "free" and user.get("videos_used",0) >= 1:
        raise HTTPException(403, "Your free video has been used. Upgrade to process more.")
    jid = str(uuid.uuid4())
    ext = Path(file.filename).suffix or ".mp4"
    fpath = f"uploads/{jid}{ext}"
    content = await file.read()
    with open(fpath, "wb") as f:
        f.write(content)
    job_set(jid, status="queued", progress=0, message="Uploaded...", clips=[])
    bg.add_task(run_pipeline, jid, None, fpath, video_title, user["id"])
    return {"job_id": jid}

@app.get("/api/status/{jid}")
async def status(jid: str, user: dict = Depends(get_user)):
    job = job_get(jid)
    if not job: raise HTTPException(404, "Job not found")
    return job

@app.post("/api/reframe")
async def reframe(
    clip_url: str = Form(...),
    aspect: str = Form(default="9:16"),
    user: dict = Depends(get_user)
):
    sizes = {"9:16":(1080,1920),"1:1":(1080,1080),"16:9":(1920,1080),"4:5":(1080,1350)}
    w,h = sizes.get(aspect,(1080,1920))
    rel = clip_url.lstrip("/")
    out = rel.replace(".mp4", f"_{aspect.replace(':','x')}.mp4")
    subprocess.run([
        "ffmpeg","-y","-i",rel,
        "-vf",f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black",
        "-c:v","libx264","-preset","fast","-c:a","aac",out
    ], capture_output=True)
    return {"file_url": f"/{out}"}

@app.post("/api/trim")
async def trim(
    clip_url: str = Form(...),
    start_seconds: float = Form(...),
    end_seconds: float = Form(...),
    user: dict = Depends(get_user)
):
    rel = clip_url.lstrip("/")
    out = rel.replace(".mp4","_trimmed.mp4")
    subprocess.run([
        "ffmpeg","-y","-i",rel,
        "-ss",str(start_seconds),"-t",str(end_seconds-start_seconds),
        "-c:v","libx264","-preset","fast","-c:a","aac",out
    ], capture_output=True)
    return {"file_url": f"/{out}"}

# ── Stripe ─────────────────────────────────────────────────────────────────────
PRICE_IDS = {
    "creator": os.environ.get("STRIPE_CREATOR_PRICE_ID",""),
    "pro": os.environ.get("STRIPE_PRO_PRICE_ID",""),
}

@app.post("/api/checkout")
async def checkout(plan: str = Form(...), user: dict = Depends(get_user)):
    if plan not in PRICE_IDS:
        raise HTTPException(400, "Invalid plan")
    session = stripe.checkout.Session.create(
        mode="subscription",
        line_items=[{"price": PRICE_IDS[plan], "quantity": 1}],
        customer_email=user.get("email",""),
        success_url=f"{BASE_URL}?payment=success",
        cancel_url=BASE_URL,
        metadata={"user_id": user["id"], "plan": plan},
    )
    return {"checkout_url": session.url}

@app.post("/api/webhook")
async def webhook(request: Request):
    payload = await request.body()
    sig = request.headers.get("stripe-signature","")
    try:
        event = stripe.Webhook.construct_event(
            payload, sig, os.environ.get("STRIPE_WEBHOOK_SECRET","")
        )
    except Exception:
        raise HTTPException(400, "Invalid signature")

    if event["type"] == "checkout.session.completed":
        data = event["data"]["object"]
        uid = data.get("metadata",{}).get("user_id","")
        plan = data.get("metadata",{}).get("plan","")
        customer = data.get("customer","")
        upath = Path(f"users/{uid}.json")
        if upath.exists():
            u = json.loads(upath.read_text())
            u["plan"] = plan
            u["stripe_customer_id"] = customer
            upath.write_text(json.dumps(u, indent=2))

    elif event["type"] in ("customer.subscription.deleted","invoice.payment_failed"):
        customer = event["data"]["object"].get("customer","")
        for p in Path("users").glob("*.json"):
            u = json.loads(p.read_text())
            if u.get("stripe_customer_id") == customer:
                u["plan"] = "free"
                p.write_text(json.dumps(u, indent=2))
                break

    return {"ok": True}

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("server:app", host="0.0.0.0", port=port)
