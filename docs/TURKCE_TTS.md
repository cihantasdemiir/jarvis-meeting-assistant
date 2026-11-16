# Türkçe Sesli Yanıt — Pocket TTS Turkish (Faz 5b)

J.A.R.V.I.S yanıtlarını **Pocket TTS Turkish** modelinin **`male_1`** sesiyle seslendirir. Model bu
bilgisayarda çalışır; metin veya ses dışarı gönderilmez.

> **Doğrulama durumu.** Kod geliştirme ortamında (Linux) sahte model ve sahte hoparlörle test edildi.
> Bu ortamda Hugging Face ve PyTorch indirmeleri engelli olduğu için **gerçek model burada çalıştırılmadı**.
> Gerçek ses, hız ve bellek ölçümü sizin Windows bilgisayarınızda yapılmalıdır (aşağıda §5 ve §6).

## 1. Kurulum (Windows PowerShell)

J.A.R.V.I.S'in kendi sanal ortamında:

```powershell
cd jarvis-meeting-assistant
.\.venv\Scripts\Activate.ps1
python --version                                   # 3.13.x bekleniyor
pip install -r requirements-voice.txt              # PyAudio: hoparlör çıkışı (mikrofon için zaten kurulu olabilir)
pip install -r requirements-tts.txt                # pocket-tts-turkish 0.1.0, pocket-tts 3.3.0, torch 2.14.1 (büyük)
python -c "import torch, pocket_tts_turkish; print(torch.__version__, torch.cuda.is_available())"
```

Sürümler, Windows üzerinde ayrı bir deneme ortamında WAV üretimiyle doğrulanmış sürümlerdir.
Bağımlılıklar J.A.R.V.I.S'in mevcut paketleriyle (python-dotenv, reportlab, pyttsx3, SpeechRecognition,
PyAudio) çakışmaz; paket meta verileri incelenerek kontrol edildi.

### Model dosyaları (yaklaşık 440 MB)

Uygulama modeli **internetten kendiliğinden indirmez**. Önce yerelde arar:

1. `.env` içinde `TTS_MODEL_DIR` doluysa o klasör (içinde `config.yaml`, `model.safetensors`,
   `tokenizer.model`, `voices\*.wav`).
2. Boşsa Hugging Face önbelleği (`%USERPROFILE%\.cache\huggingface\hub`), yalnızca yerel arama.
   `pocket-tts-turkish generate` komutuyla test ettiğinizde model bu önbelleğe indi. Önbellek kullanıcıya
   aittir, sanal ortamlar arasında ortaktır; büyük olasılıkla tekrar indirmeniz gerekmez.

Model bulunamazsa uygulama açık bir mesaj verir ve konsolla devam eder. İndirmek için (onay sorulur):

```powershell
python main.py --tts-download                      # HF önbelleğine
python main.py --tts-download C:\modeller\jarvis-tts   # belirli klasöre; sonra .env: TTS_MODEL_DIR=C:\modeller\jarvis-tts
```

İnternetsiz çalışmak için modelin önceden indirilmiş olması yeterlidir. Uygulama model yüklerken
`HF_HUB_OFFLINE=1` kullanır.

## 2. Ayarlar (`.env`)

| Ayar | Varsayılan | Açıklama |
|---|---|---|
| `TTS_ENGINE` | `pocket` | `pocket`, `pyttsx3` (eski SAPI5, yalnızca açıkça seçilirse) veya `none` (yalnızca konsol) |
| `TTS_VOICE` | `male_1` | Modelde yoksa başka sese geçilmez; hata verilir ve mevcut sesler listelenir |
| `TTS_DEVICE` | `cpu` | `cpu` veya `cuda` (bkz. §6) |
| `TTS_MODEL_DIR` | boş | Model klasörü; boşsa HF önbelleği (yalnızca yerel) |
| `TTS_CPU_THREADS` | `0` | 0 = PyTorch varsayılanı. Takılma veya komut gecikmesi varsa 2–4 deneyin |
| `TTS_VOLUME_PERCENT` | `100` | Ses düzeyi |
| `TTS_ECHO_GUARD_MS` | `700` | Oynatma bittikten sonra mikrofonun yok sayma süresi (bkz. §4) |
| `TTS_PREBUFFER_MS` | `200` | Çalmaya başlamadan önce biriktirilen ses |
| `TTS_MAX_QUEUE` | `5` | Bekleyen yanıt sınırı |
| `TTS_MAX_SPOKEN_CHARS` | `500` | Daha uzun yanıtların başı okunur, sonra "Ayrıntılar ekranda." denir |
| `TTS_OUTPUT_DEVICE` | boş | Hoparlör numarası (`python main.py --list-mics` hoparlörleri de listeler) |
| `TTS_LOAD_TIMEOUT_S` | `120` | Açılışta modelin yüklenmesi için en fazla bekleme |
| `TTS_SHUTDOWN_TIMEOUT_S` | `15` | Kapanışta son yanıtın bitmesi için en fazla bekleme |

**Önemli:** Eski `.env` dosyanızda `TTS_ENGINE=pyttsx3` yazıyorsa eski motor çalışmaya devam eder.
Pocket'a geçmek için `TTS_ENGINE=pocket` yapın. Geçersiz değerler açılışta `UYARI (.env)` olarak gösterilir.

`TTS_RATE` (konuşma hızı) ve `TTS_VOICE_HINT` yalnızca pyttsx3 için geçerlidir. Pocket TTS'de hız ayarı yoktur.

## 3. Nasıl çalışır?

| Kip | Davranış |
|---|---|
| `python main.py` / `--text` | Model açılışta **bir kez** yüklenir ve kısa bir ısınma üretimi yapılır. Her yanıt önce konsola yazılır, sonra seslendirilir. Yanıt komut döngüsünü **bekletmez** |
| `--no-tts` | Ses motoru oluşturulmaz, model yüklenmez, torch içe aktarılmaz |
| `--demo` | Demo akışı sesle eş zamanlı ilerlesin diye her yanıtın bitmesi beklenir |
| Pocket kullanılamıyor | Açık uyarı + yalnızca konsol. **İngilizce sese otomatik geçilmez** |

- **Kuyruk:** Yanıtlar sırayla, birer birer çalınır; iki yanıt üst üste binmez (süre uyarısı ile komut
  yanıtı aynı anda gelse bile). Bekleyen yanıt sayısı `TTS_MAX_QUEUE`'yu aşarsa, henüz başlamamış en eski
  yanıt seslendirilmez. Metni konsolda zaten görünür.
- **Üretim ve oynatma ayrı:** Ses parça parça üretilir ve hemen çalınır (`tts-synth` ve `tts-play`
  iş parçacıkları). Üretimin bitmesi, sesin çalındığı anlamına gelmez. Oynatmanın bittiği an, hoparlör
  tamponu boşaldığında (PortAudio `stop_stream`) belirlenir.
- **İptal ve kapanış:** Kapanışta son yanıtın bitmesi en fazla `TTS_SHUTDOWN_TIMEOUT_S` saniye beklenir;
  ikinci Ctrl+C beklemeyi keser. Hata durumunda çalan ses yaklaşık 0,1 saniye içinde kesilir.
- **Metin hazırlığı:** Sayı, tarih (15.10.2026), saat (14:30) ve yüzde okunuşunu paketin kendi Türkçe
  normalleştiricisi yapar. J.A.R.V.I.S ek olarak şunları yapar:
  - 2026-10-22 → 22.10.2026;
  - dosya yolları → "ekrandaki dosya";
  - e-posta adresleri → "e-posta adresi";
  - PDF/SMTP → harf harf okunuş;
  - J.A.R.V.I.S → Jarvis.

  Bu yalnızca seslendirilen **kopyadır**. Konsoldaki metin, transkript ve veritabanı değişmez.
- **Hatalar:**
  - Model yüklenemezse, hoparlör üç kez üst üste açılamazsa veya hoparlör takılırsa sesli yanıt kapanır ve
    konsol devam eder.
  - Tek bir yanıtın üretimi başarısız olursa yalnızca o yanıt seslendirilmez.

## 4. Mikrofonun kendi sesini duymaması

- Mikrofon **yalnızca hoparlör gerçekten çalarken** ve çalma bittikten sonraki `TTS_ECHO_GUARD_MS`
  boyunca yok sayılır. Ses üretilirken (henüz çalmazken) dinleme sürer.
- Hoparlör çalmaya başladığı anda kullanıcının söylemekte olduğu cümle **silinmez**. O ana kadarki kısım
  ayrı bir bölüt olarak tanınır. Bu davranış yalnızca Pocket motorunda açıktır; eski pyttsx3 motorunda
  önceki davranış korundu.
- Hoparlör veya üretim 3 saniyeden uzun takılırsa mikrofon yeniden dinlemeye başlar; takılan bir ses
  mikrofonu sonsuza kadar kapatamaz.
- **Desteklenmeyenler (açıkça):** Gerçek akustik yankı engelleme (AEC) yoktur. Söz kesme (barge-in) de
  yoktur: Jarvis konuşurken söylenenler duyulmaz ve transkripte girmez. Önemli bir şey söylenecekse
  Jarvis'in susması beklenmelidir.
- Bluetooth hoparlör veya yüksek ses kullanılıyorsa `TTS_ECHO_GUARD_MS=1000`–`1200` önerilir.

## 5. Performans ölçümü

```powershell
python main.py --tts-bench                        # CPU, her cümle 3 tekrar
python main.py --tts-bench --tts-runs 5 --note "Qwen açık, Teams kapalı"
python main.py --tts-bench --tts-play             # ayrıca ilk cümleyi hoparlörden çalar (dinleyin)
```

Rapor dosyaları `output\tts_bench\tts_bench_*.md` ve `.json` olarak kaydedilir. Ölçülenler:

- model yükleme süresi (ses üretimi hariç);
- ilk (soğuk) üretim;
- yüklü modelde ilk parçaya kadar geçen süre ve toplam üretim süresi;
- ses süresi ve RTF (gerçek zaman faktörü = üretim süresi / ses süresi);
- RAM: yükleme öncesi, yükleme sonrası ve en yüksek;
- TTS üretim yaparken ve boştayken komut işleme gecikmesi (geçici veritabanıyla; Google STT ve LLM hariç).

Önerilen hedefler (sizin ölçümünüzle doğrulanmalı):

| Hedef | Değer |
|---|---|
| İlk ses, yüklü model, p95 | ≤ 0,8 sn |
| RTF, medyan | ≤ 0,5 |
| Komut gecikmesi artışı, p95 | ≤ 50 ms |

Doğallık ve akıcılık otomatik testlerle kanıtlanamaz; dinleyerek değerlendirilmelidir.

## 6. GPU (yalnızca ölçümden sonra)

Varsayılan `cpu`'dur, çünkü RTX 3060 Laptop'un 6 GB VRAM'ini Qwen kullanıyor. CPU hedefleri
sağlanmazsa:

1. Önce `TTS_CPU_THREADS=2` veya `4` ile `--tts-bench` tekrarlayın.
2. `python -c "import torch; print(torch.cuda.is_available())"` `False` ise PyTorch CPU sürümüdür
   (PyPI'daki Windows paketi). CUDA'lı PyTorch ayrı kurulur; bu, `.venv`'deki torch'u değiştirir ve
   birkaç GB indirir. Kurmadan önce karar verin.
3. Qwen yüklüyken (`ollama ps`) `nvidia-smi` ile boş VRAM'i görün, sonra
   `python main.py --tts-bench --tts-device cuda` çalıştırın. Raporda GPU boş/toplam bellek ve TTS'nin tepe
   GPU belleği yer alır.
4. Ancak Qwen ve TTS birlikte sorunsuz çalışıyorsa `.env` içinde `TTS_DEVICE=cuda` yapın.

## 7. Kontrol ve sorun giderme

```powershell
python main.py --mic-check     # 3/3: "Merhaba, ben JARVIS..." cümlesi Pocket ile çalınır; çalınamazsa HATA
$env:JARVIS_TTS_SMOKE="1"; python -m pytest tests\test_tts_pocket_smoke.py -v -s; Remove-Item Env:JARVIS_TTS_SMOKE
```

| Belirti | Çözüm |
|---|---|
| "Pocket TTS Turkish kurulu değil" | `pip install -r requirements-tts.txt` (.venv etkinken) |
| "model bu bilgisayarda bulunamadı" | `python main.py --tts-download` veya `TTS_MODEL_DIR` |
| "TTS_VOICE='male_1' modelde yok" | Model klasöründe `voices\male_1.wav` olmalı; `--tts-download` ile yeniden indirin |
| Ses yok, konsol çalışıyor | `python main.py --list-mics` ile hoparlörü bulun → `TTS_OUTPUT_DEVICE` |
| Ses takılıyor | `--tts-bench` RTF'ye bakın; `TTS_PREBUFFER_MS=400`, `TTS_CPU_THREADS` deneyin |
| Jarvis kendi sesini komut sanıyor | `TTS_ECHO_GUARD_MS` artırın; hoparlörü mikrofondan uzaklaştırın |
| Açılış uzun sürüyor | Model yükleme süresi `J.A.R.V.I.S hazır` satırında yazar; `--tts-bench` ile ölçün |

## 8. Lisans ve atıf

- Kod: pocket-tts-turkish Apache-2.0, pocket-tts MIT.
- Model ağırlıkları: **CC-BY-4.0**, Kyutai Pocket TTS türevi. Atıf: *Pocket TTS Turkish — Khaled Moawad,
  Ammar Rashed, Ekrem Çetinkaya (Wite Tech), https://huggingface.co/wite-tech/pocket-tts-turkish*.
- Yerleşik sesler sentetiktir. Ses klonlama J.A.R.V.I.S'te kullanılmaz.
- Ayrıntı: `LICENSES_THIRD_PARTY.md`.
