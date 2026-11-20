import secrets
from decimal import Decimal


def weight(role_ids, multipliers):
    return max([1] + [float(multipliers.get(str(r), 1)) for r in role_ids])


def eligible(role_ids, joined_at, now, required_role, min_days):
    return (not required_role or required_role in role_ids) and (
        not min_days or joined_at is not None and now - joined_at >= min_days * 86400
    )


def draw(candidates, count, randbelow=secrets.randbelow):
    """Weighted sampling without replacement; integer weights, no ticket expansion."""
    values = {uid: Decimal(str(value)) for uid, value in candidates.items()}
    places = max([0] + [-v.as_tuple().exponent for v in values.values()])
    pool = {uid: int(value * (10 ** places)) for uid, value in values.items()}
    winners = []
    for _ in range(min(count, len(pool))):
        ticket = randbelow(sum(pool.values()))
        for uid, value in pool.items():
            ticket -= value
            if ticket < 0:
                winners.append(uid)
                del pool[uid]
                break
    return winners


def duration_seconds(amount, unit):
    factors = {'seconds': 1, 'minutes': 60, 'hours': 3600, 'days': 86400}
    if type(amount) is not int or amount < 1 or unit not in factors:
        raise ValueError('期間は1以上の整数と、秒・分・時間・日の単位で指定してください。')
    seconds = amount * factors[unit]
    if seconds > 365 * 86400:
        raise ValueError('募集期間は365日以内にしてください。')
    return seconds
