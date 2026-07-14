import sqlite3
import requests
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
import sys
import urllib3
import time
import random

# Vypnutí varování pro SSL
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- CONFIG ---
DB_FILE = 'domains.db'
MAX_WORKERS = 2

headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept-Language': 'cs-CZ,cs;q=0.9',
    'Referer': 'https://www.seznam.cz/'
}

db_lock = threading.Lock()
abort_scraping = False


def format_phone(raw_phone):
    if not raw_phone or str(raw_phone).lower() in ('nenalezeno', 'null', 'none', ''):
        return None

    # Vyfiltruje pouze číslice (odstraní mezery, +, závorky, pomlčky)
    digits = "".join(filter(str.isdigit, str(raw_phone)))

    # Pokud máme 9 nebo více čísel, vezmeme nekompromisně posledních 9 číslic
    if len(digits) >= 9:
        return digits[-9:]

    return None  # Pokud je číslo neúplné (nesmysl), vrátíme None


# --- 1. FÁZE: JEDNORÁZOVÝ ÚKLID DATABÁZE PŘED SPUŠTĚNÍM ---
def uklid_telefonnich_cisel():
    print("🧹 Spouštím kontrolu a opravu stávajících telefonních čísel v DB...")
    conn = sqlite3.connect(DB_FILE, timeout=60)
    cursor = conn.cursor()
    
    # Vytáhneme všechny e-shopy, které mají vyplněný telefon
    cursor.execute("SELECT id, telefon FROM domains WHERE is_eshop = 'true' AND telefon IS NOT NULL")
    rows = cursor.fetchall()
    
    updates = []
    for rid, raw_phone in rows:
        formatted = format_phone(raw_phone)
        # Pokud se vyčištěný formát liší od toho, co je v DB, připravíme update
        if formatted != raw_phone:
            updates.append((formatted, rid))
            
    if updates:
        print(f"🔄 Nalezeno {len(updates)} čísel v nesprávném formátu. Převádím na čistých 9 číslic...")
        cursor.executemany("UPDATE domains SET telefon = ? WHERE id = ?", updates)
        conn.commit()
        print("✅ Všechna stávající čísla v DB byla úspěšně opravena.")
    else:
        print("✨ Všechna stávající čísla jsou již v naprostém pořádku.")
        
    conn.close()


# --- HLAVNÍ WORKER PRO VYHLEDÁVÁNÍ ---
def proces_eshop_worker(eshop_id, url_domena, current_phone, current_email):
    global abort_scraping
    if abort_scraping:
        return None

    # Pokud po zformátování dostaneme platné číslo, need_phone bude False a nebudeme ho hledat
    clean_current_phone = format_phone(current_phone)
    need_phone = not clean_current_phone
    need_email = not current_email
    
    # Pokud už máme obojí (telefon i email), vlákno rovnou skončí bez hitování serveru
    if not need_phone and not need_email:
        return eshop_id, clean_current_phone, current_email, False, False
    
    # Náhodné čekání
    time.sleep(random.uniform(1, 3))

    search_url = f"https://www.firmy.cz/?q={url_domena}"
    print(f" ⏳ [Vlákno] Zahajuji hledání: {url_domena} (Potřebuje: {'tel ' if need_phone else ''}{'email' if need_email else ''})")

    try:
        response = requests.get(search_url, headers=headers, timeout=(3, 5), verify=False)

        if response.status_code in (403, 429):
            print(f"🛑 DETEKOVÁN BAN na {url_domena}! Firmy.cz nás zablokovaly.")
            abort_scraping = True
            return None

        if response.status_code != 200:
            return eshop_id, None, None, need_phone, need_email

        soup = BeautifulSoup(response.text, 'html.parser')
        articles = soup.select('.premiseList article.premiseBox')

        found_phone, found_email = None, None

        for article in articles:
            odkaz_tag = article.select_one('a')
            if not odkaz_tag or not odkaz_tag.get('href'): continue

            detail_url = "https://www.firmy.cz" + odkaz_tag.get('href') if odkaz_tag.get('href').startswith(
                '/') else odkaz_tag.get('href')

            try:
                detail_response = requests.get(detail_url, headers=headers, timeout=(3, 5), verify=False)
                if detail_response.status_code != 200: continue
            except Exception:
                continue

            detail_soup = BeautifulSoup(detail_response.text, 'html.parser')

            # Hledáme telefon pouze pokud ho opravdu potřebujeme
            if need_phone and not found_phone:
                phone_span = detail_soup.select_one('span[data-dot="origin-phone-number"]')
                if phone_span: 
                    found_phone = format_phone(phone_span.get_text(strip=True))

            # Hledáme email pouze pokud ho opravdu potřebujeme
            if need_email and not found_email:
                email_tag = (
                        detail_soup.select_one('div.detailEmail a[data-dot="e-mail"]') or
                        detail_soup.select_one('a[data-dot="e-mail"]') or
                        detail_soup.select_one('a[href^="mailto:"]')
                )
                if email_tag:
                    raw_email = email_tag.get_text(strip=True)
                    if '@' in raw_email: found_email = raw_email.strip().lower()

            # Pokud máme vše potřebné splněno, vyskočíme z cyklu detailů dřív
            if (not need_phone or found_phone) and (not need_email or found_email):
                break

        return eshop_id, found_phone, found_email, need_phone, need_email

    except requests.exceptions.ReadTimeout:
        print(f" ⚠️ [Timeout] Server Firmy.cz neodpovídá u {url_domena}.")
        return eshop_id, None, None, need_phone, need_email
    except requests.exceptions.ConnectionError:
        print(f" ⚠️ [Chyba sítě] Nelze se připojit k Firmy.cz pro {url_domena}.")
        return eshop_id, None, None, need_phone, need_email
    except Exception as e:
        print(f" ⚠️ [Neznámá chyba] u {url_domena}: {e}")
        return eshop_id, None, None, need_phone, need_email


# --- SPUŠTĚNÍ ---
# 1. Spustíme pročištění stávajících čísel v DB
uklid_telefonnich_cisel()

# 2. Načteme data pro zpracování
conn = sqlite3.connect(DB_FILE, timeout=60)
cursor = conn.cursor()

# Taháme pouze e-shopy, kde alespoň jeden z kontaktů chybí
cursor.execute("""
    SELECT id, url, telefon, email 
    FROM domains 
    WHERE is_eshop = 'true' 
      AND (telefon IS NULL OR email IS NULL)
""")
eshopy = cursor.fetchall()
conn.close()

print(f"=== SPUŠTĚNÍ PARALELNÍHO DOHLEDÁVÁNÍ KONTAKTŮ (Firmy.cz) ===")
print(f"Nalezeno {len(eshopy)} schválených e-shopů k dohledání. Spouštím...\n" + "-" * 50)

success_count = 0

with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
    futures = {executor.submit(proces_eshop_worker, eid, url, tel, eml): url for eid, url, tel, eml in eshopy}

    for index, future in enumerate(as_completed(futures), 1):
        if abort_scraping:
            print("🛑 Skript byl nouzově ukončen kvůli ochraně před BANem.")
            break

        result = future.result()
        if not result: continue

        eshop_id, f_phone, f_email, n_phone, n_email = result
        url_domena = futures[future]

        # Pokud skript nakonec nic nového nehledal, přeskočíme vypisování logu o zápisu
        if not n_phone and not n_email:
            continue

        log_msg = f"✅ [{index}/{len(eshopy)}] {url_domena} Zpracováno -> "
        parts = []
        if n_phone: parts.append(f"Tel: {f_phone if f_phone else 'Nenalezen'}")
        if n_email: parts.append(f"Email: {f_email if f_email else 'Nenalezen'}")
        print(log_msg + " | ".join(parts))

        # Zápis do DB přes Lock
        with db_lock:
            db_conn = sqlite3.connect(DB_FILE, timeout=60)
            db_cursor = db_conn.cursor()

            try:
                updated_fields = []
                params = []

                if n_phone:
                    updated_fields.append("telefon = ?")
                    params.append(f_phone)
                if n_email:
                    updated_fields.append("email = ?")
                    params.append(f_email)

                if updated_fields:
                    params.append(eshop_id)
                    db_cursor.execute(f"UPDATE domains SET {', '.join(updated_fields)} WHERE id = ?", tuple(params))
                    db_conn.commit()

            except sqlite3.IntegrityError as e:
                if "email" in str(e).lower() or "unique" in str(e).lower():
                    print(f" ⚠️ Duplikát! E-mail u {url_domena} ignorován (již existuje u jiného záznamu).")
                    if n_phone:
                        db_cursor.execute("UPDATE domains SET telefon = ? WHERE id = ?", (f_phone, eshop_id))
                        db_conn.commit()
                else:
                    print(f" ❌ Neznámá chyba databáze u {url_domena}: {e}")

            finally:
                db_conn.close()

        if f_phone or f_email: success_count += 1

print("-" * 50 + f"\nHotovo! Úspěšně obohaceno {success_count} e-shopů.")