# Video → SCORM (Moodle 5)

Web app kecil untuk mengubah video + subtitle + kuis menjadi paket SCORM 1.2 / 2004.
Tanpa database dan tanpa login: setiap pengunjung otomatis mendapat project sendiri
(`projects/<id>/`), dan semua data disimpan sebagai file JSON di folder itu.

## Jalankan lokal

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python app.py          # buka http://127.0.0.1:8000
```

Butuh `ffmpeg` dan `ffprobe` di PATH.

## Tipe soal (`questions.json`)

| type        | correct            | Keterangan                                   |
|-------------|--------------------|----------------------------------------------|
| `single`    | `0`                | Pilihan ganda, 1 jawaban benar               |
| `multiple`  | `[0, 1]`           | Jawaban ganda; nilai penuh hanya jika SEMUA jawaban benar dipilih dan tidak ada yang salah (semua-atau-nol) |
| `truefalse` | `0` = Benar, `1` = Salah | Opsi otomatis `["Benar", "Salah"]`      |

File lama tanpa `type` tetap terbaca sebagai pilihan ganda. Contoh lengkap: `questions_contoh_3tipe.json`.

## Hosting

**Wajib 1 proses uvicorn (jangan pakai `--workers` > 1).** Status generate dan antrean ffmpeg
dikoordinasi di dalam satu proses. Banyak pengguna bersamaan tetap aman karena tiap request
dilayani thread terpisah, tiap project punya folder sendiri, dan JSON ditulis secara atomik.

### Pengaturan (environment variable)

| Variabel              | Default     | Fungsi                                               |
|-----------------------|-------------|------------------------------------------------------|
| `HOST` / `PORT`       | 127.0.0.1 / 8000 | Alamat server                                   |
| `MAX_UPLOAD_MB`       | 2048        | Batas ukuran upload video                            |
| `MAX_CONCURRENT_JOBS` | 1           | Jumlah konversi ffmpeg bersamaan; sisanya antre      |
| `RETENTION_DAYS`      | 7           | Project tidak aktif lebih dari N hari dihapus otomatis (0 = tidak pernah) |

### systemd (`/etc/systemd/system/video-scorm.service`)

```ini
[Unit]
Description=Video to SCORM
After=network.target

[Service]
User=www-data
WorkingDirectory=/opt/video-scorm
Environment=HOST=127.0.0.1 PORT=8000 MAX_UPLOAD_MB=2048 MAX_CONCURRENT_JOBS=1 RETENTION_DAYS=7
ExecStart=/opt/video-scorm/.venv/bin/python app.py
Restart=always

[Install]
WantedBy=multi-user.target
```

### nginx

```nginx
server {
    server_name scorm.contoh.ac.id;

    client_max_body_size 2100m;        # sedikit di atas MAX_UPLOAD_MB
    proxy_request_buffering off;       # upload langsung diteruskan, tidak ditampung nginx dulu
    proxy_read_timeout 600s;
    proxy_send_timeout 600s;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
}
```

### Catatan

- Siapa pun yang tahu alamat web bisa memakainya. Kalau nanti perlu dibatasi, cara paling
  sederhana adalah `auth_basic` di nginx, tanpa mengubah kode.
- Simpan link halaman (`?p=<id>`) untuk melanjutkan project yang sama di lain waktu.
- Jika satu project dibuka di dua tab dan keduanya mengedit soal, simpanan terakhir yang berlaku.
- Siapkan ruang disk yang cukup: tiap project menyimpan video asli dan ZIP hasilnya sampai dihapus otomatis.
