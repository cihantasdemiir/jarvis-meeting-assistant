# Teknik Mimari

Bu belge depodaki kaynak koda göre yazıldı. Ayrıntılar `ARCHITECTURE.md` ve `LLM_MIMARISI.md` içindedir.

## 1. Genel yapı

```
main.py                     Komut satırı, kip seçimi (--text, --demo, --demo-live, araçlar)
config/settings.py          .env → Settings (dataclass), with_overrides(), ensure_dirs()
jarvis/app.py               Fabrika: build_assistant(), demo_settings(), demo_live_settings()
jarvis/assistant.py         JarvisAssistant: tek girdi → komut/diyalog/transkript → yanıt; run() ana döngü
jarvis/dialog.py            DialogManager: bekleyen soru (Clarification), bekleyen onay, hitapsız yanıt penceresi
jarvis/dialog_flow.py       DialogFlow: çok turlu sorular (kişi seçimi, eksik görev alanları, yoklama)
jarvis/voice/               Ses yakalama, STT, komut ayrıştırma, TTS, sesli kesme
jarvis/meeting/             Veri modeli, iş kuralları (MeetingManager), SQLite, konuşmacı/transkript
jarvis/reports/             PDF, ICS, Excel, tutanak servisi (MinutesService)
jarvis/mail/mailer.py       SMTP gönderimi, alıcı doğrulama, bağlantı kontrolü
jarvis/llm/                 İsteğe bağlı yerel LLM (Ollama): yönlendirici, analiz, güvenlik kontrolleri
jarvis/utils/               Türkçe metin (tr_lower, best_name_match...), tarih/saat ayrıştırma
jarvis/diagnostics.py       PipelineTrace: girdi zinciri tanılaması (varsayılan metinsiz)
```

İlke: **İş kuralları sesten bağımsızdır.**
- Klavye, mikrofon ve testler aynı `JarvisAssistant.handle_text()` girişini kullanır.
- Gerçek mikrofon olmadan da bütün iş mantığı test edilebilir.

## 2. Ana uygulama akışı

1. `main.main()` argümanları ayrıştırır. Araç seçeneklerini (`--list-mics`, `--minutes` vb.) çalıştırıp çıkar; ya da `load_settings()` ile ayarları okur.
2. `build_assistant()` bileşenleri bağlar:
   - `MeetingRepository`, `MeetingManager`, `CommandParser`;
   - ses motoru (`create_speech_engine`), `MinutesService`;
   - `build_llm_services`, `DialogManager`, `PipelineTrace`.
3. Giriş kaynağı seçilir: sesli kipte `build_voice_listener()` → `VoiceListener`; yoksa `TextListener`.
4. `JarvisAssistant.run(source)`:
   - açılışta `startup()` yarım toplantıyı kurtarır (`MeetingManager.recover_active_meeting`);
   - `TimeboxMonitor` iş parçacığı %80 uyarısını izler;
   - her satırda `handle_text()` çağrılır, yanıt `respond()` ile söylenir.
5. **Çıkış:** Ctrl+C gelirse `on_interrupt()` toplantıyı kaydeder ve ara tutanak üretir. Kuyrukta bekleyen tanınmış konuşmalar `_drain_source()` ile transkripte yazılır; komut olsalar bile çalıştırılmazlar.

`handle_text()` sırası:

```
metin ─► DialogFlow.handle()           bekleyen soru/onay varsa yanıt olarak tüketilir (transkripte yazılmaz)
      └► CommandParser.parse()         "Jarvis" ile başlıyorsa Intent; değilse None
            ├─ Intent ─► handle_intent() ─► MeetingManager / MinutesService işleyicisi
            │                           └─ UNKNOWN ─► (LLM açıksa) LlmCommandRouter → her zaman onay
            └─ None   ─► _handle_unaddressed() ─► _handle_speech() ─► transkript
```

## 3. Ses tanıma (STT)

- **`audio_capture.py`:**
  - `PyAudioSource` mikrofondan ses okur.
  - `Segmenter` enerji tabanlı konuşma algılamasıyla bölüt üretir (`SegmenterConfig`: VAD eşiği, sessizlik, en uzun bölüt).
  - `AudioCapture` tek yakalama servisidir; komut ve transkript için mikrofon iki kez açılmaz.
- **`stt.py`:**
  - `SpeechToText` arayüzünü ve `GoogleSpeechToText` (tr-TR) sınıfını içerir. `create_stt()` motoru seçer.
  - Hatalar ayrı türlerdedir: `SttNetworkError`, `SttUnavailable`.
  - `whisper` seçeneği `SttUnavailable` döndürür; uygulanmadı.
- **`voice_listener.py`:**
  - `VoiceListener` bölütleri arka plan iş parçacığında tanır ve `Utterance` üretir. `Utterance` konuşmanın başladığı ve bittiği anı taşır, böylece transkript zamanı konuşma anıdır.
  - `choose_transcript()` STT alternatiflerinden hitaplı olanı seçer.
- **Yankı koruması:** TTS çalarken ve bittikten kısa süre sonra mikrofon bölütleri yok sayılır (`is_muted`).
- **Sesli kesme:** `barge_in.py` / `BargeInDetector` asistan konuşurken yalnızca "Jarvis, dur" gibi durdurma komutlarını dinler.

## 4. TTS

- **Seçim:** `tts_factory.create_speech_engine()` motoru seçer.
  - `--no-tts` veya `TTS_ENGINE=none` → yalnızca konsol.
  - `pocket` (varsayılan) → `PocketSpeechEngine` (`pocket_speech.py`, `pocket_backend.py`, `audio_output.py`).
  - `pyttsx3` → `SpeechEngine` (Windows SAPI5).
- **Hata durumu:** Pocket kurulu değilse veya yüklenemezse konsola düşer; İngilizce sese geçmez.
- **Metin hazırlığı:** `speech_text.to_speech_text()` seslendirilecek metni hazırlar; özgün yanıt değişmez.
- **Kuyruk:** Seslendirme kuyruğu sınırlıdır. İptal (`cancel_all`) ve kapanış (`shutdown`) desteklenir.
- **Bitiş bildirimi:** Seslendirme bitince `on_speech_end` → `DialogManager.speech_ended()` çağrılır. Hitapsız yanıt penceresi sorunun okunması bittikten sonra başlar.

## 5. Komut ayrıştırma

`voice/command_parser.py` → `CommandParser`:
- `split_wake_word()` uyandırma kelimesini ve `WAKE_WORD_ALIASES` varyantlarını ayırır.
- `parse()` / `parse_command()` düzenli ifade kurallarıyla (`_RULES`) metni `Intent(IntentType, params)` nesnesine çevirir. Ayrıştırma deterministiktir.
- Tanınmayan komut `UNKNOWN` olur; yanlış bir komuta dönüştürülmez.
- Eksik, kesik veya STT bozulması şüpheli komutlar çalıştırılmaz: `truncation.py`, `stt_suggestion`.
- Hitapsız ama komut kalıbındaki cümle (`unaddressed.py`) transkripte "komut gibi duyuldu" diye işaretlenir; kendiliğinden çalışmaz.

## 6. Diyalog yönetimi

- **`DialogManager` (`dialog.py`):**
  - Tek bekleyen soru (`Clarification`) ve tek bekleyen onay (`PendingConfirmation`) tutar.
  - Zaman aşımı `DIALOG_TIMEOUT_S`; hitapsız yanıt penceresi `DIALOG_UNADDRESSED_WINDOW_S`.
- **`DialogFlow` (`dialog_flow.py`):**
  - Kişi seçimi: `open_choose()` / `choice_question()`. Yanıt tam ad, soyad veya sıra sözcüğü olabilir; `_ordinal_choice()` sıra sözcüklerini çözer.
  - Eksik görev alanları ve eksik yoklama soruları da burada yönetilir.
  - Her yanıt sonunda **onaya** dönüşür. Kayıt yalnızca "evet" ile, kurallı komut zinciri üzerinden yapılır.
  - Seçim onay beklerken seçimin tekrarı veya değişikliği diyaloğa ait sayılır ve transkripte yazılmaz.
- **Kesinti:** Açık ve bağımsız yeni komut bekleyen soruyu düşürür.

## 7. Konuşmacı ve transkript

- **`SpeakerTracker` (`speaker_tracker.py`):**
  - `SpeakerTurn` kayıtlarından kişi başı süre, söz sayısı ve söz almayanları hesaplar.
  - Söz başkasına geçince önceki tur kapanır; toplantı bitince açık tur kapanır.
  - Kurtarmada program kapalı kaldığı süre sayılmaz.
- **`TranscriptService` (`transcript_service.py`):**
  - Komut olmayan konuşmayı zaman, konuşmacı, gündem maddesi ve tür (`TranscriptKind`) ile ekler.
  - Konuşmacı yoksa "Belirtilmemiş" yazılır.
- **Kişi eşleştirme:** `MeetingManager.known_names()` / `resolve_known()` toplantı listesini ve rehberi birlikte tarar. `utils.turkish_text.best_name_match()` sırayla tam eşleşme, sözcük alt kümesi, Türkçe ekler ve bulanık eşleşme dener; belirsizse aday listesi döner.

## 8. Toplantı veritabanı

`meeting/storage.py` → `MeetingRepository` (SQLite):
- **`meetings` tablosu:** toplantının tamamı JSON `payload` olarak tutulur. Listeleme için başlık, tarih ve durum ayrı sütunlardadır.
- **`action_items` tablosu:** görevlerin sorgulanabilir kopyası. Devreden görevler buradan okunur. Görevlerin kalıcı benzersiz kimliği vardır.
- **Bütünlük:** İki tablo aynı işlemde güncellenir (`BEGIN IMMEDIATE`). WAL günlüğü ve `synchronous=FULL` ile kesintide son tamamlanmış kayıt korunur.
- **Kayıt anı:** `MeetingManager` her durum değişikliğinden sonra `save()` çağırır.
- **Veri modeli** (`meeting/models.py`): `Meeting`, `Participant`, `AgendaItem`, `Decision`, `ActionItem`, `Vote`, `SpeakerTurn`, `TranscriptEntry`, `AiDraft`. Hepsinin JSON dönüşümü testlidir.

## 9. PDF üretimi

- **`reports/pdf_generator.py` → `MinutesPdfGenerator`:** ReportLab platypus ile A4 tutanak üretir. Yalnızca çizim yapar; hangi verinin basılacağına `MinutesService.build_context()` karar verir.
  - **Görünüm:** Yapılandırılabilir kurumsal şablon. Her sayfada logo alanı, "TOPLANTI TUTANAĞI", şirket unvanı ve "Sayfa X / Y" bulunur.
  - **Bölümler:** "01 | Başlık" biçiminde numaralıdır; boş bölümler basılmaz. Ek A yalnızca transkript varsa eklenir.
  - **Dosya yazımı:** Önce geçici dosyaya yazılır, sonra taşınır; yarım PDF kalmaz.
- **`reports/fonts.py`:** Türkçe harfleri içeren TTF fontu bulur. Arama sırası: `.env`, `assets/fonts`, Windows Arial/Calibri, Linux DejaVu.
- **LLM taslakları:** `Meeting.ai_drafts` resmî tutanağa basılmaz.

## 10. SMTP, ICS ve Excel

- **`mail/mailer.py`:**
  - `collect_recipients()` adresleri doğrular; izinli alan adı ve tekrar kontrolü yapar.
  - `build_minutes_message()` düz metin ve HTML gövde ile PDF eki hazırlar.
  - `Mailer.send()` STARTTLS veya SSL ile gönderir ve hatayı aşamasıyla bildirir (`Stage`).
  - `Mailer.check_connection()` `--smtp-check` içindir; e-posta göndermez.
  - Deneme kipinde `.eml` taslağı yazar. Şifre ve kullanıcı adı günlüğe yazılmaz (`mask_address`).
- **`reports/ics.py`:** RFC 5545 takvim daveti üretir. UID toplantıya göre sabittir; değişiklikte `SEQUENCE` artar, kopya etkinlik oluşmaz.
- **`reports/excel_export.py` → `TaskExporter`:** Görev listesini `.xlsx` olarak yazar. Formül enjeksiyonuna karşı korumalıdır ve veritabanına yazmaz.
- **`reports/minutes_service.py` → `MinutesService`:** `create_pdf`, `send_minutes` (tekrar gönderim koruması, `force`), `create_invite`, `export_tasks`, `finalize`.

## 11. LLM entegrasyonu (isteğe bağlı)

- **`llm/services.py`:** `build_llm_services()` kurar. `LLM_ENABLED=false` ise `disabled_services()` döner.
- **`llm/client.py`:** `OllamaClient` yalnızca yerel adrese bağlanır. `check_privacy()` uzak veya bulut modelini `LLM_ALLOW_REMOTE` olmadan reddeder.
- **`llm/router.py`:** `LlmCommandRouter` yalnızca `UNKNOWN` komutları yorumlar. Önerdiği işlem **her zaman onaylanır**; önemli işlemler (`EXPLICIT_ONLY_INTENTS`) LLM önerisiyle açılmaz.
- **`llm/analyzer.py`:** `MeetingAnalyzer` transkriptten karar/görev/özet **taslakları** (`AiDraft`) çıkarır. LLM yoksa `rule_based_candidates` kullanılır. Taslak yalnızca "taslak N onayla" ile kayda dönüşür.
- **Güvenlik katmanları:** `utterance_guard.py`, `grounding.py`, `summary_guard.py`. Uydurma kişi, tarih veya kayıt engellenir.

## 12. Demo veri izolasyonu

- **`--demo`** (`demo_settings`):
  - Örnek katılımcı ve gündem dosyalarıyla çalışır.
  - Veritabanı her çalıştırmada silinip yeniden oluşturulan `output/demo/demo.db`'dir.
  - Toplantılar `is_demo` olarak işaretlenir. E-posta gönderilmez.
- **`--demo-live`** (`demo_live_settings`):
  - Gerçek ses, gündem ve katılımcı dosyalarıyla çalışır.
  - Her çalıştırma yeni `data/demo_live/<zaman>/jarvis.db` ve `output/demo_live/<zaman>/` açar. Normal veritabanı açılmaz.
  - E-posta gönderilmez: otomatik gönderim kapalı, `MinutesService` deneme kipinde.

## 13. Güvenlik ve hata yönetimi

| Konu | Uygulama |
|---|---|
| Gizli bilgiler | `.env` Git dışında; SMTP şifresi ve içerik günlüğe yazılmaz; `--smtp-check` şifreyi göstermez |
| Yanlış işlem | Belirsiz kişi, tarih veya oy sayısında tahmin yok; LLM önerileri ve katılımcı düzeltmeleri onaylı |
| Yanlış alıcı | `MAIL_ALLOWED_DOMAINS`, `MAIL_REDIRECT_TO`, tekrar gönderim koruması, `AUTO_SEND_MINUTES=false` varsayılanı |
| Veri kaybı | Her değişiklikte SQLite kaydı; WAL; Ctrl+C'de ara tutanak; açılışta kurtarma; PDF/SMTP hatası toplantıyı silmez |
| Donanım hataları | Mikrofon kopması bildirilir ve yeniden denenir; STT ağ hatası uygulamayı kapatmaz; TTS hatasında konsol sürer |
| Bozuk dosyalar | Katılımcı/gündem JSON hatası veya eksik dosya uyarıyla geçilir; font yoksa açıklayıcı hata; logo bozuksa uyarı |
| Gizlilik | Google STT sesi Google'a gönderir (belgelendi); LLM yalnızca yerel; tanılama günlüğü varsayılan metinsiz (`PIPELINE_TRACE_TEXT=false`) |
