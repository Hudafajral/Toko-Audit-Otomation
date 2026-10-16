"""
reset.py
Script pembersih dan reset database multi-tenant (Toko 1, Toko 2, Toko 3).
"""
import shutil
from pathlib import Path
from src.database import DATA_DIR, get_engine_for_db, get_sessionmaker_for_db, get_tenant_folders, Base
from src.models.audit_models import User, MLSetting

def reset_all_tenants():
    print("⏳ Membersihkan seluruh database dan arsip tenant...")

    # 1. Hapus folder tenants lama beserta semua file .db dan arsipnya
    if DATA_DIR.exists():
        shutil.rmtree(DATA_DIR)
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # 2. Inisialisasi ulang 3 toko default
    default_dbs = ["toko_1", "toko_2", "toko_3"]
    for db_key in default_dbs:
        engine = get_engine_for_db(db_key)
        get_tenant_folders(db_key)
        Base.metadata.create_all(bind=engine)

        session_factory = get_sessionmaker_for_db(db_key)
        with session_factory() as db:
            # Buat akun admin default
            admin = User(
                username="admin",
                password_hash="admin123",
                role="admin",
                status_approval="APPROVED",
                allowed_tabs='["*"]',
                registered_ip="127.0.0.1",
                registered_location="Sistem Utama (Server)"
            )
            db.add(admin)
            db.add(MLSetting())
            db.commit()

    print("✔ Akun default admin dan 3 database toko siap digunakan.")
    print("Selesai! Sistem sudah kembali bersih dari awal (fresh state).")

if __name__ == "__main__":
    reset_all_tenants()