import asyncio
import os
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from giveaway.core import draw, eligible, weight
from giveaway.store import Store


class RulesTests(unittest.TestCase):
    def test_max_not_product_or_sum(self):
        self.assertEqual(weight([1, 2, 3], {'1': 2, '2': 3}), 3)
        self.assertEqual(weight([], {'1': 4}), 1)

    def test_one_year_exact_boundary_and_missing_join(self):
        self.assertTrue(eligible([7], 0, 365 * 86400, 7, 365))
        self.assertFalse(eligible([7], 1, 365 * 86400, 7, 365))
        self.assertFalse(eligible([], 0, 365 * 86400, 7, 365))
        self.assertFalse(eligible([7], None, 365 * 86400, 7, 365))
        self.assertTrue(eligible([], None, 0, None, 0))

    def test_exact_weighted_ticket_space(self):
        outcomes = [draw({1: 1, 2: 3}, 1, lambda n, t=t: t)[0] for t in range(4)]
        self.assertEqual(outcomes, [1, 2, 2, 2])

    def test_no_duplicates_and_insufficient_entrants(self):
        self.assertEqual(draw({1: 100, 2: 1}, 20, lambda n: 0), [1, 2])
        self.assertEqual(draw({}, 2), [])

    def test_restart_entries_and_rule_snapshot(self):
        with tempfile.TemporaryDirectory() as d:
            path = d + '/db'
            db = Store(path)
            db.set_rules(1, {'7': 3})
            game = db.create({'rules': db.rules(1)})
            self.assertTrue(db.enter(game['id'], 42))
            self.assertFalse(db.enter(game['id'], 42))
            db.set_rules(1, {'7': 8})
            db.db.close()
            db = Store(path)
            self.assertEqual(db.get(game['id'])['rules'], {'7': 3})
            self.assertEqual(db.entries(game['id']), [42])
            db.leave(game['id'], 42)
            self.assertEqual(db.entries(game['id']), [])
            db.db.close()


class FinishTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        os.environ['DATABASE_PATH'] = self.temp.name + '/db'
        from giveaway.bot import bot
        self.bot = bot
        self.bot.store.db.close()
        self.bot.store = Store(os.environ['DATABASE_PATH'])
        self.game = self.bot.store.create(dict(guild=1, channel=2, message=3, status='drawing', rules={'7': 3}, role=None, days=0, count=1, rounds=[], dirty=False, end=0, prize='Test'))
        self.bot.store.enter(self.game['id'], 10)
        self.bot.store.enter(self.game['id'], 20)
        self.guild = SimpleNamespace(fetch_member=AsyncMock(side_effect=lambda uid: SimpleNamespace(id=uid, bot=False, joined_at=None, roles=[SimpleNamespace(id=7)])))
        self.bot.get_guild = lambda gid: self.guild
        self.bot.publish = AsyncMock()
        self.bot.announce = AsyncMock()

    async def asyncTearDown(self):
        self.bot.store.db.close()
        self.temp.cleanup()

    async def test_result_persisted_before_publish_failure(self):
        self.bot.publish.side_effect = RuntimeError('network')
        with self.assertRaises(RuntimeError):
            await self.bot.finish(self.game)
        saved = self.bot.store.get(self.game['id'])
        self.assertEqual(saved['status'], 'done')
        self.assertTrue(saved['dirty'])
        self.assertEqual(len(saved['rounds']), 1)
        self.bot.publish = AsyncMock()
        await self.bot.tick.coro(self.bot)
        self.assertEqual(self.bot.store.get(self.game['id'])['rounds'], saved['rounds'])

    async def test_fetch_failure_does_not_draw_partial_pool(self):
        self.guild.fetch_member.side_effect = RuntimeError('rate limit or network failure')
        with self.assertRaises(RuntimeError):
            await self.bot.finish(self.game)
        self.assertEqual(self.bot.store.get(self.game['id'])['rounds'], [])
        self.assertEqual(self.bot.store.get(self.game['id'])['status'], 'drawing')

    async def test_reroll_excludes_previous_winner(self):
        self.game['rounds'] = [[10]]
        await self.bot.finish(self.game)
        self.assertEqual(self.game['rounds'], [[10], [20]])

    async def test_departed_member_is_excluded(self):
        import discord
        async def fetch(uid):
            if uid == 10:
                raise discord.NotFound(SimpleNamespace(status=404, reason='missing'), 'missing')
            return SimpleNamespace(id=uid, bot=False, joined_at=None, roles=[])
        self.guild.fetch_member.side_effect = fetch
        await self.bot.finish(self.game)
        self.assertEqual(self.game['rounds'], [[20]])


if __name__ == '__main__':
    unittest.main()
