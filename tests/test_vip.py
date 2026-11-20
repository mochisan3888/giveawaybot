import asyncio
import sqlite3
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock,MagicMock
import discord
from giveaway.vip import Vip,VipPanel,register_vip
from giveaway.vip_store import VipStore
from giveaway.redemptions import ReceiptError

class VipTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.conn=sqlite3.connect(':memory:')
        self.owner,self.role,self.everyone,self.me=[MagicMock() for _ in range(4)];self.role.id=30
        self.ch=MagicMock(spec=discord.TextChannel);self.ch.id=50;self.ch.topic='vip-ticket:999:1'
        self.ch.get_partial_message.return_value.edit=AsyncMock()
        self.guild=SimpleNamespace(id=10,default_role=self.everyone,me=self.me,fetch_member=AsyncMock(return_value=self.owner),
            fetch_channels=AsyncMock(return_value=[]),create_text_channel=AsyncMock(return_value=self.ch),get_role=lambda rid:self.role)
        self.bot=SimpleNamespace(user=SimpleNamespace(id=999),store=SimpleNamespace(db=self.conn),add_view=MagicMock(),
            get_guild=lambda gid:self.guild,get_channel=lambda cid:self.ch,http=SimpleNamespace(request=AsyncMock(return_value={'id':'123'})),
            fulfillment=SimpleNamespace(validate_policy=AsyncMock(return_value=(object(),[self.role])),db=SimpleNamespace(link=lambda uid,gid:None)))
        self.owner.roles=[]
        self.s=Vip(self.bot);self.s.db.set_tiers(10,[40,41]);self.s.db.set_config(10,policy={'category':20,'roles':[30],'minecraft':False})
        self.i=SimpleNamespace(guild=self.guild,guild_id=10,user=SimpleNamespace(id=100),channel_id=50,channel=self.ch)
    async def asyncTearDown(self):self.conn.close()
    async def test_concurrent_submissions_one_private_ticket_and_only_staff_mentions(self):
        a,b=await asyncio.gather(self.s.request(self.i,'new',40),self.s.request(self.i,'new',40))
        self.assertEqual(a['id'],b['id']);self.guild.create_text_channel.assert_awaited_once()
        acl=self.guild.create_text_channel.call_args.kwargs['overwrites']
        self.assertFalse(acl[self.everyone].view_channel);self.assertTrue(acl[self.owner].view_channel)
        payload=self.bot.http.request.call_args.kwargs['json']
        self.assertEqual(payload['allowed_mentions'],{'parse':[],'users':['100'],'roles':['30']})
        self.assertEqual(self.s.db.get(a['id'])['content'],'新規申請 → <@&40>')
    async def test_policy_snapshot_and_restart_recovery(self):
        r=self.s.db.create(10,100,'スポナー5個','');r['creating']=True;self.s.db.save(r)
        self.s.db.set_config(10,policy={'category':99,'roles':[31],'minecraft':False})
        self.s.db=VipStore(self.conn)
        self.assertEqual(self.s.db.get(r['id'])['policy']['category'],20)
        self.guild.fetch_channels.return_value=[self.ch]
        await self.s.open_ticket(self.s.db.get(r['id']));self.guild.create_text_channel.assert_not_awaited()
    async def test_staff_only_lifecycle_preserves_history(self):
        r=await self.s.request(self.i,'new',40)
        self.guild.fetch_member.return_value=SimpleNamespace(id=100,roles=[])
        with self.assertRaises(ReceiptError):await self.s.action(self.i,r['id'],'start')
        staff=SimpleNamespace(id=200,roles=[self.role]);self.i.user.id=200
        self.guild.fetch_member.side_effect=lambda uid:staff if uid==200 else self.owner
        await self.s.action(self.i,r['id'],'start');await self.s.action(self.i,r['id'],'done');await self.s.action(self.i,r['id'],'close')
        self.assertEqual(self.s.db.get(r['id'])['status'],'closed')
        self.assertFalse(self.ch.edit.call_args.kwargs['overwrites'][self.owner].send_messages)
        self.assertIsNone(self.s.db.active(10,100))
    async def test_persistent_panel_and_admin_configuration(self):
        self.assertTrue(VipPanel(self.s).is_persistent())
        self.bot.tree=SimpleNamespace(add_command=MagicMock());self.bot.vip=self.s
        register_vip(self.bot);group=self.bot.tree.add_command.call_args.args[0]
        self.i.permissions=discord.Permissions.none()
        for name in ['setup','panel']:
            with self.assertRaises(discord.app_commands.MissingPermissions):
                for check in group.get_command(name).checks:check(self.i)
    async def test_blank_and_oversized_vips_rejected(self):
        for text in [' ', 'x'*1001]:
            with self.assertRaises(ReceiptError):self.s.db.create(10,100,text,'')
        self.assertIsNone(self.s.db.active(10,100))

    async def test_new_upgrade_and_maximum_tier(self):
        from giveaway.vip import available_tiers
        c={'tiers':[40,41,42]}
        self.assertEqual(available_tiers(c,SimpleNamespace(roles=[]),'new'),[40,41,42])
        member=SimpleNamespace(roles=[SimpleNamespace(id=40),SimpleNamespace(id=41)])
        self.assertEqual(available_tiers(c,member,'upgrade'),[42])
        with self.assertRaises(ReceiptError):available_tiers(c,member,'new')
        with self.assertRaises(ReceiptError):available_tiers(c,SimpleNamespace(roles=[]),'upgrade')
        with self.assertRaises(ReceiptError):available_tiers(c,SimpleNamespace(roles=[SimpleNamespace(id=42)]),'upgrade')
        with self.assertRaises(ReceiptError):await self.s.request(self.i,'upgrade',41)

    def test_vip_giveaway_scope_and_snapshot(self):
        from giveaway.vip_giveaway import vip_rules
        from giveaway.core import weight
        from giveaway.bot import qualifies
        c={'tiers':[40,41,42],'giveaway_rules':{'40':1.5,'41':2.25,'42':99}}
        rules=vip_rules(c,[40,41]);c['giveaway_rules']['41']=50
        self.assertEqual(weight([40,41,42],rules),2.25)
        game={'vip_roles':[40,41],'role':None,'days':0}
        m=lambda ids:SimpleNamespace(roles=[SimpleNamespace(id=r) for r in ids],joined_at=None)
        self.assertTrue(qualifies(m([41]),game))
        self.assertFalse(qualifies(m([42]),game))
        self.assertFalse(qualifies(m([]),game))
        for selected in [[],[40,40],[99],list(range(11))]:
            with self.assertRaises(ReceiptError):vip_rules(c,selected)

    async def test_vip_creation_persists_only_selected_rules(self):
        from giveaway.bot import start_game
        from giveaway.store import Store
        from unittest.mock import patch
        import giveaway.bot as module
        store=Store(':memory:')
        policy={'category':20,'roles':[30],'minutes':1440,'minecraft':False}
        testbot=SimpleNamespace(store=store,lock=asyncio.Lock(),fulfillment=SimpleNamespace(db=SimpleNamespace(profile=lambda g,c:policy),validate_policy=AsyncMock()),publish=AsyncMock())
        self.i.channel.send=AsyncMock(return_value=SimpleNamespace(id=88,jump_url='url'))
        self.i.followup=SimpleNamespace(send=AsyncMock())
        rules={'40':1.5,'41':2.25}
        with patch.object(module,'bot',testbot):
            await start_game(self.i,'VIP賞品',1,SimpleNamespace(value='days'),vip_roles=[40,41],override_rules=rules)
        saved=store.games()[0]
        self.assertEqual(saved['vip_roles'],[40,41]);self.assertEqual(saved['rules'],rules)
        self.assertEqual(saved['status'],'open');self.assertEqual(saved['receipt_policy'],policy)
        self.assertEqual(self.i.channel.send.call_args.kwargs['allowed_mentions'].to_dict()['parse'],[])
        store.db.close()

    def test_settings_are_independent_per_server(self):
        self.s.db.set_tiers(10,[40,41])
        self.s.db.set_config(10,giveaway_rules={'40':2.25})
        self.s.db.set_tiers(11,[70,71,72])
        self.s.db.set_config(11,giveaway_rules={'70':5},policy={'category':88,'roles':[89],'minecraft':False})
        self.assertEqual(self.s.db.tiers(10),[40,41])
        self.assertEqual(self.s.db.tiers(11),[70,71,72])
        self.assertEqual(self.s.db.config(10)['giveaway_rules'],{'40':2.25})
        self.assertEqual(self.s.db.config(10)['policy']['category'],20)

    async def test_panel_uses_supported_button_emoji(self):
        components=VipPanel(self.s).to_components()[0]['components']
        self.assertEqual(components[0]['emoji']['name'],'✨')
        self.assertEqual(components[1]['emoji']['name'],'👑')

    async def test_price_snapshot_and_decimal_upgrade(self):
        from giveaway.vip_prices import quote,validate_prices
        c={'tiers':[40,41,42],'prices':{'40':'10.1','41':'20.25','42':'50'}}
        member=SimpleNamespace(roles=[SimpleNamespace(id=40),SimpleNamespace(id=41)])
        self.assertEqual(quote(c,member,42)['due'],'29.75')
        self.assertEqual(quote(c,SimpleNamespace(roles=[]),40)['due'],'10.1')
        with self.assertRaises(ReceiptError):validate_prices([40,41],{'40':'20','41':'10'})
        self.s.db.set_config(10,prices={'40':'10.1','41':'20.25'})
        r=await self.s.request(self.i,'new',40)
        self.s.db.set_config(10,prices={'40':'15','41':'25'})
        self.assertEqual(self.s.db.get(r['id'])['quote']['due'],'10.1')
        self.assertNotIn('prices',self.s.db.config(11))

    async def test_price_panel_refresh_persists_revision(self):
        c=self.s.db.set_config(10,prices={'40':'10','41':'20'})
        self.conn.execute('INSERT INTO vip_price_panels VALUES (123,10,50,0)');self.conn.commit()
        await self.s.refresh_prices()
        edit=self.ch.get_partial_message.return_value.edit
        self.assertIn('10m',str(edit.call_args.kwargs['embed'].to_dict()))
        self.assertEqual(edit.call_args.kwargs['allowed_mentions'].to_dict()['parse'],[])
        count=edit.await_count
        await self.s.refresh_prices()
        self.assertEqual(edit.await_count,count)

    async def test_benefits_and_multiplier_changes_refresh_existing_price_panel(self):
        normal={'40':1.5};self.bot.store.rules=lambda guild:normal
        self.s.db.set_config(10,prices={'40':'10'},giveaway_rules={'40':2.25},benefits={'40':'専用チャット\n限定イベント'})
        self.conn.execute('INSERT INTO vip_price_panels VALUES (123,10,50,0)');self.conn.commit()
        await self.s.refresh_prices()
        edit=self.ch.get_partial_message.return_value.edit
        text=str(edit.call_args.kwargs['embed'].to_dict())
        self.assertIn('1.5倍',text);self.assertIn('2.25倍',text);self.assertIn('専用チャット',text)
        count=edit.await_count
        normal['40']=3
        await self.s.refresh_prices()
        self.assertEqual(edit.await_count,count+1)
        self.assertIn('3倍',str(edit.call_args.kwargs['embed'].to_dict()))
        await self.s.refresh_prices()
        self.assertEqual(edit.await_count,count+1)

    async def test_reception_blocks_both_modes_and_survives_restart(self):
        self.s.db.set_config(10,accepting=False)
        self.s.db=VipStore(self.conn)
        for kind in ['new','upgrade']:
            with self.assertRaisesRegex(ReceiptError,'受付停止中'):
                await self.s.request(self.i,kind,40)
        self.guild.create_text_channel.assert_not_awaited()
        self.s.db.set_config(10,accepting=True)
        r=await self.s.request(self.i,'new',40)
        self.s.db.set_config(10,accepting=False)
        self.assertEqual((await self.s.request(self.i,'new',40))['id'],r['id'])
        self.owner.id=100;self.owner.roles=[self.role]
        await self.s.action(self.i,r['id'],'start')
        self.assertEqual(self.s.db.get(r['id'])['status'],'working')

    async def test_reception_refreshes_buttons_and_reopens(self):
        from giveaway.vip import panel_embed
        self.s.db.save_panel(10,50,123,0)
        self.s.db.set_config(10,accepting=False)
        await self.s.refresh_panels()
        edit=self.ch.get_partial_message.return_value.edit
        self.assertTrue(all(b.disabled for b in edit.call_args.kwargs['view'].children))
        self.assertIn('受付停止中',str(edit.call_args.kwargs['embed'].to_dict()))
        self.s.db.set_config(10,accepting=True)
        await self.s.refresh_panels()
        self.assertTrue(all(not b.disabled for b in edit.call_args.kwargs['view'].children))
        self.assertTrue(self.s.db.config(11).get('accepting',True))
