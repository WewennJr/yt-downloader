import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time

import static_ffmpeg
import yt_dlp
from flask import Flask, render_template, request, send_file, jsonify

app = Flask(__name__)

TASK_TTL = 3600  # secondes avant suppression d'une tâche et de ses fichiers
SILENCE_DB = -80  # en dessous, la piste audio est considérée comme silencieuse

# {task_id: {'status': 'downloading'|'ready'|'error', 'progress': int, 'created': float, ...}}
download_tasks = {}
# {(url, audio_lang): task_id} -> évite de re-télécharger la même vidéo
task_index = {}
tasks_lock = threading.Lock()

LANG_RE = re.compile(r'^[A-Za-z0-9_-]{1,20}$')

YDL_OPTS_BASE = {
    'quiet': True,
    'no_warnings': True,
    'extract_flat': False,
    'noplaylist': True,
}

# --- Cookies YouTube (contourne "Sign in to confirm you're not a bot") ---
# 1) un fichier cookies.txt à côté de web.py (ou chemin dans YT_COOKIES_FILE)
# 2) sinon, les cookies d'un navigateur : YT_COOKIES_BROWSER=firefox (recommandé sous Windows)
COOKIES_FILE = os.environ.get('YT_COOKIES_FILE') or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), 'cookies.txt')
COOKIES_BROWSER = os.environ.get('YT_COOKIES_BROWSER', '').strip()

if os.path.isfile(COOKIES_FILE):
    YDL_OPTS_BASE['cookiefile'] = COOKIES_FILE
    print(f'[cookies] fichier utilisé : {COOKIES_FILE}')
elif COOKIES_BROWSER:
    YDL_OPTS_BASE['cookiesfrombrowser'] = (COOKIES_BROWSER,)
    print(f'[cookies] cookies du navigateur : {COOKIES_BROWSER}')
else:
    print('[cookies] aucun cookie configuré (cookies.txt ou YT_COOKIES_BROWSER)')

# --- Proxy (YT_PROXY) : pour sortir par une IP résidentielle quand l'IP du VPS est bloquée ---
# ex : YT_PROXY=socks5://127.0.0.1:1080
YT_PROXY = os.environ.get('YT_PROXY', '').strip()
if YT_PROXY:
    YDL_OPTS_BASE['proxy'] = YT_PROXY
    print(f'[proxy] yt-dlp passe par : {YT_PROXY}')

# ffmpeg est un programme externe : static-ffmpeg le télécharge au 1er lancement
# et l'ajoute au PATH du process (sans rien faire si ffmpeg est déjà dans le PATH).
static_ffmpeg.add_paths(weak=True)

# Meilleure vidéo + audio AAC (m4a) en priorité : lisible partout,
# sinon n'importe quel audio, sinon meilleur format combiné.
FORMAT_SELECTOR = 'bv*+ba[acodec^=mp4a]/bv*+ba/b'
# "res:1080"   => résolution la plus proche de 1080p sans la dépasser (donc max dispo si en dessous)
# "fps"        => à résolution égale, le plus de fps (60 > 30)
# h264/aac     => lecture garantie dans le navigateur (mp4)
# "channels:2" => STÉRÉO : sans ça, yt-dlp préfère la piste 5.1 (6 canaux), souvent muette sur PC
# Sans choix de langue, l'audio d'origine est prioritaire via le tri par défaut de yt-dlp ("lang").
FORMAT_SORT = ['res:1080', 'fps', 'vcodec:h264', 'acodec:aac', 'channels:2']


def build_format_selector(audio_lang=''):
    """Sélecteur de format, avec une langue audio précise si demandée (sinon l'originale)"""
    if not audio_lang:
        return FORMAT_SELECTOR
    return (f'bv*+ba[language={audio_lang}][acodec^=mp4a]/'
            f'bv*+ba[language={audio_lang}]/' + FORMAT_SELECTOR)


def clean_error(e):
    """Message d'erreur lisible : retire les codes couleur de yt-dlp et explique le blocage anti-bot"""
    msg = re.sub(r'\x1b\[[0-9;]*m', '', str(e)).strip()
    if 'Sign in to confirm' in msg:
        return ("YouTube bloque l'IP de ce serveur (vérification anti-bot). Configure un fichier "
                "cookies.txt à côté de web.py et/ou un proxy résidentiel (variable YT_PROXY).")
    return msg


def update_task(task_id, **kwargs):
    with tasks_lock:
        if task_id in download_tasks:
            download_tasks[task_id].update(kwargs)


def get_video_info(url):
    with yt_dlp.YoutubeDL(YDL_OPTS_BASE) as ydl:
        return ydl.extract_info(url, download=False)


def get_audio_languages(info):
    """Langues audio disponibles (YouTube multi-pistes) : [{language, name, original}]"""
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
    """Défauts : 1080p60 max, langue originale"""
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
    """Infos de la première piste audio (codec, profil, canaux), None s'il n'y en a pas"""
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
    """Vérifie la présence de l'audio, et le force en AAC-LC stéréo s'il ne l'est pas
    (Opus, HE-AAC, 5.1... = silence sur beaucoup de lecteurs). Renvoie 'aac' ou None si pas d'audio."""
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
    """Volume maximum (dB) de la piste audio sur les premières minutes, None si la mesure échoue"""
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

        # Fichier vidéo final
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

        # --- Garantie du son : on ne sert jamais un fichier sans audio ou muet ---
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

        # Qualité réelle
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
    """Lance le téléchargement de la vidéo en arrière-plan, renvoie un task_id"""
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
    """Stream la vidéo (gère les Range pour le seek)"""
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
    """Supprime les tâches (et leurs fichiers) de plus d'1h"""
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


def main():
    app.run(host='0.0.0.0', port=5004)


if __name__ == '__main__':
    main()