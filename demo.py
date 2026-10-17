"""Synthetic local HTTP integration demo; all data stays in a temporary directory."""
import json
from pathlib import Path
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
from urllib.request import Request, urlopen
import webhookdesk as w


def main():
    started = time.perf_counter()
    secret = 'synthetic-demo-token-not-for-real-data'
    with tempfile.TemporaryDirectory() as directory:
        paths = [str(Path(directory) / name) for name in ('capture.db', 'receiver.db')]
        servers = [ThreadingHTTPServer(('127.0.0.1', 0), w.handler(path, secret, i == 1))
                   for i, path in enumerate(paths)]
        threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in servers]
        for t in threads: t.start()
        try:
            body = Path(__file__).with_name('samples').joinpath('order.json').read_bytes()
            req = Request(f'http://127.0.0.1:{servers[0].server_port}/capture', body,
                          {'Authorization': 'Bearer ' + secret, 'Content-Type': 'application/json'})
            with urlopen(req, timeout=3) as response:
                event_id = json.load(response)['id']
            db = w.connect(paths[0])
            try:
                allowed = {'demo': f'http://127.0.0.1:{servers[1].server_port}/receive'}
                results = [w.replay(db, event_id, 'demo', allowed, secret) for _ in range(2)]
                report = w.inspect(db, event_id)
            finally: db.close()
            receiver = w.connect(paths[1])
            try: receipts = receiver.execute('SELECT count(*) FROM receipts').fetchone()[0]
            finally: receiver.close()
            assert receipts == 1 and len(report['attempts']) == 2
            assert [json.loads(x[0]['detail'])['duplicate'] for x in results] == [False, True]
            print(json.dumps({'fixture': 'synthetic order; real local HTTP and SQLite',
                              'captured_events': 1, 'replay_attempts': 2, 'unique_receipts': receipts,
                              'statuses': [x[0]['status'] for x in results],
                              'duplicate_flags': [False, True],
                              'elapsed_seconds': round(time.perf_counter() - started, 4)}, indent=2))
        finally:
            for s in servers: s.shutdown(); s.server_close()
            for t in threads: t.join()


if __name__ == '__main__': main()
