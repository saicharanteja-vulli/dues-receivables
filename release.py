"""Local release workflows: recurring invoices, recovery and reviewed mail outbox."""
import calendar,hashlib,json,os,secrets,sqlite3,time,smtplib,ssl
from datetime import date,timedelta,datetime,timezone
from email.message import EmailMessage
from pydantic import BaseModel,Field
from fastapi import HTTPException,Request

def install(m):
    app=m.app
    def require_owner(r):
        if r.state.user['role']!='owner':raise HTTPException(403,'Owner access required.')
    class Schedule(BaseModel):
        prefix:str=Field(min_length=1,max_length=40,pattern=r'^[a-zA-Z0-9_-]+$')
        customer:str=Field(min_length=1,max_length=100)
        amount:str
        next_date:date
        interval:str=Field(pattern='^(monthly|weekly)$')
        due_days:int=Field(default=14,ge=0,le=90)
    @app.get('/api/schedules')
    def schedules():
        with m.db() as c:return [dict(x) for x in c.execute('SELECT * FROM schedules ORDER BY id DESC')]
    @app.post('/api/schedules')
    def add_schedule(v:Schedule):
        try:n=m.money(v.amount)
        except ValueError as e:raise HTTPException(422,str(e))
        if not m.norm(v.customer):raise HTTPException(422,'Customer is required.')
        if v.next_date<date.today():raise HTTPException(422,'Choose today or a future date.')
        with m.db() as c:
            c.execute('INSERT INTO schedules(prefix,customer,amount,next_date,interval,due_days,anchor_day) VALUES(?,?,?,?,?,?,?)',(v.prefix,m.norm(v.customer),n,v.next_date.isoformat(),v.interval,v.due_days,v.next_date.day))
            m.audit(c,'recurring invoice created',v.prefix)
        return {'ok':True}
    @app.post('/api/schedules/{sid}/toggle')
    def toggle(sid:int):
        with m.db() as c:
            if not c.execute('UPDATE schedules SET active=1-active WHERE id=?',(sid,)).rowcount:raise HTTPException(404)
            m.audit(c,'schedule toggled',str(sid))
        return {'ok':True}
    @app.post('/api/jobs/run')
    def run():
        run_workspace(m,date.today());return {'ok':True}
    @app.get('/api/delivery-mode')
    def delivery_mode():return {'mode':os.getenv('DUES_MAIL_MODE','local')}
    @app.get('/api/outbox')
    def outbox():
        with m.db() as c:return [dict(x) for x in c.execute('SELECT * FROM outbox ORDER BY id DESC LIMIT 100')]
    @app.post('/api/invoices/{iid}/queue-reminder')
    def queue_reminder(iid:int):
        draft=m.reminder(iid)
        if not draft['to']:raise HTTPException(422,'Save this customer’s email before creating a delivery draft.')
        with m.db() as c:
            if c.execute("SELECT 1 FROM outbox WHERE invoice_id=? AND status IN ('draft','sending','unknown')",(iid,)).fetchone():raise HTTPException(409,'There is already a pending draft for this invoice.')
            c.execute('INSERT INTO outbox(invoice_id,recipient,subject,body,created) VALUES(?,?,?,?,?)',(iid,draft['to'],draft['subject'],draft['body'],datetime.now(timezone.utc).isoformat()))
            m.audit(c,'reminder drafted',str(iid))
        return {'ok':True}
    class Approval(BaseModel):
        reviewed:bool=False
    @app.post('/api/outbox/{oid}/send')
    def send(oid:int,v:Approval):
        if not v.reviewed:raise HTTPException(422,'Review the recipient and full message first.')
        with m.db() as c:
            c.execute('BEGIN IMMEDIATE');row=c.execute('SELECT * FROM outbox WHERE id=?',(oid,)).fetchone()
            if not row:raise HTTPException(404)
            if row['status']!='draft':raise HTTPException(409,'This draft was already processed. It will not be sent twice.')
            current=m.reminder(row['invoice_id'])
            if any(row[k]!=current[v] for k,v in [('recipient','to'),('subject','subject'),('body','body')]):raise HTTPException(409,'Invoice or customer changed. Discard this draft and create a fresh one.')
            c.execute("UPDATE outbox SET status='sending' WHERE id=?",(oid,))
        mode=os.getenv('DUES_MAIL_MODE','local');status='local-test';error=''
        if mode=='smtp':
            msg=EmailMessage();msg['To']=row['recipient'];msg['From']=os.environ['DUES_SMTP_FROM'];msg['Subject']=row['subject'];msg['Message-ID']=f'<dues-{m.workspace.get()}-{oid}@{os.environ["DUES_SMTP_FROM"].split("@")[-1]}>';msg.set_content(row['body'])
            try:
                with smtplib.SMTP(os.environ['DUES_SMTP_HOST'],int(os.getenv('DUES_SMTP_PORT','587')),timeout=20) as smtp:
                    smtp.starttls(context=ssl.create_default_context());smtp.login(os.environ['DUES_SMTP_USER'],os.environ['DUES_SMTP_PASSWORD']);smtp.send_message(msg)
                status='sent'
            except Exception:
                status='unknown';error='Delivery could not be confirmed. Check your email provider before retrying. No automatic retry.'
        with m.db() as c:
            c.execute('UPDATE outbox SET status=?,error=? WHERE id=?',(status,error,oid));m.audit(c,'reminder '+status,str(oid))
        return {'status':status,'notice':'Local test recorded. No email was sent.' if mode=='local' else error or 'Email accepted by SMTP server; inbox delivery is not guaranteed.'}
    @app.post('/api/outbox/{oid}/discard')
    def discard(oid:int):
        with m.db() as c:
            if not c.execute("UPDATE outbox SET status='discarded' WHERE id=? AND status='draft'",(oid,)).rowcount:raise HTTPException(409,'Only unsent drafts may be discarded.')
            m.audit(c,'draft discarded',str(oid))
        return {'ok':True}
    class CurrentPassword(BaseModel):
        password:str=Field(max_length=128)
    @app.post('/api/auth/recovery-codes')
    def recovery_codes(v:CurrentPassword,r:Request):
        with m.authdb() as c:
            u=c.execute('SELECT * FROM users WHERE id=?',(r.state.user['id'],)).fetchone()
            if not secrets.compare_digest(m.password_hash(v.password,u['password'].split(':')[0]),u['password']):raise HTTPException(403,'Incorrect password.')
            codes=[secrets.token_hex(12) for _ in range(5)]
            c.execute('DELETE FROM recovery WHERE user_id=?',(u['id'],))
            c.executemany('INSERT INTO recovery(user_id,hash) VALUES(?,?)',[(u['id'],hashlib.sha256(x.encode()).hexdigest()) for x in codes])
        return {'codes':codes,'notice':'Shown once. Save privately. Each code works once; generating new codes invalidates previous codes.'}
    class Recovery(BaseModel):
        username:str=Field(max_length=60)
        code:str=Field(max_length=100)
        password:str=Field(min_length=12,max_length=128)
    @app.post('/api/auth/recover')
    def recover(v:Recovery,r:Request):
        ip=r.client.host if r.client else 'unknown'
        with m.authdb() as c:
            c.execute('BEGIN IMMEDIATE');c.execute('DELETE FROM recovery_attempts WHERE at<?',(time.time()-900,))
            if c.execute('SELECT COUNT(*) FROM recovery_attempts WHERE ip=?',(ip,)).fetchone()[0]>=5:return m.JSONResponse({'detail':'Try again in 15 minutes.'},429)
            c.execute('INSERT INTO recovery_attempts VALUES(?,?)',(ip,time.time()))
            row=c.execute('SELECT r.rowid,r.user_id FROM recovery r JOIN users u ON u.id=r.user_id WHERE u.username=? AND r.hash=?',(v.username.lower(),hashlib.sha256(v.code.encode()).hexdigest())).fetchone()
            if not row:return m.JSONResponse({'detail':'Invalid recovery details.'},400)
            c.execute('DELETE FROM recovery WHERE rowid=?',(row['rowid'],));c.execute('UPDATE users SET password=? WHERE id=?',(m.password_hash(v.password),row['user_id']));c.execute('DELETE FROM sessions WHERE user_id=?',(row['user_id'],))
        return {'ok':True,'notice':'Password reset. Sign in with the new password.'}
    @app.get('/api/operations')
    def operations(r:Request):
        require_owner(r)
        return {'backups_enabled':bool(os.getenv('DUES_BACKUP_KEY')),'mail_mode':os.getenv('DUES_MAIL_MODE','local'),'storage_bytes':(m.DATA/f'workspace-{m.workspace.get()}.db').stat().st_size,'storage_limit_bytes':int(os.getenv('DUES_STORAGE_MB','100'))*1024*1024,'jobs':'Recurring invoices are checked every minute while Dues is running. Sending always requires review.','deployment':'local' if not os.getenv('DUES_PUBLIC_ORIGIN') else 'configured'}

def run_workspace(m,today):
    with m.db() as c:
        c.execute('BEGIN IMMEDIATE')
        for row in c.execute('SELECT * FROM schedules WHERE active=1').fetchall():
            due=date.fromisoformat(row['next_date']);count=0
            while due<=today and count<24:
                number=f'{row["prefix"]}-{row["id"]}-{due.isoformat()}'
                m.add_invoice(c,(m.norm(number),row['customer'],(due+timedelta(days=row['due_days'])).isoformat(),row['amount'],'INR'))
                m.audit(c,'recurring invoice generated',number)
                if row['interval']=='weekly':due+=timedelta(days=7)
                else:
                    month=due.month%12+1;year=due.year+(due.month==12)
                    due=date(year,month,min(row['anchor_day'],calendar.monthrange(year,month)[1]))
                count+=1
            c.execute('UPDATE schedules SET next_date=? WHERE id=?',(due.isoformat(),row['id']))

def run_all(m):
    with m.authdb() as c:wids=[x[0] for x in c.execute('SELECT DISTINCT workspace FROM users')]
    for wid in wids:
        t=m.workspace.set(wid)
        try:
            if (m.DATA/f'workspace-{wid}.db').stat().st_size<int(os.getenv('DUES_STORAGE_MB','100'))*1024*1024:run_workspace(m,date.today())
        finally:m.workspace.reset(t)


def scheduled_backup(m):
    if not os.getenv('DUES_BACKUP_KEY'):return
    import backup_secure
    folder=m.Path(os.getenv('DUES_BACKUP_DIR',str(m.DATA.parent/'backups')));folder.mkdir(parents=True,exist_ok=True,mode=0o700)
    target=folder/(date.today().isoformat()+'.dues-backup')
    if not target.exists():backup_secure.create(m.DATA,target,os.environ['DUES_BACKUP_KEY'].encode())
