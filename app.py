import os
import logging
import re
import sqlite3
import threading
from io import BytesIO
from flask import Flask
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import requests
from bs4 import BeautifulSoup
import struct

# --- НАСТРОЙКА ---
TOKEN = "8690077939:AAHQ22wV8zPQRdzXikhxUVNhtnzzBFRYwms"

# Настройка логирования (чтобы видеть всё в логах Render)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

@app.route('/')
def home():
    return "🎹 Zeta Piano Converter is running!"

@app.route('/health')
def health():
    return "OK"

# --- БАЗА ДАННЫХ ---
def init_db():
    conn = sqlite3.connect('songs.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS cache
                 (song_name TEXT PRIMARY KEY, notes TEXT, bpm INTEGER)''')
    conn.commit()
    conn.close()
    logger.info("База данных инициализирована")

def get_from_cache(song_name):
    conn = sqlite3.connect('songs.db')
    c = conn.cursor()
    c.execute('SELECT notes, bpm FROM cache WHERE song_name = ?', (song_name.lower(),))
    result = c.fetchone()
    conn.close()
    if result:
        logger.info(f"Найдено в кэше: {song_name}")
    return result

def save_to_cache(song_name, notes, bpm):
    conn = sqlite3.connect('songs.db')
    c = conn.cursor()
    c.execute('INSERT OR REPLACE INTO cache VALUES (?, ?, ?)', 
              (song_name.lower(), notes, bpm))
    conn.commit()
    conn.close()
    logger.info(f"Сохранено в кэш: {song_name}")

# --- ПОИСК MIDI ---
def search_midi(song_name):
    logger.info(f"Поиск MIDI для: {song_name}")
    search_url = f"https://midishow.com/search/result?search={song_name.replace(' ', '+')}"
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        response = requests.get(search_url, headers=headers, timeout=30)
        soup = BeautifulSoup(response.text, 'html.parser')
        link = soup.find('a', href=re.compile(r'/midi/\d+'))
        if link:
            midi_id = re.search(r'/midi/(\d+)', link['href']).group(1)
            url = f"https://midishow.com/midi/download/{midi_id}"
            logger.info(f"Найден MIDI: {url}")
            return url
        logger.warning(f"MIDI не найден: {song_name}")
        return None
    except Exception as e:
        logger.error(f"Ошибка поиска: {e}")
        return None

# --- КОНВЕРТЕР ---
def midi_to_virtual_piano(midi_data):
    logger.info("Начало конвертации MIDI...")
    try:
        key_map = {
            60: 'q', 61: '2', 62: 'w', 63: '3', 64: 'e',
            65: 'r', 66: '5', 67: 't', 68: '6', 69: 'y',
            70: 'u', 71: '7', 72: 'i', 73: 'o', 74: '9',
            75: 'p', 76: '0', 77: 'a', 78: 's', 79: 'd',
            80: 'f', 81: 'g', 82: 'h', 83: 'j', 84: 'k',
            85: 'l', 86: 'z', 87: 'x', 88: 'c', 89: 'v',
            90: 'b', 91: 'n', 92: 'm'
        }
        
        notes = []
        # Ищем ноты в MIDI-данных
        for i in range(len(midi_data) - 2):
            if midi_data[i] in [0x90, 0x80]:  # Note on/off
                note = midi_data[i+1]
                if note in key_map:
                    notes.append(key_map[note])
        
        result = ' '.join(notes) if notes else None
        if result:
            logger.info(f"Конвертация успешна. Найдено нот: {len(notes)}")
        else:
            logger.warning("Ноты не найдены в MIDI-файле")
        return result
    except Exception as e:
        logger.error(f"Ошибка конвертации: {e}")
        return None

# --- СКАЧИВАНИЕ MIDI ---
def download_midi(url):
    logger.info(f"Скачивание MIDI: {url}")
    try:
        response = requests.get(url, timeout=30)
        if response.status_code == 200:
            logger.info(f"MIDI скачан. Размер: {len(response.content)} байт")
            return response.content
        else:
            logger.error(f"Ошибка скачивания. Статус: {response.status_code}")
            return None
    except Exception as e:
        logger.error(f"Ошибка скачивания: {e}")
        return None

# --- ОБРАБОТЧИКИ ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    logger.info(f"Команда /start от пользователя: {user.username or user.first_name} (ID: {user.id})")
    await update.message.reply_text(
        "🎹 **Zeta Piano Converter**\n\n"
        "✅ Бот работает!\n"
        "📝 Напиши название песни, и я найду ноты для Virtual Piano.\n"
        "Пример: `Shape of You`\n\n"
        "⚡ Статус: Онлайн",
        parse_mode='Markdown'
    )

async def handle_song(update: Update, context: ContextTypes.DEFAULT_TYPE):
    song_name = update.message.text.strip()
    user = update.effective_user
    logger.info(f"Запрос песни от {user.username or user.first_name}: {song_name}")
    
    # Отправляем статус "печатает..."
    await update.message.chat.send_action(action='typing')
    
    # Проверка кэша
    cached = get_from_cache(song_name)
    if cached:
        notes, bpm = cached
        logger.info(f"Отправка из кэша для: {song_name}")
        await update.message.reply_text(
            f"🎵 **{song_name.title()}** (из кэша ⚡)\n"
            f"📊 BPM: {bpm}\n\n"
            f"```\n{notes[:500]}{'...' if len(notes) > 500 else ''}\n```\n"
            f"📝 {len(notes)} нот найдено",
            parse_mode='Markdown'
        )
        return
    
    # Поиск MIDI
    status_msg = await update.message.reply_text(f"🔍 Ищу MIDI для '{song_name}'...")
    
    midi_url = search_midi(song_name)
    if not midi_url:
        logger.warning(f"MIDI не найден для: {song_name}")
        await status_msg.edit_text(f"❌ Не нашёл MIDI для '{song_name}'.\n"
                                   f"Попробуй:\n"
                                   f"• Написать по-английски\n"
                                   f"• Уточнить название\n"
                                   f"• Попробовать другую песню")
        return
    
    # Скачивание MIDI
    await status_msg.edit_text(f"📥 Скачиваю MIDI...")
    
    midi_data = download_midi(midi_url)
    if not midi_data:
        await status_msg.edit_text(f"❌ Ошибка скачивания MIDI для '{song_name}'.")
        return
    
    # Конвертация
    await status_msg.edit_text(f"🔄 Конвертирую MIDI в ноты...")
    
    notes_text = midi_to_virtual_piano(midi_data)
    if not notes_text:
        await status_msg.edit_text(f"❌ Не удалось сконвертировать '{song_name}'.\n"
                                   f"MIDI-файл не содержит распознаваемых нот.")
        return
    
    # Сохраняем в кэш
    bpm = 120
    save_to_cache(song_name, notes_text, bpm)
    
    # Отправляем результат
    note_count = len(notes_text.split())
    if len(notes_text) <= 4000:
        await status_msg.edit_text(
            f"🎵 **{song_name.title()}**\n"
            f"📊 BPM: {bpm}\n"
            f"📝 Найдено нот: {note_count}\n\n"
            f"```\n{notes_text[:1000]}{'...' if len(notes_text) > 1000 else ''}\n```\n"
            f"✅ Скопируй текст и вставь в Virtual Piano!",
            parse_mode='Markdown'
        )
        logger.info(f"Отправлены ноты для: {song_name} ({note_count} нот)")
    else:
        # Если нот слишком много — отправляем файлом
        filename = f"{song_name}_notes.txt"
        with open(filename, 'w', encoding='utf-8') as f:
            f.write(notes_text)
        
        await status_msg.edit_text(
            f"🎵 **{song_name.title()}**\n"
            f"📊 BPM: {bpm}\n"
            f"📝 Найдено нот: {note_count}\n"
            f"📦 Ноты слишком длинные, отправляю файлом..."
        )
        
        await update.message.reply_document(
            document=open(filename, 'rb'),
            filename=filename
        )
        os.remove(filename)
        logger.info(f"Отправлен файл с нотами для: {song_name}")

# --- ЗАПУСК ---
def run_bot():
    try:
        logger.info("Инициализация базы данных...")
        init_db()
        
        logger.info("Создание приложения бота...")
        bot_app = Application.builder().token(TOKEN).build()
        bot_app.add_handler(CommandHandler("start", start))
        bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_song))
        
        logger.info("🔥 Telegram-бот запущен и готов к работе!")
        bot_app.run_polling(allowed_updates=Update.ALL_TYPES)
    except Exception as e:
        logger.error(f"Критическая ошибка при запуске бота: {e}")

if __name__ == "__main__":
    # Запускаем бота в фоновом потоке
    logger.info("Запуск бота в фоновом потоке...")
    bot_thread = threading.Thread(target=run_bot)
    bot_thread.daemon = True
    bot_thread.start()
    
    # Запускаем Flask-сервер (для Render)
    port = int(os.environ.get("PORT", 5000))
    logger.info(f"Запуск Flask-сервера на порту {port}...")
    app.run(host="0.0.0.0", port=port)
