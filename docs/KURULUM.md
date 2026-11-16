# Kurulum Kılavuzu (Windows 10/11)

Bu kılavuz, Python bilmeyen bir değerlendiricinin J.A.R.V.I.S'i adım adım kurabilmesi için yazıldı. Komutların hepsi **PowerShell** içindir: Başlat menüsüne "PowerShell" yazıp açın.

> Kısa yol: Yalnızca klavyeyle denemek için 1–3. ve 8., 10., 11. adımlar yeterlidir. Mikrofon, Türkçe ses, Ollama ve e-posta isteğe bağlıdır.

## 1. Gereken yazılımlar

| Yazılım | Zorunlu mu? | Nereden |
|---|---|---|
| Python 3.11, 3.12 veya 3.13 (64 bit) | **Evet** | https://www.python.org/downloads/windows/ — kurulumda **"Add python.exe to PATH"** kutusunu işaretleyin |
| İnternet bağlantısı | Sesli kullanımda evet (Google konuşma tanıma); kurulumda evet | — |
| Mikrofon ve hoparlör | Sesli kullanımda | Kablolu veya USB mikrofon önerilir; Bluetooth kulaklık mikrofonu testlerde kararsız sonuç verdi |
| Ollama | Hayır (isteğe bağlı yerel LLM) | https://ollama.com/download/windows |
| PDF görüntüleyici | Tutanağı açmak için | Herhangi biri |

Python'un kurulduğunu doğrulayın:

```powershell
py --list          # kurulu Python sürümleri; 3.11, 3.12 veya 3.13 görünmeli
```

## 2. Depoyu indirme ve sanal ortam

```powershell
# Depoyu klonlayın veya ZIP olarak indirip açın. Ardından proje klasörüne girin:
cd jarvis-meeting-assistant
py -3.13 -m venv .venv          # Python 3.11 kullanıyorsanız: py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1    # satırın başında (.venv) görünmeli
python -m pip install --upgrade pip
```

**"Activate.ps1 çalıştırılamıyor / betik çalıştırma devre dışı" hatası** alırsanız bir kez şunu çalıştırın, ardından etkinleştirme komutunu tekrarlayın:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

Her yeni PowerShell penceresinde proje klasörüne gidip `.\.venv\Scripts\Activate.ps1` komutunu yeniden çalıştırın.

## 3. Bağımlılıkların kurulması

| Dosya | İçerik | Gerekli mi? |
|---|---|---|
| `requirements.txt` | python-dotenv, reportlab (PDF), openpyxl (Excel) | Evet |
| `requirements-dev.txt` | requirements.txt + pytest, pypdf, aiosmtpd (testler) | Testleri çalıştırmak için |
| `requirements-voice.txt` | SpeechRecognition, PyAudio, pyttsx3 | Mikrofon ve hoparlör için |
| `requirements-tts.txt` | pocket-tts-turkish 0.1.0, pocket-tts 3.3.0, torch 2.14.1 | Doğal Türkçe ses için (büyük indirme) |

```powershell
pip install -r requirements-dev.txt
pip install -r requirements-voice.txt     # isteğe bağlı
pip install -r requirements-tts.txt       # isteğe bağlı; PyTorch yüzlerce MB indirir
python -m pytest -q                       # kurulumu doğrular; testlerin tamamı geçmeli
```

- `requirements-voice.txt` kurulamazsa (çoğunlukla PyAudio) uygulama yine çalışır, klavye moduna geçer. Python 3.11–3.13 için PyAudio'nun hazır Windows paketi vardır.
- `ModuleNotFoundError: comtypes` hatası yalnızca pyttsx3 seçiliyse görülür: `pip install comtypes pywin32`.

## 4. Mikrofon ayarları ve cihaz seçimi

1. Windows Ayarlar → Sistem → Ses → Giriş bölümünden mikrofonu seçin ve konuşunca seviye çubuğunun hareket ettiğini görün.
2. Ayarlar → Gizlilik ve güvenlik → Mikrofon bölümünde "Masaüstü uygulamalarının mikrofona erişmesine izin ver" açık olmalı.
3. Proje içinden kontrol:

```powershell
python main.py --list-mics      # mikrofon ve hoparlör numaraları
python main.py --mic-check      # seviye + tek cümle konuşma tanıma + Türkçe ses denemesi
python main.py --mic 2          # belirli mikrofonla çalıştırma (numara --list-mics çıktısından)
```

Kalıcı seçim için `.env` içinde `MICROPHONE_INDEX=2` yazın. Ayrıntılı ayarlar (gürültü eşiği, sessizlik süresi): `docs/WINDOWS_SES_KURULUMU.md`.

## 5. Türkçe ses: Pocket TTS ve ilk model yüklemesi (isteğe bağlı)

```powershell
pip install -r requirements-voice.txt     # hoparlör çıkışı için PyAudio
pip install -r requirements-tts.txt
python main.py --tts-download             # yaklaşık 440 MB model; indirmeden önce onay sorar
```

- Model Hugging Face önbelleğine iner. Belirli bir klasöre indirmek için `python main.py --tts-download C:\modeller\jarvis-tts` komutunu kullanın; ardından `.env` içinde `TTS_MODEL_DIR=C:\modeller\jarvis-tts` yazın.
- İlk açılışta model bir kez yüklenir; bilgisayara göre birkaç saniye ile birkaç on saniye sürebilir. Bu süre komut yanıt süresi değildir.
- Model veya paket yoksa uygulama uyarı verir ve yanıtları **konsola yazmaya devam eder**; İngilizce bir sese kendiliğinden geçmez.
- Windows'un kendi sesi (SAPI5, "Microsoft Tolga") yalnızca `.env` içinde `TTS_ENGINE=pyttsx3` seçilirse kullanılır. Tolga sesinin kurulumu: `docs/WINDOWS_SES_KURULUMU.md`.
- Performans ölçümü: `python main.py --tts-bench --tts-play`. GPU zorunlu değildir; varsayılan `TTS_DEVICE=cpu`.

Ayrıntılar: `docs/TURKCE_TTS.md`.

## 6. Google konuşma tanıma: internet gereksinimi

- Varsayılan motor `STT_ENGINE=google`'dır. Konuşma bölümleri internet üzerinden Google Web Speech servisine gönderilir; **internet yoksa sesli komut çalışmaz**.
- Bağlantı kesilirse uygulama kapanmaz; hata bildirir ve klavyeden komut alabilir.
- Varsayılan kullanımda API anahtarı gerekmez. `GOOGLE_SPEECH_API_KEY` isteğe bağlıdır.
- Çevrimdışı tanıma (`--stt whisper`) bu sürümde **yoktur**.

## 7. Ollama ve isteğe bağlı yerel model

Yerel LLM **zorunlu değildir** ve varsayılan olarak kapalıdır (`LLM_ENABLED=false`).

**LLM kapalıyken:**
- Tüm komutlar kurallı ayrıştırıcıyla çalışır.
- Anlaşılamayan cümleye "anlayamadım" denir.
- Toplantı analizi kural tabanlı taslak üretir.

Açmak için:

```powershell
# 1) Ollama'yı kurun (https://ollama.com/download/windows); arka planda çalışır.
ollama --version
ollama pull qwen3:4b          # birkaç GB; geliştirmede qwen3.5:4b de denendi
python main.py --llm-check    # bağlantı, yüklü modeller, GPU/CPU kullanımı
```

Ardından `.env` içinde:

```
LLM_ENABLED=true
LLM_MODEL=qwen3:4b
```

**LLM açıkken davranış:**
- Model yalnızca kurallarla anlaşılamayan komutları yorumlar.
- Önerdiği işlem **her zaman onay** ister; model veritabanına doğrudan yazmaz.
- Ollama kapalıysa veya model yanıt vermezse uygulama kurallı ayrıştırıcıyla devam eder.

**Donanım ve gizlilik:**
- GPU zorunlu değildir; CPU'da da çalışır, yalnızca yanıt daha yavaş olur.
- `OLLAMA_HOST` bu bilgisayar dışında bir adrese ayarlanırsa, `LLM_ALLOW_REMOTE=true` ile açıkça izin verilmedikçe LLM devre dışı kalır. Toplantı metni başka bilgisayara gönderilmez.

Ayrıntılar: `docs/LLM_MIMARISI.md`.

## 8. `.env` dosyasını oluşturma

```powershell
Copy-Item .env.example .env
notepad .env
```

- `.env` dosyası yoksa uygulama varsayılan ayarlarla çalışır ve bunu açılışta yazar.
- Sık değiştirilen ayarlar:
  - `MEETING_TITLE`, `MEETING_CHAIR`, `MEETING_LOCATION`
  - `COMPANY_NAME`, `LOGO_PATH` (PDF başlığı)
  - `MICROPHONE_INDEX`
  - `TTS_ENGINE`, `LLM_ENABLED`, `SMTP_*`
- `.env` kişisel bilgi ve şifre içerir. **Kimseyle paylaşmayın, ZIP'e veya Git'e eklemeyin.**

## 9. SMTP (e-posta) ayarlarını güvenli yapılandırma

`.env.example` içindeki `SMTP_USER=kullanici@example.com` ve `SMTP_PASSWORD=YOUR_APP_PASSWORD` **örnek değerlerdir**. Kendi değerlerinizi yalnızca `.env` dosyasına yazın.

```
SMTP_HOST=smtp.gmail.com          # Office 365: smtp.office365.com
SMTP_PORT=587
SMTP_USE_TLS=true
SMTP_USER=<gönderen adres>
SMTP_PASSWORD=<uygulama şifresi>
SMTP_FROM=<gönderen adres>
AUTO_SEND_MINUTES=false           # güvenli varsayılan: gönderim komut bekler
MAIL_REDIRECT_TO=<kendi adresiniz> # İLK DENEMELERDE: tüm e-postalar yalnızca buraya gider
MAIL_ALLOWED_DOMAINS=example.com
```

- **Gmail:** Normal hesap şifresi çalışmaz. 2 Adımlı Doğrulama açıkken oluşturulan 16 harfli **uygulama şifresi** gerekir.
- **Önce bağlantıyı deneyin** (e-posta göndermez; şifreyi ekrana yazmaz):

  ```powershell
  python main.py --smtp-check          # 465/SSL için: python main.py --smtp-check --smtp-ssl
  ```

- `MAIL_DRY_RUN=true` iken hiçbir e-posta gönderilmez; `.eml` taslağı `output/outbox/` altına yazılır.
- Uygulama SMTP kullanıcı adını, şifresini ve e-posta içeriğini günlüğe yazmaz.

## 10. `participants.json` ve `agenda.json` oluşturma

```powershell
Copy-Item data\participants.example.json data\participants.json
Copy-Item data\agenda.example.json data\agenda.json
notepad data\participants.json
```

Örnek dosyalardaki kişi ve şirket adları **kurgusaldır**. Dosyalar UTF-8 kaydedilmelidir (Notepad'de "Farklı Kaydet → Kodlama: UTF-8").

```json
[
  {"name": "Ayşe Yılmaz", "email": "ayse.yilmaz@example.com", "role": "Üretim Planlama"},
  {"name": "Mehmet Öztürk", "email": "mehmet.ozturk@example.com", "role": "Kalite"}
]
```

```json
[
  {"title": "Haftalık üretim planı ve hedefler", "planned_minutes": 5, "presenter": "Ayşe Yılmaz"},
  {"title": "Kalite kontrol ve uygunsuzluklar", "planned_minutes": 5}
]
```

- **Tam ad yazın.** Kişiler sesle çoğunlukla yalnızca adıyla çağrılır; aynı adlı iki kişi varsa Jarvis hangisi olduğunu sorar.
- Dosya bozuksa veya yoksa uygulama çökmez, açılışta uyarı verir.
- Başka dosya kullanmak için: `python main.py --participants data\ekip_a.json --agenda data\gundem2.json`.

## 11. Uygulamayı başlatma

```powershell
python main.py                    # mikrofonla canlı toplantı
```

- Açılışta mikrofon adı, ses motoru ve LLM durumu yazılır.
- Komutlar "Jarvis" ile başlar: "Jarvis, toplantıyı başlat".
- Çıkmak için "Jarvis, çık" deyin veya Ctrl+C'ye basın. Ctrl+C sonrası toplantı kaybolmaz; bir sonraki açılışta kurtarılır.

## 12. Klavye modu, örnek demo ve canlı demo

```powershell
python main.py --text             # klavye; satırları "Jarvis," ile başlatın, diğer satırlar transkripte yazılır
python main.py --text --no-tts    # klavye ve sessiz (yalnızca konsol)
python main.py --demo --no-tts    # hazır senaryolu örnek toplantı (output\demo\); e-posta gönderilmez
python main.py --demo-live        # final video: temiz, izole canlı toplantı (bkz. docs/DEMO_CANLI.md)
```

`--demo-live` her çalıştırmada `data\demo_live\<zaman>\` ve `output\demo_live\<zaman>\` altında **boş** bir oturum açar:
- Normal toplantı geçmişini okumaz ve değiştirmez.
- E-posta göndermez.

## 13. Yaygın hatalar ve çözümler

| Belirti | Neden / Çözüm |
|---|---|
| `py` bulunamadı | Python kurulurken PATH seçilmedi. Python'u yeniden kurup "Add python.exe to PATH" işaretleyin. |
| `Activate.ps1` engellendi | `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` |
| "Sesli mod başlatılamadı … Klavye moduyla devam ediliyor" | `requirements-voice.txt` kurulu değil veya mikrofon yok. `pip install -r requirements-voice.txt`, ardından `python main.py --mic-check` |
| Komutlar hiç algılanmıyor | Mikrofon numarası yanlış olabilir (`--list-mics`, `MICROPHONE_INDEX`); internet yoksa Google STT çalışmaz. |
| "Jarvis" yerine başka kelime yazılıyor | `--voice-test` raporundaki önerilen varyantları `.env` → `WAKE_WORD_ALIASES` satırına ekleyin. |
| Asistan kendi sesini komut sanıyor | Hoparlör sesini kısın, mikrofonu hoparlörden uzaklaştırın; `TTS_ECHO_GUARD_MS` ayarı (`docs/TURKCE_TTS.md` §4). |
| "Pocket TTS Turkish kurulu değil" / "model bulunamadı" | `pip install -r requirements-tts.txt`, ardından `python main.py --tts-download` |
| Ses yok, konsol çalışıyor | Hoparlör numarası: `--list-mics` → `TTS_OUTPUT_DEVICE` |
| "Yerel LLM: KAPALI" | Normaldir (varsayılan). Açmak için 7. adım. |
| LLM açık ama "bağlanılamadı" | Ollama çalışmıyor veya model yok: `ollama list`, `python main.py --llm-check` |
| SMTP "kimlik doğrulama" / "Connection unexpectedly closed" | Gmail için uygulama şifresi gerekir; `python main.py --smtp-check` hangi aşamada kaldığını Türkçe açıklar. |
| PDF'te Türkçe karakter bozuk | Unicode font bulunamadı. `assets\fonts\` içine DejaVuSans.ttf ve DejaVuSans-Bold.ttf koyun veya `.env` → `PDF_FONT_REGULAR`, `PDF_FONT_BOLD`. |
| PDF'te logo alanında "resmî logo dosyası tanımlanmadı" yazıyor | `.env` → `LOGO_PATH=assets/logo/<dosya>.png`. Depoda logo dosyası yoktur (`docs/PDF_SABLONU.md`). |
| "Zaten açık bir toplantı var" | Önceki toplantı kurtarıldı. "Jarvis, toplantıyı bitir" deyip yenisini başlatın. |

## 14. Başka bilgisayara taşıma

1. Yeni bilgisayarda 1–3. adımları uygulayın. `.venv` klasörü taşınmaz; her bilgisayarda yeniden oluşturulur.
2. Taşınabilecek dosyalar:
   - `.env` (şifre içerir; güvenli yoldan aktarın);
   - `data\participants.json` ve `data\agenda.json`;
   - logo dosyası;
   - geçmişin devam etmesi isteniyorsa `data\jarvis.db`.
3. Pocket TTS modelini yeni bilgisayarda yeniden indirin (`--tts-download`) veya model klasörünü kopyalayıp `TTS_MODEL_DIR` ile gösterin.
4. Ollama modelini yeniden çekin (`ollama pull ...`).
5. `python -m pytest -q` ve `python main.py --mic-check` ile doğrulayın.
