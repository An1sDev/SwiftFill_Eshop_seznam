import sqlite3
import requests
from bs4 import BeautifulSoup
import time

# 1. Připojení k DB a přidání sloupce pro telefon
conn = sqlite3.connect('eshopy.db')
cursor = conn.cursor()

# Zkusíme přidat sloupec. Pokud už existuje, databáze hodí chybu, kterou ignorujeme
try:
    cursor.execute('ALTER TABLE seznam_eshopu ADD COLUMN telefon TEXT')
    conn.commit()
except sqlite3.OperationalError:
    pass 

# Tímto říkáme serveru: "Nejsem robot, jsem Google Chrome na Windows"
headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36'
}

# Vybereme jen ty domény, kde ještě nemáme zapsaný telefon
cursor.execute("SELECT id, domena FROM seznam_eshopu WHERE telefon IS NULL")
eshopy = cursor.fetchall()

print(f"Nalezeno {len(eshopy)} e-shopů k prohledání.\n" + "-"*40)

for eshop_id, domena in eshopy:
    print(f"Hledám: {domena}")
    
    # 2. Hledání na firmy.cz
    search_url = f"https://www.firmy.cz/?q={domena}"
    response = requests.get(search_url, headers=headers)
    
    if response.status_code != 200:
        print(f"  -> Chyba serveru {response.status_code}. Zkouším dál...")
        time.sleep(2)
        continue
        
    soup = BeautifulSoup(response.text, 'html.parser')
    
    # Najdeme všechny výsledky.
    # TIP: Hledám jen ".premiseBox", protože firma může mít zaplacený profil (pak to není freeProfile, ale třeba seznampartnerProfile)
    articles = soup.select('.premiseList article.premiseBox')
    
    telefon_nalezen = False
    
    # 3. Projíždení nalezených firem
    for article in articles:
        odkaz_tag = article.select_one('a')
        
        if not odkaz_tag or not odkaz_tag.get('href'):
            continue
            
        detail_url = odkaz_tag.get('href')
        
        # Někdy systém vrátí relativní cestu, tak ji doplníme na celou URL
        if detail_url.startswith('/'):
            detail_url = "https://www.firmy.cz" + detail_url
            
        # 4. Návštěva detailu firmy
        detail_response = requests.get(detail_url, headers=headers)
        if detail_response.status_code != 200:
            continue
            
        detail_soup = BeautifulSoup(detail_response.text, 'html.parser')
        
        # 5. Hledání samotného telefonního čísla podle data-dot atributu
        phone_span = detail_soup.select_one('span[data-dot="origin-phone-number"]')
        
        if phone_span:
            telefon = phone_span.get_text(strip=True)
            
            # Zápis do databáze přímo k danému ID e-shopu
            cursor.execute('UPDATE seznam_eshopu SET telefon = ? WHERE id = ?', (telefon, eshop_id))
            conn.commit()
            
            print(f"  -> Úspěch, nalezeno číslo: {telefon}")
            telefon_nalezen = True
            break # Našli jsme číslo, ukončujeme prohledávání dalších 'articles' a jdeme na další e-shop
            
    # Pokud vnitřní cyklus projel všechny články a číslo se nenašlo:
    if not telefon_nalezen:
        print("  -> Nenalezeno žádné telefonní číslo.")
        # Zapisujeme do DB hodnotu "nenalezeno", aby se to příště nehledalo znovu
        cursor.execute("UPDATE seznam_eshopu SET telefon = 'nenalezeno' WHERE id = ?", (eshop_id,))
        conn.commit()
        
    # Zásadní krok: pauza na 1.5 vteřiny, abychom nedostali od firmy.cz blokaci
    time.sleep(1.5)

conn.close()
print("-" * 40 + "\nHotovo! Vše prošlo.")