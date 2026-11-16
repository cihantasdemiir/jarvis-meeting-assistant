# Fontlar (isteğe bağlı)

PDF tutanak Türkçe karakterler (ç ğ ı İ ö ş ü) için Unicode TTF font kullanır.
Bu klasör boşsa Windows'taki **Arial** (yoksa Calibri, Segoe UI, Tahoma) otomatik kullanılır.

Tüm bilgisayarlarda birebir aynı görünüm istenirse DejaVu fontlarını buraya koyun:

- `DejaVuSans.ttf`
- `DejaVuSans-Bold.ttf`

İndirme: https://dejavu-fonts.github.io (serbest lisans; lisans metnini `LICENSE-DejaVu.txt` olarak ekleyin).
Başka bir font kullanmak için `.env` içinde `PDF_FONT_REGULAR` ve `PDF_FONT_BOLD` yollarını belirtin.
