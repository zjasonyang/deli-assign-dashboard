import http.client
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import create_server, initialize


class ServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.db_path = Path(cls.temp.name) / 'test.sqlite3'
        cls.server = create_server(0, cls.db_path)
        cls.port = cls.server.server_port
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.temp.cleanup()

    def request(self, method, path, body=None, cookie=None, origin=True, host=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        headers = {}
        if origin:
            headers['Origin'] = f'http://127.0.0.1:{self.port}'
        if cookie:
            headers['Cookie'] = cookie
        if host:
            headers['Host'] = host
        if body is not None:
            headers['Content-Type'] = 'application/json'
        conn.request(method,path,json.dumps(body) if body is not None else None,headers)
        response = conn.getresponse()
        raw = response.read()
        data = json.loads(raw) if response.getheader('Content-Type','').startswith('application/json') else raw
        result = response.status, data, response.getheader('Set-Cookie','')
        conn.close()
        return result

    def login(self, username='admin'):
        status, data, cookie = self.request('POST','/api/login',{'username':username,'password':username+'123!'})
        self.assertEqual(status,200)
        self.assertEqual(data['user']['role'],username)
        self.assertIn('HttpOnly',cookie)
        self.assertIn('SameSite=Strict',cookie)
        return cookie.split(';')[0]

    def test_01_authentication_and_readonly_role(self):
        self.assertEqual(self.request('GET','/api/dashboard')[0],401)
        self.assertEqual(self.request('PUT','/api/assignments',{})[0],401)
        cookie = self.login('user')
        status, data, _ = self.request('GET','/api/dashboard',cookie=cookie)
        self.assertEqual(status,200)
        self.assertEqual(len(data['teams']),3)
        self.assertNotIn('password_hash',json.dumps(data))
        self.assertEqual(self.request('PUT','/api/assignments',{'role':'admin'},cookie)[0],403)
        counts = {}
        for item in data['assignments']:
            key = item['teamId'],item['date']
            counts[key] = counts.get(key,0)+1
        self.assertEqual(max(counts.values()),5)

    def test_02_save_and_persistence(self):
        cookie = self.login()
        body = {'workOrderId':'WO-1109','teamId':'north-a','date':'2030-01-03','start':'09:00','end':'10:00'}
        status, first, _ = self.request('PUT','/api/assignments',body,cookie)
        self.assertEqual(status,200)
        initialize(self.db_path)
        data = self.request('GET','/api/dashboard',cookie=cookie)[1]
        saved = next(a for a in data['assignments'] if a['workOrderId']=='WO-1109')
        self.assertEqual(saved, first['assignment'])
        body['start'],body['end'] = '10:00','11:00'
        result = self.request('PUT','/api/assignments',body,cookie)
        self.assertEqual(result[0],200)
        self.assertEqual(result[1]['assignment']['id'],saved['id'])

    def test_03_conflict_and_validation(self):
        cookie = self.login()
        body = {'workOrderId':'WO-1110','teamId':'south-a','date':'2030-01-04','start':'09:00','end':'11:00'}
        self.assertEqual(self.request('PUT','/api/assignments',body,cookie)[0],200)
        conflict = {**body,'workOrderId':'WO-1109','start':'10:00','end':'12:00'}
        self.assertEqual(self.request('PUT','/api/assignments',conflict,cookie)[0],409)
        adjacent = {**conflict,'start':'11:00','end':'12:00'}
        self.assertEqual(self.request('PUT','/api/assignments',adjacent,cookie)[0],200)
        for change in [{'date':'2030-02-30'},{'start':'25:00'},{'end':'08:00'},{'teamId':'unknown'},{'date':False}]:
            self.assertEqual(self.request('PUT','/api/assignments',{**body,**change},cookie)[0],400)
        self.assertEqual(self.request('PUT','/api/assignments',{**body,'workOrderId':'missing'},cookie)[0],404)

    def test_04_origin_host_and_logout(self):
        cookie = self.login()
        self.assertEqual(self.request('POST','/api/logout',{},cookie,origin=False)[0],403)
        self.assertEqual(self.request('GET','/api/dashboard',cookie=cookie,host='evil.example')[0],403)
        self.assertEqual(self.request('GET','/seed.json',cookie=cookie)[0],404)
        self.assertEqual(self.request('POST','/api/logout',{},cookie)[0],200)
        self.assertEqual(self.request('GET','/api/me',cookie=cookie)[0],401)

    def test_05_login_failure_and_rate_limit(self):
        for _ in range(5):
            self.assertEqual(self.request('POST','/api/login',{'username':'missing','password':'wrong'})[0],401)
        self.assertEqual(self.request('POST','/api/login',{'username':'missing','password':'wrong'})[0],429)

    def test_06_concurrent_conflict(self):
        cookie = self.login()
        values = {'teamId':'south-b','date':'2030-01-05','start':'09:00','end':'10:00'}
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda order:self.request('PUT','/api/assignments',{**values,'workOrderId':order},cookie)[0],['WO-1109','WO-1110']))
        self.assertEqual(sorted(results),[200,409])


class PublicPreviewTests(unittest.TestCase):
    def test_explicit_https_origin_and_secure_sessions(self):
        import os
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ,{
            'DELI_ADMIN_PASSWORD':'preview-admin-unique', 'DELI_USER_PASSWORD':'preview-user-unique'}):
            origin = 'https://dispatch-preview.example.com'
            server = create_server(0,Path(temp)/'public.sqlite3',origin)
            thread = threading.Thread(target=server.serve_forever,daemon=True)
            thread.start()
            def request(method,path,body=None,request_origin=origin,cookie=None,host='dispatch-preview.example.com'):
                conn = http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=10)
                headers = {'Host':host,'Origin':request_origin,'Content-Type':'application/json'}
                if cookie:
                    headers['Cookie'] = cookie
                conn.request(method,path,json.dumps(body) if body is not None else None,headers)
                response = conn.getresponse()
                result = response.status,response.read(),response.getheader('Set-Cookie','')
                conn.close()
                return result
            try:
                html = request('GET','/')[1].decode()
                self.assertNotIn('admin123!',html)
                self.assertNotIn('user123!',html)
                self.assertEqual(request('GET','/api/dashboard')[0],401)
                for wrong in ['https://evil.example.com','http://dispatch-preview.example.com','null']:
                    self.assertEqual(request('POST','/api/login',{},wrong)[0],403)
                body = {'username':'admin','password':'preview-admin-unique'}
                status,_,cookie = request('POST','/api/login',body)
                self.assertEqual(status,200)
                self.assertIn('; Secure',cookie)
                session = cookie.split(';')[0]
                self.assertEqual(request('GET','/api/me',cookie=session)[0],200)
                status,_,clear = request('POST','/api/logout',{},cookie=session)
                self.assertEqual(status,200)
                self.assertIn('; Secure',clear)
                self.assertEqual(request('GET','/api/me',cookie=session)[0],401)
                self.assertEqual(request('GET','/api/health',host='evil.example.com')[0],403)
            finally:
                server.shutdown(); server.server_close(); thread.join()

    def test_public_preview_refuses_default_accounts(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ValueError,'non-default passwords'):
                create_server(0,Path(temp)/'unsafe.sqlite3','https://preview.example.com')


if __name__ == '__main__':
    unittest.main()
