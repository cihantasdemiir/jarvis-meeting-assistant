# Final Video — İzole Canlı Demo (`python main.py --demo-live`)

## Ne yapar?

`--demo-live` normal canlı çalışmayla aynı bileşenleri kullanır:
- mikrofon, Türkçe STT, Pocket TTS ve LLM ayarları;
- komut ve diyalog yönetimi, %80 süre uyarısı;
- Kurumsal PDF tasarımı ve `LOGO_PATH`;
- `data/agenda.json` ve `data/participants.json`.

Farkı yalnızca nereye yazdığı ve e-postanın gönderilmemesidir.

| | Normal (`python main.py`) | Canlı demo (`--demo-live`) |
|---|---|---|
| Veritabanı | `DB_PATH` (ör. `data/jarvis.db`) | `data/demo_live/<YYYYMMDD-HHMMSS>/jarvis.db`, her çalıştırmada **yeni ve boş** |
| Çıktılar (PDF, .eml, .ics, Excel) | `output/` | `output/demo_live/<aynı zaman>/` |
| Yarım toplantı kurtarma | Var | Yok: oturum veritabanı boş olduğundan hiçbir toplantı kurtarılmaz |
| Devreden görev / önceki toplantı özeti | Normal geçmişten | Yalnızca o demo oturumundan (başlangıçta 0) |
| E-posta | `.env` ayarlarına göre | **Gönderilmez.** Otomatik gönderim kapalı; "tutanağı gönder" yalnızca `.eml` taslağı yazar ve bunu açıkça söyler |

**İzolasyon nasıl sağlanıyor:**
- Mevcut depolama altyapısı (`MeetingRepository`, `MinutesService`) değiştirilmedi. Demo yalnızca farklı bir veritabanı yolu ve çıktı klasörüyle çalıştırılıyor.
- Normal veritabanı açılmaz, okunmaz ve yazılmaz.
- Her çekim ayrı bir zaman damgalı klasörde başlar. Böylece önceki demo çekiminden kalan yarım toplantı veya açık görev de devralınmaz.
- `.env` ve SMTP ayarları değiştirilmez ve terminale yazdırılmaz.
- `data/demo_live/` `.gitignore` içindedir.

**Tutanak:**
- Demo tutanağında "DEMO VERİSİ" etiketi yoktur. Toplantı örnek veri değil, gerçek mikrofonla yürütülen canlı bir oturumdur; ayrımı ayrı klasör sağlar.
- Uygulama günlükleri (`logs/`) normal günlük dosyasına yazılmaya devam eder. Bunlar toplantı kaydı değildir.

## Çekim adımları (Windows)

```powershell
cd jarvis-meeting-assistant
.\.venv\Scripts\Activate.ps1
python main.py --list-mics            # gerekirse mikrofon numarasını bulun
python main.py --demo-live            # veya: python main.py --demo-live --mic 2
```

1. Açılışta "=== CANLI DEMO ===" satırını ve demo klasörlerini görün.
2. "Jarvis, toplantıyı başlat" deyin. Gündemde 4 madde ve 20 dakika olduğu söylenir; devreden görev söylenmez.
3. Yoklama, söz, karar, görev, oylama, otopark ve transkript normal çalışır.
   - İki Mehmet olduğunda seçim sorusu gelir; "birinci" veya "Öztürk" deyip "evet" ile onaylayın.
4. İlk madde 5 dakika olduğu için yaklaşık 4. dakikada %80 uyarısı bir kez verilir.
5. "Jarvis, toplantıyı bitir" deyin: PDF `output\demo_live\<zaman>\` altına yazılır.
6. "Jarvis, tutanağı gönder" denirse e-posta gönderilmez; `.eml` taslağı aynı klasöre kaydedilir.
7. Çıkış: "Jarvis, çık" veya Ctrl+C. Ctrl+C ile çıkılırsa ara tutanak yine demo klasörüne yazılır.

**Yeniden çekim:** Komutu tekrar çalıştırmanız yeterli; yeni ve temiz bir oturum açılır. Eski demo klasörleri silinmez, isterseniz elle silebilirsiniz.

**Prova:** Mikrofonsuz prova için `python main.py --demo-live --text --no-tts` kullanabilirsiniz.
