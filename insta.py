import os
import re
import time
import asyncio

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


# ✅ Fancy progress bar animation (⚪→🔴→🟠→🟡→🟢→✅)
def insta_bar(stage: int):
    # stage 0..5
    if stage <= 0:
        return "⚪⚪⚪⚪⚪⚪⚪⚪⚪⚪"
    if stage == 1:
        return "🔴🔴⚪⚪⚪⚪⚪⚪⚪⚪"
    if stage == 2:
        return "🟠🟠🟠🟠⚪⚪⚪⚪⚪⚪"
    if stage == 3:
        return "🟡🟡🟡🟡🟡🟡⚪⚪⚪⚪"
    if stage == 4:
        return "🟢🟢🟢🟢🟢🟢🟢🟢⚪⚪"
    return "✅✅✅✅✅✅✅✅✅✅"


# ✅ download with ALWAYS LIVE progress UI
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

    stage = 1
    last_ui = 0
    percent = None

    async def ui_update(force=False):
        nonlocal last_ui, stage, percent
        if not status_msg:
            return
        if not force and time.time() - last_ui < 2:
            return
        last_ui = time.time()

        # progress stage animation
        stage = (stage + 1) % 5
        bar = insta_bar(stage)

        ptxt = f"\n📊 Progress: {percent}%" if percent is not None else ""

        await safe_edit(
            status_msg,
            f"📥 Downloading Instagram Reel...\n\n{bar}{ptxt}\n\n⏳ Please wait...",
            reply_markup=kb
        )

    # init UI
    await ui_update(force=True)

    while True:
        # cancel
        if uid in USER_CANCEL:
            try:
                proc.kill()
            except:
                pass
            raise asyncio.CancelledError

        # ✅ readline with timeout (IMPORTANT FIX)
        try:
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=2.0)
        except asyncio.TimeoutError:
            # no output, still update UI
            await ui_update()
            continue

        if not line:
            break

        text = line.decode(errors="ignore").strip()

        # try extract percentage
        # example: [download]  34.5% of 5.15MiB at 1.02MiB/s ETA 00:03
        m = re.search(r"(\d+(?:\.\d+)?)%", text)
        if m:
            try:
                percent = float(m.group(1))
            except:
                pass

        await ui_update()

    await proc.wait()

    if proc.returncode != 0:
        raise Exception("Insta download failed")

    mp4_path = outtmpl.replace("%(ext)s", "mp4")
    if os.path.exists(mp4_path):
        return mp4_path

    # fallback
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
        try:
            USER_CANCEL.discard(uid)
            await safe_edit(status, "📥 Instagram downloading...\n⏳ Please wait...", kb)

            file_path = await insta_download(url, uid, status_msg=status)

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
