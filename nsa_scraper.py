#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
import json
import os
import random
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

BASE_URL = "https://orzeczenia.nsa.gov.pl"
QUERY_URL = f"{BASE_URL}/cbo/query"
SEARCH_URL = f"{BASE_URL}/cbo/search"

DEFAULT_PROXY = "http://127.0.0.1:8080"
DEFAULT_FROM_DATE = "2025-01-15"
DEFAULT_TO_DATE = "2025-01-15"
DEFAULT_COURT = "Naczelny Sąd Administracyjny"

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/139.0.0.0 Safari/537.36"
)


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def clean_text(value: str) -> str:
    value = re.sub(
        r"<script\b.*?</script>",
        " ",
        value,
        flags=re.I | re.S,
    )
    value = re.sub(
        r"<style\b.*?</style>",
        " ",
        value,
        flags=re.I | re.S,
    )
    value = re.sub(r"<br\s*/?>", "\n", value, flags=re.I)
    value = re.sub(r"</(?:p|div|tr|li|table|h\d)>", "\n", value, flags=re.I)
    value = re.sub(r"<[^>]+>", " ", value)
    value = html.unescape(value)
    value = value.replace("\xa0", " ")
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\s*\n\s*", "\n", value)
    value = re.sub(r"\n{2,}", "\n", value)
    return value.strip()


def safe_int(value: str | None) -> int | None:
    if value is None:
        return None

    normalized = re.sub(r"\D", "", value)
    return int(normalized) if normalized else None


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def extract_cookie_value(cookie_jar: Path, name: str) -> str | None:
    if not cookie_jar.exists():
        return None

    for line in cookie_jar.read_text(
        encoding="utf-8",
        errors="replace",
    ).splitlines():
        if not line or line.startswith("#"):
            continue

        fields = line.split("\t")

        if len(fields) >= 7 and fields[5] == name:
            return fields[6]

    return None


def request(
    *,
    url: str,
    cookie_jar: Path,
    proxy: str,
    method: str = "GET",
    payload: dict[str, str] | None = None,
    referer: str | None = None,
    timeout: int = 180,
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="nsa-request-") as temp_dir:
        root = Path(temp_dir)
        body_path = root / "body.html"
        headers_path = root / "headers.txt"
        stderr_path = root / "stderr.txt"

        command = [
            "curl",
            "--silent",
            "--show-error",
            "--location",
            "--http1.1",
            "--proxy",
            proxy,
            "--connect-timeout",
            "30",
            "--max-time",
            str(timeout),
            "--user-agent",
            USER_AGENT,
            "--cookie",
            str(cookie_jar),
            "--cookie-jar",
            str(cookie_jar),
            "--dump-header",
            str(headers_path),
            "--output",
            str(body_path),
            "--write-out",
            (
                "%{http_code}|%{remote_ip}|%{size_download}|"
                "%{time_starttransfer}|%{time_total}|%{url_effective}"
            ),
        ]

        if referer:
            command.extend(["--referer", referer])

        if method == "POST":
            command.extend(
                [
                    "--request",
                    "POST",
                    "--header",
                    "Content-Type: application/x-www-form-urlencoded",
                    "--data",
                    urlencode(payload or {}),
                ]
            )

        command.append(url)

        started = time.monotonic()

        with stderr_path.open("wb") as stderr_file:
            process = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=stderr_file,
                timeout=timeout + 30,
                check=False,
            )

        elapsed = time.monotonic() - started

        metrics = process.stdout.decode(
            "utf-8",
            errors="replace",
        ).strip()

        parts = metrics.split("|", 5)

        def metric(index: int, default: str = "") -> str:
            return parts[index] if len(parts) > index else default

        try:
            http_status = int(metric(0, "0"))
        except ValueError:
            http_status = 0

        body = (
            body_path.read_text(
                encoding="utf-8",
                errors="replace",
            )
            if body_path.exists()
            else ""
        )

        headers = (
            headers_path.read_text(
                encoding="utf-8",
                errors="replace",
            )
            if headers_path.exists()
            else ""
        )

        stderr = (
            stderr_path.read_text(
                encoding="utf-8",
                errors="replace",
            ).strip()
            if stderr_path.exists()
            else ""
        )

        return {
            "url": url,
            "method": method,
            "curl_exit": process.returncode,
            "http_status": http_status,
            "remote_ip": metric(1),
            "size_download": safe_int(metric(2)) or len(body.encode("utf-8")),
            "time_starttransfer": metric(3),
            "time_total": metric(4),
            "effective_url": metric(5),
            "elapsed": elapsed,
            "body": body,
            "headers": headers,
            "stderr": stderr,
            "body_sha256": sha256_text(body),
        }


def request_with_retries(
    *,
    url: str,
    cookie_jar: Path,
    proxy: str,
    method: str = "GET",
    payload: dict[str, str] | None = None,
    referer: str | None = None,
    attempts: int = 3,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    history: list[dict[str, Any]] = []
    delays = [30, 60, 120]

    for attempt in range(1, attempts + 1):
        result = request(
            url=url,
            cookie_jar=cookie_jar,
            proxy=proxy,
            method=method,
            payload=payload,
            referer=referer,
        )

        result["attempt"] = attempt
        history.append(result)

        body_size = len(result["body"].encode("utf-8"))

        successful = (
            result["curl_exit"] == 0
            and result["http_status"] == 200
            and body_size >= 1000
        )

        if successful:
            return result, history

        if attempt < attempts:
            delay = delays[min(attempt - 1, len(delays) - 1)]
            print(
                f"      próba {attempt} nieudana: "
                f"curl={result['curl_exit']} "
                f"HTTP={result['http_status']} "
                f"body={body_size} B; retry za {delay} s"
            )
            time.sleep(delay)

    return history[-1], history


def parse_summary(source: str) -> dict[str, Any]:
    visible = clean_text(source)

    total_match = re.search(
        r"Znaleziono\s+([\d\s]+)\s+orzecze",
        visible,
        flags=re.I,
    )

    page_match = re.search(
        r"Str\.\s*(\d+)\s+z\s+(\d+)",
        visible,
        flags=re.I,
    )

    captcha = bool(
        re.search(
            r"captcha|kod\s+z\s+obrazka|nie\s+jestem\s+robotem",
            visible,
            flags=re.I,
        )
    )

    return {
        "total_results": (
            safe_int(total_match.group(1))
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
        "captcha": captcha,
    }


def split_primary_tables(source: str) -> list[str]:
    return re.findall(
        r"""<table\b[^>]*id=["']tab_nsa[^"']*["'][^>]*>.*?</table>""",
        source,
        flags=re.I | re.S,
    )


def extract_label_value(
    source: str,
    labels: list[str],
) -> str | None:
    for label in labels:
        pattern = (
            rf"(?:^|\n)\s*{re.escape(label)}\s*[:\-]\s*"
            rf"(.+?)(?=\n[A-ZĄĆĘŁŃÓŚŹŻ][^:\n]{{1,50}}\s*[:\-]|\Z)"
        )

        match = re.search(
            pattern,
            source,
            flags=re.I | re.S,
        )

        if match:
            value = re.sub(
                r"\s+",
                " ",
                match.group(1),
            ).strip(" \n\t:-")

            if value:
                return value

    return None


def parse_primary_record(
    table_html: str,
    *,
    page_number: int,
    position: int,
    query_from_date: str,
    query_to_date: str,
    scraped_at: str,
) -> dict[str, Any] | None:
    links = list(
        re.finditer(
            r"""<a\b[^>]*href=["']/doc/([0-9A-Fa-f]{10})["'][^>]*>(.*?)</a>""",
            table_html,
            flags=re.I | re.S,
        )
    )

    if not links:
        return None

    primary_link = links[0]
    cbo_id = primary_link.group(1).upper()
    title = clean_text(primary_link.group(2))
    visible = clean_text(table_html)

    related_ids: list[str] = []

    for related_block in re.findall(
        r"""<span\b[^>]*class=["'][^"']*powiazane[^"']*["'][^>]*>(.*?)</span>""",
        table_html,
        flags=re.I | re.S,
    ):
        related_ids.extend(
            match.group(1).upper()
            for match in re.finditer(
                r"""href=["']/doc/([0-9A-Fa-f]{10})["']""",
                related_block,
                flags=re.I,
            )
        )

    title_match = re.match(
        r"(?P<signature>.+?)\s*-\s*"
        r"(?P<judgment_type>Wyrok|Postanowienie|Uchwała|Zarządzenie)"
        r"\s+(?P<court>.+?)\s+z\s+"
        r"(?P<date>\d{4}-\d{2}-\d{2})$",
        title,
        flags=re.I,
    )

    signature = None
    judgment_type = None
    court = None
    judgment_date = None

    if title_match:
        signature = title_match.group("signature").strip()
        judgment_type = title_match.group("judgment_type").strip()
        court = title_match.group("court").strip()
        judgment_date = title_match.group("date").strip()
    else:
        date_match = re.search(
            r"\b(\d{4}-\d{2}-\d{2})\b",
            title,
        )
        judgment_date = date_match.group(1) if date_match else None

    judges = extract_label_value(
        visible,
        ["Sędziowie", "Skład orzekający"],
    )

    symbols = extract_label_value(
        visible,
        ["Symbole", "Symbol sprawy", "Symbole sprawy"],
    )

    keywords = extract_label_value(
        visible,
        ["Hasła tematyczne", "Hasła", "Hasło tematyczne"],
    )

    authority = extract_label_value(
        visible,
        ["Organ", "Rodzaj organu"],
    )

    result_text = extract_label_value(
        visible,
        ["Treść wyniku", "Wynik"],
    )

    provisions = extract_label_value(
        visible,
        ["Powołane przepisy", "Przepisy"],
    )

    return {
        "cbo_id": cbo_id,
        "url": f"{BASE_URL}/doc/{cbo_id}",
        "title": title,
        "signature": signature,
        "judgment_date": judgment_date,
        "court": court,
        "judgment_type": judgment_type,
        "judges": judges,
        "symbols": symbols,
        "keywords": keywords,
        "authority": authority,
        "result_text": result_text,
        "provisions": provisions,
        "related_cbo_ids": sorted(set(related_ids)),
        "source_page": page_number,
        "source_position": position,
        "source_query_from_date": query_from_date,
        "source_query_to_date": query_to_date,
        "raw_result_text": visible,
        "scraped_at": scraped_at,
    }


def parse_page_records(
    source: str,
    *,
    page_number: int,
    query_from_date: str,
    query_to_date: str,
    scraped_at: str,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []

    for position, table_html in enumerate(
        split_primary_tables(source),
        start=1,
    ):
        record = parse_primary_record(
            table_html,
            page_number=page_number,
            position=position,
            query_from_date=query_from_date,
            query_to_date=query_to_date,
            scraped_at=scraped_at,
        )

        if record:
            records.append(record)

    return records


def load_checkpoint(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_checkpoint(
    path: Path,
    checkpoint: dict[str, Any],
) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(
            checkpoint,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def load_manifest(path: Path) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}

    if not path.exists():
        return records

    for line in path.read_text(
        encoding="utf-8",
        errors="replace",
    ).splitlines():
        line = line.strip()

        if not line:
            continue

        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue

        cbo_id = record.get("cbo_id")

        if cbo_id:
            records[cbo_id] = record

    return records


def save_manifest(
    path: Path,
    records: dict[str, dict[str, Any]],
) -> None:
    temporary = path.with_suffix(".tmp")

    ordered = sorted(
        records.values(),
        key=lambda record: (
            record.get("judgment_date") or "",
            record.get("signature") or "",
            record.get("cbo_id") or "",
        ),
    )

    with temporary.open("w", encoding="utf-8") as handle:
        for record in ordered:
            handle.write(
                json.dumps(
                    record,
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
            )

    temporary.replace(path)


def get_proxy_ip(proxy: str) -> dict[str, Any]:
    command = [
        "curl",
        "--silent",
        "--show-error",
        "--proxy",
        proxy,
        "--connect-timeout",
        "20",
        "--max-time",
        "60",
        "https://api.ipify.org",
    ]

    process = subprocess.run(
        command,
        capture_output=True,
        timeout=70,
        check=False,
    )

    return {
        "exit_code": process.returncode,
        "ip": process.stdout.decode(
            "utf-8",
            errors="replace",
        ).strip(),
        "stderr": process.stderr.decode(
            "utf-8",
            errors="replace",
        ).strip(),
    }


def make_payload(
    from_date: str,
    to_date: str,
    court: str,
) -> dict[str, str]:
    return {
        "sad": court,
        "symbole": "",
        "wszystkieSlowa": "",
        "takUzasadnienie": "on",
        "wystepowanie": "gdziekolwiek",
        "odmiana": "on",
        "sygnatura": "",
        "rodzaj": "dowolny",
        "odDaty": from_date,
        "doDaty": to_date,
        "sedziowie": "",
        "funkcja": "dowolna",
        "rodzaj_organu": "",
        "hasla": "",
        "akty": "",
        "przepisy": "",
        "publikacje": "",
        "glosy": "",
        "submit": "Szukaj",
    }


def markdown_escape(value: Any) -> str:
    if value is None:
        return "—"

    return (
        str(value)
        .replace("|", "\\|")
        .replace("\n", " ")
    )


def write_audit(
    *,
    path: Path,
    started_at: str,
    finished_at: str,
    from_date: str,
    to_date: str,
    court: str,
    proxy: str,
    proxy_info: dict[str, Any],
    summary: dict[str, Any],
    pages: list[dict[str, Any]],
    records: dict[str, dict[str, Any]],
    manifest_path: Path,
    checkpoint_path: Path,
    failed_page: int | None,
    resumed: bool,
) -> None:
    ordered_records = list(records.values())

    field_counts = {
        field: sum(
            1
            for record in ordered_records
            if record.get(field)
        )
        for field in [
            "signature",
            "judgment_date",
            "court",
            "judgment_type",
            "judges",
            "symbols",
            "keywords",
            "authority",
            "result_text",
            "provisions",
        ]
    }

    lines: list[str] = []

    lines.append("# NSA / CBO — audyt manifestu")
    lines.append("")
    lines.append(f"- Start: `{started_at}`")
    lines.append(f"- Koniec: `{finished_at}`")
    lines.append(f"- Sąd: `{court}`")
    lines.append(f"- Zakres dat: `{from_date}` — `{to_date}`")
    lines.append(f"- Proxy: `{proxy}`")
    lines.append(
        f"- Publiczne IP proxy: `{proxy_info.get('ip') or 'brak'}`"
    )
    lines.append(
        f"- Wznowienie z checkpointu: **{'tak' if resumed else 'nie'}**"
    )
    lines.append("")

    lines.append("## Wynik")
    lines.append("")
    lines.append(
        f"- Wyniki deklarowane przez CBO: "
        f"**{summary.get('total_results')}**"
    )
    lines.append(
        f"- Strony deklarowane przez CBO: "
        f"**{summary.get('total_pages')}**"
    )
    lines.append(
        f"- Strony pobrane w tym uruchomieniu: **{len(pages)}**"
    )
    lines.append(
        f"- Rekordy w manifeście: **{len(records)}**"
    )
    lines.append(
        f"- Nieudana strona: "
        f"**{failed_page if failed_page is not None else 'brak'}**"
    )
    lines.append(
        f"- CAPTCHA: "
        f"**{'tak' if any(page['captcha'] for page in pages) else 'nie'}**"
    )
    lines.append("")

    expected = summary.get("total_results")
    complete = (
        failed_page is None
        and expected is not None
        and len(records) == expected
    )

    lines.append(
        f"- Ocena kompletności: "
        f"**{'SUKCES' if complete else 'NIEPEŁNY WYNIK'}**"
    )
    lines.append("")

    lines.append("## Requesty i strony")
    lines.append("")
    lines.append(
        "| Strona | HTTP | curl | Próba | Bajty | "
        "Czas | Strona HTML | Rekordy | CAPTCHA |"
    )
    lines.append(
        "|---:|---:|---:|---:|---:|---:|---|---:|---|"
    )

    for page in pages:
        lines.append(
            f"| {page['requested_page']} "
            f"| {page['http_status']} "
            f"| {page['curl_exit']} "
            f"| {page['attempt']} "
            f"| {page['body_size']} "
            f"| {page['elapsed']:.2f} s "
            f"| {page.get('current_page')}/{page.get('total_pages')} "
            f"| {page['record_count']} "
            f"| {'TAK' if page['captcha'] else 'nie'} |"
        )

    lines.append("")
    lines.append("## Pokrycie pól")
    lines.append("")
    lines.append("| Pole | Uzupełnione | Pokrycie |")
    lines.append("|---|---:|---:|")

    total_records = len(records)

    for field, count in field_counts.items():
        percentage = (
            100.0 * count / total_records
            if total_records
            else 0.0
        )

        lines.append(
            f"| `{field}` | {count}/{total_records} | "
            f"{percentage:.1f}% |"
        )

    lines.append("")
    lines.append("## Próbka rekordów")
    lines.append("")

    sample = sorted(
        ordered_records,
        key=lambda record: (
            record.get("source_page", 0),
            record.get("source_position", 0),
        ),
    )[:10]

    lines.append(
        "| ID | Sygnatura | Data | Rodzaj | Sąd | "
        "Strona | Powiązane |"
    )
    lines.append(
        "|---|---|---|---|---|---:|---:|"
    )

    for record in sample:
        lines.append(
            f"| `{record.get('cbo_id')}` "
            f"| {markdown_escape(record.get('signature'))} "
            f"| {markdown_escape(record.get('judgment_date'))} "
            f"| {markdown_escape(record.get('judgment_type'))} "
            f"| {markdown_escape(record.get('court'))} "
            f"| {record.get('source_page')} "
            f"| {len(record.get('related_cbo_ids') or [])} |"
        )

    lines.append("")
    lines.append("## Pliki")
    lines.append("")
    lines.append(f"- Manifest: `{manifest_path}`")
    lines.append(f"- Checkpoint: `{checkpoint_path}`")
    lines.append(f"- Audyt: `{path}`")
    lines.append("")

    lines.append("## Błędy")
    lines.append("")

    errors = [
        page
        for page in pages
        if page["curl_exit"] != 0 or page["http_status"] != 200
    ]

    if not errors:
        lines.append("- Brak błędów transportowych w zapisanych stronach.")
    else:
        for page in errors:
            lines.append(
                f"- Strona `{page['requested_page']}`: "
                f"curl `{page['curl_exit']}`, "
                f"HTTP `{page['http_status']}`, "
                f"`{markdown_escape(page.get('stderr'))}`"
            )

    path.write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scraper manifestu orzeczeń NSA z CBO."
    )

    parser.add_argument(
        "--from-date",
        default=DEFAULT_FROM_DATE,
    )
    parser.add_argument(
        "--to-date",
        default=DEFAULT_TO_DATE,
    )
    parser.add_argument(
        "--court",
        default=DEFAULT_COURT,
    )
    parser.add_argument(
        "--proxy",
        default=os.environ.get("HTTP_PROXY", DEFAULT_PROXY),
    )
    parser.add_argument(
        "--output-dir",
        default="output",
    )
    parser.add_argument(
        "--delay-min",
        type=float,
        default=10.0,
    )
    parser.add_argument(
        "--delay-max",
        type=float,
        default=15.0,
    )
    parser.add_argument(
        "--reset",
        action="store_true",
    )

    return parser.parse_args()


def validate_date(value: str) -> None:
    try:
        dt.date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(
            f"Nieprawidłowa data ISO: {value}"
        ) from exc


def main() -> int:
    args = parse_arguments()

    validate_date(args.from_date)
    validate_date(args.to_date)

    if args.from_date > args.to_date:
        raise ValueError("--from-date nie może być później niż --to-date")

    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    manifest_path = output_dir / "nsa_manifest.jsonl"
    checkpoint_path = output_dir / "nsa_checkpoint.json"
    audit_path = output_dir / "NSA_AUDIT.md"
    cookie_jar = output_dir / "cookies.txt"

    if args.reset:
        for path in [
            manifest_path,
            checkpoint_path,
            audit_path,
            cookie_jar,
        ]:
            path.unlink(missing_ok=True)

    started_at = now_iso()
    proxy_info = get_proxy_ip(args.proxy)

    print("=" * 100)
    print("NSA / CBO — MANIFEST SCRAPER")
    print("=" * 100)
    print(f"Zakres:              {args.from_date} — {args.to_date}")
    print(f"Sąd:                 {args.court}")
    print(f"Proxy:               {args.proxy}")
    print(f"Publiczne IP proxy:  {proxy_info.get('ip') or 'BRAK'}")
    print(f"Output:               {output_dir}")
    print("-" * 100)

    checkpoint = load_checkpoint(checkpoint_path)
    existing_records = load_manifest(manifest_path)

    same_job = (
        checkpoint.get("from_date") == args.from_date
        and checkpoint.get("to_date") == args.to_date
        and checkpoint.get("court") == args.court
    )

    resumed = bool(same_job and checkpoint.get("last_completed_page"))

    if not same_job:
        checkpoint = {
            "from_date": args.from_date,
            "to_date": args.to_date,
            "court": args.court,
            "last_completed_page": 0,
            "total_pages": None,
            "total_results": None,
            "completed": False,
            "updated_at": now_iso(),
        }

        existing_records = {}
        save_manifest(manifest_path, existing_records)
        save_checkpoint(checkpoint_path, checkpoint)

    cookie_jar.unlink(missing_ok=True)
    cookie_jar.touch()

    print("[1] GET /cbo/query")

    query_result, query_history = request_with_retries(
        url=QUERY_URL,
        cookie_jar=cookie_jar,
        proxy=args.proxy,
    )

    print(
        f"    HTTP={query_result['http_status']} "
        f"curl={query_result['curl_exit']} "
        f"body={len(query_result['body'].encode('utf-8')):,} B "
        f"attempt={query_result['attempt']}"
    )

    if (
        query_result["curl_exit"] != 0
        or query_result["http_status"] != 200
        or len(query_result["body"].encode("utf-8")) < 1000
    ):
        raise RuntimeError(
            "GET /cbo/query nieudany: "
            f"curl={query_result['curl_exit']}, "
            f"HTTP={query_result['http_status']}, "
            f"stderr={query_result['stderr']}"
        )

    delay = random.uniform(args.delay_min, args.delay_max)
    print(f"    przerwa {delay:.1f} s")
    time.sleep(delay)

    print("[2] POST /cbo/search")

    search_result, search_history = request_with_retries(
        url=SEARCH_URL,
        cookie_jar=cookie_jar,
        proxy=args.proxy,
        method="POST",
        payload=make_payload(
            args.from_date,
            args.to_date,
            args.court,
        ),
        referer=QUERY_URL,
    )

    search_summary = parse_summary(search_result["body"])

    print(
        f"    HTTP={search_result['http_status']} "
        f"curl={search_result['curl_exit']} "
        f"wyniki={search_summary.get('total_results')} "
        f"strona={search_summary.get('current_page')}/"
        f"{search_summary.get('total_pages')} "
        f"attempt={search_result['attempt']}"
    )

    if (
        search_result["curl_exit"] != 0
        or search_result["http_status"] != 200
        or search_summary.get("total_pages") is None
    ):
        raise RuntimeError(
            "POST /cbo/search nieudany albo brak paginacji: "
            f"curl={search_result['curl_exit']}, "
            f"HTTP={search_result['http_status']}, "
            f"stderr={search_result['stderr']}"
        )

    total_pages = int(search_summary["total_pages"])
    total_results = int(search_summary["total_results"] or 0)

    checkpoint["total_pages"] = total_pages
    checkpoint["total_results"] = total_results
    checkpoint["updated_at"] = now_iso()
    save_checkpoint(checkpoint_path, checkpoint)

    pages_audit: list[dict[str, Any]] = []
    scraped_at = now_iso()
    failed_page: int | None = None

    page_one_records = parse_page_records(
        search_result["body"],
        page_number=1,
        query_from_date=args.from_date,
        query_to_date=args.to_date,
        scraped_at=scraped_at,
    )

    for record in page_one_records:
        existing_records[record["cbo_id"]] = record

    save_manifest(manifest_path, existing_records)

    checkpoint["last_completed_page"] = max(
        int(checkpoint.get("last_completed_page") or 0),
        1,
    )
    checkpoint["records"] = len(existing_records)
    checkpoint["updated_at"] = now_iso()
    save_checkpoint(checkpoint_path, checkpoint)

    pages_audit.append(
        {
            "requested_page": 1,
            "http_status": search_result["http_status"],
            "curl_exit": search_result["curl_exit"],
            "attempt": search_result["attempt"],
            "body_size": len(
                search_result["body"].encode("utf-8")
            ),
            "elapsed": search_result["elapsed"],
            "current_page": search_summary.get("current_page"),
            "total_pages": search_summary.get("total_pages"),
            "record_count": len(page_one_records),
            "captcha": search_summary.get("captcha", False),
            "stderr": search_result["stderr"],
        }
    )

    start_page = 2

    if resumed:
        start_page = max(
            2,
            int(checkpoint.get("last_completed_page") or 1) + 1,
        )

        print(
            f"Wznawiam od strony {start_page}; "
            f"manifest ma {len(existing_records)} rekordów."
        )

    previous_url = SEARCH_URL

    for page_number in range(2, total_pages + 1):
        if page_number < start_page:
            previous_url = f"{BASE_URL}/cbo/find?p={page_number}"
            continue

        delay = random.uniform(args.delay_min, args.delay_max)
        print(
            f"[page {page_number}/{total_pages}] "
            f"przerwa {delay:.1f} s"
        )
        time.sleep(delay)

        page_url = f"{BASE_URL}/cbo/find?p={page_number}"

        result, history = request_with_retries(
            url=page_url,
            cookie_jar=cookie_jar,
            proxy=args.proxy,
            referer=previous_url,
        )

        summary = parse_summary(result["body"])

        records = parse_page_records(
            result["body"],
            page_number=page_number,
            query_from_date=args.from_date,
            query_to_date=args.to_date,
            scraped_at=scraped_at,
        )

        print(
            f"    HTTP={result['http_status']} "
            f"curl={result['curl_exit']} "
            f"strona={summary.get('current_page')}/"
            f"{summary.get('total_pages')} "
            f"rekordy={len(records)} "
            f"attempt={result['attempt']}"
        )

        page_valid = (
            result["curl_exit"] == 0
            and result["http_status"] == 200
            and summary.get("current_page") == page_number
            and summary.get("total_pages") == total_pages
            and len(records) > 0
            and not summary.get("captcha")
        )

        pages_audit.append(
            {
                "requested_page": page_number,
                "http_status": result["http_status"],
                "curl_exit": result["curl_exit"],
                "attempt": result["attempt"],
                "body_size": len(result["body"].encode("utf-8")),
                "elapsed": result["elapsed"],
                "current_page": summary.get("current_page"),
                "total_pages": summary.get("total_pages"),
                "record_count": len(records),
                "captcha": summary.get("captcha", False),
                "stderr": result["stderr"],
            }
        )

        if not page_valid:
            failed_page = page_number
            checkpoint["failed_page"] = page_number
            checkpoint["updated_at"] = now_iso()
            save_checkpoint(checkpoint_path, checkpoint)
            break

        for record in records:
            existing_records[record["cbo_id"]] = record

        save_manifest(manifest_path, existing_records)

        checkpoint["last_completed_page"] = page_number
        checkpoint["records"] = len(existing_records)
        checkpoint["failed_page"] = None
        checkpoint["updated_at"] = now_iso()
        save_checkpoint(checkpoint_path, checkpoint)

        previous_url = page_url

    complete = (
        failed_page is None
        and len(existing_records) == total_results
        and int(checkpoint.get("last_completed_page") or 0) == total_pages
    )

    checkpoint["completed"] = complete
    checkpoint["records"] = len(existing_records)
    checkpoint["updated_at"] = now_iso()
    save_checkpoint(checkpoint_path, checkpoint)

    finished_at = now_iso()

    write_audit(
        path=audit_path,
        started_at=started_at,
        finished_at=finished_at,
        from_date=args.from_date,
        to_date=args.to_date,
        court=args.court,
        proxy=args.proxy,
        proxy_info=proxy_info,
        summary={
            "total_results": total_results,
            "total_pages": total_pages,
        },
        pages=pages_audit,
        records=existing_records,
        manifest_path=manifest_path,
        checkpoint_path=checkpoint_path,
        failed_page=failed_page,
        resumed=resumed,
    )

    print()
    print("=" * 100)
    print("PODSUMOWANIE")
    print("=" * 100)
    print(f"Wyniki CBO:          {total_results}")
    print(f"Strony CBO:          {total_pages}")
    print(f"Rekordy manifestu:   {len(existing_records)}")
    print(f"Ostatnia strona:     {checkpoint.get('last_completed_page')}")
    print(f"Nieudana strona:     {failed_page}")
    print(f"Kompletność:         {'SUKCES' if complete else 'NIEPEŁNY WYNIK'}")
    print(f"Manifest:            {manifest_path}")
    print(f"Checkpoint:          {checkpoint_path}")
    print(f"Audyt:               {audit_path}")
    print("=" * 100)

    if not complete:
        return 2

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print(
            "\nPrzerwano przez użytkownika.",
            file=sys.stderr,
        )
        raise SystemExit(130)
    except Exception as exc:
        print(
            f"\nBŁĄD: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        raise SystemExit(1)
