import os
import asyncio

from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from config import BOT_TOKEN, API_ID, API_HASH, DOWNLOAD_DIR

# ✅ Modules
from url import url_flow, url_callback_router
from compress import compressor_entry, compressor_callback_router
from insta import is_instagram_url, clean_insta_url, insta_entry
from youtube import is_youtube_url, clean_youtube_url, youtube_entry, youtube_callback_router

# ===========================
# GLOBALS
# ===========================
USER_TASKS = {}
USER_CANCEL = set()
USER_STATE = {}   # uid -> state string
UI_STATUS_MSG = {}

# ===========================
# HELPERS
# ===========================
async def get_or_create_status(message, uid):
    """
    One status msg per user to avoid UI mess.
    """
    if uid in UI_STATUS_MSG:
        return UI_STATUS_MSG[uid]
    status = await message.reply("⏳ Processing...")
    UI_STATUS_MSG[uid] = status
    return status


def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🌐 URL Uploader", callback_data="menu_url"),
            InlineKeyboardButton("🗜️ Compressor", callback_data="menu_compress")
        ],
        [
            InlineKeyboardButton("📸 Instagram", callback_data="menu_insta"),
            InlineKeyboardButton("▶️ YouTube", callback_data="menu_youtube")
        ]
    ])


def back_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back_main")]])


WELCOME_TEXT = (
    "✨ **Welcome to Multifunctional Bot! 🤖💫**\n\n"
    "🌐 **URL Uploader**\n"
    "➜ Send any direct link & I will upload it ✅\n"
    "⚠️ Limit: **2GB**\n\n"
    "🗜️ **Compressor**\n"
    "➜ Compress Video/File & get **Direct Download Link** ✅\n\n"
    "📸 **Instagram Reel Downloader**\n"
    "➜ Send reel link & bot will download + upload ✅\n\n"
    "▶️ **YouTube Downloader**\n"
    "➜ Send link ➜ Choose Video/File/Audio ➜ Select Quality ✅\n\n"
    "🚀 Now send something to start 👇😊"
)

# ===========================
# BOT INIT
# ===========================
app = Client(
    "MultiFunctionBot",
    bot_token=BOT_TOKEN,
    api_id=API_ID,
    api_hash=API_HASH
)

# ===========================
# START / BACK
# ===========================
@app.on_message(filters.private & filters.command("start"))
async def start_cmd(client, message):
    uid = message.from_user.id
    USER_STATE.pop(uid, None)
    await message.reply(WELCOME_TEXT, reply_markup=main_menu_keyboard())


@app.on_callback_query(filters.regex("^back_main$"))
async def back_main(client, cb):
    uid = cb.from_user.id
    USER_STATE.pop(uid, None)
    await cb.answer()
    await cb.message.edit(WELCOME_TEXT, reply_markup=main_menu_keyboard())


# ===========================
# MENUS
# ===========================
@app.on_callback_query(filters.regex("^menu_url$"))
async def menu_url(client, cb):
    USER_STATE[cb.from_user.id] = "WAIT_URL"
    await cb.answer()
    await cb.message.edit("🌐 **URL Uploader Mode**\n\nSend direct URL 👇", reply_markup=back_keyboard())


@app.on_callback_query(filters.regex("^menu_compress$"))
async def menu_compress(client, cb):
    USER_STATE[cb.from_user.id] = "WAIT_COMPRESS"
    await cb.answer()
    await cb.message.edit("🗜️ **Compressor Mode**\n\nSend a Video/File 👇", reply_markup=back_keyboard())


@app.on_callback_query(filters.regex("^menu_insta$"))
async def menu_insta(client, cb):
    USER_STATE[cb.from_user.id] = "WAIT_INSTA"
    await cb.answer()
    await cb.message.edit("📸 **Instagram Mode**\n\nSend Reel URL 👇", reply_markup=back_keyboard())


@app.on_callback_query(filters.regex("^menu_youtube$"))
async def menu_youtube(client, cb):
    USER_STATE[cb.from_user.id] = "WAIT_YOUTUBE"
    await cb.answer()
    await cb.message.edit("▶️ **YouTube Mode**\n\nSend YouTube URL 👇", reply_markup=back_keyboard())


# ===========================
# CANCEL
# ===========================
@app.on_callback_query(filters.regex("^cancel_"))
async def cancel_task(client, cb):
    try:
        uid = int(cb.data.split("_", 1)[1])
    except:
        return await cb.answer("Invalid", show_alert=True)

    USER_CANCEL.add(uid)
    task = USER_TASKS.get(uid)
    if task and not task.done():
        task.cancel()

    await cb.answer("✅ Cancelled!")
    try:
        await cb.message.edit("❌ Cancelled ✅", reply_markup=main_menu_keyboard())
    except:
        pass


# ===========================
# ROUTE ALL CALLBACKS
# ===========================
@app.on_callback_query()
async def all_callbacks(client, cb):
    data = cb.data

    # URL callbacks
    if data.startswith("url_"):
        return await url_callback_router(
            client, cb, USER_TASKS, USER_CANCEL, get_or_create_status, main_menu_keyboard, DOWNLOAD_DIR
        )

    # Compressor callbacks
    if data.startswith("cmp_"):
        return await compressor_callback_router(
            client, cb, USER_TASKS, USER_CANCEL, get_or_create_status, main_menu_keyboard, DOWNLOAD_DIR
        )

    # YouTube callbacks
    if data.startswith("yt_"):
        return await youtube_callback_router(
            client, cb, USER_TASKS, USER_CANCEL, get_or_create_status, main_menu_keyboard, DOWNLOAD_DIR
        )


# ===========================
# TEXT HANDLER
# ===========================
@app.on_message(filters.private & filters.text)
async def text_handler(client, message):
    uid = message.from_user.id
    text = message.text.strip()

    if text.startswith("/"):
        return

    # ✅ Auto detect Instagram
    if is_instagram_url(text):
        # ✅ FIXED: pass USER_TASKS + main_menu_keyboard
        return await insta_entry(client, message, clean_insta_url(text), USER_TASKS, main_menu_keyboard)

    # ✅ Auto detect YouTube
    if is_youtube_url(text):
        return await youtube_entry(client, message, clean_youtube_url(text))

    state = USER_STATE.get(uid, "")

    # URL uploader mode
    if state == "WAIT_URL":
        return await url_flow(client, message, text)

    return await message.reply("❌ Menu select cheyyu ✅", reply_markup=main_menu_keyboard())


# ===========================
# FILE HANDLER (compressor)
# ===========================
@app.on_message(filters.private & (filters.video | filters.document))
async def file_handler(client, message):
    uid = message.from_user.id
    state = USER_STATE.get(uid, "")

    if state == "WAIT_COMPRESS":
        return await compressor_entry(client, message)

    await message.reply("❌ Send URL / or select menu ✅", reply_markup=main_menu_keyboard())


# ===========================
# RUN
# ===========================
if __name__ == "__main__":
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    print("✅ Bot started...")
    app.run()
