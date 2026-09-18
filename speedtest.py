#!/usr/bin/env python3
"""Замер скорости интернета: N последовательных GET-запросов к одному URL.

Считает среднее время запроса, объём скачанных данных и скорость в МБ/с.
Зависимостей нет — только стандартная библиотека Python 3.
"""

import argparse
import http.client
import math
import re
import statistics
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

CHUNK_SIZE = 64 * 1024
# картинка ~9.7 МБ: чтобы скрипт можно было запустить вообще без аргументов
DEFAULT_URL = "https://upload.wikimedia.org/wikipedia/commons/f/ff/Pizigani_1367_Chart_10MB.jpg"
DEFAULT_MAX_BYTES = 512 * 1024 * 1024   # предохранитель от бесконечных потоков
DEFAULT_MAX_SECONDS = 300.0             # предохранитель от очень медленной отдачи

HEADERS = {
    "User-Agent": "speedtest-script/1.1 (+https://github.com/imbim2004/internet-speed-test)",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Accept": "*/*",
    # просим не сжимать: иначе измеряли бы скорость уже сжатого потока
    "Accept-Encoding": "identity",
}

# символы, которыми недружелюбный сервер мог бы управлять терминалом
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")

# незарезервированные символы + уже-процентные последовательности не трогаем
_SAFE_PATH = "/%:@!$&'()*+,;=~"
_SAFE_QUERY = "/?%:@!$&'()*+,;=~"


class RequestFailed(Exception):
    """Один запрос не удался; статистика считается по остальным."""


def sanitize(value, limit=300):
    """Убирает управляющие символы из текста, пришедшего от сервера.

    Сервер управляет текстом статуса и сообщениями об ошибках. Без очистки
    ANSI-escape из ответа попадает в терминал и может переписать вывод —
    например, подделать строку с итоговой скоростью.
    """
    text = _CONTROL_CHARS.sub("?", str(value))
    return text if len(text) <= limit else text[:limit] + "…"


def parse_size(text):
    """'512M' / '1G' / '1048576' -> количество байт."""
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([KMGT]?)B?\s*", str(text), re.IGNORECASE)
    if not match:
        raise argparse.ArgumentTypeError(f"не похоже на размер: {text!r} (примеры: 500M, 2G, 1048576)")
    multiplier = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}
    return int(float(match.group(1)) * multiplier[match.group(2).upper()])


def normalize_url(url):
    """Приводит URL к виду, который примет http.client: IDNA-хост, %-кодирование.

    Без этого кириллический адрес роняет скрипт с UnicodeEncodeError.
    """
    parts = urlsplit(url)
    if parts.scheme.lower() not in ("http", "https"):
        raise ValueError("URL должен начинаться с http:// или https://")

    try:
        hostname, port = parts.hostname, parts.port
    except ValueError as exc:
        raise ValueError(f"некорректный порт в URL: {exc}") from None
    if not hostname:
        raise ValueError("в URL не указан хост")

    try:
        host = hostname.encode("idna").decode("ascii")
    except UnicodeError:
        if not hostname.isascii():
            raise ValueError(f"некорректное доменное имя: {sanitize(hostname, 80)}") from None
        host = hostname  # напр. хост с завершающей точкой — отдаём как есть

    netloc = f"{host}:{port}" if port is not None else host
    if parts.username is not None:
        userinfo = quote(parts.username, safe="")
        if parts.password is not None:
            userinfo += ":" + quote(parts.password, safe="")
        netloc = f"{userinfo}@{netloc}"

    # fragment не отправляется на сервер — отбрасываем
    return urlunsplit((
        parts.scheme.lower(),
        netloc,
        quote(parts.path, safe=_SAFE_PATH),
        quote(parts.query, safe=_SAFE_QUERY),
        "",
    ))


def display_url(url):
    """URL для печати: без пароля и без управляющих символов."""
    parts = urlsplit(url)
    if parts.password:
        netloc = parts.netloc.replace(f":{parts.password}@", ":***@", 1)
        url = urlunsplit(parts._replace(netloc=netloc))
    return sanitize(url, limit=500)


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Не выпускает редиректы за пределы http/https.

    Стандартный обработчик разрешает ещё и ftp://, то есть проверка схемы
    при разборе аргументов обходится одним 302-ответом.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlsplit(newurl).scheme.lower() not in ("http", "https"):
            raise urllib.error.HTTPError(
                newurl, code,
                f"редирект на недопустимую схему: {sanitize(newurl, 120)}",
                headers, fp,
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def add_cache_buster(url, attempt):
    """Добавляет уникальный параметр в query, чтобы не получить ответ из кэша."""
    parts = urlsplit(url)
    buster = urlencode({"_cb": f"{int(time.time() * 1000)}-{attempt}"})
    query = f"{parts.query}&{buster}" if parts.query else buster
    return urlunsplit(parts._replace(query=query))


def download_once(opener, url, timeout, max_bytes, max_seconds,
                  no_cache_buster=False, attempt=0):
    """Скачивает URL целиком. Возвращает (секунды, байты, http_статус).

    Любая неудача поднимается как RequestFailed с уже очищенным текстом.
    """
    target = url if no_cache_buster else add_cache_buster(url, attempt)
    request = urllib.request.Request(target, headers=HEADERS)

    downloaded = 0
    response = None
    started = time.perf_counter()
    try:
        response = opener.open(request, timeout=timeout)
        status = response.status
        declared = response.getheader("Content-Length")
        # read1() отдаёт то, что уже пришло, а не ждёт набора полного куска:
        # иначе на медленной отдаче проверки лимитов ниже не выполняются,
        # пока сервер не дошлёт весь CHUNK_SIZE.
        while True:
            chunk = response.read1(CHUNK_SIZE)
            if not chunk:
                break
            downloaded += len(chunk)
            if max_bytes and downloaded > max_bytes:
                raise RequestFailed(
                    f"ответ больше лимита --max-bytes ({human_bytes(max_bytes)}); "
                    "похоже, по адресу бесконечный поток, а не файл"
                )
            if max_seconds and time.perf_counter() - started > max_seconds:
                raise RequestFailed(
                    f"запрос идёт дольше лимита --max-seconds ({max_seconds:g} с), "
                    f"скачано {human_bytes(downloaded)}"
                )

        elapsed = time.perf_counter() - started

        # Content-Length с чтением по кускам не проверяется само: оборванный
        # ответ иначе молча зачёлся бы как быстрый успешный запрос.
        if declared is not None:
            try:
                expected = int(declared)
            except ValueError:
                expected = None
            if expected is not None and downloaded != expected:
                raise RequestFailed(
                    f"ответ оборван: получено {downloaded} Б из заявленных {expected} Б"
                )

        return elapsed, downloaded, status

    except urllib.error.HTTPError as exc:
        exc.close()  # иначе тело ответа остаётся открытым до сборки мусора
        raise RequestFailed(f"HTTP {exc.code}: {sanitize(exc.reason)}") from None
    except urllib.error.URLError as exc:
        raise RequestFailed(sanitize(exc.reason)) from None
    except (OSError, http.client.HTTPException, ValueError) as exc:
        raise RequestFailed(f"{type(exc).__name__}: {sanitize(exc)}") from None
    finally:
        if response is not None:
            response.close()


def human_bytes(num):
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if abs(num) < 1024 or unit == "ГБ":
            return f"{int(num)} {unit}" if unit == "Б" else f"{num:.2f} {unit}"
        num /= 1024


def build_parser():
    parser = argparse.ArgumentParser(
        description="Замер скорости скачивания: N последовательных запросов к URL.",
    )
    parser.add_argument(
        "url", nargs="?", default=DEFAULT_URL,
        help="адрес, куда стучаться (например, тяжёлая картинка); "
             "если не указан, берётся картинка ~9.7 МБ с Wikimedia",
    )
    parser.add_argument(
        "-n", "--requests", type=int, default=10,
        help="количество последовательных запросов (по умолчанию 10)",
    )
    parser.add_argument(
        "-t", "--timeout", type=float, default=60.0,
        help="таймаут одной операции с сокетом, секунды (по умолчанию 60)",
    )
    parser.add_argument(
        "--max-seconds", type=float, default=DEFAULT_MAX_SECONDS,
        help="предел длительности одного запроса целиком, секунды "
             f"(по умолчанию {DEFAULT_MAX_SECONDS:g}; 0 — без предела)",
    )
    parser.add_argument(
        "--max-bytes", type=parse_size, default=DEFAULT_MAX_BYTES,
        help="предел объёма одного ответа, напр. 500M или 2G "
             f"(по умолчанию {DEFAULT_MAX_BYTES // 1024 // 1024}M; 0 — без предела)",
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
    return parser


def main(argv=None):
    # stdout при перенаправлении в файл буферизуется блоками, а stderr — нет,
    # из-за чего строки об ошибках уезжали вперёд успешных. Метода может не быть,
    # если stdout подменён не на текстовый поток.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)

    parser = build_parser()
    args = parser.parse_args(argv)

    if args.requests < 1:
        parser.error("количество запросов должно быть >= 1")
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("таймаут должен быть положительным числом")
    if not math.isfinite(args.max_seconds) or args.max_seconds < 0:
        parser.error("--max-seconds должен быть >= 0")
    if args.max_bytes < 0:
        parser.error("--max-bytes должен быть >= 0")
    try:
        url = normalize_url(args.url)
    except ValueError as exc:
        parser.error(str(exc))

    opener = urllib.request.build_opener(SafeRedirectHandler)
    limits = dict(timeout=args.timeout, max_bytes=args.max_bytes, max_seconds=args.max_seconds,
                  no_cache_buster=args.no_cache_buster)

    print(f"URL:      {display_url(url)}"
          + ("   (адрес по умолчанию)" if args.url == DEFAULT_URL else ""))
    print(f"Запросов: {args.requests}\n")

    if args.warmup:
        try:
            download_once(opener, url, attempt=-1, **limits)
            print("Прогревочный запрос выполнен (в статистику не входит)\n")
        except RequestFailed as exc:
            print(f"Прогревочный запрос не удался: {exc}\n", file=sys.stderr)

    times, sizes, failures, interrupted = [], [], 0, False

    try:
        for i in range(1, args.requests + 1):
            try:
                elapsed, size, status = download_once(opener, url, attempt=i, **limits)
            except RequestFailed as exc:
                failures += 1
                sys.stdout.flush()
                print(f"[{i:2}/{args.requests}] ОШИБКА: {exc}", file=sys.stderr)
                sys.stderr.flush()
                continue

            times.append(elapsed)
            sizes.append(size)
            if not args.quiet:
                speed = size / elapsed / 1024 / 1024 if elapsed > 0 else float("inf")
                print(
                    f"[{i:2}/{args.requests}] HTTP {status}  "
                    f"{elapsed:7.3f} с  {human_bytes(size):>10}  {speed:7.2f} МБ/с"
                )
    except KeyboardInterrupt:
        interrupted = True
        sys.stdout.flush()
        print("\nПрервано — считаю по уже выполненным запросам.", file=sys.stderr)
        sys.stderr.flush()

    if not times:
        print("\nНи один запрос не удался — считать нечего.", file=sys.stderr)
        return 130 if interrupted else 1

    total_time = sum(times)
    total_bytes = sum(sizes)
    avg_time = total_time / len(times)
    avg_bytes = total_bytes / len(sizes)

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
    if total_time > 0:
        print(f"СКОРОСТЬ:              {total_bytes / total_time / 1024 / 1024:.2f} МБ/с  "
              f"({total_bytes * 8 / total_time / 1_000_000:.2f} Мбит/с)")
    else:
        print("СКОРОСТЬ:              —  (время замера слишком мало)")
    print("=" * 52)

    if len(set(sizes)) > 1:
        print("\nВнимание: размеры ответов различаются — по этому адресу отдаётся "
              "не один и тот же файл, средняя скорость условна.", file=sys.stderr)
    if avg_bytes < 1024 * 1024:
        print("\nВнимание: файл меньше 1 МБ — результат определяется в основном задержкой "
              "соединения, а не шириной канала. Возьмите файл потяжелее.", file=sys.stderr)
    if interrupted:
        return 130
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nПрервано пользователем.", file=sys.stderr)
        sys.exit(130)
