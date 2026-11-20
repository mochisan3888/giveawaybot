from .access_policy import restricted, giveaway_runtime, auto_auth_paused
import asyncio
import hashlib
import hmac
import logging
import os
import re
import time

import aiohttp
from aiohttp import web
import discord
from discord import app_commands
from discord.ext import tasks
from discord.http import Route

from .redemptions import Redemptions, ReceiptError
from .bedrock_links import BedrockLinks

log = logging.getLogger('giveaway.receipts')


def minecraft_nickname(name, original):
    # Count UTF-16 units conservatively so emoji names fit Discord's limit too.
    short = ''
    for char in name:
        if len((short + char).encode('utf-16-le')) // 2 > 28:
            short += '…'
            break
        short += char
    prefix = short + ' / '
    room = 32 - len(prefix.encode('utf-16-le')) // 2
    tail = ''
    for char in original:
        size = len(char.encode('utf-16-le')) // 2
        if room < size:
            break
        tail += char
        room -= size
    return prefix + tail


def link_status(linked):
    states = {'done': 'ニックネーム変更済み', 'forbidden': 'ニックネーム変更待ち：BOTの「ニックネームの管理」権限とロール順位を確認してください（サーバー所有者は変更できません）。',
              'missing': 'ニックネーム変更待ち：サーバーへの参加状態を確認してください。'}
    edition = '統合版' if linked['uuid'].startswith('bedrock') else 'Java版'
    return f"このサーバーで連携済み：{linked['name']}（{edition}）\n" + states.get(linked['nickname_state'], 'ニックネーム変更待ち（自動再試行します）')


def ticket_overwrites(guild, winner, roles, bot_member, closed=False):
    overwrites = {guild.default_role: discord.PermissionOverwrite(view_channel=False)}
    overwrites[bot_member] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_channels=True)
    for role in roles:
        overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
    # Member overwrite takes priority even if the winner also holds a staff role.
    overwrites[winner] = discord.PermissionOverwrite(view_channel=True, send_messages=not closed,
        read_message_history=True, add_reactions=not closed, create_public_threads=False,
        create_private_threads=False, send_messages_in_threads=False)
    from .ticket_bridge import add_peer
    return add_peer(guild, overwrites)


def ticket_embed(receipt):
    labels = {'open': '受付済み', 'working': '対応中', 'completed': '対応完了', 'closed': 'クローズ'}
    embed = discord.Embed(title=f"景品受け取り #{receipt['id']}", color=0x7C5CFC)
    embed.add_field(name='景品', value=receipt['prize'], inline=False)
    embed.add_field(name='当選者', value=f"<@{receipt['user']}>")
    embed.add_field(name='状態', value=labels.get(receipt['status'], receipt['status']))
    embed.add_field(name='担当者', value=f"<@{receipt['assignee']}>" if receipt.get('assignee') else '未担当')
    linked = receipt.get('minecraft')
    embed.add_field(name='Minecraft ID（連携確認時）', value=linked['name'] if linked else '連携不要', inline=False)
    if linked:
        embed.add_field(name='Minecraft識別子', value=linked['uuid'], inline=False)
    embed.set_footer(text='指定スタッフが対応開始・対応完了・閉じるを操作できます。')
    return embed


class SafeView(discord.ui.View):
    async def on_error(self, interaction, error, item):
        log.error('Receipt UI error: %s', type(error).__name__)
        text = str(error) if isinstance(error, ReceiptError) else '処理できませんでした。少し待って再試行してください。'
        try:
            if interaction.response.is_done():
                await interaction.followup.send(text, ephemeral=True)
            else:
                await interaction.response.send_message(text, ephemeral=True)
        except discord.HTTPException:
            pass


class MinecraftPanelView(SafeView):
    def __init__(self, service):
        super().__init__(timeout=None)
        self.service = service

    @discord.ui.button(label='Minecraft連携', style=discord.ButtonStyle.success,
                       custom_id='minecraft:link:v1')
    async def link(self, interaction, button):
        await self.service.begin_link(interaction)


class EditionChoice(SafeView):
    def __init__(self, service, user, guild, receipt=None):
        super().__init__(timeout=600)
        self.service, self.user, self.guild, self.receipt = service, user, guild, receipt

    def check(self, interaction):
        if interaction.user.id != self.user or (interaction.guild_id and interaction.guild_id != self.guild):
            raise ReceiptError('認証を始めた本人だけが操作できます。')

    @discord.ui.button(label='Java版', style=discord.ButtonStyle.primary)
    async def java(self, interaction, button):
        self.check(interaction)
        if auto_auth_paused():raise ReceiptError("Minecraftの新規自動認証は一時停止中です。")
        self.service.require_verifier()
        await interaction.response.send_modal(MinecraftModal(self.service, self.receipt, self.guild))

    @discord.ui.button(label='統合版', style=discord.ButtonStyle.success)
    async def bedrock(self, interaction, button):
        self.check(interaction)
        await self.service.begin_bedrock(interaction, self.guild, self.receipt)


class BedrockCodeModal(discord.ui.Modal, title='統合版の確認コード'):
    code = discord.ui.TextInput(label='ゲームの接続画面に出た確認コード', min_length=24, max_length=32, placeholder='24文字の英数字')
    def __init__(self, service, guild, receipt=None):
        super().__init__()
        self.service, self.guild, self.receipt = service, guild, receipt

    async def on_submit(self, interaction):
        await self.service.complete_bedrock(interaction, self.guild, str(self.code), self.receipt)

    async def on_error(self, interaction, error):
        await SafeView.on_error(self, interaction, error, None)


class BedrockCodeView(SafeView):
    def __init__(self, service, user, guild, receipt=None):
        super().__init__(timeout=600)
        self.service, self.user, self.guild, self.receipt = service, user, guild, receipt

    @discord.ui.button(label='確認コードを入力', style=discord.ButtonStyle.success)
    async def code(self, interaction, button):
        if interaction.user.id != self.user:
            raise ReceiptError('認証を始めた本人だけが操作できます。')
        await interaction.response.send_modal(BedrockCodeModal(self.service, self.guild, self.receipt))


class ClaimView(SafeView):
    def __init__(self, service, rid):
        super().__init__(timeout=None)
        self.service, self.rid = service, rid
        button = discord.ui.Button(label='🎁 受け取る', style=discord.ButtonStyle.success, custom_id=f'receipt:{rid}:claim')
        button.callback = self.claim
        self.add_item(button)

    async def claim(self, interaction):
        await self.service.begin(interaction, self.rid)


class ReceiptSelect(SafeView):
    def __init__(self, service, receipts):
        super().__init__(timeout=180)
        select = discord.ui.Select(placeholder='受け取る当選を選択', options=[
            discord.SelectOption(label=f"受け取り #{r['id']}（抽選{r['round']}回目）", value=str(r['id'])) for r in receipts[-25:]])
        async def selected(interaction):
            await service.begin(interaction, int(select.values[0]))
        select.callback = selected
        self.add_item(select)


class TicketView(SafeView):
    def __init__(self, service, rid):
        super().__init__(timeout=None)
        for action, label, style in [('start', '対応開始', discord.ButtonStyle.primary), ('done', '対応完了', discord.ButtonStyle.success), ('close', '閉じる', discord.ButtonStyle.secondary)]:
            button = discord.ui.Button(label=label, style=style, custom_id=f'receipt:{rid}:{action}')
            async def callback(interaction, action=action):
                await interaction.response.defer(ephemeral=True)
                await service.action(interaction, rid, action)
                await interaction.followup.send('更新しました。', ephemeral=True)
            button.callback = callback
            self.add_item(button)


class MinecraftModal(discord.ui.Modal, title='Java版 Minecraft連携'):
    username = discord.ui.TextInput(label='MinecraftのプレイヤーID', placeholder='例：Steve（メールアドレスではありません）', min_length=3, max_length=16)

    def __init__(self, service, rid=None, guild=None):
        super().__init__()
        self.service, self.rid, self.guild = service, rid, guild

    async def on_submit(self, interaction):
        if auto_auth_paused():
            return await interaction.response.send_message("Minecraftの新規自動認証は一時停止中です。", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        name = str(self.username).strip()
        if not re.fullmatch(r'[A-Za-z0-9_]{3,16}', name):
            return await interaction.followup.send('IDは英数字と _ の3～16文字で入力してください。', ephemeral=True)
        try:
            receipt = self.service.db.get(self.rid) if self.rid else None
            guild_id = receipt['guild'] if receipt else self.guild
            if not guild_id or (interaction.guild_id and interaction.guild_id != guild_id):
                raise ReceiptError('認証したいサーバーで /minecraft link を実行してください。')
            guild = self.service.bot.get_guild(guild_id)
            if not guild:
                raise ReceiptError('認証元のサーバーが見つかりません。')
            member = await guild.fetch_member(interaction.user.id)
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12)) as session:
                async with session.get(f'https://api.minecraftservices.com/minecraft/profile/lookup/name/{name}') as response:
                    if response.status in (204, 404):
                        raise ReceiptError('そのMinecraft IDが見つかりません。綴りを確認してください。')
                    response.raise_for_status()
                    profile = await response.json()
            token, expires = self.service.db.challenge(interaction.user.id, profile['id'].replace('-', '').lower(), profile['name'], self.rid, guild=guild_id, original_name=member.display_name)
            address = f"{token}.{self.service.suffix}:{self.service.port}"
            await interaction.followup.send(
                f'Java版の「マルチプレイ」→「ダイレクト接続」に次のアドレスを入力してください。\n'
                f'```\n{address}\n```\n有効期限：<t:{int(expires)}:R>\n'
                '入力したIDの正規アカウントで接続してください。このアドレスは他人に渡さないでください。\n'
                '認証完了後、このサーバーのニックネームを「Minecraft ID / 元の名前」に変更します。\n'
                + ('連携後、期限内であれば受け取りチケットを自動作成します。' if self.rid else 'このサーバーで連携すると、以降の受け取りでID入力が不要になります。'), ephemeral=True)
        except ReceiptError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
        except (aiohttp.ClientError, TimeoutError, KeyError):
            await interaction.followup.send('Minecraftの認証サービスに接続できませんでした。後でもう一度お試しください。', ephemeral=True)

    async def on_error(self, interaction, error):
        log.error('Minecraft form error: %s', type(error).__name__)
        if interaction.response.is_done():
            await interaction.followup.send('処理できませんでした。もう一度お試しください。', ephemeral=True)


class ProfileView(SafeView):
    def __init__(self, service, author, guild, channel, policy):
        super().__init__(timeout=300)
        self.service, self.author, self.guild, self.channel = service, author, guild, channel
        self.policy = policy
        self.roles = []
        picker = discord.ui.RoleSelect(placeholder='対応スタッフのロール（複数可）', min_values=1, max_values=10, custom_id='receipt-config:'+os.urandom(12).hex())
        async def selected(interaction):
            if interaction.user.id != self.author:
                return await interaction.response.send_message('設定を開始した人だけが操作できます。', ephemeral=True)
            self.roles = [role.id for role in picker.values]
            await interaction.response.defer()
        picker.callback = selected
        self.add_item(picker)
        button = discord.ui.Button(label='設定を保存', style=discord.ButtonStyle.success, custom_id='receipt-config:'+os.urandom(12).hex())
        button.callback = self.save
        self.add_item(button)

    async def save(self, interaction):
        if interaction.user.id != self.author or not interaction.permissions.manage_guild:
            raise ReceiptError('この設定は開始した管理者だけが保存できます。')
        await interaction.response.defer(ephemeral=True)
        policy = dict(self.policy, roles=self.roles)
        await self.service.validate_policy(interaction.guild, policy)
        self.service.db.set_profile(self.guild, self.channel, policy)
        for child in self.children:
            child.disabled = True
        await interaction.edit_original_response(content='保存しました。次にこのチャンネルで作るGiveawayから適用します。', view=self)


class Fulfillment:
    def __init__(self, bot):
        self.bot = bot
        self.db = Redemptions(bot.store.db)
        self.bedrock = BedrockLinks(self.db)
        self.lock = asyncio.Lock()
        self.suffix = os.getenv('MC_VERIFY_DOMAIN', '').strip().lower().strip('.')
        self.port = int(os.getenv('MC_VERIFY_PORT', '25565'))
        self.secret = os.getenv('VERIFY_SHARED_SECRET', '')
        self.runner = None
        self.registered = set()

    def register(self, receipt):
        if receipt['id'] in self.registered:
            return
        self.bot.add_view(ClaimView(self, receipt['id']))
        self.bot.add_view(TicketView(self, receipt['id']))
        self.registered.add(receipt['id'])

    async def start(self):
        self.bot.add_view(MinecraftPanelView(self))
        for r in self.db.all():
            self.register(r)
        if self.secret:
            if len(self.secret) < 32:
                raise RuntimeError('VERIFY_SHARED_SECRET must be at least 32 characters')
            app = web.Application(client_max_size=4096)
            app.router.add_post('/verify', self.verified)
            app.router.add_post('/bedrock/issue', self.bedrock_issue)
            app.router.add_get('/health', lambda request: web.json_response({'ready': self.bot.is_ready()}))
            self.runner = web.AppRunner(app, access_log=None)
            await self.runner.setup()
            await web.TCPSite(self.runner, os.getenv('VERIFY_BIND', '0.0.0.0'), 8080).start()
        self.worker.start()

    async def begin_link(self, interaction):
        if not interaction.guild_id:
            raise ReceiptError('認証したいサーバーで /minecraft link を実行してください。')
        linked = self.db.link(interaction.user.id, interaction.guild_id)
        if linked:
            await interaction.response.send_message(
                link_status(linked), ephemeral=True)
            return
        if auto_auth_paused():raise ReceiptError('Minecraftの新規自動認証は一時停止中です。既存の連携は利用できます。')
        await interaction.response.send_message('認証するエディションを選んでください。', view=EditionChoice(self, interaction.user.id, interaction.guild_id), ephemeral=True)

    async def begin_bedrock(self, interaction, guild, receipt=None):
        if not guild or (interaction.guild_id and interaction.guild_id != guild):
            raise ReceiptError('認証したいサーバーで操作してください。')
        linked = self.db.link(interaction.user.id, guild)
        if linked:
            return await interaction.response.send_message(link_status(linked), ephemeral=True)
        if auto_auth_paused():raise ReceiptError('Minecraftの新規自動認証は一時停止中です。')
        address = os.getenv('BEDROCK_VERIFY_HOST', self.suffix)
        if not address or not self.secret:
            raise ReceiptError('統合版認証サーバーは準備中です。')
        port = os.getenv('BEDROCK_VERIFY_PORT', '19132')
        if receipt:
            r = self.db.get(receipt)
            self.db.authorize(r, interaction.user.id)
            if r['guild'] != guild:raise ReceiptError('認証元が違います。')
        await interaction.response.send_message(
            f'統合版で「サーバーを追加」から接続してください。\nアドレス：`{address}`\nポート：`{port}`\n'
            'Xboxアカウントでサインインして接続すると、接続終了画面に確認コードが表示されます。\n'
            '下のボタンでコードを入力してください（10分・1回限り）。コードは他人に渡さないでください。\n'
            '認証後はこのサーバーでID入力不要になり、ニックネームを変更します。',
            view=BedrockCodeView(self, interaction.user.id, guild, receipt), ephemeral=True)

    async def complete_bedrock(self, interaction, guild_id, code, receipt=None):
        if auto_auth_paused():raise ReceiptError("Minecraftの新規自動認証は一時停止中です。")
        if interaction.guild_id and interaction.guild_id != guild_id:
            raise ReceiptError('元のサーバーで操作してください。')
        await interaction.response.defer(ephemeral=True)
        guild = self.bot.get_guild(guild_id)
        if not guild:raise ReceiptError('認証元のサーバーが見つかりません。')
        member = await guild.fetch_member(interaction.user.id)
        linked = self.bedrock.consume(code, guild_id, member.id, member.display_name, receipt)
        await interaction.followup.send('統合版の連携が完了しました！\n' + link_status(linked) + ('\n受け取り期限内であればチケットを自動作成します。' if receipt else ''), ephemeral=True)

    async def bedrock_issue(self, request):
        if auto_auth_paused():
            return web.json_response({"ok": False, "message": "Automatic Minecraft authentication is temporarily paused."}, status=503)
        if not self.secret or not hmac.compare_digest(request.headers.get('Authorization', ''), 'Bearer ' + self.secret):
            raise web.HTTPUnauthorized()
        try:
            body = await request.json()
            code = self.bedrock.issue(body['xuid'], body['name'])
            return web.json_response({'ok': True, 'code': code})
        except (ReceiptError, KeyError, ValueError, TypeError):
            return web.json_response({'ok': False, 'message': '認証プロフィールが不正です。'}, status=400)

    async def stop(self):
        self.worker.cancel()
        if self.runner:
            await self.runner.cleanup()

    async def verified(self, request):
        if auto_auth_paused():
            return web.json_response({"ok": False, "message": "Automatic Minecraft authentication is temporarily paused."}, status=503)
        if not hmac.compare_digest(request.headers.get('Authorization', ''), 'Bearer ' + self.secret):
            raise web.HTTPUnauthorized()
        try:
            body = await request.json()
            token, uuid, name = body['token'], body['uuid'], body['name']
            if not isinstance(token, str) or not re.fullmatch('[0-9a-f]{32}', token):
                raise ReceiptError('無効なアドレスです。')
            if not isinstance(uuid, str) or not re.fullmatch('[0-9a-f]{32}', uuid):
                raise ReceiptError('認証プロフィールが不正です。')
            row=self.db.db.execute('SELECT guild FROM guild_link_challenges WHERE token_hash=?',(hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
            if row and restricted(row[0]):raise ReceiptError('このサーバーではMinecraft認証を停止しています。')
            uid, rid = self.db.verify(token, uuid, name)
            return web.json_response({'ok': True, 'message': 'Minecraft連携が完了しました！Discordに戻ってください。'})
        except (ReceiptError, KeyError, ValueError, TypeError) as exc:
            text = str(exc) if isinstance(exc, ReceiptError) else '認証データが不正です。'
            return web.json_response({'ok': False, 'message': text}, status=400)

    def require_verifier(self):
        if not self.suffix or not self.secret:
            raise ReceiptError('Minecraft本人確認サーバーは準備中です。スタッフにお問い合わせください。')

    async def begin(self, interaction, rid):
        r = self.db.get(rid)
        self.db.authorize(r, interaction.user.id)
        if r.get('channel'):
            return await interaction.response.send_message(f"チケット：<#{r['channel']}>", ephemeral=True)
        if r['policy']['minecraft'] and not self.db.link(interaction.user.id, r['guild']):
            if auto_auth_paused():raise ReceiptError('Minecraftの新規認証は一時停止中です。スタッフにお問い合わせください。')
            return await interaction.response.send_message('認証するエディションを選んでください。', view=EditionChoice(self, interaction.user.id, r['guild'], rid), ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        async with self.lock:
            r = self.db.get(rid)
            self.db.authorize(r, interaction.user.id)
            r['requested'] = True
            self.db.save(r)
            await self.open_ticket(r)
        await interaction.followup.send(f"チケットを開きました：<#{r['channel']}>", ephemeral=True)

    async def validate_policy(self, guild, policy):
        if not policy['roles'] or guild.id in policy['roles']:
            raise ReceiptError('@everyone 以外のスタッフロールを選んでください。')
        roles = [guild.get_role(rid) for rid in policy['roles']]
        if not all(roles):
            raise ReceiptError('スタッフロールが見つかりません。設定をやり直してください。')
        category = guild.get_channel(policy['category'])
        if not isinstance(category, discord.CategoryChannel):
            raise ReceiptError('チケット作成先カテゴリが見つかりません。')
        perms = category.permissions_for(guild.me)
        if not all(getattr(perms, p) for p in ['view_channel', 'send_messages', 'embed_links', 'read_message_history', 'manage_channels', 'manage_roles']):
            raise ReceiptError('BOTにカテゴリの閲覧・送信・埋め込み・履歴閲覧・チャンネル管理・ロール管理権限が必要です。')
        if not perms.mention_everyone and any(not role.mentionable for role in roles):
            raise ReceiptError('スタッフへの通知のため、スタッフロールをメンション可能にするか、BOTに全ロールのメンション権限を付けてください。')
        if policy['minecraft']:
            self.require_verifier()
        return category, roles

    async def send_once(self, r, key, channel_id, payload):
        state = r.setdefault('deliveries', {}).setdefault(key, {})
        if state.get('id'):
            return state['id']
        if state.get('at') and time.time() - state['at'] > 120:
            state['uncertain'] = True
            self.db.save(r)
            return None
        if not state.get('at'):
            state['at'] = time.time()
            self.db.save(r)
        nonce = hashlib.sha256(f"receipt:{r['guild']}:{r['id']}:{key}".encode()).hexdigest()[:24]
        result = await self.bot.http.request(Route('POST', '/channels/{channel_id}/messages', channel_id=channel_id),
            json=dict(payload, nonce=nonce, enforce_nonce=True))
        state['id'] = result['id']
        self.db.save(r)
        return result['id']

    async def deliver_dm(self, r):
        if r['dm'] != 'pending':
            return
        if r['status'] == 'expired':
            r['dm'] = 'expired'
            self.db.save(r)
            return
        try:
            user = self.bot.get_user(r['user']) or await self.bot.fetch_user(r['user'])
            dm = await user.create_dm()
            text = f"🎉 おめでとうございます！「{discord.utils.escape_markdown(r['prize'])}」に当選しました！"
            if r['policy']:
                text += f"\n受け取り期限：<t:{int(r['deadline'])}:F>\n「受け取る」を押して、期限内にチケットを開いてください。"
                if r['policy']['minecraft']:
                    text += '\nMinecraft連携が必要です。連携済みならID入力は不要です。'
            else:
                text += '\nこの募集は自動チケット未設定です。募集元のスタッフへお問い合わせください。'
            text += f"\nhttps://discord.com/channels/{r['guild']}/{r['source_channel']}/{r['source_message']}"
            sent = await self.send_once(r, 'dm', dm.id, {'content': text, 'allowed_mentions': {'parse': []},
                'components': ClaimView(self, r['id']).to_components() if r['policy'] else []})
            r['dm'] = 'sent' if sent else 'uncertain'
            self.db.save(r)
        except discord.Forbidden:
            r['dm'] = 'blocked'
            self.db.save(r)

    async def open_ticket(self, r):
        if r.get('channel'):
            return
        guild = self.bot.get_guild(r['guild'])
        if not guild:
            raise ReceiptError('BOTがサーバーに参加していません。')
        category, roles = await self.validate_policy(guild, r['policy'])
        member = await guild.fetch_member(r['user'])
        linked = self.db.link(r['user'], r['guild'])
        if r['policy']['minecraft'] and not linked:
            return
        marker = f"giveaway-receipt:{self.bot.user.id}:{r['id']}"
        channels = await guild.fetch_channels() if r.get('creating') else []
        existing = next((c for c in channels if isinstance(c, discord.TextChannel) and c.topic == marker), None)
        if existing:
            if existing.created_at.timestamp() > r['deadline']:
                await existing.delete(reason='受け取り期限後の作成を取り消し')
                r.update(status='expired', creating=False)
                self.db.save(r)
                raise ReceiptError('チケット作成前に受け取り期限が過ぎました。')
            channel = existing
        else:
            if time.time() >= r['deadline']:
                r.update(status='expired', creating=False)
                self.db.save(r)
            self.db.authorize(r, r['user'])
            r['creating'] = True
            self.db.save(r)
            channel = await guild.create_text_channel(f"受取-{r['id']}", category=category, topic=marker,
                overwrites=ticket_overwrites(guild, member, roles, guild.me), reason=f"Giveaway #{r['game']} 当選受取")
            if channel.created_at.timestamp() > r['deadline']:
                await channel.delete(reason='受け取り期限後の作成を取り消し')
                r.update(status='expired', creating=False)
                self.db.save(r)
                raise ReceiptError('チケット作成前に受け取り期限が過ぎました。')
        r.update(channel=channel.id, status='open', opened=channel.created_at.timestamp(), minecraft=linked, panel_dirty=True)
        self.db.save(r)
        try:
            await self.update_panel(r)
        except discord.HTTPException:
            log.warning('Receipt %s panel pending; channel is created', r['id'])

    async def update_panel(self, r):
        channel = self.bot.get_channel(r['channel']) or await self.bot.fetch_channel(r['channel'])
        view = TicketView(self, r['id'])
        if r['status'] == 'closed':
            for button in view.children:
                button.disabled = True
        embed = ticket_embed(r)
        if not r.get('control'):
            content = f"<@{r['user']}> " + ' '.join(f'<@&{role}>' for role in r['policy']['roles']) + '\n受け取りチケットを作成しました。'
            r['control'] = await self.send_once(r, 'panel', channel.id, {'content': content,
                'allowed_mentions': {'parse': [], 'roles': [str(x) for x in r['policy']['roles']], 'users': [str(r['user'])]},
                'embeds': [embed.to_dict()], 'components': view.to_components()})
        else:
            await channel.get_partial_message(int(r['control'])).edit(embed=embed, view=view)
        r['panel_dirty'] = False
        self.db.save(r)

    async def action(self, interaction, rid, action):
        async with self.lock:
            r = self.db.get(rid)
            if not r or not interaction.guild or interaction.guild.id != r['guild'] or interaction.channel_id != r['channel']:
                raise ReceiptError('このチケット内で操作してください。')
            member = await interaction.guild.fetch_member(interaction.user.id)
            if not any(role.id in r['policy']['roles'] for role in member.roles):
                raise ReceiptError('指定スタッフロールを持つ人だけが操作できます。')
            if r['status'] == 'closed':
                raise ReceiptError('このチケットは閉じています。')
            if action == 'start':
                if r['status'] != 'open':
                    raise ReceiptError('すでに対応中、または対応完了しています。')
                r.update(status='working', assignee=member.id)
            elif action == 'done':
                if r['status'] not in ('open', 'working'):
                    raise ReceiptError('すでに対応完了しています。')
                r.update(status='completed', completed_by=member.id)
            elif action == 'close':
                guild = interaction.guild
                try:
                    winner = await guild.fetch_member(r['user'])
                except discord.NotFound:
                    winner = discord.Object(id=r['user'], type=discord.Member)
                roles = [guild.get_role(x) for x in r['policy']['roles']]
                if not all(roles):
                    raise ReceiptError('スタッフロールが削除されています。管理者がチャンネル権限を確認してください。')
                await interaction.channel.edit(overwrites=ticket_overwrites(guild, winner, roles, guild.me, closed=True), reason='受け取りチケットをクローズ')
                r.update(status='closed', closed_by=member.id)
            else:
                raise ReceiptError('不明な操作です。')
            r.setdefault('history', []).append(dict(action=action, by=member.id, at=time.time()))
            r['panel_dirty'] = True
            self.db.save(r)
            await self.update_panel(r)

    async def update_nicknames(self):
        for job in self.db.nickname_jobs():
            guild_id, uid = job['guild'], job['user']
            if restricted(guild_id):continue
            try:
                guild = self.bot.get_guild(guild_id)
                if not guild:
                    self.db.nickname_result(guild_id, uid, 'missing', time.time() + 300)
                    continue
                member = await guild.fetch_member(uid)
                desired = minecraft_nickname(job['name'], job['original_name'])
                if member.nick != desired:
                    await member.edit(nick=desired, reason='このサーバーでMinecraft認証が完了')
                self.db.nickname_result(guild_id, uid, 'done')
            except discord.Forbidden:
                self.db.nickname_result(guild_id, uid, 'forbidden', time.time() + 300)
            except discord.NotFound:
                self.db.nickname_result(guild_id, uid, 'missing', time.time() + 300)
            except Exception as error:
                self.db.nickname_result(guild_id, uid, 'retry', time.time() + 60)
                log.warning('Minecraft nickname update pending: %s', type(error).__name__)

    @tasks.loop(seconds=10)
    async def worker(self):
        if not giveaway_runtime():await self.update_nicknames()
        async with self.lock:
            for game in self.bot.store.games():
                if restricted(game["guild"]):continue
                self.db.seed(game)
            for r in self.db.all():
                if restricted(r["guild"]):continue
                self.register(r)
                try:
                    # Recover a channel created before a crash even if the deadline has since passed.
                    if not r.get('channel') and r.get('creating') and r['status'] != 'expired':
                        await self.open_ticket(r)
                    if r['status'] == 'pending' and time.time() >= r['deadline']:
                        r['status'] = 'expired'
                        self.db.save(r)
                    await self.deliver_dm(r)
                    if r['status'] == 'pending' and r.get('requested'):
                        await self.open_ticket(r)
                    if r.get('channel') and r.get('panel_dirty'):
                        await self.update_panel(r)
                except Exception as exc:
                    log.warning('Receipt %s processing failed: %s', r['id'], type(exc).__name__)

    @worker.before_loop
    async def before_worker(self):
        await self.bot.wait_until_ready()


def register_commands(bot, giveaway_group):
    @giveaway_group.command(name='receipt_config', description='募集チャンネルの受け取り期限・連携・スタッフを設定')
    @app_commands.checks.has_permissions(manage_guild=True)
    async def receipt_config(interaction: discord.Interaction, channel: discord.TextChannel,
                             category: discord.CategoryChannel, deadline_minutes: app_commands.Range[int, 1, 525600], minecraft_required: bool):
        service = bot.fulfillment
        policy = dict(category=category.id, minutes=deadline_minutes, minecraft=minecraft_required)
        if minecraft_required:
            service.require_verifier()
        await interaction.response.send_message(
            f'{channel.mention} の設定\n受け取り期限：当選から{deadline_minutes}分\n'
            f'Minecraft連携：{"必須" if minecraft_required else "不要"}\n作成先：{category.mention}\n'
            'スタッフロールを選び、「設定を保存」を押してください。',
            view=ProfileView(service, interaction.user.id, interaction.guild_id, channel.id, policy), ephemeral=True)

    @giveaway_group.command(name='receipt_settings', description='この募集チャンネルの受け取り設定を確認')
    @app_commands.checks.has_permissions(manage_guild=True)
    async def receipt_settings(interaction: discord.Interaction, channel: discord.TextChannel):
        p = bot.fulfillment.db.profile(interaction.guild_id, channel.id)
        if not p:
            return await interaction.response.send_message('未設定です。/giveaway receipt_config で設定してください。', ephemeral=True)
        await interaction.response.send_message(
            f"期限：{p['minutes']}分\nMinecraft連携：{'必須' if p['minecraft'] else '不要'}\n"
            f"カテゴリ：<#{p['category']}>\nスタッフ：" + ' '.join(f'<@&{r}>' for r in p['roles']), ephemeral=True)

    minecraft = app_commands.Group(name='minecraft', description='Java版Minecraftの本人確認・連携')

    @minecraft.command(name='link', description='専用アドレスにJava版で接続してMinecraftを連携')
    async def link(interaction: discord.Interaction):
        await bot.fulfillment.begin_link(interaction)

    @minecraft.command(name='panel', description='Minecraft認証パネルを指定チャンネルに設置')
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def panel(interaction: discord.Interaction, channel: discord.TextChannel):
        bot.fulfillment.require_verifier()
        if channel.guild.id != interaction.guild_id:
            raise ReceiptError('このサーバー内のチャンネルを選択してください。')
        permissions = channel.permissions_for(interaction.guild.me)
        if not all((permissions.view_channel, permissions.send_messages, permissions.embed_links)):
            raise ReceiptError('設置先でBOTに「チャンネルを見る」「メッセージを送信」「埋め込みリンク」の権限を付けてください。')
        await interaction.response.defer(ephemeral=True)
        embed = discord.Embed(title='Minecraftアカウント認証', color=0x57F287,
            description='下の「Minecraft連携」から **Java版／統合版** を選択できます。\n\n'
                        '**Java版**：ID入力 → 専用アドレスに接続\n'
                        '**統合版**：認証サーバーに接続 → 接続画面のコードをDiscordに入力\n\n'
                        '認証はこのDiscordサーバー専用です。完了後はチケットでのID入力が不要になり、\n'
                        'ニックネームを「Minecraft ID / 元の名前」に変更します。')
        message = await channel.send(embed=embed, view=MinecraftPanelView(bot.fulfillment),
                                     allowed_mentions=discord.AllowedMentions.none())
        await interaction.followup.send(f'認証パネルを設置しました：{message.jump_url}', ephemeral=True)

    @minecraft.command(name='status', description='Minecraft連携の状態を本人だけに表示')
    async def status(interaction: discord.Interaction):
        if not interaction.guild_id:
            raise ReceiptError('確認したいサーバーで実行してください。')
        linked = bot.fulfillment.db.link(interaction.user.id, interaction.guild_id)
        await interaction.response.send_message(
            link_status(linked) if linked else '未連携です。/minecraft link から連携できます。', ephemeral=True)
    @minecraft.command(name='bedrock', description='統合版の本人確認を開始')
    async def bedrock(interaction: discord.Interaction):
        await bot.fulfillment.begin_bedrock(interaction, interaction.guild_id)

    @minecraft.command(name='bedrock_code', description='統合版の接続画面に出た確認コードを入力')
    @app_commands.checks.cooldown(5, 60, key=lambda i: i.user.id)
    async def bedrock_code(interaction: discord.Interaction):
        if not interaction.guild_id:raise ReceiptError('認証したいサーバーで操作してください。')
        await interaction.response.send_modal(BedrockCodeModal(bot.fulfillment, interaction.guild_id))

    @minecraft.command(name='list', description='このサーバーのMinecraft認証済みユーザー一覧（管理者用）')
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(page='表示ページ（1ページ15人・新しい認証順）')
    async def list_verified(interaction: discord.Interaction, page: app_commands.Range[int, 1] = 1):
        await interaction.response.defer(ephemeral=True)
        total, pages, rows = bot.fulfillment.db.list_links(interaction.guild_id, page)
        embed = discord.Embed(title='Minecraft認証済み一覧', color=0x57F287,
            description=f'このサーバーの登録数：**{total}人**\n管理者による登録を含みます。退会後も連携未解除の記録は残ります。')
        for row in rows:
            edition = '統合版' if row['uuid'].startswith('bedrock') else 'Java版'
            embed.add_field(name=discord.utils.escape_markdown(row['name']),
                value=f"<@{row['user']}> · {edition}\nDiscord ID：{row['user']}\n登録：<t:{int(row['verified'])}:d>", inline=False)
        if not rows:
            embed.description += '\nまだ認証済みのユーザーはいません。'
        embed.set_footer(text=f'{page}/{pages}ページ ｜ /minecraft list page:番号 で切り替え')
        await interaction.followup.send(embed=embed, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @minecraft.command(name='admin_register', description='人力で本人確認済みの統合版アカウントを登録')
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(user='人力で本人確認済みのDiscordユーザー', gamertag='確認済みのXboxゲーマータグ（接頭辞を付けない）', xuid='確認済みXUIDが分かる場合のみ。省略時はゲーマータグで管理')
    async def admin_register(interaction: discord.Interaction, user: discord.Member, gamertag: str, xuid: str = None):
        if user.bot:raise ReceiptError('BOTは登録できません。')
        await interaction.response.defer(ephemeral=True)
        member = await interaction.guild.fetch_member(user.id)
        linked = bot.fulfillment.bedrock.register_manual(interaction.guild_id, member.id, gamertag, member.display_name, interaction.user.id, xuid)
        await interaction.followup.send(f'{member.mention} を管理者確認済みとして登録しました。\n' + link_status(linked) + ('\nXUID未登録のため、ゲーマータグ変更時は管理者で確認が必要です。' if not xuid else ''), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @minecraft.command(name='admin_link', description='管理者がJava版・統合版IDを登録または変更')
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.choices(edition=[app_commands.Choice(name='Java版', value='java'), app_commands.Choice(name='統合版', value='bedrock')])
    @app_commands.describe(name='Java IDまたは統合版ゲーマータグ', replace='既存の連携を変更する場合はtrue', xuid='統合版のXUIDが分かる場合のみ')
    async def admin_link(interaction: discord.Interaction, user: discord.Member, edition: app_commands.Choice[str], name: str, replace: bool = False, xuid: str = None):
        if user.bot:raise ReceiptError('BOTは登録できません。')
        await interaction.response.defer(ephemeral=True)
        name=name.strip()
        identity=xuid
        if edition.value == 'java':
            if xuid:raise ReceiptError('Java版ではXUIDを指定しないでください。')
            if not re.fullmatch(r'[A-Za-z0-9_]{3,16}',name):raise ReceiptError('Java IDは英数字・_の3～16文字です。')
            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12)) as session:
                    async with session.get(f'https://api.minecraftservices.com/minecraft/profile/lookup/name/{name}') as response:
                        if response.status in (204,404):raise ReceiptError('そのJava IDは見つかりません。')
                        response.raise_for_status()
                        profile=await response.json()
                identity=profile['id'].replace('-','').lower();name=profile['name']
            except (aiohttp.ClientError,TimeoutError,KeyError):
                raise ReceiptError('Minecraftプロフィールを確認できませんでした。既存の連携は変更していません。')
        member=await interaction.guild.fetch_member(user.id)
        linked=bot.fulfillment.bedrock.admin_link(interaction.guild_id,member.id,edition.value,name,member.display_name,interaction.user.id,identity,replace)
        await interaction.followup.send(f'{member.mention} の連携を保存しました。\n'+link_status(linked),ephemeral=True,allowed_mentions=discord.AllowedMentions.none())

    @minecraft.command(name='admin_unlink', description='管理者がこのサーバーでのMinecraft連携を解除')
    @app_commands.checks.has_permissions(administrator=True)
    async def admin_unlink(interaction: discord.Interaction, user: discord.User):
        previous=bot.fulfillment.bedrock.admin_unlink(interaction.guild_id,user.id,interaction.user.id)
        await interaction.response.send_message((f'{user.mention} のMinecraft連携を解除しました。再認証・再登録できます。\nニックネームと既存チケットの記録はそのまま残ります。' if previous else '未連携です。発行済みのJava認証アドレスも無効にしました。'),ephemeral=True,allowed_mentions=discord.AllowedMentions.none())

    bot.tree.add_command(minecraft)

    ticket = app_commands.Group(name='ticket', description='当選チケットのスタッフ操作', guild_only=True)

    @ticket.command(name='manage', description='このチケットの対応開始・完了・クローズ')
    @app_commands.choices(action=[app_commands.Choice(name='対応開始', value='start'), app_commands.Choice(name='対応完了', value='done'), app_commands.Choice(name='閉じる', value='close')])
    async def manage(interaction: discord.Interaction, action: app_commands.Choice[str]):
        await interaction.response.defer(ephemeral=True)
        r = next((r for r in bot.fulfillment.db.all() if r.get('channel') == interaction.channel_id), None)
        if not r:
            raise ReceiptError('受け取りチケット内で実行してください。')
        await bot.fulfillment.action(interaction, r['id'], action.value)
        await interaction.followup.send('更新しました。', ephemeral=True)
    bot.tree.add_command(ticket)
