import asyncio
import json
import os
import time
from datetime import date, datetime, timedelta, timezone
from html import escape
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# Load private .env file from this folder. It is excluded from GitHub by .gitignore.
BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"


def load_env_file():
    if not ENV_FILE.exists():
        return
    for raw_line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_env_file()


def env_int(name, default=None):
    value = os.getenv(name)
    if value is None or value == "":
        if default is None:
            raise RuntimeError(f"Konfigurasi {name} belum diisi di file .env")
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} harus berupa angka.") from exc


def env_int_list(name):
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Konfigurasi {name} belum diisi di file .env")
    try:
        return [int(item.strip()) for item in value.split(",") if item.strip()]
    except ValueError as exc:
        raise RuntimeError(f"{name} harus berupa daftar ID angka dipisahkan koma.") from exc


# ==================== KONFIGURASI RAHASIA ====================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
TARGET_CHAT_ID = env_int("TARGET_CHAT_ID")
TARGET_NAME = os.getenv("TARGET_NAME", "").strip().lstrip("@")
ADMIN_IDS = env_int_list("ADMIN_IDS")
LOG_CHAT_ID = env_int("LOG_CHAT_ID")

KUOTA_HARIAN = env_int("KUOTA_HARIAN", 100)
DELAY = env_int("DELAY", 60)
UTC_OFFSET = env_int("UTC_OFFSET", 7)

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN belum diisi di file .env")
if not TARGET_NAME:
    raise RuntimeError("TARGET_NAME belum diisi di file .env")


# ==================== DATABASE JSON ====================

DATA_FILE = BASE_DIR / "menfess_data.json"


def muat_data():
    data = {}
    if DATA_FILE.exists():
        try:
            with DATA_FILE.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            data = {}

    data.setdefault("ban", {})
    data.setdefault("users", {})
    return data


def simpan_data():
    with DATA_FILE.open("w", encoding="utf-8") as f:
        json.dump(DB, f, ensure_ascii=False, indent=2)


DB = muat_data()
waktu_mulai = time.time()
data_user = {}


def catat_user(user):
    baru = {"username": user.username or "", "nama": user.first_name or ""}
    if DB["users"].get(str(user.id)) != baru:
        DB["users"][str(user.id)] = baru
        simpan_data()


def ambil_data(user_id):
    hari_ini = date.today()
    data = data_user.get(user_id)
    if data is None:
        data = {"tanggal": hari_ini, "terpakai": 0, "terakhir": 0}
        data_user[user_id] = data
    elif data["tanggal"] != hari_ini:
        data["tanggal"] = hari_ini
        data["terpakai"] = 0
    return data


# ==================== WAKTU DAN LOG ====================

BULAN = [
    "Januari", "Februari", "Maret", "April", "Mei", "Juni",
    "Juli", "Agustus", "September", "Oktober", "November", "Desember",
]
NAMA_ZONA = {7: "WIB", 8: "WITA", 9: "WIT"}


def waktu_sekarang():
    sekarang = datetime.now(timezone(timedelta(hours=UTC_OFFSET)))
    zona = NAMA_ZONA.get(UTC_OFFSET, f"UTC{UTC_OFFSET:+d}")
    tanggal = f"{sekarang.day} {BULAN[sekarang.month - 1]} {sekarang.year}"
    jam = sekarang.strftime("%H:%M:%S") + " " + zona
    return tanggal, jam


def papan_ban(uid):
    if str(uid) in DB["ban"]:
        tombol = InlineKeyboardButton("♻️ Unban user", callback_data=f"unban:{uid}")
    else:
        tombol = InlineKeyboardButton("🚫 Ban user", callback_data=f"ban:{uid}")
    return InlineKeyboardMarkup([[tombol]])


async def kirim_log(context, user, isi, link):
    tanggal, jam = waktu_sekarang()
    username = f"@{escape(user.username)}" if user.username else "-"
    nama = escape(user.first_name or "User")
    mention = f'<a href="tg://user?id={user.id}">{nama}</a>'
    teks = (
        "📋 <b>LOG MENFESS</b>\n\n"
        f"👤 Mention: {mention}\n"
        f"🆔 ID: <code>{user.id}</code>\n"
        f"🔗 Username: {username}\n"
        f"📅 Tanggal: {tanggal}\n"
        f"🕒 Waktu: {jam}\n\n"
        f"💬 Isi:\n{escape(isi)}\n\n"
        f'📎 <a href="{link}">Buka postingan</a>'
    )
    try:
        await context.bot.send_message(
            chat_id=LOG_CHAT_ID,
            text=teks,
            parse_mode="HTML",
            reply_markup=papan_ban(user.id),
        )
        print("Log menfess berhasil dikirim.")
    except Exception as error:
        print("Gagal mengirim log:", error)


# ==================== UI PENGGUNA ====================

def keyboard_utama():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("📖 Panduan", callback_data="ui:panduan"),
        InlineKeyboardButton("📢 Channel", url=f"https://t.me/{TARGET_NAME}"),
    ]])


async def ui_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "ui:panduan":
        await query.message.reply_text(
            "📖 <b>PANDUAN MENFESS</b>\n\n"
            "1. Langsung ketik pesan yang ingin kamu kirim di chat bot.\n"
            "2. Bot akan meneruskan pesanmu ke channel.\n"
            "3. Tunggu konfirmasi berhasil.\n\n"
            "⚠️ Jangan mengirim spam atau konten yang melanggar aturan.",
            parse_mode="HTML",
        )


# ==================== PERINTAH PENGGUNA ====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    catat_user(user)
    data = ambil_data(user.id)
    sisa = max(0, KUOTA_HARIAN - data["terpakai"])
    teks = (
        "🖤 <b>DMG MENFESS</b>\n"
        "<i>Anonymous Message System</i>\n\n"
        f"Halo, <b>{escape(user.first_name or 'User')}</b>!\n"
        "Selamat datang di layanan menfess.\n\n"
        f"📦 Kuota: {sisa}/{KUOTA_HARIAN}\n"
        f"⏱️ Delay: {DELAY} detik\n\n"
        "Ketik pesanmu langsung di chat ini.\n"
        "Atau pilih Panduan jika membutuhkan bantuan."
    )
    await update.message.reply_text(
        teks, parse_mode="HTML", reply_markup=keyboard_utama()
    )


async def kirim_menfess(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    isi = update.message.text
    catat_user(user)

    if str(user.id) in DB["ban"]:
        await update.message.reply_text("🚫 Kamu di-ban dan tidak bisa mengirim menfess.")
        return

    data = ambil_data(user.id)
    if data["terpakai"] >= KUOTA_HARIAN:
        await update.message.reply_text(
            f"❌ Kuota hari ini sudah habis ({KUOTA_HARIAN}/{KUOTA_HARIAN}).\n"
            "Coba lagi besok ya."
        )
        return

    sisa_tunggu = DELAY - (time.time() - data["terakhir"])
    if sisa_tunggu > 0:
        await update.message.reply_text(
            f"⏳ Tunggu {int(sisa_tunggu) + 1} detik lagi sebelum kirim pesan berikutnya."
        )
        return

    pesan = await context.bot.send_message(
        chat_id=TARGET_CHAT_ID,
        text=f"🔰 {escape(isi)}",
        parse_mode="HTML",
    )
    data["terpakai"] += 1
    data["terakhir"] = time.time()
    sisa = max(0, KUOTA_HARIAN - data["terpakai"])

    if pesan.chat.username:
        link = f"https://t.me/{pesan.chat.username}/{pesan.message_id}"
    else:
        link = f"https://t.me/c/{str(pesan.chat.id)[4:]}/{pesan.message_id}"

    await kirim_log(context, user, isi, link)
    tombol = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔗 Lihat postingan", url=link)
    ]])
    await update.message.reply_text(
        "✅ <b>Menfess berhasil dikirim!</b>\n\n"
        f"📦 Sisa kuota: {sisa}/{KUOTA_HARIAN}",
        parse_mode="HTML",
        reply_markup=tombol,
    )


# ==================== ADMIN: BAN / UNBAN ====================

def adalah_admin(update: Update):
    return update.effective_user.id in ADMIN_IDS


def cari_target(teks):
    teks = teks.strip()
    if teks.isdigit():
        return int(teks)
    nama = teks.lstrip("@").lower()
    if not nama:
        return None
    for tabel in (DB["users"], DB["ban"]):
        for uid, info in tabel.items():
            if info.get("username", "").lower() == nama:
                return int(uid)
    return None


def label(uid):
    info = DB["ban"].get(str(uid)) or DB["users"].get(str(uid)) or {}
    if info.get("username"):
        return f"@{info['username']} ({uid})"
    if info.get("nama"):
        return f"{info['nama']} ({uid})"
    return str(uid)


def lakukan_ban(uid):
    info = DB["users"].get(str(uid), {})
    DB["ban"][str(uid)] = {
        "username": info.get("username", ""),
        "nama": info.get("nama", ""),
    }
    simpan_data()


async def ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not adalah_admin(update):
        return
    if not context.args:
        await update.message.reply_text(
            "Format:\n/ban id\n/ban @username\n\nContoh: /ban 123456789"
        )
        return
    uid = cari_target(context.args[0])
    if uid is None:
        await update.message.reply_text(
            "User tidak ditemukan. Pakai @username hanya untuk pengguna yang pernah memakai bot ini; selain itu gunakan ID angka."
        )
        return
    if uid in ADMIN_IDS:
        await update.message.reply_text("Admin tidak bisa di-ban.")
        return
    if str(uid) in DB["ban"]:
        await update.message.reply_text("User itu sudah di-ban.")
        return
    lakukan_ban(uid)
    await update.message.reply_text(f"🚫 {label(uid)} berhasil di-ban.")


async def unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not adalah_admin(update):
        return
    if not context.args:
        await update.message.reply_text(
            "Format:\n/unban id\n/unban @username\n\nContoh: /unban 123456789"
        )
        return
    uid = cari_target(context.args[0])
    if uid is None or str(uid) not in DB["ban"]:
        await update.message.reply_text("User itu tidak sedang di-ban.")
        return
    nama = label(uid)
    DB["ban"].pop(str(uid))
    simpan_data()
    await update.message.reply_text(f"✅ {nama} berhasil di-unban.")


async def banlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not adalah_admin(update):
        return
    if not DB["ban"]:
        await update.message.reply_text("Tidak ada user yang di-ban.")
        return
    baris = [f"🚫 DAFTAR BAN ({len(DB['ban'])})", ""]
    for nomor, uid in enumerate(DB["ban"], start=1):
        baris.append(f"{nomor}. {label(uid)}")
    baris.extend(["", "Buka ban dengan /unban id atau /unban @username"])
    await update.message.reply_text("\n".join(baris))


async def tombol_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id not in ADMIN_IDS:
        await query.answer("Khusus admin.", show_alert=True)
        return
    aksi, uid = query.data.split(":")
    uid = int(uid)
    if aksi == "ban":
        if uid in ADMIN_IDS:
            await query.answer("Admin tidak bisa di-ban.", show_alert=True)
            return
        lakukan_ban(uid)
        await query.answer("🚫 User di-ban")
    else:
        DB["ban"].pop(str(uid), None)
        simpan_data()
        await query.answer("✅ User di-unban")
    try:
        await query.edit_message_reply_markup(reply_markup=papan_ban(uid))
    except Exception as error:
        print("Gagal mengubah tombol:", error)


# ==================== BROADCAST / PING ====================

async def kirim_ke_semua(context, isi):
    berhasil = gagal = 0
    for uid in list(DB["users"].keys()):
        try:
            await context.bot.send_message(chat_id=int(uid), text=isi)
            berhasil += 1
        except Exception:
            gagal += 1
        await asyncio.sleep(0.1)
    return berhasil, gagal


async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not adalah_admin(update):
        return
    _, _, isi = update.message.text.partition(" ")
    isi = isi.strip()
    if not isi:
        await update.message.reply_text(
            "Format: /broadcast isi pesan\n\n"
            "Contoh: /broadcast Halo semua, bot sedang maintenance malam ini."
        )
        return
    await update.message.reply_text(f"📢 Mengirim ke {len(DB['users'])} pengguna...")
    berhasil, gagal = await kirim_ke_semua(context, isi)
    await update.message.reply_text(
        f"✅ Broadcast selesai.\n\nTerkirim: {berhasil}\nGagal: {gagal}"
    )


def uptime_teks():
    total = int(time.time() - waktu_mulai)
    hari, sisa = divmod(total, 86400)
    jam, sisa = divmod(sisa, 3600)
    menit, detik = divmod(sisa, 60)
    return f"{hari} hari {jam} jam {menit} menit {detik} detik"


async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mulai = time.time()
    pesan = await update.message.reply_text("🏓 Pong...")
    respon = int((time.time() - mulai) * 1000)
    await pesan.edit_text(
        "🏓 Pong! DMG Menfess Bot aktif ✅\n\n"
        f"⚡ Respon: {respon} ms\n"
        f"⏱️ Aktif: {uptime_teks()}"
    )


# ==================== PANEL ADMIN ====================

def teks_panel():
    return (
        "🛠 <b>PANEL ADMIN</b>\n\n"
        f"👥 Pengguna: {len(DB['users'])}\n"
        f"🚫 Di-ban: {len(DB['ban'])}\n\n"
        "Pilih menu di bawah ini."
    )


def tombol_menu_panel():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("👥 Pengguna", callback_data="panel:users"),
            InlineKeyboardButton("🚫 Daftar Ban", callback_data="panel:banlist"),
        ],
        [
            InlineKeyboardButton("🔨 Ban", callback_data="panel:ban"),
            InlineKeyboardButton("♻️ Unban", callback_data="panel:unban"),
        ],
        [
            InlineKeyboardButton("📢 Broadcast", callback_data="panel:broadcast"),
            InlineKeyboardButton("🏓 Status", callback_data="panel:status"),
        ],
        [InlineKeyboardButton("❌ Tutup", callback_data="panel:tutup")],
    ])


def tombol_kembali():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("⬅️ Kembali", callback_data="panel:menu")
    ]])


async def tampilkan_banlist(query):
    if not DB["ban"]:
        await query.edit_message_text(
            "Tidak ada user yang di-ban.", reply_markup=tombol_kembali()
        )
        return
    baris = [f"🚫 DAFTAR BAN ({len(DB['ban'])})", ""]
    tombol = []
    for nomor, uid in enumerate(list(DB["ban"].keys())[:10], start=1):
        baris.append(f"{nomor}. {label(uid)}")
        tombol.append([InlineKeyboardButton(
            f"♻️ Unban {nomor}", callback_data=f"panel:unb:{uid}"
        )])
    if len(DB["ban"]) > 10:
        baris.append(f"\n...dan {len(DB['ban']) - 10} lainnya (pakai /unban id)")
    tombol.append([InlineKeyboardButton("⬅️ Kembali", callback_data="panel:menu")])
    await query.edit_message_text(
        "\n".join(baris), reply_markup=InlineKeyboardMarkup(tombol)
    )


async def panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not adalah_admin(update):
        return
    context.user_data.pop("menunggu", None)
    await update.message.reply_text(
        teks_panel(), parse_mode="HTML", reply_markup=tombol_menu_panel()
    )


async def batal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not adalah_admin(update):
        return
    context.user_data.pop("menunggu", None)
    context.user_data.pop("bc_teks", None)
    await update.message.reply_text("Dibatalkan. Ketik /panel untuk membuka panel.")


async def tombol_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id not in ADMIN_IDS:
        await query.answer("Khusus admin.", show_alert=True)
        return
    aksi = query.data.split(":", 1)[1]
    await query.answer()
    try:
        if aksi == "menu":
            context.user_data.pop("menunggu", None)
            await query.edit_message_text(
                teks_panel(), parse_mode="HTML", reply_markup=tombol_menu_panel()
            )
        elif aksi == "users":
            terbaru = list(DB["users"].keys())[-10:]
            baris = [f"👥 Total pengguna: {len(DB['users'])}",
                     f"🚫 Di-ban: {len(DB['ban'])}", ""]
            if terbaru:
                baris.append("Pengguna terbaru:")
                for uid in reversed(terbaru):
                    baris.append(f"• {label(uid)}")
            await query.edit_message_text("\n".join(baris), reply_markup=tombol_kembali())
        elif aksi == "banlist":
            await tampilkan_banlist(query)
        elif aksi.startswith("unb:"):
            uid = aksi.split(":")[1]
            DB["ban"].pop(uid, None)
            simpan_data()
            await tampilkan_banlist(query)
        elif aksi in ("ban", "unban"):
            context.user_data["menunggu"] = aksi
            judul = "🔨 BAN USER" if aksi == "ban" else "♻️ UNBAN USER"
            await query.edit_message_text(
                f"{judul}\n\nKirim ID atau @username user-nya sekarang.\n"
                "Ketik /batal untuk membatalkan.",
                reply_markup=tombol_kembali(),
            )
        elif aksi == "broadcast":
            context.user_data["menunggu"] = "broadcast"
            await query.edit_message_text(
                "📢 BROADCAST\n\nKirim isi pesan yang mau disebarkan sekarang.\n"
                "Ketik /batal untuk membatalkan.",
                reply_markup=tombol_kembali(),
            )
        elif aksi == "bc_ya":
            teks = context.user_data.pop("bc_teks", None)
            if not teks:
                await query.edit_message_text(
                    "Tidak ada pesan untuk dikirim.", reply_markup=tombol_kembali()
                )
                return
            await query.edit_message_text(
                f"📢 Mengirim ke {len(DB['users'])} pengguna..."
            )
            berhasil, gagal = await kirim_ke_semua(context, teks)
            await query.edit_message_text(
                f"✅ Broadcast selesai.\n\nTerkirim: {berhasil}\nGagal: {gagal}",
                reply_markup=tombol_kembali(),
            )
        elif aksi == "bc_batal":
            context.user_data.pop("bc_teks", None)
            await query.edit_message_text(
                "Broadcast dibatalkan.", reply_markup=tombol_kembali()
            )
        elif aksi == "status":
            await query.edit_message_text(
                "🏓 <b>BOT AKTIF</b> ✅\n\n"
                f"⏱️ Aktif: {uptime_teks()}\n"
                f"👥 Pengguna: {len(DB['users'])}\n"
                f"🚫 Di-ban: {len(DB['ban'])}",
                parse_mode="HTML", reply_markup=tombol_kembali(),
            )
        elif aksi == "tutup":
            context.user_data.pop("menunggu", None)
            await query.message.delete()
    except Exception as error:
        print("Panel mengalami error:", error)


async def input_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    mode = context.user_data.get("menunggu")
    if not mode:
        return
    teks = update.message.text.strip()
    context.user_data.pop("menunggu", None)
    if mode == "broadcast":
        context.user_data["bc_teks"] = teks
        tombol = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Kirim", callback_data="panel:bc_ya"),
            InlineKeyboardButton("❌ Batal", callback_data="panel:bc_batal"),
        ]])
        await update.message.reply_text(
            f"📢 Pesan ini akan dikirim ke {len(DB['users'])} pengguna:\n\n"
            f"{teks}\n\nKirim sekarang?",
            reply_markup=tombol,
        )
    else:
        uid = cari_target(teks)
        if uid is None:
            hasil = (
                "User tidak ditemukan. Pakai @username hanya untuk pengguna "
                "yang pernah memakai bot ini; selain itu gunakan ID angka."
            )
        elif mode == "ban":
            if uid in ADMIN_IDS:
                hasil = "Admin tidak bisa di-ban."
            elif str(uid) in DB["ban"]:
                hasil = "User itu sudah di-ban."
            else:
                lakukan_ban(uid)
                hasil = f"🚫 {label(uid)} berhasil di-ban."
        else:
            if str(uid) not in DB["ban"]:
                hasil = "User itu tidak sedang di-ban."
            else:
                nama = label(uid)
                DB["ban"].pop(str(uid))
                simpan_data()
                hasil = f"✅ {nama} berhasil di-unban."
        await update.message.reply_text(hasil, reply_markup=tombol_kembali())
    raise ApplicationHandlerStop


# ==================== ERROR HANDLER ====================

async def tangani_error(update, context):
    print("Terjadi error:", context.error)


# ==================== MAIN ====================

def main():
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .pool_timeout(30)
        .get_updates_connect_timeout(30)
        .get_updates_read_timeout(40)
        .get_updates_write_timeout(30)
        .get_updates_pool_timeout(30)
        .build()
    )
    pribadi = filters.ChatType.PRIVATE

    app.add_handler(CommandHandler("start", start, filters=pribadi))
    app.add_handler(CommandHandler("ban", ban, filters=pribadi))
    app.add_handler(CommandHandler("unban", unban, filters=pribadi))
    app.add_handler(CommandHandler("banlist", banlist, filters=pribadi))
    app.add_handler(CommandHandler("broadcast", broadcast, filters=pribadi))
    app.add_handler(CommandHandler("ping", ping, filters=pribadi))
    app.add_handler(CommandHandler("panel", panel, filters=pribadi))
    app.add_handler(CommandHandler("batal", batal, filters=pribadi))

    app.add_handler(CallbackQueryHandler(ui_callback, pattern=r"^ui:"))
    app.add_handler(CallbackQueryHandler(tombol_panel, pattern=r"^panel:"))
    app.add_handler(CallbackQueryHandler(tombol_ban, pattern=r"^(ban|unban):\d+$"))

    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND & pribadi, input_admin),
        group=-1,
    )
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND & pribadi, kirim_menfess)
    )
    app.add_error_handler(tangani_error)

    print("DMG Menfess Bot berjalan...")
    app.run_polling(timeout=20)


if __name__ == "__main__":
    main()
