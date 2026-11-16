# Kaynak Kod Denetimi (SOURCE_AUDIT)

Denetim tarihi: 14 Ekim 2026 · Denetleyen: Claude (kaynak kod düzeyinde, depolar klonlanarak)

## Özet karar

| Depo | Lisans | Kalite / test | Karar |
|---|---|---|---|
| A — MinuteCraft | MIT | Analiz motorunda sabit/uydurma çıktı | **Kod alınmadı.** Yalnızca yapı fikri |
| B — MeetingScribe | MIT | İyi; 34 test dosyası, ilgili 26 test burada geçti | **Ses mimarisi uyarlanacak (Faz 4/5)** |
| C — Mortalbeats J.A.R.V.I.S | **Lisans yok** | 1 commit, toplantı ile ilgisiz | **Kod alınmadı** |
| D — AI Meeting Copilot Agent | MIT | 1 commit, test yok, openwakeword + bulut API | Yalnızca mimari referans |
| E — WhisperOverlay | **Lisans dosyası yok** (README'de "MIT" yazıyor) | 1 commit, test yok | **Kod alınmadı**, referans |
| F — dateparser | BSD-3-Clause | Olgun kütüphane | **Kullanılmadı**: Türkçe göreli ifadelerde başarısız (aşağıda) |
| G — Meeting Assistant | **Lisans yok** | 1 commit, fpdf + Helvetica | **Kod alınmadı** |

Sonuç: Projenin çekirdeği (toplantı yönetimi, komut ayrıştırma, SQLite, PDF, SMTP) temiz ve test
edilebilir kendi kodumuzla yazılıyor. Dışarıdan uyarlanacak tek kod tabanı MeetingScribe'ın ses
yakalama + faster-whisper katmanıdır (MIT, telif bildirimi korunarak).

---

## A — MinuteCraft
- **Repo:** https://github.com/sanikamurhe/meeting-minutes-generator
- **Commit:** `ed8178468bc4b7430bb6ad759c37f230ca82f225` (depoda tek commit)
- **Lisans:** MIT — Copyright (c) 2026 MinuteCraft Contributors. Kopyalanan kod olursa lisans metni korunmalı.
- **İncelenen dosyalar:** `backend/ai_service.py`, `backend/export_service.py`, `backend/database.py`, `backend/models.py`, `tests/`
- **Gerçekten çalışan işlevler:** FastAPI + SQLite CRUD, ReportLab ile PDF iskeleti.
- **Bulunan sorunlar:**
  - `ai_service.py` satır 298: LLM yokken gündem maddeleri **sabit İngilizce cümlelerle** dolduruluyor
    ("Review project progress and dashboard design" …). Karar bulunamadığında varsayılan karar üretiliyor.
    Bu, talimattaki "bilgi uydurma" yasağına aykırı.
  - `export_service.py` yalnızca Helvetica kullanıyor; standart Type1 fontlar `ğ ş ı İ` içermediği için
    Türkçe tutanakta karakterler bozulur.
  - Analiz kuralları İngilizceye özgü; Türkçe metinde işe yaramaz.
- **Projeye alınan:** Kod alınmadı. Yalnızca "toplantı verisi → yapılandırılmış rapor" katman ayrımı fikri.
- **Riskler:** Doğrudan kullanılırsa tutanakta gerçekte alınmamış kararlar görünebilir (kritik).

## B — MeetingScribe
- **Repo:** https://github.com/elmoghany/meeting-scribe
- **Commit:** `3b5441f7d61545bee2df3213a630773a64284fdd`
- **Lisans:** MIT — Copyright (c) 2026 MeetingScribe contributors. Bağımlılıklar da MIT/BSD/Apache
  (paramiko LGPL, ancak o kısım bizim için gereksiz).
- **İncelenen dosyalar:** `app/pipeline/asr.py`, `app/capture/recorder.py`, `app/session.py`, `app/device.py`,
  `app/integrations/ics.py`, `pyproject.toml`, ilgili testler.
- **Çalıştırılan testler:** `test_recorder.py`, `test_session.py`, `test_device_asr.py`, `test_ics.py` → **26/26 geçti**
  (gerçek mikrofon olmadan; ses aygıtı kısımları testlerde taklit ediliyor).
- **Mimari bulgular (talimattaki sorular):**
  - *Segmentler nasıl oluşuyor?* Sabit uzunlukta pencereler (`window_sec=5`). Konuşma sınırını
    dikkate almıyor; bir komut iki pencereye bölünebilir. **JARVIS için sessizlik tabanlı (VAD/enerji)
    segmentasyon gerekli** — bu kısım uyarlanırken değiştirilecek.
  - *Whisper ana akışı bloke ediyor mu?* Hayır. Yakalama iş parçacığı pencereyi `queue.Queue(maxsize=64)`'e
    koyar, ayrı bir tüketici iş parçacığı Whisper'ı çalıştırır. **Bu desen alınacak.**
  - *Bellek:* Pencere tamponu sınırlı; ancak kuyruk dolarsa pencere **sessizce atılıyor**. JARVIS'te veri kaybı
    kabul edilemez → kuyruk dolduğunda log + kullanıcı uyarısı eklenecek.
  - *Geçici dosyalar:* Toplantı boyunca tüm ses `mic.wav` olarak diske yazılıyor. Kurtarma için faydalı,
    fakat KVKK açısından ses kaydının saklanıp saklanmayacağı ayarlanabilir olmalı.
  - *CPU gecikmesi:* `asr.py` CTranslate2 ile otomatik cihaz seçer, GPU başarısızsa CPU int8'e düşer;
    canlı modda `beam_size=1`, `vad_filter=True`. **Bu yükleme/geri düşme mantığı alınacak.**
- **Projeye alınacak (Faz 4/5):** `Transcriber` sınıfının tembel model yükleme, CPU'ya geri düşme ve
  güven puanı (`avg_logprob → 0..1`) mantığı; kuyruk + tüketici iş parçacığı deseni.
- **Alınmayacak:** FastAPI sunucusu, diarization (pyannote/resemblyzer — kapsam dışı), Zoom/bot,
  Cornell SLURM uzaktan çalıştırma, sistem sesi (loopback) kaydı (fiziksel toplantı odası için gereksiz).
- **Not:** `ics.py` yalnızca .ics **okuma** yapıyor; bizim ihtiyacımız **üretme** (G-ICS) — yeniden kullanılmayacak.

## C — Mortalbeats J.A.R.V.I.S
- **Repo:** https://github.com/Mortalbeats/J.A.R.V.I.S
- **Commit:** `49fdd43bf4664f72e0575563713d9f76fa893491` (tek commit)
- **Lisans:** **Yok.** Lisanssız kod, telif sahibinin izni olmadan kopyalanamaz.
- **İncelenen:** `jarvis-assistant/jarvis/assistant.py`, `speech.py`, dizin yapısı.
- **Bulgular:** Genel amaçlı asistan (el takibi, görüntü, LLM sohbeti). Toplantı işlevi yok. Tek test dosyası el
  takibi için. `speech.py` pyttsx3 → edge-tts geri düşmesi içeriyor (fikir olarak makul).
- **Projeye alınan:** Hiç kod alınmadı. TTS'in başarısız olduğunda konsola düşmesi fikri kendi kodumuzda var.

## D — AI Meeting Copilot Agent
- **Repo:** https://github.com/SAMBITMALLICK2003/AI-Meeting-Copilot-Agent
- **Commit:** `81c049479de5014be80e04c65c7a31e2b2f2a71c` (tek commit) · **Lisans:** MIT (c) 2025 Sambit Mallick
- **İncelenen:** `Agent_Framework/audio/wake_word.py` (212 satır), `tools/meeting_content_tools.py` (94 satır)
- **Bulgular:** Uyandırma kelimesi `openwakeword` + özel eğitilmiş model ile ses düzeyinde algılanıyor.
  "Jarvis" için Türkçe telaffuza uygun hazır model yok; eğitmek kapsam dışı. Gemini/Speechmatics bağımlı.
- **Projeye alınan:** Kod yok. Karar: uyandırma kelimesi **STT metni üzerinde** algılanır (PDF G-01 ile uyumlu),
  yanlış tanıma varyantları `.env` ile ayarlanır.

## E — WhisperOverlay
- **Repo:** https://github.com/ArseniiB-o/whisper-overlay
- **Commit:** `ab987a7d904beee32661d77f31447ad03e638e7c` · **Lisans:** README'de "MIT" yazıyor ama **LICENSE dosyası yok**
- **İncelenen:** `core/audio.py` (sounddevice callback ile tampon), `core/transcriber_local.py` (faster-whisper, VAD parametreleri)
- **Bulgular:** Basit ve okunaklı; varsayılan dil Rusça; test yok. MeetingScribe aynı işi testli yapıyor.
- **Projeye alınan:** Kod yok. `sounddevice` callback yaklaşımı Faz 4'te alternatif olarak değerlendirilecek.

## F — dateparser
- **Repo:** https://github.com/Workable/python-dateparser · **Commit:** `8575a5967e3ca1d2ca0be0b06a1ce60d243ad73f`
- **Lisans:** BSD-3-Clause (Scrapinghub)
- **Test (pip 1.4.3, taban 14.10.2026):**

| İfade | Sonuç | Durum |
|---|---|---|
| 15 Ekim | 2026-10-15 | ✅ |
| yarın | 2026-10-15 | ✅ |
| haftaya | 2026-10-21 | ✅ |
| cuma | 2026-10-16 | ✅ |
| 21 Ekim 2026 | 2026-10-21 | ✅ |
| 3 gün sonra | 2026-10-17 | ✅ |
| **gelecek cuma** | None | ❌ |
| **iki gün sonra / üç gün sonra** | None | ❌ |
| **ayın sonunda** | None | ❌ |

- **Karar:** Talimatın kendi örneklerinin üçünü çözemediği ve cümle içinden tarih çıkarma ("raporu
  **haftaya kadar** hazırlasın") yapmadığı için bağımlılık eklenmedi. `jarvis/utils/dates.py` (Faz 2) kendi
  deterministik kurallarımızla yazılacak; yukarıdaki tablo test vakası olarak kullanılacak.

## G — Meeting Assistant
- **Repo:** https://github.com/onwurahben/meeting-assistant · **Commit:** `b72f36dd579751a22b7f3ce8ff12050e660df3b9`
- **Lisans:** **Yok.** · Gradio + Deepgram + fpdf (Helvetica — Türkçe karakter sorunu) + smtplib.
- **Projeye alınan:** Kod yok. SMTP akışı standart `smtplib` ile kendi kodumuzda yazılacak.

## Faz 4'te MeetingScribe'dan alınanlar
Kod kopyalanmadı; aşağıdaki **tasarım fikirleri** sıfırdan yazılan `jarvis/voice/` modüllerinde kullanıldı:
- Yakalama iş parçacığı ile tanıma iş parçacığının bir kuyrukla ayrılması (yakalama asla tanımayı beklemez)
- Ses aygıtı kütüphanesinin geç (lazy) yüklenmesi: kütüphane yoksa uygulama yine açılır

Bilinçli farklar (denetimde tespit edilen sorunlar nedeniyle):
- Sabit pencere yerine sessizlikte biten bölütler
- Kuyruk dolunca ses atılması yerine kesintide bekletme ve diske yedekleme
- Asistan sesine karşı susturma
- Mikrofon kopmasında yeniden bağlanma

## Faz 5a (yerel LLM)
- Dış depodan kod alınmadı. Ollama ile yalnızca belgelenmiş REST API (`/api/chat`, `/api/tags`,
  `/api/ps`, `/api/version`) üzerinden, Python standart kütüphanesi (`urllib`) ile konuşulur. `ollama`
  Python paketi kullanılmadı (ek bağımlılık yok).
- D — AI Meeting Copilot Agent'taki "metinden toplantı içeriği çıkarma" fikri yalnızca kavramsal olarak
  dikkate alındı. Bu depo Gemini'ye bağlıdır ve doğrulama katmanı yoktur; JARVIS'te her öğe için kanıt
  satırı ve doğrulama zorunlu kılındı.
- A — MinuteCraft'ın LLM yokken uydurma içerik üreten yaklaşımı **bilinçli olarak tersine çevrildi**:
  LLM yoksa kural tabanlı çıkarıcı yalnızca açık işaretli cümlelerden aday üretir, özet üretmez.

## Telif bildirimleri
Faz 5a sonu itibarıyla hiçbir dış depodan kod kopyalanmadı; bu nedenle ek telif bildirimi gerekmiyor.

## Faz 5b — Pocket TTS Turkish (kütüphane olarak; kod kopyalanmadı)

| Paket | Sürüm | Lisans | İncelenen | Kullanılan API | Notlar |
|---|---|---|---|---|---|
| pocket-tts-turkish | 0.1.0 (PyPI wheel) | Apache-2.0 | `model.py`, `frontend.py`, `normalizer.py`, `cli.py`, METADATA | `TurkishTTS.from_pretrained(klasör, device)`, `.stream()`, `.voices`, `.sample_rate` | `from_pretrained` depo adıyla çağrılırsa `snapshot_download` ile **sessizce indirir**. J.A.R.V.I.S yalnızca yerelde çözülmüş klasör verir (`local_files_only=True`). Normalleştirici ISO tarihleri ve dosya yollarını yanlış okuduğu için J.A.R.V.I.S'te ön hazırlık eklendi |
| pocket-tts | 3.3.0 (PyPI wheel) | MIT | `models/tts_model.py` (yükleme, ses durumu, akış ve `stop`), bağımlılık listesi | Dolaylı | Yerleşik sesler yerel WAV dosyalarından hazırlanır; ağ gerekmez. fastapi/uvicorn yalnızca paketin kendi sunucusu içindir |
| Model ağırlıkları | `wite-tech/pocket-tts-turkish` | CC-BY-4.0 | Paket README'si | — | Atıf yükümlülüğü `LICENSES_THIRD_PARTY.md` ve `docs/TURKCE_TTS.md` içinde. Ağırlıklar bu ortamda indirilemedi; gerçek model çalıştırılmadı |
