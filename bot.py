import logging
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pymongo import MongoClient
from config import Config

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# MongoDB
mongo_client = MongoClient(Config.MONGO_DB_URI)
db = mongo_client["autofilter"]
files_col = db["files"]

# Pyrogram Client
bot = Client(
    "MovieAutoFilterBot",
    api_id=Config.API_ID,
    api_hash=Config.API_HASH,
    bot_token=Config.BOT_TOKEN,
)

# ----------------- Index Files -----------------
@bot.on_message(filters.chat(Config.FILES_CHANNEL) & (filters.document | filters.video))
async def index_files(client, message):
    file_id = message.document.file_id if message.document else message.video.file_id
    file_name = message.document.file_name if message.document else message.video.file_name

    files_col.update_one(
        {"file_id": file_id},
        {"$set": {"file_name": file_name, "file_id": file_id}},
        upsert=True
    )
    logger.info(f"Indexed file: {file_name}")

# ----------------- Start Command -----------------
@bot.on_message(filters.command("start"))
async def start(client, message):
    await message.reply_text(
        "👋 Hello! I am a Movie Auto Filter Bot.\n\n"
        "Send me a movie name and I’ll fetch it for you 🎬",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("Support", url="https://t.me/YourSupportGroup")]]
        ),
    )

# ----------------- Help Command -----------------
@bot.on_message(filters.command("help"))
async def help_cmd(client, message):
    await message.reply_text("📌 Send me a movie name and I’ll search from my database!")

# ----------------- Stats Command -----------------
@bot.on_message(filters.command("stats"))
async def stats(client, message):
    total_files = files_col.count_documents({})
    await message.reply_text(f"📊 Total files indexed: {total_files}")

# ----------------- Search Handler -----------------
@bot.on_message(filters.text & ~filters.command(["start", "help", "stats"]))
async def search_files(client, message):
    query = message.text.strip()
    results = list(files_col.find({"file_name": {"$regex": query, "$options": "i"}}).limit(20))

    if not results:
        await message.reply_text("❌ No results found.")
        return

    buttons = [
        [InlineKeyboardButton(f["file_name"], callback_data=f['file_id'])]
        for f in results
    ]
    await message.reply_text(
        f"🔎 Results for **{query}**:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

# ----------------- Send File -----------------
@bot.on_callback_query()
async def send_file(client, callback_query):
    file_id = callback_query.data
    file = files_col.find_one({"file_id": file_id})

    if file:
        await callback_query.message.reply_document(
            file_id, caption=f"🎬 {file['file_name']}"
        )
    await callback_query.answer()

# ----------------- Run Bot -----------------
bot.run()
