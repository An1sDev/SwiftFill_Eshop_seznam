import os
import re
import sqlite3
import asyncio
import dns.asyncresolver
import aiohttp
from bs4 import BeautifulSoup
from urllib.parse import urlparse, urljoin

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "cs-CZ,cs;q=0.9,en;q=0.8"
}
CLOUDS = [
    "shoptet", "shopify", "upgates", "fastcentrik", "myshoptet", "webnode", "wix", "squarespace", "byznysweb", "bizweb", 
    "eshop-rychle"
]
CARTS = ["kosik", "košík", "cart", "checkout", "objednavka"]
cizi_validatory = ["foxentry", "maps.googleapis.com", "loqate", "addressy", "places.js", "google.maps", "api.mapy.cz"]


CONCURRENCY_LIMIT = 20
timeout_config = aiohttp.ClientTimeout(total=6)

async def check_dns_cloud_async(domain):
    """Async DNS check for known clouds"""
    resolver = dns.asyncresolver.Resolver()
    resolver.timeout = 2
    resolver.lifetime = 2
    try:
        try:
            answers = await resolver.resolve(domain, 'CNAME')
            for rdata in answers:
                cname = str(rdata.target).lower()
                if any(cloud in cname for cloud in CLOUDS):
                    return True, f"Cloud CNAME ({cname})"
        except Exception:
            pass

        try:
            answers = await resolver.resolve(domain, 'A')
            for rdata in answers:
                ip = str(rdata)
                if ip.startswith("185.83."):
                    return True, "Shoptet IP adresa"
        except Exception:
            pass
    except Exception:
        pass
    return False, ""

async def probe_path_async(session, base_url, path, unique_substring):
    """Async content-verified platform check"""
    try:
        url = urljoin(base_url, path)
        async with session.get(url, headers=HEADERS, timeout=timeout_config, allow_redirects=True) as response:
            if response.status == 200:
                text = await response.text()
                if unique_substring.lower() in text.lower():
                    return True
    except Exception:
        pass
    return False

def najdi_podstranku(soup, base_url, klicova_slova):
    """Helper to find checkout/cart links"""
    for link in soup.find_all('a', href=True):
        href = link['href'].lower()
        text = link.get_text().lower()
        if any(slovo in href or slovo in text for slovo in klicova_slova):
            if any(x in href for x in ["javascript:", "mailto:", "tel:", "#"]):
                continue
            return urljoin(base_url, link['href'])
    return None

async def is_valid_eshop_target_async(session, url):
    """Main Gatekeeper analysis - Non-blocking async"""
    parsed_url = urlparse(url)
    domain = parsed_url.netloc
    
    # 1. KROK: DNS Kontrola
    je_cloud, duvod_cloudu = await check_dns_cloud_async(domain)
    if je_cloud:
        return False, f"Cloud ({duvod_cloudu})"

    # 2. KROK: Platform checks (Run concurrently)
    task_woo = probe_path_async(session, url, "/wp-content/plugins/woocommerce/", "woocommerce")
    task_presta_1 = probe_path_async(session, url, "/modules/blockcart/", "blockcart")
    # OPRAVENO: Změněno z nefunkčního /img/p/ na globální core.js
    task_presta_2 = probe_path_async(session, url, "/themes/core.js", "prestashop")
    
    je_woocommerce, je_prestashop_1, je_prestashop_2 = await asyncio.gather(task_woo, task_presta_1, task_presta_2)
    je_prestashop = je_prestashop_1 or je_prestashop_2
    
    if je_woocommerce:
        platforma = "WooCommerce"
    elif je_prestashop:
        platforma = "PrestaShop"
    else:
        platforma = "Vlastní / Jiná platforma"
        
    if platforma == "Vlastní / Jiná platforma":
        task_mag_1 = probe_path_async(session, url, "/errors/design.xml", "magento")
        task_mag_2 = probe_path_async(session, url, "/media/catalog/", "catalog")
        je_mag1, je_mag2 = await asyncio.gather(task_mag_1, task_mag_2)
        if je_mag1 or je_mag2:
            return False, "Magento Enterprise"

    # 3. KROK: Scrape text and scan GTM
    kompletni_text = ""
    gtm_ids = set()

    try:
        async with session.get(url, headers=HEADERS, timeout=timeout_config) as r_hp:
            html_hp = await r_hp.text(errors='ignore')
            html_hp = html_hp.lower()
            kompletni_text += html_hp
        
        found_gtm = re.findall(r'gtm-[a-zA-Z0-9]+', html_hp)
        if found_gtm:
            gtm_ids.update([g.upper() for g in found_gtm])
        
        soup_hp = BeautifulSoup(html_hp, 'html.parser')
        url_pokladny = najdi_podstranku(soup_hp, url, CARTS)
        
        if url_pokladny:
            async with session.get(url_pokladny, headers=HEADERS, timeout=timeout_config) as r_p:
                html_p = await r_p.text(errors='ignore')
                html_p = html_p.lower()
                kompletni_text += " " + html_p
                
            found_gtm_p = re.findall(r'gtm-[a-zA-Z0-9]+', html_p)
            if found_gtm_p:
                gtm_ids.update([g.upper() for g in found_gtm_p])
                
        # Fetch GTM scripts asynchronously
        async def fetch_gtm(gtm_id):
            try:
                gtm_url = f"https://www.googletagmanager.com/gtm.js?id={gtm_id}"
                async with session.get(gtm_url, headers=HEADERS, timeout=timeout_config) as r_gtm:
                    if r_gtm.status == 200:
                        return await r_gtm.text()
            except:
                pass
            return ""

        gtm_results = await asyncio.gather(*(fetch_gtm(g_id) for g_id in gtm_ids))
        for gtm_text in gtm_results:
            if gtm_text:
                kompletni_text += " " + gtm_text.lower()
                if "foxentry" in gtm_text.lower():
                    kompletni_text += " foxentry "

    except Exception:
        pass

    eshop_znaky = ["koupit", "přidat do košíku", "do košíku", "skladem", "cena s dph", "včetně dph", "cart", "basket"]
    if platforma == "Vlastní / Jiná platforma":
        if not any(znak in kompletni_text for znak in eshop_znaky):
            return False, "Není e-shop (Absence nákupních frází)"

    ma_jiny_validator = any(v in kompletni_text for v in cizi_validatory)
    ma_smartform = "smartform" in kompletni_text

    if ma_smartform:
        platforma = "Využívá SmartForm"

    if ma_jiny_validator and not ma_smartform:
        return False, "Cizí validátor (Detekován v kódu/GTM)"
    
    if platforma == "Vlastní / Jiná platforma" and not url_pokladny:
            return False, "Není e-shop (Nenalezen košík)"

    return True, platforma


async def worker(queue, session, cursor_dst, conn_dst):
    """Worker process that pulls jobs from queue and writes directly to SQLite"""
    while True:
        # UPRAVENO: Z fronty vytahujeme kompletní balík dat včetně telefonu a emailu
        eshop_id, domena, telefon, email = await queue.get()
        url = domena if domena.startswith("http") else f"https://www.{domena}"
        
        try:
            prosel, vysledek = await is_valid_eshop_target_async(session, url)
            if prosel:
                print(f"[+] PROŠEL: {domena} ({vysledek})")
                # UPRAVENO: Zapisujeme doménu, telefon i e-mail (použity 3 otazníky a správný tuple)
                cursor_dst.execute(
                    'INSERT OR IGNORE INTO schvalene (domena, telefon, email) VALUES (?, ?, ?)', 
                    (domena, telefon, email)
                )
                conn_dst.commit()
            else:
                print(f"[-] BLOKOVÁN: {domena} -> Důvod: {vysledek}")
        except Exception as e:
            print(f"[!] Chyba {domena}: {e}")
        finally:
            queue.task_done()


async def main():
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    source_db_path = os.path.join(BASE_DIR, 'eshopy.db')
    target_db_path = os.path.join(BASE_DIR, 'schvalene_eshopy.db')

    conn_src = sqlite3.connect(source_db_path)
    cursor_src = conn_src.cursor()
    
    conn_dst = sqlite3.connect(target_db_path)
    cursor_dst = conn_dst.cursor()
    
    cursor_dst.execute('''
        CREATE TABLE IF NOT EXISTS schvalene (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            domena TEXT UNIQUE,
            telefon TEXT,
            email TEXT UNIQUE
        )
    ''')
    conn_dst.commit()
    
    try:
        # UPRAVENO: Vytahujeme z původní databáze i sloupce telefon a email
        # (Pokud se v původní DB jmenují jinak než 'telefon' a 'email', uprav názvy v SELECTu)
        cursor_src.execute("SELECT id, domena, telefon, email FROM seznam_eshopu")
        eshopy = cursor_src.fetchall()
    except sqlite3.OperationalError:
        print("[!] Zdrojová tabulka neexistuje nebo neobsahuje sloupce telefon/email.")
        return

    # Set up async execution queue
    queue = asyncio.Queue()
    for item in eshopy:
        await queue.put(item)  # Do fronty padá celý tuple (id, domena, telefon, email)

    async with aiohttp.ClientSession() as session:
        # Fire up parallel worker tasks
        tasks = []
        for _ in range(CONCURRENCY_LIMIT):
            task = asyncio.create_task(worker(queue, session, cursor_dst, conn_dst))
            tasks.append(task)

        await queue.join()

        # Cancel workers when queue is empty
        for task in tasks:
            task.cancel()

    conn_src.close()
    conn_dst.close()
    print("Filtrace dokončena.")

if __name__ == "__main__":
    asyncio.run(main())