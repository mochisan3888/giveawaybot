"""Opt-in, bounded message history. No backfill, attachment downloads or public logs."""
import asyncio
import json
import logging
import time
import uuid

import discord
from discord import app_commands

from .access_policy import restricted, giveaway_runtime
from .redemptions import ReceiptError

TTL = 7 * 86400
GUILD_LIMIT = 10_000
TOTAL_LIMIT = 50_000
BYTE_LIMIT = 16 * 1024 * 1024
QUEUE_LIMIT = 500
NONE = discord.AllowedMentions.none()
log = logging.getLogger(__name__)


def clipped(value, limit):
    value = str(value or '')
    return value if len(value) <= limit else value[:limit - 12] + '\n[省略・上限超過]'


def attachments(items):
    # Never retain signed URLs or download file contents.
    result = [dict(id=str(a.get('id', '')), filename=clipped(a.get('filename'), 256),
                   size=a.get('size', 0), content_type=clipped(a.get('content_type'), 128))
              for a in items[:10]]
    return result


class MessageLogStore:
    def __init__(self, db):
        self.db = db
        db.executescript('''
        CREATE TABLE IF NOT EXISTS message_log_config
          (guild INTEGER PRIMARY KEY, channel INTEGER NOT NULL, generation TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS message_log_cache
          (id INTEGER PRIMARY KEY, guild INTEGER NOT NULL, seen REAL NOT NULL, data TEXT NOT NULL, bytes INTEGER NOT NULL);
        CREATE INDEX IF NOT EXISTS message_log_age ON message_log_cache(seen, id);
        CREATE INDEX IF NOT EXISTS message_log_guild_age ON message_log_cache(guild, seen, id);
        ''')
        self.cleanup()

    def config(self, gid):
        row = self.db.execute('SELECT channel,generation FROM message_log_config WHERE guild=?', (gid,)).fetchone()
        return (row[0], row[1]) if row else None

    def setup(self, gid, cid):
        with self.db:
            self.db.execute('DELETE FROM message_log_cache WHERE guild=?', (gid,))
            self.db.execute('INSERT OR REPLACE INTO message_log_config VALUES(?,?,?)', (gid, cid, uuid.uuid4().hex))

    def disable(self, gid):
        with self.db:
            self.db.execute('DELETE FROM message_log_cache WHERE guild=?', (gid,))
            self.db.execute('DELETE FROM message_log_config WHERE guild=?', (gid,))

    def cleanup(self):
        with self.db:
            self.db.execute('DELETE FROM message_log_cache WHERE seen<=?', (time.time() - TTL,))

    def get(self, gid, mid):
        row = self.db.execute('SELECT data FROM message_log_cache WHERE guild=? AND id=? AND seen>?',
                              (gid, mid, time.time() - TTL)).fetchone()
        return json.loads(row[0]) if row else None

    def save(self, record):
        self.cleanup()
        with self.db:
            encoded = json.dumps(record, ensure_ascii=False)
            self.db.execute('INSERT OR REPLACE INTO message_log_cache VALUES(?,?,?,?,?)',
                            (record['id'], record['guild'], record['seen'], encoded, len(encoded.encode('utf-8'))))
            self.db.execute('DELETE FROM message_log_cache WHERE id IN (SELECT id FROM message_log_cache WHERE guild=? ORDER BY seen DESC,id DESC LIMIT -1 OFFSET ?)', (record['guild'], GUILD_LIMIT))
            self.db.execute('DELETE FROM message_log_cache WHERE id IN (SELECT id FROM message_log_cache ORDER BY seen DESC,id DESC LIMIT -1 OFFSET ?)', (TOTAL_LIMIT,))
            total = self.db.execute('SELECT COALESCE(SUM(bytes),0) FROM message_log_cache').fetchone()[0]
            if total > BYTE_LIMIT:
                # Evict oldest payloads until the byte budget is met; freed SQLite pages are reused.
                victims = []
                for mid, size in self.db.execute('SELECT id,bytes FROM message_log_cache ORDER BY seen,id'):
                    victims.append((mid,))
                    total -= size
                    if total <= BYTE_LIMIT:
                        break
                self.db.executemany('DELETE FROM message_log_cache WHERE id=?', victims)

    def delete(self, gid, mid):
        with self.db:
            self.db.execute('DELETE FROM message_log_cache WHERE guild=? AND id=?', (gid, mid))


async def private_destination(bot, gid, cid):
    """Use fresh REST role/member/overwrite data, including uncached member overwrites."""
    guild = await bot.fetch_guild(gid)
    data = await bot.http.get_channel(cid)
    if int(data.get('guild_id', 0)) != gid or data.get('type') != 0:
        raise ReceiptError('送信先は同じサーバーの運営専用テキストチャンネルにしてください。')
    channel = discord.TextChannel(state=bot._connection, guild=guild, data=data)
    row = bot.store.db.execute('SELECT data FROM moderation_config WHERE guild=?', (gid,)).fetchone()
    configured = set(json.loads(row[0]).get('roles', [])) if row else set()
    staff_roles = {r.id for r in guild.roles if r.id != gid and
                   (r.id in configured or r.permissions.administrator or r.permissions.manage_guild)}
    bot_roles = {r.id for r in guild.roles if r.tags and r.tags.bot_id == bot.user.id}
    everyone = guild.default_role
    if everyone is None or everyone.permissions.administrator or everyone.permissions.manage_guild:
        raise ReceiptError('@everyoneに管理権限があるため運営専用と確認できません。')
    overwrites = data.get('permission_overwrites')
    if not isinstance(overwrites, list):
        raise ReceiptError('チャンネル権限を確認できません。')
    view = discord.Permissions(view_channel=True).value
    everyone_denied = False
    for overwrite in overwrites:
        oid = int(overwrite['id'])
        allow, deny = int(overwrite['allow']), int(overwrite['deny'])
        kind = overwrite['type']
        if oid == gid and kind == 0:
            everyone_denied = bool(deny & view) and not bool(allow & view)
        elif allow & view:
            if kind == 0:
                if oid not in staff_roles | bot_roles:
                    raise ReceiptError('一般ロールへの閲覧許可があります。運営専用に変更してください。')
            elif kind == 1:
                if oid not in (bot.user.id, guild.owner_id):
                    member = await guild.fetch_member(oid)
                    if not staff_roles.intersection(r.id for r in member.roles):
                        raise ReceiptError('一般メンバーへの個別閲覧許可があります。')
            else:
                raise ReceiptError('不明なチャンネル権限のため送信できません。')
    if not everyone_denied:
        raise ReceiptError('@everyoneの「チャンネルを見る」を明示的に拒否してください。')
    me = await guild.fetch_member(bot.user.id)
    permissions = channel.permissions_for(me)
    if not (permissions.view_channel and permissions.send_messages and permissions.embed_links):
        raise ReceiptError('BOTに閲覧・メッセージ送信・埋め込みリンク権限が必要です。')
    return channel


class MessageLog:
    def __init__(self, bot):
        self.bot = bot
        self.db = MessageLogStore(bot.store.db)
        self.queue = asyncio.Queue(maxsize=QUEUE_LIMIT)
        self.worker = None
        self.cleaner = None
        self.state = {}
        self.send_lock = asyncio.Lock()

    def enabled(self, gid, cid):
        if not gid or restricted(gid) or giveaway_runtime() or not self.bot.intents.message_content:
            return None
        cfg = self.db.config(gid)
        return cfg if cfg and cfg[0] != cid else None

    def enqueue(self, cfg, record, before, kind):
        try:
            self.queue.put_nowait((cfg, record, before, kind))
        except asyncio.QueueFull:
            self.state[record['guild']] = '待機上限超過のため一部通知を省略'

    async def on_message(self, message):
        gid = message.guild.id if message.guild else None
        if message.author.bot or message.webhook_id or not self.enabled(gid, message.channel.id):
            return
        self.db.save(dict(id=message.id, guild=gid, channel=message.channel.id,
                          author=message.author.id, name=clipped(str(message.author), 100),
                          body=clipped(message.content, 4000), seen=time.time(),
                          attachments=attachments([dict(id=a.id, filename=a.filename, size=a.size,
                                                        content_type=a.content_type) for a in message.attachments])))

    async def on_raw_message_edit(self, payload):
        cfg = self.enabled(payload.guild_id, payload.channel_id)
        data = payload.data
        if not cfg or not ({'content', 'attachments'} & data.keys()):
            return  # Embed-only updates do not erase content or emit duplicates.
        old = self.db.get(payload.guild_id, payload.message_id)
        author = data.get('author') or {}
        if author.get('bot') or data.get('webhook_id'):
            return
        if old:
            record = dict(old)
        else:
            # Never fetch history. Author must be supplied to exclude bots safely.
            if not author.get('id') or 'bot' not in author and not author.get('username'):
                return
            record = dict(id=payload.message_id, guild=payload.guild_id, channel=payload.channel_id,
                          author=int(author['id']), name=clipped(author.get('username', '不明'), 100),
                          body='本文不明（未受信）', attachments=[], seen=time.time())
        if 'content' in data:
            record['body'] = clipped(data['content'], 4000)
        if 'attachments' in data:
            record['attachments'] = attachments(data['attachments'])
        if old and (old['body'], old['attachments']) == (record['body'], record['attachments']):
            return
        self.db.save(record)
        self.enqueue(cfg, record, old, '編集')

    async def on_raw_message_delete(self, payload):
        cfg = self.enabled(payload.guild_id, payload.channel_id)
        if not cfg:
            return
        old = self.db.get(payload.guild_id, payload.message_id)
        self.db.delete(payload.guild_id, payload.message_id)
        if old:
            self.enqueue(cfg, old, None, '削除')
        else:
            self.state[payload.guild_id] = '未受信・期限切れ・上限超過の削除本文は復元できません'

    async def on_raw_bulk_message_delete(self, payload):
        for mid in sorted(payload.message_ids):
            from types import SimpleNamespace
            await self.on_raw_message_delete(SimpleNamespace(guild_id=payload.guild_id,
                                                             channel_id=payload.channel_id, message_id=mid))

    async def start(self):
        if self.worker is None:
            self.worker = asyncio.create_task(self.deliver())
            self.cleaner = asyncio.create_task(self.clean())

    async def stop(self):
        jobs = [task for task in (self.worker, self.cleaner) if task]
        for task in jobs:
            task.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
        self.worker = self.cleaner = None
        self.clear_pending()

    def clear_pending(self, gid=None):
        kept = []
        while not self.queue.empty():
            item = self.queue.get_nowait()
            self.queue.task_done()
            if gid is not None and item[1]['guild'] != gid:
                kept.append(item)
        for item in kept:
            self.queue.put_nowait(item)

    async def clean(self):
        while True:
            self.db.cleanup()
            await asyncio.sleep(60)

    async def deliver_one(self, item):
        cfg, record, before, kind = item
        gid = record['guild']
        async with self.send_lock:
            if self.enabled(gid, record['channel']) != cfg or record['seen'] <= time.time() - TTL:
                return
            # Fresh validation immediately before each send; failures never reroute.
            channel = await private_destination(self.bot, gid, cfg[0])
            if self.enabled(gid, record['channel']) != cfg or record['seen'] <= time.time() - TTL:
                return
            embed = discord.Embed(title='メッセージ' + kind, color=0x5865F2)
            embed.description = (f"投稿者：{discord.utils.escape_markdown(record['name'])} (ID: {record['author']})\n"
                                 f"元チャンネル：<#{record['channel']}>\n"
                                 f"[元メッセージ](https://discord.com/channels/{gid}/{record['channel']}/{record['id']})")
            if kind == '編集':
                embed.add_field(name='編集前', value=clipped(before['body'] if before else '編集前不明（未受信・期限切れ・上限超過）', 1000) or '（空本文）', inline=False)
            embed.add_field(name='編集後' if kind == '編集' else '削除された本文', value=clipped(record['body'], 1000) or '（空本文）', inline=False)
            for label, source in [('変更前の添付', before), ('添付情報', record)]:
                if source and source['attachments']:
                    body = '\n'.join(f"{a['filename']} / {a['size']} bytes / {a['content_type']}" for a in source['attachments'])
                    embed.add_field(name=label, value=clipped(body, 1000), inline=False)
            embed.set_footer(text='本文は最大1,000文字表示。添付は名前・サイズ・種類のみ。削除済みリンクは開けません。')
            await channel.send(embed=embed, allowed_mentions=NONE)
            self.state[gid] = '送信正常'

    async def deliver(self):
        while True:
            item = await self.queue.get()
            try:
                await self.deliver_one(item)
            except Exception as error:
                self.state[item[1]['guild']] = '送信停止：権限・接続を確認してください（本文の再送なし）'
                log.warning('Message log suppressed: %s', type(error).__name__)
            finally:
                self.queue.task_done()

    async def authorize(self, interaction):
        if not interaction.guild_id or restricted(interaction.guild_id) or giveaway_runtime():
            raise ReceiptError('このサーバーでは利用できません。')
        guild = await self.bot.fetch_guild(interaction.guild_id)
        member = await guild.fetch_member(interaction.user.id)
        if not member.guild_permissions.manage_guild:
            raise ReceiptError('サーバー管理権限が必要です。')


def register_message_log(bot):
    service = bot.message_log
    group = app_commands.Group(name='messagelog', description='運営専用の編集・削除ログ', guild_only=True,
                               default_permissions=discord.Permissions(manage_guild=True))

    @group.command(name='setup', description='運営専用チャンネルへの記録を有効化（履歴取得なし）')
    async def setup(i: discord.Interaction, channel: discord.TextChannel):
        await i.response.defer(ephemeral=True)
        await service.authorize(i)
        if not bot.intents.message_content or not bot.intents.guild_messages:
            raise ReceiptError('BOT側のMessage Content / Guild Messages Intentが無効です。運営に連絡してください。')
        async with service.send_lock:
            await private_destination(bot, i.guild_id, channel.id)
            service.clear_pending(i.guild_id)
            service.db.setup(i.guild_id, channel.id)
            service.state.pop(i.guild_id, None)
        await i.followup.send('有効化しました。今後受信する一般ユーザーの投稿・編集を最大7日、1サーバー10,000件・全体50,000件・本文等合計16MiBまで保存します。DM・BOT・ログ送信先は除外。過去の履歴は取得しません。再設定時は旧キャッシュを消去します。利用者への告知をお願いします。', ephemeral=True, allowed_mentions=NONE)

    @group.command(name='disable', description='記録を無効化し保存中の本文キャッシュを削除')
    async def disable(i: discord.Interaction):
        await i.response.defer(ephemeral=True)
        await service.authorize(i)
        async with service.send_lock:
            service.db.disable(i.guild_id)
            service.clear_pending(i.guild_id)
            service.state.pop(i.guild_id, None)
        await i.followup.send('無効化し、保存中のキャッシュを削除しました。送信済みのDiscordログは残ります。', ephemeral=True, allowed_mentions=NONE)

    @group.command(name='status', description='設定・保存件数・送信先の安全性を確認')
    async def status(i: discord.Interaction):
        await i.response.defer(ephemeral=True)
        await service.authorize(i)
        cfg = service.db.config(i.guild_id)
        state = '無効'
        if cfg:
            try:
                await private_destination(bot, i.guild_id, cfg[0])
                state = f'有効 / <#{cfg[0]}> / 現在の権限確認OK'
            except Exception:
                state = '有効だが送信停止：運営専用の権限・接続を確認してください'
        count = service.db.db.execute('SELECT COUNT(*) FROM message_log_cache WHERE guild=? AND seen>?', (i.guild_id, time.time() - TTL)).fetchone()[0]
        await i.followup.send(f'{state}\n保存：{count}件 / 最長7日 / 1サーバー10,000件・全体50,000件・全体16MiB\n直近：{service.state.get(i.guild_id, "通知記録なし")}\nMessage Content Intent：{bot.intents.message_content}', ephemeral=True, allowed_mentions=NONE)

    bot.tree.add_command(group)
