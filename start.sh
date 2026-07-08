#!/bin/bash
# Запускаємо WARP як proxy (socks5 на порту 40000)
warp-svc &
sleep 3
warp-cli --accept-tos register
warp-cli --accept-tos mode proxy
warp-cli --accept-tos proxy port 40000
warp-cli --accept-tos connect
sleep 5
echo "WARP status:"
warp-cli --accept-tos status

# Запускаємо сервер
uvicorn app:app --host 0.0.0.0 --port 7860
