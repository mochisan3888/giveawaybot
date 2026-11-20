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
from .donation_store import DonationStore

log = logging.getLogger('giveaway.donation')


def donation_embed(r):
    states = {'pending': '準備中', 'open': '受付済み', 'working': '対応中', 'completed': '対応完了', 'closed': 'クローズ'}
    e = discord.Embed(title=f"寄付チケット #{r['id']}", color=0xF4B942)
    e.add_field(name='申込者', value=f"<@{r['user']}>")
    e.add_field(name='寄付内容', value=r['content'], inline=False)
    if r.get('note'):
        e.add_field(name='補足・希望日時', value=r['note'], inline=False)
    if r.get('minecraft'):
        e.add_field(name='連携済みMinecraft ID', value=r['minecraft']['name'])
    e.add_field(name='状態', value=states[r['status']])
    e.add_field(name='担当者', value=f"<@{r['assignee']}>" if r.get('assignee') else '未担当')
    e.set_footer(text='ゲーム内での受け渡しはスタッフと相談してください。')
    return e


class DonationPanel(SafeView):
    def __init__(self, service):
        super().__init__(timeout=None)
        self.service = service

    @discord.ui.button(label='🎁 寄付チケットを開く', style=discord.ButtonStyle.success, custom_id='donation:open:v1')
    async def open_ticket(self, interaction, button):
        if not interaction.guild or not any(p['message'] == interaction.message.id for p in self.service.db.panels(interaction.guild_id)):
            raise ReceiptError('このパネルは使用できません。管理者に再設置を依頼してください。')
        existing = self.service.db.active(interaction.guild_id, interaction.user.id)
        if existing:
            return await interaction.response.send_message(self.service.existing_text(existing), ephemeral=True)
        self.service.configured(interaction.guild_id)
        await interaction.response.send_modal(DonationForm(self.service, interaction.guild_id))


class DonationForm(discord.ui.Modal, title='寄付の申し込み'):
    content = discord.ui.TextInput(label='寄付するもの・数量', placeholder='例：ゲーム内通貨10m、スケルトンスポナー5個', style=discord.TextStyle.paragraph, max_length=1000)
    note = discord.ui.TextInput(label='補足・希望日時（任意）', style=discord.TextStyle.paragraph, max_length=1000, required=False)

    def __init__(self, service, guild):
        super().__init__()
        self.service, self.guild = service, guild

    async def on_submit(self, interaction):
        if interaction.guild_id != self.guild:
            raise ReceiptError('元のサーバーで操作してください。')
        await interaction.response.defer(ephemeral=True)
        r = await self.service.request(interaction, self.content.value, self.note.value)
        await interaction.followup.send(self.service.existing_text(r), ephemeral=True)

    async def on_error(self, interaction, error):
        await SafeView.on_error(self, interaction, error, None)


class DonationControls(SafeView):
    def __init__(self, service, ticket):
        super().__init__(timeout=None)
        for action, label in [('start', '対応開始'), ('done', '対応完了'), ('close', '閉じる')]:
            button = discord.ui.Button(label=label, custom_id=f'donation:ticket:{ticket["id"]}:{action}',
                                       style=discord.ButtonStyle.secondary, disabled=ticket['status'] == 'closed')
            async def clicked(interaction, action=action):
                await interaction.response.defer(ephemeral=True)
                await service.action(interaction, ticket['id'], action)
                await interaction.followup.send('更新しました。', ephemeral=True)
            button.callback = clicked
            self.add_item(button)


class DonationSetup(SafeView):
    def __init__(self, service, author, category):
        super().__init__(timeout=300)
        self.service, self.author, self.category = service, author, category
        self.roles = []
        select = discord.ui.RoleSelect(placeholder='寄付対応スタッフ（複数可）', min_values=1, max_values=10)
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
        await interaction.edit_original_response(content='寄付の作成先・スタッフを保存しました。/donation panel でパネルを設置できます。', view=self)


class Donation:
    def __init__(self, bot):
        self.bot, self.db = bot, DonationStore(bot.store.db)
        self.lock = asyncio.Lock()
        self.registered = set()

    def configured(self, guild):
        config = self.db.config(guild)
        if not config.get('policy'):
            raise ReceiptError('寄付は未設定です。管理者が /donation setup を実行してください。')
        return config

    @staticmethod
    def existing_text(r):
        if r.get('channel'):
            return f"寄付チケット：<#{r['channel']}>\n追加の寄付内容もこのチケットに記入できます。"
        return '寄付チケットを準備中です。作成後、サーバー内に専用チャンネルが表示されます。'

    def register(self, r):
        if r['id'] not in self.registered:
            self.bot.add_view(DonationControls(self, r))
            self.registered.add(r['id'])

    async def start(self):
        self.bot.add_view(DonationPanel(self))
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
        message = await channel.send(embed=discord.Embed(title='🎁 寄付のご案内', color=0xF4B942,
            description='ゲーム内通貨やアイテムの寄付は、下のボタンから申し込めます。\n寄付内容・数量を入力すると、あなたとスタッフ専用のチケットが開きます。\n受け渡し方法はチケット内でご相談ください。'),
            view=DonationPanel(self), allowed_mentions=discord.AllowedMentions.none())
        self.db.save_panel(guild.id, channel.id, message.id, config['revision'])
        return message

    async def request(self, interaction, content, note):
        async with self.lock:
            r = self.db.active(interaction.guild_id, interaction.user.id)
            if r:
                return r
            config = self.configured(interaction.guild_id)
            await self.bot.fulfillment.validate_policy(interaction.guild, config['policy'])
            r = self.db.create(interaction.guild_id, interaction.user.id, content, note)
            self.register(r)
            try:
                await self.open_ticket(r)
            except discord.HTTPException:
                log.warning('Donation %s creation pending', r['id'])
            return r

    async def open_ticket(self, r):
        if r.get('channel'):
            return
        guild = self.bot.get_guild(r['guild'])
        if not guild:
            raise ReceiptError('サーバーが見つかりません。')
        category, roles = await self.bot.fulfillment.validate_policy(guild, r['policy'])
        member = await guild.fetch_member(r['user'])
        marker = f"donation-ticket:{self.bot.user.id}:{r['id']}"
        channels = await guild.fetch_channels() if r.get('creating') else []
        channel = next((c for c in channels if isinstance(c, discord.TextChannel) and c.topic == marker), None)
        if not channel:
            r['creating'] = True
            self.db.save(r)
            channel = await guild.create_text_channel(f"寄付-{r['id']}", category=category, topic=marker,
                overwrites=ticket_overwrites(guild, member, roles, guild.me), reason=f"寄付 #{r['id']}")
        r.update(channel=channel.id, status='open', minecraft=self.bot.fulfillment.db.link(r['user'], r['guild']), dirty=True)
        self.db.save(r)
        await self.update_ticket(r)

    async def update_ticket(self, r):
        channel = self.bot.get_channel(r['channel']) or await self.bot.fetch_channel(r['channel'])
        view, embed = DonationControls(self, r), donation_embed(r)
        if not r.get('control'):
            # Discord nonce deduplication is short-lived. Never blindly resend
            # an old uncertain notification; /donation manage remains available.
            if r.get('sending_at') and time.time() - r['sending_at'] > 120:
                r.update(dirty=False, notification_uncertain=True)
                self.db.save(r)
                return
            if not r.get('sending_at'):
                r['sending_at'] = time.time()
                self.db.save(r)
            result = await self.bot.http.request(Route('POST', '/channels/{channel_id}/messages', channel_id=channel.id), json={
                'content': f"<@{r['user']}> " + ' '.join(f'<@&{rid}>' for rid in r['policy']['roles']) + '\n寄付の依頼です。',
                'allowed_mentions': {'parse': [], 'users': [str(r['user'])], 'roles': [str(x) for x in r['policy']['roles']]},
                'embeds': [embed.to_dict()], 'components': view.to_components(),
                'nonce': hashlib.sha256(f"donation:{r['guild']}:{r['id']}".encode()).hexdigest()[:24], 'enforce_nonce': True})
            r['control'] = int(result['id'])
        else:
            await channel.get_partial_message(r['control']).edit(embed=embed, view=view)
        r['dirty'] = False
        self.db.save(r)

    async def action(self, interaction, rid, action):
        async with self.lock:
            r = self.db.get(rid)
            if not r or not interaction.guild or r['guild'] != interaction.guild_id or r['channel'] != interaction.channel_id:
                raise ReceiptError('対象の寄付チケット内で操作してください。')
            member = await interaction.guild.fetch_member(interaction.user.id)
            if not any(role.id in r['policy']['roles'] for role in member.roles):
                raise ReceiptError('指定された寄付スタッフだけが操作できます。')
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
                await interaction.channel.edit(overwrites=ticket_overwrites(guild, owner, roles, guild.me, closed=True), reason='寄付チケットをクローズ')
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
            for r in self.db.pending():
                if restricted(r["guild"]):continue
                try:
                    self.register(r)
                    if not r.get('channel'):
                        await self.open_ticket(r)
                    if r.get('channel') and r.get('dirty'):
                        await self.update_ticket(r)
                except Exception as error:
                    log.warning('Donation %s pending: %s', r['id'], type(error).__name__)

    @worker.before_loop
    async def before_worker(self):
        await self.bot.wait_until_ready()


def register_donation(bot):
    group = app_commands.Group(name='donation', description='寄付チケットの設定・管理', guild_only=True)

    @group.command(name='setup', description='寄付チケットのカテゴリとスタッフを設定')
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setup(interaction: discord.Interaction, category: discord.CategoryChannel):
        await interaction.response.send_message(f'作成先：{category.mention}\n寄付対応スタッフを選んで保存してください。',
            view=DonationSetup(bot.donation, interaction.user.id, category.id), ephemeral=True)

    @group.command(name='panel', description='寄付申し込みのパネルを設置')
    @app_commands.checks.has_permissions(manage_guild=True)
    async def panel(interaction: discord.Interaction, channel: discord.TextChannel):
        await interaction.response.defer(ephemeral=True)
        message = await bot.donation.post_panel(interaction.guild, channel)
        await interaction.followup.send(f'寄付パネルを設置しました：{message.jump_url}', ephemeral=True)

    @group.command(name='manage', description='寄付チケットの対応開始・完了・クローズ')
    @app_commands.choices(action=[app_commands.Choice(name=label, value=value) for value, label in [('start', '対応開始'), ('done', '対応完了'), ('close', '閉じる')]])
    async def manage(interaction: discord.Interaction, action: app_commands.Choice[str]):
        await interaction.response.defer(ephemeral=True)
        r = bot.donation.db.by_channel(interaction.guild_id, interaction.channel_id)
        if not r:
            raise ReceiptError('寄付チケット内で実行してください。')
        await bot.donation.action(interaction, r['id'], action.value)
        await interaction.followup.send('更新しました。', ephemeral=True)
    bot.tree.add_command(group)
