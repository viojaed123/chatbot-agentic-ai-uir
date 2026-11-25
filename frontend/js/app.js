'use strict';

const MAX_PERTANYAAN = 2000;
// Pengaman sisi klien bila jaringan menggantung lebih lama dari timeout server.
const BATAS_WAKTU_KLIEN_MS = 210000;

const STATUS_LABEL = {
  berhasil: 'Jawaban terverifikasi',
  bukti_tidak_cukup: 'Bukti tidak cukup',
  sumber_tidak_tersedia: 'Sumber tidak tersedia',
  batas_langkah: 'Batas pencarian tercapai',
  batas_langkah_tercapai: 'Batas langkah agen tercapai',
  ollama_tidak_tersedia: 'Ollama rollback tidak terhubung',
  penyedia_tidak_tersedia: 'Penyedia model tidak terhubung',
  rate_limit: 'Permintaan terlalu banyak, coba lagi sebentar',
  hermes_5xx: 'Server Hermes sedang gangguan',
  kunci_tidak_tersedia: 'Kredensial penyedia belum dikonfigurasi',
  model_tidak_tersedia: 'Model belum tersedia',
  timeout: 'Permintaan melewati batas waktu',
  respons_tidak_valid: 'Respons tidak dapat dibaca',
  konfigurasi_tidak_valid: 'Konfigurasi penyedia tidak valid',
  validasi_gagal: 'Pertanyaan tidak valid',
  kesalahan_internal: 'Terjadi kesalahan internal',
};

const el = {
  sidebar: document.getElementById('sidebar'),
  menu: document.getElementById('btn-menu'),
  scrim: document.getElementById('scrim'),
  newChat: document.getElementById('btn-new'),
  start: document.getElementById('btn-start'),
  form: document.getElementById('chat-form'),
  question: document.getElementById('question'),
  send: document.getElementById('btn-send'),
  sendLabel: document.getElementById('send-label'),
  help: document.getElementById('input-help'),
  error: document.getElementById('input-error'),
  count: document.getElementById('char-count'),
  thread: document.getElementById('thread'),
  threadStart: document.getElementById('thread-start'),
  history: document.getElementById('history-list'),
  historyCount: document.getElementById('history-count'),
  statusWrap: document.getElementById('status-wrap'),
  statusValue: document.getElementById('status-value'),
  topbarModel: document.getElementById('topbar-model'),
};

const state = {
  view: 'beranda',
  sessionId: null,
  conversations: [],
  activeId: null,
  loading: false,
};

/*
 * Label tampilan diturunkan dari topik pertanyaan, bukan dari fakta.
 * Seluruh isi jawaban tetap berasal dari evidence backend.
 */
const TOPIK_JUDUL = [
  [/beasiswa|bantuan pendidikan/i, 'Informasi Beasiswa di Universitas Islam Riau'],
  [/fakultas/i, 'Fakultas di Universitas Islam Riau'],
  [/program studi|prodi|katalog/i, 'Program Studi di Universitas Islam Riau'],
  [/penerimaan|pmb|mahasiswa baru|pendaftaran/i, 'Penerimaan Mahasiswa Baru UIR'],
  [/rektor|wakil rektor|pimpinan|jabatan/i, 'Pimpinan Universitas Islam Riau'],
  [/biaya|jumlah|berapa/i, 'Ringkasan Angka di UIR'],
  [/pengumuman|berita/i, 'Berita dan Pengumuman UIR'],
  [/profil|sejarah|tentang uir|apa itu uir/i, 'Profil Universitas Islam Riau'],
];

const SUFFIKS_JUDUL = {
  prosedur: 'Langkah-langkah',
  identitas: 'Identitas dan Jabatan',
  jumlah: 'Angka Kunci',
  bandingkan: 'Perbandingan',
  definisi: 'Definisi',
  daftar: 'Daftar Informasi',
  umum: '',
};

function buatId() {
  if (window.crypto && typeof window.crypto.randomUUID === 'function') {
    return window.crypto.randomUUID();
  }
  return `sesi-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function percakapanAktif() {
  return state.conversations.find((item) => item.id === state.activeId) || null;
}

function pastikanPercakapan() {
  const ada = percakapanAktif();
  if (ada) {
    return ada;
  }
  const baru = {
    id: buatId(),
    sessionId: state.sessionId,
    judul: `Percakapan ${state.conversations.length + 1}`,
    giliran: [],
    diperbarui: new Date().toISOString(),
  };
  state.conversations.push(baru);
  state.activeId = baru.id;
  renderRiwayat();
  return baru;
}

function tutupMenu() {
  el.sidebar.classList.remove('is-open');
  el.menu.setAttribute('aria-expanded', 'false');
  el.menu.setAttribute('aria-label', 'Buka navigasi');
  el.scrim.hidden = true;
}

function bukaMenu(terbuka) {
  el.sidebar.classList.toggle('is-open', terbuka);
  el.menu.setAttribute('aria-expanded', terbuka ? 'true' : 'false');
  el.menu.setAttribute('aria-label', terbuka ? 'Tutup navigasi' : 'Buka navigasi');
  el.scrim.hidden = !terbuka;
}

function setTampilan(view) {
  state.view = view;
  ['beranda', 'percakapan', 'tentang'].forEach((nama) => {
    const bagian = document.getElementById(nama);
    if (bagian) {
      bagian.hidden = nama !== view;
    }
  });
  document.querySelectorAll('.nav-item[data-view]').forEach((tombol) => {
    const aktif = tombol.dataset.view === view;
    tombol.classList.toggle('is-active', aktif);
    if (aktif) {
      tombol.setAttribute('aria-current', 'page');
    } else {
      tombol.removeAttribute('aria-current');
    }
  });
  tutupMenu();
}

function setStatus(teks, jenis) {
  el.statusValue.textContent = teks;
  el.statusWrap.classList.toggle('status--ok', jenis === 'ok');
  el.statusWrap.classList.toggle('status--bad', jenis === 'bad');
}

async function periksaKesehatan() {
  try {
    const respons = await fetch('/api/health', { headers: { Accept: 'application/json' } });
    if (!respons.ok) {
      throw new Error('health gagal');
    }
    const data = await respons.json();
    const penyedia = ['ollama', 'tokenharbor', 'deepseek'].includes(data.penyedia)
      ? data.penyedia
      : 'deepseek';
    const namaPenyedia = penyedia === 'tokenharbor'
      ? 'TokenHarbor'
      : (penyedia === 'deepseek' ? 'DeepSeek' : 'Ollama');
    // ID model dari metadata API lebih dipercaya daripada label statis.
    const model = data.model_api || data.model
      || (penyedia === 'ollama' ? 'hermes3:8b' : 'deepseek-v4.1-flash:free');

    if (el.topbarModel) el.topbarModel.textContent = model;

    if (data.ok && data.model_tersedia) {
      setStatus('Siap', 'ok');
    } else if (data.ok) {
      setStatus('Model belum tersedia', 'bad');
    } else {
      setStatus(`${namaPenyedia} tidak terhubung`, 'bad');
    }
  } catch (error) {
    setStatus('Status tidak diketahui', 'bad');
  }
}

// Health sengaja tidak mengklaim model sebelum ada inferensi live. Setelah
// /api/chat berhasil, detail_model berasal dari status run Hermes yang nyata;
// di situlah status "Siap" boleh dinyalakan.
function perbaruiStatusDariChat(data) {
  if (!data || data.ok !== true) return;
  const rinci = data.detail_model && typeof data.detail_model === 'object'
    ? data.detail_model
    : null;
  if (!rinci || rinci.agent !== 'hermes') return;
  const model = typeof rinci.model === 'string' ? rinci.model.trim() : '';
  if (!model || String(rinci.status || '').toLowerCase() !== 'completed') return;
  if (el.topbarModel) el.topbarModel.textContent = model;
  setStatus('Siap', 'ok');
}

function setGalatInput(pesan) {
  el.error.textContent = pesan || '';
  el.error.hidden = !pesan;
  if (pesan) {
    el.question.setAttribute('aria-invalid', 'true');
    el.question.focus();
  } else {
    el.question.removeAttribute('aria-invalid');
  }
}

function perbaruiHitung() {
  el.count.textContent = `${el.question.value.length}/${MAX_PERTANYAAN}`;
}

function sesuaikanTextarea() {
  el.question.style.height = 'auto';
  el.question.style.height = `${Math.min(el.question.scrollHeight, 160)}px`;
}

function buatIkon(nama) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('width', '18');
  svg.setAttribute('height', '18');
  const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
  use.setAttribute('href', `#icon-${nama}`);
  svg.appendChild(use);
  return svg;
}

function waktuTampil(iso) {
  if (typeof iso !== 'string' || !iso) {
    return null;
  }
  const tanggal = new Date(iso);
  if (Number.isNaN(tanggal.getTime())) {
    return null;
  }
  return tanggal;
}

function tambahWaktu(wadah, iso) {
  const tanggal = waktuTampil(iso);
  if (!tanggal) {
    return;
  }
  const waktu = document.createElement('time');
  waktu.dateTime = tanggal.toISOString();
  waktu.textContent = tanggal.toLocaleTimeString('id-ID', {
    hour: '2-digit',
    minute: '2-digit',
  });
  wadah.appendChild(waktu);
}

/* ---- Perapian format jawaban: lapisan presentasi, bukan verifikasi ------ */

function jenisPertanyaan(pertanyaan) {
  const q = String(pertanyaan || '').toLowerCase();
  if (/bandingkan|perbedaan|\bvs\b|mana lebih/i.test(q)) return 'bandingkan';
  if (/siapa|jabatan|pimpinan/i.test(q)) return 'identitas';
  if (/bagaimana|cara|langkah|proses|prosedur|urutan/i.test(q)) return 'prosedur';
  if (/berapa|jumlah|total/i.test(q)) return 'jumlah';
  if (/apa saja|sebutkan|daftar|kategori|jenis/i.test(q)) return 'daftar';
  if (/apa itu|pengertian|definisi|artinya/i.test(q)) return 'definisi';
  return 'umum';
}

function topikPertanyaan(pertanyaan) {
  const q = String(pertanyaan || '');
  const cocok = TOPIK_JUDUL.find(([pola]) => pola.test(q));
  return cocok ? cocok[1] : 'Informasi Universitas Islam Riau';
}

function infoTampilan(pertanyaan, lanjutan) {
  const q = typeof pertanyaan === 'string' ? pertanyaan : '';
  const jenis = jenisPertanyaan(q);
  if (lanjutan || !q.trim()) {
    return { judul: null, jenis };
  }
  const sufiks = SUFFIKS_JUDUL[jenis] || '';
  return {
    judul: sufiks ? `${topikPertanyaan(q)} · ${sufiks}` : topikPertanyaan(q),
    jenis,
  };
}

function bersihkanEscapa(teks) {
  return String(teks || '').replace(/\\([\\`*_{}\[\]()#+\-.!>~])/g, '$1');
}

function normalisasiTeks(teks) {
  return String(teks || '')
    .replace(/\r\n?/g, '\n')
    .replace(/^(\s*\d+)\\\.\s+/gm, '$1. ')
    .trim();
}

function pisahkanSumber(teks) {
  const pat = /\n[ \t]*\n[ \t]*(Sumber(?:\s+resmi)?)[ \t]*:[ \t]*\n/i;
  const cocok = teks.match(pat);
  if (!cocok) {
    return { isi: teks, sumber: [], label: 'Sumber' };
  }
  const ekor = teks.slice(cocok.index + cocok[0].length);
  const sumber = ekor.split('\n')
    .map((baris) => baris.replace(/^\s*(?:[-*•]|\d+[.)])\s+/, '').trim())
    .filter(Boolean);
  return {
    isi: teks.slice(0, cocok.index).trim(),
    sumber,
    label: /resmi/i.test(cocok[1]) ? 'Sumber resmi' : 'Sumber',
  };
}

function urlSumberValid(nilai) {
  if (typeof nilai !== 'string' || !nilai.trim()) {
    return null;
  }
  try {
    const url = new URL(nilai.trim());
    if (url.protocol !== 'https:' || url.hostname !== 'uir.ac.id') {
      return null;
    }
    if (url.username || url.password || (url.port && url.port !== '443')) {
      return null;
    }
    return url;
  } catch (error) {
    return null;
  }
}

function buatTautan(teks, nilai, kelas) {
  const url = urlSumberValid(nilai);
  const label = typeof teks === 'string' && teks ? teks : String(nilai);
  if (!url) {
    const aman = document.createElement('span');
    aman.textContent = label;
    return aman;
  }
  const tautan = document.createElement('a');
  tautan.className = kelas || 'md-link';
  tautan.href = url.href;
  tautan.target = '_blank';
  tautan.rel = 'noopener noreferrer';
  tautan.textContent = label;
  return tautan;
}

function tambahInline(induk, teks, opsi) {
  const sumber = String(teks || '');
  const aturan = [
    { re: /`([^`\n]+)`/g, jenis: 'code' },
    { re: /\*\*([^*\n]+)\*\*/g, jenis: 'bold' },
    { re: /\*([^*\n]+)\*/g, jenis: 'italic' },
    { re: /_([^_\n]+)_/g, jenis: 'italic' },
    { re: /\[([^\]\n]+)\]\(([^)\s]+)\)/g, jenis: 'link' },
    { re: /\(?\bSumber:\s*(https?:\/\/[^\s)]+)\)?/gi, jenis: 'sumber' },
    { re: /https?:\/\/[^\s)]+/g, jenis: 'url' },
  ];
  if (opsi && opsi.tonjolkanAngka) {
    aturan.push({ re: /\b\d+(?:[.,]\d+)?\b/g, jenis: 'angka' });
  }

  let posisi = 0;
  while (posisi < sumber.length) {
    let terbaik = null;
    aturan.forEach((item, indeks) => {
      item.re.lastIndex = posisi;
      const cocok = item.re.exec(sumber);
      if (!cocok) {
        return;
      }
      if (!terbaik || cocok.index < terbaik.cocok.index
          || (cocok.index === terbaik.cocok.index && indeks < terbaik.indeks)) {
        terbaik = { cocok, item, indeks };
      }
    });

    if (!terbaik) {
      induk.appendChild(document.createTextNode(bersihkanEscapa(sumber.slice(posisi))));
      break;
    }

    const { cocok, item } = terbaik;
    const adaEscapa = cocok.index > 0 && sumber[cocok.index - 1] === '\\';
    const batas = adaEscapa ? cocok.index - 1 : cocok.index;
    if (batas > posisi) {
      induk.appendChild(document.createTextNode(bersihkanEscapa(sumber.slice(posisi, batas))));
    }
    if (adaEscapa) {
      induk.appendChild(document.createTextNode(cocok[0]));
      posisi = cocok.index + cocok[0].length;
      continue;
    }
    posisi = cocok.index + cocok[0].length;

    if (item.jenis === 'code') {
      const kode = document.createElement('code');
      kode.className = 'md-code-inline';
      kode.textContent = cocok[1];
      induk.appendChild(kode);
    } else if (item.jenis === 'bold' || item.jenis === 'italic') {
      const penanda = document.createElement(item.jenis === 'bold' ? 'strong' : 'em');
      tambahInline(penanda, cocok[1], {});
      induk.appendChild(penanda);
    } else if (item.jenis === 'link') {
      const url = urlSumberValid(cocok[2]);
      if (!url) {
        const teksPolos = document.createElement('span');
        teksPolos.textContent = `[${cocok[1]}](${cocok[2]})`;
        induk.appendChild(teksPolos);
      } else {
        const tautan = document.createElement('a');
        tautan.className = 'md-link';
        tautan.href = url.href;
        tautan.target = '_blank';
        tautan.rel = 'noopener noreferrer';
        tambahInline(tautan, cocok[1], {});
        induk.appendChild(tautan);
      }
    } else if (item.jenis === 'sumber') {
      induk.appendChild(document.createTextNode('Sumber: '));
      induk.appendChild(buatTautan(
        `${urlSumberValid(cocok[1]) ? urlSumberValid(cocok[1]).hostname : 'uir.ac.id'}${urlSumberValid(cocok[1]) ? urlSumberValid(cocok[1]).pathname : ''}`,
        cocok[1],
        'md-link',
      ));
    } else if (item.jenis === 'url') {
      const url = urlSumberValid(cocok[0]);
      induk.appendChild(buatTautan(
        url ? `${url.hostname}${url.pathname}${url.search}` : cocok[0],
        cocok[0],
        'md-link',
      ));
    } else if (item.jenis === 'angka') {
      const angka = document.createElement('strong');
      angka.className = 'md-figure';
      angka.textContent = cocok[0];
      induk.appendChild(angka);
    }
  }
}

function adalahPembukaBlok(baris) {
  const teks = String(baris || '');
  const potong = teks.trim();
  return !potong
    || /^```/.test(potong)
    || /^#{1,6}\s/.test(potong)
    || /^>/.test(potong)
    || /^\s*(?:[-*•]|\d+[.)])\s+/.test(teks)
    || /^\s*(?:-{3,}|\*{3,}|_{3,})\s*$/.test(potong);
}

function adaTabel(baris, berikutnya) {
  if (typeof baris !== 'string' || !baris.includes('|') || typeof berikutnya !== 'string') {
    return false;
  }
  return /^\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)+\|?\s*$/.test(berikutnya);
}

function selTable(baris) {
  return String(baris || '').trim().replace(/^\||\|$/g, '').split('|').map((item) => item.trim());
}

function buatLista(induk, tipe, indent, tumpukan) {
  const daftar = document.createElement(tipe);
  daftar.className = 'md-list';
  induk.appendChild(daftar);
  const entri = { indent, el: daftar, tipe, li: null };
  tumpukan.push(entri);
  return entri;
}

function renderDaftar(wadah, baris, mulai, opsi) {
  const tumpukan = [];
  let i = mulai;
  while (i < baris.length) {
    const bar = baris[i];
    if (!bar || !bar.trim()) {
      const berikut = baris[i + 1];
      if (!berikut || !/^\s*(?:[-*•]|\d+[.)])\s+/.test(berikut)) {
        break;
      }
      i += 1;
      continue;
    }
    const cocok = bar.match(/^(\s*)(?:(\d+)[.)]|([-*•]))\s+(.+)$/);
    if (!cocok) {
      break;
    }
    const indent = cocok[1].replace(/\t/g, '  ').length;
    const tipe = cocok[2] ? 'ol' : 'ul';
    while (tumpukan.length && tumpukan[tumpukan.length - 1].indent >= indent) {
      tumpukan.pop();
    }
    let entri = tumpukan[tumpukan.length - 1];
    if (!entri || entri.tipe !== tipe) {
      entri = buatLista(entri && entri.li ? entri.li : wadah, tipe, indent, tumpukan);
    }
    const butir = document.createElement('li');
    if (cocok[2]) {
      butir.value = Number(cocok[2]);
    }
    tambahInline(butir, cocok[4], opsi);
    entri.el.appendChild(butir);
    entri.li = butir;
    i += 1;
  }
  return i;
}

function renderBlok(wadah, teks, opsi) {
  const baris = String(teks || '').split('\n');
  let i = 0;
  while (i < baris.length) {
    const bar = baris[i];
    if (!bar || !bar.trim()) {
      i += 1;
      continue;
    }
    const potong = bar.trim();

    if (/^```/.test(potong)) {
      const bahasa = potong.slice(3).trim();
      const isi = [];
      i += 1;
      while (i < baris.length && !/^```/.test(baris[i].trim())) {
        isi.push(baris[i]);
        i += 1;
      }
      if (i < baris.length) {
        i += 1;
      }
      const wadahKode = document.createElement('pre');
      wadahKode.className = 'md-code';
      const kode = document.createElement('code');
      kode.textContent = isi.join('\n');
      if (bahasa) {
        kode.dataset.bahasa = bahasa;
      }
      wadahKode.appendChild(kode);
      wadah.appendChild(wadahKode);
      continue;
    }

    const heading = potong.match(/^(#{1,6})\s+(.+)$/);
    if (heading) {
      const tingkat = Math.min(heading[1].length + 2, 5);
      const judul = document.createElement(`h${tingkat}`);
      judul.className = 'md-h';
      tambahInline(judul, heading[2], {});
      wadah.appendChild(judul);
      i += 1;
      continue;
    }

    if (adaTabel(bar, baris[i + 1])) {
      const kepala = selTable(bar);
      i += 2;
      const badan = [];
      while (i < baris.length && baris[i].trim() && baris[i].includes('|')) {
        badan.push(selTable(baris[i]));
        i += 1;
      }
      const wadahTabel = document.createElement('div');
      wadahTabel.className = 'md-table-scroll';
      const tabel = document.createElement('table');
      tabel.className = 'md-table';
      const thead = document.createElement('thead');
      const barisKepala = document.createElement('tr');
      kepala.forEach((item) => {
        const sel = document.createElement('th');
        tambahInline(sel, item, {});
        barisKepala.appendChild(sel);
      });
      thead.appendChild(barisKepala);
      tabel.appendChild(thead);
      const tbody = document.createElement('tbody');
      badan.forEach((barisBadan) => {
        const tr = document.createElement('tr');
        barisBadan.forEach((item) => {
          const sel = document.createElement('td');
          tambahInline(sel, item, {});
          tr.appendChild(sel);
        });
        tbody.appendChild(tr);
      });
      tabel.appendChild(tbody);
      wadahTabel.appendChild(tabel);
      wadah.appendChild(wadahTabel);
      continue;
    }

    if (/^\s*>/.test(bar)) {
      const kutipan = [];
      while (i < baris.length && /^\s*>/.test(baris[i])) {
        kutipan.push(baris[i].replace(/^\s*>\s?/, ''));
        i += 1;
      }
      const petikan = document.createElement('blockquote');
      petikan.className = 'md-quote';
      kutipan.forEach((item) => {
        const paragraf = document.createElement('p');
        tambahInline(paragraf, item, opsi);
        petikan.appendChild(paragraf);
      });
      wadah.appendChild(petikan);
      continue;
    }

    if (/^\s*(?:-{3,}|\*{3,}|_{3,})\s*$/.test(potong)) {
      const garis = document.createElement('hr');
      garis.className = 'md-hr';
      wadah.appendChild(garis);
      i += 1;
      continue;
    }

    if (/^\s*(?:[-*•]|\d+[.)])\s+/.test(bar)) {
      i = renderDaftar(wadah, baris, i, opsi);
      continue;
    }

    const bagian = [];
    while (i < baris.length && baris[i].trim() && !adalahPembukaBlok(baris[i])
           && !adaTabel(baris[i], baris[i + 1])) {
      bagian.push(baris[i].trim());
      i += 1;
    }
    if (!bagian.length) {
      i += 1;
      continue;
    }
    const paragraf = document.createElement('p');
    if (bagian.length === 1 && /^(Berdasarkan|Catatan:)/i.test(bagian[0])) {
      paragraf.className = 'md-lead';
    }
    tambahInline(paragraf, bagian.join(' '), opsi);
    wadah.appendChild(paragraf);
  }
}

function renderDaftarSumber(wadah, sumber, label) {
  if (!Array.isArray(sumber) || !sumber.length) {
    return;
  }
  const bagian = document.createElement('section');
  bagian.className = 'md-sources';
  const judul = document.createElement('h4');
  judul.textContent = label || 'Sumber';
  bagian.appendChild(judul);
  const daftar = document.createElement('ul');
  daftar.className = 'md-source-list';
  sumber.forEach((nilai) => {
    const url = urlSumberValid(nilai);
    if (!url) {
      return;
    }
    const butir = document.createElement('li');
    butir.appendChild(buatTautan(
      `${url.hostname}${url.pathname}${url.search}`,
      url.href,
      'md-source-link',
    ));
    daftar.appendChild(butir);
  });
  if (daftar.children.length) {
    bagian.appendChild(daftar);
    wadah.appendChild(bagian);
  }
}

function renderPotongan(wadah, teks, opsi) {
  const normal = normalisasiTeks(teks);
  if (!normal) {
    const paragraf = document.createElement('p');
    paragraf.textContent = 'Jawaban tidak tersedia.';
    wadah.appendChild(paragraf);
    return;
  }

  const info = infoTampilan(opsi && opsi.pertanyaan, Boolean(opsi && opsi.lanjutan));
  if (info.judul) {
    const judul = document.createElement('h3');
    judul.className = 'md-title';
    judul.textContent = info.judul;
    wadah.appendChild(judul);
  }

  const { isi, sumber, label } = pisahkanSumber(normal);
  renderBlok(wadah, isi || normal, { tonjolkanAngka: info.jenis === 'jumlah' });
  renderDaftarSumber(wadah, sumber, label);
}

/* ---- Kartu sumber, salin, dan percakapan -------------------------------- */

function tambahSumber(wadah, sumber, ditolak, gagal) {
  const judul = document.createElement('h3');
  judul.className = 'answer__rail-title';
  // Pada jawaban gagal/timeout, halaman yang sempat dibaca ditampilkan sebagai
  // bukti yang ditemukan, BUKAN sebagai tanda jawaban sudah selesai.
  judul.textContent = gagal
    ? 'Halaman ditemukan (belum diverifikasi untuk jawaban)'
    : 'Sumber terverifikasi';
  wadah.appendChild(judul);

  const daftarSumber = Array.isArray(sumber) ? sumber : [];
  const daftar = document.createElement('ul');
  daftar.className = 'source-list';

  daftarSumber.forEach((item) => {
    const url = urlSumberValid(item && item.url);
    if (!url) {
      return;
    }
    const itemDaftar = document.createElement('li');
    const tautan = document.createElement('a');
    tautan.href = url.href;
    tautan.target = '_blank';
    tautan.rel = 'noopener noreferrer';

    const lencana = document.createElement('span');
    lencana.className = 'source__badge';
    lencana.textContent = gagal ? 'Ditemukan' : 'Terverifikasi';

    const judulSumber = document.createElement('span');
    judulSumber.className = 'source__title';
    judulSumber.textContent = typeof item.judul === 'string' && item.judul.trim()
      ? item.judul.trim()
      : 'Halaman UIR';

    const meta = document.createElement('span');
    meta.className = 'source__meta';
    const ikon = buatIkon('link');
    const domain = document.createElement('span');
    domain.textContent = url.hostname;
    meta.append(ikon, domain);

    const alamat = document.createElement('span');
    alamat.className = 'source__url';
    alamat.textContent = `${url.pathname}${url.search}`;

    tautan.append(lencana, judulSumber, meta, alamat);
    itemDaftar.appendChild(tautan);
    daftar.appendChild(itemDaftar);
  });

  if (daftar.children.length) {
    wadah.appendChild(daftar);
    return;
  }

  const kosong = document.createElement('p');
  kosong.className = 'source__none';
  kosong.textContent = ditolak
    ? 'Tidak ada sumber terverifikasi untuk jawaban ini.'
    : 'Sumber tidak tersedia.';
  wadah.appendChild(kosong);
}

async function salinTeks(teks) {
  if (navigator.clipboard && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(teks);
      return true;
    } catch (error) {
      // Izin clipboard bisa ditolak. Jangan langsung menyerah: pakai fallback.
    }
  }
  const sementara = document.createElement('textarea');
  sementara.value = teks;
  sementara.setAttribute('readonly', '');
  sementara.style.position = 'fixed';
  sementara.style.top = '-1000px';
  document.body.appendChild(sementara);
  sementara.select();
  const berhasil = document.execCommand('copy');
  sementara.remove();
  return berhasil;
}

function buatTombolCobaLagi(pertanyaan) {
  const tombol = document.createElement('button');
  tombol.type = 'button';
  tombol.className = 'retry-btn';
  tombol.textContent = 'Coba lagi';
  tombol.setAttribute('aria-label', 'Kirim ulang pertanyaan yang gagal');
  tombol.addEventListener('click', () => kirimPertanyaan(pertanyaan));
  return tombol;
}

function buatTombolSalin(teks) {
  const tombol = document.createElement('button');
  tombol.type = 'button';
  tombol.className = 'copy-btn';
  const ikon = buatIkon('copy');
  const label = document.createElement('span');
  label.textContent = 'Salin';
  tombol.append(ikon, label);

  let pengaturWaktu = null;
  tombol.addEventListener('click', async () => {
    if (pengaturWaktu) {
      window.clearTimeout(pengaturWaktu);
    }
    try {
      const berhasil = await salinTeks(teks);
      label.textContent = berhasil ? 'Tersalin' : 'Gagal menyalin';
      tombol.replaceChild(buatIkon(berhasil ? 'check' : 'copy'), tombol.firstChild);
    } catch (error) {
      label.textContent = 'Gagal menyalin';
      tombol.replaceChild(buatIkon('copy'), tombol.firstChild);
    }
    pengaturWaktu = window.setTimeout(() => {
      label.textContent = 'Salin';
      tombol.replaceChild(buatIkon('copy'), tombol.firstChild);
    }, 1800);
  });
  return tombol;
}

function renderPesanUser(giliran) {
  const wadah = document.createElement('div');
  wadah.className = 'msg msg--user';
  wadah.dataset.turnId = giliran.id;
  const gelembung = document.createElement('div');
  gelembung.className = 'bubble-user';
  gelembung.textContent = giliran.teks;
  const meta = document.createElement('div');
  meta.className = 'msg__meta';
  tambahWaktu(meta, giliran.waktu);
  wadah.append(gelembung, meta);
  return wadah;
}

function renderPesanAsisten(giliran) {
  const ditolak = giliran.status === 'bukti_tidak_cukup'
    || giliran.status === 'sumber_tidak_tersedia';
  const gagal = !ditolak && giliran.ok === false;
  const teks = typeof giliran.teks === 'string' && giliran.teks.trim()
    ? giliran.teks
    : (giliran.pesan || 'Jawaban tidak tersedia.');

  const wadah = document.createElement('div');
  wadah.className = 'msg msg--assistant';
  wadah.dataset.turnId = giliran.id;

  const kartu = document.createElement('article');
  kartu.className = 'answer';
  if (ditolak) {
    kartu.classList.add('answer--rejected');
  }
  if (gagal) {
    kartu.classList.add('answer--error');
  }

  const isi = document.createElement('div');
  isi.className = 'answer__body';
  const kepala = document.createElement('div');
  kepala.className = 'answer__head';
  const status = document.createElement('span');
  status.className = 'answer__status';
  status.textContent = STATUS_LABEL[giliran.status] || 'Jawaban tidak tersedia';
  const metaWaktu = document.createElement('span');
  metaWaktu.className = 'answer__meta';
  const tanggal = waktuTampil(giliran.waktu);
  if (tanggal) {
    metaWaktu.textContent = tanggal.toLocaleTimeString('id-ID', {
      hour: '2-digit',
      minute: '2-digit',
    });
  }
  kepala.append(status, metaWaktu);

  const prosa = document.createElement('div');
  prosa.className = 'answer__prose';
  renderPotongan(prosa, teks, {
    pertanyaan: giliran.pertanyaan,
    lanjutan: giliran.lanjutan,
  });
  isi.append(kepala, prosa);

  const rel = document.createElement('div');
  rel.className = 'answer__rail';
  tambahSumber(rel, giliran.sumber, ditolak || gagal, gagal);

  kartu.append(isi, rel);
  wadah.appendChild(kartu);

  const meta = document.createElement('div');
  meta.className = 'msg__meta';
  meta.appendChild(buatTombolSalin(teks));
  if (gagal && giliran.pertanyaan) {
    meta.appendChild(buatTombolCobaLagi(giliran.pertanyaan));
  }
  if (Number.isFinite(giliran.langkah)) {
    const langkah = document.createElement('span');
    langkah.textContent = `${giliran.langkah} langkah`;
    meta.appendChild(langkah);
  }

  if (giliran.performansi && typeof giliran.performansi === 'object') {
    meta.appendChild(buatDetailPerformansi(giliran.performansi));
  }

  wadah.appendChild(meta);
  return wadah;
}

function fmtDetik(nilai) {
  return Number.isFinite(Number(nilai)) ? Number(nilai).toFixed(1) : '0,0';
}

function buatDetailPerformansi(perf) {
  const rinci = document.createElement('details');
  rinci.className = 'answer__perf';

  const ringkas = document.createElement('summary');
  ringkas.textContent = `Detail proses · ${fmtDetik(perf.total_detik)} dtk`;
  rinci.appendChild(ringkas);

  const isi = document.createElement('div');
  isi.className = 'answer__perf-body';

  const baris = [
    `Total: ${fmtDetik(perf.total_detik)} detik`,
    `Panggilan model: ${Number(perf.panggilan_model) || 0}`,
    `Langkah agen: ${Number(perf.langkah) || 0}`,
  ];

  if (perf.cache && typeof perf.cache === 'object') {
    baris.push(`Cache: ${Number(perf.cache.hit) || 0} hit / ${Number(perf.cache.miss) || 0} miss`);
  }

  if (Array.isArray(perf.paling_lambat)) {
    perf.paling_lambat.forEach((tahap) => {
      if (!tahap || !tahap.nama) return;
      baris.push(`${tahap.nama}: ${fmtDetik(tahap.total_detik)} detik (${Number(tahap.jumlah) || 0}x)`);
    });
  }

  if (perf.token && typeof perf.token === 'object') {
    baris.push(`Token: ${Number(perf.token.total) || 0} (prompt ${Number(perf.token.prompt) || 0}, jawaban ${Number(perf.token.completion) || 0})`);
  }

  // Bukti model aktual berasal dari field "model" pada respons API penyedia.
  if (perf.api && typeof perf.api === 'object') {
    baris.push(`Penyedia API: ${perf.api.penyedia || '-'}`);
    baris.push(`Model API: ${perf.api.model || '-'} (${Number(perf.api.panggilan) || 0} panggilan)`);
    if (Number.isFinite(Number(perf.api.biaya_estimasi_usd))) {
      baris.push(`Estimasi biaya: $${Number(perf.api.biaya_estimasi_usd).toFixed(6)}`);
    }
  }

  baris.forEach((teks) => {
    const elemen = document.createElement('div');
    elemen.textContent = teks;
    isi.appendChild(elemen);
  });

  rinci.appendChild(isi);
  return rinci;
}

function gulirBawah() {
  window.requestAnimationFrame(() => {
    // Konten chat menggulir secara alami pada dokumen. Fallback ini menjaga
    // perilaku lama bila thread masih menjadi kontainer scroll tersendiri.
    if (el.thread.scrollHeight > el.thread.clientHeight + 1) {
      el.thread.scrollTop = el.thread.scrollHeight;
      return;
    }
    window.scrollTo({ top: document.documentElement.scrollHeight, behavior: 'auto' });
  });
}

function renderPercakapan() {
  const aktif = percakapanAktif();
  if (!aktif || !aktif.giliran.length) {
    el.thread.replaceChildren(el.threadStart);
    return;
  }

  // Jangan membangun ulang seluruh role="log": pembaca layar hanya perlu
  // mendengar giliran baru, bukan seluruh riwayat setiap kali ada jawaban.
  if (el.thread.contains(el.threadStart)) {
    el.thread.replaceChildren();
  }
  const tampil = new Set(
    Array.from(el.thread.querySelectorAll('[data-turn-id]'))
      .map((node) => node.dataset.turnId),
  );
  const diinginkan = new Set(aktif.giliran.map((giliran) => giliran.id));
  el.thread.querySelectorAll('[data-turn-id]').forEach((node) => {
    if (!diinginkan.has(node.dataset.turnId)) {
      node.remove();
    }
  });
  aktif.giliran.forEach((giliran) => {
    if (tampil.has(giliran.id)) {
      return;
    }
    el.thread.appendChild(
      giliran.role === 'user' ? renderPesanUser(giliran) : renderPesanAsisten(giliran),
    );
  });
  gulirBawah();
}

function renderRiwayat() {
  el.history.replaceChildren();
  el.historyCount.textContent = String(state.conversations.length);

  if (!state.conversations.length) {
    const kosong = document.createElement('li');
    kosong.className = 'history-empty';
    kosong.textContent = 'Belum ada percakapan.';
    el.history.appendChild(kosong);
    return;
  }

  state.conversations.forEach((percakapan) => {
    const baris = document.createElement('li');
    baris.className = 'history-row';

    const buka = document.createElement('button');
    buka.type = 'button';
    buka.className = 'history__open';
    if (percakapan.id === state.activeId) {
      buka.classList.add('is-active');
    }
    const judul = document.createElement('span');
    judul.className = 'history__title';
    judul.textContent = percakapan.judul;
    const waktu = document.createElement('span');
    waktu.className = 'history__time';
    const tanggal = waktuTampil(percakapan.diperbarui);
    waktu.textContent = tanggal
      ? tanggal.toLocaleDateString('id-ID', { day: 'numeric', month: 'short' })
      : 'Sesi ini';
    buka.append(judul, waktu);
    buka.addEventListener('click', () => pilihPercakapan(percakapan.id));

    const hapus = document.createElement('button');
    hapus.type = 'button';
    hapus.className = 'history__delete';
    hapus.setAttribute('aria-label', `Hapus ${percakapan.judul}`);
    const ikon = buatIkon('trash');
    ikon.setAttribute('aria-hidden', 'true');
    hapus.appendChild(ikon);
    let pengaturWaktu = null;
    hapus.addEventListener('click', async () => {
      if (hapus.dataset.confirm !== 'true') {
        hapus.dataset.confirm = 'true';
        hapus.textContent = 'Yakin?';
        if (pengaturWaktu) {
          window.clearTimeout(pengaturWaktu);
        }
        pengaturWaktu = window.setTimeout(() => {
          delete hapus.dataset.confirm;
          hapus.replaceChildren(buatIkon('trash'));
        }, 3000);
        return;
      }
      await hapusPercakapan(percakapan.id);
    });

    baris.append(buka, hapus);
    el.history.appendChild(baris);
  });
}

function tambahGiliran(giliran) {
  const aktif = pastikanPercakapan();
  if (!giliran.id) {
    giliran.id = buatId();
  }
  aktif.giliran.push(giliran);
  aktif.diperbarui = giliran.waktu || new Date().toISOString();
  if (giliran.role === 'user'
      && aktif.giliran.filter((item) => item.role === 'user').length === 1) {
    aktif.judul = giliran.teks.length > 48
      ? `${giliran.teks.slice(0, 47)}…`
      : giliran.teks;
  }
  renderRiwayat();
  renderPercakapan();
}

function percakapanBaru() {
  const aktif = percakapanAktif();
  if (aktif && !aktif.giliran.length) {
    setTampilan('percakapan');
    el.question.focus();
    return;
  }
  const baru = {
    id: buatId(),
    sessionId: null,
    judul: `Percakapan ${state.conversations.length + 1}`,
    giliran: [],
    diperbarui: new Date().toISOString(),
  };
  state.conversations.push(baru);
  state.activeId = baru.id;
  state.sessionId = null;
  setGalatInput('');
  setTampilan('percakapan');
  renderRiwayat();
  renderPercakapan();
  el.question.focus();
}

function pilihPercakapan(id) {
  const dipilih = state.conversations.find((item) => item.id === id);
  if (!dipilih) {
    return;
  }
  state.activeId = id;
  state.sessionId = dipilih.sessionId;
  setGalatInput('');
  setTampilan('percakapan');
  renderRiwayat();
  renderPercakapan();
  el.question.focus();
}

async function hapusPercakapan(id) {
  const target = state.conversations.find((item) => item.id === id);
  if (!target) {
    return;
  }
  let sessionBaru = null;
  if (target.sessionId) {
    try {
      const respons = await fetch('/api/session/reset', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: target.sessionId }),
      });
      if (respons.ok) {
        const data = await respons.json();
        sessionBaru = data.session_id || null;
      }
    } catch (error) {
      sessionBaru = null;
    }
  }

  state.conversations = state.conversations.filter((item) => item.id !== id);
  if (state.activeId === id) {
    state.activeId = null;
    state.sessionId = null;
    if (sessionBaru) {
      const baru = {
        id: buatId(),
        sessionId: sessionBaru,
        judul: `Percakapan ${state.conversations.length + 1}`,
        giliran: [],
        diperbarui: new Date().toISOString(),
      };
      state.conversations.push(baru);
      state.activeId = baru.id;
      state.sessionId = sessionBaru;
    }
  }
  renderRiwayat();
  renderPercakapan();
}

function setMemproses(aktif) {
  state.loading = aktif;
  el.send.disabled = aktif;
  el.send.setAttribute('aria-busy', aktif ? 'true' : 'false');
  el.form.setAttribute('aria-busy', aktif ? 'true' : 'false');
  el.sendLabel.textContent = aktif ? 'Memproses…' : 'Kirim';
}

function buatLoading() {
  const wadah = document.createElement('div');
  wadah.className = 'loading msg msg--assistant';
  wadah.setAttribute('role', 'status');
  const spinner = document.createElement('span');
  spinner.className = 'spinner';
  spinner.setAttribute('aria-hidden', 'true');
  const teks = document.createElement('span');
  teks.textContent = 'Mencari → membaca → memverifikasi → menyusun jawaban…';
  const elapsed = document.createElement('span');
  elapsed.className = 'loading__elapsed';
  elapsed.textContent = '0,0 dtk';
  wadah.append(spinner, teks, elapsed);
  return wadah;
}

async function kirimPertanyaan(nilai) {
  if (state.loading) {
    return;
  }
  const pertanyaan = typeof nilai === 'string' ? nilai.trim() : '';
  if (!pertanyaan) {
    setGalatInput('Pertanyaan tidak boleh kosong.');
    return;
  }
  if (pertanyaan.length > MAX_PERTANYAAN) {
    setGalatInput(`Pertanyaan terlalu panjang. Maksimal ${MAX_PERTANYAAN} karakter.`);
    return;
  }

  setGalatInput('');
  const aktif = pastikanPercakapan();
  const lanjutan = aktif.giliran.some((item) => item.role === 'assistant');
  tambahGiliran({
    role: 'user',
    teks: pertanyaan,
    waktu: new Date().toISOString(),
  });

  const indikator = buatLoading();
  el.thread.appendChild(indikator);
  gulirBawah();
  setMemproses(true);

  const waktuMulai = Date.now();
  const pengaturWaktu = window.setInterval(() => {
    const penanda = indikator.querySelector('.loading__elapsed');
    if (penanda) {
      penanda.textContent = `${((Date.now() - waktuMulai) / 1000).toFixed(1)} dtk`;
    }
  }, 100);

  const endpoint = el.form.dataset.endpoint || '/api/chat';
  const pengendali = new AbortController();
  const batasKlien = window.setTimeout(
    () => pengendali.abort(),
    BATAS_WAKTU_KLIEN_MS,
  );

  try {
    const respons = await fetch(endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      signal: pengendali.signal,
      body: JSON.stringify({
        session_id: state.sessionId,
        question: pertanyaan,
      }),
    });
    const data = await respons.json().catch(() => ({}));
    if (!respons.ok) {
      throw new Error(data.pesan || 'Permintaan gagal. Silakan coba lagi.');
    }

    if (data.session_id) {
      state.sessionId = data.session_id;
      aktif.sessionId = data.session_id;
    }
    perbaruiStatusDariChat(data);
    // Kolom dikosongkan hanya setelah jawaban benar-benar berhasil. Bila
    // server mengembalikan ok:false (misalnya timeout), teks pertanyaan tetap
    // ada supaya pengguna bisa langsung mencoba lagi.
    if (data.ok !== false) {
      el.question.value = '';
      sesuaikanTextarea();
      perbaruiHitung();
    }
    tambahGiliran({
      role: 'assistant',
      teks: typeof data.jawaban === 'string' ? data.jawaban : '',
      pesan: typeof data.pesan === 'string' ? data.pesan : '',
      status: typeof data.status === 'string' ? data.status : '',
      sumber: Array.isArray(data.sumber) ? data.sumber : [],
      langkah: Number.isFinite(data.langkah) ? data.langkah : undefined,
      ok: Boolean(data.ok),
      waktu: typeof data.waktu === 'string' ? data.waktu : new Date().toISOString(),
      performansi: data.performansi && typeof data.performansi === 'object'
        ? data.performansi
        : undefined,
      pertanyaan,
      lanjutan,
    });
  } catch (error) {
    const aborted = error && error.name === 'AbortError';
    tambahGiliran({
      role: 'assistant',
      teks: '',
      pesan: aborted
        ? 'Permintaan melewati batas waktu pada sisi klien. '
          + 'Jawaban belum selesai. Silakan coba lagi.'
        : (error.message || 'Permintaan gagal. Silakan coba lagi.'),
      status: aborted ? 'timeout' : 'kesalahan_internal',
      sumber: [],
      ok: false,
      waktu: new Date().toISOString(),
      pertanyaan,
      lanjutan,
    });
  } finally {
    window.clearTimeout(batasKlien);
    window.clearInterval(pengaturWaktu);
    indikator.remove();
    setMemproses(false);
    renderPercakapan();
    el.question.focus();
  }
}

function pasangAcara() {
  document.querySelectorAll('[data-view]').forEach((tombol) => {
    tombol.addEventListener('click', () => setTampilan(tombol.dataset.view));
  });

  document.querySelectorAll('[data-question]').forEach((tombol) => {
    tombol.addEventListener('click', () => {
      const pertanyaan = tombol.dataset.question || '';
      el.question.value = pertanyaan;
      sesuaikanTextarea();
      perbaruiHitung();
      setTampilan('percakapan');
      kirimPertanyaan(pertanyaan);
    });
  });

  el.form.addEventListener('submit', (acara) => {
    acara.preventDefault();
    kirimPertanyaan(el.question.value);
  });

  el.question.addEventListener('keydown', (acara) => {
    if (acara.key === 'Enter' && !acara.shiftKey && !acara.isComposing) {
      acara.preventDefault();
      el.form.requestSubmit();
    }
  });

  el.question.addEventListener('input', () => {
    sesuaikanTextarea();
    perbaruiHitung();
    if (el.question.getAttribute('aria-invalid') === 'true') {
      setGalatInput('');
    }
  });

  el.newChat.addEventListener('click', percakapanBaru);
  el.start.addEventListener('click', () => {
    setTampilan('percakapan');
    el.question.focus();
  });
  el.menu.addEventListener('click', () => bukaMenu(!el.sidebar.classList.contains('is-open')));
  el.scrim.addEventListener('click', () => bukaMenu(false));
  document.addEventListener('keydown', (acara) => {
    if (acara.key === 'Escape') {
      bukaMenu(false);
    }
  });
}

pasangAcara();
renderRiwayat();
renderPercakapan();
perbaruiHitung();
setTampilan('beranda');
periksaKesehatan();
