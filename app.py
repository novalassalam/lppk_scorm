"""
Web app: Video → SCORM 1.2 / 2004 (Moodle 5)
Jalankan lokal :  .venv/bin/python app.py   lalu buka http://127.0.0.1:8000
Hosting        :  lihat README.md (WAJIB 1 worker uvicorn)

Pengaturan lewat environment variable:
  HOST, PORT            default 127.0.0.1 : 8000
  MAX_UPLOAD_MB         batas ukuran upload video (default 2048)
  MAX_CONCURRENT_JOBS   jumlah konversi ffmpeg bersamaan (default 1, sisanya antre)
  RETENTION_DAYS        project tidak aktif lebih dari N hari dihapus otomatis (default 7, 0 = tidak)
"""

import json, os, re, shutil, threading, time, uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal, Optional, Union

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator

import scorm_builder as sb

BASE_DIR     = Path(__file__).resolve().parent
PROJECTS_DIR = BASE_DIR / 'projects'
PROJECTS_DIR.mkdir(exist_ok=True)

VIDEO_EXTS = {'.mp4', '.m4v', '.mov', '.mkv', '.webm', '.avi'}
SUB_EXTS   = {'.srt', '.vtt', '.sbv'}

MAX_UPLOAD_BYTES = int(os.environ.get('MAX_UPLOAD_MB', 2048)) * 1024 * 1024
MAX_SUB_BYTES    = 5 * 1024 * 1024
RETENTION_DAYS   = float(os.environ.get('RETENTION_DAYS', 7))
JOB_SLOTS        = threading.Semaphore(int(os.environ.get('MAX_CONCURRENT_JOBS', 1)))

PID_RE = re.compile(r'[a-f0-9]{12}')
JID_RE = re.compile(r'([a-f0-9]{12})-[a-f0-9]{6}')


# ── Lock per project (satu proses uvicorn, banyak thread) ────────────
_locks, _locks_guard = {}, threading.Lock()

def project_lock(pid: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(pid, threading.Lock())


# ── Models ────────────────────────────────────────────────────────────
class Question(BaseModel):
    time    : str
    type    : Optional[Literal['single', 'multiple', 'truefalse']] = None
    question: str = Field(min_length=1)
    options : list[str] = Field(default_factory=list)
    correct : Union[int, list[int]]
    bobot   : float = Field(ge=0)

    @field_validator('time')
    @classmethod
    def _time(cls, v):
        v = v.strip()
        if not re.fullmatch(r'(\d+:)?\d{1,2}:\d{2}', v):
            raise ValueError('format waktu harus MM:SS atau HH:MM:SS')
        return v

    @model_validator(mode='after')
    def _check(self):
        self.type, self.options, clist = sb.check_question(self.model_dump())
        self.correct = clist if self.type == 'multiple' else clist[0]
        return self

    def to_json(self):
        b = self.bobot
        return {'time': self.time, 'type': self.type, 'question': self.question.strip(),
                'options': self.options, 'correct': self.correct,
                'bobot': int(b) if float(b).is_integer() else b}


class GenerateReq(BaseModel):
    title     : str = Field(min_length=1, max_length=200)
    resolution: str = 'original'
    passing   : float = Field(70, ge=0, le=100)
    version   : str = '1.2'


# ── Helpers ───────────────────────────────────────────────────────────
def project_dir(pid: str) -> Path:
    if not PID_RE.fullmatch(pid):
        raise HTTPException(400, 'project id tidak valid')
    d = PROJECTS_DIR / pid
    if not d.is_dir():
        raise HTTPException(404, 'project tidak ditemukan (mungkin sudah dihapus otomatis)')
    return d

def read_json(f: Path, default):
    try:
        return json.loads(f.read_text(encoding='utf-8'))
    except (FileNotFoundError, json.JSONDecodeError):
        return default

def read_meta(d: Path) -> dict:
    return read_json(d / 'meta.json', {})

def write_meta(d: Path, **kw):
    with project_lock(d.name):
        m = read_meta(d); m.update(kw)
        sb.write_json_atomic(d / 'meta.json', m)
    return m

def read_job(d: Path) -> Optional[dict]:
    return read_json(d / 'job.json', None)

def write_job(d: Path, job: dict):
    sb.write_json_atomic(d / 'job.json', job)

def job_busy(d: Path) -> bool:
    j = read_job(d)
    return bool(j and j['status'] in ('queued', 'running'))

def find_video(d: Path) -> Optional[Path]:
    return next((p for p in d.glob('source.*') if p.suffix.lower() in VIDEO_EXTS), None)

async def save_stream(request: Request, dest: Path, limit: int):
    declared = int(request.headers.get('content-length') or 0)
    if declared > limit:
        raise HTTPException(413, f'File terlalu besar (maks {limit // 1048576} MB)')
    tmp, size = dest.with_name(f'.{dest.name}.{uuid.uuid4().hex[:6]}.part'), 0
    try:
        with open(tmp, 'wb') as f:
            async for chunk in request.stream():
                size += len(chunk)
                if size > limit:
                    raise HTTPException(413, f'File terlalu besar (maks {limit // 1048576} MB)')
                f.write(chunk)
        tmp.replace(dest)
    finally:
        tmp.unlink(missing_ok=True)


# ── Pembersihan otomatis & pemulihan saat start ──────────────────────
def last_activity(d: Path) -> float:
    return max((p.stat().st_mtime for p in d.iterdir()), default=d.stat().st_mtime)

def cleanup_once():
    if RETENTION_DAYS <= 0:
        return
    limit = time.time() - RETENTION_DAYS * 86400
    for d in PROJECTS_DIR.iterdir():
        if d.is_dir() and PID_RE.fullmatch(d.name) and not job_busy(d) and last_activity(d) < limit:
            shutil.rmtree(d, ignore_errors=True)

def cleanup_loop():
    while True:
        try: cleanup_once()
        except Exception: pass
        time.sleep(3600)

def recover_jobs():
    """Job yang tercatat masih berjalan saat server mati → tandai gagal."""
    for d in PROJECTS_DIR.iterdir():
        j = read_job(d) if d.is_dir() else None
        if j and j['status'] in ('queued', 'running'):
            j.update(status='error', error='Server sempat restart, silakan klik Generate lagi.')
            write_job(d, j)

@asynccontextmanager
async def lifespan(_app):
    recover_jobs()
    threading.Thread(target=cleanup_loop, daemon=True).start()
    yield

app = FastAPI(title='Video → SCORM', lifespan=lifespan)


# ── Pages ─────────────────────────────────────────────────────────────
@app.get('/')
def index():
    return FileResponse(BASE_DIR / 'templates' / 'index.html')


# ── Projects ──────────────────────────────────────────────────────────
@app.post('/api/projects')
def create_project():
    pid = uuid.uuid4().hex[:12]
    d = PROJECTS_DIR / pid; d.mkdir()
    sb.write_json_atomic(d / 'questions.json', [])
    write_meta(d, title='', resolution='720', passing=70)
    return {'id': pid}

@app.get('/api/projects/{pid}')
def get_project(pid: str):
    d = project_dir(pid)
    return {'id': pid, 'meta': read_meta(d),
            'questions': read_json(d / 'questions.json', []),
            'has_video': find_video(d) is not None,
            'has_subtitle': (d / 'subtitle.vtt').exists(),
            'resolutions': [[k, v['label']] for k, v in sb.RESOLUTIONS.items()],
            'question_types': sb.QUESTION_TYPES,
            'max_upload_mb': MAX_UPLOAD_BYTES // 1048576,
            'retention_days': RETENTION_DAYS}


# ── Upload ────────────────────────────────────────────────────────────
@app.put('/api/projects/{pid}/video')
async def upload_video(pid: str, filename: str, request: Request):
    d, ext = project_dir(pid), Path(filename).suffix.lower()
    if ext not in VIDEO_EXTS:
        raise HTTPException(400, f'Format video tidak didukung: {ext}')
    if job_busy(d):
        raise HTTPException(409, 'Tunggu proses generate selesai sebelum mengganti video')
    tmp = d / f'.upload{ext}'
    await save_stream(request, tmp, MAX_UPLOAD_BYTES)
    if sb.probe_duration(tmp) is None:
        tmp.unlink(missing_ok=True)
        raise HTTPException(400, 'File bukan video yang valid')
    with project_lock(pid):
        old = find_video(d)
        if old: old.unlink()
        dest = tmp.replace(d / f'source{ext}')
    meta = write_meta(d, video_name=filename, video_size=dest.stat().st_size,
                      duration=sb.probe_duration(dest))
    return {'ok': True, 'meta': meta}

@app.put('/api/projects/{pid}/subtitle')
async def upload_subtitle(pid: str, filename: str, request: Request):
    d, ext = project_dir(pid), Path(filename).suffix.lower()
    if ext not in SUB_EXTS:
        raise HTTPException(400, f'Format subtitle tidak didukung: {ext}')
    body = await request.body()
    if len(body) > MAX_SUB_BYTES:
        raise HTTPException(413, 'File subtitle terlalu besar')
    vtt = sb.subtitle_to_vtt(body.decode('utf-8-sig', errors='replace'), ext)
    with project_lock(pid):
        tmp = d / '.subtitle.vtt.tmp'
        tmp.write_text(vtt, encoding='utf-8'); tmp.replace(d / 'subtitle.vtt')
    return {'ok': True, 'meta': write_meta(d, subtitle_name=filename)}

@app.delete('/api/projects/{pid}/subtitle')
def delete_subtitle(pid: str):
    d = project_dir(pid)
    with project_lock(pid):
        (d / 'subtitle.vtt').unlink(missing_ok=True)
    write_meta(d, subtitle_name=None)
    return {'ok': True}


# ── Questions (disimpan ke questions.json) ────────────────────────────
@app.get('/api/projects/{pid}/questions')
def get_questions(pid: str, download: bool = False):
    f = project_dir(pid) / 'questions.json'
    if download:
        return FileResponse(f, filename='questions.json', media_type='application/json')
    return read_json(f, [])

@app.put('/api/projects/{pid}/questions')
def save_questions(pid: str, questions: list[Question]):
    d = project_dir(pid)
    data = [q.to_json() for q in questions]
    with project_lock(pid):
        sb.write_json_atomic(d / 'questions.json', data)
    return {'ok': True, 'count': len(data)}


# ── Generate SCORM (antre lewat semaphore, status di job.json) ────────
def run_job(d: Path, job: dict, req: GenerateReq):
    def update(**kw):
        job.update(kw); write_job(d, job)
    try:
        with JOB_SLOTS:
            update(status='running', stage='Memulai…')
            src = find_video(d)
            out_dir = d / 'output'; out_dir.mkdir(exist_ok=True)

            if req.resolution == 'original' and src.suffix.lower() == '.mp4':
                final = src
            else:
                update(stage='Konversi video (ffmpeg)…')
                last = [0.0]
                def progress(p):
                    if p - last[0] >= 1 or p >= 99:      # batasi tulis file progres
                        last[0] = p; update(progress=round(p * 0.9, 1))
                final = sb.transcode(src, out_dir / 'video_converted.mp4', req.resolution, progress)

            update(stage='Membaca questions.json & membuat paket SCORM…', progress=92)
            with project_lock(d.name):
                questions = sb.load_questions(d / 'questions.json')
                sub_file  = d / 'subtitle.vtt'
                subtitle  = sub_file.read_text(encoding='utf-8') if sub_file.exists() else None

            zip_name = f'{sb.slugify(req.title)}_scorm{req.version.replace(".", "")}.zip'
            for old in out_dir.glob('*.zip'): old.unlink()
            sb.build_package(final, out_dir / zip_name, req.title, questions, subtitle,
                             req.passing, req.version)
            if final != src: final.unlink(missing_ok=True)
            update(status='done', progress=100, stage='Selesai', file=zip_name,
                   size=(out_dir / zip_name).stat().st_size, questions=len(questions))
    except Exception as e:
        update(status='error', error=str(e))

@app.post('/api/projects/{pid}/generate')
def generate(pid: str, req: GenerateReq):
    d = project_dir(pid)
    if not find_video(d):
        raise HTTPException(400, 'Upload video terlebih dahulu')
    if req.resolution not in sb.RESOLUTIONS:
        raise HTTPException(400, 'Resolusi tidak valid')
    if req.version not in sb.SCORM_VERSIONS:
        raise HTTPException(400, 'Versi SCORM tidak valid')
    with project_lock(pid):
        if job_busy(d):
            raise HTTPException(409, 'Proses generate masih berjalan')
        job = {'id': f'{pid}-{uuid.uuid4().hex[:6]}', 'pid': pid, 'status': 'queued',
               'progress': 0, 'stage': 'Menunggu antrean konversi…'}
        write_job(d, job)
    write_meta(d, title=req.title, resolution=req.resolution, passing=req.passing,
               version=req.version)
    threading.Thread(target=run_job, args=(d, job, req), daemon=True).start()
    return {'job': job['id']}

def job_for(jid: str):
    m = JID_RE.fullmatch(jid)
    job = read_job(project_dir(m.group(1))) if m else None
    if not job or job['id'] != jid:
        raise HTTPException(404, 'job tidak ditemukan')
    return job

@app.get('/api/jobs/{jid}')
def job_status(jid: str):
    return job_for(jid)

@app.get('/api/jobs/{jid}/download')
def job_download(jid: str):
    job = job_for(jid)
    if job['status'] != 'done': raise HTTPException(404, 'file belum siap')
    f = PROJECTS_DIR / job['pid'] / 'output' / job['file']
    if not f.exists(): raise HTTPException(404, 'file sudah dihapus, silakan generate ulang')
    return FileResponse(f, filename=job['file'], media_type='application/zip')


@app.exception_handler(ValueError)
def value_error(_, exc):
    return JSONResponse(status_code=400, content={'detail': str(exc)})


if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host=os.environ.get('HOST', '127.0.0.1'), port=int(os.environ.get('PORT', 8000)))
