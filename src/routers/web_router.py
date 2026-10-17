"""
src/routers/web_router.py
API Controller Keuangan POS Toko Rina:
- Auto-migrasi skema database SQLite saat startup
- Autentikasi dengan approval Admin & geolokasi GPS/IP
- Run Audit Presisi dengan sinkronisasi mutasi dan piutang
- Evaluasi Machine Learning Risk Engine menggunakan Sisa Selisih Kas Bersih
  (mencegah pembengkakan ganda uang fisik dari penyesuaian kas manual)
- Resolusi Mutasi Manual Batch dengan alokasi sisa saldo mutasi & format manual(pelanggan(nominal))
- Generator Laporan Audit Harian Dinamis:
  * Header: CASH LAPORAN, DEBIT LAPORAN, DEBIT MUTASI (total CR bank)
  * Rekap Cash Fisik murni: Masuk Brankas + Keluar Laci
  * Kotak ringkasan: Selisih, Keterangan, Sisa Selisih, dan Status Mutasi Debit
  * Auto-Sync Real-time ke Tab Perpustakaan Dokumen
- Manajemen Data: Edit & Hapus untuk Dokumen, Pelanggan, dan User
- Penanganan pratinjau tab baru (inline) untuk dokumen HTML
"""
from __future__ import annotations

import io
import json
import re
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session
import shutil
from src.database import (
    get_db, Base, DATA_DIR, DEFAULT_DB_KEY, 
    extract_db_name_from_request, get_tenant_folders, 
    get_engine_for_db, get_sessionmaker_for_db
)

from src.models.audit_models import (
    User, Customer, Receivable, AuditActionLog, MLSetting, UploadedDocument, DailyAuditArchive
)
from src.services.ingestion_service import parse_sales_report_pdf, parse_bank_mutation_csv
from src.services.receivable_service import (
    record_daily_credit_sales, get_all_debts_for_table, get_total_receivable_balance, settle_receivable
)
from src.services.transfer_service import reconcile_daily_bank_mutations
from src.services.anomaly_ml_service import evaluate_daily_audit_risks
from src.services.archive_service import save_daily_cash_files

router = APIRouter(tags=["Web App"])
BASE_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

DOCS_DIR = Path("data/documents")

try:
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    pass


# Model payload registrasi DB baru
class CreateDatabasePayload(BaseModel):
    db_name: str
    display_name: str
    initial_cash: float = 0.0
    initial_transfer: float = 0.0
    initial_debt: float = 0.0
    username: str = "admin"

@router.get("/api/databases/list")
def list_available_databases(request: Request, db: Session = Depends(get_db)):
    # Ambil semua file .db di folder tenants
    db_files = list(DATA_DIR.glob("*.db"))
    
    # Jika awal belum ada, daftarkan 3 toko bawaan
    default_keys = ["toko_1", "toko_2", "toko_3"]
    for k in default_keys:
        get_engine_for_db(k)
        get_tenant_folders(k)

    db_files = sorted(list(DATA_DIR.glob("*.db")))
    databases = []
    for f in db_files:
        name_key = f.stem
        # Format label display yang rapi
        label = name_key.replace("_", " ").title()
        databases.append({"key": name_key, "label": label})

    active_db = extract_db_name_from_request(request)
    return {"databases": databases, "active_db": active_db}

@router.post("/api/admin/databases/create")
def create_new_database(payload: CreateDatabasePayload, request: Request):
    clean_key = "".join(c for c in payload.db_name if c.isalnum() or c in ("_", "-")).lower()
    if not clean_key:
        raise HTTPException(status_code=400, detail="Nama database tidak valid!")
    
    # Inisialisasi engine, schema tabel, dan folder fisik
    engine = get_engine_for_db(clean_key)
    get_tenant_folders(clean_key)
    
    # Inisialisasi akun admin dan saldo awal di DB baru tersebut
    session_factory = get_sessionmaker_for_db(clean_key)
    with session_factory() as new_session:
        # Tambah admin utama
        admin_u = new_session.query(User).filter(User.username == "admin").first()
        if not admin_u:
            new_session.add(User(
                username="admin", 
                password_hash="admin123", 
                role="admin", 
                status_approval="APPROVED",
                allowed_tabs='["*"]'
            ))
        # Set saldo awal toko
        cfg = new_session.query(MLSetting).first()
        if not cfg:
            cfg = MLSetting()
            new_session.add(cfg)
        cfg.initial_cash_balance = payload.initial_cash
        cfg.initial_transfer_balance = payload.initial_transfer
        cfg.initial_debt_balance = payload.initial_debt
        cfg.updated_by = payload.username
        
        client_ip, client_loc, lat, lon = detect_client_info(request)
        new_session.add(AuditActionLog(
            username=payload.username,
            action_type="DB_CREATE",
            description=f"Database '{payload.display_name}' ({clean_key}) berhasil dibuat.",
            client_ip=client_ip,
            client_location=client_loc,
            latitude=lat,
            longitude=lon
        ))
        new_session.commit()

    return {"status": "SUCCESS", "message": f"Database {payload.display_name} berhasil dibuat!", "db_key": clean_key}

@router.delete("/api/admin/reconciliation/{audit_date}")
def delete_reconciliation_by_date(audit_date: str, request: Request, admin_operator: str = "admin", db: Session = Depends(get_db)):
    """
    Rollback/Hapus hasil rekonsiliasi pada tanggal tertentu di database aktif (Admin Only).
    """
    db_name = extract_db_name_from_request(request)
    folders = get_tenant_folders(db_name)
    target_raw = folders["raw"] / audit_date

    # 1. Hapus arsip harian di database Supabase (PENTING AGAR SALDO KAS & MUTASI MENJADI 0)
    db.query(DailyAuditArchive).filter(DailyAuditArchive.audit_date == audit_date).delete()

    # 2. Hapus 5 file JSON arsip folder tanggal terkait (abaikan jika di filesystem read-only serverless)
    try:
        if target_raw.exists() and target_raw.is_dir():
            shutil.rmtree(target_raw, ignore_errors=True)
    except OSError:
        pass

    # 3. Hapus dokumen laporan resmi HTML di database dan perpustakaan dokumen
    doc_report = db.query(UploadedDocument).filter(UploadedDocument.doc_date == audit_date).all()
    for d in doc_report:
        try:
            p = Path(d.file_path)
            if p.exists():
                p.unlink(missing_ok=True)
        except OSError:
            pass
        except Exception:
            pass
        db.delete(d)

    # 4. Bersihkan pencatatan piutang yang tercatat dari tanggal penjualan tersebut
    db.query(Receivable).filter(Receivable.sale_date == audit_date).delete()

    # 5. Catat jejak rollback ke audit trail
    client_ip, client_loc, lat, lon = detect_client_info(request)
    db.add(AuditActionLog(
        username=admin_operator,
        action_type="RECONCILIATION_DELETE",
        description=f"Admin menghapus seluruh hasil rekonsiliasi tanggal {audit_date} pada database {db_name}",
        client_ip=client_ip,
        client_location=client_loc,
        latitude=lat,
        longitude=lon
    ))
    db.commit()

    return {
        "status": "SUCCESS", 
        "message": f"Seluruh data audit tanggal {audit_date} pada database {db_name} berhasil dihapus dan di-rollback!"
    }

def detect_client_info(
    request: Request,
    lat: Optional[float] = None,
    lon: Optional[float] = None,
    client_loc_name: Optional[str] = None
) -> tuple[str, str, Optional[float], Optional[float]]:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        client_ip = forwarded.split(",")[0].strip()
    else:
        client_ip = request.client.host if request.client else "127.0.0.1"

    if client_loc_name and client_loc_name.strip():
        return client_ip, client_loc_name.strip(), lat, lon

    if lat is not None and lon is not None:
        try:
            url = f"https://nominatim.openstreetmap.org/reverse?format=json&lat={lat}&lon={lon}&zoom=14&addressdetails=1"
            req = urllib.request.Request(url, headers={'User-Agent': 'TokoRinaAuditPOS/2.0'})
            with urllib.request.urlopen(req, timeout=1.5) as resp:
                data = json.loads(resp.read().decode())
                addr = data.get("address", {})
                kec = addr.get("suburb") or addr.get("city_district") or addr.get("town") or addr.get("village")
                kota = addr.get("city") or addr.get("regency") or addr.get("state")
                if kec and kota:
                    return client_ip, f"{kec}, {kota}", lat, lon
                elif data.get("display_name"):
                    return client_ip, data.get("display_name")[:60], lat, lon
        except Exception:
            return client_ip, f"GPS ({lat:.4f}, {lon:.4f})", lat, lon

    if client_ip in ["127.0.0.1", "localhost", "::1"] or client_ip.startswith("192.168.") or client_ip.startswith("10."):
        return client_ip, "Lokal (Jaringan Toko Rina)", lat, lon

    location_str = "Indonesia"
    try:
        req = urllib.request.Request(f"https://ipapi.co/{client_ip}/json/", headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=1.0) as resp:
            data = json.loads(resp.read().decode())
            city = data.get("city")
            region = data.get("region")
            if city:
                location_str = f"{city}, {region}" if region else city
    except Exception:
        pass

    return client_ip, location_str, lat, lon


@router.on_event("startup")
def on_startup():
    default_dbs = ["toko_1", "toko_2", "toko_3"]
    
    for db_key in default_dbs:
        cur_engine = get_engine_for_db(db_key)
        get_tenant_folders(db_key)
        Base.metadata.create_all(bind=cur_engine)

        with cur_engine.connect() as conn:
            cols_to_add = [
                ("ml_settings", "initial_cash_balance", "FLOAT DEFAULT 0.0"),
                ("ml_settings", "initial_transfer_balance", "FLOAT DEFAULT 0.0"),
                ("ml_settings", "initial_debt_balance", "FLOAT DEFAULT 0.0"),
                ("users", "allowed_tabs", "TEXT DEFAULT '[\"homeDashboardTab\",\"auditFormTab\",\"docLibraryTab\",\"piutangTab\"]'"),
                ("users", "status_approval", "VARCHAR(20) DEFAULT 'APPROVED'"),
                ("users", "registered_ip", "VARCHAR(50) DEFAULT '127.0.0.1'"),
                ("users", "registered_location", "VARCHAR(255) DEFAULT 'Lokal / Internal'"),
                ("users", "latitude", "FLOAT"),
                ("users", "longitude", "FLOAT"),
                ("users", "assigned_tenant", "VARCHAR(50) DEFAULT 'toko_1'"),
                ("audit_action_logs", "client_location", "VARCHAR(255) DEFAULT 'Lokal / Internal'"),
                ("audit_action_logs", "latitude", "FLOAT"),
                ("audit_action_logs", "longitude", "FLOAT")
            ]
            for tbl, col_name, col_type in cols_to_add:
                try:
                    conn.execute(text(f"ALTER TABLE {tbl} ADD COLUMN {col_name} {col_type};"))
                    conn.commit()
                except Exception:
                    pass

        session_factory = get_sessionmaker_for_db(db_key)
        with session_factory() as db:
            admin_u = db.query(User).filter(User.username == "admin").first()
            if not admin_u:
                db.add(User(
                    username="admin", 
                    password_hash="admin123", 
                    role="admin", 
                    status_approval="APPROVED",
                    allowed_tabs='["*"]',
                    registered_ip="127.0.0.1",
                    registered_location="Sistem Utama (Server)",
                    latitude=-6.2917,
                    longitude=106.7170
                ))
            else:
                admin_u.role = "admin"
                admin_u.status_approval = "APPROVED"
                admin_u.allowed_tabs = '["*"]'
            
            if not db.query(MLSetting).first():
                db.add(MLSetting())
            db.commit()

@router.get("/", response_class=HTMLResponse)
def index_page(request: Request):
    return templates.TemplateResponse(request=request, name="index.html", context={})


# ================= AUTENTIKASI =================
class AuthPayload(BaseModel):
    username: str
    password: str
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    location_name: Optional[str] = None


@router.get("/api/check-user-status/{username}")
def check_user_status(username: str, db: Session = Depends(get_db)):
    uname = username.strip().lower()
    u = db.query(User).filter(User.username == uname).first()
    if not u:
        return {"exists": False, "status": "NOT_FOUND"}
    return {
        "exists": True, 
        "status": u.status_approval or "APPROVED",
        "role": u.role
    }


@router.post("/api/login")
def login(payload: AuthPayload, request: Request, db: Session = Depends(get_db)):
    uname = payload.username.strip().lower()
    u = db.query(User).filter(User.username == uname, User.password_hash == payload.password).first()
    if not u:
        raise HTTPException(status_code=401, detail="Username atau password salah!")

    if u.role != "admin" and (u.status_approval or "").upper() == "PENDING":
        raise HTTPException(
            status_code=403, 
            detail="Akun Anda sedang menunggu persetujuan Admin Toko Rina."
        )
    if u.role != "admin" and (u.status_approval or "").upper() == "REJECTED":
        raise HTTPException(
            status_code=403, 
            detail="Permintaan akses akun Anda ditolak oleh Admin."
        )

    client_ip, client_loc, lat, lon = detect_client_info(
        request, lat=payload.latitude, lon=payload.longitude, client_loc_name=payload.location_name
    )

    allowed = ["*"]
    if u.role != "admin":
        try:
            allowed = json.loads(u.allowed_tabs or '["homeDashboardTab","auditFormTab","docLibraryTab","piutangTab"]')
        except Exception:
            allowed = ["homeDashboardTab", "auditFormTab", "docLibraryTab", "piutangTab"]

    db.add(AuditActionLog(
        username=u.username,
        action_type="LOGIN",
        description=f"Pengguna '{u.username}' berhasil login ({client_loc})",
        client_ip=client_ip,
        client_location=client_loc,
        latitude=lat,
        longitude=lon
    ))
    db.commit()
    tenant_assigned = getattr(u, "assigned_tenant", "toko_1") or "toko_1"
    return {
        "status": "SUCCESS", 
        "username": u.username, 
        "role": u.role,
        "allowed_tabs": allowed,
        "assigned_tenant": tenant_assigned,
        "client_ip": client_ip,
        "client_location": client_loc,
        "latitude": lat,
        "longitude": lon
    }


@router.post("/api/register-kasir")
def register_kasir(payload: AuthPayload, request: Request):
    uname = payload.username.strip().lower()
    pwd = payload.password.strip()

    if uname == "admin":
        raise HTTPException(status_code=400, detail="Username 'admin' khusus dan dilindungi sistem!")

    client_ip, client_loc, lat, lon = detect_client_info(
        request, lat=payload.latitude, lon=payload.longitude, client_loc_name=payload.location_name
    )
    default_permissions = json.dumps(["homeDashboardTab", "auditFormTab", "docLibraryTab", "piutangTab"])

    # Ambil seluruh daftar database toko yang ada
    db_files = list(DATA_DIR.glob("*.db"))
    tenant_keys = [f.stem for f in db_files] if db_files else ["toko_1", "toko_2", "toko_3"]

    for db_key in tenant_keys:
        session_factory = get_sessionmaker_for_db(db_key)
        with session_factory() as s:
            existing = s.query(User).filter(User.username == uname).first()
            if not existing:
                new_u = User(
                    username=uname,
                    password_hash=pwd,
                    role="kasir",
                    status_approval="PENDING",
                    registered_ip=client_ip,
                    registered_location=client_loc,
                    latitude=lat,
                    longitude=lon,
                    allowed_tabs=default_permissions,
                    assigned_tenant="toko_1"
                )
                s.add(new_u)
                s.add(AuditActionLog(
                    username="SYSTEM",
                    action_type="USER_REGISTER",
                    description=f"Kasir baru mendaftar: '{uname}'. Menunggu persetujuan.",
                    client_ip=client_ip,
                    client_location=client_loc,
                    latitude=lat,
                    longitude=lon
                ))
                s.commit()

    return {
        "status": "SUCCESS",
        "message": f"Pendaftaran berhasil! Akun '{uname}' sedang menunggu persetujuan (approval) Admin."
    }

# ================= MANAJEMEN USER (ADMIN ONLY) =================
@router.get("/api/admin/users")
def get_user_list(db: Session = Depends(get_db)):
    users = db.query(User).order_by(User.id.desc()).all()
    pending_count = db.query(User).filter(User.status_approval == "PENDING").count()
    out = []
    for u in users:
        tabs = []
        try:
            tabs = json.loads(u.allowed_tabs or "[]")
        except Exception:
            tabs = []
        out.append({
            "id": u.id,
            "username": u.username,
            "role": u.role,
            "status_approval": u.status_approval or "APPROVED",
            "assigned_tenant": getattr(u, "assigned_tenant", "toko_1") or "toko_1",
            "registered_ip": u.registered_ip or "127.0.0.1",
            "registered_location": u.registered_location or "Lokal",
            "latitude": getattr(u, "latitude", None),
            "longitude": getattr(u, "longitude", None),
            "allowed_tabs": tabs,
            "created_at": u.created_at.strftime("%Y-%m-%d %H:%M") if u.created_at else "-"
        })
    return {
        "users": out,
        "pending_count": pending_count
    }


class UserApprovalPayload(BaseModel):
    user_id: int
    status: str
    username_admin: str = "admin"


@router.put("/api/admin/users/approval")
def set_user_approval(payload: UserApprovalPayload, request: Request, db: Session = Depends(get_db)):
    target_user = db.query(User).filter(User.id == payload.user_id).first()
    if not target_user:
        raise HTTPException(status_code=404, detail="Pengguna tidak ditemukan")
    if target_user.role == "admin" and target_user.username == "admin":
        raise HTTPException(status_code=400, detail="Akun Admin utama tidak dapat diubah statusnya")

    uname = target_user.username
    new_status = payload.status.upper()

    # Replikasi status approval ke semua database tenant
    db_files = list(DATA_DIR.glob("*.db"))
    tenant_keys = [f.stem for f in db_files] if db_files else ["toko_1", "toko_2", "toko_3"]

    for db_key in tenant_keys:
        session_factory = get_sessionmaker_for_db(db_key)
        with session_factory() as s:
            u_entry = s.query(User).filter(User.username == uname).first()
            if u_entry:
                u_entry.status_approval = new_status
                s.commit()

    client_ip, client_loc, lat, lon = detect_client_info(request)
    act_text = "MENYETUJUI" if new_status == "APPROVED" else "MENOLAK"
    db.add(AuditActionLog(
        username=payload.username_admin,
        action_type="USER_APPROVAL",
        description=f"Admin {act_text} aktivasi akun kasir '{uname}' (Sinkron ke seluruh cabang)",
        client_ip=client_ip,
        client_location=client_loc,
        latitude=lat,
        longitude=lon
    ))
    db.commit()

    return {"status": "SUCCESS", "message": f"Status akun {uname} berhasil diubah menjadi {new_status}"}

class UserPermissionPayload(BaseModel):
    user_id: int
    allowed_tabs: List[str]
    username_admin: str = "admin"


@router.put("/api/admin/users/permissions")
def update_user_permissions(payload: UserPermissionPayload, request: Request, db: Session = Depends(get_db)):
    target_user = db.query(User).filter(User.id == payload.user_id).first()
    if not target_user:
        raise HTTPException(status_code=404, detail="Pengguna tidak ditemukan")
    if target_user.role == "admin" and target_user.username == "admin":
        raise HTTPException(status_code=400, detail="Hak akses akun Admin utama tidak dapat dibatasi")

    target_user.allowed_tabs = json.dumps(payload.allowed_tabs)
    client_ip, client_loc, lat, lon = detect_client_info(request)
    db.add(AuditActionLog(
        username=payload.username_admin,
        action_type="PERMISSION_UPDATE",
        description=f"Admin memperbarui izin akses akun '{target_user.username}': {len(payload.allowed_tabs)} fitur aktif",
        client_ip=client_ip,
        client_location=client_loc,
        latitude=lat,
        longitude=lon
    ))
    db.commit()
    return {"status": "SUCCESS", "message": f"Izin akses untuk {target_user.username} berhasil disimpan"}


class EditUserPayload(BaseModel):
    user_id: int
    username: str
    role: str
    status_approval: str
    assigned_tenant: Optional[str] = "toko_1"
    new_password: Optional[str] = None
    admin_operator: str = "admin"


@router.put("/api/admin/users/edit")
def edit_user_profile(payload: EditUserPayload, request: Request, db: Session = Depends(get_db)):
    u = db.query(User).filter(User.id == payload.user_id).first()
    if not u:
        raise HTTPException(status_code=404, detail="User tidak ditemukan")
    if u.username == "admin" and payload.username.lower() != "admin":
        raise HTTPException(status_code=400, detail="Username akun admin utama tidak boleh diubah!")

    target_uname = payload.username.lower().strip()
    target_tenant = payload.assigned_tenant or "toko_1"
    old_uname = u.username

    # Update dan sinkronkan ke SELURUH file database toko yang ada
    db_files = list(DATA_DIR.glob("*.db"))
    tenant_keys = [f.stem for f in db_files] if db_files else ["toko_1", "toko_2", "toko_3"]

    for db_key in tenant_keys:
        try:
            s_factory = get_sessionmaker_for_db(db_key)
            with s_factory() as s:
                target_user = s.query(User).filter(User.username == old_uname).first()
                if not target_user:
                    target_user = s.query(User).filter(User.id == payload.user_id).first()
                if target_user:
                    target_user.username = target_uname
                    target_user.role = payload.role.lower().strip()
                    target_user.status_approval = payload.status_approval.upper().strip()
                    if hasattr(target_user, "assigned_tenant"):
                        target_user.assigned_tenant = target_tenant
                    if payload.new_password and len(payload.new_password.strip()) >= 4:
                        target_user.password_hash = payload.new_password.strip()
                    s.commit()
        except Exception:
            pass

    client_ip, client_loc, lat, lon = detect_client_info(request)
    db.add(AuditActionLog(
        username=payload.admin_operator,
        action_type="USER_EDIT",
        description=f"Admin mengubah profil '{old_uname}' -> '{target_uname}', Role: {payload.role}, Status: {payload.status_approval}, Penempatan Toko: {target_tenant} (Sinkron ke seluruh DB)",
        client_ip=client_ip,
        client_location=client_loc,
        latitude=lat,
        longitude=lon
    ))
    db.commit()

    return {"status": "SUCCESS", "message": f"Data user {target_uname} berhasil diperbarui di seluruh toko!"}
@router.delete("/api/admin/users/{user_id}")
def delete_user(user_id: int, request: Request, admin_operator: str = "admin", db: Session = Depends(get_db)):
    u = db.query(User).filter(User.id == user_id).first()
    if not u:
        raise HTTPException(status_code=404, detail="User tidak ditemukan")
    if u.username == "admin":
        raise HTTPException(status_code=400, detail="Akun admin utama tidak boleh dihapus!")

    uname = u.username
    db.delete(u)
    client_ip, client_loc, lat, lon = detect_client_info(request)
    db.add(AuditActionLog(
        username=admin_operator,
        action_type="USER_DELETE",
        description=f"Admin menghapus akun user '{uname}'",
        client_ip=client_ip,
        client_location=client_loc,
        latitude=lat,
        longitude=lon
    ))
    db.commit()
    return {"status": "SUCCESS", "message": f"Akun {uname} berhasil dihapus permanen!"}


# ================= DASHBOARD SUMMARY =================
@router.get("/api/dashboard-summary")
def get_dashboard_summary(request: Request, db_param: Optional[str] = None):
    # 1. Ambil nama database toko aktif dari parameter query atau header
    db_name = extract_db_name_from_request(request)
    
    # 2. Buka koneksi database tenant
    session_factory = get_sessionmaker_for_db(db_name)
    with session_factory() as tenant_db:
        if "sqlite" not in str(tenant_db.bind.url):
            safe_name = "".join(c for c in db_name if c.isalnum() or c in ("_", "-")).lower() or DEFAULT_DB_KEY
            tenant_db.execute(text(f'SET search_path TO "{safe_name}", public'))

        # Data Piutang & ML Settings dari Supabase
        debts = get_all_debts_for_table(tenant_db, include_settled=True)
        total_debt = get_total_receivable_balance(tenant_db)

        cfg = tenant_db.query(MLSetting).first()
        init_cash = float(getattr(cfg, "initial_cash_balance", 0.0) or 0.0) if cfg else 0.0
        init_tf = float(getattr(cfg, "initial_transfer_balance", 0.0) or 0.0) if cfg else 0.0
        init_debt = float(getattr(cfg, "initial_debt_balance", 0.0) or 0.0) if cfg else 0.0

        # 3. Ambil arsip resmi murni dari tabel Supabase
        db_archives = tenant_db.query(DailyAuditArchive).order_by(DailyAuditArchive.audit_date.desc()).all()
        archive_map = {a.audit_date: a for a in db_archives}

        # Hanya gunakan tanggal yang tercatat valid di Supabase
        all_dates = sorted(list(archive_map.keys()), reverse=True)

        total_kumulatif_cash_brankas = init_cash
        total_kumulatif_net_tf = init_tf
        unmatched_mutations = []

        # 4. Akumulasi saldo kas dan transfer bank murni dari Supabase
        for dt_str in all_dates:
            arch = archive_map[dt_str]
            total_kumulatif_cash_brankas += float(arch.vault_cash_in or 0.0)
            total_kumulatif_net_tf += float(arch.net_bank_mutation or 0.0)

            # Ambil daftar mutasi bank tanggal terbaru untuk dropdown manual match
            if not unmatched_mutations and arch.mutasi_db_bank:
                mut_records = arch.mutasi_db_bank.get("records", []) if isinstance(arch.mutasi_db_bank, dict) else []
                for idx, r in enumerate(mut_records):
                    status_m = str(r.get("status_matching", "")).upper()
                    tipe_m = str(r.get("tipe", r.get("mutation_type", "CR"))).upper()
                    
                    sisa_nom = float(r.get("sisa_nominal", r.get("nominal", r.get("amount", 0.0))))
                    has_leftover = bool(r.get("has_leftover", False)) or (status_m == "MATCHED_MANUAL" and sisa_nom > 1.0)
                    
                    # Munculkan jika belum dimatch, ATAU sudah dimatch manual tapi masih ada sisa dana
                    if tipe_m == "CR" and (status_m not in ["MATCHED_SALES_DEBIT", "K_DEBIT_MATCH", "MATCHED_MANUAL"] or has_leftover):
                        unmatched_mutations.append({
                            "id": idx,
                            "nominal": sisa_nom,
                            "original_nominal": float(r.get("original_nominal", r.get("nominal", r.get("amount", 0.0)))),
                            "keterangan": f"{r.get('keterangan', '-')} (Sisa Rp {sisa_nom:,.0f})" if has_leftover else r.get("keterangan", "-"),
                            "has_leftover": has_leftover
                        })

        pending_users = tenant_db.query(User).filter(User.status_approval == "PENDING").count()

        return {
            "total_cash_fisik": total_kumulatif_cash_brankas,
            "cash_brankas": total_kumulatif_cash_brankas,
            "total_active_debt": total_debt + init_debt,
            "net_transfer": total_kumulatif_net_tf,
            "debts_table": debts,
            "available_dates": all_dates,
            "unmatched_mutations": unmatched_mutations,
            "pending_registrations_count": pending_users,
            "initial_balances": {
                "cash": init_cash,
                "transfer": init_tf,
                "debt": init_debt
            }
        }
# ================= HELPER SINKRONISASI LAPORAN KE PERPUSTAKAAN =================
def sync_audit_report_to_library(request: Request, audit_date: str, db: Session, html_content: Optional[str] = None) -> None:
    db_name = extract_db_name_from_request(request)
    folders = get_tenant_folders(db_name)
    docs_dir = folders["docs"]
    if not html_content:
        resp = generate_daily_audit_report(request, audit_date, db, auto_sync=False)
        html_content = resp.body.decode("utf-8")

    filename = f"Laporan_Audit_{audit_date}.html"
    file_path = docs_dir / filename
    
    # Abaikan jika penulisan ke disk diblokir serverless
    try:
        file_path.write_text(html_content, encoding="utf-8")
    except OSError:
        pass

    size_kb = round(len(html_content.encode("utf-8")) / 1024, 1)

    doc = db.query(UploadedDocument).filter(
        UploadedDocument.doc_date == audit_date,
        UploadedDocument.filename == filename
    ).first()

    if not doc:
        new_doc = UploadedDocument(
            filename=filename,
            file_path=str(file_path),
            file_type="HTML",
            file_size_kb=size_kb,
            extra_info="Rekap Audit Resmi",
            doc_date=audit_date,
            upload_time=datetime.now().strftime("%H:%M"),
            status="Terproses"
        )
        db.add(new_doc)
    else:
        doc.file_size_kb = size_kb
        doc.upload_time = datetime.now().strftime("%H:%M")
        doc.status = "Terproses"
    
    db.commit()


# ================= RUN AUDIT =================
@router.post("/api/run-audit")
async def run_audit(
    request: Request,
    pdf_file: UploadFile = File(...),
    csv_file: UploadFile = File(...),
    vault_in: float = Form(...),
    laci_items_json: str = Form(...),
    brankas_items_json: str = Form(...),
    cash_adjustment: float = Form(0.0),
    cash_debt_receivable_ids_json: Optional[str] = Form("[]"),
    cash_adjustment_note: Optional[str] = Form(""),
    override_existing: bool = Form(False),
    username: str = Form(...),
    db: Session = Depends(get_db)
):
    try:
        pdf_bytes = await pdf_file.read()
        csv_bytes = await csv_file.read()

        pdf_data = parse_sales_report_pdf(io.BytesIO(pdf_bytes))
        audit_date = pdf_data.get("audit_date")
        if not audit_date:
            raise HTTPException(status_code=400, detail="Tanggal transaksi pada berkas PDF tidak dapat diidentifikasi.")

        # Ambil folder penyimpanan raw dan docs khusus untuk tenant/toko aktif
        db_name = extract_db_name_from_request(request)
        tenant_folders = get_tenant_folders(db_name)
        target_archive_dir = tenant_folders["raw"] / audit_date

        # Cek apakah transaksi sudah ada di DB Supabase
        existing_in_db = db.query(DailyAuditArchive).filter(DailyAuditArchive.audit_date == audit_date).first()

        if existing_in_db and not override_existing:
            raise HTTPException(
                status_code=409,
                detail=f"Laporan transaksi tanggal {audit_date} sudah pernah diaudit pada database {db_name}. Centang opsi timpa data jika ingin memproses ulang!"
            )

        bank_mutations = parse_bank_mutation_csv(io.BytesIO(csv_bytes))
        today_sales = pdf_data.get("records", [])

        laci_items = json.loads(laci_items_json) if laci_items_json else []
        brankas_items = json.loads(brankas_items_json) if brankas_items_json else []

        # 1. Simpan kas fisik (abaikan jika filesystem lokal serverless read-only)
        try:
            save_daily_cash_files(
                audit_date=audit_date,
                vault_in=vault_in,
                drawer_items=laci_items,
                vault_items=brankas_items,
                input_by=username,
                base_dir=str(tenant_folders["raw"]),
                tenant_id=db_name
            )
        except OSError:
            pass
        
        pdf_summary_data = {
            "total_cash": float(pdf_data.get("total_cash", 0.0)),
            "total_debit": float(pdf_data.get("total_debit", 0.0)),
            "total_credit": float(pdf_data.get("total_credit", 0.0)),
            "records": today_sales
        }

        # Simpan file JSON penjualan jika disk mengizinkan
        try:
            target_archive_dir.mkdir(parents=True, exist_ok=True)
            pdf_summary_file = target_archive_dir / "penjualan_pos_pdf.json"
            pdf_summary_file.write_text(json.dumps(pdf_summary_data, indent=2, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

        selected_cash_debt_ids = []
        try:
            selected_cash_debt_ids = json.loads(cash_debt_receivable_ids_json or "[]")
        except Exception:
            pass

        adj_data = {
            "nominal": cash_adjustment,
            "keterangan": cash_adjustment_note or "-",
            "receivable_ids_settled": selected_cash_debt_ids,
            "tanggal": audit_date
        }

        # Simpan file JSON penyesuaian jika disk mengizinkan
        try:
            adj_file = target_archive_dir / "penyesuaian_kas.json"
            adj_file.write_text(json.dumps(adj_data, indent=2, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

        # Simpan mutasi bank sementara jika disk mengizinkan
        try:
            bank_file = target_archive_dir / "mutasi_db_bank.json"
            bank_file.write_text(json.dumps(bank_mutations, indent=2, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

        record_daily_credit_sales(db, audit_date, today_sales)
        db.commit()

        affected_dates = set()
        if selected_cash_debt_ids:
            for debt_id in selected_cash_debt_ids:
                recv_obj = db.query(Receivable).filter(Receivable.id == debt_id).first()
                if recv_obj:
                    if recv_obj.sale_date != audit_date:
                        affected_dates.add(recv_obj.sale_date)
                    settle_receivable(db, debt_id, float(recv_obj.remaining_amount), audit_date)
            db.commit()

        # Rekonsiliasi mutasi bank
        reconcile_res = reconcile_daily_bank_mutations(
            db, 
            audit_date, 
            bank_mutations, 
            today_sales, 
            str(tenant_folders["raw"])
        )
        db.commit()

        # Ambil daftar rekonsiliasi yang sudah memuat status matching
        final_records = reconcile_res.get("reconciled_records") or []
        for r in final_records:
            r["type"] = r.get("tipe", "CR")
            r["mutation_type"] = r.get("tipe", "CR")
            r["amount"] = r.get("nominal", 0.0)

        output_payload = {
            "audit_date": audit_date,
            "total_records": len(final_records),
            "records": final_records
        }

        # Simpan ke mutasi_db_bank.json di disk lokal jika didukung
        try:
            bank_file = target_archive_dir / "mutasi_db_bank.json"
            bank_file.write_text(json.dumps(output_payload, indent=2, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

        # Perhitungan uang fisik & selisih
        total_laci = sum(float(i.get("qty", 1.0)) * float(i.get("unit_price", 0.0)) for i in laci_items)
        total_brankas_out = sum(float(i.get("qty", 1.0)) * float(i.get("unit_price", 0.0)) for i in brankas_items)
        total_cash_fisik_murni = total_laci + vault_in
        total_cash_pdf = float(pdf_data.get("total_cash", 0.0))

        raw_discrepancy = total_cash_fisik_murni - total_cash_pdf

        # Jika selisih minus (uang fisik kurang), penyesuaian positif akan menutup kekurangan tersebut
        if raw_discrepancy < 0 and cash_adjustment > 0:
            net_remaining_discrepancy = raw_discrepancy + cash_adjustment
        else:
            net_remaining_discrepancy = raw_discrepancy - cash_adjustment

        # Hitung net mutasi transfer bank hari ini (CR dikurangi DB)
        net_tf_today = sum(
            float(r.get("amount") if "amount" in r else r.get("nominal", 0.0))
            if str(r.get("tipe", "CR")).upper() == "CR"
            else -float(r.get("amount") if "amount" in r else r.get("nominal", 0.0))
            for r in final_records
        )

        # ================= SIMPAN KE POSTGRESQL SUPABASE (JSONB ARCHIVE) =================
        archive_entry = db.query(DailyAuditArchive).filter(DailyAuditArchive.audit_date == audit_date).first()
        if not archive_entry:
            archive_entry = DailyAuditArchive(audit_date=audit_date)
            db.add(archive_entry)

        archive_entry.vault_cash_in = vault_in
        archive_entry.net_bank_mutation = net_tf_today
        archive_entry.penjualan_pos_pdf = pdf_summary_data
        archive_entry.uang_masuk_brankas = {"nominal": vault_in, "uang_masuk_brankas": vault_in, "input_by": username}
        archive_entry.uang_keluar_laci = {"items": laci_items}
        archive_entry.penyesuaian_kas = adj_data
        archive_entry.mutasi_db_bank = output_payload

        # Evaluasi Machine Learning Risk Engine
        cfg = db.query(MLSetting).first() or MLSetting()
        audit_eval = evaluate_daily_audit_risks(
            current_discrepancy=net_remaining_discrepancy,
            vault_expense=total_brankas_out,
            unmatched_transfers=reconcile_res.get("unmatched_transfers_list", []),
            daily_transactions=today_sales,
            cash_hard_limit=cfg.cash_hard_limit,
            vault_emergency_limit=cfg.vault_emergency_limit,
            model_max_age_days=cfg.model_max_age_days
        )

        # Catat jejak ke audit log
        client_ip, client_loc, lat, lon = detect_client_info(request)
        db.add(AuditActionLog(
            username=username,
            action_type="AUDIT_RUN",
            description=f"Audit {audit_date} [{db_name}]: Brankas=Rp {vault_in:,.0f}, Tunai PDF=Rp {total_cash_pdf:,.0f}, Selisih Awal=Rp {raw_discrepancy:,.0f}, Sisa Selisih=Rp {net_remaining_discrepancy:,.0f}",
            client_ip=client_ip,
            client_location=client_loc,
            latitude=lat,
            longitude=lon
        ))
        db.commit()

        # Sinkronkan laporan audit (abaikan jika write HTML lokal terblokir)
        try:
            sync_audit_report_to_library(request, audit_date, db)
            for aff_date in affected_dates:
                sync_audit_report_to_library(request, aff_date, db)
        except OSError:
            pass

        return {
            "status": "SUCCESS",
            "audit_date": audit_date,
            "cash_collected": total_cash_fisik_murni,
            "system_cash_pdf": total_cash_pdf,
            "discrepancy": net_remaining_discrepancy,
            "ml_evaluation": audit_eval,
            "unmatched_list": reconcile_res.get("unmatched_transfers_list", [])
        }
    except HTTPException:
        db.rollback()
        raise
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Gagal memproses audit: {str(exc)}")

# ================= GENERATOR LAPORAN AUDIT HARIAN =================
@router.get("/api/reports/daily-audit/{audit_date}", response_class=HTMLResponse)
def generate_daily_audit_report(request: Request, audit_date: str, db: Session = Depends(get_db), auto_sync: bool = True):
    db_name = extract_db_name_from_request(request)
    folders = get_tenant_folders(db_name)
    raw_folder = folders["raw"] / audit_date

    # Ambil data arsip resmi dari tabel Supabase
    archive_entry = db.query(DailyAuditArchive).filter(DailyAuditArchive.audit_date == audit_date).first()

    # 1. Ringkasan Penjualan POS PDF
    cash_laporan_pdf = 0.0
    debit_laporan_pdf = 0.0
    kredit_laporan_pdf = 0.0

    if archive_entry and archive_entry.penjualan_pos_pdf:
        p_data = archive_entry.penjualan_pos_pdf
        cash_laporan_pdf = float(p_data.get("total_cash", 0.0))
        debit_laporan_pdf = float(p_data.get("total_debit", 0.0))
        kredit_laporan_pdf = float(p_data.get("total_credit", 0.0))
    else:
        pdf_sum_file = raw_folder / "penjualan_pos_pdf.json"
        if pdf_sum_file.exists():
            try:
                p_data = json.loads(pdf_sum_file.read_text(encoding="utf-8"))
                cash_laporan_pdf = float(p_data.get("total_cash", 0.0))
                debit_laporan_pdf = float(p_data.get("total_debit", 0.0))
                kredit_laporan_pdf = float(p_data.get("total_credit", 0.0))
            except Exception:
                pass

    # 2. Rekap Kas Fisik Murni (Brankas + Laci)
    vault_in = float(archive_entry.vault_cash_in or 0.0) if archive_entry else 0.0
    laci_items = []

    if archive_entry and archive_entry.uang_keluar_laci:
        laci_items = archive_entry.uang_keluar_laci.get("items", [])
    else:
        laci_file = raw_folder / "uang_keluar_laci.json"
        if laci_file.exists():
            try:
                d = json.loads(laci_file.read_text(encoding="utf-8"))
                laci_items = d.get("items", [])
            except Exception:
                pass

    total_keluar_laci = sum(float(i.get("qty", 1.0)) * float(i.get("unit_price", 0.0)) for i in laci_items)
    total_rekap_cash = vault_in + total_keluar_laci

    # Ambil Penyesuaian Kas dari Supabase
    penyesuaian_kas = 0.0
    penyesuaian_ket = ""

    if archive_entry and archive_entry.penyesuaian_kas:
        adj_d = archive_entry.penyesuaian_kas
        penyesuaian_kas = float(adj_d.get("nominal", 0.0))
        penyesuaian_ket = adj_d.get("keterangan", "")
    else:
        adj_file = raw_folder / "penyesuaian_kas.json"
        if adj_file.exists():
            try:
                adj_d = json.loads(adj_file.read_text(encoding="utf-8"))
                penyesuaian_kas = float(adj_d.get("nominal", 0.0))
                penyesuaian_ket = adj_d.get("keterangan", "")
            except Exception:
                pass

    selisih = total_rekap_cash - cash_laporan_pdf

    # Perhitungan sisa selisih kas fisik
    if selisih < 0 and penyesuaian_kas > 0:
        sisa_selisih = selisih + penyesuaian_kas
    elif selisih < 0 and penyesuaian_kas < 0:
        sisa_selisih = selisih - penyesuaian_kas
    else:
        sisa_selisih = selisih - penyesuaian_kas

    # 3. Baca Mutasi Bank & Hitung Total Debit Mutasi (Semua CR Bank)
    debit_matches = []
    kredit_matches = []
    utang_tersimpan_matches = []
    total_debit_mutasi_cr = 0.0

    mb_data = {}
    if archive_entry and archive_entry.mutasi_db_bank:
        mb_data = archive_entry.mutasi_db_bank
    else:
        mb_file = raw_folder / "mutasi_db_bank.json"
        if mb_file.exists():
            try:
                mb_data = json.loads(mb_file.read_text(encoding="utf-8"))
            except Exception:
                mb_data = {}

    if mb_data:
        try:
            for r in mb_data.get("records", []):
                nom_asli = float(r.get("original_nominal") if "original_nominal" in r else (r.get("amount") if "amount" in r else r.get("nominal", 0.0)))
                nom_terpakai = float(r.get("used_nominal") if "used_nominal" in r else (nom_asli if r.get("status_matching") != "UNMATCHED" else 0.0))
                desc = r.get("keterangan", "-")
                matched_with = str(r.get("matched_with", "-") or "-")
                status_m = (r.get("status_matching") or "").upper()
                tipe_m = (r.get("tipe") or "CR").upper()

                if tipe_m == "CR":
                    total_debit_mutasi_cr += nom_asli

                sender_name = desc.split("WS95011")[-1].strip() if "WS95011" in desc else desc
                sender_name = sender_name.replace("TRSF E-BANKING CR", "").replace("0510/FTSCY/WS95271", "").replace("0510/FTSCY/WS95031", "").strip()[:24]

                settled_details = r.get("settled_details", [])

                if status_m in ["MATCHED_SALES_DEBIT", "K_DEBIT_MATCH"]:
                    debit_matches.append({"nominal": nom_terpakai or nom_asli, "sender": sender_name or "Konsumen", "target": matched_with})
                elif status_m in ["MATCHED_TODAY_CREDIT", "TODAY_CREDIT_SETTLED"]:
                    debit_target_desc = matched_with
                    if settled_details:
                        detail_strs = [f"{d['customer_code']}({d['allocated_amount']:,.0f})" for d in settled_details]
                        debit_target_desc = f"manual({', '.join(detail_strs)})"
                    kredit_matches.append({"nominal": nom_terpakai or nom_asli, "sender": sender_name or "Konsumen", "target": debit_target_desc})
                elif status_m in ["MATCHED_DEBT_SETTLED", "OLD_DEBT_SETTLED", "MATCHED_MANUAL"]:
                    target_info = matched_with
                    if settled_details:
                        detail_strs = []
                        for d in settled_details:
                            status_label = "Lunas" if d.get("is_lunas") else f"Sebagian, Sisa: Rp {d.get('remaining', 0):,.0f}"
                            detail_strs.append(f"{d.get('customer_code', '-')} (Nota tgl {d.get('sale_date', audit_date)} - {status_label})")
                        target_info = f"manual({', '.join(detail_strs)})"

                    pakai_val = nom_terpakai if nom_terpakai > 0 else nom_asli
                    utang_tersimpan_matches.append({"nominal": pakai_val, "sender": sender_name or "Konsumen", "target": target_info})
        except Exception:
            pass

    total_debit_mutasi_match = sum(m["nominal"] for m in debit_matches)
    total_kredit_mutasi_match = sum(m["nominal"] for m in kredit_matches)
    total_utang_tersimpan_match = sum(m["nominal"] for m in utang_tersimpan_matches)

    # Total mutasi tercover
    total_semua_mutasi_match = total_debit_mutasi_match + total_kredit_mutasi_match + total_utang_tersimpan_match

    # Saldo lebih debit mutasi bank
    selisih_debit_mutasi = total_debit_mutasi_cr - total_semua_mutasi_match

    # 4. Pelanggan Utang Hari Ini
    receivables_today = (
        db.query(Receivable, Customer)
        .join(Customer, Receivable.customer_id == Customer.id)
        .filter(Receivable.sale_date == audit_date)
        .all()
    )

    today_debts_display = []
    total_utang_keseluruhan = 0.0
    total_pembayaran_utang = 0.0
    total_active_today_debt = 0.0

    for recv, cust in receivables_today:
        code = cust.customer_code
        rem = float(recv.remaining_amount)
        init_amt = float(recv.initial_amount)
        total_utang_keseluruhan += init_amt

        if recv.status == "LUNAS":
            paid_info = f" (Lunas tgl {recv.paid_date})" if recv.paid_date else " (LUNAS)"
            today_debts_display.append({
                "label": f"{code}{paid_info}",
                "amount": init_amt,
                "is_settled": True
            })
            total_pembayaran_utang += init_amt
        elif recv.status == "SEBAGIAN" or (rem < init_amt and rem > 0):
            paid_part = init_amt - rem
            total_pembayaran_utang += paid_part
            today_debts_display.append({
                "label": f"{code} (Bayar Sebagian Rp {paid_part:,.0f})",
                "amount": rem,
                "is_settled": False
            })
            total_active_today_debt += rem
        else:
            today_debts_display.append({
                "label": code,
                "amount": rem,
                "is_settled": False
            })
            total_active_today_debt += rem

    try:
        dt = datetime.strptime(audit_date, "%Y-%m-%d")
        indo_days = ["Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu"]
        indo_months = ["Januari", "Februari", "Maret", "April", "Mei", "Juni", "Juli", "Agustus", "September", "Oktober", "November", "Desember"]
        formatted_date = f"{indo_days[dt.weekday()]}, {dt.day:02d} {indo_months[dt.month - 1]} {dt.year}"
    except Exception:
        formatted_date = audit_date

    laci_rows_html = ""
    for it in laci_items:
        nm = it.get("item_name", "Operasional")
        pr = float(it.get("unit_price", 0.0)) * float(it.get("qty", 1.0))
        laci_rows_html += f"""
        <tr>
          <td class="indent">- {nm}</td>
          <td class="text-right">Rp {pr:,.0f}</td>
        </tr>
        """

    debit_rows_html = ""
    for d in debit_matches:
        debit_rows_html += f"""
        <tr>
          <td>Rp {d['nominal']:,.0f} ({d['sender']})</td>
          <td class="text-right">&rarr; {d['target']}</td>
        </tr>
        """
    if not debit_matches:
        debit_rows_html = "<tr><td colspan='2' class='text-slate-400'>- Tidak ada mutasi debit -</td></tr>"

    kredit_rows_html = ""
    for k in kredit_matches:
        kredit_rows_html += f"""
        <tr>
          <td>Rp {k['nominal']:,.0f} ({k['sender']})</td>
          <td class="text-right">&rarr; {k['target']}</td>
        </tr>
        """
    if not kredit_matches:
        kredit_rows_html = "<tr><td colspan='2' class='text-slate-400'>- Tidak ada mutasi kredit -</td></tr>"

    utang_tersimpan_html = ""
    for u in utang_tersimpan_matches:
        utang_tersimpan_html += f"""
        <tr>
          <td>Rp {u['nominal']:,.0f} ({u['sender']})</td>
          <td class="text-right">&rarr; {u['target']}</td>
        </tr>
        """
    if not utang_tersimpan_matches:
        utang_tersimpan_html = "<tr><td colspan='2' class='text-slate-400'>- Nihil -</td></tr>"

    debts_rows_html = ""
    for db_row in today_debts_display:
        st_style = "text-decoration: line-through; color: #16a34a;" if db_row["is_settled"] else ""
        debts_rows_html += f"""
        <tr>
          <td style="{st_style}">{db_row['label']}</td>
          <td class="text-right" style="{st_style}">Rp {db_row['amount']:,.0f}</td>
        </tr>
        """
    if not today_debts_display:
        debts_rows_html = "<tr><td colspan='2' class='text-slate-400'>- Nihil -</td></tr>"

    if selisih > 0:
        selisih_html = f"Rp {selisih:,.0f} (lebih)"
        selisih_color = "#0b7285"
    elif selisih < 0:
        selisih_html = f"Rp {abs(selisih):,.0f} (kurang)"
        selisih_color = "#e11d48"
    else:
        selisih_html = "Rp 0 (Pas / Balance)"
        selisih_color = "#16a34a"

    if abs(sisa_selisih) < 1.0:
        sisa_selisih_html = "Rp 0 (Pas / Selesai)"
        sisa_color = "#16a34a"
    elif sisa_selisih > 0:
        sisa_selisih_html = f"Rp {sisa_selisih:,.0f} (lebih)"
        sisa_color = "#0b7285"
    else:
        sisa_selisih_html = f"Rp {abs(sisa_selisih):,.0f} (kurang)"
        sisa_color = "#e11d48"

    if selisih_debit_mutasi < 0:
        status_debit_mutasi_html = f"-Rp {abs(selisih_debit_mutasi):,.0f} (kurang debit)"
        status_debit_color = "#e11d48"
    elif selisih_debit_mutasi > 0:
        status_debit_mutasi_html = f"+Rp {selisih_debit_mutasi:,.0f} (lebih debit)"
        status_debit_color = "#0b7285"
    else:
        status_debit_mutasi_html = "Rp 0 (Sesuai / Balance)"
        status_debit_color = "#16a34a"

    html_content = f"""<!DOCTYPE html>
<html lang="id">
<head>
  <meta charset="UTF-8">
  <title>Laporan Audit Harian - {audit_date}</title>
  <style>
    @page {{ size: A4 portrait; margin: 15mm; }}
    body {{
      font-family: Arial, Helvetica, sans-serif;
      color: #1a1a1a;
      background-color: #ffffff;
      margin: 0;
      padding: 20px;
      font-size: 13px;
      line-height: 1.5;
    }}
    .container {{
      max-width: 650px;
      margin: 0 auto;
      border: 1px solid #ddd;
      padding: 24px;
      border-radius: 8px;
    }}
    .header {{
      text-align: center;
      border-bottom: 2px solid #222;
      padding-bottom: 12px;
      margin-bottom: 18px;
    }}
    .header h1 {{ margin: 0; font-size: 20px; letter-spacing: 1px; text-transform: uppercase; }}
    .header p {{ margin: 4px 0 0; color: #555; font-size: 13px; font-weight: bold; }}
    .section-title {{
      font-weight: bold;
      text-transform: uppercase;
      font-size: 12px;
      letter-spacing: 0.5px;
      color: #333;
      border-bottom: 1px solid #ccc;
      padding-bottom: 4px;
      margin-top: 18px;
      margin-bottom: 8px;
    }}
    table {{ width: 100%; border-collapse: collapse; margin-bottom: 6px; }}
    td {{ padding: 4px 0; vertical-align: top; }}
    .text-right {{ text-align: right; }}
    .font-bold {{ font-weight: bold; }}
    .indent {{ padding-left: 16px; }}
    .subtotal-line {{ border-top: 1px dashed #aaa; }}
    .total-line {{ border-top: 1px solid #222; border-bottom: 2px double #222; }}
    .box-summary {{
      background-color: #f8f9fa;
      border: 1px solid #e9ecef;
      border-radius: 6px;
      padding: 10px 14px;
      margin-top: 14px;
    }}
    .footer-note {{
      text-align: center;
      margin-top: 20px;
      font-size: 11px;
      color: #777;
      font-style: italic;
    }}
    .print-bar {{
      max-width: 650px;
      margin: 0 auto 12px auto;
      display: flex;
      justify-content: space-between;
      align-items: center;
    }}
    .btn-print {{
      background: #4f46e5;
      color: #fff;
      border: none;
      padding: 8px 16px;
      border-radius: 6px;
      font-weight: bold;
      cursor: pointer;
    }}
    @media print {{
      .print-bar {{ display: none; }}
      body {{ padding: 0; }}
      .container {{ border: none; padding: 0; }}
    }}
  </style>
</head>
<body>

<div class="print-bar">
  <span style="font-weight: bold; color: #4338ca;">Toko Rina Reconciliation System</span>
  <button class="btn-print" onclick="window.print()">🖨️ Cetak / Unduh PDF</button>
</div>

<div class="container">
  <div class="header">
    <h1>Laporan Audit Harian</h1>
    <p>{formatted_date}</p>
  </div>

  <table>
    <tr>
      <td class="font-bold">CASH LAPORAN</td>
      <td class="text-right">Rp {cash_laporan_pdf:,.0f}</td>
    </tr>
    <tr>
      <td class="font-bold">DEBIT LAPORAN</td>
      <td class="text-right">Rp {debit_laporan_pdf:,.0f}</td>
    </tr>
    <tr>
      <td class="font-bold">DEBIT MUTASI</td>
      <td class="text-right">Rp {total_debit_mutasi_cr:,.0f}</td>
    </tr>
  </table>

  <div class="section-title">Mutasi Match Penjualan Hari Ini</div>
  <div style="font-weight: bold; font-size: 12px; margin: 4px 0;">[DEBIT]</div>
  <table>
    {debit_rows_html}
    <tr class="subtotal-line font-bold">
      <td>Total Debit Match</td>
      <td class="text-right">Rp {total_debit_mutasi_match:,.0f}</td>
    </tr>
  </table>

  <div style="font-weight: bold; font-size: 12px; margin: 8px 0 4px;">[KREDIT]</div>
  <table>
    {kredit_rows_html}
    <tr class="subtotal-line font-bold">
      <td>Total Kredit Match</td>
      <td class="text-right">Rp {total_kredit_mutasi_match:,.0f}</td>
    </tr>
  </table>

  <div class="section-title">Mutasi Match Utang Tersimpan</div>
  <table>
    {utang_tersimpan_html}
    <tr class="subtotal-line font-bold">
      <td>Total</td>
      <td class="text-right">Rp {total_utang_tersimpan_match:,.0f}</td>
    </tr>
  </table>

  <div class="section-title">Pelanggan Utang Hari Ini</div>
  <table>
    {debts_rows_html}
    <tr class="subtotal-line font-bold" style="border-top: 1px solid #aaa;">
      <td>Total Utang Hari Ini</td>
      <td class="text-right">Rp {total_utang_keseluruhan:,.0f}</td>
    </tr>
    <tr class="font-bold" style="color: #16a34a;">
      <td>Pembayaran Dilunasi</td>
      <td class="text-right">-Rp {total_pembayaran_utang:,.0f}</td>
    </tr>
    <tr class="total-line font-bold" style="color: #d97706;">
      <td>Utang Aktif Tersisa</td>
      <td class="text-right">Rp {total_active_today_debt:,.0f}</td>
    </tr>
  </table>

  <div class="section-title">Rekap Cash Fisik</div>
  <table>
    <tr>
      <td>Masuk brankas</td>
      <td class="text-right">Rp {vault_in:,.0f}</td>
    </tr>
    <tr>
      <td colspan="2">Keluar laci:</td>
    </tr>
    {laci_rows_html}
    <tr class="subtotal-line">
      <td class="indent font-bold">Subtotal keluar laci</td>
      <td class="text-right font-bold">Rp {total_keluar_laci:,.0f}</td>
    </tr>
    <tr class="total-line font-bold">
      <td>TOTAL REKAP CASH</td>
      <td class="text-right">Rp {total_rekap_cash:,.0f}</td>
    </tr>
  </table>

  <div class="box-summary">
    <table>
      <tr>
        <td style="width: 44%;">Cash penjualan PDF</td>
        <td style="width: 4%;">:</td>
        <td class="font-bold">Rp {cash_laporan_pdf:,.0f}</td>
      </tr>
      <tr>
        <td>Total rekap cash</td>
        <td>:</td>
        <td class="font-bold">Rp {total_rekap_cash:,.0f}</td>
      </tr>
      <tr>
        <td>Selisih</td>
        <td>:</td>
        <td class="font-bold" style="color: {selisih_color};">{selisih_html}</td>
      </tr>
      <tr>
        <td>Keterangan</td>
        <td>:</td>
        <td>{penyesuaian_ket or '-'}</td>
      </tr>
      <tr style="border-top: 1px dashed #ccc;">
        <td>Sisa selisih</td>
        <td>:</td>
        <td class="font-bold" style="color: {sisa_color};">{sisa_selisih_html}</td>
      </tr>
      <tr style="border-top: 1px dashed #ccc;">
        <td>Status mutasi debit</td>
        <td>:</td>
        <td class="font-bold" style="color: {status_debit_color};">{status_debit_mutasi_html}</td>
      </tr>
    </table>
  </div>

  <div class="footer-note">Dicetak otomatis dari Sistem Audit POS Toko Rina v2.0</div>
</div>

</body>
</html>"""
    
    if auto_sync:
        try:
            sync_audit_report_to_library(request, audit_date, db, html_content)
        except Exception:
            pass

    return HTMLResponse(content=html_content)

# ================= PERPUSTAKAAN DOKUMEN =================
@router.get("/api/documents")
def get_documents(db: Session = Depends(get_db)):
    docs = db.query(UploadedDocument).order_by(UploadedDocument.doc_date.desc(), UploadedDocument.id.desc()).all()
    return [
        {
            "id": d.id,
            "filename": d.filename,
            "file_type": d.file_type,
            "file_size_kb": d.file_size_kb,
            "extra_info": d.extra_info,
            "doc_date": d.doc_date,
            "upload_time": d.upload_time,
            "status": d.status
        }
        for d in docs
    ]


@router.post("/api/documents/upload")
async def upload_document(
    file: UploadFile = File(...),
    doc_date: str = Form(...),
    db: Session = Depends(get_db)
):
    ext = file.filename.split(".")[-1].upper() if "." in file.filename else "FILE"
    ftype = "PDF" if ext == "PDF" else ("CSV" if ext == "CSV" else ("HTML" if ext in ["HTML", "HTM"] else "OTHER"))

    safe_name = f"{doc_date}_{int(datetime.utcnow().timestamp())}_{file.filename}"
    saved_path = DOCS_DIR / safe_name
    content = await file.read()
    
    # Abaikan penulisan disk jika berjalan di lingkungan serverless read-only
    try:
        saved_path.write_bytes(content)
    except OSError:
        pass

    size_kb = round(len(content) / 1024, 1)
    extra = "1 halaman" if ftype == "PDF" else ("File Data" if ftype == "CSV" else "Laporan Audit Resmi")

    new_doc = UploadedDocument(
        filename=file.filename,
        file_path=str(saved_path),
        file_type=ftype,
        file_size_kb=size_kb,
        extra_info=extra,
        doc_date=doc_date,
        upload_time=datetime.now().strftime("%H:%M"),
        status="Belum Diproses"
    )
    db.add(new_doc)
    db.commit()
    return {"status": "SUCCESS", "message": "Dokumen berhasil disimpan ke perpustakaan"}


class EditDocumentPayload(BaseModel):
    doc_id: int
    doc_date: str
    extra_info: Optional[str] = "-"
    status: Optional[str] = "Belum Diproses"


@router.put("/api/documents/edit")
def edit_document(payload: EditDocumentPayload, db: Session = Depends(get_db)):
    doc = db.query(UploadedDocument).filter(UploadedDocument.id == payload.doc_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail="Dokumen tidak ditemukan")

    doc.doc_date = payload.doc_date
    if payload.extra_info:
        doc.extra_info = payload.extra_info
    if payload.status:
        doc.status = payload.status
    db.commit()
    return {"status": "SUCCESS", "message": "Data dokumen berhasil diperbarui!"}


@router.delete("/api/documents/{doc_id}")
def delete_document(doc_id: int, db: Session = Depends(get_db)):
    doc = db.query(UploadedDocument).filter(UploadedDocument.id == doc_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail="Dokumen tidak ditemukan")

    try:
        p = Path(doc.file_path)
        if p.exists():
            p.unlink()
    except Exception:
        pass

    db.delete(doc)
    db.commit()
    return {"status": "SUCCESS", "message": f"Dokumen '{doc.filename}' berhasil dihapus!"}


@router.get("/api/documents/{doc_id}/download")
def download_document(doc_id: int, request: Request, db: Session = Depends(get_db)):
    doc = None
    target_schema = None

    # 1. Coba ambil dari schema tenant yang terdeteksi dari request
    db_name = extract_db_name_from_request(request)
    schemas_to_try = []
    if db_name:
        safe_curr = "".join(c for c in db_name if c.isalnum() or c in ("_", "-")).lower()
        schemas_to_try.append(safe_curr)

    # Tambahkan fallback daftar schema umum jika parameter db tidak terbawa di URL
    for candidate in ["toko_3", "toko_2", "toko_1", "public"]:
        if candidate not in schemas_to_try:
            schemas_to_try.append(candidate)

    # 2. Cari dokumen di schema-schema tersebut sampai ketemu
    for sc in schemas_to_try:
        try:
            if "sqlite" not in str(db.bind.url):
                db.execute(text(f'SET search_path TO "{sc}", public'))
            found = db.query(UploadedDocument).filter(UploadedDocument.id == doc_id).first()
            if found:
                doc = found
                target_schema = sc
                break
        except Exception:
            continue

    if not doc:
        raise HTTPException(status_code=404, detail="Dokumen tidak ditemukan")

    file_obj = Path(doc.file_path) if doc.file_path else None

    # 3. Jika berkas fisik benar-benar ada di disk lokal, kirimkan langsung
    if file_obj and file_obj.exists():
        doc.status = "Terproses"
        db.commit()

        if (doc.file_type or "").upper() == "HTML":
            return FileResponse(
                path=str(file_obj), 
                filename=doc.filename, 
                media_type="text/html; charset=utf-8",
                content_disposition_type="inline"
            )
        return FileResponse(
            path=str(file_obj), 
            filename=doc.filename, 
            media_type="application/octet-stream"
        )

    # 4. Fallback Vercel: Render laporan audit langsung dari Supabase DailyAuditArchive
    if (doc.file_type or "").upper() == "HTML" or (doc.filename or "").endswith(".html"):
        date_match = re.search(r"\d{4}-\d{2}-\d{2}", doc.filename or doc.file_path or "")
        audit_date = date_match.group(0) if date_match else None
        
        if audit_date:
            doc.status = "Terproses"
            db.commit()
            
            rep_response = generate_daily_audit_report(request, audit_date, db, auto_sync=False)
            return HTMLResponse(
                content=rep_response.body,
                headers={"Content-Disposition": f'inline; filename="{doc.filename}"'}
            )

    raise HTTPException(status_code=404, detail="Berkas file tidak ditemukan di sistem")
class StatusTogglePayload(BaseModel):
    status: str


@router.put("/api/documents/{doc_id}/status")
def toggle_document_status(doc_id: int, payload: StatusTogglePayload, db: Session = Depends(get_db)):
    doc = db.query(UploadedDocument).filter(UploadedDocument.id == doc_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail="Dokumen tidak ditemukan")
    doc.status = payload.status
    db.commit()
    return {"status": "SUCCESS", "current_status": doc.status}


# ================= SET INITIAL BALANCES =================
class InitialBalancePayload(BaseModel):
    initial_cash: float = 0.0
    initial_transfer: float = 0.0
    initial_debt: float = 0.0
    username: str = "admin"


@router.post("/api/admin/set-initial-balances")
def set_initial_balances(payload: InitialBalancePayload, request: Request, db: Session = Depends(get_db)):
    cfg = db.query(MLSetting).first()
    if not cfg:
        cfg = MLSetting()
        db.add(cfg)

    cfg.initial_cash_balance = payload.initial_cash
    cfg.initial_transfer_balance = payload.initial_transfer
    cfg.initial_debt_balance = payload.initial_debt
    cfg.updated_by = payload.username
    cfg.updated_at = datetime.utcnow()

    client_ip, client_loc, lat, lon = detect_client_info(request)
    db.add(AuditActionLog(
        username=payload.username,
        action_type="BALANCE_ADJUSTMENT",
        description=f"Penyesuaian Saldo Awal: Cash=Rp {payload.initial_cash:,.0f}, TF=Rp {payload.initial_transfer:,.0f}, Utang=Rp {payload.initial_debt:,.0f}",
        client_ip=client_ip,
        client_location=client_loc,
        latitude=lat,
        longitude=lon
    ))
    db.commit()
    return {"status": "SUCCESS", "message": "Saldo awal berhasil disimpan dan diterapkan!"}


# ================= ML SETTINGS UPDATE =================
class MLSettingPayload(BaseModel):
    cash_hard_limit: float
    vault_emergency_limit: float
    model_max_age_days: int
    username: str = "admin"


@router.post("/api/admin/ml-settings")
def update_ml_settings(payload: MLSettingPayload, request: Request, db: Session = Depends(get_db)):
    cfg = db.query(MLSetting).first()
    if not cfg:
        cfg = MLSetting()
        db.add(cfg)

    cfg.cash_hard_limit = payload.cash_hard_limit
    cfg.vault_emergency_limit = payload.vault_emergency_limit
    cfg.model_max_age_days = payload.model_max_age_days
    cfg.updated_by = payload.username
    cfg.updated_at = datetime.utcnow()

    client_ip, client_loc, lat, lon = detect_client_info(request)
    db.add(AuditActionLog(
        username=payload.username,
        action_type="SETTING_UPDATE",
        description=f"Update Parameter ML: Limit Cash={payload.cash_hard_limit}, Limit Brankas={payload.vault_emergency_limit}, Age={payload.model_max_age_days} hari",
        client_ip=client_ip,
        client_location=client_loc,
        latitude=lat,
        longitude=lon
    ))
    db.commit()
    return {"status": "SUCCESS", "message": "Konfigurasi ML berhasil diperbarui"}


# ================= REKONSILIASI MANUAL DENGAN ALOKASI SISA =================
class BatchManualMatchPayload(BaseModel):
    audit_date: str
    selected_mutation_indices: List[int]
    target_receivable_ids: Optional[List[int]] = None
    target_customer_code: Optional[str] = None
    note: str
    username: str


@router.post("/api/batch-manual-match")
def batch_manual_match(payload: BatchManualMatchPayload, request: Request, db: Session = Depends(get_db)):
    db_name = extract_db_name_from_request(request)
    tenant_folders = get_tenant_folders(db_name)
    folder = tenant_folders["raw"] / payload.audit_date
    mb_file = folder / "mutasi_db_bank.json"

    # 1. Prioritaskan ambil data mutasi dari Supabase (DailyAuditArchive)
    archive_entry = db.query(DailyAuditArchive).filter(DailyAuditArchive.audit_date == payload.audit_date).first()
    data = None
    if archive_entry and archive_entry.mutasi_db_bank:
        data = dict(archive_entry.mutasi_db_bank)
    elif mb_file.exists():
        try:
            data = json.loads(mb_file.read_text(encoding="utf-8"))
        except Exception:
            data = None

    if not data:
        raise HTTPException(status_code=404, detail="Berkas mutasi bank pada tanggal ini tidak ditemukan")

    records = data.get("records", [])

    total_pool_available = 0.0
    for idx in payload.selected_mutation_indices:
        if 0 <= idx < len(records):
            r = records[idx]
            nom = float(r.get("sisa_nominal") if "sisa_nominal" in r else r.get("nominal", 0.0))
            total_pool_available += nom

    affected_dates = set()
    settled_details = []
    remaining_pool = total_pool_available

    # Ambil info mutasi yang dipilih untuk dicatat di piutang
    selected_mut_descs = [
        f"Rp {float(records[idx].get('nominal', 0.0)):,.0f} ({records[idx].get('keterangan', '')})"
        for idx in payload.selected_mutation_indices if 0 <= idx < len(records)
    ]
    mut_source_info = "; ".join(selected_mut_descs)

    if payload.target_receivable_ids:
        for debt_id in payload.target_receivable_ids:
            recv = db.query(Receivable).filter(Receivable.id == debt_id).first()
            if recv and remaining_pool > 0:
                affected_dates.add(recv.sale_date)
                c_code = recv.customer.customer_code if recv.customer else f"BON-{recv.id}"
                pay_amt = min(remaining_pool, float(recv.remaining_amount))
                settle_receivable(db, debt_id, pay_amt, payload.audit_date)
                
                db.flush()
                # Simpan sumber pelunasan mutasi bank ke piutang
                if hasattr(recv, "settlement_note"):
                    recv.settlement_note = f"Transfer Bank {mut_source_info}"

                is_lunas = (recv.status == "LUNAS")
                rem_after = float(recv.remaining_amount)

                settled_details.append({
                    "receivable_id": debt_id,
                    "customer_code": c_code,
                    "sale_date": recv.sale_date,
                    "allocated_amount": pay_amt,
                    "is_lunas": is_lunas,
                    "remaining": rem_after
                })
                remaining_pool -= pay_amt

        db.commit()

    total_used = total_pool_available - remaining_pool
    detail_labels = [f"{d['customer_code']}(Rp {d['allocated_amount']:,.0f})" for d in settled_details]
    label_keterangan = f"Manual ({', '.join(detail_labels)})" if detail_labels else (payload.target_customer_code or "Match Manual")

    temp_used = total_used
    for idx in payload.selected_mutation_indices:
        if 0 <= idx < len(records):
            r = records[idx]
            nom_awal = float(r.get("original_nominal") if "original_nominal" in r else r.get("nominal", 0.0))
            if "original_nominal" not in r:
                r["original_nominal"] = nom_awal

            cur_bal = float(r.get("sisa_nominal") if "sisa_nominal" in r else nom_awal)
            take = min(temp_used, cur_bal)
            sisa_baru = cur_bal - take
            temp_used -= take

            r["sisa_nominal"] = sisa_baru
            r["nominal"] = sisa_baru  # Update nominal aktif agar terbaca sisa di frontend
            r["used_nominal"] = float(r.get("used_nominal", 0.0)) + take
            r["settled_details"] = settled_details
            r["matched_with"] = label_keterangan
            r["catatan_admin"] = payload.note
            r["status_matching"] = "MATCHED_MANUAL"
            
            if sisa_baru > 1.0:
                r["has_leftover"] = True
            else:
                r["has_leftover"] = False

    data["records"] = records

    # 2. Simpan langsung perubahan JSON mutasi ke tabel DailyAuditArchive di Supabase
    if archive_entry:
        archive_entry.mutasi_db_bank = dict(data)
    else:
        archive_entry = DailyAuditArchive(
            audit_date=payload.audit_date,
            mutasi_db_bank=dict(data)
        )
        db.add(archive_entry)

    # 3. Penulisan ke file disk lokal hanya jika filesystem mengizinkan
    try:
        mb_file.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass

    client_ip, client_loc, lat, lon = detect_client_info(request)
    db.add(AuditActionLog(
        username=payload.username,
        action_type="MANUAL_MATCH",
        description=f"Match Manual: {len(payload.selected_mutation_indices)} mutasi CR dipasangkan ke '{label_keterangan}' (Total Terpakai Rp {total_used:,.0f})",
        client_ip=client_ip,
        client_location=client_loc,
        latitude=lat,
        longitude=lon
    ))
    db.commit()

    try:
        sync_audit_report_to_library(request, payload.audit_date, db)
        for aff_d in affected_dates:
            sync_audit_report_to_library(request, aff_d, db)
    except OSError:
        pass

    return {"status": "SUCCESS", "matched_total": total_used, "leftover_total": remaining_pool}
# ================= MASTER DATA PELANGGAN =================
# ================= MASTER DATA PELANGGAN =================
class CustomerPayload(BaseModel):
    customer_id: Optional[int] = None
    customer_code: str
    name: str
    address: str
    phone: str = ""


@router.get("/api/admin/customers")
def get_customers(request: Request):
    db_name = extract_db_name_from_request(request)
    session_factory = get_sessionmaker_for_db(db_name)
    with session_factory() as db:
        if "sqlite" not in str(db.bind.url):
            safe_name = "".join(c for c in db_name if c.isalnum() or c in ("_", "-")).lower() or DEFAULT_DB_KEY
            db.execute(text(f'SET search_path TO "{safe_name}", public'))
        custs = db.query(Customer).order_by(Customer.customer_code.asc()).all()
        return [
            {
                "id": c.id,
                "code": c.customer_code,
                "name": c.name,
                "address": c.address_raw or "-",
                "phone": c.phone or "-"
            }
            for c in custs
        ]


@router.post("/api/admin/customers")
def save_customer(payload: CustomerPayload, request: Request):
    db_name = extract_db_name_from_request(request)
    session_factory = get_sessionmaker_for_db(db_name)
    with session_factory() as db:
        if "sqlite" not in str(db.bind.url):
            safe_name = "".join(c for c in db_name if c.isalnum() or c in ("_", "-")).lower() or DEFAULT_DB_KEY
            db.execute(text(f'SET search_path TO "{safe_name}", public'))

        code_u = payload.customer_code.strip().upper()
        cust = None
        if payload.customer_id:
            cust = db.query(Customer).filter(Customer.id == payload.customer_id).first()

        if not cust:
            cust = db.query(Customer).filter(Customer.customer_code == code_u).first()

        if cust:
            cust.customer_code = code_u
            cust.name = payload.name.strip()
            cust.address_raw = payload.address.strip()
            cust.address_clean = payload.address.strip()
            cust.phone = payload.phone.strip()
        else:
            db.add(Customer(
                customer_code=code_u,
                name=payload.name.strip(),
                address_raw=payload.address.strip(),
                address_clean=payload.address.strip(),
                phone=payload.phone.strip()
            ))
        db.commit()
    return {"status": "SUCCESS", "message": f"Data pelanggan '{code_u}' berhasil disimpan!"}


@router.delete("/api/admin/customers/{customer_id}")
def delete_customer(customer_id: int, request: Request):
    db_name = extract_db_name_from_request(request)
    session_factory = get_sessionmaker_for_db(db_name)
    with session_factory() as db:
        if "sqlite" not in str(db.bind.url):
            safe_name = "".join(c for c in db_name if c.isalnum() or c in ("_", "-")).lower() or DEFAULT_DB_KEY
            db.execute(text(f'SET search_path TO "{safe_name}", public'))

        cust = db.query(Customer).filter(Customer.id == customer_id).first()
        if not cust:
            raise HTTPException(status_code=404, detail="Data pelanggan tidak ditemukan")

        code_u = cust.customer_code
        db.delete(cust)
        db.commit()
    return {"status": "SUCCESS", "message": f"Data pelanggan '{code_u}' berhasil dihapus!"}

# ================= AUDIT LOGS & ARSIP =================
@router.get("/api/admin/audit-logs")
def get_audit_logs(db: Session = Depends(get_db)):
    logs = db.query(AuditActionLog).order_by(AuditActionLog.timestamp.desc()).limit(200).all()
    return [
        {
            "id": l.id,
            "waktu": l.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
            "username": l.username,
            "aksi": l.action_type,
            "deskripsi": l.description,
            "ip": l.client_ip or "-",
            "lokasi": getattr(l, "client_location", "Lokal / Internal") or "Lokal",
            "lat": getattr(l, "latitude", None),
            "lon": getattr(l, "longitude", None)
        }
        for l in logs
    ]


@router.get("/api/folder-files/{date_str}")
def get_folder_files(request: Request, date_str: str):
    db_name = extract_db_name_from_request(request)
    session_factory = get_sessionmaker_for_db(db_name)
    
    with session_factory() as db:
        if "sqlite" not in str(db.bind.url):
            safe_name = "".join(c for c in db_name if c.isalnum() or c in ("_", "-")).lower() or DEFAULT_DB_KEY
            db.execute(text(f'SET search_path TO "{safe_name}", public'))

        # 1. Prioritaskan ambil dari tabel DailyAuditArchive di Supabase
        archive = db.query(DailyAuditArchive).filter(DailyAuditArchive.audit_date == date_str).first()
        if archive:
            return {
                "penjualan_pos_pdf.json": archive.penjualan_pos_pdf or {},
                "uang_masuk_brankas.json": archive.uang_masuk_brankas or {},
                "uang_keluar_laci.json": archive.uang_keluar_laci or {},
                "penyesuaian_kas.json": archive.penyesuaian_kas or {},
                "mutasi_db_bank.json": archive.mutasi_db_bank or {}
            }

        # 2. Cek apakah ada record piutang atau dokumen laporan di tanggal ini
        # Jika tidak ada jejak sama sekali di database, anggap data sudah terhapus dan kembalikan kosong
        has_receivables = db.query(Receivable).filter(Receivable.sale_date == date_str).first() is not None
        has_documents = db.query(UploadedDocument).filter(UploadedDocument.doc_date == date_str).first() is not None

        if not has_receivables and not has_documents:
            return {}

    # 3. Fallback disk lokal: hanya dibaca jika data memang belum dihapus di database
    folders = get_tenant_folders(db_name)
    folder = folders["raw"] / date_str
    if not folder.exists():
        return {}

    files_result = {}
    for f in folder.glob("*.json"):
        try:
            files_result[f.name] = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            files_result[f.name] = {}
            
    return files_result