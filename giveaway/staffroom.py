"""Private staff-created group tickets, with explicit membership and persistent controls."""
import asyncio
import json
import time
import discord
from discord import app_commands
from .fulfillment import SafeView
from .redemptions import ReceiptError

NONE=discord.AllowedMentions.none()


class RoomStore:
    def __init__(self,db):
        self.db=db
        db.executescript('''
        CREATE TABLE IF NOT EXISTS staff_rooms(id INTEGER PRIMARY KEY AUTOINCREMENT,guild INTEGER NOT NULL,request TEXT UNIQUE NOT NULL,data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS staff_room_settings(guild INTEGER PRIMARY KEY,category INTEGER NOT NULL);
        ''')
    def config(self,guild):
        r=self.db.execute('SELECT category FROM staff_room_settings WHERE guild=?',(guild,)).fetchone()
        return r[0] if r else None
    def configure(self,guild,category):
        with self.db:self.db.execute('INSERT OR REPLACE INTO staff_room_settings VALUES(?,?)',(guild,category))
    def all(self,guild):
        return [json.loads(r[0]) for r in self.db.execute('SELECT data FROM staff_rooms WHERE guild=? ORDER BY id DESC',(guild,))]
    def get(self,guild,rid):
        row=self.db.execute('SELECT data FROM staff_rooms WHERE guild=? AND id=?',(guild,rid)).fetchone()
        if not row:raise ReceiptError('このサーバーの相談チケットが見つかりません。')
        return json.loads(row[0])
    def save(self,r):
        with self.db:self.db.execute('UPDATE staff_rooms SET data=? WHERE id=? AND guild=?',(json.dumps(r),r['id'],r['guild']))
    def create(self,guild,user,participants,subject,category,roles,request):
        old=self.db.execute('SELECT data FROM staff_rooms WHERE request=?',(str(request),)).fetchone()
        if old:return json.loads(old[0]),False
        if not 1<=len(set(participants))<=10:raise ReceiptError('招待するメンバーは1～10人です。')
        if not 1<=len(subject.strip())<=100:raise ReceiptError('件名は1～100文字で入力してください。')
        r=dict(guild=guild,user=user,participants=list(dict.fromkeys(participants)),subject=subject.strip(),category=category,roles=roles,status='creating',channel=None,created=time.time(),history=[])
        with self.db:
            cursor=self.db.execute('INSERT INTO staff_rooms(guild,request,data) VALUES(?,?,?)',(guild,str(request),'{}'))
            r['id']=cursor.lastrowid;self.save(r)
        return r,True


def room_overwrites(guild,me,creator,participants,roles,closed=False):
    rw=not closed
    result={guild.default_role:discord.PermissionOverwrite(view_channel=False),me:discord.PermissionOverwrite(view_channel=True,send_messages=True,read_message_history=True,embed_links=True,manage_channels=True)}
    for target in [*roles,creator,*participants]:
        if target is None or target.id in (guild.id,me.id):continue
        result[target]=discord.PermissionOverwrite(view_channel=True,read_message_history=True,send_messages=rw,send_messages_in_threads=rw,create_public_threads=False,create_private_threads=False,attach_files=rw,embed_links=rw)
    from .ticket_bridge import add_peer
    return add_peer(guild,result)


class RoomControls(SafeView):
    def __init__(self,service):super().__init__(timeout=None);self.service=service
    @discord.ui.button(label='会話を残して閉じる',custom_id='staffroom:close:v1',style=discord.ButtonStyle.secondary)
    async def close_room(self,i,button):
        await i.response.defer(ephemeral=True)
        r=await self.service.change(i,'close')
        await i.followup.send(f"相談チケット #{r['id']} を閉じました。通常メンバーは閲覧のみになります。",ephemeral=True)


class RoomSelection(SafeView):
    def __init__(self,service,i,subject,category):
        super().__init__(timeout=180)
        self.service,self.owner,self.guild,self.request=service,i.user.id,i.guild_id,i.id
        self.subject,self.category,self.members,self.used=subject,category,[],False
    def check(self,i):
        if i.user.id!=self.owner or i.guild_id!=self.guild:raise ReceiptError('作成を開始したスタッフ本人だけが操作できます。')
        if self.used:raise ReceiptError('すでに作成を受け付けています。/staffroom list で確認してください。')
    @discord.ui.select(cls=discord.ui.UserSelect,placeholder='話したいメンバーを1～10人選択',min_values=1,max_values=10)
    async def select(self,i,select):
        self.check(i);self.members=list(dict.fromkeys(u.id for u in select.values))
        await i.response.defer()
    @discord.ui.button(label='非公開チケットを作成',style=discord.ButtonStyle.success)
    async def create(self,i,button):
        self.check(i)
        if not self.members:raise ReceiptError('招待メンバーを選択してください。')
        await i.response.defer(ephemeral=True);self.used=True
        r=await self.service.create(i,self.members,self.subject,self.category,self.request)
        await i.followup.send(f"相談チケットを作成しました：<#{r['channel']}>",ephemeral=True,allowed_mentions=NONE)
        self.stop()


class StaffRooms:
    def __init__(self,bot):self.bot=bot;self.db=RoomStore(bot.store.db);self.lock=asyncio.Lock()
    async def category(self,guild,cid):
        channel=await guild.fetch_channel(cid)
        if not isinstance(channel,discord.CategoryChannel):raise ReceiptError('チケット作成先にはカテゴリを指定してください。')
        me=await guild.fetch_member(self.bot.user.id)
        p=channel.permissions_for(me)
        if not(p.view_channel and p.manage_channels):raise ReceiptError('BOTにカテゴリの閲覧・チャンネル管理権限が必要です。')
        return channel
    async def overwrites(self,guild,r):
        me=await guild.fetch_member(self.bot.user.id)
        members={}
        for uid in dict.fromkeys([r['user'],*r['participants']]):
            try:members[uid]=await guild.fetch_member(uid)
            except discord.NotFound:continue
        cfg=self.bot.moderation.db.config(guild.id)
        roles=[guild.get_role(rid) for rid in cfg.get('roles',[])]
        return room_overwrites(guild,me,members.get(r['user']),[members[u] for u in r['participants'] if u in members],roles,r['status']=='closed')
    async def create(self,i,members,subject,category,request):
        async with self.lock:
            guild,actor=await self.bot.moderation.staff(i)
            cfg=self.bot.moderation.db.config(guild.id)
            if not cfg.get('roles'):raise ReceiptError('先に /moderation setup で担当ロールを設定してください。')
            cid=category or self.db.config(guild.id) or cfg.get('category')
            if not cid:raise ReceiptError('/staffroom setup で作成先カテゴリを設定してください。')
            cat=await self.category(guild,cid)
            for uid in members:
                m=await guild.fetch_member(uid)
                if m.bot:raise ReceiptError('招待対象はBOT以外のメンバーを選択してください。')
            r,new=self.db.create(guild.id,actor.id,members,subject,cid,cfg['roles'],request)
            if not new:
                if r['channel']:return r
                raise ReceiptError(f"作成結果が未確認です。/staffroom recover room_id:{r['id']} で確認してください。")
            try:
                ch=await guild.create_text_channel(f"staff-room-{r['id']}",category=cat,topic=f"staff-room:{r['id']}",overwrites=await self.overwrites(guild,r),reason=f'スタッフ相談チケット / {actor.id}')
            except (discord.HTTPException,TimeoutError,OSError):
                raise ReceiptError(f"作成結果を確認できませんでした。/staffroom recover room_id:{r['id']} で確認してください。")
            r.update(channel=ch.id,status='open',opened=time.time());self.db.save(r)
            await self.welcome(ch,r)
            return r
    async def welcome(self,ch,r):
        e=discord.Embed(title='スタッフ相談チケット',description=r['subject'],color=0x5865F2)
        e.add_field(name='作成スタッフ',value=f"<@{r['user']}>")
        e.add_field(name='招待メンバー',value=' '.join(f'<@{u}>' for u in r['participants']),inline=False)
        e.set_footer(text='指定メンバー・担当スタッフ・サーバー管理者が閲覧できます。スタッフは /staffroom add・remove でメンバーを変更できます。')
        try:
            await ch.send(' '.join(f'<@{u}>' for u in dict.fromkeys([r['user'],*r['participants']])),embed=e,view=RoomControls(self),allowed_mentions=discord.AllowedMentions(everyone=False,roles=False,users=[discord.Object(id=u) for u in dict.fromkeys([r['user'],*r['participants']])]))
        except (discord.HTTPException,TimeoutError,OSError):
            # Channel creation succeeded; command close remains available even without buttons.
            pass
    async def change(self,i,action,user=None):
        async with self.lock:
            guild,actor=await self.bot.moderation.staff(i)
            r=next((r for r in self.db.all(guild.id) if r['channel']==i.channel_id),None)
            if not r:raise ReceiptError('スタッフ相談チケットの中で実行してください。')
            if r['status']=='closed' and action!='sync':raise ReceiptError('このチケットは閉じています。')
            if action in ('add','remove'):
                member=await guild.fetch_member(user.id)
                if member.bot:raise ReceiptError('BOTは対象にできません。')
                if action=='add':
                    if member.id in r['participants']:raise ReceiptError('すでに参加しています。')
                    if len(r['participants'])>=10:raise ReceiptError('招待メンバーは最大10人です。')
                    r['participants'].append(member.id)
                else:
                    roles=self.bot.moderation.db.config(guild.id).get('roles',[])
                    if member.id==r['user'] or member.guild_permissions.administrator or set(roles).intersection(x.id for x in member.roles):raise ReceiptError('作成スタッフ・担当ロール・管理者のアクセスは参加者削除では解除できません。')
                    if member.id not in r['participants']:raise ReceiptError('招待メンバーに登録されていません。')
                    r['participants'].remove(member.id)
            elif action=='close':r['status']='closed'
            r['sync_pending']=True
            r['history'].append(dict(action=action+'_request',by=actor.id,at=time.time(),target=user.id if user else None))
            self.db.save(r)
            try:await i.channel.edit(overwrites=await self.overwrites(guild,r),reason=f'スタッフ相談チケット {action} / {actor.id}')
            except (discord.HTTPException,TimeoutError,OSError):raise ReceiptError('変更を保存しましたが、権限反映を確認できません。/staffroom sync で再反映してください。')
            r['sync_pending']=False
            r['history'].append(dict(action=action,by=actor.id,at=time.time(),target=user.id if user else None))
            self.db.save(r)
            try:await i.channel.send({'add':'参加メンバーを追加しました。','remove':'参加メンバーを外しました。','close':'このチケットは閉じられました。会話は閲覧用に残ります。','sync':'参加権限を再反映しました。'}[action]+(f' 対象：<@{user.id}>' if user else ''),allowed_mentions=NONE)
            except discord.HTTPException:pass
            return r
    async def recover(self,i,rid):
        async with self.lock:
            guild,actor=await self.bot.moderation.staff(i)
            r=self.db.get(guild.id,rid)
            if r['channel']:return r
            channels=await guild.fetch_channels()
            ch=next((ch for ch in channels if isinstance(ch,discord.TextChannel) and ch.topic==f'staff-room:{rid}'),None)
            if not ch:raise ReceiptError('作成済みチャンネルは見つかりません。重複防止のため自動再作成はしていません。時間を置いて確認し、存在しない場合のみcreateをやり直してください。')
            r.update(channel=ch.id,status='open',opened=time.time());self.db.save(r)
            await ch.edit(overwrites=await self.overwrites(guild,r),reason='スタッフ相談チケット作成結果の復旧')
            await self.welcome(ch,r)
            return r
    async def start(self):self.bot.add_view(RoomControls(self))


def register_staffroom(bot):
    service=bot.staffroom
    group=app_commands.Group(name='staffroom',description='運営が指定メンバーとの非公開相談チケットを作成',guild_only=True)
    @group.command(name='setup',description='スタッフ相談チケットの作成先カテゴリを設定')
    @app_commands.checks.has_permissions(administrator=True)
    async def setup(i:discord.Interaction,category:discord.CategoryChannel):
        await i.response.defer(ephemeral=True)
        if category.guild.id!=i.guild_id:raise ReceiptError('このサーバー内のカテゴリを選択してください。')
        await service.category(i.guild,category.id);service.db.configure(i.guild_id,category.id)
        await i.followup.send(f'作成先を {category.mention} に設定しました。担当ロールは /moderation setup の設定を使用します。',ephemeral=True)
    @group.command(name='create',description='メンバーを選んで非公開チケットを作成')
    async def create(i:discord.Interaction,subject:app_commands.Range[str,1,100],category:discord.CategoryChannel=None):
        await i.response.defer(ephemeral=True);await bot.moderation.staff(i)
        if category and category.guild.id!=i.guild_id:raise ReceiptError('同じサーバーのカテゴリを指定してください。')
        await i.followup.send('招待するメンバーを選び、「非公開チケットを作成」を押してください。指定メンバー・作成スタッフ・担当ロール・管理者が閲覧できます。',view=RoomSelection(service,i,subject,category.id if category else None),ephemeral=True)
    @group.command(name='add',description='この相談チケットにメンバーを追加')
    async def add(i:discord.Interaction,user:discord.Member):
        await i.response.defer(ephemeral=True);await service.change(i,'add',user)
        await i.followup.send('追加しました。',ephemeral=True)
    @group.command(name='remove',description='この相談チケットからメンバーを外す')
    async def remove(i:discord.Interaction,user:discord.Member):
        await i.response.defer(ephemeral=True);await service.change(i,'remove',user)
        await i.followup.send('参加者から外しました。',ephemeral=True)
    @group.command(name='close',description='会話を残して相談チケットを閉じる')
    async def close(i:discord.Interaction):
        await i.response.defer(ephemeral=True);await service.change(i,'close')
        await i.followup.send('閉じました。通常メンバーは閲覧のみになります。',ephemeral=True)
    @group.command(name='sync',description='参加メンバー・最新の担当ロールの権限を再反映')
    async def sync(i:discord.Interaction):
        await i.response.defer(ephemeral=True);await service.change(i,'sync')
        await i.followup.send('権限を再反映しました。',ephemeral=True)
    @group.command(name='list',description='このサーバーのスタッフ相談チケット一覧')
    async def listing(i:discord.Interaction,page:app_commands.Range[int,1]=1):
        await i.response.defer(ephemeral=True);await bot.moderation.staff(i)
        rows=service.db.all(i.guild_id);pages=max(1,(len(rows)+9)//10)
        if page>pages:raise ReceiptError(f'ページは1～{pages}です。')
        e=discord.Embed(title='スタッフ相談チケット',color=0x5865F2)
        for r in rows[(page-1)*10:page*10]:e.add_field(name=f"#{r['id']} {r['subject']}",value=f"{r['status']} / "+(f"<#{r['channel']}>" if r['channel'] else '作成結果未確認')+(' / 権限再反映待ち' if r.get('sync_pending') else ''),inline=False)
        e.set_footer(text=f'{page}/{pages}ページ')
        await i.followup.send(embed=e,ephemeral=True,allowed_mentions=NONE)
    @group.command(name='recover',description='通信エラーで結果不明になったチケットを確認・復旧')
    async def recover(i:discord.Interaction,room_id:int):
        await i.response.defer(ephemeral=True);r=await service.recover(i,room_id)
        await i.followup.send(f"チケット：<#{r['channel']}>",ephemeral=True,allowed_mentions=NONE)
    bot.tree.add_command(group)
