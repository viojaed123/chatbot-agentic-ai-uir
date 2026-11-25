/* Uji frontend live terhadap backend Hermes, tanpa mock API. */
'use strict';

const { spawn, spawnSync } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');

const CHROME = process.env.CHROME_PATH || 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const APP_URL = process.env.APP_URL || 'http://127.0.0.1:8012/';
const PORT = Number(process.env.CDP_PORT || 9344);
const PROFIL = path.join(os.tmpdir(), 'capstone-hermes-cdp-' + Date.now());

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

async function hubungkan(wsUrl, nama) {
  const ws = new WebSocket(wsUrl);
  await new Promise((resolve, reject) => {
    ws.onopen = resolve;
    ws.onerror = reject;
  });

  const belumSelesai = new Map();
  const kesalahan = [];
  let nomor = 1;

  ws.onmessage = (event) => {
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
      kesalahan.push(nama + ': log ' + pesan.params.entry.text);
    } else if (pesan.method === 'Network.loadingFailed') {
      kesalahan.push(nama + ': network ' + pesan.params.errorText);
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
  await kirim('Page.addScriptToEvaluateOnNewDocument', {
    source: `
      (() => {
        const asli = window.fetch.bind(window);
        window.__bukti = { health: [], chat: [] };
        window.fetch = async (...args) => {
          const respons = await asli(...args);
          try {
            const target = typeof args[0] === 'string'
              ? args[0]
              : ((args[0] && args[0].url) || '');
            if (target.includes('/api/health') || target.includes('/api/chat')) {
              const data = await respons.clone().json();
              const kunci = target.includes('/api/health') ? 'health' : 'chat';
              window.__bukti[kunci].push(data);
            }
          } catch (_) {}
          return respons;
        };
      })();
    `,
  });

  return { kirim, evaluasi, tunggu, kesalahan, ws };
}

async function kirimEnter(halaman, teks) {
  await halaman.evaluasi(`
    (() => {
      const q = document.getElementById('question');
      q.value = ${JSON.stringify(teks)};
      q.dispatchEvent(new Event('input', { bubbles: true }));
      q.dispatchEvent(new KeyboardEvent('keydown', {
        key: 'Enter', bubbles: true, cancelable: true,
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
    const halaman = await hubungkan(target.webSocketDebuggerUrl, 'tab-hermes');
    await halaman.kirim('Page.navigate', { url: APP_URL });
    await halaman.tunggu("Boolean(document.getElementById('chat-form'))");
    await halaman.tunggu("(() => { const t = document.getElementById('status-value').textContent; return t.length > 0 && !t.includes('Memeriksa'); })()");

    const awal = await halaman.evaluasi(`(() => ({
      status: document.getElementById('status-value').textContent,
      model: document.getElementById('model-label').textContent,
      mode: document.getElementById('mode-label').textContent,
      health: (window.__bukti.health[0] || null),
    }))()`);
    catat(
      'Health awal jujur dan runtime Hermes',
      Boolean(awal.health)
        && awal.health.agent_runtime === 'hermes'
        && awal.health.model_tersedia === false
        && !awal.status.includes('Siap')
        && awal.mode.includes('Hermes'),
      JSON.stringify({
        status: awal.status,
        mode: awal.mode,
        agent_runtime: awal.health && awal.health.agent_runtime,
        model_tersedia: awal.health && awal.health.model_tersedia,
      })
    );

    await kirimEnter(halaman, 'Apa visi Universitas Islam Riau?');
    await halaman.tunggu("(window.__bukti.chat.length > 0)", 240000);
    await halaman.tunggu("document.querySelectorAll('.answer').length > 0", 240000);

    const bukti = await halaman.evaluasi(`(() => {
      const d = window.__bukti.chat[0] || {};
      const jawaban = document.querySelector('.answer');
      return {
        ok: d.ok,
        status: d.status,
        agent_runtime: d.agent_runtime,
        detail_model: d.detail_model || {},
        sumber: Array.isArray(d.sumber) ? d.sumber : [],
        jawaban_api: typeof d.jawaban === 'string' ? d.jawaban : '',
        teks_ui: jawaban ? jawaban.innerText : '',
        status_ui: document.getElementById('status-value').textContent,
        model_ui: document.getElementById('model-label').textContent,
        mode_ui: document.getElementById('mode-label').textContent,
        session_id: (typeof state !== 'undefined' && state.sessionId) || null,
      };
    })()`);

    const rinci = bukti.detail_model || {};
    const token = rinci.usage || {};
    catat(
      'Inferensi live Hermes + DeepSeek terverifikasi',
      bukti.ok === true
        && bukti.status === 'berhasil'
        && bukti.agent_runtime === 'hermes'
        && rinci.agent === 'hermes'
        && String(rinci.model || '').includes('deepseek-v4.1-flash')
        && String(rinci.status || '') === 'completed'
        && Number(token.total_tokens || 0) > 0,
      JSON.stringify({
        model: rinci.model,
        status: rinci.status,
        total_tokens: token.total_tokens,
        run_id: rinci.run_id,
      })
    );
    catat(
      'Jawaban nyata memuat visi UIR',
      bukti.jawaban_api.includes('Universitas Islam Berkelas Dunia')
        && bukti.teks_ui.includes('Universitas Islam Berkelas Dunia'),
      bukti.jawaban_api.slice(0, 120)
    );
    const semuaUir = bukti.sumber.length > 0
      && bukti.sumber.every((s) => String(s.url || '').startsWith('https://uir.ac.id/'));
    const adaSitasi = bukti.sumber.some((s) => s.status && s.status.cited === true);
    catat(
      'Sitasi berasal dari tool UIR',
      semuaUir && adaSitasi,
      JSON.stringify(bukti.sumber.map((s) => ({
        url: s.url,
        cited: s.status && s.status.cited,
      })))
    );
    catat(
      'Status UI berubah setelah inferensi live',
      bukti.status_ui.includes('Siap')
        && bukti.status_ui.includes('Hermes Agent')
        && bukti.model_ui.includes('deepseek-v4.1-flash')
        && bukti.mode_ui.includes('Hermes'),
      JSON.stringify({
        status: bukti.status_ui,
        model: bukti.model_ui,
        mode: bukti.mode_ui,
      })
    );
    catat(
      'Session ID diterbitkan server',
      typeof bukti.session_id === 'string' && bukti.session_id.length >= 16,
      JSON.stringify({ session_id: bukti.session_id })
    );
    catat('Tidak ada error konsol/jaringan', halaman.kesalahan.length === 0, halaman.kesalahan.slice(0, 5).join(' || '));

    halaman.ws.close();
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
