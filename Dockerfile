FROM python:3.11-slim

WORKDIR /app

# ✅ FFmpeg + aria2 (faster yt-dlp downloads)
RUN apt-get update && apt-get install -y ffmpeg aria2 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

COPY . .

RUN chmod +x start.sh

CMD ["bash", "start.sh"]
