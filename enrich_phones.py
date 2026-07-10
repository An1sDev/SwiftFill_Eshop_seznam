import sqlite3
import requests
from bs4 import BeautifulSoup
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
import sys

# --- CONFIG ---
DB_FILE = 'schvalene_eshopy.db'
MAX_WORKERS = 5  # Počet vláken běžících naráz. Doporučuji 5 až 10 (vyšší číslo = větší riziko BANU!)

headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36'
}

db_lock = threading.Lock()
abort_scraping = False  # Globální vlajka pro okamžité zastavení při detekci banu

# --- POMOCNÉ FUNKCE ---
def format_phone(raw_phone):
    if not raw_phone:
        return None
    cleaned = "".join(c for c in raw_phone if c.isdigit() or c == '+')
    if len(cleaned) == 9:
        return "+420" + cleaned
    if cleaned.startswith("420") and len(cleaned) == 12:
        return "+" + cleaned
    if cleaned.startswith("+420") and len(cleaned) == 13:
        return cleaned
    return cleaned

# --- HLAVNÍ FUNKCE PRO JEDNO VLÁKNO ---
def proces_eshop_worker(eshop_id, domena, current_phone, current_email):
    global abort_scraping
    if abort_scraping:
        return None

    need_phone = not current_phone or current_phone in ('', 'nenalezeno')
    need_email = not current_email or current_email in ('', 'nenalezeno')
    
    search_url = f"https://www.firmy.cz/?q={domena}"
    
    try:
        response = requests.get(search_url, headers=headers, timeout=10)
        
        # Detekce ochrany proti botům (Anti-bot / Rate limit)
        if response.status_code in (403, 429):
            print(f"🛑 DETEKOVÁN BAN / BLOKOVÁNÍ (Status {response.status_code}) na doméně {domena}! Zastavuji skript...")
            abort_scraping = True
            return None
            
        if response.status_code != 200:
            return eshop_id, None, None, need_phone, need_email
            
        soup = BeautifulSoup(response.text, 'html.parser')
        articles = soup.select('.premiseList article.premiseBox')
        
        found_phone = None
        found_email = None
        
        for article in articles:
            odkaz_tag = article.select_one('a')
            if not odkaz_tag or not odkaz_tag.get('href'):
                continue
                
            detail_url = odkaz_tag.get('href')
            if detail_url.startswith('/'):
                detail_url = "https://www.firmy.cz" + detail_url
                
            try:
                detail_response = requests.get(detail_url, headers=headers, timeout=10)
                if detail_response.status_code in (403, 429):
                    abort_scraping = True
                    return None
                if detail_response.status_code != 200:
                    continue
            except Exception:
                continue
                
            detail_soup = BeautifulSoup(detail_response.text, 'html.parser')
            
            # Telefon
            if need_phone and not found_phone:
                phone_span = detail_soup.select_one('span[data-dot="origin-phone-number"]')
                if phone_span:
                    found_phone = format_phone(phone_span.get_text(strip=True))
            
            # Email
            if need_email and not found_email:
                email_tag = (
                    detail_soup.select_one('div.detailEmail a[data-dot="e-mail"]') or
                    detail_soup.select_one('a[data-dot="e-mail"]') or
                    detail_soup.select_one('a[data-dot="detail-email"]') or
                    detail_soup.select_one('.detailEmail a') or
                    detail_soup.select_one('a[href^="mailto:"]')
                )
                if email_tag:
                    raw_email = email_tag.get_text(strip=True)
                    href = email_tag.get('href', '')
                    if 'mailto:' in href:
                        raw_email = href.replace('mailto:', '').split('?')[0]
                    if '@' in raw_email:
                        found_email = raw_email.strip().lower()

            if (not need_phone or found_phone) and (not need_email or found_email):
                break
                
        return eshop_id, found_phone, found_email, need_phone, need_email

    except Exception as e:
        # Ignorujeme drobné síťové výpadky u jednotlivých webů
        return eshop_id, None, None, need_phone, need_email

# --- SPUŠTĚNÍ ---
conn = sqlite3.connect(DB_FILE, timeout=60)
cursor = conn.cursor()

try:
    cursor.execute('ALTER TABLE schvalene ADD COLUMN email TEXT')
    conn.commit()
except sqlite3.OperationalError:
    pass

# Načteme všechny e-shopy, co potřebují data
cursor.execute("""
    SELECT id, domena, telefon, email 
    FROM schvalene 
    WHERE (telefon IS NULL OR telefon = '' OR telefon = 'nenalezeno')
       OR (email IS NULL OR email = '' OR email = 'nenalezeno')
""")
eshopy = cursor.fetchall()
conn.close() # Zavřeme hlavní připojení, vlákna si otevřou vlastní/použijí lock

print(f"=== SPUŠTĚNÍ PARALELNÍHO DOHLEDÁVÁNÍ (Firmy.cz) ===")
print(f"Nalezeno {len(eshopy)} e-shopů. Spouštím v {MAX_WORKERS} vláknech naráz...\n" + "-"*50)

success_count = 0

# Spuštění ThreadPoolu
with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
    # Předáme práci všem vláknům
    futures = {
        executor.submit(proces_eshop_worker, eid, dom, tel, eml): dom 
        for eid, dom, tel, eml in eshopy
    }
    
    # Zpracováváme výsledky tak, jak postupně přicházejí z internetu
    for index, future in enumerate(as_completed(futures), 1):
        if abort_scraping:
            print("🛑 Skript byl nouzově ukončen kvůli ochraně před BANem.")
            break
            
        result = future.result()
        if not result:
            continue
            
        eshop_id, f_phone, f_email, n_phone, n_email = result
        domena = futures[future]
        
        # Logování do konzole
        log_msg = f"[{index}/{len(eshopy)}] {domena} -> "
        parts = []
        if n_phone: parts.append(f"Tel: {f_phone if f_phone else 'X'}")
        if n_email: parts.append(f"Email: {f_email if f_email else 'X'}")
        print(log_msg + " | ".join(parts))
        
        # BEZPEČNÝ ZÁPIS DO DB PŘES ZÁMEK
        with db_lock:
            db_conn = sqlite3.connect(DB_FILE, timeout=60)
            db_cursor = db_conn.cursor()
            
            updated_fields = []
            params = []
            
            if n_phone:
                updated_fields.append("telefon = ?")
                params.append(f_phone if f_phone else 'nenalezeno')
            if n_email:
                updated_fields.append("email = ?")
                params.append(f_email if f_email else 'nenalezeno')
                
            if updated_fields:
                params.append(eshop_id)
                db_cursor.execute(f"UPDATE schvalene SET {', '.join(updated_fields)} WHERE id = ?", tuple(params))
                db_conn.commit()
                
            db_conn.close()
            
        if f_phone or f_email:
            success_count += 1

print("-" * 50 + f"\nHotovo! Úspěšně obohaceno {success_count} e-shopů.")