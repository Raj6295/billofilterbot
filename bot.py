import os
import logging
import asyncio
import re
import aiohttp
from typing import List, Dict, Optional, Union, Tuple, Any
from datetime import datetime

# Pyrogram needs a current event loop initialized before import on some Python 3.14
# environments such as Render.
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

from pymongo import MongoClient
from pymongo.collection import Collection
from pymongo.database import Database
from pymongo.errors import PyMongoError
from pyrogram import Client, filters, enums
from pyrogram.types import (
    InlineKeyboardButton, 
    InlineKeyboardMarkup,
    Message,
    CallbackQuery
)
from dotenv import load_dotenv

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

def get_env_var(name: str, required: bool = True, default: str = None):
    """Helper function to get environment variables with error handling"""
    value = os.getenv(name, default)
    if value is not None:
        value = value.strip()
    if required and (value is None or value == ''):
        raise ValueError(f"Environment variable {name} is required but not set")
    return value


def parse_admin_ids(raw_value: str) -> List[int]:
    """Parse a comma-separated list of Telegram user IDs while ignoring inline comments."""
    if not raw_value:
        return []

    parsed_ids: List[int] = []
    for item in raw_value.split(','):
        cleaned = item.split('#', 1)[0].strip()
        if not cleaned:
            continue
        try:
            parsed_ids.append(int(cleaned))
        except ValueError:
            logger.warning(f"Skipping invalid admin ID value: {cleaned!r}")
    return parsed_ids


class Config:
    # Bot configuration
    API_ID = int(get_env_var('API_ID'))
    API_HASH = get_env_var('API_HASH')
    BOT_TOKEN = get_env_var('BOT_TOKEN')
    
    # Database configuration
    MONGO_DB_URI = get_env_var('MONGO_DB_URI')
    DB_NAME = "MovieFilterBot"
    
    # Channel configuration
    LOG_CHANNEL_ID = int(get_env_var('LOG_CHANNEL_ID'))
    FILES_CHANNEL_ID = int(get_env_var('FILES_CHANNEL_ID'))
    
    # Admin configuration
    ADMIN_IDS = parse_admin_ids(get_env_var('ADMIN_IDS', required=False, default=''))
    
    # Other settings
    MAX_RESULTS = 10
    SPELLCHECK_API = "https://api.dictionaryapi.dev/api/v2/entries/en/{}"
    
config = Config()

# Database class
class Database:
    def __init__(self):
        self._client: Optional[MongoClient] = None
        self.db: Optional[Database] = None
        self.collection: Optional[Collection] = None
        self.connect()

    def connect(self):
        self._client = None
        self.db = None
        self.collection = None

        try:
            if not config.MONGO_DB_URI:
                raise ValueError("MONGO_DB_URI is empty or not configured")

            self._client = MongoClient(config.MONGO_DB_URI, serverSelectionTimeoutMS=5000)
            self.db = self._client[config.DB_NAME]
            self.collection = self.db["files"]
            self._client.admin.command("ping")
            logger.info("Connected to MongoDB successfully!")
        except Exception as e:
            logger.error(f"Error connecting to MongoDB: {e}")
            self._client = None
            self.db = None
            self.collection = None

    def insert_file(self, file_data: Dict) -> bool:
        if self.collection is None:
            logger.warning("MongoDB is unavailable; skipping file insert.")
            return False
        try:
            result = self.collection.update_one(
                {"file_id": file_data["file_id"]},
                {"$set": file_data},
                upsert=True
            )
            return result.acknowledged
        except PyMongoError as e:
            logger.error(f"Error inserting file: {e}")
            return False

    def get_file_by_id(self, file_id: str) -> Optional[Dict]:
        if self.collection is None:
            logger.warning("MongoDB is unavailable; cannot fetch file by ID.")
            return None
        try:
            return self.collection.find_one({"file_id": file_id})
        except PyMongoError as e:
            logger.error(f"Error getting file by ID {file_id}: {e}")
            return None

    def search_files(self, query: str, limit: int = 10) -> List[Dict]:
        if self.collection is None:
            logger.warning("MongoDB is unavailable; returning empty search results.")
            return []
        try:
            # Check for existing text indexes
            index_info = self.collection.index_information()
            text_index_exists = any(
                'text' in idx.get('key', {}) 
                for idx in index_info.values()
            )
            
            # If no text index exists, create one
            if not text_index_exists:
                try:
                    self.collection.create_index([("caption", "text"), ("title", "text")])
                except PyMongoError as e:
                    if "IndexOptionsConflict" not in str(e):
                        logger.error(f"Error creating text index: {e}")
            
            # Perform the search
            results = self.collection.find(
                {"$text": {"$search": query}},
                {"score": {"$meta": "textScore"}}
            ).sort([("score", {"$meta": "textScore"})]).limit(limit)
            
            return list(results)
        except PyMongoError as e:
            logger.error(f"Error searching files: {e}")
            # Fallback to simple text search if text search fails
            try:
                return list(self.collection.find({
                    "$or": [
                        {"title": {"$regex": query, "$options": "i"}},
                        {"caption": {"$regex": query, "$options": "i"}}
                    ]
                }).limit(limit))
            except Exception as fallback_error:
                logger.error(f"Fallback search also failed: {fallback_error}")
                return []

    def close(self):
        if self._client:
            self._client.close()

# Initialize database
db = Database()

# Spell Checker class
class SpellChecker:
    def __init__(self):
        self.session = None
        
    async def get_session(self):
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()
        return self.session
        
    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()
    
    async def get_suggestions(self, word: str) -> Tuple[bool, List[str]]:
        """Get spelling suggestions for a word"""
        if not word or len(word) < 2:
            return True, []
            
        try:
            session = await self.get_session()
            async with session.get(config.SPELLCHECK_API.format(word)) as response:
                if response.status == 404:
                    # Word not found, try to get suggestions from the error message
                    data = await response.json()
                    if isinstance(data, dict) and 'message' in data:
                        # Extract words from the message
                        words = re.findall(r'"(.*?)"', data['message'])
                        return False, words[:5]  # Return first 5 suggestions
                
                # If we got here, the word is probably correct
                return True, []
                
        except Exception as e:
            logger.error(f"Error getting spelling suggestions: {e}")
            return True, []

# Initialize spell checker
spell_checker = SpellChecker()

# Initialize the Pyrogram client
app = Client(
    "MovieFilterBot",
    api_id=config.API_ID,
    api_hash=config.API_HASH,
    bot_token=config.BOT_TOKEN
)

# Store user sessions to handle button callbacks
user_sessions = {}

class UserSession:
    def __init__(self, user_id: int):
        self.user_id = user_id
        self.search_query = ""
        self.suggestions = []
        self.last_interaction = datetime.now()

    def update_query(self, query: str, suggestions: List[str] = None):
        self.search_query = query
        self.suggestions = suggestions or []
        self.last_interaction = datetime.now()

def get_user_session(user_id: int) -> UserSession:
    if user_id not in user_sessions:
        user_sessions[user_id] = UserSession(user_id)
    return user_sessions[user_id]

def clean_old_sessions():
    """Remove inactive user sessions to free up memory"""
    now = datetime.now()
    to_remove = [
        user_id for user_id, session in user_sessions.items()
        if (now - session.last_interaction).total_seconds() > 3600  # 1 hour
    ]
    for user_id in to_remove:
        del user_sessions[user_id]

def clean_search_query(query: str) -> str:
    """Clean and normalize search query"""
    # Remove common file extensions and special characters
    query = query.lower().strip()
    for ext in ['.mkv', '.mp4', '.avi', '.mov', '.wmv', '.flv', '.webm', '1080p', '720p', '480p', 'h264', 'x264', 'x265', 'hevc']:
        query = query.replace(ext, '')
    
    # Remove special characters and extra spaces
    import re
    query = re.sub(r'[^\w\s-]', ' ', query)  # Keep only alphanumeric, spaces, and hyphens
    query = ' '.join(query.split())  # Remove extra spaces
    
    return query

async def search_and_send_results(
    client: Client, 
    message: Message, 
    query: str, 
    reply_markup: InlineKeyboardMarkup = None,
    is_suggestion: bool = False,
    check_spelling: bool = True
) -> bool:
    """
    Search for files and send results to the user
    
    Args:
        client: Pyrogram client
        message: The message that triggered the search
        query: The search query
        reply_markup: Optional inline keyboard markup
        is_suggestion: Whether this is a suggested correction
    """
    try:
        # Clean and normalize the search query
        original_query = query
        cleaned_query = clean_search_query(query)
        logger.info(f"Searching for query: '{original_query}' (cleaned: '{cleaned_query}')")
        
        # If the query is too short after cleaning, inform the user
        if len(cleaned_query) < 3:
            if check_spelling:  # Only show error if this is the first attempt
                await message.reply_text("❌ Search query is too short. Please try with more specific terms.")
            return False
        
        # Search in database
        results = db.search_files(cleaned_query, limit=config.MAX_RESULTS)
        logger.info(f"Search for '{cleaned_query}' returned {len(results)} results")
        
        # If no results and we're not checking spelling, try a more flexible search
        if not results and not check_spelling:
            logger.info("No direct results found, trying flexible search")
            # Try a more flexible search with partial matching
            query_terms = [term for term in cleaned_query.split() if len(term) > 3]
            if len(query_terms) > 1:
                logger.info(f"Trying flexible search with terms: {query_terms}")
                results = []
                for term in query_terms:
                    term_results = db.search_files(term, limit=config.MAX_RESULTS)
                    # Add new results that aren't already in the results
                    for r in term_results:
                        if not any(r['file_id'] == existing['file_id'] for existing in results):
                            results.append(r)
                            if len(results) >= config.MAX_RESULTS:
                                break
                    if len(results) >= config.MAX_RESULTS:
                        break
                logger.info(f"Flexible search found {len(results)} results")
        
        if not results and not is_suggestion:
            # Try a more flexible search by splitting the query into words
            query_terms = [term for term in query.split() if len(term) > 3]  # Only terms longer than 3 chars
            
            if len(query_terms) > 1:
                logger.info(f"Trying partial match with individual terms: {query_terms}")
                
                # Try combinations of terms
                from itertools import combinations
                
                # Try different combinations of terms
                for r in range(len(query_terms), 0, -1):
                    for combo in combinations(query_terms, r):
                        if len(' '.join(combo)) < 3:  # Skip too short combinations
                            continue
                            
                        combo_query = ' '.join(combo)
                        logger.info(f"Trying combination: {combo_query}")
                        
                        # Search for this combination
                        combo_results = db.search_files(combo_query, limit=config.MAX_RESULTS)
                        
                        # Add new results
                        for result in combo_results:
                            if not any(r['file_id'] == result['file_id'] for r in results):
                                results.append(result)
                                
                                if len(results) >= config.MAX_RESULTS:
                                    results = results[:config.MAX_RESULTS]
                                    break
                        
                        if len(results) >= config.MAX_RESULTS:
                            break
                    
                    if results:  # If we found some results, stop trying
                        break
                
                if results:
                    logger.info(f"Found {len(results)} results using partial matching")
        
        if not results:
            logger.info("No results found after all search attempts")
            
            # Create a more helpful no-results message with suggestions
            no_results_text = f"""
🔍 *No Results Found* 🔍

I couldn't find any files matching: `{query}`

💡 *Try these tips:*
• Check for typos in your search
• Try a shorter or more general term
• Include the release year (e.g., 'Inception 2010')
• Use different keywords from the title
"""
            
            # Create suggestion buttons
            keyboard = [
                [
                    InlineKeyboardButton("🔍 Try a new search", switch_inline_query_current_chat=""),
                    InlineKeyboardButton("🎬 Popular Movies", callback_data="popular_movies")
                ],
                [
                    InlineKeyboardButton("📺 TV Shows", callback_data="tv_shows"),
                    InlineKeyboardButton("❓ Help", callback_data="help_info")
                ]
            ]
            
            await message.reply_text(
                no_results_text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=enums.ParseMode.MARKDOWN
            )
            return False
        
        # Create a beautiful results header
        results_header = f"""
✨ *Search Results* ✨

🔍 *Query:* `{query}`
📊 *Found:* {len(results)} {'result' if len(results) == 1 else 'results'}
━━━━━━━━━━━━━━━━━━
"""
        
        # Create a loading animation
        loading_phrases = [
            "🔍 Searching our database...",
            "📡 Fetching your files...",
            "⚡ Almost there..."
        ]
        
        # Send initial status message with loading animation
        status_msg = await message.reply_text(
            results_header + "\n" + loading_phrases[0],
            parse_mode=enums.ParseMode.MARKDOWN
        )
        
        # Update with loading animation
        for phrase in loading_phrases[1:]:
            await asyncio.sleep(1)
            await status_msg.edit_text(
                results_header + "\n" + phrase,
                parse_mode=enums.ParseMode.MARKDOWN
            )
        
        sent_messages = []
        
        # If only one result, send it directly
        if len(results) == 1:
            result = results[0]
            file_id = result.get('file_id')
            if file_id:
                file_type = result.get('file_type', 'document')
                caption = result.get('caption', result.get('title', ''))
                caption = f"{caption if caption else 'Untitled'}\n\n⏳ Auto-deletes in 30s\nPowered By: @Billo_Movies"
                
                try:
                    if file_type == 'video':
                        msg = await client.send_video(
                            chat_id=message.chat.id,
                            video=file_id,
                            caption=caption[:1024],
                            reply_to_message_id=message.id
                        )
                    else:
                        msg = await client.send_document(
                            chat_id=message.chat.id,
                            document=file_id,
                            caption=caption[:1024],
                            reply_to_message_id=message.id
                        )
                    
                    # Add the sent file message to deletion queue
                    user_session = get_user_session(message.from_user.id)
                    if not hasattr(user_session, 'messages_to_delete'):
                        user_session.messages_to_delete = []
                    user_session.messages_to_delete.append(msg)
                    
                    # Restart the deletion timer
                    if hasattr(user_session, 'deletion_task'):
                        user_session.deletion_task.cancel()
                    user_session.deletion_task = asyncio.create_task(
                        delete_messages_after_delay(client, user_session, 30)
                    )
                    
                    sent_messages.append(msg)
                except Exception as e:
                    logger.error(f"Error sending file {file_id}: {e}", exc_info=True)
        else:
            # For multiple results, show buttons
            buttons = []
            for i, result in enumerate(results, 1):
                file_id = result.get('file_id')
                if not file_id:
                    continue
                    
                # Use a shorter prefix and only the first few chars of the file_id
                callback_data = f"f_{file_id[:16]}"  # Using first 16 chars of file_id
                caption = result.get('caption', result.get('title', f'File {i}'))
                buttons.append([
                    InlineKeyboardButton(
                        text=f"📁 {caption[:30]}{'...' if len(caption) > 30 else ''}",
                        callback_data=callback_data
                    )
                ])
                
                # Store the mapping between short ID and full file_id in user session
                user_session = get_user_session(message.from_user.id)
                if not hasattr(user_session, 'file_mapping'):
                    user_session.file_mapping = {}
                user_session.file_mapping[file_id[:16]] = file_id
            
            # Send the results message
            results_msg = await message.reply_text(
                f"🔍 *Found {len(results)} results for '{query}'*\n\n"
                "Click on a file to view it:",
                reply_markup=InlineKeyboardMarkup(buttons),
                parse_mode=enums.ParseMode.MARKDOWN
            )
            sent_messages.append(results_msg)
            
            # Store the messages that need to be deleted
            user_session = get_user_session(message.from_user.id)
            if not hasattr(user_session, 'messages_to_delete'):
                user_session.messages_to_delete = []
            user_session.messages_to_delete.extend([status_msg, results_msg])
            
            # Start the deletion task if not already started
            if not hasattr(user_session, 'deletion_task') or user_session.deletion_task.done():
                user_session.deletion_task = asyncio.create_task(
                    delete_messages_after_delay(client, user_session, 30)
                )
        
        return True
        
    except Exception as e:
        logger.error(f"Error in search_and_send_results: {e}")
        
        # Create a more user-friendly error message
        error_text = """
⚠️ *Oops! Something went wrong* ⚠️

I encountered an error while processing your request.

🔧 *What you can do:*
• Try your search again in a moment
• Make sure your search query is clear
• Report this issue if it keeps happening

We're working to fix this as soon as possible!
"""
        
        # Create helpful buttons
        keyboard = [
            [
                InlineKeyboardButton("🔄 Try Again", switch_inline_query_current_chat=query if 'query' in locals() else ''),
                InlineKeyboardButton("❓ Get Help", url="https://t.me/your_support_username")
            ]
        ]
        
        await message.reply_text(
            error_text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=enums.ParseMode.MARKDOWN
        )
        return False

# Command handlers
@app.on_message(filters.command(["start", "help"]))
async def start_command(client: Client, message: Message):
    """Handle /start and /help commands"""
    welcome_text = """
🎬 *Welcome to Movie Filter Bot!* 🎭

🔍 *How to use:*
• Simply type the name of any movie or series
• I'll search through our database
• Get instant access to your requested media

*Note:* All files will auto-delete after 30 seconds
"""
    
    # Send welcome message without photo
    await message.reply_text(
        welcome_text,
        parse_mode=enums.ParseMode.MARKDOWN
    )

# Message handler for search queries
async def process_search_query(client: Client, message: Message, query: str, is_suggestion: bool = False):
    """Process a search query with spell checking and search"""
    try:
        user_id = message.from_user.id
        original_query = query.strip()
        
        if not original_query:
            return
        
        logger.info(f"Processing search query from {user_id}: {original_query}")
        
        # Clean and normalize the query
        cleaned_query = clean_search_query(original_query)
        
        # Get or create user session
        user_session = get_user_session(user_id)
        
        # If this is not a suggestion, check for spelling errors first
        if not is_suggestion:
            is_correct_spelling, suggestions = await spell_checker.get_suggestions(cleaned_query)
            
            # If we have spelling suggestions, show them
            if not is_correct_spelling and suggestions:
                logger.info(f"Found spelling suggestions for '{cleaned_query}': {suggestions}")
                user_session.update_query(cleaned_query, suggestions)
                
                # Create the suggestion message with the requested format
                suggestions_text = f"🔍 *No Results Found* 🔍\n"
                suggestions_text += f"I couldn't find any files matching: `{cleaned_query}`\n\n"
                suggestions_text += "🔍 *Did you mean one of these?*\n\n"
                
                # Add each suggestion with a number
                for i, suggestion in enumerate(suggestions[:5], 1):  # Max 5 suggestions
                    suggestions_text += f"{i}. {suggestion}\n"
                
                # Create buttons in a 2x2 grid for better mobile experience
                buttons = []
                row = []
                
                for i, suggestion in enumerate(suggestions[:4], 1):  # Max 4 buttons in grid
                    row.append(InlineKeyboardButton(
                        text=f"{suggestion}",
                        callback_data=f"suggest_{i-1}"
                    ))
                    
                    # Add a new row after every 2 buttons
                    if len(row) == 2 or i == len(suggestions[:4]):
                        buttons.append(row)
                        row = []
                
                reply_markup = InlineKeyboardMarkup(buttons)
                
                # Send the suggestions
                await message.reply_text(
                    suggestions_text,
                    reply_markup=reply_markup,
                    parse_mode=enums.ParseMode.MARKDOWN
                )
                return
        
        # If we get here, either spelling is correct or we're already showing suggestions
        await search_and_send_results(
            client=client,
            message=message,
            query=cleaned_query,
            is_suggestion=is_suggestion,
            check_spelling=False
        )
        
        # If we get here, either spelling is correct or we have no suggestions
        logger.info(f"Performing search for: {cleaned_query}")
        
        # Show searching message
        search_msg = await message.reply_text(f"🔍 Searching for: {cleaned_query}")
        
        try:
            # Perform the search
            await search_and_send_results(
                client=client,
                message=message,
                query=cleaned_query,
                is_suggestion=is_suggestion
            )
        finally:
            # Always try to delete the searching message
            try:
                await search_msg.delete()
            except Exception as e:
                logger.warning(f"Could not delete search message: {e}")
            
    except Exception as e:
        logger.error(f"Error in process_search_query: {e}", exc_info=True)
        try:
            await message.reply_text("❌ An error occurred while processing your search. Please try again.")
        except:
            pass

@app.on_message(filters.text & ~filters.command(["start", "help"]))
async def handle_search(client: Client, message: Message):
    """Handle search queries from users"""
    try:
        # Check if the message is from a group and is a reply to the bot
        if message.chat.type != enums.ChatType.PRIVATE:
            # Only respond to direct messages or replies to the bot in groups
            if not message.reply_to_message or message.reply_to_message.from_user.id != (await client.get_me()).id:
                return
        
        # Process the search query
        await process_search_query(client, message, message.text.strip())
    except Exception as e:
        logger.error(f"Error in handle_search: {e}", exc_info=True)
        await message.reply_text("❌ An error occurred while processing your request. Please try again.")

async def delete_messages_after_delay(client: Client, user_session, delay: int):
    """Delete all stored messages after a delay"""
    try:
        await asyncio.sleep(delay)
        if hasattr(user_session, 'messages_to_delete'):
            for msg in user_session.messages_to_delete[:]:  # Create a copy of the list
                try:
                    await msg.delete()
                    user_session.messages_to_delete.remove(msg)
                except Exception as e:
                    logger.warning(f"Could not delete message: {e}")
    except Exception as e:
        logger.error(f"Error in delete_messages_after_delay: {e}")

# Callback query handler for suggestion buttons
@app.on_callback_query()
async def handle_callback_query(client: Client, callback_query: CallbackQuery):
    """Handle button callbacks for file selection and actions"""
    try:
        user_id = callback_query.from_user.id
        data = callback_query.data
        message = callback_query.message
        
        # Show typing action while processing
        await client.send_chat_action(chat_id=message.chat.id, action=enums.ChatAction.TYPING)
        
        # Handle suggestion selection
        if data.startswith('suggest_'):
            user_session = get_user_session(user_id)
            if not hasattr(user_session, 'suggestions') or not user_session.suggestions:
                await callback_query.answer("❌ Suggestion expired. Please try your search again.")
                return
                
            try:
                suggestion_index = int(data.split('_')[1])
                if 0 <= suggestion_index < len(user_session.suggestions):
                    selected_suggestion = user_session.suggestions[suggestion_index]
                    await callback_query.answer(f"Searching for: {selected_suggestion}")
                    await process_search_query(
                        client=client,
                        message=message,
                        query=selected_suggestion,
                        is_suggestion=True
                    )
                    return
            except (IndexError, ValueError):
                pass
                
            await callback_query.answer("❌ Invalid suggestion. Please try again.")
            return
        
        # Handle file sending
        if data.startswith('f_'):
            short_file_id = data[2:18]  # Get the short file ID
            await callback_query.answer("Sending file...")
            
            # Get the full file_id from user session
            user_session = get_user_session(callback_query.from_user.id)
            file_id = getattr(user_session, 'file_mapping', {}).get(short_file_id)
            
            if not file_id:
                await callback_query.answer("❌ File not found or expired")
                return
                
            # Get file info from database
            result = db.get_file_by_id(file_id)
            if not result:
                await callback_query.answer("❌ File not found in database")
                return
                
            caption = result.get('caption', result.get('title', 'Untitled'))
            caption = f"{caption}\n\n⏳ Auto-deletes in 30s\nPowered By: @Billo_Movies"
            
            try:
                # Initialize user session for message tracking
                if not hasattr(user_session, 'messages_to_delete'):
                    user_session.messages_to_delete = []
                
                # Send the file
                if result.get('file_type') == 'video':
                    msg = await client.send_video(
                        chat_id=message.chat.id,
                        video=file_id,
                        caption=caption[:1024],
                        reply_to_message_id=message.id
                    )
                else:
                    msg = await client.send_document(
                        chat_id=message.chat.id,
                        document=file_id,
                        caption=caption[:1024],
                        reply_to_message_id=message.id
                    )
                
                # Add the sent message to deletion queue
                user_session.messages_to_delete.append(msg)
                
                # Cancel any existing deletion task and start a new one
                if hasattr(user_session, 'deletion_task'):
                    user_session.deletion_task.cancel()
                user_session.deletion_task = asyncio.create_task(
                    delete_messages_after_delay(client, user_session, 30)
                )
                
                await callback_query.answer()
                return
            except Exception as e:
                logger.error(f"Error sending file {file_id}: {e}")
                await callback_query.answer("❌ Failed to send file", show_alert=True)
                return
        
            
        # Handle copy link button
        if data.startswith('copy_'):
            file_id = data[5:]
            # In a real implementation, you would generate a direct link here
            await callback_query.answer("🔗 Link copied to clipboard!", show_alert=True)
            return
            
        # Handle unknown callbacks
        await callback_query.answer("⚠️ This action is not available right now.")
                
    except Exception as e:
        logger.error(f"Error in callback handler: {e}", exc_info=True)
        try:
            await callback_query.edit_message_text("❌ An error occurred. Please try your search again.")
        except:
            pass  # Message might be already deleted or modified
    
    # Clean up old sessions periodically
    clean_old_sessions()

# File handler to index new files in the channel
@app.on_message(filters.chat(config.FILES_CHANNEL_ID) & (filters.document | filters.video | filters.audio))
async def index_new_file(client: Client, message: Message):
    """Index new files added to the files channel"""
    try:
        logger.info(f"New file detected in channel. Message ID: {message.id}")
        file_id = None
        file_name = ""
        file_size = 0
        
        if message.document:
            file_id = message.document.file_id
            file_name = message.document.file_name or ""
            file_size = message.document.file_size or 0
            file_type = "document"
            logger.info(f"Document detected - Name: {file_name}, Size: {file_size} bytes")
        elif message.video:
            file_id = message.video.file_id
            file_name = message.video.file_name or f"video_{file_id[:8]}.mp4"
            file_size = message.video.file_size or 0
            file_type = "video"
            logger.info(f"Video detected - Name: {file_name}, Size: {file_size} bytes")
        elif message.audio:
            file_id = message.audio.file_id
            file_name = message.audio.file_name or f"audio_{file_id[:8]}.mp3"
            file_size = message.audio.file_size or 0
            file_type = "audio"
            logger.info(f"Audio detected - Name: {file_name}, Size: {file_size} bytes")
        else:
            logger.warning(f"Unsupported message type: {message.media}")
            return
        
        if not file_id:
            logger.warning("No file ID found in the message")
            return
        
        # Create file data for database
        file_data = {
            "file_id": file_id,
            "message_id": message.id,
            "title": file_name.lower(),  # Convert to lowercase for case-insensitive search
            "size": _format_size(file_size),
            "file_type": file_type,
            "date": message.date,
            "original_title": file_name  # Store original case for display
        }
        
        # Add caption if available
        if message.caption:
            file_data["caption"] = message.caption
            logger.info(f"File caption: {message.caption}")
        
        logger.info(f"Saving file to database: {file_data}")
        
        # Save to database
        if db.insert_file(file_data):
            logger.info(f"Successfully indexed file: {file_name}")
            
            # Only send to log channel if LOG_CHANNEL_ID is set and valid
            if hasattr(config, 'LOG_CHANNEL_ID') and config.LOG_CHANNEL_ID:
                try:
                    await client.send_message(
                        chat_id=int(config.LOG_CHANNEL_ID),
                        text=f"✅ New file indexed:\n📁 Name: {file_name}\n📦 Size: {file_data['size']}\n🆔 ID: {file_id}"
                    )
                except Exception as e:
                    logger.warning(f"Could not send message to log channel: {e}")
        else:
            logger.error("Failed to save file to database")
            
    except Exception as e:
        logger.error(f"Error indexing file: {e}")

def _format_size(size_bytes: int) -> str:
    """Convert file size in bytes to human-readable format"""
    if not size_bytes:
        return "0 B"
    
    units = ['B', 'KB', 'MB', 'GB', 'TB']
    i = 0
    while size_bytes >= 1024 and i < len(units) - 1:
        size_bytes /= 1024
        i += 1    
    return f"{size_bytes:.2f} {units[i]}"

# Start the bot
if __name__ == "__main__":
    logger.info("Starting Movie Filter Bot...")
    
    # Create necessary directories
    os.makedirs("logs", exist_ok=True)
    
    try:
        # Run the bot
        app.run()
    except Exception as e:
        logger.error(f"Bot stopped with error: {e}")
    finally:
        # Clean up resources
        asyncio.get_event_loop().run_until_complete(spell_checker.close())
        db.close()
        logger.info("Bot stopped.")
