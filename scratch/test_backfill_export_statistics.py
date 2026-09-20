"""Behavior checks for reconstruction at interval boundaries and recorder cutover."""
import sqlite3
import time
import unittest
import backfill_export_statistics as repair

class BackfillTests(unittest.TestCase):
    def test_power_history_clamps_each_sample_and_keeps_unknowns(self):
        c=sqlite3.connect(':memory:');c.row_factory=sqlite3.Row
        c.execute('CREATE TABLE states_meta(metadata_id INTEGER,entity_id TEXT)')
        c.execute('CREATE TABLE states(state_id INTEGER,metadata_id INTEGER,state TEXT,last_updated_ts REAL,attributes_id INTEGER)')
        c.execute('INSERT INTO states_meta VALUES(1,?)',(repair.DEMAND,))
        c.execute('INSERT INTO states_meta VALUES(2,?)',('sensor.current_export_electricity_21l4421345_2700009249389',))
        for i,(state,t) in enumerate([('-300',100),('40',160),('unavailable',220)],1):
            c.execute('INSERT INTO states VALUES(?,1,?,?,1)',(i,state,t))
        c.execute("INSERT INTO states VALUES(10,2,'unavailable',130,2)")
        plan=repair.power_history_plan(c,280)
        self.assertEqual([(r[3],r[2]) for r in plan],[(100.0,'300.0'),(130.0,'300.0'),(160.0,'0'),(220.0,'unavailable')])
        self.assertEqual(plan[1][0],10)

    def test_minute_data_wins_and_live_counter_is_preserved(self):
        c=sqlite3.connect(':memory:');c.row_factory=sqlite3.Row
        c.execute('CREATE TABLE statistics_meta(id INTEGER,statistic_id TEXT,unit_of_measurement TEXT)')
        for table in ['statistics','statistics_short_term']:
            c.execute(f'CREATE TABLE {table}(metadata_id INTEGER,start_ts REAL,state REAL,sum REAL,last_reset_ts REAL,mean REAL)')
        c.execute('CREATE TABLE states_meta(metadata_id INTEGER,entity_id TEXT)')
        c.execute('CREATE TABLE states(metadata_id INTEGER,state TEXT,last_updated_ts REAL)')
        keys=[repair.EXPORT,repair.COST,repair.METER,repair.METER_COST,repair.RATE]
        for i,k in enumerate(keys,1):c.execute('INSERT INTO statistics_meta VALUES(?,?,?)',(i,k,'GBP' if k in [repair.COST,repair.METER_COST] else 'kWh'))
        end=int(time.time()//300*300);start=end//3600*3600-7200
        c.execute('INSERT INTO states_meta VALUES(1,?)',(repair.DEMAND,))
        # 3.6 kW for 15 minutes: left integration gives exactly 0.9 kWh.
        for t,p in [(start,-3600),(start+900,0),(end+1,0)]:c.execute('INSERT INTO states VALUES(1,?,?)',(str(p),t))
        for mid in [1,2,3,4]:
            for h,val in [(start-3600,0),(start,5),(start+3600,10)]:
                c.execute('INSERT INTO statistics VALUES(?,?,?,?,NULL,NULL)',(mid,h,val,val))
        c.execute('INSERT INTO statistics VALUES(5,?,0.12,NULL,NULL,NULL)',(start-3600,))
        for mid,value in [(1,100),(2,0)]:
            c.execute('INSERT INTO statistics_short_term VALUES(?,?,?,?,?,NULL)',(mid,end-300,value,10,None if mid==1 else end-300))
        plan,report,ids=repair.build(c)
        hourly=[r for r in plan['statistics'] if r['metadata_id']==1]
        first=next(r for r in hourly if r['start_ts']==start)
        before=next(r for r in hourly if r['start_ts']==start-3600)
        self.assertAlmostEqual(first['sum']-before['sum'],0.9)
        last=next(r for r in reversed(plan['statistics_short_term']) if r['metadata_id']==1)
        self.assertEqual(last['state'],100)
        lastcost=next(r for r in reversed(plan['statistics_short_term']) if r['metadata_id']==2)
        self.assertEqual(lastcost['state'],0)
        self.assertEqual(lastcost['last_reset_ts'],end-300)
        self.assertTrue(report['provenance']['minute_demand_hours']>=2)
        # The 5-minute slots retain the actual onset/cessation timing.
        energy=[r for r in plan['statistics_short_term'] if r['metadata_id']==1]
        self.assertAlmostEqual(energy[1]['sum']-energy[0]['sum'],0.3)
        self.assertAlmostEqual(energy[3]['sum']-energy[2]['sum'],0)

if __name__=='__main__':unittest.main()
