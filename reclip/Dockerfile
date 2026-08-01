FROM denoland/deno:bin-2.9.4 AS deno

FROM python:3.12-slim

COPY --from=deno /deno /usr/local/bin/deno

RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8899
ENV HOST=0.0.0.0
ENV DOWNLOADS_PATH=/downloads
CMD ["python", "app.py"]
