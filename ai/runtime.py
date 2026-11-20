"""Lazy, failure-isolated services shared by legacy and quick-command routes."""
import importlib
import threading
import time
import os

_torch_config_lock = threading.Lock()
_torch_configured = False


class LazyService:
    def __init__(self, module, name, retry_seconds=30):
        self.module, self.name = module, name
        self.instance = None
        self.error = None
        self.retry_at = 0.0
        self.retry_seconds = retry_seconds
        self.lock = threading.RLock()

    def get(self):
        with self.lock:
            if self.instance is not None:
                return self.instance
            if time.monotonic() < self.retry_at:
                raise RuntimeError(self.error)
            try:
                factory = getattr(importlib.import_module(self.module), self.name)
                global _torch_configured
                if self.module.startswith("ai."):
                    with _torch_config_lock:
                        if not _torch_configured:
                            import torch
                            torch.set_num_threads(max(1, int(os.getenv("AI_CPU_THREADS", "2"))))
                            _torch_configured = True
                self.instance = factory()
                self.error = None
                return self.instance
            except Exception as exc:
                self.error = f"{self.name}: {type(exc).__name__}: {exc}"
                self.retry_at = time.monotonic() + self.retry_seconds
                raise RuntimeError(self.error) from exc

    def __getattr__(self, name):
        return getattr(self.get(), name)

    def status(self):
        return {"state": "ready" if self.instance is not None else ("error" if self.error else "not_loaded"),
                "error": self.error}


def vision_services():
    return {
        "object": LazyService("ai.object_detection", "ObjectDetector"),
        "expression": LazyService("ai.expression_detection", "EmotionDetector"),
        "sign": LazyService("ai.sign_detection", "SignDetector"),
        "scene": LazyService("ai.scene_segmentation", "SceneDetector"),
    }


def factory(module, name):
    """Defer imports too, so missing optional dependencies do not crash Flask."""
    return lambda: getattr(importlib.import_module(module), name)()
