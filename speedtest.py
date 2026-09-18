#!/usr/bin/env python3
"""Замер скорости интернета: N последовательных GET-запросов к одному URL.

Считает среднее время запроса, объём скачанных данных и скорость в МБ/с.
Зависимостей нет — только стандартная библиотека Python 3.
"""

import argparse
import statistics
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlencode, urlparse, urlunparse

CHUNK_SIZE = 64 * 1024
USER_AGENT = "speedtest-script/1.0 (+https://github.com/)"


def add_cache_buster(url, attempt):
    """Добавляет уникальный параметр в query, чтобы не получить ответ из кэша."""
    parts = urlparse(url)
    buster = urlencode({"_cb": f"{int(time.time() * 1000)}-{attempt}"})
    query = f"{parts.query}&{buster}" if parts.query else buster
    return urlunparse(parts._replace(query=query))


def download_once(url, timeout, no_cache_buster=False, attempt=0):
    """Скачивает URL целиком. Возвращает (секунды, байты, http_статус)."""
    target = url if no_cache_buster else add_cache_buster(url, attempt)
    request = urllib.request.Request(
        target,
        headers={
            "User-Agent": USER_AGENT,
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Accept": "*/*",
        },
    )

    downloaded = 0
    started = time.perf_counter()
    with urllib.request.urlopen(request, timeout=timeout) as response:
        status = response.status
        while True:
            chunk = response.read(CHUNK_SIZE)
            if not chunk:
                break
            downloaded += len(chunk)
    elapsed = time.perf_counter() - started
    return elapsed, downloaded, status


def human_bytes(num):
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if abs(num) < 1024 or unit == "ГБ":
            return f"{num:.2f} {unit}" if unit != "Б" else f"{int(num)} {unit}"
        num /= 1024
    return f"{num:.2f} ГБ"


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Замер скорости скачивания: N последовательных запросов к URL.",
    )
    parser.add_argument("url", help="адрес, куда стучаться (например, тяжёлая картинка)")
    parser.add_argument(
        "-n", "--requests", type=int, default=10,
        help="количество последовательных запросов (по умолчанию 10)",
    )
    parser.add_argument(
        "-t", "--timeout", type=float, default=60.0,
        help="таймаут одного запроса в секундах (по умолчанию 60)",
    )
    parser.add_argument(
        "--warmup", action="store_true",
        help="сделать один прогревочный запрос, не учитываемый в статистике",
    )
    parser.add_argument(
        "--no-cache-buster", action="store_true",
        help="не добавлять уникальный query-параметр (ответ может прийти из кэша)",
    )
    parser.add_argument("-q", "--quiet", action="store_true", help="не печатать каждый запрос")
    args = parser.parse_args(argv)

    if args.requests < 1:
        parser.error("количество запросов должно быть >= 1")
    if not urlparse(args.url).scheme in ("http", "https"):
        parser.error("URL должен начинаться с http:// или https://")

    print(f"URL:      {args.url}")
    print(f"Запросов: {args.requests}\n")

    if args.warmup:
        try:
            download_once(args.url, args.timeout, args.no_cache_buster, attempt=-1)
            print("Прогревочный запрос выполнен (в статистику не входит)\n")
        except (urllib.error.URLError, OSError) as exc:
            print(f"Прогревочный запрос не удался: {exc}\n", file=sys.stderr)

    times = []
    sizes = []
    failures = 0

    for i in range(1, args.requests + 1):
        try:
            elapsed, size, status = download_once(
                args.url, args.timeout, args.no_cache_buster, attempt=i
            )
        except (urllib.error.URLError, OSError) as exc:
            failures += 1
            print(f"[{i:2}/{args.requests}] ОШИБКА: {exc}", file=sys.stderr)
            continue

        times.append(elapsed)
        sizes.append(size)
        if not args.quiet:
            speed = size / elapsed / 1024 / 1024 if elapsed > 0 else 0.0
            print(
                f"[{i:2}/{args.requests}] HTTP {status}  "
                f"{elapsed:7.3f} с  {human_bytes(size):>10}  {speed:7.2f} МБ/с"
            )

    if not times:
        print("\nНи один запрос не удался — считать нечего.", file=sys.stderr)
        return 1

    total_time = sum(times)
    total_bytes = sum(sizes)
    avg_time = total_time / len(times)
    avg_bytes = total_bytes / len(sizes)
    speed_mb = total_bytes / total_time / 1024 / 1024   # МБ/с (мегабайты)
    speed_mbit = total_bytes * 8 / total_time / 1_000_000  # Мбит/с

    print("\n" + "=" * 52)
    print(f"Успешных запросов:     {len(times)} из {args.requests}"
          + (f" (ошибок: {failures})" if failures else ""))
    print(f"Среднее время запроса: {avg_time:.3f} с")
    if len(times) > 1:
        print(f"  мин / макс:          {min(times):.3f} с / {max(times):.3f} с")
        print(f"  медиана:             {statistics.median(times):.3f} с")
    print(f"Средний объём ответа:  {human_bytes(avg_bytes)}")
    print(f"Всего скачано:         {human_bytes(total_bytes)} ({total_bytes} байт)")
    print(f"Общее время:           {total_time:.3f} с")
    print("-" * 52)
    print(f"СКОРОСТЬ:              {speed_mb:.2f} МБ/с  ({speed_mbit:.2f} Мбит/с)")
    print("=" * 52)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nПрервано пользователем.", file=sys.stderr)
        sys.exit(130)
