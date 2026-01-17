import os
import re
import time
import asyncio

from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "downloads")

INSTA_REGEX = re.compile(r"(https?://(www\.)?instagram\.com/(reel|p)/[A-Za-z0-9_\-]+)")

# Optional cookie file path (if you want better success rate)
# Render: upload cookie.txt into repo OR mount it
INSTA_COOKIES = os.getenv("INSTA_COOKIES", "").strip()  # Example: "cookies.txt"

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

# =========================
# Instagram Download
# =========================
async def insta_download(url: str, uid: int, status_msg=None, retries: int = 2):
    """
    Download instagram reel as MP4 using yt-dlp.
    - Progress animation always alive
    - Better error reason
    - Retry support
    """
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    url = clean_insta_url(url)

    kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_{uid}")]])

    # Retry loop
    last_error = None
    for attempt in range(1, retries + 2):
        outtmpl = os.path.join(DOWNLOAD_DIR, f"insta_{uid}_{int(time.time())}.%(ext)s")

        cmd = [
            "yt-dlp",
            "-f", "bv*+ba/best",
            "--merge-output-format", "mp4",
            "--no-playlist",
            "--no-warnings",
            "--newline",
            "--retries", "3",
            "--socket-timeout", "30",
            "-o", outtmpl,
            url
        ]

        # If cookie path provided and exists
        if INSTA_COOKIES and os.path.exists(INSTA_COOKIES):
            cmd.insert(1, "--cookies")
            cmd.insert(2, INSTA_COOKIES)

        if status_msg:
            await safe_edit(
                status_msg,
                f"📥 Instagram downloading...\n\n"
                f"🔄 Attempt: **{attempt}/{retries+1}**\n"
                f"⏳ Please wait...",
                reply_markup=kb
            )

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT
        )

        last_anim = 0
        anim_state = 0
        last_lines = []

        while True:
            if uid in USER_CANCEL:
                try:
                    proc.kill()
                except:
                    pass
                raise asyncio.CancelledError

            line = await proc.stdout.readline()
            if not line:
                break

            text = line.decode(errors="ignore").strip()
            if text:
                last_lines.append(text)
                last_lines = last_lines[-35:]  # keep last 35 lines

            # ✅ Keep UI alive even if no % lines
            if status_msg and time.time() - last_anim > 3:
                last_anim = time.time()
                anim_state = (anim_state + 1) % 3
                dots = "." * (anim_state + 1)

                await safe_edit(
                    status_msg,
                    f"📥 Downloading Reel{dots}\n\n"
                    f"🟠🟠🟠🟠🟠⚪⚪⚪⚪⚪⚪⚪⚪⚪\n\n"
                    f"🔄 Attempt: **{attempt}/{retries+1}**\n"
                    f"⏳ Please wait...",
                    reply_markup=kb
                )

        await proc.wait()

        # Success case
        if proc.returncode == 0:
            mp4_path = outtmpl.replace("%(ext)s", "mp4")
            if os.path.exists(mp4_path):
                return mp4_path

            # fallback
            files = [
                f for f in os.listdir(DOWNLOAD_DIR)
                if f.startswith(f"insta_{uid}_") and f.endswith(".mp4")
            ]
            if files:
                files.sort(key=lambda x: os.path.getmtime(os.path.join(DOWNLOAD_DIR, x)), reverse=True)
                return os.path.join(DOWNLOAD_DIR, files[0])

            last_error = "Downloaded mp4 not found"
            continue

        # Fail case → analyse reason
        joined = "\n".join(last_lines).lower()

        if "login required" in joined or "cookies" in joined:
            last_error = "Instagram login required / cookies needed ❌"
        elif "private" in joined or "restricted" in joined:
            last_error = "This reel is private / restricted ❌"
        elif "429" in joined or "rate limit" in joined:
            last_error = "Instagram rate limited / blocked. Try later ❌"
        elif "not found" in joined or "404" in joined:
            last_error = "Reel not found (deleted / wrong link) ❌"
        else:
            last_error = "Instagram download failed (blocked). Try another reel ❌"

        # retry after short delay (except cookies/login)
        if attempt < retries + 1:
            await asyncio.sleep(2)

    raise Exception(last_error or "Insta download failed ❌")

# =========================
# ENTRY + CALLBACKS
# =========================
INSTA_STATE = {}

async def insta_entry(client, message, url: str):
    uid = message.from_user.id
    INSTA_STATE[uid] = {"url": clean_insta_url(url)}

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎥 Video", callback_data="insta_video"),
            InlineKeyboardButton("📁 File", callback_data="insta_file")
        ],
        [InlineKeyboardButton("⬅️ Back", callback_data="back_main")]
    ])
    await message.reply(
        f"✅ Instagram Reel Detected 📸\n\n📌 {INSTA_STATE[uid]['url']}\n\n👇 Select format:",
        reply_markup=kb
    )

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
        try:
            USER_CANCEL.discard(uid)
            await safe_edit(status, "📥 Instagram downloading...\n⏳ Please wait...", kb)

            file_path = await insta_download(url, uid, status_msg=status, retries=2)

            if uid in USER_CANCEL:
                raise asyncio.CancelledError

            await safe_edit(status, "📤 Uploading...", kb)

            if mode == "video":
                await client.send_video(
                    cb.message.chat.id,
                    video=file_path,
                    caption="✅ Instagram Reel 🎥",
                    supports_streaming=True
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
            try:
                if file_path and os.path.exists(file_path):
                    os.remove(file_path)
            except:
                pass

    USER_TASKS[uid] = asyncio.create_task(job())
