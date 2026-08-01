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


def refresh_snappfood_token_advanced(current_refresh_token: str, device_uid: str) -> dict:
    """دریافت توکن جدید با استفاده از متد رمزنگاری و CloudScraper + پروکسی ایرانی.
    
    نکته مهم: device_uid باید همان مقداری باشد که هنگام ثبت اولیه استفاده شده،
    در غیر این صورت سرور اسنپ‌فود درخواست را رد می‌کند.
    """
    if not server_public_key:
        return {'status': False, 'error': 'کلید رمزنگاری بارگذاری نشد'}

    TOKEN_ENDPOINT_URL = "https://snappfood.ir/oauth2/default/token"

    scraper = cloudscraper.create_scraper(
        browser={'browser': 'chrome', 'platform': 'android', 'desktop': False}
    )

    with scraper as session:
        # اعمال پروکسی ایرانی — ضروری برای دسترسی از خارج از ایران
        if IRAN_PROXY:
            session.proxies.update({"http": IRAN_PROXY, "https": IRAN_PROXY})
            session.trust_env = False  # جلوگیری از بازنویسی پروکسی توسط محیط

        session.headers.update({
            "x-is-bonyan": "true",
            "User-Agent": "Mozilla/5.0 (Linux; Android 10)",
            "Origin": "https://m.snappfood.ir"
        })

        grant_body = {
            "time": int(time.time()),
            "device_uid": device_uid,          # همان UUID ثبت اولیه
            "client_id": "snappfood_pwa",
            "client_secret": "snappfood_pwa_secret",
            "scopes": ["mobile_v2", "mobile_v1", "webview"],
            "grant_type": "refresh_token",
            "refresh_token": current_refresh_token
        }

        try:
            sealed_payload = seal_data_with_sodium(grant_body)
            res = session.post(
                TOKEN_ENDPOINT_URL,
                json={"data": sealed_payload},
                timeout=25
            )

            if res.status_code == 200:
                raw = res.json().get("data", {}) or {}
                # API مقدار را با snake_case برمی‌گرداند (access_token)
                new_access  = raw.get("access_token")  or raw.get("accessToken")
                new_refresh = raw.get("refresh_token") or raw.get("refreshToken") or current_refresh_token
                if new_access:
                    return {
                        'status': True,
                        'data': {
                            'accessToken':  new_access,
                            'refreshToken': new_refresh
                        }
                    }
                return {'status': False, 'error': 'access_token در پاسخ وجود ندارد'}
            logger.error(f"ریفرش توکن — HTTP {res.status_code}: {res.text[:200]}")
            return {'status': False, 'error': f"HTTP {res.status_code}"}
        except Exception as e:
            logger.error(f"خطای ریفرش توکن: {e}")
            return {'status': False, 'error': str(e)}
# ==============================================================================


# --- توابع API ---
def send_verification_code(phone_number: str) -> dict:
    url = "https://user.snappfood.ir/v1/auth/otp/send"
    payload = {"mobile_number": phone_number, "type": "Customer"}
    try:
        response = requests.post(url, json=payload, headers=BASE_HEADERS, proxies=SNAPPFOOD_PROXIES, verify=False, timeout=15)
        if response.status_code == 200:
            return response.json()
        return {'status': False, 'error': f"HTTP {response.status_code}"}
    except Exception as e:
        return {'status': False, 'error': str(e)}


def verify_code(phone_number: str, code: str, device_uid: str) -> dict:
    """تأیید کد OTP — device_uid باید ذخیره و هنگام رفرش توکن دوباره استفاده شود."""
    url = "https://user.snappfood.ir/v1/auth/token"
    payload = {
        "cellphone": phone_number, "otpCode": int(code), "grantType": "Otp",
        "data": {
            "time": int(datetime.now().timestamp()), "device_uid": device_uid,
            "client_id": "snappfood_pwa", "client_secret": "snappfood_pwa_secret",
            "scopes": ["mobile_v2", "mobile_v1", "webview"]
        }
    }
    try:
        response = requests.post(url, json=payload, headers=BASE_HEADERS, proxies=SNAPPFOOD_PROXIES, verify=False, timeout=15)
        data = response.json()
        data_inner = data.get('data', {}) or {}
        if not (data.get('status') or data.get('success')) and 'accessToken' not in data_inner:
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
    if not api_key or not domain:
        return None
    api_url = "https://api.short.io/links"
    custom_path = f"{LINK_CUSTOM_PREFIX}-{phone_number}"
    payload = {"originalURL": long_url, "domain": domain, "path": custom_path}
    headers = {"accept": "application/json", "content-type": "application/json", "Authorization": api_key}
    try:
        response = await asyncio.to_thread(requests.post, api_url, json=payload, headers=headers, timeout=10)
        if response.status_code in [400, 409]:
            payload["path"] = f"{custom_path}-{random.randint(10, 999)}"
            response = await asyncio.to_thread(requests.post, api_url, json=payload, headers=headers, timeout=10)
        response.raise_for_status()
        return response.json().get("shortURL")
    except Exception:
        return None


# ======================== کیبوردهای آماده ========================

def kb_cancel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚫  لغو عملیات", callback_data='cancel')]
    ])


def kb_resend_cancel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄  ارسال مجدد کد", callback_data='resend_code')],
        [InlineKeyboardButton("🚫  لغو عملیات",    callback_data='cancel')]
    ])


def kb_next_or_finish() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕  ثبت شماره جدید",      callback_data='next_line')],
        [InlineKeyboardButton("✅  پایان و دریافت لینک‌ها", callback_data='finish_session')],
        [InlineKeyboardButton("🚫  لغو عملیات",           callback_data='cancel')]
    ])


def kb_admin_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊  آمار دیتابیس",         callback_data='admin_stats'),
         InlineKeyboardButton("🔄  بازسازی لینک‌ها",       callback_data='admin_rebuild')],
        [InlineKeyboardButton("📥  استخراج فایل بکاپ",    callback_data='admin_extract')],
        [InlineKeyboardButton("🗑  حذف شماره",             callback_data='admin_delete_hint')]
    ])


def kb_back_to_admin() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙  بازگشت به پنل",         callback_data='admin_back')]
    ])


# =================================================================

# --- تابع لغو ---
async def cancel_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    if update.callback_query:
        await update.callback_query.answer("عملیات لغو شد.")
        await update.callback_query.edit_message_text(
            "🚫 عملیات لغو شد.\n\nبرای شروع مجدد دستور /start را ارسال کنید."
        )
    elif update.message:
        await update.message.reply_text(
            "🚫 عملیات لغو شد.\n\nبرای شروع مجدد دستور /start را ارسال کنید.",
            reply_markup=ReplyKeyboardRemove()
        )
    return ConversationHandler.END


# --- وضعیت‌های مکالمه ---
ASK_PHONE, ASK_CODE, ASK_NEXT_ACTION = range(3)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.message.from_user
    if user.id not in ALLOWED_USER_IDS:
        await update.message.reply_text("⛔️ شما دسترسی به این ربات ندارید.")
        return ConversationHandler.END

    context.user_data.clear()
    context.user_data['session_links'] = []

    text = (
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "🛒  *ربات لینک‌ساز اسنپ‌مارکت*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "📱  شماره موبایل خود را وارد کنید:\n"
        "_(فرمت صحیح: `09XXXXXXXXX`)_"
    )
    await update.message.reply_text(text, reply_markup=kb_cancel(), parse_mode='Markdown')
    return ASK_PHONE


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id not in ALLOWED_USER_IDS:
        await update.message.reply_text("⛔️ شما دسترسی به این ربات ندارید.")
        return
    text = (
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "📖  *راهنمای ربات*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "🔹 /start  —  ایجاد لینک اسنپ‌مارکت\n"
        "🔹 /admin  —  پنل مدیریت\n"
        "🔹 /delete `09XXXXXXXXX`  —  حذف یک شماره از دیتابیس\n"
        "🔹 /cancel  —  لغو عملیات جاری\n"
        "🔹 /help   —  نمایش این راهنما\n\n"
        "📌 در هر مرحله می‌توانید با دکمه *لغو عملیات* فرآیند را متوقف کنید."
    )
    await update.message.reply_text(text, parse_mode='Markdown')


async def ask_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    phone_number = update.message.text.strip()
    if not (phone_number.startswith("09") and len(phone_number) == 11 and phone_number.isdigit()):
        await update.message.reply_text(
            "⚠️  *شماره وارد شده نامعتبر است.*\n\n"
            "لطفاً شماره را با فرمت صحیح وارد کنید:\n`09XXXXXXXXX`",
            reply_markup=kb_cancel(),
            parse_mode='Markdown'
        )
        return ASK_PHONE

    context.user_data['phone_number'] = phone_number
    wait_msg = await update.message.reply_text(
        f"⏳  درحال ارسال کد تأیید به `{phone_number}` ...",
        parse_mode='Markdown'
    )

    res = await asyncio.to_thread(send_verification_code, phone_number)

    if res.get('status') or res.get('success'):
        await wait_msg.delete()
        await update.message.reply_text(
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"✅  *کد تأیید ارسال شد*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📲  کد ۵ رقمی ارسال‌شده به `{phone_number}` را وارد کنید:",
            reply_markup=kb_resend_cancel(),
            parse_mode='Markdown'
        )
        return ASK_CODE
    else:
        err_msg = res.get('error', 'ارتباط با سرور برقرار نشد.')
        await wait_msg.delete()
        await update.message.reply_text(
            f"❌  *خطا در ارسال کد تأیید*\n\n"
            f"جزئیات: `{err_msg}`\n\n"
            f"دستور /start را مجدداً ارسال کنید.",
            parse_mode='Markdown'
        )
        return ConversationHandler.END


async def resend_code_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    phone = context.user_data.get('phone_number')
    if not phone:
        await query.answer("⚠️ شماره‌ای ثبت نشده است.", show_alert=True)
        return ASK_CODE

    await query.answer("درحال ارسال مجدد کد...")
    res = await asyncio.to_thread(send_verification_code, phone)

    if res.get('status') or res.get('success'):
        await query.edit_message_text(
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🔄  *کد جدید ارسال شد*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📲  کد جدید ارسال‌شده به `{phone}` را وارد کنید:",
            reply_markup=kb_resend_cancel(),
            parse_mode='Markdown'
        )
    else:
        await query.edit_message_text(
            "❌  ارسال مجدد با خطا مواجه شد.\nلطفاً دوباره تلاش کنید یا عملیات را لغو کنید.",
            reply_markup=kb_resend_cancel()
        )
    return ASK_CODE


async def ask_code(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    code = update.message.text.strip()
    phone_number = context.user_data.get('phone_number')

    if not code.isdigit():
        await update.message.reply_text(
            "⚠️  لطفاً فقط اعداد کد تأیید را وارد کنید.",
            reply_markup=kb_resend_cancel()
        )
        return ASK_CODE

    wait_msg = await update.message.reply_text("⏳  درحال اعتبارسنجی کد و ایجاد لینک...")

    # device_uid ثابت به‌ازای هر اکانت — باید ذخیره شود تا هنگام رفرش توکن دوباره استفاده شود
    device_uid = str(uuid.uuid4())
    res = await asyncio.to_thread(verify_code, phone_number, code, device_uid)

    data_dict = res.get('data') or {}
    access_token = data_dict.get('accessToken')
    refresh_token = data_dict.get('refreshToken')

    if (res.get('status') or res.get('success')) and access_token:
        snapp_market_link = (
            f"https://snapp.market/?source=jek_pwa-food"
            f"&food_service_design=new&token={access_token}&sso_channel=food"
        )
        short_link = await shorten_url_shortio(
            snapp_market_link, SHORTIO_DOMAIN_EXPRESS, SHORTIO_API_KEY_EXPRESS, phone_number
        )
        final_link = short_link if short_link else snapp_market_link

        if redis_client:
            redis_data = {
                "phone_number": phone_number,
                "device_uid": device_uid,        # ذخیره device_uid برای رفرش توکن آینده
                "access_token": access_token,
                "refresh_token": refresh_token,
                "short_link": final_link,
                "created_at": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                "updated_at": datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }
            try:
                redis_client.set(f"snappfood:token:{phone_number}", json.dumps(redis_data, ensure_ascii=False))
            except Exception as e:
                logger.error(f"خطا در ذخیره ردیس: {e}")

        context.user_data['session_links'].append(f"📱 `{phone_number}`\n🔗 {final_link}")

        link_type = "🔗 لینک کوتاه" if short_link else "🔗 لینک مستقیم"
        await wait_msg.delete()
        await update.message.reply_text(
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🎉  *لینک با موفقیت ساخته شد!*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📱  شماره: `{phone_number}`\n"
            f"{link_type}: {final_link}\n\n"
            f"🔽  مرحله بعد را انتخاب کنید:",
            reply_markup=kb_next_or_finish(),
            parse_mode='Markdown',
            disable_web_page_preview=True
        )
        return ASK_NEXT_ACTION
    else:
        err_msg = res.get('error') or res.get('message') or 'کد نامعتبر یا منقضی شده است.'
        await wait_msg.delete()
        await update.message.reply_text(
            f"⚠️  *خطا در اعتبارسنجی*\n\n"
            f"جزئیات: `{err_msg}`\n\n"
            f"کد جدید دریافت کنید یا عملیات را لغو کنید:",
            reply_markup=kb_resend_cancel(),
            parse_mode='Markdown'
        )
        return ASK_CODE


async def next_line_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    count = len(context.user_data.get('session_links', []))
    await query.edit_message_text(
        f"✅  لینک شماره {count} ذخیره شد.\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📱  شماره موبایل بعدی را وارد کنید:\n"
        f"_(فرمت صحیح: `09XXXXXXXXX`)_",
        reply_markup=kb_cancel(),
        parse_mode='Markdown'
    )
    return ASK_PHONE


async def finish_session_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer("درحال آماده‌سازی نتیجه...")
    await query.edit_message_reply_markup(reply_markup=None)

    links = context.user_data.get('session_links', [])
    context.user_data.clear()

    if links:
        count = len(links)
        links_text = "\n\n".join(links)
        msg = (
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📦  *لیست لینک‌های این جلسه*\n"
            f"🔢  تعداد: {count} لینک\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"{links_text}\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"✅  برای جلسه جدید: /start"
        )
        await query.message.reply_text(msg, parse_mode='Markdown', disable_web_page_preview=True)
    else:
        await query.message.reply_text(
            "ℹ️  هیچ لینکی در این جلسه ثبت نشد.\n\nبرای شروع: /start"
        )
    return ConversationHandler.END


# --- پنل مدیریت ادمین ---
async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id not in ALLOWED_USER_IDS:
        return

    db_status = "🟢 متصل" if redis_client else "🔴 قطع"
    record_count = len(redis_client.keys("snappfood:token:*")) if redis_client else 0

    text = (
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⚙️  *پنل مدیریت پیشرفته*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🗄  وضعیت دیتابیس: {db_status}\n"
        f"📊  تعداد رکوردها: `{record_count}`\n\n"
        f"یک گزینه را انتخاب کنید:"
    )
    await update.message.reply_text(text, reply_markup=kb_admin_main(), parse_mode='Markdown')


async def delete_number(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id not in ALLOWED_USER_IDS:
        return
    if not context.args:
        await update.message.reply_text(
            "⚠️  *نحوه استفاده:*\n`/delete 09123456789`",
            parse_mode='Markdown'
        )
        return
    phone = context.args[0].strip()
    if not (phone.startswith("09") and len(phone) == 11 and phone.isdigit()):
        await update.message.reply_text("⚠️  فرمت شماره نامعتبر است.", parse_mode='Markdown')
        return
    if redis_client and redis_client.delete(f"snappfood:token:{phone}"):
        await update.message.reply_text(
            f"✅  شماره `{phone}` با موفقیت از دیتابیس حذف شد.",
            parse_mode='Markdown'
        )
    else:
        await update.message.reply_text(
            f"⚠️  شماره `{phone}` در دیتابیس یافت نشد.",
            parse_mode='Markdown'
        )


async def process_database_rebuild(chat_id: int, bot):
    """بازسازی توکن‌ها با رمزنگاری پیشرفته در پس‌زمینه"""
    if not redis_client:
        await bot.send_message(chat_id=chat_id, text="❌  دیتابیس ردیس متصل نیست!")
        return

    keys = redis_client.keys("snappfood:token:*")
    total = len(keys)
    if total == 0:
        await bot.send_message(chat_id=chat_id, text="ℹ️  هیچ رکوردی در دیتابیس یافت نشد.")
        return

    success_count, fail_count = 0, 0
    await bot.send_message(
        chat_id=chat_id,
        text=(
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🔄  *شروع بازسازی پیشرفته*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📊  مجموع رکوردها: `{total}`\n"
            f"⏳  لطفاً صبر کنید..."
        ),
        parse_mode='Markdown'
    )

    for key in keys:
        try:
            raw = redis_client.get(key)
            if not raw:
                fail_count += 1
                continue
            data = json.loads(raw)
            phone   = data.get("phone_number")
            r_token = data.get("refresh_token")
            # همان device_uid که هنگام ثبت اولیه ذخیره شد — اگر نبود یک UUID جدید می‌سازیم
            d_uid   = data.get("device_uid") or str(uuid.uuid4())

            if not phone or not r_token:
                fail_count += 1
                continue

            res = await asyncio.to_thread(refresh_snappfood_token_advanced, r_token, d_uid)
            new_data_dict = res.get('data') or {}
            new_access = new_data_dict.get('accessToken')
            new_refresh = new_data_dict.get('refreshToken')

            if (res.get('status') or res.get('success')) and new_access:
                long_link = (
                    f"https://snapp.market/?source=jek_pwa-food"
                    f"&food_service_design=new&token={new_access}&sso_channel=food"
                )
                new_short = await shorten_url_shortio(
                    long_link, SHORTIO_DOMAIN_EXPRESS, SHORTIO_API_KEY_EXPRESS, phone
                )
                data["access_token"] = new_access
                data["refresh_token"] = new_refresh or r_token
                data["short_link"] = new_short or long_link
                data["updated_at"] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                redis_client.set(key, json.dumps(data, ensure_ascii=False))
                success_count += 1
            else:
                fail_count += 1
        except Exception as ex:
            logger.error(f"خطا در بازسازی {key}: {ex}")
            fail_count += 1

        await asyncio.sleep(2)

    await bot.send_message(
        chat_id=chat_id,
        text=(
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"✅  *بازسازی پیشرفته پایان یافت*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📊  مجموع: `{total}`\n"
            f"🟢  موفق: `{success_count}`\n"
            f"🔴  ناموفق: `{fail_count}`\n\n"
            f"💡  حالا می‌توانید بکاپ بگیرید."
        ),
        parse_mode='Markdown'
    )


async def admin_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not redis_client:
        await query.message.reply_text("❌  دیتابیس ردیس متصل نیست!")
        return

    if query.data == 'admin_stats':
        keys = redis_client.keys("snappfood:token:*")
        count = len(keys)
        await query.edit_message_text(
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📊  *آمار دیتابیس*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🗄  تعداد کل اکانت‌های فعال: `{count}`\n\n"
            f"🗑  برای حذف یک خط:\n`/delete 09XXXXXXXXX`",
            parse_mode='Markdown',
            reply_markup=kb_back_to_admin()
        )

    elif query.data == 'admin_extract':
        keys = redis_client.keys("snappfood:token:*")
        if not keys:
            await query.answer("⚠️ دیتابیس خالی است!", show_alert=True)
            return

        await query.answer("درحال آماده‌سازی فایل...")
        lines = ["لیست استخراج شده ربات", f"تاریخ: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", "-" * 40, ""]
        for k in keys:
            try:
                data = json.loads(redis_client.get(k))
                lines.append(f"شماره:          {data.get('phone_number', 'نامشخص')}")
                lines.append(f"لینک:           {data.get('short_link', 'بدون لینک')}")
                lines.append(f"آخرین بروزرسانی: {data.get('updated_at', 'نامشخص')}")
                lines.append("-" * 40)
            except Exception:
                continue

        content = "\n".join(lines)
        doc = io.BytesIO(content.encode('utf-8'))
        doc.seek(0)
        doc.name = f"DB_Export_{datetime.now().strftime('%Y%m%d_%H%M')}.txt"
        await query.message.reply_document(
            doc,
            caption=(
                f"📥  *فایل بکاپ دیتابیس*\n"
                f"📊  تعداد رکوردها: `{len(keys)}`\n"
                f"🕐  زمان: `{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}`"
            ),
            parse_mode='Markdown'
        )

    elif query.data == 'admin_rebuild':
        await query.edit_message_text(
            "🔄  *عملیات بازسازی در پس‌زمینه آغاز شد...*\n\nبه محض پایان نتیجه ارسال می‌شود.",
            parse_mode='Markdown'
        )
        asyncio.ensure_future(process_database_rebuild(query.message.chat_id, context.bot))

    elif query.data == 'admin_delete_hint':
        await query.answer()
        await query.message.reply_text(
            "🗑  *حذف شماره از دیتابیس:*\n\n"
            "دستور زیر را ارسال کنید:\n`/delete 09XXXXXXXXX`",
            parse_mode='Markdown'
        )

    elif query.data == 'admin_back':
        db_status = "🟢 متصل" if redis_client else "🔴 قطع"
        record_count = len(redis_client.keys("snappfood:token:*")) if redis_client else 0
        text = (
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"⚙️  *پنل مدیریت پیشرفته*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🗄  وضعیت دیتابیس: {db_status}\n"
            f"📊  تعداد رکوردها: `{record_count}`\n\n"
            f"یک گزینه را انتخاب کنید:"
        )
        await query.edit_message_text(text, reply_markup=kb_admin_main(), parse_mode='Markdown')


def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        logger.critical("❌ TELEGRAM_BOT_TOKEN تنظیم نشده است!")
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
                CallbackQueryHandler(next_line_callback,     pattern='^next_line$'),
                CallbackQueryHandler(finish_session_callback, pattern='^finish_session$'),
                CallbackQueryHandler(cancel_action,           pattern='^cancel$')
            ]
        },
        fallbacks=[
            CommandHandler("cancel", cancel_action),
            CommandHandler("start",  start),
        ],
        allow_reentry=True,
    )

    application.add_handler(conv_handler)
    application.add_handler(CommandHandler("admin",  admin_command))
    application.add_handler(CommandHandler("delete", delete_number))
    application.add_handler(CommandHandler("help",   help_command))
    application.add_handler(CallbackQueryHandler(admin_callbacks, pattern="^admin_"))

    logger.info("🤖 ربات با موفقیت راه‌اندازی شد.")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

