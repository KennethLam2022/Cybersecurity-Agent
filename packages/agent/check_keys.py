import sqlite3, os
db_path = os.path.join(os.getcwd(), 'agent_data', 'conversations.db')
print('DB path:', db_path)
print('Exists:', os.path.exists(db_path))
if os.path.exists(db_path):
    c = sqlite3.connect(db_path)
    tables = c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    print('Tables:', tables)
    rows = c.execute("SELECT provider, api_key_mask FROM llm_provider_keys").fetchall()
    print('LLM Keys in SQLite:')
    for r in rows:
        print(f'  {r[0]}: mask={r[1]}')
    c.close()
