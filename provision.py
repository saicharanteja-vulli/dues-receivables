"""Operator-only account setup, using hidden terminal password entry."""
import getpass,secrets,sqlite3
import app
if __name__=='__main__':
    app.validate_config();app.init()
    username=input('Username: ').strip();business=input('Business name: ').strip()
    password=getpass.getpass('Password (12+ characters): ')
    if password!=getpass.getpass('Repeat password: '):raise SystemExit('Passwords do not match.')
    v=app.Credentials(username=username,password=password,business=business)
    wid=secrets.token_hex(16)
    with app.authdb() as c:
        if c.execute('SELECT 1 FROM users WHERE username=?',(v.username.lower(),)).fetchone():raise SystemExit('Username already exists.')
        c.execute('INSERT INTO users(username,password,workspace,role,business) VALUES(?,?,?,?,?)',(v.username.lower(),app.password_hash(v.password),wid,'owner',v.business))
        token=app.workspace.set(wid)
        try:app.init_workspace()
        finally:app.workspace.reset(token)
    print('Private workspace created. Sign in through the app.')
