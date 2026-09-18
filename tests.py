#!/usr/bin/env python3
"""Самопроверка speedtest.py — офлайн, без зависимостей и без интернета.

Поднимает локальный сервер, который намеренно отдаёт некорректные ответы,
и проверяет, что скрипт их распознаёт, а не завышает скорость и не падает.

    python3 tests.py
"""

import socket
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path

import speedtest

SCRIPT = str(Path(__file__).with_name("speedtest.py"))


def _respond(conn, path):
    """Отдаёт намеренно проблемный ответ в зависимости от пути."""
    if path == "/ok":
        body = b"A" * 200_000
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(body) + body)
    elif path == "/empty":
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
    elif path == "/truncated":
        # обещает 1000 байт, отдаёт 10 и закрывает соединение
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 1000\r\n\r\n" + b"x" * 10)
    elif path == "/ansi":
        # ANSI-escape в тексте статуса — попытка управлять терминалом клиента
        conn.sendall(b"HTTP/1.1 404 \x1b[31mPWNED\x1b[2K\r\nContent-Length: 0\r\n\r\n")
    elif path == "/infinite":
        conn.sendall(b"HTTP/1.1 200 OK\r\n\r\n")
        while True:
            conn.sendall(b"y" * 65536)
    elif path == "/drip":
        # заголовки сразу, тело — по байту, каждая порция укладывается в таймаут
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n")
        for _ in range(100):
            time.sleep(0.5)
            conn.sendall(b"z")
    elif path == "/redirect-ftp":
        conn.sendall(b"HTTP/1.1 302 Found\r\nLocation: ftp://ftp.example.com/x\r\n"
                     b"Content-Length: 0\r\n\r\n")
    else:
        conn.sendall(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")


class BrokenServer:
    """Однопоточный на соединение сервер с патологическими ответами."""

    def __init__(self):
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(32)
        self.port = self._sock.getsockname()[1]
        self._stop = False
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while not self._stop:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        try:
            conn.settimeout(10)
            request = conn.recv(65536).decode("latin-1", "replace")
            path = request.split(" ")[1].split("?")[0] if " " in request else "/"
            _respond(conn, path)
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def close(self):
        self._stop = True
        self._sock.close()


class SpeedtestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = BrokenServer()

    @classmethod
    def tearDownClass(cls):
        cls.server.close()

    def run_script(self, *args, expect_timeout=20):
        started = time.monotonic()
        proc = subprocess.run(
            [sys.executable, SCRIPT, *args],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, timeout=expect_timeout,
        )
        return proc.returncode, proc.stdout, time.monotonic() - started

    # --- корректный замер ------------------------------------------------

    def test_normal_download_is_measured(self):
        code, out, _ = self.run_script(self.server.url("/ok"), "-n", "3", "-t", "5")
        self.assertEqual(code, 0, out)
        self.assertIn("Успешных запросов:     3 из 3", out)
        self.assertIn("600000 байт", out)  # 3 × 200 000, объём сходится

    def test_empty_response_does_not_crash(self):
        code, out, _ = self.run_script(self.server.url("/empty"), "-n", "2", "-t", "5")
        self.assertEqual(code, 0, out)
        self.assertIn("0 байт", out)

    # --- битые ответы ----------------------------------------------------

    def test_truncated_response_is_an_error_not_a_fast_success(self):
        """Оборванный ответ не должен зачесться как быстрый успешный запрос."""
        code, out, _ = self.run_script(self.server.url("/truncated"), "-n", "2", "-t", "5")
        self.assertEqual(code, 1, out)
        self.assertIn("ответ оборван", out)
        self.assertNotIn("СКОРОСТЬ", out)

    def test_all_requests_failing_exits_nonzero(self):
        code, out, _ = self.run_script(self.server.url("/nope"), "-n", "2", "-t", "5")
        self.assertEqual(code, 1, out)
        self.assertIn("HTTP 404", out)

    # --- предохранители от зависания -------------------------------------

    def test_endless_stream_is_capped(self):
        code, out, seconds = self.run_script(
            self.server.url("/infinite"), "-n", "1", "-t", "5", "--max-bytes", "5M")
        self.assertEqual(code, 1, out)
        self.assertIn("--max-bytes", out)
        self.assertLess(seconds, 15, "бесконечный поток должен обрываться, а не висеть")

    def test_slow_trickle_is_capped(self):
        code, out, seconds = self.run_script(
            self.server.url("/drip"), "-n", "1", "-t", "10", "--max-seconds", "2")
        self.assertEqual(code, 1, out)
        self.assertIn("--max-seconds", out)
        self.assertLess(seconds, 10, "лимит должен срабатывать во время медленной отдачи")

    # --- безопасность ----------------------------------------------------

    def test_server_cannot_inject_escape_sequences(self):
        """Текст статуса задаёт сервер; ANSI-escape не должен дойти до терминала."""
        _, out, _ = self.run_script(self.server.url("/ansi"), "-n", "1", "-t", "5")
        self.assertNotIn("\x1b", out)
        self.assertIn("HTTP 404", out)

    def test_redirect_cannot_leave_http(self):
        """302 на ftp:// не должен обходить проверку схемы."""
        code, out, _ = self.run_script(self.server.url("/redirect-ftp"), "-n", "1", "-t", "5")
        self.assertEqual(code, 1, out)
        self.assertIn("недопустимую схему", out)

    def test_password_is_masked_in_output(self):
        url = speedtest.normalize_url("https://user:s3cret@example.com/file.bin")
        self.assertNotIn("s3cret", speedtest.display_url(url))

    # --- разбор аргументов ------------------------------------------------

    def test_invalid_arguments_are_rejected_cleanly(self):
        ok = self.server.url("/ok")
        for args, expected in [
            ([ok, "-t", "-5"], "таймаут"),
            ([ok, "-t", "0"], "таймаут"),
            ([ok, "-t", "nan"], "таймаут"),
            ([ok, "-n", "0"], "количество запросов"),
            ([ok, "--max-bytes", "abc"], "размер"),
            (["file:///etc/passwd"], "http://"),
            (["http://"], "хост"),
            (["ftp://example.com/x"], "http://"),
            (["example.com"], "http://"),
        ]:
            with self.subTest(args=args):
                code, out, _ = self.run_script(*args)
                self.assertEqual(code, 2, out)
                self.assertIn(expected, out)
                self.assertNotIn("Traceback", out)

    # --- разбор URL -------------------------------------------------------

    def test_non_ascii_url_is_encoded(self):
        url = speedtest.normalize_url("https://пример.рф/файл.jpg")
        self.assertTrue(url.isascii(), url)
        self.assertIn("xn--", url)

    def test_percent_encoding_is_not_doubled(self):
        url = speedtest.normalize_url("https://example.com/a%20b.bin?x=1%262")
        self.assertIn("/a%20b.bin", url)
        self.assertIn("x=1%262", url)

    def test_cache_buster_keeps_existing_query(self):
        busted = speedtest.add_cache_buster("https://example.com/f?a=1", 3)
        self.assertIn("a=1", busted)
        self.assertIn("_cb=", busted)

    def test_size_suffixes(self):
        self.assertEqual(speedtest.parse_size("1048576"), 1048576)
        self.assertEqual(speedtest.parse_size("2M"), 2 * 1024**2)
        self.assertEqual(speedtest.parse_size("1G"), 1024**3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
