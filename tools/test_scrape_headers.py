"""
========================================================================
test_scrape_headers.py — Diagnostic tool for script_scraping_emas.py
========================================================================

Tujuan
------
Mendiagnosis kenapa harga-emas.org kadang return 404 / 307. Apakah karena
header yang dikirim scraper, atau karena URL yang memang tidak ada?

Script ini meng-import SESSION & BULAN_ID langsung dari script_scraping_emas.py,
jadi header yang dites adalah header produksi yang sebenarnya (bukan copy yang
bisa drift).

Cara menjalankan (dari project root, via uv)
---------------------------------------------
Semua perintah di bawah ini dijalankan dari directory project root:

    cd /home/primarizki/project/LSTM-Multivariate-Gold-Prediction

1. Lihat semua header yang dipakai scraper + retry config:

       uv run python tools/test_scrape_headers.py --audit-headers

2. Probe 1 tanggal (deep-dive): status, redirect, final URL, table count,
   deteksi Antam/Pegadaian/Pluang, body preview, plus flag khusus untuk
   kasus 307 → index page:

       uv run python tools/test_scrape_headers.py --date 2024-01-15
       uv run python tools/test_scrape_headers.py --date 2024-02-30
       uv run python tools/test_scrape_headers.py --date 2026-06-23   # today (307 case)

3. 3-way A/B/C test: probe URL yang sama dengan 3 varian header:
   A) Production SESSION (semua header lengkap, Chrome/124)
   B) Bare requests.get()  (UA minimal, tanpa Sec-Fetch-*)
   C) Chrome/125 UA only   (UA lebih baru, header baseline browser)

   Berguna untuk membuktikan apakah header / UA penyebab 404:

       uv run python tools/test_scrape_headers.py --ab 2024-02-30
       uv run python tools/test_scrape_headers.py --ab 2024-01-15

   Verdict logic:
     - Semua 3 sama (200/404)     → headers BUKAN penyebab
     - A beda dari B              → production headers penyebab
     - A==C beda dari B           → stale UA (Chrome/124) faktor

4. Batch probe (compact TSV output): cocok untuk list tanggal yang 404
   dari log scraper. Output 1 baris per tanggal, dengan A/B/C status:

       uv run python tools/test_scrape_headers.py --dates 2024-02-30,2024-06-01,2026-06-23
       uv run python tools/test_scrape_headers.py --range 2024-06-01 2024-06-05

5. Lihat help lengkap:

       uv run python tools/test_scrape_headers.py --help

6. Test 4 alternate URL patterns (numeric month, tanpa leading zero, dll):
   Berguna untuk cek apakah site pakai URL pattern lain untuk tanggal yang
   kita kira 404:

       uv run python tools/test_scrape_headers.py --alt-urls 2026-02-10

7. Test CDN cache hypothesis (kirim Cache-Control: no-cache):
   Jika baseline 404 tapi dengan cache-bust jadi 200 → CDN edge serving
   stale 404:

       uv run python tools/test_scrape_headers.py --no-cache 2026-02-10

8. Test Referer requirement (kirim Referer: https://harga-emas.org/...):
   Jika dengan Referer jadi 200 → site butuh Referer dari index page:

       uv run python tools/test_scrape_headers.py --with-referer 2026-02-10

9. Test session/cookie (visit /history-harga index dulu, baru deep link):
   Jika setelah visit index jadi 200 → site butuh session/cookie:

       uv run python tools/test_scrape_headers.py --after-index 2026-02-10

10. Scope-check: probe Feb 2026 (09, 10, 11, 12, 13, 14, 15):
    Untuk cek apakah cuma 02-10 yang missing, atau Feb 2026 umumnya rusak:

       uv run python tools/test_scrape_headers.py --probe-feb-2026

Verdict legend
--------------
Setiap test (mode 6-10) akan print salah satu dari:

    [PASS]         = test konfirmasi expected behavior; tidak ada masalah
    [FAIL]         = test揭示 masalah (mis. Referer ternyata required)
    [INCONCLUSIVE] = test tidak menghasilkan kesimpulan yang bisa ditindak

Verdict dijelaskan di output per-test dengan bahasa Indonesia, jadi tidak
perlu dihafal. Yang penting: ada tiga kemungkinan dan masing-masing punya
rekomendasi next step.

Built-in test dates (untuk sanity check cepat)
----------------------------------------------
| Date          | Expected                                      |
|---------------|-----------------------------------------------|
| 2024-01-15    | 200, 2 tables, price found (normal weekday)   |
| 2024-02-30    | 404 di semua varian (invalid date)            |
| 2026-06-23    | 307 → /history-harga index, 3 tables, flagged |
| 2024-06-03    | 200, 2 tables, price found                    |
| 2026-02-10    | 404 di semua varian; tapi data ada manual     |
|               | → gunakan mode 6-10 untuk root cause          |

Catatan
-------
- Script ini DIAGNOSTIC ONLY. Tidak mengubah script_scraping_emas.py.
- Aman dijalankan berulang; tidak ada side effect ke file data.
- Tested di Ubuntu/Linux via uv. Cross-platform (Path-based, no shell).
- Untuk 1 probe: ~3-5 detik. Untuk --range 30 hari: ~2-3 menit
  (ada 1s sleep antar probe untuk anti-rate-limit).
- Mode 6-10 SELALU print satu baris VERDICT di akhir — itulah acuan
  Pass/Fail/Inconclusive.
"""


import os
import sys
import argparse
import time
from pathlib import Path
from datetime import datetime, timedelta

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests
from bs4 import BeautifulSoup

from script_scraping_emas import SESSION, BULAN_ID, scrape_harga_emas


CHROME_125_UA = (
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36'
)

BARE_HEADERS = {
    'User-Agent': 'python-requests/2.34.2',
}

INTERESTING_RESPONSE_HEADERS = ['Server', 'Content-Type', 'CF-Ray', 'CF-Cache-Status', 'Location', 'Content-Encoding']


def build_url(date_str):
    parts = date_str.split('-')
    if len(parts) != 3:
        raise ValueError(f'Invalid date format (expected YYYY-MM-DD): {date_str}')
    year, month, day = int(parts[0]), int(parts[1]), int(parts[2])
    nama_bulan = BULAN_ID[month]
    return f"https://harga-emas.org/history-harga/{year}/{nama_bulan}/{day:02d}", (year, month, day)


def print_audit_headers():
    print('=' * 60)
    print('PRODUCTION SESSION HEADERS (from script_scraping_emas.py)')
    print('=' * 60)
    for k, v in SESSION.headers.items():
        print(f'  {k}: {v}')
    print()
    print('Adapter retry config:')
    adapter = SESSION.get_adapter('https://harga-emas.org/')
    retry = getattr(adapter, 'max_retries', None)
    if retry:
        print(f'  total: {retry.total}')
        print(f'  backoff_factor: {retry.backoff_factor}')
        print(f'  status_forcelist: {retry.status_forcelist}')


def inspect_response(resp, date_str):
    print(f'  Date             : {date_str}')
    print(f'  Final status     : {resp.status_code}')
    print(f'  Final URL        : {resp.url}')
    if resp.history:
        chain = ' -> '.join([f"{h.status_code} ({h.url})" for h in resp.history])
        print(f'  Redirect chain   : {chain}')
    else:
        print(f'  Redirect chain   : (none)')
    print(f'  Body length      : {len(resp.text):,} bytes')
    body = resp.text
    table_count = body.count('<table')
    print(f'  <table> count    : {table_count}')

    soup = BeautifulSoup(body, 'html.parser')
    tables = soup.find_all('table')
    price_found = False
    for t in tables:
        ths = [th.get_text(strip=True) for th in t.find_all('th')]
        if any('Antam' in h or 'Pegadaian' in h or 'Pluang' in h for h in ths):
            price_found = True
            break
    print(f'  Price table      : {"FOUND" if price_found else "NOT FOUND"}')

    print(f'  Response headers:')
    for h in INTERESTING_RESPONSE_HEADERS:
        v = resp.headers.get(h)
        if v:
            print(f'    {h}: {v}')

    is_307_to_index = (
        resp.history
        and resp.history[0].status_code == 307
        and '/history-harga' in resp.url
        and resp.url.rstrip('/').endswith('history-harga')
    )
    if is_307_to_index:
        print(f'  ⚠️  307 REDIRECT → INDEX PAGE (today/future date; final URL has 3 tables, not 2)')

    print(f'  Body preview (first 300 chars):')
    print('    ' + body[:300].replace('\n', ' '))
    print()
    return price_found, is_307_to_index


def probe_with_headers(url, headers, label, timeout=10):
    print(f'  [{label}] headers: {list(headers.keys())}')
    try:
        r = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
        return {
            'status': r.status_code,
            'final_url': r.url,
            'redirected': bool(r.history),
            'body_len': len(r.text),
            'table_count': r.text.count('<table'),
        }
    except requests.exceptions.Timeout:
        return {'status': 'TIMEOUT', 'error': 'Timeout'}
    except requests.exceptions.ConnectionError as e:
        return {'status': 'CONN_ERR', 'error': str(e)[:80]}
    except Exception as e:
        return {'status': 'ERR', 'error': f'{type(e).__name__}: {str(e)[:80]}'}


def ab_probe(url):
    print(f'  A) Production SESSION (full headers)')
    a = probe_with_headers(url, dict(SESSION.headers), 'A')
    print(f'     {a}')
    print()

    print(f'  B) BARE requests.get() (no UA, no Sec-Fetch)')
    b = probe_with_headers(url, BARE_HEADERS, 'B')
    print(f'     {b}')
    print()

    print(f'  C) Chrome/125 UA only (fresh UA, baseline browser headers)')
    c_headers = {
        'User-Agent': CHROME_125_UA,
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7',
    }
    c = probe_with_headers(url, c_headers, 'C')
    print(f'     {c}')
    print()

    print('  VERDICT:')
    a_status = a.get('status')
    b_status = b.get('status')
    c_status = c.get('status')
    if a_status == b_status == c_status:
        print(f'    All three returned {a_status} → headers are NOT the cause of failure.')
    else:
        if a_status != b_status:
            print(f'    A={a_status} vs B={b_status} → PRODUCTION HEADERS ARE THE CAUSE.')
        if a_status == c_status and b_status != c_status:
            print(f'    A={a_status} == C={c_status}, B={b_status} → stale UA may be a factor.')
        elif a_status != c_status:
            print(f'    A={a_status} vs C={c_status} → Chrome/125 UA differs from production.')


def test_single_date(date_str):
    print('=' * 60)
    print(f'SINGLE-DATE PROBE: {date_str}')
    print('=' * 60)
    url, dt = build_url(date_str)
    print(f'  URL: {url}')
    print()

    try:
        resp = SESSION.get(url, timeout=10, allow_redirects=True)
    except requests.exceptions.Timeout:
        print('  ⛔ TIMEOUT (network issue, not headers)')
        return
    except requests.exceptions.ConnectionError as e:
        print(f'  ⛔ CONNECTION ERROR: {e}')
        return
    except Exception as e:
        print(f'  ⛔ ERROR: {type(e).__name__}: {e}')
        return

    price_found, is_307 = inspect_response(resp, date_str)

    if not price_found and not is_307:
        print('  --- Re-running scrape_harga_emas() to localize failure ---')
        try:
            hasil = scrape_harga_emas(dt[0], dt[1], dt[2])
            if hasil:
                print(f'    scrape_harga_emas() returned {len(hasil)} rows (why did we land here?)')
            else:
                print('    scrape_harga_emas() returned None — but no price table found in raw HTML.')
                print('    Either: (a) site structure changed, (b) digit filter too strict, (c) no data for date.')
        except Exception as e:
            print(f'    scrape_harga_emas() raised: {type(e).__name__}: {e}')


def test_ab(date_str):
    print('=' * 60)
    print(f'A/B/C HEADER PROBE: {date_str}')
    print('=' * 60)
    url, _ = build_url(date_str)
    print(f'  URL: {url}')
    print()
    ab_probe(url)


def test_batch(dates):
    print('=' * 80)
    print(f'BATCH PROBE: {len(dates)} dates')
    print('=' * 80)
    print(f'{"date":<12} {"A_status":<10} {"B_status":<10} {"C_status":<10} {"final_url":<50} {"tables":<8} {"price":<6}')
    print('-' * 80)

    for date_str in dates:
        try:
            url, _ = build_url(date_str)
        except ValueError as e:
            print(f'{date_str:<12} INVALID DATE: {e}')
            continue

        a = probe_with_headers(url, dict(SESSION.headers), 'A')
        time.sleep(0.5)
        b = probe_with_headers(url, BARE_HEADERS, 'B')
        time.sleep(0.5)
        c_headers = {
            'User-Agent': CHROME_125_UA,
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'id-ID,id;q=0.9',
        }
        c = probe_with_headers(url, c_headers, 'C')
        time.sleep(1.0)

        final = a.get('final_url', a.get('error', '?'))[:48]
        tables = a.get('table_count', '-')
        price = '?' if not isinstance(tables, int) else (
            'YES' if a.get('status') == 200 and any(
                kw in str(a)
                for kw in ['Antam', 'Pegadaian', 'Pluang']
            ) else 'NO'
        )
        print(f'{date_str:<12} {str(a.get("status")):<10} {str(b.get("status")):<10} {str(c.get("status")):<10} {final:<50} {str(tables):<8} {price:<6}')


def verdict(passed, label, details):
    """Format a PASS/FAIL/INCONCLUSIVE verdict line.
    passed=True  → [PASS]
    passed=False → [FAIL]
    passed=None  → [INCONCLUSIVE]
    """
    if passed is True:
        marker = '[PASS]'
    elif passed is False:
        marker = '[FAIL]'
    else:
        marker = '[INCONCLUSIVE]'
    return f'  {marker} {label}: {details}'


def probe_no_cache(date_str):
    """Test apakah CDN edge cache yang menyebabkan 404."""
    print('=' * 60)
    print(f'CDN CACHE PROBE: {date_str}')
    print('=' * 60)
    url, _ = build_url(date_str)
    print(f'  URL: {url}')

    try:
        r1 = SESSION.get(url, timeout=10, allow_redirects=True)
        s1 = r1.status_code
    except Exception as e:
        s1 = f'ERR: {type(e).__name__}'
    print(f'  Baseline (production SESSION)         : {s1}')

    try:
        r2 = SESSION.get(url, timeout=10, allow_redirects=True, headers={
            **dict(SESSION.headers),
            'Cache-Control': 'no-cache, no-store, must-revalidate',
            'Pragma': 'no-cache',
        })
        s2 = r2.status_code
    except Exception as e:
        s2 = f'ERR: {type(e).__name__}'
    print(f'  With Cache-Control: no-cache         : {s2}')

    if s1 == 200 and s2 == 200:
        print(verdict(True, 'CDN cache', 'kedua 200; cache bukan penyebab (coba mode lain)'))
    elif s1 == 404 and s2 == 200:
        print(verdict(False, 'CDN cache', 'baseline 404, cache-bust 200 → CDN serving stale 404 (lanjut cek mode 8-9)'))
    elif s1 == 404 and s2 == 404:
        print(verdict(None, 'CDN cache', 'kedua 404 → cache bukan penyebab'))
    else:
        print(verdict(None, 'CDN cache', f'unexpected: baseline={s1} → cache-bust={s2}'))


def probe_with_referer(date_str):
    """Test apakah site butuh Referer header."""
    print('=' * 60)
    print(f'REFERER PROBE: {date_str}')
    print('=' * 60)
    url, _ = build_url(date_str)
    print(f'  URL: {url}')

    try:
        r1 = SESSION.get(url, timeout=10, allow_redirects=True)
        s1 = r1.status_code
    except Exception as e:
        s1 = f'ERR: {type(e).__name__}'
    print(f'  Baseline (no Referer)                : {s1}')

    try:
        r2 = SESSION.get(url, timeout=10, allow_redirects=True, headers={
            **dict(SESSION.headers),
            'Referer': 'https://harga-emas.org/history-harga',
        })
        s2 = r2.status_code
    except Exception as e:
        s2 = f'ERR: {type(e).__name__}'
    print(f'  With Referer: https://.../history    : {s2}')

    if s1 == 200 and s2 == 200:
        print(verdict(True, 'Referer', 'kedua 200; Referer tidak required'))
    elif s1 == 404 and s2 == 200:
        print(verdict(False, 'Referer', 'baseline 404, dengan Referer 200 → Referer WAJIB (butuh fix di script_scraping_emas.py)'))
    elif s1 == 404 and s2 == 404:
        print(verdict(None, 'Referer', 'kedua 404 → Referer bukan penyebab'))
    else:
        print(verdict(None, 'Referer', f'unexpected: baseline={s1} → with-referer={s2}'))


def probe_after_index(date_str):
    """Test apakah site butuh session/cookie dari visit index page dulu."""
    print('=' * 60)
    print(f'SESSION/COOKIE PROBE: {date_str}')
    print('=' * 60)
    url, _ = build_url(date_str)
    print(f'  URL: {url}')

    # Pakai session FRESH (bukan SESSION produksi) untuk test cookie requirement
    fresh = requests.Session()
    fresh.headers.update(dict(SESSION.headers))

    try:
        r1 = fresh.get('https://harga-emas.org/history-harga', timeout=10, allow_redirects=True)
        s1 = r1.status_code
        n_cookies = len(fresh.cookies)
    except Exception as e:
        s1 = f'ERR: {type(e).__name__}'
        n_cookies = 0
    print(f'  Step 1: GET /history-harga (index)   : {s1} ({n_cookies} cookies set)')

    try:
        r2 = fresh.get(url, timeout=10, allow_redirects=True)
        s2 = r2.status_code
    except Exception as e:
        s2 = f'ERR: {type(e).__name__}'
    print(f'  Step 2: GET deep link (same session) : {s2}')

    try:
        r3 = SESSION.get(url, timeout=10, allow_redirects=True)
        s3 = r3.status_code
    except Exception as e:
        s3 = f'ERR: {type(e).__name__}'
    print(f'  Baseline (no index visit)            : {s3}')

    if s2 == 200 and s3 == 404:
        print(verdict(False, 'Session/cookie', 'dengan visit index 200, tanpa 404 → session/cookie WAJIB'))
    elif s2 == s3:
        print(verdict(None, 'Session/cookie', f'status sama ({s2}) → session/cookie bukan penyebab'))
    else:
        print(verdict(None, 'Session/cookie', f'after-index={s2}, baseline={s3} → inconclusive'))


def probe_alt_urls(date_str):
    """Test 4 URL pattern alternatif untuk tanggal yang sama."""
    print('=' * 60)
    print(f'ALTERNATE URL PATTERNS PROBE: {date_str}')
    print('=' * 60)
    parts = date_str.split('-')
    year, month_num, day = int(parts[0]), int(parts[1]), int(parts[2])
    nama_bulan = BULAN_ID[month_num]

    patterns = [
        ('Indonesian (prod)',  f'https://harga-emas.org/history-harga/{year}/{nama_bulan}/{int(day):02d}'),
        ('Numeric month',     f'https://harga-emas.org/history-harga/{year}/{month_num:02d}/{int(day):02d}'),
        ('No leading zero',   f'https://harga-emas.org/history-harga/{year}/{nama_bulan}/{int(day)}'),
        ('No /history-harga', f'https://harga-emas.org/{year}/{nama_bulan}/{int(day):02d}'),
    ]

    print(f'  {"Pattern":<25} {"Status":<8} {"Tables":<8} {"Price":<6}')
    print(f'  {"-"*25} {"-"*8} {"-"*8} {"-"*6}')
    found_200 = False
    for label, url in patterns:
        try:
            r = SESSION.get(url, timeout=10, allow_redirects=True)
            status = r.status_code
            tables = r.text.count('<table')
            has_price = _has_price_table(r.text)
            price_str = 'YES' if has_price else 'NO'
            print(f'  {label:<25} {status:<8} {tables:<8} {price_str:<6}')
            if status == 200 and has_price:
                found_200 = True
        except Exception as e:
            print(f'  {label:<25} ERR: {type(e).__name__}')
        time.sleep(0.5)

    if found_200:
        print(verdict(False, 'URL pattern', 'ada alt pattern yang 200 → URL produksi salah, pakai pattern lain'))
    else:
        print(verdict(None, 'URL pattern', 'semua pattern 404 → URL pattern benar, specific page memang missing'))


def _has_price_table(html_text):
    """Strict check: apakah ada <th> yang berisi Antam/Pegadaian/Pluang
    (bukan hanya kata 'Antam' muncul di sidebar/footer)."""
    try:
        soup = BeautifulSoup(html_text, 'html.parser')
        for t in soup.find_all('table'):
            ths = [th.get_text(strip=True) for th in t.find_all('th')]
            if any('Antam' in h or 'Pegadaian' in h or 'Pluang' in h for h in ths):
                return True
    except Exception:
        return False
    return False


def probe_feb_2026():
    """Scope-check: probe Feb 2026 dates 09-15 untuk lihat apakah cuma 02-10 yang missing."""
    print('=' * 60)
    print('FEB 2026 SCOPE PROBE: 7 dates (09, 10, 11, 12, 13, 14, 15)')
    print('=' * 60)
    dates = [f'2026-02-{d:02d}' for d in range(9, 16)]

    print(f'  {"Date":<12} {"Status":<8} {"Tables":<8} {"Price":<6}')
    print(f'  {"-"*12} {"-"*8} {"-"*8} {"-"*6}')

    count_200 = 0
    count_404 = 0
    count_200_no_price = 0
    results = []
    for date_str in dates:
        try:
            url, _ = build_url(date_str)
            r = SESSION.get(url, timeout=10, allow_redirects=True)
            status = r.status_code
            tables = r.text.count('<table')
            has_price = _has_price_table(r.text)
            price_str = 'YES' if has_price else 'NO'
            print(f'  {date_str:<12} {status:<8} {tables:<8} {price_str:<6}')
            results.append((date_str, status, has_price))
            if status == 200:
                count_200 += 1
                if not has_price:
                    count_200_no_price += 1
            elif status == 404:
                count_404 += 1
        except Exception as e:
            print(f'  {date_str:<12} ERR: {type(e).__name__}')
        time.sleep(0.5)

    if count_200 == 7:
        print(verdict(False, 'Feb 2026 scope', 'semua 7 dates 200; 02-10 harusnya 200 juga → site bug atau transient (coba lagi nanti)'))
    elif count_404 == 7:
        print(verdict(False, 'Feb 2026 scope', 'semua 7 dates 404; Feb 2026 secara umum rusak (cek site maintenance)'))
    elif count_404 == 1 and count_200 == 6:
        only_missing = [d for d, s, _ in results if s == 404][0]
        print(verdict(True, 'Feb 2026 scope', f'6/7 dates 200, hanya {only_missing} yang 404 → confirmed data gap pada tanggal spesifik ini'))
    else:
        print(verdict(None, 'Feb 2026 scope', f'mixed: {count_200}/7 OK, {count_404}/7 missing → perlu investigasi lebih lanjut'))


def parse_args():
    p = argparse.ArgumentParser(
        description='Diagnostic tool for script_scraping_emas.py headers & 404 investigation'
    )
    p.add_argument('--audit-headers', action='store_true', help='Print production SESSION headers')
    p.add_argument('--date', help='Probe a single date (YYYY-MM-DD)')
    p.add_argument('--ab', help='3-way A/B/C header probe for a single date')
    p.add_argument('--dates', help='Comma-separated dates for batch A/B/C probe')
    p.add_argument('--range', nargs=2, metavar=('START', 'END'),
                   help='Date range (YYYY-MM-DD YYYY-MM-DD) for batch probe')
    p.add_argument('--alt-urls', metavar='DATE',
                   help='Test 4 alternate URL patterns for a date (YYYY-MM-DD)')
    p.add_argument('--no-cache', metavar='DATE',
                   help='CDN cache probe: add Cache-Control: no-cache (YYYY-MM-DD)')
    p.add_argument('--with-referer', metavar='DATE',
                   help='Referer probe: add Referer header (YYYY-MM-DD)')
    p.add_argument('--after-index', metavar='DATE',
                   help='Session/cookie probe: visit index first, then deep link (YYYY-MM-DD)')
    p.add_argument('--probe-feb-2026', action='store_true',
                   help='Scope-check: probe 2026-02-09..15 to localize the 02-10 issue')
    return p.parse_args()


def main():
    args = parse_args()

    if args.audit_headers:
        print_audit_headers()
        return

    if args.date:
        test_single_date(args.date)
        return

    if args.ab:
        test_ab(args.ab)
        return

    if args.dates:
        dates = [d.strip() for d in args.dates.split(',') if d.strip()]
        test_batch(dates)
        return

    if args.range:
        start = datetime.strptime(args.range[0], '%Y-%m-%d')
        end = datetime.strptime(args.range[1], '%Y-%m-%d')
        if start > end:
            print('ERROR: START must be <= END')
            sys.exit(1)
        dates = []
        cur = start
        while cur <= end:
            dates.append(cur.strftime('%Y-%m-%d'))
            cur += timedelta(days=1)
        test_batch(dates)
        return

    if args.alt_urls:
        probe_alt_urls(args.alt_urls)
        return

    if getattr(args, 'no_cache', None):
        probe_no_cache(getattr(args, 'no_cache'))
        return

    if args.with_referer:
        probe_with_referer(args.with_referer)
        return

    if getattr(args, 'after_index', None):
        probe_after_index(getattr(args, 'after_index'))
        return

    if args.probe_feb_2026:
        probe_feb_2026()
        return

    print('No action specified. Use --help for options.')
    print()
    print('Quick start:')
    print('  uv run python tools/test_scrape_headers.py --audit-headers')
    print('  uv run python tools/test_scrape_headers.py --date 2024-01-15')
    print('  uv run python tools/test_scrape_headers.py --ab 2024-02-30')
    print('  uv run python tools/test_scrape_headers.py --date 2026-06-23')
    print('  uv run python tools/test_scrape_headers.py --range 2024-06-01 2024-06-05')
    print()
    print('Root-cause probes (for 404s where data is actually there):')
    print('  uv run python tools/test_scrape_headers.py --probe-feb-2026')
    print('  uv run python tools/test_scrape_headers.py --alt-urls 2026-02-10')
    print('  uv run python tools/test_scrape_headers.py --no-cache 2026-02-10')
    print('  uv run python tools/test_scrape_headers.py --with-referer 2026-02-10')
    print('  uv run python tools/test_scrape_headers.py --after-index 2026-02-10')


if __name__ == '__main__':
    main()
