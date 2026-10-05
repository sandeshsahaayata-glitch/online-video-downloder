import os
import re
import time
import uuid
import shutil
import tempfile
import urllib.parse
from collections import defaultdict
from pathlib import Path
from flask import Flask, render_template, request, jsonify, send_file, after_this_request, make_response
import yt_dlp

app = Flask(__name__)

# Security & Temp Storage
APP_DIR = Path(__file__).parent.resolve()
CACHE_DIR = APP_DIR / "downloads_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# -------------------------------------------------------------
# FFMPEG Auto-Discovery & Path Injection
# -------------------------------------------------------------
def get_ffmpeg_dir():
    p = shutil.which("ffmpeg")
    if p:
        return str(Path(p).parent)
    localappdata = os.environ.get("LOCALAPPDATA", "")
    if localappdata:
        winget_dir = Path(localappdata) / "Microsoft" / "WinGet" / "Packages"
        for candidate in winget_dir.glob("**/ffmpeg.exe"):
            return str(candidate.parent)
    return None

FFMPEG_DIR = get_ffmpeg_dir()
if FFMPEG_DIR and FFMPEG_DIR not in os.environ.get("PATH", ""):
    os.environ["PATH"] = FFMPEG_DIR + os.pathsep + os.environ.get("PATH", "")

# -------------------------------------------------------------
# Realistic Browser Headers (Fixes HTTP 403 on Instagram / Facebook / YouTube)
# -------------------------------------------------------------
COMMON_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,hi;q=0.8",
    "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1"
}

ALLOWED_DOMAINS = {
    "youtube.com", "m.youtube.com", "youtu.be", "music.youtube.com",
    "instagram.com", "www.instagram.com",
    "facebook.com", "www.facebook.com", "fb.watch", "m.facebook.com", "fb.com",
    "tiktok.com", "www.tiktok.com", "vm.tiktok.com",
    "twitter.com", "x.com", "mobile.twitter.com",
    "pinterest.com", "pin.it",
    "reddit.com", "www.reddit.com",
    "threads.net"
}

ALLOWED_QUALITIES = {"best_video", "720p", "480p", "audio_mp3"}

def validate_and_clean_url(raw_url: str):
    if not raw_url or not isinstance(raw_url, str):
        return None, "Kripya video ka URL enter karein."
    
    url = raw_url.strip()
    if len(url) > 1000:
        return None, "URL bahut lamba hai."

    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return None, "Kewal HTTP aur HTTPS links supported hain."

    domain = parsed.netloc.lower().split(":")[0]
    
    is_allowed = False
    for allowed in ALLOWED_DOMAINS:
        if domain == allowed or domain.endswith("." + allowed):
            is_allowed = True
            break

    if not is_allowed:
        return None, "Ye platform support nahi hai. YouTube, Instagram ya Facebook ka link use karein."

    blocked_hosts = {"localhost", "127.0.0.1", "0.0.0.0", "169.254.169.254", "::1"}
    if domain in blocked_hosts or domain.startswith("10.") or domain.startswith("192.168."):
        return None, "Invalid URL address."

    return url, None

def sanitize_filename(name: str) -> str:
    clean = re.sub(r'[^a-zA-Z0-9_\- ]', '', name).strip()
    return clean[:80] if clean else "OneClick_Video"

def detect_platform(url: str) -> str:
    u = url.lower()
    if "youtube.com" in u or "youtu.be" in u:
        return "YouTube"
    elif "instagram.com" in u:
        return "Instagram"
    elif "facebook.com" in u or "fb.watch" in u or "fb.com" in u:
        return "Facebook"
    elif "tiktok.com" in u:
        return "TikTok"
    elif "twitter.com" in u or "x.com" in u:
        return "Twitter / X"
    return "Social Media"

# Rate Limiter
request_history = defaultdict(list)

def is_rate_limited(ip: str, max_requests: int = 25, window_seconds: int = 60) -> bool:
    now = time.time()
    history = request_history[ip]
    request_history[ip] = [t for t in history if now - t < window_seconds]
    if len(request_history[ip]) >= max_requests:
        return True
    request_history[ip].append(now)
    return False

@app.after_request
def apply_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response

def cleanup_old_cache():
    try:
        now = time.time()
        for f in CACHE_DIR.glob("*"):
            if f.is_file() and (now - f.stat().st_mtime > 1800):
                f.unlink(missing_ok=True)
    except Exception:
        pass

# -------------------------------------------------------------
# Routes
# -------------------------------------------------------------
@app.route("/")
def index():
    cleanup_old_cache()
    return render_template("index.html")

@app.route("/manifest.json")
def manifest():
    return send_file(APP_DIR / "static" / "manifest.json", mimetype="application/manifest+json")

@app.route("/service-worker.js")
def service_worker():
    return send_file(APP_DIR / "static" / "service-worker.js", mimetype="application/javascript")

@app.route("/api/info", methods=["POST"])
def get_info():
    client_ip = request.remote_addr or "unknown"
    if is_rate_limited(client_ip, max_requests=30, window_seconds=60):
        return jsonify({"success": False, "error": "Bahut zyada requests. Kripya 1 minute baad koshish karein."}), 429

    data = request.get_json(silent=True) or {}
    raw_url = data.get("url")
    url, err = validate_and_clean_url(raw_url)
    if err:
        return jsonify({"success": False, "error": err}), 400

    platform = detect_platform(url)

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": False,
        "skip_download": True,
        "socket_timeout": 25,
        "http_headers": COMMON_HEADERS,
        "extractor_args": {
            "youtube": {
                "player_client": ["android", "ios"],
                "player_skip": ["web", "web_embedded", "mweb"]
            }
        }
    }
    if FFMPEG_DIR:
        ydl_opts["ffmpeg_location"] = FFMPEG_DIR

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if not info:
                return jsonify({"success": False, "error": "Video details nahi mil saki. Link check karein."}), 400

            title = info.get("title", "One Click Video")
            thumbnail = info.get("thumbnail") or ""
            duration = info.get("duration")
            duration_str = ""
            if duration:
                mins, secs = divmod(int(duration), 60)
                hours, mins = divmod(mins, 60)
                if hours:
                    duration_str = f"{hours:02d}:{mins:02d}:{secs:02d}"
                else:
                    duration_str = f"{mins:02d}:{secs:02d}"

            uploader = info.get("uploader") or info.get("channel") or platform

            formats = [
                {"id": "best_video", "label": "Full HD / Best Quality (MP4)", "type": "video"},
                {"id": "720p", "label": "HD 720p (MP4)", "type": "video"},
                {"id": "480p", "label": "SD 480p / 360p (MP4)", "type": "video"},
                {"id": "audio_mp3", "label": "Audio Only (MP3)", "type": "audio"},
            ]

            return jsonify({
                "success": True,
                "platform": platform,
                "title": title,
                "thumbnail": thumbnail,
                "duration": duration_str,
                "uploader": uploader,
                "formats": formats
            })

    except Exception as e:
        err_msg = str(e)
        if "login" in err_msg.lower() or "bot" in err_msg.lower():
            err_msg = "Is video ko dekhne ke liye account login chahiye (Private video)."
        elif "unsupported url" in err_msg.lower():
            err_msg = "Ye link support nahi karta. YouTube, Instagram ya Facebook ka link dalein."
        return jsonify({"success": False, "error": f"Error: {err_msg}"}), 500

@app.route("/api/download")
def download():
    client_ip = request.remote_addr or "unknown"
    if is_rate_limited(client_ip, max_requests=15, window_seconds=60):
        return "Rate limit exceeded. Kripya 1 minute intezaar karein.", 429

    raw_url = request.args.get("url")
    quality = request.args.get("quality", "best_video").strip()

    url, err = validate_and_clean_url(raw_url)
    if err:
        return err, 400

    if quality not in ALLOWED_QUALITIES:
        quality = "best_video"

    unique_id = uuid.uuid4().hex[:10]
    output_template = str(CACHE_DIR / f"{unique_id}_%(title).70s.%(ext)s")

    ydl_opts = {
        "outtmpl": output_template,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "retries": 3,
        "restrictfilenames": True,
        "windowsfilenames": True,
        "max_filesize": 500 * 1024 * 1024,
        "http_headers": COMMON_HEADERS,
        "extractor_args": {
            "youtube": {
                "player_client": ["android", "ios"],
                "player_skip": ["web", "web_embedded", "mweb"]
            }
        }
    }
    if FFMPEG_DIR:
        ydl_opts["ffmpeg_location"] = FFMPEG_DIR

    if quality == "audio_mp3":
        ydl_opts["format"] = "bestaudio/best"
        ydl_opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]
    elif quality == "720p":
        ydl_opts["format"] = "best[height<=720][ext=mp4]/best[height<=720]/best"
    elif quality == "480p":
        ydl_opts["format"] = "best[height<=480][ext=mp4]/best[height<=480]/best"
    else:  # best_video
        ydl_opts["format"] = "best[ext=mp4]/bestvideo[ext=mp4]+bestaudio[ext=m4a]/best"

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            title = info.get("title", "OneClick_Video")
            safe_title = sanitize_filename(title)

        downloaded_files = list(CACHE_DIR.glob(f"{unique_id}_*"))
        if not downloaded_files:
            return "Download fail ho gaya. File generate nahi ho saki.", 500

        target_file = downloaded_files[0]
        ext = target_file.suffix.replace(".", "").lower()
        if not ext:
            ext = "mp3" if quality == "audio_mp3" else "mp4"

        download_name = f"{safe_title}.{ext}"

        @after_this_request
        def cleanup(response):
            try:
                for f in CACHE_DIR.glob(f"{unique_id}_*"):
                    f.unlink(missing_ok=True)
            except Exception:
                pass
            return response

        resp = send_file(
            target_file,
            as_attachment=True,
            download_name=download_name,
            mimetype="audio/mpeg" if ext == "mp3" else "video/mp4"
        )
        resp.headers["Access-Control-Expose-Headers"] = "Content-Disposition"
        return resp

    except Exception as e:
        return f"Download Error: {str(e)}", 500

if __name__ == "__main__":
    import socket
    hostname = socket.gethostname()
    try:
        local_ip = socket.gethostbyname(hostname)
    except Exception:
        local_ip = "127.0.0.1"

    port = int(os.environ.get("PORT", 5050))
    print("=" * 60)
    print("One Click Downloader Server Ready!")
    print(f"FFmpeg Path:      {FFMPEG_DIR or 'Not found'}")
    print(f"Port:             {port}")
    print(f"PC Browser:       http://localhost:{port}")
    print(f"Mobile Browser:   http://{local_ip}:{port}")
    print("=" * 60)
    app.run(host="0.0.0.0", port=port, debug=False)
