"""Güvenilirlik: SQLite yazma hataları, beklenmeyen kapanma, süre izleyicisi."""
import os
import subprocess
import sys
import time
from pathlib import Path

from jarvis.assistant import JarvisAssistant
from jarvis.meeting.manager import MeetingManager
from jarvis.meeting.storage import MeetingRepository, StorageError
from jarvis.voice.listener import ScriptedListener

PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ------------------------------------------------------------------ SQLite yazma hatası
def test_storage_failure_reported_and_recovered_on_next_save(assistant, repo, monkeypatch):
    assistant.handle_text("Jarvis toplantıyı başlat")
    original_save = repo.save
    calls = {"n": 0}

    def failing_once(meeting):
        calls["n"] += 1
        if calls["n"] == 1:
            raise StorageError("disk dolu (test)")
        original_save(meeting)

    monkeypatch.setattr(repo, "save", failing_once)
    failed = assistant.handle_text("Jarvis karar: Plan onaylandı")
    assert not failed.ok and "Kayıt hatası" in failed.message
    # Uygulama çalışmaya devam eder; sonraki kayıt bellekteki tüm durumu yazar.
    assistant.handle_text("Jarvis otopark: Ofis taşınması")
    stored = repo.get(assistant.manager.meeting.id)
    assert [d.text for d in stored.decisions] == ["Plan onaylandı"]
    assert stored.parking_lot == ["Ofis taşınması"]


def test_storage_failure_on_transcript_does_not_crash(assistant, repo, monkeypatch):
    assistant.handle_text("Jarvis toplantıyı başlat")

    def always_fail(meeting):
        raise StorageError("kilitli (test)")

    monkeypatch.setattr(repo, "save", always_fail)
    response = assistant.handle_text("Normal konuşma")
    assert not response.ok and "Kayıt hatası" in response.message
    assert assistant.manager.meeting.transcript[-1].text == "Normal konuşma"


def test_locked_database_raises_storage_error(settings):
    import sqlite3

    repo = MeetingRepository(settings.db_path)
    blocker = sqlite3.connect(str(settings.db_path), timeout=0)
    blocker.execute("BEGIN EXCLUSIVE")
    try:
        from jarvis.meeting.models import Meeting
        repo_fast = MeetingRepository.__new__(MeetingRepository)
        repo_fast.db_path = repo.db_path

        def quick_connect():
            conn = sqlite3.connect(str(repo.db_path), timeout=0.2)
            conn.row_factory = sqlite3.Row
            return conn

        repo_fast._connect = quick_connect  # type: ignore[method-assign]
        try:
            repo_fast.save(Meeting(title="Kilitli"))
            raised = False
        except StorageError:
            raised = True
        assert raised
    finally:
        blocker.rollback()
        blocker.close()


# ------------------------------------------------------------------ beklenmeyen kapanma
def test_process_killed_mid_meeting_then_recovered(tmp_path):
    """Süreç zorla öldürülür (elektrik kesintisi / görev yöneticisi); yeniden açılışta toplantı kurtarılır."""
    env = {**os.environ, "DATA_DIR": str(tmp_path / "data"), "OUTPUT_DIR": str(tmp_path / "out"),
           "LOGS_DIR": str(tmp_path / "logs"), "PYTHONIOENCODING": "utf-8", "JARVIS_ENV_FILE": "none",
           "PARTICIPANTS_FILE": str(PROJECT_ROOT / "data" / "participants.example.json"),
           "AGENDA_FILE": str(PROJECT_ROOT / "data" / "agenda.example.json")}
    (tmp_path / "data").mkdir()
    proc = subprocess.Popen([sys.executable, "-u", "main.py", "--text", "--no-tts"], cwd=PROJECT_ROOT, env=env,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8")
    try:
        lines = ["Jarvis toplantıyı başlat", "Jarvis söz Ayşe'de", "Kesilmeden önceki cümle.",
                 "Jarvis karar: Kesinti testi kararı", "Jarvis toplantı durumu kontrol"]
        for line in lines:
            proc.stdin.write(line + "\n")
        proc.stdin.flush()
        deadline = time.time() + 30
        seen = ""
        while time.time() < deadline and "Komutu anlayamadım" not in seen:  # son satır işlendi
            seen += proc.stdout.readline()
        assert "Karar kaydedildi" in seen
    finally:
        proc.kill()  # temiz kapanış yok
        proc.wait(timeout=10)

    repo = MeetingRepository(tmp_path / "data" / "jarvis.db")
    active = repo.get_active()
    assert active is not None
    assert [e.text for e in active.transcript] == ["Kesilmeden önceki cümle."]
    assert active.transcript[0].speaker == "Ayşe Yılmaz"
    assert active.decisions[0].text == "Kesinti testi kararı"

    restart = subprocess.run([sys.executable, "main.py", "--text", "--no-tts"], cwd=PROJECT_ROOT, env=env,
                             input="Jarvis toplantıyı bitir\nJarvis çık\n", capture_output=True, text=True,
                             encoding="utf-8", timeout=60)
    assert "Yarım kalan toplantı kurtarıldı" in restart.stdout
    assert "Toplantı sona erdi" in restart.stdout


# ------------------------------------------------------------------ süre izleyicisi (arka plan)
def test_timebox_monitor_speaks_warning_once(manager, settings, clock, console):
    from jarvis.voice.command_parser import CommandParser
    from jarvis.voice.speech_engine import SpeechEngine

    assistant = JarvisAssistant(manager, CommandParser(), SpeechEngine(enabled=False, output=console))
    manager.start_meeting()
    clock.advance(minutes=13)

    class SlowListener(ScriptedListener):
        def listen(self):
            time.sleep(0.15)  # izleyicinin birkaç kez çalışmasına zaman tanı
            return super().listen()

    assistant.run(SlowListener(["Jarvis süre durumu", "Jarvis süre durumu", "Jarvis çık"]), monitor_interval=0.02)
    assert console.getvalue().count("yaklaşık 2 dakika kaldı") == 1


def test_recovered_meeting_keeps_speaker_and_item(manager, repo, settings, clock):
    manager.start_meeting()
    manager.set_speaker("Ayşe")
    manager.next_item()
    restarted = MeetingManager(repo, settings, clock=clock)
    restarted.recover_active_meeting()
    assert restarted.meeting.active_speaker == "Ayşe Yılmaz"
    assert restarted.meeting.current_item.title == "Kalite raporu"
    restarted.add_transcript("Kurtarma sonrası konuşma")
    assert restarted.meeting.transcript[-1].speaker == "Ayşe Yılmaz"
