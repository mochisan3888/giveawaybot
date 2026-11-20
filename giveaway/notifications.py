import discord
from .redemptions import ReceiptError


def notification_payload(store, guild, channel, mode):
    if mode == 'none':
        return None, discord.AllowedMentions.none(), None
    permission = channel.permissions_for(guild.me)
    if mode == 'everyone':
        if not permission.mention_everyone:
            raise ReceiptError('BOTにこのチャンネルで @everyone をメンションする権限が必要です。')
        return '@everyone', discord.AllowedMentions(everyone=True, users=False, roles=False, replied_user=False), None
    if mode != 'role':
        raise ReceiptError('通知方法が不正です。')
    rid = store.notification_role(guild.id)
    role = guild.get_role(rid) if rid else None
    if role is None or role.is_default():
        raise ReceiptError('通知ロールが未設定または削除されています。/giveaway notification_role で設定してください。')
    if not role.mentionable and not permission.mention_everyone:
        raise ReceiptError('通知ロールをメンション可能にするか、BOTにロールメンション権限を付けてください。')
    return role.mention, discord.AllowedMentions(everyone=False, users=False, roles=[role], replied_user=False), role.id
