"""Mesin render: gabung banyak video menjadi SATU video, simpan ke Library (folder output).

Mode video:
  copy    Concat tanpa re-encode (secepat menyalin file, kualitas 100% sama). Syarat: semua klip
          punya codec, resolusi, FPS, pixel format, dan susunan audio yang sama.
  auto    Pakai copy bila klip seragam; kalau tidak, tiap klip diseragamkan dulu (re-encode), lalu digabung.
  encode  Selalu re-encode tiap klip ke resolusi/FPS/kualitas pilihan, lalu digabung.

Opsional di atas semua mode:
  - Efek visual (effects.py) -> butuh re-encode video, dikerjakan satu kali pada hasil gabungan.
  - Audio: pakai audio bawaan video (keep), ganti dengan file audio (replace), atau campur (mix).
  - Ulangi daftar N kali / sampai durasi target, acak urutan tiap putaran.

Alur ffmpeg (maks. 3 tahap, tahap 1 & 2 hanya bila perlu):
  1. Seragamkan klip -> segmen sementara          (hanya jika klip tidak seragam / mode encode)
  2. Bangun trek audio latar dari beberapa file   (hanya jika audio replace/mix dan file > 1)
  3. Gabung (concat demuxer) + efek + audio -> file hasil di data/output
"""
from __future__ import annotations

import math
import random
import shutil
import subprocess
import tempfile
import threading
from datetime import datetime
from pathlib import Path

import effects as fxm
import services as svc

# Pilihan resolusi: kunci -> (W, H); None = ikuti klip pertama
RES_OPTIONS: dict[str, tuple[int, int] | None] = {
    "first": None,
    "480": (854, 480),
    "720": (1280, 720),
    "1080": (1920, 1080),
    "p720": (720, 1280),
    "p1080": (1080, 1920),
}
RES_LABEL = {"first": "Sama dengan klip pertama", "480": "480p (854×480)", "720": "720p (1280×720)",
             "1080": "1080p (1920×1080)", "p720": "Portrait 720×1280 (9:16)",
             "p1080": "Portrait 1080×1920 (9:16)"}
MODE_LABEL = {"auto": "Auto — copy bila seragam, re-encode bila tidak (disarankan)",
              "copy": "Copy — tanpa re-encode (tercepat, kualitas asli)",
              "encode": "Re-encode — seragamkan semua klip ke pengaturan di bawah"}
AUDIO_LABEL = {"keep": "Pakai audio bawaan video",
               "replace": "Ganti dengan file audio pilihan",
               "mix": "Campur: audio video + file audio latar"}
FIT_LABEL = {"fit": "Pas (bar hitam, tanpa terpotong)", "fill": "Penuh (potong tepi agar memenuhi layar)"}
PRESETS = ["ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow"]

MAX_LIST_ENTRIES = 20000          # batas baris daftar concat (klip × putaran)
AFMT = "aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo"

_ACTIVE: set[str] = set()
_LOCK = threading.Lock()


class RenderError(RuntimeError):
    pass


def active_files() -> list[Path]:
    """File yang sedang dipakai render (dilindungi dari hapus/pindah di Library)."""
    with _LOCK:
        return [Path(p) for p in _ACTIVE]


def _even(n: int) -> int:
    return max(2, int(n) - int(n) % 2)


def _vol(v, default: float) -> float:
    try:
        return max(0.0, min(2.0, float(v)))
    except (TypeError, ValueError):
        return default


# ───────────────────────── ffmpeg runner dengan progress ─────────────────────────
class _Progress:
    """Menjalankan ffmpeg, membaca -progress, dan memperbarui job["progress"] / job["detail"]
    berdasarkan bobot tiap tahap (bobot ~ lamanya tahap)."""

    def __init__(self, job: dict, total_weight: float):
        self.job = job
        self.total = max(total_weight, 1e-6)
        self.done = 0.0

    def run(self, cmd: list[str], dur: float, weight: float, label: str) -> None:
        job = self.job
        full = [*svc.NICE, "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-nostdin",
                "-progress", "pipe:1", "-nostats", *cmd]
        job["detail"] = f"{label} · 0%"
        with tempfile.TemporaryFile() as ef:
            pr = subprocess.Popen(full, stdout=subprocess.PIPE, stderr=ef, text=True,
                                  stdin=subprocess.DEVNULL)
            try:
                for line in pr.stdout:
                    if job.get("stop"):
                        pr.kill()
                        break
                    if line.startswith("out_time_us=") and dur > 0:
                        try:
                            frac = min(0.99, max(0.0, int(line.split("=")[1]) / 1e6 / dur))
                        except ValueError:
                            continue
                        job["progress"] = min(0.99, (self.done + weight * frac) / self.total)
                        job["detail"] = f"{label} · {frac * 100:.0f}%"
            finally:
                if pr.stdout:
                    pr.stdout.close()
                rc = pr.wait()
            if job.get("stop"):
                raise RenderError("Render dihentikan.")
            ef.seek(0)
            tail = ef.read()[-500:].decode("utf-8", "replace").strip()
        if rc != 0:
            raise RenderError(f"ffmpeg gagal pada tahap “{label}”: {tail or 'kode ' + str(rc)}")
        self.done += weight
        job["progress"] = min(0.99, self.done / self.total)


# ───────────────────────── analisis klip ─────────────────────────
def _sig(info: dict) -> tuple:
    v, a = info["v"], info["a"]
    return (v["codec"], v["w"], v["h"], round(v["fps"], 2), v["pix"], v["profile"],
            (a["codec"], a["sr"], a["ch"]) if a else None)


def _diff_reason(infos: list[dict], paths: list[Path]) -> str:
    """Penjelasan singkat kenapa klip tidak seragam (untuk pesan mode copy)."""
    f = infos[0]
    names = ("codec", "lebar", "tinggi", "FPS", "pixel format", "profile", "audio")
    fs = _sig(f)
    for p, i in zip(paths[1:], infos[1:]):
        s = _sig(i)
        diff = [n for n, a, b in zip(names, fs, s) if a != b]
        if diff:
            return f"{p.name} berbeda dari {paths[0].name} pada: {', '.join(diff)}"
    return "format klip berbeda"


def probe_clips(paths: list[Path]) -> list[dict]:
    infos = []
    for p in paths:
        if not p.is_file():
            raise RenderError(f"File tidak ada lagi di server: {p.name} (disk bisa ter-reset).")
        if p.stat().st_size == 0:
            raise RenderError(f"File berukuran 0 byte: {p.name}")
        i = svc.media_info(p)
        if not i["ok"] or not i["v"] or i["dur"] <= 0:
            raise RenderError(f"Bukan video yang valid / rusak: {p.name}")
        infos.append(i)
    return infos


def analyze(paths: list[Path]) -> dict:
    """Ringkasan untuk UI: durasi total, seragam atau tidak, resolusi klip pertama."""
    try:
        infos = probe_clips(paths)
    except RenderError as e:
        return {"ok": False, "error": str(e)}
    sigs = {_sig(i) for i in infos}
    v0 = infos[0]["v"]
    return {"ok": True, "uniform": len(sigs) == 1, "total": sum(i["dur"] for i in infos),
            "first": f'{v0["w"]}×{v0["h"]} · {v0["fps"]:g} fps · {v0["codec"]}',
            "reason": "" if len(sigs) == 1 else _diff_reason(infos, paths),
            "n_formats": len(sigs), "has_audio": [bool(i["a"]) for i in infos]}


# ───────────────────────── blok pembangun perintah ─────────────────────────
def venc_args(crf: int, preset: str, fps: int) -> list[str]:
    g = str(max(1, int(fps)) * 2)
    return ["-c:v", "libx264", "-preset", preset, "-crf", str(int(crf)), "-profile:v", "high",
            "-pix_fmt", "yuv420p", "-g", g, "-keyint_min", g, "-sc_threshold", "0"]


def _segment_cmd(src: Path, dst: Path, info: dict, W: int, H: int, fps: int, crf: int,
                 preset: str, fit: str) -> list[str]:
    """Seragamkan satu klip: ukuran, FPS, H.264 yuv420p, AAC 44.1k stereo (audio senyap bila klip tanpa audio)."""
    has_a = bool(info["a"])
    if fit == "fill":
        sc = (f"scale={W}:{H}:force_original_aspect_ratio=increase:flags=bicubic,crop={W}:{H}")
    else:
        sc = (f"scale={W}:{H}:force_original_aspect_ratio=decrease:flags=bicubic,"
              f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=black")
    vf = f"{sc},setsar=1,fps={fps},format=yuv420p"
    cmd = ["-i", str(src)]
    if not has_a:
        cmd += ["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo"]
    cmd += ["-map", "0:v:0", "-map", "0:a:0" if has_a else "1:a:0", "-vf", vf,
            *venc_args(crf, preset, fps),
            "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
            "-af", "aresample=async=1:first_pts=0"]
    if not has_a:
        cmd += ["-t", f"{info['dur']:.3f}"]
    cmd += ["-sn", "-dn", "-movflags", "+faststart", "-f", "mp4", str(dst)]
    return cmd


def _concat_text(files: list[Path], cycles: int, shuffle: bool) -> str:
    lines = []
    for _ in range(cycles):
        order = list(files)
        if shuffle:
            random.shuffle(order)
        for f in order:
            lines.append("file '" + str(f).replace("'", "'\\''") + "'")
    return "\n".join(lines) + "\n"


def _build_bg_audio(paths: list[Path], shuffle: bool, work: Path, prog: _Progress,
                    infos: list[dict]) -> Path:
    """Gabungkan beberapa file audio menjadi satu trek latar (M4A). 1 file -> dipakai langsung."""
    if len(paths) == 1:
        return paths[0]
    order = list(paths)
    if shuffle:
        random.shuffle(order)
    dst = work / "bg_audio.m4a"
    cmd: list[str] = []
    for p in order:
        cmd += ["-i", str(p)]
    chain = "".join(f"[{i}:a:0]{AFMT}[a{i}];" for i in range(len(order)))
    chain += "".join(f"[a{i}]" for i in range(len(order))) + f"concat=n={len(order)}:v=0:a=1[a]"
    cmd += ["-filter_complex", chain, "-map", "[a]", "-vn", "-c:a", "aac", "-b:a", "192k",
            "-ar", "44100", "-ac", "2", str(dst)]
    dur = sum(i["dur"] for i in infos)
    prog.run(cmd, dur, max(dur * 0.1, 1.0), "Menyusun audio latar")
    return dst


# ───────────────────────── render utama ─────────────────────────
def default_name() -> str:
    return datetime.now().strftime("gabungan_%Y%m%d_%H%M%S.mp4")


def run_render(job: dict, spec: dict, disk_min_gb: float = 0.5):
    """Gabung video sesuai `spec`. Mengembalikan (ok, info). Tidak melempar.

    spec:
      clips[list[str]], shuffle[bool], repeat[int], target_min[float, 0=nonaktif],
      mode[auto|copy|encode], res[kunci RES_OPTIONS], fps[int, 0=ikut klip pertama], crf[int], preset[str],
      fit[fit|fill], audio_mode[keep|replace|mix], audio_files[list[str]], audio_shuffle[bool],
      vol_video[float], vol_bg[float], fx[dict|None], out_name[str]
    """
    clips = [Path(p) for p in spec.get("clips") or []]
    audio_files = [Path(p) for p in spec.get("audio_files") or []]
    audio_mode = spec.get("audio_mode", "keep")
    if audio_mode not in AUDIO_LABEL:
        audio_mode = "keep"
    if audio_mode != "keep" and not audio_files:
        audio_mode = "keep"
    used = [*clips, *audio_files]
    with _LOCK:
        _ACTIVE.update(str(p.resolve()) for p in used)
    work = Path(tempfile.mkdtemp(prefix="render_", dir=svc.TMP))
    final = None
    try:
        final = _render(job, spec, clips, audio_files, audio_mode, work, disk_min_gb)
        return True, final
    except RenderError as e:
        return False, str(e)
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"
    finally:
        with _LOCK:
            for p in used:
                _ACTIVE.discard(str(p.resolve()))
        shutil.rmtree(work, ignore_errors=True)


def _render(job: dict, spec: dict, clips: list[Path], audio_files: list[Path], audio_mode: str,
            work: Path, disk_min_gb: float) -> str:
    if not clips:
        raise RenderError("Daftar video kosong.")
    job["detail"], job["progress"] = "Memeriksa video...", 0.0

    infos = probe_clips(clips)
    a_infos = []
    for p in audio_files:
        ai = svc.media_info(p)
        if not ai["ok"] or not ai["a"]:
            raise RenderError(f"Bukan file audio yang valid: {p.name}")
        a_infos.append(ai)

    mode = spec.get("mode", "auto")
    fx = spec.get("fx")
    fx_on = bool(fx) and fxm.active(fx)
    notes = []
    if fx_on and mode == "copy":
        mode = "auto"
        notes.append("efek aktif → mode copy diganti auto")

    # ── target ukuran / fps ──
    v0 = infos[0]["v"]
    res = RES_OPTIONS.get(spec.get("res", "first"))
    W, H = res if res else (_even(v0["w"]), _even(v0["h"]))
    fps = int(spec.get("fps") or 0) or int(round(v0["fps"])) or 30
    fps = max(1, min(60, fps))
    crf = max(10, min(35, int(spec.get("crf", 21))))
    preset = spec.get("preset") if spec.get("preset") in PRESETS else "veryfast"
    fit = spec.get("fit", "fit")

    sigs = {_sig(i) for i in infos}
    uniform = len(sigs) == 1
    explicit = bool(res) or bool(spec.get("fps"))        # user memilih ukuran/FPS tertentu
    matches_target = uniform and (not explicit or (
        (v0["w"], v0["h"]) == (W, H) and abs(v0["fps"] - fps) < 0.02))
    if mode == "copy" and not uniform:
        raise RenderError("Mode copy tidak bisa dipakai: " + _diff_reason(infos, clips) +
                          ". Pilih mode Auto/Re-encode, atau samakan dulu lewat tab Normalisasi.")

    if fx_on:
        seg_needed = not uniform
    elif mode == "encode":
        seg_needed = True
    elif mode == "auto":
        seg_needed = not matches_target
    else:
        seg_needed = False

    # ── durasi & siklus ──
    seg_dur_total = sum(i["dur"] for i in infos)
    target_s = max(0.0, float(spec.get("target_min") or 0) * 60)
    if target_s > 0:
        cycles = max(1, math.ceil(target_s / seg_dur_total))
    else:
        cycles = max(1, int(spec.get("repeat") or 1))
    if cycles * len(clips) > MAX_LIST_ENTRIES:
        raise RenderError(f"Terlalu banyak pengulangan ({cycles * len(clips)} entri). Kurangi ulangan/durasi target.")
    total = seg_dur_total * cycles
    if target_s > 0:
        total = min(total, target_s)

    # ── cek disk ──
    src_bytes = sum(p.stat().st_size for p in clips)
    temp_bytes = src_bytes * 1.3 if seg_needed else 0
    out_bytes = src_bytes * cycles
    free = shutil.disk_usage(svc.BASE).free
    if free < disk_min_gb * 1024**3 + temp_bytes + out_bytes:
        raise RenderError(f"Disk kurang ({free / 1024**3:.2f} GB bebas, perkiraan butuh "
                          f"{(temp_bytes + out_bytes) / 1024**3:.2f} GB + sisa {disk_min_gb} GB). "
                          "Hapus file di Library atau kecilkan ulangan/durasi target.")

    # ── bobot progress ──
    reenc_final = fx_on
    w_seg = seg_dur_total * 2.0 if seg_needed else 0.0
    w_bg = sum(i["dur"] for i in a_infos) * 0.1 if len(audio_files) > 1 and audio_mode != "keep" else 0.0
    w_fin = total * (1.5 if reenc_final else 0.25)
    prog = _Progress(job, w_seg + w_bg + w_fin)

    # ── tahap 1: seragamkan klip ──
    files, durs = list(clips), [i["dur"] for i in infos]
    if seg_needed:
        files, durs = [], []
        for n, (p, i) in enumerate(zip(clips, infos), 1):
            seg = work / f"seg_{n:04d}.mp4"
            prog.run(_segment_cmd(p, seg, i, W, H, fps, crf, preset, fit), i["dur"], i["dur"] * 2.0,
                     f"Menyeragamkan {n}/{len(clips)}: {p.name}")
            si = svc.media_info(seg)
            if not si["ok"] or not si["v"]:
                raise RenderError(f"Hasil penyeragaman rusak: {p.name}")
            files.append(seg)
            durs.append(si["dur"] or i["dur"])
        total_seg = sum(durs)
        total = total_seg * cycles if target_s <= 0 else min(total_seg * cycles, target_s)

    # ── tahap 2: audio latar ──
    bg = None
    if audio_mode != "keep":
        bg = _build_bg_audio(audio_files, bool(spec.get("audio_shuffle")), work, prog, a_infos)
        if audio_mode == "mix" and not any(i["a"] for i in infos) and not seg_needed:
            audio_mode = "replace"          # tidak ada audio video untuk dicampur

    # ── tahap 3: gabung ──
    lst = work / "list.txt"
    lst.write_text(_concat_text(files, cycles, bool(spec.get("shuffle"))), encoding="utf-8")
    out_dir = svc.DIRS["output"]
    name = svc.safe_name(str(spec.get("out_name") or "").strip() or default_name())
    if not name.lower().endswith(".mp4"):
        name = Path(name).stem + ".mp4"
    part = work / "hasil.mp4"

    cmd = ["-f", "concat", "-safe", "0", "-i", str(lst)]
    n_fx = 0
    if fx_on:
        fxin = fxm.inputs(fx)
        n_fx = fxin.count("-i")
        cmd += fxin
    limit = ["-t", f"{total:.3f}"] if (bg or fx_on or target_s > 0) else []
    fc: list[str] = []
    maps: list[str] = []

    if fx_on:
        fc.append(fxm.graph(fx, W, H, fps, work))
        maps += ["-map", "[v]"]
        vcodec = venc_args(crf, preset, fps)
    else:
        maps += ["-map", "0:v:0"]
        vcodec = ["-c:v", "copy"]

    if bg is not None:
        idx = 1 + n_fx
        cmd += ["-stream_loop", "-1", "-i", str(bg)]
        vb = _vol(spec.get("vol_bg"), 1.0)
        vv = _vol(spec.get("vol_video"), 1.0)
        fc.append(f"[{idx}:a]{AFMT},volume={vb:.2f}[bg]")
        if audio_mode == "mix":
            fc.append(f"[0:a]{AFMT},volume={vv:.2f}[va]")
            fc.append("[va][bg]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,"
                      "alimiter=limit=0.95:level=0[aout]")
        else:
            fc.append("[bg]alimiter=limit=0.95:level=0[aout]")
        maps += ["-map", "[aout]"]
        acodec = ["-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2"]
    else:
        maps += ["-map", "0:a:0?"]
        acodec = ["-c:a", "copy"]

    if fc:
        cmd += ["-filter_complex", ";".join(fc)]
    cmd += [*maps, *vcodec, *acodec, *limit, "-sn", "-dn", "-movflags", "+faststart", "-f", "mp4", str(part)]

    how = "re-encode + efek" if fx_on else ("copy" if not seg_needed else "seragamkan + copy")
    prog.run(cmd, total, w_fin, f"Menggabung {len(clips)} video ({how})")

    # ── verifikasi & simpan ke Library ──
    ri = svc.media_info(part)
    if not ri["ok"] or not ri["v"] or ri["dur"] <= 0:
        raise RenderError("Hasil render tidak terbaca (korup). Coba mode Re-encode.")
    if ri["dur"] < total * 0.9 - 2:
        raise RenderError(f"Durasi hasil ({ri['dur']:.0f} dtk) jauh lebih pendek dari seharusnya "
                          f"({total:.0f} dtk). Coba mode Re-encode.")
    dest = svc.unique_path(out_dir, name)
    shutil.move(str(part), str(dest))
    mb = dest.stat().st_size / 1024 / 1024
    mins = int(ri["dur"]) // 60
    extra = f" · {'; '.join(notes)}" if notes else ""
    job["detail"], job["progress"] = f"Selesai: {dest.name}", 1.0
    return f"{dest.name} | {mins}m{int(ri['dur']) % 60:02d}s | {mb:.1f} MB | {how}{extra}"
