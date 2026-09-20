"""Check tariff signs and attribution against concrete power-flow examples."""
from pathlib import Path
import unittest

import jinja2
import yaml


class CostRatesTest(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).resolve().parents[1] / 'deployment/files/templates/power_cost_rates.yaml'
        self.templates = {s['unique_id']: s for s in yaml.safe_load(path.read_text())['sensor']}
        power_path = path.with_name('house_battery_accounting_power.yaml')
        self.templates.update({s['unique_id']: s for s in yaml.safe_load(power_path.read_text())['sensor']})

    def render(self, name, *, battery=0, grid=0, ev=0, buy=0.3, sell=0.12):
        def state(entity):
            if entity in ('sensor.house_battery_avoided_import_power', 'sensor.house_battery_arbitrage_power'):
                return str(self.render(entity.removeprefix('sensor.'), battery=battery, grid=grid, ev=ev, buy=buy, sell=sell))
            if entity.endswith('battery_power'):
                return str(battery)
            if entity.endswith('current_demand'):
                return str(grid)
            if entity.endswith('export_current_rate'):
                return str(sell)
            if entity.endswith('current_rate'):
                return str(buy)
            if entity == 'sensor.ev_charger_energy_meter_power':
                return str(ev)
            raise AssertionError(entity)
        def is_number(value):
            try:
                float(value)
                return True
            except ValueError:
                return False
        env = jinja2.Environment(undefined=jinja2.StrictUndefined)
        env.globals.update(states=state, is_number=is_number)
        item = self.templates[name]
        if env.from_string(item['availability']).render().strip() != 'True':
            return None
        return float(env.from_string(item['state']).render())

    def test_ev(self):
        self.assertAlmostEqual(self.render('ev_charging_cost_rate', ev=7000, buy=.069), .483)
        self.assertAlmostEqual(self.render('ev_charging_cost_rate', ev=7000, buy=-.01), -.07)

    def test_grid(self):
        self.assertEqual(self.render('electricity_net_cost_rate', grid=5000), 1.5)
        self.assertEqual(self.render('electricity_net_cost_rate', grid=-5000), -.6)
        self.assertEqual(self.render('electricity_net_cost_rate', grid=0), 0)

    def test_charge_cost_and_no_discharge_credit(self):
        self.assertEqual(self.render('house_battery_arbitrage_cost_rate', battery=4750, buy=.069), -.345)
        self.assertEqual(self.render('house_battery_avoided_import_cost_rate', battery=4750), 0)

    def test_split_discharge_between_house_and_export(self):
        # 5 kW DC -> 4.75 kW AC, of which 3 kW is exported and 1.75 kW used at home.
        args = dict(battery=-5000, grid=-3000)
        self.assertEqual(self.render('house_battery_avoided_import_cost_rate', **args), .525)
        self.assertEqual(self.render('house_battery_arbitrage_cost_rate', **args), .36)

    def test_pv_export_cannot_be_credited_beyond_battery_discharge(self):
        self.assertEqual(self.render('house_battery_arbitrage_cost_rate', battery=-5000, grid=-6000), .57)
        self.assertEqual(self.render('house_battery_avoided_import_cost_rate', battery=-5000, grid=-6000), 0)
        self.assertEqual(self.render('house_battery_arbitrage_cost_rate', battery=0, grid=-6000), 0)

    def test_energy_ledgers_sum_to_net_ac_battery_output(self):
        for battery, grid in [(4750, 7000), (-5000, -3000), (-5000, 2000), (-5000, -6000), (0, -2000)]:
            home = self.render('house_battery_avoided_import_power', battery=battery, grid=grid)
            arb = self.render('house_battery_arbitrage_power', battery=battery, grid=grid)
            expected = -battery / .95 if battery >= 0 else -battery * .95
            self.assertAlmostEqual(home + arb, expected, places=5)

    def test_missing_data_is_not_a_zero_price(self):
        self.assertIsNone(self.render('ev_charging_cost_rate', buy='unavailable'))
        self.assertIsNone(self.render('house_battery_arbitrage_cost_rate', battery='unknown'))
        self.assertIsNone(self.render('electricity_net_cost_rate', grid='unavailable'))

    def test_non_ev_balance_reconciles_cumulative_costs(self):
        env = jinja2.Environment(undefined=jinja2.StrictUndefined)
        for net, ev, expected in [(10, 7, 3), (2, 7, -5), (-2, 1, -3)]:
            values = {'sensor.electricity_total_net_cost': str(net),
                      'sensor.ev_charging_total_cost': str(ev)}
            env.globals['states'] = values.__getitem__
            value = env.from_string(self.templates['electricity_total_net_cost_excluding_ev']['state']).render()
            self.assertEqual(float(value), expected)


if __name__ == '__main__':
    unittest.main()
