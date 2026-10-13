import contextlib
import io
import os
from pathlib import Path
import pty
import select
import tempfile
import termios
import threading
import time
import unittest
from unittest.mock import patch
from codex_usage.main import main
from codex_usage.models import UsageSample
from codex_usage.monitor import UsageMonitor
from codex_usage.storage import Store
from codex_usage.tokens import TokenEvent
from codex_usage.estimator import EstimatorConfig
try:
    from codex_usage.console import ConsoleControls
except ImportError:
    ConsoleControls=None

BASE=1800000000

class ConsoleTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(ConsoleControls,'Console key handling is missing')
        self.master,self.slave=pty.openpty()
        self.input=os.fdopen(os.dup(self.slave),'r')
        self.output=os.fdopen(os.dup(self.slave),'w',buffering=1)
        self.original=termios.tcgetattr(self.slave)

    def tearDown(self):
        if hasattr(self,'input'):
            self.input.close();self.output.close();os.close(self.master);os.close(self.slave)

    def assert_restored(self):
        restored=termios.tcgetattr(self.slave)
        expected=list(self.original)
        # macOS sets PENDIN itself on cbreak -> canonical restoration, even
        # using plain stdlib calls. Compare every user-controlled setting.
        restored[3] &= ~termios.PENDIN
        expected[3] &= ~termios.PENDIN
        self.assertEqual(restored,expected)

    def screen_until(self,marker):
        data=b'';deadline=time.monotonic()+2
        while marker not in data and time.monotonic()<deadline:
            if select.select([self.master],[],[],0.05)[0]:data+=os.read(self.master,4096)
        self.assertIn(marker,data)
        return data

    def drive(self,second_key):
        result=[]
        c=ConsoleControls(input=self.input,output=self.output,enabled=True)
        c.open()
        worker=threading.Thread(target=lambda:result.append(c.wait(0.5)))
        worker.start()
        try:
            os.write(self.master,b'r')
            self.screen_until(b'Are you sure?')
            self.assertEqual(result,[]) # r alone never resets.
            os.write(self.master,second_key)
            worker.join(2)
            self.assertFalse(worker.is_alive())
            return result,c
        finally:
            worker.join(2);c.close()

    def test_r_then_enter_confirms_and_restores_terminal(self):
        result,_=self.drive(b'\r')
        self.assertEqual(result,[True])
        self.assert_restored()

    def test_other_key_cancels_without_reset(self):
        result,_=self.drive(b'x')
        self.assertEqual(result,[False])
        self.assert_restored()

    def test_enter_without_r_does_not_reset(self):
        c=ConsoleControls(input=self.input,output=self.output,enabled=True);c.open()
        try:
            os.write(self.master,b'\r')
            self.assertFalse(c.wait(0.05))
        finally:c.close()

    def test_queued_enter_cannot_bypass_secondary_confirmation(self):
        c=ConsoleControls(input=self.input,output=self.output,enabled=True);c.open()
        try:
            os.write(self.master,b'r\r')
            self.assertFalse(c.wait(0.05))
            self.assertTrue(c.confirming)
            os.write(self.master,b'\r')
            self.assertTrue(c.wait(0.1))
        finally:c.close()

    def test_confirmation_survives_poll_deadline(self):
        c=ConsoleControls(input=self.input,output=self.output,enabled=True);c.open()
        try:
            os.write(self.master,b'r')
            self.assertFalse(c.wait(0.05))
            self.assertIn('Are you sure?',c.footer)
            os.write(self.master,b'\n')
            self.assertTrue(c.wait(0.1))
        finally:c.close()

    def test_disabled_or_redirected_controls_leave_terminal_untouched(self):
        for enabled,input,output in [(False,self.input,self.output),(True,io.StringIO('r\n'),io.StringIO())]:
            c=ConsoleControls(input=input,output=output,enabled=enabled);c.open()
            with patch('codex_usage.console.time.sleep') as sleeping:
                self.assertFalse(c.wait(10))
                sleeping.assert_called_once_with(10)
            c.close()
            self.assert_restored()

    def test_exception_restores_tty_attributes(self):
        c=ConsoleControls(input=self.input,output=self.output,enabled=True)
        c.open()
        try:
            with patch('codex_usage.console.select.select',side_effect=KeyboardInterrupt),self.assertRaises(KeyboardInterrupt):c.wait(10)
        finally:c.close()
        self.assert_restored()


class SessionResetTests(unittest.TestCase):
    def test_monitor_session_cutoff_keeps_saved_history_and_other_monitor(self):
        samples=[UsageSample(BASE+i*30,i,window_minutes=300,resets_at=BASE+18000) for i in range(11)]
        class Provider:
            def sample(self):return [samples[-1]]
        with tempfile.TemporaryDirectory() as d,Store(Path(d)/'db') as s:
            s.add(samples)
            config=EstimatorConfig(min_history_minutes=1,min_events=2)
            original=UsageMonitor(Provider(),s,config).tick()[0][1]
            self.assertIsNotNone(original.smoothed_rate)
            reset=UsageMonitor(Provider(),s,config,since=BASE+299).tick()[0][1]
            self.assertIsNone(reset.smoothed_rate)
            self.assertEqual(s.load(),samples)
            self.assertEqual(UsageMonitor(Provider(),s,config).tick()[0][1],original)

    def test_long_response_crossing_reset_is_excluded_despite_idle_cap(self):
        from test_tokens import row,record
        from codex_usage.tokens import TokenReader
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'a.jsonl'
            path.write_text(row(0,'event_msg',{'type':'task_started','turn_id':'turn'})+
                            row(0,'turn_context',{'turn_id':'turn','model':'gpt-6.1-sol'})+record(500,'long'))
            os.utime(path,(BASE+500,BASE+500))
            e=TokenReader(d).read(now=BASE+500)[0][0]
            self.assertEqual(e.started_at,BASE+200)
            self.assertIsNotNone(getattr(e,'response_started_at',None))
            self.assertEqual(e.response_started_at,BASE)
            with Store(Path(d)/'db') as store:
                store.add_token_events([e])
                self.assertEqual(store.load_token_events()[0].response_started_at,BASE)

    def test_legacy_response_boundary_keeps_uncapped_counter_interval(self):
        from test_tokens import row
        from codex_usage.tokens import TokenReader
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'a.jsonl'
            path.write_text(row(0,'event_msg',{'type':'token_count','info':{'total_token_usage':{'total_tokens':100}}})+
                            row(500,'event_msg',{'type':'token_count','info':{'total_token_usage':{'total_tokens':1100}}}))
            os.utime(path,(BASE+500,BASE+500))
            e=TokenReader(d).read(now=BASE+500)[0][0]
            self.assertEqual(e.started_at,BASE+200)
            self.assertEqual(e.response_started_at,BASE)

    def test_watch_reset_excludes_old_and_straddling_responses_without_deleting(self):
        class Controls:
            footer='[r] restart session averages'
            active=True
            def __init__(self,**kwargs):self.calls=0
            def open(self):pass
            def close(self):pass
            def wait(self,delay):self.calls+=1;return self.calls==1
        class Provider:
            credit_balance='100'
            def __init__(self):self.calls=0
            def sample(self):self.calls+=1;return [UsageSample(BASE+(30 if self.calls==1 else 600),self.calls)]
            def close(self):pass
        e=TokenEvent('old',BASE+20,BASE,1000,'native','gpt-6.1-sol',None,900,500,100,0,response_started_at=BASE)
        crossing=TokenEvent('crossing',BASE+50,BASE+20,1000,'native','gpt-6.1-sol',None,900,500,100,0,response_started_at=BASE+20)
        new=TokenEvent('new',BASE+60,BASE+50,1000,'native','gpt-6.1-sol',None,900,500,100,0,response_started_at=BASE+50)
        long=TokenEvent('long',BASE+600,BASE+300,1000,'native','gpt-6.1-sol',None,900,500,100,0,response_started_at=BASE)
        class Tty(io.StringIO):
            def isatty(self):return True
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'db'
            with Store(path) as s:s.add_token_events([e,crossing,new,long]);s.add([UsageSample(BASE,0)])
            out=Tty()
            with patch('codex_usage.main.CodexProvider',return_value=Provider()),patch('codex_usage.main.ConsoleControls',Controls),patch('codex_usage.main.time.time',return_value=BASE+40),contextlib.redirect_stdout(out):
                self.assertEqual(main(['watch','--samples','2','--interval','10','--db',str(path)]),0)
            text=out.getvalue().split('CODEX USAGE')[-1]
            self.assertIn('1 responses;',text)
            self.assertIn('Session averages since:',text)
            with Store(path) as s:
                self.assertEqual(len(s.load_token_events()),4)
                self.assertEqual(len(s.load()),3)
                self.assertEqual(s.load_token_events()[0],e)

if __name__=='__main__':unittest.main()
