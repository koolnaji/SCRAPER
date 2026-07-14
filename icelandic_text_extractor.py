import os
import asyncio
from urllib.parse import urlparse
from playwright.async_api import async_playwright
import stanza

output_folder = r"C:\Users\dawn-\Documents\icelandic_corpus"
os.makedirs(output_folder, exist_ok=True)

source_pages = [
    "https://www.ruv.is/frettir/innlent",
    "https://www.ruv.is/frettir/erlent",
    "https://www.ruv.is/frettir/ithrottir",
]

junk_phrases = [
    "vafrakökur",
    "Efstaleiti",
    "cookies",
    "RÚV er óháð hagsmunum stjórnmála",
    "Fréttaflutningur og dagskrárgerð okkar byggist á trúverðugleika",
    "Starfsfólk RÚV starfar samkvæmt",
    "Vinnureglur um fréttir og dagskrárefni",
    "ef þú ert með myndefni eða upplýsingar",
]

print("Loading Icelandic lemmatizer...")
nlp = stanza.Pipeline('is', processors='tokenize,lemma')
print("Lemmatizer ready!\n")

def lemmatize(text):
    doc = nlp(text)
    lemmas = []
    for sentence in doc.sentences:
        for word in sentence.words:
            if word.lemma and word.lemma.strip():
                lemmas.append(word.lemma)
    return " ".join(lemmas)

def normalize_url(url):
    parsed = urlparse(url)
    return parsed.scheme + "://" + parsed.netloc + parsed.path.rstrip("/")

def url_to_filename(url):
    parsed = urlparse(url)
    parts = parsed.path.rstrip("/").split("/")
    section = parts[-2] if len(parts) >= 2 else "unknown"
    slug = parts[-1]
    return f"{section}_{slug}.txt"

def get_already_scraped():
    existing = set()
    for fname in os.listdir(output_folder):
        if fname.endswith(".txt"):
            existing.add(fname)
    return existing

async def scrape_url(page, url, index, total):
    try:
        print(f"[{index}/{total}] {url}")
        await page.goto(url, wait_until="networkidle")

        try:
            await page.wait_for_selector('p', state='visible', timeout=10000)
        except:
            print(f"   ⚠️ No text found (page never loaded paragraphs)")
            return False

        paragraphs = await page.query_selector_all('p')
        texts = []
        junk_found = False
        for p in paragraphs:
            text = await p.inner_text()
            text = text.strip()
            if len(text) < 40:
                continue

            junk_match = None
            for phrase in junk_phrases:
                if phrase.lower() in text.lower():
                    junk_match = phrase
                    break

            if junk_match and len(text) < 200:
                junk_found = True
                continue

            texts.append(text)

        if junk_found:
            print(f"   🗑️ Filtered!")

        raw_text = "\n\n".join(texts)

        if raw_text.strip():
            print(f"   Lemmatizing...")
            lemmatized_text = lemmatize(raw_text)
            filename = url_to_filename(url)
            filepath = os.path.join(output_folder, filename)
            with open(filepath, "w", encoding="utf-8") as file:
                file.write(lemmatized_text)
            print(f"   ✅ Saved: {filename}")
            return True
        else:
            print(f"   ⚠️ No text found")
            return False

    except Exception as e:
        print(f"   💥 Error: {e}")
        return False

async def scrape():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        page.set_default_timeout(30000)

        all_urls = []

        for source in source_pages:
            print(f"Collecting links from: {source}")
            await page.goto(source, wait_until="networkidle")

            try:
                await page.wait_for_selector('a[href*="/frettir/"]', state='visible', timeout=10000)
            except:
                print(f"   ⚠️ Could not load links from {source}, skipping")
                continue

            links = await page.eval_on_selector_all(
                'a[href*="/frettir/"]',
                'elements => elements.map(el => el.href)'
            )

            for link in links:
                if "/frettir/" in link and link.count("-") > 3:
                    all_urls.append(normalize_url(link))

        all_urls = list(dict.fromkeys(all_urls))
        print(f"\nFound {len(all_urls)} articles total")

        already_scraped = get_already_scraped()
        new_urls = [u for u in all_urls if url_to_filename(u) not in already_scraped]

        print(f"{len(new_urls)} new articles to scrape ({len(already_scraped)} already in corpus)")
        print(f"Saving corpus to: {output_folder}\n")

        if not new_urls:
            print("Nothing new today — come back tomorrow!")
            await browser.close()
            return

        # First pass
        failed_urls = []
        saved_count = 0
        for index, url in enumerate(new_urls):
            success = await scrape_url(page, url, index + 1, len(new_urls))
            if success:
                saved_count += 1
            else:
                failed_urls.append(url)

        # Retry pass
        if failed_urls:
            print(f"\n🔁 Retrying {len(failed_urls)} failed URLs...\n")
            still_failed = []
            for index, url in enumerate(failed_urls):
                success = await scrape_url(page, url, index + 1, len(failed_urls))
                if success:
                    saved_count += 1
                else:
                    still_failed.append(url)

            if still_failed:
                print(f"\n❌ {len(still_failed)} URLs could not be scraped after retry:")
                for u in still_failed:
                    print(f"   {u}")

        await browser.close()
        print(f"\nDone! Saved {saved_count} new articles to {output_folder}/")

asyncio.run(scrape())