"""
src/database.py
Konfigurasi Database Cloud PostgreSQL (Supabase) Toko Rina
"""
import os
from pathlib import Path
from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

# Muat variabel environment dari .env
load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

# Ambil URI koneksi Supabase dari .env
DATABASE_URL = os.getenv("DATABASE_URL")

# Fallback ke SQLite jika .env kosong
if not DATABASE_URL:
    db_path = BASE_DIR / "toko_rina_audit.db"
    DATABASE_URL = f"sqlite:///{db_path}"

# Konfigurasi Engine SQLAlchemy
if DATABASE_URL.startswith("sqlite"):
    engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
else:
    # Mode PostgreSQL Supabase (AWS Singapore Pooler)
    engine = create_engine(
        DATABASE_URL,
        pool_size=10,
        max_overflow=20,
        pool_pre_ping=True
    )

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# Mapping engine multi-toko (berbagi database cloud PostgreSQL yang sama)
STORE_ENGINES = {"toko1": engine, "toko2": engine, "toko3": engine}
STORE_SESSIONS = {"toko1": SessionLocal, "toko2": SessionLocal, "toko3": SessionLocal}

def get_store_session(store_id: str = "toko3"):
    return SessionLocal()

def init_all_databases():
    """Membuat seluruh tabel skema di database PostgreSQL Supabase."""
    import src.models.audit_models  # Daftarkan seluruh model ke Base
    Base.metadata.create_all(bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()