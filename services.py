"""Backend untuk app Streamlit "Studio Render": library, impor, antrean job, normalisasi, dan helper media.

Mesin render (gabung banyak video jadi 1 video) ada di renderer.py.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import queue
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

BASE = Path("data").resolve()
DIRS = {
    "video": BASE / "videos",
    "audio": BASE / "audios",
    "output": BASE / "output",
    "thumb": BASE / "thumbnails",
    "other": BASE / "other",
}
TMP = BASE / "tmp"
LOG_FILE = BASE / "ffmpeg.log"
for _d in (*DIRS.values(), TMP):
    _d.mkdir(parents=True, exist_ok=True)

# Agar yt-dlp menemukan `deno` (dipasang lewat pip) di virtualenv Streamlit Cloud.
for _p in (str(Path(sys.executable).parent), str(Path.home() / ".deno" / "bin")):
    if _p not in os.environ.get("PATH", ""):
        os.environ["PATH"] = os.environ.get("PATH", "") + os.pathsep + _p

for _st in (sys.stdout, sys.stderr):
    try:
        _st.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".m4v", ".wmv", ".ts", ".3gp"}
AUDIO_EXTS = {".mp3", ".aac", ".m4a", ".opus", ".ogg", ".flac", ".wav", ".wma", ".aiff"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
PARTIAL_EXTS = {".part", ".ytdl", ".tmp", ".temp"}


def tool_status() -> dict:
    """Status komponen eksternal untuk panel diagnosa di sidebar."""
    try:
        import yt_dlp.version as v
        ytdlp = v.__version__
    except Exception:  # noqa: BLE001
        ytdlp = None
    return {"ffmpeg": shutil.which("ffmpeg") is not None,
            "deno": shutil.which("deno") is not None,
            "yt-dlp": ytdlp}


# ───────────────────────── library ─────────────────────────
def safe_name(name: str) -> str:
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    stem = re.sub(r"[^\w\-]+", "_", stem.encode("ascii", "ignore").decode()).strip("_") or "file"
    return f"{stem[:80]}.{ext.lower()}" if ext else stem[:80]


def unique_path(directory: Path, name: str) -> Path:
    target = directory / name
    stem, ext, n = target.stem, target.suffix, 1
    while target.exists():
        target = directory / f"{stem}_{n}{ext}"
        n += 1
    return target


def list_paths(kind: str) -> list[Path]:
    return sorted((p for p in DIRS[kind].iterdir() if p.is_file() and not p.name.startswith(".")),
                  key=lambda p: p.name.lower())


def list_files(kind: str) -> list[dict]:
    rows = []
    for p in list_paths(kind):
        st = p.stat()
        rows.append({"name": p.name, "mb": st.st_size / 1024 / 1024,
                     "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%d/%m %H:%M")})
    return rows


def library_videos() -> list[Path]:
    """Semua file video yang bisa dipakai (folder videos + output)."""
    return [p for k in ("video", "output") for p in list_paths(k) if p.suffix.lower() in VIDEO_EXTS]


def label(p: Path) -> str:
    return f"{p.parent.name}/{p.name}"


def delete_files(paths, protected=()) -> int:
    protected = {Path(p).resolve() for p in protected if p}
    n = 0
    for p in paths:
        p = Path(p).resolve()
        if BASE not in p.parents or p in protected:
            continue
        try:
            p.unlink()
            n += 1
        except OSError:
            pass
    return n


def move_files(paths, dest_kind: str, protected=()) -> int:
    protected = {Path(p).resolve() for p in protected if p}
    n = 0
    for p in paths:
        p = Path(p).resolve()
        if BASE not in p.parents or p in protected:
            continue
        try:
            shutil.move(str(p), str(unique_path(DIRS[dest_kind], p.name)))
            n += 1
        except OSError:
            pass
    return n


def clear_tmp() -> None:
    for p in TMP.iterdir():
        try:
            shutil.rmtree(p) if p.is_dir() else p.unlink()
        except OSError:
            pass


def disk_info() -> dict:
    usage = shutil.disk_usage(BASE)
    used = sum(f.stat().st_size for f in BASE.rglob("*") if f.is_file())
    return {"free_gb": usage.free / 1024**3, "data_gb": used / 1024**3}


# ───────────────────────── klasifikasi file otomatis ─────────────────────────
def _probe_streams(path: Path) -> tuple[bool, bool]:
    """(punya video asli, punya audio). Cover-art MP3 tidak dihitung sebagai video."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "stream=codec_type:stream_disposition=attached_pic", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=30)
        streams = json.loads(r.stdout or "{}").get("streams", [])
    except Exception:  # noqa: BLE001
        return False, False
    has_v = any(s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")
                for s in streams)
    has_a = any(s.get("codec_type") == "audio" for s in streams)
    return has_v, has_a


def detect_kind(path: Path, hint: str = "auto") -> str:
    ext = path.suffix.lower()
    if ext in IMAGE_EXTS:          # gambar (thumbnail) selalu ke folder thumbnail
        return "thumb"
    if hint in ("video", "audio"):
        return hint
    if ext in AUDIO_EXTS:
        return "audio"
    has_v, has_a = _probe_streams(path)
    if has_v:
        return "video"
    if has_a:
        return "audio"
    return "video" if ext in VIDEO_EXTS else "other"


def place_file(path: Path, hint: str = "auto") -> tuple[str, Path]:
    kind = detect_kind(path, hint)
    dest = unique_path(DIRS[kind], safe_name(path.name))
    shutil.move(str(path), str(dest))
    return kind, dest


def import_bytes(name: str, data, hint: str = "auto") -> tuple[str, Path]:
    stage = Path(tempfile.mkdtemp(prefix="up_", dir=TMP))
    try:
        tmp = stage / safe_name(name)
        tmp.write_bytes(data)
        return place_file(tmp, hint)
    finally:
        shutil.rmtree(stage, ignore_errors=True)


# ───────────────────────── job queue ─────────────────────────
class JobRunner:
    """Antrean background dengan N worker."""

    def __init__(self, workers: int = 1):
        self.q: queue.Queue = queue.Queue()
        self.jobs: list[dict] = []
        self.lock = threading.Lock()
        for _ in range(max(1, workers)):
            threading.Thread(target=self._worker, daemon=True).start()

    def submit(self, title: str, fn, cleanup=None) -> dict:
        job = {"id": uuid.uuid4().hex[:6], "title": title, "status": "antre", "detail": "",
               "created": time.time(), "started": None, "ended": None, "fn": fn,
               "cleanup": cleanup}
        with self.lock:
            self.jobs.append(job)
            self.jobs = self.jobs[-200:]
        self.q.put(job)
        return job

    def _worker(self):
        while True:
            job = self.q.get()
            with self.lock:
                if job["status"] != "antre":
                    continue
                job["status"], job["started"] = "berjalan", time.time()
            try:
                ok, info = job["fn"](job)
            except Exception as e:  # noqa: BLE001
                ok, info = False, f"{type(e).__name__}: {e}"
            finally:
                if job.get("cleanup"):
                    job["cleanup"]()
            job["detail"] = str(info)[:600]
            job["ended"] = time.time()
            job["progress"] = 1.0 if ok else job.get("progress", 0.0)
            job["status"] = "selesai" if ok else "gagal"

    def snapshot(self) -> list[dict]:
        with self.lock:
            rows = []
            for j in reversed(self.jobs):
                end = j["ended"] or time.time()
                dur = f"{int(end - j['started'])}s" if j["started"] else "-"
                rows.append({"ID": j["id"], "Job": j["title"], "Status": j["status"],
                             "Durasi": dur, "Info": j["detail"]})
            return rows

    def counts(self) -> dict:
        with self.lock:
            c = {"antre": 0, "berjalan": 0, "selesai": 0, "gagal": 0}
            for j in self.jobs:
                if j["status"] in c:
                    c[j["status"]] += 1
            return c

    def cancel_queued(self) -> int:
        n = 0
        with self.lock:
            for j in self.jobs:
                if j["status"] == "antre":
                    j["status"] = "dibatalkan"
                    if j.get("cleanup"):
                        j["cleanup"]()
                    n += 1
        return n

    def retry_failed(self) -> int:
        with self.lock:
            failed = [j for j in self.jobs if j["status"] == "gagal"]
            for j in failed:
                j["status"] = "diulang"
        for j in failed:
            self.submit(j["title"], j["fn"])
        return len(failed)

    def clear_finished(self):
        with self.lock:
            self.jobs = [j for j in self.jobs if j["status"] in ("antre", "berjalan")]

    def pending(self) -> int:
        c = self.counts()
        return c["antre"] + c["berjalan"]

    def stop_running(self) -> int:
        """Minta job yang sedang berjalan berhenti (dihormati oleh renderer.run_render)."""
        n = 0
        with self.lock:
            for j in self.jobs:
                if j["status"] == "berjalan":
                    j["stop"] = True
                    n += 1
        return n

    def active(self) -> list[dict]:
        """Job antre/berjalan beserta progress 0..1 (untuk progress bar)."""
        with self.lock:
            return [{"id": j["id"], "title": j["title"], "status": j["status"], "detail": j["detail"],
                     "progress": float(j.get("progress") or 0.0)}
                    for j in self.jobs if j["status"] in ("antre", "berjalan")]


# ───────────────────────── impor dari link ─────────────────────────
URL_RE = re.compile(r"https?://[^\s,;<>\"'\)\]]+", re.I)
SOURCE_LABEL = {"gdrive_file": "Google Drive · file", "gdrive_folder": "Google Drive · folder",
                "web": "Web / yt-dlp"}

# ───────────────────────── cookies yt-dlp ─────────────────────────
def parse_cookies(text: str) -> tuple[str, int, list[str]]:
    """Terima cookies JSON (Cookie-Editor / EditThisCookie) atau Netscape .txt.

    Return (teks_netscape, jumlah_cookie, daftar_domain). Raise ValueError jika tidak valid.
    """
    text = (text or "").strip()
    if not text:
        raise ValueError("kosong")

    # Format Netscape (cookies.txt) — dipakai apa adanya setelah dicek singkat.
    if not text.startswith(("[", "{")):
        rows, domains = [], []
        for line in text.splitlines():
            raw = line.strip()
            if not raw or (raw.startswith("#") and not raw.startswith("#HttpOnly_")):
                continue
            parts = raw.split("\t")
            if len(parts) != 7:
                raise ValueError("format bukan JSON dan bukan Netscape (harus 7 kolom dipisah TAB)")
            rows.append(raw)
            domains.append(parts[0].replace("#HttpOnly_", "").lstrip("."))
        if not rows:
            raise ValueError("tidak ada cookie")
        out = "# Netscape HTTP Cookie File\n" + "\n".join(rows) + "\n"
        return out, len(rows), sorted(set(domains))

    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"JSON rusak ({e.msg}, baris {e.lineno}). Pastikan menempel seluruh isi dari '[' sampai ']'")
    if isinstance(data, dict):
        data = data.get("cookies", [data])
    if not isinstance(data, list) or not data:
        raise ValueError("JSON harus berupa daftar cookie")

    lines, domains = [], []
    for c in data:
        if not isinstance(c, dict) or not c.get("name") or "value" not in c or not c.get("domain"):
            raise ValueError("tiap cookie harus punya name, value, dan domain")
        domain = str(c["domain"])
        host_only = bool(c.get("hostOnly", not domain.startswith(".")))
        flag = "FALSE" if host_only else "TRUE"
        if not host_only and not domain.startswith("."):
            domain = "." + domain
        if c.get("httpOnly"):
            domain_out = "#HttpOnly_" + domain
        else:
            domain_out = domain
        exp = c.get("expirationDate", c.get("expires", 0)) or 0
        try:
            exp = int(float(exp))
        except (TypeError, ValueError):
            exp = 0
        value = str(c["value"]).replace("\t", " ").replace("\n", "").replace("\r", "")
        lines.append("\t".join([domain_out, flag, str(c.get("path") or "/"),
                                "TRUE" if c.get("secure") else "FALSE", str(max(exp, 0)),
                                str(c["name"]), value]))
        domains.append(domain.lstrip("."))
    out = "# Netscape HTTP Cookie File\n" + "\n".join(lines) + "\n"
    return out, len(lines), sorted(set(domains))


class CookieBox:
    """Simpan cookies di memori; tulis ke file sementara hanya saat dipakai.

    Dipakai bersama oleh beberapa job (satu batch). Setelah semua job melepas
    (release), isi cookies dihapus dari memori.
    """

    def __init__(self, netscape: str):
        self._text = netscape
        self._refs = 0
        self._lock = threading.Lock()

    def make_releaser(self):
        with self._lock:
            self._refs += 1
        done = {"v": False}

        def release():
            with self._lock:
                if done["v"]:
                    return
                done["v"] = True
                self._refs -= 1
                if self._refs <= 0:
                    self._text = None
        return release

    def write_to(self, directory: Path) -> Path | None:
        with self._lock:
            text = self._text
        if not text:
            return None
        path = directory / f"ck_{uuid.uuid4().hex[:8]}.txt"
        path.write_text(text, encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return path


def parse_urls(text: str) -> list[str]:
    """Ambil semua URL dari teks bebas (dipisah koma, spasi, atau baris baru), tanpa duplikat."""
    seen, out = set(), []
    for u in URL_RE.findall(text or ""):
        u = u.rstrip(".,;")
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def classify_url(url: str) -> str:
    if "drive.google.com" in url or "drive.usercontent.google.com" in url:
        return "gdrive_folder" if "/folders/" in url else "gdrive_file"
    return "web"


def short_title(url: str) -> str:
    kind = classify_url(url)
    if kind == "gdrive_file":
        m = re.search(r"/d/([\w-]+)|[?&]id=([\w-]+)", url)
        fid = (m.group(1) or m.group(2)) if m else url[-8:]
        return f"Drive file {fid[:10]}…"
    if kind == "gdrive_folder":
        m = re.search(r"/folders/([\w-]+)", url)
        return f"Drive folder {(m.group(1) if m else url[-8:])[:10]}…"
    return re.sub(r"^https?://(www\.)?", "", url)[:48]


# ───────────────────────── Google Drive (tanpa batas ukuran dari app) ─────────────────────────
GD_BASE = "https://drive.usercontent.google.com"
GD_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
         "Chrome/124.0 Safari/537.36")


class DriveError(Exception):
    """Error pasti (tidak berguna dicoba ulang): private, kuota, tidak ditemukan, disk penuh."""


def _gd_file_id(url: str) -> str | None:
    for pat in (r"/file/d/([\w-]{10,})", r"/d/([\w-]{10,})", r"[?&]id=([\w-]{10,})"):
        m = re.search(pat, url)
        if m:
            return m.group(1)
    return None


def _cd_filename(headers) -> str | None:
    from urllib.parse import unquote
    cd = headers.get("Content-Disposition", "")
    m = re.search(r"filename\*\s*=\s*UTF-8''([^;]+)", cd, re.I)
    if m:
        return unquote(m.group(1).strip())
    m = re.search(r'filename\s*=\s*"([^"]+)"', cd) or re.search(r"filename\s*=\s*([^;]+)", cd)
    return m.group(1).strip() if m else None


def _gd_classify_html(page: str, status: int) -> None:
    """Halaman HTML dari Drive berarti ada masalah (atau halaman konfirmasi virus-scan)."""
    low = page.lower()
    if "quota exceeded" in low or "too many users" in low or "download quota" in low:
        raise DriveError("Kuota download Drive untuk file ini habis (biasanya pulih ±24 jam). "
                         "Solusi: buka file di Drive → 'Buat salinan' ke Drive-mu sendiri, lalu pakai link salinan itu.")
    if status == 404 or "not found" in low[:3000] or "can't be found" in low or "cannot be found" in low:
        raise DriveError("File tidak ditemukan. Cek link-nya (atau file sudah dihapus).")
    if "accounts.google.com" in low or "servicelogin" in low or "sign in" in low[:4000] \
            or status in (401, 403):
        raise DriveError("File tidak publik. Di Drive: Bagikan → Akses umum → 'Siapa saja yang memiliki link'.")


def _gd_confirm_request(sess, page: str):
    """Dari halaman peringatan virus-scan, bangun request lanjutan. None jika bukan halaman itu."""
    import html as _html
    from urllib.parse import urljoin
    m = re.search(r'<form[^>]*id="download-form"[^>]*>', page)
    if m:
        a = re.search(r'action="([^"]+)"', m.group(0))
        if a:
            params = {}
            for tag in re.findall(r"<input[^>]*type=\"hidden\"[^>]*>", page):
                n, v = re.search(r'name="([^"]*)"', tag), re.search(r'value="([^"]*)"', tag)
                if n:
                    params[_html.unescape(n.group(1))] = _html.unescape(v.group(1)) if v else ""
            return urljoin(GD_BASE, _html.unescape(a.group(1))), params
    m = re.search(r'href="(/uc\?export=download[^"]+)"', page)
    if m:
        return urljoin("https://drive.google.com", _html.unescape(m.group(1))), {}
    return None


def _gd_download_id(job: dict, fid: str, stage: Path, disk_min_gb: float, label_txt: str = "") -> Path:
    """Unduh satu file Drive via stream, otomatis lanjut (Range) jika koneksi putus. Tanpa batas ukuran."""
    import requests

    sess = requests.Session()
    sess.headers["User-Agent"] = GD_UA
    to = (20, 90)

    def first():
        r = sess.get(f"{GD_BASE}/download", params={"id": fid, "export": "download", "confirm": "t"},
                     stream=True, timeout=to)
        for _ in range(3):
            if "text/html" not in r.headers.get("Content-Type", ""):
                return r
            page = r.text
            _gd_classify_html(page, r.status_code)
            nxt = _gd_confirm_request(sess, page)
            if not nxt:
                raise DriveError("Drive membalas halaman yang tidak dikenal (cek apakah link bisa dibuka tanpa login).")
            r = sess.get(nxt[0], params=nxt[1], stream=True, timeout=to)
        if "text/html" in r.headers.get("Content-Type", ""):
            _gd_classify_html(r.text, r.status_code)
            raise DriveError("Drive terus meminta konfirmasi; coba lagi nanti.")
        return r

    r = first()
    if r.status_code >= 400:
        raise DriveError(f"Drive menolak (HTTP {r.status_code}).")
    name = safe_name(_cd_filename(r.headers) or f"drive_{fid}")
    part = stage / (name + ".part")
    total = int(r.headers.get("Content-Length", "0") or 0)
    final_url = r.url
    have, t0, last_ui, last_disk = 0, time.time(), 0.0, 0
    attempts = 0
    while True:
        try:
            mode = "ab" if have else "wb"
            with open(part, mode) as f:
                for chunk in r.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    f.write(chunk)
                    have += len(chunk)
                    if have - last_disk >= 64 * 1024 * 1024:
                        last_disk = have
                        if shutil.disk_usage(BASE).free < disk_min_gb * 1024**3:
                            raise DriveError("Disk server hampir penuh; unduhan dihentikan. Hapus file dulu.")
                    now = time.time()
                    if now - last_ui > 0.7:
                        last_ui = now
                        sp = have / max(now - t0, 0.1)
                        pct = f" ({have / total * 100:.0f}%)" if total else ""
                        tot = f" / {total / 1024**3:.2f} GB" if total else ""
                        job["detail"] = (f"{label_txt}{have / 1024**3:.2f} GB{tot}{pct} · "
                                         f"{sp / 1024**2:.1f} MB/s")
            if total and have < total:
                raise ConnectionError("koneksi terputus sebelum selesai")
            break
        except DriveError:
            raise
        except Exception as e:  # noqa: BLE001  (putus jaringan, timeout, dll → lanjutkan dari byte terakhir)
            attempts += 1
            if attempts > 10:
                raise DriveError(f"Terputus terus-menerus setelah {have / 1024**2:.0f} MB: {e}")
            job["detail"] = f"{label_txt}terputus di {have / 1024**2:.0f} MB, melanjutkan ({attempts}/10)..."
            time.sleep(min(3 * attempts, 20))
            try:
                r = sess.get(final_url, headers={"Range": f"bytes={have}-"}, stream=True, timeout=to)
                if "text/html" in r.headers.get("Content-Type", ""):
                    r = first()           # link/cookie kedaluwarsa → mulai dari konfirmasi lagi
                    have = 0
                elif r.status_code == 200:    # server tak mendukung Range → ulang dari awal
                    have = 0
            except Exception:  # noqa: BLE001
                continue
    dest = stage / name
    part.rename(dest)
    return dest


def _friendly_drive_error(e: Exception) -> str:
    return f"{type(e).__name__}: {str(e)[:300]}"


def _gdrive(job: dict, url: str, stage: Path, folder: bool, disk_min_gb: float = 0.3) -> tuple[bool, str]:
    try:
        if not folder:
            fid = _gd_file_id(url)
            if not fid:
                return False, "ID file tidak ditemukan di link."
            try:
                _gd_download_id(job, fid, stage, disk_min_gb)
                return True, ""
            except DriveError as e:
                return False, str(e)
            except Exception as e:  # noqa: BLE001
                err = _friendly_drive_error(e)
            # cadangan: gdown
            import gdown
            try:
                job["detail"] = "Metode utama gagal, mencoba gdown..."
                out = gdown.download(url=url, output=str(stage) + "/", quiet=True, fuzzy=True)
                return (True, "") if out else (False, err)
            except Exception as e2:  # noqa: BLE001
                return False, f"{err} | gdown: {_friendly_drive_error(e2)}"

        import gdown
        entries = None
        try:                                   # daftar isi folder saja, lalu unduh tiap file dengan downloader sendiri
            entries = gdown.download_folder(url=url, quiet=True, skip_download=True, remaining_ok=True)
        except TypeError:
            entries = None
        except Exception as e:  # noqa: BLE001
            return False, "Folder tidak bisa dibaca: " + _friendly_drive_error(e)
        if entries is None:
            files = gdown.download_folder(url=url, output=str(stage), quiet=True, remaining_ok=True)
            return (True, "") if files else (False, "Folder kosong atau tidak bisa diakses.")
        if not entries:
            return False, "Folder kosong atau tidak publik."
        fails = []
        for n, ent in enumerate(entries, 1):
            try:
                _gd_download_id(job, ent.id, stage, disk_min_gb, label_txt=f"[{n}/{len(entries)}] ")
            except DriveError as e:
                fails.append(f"{Path(ent.path).name}: {e}")
        if fails and len(fails) == len(entries):
            return False, fails[0]
        return True, (f"{len(fails)} file gagal" if fails else "")
    except DriveError as e:
        return False, str(e)


def _ytdlp(job: dict, url: str, stage: Path, hint: str, height: int, noplaylist: bool,
           audio_opts: dict | None = None, cookies: "CookieBox | None" = None) -> tuple[bool, str]:
    import yt_dlp

    def hook(d):
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            if total:
                job["detail"] = f"Mengunduh {d.get('downloaded_bytes', 0) / total * 100:.0f}%"
        elif d.get("status") == "finished":
            job["detail"] = "Memproses file..."

    opts = {
        "outtmpl": str(stage / "%(title).80s-%(id)s.%(ext)s"),
        "restrictfilenames": True,
        "noplaylist": noplaylist,
        "quiet": True,
        "no_warnings": True,
        "retries": 3,
        "socket_timeout": 30,
        "concurrent_fragment_downloads": 4,
        "progress_hooks": [hook],
    }
    ao = {"codec": "mp3", "bitrate": "192", "embed": True, "thumb": True, **(audio_opts or {})}
    cookie_file = None
    if cookies is not None:
        cookie_file = cookies.write_to(stage)
        if cookie_file is not None:
            opts["cookiefile"] = str(cookie_file)

    def attempt(extra_audio: bool) -> None:
        o = dict(opts)
        if hint == "audio":
            o["format"] = "bestaudio/best"
            pps = [{"key": "FFmpegExtractAudio", "preferredcodec": ao["codec"],
                    "preferredquality": str(ao["bitrate"])}]
            if extra_audio and (ao["embed"] or ao["thumb"]):
                o["writethumbnail"] = True
                pps.append({"key": "FFmpegThumbnailsConvertor", "format": "jpg", "when": "before_dl"})
                if ao["embed"]:
                    pps.append({"key": "FFmpegMetadata", "add_metadata": True})
                    pps.append({"key": "EmbedThumbnail", "already_have_thumbnail": bool(ao["thumb"])})
            o["postprocessors"] = pps
        else:
            h = height
            o["format"] = (f"bv*[height<={h}][ext=mp4]+ba[ext=m4a]/b[height<={h}][ext=mp4]/"
                           f"bv*[height<={h}]+ba/b[height<={h}]/b")
            o["merge_output_format"] = "mp4"
        with yt_dlp.YoutubeDL(o) as ydl:
            ydl.extract_info(url, download=True)

    try:
        try:
            attempt(extra_audio=True)
        except Exception:  # noqa: BLE001
            if hint != "audio":
                raise
            for f in stage.glob("*"):          # buang sisa percobaan gagal, ulang lebih sederhana
                if f != cookie_file:
                    f.unlink(missing_ok=True)
            job["detail"] = "Cover/thumbnail gagal, mengulang tanpa itu..."
            attempt(extra_audio=False)
        return True, ""
    except Exception as e:  # noqa: BLE001
        clean = re.sub(r"\x1b\[[0-9;]*m", "", str(e))
        return False, "yt-dlp: " + clean[:350]
    finally:
        if cookie_file is not None:
            cookie_file.unlink(missing_ok=True)


def run_download(job: dict, url: str, hint: str = "auto", height: int = 720,
                 noplaylist: bool = True, disk_min_gb: float = 0.3,
                 audio_opts: dict | None = None, cookies: "CookieBox | None" = None):
    """Unduh 1 link (Drive file/folder atau web/yt-dlp), lalu sortir otomatis ke folder yang tepat."""
    free = shutil.disk_usage(BASE).free
    if free < disk_min_gb * 1024**3:
        return False, f"Disk kurang ({free / 1024**3:.2f} GB). Hapus file dulu."
    stage = Path(tempfile.mkdtemp(prefix="dl_", dir=TMP))
    try:
        src = classify_url(url)
        job["detail"] = f"{SOURCE_LABEL[src]} · mengunduh..."
        if src == "web":
            ok, msg = _ytdlp(job, url, stage, hint, height, noplaylist, audio_opts, cookies)
        else:
            ok, msg = _gdrive(job, url, stage, folder=(src == "gdrive_folder"), disk_min_gb=disk_min_gb)
        if not ok:
            return False, msg
        files = [p for p in stage.rglob("*")
                 if p.is_file() and p.suffix.lower() not in PARTIAL_EXTS and p.stat().st_size > 0]
        if not files:
            return False, "Tidak ada file yang berhasil diunduh."
        job["detail"] = f"Menyortir {len(files)} file..."
        placed = []
        for f in files:
            kind, dest = place_file(f, hint)
            placed.append(f"{dest.name} → {kind}")
        return True, "; ".join(placed)
    finally:
        shutil.rmtree(stage, ignore_errors=True)



# ───────────────────────── helper media ─────────────────────────
NICE = ["nice", "-n", "10"] if shutil.which("nice") else []
RES = {480: (854, 480), 720: (1280, 720), 1080: (1920, 1080)}


def media_info(path) -> dict:
    """Ringkasan ffprobe: durasi, signature stream, info video/audio utama (cover-art tidak dihitung video)."""
    try:
        j = json.loads(subprocess.check_output(
            ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
            timeout=60, stderr=subprocess.DEVNULL))
    except Exception:  # noqa: BLE001
        return {"ok": False, "dur": 0.0, "sig": (), "v": None, "a": None}
    streams = j.get("streams", [])
    real_video = [x for x in streams if x.get("codec_type") == "video"
                  and not x.get("disposition", {}).get("attached_pic")]
    audio = [x for x in streams if x.get("codec_type") == "audio"]

    def fps_of(x):
        try:
            n, d = (x.get("avg_frame_rate") or x.get("r_frame_rate") or "0/1").split("/")
            return round(int(n) / int(d), 3) if int(d) else 0.0
        except Exception:  # noqa: BLE001
            return 0.0

    v = a = None
    if real_video:
        x = real_video[0]
        v = {"codec": x.get("codec_name"), "w": int(x.get("width") or 0), "h": int(x.get("height") or 0),
             "fps": fps_of(x), "pix": x.get("pix_fmt"), "profile": x.get("profile"),
             "bitrate": int(x.get("bit_rate") or 0)}
    if audio:
        x = audio[0]
        a = {"codec": x.get("codec_name"), "sr": int(x.get("sample_rate") or 0),
             "ch": int(x.get("channels") or 0)}
    try:
        dur = float(j.get("format", {}).get("duration") or 0)
    except ValueError:
        dur = 0.0
    if not v and real_video == [] and not a:
        return {"ok": False, "dur": dur, "sig": (), "v": None, "a": None}
    fbr = int(j.get("format", {}).get("bit_rate") or 0)
    if v and not v["bitrate"]:
        v["bitrate"] = fbr
    sig = tuple((x.get("codec_type"), x.get("codec_name")) for x in streams)
    return {"ok": True, "dur": dur, "sig": sig, "v": v, "a": a, "nstreams": len(streams),
            "attached_pic": len(streams) != len(real_video) + len(audio)}


def keyframe_gap(path, dur: float, window: int = 120) -> float | None:
    """Jarak keyframe terpanjang (detik) pada ±window detik pertama; None jika tak bisa diukur."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-skip_frame", "nokey",
             "-show_entries", "frame=best_effort_timestamp_time", "-of", "csv=p=0",
             "-read_intervals", f"%+{window}", str(path)],
            capture_output=True, text=True, timeout=90).stdout
        ts = sorted(float(x) for x in re.split(r"[,\s]+", out) if re.fullmatch(r"-?\d+(\.\d+)?", x))
    except Exception:  # noqa: BLE001
        return None
    if not ts:
        return None
    end = min(dur, window) if dur else ts[-1]
    pts = ts + [end]
    return max(b - a for a, b in zip(pts, pts[1:])) if len(pts) > 1 else None


def auto_vbr(h: int, fps: int) -> int:
    """Tangga bitrate otomatis (kbps) sesuai rekomendasi YouTube Live: stabil, cukup tajam, ringan di upload."""
    base = 1500 if h <= 480 else 3000 if h <= 720 else 5000 if h <= 1080 else 8000
    return int(base * 1.4) if fps > 30 else base


def br_cap(h: int) -> int:
    """Batas bitrate sumber (bps) agar boleh dikirim copy tanpa re-encode; lebih tinggi = rawan putus."""
    return 3_500_000 if h <= 480 else 6_000_000 if h <= 720 else 9_000_000 if h <= 1080 else 13_000_000


def video_copy_ok(paths, infos) -> tuple[bool, str]:
    """Apakah semua video bisa disambung & dikirim apa adanya (tanpa re-encode)?"""
    if not infos or any(not i["ok"] or not i["v"] for i in infos):
        return False, "ada file tanpa video"
    f = infos[0]
    for p, i in zip(paths, infos):
        v = i["v"]
        if v["codec"] != "h264" or v["pix"] != "yuv420p":
            return False, f"{Path(p).name}: bukan H.264 yuv420p"
        if (v["w"], v["h"]) != (f["v"]["w"], f["v"]["h"]) or abs(v["fps"] - f["v"]["fps"]) > 0.02:
            return False, "resolusi/fps antar video berbeda"
        if i["sig"] != f["sig"]:
            return False, "susunan stream antar video berbeda"
        if v["w"] % 2 or v["h"] % 2:
            return False, "resolusi ganjil"
        if v["bitrate"] and v["bitrate"] > br_cap(v["h"]):
            return False, f"{Path(p).name}: bitrate terlalu tinggi untuk live"
        gap = keyframe_gap(p, i["dur"])
        if gap is None or gap > 4.0:
            return False, f"{Path(p).name}: jarak keyframe >4 detik (YouTube butuh ≤4 dtk)"
    return True, ""


def audio_copy_ok(infos) -> bool:
    if not infos or any(not i["ok"] or not i["a"] for i in infos):
        return False
    f = infos[0]["a"]
    return all(i["a"]["codec"] == "aac" and i["a"]["sr"] == f["sr"] and i["a"]["ch"] == f["ch"]
               and not i["attached_pic"] and i["nstreams"] == 1 for i in infos)
