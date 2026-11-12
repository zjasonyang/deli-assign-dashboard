#!/usr/bin/env python3
"""Local dispatch service. Run with Python 3.12, without third-party packages."""
import argparse
from contextlib import contextmanager
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time
from datetime import date, datetime, timedelta
from http.cookies import SimpleCookie, CookieError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
SESSION_SECONDS = 12 * 60 * 60
PBKDF2_ROUNDS = 600_000


def password_hash(password, salt):
    return hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), PBKDF2_ROUNDS).hex()


@contextmanager
def connect(path):
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys = ON')
    try:
        with db:
            yield db
    finally:
        db.close()


def next_person_short(db):
    numbers = [int(row['short'][1:]) for row in db.execute('SELECT short FROM personnel')
               if row['short'] and re.fullmatch(r'P[0-9]+',row['short'],re.IGNORECASE)]
    return 'P'+str(max(numbers,default=0)+1)


def initialize(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    seed = json.loads((ROOT / 'seed.json').read_text())
    with connect(path) as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('admin','user')),
                salt TEXT NOT NULL, password_hash TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), expires_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS login_attempts (
                username TEXT PRIMARY KEY, failures INTEGER NOT NULL, until REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS teams (id TEXT PRIMARY KEY,name TEXT NOT NULL,short TEXT NOT NULL,region TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS personnel (id TEXT PRIMARY KEY,name TEXT UNIQUE NOT NULL,short TEXT NOT NULL COLLATE NOCASE);
            CREATE TABLE IF NOT EXISTS work_orders (
                id TEXT PRIMARY KEY,title TEXT NOT NULL,customer TEXT NOT NULL,region TEXT NOT NULL,address TEXT NOT NULL,
                status TEXT NOT NULL,priority TEXT NOT NULL,phone TEXT NOT NULL DEFAULT '',content TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',person1_id TEXT REFERENCES personnel(id),person2_id TEXT REFERENCES personnel(id)
            );
            CREATE TABLE IF NOT EXISTS assignments (
                id TEXT PRIMARY KEY,work_order_id TEXT UNIQUE NOT NULL REFERENCES work_orders(id),
                team_id TEXT NOT NULL REFERENCES teams(id),date TEXT NOT NULL,start TEXT NOT NULL,end TEXT NOT NULL,
                updated_by INTEGER REFERENCES users(id),CHECK(start < end)
            );
        """)
        if not db.execute('SELECT 1 FROM personnel').fetchone():
            columns = {row['name'] for row in db.execute('PRAGMA table_info(personnel)')}
            if 'short' not in columns: db.execute('ALTER TABLE personnel ADD COLUMN short TEXT')
            db.executemany('INSERT INTO personnel(id,name,short) VALUES(:id,:name,:short)',seed['personnel'])
        person_columns = {row['name'] for row in db.execute('PRAGMA table_info(personnel)')}
        if 'short' not in person_columns:
            db.execute('ALTER TABLE personnel ADD COLUMN short TEXT')
        for person in db.execute("SELECT id FROM personnel WHERE short IS NULL OR short='' ORDER BY rowid").fetchall():
            db.execute('UPDATE personnel SET short=? WHERE id=?',(next_person_short(db),person['id']))
        db.execute('CREATE UNIQUE INDEX IF NOT EXISTS personnel_short_unique ON personnel(short COLLATE NOCASE)')
        columns = {row['name'] for row in db.execute('PRAGMA table_info(work_orders)')}
        for column in ('phone','content','notes'):
            if column not in columns:
                db.execute(f"ALTER TABLE work_orders ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
        for column in ('person1_id','person2_id'):
            if column not in columns:
                db.execute(f'ALTER TABLE work_orders ADD COLUMN {column} TEXT REFERENCES personnel(id)')
        if 'person1' in columns:
            for order in db.execute('SELECT * FROM work_orders').fetchall():
                assignment = db.execute('SELECT team_id FROM assignments WHERE work_order_id=?',(order['id'],)).fetchone()
                team = assignment['team_id'] if assignment else 'north-a' if order['region']=='北部' else 'south-a'
                first = {'north-a':1,'south-a':3,'south-b':5}.get(team,1)
                ids = []
                for i,column in enumerate(('person1','person2')):
                    name = order[column]
                    if not name:
                        ids.append(None)
                    elif name in ('人員 A（示意）','人員 B（示意）'):
                        ids.append(f'p{first+i}')
                    else:
                        person = db.execute('SELECT id FROM personnel WHERE name=?',(name,)).fetchone()
                        person_id = person['id'] if person else secrets.token_hex(12)
                        if not person:
                            db.execute('INSERT INTO personnel(id,name,short) VALUES(?,?,?)',(person_id,name,next_person_short(db)))
                        ids.append(person_id)
                db.execute('UPDATE work_orders SET person1_id=?,person2_id=? WHERE id=?',(*ids,order['id']))
            db.execute('ALTER TABLE work_orders DROP COLUMN person1')
            db.execute('ALTER TABLE work_orders DROP COLUMN person2')
        for username,role,default in [('admin','admin','admin123!'),('user','user','user123!')]:
            if not db.execute('SELECT 1 FROM users WHERE username=?',(username,)).fetchone():
                salt = secrets.token_hex(16)
                password = os.environ.get(f'DELI_{role.upper()}_PASSWORD',default)
                db.execute('INSERT INTO users(username,role,salt,password_hash) VALUES(?,?,?,?)',
                           (username,role,salt,password_hash(password,salt)))
        if not db.execute('SELECT 1 FROM teams').fetchone():
            db.executemany('INSERT INTO teams VALUES(:id,:name,:short,:region)',seed['teams'])
            db.executemany("""INSERT INTO work_orders(id,title,customer,region,address,status,priority,phone,content,notes,person1_id,person2_id)
                VALUES(:id,:title,:customer,:region,:address,:status,:priority,:phone,:content,:notes,:person1Id,:person2Id)""",seed['orders'])
            today = datetime.now(ZoneInfo('Asia/Taipei')).date()
            monday = today-timedelta(days=today.weekday())
            for item in seed['assignments']:
                db.execute('INSERT INTO assignments(id,work_order_id,team_id,date,start,end) VALUES(?,?,?,?,?,?)',
                           (item['id'],item['workOrderId'],item['teamId'],str(monday+timedelta(days=item['dayOffset'])),item['start'],item['end']))
        db.execute('DELETE FROM sessions WHERE expires_at<=?',(int(time.time()),))
    path.chmod(0o600)


def read_orders(db):
    return [dict(row) for row in db.execute("""SELECT w.id,w.title,w.customer,w.region,w.address,w.status,w.priority,
        w.phone,w.content,w.notes,w.person1_id AS person1Id,w.person2_id AS person2Id,
        COALESCE(p1.name,'') AS person1,COALESCE(p2.name,'') AS person2
        FROM work_orders w LEFT JOIN personnel p1 ON p1.id=w.person1_id LEFT JOIN personnel p2 ON p2.id=w.person2_id ORDER BY w.id""")]


class ApiError(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message


class Handler(BaseHTTPRequestHandler):
    server_version = 'DeliLocal/1.0'

    def reply(self, status, payload, cookie=None):
        raw = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        if cookie:
            self.send_header('Set-Cookie', cookie)
        self.end_headers()
        self.wfile.write(raw)

    def body(self):
        if self.headers.get('Content-Type','').split(';')[0] != 'application/json':
            raise ApiError(415, '請使用 JSON 格式。')
        try:
            length = int(self.headers.get('Content-Length','0'))
            if not 0 < length <= 65536:
                raise ApiError(413, '請求內容大小不正確。')
            data = json.loads(self.rfile.read(length))
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400, 'JSON 格式不正確。')
        if not isinstance(data, dict):
            raise ApiError(400, '請提供 JSON 物件。')
        return data

    def token(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get('Cookie',''))
        except CookieError:
            return ''
        return cookie['deli_session'].value if 'deli_session' in cookie else ''

    def current_user(self, db):
        token = self.token()
        row = db.execute('''SELECT users.id,username,role FROM sessions JOIN users ON users.id = sessions.user_id
                            WHERE token_hash = ? AND expires_at > ?''',
                         (hashlib.sha256(token.encode()).hexdigest(), int(time.time()))).fetchone()
        if not row:
            raise ApiError(401, '請先登入。')
        return dict(row)

    def public_request(self):
        return bool(self.server.public_origin and self.headers.get('Host') == urlsplit(self.server.public_origin).netloc)

    def session_cookie(self, token, age):
        secure = '; Secure' if self.public_request() else ''
        return f'deli_session={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={age}{secure}'

    def validate_origin(self):
        expected = self.server.public_origin if self.public_request() else f'http://{self.headers.get("Host", "")}'
        if self.headers.get('Origin') != expected or self.headers.get('Sec-Fetch-Site') == 'cross-site':
            raise ApiError(403, '此操作必須從本站發出。')

    def dispatch(self):
        # Bind to loopback and reject arbitrary Host values, including DNS rebinding.
        allowed = {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}
        if self.server.public_origin:
            allowed.add(urlsplit(self.server.public_origin).netloc)
        if self.headers.get('Host') not in allowed:
            raise ApiError(403, '不接受此主機名稱。')
        path = urlsplit(self.path).path
        method = self.command
        if method in ('POST','PUT'):
            self.validate_origin()
        if method == 'GET' and path in ('/','/admin/personnel'):
            html = (ROOT / 'index.html').read_text()
            if self.server.public_origin:
                html = re.sub(r'<details><summary>本機測試帳號</summary>.*?</details>', '', html, flags=re.S)
            raw = html.encode()
            self.send_response(200)
            self.send_header('Content-Type','text/html; charset=utf-8')
            self.send_header('Content-Length',str(len(raw)))
            self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Referrer-Policy','same-origin')
            self.send_header('X-Frame-Options','DENY')
            self.end_headers()
            self.wfile.write(raw)
            return
        if method == 'GET' and path == '/api/health':
            self.reply(200, {'ok':True})
            return
        with connect(self.server.db_path) as db:
            if method == 'POST' and path == '/api/login':
                self.login(db)
                return
            user = self.current_user(db)
            if method == 'GET' and path == '/api/me':
                self.reply(200, {'user':user})
            elif method == 'POST' and path == '/api/logout':
                db.execute('DELETE FROM sessions WHERE token_hash = ?', (hashlib.sha256(self.token().encode()).hexdigest(),))
                self.reply(200, {'ok':True}, self.session_cookie('', 0))
            elif method == 'GET' and path == '/api/dashboard':
                self.reply(200, {
                    'user':user,
                    'teams':[dict(r) for r in db.execute('SELECT * FROM teams ORDER BY id')],
                    'orders':read_orders(db),
                    'personnel':[dict(r) for r in db.execute('SELECT * FROM personnel ORDER BY rowid')],
                    'assignments':[dict(r) for r in db.execute('''SELECT id, work_order_id AS workOrderId, team_id AS teamId,
                        date,start,end FROM assignments ORDER BY date,start''')],
                })
            elif method == 'POST' and path == '/api/orders':
                if user['role']!='admin':
                    raise ApiError(403,'只有 admin 可以建立工單。')
                self.save_order(db,None,self.body(),user,create=True)
            elif method == 'PUT' and re.fullmatch(r'/api/orders/[^/]+',path):
                self.save_order(db,path.rsplit('/',1)[1],self.body(),user)
            elif (method=='POST' and path=='/api/personnel') or (method=='PUT' and re.fullmatch(r'/api/personnel/[^/]+',path)):
                if user['role']!='admin':
                    raise ApiError(403,'只有 admin 可以管理人員。')
                self.save_person(db,path,self.body(),method)
            elif method == 'PUT' and path == '/api/assignments':
                if user['role'] != 'admin':
                    raise ApiError(403, '只有 admin 可以修改派工。')
                data = self.body()
                self.save_assignment(db, data, user)
            else:
                raise ApiError(404, '找不到此服務。')

    def login(self, db):
        data = self.body()
        username, password = data.get('username'), data.get('password')
        if not isinstance(username,str) or not isinstance(password,str) or len(username)>100 or len(password)>256:
            raise ApiError(400, '帳號或密碼格式不正確。')
        now = time.time()
        # This application accepts local requests only. Attempts are limited per username.
        db.execute('BEGIN IMMEDIATE')
        attempt = db.execute('SELECT * FROM login_attempts WHERE username=?', (username,)).fetchone()
        if attempt and attempt['failures'] >= 5 and attempt['until'] > now:
            raise ApiError(429, '登入失敗次數過多，請於 5 分鐘後重試。')
        row = db.execute('SELECT * FROM users WHERE username=?', (username,)).fetchone()
        salt = row['salt'] if row else '0'*32
        calculated = password_hash(password, salt)
        if not row or not hmac.compare_digest(row['password_hash'],calculated):
            failures = attempt['failures']+1 if attempt and attempt['until']>now else 1
            db.execute('INSERT OR REPLACE INTO login_attempts VALUES(?,?,?)',(username,failures,now+300))
            db.commit()
            raise ApiError(401, '帳號或密碼不正確。')
        db.execute('DELETE FROM login_attempts WHERE username=?',(username,))
        db.execute('DELETE FROM sessions WHERE expires_at<=?', (int(now),))
        old = self.token()
        if old:
            db.execute('DELETE FROM sessions WHERE token_hash=?',(hashlib.sha256(old.encode()).hexdigest(),))
        token = secrets.token_urlsafe(32)
        db.execute('INSERT INTO sessions VALUES(?,?,?)',(hashlib.sha256(token.encode()).hexdigest(),row['id'],int(now)+SESSION_SECONDS))
        self.reply(200, {'user':{'id':row['id'],'username':row['username'],'role':row['role']}},
                   self.session_cookie(token, SESSION_SECONDS))

    def write_assignment(self, db, data, user):
        keys = ('workOrderId','teamId','date','start','end')
        if any(not isinstance(data.get(k),str) for k in keys):
            raise ApiError(400, '請填寫完整的工單、團隊、日期與時間。')
        try:
            parsed = date.fromisoformat(data['date'])
            if parsed.isoformat() != data['date']:
                raise ValueError()
        except ValueError:
            raise ApiError(400, '出勤日期不正確。')
        for key in ('start','end'):
            if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', data[key]):
                raise ApiError(400, '時間格式不正確。')
        if data['start'] >= data['end']:
            raise ApiError(400, '結束時間須晚於開始時間。')
        if not db.in_transaction:
            db.execute('BEGIN IMMEDIATE')
        if not db.execute('SELECT 1 FROM work_orders WHERE id=?',(data['workOrderId'],)).fetchone():
            raise ApiError(404, '找不到工單。')
        if not db.execute('SELECT 1 FROM teams WHERE id=?',(data['teamId'],)).fetchone():
            raise ApiError(400, '找不到團隊。')
        conflict = db.execute('''SELECT work_order_id FROM assignments WHERE team_id=? AND date=?
            AND work_order_id != ? AND start < ? AND end > ?''',
            (data['teamId'],data['date'],data['workOrderId'],data['end'],data['start'])).fetchone()
        if conflict:
            raise ApiError(409, f'此團隊同時段已有派工 {conflict["work_order_id"]}，請調整時間或團隊。')
        existing = db.execute('SELECT id FROM assignments WHERE work_order_id=?',(data['workOrderId'],)).fetchone()
        assignment_id = existing['id'] if existing else secrets.token_hex(12)
        db.execute('''INSERT INTO assignments VALUES(?,?,?,?,?,?,?) ON CONFLICT(work_order_id)
            DO UPDATE SET team_id=excluded.team_id,date=excluded.date,start=excluded.start,end=excluded.end,updated_by=excluded.updated_by''',
            (assignment_id,data['workOrderId'],data['teamId'],data['date'],data['start'],data['end'],user['id']))
        return {'id':assignment_id,**{k:data[k] for k in keys}}

    def save_assignment(self,db,data,user):
        self.reply(200,{'assignment':self.write_assignment(db,data,user)})

    def save_order(self,db,order_id,data,user,create=False):
        fields = {'title':200,'customer':200,'region':10,'address':500,'status':20,'priority':20,'phone':100,'content':5000,'notes':5000}
        allowed = set(fields)|{'person1Id','person2Id','assignment'} if user['role']=='admin' else {'notes'}
        if not data or set(data)-allowed:
            raise ApiError(403 if user['role']!='admin' else 400,'user 只能修改備註。' if user['role']!='admin' else '工單欄位不正確。')
        if create and not {'title','customer'}.issubset(data):
            raise ApiError(400,'請填寫項目名稱與客戶名稱。')
        for name,limit in fields.items():
            if name in data and (not isinstance(data[name],str) or len(data[name])>limit):
                raise ApiError(400,f'{name} 格式或長度不正確。')
        for name in ('title','customer'):
            if name in data and not data[name].strip():
                raise ApiError(400,'項目名稱與客戶名稱不可空白。')
        for name,values in [('region',('北部','南部')),('status',('待出勤','進行中','已完成')),('priority',('normal','urgent'))]:
            if name in data and data[name] not in values:
                raise ApiError(400,'地區、狀態或優先順序不正確。')
        db.execute('BEGIN IMMEDIATE')
        if create:
            numbers = [int(row['id'][3:]) for row in db.execute('SELECT id FROM work_orders') if re.fullmatch(r'WO-[0-9]+',row['id'])]
            order_id = 'WO-'+str(max(numbers,default=0)+1).zfill(4)
            db.execute('''INSERT INTO work_orders(id,title,customer,region,address,status,priority,phone,content,notes)
                VALUES(?,?,?,'北部','','待出勤','normal','','','')''',(order_id,data['title'],data['customer']))
        existing = db.execute('SELECT * FROM work_orders WHERE id=?',(order_id,)).fetchone()
        if not existing:
            raise ApiError(404,'找不到工單。')
        people = []
        for key,column in [('person1Id','person1_id'),('person2Id','person2_id')]:
            value = data.get(key,existing[column])
            if value is not None and (not isinstance(value,str) or not db.execute('SELECT 1 FROM personnel WHERE id=?',(value,)).fetchone()):
                raise ApiError(400,'找不到所選人員。')
            people.append(value)
        if {'person1Id','person2Id'} & data.keys() and people[0] and people[0]==people[1]:
            raise ApiError(400,'兩位人員不可重複。')
        changes = {name:data[name] for name in fields if name in data}
        for key,column in [('person1Id','person1_id'),('person2Id','person2_id')]:
            if key in data:
                changes[column] = data[key]
        if changes:
            db.execute('UPDATE work_orders SET '+','.join(f'{key}=?' for key in changes)+' WHERE id=?',(*changes.values(),order_id))
        if 'assignment' in data:
            assignment = data['assignment']
            if assignment is None:
                db.execute('DELETE FROM assignments WHERE work_order_id=?',(order_id,))
            elif isinstance(assignment,dict) and set(assignment)=={'teamId','date','start','end'}:
                self.write_assignment(db,{'workOrderId':order_id,**assignment},user)
            else:
                raise ApiError(400,'派工欄位不正確。')
        self.reply(201 if create else 200,{'order':next(order for order in read_orders(db) if order['id']==order_id)})

    def save_person(self,db,path,data,method):
        if not {'name'}.issubset(data) or set(data)-{'name','short'} or not isinstance(data['name'],str) or not 1<=len(data['name'].strip())<=80:
            raise ApiError(400,'請輸入 1 至 80 字的人員姓名。')
        if 'short' in data and (not isinstance(data['short'],str) or not 1<=len(data['short'].strip())<=8):
            raise ApiError(400,'縮寫須為 1 至 8 字。')
        name = data['name'].strip()
        db.execute('BEGIN IMMEDIATE')
        person_id = secrets.token_hex(12) if method=='POST' else path.rsplit('/',1)[1]
        existing = db.execute('SELECT * FROM personnel WHERE id=?',(person_id,)).fetchone()
        if method=='PUT' and not existing:
            raise ApiError(404,'找不到人員。')
        short = data['short'].strip() if 'short' in data else existing['short'] if existing else next_person_short(db)
        if db.execute('SELECT 1 FROM personnel WHERE name=? AND id!=?',(name,person_id)).fetchone():
            raise ApiError(409,'此人員姓名已存在。')
        if any(person['id']!=person_id and person['short'].casefold()==short.casefold() for person in db.execute('SELECT id,short FROM personnel')):
            raise ApiError(409,'此人員縮寫已存在。')
        if method=='POST':
            db.execute('INSERT INTO personnel(id,name,short) VALUES(?,?,?)',(person_id,name,short))
        else:
            db.execute('UPDATE personnel SET name=?,short=? WHERE id=?',(name,short,person_id))
        self.reply(200,{'person':{'id':person_id,'name':name,'short':short}})

    def handle_request(self):
        try:
            self.dispatch()
        except ApiError as error:
            self.reply(error.status, {'error':error.message})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            logging.exception('Request failed')
            self.reply(500, {'error':'服務暫時無法處理請求，請稍後重試。'})

    do_GET = do_POST = do_PUT = handle_request


def public_origin(value):
    parsed = urlsplit(value)
    try:
        valid_port = parsed.port in (None,443)
    except ValueError:
        valid_port = False
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.path not in ('','/') or parsed.query or parsed.fragment or not valid_port):
        raise argparse.ArgumentTypeError('public origin must be one HTTPS origin without a path')
    return f'https://{parsed.netloc.lower()}'


def create_server(port, db_path, origin=None):
    origin = public_origin(origin) if origin else None
    initialize(db_path)
    if origin:
        with connect(db_path) as db:
            for account in db.execute('SELECT username,salt,password_hash FROM users'):
                if account['username'] in ('admin','user') and hmac.compare_digest(
                        account['password_hash'], password_hash(account['username']+'123!',account['salt'])):
                    raise ValueError('Public preview requires non-default passwords for admin and user.')
    server = ThreadingHTTPServer(('127.0.0.1',port), Handler)
    server.db_path = db_path
    server.public_origin = origin
    return server


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port',type=int,default=8765)
    parser.add_argument('--db',type=Path,default=ROOT/'data'/'deli.sqlite3')
    parser.add_argument('--public-origin',type=public_origin,help='Exact HTTPS origin allowed through a temporary tunnel')
    args = parser.parse_args()
    with create_server(args.port,args.db,args.public_origin) as server:
        print(f'Deli dashboard: http://127.0.0.1:{server.server_port}',flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
