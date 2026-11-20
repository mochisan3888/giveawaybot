"""Reconcile only bot-owned warning roles with the effective guild warning count."""
from .access_policy import restricted, giveaway_runtime
import asyncio
import logging
import time
import discord

log = logging.getLogger(__name__)

class WarningRoles:
    def __init__(self, bot, store):
        self.bot, self.store = bot, store
        self.lock = asyncio.Lock()
        self.next_scan = 0
        self.retry = {}
        self.pending = {}
        store.db.execute('CREATE TABLE IF NOT EXISTS warning_roles(guild INTEGER,count INTEGER,role INTEGER,PRIMARY KEY(guild,count))')
        store.db.commit()

    def install_events(self):
        # discord.py drops member-update callbacks for uncached members. Dispatch
        # our own event from the parser without enabling a full member cache.
        for name in ('GUILD_MEMBER_UPDATE', 'GUILD_MEMBER_ADD'):
            original = self.bot._connection.parsers[name]
            def parse(data, original=original):
                original(data)
                self.bot.dispatch('warning_member_update', data)
            self.bot._connection.parsers[name] = parse

    async def member_changed(self, data):
        gid, uid = int(data['guild_id']), int(data['user']['id'])
        if restricted(gid) or giveaway_runtime():
            return
        guild = self.bot.get_guild(gid)
        if guild is None:
            return
        count = self.store.warnings(gid, uid)
        owned = dict(self.store.db.execute('SELECT count,role FROM warning_roles WHERE guild=?', (gid,)))
        actual = {int(r) for r in data.get('roles', [])} & set(owned.values())
        wanted = {owned[count]} if count and owned.get(count) else set()
        if actual == wanted and (not count or wanted):
            return
        key = (gid, uid)
        # One in-flight repair per user. Own role updates are rechecked above,
        # and retries retain the existing backoff when Discord denies access.
        if key in self.pending or self.retry.get(key, 0) > time.monotonic():
            return
        self.pending[key] = True
        try:
            await self.update(guild, uid)
        finally:
            self.pending.pop(key, None)

    async def sync(self, guild, uid):
        async with self.lock:
            try:
                member = await guild.fetch_member(uid)
            except discord.NotFound:
                return '退出済みのためロール更新対象外です。'
            count = self.store.warnings(guild.id, uid)
            roles = {r.id:r for r in await guild.fetch_roles()}
            owned = dict(self.store.db.execute('SELECT count,role FROM warning_roles WHERE guild=?',(guild.id,)))
            wanted = roles.get(owned.get(count)) if count else None
            if count and not wanted:
                name = f'警告{count}回'
                # A pending creation must be resolved manually rather than creating duplicates.
                if count in owned and owned[count] is None:
                    raise RuntimeError('role creation outcome needs review')
                with self.store.db:
                    self.store.db.execute('INSERT OR REPLACE INTO warning_roles VALUES(?,?,NULL)',(guild.id,count))
                try:
                    wanted = await guild.create_role(name=name,permissions=discord.Permissions.none(),colour=discord.Colour.orange(),hoist=False,mentionable=False,reason='有効な警告回数の自動表示')
                except discord.HTTPException as error:
                    if error.status in (400,403,404,429):
                        with self.store.db:self.store.db.execute('DELETE FROM warning_roles WHERE guild=? AND count=?',(guild.id,count))
                    raise
                with self.store.db:
                    self.store.db.execute('UPDATE warning_roles SET role=? WHERE guild=? AND count=?',(wanted.id,guild.id,count))
            if wanted and wanted not in member.roles:
                await member.add_roles(wanted,reason=f'有効な警告 {count}件',atomic=True)
            stale = [r for r in member.roles if r.id in owned.values() and (not wanted or r.id != wanted.id)]
            if stale:
                await member.remove_roles(*stale,reason=f'警告回数を{count}件に更新',atomic=True)
            return f'警告ロール：{wanted.name if wanted else "なし"}'

    async def update(self, guild, uid):
        if restricted(guild.id):return "このサーバーでは警告ロールを停止しています。"
        try:
            result = await self.sync(guild, uid)
            self.retry.pop((guild.id,uid),None)
            return result
        except Exception as error:
            self.retry[guild.id,uid] = time.monotonic()+60
            log.warning('Warning role pending guild=%s user=%s error=%s',guild.id,uid,type(error).__name__)
            return '警告は記録済みですが、ロール更新は保留中です。BOTの「ロールの管理」権限とロール順位を確認してください。'

    async def reconcile(self):
        now=time.monotonic()
        targets={key for key,when in self.retry.items() if when<=now}
        if now>=self.next_scan:
            self.next_scan=now+300
            targets.update((r[0],r[1]) for r in self.store.db.execute('SELECT DISTINCT guild,user FROM moderation_cases'))
        for gid,uid in sorted(targets):
            guild=self.bot.get_guild(gid)
            if guild:await self.update(guild,uid)
