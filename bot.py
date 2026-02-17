import asyncio
import logging
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple, Union
from pyrogram import Client, filters, enums
from pyrogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, 
    InlineKeyboardButton, User, Chat
)
from pyrogram.errors import (
    FloodWait, ChatAdminRequired, UserNotParticipant,
    MessageNotModified, ButtonDataInvalid, RPCError
)
import os
from pathlib import Path
from motor.motor_asyncio import AsyncIOMotorClient
from bson import ObjectId

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Configuration with environment variable support
def load_config():
    """Load configuration from environment variables or config file"""
    config = {}
    
    # Try to load from environment variables first
    config['API_ID'] = os.getenv('API_ID')
    config['API_HASH'] = os.getenv('API_HASH') 
    config['BOT_TOKEN'] = os.getenv('BOT_TOKEN')
    config['FORCE_SUB_CHANNEL_ID'] = os.getenv('FORCE_SUB_CHANNEL_ID')
    config['MONGODB_URI'] = os.getenv('MONGODB_URI')
    config['DATABASE_NAME'] = os.getenv('DATABASE_NAME')
    admin_ids_str = os.getenv('ADMIN_IDS')
    config['ADMIN_IDS'] = [int(x.strip()) for x in admin_ids_str.split(',') if x.strip()] if admin_ids_str else []
    
    # Try to load from config.py for missing values
    try:
        import config as config_file
        config['API_ID'] = config['API_ID'] or getattr(config_file, 'API_ID', None)
        config['API_HASH'] = config['API_HASH'] or getattr(config_file, 'API_HASH', None)
        config['BOT_TOKEN'] = config['BOT_TOKEN'] or getattr(config_file, 'BOT_TOKEN', None)
        config['FORCE_SUB_CHANNEL_ID'] = config['FORCE_SUB_CHANNEL_ID'] or getattr(config_file, 'FORCE_SUB_CHANNEL_ID', None)
        config['ADMIN_IDS'] = config['ADMIN_IDS'] or getattr(config_file, 'ADMIN_IDS', [])
        config['MONGODB_URI'] = config['MONGODB_URI'] or getattr(config_file, 'MONGODB_URI', None)
        config['DATABASE_NAME'] = config['DATABASE_NAME'] or getattr(config_file, 'DATABASE_NAME', None)
    except ImportError:
        pass

    # Set hardcoded defaults if still missing
    config['MONGODB_URI'] = config['MONGODB_URI'] or ''
    config['DATABASE_NAME'] = config['DATABASE_NAME'] or 'autoposter_bot'
    
    # Validate configuration
    missing = []
    if not config['API_ID']:
        missing.append('API_ID')
    if not config['API_HASH']:
        missing.append('API_HASH')
    if not config['BOT_TOKEN']:
        missing.append('BOT_TOKEN')
    
    if missing:
        print("❌ Missing required configuration:")
        for item in missing:
            print(f"   - {item}")
        print("\n📋 Setup Instructions:")
        print("1. Get API_ID and API_HASH from https://my.telegram.org")
        print("2. Get BOT_TOKEN from @BotFather")
        print("3. Either:")
        print("   a) Set environment variables: API_ID, API_HASH, BOT_TOKEN, (optional) FORCE_SUB_CHANNEL_ID, (optional) ADMIN_IDS (comma-separated), (optional) MONGODB_URI, (optional) DATABASE_NAME")
        print("   b) Create config.py with your credentials")
        print("\nExample config.py:")
        print("API_ID = 12345678")
        print("API_HASH = 'your_api_hash_here'")
        print("BOT_TOKEN = 'your_bot_token_here'")
        print("FORCE_SUB_CHANNEL_ID = '@YourPublicChannel'")
        print("ADMIN_IDS = [123456789, 987654321]")
        print("MONGODB_URI = 'mongodb://localhost:27017'")
        print("DATABASE_NAME = 'autoposter_bot'")
        exit(1)
    
    # Convert API_ID to integer
    try:
        config['API_ID'] = int(config['API_ID'])
    except (ValueError, TypeError):
        print("❌ API_ID must be a valid integer")
        exit(1)
    
    return config

# Load configuration
CONFIG = load_config()
API_ID = CONFIG['API_ID']
API_HASH = CONFIG['API_HASH']
BOT_TOKEN = CONFIG['BOT_TOKEN']
FORCE_SUB_CHANNEL_ID = CONFIG['FORCE_SUB_CHANNEL_ID']
ADMIN_IDS = CONFIG['ADMIN_IDS']
MONGODB_URI = CONFIG['MONGODB_URI']
DATABASE_NAME = CONFIG['DATABASE_NAME']

# Rate limiting delays
ADMIN_DELAY = 1.5
FORWARD_DELAY = 2.0
BATCH_DELAY = 0.5
DELETE_DELAY = 1.0
FORCE_SUB_CHECK_DELAY = 0.5

class Database:
    def __init__(self):
        self.client = None
        self.db = None
        
    async def connect(self):
        """Connect to MongoDB"""
        try:
            self.client = AsyncIOMotorClient(MONGODB_URI)
            self.db = self.client[DATABASE_NAME]
            
            # Test connection
            await self.client.admin.command('ping')
            logger.info("✅ Connected to MongoDB successfully!")
            
            # Create indexes for better performance
            await self.create_indexes()
            
        except Exception as e:
            logger.error(f"❌ Failed to connect to MongoDB: {e}")
            raise
    
    async def create_indexes(self):
        """Create database indexes"""
        try:
            # Jobs collection indexes
            await self.db.jobs.create_index("user_id")
            await self.db.jobs.create_index("is_active")
            await self.db.jobs.create_index("created_at")
            
            # Forwarded messages collection indexes
            await self.db.forwarded_messages.create_index("job_id")
            await self.db.forwarded_messages.create_index("forwarded_at")
            
            # User states collection indexes
            await self.db.user_states.create_index("user_id", unique=True)
            
            # Users collection indexes
            await self.db.users.create_index("user_id", unique=True)
            
        except Exception as e:
            logger.error(f"Error creating indexes: {e}")
    
    async def create_job(self, user_id: int, job_data: dict) -> str:
        """Create a new forwarding job"""
        job_doc = {
            'user_id': user_id,
            'job_name': job_data['name'],
            'source_channel_id': job_data['source'],
            'target_channel_id': job_data['target'],
            'start_post_id': job_data['start_id'],
            'end_post_id': job_data['end_id'],
            'batch_size': job_data['batch_size'],
            'recurring_time': job_data['recurring_time'],
            'delete_time': job_data['delete_time'],
            'filter_type': job_data['filter_type'],
            'custom_caption': job_data.get('caption', ''),
            'button_text': job_data.get('button_text', ''),
            'button_url': job_data.get('button_url', ''),
            'is_active': False,
            'last_forwarded_id': 0,
            'created_at': datetime.utcnow().replace(tzinfo=timezone.utc),
            'updated_at': datetime.utcnow().replace(tzinfo=timezone.utc)
        }
        
        result = await self.db.jobs.insert_one(job_doc)
        return str(result.inserted_id)
    
    async def get_user_jobs(self, user_id: int) -> List[dict]:
        """Get all jobs for a user"""
        cursor = self.db.jobs.find({'user_id': user_id}).sort('created_at', -1)
        jobs = []
        async for job in cursor:
            job['id'] = str(job['_id'])
            jobs.append(job)
        return jobs
    
    async def get_job(self, job_id: str) -> Optional[dict]:
        """Get a specific job by ID"""
        try:
            job = await self.db.jobs.find_one({'_id': ObjectId(job_id)})
            if job:
                job['id'] = str(job['_id'])
            return job
        except Exception as e:
            logger.error(f"Error getting job {job_id}: {e}")
            return None
    
    async def update_job_status(self, job_id: str, is_active: bool):
        """Update job active status"""
        await self.db.jobs.update_one(
            {'_id': ObjectId(job_id)},
            {
                '$set': {
                    'is_active': is_active,
                    'updated_at': datetime.utcnow().replace(tzinfo=timezone.utc)
                }
            }
        )
    
    async def update_job(self, job_id: str, job_data: dict):
        """Update job details"""
        update_data = {
            'job_name': job_data['name'],
            'source_channel_id': job_data['source'],
            'target_channel_id': job_data['target'],
            'start_post_id': job_data['start_id'],
            'end_post_id': job_data['end_id'],
            'batch_size': job_data['batch_size'],
            'recurring_time': job_data['recurring_time'],
            'delete_time': job_data['delete_time'],
            'filter_type': job_data['filter_type'],
            'custom_caption': job_data.get('caption', ''),
            'button_text': job_data.get('button_text', ''),
            'button_url': job_data.get('button_url', ''),
            'updated_at': datetime.utcnow().replace(tzinfo=timezone.utc)
        }
        
        await self.db.jobs.update_one(
            {'_id': ObjectId(job_id)},
            {'$set': update_data}
        )
    
    async def delete_job(self, job_id: str):
        """Delete a job and its related data"""
        # Delete forwarded messages first
        await self.db.forwarded_messages.delete_many({'job_id': job_id})
        
        # Delete the job
        await self.db.jobs.delete_one({'_id': ObjectId(job_id)})
    
    async def update_last_forwarded(self, job_id: str, message_id: int):
        """Update the last forwarded message ID"""
        await self.db.jobs.update_one(
            {'_id': ObjectId(job_id)},
            {
                '$set': {
                    'last_forwarded_id': message_id,
                    'updated_at': datetime.utcnow().replace(tzinfo=timezone.utc)
                }
            }
        )
    
    async def add_forwarded_message(self, job_id: str, original_id: int, forwarded_id: int):
        """Track a forwarded message"""
        doc = {
            'job_id': job_id,
            'original_message_id': original_id,
            'forwarded_message_id': forwarded_id,
            'forwarded_at': datetime.utcnow().replace(tzinfo=timezone.utc)
        }
        await self.db.forwarded_messages.insert_one(doc)
    
    async def get_old_forwarded_messages(self, job_id: str, minutes_ago: int) -> List[int]:
        """Get forwarded messages older than specified minutes"""
        if minutes_ago <= 0:
            return []
        
        cutoff_time = datetime.utcnow().replace(tzinfo=timezone.utc) - timedelta(minutes=minutes_ago)
        
        cursor = self.db.forwarded_messages.find({
            'job_id': job_id,
            'forwarded_at': {'$lt': cutoff_time}
        })
        
        message_ids = []
        async for doc in cursor:
            message_ids.append(doc['forwarded_message_id'])
        
        # Clean up old records
        await self.db.forwarded_messages.delete_many({
            'job_id': job_id,
            'forwarded_at': {'$lt': cutoff_time}
        })
        
        return message_ids
    
    async def save_user_state(self, user_id: int, state_data: dict):
        """Save user's current state"""
        await self.db.user_states.update_one(
            {'user_id': user_id},
            {
                '$set': {
                    'state_data': json.dumps(state_data),
                    'updated_at': datetime.utcnow().replace(tzinfo=timezone.utc)
                }
            },
            upsert=True
        )
    
    async def get_user_state(self, user_id: int) -> Optional[dict]:
        """Get user's current state"""
        doc = await self.db.user_states.find_one({'user_id': user_id})
        if doc:
            return json.loads(doc['state_data'])
        return None
    
    async def clear_user_state(self, user_id: int):
        """Clear user's state"""
        await self.db.user_states.delete_one({'user_id': user_id})
    
    async def reset_job_progress(self, job_id: str, start_post_id: int):
        """Reset the last forwarded message ID for a job"""
        await self.db.jobs.update_one(
            {'_id': ObjectId(job_id)},
            {
                '$set': {
                    'last_forwarded_id': start_post_id - 1,
                    'updated_at': datetime.utcnow().replace(tzinfo=timezone.utc)
                }
            }
        )
        
        # Clear tracked forwarded messages
        await self.db.forwarded_messages.delete_many({'job_id': job_id})
    
    async def add_user_if_not_exists(self, user_id: int):
        """Add a user to the users collection if they don't already exist"""
        await self.db.users.update_one(
            {'user_id': user_id},
            {
                '$setOnInsert': {
                    'first_interaction_at': datetime.utcnow().replace(tzinfo=timezone.utc)
                }
            },
            upsert=True
        )
    
    async def get_total_users(self) -> int:
        """Get the total count of unique users"""
        return await self.db.users.count_documents({})
    
    async def get_total_jobs(self) -> int:
        """Get the total count of all jobs"""
        return await self.db.jobs.count_documents({})
    
    async def get_total_forwarded_messages(self) -> int:
        """Get the total count of all forwarded messages"""
        return await self.db.forwarded_messages.count_documents({})
    
    async def get_jobs_created_today(self) -> int:
        """Get the count of jobs created today"""
        today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
        return await self.db.jobs.count_documents({'created_at': {'$gte': today_start}})
    
    async def get_forwarded_messages_today(self) -> int:
        """Get the count of messages forwarded today"""
        today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
        return await self.db.forwarded_messages.count_documents({'forwarded_at': {'$gte': today_start}})

class AutoposterBot:
    def __init__(self):
        try:
            self.app = Client(
                "autoposter_bot",
                api_id=API_ID,
                api_hash=API_HASH,
                bot_token=BOT_TOKEN
            )
            
            self.db = Database()
            self.active_jobs = {}
            self.job_locks = {}
            self.job_tasks = {}
            self.force_sub_channel_id = FORCE_SUB_CHANNEL_ID
            self.admin_ids = ADMIN_IDS
            
            # Register handlers
            self.register_handlers()
            
            logger.info("✅ Bot initialized successfully!")
            
        except Exception as e:
            logger.error(f"❌ Failed to initialize bot: {e}")
            print(f"❌ Bot initialization failed: {e}")
            print("\n🔧 Please check your configuration and try again.")
            exit(1)
    
    def register_handlers(self):
        """Register all bot handlers"""
        
        @self.app.on_message(filters.command("start") & filters.private)
        async def start_command(client: Client, message: Message):
            await self.handle_start(client, message)
        
        @self.app.on_message(filters.command("stats") & filters.private)
        async def stats_command(client: Client, message: Message):
            await self.handle_stats(client, message)
        
        @self.app.on_callback_query()
        async def callback_handler(client: Client, callback_query: CallbackQuery):
            await self.handle_callback(client, callback_query)
        
        @self.app.on_message(filters.text & filters.private & ~filters.command("start") & ~filters.command("stats"))
        async def text_handler(client: Client, message: Message):
            await self.handle_text_message(client, message)

        @self.app.on_chat_join_request()
        async def join_request_handler(client: Client, join_request):
            try:
                await client.approve_chat_join_request(join_request.chat.id, join_request.from_user.id)
                logger.info(f"✅ Approved join request for {join_request.from_user.id} in {join_request.chat.title}")
            except Exception as e:
                logger.error(f"❌ Error approving join request: {e}")
    
    def is_user_admin(self, user_id: int) -> bool:
        """Check if the given user ID is in the admin list"""
        return user_id in self.admin_ids
    
    async def check_user_subscription(self, user_id: int, message_obj: Union[Message, CallbackQuery]) -> bool:
        """Check if user is subscribed to force subscribe channel"""
        if not self.force_sub_channel_id:
            return True
        
        try:
            await asyncio.sleep(FORCE_SUB_CHECK_DELAY)
            member = await self.app.get_chat_member(self.force_sub_channel_id, user_id)
            if member.status in [enums.ChatMemberStatus.MEMBER, enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER]:
                return True
        except UserNotParticipant:
            pass
        except Exception as e:
            logger.error(f"Error checking subscription: {e}")
            return False

        try:
            channel = await self.app.get_chat(self.force_sub_channel_id)
            channel_name = channel.title
            channel_link = f"https://t.me/{channel.username}" if channel.username else "https://t.me/"
            
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton(f"🚀 Join {channel_name}", url=channel_link)]
            ])
            
            text = f"""👋 Hello! To use this bot, you must join our channel: <b>{channel_name}</b>.

Please join the channel and then send /start again."""

            if isinstance(message_obj, Message):
                await message_obj.reply_text(text, reply_markup=keyboard, parse_mode=enums.ParseMode.HTML)
            elif isinstance(message_obj, CallbackQuery):
                await message_obj.message.edit_text(text, reply_markup=keyboard, parse_mode=enums.ParseMode.HTML)
        except Exception as e:
            logger.error(f"Error showing subscription message: {e}")

        return False
    
    async def handle_start(self, client: Client, message: Message, is_edit: bool = False, user_id: int = None):
        """Handle /start command or return to main menu"""
        if user_id is None:
            user_id = message.from_user.id
        await self.db.add_user_if_not_exists(user_id)
        
        if not await self.check_user_subscription(user_id, message):
            return
        
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🆕 Create New Job", callback_data="create_job")],
            [InlineKeyboardButton("📋 My Jobs", callback_data="my_jobs")],
            [InlineKeyboardButton("ℹ️ Help", callback_data="help")]
        ])
        
        welcome_text = """🤖 <b>Autoposter Bot</b>

Forward posts between channels automatically."""
        
        if is_edit:
            await message.edit_text(
                welcome_text,
                reply_markup=keyboard,
                parse_mode=enums.ParseMode.HTML
            )
        else:
            await message.reply_text(
                welcome_text,
                reply_markup=keyboard,
                parse_mode=enums.ParseMode.HTML
            )
    
    async def handle_stats(self, client: Client, message: Message):
        """Handle /stats command for admin users"""
        user_id = message.from_user.id
        if not self.is_user_admin(user_id):
            await message.reply_text("🚫 You are not authorized to use this command.")
            return
        
        total_users = await self.db.get_total_users()
        total_jobs = await self.db.get_total_jobs()
        total_forwarded_messages = await self.db.get_total_forwarded_messages()
        
        today_jobs = await self.db.get_jobs_created_today()
        today_forwarded_messages = await self.db.get_forwarded_messages_today()
        
        stats_text = f"""📊 <b>Bot Statistics</b>

<b>Today's Stats:</b>
• New Jobs Created: <b>{today_jobs}</b>
• Messages Forwarded: <b>{today_forwarded_messages}</b>

<b>Overall Stats:</b>
• Total Unique Users: <b>{total_users}</b>
• Total Jobs Created: <b>{total_jobs}</b>
• Total Messages Forwarded: <b>{total_forwarded_messages}</b>"""
        
        await message.reply_text(stats_text, parse_mode=enums.ParseMode.HTML)
    
    async def handle_callback(self, client: Client, callback_query: CallbackQuery):
        """Handle callback queries"""
        try:
            await callback_query.answer()
            
            user_id = callback_query.from_user.id
            await self.db.add_user_if_not_exists(user_id)
            
            if not await self.check_user_subscription(user_id, callback_query):
                return
            
            data = callback_query.data
            
            if data == "create_job":
                await self.start_job_creation(client, callback_query)
            elif data == "my_jobs":
                await self.show_user_jobs(client, callback_query)
            elif data == "help":
                await self.show_help(client, callback_query)
            elif data.startswith("job_"):
                await self.handle_job_action(client, callback_query, data)
            elif data.startswith("filter_"):
                await self.handle_filter_selection(client, callback_query, data)
            elif data.startswith("edit_"):
                await self.handle_edit_action(client, callback_query, data)
            elif data == "back_to_main":
                await self.handle_start(client, callback_query.message, is_edit=True, user_id=user_id)
            
        except Exception as e:
            logger.error(f"Error in callback handler: {e}")
            await callback_query.answer("❌ An error occurred. Please try again.", show_alert=True)
    
    async def start_job_creation(self, client: Client, callback_query: CallbackQuery):
        """Start job creation process"""
        user_id = callback_query.from_user.id
        
        state = {"step": "job_name", "mode": "create"}
        await self.db.save_user_state(user_id, state)
        
        text = """🆕 <b>Create New Autoposter Job</b>

Let's set up your forwarding job step by step.

<b>Step 1:</b> Enter a name for your job
Example: <code>News Channel Forward</code>
"""
        
        await callback_query.edit_message_text(
            text,
            parse_mode=enums.ParseMode.HTML
        )
    
    async def show_user_jobs(self, client: Client, callback_query: CallbackQuery):
        """Show user's jobs"""
        user_id = callback_query.from_user.id
        jobs = await self.db.get_user_jobs(user_id)
        
        if not jobs:
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🆕 Create First Job", callback_data="create_job")],
                [InlineKeyboardButton("🔙 Back", callback_data="back_to_main")]
            ])
            
            await callback_query.edit_message_text(
                "📋 <b>Your Jobs</b>\n\nYou don't have any jobs yet. Create your first job!",
                reply_markup=keyboard,
                parse_mode=enums.ParseMode.HTML
            )
            return
        
        text = "📋 <b>Your Jobs</b>\n\n"
        keyboard = []
        
        for job in jobs:
            status = "🟢 Active" if job['is_active'] else "🔴 Inactive"
            text += f"• <b>{job['job_name']}</b> - {status}\n"
            
            keyboard.append([
                InlineKeyboardButton(
                    f"⚙️ {job['job_name']}", 
                    callback_data=f"job_manage_{job['id']}"
                )
            ])
        
        keyboard.extend([
            [InlineKeyboardButton("🆕 Create New Job", callback_data="create_job")],
            [InlineKeyboardButton("🔙 Back", callback_data="back_to_main")]
        ])
        
        await callback_query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=enums.ParseMode.HTML
        )
    
    async def show_help(self, client: Client, callback_query: CallbackQuery):
        """Show help information"""
        help_text = """ℹ️ <b>Help</b>

<b>How to use:</b>
1. Create a new job with source and target channels
2. Bot must be admin in both channels
3. Use message links for start/end posts
4. Configure forwarding settings
5. Start the job to begin forwarding

<b>Features:</b>
• Filter by media/text/all posts
• Custom captions and buttons
• Auto-delete old messages
• Batch forwarding with delays
• Job management (start/stop/edit/reset)

<b>Tips:</b>
• Use high numbers (999999) for end post to include future posts
• Set appropriate delays to avoid rate limits
• Monitor job status regularly
"""
        
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Back", callback_data="back_to_main")]
        ])
        
        await callback_query.edit_message_text(
            help_text,
            reply_markup=keyboard,
            parse_mode=enums.ParseMode.HTML
        )
    
    async def handle_text_message(self, client: Client, message: Message):
        """Handle text messages based on user state"""
        user_id = message.from_user.id
        await self.db.add_user_if_not_exists(user_id)
        
        if not await self.check_user_subscription(user_id, message):
            return
        
        state = await self.db.get_user_state(user_id)
        
        if not state:
            return
        
        step = state.get("step")
        
        try:
            if step == "job_name":
                await self.handle_job_name(client, message, state)
            elif step == "source_channel":
                await self.handle_source_channel(client, message, state)
            elif step == "target_channel":
                await self.handle_target_channel(client, message, state)
            elif step == "start_post":
                await self.handle_start_post(client, message, state)
            elif step == "end_post":
                await self.handle_end_post(client, message, state)
            elif step == "batch_size":
                await self.handle_batch_size(client, message, state)
            elif step == "recurring_time":
                await self.handle_recurring_time(client, message, state)
            elif step == "delete_time":
                await self.handle_delete_time(client, message, state)
            elif step == "custom_caption":
                await self.handle_custom_caption(client, message, state)
            elif step == "button_text":
                await self.handle_button_text(client, message, state)
            elif step == "button_url":
                await self.handle_button_url(client, message, state)
        
        except Exception as e:
            logger.error(f"Error handling text message: {e}")
            await message.reply_text("❌ An error occurred. Please try again or use /start to restart.")
    
    async def handle_job_name(self, client: Client, message: Message, state: dict):
        """Handle job name input"""
        job_name = message.text.strip()
        
        if len(job_name) < 3:
            await message.reply_text("❌ Job name must be at least 3 characters long.")
            return
        
        state["job_name"] = job_name
        state["step"] = "source_channel"
        await self.db.save_user_state(message.from_user.id, state)
        
        text = """✅ Job name saved!

<b>Step 2:</b> Enter the source channel ID or username
You can send:
• Channel ID: <code>-1001234567890</code>
• Username: <code>@channelname</code>
• Channel link: <code>https://t.me/channelname</code>

⚠️ <b>Important:</b> Make sure the bot is admin in this channel!
"""
        
        await message.reply_text(text, parse_mode=enums.ParseMode.HTML)
    
    async def handle_source_channel(self, client: Client, message: Message, state: dict):
        """Handle source channel input"""
        channel_input = message.text.strip()
        channel_id = self.extract_channel_id(channel_input)
        
        if not channel_id:
            await message.reply_text("❌ Invalid channel format. Please try again.")
            return
        
        progress_msg = await message.reply_text("🔍 Checking channel access...")
        
        try:
            can_access = await self.test_channel_access(channel_id)
            if not can_access:
                await progress_msg.edit_text(
                    "❌ Cannot access this channel. Please check:\n"
                    "• Channel ID/username is correct\n"
                    "• Channel exists and is accessible\n"
                    "• Bot has been added to the channel"
                )
                return

            channel_info = await self.get_channel_info(channel_id)
            if not channel_info:
                await progress_msg.edit_text("❌ Cannot get channel information. Please try again.")
                return

            await progress_msg.edit_text("🔍 Checking admin permissions...")
            is_admin = await self.check_admin_status(channel_id)
            if not is_admin:
                await progress_msg.edit_text(
                    f"❌ Bot is not admin in <b>{channel_info['title']}</b>\n\n"
                    "Please:\n"
                    "1. Add the bot to the channel as admin\n"
                    "2. Give permissions: Post Messages, Delete Messages\n"
                    "3. Try again\n\n"
                    f"Channel: <code>{channel_id}</code>",
                    parse_mode=enums.ParseMode.HTML
                )
                return

            state["source_channel"] = channel_id
            state["source_info"] = channel_info
            state["step"] = "target_channel"
            await self.db.save_user_state(message.from_user.id, state)

            text = f"""✅ Source channel verified: <b>{channel_info['title']}</b>

<b>Step 3:</b> Enter the target channel ID or username
This is where the posts will be forwarded to.

⚠️ <b>Important:</b> Make sure the bot is admin in this channel too!
"""

            await progress_msg.edit_text(text, parse_mode=enums.ParseMode.HTML)
                
        except Exception as e:
            logger.error(f"Error checking source channel: {e}")
            await progress_msg.edit_text("❌ Error checking channel. Please try again.")
    
    async def handle_target_channel(self, client: Client, message: Message, state: dict):
        """Handle target channel input"""
        channel_input = message.text.strip()
        channel_id = self.extract_channel_id(channel_input)
        
        if not channel_id:
            await message.reply_text("❌ Invalid channel format. Please try again.")
            return
        
        progress_msg = await message.reply_text("🔍 Checking channel access...")
        
        try:
            can_access = await self.test_channel_access(channel_id)
            if not can_access:
                await progress_msg.edit_text(
                    "❌ Cannot access this channel. Please check:\n"
                    "• Channel ID/username is correct\n"
                    "• Channel exists and is accessible\n"
                    "• Bot has been added to the channel"
                )
                return

            channel_info = await self.get_channel_info(channel_id)
            if not channel_info:
                await progress_msg.edit_text("❌ Cannot get channel information. Please try again.")
                return

            await progress_msg.edit_text("🔍 Checking admin permissions...")
            is_admin = await self.check_admin_status(channel_id)
            if not is_admin:
                await progress_msg.edit_text(
                    f"❌ Bot is not admin in <b>{channel_info['title']}</b>\n\n"
                    "Please:\n"
                    "1. Add the bot to the channel as admin\n"
                    "2. Give permissions: Post Messages, Delete Messages\n"
                    "3. Try again\n\n"
                    f"Channel: <code>{channel_id}</code>",
                    parse_mode=enums.ParseMode.HTML
                )
                return

            state["target_channel"] = channel_id
            state["target_info"] = channel_info
            await self.db.save_user_state(message.from_user.id, state)

            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("📷 Media Only", callback_data="filter_media")],
                [InlineKeyboardButton("📝 Text Only", callback_data="filter_text")],
                [InlineKeyboardButton("📋 All Posts", callback_data="filter_all")]
            ])

            text = f"""✅ Target channel verified: <b>{channel_info['title']}</b>

<b>Step 4:</b> Choose what type of posts to forward:
"""

            await progress_msg.edit_text(
                text,
                reply_markup=keyboard,
                parse_mode=enums.ParseMode.HTML
            )
                
        except Exception as e:
            logger.error(f"Error checking target channel: {e}")
            await progress_msg.edit_text("❌ Error checking channel. Please try again.")
    
    async def handle_filter_selection(self, client: Client, callback_query: CallbackQuery, data: str):
        """Handle filter type selection"""
        user_id = callback_query.from_user.id
        filter_type = data.split("_")[1]
        
        state = await self.db.get_user_state(user_id)
        if not state:
            await callback_query.answer("❌ Session expired. Please start over.", show_alert=True)
            return
        
        state["filter_type"] = filter_type
        state["step"] = "start_post"
        await self.db.save_user_state(user_id, state)
        
        filter_names = {"media": "📷 Media Only", "text": "📝 Text Only", "all": "📋 All Posts"}
        
        text = f"""✅ Filter set to: <b>{filter_names[filter_type]}</b>

<b>Step 5:</b> Send the link of the FIRST post to forward
Example: <code>https://t.me/channelname/123</code>

This will be your starting point for forwarding.
"""
        
        await callback_query.edit_message_text(text, parse_mode=enums.ParseMode.HTML)
    
    async def handle_start_post(self, client: Client, message: Message, state: dict):
        """Handle start post link"""
        post_link = message.text.strip()
        message_id = self.extract_message_id_from_link(post_link)
        
        if not message_id:
            await message.reply_text("❌ Invalid message link format. Please try again.")
            return
        
        state["start_post_id"] = message_id
        state["step"] = "end_post"
        await self.db.save_user_state(message.from_user.id, state)
        
        text = f"""✅ Start post ID: <b>{message_id}</b>

<b>Step 6:</b> Send the link of the LAST post to forward
Example: <code>https://t.me/channelname/456</code>

This sets the range of posts to forward. You can use a very high number (like 999999) to include all future posts.
"""
        
        await message.reply_text(text, parse_mode=enums.ParseMode.HTML)
    
    async def handle_end_post(self, client: Client, message: Message, state: dict):
        """Handle end post link"""
        post_link = message.text.strip()
        
        if post_link.lower() in ["latest", "all", "999999"]:
            message_id = 999999
        else:
            message_id = self.extract_message_id_from_link(post_link)
            if not message_id:
                await message.reply_text("❌ Invalid message link format. Please try again or send 'latest' for all posts.")
                return
        
        start_id = state["start_post_id"]
        if message_id < start_id and message_id != 999999:
            await message.reply_text("❌ End post ID must be greater than start post ID.")
            return
        
        state["end_post_id"] = message_id
        state["step"] = "batch_size"
        await self.db.save_user_state(message.from_user.id, state)
        
        end_text = "All future posts" if message_id == 999999 else str(message_id)
        text = f"""✅ End post ID: <b>{end_text}</b>

<b>Step 7:</b> Enter batch size (1-20)
This is how many posts will be forwarded in each cycle.
Example: <code>5</code>
"""
        
        await message.reply_text(text, parse_mode=enums.ParseMode.HTML)
    
    async def handle_batch_size(self, client: Client, message: Message, state: dict):
        """Handle batch size input"""
        try:
            batch_size = int(message.text.strip())
            if batch_size < 1 or batch_size > 20:
                raise ValueError()
        except ValueError:
            await message.reply_text("❌ Batch size must be a number between 1 and 20.")
            return
        
        state["batch_size"] = batch_size
        state["step"] = "recurring_time"
        await self.db.save_user_state(message.from_user.id, state)
        
        text = f"""✅ Batch size: <b>{batch_size} posts</b>

<b>Step 8:</b> Enter recurring time in minutes (1-1440)
This is how often the bot will forward a new batch.
Example: <code>30</code> (every 30 minutes)
"""
        
        await message.reply_text(text, parse_mode=enums.ParseMode.HTML)
    
    async def handle_recurring_time(self, client: Client, message: Message, state: dict):
        """Handle recurring time input"""
        try:
            recurring_time = int(message.text.strip())
            if recurring_time < 1 or recurring_time > 1440:
                raise ValueError()
        except ValueError:
            await message.reply_text("❌ Recurring time must be between 1 and 1440 minutes.")
            return
        
        state["recurring_time"] = recurring_time
        state["step"] = "delete_time"
        await self.db.save_user_state(message.from_user.id, state)
        
        text = f"""✅ Recurring time: <b>{recurring_time} minutes</b>

<b>Step 9:</b> Enter delete time in minutes (0-10080)
This is how long to keep forwarded posts before deleting them.
Use <code>0</code> to never delete posts.
Example: <code>60</code> (delete after 1 hour)
"""
        
        await message.reply_text(text, parse_mode=enums.ParseMode.HTML)
    
    async def handle_delete_time(self, client: Client, message: Message, state: dict):
        """Handle delete time input"""
        try:
            delete_time = int(message.text.strip())
            if delete_time < 0 or delete_time > 10080:
                raise ValueError()
        except ValueError:
            await message.reply_text("❌ Delete time must be between 0 and 10080 minutes.")
            return
        
        state["delete_time"] = delete_time
        state["step"] = "custom_caption"
        await self.db.save_user_state(message.from_user.id, state)
        
        text = f"""✅ Delete time: <b>{delete_time} minutes</b>

<b>Step 10:</b> Enter custom caption (optional)
You can use HTML formatting:
• <code>&lt;b&gt;Bold&lt;/b&gt;</code>
• <code>&lt;i&gt;Italic&lt;/i&gt;</code>
• <code>&lt;u&gt;Underlined&lt;/u&gt;</code>
• <code>&lt;a href="link"&gt;Text&lt;/a&gt;</code>

Send <code>skip</code> to use original captions.
"""
        
        await message.reply_text(text, parse_mode=enums.ParseMode.HTML)
    
    async def handle_custom_caption(self, client: Client, message: Message, state: dict):
        """Handle custom caption input"""
        caption = message.text.strip()
        
        if caption.lower() == "skip":
            caption = ""
        
        state["custom_caption"] = caption
        state["step"] = "button_text"
        await self.db.save_user_state(message.from_user.id, state)
        
        text = """✅ Custom caption saved!

<b>Step 11:</b> Enter button text (optional)
This will add an inline button to forwarded posts.
Send <code>skip</code> to not add a button.
"""
        
        await message.reply_text(text, parse_mode=enums.ParseMode.HTML)
    
    async def handle_button_text(self, client: Client, message: Message, state: dict):
        """Handle button text input"""
        button_text = message.text.strip()
        
        if button_text.lower() == "skip":
            await self.finalize_job(client, message, state)
            return
        
        state["button_text"] = button_text
        state["step"] = "button_url"
        await self.db.save_user_state(message.from_user.id, state)
        
        text = f"""✅ Button text: <b>{button_text}</b>

<b>Step 12:</b> Enter button URL
Example: <code>https://t.me/yourchannel</code>
"""
        
        await message.reply_text(text, parse_mode=enums.ParseMode.HTML)
    
    async def handle_button_url(self, client: Client, message: Message, state: dict):
        """Handle button URL input"""
        button_url = message.text.strip()
        
        if not button_url.startswith(('http://', 'https://', 'tg://')):
            await message.reply_text("❌ Please enter a valid URL starting with http:// or https://")
            return
        
        state["button_url"] = button_url
        await self.finalize_job(client, message, state)
    
    async def finalize_job(self, client: Client, message: Message, state: dict):
        """Finalize and create/update the job"""
        user_id = message.from_user.id
        
        job_data = {
            'name': state['job_name'],
            'source': state['source_channel'],
            'target': state['target_channel'],
            'start_id': state['start_post_id'],
            'end_id': state['end_post_id'],
            'batch_size': state['batch_size'],
            'recurring_time': state['recurring_time'],
            'delete_time': state['delete_time'],
            'filter_type': state['filter_type'],
            'caption': state.get('custom_caption', ''),
            'button_text': state.get('button_text', ''),
            'button_url': state.get('button_url', '')
        }
        
        if state.get('mode') == 'edit':
            # Update existing job
            job_id = state['job_id']
            await self.db.update_job(job_id, job_data)
            action_text = "Updated"
            button_text = "▶️ Start Job" if not state.get('was_active') else "⏹️ Stop Job"
            button_callback = f"job_start_{job_id}" if not state.get('was_active') else f"job_stop_{job_id}"
        else:
            # Create new job
            job_id = await self.db.create_job(user_id, job_data)
            action_text = "Created"
            button_text = "▶️ Start Job"
            button_callback = f"job_start_{job_id}"
        
        await self.db.clear_user_state(user_id)
        
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(button_text, callback_data=button_callback)],
            [InlineKeyboardButton("📋 My Jobs", callback_data="my_jobs")]
        ])
        
        delete_info = "Never delete" if job_data['delete_time'] == 0 else f"Delete after {job_data['delete_time']} min"
        end_info = "All future posts" if job_data['end_id'] == 999999 else str(job_data['end_id'])
        
        text = f"""🎉 <b>Job {action_text} Successfully!</b>

<b>📋 Job Details:</b>
• Name: <b>{job_data['name']}</b>
• Source: <b>{state['source_info']['title']}</b>
• Target: <b>{state['target_info']['title']}</b>
• Posts Range: <b>{job_data['start_id']} - {end_info}</b>
• Batch: <b>{job_data['batch_size']} posts every {job_data['recurring_time']} min</b>
• Filter: <b>{job_data['filter_type'].title()}</b>
• Delete: <b>{delete_info}</b>

Ready to start forwarding!
"""
        
        await message.reply_text(
            text,
            reply_markup=keyboard,
            parse_mode=enums.ParseMode.HTML
        )
    
    async def handle_job_action(self, client: Client, callback_query: CallbackQuery, data: str):
        """Handle job management actions"""
        try:
            action_parts = data.split("_")
            action = action_parts[1]
            job_id = action_parts[2]
            
            job = await self.db.get_job(job_id)
            if not job:
                await callback_query.answer("❌ Job not found.", show_alert=True)
                return
            
            if action == "start":
                await self.start_job(client, callback_query, job_id)
            elif action == "stop":
                await self.stop_job(client, callback_query, job_id)
            elif action == "manage":
                await self.show_job_management(client, callback_query, job_id)
            elif action == "reset":
                await self.reset_job_progress_action(client, callback_query, job_id)
            elif action == "edit":
                await self.start_job_edit(client, callback_query, job_id)
            elif action == "delete":
                await self.confirm_job_deletion(client, callback_query, job_id)
            elif action == "confirmdelete":
                await self.delete_job_confirmed(client, callback_query, job_id)
        
        except Exception as e:
            logger.error(f"Error in job action: {e}")
            await callback_query.answer("❌ An error occurred.", show_alert=True)
    
    async def show_job_management(self, client: Client, callback_query: CallbackQuery, job_id: str):
        """Show job management options"""
        job = await self.db.get_job(job_id)
        if not job:
            await callback_query.answer("❌ Job not found.", show_alert=True)
            return
        
        status = "🟢 Active" if job['is_active'] else "🔴 Inactive"
        
        keyboard = []
        if job['is_active']:
            keyboard.append([InlineKeyboardButton("⏹️ Stop Job", callback_data=f"job_stop_{job_id}")])
        else:
            keyboard.append([InlineKeyboardButton("▶️ Start Job", callback_data=f"job_start_{job_id}")])
        
        keyboard.extend([
            [
                InlineKeyboardButton("✏️ Edit Job", callback_data=f"job_edit_{job_id}"),
                InlineKeyboardButton("🔄 Reset Progress", callback_data=f"job_reset_{job_id}")
            ],
            [InlineKeyboardButton("🗑️ Delete Job", callback_data=f"job_delete_{job_id}")],
            [InlineKeyboardButton("🔙 Back to Jobs", callback_data="my_jobs")]
        ])
        
        delete_info = "Never" if job['delete_time'] == 0 else f"{job['delete_time']} min"
        end_info = "All future" if job['end_post_id'] == 999999 else str(job['end_post_id'])
        
        text = f"""⚙️ <b>Managing Job: {job['job_name']}</b>

<b>Status:</b> {status}
<b>Source:</b> {job['source_channel_id']}
<b>Target:</b> {job['target_channel_id']}
<b>Posts Range:</b> {job['start_post_id']} - {end_info}
<b>Batch Size:</b> {job['batch_size']} posts
<b>Frequency:</b> Every {job['recurring_time']} minutes
<b>Delete after:</b> {delete_info}
<b>Last Forwarded:</b> {job['last_forwarded_id']}
"""
        
        await callback_query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=enums.ParseMode.HTML
        )
    
    async def start_job(self, client: Client, callback_query: CallbackQuery, job_id: str):
        """Start a job"""
        job = await self.db.get_job(job_id)
        if not job:
            await callback_query.answer("❌ Job not found.", show_alert=True)
            return
        
        if job['is_active']:
            await callback_query.answer("⚠️ Job is already running!", show_alert=True)
            return
        
        await self.db.update_job_status(job_id, True)
        
        if job_id not in self.active_jobs or not self.active_jobs[job_id]:
            self.active_jobs[job_id] = True
            self.job_locks[job_id] = asyncio.Lock()
            self.job_tasks[job_id] = asyncio.create_task(self.run_job(client, job_id))
        
        await callback_query.edit_message_text(
            f"✅ Job <b>{job['job_name']}</b> started successfully!",
            parse_mode=enums.ParseMode.HTML
        )
    
    async def stop_job(self, client: Client, callback_query: CallbackQuery, job_id: str):
        """Stop a job"""
        job = await self.db.get_job(job_id)
        if not job:
            await callback_query.answer("❌ Job not found.", show_alert=True)
            return
        
        await self.db.update_job_status(job_id, False)
        
        if job_id in self.active_jobs:
            self.active_jobs[job_id] = False
        
        await callback_query.edit_message_text(
            f"⏹️ Job <b>{job['job_name']}</b> stopped successfully!",
            parse_mode=enums.ParseMode.HTML
        )
    
    async def reset_job_progress_action(self, client: Client, callback_query: CallbackQuery, job_id: str):
        """Handle resetting job progress"""
        job = await self.db.get_job(job_id)
        if not job:
            await callback_query.answer("❌ Job not found.", show_alert=True)
            return
        
        if job['is_active']:
            await callback_query.answer("⚠️ Please stop the job before resetting its progress.", show_alert=True)
            return
        
        await self.db.reset_job_progress(job_id, job['start_post_id'])
        
        await callback_query.edit_message_text(
            f"🔄 Progress for job <b>{job['job_name']}</b> has been reset. It will now start from message {job['start_post_id']}.",
            parse_mode=enums.ParseMode.HTML
        )
        
        await asyncio.sleep(2)
        await self.show_job_management(client, callback_query, job_id)
    
    async def start_job_edit(self, client: Client, callback_query: CallbackQuery, job_id: str):
        """Start job editing process"""
        job = await self.db.get_job(job_id)
        if not job:
            await callback_query.answer("❌ Job not found.", show_alert=True)
            return
        
        if job['is_active']:
            await callback_query.answer("⚠️ Please stop the job before editing.", show_alert=True)
            return
        
        user_id = callback_query.from_user.id
        
        # Pre-populate state with existing job data
        state = {
            "step": "job_name",
            "mode": "edit",
            "job_id": job_id,
            "was_active": job['is_active'],
            "job_name": job['job_name'],
            "source_channel": job['source_channel_id'],
            "target_channel": job['target_channel_id'],
            "start_post_id": job['start_post_id'],
            "end_post_id": job['end_post_id'],
            "batch_size": job['batch_size'],
            "recurring_time": job['recurring_time'],
            "delete_time": job['delete_time'],
            "filter_type": job['filter_type'],
            "custom_caption": job['custom_caption'],
            "button_text": job['button_text'],
            "button_url": job['button_url']
        }
        
        await self.db.save_user_state(user_id, state)
        
        text = f"""✏️ <b>Edit Job: {job['job_name']}</b>

Let's update your job settings step by step.

<b>Step 1:</b> Enter a new name for your job
Current: <code>{job['job_name']}</code>

Send the new name or <code>keep</code> to keep current value.
"""
        
        await callback_query.edit_message_text(
            text,
            parse_mode=enums.ParseMode.HTML
        )
    
    async def confirm_job_deletion(self, client: Client, callback_query: CallbackQuery, job_id: str):
        """Confirm job deletion"""
        job = await self.db.get_job(job_id)
        if not job:
            await callback_query.answer("❌ Job not found.", show_alert=True)
            return
        
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Yes, Delete", callback_data=f"job_confirmdelete_{job_id}"),
                InlineKeyboardButton("❌ Cancel", callback_data=f"job_manage_{job_id}")
            ]
        ])
        
        text = f"""🗑️ <b>Delete Job Confirmation</b>

Are you sure you want to delete the job <b>"{job['job_name']}"</b>?

⚠️ <b>Warning:</b> This action cannot be undone. All job data and forwarded message history will be permanently deleted.
"""
        
        await callback_query.edit_message_text(
            text,
            reply_markup=keyboard,
            parse_mode=enums.ParseMode.HTML
        )
    
    async def delete_job_confirmed(self, client: Client, callback_query: CallbackQuery, job_id: str):
        """Delete job after confirmation"""
        job = await self.db.get_job(job_id)
        if not job:
            await callback_query.answer("❌ Job not found.", show_alert=True)
            return
        
        # Stop job if active
        if job['is_active']:
            await self.db.update_job_status(job_id, False)
            if job_id in self.active_jobs:
                self.active_jobs[job_id] = False
        
        # Delete job from database
        await self.db.delete_job(job_id)
        
        await callback_query.edit_message_text(
            f'🗑 Job <b>"{job["job_name"]}"</b> has been deleted successfully.' ,        
            parse_mode=enums.ParseMode.HTML
        )
        
        await asyncio.sleep(2)
        await self.show_user_jobs(client, callback_query)
    
    async def handle_edit_action(self, client: Client, callback_query: CallbackQuery, data: str):
        """Handle edit-specific actions"""
        # This can be extended for more edit-specific functionality
        pass
    
    # [Rest of the methods remain the same as in the original code]
    # Including: run_job, process_job_batch, get_message_type_from_raw_data, 
    # message_matches_filter_raw, send_custom_message, cleanup_old_messages,
    # test_channel_access, get_channel_info, check_admin_status,
    # extract_channel_id, extract_message_id_from_link
    
    async def run_job(self, client: Client, job_id: str):
        """Main job execution loop"""
        logger.info(f"Starting job {job_id}")
        
        while job_id in self.active_jobs and self.active_jobs[job_id]:
            try:
                async with self.job_locks[job_id]:
                    job = await self.db.get_job(job_id)
                    if not job or not job['is_active']:
                        break
                    
                    await self.process_job_batch(job)

                    if job['delete_time'] > 0:
                        await self.cleanup_old_messages(job)
                
                if job['end_post_id'] != 999999 and job['last_forwarded_id'] >= job['end_post_id']:
                    logger.info(f"Job {job['id']}: Reached end of specified posts. Pausing.")
                    await asyncio.sleep(job['recurring_time'] * 60 * 2)
                else:
                    await asyncio.sleep(job['recurring_time'] * 60)
                    
            except FloodWait as e:
                logger.warning(f"FloodWait in job {job_id}: {e.value} seconds")
                await asyncio.sleep(e.value)
            except Exception as e:
                logger.error(f"Error in job {job_id}: {e}")
                await asyncio.sleep(60)
        
        if job_id in self.active_jobs:
            del self.active_jobs[job_id]
        if job_id in self.job_locks:
            del self.job_locks[job_id]
        
        logger.info(f"Job {job_id} stopped")
    
    async def process_job_batch(self, job: dict):
        """Process a batch of messages for forwarding using optimized Pyrogram methods"""
        current_message_id = max(job['last_forwarded_id'] + 1, job['start_post_id'])
        messages_to_forward = []
        last_checked_message_id = job['last_forwarded_id']

        # Check up to 5x batch size or 100 messages to find matching ones
        fetch_limit = min(job['batch_size'] * 5, 100)

        logger.info(f"Job {job['id']}: Starting batch search from message ID {current_message_id}")
        
        while len(messages_to_forward) < job['batch_size']:
            if job['end_post_id'] != 999999 and current_message_id > job['end_post_id']:
                break
            
            end_id = current_message_id + fetch_limit
            if job['end_post_id'] != 999999:
                end_id = min(end_id, job['end_post_id'] + 1)

            message_ids = list(range(current_message_id, end_id))
            if not message_ids:
                break
                
            try:
                msgs = await self.app.get_messages(job['source_channel_id'], message_ids)
                if not isinstance(msgs, list):
                    msgs = [msgs]
                
                for msg in msgs:
                    last_checked_message_id = msg.id
                    if msg.empty:
                        continue

                    if self.message_matches_filter(msg, job['filter_type']):
                        messages_to_forward.append(msg)
                        if len(messages_to_forward) >= job['batch_size']:
                            break
                
                current_message_id = last_checked_message_id + 1
                if len(messages_to_forward) < job['batch_size'] and job['end_post_id'] == 999999:
                    # If we didn't find enough and it's ongoing, stop for now to wait for new posts
                    break
                
            except FloodWait as e:
                logger.warning(f"FloodWait while searching for messages in job {job['id']}: {e.value} seconds")
                await asyncio.sleep(e.value)
            except Exception as e:
                logger.error(f"Error searching for messages in job {job['id']}: {e}")
                current_message_id += 1
                await asyncio.sleep(BATCH_DELAY)
        
        forwarded_count = 0
        for msg in messages_to_forward:
            try:
                sent_message = await self.send_custom_message(job, msg)
                if sent_message:
                    await self.db.add_forwarded_message(job['id'], msg.id, sent_message.id)
                    forwarded_count += 1
                await asyncio.sleep(FORWARD_DELAY)
            except FloodWait as e:
                logger.warning(f"FloodWait during forwarding in job {job['id']}: {e.value} seconds")
                await asyncio.sleep(e.value)
                break
            except Exception as e:
                logger.error(f"Error forwarding message {msg.id} in job {job['id']}: {e}")
        
        await self.db.update_last_forwarded(job['id'], last_checked_message_id)
        logger.info(f"Job {job['id']}: Forwarded {forwarded_count} messages in this batch.")
    
    def message_matches_filter(self, msg: Message, filter_type: str) -> bool:
        """Check if message matches the filter criteria"""
        if filter_type == "all":
            return True
        elif filter_type == "media":
            return bool(msg.media)
        elif filter_type == "text":
            return bool(msg.text and not msg.media)
        return False
    
    async def send_custom_message(self, job: dict, msg: Message):
        """Send message with custom caption and button using Pyrogram"""
        try:
            caption = job['custom_caption'] if job['custom_caption'] else msg.caption
            
            reply_markup = None
            if job['button_text'] and job['button_url']:
                reply_markup = InlineKeyboardMarkup([
                    [InlineKeyboardButton(job['button_text'], url=job['button_url'])]
                ])
            
            # copy() handles both text (as text) and media (as caption) correctly
            return await msg.copy(
                chat_id=job['target_channel_id'],
                caption=caption,
                reply_markup=reply_markup
            )
        except Exception as e:
            logger.error(f"Error sending custom message for job {job['id']}: {e}")
            return None
    
    async def cleanup_old_messages(self, job: dict):
        """Delete old forwarded messages using Pyrogram"""
        try:
            old_messages = await self.db.get_old_forwarded_messages(job['id'], job['delete_time'])
            
            if not old_messages:
                return
            
            logger.info(f"Job {job['id']}: Cleaning up {len(old_messages)} old messages")
            
            await self.app.delete_messages(job['target_channel_id'], old_messages)
            
            logger.info(f"Job {job['id']}: Successfully deleted old messages")
        except Exception as e:
            logger.error(f"Error in cleanup for job {job['id']}: {e}")
    
    async def test_channel_access(self, channel_id: Union[str, int]) -> bool:
        """Test if bot can access the channel"""
        try:
            await self.app.get_chat(channel_id)
            return True
        except Exception as e:
            logger.error(f"Error testing channel access for {channel_id}: {e}")
            return False
    
    async def get_channel_info(self, channel_id: Union[str, int]) -> Optional[dict]:
        """Get channel information"""
        try:
            chat = await self.app.get_chat(channel_id)
            return {
                'id': chat.id,
                'title': chat.title or chat.first_name or 'Unknown',
                'type': str(chat.type),
                'username': chat.username or ''
            }
        except Exception as e:
            logger.error(f"Error getting channel info for {channel_id}: {e}")
            return None
    
    async def check_admin_status(self, channel_id: Union[str, int]) -> bool:
        """Check if bot is admin in the channel"""
        try:
            await asyncio.sleep(ADMIN_DELAY)
            me = await self.app.get_me()
            member = await self.app.get_chat_member(channel_id, me.id)
            return member.status in [enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER]
        except Exception as e:
            logger.error(f"Error checking admin status for {channel_id}: {e}")
            return False
    
    def extract_channel_id(self, text: str) -> Optional[Union[str, int]]:
        """Extract channel ID from various formats"""
        text = text.strip()
        
        if text.lstrip('-').isdigit():
            return int(text)
        
        if text.startswith('@'):
            return text
        
        if 't.me/' in text:
            username = text.split('t.me/')[-1].split('/')[0]
            return f"@{username}"
        
        return None
    
    def extract_message_id_from_link(self, link: str) -> Optional[int]:
        """Extract message ID from Telegram message link"""
        try:
            match = re.search(r'/(\d+)$', link)
            if match:
                return int(match.group(1))
        except:
            pass
        return None
    
    async def start(self):
        """Start the bot"""
        logger.info("Starting Autoposter Bot...")
        await self.db.connect()
        await self.app.start()
        
        # Resume active jobs
        try:
            cursor = self.db.db.jobs.find({'is_active': True})
            async for job in cursor:
                job_id = str(job['_id'])
                self.active_jobs[job_id] = True
                self.job_locks[job_id] = asyncio.Lock()
                self.job_tasks[job_id] = asyncio.create_task(self.run_job(self.app, job_id))
                logger.info(f"Resumed active job: {job.get('job_name')} ({job_id})")
        except Exception as e:
            logger.error(f"Error resuming jobs: {e}")

        logger.info("Bot started successfully!")
        await asyncio.Event().wait()
    
    async def stop(self):
        """Stop the bot"""
        logger.info("Stopping bot...")
        
        for job_id in list(self.active_jobs.keys()):
            self.active_jobs[job_id] = False
        
        for job_id, task in self.job_tasks.items():
            if not task.done():
                task.cancel()

        await self.app.stop()
        logger.info("Bot stopped.")

async def main():
    """Main function"""
    bot = AutoposterBot()
    
    try:
        await bot.start()
    except KeyboardInterrupt:
        logger.info("Received interrupt signal")
    finally:
        await bot.stop()

if __name__ == "__main__":
    asyncio.run(main())

