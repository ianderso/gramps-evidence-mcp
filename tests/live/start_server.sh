#!/usr/bin/env bash
# Start a throwaway gramps-webapi for the live suite: loopback only, empty tree.
#
#   tests/live/start_server.sh VERSION [PORT] [DIR]
#
# VERSION picks tests/live/server-VERSION.txt, the server's whole dependency
# set compiled to exact versions, so a run is the same today and next month.
# It installs gramps-webapi from PyPI into DIR/venv, creates one owner-role
# user, and serves on 127.0.0.1:PORT (default 5555). With no Celery configured
# the server runs its background tasks inline, so no Redis or worker is needed.
#
# The system libraries it compiles against are the ones its own Dockerfile.base
# installs. On Ubuntu:
#   sudo apt-get install -y libicu-dev pkg-config libcairo2-dev \
#     libgirepository1.0-dev gir1.2-gtk-3.0
#
# Prints only KEY=value lines on stdout -- the settings tests/live reads -- so
# CI can append them to $GITHUB_ENV, and a shell can `export $(...)` them.
# Everything else goes to stderr. Stop the server with: kill "$(cat DIR/server.pid)"
set -euo pipefail

version=${1:?usage: start_server.sh VERSION [PORT] [DIR]}
port=${2:-5555}
dir=${3:-$(mktemp -d)}
here=$(cd "$(dirname "$0")" && pwd)
lock="$here/server-$version.txt"
if [ ! -f "$lock" ]; then
    echo "No lock file for gramps-webapi $version: $lock" >&2
    exit 1
fi

mkdir -p "$dir"
dir=$(cd "$dir" && pwd)
echo "Installing gramps-webapi $version into $dir/venv" >&2
uv venv -q --python 3.12 "$dir/venv" >&2
VIRTUAL_ENV="$dir/venv" uv pip install -q -r "$lock" >&2

# The settings the official image sets (its Dockerfile), under DIR.
mkdir -p "$dir"/{home,grampsdb,media,export,reports,thumbnails,request_cache,persistent_cache}
touch "$dir/config.cfg"
export GRAMPSHOME="$dir/home"
export GRAMPS_DATABASE_PATH="$dir/grampsdb"
export GRAMPSWEB_TREE="Throwaway"
secret="throwaway-live-test-secret-key-not-for-real-use"
export GRAMPSWEB_SECRET_KEY="$secret"
export GRAMPSWEB_USER_DB_URI="sqlite:///$dir/users.sqlite"
export GRAMPSWEB_MEDIA_BASE_DIR="$dir/media"
export GRAMPSWEB_SEARCH_INDEX_DB_URI="sqlite:///$dir/search_index.db"
export GRAMPSWEB_EXPORT_DIR="$dir/export"
export GRAMPSWEB_REPORT_DIR="$dir/reports"
export GRAMPSWEB_THUMBNAIL_CACHE_CONFIG__CACHE_DIR="$dir/thumbnails"
export GRAMPSWEB_REQUEST_CACHE_CONFIG__CACHE_DIR="$dir/request_cache"
export GRAMPSWEB_PERSISTENT_CACHE_CONFIG__CACHE_DIR="$dir/persistent_cache"

python="$dir/venv/bin/python"
password="live-test-password"
# Creates the user tables on a fresh database. The tree is created on first start.
"$python" -m gramps_webapi --config "$dir/config.cfg" user add mcp "$password" --role 4 >&2

nohup "$python" -m gramps_webapi --config "$dir/config.cfg" \
    run --host 127.0.0.1 --port "$port" > "$dir/server.log" 2>&1 &
echo $! > "$dir/server.pid"

url="http://127.0.0.1:$port"
for _ in $(seq 1 90); do
    if curl -sf -o /dev/null -X POST -H 'Content-Type: application/json' \
        -d "{\"username\":\"mcp\",\"password\":\"$password\"}" "$url/api/token/"; then
        echo "gramps-webapi $version is serving at $url (log: $dir/server.log)" >&2
        echo "GRAMPS_LIVE_URL=$url"
        echo "GRAMPS_LIVE_USERNAME=mcp"
        echo "GRAMPS_LIVE_PASSWORD=$password"
        echo "GRAMPS_LIVE_VERSION=$version"
        # Signs the expired token the token-renewal test needs; throwaway only.
        echo "GRAMPS_LIVE_SECRET_KEY=$secret"
        echo "GRAMPS_LIVE_LOG=$dir/server.log"
        exit 0
    fi
    sleep 1
done
echo "gramps-webapi did not start within 90 seconds. Its log:" >&2
cat "$dir/server.log" >&2
exit 1
