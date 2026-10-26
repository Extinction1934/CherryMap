#!/bin/sh
# Revive CherryMap: local geographic index, CherryPro worker, then the preview server.
set -eu
cd /workspace

PY="$(ls -d /root/.local/share/uv/python/cpython-3.12*/bin/python3.12 2>/dev/null | head -1)"
if [ -z "$PY" ] || [ ! -x "$PY" ]; then
  echo "CherryMap Python runtime is missing" >&2
  exit 1
fi

INDEX_URL="mysql://cherrymap:local-index@127.0.0.1:3306/cherrymap"

index_up() {
  "$PY" -c "import pymysql; pymysql.connect(host='127.0.0.1', port=3306, user='cherrymap', password='local-index', database='cherrymap', connect_timeout=2).close()" >/dev/null 2>&1
}

if ! index_up; then
  cd /workspace/cherrymap
  "$PY" -m server.local_index_mysql >> /tmp/cherrymap-index-mysql.log 2>&1 &
  echo $! > /tmp/cherrymap-index-mysql.pid
  cd /workspace
  i=0
  while [ "$i" -lt 40 ]; do
    if index_up; then
      break
    fi
    i=$((i + 1))
    sleep 0.25
  done
  if ! index_up; then
    echo "CherryMap geographic index did not start" >&2
    exit 1
  fi
fi

imported="$("$PY" -c "
import sqlite3
from pathlib import Path
path = Path('/workspace/cherrymap/data/geonames.sqlite')
if not path.is_file():
    print('no')
else:
    row = sqlite3.connect(path).execute(\"select status, record_count from geo_imports where dataset_key='geonames-cities500'\").fetchone()
    print('yes' if row and row[0] == 'complete' and int(row[1]) > 1000 else 'no')
")"
if [ "$imported" != "yes" ]; then
  if [ ! -f /workspace/cherrymap/data/cities500.zip ]; then
    echo "GeoNames cities500 archive is missing" >&2
    exit 1
  fi
  cd /workspace/cherrymap
  DATABASE_URL="$INDEX_URL" "$PY" -m server.import_geonames data/cities500.zip
  cd /workspace
fi

kill_tree() {
  pid="$1"
  if [ -r "/proc/$pid/task/$pid/children" ]; then
    for child in $(cat "/proc/$pid/task/$pid/children"); do
      kill_tree "$child"
    done
  fi
  kill "$pid" 2>/dev/null || true
}

WORKER_PID_FILE="/tmp/cherrymap-worker.pid"
need_worker=1
if [ -f "$WORKER_PID_FILE" ] && kill -0 "$(cat "$WORKER_PID_FILE")" 2>/dev/null; then
  if tr '\0' '\n' < "/proc/$(cat "$WORKER_PID_FILE")/environ" | grep -q '^DATABASE_URL=mysql://cherrymap:'; then
    need_worker=0
  else
    kill_tree "$(cat "$WORKER_PID_FILE")"
    sleep 0.5
  fi
fi
if [ "$need_worker" -eq 1 ]; then
  for proc in /proc/[0-9]*; do
    cmd=$(tr '\0' ' ' < "$proc/cmdline" 2>/dev/null || true)
    case "$cmd" in
      *"-m server.main"*) kill "$(basename "$proc")" 2>/dev/null || true ;;
    esac
  done
  sleep 0.5
  cd /workspace/cherrymap
  # MySQL URL is only for this worker. The preview server must not inherit it.
  env -u MANUS_API_URL -u MANUS_API_KEY -u MANUS_API_BROWSER_KEY \
    DATABASE_URL="$INDEX_URL" \
    PORT=8095 \
    "$PY" -m server.main >> /tmp/cherrymap-worker.log 2>&1 &
  echo $! > "$WORKER_PID_FILE"
  cd /workspace
fi

if curl -sf -o /dev/null --max-time 1 http://127.0.0.1:8080/; then
  exit 0
fi

npm run dev >> /tmp/cherrymap-dev.log 2>&1 &
exit 0
