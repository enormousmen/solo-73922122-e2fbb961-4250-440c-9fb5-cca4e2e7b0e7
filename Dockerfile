FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY db ./db
COPY scripts ./scripts

EXPOSE 8000

# 单 worker：os._exit 崩溃演示会直接结束容器主进程，由 restart 策略拉起。
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
