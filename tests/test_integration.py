"""Uçtan uca akış testleri (mikrofon gerektirmez)."""
import os
import subprocess
import sys
from pathlib import Path

from jarvis.meeting.manager import MeetingManager
from jarvis.meeting.models import MeetingStatus
from jarvis.voice.command_parser import IntentType
from jarvis.voice.listener import ScriptedListener, TextListener

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def test_full_text_flow(assistant, repo, console):
    assistant.run(ScriptedListener([
        "Jarvis, toplantıyı başlat",
        "Jarvis, katılımcılar: Kenan, Ayşe, Mehmet",
        "Jarvis, gündemi oku",
        "İlk konuşma cümlesi.",
        "Jarvis, sıradaki madde",
        "Jarvis, gündeme ekle: Bütçe, 15 dakika",
        "Jarvis, toplantıyı bitir",
        "Jarvis, çık",
    ]))
    output = console.getvalue()
    assert "Toplantı başlatıldı" in output
    assert "3 katılımcının yoklaması alındı" in output
    assert "Toplantı sona erdi" in output
    meetings = repo.list_meetings()
    assert len(meetings) == 1 and meetings[0]["status"] == MeetingStatus.ENDED
    stored = repo.get(meetings[0]["id"])
    assert [t.text for t in stored.transcript] == ["İlk konuşma cümlesi."]
    assert stored.agenda[-1].title == "Bütçe"


def test_t02_missing_agenda_file(assistant, settings, console):
    settings.agenda_file.unlink()
    response = assistant.handle_text("Jarvis toplantıyı başlat")
    assert response.ok and "Gündem boş" in response.message


def test_commands_without_meeting_give_guidance(assistant):
    response = assistant.handle_text("Jarvis gündemi oku")
    assert not response.ok and "toplantıyı başlat" in response.message


def test_unknown_command_is_not_guessed(assistant):
    assistant.handle_text("Jarvis toplantıyı başlat")
    response = assistant.handle_text("Jarvis bana fıkra anlat")
    assert response.intent == IntentType.UNKNOWN and not response.ok


def test_minutes_not_sent_before_meeting_ends(assistant):
    assistant.handle_text("Jarvis toplantıyı başlat")
    response = assistant.handle_text("Jarvis tutanağı gönder")
    assert not response.ok and "bitmeden" in response.message
    assert assistant.manager.meeting.email_status == {}


def test_speech_without_meeting_is_ignored(assistant):
    response = assistant.handle_text("Toplantı öncesi sohbet")
    assert not response.is_command and response.message == ""


def test_ctrl_c_keeps_meeting_and_recovers(assistant, repo, settings, clock, console):
    class InterruptingListener(ScriptedListener):
        def listen(self):
            line = super().listen()
            if line is None:
                raise KeyboardInterrupt
            return line

    assistant.run(InterruptingListener([
        "Jarvis toplantıyı başlat",
        "Jarvis Ayşe katıldı",
        "Önemli bir konuşma.",
    ]))
    assert "Toplantı kaydedildi" in console.getvalue()
    active = repo.get_active()
    assert active is not None and active.find_participant("Ayşe Yılmaz").present
    assert active.transcript[0].text == "Önemli bir konuşma."

    restarted = MeetingManager(repo, settings, clock=clock)
    assert restarted.recover_active_meeting() is not None
    assert restarted.end_meeting().ok


def test_exit_with_active_meeting_keeps_it(assistant, repo):
    assistant.run(ScriptedListener(["Jarvis toplantıyı başlat", "Jarvis çık"]))
    assert repo.get_active() is not None


def test_text_listener_eof_ends_loop():
    def raise_eof(prompt):
        raise EOFError
    assert TextListener(input_func=raise_eof).listen() is None


def _run_main(args, stdin_text, tmp_path):
    env = {**os.environ, "DATA_DIR": str(tmp_path / "data"), "OUTPUT_DIR": str(tmp_path / "out"),
           "LOGS_DIR": str(tmp_path / "logs"), "PYTHONIOENCODING": "utf-8", "JARVIS_ENV_FILE": "none"}
    (tmp_path / "data").mkdir(exist_ok=True)
    return subprocess.run([sys.executable, "main.py", *args], input=stdin_text, capture_output=True,
                          text=True, encoding="utf-8", cwd=PROJECT_ROOT, env=env, timeout=60)


def test_t11_text_mode_without_microphone(tmp_path):
    """T-11: mikrofonsuz ortamda --text modu aynı akışı klavyeden çalıştırır."""
    result = _run_main(["--text", "--no-tts"],
                       "Jarvis toplantıyı başlat\nJarvis yoklama al\nJarvis toplantıyı bitir\nJarvis çık\n",
                       tmp_path)
    assert result.returncode == 0, result.stderr
    assert "Toplantı başlatıldı" in result.stdout
    assert "Toplantı sona erdi" in result.stdout
    assert (tmp_path / "data" / "jarvis.db").exists()


def test_demo_mode_runs_and_uses_separate_database(tmp_path):
    result = _run_main(["--demo", "--no-tts"], "", tmp_path)
    assert result.returncode == 0, result.stderr
    assert "DEMO MODU" in result.stdout and "Toplantı sona erdi" in result.stdout
    assert (tmp_path / "out" / "demo" / "demo.db").exists()
    assert not (tmp_path / "data" / "jarvis.db").exists()
