#!/usr/bin/env bash
set -uo pipefail

BASE="https://orzeczenia.nsa.gov.pl"
OUT="${GITHUB_WORKSPACE:-$(pwd)}/nsa-poc-output"
COOKIE_JAR="$OUT/cookies.txt"
REPORT="$OUT/NSA_POC_REPORT.md"
PROXY="http://127.0.0.1:8080"
DATE="2025-01-15"
DELAY=12

UA='Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36'

mkdir -p "$OUT"
rm -f "$COOKIE_JAR"
touch "$COOKIE_JAR"

cat > "$REPORT" <<EOF
# NSA CBO — proof of concept przez hola-proxy

- Data wyszukiwania: \`$DATE\`
- Proxy: \`$PROXY\`
- Odstęp: **$DELAY sekund**
- Strony testowe: **1–4**

EOF

request() {
    local name="$1"
    local method="$2"
    local url="$3"
    local referer="${4:-}"
    local payload="${5:-}"

    local body="$OUT/${name}.html"
    local headers="$OUT/${name}.headers.txt"
    local metrics="$OUT/${name}.metrics.txt"
    local stderr_file="$OUT/${name}.stderr.txt"

    local args=(
        curl
        --silent
        --show-error
        --location
        --http1.1
        --proxy "$PROXY"
        --connect-timeout 30
        --max-time 180
        --user-agent "$UA"
        --cookie "$COOKIE_JAR"
        --cookie-jar "$COOKIE_JAR"
        --dump-header "$headers"
        --output "$body"
        --write-out '%{http_code}|%{remote_ip}|%{size_download}|%{time_starttransfer}|%{time_total}|%{url_effective}'
    )

    if [[ -n "$referer" ]]; then
        args+=(--referer "$referer")
    fi

    if [[ "$method" == "POST" ]]; then
        args+=(--request POST)
        args+=(--header 'Content-Type: application/x-www-form-urlencoded')
        args+=(--data "$payload")
    fi

    set +e
    "${args[@]}" "$url" >"$metrics" 2>"$stderr_file"
    local exit_code=$?
    set -e

    local metric_line=""
    [[ -f "$metrics" ]] && metric_line="$(cat "$metrics")"

    local status
    status="$(printf '%s' "$metric_line" | cut -d'|' -f1)"

    local size=0
    [[ -f "$body" ]] && size="$(wc -c < "$body" | tr -d ' ')"

    echo "$exit_code|${status:-0}|$size"

    {
        echo "## Request: $name"
        echo
        echo "- Metoda: \`$method\`"
        echo "- URL: \`$url\`"
        echo "- curl exit: **$exit_code**"
        echo "- Metryki: \`${metric_line:-brak}\`"
        echo "- Rozmiar body: **$size B**"
        echo "- stderr: \`$(tr '\n' ' ' < "$stderr_file" 2>/dev/null || true)\`"
        echo
    } >> "$REPORT"
}

echo "============================================================"
echo "NSA CBO — PROOF OF CONCEPT PRZEZ HOLA-PROXY"
echo "============================================================"

echo "[0/5] Sprawdzam publiczne IP proxy"

set +e
PROXY_IP="$(
    curl \
        --silent \
        --show-error \
        --proxy "$PROXY" \
        --connect-timeout 20 \
        --max-time 60 \
        https://api.ipify.org 2>"$OUT/proxy-ip.stderr.txt"
)"
PROXY_IP_EXIT=$?
set -e

echo "Proxy IP: ${PROXY_IP:-BRAK}"
echo "curl exit: $PROXY_IP_EXIT"

{
    echo "## Proxy"
    echo
    echo "- Publiczne IP: \`${PROXY_IP:-brak}\`"
    echo "- curl exit: **$PROXY_IP_EXIT**"
    echo "- stderr: \`$(tr '\n' ' ' < "$OUT/proxy-ip.stderr.txt" 2>/dev/null || true)\`"
    echo
} >> "$REPORT"

echo "[1/5] GET /cbo/query"

RESULT="$(
    request \
        "01-query" \
        "GET" \
        "$BASE/cbo/query"
)"

IFS='|' read -r CURL_EXIT HTTP_STATUS BODY_SIZE <<<"$RESULT"

echo "curl=$CURL_EXIT HTTP=$HTTP_STATUS body=$BODY_SIZE B"

if [[ "$CURL_EXIT" != "0" || "$HTTP_STATUS" != "200" || "$BODY_SIZE" -lt 1000 ]]; then
    echo "GET /cbo/query nieudany — zatrzymuję test."
    exit 10
fi

echo "Przerwa: $DELAY s"
sleep "$DELAY"

PAYLOAD='sad=Naczelny+S%C4%85d+Administracyjny&symbole=&wszystkieSlowa=&takUzasadnienie=on&wystepowanie=gdziekolwiek&odmiana=on&sygnatura=&rodzaj=dowolny&odDaty=2025-01-15&doDaty=2025-01-15&sedziowie=&funkcja=dowolna&rodzaj_organu=&hasla=&akty=&przepisy=&publikacje=&glosy=&submit=Szukaj'

echo "[2/5] POST /cbo/search"

RESULT="$(
    request \
        "02-search-page-1" \
        "POST" \
        "$BASE/cbo/search" \
        "$BASE/cbo/query" \
        "$PAYLOAD"
)"

IFS='|' read -r CURL_EXIT HTTP_STATUS BODY_SIZE <<<"$RESULT"

echo "curl=$CURL_EXIT HTTP=$HTTP_STATUS body=$BODY_SIZE B"

if [[ "$CURL_EXIT" != "0" || "$HTTP_STATUS" != "200" || "$BODY_SIZE" -lt 1000 ]]; then
    echo "POST /cbo/search nieudany — zatrzymuję test."
    exit 20
fi

PREVIOUS_URL="$BASE/cbo/search"

for PAGE in 2 3 4; do
    echo "Przerwa: $DELAY s"
    sleep "$DELAY"

    echo "[$((PAGE + 1))/5] GET /cbo/find?p=$PAGE"

    RESULT="$(
        request \
            "0$((PAGE + 1))-find-page-$PAGE" \
            "GET" \
            "$BASE/cbo/find?p=$PAGE" \
            "$PREVIOUS_URL"
    )"

    IFS='|' read -r CURL_EXIT HTTP_STATUS BODY_SIZE <<<"$RESULT"

    echo "curl=$CURL_EXIT HTTP=$HTTP_STATUS body=$BODY_SIZE B"

    if [[ "$CURL_EXIT" != "0" || "$HTTP_STATUS" != "200" || "$BODY_SIZE" -lt 1000 ]]; then
        echo "Strona $PAGE nieudana — kończę bez dalszych prób."
        break
    fi

    PREVIOUS_URL="$BASE/cbo/find?p=$PAGE"
done

python3 - "$OUT" "$REPORT" <<'PY'
from __future__ import annotations

import html
import re
import sys
from pathlib import Path

out = Path(sys.argv[1])
report = Path(sys.argv[2])

rows = []
all_primary_ids = []

for path in sorted(out.glob("*.html")):
    source = path.read_text(encoding="utf-8", errors="replace")

    clean = re.sub(r"<script\b.*?</script>", " ", source, flags=re.I | re.S)
    clean = re.sub(r"<style\b.*?</style>", " ", clean, flags=re.I | re.S)
    clean = re.sub(r"<[^>]+>", " ", clean)
    clean = html.unescape(clean)
    clean = re.sub(r"\s+", " ", clean).strip()

    total_match = re.search(
        r"Znaleziono\s+([\d\s]+)\s+orzecze",
        clean,
        flags=re.I,
    )

    page_match = re.search(
        r"Str\.\s*(\d+)\s+z\s+(\d+)",
        clean,
        flags=re.I,
    )

    tables = re.findall(
        r"""<table\b[^>]*id=["']tab_nsa[^"']*["'][^>]*>.*?</table>""",
        source,
        flags=re.I | re.S,
    )

    primary_ids = []

    for table in tables:
        match = re.search(
            r"""href=["']/doc/([0-9A-Fa-f]{10})["']""",
            table,
            flags=re.I,
        )
        if match:
            primary_ids.append(match.group(1).upper())

    all_primary_ids.extend(primary_ids)

    rows.append(
        {
            "file": path.name,
            "bytes": path.stat().st_size,
            "total": (
                int(re.sub(r"\s+", "", total_match.group(1)))
                if total_match
                else None
            ),
            "current_page": (
                int(page_match.group(1))
                if page_match
                else None
            ),
            "total_pages": (
                int(page_match.group(2))
                if page_match
                else None
            ),
            "tables": len(tables),
            "primary_ids": primary_ids,
            "captcha": bool(
                re.search(
                    r"captcha|kod\s+z\s+obrazka|robot",
                    clean,
                    flags=re.I,
                )
            ),
        }
    )

with report.open("a", encoding="utf-8") as handle:
    handle.write("## Analiza HTML\n\n")
    handle.write(
        "| Plik | Bajty | Wyniki ogółem | Strona | "
        "Tabele tab_nsa | Główne ID | CAPTCHA |\n"
    )
    handle.write(
        "|---|---:|---:|---|---:|---:|---|\n"
    )

    for row in rows:
        page_value = (
            f"{row['current_page']}/{row['total_pages']}"
            if row["current_page"] is not None
            else "—"
        )

        handle.write(
            f"| `{row['file']}` "
            f"| {row['bytes']} "
            f"| {row['total'] if row['total'] is not None else '—'} "
            f"| {page_value} "
            f"| {row['tables']} "
            f"| {len(row['primary_ids'])} "
            f"| {'TAK' if row['captcha'] else 'nie'} |\n"
        )

    handle.write("\n## Unikalność\n\n")
    handle.write(
        f"- Główne ID łącznie: **{len(all_primary_ids)}**\n"
    )
    handle.write(
        f"- Unikalne główne ID: **{len(set(all_primary_ids))}**\n"
    )
    handle.write(
        f"- Duplikaty: **{len(all_primary_ids) - len(set(all_primary_ids))}**\n"
    )

    handle.write("\n## ID głównych orzeczeń\n\n")

    for value in all_primary_ids:
        handle.write(f"- `{value}`\n")

print()
print("ANALIZA:")
for row in rows:
    print(
        f"{row['file']}: "
        f"{row['bytes']} B, "
        f"strona={row['current_page']}/{row['total_pages']}, "
        f"główne={len(row['primary_ids'])}, "
        f"captcha={row['captcha']}"
    )

print(f"Główne ID: {len(all_primary_ids)}")
print(f"Unikalne ID: {len(set(all_primary_ids))}")
PY

echo
echo "============================================================"
echo "TEST ZAKOŃCZONY"
echo "============================================================"
cat "$REPORT"
