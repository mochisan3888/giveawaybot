"""Staff moderation and button/modal-based DM appeals (no message-content intent)."""
from .access_policy import restricted
import asyncio
import datetime
import json
import logging
import re
import time
import discord
from discord import app_commands
from discord.ext import tasks
from discord.http import Route
from .fulfillment import SafeView
from .redemptions import ReceiptError
from .warning_roles import WarningRoles
from .moderation_audit import ModerationAudit
from .moderation_store import ModerationStore, LABELS, DEFAULTS, render_template, validate_template

log=logging.getLogger('giveaway.moderation')
NONE=discord.AllowedMentions.none()
PERMISSIONS={'timeout':'moderate_members','untimeout':'moderate_members','kick':'kick_members','ban':'ban_members','unban':'ban_members'}


def ban_target_input(user=None, user_id=None):
    if (user is None) == (user_id is None):
        raise ReceiptError('user または user_id のどちらか一つを指定してください。')
    if user is not None:
        return user
    value = user_id.strip()
    if not re.fullmatch(r'[0-9]{1,20}', value) or not 0 < int(value) < 2**64:
        raise ReceiptError('user_id は有効なDiscordユーザーIDを数字で指定してください。')
    return discord.Object(id=int(value))


async def resolve_ban_target(bot, guild, uid):
    try:
        return await guild.fetch_member(uid)
    except discord.NotFound as error:
        if error.code != 10007:
            raise
    try:
        return await bot.fetch_user(uid)
    except discord.NotFound as error:
        if error.code != 10013:
            raise
        raise ReceiptError('指定したユーザーIDは見つかりません。') from None


async def ensure_not_banned(guild, target):
    try:
        await guild.fetch_ban(target)
    except discord.NotFound as error:
        if error.code == 10026:
            return
        raise
    raise ReceiptError('このユーザーはすでにBANされています。')


def check_target(guild,actor,target,me,action):
    if target.id in (actor.id,me.id,guild.owner_id):raise ReceiptError('自分・BOT自身・サーバー所有者は対象にできません。')
    if hasattr(target,'top_role'):
        if actor.id!=guild.owner_id and target.top_role>=actor.top_role:raise ReceiptError('自分と同じ順位以上のロールを持つ人は処分できません。')
        if target.top_role>=me.top_role:raise ReceiptError('対象者のロールがBOT以上です。BOTのロール順位を確認してください。')
        if action=='timeout' and target.guild_permissions.administrator:raise ReceiptError('管理者権限のある人はタイムアウトできません。')
    if action in PERMISSIONS:
        perm=PERMISSIONS[action]
        if not getattr(actor.guild_permissions,perm):raise ReceiptError('実行者にもDiscordの処分権限が必要です：'+perm)
        if not getattr(me.guild_permissions,perm):raise ReceiptError('BOTに必要な権限がありません：'+perm)


class ModModal(discord.ui.Modal):
    async def on_error(self,i,error):await SafeView.on_error(self,i,error,None)


class AppealForm(ModModal):
    def __init__(self,service,cid,staff=False):
        super().__init__(title='スタッフから返信' if staff else '異議申し立て・追加メッセージ')
        self.service,self.cid,self.staff=service,cid,staff
        self.body=discord.ui.TextInput(label='送信する文章',style=discord.TextStyle.paragraph,max_length=1500)
        self.add_item(self.body)
    async def on_submit(self,i):
        await i.response.defer(ephemeral=True)
        result=await self.service.relay(i,self.cid,self.body.value,self.staff)
        await i.followup.send(result,ephemeral=True,allowed_mentions=NONE)


class AppealButtons(SafeView):
    def __init__(self,service,cid,staff=False):
        super().__init__(timeout=None)
        self.service,self.cid=service,cid
        actions=[('reply','本人のDMへ返信'),('start','対応開始'),('done','対応完了'),('close','閉じる')] if staff else [('appeal','異議申し立て・返信')]
        for action,label in actions:
            button=discord.ui.Button(label=label,custom_id=f'moderation:{cid}:{action}',style=discord.ButtonStyle.primary if action in ('reply','appeal') else discord.ButtonStyle.secondary)
            async def callback(i,action=action):
                if action in ('appeal','reply'):
                    # No network I/O before opening the modal; all checks repeat on submit.
                    c=service.db.get(cid)
                    if not c:raise ReceiptError('処分記録が見つかりません。')
                    if action=='appeal' and (i.guild_id is not None or i.user.id!=c['user']):raise ReceiptError('本人のDMから操作してください。')
                    if action=='reply' and (i.guild_id!=c['guild'] or i.channel_id!=c['channel']):raise ReceiptError('対応チケット内で操作してください。')
                    await i.response.send_modal(AppealForm(service,cid,action=='reply'))
                else:
                    await i.response.defer(ephemeral=True)
                    await service.appeal_action(i,cid,action)
                    await i.followup.send('対応状態を更新しました。処分自体の解除は専用コマンドで行ってください。',ephemeral=True)
            button.callback=callback;self.add_item(button)


class ConfirmPunishment(SafeView):
    def __init__(self,service,owner,guild,user,action,reason,minutes,request):
        super().__init__(timeout=90)
        self.service,self.owner,self.guild,self.user=service,owner,guild,user
        self.action,self.reason,self.minutes,self.request=action,reason,minutes,request
        self.used=False
    @discord.ui.button(label='処分を実行する',style=discord.ButtonStyle.danger)
    async def confirm(self,i,button):
        if i.user.id!=self.owner or i.guild_id!=self.guild:raise ReceiptError('操作したスタッフ本人だけが確認できます。')
        await i.response.defer(ephemeral=True)
        if self.used:raise ReceiptError('すでに実行を受け付けています。履歴を確認してください。')
        self.used=True
        text=await self.service.execute(i,self.user,self.action,self.reason,self.minutes,self.request)
        await i.followup.send(text,ephemeral=True,allowed_mentions=NONE)
        self.stop()
    @discord.ui.button(label='キャンセル')
    async def cancel(self,i,button):
        if i.user.id!=self.owner:raise ReceiptError('本人だけが操作できます。')
        self.used=True;self.stop()
        await i.response.edit_message(content='キャンセルしました。',view=None)


class SetupRoles(SafeView):
    def __init__(self,service,owner,guild,channel,category):
        super().__init__(timeout=180)
        self.service,self.owner,self.guild,self.channel,self.category=service,owner,guild,channel,category
    @discord.ui.select(cls=discord.ui.RoleSelect,placeholder='Owner / Subowner / Adminなど、担当ロールを選択',min_values=1,max_values=10)
    async def select(self,i,select):
        if i.user.id!=self.owner or i.guild_id!=self.guild:raise ReceiptError('設定を開始した本人だけが操作できます。')
        await i.response.defer(ephemeral=True)
        actor=await i.guild.fetch_member(i.user.id)
        if not actor.guild_permissions.administrator:raise ReceiptError('設定には管理者権限が必要です。')
        roles=[r.id for r in select.values]
        if self.guild in roles:raise ReceiptError('@everyoneは指定できません。')
        await self.service.validate_config(i.guild,self.channel,self.category,roles)
        async with self.service.lock:
            self.service.db.configure(self.guild,roles=roles,log=self.channel,category=self.category)
            await self.service.refresh_appeal_permissions(i.guild,roles)
        await i.followup.send('設定を保存しました。担当ロールの人と管理者が利用できます。実際の処分にはDiscord側の処分権限とロール順位も必要です。',ephemeral=True)
        self.stop()


class TextForm(ModModal):
    def __init__(self,service,guild,owner,kind):
        super().__init__(title='モデレーション文面の設定')
        self.service,self.guild,self.owner,self.kind=service,guild,owner,kind
        cfg=service.db.config(guild)
        self.body=discord.ui.TextInput(label='文面（{server} {user} {reason} など）',style=discord.TextStyle.paragraph,max_length=1400,default=cfg.get('texts',{}).get(kind,DEFAULTS[kind]))
        self.add_item(self.body)
    async def on_submit(self,i):
        if i.guild_id!=self.guild or i.user.id!=self.owner:raise ReceiptError('設定を開始した本人だけが操作できます。')
        await i.response.defer(ephemeral=True)
        actor=await i.guild.fetch_member(i.user.id)
        if not actor.guild_permissions.administrator:raise ReceiptError('管理者専用です。')
        text=validate_template(self.body.value)
        texts=self.service.db.config(self.guild).get('texts',{});texts[self.kind]=text
        self.service.db.configure(self.guild,texts=texts)
        await i.followup.send('文面を保存しました。DMは次回送信から反映。パネル文面はパネルを再設置すると反映されます。理由・ケース番号などの記録欄は必ず併記します。',ephemeral=True)


class LookupForm(ModModal):
    def __init__(self,service):
        super().__init__(title='ユーザー別の警告・処分履歴')
        self.service=service
        self.uid=discord.ui.TextInput(label='DiscordユーザーID',min_length=1,max_length=22)
        self.add_item(self.uid)
    async def on_submit(self,i):
        await i.response.defer(ephemeral=True)
        await self.service.staff(i)
        if not self.uid.value.isdecimal() or not 0<int(self.uid.value)<2**63:raise ReceiptError('有効な数字のDiscord IDを入力してください。')
        await i.followup.send(embed=self.service.history(i.guild_id,int(self.uid.value),1),ephemeral=True,allowed_mentions=NONE)


class ModPanel(SafeView):
    def __init__(self,service):super().__init__(timeout=None);self.service=service
    @discord.ui.button(label='警告・処分された人の一覧',custom_id='moderation:panel:list',style=discord.ButtonStyle.primary)
    async def listing(self,i,button):
        await i.response.defer(ephemeral=True);await self.service.staff(i)
        await i.followup.send(embed=self.service.people(i.guild_id,1),ephemeral=True,allowed_mentions=NONE)
    @discord.ui.button(label='ユーザーの履歴を検索',custom_id='moderation:panel:lookup')
    async def lookup(self,i,button):
        if not i.guild_id:raise ReceiptError('サーバー内で使用してください。')
        await i.response.send_modal(LookupForm(self.service))


class Moderation:
    def __init__(self,bot):
        self.bot=bot;self.db=ModerationStore(bot.store.db);self.lock=asyncio.Lock()
        self.warning_roles=WarningRoles(bot,self.db)
        self.audit=ModerationAudit(self)
    async def staff(self,i,guild_id=None):
        gid=guild_id or i.guild_id
        if not gid or i.guild_id!=gid:raise ReceiptError('対象サーバー内で実行してください。')
        guild=self.bot.get_guild(gid)
        if not guild:raise ReceiptError('サーバーを取得できません。')
        actor=await guild.fetch_member(i.user.id)
        cfg=self.db.config(gid)
        if not actor.guild_permissions.administrator and not set(cfg.get('roles',[])).intersection(r.id for r in actor.roles):raise ReceiptError('このサーバーのモデレーション担当者専用です。')
        return guild,actor
    def configured(self,gid):
        cfg=self.db.config(gid)
        if not all(cfg.get(k) for k in ('roles','log','category')):raise ReceiptError('先に管理者が /moderation setup で担当ロール・ログ・異議申し立て先を設定してください。')
        return cfg
    async def validate_config(self,guild,channel_id,category_id,roles):
        channel=await guild.fetch_channel(channel_id);category=await guild.fetch_channel(category_id)
        me=await guild.fetch_member(self.bot.user.id)
        if not isinstance(channel,discord.TextChannel) or not isinstance(category,discord.CategoryChannel):raise ReceiptError('ログはテキストチャンネル、作成先はカテゴリを選択してください。')
        p=channel.permissions_for(me);q=category.permissions_for(me)
        if not(p.view_channel and p.send_messages and p.embed_links and q.view_channel and q.manage_channels):raise ReceiptError('BOTにログの閲覧・送信・埋め込み権限と、カテゴリの閲覧・チャンネル管理権限が必要です。')
        if not roles or guild.id in roles or any(not guild.get_role(r) for r in roles):raise ReceiptError('有効な担当ロールを指定してください。')
    async def refresh_appeal_permissions(self,guild,roles):
        me=await guild.fetch_member(self.bot.user.id)
        for c in self.db.cases(guild.id):
            if not c.get('channel'):continue
            try:channel=await guild.fetch_channel(c['channel'])
            except discord.NotFound:continue
            overwrites={guild.default_role:discord.PermissionOverwrite(view_channel=False),me:discord.PermissionOverwrite(view_channel=True,send_messages=True,embed_links=True,read_message_history=True,manage_channels=True)}
            for rid in roles:
                role=guild.get_role(rid)
                if role:overwrites[role]=discord.PermissionOverwrite(view_channel=True,send_messages=True,read_message_history=True)
            from .ticket_bridge import add_peer
            add_peer(guild,overwrites)
            try:await channel.edit(overwrites=overwrites,reason='モデレーション担当ロール変更')
            except discord.HTTPException:
                raise ReceiptError('設定は保存しましたが、一部の既存チケットの権限更新に失敗しました。BOTのチャンネル管理権限を確認してsetupを再実行してください。')

    async def stage(self,i,user,action,reason,minutes=0):
        await i.response.defer(ephemeral=True)
        guild,actor=await self.staff(i);self.configured(guild.id)
        target=await resolve_ban_target(self.bot,guild,user.id) if action=='ban' else (await guild.fetch_member(user.id) if action!='unban' else user)
        me=await guild.fetch_member(self.bot.user.id)
        check_target(guild,actor,target,me,action)
        if action=='ban':await ensure_not_banned(guild,target)
        if action=='timeout' and not 1<=minutes<=40320:raise ReceiptError('タイムアウトは1～40320分です。')
        reason=reason.strip()
        if not reason or len(reason)>1000:raise ReceiptError('理由は1～1000文字です。')
        await i.followup.send(f'**{LABELS[action]}の確認**\n対象：{discord.utils.escape_markdown(str(target))} / <@{user.id}>（{user.id}）\n理由：{reason}\n'+(f'期間：{minutes}分\n' if action=='timeout' else '')+'本人へDMを送り、処分履歴とログに記録します。',view=ConfirmPunishment(self,i.user.id,guild.id,user.id,action,reason,minutes,i.id),ephemeral=True,allowed_mentions=NONE)
    async def execute(self,i,uid,action,reason,minutes,request):
        async with self.lock:
            guild,actor=await self.staff(i);self.configured(guild.id)
            target=await resolve_ban_target(self.bot,guild,uid) if action=='ban' else (await guild.fetch_member(uid) if action!='unban' else await self.bot.fetch_user(uid))
            me=await guild.fetch_member(self.bot.user.id);check_target(guild,actor,target,me,action)
            if action=='ban':await ensure_not_banned(guild,target)
            if action=='unban':await guild.fetch_ban(target)
            until=time.time()+minutes*60 if action=='timeout' else None
            c,new=self.db.create(guild.id,uid,actor.id,action,reason,request,until)
            if not new:return f"ケース #{c['id']} はすでに受け付け済みです。状態：{c['state']}"
            self.db.event(c,'処分実行受付',actor.id,reason)
            # Kick/ban may remove the last shared server: deliver the notification first.
            if action in ('kick','ban'):await self.notify(c,guild,target,planned=True)
            try:
                audit=f"Case #{c['id']} / staff {actor.id}: {reason}"[:500]
                if action=='timeout':await target.timeout(datetime.datetime.fromtimestamp(until,datetime.timezone.utc),reason=audit)
                elif action=='untimeout':await target.timeout(None,reason=audit)
                elif action=='kick':await target.kick(reason=audit)
                elif action=='ban':await guild.ban(target,delete_message_seconds=0,reason=audit)
                elif action=='unban':await guild.unban(target,reason=audit)
                c['state']='applied';self.db.save(c);self.db.event(c,'処分実行完了',actor.id,reason)
            except (discord.HTTPException,TimeoutError,OSError) as error:
                c['state']='failed' if isinstance(error,discord.HTTPException) and error.status in (400,403,404) else 'uncertain'
                self.db.save(c);self.db.event(c,'処分失敗・結果要確認',actor.id,type(error).__name__)
                if c['dm']=='sent':
                    try:await target.send(f"ケース #{c['id']}：先ほどの処分は実行失敗、または結果を確認できていません。スタッフが確認します。",allowed_mentions=NONE)
                    except discord.HTTPException:pass
                return f"ケース #{c['id']}：処分状態は {c['state']}。Discord側の状態を確認してください。自動再実行はしません。"
            role_status = await self.warning_roles.update(guild,uid) if action=='warn' else ''
            if action not in ('kick','ban'):await self.notify(c,guild,target)
            return f"{LABELS[action]}を実行しました。ケース #{c['id']}\n有効な警告：{self.db.warnings(guild.id,uid)}件\n{role_status}\n本人DM：{'送信済み' if c['dm']=='sent' else '送信できませんでした（DM設定などを確認）'}"
    async def notify(self,c,guild,user,planned=False):
        cfg=self.db.config(guild.id)
        values=dict(server=guild.name,user=str(user),reason=c['reason'],until=f"<t:{int(c['until'])}:F>" if c['until'] else 'なし',case=str(c['id']),warnings=str(self.db.warnings(guild.id,user.id)),action=LABELS[c['action']])
        body=render_template(cfg.get('texts',{}).get(c['action'],DEFAULTS[c['action']]),values)
        e=discord.Embed(title=LABELS[c['action']]+'のお知らせ',description=body,color=0xE67E22)
        e.add_field(name='ケース番号',value=str(c['id']));e.add_field(name='理由',value=c['reason'][:1000],inline=False)
        e.add_field(name='有効な警告数',value=values['warnings'])
        if c['until']:e.add_field(name='解除予定',value=values['until'])
        if planned:e.add_field(name='実行について',value='この通知の直後に処分を実行します。失敗・結果不明の場合は追加通知します。',inline=False)
        e.set_footer(text='異議申し立ては下のボタンから文章を送信。通常のDM入力はスタッフへ転送されません。')
        view=AppealButtons(self,c['id']) if c['action'] in ('caution','warn','timeout','kick','ban') else None
        try:
            m=await user.send(embed=e,view=view,allowed_mentions=NONE)
            c.update(dm='sent',dm_message=m.id)
        except (discord.HTTPException,TimeoutError,OSError):c['dm']='failed'
        self.db.save(c);self.db.event(c,'本人DM '+c['dm'],self.bot.user.id)
    async def appeal_channel(self,c,guild):
        if c.get('channel'):
            channel=await guild.fetch_channel(c['channel'])
            if not isinstance(channel,discord.TextChannel):raise ReceiptError('異議申し立て先が不正です。')
            return channel
        cfg=self.configured(guild.id)
        marker=f"moderation-appeal:{c['id']}"
        channels=await guild.fetch_channels()
        channel=next((x for x in channels if isinstance(x,discord.TextChannel) and x.topic==marker),None)
        if channel:
            c['channel']=channel.id;self.db.save(c);return channel
        if c.get('creating'):
            raise ReceiptError('チケット作成の結果を確認できていません。重複防止のため、管理者にケース番号を伝えてください。')
        await self.validate_config(guild,cfg['log'],cfg['category'],cfg['roles'])
        category=await guild.fetch_channel(cfg['category']);me=await guild.fetch_member(self.bot.user.id)
        overwrites={guild.default_role:discord.PermissionOverwrite(view_channel=False),me:discord.PermissionOverwrite(view_channel=True,send_messages=True,embed_links=True,read_message_history=True,manage_channels=True)}
        for rid in cfg['roles']:
            role=guild.get_role(rid)
            if role:overwrites[role]=discord.PermissionOverwrite(view_channel=True,send_messages=True,read_message_history=True)
        from .ticket_bridge import add_peer
        add_peer(guild,overwrites)
        c['creating']=time.time();self.db.save(c)
        try:channel=await guild.create_text_channel(f"appeal-{c['id']}",category=category,topic=marker,overwrites=overwrites,reason=f"異議申し立て #{c['id']}")
        except discord.HTTPException as error:
            if error.status in (400,403,404):c.pop('creating',None);self.db.save(c)
            raise
        c['channel']=channel.id;self.db.save(c)
        return channel
    async def relay(self,i,cid,text,staff=False):
        text=text.strip()
        if not text:raise ReceiptError('文章を入力してください。')
        async with self.lock:
            c=self.db.get(cid)
            if not c or c['state']!='applied' or c['action'] not in ('caution','warn','timeout','kick','ban'):raise ReceiptError('このケースでは異議申し立てを受け付けていません。')
            if c['appeal'] in ('closed','completed'):raise ReceiptError('この異議申し立ては対応終了しています。')
            guild=self.bot.get_guild(c['guild'])
            if not guild:raise ReceiptError('対象サーバーに接続できません。')
            if staff:
                await self.staff(i,c['guild'])
                if i.channel_id!=c.get('channel'):raise ReceiptError('このケースのチケット内で返信してください。')
            elif i.guild_id is not None or i.user.id!=c['user']:raise ReceiptError('処分された本人のDMから送信してください。')
            key='last_staff' if staff else 'last_user'
            if time.time()-c.get(key,0)<10:raise ReceiptError('連続送信は10秒待ってください。')
            channel=await self.appeal_channel(c,guild)
            if c['appeal']=='none':
                c['appeal']='open';self.db.save(c)
                self.db.event(c,'異議申し立て開始',i.user.id)
            # Persist the request before sending; uncertain sends are never blindly repeated.
            c[key]=time.time();self.db.save(c)
            self.db.event(c,'スタッフ返信受付' if staff else '本人から異議申し立て',i.user.id,text)
            e=discord.Embed(title=f"ケース #{cid}｜{'スタッフからの返信' if staff else '本人からの異議申し立て'}",description=text,color=0x5865F2)
            e.add_field(name='対象者',value=f"<@{c['user']}>（{c['user']}）")
            e.add_field(name='処分',value=LABELS[c['action']]);e.add_field(name='理由',value=c['reason'],inline=False)
            e.set_footer(text='返信はボタンから送信してください。通常のチャット入力は相手へ転送されません。')
            try:
                if staff:
                    user=await self.bot.fetch_user(c['user'])
                    await user.send(embed=e,view=AppealButtons(self,cid),allowed_mentions=NONE)
                    self.db.event(c,'スタッフ返信DM送信済み',i.user.id,text)
                    try:await channel.send(embed=e,allowed_mentions=NONE)
                    except discord.HTTPException:pass
                else:
                    roles=self.configured(guild.id)['roles']
                    await channel.send(' '.join(f'<@&{r}>' for r in roles),embed=e,view=AppealButtons(self,cid,True),allowed_mentions=discord.AllowedMentions(everyone=False,users=False,roles=[discord.Object(id=r) for r in roles]))
                    self.db.event(c,'本人メッセージ転送済み',i.user.id)
            except (discord.HTTPException,TimeoutError,OSError) as error:
                self.db.event(c,'転送失敗・結果要確認',i.user.id,type(error).__name__)
                return '文章は記録しましたが、相手への送信を確認できませんでした。スタッフはログを確認してください。DMを閉じている場合は届きません。'
            return '相手に送信しました。返信はこのケースのボタンから送れます。'
    async def appeal_action(self,i,cid,action):
        async with self.lock:
            c=self.db.get(cid,i.guild_id)
            if not c or i.channel_id!=c.get('channel'):raise ReceiptError('対象の異議申し立てチケット内で操作してください。')
            await self.staff(i)
            if c['appeal']=='closed':raise ReceiptError('すでにクローズしています。')
            states={'start':'working','done':'completed','close':'closed'}
            c['appeal']=states[action];c['assignee']=i.user.id;self.db.save(c)
            label={'start':'対応開始','done':'対応完了','close':'クローズ'}[action]
            self.db.event(c,'異議申し立て '+label,i.user.id)
            await i.channel.send(f"ケース #{cid}：{label}（担当 <@{i.user.id}>）。処分は自動解除されません。",allowed_mentions=NONE)
            try:
                user=await self.bot.fetch_user(c['user'])
                await user.send(f"{i.guild.name}：ケース #{cid}の異議申し立てが「{label}」になりました。処分の解除は別途スタッフが判断します。",allowed_mentions=NONE)
            except discord.HTTPException:self.db.event(c,'状態通知DM失敗',i.user.id)
    def history(self,guild,user,page):
        rows=self.db.cases(guild,user);pages=max(1,(len(rows)+9)//10)
        if page>pages:raise ReceiptError(f'ページは1～{pages}です。')
        e=discord.Embed(title='警告・処分履歴',description=f"<@{user}>（{user}）\n有効な警告：**{self.db.warnings(guild,user)}件**",color=0xE67E22)
        for c in rows[(page-1)*10:page*10]:
            state='取り消し済み' if c['revoked'] else c['state']
            e.add_field(name=f"#{c['id']} {LABELS[c['action']]} · {state}",value=f"<t:{int(c['created'])}:f> / 担当 <@{c['actor']}>\n{c['reason'][:300]}\nDM：{c['dm']} / 異議申し立て：{c['appeal']}"+(f" <#{c['channel']}>" if c['channel'] else ''),inline=False)
        e.set_footer(text=f'{page}/{pages}ページ ｜ /moderation history user:対象 page:番号')
        return e
    def public_stats(self,guild,user,page):
        rows=[c for c in self.db.cases(guild,user) if c['state']=='applied' and c['action']!='caution']
        pages=max(1,(len(rows)+9)//10)
        if page>pages:raise ReceiptError(f'ページは1～{pages}です。')
        warnings=self.db.warnings(guild,user)
        revoked=sum(c['action']=='warn' and c['revoked'] for c in rows)
        counts={a:sum(c['action']==a for c in rows) for a in ('timeout','kick','ban')}
        e=discord.Embed(title='WarnStats｜警告・処分履歴',description=f"<@{user}>\n有効な警告：**{warnings}件**（取り消し済み：{revoked}件）\nタイムアウト：{counts['timeout']}回 ／ Kick：{counts['kick']}回 ／ Ban：{counts['ban']}回\nこのサーバーで、このBOTが実行した履歴です。現在の制限状態を示すものではありません。",color=0xE67E22)
        for c in rows[(page-1)*10:page*10]:
            label=LABELS[c['action']]+('（取り消し済み・有効件数に含みません）' if c['revoked'] else '')
            e.add_field(name=f"#{c['id']} {label}",value=f"<t:{int(c['created'])}:f>\n{c['reason'][:300]}",inline=False)
        if not rows:e.add_field(name='履歴',value='公開対象の警告・処分履歴はありません。')
        e.set_footer(text=f'{page}/{pages}ページ ｜ 注意・異議申し立ての会話は公開されません。')
        return e

    def people(self,guild,page):
        records=self.db.cases(guild);grouped={}
        for c in records:
            if c['state']=='applied' and c['action'] in ('caution','warn','timeout','kick','ban'):
                grouped.setdefault(c['user'],[]).append(c)
        rows=list(grouped.items());pages=max(1,(len(rows)+14)//15)
        if page>pages:raise ReceiptError(f'ページは1～{pages}です。')
        e=discord.Embed(title='警告・処分された人の一覧',description=f'記録があるユーザー：{len(rows)}人\nこのBOTで実行した履歴です。現在のBan・タイムアウト状態とは異なります。',color=0xE67E22)
        for uid,cases in rows[(page-1)*15:page*15]:
            latest=cases[0]
            e.add_field(name=f'ユーザー {uid}',value=f"<@{uid}>｜有効な警告 {self.db.warnings(guild,uid)}件｜記録 {len(cases)}件\n直近 #{latest['id']} {LABELS[latest['action']]}",inline=False)
        e.set_footer(text=f'{page}/{pages}ページ ｜ /moderation list page:番号')
        return e
    async def flush(self):
        for eid,gid,cid,channel,raw,sent in self.db.db.execute("SELECT id,guild,case_id,channel,data,sent_at FROM moderation_events WHERE state='pending' AND guild != 900000000000000001 LIMIT 20").fetchall():
            if restricted(gid):continue
            try:
                if sent and time.time()-sent>120:
                    with self.db.db:self.db.db.execute("UPDATE moderation_events SET state='uncertain' WHERE id=?",(eid,))
                    continue
                ch=self.bot.get_channel(channel) or await self.bot.fetch_channel(channel)
                if ch.guild.id!=gid:raise ReceiptError('ログのサーバーが一致しません。')
                d=json.loads(raw)
                title = f"監査ログ {d['audit_id']}｜BOT外の処分" if d.get('audit_id') else f"ケース #{cid}｜{d['kind']}"
                e=discord.Embed(title=title,description=d['text'] or '記録',color=0xE67E22)
                actor = f"<@{d['actor']}>" if d.get('actor') else "監査ログ上で不明"
                e.add_field(name='対象',value=f"<@{d['user']}>");e.add_field(name='操作',value=f"{LABELS[d['action']]} / {actor}")
                e.add_field(name='日時',value=f"<t:{int(d['at'])}:F>")
                with self.db.db:self.db.db.execute('UPDATE moderation_events SET sent_at=? WHERE id=?',(sent or time.time(),eid))
                await self.bot.http.request(Route('POST','/channels/{channel_id}/messages',channel_id=channel),json={'embeds':[e.to_dict()],'allowed_mentions':{'parse':[]},'nonce':f'moderation:{eid}','enforce_nonce':True})
                with self.db.db:self.db.db.execute("UPDATE moderation_events SET state='sent' WHERE id=?",(eid,))
            except discord.HTTPException as error:
                if error.status in (403,404,429):
                    with self.db.db:self.db.db.execute('UPDATE moderation_events SET sent_at=0 WHERE id=?',(eid,))
                log.warning('Moderation log %s: %s',eid,type(error).__name__)
            except Exception as error:log.warning('Moderation log %s: %s',eid,type(error).__name__)
    async def start(self):
        self.bot.add_view(ModPanel(self))
        for (raw,) in self.db.db.execute('SELECT data FROM moderation_cases'):
            c=json.loads(raw)
            if c['state']=='pending':
                c['state']='uncertain';self.db.save(c);self.db.event(c,'再起動前の処分結果要確認',self.bot.user.id)
            if c['action'] in ('caution','warn','timeout','kick','ban'):
                self.bot.add_view(AppealButtons(self,c['id']))
                self.bot.add_view(AppealButtons(self,c['id'],True))
        self.worker.start()
        self.audit.worker.start()
    async def stop(self):
        self.worker.cancel()
        self.audit.worker.cancel()
    @tasks.loop(seconds=10)
    async def worker(self):
        try:
            await self.flush()
            await self.warning_roles.reconcile()
        except Exception as error:log.warning('Moderation worker: %s',type(error).__name__)
    @worker.before_loop
    async def before_worker(self):await self.bot.wait_until_ready()


def register_moderation(bot):
    service=bot.moderation
    group=app_commands.Group(name='moderation',description='警告・処分・異議申し立て・スタッフ履歴',guild_only=True)
    @group.command(name='setup',description='担当ロール・ログ先・異議申し立てカテゴリを設定')
    @app_commands.checks.has_permissions(administrator=True)
    async def setup(i:discord.Interaction,log_channel:discord.TextChannel,category:discord.CategoryChannel):
        if log_channel.guild.id!=i.guild_id or category.guild.id!=i.guild_id:raise ReceiptError('同じサーバー内で指定してください。')
        await i.response.send_message('担当ロールを選択すると設定を保存します。ログ先はスタッフ専用チャンネルを指定してください。異議申し立てチケットは本人を直接追加せず、DM経由でやり取りします。',view=SetupRoles(service,i.user.id,i.guild_id,log_channel.id,category.id),ephemeral=True)
    @group.command(name='log',description='処罰・異議申し立て履歴のログ先だけを変更')
    @app_commands.checks.has_permissions(administrator=True)
    async def log_channel(i:discord.Interaction,channel:discord.TextChannel):
        await i.response.defer(ephemeral=True)
        if channel.guild.id!=i.guild_id:raise ReceiptError('同じサーバーのチャンネルを指定してください。')
        me=await i.guild.fetch_member(bot.user.id)
        p=channel.permissions_for(me)
        if not(p.view_channel and p.send_messages and p.embed_links):raise ReceiptError('BOTにログ先の閲覧・送信・埋め込み権限が必要です。')
        service.db.configure(i.guild_id,log=channel.id)
        await i.followup.send(f'処罰ログ先を {channel.mention} に設定しました。今後の処分・異議申し立て本文・返信・対応状況を記録します。すでに配送待ちのログは元の送信先に届きます。',ephemeral=True,allowed_mentions=NONE)
    @group.command(name='settings' ,description='設定・文面・ログの配送状態を確認')
    async def settings(i:discord.Interaction):
        await i.response.defer(ephemeral=True);await service.staff(i)
        cfg=service.db.config(i.guild_id)
        states=dict(service.db.db.execute('SELECT state,COUNT(*) FROM moderation_events WHERE guild=? GROUP BY state',(i.guild_id,)).fetchall())
        await i.followup.send('設定：\nログ：'+(f"<#{cfg['log']}>" if cfg.get('log') else '未設定')+'\n異議申し立てカテゴリ：'+(f"<#{cfg['category']}>" if cfg.get('category') else '未設定')+'\n担当ロール：'+' '.join(f'<@&{r}>' for r in cfg.get('roles',[]))+f"\nログ送信待ち：{states.get('pending',0)}／結果要確認：{states.get('uncertain',0)}\n文面変更：/moderation text",ephemeral=True,allowed_mentions=NONE)
    @group.command(name='caution',description='軽い注意をDM・スタッフ履歴に記録（警告数やWarnStatsには出さない）')
    async def caution(i:discord.Interaction,user:discord.Member,reason:app_commands.Range[str,1,1000]):await service.stage(i,user,'caution',reason)
    @group.command(name='warn',description='警告を記録して本人へDM（自動処分なし）')
    async def warn(i:discord.Interaction,user:discord.Member,reason:app_commands.Range[str,1,1000]):await service.stage(i,user,'warn',reason)
    @group.command(name='timeout',description='期間を指定してタイムアウト・本人へDM')
    async def timeout(i:discord.Interaction,user:discord.Member,minutes:app_commands.Range[int,1,40320],reason:app_commands.Range[str,1,1000]):await service.stage(i,user,'timeout',reason,minutes)
    @group.command(name='kick',description='確認後にKick・実行前に本人へDM')
    async def kick(i:discord.Interaction,user:discord.Member,reason:app_commands.Range[str,1,1000]):await service.stage(i,user,'kick',reason)
    @group.command(name='ban',description='確認後にBan・実行前に本人へDM（過去メッセージ削除なし）')
    async def ban(i:discord.Interaction,reason:app_commands.Range[str,1,1000],user:discord.User=None,user_id:str=None):await service.stage(i,ban_target_input(user,user_id),'ban',reason)
    @group.command(name='untimeout',description='タイムアウト解除・記録・本人へDM')
    async def untimeout(i:discord.Interaction,user:discord.Member,reason:app_commands.Range[str,1,1000]):await service.stage(i,user,'untimeout',reason)
    @group.command(name='unban',description='Ban解除・記録・本人へDM（ユーザーID指定可）')
    async def unban(i:discord.Interaction,user:discord.User,reason:app_commands.Range[str,1,1000]):await service.stage(i,user,'unban',reason)
    @group.command(name='void',description='警告を取り消す（元の履歴は保存）')
    async def void(i:discord.Interaction,case_id:int,reason:app_commands.Range[str,1,1000]):
        await i.response.defer(ephemeral=True)
        guild,actor=await service.staff(i)
        c=service.db.get(case_id,guild.id)
        if not c:raise ReceiptError('このサーバーのケース番号を指定してください。')
        # Staff may not clear warnings on peers/superiors through a weaker path.
        try:target=await guild.fetch_member(c['user'])
        except discord.NotFound:target=await bot.fetch_user(c['user'])
        check_target(guild,actor,target,await guild.fetch_member(bot.user.id),'warn')
        async with service.lock:c=service.db.revoke(i.guild_id,case_id,actor.id,reason)
        role_status=await service.warning_roles.update(guild,c['user'])
        dm='送信済み'
        try:await target.send(f'{guild.name}：警告 #{case_id} が取り消されました。\n理由：{reason}',allowed_mentions=NONE)
        except discord.HTTPException:dm='送信失敗'
        await i.followup.send(f'警告を取り消しました。履歴は残ります。本人DM：{dm}\n{role_status}',ephemeral=True)
    @group.command(name='history',description='指定した人の警告数と処分履歴（本人だけに表示）')
    async def history(i:discord.Interaction,user:discord.User,page:app_commands.Range[int,1]=1):
        await i.response.defer(ephemeral=True);await service.staff(i)
        await i.followup.send(embed=service.history(i.guild_id,user.id,page),ephemeral=True,allowed_mentions=NONE)
    @group.command(name='list',description='警告・処分されたユーザー一覧（本人だけに表示）')
    async def listing(i:discord.Interaction,page:app_commands.Range[int,1]=1):
        await i.response.defer(ephemeral=True);await service.staff(i)
        await i.followup.send(embed=service.people(i.guild_id,page),ephemeral=True,allowed_mentions=NONE)
    @group.command(name='panel',description='警告・処分履歴のスタッフ用パネルを設置')
    @app_commands.checks.has_permissions(administrator=True)
    async def panel(i:discord.Interaction,channel:discord.TextChannel,title:app_commands.Range[str,1,100]='モデレーション管理パネル'):
        await i.response.defer(ephemeral=True)
        if channel.guild.id!=i.guild_id:raise ReceiptError('同じサーバー内で指定してください。')
        service.configured(i.guild_id)
        cfg=service.db.config(i.guild_id)
        text=render_template(cfg.get('texts',{}).get('panel',DEFAULTS['panel']),dict(server=i.guild.name,user='',reason='',until='',case='',warnings='',action=''))
        m=await channel.send(embed=discord.Embed(title=title,description=text,color=0xE67E22),view=ModPanel(service),allowed_mentions=NONE)
        await i.followup.send(f'パネルを設置しました：{m.jump_url}',ephemeral=True)
    @group.command(name='text',description='DM・パネルの文章をサーバーごとに変更')
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.choices(kind=[app_commands.Choice(name=LABELS.get(k,'パネル'),value=k) for k in DEFAULTS])
    async def text(i:discord.Interaction,kind:app_commands.Choice[str]):await i.response.send_modal(TextForm(service,i.guild_id,i.user.id,kind.value))
    @group.command(name='resend_dm',description='DM送信に失敗した処分通知を再送')
    async def resend(i:discord.Interaction,case_id:int):
        await i.response.defer(ephemeral=True);guild,actor=await service.staff(i)
        async with service.lock:
            c=service.db.get(case_id,guild.id)
            if not c or c['state']!='applied' or c['dm']=='sent' or c['revoked']:raise ReceiptError('実行済みかつDM未送信のケースを指定してください。')
            await service.notify(c,guild,await bot.fetch_user(c['user']))
            service.db.event(c,'DM再送操作',actor.id)
        await i.followup.send('DM状態：'+c['dm'],ephemeral=True)
    async def template_names(i:discord.Interaction,current:str):
        if not i.guild_id:return []
        cfg=service.db.config(i.guild_id)
        if not i.user.guild_permissions.administrator and not set(cfg.get('roles',[])).intersection(r.id for r in i.user.roles):return []
        return [app_commands.Choice(name=t['name'],value=t['name']) for t in service.db.templates(i.guild_id) if current.casefold() in t['name'].casefold()][:25]

    actions=[app_commands.Choice(name=LABELS[a],value=a) for a in ('caution','warn','timeout','kick','ban')]

    @group.command(name='template_add',description='サーバー専用の処分テンプレートを追加')
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.choices(action=actions)
    async def template_add(i:discord.Interaction,name:app_commands.Range[str,1,80],action:app_commands.Choice[str],reason:app_commands.Range[str,1,1000],minutes:app_commands.Range[int,0,40320]=0):
        t=service.db.save_template(i.guild_id,name,action.value,reason,minutes)
        await i.response.send_message(f"テンプレート「{t['name']}」を追加しました。処分は実行していません。",ephemeral=True,allowed_mentions=NONE)

    @group.command(name='template_edit',description='既存テンプレートを編集（指定した項目だけ変更）')
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.autocomplete(name=template_names)
    @app_commands.choices(action=actions)
    async def template_edit(i:discord.Interaction,name:str,action:app_commands.Choice[str]=None,reason:app_commands.Range[str,1,1000]=None,minutes:app_commands.Range[int,0,40320]=None):
        old=service.db.template(i.guild_id,name)
        kind=action.value if action else old['action']
        duration=minutes if minutes is not None else (old['minutes'] if kind=='timeout' else 0)
        service.db.save_template(i.guild_id,old['name'],kind,reason if reason is not None else old['reason'],duration,editing=True)
        await i.response.send_message('テンプレートを更新しました。過去の処分や確認待ちの内容は変更されません。',ephemeral=True)

    @group.command(name='template_delete',description='このサーバーのテンプレートを削除（処分履歴は残す）')
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.autocomplete(name=template_names)
    async def template_delete(i:discord.Interaction,name:str):
        t=service.db.delete_template(i.guild_id,name)
        await i.response.send_message(f"テンプレート「{t['name']}」を削除しました。既存の処分履歴はそのままです。",ephemeral=True,allowed_mentions=NONE)

    @group.command(name='template_list',description='このサーバーの処分テンプレート一覧')
    async def template_list(i:discord.Interaction,page:app_commands.Range[int,1]=1):
        await i.response.defer(ephemeral=True);await service.staff(i)
        rows=service.db.templates(i.guild_id);pages=max(1,(len(rows)+9)//10)
        if page>pages:raise ReceiptError(f'ページは1～{pages}です。')
        e=discord.Embed(title='処分テンプレート',description='実行：/moderation template_use',color=0xE67E22)
        for t in rows[(page-1)*10:page*10]:
            e.add_field(name=t['name'],value=f"{LABELS[t['action']]}"+(f"・{t['minutes']}分" if t['action']=='timeout' else '')+'\n'+t['reason'][:300],inline=False)
        if not rows:e.description='まだテンプレートはありません。管理者が /moderation template_add で追加できます。'
        e.set_footer(text=f'{page}/{pages}ページ')
        await i.followup.send(embed=e,ephemeral=True,allowed_mentions=NONE)

    @group.command(name='template_use',description='テンプレートを選び対象者と内容を確認して実行')
    @app_commands.autocomplete(name=template_names)
    @app_commands.describe(reason='今回だけ理由を上書き（保存済みテンプレートは変更しません）',minutes='今回だけ期間を上書き（タイムアウトのみ）')
    async def template_use(i:discord.Interaction,name:str,user:discord.Member,reason:app_commands.Range[str,1,1000]=None,minutes:app_commands.Range[int,1,40320]=None):
        t=service.db.template(i.guild_id,name)
        if minutes is not None and t['action']!='timeout':raise ReceiptError('期間を指定できるのはタイムアウトだけです。')
        await service.stage(i,user,t['action'],reason if reason is not None else t['reason'],minutes if minutes is not None else t['minutes'])

    bot.tree.add_command(group)

    @bot.tree.command(name='warnstats',description='このサーバーのメンバーの警告・処分履歴を確認')
    @app_commands.guild_only()
    async def warnstats(i:discord.Interaction,user:discord.Member,page:app_commands.Range[int,1]=1):
        await i.response.defer(ephemeral=True)
        if not i.guild or user.guild.id!=i.guild_id:raise ReceiptError('このサーバーのメンバーを選んでください。')
        await i.guild.fetch_member(i.user.id)
        await i.guild.fetch_member(user.id)
        await i.followup.send(embed=service.public_stats(i.guild_id,user.id,page),ephemeral=True,allowed_mentions=NONE)

