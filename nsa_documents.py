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

BASE_URL = "https://orzeczenia.nsa.gov.pl"
DEFAULT_PROXY = "http://127.0.0.1:8080"

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
    value = re.sub(
        r"</(?:p|div|tr|td|li|table|section|h\d|dd|dt)>",
        "\n",
        value,
        flags=re.I,
    )
    value = re.sub(r"<[^>]+>", " ", value)
    value = html.unescape(value)
    value = value.replace("\xa0", " ")
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\s*\n\s*", "\n", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def normalize_spaces(value: str | None) -> str | None:
    if value is None:
        return None

    value = re.sub(r"\s+", " ", value).strip()
    return value or None


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def request(
    *,
    url: str,
    proxy: str,
    timeout: int = 180,
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="nsa-doc-") as temp_dir:
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
            "--dump-header",
            str(headers_path),
            "--output",
            str(body_path),
            "--write-out",
            (
                "%{http_code}|%{remote_ip}|%{size_download}|"
                "%{time_starttransfer}|%{time_total}|%{url_effective}"
            ),
            url,
        ]

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
        metrics = process.stdout.decode("utf-8", errors="replace").strip()
        parts = metrics.split("|", 5)

        def part(index: int, default: str = "") -> str:
            return parts[index] if len(parts) > index else default

        try:
            http_status = int(part(0, "0"))
        except ValueError:
            http_status = 0

        body = (
            body_path.read_text(encoding="utf-8", errors="replace")
            if body_path.exists()
            else ""
        )

        headers = (
            headers_path.read_text(encoding="utf-8", errors="replace")
            if headers_path.exists()
            else ""
        )

        stderr = (
            stderr_path.read_text(encoding="utf-8", errors="replace").strip()
            if stderr_path.exists()
            else ""
        )

        return {
            "url": url,
            "curl_exit": process.returncode,
            "http_status": http_status,
            "remote_ip": part(1),
            "size_download": len(body.encode("utf-8")),
            "time_starttransfer": part(3),
            "time_total": part(4),
            "effective_url": part(5),
            "elapsed": elapsed,
            "body": body,
            "headers": headers,
            "stderr": stderr,
            "body_sha256": sha256_text(body),
        }


def request_with_retries(
    *,
    url: str,
    proxy: str,
    attempts: int = 3,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    history: list[dict[str, Any]] = []
    retry_delays = [30, 60, 120]

    for attempt in range(1, attempts + 1):
        result = request(url=url, proxy=proxy)
        result["attempt"] = attempt
        history.append(result)

        success = (
            result["curl_exit"] == 0
            and result["http_status"] == 200
            and result["size_download"] >= 1000
        )

        if success:
            return result, history

        if attempt < attempts:
            delay = retry_delays[min(attempt - 1, len(retry_delays) - 1)]
            print(
                f"      nieudana próba {attempt}: "
                f"curl={result['curl_exit']} "
                f"HTTP={result['http_status']} "
                f"body={result['size_download']} B; "
                f"retry za {delay} s"
            )
            time.sleep(delay)

    return history[-1], history


def extract_heading(source: str) -> str | None:
    patterns = [
        r"<h1\b[^>]*>(.*?)</h1>",
        r"<h2\b[^>]*>(.*?)</h2>",
        r"<title\b[^>]*>(.*?)</title>",
    ]

    for pattern in patterns:
        match = re.search(pattern, source, flags=re.I | re.S)
        if match:
            value = clean_text(match.group(1))
            if value:
                return value

    return None


def extract_section_by_heading(
    source: str,
    heading_names: list[str],
    next_heading_names: list[str],
) -> str | None:
    heading_pattern = "|".join(re.escape(name) for name in heading_names)
    next_pattern = "|".join(re.escape(name) for name in next_heading_names)

    patterns = [
        rf"""
        <(?:h[1-6]|dt|th|div|span|strong)\b[^>]*>
        \s*(?:{heading_pattern})\s*
        </(?:h[1-6]|dt|th|div|span|strong)>
        (?P<body>.*?)
        (?=
            <(?:h[1-6]|dt|th|div|span|strong)\b[^>]*>
            \s*(?:{next_pattern})\s*
            </(?:h[1-6]|dt|th|div|span|strong)>
            |\Z
        )
        """,
        rf"""
        (?:^|>)
        \s*(?:{heading_pattern})\s*
        <
        (?P<body>.*?)
        (?=
            (?:^|>)
            \s*(?:{next_pattern})\s*
            <
            |\Z
        )
        """,
    ]

    for pattern in patterns:
        match = re.search(
            pattern,
            source,
            flags=re.I | re.S | re.X,
        )

        if match:
            value = clean_text(match.group("body"))
            if value:
                return value

    return None


def extract_labeled_value(
    visible: str,
    label: str,
    following_labels: list[str],
) -> str | None:
    next_labels = "|".join(re.escape(item) for item in following_labels)

    pattern = (
        rf"(?:^|\n)\s*{re.escape(label)}\s*\n"
        rf"(?P<value>.*?)"
        rf"(?=\n(?:{next_labels})\s*\n|\Z)"
    )

    match = re.search(
        pattern,
        visible,
        flags=re.I | re.S,
    )

    if not match:
        return None

    return normalize_spaces(match.group("value"))


def parse_document(
    source: str,
    *,
    cbo_id: str,
    source_manifest_record: dict[str, Any],
) -> dict[str, Any]:
    visible = clean_text(source)

    labels = [
        "Data orzeczenia",
        "Data wpływu",
        "Sąd",
        "Sędziowie",
        "Symbol z opisem",
        "Hasła tematyczne",
        "Sygn. powiązane",
        "Skarżony organ",
        "Treść wyniku",
        "Powołane przepisy",
        "Sentencja",
        "Uzasadnienie",
    ]

    def field(label: str) -> str | None:
        index = labels.index(label)
        following = labels[index + 1 :]
        return extract_labeled_value(
            visible,
            label,
            following,
        )

    heading = extract_heading(source)

    judgment_date_raw = field("Data orzeczenia")
    judgment_date_match = (
        re.search(r"\d{4}-\d{2}-\d{2}", judgment_date_raw or "")
    )

    final_status = None
    if judgment_date_raw:
        status_match = re.search(
            r"orzeczenie\s+(prawomocne|nieprawomocne)",
            judgment_date_raw,
            flags=re.I,
        )
        if status_match:
            final_status = status_match.group(0)

    reasoning = field("Uzasadnienie")
    sentencing = field("Sentencja")

    has_reasoning = bool(
        reasoning
        and len(reasoning) >= 100
        and not re.fullmatch(
            r"(brak|nie opublikowano|uzasadnienie niedostępne)",
            reasoning,
            flags=re.I,
        )
    )

    has_sentencing = bool(sentencing and len(sentencing) >= 50)

    related_signatures = []

    related_raw = field("Sygn. powiązane")
    if related_raw:
        related_signatures = re.findall(
            r"\b(?:I|II|III|IV|V|VI|VII|VIII|IX|X)\s+"
            r"(?:FSK|OSK|GSK|OZ|SA/[A-Za-z]+|SAB/[A-Za-z]+|SO/[A-Za-z]+)"
            r"\s+\d+/\d+\b",
            related_raw,
            flags=re.I,
        )

    return {
        "cbo_id": cbo_id,
        "url": f"{BASE_URL}/doc/{cbo_id}",
        "heading": heading,
        "signature": source_manifest_record.get("signature"),
        "judgment_date": (
            judgment_date_match.group(0)
            if judgment_date_match
            else source_manifest_record.get("judgment_date")
        ),
        "final_status": final_status,
        "filing_date": field("Data wpływu"),
        "court": field("Sąd") or source_manifest_record.get("court"),
        "judges": field("Sędziowie"),
        "symbol_description": field("Symbol z opisem"),
        "keywords": field("Hasła tematyczne"),
        "related_cases_text": related_raw,
        "related_signatures": related_signatures,
        "challenged_authority": field("Skarżony organ"),
        "result_text": field("Treść wyniku"),
        "provisions": field("Powołane przepisy"),
        "sentencing": sentencing,
        "reasoning": reasoning,
        "has_sentencing": has_sentencing,
        "has_reasoning": has_reasoning,
        "source_manifest": {
            "signature": source_manifest_record.get("signature"),
            "judgment_date": source_manifest_record.get("judgment_date"),
            "judgment_type": source_manifest_record.get("judgment_type"),
            "court": source_manifest_record.get("court"),
            "source_page": source_manifest_record.get("source_page"),
        },
        "raw_visible_text": visible,
        "downloaded_at": now_iso(),
    }


def load_manifest(path: Path, limit: int) -> list[dict[str, Any]]:
    records = []

    for line in path.read_text(
        encoding="utf-8",
        errors="replace",
    ).splitlines():
        line = line.strip()

        if not line:
            continue

        record = json.loads(line)

        if record.get("cbo_id"):
            records.append(record)

        if len(records) >= limit:
            break

    return records


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(
                json.dumps(
                    record,
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
            )


def get_proxy_ip(proxy: str) -> str:
    process = subprocess.run(
        [
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
        ],
        capture_output=True,
        timeout=70,
        check=False,
    )

    if process.returncode != 0:
        return ""

    return process.stdout.decode(
        "utf-8",
        errors="replace",
    ).strip()


def markdown_escape(value: Any) -> str:
    if value is None:
        return "—"

    return str(value).replace("|", "\\|").replace("\n", " ")


def write_audit(
    *,
    path: Path,
    manifest_path: Path,
    output_path: Path,
    proxy: str,
    proxy_ip: str,
    request_rows: list[dict[str, Any]],
    records: list[dict[str, Any]],
    failed_ids: list[str],
) -> None:
    fields = [
        "judgment_date",
        "final_status",
        "filing_date",
        "court",
        "judges",
        "symbol_description",
        "keywords",
        "related_cases_text",
        "challenged_authority",
        "result_text",
        "provisions",
        "sentencing",
        "reasoning",
    ]

    lines = [
        "# NSA / CBO — audyt pobierania dokumentów",
        "",
        f"- Manifest wejściowy: `{manifest_path}`",
        f"- Proxy: `{proxy}`",
        f"- Publiczne IP proxy: `{proxy_ip or 'brak'}`",
        f"- Dokumenty zaplanowane: **{len(request_rows)}**",
        f"- Dokumenty sparsowane: **{len(records)}**",
        f"- Nieudane dokumenty: **{len(failed_ids)}**",
        "",
        "## Requesty",
        "",
        "| Lp. | CBO ID | HTTP | curl | Próba | Bajty | Czas |",
        "|---:|---|---:|---:|---:|---:|---:|",
    ]

    for index, row in enumerate(request_rows, start=1):
        lines.append(
            f"| {index} "
            f"| `{row['cbo_id']}` "
            f"| {row['http_status']} "
            f"| {row['curl_exit']} "
            f"| {row['attempt']} "
            f"| {row['body_size']} "
            f"| {row['elapsed']:.2f} s |"
        )

    lines.extend(
        [
            "",
            "## Pokrycie pól",
            "",
            "| Pole | Uzupełnione | Pokrycie |",
            "|---|---:|---:|",
        ]
    )

    total = len(records)

    for field in fields:
        count = sum(
            1
            for record in records
            if record.get(field)
        )
        coverage = 100 * count / total if total else 0

        lines.append(
            f"| `{field}` | {count}/{total} | {coverage:.1f}% |"
        )

    with_sentencing = sum(
        1 for record in records if record.get("has_sentencing")
    )
    with_reasoning = sum(
        1 for record in records if record.get("has_reasoning")
    )

    lines.extend(
        [
            "",
            "## Dostępność treści",
            "",
            f"- Z sentencją: **{with_sentencing}/{total}**",
            f"- Z uzasadnieniem: **{with_reasoning}/{total}**",
            f"- Tylko sentencja, bez uzasadnienia: "
            f"**{sum(1 for r in records if r.get('has_sentencing') and not r.get('has_reasoning'))}/{total}**",
            "",
            "## Próbka",
            "",
            "| CBO ID | Sygnatura | Sentencja | Uzasadnienie | Organ | Symbol |",
            "|---|---|---|---|---|---|",
        ]
    )

    for record in records[:10]:
        lines.append(
            f"| `{record.get('cbo_id')}` "
            f"| {markdown_escape(record.get('signature'))} "
            f"| {'tak' if record.get('has_sentencing') else 'nie'} "
            f"| {'tak' if record.get('has_reasoning') else 'nie'} "
            f"| {markdown_escape(record.get('challenged_authority'))} "
            f"| {markdown_escape(record.get('symbol_description'))} |"
        )

    lines.extend(
        [
            "",
            "## Nieudane ID",
            "",
        ]
    )

    if failed_ids:
        for cbo_id in failed_ids:
            lines.append(f"- `{cbo_id}`")
    else:
        lines.append("- Brak.")

    lines.extend(
        [
            "",
            "## Pliki",
            "",
            f"- Dokumenty: `{output_path}`",
            f"- Audyt: `{path}`",
        ]
    )

    path.write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--manifest",
        required=True,
    )
    parser.add_argument(
        "--output-dir",
        default="documents-output",
    )
    parser.add_argument(
        "--proxy",
        default=os.environ.get("HTTP_PROXY", DEFAULT_PROXY),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=10,
    )
    parser.add_argument(
        "--delay-min",
        type=float,
        default=10,
    )
    parser.add_argument(
        "--delay-max",
        type=float,
        default=15,
    )

    return parser.parse_args()


def main() -> int:
    args = parse_args()

    manifest_path = Path(args.manifest).resolve()
    output_dir = Path(args.output_dir).resolve()
    raw_dir = output_dir / "raw"

    output_dir.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)

    output_path = output_dir / "nsa_documents.jsonl"
    audit_path = output_dir / "NSA_DOCUMENTS_AUDIT.md"

    manifest_records = load_manifest(
        manifest_path,
        args.limit,
    )

    proxy_ip = get_proxy_ip(args.proxy)

    print("=" * 100)
    print("NSA / CBO — DOCUMENT DOWNLOADER POC")
    print("=" * 100)
    print(f"Manifest:            {manifest_path}")
    print(f"Limit:               {args.limit}")
    print(f"Proxy:               {args.proxy}")
    print(f"Publiczne IP proxy:  {proxy_ip or 'BRAK'}")
    print("-" * 100)

    documents: list[dict[str, Any]] = []
    request_rows: list[dict[str, Any]] = []
    failed_ids: list[str] = []

    for index, manifest_record in enumerate(
        manifest_records,
        start=1,
    ):
        cbo_id = manifest_record["cbo_id"]
        url = f"{BASE_URL}/doc/{cbo_id}"

        if index > 1:
            delay = random.uniform(
                args.delay_min,
                args.delay_max,
            )
            print(f"[{index}/{len(manifest_records)}] przerwa {delay:.1f} s")
            time.sleep(delay)

        print(f"[{index}/{len(manifest_records)}] GET {url}")

        result, history = request_with_retries(
            url=url,
            proxy=args.proxy,
        )

        raw_path = raw_dir / f"{cbo_id}.html"
        raw_path.write_text(
            result["body"],
            encoding="utf-8",
        )

        request_rows.append(
            {
                "cbo_id": cbo_id,
                "http_status": result["http_status"],
                "curl_exit": result["curl_exit"],
                "attempt": result["attempt"],
                "body_size": result["size_download"],
                "elapsed": result["elapsed"],
                "stderr": result["stderr"],
            }
        )

        success = (
            result["curl_exit"] == 0
            and result["http_status"] == 200
            and result["size_download"] >= 1000
        )

        print(
            f"      HTTP={result['http_status']} "
            f"curl={result['curl_exit']} "
            f"body={result['size_download']:,} B "
            f"attempt={result['attempt']}"
        )

        if not success:
            failed_ids.append(cbo_id)
            continue

        document = parse_document(
            result["body"],
            cbo_id=cbo_id,
            source_manifest_record=manifest_record,
        )

        documents.append(document)

        write_jsonl(
            output_path,
            documents,
        )

    write_jsonl(
        output_path,
        documents,
    )

    write_audit(
        path=audit_path,
        manifest_path=manifest_path,
        output_path=output_path,
        proxy=args.proxy,
        proxy_ip=proxy_ip,
        request_rows=request_rows,
        records=documents,
        failed_ids=failed_ids,
    )

    print()
    print("=" * 100)
    print("PODSUMOWANIE")
    print("=" * 100)
    print(f"Zaplanowane:         {len(manifest_records)}")
    print(f"Pobrane:             {len(documents)}")
    print(f"Nieudane:            {len(failed_ids)}")
    print(
        f"Z sentencją:         "
        f"{sum(1 for d in documents if d.get('has_sentencing'))}"
    )
    print(
        f"Z uzasadnieniem:     "
        f"{sum(1 for d in documents if d.get('has_reasoning'))}"
    )
    print(f"Dokumenty:           {output_path}")
    print(f"Audyt:               {audit_path}")
    print("=" * 100)

    return 0 if not failed_ids else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        print(
            f"BŁĄD: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        raise SystemExit(1)
