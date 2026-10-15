import io
from concurrent.futures import ThreadPoolExecutor
import pytest
from fastapi.testclient import TestClient
import app
@pytest.fixture
def client(tmp_path,monkeypatch):
 monkeypatch.setattr(app,'DATA',tmp_path)
 with TestClient(app.app) as c:
  assert c.post('/api/auth/register',json={'username':'owner','password':'test-only-password-123','business':'Test business'},headers=H).status_code==200
  yield c
H={'X-Dues-Client':'dashboard'}
def invoice(c,number='inv-1',customer='maple',amount='100.00'):
 return c.post('/api/invoices',json={'number':number,'customer':customer,'amount':amount,'due':'2026-10-01'},headers=H)
def pay(c,rows):
 c.post('/api/import/payments',files={'file':('p.csv','transaction_id,customer,paid_date,reference,amount,currency\n'+rows)},headers=H)
 return c.post('/api/import/payments?preview=false&reviewed=true',files={'file':('p.csv','transaction_id,customer,paid_date,reference,amount,currency\n'+rows)},headers=H)
def allocate(c,p,i,a,**kw):return c.post('/api/allocate',json={'payment_id':p,'invoice_id':i,'amount':a,'reviewed':True,**kw},headers=H)
def test_partial_duplicate_and_remainder(client):
 c=client;assert invoice(c).status_code==200
 rows='b1,maple,2026-10-02,inv-1,40.00,INR\nb2,maple,2026-10-03,inv-1,60.00,INR\nb1,maple,2026-10-02,inv-1,40.00,INR\n'
 assert [r['status'] for r in pay(c,rows).json()['rows']]==['imported','imported','duplicate']
 assert allocate(c,1,1,'40').status_code==200
 i=c.get('/api/state').json()['invoices'][0];assert (i['remaining'],i['status'])==(6000,'partial')
 assert allocate(c,1,1,'40').status_code==409
 assert allocate(c,2,1,'60').status_code==200
 i=c.get('/api/state').json()['invoices'][0];assert (i['remaining'],i['status'])==(0,'paid')
 assert all(r['status']=='duplicate' for r in pay(c,rows).json()['rows'])
 assert len(c.get('/api/state').json()['payments'])==2
 assert c.post('/api/allocations/1/undo',headers=H,json={}).status_code==200
 assert c.get('/api/state').json()['invoices'][0]['remaining']==4000
 assert c.post('/api/allocations/1/undo',headers=H,json={}).status_code==409
 assert allocate(c,1,1,'40').status_code==200
@pytest.mark.parametrize('amount',['-1','0','NaN','Infinity','1.001','1e2','100000001'])
def test_bad_amount(client,amount):assert invoice(client,amount=amount).status_code==422
def test_invoice_duplicate_and_conflict(client):
 assert invoice(client).json()['result']=='imported'
 assert invoice(client,number=' INV-1 ',customer='MAPLE').json()['result']=='duplicate'
 assert invoice(client,amount='101').status_code==422
def test_payment_conflict(client):
 pay(client,'b1,maple,2026-10-02,inv-1,40,INR\n')
 r=pay(client,'b1,maple,2026-10-02,inv-1,50,INR\n').json()['rows'][0]
 assert r['status']=='rejected';assert client.get('/api/state').json()['payments'][0]['amount']==4000
def test_possible_duplicate_guard(client):
 invoice(client)
 pay(client,'b1,maple,2026-10-02,inv-1,40,INR\nb2,maple,2026-10-02,inv-1,40,INR\n')
 assert allocate(client,1,1,'40').status_code==409
 assert allocate(client,1,1,'40',duplicate_reviewed=True).status_code==200
def test_cross_customer_and_overpayment(client):
 invoice(client);invoice(client,'inv-2','cedar')
 pay(client,'b1,maple,2026-10-02,inv-1,120,INR\n')
 assert allocate(client,1,2,'40').status_code==422
 assert allocate(client,1,1,'120').status_code==409
 assert allocate(client,1,1,'100').status_code==200
 assert client.get('/api/state').json()['payments'][0]['remaining']==2000
def test_ambiguous_and_exact_reference(client):
 invoice(client,'inv-1');invoice(client,'inv-10')
 pay(client,'b1,maple,2026-10-02,inv-10,40,INR\n')
 suggestions=client.get('/api/state').json()['payments'][0]['suggestions']
 assert suggestions[0]['number']=='inv-10';assert suggestions[1]['rank']==1
 assert not client.get('/api/state').json()['allocations']
def test_concurrent_allocation(client):
 invoice(client);pay(client,'b1,maple,2026-10-02,inv-1,100,INR\n')
 with ThreadPoolExecutor(2) as ex:codes=list(ex.map(lambda _:allocate(client,1,1,'100').status_code,range(2)))
 assert sorted(codes)==[200,409]
def test_upload_validation(client):
 assert client.post('/api/import/payments?preview=false&reviewed=true',files={'file':('x.csv','hello')},headers=H).status_code==422
 assert client.post('/api/import/payments?preview=false&reviewed=true',files={'file':('x.csv',b'\xff')},headers=H).status_code==422
 assert client.post('/api/import/payments?preview=false&reviewed=true',files={'file':('x.csv',b'x'*3000001)},headers=H).status_code==413
 assert client.post('/api/invoices/pdf',files={'file':('x.pdf',b'notpdf')},headers=H).status_code==422
def test_review_and_boundary(client):
 assert client.post('/api/invoices',json={}).status_code==403
 assert client.get('/api/state',headers={'Sec-Fetch-Site':'cross-site'}).status_code==403
 assert client.get('/api/state',headers={'Host':'evil.example'}).status_code==403
 assert client.post('/api/allocate',json={'payment_id':1,'invoice_id':1,'amount':'1'},headers=H).status_code==422
def test_pdf_review_before_save(client):
 r=client.post('/api/invoices/pdf',files={'file':('invoice.pdf',(app.BASE/'samples/invoice.pdf').read_bytes())},headers=H)
 assert r.status_code==200;assert r.json()['draft']['number']=='INV-PDF-004'
 assert not client.get('/api/state').json()['invoices']
 assert client.post('/api/invoices',json=r.json()['draft'],headers=H).status_code==200
def test_csv_formula_export(client):
 invoice(client,number='=SUM(A1)',customer='+client')
 text=client.get('/api/export').text
 assert "'=sum(a1)" in text and "'+client" in text
