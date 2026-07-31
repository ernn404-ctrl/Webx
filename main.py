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
import asyncio
import aiohttp
from nacl.public import PublicKey, SealedBox
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes, ConversationHandler
)
import redis.asyncio as redis_async

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN")
ALLOWED_USER_IDS = [int(x.strip()) for x in os.getenv("ALLOWED_USER_IDS", "7701391471").split(",") if x.strip().isdigit()]

SHORTIO_API_KEY = os.getenv("SHORTIO_API_KEY", "YOUR_SHORTIO_API_KEY")
SHORTIO_DOMAIN = os.getenv("SHORTIO_DOMAIN", "baranlink.cyou")
LINK_CUSTOM_PREFIX = os.getenv("LINK_CUSTOM_PREFIX", "market")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")

IRAN_PROXY = os.getenv("IRAN_PROXY", "http://6f05828d954209c18b50__cr.ir:93cc122d6b59f8d8@gw.dataimpulse.com:823")
CLIENT_ID = os.getenv("CLIENT_ID", "snappfood_pwa")
CLIENT_SECRET = os.getenv("CLIENT_SECRET", "snappfood_pwa_secret")
SERVER_PUBLIC_KEY_B64 = os.getenv("SERVER_PUBLIC_KEY_B64", "eUhcujcdUs07+XAa6jPweavHMp26he6HCfowMUlaI08=")

server_public_key = None
if SERVER_PUBLIC_KEY_B64:
    try:
        server_public_key_bytes = base64.b64decode(SERVER_PUBLIC_KEY_B64)
        server_public_key = PublicKey(server_public_key_bytes)
    except Exception as e:
        logger.error(f"Error loading public key: {e}")

try:
    redis_client = redis_async.from_url(REDIS_URL, decode_responses=True)
except Exception as e:
    redis_client = None
    logger.error(f"Redis connection error: {e}")

BASE_HEADERS = {
    'accept': 'application/json, text/plain, */*',
    'accept-language': 'fa',
    'content-type': 'application/json',
    'origin': 'https://snappfood.ir',
    'referer': 'https://snappfood.ir/',
    'user-agent': 'Mozilla/5.0 (Linux; Android 10)'
}

ASK_PHONE = 1
ASK_CODE = 2
ASK_NEXT_ACTION = 3
ADMIN_ASK_DELETE = 4

rebuild_lock = asyncio.Lock()

def seal_data_with_sodium(data_dict: dict) -> str:
    if not server_public_key:
        return ""
    json_string = json.dumps(data_dict).encode('utf-8')
    sealed_box = SealedBox(server_public_key)
    return base64.b64encode(sealed_box.encrypt(json_string)).decode('utf-8')

def send_verification_code_sync(phone_number: str) -> dict:
    url = "https://user.snappfood.ir/v1/auth/otp/send"
    payload = {"mobile_number": phone_number, "type": "Customer"}
    scraper = cloudscraper.create_scraper(browser={'browser': 'chrome', 'platform': 'android', 'desktop': False})
    if IRAN_PROXY:
        scraper.proxies.update({"http": IRAN_PROXY, "https": IRAN_PROXY})
    
    try:
        res = scraper.post(url, json=payload, headers=BASE_HEADERS, timeout=15)
        raw_response = res.text
        if res.status_code == 200:
            return {'status': True, 'data': res.json(), 'raw': raw_response}
        return {'status': False, 'error': f"HTTP {res.status_code}", 'raw': raw_response}
    except Exception as e:
        return {'status': False, 'error': str(e), 'raw': str(e)}

def verify_code_sync(phone_number: str, code: str) -> dict:
    url = "https://user.snappfood.ir/v1/auth/token"
    payload = {
        "cellphone": phone_number, "otpCode": int(code), "grantType": "Otp",
        "data": {
            "time": int(datetime.now().timestamp()), "device_uid": str(uuid.uuid4()),
            "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
            "scopes": ["mobile_v2", "mobile_v1", "webview"]
        }
    }
    scraper = cloudscraper.create_scraper(browser={'browser': 'chrome', 'platform': 'android', 'desktop': False})
    if IRAN_PROXY:
        scraper.proxies.update({"http": IRAN_PROXY, "https": IRAN_PROXY})

    try:
        res = scraper.post(url, json=payload, headers=BASE_HEADERS, timeout=15)
        raw_response = res.text
        data = res.json() if res.status_code == 200 else {}
        
        if res.status_code != 200 or (not (data.get('status') or data.get('success')) and 'accessToken' not in data.get('data', {})):
            first_names = ["علی", "محمد", "یوسف", "امیر", "حسین", "رضا", "مهدی", "سارا", "زهرا", "مریم", "فاطمه", "امید", "نیما"]
            last_names = ["راد", "تهرانی", "حسینی", "پارسا", "دانش", "آریا", "رضایی", "کریمی", "احمدی", "مجیدی", "محمدی"]
            payload["firstName"] = random.choice(first_names)
            payload["lastName"] = random.choice(last_names)
            res2 = scraper.post(url, json=payload, headers=BASE_HEADERS, timeout=15)
            raw_response += "\n--Retry--\n" + res2.text
            data = res2.json() if res2.status_code == 200 else {}

        return {'status': res.status_code == 200 or res2.status_code == 200, 'data': data, 'raw': raw_response}
    except Exception as e:
        return {'status': False, 'error': str(e), 'raw': str(e)}

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
        return {'status': False, 'error': 'Encryption key not loaded', 'raw': 'Encryption key not loaded'}

    scraper = cloudscraper.create_scraper(browser={'browser': 'chrome', 'platform': 'android', 'desktop': False})
    if IRAN_PROXY:
        scraper.proxies.update({"http": IRAN_PROXY, "https": IRAN_PROXY})

    scraper.headers.update({
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
            return {'status': False, 'error': 'Encryption failed', 'raw': 'Encryption failed'}
        res = scraper.post(TOKEN_ENDPOINT_URL, json={"data": sealed_payload}, timeout=20)
        raw_response = res.text
        
        if res.status_code == 200:
            data = res.json().get("data", {})
            return {
                'status': True,
                'data': {
                    'accessToken': data.get("access_token") or data.get("accessToken"),
                    'refreshToken': data.get("refresh_token") or data.get("refreshToken") or current_refresh_token
                },
                'raw': raw_response
            }
        return {'status': False, 'error': f"HTTP {res.status_code}", 'raw': raw_response}
    except Exception as e:
        return {'status': False, 'error': str(e), 'raw': str(e)}

def append_log(context: ContextTypes.DEFAULT_TYPE, message: str):
    if 'api_logs' not in context.user_data:
        context.user_data['api_logs'] = []
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    context.user_data['api_logs'].append(f"[{timestamp}] {message}")

async def cancel_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text("عملیات لغو گردید.")
    else:
        await update.message.reply_text("عملیات لغو گردید.")
    return ConversationHandler.END

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message.from_user.id not in ALLOWED_USER_IDS:
        return ConversationHandler.END
    context.user_data['session_links'] = context.user_data.get('session_links', [])
    context.user_data['api_logs'] = []
    keyboard = [[InlineKeyboardButton("لغو عملیات", callback_data='cancel')]]
    await update.message.reply_text("شماره تلفن را وارد نمایید:", reply_markup=InlineKeyboardMarkup(keyboard))
    return ASK_PHONE

async def ask_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    phone = update.message.text.strip()
    if not phone.isdigit() or len(phone) != 11 or not phone.startswith("09"):
        await update.message.reply_text("فرمت شماره نامعتبر است. مجددا وارد نمایید:")
        return ASK_PHONE

    msg = await update.message.reply_text("در حال پردازش و ارسال درخواست...")
    res = await asyncio.to_thread(send_verification_code_sync, phone)
    append_log(context, f"OTP Send Response for {phone}:\n{res.get('raw', '')}")
    
    if res.get('status') and res.get('data', {}).get('status'):
        context.user_data['phone'] = phone
        keyboard = [
            [InlineKeyboardButton("ارسال مجدد کد", callback_data='resend_code'),
             InlineKeyboardButton("لغو عملیات", callback_data='cancel')]
        ]
        await msg.edit_text(f"کد تایید به شماره {phone} ارسال شد. کد دریافتی را وارد نمایید:", reply_markup=InlineKeyboardMarkup(keyboard))
        return ASK_CODE
    else:
        keyboard = [[InlineKeyboardButton("لغو عملیات", callback_data='cancel')]]
        await msg.edit_text(f"خطا در برقراری ارتباط:\n{res.get('error')}", reply_markup=InlineKeyboardMarkup(keyboard))
        return ASK_PHONE

async def resend_code_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer("در حال ارسال درخواست مجدد...")
    phone = context.user_data.get('phone')
    if not phone:
        await query.edit_message_text("شماره یافت نشد.")
        return ConversationHandler.END
    
    res = await asyncio.to_thread(send_verification_code_sync, phone)
    append_log(context, f"OTP Resend Response for {phone}:\n{res.get('raw', '')}")
    
    keyboard = [
        [InlineKeyboardButton("ارسال مجدد کد", callback_data='resend_code'),
         InlineKeyboardButton("لغو عملیات", callback_data='cancel')]
    ]
    if res.get('status') and res.get('data', {}).get('status'):
        await query.edit_message_text(f"کد جدید به شماره {phone} ارسال شد. کد را وارد نمایید:", reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await query.edit_message_text(f"خطا در ارسال مجدد:\n{res.get('error')}", reply_markup=InlineKeyboardMarkup(keyboard))
    return ASK_CODE

async def ask_code(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    code = update.message.text.strip()
    phone = context.user_data.get('phone')
    if not code.isdigit():
        await update.message.reply_text("فرمت کد نامعتبر است.")
        return ASK_CODE
        
    msg = await update.message.reply_text("در حال تایید و ثبت اطلاعات...")
    res = await asyncio.to_thread(verify_code_sync, phone, code)
    append_log(context, f"Verify Code Response for {phone}:\n{res.get('raw', '')}")
    
    data_dict = res.get('data', {})
    if res.get('status') and 'accessToken' in data_dict.get('data', {}):
        access_token = data_dict['data']['accessToken']
        refresh_token = data_dict['data']['refreshToken']
        
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

        context.user_data.setdefault('session_links', []).append(f"{phone}\n{final_link}\n")

        keyboard = [
            [InlineKeyboardButton("افزودن رکورد جدید", callback_data='next_line')],
            [InlineKeyboardButton("پایان عملیات و دریافت گزارش", callback_data='finish_session')]
        ]
        await msg.edit_text(
            f"عملیات با موفقیت انجام شد.\nلینک مرتبط: {final_link}\nاقدام بعدی را انتخاب نمایید:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            disable_web_page_preview=True
        )
        return ASK_NEXT_ACTION
    else:
        keyboard = [
            [InlineKeyboardButton("ارسال مجدد کد", callback_data='resend_code'),
             InlineKeyboardButton("لغو عملیات", callback_data='cancel')]
        ]
        await msg.edit_text(f"خطا در اعتبارسنجی:\n{res.get('error', 'دسترسی غیرمجاز')}", reply_markup=InlineKeyboardMarkup(keyboard))
        return ASK_CODE

async def next_line_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    keyboard = [[InlineKeyboardButton("لغو عملیات", callback_data='cancel')]]
    await query.edit_message_text("شماره تلفن جدید را وارد نمایید:", reply_markup=InlineKeyboardMarkup(keyboard))
    return ASK_PHONE

async def finish_session_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    links = context.user_data.get('session_links', [])
    if links:
        msg = "گزارش لینک‌های ایجاد شده:\n\n" + "\n".join(links)
        await query.edit_message_text(text=msg, disable_web_page_preview=True)
    else:
        await query.edit_message_text("هیچ رکوردی در این نشست ثبت نگردید.")
    
    logs = context.user_data.get('api_logs', [])
    if logs:
        log_content = "\n\n".join(logs)
        doc = io.BytesIO(log_content.encode('utf-8'))
        doc.name = f"Session_Logs_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        await context.bot.send_document(chat_id=query.message.chat_id, document=doc, caption="گزارش پاسخ‌های خام سرور (API Logs)")

    context.user_data.clear()
    return ConversationHandler.END

def get_admin_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("آمار پایگاه داده", callback_data='admin_stats'),
         InlineKeyboardButton("بازسازی توکن‌ها", callback_data='admin_rebuild')],
        [InlineKeyboardButton("دریافت بکاپ کامل", callback_data='admin_backup_json'),
         InlineKeyboardButton("استخراج آدرس‌ها", callback_data='admin_links_txt')],
        [InlineKeyboardButton("حذف رکورد", callback_data='admin_delete_prompt')]
    ])

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id not in ALLOWED_USER_IDS:
        return
    await update.message.reply_text("پنل مدیریت سیستم\nلطفا عملیات مورد نظر را انتخاب نمایید:", reply_markup=get_admin_keyboard())

async def process_database_rebuild(chat_id: int, bot):
    if not redis_client:
        return
    
    async with rebuild_lock:
        keys = await redis_client.keys("snappfood:token:*")
        success_count, fail_count = 0, 0
        log_content = ""
        await bot.send_message(chat_id=chat_id, text=f"عملیات بازسازی برای {len(keys)} رکورد آغاز گردید.")
        
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
                log_content += f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Refresh Token Response for {phone}:\n{res.get('raw', '')}\n\n"
                
                if res.get('status') and res.get('data', {}).get('accessToken'):
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
            except Exception as e:
                fail_count += 1
                log_content += f"Exception for {key}: {str(e)}\n\n"
                
            await asyncio.sleep(2)
            
        msg = (
            f"عملیات بازسازی خاتمه یافت.\n"
            f"مجموع رکوردها: {len(keys)}\n"
            f"بروزرسانی موفق: {success_count}\n"
            f"عملیات ناموفق: {fail_count}"
        )
        await bot.send_message(chat_id=chat_id, text=msg)
        
        if log_content:
            doc = io.BytesIO(log_content.encode('utf-8'))
            doc.name = f"Rebuild_Logs_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
            await bot.send_document(chat_id=chat_id, document=doc, caption="گزارش خام عملیات بازسازی")

async def admin_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query.from_user.id not in ALLOWED_USER_IDS:
        return ConversationHandler.END
        
    await query.answer()
    if not redis_client:
        await query.message.reply_text("ارتباط با پایگاه داده برقرار نیست.")
        return ConversationHandler.END

    if query.data == 'admin_stats':
        keys = await redis_client.keys("snappfood:token:*")
        await query.edit_message_text(f"آمار پایگاه داده:\nمجموع رکوردهای فعال: {len(keys)}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("بازگشت", callback_data='admin_back')]]))
    
    elif query.data == 'admin_backup_json':
        await query.message.reply_text("در حال آماده‌سازی فایل پشتیبان...")
        keys = await redis_client.keys("snappfood:token:*")
        all_data = []
        for k in keys:
            all_data.append(json.loads(await redis_client.get(k)))
        doc = io.BytesIO(json.dumps(all_data, ensure_ascii=False, indent=4).encode('utf-8'))
        doc.name = f"Backup_Full_{datetime.now().strftime('%Y%m%d')}.json"
        await query.message.reply_document(doc, caption="فایل پشتیبان پایگاه داده")
    
    elif query.data == 'admin_extract_links':
        await query.message.reply_text("در حال استخراج اطلاعات...")
        keys = await redis_client.keys("snappfood:token:*")
        content = ""
        for k in keys:
            data = json.loads(await redis_client.get(k))
            content += f"{data.get('phone_number', 'unknown')}: {data.get('short_link', 'بدون مقدار')}\n"
        doc = io.BytesIO(content.encode('utf-8'))
        doc.name = f"Extracted_Links_{datetime.now().strftime('%Y%m%d')}.txt"
        await query.message.reply_document(doc, caption="گزارش آدرس‌های استخراج شده")

    elif query.data == 'admin_rebuild':
        if rebuild_lock.locked():
            await query.message.reply_text("سیستم در حال پردازش درخواست دیگری است.")
        else:
            asyncio.create_task(process_database_rebuild(query.message.chat_id, context.bot))
            await query.message.reply_text("درخواست بازسازی در پس‌زمینه ثبت گردید.")

    elif query.data == 'admin_ask_delete':
        await query.edit_message_text(
            "جهت حذف رکورد، شماره تلفن مربوطه را وارد نمایید:",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("لغو", callback_data='admin_cancel')]])
        )
        return ADMIN_ASK_DELETE

    elif query.data == 'admin_back' or query.data == 'admin_cancel':
        await query.edit_message_text("پنل مدیریت سیستم\nلطفا عملیات مورد نظر را انتخاب نمایید:", reply_markup=get_admin_keyboard())
        if query.data == 'admin_cancel':
            return ConversationHandler.END

    return ConversationHandler.END

async def admin_delete_number(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    phone = update.message.text.strip()
    if redis_client:
        deleted = await redis_client.delete(f"snappfood:token:{phone}")
        if deleted:
            await update.message.reply_text(f"رکورد مرتبط با {phone} حذف گردید.\n\nبازگشت به منوی اصلی:", reply_markup=get_admin_keyboard())
        else:
            await update.message.reply_text("رکوردی با مشخصات ارائه شده یافت نشد.\n\nبازگشت به منوی اصلی:", reply_markup=get_admin_keyboard())
    return ConversationHandler.END

def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        logger.error("Telegram Bot Token is not configured.")
        return

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
    
    logger.info("Service initialized.")
    application.run_polling()

if __name__ == "__main__":
    main()
