import sqlite3
import requests
from bs4 import BeautifulSoup
import time

# 1. Připojení k NOVÉ čisté DB od Kroku 2
conn = sqlite3.connect('schvalene_eshopy.db')
cursor = conn.cursor()

headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36'
}

# Vybíráme e-shopy z čisté databáze, kde ještě nemáme zapsaný telefon
cursor.execute("SELECT id, domena FROM schvalene WHERE telefon IS NULL")
eshopy = cursor.fetchall()

print(f"=== SPUŠTĚNÍ DOHLEDÁVÁNÍ TELEFONŮ (Firmy.cz) ===")
print(f"Nalezeno {len(eshopy)} ověřených e-shopů k obohacení.\n" + "-"*50)

for eshop_id, domena in eshopy:
    print(f"Hledám na Firmy.cz: {domena}")
    
    search_url = f"https://www.firmy.cz/?q={domena}"
    response = requests.get(search_url, headers=headers)
    
    if response.status_code != 200:
        print(f"  -> Chyba serveru {response.status_code}. Zkouším dál...")
        time.sleep(2)
        continue
        
    soup = BeautifulSoup(response.text, 'html.parser')
    articles = soup.select('.premiseList article.premiseBox')
    
    telefon_nalezen = False
    
    for article in articles:
        odkaz_tag = article.select_one('a')
        if not odkaz_tag or not odkaz_tag.get('href'):
            continue
            
        detail_url = odkaz_tag.get('href')
        if detail_url.startswith('/'):
            detail_url = "https://www.firmy.cz" + detail_url
            
        detail_response = requests.get(detail_url, headers=headers)
        if detail_response.status_code != 200:
            continue
            
        detail_soup = BeautifulSoup(detail_response.text, 'html.parser')
        phone_span = detail_soup.select_one('span[data-dot="origin-phone-number"]')
        
        if phone_span:
            telefon = phone_span.get_text(strip=True)
            
            # Zápis přímo do tabulky 'schvalene' v nové databázi
            cursor.execute('UPDATE schvalene SET telefon = ? WHERE id = ?', (telefon, eshop_id))
            conn.commit()
            
            print(f"  -> Úspěch, nalezeno číslo: {telefon}")
            telefon_nalezen = True
            break 
            
    if not telefon_nalezen:
        print("  -> V profilech nenalezeno žádné telefonní číslo.")
        cursor.execute("UPDATE schvalene SET telefon = 'nenalezeno' WHERE id = ?", (eshop_id,))
        conn.commit()
        
    time.sleep(1.5)

conn.close()
print("-" * 50 + "\nHotovo! Všechny filtrované e-shopy byly zpracovány.")