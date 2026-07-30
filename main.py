# -*- coding: utf-8 -*-
import logging
import json
import requests
import random
import os
import uuid
import asyncio
import redis
import io
import time
import base64
import cloudscraper
import urllib3
from datetime import datetime
from typing import Optional
from nacl.public import PublicKey, SealedBox
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardRemove
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
    ConversationHandler,
)

# --- غیرفعال‌سازی هشدارهای امنیتی SSL ---
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- لاگ‌ها ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- متغیرهای محیطی ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
allowed_users_env = os.getenv("ALLOWED_USER_IDS", "")
ALLOWED_USER_IDS = [int(x.strip()) for x in allowed_users_env.split(",") if x.strip().isdigit()]

SHORTIO_API_KEY_EXPRESS = os.getenv("SHORTIO_API_KEY_EXPRESS")
SHORTIO_DOMAIN_EXPRESS = os.getenv("SHORTIO_DOMAIN_EXPRESS")
LINK_CUSTOM_PREFIX = os.getenv("LINK_CUSTOM_PREFIX", "market")
REDIS_URL = os.getenv("REDIS_URL")

IRAN_PROXY = os.getenv("IRAN_PROXY", "http://6f05828d954209c18b50__cr.ir:93cc122d6b59f8d8@gw.dataimpulse.com:823")
SNAPPFOOD_PROXIES = {
    "http": IRAN_PROXY,
    "https": IRAN_PROXY
}

# --- اتصال به ردیس ---
try:
    if REDIS_URL:
        redis_client = redis.from_url(REDIS_URL, decode_responses=True)
        redis_client.ping()
        logger.info("✅ اتصال به ردیس موفق بود.")
    else:
        redis_client = None
except Exception as e:
    redis_client = None
    logger.error(f"❌ خطا در ردیس: {e}")

BASE_HEADERS = {
    'accept': 'application/json, text/plain, */*',
    'accept-language': 'fa',
    'content-type': 'application/json',
    'origin': 'https://snappfood.ir',
    'referer': 'https://snappfood.ir/',
    'user-agent': 'Mozilla/5.0 (Linux; Android 10)'
}

# ================= سیستم رمزنگاری و بای‌پَس پیشرفته اسنپ‌فود =================
SERVER_PUBLIC_KEY_B64 = "eUhcujcdUs07+XAa6jPweavHMp26he6HCfowMUlaI08="
try:
    server_public_key_bytes = base64.b64decode(SERVER_PUBLIC_KEY_B64)
    server_public_key = PublicKey(server_public_key_bytes)
except Exception as e:
    logger.error(f"🔴 خطای رمزنگاری کلید عمومی: {e}")
    server_public_key = None

def seal_data_with_sodium(data_dict: dict) -> str:
    """رمزنگاری بدنه درخواست دقیقاً مشابه اپلیکیشن موبایل اسنپ‌فود"""
    json_string = json.dumps(data_dict).encode('utf-8')
    sealed_box = SealedBox(server_public_key)
    return base64.b64encode(sealed_box.encrypt(json_string)).decode('utf-8')

def refresh_snappfood_token_advanced(current_refresh_token: str) -> dict:
    """دریافت توکن جدید با استفاده از متد رمزنگاری و CloudScraper (بدون تداخل SSL)"""
    if not server_public_key:
        return {'status': False, 'error': 'کلید رمزنگاری بارگذاری نشد'}

    scraper = cloudscraper.create_scraper(browser={'browser': 'chrome', 'platform': 'android', 'desktop': False})
    
    with scraper as session:
        # اعمال پروکسی در صورت وجود
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
            "client_id": "snappfood_pwa",
            "client_secret": "snappfood_pwa_secret",
            "scopes": ["mobile_v2", "mobile_v1", "webview"],
            "grant_type": "refresh_token",
            "refresh_token": current_refresh_token
        }
        
        TOKEN_ENDPOINT_URL = "https://snappfood.ir/oauth2/default/token"
        
        try:
            sealed_payload = seal_data_with_sodium(grant_body)
            # کلوداسکرپر حالا به درستی و بدون ارور گواهینامه ریکوئست را ارسال می‌کند
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
            logger.error(f"خطای پیشرفته در ریفرش توکن: {e}")
            return {'status': False, 'error': str(e)}
# ==============================================================================

# --- توابع API معمولی (برای ورود اولیه) ---
def send_verification_code(phone_number: str) -> dict:
    url = "https://user.snappfood.ir/v1/auth/otp/send"
    payload = {"mobile_number": phone_number, "type": "Customer"}
    try:
        # اضافه شدن verify=False برای جلوگیری از خطای پروکسی
        response = requests.post(url, json=payload, headers=BASE_HEADERS, proxies=SNAPPFOOD_PROXIES, verify=False, timeout=15)
        if response.status_code == 200: return response.json()
        return {'status': False, 'error': f"HTTP {response.status_code}"}
    except Exception as e:
        return {'status': False, 'error': str(e)}

def verify_code(phone_number: str, code: str) -> dict:
    url = "https://user.snappfood.ir/v1/auth/token"
    payload = {
        "cellphone": phone_number, "otpCode": int(code), "grantType": "Otp",
        "data": {
            "time": int(datetime.now().timestamp()), "device_uid": str(uuid.uuid4()),
            "client_id": "snappfood_pwa", "client_secret": "snappfood_pwa_secret",
            "scopes": ["mobile_v2", "mobile_v1", "webview"]
        }
    }
    try:
        # اضافه شدن verify=False
        response = requests.post(url, json=payload, headers=BASE_HEADERS, proxies=SNAPPFOOD_PROXIES, verify=False, timeout=15)
        data = response.json()
        if not (data.get('status') or data.get('success')) and 'accessToken' not in data.get('data', {}):
            first_names = ["علی", "محمد", "یوسف", "امیر", "حسین", "رضا", "مهدی"]
            last_names = ["راد", "تهرانی", "حسینی", "پارسا", "دانش", "آریا"]
            payload["firstName"] = random.choice(first_names)
            payload["lastName"] = random.choice(last_names)
            response = requests.post(url, json=payload, headers=BASE_HEADERS, proxies=SNAPPFOOD_PROXIES, verify=False, timeout=15)
            data = response.json()
        return data
    except Exception as e:
        return {'status': False, 'error': str(e)}

async def shorten_url_shortio(long_url: str, domain: str, api_key: str, phone_number: str) -> Optional[str]:
    if not api_key or not domain: return None
    api_url = "https://api.short.io/links"
    custom_path = f"{LINK_CUSTOM_PREFIX}-{phone_number}"
    payload = {"originalURL": long_url, "domain": domain, "path": custom_path}
    headers = {"accept": "application/json", "content-type": "application/json", "Authorization": api_key}
    try:
        # نیازی به پروکسی و verify=False برای shortio نیست
        response = await asyncio.to_thread(requests.post, api_url, json=payload, headers=headers, timeout=10)
        if response.status_code in [400, 409]:
            payload["path"] = f"{custom_path}-{random.randint(10, 999)}"
            response = await asyncio.to_thread(requests.post, api_url, json=payload, headers=headers, timeout=10)
        response.raise_for_status()
        return response.json().get("shortURL")
    except Exception:
        return None

# --- تابع لغو ---
async def cancel_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text("🚫 عملیات لغو شد.")
    elif update.message:
        await update.message.reply_text("🚫 عملیات لغو شد.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

# --- وضعیت‌های مکالمه ---
ASK_PHONE, ASK_CODE, ASK_NEXT_ACTION = range(3)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message.from_user.id not in ALLOWED_USER_IDS: return ConversationHandler.END
    context.user_data['session_links'] = []
    
    keyboard = [[InlineKeyboardButton("❌ لغو عملیات", callback_data='cancel')]]
    await update.message.reply_text(
        "👋 **سلام!**\n\n📱 لطفاً شماره تلفن را وارد کنید (مثال: `09123456789`)", 
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )
    return ASK_PHONE

async def ask_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    phone_number = update.message.text
    if not (phone_number.startswith("09") and len(phone_number) == 11 and phone_number.isdigit()):
        keyboard = [[InlineKeyboardButton("❌ لغو عملیات", callback_data='cancel')]]
        await update.message.reply_text("⚠️ فرمت شماره نامعتبر است. مجدداً ارسال کنید.", reply_markup=InlineKeyboardMarkup(keyboard))
        return ASK_PHONE
        
    context.user_data['phone_number'] = phone_number
    await update.message.reply_text(f"⏳ درحال ارسال کد تأیید به `{phone_number}`...", parse_mode='Markdown')
    
    res = await asyncio.to_thread(send_verification_code, phone_number)
    
    if res.get('status') or res.get('success'):
        keyboard = [
            [InlineKeyboardButton("🔄 ارسال مجدد کد", callback_data='resend_code'),
             InlineKeyboardButton("❌ لغو عملیات", callback_data='cancel')]
        ]
        await update.message.reply_text(
            "✅ **کد تأیید ارسال شد.**\n\n💬 لطفاً کد دریافتی را در همینجا تایپ کنید:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )
        return ASK_CODE
    else:
        err_msg = res.get('error', 'ارتباط با سرور برقرار نشد.')
        await update.message.reply_text(f"❌ خطا در ارسال کد:\n`{err_msg}`\n\nلطفاً مجدداً با /start تلاش کنید.", parse_mode='Markdown')
        return ConversationHandler.END

async def resend_code_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    phone = context.user_data.get('phone_number')
    await query.answer("درحال ارسال مجدد...")
    await asyncio.to_thread(send_verification_code, phone)
    await query.edit_message_text(
        "🔄 **کد مجدداً ارسال شد.**\n\n💬 لطفاً کد جدید را تایپ کنید:",
        reply_markup=query.message.reply_markup,
        parse_mode='Markdown'
    )
    return ASK_CODE

async def ask_code(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    code = update.message.text
    phone_number = context.user_data.get('phone_number')
    await update.message.reply_text("⏳ درحال اعتبارسنجی و ایجاد لینک...")
    
    res = await asyncio.to_thread(verify_code, phone_number, code)
    
    data_dict = res.get('data', {})
    access_token = data_dict.get('accessToken')
    refresh_token = data_dict.get('refreshToken')

    if (res.get('status') or res.get('success')) and access_token:
        snapp_market_link = f"https://snapp.market/?source=jek_pwa-food&food_service_design=new&token={access_token}&sso_channel=food"
        short_link = await shorten_url_shortio(snapp_market_link, SHORTIO_DOMAIN_EXPRESS, SHORTIO_API_KEY_EXPRESS, phone_number)
        final_link = short_link if short_link else snapp_market_link

        if redis_client:
            redis_data = {
                "phone_number": phone_number, "access_token": access_token, 
                "refresh_token": refresh_token, "short_link": final_link,
                "created_at": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                "updated_at": datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }
            redis_client.set(f"snappfood:token:{phone_number}", json.dumps(redis_data, ensure_ascii=False))

        context.user_data['session_links'].append(f"📱 `{phone_number}`\n🔗 {final_link}\n")

        keyboard = [
            [InlineKeyboardButton("➕ ثبت خط بعدی", callback_data='next_line')],
            [InlineKeyboardButton("✅ پایان و دریافت لینک‌ها", callback_data='finish_session')]
        ]
        await update.message.reply_text(
            f"🎉 لینک برای `{phone_number}` ساخته شد!\n\nانتخاب کنید:",
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
        await update.message.reply_text(
            f"⚠️ **خطا:** {err_msg}\n\nکد جدید را بفرستید یا عملیات را لغو کنید:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )
        return ASK_CODE

async def next_line_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_reply_markup(reply_markup=None)
    
    keyboard = [[InlineKeyboardButton("❌ لغو عملیات", callback_data='cancel')]]
    await context.bot.send_message(
        chat_id=query.message.chat_id, 
        text="📱 **لطفاً شماره تلفن جدید را وارد کنید:**",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )
    return ASK_PHONE

async def finish_session_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_reply_markup(reply_markup=None)
    links = context.user_data.get('session_links', [])
    if links:
        msg = "📦 **لیست تمامی لینک‌های شما:**\n\n" + "\n".join(links)
        await context.bot.send_message(chat_id=query.message.chat_id, text=msg, parse_mode='Markdown', disable_web_page_preview=True)
    context.user_data.clear()
    return ConversationHandler.END


# --- پنل مدیریت ادمین ---
async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id not in ALLOWED_USER_IDS: return
    
    keyboard = [
        [InlineKeyboardButton("📊 آمار دیتابیس", callback_data='admin_stats'),
         InlineKeyboardButton("🔄 بازسازی لینک‌ها (پیشرفته)", callback_data='admin_rebuild')],
        [InlineKeyboardButton("📥 استخراج فایل بکاپ", callback_data='admin_extract')]
    ]
    await update.message.reply_text(
        "⚙️ **پنل مدیریت پیشرفته ربات**\n\nلطفاً یک گزینه را انتخاب کنید:", 
        reply_markup=InlineKeyboardMarkup(keyboard), 
        parse_mode='Markdown'
    )

async def delete_number(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id not in ALLOWED_USER_IDS: return
    if not context.args:
        await update.message.reply_text("⚠️ نحوه استفاده:\n`/delete 09123456789`", parse_mode='Markdown')
        return
    phone = context.args[0]
    if redis_client and redis_client.delete(f"snappfood:token:{phone}"):
        await update.message.reply_text(f"✅ شماره `{phone}` با موفقیت از دیتابیس پاک شد.", parse_mode='Markdown')
    else:
        await update.message.reply_text("⚠️ این شماره در دیتابیس پیدا نشد.")

async def process_database_rebuild(chat_id: int, bot):
    """فرآیند بازسازی با استفاده از سیستم پیشرفته رمزنگاری در پس‌زمینه"""
    if not redis_client: return
    keys = redis_client.keys("snappfood:token:*")
    success_count, fail_count = 0, 0
    await bot.send_message(chat_id=chat_id, text=f"⏳ عملیات بازسازی ایمن برای `{len(keys)}` رکورد آغاز شد...\n(درحال دور زدن کلودفلر و ایجاد توکن‌های جدید)", parse_mode='Markdown')
    
    for key in keys:
        try:
            data = json.loads(redis_client.get(key))
            phone = data.get("phone_number")
            r_token = data.get("refresh_token")
            
            if not phone or not r_token:
                fail_count += 1
                continue
                
            # --- فراخوانی تابع فوق‌پیشرفته جدید ---
            res = await asyncio.to_thread(refresh_snappfood_token_advanced, r_token)
            
            new_data_dict = res.get('data', {})
            new_access = new_data_dict.get('accessToken')
            new_refresh = new_data_dict.get('refreshToken')
            
            if (res.get('status') or res.get('success')) and new_access:
                long_link = f"https://snapp.market/?source=jek_pwa-food&food_service_design=new&token={new_access}&sso_channel=food"
                new_short = await shorten_url_shortio(long_link, SHORTIO_DOMAIN_EXPRESS, SHORTIO_API_KEY_EXPRESS, phone)
                
                data["access_token"] = new_access
                data["refresh_token"] = new_refresh
                data["short_link"] = new_short or long_link
                data["updated_at"] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                
                redis_client.set(key, json.dumps(data, ensure_ascii=False))
                success_count += 1
            else:
                fail_count += 1
        except Exception:
            fail_count += 1
            
        # وقفه حیاتی برای شبیه‌سازی رفتار انسان و جلوگیری از مسدودی
        await asyncio.sleep(2)
        
    msg = (
        f"✅ **عملیات بازسازی پیشرفته پایان یافت!**\n\n"
        f"📊 مجموع اکانت‌ها: `{len(keys)}`\n"
        f"🟢 موفق و بروزشده: `{success_count}`\n"
        f"🔴 ناموفق (منقضی یا مسدود): `{fail_count}`\n\n"
        f"💡 حالا می‌توانید یک بکاپ جدید بگیرید."
    )
    await bot.send_message(chat_id=chat_id, text=msg, parse_mode='Markdown')

async def admin_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not redis_client:
        await query.message.reply_text("⚠️ دیتابیس ردیس متصل نیست!")
        return

    if query.data == 'admin_stats':
        keys = redis_client.keys("snappfood:token:*")
        await query.edit_message_text(
            f"📊 **آمار دیتابیس:**\nتعداد کل اکانت‌های فعال: `{len(keys)}`\n\n"
            f"🗑 *برای حذف یک خط:* `/delete 0912...`", 
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data='admin_back')]])
        )
    elif query.data == 'admin_extract':
        await query.message.reply_text("⏳ درحال آماده‌سازی فایل بکاپ...")
        keys = redis_client.keys("snappfood:token:*")
        content = "لیست استخراج شده ربات\n---------------------------\n"
        for k in keys:
            data = json.loads(redis_client.get(k))
            content += f"شماره: {data.get('phone_number')}\nلینک: {data.get('short_link', 'بدون لینک')}\nآخرین بروزرسانی: {data.get('updated_at', 'نامشخص')}\n\n"
        doc = io.BytesIO(content.encode('utf-8'))
        doc.name = f"DB_Export_{datetime.now().strftime('%Y%m%d')}.txt"
        await query.message.reply_document(doc, caption=f"📥 فایل بکاپ دیتابیس\nتعداد رکوردها: {len(keys)}")
    elif query.data == 'admin_rebuild':
        asyncio.create_task(process_database_rebuild(query.message.chat_id, context.bot))
    elif query.data == 'admin_back':
        keyboard = [
            [InlineKeyboardButton("📊 آمار دیتابیس", callback_data='admin_stats'),
             InlineKeyboardButton("🔄 بازسازی لینک‌ها (پیشرفته)", callback_data='admin_rebuild')],
            [InlineKeyboardButton("📥 استخراج فایل بکاپ", callback_data='admin_extract')]
        ]
        await query.edit_message_text("⚙️ **پنل مدیریت پیشرفته ربات**\n\nلطفاً یک گزینه را انتخاب کنید:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

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
    
    application.add_handler(conv_handler)
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CommandHandler("delete", delete_number))
    application.add_handler(CallbackQueryHandler(admin_callbacks, pattern="^admin_"))
    
    logger.info("🤖 مکانیزم رمزنگاری فعال شد و ربات در حال اجراست...")
    application.run_polling()

if __name__ == "__main__":
    main()
