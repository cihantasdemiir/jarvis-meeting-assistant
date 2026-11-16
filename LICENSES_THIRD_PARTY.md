# Üçüncü Taraf Lisansları

## Projeye kopyalanan / uyarlanan kaynak kod
Yok. Faz 4'te MeetingScribe'ın (MIT) yalnızca tasarım fikirleri kullanıldı; kod kopyalanmadı
(ayrıntılar `docs/SOURCE_AUDIT.md`).

## Harici hizmet notu
Google Web Speech API, SpeechRecognition kütüphanesinin genel anahtarıyla kullanılır. Bu anahtar kişisel
ve deneme amaçlıdır; Google tarafından herhangi bir zaman geri alınabilir. Kurumsal kullanım için
`GOOGLE_SPEECH_API_KEY` ile kendi anahtarınızı tanımlayın veya Faz 5'teki çevrimdışı motoru kullanın.
Toplantı sesi Google sunucularına gönderilir; kurum KVKK politikasıyla değerlendirilmelidir.

## Yerel LLM (isteğe bağlı, Faz 5a)
Python tarafında yeni bağımlılık yok (yalnızca standart kütüphane). Kullanıcının ayrıca kurduğu bileşenler:

| Bileşen | Lisans | Not |
|---|---|---|
| Ollama (uygulama) | MIT | Ayrı kurulur; JARVIS onunla yerel HTTP üzerinden konuşur |
| Qwen3 4B (`qwen3:4b`) | Apache-2.0 (Qwen ekibinin model kartına göre) | Model dosyası projeyle dağıtılmaz. Ticari kullanım öncesi model kartındaki güncel lisansı doğrulayın |
| Qwen3.5 4B (`qwen3.5:4b`) | **Doğrulanmadı** | Kullanmadan önce ollama.com/library ve Qwen model kartındaki lisansı kontrol edin |

## Türkçe sesli yanıt (isteğe bağlı, Faz 5b)
`requirements-tts.txt` ile kurulur; projeye kod kopyalanmadı (paketler kütüphane olarak kullanılır).

| Bileşen | Lisans | Not |
|---|---|---|
| pocket-tts-turkish 0.1.0 (Wite Tech) | Apache-2.0 | Lisans paket içinden doğrulandı |
| pocket-tts 3.3.0 (Kyutai) | MIT | Lisans paket içinden doğrulandı |
| Pocket TTS Turkish model ağırlıkları (`wite-tech/pocket-tts-turkish`) | **CC-BY-4.0** | Projeyle dağıtılmaz; kullanıcı `--tts-download` ile indirir. CC-BY **atıf yükümlülüğü** vardır: dağıtılan dokümantasyonda "Pocket TTS Turkish — Khaled Moawad, Ammar Rashed, Ekrem Çetinkaya (Wite Tech), CC-BY-4.0; Kyutai Pocket TTS türevi" atfı korunmalıdır |
| PyTorch | BSD-3-Clause | pocket-tts bağımlılığı |
| numpy, scipy | BSD-3-Clause | pocket-tts bağımlılığı |
| sentencepiece | Apache-2.0 | pocket-tts bağımlılığı |
| huggingface-hub | Apache-2.0 | Model önbelleğini YEREL olarak bulmak ve açık onayla indirmek için |
| soundfile | BSD-3-Clause | pocket-tts-turkish bağımlılığı |
| fastapi, uvicorn, pydantic, typer | MIT / BSD | pocket-tts'in kendi sunucu/komut satırı bağımlılıkları; J.A.R.V.I.S bunları çalıştırmaz |

Yerleşik sesler (`male_1` vb.) paket belgesine göre sentetiktir; gerçek kişilerin kaydı değildir. Ses
klonlama özelliği J.A.R.V.I.S'te kullanılmaz.

## Kullanılan kütüphaneler

| Paket | Lisans | Kullanım |
|---|---|---|
| python-dotenv | BSD-3-Clause | `.env` okuma |
| pyttsx3 | MPL-2.0 | Sesli yanıt (Windows SAPI5) |
| SpeechRecognition | BSD-3-Clause | Google Web Speech istemcisi |
| PyAudio | MIT | Mikrofon erişimi (PortAudio, MIT) |
| audioop-lts | PSF | SpeechRecognition'ın Python 3.13 bağımlılığı (otomatik kurulur) |
| reportlab | BSD | PDF tutanak |
| pytest | MIT | Testler (yalnızca geliştirme) |
| pypdf | BSD-3-Clause | Testlerde PDF metnini okuma (yalnızca geliştirme) |
| aiosmtpd | Apache-2.0 | Testlerde yerel SMTP sunucusu (yalnızca geliştirme) |

PDF fontu projeyle dağıtılmaz. Windows'taki sistem fontu (Arial vb.) kullanılır veya kullanıcı
DejaVu fontlarını `assets/fonts/` altına ekler. DejaVu, Bitstream Vera türevi serbest bir lisansla
dağıtılır; eklenirse lisans metni de fontlarla birlikte konmalıdır.

## Denetlenen ama kodu alınmayan depolar
Ayrıntılar ve gerekçeler için `docs/SOURCE_AUDIT.md` dosyasına bakın.
