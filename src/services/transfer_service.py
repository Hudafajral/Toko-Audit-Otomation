"""
src/services/transfer_service.py
Layanan rekonsiliasi transfer bank Toko Rina:
- Mengekstrak nama pengirim asli mutasi bank BCA (teks setelah nominal .00)
- Menghubungkan mutasi bank CR ke transaksi K.Debit hari ini HANYA jika:
  1. Nominal sama persis, DAN
  2. Nama pengirim mutasi benar-benar cocok dengan pelanggan
- Mempertahankan riwayat pencocokan manual (MATCHED_MANUAL) per cabang toko
- Mencatat seluruh status ke data/raw/{store_id}/{audit_date}/mutasi_db_bank.json
"""
from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional
from sqlalchemy.orm import Session

from src.models.audit_models import Customer, CustomerAlias, Receivable
from src.services.receivable_service import settle_receivable_direct


def clean_text_normalize(text_val: Any) -> str:
    """Normalisasi huruf Unicode ke alfabet Latin standar dan huruf kapital."""
    if text_val is None:
        return ""
    s = str(text_val)
    norm = unicodedata.normalize("NFKD", s)
    char_map = {
        "Α": "A", "Β": "B", "Ε": "E", "Ζ": "Z", "Η": "H", "Ι": "I",
        "Κ": "K", "Μ": "M", "Ν": "N", "Ο": "O", "Ρ": "P", "Τ": "T",
        "Υ": "Y", "Χ": "X", "а": "A", "е": "E", "о": "O", "р": "P",
        "с": "C", "у": "Y", "х": "X"
    }
    for k, v in char_map.items():
        norm = norm.replace(k, v)
    cleaned = re.sub(r"[^A-Za-z0-9\s]", " ", norm).upper()
    return " ".join(cleaned.split())


def extract_block_code(text_val: str) -> Optional[str]:
    """Ekstraksi kode blok (contoh: 'PASADENA D 20' -> 'D20', 'H 36' -> 'H36')."""
    clean = clean_text_normalize(text_val)
    m = re.search(r"\b([A-Z])\s*(\d{1,3})\b", clean)
    if m:
        return f"{m.group(1)}{m.group(2)}"
    return None


def extract_sender_name(desc_text: str) -> str:
    """
    Mengekstrak nama pengirim asli dari teks mutasi bank BCA.
    Mengambil bagian setelah nominal dan pecahan desimal (.00).
    """
    raw = str(desc_text or "").strip()
    m = re.search(r"\d+\.00\s*(.*)", raw)
    if m and m.group(1).strip():
        return m.group(1).strip()

    parts = raw.split("/")
    if len(parts) > 1 and parts[-1].strip():
        return parts[-1].strip()

    return raw


def is_name_or_block_matched(cust_identifier: str, sender_raw_text: str) -> bool:
    """
    Mengecek kecocokan antara identitas pelanggan dengan nama pengirim mutasi.
    Menggunakan batas kata utuh (\b) agar tidak salah mendeteksi substring tunggal.
    """
    clean_cust = clean_text_normalize(cust_identifier)
    clean_sender = clean_text_normalize(sender_raw_text)

    if not clean_cust or not clean_sender:
        return False

    # 1. Cek kode blok jika format huruf + angka
    cust_block = extract_block_code(clean_cust)
    sender_block = extract_block_code(clean_sender)
    if cust_block and sender_block and cust_block == sender_block:
        return True

    # 2. Cek kesamaan persis
    if clean_cust == clean_sender:
        return True

    # 3. Cek batas kata utuh (\b)
    pattern = r"\b" + re.escape(clean_cust) + r"\b"
    if re.search(pattern, clean_sender):
        return True

    return False


def reconcile_daily_bank_mutations(
    db: Session,
    audit_date: str,
    mutations: List[Dict[str, Any]],
    today_sales: List[Dict[str, Any]],
    store_id: str = "toko3"
) -> Dict[str, Any]:
    folder_path = Path("data/raw") / store_id / audit_date
    folder_path.mkdir(parents=True, exist_ok=True)
    mb_file = folder_path / "mutasi_db_bank.json"

    # PERTAHANKAN MATCH MANUAL SEBELUMNYA DI TOKO INI
    existing_manual_matches = {}
    if mb_file.exists():
        try:
            old_data = json.loads(mb_file.read_text(encoding="utf-8"))
            for old_r in old_data.get("records", []):
                if old_r.get("status_matching") == "MATCHED_MANUAL":
                    key = f"{old_r.get('tanggal')}_{old_r.get('keterangan')}_{float(old_r.get('nominal', 0.0))}"
                    existing_manual_matches[key] = old_r
        except Exception:
            pass

    db.flush()

    # 1. Siapkan K.Debit Penjualan Hari Ini dari PDF
    today_debit_sales = []
    # 2. Siapkan Bon/Kredit Penjualan Hari Ini dari PDF
    today_credit_sales = []

    for s in today_sales:
        c_code = str(s.get("customer_code") or s.get("customer") or s.get("pelanggan") or s.get("kode_pelanggan") or "").strip()
        k_deb = float(s.get("k_debit") or s.get("debit") or s.get("jml_bayar_k_debit") or 0.0)
        k_kred = float(s.get("kredit") or s.get("credit") or s.get("jml_bayar_kredit") or 0.0)

        if k_deb > 0 and c_code:
            today_debit_sales.append({
                "customer_code": c_code,
                "nominal": k_deb,
                "is_matched": False
            })
        if k_kred > 0 and c_code:
            today_credit_sales.append({
                "customer_code": c_code,
                "nominal": k_kred,
                "is_matched": False
            })

    # 3. Siapkan Seluruh Piutang Aktif di Database Toko Ini
    active_recs = db.query(Receivable).filter(Receivable.status != "LUNAS").all()
    debt_items = []
    for r in active_recs:
        c_code = r.customer.customer_code if r.customer else (getattr(r, "customer_code_raw", "") or "")
        debt_items.append({
            "id": r.id,
            "sale_date": r.sale_date,
            "customer_code": c_code,
            "sale_invoice": r.sale_invoice or f"BON-{r.id}",
            "remaining_amount": float(r.remaining_amount or 0.0),
            "is_settled": False
        })

    # 4. Ambil Kamus Nama dan Alias Pelanggan dari Master Data Toko Ini
    alias_dict: Dict[str, List[str]] = {}
    for c in db.query(Customer).all():
        c_code_clean = clean_text_normalize(c.customer_code)
        if c_code_clean not in alias_dict:
            alias_dict[c_code_clean] = []
        if c.name:
            alias_dict[c_code_clean].append(clean_text_normalize(c.name))
        alias_dict[c_code_clean].append(c_code_clean)

    for a in db.query(CustomerAlias).all():
        if a.customer and a.alias_name:
            c_code_clean = clean_text_normalize(a.customer.customer_code)
            if c_code_clean not in alias_dict:
                alias_dict[c_code_clean] = []
            alias_dict[c_code_clean].append(clean_text_normalize(a.alias_name))

    reconciled_records = []
    unmatched_transfers = []

    # 5. Rekonsiliasi Setiap Baris Mutasi Bank
    for idx, m in enumerate(mutations):
        m_date = str(m.get("date") or audit_date)
        m_desc = str(m.get("description") or "")
        m_type = str(m.get("type") or "CR").upper()
        m_amount = float(m.get("amount") or 0.0)

        match_key = f"{m_date}_{m_desc}_{m_amount}"
        if match_key in existing_manual_matches:
            saved_rec = existing_manual_matches[match_key]
            saved_rec["id"] = idx
            reconciled_records.append(saved_rec)
            continue

        sender_name_only = extract_sender_name(m_desc)

        record_entry = {
            "id": idx,
            "tanggal": m_date,
            "keterangan": m_desc,
            "tipe": m_type,
            "nominal": m_amount,
            "status_matching": "UNMATCHED",
            "matched_with": None,
            "catatan_admin": ""
        }

        if m_type == "DB":
            record_entry["status_matching"] = "EXPENSE_DB"
            record_entry["matched_with"] = "PENGELUARAN_OPERASIONAL"
            reconciled_records.append(record_entry)
            continue

        # TAHAP 1: PRIORITAS PELUNASAN BON HARI INI
        matched_debt_today = False
        for cs in today_credit_sales:
            if cs["is_matched"]:
                continue
            if abs(cs["nominal"] - m_amount) < 1.0:
                c_clean = clean_text_normalize(cs["customer_code"])
                possible_names = alias_dict.get(c_clean, [c_clean])
                name_matches = any(is_name_or_block_matched(n, sender_name_only) for n in possible_names)

                if name_matches:
                    cs["is_matched"] = True
                    matched_debt_today = True
                    record_entry["status_matching"] = "MATCHED_DEBT_SETTLED"
                    record_entry["matched_with"] = f"{cs['customer_code']} (Bon PDF Hari Ini)"
                    record_entry["catatan_admin"] = f"Pelunasan Bon Hari Ini ({cs['customer_code']})"

                    for d in debt_items:
                        if not d["is_settled"] and d["sale_date"] == audit_date:
                            if clean_text_normalize(d["customer_code"]) == c_clean:
                                d["is_settled"] = True
                                settle_receivable_direct(db, d["id"], m_amount, audit_date)
                                break
                    break

        if matched_debt_today:
            reconciled_records.append(record_entry)
            continue

        # TAHAP 2: COCOKKAN KE TRANSAKSI K.DEBIT HARI INI
        matched_sales = False
        for ds in today_debit_sales:
            if ds["is_matched"]:
                continue
            if abs(ds["nominal"] - m_amount) < 1.0:
                c_clean = clean_text_normalize(ds["customer_code"])
                possible_names = alias_dict.get(c_clean, [c_clean])

                has_valid_name = any(is_name_or_block_matched(n, sender_name_only) for n in possible_names)

                if has_valid_name:
                    ds["is_matched"] = True
                    matched_sales = True
                    record_entry["status_matching"] = "MATCHED_SALES_DEBIT"
                    record_entry["matched_with"] = ds["customer_code"]
                    record_entry["catatan_admin"] = f"K.Debit POS ({ds['customer_code']})"
                    break

        if matched_sales:
            reconciled_records.append(record_entry)
            continue

        # TAHAP 3: COCOKKAN KE TABEL PIUTANG DATABASE LAMA
        matched_old_debt = False
        for d in debt_items:
            if d["is_settled"]:
                continue
            if abs(d["remaining_amount"] - m_amount) < 1.0 or m_amount <= d["remaining_amount"]:
                d_clean = clean_text_normalize(d["customer_code"])
                possible_names = alias_dict.get(d_clean, [d_clean])
                has_match = any(is_name_or_block_matched(n, sender_name_only) for n in possible_names)

                if has_match:
                    d["is_settled"] = True
                    matched_old_debt = True
                    record_entry["status_matching"] = "MATCHED_DEBT_SETTLED"
                    record_entry["matched_with"] = f"{d['customer_code']} ({d['sale_invoice']})"
                    record_entry["catatan_admin"] = f"Pelunasan Piutang Bon ({d['sale_invoice']})"
                    settle_receivable_direct(db, d["id"], m_amount, audit_date)
                    break

        if matched_old_debt:
            reconciled_records.append(record_entry)
            continue

        unmatched_transfers.append({
            "index": idx,
            "date": m_date,
            "description": m_desc,
            "amount": m_amount
        })
        reconciled_records.append(record_entry)

    output_data = {
        "store_id": store_id,
        "audit_date": audit_date,
        "total_records": len(reconciled_records),
        "total_unmatched_cr": len(unmatched_transfers),
        "records": reconciled_records
    }
    mb_file.write_text(json.dumps(output_data, indent=2, ensure_ascii=False), encoding="utf-8")

    return {
        "status": "SUCCESS",
        "total_mutations": len(mutations),
        "unmatched_transfers_list": unmatched_transfers,
        "reconciled_records": reconciled_records
    }