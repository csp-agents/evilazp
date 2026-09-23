import argparse
import sys
import time


parser = argparse.ArgumentParser()
parser.add_argument("--connection-string", required=True)
parser.add_argument("--local-forward", action="append", default=[])
parser.add_argument("--remote-forward", action="append", default=[])
parser.add_argument("--remote-http-forward", action="append", default=[])
args = parser.parse_args()

if "fail=true" in args.connection_string:
    print("ERROR authentication failed for [connection-string]", flush=True)
    sys.exit(3)

print("READY relay bridge active", flush=True)
for line in sys.stdin:
    if line.strip().upper() == "STOP":
        sys.exit(0)
while True:
    time.sleep(1)
