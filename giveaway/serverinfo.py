"""Slow-changing, read-only voice channels showing guild counts and Skelly prices."""
from .access_policy import restricted
import asyncio
import json
import logging
import time

import discord
from discord import app_commands
from discord.ext import tasks

from .redemptions import ReceiptError
from .skelly_store import side_open, reception_label

log = logging.getLogger('giveaway.serverinfo')
INTERVAL = 600


def channel_names(count, prices):
    status = ('🟢' if side_open(prices, 'buy') and side_open(prices, 'sell') else '🔴' if not side_open(prices, 'buy') and not side_open(prices, 'sell') else '🟡') + '｜Skelly：' + reception_label(prices)
    return {'members': f'👥｜メンバー：約{count:,}人' if count is not None else '👥｜メンバー：確認中',
            'buy': '🟢｜Skelly Buy：' + ('STOP' if not side_open(prices, 'buy') else (prices['buy'] + 'm / 個' if prices.get('buy') else '未設定')),
            'sell': '🔵｜Skelly Sell：' + ('STOP' if not side_open(prices, 'sell') else (prices['sell'] + 'm / 個' if prices.get('sell') else '未設定')),
            'status': status}


def info_overwrites(guild):
    return {guild.default_role: discord.PermissionOverwrite(view_channel=True, connect=False, send_messages=False),
            guild.me: discord.PermissionOverwrite(view_channel=True, manage_channels=True, connect=False)}


class ServerInfo:
    def __init__(self, bot):
        self.bot, self.db = bot, bot.store.db
        self.lock = asyncio.Lock()
        self.db.execute('CREATE TABLE IF NOT EXISTS server_info(guild INTEGER PRIMARY KEY, data TEXT NOT NULL)')
        self.db.commit()

    def get(self, guild):
        row = self.db.execute('SELECT data FROM server_info WHERE guild=?', (guild,)).fetchone()
        return json.loads(row[0]) if row else None

    def save(self, guild, config):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO server_info VALUES (?,?)', (guild, json.dumps(config)))

    async def names(self, guild):
        # REST counts work without privileged member intent; never use an incomplete cache.
        fresh = await self.bot.fetch_guild(guild.id, with_counts=True)
        return channel_names(fresh.approximate_member_count, self.bot.skelly.db.config(guild.id))

    async def known_channels(self, guild, config):
        try:
            return {ch.id: ch for ch in await guild.fetch_channels()}
        except discord.InvalidData:
            # A newer, unrelated Discord channel type must not break existing
            # info displays. Fetch only our stored IDs; don't mask Forbidden.
            result = {}
            for slot in config['channels'].values():
                cid = slot.get('id')
                if not cid:
                    continue
                try:
                    result[cid] = await guild.fetch_channel(cid)
                except discord.NotFound:
                    continue
            return result

    async def setup(self, guild, category):
        if category.guild.id != guild.id:
            raise ReceiptError('同じサーバーのカテゴリを選んでください。')
        perms = category.permissions_for(guild.me)
        if not (perms.manage_channels and perms.view_channel):
            raise ReceiptError('BOTに設置先の「チャンネルを見る」「チャンネルの管理」権限が必要です。')
        async with self.lock:
            c = self.get(guild.id) or {'category': category.id, 'channels': {}, 'next_update': 0}
            if c['category'] != category.id:
                raise ReceiptError('すでに別カテゴリに設置済みです。既存カテゴリを指定してください。')
            names = await self.names(guild)
            existing = await self.known_channels(guild, c)
            for key, name in names.items():
                slot = c['channels'].get(key, {})
                if slot.get('id') in existing:
                    continue
                ch = await guild.create_voice_channel(name, category=category, overwrites=info_overwrites(guild),
                                                      reason='メンバー数・Skelly価格の案内表示')
                c['channels'][key] = {'id': ch.id, 'last_edit': time.time()}
                self.save(guild.id, c)
            c['next_update'] = time.time() + INTERVAL
            self.save(guild.id, c)
            return c

    async def refresh(self, guild, *, force=False):
        async with self.lock:
            c = self.get(guild.id)
            if not c:
                if force:
                    raise ReceiptError('先に /serverinfo setup で設置してください。')
                return
            if not force and time.time() < c.get('next_update', 0):
                return
            result = {'updated': 0, 'failed': 0, 'missing': 0}
            # Persist the cooldown even after restarts or failed requests.
            c['next_update'] = time.time() + INTERVAL
            self.save(guild.id, c)
            names = await self.names(guild)
            channels = await self.known_channels(guild, c)
            for key, slot in c['channels'].items():
                ch = channels.get(slot['id'])
                if not isinstance(ch, discord.VoiceChannel):
                    result['missing'] += 1
                    continue
                if ch.name == names[key]:
                    continue
                if not force and time.time() - slot.get('last_edit', 0) < INTERVAL:
                    continue
                slot['last_edit'] = time.time()
                self.save(guild.id, c)
                try:
                    await ch.edit(name=names[key], reason='Server infoの手動更新' if force else 'メンバー数・Skelly価格の自動更新')
                    result['updated'] += 1
                except discord.HTTPException as error:
                    result['failed'] += 1
                    log.warning('Server info channel %s update pending: %s', ch.id, type(error).__name__)

            return result

    async def start(self):
        self.worker.start()

    async def stop(self):
        self.worker.cancel()

    @tasks.loop(seconds=60)
    async def worker(self):
        for (gid,) in self.db.execute('SELECT guild FROM server_info').fetchall():
            if restricted(gid):continue
            guild = self.bot.get_guild(gid)
            if guild:
                try:
                    await self.refresh(guild)
                except Exception as error:
                    log.warning('Server info guild %s update pending: %s', gid, type(error).__name__)

    @worker.before_loop
    async def before_worker(self):
        await self.bot.wait_until_ready()


def register_serverinfo(bot):
    group = app_commands.Group(name='serverinfo', description='人数・Skelly価格の自動表示', guild_only=True)

    @group.command(name='setup', description='カテゴリに人数・Skelly価格の鍵付きチャンネルを設置')
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setup(interaction: discord.Interaction, category: discord.CategoryChannel):
        await interaction.response.defer(ephemeral=True)
        c = await bot.serverinfo.setup(interaction.guild, category)
        await interaction.followup.send('設置しました。約10分ごとに自動更新します。人数はBOTを含む概数です。\n' +
                                        '\n'.join(f"<#{v['id']}>" for v in c['channels'].values()), ephemeral=True)
    @group.command(name='refresh', description='人数・Skelly価格・受付状況を今すぐ更新')
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.checks.cooldown(1, 30, key=lambda i: i.guild_id)
    async def refresh(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        result = await bot.serverinfo.refresh(interaction.guild, force=True)
        message = f"最新情報を確認しました。更新：{result['updated']}件。"
        if result['failed']:
            message += f" 更新失敗：{result['failed']}件。BOTの権限を確認して再実行してください。"
        if result['missing']:
            message += f" チャンネル不明：{result['missing']}件。同じカテゴリで /serverinfo setup を再実行してください。"
        await interaction.followup.send(message, ephemeral=True)

    bot.tree.add_command(group)
