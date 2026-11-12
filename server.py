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


def initialize(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with connect(path) as db:
        db.executescript('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('admin','user')),
                salt TEXT NOT NULL,
                password_hash TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id),
                expires_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS login_attempts (
                username TEXT PRIMARY KEY, failures INTEGER NOT NULL, until REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS teams (
                id TEXT PRIMARY KEY, name TEXT NOT NULL,
                short TEXT NOT NULL, region TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS work_orders (
                id TEXT PRIMARY KEY, title TEXT NOT NULL, customer TEXT NOT NULL,
                region TEXT NOT NULL, address TEXT NOT NULL,
                status TEXT NOT NULL, priority TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS assignments (
                id TEXT PRIMARY KEY,
                work_order_id TEXT UNIQUE NOT NULL REFERENCES work_orders(id),
                team_id TEXT NOT NULL REFERENCES teams(id),
                date TEXT NOT NULL, start TEXT NOT NULL, end TEXT NOT NULL,
                updated_by INTEGER REFERENCES users(id),
                CHECK(start < end)
            );
        ''')
        for username, role, default in [('admin','admin','admin123!'), ('user','user','user123!')]:
            if not db.execute('SELECT 1 FROM users WHERE username = ?', (username,)).fetchone():
                salt = secrets.token_hex(16)
                password = os.environ.get(f'DELI_{role.upper()}_PASSWORD', default)
                db.execute('INSERT INTO users(username,role,salt,password_hash) VALUES(?,?,?,?)',
                           (username, role, salt, password_hash(password, salt)))
        if not db.execute('SELECT 1 FROM teams').fetchone():
            seed = json.loads((ROOT / 'seed.json').read_text())
            db.executemany('INSERT INTO teams VALUES(:id,:name,:short,:region)', seed['teams'])
            db.executemany('INSERT INTO work_orders VALUES(:id,:title,:customer,:region,:address,:status,:priority)', seed['orders'])
            today = datetime.now(ZoneInfo('Asia/Taipei')).date()
            monday = today - timedelta(days=today.weekday())
            for item in seed['assignments']:
                db.execute('INSERT INTO assignments(id,work_order_id,team_id,date,start,end) VALUES(?,?,?,?,?,?)',
                           (item['id'],item['workOrderId'],item['teamId'],str(monday + timedelta(days=item['dayOffset'])),item['start'],item['end']))
        db.execute('DELETE FROM sessions WHERE expires_at <= ?', (int(time.time()),))
    path.chmod(0o600)


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
            if not 0 < length <= 16384:
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
        if method == 'GET' and path == '/':
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
                    'orders':[dict(r) for r in db.execute('SELECT * FROM work_orders ORDER BY id')],
                    'assignments':[dict(r) for r in db.execute('''SELECT id, work_order_id AS workOrderId, team_id AS teamId,
                        date,start,end FROM assignments ORDER BY date,start''')],
                })
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

    def save_assignment(self, db, data, user):
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
        self.reply(200, {'assignment':{'id':assignment_id,**{k:data[k] for k in keys}}})

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
