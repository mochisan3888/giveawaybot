"""Persisted Skelly quotes and tickets; prices are decimal millions of game currency."""
import json
import re
import time
from decimal import Decimal

from .redemptions import ReceiptError


def price(value):
    text = str(value).strip().lower().removesuffix('m').strip()
    if not re.fullmatch(r'[0-9]{1,10}(?:\.[0-9]{1,6})?', text):
        raise ReceiptError('価格はm単位で入力してください（例：1.25）。小数点以下6桁まで設定できます。')
    amount = Decimal(text)
    if not 0 < amount <= Decimal('1000000000'):
        raise ReceiptError('価格は0より大きく、1,000,000,000m以下にしてください。')
    return format(amount.normalize(), 'f')


def quantity(value):
    if not re.fullmatch(r'[0-9]{1,7}', str(value).strip()):
        raise ReceiptError('数量は1～1,000,000の整数で入力してください。')
    result = int(value)
    if not 1 <= result <= 1000000:
        raise ReceiptError('数量は1～1,000,000の整数で入力してください。')
    return result


def total(unit, count):
    return format((Decimal(price(unit)) * quantity(count)).normalize(), 'f')


def side_open(config, side):
    return config.get(side + '_accepting', config.get('accepting', True))


def reception_label(config):
    buy, sell = side_open(config, 'buy'), side_open(config, 'sell')
    if buy and sell: return '受付中'
    if not buy and not sell: return '受付停止中'
    return 'Buyのみ受付中' if buy else 'Sellのみ受付中'


class SkellyStore:
    def __init__(self, db):
        self.db = db
        db.executescript('''
            CREATE TABLE IF NOT EXISTS skelly_settings(guild INTEGER PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS skelly_panels(message INTEGER PRIMARY KEY, guild INTEGER NOT NULL, channel INTEGER NOT NULL, revision INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS skelly_tickets(id INTEGER PRIMARY KEY AUTOINCREMENT, guild INTEGER NOT NULL, user INTEGER NOT NULL, active INTEGER NOT NULL, data TEXT NOT NULL);
            CREATE UNIQUE INDEX IF NOT EXISTS skelly_active_user ON skelly_tickets(guild,user) WHERE active=1;
        ''')

    def config(self, guild):
        row = self.db.execute('SELECT data FROM skelly_settings WHERE guild=?', (guild,)).fetchone()
        return json.loads(row[0]) if row else {'revision': 0}

    def set_config(self, guild, **updates):
        data = self.config(guild)
        data.update(updates)
        data['revision'] += 1
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO skelly_settings VALUES (?,?)', (guild, json.dumps(data)))
        return data

    def panels(self, guild=None):
        rows = self.db.execute('SELECT message,guild,channel,revision FROM skelly_panels' + (' WHERE guild=?' if guild is not None else ''), (guild,) if guild is not None else ())
        return [dict(zip(('message', 'guild', 'channel', 'revision'), row)) for row in rows]

    def save_panel(self, guild, channel, message, revision):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO skelly_panels VALUES (?,?,?,?)', (message, guild, channel, revision))

    def remove_panel(self, message):
        with self.db:
            self.db.execute('DELETE FROM skelly_panels WHERE message=?', (message,))

    def get(self, rid):
        row = self.db.execute('SELECT data FROM skelly_tickets WHERE id=?', (rid,)).fetchone()
        return json.loads(row[0]) if row else None

    def active(self, guild, user):
        row = self.db.execute('SELECT data FROM skelly_tickets WHERE guild=? AND user=? AND active=1', (guild, user)).fetchone()
        return json.loads(row[0]) if row else None

    def pending(self):
        return [json.loads(r[0]) for r in self.db.execute("SELECT data FROM skelly_tickets WHERE active=1 OR json_extract(data,'$.dirty')=1")]

    def by_channel(self, guild, channel):
        row = self.db.execute("SELECT data FROM skelly_tickets WHERE guild=? AND json_extract(data,'$.channel')=?", (guild, channel)).fetchone()
        return json.loads(row[0]) if row else None

    def create(self, guild, user, side, count, quoted):
        existing = self.active(guild, user)
        if existing:
            return existing
        if side not in ('buy', 'sell'):
            raise ReceiptError('BuyまたはSellを選んでください。')
        config = self.config(guild)
        if not side_open(config, side):
            raise ReceiptError(f'現在、Skelly {side.title()}の受付は停止中です。再開までお待ちください。')
        unit = config.get(side)
        if unit is None or unit != quoted:
            raise ReceiptError('価格が更新されました。最新の価格を確認し、パネルのボタンからやり直してください。')
        if not config.get('policy'):
            raise ReceiptError('スタッフ・作成先が未設定です。管理者に連絡してください。')
        count = quantity(count)
        ticket = dict(guild=guild, user=user, side=side, quantity=count, unit=unit, total=total(unit, count),
                      policy=config['policy'], created=time.time(), status='pending', channel=None, control=None, dirty=True)
        with self.db:
            cursor = self.db.execute('INSERT INTO skelly_tickets(guild,user,active,data) VALUES (?,?,1,?)', (guild, user, '{}'))
            ticket['id'] = cursor.lastrowid
            self.save(ticket)
        return ticket

    def save(self, ticket):
        with self.db:
            self.db.execute('UPDATE skelly_tickets SET active=?,data=? WHERE id=?', (ticket['status'] != 'closed', json.dumps(ticket), ticket['id']))
