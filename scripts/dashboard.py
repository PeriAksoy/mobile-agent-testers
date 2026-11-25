"""The map of an app's screens, on your machine, with the paths of the testers on it.

Usage: python3 scripts/dashboard.py <collected run dir> [<another run dir> ...] [--port 8765]
       python3 scripts/dashboard.py --demo
       python3 scripts/dashboard.py --demo --export site/     # the online demo: static files, no server

Opens a page on http://127.0.0.1:8765 that draws every screen the testers saw as a map:
one card per screen, arrows from one screen to the next as wide as the number of testers,
a heat map of real or estimated taps on each screen. Comment a screen; comments go to
trace.notes.json. Run it on the folders that scripts/collect.sh wrote (runs/<run id>),
for instance a map maker run (briefs/map.md) with a persona run.

Every path is a trace.json in the Explore-Compile-Replay format
(https://github.com/pierridotite/explore-compile-replay), which compiles it to a Maestro
flow that replays with no model. The dashboard only reads traces: it never changes them.

The results are private: the server listens on 127.0.0.1 only, and never serves
trace.params.json (the texts the agent typed).
"""
import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import webbrowser
import xml.etree.ElementTree as ET
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

PAGE = Path(__file__).resolve().parent.parent / "dashboard"
STATIC = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"),
          "/style.css": ("style.css", "text/css")}
SCREEN = re.compile(r"^(\d{1,4})\.(png|svg)$")
MEDIA = {"png": "image/png", "svg": "image/svg+xml"}
NOTES_SCHEMA = "mobile-agent-testers.notes/0"
SAME_SCREEN = 0.6   # Jaccard similarity above which two screenshots show the same screen


# ---------- reading a run ----------

def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def screen_elements(xml_path):
    """Named elements of a saved screen tree: label, id, bounds, clickable. The system bars are left out."""
    try:
        root = ET.parse(xml_path).getroot()
    except (ET.ParseError, OSError):
        return []
    out = []
    for n in root.iter("node"):
        if (n.get("package") or "") == "com.android.systemui":
            continue
        b = re.findall(r"-?\d+", n.get("bounds", ""))
        label = (n.get("text") or "").strip() or (n.get("content-desc") or "").strip()
        rid = (n.get("resource-id") or "").strip()
        if len(b) != 4 or not (label or rid):
            continue
        out.append({"label": label[:120], "id": rid, "bounds": [int(v) for v in b],
                    "clickable": n.get("clickable") == "true"})
    return out


def tokens(elements):
    """What identifies a screen: its ids (the same in every language) and the names of its controls."""
    found = set()
    for e in elements:
        if e["id"]:
            found.add("id:" + e["id"])
        # a control's name, unless it is only a number or a time, which changes from one visit to the next
        if e["clickable"] and e["label"] and len(e["label"]) <= 40 and e["label"].strip("0123456789 :.,%"):
            found.add("label:" + e["label"])
    return found


def similarity(a, b):
    """Jaccard similarity of two screens. On ids alone when both have a few: ids do not change
    with the language, so the same screen in French and in Japanese still matches."""
    ids_a, ids_b = {t for t in a if t.startswith("id:")}, {t for t in b if t.startswith("id:")}
    if len(ids_a) >= 3 and len(ids_b) >= 3:
        a, b = ids_a, ids_b
    return len(a & b) / len(a | b) if a and b else 0.0


def title_of(elements):
    """A short name for a screen: its top-most text that is not a control."""
    texts = [e for e in elements if e["label"] and not e["clickable"] and 2 <= len(e["label"]) <= 40]
    texts.sort(key=lambda e: (e["bounds"][1], e["bounds"][0]))
    return texts[0]["label"] if texts else ""


def group_screens(personas):
    """Gives each screenshot a group: screenshots of the same screen, by any tester, share it.
    A heat map adds up the taps of a group."""
    groups = []   # [tokens of the first screenshot, titles seen, count, screenshots]
    for p in personas:
        for nn, screen in sorted(p["screens"].items()):
            toks = screen.pop("_tokens")
            best, score = None, 0.0
            for i, g in enumerate(groups):
                s = similarity(toks, g[0])
                if s > score:
                    best, score = i, s
            if best is None or score < SAME_SCREEN:
                groups.append([toks, Counter(), 0, []])
                best = len(groups) - 1
            groups[best][2] += 1
            groups[best][3].append({"key": p["key"], "nn": nn})
            if screen["title"]:
                groups[best][1][screen["title"]] += 1
            screen["group"] = f"g{best + 1}"
    # members: every screenshot of the screen; the first one stands for it on the map
    return {f"g{i + 1}": {"title": (g[1].most_common(1)[0][0] if g[1] else ""), "count": g[2], "members": g[3]}
            for i, g in enumerate(groups)}


def find_traces(root):
    """Every trace.json under a collected run, or a single results dir."""
    root = Path(root)
    found = [p for p in sorted(root.rglob("trace.json")) if "node_modules" not in p.parts]
    return found


def as_roots(roots):
    return [Path(r) for r in (roots if isinstance(roots, (list, tuple)) else [roots])]


def key_of(roots, i, folder):
    """A persona's key: its path in the run, prefixed by the run's number when several runs are open."""
    rel = folder.relative_to(roots[i]).as_posix() if folder != roots[i] else "."
    return rel if len(roots) == 1 else f"{i + 1}:{rel}"


def locate(roots, key):
    """The folder a key names, or None. It must sit inside one of the runs."""
    roots = [r.resolve() for r in roots]
    i, rel = 0, key or ""
    if len(roots) > 1:
        head, _, rel = rel.partition(":")
        if not head.isdigit() or not 1 <= int(head) <= len(roots):
            return None
        i = int(head) - 1
    root = roots[i]
    folder = (root / rel).resolve() if rel and rel != "." else root
    if folder != root and root not in folder.parents:
        return None
    return folder


def load_run(roots):
    roots = as_roots(roots)
    personas = []
    for i, root in enumerate(roots):
        for trace_path in find_traces(root):
            load_persona(personas, roots, i, trace_path)
    groups = group_screens(personas)
    return {"run": " + ".join(r.resolve().name for r in roots), "personas": personas, "groups": groups}


def load_persona(personas, roots, i, trace_path):
    folder = trace_path.parent
    trace = read_json(trace_path)
    if not isinstance(trace, dict):
        return
    screens = {}
    for f in sorted(folder.iterdir()):
        m = SCREEN.match(f.name)
        if not m:
            continue
        elements = screen_elements(folder / f"{m.group(1)}.xml")
        screens[m.group(1)] = {"file": f.name, "elements": elements, "title": title_of(elements),
                               "_tokens": tokens(elements)}
        # Optional: where a saliency model predicts the eye goes, [[x, y, weight], ...] in 0..1.
        # The map shows it instead of its own estimate.
        saliency = read_json(folder / f"{m.group(1)}.saliency.json")
        if isinstance(saliency, dict) and isinstance(saliency.get("points"), list):
            screens[m.group(1)]["saliency"] = [pt for pt in saliency["points"]
                                               if isinstance(pt, list) and len(pt) == 3 and all(isinstance(v, (int, float)) for v in pt)][:2000]
    findings = read_json(folder / "findings.json")
    notes = read_json(folder / "trace.notes.json") or {"schema": NOTES_SCHEMA, "screens": {}}
    personas.append({
        "key": key_of(roots, i, folder),
        "trace": trace,
        "notes": notes,
        "findings": findings,
        # the map maker of briefs/map.md explores every screen; it is not a persona
        "mapper": isinstance(findings, dict) and findings.get("mode") == "map",
        "screens": screens,
    })


# ---------- writing ----------

def write_atomic(path, data):
    path = Path(path)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


# ---------- the server ----------

class App:
    def __init__(self, roots):
        self.roots = [r.resolve() for r in as_roots(roots)]
        self.root = self.roots[0]
        self.lock = threading.Lock()

    def folder(self, key):
        """The results dir of a persona, only if it holds a trace and sits inside a run."""
        folder = locate(self.roots, key)
        return folder if folder and (folder / "trace.json").exists() else None


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        server_version = "mobile-agent-testers-dashboard"

        def log_message(self, *args):   # paths can hold app names: keep the terminal quiet
            pass

        def send(self, code, body, kind="application/json"):
            data = body if isinstance(body, bytes) else (json.dumps(body, ensure_ascii=False) if kind == "application/json" else body).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", kind + ("; charset=utf-8" if not kind.startswith("image/png") else ""))
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            url = urlparse(self.path)
            q = parse_qs(url.query)
            if url.path in STATIC:
                name, kind = STATIC[url.path]
                return self.send(200, (PAGE / name).read_bytes(), kind)
            if url.path == "/api/run":
                with app.lock:
                    run = load_run(app.roots)
                return self.send(200, run)
            if url.path == "/api/screen":
                folder = app.folder(q.get("persona", [""])[0])
                name = q.get("name", [""])[0]
                m = SCREEN.match(name)
                if not folder or not m or not (folder / name).is_file():
                    return self.send(404, {"error": "no such screen"})
                return self.send(200, (folder / name).read_bytes(), MEDIA[m.group(2)])
            return self.send(404, {"error": "not found"})

        def do_POST(self):
            # A page of another site cannot send this header without the browser asking first.
            if self.headers.get("X-Dashboard") != "1" or "application/json" not in (self.headers.get("Content-Type") or ""):
                return self.send(403, {"error": "refused"})
            url = urlparse(self.path)
            folder = app.folder(parse_qs(url.query).get("persona", [""])[0])
            if not folder:
                return self.send(404, {"error": "no such persona"})
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"null")
            except ValueError:
                return self.send(400, {"error": "not JSON"})
            if url.path == "/api/notes":
                if not isinstance(body, dict) or not isinstance(body.get("screens", {}), dict) or not isinstance(body.get("steps", {}), dict):
                    return self.send(400, {"error": "notes need a screens object"})
                body["schema"] = NOTES_SCHEMA
                with app.lock:
                    write_atomic(folder / "trace.notes.json", body)
                return self.send(200, {"ok": True})
            return self.send(404, {"error": "not found"})

    return Handler


def export(roots, out):
    """Writes the page and a run as static files, for the online demo. Comments then stay in the
    browser and Save downloads them. Never export a real run: the files are public once hosted."""
    out = Path(out)
    roots = as_roots(roots)
    run = load_run(roots)
    run["static"] = True
    run["run"] = "Demo: a made-up to-do app, its map and three testers"
    (out / "api").mkdir(parents=True, exist_ok=True)
    for name, _ in STATIC.values():
        shutil.copyfile(PAGE / name, out / name)
    for i, p in enumerate(run["personas"]):
        folder = locate(roots, p["key"])
        target = out / "screens" / f"t{i + 1}"
        target.mkdir(parents=True, exist_ok=True)
        for screen in p["screens"].values():
            shutil.copyfile(folder / screen["file"], target / screen["file"])
            screen["url"] = f"screens/t{i + 1}/{screen['file']}"
    for name in ("run", "run.json"):   # "run" is what the page asks for first
        (out / "api" / name).write_text(json.dumps(run, ensure_ascii=False), encoding="utf-8")
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("run", nargs="*", help="collected runs (runs/<run id>), or results dirs. Several: a map run with persona runs")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--demo", action="store_true", help="open a made-up run, to try the page")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--export", metavar="DIR", help="write a static copy for the online demo instead of serving")
    args = parser.parse_args(argv)

    if args.demo:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import dashboard_demo
        roots = [dashboard_demo.write(Path(tempfile.mkdtemp(prefix="dashboard-demo-")))]
    elif args.run:
        roots = [Path(r) for r in args.run]
    else:
        parser.error("give a run dir (runs/<run id>), or --demo")
    for root in roots:
        if not root.is_dir():
            parser.error(f"{root} is not a folder")
    root = roots[0]
    count = sum(len(find_traces(r)) for r in roots)
    if not count:
        print(f"No trace.json under {root}. Traces come from Android testers (scripts/ecr_trace.py).")
        return 1
    if args.export:
        if not args.demo:
            parser.error("--export is for the demo only: a real run is private")
        print(f"Static demo written to {export(roots, args.export)}")
        return 0
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(App(roots)))
    url = f"http://127.0.0.1:{server.server_port}/"
    print(f"{count} paths. Map: {url}  (Ctrl+C to stop)")
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
