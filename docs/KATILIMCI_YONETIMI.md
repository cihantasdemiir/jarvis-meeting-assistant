# Katılımcı Yönetimi ve Yoklama

Son güncelleme: 16.10.2026, tam ad eşleştirme (`faz6_participant_name_fix.patch`, bkz. §3).

## 1. Kişiler nereden geliyor? (mevcut davranışın incelemesi)

Kod incelemesiyle doğrulanan davranış (`MeetingManager.start_meeting`, `loaders.load_participants`):

| Kavram | Nerede tutulur | Davranış |
|---|---|---|
| **Kurumsal kişi rehberi** | `data/participants.json` (`.env` → `PARTICIPANTS_FILE`) | Program bu dosyayı **hiç değiştirmez**. |
| **Toplantının aday/davetli listesi** | Her toplantının kendi kaydı (`Meeting.participants`, SQLite) | Yeni toplantı açılırken rehber dosyası okunur ve listeye **kopyalanır**. Toplantı sırasında eklenen misafirler yalnızca o toplantının listesine girer (`added_during_meeting`); rehbere ve sonraki toplantılara geçmez. |
| **Yoklama (katıldı)** | Aynı kayıtta `present` alanı | Yeni toplantıda herkes **katılmadı** olarak başlar. Önceki toplantının yoklaması kopyalanmaz. |

**Yarım kalan toplantı.** Program açılışta yarım kalan toplantıyı kendi listesi ve yoklamasıyla **kurtarır**; bu
yeni toplantı açmak değildir. Açık toplantı varken "toplantıyı başlat" reddedilir ("Zaten açık bir toplantı var").
Yeni toplantı için önce "toplantıyı bitir" denmelidir.

**Bu fazda eklenen:** Belirli bir toplantı için başka bir liste veya gündem şablonu seçilebilir:

```powershell
python main.py --participants data\ekip_a.json --agenda data\haftalik_gundem.json
```

Bu dosyalar yalnızca **yeni açılacak** toplantıda okunur; kurtarılan toplantının listesi değişmez.

## 2. Katılımcı düzeltmeleri

STT bir adı yanlış anladığında üç ayrı düzeltme yapılabilir. Üçü de aynı işlem değildir ve her biri
**onay ister**. Onaya kadar bellek ve SQLite değişmez.

| İşlem | Örnek komut | Etki | Güvenlik |
|---|---|---|---|
| **A. Yoklama düzeltmesi** | "Jarvis, Ahmet'i yoklamadan çıkar." · "Jarvis, Ayşe yanlışlıkla katıldı görünüyor." | Kişi listede kalır, **katılmadı** olur. Söz kendisindeyse söz kapatılır. | Konuşma, söz ve görev kayıtları olduğu gibi kalır. |
| **B. Listeden çıkarma** | "Jarvis, Recep'i bu toplantıdan kaldır." · "Jarvis, yanlış eklenen Mehmet Ayşe kaydını kaldır." | Kayıt bu toplantının listesinden çıkarılır: yoklamada, e-posta alıcılarında ve ad eşleştirmede kullanılmaz. **Silinmez**; `removed_participants` içinde nedeni ve zamanıyla durur, PDF'te not olarak görünür. | Kişiye bağlı görev, söz, konuşma kaydı varsa veya tutanak e-postası gönderilmişse **yapılmaz**; A veya C önerilir. Rehberdeki kişi rehberde kalır. |
| **C. Ad düzeltme** | "Jarvis, Ahmet olarak kaydettiğin kişinin adı aslında Ahmet Yılmaz." · "Jarvis, Recep'in adını Recai Kaya olarak düzelt." | Doğru ad listede yoksa kayıt yeniden adlandırılır; ilk biçim `former_name` olarak saklanır. Doğru ad listede varsa iki kayıt **birleştirilir**: yoklama, söz, transkript ve görev ilişkileri doğru kişiye taşınır, yanlış kayıt listeden çıkarılır. | Yalnızca toplantı sırasında eklenmiş kayıtların adı değişir; rehberdeki kişinin adı toplantıdan değiştirilmez. Konuşma metinleri değişmez. |

Diğer kurallar:
- **Belirsiz ad:** "Yılmaz" gibi bir ad iki kişiye uyuyorsa adaylar sorulur ("Ayşe Yılmaz mı, Ahmet Yılmaz mı?").
  Rastgele seçim yapılmaz.
- **Geri alma:** "Jarvis, geri al" son katılımcı düzeltmesini geri alır (A, B ve C; birleştirme dahil).
- **Kapsam:** Yalnızca açık toplantı değişir. Geçmiş toplantılar, daha önce oluşmuş PDF'ler ve rehber dosyası değişmez.
- **Kayıt hatası:** Kayıt sırasında hata olursa değişiklik bellekte de geri alınır; yarım değişiklik kalmaz.
- **Hitapsız düzeltme komutu:** "Jarvis" duyulmadan söylenen bir düzeltme komutu asla çalışmaz; transkriptte
  "komut gibi duyuldu" diye işaretlenir.
- **Katılımcı listesi:** "Jarvis, katılımcı listesini oku" şunları söyler: katılanlar, katılmayanlar, toplantı
  sırasında eklenenler, ad düzeltmeleri ve listeden çıkarılan kayıtlar.

## 3. Tam ad eşleştirme ve aynı adlı kişiler

- **Eşleştirme kaynağı:** toplantı listesi + rehberde (`participants.json`) bulunup listede olmayan kişiler.
  - Rehber dosyası toplantı açıldıktan sonra güncellendiyse veya toplantı kurtarıldıysa da kişi tam adıyla bulunur.
  - Bulunan kişi rehberdeki e-posta ve rolle toplantıya eklenir.
- **Tek eşleşme:** "Mehmet katıldı", "Söz Mehmet'te", "görev: Mehmet …" → "Mehmet Öztürk". Ad kayıttan önce çözülür.
- **Birden çok eşleşme:** "İki Mehmet var: Mehmet Öztürk ve Mehmet Yılmaz. Hangisini kastediyorsunuz?"
  - Yanıt: tam ad, soyad, «birinci», «ikinci», «sonuncusu» veya sıra numarası.
  - Yanıt hiçbir adaya uymuyorsa ya da iki adaya birden uyuyorsa tahmin yapılmaz.
  - Seçimden sonra da işlem yine onay ister.
- **Çok adlı yoklama:** belirsiz adlar sırayla sorulur. Liste tamamen netleşip onaylanana kadar listedeki hiç kimse işaretlenmez.
- **Görev sorumlusu:** belirsizse görev sorumlusuz kaydedilir ve hemen sorulur. Seçilen kişi onaydan sonra yalnızca o göreve yazılır.
- **Rehberde olmayan kişi:**
  - Eklenir.
  - Benzer kayıtlı kişi varsa ikinci kayıt açılmaz, kullanıcıya sorulur.
  - Soyadı uydurulmaz.
- **Geçmiş kayıtlar:** otomatik değiştirilmez. Daha önce "Mehmet" diye kaydedilmiş görev ve yoklamalar olduğu gibi kalır.
  - Açık toplantıda düzeltme için: «Jarvis, Mehmet olarak kaydettiğin kişinin adı aslında Mehmet Öztürk».
- **Sınır:** eşleştirme metin üzerindendir; kişiyi sesinden tanımaz. STT adı bambaşka duyarsa (ör. "Mehmet" yerine "Mahmut") rehberdeki kişiyle eşleşmeyebilir.
