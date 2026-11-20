import sqlite3
import unittest
from giveaway.redemptions import Redemptions, ReceiptError

class LinkListTests(unittest.TestCase):
    def test_scope_pagination_and_removal(self):
        with sqlite3.connect(':memory:') as conn:
            db=Redemptions(conn)
            for guild in (10,20):
                for user in range(17):
                    conn.execute('INSERT INTO guild_minecraft_links(guild,user,uuid,name,verified,original_name) VALUES(?,?,?,?,?,?)',(guild,user,str(user),'Steve',100+user,''))
            total,pages,rows=db.list_links(10)
            self.assertEqual((total,pages,len(rows)),(17,2,15))
            self.assertEqual(rows[0]['user'],16)
            self.assertEqual([r['user'] for r in db.list_links(10,2)[2]],[1,0])
            conn.execute('DELETE FROM guild_minecraft_links WHERE guild=10 AND user=16')
            self.assertEqual(db.list_links(10)[0],16)
            self.assertEqual(db.list_links(20)[0],17)
            self.assertEqual(db.list_links(30),(0,1,[]))
            with self.assertRaises(ReceiptError):db.list_links(10,3)
            with self.assertRaises(ReceiptError):db.list_links(None)
