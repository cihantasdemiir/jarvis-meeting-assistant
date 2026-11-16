"""Senaryolu sahte SMTP sunucusu (127.0.0.1): STARTTLS, doğrudan SSL, AUTH PLAIN/LOGIN ve bağlantı kopması.

Gerçek Gmail'e veya internete bağlanılmaz. TLS için `tests/data/smtp_test_cert.pem` (yalnızca test amaçlı,
kendinden imzalı, CN=localhost) kullanılır. Gelen AUTH satırları kayıtta gizlenir.
"""
from __future__ import annotations

import base64
import socket
import ssl
import threading
from dataclasses import dataclass, field
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
CERT = DATA_DIR / "smtp_test_cert.pem"
KEY = DATA_DIR / "smtp_test_key.pem"


def client_context() -> ssl.SSLContext:
    """İstemcinin test sertifikasına güvenmesi için (sistem deposu yerine)."""
    context = ssl.create_default_context(cafile=str(CERT))
    return context


@dataclass
class Script:
    tls: bool = True  # STARTTLS ilan et
    direct_ssl: bool = False  # 465 gibi: bağlantı baştan TLS
    broken_tls: bool = False  # STARTTLS'ten sonra TLS el sıkışması yerine çöp gönder
    auth_advertise: str = "LOGIN PLAIN XOAUTH2"
    user: str = "jarvis.test@example.com"
    password: str = "abcdefghijklmnop"
    auth_fail: tuple[int, str] | None = None  # her girişte bu yanıt (ör. 535 ...)
    close_after_auth_fail: bool = False  # Gmail benzeri: reddedilen girişten sonra bağlantıyı kapat
    close_on: set[str] = field(default_factory=set)  # "AUTH", "MAIL", "RCPT", "DATA", "DATA_END", "STARTTLS"
    reject_rcpt: dict[str, str] = field(default_factory=dict)  # adres -> "550 5.1.1 ..."
    data_reply: str = "250 2.0.0 OK 1760000000 test-gsmtp"


class FakeSmtpServer:
    def __init__(self, script: Script | None = None):
        self.script = script or Script()
        self.commands: list[str] = []
        self.messages: list[bytes] = []
        self.auth_attempts = 0
        self.logged_in = False
        self._listener = socket.socket()
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(5)
        self.port = self._listener.getsockname()[1]
        self._server_ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        self._server_ctx.load_cert_chain(str(CERT), str(KEY))
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        try:
            self._listener.close()
        except OSError:
            pass
        self._thread.join(3)

    # ---------------------------------------------------------------- sunucu
    def _serve(self) -> None:
        self._listener.settimeout(0.2)
        while not self._stop.is_set():
            try:
                conn, _ = self._listener.accept()
            except (socket.timeout, OSError):
                continue
            try:
                self._session(conn)
            except (OSError, ssl.SSLError, ValueError):
                pass
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

    def _session(self, conn: socket.socket) -> None:
        s = self.script
        conn.settimeout(5)
        if s.direct_ssl:
            conn = self._server_ctx.wrap_socket(conn, server_side=True)
        reader = conn.makefile("rb")
        secure = s.direct_ssl

        def send(line: str) -> None:
            conn.sendall((line + "\r\n").encode())

        send("220 fake.smtp ESMTP ready")
        while True:
            raw = reader.readline()
            if not raw:
                return
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            verb = line.split(" ", 1)[0].upper()
            self.commands.append("AUTH ***" if verb == "AUTH" else line)
            if verb in s.close_on:
                return  # bağlantıyı yanıt vermeden kapat
            if verb in ("EHLO", "HELO"):
                lines = ["250-fake.smtp", "250-SIZE 35882577", "250-8BITMIME"]
                if s.tls and not secure:
                    lines.append("250-STARTTLS")
                if secure or not s.tls:
                    lines.append(f"250-AUTH {s.auth_advertise}")
                lines.append("250 SMTPUTF8")
                for item in lines:
                    send(item)
            elif verb == "STARTTLS":
                send("220 2.0.0 Ready to start TLS")
                if s.broken_tls:
                    conn.sendall(b"this is not tls\r\n")
                    return
                conn = self._server_ctx.wrap_socket(conn, server_side=True)
                reader = conn.makefile("rb")
                secure = True
            elif verb == "AUTH":
                self.auth_attempts += 1
                if not secure and s.tls:
                    send("530 5.7.0 Must issue a STARTTLS command first.")
                    continue
                user, password = self._read_auth(line, reader, send)
                if s.auth_fail is not None or (user, password) != (s.user, s.password):
                    code, text = s.auth_fail or (535, "5.7.8 Username and Password not accepted. BadCredentials")
                    send(f"{code} {text}")
                    if s.close_after_auth_fail:
                        return
                    continue
                self.logged_in = True
                send("235 2.7.0 Accepted")
            elif verb == "MAIL":
                send("250 2.1.0 OK")
            elif verb == "RCPT":
                address = line.split("<", 1)[-1].split(">", 1)[0].lower()
                if address in s.reject_rcpt:
                    send(s.reject_rcpt[address])
                else:
                    send("250 2.1.5 OK")
            elif verb == "DATA":
                send("354 Go ahead")
                body = bytearray()
                while True:
                    chunk = reader.readline()
                    if not chunk:
                        return
                    if chunk in (b".\r\n", b".\n"):
                        break
                    body += chunk
                if "DATA_END" in s.close_on:
                    return
                self.messages.append(bytes(body))
                send(s.data_reply)
            elif verb == "RSET" or verb == "NOOP":
                send("250 OK")
            elif verb == "QUIT":
                send("221 2.0.0 closing connection")
                return
            else:
                send("502 5.5.1 Unrecognized command")

    def _read_auth(self, line: str, reader, send) -> tuple[str, str]:
        parts = line.split()
        mechanism = parts[1].upper() if len(parts) > 1 else ""
        if mechanism == "PLAIN":
            payload = parts[2] if len(parts) > 2 else None
            if payload is None:
                send("334 ")
                payload = reader.readline().decode().strip()
            decoded = base64.b64decode(payload).split(b"\0")
            return decoded[1].decode(), decoded[2].decode()
        if mechanism == "LOGIN":
            if len(parts) > 2:
                user = base64.b64decode(parts[2]).decode()
            else:
                send("334 VXNlcm5hbWU6")
                user = base64.b64decode(reader.readline().strip()).decode()
            send("334 UGFzc3dvcmQ6")
            password = base64.b64decode(reader.readline().strip()).decode()
            return user, password
        send("504 5.5.4 Unrecognized authentication type")
        return "", ""
