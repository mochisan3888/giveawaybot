import asyncio
import json
import sqlite3
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
from giveaway.skelly import Skelly, SkellyPanel, price_embed, order_embed, register_skelly
from giveaway.skelly_store import SkellyStore, price, quantity, total
from giveaway.redemptions import ReceiptError


class ModelTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.db = SkellyStore(self.conn)
        self.db.set_config(10, buy='1.25', sell='0.9', policy={'category': 20, 'roles': [30, 31], 'minecraft': False})
    def tearDown(self):
        self.conn.close()
    def test_decimal_price_and_integer_quantity(self):
        self.assertEqual(price(' 1.2500m '), '1.25')
        self.assertEqual(total('0.000001', 3), '0.000003')
        self.assertEqual(total('1.25', 64), '80')
        self.assertEqual(quantity('0002'), 2)
        for bad in ['NaN', 'inf', '-1', '0', '1e3', '0.0000001', '1000000001']:
            with self.assertRaises(ReceiptError): price(bad)
        for bad in ['0', '-1', '1.5', '1000001']:
            with self.assertRaises(ReceiptError): quantity(bad)
    def test_independent_reception_and_price_preservation(self):
        self.db.set_config(10,buy_accepting=False,sell_accepting=True)
        with self.assertRaises(ReceiptError):self.db.create(10,101,'buy',1,'1.25')
        self.assertEqual(self.db.create(10,102,'sell',1,'0.9')['unit'],'0.9')
        self.db.set_config(10,buy_accepting=True,sell_accepting=False)
        self.assertEqual(self.db.create(10,103,'buy',1,'1.25')['unit'],'1.25')
        with self.assertRaises(ReceiptError):self.db.create(10,104,'sell',1,'0.9')
        self.assertEqual(self.db.config(10)['sell'],'0.9')
    def test_price_change_does_not_change_existing_ticket(self):
        r = self.db.create(10, 100, 'buy', 64, '1.25')
        self.db.set_config(10, buy='2', sell='1.1', policy={'category': 21, 'roles': [32], 'minecraft': False})
        saved = self.db.get(r['id'])
        self.assertEqual((saved['unit'], saved['total'], saved['policy']['roles']), ('1.25', '80', [30,31]))
        with self.assertRaises(ReceiptError): self.db.create(10, 101, 'buy', 64, '1.25')
    def test_duplicate_request_and_close_allow_new_order(self):
        r = self.db.create(10, 100, 'buy', 64, '1.25')
        second = self.db.create(10, 100, 'sell', 1, '0.9')
        self.assertEqual(r, second)
        r['status'] = 'closed'; self.db.save(r)
        new = self.db.create(10, 100, 'sell', 10, '0.9')
        self.assertNotEqual(r['id'], new['id'])
        self.assertEqual(new['total'], '9')
    def test_restart_preserves_panels_and_active_requests(self):
        with tempfile.TemporaryDirectory() as folder:
            p = folder + '/db'
            c = sqlite3.connect(p); db = SkellyStore(c)
            db.set_config(10, **{k:v for k,v in self.db.config(10).items() if k != 'revision'})
            db.save_panel(10, 50, 90, 1); r = db.create(10, 100, 'sell', 3, '0.9')
            c.close(); c = sqlite3.connect(p); db = SkellyStore(c)
            self.assertEqual(db.active(10,100)['id'], r['id'])
            self.assertEqual(db.panels(10)[0]['message'],90)
            c.close()


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.owner, self.role, self.bot_member, self.everyone = [MagicMock() for _ in range(4)]
        self.owner.id = 100; self.role.id = 30
        self.staff = SimpleNamespace(id=200,roles=[self.role])
        self.channel = MagicMock(spec=discord.TextChannel)
        self.channel.id = 50; self.channel.topic = 'skelly-ticket:999:1'
        self.channel.get_partial_message.return_value.edit = AsyncMock()
        self.guild = SimpleNamespace(id=10, default_role=self.everyone, me=self.bot_member,
            fetch_member=AsyncMock(return_value=self.owner), get_role=lambda rid:self.role,
            create_text_channel=AsyncMock(return_value=self.channel), fetch_channels=AsyncMock(return_value=[]))
        self.bot = SimpleNamespace(store=SimpleNamespace(db=self.conn),user=SimpleNamespace(id=999),add_view=MagicMock(),
            get_guild=lambda gid:self.guild, get_channel=lambda cid:self.channel,
            http=SimpleNamespace(request=AsyncMock(return_value={'id':'123'})),
            fulfillment=SimpleNamespace(validate_policy=AsyncMock(return_value=(object(),[self.role])),db=SimpleNamespace(link=lambda uid,guild:{'name':'Steve'})))
        self.s = Skelly(self.bot)
        self.s.db.set_config(10,buy='1.25',sell='0.9',policy={'roles':[30],'category':20,'minecraft':False})
    async def asyncTearDown(self):
        self.conn.close()
    def interaction(self, uid=100):
        return SimpleNamespace(guild_id=10,guild=self.guild,channel_id=50,channel=self.channel,user=SimpleNamespace(id=uid),
            message=SimpleNamespace(id=90),response=SimpleNamespace(send_modal=AsyncMock(),send_message=AsyncMock()))
    async def test_concurrent_requests_create_one_channel_and_private_staff_notice(self):
        r1,r2 = await asyncio.gather(self.s.request(self.interaction(),'buy',64,'1.25'),self.s.request(self.interaction(),'buy',64,'1.25'))
        self.assertEqual(r1['id'],r2['id'])
        self.guild.create_text_channel.assert_awaited_once()
        self.bot.http.request.assert_awaited_once()
        acl=self.guild.create_text_channel.call_args.kwargs['overwrites']
        self.assertFalse(acl[self.everyone].view_channel)
        for member in [self.owner,self.role,self.bot_member]: self.assertTrue(acl[member].view_channel)
        payload=self.bot.http.request.call_args.kwargs['json']
        self.assertEqual(payload['allowed_mentions'],{'parse':[],'users':['100'],'roles':['30']})
        self.assertIn('80m',json.dumps(payload,ensure_ascii=False))
        self.assertIn('Steve',json.dumps(payload))
    async def test_uncertain_create_is_recovered_without_second_channel(self):
        r=self.s.db.create(10,100,'sell',2,'0.9');r['creating']=True;self.s.db.save(r)
        self.guild.fetch_channels.return_value=[self.channel]
        await self.s.open_ticket(r)
        self.guild.create_text_channel.assert_not_awaited()
        self.assertEqual(self.s.db.get(r['id'])['channel'],50)
    async def test_price_updates_all_panels_but_not_ticket(self):
        r=await self.s.request(self.interaction(),'buy',2,'1.25')
        self.s.db.save_panel(10,50,90,1);self.s.db.save_panel(10,50,91,1)
        self.s.db.set_config(10,buy='2.5',sell='2')
        await self.s.refresh_panels(10)
        self.assertEqual(self.channel.get_partial_message.return_value.edit.await_count,2)
        self.assertTrue(all(p['revision']==2 for p in self.s.db.panels(10)))
        self.assertEqual(self.s.db.get(r['id'])['total'],'2.5')
    async def test_panel_opens_private_quantity_modal_with_current_price(self):
        self.s.db.save_panel(10,50,90,1)
        view=SkellyPanel(self.s);self.assertTrue(view.is_persistent())
        i=self.interaction();await view.children[1].callback(i)
        modal=i.response.send_modal.call_args.args[0]
        self.assertEqual((modal.side,modal.unit),('sell','0.9'))
        self.assertIn('0.9m',modal.title)
    async def test_wrong_channel_and_non_staff_cannot_manage(self):
        r=await self.s.request(self.interaction(),'buy',1,'1.25')
        i=self.interaction(200);i.channel_id=51
        with self.assertRaises(ReceiptError):await self.s.action(i,r['id'],'start')
        i.channel_id=50;self.guild.fetch_member.return_value=SimpleNamespace(id=200,roles=[])
        with self.assertRaises(ReceiptError):await self.s.action(i,r['id'],'start')
        self.assertEqual(self.s.db.get(r['id'])['status'],'open')
    async def test_staff_lifecycle_closes_readonly_and_retains_quote(self):
        r=await self.s.request(self.interaction(),'sell',10,'0.9')
        async def member(uid):return self.owner if uid==100 else self.staff
        self.guild.fetch_member.side_effect=member
        i=self.interaction(200)
        await self.s.action(i,r['id'],'start');await self.s.action(i,r['id'],'done');await self.s.action(i,r['id'],'close')
        r=self.s.db.get(r['id'])
        self.assertEqual((r['status'],r['assignee'],r['total']),('closed',200,'9'))
        self.assertEqual([h['action'] for h in r['history']],['start','done','close'])
        acl=self.channel.edit.call_args.kwargs['overwrites']
        self.assertFalse(acl[self.owner].send_messages)
        self.assertFalse(acl[self.owner].send_messages_in_threads)
        self.assertTrue(acl[self.role].send_messages)
        self.assertIsNone(self.s.db.active(10,100))
    async def test_old_uncertain_notification_is_not_duplicated(self):
        r=self.s.db.create(10,100,'buy',1,'1.25')
        r.update(channel=50,status='open',sending_at=time.time()-121)
        self.s.db.save(r);await self.s.update_ticket(r)
        self.bot.http.request.assert_not_awaited()
        self.assertTrue(self.s.db.get(r['id'])['notification_uncertain'])
    async def test_stop_blocks_open_form_submission_but_keeps_existing_ticket(self):
        existing=await self.s.request(self.interaction(),'buy',2,'1.25')
        self.s.db.set_config(10,accepting=False)
        with self.assertRaises(ReceiptError):
            await self.s.request(self.interaction(101),'sell',3,'0.9')
        self.assertEqual((await self.s.request(self.interaction(),'buy',2,'1.25'))['id'],existing['id'])
        self.assertIsNone(self.s.db.active(10,101))
        self.s.db.set_config(10,accepting=True)
        self.assertIsNotNone(await self.s.request(self.interaction(101),'sell',3,'0.9'))

    async def test_stopped_panel_disabled_and_stale_button_rejected(self):
        self.s.db.save_panel(10,50,90,1)
        self.s.db.set_config(10,accepting=False)
        self.assertTrue(all(b.disabled for b in SkellyPanel(self.s,accepting=False).children))
        with self.assertRaises(ReceiptError):
            await SkellyPanel(self.s).children[0].callback(self.interaction())
        await self.s.refresh_panels(10)
        view=self.channel.get_partial_message.return_value.edit.call_args.kwargs['view']
        self.assertTrue(all(b.disabled for b in view.children))

    async def test_admin_commands_reject_non_admin(self):
        self.bot.tree=SimpleNamespace(add_command=MagicMock());self.bot.skelly=self.s
        register_skelly(self.bot)
        group=self.bot.tree.add_command.call_args.args[0]
        i=self.interaction();i.permissions=discord.Permissions.none()
        for name in ['setup','price','panel','settings','reception']:
            command=group.get_command(name)
            self.assertTrue(command.checks)
            with self.assertRaises(discord.app_commands.MissingPermissions):
                for check in command.checks:check(i)
