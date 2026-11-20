"""Diagnose MongoDB Atlas connectivity without exposing credentials.

Run from the project root:
    python tools/check_mongodb.py
"""
from __future__ import annotations

import os
import socket
import ssl
import sys
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")


def redact_uri(uri: str) -> str:
    if not uri:
        return "<empty>"
    try:
        scheme, rest = uri.split("://", 1)
        if "@" in rest:
            _, hostpart = rest.rsplit("@", 1)
            return f"{scheme}://***:***@{hostpart}"
        return f"{scheme}://{rest}"
    except Exception:
        return "<configured>"


def main() -> int:
    uri = (os.getenv("MONGODB_URI") or os.getenv("MONGO_URI") or "").strip()
    print("=== MongoDB Atlas diagnostic ===")
    print("Python :", sys.version.split()[0])
    print("OpenSSL:", ssl.OPENSSL_VERSION)
    print("URI    :", redact_uri(uri))

    if not uri:
        print("ERROR: MONGODB_URI belum diisi di .env")
        return 2

    try:
        import certifi
        import pymongo
        from pymongo.mongo_client import MongoClient
        from pymongo.server_api import ServerApi
    except ImportError as exc:
        print("ERROR: dependency belum lengkap:", exc)
        print("Jalankan: pip install -r requirements.txt")
        return 3

    print("PyMongo:", pymongo.version)
    print("CA file:", certifi.where())

    options = dict(
        server_api=ServerApi("1"),
        serverSelectionTimeoutMS=8000,
        connectTimeoutMS=8000,
        socketTimeoutMS=10000,
        retryWrites=True,
        appname="spider-sense-mongodb-diagnostic",
        tls=True,
        tlsCAFile=certifi.where(),
        tlsDisableOCSPEndpointCheck=True,
    )

    print("\nConnecting and sending ping...")
    try:
        client = MongoClient(uri, **options)
        result = client.admin.command("ping")
        print("SUCCESS: MongoDB Atlas terhubung.")
        print("Ping result:", result)
        client.close()
        return 0
    except Exception as exc:
        msg = str(exc)
        print("FAILED:", type(exc).__name__)
        print(msg)
        print("\nChecklist:")
        print("1. Atlas > Security > Network Access: tambahkan PUBLIC IP mesin/server Python.")
        print("2. Pastikan firewall/VPN/antivirus tidak memblokir outbound TCP 27017 atau TLS/SNI.")
        print("3. Atlas > Database Access: pastikan user DB dan password benar.")
        print("4. Jika password berisi @ : / ? # [ ], URL-encode password pada URI.")
        print("5. Coba jaringan lain/hotspot untuk membedakan masalah ISP/firewall dari kode.")
        print("6. Jangan gunakan tlsInsecure/tlsAllowInvalidCertificates sebagai solusi produksi.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
