"""Mengambil teks halaman publik UIR; tidak merangkum atau memanggil AI."""
import logging
import re
from datetime import datetime, timezone
from html.parser import HTMLParser

from tools import cari_uir as _cari
from tools.cari_uir import (
    _DiblokirRobots, _SitusTidakTersedia, _cek_robots, _http_get, _validasi_url,
)

LOGGER = logging.getLogger(__name__)
REQUEST_TIMEOUT = _cari.REQUEST_TIMEOUT
USER_AGENT = _cari.USER_AGENT
BATAS_TEKS = 20000
BATAS_JUDUL = 300
MAX_URL = 2000

STATUS_BERHASIL = "berhasil"
STATUS_VALIDASI_GAGAL = "validasi_gagal"
STATUS_DIBLOKIR_ROBOTS = "diblokir_robots"
STATUS_SITUS_TIDAK_TERSEDIA = "situs_tidak_tersedia"
STATUS_KONTEN_KOSONG = "konten_kosong"

_SKIP = frozenset({
    "script", "style", "noscript", "svg", "form", "nav", "footer",
    "header", "iframe", "video", "audio", "picture", "canvas",
    "template", "button", "select", "textarea",
})
_VOID = frozenset({
    "img", "input", "br", "hr", "meta", "link", "source", "track",
    "embed", "area", "base", "col", "param", "wbr",
})
_HIDDEN_CLASSES = frozenset({
    "screen-reader-text", "skip-link", "sr-only", "visually-hidden",
})
_BLOCK = frozenset({
    "p", "div", "br", "hr", "li", "ul", "ol", "dl", "dt", "dd",
    "tr", "td", "th", "table", "section", "article", "main",
    "blockquote", "pre", "figure", "figcaption",
    "h1", "h2", "h3", "h4", "h5", "h6",
})


def _tersembunyi(attrs):
    d = {str(k).lower(): (v or "") for k, v in attrs}
    if "hidden" in d or d.get("aria-hidden", "").lower() == "true":
        return True
    style = d.get("style", "").lower().replace(" ", "")
    if "display:none" in style or "visibility:hidden" in style:
        return True
    return any(x in d.get("class", "").lower().split() for x in _HIDDEN_CLASSES)


class _EkstraktorHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title_parts, self.main_parts, self.all_parts = [], [], []
        self.in_title = False
        self.stack = []

    def _skipped(self):
        return any(hidden for _, hidden in self.stack)

    def _in_main(self):
        return any(tag in ("main", "article") for tag, _ in self.stack)

    def _newline(self):
        self.all_parts.append("\n")
        if self._in_main():
            self.main_parts.append("\n")

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if self.in_title:
            return
        if tag == "title" and not self._skipped():
            self.in_title = True
            return
        if tag in _VOID:
            if tag in _BLOCK:
                self._newline()
            return
        hidden = tag in _SKIP or _tersembunyi(attrs)
        self.stack.append((tag, hidden))
        if tag in _BLOCK and not hidden:
            self._newline()

    def handle_startendtag(self, tag, attrs):
        if tag.lower() in _BLOCK and not self._skipped():
            self._newline()

    def handle_endtag(self, tag):
        tag = tag.lower()
        if self.in_title:
            if tag == "title":
                self.in_title = False
            return
        names = [x[0] for x in self.stack]
        if tag in names:
            index = len(names) - 1 - names[::-1].index(tag)
            self.stack = self.stack[:index]
            if tag in _BLOCK:
                self._newline()

    def handle_data(self, data):
        if self.in_title:
            self.title_parts.append(data)
        elif not self._skipped():
            self.all_parts.append(data)
            if self._in_main():
                self.main_parts.append(data)


def _rapikan(parts):
    text = "".join(parts).replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\xa0", " ").replace("\u200b", "").replace("\ufeff", "")
    text = "".join(
        c for c in text
        if c in "\n\t" or ord(c) >= 32 and ord(c) != 127
    )
    lines = [
        re.sub(r"[ \t\f\v]+", " ", line).strip()
        for line in text.split("\n")
    ]
    out, previous_blank = [], True
    for line in lines:
        if line:
            out.append(line)
            previous_blank = False
        elif out and not previous_blank:
            out.append("")
            previous_blank = True
    return "\n".join(out).strip()


def _batasi(text, limit):
    if len(text) <= limit:
        return text
    part = text[:limit]
    pos = max(part.rfind(" "), part.rfind("\n"))
    if pos > limit // 2:
        part = part[:pos]
    return part.rstrip() + "…"


def _hasil(status, ok, url, judul="", teks="", pesan=""):
    return {
        "ok": ok,
        "status": status,
        "url": url,
        "judul": judul,
        "teks": teks,
        "panjang": len(teks),
        "pesan": pesan,
        "waktu": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def ambil_halaman(url):
    tampil = url if isinstance(url, str) else str(url)
    if not isinstance(url, str) or not url.strip() or len(url.strip()) > MAX_URL:
        return _hasil(
            STATUS_VALIDASI_GAGAL, False, tampil,
            pesan="URL kosong atau terlalu panjang."
        )

    alamat = _validasi_url(url)
    if not alamat:
        return _hasil(
            STATUS_VALIDASI_GAGAL, False, tampil,
            pesan="Hanya HTTPS dengan hostname tepat uir.ac.id yang diizinkan."
        )

    try:
        _cek_robots(alamat)
        raw = _http_get(alamat)
        parser = _EkstraktorHTML()
        parser.feed(raw.decode("utf-8", "replace"))
        parser.close()

        title = _batasi(_rapikan(parser.title_parts), BATAS_JUDUL)
        main = _rapikan(parser.main_parts)
        fallback = _rapikan(parser.all_parts)

        chosen = (
            main
            if main and (not fallback or len(main) * 2 >= len(fallback))
            else fallback
        )
        text = _batasi(chosen, BATAS_TEKS)

        if not text:
            return _hasil(
                STATUS_KONTEN_KOSONG, False, alamat, title,
                pesan="Halaman diambil tetapi teks tidak berhasil diekstrak."
            )

        return _hasil(
            STATUS_BERHASIL, True, alamat, title, text,
            "Berhasil mengambil halaman publik uir.ac.id."
        )

    except _DiblokirRobots as e:
        return _hasil(STATUS_DIBLOKIR_ROBOTS, False, alamat, pesan=str(e))
    except _SitusTidakTersedia as e:
        return _hasil(STATUS_SITUS_TIDAK_TERSEDIA, False, alamat, pesan=str(e))
    except Exception:
        LOGGER.exception("Kesalahan tak terduga di ambil_halaman")
        return _hasil(
            STATUS_SITUS_TIDAK_TERSEDIA, False, alamat,
            pesan="Terjadi kesalahan saat mengambil halaman UIR."
        )