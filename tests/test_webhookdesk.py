import contextlib
import http.client
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import webhookdesk as w

SECRET = 'test-only-token-with-at-least-24-characters'


@contextlib.contextmanager
def running(handler):
    server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port
    finally:
        server.shutdown(); server.server_close(); thread.join()


def post(port, path, body=b'hello', headers=None):
    c = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
    h = {'Authorization': 'Bearer ' + SECRET}
    h.update(headers or {})
    c.request('POST', path, body, h)
    r = c.getresponse(); result = r.status, json.loads(r.read()); c.close()
    return result


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / 'capture.db')
        self.db = w.connect(self.path)
        with self.db:
            self.db.execute('INSERT INTO events VALUES(?,?,?,?)', ('event1', 0, '[]', b'hello'))

    def tearDown(self):
        self.db.close(); self.tmp.cleanup()

    def test_capture_persists_binary_and_redacts(self):
        with running(w.handler(self.path, SECRET)) as port:
            code, result = post(port, '/capture', b'\xff\x00', {'Cookie': 'private', 'X-API-Key': 'private'})
        self.assertEqual(code, 201)
        other = w.connect(self.path)
        try:
            report = w.inspect(other, result['id'])
            self.assertEqual(report['body_base64'], '/wA=')
            self.assertNotIn('private', json.dumps(report['headers']))
            self.assertNotIn(SECRET, json.dumps(report))
        finally:
            other.close()

    def test_auth_origin_and_route_rejected(self):
        with running(w.handler(self.path, SECRET)) as port:
            self.assertEqual(post(port, '/capture', headers={'Authorization': 'wrong'})[0], 401)
            self.assertEqual(post(port, '/capture', headers={'Origin': 'http://evil.test'})[0], 403)
            self.assertEqual(post(port, '/other')[0], 404)
        self.assertEqual(self.db.execute('SELECT count(*) FROM events').fetchone()[0], 1)

    def test_size_and_chunking_rejected(self):
        with running(w.handler(self.path, SECRET)) as port:
            self.assertEqual(post(port, '/capture', headers={'Content-Length': str(w.MAX_BODY + 1)})[0], 413)
            self.assertEqual(post(port, '/capture', headers={'Transfer-Encoding': 'chunked'})[0], 400)
            self.assertEqual(post(port, '/capture', headers={'Content-Length': '-1'})[0], 400)

    def test_receiver_persistent_idempotency_and_conflict(self):
        path = str(Path(self.tmp.name) / 'receiver.db')
        with running(w.handler(path, SECRET, True)) as port:
            url = {'demo': f'http://127.0.0.1:{port}/receive'}
            a = w.replay(self.db, 'event1', 'demo', url, SECRET)
            b = w.replay(self.db, 'event1', 'demo', url, SECRET)
            self.assertFalse(json.loads(a[0]['detail'])['duplicate'])
            self.assertTrue(json.loads(b[0]['detail'])['duplicate'])
            self.assertEqual(post(port, '/receive', b'changed', {'Idempotency-Key': 'event1'})[0], 409)
            self.assertEqual(post(port, '/receive')[0], 400)
        with running(w.handler(path, SECRET, True)) as port:
            self.assertTrue(post(port, '/receive', headers={'Idempotency-Key': 'event1'})[1]['duplicate'])
        self.assertEqual(len(w.inspect(self.db, 'event1')['attempts']), 2)

    def test_target_configuration(self):
        path = Path(self.tmp.name) / 'targets.json'
        for url in ['https://example.com/x', 'http://localhost:123/x', 'http://127.0.0.1:123/x?y=1',
                    'http://127.0.0.1:123/x#frag', 'http://user@127.0.0.1:123/x', 'http://127.0.0.1/x',
                    'http://127.0.0.1:123/x\n', 'http://127.0.0.2:123/x']:
            path.write_text(json.dumps({'demo': url}))
            with self.assertRaises(ValueError): w.targets(path)
        path.write_text('{"demo":"http://127.0.0.1:8766/receive"}')
        self.assertIn('demo', w.targets(path))

    def test_unknown_event_target_and_retry_bounds(self):
        for args in [('unknown', 'demo', {'demo': 'http://127.0.0.1:9/x'}, 0),
                     ('event1', 'unknown', {}, 0), ('event1', 'demo', {}, 3)]:
            with self.assertRaises(ValueError): w.replay(self.db, args[0], args[1], args[2], SECRET, args[3])

    def test_retries_and_header_isolation(self):
        calls = []
        class RetryHandler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                calls.append((dict(self.headers), self.rfile.read(int(self.headers['Content-Length']))))
                self.send_response(503 if len(calls) == 1 else 200)
                self.end_headers(); self.wfile.write(b'ok')
        with running(RetryHandler) as port:
            outcomes = w.replay(self.db, 'event1', 'demo', {'demo': f'http://127.0.0.1:{port}/receive'}, SECRET, 2)
        self.assertEqual([r['status'] for r in outcomes], [503, 200])
        self.assertEqual(calls[0][0]['Idempotency-Key'], calls[1][0]['Idempotency-Key'])
        self.assertEqual(calls[0][1], b'hello')
        self.assertNotIn('Cookie', calls[0][0])

    def test_no_redirect_follow_or_client_error_retry(self):
        for status in (302, 400, 401):
            class StopHandler(BaseHTTPRequestHandler):
                def log_message(self, *args): pass
                def do_POST(self):
                    self.send_response(status)
                    self.send_header('Location', 'http://example.com/')
                    self.end_headers()
            with running(StopHandler) as port:
                outcomes = w.replay(self.db, 'event1', 'demo', {'demo': f'http://127.0.0.1:{port}/x'}, SECRET, 2)
            self.assertEqual(len(outcomes), 1)
            self.assertEqual(outcomes[0]['status'], status)

    def test_transport_failure_recorded(self):
        with patch('webhookdesk.http.client.HTTPConnection.request', side_effect=ConnectionRefusedError):
            result = w.replay(self.db, 'event1', 'demo', {'demo':'http://127.0.0.1:9/x'}, SECRET, 1)
        self.assertEqual(len(result), 2)
        self.assertIsNone(result[0]['status'])
        self.assertEqual(result[0]['detail'], 'ConnectionRefusedError')

    def test_secret_validation(self):
        for value in ('', 'short', 'a' * 25 + '\n', 'é' * 24):
            with patch.dict(os.environ, {'WEBHOOKDESK_TOKEN': value}):
                with self.assertRaises(ValueError): w.token()
        with patch.dict(os.environ, {'WEBHOOKDESK_TOKEN': SECRET}): self.assertEqual(w.token(), SECRET)


if __name__ == '__main__': unittest.main()
