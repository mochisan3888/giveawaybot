"""Operator-enforced guild restrictions, independent of server administrator roles."""
import asyncio
import os
import logging
import discord
GIVEAWAY_ONLY = frozenset({900000000000000001})
MESSAGE = 'このサーバーでは、BOT運営によりGiveaway以外の機能が停止されています。'
def restricted(guild):
    return int(guild or 0) in GIVEAWAY_ONLY

def giveaway_runtime():
    return os.getenv('BOT_RUNTIME_MODE') == 'giveaway'

def blocked(bot, data):
    gid=data.get('guild_id')
    details=data.get('data',{})
    custom=details.get('custom_id','')
    if not gid:
        parts=custom.split(':')
        if len(parts)>=3 and parts[1].isdigit():
            if parts[0]=='moderation':
                record=bot.moderation.db.get(int(parts[1]))
            elif parts[0]=='receipt':
                record=bot.fulfillment.db.get(int(parts[1]))
            else:record=None
            gid=record.get('guild') if record else None
    if not restricted(gid) and giveaway_runtime():
        if data.get('type') in (2,4):
            name=details.get('name')
            if name=='giveaway':return False
            return name not in {'vipgiveaway','ticket'}
        return not custom.startswith(('giveaway:','receipt:','receipt-config:','vipgiveaway:'))
    if not restricted(gid):return False
    if data.get('type') in (2,4):return details.get('name')!='giveaway'
    return custom not in {'giveaway:join:v1','giveaway:leave:v1','giveaway:status:v1'}

def install(bot):
    original=bot._connection.parsers['INTERACTION_CREATE']
    pending=set()
    async def reject(data):
        try:
            i=discord.Interaction(data=data,state=bot._connection)
            if data.get('type')==4:await i.response.autocomplete([])
            else:await i.response.send_message(MESSAGE if restricted(data.get('guild_id')) else '現在はGiveaway・受け取りチケットのみ稼働しています。その他の機能は一時停止中です。',ephemeral=True,allowed_mentions=discord.AllowedMentions.none())
        except Exception as error:logging.getLogger(__name__).warning('Restricted interaction reply: %s',type(error).__name__)
    def dispatch(data):
        if blocked(bot,data):
            task=asyncio.create_task(reject(data));pending.add(task);task.add_done_callback(pending.discard)
        else:original(data)
    bot._connection.parsers['INTERACTION_CREATE']=dispatch


def auto_auth_paused():
    return giveaway_runtime() or os.getenv("MC_AUTO_AUTH_PAUSED") == "1"
