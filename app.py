import os
import logging
import re
import sqlite3
import threading
from io import BytesIO
from flask import Flask
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import pretty_midi
import requests
from bs4 import BeautifulSoup

# --- НАСТРОЙКА ---
TOKEN = "8690077939:AAHQ22wV8zPQRdzXikhxUVNhtnzzBFRYwms"
logging.basicConfig(level=logging.INFO)

# --- FLASK СЕРВЕР (чтобы Render не убивал бота) ---
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

def get_from_cache(song_name):
    conn = sqlite3.connect('songs.db')
    c = conn.cursor()
    c.execute('SELECT notes, bpm FROM cache WHERE song_name = ?', (song_name.lower(),))
    result = c.fetchone()
    conn.close()
    return result

def save_to_cache(song_name, notes, bpm):
    conn = sqlite3.connect('songs.db')
    c = conn.cursor()
    c.execute('INSERT OR REPLACE INTO cache VALUES (?, ?, ?)', 
              (song_name.lower(), notes, bpm))
    conn.commit()
    conn.close()

# --- ПОИСК MIDI ---
def search_midi(song_name):
    search_url = f"https://midishow.com/search/result?search={song_name.replace(' ', '+')}"
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        response = requests.get(search_url, headers=headers, timeout=30)
        soup = BeautifulSoup(response.text, 'html.parser')
        link = soup.find('a', href=re.compile(r'/midi/\d+'))
        if link:
            midi_id = re.search(r'/midi/(\d+)', link['href']).group(1)
            return f"https://midishow.com/midi/download/{midi_id}"
        return None
    except Exception as e:
        logging.error(f"Ошибка поиска: {e}")
        return None

# --- КОНВЕРТЕР ---
def midi_to_virtual_piano(midi_data):
    try:
        midi = pretty_midi.PrettyMIDI(BytesIO(midi_data))
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
        for instrument in midi.instruments:
            for note in instrument.notes:
                if note.pitch in key_map:
                    notes.append(key_map[note.pitch])
        return ' '.join(notes) if notes else None
    except Exception as e:
        logging.error(f"Ошибка конвертации: {e}")
        return None

# --- СКАЧИВАНИЕ MIDI ---
def download_midi(url):
    try:
        response = requests.get(url, timeout=30)
        return response.content if response.status_code == 200 else None
    except Exception as e:
        logging.error(f"Ошибка скачивания: {e}")
        return None

# --- ОБРАБОТЧИКИ ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎹 **Zeta Piano Converter**\n\n"
        "Напиши название песни, и я найду ноты для Virtual Piano!\n"
        "Пример: `Shape of You`\n\n"
        "⚡ Работает 24/7 на Render.",
        parse_mode='Markdown'
    )

async def handle_song(update: Update, context: ContextTypes.DEFAULT_TYPE):
    song_name = update.message.text.strip()
    await update.message.chat.send_action(action='typing')
    
    cached = get_from_cache(song_name)
    if cached:
        notes, bpm = cached
        await update.message.reply_text(
            f"🎵 **{song_name.title()}** (из кэша)\n"
            f"📊 BPM: {bpm}\n\n"
            f"```\n{notes[:500]}{'...' if len(notes) > 500 else ''}\n```",
            parse_mode='Markdown'
        )
        return
    
    await update.message.reply_text(f"🔍 Ищу '{song_name}'...")
    
    midi_url = search_midi(song_name)
    if not midi_url:
        await update.message.reply_text(f"❌ Не нашёл. Попробуй по-английски.")
        return
    
    await update.message.reply_text(f"📥 Скачиваю...")
    
    midi_data = download_midi(midi_url)
    if not midi_data:
        await update.message.reply_text("❌ Ошибка скачивания.")
        return
    
    await update.message.reply_text(f"🔄 Конвертирую...")
    
    notes_text = midi_to_virtual_piano(midi_data)
    if not notes_text:
        await update.message.reply_text("❌ Ошибка конвертации.")
        return
    
    bpm = 120
    save_to_cache(song_name, notes_text, bpm)
    
    if len(notes_text) <= 4000:
        await update.message.reply_text(
            f"🎵 **{song_name.title()}**\n"
            f"📊 BPM: {bpm}\n\n"
            f"```\n{notes_text[:1000]}{'...' if len(notes_text) > 1000 else ''}\n```",
            parse_mode='Markdown'
        )
    else:
        with open(f"{song_name}.txt", 'w', encoding='utf-8') as f:
            f.write(notes_text)
        await update.message.reply_document(
            document=open(f"{song_name}.txt", 'rb'),
            filename=f"{song_name}_notes.txt"
        )
        os.remove(f"{song_name}.txt")

# --- ЗАПУСК ТЕЛЕГРАМ-БОТА В ОТДЕЛЬНОМ ПОТОКЕ ---
def run_bot():
    init_db()
    bot_app = Application.builder().token(TOKEN).build()
    bot_app.add_handler(CommandHandler("start", start))
    bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_song))
    print("🔥 Telegram-бот запущен!")
    bot_app.run_polling(allowed_updates=Update.ALL_TYPES)

# --- ЗАПУСК ВСЕГО ---
if __name__ == "__main__":
    # Запускаем бота в фоновом потоке
    bot_thread = threading.Thread(target=run_bot)
    bot_thread.start()
    
    # Запускаем Flask-сервер (для Render)
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)a