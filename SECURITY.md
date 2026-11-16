# Güvenlik ve Gizlilik

## Depoya eklenmemesi gerekenler

Aşağıdakiler `.gitignore` ile dışarıda tutulur; katkı gönderirken de eklemeyin:

- `.env` ve içindeki SMTP şifreleri, API anahtarları
- `data/participants.json`, `data/agenda.json` ve SQLite veritabanları (`data/*.db`)
- `output/` altındaki PDF, Excel, `.eml`, `.ics` ve ses dosyaları
- `logs/` içeriği
- Kurumsal logo ve marka varlıkları (`assets/logo/`)
- TTS/LLM model dosyaları

Örnek veri gerekiyorsa yalnızca kurgusal adlar ve `example.com` adresleri kullanın.

## Veri akışı

- Toplantı kayıtları yerel SQLite dosyasında tutulur.
- Google konuşma tanıma açıkken mikrofon sesi Google sunucularına gönderilir.
- Yerel LLM yalnızca `OLLAMA_HOST` adresine bağlanır; uzak sunucu `LLM_ALLOW_REMOTE=true` olmadan reddedilir.
- E-posta yalnızca yapılandırılan SMTP sunucusu üzerinden, komutla veya açıkça etkinleştirilen otomatik gönderimle gider.
  Uygulama SMTP kullanıcı adını, şifresini ve e-posta içeriğini günlüğe yazmaz.

## Güvenlik açığı bildirme

Bir güvenlik sorunu bulursanız lütfen ayrıntıları herkese açık bir issue'da paylaşmayın; depo sahibine GitHub
üzerinden özel olarak ulaşın (GitHub "Security → Report a vulnerability" özelliği etkinse onu kullanın).
