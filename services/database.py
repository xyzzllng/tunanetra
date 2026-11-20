"""MongoDB and Ubidots integrations with resilient, non-fatal networking.

MongoDB notes:
- Atlas SRV connections use TLS automatically; we explicitly validate against
  certifi's CA bundle to avoid stale/missing OS CA stores (common on Windows).
- Certificate verification is NEVER disabled.
- Failed connections enter exponential backoff so a temporary Atlas/network
  issue does not add a 5 second delay to every user request.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime
from typing import Optional

import certifi
import requests
from pymongo.errors import AutoReconnect, ConnectionFailure, NetworkTimeout, PyMongoError, ServerSelectionTimeoutError
from pymongo.mongo_client import MongoClient
from pymongo.server_api import ServerApi

from config.settings import settings


class Database:
    def __init__(self):
        self.MONGO_URI = settings.mongodb_uri
        self.UBIDOTS_TOKEN = settings.ubidots_token
        self.UBIDOTS_DEVICE_LABEL = settings.ubidots_device_label

        self._mongo_lock = threading.Lock()
        self.client: Optional[MongoClient] = None
        self.mongodb_available = False
        self.mongodb_last_error = ""
        self._next_mongo_retry_at = 0.0
        self._mongo_retry_delay = max(1.0, settings.mongodb_retry_initial_seconds)
        self._last_mongo_log_at = 0.0

        # One startup attempt only. If it fails, normal requests won't block on
        # MongoDB until the retry cooldown expires.
        if self.MONGO_URI:
            self._connect_mongo(force=True)

    # ------------------------------------------------------------------
    # MongoDB
    # ------------------------------------------------------------------
    def _mongo_options(self) -> dict:
        options = {
            "server_api": ServerApi("1"),
            "serverSelectionTimeoutMS": settings.mongodb_server_selection_timeout_ms,
            "connectTimeoutMS": settings.mongodb_connect_timeout_ms,
            "socketTimeoutMS": settings.mongodb_socket_timeout_ms,
            "retryWrites": True,
            "appname": "spider-sense-groq",
            "maxPoolSize": settings.mongodb_max_pool_size,
            "minPoolSize": 0,
        }

        # mongodb+srv enables TLS implicitly, but setting it explicitly is fine
        # and makes behavior identical if a standard mongodb:// Atlas URI is used.
        if settings.mongodb_tls:
            options["tls"] = True
            options["tlsCAFile"] = settings.mongodb_tls_ca_file or certifi.where()
            options["tlsDisableOCSPEndpointCheck"] = settings.mongodb_tls_disable_ocsp

        return options

    def _log_mongo_failure(self, exc: Exception) -> None:
        raw = str(exc)
        self.mongodb_last_error = f"{type(exc).__name__}: {raw}"
        if "TLSV1_ALERT_INTERNAL_ERROR" in raw or "SSL handshake failed" in raw:
            summary = (
                "TLS handshake ke MongoDB Atlas gagal. Periksa Atlas Network Access/public IP, "
                "firewall/VPN, dan jalankan tools/check_mongodb.py."
            )
        elif "timed out" in raw.lower() or "ServerSelectionTimeout" in type(exc).__name__:
            summary = "MongoDB Atlas timeout. Periksa network access, DNS, firewall, dan koneksi internet."
        else:
            summary = f"{type(exc).__name__}: {raw[:300]}"

        now = time.monotonic()
        # Rate-limit error output; the full topology exception can be several KB.
        if now - self._last_mongo_log_at >= settings.mongodb_error_log_interval_seconds:
            wait_s = max(0, int(self._next_mongo_retry_at - now))
            print(
                f"MongoDB tidak tersedia: {summary} "
                f"Retry background ~{wait_s}s; Groq/kamera/TTS/Ubidots tetap berjalan."
            )
            self._last_mongo_log_at = now

    def _mark_mongo_failed(self, exc: Exception) -> None:
        old_client = self.client
        self.client = None
        self.mongodb_available = False
        if old_client is not None:
            try:
                old_client.close()
            except Exception:
                pass

        now = time.monotonic()
        self._next_mongo_retry_at = now + self._mongo_retry_delay
        self._mongo_retry_delay = min(
            self._mongo_retry_delay * 2.0,
            settings.mongodb_retry_max_seconds,
        )
        self._log_mongo_failure(exc)

    def _connect_mongo(self, force: bool = False):
        if not self.MONGO_URI:
            return None

        now = time.monotonic()
        if not force and now < self._next_mongo_retry_at:
            return None

        try:
            client = MongoClient(self.MONGO_URI, **self._mongo_options())
            client.admin.command("ping")
            self.client = client
            self.mongodb_available = True
            self.mongodb_last_error = ""
            self._mongo_retry_delay = max(1.0, settings.mongodb_retry_initial_seconds)
            self._next_mongo_retry_at = 0.0
            return client
        except Exception as exc:
            self._mark_mongo_failed(exc)
            return None

    def _ensure_client(self, allow_connect: bool = True):
        if self.client is not None:
            return self.client
        if not self.MONGO_URI:
            return None
        if time.monotonic() < self._next_mongo_retry_at or not allow_connect:
            return None

        with self._mongo_lock:
            if self.client is not None:
                return self.client
            if time.monotonic() < self._next_mongo_retry_at:
                return None
            return self._connect_mongo()

    def mongo_status(self) -> dict:
        """Small safe status payload for health endpoints (never exposes URI)."""
        retry_in = max(0.0, self._next_mongo_retry_at - time.monotonic())
        return {
            "configured": bool(self.MONGO_URI),
            "available": bool(self.mongodb_available and self.client is not None),
            "retry_in_seconds": round(retry_in, 1),
            "last_error": self.mongodb_last_error[:500] if self.mongodb_last_error else "",
        }

    def save_interaction(self, data):
        # Never reconnect on the interactive request path. Reconnect attempts
        # are handled by the background task so MongoDB cannot add seconds of
        # latency to an AI/camera response.
        client = self._ensure_client(allow_connect=False)
        if not client:
            return False
        try:
            payload = dict(data or {})
            payload.setdefault("timestamp", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            client["SPIDER-SENSE"]["Interaksi"].insert_one(payload)
            self.mongodb_available = True
            return True
        except (AutoReconnect, ConnectionFailure, NetworkTimeout, ServerSelectionTimeoutError) as exc:
            self._mark_mongo_failed(exc)
            return False
        except PyMongoError as exc:
            # Database/auth/write errors must not crash the AI pipeline, but an
            # auth/write error does not necessarily mean the network is down.
            self.mongodb_last_error = f"{type(exc).__name__}: {exc}"
            print(f"Error saving interaction to MongoDB: {type(exc).__name__}: {exc}")
            return False

    def update_background_data(self):
        days_map = {
            "Monday": "senin", "Tuesday": "selasa", "Wednesday": "rabu",
            "Thursday": "kamis", "Friday": "jumat", "Saturday": "sabtu", "Sunday": "minggu",
        }
        now = datetime.now()
        day = days_map.get(now.strftime("%A"), "unknown")
        timestamp = now.strftime("%Y-%m-%d %H:%M:%S")

        client = self._ensure_client()
        if client:
            try:
                db = client["SPIDER-SENSE"]
                db["PenggunaanAlat"].insert_one({
                    "hari": day, "timestamp": timestamp, "komponen": "Alat Aktif", "jumlah": 1,
                })
                # Kept for dashboard compatibility with the original project.
                db["DataEmosi"].insert_one({
                    "Ekspresi": "netral", "Jumlah": 1, "hari": day, "timestamp": timestamp,
                })
                self.mongodb_available = True
            except (AutoReconnect, ConnectionFailure, NetworkTimeout, ServerSelectionTimeoutError) as exc:
                self._mark_mongo_failed(exc)
            except PyMongoError as exc:
                self.mongodb_last_error = f"{type(exc).__name__}: {exc}"
                print(f"Error updating MongoDB background data: {type(exc).__name__}: {exc}")

        # Ubidots remains independent from MongoDB.
        self._send_ubidots(day)

    # ------------------------------------------------------------------
    # Ubidots
    # ------------------------------------------------------------------
    def _send_ubidots(self, day):
        if not self.UBIDOTS_TOKEN:
            return False
        url = f"https://industrial.api.ubidots.com/api/v1.6/devices/{self.UBIDOTS_DEVICE_LABEL}/"
        headers = {"X-Auth-Token": self.UBIDOTS_TOKEN, "Content-Type": "application/json"}
        payload = {day: {"value": 1}, "jumlah_emosi": {"value": 1}, "netral": {"value": 1}}
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=(3.05, 6))
            response.raise_for_status()
            return True
        except requests.RequestException as exc:
            print(f"Error sending to Ubidots: {exc}")
            return False
