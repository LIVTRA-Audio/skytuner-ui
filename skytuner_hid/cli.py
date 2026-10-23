"""Command-line interface for SkyTuner normal-mode HID diagnostics."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional, Sequence, TextIO

from .errors import SkyTunerError
from .framebuffer import save_png
from .hid_trigger import (
    Button,
    DEFAULT_VENDOR_ID,
    LOG_TAIL_CURSOR,
    PROVISIONAL_PRODUCT_ID,
    HidTrigger,
)

UPDATE_TRANSITION_SETTLE_SECONDS = 0.5
BUTTON_ACTION_SETTLE_SECONDS = 0.3


def _usb_id(value: str) -> int:
    try:
        parsed = int(value, 0)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "USB ID must be decimal or 0x-prefixed hexadecimal"
        ) from exc
    if not 0 <= parsed <= 0xFFFF:
        raise argparse.ArgumentTypeError("USB ID must fit in 16 bits")
    return parsed


def _positive_ms(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "milliseconds must be an integer"
        ) from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("milliseconds must be positive")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="skytuner-hid",
        description=(
            "SkyTuner USB connection utilities"
        ),
    )
    parser.add_argument("--vid", type=_usb_id, default=DEFAULT_VENDOR_ID)
    parser.add_argument(
        "--pid", type=_usb_id, default=PROVISIONAL_PRODUCT_ID,
        help="USB product ID (default: 0x4001)",
    )
    parser.add_argument("--serial", help="select one exact USB serial number")
    parser.add_argument(
        "--timeout-ms", type=_positive_ms, default=1000,
        help="overall timeout for one HID transaction",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("info", help="show device identity and firmware")
    logs = commands.add_parser("logs", help="read the bounded RAM log")
    logs.add_argument(
        "--follow", action="store_true",
        help="keep polling for new records until Ctrl-C",
    )
    logs.add_argument(
        "--from-now", action="store_true",
        help="discard retained history and start at the current tail",
    )
    logs.add_argument(
        "--poll-ms", type=_positive_ms, default=100,
        help="follow/retry polling interval",
    )
    commands.add_parser(
        "enter-update",
        help="restart into firmware update mode",
    )
    button = commands.add_parser(
        "button",
        help="operate the physical Display/Tuning buttons or their combo",
    )
    button.add_argument(
        "target", choices=("display", "tuning", "combo"),
    )
    button.add_argument(
        "action", choices=("press", "release", "tap", "long"),
    )
    button.add_argument(
        "--hold-ms", type=_positive_ms,
        help="override tap/long hold duration",
    )
    screenshot = commands.add_parser(
        "screenshot",
        help="capture the current LCD framebuffer as a PNG",
    )
    screenshot.add_argument(
        "output", nargs="?", type=Path, default=Path("skytuner-ui.png"),
        help="output PNG path (default: skytuner-ui.png)",
    )
    return parser


def _run_button(
    device: HidTrigger,
    *,
    target: str,
    action: str,
    hold_ms: Optional[int],
    sleep: Callable[[float], None],
) -> str:
    keepalive_ms = 150
    release_retry_s = 0.05
    release_attempts = 3
    buttons = {
        "display": Button.DISPLAY,
        "tuning": Button.TUNING,
        "combo": Button.COMBO,
    }
    button = buttons[target]

    if action == "press":
        device.set_button(button, True)
        return "pressed (300 ms firmware failsafe)"
    if action == "release":
        device.set_button(button, False)
        return "released"

    if hold_ms is None:
        if action == "long":
            hold_ms = 900 if button is Button.COMBO else 600
        else:
            hold_ms = 100
    remaining_ms = hold_ms
    try:
        # A failed Feature write can still have reached the device before
        # Windows loses the status stage.  Enter the cleanup region before
        # the first press so every attempted tap/hold is followed by an
        # idempotent release instead of relying only on the firmware failsafe.
        device.set_button(button, True)
        while remaining_ms > 0:
            interval_ms = min(keepalive_ms, remaining_ms)
            sleep(interval_ms / 1000.0)
            remaining_ms -= interval_ms
            if remaining_ms > 0:
                device.set_button(button, True)
    finally:
        release_error = None
        for attempt in range(release_attempts):
            try:
                device.set_button(button, False)
                release_error = None
                break
            except Exception as exc:
                release_error = exc
                if attempt + 1 < release_attempts:
                    sleep(release_retry_s)
        if release_error is not None:
            raise release_error
    # Let the UI task publish and begin draining the changed screen before a
    # scripted caller starts another state-changing Feature transaction.
    sleep(BUTTON_ACTION_SETTLE_SECONDS)
    return f"{action} ({hold_ms} ms)"


def _run_logs(
    device: HidTrigger,
    *,
    follow: bool,
    from_now: bool,
    poll_ms: int,
    stdout: TextIO,
    stderr: TextIO,
    sleep: Callable[[float], None],
) -> None:
    cursor: Optional[int] = LOG_TAIL_CURSOR if from_now else None

    while True:
        chunk = device.read_log(cursor)
        if chunk.gap:
            print(
                f"warning: RAM log gap; requested {cursor}, "
                f"resumed at {chunk.start}",
                file=stderr,
            )
        if chunk.data:
            print(chunk.data, file=stdout)
        cursor = chunk.next

        if cursor == chunk.tail:
            if not follow:
                return
            sleep(poll_ms / 1000.0)
        elif not chunk.data:
            # A slot can be observed between invalidation and commit.
            sleep(poll_ms / 1000.0)


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    finder: Callable[..., HidTrigger] = HidTrigger.find,
    stdout: Optional[TextIO] = None,
    stderr: Optional[TextIO] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    args = _parser().parse_args(argv)

    try:
        with finder(
            vendor_id=args.vid,
            product_id=args.pid,
            serial_number=args.serial,
            timeout_ms=args.timeout_ms,
        ) as device:
            if args.command == "info":
                info = device.get_info()
                print(
                    f"{info.model} {info.hardware} firmware "
                    f"{info.firmware} protocol {info.protocol}",
                    file=stdout,
                )
            elif args.command == "logs":
                _run_logs(
                    device,
                    follow=args.follow,
                    from_now=args.from_now,
                    poll_ms=args.poll_ms,
                    stdout=stdout,
                    stderr=stderr,
                    sleep=sleep,
                )
            elif args.command == "enter-update":
                device.enter_update()
                # Keep the HID handle alive briefly after the ACK. Closing it
                # immediately can provoke a Windows USB reset before firmware
                # writes the retained loader flag and restarts.
                sleep(UPDATE_TRANSITION_SETTLE_SECONDS)
                print(
                    "ENTER_UPDATE acknowledged; SkyTuner will reconnect in "
                    "MSC upgrade mode",
                    file=stdout,
                )
            elif args.command == "screenshot":
                framebuffer = device.capture_framebuffer()
                save_png(framebuffer, args.output)
                print(
                    f"saved {framebuffer.width}x{framebuffer.height} "
                    f"framebuffer capture {framebuffer.capture_id} to "
                    f"{args.output}",
                    file=stdout,
                )
            else:
                result = _run_button(
                    device,
                    target=args.target,
                    action=args.action,
                    hold_ms=args.hold_ms,
                    sleep=sleep,
                )
                print(
                    f"{args.target} button {result}",
                    file=stdout,
                )
    except KeyboardInterrupt:
        return 0
    except (SkyTunerError, OSError) as exc:
        print(f"error: {exc}", file=stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
