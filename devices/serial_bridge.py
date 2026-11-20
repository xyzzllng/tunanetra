"""Bridge serial (USB) ESP32 <-> Python.

Dipakai bila ESP32 dicolok lewat kabel USB dan tidak memakai Wi-Fi. Protokolnya
satu baris JSON per pesan, sama persis dengan firmware `firmware/esp32_main`:

    ESP32 -> Python : {"event":"button_press","button":1}
                      {"event":"object_detection","device_id":"esp32-A1"}
    Python -> ESP32 : {"command":"beep","freq":900,"duration":90}

Perbaikan dibanding versi sebelumnya:
  * reconnect otomatis (thread supervisor), tidak mati permanen saat kabel dicabut
  * deteksi port otomatis (`ESP32_SERIAL_PORT=auto`) untuk CP210x/CH340/FTDI
  * `send_command()` benar-benar ada (kode lama memanggilnya padahal belum dibuat)
  * penulisan serial dilindungi lock, aman dipanggil dari thread request Flask
  * pyserial opsional: bila belum terpasang, server tetap jalan normal

Bridge ini TIDAK menjalankan deteksi apa pun. Ia hanya menyerahkan event ke
`ESP32EventHub.dispatch()`, sama seperti jalur WebSocket dan HTTP.
"""
from __future__ import annotations

import json
import threading
import time

try:                                   # pyserial opsional
    import serial
    from serial.tools import list_ports
    SERIAL_AVAILABLE = True
    SERIAL_IMPORT_ERROR = ""
except Exception as exc:               # pragma: no cover - tergantung environment
    serial = None
    list_ports = None
    SERIAL_AVAILABLE = False
    SERIAL_IMPORT_ERROR = str(exc)


# Substring deskripsi/hwid umum untuk board ESP32 DevKit.
_USB_HINTS = ("cp210", "ch340", "ch910", "ftdi", "ft232", "silicon labs",
              "usb serial", "usb-serial", "wch", "esp32")


def autodetect_port():
    """Tebak port ESP32 pertama yang masuk akal, atau None."""
    if not SERIAL_AVAILABLE:
        return None
    try:
        candidates = list(list_ports.comports())
    except Exception:
        return None
    for port in candidates:
        haystack = " ".join(filter(None, [port.description, port.manufacturer, port.hwid])).lower()
        if any(hint in haystack for hint in _USB_HINTS):
            return port.device
    return candidates[0].device if candidates else None


def available_ports():
    if not SERIAL_AVAILABLE:
        return []
    try:
        return [{"device": p.device, "description": p.description} for p in list_ports.comports()]
    except Exception:
        return []


class ESP32SerialBridge:
    """Pembaca serial dengan reconnect otomatis."""

    def __init__(self, port="auto", baudrate=115200, on_event=None, device_id="esp32-serial",
                 reconnect_seconds=3.0, logger=None):
        self.configured_port = str(port or "auto").strip() or "auto"
        self.baudrate = int(baudrate)
        self.on_event = on_event
        self.device_id = device_id
        self.reconnect_seconds = max(0.5, float(reconnect_seconds))
        self.logger = logger

        self._serial = None
        self._thread = None
        self._write_lock = threading.Lock()
        self._running = False
        self.active_port = None
        self.connected = False
        self.last_error = ""
        self.last_event_at = 0.0
        self.events_received = 0

    # ------------------------------------------------------------------ utils
    def _log(self, message, level="info"):
        text = f"[ESP32 serial] {message}"
        if self.logger:
            getattr(self.logger, level, self.logger.info)(text)
        else:
            print(text)

    def _resolve_port(self):
        if self.configured_port.lower() == "auto":
            return autodetect_port()
        return self.configured_port

    # ------------------------------------------------------------- lifecycle
    def start(self):
        """Jalankan thread supervisor. Aman dipanggil dua kali."""
        if not SERIAL_AVAILABLE:
            self.last_error = f"pyserial belum terpasang ({SERIAL_IMPORT_ERROR}). Jalankan: pip install pyserial"
            self._log(self.last_error, "warning")
            return False
        if self._running:
            return True
        self._running = True
        self._thread = threading.Thread(target=self._supervise, name="esp32-serial", daemon=True)
        self._thread.start()
        return True

    # Kompatibilitas dengan kode lama yang memanggil connect().
    connect = start

    def stop(self):
        self._running = False
        self._close_port()

    close = stop

    def _close_port(self):
        port, self._serial = self._serial, None
        self.connected = False
        if port is not None:
            try:
                port.close()
            except Exception:
                pass

    def _supervise(self):
        while self._running:
            target = self._resolve_port()
            if not target:
                self.last_error = "Tidak ada port serial yang terdeteksi"
                time.sleep(self.reconnect_seconds)
                continue
            try:
                self._serial = serial.Serial(target, self.baudrate, timeout=1)
                # ESP32 melakukan reset saat port dibuka; beri waktu boot.
                time.sleep(2.0)
                try:
                    self._serial.reset_input_buffer()
                except Exception:
                    pass
                self.active_port = target
                self.connected = True
                self.last_error = ""
                self._log(f"terhubung di {target} @ {self.baudrate}")
                self._read_loop()
            except Exception as exc:
                self.last_error = str(exc)
                self._log(f"gagal/terputus di {target}: {exc}", "warning")
            finally:
                self._close_port()
            if self._running:
                time.sleep(self.reconnect_seconds)

    def _read_loop(self):
        buffer = self._serial
        while self._running and buffer is not None and buffer.is_open:
            try:
                raw = buffer.readline()
            except Exception as exc:
                self.last_error = str(exc)
                self._log(f"koneksi terputus: {exc}", "warning")
                return
            if not raw:
                continue                      # timeout biasa, bukan error
            line = raw.decode("utf-8", errors="ignore").strip()
            if not line or not line.startswith("{"):
                if line:
                    self._log(f"pesan non-JSON diabaikan: {line[:120]}")
                continue
            try:
                message = json.loads(line)
            except ValueError:
                continue
            self._handle(message)

    def _handle(self, message):
        if not isinstance(message, dict):
            return
        self.events_received += 1
        self.last_event_at = time.time()
        if self.on_event is None:
            return
        try:
            self.on_event(message, source="serial", device_id=message.get("device_id") or self.device_id)
        except Exception as exc:              # pragma: no cover
            self._log(f"handler event gagal: {exc}", "warning")

    # ------------------------------------------------------------------ write
    def send_command(self, payload):
        """Kirim satu perintah JSON ke ESP32. Mengembalikan True bila terkirim."""
        port = self._serial
        if port is None or not getattr(port, "is_open", False):
            return False
        data = (json.dumps(payload) + "\n").encode("utf-8")
        with self._write_lock:
            try:
                port.write(data)
                return True
            except Exception as exc:
                self.last_error = str(exc)
                return False

    def beep(self, freq=800, duration=150):
        return self.send_command({"command": "beep", "freq": int(freq), "duration": int(duration)})

    send_beep = beep

    # ----------------------------------------------------------------- status
    def status(self):
        return {
            "enabled": True,
            "available": SERIAL_AVAILABLE,
            "connected": self.connected,
            "configured_port": self.configured_port,
            "active_port": self.active_port,
            "baudrate": self.baudrate,
            "events_received": self.events_received,
            "last_event_at": self.last_event_at or None,
            "last_error": self.last_error,
        }


# Nama lama dipertahankan supaya import yang sudah ada tidak rusak.
ESP32Bridge = ESP32SerialBridge


def start_serial_bridge(hub, settings, logger=None):
    """Hubungkan bridge serial ke hub event. Kembalikan bridge atau None.

    Event serial diterjemahkan ke `hub.dispatch()` - jalur yang sama dengan
    WebSocket dan HTTP, jadi tidak ada logika kembar.
    """
    if not settings.esp32_serial_enabled:
        return None

    def on_event(message, source="serial", device_id=None):
        # Firmware mengirim {"event":"button_press","button":N} atau langsung
        # {"event":"object_detection"}. Keduanya diterima.
        event = message.get("event")
        button = message.get("button")
        if event in (None, "", "button_press", "button"):
            event = None
        try:
            hub.dispatch(event=event, button=button, device_id=device_id, source=source)
        except ValueError as exc:
            if logger:
                logger.warning(f"[ESP32 serial] event diabaikan: {exc}")

    bridge = ESP32SerialBridge(
        port=settings.esp32_serial_port,
        baudrate=settings.esp32_serial_baudrate,
        on_event=on_event,
        device_id=settings.esp32_serial_device_id,
        logger=logger,
    )
    hub.serial_bridge = bridge
    bridge.start()
    return bridge
