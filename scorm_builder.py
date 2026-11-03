"""
Inti konversi Video → SCORM 1.2 / 2004 4th Edition (kompatibel Moodle 4.x / 5.x).

Dipakai oleh web app (app.py) dan CLI (mp4_to_scorm.py).
"""

import json, re, shutil, subprocess, uuid, zipfile
from html import escape as html_escape
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

BASE_DIR        = Path(__file__).resolve().parent
PLAYER_TEMPLATE = BASE_DIR / 'templates' / 'scorm_player.html'

# ── Resolusi ──────────────────────────────────────────────────────────
RESOLUTIONS = {
    'original': {'label': 'Original (tanpa konversi)'},
    '1080': {'label': '1080p (Full HD)', 'height': 1080, 'crf': 23, 'audio': '160k'},
    '720' : {'label': '720p (HD)',       'height': 720,  'crf': 23, 'audio': '128k'},
    '480' : {'label': '480p (SD)',       'height': 480,  'crf': 26, 'audio': '96k'},
    '360' : {'label': '360p (Kecil)',    'height': 360,  'crf': 28, 'audio': '64k'},
}


# ── Subtitle converters ───────────────────────────────────────────────
def srt_to_vtt(text):
    text = text.replace('\r\n', '\n').strip()
    text = re.sub(r'(\d{2}:\d{2}:\d{2}),(\d{3})', r'\1.\2', text)
    text = re.sub(r'^\d+\s*\n(?=\d{2}:\d{2})', '', text, flags=re.MULTILINE)
    return "WEBVTT\n\n" + text + "\n"

def sbv_to_vtt(text):
    lines = text.replace('\r\n', '\n').strip().splitlines()
    out, i = [], 0
    def fix(t):
        p = t.split(':')
        return f"00:{p[0].zfill(2)}:{p[1]}" if len(p) == 2 else f"{p[0].zfill(2)}:{p[1]}:{p[2]}"
    while i < len(lines):
        m = re.match(r'^(\d+:\d{2}(?::\d{2})?\.\d+),(\d+:\d{2}(?::\d{2})?\.\d+)$', lines[i].strip())
        i += 1
        if not m:
            continue
        out.append(f"{fix(m.group(1))} --> {fix(m.group(2))}")
        while i < len(lines) and lines[i].strip():
            out.append(lines[i]); i += 1
        out.append("")
    return "WEBVTT\n\n" + "\n".join(out)

def subtitle_to_vtt(content, ext):
    """Konversi isi subtitle (.srt/.sbv/.vtt) ke WebVTT. Raise ValueError jika format tak dikenal."""
    ext = ext.lower()
    if ext == '.srt': return srt_to_vtt(content)
    if ext == '.sbv': return sbv_to_vtt(content)
    if ext == '.vtt':
        content = content.replace('\r\n', '\n')
        return content if content.lstrip('﻿').startswith('WEBVTT') else "WEBVTT\n\n" + content
    raise ValueError(f"Format subtitle tidak didukung: {ext} (pakai .srt, .vtt, atau .sbv)")


# ── Time helpers ──────────────────────────────────────────────────────
def parse_time(t):
    """'MM:SS' / 'HH:MM:SS' / detik → detik (float). Raise ValueError jika tidak valid."""
    t = str(t).strip(); p = t.split(':')
    if len(p) == 1: return float(t)
    if len(p) == 2: return int(p[0]) * 60 + float(p[1])
    if len(p) == 3: return int(p[0]) * 3600 + int(p[1]) * 60 + float(p[2])
    raise ValueError(f"Format waktu tidak valid: {t}")

def fmt_time(s):
    m, s = divmod(int(s), 60); h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


# ── Questions ─────────────────────────────────────────────────────────
QUESTION_TYPES = {
    'single'   : 'Pilihan ganda (1 jawaban)',
    'multiple' : 'Jawaban ganda (bisa lebih dari 1)',
    'truefalse': 'Benar / Salah',
}
TRUEFALSE_OPTIONS = ['Benar', 'Salah']

def check_question(q):
    """
    Validasi & rapikan satu soal. Return (type, options, correct_list). Raise ValueError.
      type    : single | multiple | truefalse  (tanpa 'type' → ditebak dari 'correct')
      correct : index (angka) atau list index, mis. [0, 1] = A dan B
    """
    correct = q.get('correct')
    clist = correct if isinstance(correct, list) else [correct]
    try:
        clist = sorted({int(c) for c in clist})
    except (TypeError, ValueError):
        raise ValueError('jawaban benar harus berupa index angka (0 = A, 1 = B, …)')
    qtype = q.get('type') or ('multiple' if len(clist) > 1 else 'single')
    if qtype not in QUESTION_TYPES:
        raise ValueError(f'tipe soal tidak dikenal: {qtype}')
    if qtype == 'truefalse':
        options = TRUEFALSE_OPTIONS[:]
    else:
        options = [str(o).strip() for o in (q.get('options') or [])]
    if len(options) < 2:
        raise ValueError('minimal 2 opsi jawaban')
    if any(not o for o in options):
        raise ValueError('opsi jawaban tidak boleh kosong')
    if not clist:
        raise ValueError('pilih minimal 1 jawaban benar')
    if any(not 0 <= c < len(options) for c in clist):
        raise ValueError('index jawaban benar di luar jangkauan')
    if qtype != 'multiple' and len(clist) != 1:
        raise ValueError('soal pilihan ganda / benar-salah hanya boleh punya 1 jawaban benar')
    return qtype, options, clist

def load_questions(path):
    """Baca questions.json (format: time, type, question, options, correct, bobot). Kosong jika tidak ada."""
    path = Path(path) if path else None
    if not path or not path.exists():
        return []
    raw = json.loads(path.read_text(encoding='utf-8') or '[]')
    return normalize_questions(raw)

def normalize_questions(raw):
    """Untuk player: waktu dalam detik, 'correct' selalu list index."""
    questions = []
    for q in raw:
        t = parse_time(q['time'])
        qtype, options, clist = check_question(q)
        questions.append({
            'time'    : t,
            'timeFmt' : fmt_time(t),
            'type'    : qtype,
            'question': q['question'],
            'options' : options,
            'correct' : clist,
            'bobot'   : float(q.get('bobot', 100 / len(raw))),
        })
    questions.sort(key=lambda x: x['time'])
    return questions


# ── ffmpeg ────────────────────────────────────────────────────────────
def probe_duration(path):
    if not shutil.which('ffprobe'):
        return None
    r = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                        '-of', 'default=nw=1:nk=1', str(path)], capture_output=True, text=True)
    try:    return float(r.stdout.strip())
    except ValueError: return None

def transcode(inp, out, resolution, on_progress=None):
    """Transcode ke MP4 H.264/AAC. resolution: kunci RESOLUTIONS. on_progress(persen 0-100)."""
    if not shutil.which('ffmpeg'):
        raise RuntimeError('ffmpeg tidak ditemukan di PATH')
    preset = RESOLUTIONS.get(resolution) or RESOLUTIONS['720']
    cmd = ['ffmpeg', '-y', '-i', str(inp)]
    if 'height' in preset:
        # tidak upscale: tinggi = min(target, tinggi asli)
        cmd += ['-vf', f"scale=-2:'min({preset['height']},ih)'"]
    cmd += ['-c:v', 'libx264', '-preset', 'fast', '-crf', str(preset.get('crf', 23)),
            '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', preset.get('audio', '128k'),
            '-movflags', '+faststart', '-map_metadata', '-1',
            '-progress', 'pipe:1', '-nostats', str(out)]

    duration = probe_duration(inp) or 0
    log_path = Path(out).with_suffix('.ffmpeg.log')
    with open(log_path, 'w') as log:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=log, text=True)
        for line in proc.stdout:
            if on_progress and duration and line.startswith('out_time_us='):
                try:
                    us = int(line.split('=', 1)[1])
                    on_progress(min(99.0, us / 1e6 / duration * 100))
                except ValueError:
                    pass
        proc.wait()
    if proc.returncode != 0:
        tail = log_path.read_text(errors='replace')[-800:]
        raise RuntimeError(f'ffmpeg gagal:\n{tail}')
    log_path.unlink(missing_ok=True)
    if on_progress: on_progress(100.0)
    return Path(out)


# ── imsmanifest.xml ───────────────────────────────────────────────────
SCORM_VERSIONS = {
    '1.2' : 'SCORM 1.2 (laporan Moodle paling lengkap)',
    '2004': 'SCORM 2004 4th Edition',
}

def write_json_atomic(path, data):
    """Tulis JSON lewat file sementara + rename, agar pembaca tidak pernah melihat file setengah jadi."""
    path = Path(path)
    tmp = path.with_name(f'.{path.name}.{uuid.uuid4().hex[:6]}.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(path)


def make_manifest(title, files, version='1.2'):
    t   = xml_escape(title)
    uid = uuid.uuid4().hex[:12].upper()
    file_tags = "\n      ".join(f'<file href="{xml_escape(f)}"/>' for f in files)
    if version == '1.2':
        head = """<manifest identifier="MANIFEST-{uid}" version="1.0"
  xmlns="http://www.imsproject.org/xsd/imscp_rootv1p1p2"
  xmlns:adlcp="http://www.adlnet.org/xsd/adlcp_rootv1p2"
  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
  xsi:schemaLocation="http://www.imsproject.org/xsd/imscp_rootv1p1p2 imscp_rootv1p1p2.xsd
    http://www.imsglobal.org/xsd/imsmd_rootv1p2p1 imsmd_rootv1p2p1.xsd
    http://www.adlnet.org/xsd/adlcp_rootv1p2 adlcp_rootv1p2.xsd">
  <metadata>
    <schema>ADL SCORM</schema>
    <schemaversion>1.2</schemaversion>
  </metadata>""".format(uid=uid)
        scotype = 'adlcp:scormtype="sco"'
    else:
        head = """<manifest identifier="MANIFEST-{uid}" version="1"
  xmlns="http://www.imsglobal.org/xsd/imscp_v1p1"
  xmlns:adlcp="http://www.adlnet.org/xsd/adlcp_v1p3"
  xmlns:adlseq="http://www.adlnet.org/xsd/adlseq_v1p3"
  xmlns:adlnav="http://www.adlnet.org/xsd/adlnav_v1p3"
  xmlns:imsss="http://www.imsglobal.org/xsd/imsss"
  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
  xsi:schemaLocation="http://www.imsglobal.org/xsd/imscp_v1p1 imscp_v1p1.xsd
    http://www.adlnet.org/xsd/adlcp_v1p3 adlcp_v1p3.xsd
    http://www.adlnet.org/xsd/adlseq_v1p3 adlseq_v1p3.xsd
    http://www.adlnet.org/xsd/adlnav_v1p3 adlnav_v1p3.xsd
    http://www.imsglobal.org/xsd/imsss imsss_v1p0.xsd">
  <metadata>
    <schema>ADL SCORM</schema>
    <schemaversion>2004 4th Edition</schemaversion>
  </metadata>""".format(uid=uid)
        scotype = 'adlcp:scormType="sco"'
    return f"""<?xml version="1.0" encoding="UTF-8"?>
{head}
  <organizations default="ORG-{uid}">
    <organization identifier="ORG-{uid}">
      <title>{t}</title>
      <item identifier="ITEM-{uid}" identifierref="RES-{uid}">
        <title>{t}</title>
      </item>
    </organization>
  </organizations>
  <resources>
    <resource identifier="RES-{uid}" type="webcontent" {scotype} href="index.html">
      {file_tags}
    </resource>
  </resources>
</manifest>
"""


# ── index.html (player) ───────────────────────────────────────────────
def make_html(video_filename, title, questions, subtitle_file=None, passing_score=70,
              version='1.2'):
    qs_json = json.dumps(questions, ensure_ascii=False).replace('</', '<\\/')
    note    = f"+ {len(questions)} soal" if questions else "tanpa quiz"
    sub_track = (f'<track kind="subtitles" src="{html_escape(subtitle_file)}" '
                 f'srclang="id" label="Subtitle" default>') if subtitle_file else ''
    sub_btn   = ('<button class="btn" id="btnSub" title="Subtitle (CC)" '
                 'style="font-size:11px;width:auto;padding:0 10px">CC</button>') if subtitle_file else ''
    html = PLAYER_TEMPLATE.read_text(encoding='utf-8')
    for k, v in {
        '__TITLE__'         : html_escape(title),
        '__QUIZ_NOTE__'     : note,
        '__VIDEO__'         : html_escape(video_filename),
        '__SUB_TRACK__'     : sub_track,
        '__SUB_BTN__'       : sub_btn,
        '__QUESTIONS_JSON__': qs_json,
        '__PASSING__'       : str(float(passing_score)),
        '__SCORM_VERSION__' : version if version in SCORM_VERSIONS else '1.2',
    }.items():
        html = html.replace(k, v)
    return html


# ── Package ───────────────────────────────────────────────────────────
def slugify(s):
    s = re.sub(r'[^A-Za-z0-9]+', '_', s).strip('_')
    return s[:60] or 'video'

def build_package(video_path, output_zip, title, questions=None,
                  subtitle_vtt=None, passing_score=70, version='1.2'):
    """
    Rakit zip SCORM 2004.
      video_path   : file MP4 yang sudah final
      questions    : list hasil normalize_questions()
      subtitle_vtt : isi WebVTT (string) atau None
      version      : '1.2' atau '2004'
    """
    questions = questions or []
    video_name, sub_name = 'video.mp4', ('subtitle.vtt' if subtitle_vtt else None)
    files = ['index.html', video_name] + ([sub_name] if sub_name else [])
    with zipfile.ZipFile(output_zip, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr('imsmanifest.xml', make_manifest(title, files, version))
        zf.writestr('index.html', make_html(video_name, title, questions, sub_name, passing_score, version))
        if sub_name:
            zf.writestr(sub_name, subtitle_vtt)
        zf.write(video_path, video_name, compress_type=zipfile.ZIP_STORED)
    return Path(output_zip)
