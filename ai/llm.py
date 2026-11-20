"""AI provider abstraction.

Groq is the primary LLM provider. Camera answers are grounded exclusively in
local object/expression/traffic-sign detector results so the model cannot invent visual observations.
"""
from __future__ import annotations

import re
from typing import Any, Iterable

from groq import Groq

from config.settings import settings
from ai.output import concise_answer, spoken_text
from ai.spatial import LABELS_ID


class GroqAI:
    SIGN_HINTS = (
        "rambu", "tanda lalu lintas", "lampu merah", "lampu kuning", "lampu hijau",
        "lampu lalu lintas", "lampu penyebrangan", "zebra cross", "penyebrangan",
        "dilarang parkir", "traffic light", "polisi tidur", "putar balik",
    )
    VISION_HINTS = (
        "depan", "hadapan", "lihat", "terlihat", "kamera", "objek", "benda",
        "apa ini", "ini apa", "apa itu", "itu apa", "benda ini", "barang ini",
        "yang di depan", "di depan saya", "sekitar", "pemandangan", "foto", "wajah", "orang",
    )
    FOLLOWUP_HINTS = (
        "yang tadi", "objek tadi", "rambu tadi", "yang tersebut", "tersebut",
        "yang kamu lihat", "yang terlihat tadi", "jelaskan lagi", "warnanya tadi",
    )

    def __init__(self) -> None:
        self.api_key = settings.groq_api_key
        self.model = settings.groq_model
        self.available = False
        self.last_error: str | None = None
        self.client: Groq | None = None
        self.system_instruction = (
            "Anda adalah asisten AI Spider Sense untuk membantu pengguna tunanetra. "
            "Jawab dalam bahasa Indonesia yang ringkas, jelas, praktis, dan mudah didengar. "
            "Jangan mengulang pertanyaan pengguna di awal jawaban. "
            "Utamakan 1 sampai 3 kalimat pendek, maksimal 60 kata. "
            "Tanpa pembuka, penutup, pengulangan, markdown, confidence, probabilitas, atau skor model. "
            "Untuk objek, langsung sebut objek dan posisi yang tersedia. Untuk ekspresi cukup satu kalimat. "
            "Untuk rambu/sinyal lalu lintas, langsung sebutkan rambu yang terdeteksi (lampu merah/kuning/hijau, "
            "lampu penyebrangan, rambu dilarang parkir, atau zebra cross) dan apa artinya bagi keselamatan pengguna. "
            "Jika konteks kamera diberikan, gunakan hanya fakta dari object detection/deteksi rambu yang diberikan aplikasi. "
            "Untuk rambu, jangan menyebut rambu di luar yang benar-benar terdeteksi. "
            "Jika deteksi rambu tidak andal, katakan rambu/sinyal belum terdeteksi jelas. "
            "Jika konteks visual berasal dari deteksi sebelumnya, jelaskan bahwa itu adalah hasil terakhir yang tersimpan."
        )

        if not self.api_key:
            self.last_error = "GROQ_API_KEY belum di-set"
            return

        try:
            self.client = Groq(
                api_key=self.api_key,
                timeout=settings.groq_timeout_seconds,
                max_retries=1,
            )
            self.available = True
        except Exception as exc:  # initialization must never take server down
            self.last_error = str(exc)

    @classmethod
    def is_sign_question(cls, prompt: str | None) -> bool:
        text = (prompt or "").lower()
        return any(hint in text for hint in cls.SIGN_HINTS)

    @classmethod
    def is_vision_question(cls, prompt: str | None) -> bool:
        text = (prompt or "").lower()
        return cls.is_sign_question(text) or any(hint in text for hint in cls.VISION_HINTS)

    @classmethod
    def is_visual_followup(cls, prompt: str | None) -> bool:
        text = " ".join((prompt or "").lower().split())
        return any(hint in text for hint in cls.FOLLOWUP_HINTS)

    def _generate(self, prompt: str) -> str | None:
        if not self.available or not self.client:
            return None
        try:
            is_gpt_oss = self.model.startswith("openai/gpt-oss")
            # Groq's GPT-OSS guidance recommends putting instructions in the
            # user message. Other models keep the conventional system role.
            messages = (
                [{"role": "user", "content": f"{self.system_instruction}\n\n{prompt}"}]
                if is_gpt_oss
                else [
                    {"role": "system", "content": self.system_instruction},
                    {"role": "user", "content": prompt},
                ]
            )
            kwargs: dict[str, Any] = {
                "model": self.model,
                "messages": messages,
                "temperature": 0.2,
                "max_completion_tokens": settings.groq_max_output_tokens,
            }
            if is_gpt_oss:
                kwargs["reasoning_effort"] = settings.groq_reasoning_effort
                # Never surface internal reasoning in the assistant response.
                kwargs["include_reasoning"] = False
            response = self.client.chat.completions.create(**kwargs)
            text = (response.choices[0].message.content or "").strip()
            return concise_answer(text) or None
        except Exception as exc:
            self.last_error = str(exc)
            return None

    def ask(self, prompt: str) -> str:
        answer = self._generate((prompt or "").strip() or "Halo")
        if answer:
            return answer
        return "Maaf, layanan AI sedang tidak tersedia. Silakan coba lagi sebentar lagi."

    @staticmethod
    def _object_names(detected_objects: Iterable[Any] | None) -> list[str]:
        names: list[str] = []
        for item in detected_objects or []:
            if isinstance(item, dict):
                label = str(item.get("label_id") or item.get("label", "")).strip()
            else:
                label = re.sub(r"\s*\([0-9.]+\)\s*$", "", str(item)).strip()
            label = LABELS_ID.get(label, label)
            if label and label not in names:
                names.append(label)
        return names

    def ask_with_context(
        self,
        question: str,
        detected_objects: Iterable[Any] | None = None,
        sign_text: str = "",
        sign_confidence: float = 0.0,
        sign_reliable: bool = False,
        context_is_cached: bool = False,
    ) -> str:
        question = (question or "").strip()
        sign_text = (sign_text or "").strip()
        object_names = self._object_names(detected_objects)

        if self.is_sign_question(question) and (not sign_text or not sign_reliable):
            return (
                "Rambu atau sinyal lalu lintas di depan belum terdeteksi dengan jelas. "
                "Coba arahkan kamera ke rambu/lampu lalu lintas, lebih stabil, dan pastikan pencahayaan cukup."
            )

        safe_sign_text = sign_text if (sign_text and sign_reliable) else "[deteksi rambu tidak andal; jangan digunakan]"
        source_note = "hasil visual TERAKHIR yang tersimpan" if context_is_cached else "snapshot kamera saat ini"
        context = (
            f"SUMBER DATA VISUAL: {source_note}\n"
            f"Detected objects: {', '.join(object_names) if object_names else '[tidak ada objek terdeteksi]'}\n"
            f"Detected traffic signs/signals: {safe_sign_text}\n"
            f"Deteksi rambu reliable: {'YA' if sign_reliable else 'TIDAK'}\n\n"
            f"Pertanyaan pengguna: {question}\n\n"
            "ATURAN: Untuk pertanyaan rambu/sinyal lalu lintas, gunakan hanya Detected traffic signs/signals. "
            "Untuk objek, jangan menyebut objek di luar Detected objects. "
            "Jangan mengulang pertanyaan pengguna."
        )

        answer = self._generate(context)
        if answer:
            return answer
        if self.is_sign_question(question) and sign_text and sign_reliable:
            return ("Hasil terakhir: " if context_is_cached else "") + spoken_text(sign_text)
        if object_names:
            prefix = "Hasil terakhir: " if context_is_cached else "Di depan ada "
            return prefix + ", ".join(object_names[:6]) + "."
        return "Saya belum memiliki informasi visual yang cukup jelas untuk menjawab itu."


# Compatibility alias for older imports. New code should import GroqAI.
AIProvider = GroqAI
