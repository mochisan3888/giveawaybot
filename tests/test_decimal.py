import unittest
from giveaway.core import draw, weight

class DecimalTests(unittest.TestCase):
    def test_decimal_max(self):
        self.assertEqual(weight([1,2], {'1':1.5,'2':2.25}),2.25)
    def test_decimal_ticket_proportions(self):
        outcomes=[draw({1:1,2:1.5},1,lambda n,t=t:t)[0] for t in range(25)]
        self.assertEqual(outcomes.count(1),10)
        self.assertEqual(outcomes.count(2),15)
    def test_hundredths_and_no_duplicates(self):
        self.assertEqual(draw({1:1.01,2:2.25},3,lambda n:0),[1,2])

class ParticipationTests(unittest.IsolatedAsyncioTestCase):
    async def test_status_is_private_and_reflects_entry(self):
        import time
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        from giveaway.bot import Buttons
        game=dict(message=3,id=1,status='open',end=time.time()+60,rules={'7':1.5},role=None,days=0)
        entries=[42]
        store=SimpleNamespace(games=lambda:[game],entries=lambda gid:entries)
        view=Buttons(SimpleNamespace(store=store))
        i=SimpleNamespace(message=SimpleNamespace(id=3),user=SimpleNamespace(id=42),response=SimpleNamespace(defer=AsyncMock()),followup=SimpleNamespace(send=AsyncMock()),guild=SimpleNamespace(fetch_member=AsyncMock(return_value=SimpleNamespace(roles=[SimpleNamespace(id=7)],joined_at=None))))
        button=next(b for b in view.children if b.custom_id=='giveaway:status:v1')
        await button.callback(i)
        self.assertIn('あなたは参加しています！',i.followup.send.call_args.args[0])
        self.assertIn('1.5倍',i.followup.send.call_args.args[0])
        self.assertTrue(i.followup.send.call_args.kwargs['ephemeral'])
        entries.clear()
        await button.callback(i)
        self.assertIn('参加していません',i.followup.send.call_args.args[0])
