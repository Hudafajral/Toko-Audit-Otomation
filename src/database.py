"""
src/database.py
Multi-Tenant Database Manager: Dynamic Engine, Session Factory, dan Isolasi Folder
"""
import os
from pathlib import Path
from typing import Dict
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker, Session
from fastapi import Request

Base = declarative_base()

# Direktori utama penyimpanan data tenant
DATA_DIR = Path("data/tenants")
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Cache pool untuk engine dan sessionmaker agar tidak re-create berulang
_engine_cache: Dict[str, any] = {}
_session_cache: Dict[str, sessionmaker] = {}

DEFAULT_DB_KEY = "toko_1"

def get_db_path(db_name: str) -> Path:
    safe_name = "".join(c for c in db_name if c.isalnum() or c in ("_", "-")).lower() or DEFAULT_DB_KEY
    return DATA_DIR / f"{safe_name}.db"

def get_tenant_folders(db_name: str) -> dict:
    safe_name = "".join(c for c in db_name if c.isalnum() or c in ("_", "-")).lower() or DEFAULT_DB_KEY
    tenant_root = DATA_DIR / safe_name
    raw_dir = tenant_root / "raw"
    docs_dir = tenant_root / "documents"
    raw_dir.mkdir(parents=True, exist_ok=True)
    docs_dir.mkdir(parents=True, exist_ok=True)
    return {
        "root": tenant_root,
        "raw": raw_dir,
        "docs": docs_dir
    }

def get_engine_for_db(db_name: str):
    safe_name = "".join(c for c in db_name if c.isalnum() or c in ("_", "-")).lower() or DEFAULT_DB_KEY
    if safe_name not in _engine_cache:
        db_file = get_db_path(safe_name)
        db_url = f"sqlite:///{db_file.as_posix()}"
        engine = create_engine(db_url, connect_args={"check_same_thread": False})
        _engine_cache[safe_name] = engine
        _session_cache[safe_name] = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        # Pastikan tabel otomatis dibuat jika berkas db baru
        Base.metadata.create_all(bind=engine)
    return _engine_cache[safe_name]

def get_sessionmaker_for_db(db_name: str) -> sessionmaker:
    get_engine_for_db(db_name)
    safe_name = "".join(c for c in db_name if c.isalnum() or c in ("_", "-")).lower() or DEFAULT_DB_KEY
    return _session_cache[safe_name]

def extract_db_name_from_request(request: Request) -> str:
    # Membaca identitas database dari query param (?db= atau ?db_name=) atau header X-Database-Name
    db_name = (
        request.query_params.get("db") 
        or request.query_params.get("db_name") 
        or request.headers.get("X-Database-Name")
    )
    return db_name.strip() if db_name and db_name.strip() else DEFAULT_DB_KEY

def get_db(request: Request):
    """
    Dependency injection FastAPI:
    Membuka session database sesuai tenant aktif dari header request.
    """
    db_name = extract_db_name_from_request(request)
    session_factory = get_sessionmaker_for_db(db_name)
    db: Session = session_factory()
    try:
        yield db
    finally:
        db.close()