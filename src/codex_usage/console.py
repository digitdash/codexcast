"""Confirmed interactive keys while waiting between samples; never delete history."""
import os
import select
import sys
import termios
import time
import tty


class ConsoleControls:
    def __init__(self, *, enabled, input=None, output=None):
        self.input = sys.stdin if input is None else input
        self.output = sys.stdout if output is None else output
        self.enabled = enabled
        self.active = False
        self.confirming = False
        self.fd = None
        self.original = None

    @property
    def footer(self):
        if self.confirming:
            return 'Are you sure? [Enter] restarts session averages; any other key cancels. Saved history is kept.'
        return '[r] Restart session averages (saved history is kept). Ctrl-C to stop.'

    def open(self):
        if not self.enabled or not self.input.isatty() or not self.output.isatty():
            return
        try:
            self.fd = self.input.fileno()
            self.original = termios.tcgetattr(self.fd)
            tty.setcbreak(self.fd, termios.TCSANOW)
            self.active = True
        except (OSError, ValueError, termios.error):
            self.close()

    def close(self):
        try:
            if self.original is not None:
                termios.tcsetattr(self.fd, termios.TCSANOW, self.original)
        finally:
            self.active = False
            self.original = None

    def wait(self, seconds):
        if not self.active:
            time.sleep(seconds)
            return False
        deadline = time.monotonic()+seconds
        while self.active:
            remaining = deadline-time.monotonic()
            if remaining <= 0 or not select.select([self.fd], [], [], remaining)[0]:
                return False
            key = os.read(self.fd, 1)
            if not key:
                self.active = False
                return False
            if self.confirming:
                self.confirming = False
                if key in (b'\r', b'\n'):
                    print('\nSession averages restarted; saved history is kept.',file=self.output,flush=True)
                    return True
                print('\nReset cancelled. '+self.footer,file=self.output,flush=True)
            elif key.lower() == b'r':
                # Only an Enter pressed after the prompt can confirm. Drop pasted
                # or already queued keys; this does not clear persisted telemetry.
                termios.tcflush(self.fd, termios.TCIFLUSH)
                self.confirming = True
                print('\n'+self.footer,file=self.output,flush=True)
        return False
