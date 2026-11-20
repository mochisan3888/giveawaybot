"""One-use codes issued only after authenticated Xbox login; scoped guild links."""
import hashlib
import json
import re
import secrets
import sqlite3
import time
from .redemptions import ReceiptError


def gamertag(value):
    if not isinstance(value,str):raise ReceiptError('ゲーマータグが不正です。')
    value=value.strip()
    if not 1 <= len(value) <= 32 or any(not (c.isalnum() or c in ' _-#') for c in value):
        raise ReceiptError('統合版のゲーマータグを32文字以内で入力してください。')
    return value


def xuid_value(value):
    if not isinstance(value,str) or not re.fullmatch(r'[1-9][0-9]{0,19}',value) or int(value)>=2**64:
        raise ReceiptError('XUIDは正しい数字で入力してください。')
    return value


class BedrockLinks:
    def __init__(self, receipts):
        self.receipts,self.db=receipts,receipts.db
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS bedrock_codes(digest TEXT PRIMARY KEY,xuid TEXT NOT NULL,name TEXT NOT NULL,expires REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS minecraft_link_audit(id INTEGER PRIMARY KEY AUTOINCREMENT,guild INTEGER,user INTEGER,actor INTEGER,method TEXT,identity TEXT,name TEXT,at REAL);
        ''')

    def issue(self,xuid,name,now=None):
        now=time.time() if now is None else now
        xuid,name=xuid_value(xuid),gamertag(name)
        code=secrets.token_hex(12).upper()
        with self.db:
            self.db.execute('DELETE FROM bedrock_codes WHERE expires<=? OR xuid=?',(now,xuid))
            self.db.execute('INSERT INTO bedrock_codes VALUES (?,?,?,?)',(hashlib.sha256(code.encode()).hexdigest(),xuid,name,now+600))
        return code

    def _insert(self,guild,user,identity,name,original,method,actor,now):
        if self.receipts.link(user,guild):
            raise ReceiptError('このDiscordユーザーはすでに連携済みです。既存の登録は上書きしません。')
        others=self.db.execute("SELECT name FROM guild_minecraft_links WHERE guild=? AND uuid LIKE 'bedrock%'",(guild,)).fetchall()
        if identity.startswith('bedrock') and any(n[0].casefold()==name.casefold() for n in others):
            raise ReceiptError('このゲーマータグは、このサーバーですでに登録されています。')
        try:
            self.db.execute('INSERT INTO guild_minecraft_links(guild,user,uuid,name,verified,original_name) VALUES (?,?,?,?,?,?)',(guild,user,identity,name,now,original))
        except sqlite3.IntegrityError as error:
            raise ReceiptError('このアカウントはすでに登録されています。') from error
        self.db.execute('INSERT INTO minecraft_link_audit(guild,user,actor,method,identity,name,at) VALUES (?,?,?,?,?,?,?)',(guild,user,actor,method,identity,name,now))
        self.db.execute('DELETE FROM guild_link_challenges WHERE guild=? AND user=?',(guild,user))

    def consume(self,code,guild,user,original,receipt=None,now=None):
        now=time.time() if now is None else now
        code=re.sub(r'[\s-]','',str(code)).upper()
        if not re.fullmatch(r'[0-9A-F]{24}',code):raise ReceiptError('接続画面に表示された24文字の確認コードを入力してください。')
        digest=hashlib.sha256(code.encode()).hexdigest()
        with self.db:
            row=self.db.execute('SELECT xuid,name,expires FROM bedrock_codes WHERE digest=?',(digest,)).fetchone()
            if not row or row[2]<=now:raise ReceiptError('コードが違うか、期限切れ・使用済みです。認証サーバーへ接続し直してください。')
            if receipt:
                r=self.receipts.get(receipt)
                self.receipts.authorize(r,user,now)
                if r['guild']!=guild:raise ReceiptError('受け取り元のサーバーで操作してください。')
            self._insert(guild,user,'bedrock:'+row[0],row[1],original,'xbox_online',user,now)
            self.db.execute('DELETE FROM bedrock_codes WHERE digest=?',(digest,))
            if receipt:self.db.execute('INSERT OR IGNORE INTO receipt_requests VALUES (?)',(receipt,))
        return self.receipts.link(user,guild)

    def register_manual(self,guild,user,name,original,actor,xuid=None,now=None):
        now=time.time() if now is None else now
        name=gamertag(name)
        identity='bedrock:'+xuid_value(xuid) if xuid else 'bedrock-name:'+name.casefold()
        with self.db:self._insert(guild,user,identity,name,original,'admin_manual',actor,now)
        return self.receipts.link(user,guild)

    def admin_link(self,guild,user,edition,name,original,actor,identity=None,replace=False,now=None):
        now=time.time() if now is None else now
        if edition == 'bedrock':
            name=gamertag(name)
            identity='bedrock:'+xuid_value(identity) if identity else 'bedrock-name:'+name.casefold()
        elif edition == 'java':
            if not isinstance(identity,str) or not re.fullmatch('[0-9a-f]{32}',identity) or not re.fullmatch('[A-Za-z0-9_]{3,16}',name):
                raise ReceiptError('Javaプロフィールが不正です。')
        else:raise ReceiptError('エディションを選択してください。')
        with self.db:
            previous=self.receipts.link(user,guild)
            if previous:
                if not replace:raise ReceiptError('連携済みです。変更する場合は replace:true を指定してください。')
                original=previous['original_name']
                self.db.execute('DELETE FROM guild_minecraft_links WHERE guild=? AND user=?',(guild,user))
                self.db.execute('INSERT INTO minecraft_link_audit(guild,user,actor,method,identity,name,at) VALUES (?,?,?,?,?,?,?)',(guild,user,actor,'admin_replace_old',previous['uuid'],previous['name'],now))
            self._insert(guild,user,identity,name,original,'admin_manual',actor,now)
        return self.receipts.link(user,guild)

    def admin_unlink(self,guild,user,actor,now=None):
        now=time.time() if now is None else now
        with self.db:
            previous=self.receipts.link(user,guild)
            self.db.execute('DELETE FROM guild_link_challenges WHERE guild=? AND user=?',(guild,user))
            if previous:
                self.db.execute('DELETE FROM guild_minecraft_links WHERE guild=? AND user=?',(guild,user))
                self.db.execute('INSERT INTO minecraft_link_audit(guild,user,actor,method,identity,name,at) VALUES (?,?,?,?,?,?,?)',(guild,user,actor,'admin_unlink',previous['uuid'],previous['name'],now))
        return previous
