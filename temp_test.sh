#!/bin/bash
export PATH=$PATH:/home/ubuntu/.deno/bin
mkdir -p /tmp/leg4
rm -f /tmp/leg4/*
cd /home/ubuntu/CLIEMTE-garrabot

echo "=== deno path ==="
which deno || echo "deno nao encontrado no PATH"
deno --version 2>&1 | head -1

echo "=== teste com cookies + ejs:github ==="
./venv/bin/yt-dlp \
  --write-auto-sub --write-sub \
  --skip-download \
  --sub-lang pt,pt-PT,pt-orig,en \
  --sub-format vtt \
  --cookies ./yt_cookies.txt \
  --remote-components ejs:github \
  --output /tmp/leg4/leg \
  https://www.youtube.com/watch?v=13Mq-NHx1eU 2>&1
echo "=== arquivos gerados ==="
ls /tmp/leg4/
echo "=== primeiras linhas ==="
head -5 /tmp/leg4/*.vtt 2>/dev/null
