"""Isolation des tests backend.

Ce fichier est charge par pytest avant tout module de test, donc avant le
premier import de settings.py: les variables ci-dessous l'emportent sur
backend/.env (python-dotenv ne remplace jamais une variable deja definie).
Aucun test n'ecrit ainsi dans le vrai CACHE_DIR (analyzer.log, locks,
progressions), ni dans la vraie configuration des index, et aucun appel
reseau reel n'est possible vers les providers de fuites.
"""

import os
import shutil
import sys
import tempfile
from pathlib import Path

_TEST_ROOT = Path(tempfile.mkdtemp(prefix="oeil-backend-tests-"))

os.environ["OEIL_CACHE_DIR"] = str(_TEST_ROOT / "cache")
os.environ["OEIL_INDEXES_CONFIG"] = str(_TEST_ROOT / "indexes.json")
os.environ["OEIL_ALLOWED_HOSTS"] = "127.0.0.1,localhost,testserver"
os.environ["OEIL_ALLOW_LOCALHOST_NO_AUTH"] = "false"
os.environ["ALLOWED_ORIGINS"] = "*"
os.environ["API_KEY"] = "test-bot-key"
os.environ["HIBP_API_KEY"] = ""
os.environ["BREACHDIRECTORY_RAPIDAPI_KEY"] = ""

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(_TEST_ROOT, ignore_errors=True)
