import asyncio
import logging
import os
import sqlite3
from datetime import datetime, timedelta
import aiohttp
import yt_dlp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)
from telegram.constants import ChatMemberStatus
import json
import re
from functools import lru_cache
import tempfile
import shutil
from concurrent.futures import ThreadPoolExecutor

# Logging sozlamalari
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Bot sozlamalari
BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
PORT = int(os.getenv("PORT", 8080))
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "6852738257").split(",")]
DATABASE_PATH = os.getenv("DATABASE_URL", "bot_database.db")

# Thread pool for downloading
executor = ThreadPoolExecutor(max_workers=3)

# Ma'lumotlar bazasini yaratish
def init_database():
    conn = sqlite3.connect(DATABASE_PATH)
    cursor = conn.cursor()
    
    # Foydalanuvchilar jadvali
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            join_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            is_active BOOLEAN DEFAULT TRUE
        )
    ''')
    
    # Majburiy kanallar jadvali
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS required_channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id TEXT UNIQUE,
            channel_name TEXT,
            added_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Statistika jadvali
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS daily_stats (
            date DATE PRIMARY KEY,
            new_users INTEGER DEFAULT 0,
            active_users INTEGER DEFAULT 0,
            downloads INTEGER DEFAULT 0
        )
    ''')
    
    conn.commit()
    conn.close()

# Cache uchun
@lru_cache(maxsize=100)
def get_required_channels():
    conn = sqlite3.connect(DATABASE_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT channel_id, channel_name FROM required_channels')
    channels = cursor.fetchall()
    conn.close()
    return channels

# Foydalanuvchini ma'lumotlar bazasiga qo'shish
async def add_user(user_id, username=None, first_name=None):
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _add_user_sync, user_id, username, first_name)

def _add_user_sync(user_id, username, first_name):
    conn = sqlite3.connect(DATABASE_PATH)
    cursor = conn.cursor()
    
    cursor.execute('''
        INSERT OR IGNORE INTO users (user_id, username, first_name)
        VALUES (?, ?, ?)
    ''', (user_id, username, first_name))
    
    today = datetime.now().date()
    if cursor.rowcount > 0:
        # Yangi foydalanuvchi
        cursor.execute('''
            INSERT OR REPLACE INTO daily_stats (date, new_users, active_users, downloads)
            VALUES (?,
                    COALESCE((SELECT new_users FROM daily_stats WHERE date = ?), 0) + 1,
                    COALESCE((SELECT active_users FROM daily_stats WHERE date = ?), 0) + 1,
                    COALESCE((SELECT downloads FROM daily_stats WHERE date = ?), 0)
            )
        ''', (today, today, today, today))
    else:
        # Mavjud foydalanuvchi
        cursor.execute('''
            INSERT OR REPLACE INTO daily_stats (date, new_users, active_users, downloads)
            VALUES (?,
                    COALESCE((SELECT new_users FROM daily_stats WHERE date = ?), 0),
                    COALESCE((SELECT active_users FROM daily_stats WHERE date = ?), 0) + 1,
                    COALESCE((SELECT downloads FROM daily_stats WHERE date = ?), 0)
            )
        ''', (today, today, today, today))
    
    conn.commit()
    conn.close()

# Majburiy kanallarni tekshirish (optimallashtirilgan)
async def check_user_subscription(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    channels = get_required_channels()
    
    if not channels:
        return True
    
    # Parallel ravishda barcha kanallarni tekshirish
    tasks = []
    for channel_id, _ in channels:
        task = _check_single_channel(context, user_id, channel_id)
        tasks.append(task)
    
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    # Agar biron bir kanal False qaytarsa, False qaytaramiz
    for result in results:
        if isinstance(result, bool) and not result:
            return False
    
    return True

async def _check_single_channel(context, user_id, channel_id):
    try:
        member = await context.bot.get_chat_member(channel_id, user_id)
        return member.status in [ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER]
    except Exception:
        return True  # Xato bo'lsa, true qaytaramiz

# Obuna tugmalarini yaratish (cache bilan)
async def get_subscription_keyboard():
    channels = get_required_channels()
    
    if not channels:
        return None
    
    keyboard = []
    for channel_id, channel_name in channels:
        keyboard.append([InlineKeyboardButton(f"📢 {channel_name}", url=f"https://t.me/{channel_id.replace('@', '')}")])
    
    keyboard.append([InlineKeyboardButton("✅ Tekshirish", callback_data="check_subscription")])
    
    return InlineKeyboardMarkup(keyboard)

# Start komandasi
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await add_user(user.id, user.username, user.first_name)
    
    # Obunani tekshirish
    if not await check_user_subscription(context, user.id):
        keyboard = await get_subscription_keyboard()
        if keyboard:
            await update.message.reply_text(
                "🔒 Botdan foydalanish uchun quyidagi kanallarga obuna bo'ling:",
                reply_markup=keyboard
            )
            return
    
    welcome_text = """🎬 Video Downloader Bot ga xush kelibsiz!

📱 Quyidagi platformalardan video yuklay olasiz:
• TikTok
• YouTube (Video va Shorts)
• Facebook (Reels va Video)
• Instagram (Reels, Story, Post)

📝 Foydalanish:
Video linkini yuboring va kerakli sifatni tanlang!

👨‍💼 Admin: /admin - Admin panel
📊 Statistika: /stats"""
    
    await update.message.reply_text(welcome_text)

# Video linkini aniqlash (optimallashtirilgan)
@lru_cache(maxsize=50)
def detect_platform(url):
    patterns = {
        'tiktok': r'(?:tiktok.com|vm.tiktok.com)',
        'youtube': r'(?:youtube.com|youtu.be)',
        'facebook': r'(?:facebook.com|fb.watch)',
        'instagram': r'instagram.com'
    }
    
    for platform, pattern in patterns.items():
        if re.search(pattern, url, re.IGNORECASE):
            return platform
    return None

# Video sifat tugmalarini yaratish
def get_quality_keyboard(platform, url):
    keyboard = []
    
    if platform == 'youtube':
        keyboard = [
            [InlineKeyboardButton("🔥 720p", callback_data=f"dl_720_{url}")],
            [InlineKeyboardButton("📱 480p", callback_data=f"dl_480_{url}")],
            [InlineKeyboardButton("💾 360p", callback_data=f"dl_360_{url}")],
            [InlineKeyboardButton("🎵 Audio", callback_data=f"dl_audio_{url}")]
        ]
    else:
        keyboard = [
            [InlineKeyboardButton("🔥 Yuqori", callback_data=f"dl_high_{url}")],
            [InlineKeyboardButton("📱 O'rta", callback_data=f"dl_medium_{url}")],
            [InlineKeyboardButton("💾 Past", callback_data=f"dl_low_{url}")]
        ]
    
    return InlineKeyboardMarkup(keyboard)

# Optimallashtirilgan video yuklab olish
async def download_video(url, quality='best'):
    def _download():
        try:
            # Vaqtinchalik fayl nomi
            temp_dir = tempfile.mkdtemp()
            
            ydl_opts = {
                'outtmpl': f'{temp_dir}/%(title)s.%(ext)s',
                'writesubtitles': False,
                'writeautomaticsub': False,
                'noplaylist': True,
                'extract_flat': False,
                'ignoreerrors': False,
                'no_warnings': True,
                'quiet': True,
                'extractaudio': False,
                'audioformat': 'mp3',
                'embed_subs': False,
                'writeinfojson': False,
                'writethumbnail': False,
            }
            
            # Sifat sozlamalari (optimallashtirilgan)
            format_map = {
                '720': 'best[height<=720]/best[width<=1280]/best',
                '480': 'best[height<=480]/best[width<=854]/best',
                '360': 'best[height<=360]/best[width<=640]/best',
                'audio': 'bestaudio[ext=m4a]/bestaudio/best',
                'high': 'best[filesize<50M]/best',
                'medium': 'best[height<=720][filesize<30M]/best[height<=480]',
                'low': 'worst[height>=240]/worst'
            }
            
            ydl_opts['format'] = format_map.get(quality, 'best[filesize<100M]/best')
            
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                filename = ydl.prepare_filename(info)
                
                return {
                    'success': True,
                    'filename': filename,
                    'title': info.get('title', 'Unknown')[:50] + '...' if len(info.get('title', '')) > 50 else info.get('title', 'Unknown'),
                    'duration': info.get('duration', 0),
                    'temp_dir': temp_dir
                }
                
        except Exception as e:
            return {
                'success': False,
                'error': str(e)[:100] + '...' if len(str(e)) > 100 else str(e)
            }
    
    # Thread pool da yuklab olish
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(executor, _download)
    return result

# Xabar handler (optimallashtirilgan)
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message_text = update.message.text
    
    # Obunani tekshirish
    if not await check_user_subscription(context, user.id):
        keyboard = await get_subscription_keyboard()
        if keyboard:
            await update.message.reply_text(
                "🔒 Botdan foydalanish uchun quyidagi kanallarga obuna bo'ling:",
                reply_markup=keyboard
            )
            return
    
    # URL tekshirish
    if not (message_text.startswith('http://') or message_text.startswith('https://')):
        await update.message.reply_text(
            "❌ Iltimos, to'g'ri video linkini yuboring!\n\n"
            "📱 Qo'llab-quvvatlanadigan platformalar:\n"
            "• TikTok • YouTube • Facebook • Instagram"
        )
        return
    
    platform = detect_platform(message_text)
    
    if not platform:
        await update.message.reply_text(
            "❌ Noma'lum platforma! Qo'llab-quvvatlanadigan:\n"
            "• TikTok • YouTube • Facebook • Instagram"
        )
        return
    
    # Sifat tanlash tugmalari
    keyboard = get_quality_keyboard(platform, message_text)
    
    platform_names = {
        'tiktok': 'TikTok',
        'youtube': 'YouTube',
        'facebook': 'Facebook',
        'instagram': 'Instagram'
    }
    
    await update.message.reply_text(
        f"📱 {platform_names[platform]} video aniqlandi!\n🎬 Kerakli sifatni tanlang:",
        reply_markup=keyboard
    )

# Statistikani yangilash
async def update_download_stats():
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _update_download_stats_sync)

def _update_download_stats_sync():
    conn = sqlite3.connect(DATABASE_PATH)
    cursor = conn.cursor()
    today = datetime.now().date()
    cursor.execute('''
        INSERT OR REPLACE INTO daily_stats (date, new_users, active_users, downloads)
        VALUES (?,
                COALESCE((SELECT new_users FROM daily_stats WHERE date = ?), 0),
                COALESCE((SELECT active_users FROM daily_stats WHERE date = ?), 0),
                COALESCE((SELECT downloads FROM daily_stats WHERE date = ?), 0) + 1
        )
    ''', (today, today, today, today))
    conn.commit()
    conn.close()

# Callback query handler (optimallashtirilgan)
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data == "check_subscription":
        if await check_user_subscription(context, query.from_user.id):
            await query.edit_message_text("✅ Tabriklaymiz! Endi botdan foydalanishingiz mumkin.")
            
            # Start ni chaqirish uchun fake update yaratamiz
            fake_update = Update(
                update_id=0,
                message=query.message
            )
            await start(fake_update, context)
        else:
            await query.answer("❌ Siz hali barcha kanallarga obuna bo'lmadingiz!", show_alert=True)
        return
    
    if query.data.startswith("dl_"):
        parts = query.data.split("_", 2)
        quality = parts[1]
        url = parts[2]
        
        # Yuklanish jarayoni haqida xabar
        progress_message = await query.edit_message_text("⏳ Video yuklanmoqda... (3-5 soniya)")
        
        try:
            # Video yuklab olish
            result = await download_video(url, quality)
            
            if result['success']:
                # Fayl yuborish
                with open(result['filename'], 'rb') as video_file:
                    caption = f"🎬 {result['title']}\n📤 @{context.bot.username}"
                    
                    # Fayl hajmini tekshirish
                    file_size = os.path.getsize(result['filename'])
                    if file_size > 50 * 1024 * 1024:  # 50MB
                        await progress_message.edit_text("❌ Fayl hajmi juda katta (50MB dan ortiq)")
                    else:
                        await context.bot.send_video(
                            chat_id=query.message.chat_id,
                            video=video_file,
                            caption=caption,
                            supports_streaming=True
                        )
                        await progress_message.delete()
                
                # Vaqtinchalik fayllarni tozalash
                shutil.rmtree(result['temp_dir'], ignore_errors=True)
                
                # Statistikani yangilash
                await update_download_stats()
                
            else:
                await progress_message.edit_text(f"❌ Xatolik: {result['error']}")
                
        except Exception as e:
            await progress_message.edit_text(f"❌ Fayl yuborishda xatolik: {str(e)[:100]}")

# Admin panel
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("❌ Sizda admin huquqi yo'q!")
        return
    
    keyboard = [
        [InlineKeyboardButton("📊 Statistika", callback_data="admin_stats")],
        [InlineKeyboardButton("📢 Xabar yuborish", callback_data="admin_broadcast")],
        [InlineKeyboardButton("⚙️ Kanal sozlamalari", callback_data="admin_channels")],
        [InlineKeyboardButton("👥 Foydalanuvchilar", callback_data="admin_users")]
    ]
    
    await update.message.reply_text(
        "👨‍💼 Admin Panel",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# Admin callback handler
async def handle_admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    
    if user_id not in ADMIN_IDS:
        await query.answer("❌ Ruxsat yo'q!", show_alert=True)
        return
    
    await query.answer()
    
    if query.data == "admin_stats":
        loop = asyncio.get_event_loop()
        stats = await loop.run_in_executor(None, get_admin_stats)
        await query.edit_message_text(stats, parse_mode='Markdown')

def get_admin_stats():
    conn = sqlite3.connect(DATABASE_PATH)
    cursor = conn.cursor()
    
    # Bugungi statistika
    today = datetime.now().date()
    cursor.execute('SELECT * FROM daily_stats WHERE date = ?', (today,))
    today_stats = cursor.fetchone()
    
    # Jami foydalanuvchilar
    cursor.execute('SELECT COUNT(*) FROM users')
    total_users = cursor.fetchone()[0]
    
    # Oxirgi 7 kunlik statistika
    week_ago = today - timedelta(days=7)
    cursor.execute('SELECT SUM(new_users), SUM(downloads) FROM daily_stats WHERE date >= ?', (week_ago,))
    week_stats = cursor.fetchone()
    
    conn.close()
    
    if today_stats:
        return f"""📊 Bot Statistikasi

📅 Bugun ({today})
👥 Yangi: {today_stats[1]}
🔥 Faol: {today_stats[2]}
⬇️ Yuklab olingan: {today_stats[3]}

📈 Umumiy
👥 Jami: {total_users}

📊 Oxirgi 7 kun
👥 Yangi: {week_stats[0] or 0}
⬇️ Yuklab olingan: {week_stats[1] or 0}"""
    else:
        return f"📊 Bot Statistikasi\n\n👥 Jami foydalanuvchilar: {total_users}\n📅 Bugun hali faoliyat bo'lmagan"

# Xabar yuborish
async def broadcast_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    if user_id not in ADMIN_IDS:
        return
    
    if not context.args:
        await update.message.reply_text("Foydalanish: /broadcast <xabar matni>")
        return
    
    message_text = " ".join(context.args)
    
    # Foydalanuvchilar ro'yxatini olish
    loop = asyncio.get_event_loop()
    users = await loop.run_in_executor(None, get_active_users)
    
    sent = 0
    failed = 0
    
    progress_msg = await update.message.reply_text("📤 Xabar yuborilmoqda...")
    
    # Parallel ravishda yuborish
    tasks = []
    for user_id in users:
        task = send_broadcast_message(context, user_id, message_text)
        tasks.append(task)
    
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    for result in results:
        if isinstance(result, bool):
            if result:
                sent += 1
            else:
                failed += 1
        else:
            failed += 1
    
    await progress_msg.edit_text(
        f"✅ Xabar yuborish yakunlandi!\n📤 Yuborildi: {sent}\n❌ Yuborilmadi: {failed}"
    )

def get_active_users():
    conn = sqlite3.connect(DATABASE_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT user_id FROM users WHERE is_active = TRUE')
    users = [row[0] for row in cursor.fetchall()]
    conn.close()
    return users

async def send_broadcast_message(context, user_id, message_text):
    try:
        await context.bot.send_message(chat_id=user_id, text=message_text)
        return True
    except Exception:
        return False

# Kanal qo'shish
async def add_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    if user_id not in ADMIN_IDS:
        return
    
    if len(context.args) != 2:
        await update.message.reply_text("Foydalanish: /addchannel @kanal_username Kanal_nomi")
        return
    
    channel_id = context.args[0]
    channel_name = context.args[1]
    
    loop = asyncio.get_event_loop()
    success = await loop.run_in_executor(None, add_channel_to_db, channel_id, channel_name)
    
    if success:
        # Cache ni tozalash
        get_required_channels.cache_clear()
        await update.message.reply_text(f"✅ Kanal qo'shildi: {channel_name} ({channel_id})")
    else:
        await update.message.reply_text("❌ Bu kanal allaqachon qo'shilgan!")

def add_channel_to_db(channel_id, channel_name):
    conn = sqlite3.connect(DATABASE_PATH)
    cursor = conn.cursor()
    
    try:
        cursor.execute('INSERT INTO required_channels (channel_id, channel_name) VALUES (?, ?)',
                      (channel_id, channel_name))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()

# Webhook handler
async def webhook_handler(request):
    try:
        from aiohttp import web
        
        data = await request.json()
        update = Update.de_json(data, application.bot)
        await application.process_update(update)
        return web.Response(text="OK")
    except Exception as e:
        logger.error(f"Webhook xatosi: {e}")
        return web.Response(text="ERROR", status=500)

# Web server setup
async def setup_webhook():
    from aiohttp import web
    
    app = web.Application()
    app.router.add_post('/webhook', webhook_handler)
    
    return app

# Global application o'zgaruvchisi
application = None

# Asosiy dastur
async def main():
    global application
    
    # Ma'lumotlar bazasini yaratish
    init_database()
    
    # Bot yaratish - yangi usul
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Handlerlarni qo'shish
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("admin", admin_panel))
    application.add_handler(CommandHandler("broadcast", broadcast_message))
    application.add_handler(CommandHandler("addchannel", add_channel))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(CallbackQueryHandler(handle_admin_callback, pattern="^admin_"))
    
    if WEBHOOK_URL:
        # Webhook rejimi
        print("🌐 Webhook rejimida ishga tushmoqda...")
        
        # Bot ni ishga tushirish (yangilamalarsiz)
        await application.initialize()
        await application.start()
        
        # Webhook o'rnatish
        await application.bot.set_webhook(url=f"{WEBHOOK_URL}/webhook")
        
        # Web server
        from aiohttp import web
        app = await setup_webhook()
        
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '0.0.0.0', PORT)
        await site.start()
        
        print(f"🚀 Server {PORT} portda ishga tushdi")
        
        # Server ni ishlab turish
        try:
            while True:
                await asyncio.sleep(3600)
        except (KeyboardInterrupt, SystemExit):
            print("🛑 Server to'xtatilmoqda...")
        finally:
            await application.stop()
            await application.shutdown()
            await runner.cleanup()
    
    else:
        # Polling rejimi
        print("🤖 Polling rejimida ishga tushmoqda...")
        await application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    asyncio.run(main())