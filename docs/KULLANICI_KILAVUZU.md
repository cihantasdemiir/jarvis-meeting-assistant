# Kullanıcı Kılavuzu — Toplantı Başkanı İçin

Bu kılavuz, toplantıyı J.A.R.V.I.S ile yöneten kişi için yazıldı. Kurulum için: `KURULUM.md`.

**Temel kural:** Jarvis'e verilen her komut **"Jarvis"** sözcüğüyle başlar. "Jarvis" ile başlamayan cümleler toplantı konuşmasıdır ve o an sözü olan kişi adına transkripte yazılır.

## 1. Toplantı öncesi hazırlık

1. `data\participants.json`: davetlilerin **tam adı**, e-postası ve rolü.
2. `data\agenda.json`: gündem maddeleri ve planlanan dakikalar.
3. `.env`: `MEETING_TITLE`, `MEETING_CHAIR`, `MEETING_LOCATION`. Tutanak göndermek için SMTP ayarları (bkz. `KURULUM.md` §9).
4. Kontrol: `python main.py --mic-check`.
5. Başlatma: `python main.py` (mikrofon) veya `python main.py --text` (klavye).

## 2. Toplantıyı başlatma ve bitirme

| Söyleyin | Sonuç |
|---|---|
| "Jarvis, toplantıyı başlat" | Toplantı açılır; gündem sayısı, toplam süre ve ilk madde söylenir. Önceki toplantılardan devreden açık görev varsa bildirilir. |
| "Jarvis, toplantıyı başlat: Haftalık Üretim" | Başlığı belirterek başlatır. |
| "Jarvis, toplantıyı bitir" | Söz süresi kapanır, toplantı kaydedilir, PDF tutanak üretilir ve kapanış özeti okunur. |

## 3. Katılımcılar ve yoklama

| Söyleyin | Sonuç |
|---|---|
| "Jarvis, Ahmet katıldı" | Rehberdeki tam adla yoklamaya işlenir. Yanıtta kaydedilen tam ad söylenir ("Kaydedilen: Ahmet Yılmaz."). |
| "Jarvis, katılımcılar: Kenan, Ayşe, Mehmet" | Birden çok kişi işlenir. Aynı kişi iki kez eklenmez. |
| "Jarvis, yoklama al" | Katılan ve katılmayanlar okunur. |
| "Jarvis, katılımcı listesini oku" | Toplantı listesi okunur. |
| "Jarvis, Ahmet'i yoklamadan çıkar" | Kişi "katılmadı" olarak işaretlenir (onay ister). |
| "Jarvis, Recep'i bu toplantıdan kaldır" | Yanlış eklenen kayıt bu toplantının listesinden çıkarılır (onay ister). |
| "Jarvis, Ahmet olarak kaydettiğin kişinin adı aslında Ahmet Yılmaz" | Ad düzeltilir (onay ister). |

**Rehberde olmayan kişi:**
- Kişi bu toplantıya eklenir; soyadı uydurulmaz.
- Benzer adlı kayıtlı biri varsa ikinci kayıt açılmadan sorulur.

## 4. Aynı adlı kişiler arasında seçim

Rehberde iki "Mehmet" varsa Jarvis şöyle sorar:

> "İki Mehmet var: Mehmet Öztürk ve Mehmet Yılmaz. Hangisini kastediyorsunuz? Tam adı, soyadı veya «birinci», «ikinci» diyebilirsiniz."

- **Yanıt:** "Mehmet Yılmaz", "Yılmaz", "birinci" veya "ikinci". "Jarvis" demeniz gerekmez.
- **Onay:** Jarvis seçimi onaylatır ("… kaydedilsin mi?"). "Evet" deyince kayıt yapılır; "hayır" deyince iptal edilir.
- **Kayıt öncesi:** Seçim çözülene kadar yoklama, söz veya görev sorumlusu kaydı yapılmaz.
- **Transkript:** Seçim ve onay yanıtları transkripte yazılmaz.
- **Süre:** Hitapsız yanıt, Jarvis'in sorusu bittikten sonra yaklaşık 20 saniye içinde verilmelidir. Daha sonra "Jarvis, Mehmet Yılmaz" diyerek yanıtlayabilirsiniz.

## 5. Gündem

| Söyleyin | Sonuç |
|---|---|
| "Jarvis, gündemi oku" | Maddeler, süreler ve durumlar okunur. |
| "Jarvis, sıradaki madde" / "Jarvis, önceki madde" | Gündemde ilerler veya geri döner. |
| "Jarvis, maddeyi ertele" | Etkin madde ertelenir. |
| "Jarvis, gündeme ekle: Bütçe, 15 dakika" | Yeni madde eklenir. |
| "Jarvis, süre durumu" | Etkin maddede geçen ve kalan süre söylenir. |

**%80 süre uyarısı:**
- Etkin madde planlanan sürenin %80'ine ulaşınca Jarvis bir kez uyarır ("… maddesi için yaklaşık 1 dakika kaldı."). Örneğin 5 dakikalık maddede yaklaşık 4. dakikada.
- Aynı madde için uyarı tekrar edilmez; program yeniden başlatılsa da tekrarlanmaz.

## 6. Konuşmacı seçme ve transkript

| Söyleyin | Sonuç |
|---|---|
| "Jarvis, söz Ayşe'de" | Ayşe Yılmaz etkin konuşmacı olur; önceki konuşmacının süresi kapanır. |
| "Jarvis, sözü bırak" | Etkin konuşmacı kalmaz. |
| "Jarvis, konuşma süreleri" | Kişi başı söz süresi ve söz alma sayısı. |
| "Jarvis, kim konuşmadı" | Söz almayan katılımcılar. |

- "Jarvis" ile başlamayan her cümle, o an sözü olan kişi adına ve zaman damgasıyla transkripte yazılır.
- Söz kimsede değilse konuşmacı "Belirtilmemiş" olarak kaydedilir; rastgele kişi atanmaz.
- Sistem kişiyi sesinden tanımaz. Söz başkasına geçince "Jarvis, söz …'de" demeyi unutmayın.
- "Söz süresi", sözün kişide kaldığı süredir; kişinin gerçekten konuştuğu süre değildir.

## 7. Karar, görev, sorumlu ve son tarih

| Söyleyin | Sonuç |
|---|---|
| "Jarvis, karar: Yeni üretim planı pazartesi uygulanacak" | Karar etkin gündem maddesine bağlı kaydedilir. |
| "Jarvis, görev: Mehmet raporu haftaya kadar hazırlasın" | Görev, sorumlu (Mehmet Öztürk) ve son tarih (toplantı tarihi + 7 gün) çıkarılır. |
| "Jarvis, sorumlu: Ayşe Yılmaz" | Son kaydedilen görevin sorumlusunu belirler. |
| "Jarvis, son tarih: 21 Ekim" | Son kaydedilen görevin tarihini belirler. |
| "Jarvis, görev tamamlandı: rapor" / "Jarvis, görev iptal: rapor" | Görevin durumunu değiştirir. |
| "Jarvis, not: Hat 2 bakımda" | Gündem maddesine not ekler. |
| "Jarvis, geri al" | Son kaydı geri alır. |

- **Anlaşılan tarih ifadeleri:** bugün, yarın, haftaya, gelecek cuma, 15 Ekim, 21 Ekim 2026, üç gün sonra.
- **Belirsiz bilgi:** Sorumlu veya tarih anlaşılamazsa uydurulmaz; görev "belirlenmedi" olarak kaydedilir ve bildirilir.

## 8. Geçmiş toplantı ve devreden işler

| Söyleyin | Sonuç |
|---|---|
| "Jarvis, önceki toplantıyı özetle" | Son toplantının tarihi, katılanları, kararları ve açık görevleri. |
| "Jarvis, açık görevleri oku" | Bu ve önceki toplantılardan açık görevler. |
| "Jarvis, görev tamamlandı: rapor" | Devreden görev kapatılır; artık açık görevlerde görünmez. |

Tutanakta "Devreden İşler" bölümü önceki toplantılardan gelen açık ve bu toplantıda kapatılan görevleri gösterir.

## 9. Oylama ve gündem dışı konular

| Söyleyin | Sonuç |
|---|---|
| "Jarvis, oylama: fazla mesai lehte 5 aleyhte 2 çekimser 1" | Oylar ve sonuç kaydedilir. |
| "Jarvis, otopark: Ofis taşınması" | Gündem dışı konu otopark listesine alınır. |

- Oy sayıları anlaşılamazsa tahmin edilmez.
- Toplam oy, yoklamaya işlenen kişi sayısından fazlaysa oy kaydedilir ama uyarı verilir. Oylamadan önce yoklamayı tamamlayın.

## 10. Sonraki toplantı ve takvim daveti

| Söyleyin | Sonuç |
|---|---|
| "Jarvis, sonraki toplantı 21 Ekim saat 14.00" | Tarih ve saat kaydedilir; `.ics` takvim daveti oluşturulur. |
| "Jarvis, sonraki toplantı 21 Ekim" | Tarih kaydedilir; saat söylenmediği için davet oluşturulmaz (saat uydurulmaz). |
| "Jarvis, takvim daveti oluştur" | Kayıtlı tarih ve saatten davet oluşturur. |
| "Jarvis, toplantı davetini e-postaya ekle" | Davet tutanak e-postasına eklenir (göndermez). |

"Saat 2" gibi belirsiz saatlerde hiçbir şey kaydedilmez; Jarvis "saat 14.00" veya "sabah saat 9" biçiminde tekrar söylemenizi ister.

## 11. PDF tutanak ve Excel

| Söyleyin | Sonuç |
|---|---|
| "Jarvis, tutanak oluştur" | Toplantı sürerken **ara tutanak**, bittikten sonra tutanak üretir (`output\`). |
| "Jarvis, görevleri Excel'e aktar" | Bu toplantının görevleri ve devreden işler `.xlsx` olarak (`output\excel\`). |
| "Jarvis, açık görevleri Excel olarak çıkar" | Tüm açık görevler. |

**Komut satırından:**
- `python main.py --minutes`: son toplantının PDF'ini yeniden üretir.
- `python main.py --export-tasks acik`: tüm açık görevleri Excel'e aktarır.

**Tutanak içeriği:**
- Bölümler: künye, yönetici özeti, katılım, gündem, kararlar, görevler, devreden işler, oylama, otopark, konuşmacı istatistikleri, sonraki toplantı.
- Transkript varsa Ek A olarak eklenir.
- Verisi olmayan bölümler basılmaz.

## 12. E-posta gönderme

| Söyleyin | Sonuç |
|---|---|
| "Jarvis, tutanağı gönder" | PDF (ve varsa davet) katılımcı listesindeki adreslere gönderilir. |
| "Jarvis, tutanağı tekrar gönder" | Daha önce gönderilenlere de yeniden gönderir. |

- Aynı tutanak aynı kişiye ikinci kez kendiliğinden gönderilmez.
- Gönderim başarısız olursa PDF ve toplantı korunur; hata Türkçe açıklanır. Tekrar denemek için komutu yeniden verin.
- `MAIL_DRY_RUN=true`, `--demo` ve `--demo-live` kiplerinde e-posta gönderilmez; yalnızca `.eml` taslağı kaydedilir ve bu açıkça söylenir.

## 13. Ctrl+C ve kurtarma

- Ctrl+C ile çıkınca toplantı kaydedilir ve **ara tutanak PDF'i** üretilir. Toplantı kapatılmaz.
- Program yeniden açıldığında yarım toplantı kendiliğinden kurtarılır ve kaldığı maddeden devam eder.
- Program kapalıyken geçen süre söz süresine eklenmez.

## 14. Canlı demo modu ve veri izolasyonu

`python main.py --demo-live`, video veya sunum için **temiz** bir toplantı açar.
- Normal veritabanını okumaz ve yazmaz; geçmiş toplantılar ve açık görevler devralınmaz.
- Veriler `data\demo_live\<zaman>\`, çıktılar `output\demo_live\<zaman>\` altına yazılır.
- E-posta gönderilmez.

Ayrıntılar: `DEMO_CANLI.md`.

## 15. Diğer yardımcı komutlar

| Söyleyin | Sonuç |
|---|---|
| "Jarvis, evet" / "Jarvis, hayır" | Jarvis'in önerdiği işlemi onaylar veya reddeder. Soru yeni sorulduysa "evet"/"hayır" yeterlidir. |
| "Jarvis, son söylediğini tekrar et" | Son yanıtı tekrarlar. |
| "Jarvis, dur" | Yalnızca sesli yanıtı keser; klavyede **Esc** tuşu da keser. Kayıtlar ve onaylar etkilenmez. |
| "Jarvis, yardım" | Komut listesini okur. |
| "Jarvis, çık" | Programı kapatır. |

**Yerel LLM açıksa** (`LLM_ENABLED=true`):

| Söyleyin | Sonuç |
|---|---|
| "Jarvis, toplantıyı analiz et" | Transkriptten karar/görev **taslakları** ve özet taslağı çıkarır; resmî kayıt yapmaz. |
| "Jarvis, taslakları oku" / "Jarvis, taslak 3 onayla" / "Jarvis, taslak 3 reddet" | Taslakları inceleyip onaylar. Yalnızca onaylananlar karar/görev olur. |
| "Jarvis, özet taslağını oku" | Özet taslağını okur. |
| "Jarvis, soru: geçen toplantıda bütçe için ne karar verdik?" | Kayıtlı geçmiş toplantılardan yanıt arar. |

LLM kapalıyken analiz kural tabanlı çalışır. Doğal dille söylenen ve kurallarca anlaşılamayan komutlara "anlayamadım" denir; komutu kılavuzdaki biçimde tekrarlayın.

## 16. Komut listesi (özet)

Tablolardaki örneklerin tamamı (52 komut) uygulamanın komut ayrıştırıcısından geçirildi ve beklenen işleme karşılık geldiği doğrulandı. Sesli kullanımda konuşma tanıma hataları ayrıca etkili olabilir.
