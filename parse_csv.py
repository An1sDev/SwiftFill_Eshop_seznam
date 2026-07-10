import sqlite3
import re
import os
import sys

# --- CONFIG ---
CSV_FILE = 'Firma.csv'
DB_FILE = 'eshopy.db'

# --- 1. Inicializace DB a kontrola struktury ---
conn = sqlite3.connect(DB_FILE, timeout=60)
cursor = conn.cursor()

# Vytvoření tabulky, pokud neexistuje
cursor.execute('''
    CREATE TABLE IF NOT EXISTS seznam_eshopu (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        domena TEXT UNIQUE,
        telefon TEXT,
        email TEXT
    )
''')
conn.commit()

# Pojistka: Pokud by sloupec email chyběl ve staré DB
try:
    cursor.execute('ALTER TABLE seznam_eshopu ADD COLUMN email TEXT')
    conn.commit()
except sqlite3.OperationalError:
    pass

# --- 2. Regexy upravené přesně pro tvůj formát textu ---

# Vyhledá hodnotu v uvozovkách hned za "Web":"
web_regex = re.compile(r'"Web"\s*:\s*"([^"]+)"', re.IGNORECASE)

# Vyhledá hodnotu v uvozovkách hned za "Email":"
email_regex = re.compile(r'"Email"\s*:\s*"([^"]+)"', re.IGNORECASE)

# Vyhledá hodnotu v uvozovkách hned za "Telefon":"
phone_regex = re.compile(r'"Telefon"\s*:\s*"([^"]+)"', re.IGNORECASE)

# Pomocný regex pro ořezání domény na čistý tvar
domain_cleaner = re.compile(r'(?:https?://)?(?:www\.)?([a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)+)', re.IGNORECASE)

# Seznam neplatných hodnot / systémových domén
DOMAIN_BLACKLIST = {
    's.r.o', 'z.s', 'a.s', 'v.o.s', 'k.s', 'seznam.cz', 'gmail.com', 
    'email.cz', 'centrum.cz', 'volny.cz', 'outlook.com', 'post.cz'
}

def get_clean_domain(row_text):
    web_match = web_regex.search(row_text)
    if web_match:
        raw_web = web_match.group(1).strip().lower()
        
        # Očistíme od http, https, www a lomítek
        clean_match = domain_cleaner.search(raw_web)
        if clean_match:
            domain = clean_match.group(1)
            if domain.startswith('www.'):
                domain = domain[4:]
            domain = domain.split('/')[0]
            
            # Odfiltrujeme nesmysly a příliš krátké domény
            if domain in DOMAIN_BLACKLIST or len(domain.split('.')) < 2:
                return None
            return domain
    return None

def get_clean_email(row_text):
    email_match = email_regex.search(row_text)
    if email_match:
        email = email_match.group(1).strip().lower()
        # Pokud je pole prázdné (např. "") nebo obsahuje jen smetí, vrátíme None
        if email and '@' in email:
            return email
    return None

def get_clean_phone(row_text):
    phone_match = phone_regex.search(row_text)
    if phone_match:
        phone = phone_match.group(1).strip()
        # Odstraníme mezery (z "234 602 209" udělá "234602209")
        phone = phone.replace(" ", "")
        if phone:
            return phone
    return None

# --- 3. Spuštění importu ---
if not os.path.exists(CSV_FILE):
    print(f"❌ Soubor {CSV_FILE} nebyl nalezen v aktuální složce!")
    sys.exit(1)

print(f"Spouštím precizní extrakci dat ze souboru {CSV_FILE}...")
print("-" * 50)

inserted_count = 0
updated_count = 0
skipped_count = 0

# Čtení jako surový text po řádcích (UTF-16 podle předchozího zjištění)
with open(CSV_FILE, mode='r', encoding='utf-16', errors='ignore') as f:
    for row_id, line in enumerate(f, 1):
        if not line.strip():
            continue
            
        domain = get_clean_domain(line)
        email = get_clean_email(line)
        phone = get_clean_phone(line)
        
        if domain:
            try:
                # Vložení s aktualizací chybějících polí (ON CONFLICT)
                cursor.execute('''
                    INSERT INTO seznam_eshopu (domena, telefon, email) 
                    VALUES (?, ?, ?)
                    ON CONFLICT(domena) DO UPDATE SET
                        telefon = COALESCE(seznam_eshopu.telefon, EXCLUDED.telefon),
                        email = COALESCE(seznam_eshopu.email, EXCLUDED.email)
                ''', (domain, phone, email))
                
                if cursor.rowcount == 1:
                    inserted_count += 1
                else:
                    updated_count += 1
                    
            except sqlite3.Error as e:
                print(f"Chyba DB na řádku {row_id} ({domain}): {e}")
        else:
            skipped_count += 1

        # Průběžné info do konzole pro kontrolu každých 20 000 řádků
        if row_id % 20000 == 0:
            print(f" Zpracováno {row_id} řádků... (Aktuálně uloženo: {inserted_count})")

# Zápis do databáze
conn.commit()
conn.close()

print("-" * 50)
print(f"✅ Extrakce úspěšně dokončena!")
print(f" - Nově přidané domény: {inserted_count}")
print(f" - Aktualizované kontakty (doplněné k Heurece): {updated_count}")
print(f" - Přeskočené řádky (bez platné domény): {skipped_count}")