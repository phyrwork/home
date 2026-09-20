"""Run on HA with Core stopped. Backup before writing; refuse repeat application."""
import json
import sqlite3
import time
from pathlib import Path

PREFIX = 'sensor.garage_inverter_telemetry_garage_inverter_'
TARGET = 'sensor.garage_inverter_losses_estimated'


def losses(charged, discharged):
    return round(charged * (1 / 0.95 - 1) + discharged * (1 - 0.95), 6)


def build_rows(charge, discharge):
    # Require exact common coverage: never invent missing observations.
    assert charge.keys() == discharge.keys(), 'Source time coverage differs'
    rows = []
    for start in sorted(charge):
        cs, cu = charge[start]
        ds, du = discharge[start]
        assert all(v is not None and v >= 0 for v in (cs, cu, ds, du))
        rows.append((start, losses(cs, ds), losses(cu, du)))
    assert rows
    assert all(b[2] >= a[2] for a, b in zip(rows, rows[1:])), 'Non-monotonic sums'
    return rows


def main():
    folder = Path('/config/inverter-losses-backfill-20260920')
    folder.mkdir(exist_ok=True)
    backup = folder / 'recorder-before.sqlite'
    assert not backup.exists(), 'Backup exists: inspect previous run before retrying'
    db = sqlite3.connect('/config/home-assistant_v2.db')
    ids = [db.execute('SELECT id FROM statistics_meta WHERE statistic_id=?',
                     (PREFIX + name,)).fetchone()[0]
           for name in ('total_energy_charged', 'total_energy_discharged')]
    prepared = {}
    for table in ('statistics', 'statistics_short_term'):
        sources = [dict((start, (state, total)) for start, state, total in db.execute(
            f'SELECT start_ts,state,sum FROM {table} WHERE metadata_id=?', (mid,)))
            for mid in ids]
        prepared[table] = build_rows(*sources)
    with sqlite3.connect(backup) as destination:
        db.backup(destination)
    report = {}
    with db:
        db.execute('''INSERT OR IGNORE INTO statistics_meta
            (statistic_id,source,unit_of_measurement,unit_class,has_sum,mean_type)
            VALUES (?, 'recorder','kWh','energy',1,0)''', (TARGET,))
        target_id = db.execute('SELECT id FROM statistics_meta WHERE statistic_id=?',
                               (TARGET,)).fetchone()[0]
        for table, rows in prepared.items():
            # Replace only this new estimate's statistics. Its source sums carry
            # recorder reset handling; states match the live lifetime formula.
            db.execute(f'DELETE FROM {table} WHERE metadata_id=?', (target_id,))
            db.executemany(f'''INSERT INTO {table}
                (metadata_id,created_ts,start_ts,state,sum) VALUES (?,?,?,?,?)''',
                [(target_id, time.time(), start, state, total) for start, state, total in rows])
            actual = db.execute(f'SELECT start_ts,state,sum FROM {table} WHERE metadata_id=? ORDER BY start_ts',
                                (target_id,)).fetchall()
            assert actual == rows
            report[table] = dict(rows=len(rows), start=rows[0][0], end=rows[-1][0],
                                 first_sum=rows[0][2], last_sum=rows[-1][2])
    assert db.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
    db.close()
    (folder / 'applied.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(report))


if __name__ == '__main__':
    main()
