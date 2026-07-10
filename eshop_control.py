import os
import re
import sqlite3
import dns.resolver
import requests
from bs4 import BeautifulSoup
from urllib.parse import urlparse, urljoin

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "cs-CZ,cs;q=0.9,en;q=0.8"
}

def check_dns_cloud(domain):
    """Kontrola DNS záznamů na přítomnost známých cloudů"""
    try:
        try:
            answers = dns.resolver.resolve(domain, 'CNAME')
            for rdata in answers:
                cname = str(rdata.target).lower()
                if any(cloud in cname for cloud in ["shoptet", "shopify", "upgates", "fastcentrik", "myshoptet"]):
                    return True, f"Cloud CNAME ({cname})"
        except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN):
            pass

        try:
            answers = dns.resolver.resolve(domain, 'A')
            for rdata in answers:
                ip = str(rdata)
                if ip.startswith("185.83."):  # Shoptet IP rozsah
                    return True, "Shoptet IP adresa"
        except Exception:
            pass
    except Exception:
        pass
    return False, ""

def probe_path_secure(base_url, path, unique_substring):
    """
    OPRAVENO: Bezpečnější kontrola existence platformy.
    Nestačí status 200/403, musíme v HTML/JS ověřit existenci specifického řetězce,
    abychom eliminovali weby, které na neexistující cestu vrací homepage (Alza, Allegro).
    """
    try:
        url = urljoin(base_url, path)
        # Použijeme GET místo HEAD, abychom mohli zkontrolovat obsah
        response = requests.get(url, headers=HEADERS, timeout=4, allow_redirects=True)
        
        if response.status_code == 200:
            # Pokud web vrátil 200, ověříme, že to není podvržená homepage,
            # ale skutečně hledaný skript/složka
            if unique_substring.lower() in response.text.lower():
                return True
    except Exception:
        pass
    return False

def najdi_podstranku(soup, base_url, klicova_slova):
    """Pomocná funkce pro vyhledání odkazu na pokladnu v HTML"""
    for link in soup.find_all('a', href=True):
        href = link['href'].lower()
        text = link.get_text().lower()
        if any(slovo in href or slovo in text for slovo in klicova_slova):
            if any(x in href for x in ["javascript:", "mailto:", "tel:", "#"]):
                continue
            return urljoin(base_url, link['href'])
    return None

def is_valid_eshop_target(url):
    """
    HLAVNÍ FILTRAČNÍ FUNKCE (Gatekeeper) - BEZ PLAYWRIGHTU
    """
    parsed_url = urlparse(url)
    domain = parsed_url.netloc
    
    # 1. KROK: DNS Kontrola na cloudy
    je_cloud, duvod_cloudu = check_dns_cloud(domain)
    if je_cloud:
        return False, f"Cloud ({duvod_cloudu})"

    # 2. KROK: Detekce platformy pomocí ověření obsahu (Zde opraveno o substringy)
    je_woocommerce = probe_path_secure(url, "/wp-content/plugins/woocommerce/", "woocommerce")
    je_prestashop = (probe_path_secure(url, "/modules/blockcart/", "blockcart") or 
                     probe_path_secure(url, "/img/p/", "vlastni_overeni_nebo_nechat_jen_skripty")) # Lepší cílit na js/css
    
    if je_woocommerce:
        platforma = "WooCommerce"
    elif je_prestashop:
        platforma = "PrestaShop"
    else:
        platforma = "Vlastní / Jiná platforma"
        
    # Pokud je to vlastní platforma, zkontrolujeme, zda to není velké Magento Enterprise
    if platforma == "Vlastní / Jiná platforma":
        if probe_path_secure(url, "/errors/design.xml", "magento") or probe_path_secure(url, "/media/catalog/", "catalog"):
            return False, "Magento Enterprise"

    # 3. KROK: Kontrola Smartformu a skrytých validátorů (včetně GTM scriptů)
    kompletni_text = ""
    gtm_ids = set()

    try:
        # Stáhneme hlavní stránku
        r_hp = requests.get(url, headers=HEADERS, timeout=5)
        html_hp = r_hp.text.lower()
        kompletni_text += html_hp
        
        # Vytáhneme případná GTM ID z HP
        found_gtm = re.findall(r'gtm-[a-zA-Z0-9]+', html_hp)
        if found_gtm:
            gtm_ids.update([g.upper() for g in found_gtm])
        
        # Pokusíme se najít odkaz do košíku
        soup_hp = BeautifulSoup(html_hp, 'html.parser')
        url_pokladny = najdi_podstranku(soup_hp, url, ["kosik", "košík", "cart", "checkout", "objednavka"])
        
        # Pokud košík existuje, stáhneme ho celý (ne jen 40kb, skripty bývají na konci!)
        if url_pokladny:
            r_p = requests.get(url_pokladny, headers=HEADERS, timeout=5)
            html_p = r_p.text.lower()
            kompletni_text += " " + html_p
            
            # Vytáhneme GTM ID i z košíku
            found_gtm_p = re.findall(r'gtm-[a-zA-Z0-9]+', html_p)
            if found_gtm_p:
                gtm_ids.update([g.upper() for g in found_gtm_p])
                
        # Proskenujeme samotné vnitřky Google Tag Manager kontejnerů
        for gtm_id in gtm_ids:
            try:
                r_gtm = requests.get(f"https://www.googletagmanager.com/gtm.js?id={gtm_id}", headers=HEADERS, timeout=4)
                if r_gtm.status_code == 200:
                    gtm_text = r_gtm.text.lower()
                    kompletni_text += " " + gtm_text
                    
                    # Zkusíme v GTM textu najít zmínky o foxentry skriptech, i když jsou schované v řetězci.
                    if "foxentry" in gtm_text:
                        kompletni_text += " foxentry "
            except:
                pass

    except Exception:
        pass

    # Vyhodnocení validátorů
    cizi_validatory = ["foxentry", "maps.googleapis.com", "loqate", "addressy", "places.js", "google.maps", "api.mapy.cz"]
    ma_jiny_validator = any(v in kompletni_text for v in cizi_validatory)
    ma_smartform = "smartform" in kompletni_text

    if ma_smartform:
        platforma += " + Smartform"

    # Pokud má cizí validátor a NEMÁ Smartform -> Vyřadit
    if ma_jiny_validator and not ma_smartform:
        return False, f"Cizí validátor (Detekován v kódu/GTM)"

    return True, platforma


if __name__ == "__main__":
    # Zjištění absolutní cesty k adresáři, kde leží tento skript
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    
    # Sestavení absolutních cest k souborům databází
    source_db_path = os.path.join(BASE_DIR, 'eshopy.db')
    target_db_path = os.path.join(BASE_DIR, 'schvalene_eshopy.db')

    # 1. Připojení ke zdrojové databázi (Surové domény z parseru)
    conn_src = sqlite3.connect(source_db_path)
    cursor_src = conn_src.cursor()
    
    # 2. Připojení k NOVÉ cílové databázi (Ukládáme jen schválené čisté kousky)
    conn_dst = sqlite3.connect(target_db_path)
    cursor_dst = conn_dst.cursor()
    
    # Vytvoříme v nové DB čistou tabulku (pokud neexistuje)
    cursor_dst.execute('''
        CREATE TABLE IF NOT EXISTS schvalene (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            domena TEXT UNIQUE,
            platforma TEXT,
            telefon TEXT
        )
    ''')
    # Okamžitý commit vynutí, že soubor schvalene_eshopy.db fyzicky vznikne na disku hned teď
    conn_dst.commit()
    
    # Vytáhneme e-shopy ze zdrojové DB
    try:
        cursor_src.execute("SELECT id, domena FROM seznam_eshopu")
        eshopy = cursor_src.fetchall()
    except sqlite3.OperationalError:
        print(f"[!] Chyba: Tabulka 'seznam_eshopu' v databázi '{source_db_path}' neexistuje. Spusť nejprve parser.py.")
        exit()
    
    print(f"=== SPUŠTĚNÍ FILTRACE (Gatekeeper) ===")
    print(f"Zdrojová DB: {source_db_path}")
    print(f"Cílová DB: {target_db_path}")
    print(f"Načteno {len(eshopy)} domén ze zdrojové DB k prověření.\n" + "-"*50)
    
    for eshop_id, domena in eshopy:
        url = domena if domena.startswith("http") else f"https://www.{domena}"
        
        try:
            prosel, vysledek = is_valid_eshop_target(url)
            
            if prosel:
                print(f"[+] PROŠEL: {domena} ({vysledek}) -> Zapisuji do schvalene_eshopy.db")
                # Zapíšeme e-shop do NOVÉ databáze. 'INSERT OR IGNORE' zajistí unikalitu
                cursor_dst.execute(
                    'INSERT OR IGNORE INTO schvalene (domena, platforma) VALUES (?, ?)', 
                    (domena, vysledek)
                )
                conn_dst.commit()
            else:
                print(f"[-] BLOKOVÁN: {domena} -> Důvod: {vysledek}")
                
        except Exception as e:
            print(f"[!] Chyba při analýze domény {domena}: {e}")
            
    conn_src.close()
    conn_dst.close()
    print("-" * 50 + f"\nFiltrace dokončena. Čistá data uložena v '{target_db_path}'.")