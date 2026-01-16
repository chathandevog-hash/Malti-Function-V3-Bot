import os
import re
import time
import asyncio

DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "downloads")

YT_REGEX = re.compile(
    r"(https?://)?(www\.)?(youtube\.com/watch\?v=|youtu\.be/|youtube\.com/shorts/)[A-Za-z0-9_\-]+"
)

def is_youtube_url(text: str) -> bool:
    return bool(YT_REGEX.search(text or ""))

def clean_youtube_url(text: str) -> str:
    return (text or "").strip()

def _yt_quality_format(q: str):
    """
    Return yt-dlp format selector.
    q example: 1080p, 720p, 360p...
    """
    h = int(q.replace("p", ""))
    return f"bv*[height<={h}]+ba/b[height<={h}]/best"

def _audio_quality_args(kbps: str):
    """
    kbps example: 320, 192, 128, 64
    """
    return ["--audio-quality", f"{kbps}K"]

async def youtube_download_video(url: str, uid: int, quality: str, status_msg=None):
    """
    Download YouTube as MP4 video.
    Returns mp4 filepath.
    """
    import subprocess

    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    url = clean_youtube_url(url)
    outtmpl = os.path.join(DOWNLOAD_DIR, f"yt_{uid}_{int(time.time())}.%(ext)s")
    fmt = _yt_quality_format(quality)

    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--no-warnings",
        "--newline",
        "--retries", "5",
        "--socket-timeout", "30",
        "-f", fmt,
        "--merge-output-format", "mp4",
        "-o", outtmpl,
        url
    ]

    if status_msg:
        try:
            await status_msg.edit(f"📥 Downloading YouTube Video ({quality})...\n⏳ Please wait...")
        except:
            pass

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT
    )
    await proc.communicate()

    if proc.returncode != 0:
        raise Exception("YouTube download failed (restricted / blocked / private).")

    mp4_path = outtmpl.replace("%(ext)s", "mp4")
    if os.path.exists(mp4_path):
        return mp4_path

    # fallback scan
    files = [f for f in os.listdir(DOWNLOAD_DIR) if f.startswith(f"yt_{uid}_") and f.endswith(".mp4")]
    if not files:
        raise Exception("Downloaded MP4 not found.")
    files.sort(key=lambda x: os.path.getmtime(os.path.join(DOWNLOAD_DIR, x)), reverse=True)
    return os.path.join(DOWNLOAD_DIR, files[0])

async def youtube_download_file(url: str, uid: int, quality: str, status_msg=None):
    """
    Download YouTube as mp4 and return path.
    (bot will send as Document)
    """
    return await youtube_download_video(url, uid, quality, status_msg=status_msg)

async def youtube_download_audio(url: str, uid: int, kbps: str, status_msg=None):
    """
    Download YouTube audio as MP3.
    Returns mp3 filepath.
    """
    import subprocess

    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    url = clean_youtube_url(url)
    outtmpl = os.path.join(DOWNLOAD_DIR, f"yta_{uid}_{int(time.time())}.%(ext)s")

    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--no-warnings",
        "--newline",
        "--retries", "5",
        "--socket-timeout", "30",
        "-x",
        "--audio-format", "mp3",
        "-o", outtmpl,
        url
    ] + _audio_quality_args(kbps)

    if status_msg:
        try:
            await status_msg.edit(f"🎵 Downloading YouTube Audio ({kbps}kbps)...\n⏳ Please wait...")
        except:
            pass

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT
    )
    await proc.communicate()

    if proc.returncode != 0:
        raise Exception("YouTube audio download failed (restricted / blocked / private).")

    mp3_path = outtmpl.replace("%(ext)s", "mp3")
    if os.path.exists(mp3_path):
        return mp3_path

    # fallback scan
    files = [f for f in os.listdir(DOWNLOAD_DIR) if f.startswith(f"yta_{uid}_") and f.endswith(".mp3")]
    if not files:
        raise Exception("Downloaded MP3 not found.")
    files.sort(key=lambda x: os.path.getmtime(os.path.join(DOWNLOAD_DIR, x)), reverse=True)
    return os.path.join(DOWNLOAD_DIR, files[0])
