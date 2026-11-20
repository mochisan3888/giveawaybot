from .access_policy import restricted
import asyncio
import hashlib
import json
import logging
import time
import discord
from discord import app_commands
from discord.ext import tasks
from discord.http import Route
from .fulfillment import SafeView, ticket_overwrites
from .redemptions import ReceiptError
from .vip_store import VipStore
from .vip_prices import price, validate_prices, quote, price_embed

log = logging.getLogger('giveaway.vip')


def vip_embed(r):
    states = {'pending': '準備中', 'open': '受付済み', 'working': '対応中', 'completed': '対応完了', 'closed': 'クローズ'}
    e = discord.Embed(title=f"VIP申請チケット #{r['id']}", color=0xD4AF37)
    e.add_field(name='申込者', value=f"<@{r['user']}>")
    e.add_field(name='VIP申請内容', value=r['content'], inline=False)
    if r.get('quote'):
        q = r['quote']
        e.add_field(name='お支払い予定額（申請時点）', value=f"**{q['due']}m**\n{q['price']}m − {q['base']}m", inline=False)
    else:
        e.add_field(name='お支払い予定額', value='料金未設定・スタッフに確認', inline=False)
    if r.get('note'):
        e.add_field(name='補足・希望日時', value=r['note'], inline=False)
    if r.get('minecraft'):
        e.add_field(name='連携済みMinecraft ID', value=r['minecraft']['name'])
    e.add_field(name='状態', value=states[r['status']])
    e.add_field(name='担当者', value=f"<@{r['assignee']}>" if r.get('assignee') else '未担当')
    e.set_footer(text='DonutSMPゲーム内通貨で購入。受け渡し・ランク付与はスタッフが対応します。')
    return e


def available_tiers(config, member, kind):
    tiers = config.get('tiers', [])
    if not tiers:
        raise ReceiptError('VIPロールが未設定です。管理者が /vip tiers で登録してください。')
    owned = {r.id for r in member.roles}
    highest = max([i for i, rid in enumerate(tiers) if rid in owned], default=-1)
    if kind == 'new' and highest >= 0:
        raise ReceiptError('VIPをお持ちです。「ランクアップ申請」を選んでください。')
    if kind == 'upgrade' and highest < 0:
        raise ReceiptError('VIPをお持ちではありません。「新規申請」を選んでください。')
    choices = tiers[highest + 1:]
    if not choices:
        raise ReceiptError('すでに最上位のVIPをお持ちです。')
    return choices


def panel_embed(accepting=True):
    e = discord.Embed(title='👑 VIP MEMBERSHIP', color=0xD4AF37,
        description='━━━━━━━━━━━━━━━━━━━━\n**特別な体験を、あなたに。**\n━━━━━━━━━━━━━━━━━━━━\n\nご希望の申請をお選びください。専用チケットでスタッフがご案内します。')
    e.add_field(name='✦ 新規申請', value='VIPをまだお持ちでない方はこちら。', inline=False)
    e.add_field(name='✧ ランクアップ申請', value='現在より上位のVIPをご希望の方はこちら。', inline=False)
    e.add_field(name='お支払いについて', value='DonutSMPのゲーム内通貨で購入できます。\n金額・受け渡しはチケット内でスタッフにご確認ください。', inline=False)
    e.add_field(name='受付状況', value='🟢 受付中' if accepting else '🔴 受付停止中', inline=False)
    e.set_footer(text='VIP CONCIERGE • お支払い・ランク付与はスタッフが対応します')
    return e


class VipPanel(SafeView):
    def __init__(self, service, accepting=True):
        super().__init__(timeout=None)
        self.service = service
        for child in self.children:
            child.disabled = not accepting

    async def begin(self, interaction, kind):
        if not interaction.guild or not any(p['message'] == interaction.message.id for p in self.service.db.panels(interaction.guild_id)):
            raise ReceiptError('このパネルは使用できません。管理者に再設置を依頼してください。')
        await interaction.response.defer(ephemeral=True)
        existing = self.service.db.active(interaction.guild_id, interaction.user.id)
        if existing:
            return await interaction.followup.send(self.service.existing_text(existing), ephemeral=True)
        config = self.service.configured(interaction.guild_id)
        if not config.get('accepting', True):
            raise ReceiptError('VIP申請は現在、受付停止中です。')
        member = await interaction.guild.fetch_member(interaction.user.id)
        roles = available_tiers(config, member, kind)
        await interaction.followup.send('ご希望のVIPを選択してください。', view=VipTarget(self.service, interaction.user.id, interaction.guild, roles, kind), ephemeral=True)

    @discord.ui.button(label='新規申請', emoji='✨', style=discord.ButtonStyle.primary, custom_id='vip:new:v1')
    async def new(self, interaction, button):
        await self.begin(interaction, 'new')

    @discord.ui.button(label='ランクアップ申請', emoji='👑', style=discord.ButtonStyle.secondary, custom_id='vip:upgrade:v1')
    async def upgrade(self, interaction, button):
        await self.begin(interaction, 'upgrade')


class VipTarget(SafeView):
    def __init__(self, service, author, guild, roles, kind):
        super().__init__(timeout=300)
        options = [discord.SelectOption(label=guild.get_role(r).name[:100], value=str(r)) for r in roles if guild.get_role(r)]
        if not options:
            raise ReceiptError('VIPロールが削除されています。管理者に連絡してください。')
        select = discord.ui.Select(placeholder='希望のVIP', options=options)
        async def selected(interaction):
            if interaction.user.id != author:
                raise ReceiptError('申請した本人だけが操作できます。')
            await interaction.response.defer(ephemeral=True)
            r = await service.request(interaction, kind, int(select.values[0]))
            await interaction.followup.send(service.existing_text(r), ephemeral=True)
        select.callback = selected
        self.add_item(select)


class VipControls(SafeView):
    def __init__(self, service, ticket):
        super().__init__(timeout=None)
        for action, label in [('start', '対応開始'), ('done', '対応完了'), ('close', '閉じる')]:
            button = discord.ui.Button(label=label, custom_id=f'vip:ticket:{ticket["id"]}:{action}',
                                       style=discord.ButtonStyle.secondary, disabled=ticket['status'] == 'closed')
            async def clicked(interaction, action=action):
                await interaction.response.defer(ephemeral=True)
                await service.action(interaction, ticket['id'], action)
                await interaction.followup.send('更新しました。', ephemeral=True)
            button.callback = clicked
            self.add_item(button)


class VipSetup(SafeView):
    def __init__(self, service, author, category):
        super().__init__(timeout=300)
        self.service, self.author, self.category = service, author, category
        self.roles = []
        select = discord.ui.RoleSelect(placeholder='VIP申請対応スタッフ（複数可）', min_values=1, max_values=10)
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
        await interaction.edit_original_response(content='VIP申請の作成先・スタッフを保存しました。/vip panel でパネルを設置できます。', view=self)


class Vip:
    def __init__(self, bot):
        self.bot, self.db = bot, VipStore(bot.store.db)
        self.lock = asyncio.Lock()
        self.registered = set()

    def configured(self, guild):
        config = self.db.config(guild)
        if not config.get('policy'):
            raise ReceiptError('VIP申請は未設定です。管理者が /vip setup を実行してください。')
        return config

    @staticmethod
    def existing_text(r):
        if r.get('channel'):
            return f"VIP申請チケット：<#{r['channel']}>\nお支払い・ランク付与はスタッフがご案内します。"
        return 'VIP申請チケットを準備中です。作成後、サーバー内に専用チャンネルが表示されます。'

    def register(self, r):
        if r['id'] not in self.registered:
            self.bot.add_view(VipControls(self, r))
            self.registered.add(r['id'])

    async def start(self):
        self.bot.add_view(VipPanel(self))
        for r in self.db.pending():
            self.register(r)
        self.worker.start()

    async def stop(self):
        self.worker.cancel()

    async def post_panel(self, guild, channel):
        if channel.guild.id != guild.id:
            raise ReceiptError('同じサーバーのチャンネルを選んでください。')
        config = self.configured(guild.id)
        await self.bot.fulfillment.validate_policy(guild, config['policy'])
        perms = channel.permissions_for(guild.me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links):
            raise ReceiptError('パネル設置先の閲覧・送信・埋め込みリンク権限が必要です。')
        if not config.get('tiers'):
            raise ReceiptError('先に /vip tiers でVIPロールを登録してください。')
        message = await channel.send(embed=panel_embed(config.get('accepting', True)), view=VipPanel(self, config.get('accepting', True)), allowed_mentions=discord.AllowedMentions.none())
        self.db.save_panel(guild.id, channel.id, message.id, config['revision'])
        return message

    async def request(self, interaction, kind, target):
        async with self.lock:
            r = self.db.active(interaction.guild_id, interaction.user.id)
            if r:
                return r
            config = self.configured(interaction.guild_id)
            if not config.get('accepting', True):
                raise ReceiptError('VIP申請は現在、受付停止中です。')
            await self.bot.fulfillment.validate_policy(interaction.guild, config['policy'])
            member = await interaction.guild.fetch_member(interaction.user.id)
            if target not in available_tiers(config, member, kind) or not interaction.guild.get_role(target):
                raise ReceiptError('選択したVIPには申請できません。パネルから選び直してください。')
            content = ('新規申請' if kind == 'new' else 'ランクアップ申請') + f' → <@&{target}>'
            pricing = quote(config, member, target)
            r = self.db.create(interaction.guild_id, interaction.user.id, content, '')
            r.update(kind=kind, target=target, quote=pricing)
            self.db.save(r)
            self.register(r)
            try:
                await self.open_ticket(r)
            except discord.HTTPException:
                log.warning('Vip %s creation pending', r['id'])
            return r

    async def open_ticket(self, r):
        if r.get('channel'):
            return
        guild = self.bot.get_guild(r['guild'])
        if not guild:
            raise ReceiptError('サーバーが見つかりません。')
        category, roles = await self.bot.fulfillment.validate_policy(guild, r['policy'])
        member = await guild.fetch_member(r['user'])
        marker = f"vip-ticket:{self.bot.user.id}:{r['id']}"
        channels = await guild.fetch_channels() if r.get('creating') else []
        channel = next((c for c in channels if isinstance(c, discord.TextChannel) and c.topic == marker), None)
        if not channel:
            r['creating'] = True
            self.db.save(r)
            channel = await guild.create_text_channel(f"VIP申請-{r['id']}", category=category, topic=marker,
                overwrites=ticket_overwrites(guild, member, roles, guild.me), reason=f"VIP申請 #{r['id']}")
        r.update(channel=channel.id, status='open', minecraft=self.bot.fulfillment.db.link(r['user'], r['guild']), dirty=True)
        self.db.save(r)
        await self.update_ticket(r)

    async def update_ticket(self, r):
        channel = self.bot.get_channel(r['channel']) or await self.bot.fetch_channel(r['channel'])
        view, embed = VipControls(self, r), vip_embed(r)
        if not r.get('control'):
            # Discord nonce deduplication is short-lived. Never blindly resend
            # an old uncertain notification; /vip manage remains available.
            if r.get('sending_at') and time.time() - r['sending_at'] > 120:
                r.update(dirty=False, notification_uncertain=True)
                self.db.save(r)
                return
            if not r.get('sending_at'):
                r['sending_at'] = time.time()
                self.db.save(r)
            result = await self.bot.http.request(Route('POST', '/channels/{channel_id}/messages', channel_id=channel.id), json={
                'content': f"<@{r['user']}> " + ' '.join(f'<@&{rid}>' for rid in r['policy']['roles']) + '\nVIP申請の依頼です。',
                'allowed_mentions': {'parse': [], 'users': [str(r['user'])], 'roles': [str(x) for x in r['policy']['roles']]},
                'embeds': [embed.to_dict()], 'components': view.to_components(),
                'nonce': hashlib.sha256(f"vip:{r['guild']}:{r['id']}".encode()).hexdigest()[:24], 'enforce_nonce': True})
            r['control'] = int(result['id'])
        else:
            await channel.get_partial_message(r['control']).edit(embed=embed, view=view)
        r['dirty'] = False
        self.db.save(r)

    async def action(self, interaction, rid, action):
        async with self.lock:
            r = self.db.get(rid)
            if not r or not interaction.guild or r['guild'] != interaction.guild_id or r['channel'] != interaction.channel_id:
                raise ReceiptError('対象のVIP申請チケット内で操作してください。')
            member = await interaction.guild.fetch_member(interaction.user.id)
            if not any(role.id in r['policy']['roles'] for role in member.roles):
                raise ReceiptError('指定されたVIP申請スタッフだけが操作できます。')
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
                await interaction.channel.edit(overwrites=ticket_overwrites(guild, owner, roles, guild.me, closed=True), reason='VIP申請チケットをクローズ')
                r.update(status='closed', closed_by=member.id)
            else:
                raise ReceiptError('不明な操作です。')
            r.setdefault('history', []).append(dict(action=action, by=member.id, at=time.time()))
            r['dirty'] = True
            self.db.save(r)
            await self.update_ticket(r)

    async def refresh_panels(self):
        for panel in self.db.panels():
            if restricted(panel["guild"]):continue
            config = self.db.config(panel['guild'])
            if panel['revision'] == config['revision']:
                continue
            try:
                channel = self.bot.get_channel(panel['channel']) or await self.bot.fetch_channel(panel['channel'])
                accepting = config.get('accepting', True)
                await channel.get_partial_message(panel['message']).edit(embed=panel_embed(accepting), view=VipPanel(self, accepting), allowed_mentions=discord.AllowedMentions.none())
                self.db.save_panel(panel['guild'], panel['channel'], panel['message'], config['revision'])
            except discord.NotFound:
                self.db.remove_panel(panel['message'])
            except discord.HTTPException as error:
                log.warning('VIP application panel %s pending: %s', panel['message'], type(error).__name__)

    def price_display(self, guild):
        normal = self.bot.store.rules(guild) if hasattr(self.bot.store, 'rules') else {}
        embed = price_embed(self.db.config(guild), normal)
        revision = hashlib.sha256(json.dumps(embed.to_dict(), sort_keys=True).encode()).hexdigest()
        return embed, revision

    async def refresh_prices(self):
        rows = self.db.db.execute('SELECT message,guild,channel,revision FROM vip_price_panels').fetchall()
        for message, guild, channel, revision in rows:
            if restricted(guild):continue
            embed, current_revision = self.price_display(guild)
            if revision == current_revision:
                continue
            try:
                ch = self.bot.get_channel(channel) or await self.bot.fetch_channel(channel)
                await ch.get_partial_message(message).edit(embed=embed, allowed_mentions=discord.AllowedMentions.none())
                with self.db.db:
                    self.db.db.execute('UPDATE vip_price_panels SET revision=? WHERE message=?', (current_revision, message))
            except discord.NotFound:
                with self.db.db:self.db.db.execute('DELETE FROM vip_price_panels WHERE message=?', (message,))
            except discord.HTTPException as error:
                log.warning('VIP price panel %s pending: %s', message, type(error).__name__)

    @tasks.loop(seconds=15)
    async def worker(self):
        await self.refresh_panels()
        await self.refresh_prices()
        async with self.lock:
            for r in self.db.pending():
                if restricted(r["guild"]):continue
                try:
                    self.register(r)
                    if not r.get('channel'):
                        await self.open_ticket(r)
                    if r.get('channel') and r.get('dirty'):
                        await self.update_ticket(r)
                except Exception as error:
                    log.warning('Vip %s pending: %s', r['id'], type(error).__name__)

    @worker.before_loop
    async def before_worker(self):
        await self.bot.wait_until_ready()


def register_vip(bot):
    group = app_commands.Group(name='vip', description='VIP申請チケットの設定・管理', guild_only=True)

    @group.command(name='setup', description='VIP申請チケットのカテゴリとスタッフを設定')
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setup(interaction: discord.Interaction, category: discord.CategoryChannel):
        await interaction.response.send_message(f'作成先：{category.mention}\nVIP申請対応スタッフを選んで保存してください。',
            view=VipSetup(bot.vip, interaction.user.id, category.id), ephemeral=True)

    @group.command(name='panel', description='VIP申請申し込みのパネルを設置')
    @app_commands.checks.has_permissions(manage_guild=True)
    async def panel(interaction: discord.Interaction, channel: discord.TextChannel):
        await interaction.response.defer(ephemeral=True)
        message = await bot.vip.post_panel(interaction.guild, channel)
        await interaction.followup.send(f'VIP申請パネルを設置しました：{message.jump_url}', ephemeral=True)

    @group.command(name='manage', description='VIP申請チケットの対応開始・完了・クローズ')
    @app_commands.choices(action=[app_commands.Choice(name=label, value=value) for value, label in [('start', '対応開始'), ('done', '対応完了'), ('close', '閉じる')]])
    async def manage(interaction: discord.Interaction, action: app_commands.Choice[str]):
        await interaction.response.defer(ephemeral=True)
        r = bot.vip.db.by_channel(interaction.guild_id, interaction.channel_id)
        if not r:
            raise ReceiptError('VIP申請チケット内で実行してください。')
        await bot.vip.action(interaction, r['id'], action.value)
        await interaction.followup.send('更新しました。', ephemeral=True)
    @group.command(name='tiers', description='VIPロールを下位から上位の順で登録・確認（最大10個）')
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(roles='下位→上位の順にロールをメンション。例：@VIP @VIP+ @MVP。省略で確認')
    async def tiers(interaction: discord.Interaction, roles: str = None):
        import re
        if roles is not None:
            ids = [int(x) for x in re.findall(r'<@&(\d+)>', roles)]
            if re.sub(r'<@&\d+>', '', roles).strip(' ,、\n'):
                raise ReceiptError('VIPロールを下位→上位の順でメンションしてください。')
            if any(interaction.guild.get_role(r) is None for r in ids):
                raise ReceiptError('このサーバーに存在するロールを指定してください。')
            bot.vip.db.set_tiers(interaction.guild_id, ids)
        current = bot.vip.db.tiers(interaction.guild_id)
        await interaction.response.send_message('VIP順序（下位→上位）\n' + (' → '.join(f'<@&{r}>' for r in current) or '未設定'), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @group.command(name='copy_receipt', description='Giveawayの受け取り設定から作成先とスタッフをコピー')
    @app_commands.checks.has_permissions(manage_guild=True)
    async def copy_receipt(interaction: discord.Interaction, channel: discord.TextChannel):
        policy = bot.fulfillment.db.profile(interaction.guild_id, channel.id)
        if not policy:
            raise ReceiptError('このチャンネルに受け取り設定がありません。')
        await interaction.response.defer(ephemeral=True)
        await bot.fulfillment.validate_policy(interaction.guild, policy)
        bot.vip.db.set_config(interaction.guild_id, policy=dict(category=policy['category'], roles=policy['roles'], minecraft=False))
        await interaction.followup.send('VIP申請の作成先・スタッフ設定をコピーしました。', ephemeral=True)

    @group.command(name='price', description='VIPの新規購入価格をm単位で設定')
    @app_commands.checks.has_permissions(manage_guild=True)
    async def set_price(interaction: discord.Interaction, role: discord.Role, amount: str):
        config = bot.vip.db.config(interaction.guild_id)
        if role.id not in config.get('tiers', []):
            raise ReceiptError('先に /vip tiers にVIPロールを登録してください。')
        prices = dict(config.get('prices', {}))
        prices[str(role.id)] = price(amount)
        validate_prices(config['tiers'], prices)
        bot.vip.db.set_config(interaction.guild_id, prices=prices)
        await interaction.response.send_message(f'{role.mention}：{prices[str(role.id)]}m に設定しました。料金表は約15秒以内に更新します。既存チケットの料金は変わりません。', ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @group.command(name='prices', description='VIP料金表を自分だけに表示')
    @app_commands.checks.has_permissions(manage_guild=True)
    async def prices(interaction: discord.Interaction):
        await interaction.response.send_message(embed=bot.vip.price_display(interaction.guild_id)[0], ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @group.command(name='price_panel', description='自動更新するVIP料金表を設置')
    @app_commands.checks.has_permissions(manage_guild=True)
    async def price_panel(interaction: discord.Interaction, channel: discord.TextChannel):
        if channel.guild.id != interaction.guild_id:
            raise ReceiptError('同じサーバーのチャンネルを指定してください。')
        config = bot.vip.db.config(interaction.guild_id)
        if not config.get('tiers'):
            raise ReceiptError('先に /vip tiers でVIPを登録してください。')
        p = channel.permissions_for(interaction.guild.me)
        if not (p.view_channel and p.send_messages and p.embed_links):
            raise ReceiptError('BOTに料金表の設置先の閲覧・送信・埋め込み権限が必要です。')
        await interaction.response.defer(ephemeral=True)
        embed, revision = bot.vip.price_display(interaction.guild_id)
        message = await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        with bot.vip.db.db:
            bot.vip.db.db.execute('INSERT INTO vip_price_panels VALUES (?,?,?,?)', (message.id, interaction.guild_id, channel.id, revision))
        await interaction.followup.send(f'料金表を設置しました：{message.jump_url}', ephemeral=True)

    @group.command(name='benefits', description='VIP料金表に表示する自由記述の特典を編集')
    @app_commands.checks.has_permissions(manage_guild=True)
    async def benefits(interaction: discord.Interaction, role: discord.Role):
        config = bot.vip.db.config(interaction.guild_id)
        if role.id not in config.get('tiers', []):
            raise ReceiptError('先に /vip tiers にVIPロールを登録してください。')
        modal = discord.ui.Modal(title='VIP特典を編集')
        field = discord.ui.TextInput(label='特典・メリット（空欄で削除）', style=discord.TextStyle.paragraph,
            max_length=300, required=False, default=config.get('benefits', {}).get(str(role.id), ''),
            placeholder='例：専用チャットが利用可能\nVIP限定イベントに参加可能')
        modal.add_item(field)
        author, guild = interaction.user.id, interaction.guild_id
        async def submit(i):
            if i.user.id != author or i.guild_id != guild or not i.permissions.manage_guild:
                raise ReceiptError('設定を開始した管理者だけが保存できます。')
            c = bot.vip.db.config(guild)
            if role.id not in c.get('tiers', []):
                raise ReceiptError('VIPロールが登録から外れています。')
            descriptions = dict(c.get('benefits', {}))
            text = field.value.strip()
            if text: descriptions[str(role.id)] = text
            else: descriptions.pop(str(role.id), None)
            bot.vip.db.set_config(guild, benefits=descriptions)
            await i.response.send_message('特典を保存しました。料金表は約15秒以内に更新します。', ephemeral=True)
        async def on_error(i, error):
            await SafeView.on_error(modal, i, error, None)
        modal.on_submit = submit
        modal.on_error = on_error
        await interaction.response.send_modal(modal)

    @group.command(name='reception', description='VIP申請の受付中・受付停止中を切り替え')
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.choices(status=[app_commands.Choice(name='受付中', value='open'), app_commands.Choice(name='受付停止中', value='closed')])
    async def reception(interaction: discord.Interaction, status: app_commands.Choice[str]):
        await interaction.response.defer(ephemeral=True)
        async with bot.vip.lock:
            bot.vip.db.set_config(interaction.guild_id, accepting=status.value == 'open')
        await interaction.followup.send(('🟢 受付中' if status.value == 'open' else '🔴 受付停止中') + ' に変更しました。申請パネルは約15秒以内に更新します。既存チケットは引き続き対応できます。', ephemeral=True)

    bot.tree.add_command(group)
