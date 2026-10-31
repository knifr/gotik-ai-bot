FROM python:3.11-slim

WORKDIR /app

# Pillow нужен libjpeg/zlib для работы с изображениями
RUN apt-get update && apt-get install -y --no-install-recommends \
    libjpeg62-turbo zlib1g libpng16-16 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

ENV PORT=8080
EXPOSE 8080

CMD ["python", "bot.py"]
