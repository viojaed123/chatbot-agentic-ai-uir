"""Adapter API FastAPI untuk UIR AI Assistant.

Modul ini HANYA menjembatani frontend dengan :class:`app.orchestrator.Orkestrator`.
Tidak ada tool yang dipanggil langsung dari sini, tidak ada koneksi langsung ke
penyedia model selain lewat Orkestrator, dan tidak ada perubahan pada format
hasil internal.

Sesi disimpan sementara di memori. Artinya: bila server dimatikan, riwayat dan
bukti sesi ikut hilang. Ini disengaja agar tidak menambah penyimpanan privat.
"""

import json
import logging
import os
import re
import secrets
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import hermes_runtime, model_client, telemetri
from app.orchestrator import Orkestrator
from tools.cari_uir import _validasi_url
from web.helpers import cek_model as cek_ollama, sumber_terverifikasi

LOGGER = logging.getLogger(__name__)

MAX_PERTANYAAN = 2000
MAX_SESI = 100
MAX_JUDUL = 200

SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")


class ChatMasuk(BaseModel):
    """Body ``POST /api/chat``.

    ``session_id`` boleh kosong pada pertanyaan pertama. Field dibiarkan longgar
    agar validasi pesan error bisa dibuat konsisten di dalam endpoint.
    """

    session_id: Optional[str] = None
    question: str = ""


class ResetMasuk(BaseModel):
    """Body ``POST /api/session/reset``."""

    session_id: Optional[str] = None


@dataclass
class _Sesi:
    """Satu sesi percakapan milik satu pengguna."""

    id: str
    orkestrator: Any
    runtime: Any = None
    kunci: threading.RLock = field(default_factory=threading.RLock)


class PenyimpananSesi:
    """Penyimpanan sesi dalam memori, aman untuk permintaan bersamaan."""

    def __init__(
        self,
        pembuat_orkestrator: Callable[[], Any] = Orkestrator,
        pembuat_runtime: Optional[Callable[[], Any]] = None,
    ):
        self._pembuat = pembuat_orkestrator
        self._pembuat_runtime = pembuat_runtime
        self._data: Dict[str, _Sesi] = {}
        self._kunci = threading.RLock()

    def _buat_terkunci(self) -> Tuple[str, _Sesi]:
        """Buat sesi baru. Pemanggil harus memegang ``self._kunci``."""
        while True:
            session_id = secrets.token_urlsafe(24)
            if session_id not in self._data:
                break

        if len(self._data) >= MAX_SESI:
            # dict menjaga urutan sisipan, jadi ini sesi paling lama.
            lama = next(iter(self._data))
            del self._data[lama]
            LOGGER.info("Sesi lama dibuang karena batas %d sesi tercapai.", MAX_SESI)

        sesi = _Sesi(
            id=session_id,
            orkestrator=self._pembuat(),
            runtime=self._pembuat_runtime() if self._pembuat_runtime else None,
        )
        self._data[session_id] = sesi
        return session_id, sesi

    def dapatkan(self, session_id: Optional[str]) -> Tuple[str, _Sesi]:
        """Ambil sesi yang dikenal, atau buat sesi baru yang ID-nya dibuat server."""
        if isinstance(session_id, str) and SESSION_ID_RE.fullmatch(session_id):
            with self._kunci:
                sesi = self._data.get(session_id)
                if sesi is not None:
                    return session_id, sesi

        # ID yang tidak dikenal tidak dipercaya. Kami mengeluarkan ID baru.
        with self._kunci:
            return self._buat_terkunci()

    def reset(self, session_id: Optional[str]) -> Tuple[str, _Sesi]:
        """Hapus sesi lama dan buat sesi baru dengan instance Orkestrator baru."""
        with self._kunci:
            if isinstance(session_id, str) and SESSION_ID_RE.fullmatch(session_id):
                self._data.pop(session_id, None)
            return self._buat_terkunci()


def _waktu():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _respons(status, pesan, http_status=200, **tambahan):
    isi = {
        "ok": bool(http_status < 400),
        "status": status,
        "pesan": pesan,
    }
    isi.update(tambahan)
    return JSONResponse(status_code=http_status, content=isi)


def _bersihkan_pertanyaan(nilai):
    """Kembalikan (pertanyaan, pesan_galat)."""
    if not isinstance(nilai, str):
        return None, "Pertanyaan harus berupa teks."
    pertanyaan = nilai.strip()
    if not pertanyaan:
        return None, "Pertanyaan tidak boleh kosong."
    if len(pertanyaan) > MAX_PERTANYAAN:
        return None, f"Pertanyaan terlalu panjang. Maksimal {MAX_PERTANYAAN} karakter."
    if "\x00" in pertanyaan:
        return None, "Pertanyaan mengandung karakter yang tidak diizinkan."
    return pertanyaan, ""


def _judul_sumber(orkestrator):
    """Peta URL -> judul dari bukti internal bila tersedia."""
    peta = {}
    bukti = getattr(orkestrator, "bukti", None) or []
    if not isinstance(bukti, (list, tuple)):
        return peta

    for rekam in bukti:
        if not isinstance(rekam, dict):
            continue
        url = _validasi_url(rekam.get("url"))
        judul = rekam.get("judul")
        if url and isinstance(judul, str) and judul.strip():
            peta[url] = judul.strip()[:MAX_JUDUL]
    return peta


def _status_sumber_api(hasil):
    """Status sumber per halaman, bukan sekadar daftar URL."""
    nilai = hasil.get("status_sumber")
    if not isinstance(nilai, dict):
        return {}

    aman = {}
    for url, detail in nilai.items():
        if not isinstance(detail, dict):
            continue
        aman[str(url)] = {
            kunci: detail.get(kunci)
            for kunci in (
                "retrieved", "read", "evidence", "used", "cited",
                "judul", "jenis", "tanggal", "http", "panjang_teks",
                "skor", "alasan",
            )
            if kunci in detail
        }
    return aman


def _log_status_sumber(session_id, status_sumber):
    """Log terstruktur per permintaan dan per halaman."""

    if not isinstance(status_sumber, dict):
        return

    LOGGER.info("status_sumber permintaan session=%s halaman=%d",
                session_id, len(status_sumber))

    for url, detail in status_sumber.items():
        if not isinstance(detail, dict):
            continue

        LOGGER.info("status_sumber halaman %s", json.dumps({
            "session": session_id,
            "url": url,
            "retrieved": bool(detail.get("retrieved")),
            "read": bool(detail.get("read")),
            "evidence": bool(detail.get("evidence")),
            "used": bool(detail.get("used")),
            "cited": bool(detail.get("cited")),
            "http": detail.get("http"),
            "panjang_teks": detail.get("panjang_teks", 0),
            "skor": detail.get("skor", 0.0),
            "alasan": detail.get("alasan", []),
        }, ensure_ascii=False, default=str))


def _sumber_api(hasil, orkestrator):
    """Hanya kembalikan URL yang sudah terverifikasi backend."""
    urls = sumber_terverifikasi(hasil)
    if not urls:
        return []

    judul = _judul_sumber(orkestrator)
    status = _status_sumber_api(hasil)
    keluaran = []

    for url in urls:
        item = {"url": url, "judul": judul.get(url, "")}
        detail = status.get(url)
        if detail:
            item["status"] = detail
        keluaran.append(item)

    return keluaran


def create_app(
    pembuat_orkestrator: Callable[[], Any] = Orkestrator,
    penyimpanan: Optional[PenyimpananSesi] = None,
    direktori_frontend: Optional[Path] = None,
    pembuat_runtime: Optional[Callable[[], Any]] = None,
    runtime_aktif: Optional[str] = None,
) -> FastAPI:
    """Buat aplikasi FastAPI. Parameter memudahkan pengujian tanpa jaringan."""
    app = FastAPI(
        title="UIR AI Assistant API",
        description="Adapter API untuk chatbot Agentic AI UIR.",
        version="1.0.0",
    )

    if runtime_aktif is None:
        runtime_aktif = os.environ.get("AGENT_RUNTIME", "hermes")
    # Injeksi orkestrator tiruan (unit test / modul demo) secara eksplisit
    # memilih runtime internal agar perilaku lama tetap teruji.
    if pembuat_orkestrator is not Orkestrator:
        runtime_aktif = "internal"
    runtime_aktif = "hermes" if str(runtime_aktif).lower() == "hermes" else "internal"
    app.state.agent_runtime = runtime_aktif

    if runtime_aktif == "hermes" and pembuat_runtime is None:
        pembuat_runtime = hermes_runtime.HermesRuntime
    pembuat = pembuat_runtime if runtime_aktif == "hermes" else None
    sesi_store = penyimpanan or PenyimpananSesi(pembuat_orkestrator, pembuat)

    @app.exception_handler(RequestValidationError)
    async def _validasi_gagal(request: Request, exc: RequestValidationError):
        return _respons(
            "validasi_gagal",
            "Permintaan tidak valid. Periksa format JSON yang dikirim.",
            400,
        )

    @app.exception_handler(Exception)
    async def _kesalahan_internal(request: Request, exc: Exception):
        LOGGER.exception("Kesalahan tak terduga pada API")
        return _respons(
            "kesalahan_internal",
            "Terjadi kesalahan internal. Detail telah dicatat dalam log.",
            500,
        )

    @app.get("/api/health")
    def health():
        try:
            if runtime_aktif == "hermes":
                status = hermes_runtime.health()
            else:
                status = cek_ollama()
        except Exception:
            LOGGER.exception("Pemeriksaan penyedia model gagal")
            # Jangan salah melaporkan Ollama saat konfigurasi aktif TokenHarbor.
            aktif = model_client.penyedia_aktif()
            status = {
                "ok": False,
                "model": model_client.DEEPSEEK_MODEL,
                "model_api": "",
                "model_tersedia": False,
                "penyedia": aktif,
                "lokal": aktif == "ollama",
                "pesan": "Pemeriksaan penyedia model gagal.",
            }

        # Status lama (sebelum migrasi) hanya berisi data Ollama. Jika kedua
        # field penyedia tidak ada, perlakukan sebagai rollback Ollama agar
        # kontrak lama tetap berfungsi. Untuk API cloud, field lokal berasal
        # dari cek_model dan bernilai False.
        penyedia = status.get("penyedia")
        lokal = status.get("lokal")
        if penyedia is None and lokal is None:
            penyedia, lokal = "ollama", True
        else:
            penyedia = penyedia or ("ollama" if lokal else "deepseek")
            lokal = bool(lokal) if lokal is not None else penyedia == "ollama"

        return {
            "ok": bool(status.get("ok")),
            "model": status.get("model", ""),
            # ID model dari metadata penyedia (/models), bila tersedia.
            "model_api": status.get("model_api", ""),
            "model_tersedia": bool(status.get("model_tersedia")),
            "penyedia": penyedia,
            # Tetap disertakan untuk klien lama, namun nilainya mengikuti
            # penyedia aktif, bukan diforce true.
            "lokal": bool(lokal),
            "agent_runtime": runtime_aktif,
            "pesan": status.get("pesan", ""),
            "waktu": _waktu(),
        }

    @app.post("/api/chat")
    def chat(body: ChatMasuk):
        telemetri.mulai()
        telemetri.tandai("api_terima")

        try:
            with telemetri.ukur("validasi"):
                pertanyaan, galat = _bersihkan_pertanyaan(body.question)
            telemetri.tandai("validasi_selesai")
            if galat:
                return _respons("validasi_gagal", galat, 400)

            with telemetri.ukur("pengambilan_sesi"):
                session_id, sesi = sesi_store.dapatkan(body.session_id)
            telemetri.tandai("sesi_selesai")

            try:
                # Kunci per sesi mencegah dua permintaan mengubah sesi yang sama
                # secara bersamaan. Agent runtime dipilih sekali per aplikasi.
                with sesi.kunci:
                    if runtime_aktif == "hermes":
                        if sesi.runtime is None:
                            raise RuntimeError("Runtime Hermes tidak tersedia untuk sesi ini.")
                        telemetri.tandai("hermes_mulai")
                        hasil = sesi.runtime.tanya(pertanyaan, session_id)
                        telemetri.tandai("hermes_selesai")
                    else:
                        telemetri.tandai("orkestrator_mulai")
                        hasil = sesi.orkestrator.tanya(pertanyaan)
                        telemetri.tandai("orkestrator_selesai")
            except Exception:
                LOGGER.exception("Agent runtime gagal memproses pertanyaan")
                return _respons(
                    "kesalahan_internal",
                    "Terjadi kesalahan internal. Detail telah dicatat dalam log.",
                    500,
                    session_id=session_id,
                    jawaban="",
                    sumber=[],
                    performansi=telemetri.selesai(),
                )

            if not isinstance(hasil, dict):
                return _respons(
                    "respons_tidak_valid",
                    "Hasil Orkestrator tidak dapat dibaca.",
                    500,
                    session_id=session_id,
                    jawaban="",
                    sumber=[],
                    performansi=telemetri.selesai(),
                )

            try:
                langkah = int(hasil.get("langkah", 0))
            except (TypeError, ValueError):
                langkah = 0

            telemetri.tambah("langkah", langkah)

            jawaban = hasil.get("jawaban")
            pesan = hasil.get("pesan")

            status_sumber = _status_sumber_api(hasil)
            _log_status_sumber(session_id, status_sumber)
            telemetri.tandai("respons_siap")
            trace_id = telemetri.aktif()["trace_id"] if telemetri.aktif() else ""

            telemetri.tandai("respons_kirim")
            return _respons(
                str(hasil.get("status", "")),
                str(pesan) if pesan else "",
                200,
                session_id=session_id,
                jawaban=str(jawaban) if isinstance(jawaban, str) else "",
                sumber=_sumber_api(hasil, sesi.orkestrator),
                status_sumber=status_sumber,
                langkah=langkah,
                waktu=_waktu(),
                performansi=telemetri.selesai(),
                trace_id=trace_id,
                agent_runtime=runtime_aktif,
                detail_model=hasil.get("runtime") if isinstance(hasil.get("runtime"), dict) else {},
                # ok mengikuti hasil agent runtime, bukan status HTTP.
                **{"ok": bool(hasil.get("ok"))},
            )
        finally:
            # Pastikan konteks thread dibersihkan walau ada return lebih awal.
            telemetri.selesai()

    @app.post("/api/session/reset")
    def reset(body: ResetMasuk):
        session_id, _ = sesi_store.reset(body.session_id)
        return _respons(
            "berhasil",
            "Percakapan baru dimulai. Riwayat sesi hanya disimpan sementara.",
            200,
            session_id=session_id,
        )

    folder = direktori_frontend
    if folder is None:
        folder = Path(__file__).resolve().parent.parent / "frontend"

    # Frontend belum tentu ada saat pengujian API dijalankan.
    if folder.is_dir():
        app.mount(
            "/",
            StaticFiles(directory=str(folder), html=True),
            name="frontend",
        )

    return app


app = create_app()


if __name__ == "__main__":
    # Satu perintah untuk demonstrasi: python -m web.api
    import uvicorn

    uvicorn.run("web.api:app", host="127.0.0.1", port=8000, log_level="info")
