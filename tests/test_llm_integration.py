"""Faz 5 entegrasyonu: yeni komutlar, veri modeli, ayarlar, servis kurulumu ve komut satırı."""
from __future__ import annotations

import os
import subprocess
import sys
import urllib.error
from datetime import date, datetime
from pathlib import Path

import pytest

from config.settings import load_settings
from jarvis.llm.services import build_llm_services
from jarvis.meeting.models import AiDraft, DraftKind, Evidence, Meeting
from jarvis.voice.command_parser import CommandParser, IntentType
from tests.llm_fakes import FakeLlm

PARSER = CommandParser()
ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("text,intent,params", [
    ("Jarvis evet", IntentType.CONFIRM, {}),
    ("Jarvis, onaylıyorum", IntentType.CONFIRM, {}),
    ("Jarvis hayır", IntentType.REJECT_PENDING, {}),
    ("Jarvis vazgeç", IntentType.REJECT_PENDING, {}),
    ("Jarvis toplantıyı analiz et", IntentType.ANALYZE_MEETING, {}),
    ("Jarvis taslakları oku", IntentType.LIST_DRAFTS, {}),
    ("Jarvis taslak 4 onayla", IntentType.APPROVE_DRAFT, {"number": 4}),
    ("Jarvis taslak iki onayla", IntentType.APPROVE_DRAFT, {"number": 2}),
    ("Jarvis 3 numaralı taslağı onayla", IntentType.APPROVE_DRAFT, {"number": 3}),
    ("Jarvis taslak 5 reddet", IntentType.REJECT_DRAFT, {"number": 5}),
    ("Jarvis özet taslağını oku", IntentType.READ_AI_SUMMARY, {}),
    ("Jarvis, soru: bütçe kararı neydi?", IntentType.ASK_HISTORY, {"question": "bütçe kararı neydi"}),
])
def test_new_commands(text, intent, params):
    parsed = PARSER.parse(text)
    assert parsed.type == intent
    for key, value in params.items():
        assert parsed.params[key] == value


@pytest.mark.parametrize("text,intent", [
    ("Jarvis, oylama: bütçe lehte 3 aleyhte 1", IntentType.VOTE),
    ("Jarvis, görev iptal: rapor", IntentType.CANCEL_ACTION),
    ("Jarvis evet diyorum ama", IntentType.UNKNOWN),  # tam eşleşme gerekir: kısmi "evet" onay değildir
    ("Jarvis, evet karar: plan onaylandı", IntentType.UNKNOWN),
])
def test_existing_commands_unaffected_and_partial_yes_is_not_confirm(text, intent):
    assert PARSER.parse(text).type == intent


def test_yes_without_wake_word_is_speech():
    assert PARSER.parse("Evet") is None and PARSER.parse("evet, onaylıyorum") is None


def test_ai_draft_roundtrip():
    meeting = Meeting(title="x", started_at=datetime(2026, 10, 14, 10))
    meeting.ai_drafts.append(AiDraft(1, DraftKind.ACTION, "raporu hazırlasın",
                                     [Evidence("L2", datetime(2026, 10, 14, 10, 1), "Kenan", "Ahmet raporu ...")],
                                     owner="Ahmet Yılmaz", due_date=date(2026, 10, 21), due_text="haftaya",
                                     source="llm:qwen3:4b", notes=["not"]))
    meeting.ai_summary = {"overall": "özet", "method": "llm:qwen3:4b"}
    restored = Meeting.from_dict(meeting.to_dict())
    assert restored.ai_drafts[0].to_dict() == meeting.ai_drafts[0].to_dict()
    assert restored.ai_summary == meeting.ai_summary
    old = meeting.to_dict()
    del old["ai_drafts"], old["ai_summary"]  # Faz 4 kayıtları da açılabilmeli
    assert Meeting.from_dict(old).ai_drafts == []


def test_llm_settings_defaults_are_safe(monkeypatch):
    monkeypatch.setenv("JARVIS_ENV_FILE", "none")
    for key in ("LLM_ENABLED", "OLLAMA_HOST", "LLM_MODEL", "LLM_ALLOW_REMOTE", "LLM_COMPARE_MODELS"):
        monkeypatch.delenv(key, raising=False)
    s = load_settings()
    assert s.llm_enabled is False and s.llm_allow_remote is False
    assert s.ollama_host == "http://127.0.0.1:11434" and s.llm_model == "qwen3:4b"
    assert s.llm_compare_models == ["qwen3.5:4b", "qwen3:4b"] and s.llm_temperature == 0.0


def test_llm_settings_from_env(monkeypatch):
    monkeypatch.setenv("JARVIS_ENV_FILE", "none")
    monkeypatch.setenv("LLM_ENABLED", "true")
    monkeypatch.setenv("LLM_MODEL", "qwen3.5:4b")
    monkeypatch.setenv("LLM_COMPARE_MODELS", "a:1, b:2")
    monkeypatch.setenv("LLM_NUM_CTX", "999999")
    monkeypatch.setenv("LLM_ROUTER_TIMEOUT_S", "7,5")
    s = load_settings()
    assert s.llm_enabled and s.llm_model == "qwen3.5:4b" and s.llm_compare_models == ["a:1", "b:2"]
    assert s.llm_num_ctx == 131072 and s.llm_router_timeout_s == 7.5


def test_services_disabled_by_default(settings):
    services = build_llm_services(settings, PARSER)
    assert not services.enabled and services.router is None and not services.analyzer.uses_llm


def test_services_refuse_remote_host(settings):
    remote = settings.with_overrides(llm_enabled=True, ollama_host="http://10.0.0.5:11434")
    services = build_llm_services(remote, PARSER)
    assert not services.enabled and "yerel değil" in services.status


def test_services_enabled_with_missing_model_warns(settings):
    enabled = settings.with_overrides(llm_enabled=True, llm_model="qwen3.5:4b")
    services = build_llm_services(enabled, PARSER, client=FakeLlm({}, models=["qwen3:4b"]))
    assert services.enabled and services.router is not None and services.analyzer.uses_llm
    assert "ollama pull qwen3.5:4b" in services.status


def _run(*args, env_extra=None):
    env = {**os.environ, "JARVIS_ENV_FILE": "none", "PYTHONIOENCODING": "utf-8", **(env_extra or {})}
    return subprocess.run([sys.executable, "main.py", *args], cwd=ROOT, capture_output=True, text=True,
                          encoding="utf-8", timeout=120, env=env)


def test_cli_llm_check_reports_unreachable_server(monkeypatch, capsys):
    """Gerçek kapalı port yerine sahte opener (Windows/Linux farkı yok)."""
    import main as cli
    from jarvis.llm import client as llm_client

    class Refused:
        def open(self, request, timeout=None):
            raise urllib.error.URLError(ConnectionRefusedError(10061, "bağlantı reddedildi"))

    monkeypatch.setenv("JARVIS_ENV_FILE", "none")
    monkeypatch.setenv("OLLAMA_HOST", "http://127.0.0.1:11434")
    monkeypatch.setattr(cli, "configure_logging", lambda *a, **k: None)
    monkeypatch.setattr(llm_client.urllib.request, "build_opener", lambda *handlers: Refused())
    assert cli.run_llm_check() == 1
    assert "ulaşılamadı" in capsys.readouterr().out


def test_cli_llm_check_refuses_remote(tmp_path):
    proc = _run("--llm-check", env_extra={"OLLAMA_HOST": "http://10.0.0.5:11434", "LOGS_DIR": str(tmp_path)})
    assert proc.returncode == 1 and "yerel değil" in proc.stdout


def test_cli_analyze_rule_based(tmp_path):
    db = tmp_path / "t.db"
    env = {"DB_PATH": str(db), "OUTPUT_DIR": str(tmp_path / "out"), "LOGS_DIR": str(tmp_path / "logs"),
           "PARTICIPANTS_FILE": str(ROOT / "data" / "participants.example.json"),
           "AGENDA_FILE": str(ROOT / "data" / "agenda.example.json")}
    from config.settings import Settings
    from jarvis.meeting.manager import MeetingManager
    from jarvis.meeting.storage import MeetingRepository

    settings = Settings(participants_file=ROOT / "data" / "participants.example.json",
                        agenda_file=ROOT / "data" / "agenda.example.json", db_path=db)
    manager = MeetingManager(MeetingRepository(db), settings)
    manager.start_meeting("Analiz testi")
    manager.add_transcript("Yeni planı pazartesi uygulamaya karar verdik.")
    manager.add_transcript("Mehmet Öztürk raporu cumaya kadar hazırlasın.")
    manager.end_meeting()
    proc = _run("--analyze", env_extra=env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Yerel LLM kapalı" in proc.stdout and "Taslak 1 (karar)" in proc.stdout
    assert "Taslak 2 (görev)" in proc.stdout and "Mehmet Öztürk" in proc.stdout
    stored = MeetingRepository(db).get_last_ended()
    assert len(stored.ai_drafts) == 2 and stored.decisions == [] and stored.action_items == []
