# Windows Ses Kurulumu ve Gerçek Mikrofon Testleri

Bu belge Faz 4'ün **sizin bilgisayarınızda** yapılması gereken adımlarını anlatır. Bu adımlar şunlardır:
- Mikrofon kurulumu
- Türkçe sesin (Microsoft Tolga) kurulumu
- Sesli komut doğruluğunun ölçülmesi (hedef ≥ %85)
- Yanıt süresinin ölçülmesi (T-01, ≤ 5 sn)

Otomatik testler (`pytest`) mikrofon kullanmaz; yapay ses ve taklit STT ile çalışır. Gerçek doğruluk ancak
aşağıdaki adımlarla ölçülebilir.

---

## 1. Kurulum (PowerShell)

```powershell
cd jarvis-meeting-assistant
.\.venv\Scripts\Activate.ps1
pip install -r requirements-voice.txt
python -c "import pyaudio, speech_recognition, pyttsx3; print('ses kütüphaneleri hazır')"
```

Python 3.13 notu: `SpeechRecognition` 3.12 ve sonrası Python 3.13'te kaldırılan `audioop` modülü yerine
`audioop-lts` paketini otomatik kurar. Ek bir işlem gerekmez.

| Sorun | Çözüm |
|---|---|
| `PyAudio` kurulamıyor | `python -m pip install --upgrade pip` ardından `pip install PyAudio`. Python 3.11–3.13 için Windows paketi hazırdır. |
| `ModuleNotFoundError: comtypes` | `pip install comtypes pywin32` |
| Sesli yanıt ilk cümleden sonra susuyor | `pip install pyttsx3==2.90` ile tekrar deneyin. Uygulama takılmaz; sesli yanıtı kapatıp konsola yazmaya devam eder. |

## 2. Windows ayarları

1. **Mikrofon izni:** Ayarlar → Gizlilik ve güvenlik → Mikrofon. Şu iki seçenek açık olmalı:
   - "Mikrofon erişimi"
   - "Masaüstü uygulamalarının mikrofonunuza erişmesine izin verin"
2. **Türkçe ses (Microsoft Tolga):**
   1. Ayarlar → Saat ve dil → Konuşma → "Sesler" bölümünden **Türkçe** ses paketini ekleyin.
   2. Paket yüklendikten sonra bilgisayarı yeniden başlatın.
   3. pyttsx3'ün sesi görüp görmediğini kontrol edin:

      ```powershell
      python -c "import pyttsx3; [print(v.name, '|', v.id) for v in pyttsx3.init().getProperty('voices')]"
      ```

   4. **Listede Tolga yoksa:** Dil paketiyle gelen ses "OneCore" sesi olarak kurulmuştur. pyttsx3 ise
      yalnızca klasik SAPI5 seslerini listeler.
      - Uygulama bu durumda çalışmaya devam eder: varsayılan (İngilizce) sesle okur ve uyarı verir.
      - Bilinen geçici çözüm, Kayıt Defteri'nde `HKLM\SOFTWARE\Microsoft\Speech_OneCore\Voices\Tokens`
        altındaki Türkçe ses anahtarını (adı `TR-TR` / `Tolga` içerir) `HKLM\SOFTWARE\Microsoft\Speech\Voices\Tokens`
        altına kopyalamaktır. 32 bit Python kullanılıyorsa hedef `WOW6432Node` altındaki aynı yoldur.
      - Bu işlem yönetici yetkisi ister. Kurumsal bilgisayarda BT ekibine yaptırılması önerilir. Önce
        Kayıt Defteri yedeği alınmalıdır.
      - Değişiklikten sonra 3. adımdaki komutu tekrar çalıştırın; Tolga listede görünmelidir.
      - Yardımcı betik (Faz 5a, **Windows'ta henüz denenmedi**). Önce yalnızca kontrol kipinde çalıştırın:

        ```powershell
        powershell -ExecutionPolicy Bypass -File scripts\tolga_sapi5.ps1          # yalnızca rapor
        # Yönetici PowerShell'de, yedek alarak uygular:
        powershell -ExecutionPolicy Bypass -File scripts\tolga_sapi5.ps1 -Apply
        ```
      - `--mic-check` artık Türkçe ses bulunamazsa bunu **hata** olarak sayar.
3. **Toplantı odası mikrofonu:** Masa mikrofonu veya konferans hoparlörü (ör. Jabra / Poly)
   kullanılıyorsa, Windows ses ayarlarında bu aygıtın varsayılan giriş aygıtı seçili olduğundan emin
   olun. Alternatif olarak `--mic` ile numarasını verin.

## 3. Mikrofonu seçme

```powershell
python main.py --list-mics
```

Listede doğru mikrofonun numarasını bulun. Sonra iki yoldan birini kullanın:
- Her çalıştırmada `--mic 2` seçeneğini ekleyin.
- `.env` dosyasına `MICROPHONE_INDEX=2` yazın.

## 4. Kurulum kontrolü (5 dakika)

```powershell
python main.py --mic-check
```

Kontrol üç adımdan oluşur:
1. **3 saniye sessiz kalın:** Gürültü tabanı ve önerilen eşik ölçülür.
2. **Enter'a basın ve «Jarvis, gündemi oku» deyin:** Google'ın tanıdığı metin ve komut sonucu gösterilir.
3. **Bir cümle seslendirilir:** Hangi Türkçe sesin kullanıldığı yazılır.

"Sonuç: her şey çalışıyor" görmelisiniz. Görmüyorsanız sorunlar aşağıdaki gibi giderilebilir:

| Belirti | Ayar |
|---|---|
| "Konuşma algılanmadı" | Mikrofona yaklaşın. Gerekirse `.env` içinde `VAD_THRESHOLD` değerini önerilen eşiğin %70'ine ayarlayın. |
| Gürültü (klima, projektör) konuşma sanılıyor | `VAD_THRESHOLD` değerini önerilen eşiğin 1,5 katına çıkarın. |
| Uzun komutlar ortadan bölünüyor | `VAD_SILENCE_MS=1000` |
| Jarvis kendi sesine yanıt veriyor | `TTS_ECHO_GUARD_MS=1200` yapın, hoparlör sesini kısın veya kulaklık kullanın. |
| "İnternet bağlantısı yok" | Google STT internet gerektirir. Şirket ağı/proxy `www.google.com` adresine erişimi engelliyor olabilir. |

## 5. Sesli komut doğruluk testi (PDF Hafta 2, hedef ≥ %85)

Test seti 27 maddedir (`jarvis/voice/voice_test_set.json`):
- **23 komut:** Doğruluk bu maddelerden hesaplanır.
- **4 normal konuşma cümlesi:** Bunlar komut sanılmamalıdır; yanlış tetikleme bu maddelerden ölçülür.

```powershell
python main.py --voice-test --save-audio --note "sessiz oda, mikrofona 50 cm"
```

Test şöyle ilerler:
- Her maddede ekranda okunacak cümle görünür. **Enter'a basın, sonra cümleyi normal sesle okuyun.**
- Bir maddeyi atlamak için `a`, testi bitirmek için `q` yazın. Atlanan maddeler doğruluk hesabına
  girmez ve raporda ayrıca gösterilir.
- Bitince `output\voice_tests\` klasörüne iki dosya yazılır:
  - `dogruluk_*.md`: rapor
  - `dogruluk_*.csv`: Excel'de açılabilen tablo
- Raporda şunlar yer alır:
  - Komut doğruluğu
  - Uyandırma kelimesi algılama oranı
  - Yanlış tetikleme sayısı
  - STT süreleri
  - Önerilen `WAKE_WORD_ALIASES` varyantları

**Önerilen ölçüm planı** (PDF: gürültülü ortam testleri dahil):

| Ölçüm | Koşul | `--note` örneği |
|---|---|---|
| 1 | Sessiz oda, mikrofona 50 cm | `"sessiz, 50 cm"` |
| 2 | Toplantı odası, masanın karşı ucu (2–3 m) | `"toplantı odası, 2.5 m"` |
| 3 | Arka planda konuşma / klima açık | `"gürültülü, klima açık"` |
| 4 | Farklı bir konuşmacı (kadın/erkek sesi) | `"ikinci konuşmacı"` |

Uyandırma kelimesi varyantları raporda önerilirse şu adımları izleyin:
1. Önerilen biçimleri `.env` dosyasındaki `WAKE_WORD_ALIASES` satırına ekleyin.
2. Kaydedilmiş seslerle testi **yeniden okumadan** tekrarlayın:

```powershell
python main.py --voice-test-replay output\voice_tests\ses_YYYYMMDD_HHMMSS
```

**Dürüstlük kuralı:** Raporlardaki sayılar elle değiştirilmemelidir. %85 tutmazsa
kendi test kayıtlarınıza olduğu gibi yazılmalı ve iyileştirme (alias, eşik, mikrofon değişikliği)
yapıldıktan sonra ölçüm tekrarlanmalıdır.

## 6. Yanıt süresi (T-01: "Jarvis, toplantıyı başlat" → 5 sn içinde yanıt)

```powershell
python main.py
```

1. «Jarvis, toplantıyı başlat» deyin ve yanıtı bekleyin.
2. `logs\jarvis.log` dosyasında şu biçimde bir satır görünür:

   ```
   Gecikme: STT 1.20 sn, konuşma sonundan yanıta toplam 1.35 sn
   ```

3. Bu satırdaki **toplam** değer 5 sn'den küçük olmalıdır. Not: sesli yanıtın okunma süresi bu değere dahil değildir.

`--voice-test` raporundaki "En uzun konuşma sonu → niyet süresi" satırı da aynı ölçümü verir.

## 7. Gerçek toplantı provası (10 dakika)

```powershell
python main.py
```

Sırayla şunları söyleyin:
1. «Jarvis, toplantıyı başlat»
2. «Jarvis, katılımcılar Kenan, Ayşe ve Mehmet»
3. «Jarvis, söz Ayşe'de»
4. Normal konuşun: 3 cümle (T-03)
5. «Jarvis, görev: Mehmet raporu haftaya kadar hazırlasın»
6. «Jarvis, karar: yeni plan pazartesi uygulanacak»
7. **İnternet testi:** Wi-Fi'yi 20 saniye kapatıp konuşun, sonra açın. "Konuşmalar sırayla bekletiliyor" ve
   "yeniden bağlanıldı" mesajları gelmeli; söylenenler kaybolmamalı.
8. **Mikrofon kopma testi:** USB mikrofonu çıkarıp takın. "Mikrofon bağlantısı koptu" ve "yeniden
   bağlandı" mesajları gelmeli.
9. «Jarvis, toplantıyı bitir»

Ardından `output\` altındaki PDF'te şunları kontrol edin:
- Transkriptteki üç cümle **Ayşe Yılmaz** adına mı?
- Görev ve karar doğru kaydedilmiş mi?

## 8. Sonuçları bildirme

Bana şunları iletin:
- `--mic-check` çıktısı
- Her ölçümün `dogruluk_*.md` dosyası
- Provada gözlenen sorunlar

Bu ölçümler, sesli komut doğruluğunun kendi ortamınızdaki gerçek durumunu gösterir.

## Faz 5a: kelime sonu kesilmesi, yavaş tanıma ve Bluetooth mikrofonlar

**Saha bulgusu (Faz 4):** "toplantıyı başl", "önceki toplantıyı özet" gibi kesilmeler oldu.

- **Neden:** Bölüt bitince yalnızca 200 ms sessizlik tutuluyordu. Türkçe kelime sonları eşiğin altında kalıp
  sessizlik sayılabiliyordu.
- **Faz 5a varsayılanları:** `VAD_TAIL_KEEP_MS=500`, `VAD_RELEASE_PERCENT=50`.
- **Hâlâ kesiliyorsa:** `.env` içinde `VAD_TAIL_KEEP_MS=700` deneyin. Cümle ortasında bölünme oluyorsa
  (raporda "İkiye bölünen konuşma") `VAD_SILENCE_MS=1000` deneyin. Bu ayar yanıt süresini yaklaşık 0,2 sn artırır.
- **Kesik komut:** Jarvis "Komut eksik algılandı" der ve **hiçbir işlem yapmaz**. Komutu baştan söyleyin.

**Yavaş tanıma:** Ses artık Google'a 16 kHz olarak gönderilir (`STT_RESAMPLE_HZ=16000`). `logs\jarvis.log`
içindeki satırlar şöyledir:

```
STT 5.80 sn YAVAŞ (ses 2.1 sn, 16000 Hz, hazırlık 0.03 sn, bekleyen 0 bölüt, ...)
```

- *ses* uzunsa: bölüt uzun demektir (VAD ayarlarına bakın).
- *bekleyen* > 0 ise: önceki bölütler sırada bekliyordur.
- Hiçbiri değilse: gecikme ağdan veya Google'dan kaynaklanıyordur.

**Bluetooth kulaklıklar (AirPods vb.):** Windows, mikrofon açıldığında bu aygıtları dar bantlı
"Hands-Free" profiline geçirir. Faz 4 ölçümünde %70 ve %9,1 sonuçları alındı. Uygulama aygıt adından tahmin
edip uyarı verir. Toplantılarda kablolu veya USB mikrofon kullanın.

**Yeniden ölçüm (önerilen sıra):**

```powershell
python main.py --mic-check --mic <mikrofon numarası>
python main.py --voice-test --save-audio --mic <numara> --note "masa mikrofonu, 50 cm"
# Aynı kayıtlarla farklı ayarları mikrofonsuz karşılaştırmak için:
python main.py --voice-test-replay output\voice_tests\ses_<tarih>
```

Not: `--voice-test-replay`, kayıtlı bölütleri yeniden tanır. VAD ayar değişikliklerinin etkisini göstermez;
VAD için canlı testi tekrarlayın.

## Faz 5a.3: Türkçe sesli yanıt (TTS) yoksa — çözüm önerisi ve güvenli kurulum seçenekleri

> **Faz 5b güncellemesi:** Çevrimdışı seçenek olarak **Pocket TTS Turkish (`male_1`)** seçildi ve entegre
> edildi. Bu artık varsayılan motordur (`TTS_ENGINE=pocket`). Kurulum ve ölçüm için `docs/TURKCE_TTS.md`
> dosyasına bakın. Aşağıdaki tablo, eski pyttsx3/SAPI5 motoru için geçerliliğini koruyor.

Kullanıcının mikrofon testinde bilgisayarda Türkçe TTS sesi bulunamadı. Bu patch **Windows seslerini ve
Kayıt Defteri'ni otomatik olarak değiştirmez**; uygulama hiçbir koşulda yönetici yetkisi istemez. Aşağıdaki
seçenekler en güvenliden başlayarak sıralanmıştır. Hiçbiri geliştirme ortamında Windows'ta denenmedi.

| # | Seçenek | Yetki | Risk | Durum |
|---|---|---|---|---|
| 1 | Konsola yazma (`python main.py --no-tts`) | Yok | Yok | Çalışıyor. Yanıtlar ekranda okunur. Türkçe ses kurulana kadar önerilen kip |
| 2 | Windows Türkçe konuşma paketini yükleme | Kullanıcı (bazı kurumlarda yönetici) | Düşük | Ayarlar → Saat ve dil → Konuşma → Sesler → **Türkçe**. Sonra yeniden başlatın ve aşağıdaki kontrolü çalıştırın |
| 3 | OneCore sesini SAPI5'e görünür yapma (`scripts\tolga_sapi5.ps1`) | Yönetici | Orta: Kayıt Defteri değişir | Betik varsayılan olarak **yalnızca rapor** verir; `-Apply` yedek alır. Windows'ta **denenmedi**. Kurumsal bilgisayarda BT ekibine yaptırın |
| 4 | Çevrimdışı Piper TTS (Türkçe ses modelleri) | Yok (kullanıcı klasörü) | Düşük; ses verisi bilgisayardan çıkmaz | **Uygulamada yok, öneri.** Türkçe ses modellerinin adı, lisansı ve kalitesi kurulumdan önce doğrulanmalı. Entegrasyon ayrı bir faz olarak planlanmalı |
| 5 | Bulut TTS (Azure, Google vb.) | API anahtarı | **Gizlilik:** yanıt metinleri (karar, görev, kişi adları) dışarı gönderilir | **Önerilmez.** KVKK değerlendirmesi ve şirket onayı olmadan kullanılmamalı |

Ses paketinden sonra kontrol (değişiklik yapmaz):

```powershell
python -c "import pyttsx3; [print(v.name, '|', v.id) for v in pyttsx3.init().getProperty('voices')]"
python main.py --mic-check          # Türkçe ses bulunamazsa HATA olarak raporlar
powershell -ExecutionPolicy Bypass -File scripts\tolga_sapi5.ps1   # yalnızca rapor; -Apply KULLANMAYIN
```

Bilinen durum: Windows 10/11'de Türkçe ses paketi çoğu zaman yalnızca OneCore sesi olarak kurulur.
pyttsx3 (SAPI5) bu sesi göremez; bu yüzden 2. seçenek tek başına yetmeyebilir. Bu durumda Türkçe metin
İngilizce sesle okunur ve anlaşılmaz. Ses bulunamadığında toplantıda `--no-tts` kullanılması önerilir.

Sonraki faz için öneri (bu patch'te uygulanmadı): Türkçe ses bulunamazsa sesli yanıtı kendiliğinden
kapatıp konsola yazan bir ayar (`TTS_REQUIRE_TURKISH=true`). Ayrıca seçenek 4 için ayrı bir
`PiperSpeechEngine` eklenebilir.

## Faz 5a.3: mikrofon testindeki tanıma hataları

Gerçek testte görülen hatalar ve uygulamanın tepkisi. Hiçbir düzeltilmiş komut **doğrudan çalışmaz**.
Asistan "Ses tanıma «…» olarak duydu. «Jarvis, …» mı demek istediniz?" diye sorar. Kayıt yalnızca
«Jarvis, evet» ile yapılır.

| Duyulan | Kastedilen | Davranış |
|---|---|---|
| `Jarvis carrer …` (ayrıca `carer`, `karer`, `kerar`, `carar`, `karrar`, `kar ar`) | `karar …` | Onay sorulur. Yalnızca komutun ilk kelimesinde düzeltilir. Soru cümlesinde ("carrer görüşmesi ne zaman?") öneri yapılmaz |
| `… lehte 5 alekte 2 …` | `aleyhte` | Sayılar eksiksizse onay sorulur. Sayı duyulmadıysa tahmin edilmez |
| `maddeye ertele` | `maddeyi ertele` | Onay sorulur |
| `Charles …` | `Jarvis …` | **Varsayılan: hiçbir şey yapılmaz**; satır transkripte yazılır. `.env` içinde `WAKE_WORD_NEAR_MISSES=charles` ayarlanırsa, satır yine transkripte yazılır ve geçerli bir komuta benziyorsa yalnızca onay sorulur. Bitir, gönder, geri al gibi önemli işlemler bu yolla hiçbir zaman önerilmez. Toplantıda Charles adında biri varsa bu ayarı kullanmayın |
