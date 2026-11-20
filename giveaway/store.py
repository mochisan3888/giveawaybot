import json
import sqlite3


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS notification_settings(guild INTEGER PRIMARY KEY, role INTEGER);
            CREATE TABLE IF NOT EXISTS settings(guild INTEGER PRIMARY KEY, rules TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS games(id INTEGER PRIMARY KEY AUTOINCREMENT, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS entries(game INTEGER, user INTEGER, PRIMARY KEY(game,user));
        ''')

    def rules(self, guild):
        row = self.db.execute('SELECT rules FROM settings WHERE guild=?', (guild,)).fetchone()
        return json.loads(row[0]) if row else {}

    def set_rules(self, guild, rules):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (guild, json.dumps(rules)))

    def create(self, game):
        with self.db:
            cur = self.db.execute('INSERT INTO games(data) VALUES (?)', (json.dumps(game),))
            game['id'] = cur.lastrowid
            self.save(game)
        return game

    def save(self, game):
        with self.db:
            self.db.execute('UPDATE games SET data=? WHERE id=?', (json.dumps(game), game['id']))

    def get(self, gid):
        row = self.db.execute('SELECT data FROM games WHERE id=?', (gid,)).fetchone()
        return json.loads(row[0]) if row else None

    def games(self):
        return [json.loads(r[0]) for r in self.db.execute('SELECT data FROM games')]

    def enter(self, gid, uid):
        with self.db:
            cur = self.db.execute('INSERT OR IGNORE INTO entries VALUES (?,?)', (gid, uid))
        return bool(cur.rowcount)

    def leave(self, gid, uid):
        with self.db:
            self.db.execute('DELETE FROM entries WHERE game=? AND user=?', (gid, uid))

    def entries(self, gid):
        return [r[0] for r in self.db.execute('SELECT user FROM entries WHERE game=?', (gid,))]

    def notification_role(self, guild):
        row = self.db.execute('SELECT role FROM notification_settings WHERE guild=?', (guild,)).fetchone()
        return row[0] if row else None

    def set_notification_role(self, guild, role):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO notification_settings VALUES (?,?)', (guild, role))
