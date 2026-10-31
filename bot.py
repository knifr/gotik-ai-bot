import os
import io
import asyncio
import logging
import math
import urllib.parse
from PIL import Image, ImageDraw, ImageFilter, ImageEnhance, ImageOps
import aiohttp

from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import (
    Message, BufferedInputFile, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
)
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

# ==================== СЛОВАРИ ====================
FILTERS = {
    "пиксельный": "pixel",
    "закатный": "sunset",
    "черно-белый": "bw",
    "черный белый": "bw",
    "чб": "bw",
    "розовый": "pink",
    "теневой": "shadow",
    "тень": "shadow",
}

SHAPES = {
    "квадрат": "square",
    "круг": "circle",
    "треугольник": "triangle",
    "сердце": "heart",
}

# ==================== FSM (Состояния) ====================
class UserState(StatesGroup):
    waiting_image = State()
    waiting_command = State()
    waiting_search = State()
    waiting_generate = State()
    waiting_text_on_photo = State()

user_images: dict[int, Image.Image] = {}

# ==================== КЛАВИАТУРА ====================
def get_main_keyboard():
    kb = [
        [KeyboardButton(text="🖼️ Инструкция по фото"), KeyboardButton(text="🔍 Поиск в интернете")],
        [KeyboardButton(text="🎨 Нарисовать картинку"), KeyboardButton(text="✍️ Добавить текст на фото")],
        [KeyboardButton(text="🔄 Сбросить фото")]
    ]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True, input_field_placeholder="Напиши команду или выбери кнопку...")

# ==================== ПАРСИНГ КОМАНД ====================
def parse_filter_command(text: str):
    lower = text.lower().strip()
    filter_info = None
    shape = None
    dimensions = None

    for rus, eng in FILTERS.items():
        if rus in lower:
            import re
            m = re.search(r"(\d{1,3})\s*%", text)
            intensity = int(m.group(1)) if m else 50
            intensity = max(1, min(100, intensity))
            filter_info = {"filter": eng, "filterName": rus, "intensity": intensity}
            break

    for rus, eng in SHAPES.items():
        if rus in lower:
            shape = {"name": rus, "type": eng}
            break

    import re
    m = re.search(r"размер\s+(\d+)\s*[xх]\s*(\d+)", text, re.IGNORECASE) or \
        re.search(r"(\d+)\s*[xх]\s*(\d+)", text, re.IGNORECASE)
    if m:
        w = max(10, min(2000, int(m.group(1))))
        h = max(10, min(2000, int(m.group(2))))
        dimensions = {"width": w, "height": h}

    return {"filter": filter_info, "shape": shape, "dimensions": dimensions}


# ==================== ФИЛЬТРЫ ====================
def apply_pixel_filter(img, intensity):
    pixel_size = max(3, round(25 * intensity)) 
    w, h = img.size
    src = img.load()
    out = img.copy()
    dst = out.load()
    for y in range(0, h, pixel_size):
        for x in range(0, w, pixel_size):
            cx = min(x + pixel_size // 2, w - 1)
            cy = min(y + pixel_size // 2, h - 1)
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

def apply_pink_filter(img, intensity):
    r, g, b = img.split()[:3]
    r = r.point(lambda v: min(255, int(v * 1.6 + 50)) * intensity + v * (1 - intensity))
    g = g.point(lambda v: int(v * 0.5) * intensity + v * (1 - intensity))
    b = b.point(lambda v: min(255, int(v * 1.4 + 30)) * intensity + v * (1 - intensity))
    return Image.merge("RGB", (r, g, b))

def apply_shadow_filter(img, intensity):
    r, g, b = img.split()[:3]
    r = r.point(lambda v: int(v * 0.4) * intensity + v * (1 - intensity))
    g = g.point(lambda v: int(v * 0.5) * intensity + v * (1 - intensity))
    b = b.point(lambda v: min(255, int(v * 0.8 + 20)) * intensity + v * (1 - intensity))
    return Image.merge("RGB", (r, g, b))

def apply_shape_crop(img, shape_type):
    w, h = img.size
    mask = Image.new("L", (w, h), 0)
    draw = ImageDraw.Draw(mask)
    if shape_type == "circle":
        radius = min(w, h) // 2
        draw.ellipse([w / 2 - radius, h / 2 - radius, w / 2 + radius, h / 2 + radius], fill=255)
    elif shape_type == "square":
        size = min(w, h)
        x, y = (w - size) // 2, (h - size) // 2
        draw.rectangle([x, y, x + size, y + size], fill=255)
    elif shape_type == "triangle":
        draw.polygon([(w / 2, 0), (0, h), (w, h)], fill=255)
    elif shape_type == "heart":
        scale = min(w, h) / 40
        pts = []
        for i in range(101):
            t = i / 100 * 2 * math.pi
            x = 16 * math.sin(t) ** 3
            y = -(13 * math.cos(t) - 5 * math.cos(2 * t) - 2 * math.cos(3 * t) - math.cos(4 * t))
            pts.append((x * scale + w / 2, y * scale + h / 2))
        draw.polygon(pts, fill=255)
    result = img.convert("RGBA")
    result.putalpha(mask)
    return result

def process_image_with_commands(img, filter_info, shape_info, dimensions):
    if dimensions:
        img = img.resize((dimensions["width"], dimensions["height"]), Image.LANCZOS)
    elif img.mode != "RGB":
        img = img.convert("RGB")

    if shape_info:
        img = apply_shape_crop(img, shape_info["type"])

    if img.mode != "RGB":
        base = Image.new("RGB", img.size, (0, 0, 0))
        base.paste(img, mask=img.split()[-1] if img.mode == "RGBA" else None)
        img_rgb = base
    else:
        img_rgb = img

    if filter_info:
        intensity = filter_info["intensity"] / 100
        f = filter_info["filter"]
        if f == "pixel": img_rgb = apply_pixel_filter(img_rgb, intensity)
        elif f == "sunset": img_rgb = apply_sunset_filter(img_rgb, intensity)
        elif f == "bw": img_rgb = apply_bw_filter(img_rgb, intensity)
        elif f == "pink": img_rgb = apply_pink_filter(img_rgb, intensity)
        elif f == "shadow": img_rgb = apply_shadow_filter(img_rgb, intensity)

    if shape_info:
        mask = img.split()[-1] if img.mode == "RGBA" else None
        final = img_rgb.convert("RGBA")
        if mask: final.putalpha(mask)
        return final

    return img_rgb

def image_to_bytes(img, fmt="PNG"):
    buf = io.BytesIO()
    if fmt == "JPEG" and img.mode == "RGBA":
        img = img.convert("RGB")
    img.save(buf, format=fmt)
    buf.seek(0)
    return buf.read()

def describe_command(cmd):
    parts = []
    if cmd["filter"]: parts.append(f"Фильтр: <b>{cmd['filter']['filterName']}</b> ({cmd['filter']['intensity']}%)")
    if cmd["shape"]: parts.append(f"Форма: <b>{cmd['shape']['name']}</b>")
    if cmd["dimensions"]: parts.append(f"Размер: <b>{cmd['dimensions']['width']}×{cmd['dimensions']['height']}</b>")
    return "\n".join(parts) if parts else "Без параметров"


# ==================== ИИ И ПОИСК ====================
async def generate_ai_image(prompt: str) -> bytes:
    encoded_prompt = urllib.parse.quote(prompt)
    url = f"https://image.pollinations.ai/prompt/{encoded_prompt}?width=1024&height=1024&nologo=true"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as response:
            if response.status == 200:
                return await response.read()
            raise Exception("Ошибка API")

async def do_internet_search(query: str) -> str:
    try:
        def fetch():
            with DDGS() as ddgs:
                return list(ddgs.text(query, max_results=5))
        
        loop = asyncio.get_running_loop()
        results = await loop.run_in_executor(None, fetch)
        
        if not results:
            return "Ничего не найдено по вашему запросу."
        
        text = f"🔍 <b>Результаты поиска по запросу:</b> <i>{query}</i>\n\n"
        for i, r in enumerate(results, 1):
            title = r.get('title', 'Без названия')
            body = r.get('body', '')
            href = r.get('href', '')
            text += f"{i}. <a href='{href}'><b>{title}</b></a>\n{body}\n\n"
        return text
    except Exception as e:
        logger.error(f"Ошибка поиска: {e}")
        return "❌ Не удалось выполнить поиск. Попробуйте позже."

# ==================== РОУТЕР ====================
router = Router()

WELCOME = (
    "Привет! Я <b>Gotik AI</b> 🤖\n\n"
    "Я умею:\n"
    "🖼️ <b>Обрабатывать фото</b> (фильтры, формы, размер)\n"
    "🔍 <b>Искать в интернете</b>\n"
    "🎨 <b>Рисовать картинки</b> по описанию\n\n"
    "Просто отправь фото, или напиши команду. Кнопки помогут!"
)

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.set_state(UserState.waiting_image)
    await message.answer(WELCOME, parse_mode=ParseMode.HTML, reply_markup=get_main_keyboard())

@router.message(F.text == "🖼️ Инструкция по фото")
async def btn_photo_help(message: Message):
    await message.answer(
        "📸 <b>Как обрабатывать фото:</b>\n"
        "1. Отправь мне любое фото.\n"
        "2. Напиши команду, например:\n"
        "   • <code>пиксельный 50%</code>\n"
        "   • <code>квадрат</code>\n"
        "   • <code>закатный 75% круг</code>\n"
        "   • <code>размер 300x300</code>",
        parse_mode=ParseMode.HTML
    )

@router.message(F.text == "🔄 Сбросить фото")
async def btn_reset_photo(message: Message, state: FSMContext):
    user_images.pop(message.from_user.id, None)
    await state.set_state(UserState.waiting_image)
    await message.answer("🗑️ Изображение удалено из памяти. Отправь новое, если нужно.")

@router.message(F.text == "🔍 Поиск в интернете")
async def btn_search(message: Message, state: FSMContext):
    await state.set_state(UserState.waiting_search)
    await message.answer("🔍 Введите ваш поисковый запрос:", reply_markup=ReplyKeyboardRemove())

@router.message(F.text == "🎨 Нарисовать картинку")
async def btn_generate(message: Message, state: FSMContext):
    await state.set_state(UserState.waiting_generate)
    await message.answer("🎨 Опишите, что нужно нарисовать (на русском или английском):", reply_markup=ReplyKeyboardRemove())

@router.message(F.text == "✍️ Добавить текст на фото")
async def btn_add_text(message: Message, state: FSMContext):
    uid = message.from_user.id
    if uid not in user_images:
        await message.answer("Сначала отправь фотографию, на которую нужно нанести текст.")
        return
    await state.set_state(UserState.waiting_text_on_photo)
    await message.answer("✍️ Напишите текст, который нужно добавить на фото:", reply_markup=ReplyKeyboardRemove())


@router.message(F.photo)
async def handle_photo(message: Message, state: FSMContext, bot: Bot):
    photo = message.photo[-1]
    file = await bot.get_file(photo.file_id)
    file_bytes = await bot.download_file(file.file_path)
    img = Image.open(io.BytesIO(file_bytes.read()))
    if img.mode != "RGB": img = img.convert("RGB")
    user_images[message.from_user.id] = img
    await state.set_state(UserState.waiting_command)
    await message.answer("✅ Изображение получено! Напиши команду (например: <code>пиксельный 50%</code>)", parse_mode=ParseMode.HTML, reply_markup=get_main_keyboard())


# ==================== ОБРАБОТКА СОСТОЯНИЙ ====================
@router.message(UserState.waiting_search)
async def process_search_query(message: Message, state: FSMContext):
    query = message.text
    status = await message.answer(f"⏳ Ищу информацию по запросу: <i>{query}</i>...", parse_mode=ParseMode.HTML)
    result_text = await do_internet_search(query)
    
    if len(result_text) > 4000:
        for i in range(0, len(result_text), 4000):
            await message.answer(result_text[i:i+4000], parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    else:
        await message.answer(result_text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    
    await status.delete()
    await state.set_state(UserState.waiting_image)
    await message.answer("Что-нибудь еще?", reply_markup=get_main_keyboard())

@router.message(UserState.waiting_generate)
async def process_generate_query(message: Message, state: FSMContext):
    prompt = message.text
    status = await message.answer(f"🎨 <b>Рисую...</b>\nЗапрос: <i>{prompt}</i>\n⏳ Это может занять 10-20 секунд...", parse_mode=ParseMode.HTML)
    try:
        image_bytes = await generate_ai_image(prompt)
        await message.answer_photo(
            BufferedInputFile(image_bytes, filename="ai_image.jpg"),
            caption=f"🖼️ Готово!\nЗапрос: <i>{prompt}</i>",
            parse_mode=ParseMode.HTML
        )
        await status.delete()
    except Exception as e:
        logger.error(f"Ошибка генерации: {e}")
        await status.edit_text("❌ Не удалось сгенерировать картинку. Попробуй другой запрос.")
    
    await state.set_state(UserState.waiting_image)
    await message.answer("Что-нибудь еще?", reply_markup=get_main_keyboard())

@router.message(UserState.waiting_text_on_photo)
async def process_add_text(message: Message, state: FSMContext):
    custom_text = message.text
    uid = message.from_user.id
    img = user_images.get(uid)
    
    if not img:
        await message.answer("Фото потерялось. Загрузите заново.")
        await state.set_state(UserState.waiting_image)
        return

    status = await message.answer("✍️ Накладываю текст...")
    
    loop = asyncio.get_running_loop()
    def add_text():
        img_copy = img.copy().convert("RGB")
        draw = ImageDraw.Draw(img_copy)
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", size=int(img_copy.width * 0.05))
        except:
            font = ImageFont.load_default()
        
        bbox = draw.textbbox((0, 0), custom_text, font=font)
        text_w, text_h = bbox[2] - bbox[0], bbox[3] - bbox[1]
        x = (img_copy.width - text_w) / 2
        y = img_copy.height - text_h - 20
        
        draw.text((x-2, y), custom_text, font=font, fill="black")
        draw.text((x+2, y), custom_text, font=font, fill="black")
        draw.text((x, y-2), custom_text, font=font, fill="black")
        draw.text((x, y+2), custom_text, font=font, fill="black")
        draw.text((x, y), custom_text, font=font, fill="white")
        return img_copy

    result = await loop.run_in_executor(None, add_text)
    data = image_to_bytes(result, "JPEG")
    await message.answer_photo(BufferedInputFile(data, filename="text_added.jpg"), caption="✅ Текст добавлен!")
    await status.delete()
    
    await state.set_state(UserState.waiting_image)
    await message.answer("Что-нибудь еще?", reply_markup=get_main_keyboard())


# ==================== ОБЫЧНЫЙ ТЕКСТ / ФИЛЬТРЫ ====================
@router.message(F.text)
async def handle_text(message: Message, state: FSMContext, bot: Bot):
    text = message.text
    lower = text.lower().strip()
    uid = message.from_user.id
    
    if lower.startswith("поиск ") or lower.startswith("найди "):
        query = text.split(" ", 1)[1]
        status = await message.answer(f"⏳ Ищу: <i>{query}</i>...", parse_mode=ParseMode.HTML)
        res = await do_internet_search(query)
        await message.answer(res, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        await status.delete()
        return

    if lower.startswith("нарисуй "):
        prompt = text[8:].strip()
        status = await message.answer(f"🎨 <b>Рисую...</b>\n{prompt}\n⏳ Подожди...", parse_mode=ParseMode.HTML)
        try:
            image_bytes = await generate_ai_image(prompt)
            await message.answer_photo(BufferedInputFile(image_bytes, filename="ai_image.jpg"), caption=f"🖼️ {prompt}")
            await status.delete()
        except Exception as e:
            await status.edit_text("❌ Ошибка генерации.")
        return

    img = user_images.get(uid)
    if not img:
        await message.answer("Сначала отправь изображение 📷, или используй кнопки внизу.", reply_markup=get_main_keyboard())
        return

    cmd = parse_filter_command(text)
    if not (cmd["filter"] or cmd["shape"] or cmd["dimensions"]):
        await message.answer("Не понял команду. Примеры:\n• <code>пиксельный 50%</code>\n• <code>квадрат</code>", parse_mode=ParseMode.HTML)
        return

    desc = describe_command(cmd)
    status = await message.answer(f"🖼️ <b>Обработка...</b>\n{desc}", parse_mode=ParseMode.HTML)
    
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(
        None, process_image_with_commands, 
        img.copy(), cmd["filter"], cmd["shape"], cmd["dimensions"]
    )

    fmt = "PNG" if result.mode == "RGBA" else "JPEG"
    ext = "png" if fmt == "PNG" else "jpg"
    data = image_to_bytes(result, fmt)

    await message.answer_photo(
        BufferedInputFile(data, filename=f"result.{ext}"),
        caption=f"✅ <b>Готово!</b>\n{desc}",
        parse_mode=ParseMode.HTML,
    )
    await status.delete()
    await state.set_state(UserState.waiting_image)


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
