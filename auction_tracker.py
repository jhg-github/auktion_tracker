import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from html import escape
from urllib.parse import urljoin, urlparse

import requests
import yaml
from bs4 import BeautifulSoup

session = requests.Session()
adapter = requests.adapters.HTTPAdapter(pool_connections=30, pool_maxsize=30)
session.mount("https://", adapter)
session.mount("http://", adapter)
session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})


def normalize(text):
    return " ".join(text.lower().split())


def extract_price_from_soup(soup):
    html_text = soup.get_text(" ", strip=False)
    labels = "\n".join([html_text, str(soup)])

    highest_bid = ""
    incl_vat_fee = ""

    danish_num_pattern = r"([0-9]{1,3}(?:[.\s][0-9]{3})*(?:,[0-9]{1,2})?)"

    highest_bid_match = re.search(
        r"Højeste\s+bud\s*\(\s*DKK\s*\)\s*\n?\s*" + danish_num_pattern,
        labels,
        re.IGNORECASE,
    )
    if highest_bid_match:
        highest_bid = highest_bid_match.group(1).strip()

    incl_vat_fee_match = re.search(
        r"inkl\.?\s*moms\s+og\s+salær\s*:?\s*\n?\s*" + danish_num_pattern,
        labels,
        re.IGNORECASE,
    )
    if incl_vat_fee_match:
        incl_vat_fee = incl_vat_fee_match.group(1).strip()

    if not highest_bid:
        generic_highest = re.search(
            r"(?:highest bid|højeste bud|bud)\s*(?:\(DKK\)|\(dkk\))?\s*[:=]?\s*" + danish_num_pattern,
            labels,
            re.IGNORECASE,
        )
        if generic_highest:
            highest_bid = generic_highest.group(1).strip()

    if not incl_vat_fee:
        generic_fee = re.search(
            r"(?:inkl\.?\s*moms og salær|incl\.?\s*vats? and fee|moms og salær).*?" + danish_num_pattern,
            labels,
            re.IGNORECASE,
        )
        if generic_fee:
            incl_vat_fee = generic_fee.group(1).strip()

    if not highest_bid:
        price_label_re = re.compile(
            r"(?i)(?:bud|bid|price|fyret bud|start price|current price|hammerslag|minimum bid)\s*(?:[:=]|\s)?\s*" + danish_num_pattern + r"\s*(?:kr|kr\.|kroner|dkk)?"
        )
        price_value_re = re.compile(
            r"(?i)" + danish_num_pattern + r"\s*(?:kr|kr\.|kroner|dkk)"
        )
        
        price_match = price_label_re.search(labels) or price_value_re.search(labels)
        if price_match:
            highest_bid = price_match.group(1).strip()

    return {
        "highest_bid": highest_bid,
        "incl_vat_fee": incl_vat_fee,
    }


def collect_category_lots(start_url):
    visited_pages = set()
    pages_to_visit = [start_url]
    parsed_start = urlparse(start_url)
    base_path = parsed_start.path
    
    lots = {}
    
    while pages_to_visit:
        curr_url = pages_to_visit.pop(0)
        if curr_url in visited_pages:
            continue
        visited_pages.add(curr_url)
        
        r = session.get(curr_url, timeout=10)
        if r.status_code != 200:
            continue
        soup = BeautifulSoup(r.text, "html.parser")
        
        for a in soup.find_all("a", href=True):
            href = a["href"]
            full_url = urljoin(curr_url, href)
            pu = urlparse(full_url)
            if pu.netloc == parsed_start.netloc and pu.path == base_path and "page=" in pu.query:
                if full_url not in visited_pages and full_url not in pages_to_visit:
                    pages_to_visit.append(full_url)
                    
        items = soup.find_all("li", class_=lambda c: c and "lot-item" in c)
        for item in items:
            a_link = item.find("a", href=lambda h: h and "/lots/" in h)
            if not a_link:
                continue
            lot_url = urljoin(curr_url, a_link["href"])
            
            h3 = item.find("h3")
            title = h3.find("span").get_text(strip=True) if h3 and h3.find("span") else (h3.get_text(strip=True) if h3 else "")
            
            img = item.find("img")
            img_url = ""
            if img:
                img_url = img.get("src") or img.get("data-src") or ""
                if img_url and not img_url.startswith("http"):
                    img_url = urljoin(curr_url, img_url)
                    
            bid_elem = item.find(class_=lambda c: c and "bid-amount" in c)
            highest_bid = bid_elem.get_text(strip=True) if bid_elem else ""
            
            total_elem = item.find(class_=lambda c: c and "bid-amount-total" in c)
            incl_vat = total_elem.get_text(strip=True) if total_elem else ""
            
            lots[lot_url] = {
                "url": lot_url,
                "title": title,
                "image_url": img_url,
                "highest_bid": highest_bid,
                "incl_vat_fee": incl_vat,
                "description": ""
            }
            
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "/lots/" in href:
                lot_url = urljoin(curr_url, href)
                if lot_url not in lots:
                    lots[lot_url] = {
                        "url": lot_url,
                        "title": a.get_text(strip=True),
                        "image_url": "",
                        "highest_bid": "",
                        "incl_vat_fee": "",
                        "description": ""
                    }
                    
    return list(lots.values())


def fetch_lot_details(lot):
    r = session.get(lot["url"], timeout=5)
    if r.status_code == 200:
        soup = BeautifulSoup(r.text, "html.parser")
        
        if soup.title:
            t = soup.title.get_text(" ", strip=True)
            if t:
                lot["title"] = t
                
        meta_desc = ""
        meta_tag = soup.find("meta", attrs={"name": "description"})
        if meta_tag and meta_tag.get("content"):
            meta_desc = meta_tag["content"]
            
        og_desc = ""
        og_tag = soup.find("meta", attrs={"property": "og:description"})
        if og_tag and og_tag.get("content"):
            og_desc = og_tag["content"]
            
        lot["description"] = " ".join([meta_desc, og_desc])
        
        if not lot["image_url"]:
            img_tag = soup.find("meta", attrs={"property": "og:image"})
            if img_tag and img_tag.get("content"):
                lot["image_url"] = img_tag["content"]
                
        if not lot["highest_bid"] or not lot["incl_vat_fee"]:
            extracted = extract_price_from_soup(soup)
            if not lot["highest_bid"]:
                lot["highest_bid"] = extracted.get("highest_bid", "")
            if not lot["incl_vat_fee"]:
                lot["incl_vat_fee"] = extracted.get("incl_vat_fee", "")
    return lot


def write_html_report(product_to_find, matches, output_path):
    rows = []
    for item in matches:
        image_html = ""
        if item.get("image_url"):
            image_html = f'<img src="{escape(item["image_url"])}" alt="{escape(item["title"])}">'
        price_data = item.get("price") or {}
        highest_bid = price_data.get("highest_bid") or "-"
        incl_vat_fee = price_data.get("incl_vat_fee") or "-"
        rows.append(
            f'<tr><td class="image">{image_html}</td><td class="title">{escape(item["title"])}</td><td class="price">{escape(highest_bid)} kr</td><td class="price">{escape(incl_vat_fee)} kr</td><td class="url"><a href="{escape(item["url"])}" target="_blank">Open auction</a></td></tr>'
        )
    content = "\n".join(rows) if rows else '<tr><td colspan="5">No matches found.</td></tr>'
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Search results for {escape(product_to_find)}</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 0; background: #f5f7fb; color: #222; }}
    h1 {{ padding: 24px; margin: 0; background: #ffffff; border-bottom: 1px solid #e5e7eb; }}
    .results {{ padding: 24px; }}
    table {{ width: 100%; border-collapse: collapse; background: white; }}
    th {{ text-align: left; padding: 12px; background: #e5e7eb; }}
    td {{ padding: 12px; border-bottom: 1px solid #e5e7eb; vertical-align: top; }}
    .image {{ width: 160px; }}
    .image img {{ max-width: 120px; max-height: 100px; object-fit: cover; }}
    .title {{ font-weight: bold; }}
    .price {{ font-weight: bold; color: #334155; }}
    .url a {{ color: #0f766e; text-decoration: none; font-weight: bold; }}
  </style>
</head>
<body>
  <h1>Search results for {escape(product_to_find)}</h1>
  <div class="results">
    <table>
      <thead>
        <tr>
          <th>Image</th>
          <th>Product</th>
          <th>Højeste bud (DKK)</th>
          <th>Inkl. moms og salær</th>
          <th>Link</th>
        </tr>
      </thead>
      <tbody>
        {content}
      </tbody>
    </table>
  </div>
</body>
</html>
"""
    with open(output_path, "w", encoding="utf-8") as handle:
        handle.write(html)


def main():
    config_path = sys.argv[1] if len(sys.argv) > 1 else "products.yaml"
    output_path = sys.argv[2] if len(sys.argv) > 2 else "results.html"
    with open(config_path, encoding="utf-8") as handle:
        config = yaml.safe_load(handle)
    product_to_find = config.get("product_to_find", "")
    keywords = [str(item).lower() for item in config.get("keywords", [])]
    categories = config.get("auction_categories", [])
    if not categories:
        print("No auction categories were found in the config.")
        return

    print(f"Starting search for: {product_to_find}")
    print(f"Scanning {len(categories)} auction category(s)...")

    all_lots = []
    for category_index, category in enumerate(categories, start=1):
        print(f"Category {category_index}/{len(categories)}: gathering lot links from {category}", flush=True)
        lots = collect_category_lots(category)
        print(f"Found {len(lots)} lot link(s) in {category}", flush=True)
        all_lots.extend(lots)

    unique_dict = {lot["url"]: lot for lot in all_lots}
    unique_lots = list(unique_dict.values())
    print(f"Unique lot URLs after dedupe: {len(unique_lots)}", flush=True)

    print("Fetching lot details in parallel...", flush=True)
    enriched_lots = []
    with ThreadPoolExecutor(max_workers=25) as executor:
        futures = [executor.submit(fetch_lot_details, lot) for lot in unique_lots]
        for f in as_completed(futures):
            enriched_lots.append(f.result())

    matches = []
    for lot_index, lot_data in enumerate(enriched_lots, start=1):
        text = normalize(" ".join([lot_data["title"], lot_data["description"], lot_data["url"]]))
        
        matched = False
        if product_to_find.lower() in text:
            matched = True
        elif any(keyword in text for keyword in keywords):
            matched = True

        if matched:
            match_obj = {
                "title": lot_data["title"],
                "url": lot_data["url"],
                "image_url": lot_data["image_url"],
                "price": {
                    "highest_bid": lot_data["highest_bid"],
                    "incl_vat_fee": lot_data["incl_vat_fee"],
                }
            }
            matches.append(match_obj)
            highest_bid = lot_data["highest_bid"] or "-"
            incl_vat_fee = lot_data["incl_vat_fee"] or "-"
            print(f"  Match found: {lot_data['title']} | Højeste bud: {highest_bid} kr | Inkl. moms og salær: {incl_vat_fee} kr", flush=True)

    write_html_report(product_to_find, matches, output_path)
    print(json.dumps({"product_to_find": product_to_find, "matches": matches}, indent=2, ensure_ascii=False))
    print(f"Wrote HTML report to {output_path}")


if __name__ == "__main__":
    main()
