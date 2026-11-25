"""Helper antarmuka web yang dapat diuji tanpa Streamlit dan tanpa jaringan.

Modul ini sengaja berisi logika murni:

* pemeriksaan koneksi penyedia model aktif (DeepSeek API atau Ollama rollback);
* filter sumber hanya dari data yang dikembalikan orchestrator;
* normalisasi hasil ``Orkestrator.tanya()`` untuk ditampilkan;
* inisialisasi dan pengaturan ulang ``st.session_state``.

UI di :mod:`web.app` hanya memanggil fungsi-fungsi di sini; tidak ada tool
calling kedua, tidak ada klien model cloud, dan tidak ada eksekusi kode/SQL
dari input pengguna.
"""

import json
import logging
import socket
import urllib.error
import urllib.parse
import urllib.request

from app import model_client, ollama_client
from app.model_client import (
    KunciTidakTersedia,
    ModelError,
    ModelTidakTersedia as ModelTidakTersediaAPI,
    ModelTimeout,
    PenyediaTidakTersedia,
    ResponsTidakValid as ResponsModelTidakValid,
)
from app.ollama_client import (
    KonfigurasiTidakValid,
    ModelTidakTersedia,
    OllamaError,
    OllamaTidakTersedia,
    OllamaTimeout,
    ResponsTidakValid,
)
from app.orchestrator import Orkestrator
from tools.cari_uir import _validasi_url

LOGGER = logging.getLogger(__name__)

# Pesan ramah untuk status yang dikembalikan orchestrator. Tidak ada detail
# teknis maupun traceback yang diteruskan ke pengguna.
STATUS_PESAN = {
    "ollama_tidak_tersedia": "Ollama lokal tidak dapat dihubungi. Pastikan server Ollama berjalan.",
    "penyedia_tidak_tersedia": "Penyedia model tidak dapat dihubungi. Silakan coba lagi.",
    "kunci_tidak_tersedia": "Kredensial penyedia model belum dikonfigurasi dengan benar.",
    "model_tidak_tersedia": "Model yang diminta belum tersedia pada penyedia.",
    "timeout": "Permintaan ke penyedia model melewati batas waktu. Silakan coba lagi.",
    "respons_tidak_valid": "Respons penyedia model tidak dapat dipahami. Silakan coba lagi.",
    "konfigurasi_tidak_valid": "Konfigurasi penyedia model tidak sesuai.",
    "sumber_tidak_tersedia": "Informasi belum berhasil diverifikasi dari halaman resmi UIR.",
    "bukti_tidak_cukup": (
        "Halaman UIR sudah dibaca, tetapi butir yang diminta tidak ditemukan "
        "sebagai bukti yang mendukung jawaban."
    ),
    "tool_tidak_dikenal": (
        "Permintaan tool tidak dikenal. Silakan ajukan pertanyaan kembali."
    ),
    "batas_langkah": "Batas pencarian tercapai; jawaban disusun dari data yang sudah tersedia.",
    "batas_langkah_tercapai": (
        "Batas langkah agen tercapai. Silakan persempit pertanyaan atau coba lagi."
    ),
    "validasi_gagal": "Pertanyaan kosong atau tidak valid.",
    "kesalahan_internal": "Terjadi kesalahan internal. Detail telah dicatat dalam log.",
}

_PETA_EXCEPTION = (
    (KunciTidakTersedia, "kunci_tidak_tersedia"),
    (OllamaTidakTersedia, "ollama_tidak_tersedia"),
    (PenyediaTidakTersedia, "penyedia_tidak_tersedia"),
    (ModelTidakTersedia, "model_tidak_tersedia"),
    (ModelTidakTersediaAPI, "model_tidak_tersedia"),
    (OllamaTimeout, "timeout"),
    (ModelTimeout, "timeout"),
    (ResponsTidakValid, "respons_tidak_valid"),
    (ResponsModelTidakValid, "respons_tidak_valid"),
    (KonfigurasiTidakValid, "konfigurasi_tidak_valid"),
)


def _gagal(pesan):
    return {
        "ok": False,
        "model": ollama_client.OLLAMA_MODEL,
        "model_api": "",
        "model_tersedia": False,
        "jumlah_model": 0,
        "penyedia": "ollama",
        "lokal": True,
        "pesan": pesan,
    }


def url_tags(url=None):
    """Bangun URL ``/api/tags`` lokal dari endpoint chat lokal."""
    alamat = ollama_client._validasi_endpoint(url or ollama_client.OLLAMA_URL)
    bagian = urllib.parse.urlsplit(alamat)
    jalur = bagian.path.rstrip("/")
    if not jalur.endswith("/api/chat"):
        raise KonfigurasiTidakValid("Endpoint Ollama bukan /api/chat.")
    return urllib.parse.urlunsplit((bagian.scheme, bagian.netloc, "/api/tags", "", ""))


def cek_ollama(url=None, timeout=None, model=None):
    """Periksa koneksi ke Ollama lokal secara nyata.

    Mengembalikan dictionary berisi status koneksi, nama model, dan ketersediaan
    model. Kegagalan dikembalikan sebagai data, bukan exception.
    """
    nama = model or ollama_client.OLLAMA_MODEL
    try:
        endpoint = url_tags(url)
        batas = timeout if timeout is not None else min(ollama_client.OLLAMA_TIMEOUT, 10)
        permintaan = urllib.request.Request(
            endpoint, headers={"Accept": "application/json"}, method="GET"
        )
        with urllib.request.urlopen(permintaan, timeout=batas) as respons:
            isi = respons.read().decode("utf-8")
        data = json.loads(isi)
        if not isinstance(data, dict) or not isinstance(data.get("models"), list):
            raise ValueError("Struktur respons /api/tags tidak sesuai.")

        daftar = data["models"]
        tersedia = any(
            isinstance(item, dict) and item.get("name") == nama for item in daftar
        )
        return {
            "ok": True,
            "model": nama,
            "model_api": nama if tersedia else "",
            "model_tersedia": tersedia,
            "jumlah_model": len(daftar),
            "penyedia": "ollama",
            "lokal": True,
            "pesan": (
                "Ollama lokal terhubung." if tersedia
                else "Ollama terhubung, tetapi model belum tersedia."
            ),
        }
    except KonfigurasiTidakValid as exc:
        LOGGER.warning("Endpoint Ollama ditolak: %s", exc)
        return _gagal("Endpoint Ollama tidak valid; hanya server lokal yang diizinkan.")
    except urllib.error.HTTPError as exc:
        LOGGER.warning("Ollama lokal mengembalikan HTTP %s", exc.code)
        return _gagal("Server Ollama lokal mengembalikan kesalahan.")
    except (socket.timeout, TimeoutError):
        LOGGER.warning("Pemeriksaan Ollama lokal timeout")
        return _gagal("Pemeriksaan koneksi Ollama melewati batas waktu.")
    except (urllib.error.URLError, OSError, ValueError, UnicodeDecodeError) as exc:
        LOGGER.warning("Gagal memeriksa Ollama lokal: %s", exc)
        return _gagal("Ollama lokal belum berjalan atau tidak dapat dihubungi.")


def cek_model(url=None, timeout=None, model=None):
    """Periksa penyedia model yang sedang aktif.

    Fungsi ini juga dipakai oleh endpoint health. Untuk rollback Ollama,
    pemeriksaan tetap memakai ``/api/tags`` lokal seperti sebelumnya.
    """
    if model_client.PROVIDER == "ollama":
        return cek_ollama(url=url, timeout=timeout, model=model)

    # Teruskan model yang diminta supaya identitas dari metadata API
    # diverifikasi terhadap konfigurasi aktif, bukan terhadap label UI.
    return model_client.cek_koneksi(base_url=url, timeout=timeout, model=model)


def sumber_terverifikasi(hasil):
    """Ambil tautan sumber yang benar-benar mendukung jawaban akhir.

    Bila orchestrator menyediakan ``status_sumber``, hanya URL berflag
    ``cited`` yang dikembalikan. Untuk hasil lama tanpa status tersebut,
    fungsi tetap memakai ``sumber_terbaca`` agar perilaku lama tidak berubah.
    ``sumber`` (kandidat hasil pencarian) tetap diabaikan karena belum dibaca.
    """
    if not isinstance(hasil, dict):
        return []

    status = hasil.get("status_sumber")
    kandidat = None

    if isinstance(status, dict) and status:
        kandidat = [
            url for url, detail in status.items()
            if isinstance(detail, dict) and detail.get("cited")
        ]

    if kandidat is None:
        kandidat = hasil.get("sumber_terbaca") or []

    keluaran = []
    for url in kandidat:
        if not isinstance(url, str):
            continue
        alamat = _validasi_url(url)
        if alamat and alamat not in keluaran:
            keluaran.append(alamat)
    return keluaran


def pesan_status(status):
    """Pesan ramah untuk status hasil orchestrator."""
    if not isinstance(status, str):
        return STATUS_PESAN["kesalahan_internal"]
    return STATUS_PESAN.get(status, "Jawaban tidak dapat disajikan. Silakan coba pertanyaan lain.")


def jalankan_pertanyaan(orkestrator, pertanyaan):
    """Kirim pertanyaan ke orchestrator dan normalkan hasilnya untuk UI."""
    teks = pertanyaan.strip() if isinstance(pertanyaan, str) else ""
    if not teks:
        return None

    try:
        hasil = orkestrator.tanya(teks)
    except (OllamaError, ModelError) as exc:
        LOGGER.warning("Gagal memanggil orchestrator: %s", exc)
        status = next(
            (stat for jenis, stat in _PETA_EXCEPTION if isinstance(exc, jenis)),
            "kesalahan_internal",
        )
        return {
            "content": "", "sumber": [], "status": status, "ok": False,
            "pesan": pesan_status(status),
        }
    except Exception:
        LOGGER.exception("Kesalahan tak terduga saat memanggil orchestrator")
        return {
            "content": "", "sumber": [], "status": "kesalahan_internal", "ok": False,
            "pesan": pesan_status("kesalahan_internal"),
        }

    if not isinstance(hasil, dict):
        return {
            "content": "", "sumber": [], "status": "respons_tidak_valid", "ok": False,
            "pesan": pesan_status("respons_tidak_valid"),
        }

    jawaban = hasil.get("jawaban")
    return {
        # Jawaban dari orchestrator tidak diubah sama sekali.
        "content": jawaban if isinstance(jawaban, str) else "",
        "sumber": sumber_terverifikasi(hasil),
        "status": hasil.get("status") or "",
        "ok": bool(hasil.get("ok")),
        "pesan": hasil.get("pesan") or "",
    }


def siapkan_state(state, pembuat=None, pemeriksa=None):
    """Inisialisasi kunci sesi bila belum ada (tetap pakai objek lama)."""
    if "orkestrator" not in state:
        state["orkestrator"] = (pembuat or Orkestrator)()
    if "pesan" not in state:
        state["pesan"] = []
    if "status_ollama" not in state:
        state["status_ollama"] = (pemeriksa or cek_model)()
    return state


def reset_state(state, pembuat=None, pemeriksa=None):
    """Mulai percakapan baru: buang riwayat dan buat orchestrator baru."""
    state["orkestrator"] = (pembuat or Orkestrator)()
    state["pesan"] = []
    state["status_ollama"] = (pemeriksa or cek_model)()
    return state


def tambah_pesan(state, role, content, sumber=None, status="", ok=True):
    """Tambah satu pesan ke riwayat sesi tanpa menghapus pesan lama."""
    if "pesan" not in state or not isinstance(state["pesan"], list):
        state["pesan"] = []
    state["pesan"].append({
        "role": role,
        "content": content,
        "sumber": list(sumber or []),
        "status": status,
        "ok": bool(ok),
    })
    return state
