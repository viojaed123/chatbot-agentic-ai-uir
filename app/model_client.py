"""Client model terpusat: API OpenAI-compatible (utama) atau Ollama rollback.

Semua inferensi aplikasi melewati modul ini. Provider dipilih lewat variabel
lingkungan ``MODEL_PROVIDER``:

* ``deepseek`` (default) — endpoint OpenAI-compatible. Secara default memakai
  TokenHarbor dan model ``deepseek-v4.1-flash:free``; dapat diarahkan ke
  penyedia lain lewat ``DEEPSEEK_BASE_URL``;
* ``ollama`` — Hermes 3 8B lokal, dipertahankan sebagai rollback.

Kredensial dibaca hanya dari environment variable ``DEEPSEEK_API_KEY`` (atau
dari ``.env`` lokal yang dimuat sekali oleh :mod:`app`). Nilainya tidak pernah
ditulis ke source, log, telemetri, laporan, atau pesan galat pengguna.
"""

import json
import logging
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

from app import telemetri
from app.ollama_client import OllamaClient

LOGGER = logging.getLogger(__name__)

PROVIDER = os.environ.get("MODEL_PROVIDER", "deepseek").strip().lower()
DEEPSEEK_BASE_URL = os.environ.get(
    "DEEPSEEK_BASE_URL", "https://tokenharbor.ai/v1"
).strip()
DEEPSEEK_MODEL = os.environ.get(
    "DEEPSEEK_MODEL", "deepseek-v4.1-flash:free"
).strip()
DEEPSEEK_TIMEOUT = int(os.environ.get("DEEPSEEK_TIMEOUT", "90"))
DEEPSEEK_MAX_RETRY = max(
    0, min(3, int(os.environ.get("DEEPSEEK_MAX_RETRY", "1")))
)
DEEPSEEK_MAX_TOKENS = int(os.environ.get("DEEPSEEK_MAX_TOKENS", "512"))
DEEPSEEK_MAX_TOKENS_TOOL = int(
    os.environ.get("DEEPSEEK_MAX_TOKENS_TOOL", "256")
)
SUHU = 0

HOST_LOKAL = frozenset({"localhost", "127.0.0.1", "::1"})
_KODE_RETRY = frozenset({408, 429, 500, 502, 503, 504})

# Estimasi per 1 juta token. Model ":free" TokenHarbor tidak memakai token
# berbayar; angka DeepSeek resmi dipertahankan bila base URL diganti ke sana.
# Tagihan sebenarnya selalu berada di dashboard penyedia.
_HARGA = {
    "deepseek-v4.1-flash:free": {"input": 0.0, "output": 0.0, "cache": 0.0},
    "deepseek-flash": {"input": 0.15, "output": 0.60, "cache": 0.003},
    "deepseek-v4-pro": {"input": 0.66, "output": 1.98, "cache": 0.022},
}
_GRATIS = {"input": 0.0, "output": 0.0, "cache": 0.0}


class ModelError(Exception):
    """Induk seluruh kegagalan penyedia model."""


class KonfigurasiTidakValid(ModelError):
    """Konfigurasi penyedia tidak sah."""


class KunciTidakTersedia(KonfigurasiTidakValid):
    """Kredensial API belum tersedia di environment."""


class PenyediaTidakTersedia(ModelError):
    """Layanan model tidak dapat dihubungi atau menolak permintaan."""


class ModelTidakTersedia(ModelError):
    """Model yang diminta tidak tersedia atau tidak diizinkan."""


class ModelTimeout(ModelError):
    """Permintaan melewati batas waktu."""


class ResponsTidakValid(ModelError):
    """Respons API bukan struktur chat completion yang diharapkan."""


def kunci_tersedia():
    """Apakah variabel kredensial DeepSeek sudah diisi (tanpa membacanya keluar)."""
    return bool(os.environ.get("DEEPSEEK_API_KEY", "").strip())


def _validasi_base_url(url):
    """Terima HTTPS untuk produksi; HTTP hanya untuk pengujian lokal."""
    if not isinstance(url, str) or not url.strip():
        raise KonfigurasiTidakValid("Base URL DeepSeek harus berupa URL.")
    try:
        bagian = urllib.parse.urlsplit(url.strip())
    except ValueError as exc:
        raise KonfigurasiTidakValid("Base URL DeepSeek tidak valid.") from exc

    if bagian.scheme not in ("http", "https"):
        raise KonfigurasiTidakValid("Skema base URL harus http atau https.")
    if bagian.username or bagian.password:
        raise KonfigurasiTidakValid("Base URL tidak boleh memuat kredensial.")
    if not bagian.hostname:
        raise KonfigurasiTidakValid("Host base URL tidak ditemukan.")
    if bagian.scheme == "http" and bagian.hostname not in HOST_LOKAL:
        raise KonfigurasiTidakValid(
            "Base URL produksi DeepSeek wajib memakai HTTPS."
        )
    return url.strip().rstrip("/")


def _endpoint_chat(base_url):
    return _validasi_base_url(base_url) + "/chat/completions"


def _endpoint_saldo(base_url):
    """Endpoint lama DeepSeek resmi; dipertahankan untuk kompatibilitas."""
    return _validasi_base_url(base_url) + "/user/balance"


def _endpoint_model(base_url):
    """Endpoint OpenAI-compatible ``/models`` untuk verifikasi model aktual."""
    return _validasi_base_url(base_url) + "/models"


def _nama_penyedia(base_url):
    """Label penyedia berdasarkan host, bukan sekadar nama variabel env."""
    try:
        host = (urllib.parse.urlsplit(base_url).hostname or "").lower()
    except ValueError:
        host = ""
    return "tokenharbor" if "tokenharbor" in host else "deepseek"


def _model_cocok(dikonfigurasi, dari_api):
    """Terima identitas persis atau varian tanpa akhiran ``:free``."""
    if not dari_api:
        return True
    a = str(dikonfigurasi or "").strip().lower()
    b = str(dari_api or "").strip().lower()
    if not a or not b:
        return False
    return a == b or a.split(":", 1)[0] == b.split(":", 1)[0]


def _daftar_model(data):
    """Ambil daftar ID model dari respons ``/models`` OpenAI-compatible."""
    if not isinstance(data, dict):
        return []

    nilai = None
    for kunci in ("data", "models"):
        if isinstance(data.get(kunci), list):
            nilai = data[kunci]
            break
    if nilai is None:
        return []

    hasil = []
    for item in nilai:
        if isinstance(item, str):
            hasil.append(item)
        elif isinstance(item, dict):
            identitas = item.get("id") or item.get("name")
            if isinstance(identitas, str):
                hasil.append(identitas)
    return hasil


def _parse_call(call):
    """Ambil nama dan argumen tool dari format internal maupun DeepSeek."""
    if not isinstance(call, dict):
        return "", {}
    fungsi = call.get("function")
    if not isinstance(fungsi, dict):
        return "", {}
    nama = fungsi.get("name", "")
    args = fungsi.get("arguments", {})
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except (ValueError, TypeError):
            args = {}
    if not isinstance(args, dict):
        args = {}
    return (nama if isinstance(nama, str) else ""), args


def _ke_deepseek(messages):
    """Terjemahkan pesan internal ke format Chat Completions DeepSeek.

    Format internal sengaja mengikuti Ollama supaya orkestrator tidak berubah.
    Tool call DeepSeek harus memuat ``id`` dan argumen berupa string JSON,
    serta pesan tool harus menyertakan ``tool_call_id`` yang cocok.
    """
    hasil = []
    peta_id = {}

    for indeks, item in enumerate(messages or []):
        if not isinstance(item, dict):
            continue
        peran = item.get("role")

        if peran == "assistant" and item.get("tool_calls"):
            panggilan = []
            for urut, call in enumerate(item["tool_calls"] or []):
                nama, args = _parse_call(call)
                identitas = call.get("id") if isinstance(call, dict) else None
                identitas = identitas or "call_%d_%d" % (indeks, urut)
                peta_id[nama] = identitas
                panggilan.append({
                    "id": identitas,
                    "type": "function",
                    "function": {
                        "name": nama,
                        "arguments": json.dumps(args, ensure_ascii=False),
                    },
                })
            hasil.append({
                "role": "assistant",
                "content": item.get("content") or "",
                "tool_calls": panggilan,
            })
            continue

        if peran == "tool":
            nama = item.get("tool_name", "")
            identitas = peta_id.get(nama) or "call_%d" % indeks
            hasil.append({
                "role": "tool",
                "tool_call_id": identitas,
                "content": item.get("content") or "",
            })
            continue

        hasil.append({
            "role": peran,
            "content": item.get("content") or "",
        })

    return hasil


def _estimasi_biaya(model, usage):
    """Estimasi USD dari usage resmi API; bukan tagihan final."""
    nama = str(model or "").strip().lower()
    harga = _HARGA.get(nama) or _HARGA.get(nama.split(":", 1)[0]) or _GRATIS
    try:
        prompt = int(usage.get("prompt_tokens", 0))
        cache = int(usage.get("prompt_cache_hit_tokens", 0))
        output = int(usage.get("completion_tokens", 0))
    except (TypeError, ValueError):
        return 0.0
    miss = max(0, prompt - cache)
    return round(
        (cache / 1_000_000) * harga["cache"]
        + (miss / 1_000_000) * harga["input"]
        + (output / 1_000_000) * harga["output"],
        6,
    )


class DeepSeekClient:
    """Klien DeepSeek API dengan kontrak internal yang sama dengan Ollama."""

    penyedia = "deepseek"
    mode_produksi = True

    def __init__(
        self,
        base_url=None,
        model=None,
        api_key=None,
        timeout=None,
        max_retry=None,
    ):
        self.url = _validasi_base_url(base_url or DEEPSEEK_BASE_URL)
        self.penyedia = _nama_penyedia(self.url)
        self.model = (model or DEEPSEEK_MODEL).strip()
        if not self.model:
            raise KonfigurasiTidakValid("Nama model DeepSeek tidak boleh kosong.")

        kunci = api_key if api_key is not None else os.environ.get(
            "DEEPSEEK_API_KEY", ""
        )
        if not isinstance(kunci, str) or not kunci.strip():
            raise KunciTidakTersedia(
                "DEEPSEEK_API_KEY belum dikonfigurasi di environment."
            )
        # Disimpan privat; tidak pernah masuk log, telemetri, atau pesan galat.
        self._kunci = kunci.strip()
        self.timeout = timeout if timeout is not None else DEEPSEEK_TIMEOUT
        self.max_retry = (
            max_retry if max_retry is not None else DEEPSEEK_MAX_RETRY
        )

    def __repr__(self):
        return "<DeepSeekClient model=%s penyedia=%s>" % (
            self.model,
            self.penyedia,
        )

    def _headers(self):
        return {
            "Content-Type": "application/json",
            "Authorization": "Bearer " + self._kunci,
        }

    def _payload(self, messages, tools):
        batas = (
            DEEPSEEK_MAX_TOKENS_TOOL if tools else DEEPSEEK_MAX_TOKENS
        )
        payload = {
            "model": self.model,
            "messages": _ke_deepseek(messages),
            # Streaming sengaja dimatikan: frontend menunggu respons lengkap.
            "stream": False,
            "temperature": SUHU,
            "max_tokens": batas,
        }
        # Hanya DeepSeek resmi yang menerima parameter ``thinking``. Proxy
        # OpenAI-compatible seperti TokenHarbor tidak perlu menerima field ini;
        # reasoning_content tetap dibuang oleh _normalkan().
        if self.penyedia == "deepseek":
            payload["thinking"] = {"type": "disabled"}
        if tools:
            payload["tools"] = tools
        return payload, batas

    def chat(self, messages, tools=None):
        payload, batas = self._payload(messages, tools)
        isi_permintaan = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        info_prompt = {}
        with telemetri.ukur("prompt", detail=info_prompt):
            info_prompt["pesan"] = len(payload["messages"])
            info_prompt["karakter"] = len(isi_permintaan)
            info_prompt["alat"] = bool(tools)
            info_prompt["batas_token"] = batas

        telemetri.tandai("deepseek_kirim")

        for percobaan in range(self.max_retry + 1):
            permintaan = urllib.request.Request(
                _endpoint_chat(self.url),
                data=isi_permintaan,
                headers=self._headers(),
                method="POST",
            )
            try:
                with telemetri.ukur("model_jaringan"):
                    with urllib.request.urlopen(
                        permintaan, timeout=self.timeout
                    ) as respons:
                        isi = respons.read().decode("utf-8")
                break
            except urllib.error.HTTPError as exc:
                kode = exc.code
                if kode in _KODE_RETRY and percobaan < self.max_retry:
                    time.sleep(0.25 * (percobaan + 1))
                    continue
                if kode in (401, 403):
                    raise KunciTidakTersedia(
                        "Autentikasi penyedia ditolak. Periksa konfigurasi kredensial."
                    ) from exc
                if kode in (400, 402, 404):
                    raise ModelTidakTersedia(
                        "Model atau saldo penyedia tidak tersedia."
                    ) from exc
                raise PenyediaTidakTersedia(
                    "Penyedia model mengembalikan HTTP %s." % kode
                ) from exc
            except (socket.timeout, TimeoutError, urllib.error.URLError) as exc:
                if percobaan < self.max_retry:
                    time.sleep(0.25 * (percobaan + 1))
                    continue
                raise ModelTimeout(
                    "Permintaan ke penyedia model melewati batas waktu."
                ) from exc
            except OSError as exc:
                raise PenyediaTidakTersedia(
                    "Penyedia model tidak dapat dihubungi."
                ) from exc

        telemetri.tandai("penyedia_terima")

        try:
            with telemetri.ukur("model_respons"):
                data = json.loads(isi)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            LOGGER.warning("Respons penyedia bukan JSON yang valid")
            raise ResponsTidakValid(
                "Respons penyedia bukan JSON yang valid."
            ) from exc

        hasil = self._normalkan(data)
        model_api = hasil.get("model") or ""
        if not _model_cocok(self.model, model_api):
            LOGGER.warning("Model dari API berbeda dengan konfigurasi aktif")
            raise ModelTidakTersedia(
                "Model yang dikembalikan penyedia tidak sesuai dengan konfigurasi."
            )
        hasil["model_dari_api"] = model_api

        usage = hasil.get("usage", {})
        telemetri.catat_token(
            usage.get("prompt_tokens"), usage.get("completion_tokens")
        )
        telemetri.catat_api(
            penyedia=self.penyedia,
            model=model_api or self.model,
            usage=usage,
            biaya=_estimasi_biaya(self.model, usage),
        )
        return hasil

    @staticmethod
    def _normalkan(data):
        if not isinstance(data, dict):
            raise ResponsTidakValid("Struktur respons DeepSeek tidak sesuai.")

        pilihan = data.get("choices")
        if not isinstance(pilihan, list) or not pilihan:
            raise ResponsTidakValid("DeepSeek tidak mengembalikan pilihan jawaban.")

        pesan = pilihan[0].get("message")
        if not isinstance(pesan, dict):
            raise ResponsTidakValid("Pesan DeepSeek tidak valid.")

        konten = pesan.get("content")
        if konten is None:
            konten = ""
        if not isinstance(konten, str):
            raise ResponsTidakValid("Konten jawaban DeepSeek bukan teks.")

        panggilan = []
        for call in pesan.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            fungsi = call.get("function")
            if not isinstance(fungsi, dict):
                continue
            panggilan.append({
                "id": call.get("id") or "",
                "type": "function",
                "function": {
                    "name": fungsi.get("name", ""),
                    # Format internal menerima string JSON maupun dictionary.
                    "arguments": fungsi.get("arguments", "{}"),
                },
            })

        usage = data.get("usage")
        if not isinstance(usage, dict):
            usage = {}

        return {
            "message": {
                "role": "assistant",
                "content": konten,
                "tool_calls": panggilan,
            },
            "model": data.get("model", ""),
            "usage": dict(usage),
        }


def penyedia_aktif():
    """Label penyedia sesuai konfigurasi aktif (dipakai UI dan health)."""
    if PROVIDER == "ollama":
        return "ollama"
    return _nama_penyedia(DEEPSEEK_BASE_URL)


def buat_client():
    """Buat client sesuai konfigurasi; DeepSeek adalah provider utama."""
    if PROVIDER == "ollama":
        return OllamaClient()
    if PROVIDER == "deepseek":
        return DeepSeekClient()
    raise KonfigurasiTidakValid(
        "MODEL_PROVIDER harus 'deepseek' atau 'ollama'."
    )


def cek_koneksi(base_url=None, api_key=None, timeout=None, model=None):
    """Periksa kredensial dan verifikasi model aktif lewat ``/models``.

    Endpoint OpenAI-compatible ``/models`` menguji autentikasi sekaligus
    memastikan model yang dikonfigurasi benar-benar tercantam pada penyedia,
    tanpa menghasilkan token inferensi. Bila endpoint tidak tersedia (404),
    pemeriksaan jatuh ke ``/user/balance`` untuk kompatibilitas dengan DeepSeek
    resmi. Nilai kredensial tidak pernah dikembalikan.
    """
    url = base_url or DEEPSEEK_BASE_URL
    nama_model = (model or DEEPSEEK_MODEL).strip()
    penyedia = _nama_penyedia(url)

    def _hasil(ok, model_api="", daftar=None, pesan=""):
        model_api = str(model_api or "")
        return {
            "ok": bool(ok),
            "model": nama_model,
            # Identitas model dari metadata API, bukan label frontend.
            "model_api": model_api,
            "model_tersedia": bool(
                ok and model_api and _model_cocok(nama_model, model_api)
            ),
            "penyedia": penyedia,
            "lokal": False,
            # Field lama dipertahankan untuk klien lama; kini mengikuti
            # keberhasilan autentikasi, bukan saldo DeepSeek resmi.
            "saldo_tersedia": bool(ok),
            "jumlah_model": len(daftar or []),
            "pesan": pesan,
        }

    kunci = api_key if api_key is not None else os.environ.get(
        "DEEPSEEK_API_KEY", ""
    )
    if not isinstance(kunci, str) or not kunci.strip():
        return _hasil(
            False,
            pesan="Kredensial penyedia belum dikonfigurasi di environment.",
        )

    batas = timeout if timeout is not None else min(DEEPSEEK_TIMEOUT, 10)
    header = {
        "Accept": "application/json",
        "Authorization": "Bearer " + kunci.strip(),
    }

    def _get(endpoint):
        permintaan = urllib.request.Request(endpoint, headers=header, method="GET")
        with urllib.request.urlopen(permintaan, timeout=batas) as respons:
            return json.loads(respons.read().decode("utf-8"))

    try:
        data = _get(_endpoint_model(url))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            # Base URL lama mungkin belum menyediakan /models.
            try:
                data = _get(_endpoint_saldo(url))
            except Exception:
                LOGGER.warning("Fallback balance check gagal")
                return _hasil(
                    False,
                    pesan="Penyedia model menolak permintaan pemeriksaan koneksi.",
                )
            if isinstance(data, dict):
                saldo = bool(data.get("is_available"))
                return _hasil(
                    saldo,
                    pesan=(
                        "Penyedia terhubung (endpoint saldo lama)."
                        if saldo else "Saldo penyedia tidak tersedia."
                    ),
                )
            return _hasil(False, pesan="Respons pemeriksaan saldo tidak sesuai.")
        if exc.code in (401, 403):
            pesan = "Autentikasi penyedia ditolak. Periksa konfigurasi kredensial."
        else:
            pesan = "Penyedia model menolak permintaan pemeriksaan koneksi."
        LOGGER.warning("Pemeriksaan model penyedia HTTP %s", exc.code)
        return _hasil(False, pesan=pesan)
    except (socket.timeout, TimeoutError, urllib.error.URLError, OSError, ValueError):
        LOGGER.warning("Gagal memeriksa koneksi penyedia model")
        return _hasil(False, pesan="Penyedia model tidak dapat dihubungi.")

    if not isinstance(data, dict):
        return _hasil(False, pesan="Respons pemeriksaan model tidak sesuai.")

    daftar = _daftar_model(data)
    if not daftar:
        return _hasil(False, pesan="Penyedia tidak mengembalikan daftar model.")

    # Utamakan ID persis bila penyedia mencantumkan dua entri (misalnya
    # ``deepseek-v4.1-flash:free`` dan ``deepseek-v4.1-flash``) agar bukti
    # health check tidak ambigu. Varian tanpa akhiran tetap diterima.
    nama_normal = nama_model.strip().lower()
    cocok = next(
        (item for item in daftar if str(item).strip().lower() == nama_normal), ""
    )
    if not cocok:
        cocok = next((item for item in daftar if _model_cocok(nama_model, item)), "")
    if not cocok:
        return _hasil(
            True,
            daftar=daftar,
            pesan="Penyedia terhubung, tetapi model %s tidak ditemukan." % nama_model,
        )
    return _hasil(
        True,
        model_api=cocok,
        daftar=daftar,
        pesan="Penyedia terhubung; model %s tersedia." % cocok,
    )
