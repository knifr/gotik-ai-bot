# Gotik AI Telegram Bot

Порт веб-версии Gotik AI в Telegram-бота.

## Функции
- Обработка изображений по командам
- Фильтры: пиксельный, закатный, чёрно-белый, розовый, теневой (с % интенсивности)
- Обрезка: квадрат, круг, треугольник, сердце
- Ресайз: `размер 300x300`

## Деплой на Render

### Через GitHub (Blueprint)
1. Залей репозиторий на GitHub.
2. На Render → **New → Blueprint** → выбери репозиторий.
3. Render сам прочитает `render.yaml`.
4. В настройках сервиса добавь переменную `BOT_TOKEN` (токен от @BotFather).
5. Deploy.

### Вручную (Docker Web Service)
1. New → Web Service → подключи репозиторий.
2. Runtime: **Docker**.
3. Добавь env `BOT_TOKEN`.
4. Deploy.

## Локальный запуск
```bash
export BOT_TOKEN="123456:ABC..."
pip install -r requirements.txt
python bot.py
