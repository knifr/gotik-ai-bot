import os
import io
import asyncio
import logging
import math
import random
from PIL import Image, ImageDraw, ImageFilter, ImageEnhance

from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import Message, BufferedInputFile, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiohttp import web

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

class UserState(StatesGroup):
    waiting_image = State()
    waiting_command = State()

user_images: dict[int, Image.Image] = {}

# ==================== ПАРСИНГ ====================
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


# ==================== УЛУЧШЕННЫЕ ФИЛЬТРЫ ====================
def apply_pixel_filter(img: Image.Image, intensity: float) -> Image.Image:
    # Увеличили базовый размер пикселя, чтобы эффект был заметнее
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
                    # Делаем смешивание более резким
                    orr, og, ob = dst[px, py][:3]
                    dst[px, py] = (
                        int(r * 0.9 + orr * 0.1),
                        int(g * 0.9 + og * 0.1),
                        int(b * 0.9 + ob * 0.1),
                    )
    return out

def apply_sunset_filter(img: Image.Image, intensity: float) -> Image.Image:
    r, g, b = img.split()[:3]
    # Усилили красный и желтый
    r = r.point(lambda v: min(255, int(v * 1.5 + 40)) * intensity + v * (1 - intensity))
    g = g.point(lambda v: min(255, int(v * 0.9 + 20)) * intensity + v * (1 - intensity))
    b = b.point(lambda v: int(v * 0.3) * intensity + v * (1 - intensity))
    return Image.merge("RGB", (r, g, b))

def apply_bw_filter(img: Image.Image, intensity: float) -> Image.Image:
    gray = img.convert("L").convert("RGB")
    # Усилили контраст для ч/б
    enhancer = ImageEnhance.Contrast(gray)
    gray = enhancer.enhance(1.5)
    return Image.blend(img, gray, intensity)

def apply_pink_filter(img: Image.Image, intensity: float) -> Image.Image:
    r, g, b = img.split()[:3]
    r = r.point(lambda v: min(255, int(v * 1.6 + 50)) * intensity + v * (1 - intensity))
    g = g.point(lambda v: int(v * 0.5) * intensity + v * (1 - intensity))
    b = b.point(lambda v: min(255, int(v * 1.4 + 30)) * intensity + v * (1 - intensity))
    return Image.merge("RGB", (r, g, b))

def apply_shadow_filter(img: Image.Image, intensity: float) -> Image.Image:
    # Затемнение с синеватым оттенком
    r, g, b = img.split()[:3]
    r = r.point(lambda v: int(v * 0.4) * intensity + v * (1 - intensity))
    g = g.point(lambda v: int(v * 0.5) * intensity + v * (1 - intensity))
    b = b.point(lambda v: min(255, int(v * 0.8 + 20)) * intensity + v * (1 - intensity))
    return Image.merge("RGB", (r, g, b))


def apply_shape_crop(img: Image.Image, shape_type: str) -> Image.Image:
    w, h = img.size
    mask = Image.new("L", (w, h), 0)
    draw = ImageDraw.Draw(mask)

    if shape_type == "circle":
        radius = min(w, h) // 2
        draw.ellipse([w / 2 - radius, h / 2 - radius, w / 2 + radius, h / 2 + radius], fill=255)
    elif shape_type == "square":
        size = min(w, h)
        x = (w - size) // 2
        y = (h - size) // 2
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


def process_image_with_commands(img: Image.Image, filter_info, shape_info, dimensions):
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
        if f == "pixel":
            img_rgb = apply_pixel_filter(img_rgb, intensity)
        elif f == "sunset":
            img_rgb = apply_sunset_filter(img_rgb, intensity)
        elif f == "bw":
            img_rgb = apply_bw_filter(img_rgb, intensity)
        elif f == "pink":
            img_rgb = apply_pink_filter(img_rgb, intensity)
        elif f == "shadow":
            img_rgb = apply_shadow_filter(img_rgb, intensity)

    if shape_info:
        mask = img.split()[-1] if img.mode == "RGBA" else None
        final = img_rgb.convert("RGBA")
        if mask:
            final.putalpha(mask)
        return final

    return img_rgb


def image_to_bytes(img: Image.Image, fmt="PNG") -> bytes:
    buf = io.BytesIO()
    if fmt == "JPEG" and img.mode == "RGBA":
        img = img.convert("RGB")
    img.save(buf, format=fmt)
    buf.seek(0)
    return buf.read()

def describe_command(cmd) -> str:
    parts = []
    if cmd["filter"]:
        s = f"Фильтр: <b>{cmd['filter']['filterName']}</b> ({cmd['filter']['intensity']}%)"
        parts.append(s)
    if cmd["shape"]:
        parts.append(f"Форма: <b>{cmd['shape']['name']}</b>")
    if cmd["dimensions"]:
        parts.append(f"Размер: <b>{cmd['dimensions']['width']}×{cmd['dimensions']['height']}</b>")
    return "\n".join(parts) if parts else "Без параметров"


# ==================== РОУТЕР ====================
router = Router()

WELCOME = (
    "Привет! Я <b>Gotik AI</b> 🤖\n\n"
    "Могу помочь с обработкой изображений:\n"
    "📚 Математика, физика, химия, электротехника\n"
    "🖼️ Обработка изображений\n\n"
    "<b>Как работать с фото:</b>\n"
    "1. Отправь изображение\n"
    "2. Напиши команду\n\n"
    "<b>Доступные команды:</b>\n"
    "• <b>Фильтры:</b> «пиксельный 50%», «закатный 75%», «черно-белый»\n"
    "• <b>Формы:</b> «квадрат», «круг», «треугольник», «сердце»\n"
    "• <b>Размер:</b> «размер 300x300»\n"
    "• <b>Комбинации:</b> «пиксельный 50% квадрат»"
)

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.set_state(UserState.waiting_image)
    await message.answer(WELCOME, parse_mode=ParseMode.HTML)

@router.message(F.photo)
async def handle_photo(message: Message, state: FSMContext, bot: Bot):
    photo = message.photo[-1]
    file = await bot.get_file(photo.file_id)
    file_bytes = await bot.download_file(file.file_path)
    img = Image.open(io.BytesIO(file_bytes.read()))
    
    # Конвертируем в RGB на всякий случай (чтобы не было ошибок с палитрой)
    if img.mode != "RGB":
        img = img.convert("RGB")
        
    user_images[message.from_user.id] = img
    await state.set_state(UserState.waiting_command)

    await message.answer("✅ Изображение получено! Напиши команду (например: <code>пиксельный 50%</code>)", parse_mode=ParseMode.HTML)


# ==================== АНИМАЦИЯ ОБРАБОТКИ ====================
async def simulate_processing(message: Message, bot: Bot, cmd_desc: str):
    # Этап 1
    msg = await message.answer(f"🖼️ <b>Начинаю обработку...</b>\n\n{cmd_desc}\n\n[░░░░░░░░░░] 0%")
    await asyncio.sleep(1.5)
    
    # Этап 2 (Редактируем сообщение)
    await msg.edit_text(f"🖼️ <b>Начинаю обработку...</b>\n\n{cmd_desc}\n\n[███░░░░░░░] 30%\n🔍 <i>Анализ изображения...</i>", parse_mode=ParseMode.HTML)
    
    # Отправляем гифку, если она есть в папке
    if os.path.exists("loading.gif"):
        try:
            await message.answer_animation(
                animation=BufferedInputFile(open("loading.gif", "rb").read(), filename="loading.gif"),
                caption="⚙️ Применяю эффекты..."
            )
        except Exception as e:
            logger.error(f"Не удалось отправить гифку: {e}")
            
    await asyncio.sleep(2)
    
    # Этап 3
    await msg.edit_text(f"🖼️ <b>Начинаю обработку...</b>\n\n{cmd_desc}\n\n[██████░░░░] 60%\n⚙️ <i>Наложение фильтров...</i>", parse_mode=ParseMode.HTML)
    await asyncio.sleep(2)
    
    await msg.edit_text(f"🖼️ <b>Начинаю обработку...</b>\n\n{cmd_desc}\n\n[█████████░] 90%\n✨ <i>Финальная обработка...</i>", parse_mode=ParseMode.HTML)
    await asyncio.sleep(1)
    
    await msg.edit_text(f"🖼️ <b>Начинаю обработку...</b>\n\n{cmd_desc}\n\n[██████████] 100%\n✅ <i>Готово!</i>", parse_mode=ParseMode.HTML)
    return msg


@router.message(F.text)
async def handle_text(message: Message, state: FSMContext, bot: Bot):
    text = message.text
    uid = message.from_user.id
    img = user_images.get(uid)
    
    if not img:
        await message.answer("Сначала отправь изображение 📷, затем напиши команду.")
        return

    cmd = parse_filter_command(text)
    if not (cmd["filter"] or cmd["shape"] or cmd["dimensions"]):
        await message.answer("Не понял команду. Примеры:\n• <code>пиксельный 50%</code>\n• <code>квадрат</code>", parse_mode=ParseMode.HTML)
        return

    # Запускаем анимацию
    desc = describe_command(cmd)
    status_msg = await simulate_processing(message, bot, desc)

    try:
        # Обрабатываем в фоне (чтобы бот не завис на больших фото)
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None, 
            process_image_with_commands, 
            img.copy(), cmd["filter"], cmd["shape"], cmd["dimensions"]
        )

        fmt = "PNG" if result.mode == "RGBA" else "JPEG"
        ext = "png" if fmt == "PNG" else "jpg"
        data = image_to_bytes(result, fmt)

        # Отправляем финальный результат
        await message.answer_photo(
            BufferedInputFile(data, filename=f"gotik_ai_result.{ext}"),
            caption=f"✅ <b>Обработка завершена</b>\n\n{desc}",
            parse_mode=ParseMode.HTML,
        )
        
        # Удаляем сообщение с прогресс-баром, чтобы не мусорить
        await status_msg.delete()
        await state.set_state(UserState.waiting_image)

    except Exception as e:
        logger.exception("Ошибка обработки: %s", e)
        await status_msg.edit_text("❌ Ошибка при обработке изображения.")

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
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN не задан!")
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    asyncio.create_task(start_web_server())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
