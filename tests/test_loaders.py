"""Katılımcı/gündem dosyası yükleme: eksik, bozuk ve hatalı veri durumları."""
import json

from jarvis.meeting.loaders import load_agenda, load_participants


def test_missing_files_return_warnings(tmp_path):
    p = load_participants(tmp_path / "yok.json")
    a = load_agenda(tmp_path / "yok.json", 10)
    assert p.items == [] and "bulunamadı" in p.warnings[0]
    assert a.items == [] and "bulunamadı" in a.warnings[0]


def test_corrupt_json_does_not_crash(tmp_path):
    path = tmp_path / "participants.json"
    path.write_text('[{"name": "Ayşe", ', encoding="utf-8")
    result = load_participants(path)
    assert result.items == []
    assert "bozuk" in result.warnings[0]


def test_participants_valid_and_invalid_records(tmp_path):
    path = tmp_path / "p.json"
    path.write_text(json.dumps([
        {"name": "Ayşe Yılmaz", "email": "ayse@example.com"},
        {"name": "Ayşe Yılmaz", "email": "x@example.com"},
        {"email": "isimsiz@example.com"},
        {"name": "Mehmet", "email": "gecersiz-adres"},
        "Zeynep",
    ], ensure_ascii=False), encoding="utf-8")
    result = load_participants(path)
    assert [p.name for p in result.items] == ["Ayşe Yılmaz", "Mehmet", "Zeynep"]
    assert result.items[1].email == ""
    assert len(result.warnings) == 3


def test_participants_never_loaded_as_present(tmp_path):
    path = tmp_path / "p.json"
    path.write_text(json.dumps([{"name": "Ayşe", "present": True}]), encoding="utf-8")
    assert load_participants(path).items[0].present is False


def test_agenda_dict_format_and_default_minutes(tmp_path):
    path = tmp_path / "a.json"
    path.write_text(json.dumps({"agenda": [{"title": "Bütçe"}, {"title": "Plan", "minutes": "x"},
                                           {"title": "Risk", "planned_minutes": 20}]}), encoding="utf-8")
    result = load_agenda(path, 7)
    assert [(i.title, i.planned_minutes) for i in result.items] == [("Bütçe", 7), ("Plan", 7), ("Risk", 20)]
    assert len(result.warnings) == 1


def test_utf8_bom_file_is_accepted(tmp_path):
    path = tmp_path / "a.json"
    path.write_bytes('﻿[{"title": "Şirket içi eğitim"}]'.encode("utf-8"))
    assert load_agenda(path, 10).items[0].title == "Şirket içi eğitim"
