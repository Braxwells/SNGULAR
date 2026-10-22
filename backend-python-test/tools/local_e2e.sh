#!/usr/bin/env bash
# Validacion end-to-end SIN Docker (provider real + app real + k6 real).
#
# Pensado para maquinas donde no hay Docker disponible. Replica el grafo de
# docker-compose: la app apunta a localhost:3001 y k6 ataca localhost:5000.
#
#   Requisitos: python3 >= 3.10 y el binario `k6` en el PATH.
#   Uso:        bash tools/local_e2e.sh [--chaos]
#
# Con --chaos, el provider se mata a los 15 s para comprobar que la disponibilidad
# del servicio se mantiene aunque el proveedor externo desaparezca.
set -u

PROJ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="${VENV:-$HOME/.cache/singular-notifier-venv}"
RUN="${RUN:-$(mktemp -d)}"
CHAOS=0
[ "${1:-}" = "--chaos" ] && CHAOS=1

command -v k6 > /dev/null || { echo "ERROR: k6 no esta en el PATH"; exit 1; }

[ -d "$VENV" ] || python3 -m venv "$VENV"
"$VENV/bin/pip" -q install -r "$PROJ/app/requirements.txt"
PY="$VENV/bin/python"

cleanup() {
  [ -n "${APP_PID:-}" ] && kill "$APP_PID" 2>/dev/null
  [ -n "${PROV_PID:-}" ] && kill "$PROV_PID" 2>/dev/null
  wait 2>/dev/null
}
trap cleanup EXIT

echo "--- arrancando provider en :3001"
( cd "$PROJ/provider" && "$PY" -m uvicorn app:app --host 127.0.0.1 --port 3001 \
    --no-access-log > "$RUN/provider.log" 2>&1 & echo $! > "$RUN/prov.pid" )
PROV_PID="$(cat "$RUN/prov.pid")"

echo "--- arrancando la app en :5000"
export NOTIFIER_PROVIDER_URL=http://127.0.0.1:3001
export NOTIFIER_WORKERS="${NOTIFIER_WORKERS:-16}"
export NOTIFIER_RATE_LIMIT_MAX="${NOTIFIER_RATE_LIMIT_MAX:-45}"
export NOTIFIER_RETRY_MAX_ATTEMPTS="${NOTIFIER_RETRY_MAX_ATTEMPTS:-4}"
( cd "$PROJ/app" && "$PY" -m uvicorn main:app --host 127.0.0.1 --port 5000 --workers 1 \
    --no-access-log > "$RUN/app.log" 2>&1 & echo $! > "$RUN/app.pid" )
APP_PID="$(cat "$RUN/app.pid")"

for _ in $(seq 1 60); do
  curl -fsS http://127.0.0.1:3001/docs   > /dev/null 2>&1 \
    && curl -fsS http://127.0.0.1:5000/health > /dev/null 2>&1 && break
  sleep 0.5
done
curl -fsS http://127.0.0.1:5000/health > /dev/null || { echo "la app no arranca"; cat "$RUN/app.log"; exit 1; }

if [ "$CHAOS" = "1" ]; then
  echo "--- modo CAOS: el provider morira a los 15 s"
  ( sleep 15; echo ">>> matando el provider <<<"; kill -9 "$PROV_PID" 2>/dev/null ) &
fi

echo "--- lanzando k6"
TARGET_URL=http://127.0.0.1:5000 k6 run --quiet \
  --out "json=$RUN/raw.json" --summary-export "$RUN/summary.json" \
  "$PROJ/platform/k6/test.js"

echo
"$PY" "$PROJ/tools/score_k6.py" "$RUN/raw.json" "$RUN/summary.json"

echo
echo "=== /internal/metrics al terminar ==="
curl -fsS http://127.0.0.1:5000/internal/metrics | "$PY" -m json.tool
echo
echo "logs y JSON crudo en: $RUN"
