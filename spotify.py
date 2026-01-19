import os
import re
import time
import json
import asyncio
import shutil
import subprocess
import aiohttp

from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

SPOTIFY_STATE = {}  # uid -> spotify url


def is_spotify_url(text: str):
    return bool(re.search(r"(open\.spotify\.com/(track|album|playlist)/|spotify:track:)", text or ""))


def clean_spotify_url(url: str):
    return (url or "").strip().split("?")[0]


def _bin_exists(name: str):
    return shutil.which(name) is not None


def _spotdl_ok():
    return _bin_exists("spotdl")


def _ffmpeg_ok():
    return _bin_exists("ffmpeg") and _bin_exists("ffprobe")


def _ytdlp_ok():
    return _bin_exists("yt-dlp")


async def safe_edit(msg, text, reply_markup=None):
    try:
        await msg.edit(text, reply_markup=reply_markup)
    except:
        pass


def nice_name(path: str):
    base = os.path.basename(path)
    base = os.path.splitext(base)[0]
    base = re.sub(r"\s+", " ", base).strip()
    return base[:60]


# ✅ LOOPING ANIMATION (never stuck UI)
async def animated_processing(status, title, delay=0.3, cancel_kb=None):
    bar_len = 14
    i = 0
    while True:
        filled = i % (bar_len + 1)
        bar = "🟣" * filled + "⚪" * (bar_len - filled)
        text = f"{title}\n\n[{bar}]\n\n⏳ Please wait..."
        await safe_edit(status, text, reply_markup=cancel_kb)
        await asyncio.sleep(delay)
        i += 1


async def get_spotify_metadata(spotify_url: str):
    if not _spotdl_ok():
        return {}

    tmp_json = f"/tmp/sp_meta_{int(time.time())}.json"

    try:
        cmd = ["spotdl", "save", spotify_url, "--save-file", tmp_json]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        await proc.communicate()

        if not os.path.exists(tmp_json):
            return {}

        with open(tmp_json, "r", encoding="utf-8", errors="ignore") as f:
            data = json.load(f)

        if isinstance(data, list) and data:
            data = data[0]

        if not isinstance(data, dict):
            return {}

        title = data.get("name") or data.get("title") or ""
        artists = data.get("artists") or data.get("artist") or ""
        cover_url = (
            data.get("cover_url")
            or data.get("coverUrl")
            or data.get("album_cover_url")
            or data.get("albumCoverUrl")
            or ""
        )

        if isinstance(artists, list):
            artists = ", ".join([str(x) for x in artists if x])

        return {
            "title": str(title).strip(),
            "artists": str(artists).strip(),
            "cover_url": str(cover_url).strip()
        }

    except:
        return {}
    finally:
        try:
            if os.path.exists(tmp_json):
                os.remove(tmp_json)
        except:
            pass


async def fetch_album_cover_from_url(cover_url: str, save_path: str):
    try:
        if not cover_url:
            return None

        timeout = aiohttp.ClientTimeout(total=25)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(cover_url) as r:
                if r.status != 200:
                    return None
                content = await r.read()

        with open(save_path, "wb") as f:
            f.write(content)

        if os.path.exists(save_path) and os.path.getsize(save_path) > 0:
            return save_path
    except:
        return None
    return None


def build_best_queries(title: str, artists: str):
    title = (title or "").strip()
    artists = (artists or "").strip()
    if not title:
        return []

    base = f"{title} {artists}".strip()
    return [
        f"{base} audio",
        f"{base} official audio",
        f"{base} topic",
        f"{base} lyrics",
        f"{title} {artists} song",
    ]


async def download_with_spotdl(spotify_url: str, out_dir: str, timeout_sec=420):
    if not _spotdl_ok():
        return None

    cmd = ["spotdl", spotify_url, "--output", out_dir, "--audio", "youtube-music"]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    try:
        await asyncio.wait_for(proc.communicate(), timeout=timeout_sec)
    except asyncio.TimeoutError:
        proc.kill()
        return None

    for root, _, files in os.walk(out_dir):
        for f in files:
            if f.lower().endswith(".mp3"):
                return os.path.join(root, f)
    return None


async def download_with_ytdlp_queries(queries: list, out_dir: str, timeout_sec=420):
    if not _ytdlp_ok():
        return None

    outtmpl = os.path.join(out_dir, "%(title).80s.%(ext)s")

    extra = [
        "--geo-bypass",
        "--no-check-certificates",
        "--extractor-args", "youtube:player_client=android"
    ]

    for q in queries:
        cmd = [
            "yt-dlp",
            f"ytsearch1:{q}",
            "-x",
            "--audio-format", "mp3",
            "--audio-quality", "0",
            "-o", outtmpl,
            "--no-playlist",
            *extra
        ]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        try:
            await asyncio.wait_for(proc.communicate(), timeout=timeout_sec)
        except asyncio.TimeoutError:
            proc.kill()
            continue

        for root, _, files in os.walk(out_dir):
            for f in files:
                if f.lower().endswith(".mp3"):
                    return os.path.join(root, f)

    return None


async def spotify_auto_download(
    client,
    message,
    spotify_url: str,
    USER_TASKS,
    USER_CANCEL,
    get_or_create_status,
    main_menu_keyboard,
    DOWNLOAD_DIR
):
    uid = message.from_user.id
    spotify_url = clean_spotify_url(spotify_url)
    SPOTIFY_STATE[uid] = spotify_url

    status = await get_or_create_status(message, uid)
    await safe_edit(status, "🎧 **Spotify Link Detected**\n\n⏳ Processing started...")
    await asyncio.sleep(0.4)

    async def job():
        out_dir = os.path.join(DOWNLOAD_DIR, f"spotify_{uid}_{int(time.time())}")
        os.makedirs(out_dir, exist_ok=True)

        kb_cancel = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_{uid}")]])

        thumb_path = None
        audio_path = None
        anim_task = None

        try:
            USER_CANCEL.discard(uid)

            if not _ffmpeg_ok():
                raise Exception("ffmpeg not installed. Run: apt-get install -y ffmpeg")

            if not _spotdl_ok():
                raise Exception("spotdl not installed. Run: pip install -U spotdl")

            if not _ytdlp_ok():
                raise Exception("yt-dlp not installed. Run: pip install -U yt-dlp")

            # metadata
            await safe_edit(status, "🔎 Fetching Spotify metadata...\n\n⏳ Please wait...", kb_cancel)
            meta = await get_spotify_metadata(spotify_url)
            title = meta.get("title", "")
            artists = meta.get("artists", "")
            cover_url = meta.get("cover_url", "")

            # cover
            anim_task = asyncio.create_task(animated_processing(status, "🖼 Fetching Album Cover...", 0.25, kb_cancel))
            thumb_path = await fetch_album_cover_from_url(cover_url, os.path.join(out_dir, "cover.jpg"))
            anim_task.cancel()

            # spotdl
            anim_task = asyncio.create_task(animated_processing(status, "⬇️ Downloading MP3 (spotdl)...", 0.30, kb_cancel))
            audio_path = await download_with_spotdl(spotify_url, out_dir, timeout_sec=300)  # ✅ 5 min
            anim_task.cancel()

            if uid in USER_CANCEL:
                raise asyncio.CancelledError

            # fallback
            if not audio_path:
                await safe_edit(status, "⚠️ spotdl stuck/blocked.\n\n🔁 Switching to yt-dlp 99% mode...", kb_cancel)
                await asyncio.sleep(1)

                queries = build_best_queries(title, artists)
                if not queries:
                    queries = [spotify_url]

                anim_task = asyncio.create_task(animated_processing(status, "⬇️ Downloading MP3 (yt-dlp fallback)...", 0.28, kb_cancel))
                audio_path = await download_with_ytdlp_queries(queries, out_dir, timeout_sec=300)
                anim_task.cancel()

            if not audio_path:
                raise Exception("Song not downloaded.\nYouTube blocked or match not found.\nTry later.")

            # upload
            anim_task = asyncio.create_task(animated_processing(status, "📤 Uploading MP3...", 0.28, kb_cancel))

            await client.send_audio(
                chat_id=message.chat.id,
                audio=audio_path,
                thumb=thumb_path if thumb_path and os.path.exists(thumb_path) else None,
                caption=(
                    "✅ **Spotify Download Completed** 🎵\n\n"
                    f"🎧 **{nice_name(audio_path)}**\n"
                    f"👤 Artist: **{artists or 'Unknown'}**\n"
                    "📌 Format: **MP3**\n"
                    "🖼 Thumb: **Album Cover** ✅"
                )
            )

            anim_task.cancel()
            await safe_edit(status, "✅ Done ✅", reply_markup=main_menu_keyboard())

        except asyncio.CancelledError:
            try:
                if anim_task:
                    anim_task.cancel()
            except:
                pass
            await safe_edit(status, "❌ Cancelled ✅", reply_markup=main_menu_keyboard())

        except Exception as e:
            try:
                if anim_task:
                    anim_task.cancel()
            except:
                pass
            await safe_edit(status, f"❌ **Spotify Download Failed**\n\nError:\n`{e}`", reply_markup=main_menu_keyboard())

        finally:
            SPOTIFY_STATE.pop(uid, None)
            USER_CANCEL.discard(uid)

            # cleanup
            try:
                if os.path.exists(out_dir):
                    for r, d, f in os.walk(out_dir, topdown=False):
                        for ff in f:
                            try:
                                os.remove(os.path.join(r, ff))
                            except:
                                pass
                        for dd in d:
                            try:
                                os.rmdir(os.path.join(r, dd))
                            except:
                                pass
                    try:
                        os.rmdir(out_dir)
                    except:
                        pass
            except:
                pass

    USER_TASKS[uid] = asyncio.create_task(job())
