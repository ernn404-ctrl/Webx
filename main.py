import asyncio
import logging
import json
import random
import os
import uuid
import base64
import time
from datetime import datetime
import io
import cloudscraper
import aiohttp
from nacl.public import PublicKey, SealedBox
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes, ConversationHandler
)

# Logging
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# Env variables
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ALLOWED_USER_IDS = [int(x.strip()) for x in os.getenv("ALLOWED_USER_IDS", "").split(",") if x.strip().isdigit()]

SHORTIO_API_KEY = os.getenv("SHORTIO_API_KEY")
SHORTIO_DOMAIN = os.getenv("SHORTIO_DOMAIN")
LINK_CUSTOM_PREFIX = os.getenv("LINK_CUSTOM_PREFIX", "market")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")

IRAN_PROXY = "http://6f05828d954209c18b50__cr.ir:93cc122d6b59f8d8@gw.dataimpulse.com:823"
CLIENT_ID = os.getenv("CLIENT_ID", "snappfood_pwa")
CLIENT_SECRET = os.getenv("CLIENT_SECRET", "snappfood_pwa_secret")

SERVER_PUBLIC_KEY_B64 = os.getenv("SERVER_PUBLIC_KEY_B64", "eUhcujcdUs07+XAa6jPweavHMp26he6HCfowMUlaI08=")

# Sodium Key initialization
server_public_key = None
if SERVER_PUBLIC_KEY_B64:
    try:
        server_public_key_bytes = base64.b64decode(SERVER_PUBLIC_KEY_B64)
        server_public_key = PublicKey(server_public_key_bytes)
    except Exception as e:
        logger.error(f"Error loading public key: {e}")

# Redis connection
import redis.asyncio as redis_async
try:
    redis_client = redis_async.from_url(REDIS_URL, decode_responses=True)
except Exception as e:
    redis_client = None
    logger.error(f"Redis connection error: {e}")

# Constants
BASE_HEADERS = {
    'accept': 'application/json, text/plain, */*',
    'accept-language': 'fa',
    'content-type': 'application/json',
    'origin': 'https://snappfood.ir',
    'referer': 'https://snappfood.ir/',
    'user-agent': 'Mozilla/5.0 (Linux; Android 10)'
}

# Conversation States
ASK_PHONE = 1
ASK_CODE = 2
ASK_NEXT_ACTION = 3
ADMIN_ASK_DELETE = 4

# Global Lock for rebuild
rebuild_lock = asyncio.Lock()

def seal_data_with_sodium(data_dict: dict) -> str:
    if not server_public_key:
        return ""
    json_string = json.dumps(data_dict).encode('utf-8')
    sealed_box = SealedBox(server_public_key)
    return base64.b64encode(sealed_box.encrypt(json_string)).decode('utf-8')

async def send_verification_code(phone_number: str) -> dict:
    url = "https://user.snappfood.ir/v1/auth/otp/send"
    payload = {"mobile_number": phone_number, "type": "Customer"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=BASE_HEADERS, proxy=IRAN_PROXY, ssl=False, timeout=15) as response:
                if response.status == 200:
                    return await response.json()
                return {'status': False, 'error': f"HTTP {response.status}"}
    except Exception as e:
        return {'status': False, 'error': str(e)}

async def verify_code(phone_number: str, code: str) -> dict:
    url = "https://user.snappfood.ir/v1/auth/token"
    payload = {
        "cellphone": phone_number, "otpCode": int(code), "grantType": "Otp",
        "data": {
            "time": int(datetime.now().timestamp()), "device_uid": str(uuid.uuid4()),
            "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
            "scopes": ["mobile_v2", "mobile_v1", "webview"]
        }
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=BASE_HEADERS, proxy=IRAN_PROXY, ssl=False, timeout=15) as response:
                data = await response.json()
                if not (data.get('status') or data.get('success')) and 'accessToken' not in data.get('data', {}):
                    # لیست کامل‌تر برای جلوگیری از تشخیص بات
                    first_names = ["علی", "محمد", "یوسف", "امیر", "حسین", "رضا", "مهدی", "سارا", "زهرا", "مریم", "فاطمه", "امید", "نیما"]
                    last_names = ["راد", "تهرانی", "حسینی", "پارسا", "دانش", "آریا", "رضایی", "کریمی", "احمدی", "مجیدی", "محمدی"]
                    payload["firstName"] = random.choice(first_names)
                    payload["lastName"] = random.choice(last_names)
                    async with session.post(url, json=payload, headers=BASE_HEADERS, proxy=IRAN_PROXY, ssl=False, timeout=15) as res2:
                        return await res2.json()
                return data
    except Exception as e:
        return {'status': False, 'error': str(e)}

async def shorten_url_shortio(long_url: str, phone_number: str) -> str:
    if not SHORTIO_API_KEY or not SHORTIO_DOMAIN:
        return long_url
    api_url = "https://api.short.io/links"
    custom_path = f"{LINK_CUSTOM_PREFIX}-{phone_number}"
    payload = {"originalURL": long_url, "domain": SHORTIO_DOMAIN, "path": custom_path}
    headers = {"accept": "application/json", "content-type": "application/json", "Authorization": SHORTIO_API_KEY}
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(api_url, json=payload, headers=headers, timeout=10) as response:
                if response.status in [400, 409]:
                    payload["path"] = f"{custom_path}-{random.randint(100, 999)}"
                    async with session.post(api_url, json=payload, headers=headers, timeout=10) as res2:
                        res2.raise_for_status()
                        data = await res2.json()
                        return data.get("shortURL", long_url)
                response.raise_for_status()
                data = await response.json()
                return data.get("shortURL", long_url)
    except Exception as e:
        logger.error(f"Short.io Error: {e}")
        return long_url

def refresh_token_sync(current_refresh_token: str) -> dict:
    if not server_public_key:
        return {'status': False, 'error': 'Encryption key not loaded'}

    scraper = cloudscraper.create_scraper(browser={'browser': 'chrome', 'platform': 'android', 'desktop': False})
    
    with scraper as session:
        if IRAN_PROXY:
            session.proxies.update({"http": IRAN_PROXY, "https": IRAN_PROXY})

        session.headers.update({
            "x-is-bonyan": "true",
            "User-Agent": "Mozilla/5.0 (Linux; Android 10)", 
            "Origin": "https://m.snappfood.ir"
        })
        
        device_uid = str(uuid.uuid4())
        grant_body = {
            "time": int(time.time()),
            "device_uid": device_uid,
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "scopes": ["mobile_v2", "mobile_v1", "webview"],
            "grant_type": "refresh_token",
            "refresh_token": current_refresh_token
        }
        
        TOKEN_ENDPOINT_URL = "https://snappfood.ir/oauth2/default/token"
        
        try:
            sealed_payload = seal_data_with_sodium(grant_body)
            if not sealed_payload:
                return {'status': False, 'error': 'Encryption failed'}
            res = session.post(TOKEN_ENDPOINT_URL, json={"data": sealed_payload}, timeout=20)
            
            if res.status_code == 200:
                data = res.json().get("data", {})
                return {
                    'status': True,
                    'data': {
                        'accessToken': data.get("access_token") or data.get("accessToken"),
                        'refreshToken': data.get("refresh_token") or data.get("refreshToken") or current_refresh_token
                    }
                }
            return {'status': False, 'error': f"HTTP {res.status_code} - {res.text}"}
        except Exception as e:
            logger.error(f"Refresh Token Error: {e}")
            return {'status': False, 'error': str(e)}

async def cancel_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text("🚫 عملیات لغو شد.")
    else:
        await update.message.reply_text("🚫 عملیات لغو شد.")
    return ConversationHandler.END

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message.from_user.id not in ALLOWED_USER_IDS:
        return ConversationHandler.END
    context.user_data['session_links'] = context.user_data.get('session_links', [])
    keyboard = [[InlineKeyboardButton("❌ لغو عملیات", callback_data='cancel')]]
    await update.message.reply_text("📱 لطفاً شماره تلفن را وارد کنید (مثال: 09123456789):", reply_markup=InlineKeyboardMarkup(keyboard))
    return ASK_PHONE

async def ask_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    phone = update.message.text.strip()
    if not phone.isdigit() or len(phone) != 11 or not phone.startswith("09"):
        await update.message.reply_text("⚠️ شماره نامعتبر است. مجدداً وارد کنید:")
        return ASK_PHONE

    msg = await update.message.reply_text("⏳ در حال ارسال کد...")
    res = await send_verification_code(phone)
    
    if res.get('status') or res.get('success'):
        context.user_data['phone'] = phone
        keyboard = [
            [InlineKeyboardButton("🔄 ارسال مجدد کد", callback_data='resend_code'),
             InlineKeyboardButton("❌ لغو عملیات", callback_data='cancel')]
        ]
        await msg.edit_text(f"✅ کد تایید برای `{phone}` ارسال شد. لطفاً کد را وارد کنید:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
        return ASK_CODE
    else:
        keyboard = [[InlineKeyboardButton("❌ لغو عملیات", callback_data='cancel')]]
        await msg.edit_text(f"❌ خطا در ارسال کد:\n{res.get('error')}", reply_markup=InlineKeyboardMarkup(keyboard))
        return ASK_PHONE

async def resend_code_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer("در حال ارسال مجدد...")
    phone = context.user_data.get('phone')
    if not phone:
        await query.edit_message_text("⚠️ خطا: شماره یافت نشد.")
        return ConversationHandler.END
    
    res = await send_verification_code(phone)
    keyboard = [
        [InlineKeyboardButton("🔄 ارسال مجدد کد", callback_data='resend_code'),
         InlineKeyboardButton("❌ لغو عملیات", callback_data='cancel')]
    ]
    if res.get('status') or res.get('success'):
        await query.edit_message_text(f"✅ کد جدید برای `{phone}` ارسال شد. لطفا وارد کنید:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    else:
        await query.edit_message_text(f"❌ خطا در ارسال مجدد:\n{res.get('error')}", reply_markup=InlineKeyboardMarkup(keyboard))
    return ASK_CODE

async def ask_code(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    code = update.message.text.strip()
    phone = context.user_data.get('phone')
    if not code.isdigit():
        await update.message.reply_text("⚠️ کد باید فقط شامل اعداد باشد.")
        return ASK_CODE
        
    msg = await update.message.reply_text("⏳ در حال تایید کد و ساخت لینک...")
    res = await verify_code(phone, code)
    
    if (res.get('status') or res.get('success')) and 'accessToken' in res.get('data', {}):
        data_dict = res.get('data', {})
        access_token = data_dict.get('accessToken')
        refresh_token = data_dict.get('refreshToken')
        
        long_link = f"https://snapp.market/?source=jek_pwa-food&food_service_design=new&token={access_token}&sso_channel=food"
        final_link = await shorten_url_shortio(long_link, phone)
        
        if redis_client:
            redis_data = {
                "phone_number": phone, "access_token": access_token, 
                "refresh_token": refresh_token, "short_link": final_link,
                "created_at": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                "updated_at": datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }
            await redis_client.set(f"snappfood:token:{phone}", json.dumps(redis_data, ensure_ascii=False))

        context.user_data.setdefault('session_links', []).append(f"📱 `{phone}`\n🔗 {final_link}\n")

        keyboard = [
            [InlineKeyboardButton("➕ اضافه کردن شماره جدید", callback_data='next_line')],
            [InlineKeyboardButton("✅ پایان و دریافت لینک‌ها", callback_data='finish_session')]
        ]
        await msg.edit_text(
            f"🎉 لینک برای `{phone}` با موفقیت ساخته و ذخیره شد!\n\nانتخاب کنید:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )
        return ASK_NEXT_ACTION
    else:
        err_msg = res.get('error', 'کد نامعتبر است.')
        keyboard = [
            [InlineKeyboardButton("🔄 ارسال مجدد کد", callback_data='resend_code'),
             InlineKeyboardButton("❌ لغو عملیات", callback_data='cancel')]
        ]
        await msg.edit_text(f"⚠️ خطا: {err_msg}\n\nکد جدید را بفرستید یا لغو کنید:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
        return ASK_CODE

async def next_line_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    keyboard = [[InlineKeyboardButton("❌ لغو عملیات", callback_data='cancel')]]
    await query.edit_message_text("📱 لطفاً شماره تلفن جدید را وارد کنید:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    return ASK_PHONE

async def finish_session_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    links = context.user_data.get('session_links', [])
    if links:
        msg = "📦 **لیست تمامی لینک‌های شما در این نشست:**\n\n" + "\n".join(links)
        await query.edit_message_text(text=msg, parse_mode='Markdown', disable_web_page_preview=True)
    else:
        await query.edit_message_text("هیچ لینکی در این نشست ساخته نشد.")
    context.user_data.clear()
    return ConversationHandler.END

# --- Admin Panel ---
def get_admin_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 آمار دیتابیس", callback_data='admin_stats'),
         InlineKeyboardButton("🔄 بازسازی توکن‌ها", callback_data='admin_rebuild')],
        [InlineKeyboardButton("📥 دریافت بکاپ کامل", callback_data='admin_backup_full'),
         InlineKeyboardButton("🔗 استخراج لیست لینک‌ها", callback_data='admin_extract_links')],
        [InlineKeyboardButton("🗑 حذف اکانت", callback_data='admin_ask_delete')]
    ])

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id not in ALLOWED_USER_IDS:
        return
    await update.message.reply_text("⚙️ **پنل مدیریت پیشرفته ربات**\nلطفاً یک گزینه را انتخاب کنید:", reply_markup=get_admin_keyboard(), parse_mode='Markdown')

async def process_database_rebuild(chat_id: int, bot):
    if not redis_client:
        return
    
    async with rebuild_lock:
        keys = await redis_client.keys("snappfood:token:*")
        success_count, fail_count = 0, 0
        await bot.send_message(chat_id=chat_id, text=f"⏳ عملیات بازسازی برای `{len(keys)}` رکورد آغاز شد...", parse_mode='Markdown')
        
        for key in keys:
            try:
                raw_data = await redis_client.get(key)
                data = json.loads(raw_data)
                phone = data.get("phone_number")
                r_token = data.get("refresh_token")
                
                if not phone or not r_token:
                    fail_count += 1
                    continue
                    
                res = await asyncio.to_thread(refresh_token_sync, r_token)
                
                if (res.get('status') or res.get('success')) and res.get('data', {}).get('accessToken'):
                    new_access = res['data']['accessToken']
                    new_refresh = res['data']['refreshToken']
                    
                    long_link = f"https://snapp.market/?source=jek_pwa-food&food_service_design=new&token={new_access}&sso_channel=food"
                    new_short = await shorten_url_shortio(long_link, phone)
                    
                    data["access_token"] = new_access
                    data["refresh_token"] = new_refresh
                    data["short_link"] = new_short
                    data["updated_at"] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    
                    await redis_client.set(key, json.dumps(data, ensure_ascii=False))
                    success_count += 1
                else:
                    fail_count += 1
            except Exception:
                fail_count += 1
                
            await asyncio.sleep(2)
            
        msg = (
            f"✅ **عملیات بازسازی پایان یافت!**\n\n"
            f"📊 مجموع: `{len(keys)}`\n"
            f"🟢 موفق: `{success_count}`\n"
            f"🔴 ناموفق: `{fail_count}`\n"
        )
        await bot.send_message(chat_id=chat_id, text=msg, parse_mode='Markdown')

async def admin_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query.from_user.id not in ALLOWED_USER_IDS:
        return ConversationHandler.END
        
    await query.answer()
    if not redis_client:
        await query.message.reply_text("⚠️ دیتابیس ردیس متصل نیست!")
        return ConversationHandler.END

    if query.data == 'admin_stats':
        keys = await redis_client.keys("snappfood:token:*")
        await query.edit_message_text(f"📊 **آمار دیتابیس:**\nتعداد کل اکانت‌های فعال: `{len(keys)}`", parse_mode='Markdown', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data='admin_back')]]))
    
    elif query.data == 'admin_backup_full':
        await query.message.reply_text("⏳ درحال آماده‌سازی بکاپ کامل (JSON)...")
        keys = await redis_client.keys("snappfood:token:*")
        all_data = []
        for k in keys:
            all_data.append(json.loads(await redis_client.get(k)))
        doc = io.BytesIO(json.dumps(all_data, ensure_ascii=False, indent=4).encode('utf-8'))
        doc.name = f"Backup_Full_{datetime.now().strftime('%Y%m%d')}.json"
        await query.message.reply_document(doc, caption=f"📥 بکاپ کامل دیتابیس (JSON)")
    
    elif query.data == 'admin_extract_links':
        await query.message.reply_text("⏳ درحال استخراج لینک‌ها...")
        keys = await redis_client.keys("snappfood:token:*")
        content = ""
        for k in keys:
            data = json.loads(await redis_client.get(k))
            content += f"{data.get('phone_number', 'unknown')}: {data.get('short_link', 'بدون لینک')}\n"
        doc = io.BytesIO(content.encode('utf-8'))
        doc.name = f"Links_{datetime.now().strftime('%Y%m%d')}.txt"
        await query.message.reply_document(doc, caption=f"🔗 فایل لینک‌های استخراج شده")

    elif query.data == 'admin_rebuild':
        if rebuild_lock.locked():
            await query.message.reply_text("⚠️ عملیات در حال انجام است. لطفاً منتظر بمانید.")
        else:
            asyncio.create_task(process_database_rebuild(query.message.chat_id, context.bot))
            await query.message.reply_text("✅ درخواست بازسازی در پس‌زمینه شروع شد.")

    elif query.data == 'admin_ask_delete':
        await query.edit_message_text(
            "🗑 لطفاً شماره تلفن اکانتی که می‌خواهید حذف کنید را وارد کنید:",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو", callback_data='admin_cancel')]])
        )
        return ADMIN_ASK_DELETE

    elif query.data == 'admin_back' or query.data == 'admin_cancel':
        await query.edit_message_text("⚙️ **پنل مدیریت پیشرفته ربات**\nلطفاً یک گزینه را انتخاب کنید:", reply_markup=get_admin_keyboard(), parse_mode='Markdown')
        if query.data == 'admin_cancel':
            return ConversationHandler.END

    return ConversationHandler.END

async def admin_delete_number(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    phone = update.message.text.strip()
    if redis_client:
        deleted = await redis_client.delete(f"snappfood:token:{phone}")
        if deleted:
            await update.message.reply_text(f"✅ شماره `{phone}` با موفقیت حذف شد.\n\n⚙️ بازگشت به منو:", reply_markup=get_admin_keyboard(), parse_mode='Markdown')
        else:
            await update.message.reply_text("⚠️ این شماره در دیتابیس پیدا نشد.\n\n⚙️ بازگشت به منو:", reply_markup=get_admin_keyboard())
    return ConversationHandler.END

def main() -> None:
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            ASK_PHONE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, ask_phone),
                CallbackQueryHandler(cancel_action, pattern='^cancel$')
            ],
            ASK_CODE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, ask_code),
                CallbackQueryHandler(resend_code_callback, pattern='^resend_code$'),
                CallbackQueryHandler(cancel_action, pattern='^cancel$')
            ],
            ASK_NEXT_ACTION: [
                CallbackQueryHandler(next_line_callback, pattern='^next_line$'),
                CallbackQueryHandler(finish_session_callback, pattern='^finish_session$'),
                CallbackQueryHandler(cancel_action, pattern='^cancel$')
            ]
        },
        fallbacks=[CommandHandler("cancel", cancel_action)],
    )

    admin_conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("admin", admin_command),
            CallbackQueryHandler(admin_callbacks, pattern="^admin_")
        ],
        states={
            ADMIN_ASK_DELETE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_delete_number),
                CallbackQueryHandler(admin_callbacks, pattern='^admin_cancel$')
            ]
        },
        fallbacks=[]
    )
    
    application.add_handler(conv_handler)
    application.add_handler(admin_conv_handler)
    
    logger.info("Bot started...")
    application.run_polling()

if __name__ == "__main__":
    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN is not set.")
    else:
        main()
