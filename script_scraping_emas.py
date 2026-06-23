import os
import time
import requests
import polars as pl
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BULAN_ID = {
    1: 'Januari', 2: 'Februari', 3: 'Maret', 4: 'April',
    5: 'Mei', 6: 'Juni', 7: 'Juli', 8: 'Agustus',
    9: 'September', 10: 'Oktober', 11: 'November', 12: 'Desember'
}

def buat_session():
    session = requests.Session()
    retry = Retry(
        total=5,                        # max 3 kali retry
        backoff_factor=4,               # jeda: 2s, 4s, 8s
        status_forcelist=[429, 500, 502, 503, 504]  # retry kalau status ini
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount('https://', adapter)
    session.headers.update({
        'User-Agent'               : 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Accept'                   : 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language'          : 'id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7',
        'Accept-Encoding'          : 'gzip, deflate, br',
        'Connection'               : 'keep-alive',
        'Upgrade-Insecure-Requests': '1',
        'Sec-Fetch-Dest'           : 'document',
        'Sec-Fetch-Mode'           : 'navigate',
        'Sec-Fetch-Site'           : 'none',
        'Sec-Fetch-User'           : '?1',
    })
    return session

SESSION = buat_session()

def parse_harga(text):
    """'Rp2.796.000' → 2796000, kalau gagal return 0"""
    try:
        return int(text.replace('Rp', '').replace('.', '').replace(',', '').strip())
    except Exception:
        return 0

def scrape_harga_emas(tahun, bulan, tanggal, max_retry=5):
    nama_bulan = BULAN_ID[bulan]
    url = f"https://harga-emas.org/history-harga/{tahun}/{nama_bulan}/{tanggal:02d}"

    for attempt in range(1, max_retry + 1):
        try:
            resp = SESSION.get(url, timeout=10, allow_redirects=True)

            if resp.status_code not in [200, 307]:
                print(f"  [{tahun}/{nama_bulan}/{tanggal:02d}] HTTP {resp.status_code} (attempt {attempt}/{max_retry})")
                if attempt < max_retry:
                    time.sleep(2 ** attempt)  # backoff: 2s, 4s, 8s
                    continue
                return None

            soup = BeautifulSoup(resp.text, 'html.parser')
            tables = soup.find_all('table')

            harga_table = None
            for table in tables:
                headers_row = [th.get_text(strip=True) for th in table.find_all('th')]
                if any('Antam' in h or 'Pegadaian' in h or 'Pluang' in h for h in headers_row):
                    harga_table = table
                    break

            if harga_table is None:
                # Tidak di-retry — kemungkinan memang tidak ada data (libur/weekend)
                return None

            all_headers = [th.get_text(strip=True) for th in harga_table.find_all('th')]

            def cari_index(keyword):
                for i, h in enumerate(all_headers):
                    if keyword.lower() in h.lower():
                        return i
                return None

            idx_antam     = cari_index('Antam')
            idx_pegadaian = cari_index('Pegadaian')
            idx_pluang    = cari_index('Pluang')

            hasil = []
            for row in harga_table.find_all('tr'):
                cols = row.find_all('td')
                if len(cols) < 2:
                    continue
                satuan = cols[0].get_text(strip=True)
                if not any(c.isdigit() for c in satuan):
                    continue

                def get_col(idx):
                    if idx is not None and idx < len(cols):
                        return parse_harga(cols[idx].get_text(strip=True))
                    return 0

                hasil.append({
                    'tanggal'   : f"{tahun}-{bulan:02d}-{tanggal:02d}",
                    'satuan_gr' : satuan,
                    'antam'     : get_col(idx_antam),
                    'pegadaian' : get_col(idx_pegadaian),
                    'pluang'    : get_col(idx_pluang),
                })

            return hasil if hasil else None

        except requests.exceptions.Timeout:
            print(f"  [{tahun}/{nama_bulan}/{tanggal:02d}] Timeout (attempt {attempt}/{max_retry})")
            if attempt < max_retry:
                time.sleep(2 ** attempt)
        except requests.exceptions.ConnectionError:
            print(f"  [{tahun}/{nama_bulan}/{tanggal:02d}] Connection error (attempt {attempt}/{max_retry})")
            if attempt < max_retry:
                time.sleep(2 ** attempt)
        except Exception as e:
            print(f"  [{tahun}/{nama_bulan}/{tanggal:02d}] Error: {e}")
            return None  # error tak terduga, langsung skip

    return None


def load_scrape_index(output_dir):
    """Load set of tanggal yang sudah pernah di-attempt.
    Auto-seed dari per-month parquets kalau sidecar belum ada (first run).
    """
    path = f"{output_dir}/.scrape_index.parquet"
    attempted = set()
    if os.path.exists(path):
        df = pl.read_parquet(path)
        attempted = set(df['tanggal'].dt.strftime('%Y-%m-%d').to_list())
        return attempted

    # First run: seed dari per-month parquets (data dates only)
    # Off-days di masa lalu akan di-retry sekali, lalu di-cache permanent
    if os.path.exists(output_dir):
        for f in os.listdir(output_dir):
            if f.endswith('.parquet') and not f.startswith('.'):
                try:
                    df = pl.read_parquet(f"{output_dir}/{f}")
                    attempted |= set(df['tanggal'].dt.strftime('%Y-%m-%d').to_list())
                except Exception:
                    continue
    return attempted


def save_scrape_index(output_dir, attempted):
    """Persist set of attempted dates ke sidecar file."""
    if not attempted:
        return
    path = f"{output_dir}/.scrape_index.parquet"
    df = pl.DataFrame({'tanggal': sorted(attempted)}).with_columns(
        pl.col('tanggal').str.to_date('%Y-%m-%d')
    )
    df.write_parquet(path)


def audit_missing_dates(output_dir, start_date, end_date):
    """List tanggal di range yang tidak ada di FINAL parquet.
    Memisahkan weekday gaps (perlu investigasi) vs weekend off-days (kemungkinan normal).
    """
    final_path = f"{output_dir}/harga_emas_FINAL.parquet"
    if not os.path.exists(final_path):
        print("FINAL parquet tidak ditemukan.")
        return None

    df = pl.read_parquet(final_path)
    have = set(df['tanggal'].dt.strftime('%Y-%m-%d').to_list())

    start = datetime.strptime(start_date, '%Y-%m-%d')
    end   = datetime.strptime(end_date,   '%Y-%m-%d')
    cur   = start
    missing = []
    while cur <= end:
        label = cur.strftime('%Y-%m-%d')
        if label not in have:
            missing.append((label, cur.strftime('%a')))
        cur += timedelta(days=1)

    weekdays = [d for d, w in missing if w not in ('Sat', 'Sun')]
    weekends = [d for d, w in missing if w in ('Sat', 'Sun')]
    sat_count = sum(1 for d, w in missing if w == 'Sat')
    sun_count = sum(1 for d, w in missing if w == 'Sun')

    print(f"Total missing: {len(missing)} tanggal")
    print(f"  - Weekday gaps (perlu investigasi): {len(weekdays)}")
    print(f"  - Weekend off-days (kemungkinan normal): {len(weekends)}")
    print(f"    breakdown: {sat_count} Sabtu, {sun_count} Minggu")
    if weekdays:
        print(f"  - Contoh weekday gaps (max 20): {weekdays[:20]}")
    return missing


def scrape_range(start_date, end_date, delay=1.0, output_dir='data_emas',
                 refresh_index=False):
    os.makedirs(output_dir, exist_ok=True)

    if refresh_index:
        attempted = set()
        print("[REFRESH] Ignoring sidecar; akan re-attempt semua tanggal")
    else:
        attempted = load_scrape_index(output_dir)
        if attempted:
            print(f"[INDEX] Loaded {len(attempted)} tanggal yang sudah pernah di-attempt")

    start   = datetime.strptime(start_date, '%Y-%m-%d')
    end     = datetime.strptime(end_date,   '%Y-%m-%d')
    current = start

    while current <= end:
        tahun = current.year
        bulan = current.month
        nama_bulan = BULAN_ID[bulan]
        output_file = f"{output_dir}/{tahun}_{bulan:02d}.parquet"

        # Load existing month parquet (untuk merge/dedup kalau ada data baru)
        df_existing = pl.read_parquet(output_file) if os.path.exists(output_file) else None

        # Scrape semua hari dalam bulan ini, skip tanggal yang sudah pernah di-attempt
        bulan_rows = []
        while current.month == bulan and current <= end:
            label = current.strftime('%Y-%m-%d')
            if label in attempted:
                # Sudah pernah di-attempt (data atau off-day), skip silent
                pass
            else:
                print(f"  Fetching {label}...", end=' ')
                hasil = scrape_harga_emas(current.year, current.month, current.day)
                if hasil:
                    bulan_rows.extend(hasil)
                    print(f"✓ {len(hasil)} baris")
                else:
                    print("(off-day)")
                time.sleep(delay)
            attempted.add(label)  # Track setiap attempt, terlepas hasilnya
            current += timedelta(days=1)

        # Merge & dedup kalau ada data baru
        if bulan_rows:
            df_new = pl.DataFrame(bulan_rows).with_columns(
                pl.col('tanggal').str.to_date('%Y-%m-%d')
            )
            if df_existing is not None:
                df_combined = pl.concat([df_existing, df_new]).unique(
                    subset=['tanggal', 'satuan_gr'], keep='last'
                ).sort(['tanggal', 'satuan_gr'])
            else:
                df_combined = df_new.sort(['tanggal', 'satuan_gr'])
            df_combined.write_parquet(output_file)
            print(f"  ✓ Saved {output_file} ({len(df_combined):,} baris)\n")

    # Persist attempt index
    save_scrape_index(output_dir, attempted)

    # Gabungkan semua file parquet jadi satu
    print("Menggabungkan semua file...")
    all_files = sorted([f"{output_dir}/{f}" for f in os.listdir(output_dir)
                        if f.endswith('.parquet') and not f.startswith('.')])
    if not all_files:
        print(f"Tidak ada file parquet di {output_dir}, skip merge.")
        return None
    df_final = pl.concat([pl.read_parquet(f) for f in all_files])
    df_final = df_final.sort(['tanggal', 'satuan_gr'])
    df_final.write_parquet(f"{output_dir}/harga_emas_FINAL.parquet")

    print(f"\n✓ Selesai! Total {len(df_final):,} baris, {df_final['tanggal'].n_unique():,} hari")
    print(f"✓ Tersimpan di {output_dir}/harga_emas_FINAL.parquet")
    return df_final


# ── Test satu tanggal dulu ──
if __name__ == "__main__":
    tanggal_awal = '2018-02-01'
    tanggal_akhir = '2026-06-23'
    delay = 1.0
    output_file = 'data_emas_hargaemas-org'
    hasil = scrape_range(tanggal_awal, tanggal_akhir, delay, output_file)