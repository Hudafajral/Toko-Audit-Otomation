FROM python:3.11-slim

# Pasang dependensi sistem untuk PostgreSQL dan parser berkas
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Salin dan pasang paket Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Salin seluruh kode proyek
COPY . .

# Jalankan Uvicorn dengan membaca dinamis port dari environment Koyeb
CMD ["sh", "-c", "uvicorn src.main:app --host 0.0.0.0 --port ${PORT:-8000}"]