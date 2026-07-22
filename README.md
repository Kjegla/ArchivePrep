# 📷 Kjegla's Photo Organizer

Sorts a messy folder of photos and videos into tidy folders by **camera model**
(e.g. `Sony A6000`, `iPhone 16 Pro`, `Samsung Galaxy S24 Ultra`), with optional
date subfolders inside each camera folder.

```
My Photos\
├── Sony A6000\2024\02-February\DSC01234.JPG
├── iPhone 16 Pro\2025\01-January\IMG_5555.HEIC
└── Samsung Galaxy S24 Ultra\2024\06-June\20240610_101512.jpg
```

## ✨ Features

- **Move or Copy** — copy mode never touches your originals
- **Preview (dry run)** — see exactly what would happen before doing it
- **Undo Last Run** — one click puts everything back
- Date subfolders: none, year, month, or year + month
- Reads real photo dates from EXIF, and real video dates from MP4/MOV metadata
- iPhone **HEIC** photos supported (sorted by exact model)
- RAW files (ARW, CR2, DNG, NEF, …) matched to their JPEG and sorted together
- Optional separation of RAW files and Android screenshots into subfolders
- Skips identical duplicates instead of piling up copies
- Optional recursive scan of subfolders — safe to re-run any time
- Fast: multithreaded scanning, live progress with ETA

## 🚀 Easiest way (Windows, no install)

1. Go to the [**Releases**](../../releases) page
2. Download `PhotoOrganizerV33.exe`
3. Double-click it — that's the whole install

> Windows SmartScreen may warn about an unknown publisher the first time.
> Click **More info → Run anyway**.

## 🐍 Running from source (Windows/Mac/Linux)

Requires [Python 3.10+](https://www.python.org/downloads/).

```bash
git clone https://github.com/USERNAME/kjeglas-photo-organizer.git
cd kjeglas-photo-organizer
pip install -r requirements.txt
python photo_organizer.py
```

## 🛟 How to use it safely

1. Pick your photo folder with **Browse...**
2. Choose **Copy** mode the first time (originals stay untouched)
3. Click **🔍 Preview (Dry Run)** and read what it plans to do
4. Happy? Click **▶️ Execute Operation**
5. Changed your mind? **↩️ Undo Last Run**

A log file (`kjegla_media_log_*.txt`) is written into the source folder for
every run, so you can always see what happened.

## Building the .exe yourself

```bash
pip install -r requirements.txt pyinstaller
python -m PyInstaller --onefile --windowed --noconfirm --collect-all sv_ttk --name PhotoOrganizerV33 photo_organizer.py
```
