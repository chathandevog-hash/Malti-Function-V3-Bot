import os
import re
import time
import asyncio
import shutil
import subprocess
import aiohttp

from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

SPOTIFY_STATE = {}  # uid -> spotify url


# -------------------------
# Utils
# -------------------------
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


# -------------------------
# Album Cover Fetch ✅
# -------------------------
async def fetch_album_cover(spotify_url: str, save_path: str):
    """
    Uses spotdl save metadata -> gets cover url -> downloads it.
    """
    try:
        if not _spotdl_ok():
            return None

        meta_json = save_path + ".json"

        cmd = ["spotdl", "save", spotify_url, "--save-file", meta_json]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        await proc.communicate()

        if not os.path.exists(meta_json):
            return None

        import json
        with open(meta_json, "r", encoding="utf-8", errors="ignore") as f:
            data = json.load(f)

        cover_url = None
        if isinstance(data, dict):
            cover_url = (
                data.get("cover_url")
                or data.get("coverUrl")
                or data.get("album_cover_url")
                or data.get("albumCoverUrl")
            )

        if not cover_url and isinstance(data, list) and data:
            d0 = data[0]
            cover_url = (
                d0.get("cover_url")
                or d0.get("coverUrl")
                or d0.get("album_cover_url")
                or d0.get("albumCoverUrl")
            )

        try:
            os.remove(meta_json)
        except:
            pass

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


# -------------------------
# AUTO START DOWNLOAD ✅
# -------------------------
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

    # ✅ instant UI
    await safe_edit(
        status,
        "🎧 **Spotify Link Detected**\n\n"
        "⏳ Processing started...\n"
        "🔍 Checking tools..."
    )
    await asyncio.sleep(0.5)

    async def job():
        out_dir = os.path.join(DOWNLOAD_DIR, f"spotify_{uid}_{int(time.time())}")
        os.makedirs(out_dir, exist_ok=True)

        thumb_path = None
        audio_path = None

        try:
            USER_CANCEL.discard(uid)

            if not _spotdl_ok():
                raise Exception("spotdl not installed. Run: pip install -U spotdl")

            if not _ffmpeg_ok():
                raise Exception("ffmpeg not installed. Run: apt-get install -y ffmpeg")

            kb_cancel = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_{uid}")]])

            # ✅ fetch album cover
            await safe_edit(status, "🖼 Fetching album cover...\n\n⏳ Please wait...", kb_cancel)
            thumb_path = await fetch_album_cover(spotify_url, os.path.join(out_dir, "cover.jpg"))

            # ✅ spotdl download
            await safe_edit(status, "⬇️ Downloading MP3...\n\n⏳ Please wait...", kb_cancel)

            cmd = ["spotdl", spotify_url, "--output", out_dir]
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )

            # cancel support
            while True:
                if uid in USER_CANCEL:
                    proc.kill()
                    raise asyncio.CancelledError
                if proc.returncode is not None:
                    break
                await asyncio.sleep(0.5)

            await proc.communicate()

            # find mp3
            for root, dirs, files in os.walk(out_dir):
                for f in files:
                    if f.lower().endswith(".mp3"):
                        audio_path = os.path.join(root, f)
                        break
                if audio_path:
                    break

            if not audio_path:
                raise Exception("Song not downloaded. Try again later / another track.")

            title = nice_name(audio_path)

            # upload
            await safe_edit(status, "📤 Uploading to Telegram...\n\n⏳ Please wait...", kb_cancel)

            await client.send_audio(
                chat_id=message.chat.id,
                audio=audio_path,
                thumb=thumb_path if thumb_path and os.path.exists(thumb_path) else None,
                caption=(
                    "✅ **Spotify Download Completed** 🎵\n\n"
                    f"🎧 **{title}**\n"
                    "📌 Format: **MP3**\n"
                    "🖼 Thumb: **Album Cover** ✅"
                )
            )

            await safe_edit(status, "✅ Done ✅", reply_markup=main_menu_keyboard())

        except asyncio.CancelledError:
            await safe_edit(status, "❌ Cancelled ✅", reply_markup=main_menu_keyboard())

        except Exception as e:
            await safe_edit(
                status,
                f"❌ **Spotify Download Failed**\n\nError:\n`{e}`",
                reply_markup=main_menu_keyboard()
            )

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
