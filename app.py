import os
import logging
import re
import sqlite3
import threading
import time
import subprocess
import tempfile
from flask import Flask
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import requests
from bs4 import BeautifulSoup
from urllib.parse import quote_plus
import yt_dlp
from basic_pitch.inference import predict_and_save
import librosa
import numpy as np

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

# --- ПОИСК MIDI (СТАРАЯ ВЕРСИЯ ДЛЯ НАЗВАНИЙ) ---
def search_midi(song_name):
    """Ищет MIDI на нескольких сайтах, пробуя разные вариации названия"""
    logger.info(f"Поиск MIDI для: {song_name}")
    
    search_terms = [
        song_name,
        f"{song_name} midi",
        f"{song_name} mid",
        f"{song_name} filetype:mid",
    ]
    
    parts = song_name.split()
    if len(parts) > 2:
        search_terms.append(' '.join(parts[:2]))
        search_terms.append(' '.join(parts[1:]))
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    }
    
    session = requests.Session()
    
    # --- MidiShow ---
    for term in search_terms[:3]:
        try:
            url = f"https://midishow.com/search/result?search={quote_plus(term)}"
            logger.info(f"Пробую MidiShow: {url}")
            response = session.get(url, headers=headers, timeout=15)
            if response.status_code != 200:
                continue
            soup = BeautifulSoup(response.text, 'html.parser')
            download_links = soup.find_all('a', href=re.compile(r'/midi/download/\d+'))
            for link in download_links:
                href = link.get('href')
                if href:
                    if href.startswith('/'):
                        href = f"https://midishow.com{href}"
                    logger.info(f"Найден MIDI (MidiShow): {href}")
                    return href
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
    
    # --- BitMidi ---
    for term in search_terms[:3]:
        try:
            url = f"https://bitmidi.com/search/{quote_plus(term)}"
            logger.info(f"Пробую BitMidi: {url}")
            response = session.get(url, headers=headers, timeout=15)
            if response.status_code != 200:
                continue
            soup = BeautifulSoup(response.text, 'html.parser')
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
    
    # --- FreeMidi ---
    for term in search_terms[:3]:
        try:
            url = f"https://freemidi.org/search?q={quote_plus(term)}"
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
    
    logger.warning(f"MIDI не найден: {song_name}")
    return None

# --- НОВЫЙ КОНВЕЙЕР: YOUTUBE → AUDIO → MIDI ---
def download_audio_from_youtube(youtube_url):
    """Скачивает аудио с YouTube и возвращает путь к файлу"""
    logger.info(f"Скачивание аудио с YouTube: {youtube_url}")
    
    with tempfile.TemporaryDirectory() as temp_dir:
        ydl_opts = {
            'format': 'bestaudio/best',
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'wav',
                'preferredquality': '192',
            }],
            'outtmpl': os.path.join(temp_dir, '%(title)s.%(ext)s'),
            'quiet': True,
            'no_warnings': True,
        }
        
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(youtube_url, download=True)
                filename = ydl.prepare_filename(info)
                # Меняем расширение на .wav
                wav_filename = filename.rsplit('.', 1)[0] + '.wav'
                logger.info(f"Аудио скачано: {wav_filename}")
                return wav_filename
        except Exception as e:
            logger.error(f"Ошибка скачивания YouTube: {e}")
            return None

def audio_to_midi(audio_path):
    """Конвертирует аудио в MIDI с помощью basic-pitch"""
    logger.info(f"Конвертация аудио в MIDI: {audio_path}")
    
    try:
        # Создаём временную папку для MIDI
        with tempfile.TemporaryDirectory() as output_dir:
            # Запускаем basic-pitch
            predict_and_save(
                [audio_path],
                output_dir,
                save_midi=True,
                sonify_midi=False,
                save_model_outputs=False
            )
            
            # Ищем созданный MIDI-файл
            midi_files = [f for f in os.listdir(output_dir) if f.endswith('.mid')]
            if midi_files:
                midi_path = os.path.join(output_dir, midi_files[0])
                logger.info(f"MIDI создан: {midi_path}")
                
                # Читаем MIDI-файл и возвращаем его содержимое
                with open(midi_path, 'rb') as f:
                    midi_data = f.read()
                return midi_data
            else:
                logger.error("MIDI-файл не найден")
                return None
    except Exception as e:
        logger.error(f"Ошибка конвертации аудио в MIDI: {e}")
        return None

def youtube_to_roblox_notes(youtube_url):
    """Полный конвейер: YouTube → ноты для Roblox"""
    logger.info(f"Запуск конвейера YouTube → Roblox: {youtube_url}")
    
    # Шаг 1: Скачиваем аудио
    audio_path = download_audio_from_youtube(youtube_url)
    if not audio_path:
        return None
    
    # Шаг 2: Конвертируем аудио в MIDI
    midi_data = audio_to_midi(audio_path)
    if not midi_data:
        return None
    
    # Шаг 3: Конвертируем MIDI в ноты Roblox
    notes_text = midi_to_roblox_notes(midi_data)
    
    # Шаг 4: Очищаем временные файлы
    try:
        os.remove(audio_path)
    except:
        pass
    
    return notes_text

# --- КОНВЕРТЕР MIDI → ROBLOX ---
def midi_to_roblox_notes(midi_data):
    """Конвертирует MIDI-данные в текст для Virtual Piano"""
    logger.info("Начало конвертации MIDI в ноты Roblox...")
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

# --- ОБРАБОТЧИКИ ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    logger.info(f"Команда /start от пользователя: {user.username or user.first_name} (ID: {user.id})")
    await update.message.reply_text(
        "🎹 **Zeta Piano Converter**\n\n"
        "✅ Бот работает!\n\n"
        "📝 **Как использовать:**\n"
        "1. Напиши название песни (например, `Shape of You`)\n"
        "2. Или отправь ссылку на YouTube (например, `https://youtu.be/...`)\n\n"
        "⚡ Статус: Онлайн",
        parse_mode='Markdown'
    )

async def handle_song(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_input = update.message.text.strip()
    user = update.effective_user
    logger.info(f"Запрос от {user.username or user.first_name}: {user_input}")
    
    await update.message.chat.send_action(action='typing')
    
    # Проверяем, является ли запрос ссылкой на YouTube
    if re.search(r'(youtube\.com|youtu\.be)', user_input):
        await handle_youtube(update, user_input)
    else:
        await handle_search(update, user_input)

async def handle_search(update: Update, song_name: str):
    """Обработка поиска по названию"""
    status_msg = await update.message.reply_text(f"🔍 Ищу MIDI для '{song_name}'...")
    
    cached = get_from_cache(song_name)
    if cached:
        notes, bpm = cached
        await status_msg.edit_text(
            f"🎵 **{song_name.title()}** (из кэша ⚡)\n"
            f"📊 BPM: {bpm}\n\n"
            f"```\n{notes[:500]}{'...' if len(notes) > 500 else ''}\n```\n"
            f"📝 {len(notes)} нот найдено",
            parse_mode='Markdown'
        )
        return
    
    midi_url = search_midi(song_name)
    if not midi_url:
        await status_msg.edit_text(
            f"❌ Не нашёл MIDI для '{song_name}'.\n\n"
            f"Попробуй:\n"
            f"• Отправить ссылку на YouTube\n"
            f"• Уточнить название\n"
            f"• Попробовать другую песню"
        )
        return
    
    await status_msg.edit_text(f"📥 Скачиваю MIDI...")
    midi_data = download_midi(midi_url)
    if not midi_data:
        await status_msg.edit_text(f"❌ Ошибка скачивания MIDI для '{song_name}'.")
        return
    
    await status_msg.edit_text(f"🔄 Конвертирую MIDI в ноты...")
    notes_text = midi_to_roblox_notes(midi_data)
    if not notes_text:
        await status_msg.edit_text(f"❌ Не удалось сконвертировать '{song_name}'.")
        return
    
    bpm = 120
    save_to_cache(song_name, notes_text, bpm)
    await send_notes(update, song_name, notes_text, bpm, status_msg)

async def handle_youtube(update: Update, youtube_url: str):
    """Обработка ссылки на YouTube"""
    status_msg = await update.message.reply_text("🎵 **YouTube → Roblox**\n\n⏳ Скачиваю аудио с YouTube...")
    
    try:
        # Шаг 1: Скачиваем аудио
        await status_msg.edit_text("🎵 **YouTube → Roblox**\n\n✅ Аудио скачано!\n⏳ Конвертирую в MIDI...")
        
        # Шаг 2: Конвертируем аудио в MIDI
        audio_path = download_audio_from_youtube(youtube_url)
        if not audio_path:
            await status_msg.edit_text("❌ Не удалось скачать аудио с YouTube.\nПроверь ссылку или попробуй другую.")
            return
        
        await status_msg.edit_text("🎵 **YouTube → Roblox**\n\n✅ Аудио скачано!\n✅ MIDI создан!\n⏳ Конвертирую в ноты для Roblox...")
        
        midi_data = audio_to_midi(audio_path)
        if not midi_data:
            await status_msg.edit_text("❌ Не удалось конвертировать аудио в MIDI.\nПопробуй другую песню.")
            try:
                os.remove(audio_path)
            except:
                pass
            return
        
        await status_msg.edit_text("🎵 **YouTube → Roblox**\n\n✅ Аудио скачано!\n✅ MIDI создан!\n✅ Ноты готовы!\n📤 Отправляю...")
        
        # Шаг 3: Конвертируем MIDI в ноты Roblox
        notes_text = midi_to_roblox_notes(midi_data)
        if not notes_text:
            await status_msg.edit_text("❌ Не удалось сконвертировать MIDI в ноты.\nПопробуй другую песню.")
            try:
                os.remove(audio_path)
            except:
                pass
            return
        
        # Шаг 4: Сохраняем в кэш
        song_name = f"youtube_{int(time.time())}"  # Временное имя для кэша
        bpm = 120
        save_to_cache(song_name, notes_text, bpm)
        
        # Шаг 5: Отправляем
        await send_notes(update, "YouTube", notes_text, bpm, status_msg)
        
        # Шаг 6: Очищаем
        try:
            os.remove(audio_path)
        except:
            pass
            
    except Exception as e:
        logger.error(f"Ошибка в YouTube конвейере: {e}")
        await status_msg.edit_text(f"❌ Ошибка: {str(e)[:100]}\nПопробуй другую песню или ссылку.")

async def send_notes(update: Update, song_name: str, notes_text: str, bpm: int, status_msg):
    """Отправляет ноты пользователю"""
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
    else:
        filename = f"{song_name.replace(' ', '_')}_notes.txt"
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

# --- ЗАПУСК БОТА ---
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
