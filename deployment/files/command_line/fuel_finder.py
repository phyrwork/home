"""Fetch the public Fuel Finder CSV and retain the last valid Bar Hill E10 price.

The developer portal's public download flow sets a cookie, then returns a signed
CSV URL inside an AES envelope. PUBLIC_CLIENT_KEY is shipped in its public JS
bundle: it is response encoding, not an account credential. No login is used.
If that portal contract changes, report the failure and keep the cached price.
"""
import argparse
import csv
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import http.cookiejar
import io
import json
from pathlib import Path
import sys
import urllib.request

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

COOKIE_URL = 'https://www.developer.fuel-finder.service.gov.uk/fuel-finder/internal-api/download-csv'
METADATA_URL = 'https://www.fuel-finder.service.gov.uk/internal/v1.0.2/csv/generate-presigned-url'
PUBLIC_CLIENT_KEY = 'wAndRKxKefFpcuAMQyObcik5zkthu80KcrvK7av3oXCUeyjm3HyzZUuFvR9UN97Kd6jGJANMU9K3TITw1dCPLc'


def decode_metadata(value):
    if 'nxhex' not in value:
        return value
    key = hashlib.sha256(PUBLIC_CLIENT_KEY.encode()).digest()
    decrypt = Cipher(algorithms.AES(key), modes.CBC(bytes.fromhex(value['iv']))).decryptor()
    plain = decrypt.update(bytes.fromhex(value['nxhex'])) + decrypt.finalize()
    unpad = padding.PKCS7(128).unpadder()
    return json.loads(unpad.update(plain) + unpad.finalize())


def timestamp(value):
    # CSV timestamps use JavaScript's UTC date representation.
    return datetime.strptime(value.split(' GMT')[0], '%a %b %d %Y %H:%M:%S').replace(tzinfo=timezone.utc).isoformat()


def parse_price(content):
    rows = list(csv.DictReader(io.StringIO(content.decode('utf-8-sig'))))
    matches = [r for r in rows if r.get('forecourts.location.postcode', '').replace(' ', '').upper() == 'CB238EL'
               and r.get('forecourts.brand_name', '').upper() == 'TESCO']
    if len(matches) != 1:
        raise ValueError('Expected one Tesco Bar Hill row')
    row = matches[0]
    price = Decimal(row['forecourts.fuel_price.E10']) / 100
    if not price.is_finite() or not Decimal('0.1') < price < Decimal('10'):
        raise ValueError('Invalid E10 price')
    if row.get('forecourts.permanent_closure', '').lower() == 'true':
        raise ValueError('Forecourt permanently closed')
    return {'price': float(price), 'last_updated': timestamp(row['forecourts.price_change_effective_timestamp.E10']),
            'price_submitted': timestamp(row['forecourts.price_submission_timestamp.E10']),
            'source': 'Fuel Finder CSV', 'fuel_type': 'E10', 'postcode': 'CB23 8EL',
            'station': row['forecourts.trading_name']}


def fetch_price():
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    opener.addheaders = [('User-Agent', 'Mozilla/5.0'), ('Accept', '*/*')]
    with opener.open(COOKIE_URL, timeout=20) as response:
        response.read()
    with opener.open(METADATA_URL, timeout=20) as response:
        metadata = decode_metadata(json.load(response))['data']
    url = metadata['redirectUrl']
    if not url.startswith('https://ff-raw-data-bronze-ics-prod.s3.eu-west-2.amazonaws.com/'):
        raise ValueError('Unexpected CSV download host')
    with opener.open(url, timeout=30) as response:
        value = parse_price(response.read(20_000_001))
    value['csv_generated_at'] = metadata['generated_at']
    return value


def update(cache, fetch=fetch_price):
    now = datetime.now(timezone.utc).isoformat()
    try:
        value = fetch()
        value.update(last_successful_fetch=now, last_attempt=now, fetch_status='ok')
        cache.parent.mkdir(parents=True, exist_ok=True)
        temporary = cache.with_suffix('.tmp')
        temporary.write_text(json.dumps(value))
        temporary.replace(cache)
        return value
    except Exception as exc:
        # Never log exception URLs: they can contain temporary signed credentials.
        if not cache.exists():
            raise RuntimeError('No valid cached fuel price; fetch failed: ' + type(exc).__name__) from None
        value = json.loads(cache.read_text())
        value.update(last_attempt=now, fetch_status='cached', fetch_error=type(exc).__name__)
        return value


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--cache', type=Path, default=Path('/config/fuel_finder_bar_hill.json'))
    args = parser.parse_args()
    try:
        print(json.dumps(update(args.cache)))
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
