import os
import re
import sqlite3
import asyncio
import dns.asyncresolver
import aiohttp
from bs4 import BeautifulSoup
from urllib.parse import urlparse, urljoin

# --- CONFIG ---
DB_NAME = "domains.db"
CONCURRENCY_LIMIT = 20
timeout_config = aiohttp.ClientTimeout(total=6)

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

# --- POMOCNÉ FUNKCE (Zůstávají stejné) ---
async def check_dns_cloud_async(domain):
    resolver = dns.asyncresolver.Resolver()
    resolver.timeout = 2
    resolver.lifetime = 2
    try:
        try:
            answers = await resolver.resolve(domain, 'CNAME')
            for rdata in answers:
                cname = str(rdata.target).lower()
                if any(cloud in cname for cloud in CLOUDS): return True, f"Cloud CNAME ({cname})"
        except Exception: pass
        try:
            answers = await resolver.resolve(domain, 'A')
            for rdata in answers:
                if str(rdata).startswith("185.83."): return True, "Shoptet IP adresa"
        except Exception: pass
    except Exception: pass
    return False, ""

async def probe_path_async(session, base_url, path, unique_substring):
    try:
        url = urljoin(base_url, path)
        async with session.get(url, headers=HEADERS, timeout=timeout_config, allow_redirects=True) as response:
            if response.status == 200:
                text = await response.text()
                if unique_substring.lower() in text.lower(): return True
    except Exception: pass
    return False

def najdi_podstranku(soup, base_url, klicova_slova):
    for link in soup.find_all('a', href=True):
        href = link.get('href', '').lower()
        text = link.get_text().lower()
        if any(slovo in href or slovo in text for slovo in klicova_slova):
            if any(x in href for x in ["javascript:", "mailto:", "tel:", "#"]): continue
            return urljoin(base_url, link['href'])
    return None

async def is_valid_eshop_target_async(session, url):
    parsed_url = urlparse(url)
    domain = parsed_url.netloc
    if not domain: domain = url.replace("https://", "").replace("http://", "").split('/')[0]

    je_cloud, duvod_cloudu = await check_dns_cloud_async(domain)
    if je_cloud: return False, f"Cloud ({duvod_cloudu})"

    task_woo = probe_path_async(session, url, "/wp-content/plugins/woocommerce/", "woocommerce")
    task_presta_1 = probe_path_async(session, url, "/modules/blockcart/", "blockcart")
    task_presta_2 = probe_path_async(session, url, "/themes/core.js", "prestashop")

    je_woocommerce, je_prestashop_1, je_prestashop_2 = await asyncio.gather(task_woo, task_presta_1, task_presta_2)
    je_prestashop = je_prestashop_1 or je_prestashop_2

    if je_woocommerce: platforma = "WooCommerce"
    elif je_prestashop: platforma = "PrestaShop"
    else: platforma = "Vlastní / Jiná platforma"

    if platforma == "Vlastní / Jiná platforma":
        task_mag_1 = probe_path_async(session, url, "/errors/design.xml", "magento")
        task_mag_2 = probe_path_async(session, url, "/media/catalog/", "catalog")
        je_mag1, je_mag2 = await asyncio.gather(task_mag_1, task_mag_2)
        if je_mag1 or je_mag2: return False, "Magento Enterprise"

    kompletni_text = ""
    gtm_ids = set()

    try:
        async with session.get(url, headers=HEADERS, timeout=timeout_config) as r_hp:
            html_hp = await r_hp.text(errors='ignore')
            kompletni_text += html_hp.lower()

        found_gtm = re.findall(r'gtm-[a-zA-Z0-9]+', html_hp.lower())
        if found_gtm: gtm_ids.update([g.upper() for g in found_gtm])

        soup_hp = BeautifulSoup(html_hp, 'html.parser')
        url_pokladny = najdi_podstranku(soup_hp, url, CARTS)

        if url_pokladny:
            async with session.get(url_pokladny, headers=HEADERS, timeout=timeout_config) as r_p:
                html_p = await r_p.text(errors='ignore')
                kompletni_text += " " + html_p.lower()
            found_gtm_p = re.findall(r'gtm-[a-zA-Z0-9]+', html_p.lower())
            if found_gtm_p: gtm_ids.update([g.upper() for g in found_gtm_p])

        async def fetch_gtm(gtm_id):
            try:
                async with session.get(f"https://www.googletagmanager.com/gtm.js?id={gtm_id}", headers=HEADERS, timeout=timeout_config) as r_gtm:
                    if r_gtm.status == 200: return await r_gtm.text()
            except: pass
            return ""

        gtm_results = await asyncio.gather(*(fetch_gtm(g_id) for g_id in gtm_ids))
        for gtm_text in gtm_results:
            if gtm_text:
                kompletni_text += " " + gtm_text.lower()
                if "foxentry" in gtm_text.lower(): kompletni_text += " foxentry "
    except Exception: pass

    eshop_znaky = ["koupit", "přidat do košíku", "do košíku", "skladem", "cena s dph", "včetně dph", "cart", "basket"]
    if platforma == "Vlastní / Jiná platforma":
        if not any(znak in kompletni_text for znak in eshop_znaky):
            return False, "Není e-shop (Absence nákupních frází)"

    ma_jiny_validator = any(v in kompletni_text for v in cizi_validatory)
    ma_smartform = "smartform" in kompletni_text

    if ma_smartform: platforma = "Využívá SmartForm"
    if ma_jiny_validator and not ma_smartform: return False, "Cizí validátor (Detekován v kódu/GTM)"
    if platforma == "Vlastní / Jiná platforma" and not url_pokladny: return False, "Není e-shop (Nenalezen košík)"

    return True, platforma


# --- WORKER PRO SPUŠTĚNÍ ---
async def worker(queue, session, db_path, db_lock):
    while True:
        db_id, url = await queue.get()
        target_url = url if url.startswith("http") else f"https://www.{url}"

        try:
            prosel, platforma_nebo_duvod = await is_valid_eshop_target_async(session, target_url)
            
            # TADY UKLÁDÁME TEXTOVÉ 'true' NEBO 'false' UMÍSTĚNÉ V DATABÁZI
            is_eshop_val = "true" if prosel else "false"
            
            async with db_lock:
                conn = sqlite3.connect(db_path, timeout=30)
                cursor = conn.cursor()
                cursor.execute('''
                    UPDATE domains 
                    SET is_eshop = ?, technologie = ? 
                    WHERE id = ?
                ''', (is_eshop_val, platforma_nebo_duvod, db_id))
                conn.commit()
                conn.close()
                
            status = "✅ E-SHOP" if prosel else f"❌ NE ({platforma_nebo_duvod})"
            print(f"[{status}] {url}")

        except Exception as e:
            print(f"[!] Chyba {url}: {e}")
        finally:
            queue.task_done()


# --- HLAVNÍ LOOP ---
async def main():
    db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), DB_NAME)
    
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL") 
    cursor = conn.cursor()
    
    # Filtrace funguje na základě textového stavu NULL (dosud nezpracováno)
    cursor.execute("SELECT id, url FROM domains WHERE is_eshop IS NULL")
    eshopy_k_zpracovani = cursor.fetchall()
    conn.close()

    print(f"Celkem zbývá ke zpracování: {len(eshopy_k_zpracovani)} domén.")
    print("-" * 50)

    if not eshopy_k_zpracovani:
        print("Všechny domény jsou již označeny.")
        return

    queue = asyncio.Queue()
    for item in eshopy_k_zpracovani:
        await queue.put(item)

    db_lock = asyncio.Lock()

    async with aiohttp.ClientSession() as session:
        tasks = []
        for _ in range(CONCURRENCY_LIMIT):
            task = asyncio.create_task(worker(queue, session, db_path, db_lock))
            tasks.append(task)

        await queue.join()

        for task in tasks:
            task.cancel()

    print("Hotovo.")

if __name__ == "__main__":
    if os.name == 'nt':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())