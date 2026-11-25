"""Paket aplikasi UIR AI Assistant.

Pemanggilan ``muat_env()`` dilakukan di sini supaya seluruh modul aplikasi
melihat konfigurasi dari ``.env`` lokal maupun dari export shell. Nilai yang
sudah diekspor di shell tidak ditimpa.
"""

from app.env_loader import muat_env

muat_env()
