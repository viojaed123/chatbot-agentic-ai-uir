# Product Requirements Document (PRD) — UIR AI Assistant

| Field | Nilai |
|---|---|
| Nama produk | UIR AI Assistant — chatbot informasi internal Universitas Islam Riau berbasis Agentic AI |
| Versi dokumen | 1.0 |
| Tanggal | 24 November 2026 |
| Status | Dokumen kebutuhan (disusun dari laporan capstone) |
| Sumber acuan utama | `C:\Users\USER\Downloads\CAPSTONE AGENT.docx`, Bab I, Bab II, dan Bab III |
| Dokumen pendukung | `AGENTS.md`, `README.md` |

Dokumen ini menyusun kembali kebutuhan produk dari laporan capstone tanpa mengubah tujuan, ruang lingkup, atau arsitektur utama yang ditetapkan.

---

## 1. Konteks dan Pernyataan Masalah

Informasi tentang Universitas Islam Riau (profil, fakultas dan program studi, penerimaan mahasiswa baru, berita, pengumuman, serta layanan akademik) tersebar di banyak halaman pada situs resmi `https://uir.ac.id/`. Calon mahasiswa, mahasiswa, dosen, dan tenaga kependidikan harus menelusuri halaman satu per satu, sehingga memakan waktu dan informasi dapat terlewat.

Chatbot berbasis LLM konvensional memiliki pengetahuan statis dan berisiko menghasilkan jawaban keliru (halusinasi), terutama untuk informasi institusi yang mutakhir. Pendekatan RAG membantu, tetapi umumnya bergantung pada indeks dokumen yang harus diperbarui saat situs berubah. Proyek ini memilih pendekatan **agentic**: agen sendiri memutuskan kapan dan apa yang dicari serta dibaca langsung dari situs resmi saat pertanyaan diajukan, sehingga jawaban lebih mutakhir dan dapat ditelusuri ke sumbernya.

**Rumusan masalah:**

1. Bagaimana merancang arsitektur chatbot agentic AI yang memperoleh informasi dari `uir.ac.id`?
2. Bagaimana mengimplementasikan Hermes sebagai inti penalaran agen beserta tool pencarian dan pembacaan halaman web?
3. Bagaimana tingkat akurasi, kelengkapan jawaban, waktu respons, dan penilaian kegunaan sistem?

*(Sumber: Bab I.1–I.2)*

---

## 2. Tujuan dan Manfaat

### 2.1 Tujuan

1. Merancang arsitektur chatbot berbasis agentic AI yang mengambil informasi dari situs `uir.ac.id`.
2. Mengimplementasikan chatbot dengan Hermes sebagai inti penalaran agen dan tool pengambil informasi dari situs resmi universitas.
3. Menguji dan mengevaluasi sistem dari sisi fungsionalitas, akurasi dan kelengkapan jawaban, waktu respons, serta kegunaan bagi pengguna.

### 2.2 Manfaat

- **Teoretis:** kontribusi kajian penerapan agentic AI dan pengambilan informasi berbasis web pada layanan informasi perguruan tinggi berbahasa Indonesia.
- **Pengguna:** mempermudah calon mahasiswa, mahasiswa, dosen, dan tenaga kependidikan memperoleh informasi UIR secara cepat melalui percakapan.
- **Universitas:** prototipe layanan informasi otomatis yang dapat mengurangi beban pelayanan manual dan mendukung transformasi digital.
- **Mahasiswa:** pengalaman dalam perancangan, implementasi, dan evaluasi sistem berbasis agen kecerdasan buatan.

*(Sumber: Bab I.3–I.4)*

---

## 3. Ruang Lingkup

### 3.1 Termasuk dalam ruang lingkup

- Sumber informasi hanya halaman publik pada domain `uir.ac.id`.
- Bahasa utama interaksi: **Bahasa Indonesia**.
- Sistem bersifat **read-only**: menjawab pertanyaan informasi, tidak melakukan transaksi.
- Inti penalaran agen menggunakan **Hermes**; versi dan cara penjalanan model ditetapkan sesuai sumber daya yang tersedia.
- Pengujian skala terbatas dengan sekumpulan pertanyaan uji dan responden terpilih.
- Evaluasi hanya terhadap informasi yang tersedia pada situs saat pengujian berlangsung.

### 3.2 Di luar ruang lingkup

- Data pribadi atau sistem informasi akademik yang memerlukan autentikasi.
- Sumber selain domain `uir.ac.id`.
- Perubahan konten situs setelah masa pengujian.
- Transaksi, pengubahan data, atau tindakan lain yang bersifat write.

*(Sumber: Bab I.5)*

### 3.3 Pengguna sasaran

Calon mahasiswa, mahasiswa, dosen, dan tenaga kependidikan Universitas Islam Riau.

---

## 4. Kebutuhan Fungsional

| Kode | Kebutuhan | Kriteria penerimaan ringkas |
|---|---|---|
| F1 | Menerima pertanyaan teks berbahasa Indonesia melalui antarmuka chat | Pertanyaan dapat dikirim dan balasan diterima melalui antarmuka web |
| F2 | Memeriksa apakah pertanyaan berada dalam lingkup informasi UIR | Pertanyaan di luar lingkup ditolak dengan sopan dan diarahkan ke kanal resmi |
| F3 | Mencari halaman yang relevan pada domain `uir.ac.id` | Tool pencarian hanya mengembalikan hasil dari domain yang diizinkan |
| F4 | Membaca halaman dan mengekstrak teks yang bermakna | Judul dan teks bersih halaman tersedia untuk dijadikan dasar jawaban |
| F5 | Menyusun jawaban disertai tautan sumber | Setiap jawaban faktual memiliki tautan yang mendukung isi jawaban |
| F6 | Menyimpan memori percakapan selama satu sesi dan mencatat log pengujian | Pertanyaan lanjutan memahami konteks sebelumnya; log pengujian tercatat |

*(Sumber: Tabel 3.3)*

---

## 5. Kebutuhan Nonfungsional

| Kode | Kategori | Kebutuhan |
|---|---|---|
| NF1 | Keamanan | Akses sumber dibatasi pada domain `uir.ac.id` dan bersifat read-only |
| NF2 | Kinerja | Waktu respons berada dalam batas pada kriteria keberhasilan (rata-rata ≤ 30 detik) |
| NF3 | Keandalan | Pesan kegagalan yang jelas ditampilkan ketika situs tidak dapat diakses atau lambat; agen tidak berhenti tanpa respons |
| NF4 | Kepatuhan | Aturan `robots.txt` dan pembatasan frekuensi permintaan dipatuhi |

Kebutuhan pendukung terkait: batas jumlah langkah agen (N = 5), timeout, cache konten dengan masa berlaku terbatas, serta penolakan permintaan data pribadi atau akses terautentikasi.

*(Sumber: Tabel 3.3, Bab III.7–III.8)*

---

## 6. Arsitektur dan Komponen Sistem

```text
Pengguna
  -> Antarmuka chat (frontend/; HTML/CSS/JS, atau antarmuka Streamlit lama)
     -> web/api.py (FastAPI: sesi + guardrail adapter)
        -> app/orchestrator.py (percakapan, memori, guardrail, verifikasi, maks. 5 langkah)
           -> app/ollama_client.py (Hermes sebagai inti penalaran)
           -> tools/cari_uir.py
           -> tools/ambil_halaman.py
           -> tools/baca_cache.py (cache konten, masa berlaku terbatas)
           -> app/telemetri.py (waktu, tool, token, cache)
```

Peran komponen:

- **Antarmuka pengguna:** menerima pertanyaan dan menampilkan jawaban beserta tautan sumber.
- **Orkestrator agen:** meneruskan pertanyaan, merencanakan langkah, memilih tool, menjaga memori percakapan, menerapkan guardrails, memverifikasi jawaban, dan membatasi jumlah langkah.
- **Hermes:** inti penalaran dan pemanggilan fungsi (function calling). Laporan menetapkan varian sesuai sumber daya; implementasi saat ini menjalankan `hermes3:8b` melalui Ollama lokal (`README.md`).
- **Tool:** pencarian, pengambilan halaman, dan cache — seluruhnya read-only.
- **Penyimpanan:** basis data ringan untuk cache konten dan log percakapan pengujian.

Kebutuhan perangkat lunak dan keras: Python beserta pustaka agen/tool, modul pencarian terbatas domain dan parser HTML, basis data ringan, antarmuka chat web, komputer/server dengan sumber daya memadai untuk model serta koneksi internet stabil.

*(Sumber: Bab III.4–III.5, Tabel 3.2, `README.md`)*

---

## 7. Spesifikasi Tool

| Tool | Fungsi | Masukan dan keluaran | Batasan |
|---|---|---|---|
| `cari_uir` | Mencari halaman yang relevan dengan kebutuhan informasi | Masukan: kata kunci. Keluaran: daftar tautan beserta judul dan cuplikan | Hanya domain `uir.ac.id`; jumlah hasil dibatasi |
| `ambil_halaman` | Mengunduh halaman dan mengekstrak teks bermakna | Masukan: URL pada domain `uir.ac.id`. Keluaran: judul dan teks bersih halaman | Mematuhi `robots.txt`; ada timeout dan pembatasan frekuensi |
| `baca_cache` | Membaca dan menyimpan konten yang sering diakses | Masukan: URL atau kunci. Keluaran: konten tersimpan beserta waktu simpan | Masa berlaku cache terbatas; konten kedaluwarsa diambil ulang |

Seluruh tool bersifat read-only sehingga agen tidak dapat mengubah data apa pun pada situs.

*(Sumber: Tabel 3.4)*

---

## 8. Instruksi Sistem Agen (System Prompt)

Perilaku agen diatur melalui system prompt yang memuat:

1. Peran sebagai asisten informasi UIR yang menjawab dalam Bahasa Indonesia.
2. Lingkup pertanyaan yang boleh dijawab serta cara menolak pertanyaan di luar lingkup dengan sopan.
3. Kewajiban memakai tool sebelum menjawab pertanyaan faktual dan menyertakan tautan sumber pada setiap jawaban.
4. Larangan mengarang jawaban; jika informasi tidak ditemukan, agen menyatakannya secara jujur.
5. Isi halaman web diperlakukan sebagai **data**, bukan instruksi yang harus dipatuhi.

*(Sumber: Bab III.6.2)*

---

## 9. Alur Kerja Agen

1. Periksa apakah pertanyaan berada dalam lingkup informasi UIR. Jika tidak, tolak dengan sopan dan arahkan ke kanal resmi.
2. Jika dalam lingkup, rencanakan langkah dan panggil tool untuk mencari serta membaca halaman `uir.ac.id`.
3. Nilai apakah informasi yang diperoleh sudah cukup.
4. Jika belum cukup, ulangi proses dengan **batas maksimum N = 5 langkah** agar agen tidak berputar tanpa akhir.
5. Jika cukup, susun jawaban beserta tautan sumber dan kirimkan kepada pengguna.

Aturan keandalan:

- Agen hanya mengambil sumber dari domain `uir.ac.id`.
- Setiap jawaban faktual wajib disertai tautan sumber.
- Jika informasi tidak ditemukan, agen menyatakannya secara jujur dan tidak mengarang.
- Agen tidak memproses permintaan yang memerlukan data pribadi atau akses sistem akademik.
- Jika situs tidak dapat diakses atau lambat, agen memberikan pesan kegagalan yang jelas.

*(Sumber: Bab III.7)*

---

## 10. Guardrails, Keamanan, dan Mitigasi Risiko

Risiko dikaji mengacu pada daftar risiko aplikasi LLM OWASP Foundation (2025).

| Risiko | Dampak | Mitigasi |
|---|---|---|
| Prompt injection tidak langsung pada isi halaman | Agen mengikuti instruksi tersembunyi | Isi halaman diperlakukan sebagai data; tool read-only; instruksi sistem tidak dapat diubah oleh konten |
| Halusinasi | Jawaban keliru atau tidak bersumber | Jawaban wajib bersumber dari halaman yang dibaca; penolakan jujur jika informasi tidak ditemukan |
| Pencarian keluar domain | Sumber tidak resmi dipakai | Allowlist domain pada tool pencarian dan pengambil halaman |
| Beban berlebih pada server situs | Gangguan layanan situs | Cache, pembatasan frekuensi, kepatuhan `robots.txt` |
| Agen berputar tanpa akhir | Waktu respons panjang dan sumber daya terbuang | Batas langkah maksimum dan timeout |
| Permintaan data pribadi | Pelanggaran privasi | Permintaan ditolak; tidak ada akses ke sistem yang memerlukan autentikasi |

*(Sumber: Bab III.8, Tabel 3.5)*

---

## 11. Kebutuhan Data dan Kategori Pertanyaan Uji

- **Data primer:** daftar pertanyaan uji dari kebutuhan informasi nyata serta hasil kuesioner kegunaan responden.
- **Data sekunder:** konten halaman `uir.ac.id` sebagai sumber jawaban dan literatur ilmiah terkait chatbot, LLM, dan agentic AI.
- **Teknik pengumpulan:** studi pustaka dan observasi struktur/kategori informasi pada situs `uir.ac.id`.

| No | Kategori | Contoh pertanyaan | Jumlah |
|---|---|---|---|
| 1 | Profil universitas | Apa visi dan misi Universitas Islam Riau? | 10 |
| 2 | Fakultas dan program studi | Program studi apa saja yang tersedia pada Fakultas Teknik? | 10 |
| 3 | Penerimaan mahasiswa baru | Bagaimana alur pendaftaran mahasiswa baru? | 10 |
| 4 | Berita dan pengumuman terbaru | Apa pengumuman terbaru yang dipublikasikan universitas? | 10 |
| 5 | Layanan akademik dan kemahasiswaan | Layanan apa saja yang tersedia bagi mahasiswa? | 10 |
| 6 | Di luar lingkup / tidak tersedia di situs | Pertanyaan tentang topik di luar UIR atau informasi yang tidak dimuat situs | 10 |
| | **Total** | | **60** |

*(Sumber: Bab III.2, Tabel 3.1)*

---

## 12. Kriteria Penerimaan dan Pengujian

### 12.1 Skenario pengujian fungsional (black box)

| No | Skenario | Hasil yang diharapkan |
|---|---|---|
| 1 | Pertanyaan profil dan program studi UIR | Jawaban sesuai isi halaman `uir.ac.id` disertai tautan sumber |
| 2 | Pertanyaan penerimaan mahasiswa baru atau pengumuman | Agen mengambil halaman relevan dan menjawab informasi terkini |
| 3 | Pertanyaan yang jawabannya tidak ada di situs | Agen menyatakan informasi tidak ditemukan tanpa mengarang |
| 4 | Pertanyaan di luar lingkup UIR | Agen menolak dengan sopan dan mengarahkan ke kanal resmi |
| 5 | Pertanyaan lanjutan dalam satu percakapan | Agen memahami konteks percakapan sebelumnya |
| 6 | Situs tidak dapat diakses atau lambat | Agen memberi pesan kegagalan yang jelas dan tidak berhenti tanpa respons |
| 7 | Halaman memuat instruksi tersembunyi | Agen tetap mematuhi instruksi sistem dan tidak menjalankan instruksi dari isi halaman |

### 12.2 Metrik pengujian

- **Akurasi:** `A = (B / N) × 100%`, dengan `B` = jumlah jawaban benar, `N` = jumlah seluruh pertanyaan uji (60).
- **Ketepatan tautan sumber:** `T = (S / F) × 100%`, dengan `S` = jumlah jawaban faktual dengan tautan yang mendukung isi jawaban, `F` = jumlah seluruh jawaban faktual.
- **Waktu respons rata-rata:** `W = (w1 + w2 + ... + wN) / N` (detik).
- **Kelengkapan jawaban:** skala 0 (tidak lengkap), 1 (sebagian lengkap), 2 (lengkap).
- **Kegunaan:** kuesioner System Usability Scale (SUS) skala 1–5 diisi calon mahasiswa, mahasiswa, dosen, dan tenaga kependidikan; `SUS = 2,5 × [Σ(Ui − 1) + Σ(5 − Gj)]` dengan `Ui` = skor butir ganjil dan `Gj` = skor butir genap (Brooke, 1996).

Untuk kategori di luar lingkup atau tidak tersedia di situs, jawaban dinilai benar apabila agen menolak atau menyatakan informasi tidak ditemukan secara tepat.

### 12.3 Target awal keberhasilan

| No | Indikator | Target awal | Metode pengukuran |
|---|---|---|---|
| 1 | Akurasi jawaban | ≥ 80% | Metrik akurasi pada 60 pertanyaan uji |
| 2 | Ketepatan tautan sumber | ≥ 90% | Metrik ketepatan tautan |
| 3 | Waktu respons rata-rata | ≤ 30 detik | Metrik waktu respons |
| 4 | Skor SUS | ≥ 68 | Kuesioner SUS responden |
| 5 | Pengujian fungsional | Seluruh skenario lulus | Tabel skenario di atas |

Target dapat disesuaikan pada tahap evaluasi prototipe sesuai sumber daya yang tersedia.

*(Sumber: Bab III.10, Tabel 3.6–3.7)*

---

## 13. Tahapan Pengembangan (Prototyping)

1. **Pengumpulan kebutuhan:** identifikasi jenis informasi yang dibutuhkan dan pemetaan struktur situs `uir.ac.id`.
2. **Perancangan cepat:** arsitektur, alur agen, system prompt, dan definisi tool.
3. **Pembuatan prototipe:** agen Hermes, tool pencarian/pengambil halaman, dan antarmuka chat.
4. **Evaluasi prototipe:** uji dengan pertanyaan uji dan masukan pengguna.
5. **Perbaikan prototipe:** penyempurnaan prompt, tool, dan aturan; ulangi tahap 3–5 hingga hasil memadai.
6. **Finalisasi:** sistem akhir dan dokumentasi penggunaan.

*(Sumber: Bab III.9)*

---

## 14. Asumsi dan Ketergantungan

- Python 3.10+ dan lingkungan Ollama lokal dengan model Hermes terpasang (`hermes3:8b` pada implementasi saat ini).
- Koneksi internet stabil untuk mengakses halaman publik `uir.ac.id`.
- `robots.txt` dipatuhi sebagai syarat minimum, bukan izin pengambilan data tanpa batas (RFC 9309).
- Perubahan konten situs di luar periode pengujian tidak termasuk dalam evaluasi.
- Konfigurasi rahasia disimpan melalui environment variables; tidak ada kunci/token/password di source code.

*(Sumber: Bab I.5, Bab II.6, `README.md`, `AGENTS.md`)*

---

## 15. Pemetaan Sumber (Traceability)

| Bagian PRD | Sumber |
|---|---|
| Konteks, rumusan masalah, tujuan, manfaat, ruang lingkup | Bab I.1–I.5 |
| Pendekatan agentic, Hermes, tool use, robots.txt | Bab II.3–II.6 |
| Kebutuhan perangkat, fungsional, nonfungsional | Bab III.4, Tabel 3.2–3.3 |
| Arsitektur dan komponen | Bab III.5, `README.md` |
| Tool, system prompt, alur kerja | Bab III.6–III.7, Tabel 3.4 |
| Guardrails dan mitigasi risiko | Bab III.8, Tabel 3.5 |
| Tahapan pengembangan | Bab III.9 |
| Pengujian dan kriteria keberhasilan | Bab III.10, Tabel 3.1, 3.6–3.7 |
| Batasan implementasi dan konfigurasi | `AGENTS.md`, `README.md`, `.env.example` |
