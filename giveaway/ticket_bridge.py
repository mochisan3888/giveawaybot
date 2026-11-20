"""Metadata-only cross-bot ticket registry and peer read access."""
import asyncio
import json
import logging
import os
import time
from pathlib import Path
import discord
from discord.ext import tasks
from .redemptions import ReceiptError
from .rotate_ai_key import atomic
from .ticketlog import TABLES

PEER = int(os.getenv('PEER_BOT_ID','900000000000000000'))
log = logging.getLogger(__name__)


def add_peer(guild, overwrites):
    # A managed bot role proves this is a member of this guild.
    if any(getattr(getattr(r,'tags',None),'bot_id',None)==PEER for r in getattr(guild,'roles',[])):
        member = discord.Object(id=PEER, type=discord.Member)
        existing = next((v for k,v in overwrites.items() if k.id==PEER),discord.PermissionOverwrite())
        existing.update(view_channel=True,read_message_history=True)
        overwrites[member] = existing
    return overwrites


def bridge_root():
    return Path(os.environ['TICKET_BRIDGE_DIR'])


def foreign():
    try:
        data=json.loads((bridge_root()/'ticket-bridge.json').read_text())
        s=data['tormo']
        if data['version']!=1 or s['version']!=1 or not -30 <= time.time()-s['updated_at'] <=180:
            raise ValueError()
        return s
    except (OSError,KeyError,ValueError,TypeError):
        raise ReceiptError('Tormoとの設定同期を確認できません。少し待って再実行してください。') from None


async def reserve(i):
    payload=dict(request_id=str(i.id),guild=str(i.guild_id),actor=str(i.user.id))
    if os.getenv('TICKET_AI_RESERVE_COMMAND'):
        argv=json.loads(os.environ['TICKET_AI_RESERVE_COMMAND'])
        proc=await asyncio.create_subprocess_exec(*argv,stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL)
        try:
            out,_=await asyncio.wait_for(proc.communicate(json.dumps(payload).encode()),20)
        except asyncio.TimeoutError:
            proc.kill();await proc.wait()
            raise ReceiptError('AI利用枠の確認がタイムアウトしました。') from None
        if proc.returncode:raise ReceiptError('AI利用枠を確認できません。')
        result=json.loads(out)
    else:
        from .ticket_bridge_rpc import reserve as local_reserve
        result=local_reserve(bridge_root()/'ticket-ai-limits.sqlite3',payload)
    if not result.get('ok'):raise ReceiptError(result.get('error','AI利用制限に達しました。'))


class TicketBridge:
    def __init__(self,bot):self.bot=bot;self.checked={}

    def snapshot(self):
        db=self.bot.store.db
        tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        tickets=[]
        for table in [*TABLES,'moderation_cases']:
            if table not in tables:continue
            for row in db.execute(f'SELECT data FROM {table}'):
                r=json.loads(row[0])
                if not r.get('guild') or not r.get('channel'):continue
                roles=r.get('policy',{}).get('roles',r.get('roles',[]))
                if table in ('moderation_cases','staff_rooms'):
                    roles=self.bot.moderation.db.config(r['guild']).get('roles',[])
                tickets.append(dict(guild=str(r['guild']),channel=str(r['channel']),roles=[str(x) for x in roles],kind=table,status='closed' if r.get('status')=='closed' or r.get('appeal')=='closed' else 'open'))
        settings={str(g):dict(enabled=bool(e),rules=r) for g,e,r in db.execute('SELECT guild,enabled,rules FROM ticket_ai_settings')}
        return dict(version=1,updated_at=time.time(),settings=settings,tickets=tickets)

    async def start(self):
        if os.getenv('TICKET_BRIDGE_DIR'):self.worker.start()

    async def stop(self):self.worker.cancel()

    @tasks.loop(seconds=15)
    async def worker(self):
        try:
            snapshot=self.snapshot()
            atomic(bridge_root()/'ticket-bridge-giveaway.json',snapshot)
            for r in snapshot['tickets']:
                cid=int(r['channel'])
                if time.time()-self.checked.get(cid,0)<300:continue
                guild=self.bot.get_guild(int(r['guild']))
                if not guild:continue
                try:
                    peer=await guild.fetch_member(PEER)
                    ch=await guild.fetch_channel(cid)
                    current=ch.overwrites_for(peer)
                    if current.view_channel is not True or current.read_message_history is not True:
                        current.update(view_channel=True,read_message_history=True)
                        await ch.set_permissions(peer,overwrite=current,reason='両BOTのチケットAI連携：閲覧・履歴のみ追加')
                    self.checked[cid]=time.time()
                except discord.NotFound:self.checked[cid]=time.time()
                except discord.HTTPException as e:
                    self.checked[cid]=time.time()
                    log.warning('Peer ticket access pending channel=%s status=%s',cid,e.status)
        except Exception as e:log.warning('Ticket registry update failed: %s',type(e).__name__)

    @worker.before_loop
    async def before(self):await self.bot.wait_until_ready()
