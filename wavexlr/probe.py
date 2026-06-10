"""Vendor protocol probe — verify a device behaves like the known profiles.

    python3 -m wavexlr.probe dump [--wvalue 0xN] [--len 512]
    python3 -m wavexlr.probe watch [--interval 0.1]
    python3 -m wavexlr.probe poke --offset N --byte 0xNN
    python3 -m wavexlr.probe poke --noop

dump with no --wvalue reads the profile's config/meter/devinfo blocks and
reports the actual returned lengths. watch polls the config block and prints
per-offset byte diffs while you twiddle the hardware controls.
"""

import argparse
import sys
import time

from .device import WaveDevice


def _hexdump(data):
    for i in range(0, len(data), 16):
        chunk = data[i:i + 16]
        hexs = " ".join(f"{b:02x}" for b in chunk)
        text = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        print(f"  {i:04x}  {hexs:<47}  {text}")


def _read(dev, wvalue, length):
    try:
        data = dev._ctrl_read(wvalue, length)
    except RuntimeError as e:
        print(f"wValue 0x{wvalue:04X}: {e}")
        return None
    print(f"wValue 0x{wvalue:04X}: {len(data)} bytes")
    _hexdump(data)
    return data


def cmd_dump(dev, args):
    if args.wvalue is not None:
        _read(dev, args.wvalue, args.len)
        return
    p = dev.profile
    for name, wvalue, expected in (
        ("config", p.wvalue_config, p.config_len),
        ("meter", p.wvalue_meter, p.meter_len),
        ("devinfo", p.wvalue_devinfo, p.devinfo_len),
    ):
        print(f"-- {name} (expected {expected} bytes)")
        data = _read(dev, wvalue, args.len)
        if data is not None and len(data) != expected:
            print(f"   DEVIATION: expected {expected} bytes, got {len(data)}")


def cmd_watch(dev, args):
    last = dev.read_config()
    print(f"config: {len(last)} bytes — twiddle controls, Ctrl+C to stop")
    _hexdump(last)
    while True:
        time.sleep(args.interval)
        cur = dev.read_config()
        if cur != last:
            ts = time.strftime("%H:%M:%S")
            for off, (a, b) in enumerate(zip(last, cur)):
                if a != b:
                    print(f"{ts}  off {off:2d}: {a:02x} -> {b:02x}")
            last = cur


def cmd_poke(dev, args):
    config = dev.read_config()
    if args.noop:
        dev.write_config(config)
        print(f"wrote back {len(config)} bytes unchanged")
        return
    if args.offset is None or args.byte is None:
        sys.exit("poke needs --offset and --byte (or --noop)")
    old = config[args.offset]
    print(f"off {args.offset}: {old:02x} -> {args.byte:02x}")
    if not args.yes and input("write? [y/N] ").strip().lower() != "y":
        return
    config[args.offset] = args.byte
    dev.write_config(config)
    _hexdump(dev.read_config())


def main():
    parser = argparse.ArgumentParser(prog="python3 -m wavexlr.probe")
    sub = parser.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("dump")
    d.add_argument("--wvalue", type=lambda x: int(x, 0))
    d.add_argument("--len", type=lambda x: int(x, 0), default=512)

    w = sub.add_parser("watch")
    w.add_argument("--interval", type=float, default=0.1)

    k = sub.add_parser("poke")
    k.add_argument("--offset", type=lambda x: int(x, 0))
    k.add_argument("--byte", type=lambda x: int(x, 0))
    k.add_argument("--noop", action="store_true")
    k.add_argument("--yes", action="store_true")

    args = parser.parse_args()
    sys.stdout.reconfigure(line_buffering=True)

    dev = WaveDevice()
    try:
        dev.connect()
    except RuntimeError as e:
        sys.exit(str(e))
    print(f"connected: {dev.profile.display_name} (card {dev._card})")

    try:
        {"dump": cmd_dump, "watch": cmd_watch, "poke": cmd_poke}[args.cmd](dev, args)
    except KeyboardInterrupt:
        pass
    finally:
        dev.disconnect()


if __name__ == "__main__":
    main()
