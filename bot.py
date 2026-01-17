import os
import asyncio
from pyrogram import Client, filters
from pyrogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    Message,
    CallbackQuery,
)

# ✅ Load env from config.py
from config import BOT_TOKEN, API_ID, API_HASH, DOWNLOAD_DIR

# ✅ Import modules
import url
import insta
import youtube
import compress


# -------------------------
# ✅ Pyrogram Client (Rename to avoid Flask conflict)
# -------------------------
tg = Client(
    "MultiFunctionBot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
)


# -------------------------
# Main Menu Buttons
# -------------------------
def main_menu():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🌐 URL Uploader", callback_data="menu_url"),
                InlineKeyboardButton("🗜 Compressor", callback_data="menu_compress"),
            ],
            [
                InlineKeyboardButton("📸 Instagram", callback_data="menu_insta"),
                InlineKeyboardButton("▶️ YouTube", callback_data="menu_youtube"),
            ],
        ]
    )


WELCOME_TEXT = """✨ Welcome to Multifunctional Bot! 🤖💫
Here you can do multiple things in one bot 🚀

🌐 URL Uploader
➜ Send any direct link and I will upload it for you instantly ✅

🗜️ Compressor
➜ Compress Video/File and get Direct Download Link ✅

📸 Instagram Reel Downloader
➜ Send reel link & choose Video/File ✅

▶️ YouTube Downloader
➜ Send link ➜ Choose Video/File/Audio ➜ Select Quality ✅

📌 How to use?
1️⃣ Send a File / Video / Audio / URL
2️⃣ Select your needed option ✅
3️⃣ Wait for processing ⏳
4️⃣ Get your output 🎉

💡 Use /help for all commands & guide 🛠️
🚀 Now send something to start 👇😊
"""


# -------------------------
# Commands
# -------------------------
@tg.on_message(filters.command("start"))
async def start_cmd(client: Client, message: Message):
    await message.reply_text(WELCOME_TEXT, reply_markup=main_menu())


@tg.on_message(filters.command("help"))
async def help_cmd(client: Client, message: Message):
    await message.reply_text(WELCOME_TEXT, reply_markup=main_menu())


# -------------------------
# Menu Callback
# -------------------------
@tg.on_callback_query(filters.regex("^menu_"))
async def menu_handler(client: Client, cb: CallbackQuery):
    data = cb.data

    if data == "menu_url":
        await cb.message.reply_text(
            "🌐 URL Uploader Mode ✅\n\nSend any direct download URL now 👇",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Back", callback_data="back_home")]]
            ),
        )

    elif data == "menu_compress":
        await cb.message.reply_text(
            "🗜 Compressor Mode ✅\n\nSend Video/File now 👇",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Back", callback_data="back_home")]]
            ),
        )

    elif data == "menu_insta":
        await cb.message.reply_text(
            "📸 Instagram Reel Downloader ✅\n\nSend Instagram Reel link now 👇",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Back", callback_data="back_home")]]
            ),
        )

    elif data == "menu_youtube":
        await cb.message.reply_text(
            "▶️ YouTube Downloader ✅\n\nSend YouTube link now 👇",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Back", callback_data="back_home")]]
            ),
        )

    await cb.answer()


@tg.on_callback_query(filters.regex("^back_home$"))
async def back_home(client: Client, cb: CallbackQuery):
    await cb.message.reply_text("✅ Menu select cheyyu 👇", reply_markup=main_menu())
    await cb.answer()


# -------------------------
# Router: URL / Insta / YouTube / Files
# -------------------------
@tg.on_message(filters.text & ~filters.command(["start", "help"]))
async def text_router(client: Client, message: Message):
    text = message.text.strip()

    # ✅ Instagram Reel
    if "instagram.com" in text or "instagr.am" in text:
        await insta.handle_instagram_link(client, message, text)
        return

    # ✅ YouTube
    if "youtube.com" in text or "youtu.be" in text:
        await youtube.handle_youtube_link(client, message, text)
        return

    # ✅ URL Upload (Direct Link)
    if text.startswith("http://") or text.startswith("https://"):
        await url.handle_url_link(client, message, text)
        return

    await message.reply_text("❌ Please send a direct URL / Instagram / YouTube link.")


# ✅ Files for Compressor
@tg.on_message(filters.document | filters.video | filters.audio)
async def file_router(client: Client, message: Message):
    await compress.handle_compress_file(client, message)


# -------------------------
# ✅ Start bot correctly
# -------------------------
if __name__ == "__main__":
    if not BOT_TOKEN or not API_ID or not API_HASH:
        print("❌ Please set BOT_TOKEN, API_ID, API_HASH in env!")
        raise SystemExit

    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    print("✅ Bot started...")

    tg.run()
