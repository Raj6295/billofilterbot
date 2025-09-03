import os
from dotenv import load_dotenv

# Load environment variables from .env
load_dotenv()

class Config:
    API_ID = int(os.getenv("API_ID", "12345"))
    API_HASH = os.getenv("API_HASH", "")
    BOT_TOKEN = os.getenv("BOT_TOKEN", "")
    MONGO_DB_URI = os.getenv("MONGO_DB_URI", "")
    LOG_CHANNEL = int(os.getenv("LOG_CHANNEL", "-100123456789"))
    FILES_CHANNEL = int(os.getenv("FILES_CHANNEL", "-100987654321"))
    BOT_USERNAME = os.getenv("BOT_USERNAME", "MovieFilterBot")
