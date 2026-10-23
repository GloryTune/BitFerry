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


def belongs_to_device(record, peer_ip, devs, ips):
    """dev 优先、ip 兜底：带设备标识的记录只认设备，老记录(没有 dev)才认 IP。"""
    dev = record.get('dev')
    return dev in devs if dev else peer_ip in ips


class HistoryStore(MutableMapping):
    def __init__(self, path, legacy=None, identities=None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._cache = {}
        self._saved = {}
        with self.connection() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT)')
            db.execute('''CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY, ip TEXT NOT NULL, mid TEXT, day TEXT, ts TEXT,
                seq INTEGER, status TEXT, body TEXT NOT NULL, search TEXT NOT NULL,
                dev TEXT)''')
            if 'dev' not in {row[1] for row in db.execute('PRAGMA table_info(messages)')}:
                db.execute('ALTER TABLE messages ADD COLUMN dev TEXT')     # 升级旧库
            db.execute('CREATE INDEX IF NOT EXISTS messages_peer_time ON messages(ip, day, ts, seq)')
            db.execute('CREATE INDEX IF NOT EXISTS messages_device_time ON messages(dev, day, ts, seq)')
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
            self._backfill_dev(db, identities() if callable(identities) else identities)

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
        db.execute('''INSERT INTO messages (id, ip, mid, day, ts, seq, status, body, search, dev)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET ip=excluded.ip, mid=excluded.mid,
            day=excluded.day, ts=excluded.ts, seq=excluded.seq, status=excluded.status,
            body=excluded.body, search=excluded.search, dev=excluded.dev''',
            (rid, ip, rec.get('msg_id'), rec.get('day', ''), rec.get('ts', ''),
             rec.get('seq', 0), rec.get('delivery'), json.dumps(rec, ensure_ascii=False),
             search_text(rec), rec.get('dev')))

    @staticmethod
    def _scope(devs, ips):
        """把 belongs_to_device 翻译成 SQL 条件：dev 命中设备，或老记录按 IP 兜底。"""
        parts, args = [], []
        if devs:
            parts.append('dev IN (' + ','.join('?' for _ in devs) + ')')
            args += list(devs)
        if ips:
            parts.append('(dev IS NULL AND ip IN (' + ','.join('?' for _ in ips) + '))')
            args += list(ips)
        return '(' + ' OR '.join(parts) + ')', args

    def _backfill_dev(self, db, names):
        """老记录只按 IP 存，IP 被别的机器拿到后就会整段串台。这里按「同一 IP 同一天
        只和一台设备说话」把它们认领回原主：当天该 IP 下收到的消息只来自一台已知设备
        时，当天全部记录(含自己发出的)归给它；当天分不清就只认领能确定发信人的那几条，
        其余留空继续按 IP 兜底。"""
        if not names or db.execute("SELECT 1 FROM metadata WHERE key='dev_backfilled'").fetchone():
            return
        groups = {}
        for rid, ip, day, body in db.execute('SELECT id, ip, day, body FROM messages WHERE dev IS NULL'):
            groups.setdefault((ip, day), []).append((rid, json.loads(body)))
        for items in groups.values():
            senders = {names[rec['name']] for rid, rec in items
                       if not rec.get('mine') and rec.get('name') in names}
            for rid, rec in items:
                if len(senders) == 1:
                    rec['dev'] = next(iter(senders))
                elif not rec.get('mine') and rec.get('name') in names:
                    rec['dev'] = names[rec['name']]
                else:
                    continue
                db.execute('UPDATE messages SET dev=?, body=? WHERE id=?',
                           (rec['dev'], json.dumps(rec, ensure_ascii=False), rid))
        db.execute("INSERT INTO metadata VALUES ('dev_backfilled', '1')")

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

    def delete_device(self, ips, devs=()):
        """删除设备时按归属删：同一 IP 下属于别的设备的记录要留下。"""
        if not ips and not devs:
            return
        where, args = self._scope(devs, ips)
        with self.connection() as db:
            db.execute('DELETE FROM messages WHERE ' + where, args)
        for rid, (peer, rec) in list(self._cache.items()):
            if belongs_to_device(rec, peer, devs, ips):
                self._cache.pop(rid)
                self._saved.pop(rid, None)

    def count(self, ips, devs=()):
        """归属该设备的记录条数：库里已落盘的，加上还留在缓存里没落盘的。"""
        total = self.page(ips, page=0, size=1, devs=devs)[0]
        return total + sum(1 for rid, (peer, rec) in self._cache.items()
                           if rid not in self._saved and belongs_to_device(rec, peer, devs, ips))

    def __contains__(self, ip):
        if any(peer == ip for peer, rec in self._cache.values()):
            return True
        with self.connection() as db:
            return db.execute('SELECT 1 FROM messages WHERE ip=? LIMIT 1', (ip,)).fetchone() is not None

    def page(self, ips, query='', page=0, size=50, cancel=None, devs=()):
        if not ips and not devs:
            return 0, 0, []
        where, args = self._scope(devs, ips)
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

    def context_page(self, ips, record, size=50, devs=()):
        with self.connection() as db:
            if record.get('msg_id'):
                row = db.execute('SELECT day,ts,seq,rowid FROM messages WHERE mid=? LIMIT 1', (record['msg_id'],)).fetchone()
            else:
                row = db.execute('SELECT day,ts,seq,rowid FROM messages WHERE body=? LIMIT 1', (json.dumps(record, ensure_ascii=False),)).fetchone()
            if row is None or not (ips or devs):
                return 0
            where, args = self._scope(devs, ips)
            before = db.execute('SELECT count(*) FROM messages WHERE ' + where +
                ' AND (day,ts,seq,rowid)<(?,?,?,?)', args + list(row)).fetchone()[0]
            return before // size

    def referenced_records(self):
        with self.connection() as db:
            for row in db.execute('SELECT body FROM messages'):
                yield json.loads(row[0])
