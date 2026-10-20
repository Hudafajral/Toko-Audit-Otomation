"""
src/services/ingestion_service.py
Layanan ekstraksi Laporan Penjualan POS Toko Rina & Mutasi CSV Bank BCA.
Hanya mengambil baris transaksi riil (mengabaikan no rekening, saldo, & ringkasan footer).
"""
from __future__ import annotations

import csv
import io
import re
from typing import Any, Dict, List, Union
import pdfplumber


def parse_rupiah_number(val: Any) -> float:
    """Mengonversi teks angka format rupiah ke float murni."""
    if val is None:
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip()
    if not s or s == "-":
        return 0.0
    s = re.sub(r"[^\d.,\-]", "", s)
    if "." in s and "," in s:
        s = s.replace(".", "").replace(",", ".")
    elif "." in s:
        s = s.replace(".", "")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return 0.0


def parse_sales_report_pdf(file_source: Union[io.BytesIO, bytes, str]) -> Dict[str, Any]:
    """Mengekstrak tanggal dan rincian transaksi per pelanggan dari PDF."""
    if isinstance(file_source, bytes):
        file_source = io.BytesIO(file_source)

    records: List[Dict[str, Any]] = []
    audit_date = ""
    total_tunai_pdf = 0.0
    total_kredit_pdf = 0.0
    total_debit_pdf = 0.0

    with pdfplumber.open(file_source) as pdf:
        full_text = ""
        for page in pdf.pages:
            full_text += (page.extract_text() or "") + "\n"

    # Ekstraksi Tanggal Periode: DD/MM/YY atau DD/MM/YYYY
    date_match = re.search(
        r"PERIODE\s*:\s*(\d{1,2})[/.-](\d{1,2})[/.-](\d{2,4})", 
        full_text, 
        re.IGNORECASE
    )
    if date_match:
        d, mo, y = date_match.groups()
        year = f"20{y}" if len(y) == 2 else y
        audit_date = f"{year}-{mo.zfill(2)}-{d.zfill(2)}"

    # Ekstraksi baris ringkasan TOTAL
    upper_text = full_text.upper()
    total_pos = upper_text.rfind("TOTAL")
    if total_pos != -1:
        footer_chunk = full_text[total_pos:]
        clean_chunk = re.sub(r"\d{2}/\d{2}/\d{4}|\d{2}:\d{2}", " ", footer_chunk)
        footer_nums = re.findall(r"\b\d{1,3}(?:\.\d{3})+(?:,\d+)?\b|\b0\b", clean_chunk)
        if len(footer_nums) >= 2:
            total_tunai_pdf = parse_rupiah_number(footer_nums[1])
        if len(footer_nums) >= 3:
            total_kredit_pdf = parse_rupiah_number(footer_nums[2])
        if len(footer_nums) >= 4:
            total_debit_pdf = parse_rupiah_number(footer_nums[3])

    # Ekstraksi Rincian Penjualan Pelanggan
    for line in full_text.split("\n"):
        line_clean = line.strip()
        if not line_clean:
            continue
        line_u = line_clean.upper()
        if any(h in line_u for h in ["LAPORAN", "TOKO RINA", "MUTIARA", "PERIODE", "PELANGGAN", "TOTAL TRANSAKSI", "TOTAL:"]):
            continue

        nums = re.findall(r"\b\d{1,3}(?:\.\d{3})+(?:,\d+)?\b|\b0\b", line_clean)
        if len(nums) >= 2:
            first_num_idx = line_clean.find(nums[0])
            cust_name = line_clean[:first_num_idx].replace("|", "").strip()

            if not cust_name or "TOTAL" in cust_name.upper():
                continue

            c_tunai = parse_rupiah_number(nums[1]) if len(nums) > 1 else 0.0
            c_kredit = parse_rupiah_number(nums[2]) if len(nums) > 2 else 0.0
            c_debit = parse_rupiah_number(nums[3]) if len(nums) > 3 else 0.0

            records.append({
                "customer_code": cust_name,
                "tunai": c_tunai,
                "cash": c_tunai,
                "kredit": c_kredit,
                "credit": c_kredit,
                "k_debit": c_debit,
                "debit": c_debit
            })

    if total_tunai_pdf == 0.0 and records:
        total_tunai_pdf = sum(r["tunai"] for r in records)
    if total_kredit_pdf == 0.0 and records:
        total_kredit_pdf = sum(r["kredit"] for r in records)
    if total_debit_pdf == 0.0 and records:
        total_debit_pdf = sum(r["k_debit"] for r in records)

    return {
        "audit_date": audit_date,
        "records": records,
        "total_cash": total_tunai_pdf,
        "total_credit": total_kredit_pdf,
        "total_debit": total_debit_pdf
    }


def parse_bank_mutation_csv(file_source: Union[io.BytesIO, bytes, str]) -> List[Dict[str, Any]]:
    """
    Mengekstrak baris transaksi riil mutasi CSV BCA.
    Mengabaikan header rekening, footer ringkasan, dan saldo berjalan.
    """
    if isinstance(file_source, bytes):
        raw_text = file_source.decode("utf-8-sig", errors="ignore")
    elif isinstance(file_source, io.BytesIO):
        raw_text = file_source.getvalue().decode("utf-8-sig", errors="ignore")
    else:
        raw_text = str(file_source)

    reader = csv.reader(io.StringIO(raw_text))
    mutations: List[Dict[str, Any]] = []

    # Kata kunci pemfilteran metadata BCA
    EXCLUDE_KEYWORDS = [
        "NO. REKENING", "NO REKENING", "NOMOR REKENING", "ACCOUNT",
        "SALDO AWAL", "SALDO AKHIR", "MUTASI KREDIT", "MUTASI DEBET",
        "TOTAL KREDIT", "TOTAL DEBET", "KREDIT =", "DEBET =",
        "RINGKASAN", "PERIODE", "MATA UANG"
    ]

    for row in reader:
        if not row or len(row) < 3:
            continue
        
        # 2. Lewati hanya baris header judul tabel kolom pertama
        if row[0].strip().upper() in ["TANGGAL", "DATE"]:
            continue

        line_str = " ".join(row).upper()
        if any(keyword in line_str for keyword in EXCLUDE_KEYWORDS):
            continue

        trx_date = row[0].strip().replace("'", "")
        # 3. Izinkan tanggal DD/MM/YYYY serta transaksi hari berjalan ('PEND)
        if not (re.search(r"\d{1,2}/\d{1,2}", trx_date) or "PEND" in trx_date.upper()):
            continue
        
        desc = row[1].strip() if len(row) > 1 else ""
        amount = 0.0
        m_type = "CR"

        cr_db_col_idx = -1
        for idx, col in enumerate(row):
            c_up = col.strip().upper()
            if c_up in ["CR", "DB"]:
                m_type = c_up
                cr_db_col_idx = idx
                break

        # Pada CSV BCA, nominal transaksi selalu berada tepat di kolom sebelum tanda CR/DB
        if cr_db_col_idx > 1:
            amount = parse_rupiah_number(row[cr_db_col_idx - 1])
        elif len(row) >= 4 and parse_rupiah_number(row[3]) > 0:
            amount = parse_rupiah_number(row[3])
        else:
            for col in row[2:]:
                val = parse_rupiah_number(col)
                if val > 0:
                    amount = val
                    break

        if amount > 0:
            mutations.append({
                "date": trx_date,
                "description": desc,
                "type": m_type,
                "amount": amount
            })

    return mutations