#!/usr/bin/env bash
# 번역기 cloudflared quick tunnel URL 을 GitHub secret gist 의 translator.json 에 게시.
#
# 재고 서버용 inventory_monitor/tunnel_publisher/publish-tunnel-url.sh 와 같은 gist 와
# 토큰(~/.github_gist_token)을 쓰고, 파일만 translator.json 으로 다릅니다.
# 파일 이름이 monitor.json 보다 뒤에 정렬되므로 gist 의 /raw (첫 파일) 는 계속 monitor.json 입니다.
set -euo pipefail

GIST_ID="${TRANSLATOR_GIST_ID:-5a69e99d96fa2ae34ba4af96c117d5e0}"
GIST_OWNER="${TRANSLATOR_GIST_OWNER:-beiko3444}"
GIST_FILE="translator.json"
TOKEN_FILE="${HOME}/.github_gist_token"
SERVICE="kr-translator-tunnel.service"
LOG_TAG="publish-translator-url"

log() { echo "[$(date -Iseconds)] [${LOG_TAG}] $*"; }

if [[ ! -r "${TOKEN_FILE}" ]]; then
    log "ERROR: token file not found: ${TOKEN_FILE}"
    exit 2
fi
TOKEN="$(tr -d '[:space:]' < "${TOKEN_FILE}")"
if [[ -z "${TOKEN}" ]]; then
    log "ERROR: token file empty"
    exit 2
fi

# Prints nothing (and still succeeds) while cloudflared has not logged a URL yet.
extract_url() {
    journalctl -u "${SERVICE}" -b --no-pager 2>/dev/null \
        | grep -Eio 'https://[a-z0-9-]+\.trycloudflare\.com' \
        | grep -v '^https://api\.trycloudflare\.com$' \
        | tail -1 || true
}

check_tunnel() {
    curl -sSf --max-time 8 "${1}/healthz" > /dev/null
}

wait_for_url() {
    local deadline=$(( $(date +%s) + 60 )) candidate=""
    while [[ "$(date +%s)" -lt ${deadline} ]]; do
        candidate="$(extract_url)"
        if [[ -n "${candidate}" && "${candidate}" != "${1:-}" ]] && check_tunnel "${candidate}"; then
            echo "${candidate}"
            return 0
        fi
        sleep 3
    done
    return 1
}

TUNNEL_URL="$(extract_url)"
if [[ -z "${TUNNEL_URL}" ]] || ! check_tunnel "${TUNNEL_URL}"; then
    log "tunnel not answering yet (${TUNNEL_URL:-no url}); waiting"
    if ! TUNNEL_URL="$(wait_for_url "")"; then
        STALE_URL="$(extract_url)"
        log "tunnel stale; restarting ${SERVICE}"
        if ! sudo -n systemctl restart "${SERVICE}"; then
            log "ERROR: failed to restart ${SERVICE}"
            exit 4
        fi
        if ! TUNNEL_URL="$(wait_for_url "${STALE_URL}")"; then
            log "ERROR: replacement tunnel did not become healthy within 60s"
            exit 4
        fi
    fi
fi
log "tunnel healthy: ${TUNNEL_URL}"

CURRENT_URL="$(curl -sS --max-time 10 "https://gist.githubusercontent.com/${GIST_OWNER}/${GIST_ID}/raw/${GIST_FILE}?t=$(date +%s)" \
                | python3 -c 'import json,sys; print(json.load(sys.stdin).get("url",""))' 2>/dev/null || true)"
if [[ "${CURRENT_URL}" == "${TUNNEL_URL}" ]]; then
    log "gist already up-to-date, skip"
    exit 0
fi

PAYLOAD="$(python3 -c "import json,sys; print(json.dumps({'files': {sys.argv[2]: {'content': json.dumps({'url': sys.argv[1]})}}}))" "${TUNNEL_URL}" "${GIST_FILE}")"
RESPONSE_FILE="$(mktemp)"
HTTP_CODE="$(curl -sS -o "${RESPONSE_FILE}" -w '%{http_code}' \
    -X PATCH \
    -H "Authorization: token ${TOKEN}" \
    -H "Accept: application/vnd.github+json" \
    -H "X-GitHub-Api-Version: 2022-11-28" \
    --max-time 20 \
    --data "${PAYLOAD}" \
    "https://api.github.com/gists/${GIST_ID}")"

if [[ "${HTTP_CODE}" != "200" ]]; then
    log "ERROR: gist PATCH failed HTTP ${HTTP_CODE}:"
    cat "${RESPONSE_FILE}" >&2 || true
    rm -f "${RESPONSE_FILE}"
    exit 5
fi
rm -f "${RESPONSE_FILE}"
log "gist ${GIST_FILE} updated OK (${TUNNEL_URL})"
