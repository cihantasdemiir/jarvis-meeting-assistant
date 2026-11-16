"""Ortak test araçları: geçici ayarlar, sabit saat, örnek veri dosyaları."""
from __future__ import annotations

import io
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from config.settings import Settings
from jarvis.assistant import JarvisAssistant
from jarvis.mail.mailer import Mailer
from jarvis.meeting.manager import MeetingManager
from jarvis.meeting.storage import MeetingRepository
from jarvis.reports.minutes_service import MinutesService
from jarvis.voice.command_parser import CommandParser
from jarvis.voice.speech_engine import SpeechEngine

PARTICIPANTS = [
    {"name": "Kenan Çetin", "email": "kenan@example.com", "role": "Başkan"},
    {"name": "Ayşe Yılmaz", "email": "ayse@example.com"},
    {"name": "Mehmet Öztürk", "email": "mehmet@example.com"},
    {"name": "Ahmet Yılmaz", "email": "ahmet@example.com"},
]

AGENDA = [
    {"title": "Üretim planlaması", "planned_minutes": 15, "presenter": "Ayşe Yılmaz"},
    {"title": "Kalite raporu", "planned_minutes": 10},
    {"title": "Diğer konular"},
]


class FakeClock:
    """Testlerde zamanı elle ilerletmek için."""

    def __init__(self, start: datetime = datetime(2026, 10, 14, 10, 0, 0)):
        self.current = start

    def __call__(self) -> datetime:
        return self.current

    def advance(self, **kwargs) -> None:
        self.current += timedelta(**kwargs)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    data = tmp_path / "data"
    data.mkdir()
    (data / "participants.json").write_text(json.dumps(PARTICIPANTS, ensure_ascii=False), encoding="utf-8")
    (data / "agenda.json").write_text(json.dumps(AGENDA, ensure_ascii=False), encoding="utf-8")
    return Settings(
        base_dir=tmp_path,
        data_dir=data,
        output_dir=tmp_path / "output",
        logs_dir=tmp_path / "logs",
        db_path=data / "test.db",
        participants_file=data / "participants.json",
        agenda_file=data / "agenda.json",
        meeting_title="Test Toplantısı",
        meeting_chair="Kenan Çetin",
        meeting_location="Oda 1",
    )


@pytest.fixture
def repo(settings: Settings) -> MeetingRepository:
    return MeetingRepository(settings.db_path)


@pytest.fixture
def manager(repo: MeetingRepository, settings: Settings, clock: FakeClock) -> MeetingManager:
    return MeetingManager(repo, settings, clock=clock)


@pytest.fixture
def console() -> io.StringIO:
    return io.StringIO()


@pytest.fixture
def minutes(repo: MeetingRepository, settings: Settings, clock: FakeClock) -> MinutesService:
    """Testlerde e-posta her zaman dry-run kipindedir; gerçek SMTP kullanılmaz."""
    return MinutesService(repo, settings, mailer=Mailer(settings, dry_run=True), clock=clock)


@pytest.fixture
def assistant(manager: MeetingManager, settings: Settings, console: io.StringIO,
              minutes: MinutesService) -> JarvisAssistant:
    parser = CommandParser(settings.wake_word, settings.wake_word_aliases)
    speech = SpeechEngine(enabled=False, output=console)
    return JarvisAssistant(manager, parser, speech, minutes=minutes)
