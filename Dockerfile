FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY chat_automation_bot.py image_gen.py storage.py main_menu.png ./
COPY assets ./assets

# Здесь будет лежать sqlite-файл, если смонтируешь volume на /app/data
RUN mkdir -p /app/data
ENV MEMTRACE_DB_PATH=/app/data/memtrace.sqlite3

CMD ["python", "chat_automation_bot.py"]
