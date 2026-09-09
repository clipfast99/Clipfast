import subprocess
import os
import re
from dataclasses import dataclass


@dataclass
class Word:
    text: str
    start: float
    end: float


@dataclass
class CaptionStyle:
    name: str
    font: str
    font_size: int
    primary_color: str
    highlight_color: str
    outline_color: str
    back_color: str
    bold: bool
    outline: float
    shadow: float
    margin_v: int


STYLES = {
    "tiktok_white": CaptionStyle(
        name="TikTok White", font="DejaVu Sans", font_size=22,
        primary_color="&H00FFFFFF", highlight_color="&H0000FFFF",
        outline_color="&H00000000", back_color="&H80000000",
        bold=True, outline=2.5, shadow=1.0, margin_v=80,
    ),
    "fire_yellow": CaptionStyle(
        name="Fire Yellow", font="DejaVu Sans", font_size=24,
        primary_color="&H0000FFFF", highlight_color="&H000000FF",
        outline_color="&H00000000", back_color="&HA0000000",
        bold=True, outline=3.0, shadow=1.5, margin_v=80,
    ),
    "clean_white": CaptionStyle(
        name="Clean White", font="DejaVu Sans", font_size=20,
        primary_color="&H00FFFFFF", highlight_color="&H00AAFFAA",
        outline_color="&H00000000", back_color="&H60000000",
        bold=False, outline=2.0, shadow=0.5, margin_v=90,
    ),
}


def get_word_timestamps(audio_path: str) -> list[Word]:
    import assemblyai as aai
    aai.settings.api_key = os.environ["ASSEMBLYAI_API_KEY"]
    config = aai.TranscriptionConfig(punctuate=True, format_text=True)
    transcriber = aai.Transcriber(config=config)
    transcript = transcriber.transcribe(audio_path)
    if transcript.status == aai.TranscriptStatus.error:
        raise RuntimeError(f"Transcription failed: {transcript.error}")
    words = []
    for w in transcript.words:
        clean = re.sub(r'[^\w\s\'\-]', '', w.text).strip()
        if clean:
            words.append(Word(text=clean, start=w.start / 1000, end=w.end / 1000))
    return words


def words_to_ass(words: list[Word], style: CaptionStyle, video_width=1080, video_height=1920) -> str:
    bold_val = -1 if style.bold else 0
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {video_width}
PlayResY: {video_height}
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{style.font},{style.font_size},{style.primary_color},{style.highlight_color},{style.outline_color},{style.back_color},{bold_val},0,0,0,100,100,0,0,1,{style.outline},{style.shadow},2,10,10,{style.margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    GROUP_SIZE = 5
    groups = [words[i:i+GROUP_SIZE] for i in range(0, len(words), GROUP_SIZE)]
    events = []
    for group in groups:
        if not group:
            continue
        line_end = group[-1].end
        for wi, current_word in enumerate(group):
            word_end = current_word.end if wi < len(group) - 1 else line_end
            parts = []
            for i, w in enumerate(group):
                if i == wi:
                    parts.append(f"{{\\c{style.highlight_color}\\b1}}{w.text}{{\\c{style.primary_color}\\b{bold_val}}}")
                else:
                    parts.append(w.text)
            text_line = " ".join(parts)
            start_ts = _secs_to_ass(current_word.start)
            end_ts = _secs_to_ass(word_end)
            events.append(f"Dialogue: 0,{start_ts},{end_ts},Default,,0,0,0,,{text_line}")
    return header + "\n".join(events)


def _secs_to_ass(secs: float) -> str:
    h = int(secs // 3600)
    m = int((secs % 3600) // 60)
    s = secs % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def burn_captions(video_path: str, output_path: str, style_name: str = "tiktok_white", watermark: bool = False):
    style = STYLES.get(style_name, STYLES["tiktok_white"])
    audio_path = video_path.replace(".mp4", "_cap_audio.mp3")
    subprocess.run(["ffmpeg", "-y", "-i", video_path, "-ac", "1", "-ar", "16000", audio_path], check=True, capture_output=True)
    words = get_word_timestamps(audio_path)
    os.remove(audio_path)
    if not words:
        subprocess.run(["ffmpeg", "-y", "-i", video_path, "-c", "copy", output_path], check=True)
        return
    ass_path = video_path.replace(".mp4", ".ass")
    with open(ass_path, "w", encoding="utf-8") as f:
        f.write(words_to_ass(words, style))
    filters = [f"ass={ass_path}"]
    if watermark:
        font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        filters.append(f"drawtext=fontfile={font_path}:text='ClipFast':x=20:y=40:fontsize=28:fontcolor=white@0.6:borderw=2:bordercolor=black@0.4")
    subprocess.run(["ffmpeg", "-y", "-i", video_path, "-vf", ",".join(filters), "-c:v", "libx264", "-preset", "fast", "-crf", "22", "-c:a", "aac", output_path], check=True, capture_output=True)
    if os.path.exists(ass_path):
        os.remove(ass_path)
