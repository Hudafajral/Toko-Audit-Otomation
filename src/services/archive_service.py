"""
src/services/archive_service.py
Layanan penyimpanan berkas kas fisik harian ke folder data/raw/{store_id}/{YYYY-MM-DD}/.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

def save_daily_cash_files(
    audit_date: str,
    vault_in: float,
    drawer_items: List[Dict[str, Any]],
    vault_items: List[Dict[str, Any]],
    input_by: str,
    base_dir: str = "data/raw",
    store_id: str = "toko3"
) -> Dict[str, str]:
    # Mengarahkan penyimpanan ke direktori cabang toko yang sesuai
    target_dir = Path(base_dir) / store_id / audit_date
    target_dir.mkdir(parents=True, exist_ok=True)

    # 1. uang_masuk_brankas.json (nominal total setoran bersih)
    f_vault_in = {
        "date": audit_date,
        "nominal": vault_in,
        "input_by": input_by,
        "store_id": store_id
    }
    path_vault_in = target_dir / "uang_masuk_brankas.json"
    path_vault_in.write_text(json.dumps(f_vault_in, indent=2, ensure_ascii=False), encoding="utf-8")

    # 2. uang_keluar_laci.json (itemized belanja operasional kasir)
    total_laci = sum(float(i.get("qty", 1.0)) * float(i.get("unit_price", 0.0)) for i in drawer_items)
    f_drawer_out = {
        "date": audit_date,
        "total_nominal": total_laci,
        "input_by": input_by,
        "store_id": store_id,
        "items": drawer_items
    }
    path_drawer_out = target_dir / "uang_keluar_laci.json"
    path_drawer_out.write_text(json.dumps(f_drawer_out, indent=2, ensure_ascii=False), encoding="utf-8")

    # 3. uang_keluar_brankas.json (itemized pembelian barang toko)
    total_brankas_out = sum(float(i.get("qty", 1.0)) * float(i.get("unit_price", 0.0)) for i in vault_items)
    f_vault_out = {
        "date": audit_date,
        "total_nominal": total_brankas_out,
        "input_by": input_by,
        "store_id": store_id,
        "items": vault_items
    }
    path_vault_out = target_dir / "uang_keluar_brankas.json"
    path_vault_out.write_text(json.dumps(f_vault_out, indent=2, ensure_ascii=False), encoding="utf-8")

    return {
        "uang_masuk_brankas": str(path_vault_in),
        "uang_keluar_laci": str(path_drawer_out),
        "uang_keluar_brankas": str(path_vault_out)
    }