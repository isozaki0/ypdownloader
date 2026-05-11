import os
import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from functools import wraps
from flask import Flask, request, jsonify, render_template, send_file, after_this_request, session, redirect, url_for

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "changeme-random-key-xk29zp")

# ── 설정 ──────────────────────────────────────────────
YTDLP_CMD = os.environ.get("YTDLP_CMD", "yt-dlp")
REQUEST_TIMEOUT = 30   # yt-dlp 실행 최대 대기 시간(초)
PASSWORD = os.environ.get("APP_PASSWORD", "6658")
COOKIES_FILE = Path(__file__).parent / "cookies.txt"


# ── 인증 데코레이터 ───────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("authenticated"):
            if request.is_json:
                return jsonify({"error": "인증이 필요합니다."}), 401
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated

# 표시할 자막 언어 (이 목록에 있는 것만 노출)
PREF_LANGS = ["ko", "en", "en-US", "en-GB"]


# ── 헬퍼 ──────────────────────────────────────────────
def pick_best_video_url(formats: list) -> dict | None:
    """
    합본(영상+음성) 스트림 중 해상도가 가장 높은 것을 반환.
    없으면 None.
    """
    candidates = [
        f for f in formats
        if f.get("vcodec", "none") != "none"
        and f.get("acodec", "none") != "none"
        and f.get("protocol") not in ("m3u8", "m3u8_native", "dash")
        and f.get("url")
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda f: f.get("height") or 0)


def pick_best_audio_url(formats: list) -> dict | None:
    """
    오디오 전용 스트림 중 비트레이트가 가장 높은 것을 반환.
    m4a(AAC) 우선, 없으면 다른 오디오 포맷.
    """
    audio_only = [
        f for f in formats
        if f.get("acodec", "none") != "none"
        and f.get("vcodec", "none") == "none"
        and f.get("protocol") not in ("m3u8", "m3u8_native", "dash")
        and f.get("url")
    ]
    if not audio_only:
        return None
    # m4a 먼저, 그 다음 비트레이트 높은 순
    m4a = [f for f in audio_only if f.get("ext") == "m4a"]
    pool = m4a if m4a else audio_only
    return max(pool, key=lambda f: f.get("abr") or f.get("tbr") or 0)


def pick_subtitles(info: dict) -> dict:
    """
    자막 URL 딕셔너리 반환.  {lang_code: {url, ext}} 형태.
    - 수동 자막: 모든 언어 포함
    - 자동 자막: PREF_LANGS 에 있는 언어만 포함 (전 언어 노출 방지)
    """
    result = {}
    EXTS = ("srt", "vtt")

    # 수동 자막 - 선호 언어만
    for lang, subs in (info.get("subtitles") or {}).items():
        if lang not in PREF_LANGS:
            continue
        for sub in (subs or []):
            if sub.get("ext") in EXTS and sub.get("url"):
                result[lang] = {"url": sub["url"], "ext": sub["ext"], "auto": False}
                break

    # 자동 자막 - 선호 언어만
    for lang, subs in (info.get("automatic_captions") or {}).items():
        if lang not in PREF_LANGS:
            continue
        if lang in result:
            continue
        for sub in (subs or []):
            if sub.get("ext") in EXTS and sub.get("url"):
                result[lang] = {"url": sub["url"], "ext": sub["ext"], "auto": True}
                break

    # 선호 언어 순 정렬
    ordered = {}
    for lang in PREF_LANGS:
        if lang in result:
            ordered[lang] = result.pop(lang)
    ordered.update(result)
    return ordered


# ── 라우트 ────────────────────────────────────────────
@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        pw = (request.form.get("password") or "").strip()
        if pw == PASSWORD:
            session["authenticated"] = True
            return redirect(url_for("index"))
        error = "비밀번호가 틀렸습니다."
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def index():
    return render_template("index.html")


@app.route("/extract", methods=["POST"])
@login_required
def extract():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()

    if not url:
        return jsonify({"error": "URL을 입력해 주세요."}), 400

    try:
        proc = subprocess.run(
            [YTDLP_CMD, "--dump-json", "--no-playlist",
             "--extractor-args", "youtube:player_client=web",
             "--user-agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"]
            + (["--cookies", str(COOKIES_FILE)] if COOKIES_FILE.exists() else [])
            + [url],
            capture_output=True,
            text=True,
            timeout=REQUEST_TIMEOUT,
        )
    except FileNotFoundError:
        return jsonify({"error": "yt-dlp를 찾을 수 없습니다. 서버 설정을 확인해 주세요."}), 500
    except subprocess.TimeoutExpired:
        return jsonify({"error": "영상 정보를 가져오는 데 시간이 너무 오래 걸렸습니다."}), 408

    if proc.returncode != 0:
        err_msg = proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else "알 수 없는 오류"
        return jsonify({"error": f"yt-dlp 오류: {err_msg}"}), 400

    try:
        info = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return jsonify({"error": "영상 정보를 파싱할 수 없습니다."}), 500

    # 최고 해상도 파악 (표시용)
    formats = info.get("formats") or []
    all_heights = [f.get("height") for f in formats if f.get("height")]
    best_height = max(all_heights) if all_heights else None

    # 오디오 지원 여부 (오디오 전용 스트림 존재 확인)
    has_audio = any(
        f.get("acodec", "none") != "none" and f.get("vcodec", "none") == "none"
        for f in formats
    )

    # 자막 선택
    subtitles = pick_subtitles(info)

    return jsonify({
        "title":      info.get("title") or "영상",
        "thumbnail":  info.get("thumbnail"),
        "duration":   info.get("duration"),
        "uploader":   info.get("uploader") or info.get("channel"),
        "has_video":  True,
        "quality":    f"{best_height}p" if best_height else "best",
        "has_audio":  has_audio,
        "subtitles":  subtitles,
    })


@app.route("/download/video", methods=["POST"])
@login_required
def download_video():
    """
    yt-dlp + ffmpeg으로 최고화질 영상을 서버에서 병합 후 전송.
    """
    data  = request.get_json(silent=True) or {}
    url   = (data.get("url")   or "").strip()
    title = (data.get("title") or "video").strip()

    if not url:
        return jsonify({"error": "URL이 필요합니다."}), 400

    tmpdir = tempfile.mkdtemp()
    try:
        output_template = os.path.join(tmpdir, "video.%(ext)s")
        proc = subprocess.run(
            [
                YTDLP_CMD,
                "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best",
                "--merge-output-format", "mp4",
                "--no-playlist",
                "--extractor-args", "youtube:player_client=web",
                "--user-agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                "-o", output_template,
            ]
            + (["--cookies", str(COOKIES_FILE)] if COOKIES_FILE.exists() else [])
            + [url],
            capture_output=True,
            timeout=600,
        )
        if proc.returncode != 0:
            err = proc.stderr.decode(errors="ignore").strip().splitlines()
            return jsonify({"error": err[-1] if err else "영상 다운로드 실패"}), 500

        files = list(Path(tmpdir).glob("video.*"))
        if not files:
            return jsonify({"error": "다운로드된 파일을 찾을 수 없습니다."}), 500

        video_file = files[0]
        safe_title = "".join(c for c in title if c not in r'\/:*?"<>|').strip() or "video"

        @after_this_request
        def cleanup(response):
            shutil.rmtree(tmpdir, ignore_errors=True)
            return response

        return send_file(
            str(video_file),
            mimetype="video/mp4",
            as_attachment=True,
            download_name=f"{safe_title}.mp4",
        )

    except subprocess.TimeoutExpired:
        shutil.rmtree(tmpdir, ignore_errors=True)
        return jsonify({"error": "다운로드 시간 초과 (10분)"}), 408
    except Exception as e:
        shutil.rmtree(tmpdir, ignore_errors=True)
        return jsonify({"error": str(e)}), 500


@app.route("/download/audio", methods=["POST"])
@login_required
def download_audio():
    """
    yt-dlp로 임시 파일에 오디오를 받은 뒤 브라우저로 전송.
    YouTube 스로틀링 우회 목적.
    """
    data  = request.get_json(silent=True) or {}
    url   = (data.get("url")   or "").strip()
    title = (data.get("title") or "audio").strip()

    if not url:
        return jsonify({"error": "URL이 필요합니다."}), 400

    tmpdir = tempfile.mkdtemp()
    try:
        output_template = os.path.join(tmpdir, "audio.%(ext)s")
        proc = subprocess.run(
            [
                YTDLP_CMD,
                "-f", "bestaudio[ext=m4a]/bestaudio",
                "--no-playlist",
                "--extractor-args", "youtube:player_client=web",
                "--user-agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                "-o", output_template,
            ]
            + (["--cookies", str(COOKIES_FILE)] if COOKIES_FILE.exists() else [])
            + [url],
            capture_output=True,
            timeout=180,
        )
        if proc.returncode != 0:
            err = proc.stderr.decode(errors="ignore").strip().splitlines()
            return jsonify({"error": err[-1] if err else "오디오 다운로드 실패"}), 500

        files = list(Path(tmpdir).glob("audio.*"))
        if not files:
            return jsonify({"error": "다운로드된 파일을 찾을 수 없습니다."}), 500

        audio_file = files[0]
        ext = audio_file.suffix.lstrip(".") or "m4a"
        safe_title = "".join(c for c in title if c not in r'\/:*?"<>|').strip() or "audio"

        @after_this_request
        def cleanup(response):
            shutil.rmtree(tmpdir, ignore_errors=True)
            return response

        return send_file(
            str(audio_file),
            mimetype="audio/mp4" if ext == "m4a" else "audio/webm",
            as_attachment=True,
            download_name=f"{safe_title}.{ext}",
        )

    except subprocess.TimeoutExpired:
        shutil.rmtree(tmpdir, ignore_errors=True)
        return jsonify({"error": "다운로드 시간 초과 (3분)"}), 408
    except Exception as e:
        shutil.rmtree(tmpdir, ignore_errors=True)
        return jsonify({"error": str(e)}), 500


# ── 진입점 ────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
