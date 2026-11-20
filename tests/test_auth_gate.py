import sqlite3
import unittest
from giveaway.redemptions import Redemptions
from giveaway.bot import minecraft_eligible

class AuthGateTests(unittest.TestCase):
    def test_deadline_guild_and_unlink(self):
        c=sqlite3.connect(':memory:')
        self.addCleanup(c.close)
        Redemptions(c)
        g=dict(guild=1,end=100,receipt_policy={'minecraft':True})
        self.assertFalse(minecraft_eligible(c,7,g))
        c.execute('INSERT INTO guild_minecraft_links(guild,user,uuid,name,verified,original_name) VALUES(2,7,?, ?,99,?)',('a','Steve',''))
        self.assertFalse(minecraft_eligible(c,7,g))
        c.execute('INSERT INTO guild_minecraft_links(guild,user,uuid,name,verified,original_name) VALUES(1,7,?, ?,100,?)',('a','Steve',''))
        self.assertTrue(minecraft_eligible(c,7,g))
        c.execute('UPDATE guild_minecraft_links SET verified=101 WHERE guild=1')
        self.assertFalse(minecraft_eligible(c,7,g))
        c.execute('DELETE FROM guild_minecraft_links WHERE guild=1')
        self.assertFalse(minecraft_eligible(c,7,g))
        g['receipt_policy']['minecraft']=False
        self.assertTrue(minecraft_eligible(c,7,g))
