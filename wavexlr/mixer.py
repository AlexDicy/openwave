"""Audio mixer — manages pw-loopback subprocesses for the matrix.

A loopback exists for each non-zero cell in the matrix (mic → mix), plus one
that always routes Personal Mix → Wave XLR headphones so the user hears
anything routed there. Volume + mute per cell are pushed onto the loopback's
playback node via wpctl.

State is persisted to ~/.config/openwave/mixes.json so per-cell levels survive
restarts (the loopbacks themselves do not — they're respawned by start()).
"""

import atexit
import ctypes
import json
import os
import signal
import subprocess
import time
from threading import Lock

# Linux-only: make spawned children receive SIGTERM if our process dies.
# Survives SIGKILL on the parent, hard crashes, anything that skips Python
# cleanup paths. Without this, pw-loopback children leak on unclean exit.
_PR_SET_PDEATHSIG = 1
try:
    _libc = ctypes.CDLL("libc.so.6", use_errno=True)
    _libc.prctl.argtypes = (
        ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong,
    )
    _libc.prctl.restype = ctypes.c_int
except (OSError, AttributeError):
    _libc = None


def _set_pdeathsig():
    if _libc is not None:
        _libc.prctl(_PR_SET_PDEATHSIG, int(signal.SIGTERM), 0, 0, 0)

CONFIG_PATH = os.path.expanduser("~/.config/openwave/mixes.json")

MIX_SINKS = {
    "personal": "openwave_personal_mix",
    "chat":     "openwave_chat_mix",
    "record":   "openwave_record_mix",
}
PERSONAL_MIX_SINK = "openwave_personal_mix"
HP_LOOPBACK_KEY = "_personal_to_hp"
HP_LOOPBACK_NODE = "openwave_loop_personal_to_hp"


def _pactl_short(kind):
    try:
        r = subprocess.run(
            ["pactl", "list", "short", kind],
            capture_output=True, text=True, timeout=3,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return []
    return [line.split("\t") for line in r.stdout.splitlines() if line.strip()]


def find_wave_xlr_alsa():
    """Return (mic_node_name, hp_node_name); either may be None if unplugged."""
    mic = next(
        (p[1] for p in _pactl_short("sources")
         if len(p) > 1 and p[1].startswith("alsa_input") and "Wave_XLR" in p[1]),
        None,
    )
    hp = next(
        (p[1] for p in _pactl_short("sinks")
         if len(p) > 1 and p[1].startswith("alsa_output") and "Wave_XLR" in p[1]),
        None,
    )
    return mic, hp


def _node_id_by_name(name, retries=20):
    """Look up a PipeWire node's global id by node.name, polling briefly so
    we don't race a just-spawned pw-loopback. Returns None if not found."""
    for _ in range(retries):
        try:
            r = subprocess.run(
                ["pw-cli", "ls", "Node"],
                capture_output=True, text=True, timeout=3,
            )
        except (FileNotFoundError, subprocess.SubprocessError):
            return None
        current_id = None
        for raw in r.stdout.splitlines():
            line = raw.strip()
            if line.startswith("id "):
                try:
                    current_id = line.split()[1].rstrip(",")
                except (IndexError, ValueError):
                    current_id = None
            elif current_id and line == f'node.name = "{name}"':
                return current_id
        time.sleep(0.05)
    return None


def _wpctl(*args):
    try:
        subprocess.run(
            ["wpctl", *args],
            capture_output=True, text=True, timeout=3,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        pass


def list_audio_streams():
    """Return [{id, app_name, media_name, node_name}, ...] for active output streams."""
    import json as _json
    try:
        r = subprocess.run(
            ["pw-dump"], capture_output=True, text=True, timeout=5,
        )
        if r.returncode != 0:
            return []
        objects = _json.loads(r.stdout)
    except (FileNotFoundError, subprocess.SubprocessError, _json.JSONDecodeError):
        return []

    out = []
    for obj in objects:
        if obj.get("type") != "PipeWire:Interface:Node":
            continue
        props = (obj.get("info") or {}).get("props") or {}
        if props.get("media.class") != "Stream/Output/Audio":
            continue
        app = props.get("application.name") or props.get("node.name") or "Unknown"
        # Skip our own loopbacks
        node_name = props.get("node.name", "")
        if node_name.startswith("openwave_"):
            continue
        out.append({
            "id": obj["id"],
            "app_name": app,
            "media_name": props.get("media.name", ""),
            "node_name": node_name,
            "binary": props.get("application.process.binary", ""),
        })
    return out


class Mixer:
    """Manages pw-loopback subprocesses for the matrix's mic row."""

    def __init__(self):
        self._lock = Lock()
        self._procs = {}
        self._state = self._load_state()
        self._sources = {}
        self._streams = {}
        self.mic, self.hp = find_wave_xlr_alsa()
        # Belt-and-suspenders: even if do_shutdown is skipped, the interpreter
        # almost always runs atexit before the process image goes away.
        atexit.register(self._atexit_cleanup)

    # ----- persistence -----
    def _load_state(self):
        try:
            with open(CONFIG_PATH) as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save_state(self):
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self._state, f, indent=2)
        os.replace(tmp, CONFIG_PATH)

    def get_cell(self, source_id, mix_id):
        return self._state.get(
            f"{source_id}.{mix_id}", {"volume": 0.0, "muted": False}
        )

    def cells(self):
        return dict(self._state)

    # ----- subprocess lifecycle -----
    def _spawn_loopback(self, key, capture_target, playback_target, node_name):
        if key in self._procs:
            return
        try:
            proc = subprocess.Popen(
                [
                    "pw-loopback",
                    f"--capture-props=target.object={capture_target}",
                    f"--playback-props=target.object={playback_target} node.name={node_name}",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                preexec_fn=_set_pdeathsig,
            )
        except (FileNotFoundError, OSError):
            return
        self._procs[key] = proc

    def _destroy_loopback(self, key):
        proc = self._procs.pop(key, None)
        if proc is None:
            return
        try:
            proc.terminate()
        except (OSError, ProcessLookupError):
            return
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
                proc.wait(timeout=1)
            except (OSError, ProcessLookupError, subprocess.TimeoutExpired):
                pass

    def _atexit_cleanup(self):
        """Fast best-effort tear-down on interpreter exit. No locking, no waits."""
        for proc in list(self._procs.values()):
            try:
                proc.terminate()
            except (OSError, ProcessLookupError):
                continue
        self._procs.clear()

    # ----- public API -----
    def start(self):
        """Spawn always-on Personal→HP loopback, snapshot streams, restore cells.

        First, sweep stale pw-loopback children from prior runs that didn't shut
        down cleanly (kill -9, crash, log-out before atexit) — without this they
        accumulate across launches and silently produce audio feedback.
        """
        self._sweep_stale_loopbacks()
        with self._lock:
            if self.hp:
                self._spawn_loopback(
                    HP_LOOPBACK_KEY, PERSONAL_MIX_SINK, self.hp, HP_LOOPBACK_NODE,
                )
            self._streams = {s["id"]: s for s in list_audio_streams()}
            self._reconcile_all()

    @staticmethod
    def _sweep_stale_loopbacks():
        try:
            subprocess.run(
                ["pkill", "-f", "pw-loopback.*openwave_loop_"],
                capture_output=True, timeout=2,
            )
        except (FileNotFoundError, subprocess.SubprocessError):
            return
        time.sleep(0.2)  # give the kernel a beat to reap so we don't race

    def stop(self):
        with self._lock:
            for key in list(self._procs.keys()):
                self._destroy_loopback(key)

    # Below this, the slider snaps to 0 — sub-1% values keep the loopback alive
    # at imperceptible-but-not-silent volume and confuse "I put it back to 0".
    _ZERO_THRESHOLD = 0.01

    def set_cell(self, source_id, mix_id, volume, muted):
        volume = max(0.0, min(1.0, float(volume)))
        if volume < self._ZERO_THRESHOLD:
            volume = 0.0
        with self._lock:
            self._state[f"{source_id}.{mix_id}"] = {
                "volume": volume, "muted": bool(muted),
            }
            self._save_state()
            self._reconcile_cell(source_id, mix_id)

    def set_sources(self, sources):
        """Update the app-source configuration; reconciles loopbacks."""
        with self._lock:
            self._sources = dict(sources)
            self._reconcile_all()

    def remove_source(self, source_id):
        """Tear down all loopbacks for a source and forget its persisted cells."""
        with self._lock:
            for key in list(self._procs.keys()):
                if isinstance(key, tuple) and key and key[0] == source_id:
                    self._destroy_loopback(key)
            prefix = f"{source_id}."
            for cell_key in [k for k in self._state if k.startswith(prefix)]:
                del self._state[cell_key]
            self._save_state()
            self._sources.pop(source_id, None)

    def poll_streams(self):
        """Refresh the active-stream cache and adjust loopbacks. Returns the
        diff (added, removed) of stream ids for the caller's bookkeeping."""
        new = {s["id"]: s for s in list_audio_streams()}
        with self._lock:
            added = set(new) - set(self._streams)
            removed = set(self._streams) - set(new)
            self._streams = new
            if added or removed:
                self._reconcile_all()
        return added, removed

    # ----- internal -----
    def _reconcile_all(self):
        for source_id in (["mic"] + list(self._sources.keys())):
            for mix_id in MIX_SINKS:
                self._reconcile_cell(source_id, mix_id)

    def _reconcile_cell(self, source_id, mix_id):
        state = self._state.get(
            f"{source_id}.{mix_id}", {"volume": 0.0, "muted": False}
        )
        if source_id == "mic":
            self._reconcile_mic_cell(mix_id, state["volume"], state["muted"])
        else:
            self._reconcile_app_cell(source_id, mix_id, state["volume"], state["muted"])

    def _reconcile_mic_cell(self, mix_id, volume, muted):
        if not self.mic:
            return
        mix_sink = MIX_SINKS.get(mix_id)
        if not mix_sink:
            return
        key = ("mic", mix_id)
        node_name = f"openwave_loop_mic_to_{mix_id}"
        if volume <= 0.0:
            self._destroy_loopback(key)
            return
        if key not in self._procs:
            self._spawn_loopback(key, self.mic, mix_sink, node_name)
        node_id = _node_id_by_name(node_name)
        if node_id is not None:
            _wpctl("set-volume", node_id, f"{volume:.3f}")
            _wpctl("set-mute", node_id, "1" if muted else "0")

    def _reconcile_app_cell(self, source_id, mix_id, volume, muted):
        source = self._sources.get(source_id)
        if not source:
            return
        mix_sink = MIX_SINKS.get(mix_id)
        if not mix_sink:
            return
        match = source.get("match_app_name")
        matching_stream_ids = {
            sid for sid, s in self._streams.items() if s.get("app_name") == match
        }
        existing_keys = {
            k for k in self._procs
            if len(k) == 3 and k[0] == source_id and k[1] == mix_id
        }

        # Tear down loopbacks for streams that vanished or for a zeroed cell
        for k in list(existing_keys):
            if volume <= 0.0 or k[2] not in matching_stream_ids:
                self._destroy_loopback(k)

        if volume <= 0.0:
            return

        # Spawn (or update volume on) loopbacks for each currently-matching stream
        for stream_id in matching_stream_ids:
            key = (source_id, mix_id, stream_id)
            node_name = f"openwave_loop_{source_id}_{mix_id}_{stream_id}"
            if key not in self._procs:
                self._spawn_loopback(key, str(stream_id), mix_sink, node_name)
            node_id = _node_id_by_name(node_name)
            if node_id is not None:
                _wpctl("set-volume", node_id, f"{volume:.3f}")
                _wpctl("set-mute", node_id, "1" if muted else "0")
