import sqlite3
import requests
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
import sys
import urllib3
import time
import random

# Vypnutí varování, pokud by to padalo na SSL certifikátech
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- CONFIG ---
DB_FILE = 'schvalene_eshopy.db'
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

    # Vyfiltruje všechno kromě číslic (odstraní +, mezery, závorky atd.)
    digits = "".join(filter(str.isdigit, str(raw_phone)))

    # Pokud máme 9 nebo více čísel, vezmeme jen posledních 9
    # (To spolehlivě ořízne 420, 00420, +420 i případné další nesmysly)
    if len(digits) >= 9:
        return digits[-9:]

    return None  # Pokud je číslo kratší než 9 (např. nějaký nesmysl), vrátíme None


# --- HLAVNÍ FUNKCE PRO JEDNO VLÁKNO ---
def proces_eshop_worker(eshop_id, domena, current_phone, current_email):
    global abort_scraping
    if abort_scraping:
        return None

    need_phone = not current_phone
    need_email = not current_email
    # Předstíráme, že jsme člověk - náhodně čekáme 2 až 5 vteřin před každým hledáním
    time.sleep(random.uniform(1, 3))

    search_url = f"https://www.firmy.cz/?q={domena}"

    # ZDE JE ZMĚNA - Vidíme, že vlákno začalo pracovat
    print(f" ⏳ [Vlákno] Zahajuji hledání: {domena}")

    try:
        # Přísnější Timeout: 3s na spojení, 5s na načtení dat. Zabrání to nekonečnému zaseknutí.
        response = requests.get(search_url, headers=headers, timeout=(3, 5), verify=False)

        if response.status_code in (403, 429):
            print(f"🛑 DETEKOVÁN BAN na {domena}! Firmy.cz nás zablokovaly.")
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

            if need_phone and not found_phone:
                phone_span = detail_soup.select_one('span[data-dot="origin-phone-number"]')
                if phone_span: found_phone = format_phone(phone_span.get_text(strip=True))

            if need_email and not found_email:
                email_tag = (
                        detail_soup.select_one('div.detailEmail a[data-dot="e-mail"]') or
                        detail_soup.select_one('a[data-dot="e-mail"]') or
                        detail_soup.select_one('a[href^="mailto:"]')
                )
                if email_tag:
                    raw_email = email_tag.get_text(strip=True)
                    if '@' in raw_email: found_email = raw_email.strip().lower()

            if (not need_phone or found_phone) and (not need_email or found_email):
                break

        return eshop_id, found_phone, found_email, need_phone, need_email

    except requests.exceptions.ReadTimeout:
        print(f" ⚠️ [Timeout] Server Firmy.cz potichu zahazuje spojení pro {domena} (Možná forma banu).")
        return eshop_id, None, None, need_phone, need_email
    except requests.exceptions.ConnectionError:
        print(f" ⚠️ [Chyba sítě] Nelze se připojit k Firmy.cz pro {domena}.")
        return eshop_id, None, None, need_phone, need_email
    except Exception as e:
        print(f" ⚠️ [Neznámá chyba] u {domena}: {e}")
        return eshop_id, None, None, need_phone, need_email


# --- SPUŠTĚNÍ ---
conn = sqlite3.connect(DB_FILE, timeout=60)
cursor = conn.cursor()
cursor.execute("SELECT id, domena, telefon, email FROM schvalene WHERE telefon IS NULL OR email IS NULL")
eshopy = cursor.fetchall()
conn.close()

print(f"=== SPUŠTĚNÍ PARALELNÍHO DOHLEDÁVÁNÍ (Firmy.cz) ===")
print(f"Nalezeno {len(eshopy)} e-shopů. Spouštím...\n" + "-" * 50)

success_count = 0

with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
    futures = {executor.submit(proces_eshop_worker, eid, dom, tel, eml): dom for eid, dom, tel, eml in eshopy}

    for index, future in enumerate(as_completed(futures), 1):
        if abort_scraping:
            print("🛑 Skript byl nouzově ukončen kvůli ochraně před BANem.")
            break

        result = future.result()
        if not result: continue

        eshop_id, f_phone, f_email, n_phone, n_email = result
        domena = futures[future]

        log_msg = f"✅ [{index}/{len(eshopy)}] {domena} Zpracováno -> "
        parts = []
        if n_phone: parts.append(f"Tel: {f_phone if f_phone else 'Nenalezen'}")
        if n_email: parts.append(f"Email: {f_email if f_email else 'Nenalezen'}")
        print(log_msg + " | ".join(parts))

        # BEZPEČNÝ ZÁPIS DO DB PŘES ZÁMEK
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
                    # Pokus o normální uložení telefonu i e-mailu
                    db_cursor.execute(f"UPDATE schvalene SET {', '.join(updated_fields)} WHERE id = ?", tuple(params))
                    db_conn.commit()

            except sqlite3.IntegrityError as e:
                # Záchytná síť: Pokud databáze odmítne uložit e-mail kvůli duplicitě (UNIQUE constraint)
                if "email" in str(e).lower() or "unique" in str(e).lower():
                    print(f" ⚠️ Duplikát! E-mail u {domena} ignorován (stejný e-mail už má jiný e-shop).")
                    # Pokusíme se zachránit a uložit alespoň telefon, pokud jsme ho našli
                    if n_phone:
                        db_cursor.execute("UPDATE schvalene SET telefon = ? WHERE id = ?", (f_phone, eshop_id))
                        db_conn.commit()
                else:
                    print(f" ❌ Neznámá chyba databáze u {domena}: {e}")

            finally:
                db_conn.close()

        if f_phone or f_email: success_count += 1

print("-" * 50 + f"\nHotovo! Úspěšně obohaceno {success_count} e-shopů.")