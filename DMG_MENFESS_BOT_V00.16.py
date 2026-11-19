import asyncio
import json
import os
import re
import time
import tempfile
from datetime import date, datetime, timedelta, timezone
from html import escape
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatType
from telegram.error import TelegramError
from telegram.ext import (
    Application, ApplicationHandlerStop, CallbackQueryHandler, CommandHandler,
    ContextTypes, MessageHandler, filters,
)

# ==================== ENV ====================
BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"


def load_env_file():
    if not ENV_FILE.exists():
        return
    for raw in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_env_file()


def env_int(name, default=None):
    value = os.getenv(name)
    if value in (None, ""):
        if default is None:
            raise RuntimeError(f"Konfigurasi {name} belum diisi di .env")
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} harus berupa angka.") from exc


def env_int_list(name):
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Konfigurasi {name} belum diisi di .env")
    try:
        return [int(x.strip()) for x in value.split(",") if x.strip()]
    except ValueError as exc:
        raise RuntimeError(f"{name} harus berupa daftar ID angka dipisahkan koma.") from exc


BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = env_int_list("ADMIN_IDS")
DEFAULT_POST_ID = os.getenv("TARGET_CHAT_ID", "").strip()
DEFAULT_POST_NAME = os.getenv("TARGET_NAME", "").strip().lstrip("@")
DEFAULT_LOG_ID = os.getenv("LOG_CHAT_ID", "").strip()
DEFAULT_QUOTA = env_int("KUOTA_HARIAN", 100)
DEFAULT_DELAY = env_int("DELAY", 60)
UTC_OFFSET = env_int("UTC_OFFSET", 7)
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN belum diisi di .env")

# ==================== JSON DATABASE ====================
DATA_FILE = BASE_DIR / "menfess_data.json"
DEFAULT_DATA = {
    "ban": {}, "users": {}, "settings": {}, "usage": {},
    "pending_media": {}, "secret_conversations": {}, "menfess_posts": {},
}


def muat_data():
    data = {}
    if DATA_FILE.exists():
        try:
            data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {}
        except (json.JSONDecodeError, OSError):
            # Simpan salinan file rusak sebelum memulai database kosong.
            try:
                DATA_FILE.replace(DATA_FILE.with_name("menfess_data.corrupt.json"))
            except OSError:
                pass
            data = {}
    for key, value in DEFAULT_DATA.items():
        data.setdefault(key, value.copy() if isinstance(value, dict) else value)
    cfg = data["settings"]
    cfg.setdefault("post_chat_id", DEFAULT_POST_ID)
    cfg.setdefault("post_name", DEFAULT_POST_NAME or "Tujuan Post")
    cfg.setdefault("post_title", "")
    cfg.setdefault("post_username", "")
    cfg.setdefault("log_chat_id", DEFAULT_LOG_ID)
    cfg.setdefault("quota", DEFAULT_QUOTA)
    cfg.setdefault("delay", DEFAULT_DELAY)
    cfg.setdefault("blacklist", [])
    cfg.setdefault("blacklist_enabled", True)
    cfg.setdefault("hashtags", [])
    cfg.setdefault("force_subs", [])
    cfg.setdefault("menfess_open", True)
    cfg.setdefault("guide_sections", [
        {"title": "Cara Mengirim Menfess", "content": "Kirim teks atau media melalui chat bot. Letakkan hashtag wajib di bagian paling awal teks atau caption."},
        {"title": "Publikasi Menfess", "content": "Menfess teks akan diterbitkan otomatis jika memenuhi ketentuan. Foto/video masuk antrean dan diterbitkan setelah disetujui admin."},
        {"title": "Lapor Admin", "content": "Gunakan tombol Lapor untuk mengirim pesan atau laporan secara pribadi kepada admin. Admin dapat membalas melalui bot."},
        {"title": "Privasi", "content": "Identitas pengirim tidak dicantumkan pada postingan menfess. Admin dapat melihat Telegram ID untuk keperluan pengelolaan laporan dan moderasi."},
    ])
    return data


DB = muat_data()
SAVE_LOCK = asyncio.Lock()
waktu_mulai = time.time()


async def simpan_data():
    async with SAVE_LOCK:
        fd, tmp = tempfile.mkstemp(prefix="menfess-", suffix=".tmp", dir=BASE_DIR)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(DB, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, DATA_FILE)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)


def cfg():
    return DB["settings"]


def adalah_admin(update):
    return bool(update.effective_user and update.effective_user.id in ADMIN_IDS)


def catat_user(user):
    DB["users"][str(user.id)] = {
        "username": user.username or "",
        "nama": user.first_name or user.full_name or "",
    }


def ambil_data(user_id):
    today = date.today().isoformat()
    data = DB["usage"].setdefault(str(user_id), {})
    if data.get("tanggal") != today:
        data.update({"tanggal": today, "terpakai": 0, "terakhir": 0})
    return data


# ==================== TIME / LOG ====================
BULAN = [
    "Januari", "Februari", "Maret", "April", "Mei", "Juni",
    "Juli", "Agustus", "September", "Oktober", "November", "Desember",
]
NAMA_ZONA = {7: "WIB", 8: "WITA", 9: "WIT"}


def waktu_sekarang():
    now = datetime.now(timezone(timedelta(hours=UTC_OFFSET)))
    zona = NAMA_ZONA.get(UTC_OFFSET, f"UTC{UTC_OFFSET:+d}")
    return (
        f"{now.day} {BULAN[now.month - 1]} {now.year}",
        now.strftime("%H:%M:%S") + " " + zona,
    )


def papan_ban(uid):
    action = "unban" if str(uid) in DB["ban"] else "ban"
    label = "♻️ Unban user" if action == "unban" else "🚫 Ban user"
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=f"{action}:{uid}")]])


async def kirim_log(context, user, isi, link):
    log_id = cfg().get("log_chat_id")
    if not log_id:
        return
    tanggal, jam = waktu_sekarang()
    username = f"@{escape(user.username)}" if user.username else "-"
    nama = escape(user.first_name or "User")
    mention = f'<a href="tg://user?id={user.id}">{nama}</a>'
    teks = (
        "📋 <b>LOG MENFESS</b>\n\n"
        f"👤 Mention: {mention}\n🆔 ID: <code>{user.id}</code>\n"
        f"🔗 Username: {username}\n📅 Tanggal: {tanggal}\n🕒 Waktu: {jam}\n\n"
        f"💬 Isi:\n{escape(isi or '[Media]')}\n\n"
        f'📎 <a href="{escape(link, quote=True)}">Buka postingan</a>'
    )
    try:
        await context.bot.send_message(
            chat_id=log_id, text=teks, parse_mode="HTML",
            reply_markup=papan_ban(user.id),
        )
    except TelegramError:
        print("Gagal mengirim log:", log_id)


def link_post(chat, message_id):
    if chat.username:
        return f"https://t.me/{chat.username}/{message_id}"
    chat_id = str(chat.id)
    if chat_id.startswith("-100"):
        return f"https://t.me/c/{chat_id[4:]}/{message_id}"
    return ""


async def footer_menfess(context):
    """Footer HTML dengan username bot yang diambil secara dinamis."""
    me = await context.bot.get_me()
    username = me.username or ""
    if username:
        return ("<blockquote><b>💌 Kirim menfess via : "
                f'<a href="https://t.me/{escape(username, quote=True)}">@{escape(username)}</a>'
                "</b></blockquote>")
    return "<blockquote><b>💌 Kirim menfess via bot DMG</b></blockquote>"


def teks_panduan():
    sections = cfg().get("guide_sections", [])
    parts = ["📖 <b>PANDUAN DMG MENFESS</b>"]
    for index, item in enumerate(sections, 1):
        title = escape(str(item.get("title") or f"Bagian {index}"))
        content = escape(str(item.get("content") or ""))
        parts.append(f"<b>{index}. {title}</b>\n{content}")
    if not sections:
        parts.append("Panduan belum tersedia. Silakan hubungi admin.")
    return "\n\n".join(parts)


def keyboard_panduan_admin():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Tambah isi", callback_data="setting:guide_add"),
         InlineKeyboardButton("✏️ Edit isi", callback_data="setting:guide_edit_list")],
        [InlineKeyboardButton("🗑️ Hapus isi", callback_data="setting:guide_delete_list")],
        [InlineKeyboardButton("⬅️ Kembali", callback_data="panel:settings")],
    ])


def catat_post_menfess(chat_id, message_id, user_id, username, nama, isi, jenis="Teks"):
    key = f"{chat_id}:{message_id}"
    DB["menfess_posts"][key] = {
        "chat_id": int(chat_id),
        "message_id": int(message_id),
        "user_id": int(user_id),
        "username": username or "",
        "nama": nama or "User",
        "isi": isi or "",
        "jenis": jenis,
        "created_at": int(time.time()),
    }


# ==================== USER UI ====================
def keyboard_utama():
    # Nama tujuan dan username disimpan terpisah agar label tombol
    # menampilkan judul Channel/Group, sedangkan URL memakai username.
    post_name = cfg().get("post_title") or cfg().get("post_name") or "Channel/Grup Post"
    username = str(cfg().get("post_username") or "").strip().lstrip("@")

    # Kompatibilitas dengan database versi lama yang menyimpan @username
    # di post_name. Untuk data lama, gunakan nilai tersebut sebagai URL.
    legacy_name = str(cfg().get("post_name") or "").strip()
    if not username and legacy_name.startswith("@"):
        username = legacy_name.lstrip("@")

    buttons = [InlineKeyboardButton("📖 Panduan", callback_data="ui:panduan")]
    if username and not username.startswith("-") and " " not in username:
        buttons.append(
            InlineKeyboardButton(
                "📢 " + str(post_name)[:35],
                url=f"https://t.me/{username}"
            )
        )
    return InlineKeyboardMarkup([
        buttons,
        [InlineKeyboardButton("📩 Lapor", callback_data="secret:start")],
    ])


def hashtag_valid(text):
    allowed = {str(x).lower().lstrip("#") for x in cfg().get("hashtags", [])}
    if not allowed:
        return False
    match = re.match(r"^\s*(#[A-Za-z0-9_]+)(?:\s|$)", text or "")
    return bool(match and match.group(1)[1:].lower() in allowed)


def teks_hashtag_tidak_valid():
    tags = cfg().get("hashtags", [])
    if not tags:
        return "❌ Hashtag belum dikonfigurasi admin. Silakan hubungi admin."
    daftar = "\n".join("• #" + escape(str(x).lstrip("#")) for x in tags)
    return ("❌ Pesan harus diawali salah satu hashtag wajib.\n\n"
            "Hashtag yang tersedia:\n" + daftar)


async def cek_force_sub(context, user_id):
    if user_id in ADMIN_IDS:
        return True
    for item in cfg().get("force_subs", []):
        if not item.get("enabled", True):
            continue
        try:
            member = await context.bot.get_chat_member(item["chat_id"], user_id)
            if member.status in ("left", "kicked"):
                return False
            if member.status == "restricted" and not getattr(member, "is_member", False):
                return False
        except TelegramError:
            return False
    return True


def keyboard_force_sub():
    rows = []
    for item in cfg().get("force_subs", []):
        if item.get("enabled", True):
            rows.append([InlineKeyboardButton(
                "📢 " + str(item.get("title") or "Grup/Channel")[:50],
                url=item["link"]
            )])
    rows.append([InlineKeyboardButton("🔄 Saya sudah bergabung", callback_data="ui:cek_sub")])
    return InlineKeyboardMarkup(rows)


async def tampilkan_force_sub(message):
    await message.reply_text(
        "🔒 <b>AKSES BELUM TERBUKA</b>\n\n"
        "Silakan bergabung ke semua grup/channel berikut terlebih dahulu, "
        "lalu tekan tombol pemeriksaan.",
        parse_mode="HTML", reply_markup=keyboard_force_sub()
    )


async def kirim_sambutan(message, user):
    post_name = escape(str(cfg().get("post_name") or "Channel/Grup Post"))
    fullname = escape(user.full_name or user.first_name or "User")
    tags = cfg().get("hashtags", [])
    hashtag_text = " ".join("#" + escape(str(x).lstrip("#")) for x in tags) if tags else "Belum diatur admin"
    teks = (
        "💌 <b>DMG MENFESS</b>\n\n"
        f"Hello, {fullname}!\n\n"
        f"Layanan auto-post menfess menuju <b>{post_name}</b>.\n\n"
        "<b>HOW TO SEND</b>\n"
        "1. Baca rules terlebih dahulu.\n"
        "2. Gunakan hashtag di awal pesan.\n"
        "3. Kirim teks, foto, atau video.\n\n"
        f"<b>HASHTAGS</b>\n{hashtag_text}\n\n"
        "<i>Contoh: #fwbgirl halo semuanya!</i>"
    )
    await message.reply_text(teks, parse_mode="HTML", reply_markup=keyboard_utama())


async def ui_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.data == "ui:cek_sub":
        if await cek_force_sub(context, query.from_user.id):
            await query.answer("Berhasil! Akses dibuka.")
            await kirim_sambutan(query.message, query.from_user)
        else:
            await query.answer("Kamu belum bergabung ke semua tujuan.", show_alert=True)
        return
    await query.answer()
    if query.data == "ui:panduan":
        await query.message.reply_text(teks_panduan(), parse_mode="HTML", disable_web_page_preview=True)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    catat_user(user)
    await simpan_data()
    if not await cek_force_sub(context, user.id):
        await tampilkan_force_sub(update.effective_message)
        return
    await kirim_sambutan(update.effective_message, user)


# ==================== LAPORAN MEMBER ====================
def percakapan_rahasia(user_id):
    item = DB["secret_conversations"].get(str(user_id))
    if item and item.get("status") == "open":
        return item
    return None


def keyboard_pesan_rahasia(user_id, conversation_id):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🔒 Akhiri", callback_data=f"secret:close:{user_id}:{conversation_id}"),
    ]])


async def pesan_rahasia_callback(update, context):
    query = update.callback_query
    parts = query.data.split(":")
    action = parts[1]

    if action == "start":
        user = query.from_user
        item = percakapan_rahasia(user.id)
        if not item:
            conversation_id = str(int(time.time() * 1000))
            item = {
                "id": conversation_id,
                "user_id": user.id,
                "nama": user.full_name or user.first_name or "User",
                "username": user.username or "",
                "status": "open",
                "created_at": int(time.time()),
            }
            DB["secret_conversations"][str(user.id)] = item
            await simpan_data()
        await query.answer()
        await query.message.reply_text(
            "🔒 <b>LAPORAN MEMBER UNTUK ADMIN</b>\n\n"
            "Silakan kirim pesan atau laporan kamu di chat ini. Laporan hanya diteruskan kepada admin DMG. "
            "Admin dapat melihat Telegram ID kamu dan membalas melalui bot.\n\n"
            "Tekan tombol Akhiri jika ingin menutup percakapan.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔒 Akhiri Laporan", callback_data=f"secret:user_close:{item['id']}")
            ]]),
        )
        return

    if action == "user_close":
        item = percakapan_rahasia(query.from_user.id)
        if item and item.get("id") == parts[2]:
            item["status"] = "closed"
            await simpan_data()
            await query.answer("Percakapan ditutup.")
            await query.message.reply_text("🔒 Sesi laporan sudah kamu akhiri.")
            for admin_id in ADMIN_IDS:
                try:
                    await context.bot.send_message(
                        chat_id=admin_id,
                        text=f"🔒 Pengguna ID <code>{query.from_user.id}</code> mengakhiri sesi laporan.",
                        parse_mode="HTML",
                    )
                except TelegramError:
                    pass
        else:
            await query.answer("Tidak ada percakapan aktif.", show_alert=True)
        return

    if query.from_user.id not in ADMIN_IDS:
        await query.answer("Khusus admin.", show_alert=True)
        return

    if len(parts) < 4:
        await query.answer("Data percakapan tidak valid.", show_alert=True)
        return
    user_id, conversation_id = parts[2], parts[3]
    item = DB["secret_conversations"].get(user_id)
    if not item or item.get("id") != conversation_id or item.get("status") != "open":
        await query.answer("Percakapan sudah ditutup atau tidak tersedia.", show_alert=True)
        return

    if action == "reply":
        context.user_data["menunggu"] = "secret_reply"
        context.user_data["secret_reply_to"] = user_id
        context.user_data["secret_reply_id"] = conversation_id
        await query.answer()
        await query.message.reply_text(
            f"↩️ Kirim balasan untuk pengguna ID <code>{user_id}</code>.\n"
            "Balasan berikutnya akan dikirim melalui bot. Ketik /batal untuk membatalkan.",
            parse_mode="HTML",
        )
    elif action == "close":
        item["status"] = "closed"
        await simpan_data()
        await query.answer("Percakapan ditutup.")
        try:
            await context.bot.send_message(
                chat_id=int(user_id),
                text="🔒 Admin telah mengakhiri sesi laporan ini.",
            )
        except TelegramError:
            pass
        await query.edit_message_text(
            f"🔒 Percakapan dengan pengguna ID <code>{user_id}</code> telah ditutup.",
            parse_mode="HTML",
        )


async def pesan_rahasia_pengguna(update, context):
    message, user = update.effective_message, update.effective_user
    item = percakapan_rahasia(user.id)
    if not item:
        return False
    if not message.text:
        await message.reply_text("Fitur Lapor saat ini hanya mendukung pesan teks.")
        return True

    teks = message.text.strip()
    if not teks:
        await message.reply_text("Pesan tidak boleh kosong.")
        return True

    username = f"@{item['username']}" if item.get("username") else "(tanpa username)"
    notif = (
        "💌 <b>LAPORAN MEMBER BARU</b>\n\n"
        f"👤 Nama: {escape(item.get('nama') or 'User')}\n"
        f"🔗 Username: {escape(username)}\n"
        f"🆔 Telegram ID: <code>{user.id}</code>\n"
        f"🧵 Percakapan: <code>{item['id']}</code>\n\n"
        f"💬 <b>Pesan:</b>\n{escape(teks[:3500])}"
    )
    berhasil = 0
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=notif,
                parse_mode="HTML",
                reply_markup=keyboard_pesan_rahasia(user.id, item["id"]),
            )
            berhasil += 1
        except TelegramError:
            continue
    if berhasil:
        await message.reply_text("✅ Laporan berhasil diteruskan kepada admin.")
    else:
        await message.reply_text("⚠️ Laporan belum bisa diteruskan. Silakan coba lagi nanti.")
    return True


# ==================== BLACKLIST ====================
def normalize_word(text):
    return re.sub(r"[\W_]+", "", text.casefold(), flags=re.UNICODE)


def kata_terlarang(text):
    if not cfg().get("blacklist_enabled", True):
        return None
    normalized = normalize_word(text)
    for word in cfg().get("blacklist", []):
        candidate = normalize_word(word)
        if candidate and candidate in normalized:
            return word
    return None


# ==================== MEDIA ====================
def pesan_media(message):
    return bool(
        message.photo or message.video or message.animation or message.document
        or message.audio or message.voice or message.video_note or message.sticker
    )


def jenis_media(message):
    if message.photo: return "Foto"
    if message.video: return "Video"
    if message.animation: return "GIF"
    if message.document: return "Dokumen"
    if message.audio: return "Audio"
    if message.voice: return "Voice"
    if message.video_note: return "Video note"
    if message.sticker: return "Stiker"
    return "Media"


async def notifikasi_admin_media(context, pending_id, user, caption, kind):
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔍 Periksa & putuskan", callback_data=f"media:review:{pending_id}")
    ]])
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(
                admin_id,
                f"🆕 <b>MEDIA MENUNGGU PERSETUJUAN</b>\n"
                f"ID: <code>{pending_id}</code>\nPengirim: {escape(user.full_name)}\n"
                f"Jenis: {kind}\nCaption: {escape(caption or '-')}",
                parse_mode="HTML", reply_markup=keyboard,
            )
        except TelegramError:
            print("Tidak dapat mengirim notifikasi media ke admin:", admin_id)


async def proses_media(update, context):
    message, user = update.effective_message, update.effective_user
    if not message or not user:
        return
    catat_user(user)
    if not cfg().get("menfess_open", True):
        await message.reply_text("🔒 Penerimaan menfess sedang ditutup sementara oleh admin.")
        return
    if not await cek_force_sub(context, user.id):
        await tampilkan_force_sub(message)
        return
    if str(user.id) in DB["ban"]:
        await message.reply_text("🚫 Kamu di-ban dan tidak bisa mengirim menfess.")
        return
    data = ambil_data(user.id)
    quota = int(cfg()["quota"])
    if int(data.get("terpakai", 0)) >= quota:
        await message.reply_text(f"❌ Kuota hari ini sudah habis ({quota}/{quota}).")
        return
    wait = int(cfg()["delay"]) - (time.time() - float(data.get("terakhir", 0)))
    if wait > 0:
        await message.reply_text(f"⏳ Tunggu {int(wait) + 1} detik sebelum mengirim lagi.")
        return
    caption = message.caption or ""
    if not hashtag_valid(caption):
        await message.reply_text(teks_hashtag_tidak_valid(), parse_mode="HTML")
        return
    blocked = kata_terlarang(caption)
    if blocked:
        await message.reply_text("⛔ Media ditolak karena caption mengandung kata yang masuk blacklist.")
        return
    pending_id = str(int(time.time() * 1000))
    DB["pending_media"][pending_id] = {
        "user_id": user.id, "user_name": user.full_name,
        "username": user.username or "",
        "source_chat_id": message.chat_id, "source_message_id": message.message_id,
        "caption": caption, "kind": jenis_media(message),
        "created_at": int(time.time()),
    }
    await simpan_data()
    await message.reply_text(
        "🕒 Media kamu sudah masuk antrean pemeriksaan admin.\n"
        "Media tidak akan diterbitkan sebelum disetujui."
    )
    await notifikasi_admin_media(context, pending_id, user, caption, jenis_media(message))


async def terbitkan_media(context, pending_id, admin_id, query):
    item = DB["pending_media"].get(pending_id)
    if not item:
        await query.message.reply_text("Kiriman sudah tidak ada atau sudah diproses.")
        return
    post_id = cfg().get("post_chat_id")
    if not post_id:
        await query.message.reply_text("Tujuan Post belum diatur. Atur melalui /menu.")
        return
    try:
        copied = await context.bot.copy_message(
            chat_id=post_id,
            from_chat_id=item["source_chat_id"],
            message_id=item["source_message_id"],
        )
        footer = await footer_menfess(context)
        caption_html = escape(item.get("caption") or "")
        caption_with_footer = (caption_html + "\n\n" if caption_html else "") + footer
        if len(caption_with_footer) <= 1024:
            try:
                await context.bot.edit_message_caption(
                    chat_id=post_id, message_id=copied.message_id,
                    caption=caption_with_footer, parse_mode="HTML",
                )
            except TelegramError:
                await context.bot.send_message(
                    chat_id=post_id,
                    text=footer,
                    parse_mode="HTML",
                    reply_to_message_id=copied.message_id,
                    disable_web_page_preview=True,
                )
        else:
            await context.bot.send_message(
                chat_id=post_id,
                text=footer,
                parse_mode="HTML",
                reply_to_message_id=copied.message_id,
                disable_web_page_preview=True,
            )
        chat = await context.bot.get_chat(post_id)
        link = link_post(chat, copied.message_id)
        catat_post_menfess(
            post_id, copied.message_id, int(item["user_id"]),
            item.get("username", ""), item.get("user_name", "User"),
            f"[{item.get('kind', 'Media')}] {item.get('caption', '')}",
            item.get("kind", "Media"),
        )
        DB["pending_media"].pop(pending_id, None)
        data = ambil_data(int(item["user_id"]))
        data["terpakai"] = int(data.get("terpakai", 0)) + 1
        data["terakhir"] = time.time()
        await simpan_data()
        try:
            await context.bot.send_message(
                item["user_id"], "✅ Media kamu disetujui dan berhasil diterbitkan."
            )
        except TelegramError:
            pass
        if link:
            # Media log memakai format log yang sama.
            fake_user = type("UserInfo", (), {
                "id": int(item["user_id"]), "username": item.get("username", ""),
                "first_name": item.get("user_name", "User"),
            })()
            await kirim_log(context, fake_user, f"[{item['kind']}] {item.get('caption', '')}", link)
        await query.edit_message_text(f"✅ Media #{pending_id} disetujui dan diterbitkan.")
    except TelegramError as exc:
        await query.message.reply_text(f"Gagal menerbitkan media. Periksa izin bot pada tujuan Post.\n{exc}")


async def media_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id not in ADMIN_IDS:
        await query.answer("Khusus admin.", show_alert=True)
        return
    await query.answer()
    parts = query.data.split(":")
    action = parts[1]
    if action == "queue":
        await tampilkan_antrean(query)
        return
    pending_id = parts[2]
    item = DB["pending_media"].get(pending_id)
    if not item:
        await query.message.reply_text("Kiriman sudah diproses atau tidak tersedia.")
        return
    if action == "review":
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Setujui & Post", callback_data=f"media:approve:{pending_id}"),
            InlineKeyboardButton("❌ Tolak", callback_data=f"media:reject:{pending_id}"),
        ], [InlineKeyboardButton("⬅️ Antrean", callback_data="media:queue")]])
        await query.message.reply_text(
            f"🖼️ <b>PEMERIKSAAN MEDIA</b>\nID: <code>{pending_id}</code>\n"
            f"Pengirim: {escape(item['user_name'])}\nJenis: {item['kind']}\n"
            f"Caption: {escape(item.get('caption') or '-')}",
            parse_mode="HTML", reply_markup=keyboard,
        )
        try:
            await context.bot.copy_message(
                chat_id=query.from_user.id,
                from_chat_id=item["source_chat_id"],
                message_id=item["source_message_id"],
            )
        except TelegramError as exc:
            await query.message.reply_text(f"Tidak bisa menampilkan media: {exc}")
    elif action == "approve":
        await terbitkan_media(context, pending_id, query.from_user.id, query)
    elif action == "reject":
        DB["pending_media"].pop(pending_id, None)
        await simpan_data()
        try:
            await context.bot.send_message(item["user_id"], "❌ Maaf, media kamu ditolak admin.")
        except TelegramError:
            pass
        await query.edit_message_text(f"❌ Media #{pending_id} ditolak dan tidak diterbitkan.")
    elif action == "queue":
        await tampilkan_antrean(query)


async def tampilkan_antrean(query):
    pending = DB["pending_media"]
    if not pending:
        await query.edit_message_text("🖼️ Tidak ada media yang menunggu.", reply_markup=tombol_kembali())
        return
    rows = []
    for pid, item in list(pending.items())[:30]:
        rows.append([InlineKeyboardButton(
            f"{item.get('kind', 'Media')} · {item.get('user_name', 'User')[:20]} · #{pid[-5:]}",
            callback_data=f"media:review:{pid}",
        )])
    rows.append([InlineKeyboardButton("⬅️ Kembali", callback_data="panel:menu")])
    await query.edit_message_text("🖼️ <b>ANTREAN MEDIA</b>\nPilih kiriman untuk diperiksa:", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows))


# ==================== TEXT MENFESS ====================
async def kirim_menfess(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message, user = update.effective_message, update.effective_user
    if not message or not user or not message.text:
        return
    catat_user(user)
    if not cfg().get("menfess_open", True):
        await message.reply_text("🔒 Penerimaan menfess sedang ditutup sementara oleh admin.")
        return
    if not await cek_force_sub(context, user.id):
        await tampilkan_force_sub(message)
        return
    if str(user.id) in DB["ban"]:
        await message.reply_text("🚫 Kamu di-ban dan tidak bisa mengirim menfess.")
        return
    isi = message.text.strip()
    if not hashtag_valid(isi):
        await message.reply_text(teks_hashtag_tidak_valid(), parse_mode="HTML")
        return
    blocked = kata_terlarang(isi)
    if blocked:
        await message.reply_text("⛔ Pesan ditolak karena mengandung kata/frasa yang masuk blacklist.")
        return
    data = ambil_data(user.id)
    quota = int(cfg()["quota"])
    if int(data.get("terpakai", 0)) >= quota:
        await message.reply_text(f"❌ Kuota hari ini sudah habis ({quota}/{quota}).")
        return
    wait = int(cfg()["delay"]) - (time.time() - float(data.get("terakhir", 0)))
    if wait > 0:
        await message.reply_text(f"⏳ Tunggu {int(wait) + 1} detik lagi sebelum kirim pesan berikutnya.")
        return
    post_id = cfg().get("post_chat_id")
    if not post_id:
        await message.reply_text("Tujuan Post belum diatur oleh admin. Silakan coba lagi nanti.")
        return
    try:
        footer = await footer_menfess(context)
        post_text = f"{escape(isi)}\n\n{footer}"
        if len(post_text) <= 4096:
            sent = await context.bot.send_message(
                chat_id=post_id,
                text=post_text,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        else:
            sent = await context.bot.send_message(
                chat_id=post_id,
                text=escape(isi),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            await context.bot.send_message(
                chat_id=post_id,
                text=footer,
                parse_mode="HTML",
                reply_to_message_id=sent.message_id,
                disable_web_page_preview=True,
            )
        chat = await context.bot.get_chat(post_id)
    except TelegramError:
        await message.reply_text("❌ Gagal menerbitkan menfess. Admin perlu memeriksa tujuan Post dan izin bot.")
        return
    data["terpakai"] = int(data.get("terpakai", 0)) + 1
    data["terakhir"] = time.time()
    await simpan_data()
    link = link_post(chat, sent.message_id)
    catat_post_menfess(
        post_id, sent.message_id, user.id, user.username or "",
        user.full_name or user.first_name or "User", isi, "Teks",
    )
    await simpan_data()
    await kirim_log(context, user, isi, link or "https://t.me/")
    buttons = InlineKeyboardMarkup([[InlineKeyboardButton("🔗 Lihat postingan", url=link)]]) if link else None
    remaining = max(0, quota - int(data["terpakai"]))
    await message.reply_text(
        f"✅ <b>Menfess berhasil dikirim!</b>\n\n📦 Sisa kuota: {remaining}/{quota}",
        parse_mode="HTML", reply_markup=buttons,
    )


# ==================== BAN / UNBAN ====================
def cari_target(teks):
    teks = teks.strip()
    if teks.isdigit():
        return int(teks)
    nama = teks.lstrip("@").lower()
    if not nama:
        return None
    for table in (DB["users"], DB["ban"]):
        for uid, info in table.items():
            if info.get("username", "").lower() == nama:
                return int(uid)
    return None


def label(uid):
    info = DB["ban"].get(str(uid)) or DB["users"].get(str(uid)) or {}
    if info.get("username"):
        return f"@{info['username']} ({uid})"
    return f"{info.get('nama') or uid} ({uid})"


def lakukan_ban(uid):
    info = DB["users"].get(str(uid), {})
    DB["ban"][str(uid)] = {"username": info.get("username", ""), "nama": info.get("nama", "")}


async def ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not adalah_admin(update): return
    if not context.args:
        await update.message.reply_text("Format: /ban ID atau /ban @username"); return
    uid = cari_target(context.args[0])
    if uid is None:
        await update.message.reply_text("User tidak ditemukan."); return
    if uid in ADMIN_IDS:
        await update.message.reply_text("Admin tidak bisa di-ban."); return
    lakukan_ban(uid); await simpan_data()
    await update.message.reply_text(f"🚫 {label(uid)} berhasil di-ban.")


async def unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not adalah_admin(update): return
    if not context.args:
        await update.message.reply_text("Format: /unban ID atau /unban @username"); return
    uid = cari_target(context.args[0])
    if uid is None or str(uid) not in DB["ban"]:
        await update.message.reply_text("User tidak sedang di-ban."); return
    name = label(uid); DB["ban"].pop(str(uid), None); await simpan_data()
    await update.message.reply_text(f"♻️ {name} berhasil di-unban.")


async def banlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not adalah_admin(update): return
    if not DB["ban"]:
        await update.message.reply_text("Tidak ada user yang di-ban."); return
    lines = [f"🚫 DAFTAR BAN ({len(DB['ban'])})", ""]
    lines += [f"{i}. {label(uid)}" for i, uid in enumerate(DB["ban"], 1)]
    await update.message.reply_text("\n".join(lines))


async def tombol_ban(update, context):
    query = update.callback_query
    if query.from_user.id not in ADMIN_IDS:
        await query.answer("Khusus admin.", show_alert=True); return
    action, uid = query.data.split(":"); uid = int(uid)
    if action == "ban":
        if uid in ADMIN_IDS:
            await query.answer("Admin tidak bisa di-ban.", show_alert=True); return
        lakukan_ban(uid)
    else:
        DB["ban"].pop(str(uid), None)
    await simpan_data()
    await query.answer("Berhasil diperbarui.")
    try: await query.edit_message_reply_markup(reply_markup=papan_ban(uid))
    except TelegramError: pass


# ==================== BROADCAST / PING ====================
async def kirim_ke_semua(context, isi):
    ok = fail = 0
    for uid in list(DB["users"]):
        try:
            await context.bot.send_message(chat_id=int(uid), text=isi); ok += 1
        except TelegramError:
            fail += 1
        await asyncio.sleep(0.08)
    return ok, fail


async def broadcast(update, context):
    if not adalah_admin(update): return
    isi = update.message.text.partition(" ")[2].strip()
    if not isi:
        await update.message.reply_text("Format: /broadcast isi pesan"); return
    await update.message.reply_text(f"📢 Mengirim ke {len(DB['users'])} pengguna...")
    ok, fail = await kirim_ke_semua(context, isi)
    await update.message.reply_text(f"✅ Selesai.\nTerkirim: {ok}\nGagal: {fail}")


def uptime_teks():
    total = int(time.time() - waktu_mulai)
    d, rem = divmod(total, 86400); h, rem = divmod(rem, 3600); m, s = divmod(rem, 60)
    return f"{d} hari {h} jam {m} menit {s} detik"


async def ping(update, context):
    start_time = time.time()
    msg = await update.message.reply_text("🏓 Pong...")
    ms = int((time.time() - start_time) * 1000)
    await msg.edit_text(f"🏓 DMG Menfess Bot aktif ✅\n⚡ Respon: {ms} ms\n⏱️ Aktif: {uptime_teks()}")


# ==================== SETTINGS / PANEL ====================
def tombol_kembali(target="panel:menu"):
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Kembali", callback_data=target)]])


def tombol_settings():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔒 Auto Close", callback_data="setting:menfess_toggle")],
        [InlineKeyboardButton("📢 Channel Post", callback_data="setting:post")],
        [InlineKeyboardButton("📜 Logs", callback_data="setting:logs")],
        [InlineKeyboardButton("🛡️ Filter", callback_data="setting:filter")],
        [InlineKeyboardButton("📖 Panduan", callback_data="setting:guide")],
        [InlineKeyboardButton("⬅️ Kembali ke Menu", callback_data="panel:menu")],
    ])


def teks_panel():
    return (
        "🛠 <b>PANEL ADMIN DMG MENFESS</b>\n\n"
        f"👥 Pengguna: {len(DB['users'])}\n🚫 Di-ban: {len(DB['ban'])}\n"
        f"🖼️ Media menunggu: {len(DB['pending_media'])}\n"
        f"📩 Laporan aktif: {sum(1 for x in DB.get('secret_conversations', {}).values() if x.get('status') == 'open')}\n\n"
        "Pilih menu:"
    )


def tombol_menu_panel():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Statistik", callback_data="panel:stats")],
        [InlineKeyboardButton("👥 Admin", callback_data="panel:admins")],
        [InlineKeyboardButton("📥 Menfess Pending", callback_data="media:queue")],
        [InlineKeyboardButton("📩 Laporan Member", callback_data="panel:reports")],
        [InlineKeyboardButton("⚙️ Settings", callback_data="panel:settings")],
        [InlineKeyboardButton("❌ Tutup Menu", callback_data="panel:tutup")],
    ])


def tombol_submenu_admin():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 Daftar Pengguna", callback_data="panel:users"),
         InlineKeyboardButton("🚫 Daftar Ban", callback_data="panel:banlist")],
        [InlineKeyboardButton("🔨 Ban", callback_data="panel:ban"),
         InlineKeyboardButton("♻️ Unban", callback_data="panel:unban")],
        [InlineKeyboardButton("📢 Broadcast", callback_data="panel:broadcast"),
         InlineKeyboardButton("🏓 Status Bot", callback_data="panel:status")],
        [InlineKeyboardButton("⬅️ Kembali ke Menu", callback_data="panel:menu")],
    ])


def tombol_filter():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚫 Blacklist kata", callback_data="setting:blacklist")],
        [InlineKeyboardButton("🏷️ Hashtag wajib", callback_data="setting:hashtags")],
        [InlineKeyboardButton("🔒 Force Subscribe", callback_data="setting:forcesub")],
        [InlineKeyboardButton("⏱️ Cooldown & Kuota", callback_data="setting:limits")],
        [InlineKeyboardButton("⬅️ Kembali", callback_data="panel:settings")],
    ])


async def panel(update, context):
    if not adalah_admin(update): return
    context.user_data.pop("menunggu", None)
    await update.message.reply_text(teks_panel(), parse_mode="HTML", reply_markup=tombol_menu_panel())


async def batal(update, context):
    if not adalah_admin(update): return
    context.user_data.pop("menunggu", None)
    context.user_data.pop("bc_teks", None)
    context.user_data.pop("secret_reply_to", None)
    context.user_data.pop("secret_reply_id", None)
    await update.message.reply_text("Dibatalkan. Ketik /menu untuk membuka panel.")


async def validasi_tujuan(context, raw):
    raw = raw.strip()
    if raw.startswith("https://t.me/"):
        raw = "@" + raw.rstrip("/").split("/")[-1]
    chat = await context.bot.get_chat(raw)
    if chat.type not in (ChatType.CHANNEL, ChatType.GROUP, ChatType.SUPERGROUP):
        raise ValueError("Tujuan harus Channel atau Group.")
    me = await context.bot.get_me()
    member = await context.bot.get_chat_member(chat.id, me.id)
    status = member.status
    if chat.type == ChatType.CHANNEL and status not in ("administrator", "creator"):
        raise ValueError("Bot harus menjadi admin Channel dan memiliki izin posting.")
    if chat.type in (ChatType.GROUP, ChatType.SUPERGROUP) and status in ("left", "kicked"):
        raise ValueError("Bot belum bergabung ke Group.")
    return chat


async def tampilkan_banlist(query):
    if not DB["ban"]:
        await query.edit_message_text("Tidak ada user yang di-ban.", reply_markup=tombol_kembali()); return
    lines = [f"🚫 DAFTAR BAN ({len(DB['ban'])})", ""]
    buttons = []
    for i, uid in enumerate(list(DB["ban"])[:20], 1):
        lines.append(f"{i}. {label(uid)}")
        buttons.append([InlineKeyboardButton(f"♻️ Unban {i}", callback_data=f"panel:unb:{uid}")])
    buttons.append([InlineKeyboardButton("⬅️ Kembali", callback_data="panel:menu")])
    await query.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons))


async def panel_callback(update, context):
    query = update.callback_query
    if query.from_user.id not in ADMIN_IDS:
        await query.answer("Khusus admin.", show_alert=True); return
    await query.answer()
    action = query.data.split(":", 1)[1]
    if action == "bc_ya":
        text = context.user_data.pop("bc_teks", None)
        if not text:
            await query.edit_message_text("Tidak ada pesan broadcast.", reply_markup=tombol_kembali())
            return
        await query.edit_message_text("📢 Broadcast sedang dikirim...")
        ok, fail = await kirim_ke_semua(context, text)
        await query.edit_message_text(f"✅ Broadcast selesai.\nTerkirim: {ok}\nGagal: {fail}", reply_markup=tombol_kembali())
    elif action == "bc_batal":
        context.user_data.pop("bc_teks", None)
        await query.edit_message_text("Broadcast dibatalkan.", reply_markup=tombol_kembali())
    elif action == "menu":
        context.user_data.pop("menunggu", None)
        await query.edit_message_text(teks_panel(), parse_mode="HTML", reply_markup=tombol_menu_panel())
    elif action == "settings":
        c = cfg()
        await query.edit_message_text(
            "⚙️ <b>SETTINGS BOT</b>\n\n"
            f"📢 Post: {escape(str(c.get('post_name') or c.get('post_chat_id') or 'Belum diatur'))}\n"
            f"🔒 Auto Close: {'Aktif' if not c.get('menfess_open', True) else 'Nonaktif'}\n"
            f"📜 Logs: {escape(str(c.get('log_chat_id') or 'Belum diatur'))}\n"
            f"🛡️ Filter: {'Aktif' if c.get('blacklist_enabled') else 'Nonaktif'}\n"
            f"📖 Bagian panduan: {len(c.get('guide_sections', []))}",
            parse_mode="HTML", reply_markup=tombol_settings(),
        )
    elif action == "stats":
        c = cfg()
        await query.edit_message_text(
            "📊 <b>STATISTIK BOT</b>\n\n"
            f"👥 Total pengguna: {len(DB['users'])}\n🚫 User diban: {len(DB['ban'])}\n"
            f"📥 Media pending: {len(DB['pending_media'])}\n"
            f"📩 Laporan aktif: {sum(1 for x in DB.get('secret_conversations', {}).values() if x.get('status') == 'open')}\n"
            f"⏱️ Uptime: {uptime_teks()}\n📦 Kuota: {c['quota']}/hari",
            parse_mode="HTML", reply_markup=tombol_kembali(),
        )
    elif action == "admins":
        lines = ["👥 <b>ADMIN BOT</b>", ""]
        lines.extend(f"• <code>{uid}</code>" for uid in ADMIN_IDS)
        await query.edit_message_text("\n".join(lines), parse_mode="HTML", reply_markup=tombol_submenu_admin())
    elif action == "reports":
        active = [(uid, item) for uid, item in DB.get("secret_conversations", {}).items() if item.get("status") == "open"]
        rows = []
        lines = [f"📩 <b>LAPORAN MEMBER ({len(active)})</b>", ""]
        for uid, item in active[:25]:
            name = escape(str(item.get("nama") or "Member"))
            lines.append(f"• {name} — ID <code>{uid}</code>")
            rows.append([InlineKeyboardButton(f"Buka laporan {uid}", callback_data=f"panel:report:{uid}")])
        if not active:
            lines.append("Belum ada laporan aktif.")
        rows.append([InlineKeyboardButton("⬅️ Kembali ke Menu", callback_data="panel:menu")])
        await query.edit_message_text("\n".join(lines), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows))
    elif action.startswith("report:"):
        uid = action.split(":", 1)[1]
        item = DB.get("secret_conversations", {}).get(uid)
        if not item or item.get("status") != "open":
            await query.edit_message_text("Laporan tidak tersedia atau sudah ditutup.", reply_markup=tombol_kembali("panel:reports")); return
        context.user_data["menunggu"] = "secret_reply"
        context.user_data["secret_reply_to"] = uid
        context.user_data["secret_reply_id"] = item.get("id")
        await query.edit_message_text(
            f"📩 <b>LAPORAN MEMBER</b>\n\nNama: {escape(str(item.get('nama') or 'Member'))}\n"
            f"Username: @{escape(str(item.get('username') or '-'))}\nTelegram ID: <code>{uid}</code>\n\n"
            "Kirim balasan sebagai pesan berikutnya. Ketik /batal untuk membatalkan.",
            parse_mode="HTML", reply_markup=tombol_kembali("panel:reports"),
        )
    elif action == "users":
        lines = [f"👥 Total pengguna: {len(DB['users'])}", ""]
        for uid in list(DB["users"])[-15:][::-1]:
            lines.append("• " + label(uid))
        await query.edit_message_text("\n".join(lines), reply_markup=tombol_submenu_admin())
    elif action == "banlist":
        await tampilkan_banlist(query)
    elif action in ("ban", "unban"):
        context.user_data["menunggu"] = action
        await query.edit_message_text(
            f"Kirim ID atau @username untuk {action}.\nKetik /batal untuk membatalkan.",
            reply_markup=tombol_kembali("panel:admins"),
        )
    elif action == "broadcast":
        context.user_data["menunggu"] = "broadcast"
        await query.edit_message_text("Kirim teks broadcast.\nKetik /batal untuk membatalkan.", reply_markup=tombol_kembali("panel:admins"))
    elif action == "status":
        c = cfg()
        await query.edit_message_text(
            f"🏓 BOT AKTIF\n\n⏱️ Uptime: {uptime_teks()}\n"
            f"👥 Pengguna: {len(DB['users'])}\n🚫 Ban: {len(DB['ban'])}\n"
            f"🖼️ Antrean media: {len(DB['pending_media'])}\n"
            f"Cooldown: {c['delay']} detik\nKuota: {c['quota']}/hari",
            reply_markup=tombol_submenu_admin(),
        )
    elif action.startswith("unb:"):
        uid = action.split(":")[1]
        DB["ban"].pop(uid, None); await simpan_data(); await tampilkan_banlist(query)
    elif action == "tutup":
        await query.message.delete()


async def settings_callback_refresh_fsub(query):
    tujuan = cfg().get("force_subs", [])
    lines = [f"🔒 <b>FORCE SUBSCRIBE ({len(tujuan)}/3)</b>", ""]
    rows = []
    for i, item in enumerate(tujuan):
        status = "Aktif" if item.get("enabled", True) else "Nonaktif"
        lines.append(f"{i+1}. {escape(str(item.get('title') or item.get('chat_id')))} — {status}")
        rows.append([
            InlineKeyboardButton(("🟢 " if item.get("enabled", True) else "🔴 ") + f"Toggle {i+1}",
                                 callback_data=f"setting:fsub_toggle:{i}"),
            InlineKeyboardButton(f"🗑 Hapus {i+1}", callback_data=f"setting:fsub_remove:{i}")
        ])
    if len(tujuan) < 3:
        rows.append([InlineKeyboardButton("➕ Tambah tujuan", callback_data="setting:fsub_add")])
    rows.append([InlineKeyboardButton("⬅️ Kembali", callback_data="setting:filter")])
    await query.edit_message_text(
        "\n".join(lines) + "\n\nPengguna wajib bergabung ke semua tujuan yang aktif.",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows)
    )


async def settings_callback(update, context):
    query = update.callback_query
    if query.from_user.id not in ADMIN_IDS:
        await query.answer("Khusus admin.", show_alert=True); return
    await query.answer()
    action = query.data.split(":", 1)[1]
    if action == "guide":
        await query.edit_message_text(teks_panduan(), parse_mode="HTML", reply_markup=keyboard_panduan_admin(), disable_web_page_preview=True)
    elif action == "guide_add":
        context.user_data["menunggu"] = "guide_add"
        await query.message.reply_text("Kirim isi baru dengan format: Judul | Isi panduan\nContoh: Aturan | Gunakan bahasa yang sopan.\nKetik /batal untuk membatalkan.")
    elif action in ("guide_edit_list", "guide_delete_list"):
        sections = cfg().get("guide_sections", [])
        rows = []
        for i, item in enumerate(sections):
            label_text = str(item.get("title") or f"Bagian {i+1}")[:40]
            verb = "Edit" if action == "guide_edit_list" else "Hapus"
            rows.append([InlineKeyboardButton(f"{verb}: {label_text}", callback_data=f"setting:guide_{'edit' if action == 'guide_edit_list' else 'delete'}:{i}")])
        rows.append([InlineKeyboardButton("⬅️ Kembali", callback_data="setting:guide")])
        await query.edit_message_text("Pilih bagian panduan:", reply_markup=InlineKeyboardMarkup(rows))
    elif action.startswith("guide_edit:"):
        index = int(action.split(":")[1])
        sections = cfg().get("guide_sections", [])
        if not 0 <= index < len(sections):
            await query.edit_message_text("Bagian panduan tidak ditemukan.", reply_markup=keyboard_panduan_admin()); return
        context.user_data["menunggu"] = f"guide_edit_{index}"
        await query.message.reply_text("Kirim judul dan isi baru dengan format: Judul | Isi panduan\nKetik /batal untuk membatalkan.")
    elif action.startswith("guide_delete:"):
        index = int(action.split(":")[1])
        sections = cfg().get("guide_sections", [])
        if 0 <= index < len(sections):
            removed = sections.pop(index)
            await simpan_data()
            await query.edit_message_text(f"🗑️ Bagian '{escape(str(removed.get('title') or 'Panduan'))}' dihapus.\n\n" + teks_panduan(), parse_mode="HTML", reply_markup=keyboard_panduan_admin())
        else:
            await query.edit_message_text("Bagian panduan tidak ditemukan.", reply_markup=keyboard_panduan_admin())
    elif action == "filter":
        await query.edit_message_text("🛡️ <b>PENGATURAN FILTER</b>\nPilih pengaturan:", parse_mode="HTML", reply_markup=tombol_filter())
    elif action == "limits":
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("⏱️ Atur Cooldown", callback_data="setting:delay")],
            [InlineKeyboardButton("📦 Atur Kuota Harian", callback_data="setting:quota")],
            [InlineKeyboardButton("⬅️ Kembali", callback_data="setting:filter")],
        ])
        await query.edit_message_text(f"⏱️ Cooldown: {cfg()['delay']} detik\n📦 Kuota: {cfg()['quota']} per hari", reply_markup=keyboard)
    elif action == "menfess_toggle":
        cfg()["menfess_open"] = not cfg().get("menfess_open", True)
        await simpan_data()
        c = cfg()
        await query.edit_message_text(
            "⚙️ <b>SETTINGS BOT</b>\n\n"
            f"📢 Post: {escape(str(c.get('post_name') or c.get('post_chat_id') or 'Belum diatur'))}\n"
            f"🔒 Auto Close: {'Penerimaan ditutup' if not c.get('menfess_open', True) else 'Penerimaan dibuka'}\n"
            f"📜 Logs: {escape(str(c.get('log_chat_id') or 'Belum diatur'))}\n"
            f"🛡️ Filter: {'Aktif' if c['blacklist_enabled'] else 'Nonaktif'}\n"
            f"📖 Bagian panduan: {len(c.get('guide_sections', []))}",
            parse_mode="HTML", reply_markup=tombol_settings(),
        )
    elif action in ("post", "logs", "delay", "quota", "blacklist_add", "blacklist_remove",
                    "hashtag_add", "hashtag_remove", "fsub_add"):
        context.user_data["menunggu"] = "setting_" + action
        prompts = {
            "post": "Kirim ID atau username Channel/Group untuk tujuan Channel Post.\nContoh: -1001234567890 atau @username",
            "logs": "Kirim ID atau username Channel/Group untuk tujuan Logs.\nContoh: -1001234567890 atau @username",
            "delay": "Kirim cooldown dalam detik. Contoh: 60",
            "quota": "Kirim kuota menfess per hari. Contoh: 100",
            "blacklist_add": "Kirim kata/frasa yang ingin ditambahkan ke blacklist.",
            "blacklist_remove": "Kirim kata/frasa yang ingin dihapus dari blacklist.",
            "hashtag_add": "Kirim hashtag yang ingin ditambahkan. Contoh: #fwbgirl",
            "hashtag_remove": "Kirim hashtag yang ingin dihapus. Contoh: #fwbgirl",
            "fsub_add": (
                "Kirim ID angka Channel/Group yang ingin dijadikan Force Subscribe.\n"
                "Contoh: -1001234567890\n\n"
                "Bot akan mengambil nama tujuan dan membuat link undangan otomatis.\n"
                "Pastikan bot sudah menjadi admin dan memiliki izin Invite Users / Undang Pengguna."
            ),
        }
        await query.message.reply_text(prompts[action] + "\nKetik /batal untuk membatalkan.")
    elif action == "hashtags":
        tags = cfg().get("hashtags", [])
        display = "\n".join("• #" + escape(str(x).lstrip("#")) for x in tags) if tags else "Belum ada hashtag."
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Tambah hashtag", callback_data="setting:hashtag_add")],
            [InlineKeyboardButton("➖ Hapus hashtag", callback_data="setting:hashtag_remove")],
            [InlineKeyboardButton("⬅️ Kembali", callback_data="setting:filter")],
        ])
        await query.edit_message_text(
            "🏷️ <b>HASHTAG WAJIB</b>\n\nPesan harus diawali salah satu hashtag terdaftar.\n\n" + display,
            parse_mode="HTML", reply_markup=keyboard
        )
    elif action == "forcesub":
        await settings_callback_refresh_fsub(query)
    elif action.startswith("fsub_toggle:"):
        index = int(action.split(":")[1])
        tujuan = cfg().get("force_subs", [])
        if 0 <= index < len(tujuan):
            tujuan[index]["enabled"] = not tujuan[index].get("enabled", True)
            await simpan_data()
        await settings_callback_refresh_fsub(query)
    elif action.startswith("fsub_remove:"):
        index = int(action.split(":")[1])
        tujuan = cfg().get("force_subs", [])
        if 0 <= index < len(tujuan):
            tujuan.pop(index)
            await simpan_data()
        await settings_callback_refresh_fsub(query)
    elif action == "blacklist":
        words = cfg().get("blacklist", [])
        display = "\n".join("• " + escape(w) for w in words) if words else "Belum ada kata terlarang."
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Tambah kata", callback_data="setting:blacklist_add")],
            [InlineKeyboardButton("➖ Hapus kata", callback_data="setting:blacklist_remove")],
            [InlineKeyboardButton(
                "🔴 Nonaktifkan" if cfg()["blacklist_enabled"] else "🟢 Aktifkan",
                callback_data="setting:blacklist_toggle",
            )],
            [InlineKeyboardButton("⬅️ Kembali", callback_data="setting:filter")],
        ])
        await query.edit_message_text(
            f"🚫 <b>BLACKLIST</b>\nStatus: {'Aktif' if cfg()['blacklist_enabled'] else 'Nonaktif'}\n\n{display}",
            parse_mode="HTML", reply_markup=keyboard,
        )
    elif action == "blacklist_toggle":
        cfg()["blacklist_enabled"] = not cfg()["blacklist_enabled"]
        await simpan_data()
        words = cfg().get("blacklist", [])
        display = "\n".join("• " + escape(w) for w in words) if words else "Belum ada kata terlarang."
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Tambah kata", callback_data="setting:blacklist_add")],
            [InlineKeyboardButton("➖ Hapus kata", callback_data="setting:blacklist_remove")],
            [InlineKeyboardButton("🔴 Nonaktifkan" if cfg()["blacklist_enabled"] else "🟢 Aktifkan", callback_data="setting:blacklist_toggle")],
            [InlineKeyboardButton("⬅️ Kembali", callback_data="setting:filter")],
        ])
        await query.edit_message_text(
            f"🚫 <b>BLACKLIST</b>\nStatus: {'Aktif' if cfg()['blacklist_enabled'] else 'Nonaktif'}\n\n{display}",
            parse_mode="HTML", reply_markup=keyboard,
        )


async def input_admin(update, context):
    if not update.effective_user or update.effective_user.id not in ADMIN_IDS or not update.message:
        return
    mode = context.user_data.get("menunggu")
    if not mode:
        return
    value = update.message.text.strip()
    if mode == "guide_add" or mode.startswith("guide_edit_"):
        if "|" not in value:
            await update.message.reply_text("Format belum benar. Gunakan: Judul | Isi panduan")
            raise ApplicationHandlerStop
        title, content = [part.strip() for part in value.split("|", 1)]
        if not title or not content:
            await update.message.reply_text("Judul dan isi panduan tidak boleh kosong.")
            raise ApplicationHandlerStop
        if len(teks_panduan()) + len(title) + len(content) > 3900:
            await update.message.reply_text("Panduan terlalu panjang untuk ditampilkan dalam satu pesan Telegram.")
            raise ApplicationHandlerStop
        sections = cfg().setdefault("guide_sections", [])
        if mode == "guide_add":
            sections.append({"title": title, "content": content})
            result = "✅ Bagian panduan berhasil ditambahkan."
        else:
            index = int(mode.rsplit("_", 1)[1])
            if not 0 <= index < len(sections):
                context.user_data.pop("menunggu", None)
                await update.message.reply_text("Bagian panduan tidak ditemukan.")
                raise ApplicationHandlerStop
            sections[index] = {"title": title, "content": content}
            result = "✅ Bagian panduan berhasil diperbarui."
        await simpan_data()
        context.user_data.pop("menunggu", None)
        await update.message.reply_text(result + "\n\n" + teks_panduan(), parse_mode="HTML", reply_markup=keyboard_panduan_admin())
        raise ApplicationHandlerStop
    if mode == "secret_reply":
        user_id = context.user_data.pop("secret_reply_to", None)
        conversation_id = context.user_data.pop("secret_reply_id", None)
        context.user_data.pop("menunggu", None)
        item = DB["secret_conversations"].get(str(user_id)) if user_id else None
        if not value:
            await update.message.reply_text("Balasan tidak boleh kosong.")
        elif not item or item.get("id") != conversation_id or item.get("status") != "open":
            await update.message.reply_text("Percakapan sudah ditutup atau tidak tersedia.")
        else:
            try:
                await context.bot.send_message(
                    chat_id=int(user_id),
                    text=value,
                )
                await update.message.reply_text(
                    f"✅ Balasan berhasil dikirim ke pengguna ID <code>{user_id}</code>.",
                    parse_mode="HTML",
                )
            except TelegramError:
                await update.message.reply_text("⚠️ Balasan gagal dikirim. Pengguna mungkin belum membuka chat bot.")
        raise ApplicationHandlerStop
    if mode == "broadcast":
        context.user_data.pop("menunggu", None)
        context.user_data["bc_teks"] = value
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Kirim", callback_data="panel:bc_ya"),
            InlineKeyboardButton("❌ Batal", callback_data="panel:bc_batal"),
        ]])
        await update.message.reply_text(
            f"Pesan akan dikirim ke {len(DB['users'])} pengguna:\n\n{value}\n\nKirim sekarang?",
            reply_markup=keyboard,
        )
        raise ApplicationHandlerStop
    if mode in ("ban", "unban"):
        context.user_data.pop("menunggu", None)
        uid = cari_target(value)
        if uid is None:
            await update.message.reply_text("User tidak ditemukan.")
        elif mode == "ban":
            if uid in ADMIN_IDS:
                await update.message.reply_text("Admin tidak bisa di-ban.")
            else:
                lakukan_ban(uid); await simpan_data()
                await update.message.reply_text(f"🚫 {label(uid)} berhasil di-ban.")
        else:
            if str(uid) not in DB["ban"]:
                await update.message.reply_text("User tidak sedang di-ban.")
            else:
                name = label(uid); DB["ban"].pop(str(uid)); await simpan_data()
                await update.message.reply_text(f"♻️ {name} berhasil di-unban.")
        raise ApplicationHandlerStop
    if mode.startswith("setting_"):
        setting = mode[len("setting_"):]
        try:
            if setting in ("post", "logs"):
                chat = await validasi_tujuan(context, value)
                if setting == "post":
                    cfg()["post_chat_id"] = chat.id
                    # Simpan judul dan username secara terpisah.
                    # post_name tetap diisi judul untuk kompatibilitas fitur lama.
                    cfg()["post_title"] = chat.title or chat.username or str(chat.id)
                    cfg()["post_name"] = chat.title or chat.username or str(chat.id)
                    cfg()["post_username"] = chat.username or ""
                    label_setting = "Post"
                else:
                    cfg()["log_chat_id"] = chat.id
                    label_setting = "Logs"
                result = f"✅ Tujuan {label_setting} berhasil diubah ke {chat.title or chat.username or chat.id}\nID: {chat.id}"
            elif setting == "delay":
                number = int(value)
                if number < 0 or number > 86400:
                    raise ValueError("Cooldown harus antara 0 sampai 86400 detik.")
                cfg()["delay"] = number
                result = f"✅ Cooldown diubah menjadi {number} detik."
            elif setting == "quota":
                number = int(value)
                if number < 1 or number > 100000:
                    raise ValueError("Kuota harus antara 1 sampai 100000.")
                cfg()["quota"] = number
                result = f"✅ Kuota harian diubah menjadi {number}."
            elif setting == "blacklist_add":
                if not value:
                    raise ValueError("Kata tidak boleh kosong.")
                words = cfg().setdefault("blacklist", [])
                if any(normalize_word(x) == normalize_word(value) for x in words):
                    raise ValueError("Kata tersebut sudah ada.")
                words.append(value)
                result = f"✅ Kata/frasa '{value}' ditambahkan ke blacklist."
            elif setting == "blacklist_remove":
                words = cfg().setdefault("blacklist", [])
                match = next((x for x in words if normalize_word(x) == normalize_word(value)), None)
                if match is None:
                    raise ValueError("Kata tersebut tidak ditemukan.")
                words.remove(match)
                result = f"✅ Kata/frasa '{match}' dihapus dari blacklist."
            elif setting == "hashtag_add":
                tag = value.strip().lstrip("#").lower()
                if not re.fullmatch(r"[a-z0-9_]{1,32}", tag):
                    raise ValueError("Hashtag tidak valid. Gunakan huruf, angka, atau underscore.")
                tags = cfg().setdefault("hashtags", [])
                if tag in tags:
                    raise ValueError("Hashtag tersebut sudah terdaftar.")
                tags.append(tag)
                result = f"✅ Hashtag #{tag} berhasil ditambahkan."
            elif setting == "hashtag_remove":
                tag = value.strip().lstrip("#").lower()
                tags = cfg().setdefault("hashtags", [])
                if tag not in tags:
                    raise ValueError("Hashtag tidak ditemukan.")
                tags.remove(tag)
                result = f"✅ Hashtag #{tag} berhasil dihapus."
            elif setting == "fsub_add":
                tujuan = cfg().setdefault("force_subs", [])
                if len(tujuan) >= 3:
                    raise ValueError("Maksimal 3 tujuan Force Subscribe.")
                if not re.fullmatch(r"-?\d+", value):
                    raise ValueError("Masukkan ID chat berupa angka saja. Contoh: -1001234567890")
                chat = await context.bot.get_chat(int(value))
                if chat.type not in (ChatType.CHANNEL, ChatType.GROUP, ChatType.SUPERGROUP):
                    raise ValueError("ID tersebut bukan ID grup atau channel.")
                if any(str(x.get("chat_id")) == str(chat.id) for x in tujuan):
                    raise ValueError("Tujuan tersebut sudah terdaftar.")
                me = await context.bot.get_me()
                bot_member = await context.bot.get_chat_member(chat.id, me.id)
                if bot_member.status not in ("administrator", "creator"):
                    raise ValueError("Bot harus menjadi admin di grup/channel.")
                if bot_member.status == "administrator" and not getattr(bot_member, "can_invite_users", False):
                    raise ValueError("Bot harus diberi izin Invite Users / Undang Pengguna.")
                try:
                    invite = await context.bot.create_chat_invite_link(
                        chat_id=chat.id,
                        name="DMG Menfess Force Subscribe",
                    )
                except TelegramError as exc:
                    raise ValueError(
                        "Bot gagal membuat link undangan. Pastikan bot admin dan memiliki izin Invite Users / Undang Pengguna."
                    ) from exc
                title = chat.title or chat.username or str(chat.id)
                tujuan.append({
                    "chat_id": chat.id,
                    "title": title,
                    "link": invite.invite_link,
                    "enabled": True,
                })
                result = f"✅ Force Subscribe ditambahkan: {title}\nLink undangan berhasil dibuat otomatis."
            else:
                result = "Pengaturan tidak dikenal."
            await simpan_data()
            context.user_data.pop("menunggu", None)
            await update.message.reply_text(result, reply_markup=tombol_kembali("setting:filter" if setting in ("delay", "quota", "blacklist_add", "blacklist_remove", "hashtag_add", "hashtag_remove", "fsub_add") else "panel:settings"))
        except (ValueError, TelegramError) as exc:
            await update.message.reply_text(f"⚠️ {exc}\nCoba lagi atau /batal.")
        raise ApplicationHandlerStop


async def tombol_panel_lanjutan(update, context):
    query = update.callback_query
    if query.from_user.id not in ADMIN_IDS:
        await query.answer("Khusus admin.", show_alert=True); return
    action = query.data.split(":", 1)[1]
    if action == "bc_ya":
        await query.answer()
        text = context.user_data.pop("bc_teks", None)
        if not text:
            await query.message.reply_text("Tidak ada pesan broadcast."); return
        await query.edit_message_text("📢 Broadcast sedang dikirim...")
        ok, fail = await kirim_ke_semua(context, text)
        await query.edit_message_text(f"✅ Broadcast selesai.\nTerkirim: {ok}\nGagal: {fail}", reply_markup=tombol_kembali())
    elif action == "bc_batal":
        await query.answer("Dibatalkan.")
        context.user_data.pop("bc_teks", None)
        await query.edit_message_text("Broadcast dibatalkan.", reply_markup=tombol_kembali())


# ==================== BALAS LAPORAN MEMBER DENGAN REPLY TELEGRAM ====================
async def balas_pesan_rahasia_reply(update, context):
    message = update.effective_message
    user = update.effective_user
    if not message or not user or user.id not in ADMIN_IDS or not message.reply_to_message:
        return False

    original = message.reply_to_message
    bot_user = await context.bot.get_me()
    if not original.from_user or original.from_user.id != bot_user.id or not original.text:
        return False
    if "LAPORAN MEMBER BARU" not in original.text:
        return False

    id_match = re.search(r"Telegram ID:\s*(?:<code>)?(\d+)(?:</code>)?", original.text)
    conversation_match = re.search(r"Percakapan:\s*(?:<code>)?(\d+)(?:</code>)?", original.text)
    if not id_match or not conversation_match:
        await message.reply_text("Data percakapan pada pesan yang dibalas tidak terbaca.")
        return True

    target_id = int(id_match.group(1))
    conversation_id = conversation_match.group(1)
    item = DB.get("secret_conversations", {}).get(str(target_id))
    if not item or item.get("id") != conversation_id or item.get("status") != "open":
        await message.reply_text("Percakapan sudah ditutup atau tidak tersedia.")
        return True

    try:
        if message.text:
            await context.bot.send_message(chat_id=target_id, text=message.text)
        else:
            await context.bot.copy_message(
                chat_id=target_id,
                from_chat_id=message.chat_id,
                message_id=message.message_id,
            )
        await message.reply_text("✅ Balasan terkirim.")
    except TelegramError:
        await message.reply_text("⚠️ Balasan gagal dikirim. Pengguna mungkin belum membuka chat bot.")
    return True


# ==================== ROUTER PESAN ====================
async def router_pesan(update, context):
    message, user = update.effective_message, update.effective_user
    if not message or not user:
        return
    if user.id in ADMIN_IDS and await balas_pesan_rahasia_reply(update, context):
        return
    if user.id in ADMIN_IDS and message.text and await input_admin(update, context):
        return
    if await pesan_rahasia_pengguna(update, context):
        return
    if pesan_media(message):
        await proses_media(update, context)
    elif message.text and not message.text.startswith("/"):
        await kirim_menfess(update, context)


async def tangani_error(update, context):
    print("Terjadi error:", context.error)


def main():
    app = (
        Application.builder().token(BOT_TOKEN)
        .connect_timeout(30).read_timeout(30).write_timeout(30).pool_timeout(30)
        .get_updates_connect_timeout(30).get_updates_read_timeout(40)
        .get_updates_write_timeout(30).get_updates_pool_timeout(30).build()
    )
    private = filters.ChatType.PRIVATE
    app.add_handler(CommandHandler("start", start, filters=private))
    app.add_handler(CommandHandler("ban", ban, filters=private))
    app.add_handler(CommandHandler("unban", unban, filters=private))
    app.add_handler(CommandHandler("banlist", banlist, filters=private))
    app.add_handler(CommandHandler("broadcast", broadcast, filters=private))
    app.add_handler(CommandHandler("ping", ping, filters=private))
    app.add_handler(CommandHandler("menu", panel, filters=private))
    app.add_handler(CommandHandler("batal", batal, filters=private))

    app.add_handler(CallbackQueryHandler(ui_callback, pattern=r"^ui:"))
    app.add_handler(CallbackQueryHandler(panel_callback, pattern=r"^panel:"))
    app.add_handler(CallbackQueryHandler(settings_callback, pattern=r"^setting:"))
    app.add_handler(CallbackQueryHandler(media_callback, pattern=r"^media:"))
    app.add_handler(CallbackQueryHandler(tombol_ban, pattern=r"^(ban|unban):\d+$"))
    app.add_handler(CallbackQueryHandler(pesan_rahasia_callback, pattern=r"^secret:"))

    app.add_handler(MessageHandler(private & ~filters.COMMAND, router_pesan))
    app.add_error_handler(tangani_error)
    print("DMG Menfess Bot berjalan...")
    app.run_polling(timeout=20)


if __name__ == "__main__":
    main()
