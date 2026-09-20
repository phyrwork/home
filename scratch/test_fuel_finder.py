import csv
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest

path = Path(__file__).resolve().parents[1] / 'deployment/files/command_line/fuel_finder.py'
spec = importlib.util.spec_from_file_location('fuel_finder', path)
fuel = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fuel)


class FuelFinderTest(unittest.TestCase):
    def row(self, price='170.9000'):
        return {'forecourts.brand_name': 'TESCO', 'forecourts.location.postcode': 'CB23 8EL',
                'forecourts.trading_name': 'CAMBRIDGE BAR HILL EXTRA',
                'forecourts.fuel_price.E10': price,
                'forecourts.price_change_effective_timestamp.E10': 'Fri Sep 18 2026 14:18:55 GMT+0000 (Coordinated Universal Time)',
                'forecourts.price_submission_timestamp.E10': 'Fri Sep 18 2026 14:23:24 GMT+0000 (Coordinated Universal Time)'}

    def csv(self, rows):
        out = io.StringIO()
        writer = csv.DictWriter(out, fieldnames=list(self.row()))
        writer.writeheader(); writer.writerows(rows)
        return out.getvalue().encode('utf-8-sig')

    def test_price_conversion_and_effective_date(self):
        result = fuel.parse_price(self.csv([self.row()]))
        self.assertEqual(result['price'], 1.709)
        self.assertEqual(result['last_updated'], '2026-09-18T14:18:55+00:00')
        self.assertEqual(result['fuel_type'], 'E10')

    def test_ambiguous_or_missing_station_fails(self):
        for rows in ([], [self.row(), self.row()]):
            with self.assertRaises(ValueError): fuel.parse_price(self.csv(rows))

    def test_invalid_price_is_not_accepted(self):
        for price in ('0', 'NaN', '-1', '9999', ''):
            with self.assertRaises(Exception): fuel.parse_price(self.csv([self.row(price)]))

    def test_cache_keeps_price_and_original_freshness_on_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / 'price.json'
            good = fuel.update(cache, lambda: fuel.parse_price(self.csv([self.row()])))
            def failing(): raise OSError('private signed URL must not be printed')
            cached = fuel.update(cache, failing)
            self.assertEqual(cached['price'], 1.709)
            self.assertEqual(cached['last_updated'], good['last_updated'])
            self.assertEqual(cached['last_successful_fetch'], good['last_successful_fetch'])
            self.assertEqual(cached['fetch_status'], 'cached')
            self.assertNotIn('private signed', json.dumps(cached))
            self.assertEqual(json.loads(cache.read_text()), good)

    def test_first_fetch_failure_does_not_invent_price(self):
        with tempfile.TemporaryDirectory() as directory:
            def failing(): raise OSError('failed')
            with self.assertRaises(RuntimeError): fuel.update(Path(directory) / 'price.json', failing)


if __name__ == '__main__':
    unittest.main()
