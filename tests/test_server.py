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
        sample = next(o for o in data['orders'] if o['id']=='WO-1101')
        self.assertEqual(sample['title'],'設備定期保養')
        self.assertEqual(sample['person1'],'人員 1')
        self.assertEqual(sample['person2'],'人員 2')
        self.assertEqual(sample['address'],'新北市永和區')
        self.assertEqual(sample['phone'],'02-0000-0000')
        self.assertEqual(sample['content'],'示意內容：設備定期保養。\n到場後與門市確認作業範圍，完成檢查後回報處理結果。')
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


    def test_07_field_permissions_and_atomic_order_save(self):
        admin,user = self.login(),self.login('user')
        payload = {'title':'新的檢修項目','customer':'測試客戶','region':'北部','address':'測試地址','phone':'02-0000-1234',
                   'content':'第一行\n第二行','notes':'管理員備註','status':'進行中','priority':'urgent','person1Id':'p1','person2Id':'p2',
                   'assignment':{'teamId':'north-a','date':'2031-01-01','start':'09:00','end':'10:00'}}
        status,result,_ = self.request('PUT','/api/orders/WO-1101',payload,admin)
        self.assertEqual(status,200)
        for key,value in payload.items():
            if key!='assignment': self.assertEqual(result['order'][key],value)
        before = result['order']
        from server import connect
        with connect(self.db_path) as db:
            db.execute("UPDATE work_orders SET person2_id=person1_id WHERE id='WO-1102'")
        self.assertEqual(self.request('PUT','/api/orders/WO-1102',{'notes':'重複舊人員仍可加註'},user)[0],200)
        with connect(self.db_path) as db:
            db.execute("UPDATE work_orders SET person2_id='p2',notes='' WHERE id='WO-1102'")
        self.assertEqual(self.request('PUT','/api/orders/WO-1101',{'notes':'不得寫入','title':'越權修改'},user)[0],403)
        data = self.request('GET','/api/dashboard',cookie=user)[1]
        self.assertEqual(next(o for o in data['orders'] if o['id']=='WO-1101'),before)
        status,result,_ = self.request('PUT','/api/orders/WO-1101',{'notes':'使用者備註'},user)
        self.assertEqual(status,200)
        self.assertEqual(result['order'],{**before,'notes':'使用者備註'})
        self.assertEqual(self.request('PUT','/api/orders/WO-1101',{'person1Id':'p1','person2Id':'p1'},admin)[0],400)
        self.assertEqual(self.request('PUT','/api/orders/WO-1101',{'person1Id':'missing'},admin)[0],400)
        conflict = {'title':'不應儲存的名稱','notes':'不應儲存的備註','assignment':payload['assignment']}
        self.assertEqual(self.request('PUT','/api/orders/WO-1102',conflict,admin)[0],409)
        after = self.request('GET','/api/dashboard',cookie=admin)[1]
        original = next(o for o in after['orders'] if o['id']=='WO-1102')
        self.assertEqual(original['title'],'冷藏設備檢修')
        self.assertEqual(original['notes'],'')
        self.assertEqual(self.request('PUT','/api/orders/WO-1101',{'notes':'備'*5000},user)[0],200)
        self.assertEqual(self.request('PUT','/api/orders/WO-1101',{'assignment':None},user)[0],403)
        self.assertEqual(self.request('PUT','/api/orders/WO-1101',{'assignment':None},admin)[0],200)
        after = self.request('GET','/api/dashboard',cookie=admin)[1]
        self.assertFalse(any(a['workOrderId']=='WO-1101' for a in after['assignments']))

    def test_08_personnel_management(self):
        admin,user = self.login(),self.login('user')
        data = self.request('GET','/api/dashboard',cookie=admin)[1]
        self.assertEqual(len(data['personnel']),6)
        self.assertEqual(self.request('POST','/api/personnel',{'name':'無權新增'},user)[0],403)
        self.assertEqual(self.request('PUT','/api/personnel/p1',{'name':'無權修改'},user)[0],403)
        self.assertEqual(self.request('POST','/api/personnel',{'name':''},admin)[0],400)
        self.assertEqual(self.request('PUT','/api/personnel/p1',{'name':'人員 2'},admin)[0],409)
        self.assertEqual(self.request('PUT','/api/personnel/p1',{'name':'改名人員'},admin)[0],200)
        data = self.request('GET','/api/dashboard',cookie=user)[1]
        self.assertEqual(next(o for o in data['orders'] if o['id']=='WO-1101')['person1'],'改名人員')
        self.assertEqual(self.request('PUT','/api/personnel/p1',{'name':'人員 1'},admin)[0],200)
        result = self.request('POST','/api/personnel',{'name':'新增人員'},admin)
        self.assertEqual(result[0],200)
        self.assertEqual(result[1]['person']['name'],'新增人員')


    def test_09_create_orders_permissions_and_rollback(self):
        admin,user = self.login(),self.login('user')
        data = self.request('GET','/api/dashboard',cookie=admin)[1]
        count = len(data['orders'])
        self.assertEqual(self.request('POST','/api/orders',{'title':'越權','customer':'門市'},user)[0],403)
        self.assertEqual(self.request('POST','/api/orders',{'title':'缺客戶'},admin)[0],400)
        invalid = {'title':'無效工單','customer':'門市','person1Id':'p1','person2Id':'p1'}
        self.assertEqual(self.request('POST','/api/orders',invalid,admin)[0],400)
        self.assertEqual(len(self.request('GET','/api/dashboard',cookie=admin)[1]['orders']),count)
        body = {'title':'新建保養','customer':'新客戶','region':'南部','address':'新地址','phone':'07-0000-1234','content':'建立測試',
                'notes':'建立備註','status':'待出勤','priority':'urgent','person1Id':'p3','person2Id':'p4','assignment':None}
        status,result,_ = self.request('POST','/api/orders',body,admin)
        self.assertEqual(status,201)
        self.assertRegex(result['order']['id'],r'^WO-[0-9]+$')
        for key,value in body.items():
            if key!='assignment': self.assertEqual(result['order'][key],value)
        data = self.request('GET','/api/dashboard',cookie=user)[1]
        self.assertFalse(any(a['workOrderId']==result['order']['id'] for a in data['assignments']))
        planned = {**body,'title':'連同派工建立','assignment':{'teamId':'south-a','date':'2032-01-01','start':'09:00','end':'10:00'}}
        status,result,_ = self.request('POST','/api/orders',planned,admin)
        self.assertEqual(status,201)
        self.assertEqual(self.request('POST','/api/orders',{**planned,'title':'不應留下的工單'},admin)[0],409)
        data = self.request('GET','/api/dashboard',cookie=admin)[1]
        self.assertEqual(len(data['orders']),count+2)
        self.assertFalse(any(o['title']=='不應留下的工單' for o in data['orders']))
        self.assertTrue(any(a['workOrderId']==result['order']['id'] for a in data['assignments']))
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda n:self.request('POST','/api/orders',{'title':f'並行工單 {n}','customer':'測試門市'},admin),[1,2]))
        self.assertEqual([r[0] for r in results],[201,201])
        self.assertNotEqual(results[0][1]['order']['id'],results[1][1]['order']['id'])

    def test_10_personnel_abbreviations(self):
        admin = self.login()
        body={'name':'人員 1','short':'QL'}
        self.assertEqual(self.request('PUT','/api/personnel/p1',body,admin)[0],200)
        self.assertEqual(self.request('PUT','/api/personnel/p2',{'name':'人員 2','short':'ql'},admin)[0],409)
        self.assertEqual(self.request('POST','/api/personnel',{'name':'空縮寫','short':''},admin)[0],400)
        self.assertEqual(self.request('PUT','/api/personnel/p1',{'name':'改名保持縮寫'},admin)[1]['person']['short'],'QL')
        self.assertEqual(self.request('PUT','/api/personnel/p1',{'name':'人員 1','short':'Ä'},admin)[0],200)
        self.assertEqual(self.request('PUT','/api/personnel/p2',{'name':'人員 2','short':'ä'},admin)[0],409)
        self.assertEqual(self.request('PUT','/api/personnel/p1',{'name':'人員 1','short':'P1'},admin)[0],200)
        result=self.request('POST','/api/personnel',{'name':'自訂縮寫人員','short':'林'},admin)
        self.assertEqual(result[0],200)
        self.assertEqual(result[1]['person']['short'],'林')


class MigrationTests(unittest.TestCase):
    def test_existing_database_preserves_schedule_and_custom_details(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'old.sqlite3'
            initialize(path)
            with sqlite3.connect(path) as db:
                db.execute("ALTER TABLE work_orders ADD COLUMN person1 TEXT NOT NULL DEFAULT ''")
                db.execute("ALTER TABLE work_orders ADD COLUMN person2 TEXT NOT NULL DEFAULT ''")
                db.execute("UPDATE work_orders SET person1='人員 A（示意）',person2='人員 B（示意）'")
                db.execute("UPDATE work_orders SET person1='王先生',person2='李小姐',phone='02-0000-1234',content='保留自訂內容' WHERE id='WO-1101'")
                for column in ('person1_id','person2_id','notes'):
                    db.execute(f'ALTER TABLE work_orders DROP COLUMN {column}')
                db.execute("UPDATE assignments SET date='2030-01-01' WHERE id='a0'")
            initialize(path)
            initialize(path)
            from server import read_orders
            with sqlite3.connect(path) as db:
                db.row_factory=sqlite3.Row
                self.assertEqual(db.execute("SELECT date FROM assignments WHERE id='a0'").fetchone()[0],'2030-01-01')
                orders=read_orders(db)
                custom=next(o for o in orders if o['id']=='WO-1101')
                self.assertEqual((custom['person1'],custom['person2'],custom['phone'],custom['content'],custom['notes']),('王先生','李小姐','02-0000-1234','保留自訂內容',''))
                demo=next(o for o in orders if o['id']=='WO-1107')
                self.assertEqual((demo['person1'],demo['person2']),('人員 5','人員 6'))
                self.assertEqual(db.execute('SELECT COUNT(*) FROM personnel').fetchone()[0],8)


    def test_personnel_short_migration(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'old-personnel.sqlite3'
            initialize(path)
            with sqlite3.connect(path) as db:
                db.execute('DROP INDEX personnel_short_unique')
                db.execute('ALTER TABLE personnel DROP COLUMN short')
                db.execute("INSERT INTO personnel(id,name) VALUES('custom','舊自訂姓名')")
            initialize(path)
            initialize(path)
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute("SELECT name,short FROM personnel WHERE id='p1'").fetchone(),('人員 1','P1'))
                self.assertEqual(db.execute("SELECT name,short FROM personnel WHERE id='custom'").fetchone(),('舊自訂姓名','P7'))
                db.execute("UPDATE personnel SET short='舊' WHERE id='custom'")
            initialize(path)
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute("SELECT short FROM personnel WHERE id='custom'").fetchone()[0],'舊')


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
