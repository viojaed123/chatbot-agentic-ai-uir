
"""
Antarmuka web Streamlit — Chatbot Agentic AI UIR.

Jalankan dari root proyek:
    streamlit run web/app.py

Seluruh penalaran, tool calling, memori sesi, guardrails, dan
verifikasi bukti tetap berada di app.orchestrator.

File ini hanya menampilkan hasil dan meneruskan pertanyaan
pengguna ke satu instance orchestrator per sesi.
"""

# ============================================================
# 1. PERSIAPAN PATH PROYEK
# ============================================================

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Pastikan root proyek berada di urutan pertama pencarian modul.
project_root_str = str(PROJECT_ROOT)

if project_root_str in sys.path:
    sys.path.remove(project_root_str)

sys.path.insert(0, project_root_str)

# Pastikan folder app benar-benar merupakan package Python.
if not (PROJECT_ROOT / "app" / "__init__.py").is_file():
    raise ImportError(
        "Folder app tidak ditemukan atau app/__init__.py belum tersedia. "
        f"Lokasi proyek: {PROJECT_ROOT}"
    )

# ============================================================
# 2. IMPORT MODUL
# ============================================================

import logging

import streamlit as st

from app import model_client

from web.helpers import (
    cek_model,
    jalankan_pertanyaan,
    pesan_status,
    reset_state,
    siapkan_state,
    tambah_pesan,
)

LOGGER = logging.getLogger(__name__)

# ============================================================
# 3. KONFIGURASI APLIKASI
# ============================================================

NAMA_APLIKASI = "Chatbot Agentic AI UIR"

# Label penyedia mengikuti host endpoint, bukan sekadar nama variabel env.
NAMA_PENYEDIA = {
    "tokenharbor": "TokenHarbor (DeepSeek)",
    "deepseek": "DeepSeek API",
    "ollama": "Ollama lokal",
}

DESKRIPSI = (
    "Asisten informasi Universitas Islam Riau berbasis Agentic "
    "Artificial Intelligence."
)

CONTOH_PERTANYAAN = [
    "Apa itu Universitas Islam Riau?",
    "Fakultas apa saja yang tersedia di UIR?",
    "Bagaimana informasi penerimaan mahasiswa baru?",
    "Di mana saya bisa menemukan informasi beasiswa UIR?",
]

# ============================================================
# 4. CSS TAMPILAN
# ============================================================

CSS = """
<style>
    .stApp {
        background: linear-gradient(
            180deg,
            #eef5ff 0%,
            #ffffff 42%
        );
    }

    .blok-judul {
        background: linear-gradient(
            120deg,
            #0b3d91 0%,
            #1a73e8 100%
        );
        color: #ffffff;
        padding: 1.35rem 1.5rem;
        border-radius: 18px;
        margin-bottom: 1.1rem;
        box-shadow: 0 10px 26px rgba(11, 61, 145, 0.18);
    }

    .blok-judul h1 {
        margin: 0;
        font-size: 1.65rem;
        color: #ffffff;
    }

    .blok-judul p {
        margin: 0.35rem 0 0;
        color: #e8f1ff;
        font-size: 0.92rem;
    }

    .kartu-sambutan {
        background: #ffffff;
        border: 1px solid #d9e6fb;
        border-left: 5px solid #1a73e8;
        border-radius: 14px;
        padding: 1.1rem 1.25rem;
        margin-bottom: 0.9rem;
    }

    .kartu-sambutan h3 {
        margin: 0 0 0.4rem;
        color: #0b3d91;
    }

    .kartu-sambutan p {
        margin: 0;
        color: #42526e;
    }

    .stChatMessage {
        border-radius: 14px;
    }

    .stSidebar {
        background: #f7faff;
    }

    div[data-testid="stSidebar"] h3 {
        color: #0b3d91;
    }

    .stButton > button {
        border-radius: 10px;
        border: 1px solid #c7d9f7;
    }

    .stChatInputContainer {
        padding-bottom: 1.2rem;
    }

    @media (max-width: 640px) {
        .blok-judul {
            padding: 1rem;
            border-radius: 12px;
        }

        .blok-judul h1 {
            font-size: 1.3rem;
        }

        .kartu-sambutan {
            padding: 0.9rem;
        }
    }
</style>
"""


# ============================================================
# 5. STATUS PENYEDIA MODEL
# ============================================================

def _tampilkan_status_sidebar(status):
    """Menampilkan status koneksi berdasarkan hasil pemeriksaan."""

    penyedia = status.get("penyedia") or model_client.PROVIDER
    nama = NAMA_PENYEDIA.get(penyedia, NAMA_PENYEDIA["ollama"])

    if status.get("ok") and status.get("model_tersedia"):
        st.success("%s: terhubung" % nama)

    elif status.get("ok"):
        st.warning("%s terhubung, model belum tersedia" % nama)

    else:
        st.error("%s: tidak terhubung" % nama)

    st.caption(status.get("pesan", ""))

    # Utamakan identitas dari metadata API bila tersedia.
    model = (
        status.get("model_api")
        or status.get("model")
        or model_client.DEEPSEEK_MODEL
    )

    st.markdown(
        "**Model aktif:** `%s`" % model
    )


# ============================================================
# 6. SIDEBAR
# ============================================================

def _sidebar():
    """Menampilkan informasi dan kontrol pada sidebar."""

    with st.sidebar:

        st.markdown("### " + NAMA_APLIKASI)

        st.caption(DESKRIPSI)

        st.divider()

        # Status sistem
        st.markdown("#### Status sistem")

        _tampilkan_status_sidebar(
            st.session_state.status_ollama
        )

        # Perbarui status koneksi
        if st.button(
            "Perbarui status koneksi",
            use_container_width=True
        ):
            st.session_state.status_ollama = cek_model()
            st.rerun()

        # Percakapan baru
        if st.button(
            "Percakapan baru",
            use_container_width=True
        ):
            reset_state(st.session_state)
            st.rerun()

        st.divider()

        # Kemampuan chatbot
        st.markdown("#### Kemampuan chatbot")

        st.markdown(
            "- Mencari halaman publik UIR\n"
            "- Membaca isi halaman resmi\n"
            "- Memakai cache agar lebih cepat\n"
            "- Menyertakan tautan sumber\n"
            "- Membatasi pencarian maksimal 5 langkah"
        )

        st.divider()

        st.caption(
            "Menggunakan informasi publik dari "
            "https://uir.ac.id/. "
            "Belum merupakan layanan resmi Universitas Islam Riau."
        )


# ============================================================
# 7. MENAMPILKAN SUMBER INFORMASI
# ============================================================

def _tampilkan_sumber(sumber, ok):
    """Menampilkan tautan sumber yang dikirim orchestrator."""

    if sumber:

        st.markdown("**Sumber informasi:**")

        for url in sumber:

            st.markdown(
                "- [%s](%s)" % (url, url)
            )

    elif ok:

        st.caption(
            "Sumber informasi tidak tersedia untuk jawaban ini."
        )


# ============================================================
# 8. MENAMPILKAN RIWAYAT PERCAKAPAN
# ============================================================

def _tampilkan_riwayat():
    """Menampilkan seluruh pesan dalam sesi percakapan."""

    for item in st.session_state.pesan:

        with st.chat_message(item["role"]):

            if item.get("content"):
                st.markdown(item["content"])

            if item["role"] == "assistant":

                _tampilkan_sumber(
                    item.get("sumber") or [],
                    item.get("ok", True)
                )

                if not item.get("ok"):

                    st.warning(
                        pesan_status(item.get("status"))
                    )


# ============================================================
# 9. TAMPILAN SAMBUTAN DAN CONTOH PERTANYAAN
# ============================================================

def _tampilkan_sambutan():

    st.markdown(
        '<div class="kartu-sambutan">'
        "<h3>Selamat datang di Chatbot Agentic AI UIR</h3>"
        "<p>Saya dapat membantu mencari informasi publik "
        "tentang Universitas Islam Riau dari halaman resmi "
        "uir.ac.id. Pilih contoh pertanyaan atau ketik "
        "pertanyaan Anda di bawah.</p>"
        "</div>",
        unsafe_allow_html=True,
    )

    st.markdown("**Contoh pertanyaan**")

    kolom = st.columns(2)

    for indeks, pertanyaan in enumerate(CONTOH_PERTANYAAN):

        with kolom[indeks % 2]:

            if st.button(
                pertanyaan,
                use_container_width=True,
                key="contoh_%d" % indeks
            ):
                _kirim(pertanyaan)


# ============================================================
# 10. MENGIRIM PERTANYAAN KE ORCHESTRATOR
# ============================================================

def _kirim(pertanyaan):
    """
    Meneruskan pertanyaan ke satu instance orchestrator
    yang sama selama sesi Streamlit berlangsung.
    """

    teks = (
        pertanyaan.strip()
        if isinstance(pertanyaan, str)
        else ""
    )

    if not teks:
        return

    # Simpan pesan pengguna.
    tambah_pesan(
        st.session_state,
        "user",
        teks
    )

    # Jalankan orchestrator dengan label sesuai penyedia aktif.
    penyedia = st.session_state.get("status_ollama", {}).get(
        "penyedia", model_client.PROVIDER
    )
    nama_penyedia = NAMA_PENYEDIA.get(penyedia, "Ollama")
    with st.spinner(
        f"{nama_penyedia} sedang memproses pertanyaan..."
    ):

        hasil = jalankan_pertanyaan(
            st.session_state.orkestrator,
            teks
        )

    if hasil is None:
        return

    # Simpan jawaban assistant beserta sumber dan statusnya.
    tambah_pesan(
        st.session_state,
        "assistant",
        hasil["content"],
        sumber=hasil["sumber"],
        status=hasil["status"],
        ok=hasil["ok"],
    )

    st.rerun()


# ============================================================
# 11. FUNGSI UTAMA APLIKASI
# ============================================================

def main():

    # Konfigurasi halaman Streamlit.
    st.set_page_config(
        page_title=NAMA_APLIKASI,
        page_icon="🎓",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    # Terapkan CSS.
    st.markdown(
        CSS,
        unsafe_allow_html=True
    )

    # Siapkan state dan satu instance orchestrator per sesi.
    siapkan_state(st.session_state)

    # Header aplikasi.
    st.markdown(
        '<div class="blok-judul">'
        "<h1>" + NAMA_APLIKASI + "</h1>"
        "<p>" + DESKRIPSI + "</p>"
        "</div>",
        unsafe_allow_html=True,
    )

    # Sidebar.
    _sidebar()

    # Sambutan hanya muncul ketika belum ada percakapan.
    if not st.session_state.pesan:
        _tampilkan_sambutan()

    # Riwayat percakapan.
    _tampilkan_riwayat()

    # Kolom input pertanyaan.
    pertanyaan = st.chat_input(
        "Tanyakan sesuatu tentang UIR..."
    )

    if pertanyaan:
        _kirim(pertanyaan)


# ============================================================
# 12. EKSEKUSI APLIKASI
# ============================================================

if __name__ == "__main__":
    main()
