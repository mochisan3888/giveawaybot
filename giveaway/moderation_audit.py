"""Import native Discord punishments into the existing durable log outbox."""
import datetime
import json
import logging
import discord
from discord.ext import tasks
from .access_policy import restricted, giveaway_runtime

log = logging.getLogger(__name__)


def punishment(entry):
    action = {discord.AuditLogAction.kick: 'kick', discord.AuditLogAction.ban: 'ban',
              discord.AuditLogAction.unban: 'unban'}.get(entry.action)
    text = entry.reason or '理由の記載なし'
    if entry.action == discord.AuditLogAction.member_update:
        if not hasattr(entry.after, 'timed_out_until'):
            return None
        until = entry.after.timed_out_until
        action = 'timeout' if until else 'untimeout'
        if until:
            text += f'\n解除予定：<t:{int(until.timestamp())}:F>'
    if not action or not getattr(entry.target, 'id', None):
        return None
    return dict(kind='Discord監査ログ｜BOT外の処分', actor=entry.user_id,
                user=entry.target.id, action=action, at=entry.created_at.timestamp(),
                text=text[:1500], audit_id=str(entry.id))


class ModerationAudit:
    def __init__(self, service):
        self.service = service
        self.db = service.db.db
        self.since = discord.utils.time_snowflake(discord.utils.utcnow())
        self.db.execute('CREATE TABLE IF NOT EXISTS moderation_audit_cursor(guild INTEGER PRIMARY KEY, cursor INTEGER NOT NULL)')
        self.db.commit()

    def record(self, guild, entry, channel):
        data = None if entry.user_id == self.service.bot.user.id else punishment(entry)
        with self.db:
            row = self.db.execute('SELECT cursor FROM moderation_audit_cursor WHERE guild=?', (guild,)).fetchone()
            if row and entry.id <= row[0]:
                return
            if data:
                self.db.execute('INSERT INTO moderation_events(guild,case_id,channel,data,state) VALUES(?,?,?,?,?)',
                                (guild, 0, channel, json.dumps(data), 'pending'))
            self.db.execute('INSERT OR REPLACE INTO moderation_audit_cursor VALUES(?,?)', (guild, entry.id))

    async def poll(self):
        if giveaway_runtime():
            return
        for gid, raw in self.db.execute('SELECT guild,data FROM moderation_config').fetchall():
            channel = json.loads(raw).get('log')
            guild = self.service.bot.get_guild(gid)
            if restricted(gid) or not channel or not guild:
                continue
            with self.db:
                self.db.execute('INSERT OR IGNORE INTO moderation_audit_cursor VALUES(?,?)', (gid, self.since))
            cursor = self.db.execute('SELECT cursor FROM moderation_audit_cursor WHERE guild=?', (gid,)).fetchone()[0]
            try:
                async for entry in guild.audit_logs(limit=100, after=discord.Object(id=cursor), oldest_first=True):
                    self.record(gid, entry, channel)
            except discord.HTTPException as error:
                log.warning('External moderation audit guild=%s: %s (View Audit Log permission required)', gid, type(error).__name__)

    @tasks.loop(seconds=30)
    async def worker(self):
        try:
            await self.poll()
        except Exception:
            log.exception('External moderation audit poll failed')

    @worker.before_loop
    async def before_worker(self):
        await self.service.bot.wait_until_ready()
