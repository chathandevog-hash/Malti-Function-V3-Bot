import os
import re
import time
import asyncio
import aiohttp

from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton


# ==========================
# CONFIG
# ==========================
DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "downloads")
CLOUDCONVERT_API_BASE = "https://api.cloudconvert.com/v2"

HIGH_QUALITIES = ["1080p", "720p", "480p"]
LOW_QUALITIES = ["360p", "240p", "144p"]

COMPRESS_STATE = {}  # uid -> {"msg_id": int, "mode": str, "quality": str}


# ==========================
# UTILS
# ==========================
async def safe_edit(msg, text, reply_markup=None):
    try:
        await msg.edit(text, reply_markup=reply_markup)
    except:
        pass

def make_circle_bar(percent: float, slots: int = 14):
    percent = max(0, min(100, percent))
    filled = int((percent / 100) * slots)

    if percent <= 0:
        icon = "⚪"
    elif percent < 25:
        icon = "🔴"
    elif percent < 50:
        icon = "🟠"
    elif percent < 75:
        icon = "🟡"
    elif percent < 100:
        icon = "🟢"
    else:
        icon = "✅"

    return f"[{icon * filled}{'⚪' * (slots - filled)}]"

def get_cloudconvert_key():
    return os.getenv("CLOUDCONVERT_API_KEY", "").strip()

def compressor_menu_kb():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🟢 Higher Quality", callback_data="cmp_high"),
            InlineKeyboardButton("🔴 Lower Quality", callback_data="cmp_low")
        ],
        [InlineKeyboardButton("⬅️ Back", callback_data="back_main")]
    ])

def quality_kb(mode: str):
    qualities = HIGH_QUALITIES if mode == "high" else LOW_QUALITIES
    rows = []
    row = []
    for q in qualities:
        row.append(InlineKeyboardButton(f"🎞 {q}", callback_data=f"cmp_q_{q}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="cmp_back_menu")])
    return InlineKeyboardMarkup(rows)


# ==========================
# CLOUDCONVERT
# ==========================
async def cloudconvert_create_job(session: aiohttp.ClientSession, api_key: str, quality: str):
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    height_map = {
        "2160p": 2160, "1440p": 1440, "1080p": 1080, "720p": 720,
        "480p": 480, "360p": 360, "240p": 240, "144p": 144,
    }
    height = height_map.get(quality, 360)

    payload = {
        "tasks": {
            "import-1": {"operation": "import/upload"},
            "convert-1": {
                "operation": "convert",
                "input": "import-1",
                "input_format": "auto",
                "output_format": "mp4",
                "engine": "ffmpeg",
                "video_codec": "x264",
                "crf": 28,
                "height": height,
                "audio_codec": "aac",
                "audio_bitrate": 96
            },
            "export-1": {"operation": "export/url", "input": "convert-1"}
        }
    }

    async with session.post(f"{CLOUDCONVERT_API_BASE}/jobs", headers=headers, json=payload) as r:
        data = await r.json()
        if r.status >= 400:
            raise Exception(f"CloudConvert API Error ({r.status}): {data}")
        return data

def extract_upload_form(job_json: dict):
    tasks = job_json.get("data", {}).get("tasks", [])
    for t in tasks:
        if t.get("operation") == "import/upload":
            return t.get("result", {}).get("form")
    return None

def extract_export_url(job_json: dict):
    tasks = job_json.get("data", {}).get("tasks", [])
    for t in tasks:
        if t.get("operation") == "export/url" and t.get("status") == "finished":
            files = t.get("result", {}).get("files", [])
            if files:
                return files[0].get("url")
    return None

async def cloudconvert_upload(session: aiohttp.ClientSession, upload_form: dict, file_path: str):
    url = upload_form.get("url")
    params = upload_form.get("parameters", {})
    if not url:
        raise Exception("CloudConvert upload url missing")

    form = aiohttp.FormData()
    for k, v in params.items():
        form.add_field(k, str(v))
    form.add_field("file", open(file_path, "rb"), filename=os.path.basename(file_path))

    async with session.post(url, data=form) as r:
        if r.status >= 400:
            txt = await r.text()
            raise Exception(f"Upload failed HTTP {r.status}: {txt}")

async def cloudconvert_wait(session: aiohttp.ClientSession, api_key: str, job_id: str, status_msg, uid, USER_CANCEL):
    headers = {"Authorization": f"Bearer {api_key}"}
    last_edit = 0

    while True:
        if uid in USER_CANCEL:
            raise asyncio.CancelledError

        async with session.get(f"{CLOUDCONVERT_API_BASE}/jobs/{job_id}", headers=headers) as r:
            job_json = await r.json()
            if r.status >= 400:
                raise Exception(f"CloudConvert API Error ({r.status}): {job_json}")

        tasks = job_json.get("data", {}).get("tasks", [])
        total = len(tasks) if tasks else 1
        finished = len([t for t in tasks if t.get("status") == "finished"])
        pct = (finished / total) * 100

        if time.time() - last_edit > 2:
            last_edit = time.time()
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_{uid}")]])
            await safe_edit(
                status_msg,
                f"⚙️ **Compressing in cloud...**\n\n{make_circle_bar(pct)}\n\n📊 **{pct:.2f}%**",
                kb
            )

        if job_json.get("data", {}).get("status") == "finished":
            return job_json

        if job_json.get("data", {}).get("status") == "error":
            raise Exception("CloudConvert job failed")

        await asyncio.sleep(2)


# ==========================
# ENTRY + CALLBACKS
# ==========================
async def compressor_entry(client, message):
    uid = message.from_user.id

    media = message.video or message.document
    if not media:
        return await message.reply("❌ Send a video or file.")

    COMPRESS_STATE[uid] = {"msg_id": message.id}

    await message.reply(
        "🗜️ **Compressor**\n\nChoose mode 👇",
        reply_markup=compressor_menu_kb()
    )

async def compressor_callback_router(client, cb, USER_TASKS, USER_CANCEL, get_or_create_status, main_menu_keyboard, DOWNLOAD_DIR):
    uid = cb.from_user.id
    data = cb.data

    if data == "cmp_back_menu":
        await cb.answer()
        return await cb.message.edit("🗜️ Choose mode 👇", reply_markup=compressor_menu_kb())

    if data in ["cmp_high", "cmp_low"]:
        await cb.answer()
        mode = "high" if data == "cmp_high" else "low"
        st = COMPRESS_STATE.get(uid) or {}
        st["mode"] = mode
        COMPRESS_STATE[uid] = st

        return await cb.message.edit(
            f"🎚 Select {'Higher' if mode=='high' else 'Lower'} Quality:",
            reply_markup=quality_kb(mode)
        )

    if data.startswith("cmp_q_"):
        await cb.answer()
        quality = data.replace("cmp_q_", "")
        st = COMPRESS_STATE.get(uid) or {}
        msg_id = st.get("msg_id")

        if not msg_id:
            return await cb.message.edit("❌ Send file/video first.", reply_markup=main_menu_keyboard())

        status = await get_or_create_status(cb.message, uid)
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_{uid}")]])
        await safe_edit(status, f"⚙️ Starting compress ({quality})...", kb)

        async def job():
            input_path = None
            try:
                USER_CANCEL.discard(uid)

                api_key = get_cloudconvert_key()
                if not api_key:
                    raise Exception("CLOUDCONVERT_API_KEY missing in env")

                # find original media message by msg_id
                media_msg = await client.get_messages(cb.message.chat.id, msg_id)
                media = media_msg.video or media_msg.document
                if not media:
                    raise Exception("Media not found")

                os.makedirs(DOWNLOAD_DIR, exist_ok=True)
                input_path = os.path.join(DOWNLOAD_DIR, f"cmp_{uid}_{int(time.time())}_{media.file_unique_id}.bin")

                await safe_edit(status, "⬇️ Downloading from Telegram...", kb)
                await client.download_media(media_msg, file_name=input_path)

                await safe_edit(status, "☁️ Creating cloud job...", kb)

                timeout = aiohttp.ClientTimeout(total=None)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    job_json = await cloudconvert_create_job(session, api_key, quality)
                    job_id = job_json["data"]["id"]

                    upload_form = extract_upload_form(job_json)
                    if not upload_form:
                        raise Exception("Upload form missing")

                    await safe_edit(status, "☁️ Uploading to cloud...", kb)
                    await cloudconvert_upload(session, upload_form, input_path)

                    await safe_edit(status, "⚙️ Compressing...", kb)
                    final_job = await cloudconvert_wait(session, api_key, job_id, status, uid, USER_CANCEL)

                    out_url = extract_export_url(final_job)
                    if not out_url:
                        raise Exception("Output link missing")

                await safe_edit(
                    status,
                    f"✅ **Compressed Successfully ☁️**\n\n"
                    f"🎞 Quality: **{quality}**\n"
                    f"⬇️ Download Link:\n{out_url}\n\n"
                    f"📌 Tip: Paste this link in 🌐 URL Uploader to upload to Telegram ✅",
                    reply_markup=main_menu_keyboard()
                )

            except asyncio.CancelledError:
                await safe_edit(status, "❌ Cancelled ✅", reply_markup=main_menu_keyboard())
            except Exception as e:
                await safe_edit(status, f"❌ Failed!\n\nError: `{e}`", reply_markup=main_menu_keyboard())
            finally:
                USER_CANCEL.discard(uid)
                try:
                    if input_path and os.path.exists(input_path):
                        os.remove(input_path)
                except:
                    pass

        USER_TASKS[uid] = asyncio.create_task(job())
