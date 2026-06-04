import sqlite3
db = 'agent_data/conversations.db'
print(f"Checking DB: {db}")
c = sqlite3.connect(db)
tables = c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
print(f'Tables:')
for t in tables:
    count = c.execute(f'SELECT COUNT(*) FROM {t[0]}').fetchone()[0]
    print(f'  {t[0]}: {count} rows')
c.close()
