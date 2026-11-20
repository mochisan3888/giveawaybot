from decimal import Decimal
import secrets
import discord
from discord import app_commands
from .fulfillment import SafeView
from .redemptions import ReceiptError


def vip_rules(config, selected):
    tiers = config.get('tiers', [])
    if not 1 <= len(selected) <= 10 or len(set(selected)) != len(selected) or any(r not in tiers for r in selected):
        raise ReceiptError('登録済みのVIPロールを1～10個選択してください。')
    return {str(r): config.get('giveaway_rules', {}).get(str(r), 1) for r in selected}


class VipGiveawaySelection(SafeView):
    def __init__(self, bot, interaction, args, start_game):
        super().__init__(timeout=300)
        self.bot, self.author, self.guild, self.channel = bot, interaction.user.id, interaction.guild_id, interaction.channel_id
        self.selected, self.used = [], False
        tiers = bot.vip.db.tiers(self.guild)
        options = [discord.SelectOption(label=interaction.guild.get_role(r).name[:100], value=str(r)) for r in tiers if interaction.guild.get_role(r)]
        if not options:
            raise ReceiptError('先に /vip tiers でVIPロールを登録してください。')
        select = discord.ui.Select(custom_id='vipgiveaway:'+secrets.token_hex(12), placeholder='参加を許可するVIP（いずれか所持で参加可能）', options=options, min_values=1, max_values=len(options))
        async def selected(i):
            self.check(i)
            self.selected = [int(v) for v in select.values]
            await i.response.defer()
        select.callback = selected
        self.add_item(select)
        button = discord.ui.Button(custom_id='vipgiveaway:'+secrets.token_hex(12), label='この条件で募集開始', style=discord.ButtonStyle.success)
        async def confirm(i):
            self.check(i)
            if self.used:
                raise ReceiptError('この作成画面は使用済みです。')
            rules = vip_rules(bot.vip.db.config(i.guild_id), self.selected)
            if any(i.guild.get_role(r) is None for r in self.selected):
                raise ReceiptError('VIPロールが削除されています。選び直してください。')
            self.used = True
            await i.response.defer(ephemeral=True)
            # A failed/uncertain send is never retried from this same confirmation.
            await start_game(i, **args, vip_roles=list(self.selected), override_rules=rules)
            for child in self.children:child.disabled = True
            await i.edit_original_response(content='VIP限定Giveawayを作成しました。', view=self)
        button.callback = confirm
        self.add_item(button)

    def check(self, i):
        if i.user.id != self.author or i.guild_id != self.guild or i.channel_id != self.channel or not i.permissions.manage_guild:
            raise ReceiptError('作成を開始した管理者だけが元のチャンネルで操作できます。')


def register_vipgiveaway(bot, start_game):
    group = app_commands.Group(name='vipgiveaway', description='VIP限定Giveaway', guild_only=True, default_permissions=discord.Permissions(manage_guild=True))

    @group.command(name='multiplier', description='VIP限定Giveaway専用のロール倍率を設定')
    @app_commands.checks.has_permissions(manage_guild=True)
    async def multiplier(i: discord.Interaction, role: discord.Role, value: app_commands.Range[float, 1, 100]):
        config = bot.vip.db.config(i.guild_id)
        if role.id not in config.get('tiers', []):
            raise ReceiptError('先に /vip tiers に登録したVIPロールを指定してください。')
        if Decimal(str(value)) != Decimal(str(value)).quantize(Decimal('0.01')):
            raise ReceiptError('倍率は小数点以下2桁までです。')
        rules = config.get('giveaway_rules', {})
        rules[str(role.id)] = value
        bot.vip.db.set_config(i.guild_id, giveaway_rules=rules)
        await i.response.send_message(f'{role.mention}：{value:g}倍。次のVIP限定募集から適用します。', ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @group.command(name='settings', description='VIP限定Giveaway専用の倍率を確認')
    @app_commands.checks.has_permissions(manage_guild=True)
    async def settings(i: discord.Interaction):
        c = bot.vip.db.config(i.guild_id)
        text = '\n'.join(f"<@&{r}>：{c.get('giveaway_rules', {}).get(str(r), 1):g}倍" for r in c.get('tiers', []))
        await i.response.send_message((text or 'VIPロール未設定')+'\n複数該当時は最大倍率のみ適用。', ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @group.command(name='create', description='最大10ロールから対象を選んでVIP限定募集を開始')
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.choices(unit=[app_commands.Choice(name=n,value=v) for n,v in [('秒','seconds'),('分','minutes'),('時間','hours'),('日','days')]], notification=[app_commands.Choice(name=n,value=v) for n,v in [('通知なし','none'),('通知ロール','role'),('@everyone','everyone')]])
    async def create(i: discord.Interaction, prize: app_commands.Range[str,1,200], duration: app_commands.Range[int,1,31536000], unit: app_commands.Choice[str], winners: app_commands.Range[int,1,20]=1, notification: app_commands.Choice[str]=None):
        from .core import duration_seconds
        duration_seconds(duration,unit.value)
        args=dict(prize=prize,duration=duration,unit=unit,winners=winners,required_role=None,min_days=0,notification=notification)
        await i.response.send_message('参加対象のVIPロールを選び、募集開始を押してください。複数ロールを持つ場合は対象ロールの最大倍率のみ適用します。',view=VipGiveawaySelection(bot,i,args,start_game),ephemeral=True)
    bot.tree.add_command(group)
