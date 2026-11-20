"""Durable ticket activity outbox; ticket state/history is the source of truth."""
from .access_policy import restricted, giveaway_runtime
import hashlib
import json
import logging
import time
import discord
from discord import app_commands
from discord.ext import tasks
from discord.http import Route
from .redemptions import ReceiptError

log = logging.getLogger('giveaway.ticketlog')
TABLES = {'receipts':'Giveaway受け取り', 'skelly_tickets':'Skelly', 'donation_tickets':'寄付', 'vip_tickets':'VIP申請', 'staff_rooms':'スタッフ相談'}
ACTIONS = {'open':'チケット作成', 'start':'対応開始', 'done':'対応完了', 'close':'クローズ','add':'メンバー追加','remove':'メンバー退出','sync':'権限再反映','add_request':'メンバー追加要求','remove_request':'メンバー退出要求','close_request':'クローズ要求','sync_request':'権限再反映要求'}


class TicketLog:
    def __init__(self, bot):
        self.bot, self.db = bot, bot.store.db
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS ticketlog_settings(guild INTEGER PRIMARY KEY, channel INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS ticketlog_events(key TEXT PRIMARY KEY, guild INTEGER NOT NULL, channel INTEGER NOT NULL, data TEXT NOT NULL, state TEXT NOT NULL, sent_at REAL NOT NULL DEFAULT 0);
        ''')

    def config(self, guild):
        row = self.db.execute('SELECT channel FROM ticketlog_settings WHERE guild=?',(guild,)).fetchone()
        return row[0] if row else None

    def events(self, guild):
        tables = {r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table, label in TABLES.items():
            if giveaway_runtime() and table != "receipts":continue
            if table not in tables:continue
            for row in self.db.execute(f'SELECT data FROM {table}'):
                r = json.loads(row[0])
                if r.get('guild') != guild or not r.get('channel'):continue
                events = [dict(action='open',by=r['user'],at=r.get('opened',r.get('created',time.time())))] + r.get('history',[])
                for idx,e in enumerate(events):
                    yield f'{table}:{r["id"]}:{idx}', dict(kind=label,ticket=r['id'],owner=r['user'],ticket_channel=r['channel'],**e)

    def configure(self, guild, channel):
        # Baseline old history only on first setup. Pending events retain their destination.
        initial = self.config(guild) is None
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO ticketlog_settings VALUES (?,?)',(guild,channel))
            if initial:
                for key, data in self.events(guild):
                    self.db.execute('INSERT OR IGNORE INTO ticketlog_events(key,guild,channel,data,state) VALUES (?,?,?,?,?)',(key,guild,channel,json.dumps(data),'baseline'))

    def collect(self):
        with self.db:
            for guild,channel in self.db.execute('SELECT guild,channel FROM ticketlog_settings').fetchall():
                for key,data in self.events(guild):
                    self.db.execute('INSERT OR IGNORE INTO ticketlog_events(key,guild,channel,data,state) VALUES (?,?,?,?,?)',(key,guild,channel,json.dumps(data),'pending'))

    async def flush(self):
        self.collect()
        rows = self.db.execute("SELECT key,guild,channel,data,sent_at FROM ticketlog_events WHERE state='pending' AND guild != 900000000000000001 LIMIT 20").fetchall()
        for key,guild,channel,data,sent_at in rows:
            if restricted(guild):continue
            try:
                r=json.loads(data)
                if giveaway_runtime() and r.get("kind") != "Giveaway受け取り":continue
                ch=self.bot.get_channel(channel) or await self.bot.fetch_channel(channel)
                if ch.guild.id != guild:raise ReceiptError('ログ先のサーバーが一致しません。')
                nonce=hashlib.sha256(('ticketlog:'+key).encode()).hexdigest()[:24]
                if sent_at and time.time()-sent_at>120:
                    # Do not blindly retry an uncertain message after Discord's nonce window.
                    with self.db:self.db.execute("UPDATE ticketlog_events SET state='uncertain' WHERE key=?",(key,))
                    log.error('Ticket log delivery uncertain: %s',key)
                    continue
                e=discord.Embed(title=f"📋 {ACTIONS.get(r['action'],r['action'])}｜{r['kind']}",color=0xD4AF37)
                e.add_field(name='チケット',value=f"<#{r['ticket_channel']}> · #{r['ticket']}")
                e.add_field(name='申請者・当選者',value=f"<@{r['owner']}>")
                e.add_field(name='操作した人',value=f"<@{r['by']}>")
                if r.get('target'):e.add_field(name='変更対象メンバー',value=f"<@{r['target']}>")
                e.add_field(name='日時',value=f"<t:{int(r['at'])}:F>")
                e.set_footer(text='イベント '+key)
                with self.db:self.db.execute('UPDATE ticketlog_events SET sent_at=? WHERE key=?',(sent_at or time.time(),key))
                await self.bot.http.request(Route('POST','/channels/{channel_id}/messages',channel_id=channel),json={'embeds':[e.to_dict()],'allowed_mentions':{'parse':[]},'nonce':nonce,'enforce_nonce':True})
                with self.db:self.db.execute("UPDATE ticketlog_events SET state='sent' WHERE key=?",(key,))
            except discord.HTTPException as error:
                if error.status in (403,404,429):
                    with self.db:self.db.execute('UPDATE ticketlog_events SET sent_at=0 WHERE key=?',(key,))
                log.warning('Ticket log %s pending: %s',key,type(error).__name__)
            except Exception as error:
                log.warning('Ticket log %s pending: %s',key,type(error).__name__)

    async def start(self):self.worker.start()
    async def stop(self):self.worker.cancel()

    @tasks.loop(seconds=10)
    async def worker(self):
        try:await self.flush()
        except Exception as error:log.warning('Ticket log worker: %s',type(error).__name__)

    @worker.before_loop
    async def before_worker(self):await self.bot.wait_until_ready()


def register_ticketlog(bot):
    group=app_commands.Group(name='ticketlog',description='全チケット共通の操作ログ',guild_only=True,default_permissions=discord.Permissions(manage_guild=True))

    @group.command(name='setup',description='チケット作成・対応開始・完了・クローズのログ先を設定')
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setup(i:discord.Interaction,channel:discord.TextChannel):
        if channel.guild.id!=i.guild_id:raise ReceiptError('同じサーバーのチャンネルを指定してください。')
        p=channel.permissions_for(i.guild.me)
        if not(p.view_channel and p.send_messages and p.embed_links):raise ReceiptError('BOTにログ先の閲覧・送信・埋め込み権限が必要です。')
        bot.ticketlog.collect()
        bot.ticketlog.configure(i.guild_id,channel.id)
        await i.response.send_message(f'ログ先：{channel.mention}。設定後のチケット操作を約10秒以内に記録します。',ephemeral=True)

    @group.command(name='status',description='ログ先と未送信の件数を確認')
    @app_commands.checks.has_permissions(manage_guild=True)
    async def status(i:discord.Interaction):
        channel=bot.ticketlog.config(i.guild_id)
        counts=dict(bot.store.db.execute("SELECT state,COUNT(*) FROM ticketlog_events WHERE guild=? AND state IN ('pending','uncertain') GROUP BY state",(i.guild_id,)).fetchall())
        await i.response.send_message((f'ログ先：<#{channel}>' if channel else '未設定')+f"\n送信待ち：{counts.get('pending',0)}件／送信結果不明：{counts.get('uncertain',0)}件",ephemeral=True)
    bot.tree.add_command(group)
