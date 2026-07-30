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
from datetime import datetime
from typing import Optional
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
    ConversationHandler,
)

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
LINK_CUSTOM_PREFIX = os.getenv("LINK_CUSTOM_PREFIX", "express")

REDIS_URL = os.getenv("REDIS_URL")

# --- پروکسی ایرانی اختصاصی اسنپ فود ---
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
        logger.warning("⚠️ دیتابیس ردیس تنظیم نشده است.")
except Exception as e:
    redis_client = None
    logger.error(f"❌ خطا در ردیس: {e}")

BASE_HEADERS = {
    'accept': 'application/json, text/plain, */*',
    'accept-language': 'fa',
    'content-type': 'application/json',
    'origin': 'https://snappfood.ir',
    'referer': 'https://snappfood.ir/',
    'user-agent': 'Mozilla/5.0 (Linux; Android 10) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Mobile Safari/537.36'
}

# --- توابع API ---
def send_verification_code(phone_number: str) -> dict:
    url = "https://user.snappfood.ir/v1/auth/otp/send"
    payload = {"mobile_number": phone_number, "type": "Customer"}
    try:
        response = requests.post(url, json=payload, headers=BASE_HEADERS, proxies=SNAPPFOOD_PROXIES, timeout=15)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        logger.error(f"خطا در ارسال کد: {e}")
        return {'success': False, 'error': str(e)}

def verify_code(phone_number: str, code: str) -> dict:
    url = "https://user.snappfood.ir/v1/auth/token"
    device_uid = str(uuid.uuid4())
    payload = {
        "cellphone": phone_number,
        "otpCode": int(code),
        "grantType": "Otp",
        "data": {
            "time": int(datetime.now().timestamp()),
            "device_uid": device_uid,
            "client_id": "snappfood_pwa",
            "client_secret": "snappfood_pwa_secret",
            "scopes": ["mobile_v2", "mobile_v1", "webview"]
        }
    }
    try:
        response = requests.post(url, json=payload, headers=BASE_HEADERS, proxies=SNAPPFOOD_PROXIES, timeout=15)
        data = response.json()
        if not data.get('success'):
            first_names = ["علی", "محمد", "یوسف", "امیر", "حسین", "رضا", "مهدی", "سارا", "زهرا", "مریم"]
            last_names = ["راد", "تهرانی", "حسینی", "پارسا", "دانش", "آریا", "کریمی", "احمدی", "رضایی"]
            payload["firstName"] = random.choice(first_names)
            payload["lastName"] = random.choice(last_names)
            response = requests.post(url, json=payload, headers=BASE_HEADERS, proxies=SNAPPFOOD_PROXIES, timeout=15)
            data = response.json()
        return data
    except requests.exceptions.RequestException as e:
        logger.error(f"خطا در تایید کد: {e}")
        return {'success': False, 'error': str(e)}

def refresh_snappfood_token(refresh_token: str) -> dict:
    """تابع دریافت توکن جدید با استفاده از Refresh Token"""
    url = "https://user.snappfood.ir/v1/auth/token"
    payload = {
        "grantType": "RefreshToken",
        "refreshToken": refresh_token,
        "client_id": "snappfood_pwa",
        "client_secret": "snappfood_pwa_secret"
    }
    try:
        response = requests.post(url, json=payload, headers=BASE_HEADERS, proxies=SNAPPFOOD_PROXIES, timeout=15)
        return response.json()
    except requests.exceptions.RequestException as e:
        logger.error(f"خطا در بازسازی توکن: {e}")
        return {'success': False, 'error': str(e)}

async def shorten_url_shortio(long_url: str, domain: str, api_key: str, phone_number: str) -> Optional[str]:
    if not api_key or not domain:
        return None
    api_url = "https://api.short.io/links"
    custom_path = f"{LINK_CUSTOM_PREFIX}-{phone_number}"
    payload = {"originalURL": long_url, "domain": domain, "path": custom_path}
    headers = {"accept": "application/json", "content-type": "application/json", "Authorization": api_key}
    
    try:
        response = await asyncio.to_thread(requests.post, api_url, json=payload, headers=headers, timeout=10)
        # اگر لینک قبلاً وجود داشت، چند عدد رندوم به انتهای آن اضافه می‌کنیم
        if response.status_code in [400, 409]:
            payload["path"] = f"{custom_path}-{random.randint(10, 99)}"
            response = await asyncio.to_thread(requests.post, api_url, json=payload, headers=headers, timeout=10)
        response.raise_for_status()
        return response.json().get("shortURL")
    except requests.exceptions.RequestException as e:
        logger.error(f"خطا در شورت‌آی‌او: {e}")
        return None

# --- وضعیت‌های بات ---
ASK_PHONE, ASK_CODE, ASK_NEXT_ACTION = range(3)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message.from_user.id not in ALLOWED_USER_IDS:
        return ConversationHandler.END

    context.user_data['session_links'] = []
    await update.message.reply_text(
        "سلام! شماره تلفن را وارد کنید (مثال: 09123456789):",
        reply_markup=ReplyKeyboardRemove()
    )
    return ASK_PHONE

async def ask_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    phone_number = update.message.text
    if not (phone_number.startswith("09") and len(phone_number) == 11 and phone_number.isdigit()):
        await update.message.reply_text("فرمت شماره نامعتبر است. مجدداً ارسال کنید.")
        return ASK_PHONE
        
    context.user_data['phone_number'] = phone_number
    await update.message.reply_text(f"درحال ارسال کد تأیید به {phone_number} از طریق پروکسی...")
    
    verification_response = await asyncio.to_thread(send_verification_code, phone_number)
    
    if verification_response.get('success'):
        keyboard = [['ارسال مجدد کد', 'لغو عملیات']]
        await update.message.reply_text(
            "کد تأیید ارسال شد. لطفاً کد را وارد کنید:",
            reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        )
        return ASK_CODE
    else:
        await update.message.reply_text(f"خطا در ارسال کد. ربات را دوباره با /start اجرا کنید.", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END

async def resend_code(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    phone_number = context.user_data.get('phone_number')
    if phone_number:
        await update.message.reply_text("در حال ارسال مجدد کد...")
        await asyncio.to_thread(send_verification_code, phone_number)
        await update.message.reply_text("کد مجدداً ارسال شد. لطفاً آن را وارد کنید:")
    return ASK_CODE

async def ask_code(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    code = update.message.text
    if code == 'لغو عملیات':
        return await cancel(update, context)

    phone_number = context.user_data.get('phone_number')
    await update.message.reply_text("درحال بررسی و ایجاد لینک، لطفاً صبر کنید...", reply_markup=ReplyKeyboardRemove())
    
    login_response = await asyncio.to_thread(verify_code, phone_number, code)

    if login_response.get('success') and isinstance(login_response.get('data'), dict):
        access_token = login_response['data'].get('accessToken')
        refresh_token = login_response['data'].get('refreshToken')

        snapp_express_direct_link = f"https://snapp.express/?source=jek_pwa-food&food_service_design=new&token={access_token}&sso_channel=food"
        shortened_express_link = await shorten_url_shortio(
            snapp_express_direct_link, SHORTIO_DOMAIN_EXPRESS, SHORTIO_API_KEY_EXPRESS, phone_number
        )

        final_link = shortened_express_link if shortened_express_link else snapp_express_direct_link

        if redis_client:
            redis_data = {
                "phone_number": phone_number,
                "access_token": access_token,
                "refresh_token": refresh_token,
                "short_link": final_link,
                "created_at": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                "updated_at": datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }
            redis_client.set(f"snappfood:token:{phone_number}", json.dumps(redis_data, ensure_ascii=False))

        context.user_data['session_links'].append(f"📱 `{phone_number}`\n🚀 {final_link}\n")

        keyboard = [['➕ ثبت خط بعدی', '✅ پایان و دریافت لینک‌ها']]
        await update.message.reply_text(
            f"✅ لینک برای {phone_number} با موفقیت ساخته شد!\n\nآیا می‌خواهید خط دیگری وارد کنید؟",
            reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        )
        return ASK_NEXT_ACTION
    else:
        await update.message.reply_text("کد اشتباه است یا خطایی رخ داد. کد جدید را وارد کنید یا 'لغو عملیات' را بزنید.")
        return ASK_CODE

async def next_line(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("شماره جدید را وارد کنید:", reply_markup=ReplyKeyboardRemove())
    return ASK_PHONE

async def finish_session(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    links = context.user_data.get('session_links', [])
    if links:
        message_body = "🎉 **لیست تمامی لینک‌های ساخته شده:**\n\n" + "\n".join(links)
        await update.message.reply_text(
            message_body, 
            reply_markup=ReplyKeyboardRemove(),
            disable_web_page_preview=True,
            parse_mode='Markdown'
        )
    else:
        await update.message.reply_text("لینکی ساخته نشد.", reply_markup=ReplyKeyboardRemove())
        
    context.user_data.clear()
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("عملیات لغو شد.", reply_markup=ReplyKeyboardRemove())
    context.user_data.clear()
    return ConversationHandler.END

# --- توابع ادمین و بک‌گراند تسک‌ها ---
async def delete_number(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id not in ALLOWED_USER_IDS:
        return
    if not context.args:
        await update.message.reply_text("لطفاً شماره را وارد کنید.\nمثال: `/delete 09123456789`", parse_mode='Markdown')
        return
        
    phone = context.args[0]
    if redis_client:
        result = redis_client.delete(f"snappfood:token:{phone}")
        if result:
            await update.message.reply_text(f"✅ شماره {phone} با موفقیت از دیتابیس حذف شد.")
        else:
            await update.message.reply_text(f"⚠️ شماره {phone} در دیتابیس یافت نشد.")

async def process_database_rebuild(chat_id: int, bot):
    """پردازش بک‌گراند برای بازسازی تمام توکن‌های دیتابیس"""
    if not redis_client:
        return
        
    keys = redis_client.keys("snappfood:token:*")
    success_count, fail_count = 0, 0
    
    await bot.send_message(chat_id=chat_id, text=f"⏳ عملیات بازسازی برای `{len(keys)}` رکورد آغاز شد...\n(ربات قفل نیست و می‌توانید کارهای دیگر را انجام دهید)", parse_mode='Markdown')
    
    for key in keys:
        try:
            data = json.loads(redis_client.get(key))
            phone = data.get("phone_number")
            r_token = data.get("refresh_token")
            
            if not phone or not r_token:
                fail_count += 1
                continue
                
            res = await asyncio.to_thread(refresh_snappfood_token, r_token)
            if res.get("success") and isinstance(res.get("data"), dict):
                new_access = res["data"].get("accessToken")
                new_refresh = res["data"].get("refreshToken")
                
                long_link = f"https://snapp.express/?source=jek_pwa-food&food_service_design=new&token={new_access}&sso_channel=food"
                new_short = await shorten_url_shortio(long_link, SHORTIO_DOMAIN_EXPRESS, SHORTIO_API_KEY_EXPRESS, phone)
                
                data["access_token"] = new_access
                data["refresh_token"] = new_refresh
                data["short_link"] = new_short or long_link
                data["updated_at"] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                
                redis_client.set(key, json.dumps(data, ensure_ascii=False))
                success_count += 1
            else:
                fail_count += 1
        except Exception as e:
            logger.error(f"خطا در بازسازی {key}: {e}")
            fail_count += 1
            
        # وقفه کوتاه برای جلوگیری از بلاک شدن توسط سرور اسنپ‌فود
        await asyncio.sleep(1)
        
    msg = (
        f"✅ **عملیات بازسازی پایان یافت!**\n\n"
        f"📊 مجموع اکانت‌ها: `{len(keys)}`\n"
        f"🟢 موفق و بروزشده: `{success_count}`\n"
        f"🔴 ناموفق (نیاز به لاگین مجدد): `{fail_count}`\n\n"
        f"💡 می‌توانید با دریافت بکاپ جدید، لینک‌های بروز شده را بردارید."
    )
    await bot.send_message(chat_id=chat_id, text=msg, parse_mode='Markdown')

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id not in ALLOWED_USER_IDS:
        return
        
    keyboard = [
        [InlineKeyboardButton("📊 آمار دیتابیس", callback_data='admin_stats')],
        [InlineKeyboardButton("📥 استخراج شماره‌ها و لینک‌ها", callback_data='admin_extract')],
        [InlineKeyboardButton("🔄 بازسازی دیتابیس لینک‌ها", callback_data='admin_rebuild')]
    ]
    await update.message.reply_text("⚙️ **پنل مدیریت ربات:**", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

async def admin_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not redis_client:
        await query.message.reply_text("⚠️ دیتابیس ردیس متصل نیست!")
        return

    if query.data == 'admin_stats':
        keys = redis_client.keys("snappfood:token:*")
        await query.edit_message_text(
            f"📊 **آمار دیتابیس:**\nتعداد کل اکانت‌های ذخیره شده: `{len(keys)}`\n\n"
            f"برای حذف یک شماره مسدود از دستور زیر استفاده کنید:\n`/delete شماره`", 
            parse_mode='Markdown'
        )

    elif query.data == 'admin_extract':
        await query.message.reply_text("درحال استخراج اطلاعات دیتابیس...")
        keys = redis_client.keys("snappfood:token:*")
        content = "لیست استخراج شده ربات اسنپ اکسپرس\n---------------------------\n"
        
        for k in keys:
            data = json.loads(redis_client.get(k))
            content += f"شماره: {data.get('phone_number')}\nلینک: {data.get('short_link', 'بدون لینک')}\nآخرین بروزرسانی: {data.get('updated_at', 'نامشخص')}\n\n"
            
        doc = io.BytesIO(content.encode('utf-8'))
        doc.name = f"Database_Export_{datetime.now().strftime('%Y%m%d')}.txt"
        await query.message.reply_document(doc, caption=f"📥 فایل بکاپ دیتابیس\nتعداد رکوردها: {len(keys)}")

    elif query.data == 'admin_rebuild':
        # اجرای تابع سنگین در بک‌گراند تسک تا ربات قفل نشود
        asyncio.create_task(process_database_rebuild(query.message.chat_id, context.bot))

def main() -> None:
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            ASK_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_phone)],
            ASK_CODE: [
                MessageHandler(filters.Regex('^ارسال مجدد کد$'), resend_code),
                MessageHandler(filters.TEXT & ~filters.COMMAND, ask_code)
            ],
            ASK_NEXT_ACTION: [
                MessageHandler(filters.Regex('^➕ ثبت خط بعدی$'), next_line),
                MessageHandler(filters.Regex('^✅ پایان و دریافت لینک‌ها$'), finish_session)
            ]
        },
        fallbacks=[CommandHandler("cancel", cancel), MessageHandler(filters.Regex('^لغو عملیات$'), cancel)],
    )
    
    application.add_handler(conv_handler)
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CommandHandler("delete", delete_number)) # هندلر حذف تکی
    application.add_handler(CallbackQueryHandler(admin_callbacks, pattern="^admin_"))
    
    logger.info("ربات با موفقیت روشن شد...")
    application.run_polling()

if __name__ == "__main__":
    main()
