#!/bin/bash
# Запуск бота: ./run.sh
set -e
cd "$(dirname "$0")"
if [ ! -f .env ]; then
  cp .env.example .env
  echo "Создан .env — заполни IG_ACCESS_TOKEN, META_APP_SECRET, LLM_API_KEY и запусти ./run.sh снова"
  open -e .env 2>/dev/null || true
  exit 1
fi
VENV="$HOME/.venvs/studio-lokon-bot"   # venv вне iCloud, чтобы не синхронизировался
[ -d "$VENV" ] || python3 -m venv "$VENV"
"$VENV/bin/pip" install -q -r requirements.txt
exec "$VENV/bin/uvicorn" app.main:app --port 8000 --workers 1
