"""Faz 3 komutları: ayrıştırıcı ve komut satırı (yeniden başlatma sonrası tutanak işlemleri)."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from jarvis.voice.command_parser import CommandParser, IntentType as I

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("text,expected,force", [
    ("Jarvis tutanağı gönder", I.SEND_MINUTES, False),
    ("Jarvis tutanağı e-postayla gönder", I.SEND_MINUTES, False),
    ("Jarvis tutanağı tekrar gönder", I.SEND_MINUTES, True),
    ("Jarvis tutanağı yeniden gönder", I.SEND_MINUTES, True),
])
def test_send_minutes_force_flag(text, expected, force):
    intent = CommandParser().parse(text)
    assert intent.type == expected and intent.get("force") is force


@pytest.mark.parametrize("text", ["Jarvis tutanak oluştur", "Jarvis tutanağı hazırla", "Jarvis ara tutanak",
                                  "Jarvis pdf oluştur"])
def test_create_minutes_intent(text):
    assert CommandParser().parse(text).type == I.CREATE_MINUTES


def _env(tmp_path: Path, **extra) -> dict:
    env = {**os.environ, "DATA_DIR": str(tmp_path / "data"), "OUTPUT_DIR": str(tmp_path / "out"),
           "LOGS_DIR": str(tmp_path / "logs"), "PYTHONIOENCODING": "utf-8", "JARVIS_ENV_FILE": "none",
           "PARTICIPANTS_FILE": str(PROJECT_ROOT / "data" / "participants.example.json"),
           "AGENDA_FILE": str(PROJECT_ROOT / "data" / "agenda.example.json"),
           "SMTP_HOST": "", "SMTP_USER": "", "SMTP_FROM": "", "MAIL_DRY_RUN": "false", "MAIL_REDIRECT_TO": "",
           "AUTO_SEND_MINUTES": "false"}
    env.update(extra)
    (tmp_path / "data").mkdir(exist_ok=True)
    return env


def _run(args, env, stdin=""):
    return subprocess.run([sys.executable, "main.py", *args], cwd=PROJECT_ROOT, env=env, input=stdin,
                          capture_output=True, text=True, encoding="utf-8", timeout=120)


def test_cli_minutes_flow_after_restart(tmp_path):
    env = _env(tmp_path)
    session = _run(["--text", "--no-tts"], env,
                   "Jarvis toplantıyı başlat\nJarvis karar: CLI test kararı\nJarvis toplantıyı bitir\nJarvis çık\n")
    assert session.returncode == 0, session.stderr
    assert "Tutanak oluşturuldu" in session.stdout
    pdfs = list((tmp_path / "out").glob("*.pdf"))
    assert len(pdfs) == 1

    listing = _run(["--list-meetings"], env)
    assert "[bitti]" in listing.stdout and "e-posta: gönderilmedi" in listing.stdout

    pdfs[0].unlink()
    regenerate = _run(["--minutes"], env)
    assert regenerate.returncode == 0 and "Tutanak oluşturuldu" in regenerate.stdout
    assert len(list((tmp_path / "out").glob("*.pdf"))) == 1

    no_smtp = _run(["--send-minutes"], env)
    assert no_smtp.returncode == 1 and "SMTP_HOST" in no_smtp.stdout

    dry = _run(["--send-minutes"], _env(tmp_path, MAIL_DRY_RUN="true"))
    assert dry.returncode == 0 and "Deneme kipi" in dry.stdout
    assert len(list((tmp_path / "out" / "outbox").glob("*.eml"))) == 1


def test_cli_unknown_meeting(tmp_path):
    result = _run(["--minutes", "olmayan-id"], _env(tmp_path))
    assert result.returncode == 1 and "bulunamadı" in result.stdout
