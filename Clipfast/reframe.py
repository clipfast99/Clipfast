import subprocess
import json

ASPECT_RATIOS = {
    "9:16": (1080, 1920),
    "1:1":  (1080, 1080),
    "16:9": (1920, 1080),
    "4:5":  (1080, 1350),
}


def reframe_video(input_path: str, output_path: str, aspect: str = "9:16") -> str:
    if aspect not in ASPECT_RATIOS:
        aspect = "9:16"
    w, h = ASPECT_RATIOS[aspect]
    vf = f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black,setsar=1"
    subprocess.run(["ffmpeg", "-y", "-i", input_path, "-vf", vf, "-c:v", "libx264", "-preset", "fast", "-crf", "22", "-c:a", "aac", output_path], check=True, capture_output=True)
    return output_path


def trim_clip(input_path: str, output_path: str, start_seconds: float, end_seconds: float) -> str:
    duration = end_seconds - start_seconds
    subprocess.run(["ffmpeg", "-y", "-i", input_path, "-ss", str(start_seconds), "-t", str(duration), "-c:v", "libx264", "-preset", "fast", "-c:a", "aac", output_path], check=True, capture_output=True)
    return output_path


def get_video_info(path: str) -> dict:
    result = subprocess.run(["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", "-show_format", path], capture_output=True, text=True, check=True)
    data = json.loads(result.stdout)
    video_stream = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), {})
    return {
        "width": video_stream.get("width", 1920),
        "height": video_stream.get("height", 1080),
        "duration": float(data["format"].get("duration", 0)),
    }
