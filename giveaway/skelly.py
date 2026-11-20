from .access_policy import restricted
import asyncio
import hashlib
import logging
import time

import discord
from discord import app_commands
from discord.ext import tasks
from discord.http import Route

from .fulfillment import SafeView, ticket_overwrites
from .redemptions import ReceiptError
from .skelly_store import SkellyStore, price, quantity, side_open, reception_label

log = logging.getLogger('giveaway.skelly')
SIDES = {'buy': 'Buy｜Skellyを買いたい', 'sell': 'Sell｜Skellyを売りたい'}


def price_embed(config):
    embed = discord.Embed(title='Skelly Price｜スケルトンスポナー', color=0xE9EDEF,
        description='価格はスポナー **1個あたり・ゲーム内通貨m単位**です。\n希望のボタンを押して数量を入力してください。')
    embed.add_field(name='受付状況', value=reception_label(config), inline=False)
    embed.add_field(name=SIDES['buy'], value=(f"**{config['buy']}m / 個**" if side_open(config, 'buy') else '**STOP｜受付停止中**'), inline=False)
    embed.add_field(name=SIDES['sell'], value=(f"**{config['sell']}m / 個**" if side_open(config, 'sell') else '**STOP｜受付停止中**'), inline=False)
    embed.set_footer(text='専用チケットでスタッフが対応します。ゲーム内の支払い・アイテム移動は自動では行いません。')
    return embed


def order_embed(r):
    states = {'pending': '準備中', 'open': '受付済み', 'working': '対応中', 'completed': '対応完了', 'closed': 'クローズ'}
    embed = discord.Embed(title=f"Skelly取引 #{r['id']}", color=0xE9EDEF)
    for label, value in [('取引', SIDES[r['side']]), ('依頼者', f"<@{r['user']}>"), ('数量', f"{r['quantity']:,}個"),
                         ('受付時の単価', f"{r['unit']}m / 個"), ('合計金額', f"{r['total']}m"),
                         ('状態', states[r['status']]), ('担当者', f"<@{r['assignee']}>" if r.get('assignee') else '未担当')]:
        embed.add_field(name=label, value=value)
    if r.get('minecraft'):
        embed.add_field(name='連携済みMinecraft ID', value=r['minecraft']['name'], inline=False)
    embed.set_footer(text='受付時の価格を記録しています。実際の受け渡しはスタッフと確認してください。')
    return embed


class SkellyPanel(SafeView):
    def __init__(self, service, accepting=True, config=None):
        super().__init__(timeout=None)
        for side, label in SIDES.items():
            button = discord.ui.Button(label=label, custom_id=f'skelly:{side}:v1', disabled=not (side_open(config, side) if config is not None else accepting),
                                       style=discord.ButtonStyle.success if side == 'buy' else discord.ButtonStyle.primary)
            async def clicked(interaction, side=side):
                if not interaction.guild or not any(p['message'] == interaction.message.id for p in service.db.panels(interaction.guild_id)):
                    raise ReceiptError('このパネルは使用できません。管理者に再設置を依頼してください。')
                existing = service.db.active(interaction.guild_id, interaction.user.id)
                if existing:
                    return await interaction.response.send_message(service.existing_text(existing), ephemeral=True)
                config = service.configured(interaction.guild_id)
                if not side_open(config, side):
                    raise ReceiptError(f'現在、Skelly {side.title()}の受付は停止中です。再開までお待ちください。')
                await interaction.response.send_modal(SkellyQuantity(service, interaction.guild_id, side, config[side]))
            button.callback = clicked
            self.add_item(button)


class SkellyQuantity(discord.ui.Modal):
    def __init__(self, service, guild, side, unit):
        super().__init__(title=f"{'購入' if side == 'buy' else '売却'}数量｜{unit}m / 個")
        self.service, self.guild, self.side, self.unit = service, guild, side, unit
        self.count = discord.ui.TextInput(label='スケルトンスポナーの数量', placeholder='例：64', min_length=1, max_length=7)
        self.add_item(self.count)

    async def on_submit(self, interaction):
        if interaction.guild_id != self.guild:
            raise ReceiptError('元のサーバーで操作してください。')
        count = quantity(self.count.value)
        await interaction.response.defer(ephemeral=True)
        r = await self.service.request(interaction, self.side, count, self.unit)
        await interaction.followup.send(self.service.existing_text(r), ephemeral=True)

    async def on_error(self, interaction, error):
        await SafeView.on_error(self, interaction, error, None)


class SkellyControls(SafeView):
    def __init__(self, service, ticket):
        super().__init__(timeout=None)
        for action, label in [('start', '対応開始'), ('done', '対応完了'), ('close', '閉じる')]:
            button = discord.ui.Button(label=label, custom_id=f'skelly:ticket:{ticket["id"]}:{action}',
                                       style=discord.ButtonStyle.secondary, disabled=ticket['status'] == 'closed')
            async def clicked(interaction, action=action):
                await interaction.response.defer(ephemeral=True)
                await service.action(interaction, ticket['id'], action)
                await interaction.followup.send('更新しました。', ephemeral=True)
            button.callback = clicked
            self.add_item(button)


class SkellySetup(SafeView):
    def __init__(self, service, author, category):
        super().__init__(timeout=300)
        self.service, self.author, self.category = service, author, category
        self.roles = []
        select = discord.ui.RoleSelect(placeholder='Skelly対応スタッフ（複数可）', min_values=1, max_values=10)
        async def selected(interaction):
            if interaction.user.id != author:
                raise ReceiptError('設定を開始した管理者だけが操作できます。')
            self.roles = [role.id for role in select.values]
            await interaction.response.defer()
        select.callback = selected
        self.add_item(select)

    @discord.ui.button(label='設定を保存', style=discord.ButtonStyle.success)
    async def save(self, interaction, button):
        if interaction.user.id != self.author or not interaction.permissions.manage_guild:
            raise ReceiptError('設定を開始した管理者だけが保存できます。')
        await interaction.response.defer(ephemeral=True)
        policy = dict(category=self.category, roles=self.roles, minecraft=False)
        await self.service.bot.fulfillment.validate_policy(interaction.guild, policy)
        async with self.service.lock:
            self.service.db.set_config(interaction.guild_id, policy=policy)
        for child in self.children:
            child.disabled = True
        await interaction.edit_original_response(content='Skellyの作成先・スタッフを保存しました。価格は /skelly price、パネルは /skelly panel で設定できます。', view=self)


class Skelly:
    def __init__(self, bot):
        self.bot, self.db = bot, SkellyStore(bot.store.db)
        self.lock = asyncio.Lock()
        self.registered = set()

    def configured(self, guild):
        config = self.db.config(guild)
        if not config.get('policy') or not config.get('buy') or not config.get('sell'):
            raise ReceiptError('Skellyは未設定です。管理者が /skelly setup と /skelly price を実行してください。')
        return config

    @staticmethod
    def existing_text(r):
        if r.get('channel'):
            return f"Skelly取引チケット：<#{r['channel']}>\n受付時の合計：{r['total']}m。別の取引はこのチケットが閉じられてから申し込めます。"
        return 'Skellyチケットを準備中です。作成後、サーバー内に専用チャンネルが表示されます。'

    def register(self, r):
        if r['id'] not in self.registered:
            self.bot.add_view(SkellyControls(self, r))
            self.registered.add(r['id'])

    async def start(self):
        self.bot.add_view(SkellyPanel(self))
        for r in self.db.pending():
            self.register(r)
        self.worker.start()

    async def stop(self):
        self.worker.cancel()

    async def request(self, interaction, side, count, unit):
        async with self.lock:
            r = self.db.active(interaction.guild_id, interaction.user.id)
            if r:
                return r
            config = self.configured(interaction.guild_id)
            await self.bot.fulfillment.validate_policy(interaction.guild, config['policy'])
            r = self.db.create(interaction.guild_id, interaction.user.id, side, count, unit)
            self.register(r)
            try:
                await self.open_ticket(r)
            except discord.HTTPException:
                log.warning('Skelly %s creation pending', r['id'])
            return r

    async def open_ticket(self, r):
        if r.get('channel'):
            return
        guild = self.bot.get_guild(r['guild'])
        if not guild:
            raise ReceiptError('サーバーが見つかりません。')
        category, roles = await self.bot.fulfillment.validate_policy(guild, r['policy'])
        member = await guild.fetch_member(r['user'])
        marker = f"skelly-ticket:{self.bot.user.id}:{r['id']}"
        channels = await guild.fetch_channels() if r.get('creating') else []
        channel = next((c for c in channels if isinstance(c, discord.TextChannel) and c.topic == marker), None)
        if not channel:
            r['creating'] = True
            self.db.save(r)
            channel = await guild.create_text_channel(f"skelly-{r['side']}-{r['id']}", category=category, topic=marker,
                overwrites=ticket_overwrites(guild, member, roles, guild.me), reason=f"Skelly取引 #{r['id']}")
        r.update(channel=channel.id, status='open', minecraft=self.bot.fulfillment.db.link(r['user'], r['guild']), dirty=True)
        self.db.save(r)
        await self.update_ticket(r)

    async def update_ticket(self, r):
        channel = self.bot.get_channel(r['channel']) or await self.bot.fetch_channel(r['channel'])
        view, embed = SkellyControls(self, r), order_embed(r)
        if not r.get('control'):
            # Discord nonce deduplication is short-lived. Never blindly resend
            # an old uncertain notification; /skelly manage remains available.
            if r.get('sending_at') and time.time() - r['sending_at'] > 120:
                r.update(dirty=False, notification_uncertain=True)
                self.db.save(r)
                return
            if not r.get('sending_at'):
                r['sending_at'] = time.time()
                self.db.save(r)
            result = await self.bot.http.request(Route('POST', '/channels/{channel_id}/messages', channel_id=channel.id), json={
                'content': f"<@{r['user']}> " + ' '.join(f'<@&{rid}>' for rid in r['policy']['roles']) + '\nSkelly取引の依頼です。',
                'allowed_mentions': {'parse': [], 'users': [str(r['user'])], 'roles': [str(x) for x in r['policy']['roles']]},
                'embeds': [embed.to_dict()], 'components': view.to_components(),
                'nonce': hashlib.sha256(f"skelly:{r['guild']}:{r['id']}".encode()).hexdigest()[:24], 'enforce_nonce': True})
            r['control'] = int(result['id'])
        else:
            await channel.get_partial_message(r['control']).edit(embed=embed, view=view)
        r['dirty'] = False
        self.db.save(r)

    async def refresh_panels(self, guild=None):
        for panel in self.db.panels(guild):
            if restricted(panel["guild"]):continue
            config = self.db.config(panel['guild'])
            if config['revision'] == panel['revision']:
                continue
            try:
                channel = self.bot.get_channel(panel['channel']) or await self.bot.fetch_channel(panel['channel'])
                await channel.get_partial_message(panel['message']).edit(embed=price_embed(config), view=SkellyPanel(self, config=config))
                self.db.save_panel(panel['guild'], panel['channel'], panel['message'], config['revision'])
            except discord.NotFound:
                self.db.remove_panel(panel['message'])
            except discord.HTTPException:
                log.warning('Skelly price panel %s update pending', panel['message'])

    async def action(self, interaction, rid, action):
        async with self.lock:
            r = self.db.get(rid)
            if not r or not interaction.guild or r['guild'] != interaction.guild_id or r['channel'] != interaction.channel_id:
                raise ReceiptError('対象のSkellyチケット内で操作してください。')
            member = await interaction.guild.fetch_member(interaction.user.id)
            if not any(role.id in r['policy']['roles'] for role in member.roles):
                raise ReceiptError('指定されたSkellyスタッフだけが操作できます。')
            if r['status'] == 'closed':
                raise ReceiptError('このチケットは閉じています。')
            if action == 'start':
                if r['status'] != 'open':
                    raise ReceiptError('すでに対応中または対応完了です。')
                r.update(status='working', assignee=member.id)
            elif action == 'done':
                if r['status'] not in ('open', 'working'):
                    raise ReceiptError('すでに対応完了です。')
                r.update(status='completed', completed_by=member.id)
            elif action == 'close':
                guild = interaction.guild
                try:
                    owner = await guild.fetch_member(r['user'])
                except discord.NotFound:
                    owner = discord.Object(id=r['user'], type=discord.Member)
                roles = [guild.get_role(rid) for rid in r['policy']['roles']]
                if not all(roles):
                    raise ReceiptError('スタッフロールが削除されています。管理者が権限を確認してください。')
                await interaction.channel.edit(overwrites=ticket_overwrites(guild, owner, roles, guild.me, closed=True), reason='Skellyチケットをクローズ')
                r.update(status='closed', closed_by=member.id)
            else:
                raise ReceiptError('不明な操作です。')
            r.setdefault('history', []).append(dict(action=action, by=member.id, at=time.time()))
            r['dirty'] = True
            self.db.save(r)
            await self.update_ticket(r)

    @tasks.loop(seconds=15)
    async def worker(self):
        async with self.lock:
            await self.refresh_panels()
            for r in self.db.pending():
                if restricted(r["guild"]):continue
                try:
                    self.register(r)
                    if not r.get('channel'):
                        await self.open_ticket(r)
                    if r.get('channel') and r.get('dirty'):
                        await self.update_ticket(r)
                except Exception as error:
                    log.warning('Skelly %s pending: %s', r['id'], type(error).__name__)

    @worker.before_loop
    async def before_worker(self):
        await self.bot.wait_until_ready()


def register_skelly(bot):
    group = app_commands.Group(name='skelly', description='Skellyの価格・取引チケット', guild_only=True)

    @group.command(name='setup', description='Skellyチケットのカテゴリとスタッフを設定')
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setup(interaction: discord.Interaction, category: discord.CategoryChannel):
        await interaction.response.send_message(f'作成先：{category.mention}\nSkelly対応スタッフを選んで保存してください。',
                                               view=SkellySetup(bot.skelly, interaction.user.id, category.id), ephemeral=True)

    @group.command(name='price', description='1個あたりのBuy・Sell価格をm単位で更新')
    @app_commands.describe(buy='利用者が買う価格（m/個）。例：1.5', sell='利用者が売る価格（m/個）。例：1.2')
    @app_commands.checks.has_permissions(manage_guild=True)
    async def prices(interaction: discord.Interaction, buy: str, sell: str):
        buy, sell = price(buy), price(sell)
        await interaction.response.defer(ephemeral=True)
        async with bot.skelly.lock:
            bot.skelly.db.set_config(interaction.guild_id, buy=buy, sell=sell)
            await bot.skelly.refresh_panels(interaction.guild_id)
        await interaction.followup.send(f'価格を保存しました。Buy：{buy}m / 個、Sell：{sell}m / 個。\n設置済みパネルも更新します。既存チケットの金額は変わりません。', ephemeral=True)

    @group.command(name='panel', description='Skellyの価格とBuy・Sellチケットパネルを設置')
    @app_commands.checks.has_permissions(manage_guild=True)
    async def panel(interaction: discord.Interaction, channel: discord.TextChannel):
        if channel.guild.id != interaction.guild_id:
            raise ReceiptError('同じサーバーのチャンネルを選んでください。')
        perms = channel.permissions_for(interaction.guild.me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links):
            raise ReceiptError('設置先でBOTの閲覧・送信・埋め込みリンク権限が必要です。')
        await interaction.response.defer(ephemeral=True)
        async with bot.skelly.lock:
            config = bot.skelly.configured(interaction.guild_id)
            await bot.fulfillment.validate_policy(interaction.guild, config['policy'])
            message = await channel.send(embed=price_embed(config), view=SkellyPanel(bot.skelly, config=config), allowed_mentions=discord.AllowedMentions.none())
            bot.skelly.db.save_panel(interaction.guild_id, channel.id, message.id, config['revision'])
        await interaction.followup.send(f'Skellyパネルを設置しました：{message.jump_url}', ephemeral=True)

    @group.command(name='reception', description='Skellyの新規受付を開始・停止する')
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.choices(status=[app_commands.Choice(name='受付中', value='open'), app_commands.Choice(name='受付停止中', value='closed')])
    @app_commands.choices(side=[app_commands.Choice(name='両方', value='both'), app_commands.Choice(name='Buy', value='buy'), app_commands.Choice(name='Sell', value='sell')])
    async def reception(interaction: discord.Interaction, status: app_commands.Choice[str], side: str = 'both'):
        await interaction.response.defer(ephemeral=True)
        async with bot.skelly.lock:
            accepting = status.value == 'open'
            c = bot.skelly.db.config(interaction.guild_id)
            updates = {k + '_accepting': (accepting if side in ('both', k) else side_open(c, k)) for k in ('buy', 'sell')}
            bot.skelly.db.set_config(interaction.guild_id, **updates)
            await bot.skelly.refresh_panels(interaction.guild_id)
        label = reception_label(bot.skelly.db.config(interaction.guild_id))
        try:
            await bot.serverinfo.refresh(interaction.guild, force=True)
        except Exception as error:
            log.warning('Skelly reception info refresh pending: %s', type(error).__name__)
        await interaction.followup.send(f'Skellyを「{label}」に変更しました。既存チケットは引き続き対応できます。\n概要のチャンネル名も更新します。反映されない場合は /serverinfo refresh を実行してください。', ephemeral=True)

    @group.command(name='settings', description='Skellyの価格とスタッフ設定を確認')
    @app_commands.checks.has_permissions(manage_guild=True)
    async def settings(interaction: discord.Interaction):
        c = bot.skelly.db.config(interaction.guild_id)
        policy = c.get('policy', {})
        await interaction.response.send_message(f"受付：{reception_label(c)}\nBuy：{c.get('buy', '未設定')}m / 個\nSell：{c.get('sell', '未設定')}m / 個\n"
            f"カテゴリ：{('<#' + str(policy['category']) + '>') if policy else '未設定'}\nスタッフ：" +
            (' '.join(f'<@&{rid}>' for rid in policy.get('roles', [])) or '未設定'), ephemeral=True)

    @group.command(name='manage', description='このSkellyチケットを対応開始・完了・クローズ')
    @app_commands.choices(action=[app_commands.Choice(name=label, value=value) for value, label in [('start', '対応開始'), ('done', '対応完了'), ('close', '閉じる')]])
    async def manage(interaction: discord.Interaction, action: app_commands.Choice[str]):
        await interaction.response.defer(ephemeral=True)
        r = bot.skelly.db.by_channel(interaction.guild_id, interaction.channel_id)
        if not r:
            raise ReceiptError('Skellyチケット内で実行してください。')
        await bot.skelly.action(interaction, r['id'], action.value)
        await interaction.followup.send('更新しました。', ephemeral=True)

    bot.tree.add_command(group)
