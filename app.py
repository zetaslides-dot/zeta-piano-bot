import os
import logging
import re
import sqlite3
import threading
import time
from flask import Flask
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import requests
from bs4 import BeautifulSoup
from urllib.parse import quote_plus

# --- НАСТРОЙКА ---
TOKEN = "8690077939:AAHQ22wV8zPQRdzXikhxUVNhtnzzBFRYwms"

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

# --- УЛУЧШЕННЫЙ ПОИСК MIDI ---
def search_midi(song_name):
    """Ищет MIDI на нескольких сайтах, пробуя разные вариации названия"""
    logger.info(f"Поиск MIDI для: {song_name}")
    
    # Генерируем варианты названий для поиска
    search_terms = [
        song_name,
        f"{song_name} midi",
        f"{song_name} mid",
        f"{song_name} filetype:mid",
    ]
    
    # Если название состоит из нескольких слов, пробуем убрать лишнее
    parts = song_name.split()
    if len(parts) > 2:
        search_terms.append(' '.join(parts[:2]))  # Первые два слова
        search_terms.append(' '.join(parts[1:]))  # Последние слова
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.5',
        'Accept-Encoding': 'gzip, deflate, br',
        'DNT': '1',
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1'
    }
    
    session = requests.Session()
    
    # --- 1. MidiShow ---
    for term in search_terms[:3]:  # Пробуем первые 3 варианта
        try:
            encoded_term = quote_plus(term)
            url = f"https://midishow.com/search/result?search={encoded_term}"
            logger.info(f"Пробую MidiShow: {url}")
            
            response = session.get(url, headers=headers, timeout=15)
            if response.status_code != 200:
                continue
                
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # Ищем ссылки на скачивание
            download_links = soup.find_all('a', href=re.compile(r'/midi/download/\d+'))
            for link in download_links:
                href = link.get('href')
                if href:
                    if href.startswith('/'):
                        href = f"https://midishow.com{href}"
                    logger.info(f"Найден MIDI (MidiShow): {href}")
                    return href
            
            # Ищем ссылки на страницы с MIDI
            midi_links = soup.find_all('a', href=re.compile(r'/midi/\d+'))
            for link in midi_links[:5]:
                href = link.get('href')
                if href and 'download' not in href:
                    page_url = f"https://midishow.com{href}"
                    try:
                        time.sleep(0.5)
                        page_response = session.get(page_url, headers=headers, timeout=10)
                        if page_response.status_code == 200:
                            page_soup = BeautifulSoup(page_response.text, 'html.parser')
                            download_link = page_soup.find('a', href=re.compile(r'/midi/download/\d+'))
                            if download_link:
                                dl_href = download_link.get('href')
                                if dl_href:
                                    if dl_href.startswith('/'):
                                        dl_href = f"https://midishow.com{dl_href}"
                                    logger.info(f"Найден MIDI на странице: {dl_href}")
                                    return dl_href
                    except:
                        continue
        except Exception as e:
            logger.error(f"Ошибка MidiShow: {e}")
    
    # --- 2. BitMidi ---
    for term in search_terms[:3]:
        try:
            encoded_term = quote_plus(term)
            url = f"https://bitmidi.com/search/{encoded_term}"
            logger.info(f"Пробую BitMidi: {url}")
            
            response = session.get(url, headers=headers, timeout=15)
            if response.status_code != 200:
                continue
                
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # Ищем ссылки на .mid файлы
            for link in soup.find_all('a', href=True):
                href = link.get('href')
                if href and href.endswith('.mid'):
                    if href.startswith('/'):
                        href = f"https://bitmidi.com{href}"
                    logger.info(f"Найден MIDI (BitMidi): {href}")
                    return href
                
                if href and '/midi/' in href and 'search' not in href:
                    page_url = href if href.startswith('http') else f"https://bitmidi.com{href}"
                    try:
                        time.sleep(0.5)
                        page_response = session.get(page_url, headers=headers, timeout=10)
                        if page_response.status_code == 200:
                            page_soup = BeautifulSoup(page_response.text, 'html.parser')
                            for file_link in page_soup.find_all('a', href=True):
                                file_href = file_link.get('href')
                                if file_href and (file_href.endswith('.mid') or '/download/' in file_href):
                                    if file_href.startswith('/'):
                                        file_href = f"https://bitmidi.com{file_href}"
                                    logger.info(f"Найден MIDI на странице BitMidi: {file_href}")
                                    return file_href
                    except:
                        continue
        except Exception as e:
            logger.error(f"Ошибка BitMidi: {e}")
    
    # --- 3. FreeMidi ---
    for term in search_terms[:3]:
        try:
            encoded_term = quote_plus(term)
            url = f"https://freemidi.org/search?q={encoded_term}"
            logger.info(f"Пробую FreeMidi: {url}")
            
            response = session.get(url, headers=headers, timeout=15)
            if response.status_code != 200:
                continue
                
            soup = BeautifulSoup(response.text, 'html.parser')
            
            for link in soup.find_all('a', href=True):
                href = link.get('href')
                if href and ('/download/' in href or href.endswith('.mid')):
                    if href.startswith('/'):
                        href = f"https://freemidi.org{href}"
                    logger.info(f"Найден MIDI (FreeMidi): {href}")
                    return href
        except Exception as e:
            logger.error(f"Ошибка FreeMidi: {e}")
    
    # --- 4. MidiFiles ---
    for term in search_terms[:2]:
        try:
            encoded_term = quote_plus(term)
            url = f"https://midifiles.com/search?q={encoded_term}"
            logger.info(f"Пробую MidiFiles: {url}")
            
            response = session.get(url, headers=headers, timeout=15)
            if response.status_code != 200:
                continue
                
            soup = BeautifulSoup(response.text, 'html.parser')
            
            for link in soup.find_all('a', href=True):
                href = link.get('href')
                if href and '/download/' in href:
                    if href.startswith('/'):
                        href = f"https://midifiles.com{href}"
                    logger.info(f"Найден MIDI (MidiFiles): {href}")
                    return href
        except Exception as e:
            logger.error(f"Ошибка MidiFiles: {e}")
    
    logger.warning(f"MIDI не найден: {song_name}")
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
        for i in range(len(midi_data) - 2):
            if midi_data[i] in [0x90, 0x80]:
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

def download_midi(url):
    logger.info(f"Скачивание MIDI: {url}")
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        }
        response = requests.get(url, headers=headers, timeout=30)
        if response.status_code == 200:
            logger.info(f"MIDI скачан. Размер: {len(response.content)} байт")
            return response.content
        else:
            logger.error(f"Ошибка скачивания. Статус: {response.status_code}")
            return None
    except Exception as e:
        logger.error(f"Ошибка скачивания: {e}")
        return None

# --- ОБРАБОТЧИКИ (без изменений) ---
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
    
    await update.message.chat.send_action(action='typing')
    
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
    
    await status_msg.edit_text(f"📥 Скачиваю MIDI...")
    
    midi_data = download_midi(midi_url)
    if not midi_data:
        await status_msg.edit_text(f"❌ Ошибка скачивания MIDI для '{song_name}'.")
        return
    
    await status_msg.edit_text(f"🔄 Конвертирую MIDI в ноты...")
    
    notes_text = midi_to_virtual_piano(midi_data)
    if not notes_text:
        await status_msg.edit_text(f"❌ Не удалось сконвертировать '{song_name}'.\n"
                                   f"MIDI-файл не содержит распознаваемых нот.")
        return
    
    bpm = 120
    save_to_cache(song_name, notes_text, bpm)
    
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

# --- ЗАПУСК (без изменений) ---
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

def run_flask():
    port = int(os.environ.get("PORT", 5000))
    logger.info(f"Запуск Flask-сервера на порту {port}...")
    app.run(host="0.0.0.0", port=port)

if __name__ == "__main__":
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()
    run_bot()
