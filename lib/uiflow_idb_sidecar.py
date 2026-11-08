"""Runs under the managed fb-idb venv, never under the skill's own interpreter.

Holds one live gRPC connection to idb_companion and serves newline-delimited JSON batches over a
unix socket. The connection is the whole point: a single `idb ui tap` process costs ~2s, while a
tap on a held connection costs ~2ms.
"""

import argparse
import asyncio
import json
import logging
import os
import time

from idb.common.types import (
    AccessibilityInfoOptions,
    AccessibilityPoint,
    HIDDelay,
    HIDDirection,
    HIDPress,
    HIDTouch,
    Point,
    TCPAddress,
)
from idb.grpc.client import Client


class StepFailed(Exception):
    def __init__(self, reason, detail=""):
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail


def _point(step, x_key="x", y_key="y"):
    x, y = step.get(x_key), step.get(y_key)
    if x is None or y is None:
        raise StepFailed("badStep", "%s needs %s and %s" % (step.get("op"), x_key, y_key))
    return float(x), float(y)


NORMALIZED_KEY = {"x": "nx", "y": "ny", "fromX": "fromNx", "fromY": "fromNy",
                  "toX": "toNx", "toY": "toNy"}


def needs_screen(step):
    if any(step.get(key) is not None for key in NORMALIZED_KEY.values()):
        return True
    return any(isinstance(point, dict) for point in (step.get("points") or []))


def _axis(step, absolute_key, normalized_key, extent):
    raw, fraction = step.get(absolute_key), step.get(normalized_key)
    if raw is not None and fraction is not None:
        raise StepFailed("badStep",
                         "%s got both %s and %s" % (step.get("op"), absolute_key, normalized_key))
    if raw is not None:
        return float(raw)
    if fraction is None:
        return None
    if extent is None:
        raise StepFailed("noScreen",
                         "%s is normalized but the screen size is unknown" % normalized_key)
    value = float(fraction)
    if not 0.0 <= value <= 1.0:
        raise StepFailed("badStep", "%s must be between 0 and 1, got %s" % (normalized_key, value))
    return value * float(extent)


def resolve_point(step, screen, x_key="x", y_key="y"):
    x = _axis(step, x_key, NORMALIZED_KEY[x_key], (screen or {}).get("w"))
    y = _axis(step, y_key, NORMALIZED_KEY[y_key], (screen or {}).get("h"))
    if x is None or y is None:
        raise StepFailed("badStep", "%s needs %s and %s" % (step.get("op"), x_key, y_key))
    return x, y


def burst_points(step, screen):
    points = step.get("points")
    if points:
        return [resolve_point(dict(point, op="tapBurst"), screen) if isinstance(point, dict)
                else (float(point[0]), float(point[1])) for point in points]
    key_map = step.get("keyMap") or {}
    keys = step.get("keys")
    if keys is None:
        raise StepFailed("badStep", "tapBurst needs points or keys")
    resolved = []
    for key in keys:
        at = key_map.get(key)
        if at is None:
            raise StepFailed("notFound", "no keyMap entry for %r" % key)
        resolved.append((float(at[0]), float(at[1])))
    return resolved


def _press(x, y, hold, gap):
    at = Point(x=x, y=y)
    return [
        HIDPress(action=HIDTouch(point=at), direction=HIDDirection.DOWN),
        HIDDelay(duration=hold),
        HIDPress(action=HIDTouch(point=at), direction=HIDDirection.UP),
        HIDDelay(duration=gap),
    ]


async def _stream(client, events):
    async def generate():
        for event in events:
            yield event

    await client.hid(generate())


async def _ax_at(client, x, y):
    info = await client.accessibility_info(
        target=AccessibilityPoint(x=float(x), y=float(y)), options=AccessibilityInfoOptions()
    )
    return json.loads(info.json)


class Sidecar:
    def __init__(self, client):
        self.client = client
        self.should_quit = False
        self.screen = None
        self.screen_at = 0.0

    async def screen_size(self, step=None):
        if step is not None and not needs_screen(step):
            return None
        # Re-read periodically: this process outlives a rotation, which changes every fraction.
        if self.screen is None or time.time() - self.screen_at > 5.0:
            target = await self.client.describe()
            size = getattr(target, "screen_dimensions", None)
            if size:
                density = size.density or 1.0
                self.screen = {"w": size.width / density, "h": size.height / density,
                               "density": size.density}
                self.screen_at = time.time()
        return self.screen

    async def run(self, step):
        op = step.get("op")
        handler = getattr(self, "op_" + str(op), None)
        if handler is None:
            raise StepFailed("badStep", "unknown op %r" % op)
        return await handler(step)

    async def op_ping(self, step):
        return {}

    async def op_quit(self, step):
        self.should_quit = True
        return {}

    async def op_tap(self, step):
        x, y = resolve_point(step, await self.screen_size(step))
        duration = step.get("durationMs")
        await self.client.tap(x, y, duration=(float(duration) / 1000.0) if duration else None)
        return {}

    async def op_tapBurst(self, step):
        points = burst_points(step, await self.screen_size(step))
        hold = float(step.get("holdMs", 4)) / 1000.0
        gap = float(step.get("gapMs", 4)) / 1000.0
        events = []
        for x, y in points:
            events.extend(_press(x, y, hold, gap))
        started = time.time()
        await _stream(self.client, events)
        elapsed = max(1.0, (time.time() - started) * 1000.0)
        return {
            "taps": len(points),
            "ms": round(elapsed),
            "cpm": round(len(points) * 60000.0 / elapsed),
        }

    async def op_swipe(self, step):
        screen = await self.screen_size(step)
        x1, y1 = resolve_point(step, screen, "fromX", "fromY")
        x2, y2 = resolve_point(step, screen, "toX", "toY")
        duration = step.get("durationMs")
        await self.client.swipe(
            p_start=(x1, y1),
            p_end=(x2, y2),
            duration=(float(duration) / 1000.0) if duration else None,
        )
        return {}

    async def op_path(self, step):
        points = step.get("points")
        if not points:
            raise StepFailed("badStep", "path needs points")
        events = []
        for point in points:
            if len(point) < 2:
                raise StepFailed("badStep", "each path point needs x and y")
            at = Point(x=float(point[0]), y=float(point[1]))
            events.append(HIDPress(action=HIDTouch(point=at), direction=HIDDirection.DOWN))
            hold = float(point[2]) / 1000.0 if len(point) > 2 else 0.008
            if hold > 0:
                events.append(HIDDelay(duration=hold))
        last = points[-1]
        events.append(
            HIDPress(
                action=HIDTouch(point=Point(x=float(last[0]), y=float(last[1]))),
                direction=HIDDirection.UP,
            )
        )
        started = time.time()
        await _stream(self.client, events)
        return {"points": len(points), "ms": round(max(1.0, (time.time() - started) * 1000.0))}

    async def op_tapSeq(self, step):
        taps = step.get("taps")
        if not taps:
            raise StepFailed("badStep", "tapSeq needs taps")
        hold = float(step.get("holdMs", 4)) / 1000.0
        events = []
        for tap in taps:
            if len(tap) < 2:
                raise StepFailed("badStep", "each tap needs x and y")
            gap = float(tap[2]) / 1000.0 if len(tap) > 2 else 0.004
            events.extend(_press(float(tap[0]), float(tap[1]), hold, gap))
        started = time.time()
        await _stream(self.client, events)
        elapsed = max(1.0, (time.time() - started) * 1000.0)
        return {"taps": len(taps), "ms": round(elapsed), "cpm": round(len(taps) * 60000.0 / elapsed)}

    async def op_ops(self, step):
        return {"ops": sorted(name[3:] for name in dir(self) if name.startswith("op_"))}

    async def op_text(self, step):
        text = step.get("text")
        if text is None:
            raise StepFailed("badStep", "text needs a text")
        await self.client.text(text)
        return {"chars": len(text)}

    async def op_tree(self, step):
        info = await self.client.accessibility_info(
            target=None, options=AccessibilityInfoOptions(nested=bool(step.get("nested")))
        )
        return {"tree": json.loads(info.json)}

    async def op_value(self, step):
        x, y = _point(step)
        data = await _ax_at(self.client, x, y)
        if isinstance(data, list):
            data = data[0] if data else {}
        return {"value": data.get("AXValue"), "label": data.get("AXLabel"), "role": data.get("role")}

    async def op_assertValue(self, step):
        x, y = _point(step)
        wanted = step.get("equals")
        deadline = time.time() + float(step.get("timeoutMs", 2000)) / 1000.0
        seen = None
        while True:
            data = await _ax_at(self.client, x, y)
            if isinstance(data, list):
                data = data[0] if data else {}
            seen = data.get("AXValue")
            if seen == wanted:
                return {"value": seen}
            if time.time() >= deadline:
                raise StepFailed("valueMismatch", "wanted %r, saw %r" % (wanted, seen))
            await asyncio.sleep(0.05)

    async def op_key(self, step):
        code = step.get("code")
        if code is None:
            raise StepFailed("badStep", "key needs a code")
        await self.client.key(int(code), duration=None)
        return {}

    async def op_describe(self, step):
        screen = await self.screen_size()
        if not screen:
            raise StepFailed("notFound", "target reports no screen dimensions")
        return {"width": screen["w"], "height": screen["h"], "density": screen["density"]}

    async def op_screenshot(self, step):
        image = await self.client.screenshot()
        path = step.get("path") or os.path.join(
            os.environ.get("TMPDIR", "/tmp"), "uiflow-idb-%d.png" % int(time.time() * 1000)
        )
        with open(path, "wb") as handle:
            handle.write(image)
        return {"path": path, "bytes": len(image)}

    async def op_launch(self, step):
        app = step.get("app")
        if not app:
            raise StepFailed("badStep", "launch needs an app")
        await self.client.launch(app, foreground_if_running=True)
        return {}

    async def op_terminate(self, step):
        app = step.get("app")
        if not app:
            raise StepFailed("badStep", "terminate needs an app")
        try:
            await self.client.terminate(app)
        except Exception as error:
            return {"note": str(error)[:120]}
        return {}

    async def op_appState(self, step):
        app = step.get("app")
        apps = await self.client.list_apps(fetch_process_state=True)
        for entry in apps:
            if entry.bundle_id == app:
                return {"state": str(entry.process_state).split(".")[-1]}
        raise StepFailed("notFound", "no installed app %r" % app)

    async def op_sleep(self, step):
        await asyncio.sleep(float(step.get("ms", 0)) / 1000.0)
        return {}


async def handle(sidecar, reader, writer):
    line = await reader.readline()
    if not line:
        writer.close()
        return
    try:
        request = json.loads(line.decode("utf-8", "replace"))
    except ValueError:
        writer.write(b'{"ok": false, "error": {"reason": "badRequest"}}\n')
        await writer.drain()
        writer.close()
        return
    ops = request.get("ops") or []
    results = []
    reply = {"ok": True, "results": results}
    for index, step in enumerate(ops):
        step = {"op": step} if isinstance(step, str) else dict(step)
        started = time.time()
        try:
            outcome = await sidecar.run(step)
        except StepFailed as error:
            reply["ok"] = False
            reply["error"] = {"index": index, "op": step.get("op"), "reason": error.reason, "detail": error.detail}
            break
        except Exception as error:  # a gRPC failure must not take the sidecar down
            reply["ok"] = False
            reply["error"] = {
                "index": index,
                "op": step.get("op"),
                "reason": type(error).__name__,
                "detail": str(error)[:400],
            }
            break
        outcome = dict(outcome or {})
        outcome.setdefault("op", step.get("op"))
        outcome.setdefault("ms", round((time.time() - started) * 1000))
        results.append(outcome)
    writer.write((json.dumps(reply, ensure_ascii=False) + "\n").encode())
    await writer.drain()
    writer.close()


async def serve(udid, port, path):
    logging.basicConfig(level=logging.ERROR)
    logger = logging.getLogger("uiflow.idb")
    try:
        os.remove(path)
    except OSError:
        pass
    async with Client.build(address=TCPAddress(host="localhost", port=port), logger=logger) as client:
        sidecar = Sidecar(client)
        stop = asyncio.Event()

        async def on_client(reader, writer):
            await handle(sidecar, reader, writer)
            if sidecar.should_quit:
                stop.set()

        server = await asyncio.start_unix_server(on_client, path=path)
        async with server:
            await stop.wait()
    try:
        os.remove(path)
    except OSError:
        pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--udid", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--socket", required=True)
    args = parser.parse_args()
    asyncio.run(serve(args.udid, args.port, args.socket))


if __name__ == "__main__":
    main()
