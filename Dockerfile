# ── 베이스 이미지 ──────────────────────────────────────
FROM python:3.11-slim

# ── 시스템 패키지 (yt-dlp 실행에 필요) ──────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# ── 작업 디렉터리 ────────────────────────────────────────
WORKDIR /app

# ── Python 의존성 ────────────────────────────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── 앱 소스 복사 ────────────────────────────────────────
COPY . .

# ── 포트 (Cloud Run 기본값 8080) ─────────────────────────
ENV PORT=8080
EXPOSE 8080

# ── 실행 ─────────────────────────────────────────────────
CMD ["python", "app.py"]
