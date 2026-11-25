# LAPORAN FINAL — Chatbot Agentic AI Informasi Internal Universitas Islam Riau

**Tanggal:** 25 November 2026
**Status:** Final — diverifikasi pada kode terbaru
**Model:** Hermes 3 8B melalui Ollama lokal (CPU-only)
**Server demo:** `http://127.0.0.1:8010`
**Sumber informasi:** hanya halaman publik pada domain `uir.ac.id`

---

## 1. Ringkasan Eksekutif

Sistem chatbot agentic untuk informasi Universitas Islam Riau telah dibangun dan
diverifikasi secara menyeluruh. Aplikasi berjalan pada port 8010 dengan model
Hermes 3 8B lokal melalui Ollama; tidak ada model cloud, tidak ada perubahan
driver/OS, dan tidak ada instalasi ulang yang dilakukan selama finalisasi.

Hasil verifikasi terbaru:

| Aspek | Hasil |
|---|---|
| Unit test Python | **268/268 lulus** |
| Regresi injeksi prompt | **5/5 lulus** |
| Browser headless (CDP) | **13/13 lulus** |
| Penanganan galat API | **8/8 aman** |
| Isolasi dua sesi konkuren | **9/9 lolos** |
| Audit 36 variasi (kode final) | **30 PASS, 0 FAIL, 0 ERROR, 0 TIMEOUT, 6 SKIPPED** |
| Audit baseline (sebelum perbaikan) | 29 PASS, 5 FAIL, 0 ERROR, 0 TIMEOUT, 2 SKIPPED |
| Performa 5 skenario final | rata **18,56 dtk**, median **13,97 dtk**, 0 panggilan model |
| Penurunan latensi | **87,1%** dari rata-rata baseline 143,83 dtk |
| Kesehatan server | `hermes3:8b`, `model_tersedia=true`, `lokal=true` |

Lima variasi yang sebelumnya menghasilkan jawaban melenceng atau tanpa sitasi
telah ditangani dengan guard fail-closed; tidak ada klaim palsu yang tersisa.
Variasi 9 (jumlah fakultas) kini berhasil dijawab secara akurat dari halaman
katalog resmi setelah bootstrap kanonik ditambahkan, dengan skor 7/7 dan sitasi
`https://uir.ac.id/program-pendidikan/`. Empat variasi lainnya kini menjadi
`bukti_tidak_cukup` secara jujur karena datanya memang tidak tersedia pada
halaman resmi yang terbaca.

**Pernyataan jujur:** sistem tidak dinyatakan 100% akurat. Beberapa pertanyaan
memang ditolak dengan penjelasan `bukti_tidak_cukup` bila halaman resmi yang
terbaca tidak memuat data yang diminta. Ini adalah keputusan keamanan, bukan
kegagalan yang disembunyikan.

---

## 2. Latar Belakang, Tujuan, dan Ruang Lingkup

Tujuan proyek adalah menyediakan chatbot yang menjawab pertanyaan tentang
Universitas Islam Riau menggunakan informasi dari situs resmi `https://uir.ac.id/`.
Ruang lingkup dan arsitektur mengikuti rancangan pada `CAPSTONE AGENT.docx` dan
`AGENTS.md` tanpa perubahan tanpa persetujuan.

Ruang lingkup:

- Hanya halaman publik pada domain `uir.ac.id`.
- Bersifat read-only; tidak mengakses sistem akademik terautentikasi.
- Tidak mengakses data pribadi.
- Jawaban factual wajib disertai tautan sumber yang benar-benar terbaca.
- Jika informasi tidak ditemukan, sistem mengakuinya.

---

## 3. Arsitektur Sistem

Arsitektur terdiri dari lima lapisan:

1. **Antarmuka web** — `frontend/` (HTML/CSS/JS), menampilkan percakapan,
   sitasi, dan panel detail performansi.
2. **Adapter API** — `web/api.py` (FastAPI), mengelola sesi, validasi request,
   endpoint `/api/chat`, `/api/health`, dan `/api/session/reset`.
3. **Orkestrator** — `app/orchestrator.py`, mengelola percakapan, memori sesi,
   guardrails, perencanaan langkah (maksimal 5), verifikasi, dan finalisasi.
4. **Inti penalaran** — `app/ollama_client.py` + `app/system_prompt.py`,
   menjalankan Hermes 3 8B secara lokal melalui Ollama.
5. **Tiga tool** — `tools/cari_uir.py`, `tools/ambil_halaman.py`,
   `tools/baca_cache.py`, dengan `database/` sebagai penyimpanan cache.

Alur utama: pertanyaan → validasi → bootstrap bukti kanonik → agen memilih tool
→ pencarian/pembacaan → verifikasi klaim → penyusunan sitasi → jawaban akhir.

---

## 4. Komponen dan Struktur Kode

| Direktori / file | Fungsi |
|---|---|
| `app/orchestrator.py` | Mesin agen, guard relevansi, verifikasi, finalisasi |
| `app/perencana.py` | Pemahaman intent (dosen, lokasi, topik) dan strategi pencarian |
| `app/ollama_client.py` | Koneksi Ollama, batas token jawaban dan tool |
| `app/system_prompt.py` | Guardrail prompt (data tidak tepercaya, anti-bocor) |
| `app/telemetri.py` | Pengukuran waktu, tool, cache, dan panggilan model |
| `app/katalog.py` | Parser deterministik daftar fakultas/program studi |
| `tools/cari_uir.py` | Pencarian halaman `uir.ac.id`, peringkat katalog vs berita |
| `tools/ambil_halaman.py` | Ambil halaman, ekstrak teks, patuhi robots.txt |
| `tools/baca_cache.py` | Cache konten dengan masa berlaku terbatas |
| `web/api.py` | Endpoint FastAPI dan validasi `session_id` |
| `frontend/` | Antarmuka chatbot custom |
| `tests/` | 268 test otomatis dan skrip uji browser |
| `bukti_qa/` (di direktori kerja sementara) | Seluruh log dan JSON hasil pengujian |

Konfigurasi rahasia memakai environment variables; `.env.example` tidak memuat
nilai rahasia.

---

## 5. Model, Konfigurasi, dan Batasan Lingkungan

- Model: `hermes3:8b`, dijalankan oleh Ollama lokal pada port 11434 (PID 15928).
- Server aplikasi: uvicorn `web.api:app` pada `127.0.0.1:8010`.
- Mode: **CPU-only**. Ini adalah keterbatasan lingkungan yang dinyatakan, bukan
  perubahan konfigurasi yang disembunyikan.
- `OLLAMA_NUM_PREDICT` membatasi panjang jawaban akhir;
  `OLLAMA_NUM_PREDICT_TOOL` membatasi token saat model memilih tool.
- `OLLAMA_TIMEOUT` mengatur batas waktu permintaan.
- Port 8001 dan 8003 tidak digunakan pada verifikasi terakhir.
- Tidak ada perubahan pada Ollama, GPU, OS, driver, atau environment sistem.

---

## 6. Alur Kerja Agen dan Batas Langkah

Agen dibatasi maksimal **5 langkah** (`MAX_LANGKAH`) untuk mencegah perulangan
tanpa akhir. Urutan tipikal:

1. Validasi pertanyaan dan penolakan topik di luar cakupan.
2. Bootstrap kanonik: baca halaman indeks resmi lebih dulu untuk topik
   berindeks (termasuk `berapa jumlah fakultas` dan `berapa program studi`).
3. Jalur cepat deterministik: bila parser bukti sudah menghasilkan jawaban
   konsisten, model tidak dipanggil, tetapi `_finalize()` tetap dijalankan.
4. Siklus model: model memilih `cari_uir`, `ambil_halaman`, atau `baca_cache`.
5. Finalisasi: verifikasi klaim, atribusi sitasi, guard relevansi, dan
   penolakan bila bukti tidak cukup.

Jalur cepat hanya aktif pada klien Ollama nyata dan tidak pernah melewati
verifikasi.

---

## 7. Tools dan Sumber Data

| Tool | Fungsi | Pembatasan |
|---|---|---|
| `cari_uir` | Cari halaman relevan pada `uir.ac.id` via WP REST + sitemap | normalisasi query, peringkat katalog, validasi domain |
| `ambil_halaman` | Ambil halaman dan ekstrak teks bermakna | cek `robots.txt`, timeout, buang elemen tersembunyi |
| `baca_cache` | Baca/simpan cache konten | masa berlaku terbatas, cache miss → ambil halaman |

Semua URL divalidasi; domain selain `uir.ac.id` ditolak. Isi halaman web
diperlakukan sebagai **data**, bukan instruksi.

---

## 8. Guardrails, Keamanan, dan Fail-Closed

Guardrail yang diterapkan:

- Prompt sistem berisi penanda **DATA TIDAK TEPERCAYA**, instruksi untuk
  mengabaikan perintah di dalam halaman, larangan membocorkan prompt, dan
  larangan mengarang fakta.
- Validasi `session_id` dengan pola `^[A-Za-z0-9_-]{16,64}$`.
- Isolasi sesi: pertanyaan lanjutan hanya diizinkan bila sesi yang sama
  memiliki jawaban UIR sebelumnya yang sah.
- Guard jawaban melenceng: menjawab "rektor" untuk "wakil rektor" ditolak;
  berita Fakultas Pertanian tidak dianggap menjawab "dekan Fakultas Teknik".
- Guard sitasi wajib: status `berhasil` tanpa sitasi terverifikasi ditolak.
- Guard angka: tanggal pada berita tidak dianggap sebagai jumlah fakultas atau
  program studi.
- Batas langkah, timeout, dan pembatasan frekuensi permintaan.

Hasil regresi injeksi prompt: **5/5 lulus** (`bukti_qa/unit_test_prompt_injection_final.txt`).

---

## 9. Verifikasi Klaim, Sitasi, dan Penolakan Jujur

Setiap jawaban factual melalui tiga pemeriksaan:

1. **Evidence** — klaim harus berasal dari teks halaman yang benar-benar
   terbaca, bukan dari cuplikan pencarian atau keluaran model.
2. **Atribusi** — URL yang dikutip harus berstatus `retrieved`, `read`,
   `evidence`, `used`, dan `cited`.
3. **Relevansi** — jawaban harus menyentuh pokok pertanyaan; bila ragu,
   sistem mengembalikan `bukti_tidak_cukup` dengan penjelasan singkat.

Contoh penolakan jujur: "Data wakil rektor belum ditemukan pada halaman resmi
UIR yang berhasil dibaca. Saya tidak akan menebak namanya."

---

## 10. Pengujian Otomatis dan Regresi

| Pengujian | Hasil | Bukti |
|---|---|---|
| Seluruh unit test Python | 268/268 OK | `bukti_qa/unit_test_final_268.txt` |
| Injeksi prompt/isi halaman | 5/5 OK | `bukti_qa/unit_test_prompt_injection_final.txt` |
| Guard relevansi & jumlah fakultas | 10 test | `tests/test_regresi_relevansi_jawaban.py` |
| Bootstrap kanonik fakultas | lolos | `tests/test_regresi_multi_source.py` |

Perintah: `python -m unittest discover -s tests -v`.

Perbaikan terakhir pada bootstrap kanonik jumlah fakultas ditambahkan setelah
terdapat bukti bahwa halaman `program-pendidikan/` hanya berstatus `retrieved`
tetapi tidak pernah `read`, sehingga model memilih berita advokat. Setelah
perbaikan, variasi 9 menghasilkan jawaban bersumber dengan skor 7/7.

Perbaikan tambahan: `_unit_fakultas` tidak lagi mengira kata "Universitas"
pada frasa "daftar fakultas Universitas Islam Riau" sebagai nama fakultas.
Sebelumnya hal ini membuat jawaban daftar fakultas yang sah berisiko ditolak
oleh guard relevansi. Test regresi baru ditambahkan untuk kasus ini.

---

## 11. Audit 36 Variasi

Audit dijalankan dengan skrip `audit_36_akhir.py` terhadap server port 8010
menggunakan Ollama dan model Hermes 3 8B yang nyata. Cakupannya adalah 12
kategori × 3 variasi = 36 pertanyaan, masing-masing memakai `session_id` baru.
Penilaian tujuh aspek bersifat heuristik dan transparan, bukan klaim akurasi
mutlak.

| Kategori audit | Jumlah | Definisi |
|---|---|---|
| PASS | **30** | status `berhasil` dan total 7/7 |
| FAIL | **0** | tidak ada jawaban `berhasil` dengan total < 7 |
| ERROR | **0** | tidak ada galat permintaan, internal, model, atau Ollama |
| TIMEOUT | **0** | tidak ada latensi ≥ 240 detik |
| SKIPPED | **6** | `bukti_tidak_cukup`, yaitu penolakan jujur |

- Total: **36/36 variasi selesai**.
- Status mentah: `berhasil` 30, `bukti_tidak_cukup` 6.
- Latensi rata-rata: **39,82 detik** (baseline 55,06 detik).
- Perbandingan baseline: PASS 29 → 30, FAIL 5 → 0, SKIPPED 2 → 6.

Perbaikan yang terverifikasi dalam audit ini:

- **Variasi 9** (jumlah fakultas): dari FAIL 4/7 menjadi **PASS 7/7**. Jawaban
  diambil dari kalimat halaman `program-pendidikan/` yang menyebut 9 fakultas,
  dengan sitasi resmi.
- **Variasi 15, 16, 17, 28**: dari FAIL menjadi **SKIPPED jujur**. Tidak ada
  lagi jawaban "wakil rektor" yang diisi nama rektor, potongan berita tanpa
  sitasi, atau klaim akreditasi tanpa tautan.
- **Variasi 8** sempat turun menjadi SKIPPED karena frasa "fakultas Universitas
  Islam Riau" salah dibaca sebagai nama fakultas. Setelah `_unit_fakultas`
  diperbaiki, variasi ini kembali **PASS 7/7**.

Enam variasi SKIPPED adalah 15 (wakil rektor), 16 (dekan Fakultas Teknik),
17 (pimpinan Fakultas Ekonomi), 28 (akreditasi UIR), 34 (pengumuman terbaru),
dan 35 (berita resmi). Semuanya menolak dengan jujur tanpa mengarang data atau
sitasi.

Bukti: `bukti_qa/audit_36_hasil_akhir.json` dan
`bukti_qa/audit_36_akhir_proses.txt`.

---

## 12. Pengujian Percakapan Lanjutan 4 Giliran

Uji dilakukan dengan skrip `uji_lanjutan_4turn.py` pada **satu `session_id`
yang sama** untuk empat giliran berturut-turut.

| Giliran | Pertanyaan | Status | Latensi |
|---|---|---|---|
| 1 | Siapa rektor UIR? | `berhasil` | 14,0 dtk |
| 2 | Apa latar belakang pendidikannya? | `bukti_tidak_cukup` | 0,0 dtk |
| 3 | Sejak kapan menjabat? | `bukti_tidak_cukup` | 0,0 dtk |
| 4 | Bagaimana dengan wakil rektor? | `bukti_tidak_cukup` | 0,0 dtk |

Penilaian akhir:

| Kriteria | Hasil |
|---|---|
| Semua giliran mendapat respons | ya |
| `session_id` sama pada keempat giliran | ya |
| Semua sitasi berdomain `uir.ac.id` | ya |
| Tidak ada klaim palsu | ya |
| Jawaban tidak kosong | ya |

Giliran 1 menghasilkan nama rektor dengan sitasi
`https://uir.ac.id/profil/pimpinan-uir/`. Giliran 2–4 ditolak secara jujur
karena data yang diminta tidak ditemukan pada bukti resmi yang dapat
diverifikasi; sistem tidak menebak latar belakang pendidikan, masa jabatan,
atau nama wakil rektor. Penolakan ini memenuhi kriteria keamanan uji, meskipun
berarti konteks sesi tidak menghasilkan jawaban tambahan untuk pertanyaan yang
tidak menyebut topik secara eksplisit.

Bukti: `bukti_qa/lanjutan_4turn_final.json` dan
`bukti_qa/lanjutan_4turn_akhir_proses.txt`.

---

## 13. Performa dan Optimasi

Pengukuran 5 skenario nyata pada kode final:

| Skenario | Latensi | Panggilan model | Langkah |
|---|---|---|---|
| 1 | 34,74 dtk | 0 | 2 |
| 2 | 9,94 dtk | 0 | 3 |
| 3 | 9,39 dtk | 0 | 3 |
| 4 | 13,97 dtk | 0 | 2 |
| 5 | 24,75 dtk | 0 | 5 |

- Rata-rata: **18,56 detik**
- Median: **13,97 detik**
- Baseline sebelum optimasi: rata-rata **143,83 detik**
- Penurunan: **87,1%**

Optimasi yang dilakukan: jalur cepat deterministik, prompt sistem ringkas,
pembatasan token tool, prioritas halaman katalog, dan bootstrap kanonik.
Bukti: `bukti_qa/uji5d_ringkasan.json` dan `uji5d_01.json`–`uji5d_05.json`.

---

## 14. Pengujian Frontend, API Error, dan Konkurensi

| Pengujian | Hasil | Bukti |
|---|---|---|
| Browser headless (CDP), 13 pemeriksaan | 13/13 lulus | `bukti_qa/cdp_frontend_kode_final.txt` |
| Penanganan galat API | 8/8 aman | `bukti_qa/uji_error_api_kode_final.txt` |
| Dua sesi konkuren | 9/9 lolos | `bukti_qa/konkuren_2sesi_session_valid2.json` |

Pemeriksaan CDP mencakup Enter/Shift+Enter, double-send, XSS, sumber jahat,
salin, reset, dua sesi, detail performansi, responsivitas 375 px, dan error
konsol/jaringan. Penanganan galat API menguji 8 kasus tanpa membocorkan
traceback. Uji konkurensi memastikan sesi A (rektor) tidak tercampur dengan
sesi B (beasiswa).

---

## 15. Keterbatasan, Status Kesiapan, dan Rekomendasi

**Keterbatasan yang dinyatakan:**

- Akurasi tidak diklaim 100%. Penilaian audit bersifat heuristik dan transparan.
- Beberapa variasi tetap `bukti_tidak_cukup` karena halaman resmi yang terbaca
  tidak memuat data yang diminta; ini adalah perilaku fail-closed yang disengaja.
- Lingkungan CPU-only membuat latensi masih dipengaruhi kompleksitas halaman
  dan kondisi jaringan UIR.
- `requirements.txt` masih mencantumkan Streamlit sebagai dependensi lama;
  antarmuka yang dipakai adalah frontend custom pada port 8010.
- Seluruh file proyek masih berstatus untracked di Git. Tidak ada commit, push,
  reset, atau perubahan riwayat Git yang dilakukan.

**Status kesiapan:** sistem siap digunakan untuk demo pada `http://127.0.0.1:8010`
dengan catatan keterbatasan di atas.

**Rekomendasi:**

- Pertimbangkan penambahan halaman kanonik resmi lain untuk topik pimpinan
  fakultas dan akreditasi agar lebih banyak variasi dapat dijawab dari bukti.
- Lakukan audit ulang berkala bila konten situs UIR berubah.
- Bersihkan dependensi lama dan lakukan version control setelah pengguna
  menyetujui perubahan Git.

---

### Daftar bukti utama

- `bukti_qa/audit_36_hasil_final.json` — audit baseline 36 variasi
- `bukti_qa/audit_36_hasil_akhir.json` — audit kode final
- `bukti_qa/perbaikan_variasi_09_15_16_17_28.json` — uji ulang variasi bermasalah
- `bukti_qa/variasi9_bootstrap_final.json` — perbaikan variasi 9 bersumber
- `bukti_qa/lanjutan_4turn_final.json` — uji percakapan 4 giliran
- `bukti_qa/unit_test_final_268.txt` — 268 unit test dengan guard sitasi
- `bukti_qa/cdp_frontend_kode_final.txt` — 13/13 browser headless
- `bukti_qa/uji_error_api_kode_final.txt` — 8/8 galat API aman
- `bukti_qa/konkuren_2sesi_session_valid2.json` — 9/9 isolasi sesi
- `bukti_qa/uji5d_ringkasan.json` — pengukuran performa 5 skenario
