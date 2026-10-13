"""
src/routers/web_router.py
API Controller Keuangan POS Toko Rina (Fully Dynamic Multi-Store)
"""
from __future__ import annotations

import io
import json
import re
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from src.database import (
    Base, BASE_DIR, STORE_ENGINES, STORE_SESSIONS, get_store_session, init_all_databases
)
from src.models.audit_models import (
    User, Customer, Receivable, AuditActionLog, MLSetting, UploadedDocument
)
from src.services.ingestion_service import parse_sales_report_pdf, parse_bank_mutation_csv
from src.services.receivable_service import (
    record_daily_credit_sales, get_all_debts_for_table, get_total_receivable_balance, settle_receivable
)
from src.services.transfer_service import reconcile_daily_bank_mutations
from src.services.anomaly_ml_service import evaluate_daily_audit_risks
from src.services.archive_service import save_daily_cash_files

router = APIRouter(tags=["Web App"])
# templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
# Path yang tepat mengarah ke src/templates
TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

def format_store_title(store_id: str) -> str:
    sid = (store_id or "toko3").strip()
    m = re.match(r"toko(\d+)", sid, re.IGNORECASE)
    if m:
        return f"TOKO RINA {m.group(1)}"
    return sid.replace("_", " ").upper()

def get_store_from_request(request: Request, x_store_id: Optional[str] = Header(None)) -> str:
    store = request.query_params.get("store_id") or x_store_id or "toko3"
    return store.strip().lower()

def get_db(request: Request, x_store_id: Optional[str] = Header(None)):
    store_id = get_store_from_request(request, x_store_id)
    db = get_store_session(store_id)
    try:
        yield db
    finally:
        db.close()

def get_store_dirs(store_id: str) -> tuple[Path, Path]:
    valid_store = (store_id or "toko3").strip().lower()
    docs_dir = Path("data/documents") / valid_store
    raw_dir = Path("data/raw") / valid_store
    docs_dir.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)
    return docs_dir, raw_dir

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
    # 1. Pastikan tabel dibuat di semua toko terlebih dahulu
    init_all_databases()

    # 2. Migrasi kolom tambahan jika belum ada di database lama
    cols_to_add = [
        ("ml_settings", "initial_cash_balance", "FLOAT DEFAULT 0.0"),
        ("ml_settings", "initial_transfer_balance", "FLOAT DEFAULT 0.0"),
        ("ml_settings", "initial_debt_balance", "FLOAT DEFAULT 0.0"),
        ("users", "allowed_tabs", "TEXT DEFAULT '[\"homeDashboardTab\",\"auditFormTab\",\"docLibraryTab\",\"piutangTab\"]'"),
        ("users", "assigned_store", "VARCHAR(20) DEFAULT 'toko3'"),
        ("users", "status_approval", "VARCHAR(20) DEFAULT 'APPROVED'"),
        ("users", "registered_ip", "VARCHAR(50) DEFAULT '127.0.0.1'"),
        ("users", "registered_location", "VARCHAR(255) DEFAULT 'Lokal / Internal'"),
        ("users", "latitude", "FLOAT"),
        ("users", "longitude", "FLOAT"),
        ("audit_action_logs", "client_location", "VARCHAR(255) DEFAULT 'Lokal / Internal'"),
        ("audit_action_logs", "latitude", "FLOAT"),
        ("audit_action_logs", "longitude", "FLOAT")
    ]

    for store_name, eng in list(STORE_ENGINES.items()):
        with eng.connect() as conn:
            for tbl, col_name, col_type in cols_to_add:
                try:
                    conn.execute(text(f"ALTER TABLE {tbl} ADD COLUMN {col_name} {col_type};"))
                    conn.commit()
                except Exception:
                    pass

        # 3. Pastikan user admin default ada di setiap toko
        db = STORE_SESSIONS[store_name]()
        try:
            admin_u = db.query(User).filter(User.username == "admin").first()
            if not admin_u:
                db.add(User(
                    username="admin", 
                    password_hash="hudafajral15#", 
                    role="admin", 
                    assigned_store="*",
                    status_approval="APPROVED",
                    allowed_tabs='["*"]',
                    registered_ip="127.0.0.1",
                    registered_location="Sistem Utama (Server)",
                    latitude=-6.2917,
                    longitude=106.7170
                ))
            else:
                admin_u.role = "admin"
                admin_u.assigned_store = "*"
                admin_u.status_approval = "APPROVED"
                admin_u.allowed_tabs = '["*"]'

            if not db.query(MLSetting).first():
                db.add(MLSetting())
            db.commit()
        finally:
            db.close()

@router.get("/", response_class=HTMLResponse)
def index_page(request: Request):
    return templates.TemplateResponse(request=request, name="index.html", context={})

# ================= DAFTAR CABANG TOKO DINAMIS =================
@router.get("/api/stores")
def get_registered_stores():
    stores_set = {"toko1", "toko2", "toko3"}
    for db_file in BASE_DIR.glob("toko_rina_audit_*.db"):
        name = db_file.stem.replace("toko_rina_audit_", "")
        stores_set.add(name)
    
    raw_dir = Path("data/raw")
    if raw_dir.exists():
        for d in raw_dir.iterdir():
            if d.is_dir():
                stores_set.add(d.name)

    result = []
    for sid in sorted(stores_set):
        result.append({
            "id": sid,
            "name": format_store_title(sid)
        })
    return result

# ================= AUTENTIKASI =================
class AuthPayload(BaseModel):
    username: str
    password: str
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    location_name: Optional[str] = None
    store_id: Optional[str] = "toko3"

@router.get("/api/check-user-status/{username}")
def check_user_status(username: str):
    uname = username.strip().lower()
    for s_name in list(STORE_SESSIONS.keys()):
        db = get_store_session(s_name)
        try:
            u = db.query(User).filter(User.username == uname).first()
            if u:
                return {
                    "exists": True, 
                    "status": u.status_approval or "APPROVED",
                    "role": u.role,
                    "assigned_store": getattr(u, "assigned_store", "toko3") or "toko3"
                }
        finally:
            db.close()
    return {"exists": False, "status": "NOT_FOUND"}

@router.post("/api/login")
def login(payload: AuthPayload, request: Request):
    uname = payload.username.strip().lower()
    found_user = None
    found_store = "toko3"

    for s_name in list(STORE_SESSIONS.keys()):
        db = get_store_session(s_name)
        try:
            u = db.query(User).filter(User.username == uname, User.password_hash == payload.password).first()
            if u:
                found_user = u
                found_store = s_name
                break
        finally:
            db.close()

    if not found_user:
        raise HTTPException(status_code=401, detail="Username atau password salah!")

    if found_user.role != "admin" and (found_user.status_approval or "").upper() == "PENDING":
        raise HTTPException(status_code=403, detail="Akun Anda sedang menunggu persetujuan Admin Toko Rina.")
    if found_user.role != "admin" and (found_user.status_approval or "").upper() == "REJECTED":
        raise HTTPException(status_code=403, detail="Permintaan akses akun Anda ditolak oleh Admin.")

    client_ip, client_loc, lat, lon = detect_client_info(
        request, lat=payload.latitude, lon=payload.longitude, client_loc_name=payload.location_name
    )

    allowed = ["*"]
    if found_user.role != "admin":
        try:
            allowed = json.loads(found_user.allowed_tabs or '["homeDashboardTab","auditFormTab","docLibraryTab","piutangTab"]')
        except Exception:
            allowed = ["homeDashboardTab", "auditFormTab", "docLibraryTab", "piutangTab"]

    assigned_store = getattr(found_user, "assigned_store", "toko3") or "toko3"
    effective_store = assigned_store if found_user.role != "admin" else (payload.store_id or "toko3")

    db_log = get_store_session(effective_store)
    try:
        db_log.add(AuditActionLog(
            username=found_user.username,
            action_type="LOGIN",
            description=f"Pengguna '{found_user.username}' berhasil login ({client_loc}) - Toko: {effective_store}",
            client_ip=client_ip,
            client_location=client_loc,
            latitude=lat,
            longitude=lon
        ))
        db_log.commit()
    finally:
        db_log.close()

    return {
        "status": "SUCCESS", 
        "username": found_user.username, 
        "role": found_user.role,
        "assigned_store": assigned_store,
        "active_store": effective_store,
        "allowed_tabs": allowed,
        "client_ip": client_ip,
        "client_location": client_loc,
        "latitude": lat,
        "longitude": lon
    }

@router.post("/api/register-kasir")
def register_kasir(payload: AuthPayload, request: Request):
    uname = payload.username.strip().lower()
    pwd = payload.password.strip()
    if uname == "admin" or pwd == "admin123":
        raise HTTPException(status_code=400, detail="Username/Password kasir tidak boleh sama dengan kredensial Admin!")

    for s_name in list(STORE_SESSIONS.keys()):
        db = get_store_session(s_name)
        try:
            if db.query(User).filter(User.username == uname).first():
                raise HTTPException(status_code=400, detail="Username kasir sudah terdaftar!")
        finally:
            db.close()

    client_ip, client_loc, lat, lon = detect_client_info(
        request, lat=payload.latitude, lon=payload.longitude, client_loc_name=payload.location_name
    )
    default_permissions = json.dumps(["homeDashboardTab", "auditFormTab", "docLibraryTab", "piutangTab"])
    target_store = payload.store_id or "toko3"

    for s_name in list(STORE_SESSIONS.keys()):
        db = get_store_session(s_name)
        try:
            new_user = User(
                username=uname, 
                password_hash=pwd, 
                role="kasir",
                assigned_store=target_store,
                status_approval="PENDING",
                registered_ip=client_ip,
                registered_location=client_loc,
                latitude=lat,
                longitude=lon,
                allowed_tabs=default_permissions
            )
            db.add(new_user)
            db.add(AuditActionLog(
                username="SYSTEM",
                action_type="USER_REGISTER",
                description=f"Kasir baru mendaftar: '{uname}' dari {client_ip} ({client_loc}) - Penugasan: {target_store}. Menunggu persetujuan.",
                client_ip=client_ip,
                client_location=client_loc,
                latitude=lat,
                longitude=lon
            ))
            db.commit()
        finally:
            db.close()

    return {
        "status": "SUCCESS", 
        "message": f"Pendaftaran berhasil! Akun '{uname}' sedang menunggu persetujuan (approval) dari Admin.",
        "assigned_store": target_store,
        "client_ip": client_ip,
        "client_location": client_loc,
        "latitude": lat,
        "longitude": lon
    }

# ================= MANAJEMEN USER =================
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
            "assigned_store": getattr(u, "assigned_store", "toko3") or "toko3",
            "status_approval": u.status_approval or "APPROVED",
            "registered_ip": u.registered_ip or "127.0.0.1",
            "registered_location": u.registered_location or "Lokal",
            "latitude": getattr(u, "latitude", None),
            "longitude": getattr(u, "longitude", None),
            "allowed_tabs": tabs,
            "created_at": u.created_at.strftime("%Y-%m-%d %H:%M") if u.created_at else "-"
        })
    return {"users": out, "pending_count": pending_count}

class UserApprovalPayload(BaseModel):
    user_id: int
    status: str
    assigned_store: Optional[str] = None
    username_admin: str = "admin"

@router.put("/api/admin/users/approval")
def set_user_approval(payload: UserApprovalPayload, request: Request, db: Session = Depends(get_db)):
    target_user = db.query(User).filter(User.id == payload.user_id).first()
    if not target_user:
        raise HTTPException(status_code=404, detail="Pengguna tidak ditemukan")
    if target_user.role == "admin" and target_user.username == "admin":
        raise HTTPException(status_code=400, detail="Akun Admin utama tidak dapat diubah statusnya")

    uname = target_user.username
    status_u = payload.status.upper()
    client_ip, client_loc, lat, lon = detect_client_info(request)

    for s_name in list(STORE_SESSIONS.keys()):
        s_db = get_store_session(s_name)
        try:
            u_obj = s_db.query(User).filter(User.username == uname).first()
            if u_obj:
                u_obj.status_approval = status_u
                if payload.assigned_store:
                    u_obj.assigned_store = payload.assigned_store
                s_db.commit()
        finally:
            s_db.close()

    act_text = "MENYETUJUI" if status_u == "APPROVED" else "MENOLAK"
    db.add(AuditActionLog(
        username=payload.username_admin,
        action_type="USER_APPROVAL",
        description=f"Admin {act_text} aktivasi akun kasir '{uname}' (Store: {payload.assigned_store or 'toko3'})",
        client_ip=client_ip,
        client_location=client_loc,
        latitude=lat,
        longitude=lon
    ))
    db.commit()
    return {"status": "SUCCESS", "message": f"Status akun {uname} berhasil diubah menjadi {status_u}"}

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

    uname = target_user.username
    perm_json = json.dumps(payload.allowed_tabs)

    for s_name in list(STORE_SESSIONS.keys()):
        s_db = get_store_session(s_name)
        try:
            u_obj = s_db.query(User).filter(User.username == uname).first()
            if u_obj:
                u_obj.allowed_tabs = perm_json
                s_db.commit()
        finally:
            s_db.close()

    client_ip, client_loc, lat, lon = detect_client_info(request)
    db.add(AuditActionLog(
        username=payload.username_admin,
        action_type="PERMISSION_UPDATE",
        description=f"Admin memperbarui izin akses akun '{uname}': {len(payload.allowed_tabs)} fitur aktif",
        client_ip=client_ip,
        client_location=client_loc,
        latitude=lat,
        longitude=lon
    ))
    db.commit()
    return {"status": "SUCCESS", "message": f"Izin akses untuk {uname} berhasil disimpan"}

class EditUserPayload(BaseModel):
    user_id: int
    username: str
    role: str
    assigned_store: str = "toko3"
    status_approval: str
    new_password: Optional[str] = None
    admin_operator: str = "admin"

@router.put("/api/admin/users/edit")
def edit_user_profile(payload: EditUserPayload, request: Request, db: Session = Depends(get_db)):
    u = db.query(User).filter(User.id == payload.user_id).first()
    if not u:
        raise HTTPException(status_code=404, detail="User tidak ditemukan")
    if u.username == "admin" and payload.username.lower() != "admin":
        raise HTTPException(status_code=400, detail="Username akun admin utama tidak boleh diubah!")

    old_uname = u.username
    new_uname = payload.username.lower().strip()
    role_u = payload.role.lower().strip()
    status_u = payload.status_approval.upper().strip()
    store_u = payload.assigned_store.strip()

    for s_name in list(STORE_SESSIONS.keys()):
        s_db = get_store_session(s_name)
        try:
            u_obj = s_db.query(User).filter(User.username == old_uname).first()
            if u_obj:
                u_obj.username = new_uname
                u_obj.role = role_u
                u_obj.status_approval = status_u
                u_obj.assigned_store = store_u
                if payload.new_password and len(payload.new_password.strip()) >= 4:
                    u_obj.password_hash = payload.new_password.strip()
                s_db.commit()
        finally:
            s_db.close()

    client_ip, client_loc, lat, lon = detect_client_info(request)
    db.add(AuditActionLog(
        username=payload.admin_operator,
        action_type="USER_EDIT",
        description=f"Admin mengubah profil user '{old_uname}' -> '{new_uname}', Toko: {store_u}, Role: {role_u}, Status: {status_u}",
        client_ip=client_ip,
        client_location=client_loc,
        latitude=lat,
        longitude=lon
    ))
    db.commit()
    return {"status": "SUCCESS", "message": f"Data user {new_uname} berhasil diperbarui!"}

@router.delete("/api/admin/users/{user_id}")
def delete_user(user_id: int, request: Request, admin_operator: str = "admin", db: Session = Depends(get_db)):
    u = db.query(User).filter(User.id == user_id).first()
    if not u:
        raise HTTPException(status_code=404, detail="User tidak ditemukan")
    if u.username == "admin":
        raise HTTPException(status_code=400, detail="Akun admin utama tidak boleh dihapus!")

    uname = u.username
    for s_name in list(STORE_SESSIONS.keys()):
        s_db = get_store_session(s_name)
        try:
            u_obj = s_db.query(User).filter(User.username == uname).first()
            if u_obj:
                s_db.delete(u_obj)
                s_db.commit()
        finally:
            s_db.close()

    client_ip, client_loc, lat, lon = detect_client_info(request)
    db.add(AuditActionLog(
        username=admin_operator,
        action_type="USER_DELETE",
        description=f"Admin menghapus akun user '{uname}' di seluruh cabang",
        client_ip=client_ip,
        client_location=client_loc,
        latitude=lat,
        longitude=lon
    ))
    db.commit()
    return {"status": "SUCCESS", "message": f"Akun {uname} berhasil dihapus permanen!"}

# ================= DASHBOARD SUMMARY =================
@router.get("/api/dashboard-summary")
def get_dashboard_summary(request: Request, db: Session = Depends(get_db)):
    store_id = get_store_from_request(request)
    docs_dir, raw_dir = get_store_dirs(store_id)

    debts = get_all_debts_for_table(db, include_settled=True)
    total_debt = get_total_receivable_balance(db)

    cfg = db.query(MLSetting).first()
    init_cash = float(getattr(cfg, "initial_cash_balance", 0.0) or 0.0) if cfg else 0.0
    init_tf = float(getattr(cfg, "initial_transfer_balance", 0.0) or 0.0) if cfg else 0.0
    init_debt = float(getattr(cfg, "initial_debt_balance", 0.0) or 0.0) if cfg else 0.0

    raw_dirs = sorted([d for d in raw_dir.glob("*") if d.is_dir()], reverse=True)

    total_kumulatif_cash_brankas = init_cash
    total_kumulatif_net_tf = init_tf
    latest_db_items = []
    unmatched_mutations = []
    available_dates = [d.name for d in raw_dirs]

    for date_folder in raw_dirs:
        vm_path = date_folder / "uang_masuk_brankas.json"
        if vm_path.exists():
            try:
                vm_data = json.loads(vm_path.read_text(encoding="utf-8"))
                val = float(vm_data.get("nominal") or vm_data.get("uang_masuk_brankas") or 0.0)
                total_kumulatif_cash_brankas += val
            except Exception:
                pass

        mb_path = date_folder / "mutasi_db_bank.json"
        if mb_path.exists():
            try:
                mb_data = json.loads(mb_path.read_text(encoding="utf-8"))
                records = mb_data.get("records", [])
                cr = sum(r.get("nominal", 0.0) for r in records if r.get("tipe") == "CR")
                db_exp = sum(r.get("nominal", 0.0) for r in records if r.get("tipe") == "DB")
                total_kumulatif_net_tf += (cr - db_exp)

                if not unmatched_mutations:
                    for idx, r in enumerate(records):
                        if r.get("tipe") == "CR":
                            nom_asli = float(r.get("original_nominal") if "original_nominal" in r else r.get("nominal", 0.0))
                            nom_aktif = float(r.get("sisa_nominal") if "sisa_nominal" in r else nom_asli)
                            is_unmatched = r.get("status_matching") == "UNMATCHED"
                            has_leftover = nom_aktif > 1.0 and (nom_aktif < nom_asli or r.get("has_leftover", False))

                            if is_unmatched or has_leftover:
                                unmatched_mutations.append({
                                    **r,
                                    "id": idx,
                                    "nominal": nom_aktif,
                                    "original_nominal": nom_asli,
                                    "is_leftover": has_leftover
                                })
                    latest_db_items = [r for r in records if r.get("tipe") == "DB"]
            except Exception:
                pass

    pending_users = db.query(User).filter(User.status_approval == "PENDING").count()

    return {
        "active_store": store_id,
        "total_cash_fisik": total_kumulatif_cash_brankas,
        "cash_brankas": total_kumulatif_cash_brankas,
        "total_active_debt": total_debt + init_debt,
        "net_transfer": total_kumulatif_net_tf,
        "debts_table": debts,
        "latest_db_items": latest_db_items,
        "unmatched_mutations": unmatched_mutations,
        "available_dates": available_dates,
        "pending_registrations_count": pending_users,
        "initial_balances": {
            "cash": init_cash,
            "transfer": init_tf,
            "debt": init_debt
        },
        "ml_settings": {
            "cash_hard_limit": cfg.cash_hard_limit if cfg else 10000.0,
            "vault_emergency_limit": cfg.vault_emergency_limit if cfg else 500000.0,
            "model_max_age_days": cfg.model_max_age_days if cfg else 7
        }
    }

# ================= FUNGSI GENERATOR HTML LAPORAN MURNI =================
def build_daily_audit_report_html(audit_date: str, store_id: str, db: Session) -> str:
    active_store = (store_id or "toko3").strip().lower()
    _, raw_dir = get_store_dirs(active_store)
    raw_folder = raw_dir / audit_date
    header_store_title = format_store_title(active_store)

    cash_laporan_pdf = 0.0
    debit_laporan_pdf = 0.0
    kredit_laporan_pdf = 0.0

    pdf_sum_file = raw_folder / "penjualan_pos_pdf.json"
    if pdf_sum_file.exists():
        try:
            p_data = json.loads(pdf_sum_file.read_text(encoding="utf-8"))
            cash_laporan_pdf = float(p_data.get("total_cash", 0.0))
            debit_laporan_pdf = float(p_data.get("total_debit", 0.0))
            kredit_laporan_pdf = float(p_data.get("total_credit", 0.0))
        except Exception:
            pass

    vault_in = 0.0
    laci_items = []
    vm_file = raw_folder / "uang_masuk_brankas.json"
    if vm_file.exists():
        try:
            d = json.loads(vm_file.read_text(encoding="utf-8"))
            vault_in = float(d.get("nominal") or d.get("uang_masuk_brankas") or 0.0)
        except Exception:
            pass

    laci_file = raw_folder / "uang_keluar_laci.json"
    if laci_file.exists():
        try:
            d = json.loads(laci_file.read_text(encoding="utf-8"))
            laci_items = d.get("items", [])
        except Exception:
            pass

    total_keluar_laci = sum(float(i.get("qty", 1.0)) * float(i.get("unit_price", 0.0)) for i in laci_items)
    total_rekap_cash = vault_in + total_keluar_laci

    penyesuaian_kas = 0.0
    penyesuaian_ket = ""
    adj_file = raw_folder / "penyesuaian_kas.json"
    if adj_file.exists():
        try:
            d = json.loads(adj_file.read_text(encoding="utf-8"))
            penyesuaian_kas = float(d.get("nominal", 0.0))
            penyesuaian_ket = d.get("keterangan", "")
        except Exception:
            pass

    selisih = total_rekap_cash - cash_laporan_pdf
    sisa_selisih = selisih - penyesuaian_kas

    debit_matches = []
    kredit_matches = []
    utang_tersimpan_matches = []
    total_debit_mutasi_cr = 0.0

    mb_file = raw_folder / "mutasi_db_bank.json"
    if mb_file.exists():
        try:
            mb_data = json.loads(mb_file.read_text(encoding="utf-8"))
            for r in mb_data.get("records", []):
                nom_asli = float(r.get("nominal", 0.0))
                nom_terpakai = float(r.get("used_nominal") if "used_nominal" in r else nom_asli)
                desc = r.get("keterangan", "-")
                matched_with = str(r.get("matched_with", "-") or "-")
                status_m = (r.get("status_matching") or "").upper()
                tipe_m = (r.get("tipe") or "CR").upper()

                if tipe_m == "CR":
                    total_debit_mutasi_cr += nom_asli

                sender_name = desc.split("WS95011")[-1].strip() if "WS95011" in desc else desc
                sender_name = sender_name.replace("TRSF E-BANKING CR", "").replace("0510/FTSCY/WS95271", "").replace("0510/FTSCY/WS95031", "").strip()[:24]

                is_today_debt = False
                if "BON PDF HARI INI" in matched_with.upper():
                    is_today_debt = True

                settled_details = r.get("settled_details", [])
                if settled_details:
                    all_today = all(item.get("sale_date") == audit_date for item in settled_details)
                    if all_today:
                        is_today_debt = True

                if status_m in ["MATCHED_SALES_DEBIT", "K_DEBIT_MATCH"]:
                    debit_matches.append({"nominal": nom_terpakai, "sender": sender_name or "Konsumen", "target": matched_with})
                elif status_m in ["MATCHED_TODAY_CREDIT", "TODAY_CREDIT_SETTLED"] or is_today_debt:
                    debit_target_desc = matched_with
                    if settled_details:
                        detail_strs = [f"{d['customer_code']}({d['allocated_amount']:,.0f})" for d in settled_details]
                        debit_target_desc = f"manual({', '.join(detail_strs)})"
                    kredit_matches.append({"nominal": nom_terpakai, "sender": sender_name or "Konsumen", "target": debit_target_desc})
                elif status_m in ["MATCHED_DEBT_SETTLED", "OLD_DEBT_SETTLED", "MATCHED_MANUAL"]:
                    if is_today_debt:
                        continue

                    target_info = matched_with
                    if settled_details:
                        detail_strs = []
                        for d in settled_details:
                            status_label = "Lunas" if d.get("is_lunas") else f"Sebagian, Sisa: Rp {d.get('remaining', 0):,.0f}"
                            detail_strs.append(f"{d['customer_code']} (Nota tgl {d['sale_date']} - {status_label})")
                        target_info = f"manual({', '.join(detail_strs)})"

                    utang_tersimpan_matches.append({"nominal": nom_terpakai, "sender": sender_name or "Konsumen", "target": target_info})
        except Exception:
            pass

    total_debit_mutasi_match = sum(m["nominal"] for m in debit_matches)
    total_kredit_mutasi_match = sum(m["nominal"] for m in kredit_matches)
    total_utang_tersimpan_match = sum(m["nominal"] for m in utang_tersimpan_matches)

    total_semua_mutasi_match = total_debit_mutasi_match + total_kredit_mutasi_match + total_utang_tersimpan_match
    selisih_debit_mutasi = total_semua_mutasi_match - total_debit_mutasi_cr

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
        selisih_html = f"+Rp {selisih:,.0f} (lebih)"
        selisih_color = "#0b7285"
    elif selisih < 0:
        selisih_html = f"-Rp {abs(selisih):,.0f} (kurang)"
        selisih_color = "#e11d48"
    else:
        selisih_html = "Rp 0 (Pas / Balance)"
        selisih_color = "#16a34a"

    if sisa_selisih > 0:
        sisa_selisih_html = f"+Rp {sisa_selisih:,.0f} (lebih)"
        sisa_color = "#0b7285"
    elif sisa_selisih < 0:
        sisa_selisih_html = f"-Rp {abs(sisa_selisih):,.0f} (kurang)"
        sisa_color = "#e11d48"
    else:
        sisa_selisih_html = "Rp 0 (Pas / Selesai)"
        sisa_color = "#16a34a"

    if selisih_debit_mutasi < 0:
        status_debit_mutasi_html = f"-Rp {abs(selisih_debit_mutasi):,.0f} (kurang debit)"
        status_debit_color = "#e11d48"
    elif selisih_debit_mutasi > 0:
        status_debit_mutasi_html = f"+Rp {selisih_debit_mutasi:,.0f} (lebih debit)"
        status_debit_color = "#0b7285"
    else:
        status_debit_mutasi_html = "Rp 0 (Sesuai / Balance)"
        status_debit_color = "#16a34a"

    return f"""<!DOCTYPE html>
<html lang="id">
<head>
  <meta charset="UTF-8">
  <title>Laporan Audit Harian - {header_store_title} - {audit_date}</title>
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
    .header h1 {{ margin: 0; font-size: 19px; letter-spacing: 1px; text-transform: uppercase; }}
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
  <span style="font-weight: bold; color: #4338ca;">{header_store_title} Reconciliation System</span>
  <button class="btn-print" onclick="window.print()">🖨️ Cetak / Unduh PDF</button>
</div>

<div class="container">
  <div class="header">
    <h1>Laporan Audit Harian - {header_store_title}</h1>
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

  <div class="footer-note">Dicetak otomatis dari Sistem Audit POS {header_store_title} v2.0</div>
</div>

</body>
</html>"""

# ================= HELPER SINKRONISASI LAPORAN =================
def sync_audit_report_to_library(audit_date: str, store_id: str, db: Session, html_content: Optional[str] = None) -> None:
    docs_dir, _ = get_store_dirs(store_id)
    if not html_content:
        html_content = build_daily_audit_report_html(audit_date=audit_date, store_id=store_id, db=db)

    filename = f"Laporan_Audit_{audit_date}.html"
    file_path = docs_dir / filename
    file_path.write_text(html_content, encoding="utf-8")
    size_kb = round(len(html_content.encode("utf-8")) / 1024, 1)

    doc = db.query(UploadedDocument).filter(
        UploadedDocument.doc_date == audit_date,
        UploadedDocument.filename == filename
    ).first()

    label_toko = f"Rekap {format_store_title(store_id)}"
    if not doc:
        new_doc = UploadedDocument(
            filename=filename,
            file_path=str(file_path),
            file_type="HTML",
            file_size_kb=size_kb,
            extra_info=label_toko,
            doc_date=audit_date,
            upload_time=datetime.now().strftime("%H:%M"),
            status="Terproses"
        )
        db.add(new_doc)
    else:
        doc.file_size_kb = size_kb
        doc.extra_info = label_toko
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
    store_id: Optional[str] = Form(None),
    db: Session = Depends(get_db)
):
    try:
        active_store = (store_id or request.query_params.get("store_id") or "toko3").strip().lower()
        docs_dir, raw_dir = get_store_dirs(active_store)

        pdf_bytes = await pdf_file.read()
        csv_bytes = await csv_file.read()

        pdf_data = parse_sales_report_pdf(io.BytesIO(pdf_bytes))
        audit_date = pdf_data.get("audit_date")
        if not audit_date:
            raise HTTPException(status_code=400, detail="Tanggal transaksi pada berkas PDF tidak dapat diidentifikasi.")

        target_archive_dir = raw_dir / audit_date
        target_archive_dir.mkdir(parents=True, exist_ok=True)
        existing_marker = target_archive_dir / "uang_masuk_brankas.json"

        if existing_marker.exists() and not override_existing:
            raise HTTPException(
                status_code=409,
                detail=f"Laporan transaksi tanggal {audit_date} di {format_store_title(active_store)} sudah pernah diaudit. Centang opsi timpa data jika ingin memproses ulang!"
            )

        bank_mutations = parse_bank_mutation_csv(io.BytesIO(csv_bytes))
        today_sales = pdf_data.get("records", [])

        laci_items = json.loads(laci_items_json) if laci_items_json else []
        brankas_items = json.loads(brankas_items_json) if brankas_items_json else []

        save_daily_cash_files(
            audit_date=audit_date,
            vault_in=vault_in,
            drawer_items=laci_items,
            vault_items=brankas_items,
            input_by=username,
            base_dir="data/raw",
            store_id=active_store
        )
        
        pdf_summary_file = target_archive_dir / "penjualan_pos_pdf.json"
        pdf_summary_data = {
            "total_cash": float(pdf_data.get("total_cash", 0.0)),
            "total_debit": float(pdf_data.get("total_debit", 0.0)),
            "total_credit": float(pdf_data.get("total_credit", 0.0)),
            "records": today_sales
        }
        pdf_summary_file.write_text(json.dumps(pdf_summary_data, indent=2, ensure_ascii=False), encoding="utf-8")

        selected_cash_debt_ids = []
        try:
            selected_cash_debt_ids = json.loads(cash_debt_receivable_ids_json or "[]")
        except Exception:
            pass

        adj_file = target_archive_dir / "penyesuaian_kas.json"
        adj_data = {
            "nominal": cash_adjustment,
            "keterangan": cash_adjustment_note or "-",
            "receivable_ids_settled": selected_cash_debt_ids,
            "tanggal": audit_date
        }
        adj_file.write_text(json.dumps(adj_data, indent=2, ensure_ascii=False), encoding="utf-8")

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

        reconcile_res = reconcile_daily_bank_mutations(db, audit_date, bank_mutations, today_sales, store_id=active_store)
        db.commit()

        total_laci = sum(float(i.get("qty", 1.0)) * float(i.get("unit_price", 0.0)) for i in laci_items)
        total_brankas_out = sum(float(i.get("qty", 1.0)) * float(i.get("unit_price", 0.0)) for i in brankas_items)
        total_cash_fisik_murni = total_laci + vault_in
        total_cash_pdf = float(pdf_data.get("total_cash", 0.0))

        raw_discrepancy = total_cash_fisik_murni - total_cash_pdf
        net_remaining_discrepancy = raw_discrepancy - cash_adjustment

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

        client_ip, client_loc, lat, lon = detect_client_info(request)
        db.add(AuditActionLog(
            username=username,
            action_type="AUDIT_RUN",
            description=f"Audit {format_store_title(active_store)} {audit_date}: Brankas=Rp {vault_in:,.0f}, Tunai PDF=Rp {total_cash_pdf:,.0f}, Sisa Selisih=Rp {net_remaining_discrepancy:,.0f}",
            client_ip=client_ip,
            client_location=client_loc,
            latitude=lat,
            longitude=lon
        ))
        db.commit()

        sync_audit_report_to_library(audit_date, active_store, db)
        for aff_date in affected_dates:
            sync_audit_report_to_library(aff_date, active_store, db)

        return {
            "status": "SUCCESS",
            "store_id": active_store,
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

# ================= ROUTE LAPORAN AUDIT HARIAN =================
@router.get("/api/reports/daily-audit/{audit_date}", response_class=HTMLResponse)
def generate_daily_audit_report(
    audit_date: str, 
    request: Request,
    store_id: Optional[str] = None, 
    db: Session = Depends(get_db)
):
    active_store = (store_id or request.query_params.get("store_id") or "toko3").strip().lower()
    html_content = build_daily_audit_report_html(audit_date=audit_date, store_id=active_store, db=db)
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
    request: Request,
    file: UploadFile = File(...),
    doc_date: str = Form(...),
    store_id: Optional[str] = Form(None),
    db: Session = Depends(get_db)
):
    active_store = (store_id or request.query_params.get("store_id") or "toko3").strip().lower()
    docs_dir, _ = get_store_dirs(active_store)

    ext = file.filename.split(".")[-1].upper() if "." in file.filename else "FILE"
    ftype = "PDF" if ext == "PDF" else ("CSV" if ext == "CSV" else ("HTML" if ext in ["HTML", "HTM"] else "OTHER"))

    safe_name = f"{doc_date}_{int(datetime.utcnow().timestamp())}_{file.filename}"
    saved_path = docs_dir / safe_name
    content = await file.read()
    saved_path.write_bytes(content)

    size_kb = round(len(content) / 1024, 1)
    extra = "1 halaman" if ftype == "PDF" else ("File Data" if ftype == "CSV" else f"Rekap {format_store_title(active_store)}")

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
    return {"status": "SUCCESS", "message": f"Dokumen berhasil disimpan ke perpustakaan {format_store_title(active_store)}"}

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
def download_document(doc_id: int, db: Session = Depends(get_db)):
    doc = db.query(UploadedDocument).filter(UploadedDocument.id == doc_id).first()
    if not doc or not Path(doc.file_path).exists():
        raise HTTPException(status_code=404, detail="Berkas file tidak ditemukan di sistem")

    doc.status = "Terproses"
    db.commit()

    if (doc.file_type or "").upper() == "HTML":
        return FileResponse(
            path=doc.file_path, 
            filename=doc.filename, 
            media_type="text/html; charset=utf-8",
            content_disposition_type="inline"
        )

    return FileResponse(
        path=doc.file_path, 
        filename=doc.filename, 
        media_type="application/octet-stream"
    )

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

# ================= REKONSILIASI MANUAL =================
class BatchManualMatchPayload(BaseModel):
    audit_date: str
    selected_mutation_indices: List[int]
    target_receivable_ids: Optional[List[int]] = None
    target_customer_code: Optional[str] = None
    note: str
    username: str
    store_id: Optional[str] = None

@router.post("/api/batch-manual-match")
def batch_manual_match(payload: BatchManualMatchPayload, request: Request, db: Session = Depends(get_db)):
    active_store = (payload.store_id or request.query_params.get("store_id") or "toko3").strip().lower()
    _, raw_dir = get_store_dirs(active_store)
    folder = raw_dir / payload.audit_date
    mb_file = folder / "mutasi_db_bank.json"
    if not mb_file.exists():
        raise HTTPException(status_code=404, detail="Berkas mutasi bank pada tanggal ini tidak ditemukan")

    data = json.loads(mb_file.read_text(encoding="utf-8"))
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

    if payload.target_receivable_ids:
        for debt_id in payload.target_receivable_ids:
            recv = db.query(Receivable).filter(Receivable.id == debt_id).first()
            if recv and remaining_pool > 0:
                affected_dates.add(recv.sale_date)
                c_code = recv.customer.customer_code if recv.customer else f"BON-{recv.id}"
                pay_amt = min(remaining_pool, float(recv.remaining_amount))
                settle_receivable(db, debt_id, pay_amt, payload.audit_date)
                
                db.flush()
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

    detail_labels = [f"{d['customer_code']}({d['allocated_amount']:,.0f})" for d in settled_details]
    label_keterangan = f"manual({', '.join(detail_labels)})" if detail_labels else (payload.target_customer_code or "Match Manual")

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
    mb_file.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    client_ip, client_loc, lat, lon = detect_client_info(request)
    db.add(AuditActionLog(
        username=payload.username,
        action_type="MANUAL_MATCH",
        description=f"Match Manual {format_store_title(active_store)}: {len(payload.selected_mutation_indices)} mutasi CR dipasangkan ke '{label_keterangan}' (Total Terpakai Rp {total_used:,.0f})",
        client_ip=client_ip,
        client_location=client_loc,
        latitude=lat,
        longitude=lon
    ))
    db.commit()

    sync_audit_report_to_library(payload.audit_date, active_store, db)
    for aff_d in affected_dates:
        sync_audit_report_to_library(aff_d, active_store, db)

    return {"status": "SUCCESS", "matched_total": total_used, "leftover_total": remaining_pool}

# ================= MASTER DATA PELANGGAN =================
class CustomerPayload(BaseModel):
    customer_id: Optional[int] = None
    customer_code: str
    name: str
    address: str
    phone: str = ""

@router.get("/api/admin/customers")
def get_customers(db: Session = Depends(get_db)):
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
def save_customer(payload: CustomerPayload, db: Session = Depends(get_db)):
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
def delete_customer(customer_id: int, db: Session = Depends(get_db)):
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
def get_folder_files(date_str: str, request: Request, store_id: Optional[str] = None):
    active_store = (store_id or request.query_params.get("store_id") or "toko3").strip().lower()
    _, raw_dir = get_store_dirs(active_store)
    folder = raw_dir / date_str
    if not folder.exists():
        return {}

    files_result = {}
    for f in folder.glob("*.json"):
        try:
            files_result[f.name] = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            files_result[f.name] = {}
    return files_result