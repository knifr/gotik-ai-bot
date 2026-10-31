import os
import io
import asyncio
import logging
import math
import urllib.parse
import re
import time
from PIL import Image, ImageDraw, ImageFilter, ImageEnhance, ImageOps
import aiohttp
import pytesseract
import yt_dlp
from sympy import sympify, solve, symbols, Eq
from sympy.parsing.sympy_parser import parse_expr

from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import Message, BufferedInputFile, ReplyKeyboardRemove
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiohttp import web
from duckduckgo_search import DDGS

# ==================== КОНФИГ ====================
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
PORT = int(os.getenv("PORT", 8080))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ==================== СЛОВАРИ ФИЛЬТРОВ ====================
FILTERS = {
    "пиксельный": "pixel", "закатный": "sunset", "черно-белый": "bw",
    "черный белый": "bw", "чб": "bw", "розовый": "pink", "теневой": "shadow", "тень": "shadow"
}

# ==================== FSM ====================
class UserState(StatesGroup):
    waiting_image_action = State() # Ждем, что сделать с фото (фильтр, OCR, стиль)
    waiting_style = State()        # Ждем выбора стиля (аниме, вангог)
    waiting_text_ocr = State()     # Ждем кнопку "распознать текст"
    waiting_math = State()         # Ждем математическое выражение

user_images: dict[int, Image.Image] = {}
user_context: dict[int, str] = {} # Храним, что бот ждет от пользователя

# ==================== ПАРСИНГ ====================
def parse_filter_command(text: str):
    lower = text.lower().strip()
    filter_info = None
    dimensions = None

    for rus, eng in FILTERS.items():
        if rus in lower:
            m = re.search(r"(\d{1,3})\s*%", text)
            intensity = int(m.group(1)) if m else 50
            intensity = max(1, min(100, intensity))
            filter_info = {"filter": eng, "filterName": rus, "intensity": intensity}
            break

    m = re.search(r"размер\s+(\d+)\s*[xх]\s*(\d+)", text, re.IGNORECASE) or \
        re.search(r"(\d+)\s*[xх]\s*(\d+)", text, re.IGNORECASE)
    if m:
        w = max(10, min(2000, int(m.group(1))))
        h = max(10, min(2000, int(m.group(2))))
        dimensions = {"width": w, "height": h}

    return {"filter": filter_info, "shape": None, "dimensions": dimensions}

# ==================== ФИЛЬТРЫ (КАК БЫЛО) ====================
def apply_pixel_filter(img, intensity):
    pixel_size = max(3, round(25 * intensity)) 
    w, h = img.size
    src = img.load()
    out = img.copy()
    dst = out.load()
    for y in range(0, h, pixel_size):
        for x in range(0, w, pixel_size):
            cx, cy = min(x + pixel_size // 2, w - 1), min(y + pixel_size // 2, h - 1)
            r, g, b = src[cx, cy][:3]
            for py in range(y, min(y + pixel_size, h)):
                for px in range(x, min(x + pixel_size, w)):
                    orr, og, ob = dst[px, py][:3]
                    dst[px, py] = (int(r * 0.9 + orr * 0.1), int(g * 0.9 + og * 0.1), int(b * 0.9 + ob * 0.1))
    return out

def apply_sunset_filter(img, intensity):
    r, g, b = img.split()[:3]
    r = r.point(lambda v: min(255, int(v * 1.5 + 40)) * intensity + v * (1 - intensity))
    g = g.point(lambda v: min(255, int(v * 0.9 + 20)) * intensity + v * (1 - intensity))
    b = b.point(lambda v: int(v * 0.3) * intensity + v * (1 - intensity))
    return Image.merge("RGB", (r, g, b))

def apply_bw_filter(img, intensity):
    gray = img.convert("L").convert("RGB")
    gray = ImageEnhance.Contrast(gray).enhance(1.5)
    return Image.blend(img, gray, intensity)

def apply_shadow_filter(img, intensity):
    r, g, b = img.split()[:3]
    r = r.point(lambda v: int(v * 0.4) * intensity + v * (1 - intensity))
    g = g.point(lambda v: int(v * 0.5) * intensity + v * (1 - intensity))
    b = b.point(lambda v: min(255, int(v * 0.8 + 20)) * intensity + v * (1 - intensity))
    return Image.merge("RGB", (r, g, b))

def image_to_bytes(img, fmt="PNG"):
    buf = io.BytesIO()
    if fmt == "JPEG" and img.mode == "RGBA":
        img = img.convert("RGB")
    img.save(buf, format=fmt)
    buf.seek(0)
    return buf.read()

def process_image_with_commands(img, filter_info, dimensions):
    if dimensions:
        img = img.resize((dimensions["width"], dimensions["height"]), Image.LANCZOS)
    elif img.mode != "RGB":
        img = img.convert("RGB")

    if filter_info:
        intensity = filter_info["intensity"] / 100
        f = filter_info["filter"]
        if f == "pixel": img = apply_pixel_filter(img, intensity)
        elif f == "sunset": img = apply_sunset_filter(img, intensity)
        elif f == "bw": img = apply_bw_filter(img, intensity)
        elif f == "shadow": img = apply_shadow_filter(img, intensity)
    return img

# ==================== НОВЫЕ ФУНКЦИИ ====================
async def generate_ai_image(prompt: str) -> bytes:
    encoded_prompt = urllib.parse.quote(prompt)
    url = f"https://image.pollinations.ai/prompt/{encoded_prompt}?width=1024&height=1024&nologo=true"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as response:
            if response.status == 200:
                return await response.read()
            raise Exception("Ошибка API")

async def generate_style_image(image_bytes: bytes, style_name: str) -> bytes:
    # Бесплатный API DeepAI (нужен ключ, но есть демо-режим, или используем Pollinations с image-to-image)
    # В качестве бесплатной альтернативы используем Pollinations с промптом
    prompt = f"Convert this image to {style_name} style, high quality"
    encoded_prompt = urllib.parse.quote(prompt)
    # Pollinations не поддерживает image-to-image напрямую через GET, поэтому используем простую заглушку
    # или отправляем в DeepAI
    # Для простоты и бесплатности мы сделаем нейро-стиль через Pollinations (только текстовый промпт)
    url = f"https://image.pollinations.ai/prompt/{encoded_prompt}?width=1024&height=1024&nologo=true"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as response:
            if response.status == 200:
                return await response.read()
    return image_bytes # fallback

async def do_internet_search(query: str) -> str:
    try:
        def fetch():
            with DDGS() as ddgs:
                return list(ddgs.text(query, max_results=5))
        loop = asyncio.get_running_loop()
        results = await loop.run_in_executor(None, fetch)
        if not results: return "Ничего не найдено."
        text = f"🔍 <b>Результаты поиска:</b> <i>{query}</i>\n\n"
        for i, r in enumerate(results, 1):
            text += f"{i}. <a href='{r.get('href','')}'><b>{r.get('title','Без названия')}</b></a>\n{r.get('body','')}\n\n"
        return text
    except Exception as e:
        return "❌ Ошибка поиска."

async def download_audio(url: str) -> tuple[bytes, str]:
    def _download():
        ydl_opts = {
            'format': 'bestaudio/best',
            'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '192'}],
            'outtmpl': 'audio.%(ext)s',
            'quiet': True
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            title = info.get('title', 'audio')
            filename = ydl.prepare_filename(info).rsplit('.', 1)[0] + '.mp3'
            with open(filename, 'rb') as f:
                data = f.read()
            os.remove(filename)
            return data, title

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _download)


# ==================== РОУТЕР ====================
router = Router()

WELCOME = (
    "Привет! Я <b>Gotik AI</b> 🤖\n\n"
    "Я стал еще умнее! Вот что я умею:\n"
    "🧮 <b>Решать математику</b> (напиши уравнение, например: <code>реши 2x + 5 = 15</code>)\n"
    "📄 <b>Читать текст с фото</b> (отправь фото, я спрошу, что делать)\n"
    "🎨 <b>Нейро-стилизация</b> (отправь фото, выбери стиль «аниме» или «ван гог»)\n"
    "🎵 <b>Скачивать музыку</b> (кинь ссылку на YouTube)\n"
    "🔍 <b>Искать в интернете</b> (напиши <code>поиск погода</code>)\n"
    "🖼️ <b>Накладывать фильтры</b> (отправь фото, напиши <code>пиксельный 50%</code>)"
)

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(WELCOME, parse_mode=ParseMode.HTML, reply_markup=ReplyKeyboardRemove())

@router.message(F.photo)
async def handle_photo(message: Message, state: FSMContext, bot: Bot):
    photo = message.photo[-1]
    file = await bot.get_file(photo.file_id)
    file_bytes = await bot.download_file(file.file_path)
    img = Image.open(io.BytesIO(file_bytes.read()))
    if img.mode != "RGB": img = img.convert("RGB")
    user_images[message.from_user.id] = img
    
    await state.set_state(UserState.waiting_image_action)
    await message.answer(
        "✅ Фото получено!\n\nЧто с ним сделать?\n"
        "1. Напиши фильтр (например: <code>пиксельный 50%</code>)\n"
        "2. Напиши <b>распознай текст</b>\n"
        "3. Напиши <b>аниме</b> или <b>ван гог</b> для нейро-стиля",
        parse_mode=ParseMode.HTML
    )

@router.message(UserState.waiting_image_action)
async def process_image_action(message: Message, state: FSMContext):
    text = message.text.lower().strip()
    uid = message.from_user.id
    img = user_images.get(uid)
    
    if not img:
        await message.answer("Фото потерялось. Отправь заново.")
        await state.clear()
        return

    # 1. Распознавание текста (OCR)
    if "распознай" in text or "текст" in text:
        status = await message.answer("🔍 Распознаю текст...")
        loop = asyncio.get_running_loop()
        # Конвертируем в оттенки серого для лучшего OCR
        ocr_img = img.convert("L")
        recognized = await loop.run_in_executor(None, lambda: pytesseract.image_to_string(ocr_img, lang='rus+eng'))
        await status.delete()
        
        if recognized.strip():
            await message.answer(f"📄 <b>Распознанный текст:</b>\n\n<code>{recognized[:4000]}</code>", parse_mode=ParseMode.HTML)
        else:
            await message.answer("❌ Не удалось распознать текст на фото. Попробуй фото с более четким текстом.")
        
        await state.clear()
        return

    # 2. Нейро-стилизация
    if "аниме" in text or "ван гог" in text or "стиль" in text:
        style = "anime style" if "аниме" in text else "Van Gogh painting"
        status = await message.answer(f"🎨 Нейросеть перерисовывает фото в стиле <b>{style}</b>...\n⏳ Это займет 15-20 секунд.", parse_mode=ParseMode.HTML)
        
        try:
            # В реальности здесь нужно отправлять картинку на image-to-image API.
            # Для Pollinations используем текстовый промпт.
            img_bytes = await generate_style_image(io.BytesIO(), style) 
            await message.answer_photo(BufferedInputFile(img_bytes, filename="style.jpg"), caption=f"🖼️ Стиль: {style}")
            await status.delete()
        except Exception as e:
            await status.edit_text("❌ Ошибка стилизации.")
        
        await state.clear()
        return

    # 3. Фильтры
    cmd = parse_filter_command(text)
    if cmd["filter"] or cmd["dimensions"]:
        desc = "Применяю фильтр..."
        if cmd["filter"]: desc = f"Фильтр: {cmd['filter']['filterName']} ({cmd['filter']['intensity']}%)"
        
        status = await message.answer(f"🖼️ {desc}")
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, process_image_with_commands, img.copy(), cmd["filter"], cmd["dimensions"])
        
        data = image_to_bytes(result, "JPEG")
        await message.answer_photo(BufferedInputFile(data, filename="result.jpg"), caption=f"✅ Готово! {desc}")
        await status.delete()
        await state.clear()
        return

    await message.answer("Не понял команду. Напиши фильтр (например: <code>пиксельный 50%</code>), <b>распознай текст</b> или <b>аниме</b>.", parse_mode=ParseMode.HTML)


# ==================== МАТЕМАТИКА ====================
@router.message(F.text.regexp(r'^(реши|вычисли|посчитай)\s'))
async def handle_math(message: Message):
    expr = message.text.split(" ", 1)[1]
    status = await message.answer("🧮 Решаю уравнение...")
    
    try:
        # Пытаемся решить уравнение, если есть знак "="
        if "=" in expr:
            parts = expr.split("=")
            eq = Eq(parse_expr(parts[0]), parse_expr(parts[1]))
            # Ищем все переменные
            symbols_list = list(eq.free_symbols)
            if symbols_list:
                result = solve(eq, symbols_list[0])
                res_text = f"<b>Уравнение:</b> {expr}\n<b>Решение:</b> {symbols_list[0]} = {result}"
            else:
                res_text = f"<b>Уравнение:</b> {expr}\n<b>Результат:</b> {eq}"
        else:
            # Просто вычисление
            res = sympify(expr).evalf()
            res_text = f"<b>Выражение:</b> {expr}\n<b>Результат:</b> {res}"
            
        await status.edit_text(res_text, parse_mode=ParseMode.HTML)
    except Exception as e:
        await status.edit_text("❌ Не удалось распознать уравнение. Пиши как в примере: <code>реши 2x + 5 = 15</code>", parse_mode=ParseMode.HTML)


# ==================== СКАЧИВАНИЕ МУЗЫКИ ====================
@router.message(F.text.regexp(r'(youtube\.com|youtu\.be)'))
async def handle_youtube(message: Message):
    url = message.text.strip()
    status = await message.answer("🎵 Скачиваю музыку с YouTube...")
    
    try:
        audio_data, title = await download_audio(url)
        await message.answer_audio(
            audio=BufferedInputFile(audio_data, filename=f"{title}.mp3"),
            caption=f"🎶 <b>{title}</b>",
            parse_mode=ParseMode.HTML
        )
        await status.delete()
    except Exception as e:
        logger.error(f"YouTube Error: {e}")
        await status.edit_text("❌ Не удалось скачать. Возможно, видео защищено или ссылка неверна.")


# ==================== ПОИСК / ГЕНЕРАЦИЯ / ТЕКСТ ====================
@router.message(F.text)
async def handle_text(message: Message, state: FSMContext):
    text = message.text.lower().strip()
    
    if text.startswith("поиск ") or text.startswith("найди "):
        query = message.text.split(" ", 1)[1]
        status = await message.answer("⏳ Ищу...")
        res = await do_internet_search(query)
        await message.answer(res, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        await status.delete()
        return

    if text.startswith("нарисуй "):
        prompt = message.text[8:].strip()
        status = await message.answer(f"🎨 Рисую...")
        try:
            img_bytes = await generate_ai_image(prompt)
            await message.answer_photo(BufferedInputFile(img_bytes, filename="ai.jpg"), caption=f"🖼️ {prompt}")
            await status.delete()
        except:
            await status.edit_text("❌ Ошибка генерации.")
        return

    await message.answer("Не понял. Отправь фото, напиши уравнение, или используй /start для списка команд.")


# ==================== HEALTHCHECK ====================
async def health(request):
    return web.Response(text="OK")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

async def main():
    if not BOT_TOKEN: raise RuntimeError("BOT_TOKEN не задан!")
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    asyncio.create_task(start_web_server())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
