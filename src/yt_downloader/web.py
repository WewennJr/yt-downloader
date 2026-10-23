import atexit
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import webbrowser

import static_ffmpeg
import yt_dlp
from flask import Flask, render_template, request, send_file, jsonify

app = Flask(__name__)

TASK_TTL = 3600  # seconds before a task and its files are deleted
SILENCE_DB = -80  # below this level, the audio track is considered silent

# {task_id: {'status': 'downloading'|'ready'|'error', 'progress': int, 'created': float, ...}}
download_tasks = {}
# {(url, audio_lang): task_id} -> avoids downloading the same video again
task_index = {}
tasks_lock = threading.Lock()

LANG_RE = re.compile(r'^[A-Za-z0-9_-]{1,20}$')

YDL_OPTS_BASE = {
    'quiet': True,
    'no_warnings': True,
    'extract_flat': False,
    'noplaylist': True,
}

# ffmpeg is an external program: static-ffmpeg downloads it on the first launch
# and adds it to the process PATH (doing nothing if ffmpeg is already in the PATH).
static_ffmpeg.add_paths(weak=True)

# Best video + AAC audio (m4a) as the first choice: playable everywhere,
# then any audio, then the best combined format.
FORMAT_SELECTOR = 'bv*+ba[acodec^=mp4a]/bv*+ba/b'
# "res:1080"   => resolution closest to 1080p without exceeding it (maximum available if lower)
# "fps"        => at the same resolution, the highest fps (60 > 30)
# h264/aac     => guaranteed browser playback (mp4)
# "channels:2" => STEREO: without this, yt-dlp prefers the 5.1 track (6 channels), often silent on PCs
# Without a language choice, the original audio is prioritized by yt-dlp's default sorting ("lang").
FORMAT_SORT = ['res:1080', 'fps', 'vcodec:h264', 'acodec:aac', 'channels:2']


def build_format_selector(audio_lang=''):
    """Format selector with a specific audio language if requested (otherwise the original)."""
    if not audio_lang:
        return FORMAT_SELECTOR
    return (f'bv*+ba[language={audio_lang}][acodec^=mp4a]/'
            f'bv*+ba[language={audio_lang}]/' + FORMAT_SELECTOR)


def clean_error(e):
    """Return a readable error message by removing yt-dlp color codes."""
    return re.sub(r'\x1b\[[0-9;]*m', '', str(e)).strip()


def update_task(task_id, **kwargs):
    with tasks_lock:
        if task_id in download_tasks:
            download_tasks[task_id].update(kwargs)


def get_video_info(url):
    with yt_dlp.YoutubeDL(YDL_OPTS_BASE) as ydl:
        return ydl.extract_info(url, download=False)


def get_audio_languages(info):
    """Return available audio languages (YouTube multi-track): [{language, name, original}]."""
    langs = {}
    for f in info.get('formats') or []:
        if f.get('vcodec') not in (None, 'none') or f.get('acodec') in (None, 'none'):
            continue
        lang = f.get('language')
        if not lang:
            continue
        note = f.get('format_note') or ''
        entry = langs.setdefault(lang, {'language': lang, 'name': note.split(',')[0].strip() or lang,
                                        'original': False})
        if 'original' in note.lower():
            entry['original'] = True
    return list(langs.values())


def select_default_formats(info):
    """Return defaults: maximum 1080p60 and the original language."""
    original_lang = info.get('language') or info.get('original_language')
    return {
        'format': FORMAT_SELECTOR,
        'audio_language': original_lang,
    }


def make_progress_hook(task_id):
    done = {}

    def hook(d):
        if d.get('status') not in ('downloading', 'finished'):
            return
        done[d.get('filename')] = d.get('downloaded_bytes') or 0
        info = d.get('info_dict') or {}
        fmts = info.get('requested_formats') or [info]
        total = sum((f.get('filesize') or f.get('filesize_approx') or 0) for f in fmts)
        if total:
            update_task(task_id, progress=min(99, int(sum(done.values()) * 100 / total)))

    return hook


def probe_audio(path):
    """Return information about the first audio track (codec, profile, channels), or None."""
    out = subprocess.run(
        ['ffprobe', '-v', 'error', '-select_streams', 'a:0',
         '-show_entries', 'stream=codec_name,profile,channels,sample_rate', '-of', 'json', path],
        capture_output=True, text=True,
    )
    try:
        streams = json.loads(out.stdout).get('streams') or []
    except ValueError:
        return None
    return streams[0] if streams else None


def ensure_compatible_audio(path):
    """Ensure audio is present and convert it to stereo AAC-LC if needed.
    (Opus, HE-AAC, 5.1... = silence on many players.) Return 'aac' or None if there is no audio."""
    audio = probe_audio(path)
    if audio is None:
        return None
    channels = audio.get('channels') or 2
    if not (audio.get('codec_name') == 'aac' and audio.get('profile') == 'LC' and channels <= 2):
        tmp = path + '.fixed.mp4'
        subprocess.run(
            ['ffmpeg', '-y', '-i', path, '-map', '0', '-c:v', 'copy',
             '-c:a', 'aac', '-ac', '2', '-b:a', '192k', '-movflags', '+faststart', tmp],
            check=True, capture_output=True,
        )
        os.replace(tmp, path)
    return 'aac'


def measure_max_volume(path, seconds=600):
    """Return the maximum audio-track volume (dB) over the first minutes, or None if measurement fails."""
    out = subprocess.run(
        ['ffmpeg', '-hide_banner', '-nostats', '-t', str(seconds), '-i', path,
         '-vn', '-af', 'volumedetect', '-f', 'null', '-'],
        capture_output=True, text=True,
    )
    m = re.search(r'max_volume:\s*(-inf|-?[\d.]+)\s*dB', out.stderr)
    if not m:
        return None
    return float('-inf') if m.group(1) == '-inf' else float(m.group(1))


def run_download(task_id, url, audio_lang=''):
    tmpdir = tempfile.mkdtemp(prefix='yt_dl_')
    update_task(task_id, tmpdir=tmpdir)
    try:
        ydl_opts = {
            **YDL_OPTS_BASE,
            'format': build_format_selector(audio_lang),
            'format_sort': FORMAT_SORT,
            'outtmpl': os.path.join(tmpdir, '%(id)s.%(ext)s'),
            'merge_output_format': 'mp4',
            'progress_hooks': [make_progress_hook(task_id)],
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)

        # Final video file
        filepath = None
        downloads = info.get('requested_downloads') or []
        if downloads:
            filepath = downloads[0].get('filepath')
        if not filepath or not os.path.exists(filepath):
            videos = [f for f in os.listdir(tmpdir) if f.endswith(('.mp4', '.mkv', '.webm'))]
            filepath = os.path.join(tmpdir, videos[0]) if videos else None

        if not filepath:
            update_task(task_id, status='error', error='File not found after download')
            return

        # --- Audio guarantee: never serve a file without audio or with a silent track ---
        raw_audio = probe_audio(filepath)
        audio_codec = ensure_compatible_audio(filepath)
        if audio_codec is None:
            update_task(task_id, status='error', error="La vidéo téléchargée n'a pas de piste audio")
            return

        max_volume = measure_max_volume(filepath)
        print(f'[{task_id[:6]}] audio source={raw_audio} -> {audio_codec}, volume max={max_volume} dB')
        if max_volume is not None and max_volume <= SILENCE_DB:
            update_task(task_id, status='error', error='La piste audio de cette vidéo est silencieuse')
            return

        # Actual quality
        dl = downloads[0] if downloads else info
        height = dl.get('height') or info.get('height')
        fps = dl.get('fps') or info.get('fps')
        quality = f'{height}p{round(fps)}' if height and fps else (f'{height}p' if height else None)

        safe_title = yt_dlp.utils.sanitize_filename(info.get('title') or 'video')
        update_task(
            task_id,
            status='ready',
            progress=100,
            quality=quality,
            audio=audio_codec,
            filepath=filepath,
            filename=f'{safe_title}{os.path.splitext(filepath)[1]}',
        )
    except Exception as e:
        update_task(task_id, status='error', error=clean_error(e))


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/info', methods=['POST'])
def api_info():
    data = request.get_json(silent=True) or {}
    url = (data.get('url') or '').strip()

    if not url:
        return jsonify({'error': 'URL is required'}), 400

    try:
        info = get_video_info(url)
        return jsonify({
            'title': info.get('title'),
            'thumbnail': info.get('thumbnail'),
            'duration': info.get('duration'),
            'uploader': info.get('uploader'),
            'view_count': info.get('view_count'),
            'audio_languages': get_audio_languages(info),
            'defaults': select_default_formats(info),
        })
    except Exception as e:
        return jsonify({'error': clean_error(e)}), 500


@app.route('/api/prepare', methods=['POST'])
def api_prepare():
    """Start downloading the video in the background and return a task_id."""
    data = request.get_json(silent=True) or {}
    url = (data.get('url') or '').strip()
    audio_lang = (data.get('audio_lang') or '').strip()

    if not url:
        return jsonify({'error': 'URL is required'}), 400
    if audio_lang and not LANG_RE.match(audio_lang):
        return jsonify({'error': 'Invalid audio language'}), 400

    key = (url, audio_lang)
    with tasks_lock:
        existing_id = task_index.get(key)
        existing = download_tasks.get(existing_id) if existing_id else None
        if existing and existing.get('status') in ('downloading', 'ready'):
            return jsonify({'task_id': existing_id})

        task_id = os.urandom(16).hex()
        download_tasks[task_id] = {
            'status': 'downloading',
            'progress': 0,
            'created': time.time(),
            'key': key,
        }
        task_index[key] = task_id

    threading.Thread(target=run_download, args=(task_id, url, audio_lang), daemon=True).start()
    return jsonify({'task_id': task_id})


@app.route('/api/status/<task_id>')
def api_status(task_id):
    with tasks_lock:
        task = download_tasks.get(task_id)
        if not task:
            return jsonify({'error': 'Task not found'}), 404
        return jsonify({
            'status': task.get('status'),
            'progress': task.get('progress', 0),
            'error': task.get('error'),
            'quality': task.get('quality'),
            'audio': task.get('audio'),
        })


def get_ready_task(task_id):
    with tasks_lock:
        task = download_tasks.get(task_id)
        if not task or task.get('status') != 'ready':
            return None
        return dict(task)


@app.route('/api/stream/<task_id>')
def api_stream(task_id):
    """Stream the video (supports Range requests for seeking)."""
    task = get_ready_task(task_id)
    if not task or not os.path.exists(task['filepath']):
        return 'Video not ready', 404
    return send_file(task['filepath'], conditional=True)


@app.route('/api/download/<task_id>')
def api_download(task_id):
    task = get_ready_task(task_id)
    if not task or not os.path.exists(task['filepath']):
        return 'Video not ready', 404
    return send_file(task['filepath'], as_attachment=True, download_name=task['filename'])


def cleanup_old_tasks():
    """Delete tasks (and their files) that are more than one hour old."""
    while True:
        time.sleep(300)
        now = time.time()
        removed = []
        with tasks_lock:
            for task_id, task in list(download_tasks.items()):
                if task.get('status') == 'downloading':
                    continue
                if now - task.get('created', now) > TASK_TTL:
                    removed.append(download_tasks.pop(task_id))
                    if task_index.get(task.get('key')) == task_id:
                        task_index.pop(task.get('key'), None)
        for task in removed:
            tmpdir = task.get('tmpdir')
            if tmpdir and os.path.exists(tmpdir):
                shutil.rmtree(tmpdir, ignore_errors=True)


threading.Thread(target=cleanup_old_tasks, daemon=True).start()


def cleanup_all():
    """On application shutdown, delete all remaining temporary files."""
    with tasks_lock:
        tasks = list(download_tasks.values())
    for task in tasks:
        tmpdir = task.get('tmpdir')
        if tmpdir and os.path.exists(tmpdir):
            shutil.rmtree(tmpdir, ignore_errors=True)


atexit.register(cleanup_all)

PORT = 5000  # Change to the desired port


def main():
    # Open the browser to the app one second after the server starts.
    threading.Timer(1.0, lambda: webbrowser.open(f'http://localhost:{PORT}')).start()
    app.run(host='127.0.0.1', port=PORT)


if __name__ == '__main__':
    main()