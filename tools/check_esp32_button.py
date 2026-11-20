#!/usr/bin/env python3
"""Uji integrasi tombol ESP32 tanpa perangkat keras.

Skrip ini menembakkan event yang PERSIS sama dengan yang dikirim ESP32, jadi
Anda bisa memastikan jalur backend -> ESP32-CAM -> deteksi -> speaker sudah benar
sebelum menyolder tombol apa pun. Tidak perlu browser.

Contoh:
    python tools/check_esp32_button.py --status
    python tools/check_esp32_button.py --button 1
    python tools/check_esp32_button.py --all --delay 8
    python tools/check_esp32_button.py --host 192.168.1.100 --button 3
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

BUTTON_LABEL = {1: "Deteksi Objek", 2: "Deteksi Ekspresi", 3: "Deteksi Rambu Lalu Lintas"}


def call(url, payload=None, token=""):
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["X-ESP32-Token"] = token
    request = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="ignore")
        try:
            return exc.code, json.loads(body)
        except ValueError:
            return exc.code, {"message": body}
    except urllib.error.URLError as exc:
        raise SystemExit(f"Tidak bisa menghubungi server: {exc.reason}. "
                         f"Pastikan main.py sudah berjalan.") from None


def show_status(base, token):
    _, data = call(f"{base}/api/esp32/status", token=token)
    print(f"Layanan deteksi terpasang: {data.get('trigger_ready')}")
    print(f"Monitor web (opsional)   : {data.get('monitors', 0)}")
    print(f"Cooldown server          : {data.get('cooldown_ms')} ms")
    serial_state = data.get("serial", {})
    if serial_state.get("enabled"):
        print(f"Serial                   : port={serial_state.get('active_port')} "
              f"terhubung={serial_state.get('connected')} error={serial_state.get('last_error') or '-'}")
    else:
        print("Serial                   : nonaktif (ESP32_SERIAL_ENABLED=false)")
    print("Pemetaan tombol:")
    for item in data.get("mapping", []):
        print(f"  {item['button']} -> {item['event']:<22} -> {item['label']}")
    log = data.get("log", [])
    if log:
        print("Log terakhir:")
        for entry in log[-8:]:
            stamp = time.strftime("%H:%M:%S", time.localtime(entry["at"]))
            print(f"  {stamp} [{entry['level']}] {entry['message']}")
    if not data.get("trigger_ready"):
        print("\nPeringatan: layanan deteksi belum terpasang di server.")


def press(base, button, token, device_id):
    status, data = call(f"{base}/api/esp32/event",
                        {"button": button, "device_id": device_id, "source": "test-tool"},
                        token=token)
    label = BUTTON_LABEL.get(button, "?")
    if data.get("status") == "started":
        print(f"OK   tombol {button} ({label}) dimulai; hasil diucapkan di speaker ESP32 "
              f"(request {data.get('request_id')})")
    elif data.get("status") == "busy":
        print(f"SIBUK tombol {button} ({label}): perintah sebelumnya masih berjalan.")
    elif data.get("status") == "ignored":
        print(f"SKIP tombol {button} ({label}): tertahan cooldown server. Beri jeda lalu ulangi.")
    else:
        print(f"HTTP {status}: {data.get('message') or data}")
    return data.get("status") == "started"


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1", help="IP server (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--token", default="", help="Isi bila ESP32_SHARED_TOKEN dipakai")
    parser.add_argument("--device-id", default="esp32-A1")
    parser.add_argument("--button", type=int, choices=[1, 2, 3], help="Tekan satu tombol")
    parser.add_argument("--all", action="store_true", help="Tekan tombol 1, 2, 3 berurutan")
    parser.add_argument("--delay", type=float, default=10.0,
                        help="Jeda antar tombol pada --all (detik). Beri waktu deteksi selesai.")
    parser.add_argument("--status", action="store_true", help="Tampilkan status saja")
    args = parser.parse_args()

    base = f"http://{args.host}:{args.port}"
    if args.status or (args.button is None and not args.all):
        show_status(base, args.token)
        return 0

    ok = True
    if args.all:
        for index, button in enumerate([1, 2, 3]):
            ok = press(base, button, args.token, args.device_id) and ok
            if index < 2:
                print(f"     menunggu {args.delay:g} detik agar deteksi selesai...")
                time.sleep(args.delay)
    else:
        ok = press(base, args.button, args.token, args.device_id)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
