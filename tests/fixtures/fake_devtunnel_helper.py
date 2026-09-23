import argparse
import sys


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tunnel-id", required=True)
    parser.add_argument("--remote-port", required=True, type=int)
    parser.add_argument("--local-port", required=True, type=int)
    parser.add_argument("--connect-timeout", type=int, default=70)
    parser.add_argument("--fail")
    args = parser.parse_args()
    if args.tunnel_id == "fail":
        print("ERROR authentication failed", flush=True)
        return 2
    print(
        f"READY local=127.0.0.1:{args.local_port} tunnel={args.tunnel_id} port={args.remote_port}",
        flush=True,
    )
    for line in sys.stdin:
        if line.strip() == "STOP":
            return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
