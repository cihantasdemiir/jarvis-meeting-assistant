"""Pocket TTS Turkish modeli için ince adaptör (Faz 5b).

Kullanılan kütüphane API'si (pocket-tts-turkish 0.1.0 kaynak kodundan doğrulandı):
  * `TurkishTTS.from_pretrained(klasör, device=...)` — klasör verilirse indirme YAPILMAZ.
    Hugging Face depo adı verilirse `snapshot_download` ile sessizce indirir; bu yüzden burada asla
    depo adı verilmez, klasör önce yalnızca yerelde çözülür.
  * `tts.stream(metin, voice=...)` — ~80 ms'lik float32 parçalar (24 kHz) üretir; döngüden çıkınca
    üretim durur. Bir örnek iş parçacıkları arasında paylaşılabilir, çağrılar sırayla çalışır.
  * `tts.voices`, `tts.sample_rate`.

Bu modül torch / numpy / pocket_tts_turkish'i yalnızca `load()` içinde içe aktarır; `--no-tts` veya
`TTS_ENGINE=pyttsx3` ile çalışırken bu paketler hiç yüklenmez.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Callable, Iterator, Protocol

log = logging.getLogger(__name__)

REQUIRED_FILES = ("config.yaml", "model.safetensors", "tokenizer.model")
DOWNLOAD_PATTERNS = [*REQUIRED_FILES, "voices/*.wav"]
INSTALL_HINT = "Kurulum: pip install -r requirements-tts.txt (ayrıntı: docs/TURKCE_TTS.md)."
DOWNLOAD_HINT = ("Modeli bir kez indirmek için (yaklaşık 440 MB, internet gerekir, onay sorulur): "
                 "python main.py --tts-download")


class TtsUnavailable(RuntimeError):
    """Ses motoru kullanılamıyor (paket yok, model yok, aygıt yok...). Mesaj kullanıcıya gösterilir."""


class TtsModelMissing(TtsUnavailable):
    """Model dosyaları bu bilgisayarda yok. İnternetten kendiliğinden indirilmez."""


class SpeechBackend(Protocol):
    sample_rate: int
    voices: list[str]
    description: str

    def load(self) -> None: ...

    def stream(self, text: str, voice: str) -> Iterator[Any]: ...


def check_model_folder(folder: Path) -> Path:
    missing = [name for name in REQUIRED_FILES if not (folder / name).is_file()]
    if not (folder / "voices").is_dir() or not any((folder / "voices").glob("*.wav")):
        missing.append("voices/*.wav")
    if missing:
        raise TtsModelMissing(f"Model klasörü eksik: {folder} ({', '.join(missing)} yok). {DOWNLOAD_HINT}")
    return folder


def resolve_model_folder(model_dir: Path | None, repo: str,
                         snapshot_download: Callable[..., str] | None = None) -> Path:
    """Model klasörünü YALNIZCA yerelde bulur; internete çıkmaz.

    TTS_MODEL_DIR verilmişse o klasör kullanılır. Verilmemişse Hugging Face önbelleğinde
    (`local_files_only=True`) aranır; kullanıcı modeli başka bir ortamda `pocket-tts-turkish` komutuyla
    indirdiyse önbellek ortaktır ve burada bulunur.
    """
    if model_dir is not None:
        if not model_dir.is_dir():
            raise TtsModelMissing(f"TTS_MODEL_DIR klasörü bulunamadı: {model_dir}. {DOWNLOAD_HINT}")
        return check_model_folder(model_dir)
    if snapshot_download is None:
        try:
            from huggingface_hub import snapshot_download  # noqa: PLC0415
        except ImportError as exc:
            raise TtsUnavailable(f"huggingface-hub kurulu değil. {INSTALL_HINT}") from exc
    try:
        folder = snapshot_download(repo_id=repo, allow_patterns=DOWNLOAD_PATTERNS, ignore_patterns=["voices/*/*"],
                                   local_files_only=True)
    except Exception as exc:  # LocalEntryNotFoundError, OfflineModeIsEnabled ... sürüme göre değişir
        raise TtsModelMissing(f"Türkçe ses modeli ({repo}) bu bilgisayarda bulunamadı. {DOWNLOAD_HINT}") from exc
    return check_model_folder(Path(folder))


def download_model(repo: str, target: Path | None = None,
                   snapshot_download: Callable[..., str] | None = None) -> Path:
    """Modeli Hugging Face'ten indirir. Yalnızca kullanıcının açık onayıyla (--tts-download) çağrılır."""
    if snapshot_download is None:
        os.environ["HF_HUB_OFFLINE"] = "0"
        from huggingface_hub import snapshot_download  # noqa: PLC0415
    kwargs: dict[str, Any] = {"repo_id": repo, "allow_patterns": DOWNLOAD_PATTERNS, "ignore_patterns": ["voices/*/*"]}
    if target is not None:
        kwargs["local_dir"] = str(target)
    return check_model_folder(Path(snapshot_download(**kwargs)))


class PocketTurkishBackend:
    """Gerçek Pocket TTS Turkish modeli. `load()` ağır işlemdir; bir kez çağrılır."""

    def __init__(self, model_dir: Path | None = None, repo: str = "wite-tech/pocket-tts-turkish",
                 device: str = "cpu", cpu_threads: int = 0):
        self.model_dir = model_dir
        self.repo = repo
        self.device = device
        self.cpu_threads = cpu_threads
        self.sample_rate = 24000
        self.voices: list[str] = []
        self.description = "Pocket TTS Turkish"
        self.folder: Path | None = None
        self._tts = None
        self._np = None

    def load(self) -> None:
        # İnternete hiç çıkılmaz: kütüphanenin iç yollarında bir indirme denemesi olsa bile engellenir.
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
        try:
            import numpy as np  # noqa: PLC0415
            import torch  # noqa: PLC0415
            from pocket_tts_turkish import TurkishTTS  # noqa: PLC0415
        except ImportError as exc:
            raise TtsUnavailable(f"Pocket TTS Turkish kurulu değil ({exc.name or exc}). {INSTALL_HINT}") from exc
        self.folder = resolve_model_folder(self.model_dir, self.repo)
        if self.device == "cuda" and not torch.cuda.is_available():
            raise TtsUnavailable("TTS_DEVICE=cuda seçili ama PyTorch CUDA'yı görmüyor (CPU sürümü kurulu olabilir). "
                                 "TTS_DEVICE=cpu kullanın veya docs/TURKCE_TTS.md 'GPU' bölümüne bakın.")
        if self.device == "cpu" and self.cpu_threads:
            torch.set_num_threads(self.cpu_threads)
        try:
            self._tts = TurkishTTS.from_pretrained(self.folder, device=self.device)
        except Exception as exc:
            raise TtsUnavailable(f"Türkçe ses modeli yüklenemedi: {exc}") from exc
        self._np = np
        self.sample_rate = int(self._tts.sample_rate)
        self.voices = list(self._tts.voices)
        self.description = f"Pocket TTS Turkish ({self.device})"

    def stream(self, text: str, voice: str) -> Iterator[Any]:
        if self._tts is None:
            raise TtsUnavailable("Ses modeli yüklenmedi.")
        return self._tts.stream(text, voice=voice)

    def torch_info(self) -> dict[str, Any]:
        """Performans aracı için: PyTorch sürümü, CUDA durumu, iş parçacığı sayısı, GPU belleği."""
        try:
            import torch  # noqa: PLC0415
        except ImportError:
            return {}
        info: dict[str, Any] = {"torch": torch.__version__, "cuda_available": bool(torch.cuda.is_available()),
                                "threads": torch.get_num_threads(), "device": self.device}
        if self.device == "cuda" and torch.cuda.is_available():
            free, total = torch.cuda.mem_get_info()
            info.update(gpu=torch.cuda.get_device_name(0), gpu_free_mb=free / 2**20, gpu_total_mb=total / 2**20,
                        gpu_peak_allocated_mb=torch.cuda.max_memory_allocated() / 2**20)
        return info


def to_pcm16(chunk: Any, volume: float = 1.0) -> bytes:
    """Model çıktısını (float32 dizi, -1..1) 16 bit PCM'e çevirir. Zaten bayt ise olduğu gibi döner."""
    if isinstance(chunk, (bytes, bytearray, memoryview)):
        return bytes(chunk)
    import numpy as np  # noqa: PLC0415

    data = np.asarray(chunk, dtype=np.float32).reshape(-1)
    if volume != 1.0:
        data = data * float(volume)
    return (np.clip(data, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
