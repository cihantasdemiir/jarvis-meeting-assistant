# PDF Tutanak Şablonu

Tutanak ReportLab ile A4 boyutunda üretilir (`jarvis/reports/pdf_generator.py`). Görünüm kurumdan bağımsızdır ve
`.env` ile uyarlanır; depoda hiçbir kuruma ait logo veya marka varlığı yoktur.

## Yapılandırma

| Anahtar | Açıklama |
|---|---|
| `COMPANY_NAME` | Her sayfanın altbilgisinde ve logo yoksa başlıkta yazılır. Örnek: `Örnek Demir Çelik San. ve Tic. A.Ş.` |
| `LOGO_PATH` | Logo dosyası (PNG önerilir; JPG/WEBP de okunur). Örnek: `assets/logo/sirket_logo.png` |
| `PDF_PRIMARY_COLOR` | Başlık ve tablo başlığı rengi (HTML e-postanın başlık rengi de budur) |
| `PDF_ACCENT_COLOR` | Bölüm numaraları ve üst şerit için ince vurgu rengi |
| `PDF_FONT_REGULAR`, `PDF_FONT_BOLD` | Türkçe destekli TTF yolları (boşsa `assets/fonts/` ve Windows sistem fontları denenir) |

## Logo

- Logo oranı korunarak başlıktaki kutuya sığdırılır; gerilmez.
- `LOGO_PATH` boşsa veya dosya okunamıyorsa logo alanı metinle işaretlenir ve şirket adı düz metin olarak yazılır.
  Uygulama logo çizmez veya taklit etmez.
- `assets/logo/` klasörünün içeriği `.gitignore` ile Git dışında tutulur. Kurumunuza ait logoyu depoya eklemeyin.

## Sayfa yapısı

1. Başlık ve toplantı bilgileri
2. Katılımcılar
3. Gündem
4. Görüşme notları
5. Alınan kararlar
6. Görevler, sorumlular ve son tarihler
7. Devreden işler
8. Oylamalar
9. Konuşmacı istatistikleri
10. Ek A — Transkript

- Her sayfada üst bilgi ("TOPLANTI TUTANAĞI", logo/şirket adı) ve "Sayfa X / Y" altbilgisi bulunur.
- Boş bölümler gizlenir; transkript boşsa Ek A sayfası açılmaz.
- Uzun tablolar sayfalar arasında bölünür ve başlık satırı her sayfada yinelenir.
- Onaylanmamış LLM taslakları resmî tutanağa girmez.
- `--demo` ile üretilen tutanaklar "DEMO VERİSİ" olarak işaretlenir.

## Örnek üretme

```powershell
python main.py --demo --no-tts      # output/demo/ altında örnek tutanak
python main.py --minutes            # son toplantının tutanağını yeniden üretir
```
