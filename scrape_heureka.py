import sqlite3
import cloudscraper
import time
import random
import re
import sys

# 1. Inicializace databáze
conn = sqlite3.connect('eshopy.db')
cursor = conn.cursor()
cursor.execute('''
    CREATE TABLE IF NOT EXISTS seznam_eshopu (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        domena TEXT UNIQUE,
        telefon TEXT
    )
''')
conn.commit()

def get_clean_domain(url):
    match = re.search(r'/exit/([a-z0-9-]+)', url)
    if match:
        slug = match.group(1)
        if '-' in slug:
            parts = slug.rsplit('-', 1)
            return f"{parts[0]}.{parts[1]}"
    return None

# Inicializace scraperu
scraper = cloudscraper.create_scraper()
base_url = "https://obchody.heureka.cz/"

# Snadný restart: pokud skript spadne, stačí příště zadat stránku
try:
    start_page = int(input("Na jaké stránce začít? (výchozí 1): ") or 1)
except ValueError:
    start_page = 1

page = start_page

print(f"Spouštím parser od stránky {page}...")
print("-" * 50)

while True:
    url = f"{base_url}?f={page}"
    print(f"\n>>> Načítám stránku {page} ({url})")
    
    success = False
    for attempt in range(3): # Zkusí načíst stránku max 3x
        try:
            response = scraper.get(url, timeout=20)
            if response.status_code == 200:
                success = True
                break
            elif response.status_code == 403:
                print("❌ 403 Forbidden - blokace Cloudflare!")
                time.sleep(20) # Delší pauza při 403
            else:
                print(f"Chyba serveru {response.status_code}, pokus {attempt+1}")
        except Exception as e:
            print(f"Chyba spojení (pokus {attempt+1}): {e}")
            time.sleep(10)
    
    if not success:
        print("Stránku se nepodařilo načíst ani po 3 pokusech. Končím.")
        break

    from bs4 import BeautifulSoup
    soup = BeautifulSoup(response.text, 'html.parser')
    obchody = soup.select('th.c-shops-table__cell--name a.c-shops-table__name')
    
    if not obchody:
        print(f"Info: Na stránce {page} už nebyly nalezeny žádné obchody. Hotovo!")
        break
        
    for obchod in obchody:
        clean_domain = get_clean_domain(obchod.get('href'))
        if clean_domain:
            cursor.execute('INSERT OR IGNORE INTO seznam_eshopu (domena) VALUES (?)', (clean_domain,))
            # Výpis jen v případě úspěšného čištění
            print(f" - Uloženo: {clean_domain}")
    
    conn.commit()
    
    page += 1
    pause = random.uniform(5.0, 12.0) # Trochu delší pauzy pro větší stabilitu
    print(f"Čekám {pause:.2f} sekund...")
    time.sleep(pause)

conn.close()
print("-" * 50)
print("Parser dokončen.")