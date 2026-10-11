#!/usr/bin/env python3
"""Standard-library webhook receiver and persistent transcription queue."""

import hmac
import json
import logging
import os
import re
import secrets
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


FILE_PATTERN = re.compile(r"^[0-9]+-[0-9a-f]{8}\.json$")
QUEUE_LOCK = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        logging.info("%s %s %s", self.command, self.path, format % args)

    def log_request(self, code="-", size="-"):
        logging.info("%s %s %s", self.command, self.path, code)

    def respond(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def authorized(self):
        supplied = self.headers.get("Authorization", "").encode("utf-8")
        expected = ("Bearer " + self.server.token).encode("utf-8")
        if hmac.compare_digest(supplied, expected):
            return True
        self.respond(401, {"error": "unauthorized"})
        return False

    def read_body(self):
        if self.headers.get("Transfer-Encoding"):
            raise ValueError("Transfer-Encoding is not supported")
        length = int(self.headers.get("Content-Length", "0"))
        if length < 0:
            raise ValueError("invalid Content-Length")
        body = self.rfile.read(length)
        if len(body) != length:
            raise ValueError("incomplete body")
        return body

    def queue_files(self):
        return [p for p in self.server.queue_dir.iterdir()
                if FILE_PATTERN.fullmatch(p.name) and p.is_file()]

    def extract_text(self, body):
        content_type = self.headers.get("Content-Type", "text/plain")
        media_type = content_type.split(";", 1)[0].strip().lower()
        if media_type == "application/json":
            data = json.loads(body)
            if not isinstance(data, dict):
                raise ValueError("expected a JSON object")
            value = data.get("text", "")
            return value.strip() if isinstance(value, str) else ""
        if media_type == "application/x-www-form-urlencoded":
            fields = parse_qs(body.decode("utf-8"), keep_blank_values=True)
        elif media_type == "multipart/form-data":
            message = BytesParser(policy=policy.default).parsebytes(
                b"Content-Type: " + content_type.encode("utf-8")
                + b"\r\nMIME-Version: 1.0\r\n\r\n" + body)
            if not message.is_multipart():
                raise ValueError("invalid multipart body")
            fields = {}
            for part in message.iter_parts():
                name = part.get_param("name", header="content-disposition")
                if name in ("transcription", "text", "transcript"):
                    value = (part.get_payload(decode=True) or b"").decode(
                        part.get_content_charset() or "utf-8")
                    fields.setdefault(name, []).append(value)
        elif media_type == "text/plain":
            return body.decode("utf-8").strip()
        else:
            raise ValueError("unsupported Content-Type")
        for name in ("transcription", "text", "transcript"):
            for value in fields.get(name, []):
                if value.strip():
                    return value.strip()
        return ""

    def delete_item(self, item_id):
        filename = item_id + ".json" if isinstance(item_id, str) else ""
        if not FILE_PATTERN.fullmatch(filename):
            self.respond(404, {"error": "not found"})
            return
        with QUEUE_LOCK:
            try:
                (self.server.queue_dir / filename).unlink()
            except FileNotFoundError:
                self.respond(404, {"error": "not found"})
                return
        self.respond(200, {"ok": True})

    def do_GET(self):
        path = urlsplit(self.path).path
        if path.startswith("/queue/") and not self.authorized():
            return
        if path == "/health":
            with QUEUE_LOCK:
                count = len(self.queue_files())
            self.respond(200, {"ok": True, "pending": count})
        elif path == "/queue/pending":
            with QUEUE_LOCK:
                pending = [json.loads(p.read_text(encoding="utf-8"))
                           for p in self.queue_files()]
            pending.sort(key=lambda item: (item["received_at"], item["id"]))
            self.respond(200, {"pending": pending})
        else:
            self.respond(404, {"error": "not found"})

    def do_POST(self):
        path = urlsplit(self.path).path
        if path.startswith("/queue/") and not self.authorized():
            return
        if path not in ("/webhook", "/webhook/", "/queue/ack"):
            self.respond(404, {"error": "not found"})
            return
        try:
            body = self.read_body()
            if path == "/queue/ack":
                data = json.loads(body)
                if not isinstance(data, dict):
                    raise ValueError("expected a JSON object")
                self.delete_item(data.get("id"))
                return
            text = self.extract_text(body)
        except (ValueError, UnicodeError, LookupError) as error:
            self.respond(400, {"error": str(error)})
            return
        if not text:
            self.respond(400, {"error": "empty text"})
            return
        with QUEUE_LOCK:
            item_id = f"{int(time.time())}-{secrets.token_hex(4)}"
            while (self.server.queue_dir / (item_id + ".json")).exists():
                item_id = f"{int(time.time())}-{secrets.token_hex(4)}"
            item = {"id": item_id, "text": text,
                    "received_at": datetime.now(timezone.utc).isoformat()}
            # Publish complete files atomically so polling never sees partial JSON.
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                                             dir=self.server.queue_dir,
                                             delete=False) as temporary:
                temporary_path = Path(temporary.name)
                try:
                    json.dump(item, temporary, ensure_ascii=False)
                    temporary.flush()
                    os.fsync(temporary.fileno())
                except BaseException:
                    temporary_path.unlink(missing_ok=True)
                    raise
            temporary_path.replace(self.server.queue_dir / (item_id + ".json"))
        self.respond(200, {"ok": True, "id": item_id})

    def do_DELETE(self):
        path = urlsplit(self.path).path
        if path.startswith("/queue/"):
            if self.authorized():
                self.delete_item(path[len("/queue/"):])
        else:
            self.respond(404, {"error": "not found"})


def main():
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)sZ %(message)s")
    logging.Formatter.converter = time.gmtime
    token = os.environ.get("WEBHOOK_TOKEN")
    if not token:
        logging.error("WEBHOOK_TOKEN is required")
        return 1
    queue_dir = Path(os.environ.get("QUEUE_DIR", "./queue"))
    queue_dir.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8787"))), Handler)
    server.token = token
    server.queue_dir = queue_dir
    logging.info("Listening on 0.0.0.0:%s", server.server_port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
