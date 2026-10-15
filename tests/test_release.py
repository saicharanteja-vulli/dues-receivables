import os,sqlite3
from datetime import date
import pytest
import app,release
from test_workspace import clients,inv,H

def test_recurring_generation_idempotence(clients):
 a,b=clients
 d={'prefix':'RETAINER','customer':'maple','amount':'100','next_date':date.today().isoformat(),'interval':'monthly','due_days':14}
 assert a.post('/api/schedules',json=d,headers=H).status_code==200
 assert a.post('/api/jobs/run',headers=H).status_code==200
 assert a.post('/api/jobs/run',headers=H).status_code==200
 assert len(a.get('/api/state').json()['invoices'])==1
 assert b.get('/api/schedules').json()==[]
 assert b.post('/api/schedules/1/toggle',headers=H).status_code==404
 assert a.post('/api/schedules/1/toggle',headers=H).status_code==200
 assert a.get('/api/schedules').json()[0]['active']==0

def test_outbox_review_stale_and_local_delivery(clients):
 a,b=clients;inv(a)
 a.post('/api/customers',json={'name':'maple','email':'client@example.com'},headers=H)
 assert a.post('/api/invoices/1/queue-reminder',headers=H).status_code==200
 assert b.get('/api/outbox').json()==[]
 assert b.post('/api/outbox/1/send',json={'reviewed':True},headers=H).status_code==404
 assert a.post('/api/outbox/1/send',json={'reviewed':False},headers=H).status_code==422
 a.post('/api/invoices/1/credit',json={'amount':'10','reason':'discount'},headers=H)
 assert a.post('/api/outbox/1/send',json={'reviewed':True},headers=H).status_code==409
 assert a.post('/api/outbox/1/discard',headers=H).status_code==200
 assert a.post('/api/invoices/1/queue-reminder',headers=H).status_code==200
 assert a.post('/api/outbox/2/send',json={'reviewed':True},headers=H).json()['status']=='local-test'
 assert a.post('/api/outbox/2/send',json={'reviewed':True},headers=H).status_code==409

def test_recovery_one_use_and_session_revocation(clients):
 a,b=clients
 assert a.post('/api/auth/recovery-codes',json={'password':'wrong'},headers=H).status_code==403
 codes=a.post('/api/auth/recovery-codes',json={'password':'a-long-test-password'},headers=H).json()['codes']
 assert len(codes)==5
 payload={'username':'alice','code':codes[0],'password':'replacement-password'}
 assert b.post('/api/auth/recover',json=payload,headers=H).status_code==200
 assert a.get('/api/state').status_code==401
 assert b.post('/api/auth/recover',json=payload,headers=H).status_code==400
 assert a.post('/api/auth/login',json={'username':'alice','password':'replacement-password'},headers=H).status_code==200

def test_config_and_quotas(clients,monkeypatch):
 a,b=clients
 monkeypatch.setenv('DUES_STORAGE_MB','0')
 assert inv(a).status_code==413
 assert a.get('/api/export').status_code==200
 monkeypatch.setenv('DUES_STORAGE_MB','100')
 monkeypatch.setenv('DUES_PUBLIC_ORIGIN','https://dues.example.com')
 with pytest.raises(RuntimeError):app.validate_config()
 monkeypatch.setenv('DUES_SECURE_COOKIE','1');monkeypatch.setenv('DUES_ALLOW_SIGNUP','0')
 app.validate_config()

def test_encrypted_backup_restore(clients,tmp_path):
 from cryptography.fernet import Fernet,InvalidToken
 import backup_secure
 a,_=clients;inv(a);key=Fernet.generate_key();target=tmp_path/'snapshot.dues-backup'
 backup_secure.create(app.DATA,target,key)
 assert b'SQLite format' not in target.read_bytes()
 assert backup_secure.restore(target,tmp_path/'restore',key)==3
 with pytest.raises(InvalidToken):backup_secure.restore(target,tmp_path/'wrong',Fernet.generate_key())
 raw=bytearray(target.read_bytes());raw[-10]^=1;target.write_bytes(raw)
 with pytest.raises(InvalidToken):backup_secure.restore(target,tmp_path/'tampered',key)

def test_scanned_invoice_ocr(clients):
 from pypdf import PdfReader
 a,_=clients;path=app.BASE/'samples/scanned-invoice.pdf'
 assert not (PdfReader(path).pages[0].extract_text() or '').strip()
 r=a.post('/api/invoices/pdf',files={'file':('scan.pdf',path.read_bytes())},headers=H)
 assert r.status_code==200,r.text
 assert r.json()['draft']['number']=='INV-PDF-004'
 assert r.json()['draft']['amount']=='2500.00'
 assert not a.get('/api/state').json()['invoices']

def test_smtp_failure_never_auto_retries(clients,monkeypatch):
 a,_=clients;inv(a);a.post('/api/customers',json={'name':'maple','email':'client@example.com'},headers=H)
 a.post('/api/invoices/1/queue-reminder',headers=H)
 for k,v in {'DUES_MAIL_MODE':'smtp','DUES_SMTP_HOST':'example.invalid','DUES_SMTP_USER':'fixture','DUES_SMTP_PASSWORD':'fixture','DUES_SMTP_FROM':'test@example.com'}.items():monkeypatch.setenv(k,v)
 def fail(*args,**kwargs):raise OSError('Simulated connection failure')
 monkeypatch.setattr(release.smtplib,'SMTP',fail)
 assert a.post('/api/outbox/1/send',json={'reviewed':True},headers=H).json()['status']=='unknown'
 assert a.post('/api/outbox/1/send',json={'reviewed':True},headers=H).status_code==409

def test_month_end_schedule_anchor(clients):
 a,_=clients
 with app.authdb() as c:wid=c.execute("SELECT workspace FROM users WHERE username='alice'").fetchone()[0]
 token=app.workspace.set(wid)
 try:
  with app.db() as c:c.execute("INSERT INTO schedules(prefix,customer,amount,next_date,interval,due_days,anchor_day) VALUES('MONTH','maple',1000,'2028-01-31','monthly',14,31)")
  release.run_workspace(app,date(2028,2,29))
  with app.db() as c:
   assert c.execute('SELECT next_date FROM schedules').fetchone()[0]=='2028-03-31'
   assert c.execute('SELECT COUNT(*) FROM invoices').fetchone()[0]==2
 finally:app.workspace.reset(token)
