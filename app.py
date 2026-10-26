import json
import math
import shutil
import subprocess
import threading
import uuid
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_file
from werkzeug.utils import secure_filename

app = Flask(__name__)

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
OUTPUT_DIR = BASE_DIR / "output"
UPLOAD_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)

app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024

export_jobs = {}
export_jobs_lock = threading.Lock()


@app.route("/")
def index():
    return render_template("index.html")


def get_audio_duration(audio_path):
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(audio_path),
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    duration = float(result.stdout.strip())
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("Durasi audio tidak valid.")
    return duration


def ass_timestamp(seconds):
    centiseconds_total = max(0, round(float(seconds) * 100))
    hours, remainder = divmod(centiseconds_total, 360000)
    minutes, remainder = divmod(remainder, 6000)
    seconds_whole, centiseconds = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{seconds_whole:02d}.{centiseconds:02d}"


def escape_ass_text(text):
    return (
        str(text)
        .replace("\\", r"\\")
        .replace("{", r"\{")
        .replace("}", r"\}")
        .replace("\n", " ")
        .replace("\r", " ")
    )


def create_ass_subtitles(lyrics, duration, width, height, ass_path):
    # Wrap lirik secara manual agar baris panjang tidak keluar dari sisi video.
    base_font_size = 58 if width > height else 64
    margin = 100 if width > height else 90
    max_text_width = width - (margin * 2) - 40
    # Perkiraan lebar karakter Arial tebal; dipakai untuk membungkus teks.
    chars_per_line = max(8, int(max_text_width / (base_font_size * 0.58)))

    def wrap_lyric(value, limit):
        value = str(value).replace("\r", " ").replace("\n", " ").strip()
        if not value:
            return [""]
        words = value.split()
        lines = []
        current = ""
        for word in words:
            # Pecah kata yang sangat panjang supaya tetap muat di frame.
            pieces = [word[i:i + limit] for i in range(0, len(word), limit)]
            for piece in pieces:
                candidate = f"{current} {piece}".strip()
                if current and len(candidate) > limit:
                    lines.append(current)
                    current = piece
                else:
                    current = candidate
        if current:
            lines.append(current)
        return lines or [""]

    groups = {}
    for lyric in lyrics:
        groups.setdefault(float(lyric["start"]), []).append(str(lyric["text"]))

    # Hitung jumlah baris terbanyak yang tampil bersamaan, lalu sesuaikan
    # ukuran font agar grup lirik yang panjang tidak terpotong vertikal.
    wrapped_groups = {}
    max_lines = 1
    for start, items in groups.items():
        wrapped = []
        for item in items:
            wrapped.extend(wrap_lyric(item, chars_per_line))
        wrapped_groups[start] = wrapped
        max_lines = max(max_lines, len(wrapped))

    available_height = max(120, height - 2 * margin - 80)
    font_size = min(base_font_size, max(12, int(available_height / (max_lines * 1.25))))
    # Jika font mengecil, izinkan lebih banyak karakter per baris agar hasil rapi.
    chars_per_line = max(4, int(max_text_width / (font_size * 0.58)))
    wrapped_groups = {
        start: [line for item in items for line in wrap_lyric(item, chars_per_line)]
        for start, items in groups.items()
    }
    max_lines = max((len(items) for items in wrapped_groups.values()), default=1)
    font_size = min(font_size, max(12, int(available_height / (max_lines * 1.25))))

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Lyric,Arial,{font_size},&H00FFFFFF,&H000000FF,&H00000000,&H80000000,1,0,0,0,100,100,0,0,1,2,0,5,{margin},{margin},80,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    starts = sorted(wrapped_groups)
    events = []
    for index, start in enumerate(starts):
        end = min(starts[index + 1] if index + 1 < len(starts) else duration, duration)
        if start >= duration or end <= start:
            continue
        text = r"\N".join(escape_ass_text(line) for line in wrapped_groups[start])
        text = r"{\fad(400,400)}" + text
        events.append(
            f"Dialogue: 0,{ass_timestamp(start)},{ass_timestamp(end)},Lyric,,0,0,0,,{text}"
        )

    ass_path.write_text(header + "\n".join(events) + "\n", encoding="utf-8-sig")


def render_video_job(job_id, command, video_path, duration, job_dir):
    log_path = job_dir / "ffmpeg.log"
    process = None
    try:
        with log_path.open("w", encoding="utf-8", errors="replace") as log_file:
            process = subprocess.Popen(
                command,
                cwd=BASE_DIR,
                stdout=subprocess.PIPE,
                stderr=log_file,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
            with export_jobs_lock:
                export_jobs[job_id]["status"] = "rendering"
                export_jobs[job_id]["message"] = "Sedang merender video..."

            for raw_line in process.stdout:
                line = raw_line.strip()
                if line.startswith("out_time_us="):
                    try:
                        elapsed = int(line.split("=", 1)[1]) / 1_000_000
                        percent = max(0, min(99, int(elapsed / duration * 100)))
                        with export_jobs_lock:
                            if job_id in export_jobs:
                                export_jobs[job_id]["progress"] = percent
                    except (ValueError, ZeroDivisionError):
                        pass

            return_code = process.wait(timeout=3600)

        if return_code != 0 or not video_path.exists() or video_path.stat().st_size == 0:
            detail = log_path.read_text(encoding="utf-8", errors="replace")[-3000:]
            app.logger.error("FFmpeg gagal untuk job %s: %s", job_id, detail)
            with export_jobs_lock:
                export_jobs[job_id].update(
                    status="failed", progress=0,
                    message="FFmpeg gagal membuat video.", error=detail,
                )
            return

        with export_jobs_lock:
            export_jobs[job_id].update(
                status="completed", progress=100,
                message="Video selesai dibuat.", error=None,
            )
    except subprocess.TimeoutExpired:
        if process is not None:
            process.kill()
            process.wait()
        with export_jobs_lock:
            export_jobs[job_id].update(
                status="failed",
                message="Proses ekspor melewati batas waktu satu jam.",
                error="Batas waktu ekspor terlampaui.",
            )
    except Exception as error:
        app.logger.exception("Kesalahan saat merender job %s", job_id)
        with export_jobs_lock:
            if job_id in export_jobs:
                export_jobs[job_id].update(
                    status="failed",
                    message="Terjadi kesalahan saat ekspor.",
                    error=str(error),
                )


@app.route("/export-video", methods=["POST"])
def export_video():
    audio = request.files.get("audio")
    lyrics_json = request.form.get("lyrics")
    aspect_ratio = request.form.get("aspect_ratio", "16:9")

    if not audio or not audio.filename:
        return jsonify(error="Pilih file audio terlebih dahulu."), 400
    if not lyrics_json:
        return jsonify(error="Data timing lirik tidak ditemukan."), 400

    if aspect_ratio == "16:9":
        width, height = 1920, 1080
    elif aspect_ratio == "9:16":
        width, height = 1080, 1920
    else:
        return jsonify(error="Rasio video tidak valid."), 400

    try:
        lyrics = json.loads(lyrics_json)
    except json.JSONDecodeError:
        return jsonify(error="Data timing lirik bukan JSON yang valid."), 400

    if not isinstance(lyrics, list) or not lyrics:
        return jsonify(error="Belum ada lirik untuk diekspor."), 400

    for lyric in lyrics:
        if (
            not isinstance(lyric, dict)
            or not isinstance(lyric.get("text"), str)
            or not lyric["text"].strip()
            or isinstance(lyric.get("start"), bool)
            or not isinstance(lyric.get("start"), (int, float))
            or not math.isfinite(lyric["start"])
            or lyric["start"] < 0
        ):
            return jsonify(error="Semua baris harus memiliki teks dan timing yang valid."), 400

    lyrics.sort(key=lambda item: float(item["start"]))
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or not shutil.which("ffprobe"):
        return jsonify(error="FFmpeg/FFprobe tidak ditemukan. Pastikan keduanya tersedia di PATH."), 500

    job_id = uuid.uuid4().hex
    job_dir = OUTPUT_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    try:
        audio_path = job_dir / (secure_filename(audio.filename) or "audio")
        video_path = job_dir / "lyricforge.mp4"
        ass_path = job_dir / "lyrics.ass"
        audio.save(audio_path)

        duration = get_audio_duration(audio_path)
        if any(float(item["start"]) >= duration for item in lyrics):
            shutil.rmtree(job_dir, ignore_errors=True)
            return jsonify(error="Timing lirik harus berada sebelum akhir audio."), 400

        create_ass_subtitles(lyrics, duration, width, height, ass_path)

        # Escape karakter khusus filter subtitle, termasuk drive/path Windows.
        relative_ass_path = ass_path.relative_to(BASE_DIR).as_posix()
        escaped_ass_path = (
            relative_ass_path.replace("\\", r"\\")
            .replace(":", r"\:")
            .replace("'", r"\'")
            .replace(",", r"\,")
            .replace("[", r"\[")
            .replace("]", r"\]")
        )
        subtitle_filter = f"subtitles=filename='{escaped_ass_path}'"

        command = [
            ffmpeg, "-y",
            "-i", str(audio_path),
            "-f", "lavfi",
            "-i", f"color=c=black:s={width}x{height}:r=30:d={duration}",
            "-map", "1:v:0", "-map", "0:a:0",
            "-vf", subtitle_filter,
            "-c:v", "libx264", "-preset", "medium", "-crf", "20",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k",
            "-t", str(duration),
            "-progress", "pipe:1", "-nostats",
            "-movflags", "+faststart",
            str(video_path),
        ]

        with export_jobs_lock:
            export_jobs[job_id] = {
                "status": "queued",
                "progress": 0,
                "message": "Ekspor masuk antrean...",
                "error": None,
                "filename": f"LyricForge-{aspect_ratio.replace(':', 'x')}.mp4",
            }

        threading.Thread(
            target=render_video_job,
            args=(job_id, command, video_path, duration, job_dir),
            daemon=True,
        ).start()

        return jsonify(job_id=job_id), 202
    except (subprocess.SubprocessError, ValueError, OSError) as error:
        app.logger.exception("Gagal mempersiapkan ekspor video")
        shutil.rmtree(job_dir, ignore_errors=True)
        return jsonify(error=f"Gagal mempersiapkan ekspor: {error}"), 500
    except Exception:
        app.logger.exception("Kesalahan saat mempersiapkan ekspor video")
        shutil.rmtree(job_dir, ignore_errors=True)
        return jsonify(error="Terjadi kesalahan saat ekspor. Periksa terminal Flask."), 500


@app.route("/export-progress/<job_id>", methods=["GET"])
def export_progress(job_id):
    with export_jobs_lock:
        job = export_jobs.get(job_id)
        if job is None:
            return jsonify(error="Pekerjaan ekspor tidak ditemukan."), 404
        return jsonify({
            "status": job["status"],
            "progress": job["progress"],
            "message": job["message"],
            "error": job.get("error"),
        })


@app.route("/download-video/<job_id>", methods=["GET"])
def download_video(job_id):
    with export_jobs_lock:
        job = export_jobs.get(job_id)
        if job is None:
            return jsonify(error="Pekerjaan ekspor tidak ditemukan."), 404
        if job["status"] != "completed":
            return jsonify(error="Video belum selesai dibuat."), 409
        filename = job["filename"]

    video_path = OUTPUT_DIR / job_id / "lyricforge.mp4"
    if not video_path.exists():
        return jsonify(error="File video tidak ditemukan."), 404

    return send_file(
        video_path,
        as_attachment=True,
        download_name=filename,
        mimetype="video/mp4",
    )


if __name__ == "__main__":
    app.run(debug=True, threaded=True)
