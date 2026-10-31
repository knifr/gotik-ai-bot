import os
import io
import asyncio
import logging
import math
from PIL import Image, ImageDraw, ImageFilter, ImageEnhance

from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import Message, BufferedInputFile, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiohttp import web

# ==================== КОНФИГ ====================
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
PORT = int(os.getenv("PORT", 8080))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ==================== СЛОВАРИ (из HTML) ====================
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

# ==================== FSM ====================
class UserState(StatesGroup):
    waiting_image = State()
    waiting_command = State()

# Хранилище последнего изображения пользователя (в памяти)
user_images: dict[int, Image.Image] = {}

# ==================== ПАРСИНГ КОМАНДЫ ====================
def parse_filter_command(text: str):
    """Полный аналог parseFilterCommand из HTML."""
    lower = text.lower().strip()
    filter_info = None
    shape = None
    dimensions = None

    # Фильтры
    for rus, eng in FILTERS.items():
        if rus in lower:
            import re
            m = re.search(r"(\d{1,3})\s*%", text)
            intensity = int(m.group(1)) if m else 50
            intensity = max(1, min(100, intensity))
            filter_info = {"filter": eng, "filterName": rus, "intensity": intensity}
            break

    # Формы
    for rus, eng in SHAPES.items():
        if rus in lower:
            shape = {"name": rus, "type": eng}
            break

    # Размер
    import re
    m = re.search(r"размер\s+(\d+)\s*[xх]\s*(\d+)", text, re.IGNORECASE) or \
        re.search(r"(\d+)\s*[xх]\s*(\d+)", text, re.IGNORECASE)
    if m:
        w = max(10, min(2000, int(m.group(1))))
        h = max(10, min(2000, int(m.group(2))))
        dimensions = {"width": w, "height": h}

    return {"filter": filter_info, "shape": shape, "dimensions": dimensions}


# ==================== ФИЛЬТРЫ (порт из JS) ====================
def apply_pixel_filter(img: Image.Image, intensity: float) -> Image.Image:
    """Пиксельный фильтр — усреднение блоков + смешивание."""
    pixel_size = max(2, round(10 * intensity))
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
                    dst[px, py] = (
                        int(r * intensity + orr * (1 - intensity)),
                        int(g * intensity + og * (1 - intensity)),
                        int(b * intensity + ob * (1 - intensity)),
                    )
    return out


def apply_sunset_filter(img: Image.Image, intensity: float) -> Image.Image:
    r, g, b = img.split()[:3]
    r = r.point(lambda v: min(255, int(v * 1.3 + 30)) * intensity + v * (1 - intensity))
    g = g.point(lambda v: int(v * 0.8 + 10) * intensity + v * (1 - intensity))
    b = b.point(lambda v: int(v * 0.6) * intensity + v * (1 - intensity))
    return Image.merge("RGB", (r, g, b))


def apply_bw_filter(img: Image.Image, intensity: float) -> Image.Image:
    gray = img.convert("L").convert("RGB")
    return Image.blend(img, gray, intensity)


def apply_pink_filter(img: Image.Image, intensity: float) -> Image.Image:
    r, g, b = img.split()[:3]
    r = r.point(lambda v: min(255, int(v * 1.4 + 40)) * intensity + v * (1 - intensity))
    g = g.point(lambda v: int(v * 0.7) * intensity + v * (1 - intensity))
    b = b.point(lambda v: min(255, int(v * 1.2 + 20)) * intensity + v * (1 - intensity))
    return Image.merge("RGB", (r, g, b))


def apply_shadow_filter(img: Image.Image, intensity: float) -> Image.Image:
    r, g, b = img.split()[:3]
    def contrast(v, mult, add, c=1.2):
        return max(0, min(255, int(((v * mult * add - 128) * c) + 128)))
    r = r.point(lambda v: contrast(v, 0.6, 0.9) * intensity + v * (1 - intensity))
    g = g.point(lambda v: contrast(v, 0.6, 0.8) * intensity + v * (1 - intensity))
    b = b.point(lambda v: contrast(v, 0.6, 1.1) * intensity + v * (1 - intensity))
    return Image.merge("RGB", (r, g, b))


def apply_shape_crop(img: Image.Image, shape_type: str) -> Image.Image:
    """Обрезка по форме (круг, квадрат, треугольник, сердце) с прозрачным фоном."""
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
        # Аппроксимация сердца через параметрическое уравнение
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
    """Полный аналог processImageWithCommands из JS."""
    # 1. Ресайз
    if dimensions:
        img = img.resize((dimensions["width"], dimensions["height"]), Image.LANCZOS)
    elif img.mode != "RGB":
        img = img.convert("RGB")

    # 2. Обрезка по форме (делаем после ресайза)
    if shape_info:
        img = apply_shape_crop(img, shape_info["type"])

    # Убедимся, что для фильтров RGB
    if img.mode != "RGB":
        base = Image.new("RGB", img.size, (0, 0, 0))
        base.paste(img, mask=img.split()[-1] if img.mode == "RGBA" else None)
        img_rgb = base
    else:
        img_rgb = img

    # 3. Фильтры
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

    # Если была форма — вернём RGBA (с прозрачностью по маске)
    if shape_info:
        # Пересобираем с маской
        mask = img.split()[-1] if img.mode == "RGBA" else None
        final = img_rgb.convert("RGBA")
        if mask:
            final.putalpha(mask)
        return final

    return img_rgb


# ==================== ХЕЛПЕРЫ ====================
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
    "• <b>Только фильтры:</b> «пиксельный 50%», «закатный 75%», «черно-белый»\n"
    "• <b>Только обрезка:</b> «квадрат», «круг», «треугольник», «сердце»\n"
    "• <b>Только размер:</b> «размер 300x300», «500x500»\n"
    "• <b>Комбинации:</b> «пиксельный 50% квадрат», «размер 800x600 круг»\n\n"
    "✈️ /telegram — открыть Telegram Web"
)


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.set_state(UserState.waiting_image)
    await message.answer(WELCOME, parse_mode=ParseMode.HTML)


@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(WELCOME, parse_mode=ParseMode.HTML)


@router.message(Command("telegram"))
async def cmd_telegram(message: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Открыть Telegram Web", url="https://web.telegram.org/")]
    ])
    await message.answer("Нажми кнопку ниже:", reply_markup=kb)


@router.message(F.photo)
async def handle_photo(message: Message, state: FSMContext, bot: Bot):
    # Берём максимальное разрешение
    photo = message.photo[-1]
    file = await bot.get_file(photo.file_id)
    file_bytes = await bot.download_file(file.file_path)
    img = Image.open(io.BytesIO(file_bytes.read()))

    user_images[message.from_user.id] = img
    await state.set_state(UserState.waiting_command)

    await message.answer(
        "✅ Изображение получено!\n\n"
        "Теперь напиши команду обработки. Примеры:\n"
        "• <code>пиксельный 50%</code>\n"
        "• <code>квадрат</code>\n"
        "• <code>размер 300x300</code>\n"
        "• <code>пиксельный 50% квадрат</code>\n"
        "• <code>закатный 75% круг</code>",
        parse_mode=ParseMode.HTML,
    )


@router.message(F.document)
async def handle_document(message: Message, state: FSMContext, bot: Bot):
    doc = message.document
    if not doc.mime_type or not doc.mime_type.startswith("image/"):
        await message.answer("Пожалуйста, отправь файл изображения (JPG, PNG, GIF).")
        return
    file = await bot.get_file(doc.file_id)
    file_bytes = await bot.download_file(file.file_path)
    try:
        img = Image.open(io.BytesIO(file_bytes.read()))
    except Exception:
        await message.answer("Не удалось прочитать изображение.")
        return
    user_images[message.from_user.id] = img
    await state.set_state(UserState.waiting_command)
    await message.answer("✅ Изображение получено! Напиши команду обработки.")


@router.message(F.text)
async def handle_text(message: Message, state: FSMContext):
    text = message.text
    lower = text.lower().strip()
    uid = message.from_user.id

    # Приветствие
    if "привет" in lower or "здравствуй" in lower:
        await message.answer("Привет! Отправь изображение 📷, затем напиши команду.")
        return

    # Telegram
    if "telegram" in lower or "телеграм" in lower:
        await cmd_telegram(message)
        return

    img = user_images.get(uid)
    if img is None:
        await message.answer(
            "Сначала отправь изображение 📷, затем напиши команду.\n"
            "Примеры: «пиксельный 50%», «квадрат», «размер 300x300»."
        )
        return

    # Парсим команду
    cmd = parse_filter_command(text)

    if not (cmd["filter"] or cmd["shape"] or cmd["dimensions"]):
        await message.answer(
            "Не понял команду. Примеры:\n"
            "• <code>пиксельный 50%</code>\n"
            "• <code>квадрат</code>\n"
            "• <code>размер 300x300</code>\n"
            "• <code>пиксельный 50% квадрат</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    # Сообщение «в обработке»
    status = await message.answer(
        f"🖼️ <b>Начинаю обработку...</b>\n\n{describe_command(cmd)}\n\n⏳ Подожди несколько секунд...",
        parse_mode=ParseMode.HTML,
    )

    try:
        # Небольшая пауза для UX (как в оригинале)
        await asyncio.sleep(1.5)
        result = process_image_with_commands(
            img.copy(),
            cmd["filter"],
            cmd["shape"],
            cmd["dimensions"],
        )

        # Если есть прозрачность — PNG, иначе JPEG
        if result.mode == "RGBA":
            fmt, mime, ext = "PNG", "image/png", "png"
        else:
            fmt, mime, ext = "JPEG", "image/jpeg", "jpg"

        data = image_to_bytes(result, fmt)

        caption = f"✅ <b>Обработка завершена</b>\n\n{describe_command(cmd)}"
        await message.answer_photo(
            BufferedInputFile(data, filename=f"gotik_ai_result.{ext}"),
            caption=caption,
            parse_mode=ParseMode.HTML,
        )
        await status.delete()
        await state.set_state(UserState.waiting_image)

    except Exception as e:
        logger.exception("Ошибка обработки: %s", e)
        await status.edit_text("❌ Ошибка при обработке изображения. Попробуй ещё раз.")


# ==================== HEALTHCHECK ДЛЯ RENDER ====================
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
    logger.info("Healthcheck на порту %s", PORT)


# ==================== ЗАПУСК ====================
async def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN не задан в переменных окружения!")

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    # Запускаем healthcheck
    asyncio.create_task(start_web_server())

    logger.info("Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
