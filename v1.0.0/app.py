"""
Roblox DataStore Copier
Salin entry DataStore dari experience (game) 1 ke experience (game) 2 lewat Open Cloud.
Game 1 hanya dibaca, tidak ada yang dihapus atau diubah.

Jalankan di komputermu sendiri:
    pip install -r requirements.txt
    python app.py
Browser terbuka otomatis. Atau buka http://127.0.0.1:5000

Opsi:
    python app.py --verbose      tampilkan log request (GET/POST) di terminal
    python app.py --no-browser   jangan buka browser otomatis
    python app.py --port 8080    ganti port

API key hanya dipakai di memori selama proses berjalan dan tidak disimpan ke disk.
"""
import argparse
import base64
import hashlib
import logging
import threading
import time
import webbrowser

import requests
from flask import Flask, jsonify, request, Response
from flask import cli as flask_cli

# Nama yang ditampilkan di halaman kredit.
AUTHOR = "Fari Noveri"
VERSION = "1.0.0"

app = Flask(__name__)
state = {"silent": True}  # True = log request di terminal dimatikan


def apply_silence():
    logging.getLogger("werkzeug").setLevel(logging.ERROR if state["silent"] else logging.INFO)

API = "https://apis.roblox.com/datastores/v1/universes/{}/standard-datastores/datastore/entries"
LIST_STORES = "https://apis.roblox.com/datastores/v1/universes/{}/standard-datastores"

job = {"state": "idle", "done": 0, "copied": 0, "skipped": 0, "failed": 0, "dry": False, "logs": []}
stop_flag = threading.Event()
lock = threading.Lock()


class Stopped(Exception):
    pass


def log(msg):
    with lock:
        job["logs"].append(time.strftime("%H:%M:%S ") + msg)
        del job["logs"][:-300]


def bump(field):
    with lock:
        job[field] += 1


def transform(key: str, value: bytes) -> bytes:
    """Ubah data di sini kalau format game 2 berbeda.
    `value` berisi JSON mentah (bytes). Kembalikan bytes JSON yang baru.
    Contoh:
        import json
        d = json.loads(value)
        d["Coins"] = d.pop("Money", 0)
        return json.dumps(d).encode()
    """
    return value


def call(method, url, **kw):
    for attempt in range(8):
        if stop_flag.is_set():
            raise Stopped()
        r = requests.request(method, url, timeout=30, **kw)
        if r.status_code == 429:
            try:
                wait = int(r.headers.get("retry-after", 5))
            except ValueError:
                wait = 5
            log(f"Kena rate limit, menunggu {wait} detik...")
            time.sleep(wait)
            continue
        if r.status_code >= 500:
            time.sleep(2 ** attempt)
            continue
        return r
    raise RuntimeError("Terlalu banyak percobaan ulang ke Roblox")


def iter_keys(cfg):
    cursor = ""
    while True:
        params = {
            "datastoreName": cfg["src_store"],
            "scope": cfg["scope"],
            "limit": 100,
            "cursor": cursor,
        }
        if cfg["prefix"]:
            params["prefix"] = cfg["prefix"]
        r = call("GET", API.format(cfg["src_universe"]), headers={"x-api-key": cfg["src_key"]}, params=params)
        if not r.ok:
            raise RuntimeError(f"Gagal membaca daftar key ({r.status_code}): {r.text[:300]}")
        data = r.json()
        for k in data.get("keys", []):
            yield k["key"]
        cursor = data.get("nextPageCursor")
        if not cursor:
            return


def worker(cfg):
    try:
        src_url = API.format(cfg["src_universe"]) + "/entry"
        dst_url = API.format(cfg["dst_universe"]) + "/entry"
        mode = "SIMULASI" if cfg["dry_run"] else "SALIN"
        log(f"Mulai {mode}. Store: {cfg['src_store']} -> {cfg['dst_store']}")

        for key in iter_keys(cfg):
            if stop_flag.is_set():
                raise Stopped()
            if cfg["limit"] and job["done"] >= cfg["limit"]:
                log(f"Batas {cfg['limit']} key tercapai.")
                break
            try:
                r = call("GET", src_url, headers={"x-api-key": cfg["src_key"]},
                         params={"datastoreName": cfg["src_store"], "scope": cfg["scope"], "entryKey": key})
                if r.status_code == 404:
                    bump("skipped")
                    log(f"[lewat] {key}: tidak ditemukan di game 1")
                elif not r.ok:
                    bump("failed")
                    log(f"[gagal] {key}: baca {r.status_code} {r.text[:150]}")
                else:
                    body = transform(key, r.content)
                    dst_params = {"datastoreName": cfg["dst_store"], "scope": cfg["scope"], "entryKey": key}

                    exists = False
                    if cfg["skip_existing"]:
                        chk = call("GET", dst_url, headers={"x-api-key": cfg["dst_key"]}, params=dst_params)
                        exists = chk.status_code == 200

                    if exists:
                        bump("skipped")
                        log(f"[lewat] {key}: sudah ada di game 2")
                    elif cfg["dry_run"]:
                        bump("copied")
                        log(f"[simulasi] {key} ({len(body)} byte)")
                    else:
                        headers = {
                            "x-api-key": cfg["dst_key"],
                            "content-type": "application/json",
                            "content-md5": base64.b64encode(hashlib.md5(body).digest()).decode(),
                        }
                        for h in ("roblox-entry-userids", "roblox-entry-attributes"):
                            if h in r.headers:
                                headers[h] = r.headers[h]
                        w = call("POST", dst_url, headers=headers, params=dst_params, data=body)
                        if w.ok:
                            bump("copied")
                            log(f"[ok] {key}")
                        else:
                            bump("failed")
                            log(f"[gagal] {key}: tulis {w.status_code} {w.text[:150]}")
            except Stopped:
                raise
            except Exception as e:  # satu key gagal tidak menghentikan semuanya
                bump("failed")
                log(f"[gagal] {key}: {e}")
            bump("done")

        with lock:
            job["state"] = "finished"
        log("Selesai.")
    except Stopped:
        with lock:
            job["state"] = "stopped"
        log("Dihentikan.")
    except Exception as e:
        with lock:
            job["state"] = "error"
        log(f"[error] {e}")


# ---------- Info game (publik, tanpa API key) ----------

@app.get("/api/universe/<uid>")
def universe(uid):
    if not uid.isdigit():
        return jsonify(error="Universe ID harus berupa angka"), 400
    try:
        info = requests.get("https://games.roblox.com/v1/games",
                            params={"universeIds": uid}, timeout=12).json().get("data", [])
        thumb = requests.get("https://thumbnails.roblox.com/v1/games/icons",
                             params={"universeIds": uid, "size": "256x256", "format": "Png",
                                     "returnPolicy": "PlaceHolder"}, timeout=12).json().get("data", [])
    except Exception as e:
        return jsonify(error=f"Tidak bisa menghubungi Roblox: {e}"), 502
    if not info:
        return jsonify(error="Experience tidak ditemukan atau masih privat"), 404
    g = info[0]
    return jsonify(
        name=g.get("name"),
        creator=(g.get("creator") or {}).get("name"),
        visits=g.get("visits"),
        playing=g.get("playing"),
        image=thumb[0].get("imageUrl") if thumb else None,
    )


@app.post("/api/stores")
def stores():
    d = request.get_json(force=True)
    uid, key = str(d.get("universe", "")).strip(), str(d.get("key", "")).strip()
    if not uid.isdigit() or not key:
        return jsonify(error="Isi Universe ID dan API key dulu"), 400
    names, cursor = [], ""
    try:
        for _ in range(10):
            r = requests.get(LIST_STORES.format(uid), headers={"x-api-key": key},
                             params={"limit": 100, "cursor": cursor}, timeout=15)
            if not r.ok:
                hint = " (key perlu izin List Data Stores)" if r.status_code in (401, 403) else ""
                return jsonify(error=f"Roblox menjawab {r.status_code}{hint}"), 400
            j = r.json()
            names += [s["name"] for s in j.get("datastores", [])]
            cursor = j.get("nextPageCursor")
            if not cursor:
                break
    except Exception as e:
        return jsonify(error=str(e)), 502
    return jsonify(names=sorted(set(names)))


# ---------- Kontrol proses ----------

@app.post("/api/start")
def start():
    if job["state"] == "running":
        return jsonify(error="Masih ada proses yang berjalan"), 409
    d = request.get_json(force=True)
    cfg = {
        "src_universe": str(d.get("src_universe", "")).strip(),
        "dst_universe": str(d.get("dst_universe", "")).strip(),
        "src_key": str(d.get("src_key", "")).strip(),
        "dst_key": str(d.get("dst_key", "")).strip() or str(d.get("src_key", "")).strip(),
        "src_store": str(d.get("src_store", "")).strip(),
        "dst_store": str(d.get("dst_store", "")).strip() or str(d.get("src_store", "")).strip(),
        "scope": str(d.get("scope", "")).strip() or "global",
        "prefix": str(d.get("prefix", "")).strip(),
        "limit": int(d.get("limit") or 0),
        "dry_run": bool(d.get("dry_run")),
        "skip_existing": bool(d.get("skip_existing")),
    }
    missing = [k for k in ("src_universe", "dst_universe", "src_key", "src_store") if not cfg[k]]
    if missing:
        return jsonify(error="Field wajib belum diisi: " + ", ".join(missing)), 400
    stop_flag.clear()
    with lock:
        job.update(state="running", done=0, copied=0, skipped=0, failed=0, dry=cfg["dry_run"], logs=[])
    threading.Thread(target=worker, args=(cfg,), daemon=True).start()
    return jsonify(ok=True)


@app.post("/api/stop")
def stop():
    stop_flag.set()
    return jsonify(ok=True)


@app.get("/api/status")
def status():
    with lock:
        return jsonify(job)


@app.get("/api/settings")
def get_settings():
    return jsonify(silent=state["silent"], author=AUTHOR, version=VERSION)


@app.post("/api/settings")
def set_settings():
    d = request.get_json(force=True)
    if "silent" in d:
        state["silent"] = bool(d["silent"])
        apply_silence()
    return jsonify(silent=state["silent"])


@app.get("/")
def index():
    return Response(PAGE, mimetype="text/html")


PAGE = r"""<!doctype html>
<html lang="id">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Salin DataStore Roblox</title>
<style>
:root{
  --bg:#fff0f7; --panel:rgba(255,255,255,.88); --sunk:#fff5fa; --ink:#48253c; --mute:#98758b; --line:#f4cfe2;
  --accent:#e65b9b; --accent-ink:#fff; --ok:#388f7c; --warn:#dc526e; --amber:#b7791f;
  --log:#342334; --log-ink:#ffe7f3; --shadow:0 4px 12px rgba(173,79,128,.08),0 22px 48px -24px rgba(173,79,128,.3);
  color-scheme: light;
}
*{box-sizing:border-box}
html{scroll-behavior:smooth;min-height:100%}
body{min-height:100vh;margin:0;position:relative;isolation:isolate;overflow-x:hidden;
  background:radial-gradient(ellipse at 12% 8%,rgba(255,174,214,.42),transparent 30%),
    radial-gradient(ellipse at 88% 42%,rgba(255,206,227,.55),transparent 34%),
    linear-gradient(145deg,var(--bg),#fffafd 55%,#ffe8f2);
  color:var(--ink);font:15px/1.5 "Segoe UI Variable","Segoe UI",Inter,system-ui,-apple-system,sans-serif}
body::before,body::after{position:fixed;pointer-events:none;z-index:0}
body::before{content:"";inset:-10%;opacity:.48;
  background-image:radial-gradient(circle,rgba(255,255,255,.95) 1.5px,transparent 2.5px),
    radial-gradient(circle,rgba(232,113,168,.36) 2px,transparent 3px),
    radial-gradient(ellipse,rgba(255,184,215,.48) 0 2px,transparent 3px);
  background-size:120px 120px,190px 190px,260px 220px;background-position:10px 20px,70px 95px,140px 40px;
  animation:petal-drift 32s ease-in-out infinite alternate}
body::after{content:"✧";right:7%;top:12%;color:rgba(230,91,155,.48);font-size:2.3rem;
  text-shadow:22px 48px 0 rgba(255,172,209,.56),-38px 108px 0 rgba(230,91,155,.36);
  animation:sparkle 4s ease-in-out infinite}
@keyframes petal-drift{to{background-position:90px 70px,10px 30px,210px 110px;transform:rotate(5deg)}}
@keyframes sparkle{50%{opacity:.52;transform:translateY(-8px) rotate(18deg)}}
.tut,main{position:relative;z-index:1}
main{max-width:1120px;margin:0 auto;padding:36px 22px 64px}
header.top{display:flex;justify-content:space-between;align-items:flex-start;gap:16px;margin-bottom:28px}
h1{font-size:2rem;line-height:1.15;margin:0 0 6px;letter-spacing:-.02em;
  background:linear-gradient(100deg,#d94e91,#a84f9d 58%,#e678a8);background-clip:text;color:transparent}
.lead{margin:0;color:var(--mute);max-width:56ch}
.pill{flex:none;padding:6px 14px;border-radius:99px;border:1px solid var(--line);background:var(--panel);
  font-size:.85rem;font-weight:600;color:var(--mute)}
.pill.running{color:var(--accent);border-color:var(--accent)}
.pill.finished{color:var(--ok);border-color:var(--ok)}
.pill.error,.pill.stopped{color:var(--warn);border-color:var(--warn)}

/* Dua game + penghubung */
.pair{display:grid;grid-template-columns:1fr 120px 1fr;align-items:center;margin-bottom:22px}
.link{display:flex;flex-direction:column;align-items:center;gap:4px;color:var(--mute);font-size:.8rem}
.link svg{width:100%;height:32px;overflow:visible}
.link path.rail{stroke:var(--line);stroke-width:2;fill:none}
.link path.flow{stroke:var(--accent);stroke-width:2.5;fill:none;stroke-dasharray:6 8;stroke-linecap:round;opacity:0}
.link polygon{fill:var(--line)}
.running .link path.flow{opacity:1;animation:flow .9s linear infinite}
.running .link polygon{fill:var(--accent)}
@keyframes flow{to{stroke-dashoffset:-14}}
.card{background:var(--panel);border:1px solid var(--line);border-radius:24px;padding:22px;box-shadow:var(--shadow);
  backdrop-filter:blur(12px);transition:transform .28s ease,box-shadow .28s ease,border-color .28s ease}
.card:hover{transform:translateY(-3px);border-color:color-mix(in srgb,var(--accent) 42%,var(--line));
  box-shadow:0 10px 28px rgba(173,79,128,.13),0 24px 48px -26px rgba(173,79,128,.3)}
.who{display:flex;gap:16px;align-items:center;margin-bottom:18px}
.thumb{flex:none;width:96px;height:96px;border-radius:22px;overflow:hidden;background:var(--sunk);
  border:1px solid var(--line);display:grid;place-items:center;font-size:2rem;color:var(--mute);font-weight:700}
.thumb img{width:100%;height:100%;object-fit:cover;display:block}
.thumb.load{animation:pulse 1.2s ease-in-out infinite}
@keyframes pulse{50%{opacity:.45}}
.role{font-size:.8rem;color:var(--mute);margin:0 0 2px}
.who h3{margin:0;font-size:1.15rem;line-height:1.25;overflow-wrap:anywhere}
.who .by{margin:2px 0 8px;color:var(--mute);font-size:.88rem}
.chips{display:flex;gap:6px;flex-wrap:wrap}
.chip{font-size:.78rem;padding:2px 9px;border-radius:99px;background:var(--sunk);border:1px solid var(--line)}
.err{color:var(--warn);font-size:.85rem}

label{display:block;font-size:.84rem;font-weight:600;margin:0 0 12px}
label em{font-weight:400;font-style:normal;color:var(--mute)}
input[type=text],input[type=password],input[type=number]{
  display:block;width:100%;margin-top:5px;padding:10px 12px;font:inherit;font-weight:400;color:var(--ink);
  background:var(--sunk);border:1px solid var(--line);border-radius:10px}
input::placeholder{color:var(--mute);opacity:.7}
input:focus-visible,button:focus-visible,.sw input:focus-visible+i{outline:2px solid var(--accent);outline-offset:2px}

/* Bagian bawah */
.lower{display:grid;grid-template-columns:5fr 6fr;gap:22px}
.card h2{margin:0 0 16px;font-size:1.05rem}
.two{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.linkbtn{background:none;border:0;padding:0;color:var(--accent);font:inherit;font-size:.8rem;font-weight:600;
  cursor:pointer;margin:-6px 0 12px;display:block}
.sw{display:flex;gap:12px;align-items:flex-start;margin:0 0 14px;cursor:pointer;font-weight:400}
.sw input{position:absolute;opacity:0}
.sw i{flex:none;width:40px;height:23px;border-radius:99px;background:var(--line);position:relative;transition:background .15s}
.sw i::after{content:"";position:absolute;left:3px;top:3px;width:17px;height:17px;border-radius:50%;background:#fff;transition:transform .15s}
.sw input:checked+i{background:var(--accent)}
.sw input:checked+i::after{transform:translateX(17px)}
.sw b{display:block;font-size:.9rem}
.sw small{display:block;color:var(--mute);font-size:.8rem}
.actions{display:flex;gap:10px;margin-top:6px}
button.btn{font:inherit;font-weight:700;padding:12px 20px;border-radius:16px;border:1px solid var(--accent);
  background:linear-gradient(120deg,var(--accent),#ee78ad);color:var(--accent-ink);cursor:pointer;flex:1;
  box-shadow:0 6px 16px rgba(230,91,155,.2);transition:transform .2s ease,box-shadow .2s ease,filter .2s ease}
button.btn:hover:not(:disabled){transform:translateY(-2px);box-shadow:0 10px 22px rgba(230,91,155,.3);filter:saturate(1.08)}
button.btn:active:not(:disabled){transform:translateY(1px) scale(.99)}
button.btn.ghost{background:transparent;color:var(--accent);flex:none}
button.btn.danger{border-color:var(--warn);color:var(--warn)}
button:disabled{opacity:.4;cursor:not-allowed}
#msg{color:var(--warn);font-size:.88rem;margin-top:10px;min-height:1.3em}

.bar{height:10px;border-radius:99px;background:var(--sunk);border:1px solid var(--line);overflow:hidden;margin-bottom:16px}
.bar i{display:block;height:100%;width:0;background:var(--accent);border-radius:99px;transition:width .4s}
.bar.indet i{width:35%;animation:slide 1.3s ease-in-out infinite}
@keyframes slide{0%{margin-left:-35%}100%{margin-left:100%}}
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:16px}
.stat{background:var(--sunk);border:1px solid var(--line);border-radius:14px;padding:10px 12px}
.stat b{display:block;font-size:1.5rem;line-height:1.2;font-variant-numeric:tabular-nums}
.stat span{font-size:.78rem;color:var(--mute)}
.stat.bad b{color:var(--warn)}
#log{background:var(--log);color:var(--log-ink);border-radius:14px;padding:12px 14px;height:280px;overflow:auto;
  font:.78rem/1.65 ui-monospace,"Cascadia Mono",Consolas,monospace}
#log div{white-space:pre-wrap;word-break:break-all}
#log .t-ok{color:#5be3c4}#log .t-bad{color:#ff8a93}#log .t-skip{color:#f0b45a}#log .t-sim{color:#8ea2ff}
#log .empty{color:#6c779e}
.note{font-size:.82rem;color:var(--mute);margin:14px 0 0}

[hidden]{display:none!important}
.hdr-right{display:flex;gap:14px;align-items:center}
.tutbtn{margin:0!important;font-size:.88rem!important}
.pages{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:18px}
.tut-pages{grid-template-columns:repeat(4,1fr)}
.page-tab{padding:10px 12px;border:1px solid var(--line);border-radius:12px;background:var(--panel);
  color:var(--mute);font:inherit;font-weight:600;cursor:pointer;transition:transform .2s ease,color .2s ease,background .2s ease,box-shadow .2s ease}
.page-tab:hover{transform:translateY(-2px);color:var(--accent)}
.page-tab[aria-current="step"]{color:var(--accent);border-color:var(--accent);background:var(--panel);
  box-shadow:0 5px 16px rgba(230,91,155,.16)}
.wizard-page>.card{max-width:760px;margin:0 auto}
.tut-page>.card{margin-bottom:20px}
.wizard-page:not([hidden]),.tut-page:not([hidden]){animation:page-pop .42s cubic-bezier(.2,.8,.2,1) both}
@keyframes page-pop{from{opacity:0;transform:translateY(12px) scale(.99)}to{opacity:1;transform:translateY(0) scale(1)}}
.page-actions{display:flex;justify-content:space-between;gap:10px;margin-top:18px}
.page-actions .btn{flex:0 1 220px}
.page-actions .btn:only-child{margin-left:auto}
.credit{text-align:center;color:var(--mute);font-size:.8rem;margin-top:32px}
.credit p{margin:2px 0}
.tut .wrap{max-width:820px;margin:0 auto;padding:48px 22px 72px}
.tut h1{font-size:2.5rem;margin-bottom:12px}
.tut .card{margin-bottom:20px}
.tut .lead{font-size:1.05rem;margin-bottom:28px}
.steps{list-style:none;counter-reset:s;margin:0;padding:0;display:grid;gap:18px}
.steps li{counter-increment:s;display:grid;grid-template-columns:40px 1fr;gap:14px}
.steps li::before{content:counter(s);width:40px;height:40px;border-radius:12px;background:var(--accent);
  color:var(--accent-ink);display:grid;place-items:center;font-weight:700}
.steps b{display:block}
.steps p{margin:2px 0 0;color:var(--mute)}
.tablewrap{overflow-x:auto}
table.perm{width:100%;border-collapse:collapse;font-size:.9rem;min-width:520px}
.perm th,.perm td{text-align:left;padding:10px 12px;border-bottom:1px solid var(--line);vertical-align:top}
.perm th{color:var(--mute);font-weight:600;font-size:.82rem}
.perm tr:last-child td{border-bottom:0}
.tag{display:inline-block;padding:1px 9px;border-radius:99px;font-size:.78rem;background:var(--sunk);
  border:1px solid var(--line);margin:0 4px 4px 0}
.tag.opt{color:var(--mute);border-style:dashed}
.tag.no{color:var(--warn);border-color:var(--warn)}
details{border:1px solid var(--line);border-radius:12px;background:var(--sunk);padding:0 16px;margin-bottom:10px}
summary{cursor:pointer;font-weight:600;padding:13px 0}
details[open] summary{border-bottom:1px solid var(--line);margin-bottom:12px}
details p,details ul{margin:0 0 12px}
details ul{padding-left:20px}
details a,.credit a{color:var(--accent)}
.ack{display:flex;gap:10px;align-items:flex-start;font-weight:500;font-size:.95rem}
.ack input{margin-top:4px;width:18px;height:18px;flex:none}
#enter{margin-top:16px;width:100%}
@media (max-width:860px){
  .tut h1{font-size:2rem}
  .pair{grid-template-columns:1fr;gap:6px}
  .link{transform:rotate(90deg);height:60px;justify-self:center;width:90px}
  .lower{grid-template-columns:1fr}
  .stats{grid-template-columns:repeat(2,1fr)}
}
@media (max-width:520px){.tut-pages{grid-template-columns:repeat(2,1fr)}main{padding:24px 14px 44px}
  .tut .wrap{padding:30px 14px 48px}.page-tab{padding:9px 5px;font-size:.88rem}.card{padding:18px}}
@media (prefers-reduced-motion:reduce){*,*::before,*::after{animation:none!important;transition:none!important;scroll-behavior:auto!important}}
</style>
</head>
<body>
<div class="tut" id="view-tut" hidden>
 <div class="wrap">
  <h1>Salin DataStore Roblox</h1>
  <p class="lead">Aplikasi ini menyalin data DataStore dari satu experience (game 1) ke experience lain (game 2) lewat API resmi Roblox. Game 1 hanya dibaca, tidak ada yang dihapus. Baca tutorial singkat ini sebelum mulai.</p>

  <nav class="pages tut-pages" aria-label="Halaman tutorial">
    <button type="button" class="page-tab" data-tut-page="1" aria-current="step">1. Langkah</button>
    <button type="button" class="page-tab" data-tut-page="2">2. Izin API</button>
    <button type="button" class="page-tab" data-tut-page="3">3. FAQ</button>
    <button type="button" class="page-tab" data-tut-page="4">4. Mulai</button>
  </nav>

  <section class="tut-page" id="tut-page-1">
    <div class="card">
      <h2>Langkah penggunaan</h2>
      <ol class="steps">
        <li><div><b>Catat Universe ID kedua game</b><p>Buka Creator Hub, pilih experience, lalu lihat Universe ID di halaman Overview. Aplikasi akan menampilkan ikon dan nama game supaya kamu tahu ID-nya benar.</p></div></li>
        <li><div><b>Buat API key</b><p>Di Creator Hub: Credentials, API Keys, Create API Key. Pilih sistem Data Stores, tambahkan experience, dan centang izin sesuai tabel di bawah. Beri masa berlaku (Expiration) yang pendek.</p></div></li>
        <li><div><b>Isi form di aplikasi</b><p>Masukkan Universe ID, API key, dan nama DataStore. Tombol "Muat daftar" membantu memilih nama DataStore tanpa mengetik.</p></div></li>
        <li><div><b>Simulasi dulu, lalu salin 10 key</b><p>Mode Simulasi hanya membaca. Kalau lancar, matikan Simulasi, salin 10 key, lalu cek hasilnya di game 2.</p></div></li>
        <li><div><b>Salin semuanya, lalu hapus API key</b><p>Isi jumlah key dengan 0 untuk memproses semua. Setelah selesai, hapus API key di Creator Hub.</p></div></li>
      </ol>
    </div>
  </section>

  <section class="tut-page" id="tut-page-2" hidden>
    <div class="card">
      <h2>Izin API key yang dibutuhkan</h2>
      <div class="tablewrap"><table class="perm">
        <tr><th>Key</th><th>Izin</th><th>Keterangan</th></tr>
        <tr><td>Game 1 (sumber)</td>
          <td><span class="tag">Read Entry</span><span class="tag">List Entries</span><span class="tag opt">List Data Stores</span></td>
          <td>Tanpa izin tulis, jadi game 1 tidak bisa berubah.</td></tr>
        <tr><td>Game 2 (tujuan)</td>
          <td><span class="tag">Create Entry</span><span class="tag">Update Entry</span><span class="tag">Read Entry</span><span class="tag opt">List Data Stores</span></td>
          <td>Read Entry dipakai oleh opsi "Lewati key yang sudah ada".</td></tr>
        <tr><td>Tidak perlu</td>
          <td><span class="tag no">Delete Entry</span></td>
          <td>Aplikasi ini tidak pernah menghapus data.</td></tr>
      </table></div>
      <p class="note">Izin yang berlabel putus-putus (List Data Stores) opsional. Tanpanya, nama DataStore diketik manual.</p>
    </div>
  </section>

  <section class="tut-page" id="tut-page-3" hidden>
    <div class="card">
      <h2>Pertanyaan umum</h2>
    <details open><summary>Apakah ini melanggar Terms of Service Roblox?</summary>
      <p>Kemungkinan besar tidak, selama dipakai untuk experience milikmu sendiri atau yang kamu punya izinnya. Open Cloud adalah API resmi Roblox untuk mengakses DataStore dari luar game, dan dokumentasi Roblox sendiri membahas kasus seperti migrasi data.</p>
      <p>Tapi aplikasi ini buatan pihak ketiga, bukan produk Roblox, dan tidak ada jaminan hukum. Isi aturan Roblox bisa berubah, jadi cek sendiri <a href="https://www.roblox.com/info/terms" target="_blank" rel="noopener noreferrer">Terms of Use</a> dan <a href="https://create.roblox.com/docs/cloud/guides/usage-data-stores" target="_blank" rel="noopener noreferrer">panduan DataStore Open Cloud</a>.</p></details>
    <details><summary>Kapan ini bisa jadi masalah?</summary>
      <ul>
        <li>Menyalin data dari game milik orang lain tanpa izin.</li>
        <li>Membagikan API key atau menaruhnya di GitHub atau script game.</li>
        <li>Memakai data pemain untuk keperluan di luar game, atau menjualnya.</li>
        <li>Menjalankan permintaan berlebihan untuk menembus rate limit.</li>
      </ul></details>
    <details><summary>Apakah data di game 1 bisa rusak atau hilang?</summary>
      <p>Tidak, selama key game 1 hanya punya izin baca dan list. Aplikasi ini tidak pernah mengirim perintah tulis atau hapus ke game 1. Yang bisa tertimpa hanya data di game 2, dan itu dicegah oleh opsi "Lewati key yang sudah ada" (aktif secara default).</p></details>
    <details><summary>Apakah API key saya aman?</summary>
      <p>Aplikasi hanya berjalan di komputermu (127.0.0.1), key disimpan di memori selama proses berjalan dan tidak ditulis ke disk. Key hanya dikirim ke Roblox. Tetap hindari memakainya di komputer bersama atau saat layar sedang dibagikan, dan hapus key setelah selesai.</p></details>
    <details><summary>Apa yang tidak ikut tersalin?</summary>
      <p>OrderedDataStore dan riwayat versi tidak ikut. User ID dan atribut tiap entry ikut disalin, sehingga permintaan penghapusan data pemain tetap bisa dijalankan di game 2. Kalau pemain masih bermain di game 1 saat proses berjalan, data barunya perlu disalin ulang.</p></details>
    <details><summary>Muncul error 401, 403, 404, atau 429</summary>
      <p><b>401/403</b>: izin belum dicentang atau experience belum ditambahkan ke key. <b>404</b>: nama DataStore salah, atau key tidak ada. <b>429</b>: kena rate limit, aplikasi menunggu otomatis lalu lanjut.</p></details>
    </div>
  </section>

  <section class="tut-page" id="tut-page-4" hidden>
    <div class="card">
      <h2>Kredit</h2>
      <p style="margin:0 0 6px"><span class="by-line"></span> · <span class="app-version"></span></p>
      <p class="note" style="margin:0">Memakai Roblox Open Cloud API, Flask, dan Requests. Aplikasi ini tidak berafiliasi dengan, didukung, atau dibuat oleh Roblox Corporation. Roblox adalah merek dagang Roblox Corporation.</p>
    </div>

    <div class="card">
      <label class="ack"><input type="checkbox" id="ack">
        <span>Saya hanya akan menyalin data dari experience milik saya sendiri, atau yang saya punya izin resminya.</span></label>
      <button type="button" class="btn" id="enter" disabled>Lanjut ke aplikasi</button>
    </div>
  </section>
  <div class="page-actions">
    <button type="button" class="btn ghost" id="tut-back" hidden>Kembali</button>
    <button type="button" class="btn" id="tut-next">Lanjut</button>
  </div>
 </div>
</div>

<main id="view-app" hidden>
  <header class="top">
    <div>
      <h1>Salin DataStore Roblox</h1>
      <p class="lead">Salin data dari game 1 ke game 2. Game 1 hanya dibaca, tidak ada yang dihapus. API key tetap di komputermu.</p>
    </div>
    <div class="hdr-right"><button type="button" class="linkbtn tutbtn" id="open-tut">Tutorial</button><span class="pill" id="badge">Siap</span></div>
  </header>

  <form id="f" autocomplete="off" novalidate>
    <nav class="pages" aria-label="Halaman aplikasi">
      <button type="button" class="page-tab" data-page="1" aria-current="step">1. Game</button>
      <button type="button" class="page-tab" data-page="2">2. Pengaturan</button>
      <button type="button" class="page-tab" data-page="3">3. Progres</button>
    </nav>

    <section class="wizard-page" id="page-1" aria-label="Pilih game">
      <section class="pair" id="pair"></section>
      <div class="page-actions">
        <button type="button" class="btn" data-next="2">Lanjut ke Pengaturan</button>
      </div>
    </section>

    <section class="wizard-page" id="page-2" aria-label="Pengaturan penyalinan" hidden>
      <div class="card">
        <h2>Pengaturan</h2>
        <label>Nama DataStore di game 1
          <input type="text" name="src_store" list="dl-src" required placeholder="PlayerData"></label>
        <button type="button" class="linkbtn" data-load="src">Muat daftar dari game 1</button>
        <label>Nama DataStore di game 2 <em>(kosong = sama)</em>
          <input type="text" name="dst_store" list="dl-dst"></label>
        <button type="button" class="linkbtn" data-load="dst">Muat daftar dari game 2</button>
        <datalist id="dl-src"></datalist><datalist id="dl-dst"></datalist>
        <div class="two">
          <label>Scope<input type="text" name="scope" value="global"></label>
          <label>Awalan key <em>(opsional)</em><input type="text" name="prefix" placeholder="Player_"></label>
        </div>
        <label>Jumlah key yang diproses <em>(0 = semua)</em><input type="number" name="limit" value="10" min="0"></label>

        <label class="sw"><input type="checkbox" id="dry" checked><i></i>
          <span><b>Simulasi</b><small>Hanya membaca, tidak menulis ke game 2. Cocok untuk uji coba.</small></span></label>
        <label class="sw"><input type="checkbox" id="skip" checked><i></i>
          <span><b>Lewati key yang sudah ada di game 2</b><small>Data lama di game 2 tidak tertimpa.</small></span></label>

        <label class="sw"><input type="checkbox" id="silent" checked><i></i>
          <span><b>Senyapkan log terminal</b><small>Menyembunyikan baris GET/POST di jendela terminal.</small></span></label>

        <div class="page-actions">
          <button type="button" class="btn ghost" data-next="1">Kembali</button>
          <button type="submit" class="btn" id="go">Mulai simulasi</button>
        </div>
        <div id="msg" role="alert"></div>
      </div>
    </section>

    <section class="wizard-page" id="page-3" aria-label="Progres penyalinan" hidden>
      <div class="card" aria-live="polite">
        <h2>Progres</h2>
        <div class="bar" id="bar"><i></i></div>
        <div class="stats">
          <div class="stat"><b id="s-done">0</b><span>Diproses</span></div>
          <div class="stat"><b id="s-copied">0</b><span id="l-copied">Disalin</span></div>
          <div class="stat"><b id="s-skipped">0</b><span>Dilewati</span></div>
          <div class="stat bad"><b id="s-failed">0</b><span>Gagal</span></div>
        </div>
        <div id="log"><div class="empty">Log akan muncul di sini.</div></div>
        <p class="note">Urutan aman: simulasi 10 key, salin 10 key, cek di game 2, lalu salin semuanya (isi 0). Hapus API key di Creator Hub setelah selesai.</p>
        <div class="page-actions">
          <button type="button" class="btn ghost" data-next="2">Kembali ke Pengaturan</button>
          <button type="button" class="btn ghost danger" id="stop" disabled>Hentikan</button>
        </div>
      </div>
    </section>
  </form>
  <footer class="credit">
    <p><span class="by-line"></span> · <span class="app-version"></span></p>
    <p>Tidak berafiliasi dengan Roblox Corporation.</p>
  </footer>
</main>

<script>
const $ = id => document.getElementById(id);
const f = $("f"), go = $("go"), stopBtn = $("stop"), msg = $("msg"), dry = $("dry");
const fmt = n => new Intl.NumberFormat("id-ID", {notation:"compact"}).format(n);

let currentPage = 1;
function showPage(n){
  currentPage = n;
  document.querySelectorAll(".wizard-page").forEach((el, i) => { el.hidden = i + 1 !== n; });
  document.querySelectorAll(".page-tab").forEach((el, i) => {
    if (i + 1 === n) el.setAttribute("aria-current", "step");
    else el.removeAttribute("aria-current");
  });
  window.scrollTo(0, 0);
}
function validatePage(n){
  const page = $("page-" + n);
  for (const input of page.querySelectorAll("input[required]")){
    if (!input.checkValidity()){
      showPage(n);
      input.reportValidity();
      return false;
    }
  }
  return true;
}
document.querySelectorAll("[data-page]").forEach(b => b.addEventListener("click", () => showPage(Number(b.dataset.page))));
document.querySelectorAll("[data-next]").forEach(b => b.addEventListener("click", () => {
  const next = Number(b.dataset.next);
  if (next > currentPage && !validatePage(currentPage)) return;
  showPage(next);
}));

// Kartu game 1 & game 2
const sides = {
  src: {role:"Sumber · game 1", keyLabel:"API key game 1", keyHint:"Izin: Read Entry, List Entries"},
  dst: {role:"Tujuan · game 2", keyLabel:"API key game 2", keyHint:"kosong = pakai key game 1"}
};
$("pair").innerHTML = ["src","dst"].map((s, i) => `
  ${i === 1 ? `<div class="link" aria-hidden="true">
    <svg viewBox="0 0 120 32"><path class="rail" d="M4 16H104"/><path class="flow" d="M4 16H104"/><polygon points="102,8 118,16 102,24"/></svg>
    <span>salin</span></div>` : ""}
  <article class="card">
    <div class="who">
      <div class="thumb" id="th-${s}"><span>?</span></div>
      <div>
        <p class="role">${sides[s].role}</p>
        <h3 id="nm-${s}">Belum dipilih</h3>
        <p class="by" id="by-${s}">Masukkan Universe ID di bawah</p>
        <div class="chips" id="ch-${s}"></div>
      </div>
    </div>
    <label>Universe ID<input type="text" name="${s}_universe" inputmode="numeric" required placeholder="mis. 1234567890"></label>
    <label>${sides[s].keyLabel} <em>(${sides[s].keyHint})</em>
      <input type="password" name="${s}_key" ${s === "src" ? "required" : ""}></label>
  </article>`).join("");

const timers = {};
function setCard(s, d, error){
  const th = $("th-"+s), nm = $("nm-"+s), by = $("by-"+s), ch = $("ch-"+s);
  th.classList.remove("load"); ch.textContent = "";
  if (error){ th.innerHTML = "<span>?</span>"; nm.textContent = "Tidak terbaca"; by.textContent = ""; by.className = "by err"; by.textContent = error; return; }
  by.className = "by";
  nm.textContent = d.name || "Tanpa nama";
  by.textContent = d.creator ? "oleh " + d.creator : "";
  if (d.image){ const im = new Image(); im.alt = ""; im.referrerPolicy = "no-referrer"; im.src = d.image; th.replaceChildren(im); }
  else th.innerHTML = "<span>" + (d.name || "?").slice(0,1).toUpperCase() + "</span>";
  [["kunjungan", d.visits], ["bermain", d.playing]].forEach(([l, v]) => {
    if (v == null) return; const c = document.createElement("span"); c.className = "chip"; c.textContent = fmt(v) + " " + l; ch.append(c);
  });
}
async function lookup(s){
  const id = f.elements[s+"_universe"].value.trim(); if (!id) return;
  $("th-"+s).classList.add("load");
  try {
    const r = await fetch("/api/universe/" + encodeURIComponent(id)), j = await r.json();
    r.ok ? setCard(s, j) : setCard(s, null, j.error);
  } catch(e){ setCard(s, null, "Server tidak merespons"); }
}
["src","dst"].forEach(s => f.elements[s+"_universe"].addEventListener("input", () => {
  clearTimeout(timers[s]); timers[s] = setTimeout(() => lookup(s), 600);
}));

// Muat daftar DataStore
document.querySelectorAll("[data-load]").forEach(b => b.addEventListener("click", async () => {
  const s = b.dataset.load; msg.textContent = "";
  const key = f.elements[s+"_key"].value || f.elements.src_key.value;
  b.textContent = "Memuat..."; 
  const r = await fetch("/api/stores", {method:"POST", headers:{"content-type":"application/json"},
    body: JSON.stringify({universe: f.elements[s+"_universe"].value, key})});
  const j = await r.json();
  b.textContent = "Muat daftar dari game " + (s === "src" ? 1 : 2);
  if (!r.ok){ msg.textContent = j.error; return; }
  $("dl-"+s).replaceChildren(...j.names.map(n => Object.assign(document.createElement("option"), {value:n})));
  msg.textContent = j.names.length ? "" : "Belum ada DataStore di game ini.";
  if (j.names.length) b.textContent = j.names.length + " DataStore ditemukan, klik kolom untuk memilih";
}));

dry.addEventListener("change", () => { go.textContent = dry.checked ? "Mulai simulasi" : "Mulai salin"; });

// Progres
const labels = {idle:"Siap", running:"Berjalan", finished:"Selesai", stopped:"Dihentikan", error:"Error"};
let timer = null, planned = 0;
const esc = t => t.replace(/[&<>]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));
const cls = l => l.includes("[ok]") ? "t-ok" : l.includes("[gagal]") || l.includes("[error]") ? "t-bad"
  : l.includes("[lewat]") ? "t-skip" : l.includes("[simulasi]") ? "t-sim" : "";

function render(s){
  $("s-done").textContent = s.done; $("s-copied").textContent = s.copied;
  $("l-copied").textContent = s.dry ? "Terbaca" : "Disalin";
  $("s-skipped").textContent = s.skipped; $("s-failed").textContent = s.failed;
  const b = $("badge"); b.textContent = labels[s.state] || s.state; b.className = "pill " + s.state;
  document.body.classList.toggle("running", s.state === "running");
  const bar = $("bar"), running = s.state === "running";
  bar.classList.toggle("indet", running && !planned);
  bar.firstElementChild.style.width = planned ? Math.min(100, s.done / planned * 100) + "%"
    : (s.state === "finished" ? "100%" : running ? "" : "0");
  const log = $("log"), atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 30;
  if (s.logs.length){ log.innerHTML = s.logs.map(l => `<div class="${cls(l)}">${esc(l)}</div>`).join(""); if (atBottom) log.scrollTop = log.scrollHeight; }
  go.disabled = running; stopBtn.disabled = !running;
  if (!running && timer){ clearInterval(timer); timer = null; }
}
const poll = async () => { try { render(await (await fetch("/api/status")).json()); } catch(e){} };

f.addEventListener("submit", async e => {
  e.preventDefault(); msg.textContent = "";
  if (!validatePage(1) || !validatePage(2)){ return; }
  if (!dry.checked && !confirm("Ini akan MENULIS data ke game 2. Lanjutkan?")) return;
  const d = {};
  for (const [k, v] of new FormData(f).entries()) d[k] = v;
  d.dry_run = dry.checked; d.skip_existing = $("skip").checked;
  planned = parseInt(d.limit) || 0;
  const r = await fetch("/api/start", {method:"POST", headers:{"content-type":"application/json"}, body: JSON.stringify(d)});
  const j = await r.json();
  if (!r.ok){ msg.textContent = j.error || "Gagal memulai"; return; }
  showPage(3);
  if (!timer) timer = setInterval(poll, 900);
  poll();
});
stopBtn.addEventListener("click", () => fetch("/api/stop", {method:"POST"}));

// Tutorial, setelan, kredit
const tut = $("view-tut"), appv = $("view-app"), ack = $("ack"), enter = $("enter");
let currentTutorialPage = 1;
function showTutorialPage(n){
  currentTutorialPage = n;
  document.querySelectorAll(".tut-page").forEach((el, i) => { el.hidden = i + 1 !== n; });
  document.querySelectorAll("[data-tut-page]").forEach((el, i) => {
    if (i + 1 === n) el.setAttribute("aria-current", "step");
    else el.removeAttribute("aria-current");
  });
  $("tut-back").hidden = n === 1;
  $("tut-next").hidden = n === 4;
  window.scrollTo(0, 0);
}
document.querySelectorAll("[data-tut-page]").forEach(b => b.addEventListener("click", () => showTutorialPage(Number(b.dataset.tutPage))));
$("tut-back").addEventListener("click", () => showTutorialPage(currentTutorialPage - 1));
$("tut-next").addEventListener("click", () => showTutorialPage(currentTutorialPage + 1));
function show(v){ tut.hidden = v !== "tut"; appv.hidden = v !== "app"; window.scrollTo(0, 0); }
let seen = false; try { seen = localStorage.getItem("rdc_seen") === "1"; } catch(e){}
ack.addEventListener("change", () => { enter.disabled = !ack.checked; });
enter.addEventListener("click", () => { try { localStorage.setItem("rdc_seen", "1"); } catch(e){} show("app"); });
$("open-tut").addEventListener("click", () => { ack.checked = true; enter.disabled = false; show("tut"); });
showTutorialPage(1);
show(seen ? "app" : "tut");

fetch("/api/settings").then(r => r.json()).then(s => {
  $("silent").checked = s.silent;
  const t = "by " + (s.author || "Fari Noveri");
  document.querySelectorAll(".by-line").forEach(el => { el.textContent = t; });
  document.querySelectorAll(".app-version").forEach(el => { el.textContent = "v" + s.version; });
}).catch(() => {});
$("silent").addEventListener("change", e => fetch("/api/settings", {method:"POST",
  headers:{"content-type":"application/json"}, body: JSON.stringify({silent: e.target.checked})}));
poll();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Salin DataStore Roblox")
    ap.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    ap.add_argument("--verbose", action="store_true", help="tampilkan log request di terminal")
    ap.add_argument("--no-browser", action="store_true", help="jangan buka browser otomatis")
    ap.add_argument("--port", type=int, default=5000)
    a = ap.parse_args()

    state["silent"] = not a.verbose
    apply_silence()
    flask_cli.show_server_banner = lambda *x, **k: None
    url = f"http://127.0.0.1:{a.port}"
    print(f"Salin DataStore Roblox berjalan di {url}  (Ctrl+C untuk berhenti)")
    if not a.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    # Hanya bisa diakses dari komputermu sendiri
    app.run(host="127.0.0.1", port=a.port, debug=False)