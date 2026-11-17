DMG Menfess Bot
Bot Telegram berbasis Python dan python-telegram-bot.
Instalasi
1. Buat file .env berdasarkan .env.example.
2. Isi token, ID channel tujuan, username channel, ID admin, dan ID channel log di .env.
4. Install dependensi: pip install "python-telegram-bot>=22,<23"
Keamanan
.env menyimpan konfigurasi rahasia dan dikecualikan oleh .gitignore.
menfess_data.json berisi data pengguna dan daftar ban; file ini juga dikecualikan.
Jika token pernah dibagikan, cabut token lama lewat BotFather dan gunakan token baru.
Bot harus menjadi admin di channel tujuan dan channel log.
