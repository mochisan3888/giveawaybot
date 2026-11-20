"""Owner-confirmed Community provisioning with durable per-resource checkpoints."""
import asyncio
import hashlib
import json
import os
import struct
import time
import zlib
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import tasks
from discord.http import Route
from .access_policy import restricted, giveaway_runtime
from .community_template import SOURCE, manifest, rpc
from .fulfillment import SafeView
from .redemptions import ReceiptError

RULES='900000000000000006'
STAFF='900000000000000010'
MEMBER='900000000000000004'
GUIDES=['900000000000000009','900000000000000011']
TYPES=['認証トラブル','Role / Channel問題','BOTの問題','運営への相談','処分確認','異議申立て','その他','Giveaway開催申請','パートナー相談']
NONE=discord.AllowedMentions.none()


def black_png():
    def chunk(kind,data):return struct.pack('!I',len(data))+kind+data+struct.pack('!I',zlib.crc32(kind+data)&0xffffffff)
    return b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('!2I5B',128,128,8,2,0,0,0))+chunk(b'IDAT',zlib.compress((b'\0'+b'\0'*384)*128))+chunk(b'IEND',b'')


def blueprint(source):
    excluded=set(source.get('excluded_channel_ids',[]))
    channels=[c for c in source['channels'] if c['id'] not in excluded and c.get('parent_id') not in excluded and c['type'] in (0,2,4,5,13,15)]
    roles=[r for r in source['roles'] if not r.get('managed') and r['id']!=SOURCE and not r['name'].startswith('警告')]
    return roles,sorted(channels,key=lambda c:(c['type']!=4,c['type']==15,c.get('position',0)))


def role_order_matches(roles, mapping, current):
    expected=[mapping[r['id']] for r in sorted(roles,key=lambda r:(r.get('position',0),-int(r['id'])))]
    actual=[r['id'] for r in sorted(current,key=lambda r:(r['position'],-int(r['id']))) if r['id'] in set(expected)]
    return actual==expected


def channel_payload(c,role_map,channel_map,bot_id):
    body={k:c[k] for k in ('name','type','position','nsfw','rate_limit_per_user') if k in c}
    if c['type'] in (0,5,15):body['topic']='' if c['id']==RULES else c.get('topic')
    if c.get('parent_id'):body['parent_id']=channel_map[c['parent_id']]
    if c['type'] in (2,13):body.update(bitrate=min(c.get('bitrate',64000),96000),user_limit=c.get('user_limit',0))
    if c['type']==15:
        body['available_tags']=[{'name':t['name'],'moderated':t.get('moderated',False),**({'emoji_name':t['emoji_name']} if t.get('emoji_name') else {})} for t in c.get('available_tags',[])]
        body['default_forum_layout']=c.get('default_forum_layout',0)
        if c.get('default_sort_order') is not None:body['default_sort_order']=c['default_sort_order']
    overwrites=[]
    for o in c.get('permission_overwrites',[]):
        if o['type']==0 and o['id'] in role_map:overwrites.append({**o,'id':role_map[o['id']]})
    # Keep the provisioning bot able to operate private channels. No member copies.
    overwrites.append({'id':str(bot_id),'type':1,'allow':str(discord.Permissions(view_channel=True,send_messages=True,read_message_history=True,embed_links=True,manage_channels=True).value),'deny':'0'})
    body['permission_overwrites']=overwrites
    return body


class NativePanel(SafeView):
    def __init__(self,service,kind):
        super().__init__(timeout=None);self.service,self.kind=service,kind
        labels=['認証する'] if kind=='member' else TYPES
        for index,label in enumerate(labels):
            button=discord.ui.Button(label=label,custom_id=f'community:{kind}:{index}',style=discord.ButtonStyle.success if kind=='member' else discord.ButtonStyle.secondary)
            async def click(i,index=index):
                await i.response.defer(ephemeral=True)
                if not service.valid_panel(i,kind):raise ReceiptError('このサーバーの登録済みパネルを使ってください。')
                if kind=='member':
                    data=service.get(i.guild_id);rid=data['roles'][MEMBER]
                    role=i.guild.get_role(int(rid));me=await i.guild.fetch_member(service.bot.user.id)
                    if not role or role.managed or role>=me.top_role or any(getattr(role.permissions,k) for k in ('administrator','manage_guild','manage_roles','manage_channels','kick_members','ban_members','moderate_members')):raise ReceiptError('認証ロールの設定・順位を確認してください。')
                    user=await i.guild.fetch_member(i.user.id)
                    await user.add_roles(role,reason='Community認証パネル')
                    await i.followup.send('認証しました。',ephemeral=True)
                else:
                    channel=await service.open_ticket(i,TYPES[index])
                    await i.followup.send(f'問い合わせチケット：<#{channel}>',ephemeral=True,allowed_mentions=NONE)
            button.callback=click;self.add_item(button)


class Confirm(SafeView):
    def __init__(self,service,owner,guild):
        super().__init__(timeout=180);self.service,self.owner,self.guild=service,owner,guild
    @discord.ui.button(label='このサーバーへ設置する',style=discord.ButtonStyle.danger)
    async def confirm(self,i,button):
        if i.user.id!=self.owner or i.guild_id!=self.guild:raise ReceiptError('開始した所有者だけ操作できます。')
        await i.response.defer(ephemeral=True)
        await self.service.preflight(i)
        self.service.queue(i)
        await i.edit_original_response(content='一括設置を開始しました。数分かかる場合があります。/community status で進捗を確認できます。',view=None)
        self.stop()


class CommunitySetup:
    def __init__(self,bot):
        self.bot,self.db=bot,bot.store.db;self.ticket_lock=asyncio.Lock()
        self.db.execute('CREATE TABLE IF NOT EXISTS community_setups(guild INTEGER PRIMARY KEY,data TEXT NOT NULL)');self.db.commit()
        for gid,raw in self.db.execute('SELECT guild,data FROM community_setups').fetchall():
            data=json.loads(raw)
            if data['state']=='running':data['state']='failed';data['error']='再起動しました。/community resume で続行できます。';self.save(gid,data)
    def source(self):return json.loads((Path(os.environ['DATABASE_PATH']).parent/'community-template-source.json').read_text())
    def get(self,gid):
        row=self.db.execute('SELECT data FROM community_setups WHERE guild=?',(gid,)).fetchone()
        return json.loads(row[0]) if row else None
    def save(self,gid,data):
        with self.db:self.db.execute('INSERT OR REPLACE INTO community_setups VALUES(?,?)',(gid,json.dumps(data)))
    async def authorize(self,i):
        if not i.guild or str(i.guild_id)==SOURCE or restricted(i.guild_id) or giveaway_runtime():raise ReceiptError('元サーバー以外の展開先で実行してください。')
        guild=await self.bot.fetch_guild(i.guild_id)
        if i.user.id!=guild.owner_id:raise ReceiptError('展開先のサーバー所有者だけ実行できます。')
        origin=self.bot.get_guild(int(SOURCE))
        if not origin:raise ReceiptError('元サーバーを確認できません。')
        member=await origin.fetch_member(i.user.id)
        if member.id!=origin.owner_id and not member.guild_permissions.administrator:raise ReceiptError('元サーバーの管理者権限も必要です。')
        me=await i.guild.fetch_member(self.bot.user.id)
        if not me.guild_permissions.administrator:raise ReceiptError('展開先でBOTに管理者権限が必要です。BOTロールを最上位に配置してください。')
        return me
    async def preflight(self,i):
        me=await self.authorize(i)
        if self.get(i.guild_id):raise ReceiptError('既に設置記録があります。/community status または resume を使ってください。')
        channels=await self.bot.http.get_all_guild_channels(i.guild_id)
        roles=await i.guild.fetch_roles()
        if len(channels)>3 or len([r for r in roles if not r.managed and not r.is_default()])>0:
            raise ReceiptError('初回は未使用に近いサーバー（既存チャンネル3個以下・独自ロールなし）で実行してください。既存チャンネルは削除しません。')
        source=self.source();info=manifest(source);normal,chs=blueprint(source)
        if len(chs)+len(channels)>500 or len(normal)+len(roles)>250:raise ReceiptError('Discordのチャンネル/ロール上限を超えます。')
        managed={str(r.tags.bot_id) : str(r.id) for r in roles if r.tags and r.tags.bot_id}
        available={r['id'] for r in normal}|{SOURCE}
        for r in source['roles']:
            if r.get('managed') and str(r.get('tags',{}).get('bot_id')) in managed:available.add(r['id'])
        if set(info['required_role_ids'])-available:raise ReceiptError('必須BOTロールを対応付けできません。両方のBOTを確認してください。')
        return normal,chs
    def queue(self,i):
        if self.get(i.guild_id):raise ReceiptError('既に開始されています。')
        source=self.source()
        data=dict(state='queued',actor=str(i.user.id),roles={SOURCE:str(i.guild_id)},channels={},messages={},pending=None,error=None,hash=hashlib.sha256(json.dumps(source,sort_keys=True).encode()).hexdigest(),warnings=source.get('warnings',[])+['メンバー・通常メッセージ・個人別権限・他BOT専用機能は移しません。認証と問い合わせはGiveAwayBotで動作します。'])
        self.save(i.guild_id,data)
    async def resource(self,gid,data,key,kind,call):
        if data.get('pending'):raise ReceiptError('作成結果未確認：'+json.dumps(data['pending'])+' /community recover で作成済みIDを登録してください。')
        data['pending']={'key':key,'kind':kind};self.save(gid,data)
        try:obj=await call()
        except discord.HTTPException as e:
            if 400<=e.status<500:data['pending']=None;self.save(gid,data)
            raise
        data[kind][key]=str(obj['id']);data['pending']=None;self.save(gid,data)
        return obj
    async def build(self,gid,data):
        source=self.source();guild=self.bot.get_guild(gid)
        if not guild:raise ReceiptError('展開先にBOTがいません。')
        i=type('Context',(),{'guild':guild,'guild_id':gid,'user':discord.Object(id=int(data['actor']))})()
        await self.authorize(i)
        if data['hash']!=hashlib.sha256(json.dumps(source,sort_keys=True).encode()).hexdigest():raise ReceiptError('原本が変わったため停止しました。')
        if data.get('pending'):raise ReceiptError('作成結果未確認です。/community recover で確認してください。')
        roles,channels=blueprint(source)
        current=await guild.fetch_roles()
        managed={str(r.tags.bot_id):str(r.id) for r in current if r.tags and r.tags.bot_id}
        if not data.get('managed_mapped'):
            for r in source['roles']:
                peer=str(r.get('tags',{}).get('bot_id'))
                if r.get('managed') and peer in managed:data['roles'][r['id']]=managed[peer]
            data['managed_mapped']=True
            self.save(gid,data)
        for r in sorted(roles,key=lambda r:(r.get('position',0),-int(r['id'])),reverse=True):
            if r['id'] in data['roles']:continue
            payload={k:r[k] for k in ('name','permissions','color','hoist','mentionable') if k in r}
            await self.resource(gid,data,r['id'],'roles',lambda payload=payload:self.bot.http.request(Route('POST','/guilds/{guild_id}/roles',guild_id=gid),json=payload,reason='Communityテンプレート設置'))
        if not data.get('role_order'):
            current=await self.bot.http.get_roles(gid)
            if not role_order_matches(roles,data['roles'],current):
                positions=[{'id':data['roles'][r['id']],'position':index+1} for index,r in enumerate(sorted(roles,key=lambda r:(r.get('position',0),-int(r['id']))))]
                try:
                    await self.bot.http.request(Route('PATCH','/guilds/{guild_id}/roles',guild_id=gid),json=positions,reason='Communityロール順序')
                except discord.HTTPException as error:
                    if error.code==50013:raise ReceiptError('GiveAwayBotロールを一度下へ動かして最上位へ戻し保存した後、/community resume を実行してください。') from None
                    raise
                current=await self.bot.http.get_roles(gid)
                if not role_order_matches(roles,data['roles'],current):raise ReceiptError('ロール順序を確認できません。BOTロールを最上位に保存してから再開してください。')
            data['role_order']=True;self.save(gid,data)
        for c in channels:
            if c['id'] in data['channels']:continue
            if c['type']==15 and not data.get('community'):
                await self.bot.http.request(Route('PATCH','/guilds/{guild_id}',guild_id=gid),json={'features':list(set(guild.features)|{'COMMUNITY'}),'rules_channel_id':data['channels'][RULES],'public_updates_channel_id':data['channels'][STAFF],'verification_level':max(guild.verification_level.value,1),'explicit_content_filter':2},reason='フォーラム設置用のCommunity設定')
                data['community']=True;self.save(gid,data)
            payload=channel_payload(c,data['roles'],data['channels'],self.bot.user.id)
            await self.resource(gid,data,c['id'],'channels',lambda payload=payload:self.bot.http.request(Route('POST','/guilds/{guild_id}/channels',guild_id=gid),json=payload,reason='Communityテンプレート設置'))
        if not data.get('identity'):
            everyone=next(r for r in source['roles'] if r['id']==SOURCE)
            await guild.default_role.edit(permissions=discord.Permissions(int(everyone['permissions'])),reason='Community基本権限')
            await guild.edit(name='Community',icon=black_png(),reason='Communityテンプレート設置')
            data['identity']=True;self.save(gid,data)
        if not data.get('order'):
            await self.bot.http.request(Route('PATCH','/guilds/{guild_id}/channels',guild_id=gid),json=[{'id':data['channels'][c['id']],'position':c['position']} for c in channels],reason='Communityチャンネル順序')
            data['order']=True;self.save(gid,data)
        result=rpc(Path(os.environ['DATABASE_PATH']).parent,dict(action='apply',deployment_id=f'community-{gid}',source_guild_id=SOURCE,target_guild_id=str(gid),actor_id=data['actor'],role_map=data['roles'],channel_map=data['channels']))
        data['state']='linking';self.save(gid,data)
    def valid_panel(self,i,kind):
        if not i.guild or restricted(i.guild_id) or giveaway_runtime():return False
        row=self.db.execute('SELECT progress FROM community_deployments WHERE target=?',(str(i.guild_id),)).fetchone()
        if not row:return False
        panels=json.loads(row[0]).get('panels',{})
        return any(p['kind']==kind and panels.get(p['source_message_id'])==str(i.message.id) for p in self.source()['panels'])
    async def open_ticket(self,i,subject):
        async with self.ticket_lock:
            store=self.bot.staffroom.db
            for r in store.all(i.guild_id):
                if r['user']==i.user.id and r['status']!='closed':
                    if r['channel']:return r['channel']
                    raise ReceiptError(f"前の作成結果が不明です。スタッフが /staffroom recover room_id:{r['id']} で確認してください。")
            state=self.get(i.guild_id);cfg=self.bot.moderation.db.config(i.guild_id)
            category=await self.bot.staffroom.category(i.guild,int(state['channels']['900000000000000007']))
            r,_=store.create(i.guild_id,i.user.id,[i.user.id],subject,category.id,cfg['roles'],i.id)
            channel=await i.guild.create_text_channel(f"相談-{r['id']}",category=category,topic=f"staff-room:{r['id']}",overwrites=await self.bot.staffroom.overwrites(i.guild,r),reason='Community問い合わせ')
            r.update(channel=channel.id,status='open',opened=time.time());store.save(r)
            from .staffroom import RoomControls
            await channel.send(f'<@{i.user.id}>\n{subject}\n相談内容をここに入力してください。担当スタッフが対応します。',view=RoomControls(self.bot.staffroom),allowed_mentions=NONE)
            return channel.id
    async def guides(self,gid,data):
        for old in GUIDES:
            if old not in data['channels'] or old in data['messages']:continue
            started=data.setdefault('sending',{}).get(old)
            if started and time.time()-started>110:raise ReceiptError('案内文の送信結果が不明です。重複防止のため停止しました。')
            data['sending'][old]=started or time.time();self.save(gid,data)
            embed=discord.Embed(title='Community｜コマンド案内',description='**メンバー向け**\n`/minecraft link` Minecraft連携\n`/minecraft status` 連携状態\n`/warnstats` 警告・処分履歴\n\n**運営向け**\n`/giveaway` 抽選・受け取り設定\n`/skelly` 売買受付・価格\n`/donation` 寄付チケット\n`/moderation` 処分・ログ設定\n`/staffroom` 相談チケット管理\n`/ticket` チケットログ・AI補助\n`/messagelog` 編集・削除ログ\n`/禁止ワード` 自動削除\n`/serverinfo refresh` 情報表示の更新\n`/community status` 一括設置状況\n\n認証・寄付・Skelly・問い合わせは各チャンネルのパネルから利用できます。VIPは今回対象外です。',color=0x303136)
            obj=await self.bot.http.request(Route('POST','/channels/{channel_id}/messages',channel_id=data['channels'][old]),json={'embeds':[embed.to_dict()],'allowed_mentions':{'parse':[]},'nonce':hashlib.sha256(f'guide:{gid}:{old}'.encode()).hexdigest()[:24],'enforce_nonce':True})
            data['messages'][old]=obj['id'];data['sending'].pop(old,None);self.save(gid,data)
    async def start(self):
        self.bot.add_view(NativePanel(self,'member'));self.bot.add_view(NativePanel(self,'support'))
        self.worker.start()
    async def stop(self):self.worker.cancel()
    @tasks.loop(seconds=3)
    async def worker(self):
        for gid,raw in self.db.execute('SELECT guild,data FROM community_setups').fetchall():
            data=json.loads(raw)
            if data['state'] not in ('queued','linking'):continue
            try:
                if data['state']=='queued':
                    data['state']='running';self.save(gid,data);await self.build(gid,data)
                else:
                    row=self.db.execute('SELECT state,error FROM community_deployments WHERE id=?',(f'community-{gid}',)).fetchone()
                    if not row:raise ReceiptError('連携ジョブが見つかりません。')
                    if row[0]=='completed':await self.guides(gid,data);data['state']='completed';data['error']=None;self.save(gid,data)
                    elif row[0] in ('failed','uncertain'):raise ReceiptError('パネル/設定適用：'+str(row[1]))
            except Exception as e:
                data['state']='failed';data['error']=str(e)[:1000] if isinstance(e,ReceiptError) else f'{type(e).__name__}: '+str(e)[:300];self.save(gid,data)
    @worker.before_loop
    async def before(self):await self.bot.wait_until_ready()


def register_community(bot):
    s=bot.community_setup
    group=app_commands.Group(name='community',description='Community構成を別サーバーに一括設置',guild_only=True,default_permissions=discord.Permissions(administrator=True))
    @group.command(name='setup',description='構成・パネル・ログ設定を一括設置（所有者専用）')
    async def setup(i:discord.Interaction):
        await i.response.defer(ephemeral=True);roles,channels=await s.preflight(i)
        text=f'**展開先：{i.guild.name}（{i.guild_id}）**\nロール {len(roles)}個・チャンネル {len(channels)}個を追加。名前をCommunity・アイコンを黒に変更します。\n認証・問い合わせ・Skelly・寄付・Minecraft・処罰一覧のパネル、案内文、ログ先を自動設定。フォーラム用にDiscord Community設定も有効化します。\nVIP・個別チケット・他BOT専用機能・メンバー・通常メッセージは対象外。個人別権限はコピーしません。ルール本文は空欄。既存チャンネルは削除しません。'
        await i.followup.send(text,view=Confirm(s,i.user.id,i.guild_id),ephemeral=True,allowed_mentions=NONE)
    @group.command(name='status',description='一括設置の進捗・エラーを確認')
    async def status(i:discord.Interaction):
        await i.response.defer(ephemeral=True);await s.authorize(i);d=s.get(i.guild_id)
        text='まだ設置していません。' if not d else f"状態：{d['state']}\nロール対応 {len(d['roles'])} / チャンネル {len(d['channels'])}\nエラー：{d.get('error') or 'なし'}\n"+'\n'.join(d['warnings'])
        await i.followup.send(text[:1900],ephemeral=True,allowed_mentions=NONE)
    @group.command(name='resume',description='途中で止まった設置を続行（作成済みは重複させない）')
    async def resume(i:discord.Interaction):
        await i.response.defer(ephemeral=True);await s.authorize(i);d=s.get(i.guild_id)
        if not d or d['state']!='failed':raise ReceiptError('失敗した設置記録がありません。')
        if d.get('pending'):raise ReceiptError('作成結果未確認です。先に /community recover を実行してください。')
        d['state']='queued';d['error']=None;s.save(i.guild_id,d)
        await i.followup.send('再開しました。/community status で確認できます。',ephemeral=True)
    @group.command(name='recover',description='通信切断などで結果不明の作成済みロール/チャンネルIDを登録')
    async def recover(i:discord.Interaction,object_id:str):
        await i.response.defer(ephemeral=True);await s.authorize(i);d=s.get(i.guild_id)
        if not d or d['state']!='failed' or not d.get('pending') or not object_id.isdigit():raise ReceiptError('復旧対象がありません。')
        pending=d['pending'];source=s.source();items=source['roles'] if pending['kind']=='roles' else source['channels'];old=next(x for x in items if x['id']==pending['key'])
        if pending['kind']=='roles':
            obj=next((r for r in await i.guild.fetch_roles() if str(r.id)==object_id),None)
            if not obj or obj.managed or obj.is_default() or obj.name!=old['name']:raise ReceiptError('作成したロールと一致しません。')
        else:
            obj=await i.guild.fetch_channel(int(object_id))
            if obj.name!=old['name'] or obj.type.value!=old['type']:raise ReceiptError('作成したチャンネルと一致しません。')
        if object_id in d[pending['kind']].values():raise ReceiptError('既に他の対象に紐づいています。')
        d[pending['kind']][pending['key']]=object_id;d['pending']=None;s.save(i.guild_id,d)
        await i.followup.send('復旧しました。/community resume で再開してください。',ephemeral=True)
    bot.tree.add_command(group)
