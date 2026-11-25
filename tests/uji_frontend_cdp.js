/* Uji frontend headless lewat Chrome DevTools Protocol, tanpa dependency. */
'use strict';

const { spawn, spawnSync } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');

const CHROME = process.env.CHROME_PATH || 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const APP_URL = process.env.APP_URL || 'http://127.0.0.1:8003/';
const PORT = Number(process.env.CDP_PORT || 9333);
const PROFIL = path.join(os.tmpdir(), 'capstone-chrome-' + Date.now());

const hasil = [];
function catat(nama, lulus, detail) {
  hasil.push({ nama, lulus: Boolean(lulus), detail: detail || '' });
  console.log((lulus ? 'LULUS ' : 'GAGAL ') + nama + (detail ? ' | ' + detail : ''));
}

const tidur = (ms) => new Promise((r) => setTimeout(r, ms));

async function tungguEndpoint() {
  for (let i = 0; i < 60; i++) {
    try {
      const r = await fetch(`http://127.0.0.1:${PORT}/json/version`);
      if (r.ok) return;
    } catch (_) {}
    await tidur(100);
  }
  throw new Error('Chrome remote debugging tidak siap.');
}

function buatRespons(status, jawaban, sumber, sessionId, performansi) {
  return {
    ok: true,
    status: status || 'berhasil',
    jawaban: jawaban || '',
    pesan: '',
    sumber: sumber || [],
    langkah: 1,
    session_id: sessionId,
    waktu: new Date().toISOString(),
    performansi: performansi || {
      total_detik: 1.234,
      tahapan: { model: { jumlah: 1, total_detik: 1.1, maks_detik: 1.1, rata_detik: 1.1 } },
      paling_lambat: [{ nama: 'model', jumlah: 1, total_detik: 1.1, maks_detik: 1.1, rata_detik: 1.1 }],
      panggilan_model: 1,
      langkah: 1,
      tool: { baca_cache: { jumlah: 1, status: 'ditemukan' } },
      cache: { hit: 1, miss: 0 },
      token: { prompt: 100, completion: 20, total: 120 },
    },
  };
}

async function hubungkan(wsUrl, nama, opsi = {}) {
  const healthGagal = Boolean(opsi.healthGagal);
  const ws = new WebSocket(wsUrl);
  await new Promise((resolve, reject) => {
    ws.onopen = resolve;
    ws.onerror = reject;
  });

  const belumSelesai = new Map();
  const kesalahan = [];
  const antreanChat = [];
  const badanChat = [];
  let nomor = 1;

  ws.onmessage = async (event) => {
    const pesan = JSON.parse(event.data);

    if (pesan.id && belumSelesai.has(pesan.id)) {
      const { resolve, reject } = belumSelesai.get(pesan.id);
      belumSelesai.delete(pesan.id);
      if (pesan.error) reject(new Error(JSON.stringify(pesan.error)));
      else resolve(pesan.result);
      return;
    }

    if (pesan.method === 'Runtime.exceptionThrown') {
      kesalahan.push(nama + ': exception ' + JSON.stringify(pesan.params.exceptionDetails.text));
    } else if (pesan.method === 'Runtime.consoleAPICalled' && pesan.params.type === 'error') {
      kesalahan.push(nama + ': console ' + JSON.stringify(pesan.params.args.map((a) => a.value || a.description)));
    } else if (pesan.method === 'Log.entryAdded' && pesan.params.entry.level === 'error') {
      kesalahan.push(nama + ': log ' + pesan.params.entry.text + ' @ ' + (pesan.params.entry.url || ''));
    } else if (pesan.method === 'Network.loadingFailed') {
      kesalahan.push(nama + ': network ' + pesan.params.errorText);
    } else if (pesan.method === 'Fetch.requestPaused') {
      const { requestId, request } = pesan.params;
      const url = request.url || '';

      try {
        if (url.includes('/api/health')) {
          const badan = JSON.stringify(healthGagal ? {
            ok: false,
            model: 'deepseek-v4.1-flash:free',
            model_api: '',
            model_tersedia: false,
            penyedia: 'tokenharbor',
            lokal: false,
            pesan: 'Kredensial penyedia belum dikonfigurasi di environment.',
            waktu: new Date().toISOString(),
          } : {
            ok: true,
            model: 'deepseek-v4.1-flash:free',
            model_api: 'deepseek-v4.1-flash:free',
            model_tersedia: true,
            penyedia: 'tokenharbor',
            lokal: false,
            pesan: 'Penyedia terhubung; model deepseek-v4.1-flash:free tersedia.',
            waktu: new Date().toISOString(),
          });
          await kirim('Fetch.fulfillRequest', {
            requestId,
            responseCode: 200,
            responseHeaders: [{ name: 'Content-Type', value: 'application/json' }],
            body: Buffer.from(badan).toString('base64'),
          });
          return;
        }

        if (url.includes('/api/chat')) {
          badanChat.push({
            url,
            body: request.postData ? JSON.parse(request.postData) : {},
          });
          await tidur(350);

          const berikut = antreanChat.length
            ? antreanChat.shift()
            : buatRespons('berhasil', 'Jawaban default.', [], 'session-default');

          await kirim('Fetch.fulfillRequest', {
            requestId,
            responseCode: 200,
            responseHeaders: [{ name: 'Content-Type', value: 'application/json' }],
            body: Buffer.from(JSON.stringify(berikut)).toString('base64'),
          });
          return;
        }

        if (url.includes('/api/session/reset')) {
          await kirim('Fetch.fulfillRequest', {
            requestId,
            responseCode: 200,
            responseHeaders: [{ name: 'Content-Type', value: 'application/json' }],
            body: Buffer.from(JSON.stringify({ ok: true, session_id: 'session-reset' })).toString('base64'),
          });
          return;
        }

        await kirim('Fetch.continueRequest', { requestId });
      } catch (e) {
        kesalahan.push(nama + ': fetch ' + e.message);
        try { await kirim('Fetch.failRequest', { requestId, errorReason: 'Failed' }); } catch (_) {}
      }
    }
  };

  function kirim(method, params = {}) {
    return new Promise((resolve, reject) => {
      const id = nomor++;
      belumSelesai.set(id, { resolve, reject });
      ws.send(JSON.stringify({ id, method, params }));
    });
  }

  async function evaluasi(expression) {
    const r = await kirim('Runtime.evaluate', {
      expression,
      awaitPromise: true,
      returnByValue: true,
    });
    if (r.exceptionDetails) {
      throw new Error(expression + ' -> ' + JSON.stringify(r.exceptionDetails));
    }
    return r.result.value;
  }

  async function tunggu(expression, batas = 8000) {
    const awal = Date.now();
    while (Date.now() - awal < batas) {
      if (await evaluasi(expression)) return true;
      await tidur(100);
    }
    throw new Error('Timeout menunggu: ' + expression);
  }

  await kirim('Runtime.enable');
  await kirim('Page.enable');
  await kirim('Network.enable');
  await kirim('Log.enable');
  await kirim('Fetch.enable', {
    patterns: [{ urlPattern: '*', requestStage: 'Request' }],
  });
  // Beri waktu pattern Fetch terpasang sebelum halaman meminta /api/health.
  await tidur(150);

  return { kirim, evaluasi, tunggu, kesalahan, antreanChat, badanChat, ws };
}

async function kirimEnter(halaman, teks, opsi = {}) {
  await halaman.evaluasi(`
    (() => {
      const q = document.getElementById('question');
      q.value = ${JSON.stringify(teks)};
      q.dispatchEvent(new Event('input', { bubbles: true }));
      q.dispatchEvent(new KeyboardEvent('keydown', {
        key: 'Enter',
        shiftKey: ${Boolean(opsi.shift)},
        bubbles: true,
        cancelable: true,
      }));
      return true;
    })()
  `);
}

(async () => {
  const chrome = spawn(CHROME, [
    '--headless=new',
    '--disable-gpu',
    '--no-first-run',
    '--no-default-browser-check',
    '--disable-extensions',
    `--remote-debugging-port=${PORT}`,
    `--user-data-dir=${PROFIL}`,
    'about:blank',
  ], { stdio: 'ignore' });

  try {
    await tungguEndpoint();

    const daftar = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
    const target = daftar.find((t) => t.type === 'page');
    const halaman = await hubungkan(target.webSocketDebuggerUrl, 'tab-1');
    await halaman.kirim('Page.navigate', { url: APP_URL });
    await halaman.tunggu("Boolean(document.getElementById('chat-form'))");
    await halaman.tunggu("(() => { const t = document.getElementById('status-value').textContent; return t.length > 0 && !t.includes('Memeriksa'); })()");

    const statusAwal = await halaman.evaluasi("document.getElementById('status-value').textContent");
    const labelAwal = await halaman.evaluasi("document.getElementById('model-label').textContent");
    const modeAwal = await halaman.evaluasi("document.getElementById('mode-label').textContent");
    catat(
      'Muat awal dan health',
      statusAwal.includes('deepseek-v4.1-flash:free')
        && labelAwal === 'deepseek-v4.1-flash:free'
        && modeAwal === 'TokenHarbor',
      `${statusAwal} | ${labelAwal} | ${modeAwal}`
    );

    const jawabanXss = 'Jawaban aman <img src=x onerror="window.__xss=1"> <script>window.__xss=1<\/script>';
    halaman.antreanChat.push(buatRespons(
      'berhasil',
      jawabanXss,
      [
        { url: 'javascript:alert(1)', judul: 'Sumber jahat' },
        { url: 'https://evil.example/', judul: 'Domain lain' },
        { url: 'https://uir.ac.id/profil/', judul: 'Profil UIR' },
      ],
      'session-a',
    ));

    await kirimEnter(halaman, 'Apa itu UIR?');
    await halaman.evaluasi(`
      (() => {
        const q = document.getElementById('question');
        q.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }));
        return true;
      })()
    `);
    await halaman.tunggu("document.querySelectorAll('.answer').length > 0");
    await tidur(200);

    catat('Enter mengirim satu permintaan', halaman.badanChat.length === 1, 'jumlah=' + halaman.badanChat.length);
    catat('Double-send dicegah', await halaman.evaluasi("document.getElementById('btn-send').disabled === false"));

    const aman = await halaman.evaluasi(`
      (() => {
        const answer = document.querySelector('.answer');
        const teks = answer ? answer.textContent : '';
        const gambar = answer ? answer.querySelectorAll('img, script').length : -1;
        const tautan = Array.from(document.querySelectorAll('.source-list a')).map(a => a.href);
        return {
          memuatLit: teks.includes('<img'),
          gambar,
          xss: window.__xss || 0,
          tautan,
          perf: Boolean(document.querySelector('.answer__perf')),
          perfTeks: document.querySelector('.answer__perf')?.textContent || '',
        };
      })()
    `);
    catat('XSS tidak dieksekusi', aman.memuatLit && aman.gambar === 0 && aman.xss === 0, JSON.stringify({ gambar: aman.gambar, xss: aman.xss }));
    catat('Sumber jahat tidak dirender', aman.tautan.length === 1 && aman.tautan[0].startsWith('https://uir.ac.id/'), JSON.stringify(aman.tautan));
    catat('Detail performansi tampil', aman.perf && aman.perfTeks.includes('Panggilan model'), aman.perfTeks.slice(0, 90));

    const sebelumShift = halaman.badanChat.length;
    await kirimEnter(halaman, 'Shift enter', { shift: true });
    await tidur(250);
    catat('Shift+Enter tidak mengirim', halaman.badanChat.length === sebelumShift, 'jumlah=' + halaman.badanChat.length);

    await halaman.evaluasi(`
      (() => {
        window.__disalin = null;
        navigator.clipboard.writeText = async (teks) => { window.__disalin = teks; return true; };
        const tombol = document.querySelector('.copy-btn');
        if (tombol) tombol.click();
        return true;
      })()
    `);
    await halaman.tunggu('typeof window.__disalin === "string"', 3000);
    catat('Tombol salin bekerja', await halaman.evaluasi('window.__disalin.includes("Jawaban aman")'));

    await halaman.evaluasi("document.getElementById('btn-new').click()");
    catat('Percakapan baru mereset session', await halaman.evaluasi('state.sessionId === null'));

    halaman.antreanChat.push(buatRespons('berhasil', 'Jawaban sesi A baru.', [], 'session-a2'));
    await kirimEnter(halaman, 'Pertanyaan sesi A');
    await halaman.tunggu("document.body.innerText.includes('Jawaban sesi A baru.')");
    const badanA = halaman.badanChat[halaman.badanChat.length - 1].body;
    catat('Session_id baru dipakai setelah reset', badanA.session_id === null, JSON.stringify(badanA));

    const tab2 = await (await fetch(`http://127.0.0.1:${PORT}/json/new?about:blank`, { method: 'PUT' })).json();
    const halaman2 = await hubungkan(tab2.webSocketDebuggerUrl, 'tab-2');
    await halaman2.kirim('Page.navigate', { url: APP_URL });
    await halaman2.tunggu(`location.href.startsWith(${JSON.stringify(APP_URL)}) && Boolean(document.getElementById('chat-form'))`);
    await halaman2.tunggu("(() => { const t = document.getElementById('status-value').textContent; return t.length > 0 && !t.includes('Memeriksa'); })()");
    halaman2.antreanChat.push(buatRespons('berhasil', 'Jawaban sesi B.', [], 'session-b'));

    await kirimEnter(halaman2, 'Pertanyaan sesi B');
    for (let i = 0; i < 50 && halaman2.badanChat.length < 1; i++) await tidur(100);
    if (halaman2.badanChat.length < 1) throw new Error('Tab 2 tidak mengirim permintaan chat.');
    await halaman2.tunggu("document.body.textContent.includes('Jawaban sesi B.')");
    const badanB = halaman2.badanChat[0].body;
    const idA = await halaman.evaluasi('state.sessionId');
    const idB = await halaman2.evaluasi('state.sessionId');

    const terpisah = idA === 'session-a2' && idB === 'session-b' && idA !== idB;
    catat('Dua sesi terpisah', terpisah, JSON.stringify({ a: idA, b: idB, permintaanA: badanA.session_id, permintaanB: badanB.session_id }));

    await halaman2.evaluasi("document.getElementById('btn-start').click()");
    await halaman2.kirim('Emulation.setDeviceMetricsOverride', {
      width: 375, height: 812, deviceScaleFactor: 2, mobile: true,
    });
    await tidur(300);
    const responsif = await halaman2.evaluasi(`
      (() => ({
        inner: window.innerWidth,
        scroll: document.documentElement.scrollWidth,
        form: Boolean(document.getElementById('chat-form').offsetParent),
      }))()
    `);
    catat('Tampilan responsif 375px', responsif.scroll <= responsif.inner + 1 && responsif.form, JSON.stringify(responsif));

    const tabGagal = await (await fetch(`http://127.0.0.1:${PORT}/json/new?about:blank`, { method: 'PUT' })).json();
    const halamanGagal = await hubungkan(tabGagal.webSocketDebuggerUrl, 'tab-gagal', { healthGagal: true });
    await halamanGagal.kirim('Page.navigate', { url: APP_URL });
    await halamanGagal.tunggu("Boolean(document.getElementById('chat-form'))");
    await halamanGagal.tunggu("(() => { const t = document.getElementById('status-value').textContent; return t.length > 0 && !t.includes('Memeriksa'); })()");
    const statusGagal = await halamanGagal.evaluasi("document.getElementById('status-value').textContent");
    catat(
      'Status tidak siap saat API gagal',
      !statusGagal.includes('Siap') && statusGagal.includes('TokenHarbor'),
      statusGagal
    );

    const semuaKesalahan = halaman.kesalahan.concat(halaman2.kesalahan, halamanGagal.kesalahan);
    catat('Tidak ada error konsol/jaringan', semuaKesalahan.length === 0, semuaKesalahan.slice(0, 5).join(' || '));

    halaman.ws.close();
    halaman2.ws.close();
    halamanGagal.ws.close();
  } finally {
    if (process.platform === 'win32') {
      spawnSync('taskkill', ['/PID', String(chrome.pid), '/T', '/F'], { stdio: 'ignore' });
    } else {
      chrome.kill('SIGTERM');
    }
    try { fs.rmSync(PROFIL, { recursive: true, force: true }); } catch (_) {}
  }

  const gagal = hasil.filter((h) => !h.lulus);
  console.log(`RINGKASAN ${hasil.length - gagal.length}/${hasil.length} lulus`);
  process.exit(gagal.length ? 1 : 0);
})().catch((e) => {
  console.error('FATAL', e.stack || e);
  process.exit(1);
});
