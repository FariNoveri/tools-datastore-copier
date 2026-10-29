# Tools DataStore Copier

Aplikasi desktop lokal sederhana untuk menyalin entry DataStore dari satu Roblox experience ke experience lain melalui Roblox Open Cloud API.

> **Versi saat ini: 1.0.0**

## Fitur

- Menyalin entry dari DataStore sumber ke DataStore tujuan.
- Mode simulasi untuk memeriksa data tanpa menulis ke experience tujuan.
- Opsi melewati key yang sudah ada di tujuan.
- Filter berdasarkan scope dan awalan key.
- Progres dan log proses langsung di halaman aplikasi.
- Aplikasi berjalan lokal di `127.0.0.1`.

Data sumber hanya dibaca. Untuk keamanan, gunakan API key dengan izin minimum yang diperlukan dan hapus key setelah selesai. API key hanya digunakan di memori selama aplikasi berjalan; jangan membagikannya atau memasukkannya ke repository.

## Menjalankan aplikasi

Pastikan Python dan Git sudah terpasang, lalu jalankan di PowerShell:

```powershell
git clone https://github.com/FariNoveri/tools-datastore-copier.git
Set-Location .\tools-datastore-copier\v1.0.0
python -m pip install -r requirements.txt
python app.py
```

Browser akan terbuka di `http://127.0.0.1:5000`. Jalankan `python app.py --help` untuk melihat pilihan seperti `--no-browser`, `--verbose`, dan `--port`.

Sebelum menyalin data sungguhan, baca tutorial di aplikasi. Pastikan kamu hanya mengakses experience milikmu sendiri atau yang kamu punya izin resminya.

## Pengujian

Dari folder `v1.0.0`, jalankan:

```powershell
python -m unittest discover -s tests
```

## Struktur repository

```text
tools-datastore-copier/
├── LICENSE
├── README.md
└── v1.0.0/
    ├── app.py
    ├── requirements.txt
    ├── README.md
    ├── docs/
    │   └── CHANGELOG.md
    └── tests/
        └── test_app.py
```

## Kredit

Dibuat oleh **Fari Noveri**.

Dibuat dengan semangat dan cinta untuk pacarku, **Illyasviel**.

Project ini menggunakan Roblox Open Cloud API dan tidak berafiliasi dengan, didukung, atau dibuat oleh Roblox Corporation.

## Lisensi

Project ini tersedia di bawah [MIT License](./LICENSE).
