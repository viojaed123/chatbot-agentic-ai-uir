"""Client HTTP untuk server Ollama LOKAL (bukan layanan AI cloud).

Modul ini hanya menyerang endpoint ``http://localhost:11434/api/chat`` dan
menerjemahkan kegagalan jaringan/respons menjadi exception yang dipahami
:mod:`app.orchestrator`. Tidak ada fallback ke penyedia model eksternal.

Konfigurasi dibaca dari environment variable (tanpa nilai rahasia):

* ``OLLAMA_URL``     — endpoint chat (default lokal).
* ``OLLAMA_MODEL``   — nama model (default ``hermes3:8b``).
* ``OLLAMA_TIMEOUT``     — timeout request dalam detik (default 180).
* ``OLLAMA_NUM_PREDICT`` — batas token untuk jawaban akhir (default 384).
* ``OLLAMA_NUM_PREDICT_TOOL`` — batas token saat model memilih tool
  (default 192). Pemanggilan tool hanya memerlukan JSON singkat, jadi batas
  ini lebih kecil daripada jawaban akhir. Keduanya mencegah satu generasi
  panjang menghabiskan seluruh anggaran waktu pada inference CPU; verifikasi
  tetap dilakukan di orkestrator.
"""

import json
import logging
import os
import socket
import urllib.error
import urllib.parse
import urllib.request

from app import telemetri

LOGGER = logging.getLogger(__name__)

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434/api/chat")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "hermes3:8b")
OLLAMA_TIMEOUT = int(os.environ.get("OLLAMA_TIMEOUT", "180"))
OLLAMA_NUM_PREDICT = int(os.environ.get("OLLAMA_NUM_PREDICT", "384"))
OLLAMA_NUM_PREDICT_TOOL = int(
    os.environ.get("OLLAMA_NUM_PREDICT_TOOL", "192")
)
# Model tetap hangat di memori Ollama agar tidak dimuat ulang tiap pertanyaan.
OLLAMA_KEEP_ALIVE = os.environ.get("OLLAMA_KEEP_ALIVE", "10m")
SUHU = 0  # jawaban faktual lebih konsisten

HOST_LOKAL = frozenset({"localhost", "127.0.0.1", "::1"})


class OllamaError(Exception):
    """Induk seluruh kegagalan Ollama lokal."""


class KonfigurasiTidakValid(OllamaError):
    """Endpoint yang dikonfigurasi bukan server lokal."""


class OllamaTidakTersedia(OllamaError):
    """Server Ollama lokal tidak dapat dihubungi."""


class ModelTidakTersedia(OllamaError):
    """Model yang diminta tidak terpasang di Ollama lokal."""


class OllamaTimeout(OllamaError):
    """Permintaan melewati batas waktu."""


class ResponsTidakValid(OllamaError):
    """Respons Ollama bukan JSON/struktur chat yang diharapkan."""


def _validasi_endpoint(url):
    """Terima hanya endpoint lokal agar tidak terjadi request AI eksternal."""
    if not isinstance(url, str) or not url.strip():
        raise KonfigurasiTidakValid("Endpoint Ollama harus berupa URL.")
    try:
        bagian = urllib.parse.urlsplit(url.strip())
    except ValueError as exc:
        raise KonfigurasiTidakValid("URL Ollama tidak valid.") from exc
    if bagian.scheme not in ("http", "https") or bagian.hostname not in HOST_LOKAL:
        raise KonfigurasiTidakValid(
            "Hanya endpoint Ollama lokal (localhost/127.0.0.1/::1) yang diizinkan."
        )
    return url.strip()


class OllamaClient:
    """Klien minimal untuk POST ``/api/chat`` dengan tool calling."""

    def __init__(self, url=None, model=None, timeout=None):
        self.url = _validasi_endpoint(url or OLLAMA_URL)
        self.model = model or OLLAMA_MODEL
        self.timeout = timeout if timeout is not None else OLLAMA_TIMEOUT

    def chat(self, messages, tools=None):
        """Kirim percakapan ke Ollama; kembalikan dictionary respons lengkap.

        Parameter
        ---------
        messages : list
            Riwayat pesan Ollama (system/user/assistant/tool).
        tools : list atau None
            Definisi tool. Bila None, kunci ``tools`` tidak dikirim sehingga
            model diminta menyusun jawaban akhir tanpa tool.
        """
        # Pemanggilan untuk memilih tool tidak memerlukan jawaban panjang.
        # Batas ini proporsional dengan kompleksitas keluaran yang diharapkan.
        batas_token = OLLAMA_NUM_PREDICT_TOOL if tools else OLLAMA_NUM_PREDICT

        info_prompt = {}
        with telemetri.ukur("prompt", detail=info_prompt):
            payload = {
                "model": self.model,
                "messages": messages,
                "stream": False,
                "keep_alive": OLLAMA_KEEP_ALIVE,
                "options": {
                    "temperature": SUHU,
                    "num_predict": batas_token,
                },
            }
            if tools:
                payload["tools"] = tools

            isi_permintaan = json.dumps(payload).encode("utf-8")
            info_prompt["pesan"] = len(messages)
            info_prompt["karakter"] = len(isi_permintaan)
            info_prompt["alat"] = bool(tools)
            info_prompt["batas_token"] = batas_token

            permintaan = urllib.request.Request(
                self.url,
                data=isi_permintaan,
                headers={"Content-Type": "application/json"},
                method="POST",
            )

        try:
            telemetri.tandai("ollama_kirim")
            with telemetri.ukur("model_jaringan"):
                with urllib.request.urlopen(
                    permintaan, timeout=self.timeout
                ) as respons:
                    isi = respons.read().decode("utf-8")
            telemetri.tandai("ollama_terima")
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", "replace").lower()
            except Exception:
                detail = ""
            if exc.code == 404 and "model" in detail and "not found" in detail:
                raise ModelTidakTersedia(
                    "Model %s tidak tersedia di Ollama lokal." % self.model
                ) from exc
            raise OllamaTidakTersedia(
                "Ollama lokal mengembalikan HTTP %s." % exc.code
            ) from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, (socket.timeout, TimeoutError)):
                raise OllamaTimeout("Permintaan ke Ollama lokal melewati batas waktu.") from exc
            raise OllamaTidakTersedia(
                "Ollama lokal tidak dapat dihubungi."
            ) from exc
        except (socket.timeout, TimeoutError) as exc:
            raise OllamaTimeout("Permintaan ke Ollama lokal melewati batas waktu.") from exc
        except OSError as exc:
            raise OllamaTidakTersedia(
                "Ollama lokal tidak dapat dihubungi."
            ) from exc

        try:
            with telemetri.ukur("model_respons"):
                hasil = json.loads(isi)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            LOGGER.warning("Respons Ollama bukan JSON yang valid")
            raise ResponsTidakValid("Respons Ollama bukan JSON yang valid.") from exc

        if not isinstance(hasil, dict) or not isinstance(hasil.get("message"), dict):
            LOGGER.warning("Struktur respons Ollama tidak sesuai: %s", type(hasil))
            raise ResponsTidakValid("Struktur respons Ollama tidak sesuai.")

        telemetri.catat_token(
            hasil.get("prompt_eval_count"),
            hasil.get("eval_count"),
        )
        return hasil
