"""Persistent receipt policies, claims, and authenticated Minecraft identities."""
import hashlib
import json
import re
import secrets
import sqlite3
import time


class ReceiptError(ValueError):
    pass


class Redemptions:
    def __init__(self, db):
        self.db = db
        db.executescript('''
        CREATE TABLE IF NOT EXISTS receipt_profiles(guild INTEGER, channel INTEGER, data TEXT NOT NULL, PRIMARY KEY(guild,channel));
        CREATE TABLE IF NOT EXISTS receipts(id INTEGER PRIMARY KEY AUTOINCREMENT, game INTEGER, round INTEGER, user INTEGER, data TEXT NOT NULL, UNIQUE(game,round,user));
        CREATE TABLE IF NOT EXISTS receipt_requests(receipt INTEGER PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS minecraft_links(user INTEGER PRIMARY KEY, uuid TEXT NOT NULL UNIQUE, name TEXT NOT NULL, verified REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS link_challenges(token_hash TEXT PRIMARY KEY, user INTEGER UNIQUE, uuid TEXT NOT NULL, name TEXT NOT NULL, expires REAL NOT NULL, receipt INTEGER);
        CREATE TABLE IF NOT EXISTS guild_minecraft_links(
            guild INTEGER NOT NULL, user INTEGER NOT NULL, uuid TEXT NOT NULL, name TEXT NOT NULL,
            verified REAL NOT NULL, original_name TEXT NOT NULL, nickname_state TEXT NOT NULL DEFAULT 'pending',
            nickname_retry REAL NOT NULL DEFAULT 0, PRIMARY KEY(guild,user), UNIQUE(guild,uuid));
        CREATE TABLE IF NOT EXISTS guild_link_challenges(
            token_hash TEXT PRIMARY KEY, guild INTEGER NOT NULL, user INTEGER NOT NULL,
            uuid TEXT NOT NULL, name TEXT NOT NULL, expires REAL NOT NULL, receipt INTEGER,
            original_name TEXT NOT NULL, UNIQUE(guild,user));
        ''')

    def profile(self, guild, channel):
        row = self.db.execute('SELECT data FROM receipt_profiles WHERE guild=? AND channel=?', (guild, channel)).fetchone()
        return json.loads(row[0]) if row else None

    def set_profile(self, guild, channel, data):
        if not data['roles'] or len(data['roles']) > 10 or guild in data['roles']:
            raise ReceiptError('スタッフロールは @everyone 以外を1～10個指定してください。')
        if not 1 <= data['minutes'] <= 525600:
            raise ReceiptError('受け取り期限は1～525600分です。')
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO receipt_profiles VALUES (?,?,?)', (guild, channel, json.dumps(data)))

    def save(self, receipt):
        with self.db:
            self.db.execute('UPDATE receipts SET data=? WHERE id=?', (json.dumps(receipt), receipt['id']))

    def get(self, rid):
        row = self.db.execute('SELECT data FROM receipts WHERE id=?', (rid,)).fetchone()
        return self._unpack(row[0]) if row else None

    def _unpack(self, raw):
        receipt = json.loads(raw)
        if self.db.execute('SELECT 1 FROM receipt_requests WHERE receipt=?', (receipt['id'],)).fetchone():
            receipt['requested'] = True
        return receipt

    def all(self):
        return [self._unpack(r[0]) for r in self.db.execute('SELECT data FROM receipts')]

    def seed(self, game):
        """Idempotently import only new award events, never historical draws."""
        for event in game.get('receipt_events', []):
            for uid in event['winners']:
                data = dict(game=game['id'], round=event['round'], user=uid, guild=game['guild'],
                            source_channel=game['channel'], source_message=game['message'], prize=game['prize'],
                            policy=game.get('receipt_policy'), deadline=event['deadline'], awarded=event['at'],
                            status='pending', requested=False, channel=None, control=None, dm='pending')
                with self.db:
                    cur = self.db.execute('INSERT OR IGNORE INTO receipts(game,round,user,data) VALUES (?,?,?,?)',
                                          (game['id'], event['round'], uid, json.dumps(data)))
                    if cur.rowcount:
                        data['id'] = cur.lastrowid
                        self.db.execute('UPDATE receipts SET data=? WHERE id=?', (json.dumps(data), data['id']))

    def for_game(self, gid, uid):
        return [r for r in self.all() if r['game'] == gid and r['user'] == uid]

    def link(self, uid, guild):
        row = self.db.execute('SELECT uuid,name,verified,original_name,nickname_state FROM guild_minecraft_links WHERE guild=? AND user=?', (guild, uid)).fetchone()
        return dict(zip(('uuid', 'name', 'verified', 'original_name', 'nickname_state'), row)) if row else None

    def list_links(self, guild, page=1, page_size=15):
        if not guild or page < 1 or not 1 <= page_size <= 25:
            raise ReceiptError('サーバーと有効なページを指定してください。')
        total = self.db.execute('SELECT count(*) FROM guild_minecraft_links WHERE guild=?', (guild,)).fetchone()[0]
        pages = max(1, (total + page_size - 1) // page_size)
        if page > pages:
            raise ReceiptError(f'ページは1～{pages}で指定してください。')
        rows = self.db.execute('SELECT user,uuid,name,verified FROM guild_minecraft_links WHERE guild=? ORDER BY verified DESC,user ASC LIMIT ? OFFSET ?', (guild, page_size, (page-1)*page_size)).fetchall()
        return total, pages, [dict(zip(('user','uuid','name','verified'), row)) for row in rows]

    def nickname_jobs(self, now=None):
        now = time.time() if now is None else now
        return [dict(zip(('guild', 'user', 'name', 'original_name'), r)) for r in self.db.execute(
            "SELECT guild,user,name,original_name FROM guild_minecraft_links WHERE nickname_state!='done' AND nickname_retry<=? ORDER BY nickname_retry LIMIT 20", (now,))]

    def nickname_result(self, guild, uid, state, retry=0):
        with self.db:
            self.db.execute('UPDATE guild_minecraft_links SET nickname_state=?,nickname_retry=? WHERE guild=? AND user=?', (state, retry, guild, uid))

    def authorize(self, receipt, uid, now=None):
        now = time.time() if now is None else now
        if not receipt or receipt['user'] != uid:
            raise ReceiptError('この受け取りは当選者本人だけが操作できます。')
        if receipt.get('channel'):
            return
        if receipt['status'] == 'expired' or now >= receipt['deadline']:
            raise ReceiptError('受け取り期限を過ぎています。スタッフにお問い合わせください。')
        if not receipt.get('policy'):
            raise ReceiptError('この募集は自動チケット未設定です。募集元のスタッフへお問い合わせください。')

    def challenge(self, uid, uuid, name, receipt=None, now=None, *, guild, original_name):
        now = time.time() if now is None else now
        if not isinstance(guild, int) or guild <= 0:
            raise ReceiptError('認証するサーバーを指定してください。')
        if self.link(uid, guild):
            raise ReceiptError('すでに連携済みです。IDの再入力は不要です。')
        other = self.db.execute('SELECT user FROM guild_minecraft_links WHERE guild=? AND uuid=?', (guild, uuid)).fetchone()
        if other:
            raise ReceiptError('このMinecraftアカウントはすでに連携されています。')
        previous = self.db.execute('SELECT expires FROM guild_link_challenges WHERE guild=? AND user=?', (guild, uid)).fetchone()
        if previous and previous[0] > now + 570:
            raise ReceiptError('アドレスの再発行は30秒待ってください。')
        if not re.fullmatch(r'[0-9a-f]{32}', uuid) or not re.fullmatch(r'[A-Za-z0-9_]{3,16}', name):
            raise ReceiptError('Minecraftプロフィールが不正です。')
        expires = now + 600
        if receipt:
            r = self.get(receipt)
            self.authorize(r, uid, now)
            if r['guild'] != guild:
                raise ReceiptError('受け取り元とは別のサーバーです。')
            expires = min(expires, r['deadline'])
        token = secrets.token_hex(16)
        with self.db:
            self.db.execute('DELETE FROM guild_link_challenges WHERE (guild=? AND user=?) OR expires<=?', (guild, uid, now))
            self.db.execute('INSERT INTO guild_link_challenges VALUES (?,?,?,?,?,?,?,?)',
                            (hashlib.sha256(token.encode()).hexdigest(), guild, uid, uuid, name, expires, receipt, original_name))
        return token, expires

    def verify(self, token, uuid, name, now=None):
        """Called only by the private, authenticated online-mode verifier."""
        now = time.time() if now is None else now
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.db:
            row = self.db.execute('SELECT user,uuid,name,expires,receipt,guild,original_name FROM guild_link_challenges WHERE token_hash=?', (digest,)).fetchone()
            if not row or row[3] <= now:
                raise ReceiptError('連携アドレスの期限切れ、または使用済みです。')
            uid, expected, _, _, rid, guild, original_name = row
            if uuid != expected:
                raise ReceiptError('入力したIDとは別のMinecraftアカウントです。')
            if not re.fullmatch(r'[A-Za-z0-9_]{3,16}', name):
                raise ReceiptError('プロフィール名が不正です。')
            try:
                self.db.execute('INSERT INTO guild_minecraft_links(guild,user,uuid,name,verified,original_name) VALUES (?,?,?,?,?,?)', (guild, uid, uuid, name, now, original_name))
            except sqlite3.IntegrityError as exc:
                raise ReceiptError('DiscordまたはMinecraftアカウントはすでに連携済みです。') from exc
            self.db.execute('DELETE FROM guild_link_challenges WHERE token_hash=?', (digest,))
            if rid:
                r = self.get(rid)
                if r and r['guild'] == guild and r['user'] == uid and r['status'] == 'pending' and r['deadline'] > now:
                    self.db.execute('INSERT OR IGNORE INTO receipt_requests VALUES (?)', (rid,))
        return uid, rid
