FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

# נקודת mount לדיסק (רק במצב SQLite — אם DATABASE_URL לא מוגדר)
RUN mkdir -p /data

CMD ["python3", "main.py"]
