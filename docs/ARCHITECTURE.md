# Mimari

```
Girdi kaynağı (InputSource)          Orkestrasyon               İş kuralları            Kalıcılık
┌──────────────────────┐   metin   ┌────────────────────┐     ┌─────────────────┐     ┌──────────────────┐
│ TextListener (--text)│──────────▶│ JarvisAssistant    │────▶│ MeetingManager  │────▶│ MeetingRepository│
│ ScriptedListener     │           │  CommandParser     │     │  (tüm kurallar) │     │  SQLite (WAL)    │
│ VoiceListener (ses)  │           │  ├ "Jarvis …" → Intent   │                 │     │  meetings+actions│
└──────────────────────┘           │  └ diğer → transkript    └─────────────────┘     └──────────────────┘
                                   │  SpeechEngine (TTS)│
                                   └────────────────────┘
                                   Faz 3: PdfGenerator → Mailer
```

## İlkeler
- **Tek veri modeli:** `jarvis/meeting/models.py` içindeki `Meeting`. PDF'teki alanlar aynen korunur.
  İşletim için birkaç alan eklenmiştir: `status`, `current_item_index`, `active_speaker`, `is_demo`,
  `minutes_pdf`, `email_status`, `load_warnings`.
- **Tek iş kuralları merkezi:** `MeetingManager`. `JarvisAssistant` yalnızca yönlendirme yapar.
- **Her değişiklik kaydedilir:** Her manager işleminin sonunda `repo.save()` çağrılır. Kayıt tek bir
  SQLite işlemi içinde yapılır. Toplantının JSON'u ile `action_items` tablosu birlikte güncellenir.
- **Ses katmanı yalnızca metin üretir:** Bu sayede tüm akış mikrofon olmadan test edilir.
- **Zaman enjekte edilebilir:** `MeetingManager(clock=...)` ile süre testleri deterministiktir.

## Komut / konuşma ayrımı
`CommandParser.parse()` iki durumda `None` döner:
- Metin uyandırma kelimesiyle başlamıyorsa
- Uyandırma kelimesi ekli biçimdeyse ("Jarvis'in önerisi")

Bu iki durumda metin normal konuşmadır ve transkripte yazılır. Hitap edilmiş ama anlaşılamamış
komutlar `UNKNOWN` olarak döner ve hiçbir işlem yapılmaz.

## Ses mimarisi (Faz 4)
```
mikrofon ─► AudioCapture (TEK okuyucu iş parçacığı)
              │ 30 ms çerçeveler
              ├─ SpeechEngine.is_muted()? → çerçeve atılır (asistanın kendi sesi)
              ▼
            Segmenter: ortam gürültüsü kalibrasyonu, enerji eşiği, 300 ms ön tampon,
                       800 ms sessizlikte bölüt sonu, <300 ms sesleri eleme, 15 sn azami
              │ AudioSegment (PCM + başlangıç/bitiş zamanı)
              ▼
            segment kuyruğu ─► STT iş parçacığı (TEK, sıralı) ─ GoogleSpeechToText (tr-TR)
                                  │  ağ hatası: bölüt kuyruğun başında bekler, WAV yedeklenir,
                                  │  artan aralıkla yeniden denenir, kullanıcı bir kez uyarılır
                                  ▼
                               Utterance (metin + söylendiği an + STT süresi)
                                  ▼
            JarvisAssistant.handle_text ─ "Jarvis …" → komut
                                         ─ cümle ortasında "Jarvis …" → önceki kısım transkript + komut
                                         ─ diğer → transkript (söylendiği anla, aktif konuşmacıya)
```
- **Tek mikrofon okuyucusu.** Komut ve transkript için ayrı okuyucu yoktur; aynı bölüt akışı ikisini de besler.
- **Sıra korunur.** Tanıma tek iş parçacığında, geliş sırasıyla yapılır. Böylece "söz Mehmet'te"
  komutundan önceki konuşma Mehmet'e yazılmaz.
- **Yankı koruması.** TTS tek iş parçacığında çalışır (SAPI5/COM). Konuşma süresince ve bittikten sonraki
  `TTS_ECHO_GUARD_MS` boyunca mikrofon sesi atılır. Susturma sırasında başlamış bir konuşmanın kuyruğu
  yeni bölüt sayılmaz.
- **Kapanış.** Ctrl+C veya "çık" ile kapanırken yarım konuşma ve kuyruktaki bölütler tanınıp transkripte
  yazılır. Bu aşamada gelen komutlar güvenlik için çalıştırılmaz. Ctrl+C ile kapanışta ardından ara
  tutanak üretilir.
- **Bölüt sonu (Faz 5a).** Son sesli çerçeveden sonra 500 ms korunur (`VAD_TAIL_KEEP_MS`); konuşma
  sürerken eşiğin %50'si (`VAD_RELEASE_PERCENT`) konuşma sayılır. Zayıf Türkçe kelime sonları kesilmez.
  STT'ye gönderilmeden önce ses 16 kHz'e indirilir.
- **Kesik komut (Faz 5a).** "Jarvis toplantıyı başl" gibi eksik komutlar tespit edilir; hiçbir işlem
  yapılmaz ve tahminle tamamlanmaz (`voice/truncation.py`).
- **Motor bağımsızlığı.** `SpeechToText` arayüzü sayesinde Faz 5'teki faster-whisper motoru
  `voice_listener.py` değişmeden eklenebilir.

## Yerel LLM katmanı (Faz 5a)
Ayrıntı ve risk değerlendirmesi: `docs/LLM_MIMARISI.md`.

```
CommandParser ─UNKNOWN + hitap + kesik değil─► LlmCommandRouter ─► Ollama (localhost, JSON Schema)
                                                   │ şema + izinli niyet + kanıt + katılımcı/tarih doğrulama
                                                   │ kanonik komut ─► CommandParser'dan tekrar geçer
                                                   ▼
                     okuma: çalıştır · kayıt değiştiren: "Jarvis, evet" bekle · önemli işlem: engelle
"toplantıyı analiz et" ─► MeetingAnalyzer (parçalı, kanıtlı) ─► Meeting.ai_drafts (taslak) ─► onay ─► Manager
"soru: ..." ─► SQLite kanıtları ─► LLM ─► geçerli kanıt numarası yoksa "dayanak bulamadım"
```
- `jarvis/llm` paketi `MeetingManager` dışında hiçbir kaydı değiştiremez.
- LLM kapalıyken (`LLM_ENABLED=false`, varsayılan) ağ isteği yapılmaz. Analiz kural tabanlıdır.
- Uzun LLM işleri (analiz, soru-cevap) arka plan iş parçacığında çalışır; toplantı akışı ve süre uyarıları beklemez.
