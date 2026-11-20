import json
import time
from .redemptions import ReceiptError


class DonationStore:
    def __init__(self, db):
        self.db = db
        db.executescript('''
            CREATE TABLE IF NOT EXISTS donation_settings(guild INTEGER PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS donation_panels(message INTEGER PRIMARY KEY, guild INTEGER NOT NULL, channel INTEGER NOT NULL, revision INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS donation_tickets(id INTEGER PRIMARY KEY AUTOINCREMENT, guild INTEGER NOT NULL, user INTEGER NOT NULL, active INTEGER NOT NULL, data TEXT NOT NULL);
            CREATE UNIQUE INDEX IF NOT EXISTS donation_active_user ON donation_tickets(guild,user) WHERE active=1;
        ''')

    def config(self, guild):
        row = self.db.execute('SELECT data FROM donation_settings WHERE guild=?', (guild,)).fetchone()
        return json.loads(row[0]) if row else {'revision': 0}

    def set_config(self, guild, **updates):
        data = self.config(guild)
        data.update(updates)
        data['revision'] += 1
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO donation_settings VALUES (?,?)', (guild, json.dumps(data)))
        return data

    def panels(self, guild=None):
        rows = self.db.execute('SELECT message,guild,channel,revision FROM donation_panels' + (' WHERE guild=?' if guild is not None else ''), (guild,) if guild is not None else ())
        return [dict(zip(('message', 'guild', 'channel', 'revision'), row)) for row in rows]

    def save_panel(self, guild, channel, message, revision):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO donation_panels VALUES (?,?,?,?)', (message, guild, channel, revision))

    def remove_panel(self, message):
        with self.db:
            self.db.execute('DELETE FROM donation_panels WHERE message=?', (message,))

    def get(self, rid):
        row = self.db.execute('SELECT data FROM donation_tickets WHERE id=?', (rid,)).fetchone()
        return json.loads(row[0]) if row else None

    def active(self, guild, user):
        row = self.db.execute('SELECT data FROM donation_tickets WHERE guild=? AND user=? AND active=1', (guild, user)).fetchone()
        return json.loads(row[0]) if row else None

    def pending(self):
        return [json.loads(r[0]) for r in self.db.execute("SELECT data FROM donation_tickets WHERE active=1 OR json_extract(data,'$.dirty')=1")]

    def by_channel(self, guild, channel):
        row = self.db.execute("SELECT data FROM donation_tickets WHERE guild=? AND json_extract(data,'$.channel')=?", (guild, channel)).fetchone()
        return json.loads(row[0]) if row else None

    def create(self, guild, user, content, note):
        existing = self.active(guild, user)
        if existing:
            return existing
        content, note = content.strip(), note.strip()
        if not content or len(content) > 1000 or len(note) > 1000:
            raise ReceiptError('寄付内容を1～1000文字、補足を1000文字以内で入力してください。')
        config = self.config(guild)
        if not config.get('policy'):
            raise ReceiptError('寄付チケットが未設定です。管理者に連絡してください。')
        ticket = dict(guild=guild, user=user, content=content, note=note, policy=config['policy'],
                      created=time.time(), status='pending', channel=None, control=None, dirty=True)
        with self.db:
            cursor = self.db.execute('INSERT INTO donation_tickets(guild,user,active,data) VALUES (?,?,1,?)', (guild, user, '{}'))
            ticket['id'] = cursor.lastrowid
            self.save(ticket)
        return ticket

    def save(self, ticket):
        with self.db:
            self.db.execute('UPDATE donation_tickets SET active=?,data=? WHERE id=?', (ticket['status'] != 'closed', json.dumps(ticket), ticket['id']))
