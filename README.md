# J.A.R.V.I.S — Sesli Toplantı Yönetim Asistanı

J.A.R.V.I.S, şirket içi toplantıları **Türkçe sesli komutlarla** yöneten bir Windows masaüstü (konsol) uygulamasıdır.
- Toplantıyı başlatır, gündemi ve katılımcıları yükler, yoklama alır.
- Konuşmacı sürelerini tutar; kararları, görevleri, oylamaları ve normal konuşmaların transkriptini kaydeder.
- Toplantı sonunda kurumsal görünümlü **Türkçe PDF tutanak** üretir ve tutanak isteğe bağlı olarak **e-postayla** gönderilir.

> **Durum: geliştirme aşamasında.** Proje aktif olarak geliştirilmektedir; arayüzler, komutlar ve yapılandırma
> anahtarları değişebilir. Bu depo, belirli bir andaki sürümün yayın kopyasıdır ve geliştirme deposuyla otomatik
> eşitlenmez. Depodaki bütün kişi, şirket ve e-posta örnekleri **kurgusaldır** (`example.com`).

> **Lisans:** Bu depoda henüz bir lisans dosyası yoktur; lisans koşulları ayrıca duyurulacaktır. Lisans eklenene
> kadar kodun kullanım, kopyalama ve dağıtım hakları saklıdır. Üçüncü taraf bileşenlerin lisansları için
> [`LICENSES_THIRD_PARTY.md`](LICENSES_THIRD_PARTY.md) dosyasına bakın.

## JARVIS nedir?

Toplantı başkanı "Jarvis, …" diye başlayan komutlarla toplantıyı yönetir; "Jarvis" ile başlamayan konuşmalar
toplantı transkriptine yazılır. Uygulama yerelde çalışır: kayıtlar SQLite dosyasında tutulur, komutlar kural tabanlı
(deterministik) bir ayrıştırıcıyla anlaşılır. Yerel LLM (Ollama) isteğe bağlıdır ve yalnızca onaya sunulan öneriler üretir.

## Özellikler

| Kod | Özellik | Durum |
|---|---|---|
| G-01 | "Jarvis" uyandırma kelimesiyle Türkçe sesli komut (Google Web Speech, tr-TR) | Var |
| G-02 | Türkçe sesli yanıt (Pocket TTS Turkish; isteğe bağlı pyttsx3/SAPI5; kullanılamazsa konsol) | Var |
| G-03 | Toplantı başlatma/bitirme, yarım toplantıyı kurtarma | Var |
| G-04 | Yoklama (tek/çoklu kişi, tekrar kayıt önleme, tam ad eşleştirme) | Var |
| G-05 | Gündem okuma, sonraki/önceki madde, erteleme, madde ekleme | Var |
| G-06 | Önceki toplantı özeti (SQLite) | Var |
| G-07 | Konuşmacı takibi ("söz Ayşe'de"), süreler, söz almayanlar | Var |
| G-08 | Karar ve görev kaydı; Türkçe sorumlu ve tarih çıkarımı | Var |
| G-09 | Komut olmayan konuşmaların zaman damgalı transkripti | Var |
| G-10 | Kurumsal Türkçe PDF tutanak (yapılandırılabilir kurumsal şablon) | Var |
| G-11 | SMTP ile PDF ekli e-posta (TLS/SSL, deneme kipi, tekrar gönderimi önleme) | Var |
| G-12 | Gündem süre yönetimi, %80 uyarısı (tek sefer) | Var |
| G-13 | Devreden görevler | Var |
| G-14 | Oylama ve otopark (gündem dışı konular) | Var |
| G-15 | Gelişmiş özet: yerel LLM (Ollama) taslakları + kural tabanlı özet | Kısmen; ayrıntı aşağıda |
| G-16 | Çevrimdışı STT (faster-whisper) | **Yok**; `--stt whisper` hata verir |
| Ek | Takvim daveti (.ics) ve Excel görev listesi | Var |

**Notlar:**
- **G-15:** OpenAI entegrasyonu yazılmadı. `.env.example` içindeki `OPENAI_API_KEY` kullanılmıyor. Gelişmiş özet yerel Ollama ile **onaylanmamış taslak** olarak üretilir; taslaklar resmî tutanağa girmez.

## Python kurulumu ve sistem gereksinimleri

- **İşletim sistemi:** Windows 10 veya 11. Geliştirme ve canlı testler Windows'ta yapıldı. Otomatik testler Linux'ta da geçiyor.
- **Python:** 3.11, 3.12 veya 3.13. Testler 3.11 ve 3.13'te çalıştırıldı.
- **Sesli kullanım için:**
  - mikrofon ve hoparlör;
  - **internet bağlantısı** (Google konuşma tanıma).
- **İsteğe bağlı bileşenler:**
  - Doğal Türkçe ses (Pocket TTS): yaklaşık 440 MB model ve PyTorch kurulumu.
  - Yerel LLM: [Ollama](https://ollama.com) ve bir Qwen modeli. Varsayılan olarak kapalıdır, zorunlu değildir; GPU gerekmez.

## Bağımlılıklar

| Dosya | İçerik | Zorunlu mu? |
|---|---|---|
| `requirements.txt` | Çekirdek: python-dotenv, reportlab (PDF), openpyxl (Excel) | Evet |
| `requirements-dev.txt` | Çekirdek + pytest, pypdf, aiosmtpd (testler) | Testler için |
| `requirements-voice.txt` | Mikrofon, konuşma tanıma ve hoparlör paketleri | Sesli kullanım için |
| `requirements-tts.txt` | Pocket TTS Turkish ve PyTorch | Doğal Türkçe ses için |

Klavye modu, demo, PDF, Excel ve e-posta için yalnızca `requirements.txt` yeterlidir.

## Hızlı kurulum (PowerShell)

```powershell
cd jarvis-meeting-assistant                         # depoyu indirdiğiniz klasör
py -3.13 -m venv .venv                              # veya py -3.11 / py -3.12
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements-dev.txt                 # çekirdek + testler
pip install -r requirements-voice.txt               # mikrofon ve hoparlör (isteğe bağlı)
pip install -r requirements-tts.txt                 # Pocket TTS Turkish (isteğe bağlı, büyük)

Copy-Item .env.example .env
Copy-Item data\participants.example.json data\participants.json
Copy-Item data\agenda.example.json data\agenda.json
```

Ayrıntılı ve adım adım kurulum: [`docs/KURULUM.md`](docs/KURULUM.md).

## Örnek toplantı, klavye modu ve mikrofon

```powershell
python -m pytest                     # otomatik testler
python main.py --demo --no-tts       # örnek toplantı: ayrı demo veritabanı, e-posta gönderilmez
python main.py --text --no-tts       # klavye modu, mikrofon gerekmez
python main.py --mic-check           # mikrofon + konuşma tanıma + sesli yanıt kontrolü
python main.py                       # mikrofonla canlı kullanım (mikrofon yoksa klavyeye geçer)
```

## Komut satırı seçenekleri

Aşağıdaki seçeneklerin hepsi `main.py` içinde vardır (`python main.py --help`).

| Seçenek | Ne yapar |
|---|---|
| *(seçeneksiz)* | Mikrofonla canlı toplantı; ses kurulamazsa klavye moduna geçer |
| `--text` | Klavye modu (komutlar "Jarvis," ile başlar, diğer satırlar transkripte yazılır) |
| `--no-tts` | Sesli yanıt kapalı, yalnızca konsol |
| `--demo` | Hazır senaryolu örnek toplantı; `output/demo/` altında, e-posta gönderilmez |
| `--demo-live` | Tanıtım/video için **temiz ve izole canlı toplantı**: gerçek mikrofon, geçmiş devralınmaz, e-posta gönderilmez |
| `--participants DOSYA`, `--agenda DOSYA` | Yeni toplantı için başka katılımcı/gündem dosyası |
| `--list-mics`, `--mic N`, `--mic-check` | Mikrofonları listele / seç / kontrol et |
| `--stt google` | Konuşma tanıma motoru (`whisper` seçeneği henüz uygulanmadı) |
| `--voice-test [--save-audio] [--note "..."]` | Sesli komut doğruluk testi; rapor `output/voice_tests/` |
| `--voice-test-replay KLASÖR` | Kaydedilmiş test seslerini yeniden değerlendir |
| `--list-meetings` | Kayıtlı toplantılar ve e-posta durumları |
| `--minutes [ID]` | Toplantının PDF tutanağını (yeniden) üret |
| `--send-minutes [ID] [--force]` | Tutanağı e-postayla gönder |
| `--export-tasks [ID\|son\|acik]` | Görevleri Excel'e aktar |
| `--smtp-check [--smtp-ssl]` | SMTP bağlantı ve kimlik doğrulama kontrolü; **e-posta göndermez** |
| `--llm-check`, `--llm-benchmark`, `--analyze [ID]` | Yerel LLM kontrolü, ölçümü, toplantı analizi (taslak) |
| `--tts-download [KLASÖR]`, `--tts-bench` | Pocket TTS modelini indir / performansını ölç |
| `--trace-report [DOSYA]` | Girdi zinciri tanılama özeti |

## Katılımcı ve gündem dosyaları

`data/participants.json`:

```json
[{"name": "Ayşe Yılmaz", "email": "ayse.yilmaz@example.com", "role": "Üretim Planlama"}]
```

`data/agenda.json`:

```json
[{"title": "Üretim planlaması", "planned_minutes": 15, "presenter": "Ayşe Yılmaz"}]
```

- Toplantı açılırken her iki dosya da okunur. Dosya yoksa veya bozuksa uygulama çökmez, uyarı verir.
- Rehberdeki **tam adlar** kullanılır: "Mehmet katıldı" → "Mehmet Öztürk".
- Aynı adlı iki kişi varsa Jarvis hangisinin kastedildiğini sorar.

## Mikrofon kullanımı

```powershell
pip install -r requirements-voice.txt
python main.py --list-mics           # mikrofon ve hoparlör numaraları
python main.py --mic-check           # mikrofon + konuşma tanıma + sesli yanıt kontrolü
python main.py --mic 2               # belirli mikrofonla canlı kullanım (kalıcı: .env → MICROPHONE_INDEX)
```

- **Konuşma tanıma:** Google Web Speech (tr-TR). İnternet gerekir; ses Google'a gönderilir.
- Komutlar "Jarvis" ile başlar; diğer konuşmalar aktif konuşmacı adına transkripte yazılır.
- Jarvis konuşurken kendi sesini komut olarak algılamaması için mikrofon kısa süre susturulur (`TTS_ECHO_GUARD_MS`).
- Kablolu veya USB mikrofon önerilir; Bluetooth kulaklık mikrofonlarında doğruluk düşer.
- Ayrıntı: [`docs/WINDOWS_SES_KURULUMU.md`](docs/WINDOWS_SES_KURULUMU.md), [`docs/SESLI_KESME.md`](docs/SESLI_KESME.md).

## Klavye modu

```powershell
python main.py --text --no-tts
```

Mikrofon gerekmez. "Jarvis," ile başlayan satırlar komut olarak çalışır, diğer satırlar transkripte yazılır.
Bütün iş mantığı bu kipte denenebilir. Tam komut listesi: [`docs/KULLANICI_KILAVUZU.md`](docs/KULLANICI_KILAVUZU.md).

## Türkçe TTS kurulumu

```powershell
pip install -r requirements-tts.txt
python main.py --tts-download        # modeli indirir (yaklaşık 440 MB)
python main.py --tts-bench           # bu bilgisayardaki hız ölçümü
```

- Varsayılan motor **Pocket TTS Turkish** (`TTS_ENGINE=pocket`, `TTS_VOICE=male_1`); model yerelde çalışır.
- Kurulu değilse veya model yüklenemezse uyarı verilir ve yanıtlar yalnızca konsola yazılır; İngilizce bir sese
  kendiliğinden geçilmez.
- Windows SAPI5 motoru yalnızca `TTS_ENGINE=pyttsx3` ile açıkça seçilirse kullanılır (Türkçe SAPI5 sesi kurulu olmalıdır).
- Model ağırlıkları depoda yoktur; lisans ve atıf koşulları için `LICENSES_THIRD_PARTY.md` dosyasına bakın.
- Ayrıntı: [`docs/TURKCE_TTS.md`](docs/TURKCE_TTS.md).

## Ollama kurulumu ve model yapılandırması (isteğe bağlı)

Yerel LLM varsayılan olarak **kapalıdır** (`LLM_ENABLED=false`); uygulama onsuz eksiksiz çalışır.

```powershell
# 1) Ollama'yı kurun: https://ollama.com/download
ollama pull qwen3:4b
python main.py --llm-check           # bağlantı, yüklü modeller, GPU/CPU durumu
```

`.env` içinde:

```
LLM_ENABLED=true
OLLAMA_HOST=http://127.0.0.1:11434
LLM_MODEL=qwen3:4b
```

- Model yalnızca kurallarla anlaşılamayan, Jarvis'e açıkça söylenmiş komutları yorumlar.
- Önerdiği işlem **her zaman onaya** sunulur; LLM veritabanına doğrudan yazmaz. Toplantıyı bitirme, e-posta
  gönderme, görev tamamlama/iptal gibi önemli işlemler LLM ile hiç çalışmaz.
- Ollama kapalıysa veya yanıt vermezse sistem kurallı ayrıştırıcıyla devam eder.
- Uzak Ollama sunucusu yalnızca `LLM_ALLOW_REMOTE=true` ile kullanılabilir (toplantı metni o sunucuya gider).
- Diğer ayarlar (`LLM_ROUTER_TIMEOUT_S`, `LLM_NUM_CTX`, `LLM_MIN_CONFIDENCE` …) `.env.example` içinde açıklanmıştır.
- Ayrıntı: [`docs/LLM_MIMARISI.md`](docs/LLM_MIMARISI.md).

## PDF ve Excel çıktıları

- "Jarvis, toplantıyı bitir" komutuyla Türkçe PDF tutanak `output/` altına yazılır: toplantı bilgileri, katılım,
  gündem, kararlar, görevler, devreden işler, oylamalar, konuşmacı istatistikleri ve Ek A (transkript).
- Şirket adı, logo ve renkler `.env` ile ayarlanır (`COMPANY_NAME`, `LOGO_PATH`, `PDF_PRIMARY_COLOR`,
  `PDF_ACCENT_COLOR`). Depoda logo yoktur. Ayrıntı: [`docs/PDF_SABLONU.md`](docs/PDF_SABLONU.md).
- Türkçe karakterler için Unicode font gerekir: Windows'ta sistem fontları kendiliğinden bulunur; diğer
  sistemlerde `assets/fonts/README.md` dosyasına bakın.
- Tutanağı yeniden üretmek: `python main.py --minutes [ID]`.
- Görev listesini Excel'e aktarmak: "Jarvis, görevleri Excel'e aktar" veya `python main.py --export-tasks [ID|son|acik]`.
- "Jarvis, sonraki toplantı 21 Ekim saat 14.00" komutu takvim daveti (`.ics`) oluşturur.

## E-posta yapılandırması

`.env` içinde SMTP ayarlarını doldurun (gerçek değerleri yalnızca kendi `.env` dosyanıza yazın):

```
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_USER=kullanici@example.com
SMTP_PASSWORD=YOUR_APP_PASSWORD
SMTP_FROM=kullanici@example.com
SMTP_USE_TLS=true
```

```powershell
python main.py --smtp-check          # bağlantı ve kimlik doğrulama kontrolü; e-posta GÖNDERMEZ
```

- Gmail için 2 Adımlı Doğrulama ile oluşturulan **uygulama şifresi** gerekir; hesap şifresi çalışmaz.
- `MAIL_DRY_RUN=true` ile hiçbir e-posta gönderilmez; yalnızca `.eml` taslağı yazılır. İlk denemeler için önerilir.
- `MAIL_ALLOWED_DOMAINS` ile alıcılar belirli alan adlarıyla sınırlandırılabilir; `MAIL_REDIRECT_TO` deneme
  amacıyla bütün gönderimleri tek adrese yönlendirir.

### Tutanak ve e-posta akışı

1. "Jarvis, toplantıyı bitir" → açık söz süresi kapanır, toplantı kaydedilir, PDF `output/` altına yazılır.
2. "Jarvis, tutanağı gönder" → PDF (ve varsa .ics daveti) katılımcı listesindeki adreslere gönderilir.
   - `AUTO_SEND_MINUTES=false` varsayılandır; gönderim için komut beklenir.
   - Aynı alıcıya ikinci kez gönderilmez. Bunun için "tutanağı tekrar gönder" veya `--send-minutes --force` gerekir.
   - `MAIL_DRY_RUN=true`, `--demo` ve `--demo-live` kiplerinde e-posta gönderilmez; yalnızca `.eml` taslağı yazılır.
3. SMTP hatası toplantıyı ve PDF'i silmez.

## Testlerin çalıştırılması

```powershell
pip install -r requirements-dev.txt
python -m pytest -q
python -m ruff check --select E,F,W --line-length 120 jarvis tests   # pip install ruff
python -m compileall -q jarvis config main.py tests
```

## Belgeler

| Belge | İçerik |
|---|---|
| [docs/KURULUM.md](docs/KURULUM.md) | Windows'a adım adım kurulum, sorun giderme, taşıma |
| [docs/KULLANICI_KILAVUZU.md](docs/KULLANICI_KILAVUZU.md) | Toplantı başkanı için kullanım ve tam komut listesi |
| [docs/TEKNIK_MIMARI.md](docs/TEKNIK_MIMARI.md) | Modüller, veri akışı, güvenlik |
| [docs/DEMO_CANLI.md](docs/DEMO_CANLI.md) | `--demo-live` ile izole canlı demo |
| [docs/KATILIMCI_YONETIMI.md](docs/KATILIMCI_YONETIMI.md), [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Katılımcı eşleştirme kuralları, mimari kararlar |
| [CHANGELOG.md](CHANGELOG.md), [SECURITY.md](SECURITY.md) | Sürüm notları, güvenlik ve gizlilik |
| [docs/PDF_SABLONU.md](docs/PDF_SABLONU.md) | PDF tasarımı, logo ve renkler |
| [docs/TURKCE_TTS.md](docs/TURKCE_TTS.md), [docs/WINDOWS_SES_KURULUMU.md](docs/WINDOWS_SES_KURULUMU.md) | Ses kurulumu ayrıntıları |
| [docs/LLM_MIMARISI.md](docs/LLM_MIMARISI.md), [docs/SESLI_KESME.md](docs/SESLI_KESME.md) | Yerel LLM güvenlik mimarisi, sesli yanıtı kesme |
| [LICENSES_THIRD_PARTY.md](LICENSES_THIRD_PARTY.md), [docs/SOURCE_AUDIT.md](docs/SOURCE_AUDIT.md) | Üçüncü taraf lisanslar ve kaynak incelemesi |

## Bilinen sınırlamalar

- **Çevrimdışı konuşma tanıma yok:** faster-whisper entegrasyonu yazılmadı; `--stt whisper` hata verir. Sesli kullanım internet gerektirir.
- **OpenAI entegrasyonu yok:** `OPENAI_API_KEY` okunur ama kullanılmaz.
- **Konuşmacı otomatik tanınmaz:** Aktif konuşmacı "Jarvis, söz Ayşe'de" komutuyla belirlenir.
- **Sesli komut doğruluğu** mikrofona, ortama ve Google tanımaya bağlıdır; kendi ortamınızda `--voice-test` ile ölçün.
- **Platform:** Canlı ses yalnızca Windows 10/11'de denendi. Otomatik testler Linux'ta da geçer; macOS denenmedi.
- **Arayüz:** Yalnızca konsol; web/mobil arayüz, Teams/Zoom entegrasyonu yoktur.
- **Yerel LLM** çıktıları taslaktır; küçük modeller Türkçe komutları yanlış yorumlayabilir, bu yüzden her öneri onay ister.
- Görev hatırlatma e-postaları henüz uygulanmadı.
- Gerçek toplantılarda kapsamlı saha kullanımı henüz sınırlıdır; hatalarla karşılaşabilirsiniz.

## Gizlilik

- Google konuşma tanıma açıkken mikrofon sesi Google sunucularına gönderilir.
- Yerel LLM yalnızca `OLLAMA_HOST` adresindeki sunucuya bağlanır. Varsayılan adres bu bilgisayardır; uzak sunucu `LLM_ALLOW_REMOTE` ile açıkça izin verilmedikçe kullanılmaz.
- Toplantı kayıtları yerel SQLite dosyasında tutulur ve `.gitignore` ile Git dışında bırakılır.
- `.env` dosyası Git'e eklenmez ve bu depoda yoktur.
