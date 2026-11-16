# Sesli Yanıtı Kesme: "Jarvis, dur" ve Esc

Son güncelleme: 16.10.2026 (`faz5c2_voice_interrupt.patch`).

## 1. Mevcut yapının incelemesi

**Pocket TTS iki ayrı iş parçacığıyla çalışıyordu:**
- `tts-synth` sesi üretir.
- `tts-play` sesi hoparlöre en fazla 100 ms'lik parçalarla yazar.
- Bu yüzden oynatma, TTS'i veya uygulamayı kapatmadan ~0,1 sn içinde durdurulabiliyordu (`cancel_all()`); ama
  bunu tetikleyen bir kullanıcı yolu yoktu.

**Yankı koruması ve kesme sorunu:**
- Hoparlör çalarken mikrofon çerçeveleri tamamen atılıyordu.
- Bu, asistanın kendi sesini komut sanmasını önlüyordu.
- Aynı nedenle konuşma sırasında söylenen "Jarvis, dur" hiç duyulmuyordu.

**Mikrofon** yalnızca `AudioCapture` iş parçacığı tarafından okunuyor. Bu kural korundu.

## 2. Yapılan

| Yol | Nasıl çalışır |
|---|---|
| **"Jarvis, dur / sus / yeter / dinle"** (hoparlör çalarken) | Susturma penceresindeki çerçevelerin bir kopyası yalnızca `BargeInDetector`'a gider (`voice/barge_in.py`). Ayrı ve kısa bir bölütleyici her oynatma başında yankı düzeyine göre kalibre olur; kullanıcının sesi yankının `BARGE_IN_ECHO_MARGIN` katını aşmalıdır. En fazla 3 sn'lik bölütler, ayrı bir iş parçacığında ve aynı anda en fazla bir istekle tanınır. Kural deterministiktir, Qwen'e gitmez: yalnızca hitap + durdurma sözcüğü (en fazla 4 sözcük) kabul edilir. Tek etkisi sesi kesmektir. |
| **"Jarvis, dur"** (sessizken veya klavyede) | Normal komut (`STOP_SPEECH`): sesi keser. Bekleyen onaya, soruya veya kayıtlara dokunmaz. Yanıtı seslendirilmez, yalnızca konsola yazılır. |
| **Esc** | Sesli kipte arka planda tuş izleyici çalışır. Klavye kipinde Windows'ta satır tek bir okuyucuyla okunur, böylece `input()` ile çakışma olmaz; yazılan satır korunur. Esc yalnızca konuşma sırasında sesi keser, uygulamayı kapatmaz. **Ctrl+C** eskisi gibi uygulamayı kapatır (kayıt ve ara PDF). |

**Güvenlik kuralları:**
- Susturma penceresinde tanınan başka **hiçbir** metin asistana iletilmez. Hoparlörden gelen "evet" veya
  "onaylıyorum" hiçbir kaydı tetikleyemez; en kötü durumda sesi gereksiz yere keser.
- Tanınan "Jarvis, dur", o an çalınan cümlede de geçiyorsa ("…«Jarvis, dur» diyebilirsiniz") yankı sayılır ve yok
  sayılır.
- Algılayıcıda oluşan bir hata mikrofon döngüsünü durdurmaz.
- Kapanışta algılayıcının iş parçacığı zaman aşımıyla kapanır.
- "Son söylediğini tekrar et", kesilen yanıtı yeniden okur.

## 3. Dürüst sınırlar (gerçek donanımda ölçülmedi)

- **Yankı engelleme (AEC) yok.** Hoparlör mikrofona yakın ve yüksek sesliyse kullanıcının sesi eşiği aşamayabilir;
  sesli kesme o zaman çalışmaz. Kulaklık veya mikrofondan uzak hoparlör önerilir. **Esc her durumda çalışır.**
- **Gecikme.** Kesme, Google STT'nin tanıma süresi kadar (tipik olarak 1–2 sn) gecikir. Esc ~0,1 sn'dir.
- **Ek istek.** Oynatma sırasında yankıdan belirgin biçimde yüksek her kısa ses Google STT'ye gider (pencere
  başına en fazla 4 istek).
- **Motor.** Sesli kesme yalnızca Pocket TTS ile etkindir; oynatma anı yalnızca bu motorda biliniyor. pyttsx3
  (SAPI5) ile "Jarvis, dur" ve Esc en iyi çaba düzeyindedir.
- **Yanlış kesme.** Gürültülü ortamda arka plandaki birinin "Jarvis, dur" demesi de sesi keser. Zararsızdır, ama
  olursa `BARGE_IN_ECHO_MARGIN` artırılabilir.
- **Tuş okuma.** Gerçek Windows konsolunda tuş okuma, sahte tuş dizileriyle test edildi. Windows Terminal ve klasik
  konsolda ayrıca denenmesi gerekir.

## 4. Ayarlar (`.env`)

```
BARGE_IN_ENABLED=true        # false: konuşurken yalnızca Esc çalışır
BARGE_IN_ECHO_MARGIN=2.5     # yanlış kesme oluyorsa 3-4; hiç algılamıyorsa 2.0
KEYBOARD_STOP=true           # Esc ile kesme
```

## 5. Önerilen gerçek prova

1. `python main.py`, ardından "Jarvis, toplantıyı başlat" ve "Jarvis, gündemi oku".
   - Okuma sürerken "Jarvis, dur" deyin.
   - Sesin kaç saniyede kesildiğini not edin.
   - Hemen ardından "Jarvis, sıradaki madde" deyin.
2. Aynısını hoparlörü yüksek açıp kulaklıksız deneyin: algılanıyor mu, yanlış kesme oluyor mu?
3. Bir onay sorusu sırasında hiç konuşmayın: asistanın kendi "evet" sözcüğü hiçbir işlem yapmamalı. Ardından
   onay hâlâ bekliyor olmalı.
4. Uzun yanıtta Esc'e basın. Klavye kipinde (`--text`) yazarken Esc'e basın: yazdığınız satır korunmalı.
5. Ctrl+C ile çıkın: toplantı kaydedilmeli, ara PDF oluşmalı.
6. `python main.py --trace-report` çıktısını ve `logs/jarvis.log` içindeki "Sesli kesme" satırlarını gönderin.
