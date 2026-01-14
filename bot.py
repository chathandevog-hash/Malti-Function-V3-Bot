import os
import re
import time
import asyncio
import aiohttp
import humanize
from urllib.parse import urlparse, unquote

from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from config import BOT_TOKEN, API_ID, API_HASH, DOWNLOAD_DIR

USER_URL = {}
USER_TASKS = {}
USER_CANCEL = set()

def is_url(text: str):
    return text.startswith("http://") or text.startswith("https://")

def safe_filename(name: str):
    name = re.sub(r"[\\/:*?\"<>|]", "_", name)
    name = name.strip().strip(".")
    if not name:
        name = f"file_{int(time.time())}"
    return name[:180]

def format_time(seconds: float):
    if seconds <= 0:
        return "0s"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"

def make_progress_text(title, downloaded, total, speed, eta):
    percent = (downloaded / total * 100) if total else 0
    bar_len = 18
    filled = int(bar_len * percent / 100) if total else 0
    bar = "█" * filled + "░" * (bar_len - filled)

    d_str = humanize.naturalsize(downloaded, binary=True)
    t_str = humanize.naturalsize(total, binary=True) if total else "Unknown"
    s_str = humanize.naturalsize(speed, binary=True) + "/s" if speed else "0 B/s"

    return (
        f"{title}\n\n"
        f"`{bar}`  **{percent:.1f}%**\n"
        f"📦 {d_str} / {t_str}\n"
        f"⚡ {s_str}\n"
        f"⏳ ETA: {format_time(eta)}"
    )

async def get_filename_and_size(url: str):
    filename = None
    total = 0
    try:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            try:
                async with session.head(url, allow_redirects=True) as r:
                    cd = r.headers.get("Content-Disposition", "")
                    if "filename=" in cd:
                        filename = cd.split("filename=")[-1].strip().strip('"').strip("'")
                    if r.headers.get("Content-Length"):
                        total = int(r.headers.get("Content-Length"))
            except:
                pass

            if not filename:
                async with session.get(url, allow_redirects=True) as r:
                    cd = r.headers.get("Content-Disposition", "")
                    if "filename=" in cd:
                        filename = cd.split("filename=")[-1].strip().strip('"').strip("'")
                    if not filename:
                        p = urlparse(str(r.url))
                        base = os.path.basename(p.path)
                        base = unquote(base)
                        if base:
                            filename = base
                    if not total and r.headers.get("Content-Length"):
                        total = int(r.headers.get("Content-Length"))
    except:
        pass

    if not filename:
        filename = f"file_{int(time.time())}.bin"

    return safe_filename(filename), total

async def download_stream(url, file_path, status_msg, uid):
    USER_CANCEL.discard(uid)
    timeout = aiohttp.ClientTimeout(total=None)
    downloaded = 0
    start_time = time.time()
    last_edit = 0
    total = 0

    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, allow_redirects=True) as r:
            if r.status != 200:
                raise Exception(f"HTTP {r.status}")

            if r.headers.get("Content-Length"):
                total = int(r.headers["Content-Length"])

            os.makedirs(os.path.dirname(file_path), exist_ok=True)

            with open(file_path, "wb") as f:
                async for chunk in r.content.iter_chunked(1024 * 256):
                    if uid in USER_CANCEL:
                        raise asyncio.CancelledError
                    if not chunk:
                        continue
                    f.write(chunk)
                    downloaded += len(chunk)

                    elapsed = time.time() - start_time
                    speed = downloaded / elapsed if elapsed > 0 else 0
                    eta = (total - downloaded) / speed if total and speed > 0 else 0

                    if time.time() - last_edit > 2:
                        last_edit = time.time()
                        kb = InlineKeyboardMarkup([
                            [InlineKeyboardButton("❌ Cancel Download", callback_data=f"cancel_{uid}")]
                        ])
                        await status_msg.edit(
                            make_progress_text("⬇️ Downloading...", downloaded, total, speed, eta),
                            reply_markup=kb
                        )

    return downloaded, total

async def upload_progress(current, total, status_msg, uid, start_time):
    if uid in USER_CANCEL:
        raise asyncio.CancelledError

    elapsed = time.time() - start_time
    speed = current / elapsed if elapsed > 0 else 0
    eta = (total - current) / speed if speed > 0 else 0

    now = time.time()
    if not hasattr(status_msg, "_last_edit"):
        status_msg._last_edit = 0

    if now - status_msg._last_edit > 2:
        status_msg._last_edit = now
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Cancel Upload", callback_data=f"cancel_{uid}")]
        ])
        await status_msg.edit(
            make_progress_text("📤 Uploading...", current, total, speed, eta),
            reply_markup=kb
        )

async def convert_to_mp4(input_path: str, status_msg, uid):
    if uid in USER_CANCEL:
        raise asyncio.CancelledError

    if input_path.lower().endswith(".mp4"):
        return input_path

    base = os.path.splitext(input_path)[0]
    out_path = base + "_mp4.mp4"

    await status_msg.edit("🎬 Converting to MP4...\n(FFmpeg running...)")

    cmd = [
        "ffmpeg",
        "-y",
        "-i", input_path,
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "23",
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        out_path
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL
    )

    while True:
        if uid in USER_CANCEL:
            try:
                proc.kill()
            except:
                pass
            raise asyncio.CancelledError

        if proc.returncode is not None:
            break

        await asyncio.sleep(1)
        try:
            await status_msg.edit("🎬 Converting to MP4...\nPlease wait...")
        except:
            pass

    await proc.wait()

    if proc.returncode != 0 or not os.path.exists(out_path):
        raise Exception("MP4 conversion failed!")

    return out_path

app = Client(
    "UrlUploaderBot",
    bot_token=BOT_TOKEN,
    api_id=API_ID,
    api_hash=API_HASH
)

@app.on_message(filters.private & filters.command("start"))
async def start_cmd(client, message):
    await message.reply(
        "✅ **URL Uploader Bot**\n\n"
        "➡️ Send a direct URL\n"
        "Then choose:\n"
        "📁 File = Document\n"
        "🎥 Video = Convert MP4 + Video upload\n\n"
        "Cancel supported ✅"
    )

@app.on_message(filters.private & filters.text)
async def url_handler(client, message):
    text = message.text.strip()
    if not is_url(text):
        return await message.reply("❌ Valid URL send cheyyu (http/https).")

    USER_URL[message.from_user.id] = text

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📁 File", callback_data="send_file"),
            InlineKeyboardButton("🎥 Video", callback_data="send_video")
        ]
    ])
    await message.reply("✅ URL Received!\n\nSelect upload type:", reply_markup=kb)

@app.on_callback_query(filters.regex("^cancel_"))
async def cancel_task(client, cb):
    try:
        _, uid_str = cb.data.split("_", 1)
        uid = int(uid_str)
    except:
        return await cb.answer("Invalid", show_alert=True)

    USER_CANCEL.add(uid)

    task = USER_TASKS.get(uid)
    if task and not task.done():
        task.cancel()

    await cb.answer("✅ Cancelled!", show_alert=False)
    try:
        await cb.message.edit("❌ Cancelled by user.")
    except:
        pass

@app.on_callback_query(filters.regex("^(send_file|send_video)$"))
async def send_type_selected(client, cb):
    uid = cb.from_user.id
    if uid not in USER_URL:
        return await cb.message.edit("❌ Session expired. URL again send cheyyu.")

    url = USER_URL[uid]
    mode = cb.data.replace("send_", "")

    await cb.answer()
    await cb.message.edit(f"✅ Selected: **{mode.upper()}**\n\n⚙️ Starting...")

    status = await cb.message.reply("⏳ Preparing...")

    async def job():
        file_path = None
        mp4_path = None
        try:
            filename, _ = await get_filename_and_size(url)
            if "." not in filename:
                filename += ".bin"

            file_path = os.path.join(DOWNLOAD_DIR, f"{uid}_{int(time.time())}_{filename}")

            await status.edit("⬇️ Starting download...")
            await download_stream(url, file_path, status, uid)

            if uid in USER_CANCEL:
                return

            upload_path = file_path
            if mode == "video":
                mp4_path = await convert_to_mp4(file_path, status, uid)
                upload_path = mp4_path

            if uid in USER_CANCEL:
                return

            await status.edit("📤 Upload starting...")

            up_start = time.time()
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ Cancel Upload", callback_data=f"cancel_{uid}")]
            ])
            await status.edit("📤 Uploading...", reply_markup=kb)

            if mode == "video":
                await client.send_video(
                    chat_id=cb.message.chat.id,
                    video=upload_path,
                    caption=f"✅ Uploaded as MP4 Video\n\n📌 {os.path.basename(upload_path)}",
                    progress=upload_progress,
                    progress_args=(status, uid, up_start)
                )
            else:
                await client.send_document(
                    chat_id=cb.message.chat.id,
                    document=upload_path,
                    caption=f"✅ Uploaded as File\n\n📌 {os.path.basename(upload_path)}",
                    progress=upload_progress,
                    progress_args=(status, uid, up_start)
                )

            await status.edit("✅ Done ✅")
            await asyncio.sleep(2)
            await status.delete()

        except asyncio.CancelledError:
            try:
                await status.edit("❌ Cancelled ✅")
            except:
                pass
        except Exception as e:
            try:
                await status.edit(f"❌ Failed!\n\nError: `{e}`")
            except:
                pass
        finally:
            for p in [file_path, mp4_path]:
                if p and os.path.exists(p):
                    try:
                        os.remove(p)
                    except:
                        pass

            USER_URL.pop(uid, None)
            USER_TASKS.pop(uid, None)
            USER_CANCEL.discard(uid)

    t = asyncio.create_task(job())
    USER_TASKS[uid] = t

if __name__ == "__main__":
    if not BOT_TOKEN or not API_ID or not API_HASH:
        print("❌ Please set BOT_TOKEN, API_ID, API_HASH in env!")
        raise SystemExit

    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    print("✅ Bot started...")
    app.run()
