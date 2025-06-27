import logging
import re
import requests
import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ConversationHandler,
    ContextTypes,
    CallbackQueryHandler,
    ApplicationBuilder
)

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", 
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Define states for conversation
MENU, GET_ROLL = range(2)
BOT_TOKEN = "7637993108:AAF0ms06uFbGFZ88J6WxCfosUlAH0UhWXBk"

# Create a thread pool for synchronous operations
executor = ThreadPoolExecutor(max_workers=20)

# Session storage
user_sessions = {}

# Database setup for storing user IDs
def init_db():
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

init_db()

def store_user(user_id, username, first_name, last_name):
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    cursor.execute('''
        INSERT OR IGNORE INTO users (user_id, username, first_name, last_name)
        VALUES (?, ?, ?, ?)
    ''', (user_id, username, first_name, last_name))
    conn.commit()
    conn.close()

def get_all_users():
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    cursor.execute('SELECT user_id FROM users')
    users = [row[0] for row in cursor.fetchall()]
    conn.close()
    return users

async def broadcast_startup_notification(app: Application):
    users = get_all_users()
    if not users:
        logger.info("No users to notify")
        return
    
    message = (
        "🚀 *Bot Restarted!*\n\n"
        "The National University Admission Result Bot has been updated/restarted. "
        "You can now check your results again using /start command."
    )
    
    success = 0
    failed = 0
    
    for user_id in users:
        try:
            await app.bot.send_message(
                chat_id=user_id,
                text=message,
                parse_mode="Markdown"
            )
            success += 1
            await asyncio.sleep(0.1)  # Rate limiting
        except Exception as e:
            logger.error(f"Failed to send notification to {user_id}: {e}")
            failed += 1
    
    logger.info(f"Startup notifications sent: {success} successful, {failed} failed")

def parse_result(raw_result, roll):
    data = {
        "application_id": "N/A",
        "roll": roll,
        "name": "N/A",
        "message": "No result information found or could not be parsed."
    }

    # Match after </font> tag for all fields
    app_id_match = re.search(r'Application ID\s*:</font>\s*([\d]+)', raw_result, re.IGNORECASE)
    roll_match = re.search(r'Admission Test Roll No\s*:</font>\s*([\d]+)', raw_result, re.IGNORECASE)
    name_match = re.search(r'Applicant Name\s*:</font>\s*([^<]+)', raw_result, re.IGNORECASE)
    result_match = re.search(r'Result\s*:</font>\s*(.*?)</div>', raw_result, re.IGNORECASE | re.DOTALL)

    if app_id_match:
        data['application_id'] = app_id_match.group(1).strip()
    if roll_match:
        data['roll'] = roll_match.group(1).strip()
    if name_match:
        data['name'] = name_match.group(1).strip()
    if result_match:
        message = result_match.group(1).strip()
        message = re.sub(r'\s+', ' ', message)
        data['message'] = message

    return data

async def fetch_result_with_session(roll, session):
    url = "http://app5.nu.edu.bd/nu-web/fetchAdmissionTestResultInformation"
    headers = {
        "accept": "text/plain, */*; q=0.01",
        "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
        "x-requested-with": "XMLHttpRequest",
        "referer": "http://app5.nu.edu.bd/nu-web/admissionTestResultQueryForm",
        "User-Agent": "Mozilla/5.0",
        "Cookie": f"JSESSIONID={session}" if session else ""
    }
    data = f"admissionRoll={roll}"

    async def attempt_request():
        for attempt in range(1, 20):
            try:
                logger.info(f"[Attempt {attempt}] Fetching result for: {roll}")
                response = await asyncio.to_thread(
                    requests.post, url, headers=headers, data=data, timeout=15
                )
                response.raise_for_status()
                
                if not session:
                    new_session = response.cookies.get('JSESSIONID')
                    if new_session:
                        return response.text, False, new_session
                
                if response.text.strip():
                    return response.text, False, session
            except Exception as e:
                logger.warning(f"[{roll}] Error on attempt {attempt}: {e}")
            await asyncio.sleep(2)
        return None, True, session

    return await attempt_request()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    username = update.effective_user.username
    first_name = update.effective_user.first_name
    last_name = update.effective_user.last_name
    
    # Store user in database
    store_user(user_id, username, first_name, last_name)
    
    if user_id in user_sessions:
        del user_sessions[user_id]
    
    keyboard = [
        [InlineKeyboardButton("🔍 Check Result", callback_data='check_result')],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_html(
        f"হাই {update.effective_user.mention_html()}! �\n"
        "জাতীয় বিশ্ববিদ্যালয়ের ভর্তি ফলাফল চেকার বটে আপনাকে স্বাগতম! \n\n"
        "নিচের মেনু থেকে একটি অপশন নির্বাচন করুন:",
        reply_markup=reply_markup
    )
    return MENU

async def menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data == 'check_result':
        await query.edit_message_text("📝 অনুগ্রহ করে আপনার রোল নম্বর বা আবেদন নম্বর পাঠান:")
        return GET_ROLL

async def get_roll_number(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    roll = update.message.text.strip()
    context.user_data['current_roll'] = roll
    logger.info(f"[{user_id}] Checking result for roll: {roll}")
    session = user_sessions.get(user_id)
    processing_task = asyncio.create_task(
        process_result_request(update, roll, session, user_id)
    )
    context.user_data['processing_task'] = processing_task
    
async def process_result_request(update: Update, roll: str, session: str, user_id: int):
    try:
        await update.message.reply_text("⚙️ অনুগ্রহ করে অপেক্ষা করুন, আপনার ফলাফল খোঁজা হচ্ছে...")

        raw_result, is_error, new_session = await fetch_result_with_session(roll, session)
        
        if new_session and new_session != session:
            user_sessions[user_id] = new_session

        if is_error or not raw_result:
            keyboard = [
                [InlineKeyboardButton("🔄 Check New Roll", callback_data='check_new_roll')],
                [InlineKeyboardButton("🏠 Live Chat Support", url="https://t.me/+Atvw7MKKJ3ZmODVl")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await update.message.reply_text(
                "No result found or server error.",
                reply_markup=reply_markup
            )
            return

        result_data = await asyncio.to_thread(parse_result, raw_result, roll)

        result_msg = (
            "🎉 *জাতীয় বিশ্ববিদ্যালয়ের ভর্তি ফলাফল* 🎉\n\n"
            f"📌 *Application ID:* `{result_data['application_id']}`\n"
            f"📌 *Roll No:* `{result_data['roll']}`\n"
            f"📌 *Name:* *{result_data['name']}*\n"
            f"📌 *Result:* *{result_data['message']}*"
        )
        
        keyboard = [
            [InlineKeyboardButton("🔄 Check New Roll", callback_data='check_new_roll')],
            [InlineKeyboardButton("🏠 Live Chat Support", url="https://t.me/+Atvw7MKKJ3ZmODVl")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            result_msg, 
            parse_mode="Markdown",
            reply_markup=reply_markup
        )
        
    except Exception as e:
        logger.error(f"Error processing request for roll {roll}: {e}")
        await update.message.reply_text("❌ ফলাফল প্রক্রিয়াকরণের সময় একটি ত্রুটি ঘটেছে।")

async def check_new_roll(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("📝 অনুগ্রহ করে আপনার নতুন রোল নম্বর বা আবেদন নম্বর পাঠান:")
    return GET_ROLL

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id in user_sessions:
        del user_sessions[user_id]
    if 'processing_task' in context.user_data:
        context.user_data['processing_task'].cancel()
    await update.message.reply_text("❎ অপারেশন বাতিল করা হয়েছে। শুরু করতে /start ব্যবহার করুন।")
    return ConversationHandler.END

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(msg="Exception while handling update:", exc_info=context.error)
    if update and hasattr(update, 'message'):
        await update.message.reply_text("❌ একটি অপ্রত্যাশিত ত্রুটি ঘটেছে। পরে আবার চেষ্টা করুন।")

def main():
    # Initialize the database
    init_db()
    
    # Build the application
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(broadcast_startup_notification).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            MENU: [CallbackQueryHandler(menu_handler)],
            GET_ROLL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, get_roll_number),
                CallbackQueryHandler(check_new_roll)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(conv_handler)
    app.add_error_handler(error_handler)

    print("Bot is running and ready to handle multiple users concurrently...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()