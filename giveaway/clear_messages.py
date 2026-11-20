"""Bounded, channel-scoped deletion with fresh permissions and no history backfill."""
import datetime
import discord
from discord import app_commands
from .access_policy import restricted, giveaway_runtime
from .redemptions import ReceiptError


async def delete_selected(channel, messages, reason):
    """Only report confirmed deletions; older messages use Discord's single-delete API."""
    deleted=0
    cutoff=discord.utils.utcnow()-datetime.timedelta(days=14,minutes=-2)
    recent=[m for m in messages if m.created_at>cutoff]
    old=[m for m in messages if m.created_at<=cutoff]
    try:
        for offset in range(0,len(recent),100):
            batch=recent[offset:offset+100]
            if len(batch)>1:
                await channel.delete_messages(batch,reason=reason)
                deleted+=len(batch)
            else:old.extend(batch)
        for message in old:
            try:await message.delete()
            except discord.NotFound:continue
            deleted+=1
    except (discord.HTTPException,OSError,TimeoutError) as error:
        return deleted,type(error).__name__
    return deleted,None


def register_clear(bot):
    active=set()
    @bot.tree.command(name='clear',description='このチャンネルの直近メッセージを指定件数削除（1～1000）')
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.describe(count='削除するメッセージ数（1～1000）。BOT・ピン留めも含む')
    async def clear(i:discord.Interaction,count:app_commands.Range[int,1,1000]):
        await i.response.defer(ephemeral=True)
        if not i.guild_id or restricted(i.guild_id) or giveaway_runtime():raise ReceiptError('このサーバーでは利用できません。')
        if i.channel_id in active:raise ReceiptError('このチャンネルでは削除処理中です。少し待ってください。')
        active.add(i.channel_id)
        try:
            channel=await bot.fetch_channel(i.channel_id)
            if not isinstance(channel,(discord.TextChannel,discord.Thread)) or channel.guild.id!=i.guild_id:raise ReceiptError('サーバーのテキストチャンネルまたはスレッドで実行してください。')
            # Fetch fresh role definitions as well as current membership.
            guild=await bot.fetch_guild(i.guild_id)
            actor=await guild.fetch_member(i.user.id)
            me=await guild.fetch_member(bot.user.id)
            # Channel overwrite resolution uses the fresh guild role collection.
            permission_channel=await bot.fetch_channel(channel.parent_id) if isinstance(channel,discord.Thread) else channel
            if permission_channel.guild.id!=i.guild_id:raise ReceiptError('チャンネルを確認できません。')
            permission_channel.guild=guild
            for member,label in [(actor,'実行者'),(me,'BOT')]:
                perms=permission_channel.permissions_for(member)
                if not (perms.view_channel and perms.read_message_history and perms.manage_messages):
                    raise ReceiptError(label+'に「チャンネルを見る」「メッセージ履歴を読む」「メッセージの管理」権限が必要です。')
            if isinstance(channel,discord.Thread) and channel.is_private() and not permission_channel.permissions_for(actor).manage_threads:
                try:await channel.fetch_member(actor.id)
                except discord.HTTPException:raise ReceiptError('この非公開スレッドに参加している人だけ実行できます。') from None
            if isinstance(channel,discord.Thread) and (channel.archived or channel.locked):raise ReceiptError('アーカイブ・ロック中のスレッドは先に解除してください。')
            messages=[m async for m in channel.history(limit=count,before=discord.Object(id=i.id))]
            deleted,error=await delete_selected(channel,messages,f'/clear by {i.user.id}; request {i.id}; count {count}')
            text=f'{deleted}件の削除を確認しました。'
            if error:text+=' 処理途中に通信または権限エラーが発生しました。削除結果が未確認のメッセージが残る場合があります。チャンネルを確認してください。'
            elif len(messages)<count:text+=f'（取得できたメッセージは{len(messages)}件でした）'
            await i.followup.send(text,ephemeral=True,allowed_mentions=discord.AllowedMentions.none())
        finally:active.discard(i.channel_id)
