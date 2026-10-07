"""mcp-relay.py: a dead or missing proxy ends the relay instead of hanging it."""

import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
RELAY = os.path.join(HERE, "..", "mcp-relay.py")


class Relay(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fin = os.path.join(self.tmp.name, "in")
        self.fout = os.path.join(self.tmp.name, "out")
        os.mkfifo(self.fin)
        os.mkfifo(self.fout)

    def run_relay(self, stdin=b"", timeout=30):
        return subprocess.run([sys.executable, RELAY, self.fin, self.fout], input=stdin,
                              capture_output=True, timeout=timeout)

    def test_rejects_a_path_that_is_not_a_fifo(self):
        plain = os.path.join(self.tmp.name, "plain")
        open(plain, "w").close()
        self.fin = plain
        proc = self.run_relay()
        self.assertEqual(proc.returncode, 1)
        self.assertIn(b"not a FIFO", proc.stderr)

    def test_relays_both_ways_with_a_live_proxy(self):
        # A stand-in proxy holding both FIFOs read-write, as the workflow opens them.
        echo = ("import os,sys\n"
                "i=os.open(sys.argv[1],os.O_RDWR); o=os.open(sys.argv[2],os.O_RDWR)\n"
                "os.write(o, os.read(i, 100).upper())\n"
                "import time; time.sleep(30)\n")
        proxy = subprocess.Popen([sys.executable, "-c", echo, self.fin, self.fout])
        self.addCleanup(proxy.kill)
        relay = subprocess.Popen([sys.executable, RELAY, self.fin, self.fout],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        self.addCleanup(relay.kill)
        relay.stdin.write(b"ping\n")
        relay.stdin.flush()
        self.assertEqual(relay.stdout.read(5), b"PING\n")
        relay.stdin.close()
        self.assertEqual(relay.wait(timeout=10), 0)
        relay.stdout.close()

    def test_exits_when_the_proxy_dies(self):
        # The proxy dies after the relay connected: the relay must exit, not
        # block forever reading its stdin.
        die = ("import os,sys,time\n"
               "i=os.open(sys.argv[1],os.O_RDWR); o=os.open(sys.argv[2],os.O_RDWR)\n"
               "time.sleep(1)\n")
        proxy = subprocess.Popen([sys.executable, "-c", die, self.fin, self.fout])
        self.addCleanup(proxy.kill)
        r, w = os.pipe()  # a stdin that never closes
        self.addCleanup(os.close, w)
        relay = subprocess.Popen([sys.executable, RELAY, self.fin, self.fout], stdin=r)
        os.close(r)
        self.addCleanup(relay.kill)
        self.assertEqual(relay.wait(timeout=15), 1)


if __name__ == "__main__":
    unittest.main()
