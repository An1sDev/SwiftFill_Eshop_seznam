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
    for link in soup.find_all('a', href=True):
        href = link.get('href', '').lower()
        text = link.get_text().lower()
        if any(slovo in href or slovo in text for slovo in klicova_slova):
            if any(x in href for x in ["javascript:", "mailto:", "tel:", "#"]):
                continue
            return urljoin(base_url, link['href'])
    return None


async def is_valid_eshop_target_async(session, url):
    parsed_url = urlparse(url)
    domain = parsed_url.netloc

    je_cloud, duvod_cloudu = await check_dns_cloud_async(domain)
    if je_cloud:
        return False, f"Cloud ({duvod_cloudu})"

    task_woo = probe_path_async(session, url, "/wp-content/plugins/woocommerce/", "woocommerce")
    task_presta_1 = probe_path_async(session, url, "/modules/blockcart/", "blockcart")
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


# Přidán zámek (db_lock) pro bezpečný zápis do databáze z více vláken
async def worker(queue, session, conn_dst, db_lock):
    while True:
        eshop_id, domena, telefon, email = await queue.get()
        url = domena if domena.startswith("http") else f"https://www.{domena}"

        try:
            prosel, vysledek = await is_valid_eshop_target_async(session, url)

            # Bezpečně zamkneme databázi, než do ní začneme psát
            async with db_lock:
                cursor_dst = conn_dst.cursor()
                if prosel:
                    print(f"[+] PROŠEL: {domena} ({vysledek})")
                    cursor_dst.execute(
                        'INSERT OR IGNORE INTO schvalene (domena, telefon, email) VALUES (?, ?, ?)',
                        (domena, telefon, email)
                    )
                else:
                    print(f"[-] BLOKOVÁN: {domena} -> Důvod: {vysledek}")

                # Uložíme doménu do seznamu zpracovaných, abychom ji příště přeskočili
                cursor_dst.execute('INSERT OR IGNORE INTO zpracovano (domena) VALUES (?)', (domena,))
                conn_dst.commit()

        except Exception as e:
            print(f"[!] Chyba {domena}: {e}")
            # I v případě síťové chyby můžeme doménu poznačit jako zpracovanou (volitelné)
            async with db_lock:
                cursor_dst = conn_dst.cursor()
                cursor_dst.execute('INSERT OR IGNORE INTO zpracovano (domena) VALUES (?)', (domena,))
                conn_dst.commit()
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

    # Nová tabulka pro trackování toho, co už skript prošel
    cursor_dst.execute('''
        CREATE TABLE IF NOT EXISTS zpracovano (
            domena TEXT PRIMARY KEY
        )
    ''')
    conn_dst.commit()

    # Překlopíme již dříve schválené e-shopy do tabulky zpracováno
    cursor_dst.execute("INSERT OR IGNORE INTO zpracovano (domena) SELECT domena FROM schvalene")
    conn_dst.commit()

    # Zjištění, co už máme hotové
    cursor_dst.execute("SELECT domena FROM zpracovano")
    hotove_domeny = set(row[0] for row in cursor_dst.fetchall())

    try:
        cursor_src.execute("SELECT id, domena, telefon, email FROM seznam_eshopu")
        eshopy_vsechny = cursor_src.fetchall()
    except sqlite3.OperationalError:
        print("[!] Zdrojová tabulka neexistuje nebo neobsahuje sloupce telefon/email.")
        return

    # Vyfiltrujeme domény, které už jsou tabulce "zpracovano"
    eshopy_k_zpracovani = [item for item in eshopy_vsechny if item[1] not in hotove_domeny]

    print(f"Celkem domén v databázi: {len(eshopy_vsechny)}")
    print(f"Již zpracováno: {len(hotove_domeny)}")
    print(f"Zbývá ke zpracování: {len(eshopy_k_zpracovani)}")
    print("-" * 50)

    queue = asyncio.Queue()
    for item in eshopy_k_zpracovani:
        await queue.put(item)

    db_lock = asyncio.Lock()

    async with aiohttp.ClientSession() as session:
        tasks = []
        for _ in range(CONCURRENCY_LIMIT):
            task = asyncio.create_task(worker(queue, session, conn_dst, db_lock))
            tasks.append(task)

        await queue.join()

        for task in tasks:
            task.cancel()

    conn_src.close()
    conn_dst.close()
    print("Filtrace dokončena.")


if __name__ == "__main__":
    # Na Windows doporučeno pro správné fungování asyncio smyčky
    if os.name == 'nt':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())