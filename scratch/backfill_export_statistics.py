"""Repair this installation's export statistics, with HA stopped for --apply.

Dry-run is read-only. Prefer retained minute demand; for older gaps use Octopus statistics and
integrate retained minute demand (left rule). Preserve existing data in older gaps.
Only the configured export-energy and compensation statistic rows are written.
A complete SQLite backup is mandatory for apply. Export-power history is also
reconstructed from the original minute demand; other raw entities are untouched.
"""
import argparse
import bisect
import collections
from datetime import datetime, UTC
import json
import math
from pathlib import Path
import sqlite3
import time

LEGACY = 'sensor.current_accumulative_consumption_export_electricity_21l4421345_2700009249389'
LEGACY_COST = LEGACY + '_compensation_2'
EXPORT = LEGACY
COST = LEGACY_COST
METER = 'octopus_energy:electricity_21l4421345_2700009249389_export_previous_accumulative_consumption'
METER_COST = 'octopus_energy:electricity_21l4421345_2700009249389_export_previous_accumulative_cost'
DEMAND = 'sensor.octopus_energy_electricity_21l4421345_2700007165105_current_demand'
RATE = 'sensor.octopus_energy_electricity_21l4421345_2700009249389_export_current_rate'

def iso(t):
    return datetime.fromtimestamp(t, UTC).isoformat()

def numeric(v):
    try:
        v = float(v)
        return v if math.isfinite(v) else None
    except (ValueError, TypeError):
        return None

def build(c):
    meta = {r['statistic_id']: dict(r) for r in c.execute('SELECT * FROM statistics_meta')}
    ids = {k: meta[k]['id'] for k in [EXPORT, COST, METER, METER_COST, RATE, LEGACY, LEGACY_COST]}
    assert meta[EXPORT]['unit_of_measurement'] == 'kWh'
    assert meta[COST]['unit_of_measurement'] == 'GBP'
    def rows(table, key):
        return [dict(r) for r in c.execute(f'SELECT * FROM {table} WHERE metadata_id=? ORDER BY start_ts', (ids[key],))]
    old = {k: rows('statistics', k) for k in ids}
    short = {k: rows('statistics_short_term', k) for k in [EXPORT, COST]}
    assert short[EXPORT] and short[COST], 'Both live sensors must have recent statistics'
    last = min(short[k][-1]['start_ts'] for k in short)
    end = int(last + 300)
    assert time.time() - end < 1200, 'Live statistics are stale'
    # The restored state at cutover must equal the final retained state. A backfill
    # changes the statistics baseline, not the running sensor's restored counter.
    live = {}
    for key in [EXPORT, COST]:
        candidates = [r for r in short[key] if r['start_ts'] <= last]
        live[key] = candidates[-1]
        assert all(abs(r['state']-live[key]['state']) < 1e-7 for r in short[key] if r['start_ts'] >= last), 'Sensor changed after cutover'
    dm = c.execute('SELECT metadata_id FROM states_meta WHERE entity_id=?', (DEMAND,)).fetchone()[0]
    raw = [dict(r) for r in c.execute('SELECT state,last_updated_ts FROM states WHERE metadata_id=? ORDER BY last_updated_ts', (dm,))]
    assert raw and raw[-1]['last_updated_ts'] >= end
    five = collections.defaultdict(float)
    coverage = collections.defaultdict(float)
    bridged_zero = []
    for i,(a,b) in enumerate(zip(raw, raw[1:])):
        p = numeric(a['state'])
        if p is None:
            before = numeric(raw[i-1]['state']) if i else None
            after = numeric(b['state'])
            gap = b['last_updated_ts']-a['last_updated_ts']
            if before is not None and after is not None and before >= 0 and after >= 0 and gap <= 180:
                p = 0
                bridged_zero.append({'start':iso(a['last_updated_ts']),'seconds':round(gap,3),'note':'Short unavailable gap bracketed by importing readings; estimated zero export'})
            else:
                continue
        p = max(-p, 0)
        t = a['last_updated_ts']
        stop = min(b['last_updated_ts'], end)
        while t < stop:
            slot = math.floor(t/300)*300
            edge = min(stop,slot+300)
            five[slot] += p*(edge-t)/3600000
            coverage[slot] += edge-t
            t = edge
    def increments(rr):
        out = {}
        for a,b in zip(rr,rr[1:]):
            if a['sum'] is not None and b['sum'] is not None:
                delta = b['sum']-a['sum']
                assert delta > -1e-6, 'Unexpected decreasing export sum'
                out[int(b['start_ts'])] = max(delta,0)
        return out
    meter, meter_cost = increments(old[METER]), increments(old[METER_COST])
    previous = increments(old[LEGACY])
    previous_cost = increments(old[LEGACY_COST])
    rates = [(r['start_ts'],r['mean'] if r['mean'] is not None else r['state']) for r in old[RATE] if r['mean'] is not None or r['state'] is not None]
    def rate_at(t):
        pos = bisect.bisect_right([r[0] for r in rates],t)-1
        assert pos >= 0, 'Missing historical rate'
        return rates[pos][1]
    start = int(old[LEGACY][0]['start_ts'])
    sums = {start: [old[LEGACY][0]['sum'],0.0]}
    hourly = {}; provenance = collections.Counter(); uncertain = []
    latest_meter_hour = max(meter)
    for h in range(start, end,3600):
        slots = [h+i*300 for i in range(12) if h+i*300 < end]
        measured = sum(five[t] for t in slots)
        covered = sum(coverage[t] for t in slots)
        if covered >= len(slots)*300-1:
            amount = measured
            price = amount*rate_at(h)
            provenance['minute_demand_hours'] += 1
        elif h in meter and h+3600 <= end:
            amount = meter[h]
            price = meter_cost.get(h,amount*rate_at(h))
            provenance['meter_hours'] += 1
            if h >= int(raw[0]['last_updated_ts']//3600*3600) and covered < 3599 and amount > 0:
                uncertain.append({'hour':iso(h),'covered_seconds':round(covered),'kwh':amount,'note':'Metered hourly total; five-minute allocation is estimated'})
        elif h >= latest_meter_hour+3600:
            assert covered >= len(slots)*300-1, f'Missing demand after metered coverage at {iso(h)}'
            amount = measured
            price = amount*rate_at(h)
            provenance['demand_hours'] += 1
        else:
            # Do not pretend missing supplier history can be recovered from means.
            amount = previous.get(h,0)
            price = previous_cost.get(h,amount*rate_at(h))
            provenance['preserved_gap_hours'] += 1
        hourly[h] = (amount,price)
        base = sums[h]
        running = list(base)
        for i,t in enumerate(slots):
            if covered < len(slots)*300-1 and measured <= amount and h >= int(raw[0]['last_updated_ts']//3600*3600):
                missing = len(slots)*300-covered
                step_energy = five[t]+(amount-measured)*(300-coverage[t])/missing
                weight = step_energy/amount if amount > 0 else 1/len(slots)
            else:
                weight = five[t]/measured if measured > 0 else 1/len(slots)
            running = [running[0]+amount*weight,running[1]+price*weight]
            if i == len(slots)-1:
                running = [base[0]+amount,base[1]+price]
            sums[t+300] = list(running)
    final = sums[end]
    last_numeric = next((numeric(r['state']) for r in reversed(raw) if numeric(r['state']) is not None), None)
    assert last_numeric is not None and last_numeric >= 0, 'Apply during a zero-export interval'
    planned = {'statistics': [], 'statistics_short_term': []}
    short_start = math.ceil(raw[0]['last_updated_ts']/300)*300
    cost_reset = live[COST]['last_reset_ts']
    def point(table,key,t,period):
        idx = 0 if key == EXPORT else 1
        value = sums[t+period][idx]
        if key == EXPORT:
            state = live[key]['state'] + value-final[idx]
            reset = live[key]['last_reset_ts']
        else:
            state,reset = value,None
            if cost_reset is not None and t+period > cost_reset:
                # No export since live compensation was restarted at cutover.
                # Preserve its exact state/reset so recorder resumes without a jump.
                state = live[key]['state'] + value-final[idx]
                reset = cost_reset
        assert state > -1e-6, 'Cannot align reconstructed state to live counter'
        planned[table].append({'metadata_id':ids[key],'start_ts':t,'state':state,'sum':value,'last_reset_ts':reset})
    for h in range(start,end//3600*3600,3600):
        for key in [EXPORT,COST]: point('statistics',key,h,3600)
    for t in range(short_start,end,300):
        for key in [EXPORT,COST]: point('statistics_short_term',key,t,300)
    days = collections.defaultdict(lambda:[0.0,0.0])
    from zoneinfo import ZoneInfo
    for h,(energy,cost) in hourly.items():
        day = datetime.fromtimestamp(h,ZoneInfo('Europe/London')).date().isoformat()
        days[day][0] += energy;days[day][1] += cost
    report = {'start':iso(start),'end':iso(end),'provenance':dict(provenance),'uncertain_five_minute_allocation':uncertain,'estimated_zero_export_gaps':bridged_zero,'live_state_preserved':{k:live[k]['state'] for k in live},'old_latest_sums':{k:live[k]['sum'] for k in live},'new_latest_sums':dict(zip([EXPORT,COST],final)),'rows':{k:len(v) for k,v in planned.items()},'recent_local_days':{k:[round(v[0],6),round(v[1],6)] for k,v in days.items() if k >= '2026-09-01'}}
    return planned,report,ids

def power_history_plan(c,end):
    power='sensor.current_export_electricity_21l4421345_2700009249389'
    source_id=c.execute('SELECT metadata_id FROM states_meta WHERE entity_id=?',(DEMAND,)).fetchone()[0]
    target_id=c.execute('SELECT metadata_id FROM states_meta WHERE entity_id=?',(power,)).fetchone()[0]
    attrs=c.execute('SELECT attributes_id FROM states WHERE metadata_id=? AND attributes_id IS NOT NULL ORDER BY last_updated_ts DESC LIMIT 1',(target_id,)).fetchone()[0]
    source=list(c.execute('SELECT state,last_updated_ts FROM states WHERE metadata_id=? AND last_updated_ts<? ORDER BY last_updated_ts',(source_id,end)))
    times=[];values=[];changes=[];previous=None;changed=None
    for r in source:
        v=numeric(r['state']);state='unavailable' if v is None else str(max(-v,0))
        t=r['last_updated_ts']
        if state!=previous:changed=t
        times.append(t);values.append(state);changes.append(changed);previous=state
    existing=list(c.execute('SELECT state_id,last_updated_ts FROM states WHERE metadata_id=? AND last_updated_ts>=? AND last_updated_ts<?',(target_id,times[0],end)))
    known={r['last_updated_ts']:r['state_id'] for r in existing}
    plan=[]
    for t in sorted(set(times)|set(known)):
        i=bisect.bisect_right(times,t)-1
        plan.append((known.get(t),target_id,values[i],t,changes[i],attrs))
    return plan

def main():
    p=argparse.ArgumentParser();p.add_argument('database');p.add_argument('--apply',action='store_true');p.add_argument('--backup');p.add_argument('--report',required=True);a=p.parse_args()
    c=sqlite3.connect(f'file:{a.database}?mode={"rw" if a.apply else "ro"}',uri=True);c.row_factory=sqlite3.Row
    plan,report,ids=build(c)
    power_plan=power_history_plan(c,int(datetime.fromisoformat(report['end']).timestamp()))
    report['power_history']={'updated_rows':sum(r[0] is not None for r in power_plan),'inserted_rows':sum(r[0] is None for r in power_plan),'formula':'max(-current_demand,0); unavailable source remains unavailable'}
    Path(a.report).write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2),flush=True)
    if not a.apply:return
    assert a.backup and not Path(a.backup).exists(),'A new full backup path is required'
    # Caller must stop Home Assistant first. A changing raw-state table aborts.
    before=c.execute('SELECT max(state_id) FROM states').fetchone()[0]
    time.sleep(2)
    assert before==c.execute('SELECT max(state_id) FROM states').fetchone()[0], 'Recorder is still writing'
    with sqlite3.connect(a.backup) as backup:c.backup(backup)
    print('Full database backup complete',flush=True)
    with c:
        for state_id,metadata_id,state,updated,changed,attrs in power_plan:
            if state_id is None:
                c.execute('INSERT INTO states(metadata_id,state,last_updated_ts,last_changed_ts,attributes_id) VALUES(?,?,?,?,?)',(metadata_id,state,updated,changed,attrs))
            else:
                c.execute('UPDATE states SET state=?,last_changed_ts=?,attributes_id=? WHERE state_id=?',(state,changed,attrs,state_id))
        for table,rows in plan.items():
            for row in rows:
                c.execute(f'INSERT INTO {table}(metadata_id,start_ts,created_ts,state,sum,last_reset_ts) VALUES(:metadata_id,:start_ts,:created_ts,:state,:sum,:last_reset_ts) ON CONFLICT(metadata_id,start_ts) DO UPDATE SET state=excluded.state,sum=excluded.sum,last_reset_ts=excluded.last_reset_ts',dict(row,created_ts=time.time()))
        for table in plan:
            for key in [EXPORT,COST]:
                rr=c.execute(f'SELECT sum FROM {table} WHERE metadata_id=? ORDER BY start_ts',(ids[key],)).fetchall()
                assert all(b[0]>=a[0]-1e-6 for a,b in zip(rr,rr[1:])),'Non-monotonic repaired sums'
    assert c.execute('PRAGMA quick_check').fetchone()[0]=='ok'
    print('Repair committed; database quick_check passed',flush=True)

if __name__=='__main__': main()
