# LinkedIn Profile Scraper

15.000+ LinkedIn profilinden iş deneyimi ve eğitim bilgilerini eksiksiz çeken, üretim ortamına hazır araç.

> **Not:** Proxycurl, LinkedIn'in açtığı dava sonucu **4 Temmuz 2025'te kalıcı olarak kapandı.**
> Bu araç yalnızca aktif ve çalışır API'leri kullanır.

## Özellikler

- **Tam veri**: Tüm iş deneyimleri (eski + güncel, tarihler dahil), tüm eğitim bilgileri
- **7 Aktif API**: Bright Data (en yasal + en zengin veri) + 6 ucuz alternatif, otomatik fallback
- **Kaldığı yerden devam**: SQLite checkpoint ile yarıda kesilen işi devam ettir
- **Async & hızlı**: aiohttp ile eş zamanlı istek
- **Üçlü çıktı**: CSV (Excel) + JSON Lines + SQLite
- **Rate limiting**: Otomatik bekleme ve retry (exponential backoff)
- **Zengin terminal**: Rich kütüphanesi ile canlı progress bar

## Kurulum

```bash
python -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
# .env dosyasını aç, en az bir API anahtarı gir
```

## Aktif API'ler (Proxycurl Kapalı)

| Seçenek | API | Fiyat (15k profil) | Mod | Yasal Güvence |
|---------|-----|---------------------|-----|----------------|
| **En Zengin/Yasal** | **Bright Data** — [brightdata.com](https://brightdata.com) | ~**$750** | Batch (trigger→poll→download) | **En yüksek** — 2024'te Meta + X Corp davalarını kazandı |
| PRIMARY | **Scrapingdog** — [scrapingdog.com](https://www.scrapingdog.com) | ~**$135** | Real-time | Orta |
| FALLBACK 1 | Netrows — [netrows.com](https://www.netrows.com) | ~**€75** | Real-time | Orta |
| FALLBACK 2 | LinkdAPI — [linkdapi.com](https://linkdapi.com) | Kredi bazlı | Real-time | İyi (Proxycurl'ün resmi yedeği) |
| FALLBACK 3 | People Data Labs — [peopledatalabs.com](https://www.peopledatalabs.com) | ~$600 | Real-time | İyi |
| FALLBACK 4 | ScrapIn — [scrapin.io](https://scrapin.io) | $1k+/mo | Real-time | İyi |
| FALLBACK 5 | RocketReach — [rocketreach.co](https://rocketreach.co/api) | $53+/mo | Real-time | Orta |

### Hangi Kombinasyonu Seçmeli?

- **Bütçe öncelikli**: `SCRAPINGDOG_API_KEY` + `NETROWS_API_KEY` → toplam ~$200, iyi başarı oranı
- **Yasal güvence + en zengin veri öncelikli**: `BRIGHTDATA_API_TOKEN` tek başına yeterli (sertifikalar, tavsiyeler, patentler dahil), ama ~%20 başarısızlık oranına karşı retry gerekir
- **En güçlü kombinasyon**: Bright Data (Phase 1, bulk) + Scrapingdog (Phase 2, başarısızları ucuza retry) → en yüksek başarı oranı + en zengin veri

### Bright Data'nın Sınırları (Önemli)

- E-posta/telefon **yok** — sadece public LinkedIn verisi
- Private profiller kısmi/boş veri döner
- ~%80 başarı oranı — ~3.000 profil retry gerekebilir
- Gerçek zamanlı değil — batch async (trigger → poll → download), dakikalar-saatler sürebilir
- Avatar/banner URL'leri 24 saatte expire olur

## Kullanım

```bash
# Temel (CSV dosyasından)
python main.py scrape --input links.csv

# Daha hızlı
python main.py scrape --input links.csv --concurrency 10

# Kaldığı yerden devam (varsayılan açık)
python main.py scrape --input links.csv --resume

# Baştan başla
python main.py scrape --input links.csv --no-resume

# İlerlemeyi gör
python main.py status

# Dışa aktar
python main.py export            # CSV + JSON
python main.py export --format csv
```

## Input Formatı

### CSV
```csv
linkedin_url
https://www.linkedin.com/in/johndoe
https://www.linkedin.com/in/janedoe
```
Kabul edilen sütun adları: `linkedin_url`, `linkedin`, `url`, `profile_url`, `link`

### TXT (her satır bir URL)
```
https://www.linkedin.com/in/johndoe
https://www.linkedin.com/in/janedoe
```

## Output

### `data/output/profiles.csv` — Excel uyumlu
Her satır bir iş deneyimi VEYA eğitim kaydı, `record_type` sütunundan ayırt edilir.

### `data/output/profiles.jsonl` — JSON Lines
Her satır tam profil (tüm alanlar, nested experiences[]/education[] dahil).

### `data/linkedin_profiles.db` — SQLite
4 tablo: `profiles`, `experiences`, `education`, `job_status`

## Proje Yapısı

```
├── main.py                      CLI (scrape / status / export)
├── scraper/
│   ├── engine.py                Dual-pipeline orkestratör (Bright Data + fallback zinciri)
│   ├── models.py                Veri modelleri
│   ├── brightdata_client.py     Batch trigger/poll/download
│   ├── scrapingdog_client.py    PRIMARY
│   ├── netrows_client.py        FALLBACK #1
│   ├── linkdapi_client.py       FALLBACK #2
│   ├── pdl_client.py            FALLBACK #3
│   ├── scrapin_client.py        FALLBACK #4
│   └── rocketreach_client.py    FALLBACK #5
├── utils/
│   ├── storage.py               SQLite + CSV + JSON yazıcıları
│   ├── validators.py            URL doğrulama + input yükleme
│   └── progress.py              Rich progress bar
├── PROJECT_NOTES.md              Detaylı proje geçmişi ve devam rehberi
├── .env.example                 API anahtar şablonu
└── requirements.txt
```

Ayrıntılı araştırma notları, mimari kararlar, bilinen riskler ve devam rehberi için **`PROJECT_NOTES.md`** dosyasına bakın.

## Lisans

MIT License — Copyright (c) 2026 Xtra01
