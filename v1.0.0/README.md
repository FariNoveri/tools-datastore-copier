# Roblox DataStore Copier

Aplikasi lokal untuk menyalin entry DataStore antar-experience Roblox melalui Open Cloud API. Data dari game sumber hanya dibaca; aplikasi berjalan di `127.0.0.1`.

**Versi saat ini: 1.0.0**

## Menjalankan aplikasi

1. Pastikan Python 3 sudah terpasang.
2. Buka terminal di folder project.
3. Pasang dependensi:

   ```powershell
   python -m pip install -r requirements.txt
   ```

4. Jalankan:

   ```powershell
   python app.py
   ```

Browser terbuka otomatis di `http://127.0.0.1:5000`. Opsi seperti `--no-browser`, `--verbose`, dan `--port 8080` tersedia; jalankan `python app.py --help` untuk detail.

## Menjalankan pengujian

```powershell
python -m unittest discover -s tests
```

## Struktur project

```text
DataStore/
├── app.py
├── requirements.txt
├── README.md
├── .gitignore
├── docs/
│   └── CHANGELOG.md
└── tests/
    └── test_app.py
```

Versi aplikasi didefinisikan di `app.py` dan ditampilkan pada halaman aplikasi serta endpoint `/api/settings`. Riwayat versi disimpan di `docs/CHANGELOG.md`.

API key hanya digunakan di memori selama aplikasi berjalan. Jangan membagikan atau mengunggah API key.
