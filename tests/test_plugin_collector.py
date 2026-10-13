import concurrent.futures
import os
from pathlib import Path
import signal
import json
import socket
import subprocess
import tempfile
import time
import unittest
try:
    from codex_usage.collector import CollectorClient, CollectorConfig, collector_health
    from codex_usage.lifecycle import CollectionLock, process_identity, CollectorBusy
except ImportError:
    CollectorClient = None

FAKE = '''#!/usr/bin/env python3
import json,sys
from pathlib import Path
count=0
for line in sys.stdin:
 r=json.loads(line)
 if 'id' not in r: continue
 if r['method']=='initialize': result={}
 elif Path(sys.argv[0]).with_name('fail').exists():
  print(json.dumps({'id':r['id'],'error':{'message':'private diagnostic must not leak'}}),flush=True)
  continue
 else:
  count+=1
  result={'rateLimitsByLimitId':{'codex':{'primary':{'usedPercent':count,'windowDurationMins':300,'resetsAt':2000000000}}}}
 print(json.dumps({'id':r['id'],'result':result}),flush=True)
'''


def eventually(predicate, timeout=8):
    deadline = time.monotonic()+timeout
    while time.monotonic()<deadline:
        if predicate(): return
        time.sleep(.05)
    raise AssertionError('Condition not reached within deadline')


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(CollectorClient, 'Shared collector is missing')
        self.temp = tempfile.TemporaryDirectory(prefix='cue-')
        self.addCleanup(self.temp.cleanup)
        root=Path(self.temp.name)
        fake=root/'codex';fake.write_text(FAKE);fake.chmod(0o700)
        self.owner=subprocess.Popen(['sleep','60'])
        self.addCleanup(self.clean_owner)
        self.config=CollectorConfig(root/'usage.sqlite3',str(fake),root/'home', interval=.1,
                                    runtime_root=root/'runtime')
        self.client=CollectorClient(self.config,owner=process_identity(self.owner.pid))
        self.addCleanup(self.stop)

    def clean_owner(self):
        self.owner.terminate();self.owner.wait()

    def stop(self):
        try:self.client.stop()
        except (OSError,ValueError):pass

    def test_stats_never_starts_collector(self):
        self.assertEqual(self.client.stats('a')['state'],'stopped')
        self.assertFalse(self.config.db.exists())

    def test_simultaneous_starts_attach_to_one_collector_and_timer_runs_without_calls(self):
        with concurrent.futures.ThreadPoolExecutor(4) as pool:
            starts=list(pool.map(lambda _:self.client.start(),range(4)))
        self.assertEqual(len({r['collector_id'] for r in starts}),1)
        first=self.client.stats('a')
        time.sleep(.4)
        second=self.client.stats('b')
        self.assertGreaterEqual(second['successful_polls']-first['successful_polls'],3)
        self.assertGreater(second['latest_sample_timestamp'],first['latest_sample_timestamp'])
        self.assertEqual(second['provider_pid'],first['provider_pid'])
        self.assertEqual(second['collector_id'],first['collector_id'])
        self.assertGreater(second['windows'][0]['sample']['used_percent'],first['windows'][0]['sample']['used_percent'])

    def test_reset_isolation_and_stats_do_not_poll(self):
        self.client.start()
        before=self.client.stats('a')
        self.client.reset('a')
        a=self.client.stats('a');b=self.client.stats('b')
        self.assertIsNotNone(a['calculation_since'])
        self.assertIsNone(b['calculation_since'])
        self.assertGreaterEqual(a['sample_count'],before['sample_count'])
        self.client.reset(None)
        self.assertEqual(self.client.stats('a')['calculation_since'],self.client.stats('b')['calculation_since'])

    def test_client_exit_stops_collector_and_provider(self):
        self.client.start();status=self.client.stats('a')
        self.owner.terminate();self.owner.wait()
        eventually(lambda:self.client.stats('a')['state']=='stopped')
        eventually(lambda:process_identity(status['provider_pid']) is None)

    def test_second_owner_survives_first_owner_exit(self):
        owner2=subprocess.Popen(['sleep','60'])
        try:
            first=self.client.start()
            other=CollectorClient(self.config,owner=process_identity(owner2.pid))
            self.assertEqual(other.start()['collector_id'],first['collector_id'])
            self.owner.terminate();self.owner.wait();time.sleep(.3)
            self.assertEqual(other.stats('b')['state'],'running')
        finally:owner2.terminate();owner2.wait()

    def test_crash_releases_lock_and_next_start_recovers_without_losing_history(self):
        self.client.start();before=self.client.stats('a')
        os.kill(before['collector_pid'],signal.SIGKILL)
        eventually(lambda:process_identity(before['collector_pid']) is None)
        self.client.start();after=self.client.stats('a')
        self.assertNotEqual(after['collector_id'],before['collector_id'])
        self.assertGreater(after['sample_count'],before['sample_count'])
        # An orphaned provider is handled by EOF on its input pipe.
        eventually(lambda:process_identity(before['provider_pid']) is None)

    def test_watch_lock_prevents_second_collector_for_database(self):
        with CollectionLock(self.config.db):
            with self.assertRaises(CollectorBusy):self.client.start()
        self.assertEqual(self.client.start()['state'],'running')

    def test_invalid_session_and_missing_owner_do_not_start_or_reset(self):
        with self.assertRaises(ValueError):self.client.stats('../x')
        with self.assertRaises(ValueError):self.client.reset('')
        with self.assertRaises(ValueError):CollectorClient(self.config,owner=None).start()

    def test_provider_failure_reports_error_retains_history_and_recovers(self):
        self.client.start();before=self.client.stats('a')
        marker=Path(self.temp.name)/'fail';marker.touch()
        eventually(lambda:self.client.stats('a')['state']=='error')
        broken=self.client.stats('a')
        self.assertGreaterEqual(broken['sample_count'],before['sample_count'])
        self.assertNotIn('private diagnostic',str(broken))
        marker.unlink()
        eventually(lambda:self.client.stats('a')['state']=='running')

    def test_repeated_stats_do_not_increment_poll_count(self):
        self.config.interval=30
        self.client.start()
        for _ in range(5):self.assertEqual(self.client.stats('a')['successful_polls'],1)

    def test_health_distinguishes_starting_stale_and_error(self):
        self.assertEqual(collector_health(None,None,1000,30),('starting',None))
        self.assertEqual(collector_health(950,None,1000,30),('running',50))
        self.assertEqual(collector_health(800,None,1000,30),('stale',200))
        self.assertEqual(collector_health(950,'offline',1000,30),('error',50))

    def test_bundled_client_starts_from_unrelated_directory_without_pythonpath(self):
        import sys
        source=Path(__file__).resolve().parents[1]/'src'
        script='import sys,json;sys.path.insert(0,sys.argv[1]);from codex_usage.collector import CollectorClient,CollectorConfig; c=CollectorClient(CollectorConfig(**json.loads(sys.argv[2])),owner=json.loads(sys.argv[3]));print(json.dumps(c.start()))'
        env=dict(os.environ);env.pop('PYTHONPATH',None)
        result=subprocess.run([sys._base_executable,'-c',script,str(source),json.dumps(self.config.wire()),
                               json.dumps(process_identity(self.owner.pid))],env=env,cwd=self.temp.name,
                              text=True,capture_output=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout)['state'],'running')

    def test_disconnected_and_malformed_local_clients_cannot_kill_collection(self):
        before=self.client.start()
        for data in (b'',b'not-json\n',b'{"command":"stats","session_id":"a"}\n'):
            with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as connection:
                connection.connect(str(self.config.socket));connection.sendall(data)
        time.sleep(.2)
        self.assertEqual(self.client.stats('a')['collector_id'],before['collector_id'])

    def test_stopped_stats_exposes_saved_measurements_without_polling_or_writing(self):
        self.client.start();before=self.client.stats('a');self.client.stop()
        stamp=self.config.db.stat().st_mtime_ns
        after=self.client.stats('a')
        self.assertEqual(after['state'],'stopped')
        self.assertEqual(after['latest_sample_timestamp'],before['latest_sample_timestamp'])
        self.assertEqual(after['windows'][0]['sample'],before['windows'][0]['sample'])
        self.assertEqual(after['sample_count'],before['sample_count'])
        self.assertEqual(self.config.db.stat().st_mtime_ns,stamp)

    def test_restart_offline_retains_last_known_measurement_and_reports_error(self):
        self.client.start();before=self.client.stats('a');self.client.stop()
        (Path(self.temp.name)/'fail').touch()
        self.client.start();after=self.client.stats('a')
        self.assertEqual(after['state'],'error')
        self.assertEqual(after['successful_polls'],0)
        self.assertEqual(after['latest_sample_timestamp'],before['latest_sample_timestamp'])
        self.assertEqual(after['windows'][0]['sample'],before['windows'][0]['sample'])

    def test_stats_connection_closed_during_shutdown_returns_saved_stopped_state(self):
        import threading
        from codex_usage.storage import Store
        from codex_usage.models import UsageSample
        with Store(self.config.db) as store:
            store.add([UsageSample(1800000000,17,window_minutes=300,resets_at=1800010000)])
        self.config.root.mkdir(parents=True)
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as server:
            server.bind(str(self.config.socket));server.listen(1)
            def disappear():
                with server.accept()[0] as connection:connection.recv(4096)
            worker=threading.Thread(target=disappear);worker.start()
            try:
                result=self.client.stats('a')
                self.assertEqual(result['state'],'stopped')
                self.assertEqual(result['windows'][0]['sample']['used_percent'],17)
            finally:worker.join(2)
