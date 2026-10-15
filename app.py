import csv,io,json,os,re,sqlite3,hashlib,secrets,time,sys,asyncio,logging
from urllib.parse import urlparse
from contextvars import ContextVar
from decimal import Decimal,InvalidOperation
from datetime import date,datetime,timezone
from pathlib import Path
from contextlib import asynccontextmanager,contextmanager
from fastapi import FastAPI,Request,HTTPException,UploadFile,File
from fastapi.responses import FileResponse,JSONResponse,Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel,Field,ConfigDict
from pypdf import PdfReader
BASE=Path(__file__).resolve().parent
DATA=Path(os.getenv('DUES_DATA',BASE/'data'))
def money(s):
    s=str(s).strip().replace('₹','').replace(',','')
    if not re.fullmatch(r'\d{1,10}(\.\d{1,2})?',s):raise ValueError('Use a positive amount with at most two decimal places.')
    n=int(Decimal(s)*100)
    if not 0<n<=10000000000:raise ValueError('Amount must be between ₹0.01 and ₹10 crore.')
    return n
def norm(s):return ' '.join(str(s).strip().casefold().split())
def iso(s):return date.fromisoformat(str(s).strip()).isoformat()
@contextmanager
def db():
    wid=workspace.get()
    if not wid:raise HTTPException(401,'Sign in to your workspace.')
    c=sqlite3.connect(DATA/f'workspace-{wid}.db',timeout=10);c.row_factory=sqlite3.Row
    c.execute('PRAGMA foreign_keys=ON')
    try:
        yield c;c.commit()
    except: c.rollback();raise
    finally:c.close()
workspace=ContextVar('workspace',default=None)
actor=ContextVar('actor',default='system')
@contextmanager
def authdb():
    c=sqlite3.connect(DATA/'accounts.db',timeout=10);c.row_factory=sqlite3.Row
    try:yield c;c.commit()
    except:c.rollback();raise
    finally:c.close()
def init():
    DATA.mkdir(parents=True,exist_ok=True,mode=0o700);DATA.chmod(0o700)
    with authdb() as c:c.executescript("""
    CREATE TABLE IF NOT EXISTS recovery(user_id INTEGER,hash TEXT UNIQUE);
    CREATE TABLE IF NOT EXISTS recovery_attempts(ip TEXT,at REAL);
    CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,workspace TEXT,role TEXT,business TEXT);
    CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY,user_id INTEGER,expires REAL);
    CREATE TABLE IF NOT EXISTS attempts(ip TEXT,at REAL);
    CREATE TABLE IF NOT EXISTS invites(token TEXT PRIMARY KEY,workspace TEXT,role TEXT,business TEXT,expires REAL);
    """)
    (DATA/'accounts.db').chmod(0o600)
    with authdb() as c:wids=[x[0] for x in c.execute('SELECT DISTINCT workspace FROM users')]
    for wid in wids:
        t=workspace.set(wid)
        try:init_workspace()
        finally:workspace.reset(t)
def init_workspace():
    with db() as c:c.executescript("""
CREATE TABLE IF NOT EXISTS schedules(id INTEGER PRIMARY KEY,prefix TEXT,customer TEXT,amount INTEGER,next_date TEXT,interval TEXT,due_days INTEGER,anchor_day INTEGER,active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS outbox(id INTEGER PRIMARY KEY,invoice_id INTEGER REFERENCES invoices(id),recipient TEXT,subject TEXT,body TEXT,created TEXT,status TEXT DEFAULT 'draft',error TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS invoices(id INTEGER PRIMARY KEY,number TEXT,customer TEXT,due TEXT,amount INTEGER,currency TEXT,cancelled INTEGER DEFAULT 0,UNIQUE(number,customer));
CREATE TABLE IF NOT EXISTS payments(id INTEGER PRIMARY KEY,transaction_id TEXT UNIQUE,customer TEXT,paid_date TEXT,reference TEXT,amount INTEGER,currency TEXT,excluded INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS allocations(id INTEGER PRIMARY KEY,payment_id INTEGER REFERENCES payments(id),invoice_id INTEGER REFERENCES invoices(id),amount INTEGER,active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY,at TEXT,event TEXT,detail TEXT);
CREATE TABLE IF NOT EXISTS credits(id INTEGER PRIMARY KEY,invoice_id INTEGER REFERENCES invoices(id),amount INTEGER,reason TEXT,at TEXT);
CREATE TABLE IF NOT EXISTS refunds(id INTEGER PRIMARY KEY,payment_id INTEGER REFERENCES payments(id),amount INTEGER,reference TEXT UNIQUE,reason TEXT,at TEXT);
CREATE TABLE IF NOT EXISTS previews(digest TEXT PRIMARY KEY,expires REAL);
CREATE TABLE IF NOT EXISTS attachments(invoice_id INTEGER PRIMARY KEY REFERENCES invoices(id),name TEXT,content BLOB);
CREATE TABLE IF NOT EXISTS customers(name TEXT PRIMARY KEY,email TEXT DEFAULT '',notes TEXT DEFAULT '');
""")
    (DATA/f'workspace-{workspace.get()}.db').chmod(0o600)
def password_hash(password,salt=None):
    salt=salt or secrets.token_hex(16)
    return salt+':'+hashlib.scrypt(password.encode(),salt=salt.encode(),n=16384,r=8,p=1).hex()
def audit(c,event,detail):c.execute('INSERT INTO audit(at,event,detail) VALUES(?,?,?)',(datetime.now(timezone.utc).isoformat(),event,actor.get()+': '+detail))
@asynccontextmanager
async def lifespan(app):
    validate_config();init()
    async def worker():
        while True:
            await asyncio.sleep(60)
            try:
                await asyncio.to_thread(release.run_all,sys.modules[__name__])
                await asyncio.to_thread(release.scheduled_backup,sys.modules[__name__])
            except Exception:logging.exception('Dues scheduled job failed')
    task=asyncio.create_task(worker())
    try:yield
    finally:
        task.cancel()
        try:await task
        except asyncio.CancelledError:pass
app=FastAPI(title='Dues',lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)
@app.middleware('http')
async def boundary(r:Request,call_next):
    if r.url.hostname not in ('127.0.0.1','localhost','testserver',urlparse(os.getenv('DUES_PUBLIC_ORIGIN','')).hostname):return JSONResponse({'detail':'Dues is local-only.'},403)
    if r.headers.get('sec-fetch-site')=='cross-site' or (r.headers.get('origin') and r.headers['origin']!=(os.getenv('DUES_PUBLIC_ORIGIN') or str(r.base_url).rstrip('/'))):return JSONResponse({'detail':'Cross-site request blocked.'},403)
    if r.method not in ('GET','HEAD') and r.headers.get('x-dues-client')!='dashboard':return JSONResponse({'detail':'Dashboard header required.'},403)
    # Bound the actual incoming body, including chunked uploads.
    if r.method not in ('GET','HEAD'):
        chunks=[];total=0
        async for chunk in r.stream():
            total+=len(chunk)
            if total>3_000_000:return JSONResponse({'detail':'Upload limit is 3 MB.'},413)
            chunks.append(chunk)
        r._body=b''.join(chunks)
    token=None;actor_token=None
    if r.url.path.startswith('/api/') and r.url.path not in ('/api/auth/register','/api/auth/login','/api/auth/recover','/api/health'):
        raw=r.cookies.get('dues_session','')
        with authdb() as c:
            user=c.execute('SELECT u.* FROM users u JOIN sessions s ON s.user_id=u.id WHERE s.token=? AND s.expires>?',(hashlib.sha256(raw.encode()).hexdigest(),time.time())).fetchone()
        if not user:return JSONResponse({'detail':'Sign in to continue.'},401)
        r.state.user=dict(user)
        if r.method not in ('GET','HEAD') and not r.url.path.startswith('/api/auth/'):
            size=(DATA/f'workspace-{user["workspace"]}.db').stat().st_size
            if size>=int(os.getenv('DUES_STORAGE_MB','100'))*1024*1024:return JSONResponse({'detail':'Workspace storage limit reached. Contact your operator; exports remain available.'},413)
        if r.method not in ('GET','HEAD') and user['role']=='viewer' and r.url.path not in ('/api/auth/logout','/api/auth/password','/api/auth/recovery-codes'):return JSONResponse({'detail':'Your role is read-only.'},403)
        token=workspace.set(user['workspace']);actor_token=actor.set(user['username'])
    try:res=await call_next(r)
    finally:
        if token is not None:workspace.reset(token)
        if actor_token is not None:actor.reset(actor_token)
    res.headers.update({'Cache-Control':'no-store','X-Content-Type-Options':'nosniff','X-Frame-Options':'DENY','Content-Security-Policy':"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; object-src 'none'; frame-ancestors 'none'; base-uri 'none'"})
    return res
class Invoice(BaseModel):
    model_config=ConfigDict(extra='forbid')
    number:str=Field(min_length=1,max_length=80)
    customer:str=Field(min_length=1,max_length=100)
    due:str
    amount:str
    currency:str='INR'
def invoice_row(v):
    v=Invoice.model_validate(v);n=norm(v.number);customer=norm(v.customer)
    if not n or not customer:raise ValueError('Invoice number and customer are required.')
    if v.currency.upper()!='INR':raise ValueError('Only INR is supported in this version.')
    return (n,customer,iso(v.due),money(v.amount),'INR')
def add_invoice(c,row):
    old=c.execute('SELECT * FROM invoices WHERE number=? AND customer=?',row[:2]).fetchone()
    if old:
        if tuple(old[k] for k in ('number','customer','due','amount','currency'))==row:return 'duplicate'
        raise ValueError('Invoice number already exists for this customer with different details.')
    c.execute('INSERT INTO invoices(number,customer,due,amount,currency) VALUES(?,?,?,?,?)',row);return 'imported'
@app.post('/api/invoices')
def save_invoice(v:Invoice):
    try:
        with db() as c:
            result=add_invoice(c,invoice_row(v.model_dump()));audit(c,'invoice',result+': '+v.number)
        return {'result':result}
    except ValueError as e:raise HTTPException(422,str(e))
@app.post('/api/invoices/pdf')
async def extract_pdf(file:UploadFile=File(...)):
    raw=await file.read()
    if not raw.startswith(b'%PDF-'):raise HTTPException(422,'Choose a PDF invoice.')
    try:
        reader=PdfReader(io.BytesIO(raw))
        if reader.is_encrypted or len(reader.pages)>10:raise ValueError('Use an unencrypted PDF with at most 10 pages.')
        text='\n'.join((p.extract_text() or '')[:20000] for p in reader.pages)[:30000]
        if not text.strip():
            if len(reader.pages)>3:raise ValueError('Scanned PDFs are limited to three pages for OCR.')
            import ocr
            text=await asyncio.to_thread(ocr.extract,raw)
            if not text.strip():raise ValueError('No text recognized. Enter the invoice manually.')
    except Exception as e:raise HTTPException(422,str(e)[:180] if isinstance(e,ValueError) else 'Unable to read this PDF. Enter the invoice manually.')
    def pick(pattern):
        m=re.search(pattern,text,re.I|re.M);return m.group(1).strip() if m else ''
    return {'text':text,'draft':{'number':pick(r'Invoice\s*(?:number|no\.?|#)\s*:?\s*([^\n]+)'), 'customer':pick(r'(?:Bill to|Customer)\s*:\s*([^\n]+)'), 'due':pick(r'Due(?: date)?\s*:\s*(\d{4}-\d{2}-\d{2})'), 'amount':pick(r'(?:Grand total|Total due|Amount due)\s*:\s*(?:INR|₹)?\s*([\d,]+(?:\.\d{1,2})?)'),'currency':'INR'},'notice':'Nothing saved. Verify every field against the PDF; extraction is best effort.'}
@app.post('/api/import/{kind}')
async def import_csv(kind:str,file:UploadFile=File(...),preview:bool=True,reviewed:bool=False,mapping:str='{}'):
    if not preview and not reviewed:raise HTTPException(422,'Preview and review the import before saving.')
    if kind not in ('invoices','payments'):raise HTTPException(404)
    try:
        raw=(await file.read()).decode('utf-8-sig');digest=hashlib.sha256((kind+raw).encode()).hexdigest();reader=csv.DictReader(io.StringIO(raw))
        required={'number','customer','due','amount','currency'} if kind=='invoices' else {'transaction_id','customer','paid_date','reference','amount','currency'}
        if not reader.fieldnames or (mapping=='{}' and not required.issubset(reader.fieldnames)):raise ValueError('Required CSV columns: '+', '.join(sorted(required)))
        if len(reader.fieldnames)!=len(set(reader.fieldnames)):raise ValueError('Duplicate CSV column names are not allowed.')
        column_map=json.loads(mapping)
        if not isinstance(column_map,dict) or any(not isinstance(k,str) or not isinstance(v,str) for k,v in column_map.items()):raise ValueError('Invalid column mapping.')
        if column_map:
            if set(column_map)!=required or not set(column_map.values()).issubset(reader.fieldnames) or len(set(column_map.values()))!=len(required):raise ValueError('Map each required field to a different CSV column.')
            rows=[{k:r.get(column_map[k]) for k in required} for r in reader]
        else:rows=list(reader)
        digest=hashlib.sha256((kind+json.dumps(column_map,sort_keys=True)+raw).encode()).hexdigest()
        if not rows or len(rows)>500:raise ValueError('Upload between 1 and 500 rows.')
        results=[]
        with db() as c:
            c.execute('BEGIN IMMEDIATE')
            if not preview and not c.execute('SELECT 1 FROM previews WHERE digest=? AND expires>?',(digest,time.time())).fetchone():raise HTTPException(409,'Preview this exact file before confirming. Previews expire in 30 minutes.')
            for line,r in enumerate(rows,2):
                try:
                    if any(v is None or len(str(v))>500 for v in r.values()):raise ValueError('Missing or oversized fields.')
                    if kind=='invoices':result=add_invoice(c,invoice_row({k:r[k] for k in required}))
                    else:
                        tx=norm(r['transaction_id']);customer=norm(r['customer']);ref=norm(r['reference'])
                        if not tx or not customer:raise ValueError('Transaction ID and customer are required. Use a stable bank ID.')
                        if r['currency'].upper()!='INR':raise ValueError('Only INR is supported.')
                        row=(tx,customer,iso(r['paid_date']),ref,money(r['amount']),'INR')
                        old=c.execute('SELECT * FROM payments WHERE transaction_id=?',(tx,)).fetchone()
                        if old:
                            if tuple(old[k] for k in ('transaction_id','customer','paid_date','reference','amount','currency'))!=row:raise ValueError('Transaction ID conflict: existing payment has different details; row not imported.')
                            result='duplicate'
                        else:
                            c.execute('INSERT INTO payments(transaction_id,customer,paid_date,reference,amount,currency) VALUES(?,?,?,?,?,?)',row);result='imported'
                    results.append({'line':line,'status':result,'values':{k:r[k] for k in sorted(required)}})
                except (ValueError,TypeError) as e:results.append({'line':line,'status':'rejected','reason':str(e)[:200]})
            audit(c,'import',json.dumps({'kind':kind,'imported':sum(x['status']=='imported' for x in results),'duplicates':sum(x['status']=='duplicate' for x in results),'rejected':sum(x['status']=='rejected' for x in results)}))
            if preview:
                c.rollback();c.execute('INSERT OR REPLACE INTO previews VALUES(?,?)',(digest,time.time()+1800))
            else:c.execute('DELETE FROM previews WHERE digest=?',(digest,))
        return {'rows':results,'preview':preview}
    except (ValueError,UnicodeError,csv.Error) as e:raise HTTPException(422,str(e))
def snapshot(c):
    invoices=[dict(x) for x in c.execute('SELECT i.*,COALESCE((SELECT SUM(amount) FROM allocations WHERE invoice_id=i.id AND active=1),0) paid,COALESCE((SELECT SUM(amount) FROM credits WHERE invoice_id=i.id),0) credited FROM invoices i ORDER BY due,id')]
    payments=[dict(x) for x in c.execute('SELECT p.*,COALESCE((SELECT SUM(amount) FROM allocations WHERE payment_id=p.id AND active=1),0) allocated FROM payments p ORDER BY id DESC')]
    for i in invoices:i['has_attachment']=bool(c.execute('SELECT 1 FROM attachments WHERE invoice_id=?',(i['id'],)).fetchone());i['remaining']=0 if i['cancelled'] else i['amount']-i['paid']-i['credited'];i['status']='cancelled' if i['cancelled'] else 'settled' if i['credited'] and not i['remaining'] else 'paid' if not i['remaining'] else 'partial' if i['paid'] else 'unpaid';i['overdue']=i['due']<date.today().isoformat() and i['remaining']>0
    for p in payments:
        p['refunded']=c.execute('SELECT COALESCE(SUM(amount),0) FROM refunds WHERE payment_id=?',(p['id'],)).fetchone()[0];p['remaining']=0 if p['excluded'] else p['amount']-p['allocated']-p['refunded'];p['suggestions']=[]
        p['possible_duplicate']=any(not q['excluded'] and q['id']!=p['id'] and all(q[k]==p[k] for k in ('customer','paid_date','amount','reference','currency')) for q in payments)
        for i in invoices:
            if p['remaining'] and i['remaining'] and p['customer']==i['customer']:
                exact=bool(re.search(r'(?<![\w-])'+re.escape(i['number'])+r'(?![\w-])',p['reference']))
                p['suggestions'].append({'invoice_id':i['id'],'number':i['number'],'remaining':i['remaining'],'amount':min(p['remaining'],i['remaining']),'reason':'Invoice number and customer match' if exact else 'Customer matches; invoice reference not confirmed','rank':2 if exact else 1})
        p['suggestions'].sort(key=lambda x:-x['rank'])
    return {'today':date.today().isoformat(),'refunds':[dict(x) for x in c.execute('SELECT * FROM refunds ORDER BY id DESC')],'customers':[dict(x) for x in c.execute('SELECT * FROM customers ORDER BY name')], 'credits':[dict(x) for x in c.execute('SELECT * FROM credits ORDER BY id DESC')], 'invoices':invoices,'payments':payments,'allocations':[dict(x) for x in c.execute('SELECT a.*,p.transaction_id,i.number FROM allocations a JOIN payments p ON p.id=a.payment_id JOIN invoices i ON i.id=a.invoice_id WHERE active=1 ORDER BY a.id DESC')],'audit':[dict(x) for x in c.execute('SELECT * FROM audit ORDER BY id DESC LIMIT 50')]}
@app.get('/api/state')
def state():
    with db() as c:return snapshot(c)
class Allocation(BaseModel):
    payment_id:int
    invoice_id:int
    amount:str
    reviewed:bool=False
    duplicate_reviewed:bool=False
@app.post('/api/allocate')
def allocate(v:Allocation):
    if not v.reviewed:raise HTTPException(422,'Review this allocation first.')
    try:n=money(v.amount)
    except ValueError as e:raise HTTPException(422,str(e))
    with db() as c:
        c.execute('BEGIN IMMEDIATE');s=snapshot(c)
        p=next((p for p in s['payments'] if p['id']==v.payment_id),None);i=next((i for i in s['invoices'] if i['id']==v.invoice_id),None)
        if not p or not i or p['excluded'] or i['cancelled']:raise HTTPException(404,'Payment or invoice missing.')
        if p['customer']!=i['customer'] or p['currency']!=i['currency']:raise HTTPException(422,'Customer and currency must match.')
        if p['possible_duplicate'] and not v.duplicate_reviewed:raise HTTPException(409,'Possible duplicate: confirm these are distinct bank transactions before allocating.')
        if n>p['remaining'] or n>i['remaining']:raise HTTPException(409,'Balances changed or amount exceeds the unallocated payment / invoice balance.')
        c.execute('INSERT INTO allocations(payment_id,invoice_id,amount) VALUES(?,?,?)',(p['id'],i['id'],n));audit(c,'allocated',f"{p['transaction_id']} → {i['number']}: INR {n/100:.2f}")
    return {'ok':True}
@app.post('/api/allocations/{aid}/undo')
def undo(aid:int):
    with db() as c:
        c.execute('BEGIN IMMEDIATE');r=c.execute('UPDATE allocations SET active=0 WHERE id=? AND active=1',(aid,))
        if not r.rowcount:raise HTTPException(409,'Allocation already reversed or missing.')
        audit(c,'reversed',f'Allocation {aid}; payment and invoice balances restored.')
    return {'ok':True}
@app.get('/api/export')
def export():
    with db() as c:s=snapshot(c)
    out=io.StringIO();w=csv.writer(out);w.writerow(['invoice','customer','due','amount_INR','paid_INR','remaining_INR','status','credits_INR'])
    def safe(v):return "'"+v if str(v).startswith(('=','+','-','@','\t','\r')) else v
    for i in s['invoices']:w.writerow([safe(i['number']),safe(i['customer']),i['due'],f"{i['amount']/100:.2f}",f"{i['paid']/100:.2f}",f"{i['remaining']/100:.2f}",i['status'],f"{i['credited']/100:.2f}"])
    return Response(out.getvalue(),media_type='text/csv',headers={'Content-Disposition':'attachment; filename="dues-reconciliation.csv"'})
@app.get('/samples/{name}')
def sample(name:str):
    if name not in ('invoices.csv','payments.csv','invoice.pdf'):raise HTTPException(404)
    return FileResponse(BASE/'samples'/name,filename=name)
app.mount('/static',StaticFiles(directory=BASE/'static'),name='static')
@app.get('/')
def home():return FileResponse(BASE/'static/index.html')

class Credentials(BaseModel):
    username:str=Field(min_length=3,max_length=60,pattern=r'^[a-zA-Z0-9_.-]+$')
    password:str=Field(min_length=12,max_length=128)
    business:str=Field(default='My business',min_length=1,max_length=100)
    invite:str=''
def session_response(c,user):
    raw=secrets.token_urlsafe(32)
    c.execute('DELETE FROM sessions WHERE expires<?',(time.time(),))
    c.execute('INSERT INTO sessions VALUES(?,?,?)',(hashlib.sha256(raw.encode()).hexdigest(),user['id'],time.time()+43200))
    res=JSONResponse({'username':user['username'],'business':user['business'],'role':user['role']})
    res.set_cookie('dues_session',raw,httponly=True,samesite='strict',secure=os.getenv('DUES_SECURE_COOKIE')=='1',max_age=43200)
    return res
@app.post('/api/auth/register')
def register(v:Credentials,r:Request):
    with authdb() as c:
        c.execute('BEGIN IMMEDIATE')
        if os.getenv('DUES_ALLOW_SIGNUP','1')!='1':raise HTTPException(403,'Registration is closed. Contact the workspace operator.')
        if c.execute('SELECT COUNT(*) FROM users').fetchone()[0]>=int(os.getenv('DUES_MAX_USERS','100')):raise HTTPException(429,'Account capacity reached.')
        if c.execute('SELECT 1 FROM users WHERE username=?',(v.username.lower(),)).fetchone():raise HTTPException(409,'Username already exists.')
        wid=secrets.token_hex(16);role='owner';business=v.business.strip()
        if not business:raise HTTPException(422,'Business name is required.')
        if v.invite:
            inv=c.execute('SELECT * FROM invites WHERE token=? AND expires>?',(hashlib.sha256(v.invite.encode()).hexdigest(),time.time())).fetchone()
            if not inv:raise HTTPException(422,'Invitation is invalid, expired or already used.')
            wid,role,business=inv['workspace'],inv['role'],inv['business']
            c.execute('DELETE FROM invites WHERE token=?',(inv['token'],))
        c.execute('INSERT INTO users(username,password,workspace,role,business) VALUES(?,?,?,?,?)',(v.username.lower(),password_hash(v.password),wid,role,business))
        t=workspace.set(wid)
        try:init_workspace()
        finally:workspace.reset(t)
        user=c.execute('SELECT * FROM users WHERE username=?',(v.username.lower(),)).fetchone()
        return session_response(c,user)
@app.post('/api/auth/login')
def login(v:Credentials,r:Request):
    ip=r.client.host if r.client else 'unknown'
    with authdb() as c:
        c.execute('DELETE FROM attempts WHERE at<?',(time.time()-900,))
        count=c.execute('SELECT COUNT(*) FROM attempts WHERE ip=?',(ip,)).fetchone()[0]
        if count>=10:return JSONResponse({'detail':'Too many attempts. Try again in 15 minutes.'},429)
        c.execute('INSERT INTO attempts VALUES(?,?)',(ip,time.time()))
        user=c.execute('SELECT * FROM users WHERE username=?',(v.username.lower(),)).fetchone()
        stored=user['password'] if user else password_hash('dummy-password-for-timing')
        if not secrets.compare_digest(password_hash(v.password,stored.split(':')[0]),stored) or not user:
            return JSONResponse({'detail':'Incorrect username or password.'},401)
        c.execute('DELETE FROM attempts WHERE ip=?',(ip,))
        return session_response(c,user)
@app.get('/api/auth/me')
def me(r:Request):return {k:r.state.user[k] for k in ('username','business','role')}
@app.post('/api/auth/logout')
def logout(r:Request):
    with authdb() as c:c.execute('DELETE FROM sessions WHERE token=?',(hashlib.sha256(r.cookies.get('dues_session','').encode()).hexdigest(),))
    res=JSONResponse({'ok':True});res.delete_cookie('dues_session');return res
class Invite(BaseModel):
    role:str=Field(pattern='^(editor|viewer)$')
@app.post('/api/team/invite')
def invite(v:Invite,r:Request):
    if r.state.user['role']!='owner':raise HTTPException(403,'Only the owner can invite members.')
    raw=secrets.token_urlsafe(24)
    with authdb() as c:c.execute('INSERT INTO invites VALUES(?,?,?,?,?)',(hashlib.sha256(raw.encode()).hexdigest(),workspace.get(),v.role,r.state.user['business'],time.time()+86400))
    return {'code':raw,'notice':'One use; expires in 24 hours. Share privately with your teammate.'}
@app.get('/api/team')
def team():
    with authdb() as c:return [dict(x) for x in c.execute('SELECT id,username,role FROM users WHERE workspace=?',(workspace.get(),))]
@app.delete('/api/team/{uid}')
def remove_member(uid:int,r:Request):
    if r.state.user['role']!='owner':raise HTTPException(403,'Owner access required.')
    with authdb() as c:
        user=c.execute('SELECT * FROM users WHERE id=? AND workspace=? AND role!=?',(uid,workspace.get(),'owner')).fetchone()
        if not user:raise HTTPException(404,'Team member not found.')
        c.execute('DELETE FROM sessions WHERE user_id=?',(uid,));c.execute('DELETE FROM users WHERE id=?',(uid,))
    return {'ok':True}
@app.get('/api/health')
def health():
    with authdb() as c:c.execute('SELECT 1').fetchone()
    return {'status':'ok'}
@app.put('/api/invoices/{iid}')
def edit_invoice(iid:int,v:Invoice):
    try:row=invoice_row(v.model_dump())
    except ValueError as e:raise HTTPException(422,str(e))
    with db() as c:
        c.execute('BEGIN IMMEDIATE');i=next((x for x in snapshot(c)['invoices'] if x['id']==iid),None)
        if not i:raise HTTPException(404,'Invoice missing.')
        if i['cancelled']:raise HTTPException(409,'Cancelled invoices cannot be edited.')
        if (i['paid'] or i['credited']) and row[1]!=i['customer']:raise HTTPException(409,'Cannot change the customer after payment or credit.')
        if row[3]<i['paid']+i['credited']:raise HTTPException(409,'Total cannot be below payments and credits.')
        try:c.execute('UPDATE invoices SET number=?,customer=?,due=?,amount=?,currency=? WHERE id=?',(*row,iid))
        except sqlite3.IntegrityError:raise HTTPException(409,'Invoice number already used for this customer.')
        audit(c,'invoice edited',json.dumps({'before':i,'after':row}))
    return {'ok':True}
class Note(BaseModel):
    reason:str=Field(min_length=3,max_length=500)
@app.post('/api/invoices/{iid}/cancel')
def cancel_invoice(iid:int,v:Note):
    with db() as c:
        c.execute('BEGIN IMMEDIATE');i=next((x for x in snapshot(c)['invoices'] if x['id']==iid),None)
        if not i:raise HTTPException(404)
        if i['paid'] or i['credited'] or i['cancelled']:raise HTTPException(409,'Only unpaid, uncredited active invoices can be cancelled. Reverse allocations first.')
        c.execute('UPDATE invoices SET cancelled=1 WHERE id=?',(iid,));audit(c,'invoice cancelled',i['number']+': '+v.reason)
    return {'ok':True}
class Credit(Note):
    amount:str
@app.post('/api/invoices/{iid}/credit')
def credit(iid:int,v:Credit):
    try:n=money(v.amount)
    except ValueError as e:raise HTTPException(422,str(e))
    with db() as c:
        c.execute('BEGIN IMMEDIATE');i=next((x for x in snapshot(c)['invoices'] if x['id']==iid),None)
        if not i:raise HTTPException(404)
        if n>i['remaining'] or i['cancelled']:raise HTTPException(409,'Credit exceeds the open invoice balance.')
        c.execute('INSERT INTO credits(invoice_id,amount,reason,at) VALUES(?,?,?,?)',(iid,n,v.reason,datetime.now(timezone.utc).isoformat()));audit(c,'credit note',f'{i["number"]}: {n} paise; {v.reason}')
    return {'ok':True}
@app.post('/api/payments/{pid}/exclude')
def exclude_payment(pid:int,v:Note):
    with db() as c:
        c.execute('BEGIN IMMEDIATE');p=next((x for x in snapshot(c)['payments'] if x['id']==pid),None)
        if not p:raise HTTPException(404)
        if p['allocated'] or p['refunded']:raise HTTPException(409,'Reverse allocations before excluding this receipt.')
        c.execute('UPDATE payments SET excluded=1 WHERE id=?',(pid,));audit(c,'payment excluded',p['transaction_id']+': '+v.reason)
    return {'ok':True}
@app.post('/api/payments/{pid}/restore')
def restore_payment(pid:int):
    with db() as c:
        if not c.execute('UPDATE payments SET excluded=0 WHERE id=? AND excluded=1',(pid,)).rowcount:raise HTTPException(404)
        audit(c,'payment restored',str(pid))
    return {'ok':True}
class Customer(BaseModel):
    name:str=Field(min_length=1,max_length=100)
    email:str=Field(default='',max_length=200)
    notes:str=Field(default='',max_length=1000)
@app.post('/api/customers')
def customer(v:Customer):
    name=norm(v.name)
    if not name or (v.email and not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',v.email)):raise HTTPException(422,'Enter a name and valid email address.')
    with db() as c:
        c.execute('INSERT INTO customers VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET email=excluded.email,notes=excluded.notes',(name,v.email,v.notes));audit(c,'customer saved',name)
    return {'ok':True}
@app.get('/api/invoices/{iid}/reminder')
def reminder(iid:int):
    with db() as c:
        i=next((x for x in snapshot(c)['invoices'] if x['id']==iid),None)
        if not i:raise HTTPException(404)
        if not i['remaining']:raise HTTPException(409,'No outstanding balance to remind about.')
        contact=c.execute('SELECT email FROM customers WHERE name=?',(i['customer'],)).fetchone()
    return {'to':contact['email'] if contact else '', 'subject':f'Payment reminder — invoice {i["number"]}', 'body':f'Hello {i["customer"]},\n\nA friendly reminder about invoice {i["number"]}, due {i["due"]}. The outstanding balance is INR {i["remaining"]/100:.2f}.\n\nIf you have already paid, please share the transaction reference so we can reconcile our records.\n\nThank you!', 'notice':'Draft only. Nothing has been sent.'}
@app.get('/api/reports')
def reports():
    with db() as c:s=snapshot(c)
    buckets={k:0 for k in ('Not due','1–30 days','31–60 days','61–90 days','90+ days')};customers={}
    for i in s['invoices']:
        if i['cancelled']:continue
        days=(date.today()-date.fromisoformat(i['due'])).days
        bucket='Not due' if days<=0 else '1–30 days' if days<=30 else '31–60 days' if days<=60 else '61–90 days' if days<=90 else '90+ days'
        buckets[bucket]+=i['remaining']
        customers[i['customer']]=customers.get(i['customer'],0)+i['remaining']
    return {'aging':buckets,'customers':customers,'as_of':date.today().isoformat()}
@app.get('/api/backup')
def backup(r:Request):
    if r.state.user['role']!='owner':raise HTTPException(403,'Owner access required.')
    with db() as c:
        dest=sqlite3.connect(':memory:');c.backup(dest);raw=dest.serialize();dest.close()
    return Response(raw,media_type='application/octet-stream',headers={'Content-Disposition':'attachment; filename="dues-workspace.db"'})
class PasswordChange(BaseModel):
    current:str=Field(max_length=128)
    new:str=Field(min_length=12,max_length=128)
@app.post('/api/auth/password')
def change_password(v:PasswordChange,r:Request):
    with authdb() as c:
        user=c.execute('SELECT * FROM users WHERE id=?',(r.state.user['id'],)).fetchone()
        if not secrets.compare_digest(password_hash(v.current,user['password'].split(':')[0]),user['password']):raise HTTPException(403,'Current password is incorrect.')
        c.execute('UPDATE users SET password=? WHERE id=?',(password_hash(v.new),user['id']))
        c.execute('DELETE FROM sessions WHERE user_id=?',(user['id'],))
        return session_response(c,user)
@app.post('/api/invoices/{iid}/attachment')
async def attach_invoice(iid:int,file:UploadFile=File(...)):
    raw=await file.read()
    if not raw.startswith(b'%PDF-'):raise HTTPException(422,'Only PDF attachments are supported.')
    with db() as c:
        if not c.execute('SELECT 1 FROM invoices WHERE id=?',(iid,)).fetchone():raise HTTPException(404)
        if c.execute('SELECT 1 FROM attachments WHERE invoice_id=?',(iid,)).fetchone():raise HTTPException(409,'An original is already attached. It cannot be silently replaced.')
        c.execute('INSERT INTO attachments VALUES(?,?,?)',(iid,Path(file.filename or 'invoice.pdf').name[:150],raw));audit(c,'original attached',f'Invoice {iid}; SHA256 {hashlib.sha256(raw).hexdigest()}')
    return {'ok':True}
@app.get('/api/invoices/{iid}/attachment')
def download_attachment(iid:int):
    with db() as c:a=c.execute('SELECT * FROM attachments WHERE invoice_id=?',(iid,)).fetchone()
    if not a:raise HTTPException(404,'No original attached.')
    return Response(a['content'],media_type='application/pdf',headers={'Content-Disposition':f'attachment; filename="invoice-{iid}.pdf"'})
class Refund(Credit):
    reference:str=Field(min_length=1,max_length=100)
@app.post('/api/payments/{pid}/refund')
def refund(pid:int,v:Refund):
    try:n=money(v.amount)
    except ValueError as e:raise HTTPException(422,str(e))
    ref=norm(v.reference)
    if not ref:raise HTTPException(422,'Bank refund reference is required.')
    with db() as c:
        c.execute('BEGIN IMMEDIATE');p=next((x for x in snapshot(c)['payments'] if x['id']==pid),None)
        if not p:raise HTTPException(404)
        if p['excluded'] or n>p['remaining']:raise HTTPException(409,'Refund exceeds available receipt balance. Reverse allocations first if necessary.')
        try:c.execute('INSERT INTO refunds(payment_id,amount,reference,reason,at) VALUES(?,?,?,?,?)',(pid,n,ref,v.reason,datetime.now(timezone.utc).isoformat()))
        except sqlite3.IntegrityError:raise HTTPException(409,'Refund reference already recorded.')
        audit(c,'refund recorded',f'{p["transaction_id"]}: {n} paise; {ref}; {v.reason}. Record only; no funds transferred.')
    return {'ok':True}
@app.post('/api/csv-columns')
async def csv_columns(file:UploadFile=File(...)):
    try:
        reader=csv.reader(io.StringIO((await file.read()).decode('utf-8-sig')));columns=next(reader)
        if not columns or len(columns)>40 or len(set(columns))!=len(columns) or any(not c or len(c)>100 for c in columns):raise ValueError('Use up to 40 unique, nonempty column names.')
        return {'columns':columns}
    except (ValueError,UnicodeError,csv.Error,StopIteration):raise HTTPException(422,'Choose a UTF-8 CSV with a valid header row.')

import release
release.install(sys.modules[__name__])

def validate_config():
    origin=os.getenv('DUES_PUBLIC_ORIGIN','')
    if origin:
        parsed=urlparse(origin)
        if parsed.scheme!='https' or not parsed.hostname or parsed.path or parsed.username or parsed.query or parsed.fragment:raise RuntimeError('DUES_PUBLIC_ORIGIN must be an HTTPS origin without path or credentials.')
        if os.getenv('DUES_SECURE_COOKIE')!='1':raise RuntimeError('Public deployment requires secure cookies.')
        if os.getenv('DUES_ALLOW_SIGNUP')!='0':raise RuntimeError('Public signup must remain closed until email verification is integrated. Pre-provision accounts locally.')
    if os.getenv('DUES_MAIL_MODE','local') not in ('local','smtp'):raise RuntimeError('Invalid mail mode.')
    if os.getenv('DUES_MAIL_MODE')=='smtp' and not all(os.getenv(k) for k in ('DUES_SMTP_HOST','DUES_SMTP_USER','DUES_SMTP_PASSWORD','DUES_SMTP_FROM')):raise RuntimeError('SMTP configuration is incomplete.')
    if int(os.getenv('DUES_STORAGE_MB','100'))<1:raise RuntimeError('Storage limit must be positive.')
