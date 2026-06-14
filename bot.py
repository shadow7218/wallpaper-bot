
import os
import logging
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import datetime
import pytz
from dotenv import load_dotenv
import base64
from PIL import Image
import io
import httpx
import json

# Load environment variables from .env file
load_dotenv()

# Gemini API configuration (FREE!)
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent"

# Configuration
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")
QUEUE_DIR = "./queue"
OWNER_ID = None
POST_TIME = "12:00"
TIMEZONE = "Asia/Kolkata"  # IST (GMT+5:30)

# Wallhaven API configuration
WALLHAVEN_API_URL = "https://wallhaven.cc/api/v1/search"
WALLHAVEN_CATEGORIES = "010"  # 010 for Anime
WALLHAVEN_PURITY = "100"  # SFW only

# Set up logging
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)


async def analyze_image_with_gemini(image_path: str) -> dict:
    """Analyze an image using Google Gemini Vision to extract tags (FREE)."""
    if not GEMINI_API_KEY:
        logger.error("Gemini API key not set.")
        return {}

    try:
        with Image.open(image_path) as img:
            width, height = img.size
            aspect_ratio_tag = "#mobilewallpaper" if height > width else "#desktopwallpaper"

            # Convert image to base64
            buffered = io.BytesIO()
            img_rgb = img.convert("RGB")  # Ensure it's RGB for JPEG
            img_rgb.save(buffered, format="JPEG")
            base64_image = base64.b64encode(buffered.getvalue()).decode("utf-8")

        headers = {
            "Content-Type": "application/json"
        }

        payload = {
            "contents": [
                {
                    "parts": [
                        {
                            "text": (
                                "Analyze this anime wallpaper. Identify the main character(s) if any "
                                "(e.g., Naruto, Gojo, Luffy, Madara), and the general category "
                                "(e.g., anime, nature, abstract). "
                                "Provide the character names as lowercase hashtags without spaces "
                                "(e.g., #naruto, #gojo). "
                                "Provide the category as a lowercase hashtag "
                                "(e.g., #animewallpaper, #naturewallpaper). "
                                "Do not include any other text, just the hashtags, separated by spaces."
                            )
                        },
                        {
                            "inline_data": {
                                "mime_type": "image/jpeg",
                                "data": base64_image
                            }
                        }
                    ]
                }
            ],
            "generationConfig": {
                "maxOutputTokens": 100
            }
        }

        url = f"{GEMINI_API_URL}?key={GEMINI_API_KEY}"

        async with httpx.AsyncClient() as client:
            response = await client.post(url, headers=headers, json=payload, timeout=30.0)
            response.raise_for_status()
            response_data = response.json()

            content = response_data["candidates"][0]["content"]["parts"][0]["text"].strip()
            tags = content.split()
            tags.append(aspect_ratio_tag)
            return {"tags": tags, "width": width, "height": height}

    except Exception as e:
        logger.error(f"Error analyzing image with Gemini: {e}")
        return {}


async def fetch_wallhaven_wallpaper() -> dict | None:
    """Fetch a random anime wallpaper from Wallhaven API."""
    try:
        params = {
            "q": "anime",
            "categories": WALLHAVEN_CATEGORIES,
            "purity": WALLHAVEN_PURITY,
            "sorting": "random",
            "atleast": "1920x1080"
        }
        async with httpx.AsyncClient() as client:
            response = await client.get(WALLHAVEN_API_URL, params=params, timeout=10.0)
            response.raise_for_status()
            data = response.json()
            if data and data["data"]:
                return data["data"][0]
    except Exception as e:
        logger.error(f"Error fetching wallpaper from Wallhaven: {e}")
    return None


async def download_wallpaper(url: str, file_path: str) -> bool:
    """Download a wallpaper from a given URL to a specified path."""
    try:
        async with httpx.AsyncClient() as client:
            async with client.stream("GET", url, timeout=30.0) as response:
                response.raise_for_status()
                with open(file_path, "wb") as f:
                    async for chunk in response.aiter_bytes():
                        f.write(chunk)
        return True
    except Exception as e:
        logger.error(f"Error downloading wallpaper from {url}: {e}")
        return False


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a message when the command /start is issued."""
    global OWNER_ID
    if OWNER_ID is None:
        OWNER_ID = update.effective_user.id
        logger.info(f"Bot owner set to: {OWNER_ID}")
        await update.message.reply_text(
            f"Hello! I am your wallpaper bot. You ({OWNER_ID}) are now registered as the owner. "
            "Send me wallpapers, and I'll queue them up for daily posting."
        )
    else:
        await update.message.reply_text("Hello! I am your wallpaper bot. Send me wallpapers!")


async def handle_wallpaper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle incoming photos and documents (wallpapers)."""
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("Sorry, only the bot owner can send wallpapers to the queue.")
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
        else:
            await update.message.reply_text("Please send an image file.")
            return

    if file_id and file_name:
        new_file = await context.bot.get_file(file_id)
        os.makedirs(QUEUE_DIR, exist_ok=True)
        file_path = os.path.join(QUEUE_DIR, file_name)
        await new_file.download_to_drive(file_path)
        logger.info(f"Wallpaper saved: {file_path}")

        await update.message.reply_text("Wallpaper received! Analyzing with Gemini AI... please wait.")

        # Analyze image and get tags using Gemini
        analysis_result = await analyze_image_with_gemini(file_path)
        tags = analysis_result.get("tags", [])

        # Store tags alongside the wallpaper
        tags_file_path = file_path + ".tags"
        with open(tags_file_path, "w") as f:
            f.write(" ".join(tags))
        logger.info(f"Tags saved for {file_name}: {tags}")

        await update.message.reply_text(f"Wallpaper '{file_name}' added to queue!\nTags: {' '.join(tags)}")
    else:
        await update.message.reply_text("Could not process the wallpaper. Please send a photo or an image document.")


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Check how many wallpapers are queued."""
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("Sorry, only the bot owner can check the status.")
        return

    os.makedirs(QUEUE_DIR, exist_ok=True)
    queued_wallpapers = [
        f for f in os.listdir(QUEUE_DIR)
        if os.path.isfile(os.path.join(QUEUE_DIR, f)) and not f.endswith(".tags")
    ]
    await update.message.reply_text(f"There are {len(queued_wallpapers)} wallpapers in the queue.")


async def post_wallpapers(application: Application) -> None:
    """Posts wallpapers from the queue, filling missing slots from Wallhaven."""
    logger.info("Starting daily wallpaper posting.")
    bot = application.bot

    os.makedirs(QUEUE_DIR, exist_ok=True)
    queued_files = [
        f for f in os.listdir(QUEUE_DIR)
        if os.path.isfile(os.path.join(QUEUE_DIR, f)) and not f.endswith(".tags")
    ]
    queued_wallpapers_info = []

    for file_name in queued_files:
        file_path = os.path.join(QUEUE_DIR, file_name)
        tags_file_path = file_path + ".tags"
        tags = []
        if os.path.exists(tags_file_path):
            with open(tags_file_path, "r") as f:
                tags = f.read().split()
        queued_wallpapers_info.append({"path": file_path, "tags": tags})

    wallpapers_to_post = []
    for wp_info in queued_wallpapers_info:
        if len(wallpapers_to_post) < 4:
            wallpapers_to_post.append(wp_info)

    # Fill remaining slots from Wallhaven if needed
    while len(wallpapers_to_post) < 4:
        logger.info("Fetching wallpaper from Wallhaven to fill slots.")
        wallhaven_data = await fetch_wallhaven_wallpaper()
        if wallhaven_data:
            image_url = wallhaven_data["path"]
            file_extension = image_url.split(".")[-1]
            wp_id = wallhaven_data['id']
            temp_file_name = f"wallhaven_{wp_id}.{file_extension}"
            temp_file_path = os.path.join(QUEUE_DIR, temp_file_name)

            if await download_wallpaper(image_url, temp_file_path):
                analysis_result = await analyze_image_with_gemini(temp_file_path)
                tags = analysis_result.get("tags", [])
                tags_file_path = temp_file_path + ".tags"
                with open(tags_file_path, "w") as f:
                    f.write(" ".join(tags))
                logger.info(f"Wallhaven wallpaper {temp_file_name} downloaded and analyzed.")
                wallpapers_to_post.append({"path": temp_file_path, "tags": tags})
            else:
                logger.warning(f"Failed to download Wallhaven wallpaper: {image_url}")
        else:
            logger.warning("Could not fetch enough wallpapers from Wallhaven.")
            break

    if not wallpapers_to_post:
        logger.info("No wallpapers to post today.")
        if OWNER_ID:
            await bot.send_message(chat_id=OWNER_ID, text="No wallpapers to post today.")
        return

    # Post wallpapers
    for wp_info in wallpapers_to_post:
        file_path = wp_info["path"]
        caption = " ".join(wp_info["tags"])
        file_name = os.path.basename(file_path)

        try:
            # Post as compressed photo
            with open(file_path, "rb") as f:
                await bot.send_photo(chat_id=CHANNEL_ID, photo=f, caption=caption)
            logger.info(f"Posted {file_name} as photo.")

            # Post as full-quality document
            with open(file_path, "rb") as f:
                await bot.send_document(chat_id=CHANNEL_ID, document=f, caption=f"Full quality: {caption}")
            logger.info(f"Posted {file_name} as document.")

            # Remove from queue after posting
            os.remove(file_path)
            tags_file_path = file_path + ".tags"
            if os.path.exists(tags_file_path):
                os.remove(tags_file_path)
            logger.info(f"Removed {file_name} from queue.")

        except Exception as e:
            logger.error(f"Error posting wallpaper {file_name}: {e}")
            if OWNER_ID:
                await bot.send_message(chat_id=OWNER_ID, text=f"Error posting wallpaper {file_name}: {e}")

    logger.info("Daily wallpaper posting finished.")
    if OWNER_ID:
        await bot.send_message(
            chat_id=OWNER_ID,
            text=f"Daily posting done! {len(wallpapers_to_post)} wallpapers posted."
        )


async def post_wallpapers_manual(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Manually trigger posting of wallpapers."""
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("Sorry, only the bot owner can trigger manual posting.")
        return
    await update.message.reply_text("Manual posting triggered. Please wait...")
    await post_wallpapers(context.application)


def main() -> None:
    """Start the bot."""
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN not found. Please set it in the .env file.")
        return
    if not GEMINI_API_KEY:
        logger.error("GEMINI_API_KEY not found. Please set it in the .env file.")
        return
    if not CHANNEL_ID:
        logger.warning("CHANNEL_ID not found. Please set it in the .env file for posting to a channel.")

    application = Application.builder().token(BOT_TOKEN).build()

    # Register handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("post", post_wallpapers_manual))
    application.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_wallpaper))

    # Set up scheduler
    scheduler = AsyncIOScheduler(timezone=TIMEZONE)
    hour, minute = map(int, POST_TIME.split(":"))
    scheduler.add_job(post_wallpapers, CronTrigger(hour=hour, minute=minute), args=(application,))
    scheduler.start()
    logger.info(f"Scheduler started. Posts scheduled daily at {POST_TIME} {TIMEZONE}.")

    logger.info("Bot started. Press Ctrl-C to stop.")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
