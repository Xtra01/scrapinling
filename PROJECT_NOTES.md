# LinkedIn Profile Scraper — Proje Notları & Devam Rehberi

> Son güncelleme: 2026-08-07
> Repo: `Xtra01/scrapinling` — branch: `claude/linkedin-profile-scraper-ejvUZ`
> Amaç: 15.000 LinkedIn linkinden **tüm iş deneyimi + eğitim bilgilerini** yasal API'lerle eksiksiz çekmek.

---

## 1. En Kritik Bilgi: Proxycurl Kapandı

**Proxycurl (nubela.co/proxycurl) 4 Temmuz 2025'te kalıcı olarak kapandı.**

- LinkedIn (Microsoft) Ocak 2025'te federal dava açtı — "yüz binlerce sahte hesapla scraping" iddiası
- Proxycurl tüm scraped veriyi sildi, kalıcı mahkeme yasağını kabul etti
- Kapanış anında ~$10M ARR'lık bir işti
- **Sakın Proxycurl üzerine bir şey inşa etme**

Bu yüzden proje **7 aktif API** ile inşa edildi, hepsi fallback zinciri halinde çalışıyor.

---

## 2. Aktif API'lerin Tam Karşılaştırması

| Öncelik | API | 15k Maliyet | Mod | Yasal Durum | Veri Zenginliği |
|---------|-----|-------------|-----|--------------|-------------------|
| **Opsiyonel Phase 1** | **Bright Data** | **~$750** | Batch (trigger→poll→download) | **En güçlü** (2024'te Meta + X Corp davalarını kazandı) | **En zengin** (+certifications, +recommendations tam metin, +patents, +publications, +projects, +honors, +volunteer) |
| PRIMARY | **Scrapingdog** | ~$135 | Real-time REST | Orta | İyi (temel exp+edu) |
| FALLBACK 1 | **Netrows** | ~€75 | Real-time REST | Orta | İyi, 48+ endpoint |
| FALLBACK 2 | **LinkdAPI** | Kredi bazlı | Real-time REST | İyi (sıfır fake hesap, LinkedIn mobile API kullanır) | İyi, Proxycurl'ün resmi yedeği |
| FALLBACK 3 | **PDL (People Data Labs)** | ~$600 | Real-time | İyi | En derin kariyer geçmişi |
| FALLBACK 4 | **ScrapIn** | $1k+/ay | Real-time | İyi | Real-time, GDPR uyumlu |
| FALLBACK 5 | **RocketReach** | $53+/ay | REST | Orta | B2B, iletişim bilgisi de var |

### Maliyet/Yasal Güvence Trade-off'u

- **Bütçe önceliklyse**: Scrapingdog ($135) + Netrows (€75) = ~$200 toplamda, %90+ başarı oranı beklenir
- **Yasal güvenlik önceliklyse**: Bright Data tek başına yeterli, ama pahalı (~$750) ve ~%20 başarısızlık oranı var (retry gerekir)
- **En iyi kombinasyon (önerilen)**: Bright Data Phase 1 (bulk, en zengin veri) + Scrapingdog Phase 2 (başarısız ~3k'yı ucuza retry) → en yüksek başarı oranı + en zengin veri

---

## 3. Bright Data — Detaylı Teknik Bilgiler (Doğrulanmış)

### API Yapısı
```
Dataset ID (LinkedIn People Profiles): gd_l1viktl72bvl7bjuj0
Dataset ID (Companies):                gd_l1vikfnt1wgvvqz95w
Dataset ID (Jobs):                     gd_lpfll7v5hcqtkxl6l
Dataset ID (Posts):                    gd_lyy3tktm25m4avu764

Sync (real-time, ≤20 URL, 60s timeout):
  POST https://api.brightdata.com/datasets/v3/scrape?dataset_id=...&format=json

Async (batch, büyük hacimler, 1GB'a kadar input):
  POST https://api.brightdata.com/datasets/v3/trigger?dataset_id=...&format=json
  → { "snapshot_id": "s_xxx" }

  GET https://api.brightdata.com/datasets/v3/progress/{snapshot_id}
  → status: collecting → digesting → ready (veya failed/error/terminated)

  GET https://api.brightdata.com/datasets/v3/snapshot/{snapshot_id}?format=json
  → 202 (henüz hazır değil) veya 200 (data hazır)
```

### Kritik Sınırlar (Unutma!)
- **~%80 başarı oranı** — 15k'dan ~3.000 profil başarısız/boş dönebilir, retry mekanizması ŞART
- **E-posta/telefon YOK** — sadece public LinkedIn verisi
- **Private profiller** kısmi/boş veri döner
- **Avatar/banner URL'leri 24 saatte expire olur**
- **Snapshot'lar 30 gün sonra silinir**
- **Büyük batch'ler saatler sürebilir**, resmi SLA yok
- Resmi Python SDK yok, sadece REST

### Test Edilmesi Gerekenler
- Kod tam yazıldı ama **gerçek API key ile hiç test edilmedi**
- Scrapingdog, Netrows, LinkdAPI, ScrapIn, RocketReach response parse mantığı dokümantasyon+üçüncü parti kaynaklardan tahmine dayalı, gerçek alan adları farklı çıkabilir

### Yasal Emsal
1. **hiQ v. LinkedIn (2017-2022)**: 9. Daire, public veri scraping'in CFAA ihlali olmadığına hükmetti
2. **Meta v. Bright Data (Ocak 2024)**: Bright Data lehine summary judgment
3. **X Corp v. Bright Data (Mayıs 2024)**: Dava reddedildi

---

## 4. Proje Mimarisi

```
scrapinling/
├── main.py                          CLI: scrape / status / export
├── scraper/
│   ├── engine.py                    Dual pipeline orkestratör
│   ├── models.py                    LinkedInProfile/WorkExperience/Education
│   ├── brightdata_client.py         Batch trigger/poll/download
│   ├── scrapingdog_client.py        PRIMARY
│   ├── netrows_client.py            FALLBACK #1
│   ├── linkdapi_client.py           FALLBACK #2
│   ├── pdl_client.py                FALLBACK #3
│   ├── scrapin_client.py            FALLBACK #4
│   └── rocketreach_client.py        FALLBACK #5
├── utils/
│   ├── storage.py                   SQLite checkpoint, CSV/JSON writers
│   ├── validators.py                URL yükleme+normalize+validate
│   └── progress.py                  Rich progress bar
├── .env.example
├── requirements.txt
├── LICENSE                          MIT — Copyright (c) 2026 Xtra01
└── README.md
```

### Veri Akışı
```
CSV/TXT input → validators.py (normalize+dedupe)
             → storage.py Database.init_job_queue() (SQLite'a pending yaz)
             → engine.py ScraperEngine.run()
                 Phase 1 (varsa): Bright Data bulk_fetch()
                 Phase 2: Standart clientlar sırasıyla dener
             → db.save_profile() + csv_writer.write() + jsonl_writer.write()
             → SONUÇ: data/output/profiles.csv + profiles.jsonl + data/linkedin_profiles.db
```

### Resume/Checkpoint
- SQLite `job_status` tablosu: pending/in_progress/success/failed/not_found
- `python main.py scrape --resume` (varsayılan açık) → sadece bekleyenleri işler

---

## 5. Bilinmeyen Riskler / Yapılmadı

1. **Hiçbir API gerçek key ile test edilmedi** — syntax doğru (`py_compile` geçti) ama gerçek response formatı doğrulanmadı
2. **LinkedIn URL formatı** — sadece `linkedin.com/in/...`, şirket sayfaları desteklenmiyor
3. **15k URL'lik gerçek test dosyası henüz verilmedi**
4. **Yerel git push bu ortamda çalışmıyordu** (403) — teslimat GitHub MCP `push_files`/`create_branch` araclarıyla yapıldı

---

## 6. Sıradaki Adımlar

1. Kullanıcıdan gerçek API key'lerini iste (Scrapingdog/Netrows en ucuz başlangıç)
2. Küçük test seti (10-20 URL) ile gerçek response'ları doğrula, parser düzelt
3. 15.000 linkin bulunduğu CSV/TXT dosyasını al
4. `python main.py scrape --input <dosya>` çalıştır
5. Sonuçları `data/output/profiles.csv` + `profiles.jsonl` olarak teslim et

---

## 7. Genel Püf Noktaları

- CSV input sütun adı: `linkedin_url`, `linkedin`, `url`, `profile_url`, `link` — otomatik algılanıyor
- URL normalize otomatik: `http://`→`https://`, `linkedin.com`→`www.linkedin.com`, dedupe
- CSV output: her satırda 1 experience VEYA 1 education kaydı (`record_type` ile ayırt edilir)
- JSON Lines output: her satırda TAM profil (nested array'lerle)
- `.env` asla commit edilmez (`.gitignore`'da)
- Proje MIT lisanslı, "Copyright (c) 2026 Xtra01"
