from .access_policy import restricted, giveaway_runtime
from .notifications import notification_payload
from .clear_messages import register_clear
import asyncio
from decimal import Decimal
import logging
import os
import time
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import tasks
from discord.http import Route

from .core import draw, eligible, weight, duration_seconds
from .store import Store
from .fulfillment import Fulfillment, ReceiptSelect, register_commands
from .skelly import Skelly, register_skelly
from .serverinfo import ServerInfo, register_serverinfo
from .donation import Donation, register_donation
from .vip import Vip, register_vip
from .vip_giveaway import register_vipgiveaway
from .ticketlog import TicketLog, register_ticketlog
from .ticket_ai import register_ticket_ai
from .ticket_bridge import TicketBridge
from .moderation import Moderation, register_moderation
from .staffroom import StaffRooms, register_staffroom
from .message_log import MessageLog, register_message_log
from .auto_translate import AutoTranslate, register_auto_translate
from .word_filter import WordFilter, register_word_filter
from .community_template import CommunityTemplate
from .community_setup import CommunitySetup, register_community
from .redemptions import ReceiptError

log = logging.getLogger('giveaway')


class Buttons(discord.ui.View):
    def __init__(self, bot, finished=False):
        super().__init__(timeout=None)
        self.bot = bot
        if finished:
            for child in list(self.children):
                if child.custom_id != 'giveaway:claim:v1':
                    self.remove_item(child)

    async def change(self, interaction, joining):
        await interaction.response.defer(ephemeral=True)
        async with self.bot.lock:
            game = next((g for g in self.bot.store.games() if g.get('message') == interaction.message.id), None)
            if not game or game['status'] != 'open' or time.time() >= game['end']:
                return await interaction.followup.send('募集は終了しています。', ephemeral=True)
            if not joining:
                self.bot.store.leave(game['id'], interaction.user.id)
                game['dirty'] = True
                self.bot.store.save(game)
                return await interaction.followup.send('応募を取り消しました。', ephemeral=True)
            member = await interaction.guild.fetch_member(interaction.user.id)
            if member.bot or not qualifies(member, game):
                return await interaction.followup.send('参加条件を満たしていません。', ephemeral=True)
            if (game.get('receipt_policy') or {}).get('minecraft') and not minecraft_eligible(self.bot.store.db, member.id, game):
                return await interaction.followup.send('このGiveawayへの応募には、このサーバーでMinecraft認証が必要です。/minecraft link から認証してください。既存の応募者も締め切りまでの認証が必要です。', ephemeral=True)
            added = self.bot.store.enter(game['id'], member.id)
            game['dirty'] = True
            self.bot.store.save(game)
            value = weight([r.id for r in member.roles], game['rules'])
            await interaction.followup.send(
                f"あなたは参加しています！ 現在の倍率：{value:g}倍。抽選時にも条件・ロールを確認します。", ephemeral=True)

    @discord.ui.button(label='🎉 参加', custom_id='giveaway:join:v1', style=discord.ButtonStyle.success)
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.change(interaction, True)

    @discord.ui.button(label='参加取消', custom_id='giveaway:leave:v1')
    async def leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.change(interaction, False)

    @discord.ui.button(label='自分の参加状況', custom_id='giveaway:status:v1', style=discord.ButtonStyle.primary)
    async def status(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        game = next((g for g in self.bot.store.games() if g.get('message') == interaction.message.id), None)
        if not game:
            return await interaction.followup.send('募集が見つかりません。', ephemeral=True)
        if interaction.user.id not in self.bot.store.entries(game['id']):
            return await interaction.followup.send('あなたはこのGiveawayに参加していません。', ephemeral=True)
        if game['status'] != 'open' or time.time() >= game['end']:
            return await interaction.followup.send('あなたはこのGiveawayに応募済みです。受付は終了しています。', ephemeral=True)
        member = await interaction.guild.fetch_member(interaction.user.id)
        value = weight([r.id for r in member.roles], game['rules'])
        text = f'あなたは参加しています！ 現在の倍率：{value:g}倍。'
        if (game.get('receipt_policy') or {}).get('minecraft') and not minecraft_eligible(self.bot.store.db, member.id, game):
            text += '\n⚠️ Minecraft認証が未完了です。締め切りまでにこのサーバーで認証しないと抽選対象外になります。/minecraft link'
        if not qualifies(member, game):
            text += '\n現在は参加条件を満たしていません。抽選時も満たしていない場合は対象外です。'
        await interaction.followup.send(text, ephemeral=True)

    @discord.ui.button(label='🎁 受け取る', custom_id='giveaway:claim:v1', style=discord.ButtonStyle.primary)
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        game = next((g for g in self.bot.store.games() if g.get('message') == interaction.message.id), None)
        if not game:
            raise ReceiptError('募集が見つかりません。')
        service = self.bot.fulfillment
        service.db.seed(game)
        receipts = service.db.for_game(game['id'], interaction.user.id)
        if not receipts:
            return await interaction.response.send_message('当選した本人だけが受け取れます。', ephemeral=True)
        if len(receipts) == 1:
            return await service.begin(interaction, receipts[0]['id'])
        await interaction.response.send_message('受け取る当選を選んでください。', view=ReceiptSelect(service, receipts), ephemeral=True)

    async def on_error(self, interaction, error, item):
        if isinstance(error, ReceiptError):
            if interaction.response.is_done():
                return await interaction.followup.send(str(error), ephemeral=True)
            return await interaction.response.send_message(str(error), ephemeral=True)
        log.error('Button failed', exc_info=(type(error), error, error.__traceback__))
        if interaction.response.is_done():
            await interaction.followup.send('処理できませんでした。少し待って再試行してください。', ephemeral=True)
        else:
            await interaction.response.send_message('処理できませんでした。', ephemeral=True)


def minecraft_eligible(db, uid, game):
    if not (game.get('receipt_policy') or {}).get('minecraft'):
        return True
    row = db.execute('SELECT verified FROM guild_minecraft_links WHERE guild=? AND user=?', (game['guild'], uid)).fetchone()
    return row is not None and row[0] <= game['end']


def qualifies(member, game):
    joined = member.joined_at.timestamp() if member.joined_at else None
    if game.get('vip_roles') is not None and not set(game['vip_roles']).intersection(r.id for r in member.roles):
        return False
    return eligible([r.id for r in member.roles], joined, time.time(), game['role'], game['days'])


def embed_for(game):
    status = {'open': '募集中', 'drawing': '抽選中', 'done': '終了', 'cancelled': '中止', 'draft': '準備中'}[game['status']]
    e = discord.Embed(title=game['prize'], description=f"Giveaway #{game['id']} · {status}", color=0x7C5CFC)
    e.add_field(name='ホスト', value=f"<@{game['host']}>" if game.get('host') else '記録なし（旧募集）')
    e.add_field(name='当選人数', value=str(game['count']))
    e.add_field(name='応募人数', value=f"{game.get('entry_count', 0)}人")
    deadline = f"<t:{int(game['end'])}:F>"
    if game['status'] == 'open' and time.time() < game['end']:
        deadline += f"\n<t:{int(game['end'])}:R>"
    else:
        deadline += '\n' + ('募集を中止しました' if game['status'] == 'cancelled' else '受付終了')
    e.add_field(name='締切', value=deadline)
    conditions = []
    if game.get('vip_roles'):
        e.color = 0xD4AF37
        e.description = f"VIP限定Giveaway #{game['id']} · {status}"
        conditions.append('次のいずれかのVIP所持：' + ' / '.join(f'<@&{r}>' for r in game['vip_roles']))
    if game['role']:
        conditions.append(f"<@&{game['role']}> 所持")
    if game['days']:
        conditions.append(f"サーバー加入から{game['days']}日以上")
    e.add_field(name='参加条件（すべて満たす）', value=' ／ '.join(conditions) or '全員（BOTを除く）', inline=False)
    rules = '\n'.join(f'<@&{r}>：{v}倍' for r, v in game['rules'].items())
    e.add_field(name='倍率', value='通常1倍。複数該当時は最大のみ。\n' + (rules or '特別倍率なし'), inline=False)
    if game['status'] == 'done':
        e.add_field(name=f"当選者（抽選{len(game['rounds'])}回目）", value=' '.join(f'<@{u}>' for u in game['rounds'][-1]) or '対象者なし', inline=False)
    policy = game.get('receipt_policy')
    if policy:
        e.add_field(name='受け取り', value=f"当選から{policy['minutes']}分以内にチケットを作成\nMinecraft連携：{'応募・抽選に必須（締め切りまでの認証が必要）' if policy['minecraft'] else '不要'}", inline=False)
    e.set_footer(text='抽選時のロール・加入期間で判定。退出・再加入すると加入期間はリセット。')
    return e


class GiveawayBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.none()
        intents.guilds = True
        intents.members = True
        intents.guild_messages = True
        intents.message_content = os.getenv('TICKET_AI_MESSAGE_CONTENT') == '1'
        super().__init__(intents=intents, member_cache_flags=discord.MemberCacheFlags.none(), max_messages=None)
        self.tree = app_commands.CommandTree(self)
        path = Path(os.getenv('DATABASE_PATH', 'data/giveaway.sqlite3'))
        path.parent.mkdir(parents=True, exist_ok=True)
        self.store = Store(path)
        self.lock = asyncio.Lock()
        self.fulfillment = Fulfillment(self)
        self.skelly = Skelly(self)
        self.serverinfo = ServerInfo(self)
        self.donation = Donation(self)
        self.vip = Vip(self)
        self.ticketlog = TicketLog(self)
        self.moderation = Moderation(self)
        self.moderation.warning_roles.install_events()
        self.staffroom = StaffRooms(self)
        self.ticket_bridge = TicketBridge(self)
        self.message_log = MessageLog(self)
        self.auto_translate = AutoTranslate(self)
        self.word_filter = WordFilter(self)
        self.community_template = CommunityTemplate(self)
        self.community_setup = CommunitySetup(self)
        from .access_policy import install
        install(self)

    async def close(self):
        await self.community_setup.stop()
        await self.community_template.stop()
        await self.auto_translate.stop()
        await self.message_log.stop()
        await self.ticket_bridge.stop()
        self.tick.cancel()
        await self.moderation.stop()
        await self.ticketlog.stop()
        await self.vip.stop()
        await self.donation.stop()
        await self.serverinfo.stop()
        await self.skelly.stop()
        await self.fulfillment.stop()
        await super().close()

    async def watch_stop_file(self):
        while not self.is_closed():
            if Path(os.environ["BOT_STOP_FILE"]).exists():
                await self.close()
                return
            await asyncio.sleep(1)

    async def setup_hook(self):
        if os.getenv("BOT_STOP_FILE"):
            self.local_stop_watch = asyncio.create_task(self.watch_stop_file())
        self.add_view(Buttons(self))
        guild_id = os.getenv('TEST_GUILD_ID')
        if guild_id:
            guild = discord.Object(id=int(guild_id))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()
        self.tick.start()
        await self.fulfillment.start()
        await self.ticket_bridge.start()
        await self.message_log.start()
        await self.auto_translate.start()
        if giveaway_runtime():
            await self.ticketlog.start()
            return
        await self.skelly.start()
        await self.serverinfo.start()
        await self.donation.start()
        await self.vip.start()
        await self.ticketlog.start()
        await self.moderation.start()
        await self.community_template.start()
        await self.community_setup.start()
        await self.staffroom.start()

    async def on_warning_member_update(self, data):
        await self.moderation.warning_roles.member_changed(data)

    async def on_message(self, message):
        await self.message_log.on_message(message)
        if await self.word_filter.check(message):return
        await self.auto_translate.enqueue(message)

    async def on_raw_message_edit(self, payload):
        await self.message_log.on_raw_message_edit(payload)
        if await self.word_filter.edited(payload):return
        await self.auto_translate.edited(payload)

    async def on_raw_message_delete(self, payload):
        await self.message_log.on_raw_message_delete(payload)

    async def on_raw_bulk_message_delete(self, payload):
        await self.message_log.on_raw_bulk_message_delete(payload)

    async def publish(self, game):
        channel = self.get_channel(game['channel']) or await self.fetch_channel(game['channel'])
        game['entry_count'] = len(self.store.entries(game['id']))
        await channel.get_partial_message(game['message']).edit(allowed_mentions=discord.AllowedMentions.none(), embed=embed_for(game), view=Buttons(self) if game['status'] == 'open' else (Buttons(self, finished=True) if game.get('receipt_events') else None))
        game['dirty'] = False
        self.store.save(game)

    async def finish(self, game):
        guild = self.get_guild(game['guild'])
        if not guild:
            raise RuntimeError('Guild unavailable')
        candidates = {}
        previous = {u for result in game['rounds'] for u in result}
        for uid in self.store.entries(game['id']):
            if uid in previous:
                continue
            try:
                member = await guild.fetch_member(uid)
            except discord.NotFound:
                continue
            # Other HTTP failures abort this attempt. Never silently exclude someone.
            if not member.bot and qualifies(member, game) and minecraft_eligible(self.store.db, uid, game):
                candidates[uid] = weight([r.id for r in member.roles], game['rules'])
        game['rounds'].append(draw(candidates, game['count']))
        game['status'] = 'done'
        awarded_at = time.time()
        policy = game.get('receipt_policy')
        game.setdefault('receipt_events', []).append(dict(round=len(game['rounds']), winners=game['rounds'][-1], at=awarded_at, deadline=awarded_at + (policy['minutes'] * 60 if policy else 86400)))
        game['announcement'] = {'round': len(game['rounds']), 'state': 'pending'}
        game['dirty'] = True
        self.store.save(game)  # Persist winners BEFORE editing Discord; retries cannot redraw.
        await self.publish(game)

    async def announce(self, game):
        delivery = game.get('announcement')
        if not delivery or delivery['state'] != 'pending':
            return
        # Discord deduplicates nonces only for a few minutes. Stop retrying an
        # ambiguous send beyond that window rather than ping winners twice.
        if delivery.get('started') and time.time() - delivery['started'] > 120:
            delivery['state'] = 'uncertain'
            self.store.save(game)
            log.error('Giveaway %s announcement delivery uncertain; inspect channel before retry', game['id'])
            return
        if not delivery.get('started'):
            delivery['started'] = time.time()
            self.store.save(game)
        winners = game['rounds'][delivery['round'] - 1]
        prize = discord.utils.escape_markdown(game['prize'])
        mentions = ' '.join(f'<@{uid}>' for uid in winners)
        prefix = '【再抽選】' if delivery['round'] > 1 else ''
        content = (f'{prefix}🎉 おめでとう {mentions}！「{prize}」に当選しました！' if winners
                   else f'{prefix}「{prize}」の抽選が終了しました。参加条件を満たす当選対象者はいませんでした。')
        content += f"\nhttps://discord.com/channels/{game['guild']}/{game['channel']}/{game['message']}"
        result = await self.http.request(Route('POST', '/channels/{channel_id}/messages', channel_id=game['channel']), json={
            'content': content,
            'allowed_mentions': {'parse': [], 'users': [str(uid) for uid in winners]},
            'nonce': f"{game['message']}:{delivery['round']}", 'enforce_nonce': True,
        })
        delivery.update(state='sent', message=result['id'])
        self.store.save(game)

    @tasks.loop(seconds=15)
    async def tick(self):
        async with self.lock:
            for game in self.store.games():
                try:
                    if game['status'] == 'open' and game['end'] <= time.time():
                        game['status'] = 'drawing'
                        self.store.save(game)
                    if game['status'] == 'drawing':
                        await self.finish(game)
                    elif game.get('dirty') and game.get('message'):
                        await self.publish(game)
                except Exception:
                    log.exception('Giveaway %s: retry on next tick', game['id'])
                try:
                    await self.announce(game)
                except Exception:
                    log.exception('Giveaway %s: announcement retry on next tick', game['id'])

    @tick.before_loop
    async def before_tick(self):
        await self.wait_until_ready()


bot = GiveawayBot()
group = app_commands.Group(name='giveaway', description='Giveawayの作成・管理', guild_only=True, default_permissions=discord.Permissions(manage_guild=True))


def admin(interaction):
    return interaction.guild is not None and interaction.permissions.manage_guild


@group.command(name='multiplier', description='ロール倍率を設定。1倍にすると特別倍率を解除')
@app_commands.check(admin)
async def multiplier(interaction: discord.Interaction, role: discord.Role, value: app_commands.Range[float, 1, 100]):
    if Decimal(str(value)) != Decimal(str(value)).quantize(Decimal('0.01')):
        return await interaction.response.send_message('倍率は小数点以下2桁までで指定してください（例：1.5、2.25）。', ephemeral=True)
    rules = bot.store.rules(interaction.guild_id)
    if value == 1:
        rules.pop(str(role.id), None)
    else:
        if str(role.id) not in rules and len(rules) >= 20:
            return await interaction.response.send_message('設定できる倍率ロールは20個までです。', ephemeral=True)
        rules[str(role.id)] = value
    bot.store.set_rules(interaction.guild_id, rules)
    await interaction.response.send_message(f'{role.mention}：{value}倍。次に作成する募集から適用します。', ephemeral=True)


@group.command(name='settings', description='次回の募集に適用する倍率設定を表示')
@app_commands.check(admin)
async def settings(interaction: discord.Interaction):
    rules = bot.store.rules(interaction.guild_id)
    await interaction.response.send_message('通常1倍／最大倍率のみ適用\n' + ('\n'.join(f'<@&{r}>：{v}倍' for r, v in rules.items()) or '特別倍率なし'), ephemeral=True)


@group.command(name='notification_role', description='Giveaway共通の通知ロールを設定・確認・解除')
@app_commands.check(admin)
async def notification_role(interaction: discord.Interaction, role: discord.Role = None, clear: bool = False):
    if clear and role:
        raise ReceiptError('ロール設定と解除は同時に指定できません。')
    if role and role.is_default():
        raise ReceiptError('@everyone は作成時に通知方法として選択してください。')
    if clear or role:
        bot.store.set_notification_role(interaction.guild_id, None if clear else role.id)
    rid = bot.store.notification_role(interaction.guild_id)
    await interaction.response.send_message('共通の通知ロール：' + (f'<@&{rid}>' if rid else '未設定') + '\n作成時に notification を選ぶと通知します。', ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


@group.command(name='create', description='このチャンネルで募集を開始')
@app_commands.check(admin)
@app_commands.choices(notification=[app_commands.Choice(name='通知なし', value='none'), app_commands.Choice(name='通知ロール', value='role'), app_commands.Choice(name='@everyone', value='everyone')])
@app_commands.describe(notification='今回の通知先（省略すると通知なし）', duration='募集期間の数値（1以上）', unit='期間の単位：秒・分・時間・日')
@app_commands.choices(unit=[app_commands.Choice(name=label, value=value) for label, value in [('秒', 'seconds'), ('分', 'minutes'), ('時間', 'hours'), ('日', 'days')]])
async def create(interaction: discord.Interaction, prize: app_commands.Range[str, 1, 200], duration: app_commands.Range[int, 1, 31536000], unit: app_commands.Choice[str], winners: app_commands.Range[int, 1, 20] = 1, required_role: discord.Role = None, min_days: app_commands.Range[int, 0, 3650] = 0, notification: app_commands.Choice[str] = None):
    await interaction.response.defer(ephemeral=True)
    await start_game(interaction, prize, duration, unit, winners, required_role, min_days, notification)


async def start_game(interaction, prize, duration, unit, winners=1, required_role=None, min_days=0, notification=None, *, vip_roles=None, override_rules=None):
    try:
        seconds = duration_seconds(duration, unit.value)
    except ValueError as error:
        raise ReceiptError(str(error)) from error
    mode = notification.value if notification else 'none'
    content, mentions, notify_role = notification_payload(bot.store, interaction.guild, interaction.channel, mode)
    policy = None if restricted(interaction.guild_id) else bot.fulfillment.db.profile(interaction.guild_id, interaction.channel_id)
    if not policy:
        raise ReceiptError('この募集チャンネルの受け取り設定が未設定です。/giveaway receipt_config を先に実行してください。')
    await bot.fulfillment.validate_policy(interaction.guild, policy)
    async with bot.lock:
        game = bot.store.create(dict(guild=interaction.guild_id, host=interaction.user.id, channel=interaction.channel_id, message=None, prize=prize, count=winners, end=time.time() + seconds, role=required_role.id if required_role else None, days=min_days, rules=override_rules if override_rules is not None else bot.store.rules(interaction.guild_id), vip_roles=vip_roles, status='draft', rounds=[], dirty=False, receipt_policy=policy, notification=mode, notification_role=notify_role))
        # Send without buttons, save its ID, then open registration. A crash cannot leave an active untracked button.
        message = await interaction.channel.send(content=content, embed=embed_for(game), allowed_mentions=mentions)
        game.update(message=message.id, status='open', dirty=True)
        bot.store.save(game)
        await bot.publish(game)
    await interaction.followup.send(f"作成しました：{message.jump_url}\n管理ID：{game['id']}", ephemeral=True)


@group.command(name='manage', description='終了・中止・再抽選。再抽選では過去の当選者を除外')
@app_commands.check(admin)
@app_commands.choices(action=[app_commands.Choice(name='今すぐ終了して抽選', value='end'), app_commands.Choice(name='中止', value='cancel'), app_commands.Choice(name='再抽選', value='reroll')])
async def manage(interaction: discord.Interaction, giveaway_id: int, action: app_commands.Choice[str]):
    await interaction.response.defer(ephemeral=True)
    async with bot.lock:
        game = bot.store.get(giveaway_id)
        if not game or game['guild'] != interaction.guild_id:
            return await interaction.followup.send('このサーバーの募集が見つかりません。', ephemeral=True)
        if game.get('announcement', {}).get('state') == 'pending':
            return await interaction.followup.send('当選発表の送信処理中です。少し待ってから操作してください。', ephemeral=True)
        if action.value == 'cancel' and game['status'] == 'open':
            game.update(status='cancelled', dirty=True)
        elif action.value == 'end' and game['status'] == 'open' or action.value == 'reroll' and game['status'] == 'done':
            game['status'] = 'drawing'
        else:
            return await interaction.followup.send('現在の状態ではその操作はできません。', ephemeral=True)
        bot.store.save(game)
    await interaction.followup.send('受け付けました。結果は募集メッセージに反映します。', ephemeral=True)


@group.command(name='list', description='このサーバーの最近の募集を表示')
@app_commands.check(admin)
async def listing(interaction: discord.Interaction):
    games = [g for g in bot.store.games() if g['guild'] == interaction.guild_id][-10:]
    lines = [f"#{g['id']} · {g['status']} · 応募{len(bot.store.entries(g['id']))}人 · {g['prize'][:70]}" for g in games]
    await interaction.response.send_message('\n'.join(lines) or '募集はありません。', ephemeral=True)


@bot.tree.error
async def command_error(interaction, error):
    log.error('Command failed', exc_info=(type(error), error, error.__traceback__))
    cause = getattr(error, 'original', error)
    if isinstance(cause, ReceiptError):
        if interaction.response.is_done():
            return await interaction.followup.send(str(cause), ephemeral=True)
        return await interaction.response.send_message(str(cause), ephemeral=True)
    text = 'この操作には「サーバー管理」権限が必要です。' if isinstance(error, app_commands.CheckFailure) else '処理できませんでした。権限やBOTのログを確認してください。'
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True)
    else:
        await interaction.response.send_message(text, ephemeral=True)


register_commands(bot, group)
register_ticket_ai(bot)
register_skelly(bot)
register_serverinfo(bot)
register_donation(bot)
register_vip(bot)
register_vipgiveaway(bot, start_game)
register_ticketlog(bot)
register_moderation(bot)
register_staffroom(bot)
register_message_log(bot)
register_auto_translate(bot)
register_word_filter(bot)
register_community(bot)
register_clear(bot)
bot.tree.add_command(group)

if __name__ == '__main__':
    bot.run(os.environ['DISCORD_TOKEN'], log_level=logging.INFO)
