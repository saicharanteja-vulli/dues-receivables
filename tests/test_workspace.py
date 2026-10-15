import sqlite3
import pytest
from fastapi.testclient import TestClient
import app
H={'X-Dues-Client':'dashboard'}
@pytest.fixture
def clients(tmp_path,monkeypatch):
 monkeypatch.setattr(app,'DATA',tmp_path)
 with TestClient(app.app) as a,TestClient(app.app) as b:
  for c,name in ((a,'alice'),(b,'bravo')):
   assert c.post('/api/auth/register',json={'username':name,'password':'a-long-test-password','business':name},headers=H).status_code==200
  yield a,b

def inv(c):return c.post('/api/invoices',json={'number':'INV-1','customer':'maple','amount':'100','due':'2026-10-01'},headers=H)
def upload(c,preview=True):
 if not preview:upload(c,True)
 return c.post('/api/import/payments?preview='+str(preview).lower()+'&reviewed=true',files={'file':('p.csv','transaction_id,customer,paid_date,reference,amount,currency\nb1,maple,2026-10-01,INV-1,40,INR\n')},headers=H)
def test_workspace_isolation(clients):
 a,b=clients;inv(a);upload(a,False)
 assert b.get('/api/state').json()['invoices']==[]
 assert b.get('/api/state').json()['payments']==[]
 assert b.post('/api/allocate',json={'payment_id':1,'invoice_id':1,'amount':'10','reviewed':True},headers=H).status_code==404
 assert b.post('/api/invoices/1/cancel',json={'reason':'not mine'},headers=H).status_code==404
 assert b.get('/api/invoices/1/reminder').status_code==404
 assert 'inv-1' not in b.get('/api/export').text
 assert inv(b).status_code==200
 assert len(a.get('/api/state').json()['invoices'])==1

def test_preview_does_not_write(clients):
 a,_=clients;inv(a)
 assert upload(a).json()['rows'][0]['status']=='imported'
 assert not a.get('/api/state').json()['payments']
 assert upload(a,False).json()['rows'][0]['status']=='imported'
 assert upload(a).json()['rows'][0]['status']=='duplicate'

def test_sessions_and_roles(clients):
 a,b=clients
 with TestClient(app.app) as guest:
  assert guest.get('/api/state').status_code==401
  assert guest.get('/api/backup').status_code==401
  assert guest.post('/api/auth/register',json={'username':'short','password':'tiny'},headers=H).status_code==422
  code=a.post('/api/team/invite',json={'role':'viewer'},headers=H).json()['code']
  assert guest.post('/api/auth/register',json={'username':'reader','password':'reader-test-password','invite':code},headers=H).status_code==200
  inv(a)
  assert len(guest.get('/api/state').json()['invoices'])==1
  assert inv(guest).status_code==403
  assert guest.post('/api/allocations/1/undo',headers=H).status_code==403
  assert guest.get('/api/backup').status_code==403
  uid=next(x['id'] for x in a.get('/api/team').json() if x['username']=='reader')
  assert b.delete(f'/api/team/{uid}',headers=H).status_code==404
  assert a.delete(f'/api/team/{uid}',headers=H).status_code==200
  assert guest.get('/api/state').status_code==401
 assert a.post('/api/auth/logout',headers=H).status_code==200
 assert a.get('/api/state').status_code==401
 assert a.post('/api/auth/login',json={'username':'alice','password':'wrong-but-long-password'},headers=H).status_code==401
 res=a.post('/api/auth/login',json={'username':'alice','password':'a-long-test-password'},headers=H)
 assert res.status_code==200
 assert 'HttpOnly' in res.headers['set-cookie'] and 'SameSite=strict' in res.headers['set-cookie']

def test_credits_edits_exclusions_and_backup(clients):
 a,_=clients;inv(a);upload(a,False)
 assert a.post('/api/allocate',json={'payment_id':1,'invoice_id':1,'amount':'40','reviewed':True},headers=H).status_code==200
 assert a.post('/api/payments/1/exclude',json={'reason':'duplicate'},headers=H).status_code==409
 assert a.post('/api/invoices/1/cancel',json={'reason':'cancelled'},headers=H).status_code==409
 assert a.put('/api/invoices/1',json={'number':'INV-1','customer':'maple','amount':'20','due':'2026-10-02'},headers=H).status_code==409
 assert a.post('/api/invoices/1/credit',json={'amount':'70','reason':'discount'},headers=H).status_code==409
 assert a.post('/api/invoices/1/credit',json={'amount':'10','reason':'discount'},headers=H).status_code==200
 assert a.get('/api/state').json()['invoices'][0]['remaining']==5000
 assert sum(a.get('/api/reports').json()['aging'].values())==5000
 assert '50.00' in a.get('/api/invoices/1/reminder').json()['body']
 backup=a.get('/api/backup');assert backup.status_code==200
 conn=sqlite3.connect(':memory:');conn.deserialize(backup.content)
 assert conn.execute('SELECT amount FROM credits').fetchone()[0]==1000
 conn.close()
 assert a.post('/api/allocations/1/undo',headers=H).status_code==200
 assert a.post('/api/payments/1/exclude',json={'reason':'duplicate'},headers=H).status_code==200
 assert a.get('/api/state').json()['payments'][0]['remaining']==0
 assert a.post('/api/payments/1/restore',headers=H).status_code==200
 assert a.get('/api/state').json()['payments'][0]['remaining']==4000

def test_cancel_and_contacts(clients):
 a,_=clients;inv(a)
 assert a.post('/api/customers',json={'name':'maple','email':'x@example.com','notes':'Monthly'},headers=H).status_code==200
 assert a.get('/api/invoices/1/reminder').json()['to']=='x@example.com'
 assert a.post('/api/invoices/1/cancel',json={'reason':'Issued in error'},headers=H).status_code==200
 i=a.get('/api/state').json()['invoices'][0];assert i['remaining']==0 and i['status']=='cancelled'
 assert sum(a.get('/api/reports').json()['aging'].values())==0

def test_login_rate_limit(clients):
 a,_=clients
 for _ in range(10):assert a.post('/api/auth/login',json={'username':'alice','password':'wrong-long-password'},headers=H).status_code==401
 assert a.post('/api/auth/login',json={'username':'alice','password':'a-long-test-password'},headers=H).status_code==429

def test_preview_bound_to_file_and_workspace(clients):
 a,b=clients
 raw='transaction_id,customer,paid_date,reference,amount,currency\nb1,maple,2026-10-01,INV-1,40,INR\n'
 assert b.post('/api/import/payments?preview=false&reviewed=true',files={'file':('p.csv',raw)},headers=H).status_code==409
 upload(a)
 assert b.post('/api/import/payments?preview=false&reviewed=true',files={'file':('p.csv',raw)},headers=H).status_code==409
 assert a.post('/api/import/payments?preview=false&reviewed=true',files={'file':('p.csv',raw.replace(',40,',',90,'))},headers=H).status_code==409

def test_attachment_and_password(clients):
 a,b=clients;inv(a)
 raw=(app.BASE/'samples/invoice.pdf').read_bytes()
 assert a.post('/api/invoices/1/attachment',files={'file':('i.pdf',raw)},headers=H).status_code==200
 assert a.get('/api/invoices/1/attachment').content==raw
 assert b.get('/api/invoices/1/attachment').status_code==404
 assert a.post('/api/invoices/1/attachment',files={'file':('i.pdf',raw)},headers=H).status_code==409
 assert a.post('/api/auth/password',json={'current':'wrong','new':'a-new-long-password'},headers=H).status_code==403
 assert a.post('/api/auth/password',json={'current':'a-long-test-password','new':'a-new-long-password'},headers=H).status_code==200
 assert a.get('/api/state').status_code==200
 assert a.post('/api/auth/login',json={'username':'alice','password':'a-long-test-password'},headers=H).status_code==401

def test_refund_bounds_and_duplicate_reference(clients):
 a,_=clients;inv(a);upload(a,False)
 payload={'amount':'10','reason':'Returned overpayment','reference':'refund-1'}
 assert a.post('/api/payments/1/refund',json=payload,headers=H).status_code==200
 assert a.get('/api/state').json()['payments'][0]['remaining']==3000
 assert a.post('/api/payments/1/refund',json=payload,headers=H).status_code==409
 assert a.post('/api/payments/1/refund',json={**payload,'amount':'40','reference':'refund-2'},headers=H).status_code==409
 assert a.post('/api/allocate',json={'payment_id':1,'invoice_id':1,'amount':'40','reviewed':True},headers=H).status_code==409
 assert a.post('/api/payments/1/exclude',json={'reason':'duplicate'},headers=H).status_code==409

def test_custom_csv_mapping(clients):
 import json
 a,_=clients
 raw='ID,Client,Date,Value,Unit\nC-1,Maple,2026-10-01,20,INR\n'
 mapping=json.dumps({'number':'ID','customer':'Client','due':'Date','amount':'Value','currency':'Unit'})
 args={'files':{'file':('custom.csv',raw)},'headers':H}
 r=a.post('/api/import/invoices',params={'mapping':mapping},**args)
 assert r.status_code==200 and r.json()['rows'][0]['status']=='imported'
 assert not a.get('/api/state').json()['invoices']
 r=a.post('/api/import/invoices',params={'mapping':mapping,'preview':'false','reviewed':'true'},**args)
 assert r.status_code==200
 assert a.get('/api/state').json()['invoices'][0]['amount']==2000

def test_full_backup_restore(clients,tmp_path):
 import maintenance
 a,_=clients;inv(a)
 out=tmp_path/'backup';restored=tmp_path/'restored'
 assert maintenance.backup(app.DATA,out)==3
 assert maintenance.restore(out,restored)==3
 with sqlite3.connect(restored/'accounts.db') as c:wid=c.execute("SELECT workspace FROM users WHERE username='alice'").fetchone()[0]
 with sqlite3.connect(restored/f'workspace-{wid}.db') as c:assert c.execute('SELECT number FROM invoices').fetchone()[0]=='inv-1'
 with pytest.raises(ValueError):maintenance.restore(out,restored)
 (out/'accounts.db').write_bytes(b'corrupted')
 with pytest.raises(ValueError):maintenance.restore(out,tmp_path/'bad-restore')
