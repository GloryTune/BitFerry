"""Transactional message storage; each query opens its own SQLite connection."""
import json
import sqlite3
import uuid
from collections.abc import MutableMapping
from contextlib import contextmanager
from pathlib import Path


def search_text(record):
    payload = record.get('payload', '')
    if record.get('kind') == 'batch':
        try:
            payload = json.dumps(json.loads(payload), ensure_ascii=False)
        except (ValueError, TypeError):
            pass
    return ' '.join([str(payload)] + [str(record.get(k, '')) for k in ('name', 'day', 'ts')]).casefold()


class HistoryStore(MutableMapping):
    def __init__(self, path, legacy=None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._cache = {}
        self._saved = {}
        with self.connection() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT)')
            db.execute('''CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY, ip TEXT NOT NULL, mid TEXT, day TEXT, ts TEXT,
                seq INTEGER, status TEXT, body TEXT NOT NULL, search TEXT NOT NULL)''')
            db.execute('CREATE INDEX IF NOT EXISTS messages_peer_time ON messages(ip, day, ts, seq)')
            db.execute('CREATE INDEX IF NOT EXISTS messages_mid ON messages(mid)')
            db.execute('CREATE INDEX IF NOT EXISTS messages_status ON messages(status)')
            if not db.execute("SELECT 1 FROM metadata WHERE key='json_migrated'").fetchone():
                source = legacy() if callable(legacy) else legacy
                for ip, records in (source or {}).items():
                    if not isinstance(records, list):
                        continue
                    for record in records:
                        if isinstance(record, dict) and all(k in record for k in ('kind', 'payload', 'mine', 'ts', 'name')):
                            self._write(db, str(uuid.uuid4()), ip, record)
                db.execute("INSERT INTO metadata VALUES ('json_migrated', '1')")

    @contextmanager
    def connection(self):
        db = sqlite3.connect(str(self.path), timeout=5)
        try:
            db.execute('PRAGMA synchronous=FULL')
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _write(db, rid, ip, rec):
        db.execute('''INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET ip=excluded.ip, mid=excluded.mid,
            day=excluded.day, ts=excluded.ts, seq=excluded.seq, status=excluded.status,
            body=excluded.body, search=excluded.search''',
            (rid, ip, rec.get('msg_id'), rec.get('day', ''), rec.get('ts', ''),
             rec.get('seq', 0), rec.get('delivery'), json.dumps(rec, ensure_ascii=False), search_text(rec)))

    def _remember(self, rid, ip, body):
        if rid not in self._cache:
            rec = json.loads(body)
            self._cache[rid] = (ip, rec)
            self._saved[rid] = json.dumps(rec, ensure_ascii=False)
        return self._cache[rid][1]

    def append(self, ip, record):
        self._cache[str(uuid.uuid4())] = (ip, record)

    def save(self):
        changed = [(rid, ip, rec, json.dumps(rec, ensure_ascii=False))
                   for rid, (ip, rec) in self._cache.items()
                   if json.dumps(rec, ensure_ascii=False) != self._saved.get(rid)]
        if changed:
            with self.connection() as db:
                for rid, ip, rec, body in changed:
                    self._write(db, rid, ip, rec)
            for rid, ip, rec, body in changed:
                self._saved[rid] = body
        # Keep only current/recent records; old messages are loaded on demand.
        if len(self._cache) > 512:
            for rid in list(self._cache)[:-256]:
                self._cache.pop(rid, None)
                self._saved.pop(rid, None)
        return True

    def find(self, mid):
        for ip, rec in self._cache.values():
            if rec.get('msg_id') == mid:
                return ip, rec
        with self.connection() as db:
            row = db.execute('SELECT id, ip, body FROM messages WHERE mid=? LIMIT 1', (mid,)).fetchone()
        return (row[1], self._remember(*row)) if row else (None, None)

    def recover(self):
        with self.connection() as db:
            for rid, ip, body in db.execute("SELECT id, ip, body FROM messages WHERE status IN ('sending','cancelling')").fetchall():
                rec = json.loads(body)
                rec.update(delivery='unconfirmed', pending=False)
                self._write(db, rid, ip, rec)

    def __iter__(self):
        with self.connection() as db:
            keys = [row[0] for row in db.execute('SELECT DISTINCT ip FROM messages')]
        return iter(dict.fromkeys(keys + [ip for ip, rec in self._cache.values()]))

    def __len__(self):
        return len(list(iter(self)))

    def __getitem__(self, ip):
        with self.connection() as db:
            rows = db.execute('SELECT id, ip, body FROM messages WHERE ip=? ORDER BY day,ts,seq,rowid', (ip,)).fetchall()
        records = [self._remember(*row) for row in rows]
        ids = {r[0] for r in rows}
        records += [rec for rid, (peer, rec) in self._cache.items() if peer == ip and rid not in ids]
        if not records:
            raise KeyError(ip)
        return records

    def __setitem__(self, ip, records):
        self.__delitem__(ip)
        for record in records:
            self.append(ip, record)

    def __delitem__(self, ip):
        with self.connection() as db:
            db.execute('DELETE FROM messages WHERE ip=?', (ip,))
        for rid, (peer, rec) in list(self._cache.items()):
            if peer == ip:
                self._cache.pop(rid)
                self._saved.pop(rid, None)

    def __contains__(self, ip):
        if any(peer == ip for peer, rec in self._cache.values()):
            return True
        with self.connection() as db:
            return db.execute('SELECT 1 FROM messages WHERE ip=? LIMIT 1', (ip,)).fetchone() is not None

    def page(self, ips, query='', page=0, size=50, cancel=None):
        if not ips:
            return 0, 0, []
        where = 'ip IN (' + ','.join('?' for _ in ips) + ')'
        args = list(ips)
        if query:
            where += ' AND instr(search, ?) > 0'
            args.append(query.casefold())
        with self.connection() as db:
            if cancel is not None:
                db.set_progress_handler(lambda: int(cancel.is_set()), 1000)
            db.execute('BEGIN')
            total = db.execute('SELECT count(*) FROM messages WHERE ' + where, args).fetchone()[0]
            last = max(0, (total - 1) // size)
            page = last if page < 0 else max(0, min(page, last))
            rows = db.execute('SELECT body FROM messages WHERE ' + where +
                ' ORDER BY day,ts,seq,rowid LIMIT ? OFFSET ?', args + [size, page * size]).fetchall()
        return total, page, [json.loads(r[0]) for r in rows]

    def context_page(self, ips, record, size=50):
        with self.connection() as db:
            if record.get('msg_id'):
                row = db.execute('SELECT day,ts,seq,rowid FROM messages WHERE mid=? LIMIT 1', (record['msg_id'],)).fetchone()
            else:
                row = db.execute('SELECT day,ts,seq,rowid FROM messages WHERE body=? LIMIT 1', (json.dumps(record, ensure_ascii=False),)).fetchone()
            if row is None or not ips:
                return 0
            where = 'ip IN (' + ','.join('?' for _ in ips) + ')'
            before = db.execute('SELECT count(*) FROM messages WHERE '+where+' AND (day,ts,seq,rowid)<(?,?,?,?)', list(ips)+list(row)).fetchone()[0]
            return before // size

    def referenced_records(self):
        with self.connection() as db:
            for row in db.execute('SELECT body FROM messages'):
                yield json.loads(row[0])
