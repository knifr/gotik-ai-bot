FROM python:3.11-slim

WORKDIR /app

# Устанавливаем системные зависимости: шрифты, OCR, ffmpeg для аудио
RUN apt-get update && apt-get install -y --no-install-recommends \
    libjpeg62-turbo zlib1g libpng16-16 fonts-dejavu-core \
    tesseract-ocr tesseract-ocr-rus tesseract-ocr-eng \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .
COPY loading.gif* . 

ENV PORT=8080
EXPOSE 8080

CMD ["python", "bot.py"]
