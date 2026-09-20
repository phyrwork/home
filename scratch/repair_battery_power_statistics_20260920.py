import sqlite3, json, time
from pathlib import Path
p=Path('/config/battery-power-repair-20260920');p.mkdir(exist_ok=True)
c=sqlite3.connect('/config/home-assistant_v2.db')
assert c.execute('PRAGMA quick_check').fetchone()[0]=='ok'
backup=p/'recorder-before.sqlite'
assert not backup.exists(), 'Backup already exists; do not rerun blindly'
b=sqlite3.connect(backup);c.backup(b);b.close()
source='sensor.garage_inverter_telemetry_garage_inverter_battery_power'
sid=c.execute('SELECT id FROM statistics_meta WHERE statistic_id=?',(source,)).fetchone()[0]
tid=c.execute('SELECT id FROM statistics_meta WHERE statistic_id=?',(source+'_inverted',)).fetchone()[0]
report={}
with c:
 for table in ['statistics','statistics_short_term']:
  before=c.execute(f'SELECT count(*) FROM {table} WHERE metadata_id=?',(tid,)).fetchone()[0]
  rows=c.execute(f'SELECT created_ts,start_ts,mean,mean_weight,min,max FROM {table} WHERE metadata_id=?',(sid,)).fetchall()
  assert rows
  for created,start,mean,weight,low,high in rows:
   c.execute(f'''INSERT INTO {table} (metadata_id,created_ts,start_ts,mean,mean_weight,min,max)
    VALUES (?,?,?,?,?,?,?) ON CONFLICT(metadata_id,start_ts) DO UPDATE SET
    mean=excluded.mean,mean_weight=excluded.mean_weight,min=excluded.min,max=excluded.max''',
    (tid,created,start,None if mean is None else -mean,weight,None if high is None else -high,None if low is None else -low))
  bad=c.execute(f'''SELECT count(*) FROM {table} s JOIN {table} t ON t.start_ts=s.start_ts AND t.metadata_id=?
   WHERE s.metadata_id=? AND (t.mean IS NOT -s.mean OR t.min IS NOT -s.max OR t.max IS NOT -s.min OR t.mean_weight IS NOT s.mean_weight)''',(tid,sid)).fetchone()[0]
  assert bad==0
  report[table]={'source_rows':len(rows),'target_before':before,'mismatches':bad,'start':rows[0][1],'end':rows[-1][1]}
assert c.execute('PRAGMA quick_check').fetchone()[0]=='ok'
c.close();(p/'applied.json').write_text(json.dumps(report,indent=2));print(json.dumps(report))
