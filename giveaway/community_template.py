"""Trusted SSH jobs for Tormo; only the fixed, reviewed Community template is accepted."""
import hashlib
import json
import os
import re
import sqlite3
import time
from pathlib import Path

import discord
from discord.ext import tasks
from discord.http import Route
from .access_policy import restricted, giveaway_runtime
from .redemptions import ReceiptError

SOURCE=os.getenv('COMMUNITY_SOURCE_GUILD_ID','900000000000000003')
TABLES={'settings':'rules','receipt_profiles':'data','skelly_settings':'data','donation_settings':'data',
        'vip_settings':'data','notification_settings':None,'ticketlog_settings':None,'moderation_config':'data',
        'staff_room_settings':None,'ticket_ai_settings':None,'server_info':'data','message_log_config':None,
        'translation_config':None,'word_filter_config':'data','word_filter_rules':'data','moderation_templates':'data'}
SCHEMA='''CREATE TABLE IF NOT EXISTS community_deployments(
 id TEXT PRIMARY KEY,target TEXT NOT NULL UNIQUE,payload TEXT NOT NULL,state TEXT NOT NULL,
 progress TEXT NOT NULL,error TEXT,created REAL NOT NULL);
'''

def snowflake(v):return isinstance(v,int) and v>10**15 or isinstance(v,str) and v.isdigit() and int(v)>10**15

def identifiers(value):
    out=set()
    if snowflake(value):out.add(str(value))
    elif isinstance(value,dict):
        for k,v in value.items():out.update(identifiers(k));out.update(identifiers(v))
    elif isinstance(value,list):
        for v in value:out.update(identifiers(v))
    return out

def mapped(value,mapping):
    if snowflake(value):
        if str(value) not in mapping:raise ReceiptError('ID対応表が不足しています：'+str(value))
        return int(mapping[str(value)]) if isinstance(value,int) else mapping[str(value)]
    if isinstance(value,dict):return {mapped(k,mapping):mapped(v,mapping) for k,v in value.items()}
    if isinstance(value,list):return [mapped(v,mapping) for v in value]
    if isinstance(value,str):
        # Remap explicit Discord mentions and channel links, not arbitrary numbers in prose.
        for old,new in mapping.items():
            value=value.replace('<@&'+old+'>','<@&'+new+'>').replace('<#'+old+'>','<#'+new+'>')
        return value
    return value

def manifest(source):
    ids=set()
    for table,column in TABLES.items():
        for row in source['settings'].get(table,[]):
            data=dict(row)
            if table=='word_filter_rules':data.pop('id',None)
            if column:data[column]=json.loads(data[column])
            ids.update(identifiers(data))
    ids.update(p['channel_id'] for p in source['panels'])
    channels={str(c['id']) for c in source['channels']}
    roles={str(r['id']) for r in source['roles']}
    unknown=ids-channels-roles-{SOURCE}
    if unknown:raise ReceiptError('テンプレートに削除済み参照があります：'+','.join(sorted(unknown)))
    return dict(ok=True,version=1,source_guild_id=SOURCE,panels=[{k:p[k] for k in ('kind','channel_id','source_message_id')} for p in source['panels']],
                excluded_channel_ids=source.get('excluded_channel_ids',[]),required_channel_ids=sorted(ids & channels),required_role_ids=sorted((ids & roles)-{SOURCE}),warnings=source.get('warnings',[]))

def result(row):
    progress=json.loads(row[4])
    return dict(ok=True,deployment_id=row[0],target_guild_id=row[1],status=row[3],panel_message_ids=progress.get('panels',{}),warnings=progress.get('warnings',[]),error=row[5])

def rpc(root,p):
    root=Path(root);source=json.loads((root/'community-template-source.json').read_text())
    info=manifest(source)
    if str(p.get('source_guild_id',SOURCE))!=SOURCE:raise ValueError('Unsupported source')
    if p.get('action')=='export':
        info['protocol']={'command':'community-template','actions':['export','apply','status'],
            'apply_required':['deployment_id','source_guild_id','target_guild_id','actor_id','role_map','channel_map'],
            'id_format':'All IDs and map keys/values are decimal strings',
            'status_required':['deployment_id'],
            'states':['queued','running','completed','failed','uncertain'],
            'result_fields':['status','panel_message_ids','warnings','error'],
            'notes':'Fixed source snapshot; no client template. Actor must be owner/admin in both guilds. Re-submit identical apply to resume failed job. VIP excluded.'}
        return info
    did=p.get('deployment_id','')
    if not isinstance(did,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',did):raise ValueError('Invalid deployment_id')
    database=Path(os.getenv('DATABASE_PATH',str(root/'giveaway.sqlite3')))
    if database.parent.resolve()!=root.resolve():database=root/'giveaway.sqlite3'
    with sqlite3.connect(database,timeout=15) as db:
        db.executescript(SCHEMA)
        old=db.execute('SELECT * FROM community_deployments WHERE id=?',(did,)).fetchone()
        if p.get('action')=='status':return result(old) if old else dict(ok=False,error='Unknown deployment')
        if p.get('action')!='apply':raise ValueError('Unsupported action')
        target=str(p.get('target_guild_id',''));actor=str(p.get('actor_id',''))
        if not target.isdigit() or not actor.isdigit() or target==SOURCE or restricted(int(target)):raise ValueError('Invalid target or actor')
        clean=dict(deployment_id=did,source_guild_id=SOURCE,target_guild_id=target,actor_id=actor,template_hash=hashlib.sha256(json.dumps(source,sort_keys=True).encode()).hexdigest())
        for key,required in [('channel_map',info['required_channel_ids']),('role_map',info['required_role_ids'])]:
            mapping=p.get(key,{})
            if not isinstance(mapping,dict) or len(mapping)>1000 or not all(isinstance(k,str) and k.isdigit() and isinstance(v,str) and v.isdigit() for k,v in mapping.items()):raise ValueError('Invalid mapping')
            if any(k not in mapping for k in required):raise ValueError('Incomplete '+key)
            if len(set(mapping.values()))!=len(mapping):raise ValueError('Duplicate destination IDs')
            clean[key]=mapping
        serialized=json.dumps(clean,sort_keys=True)
        if old:
            if old[2]!=serialized:raise ValueError('Deployment payload conflict')
            if old[3]=='failed':db.execute("UPDATE community_deployments SET state='queued',error=NULL WHERE id=?",(did,))
        else:
            db.execute('INSERT INTO community_deployments VALUES(?,?,?,?,?,?,?)',(did,target,serialized,'queued',json.dumps({'panels':{},'warnings':info['warnings']}),None,time.time()))
        return result(db.execute('SELECT * FROM community_deployments WHERE id=?',(did,)).fetchone())

class CommunityTemplate:
    def __init__(self,bot):
        self.bot=bot;self.db=bot.store.db
        self.db.executescript(SCHEMA)
        # A send may have succeeded just before restart; never blindly resend.
        with self.db:self.db.execute("UPDATE community_deployments SET state='queued' WHERE state='running'")

    def save(self,did,progress):
        with self.db:self.db.execute('UPDATE community_deployments SET progress=? WHERE id=?',(json.dumps(progress),did))

    async def validate(self,p,source):
        if giveaway_runtime():raise ReceiptError('全機能モードで実行してください。')
        target=self.bot.get_guild(int(p['target_guild_id']));origin=self.bot.get_guild(int(SOURCE))
        if not target or not origin:raise ReceiptError('元と先の両サーバーにGiveAwayBotが必要です。')
        for guild in (origin,target):
            actor=await guild.fetch_member(int(p['actor_id']))
            if actor.id!=guild.owner_id and not actor.guild_permissions.administrator:raise ReceiptError('元と先の管理者権限が必要です。')
        roles={str(r.id) for r in await target.fetch_roles()}
        for cid in set(p['channel_map'].values()):
            ch=await self.bot.fetch_channel(int(cid))
            if ch.guild.id!=target.id:raise ReceiptError('別サーバーのチャンネルが指定されています。')
        if not set(p['role_map'].values())<=roles:raise ReceiptError('別サーバーのロールが指定されています。')
        if str(target.id) in p['role_map'].values() and p['role_map'].get(SOURCE)!=str(target.id):raise ReceiptError('everyoneへのロール統合は禁止です。')
        return target

    def settings(self,p,source):
        mapping={**p['role_map'],**p['channel_map'],SOURCE:p['target_guild_id']}
        rows=[]
        for table,column in TABLES.items():
            for raw in source['settings'].get(table,[]):
                raw=dict(raw)
                if table=='word_filter_rules':raw.pop('id',None)
                if column:raw[column]=json.loads(raw[column])
                row=mapped(raw,mapping)
                if table=='ticket_ai_settings':row['rules']=''
                if table=='message_log_config':row['generation']=os.urandom(16).hex()
                if table=='server_info':
                    row['data']['next_update']=0
                    for slot in row['data']['channels'].values():slot['last_edit']=0
                if column:row[column]=json.dumps(row[column])
                rows.append((table,row))
        return rows

    async def run_job(self,row):
        did=row[0];p=json.loads(row[2]);progress=json.loads(row[4])
        source=json.loads((Path(os.environ['DATABASE_PATH']).parent/'community-template-source.json').read_text())
        if hashlib.sha256(json.dumps(source,sort_keys=True).encode()).hexdigest()!=p['template_hash']:raise ReceiptError('テンプレート原本が変更されたため停止しました。')
        target=await self.validate(p,source)
        if not progress.get('settings'):
            rows=self.settings(p,source)
            # Never overwrite pre-existing destination configuration from another setup.
            for table in TABLES:
                if self.db.execute(f'SELECT 1 FROM {table} WHERE guild=? LIMIT 1',(target.id,)).fetchone():
                    raise ReceiptError('展開先に既存GiveAwayBot設定があります：'+table)
            with self.db:
                for table,data in rows:
                    columns=list(data)
                    self.db.execute(f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",list(data.values()))
                progress['settings']=True;self.save(did,progress)
        for panel in source['panels']:
            key=panel['source_message_id']
            if key in progress['panels']:continue
            channel=await target.fetch_channel(int(p['channel_map'][panel['channel_id']]))
            started=progress.setdefault('sending',{}).get(key)
            if started and time.time()-started>110:
                raise ReceiptError('投稿結果が不明です。重複を避けるため停止しました。管理者が実際の投稿を確認してください。')
            body,view=self.panel_body(panel,target,p)
            progress['sending'][key]=started or time.time();self.save(did,progress)
            nonce=hashlib.sha256((did+':'+key).encode()).hexdigest()[:24]
            message=await self.bot.http.request(Route('POST','/channels/{channel_id}/messages',channel_id=channel.id),json={**body,'allowed_mentions':{'parse':[]},'nonce':nonce,'enforce_nonce':True})
            mid=int(message['id'])
            with self.db:
                kind=panel['kind'];table={'skelly':'skelly_panels','donation':'donation_panels','vip':'vip_panels','vip_price':'vip_price_panels'}.get(kind)
                if table:
                    service=getattr(self.bot,'vip' if kind=='vip_price' else kind)
                    rev=service.price_display(target.id)[1] if kind=='vip_price' else service.db.config(target.id)['revision']
                    self.db.execute(f'INSERT INTO {table} VALUES(?,?,?,?)',(mid,target.id,channel.id,rev))
                progress['panels'][key]=str(mid);progress['sending'].pop(key,None);self.save(did,progress)
            if view:self.bot.add_view(view,message_id=mid)
        with self.db:self.db.execute("UPDATE community_deployments SET state='completed',error=NULL WHERE id=?",(did,))

    def panel_body(self,panel,guild,p):
        from .skelly import SkellyPanel,price_embed
        from .donation import DonationPanel
        from .vip import VipPanel,panel_embed
        from .fulfillment import MinecraftPanelView
        from .moderation import ModPanel
        kind=panel['kind'];view=None
        mapping={**p['role_map'],**p['channel_map'],SOURCE:str(guild.id)}
        body=mapped({'content':panel.get('content',''),'embeds':panel.get('embeds',[])},mapping)
        if kind=='skelly':
            cfg=self.bot.skelly.db.config(guild.id);view=SkellyPanel(self.bot.skelly,config=cfg);body['embeds']=[price_embed(cfg).to_dict()]
        elif kind=='donation':view=DonationPanel(self.bot.donation)
        elif kind=='vip':
            cfg=self.bot.vip.db.config(guild.id);view=VipPanel(self.bot.vip,cfg.get('accepting',True));body['embeds']=[panel_embed(cfg.get('accepting',True)).to_dict()]
        elif kind=='vip_price':body['embeds']=[self.bot.vip.price_display(guild.id)[0].to_dict()]
        elif kind=='minecraft':view=MinecraftPanelView(self.bot.fulfillment)
        elif kind=='moderation':view=ModPanel(self.bot.moderation)
        elif kind in ('member','support'):
            from .community_setup import NativePanel
            view=NativePanel(self.bot.community_setup,kind)
        else:raise ReceiptError('未知のパネルです。')
        if view:body['components']=view.to_components()
        return body,view

    async def start(self):self.worker.start()
    async def stop(self):self.worker.cancel()
    @tasks.loop(seconds=3)
    async def worker(self):
        row=self.db.execute("SELECT * FROM community_deployments WHERE state='queued' ORDER BY created LIMIT 1").fetchone()
        if not row:return
        with self.db:self.db.execute("UPDATE community_deployments SET state='running' WHERE id=?",(row[0],))
        try:await self.run_job(row)
        except Exception as e:
            error=str(e)[:500] if isinstance(e,ReceiptError) else type(e).__name__
            with self.db:self.db.execute('UPDATE community_deployments SET state=?,error=? WHERE id=?',('uncertain' if '投稿結果が不明' in error else 'failed',error,row[0]))
    @worker.before_loop
    async def before(self):await self.bot.wait_until_ready()
