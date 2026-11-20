from decimal import Decimal
import discord
from .redemptions import ReceiptError
from .skelly_store import price


def validate_prices(tiers, prices):
    known = [Decimal(prices[str(r)]) for r in tiers if str(r) in prices]
    if any(a >= b for a,b in zip(known,known[1:])):
        raise ReceiptError('上位VIPの価格は下位VIPより高く設定してください。')


def quote(config, member, target):
    tiers, prices = config.get('tiers',[]), config.get('prices',{})
    owned = {r.id for r in member.roles}
    current = next((r for r in reversed(tiers) if r in owned), None)
    amount = prices.get(str(target))
    base = prices.get(str(current)) if current else '0'
    if amount is None or base is None:return None
    difference = Decimal(amount) - Decimal(base)
    if difference <= 0:raise ReceiptError('VIP料金設定を確認してください。上位VIPの価格が現在のVIP以下です。')
    return dict(current=current,target=target,base=base,price=amount,due=format(difference.normalize(),'f'))


def price_embed(config, normal_rules=None):
    normal_rules = normal_rules or {}
    e=discord.Embed(title='👑 VIP PRICE LIST',description='━━━━━━━━━━━━━━━━━━━━\n**DonutSMP ゲーム内通貨で、特別な体験を。**\n━━━━━━━━━━━━━━━━━━━━',color=0xD4AF37)
    tiers,prices=config.get('tiers',[]),config.get('prices',{})
    for r in tiers:
        text = f"<@&{r}>\n**{prices[str(r)]+'m' if str(r) in prices else '料金未設定'}**"
        text += f"\n通常Giveaway：**{normal_rules.get(str(r), 1):g}倍**"
        text += f"\nVIP Giveaway：**{config.get('giveaway_rules', {}).get(str(r), 1):g}倍**"
        benefits = config.get('benefits', {}).get(str(r))
        if benefits:
            text += '\n\n' + benefits
        e.add_field(name='✦ VIPランク',value=text,inline=False)
    lines=[]
    for lower,upper in zip(tiers,tiers[1:]):
        if str(lower) in prices and str(upper) in prices:
            delta=format((Decimal(prices[str(upper)])-Decimal(prices[str(lower)])).normalize(),'f')
            lines.append(f'<@&{lower}> → <@&{upper}>：**{delta}m**')
    e.add_field(name='✧ ランクアップ',value='上位VIP価格 − 所持する最上位VIP価格の差額。\n'+('\n'.join(lines) or '料金設定後に差額を表示します。'),inline=False)
    e.add_field(name='倍率について', value='各VIPロールの設定倍率です。複数所持時は最大のみ適用。VIP限定募集は対象に選ばれたロールのみ有効です。倍率変更は新しい募集から適用します。', inline=False)
    e.set_footer(text='お支払い・ランク付与はスタッフがゲーム内で対応 • 申請時点の料金をチケットに保存')
    return e
