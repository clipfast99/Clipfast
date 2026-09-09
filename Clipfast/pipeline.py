import asyncio
import json
import os
import subprocess
from typing import Callable

import anthropic
import assemblyai as aai
import yt_dlp

anthropic_client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
aai.settings.api_key = os.environ["ASSEMBLYAI_API_KEY"]

SYSTEM_PROMPT = """You are an expert viral content strategist with deep knowledge of what makes
short-form video go viral on TikTok, Instagram Reels, and YouTube Shorts.

You will be given a full video transcript with timestamps. Identify the 3 to 7 best clips
that have the highest viral potential.

A great viral clip has ONE OR MORE of these qualities:
- Strong hook in the first 3 seconds (surprising stat, bold claim, question)
- Emotional arc: tension to resolution, or problem to solution
- Quotable one-liner or memorable punchline
- Controversial or counterintuitive take
- Clear self-contained story — no context needed from the full video
- Ends on a high note (insight, laugh, or cliffhanger)

Be brutally honest with viral_score. Most clips score 60-75. Only award 85+ if the hook
is genuinely exceptional.

Respond ONLY with a valid JSON array. No explanation, no markdown, no extra text."""


def download_youtube(url: str, job_id: str) -> str:
    out_path = f"uploads/{job_id}.mp4"
    ydl_opts = {
        "format": "bestvideo[ext=mp4][height<=1080]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "outtmpl": out_path,
        "quiet": True,
        "merge_output_format": "mp4",
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])
    return out_path


def extract_audio(video_path: str) -> str:
    audio_path = video_path.replace(".mp4", ".mp3").replace(".mkv", ".mp3")
    subprocess.run(["ffmpeg", "-y", "-i", video_path, "-ac", "1", "-ar", "16000", "-q:a", "0", audio_path], check=True, capture_output=True)
    return audio_path


async def transcribe(audio_path: str) -> str:
    config = aai.TranscriptionConfig(speaker_labels=True, punctuate=True, format_text=True)
    transcriber = aai.Transcriber(config=config)
    transcript = await asyncio.to_thread(transcriber.transcribe, audio_path)
    if transcript.status == aai.TranscriptStatus.error:
        raise RuntimeError(f"Transcription failed: {transcript.error}")
    lines = []
    if transcript.utterances:
        for utt in transcript.utterances:
            ts = format_time(utt.start / 1000)
            lines.append(f"[{ts}] Speaker {utt.speaker}: {utt.text}")
    else:
        lines.append(transcript.text or "")
    return "\n".join(lines)


def extract_viral_clips(transcript: str, title: str, duration_min: int) -> list[dict]:
    user_prompt = f"""Here is the transcript of a {duration_min}-minute video titled "{title}":

{transcript}

---

Identify the best viral clips. Return ONLY a JSON array:
[
  {{
    "clip_number": 1,
    "start_time": "00:04:32",
    "end_time": "00:05:18",
    "duration_seconds": 46,
    "title": "Short punchy title max 8 words",
    "hook": "The exact first sentence of this clip",
    "why_viral": "One sentence explaining the viral potential",
    "viral_score": 87,
    "best_platform": "TikTok",
    "emotion": "surprise",
    "clip_type": "hot_take",
    "suggested_caption": "Caption with 3-4 relevant hashtags"
  }}
]

Rules:
- start_time and end_time must be exact timestamps from the transcript
- duration_seconds must be between 30 and 90
- viral_score must be between 60 and 100
- clip_type: hot_take, story, tutorial, reaction, insight, funny, controversy
- emotion: surprise, inspiration, humor, anger, curiosity, nostalgia, fear
- best_platform: TikTok, Reels, Shorts, All
- Return 3 to 7 clips, ordered by viral_score descending
- Only use timestamps that appear exactly in the transcript"""

    message = anthropic_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )
    raw = message.content[0].text.strip()
    if raw.startswith("```"):
        parts = raw.split("```")
        raw = parts[1]
        if raw.startswith("json"):
            raw = raw[4:]
    clips = json.loads(raw.strip())
    return sorted(clips, key=lambda x: x["viral_score"], reverse=True)


def cut_clip(source: str, start: str, end: str, out_path: str):
    # Clamp timestamps to video duration
    duration = get_duration(source)
    def ts_to_sec(ts):
        parts = ts.split(":")
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    start_sec = min(ts_to_sec(start), duration - 1)
    end_sec = min(ts_to_sec(end), duration)
    if end_sec <= start_sec:
        end_sec = min(start_sec + 30, duration)
    subprocess.run([
        "ffmpeg", "-y", "-i", source,
        "-ss", str(start_sec), "-to", str(end_sec),
        "-c:v", "libx264", "-preset", "fast", "-c:a", "aac",
        "-vf", "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black",
        out_path,
    ], check=True, capture_output=True)


async def process_video(youtube_url, file_path, title, update_fn: Callable, job_id: str) -> list[dict]:
    if youtube_url:
        update_fn("downloading", 10, "Downloading video...")
        video_path = await asyncio.to_thread(download_youtube, youtube_url, job_id)
    else:
        video_path = file_path

    update_fn("transcribing", 25, "Extracting audio...")
    audio_path = await asyncio.to_thread(extract_audio, video_path)

    update_fn("transcribing", 40, "Transcribing with AssemblyAI...")
    transcript = await transcribe(audio_path)

    duration_sec = get_duration(video_path)
    duration_min = max(1, int(duration_sec // 60))

    update_fn("analyzing", 60, "Finding viral moments with AI...")
    clips = await asyncio.to_thread(extract_viral_clips, transcript, title, duration_min)

    update_fn("cutting", 75, f"Cutting {len(clips)} clips...")
    os.makedirs(f"outputs/{job_id}", exist_ok=True)

    output_clips = []
    for i, clip in enumerate(clips):
        out_path = f"outputs/{job_id}/clip_{clip['clip_number']}.mp4"
        await asyncio.to_thread(cut_clip, video_path, clip["start_time"], clip["end_time"], out_path)
        output_clips.append({**clip, "file_url": f"/outputs/{job_id}/clip_{clip['clip_number']}.mp4"})
        update_fn("cutting", 75 + int((i + 1) / len(clips) * 20), f"Cut clip {i+1} of {len(clips)}...")

    return output_clips


def get_duration(path: str) -> float:
    result = subprocess.run(["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", path], capture_output=True, text=True, check=True)
    data = json.loads(result.stdout)
    return float(data["format"]["duration"])


def format_time(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h:02d}:{m:02d}:{s:02d}"
