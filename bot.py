import os
import asyncio
import logging
from pyrogram import Client, filters
from motor.motor_asyncio import AsyncIOMotorClient
from pyrogram.errors import FloodWait, RPCError

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Load from environment
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
LOG_CHANNEL = int(os.getenv("LOG_CHANNEL"))
FILES_CHANNEL = int(os.getenv("FILES_CHANNEL"))

# Initialize bot
bot = Client(
    "MovieAutoFilterBot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
)

# Database setup
mongo_client = AsyncIOMotorClient(MONGO_URI)
db = mongo_client["autofilter"]
files_collection = db["files"]


# ----------------------- FILE SENDER -----------------------
async def send_file(client, message, file):
    file_id = file.get("file_id")
    file_type = file.get("file_type", "document")  # default to document
    caption = file.get("caption", "")

    try:
        if file_type == "document":
            await client.send_document(message.chat.id, file_id, caption=caption)
        elif file_type == "video":
            await client.send_video(message.chat.id, file_id, caption=caption)
        elif file_type == "photo":
            await client.send_photo(message.chat.id, file_id, caption=caption)
        else:
            await message.reply("⚠️ Unsupported file type.")
    except FloodWait as e:
        logger.warning(f"FloodWait: Sleeping for {e.value} seconds")
        await asyncio.sleep(e.value)
        return await send_file(client, message, file)
    except RPCError as e:
        logger.error(f"RPC Error while sending file: {e}")
        await message.reply(f"❌ Error: {e}")


# ----------------------- SEARCH HANDLER -----------------------
@bot.on_message(filters.private & filters.text)
async def search_handler(client, message):
    # Prevent replying to itself
    if message.from_user.is_bot:
        return

    query = message.text.strip()
    if not query:
        return await message.reply("❌ Please provide a search term.")

    try:
        files_cursor = files_collection.find(
            {"file_name": {"$regex": query, "$options": "i"}}
        )
        files = await files_cursor.to_list(length=5)

        if not files:
            return await message.reply("⚠️ No results found.")

        for file in files:
            await send_file(client, message, file)
            await asyncio.sleep(1)  # prevent flood
    except Exception as e:
        logger.error(f"Search error: {e}")
        await message.reply("❌ Something went wrong while searching.")


# ----------------------- START HANDLER -----------------------
@bot.on_message(filters.command("start"))
async def start_handler(client, message):
    await message.reply(
        "👋 Hello! I am your **Movie AutoFilter Bot**.\n\n"
        "🔍 Send me a movie name and I’ll fetch it for you!"
    )


# ----------------------- MAIN -----------------------
if __name__ == "__main__":
    logger.info("🚀 Bot is starting...")
    bot.run()
