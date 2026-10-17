"""Local-only webhook capture, inspection and deliberate replay. Python 3.12+."""
import argparse
import base64
import hashlib
import hmac
import http.client
import json
import os
import re
import sqlite3
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

MAX_BODY = 1024 * 1024


def connect(path):
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    db.executescript('''
    CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY, created REAL, headers TEXT, body BLOB);
    CREATE TABLE IF NOT EXISTS attempts(id INTEGER PRIMARY KEY, event TEXT, target TEXT,
        created REAL, status INTEGER, detail TEXT);
    CREATE TABLE IF NOT EXISTS receipts(key TEXT PRIMARY KEY, digest TEXT, created REAL);
    ''')
    return db


def token():
    value = os.environ.get('WEBHOOKDESK_TOKEN', '')
    if len(value) < 24 or not value.isascii() or any(c.isspace() for c in value):
        raise ValueError('WEBHOOKDESK_TOKEN must contain at least 24 non-whitespace ASCII characters')
    return value


def targets(path):
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(data, dict) or not data:
        raise ValueError('targets must be a nonempty object of names to URLs')
    for name, url in data.items():
        if not isinstance(name, str) or not re.fullmatch(r'[a-zA-Z0-9_-]+', name) or not isinstance(url, str):
            raise ValueError('invalid target name or URL')
        p = urlsplit(url)
        if (p.scheme != 'http' or p.hostname != '127.0.0.1' or p.username or p.password
                or p.fragment or p.query or not p.port or not p.path.startswith('/')
                or any(ord(c) < 33 or ord(c) > 126 for c in url)):
            raise ValueError('targets require http://127.0.0.1:PORT/path without credentials/query/fragment')
    return data


def event(db, event_id):
    row = db.execute('SELECT * FROM events WHERE id=?', (event_id,)).fetchone()
    if row is None:
        raise ValueError('unknown event')
    return row


def inspect(db, event_id):
    row = event(db, event_id)
    body = bytes(row['body'])
    return dict(id=row['id'], created=row['created'], headers=json.loads(row['headers']),
                body_base64=base64.b64encode(body).decode(), body_utf8=body.decode('utf-8', errors='replace'),
                attempts=[dict(r) for r in db.execute('SELECT * FROM attempts WHERE event=? ORDER BY id', (event_id,))])


def replay(db, event_id, target_name, allowed, secret, retries=0):
    if type(retries) is not int or not 0 <= retries <= 2:
        raise ValueError('retries must be 0..2')
    if target_name not in allowed:
        raise ValueError('target not allowlisted')
    row = event(db, event_id)
    p = urlsplit(allowed[target_name])
    # Validate even when called directly rather than through the CLI loader.
    if p.scheme != 'http' or p.hostname != '127.0.0.1' or not p.port or p.username or p.password or p.query or p.fragment:
        raise ValueError('only literal IPv4 loopback HTTP targets are supported')
    outcomes = []
    for attempt in range(retries + 1):
        conn = http.client.HTTPConnection('127.0.0.1', p.port, timeout=3)
        status = None
        try:
            conn.request('POST', p.path or '/', bytes(row['body']), {
                'Content-Type': 'application/octet-stream',
                'Authorization': 'Bearer ' + secret,
                'Idempotency-Key': event_id,
                'X-WebhookDesk-Event': event_id})
            response = conn.getresponse()
            status = response.status
            detail = response.read(4096).decode('utf-8', errors='replace')
        except (OSError, http.client.HTTPException) as exc:
            detail = type(exc).__name__
        finally:
            conn.close()
        with db:
            db.execute('INSERT INTO attempts(event,target,created,status,detail) VALUES(?,?,?,?,?)',
                       (event_id, target_name, time.time(), status, detail))
        outcomes.append({'status': status, 'detail': detail})
        if status is not None and status != 429 and status < 500:
            break  # Never follow redirects; do not retry client failures.
        if attempt < retries:
            time.sleep(0.1 * (2 ** attempt))
    return outcomes


def handler(db_path, secret, receiver=False):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Avoid logging captured secrets or terminal control characters.

        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def reply(self, code, data):
            body = json.dumps(data).encode()
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if self.headers.get('Origin') is not None:
                return self.reply(403, {'error': 'browser origins are not supported'})
            auth = self.headers.get_all('Authorization', [])
            if len(auth) != 1 or not hmac.compare_digest(auth[0].encode(), ('Bearer ' + secret).encode()):
                return self.reply(401, {'error': 'unauthorized'})
            if self.path != ('/receive' if receiver else '/capture'):
                return self.reply(404, {'error': 'unknown route'})
            sizes = self.headers.get_all('Content-Length', [])
            if self.headers.get('Transfer-Encoding') or len(sizes) != 1 or not re.fullmatch(r'[0-9]+', sizes[0]):
                return self.reply(400, {'error': 'one Content-Length required; chunking unsupported'})
            if len(sizes[0]) > 10:
                return self.reply(413, {'error': 'Content-Length too large'})
            length = int(sizes[0])
            if length > MAX_BODY:
                return self.reply(413, {'error': 'body exceeds 1 MiB'})
            try:
                body = self.rfile.read(length)
            except OSError:
                return self.reply(408, {'error': 'body read timeout'})
            if len(body) != length:
                return self.reply(400, {'error': 'truncated body'})
            db = connect(db_path)
            try:
                with db:
                    if receiver:
                        keys = self.headers.get_all('Idempotency-Key', [])
                        if len(keys) != 1 or not re.fullmatch(r'[a-zA-Z0-9_-]{1,128}', keys[0]):
                            return self.reply(400, {'error': 'one valid Idempotency-Key required'})
                        key = keys[0]
                        digest = hashlib.sha256(body).hexdigest()
                        cursor = db.execute('INSERT OR IGNORE INTO receipts VALUES(?,?,?)', (key, digest, time.time()))
                        duplicate = cursor.rowcount == 0
                        prior = db.execute('SELECT digest FROM receipts WHERE key=?', (key,)).fetchone()[0]
                        if prior != digest:
                            return self.reply(409, {'error': 'key reused with different body'})
                        result = {'duplicate': duplicate, 'idempotency_key': key}
                        code = 200
                    else:
                        event_id = str(uuid.uuid4())
                        # Preserve repeated headers, but redact common credential headers.
                        headers = [[k, '[REDACTED]' if k.lower() in {'authorization', 'cookie', 'x-api-key', 'proxy-authorization'} else v]
                                   for k, v in self.headers.items()]
                        db.execute('INSERT INTO events VALUES(?,?,?,?)', (event_id, time.time(), json.dumps(headers), body))
                        result, code = {'id': event_id}, 201
                self.reply(code, result)  # Respond only after transaction commits.
            finally:
                db.close()
    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', default='webhookdesk.sqlite3')
    sub = parser.add_subparsers(dest='command', required=True)
    for name, port in [('serve', 8765), ('receiver', 8766)]:
        p = sub.add_parser(name)
        p.add_argument('--port', type=int, default=port)
    sub.add_parser('list')
    p = sub.add_parser('show'); p.add_argument('id')
    p = sub.add_parser('replay'); p.add_argument('id'); p.add_argument('--target', required=True)
    p.add_argument('--targets', default='targets.json'); p.add_argument('--retries', type=int, default=0)
    args = parser.parse_args()
    try:
        if args.command in ('serve', 'receiver'):
            server = ThreadingHTTPServer(('127.0.0.1', args.port), handler(args.db, token(), args.command == 'receiver'))
            print(f'{args.command} listening on http://127.0.0.1:{server.server_port}', flush=True)
            try:
                server.serve_forever()
            finally:
                server.server_close()
        else:
            db = connect(args.db)
            try:
                if args.command == 'list':
                    result = [dict(r) for r in db.execute('SELECT id,created,length(body) AS bytes FROM events ORDER BY created')]
                elif args.command == 'show':
                    result = inspect(db, args.id)
                else:
                    result = replay(db, args.id, args.target, targets(args.targets), token(), args.retries)
                print(json.dumps(result, indent=2))
            finally:
                db.close()
    except (ValueError, OSError, sqlite3.Error) as exc:
        parser.exit(2, f'Error: {exc}\n')
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
