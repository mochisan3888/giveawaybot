import sqlite3
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
import discord
from giveaway.warning_roles import WarningRoles
from giveaway.moderation_store import ModerationStore

class WarningRoleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.conn=sqlite3.connect(':memory:');self.conn.row_factory=sqlite3.Row;self.store=ModerationStore(self.conn)
        self.member=SimpleNamespace(roles=[])
        self.member.add_roles=AsyncMock(side_effect=lambda *r,**kw:self.member.roles.extend(r))
        self.member.remove_roles=AsyncMock(side_effect=lambda *r,**kw:setattr(self.member,'roles',[x for x in self.member.roles if x not in r]))
        self.roles=[]
        async def create(**kw):
            self.assertEqual(kw['permissions'].value,0)
            role=SimpleNamespace(id=len(self.roles)+100,name=kw['name']);self.roles.append(role);return role
        self.guild=SimpleNamespace(id=1,fetch_member=AsyncMock(return_value=self.member),fetch_roles=AsyncMock(side_effect=lambda:self.roles),create_role=AsyncMock(side_effect=create))
        self.service=WarningRoles(SimpleNamespace(),self.store)
    async def asyncTearDown(self):self.conn.close()
    def case(self,action='warn'):
        c,_=self.store.create(1,9,8,action,'reason',str(len(self.store.cases(1))))
        c['state']='applied';self.store.save(c);return c
    async def test_progress_revoke_and_preserve_other_roles(self):
        unrelated=SimpleNamespace(id=90,name='Member');self.member.roles=[unrelated]
        a=self.case();await self.service.sync(self.guild,9)
        self.assertEqual([r.name for r in self.member.roles],['Member','警告1回'])
        b=self.case();await self.service.sync(self.guild,9)
        self.assertEqual([r.name for r in self.member.roles],['Member','警告2回'])
        self.store.revoke(1,b['id'],8,'cancel');await self.service.sync(self.guild,9)
        self.assertEqual([r.name for r in self.member.roles],['Member','警告1回'])
        self.store.revoke(1,a['id'],8,'cancel');await self.service.sync(self.guild,9)
        self.assertEqual(self.member.roles,[unrelated])
    async def test_caution_and_repeated_sync(self):
        self.case('caution');await self.service.sync(self.guild,9)
        self.guild.create_role.assert_not_awaited()
        self.case();await self.service.sync(self.guild,9);await self.service.sync(self.guild,9)
        self.assertEqual(self.guild.create_role.await_count,1)
        self.assertEqual(self.member.add_roles.await_count,1)
    async def test_uncertain_create_does_not_duplicate(self):
        self.case();self.guild.create_role.side_effect=TimeoutError()
        await self.service.update(self.guild,9);await self.service.update(self.guild,9)
        self.assertEqual(self.guild.create_role.await_count,1)
        self.assertIn((1,9),self.service.retry)

    async def test_scan_with_production_row_factory(self):
        self.case()
        c,_=self.store.create(1,10,8,'warn','reason','second-user')
        c['state']='applied';self.store.save(c)
        self.service.bot=SimpleNamespace(get_guild=lambda gid:self.guild)
        self.service.update=AsyncMock()
        await self.service.reconcile()
        self.assertEqual(self.service.update.await_count,2)

    async def test_event_restores_removed_role_and_revoke_stops_restore(self):
        self.service.bot=SimpleNamespace(get_guild=lambda gid:self.guild)
        case=self.case()
        await self.service.sync(self.guild,9)
        role=self.roles[0]
        self.member.roles=[]
        await self.service.member_changed({'guild_id':'1','user':{'id':'9'},'roles':[]})
        self.assertEqual(self.member.roles,[role])
        self.member.add_roles.reset_mock()
        await self.service.member_changed({'guild_id':'1','user':{'id':'9'},'roles':[str(role.id)]})
        self.member.add_roles.assert_not_awaited()
        self.store.revoke(1,case['id'],8,'cancel')
        await self.service.member_changed({'guild_id':'1','user':{'id':'9'},'roles':[str(role.id)]})
        self.assertEqual(self.member.roles,[])
        await self.service.member_changed({'guild_id':'1','user':{'id':'9'},'roles':[]})
        self.member.add_roles.assert_not_awaited()

    async def test_events_skip_restricted_and_backoff(self):
        self.case(); self.service.bot=SimpleNamespace(get_guild=lambda gid:self.guild)
        self.service.update=AsyncMock()
        import time
        self.service.retry[1,9]=time.monotonic()+60
        await self.service.member_changed({'guild_id':'1','user':{'id':'9'},'roles':[]})
        await self.service.member_changed({'guild_id':'900000000000000001','user':{'id':'9'},'roles':[]})
        self.service.update.assert_not_awaited()

    def test_uncached_member_parser_preserves_existing_handler(self):
        original=Mock()
        bot=SimpleNamespace(_connection=SimpleNamespace(parsers={'GUILD_MEMBER_UPDATE':original,'GUILD_MEMBER_ADD':original}),dispatch=Mock())
        self.service.bot=bot;self.service.install_events()
        data={'guild_id':'1','user':{'id':'9'},'roles':[]}
        bot._connection.parsers['GUILD_MEMBER_UPDATE'](data)
        original.assert_called_once_with(data)
        bot.dispatch.assert_called_once_with('warning_member_update',data)
