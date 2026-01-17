import os
import re
import time
import asyncio
import subprocess

from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "downloads")

INSTA_REGEX = re.compile(r"(https?://(www\.)?instagram\.com/(reel|p)/[A-Za-z0-9_\-]+)")

try:
    from bot import USER_CANCEL
except:
    USER_CANCEL = set()

def is_instagram_url(text: str) -> bool:
    return bool(INSTA_REGEX.search(text or ""))

def clean_insta_url(text: str) -> str:
    m = INSTA_REGEX.search(text or "")
    return m.group(1) if m else (text or "").strip()

async def safe_edit(msg, text, reply_markup=None):
    try:
        await msg.edit(text, reply_markup=reply_markup)
    except:
        pass

def get_video_meta(path: str):
    # returns (duration, width, height)
    try:
        cmd = [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            path
        ]
        out = subprocess.check_output(cmd).decode().strip().splitlines()
        # order: duration, width, height (depends) -> handle safely
        nums = [x.strip() for x in out if x.strip()]
        duration = int(float(nums[0])) if len(nums) > 0 else 0
        width = int(nums[1]) if len(nums) > 1 else 0
        height = int(nums[2]) if len(nums) > 2 else 0
        return duration, width, height
    except:
        return 0, 0, 0

def make_thumb(video_path: str):
    # capture frame from middle (better than starting frame)
    try:
        base = os.path.splitext(video_path)[0]
        thumb = base + "_thumb.jpg"
        duration, _, _ = get_video_meta(video_path)
        seek = max(1, duration // 2) if duration else 1

        cmd = [
            "ffmpeg", "-y",
            "-ss", str(seek),
            "-i", video_path,
            "-frames:v", "1",
            "-q:v", "4",
            thumb
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if os.path.exists(thumb):
            return thumb
    except:
        pass
    return None

# ✅ yt-dlp download with ALWAYS progress animation
async def insta_download(url: str, uid: int, status_msg=None):
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    outtmpl = os.path.join(DOWNLOAD_DIR, f"insta_{uid}_{int(time.time())}.%(ext)s")

    cmd = [
        "yt-dlp",
        "-f", "bv*+ba/best",
        "--merge-output-format", "mp4",
        "-o", outtmpl,
        "--newline",
        url
    ]

    kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_{uid}")]])

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT
    )

    last_ui = 0
    anim = 0

    async def update_anim():
        nonlocal anim, last_ui
        if status_msg and time.time() - last_ui > 2:
            last_ui = time.time()
            anim = (anim + 1) % 5

            bars = ["⚪⚪⚪⚪⚪", "🔴⚪⚪⚪⚪", "🟠🔴⚪⚪⚪", "🟡🟠🔴⚪⚪", "🟢🟡🟠🔴⚪"]
            bar = bars[anim] + "⚪⚪⚪⚪⚪⚪⚪⚪⚪"

            await safe_edit(
                status_msg,
                f"📥 Downloading Instagram Reel...\n\n{bar}\n\n⏳ Please wait...",
                reply_markup=kb
            )

    # 🔥 key fix: readline timeout
    while True:
        if uid in USER_CANCEL:
            try:
                proc.kill()
            except:
                pass
            raise asyncio.CancelledError

        try:
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=2.0)
        except asyncio.TimeoutError:
            await update_anim()
            continue

        if not line:
            break

        # keep UI alive
        await update_anim()

    await proc.wait()

    if proc.returncode != 0:
        raise Exception("Insta download failed")

    mp4_path = outtmpl.replace("%(ext)s", "mp4")
    if os.path.exists(mp4_path):
        return mp4_path

    # fallback find
    files = [f for f in os.listdir(DOWNLOAD_DIR) if f.startswith(f"insta_{uid}_") and f.endswith(".mp4")]
    if not files:
        raise Exception("Downloaded mp4 not found")
    files.sort(key=lambda x: os.path.getmtime(os.path.join(DOWNLOAD_DIR, x)), reverse=True)
    return os.path.join(DOWNLOAD_DIR, files[0])

# =========================
# ENTRY + CALLBACKS
# =========================
INSTA_STATE = {}

async def insta_entry(client, message, url: str):
    uid = message.from_user.id
    INSTA_STATE[uid] = {"url": url}

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎥 Video", callback_data="insta_video"),
            InlineKeyboardButton("📁 File", callback_data="insta_file")
        ],
        [InlineKeyboardButton("⬅️ Back", callback_data="back_main")]
    ])
    await message.reply(f"✅ Instagram Reel Detected 📸\n\n📌 {url}\n\n👇 Select format:", reply_markup=kb)

async def insta_callback_router(client, cb, USER_TASKS, USER_CANCEL, get_or_create_status, main_menu_keyboard, DOWNLOAD_DIR):
    uid = cb.from_user.id
    data = cb.data

    if data not in ["insta_video", "insta_file"]:
        return

    st = INSTA_STATE.get(uid) or {}
    url = st.get("url")
    if not url:
        return await cb.message.edit("❌ Session expired. Send reel again.", reply_markup=main_menu_keyboard())

    mode = "video" if data.endswith("video") else "file"
    await cb.answer()

    status = await get_or_create_status(cb.message, uid)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_{uid}")]])

    async def job():
        file_path = None
        thumb_path = None
        try:
            USER_CANCEL.discard(uid)

            await safe_edit(status, "📥 Instagram downloading...\n⏳ Please wait...", kb)

            file_path = await insta_download(url, uid, status_msg=status)

            if uid in USER_CANCEL:
                raise asyncio.CancelledError

            await safe_edit(status, "🖼 Generating thumbnail...\n⏳ Please wait...", kb)
            thumb_path = make_thumb(file_path)

            await safe_edit(status, "📤 Uploading...\n⏳ Please wait...", kb)

            if mode == "video":
                duration, width, height = get_video_meta(file_path)

                await client.send_video(
                    cb.message.chat.id,
                    video=file_path,
                    caption="✅ Instagram Reel 🎥",
                    supports_streaming=True,
                    thumb=thumb_path,
                    duration=duration or None,
                    width=width or None,
                    height=height or None
                )
            else:
                await client.send_document(
                    cb.message.chat.id,
                    document=file_path,
                    caption="✅ Instagram Reel 📁"
                )

            await safe_edit(status, "✅ Done ✅", reply_markup=main_menu_keyboard())

        except asyncio.CancelledError:
            await safe_edit(status, "❌ Cancelled ✅", reply_markup=main_menu_keyboard())
        except Exception as e:
            await safe_edit(status, f"❌ Insta Failed!\n\nError: `{e}`", reply_markup=main_menu_keyboard())
        finally:
            INSTA_STATE.pop(uid, None)
            USER_CANCEL.discard(uid)

            for p in [thumb_path, file_path]:
                try:
                    if p and os.path.exists(p):
                        os.remove(p)
                except:
                    pass

    USER_TASKS[uid] = asyncio.create_task(job())
