import sqlite3
import requests
from bs4 import BeautifulSoup

# 1. Připojení k DB (ukládáme čisté domény)
conn = sqlite3.connect('eshopy.db')
cursor = conn.cursor()

cursor.execute('''
    CREATE TABLE IF NOT EXISTS seznam_eshopu (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        domena TEXT UNIQUE
    )
''')
conn.commit()

url = "https://www.e-shopy.cz/seznam-internetovych-obchodu/"
response = requests.get(url)

if response.status_code == 200:
    soup = BeautifulSoup(response.text, 'html.parser')
    odkazy = soup.select('#seznam-shopy li a')
    
    nove_ulozeno = 0
    
    for odkaz in odkazy:
        href = odkaz.get('href')
        
        if href and href.strip().startswith('http'):
            # KROK 1: Odstraníme "https://odkaz.e-shopy.cz/" a koncové lomítko "/"
            # Z "https://odkaz.e-shopy.cz/uni-max-cz/" vznikne "uni-max-cz"
            slug = href.replace("https://odkaz.e-shopy.cz/", "").strip("/")
            
            # KROK 2: Rozdělíme řetězec od konce podle první nalezené pomlčky
            # "uni-max-cz" -> rozpadne se na "uni-max" a "cz"
            if "-" in slug:
                nazev, tld = slug.rsplit("-", 1)
                cista_domena = f"{nazev}.{tld}" # Spojíme tečkou -> "uni-max.cz"
                
                # KROK 3: Zápis do DB
                cursor.execute('INSERT OR IGNORE INTO seznam_eshopu (domena) VALUES (?)', (cista_domena,))
                
                if cursor.rowcount > 0:
                    nove_ulozeno += 1
                    print(f"Vyčištěno a uloženo: {cista_domena}")

    conn.commit()
    print(f"\nHotovo! Do databáze bylo úspěšně uloženo {nove_ulozeno} čistých domén.")

else:
    print(f"Chyba při načítání stránky: {response.status_code}")

conn.close()