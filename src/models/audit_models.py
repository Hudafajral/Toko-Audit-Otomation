"""
src/models/audit_models.py
Skema Database Relasional Toko Rina:
Pelanggan, Piutang, Transaksi Kas Itemized, Mutasi Bank, Akun User & Izin Akses (dengan Status Approval & Lokasi GPS/IP),
Log Audit, Setting ML + Saldo Awal, dan Perpustakaan Dokumen.
"""
from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Float, DateTime, ForeignKey, Text
)
from sqlalchemy.orm import relationship
from src.database import Base
from sqlalchemy import Column, Integer, String, Text, Float, DateTime, JSON



class User(Base):
    """Tabel Pengguna & Hak Akses Fitur Toko."""
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), unique=True, nullable=False)
    password_hash = Column(String(128), nullable=False)
    role = Column(String(20), default="kasir")
    status_approval = Column(String(20), default="APPROVED")  # 'PENDING', 'APPROVED', 'REJECTED'
    registered_ip = Column(String(50), default="127.0.0.1")
    registered_location = Column(String(255), default="Lokal / Internal")
    latitude = Column(Float, nullable=True)
    longitude = Column(Float, nullable=True)
    assigned_tenant = Column(String(50), default="toko_1")
    # Daftar tab fitur yang diizinkan (format JSON list string)
    allowed_tabs = Column(Text, default='["homeDashboardTab","auditFormTab","docLibraryTab","piutangTab"]')
    created_at = Column(DateTime, default=datetime.utcnow)


class Customer(Base):
    """Master Pelanggan Toko Rina."""
    __tablename__ = "customers"

    id = Column(Integer, primary_key=True, index=True)
    customer_code = Column(String(50), unique=True, index=True, nullable=False)
    name = Column(String(100), nullable=True)
    address_raw = Column(String(200), nullable=True)
    address_clean = Column(String(200), nullable=True)
    phone = Column(String(30), nullable=True)

    receivables = relationship("Receivable", back_populates="customer")
    aliases = relationship("CustomerAlias", back_populates="customer")


class CustomerAlias(Base):
    """Kamus Pemetaan Nama Rekening Bank ke Pelanggan Toko."""
    __tablename__ = "customer_aliases"

    id = Column(Integer, primary_key=True, index=True)
    customer_id = Column(Integer, ForeignKey("customers.id"), nullable=False)
    alias_name = Column(String(100), index=True, nullable=False)

    customer = relationship("Customer", back_populates="aliases")


class Receivable(Base):
    """Buku Piutang / Catatan Utang Pelanggan."""
    __tablename__ = "receivables"

    id = Column(Integer, primary_key=True, index=True)
    customer_id = Column(Integer, ForeignKey("customers.id"), nullable=False)
    sale_date = Column(String(20), nullable=False)
    sale_invoice = Column(String(50), nullable=True)
    initial_amount = Column(Float, nullable=False)
    remaining_amount = Column(Float, nullable=False)
    status = Column(String(20), default="BELUM")
    paid_date = Column(String(20), nullable=True)

    customer = relationship("Customer", back_populates="receivables")


class ExpenseItem(Base):
    """Rincian Pengeluaran Kas Berbasis Item."""
    __tablename__ = "expense_items"

    id = Column(Integer, primary_key=True, index=True)
    audit_date = Column(String(20), nullable=False, index=True)
    source = Column(String(20), nullable=False)
    item_name = Column(String(150), nullable=False)
    qty = Column(Float, default=1.0)
    unit_price = Column(Float, nullable=False)
    subtotal = Column(Float, nullable=False)
    created_by = Column(String(50), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class AuditActionLog(Base):
    """Audit Trail Aktivitas Sistem."""
    __tablename__ = "audit_action_logs"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), nullable=False)
    action_type = Column(String(50), nullable=False)
    description = Column(Text, nullable=False)
    client_ip = Column(String(50), nullable=True)
    client_location = Column(String(255), default="Lokal / Internal")
    latitude = Column(Float, nullable=True)
    longitude = Column(Float, nullable=True)
    timestamp = Column(DateTime, default=datetime.utcnow)


class MLSetting(Base):
    """Tabel Pengaturan Parameter Deteksi AI & Saldo Awal Toko."""
    __tablename__ = "ml_settings"

    id = Column(Integer, primary_key=True, index=True)
    cash_hard_limit = Column(Float, default=10000.0)
    vault_emergency_limit = Column(Float, default=500000.0)
    model_max_age_days = Column(Integer, default=7)

    # Penyesuaian Saldo Awal (Baseline Card Dashboard)
    initial_cash_balance = Column(Float, default=0.0)
    initial_transfer_balance = Column(Float, default=0.0)
    initial_debt_balance = Column(Float, default=0.0)

    updated_by = Column(String(50), default="admin")
    updated_at = Column(DateTime, default=datetime.utcnow)


class ClusterAbbr(Base):
    """Kamus Singkatan Cluster / Wilayah Pelanggan."""
    __tablename__ = "cluster_abbrs"

    id = Column(Integer, primary_key=True, index=True)
    abbr_code = Column(String(50), unique=True, index=True, nullable=False)
    full_name = Column(String(100), nullable=False)


class CashAuditLog(Base):
    """Log Riwayat Rekonsiliasi Kas Harian."""
    __tablename__ = "cash_audit_logs"

    id = Column(Integer, primary_key=True, index=True)
    audit_date = Column(String(20), unique=True, index=True, nullable=False)
    drawer_cash_out = Column(Float, default=0.0)
    vault_cash_in = Column(Float, default=0.0)
    vault_expense = Column(Float, default=0.0)
    total_physical_cash = Column(Float, default=0.0)
    system_cash_sales = Column(Float, default=0.0)
    discrepancy = Column(Float, default=0.0)
    note = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class BankLedgerDB(Base):
    """Buku Kas Pengeluaran Bank (DB) dan Saldo Berjalan."""
    __tablename__ = "bank_ledger_db"

    id = Column(Integer, primary_key=True, index=True)
    trx_date = Column(String(20), nullable=False)
    amount_out = Column(Float, nullable=False)
    recipient_desc = Column(String(200), nullable=False)
    running_balance = Column(Float, default=0.0)
    created_at = Column(DateTime, default=datetime.utcnow)


class UploadedDocument(Base):
    """Penyimpanan Berkas Perpustakaan Dokumen Toko Rina."""
    __tablename__ = "uploaded_documents"

    id = Column(Integer, primary_key=True, index=True)
    filename = Column(String(200), nullable=False)
    file_path = Column(String(300), nullable=False)
    file_type = Column(String(20), nullable=False)
    file_size_kb = Column(Float, default=0.0)
    extra_info = Column(String(100), default="-")
    doc_date = Column(String(20), nullable=False, index=True)
    upload_time = Column(String(10), default="12:00")
    status = Column(String(30), default="Belum Diproses")
    created_at = Column(DateTime, default=datetime.utcnow)


class DailyAuditArchive(Base):
    __tablename__ = "daily_audit_archives"

    id = Column(Integer, primary_key=True, index=True)
    audit_date = Column(String(20), index=True, nullable=False)
    vault_cash_in = Column(Float, default=0.0)
    net_bank_mutation = Column(Float, default=0.0)
    
    # Simpan 5 file JSON langsung ke dalam kolom JSON/JSONB
    penjualan_pos_pdf = Column(JSON, nullable=True)
    uang_masuk_brankas = Column(JSON, nullable=True)
    uang_keluar_laci = Column(JSON, nullable=True)
    penyesuaian_kas = Column(JSON, nullable=True)
    mutasi_db_bank = Column(JSON, nullable=True)
    
    created_at = Column(DateTime, default=datetime.utcnow)