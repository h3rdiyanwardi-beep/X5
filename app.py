import hashlib
import os
import random
import secrets
import shutil
from functools import partial
from pathlib import Path
from urllib.parse import quote

import pandas as pd
import streamlit as st

import effects as fxm
import mediatools as mt
import renderer
import services as svc
import uploader

st.set_page_config(page_title="Studio Render", page_icon="🎬", layout="wide")


KIND_LABEL = {"video": "🎞️ Video", "audio": "🎵 Audio", "output": "📦 Hasil",
              "thumb": "🖼️ Thumbnail", "other": "🗂️ Lainnya"}
STATUS_ICON = {"antre": "⏳ antre", "berjalan": "🔄 berjalan", "selesai": "✅ selesai",
               "gagal": "❌ gagal", "dibatalkan": "🚫 dibatalkan", "diulang": "🔁 diulang"}
HINT_LABEL = {"auto": "Otomatis (deteksi isi file)", "video": "Video", "audio": "Audio"}


@st.cache_data(ttl=15, show_spinner=False)
def _disk_info() -> dict:
    return svc.disk_info()


@st.cache_resource
def get_runner(name: str, workers: int) -> svc.JobRunner:
    return svc.JobRunner(workers)


@st.cache_resource
def get_upload_server():
    """Server upload resumable di port sendiri (default 8502) + tunnel opsional."""
    port = int(os.environ.get("UPLOAD_PORT", "8502"))
    token = os.environ.get("UPLOAD_TOKEN")
    if not token:
        try:
            token = st.secrets.get("UPLOAD_TOKEN")
        except Exception:  # noqa: BLE001
            token = None
    token = token or secrets.token_urlsafe(18)
    srv = uploader.UploadServer(port, token)
    srv.start()
    return srv, uploader.Tunnel(port)


dl_jobs = get_runner("download", 2)   # 2 unduhan paralel
jobs = get_runner("process", 1)       # render/normalisasi satu per satu (hemat CPU/RAM)

if shutil.which("ffmpeg") is None:
    st.error("ffmpeg tidak ditemukan. Pastikan `packages.txt` berisi `ffmpeg`.")
    st.stop()


def protected_files():
    """File yang sedang dipakai render tidak boleh dihapus/dipindah dari Library."""
    return renderer.active_files()


def _mmss(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 3600}:{(sec % 3600) // 60:02d}:{sec % 60:02d}"


@st.cache_data(show_spinner=False)
def _dur(path_str: str, mtime: float) -> float:
    return svc.media_info(path_str)["dur"]


def file_actions(sel: Path, kind: str, key: str) -> None:
    """Preview + tombol download untuk satu file Library (dipakai tab Render)."""
    mb = sel.stat().st_size / 1024 / 1024
    _srv, _tn = get_upload_server()
    fq = f"/file?kind={kind}&name={quote(sel.name)}&t={_srv.token}"
    base_url = _tn.url if _tn.running and _tn.url else None
    ext = sel.suffix.lower()
    if ext in svc.VIDEO_EXTS and mb < 200:
        st.video(str(sel))
    elif ext in svc.VIDEO_EXTS and base_url:
        st.components.v1.html(f'<video controls preload="metadata" style="width:100%;max-height:420px" '
                              f'src="{base_url}{fq}"></video>', height=440)
    else:
        st.caption("Preview file besar butuh tunnel (buka dari tab Impor → Upload file besar).")
    if mb < 300:
        st.download_button(f"⬇️ Download {sel.name} ({mb:,.1f} MB)", sel.read_bytes(), file_name=sel.name,
                           key=f"{key}_dl", type="primary")
    elif base_url:
        st.link_button(f"⬇️ Download {mb:.0f} MB (streaming, bisa dilanjut)", f"{base_url}{fq}&dl=1")
    else:
        st.caption(f"File {mb:.0f} MB: buka tunnel di tab Impor dulu agar link download streaming muncul "
                   "(tanpa batas ukuran, tidak memakai RAM).")


def fx_panel(prefix: str) -> dict:
    """Panel efek visual. Dict hasilnya diteruskan ke renderer.run_render."""
    thumbs = svc.list_paths("thumb")
    gifs = [p for p in thumbs if p.suffix.lower() == ".gif"]
    txts = [p for p in svc.list_paths("other") if p.suffix.lower() == ".txt"]

    def K(name: str) -> str:
        return f"{prefix}_{name}"

    with st.container(border=True):
        st.markdown("**✨ Efek visual** — crop, warna, teks, subtitle, gambar, GIF")
        tabs = st.tabs(["🔍 Crop & zoom", "🎨 Warna", "🔤 Teks diam", "📰 Teks berjalan",
                        "💬 Subtitle .txt", "🖼️ Gambar", "🎞️ GIF"])

        # ── crop (default) + zoom animasi opsional ──
        with tabs[0]:
            crop_on = st.checkbox("Crop tetap (video tampil lebih 'maju', bukan fit-in)", value=False, key=K("crop"),
                                  help="Video diperbesar lalu tepinya dipotong, hasilnya tetap dan jadi default. "
                                       "Matikan untuk kembali ke tampilan fit-in biasa.")
            c1, c2 = st.columns(2)
            z_amt = c1.slider("Besar crop (%)", 2, 60, 15, 1, key=K("zamt"), disabled=not crop_on)
            z_anc = c2.selectbox("Bagian yang dipertahankan", fxm.POSITIONS,
                                 index=fxm.POSITIONS.index("tengah"), key=K("zanc"), disabled=not crop_on,
                                 help="Tengah = potong rata di semua sisi. Pilihan lain menggeser fokus crop.")
            z_anim = st.selectbox("Zoom in/out animasi (opsional, di atas ukuran crop)", list(fxm.ZOOM_ANIMS),
                                  format_func=fxm.ZOOM_ANIMS.get, key=K("zanim"))
            z1, z2 = st.columns(2)
            za_amt = z1.slider("Tambahan zoom maksimum (%)", 2, 50, 10, 1, key=K("zaamt"),
                               disabled=z_anim == "none")
            z_per = z2.slider("Siklus (dtk)", 4, 60, 12, 1, key=K("zper"), disabled=z_anim == "none",
                              help="Waktu satu putaran zoom.")

        # ── warna ──
        with tabs[1]:
            col_on = st.checkbox("Warnai ulang video", key=K("con"))
            q1, q2, q3 = st.columns(3)
            sat = q1.slider("Saturasi", 0.0, 3.0, 1.3, 0.05, key=K("csat"), disabled=not col_on,
                            help="1.0 = asli · 0 = hitam putih · di atas 1.5 = sangat tajam.")
            con = q2.slider("Kontras", 0.5, 2.0, 1.0, 0.05, key=K("ccon"), disabled=not col_on)
            bri = q3.slider("Kecerahan", -0.3, 0.3, 0.0, 0.02, key=K("cbri"), disabled=not col_on)

        # ── teks diam ──
        with tabs[2]:
            t1, t2 = st.columns([1, 3])
            t_on = t1.checkbox("Teks diam", key=K("ton"))
            t_val = t2.text_input("Teks overlay", key=K("tval"), disabled=not t_on,
                                  label_visibility="collapsed", placeholder="Teks overlay, mis. nama channel")
            t3, t4, t5, t6 = st.columns(4)
            t_pos = t3.selectbox("Posisi teks", fxm.POSITIONS, index=5, key=K("tpos"), disabled=not t_on)
            t_size = t4.slider("Ukuran (% tinggi)", 2, 15, 5, key=K("tsize"), disabled=not t_on)
            t_col = t5.selectbox("Warna", fxm.COLORS, key=K("tcol"), disabled=not t_on)
            t_box = t6.checkbox("Kotak latar", value=True, key=K("tbox"), disabled=not t_on)
            t7, t8 = st.columns(2)
            t_anim = t7.selectbox("Animasi", list(fxm.ANIMS), format_func=fxm.ANIMS.get, key=K("tanim"),
                                  disabled=not t_on)
            t_op = t8.slider("Opasitas (%)", 10, 100, 100, 5, key=K("top"), disabled=not t_on)

        # ── teks berjalan (berita) ──
        with tabs[3]:
            k_on = st.checkbox("Teks berjalan ala berita", key=K("kon"))
            k_val = st.text_area("Isi teks berjalan (satu baris per berita)", key=K("kval"), height=90,
                                 disabled=not k_on, placeholder="Berita 1\nBerita 2\nBerita 3")
            k1, k2, k3 = st.columns(3)
            k_pos = k1.radio("Posisi bar", fxm.TICKER_POS, horizontal=True, key=K("kpos"), disabled=not k_on)
            k_size = k2.slider("Ukuran (% tinggi)", 2.0, 12.0, 4.5, 0.5, key=K("ksize"), disabled=not k_on)
            k_speed = k3.slider("Kecepatan (% lebar layar / dtk)", 1, 40, 5, 1, key=K("kspeed"),
                                disabled=not k_on, help="5 = pelan seperti running text berita. Naikkan untuk lebih cepat.")
            k4, k5, k6 = st.columns(3)
            k_col = k4.selectbox("Warna", fxm.COLORS, key=K("kcol"), disabled=not k_on)
            k_bar = k5.checkbox("Bar latar gelap", value=True, key=K("kbar"), disabled=not k_on)
            k_bop = k6.slider("Opasitas bar (%)", 10, 100, 65, 5, key=K("kbop"), disabled=not (k_on and k_bar))

        # ── subtitle dari file .txt ──
        with tabs[4]:
            s_on = st.checkbox("Subtitle dari file .txt (tengah bawah)", key=K("son"), disabled=not txts)
            s_path = st.selectbox("File .txt", txts, index=0 if txts else None, format_func=lambda p: p.name,
                                  key=K("spath"), disabled=not (s_on and txts), placeholder="Pilih file .txt...")
            s1, s2, s3 = st.columns(3)
            s_words = s1.slider("Kata per tampilan", 2, 12, 6, 1, key=K("swords"), disabled=not s_on)
            s_secs = s2.slider("Detik per tampilan", 1.0, 15.0, 3.0, 0.5, key=K("ssecs"), disabled=not s_on)
            s_size = s3.slider("Ukuran (% tinggi)", 2, 12, 5, 1, key=K("ssize"), disabled=not s_on)
            s4, s5 = st.columns(2)
            s_col = s4.selectbox("Warna", fxm.COLORS, key=K("scol"), disabled=not s_on)
            s_box = s5.checkbox("Kotak latar", value=True, key=K("sbox"), disabled=not s_on)
            if s_on and s_path:
                n_chunk, cyc = fxm.sub_info(str(s_path), s_words, s_secs)
                st.caption(f"{n_chunk} potongan · satu siklus {cyc / 60:.1f} menit, lalu mengulang dari awal "
                           f"(dimulai ulang tiap video baru).")
            if not txts:
                st.caption("Upload file .txt lewat tab Impor (masuk folder Lainnya). Teks dipecah otomatis "
                           "per beberapa kata dan ditampilkan bergantian seperti subtitle.")

        # ── gambar ──
        with tabs[5]:
            i1, i2 = st.columns([1, 3])
            i_on = i1.checkbox("Overlay gambar", key=K("ion"), disabled=not thumbs)
            i_path = i2.selectbox("Gambar overlay", thumbs, index=0 if thumbs else None,
                                  format_func=lambda p: p.name, key=K("ipath"),
                                  disabled=not (i_on and thumbs), label_visibility="collapsed",
                                  placeholder="Pilih gambar/logo...")
            i_full = st.checkbox("Gambar layar penuh (menutupi seluruh video, atur opasitas untuk efek overlay)",
                                 key=K("ifull"), disabled=not i_on)
            i3, i4, i5, i6 = st.columns(4)
            i_pos = i3.selectbox("Posisi gambar", fxm.POSITIONS, index=2, key=K("ipos"),
                                 disabled=not i_on or i_full)
            i_w = i4.slider("Lebar (% layar)", 3, 60, 15, key=K("iw"), disabled=not i_on or i_full)
            i_op = i5.slider("Opasitas (%)", 5, 100, 100, 5, key=K("iop"), disabled=not i_on)
            i_anim = i6.selectbox("Animasi", list(fxm.ANIMS), format_func=fxm.ANIMS.get, key=K("ianim"),
                                  disabled=not i_on)
            if not thumbs:
                st.caption("Untuk overlay gambar/logo, upload PNG/JPG lewat tab Impor (masuk folder Thumbnail).")

        # ── GIF + chromakey ──
        with tabs[6]:
            g1, g2 = st.columns([1, 3])
            g_on = g1.checkbox("Overlay GIF", key=K("gon"), disabled=not gifs)
            g_path = g2.selectbox("File GIF", gifs, index=0 if gifs else None, format_func=lambda p: p.name,
                                  key=K("gpath"), disabled=not (g_on and gifs), label_visibility="collapsed",
                                  placeholder="Pilih file .gif...")
            g_full = st.checkbox("Layar penuh (bergerak di atas video)", value=True, key=K("gfull"),
                                 disabled=not g_on)
            g3, g4 = st.columns(2)
            g_key = g3.selectbox("Chromakey (latar jadi transparan)", list(fxm.KEY_MODES),
                                 format_func=fxm.KEY_MODES.get, key=K("gkey"), disabled=not g_on)
            g_op = g4.slider("Opasitas (%)", 5, 100, 100, 5, key=K("gop"), disabled=not g_on)
            g5, g6, g7, g8 = st.columns(4)
            g_sim = g5.slider("Toleransi warna", 0.02, 0.6, 0.22, 0.02, key=K("gsim"),
                              disabled=not g_on or g_key in ("off",),
                              help="Naikkan jika sisa warna latar masih terlihat; turunkan jika objek ikut hilang.")
            g_bl = g6.slider("Halus tepi", 0.0, 0.5, 0.1, 0.02, key=K("gbl"), disabled=not g_on or g_key == "off")
            g_pos = g7.selectbox("Posisi", fxm.POSITIONS, index=5, key=K("gpos"), disabled=not g_on or g_full)
            g_w = g8.slider("Lebar (% layar)", 5, 100, 30, key=K("gw"), disabled=not g_on or g_full)
            if not gifs:
                st.caption("Upload file .gif lewat tab Impor (masuk folder Thumbnail).")

    return {"zoom": {"crop": crop_on, "anchor": z_anc, "amount": z_amt / 100, "anim": z_anim,
                     "anim_amount": za_amt / 100, "period": z_per},
            "color": {"on": col_on, "saturation": sat, "contrast": con, "brightness": bri},
            "text": {"on": t_on, "value": t_val, "pos": t_pos, "size": t_size / 100, "color": t_col,
                     "box": t_box, "opacity": t_op / 100, "anim": t_anim},
            "ticker": {"on": k_on, "value": k_val, "pos": k_pos, "size": k_size / 100,
                       "speed": k_speed / 100, "color": k_col, "bar": k_bar, "bar_opacity": k_bop / 100},
            "subs": {"on": s_on, "path": str(s_path) if s_path else "", "words": s_words, "seconds": s_secs,
                     "size": s_size / 100, "color": s_col, "box": s_box},
            "image": {"on": i_on, "path": str(i_path) if i_path else "", "pos": i_pos, "full": i_full,
                      "width": i_w / 100, "opacity": i_op / 100, "anim": i_anim},
            "gif": {"on": g_on, "path": str(g_path) if g_path else "", "full": g_full, "key": g_key,
                    "similarity": g_sim, "blend": g_bl, "pos": g_pos, "width": g_w / 100, "opacity": g_op / 100}}


def jobs_table(rows, key=""):
    if not rows:
        st.caption("Belum ada job.")
        return
    df = pd.DataFrame(rows)
    df["Status"] = df["Status"].map(lambda s: STATUS_ICON.get(s, s))
    st.dataframe(df, hide_index=True, width="stretch")
    failed = [r for r in rows if r["Status"] == "gagal"]
    if failed:
        with st.expander(f"❌ Detail error ({len(failed)} gagal)", expanded=True):
            for r in failed[:8]:
                st.markdown(f"**{r['Job']}** · `{r['ID']}`")
                st.code(str(r["Info"]) or "(tanpa pesan)", language="text")


# ───────────── sidebar ─────────────
with st.sidebar:
    st.title("🎬 Studio Render")
    di = _disk_info()
    c1, c2 = st.columns(2)
    c1.metric("Disk kosong", f"{di['free_gb']:.1f} GB")
    c2.metric("Data app", f"{di['data_gb']:.2f} GB")
    disk_min = st.number_input("Batas minimum sisa disk (GB)", 0.0, 50.0, 0.5, 0.1, key="disk_min",
                               help="Unduh, render, dan normalisasi dibatalkan jika sisa disk di bawah angka ini.")
    st.caption(f"Antrean — unduh: {dl_jobs.pending()} · render/normalisasi: {jobs.pending()}")
    if st.button("🧹 Bersihkan folder tmp", width="stretch"):
        if dl_jobs.pending() or jobs.pending():
            st.toast("Tunggu semua job selesai dulu.")
        else:
            svc.clear_tmp()
            st.toast("Folder tmp dibersihkan")
    with st.expander("🔧 Komponen"):
        ts = svc.tool_status()
        st.write(("✅" if ts["ffmpeg"] else "❌") + " ffmpeg")
        st.write(("✅" if ts["deno"] else "⚠️") + " deno (untuk YouTube)")
        st.write(("✅ yt-dlp " + ts["yt-dlp"]) if ts["yt-dlp"] else "❌ yt-dlp")
    st.divider()
    st.caption("⚠️ Penyimpanan Streamlit Cloud tidak permanen. File bisa hilang saat app "
               "restart/redeploy; download hasil penting dari tab Render atau Library.")

st.title("Studio Render")

tab_render, tab_lib, tab_imp, tab_norm, tab_jobs = st.tabs(
    ["🎬 Render", "📁 Library", "⬇️ Impor", "🧰 Normalisasi & Tes", "⚙️ Job"])

# ───────────── RENDER ─────────────
def _rd_items() -> list:
    return list(st.session_state.get("rd_order", []))


def _rd_add():
    add = [str(p) for p in (st.session_state.get("rd_add") or [])]
    st.session_state["rd_order"] = [*_rd_items(), *add]
    st.session_state["rd_add"] = []


def _rd_move(i: int, delta: int):
    o = _rd_items()
    j = i + delta
    if 0 <= i < len(o) and 0 <= j < len(o):
        o[i], o[j] = o[j], o[i]
        st.session_state["rd_order"] = o


def _rd_top(i: int):
    o = _rd_items()
    if 0 < i < len(o):
        o.insert(0, o.pop(i))
        st.session_state["rd_order"] = o


def _rd_del(i: int):
    o = _rd_items()
    if 0 <= i < len(o):
        o.pop(i)
        st.session_state["rd_order"] = o


def _rd_clear():
    st.session_state["rd_order"] = []


def _rd_sort():
    st.session_state["rd_order"] = sorted(_rd_items(), key=lambda s: Path(s).name.lower())


def _rd_shuffle():
    o = _rd_items()
    random.shuffle(o)
    st.session_state["rd_order"] = o


@st.cache_data(show_spinner=False)
def _analyze(paths: tuple, stamps: tuple) -> dict:
    return renderer.analyze([Path(p) for p in paths])


with tab_render:
    st.markdown("### 🎬 Render — gabung banyak video jadi 1 video")
    st.caption("Susun video → pilih mode → **Render**. Hasil otomatis tersimpan di Library (📦 Hasil) dan "
               "bisa langsung diunduh dari bagian bawah tab ini.")

    rd_vlib = svc.library_videos()
    if not rd_vlib:
        st.warning("Library video masih kosong. Tambahkan video lewat tab **Impor**.")
    order = [p for p in _rd_items() if Path(p).is_file()]       # buang file yang sudah dihapus dari Library
    if order != _rd_items():
        st.session_state["rd_order"] = order

    # ── 1. susun urutan ──
    st.markdown("#### 1️⃣ Susun urutan video")
    a1, a2 = st.columns([5, 1.2], vertical_alignment="bottom")
    a1.multiselect("Tambah video ke urutan (sesuai urutan klik; video yang sama boleh ditambahkan lagi)",
                   rd_vlib, format_func=svc.label, key="rd_add", placeholder="Pilih satu atau beberapa video...")
    a2.button("➕ Tambahkan", key="rd_addbtn", on_click=_rd_add, width="stretch",
              disabled=not st.session_state.get("rd_add"))

    if order:
        for i, ps in enumerate(order):
            p = Path(ps)
            c0, c1, c2, c3, c4, c5, c6 = st.columns([0.5, 5, 1.2, 0.6, 0.6, 0.6, 0.6], vertical_alignment="center")
            c0.markdown(f"**{i + 1}**")
            c1.write(svc.label(p))
            c2.caption(_mmss(_dur(ps, p.stat().st_mtime)))
            c3.button("⏫", key=f"rd_top_{i}", on_click=_rd_top, args=(i,), disabled=i == 0,
                      help="Pindahkan ke paling atas")
            c4.button("🔼", key=f"rd_up_{i}", on_click=_rd_move, args=(i, -1), disabled=i == 0,
                      help="Naik satu posisi")
            c5.button("🔽", key=f"rd_dn_{i}", on_click=_rd_move, args=(i, 1), disabled=i == len(order) - 1,
                      help="Turun satu posisi")
            c6.button("❌", key=f"rd_rm_{i}", on_click=_rd_del, args=(i,), help="Hapus dari urutan")
        t1, t2, t3, _ = st.columns([1, 1, 1, 3])
        t1.button("🔤 Urut A–Z", key="rd_sort", on_click=_rd_sort, width="stretch")
        t2.button("🔀 Acak sekali", key="rd_shuf", on_click=_rd_shuffle, width="stretch")
        t3.button("🧹 Kosongkan", key="rd_clr", on_click=_rd_clear, width="stretch")
        stamps = tuple(Path(p).stat().st_mtime_ns for p in order)
        an = _analyze(tuple(order), stamps)
    else:
        st.info("Urutan masih kosong. Pilih video di atas lalu klik **Tambahkan**.")
        an = None

    uniform = bool(an and an.get("ok") and an["uniform"])
    if an and not an.get("ok"):
        st.error(an["error"])
    elif an:
        if uniform:
            st.success(f"{len(order)} video · total {_mmss(an['total'])} · format seragam ({an['first']}) → "
                       "bisa digabung **tanpa re-encode** (copy, tercepat).")
        else:
            st.warning(f"{len(order)} video · total {_mmss(an['total'])} · format berbeda "
                       f"({an['n_formats']} jenis): {an['reason']}. Mode Auto akan menyeragamkan dulu "
                       "(re-encode), atau pakai tab Normalisasi.")

    # ── 2. mode & kualitas ──
    st.markdown("#### 2️⃣ Mode gabung & kualitas")
    rd_mode = st.radio("Mode video", list(renderer.MODE_LABEL), key="rd_mode",
                       format_func=renderer.MODE_LABEL.get)
    with st.expander("⚙️ Ukuran, FPS & kualitas (dipakai saat re-encode / efek)",
                     expanded=rd_mode == "encode"):
        q1, q2, q3 = st.columns(3)
        rd_res = q1.selectbox("Resolusi", list(renderer.RES_OPTIONS), format_func=renderer.RES_LABEL.get,
                              key="rd_res")
        rd_fps = q2.selectbox("FPS", [0, 24, 25, 30, 60], format_func=lambda v: "Ikut klip pertama" if not v else f"{v} fps",
                              key="rd_fps")
        rd_fit = q3.radio("Penyesuaian layar", list(renderer.FIT_LABEL), format_func=renderer.FIT_LABEL.get,
                          key="rd_fit")
        q4, q5 = st.columns(2)
        rd_crf = q4.slider("Kualitas (CRF — makin kecil makin tajam & besar)", 16, 30, 21, key="rd_crf",
                           help="18 ≈ hampir tanpa kehilangan, 21 seimbang, 26+ hemat ukuran.")
        rd_preset = q5.selectbox("Kecepatan encode", renderer.PRESETS, index=2, key="rd_preset",
                                 help="Lebih lambat = file lebih kecil untuk kualitas yang sama.")
        st.caption("Pengaturan ini diabaikan oleh mode Copy.")

    # ── 3. audio ──
    st.markdown("#### 3️⃣ Audio")
    rd_amode = st.radio("Sumber audio", list(renderer.AUDIO_LABEL), key="rd_amode", horizontal=True,
                        format_func=renderer.AUDIO_LABEL.get)
    rd_afiles, rd_ashuf, rd_vbg, rd_vvid = [], False, 1.0, 1.0
    if rd_amode != "keep":
        alib = svc.list_paths("audio")
        if not alib:
            st.warning("Folder audio kosong. Tambahkan file audio lewat tab **Impor**; tanpa file audio, "
                       "audio bawaan video yang dipakai.")
        rd_afiles = st.multiselect("File audio (diputar berurutan, diulang otomatis sampai video selesai)",
                                   alib, format_func=lambda p: p.name, key="rd_afiles")
        v1, v2, v3 = st.columns(3)
        rd_ashuf = v1.checkbox("Acak urutan audio", key="rd_ashuf")
        rd_vbg = v2.slider("Volume audio latar", 0.0, 2.0, 1.0 if rd_amode == "replace" else 0.6, 0.05,
                           key=f"rd_vbg_{rd_amode}")
        rd_vvid = v3.slider("Volume audio video", 0.0, 2.0, 1.0, 0.05, key="rd_vvid",
                            disabled=rd_amode != "mix")

    # ── 4. efek ──
    st.markdown("#### 4️⃣ Efek visual (opsional)")
    with st.expander("✨ Crop, warna, teks, gambar, GIF…"):
        rd_fx = fx_panel("rd")
    fx_on = fxm.active(rd_fx)
    if fx_on:
        st.info("Efek aktif → video di-encode ulang satu kali pada hasil gabungan (lebih lama dari mode copy).")
        for w in fxm.warnings(rd_fx):
            st.warning(w)

    # ── 5. ulangi & simpan ──
    st.markdown("#### 5️⃣ Pengulangan & nama file")
    r1, r2, r3 = st.columns(3)
    rd_repeat = r1.number_input("Ulangi daftar (kali)", 1, 500, 1, 1, key="rd_repeat")
    rd_target = r2.number_input("…atau sampai durasi (menit, 0 = nonaktif)", 0.0, 1440.0, 0.0, 1.0,
                                key="rd_target", help="Jika diisi, daftar diulang sampai durasi ini lalu dipotong.")
    rd_shuf_cycle = r3.checkbox("Acak urutan tiap putaran", key="rd_shufc",
                                help="Berlaku saat daftar diulang lebih dari sekali.")
    rd_name = st.text_input("Nama file hasil", key="rd_name", placeholder="kosong = gabungan_TANGGAL_JAM.mp4")

    if an and an.get("ok"):
        out_sec = rd_target * 60 if rd_target > 0 else an["total"] * rd_repeat
        st.caption(f"Perkiraan durasi hasil: **{_mmss(out_sec)}**")

    blocked = ""
    if not order:
        blocked = "Susun minimal 1 video dulu."
    elif an and not an.get("ok"):
        blocked = "Ada video yang tidak valid."
    elif rd_mode == "copy" and not uniform and not fx_on:
        blocked = "Mode Copy butuh format seragam. Pakai mode Auto atau Re-encode."
    elif rd_amode != "keep" and not rd_afiles:
        blocked = "Pilih minimal 1 file audio, atau ganti sumber audio ke “bawaan video”."
    if blocked:
        st.caption(f"⛔ {blocked}")

    if st.button("🎬 Render sekarang", type="primary", key="rd_go", disabled=bool(blocked), width="stretch"):
        spec = {"clips": list(order), "shuffle": rd_shuf_cycle, "repeat": int(rd_repeat),
                "target_min": float(rd_target), "mode": rd_mode, "res": rd_res, "fps": int(rd_fps),
                "crf": int(rd_crf), "preset": rd_preset, "fit": rd_fit, "audio_mode": rd_amode,
                "audio_files": [str(p) for p in rd_afiles], "audio_shuffle": rd_ashuf,
                "vol_video": rd_vvid, "vol_bg": rd_vbg, "fx": rd_fx if fx_on else None,
                "out_name": rd_name}
        title = f"Render · {svc.safe_name(rd_name) if rd_name.strip() else f'{len(order)} video digabung'}"
        jobs.submit(title, partial(renderer.run_render, spec=spec, disk_min_gb=disk_min))
        st.toast("Render diantrekan. Pantau progres di bawah.")

    # ── progres & hasil ──
    st.markdown("#### 📊 Progres")

    @st.fragment(run_every=2)
    def render_progress():
        act = [j for j in jobs.active() if j["title"].startswith("Render")]
        was = st.session_state.get("rd_was_active", False)
        st.session_state["rd_was_active"] = bool(act)
        if was and not act:                      # render baru selesai → segarkan daftar hasil di bawah
            st.rerun()
        for j in act:
            st.progress(min(1.0, j["progress"]),
                        text=f"{j['title']} — {STATUS_ICON.get(j['status'], j['status'])} · {j['detail']}")
        if any(j["status"] == "berjalan" for j in act):
            if st.button("⏹ Hentikan render yang berjalan", key="rd_stop"):
                st.toast(f"{jobs.stop_running()} render dihentikan")
        rows = [r for r in jobs.snapshot() if str(r["Job"]).startswith("Render")][:5]
        if rows:
            jobs_table(rows)
        elif not act:
            st.caption("Belum ada render.")

    render_progress()

    st.markdown("#### 📦 Hasil render (tersimpan di Library → Hasil)")
    outs = sorted(svc.list_paths("output"), key=lambda p: p.stat().st_mtime, reverse=True)[:30]
    if not outs:
        st.caption("Belum ada hasil. Hasil render muncul di sini dan di Library → 📦 Hasil.")
    else:
        sel_out = st.selectbox("File hasil (terbaru di atas)", outs, format_func=lambda p: f"{p.name} · "
                               f"{p.stat().st_size / 1024 / 1024:,.1f} MB", key="rd_result")
        if sel_out is not None:
            file_actions(sel_out, "output", "rd_res")

# ───────────── LIBRARY ─────────────
with tab_lib:
    counts = {k: len(svc.list_paths(k)) for k in svc.DIRS}
    kind = st.radio("Folder", list(svc.DIRS), horizontal=True, key="lib_kind",
                    format_func=lambda k: f"{KIND_LABEL[k]} ({counts[k]})")
    files = svc.list_files(kind)
    if not files:
        st.info("Folder kosong. Tambahkan file lewat tab **Impor**; hasil render ada di 📦 Hasil.")
    else:
        q = st.text_input("🔎 Cari file", key=f"lib_q_{kind}", placeholder="ketik sebagian nama file…")
        shown = [f for f in files if q.lower() in f["name"].lower()] if q else files
        ver_k, all_k = f"tv_{kind}", f"tall_{kind}"
        s1, s2, s3 = st.columns([1, 1, 4])
        if s1.button("☑️ Pilih semua", key=f"sa_{kind}", disabled=not shown, width="stretch"):
            st.session_state[all_k] = True
            st.session_state[ver_k] = st.session_state.get(ver_k, 0) + 1
            st.rerun()
        if s2.button("⬜ Kosongkan", key=f"sn_{kind}", width="stretch"):
            st.session_state[all_k] = False
            st.session_state[ver_k] = st.session_state.get(ver_k, 0) + 1
            st.rerun()
        s3.caption(f"{len(shown)} file ditampilkan" + (f" dari {len(files)}" if q else "")
                   + " · centang manual per baris, atau pakai Pilih semua.")
        df = pd.DataFrame({"Pilih": bool(st.session_state.get(all_k, False)) if shown else [],
                           "File": [f["name"] for f in shown],
                           "MB": [round(f["mb"], 1) for f in shown],
                           "Diubah": [f["mtime"] for f in shown]})
        # kunci tabel ikut berubah saat daftar file / filter berubah, agar centang tidak bergeser ke baris lain
        sig = hashlib.md5("|".join(f["name"] for f in shown).encode()).hexdigest()[:8]
        tkey = f"tbl_{kind}_{st.session_state.get(ver_k, 0)}_{sig}"
        edited = st.data_editor(df, key=tkey, hide_index=True, width="stretch",
                                disabled=["File", "MB", "Diubah"],
                                column_config={"Pilih": st.column_config.CheckboxColumn("Pilih", width="small")})
        chosen = [svc.DIRS[kind] / n for n in edited.loc[edited["Pilih"], "File"]]
        if chosen:
            tot_mb = sum(f["mb"] for f in shown if f["name"] in {c.name for c in chosen})
            st.caption(f"✔ {len(chosen)} file dipilih · {tot_mb:,.1f} MB")
            a_, b_, c_ = st.columns([1, 1, 1])
            with a_.popover(f"🗑️ Hapus {len(chosen)} file", width="stretch"):
                st.warning(f"{len(chosen)} file akan dihapus permanen.")
                if st.button("Ya, hapus sekarang", type="primary", key=f"lib_del_{kind}"):
                    n = svc.delete_files(chosen, protected=protected_files())
                    st.session_state[all_k] = False
                    st.session_state[ver_k] = st.session_state.get(ver_k, 0) + 1
                    skipped = len(chosen) - n
                    st.toast(f"{n} file dihapus" + (f" · {skipped} dilewati (sedang dipakai render)" if skipped else ""))
                    st.rerun()
            dest = b_.selectbox("Pindahkan ke", [k for k in svc.DIRS if k != kind],
                                format_func=lambda k: KIND_LABEL[k], label_visibility="collapsed")
            if c_.button("📂 Pindahkan", key=f"lib_move_{kind}", width="stretch"):
                n = svc.move_files(chosen, dest, protected=protected_files())
                st.session_state[all_k] = False
                st.session_state[ver_k] = st.session_state.get(ver_k, 0) + 1
                st.toast(f"{n} file dipindahkan ke {KIND_LABEL[dest]}")
                st.rerun()
        with st.expander("Preview & download"):
            paths = svc.list_paths(kind)
            sel = st.selectbox("File", paths, format_func=lambda p: p.name, index=None,
                               placeholder="Pilih file...", key="lib_prev")
            if sel is not None:
                mb = sel.stat().st_size / 1024 / 1024
                _srv, _tn = get_upload_server()
                fq = f"/file?kind={kind}&name={quote(sel.name)}&t={_srv.token}"
                base_url = _tn.url if _tn.running and _tn.url else None
                ext = sel.suffix.lower()
                if ext in svc.AUDIO_EXTS and mb < 200:
                    st.audio(str(sel))
                elif ext in svc.IMAGE_EXTS:
                    st.image(str(sel), width=240)
                elif ext in svc.VIDEO_EXTS and mb < 200:
                    st.video(str(sel))
                elif ext in svc.VIDEO_EXTS | svc.AUDIO_EXTS and base_url:
                    tag = "video" if ext in svc.VIDEO_EXTS else "audio"
                    st.components.v1.html(
                        f'<{tag} controls preload="metadata" style="width:100%;max-height:420px" '
                        f'src="{base_url}{fq}"></{tag}>', height=440 if tag == "video" else 60)
                else:
                    st.caption("Preview file besar butuh tunnel (buka dari tab Impor → Upload file besar).")
                if mb < 300:
                    st.download_button("⬇️ Download ke komputer", sel.read_bytes(),
                                       file_name=sel.name, key="lib_dl")
                elif base_url:
                    st.link_button(f"⬇️ Download {mb:.0f} MB (streaming, bisa dilanjut)",
                                   f"{base_url}{fq}&dl=1")
                else:
                    st.caption(f"File {mb:.0f} MB: buka tunnel di tab Impor dulu agar link download "
                               "streaming muncul (tanpa batas ukuran, tidak memakai RAM).")

# ───────────── IMPOR ─────────────
with tab_imp:
    def _cookie_text() -> str:
        """Cookies dari file upload (prioritas) atau dari kotak teks."""
        ss = st.session_state
        up_file = ss.get(f"ck_up_{ss.get('ck_up_n', 0)}")
        if up_file is not None:
            return up_file.getvalue().decode("utf-8", errors="replace")
        return ss.get("imp_cookies", "")

    def queue_downloads():
        """Callback tombol: antrekan semua link, lalu hapus link & cookies dari form."""
        ss = st.session_state
        urls_ = svc.parse_urls(ss.get("imp_raw", ""))
        if not urls_:
            return
        audio_opts = {"codec": ss.get("au_codec", "mp3"), "bitrate": ss.get("au_bitrate", "192"),
                      "embed": ss.get("au_embed", True), "thumb": ss.get("au_thumb", True)}
        box = None
        ctext = _cookie_text().strip()
        if ctext and any(svc.classify_url(u) == "web" for u in urls_):
            try:
                netscape, _, _ = svc.parse_cookies(ctext)
            except ValueError as e:
                ss["imp_msg"] = ("error", f"Cookies tidak valid: {e}")
                return
            box = svc.CookieBox(netscape)
        runner = get_runner("download", 2)
        for u in urls_:
            use = box if (box is not None and svc.classify_url(u) == "web") else None
            runner.submit(
                svc.short_title(u),
                partial(svc.run_download, url=u, hint=ss.get("imp_hint", "auto"),
                        height=ss.get("imp_height", 720), noplaylist=ss.get("imp_noplaylist", True),
                        disk_min_gb=ss.get("disk_min", 0.5), audio_opts=audio_opts, cookies=use),
                cleanup=use.make_releaser() if use is not None else None)
        ss["imp_raw"] = ""
        ss["imp_cookies"] = ""
        ss["ck_up_n"] = ss.get("ck_up_n", 0) + 1     # reset uploader cookies
        ss["imp_msg"] = ("success", f"{len(urls_)} unduhan diantrekan"
                                    + (" (dengan cookies)" if box is not None else ""))

    with st.container(border=True):
        st.subheader("🔗 Impor dari link")
        st.caption("Tempel banyak link sekaligus, dipisah koma, spasi, atau baris baru. "
                   "Jenis link dikenali otomatis: Google Drive (file/folder), YouTube dan situs "
                   "lain yang didukung yt-dlp, atau link langsung ke file.")
        flash = st.session_state.pop("imp_msg", None)
        if flash:
            (st.success if flash[0] == "success" else st.error)(flash[1])
        raw = st.text_area("Link", height=150, key="imp_raw", label_visibility="collapsed",
                           placeholder="https://drive.google.com/file/d/.../view, https://youtu.be/...")
        urls = svc.parse_urls(raw)
        o1, o2, o3 = st.columns(3)
        hint = o1.radio("Simpan sebagai", list(HINT_LABEL), format_func=HINT_LABEL.get, key="imp_hint",
                        help="Otomatis memeriksa isi file dengan ffprobe lalu memasukkannya ke folder "
                             "yang tepat. Pilih Audio untuk mengambil audio (MP3/M4A) dari link YouTube.")
        o2.selectbox("Maks. tinggi video (yt-dlp)", [360, 480, 720, 1080], index=2, key="imp_height",
                     disabled=hint == "audio")
        o3.checkbox("Abaikan playlist (1 video saja)", value=True, key="imp_noplaylist",
                    help="Matikan untuk mengunduh seluruh playlist dari link playlist.")

        with st.expander("🎧 Opsi audio (MP3 / M4A, cover, thumbnail)", expanded=(hint == "audio")):
            a1, a2 = st.columns(2)
            a1.selectbox("Format", ["mp3", "m4a"], key="au_codec",
                         format_func=lambda c: {"mp3": "MP3", "m4a": "M4A (AAC)"}[c])
            a2.selectbox("Bitrate (kbps)", ["320", "256", "192", "128"], index=2, key="au_bitrate")
            st.checkbox("Sematkan cover & metadata ke dalam file audio", value=True, key="au_embed")
            st.checkbox("Simpan thumbnail .jpg terpisah (folder Thumbnail)", value=True, key="au_thumb")
            st.caption("Berlaku saat 'Simpan sebagai' = Audio. Bitrate di atas kualitas asli YouTube tidak "
                       "menambah kualitas; 192 kbps cukup untuk kebanyakan kasus. Jika cover/thumbnail "
                       "gagal diproses, unduhan otomatis diulang dengan opsi lebih sederhana.")

        with st.expander("🍪 Cookies untuk yt-dlp (opsional)"):
            st.caption("Untuk video yang meminta login atau error \"not a bot\". Login ke YouTube di browser → "
                       "ekstensi Cookie-Editor → Export → JSON → upload file atau tempel di bawah. "
                       "Dipakai untuk semua link web/yt-dlp dalam satu batch (bulk), bukan untuk Google Drive.")
            ck_n = st.session_state.setdefault("ck_up_n", 0)
            st.file_uploader("Upload cookies (.json / .txt)", type=["json", "txt"], key=f"ck_up_{ck_n}")
            st.text_area("Atau tempel cookies JSON", height=120, key="imp_cookies",
                         placeholder="[{\"domain\": \".youtube.com\", \"name\": \"...\", \"value\": \"...\"}, ...]")
            ctext = _cookie_text().strip()
            if ctext:
                try:
                    _, n_cookie, doms = svc.parse_cookies(ctext)
                    st.success(f"{n_cookie} cookie terbaca · domain: {', '.join(doms[:6])}"
                               + (" …" if len(doms) > 6 else ""))
                except ValueError as e:
                    st.error(f"Cookies tidak valid: {e}")
            st.warning("Cookies = akses ke akunmu. Jangan dibagikan atau di-screenshot. Cookies hanya "
                       "disimpan di memori server selama batch berjalan, ditulis ke file sementara "
                       "per unduhan, lalu dihapus. Form dikosongkan otomatis setelah diantrekan. "
                       "Pakai akun cadangan dan jaga agar app ini privat.")

        if urls:
            st.dataframe(
                pd.DataFrame({"No": range(1, len(urls) + 1),
                              "Sumber": [svc.SOURCE_LABEL[svc.classify_url(u)] for u in urls],
                              "Link": [u[:90] for u in urls]}),
                hide_index=True, width="stretch")
        st.button(f"⬇️ Mulai unduh {len(urls)} link", type="primary", disabled=not urls,
                  key="imp_go", on_click=queue_downloads)

    @st.fragment(run_every=2)
    def dl_progress():
        c = dl_jobs.counts()
        total = sum(c.values())
        if not total:
            return
        done = c["selesai"] + c["gagal"]
        st.progress(done / total, text=f"{done}/{total} selesai · {c['berjalan']} berjalan · "
                                       f"{c['antre']} antre · {c['gagal']} gagal")
        jobs_table(dl_jobs.snapshot()[:30])

    dl_progress()
    r1, r2, r3 = st.columns(3)
    if r1.button("🔁 Coba ulang yang gagal", key="dl_retry"):
        st.toast(f"{dl_jobs.retry_failed()} unduhan diulang")
    if r2.button("🚫 Batalkan yang antre", key="dl_cancel"):
        st.toast(f"{dl_jobs.cancel_queued()} dibatalkan")
    if r3.button("🧽 Bersihkan riwayat", key="dl_clear"):
        dl_jobs.clear_finished()
        st.rerun()

    with st.container(border=True):
        st.subheader("📥 Upload dari komputer")
        up_n = st.session_state.setdefault("up_n", 0)
        for msg in st.session_state.pop("up_msg", []):
            st.success(msg)
        up = st.file_uploader("Pilih file apa saja (video, audio, dll.)", accept_multiple_files=True,
                              key=f"imp_up_{up_n}")
        if up and st.button(f"💾 Simpan {len(up)} file (mode: {HINT_LABEL[hint]})", key="up_go"):
            msgs = []
            for f in up:
                k, dest = svc.import_bytes(f.name, f.getbuffer(), hint)
                msgs.append(f"{dest.name} → {KIND_LABEL[k]}")
            st.session_state["up_msg"] = msgs
            st.session_state["up_n"] = up_n + 1
            st.rerun()
        st.caption("Upload lewat tombol di atas dibatasi (default 200 MB per file). Untuk file besar, "
                   "pakai upload resumable di bawah atau link Google Drive.")

    with st.expander("🚀 Upload file besar (pilih file di browser, resumable)", expanded=True):
        up_srv, up_tunnel = get_upload_server()
        if up_srv.error:
            st.error(up_srv.error)
        else:
            st.markdown("File besar dikirim per potongan langsung ke folder internal app (tanpa batas 200 MB). "
                        "Jika koneksi putus, pilih file yang sama lagi — upload **lanjut** dari bagian terakhir.")
            st.markdown("Streamlit Cloud hanya membuka satu port, jadi halaman upload dibuka lewat tunnel "
                        "publik (cloudflared, tanpa akun).")
            t1, t2 = st.columns(2)
            if t1.button("🌐 Buka tunnel upload", key="tn_open", disabled=up_tunnel.running):
                up_tunnel.start()
            if t2.button("⛔ Tutup tunnel", key="tn_close", disabled=not up_tunnel.running):
                up_tunnel.stop()

            @st.fragment(run_every=3)
            def tunnel_panel():
                if up_tunnel.error:
                    st.error(up_tunnel.error)
                if up_tunnel.running and not up_tunnel.url:
                    st.info("Menyiapkan tunnel... (beberapa detik)")
                link = f"{up_tunnel.url}/#t={up_srv.token}" if up_tunnel.url else None
                if link:
                    st.link_button("📂 Buka halaman upload (tab baru)", link, type="primary")
                    st.caption("Link ini sudah berisi token — jangan dibagikan. Di HP/komputer lain cukup "
                               "buka link yang sama.")
                else:
                    st.caption(f"Tanpa tunnel (VPS/lokal): buka `http://IP-SERVER:{up_srv.port}/#t=TOKEN`.")
                    st.caption("Token:")
                    st.code(up_srv.token, language="text")
                parts = up_srv.partials()
                if parts:
                    st.caption("Upload terjeda/sedang jalan: " + ", ".join(
                        f"{x['name']} ({x['mb']:.0f} MB)" for x in parts))

            tunnel_panel()
            if up_tunnel.url:
                st.components.v1.iframe(f"{up_tunnel.url}/#t={up_srv.token}", height=520, scrolling=True)

# ───────────── NORMALISASI & TES ─────────────
LMODE = {"two": "Presisi (2-pass, disarankan)", "one": "Cepat (1-pass)",
         "off": "Tanpa loudness (hanya samakan format)"}
OUTMODE = {"new": "File baru (*_norm), aslinya dipertahankan", "replace": "Ganti file asli (setelah lolos verifikasi)"}
DECMODE = {"none": "Tidak", "quick": "Cepat (60 dtk awal + akhir)", "full": "Penuh (lambat, paling teliti)"}


def _pick_files(prefix: str, paths: list, noun: str) -> list:
    if st.checkbox(f"Semua {noun} ({len(paths)})", value=True, key=f"mt_{prefix}_all"):
        return list(paths)
    return st.multiselect(f"Pilih {noun}", paths, format_func=svc.label, key=f"mt_{prefix}_sel")


def _report(rows: list, kind: str, key: str, tgt_audio=None) -> dict:
    S = mt.summarize(rows, kind, tgt_audio)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Diuji", S["total"])
    c2.metric("✅ Seragam & aman", S["ok"])
    c3.metric("⚠️ Tidak seragam", S["warn"])
    c4.metric("❌ Bermasalah", S["bad"])
    if S["uniform"]:
        st.success("Semua file seragam dan aman dipakai.")
    elif S["bad"]:
        st.error(f"{S['bad']} file bermasalah (rusak / terpotong / senyap). Jangan dipakai render: ganti atau hapus.")
    else:
        st.warning(f"{S['warn']} file belum seragam. Jalankan normalisasi di bawah.")
    for n in S["notes"]:
        st.info(n)
    if S["groups"]:
        st.caption("Format terdeteksi: " + " · ".join(f"{g} (×{n})" for g, n in S["groups"]))
    only = st.checkbox("Tampilkan hanya yang tidak OK", value=S["total"] > 8, key=f"mt_{key}_only")
    df = pd.DataFrame(rows)
    if only:
        df = df[df["Status"] != mt.OK]
    if len(df):
        st.dataframe(df.drop(columns=["_path"]), hide_index=True, width="stretch")
    return S


with tab_norm:
    st.caption("Samakan format semua file agar penggabungan bisa memakai mode copy (cepat) dan volume tidak naik-turun: audio → AAC .m4a "
               "(sample rate, channel, bitrate, loudness seragam), video → H.264 dengan ukuran & FPS seragam. "
               "Alur: **Tes** dulu untuk melihat mana yang tidak seragam, lalu **Normalisasi**. Hasil ditulis ke "
               "file sementara, diverifikasi, baru masuk Library.")
    n_aud, n_vid = st.tabs(["🎵 Audio (AAC / M4A)", "🎞️ Video (H.264 seragam)"])

    # ---------------- AUDIO ----------------
    with n_aud:
        apaths = svc.list_paths("audio")
        if not apaths:
            st.info("Folder audio kosong. Tambahkan file lewat tab **Impor**.")
        else:
            xf_now = float(st.session_state.get("l_xfmax", 8.0)) if st.session_state.get("l_xfon", True) else 0.0
            with st.expander("🎯 Target audio seragam", expanded=True):
                c1, c2, c3, c4 = st.columns(4)
                a_sr = c1.selectbox("Sample rate (Hz)", [44100, 48000], key="mt_a_sr",
                                    help="44100 = sama dengan jalur live (tanpa resample tambahan).")
                a_ch = c2.selectbox("Channel", [2, 1], key="mt_a_ch", format_func={2: "Stereo", 1: "Mono"}.get)
                a_br = c3.selectbox("Bitrate AAC (kbps)", [128, 160, 192, 256], index=1, key="mt_a_br")
                a_lufs = c4.number_input("Loudness target (LUFS)", -30.0, -8.0, -16.0, 0.5, key="mt_a_lufs",
                                         help="-16 LUFS = umum untuk musik streaming/YouTube. Semua lagu dibawa ke "
                                              "angka ini sehingga volume antar lagu sama.")
                d1, d2 = st.columns(2)
                a_tol = d1.number_input("Toleransi selisih loudness (LU)", 0.5, 6.0, 2.0, 0.5, key="mt_a_tol")
                a_tp = d2.number_input("Batas true-peak (dBTP)", -6.0, -0.1, -1.5, 0.1, key="mt_a_tp",
                                       help="Puncak sinyal maksimum; -1.5 memberi ruang agar tidak clipping saat di-mix.")
            tgt_a = mt.AudioTarget(sr=a_sr, ch=a_ch, bitrate_k=a_br, lufs=a_lufs, tp=a_tp, tol_lu=a_tol,
                                   min_dur=3 * xf_now)

            st.markdown("##### 🧪 Tes audio")
            sel_a = _pick_files("a", apaths, "file audio")
            t1, t2 = st.columns(2)
            do_loud = t1.checkbox("Ukur loudness (LUFS & puncak)", value=True, key="mt_a_loud")
            do_dec = t2.checkbox("Tes decode penuh (deteksi file rusak / terpotong)", value=True, key="mt_a_dec")
            if st.button(f"🧪 Tes {len(sel_a)} file audio", type="primary", key="mt_a_test", disabled=not sel_a):
                bar = st.progress(0.0, text="Menguji...")
                st.session_state["mt_a_rows"] = mt.run_tests(
                    sel_a, lambda p: mt.audio_row(p, tgt_a, do_loud, do_dec),
                    progress=lambda d, t: bar.progress(d / t, text=f"{d}/{t} file diuji"))
                bar.empty()
            rows_a = st.session_state.get("mt_a_rows") or []
            if rows_a:
                _report(rows_a, "audio", "a", tgt_a)
                st.caption("Hasil tes dihitung pada target saat tes dijalankan. Setelah normalisasi selesai, "
                           "jalankan tes lagi untuk memastikan.")

            st.markdown("##### 🔧 Normalisasi audio")
            n1, n2 = st.columns(2)
            a_lmode = n1.selectbox("Loudness", list(LMODE), format_func=LMODE.get, key="mt_a_lmode")
            a_out = n2.selectbox("Hasil", list(OUTMODE), format_func=OUTMODE.get, key="mt_a_out")
            n3, n4 = st.columns(2)
            a_skip = n3.checkbox("Lewati file yang sudah seragam", value=True, key="mt_a_skip")
            a_strip = n4.checkbox("Buang metadata & cover", value=False, key="mt_a_strip")
            fix_a = [r["_path"] for r in rows_a if r["Status"] == mt.WARN]

            def _submit_a(paths: list) -> None:
                opts = {"mode": a_out, "loud": a_lmode, "skip_ok": a_skip, "strip_meta": a_strip}
                n = mt.submit_normalize(jobs, "audio", paths, tgt_a, opts, disk_min, protected_files)
                st.toast(f"{n} job normalisasi audio diantrekan")

            b1, b2 = st.columns(2)
            if b1.button(f"🔧 Normalisasi {len(fix_a)} file yang tidak seragam", type="primary",
                         key="mt_a_fix", disabled=not fix_a, width="stretch"):
                _submit_a(fix_a)
            if b2.button(f"🔧 Normalisasi semua {len(sel_a)} file terpilih", key="mt_a_fixall",
                         disabled=not sel_a, width="stretch"):
                _submit_a(sel_a)

    # ---------------- VIDEO ----------------
    with n_vid:
        vpaths = svc.library_videos()
        if not vpaths:
            st.info("Belum ada video. Tambahkan file lewat tab **Impor**.")
        else:
            with st.expander("🎯 Target video seragam", expanded=True):
                c1, c2, c3 = st.columns(3)
                v_h = c1.selectbox("Resolusi", [480, 720, 1080], index=1, key="mt_v_h",
                                   format_func=lambda h: f"{h}p ({svc.RES[h][0]}x{svc.RES[h][1]})")
                v_fps = c2.selectbox("FPS", [24, 25, 30], index=2, key="mt_v_fps")
                v_br = c3.selectbox("Bitrate video (kbps)", [0, 1500, 2500, 4000, 6000], key="mt_v_br",
                                    format_func=lambda b: "Auto (sesuai resolusi)" if b == 0 else str(b))
                d1, d2, d3 = st.columns(3)
                v_aud = d1.radio("Audio di dalam video", ["keep", "drop"], horizontal=True, key="mt_v_aud",
                                 format_func={"keep": "Pertahankan (AAC)", "drop": "Hapus"}.get,
                                 help="Live memakai audio dari panel audio, jadi audio video tidak terpakai. "
                                      "Menghapusnya menghemat disk.")
                v_pre = d2.selectbox("Kecepatan encode", ["superfast", "veryfast", "faster", "medium"], index=1,
                                     key="mt_v_pre", help="Lebih lambat = file lebih kecil / kualitas lebih baik.")
                v_thr = d3.selectbox("Thread CPU", [0, 1, 2, 4], index=2, key="mt_v_thr",
                                     format_func=lambda n: "Otomatis" if n == 0 else str(n),
                                     help="Batasi agar live yang sedang berjalan tidak tersendat.")
            tgt_v = mt.VideoTarget(height=v_h, fps=v_fps, vbr_k=v_br, keep_audio=v_aud == "keep", preset=v_pre)
            st.caption(f"Target: H.264 High · yuv420p · {tgt_v.size[0]}x{tgt_v.size[1]} · {v_fps} fps · "
                       f"{tgt_v.bitrate} kbps · keyframe tiap 2 detik → langsung bisa dikirim tanpa re-encode.")

            st.markdown("##### 🧪 Tes video")
            sel_v = _pick_files("v", vpaths, "video")
            t1, t2 = st.columns(2)
            do_gop = t1.checkbox("Ukur jarak keyframe", value=True, key="mt_v_gop",
                                 help="YouTube Live butuh keyframe ≤ 4 detik.")
            do_vdec = t2.selectbox("Tes decode (deteksi file rusak / terpotong)", list(DECMODE), index=1,
                                   format_func=DECMODE.get, key="mt_v_dec")
            if st.button(f"🧪 Tes {len(sel_v)} file video", type="primary", key="mt_v_test", disabled=not sel_v):
                bar = st.progress(0.0, text="Menguji...")
                st.session_state["mt_v_rows"] = mt.run_tests(
                    sel_v, lambda p: mt.video_row(p, tgt_v, do_gop, do_vdec),
                    progress=lambda d, t: bar.progress(d / t, text=f"{d}/{t} file diuji"))
                bar.empty()
            rows_v = st.session_state.get("mt_v_rows") or []
            if rows_v:
                S_v = _report(rows_v, "video", "v")
                st.caption(f"{S_v['ok']} dari {S_v['total']} video siap dikirim apa adanya saat live (CPU ≈ 0). "
                           "Hasil tes dihitung pada target saat tes dijalankan.")

            st.markdown("##### 🔧 Normalisasi video")
            v_out = st.selectbox("Hasil", list(OUTMODE), format_func=OUTMODE.get, key="mt_v_out")
            v_skip = st.checkbox("Lewati file yang sudah seragam", value=True, key="mt_v_skip")
            fix_v = [r["_path"] for r in rows_v if r["Status"] == mt.WARN]

            def _submit_v(paths: list) -> None:
                opts = {"mode": v_out, "skip_ok": v_skip, "threads": v_thr}
                n = mt.submit_normalize(jobs, "video", paths, tgt_v, opts, disk_min, protected_files)
                st.toast(f"{n} job normalisasi video diantrekan")

            b1, b2 = st.columns(2)
            if b1.button(f"🔧 Normalisasi {len(fix_v)} video yang tidak seragam", type="primary",
                         key="mt_v_fix", disabled=not fix_v, width="stretch"):
                _submit_v(fix_v)
            if b2.button(f"🔧 Normalisasi semua {len(sel_v)} video terpilih", key="mt_v_fixall",
                         disabled=not sel_v, width="stretch"):
                _submit_v(sel_v)

    @st.fragment(run_every=3)
    def norm_status():
        rows_ = [r for r in jobs.snapshot() if str(r["Job"]).startswith("Normalisasi")]
        if rows_:
            st.markdown("##### 📋 Antrean normalisasi")
            jobs_table(rows_[:30])
            st.caption("Diproses satu per satu. Job yang berjalan tidak bisa dibatalkan; yang antre bisa "
                       "dibatalkan dari tab Job.")

    norm_status()

with tab_jobs:
    b1, b2, b3 = st.columns(3)
    if b1.button("🔁 Coba ulang yang gagal", key="pj_retry"):
        st.toast(f"{jobs.retry_failed() + dl_jobs.retry_failed()} job diulang")
    if b2.button("🚫 Batalkan yang antre", key="pj_cancel"):
        st.toast(f"{jobs.cancel_queued() + dl_jobs.cancel_queued()} job dibatalkan")
    if b3.button("🧽 Bersihkan riwayat", key="pj_clear"):
        jobs.clear_finished()
        dl_jobs.clear_finished()
        st.rerun()

    @st.fragment(run_every=3)
    def jobs_panel():
        rm = jobs.snapshot()
        if rm:                                   
            st.markdown("**Render & Normalisasi**")
            jobs_table(rm)
        st.markdown("**Impor / Unduhan**")
        jobs_table(dl_jobs.snapshot())

    jobs_panel()
    st.caption("Job yang sedang berjalan tidak bisa dibatalkan. Hasil unduhan muncul di Library.")
