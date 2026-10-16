"""
src/services/archive_service.py
Layanan penyimpanan berkas kas fisik harian ke Supabase Storage & local fallback.
"""
from __future__ import annotations

import os
import json
from pathlib import Path
from typing import Any, Dict, List
from supabase import create_client, Client

SUPABASE_URL = os.getenv("https://xkbnszdqajjbxyjtqkby.supabase.co")
SUPABASE_KEY = os.getenv("sb_publishable_v3WlIqLetyOcbdq1luJjtw_wAWjL1tI")
BUCKET_NAME = "audit-files"

def get_supabase_client() -> Client | None:
    if SUPABASE_URL and SUPABASE_KEY:
        return create_client(SUPABASE_URL, SUPABASE_KEY)
    return None

def upload_to_supabase(path_in_bucket: str, json_content: dict) -> str | None:
    client = get_supabase_client()
    if not client:
        return None
    try:
        data_bytes = json.dumps(json_content, indent=2, ensure_ascii=False).encode("utf-8")
        client.storage.from_(BUCKET_NAME).upload(
            path=path_in_bucket,
            file=data_bytes,
            file_options={"content-type": "application/json", "upsert": "true"}
        )
        return client.storage.from_(BUCKET_NAME).get_public_url(path_in_bucket)
    except Exception as e:
        print(f"[Supabase Storage Error] Gagal upload {path_in_bucket}: {e}")
        return None

def save_daily_cash_files(
    audit_date: str,
    vault_in: float,
    drawer_items: List[Dict[str, Any]],
    vault_items: List[Dict[str, Any]],
    input_by: str,
    base_dir: str = "data/raw",
    tenant_id: str = "toko_1"
) -> Dict[str, str]:
    target_dir = Path(base_dir) / audit_date
    target_dir.mkdir(parents=True, exist_ok=True)

    # 1. Uang Masuk Brankas
    f_vault_in = {
        "tenant_id": tenant_id,
        "date": audit_date,
        "nominal": vault_in,
        "input_by": input_by
    }
    path_vault_in = target_dir / "uang_masuk_brankas.json"
    path_vault_in.write_text(json.dumps(f_vault_in, indent=2, ensure_ascii=False), encoding="utf-8")
    sb_url_vault_in = upload_to_supabase(f"{tenant_id}/{audit_date}/uang_masuk_brankas.json", f_vault_in)

    # 2. Uang Keluar Laci
    total_laci = sum(float(i.get("qty", 1.0)) * float(i.get("unit_price", 0.0)) for i in drawer_items)
    f_drawer_out = {
        "tenant_id": tenant_id,
        "date": audit_date,
        "total_nominal": total_laci,
        "input_by": input_by,
        "items": drawer_items
    }
    path_drawer_out = target_dir / "uang_keluar_laci.json"
    path_drawer_out.write_text(json.dumps(f_drawer_out, indent=2, ensure_ascii=False), encoding="utf-8")
    sb_url_drawer_out = upload_to_supabase(f"{tenant_id}/{audit_date}/uang_keluar_laci.json", f_drawer_out)

    # 3. Uang Keluar Brankas
    total_brankas_out = sum(float(i.get("qty", 1.0)) * float(i.get("unit_price", 0.0)) for i in vault_items)
    f_vault_out = {
        "tenant_id": tenant_id,
        "date": audit_date,
        "total_nominal": total_brankas_out,
        "input_by": input_by,
        "items": vault_items
    }
    path_vault_out = target_dir / "uang_keluar_brankas.json"
    path_vault_out.write_text(json.dumps(f_vault_out, indent=2, ensure_ascii=False), encoding="utf-8")
    sb_url_vault_out = upload_to_supabase(f"{tenant_id}/{audit_date}/uang_keluar_brankas.json", f_vault_out)

    return {
        "uang_masuk_brankas": sb_url_vault_in or str(path_vault_in),
        "uang_keluar_laci": sb_url_drawer_out or str(path_drawer_out),
        "uang_keluar_brankas": sb_url_vault_out or str(path_vault_out)
    }