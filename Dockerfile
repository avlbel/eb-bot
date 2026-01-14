FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app/

# Timeweb App Platform обычно прокидывает PORT, но оставим дефолт.
ENV PORT=8080
EXPOSE 8080

HEALTHCHECK --interval=5s --timeout=3s --start-period=5s --retries=12 \
  CMD python -c "import os,urllib.request; p=os.getenv('PORT','8080'); urllib.request.urlopen(f'http://127.0.0.1:{p}/', timeout=2).read()"

CMD ["sh", "-c", "uvicorn main:api --host 0.0.0.0 --port ${PORT} --access-log"]

