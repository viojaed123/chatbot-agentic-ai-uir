"""Klien resmi Hermes Agent API Server untuk aplikasi UIR.

Modul ini HANYA menjembatani FastAPI ke Hermes Agent. Ia tidak membuat tool,
tidak merencanakan pencarian, dan tidak menjalankan agent loop. Reasoning,
planning, tool calling, dan eksekusi dilakukan oleh Hermes Agent di proses
tersendiri melalui ``http://127.0.0.1:8642``.

Bukti bahwa DeepSeek benar-benar dipakai diambil dari field ``runtime`` pada
status run Hermes, bukan dari label frontend. Kredensial dibaca dari
environment variable; tidak pernah dicetak atau disimpan di source code.
"""

import json
import logging
import os
import re
import secrets
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from app import telemetri
from app.system_prompt import SYSTEM_PROMPT
from tools.cari_uir import _validasi_url

LOGGER = logging.getLogger(__name__)

HERMES_API_URL = os.environ.get("HERMES_API_URL", "http://127.0.0.1:8642").rstrip("/")
HERMES_API_KEY = os.environ.get("HERMES_API_KEY", "")
HERMES_TIMEOUT = float(os.environ.get("HERMES_TIMEOUT", "180"))
HERMES_POLL = float(os.environ.get("HERMES_POLL", "0.3"))
# SSE resmi Hermes dipakai untuk menunggu akhir run. Set ke ``polling`` bila
# ingin kembali ke perilaku lama tanpa mengubah kontrak API/frontend.
HERMES_WAIT_MODE = os.environ.get("HERMES_WAIT_MODE", "sse").strip().lower()
# Hanya permintaan baca (GET) yang diulang otomatis. POST /v1/runs sengaja
# tidak diulang agar satu pertanyaan tidak menghasilkan dua eksekusi agen.
HERMES_MAX_RETRY = max(0, min(3, int(os.environ.get("HERMES_MAX_RETRY", "2"))))
HERMES_RETRY_DELAY = max(0.1, float(os.environ.get("HERMES_RETRY_DELAY", "0.5")))
HERMES_RETRY_MAX_DELAY = float(os.environ.get("HERMES_RETRY_MAX_DELAY", "5"))
HERMES_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4.1-flash:free")
HERMES_PROVIDER = "custom"

_STATUS_AKIR = {"completed", "failed", "cancelled", "interrupted", "unknown"}
_URL_RE = re.compile("https://uir\\.ac\\.id/[^\\s\"'<>)\\},\\\\]+", re.IGNORECASE)

# Bukti model live harus dapat dibaca oleh instance lain (termasuk instance
# HermesRuntime() yang dibuat module-level health() untuk GET /api/health).
_KUNCI_TERVERIFIKASI = threading.Lock()
_MODEL_TERVERIFIKASI = {"model": "", "provider": "", "waktu": 0.0}


def _catat_model_terverifikasi(model, provider=""):
    """Simpan bukti inferensi live, bukan sekadar konfigurasi."""
    with _KUNCI_TERVERIFIKASI:
        _MODEL_TERVERIFIKASI["model"] = str(model or "")
        _MODEL_TERVERIFIKASI["provider"] = str(provider or "")
        _MODEL_TERVERIFIKASI["waktu"] = time.time()


def _baca_model_terverifikasi():
    with _KUNCI_TERVERIFIKASI:
        return dict(_MODEL_TERVERIFIKASI)


class HermesError(Exception):
    """Induk exception untuk kegagalan akses Hermes Agent."""


class HermesTidakTersedia(HermesError):
    def __init__(self, pesan, kode=None):
        super().__init__(pesan)
        self.kode = kode


class HermesTimeout(HermesError):
    pass


class HermesResponsTidakValid(HermesError):
    pass


class HermesModelTidakSesuai(HermesError):
    pass


def _model_cocok(model):
    """Terima ID persis dan varian tanpa suffix ``:free``."""
    if not isinstance(model, str) or not model.strip():
        return False
    model = model.strip()
    if model == HERMES_MODEL:
        return True
    return model == HERMES_MODEL.split(":")[0]


class HermesRuntime:
    """Satu klien untuk satu sesi pengguna.

    Instance dibuat oleh ``web.api.PenyimpananSesi``. ``session_id`` aplikasi
    dipetakan ke session ID Hermes yang tidak pernah dikirim ke frontend.
    """

    def __init__(self, api_url=None, api_key=None, timeout=None, poll=None):
        self.api_url = (api_url or HERMES_API_URL).rstrip("/")
        self.api_key = api_key if api_key is not None else HERMES_API_KEY
        self.timeout = float(timeout if timeout is not None else HERMES_TIMEOUT)
        self.poll = max(0.2, float(poll if poll is not None else HERMES_POLL))
        self.model_terverifikasi = ""
        self.panggilan = 0

    # ------------------------------------------------------------------
    # HTTP dasar
    # ------------------------------------------------------------------
    def _permintaan(self, jalur, data=None, metode=None, timeout=None, session_id=None):
        if not self.api_key:
            raise HermesTidakTersedia("HERMES_API_KEY belum dikonfigurasi.", "kunci_tidak_tersedia")

        alamat = self.api_url + jalur
        badan = None
        kepala = {
            "Authorization": "Bearer " + self.api_key,
            "Accept": "application/json",
        }
        if data is not None:
            badan = json.dumps(data).encode("utf-8")
            kepala["Content-Type"] = "application/json"
        if session_id:
            # Memori jangka panjang dan transkrip dipisah per pengguna.
            kepala["X-Hermes-Session-Id"] = session_id
            kepala["X-Hermes-Session-Key"] = "uir:" + session_id

        permintaan = urllib.request.Request(
            alamat, data=badan, headers=kepala, method=metode
        )
        batas = self.timeout if timeout is None else timeout
        # Idempoten: GET boleh diulang; POST /v1/runs tidak boleh diulang.
        boleh_ulang = metode in (None, "GET")
        jumlah_percobaan = HERMES_MAX_RETRY + 1 if boleh_ulang else 1
        isi = ""

        for percobaan in range(jumlah_percobaan):
            try:
                with urllib.request.urlopen(permintaan, timeout=batas) as respons:
                    isi = respons.read().decode("utf-8", "replace")
                break
            except urllib.error.HTTPError as exc:
                if exc.code in (401, 403):
                    raise HermesTidakTersedia(
                        "Hermes Agent menolak kredensial API.", "kunci_tidak_tersedia"
                    ) from exc
                if exc.code not in (429,) and not 500 <= exc.code < 600:
                    raise HermesResponsTidakValid(
                        "Hermes Agent mengembalikan HTTP %d." % exc.code
                    ) from exc
                kode = "rate_limit" if exc.code == 429 else "hermes_5xx"
                pesan = (
                    "Hermes Agent sedang dibatasi frekuensi permintaannya."
                    if kode == "rate_limit" else
                    "Hermes Agent mengalami gangguan server."
                )
                if percobaan + 1 >= jumlah_percobaan:
                    detik = self._detik_tunggu(exc.headers.get("Retry-After"))
                    if detik:
                        pesan += " Coba lagi dalam %d detik." % detik
                    raise HermesTidakTersedia(pesan, kode) from exc
                time.sleep(self._jeda_ulang(percobaan, exc.headers.get("Retry-After")))
            except (socket.timeout, TimeoutError):
                if percobaan + 1 >= jumlah_percobaan:
                    raise HermesTimeout(
                        "Permintaan ke Hermes Agent melewati batas waktu."
                    ) from None
                time.sleep(self._jeda_ulang(percobaan, None))
            except urllib.error.URLError:
                if percobaan + 1 >= jumlah_percobaan:
                    raise HermesTidakTersedia(
                        "Hermes Agent tidak dapat dihubungi.", "koneksi_gagal"
                    ) from None
                time.sleep(self._jeda_ulang(percobaan, None))
            except OSError:
                if percobaan + 1 >= jumlah_percobaan:
                    raise HermesTidakTersedia(
                        "Koneksi ke Hermes Agent gagal.", "koneksi_gagal"
                    ) from None
                time.sleep(self._jeda_ulang(percobaan, None))

        try:
            return json.loads(isi) if isi.strip() else {}
        except (ValueError, UnicodeDecodeError):
            raise HermesResponsTidakValid("Respons Hermes Agent bukan JSON valid.") from None

    @staticmethod
    def _detik_tunggu(nilai):
        """Ambil detik dari Retry-After bila berupa angka; abaikan format tanggal."""
        try:
            detik = int(float(str(nilai).strip()))
        except (TypeError, ValueError):
            return 0
        return max(0, min(detik, 60))

    @staticmethod
    def _jeda_ulang(percobaan, retry_after=None):
        jeda = HERMES_RETRY_DELAY * (2 ** min(percobaan, 3))
        detik = HermesRuntime._detik_tunggu(retry_after)
        if detik:
            jeda = max(jeda, detik)
        return min(jeda, HERMES_RETRY_MAX_DELAY)

    # ------------------------------------------------------------------
    # Kesehatan: tidak mengklaim model aktif sebelum inferensi berhasil
    # ------------------------------------------------------------------
    def health(self):
        try:
            status = self._permintaan("/health", timeout=min(10, self.timeout))
        except HermesError as exc:
            LOGGER.warning("Health Hermes gagal: %s", exc)
            return {
                "ok": False,
                "model": HERMES_MODEL,
                "model_api": "",
                "model_tersedia": False,
                "jumlah_model": 0,
                "penyedia": "tokenharbor",
                "lokal": False,
                "agent_runtime": "hermes",
                "pesan": "Hermes Agent tidak dapat dihubungi.",
            }

        siap = str(status.get("status") or "").lower() == "ok"
        bukti = _baca_model_terverifikasi()
        model_aktual = self.model_terverifikasi or bukti.get("model", "")
        terverifikasi = siap and _model_cocok(model_aktual)
        return {
            "ok": siap,
            "model": HERMES_MODEL,
            "model_api": model_aktual,
            "model_tersedia": terverifikasi,
            "jumlah_model": 1 if terverifikasi else 0,
            "penyedia": "tokenharbor",
            "lokal": False,
            "agent_runtime": "hermes",
            "pesan": (
                "Hermes Agent siap dan model TokenHarbor terverifikasi."
                if terverifikasi else
                "Hermes Agent siap; model belum diverifikasi lewat inferensi live."
                if siap else
                "Hermes Agent tidak siap."
            ),
        }

    # ------------------------------------------------------------------
    # Eksekusi melalui Hermes Agent
    # ------------------------------------------------------------------
    def _mulai_run(self, pertanyaan, session_hermes):
        data = {
            "input": pertanyaan,
            "session_id": session_hermes,
            "instructions": SYSTEM_PROMPT,
            "model": HERMES_MODEL,
            "provider": HERMES_PROVIDER,
        }
        # Setiap pertanyaan sengaja menjadi run baru tanpa idempotency key,
        # sehingga tidak ada jawaban lama yang diputar ulang.
        telemetri.tandai("hermes_run_post_mulai")
        with telemetri.ukur("hermes_run_post"):
            hasil = self._permintaan(
                "/v1/runs", data=data, metode="POST", session_id=session_hermes
            )
        telemetri.tandai("hermes_run_post_selesai")
        if not isinstance(hasil, dict) or not hasil.get("run_id"):
            raise HermesResponsTidakValid("Hermes tidak mengembalikan run_id.")
        self.panggilan += 1
        return str(hasil["run_id"])

    def _tunggu_sse(self, run_id, session_hermes=None):
        """Tunggu event terminal lewat SSE resmi ``/v1/runs/{id}/events``.

        Hanya nama event yang dibaca. Isi ``preview``, ``text``, dan ``output``
        tidak disimpan, sehingga tidak ada teks halaman atau kredensial yang
        masuk log/telemetri.
        """
        if not self.api_key:
            raise HermesTidakTersedia(
                "HERMES_API_KEY belum dikonfigurasi.", "kunci_tidak_tersedia"
            )
        alamat = (
            self.api_url + "/v1/runs/"
            + urllib.parse.quote(run_id, safe="") + "/events"
        )
        kepala = {
            "Authorization": "Bearer " + self.api_key,
            "Accept": "text/event-stream",
        }
        if session_hermes:
            kepala["X-Hermes-Session-Id"] = session_hermes
            kepala["X-Hermes-Session-Key"] = "uir:" + session_hermes
        permintaan = urllib.request.Request(alamat, headers=kepala, method="GET")
        batas = min(self.timeout, 30.0)
        with urllib.request.urlopen(permintaan, timeout=batas) as respons:
            for baris in respons:
                if not baris.startswith(b"data: "):
                    continue
                try:
                    peristiwa = json.loads(baris[6:].decode("utf-8", "replace"))
                except (ValueError, UnicodeDecodeError):
                    continue
                if not isinstance(peristiwa, dict):
                    continue
                nama = str(peristiwa.get("event") or "").lower()
                if nama in _STATUS_AKIR or nama in (
                    "run.completed", "run.failed", "run.cancelled", "run.interrupted"
                ):
                    return True
        return False

    def _tunggu_run(self, run_id, session_hermes=None):
        mulai = time.monotonic()
        jalur = "/v1/runs/" + urllib.parse.quote(run_id, safe="")
        telemetri.tandai("hermes_polling_mulai")

        if HERMES_WAIT_MODE == "sse":
            try:
                telemetri.tambah("sse")
                with telemetri.ukur("tunggu_sse"):
                    selesai = self._tunggu_sse(run_id, session_hermes)
                if selesai:
                    with telemetri.ukur("status_akhir"):
                        status = self._permintaan(jalur)
                    if isinstance(status, dict):
                        telemetri.tandai("hermes_polling_selesai")
                        return status
            except (HermesError, OSError, TimeoutError, ValueError) as exc:
                telemetri.tambah("sse_gagal")
                LOGGER.warning(
                    "SSE Hermes tidak dapat dipakai (%s); beralih ke polling.",
                    type(exc).__name__,
                )

        while True:
            telemetri.tambah("polling")
            with telemetri.ukur("polling_status"):
                status = self._permintaan(jalur)
            if not isinstance(status, dict):
                raise HermesResponsTidakValid("Status run Hermes tidak valid.")
            keadaan = str(status.get("status") or "").lower()
            if keadaan in _STATUS_AKIR:
                telemetri.tandai("hermes_polling_selesai")
                return status
            if time.monotonic() - mulai >= self.timeout:
                raise HermesTimeout("Run Hermes Agent melewati batas waktu.")
            time.sleep(self.poll)

    def _kumpulkan_url(self, objek, keluaran=None):
        if keluaran is None:
            keluaran = []
        if isinstance(objek, str):
            for cocok in _URL_RE.findall(objek):
                url = _validasi_url(cocok.rstrip(".,;\\"))
                if url and url not in keluaran:
                    keluaran.append(url)
        elif isinstance(objek, dict):
            for nilai in objek.values():
                self._kumpulkan_url(nilai, keluaran)
        elif isinstance(objek, (list, tuple)):
            for nilai in objek:
                self._kumpulkan_url(nilai, keluaran)
        return keluaran

    def _tool_dan_sumber(self, session_hermes, sejak=None):
        """Ambil keluaran tool UIR dari sesi Hermes untuk verifikasi sitasi.

        Hanya pesan ``role=tool`` dari ``mcp__uir__*`` yang dihitung. URL juga
        diambil hanya dari pesan tersebut, sehingga sitasi tidak bisa muncul
        dari teks yang dibuat model tanpa melalui tool.
        """
        telemetri.tandai("hermes_transkrip_mulai")
        try:
            with telemetri.ukur("transkrip_sumber"):
                pesan = self._permintaan(
                    "/api/sessions/" + urllib.parse.quote(session_hermes, safe="") + "/messages",
                    timeout=min(20, self.timeout),
                )
        except HermesError:
            telemetri.tandai("hermes_transkrip_selesai")
            return 0, []

        daftar = None
        if isinstance(pesan, dict):
            daftar = pesan.get("messages")
            if not isinstance(daftar, list):
                daftar = pesan.get("data")
        if not isinstance(daftar, list):
            daftar = pesan if isinstance(pesan, list) else []

        jumlah_tool = 0
        url_tool = []
        for item in daftar:
            if not isinstance(item, dict):
                continue
            peran = str(item.get("role") or item.get("type") or "").lower()
            if "tool" not in peran:
                continue
            nama = str(item.get("tool_name") or "")
            isi = item.get("content")
            if not nama.startswith("mcp__uir__") and "source=\"mcp__uir__" not in str(isi):
                continue
            if sejak is not None:
                try:
                    if float(item.get("timestamp") or 0) < float(sejak) - 0.5:
                        continue
                except (TypeError, ValueError):
                    pass
            jumlah_tool += 1
            for url in self._kumpulkan_url(isi):
                if url not in url_tool:
                    url_tool.append(url)
        telemetri.tandai("hermes_transkrip_selesai")
        return jumlah_tool, url_tool

    def tanya(self, pertanyaan, session_id):
        """Jalankan satu giliran percakapan sepenuhnya di Hermes Agent."""
        if not isinstance(session_id, str) or not session_id:
            session_id = secrets.token_urlsafe(24)
        session_hermes = "uir-" + re.sub(r"[^A-Za-z0-9_-]", "_", session_id)[:200]

        try:
            run_id = self._mulai_run(pertanyaan, session_hermes)
            status = self._tunggu_run(run_id, session_hermes)
        except HermesTidakTersedia as exc:
            status_map = {
                "kunci_tidak_tersedia": "kunci_tidak_tersedia",
                "rate_limit": "rate_limit",
                "hermes_5xx": "hermes_5xx",
                "koneksi_gagal": "penyedia_tidak_tersedia",
            }
            status = status_map.get(exc.kode, "penyedia_tidak_tersedia")
            return {
                "ok": False, "status": status, "jawaban": "", "pesan": str(exc),
                "sumber_terbaca": [], "status_sumber": {}, "langkah": 0,
                "runtime": {"agent": "hermes", "run_id": ""},
            }
        except HermesTimeout as exc:
            return {
                "ok": False, "status": "timeout", "jawaban": "", "pesan": str(exc),
                "sumber_terbaca": [], "status_sumber": {}, "langkah": 0,
                "runtime": {"agent": "hermes", "run_id": run_id if "run_id" in locals() else ""},
            }
        except HermesError as exc:
            LOGGER.exception("Gagal menjalankan Hermes Agent")
            return {
                "ok": False, "status": "respons_tidak_valid", "jawaban": "", "pesan": str(exc),
                "sumber_terbaca": [], "status_sumber": {}, "langkah": 0,
                "runtime": {"agent": "hermes", "run_id": run_id if "run_id" in locals() else ""},
            }

        telemetri.tandai("hermes_status_akhir")
        keadaan = str(status.get("status") or "").lower()
        info = status.get("runtime") if isinstance(status.get("runtime"), dict) else {}
        model_aktual = info.get("model") or status.get("model") or ""
        penyedia_aktual = info.get("provider") or ""
        rinci = {
            "agent": "hermes",
            "run_id": str(status.get("run_id") or run_id),
            "model": str(model_aktual),
            "provider": str(penyedia_aktual),
            "status": keadaan,
            "usage": status.get("usage") if isinstance(status.get("usage"), dict) else {},
        }

        if keadaan != "completed":
            teks_status = json.dumps(status, ensure_ascii=False).lower()
            if (
                "iteration_budget" in teks_status
                or "iteration budget" in teks_status
                or "maximum iterations" in teks_status
                or keadaan == "cancelled"
            ):
                status_kode = "batas_langkah_tercapai"
            else:
                status_kode = "penyedia_tidak_tersedia"
            pesan = (
                "Batas langkah agen tercapai. Coba pertanyaan yang lebih spesifik "
                "atau singkat agar pencarian lebih cepat selesai."
                if status_kode == "batas_langkah_tercapai" else
                "Hermes Agent tidak menyelesaikan permintaan."
            )
            return {
                "ok": False, "status": status_kode, "jawaban": "",
                "pesan": pesan,
                "sumber_terbaca": [], "status_sumber": {}, "langkah": 0,
                "runtime": rinci,
            }

        if not _model_cocok(model_aktual):
            LOGGER.error("Model Hermes berbeda dari konfigurasi: %s", model_aktual)
            return {
                "ok": False, "status": "model_tidak_tersedia", "jawaban": "",
                "pesan": "Model yang digunakan Hermes tidak sesuai dengan konfigurasi TokenHarbor.",
                "sumber_terbaca": [], "status_sumber": {}, "langkah": 0,
                "runtime": rinci,
            }
        jawaban = status.get("output")
        if not isinstance(jawaban, str):
            jawaban = ""
        jawaban = jawaban.strip()
        if jawaban:
            self.model_terverifikasi = str(model_aktual)
            _catat_model_terverifikasi(model_aktual, penyedia_aktual)

        jumlah_tool, url_tool = self._tool_dan_sumber(session_hermes, status.get("created_at"))
        url_jawaban = [
            url for url in self._kumpulkan_url(jawaban) if url in url_tool
        ]

        status_sumber = {}
        for url in url_tool:
            status_sumber[url] = {
                "retrieved": True,
                "read": True,
                "evidence": True,
                "used": True,
                "cited": url in url_jawaban,
                "alasan": ["tool Hermes Agent"],
            }

        if not jawaban:
            return {
                "ok": False, "status": "sumber_tidak_tersedia", "jawaban": "",
                "pesan": "Hermes Agent tidak menghasilkan jawaban akhir.",
                "sumber_terbaca": url_tool, "status_sumber": status_sumber,
                "langkah": jumlah_tool, "runtime": rinci,
            }

        telemetri.tandai("hermes_jawaban_siap")
        return {
            "ok": True,
            "status": "berhasil",
            "jawaban": jawaban,
            "pesan": "",
            "sumber_terbaca": url_tool,
            "status_sumber": status_sumber,
            "langkah": jumlah_tool,
            "runtime": rinci,
        }


def health():
    """Pemeriksaan ringan untuk ``GET /api/health``.

    Tidak pernah mengklaim model aktif hanya dari konfigurasi; status
    ``model_tersedia`` baru benar setelah ``HermesRuntime.tanya()`` berhasil
    dan buktinya disimpan bersama di modul ini.
    """
    return HermesRuntime().health()
