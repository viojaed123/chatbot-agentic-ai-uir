"""Jalankan Hermes Agent API Server sebagai runtime utama proyek UIR.

Skrip ini hanya meluncurkan proses gateway. Reasoning, planning, tool calling,
dan execution loop tetap berada di Hermes Agent. Kredensial dibaca dari
environment/.env dan tidak pernah dicetak.
"""

import os
import pathlib
import subprocess
import sys
import tempfile

_AKAR = pathlib.Path(__file__).resolve().parents[1]
if str(_AKAR) not in sys.path:
    sys.path.insert(0, str(_AKAR))

from app.env_loader import muat_env

muat_env()

for _nama in ("DEEPSEEK_API_KEY", "HERMES_API_KEY"):
    if not os.environ.get(_nama):
        raise SystemExit("Variabel " + _nama + " belum diisi di .env/environment.")

_sumber = pathlib.Path(
    os.environ.get("HERMES_SOURCE")
    or pathlib.Path.home() / "AppData" / "Local" / "hermes" / "hermes-agent"
)
if not _sumber.is_dir():
    raise SystemExit("Source Hermes tidak ditemukan: " + str(_sumber))

_home = pathlib.Path(
    os.environ.get("HERMES_HOME")
    or pathlib.Path.home() / "AppData" / "Local" / "hermes-uir"
)
if not (_home / "config.yaml").is_file():
    raise SystemExit("config.yaml tidak ditemukan di HERMES_HOME: " + str(_home))

_log_dir = pathlib.Path(
    os.environ.get("HERMES_LOG_DIR")
    or pathlib.Path(tempfile.gettempdir()) / "capstone-hermes"
)
_log_dir.mkdir(parents=True, exist_ok=True)
_log = _log_dir / "gateway.log"
_pid = _log_dir / "gateway.pid"

_env = os.environ.copy()
_env["PYTHONPATH"] = str(_sumber) + os.pathsep + _env.get("PYTHONPATH", "")
_env["HERMES_HOME"] = str(_home)
_env["PYTHONIOENCODING"] = "utf-8"

_pilihan = {}
if os.name == "nt":
    _pilihan["creationflags"] = (
        getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        | getattr(subprocess, "DETACHED_PROCESS", 0)
    )
else:
    _pilihan["start_new_session"] = True

with _log.open("ab") as _keluaran:
    _proses = subprocess.Popen(
        [sys.executable, "-m", "hermes_cli.main", "gateway"],
        cwd=str(_sumber),
        env=_env,
        stdout=_keluaran,
        stderr=subprocess.STDOUT,
        close_fds=True,
        **_pilihan,
    )

_pid.write_text(str(_proses.pid), encoding="utf-8")
print("HERMES_SOURCE=", _sumber)
print("HERMES_HOME=", _home)
print("PID=", _proses.pid)
print("LOG=", _log)
print("Tidak ada kredensial yang dicetak.")
