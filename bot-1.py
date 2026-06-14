import os
import logging
import asyncio
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import datetime
import pytz
from dotenv import load_dotenv
import base64
from PIL import Image
import io
import httpx

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent"

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")
QUEUE_DIR = "./queue"
OWNER_ID = None
TIMEZONE = "Asia/Kolkata"
POST_HOUR = 12
POST_MINUTE = 0

WALLHAVEN_API_URL = "https://wallhaven.cc/api/v1/search"

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)


async def analyze_image_with_gemini(image_path: str) -> dict:
    if not GEMINI_API_KEY:
        return {}
    try:
        with Image.open(image_path) as img:
            width, height = img.size
            aspect_ratio_tag = "#mobilewallpaper" if height > width else "#desktopwallpaper"
            buffered = io.BytesIO()
            img.convert("RGB").save(buffered, format="JPEG")
            base64_image = base64.b64encode(buffered.getvalue()).decode("utf-8")

        payload = {
            "contents": [{"parts": [
                {"text": "Analyze this anime wallpaper. Identify the main character(s) if any and the general category. Provide character names as lowercase hashtags (e.g., #naruto, #gojo) and category as hashtag (e.g., #animewallpaper). Only hashtags, separated by spaces, nothing else."},
                {"inline_data": {"mime_type": "image/jpeg", "data": base64_image}}
            ]}],
            "generationConfig": {"maxOutputTokens": 100}
        }

        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{GEMINI_API_URL}?key={GEMINI_API_KEY}",
                headers={"Content-Type": "application/json"},
                json=payload,
                timeout=30.0
            )
            response.raise_for_status()
            content = response.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
            tags = content.split()
            tags.append(aspect_ratio_tag)
            return {"tags": tags, "width": width, "height": height}
    except Exception as e:
        logger.error(f"Gemini error: {e}")
        return {}


async def fetch_wallhaven_wallpaper():
    try:
        params = {"q": "anime", "categories": "010", "purity": "100", "sorting": "random", "atleast": "1920x1080"}
        async with httpx.AsyncClient() as client:
            response = await client.get(WALLHAVEN_API_URL, params=params, timeout=10.0)
            response.raise_for_status()
            data = response.json()
            if data and data["data"]:
                return data["data"][0]
    except Exception as e:
        logger.error(f"Wallhaven error: {e}")
    return None


async def download_wallpaper(url: str, file_path: str) -> bool:
    try:
        async with httpx.AsyncClient() as client:
            async with client.stream("GET", url, timeout=30.0) as response:
                response.raise_for_status()
                with open(file_path, "wb") as f:
                    async for chunk in response.aiter_bytes():
                        f.write(chunk)
        return True
    except Exception as e:
        logger.error(f"Download error: {e}")
        return False


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global OWNER_ID
    if OWNER_ID is None:
        OWNER_ID = update.effective_user.id
        await update.message.reply_text(
            f"Hello! I am your wallpaper bot. You ({OWNER_ID}) are now the owner! Send me wallpapers to queue them."
        )
    else:
        await update.message.reply_text("Hello! Send me wallpapers and I'll post them to your channel!")


async def handle_wallpaper(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("Sorry, only the bot owner can do this.")
        return

    file_id = None
    file_name = None
    if update.message.photo:
        file_id = update.message.photo[-1].file_id
        file_name = f"photo_{file_id}.jpg"
    elif update.message.document:
        if update.message.document.mime_type and update.message.document.mime_type.startswith("image/"):
            file_id = update.message.document.file_id
            file_name = update.message.document.file_name

    if file_id and file_name:
        os.makedirs(QUEUE_DIR, exist_ok=True)
        file_path = os.path.join(QUEUE_DIR, file_name)
        new_file = await context.bot.get_file(file_id)
        await new_file.download_to_drive(file_path)

        await update.message.reply_text("Analyzing with Gemini AI... please wait.")
        analysis = await analyze_image_with_gemini(file_path)
        tags = analysis.get("tags", [])

        with open(file_path + ".tags", "w") as f:
            f.write(" ".join(tags))

        await update.message.reply_text(f"✅ Added to queue!\nTags: {' '.join(tags)}")
    else:
        await update.message.reply_text("Please send an image file.")


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("Only the owner can check status.")
        return
    os.makedirs(QUEUE_DIR, exist_ok=True)
    count = len([f for f in os.listdir(QUEUE_DIR) if not f.endswith(".tags")])
    await update.message.reply_text(f"There are {count} wallpapers in the queue.")


async def post_wallpapers(application: Application):
    logger.info("Starting wallpaper posting.")
    bot = application.bot
    os.makedirs(QUEUE_DIR, exist_ok=True)

    queued_files = [f for f in os.listdir(QUEUE_DIR) if not f.endswith(".tags")]
    wallpapers = []

    for file_name in queued_files:
        file_path = os.path.join(QUEUE_DIR, file_name)
        tags = []
        if os.path.exists(file_path + ".tags"):
            with open(file_path + ".tags") as f:
                tags = f.read().split()
        wallpapers.append({"path": file_path, "tags": tags})
        if len(wallpapers) >= 4:
            break

    while len(wallpapers) < 4:
        data = await fetch_wallhaven_wallpaper()
        if data:
            url = data["path"]
            ext = url.split(".")[-1]
            temp_path = os.path.join(QUEUE_DIR, f"wallhaven_{data['id']}.{ext}")
            if await download_wallpaper(url, temp_path):
                analysis = await analyze_image_with_gemini(temp_path)
                tags = analysis.get("tags", [])
                with open(temp_path + ".tags", "w") as f:
                    f.write(" ".join(tags))
                wallpapers.append({"path": temp_path, "tags": tags})
        else:
            break

    for wp in wallpapers:
        try:
            caption = " ".join(wp["tags"])
            with open(wp["path"], "rb") as f:
                await bot.send_photo(chat_id=CHANNEL_ID, photo=f, caption=caption)
            with open(wp["path"], "rb") as f:
                await bot.send_document(chat_id=CHANNEL_ID, document=f, caption=f"Full quality: {caption}")
            os.remove(wp["path"])
            if os.path.exists(wp["path"] + ".tags"):
                os.remove(wp["path"] + ".tags")
        except Exception as e:
            logger.error(f"Post error: {e}")

    if OWNER_ID:
        await bot.send_message(chat_id=OWNER_ID, text=f"✅ Posted {len(wallpapers)} wallpapers!")


async def post_manual(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("Only the owner can do this.")
        return
    await update.message.reply_text("Posting now, please wait...")
    await post_wallpapers(context.application)


async def scheduler_loop(application: Application):
    tz = pytz.timezone(TIMEZONE)
    while True:
        now = datetime.datetime.now(tz)
        if now.hour == POST_HOUR and now.minute == POST_MINUTE:
            await post_wallpapers(application)
            await asyncio.sleep(60)
        await asyncio.sleep(30)


async def main():
    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("post", post_manual))
    application.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_wallpaper))

    await application.initialize()
    await application.start()
    await application.updater.start_polling()

    logger.info("Bot started!")
    await scheduler_loop(application)


if __name__ == "__main__":
    asyncio.run(main())
