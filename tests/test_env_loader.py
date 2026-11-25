"""Uji pemuat .env: nilai shell tetap prioritas dan kredensial tidak dicetak."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app import env_loader


class UjiMuatEnv(unittest.TestCase):
    def setUp(self):
        self._flag_awal = env_loader._SUDAH_DIMUAT

    def tearDown(self):
        env_loader._SUDAH_DIMUAT = self._flag_awal

    def test_file_tidak_ada_tidak_mengubah_env(self):
        dengan = {"DEEPSEEK_API_KEY": "dari-shell"}
        with mock.patch.dict(os.environ, dengan, clear=False), \
             tempfile.TemporaryDirectory() as folder:
            env_loader.muat_env(Path(folder) / ".env")
            self.assertEqual(os.environ["DEEPSEEK_API_KEY"], "dari-shell")

    def test_nilai_file_dipakai_bila_belum_ada_di_env(self):
        dengan = {"DEEPSEEK_API_KEY": "", "MODEL_PROVIDER": ""}
        isi = (
            "# komentar\n"
            "DEEPSEEK_API_KEY=kunci-dari-file\n"
            'DEEPSEEK_MODEL="deepseek-v4.1-flash:free"\n'
            "DEEPSEEK_BASE_URL=https://tokenharbor.ai/v1 # inline\n"
            "baris-tanpa-sama\n"
        )
        with tempfile.TemporaryDirectory() as folder:
            berkas = Path(folder) / ".env"
            berkas.write_text(isi, encoding="utf-8")
            with mock.patch.dict(os.environ, dengan, clear=False):
                for kunci in dengan:
                    os.environ.pop(kunci, None)
                env_loader.muat_env(berkas)
                self.assertEqual(os.environ["DEEPSEEK_API_KEY"], "kunci-dari-file")
                self.assertEqual(
                    os.environ["DEEPSEEK_MODEL"], "deepseek-v4.1-flash:free"
                )
                self.assertEqual(
                    os.environ["DEEPSEEK_BASE_URL"], "https://tokenharbor.ai/v1"
                )

    def test_export_shell_tidak_ditimpa_file(self):
        dengan = {"DEEPSEEK_API_KEY": "kunci-dari-shell"}
        isi = "DEEPSEEK_API_KEY=kunci-dari-file\n"
        with tempfile.TemporaryDirectory() as folder:
            berkas = Path(folder) / ".env"
            berkas.write_text(isi, encoding="utf-8")
            with mock.patch.dict(os.environ, dengan, clear=False):
                env_loader.muat_env(berkas)
                self.assertEqual(os.environ["DEEPSEEK_API_KEY"], "kunci-dari-shell")


if __name__ == "__main__":
    unittest.main()
