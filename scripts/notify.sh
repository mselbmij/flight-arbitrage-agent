#!/bin/sh
# Last-resort ntfy ping from shell, for failures Python cannot report itself: a backup that failed,
# or `fares` that will not start at all (missing venv, broken install). ARCHITECTURE.md section 7a.
#
#   scripts/notify.sh "fare tracker: backup failed" "see ops/backup.log"
#
# NTFY_SERVER / NTFY_TOPIC / NTFY_TOKEN come from the environment, else from .env. Priority 4.
# Uses curl, else python3. Exits 1 if there is no topic or the POST did not succeed.
set -eu

cd "$(dirname "$0")/.."

TITLE=${1:?usage: notify.sh TITLE [BODY]}
BODY=${2:-$TITLE}

from_env_file() {
    [ -f .env ] || return 0
    sed -n "s/^$1=//p" .env | tail -n 1 | sed 's/[[:space:]]*#.*$//; s/^["'"'"']//; s/["'"'"']$//'
}

NTFY_SERVER=${NTFY_SERVER:-$(from_env_file NTFY_SERVER)}
NTFY_TOPIC=${NTFY_TOPIC:-$(from_env_file NTFY_TOPIC)}
NTFY_TOKEN=${NTFY_TOKEN:-$(from_env_file NTFY_TOKEN)}
NTFY_SERVER=${NTFY_SERVER:-https://ntfy.sh}

if [ -z "$NTFY_TOPIC" ]; then
    echo "notify: NTFY_TOPIC is not set, not sent: $TITLE" >&2
    exit 1
fi
URL="${NTFY_SERVER%/}/$NTFY_TOPIC"

if command -v curl >/dev/null 2>&1; then
    if [ -n "$NTFY_TOKEN" ]; then
        curl -fsS -m 10 -o /dev/null -H "Title: $TITLE" -H "Priority: 4" -H "Tags: warning" \
            -H "Authorization: Bearer $NTFY_TOKEN" --data-binary "$BODY" "$URL"
    else
        curl -fsS -m 10 -o /dev/null -H "Title: $TITLE" -H "Priority: 4" -H "Tags: warning" \
            --data-binary "$BODY" "$URL"
    fi
else
    NTFY_TOKEN=$NTFY_TOKEN python3 - "$URL" "$TITLE" "$BODY" <<'PY'
import os
import sys
import urllib.request

url, title, body = sys.argv[1:4]
headers = {"Title": title.encode("latin-1", "ignore").decode("latin-1"), "Priority": "4",
           "Tags": "warning"}
if os.environ.get("NTFY_TOKEN"):
    headers["Authorization"] = "Bearer " + os.environ["NTFY_TOKEN"]
req = urllib.request.Request(url, data=body.encode(), headers=headers, method="POST")
urllib.request.urlopen(req, timeout=10).close()
PY
fi
echo "notify: sent: $TITLE"
