"""
src/database.py
Multi-Tenant Database Manager: PostgreSQL Supabase Multi-Schema Isolation
"""
import os
from typing import Dict
from sqlalchemy import create_engine, text
from sqlalchemy.orm import declarative_base, sessionmaker, Session
from fastapi import Request
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

Base = declarative_base()

# Ambil DATABASE_URL yang benar dari .env
DATABASE_URL = os.getenv("DATABASE_URL")
print(f"--> KONEKSI AKTIF: {DATABASE_URL}")
if not DATABASE_URL:
    DATABASE_URL = "sqlite:///./toko_rina_audit.db"

# Pastikan dialek URI kompatibel dengan SQLAlchemy
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

_engine = None
_session_cache: Dict[str, sessionmaker] = {}
DEFAULT_DB_KEY = "toko_1"

def get_base_engine():
    global _engine
    if _engine is None:
        if "sqlite" in DATABASE_URL:
            _engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
        else:
            _engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_size=10, max_overflow=20)
    return _engine

def init_tenant_schema(schema_name: str):
    """Membuat schema PostgreSQL jika belum ada dan membuat tabel di schema tersebut."""
    engine = get_base_engine()
    if "sqlite" not in DATABASE_URL:
        with engine.connect() as conn:
            conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema_name}"'))
            conn.commit()
        # Buat tabel langsung di dalam schema tenant terkait
        schema_engine = engine.execution_options(schema_translate_map={None: schema_name})
        Base.metadata.create_all(bind=schema_engine)
    else:
        Base.metadata.create_all(bind=engine)

def get_engine_for_db(db_name: str = DEFAULT_DB_KEY):
    """Mengembalikan engine yang sudah diset ke schema tenant spesifik."""
    safe_name = "".join(c for c in db_name if c.isalnum() or c in ("_", "-")).lower() or DEFAULT_DB_KEY
    init_tenant_schema(safe_name)
    engine = get_base_engine()
    if "sqlite" not in DATABASE_URL:
        return engine.execution_options(schema_translate_map={None: safe_name})
    return engine

def get_sessionmaker_for_db(db_name: str) -> sessionmaker:
    safe_name = "".join(c for c in db_name if c.isalnum() or c in ("_", "-")).lower() or DEFAULT_DB_KEY
    
    if safe_name not in _session_cache:
        init_tenant_schema(safe_name)
        engine = get_base_engine()
        
        if "sqlite" not in DATABASE_URL:
            schema_engine = engine.execution_options(
                schema_translate_map={None: safe_name}
            )
            _session_cache[safe_name] = sessionmaker(autocommit=False, autoflush=False, bind=schema_engine)
        else:
            _session_cache[safe_name] = sessionmaker(autocommit=False, autoflush=False, bind=engine)
            
    return _session_cache[safe_name]

def extract_db_name_from_request(request: Request) -> str:
    db_name = (
        request.query_params.get("db") 
        or request.query_params.get("db_name") 
        or request.headers.get("X-Database-Name")
    )
    return db_name.strip() if db_name and db_name.strip() else DEFAULT_DB_KEY

def get_db(request: Request):
    db_name = extract_db_name_from_request(request)
    session_factory = get_sessionmaker_for_db(db_name)
    db: Session = session_factory()
    try:
        if "sqlite" not in DATABASE_URL:
            safe_name = "".join(c for c in db_name if c.isalnum() or c in ("_", "-")).lower() or DEFAULT_DB_KEY
            db.execute(text(f'SET search_path TO "{safe_name}", public'))
        yield db
    finally:
        db.close()

def get_tenant_folders(db_name: str = DEFAULT_DB_KEY):
    """Mengembalikan dictionary path direktori lokal untuk tenant tertentu."""
    safe_name = "".join(c for c in db_name if c.isalnum() or c in ("_", "-")).lower() or DEFAULT_DB_KEY
    tenant_dir = DATA_DIR / "tenants" / safe_name
    docs_dir = tenant_dir / "documents"
    raw_dir = tenant_dir / "raw"
    
    tenant_dir.mkdir(parents=True, exist_ok=True)
    docs_dir.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)
    
    return {
        "tenant_dir": tenant_dir,
        "docs_dir": docs_dir,
        "raw_dir": raw_dir,
        "raw": raw_dir,
        "docs": docs_dir,
        "documents": docs_dir,
    }