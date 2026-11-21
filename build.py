#!/usr/bin/env python3
"""
Radar · generador de noticias

Junta noticias de muchas fuentes (RSS + Google News), quita duplicados,
las puntúa según tu perfil (config.json), agrupa historias, detecta
tendencias, trae precios de mercado y genera news.json.

Con GEMINI_API_KEY (gratis en Google AI Studio) además escribe en español:
  - el resumen del día
  - las historias principales (varios titulares de distintos medios = 1 historia)
  - un resumen de una frase por noticia
Para cuidar el límite gratis, la IA corre como máximo cada AI_INTERVAL_MIN
minutos (180 por defecto); entre medias se reutiliza el último resultado.
"""
import html
import json
import math
import os
import re
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import feedparser

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, "news.json")
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
NOW = datetime.now(timezone.utc)

STOPWORDS = set("""
a al algo ante como con contra de del desde donde el ella ellos en entre era es esa ese esta este fue ha han hay la las le les lo los mas me muy no nos o para pero por que se sin sobre son su sus tambien un una unos y ya tras segun hoy dia cada
the a an and or of to in on for with at by from is are was were be been it its this that as after over into new says say will can how why what who than more about out off his her their your our not but has have amid could would may just now today here
""".split())

# Sinónimos para reconocer la misma historia contada con otras palabras
SYN = {
    "share": "stock", "acciones": "stock", "accion": "stock", "equity": "stock",
    "tank": "fall", "slide": "fall", "slid": "fall", "drop": "fall", "dive": "fall", "slip": "fall", "plunge": "fall",
    "sink": "fall", "tumble": "fall", "fell": "fall", "cae": "fall", "caen": "fall", "plummet": "fall", "crash": "fall",
    "retreat": "fall", "slump": "fall", "baja": "fall", "desploma": "fall",
    "jump": "rise", "soar": "rise", "surge": "rise", "rally": "rise", "climb": "rise", "gain": "rise", "sube": "rise",
    "dispara": "rise", "spike": "rise",
}

TREND_STOP = set("""
the a an and or of to in on for with at by from is are was why how what who this that its as after over new says will can
el la los las un una de del en y por para con que como su sus al se lo es tras segun hoy
ai ia ceo ipo us eu uk news stock stocks market markets update weekly review exclusive report live breaking analysis opinion video podcast
wall street inc corp co ltd group tech big top best here these those first
lunes martes miercoles jueves viernes sabado domingo monday tuesday wednesday thursday friday saturday sunday
enero febrero marzo abril mayo junio julio agosto septiembre octubre noviembre diciembre january february march april may june july august september october november december
""".split())


# ---------------------------------------------------------------- utilidades
def norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", text.lower()).strip()


def strip_tags(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def kw_regex(words):
    parts = [re.escape(norm(w)) for w in words if w.strip()]
    if not parts:
        return None
    return re.compile(r"(?<![a-z0-9])(" + "|".join(sorted(parts, key=len, reverse=True)) + r")(?![a-z0-9])")


def stem(w: str) -> str:
    if w in SYN:
        return SYN[w]
    for suf, rep in (("ing", ""), ("ies", "y"), ("ed", ""), ("s", "")):
        if len(w) > len(suf) + 3 and w.endswith(suf) and not w.endswith("ss"):
            w = w[: -len(suf)] + rep
            break
    return SYN.get(w, w)


def tokens(title: str) -> set:
    words = re.findall(r"[a-z0-9]+", norm(title))
    return {stem(w) for w in words if len(w) > 2 and w not in STOPWORDS}


def gnews_url(q: str, idioma: str) -> str:
    hl, gl, ceid = ("es-419", "US", "US:es-419") if idioma == "es" else ("en-US", "US", "US:en")
    query = urllib.parse.quote(f"{q} when:1d")
    return f"https://news.google.com/rss/search?q={query}&hl={hl}&gl={gl}&ceid={ceid}"


def fetch(url: str, timeout: int = 15) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/rss+xml, application/xml, application/json, text/xml, */*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def entry_time(e):
    for key in ("published_parsed", "updated_parsed"):
        t = e.get(key)
        if t:
            try:
                return datetime.fromtimestamp(time.mktime(t), tz=timezone.utc)
            except Exception:
                pass
    return None


# ---------------------------------------------------------------- noticias
def read_source(src: dict) -> list:
    name, url = src["nombre"], src["url"]
    try:
        feed = feedparser.parse(fetch(url))
    except Exception as ex:
        print(f"  ✗ {name}: {ex}")
        return []
    items = []
    for e in feed.entries[:40]:
        title = strip_tags(e.get("title", ""))
        link = e.get("link", "")
        if not title or not link:
            continue
        source_name = name
        if src.get("google"):
            gsrc = e.get("source") or {}
            real = gsrc.get("title") if isinstance(gsrc, dict) else None
            if not real and " - " in title:
                real = title.rsplit(" - ", 1)[1]
            if real:
                source_name = real.strip()
                if title.endswith(" - " + source_name):
                    title = title[: -len(" - " + source_name)].strip()
        summary = "" if src.get("google") else strip_tags(e.get("summary", ""))
        if len(summary) > 260:
            summary = summary[:260].rsplit(" ", 1)[0] + "…"
        items.append({
            "title": title, "link": link, "source": source_name,
            "published": entry_time(e), "summary": summary,
            "feed_topic": src.get("tema"), "feed_weight": src.get("peso", 0),
        })
    print(f"  ✓ {name}: {len(items)}")
    return items


def collect(cfg: dict) -> list:
    sources = list(cfg.get("fuentes", []))
    for b in cfg.get("busquedas_google_news", []):
        sources.append({"nombre": f"Google News: {b['q']}", "url": gnews_url(b["q"], b.get("idioma", "es")),
                        "tema": b.get("tema"), "google": True})
    print(f"Leyendo {len(sources)} fuentes…")
    direct = [s for s in sources if not s.get("google")]
    google = [s for s in sources if s.get("google")]

    def read_google():
        # Google News bloquea las ráfagas: de a una, con pausa y un reintento
        out = []
        for src in google:
            got = read_source(src)
            if not got:
                time.sleep(4)
                got = read_source(src)
            out.append(got)
            time.sleep(1.2)
        return out

    with ThreadPoolExecutor(max_workers=8) as pool:
        g = pool.submit(read_google)
        results = list(pool.map(read_source, direct)) + g.result()
    return [it for batch in results for it in batch]


def cluster(items: list) -> list:
    """Agrupa la misma noticia publicada por varios medios."""
    clusters = []
    for it in sorted(items, key=lambda x: x["published"] or NOW, reverse=True):
        tk = tokens(it["title"])
        placed = False
        if len(tk) >= 3:
            for c in clusters:
                inter = len(tk & c["tk"])
                if inter >= 3 and inter / (len(tk | c["tk"]) or 1) >= 0.42:
                    c["members"].append(it)
                    placed = True
                    break
        if not placed:
            clusters.append({"tk": tk, "members": [it]})
    return clusters


def score_clusters(clusters: list, cfg: dict) -> list:
    topics = cfg.get("temas", {})
    topic_re = {k: kw_regex(v.get("palabras", [])) for k, v in topics.items()}
    urgent_re = kw_regex(cfg.get("senales_urgentes", []))
    exclude_re = kw_regex(cfg.get("excluir", []))
    max_age = cfg.get("horas_maximas", 48)

    out = []
    for c in clusters:
        members = c["members"]
        lead = max(members, key=lambda m: (bool(m["summary"]), m["feed_weight"], len(m["summary"])))
        titles = norm(" ".join(m["title"] for m in members))
        text = norm(" ".join(m["title"] + " " + m["summary"] for m in members))
        if exclude_re and exclude_re.search(titles):
            continue
        newest = max((m["published"] for m in members if m["published"]), default=None)
        age_h = (NOW - newest).total_seconds() / 3600 if newest else 24
        if age_h > max_age:
            continue

        # Temas por palabras clave (el título pesa doble); el tema de la fuente solo suma un poco.
        topic_scores = {}
        for key, rx in topic_re.items():
            if not rx:
                continue
            in_title = set(rx.findall(titles))
            hits = 2 * len(in_title) + len(set(rx.findall(text)) - in_title)
            if hits:
                topic_scores[key] = min(hits, 4) * topics[key].get("peso", 1)
        feed_topics = [m["feed_topic"] for m in members if m["feed_topic"] in topics]
        if feed_topics:
            fk = max(set(feed_topics), key=feed_topics.count)
            topic_scores[fk] = topic_scores.get(fk, 0) + topics[fk].get("peso", 1)
        if not topic_scores:
            continue

        n_sources = len({m["source"] for m in members})
        urgent = len(set(urgent_re.findall(titles))) if urgent_re else 0
        base = sum(topic_scores.values()) + 6 * math.log2(n_sources) + 2 * min(urgent, 2)
        base += max(m["feed_weight"] for m in members)
        recency = 0.35 + 0.65 * math.exp(-max(age_h, 0) / 14)

        seen, others = {lead["source"]}, []
        for m in members:
            if m["source"] not in seen:
                seen.add(m["source"])
                others.append({"source": m["source"], "link": m["link"], "title": m["title"]})

        out.append({
            "title": lead["title"], "link": lead["link"], "source": lead["source"],
            "published": newest.isoformat(timespec="seconds") if newest else None,
            "summary": lead["summary"],
            "topics": sorted(topic_scores, key=topic_scores.get, reverse=True)[:2],
            "n_sources": n_sources, "also": others[:5],
            "score": round(base * recency, 2),
        })
    out.sort(key=lambda x: x["score"], reverse=True)
    out = out[: cfg.get("max_noticias", 150)]
    for i, it in enumerate(out):
        it["id"] = f"n{i}"
    return out


def trends(items: list, n: int = 12) -> list:
    """Nombres propios que más se repiten en los titulares (Anthropic, Nvidia, Fed…)."""
    counts, display = Counter(), {}
    for it in items:
        words = re.findall(r"[A-Za-zÀ-ÿ][\w&\.'’-]*", it["title"])
        if len(words) < 3:
            continue
        caps = sum(1 for w in words if w[0].isupper())
        title_case = caps / len(words) > 0.6  # "Titulares En Mayúsculas": solo cuentan siglas
        seen = set()
        for i, w in enumerate(words):
            w = re.sub(r"(['’]s|['’])$", "", w.strip(".-"))
            key = norm(w)
            acronym = w.isupper() and 2 < len(w) <= 5
            proper = w[:1].isupper() and i > 0 and not title_case
            if not (acronym or proper) or len(key) < 3 or key in TREND_STOP or key in STOPWORDS or key in seen:
                continue
            seen.add(key)
            counts[key] += 1 + min(it["n_sources"] - 1, 3) * 0.5
            display.setdefault(key, w)
    return [{"term": display[k], "count": round(v)} for k, v in counts.most_common(n) if v >= 2.5]


# ---------------------------------------------------------------- mercados
# Símbolos equivalentes en CNBC (fuente principal: una sola consulta para todo)
CNBC = {"^GSPC": ".SPX", "^IXIC": ".IXIC", "^DJI": ".DJI", "BTC-USD": "BTC.CM=", "ETH-USD": "ETH.CM=",
        "GC=F": "@GC.1", "CL=F": "@CL.1", "EURUSD=X": "EUR="}


def _num(v):
    try:
        return float(str(v).replace(",", "").replace("%", "").replace("+", "").strip())
    except Exception:
        return None


def _cnbc(cfg):
    codes = {t["symbol"]: t.get("cnbc") or CNBC.get(t["symbol"], t["symbol"]) for t in cfg.get("mercados", [])}
    url = ("https://quote.cnbc.com/quote-html-webservice/restQuote/symbolType/symbol?symbols="
           + urllib.parse.quote("|".join(codes.values())) + "&requestMethod=itv&noform=1&partnerId=2&fund=1&exthrs=1&output=json")
    data = json.loads(fetch(url, timeout=15))
    quotes = data.get("FormattedQuoteResult", {}).get("FormattedQuote", [])
    by_code = {q.get("symbol"): q for q in quotes}
    out = {}
    for sym, code in codes.items():
        q = by_code.get(code)
        if not q:
            continue
        price = _num(q.get("last"))
        chg = _num(q.get("change_pct"))
        if chg is None and _num(q.get("previous_day_closing")):
            chg = (price / _num(q["previous_day_closing"]) - 1) * 100
        if price:
            out[sym] = (price, chg)
    return out


def _yahoo(sym):
    url = f"https://query2.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(sym)}?range=5d&interval=1d"
    r = json.loads(fetch(url, timeout=12))["chart"]["result"][0]
    closes = [c for c in r["indicators"]["quote"][0]["close"] if c is not None]
    price = r["meta"].get("regularMarketPrice") or closes[-1]
    prev = r["meta"].get("chartPreviousClose") if len(closes) < 2 else (closes[-2] if abs(closes[-1] - price) / price < 0.0005 else closes[-1])
    return price, ((price / prev - 1) * 100 if prev else None)


def markets(cfg: dict, old: list) -> list:
    try:
        got = _cnbc(cfg)
        print(f"  ✓ CNBC: {len(got)} precios")
    except Exception as ex:
        print(f"  ✗ CNBC: {ex}")
        got = {}
    for t in cfg.get("mercados", []):
        if t["symbol"] not in got:
            try:
                got[t["symbol"]] = _yahoo(t["symbol"])
            except Exception as ex:
                print(f"  ✗ mercado {t['symbol']} (Yahoo): {ex}")
            time.sleep(0.5)
    prev = {m["symbol"]: m for m in old}
    res = []
    for t in cfg.get("mercados", []):
        sym = t["symbol"]
        if sym not in got:
            continue
        price, chg = got[sym]
        # minigráfico: se arma con los precios de cada actualización (últimas ~30 horas)
        spark = (prev.get(sym, {}).get("spark") or [])[-29:] + [round(price, 4)]
        res.append({"symbol": sym, "name": t.get("nombre", sym), "price": round(price, 4),
                    "change": round(chg, 2) if chg is not None else None, "spark": spark})
    print(f"Mercados: {len(res)} de {len(cfg.get('mercados', []))}")
    return res


def merge_markets(cfg, fresh, old):
    """Si un precio no se pudo actualizar, se conserva el último conocido."""
    got = {m["symbol"]: m for m in fresh}
    prev = {m["symbol"]: m for m in old}
    return [got.get(t["symbol"]) or prev.get(t["symbol"]) for t in cfg.get("mercados", [])
            if got.get(t["symbol"]) or prev.get(t["symbol"])]


# ---------------------------------------------------------------- traducción al español
ES_HINT = set("el la los las de del que y en un una por para con se su sus es al lo como mas pero tras sobre ante hoy".split())
EN_HINT = set("the of and to in for on with is are as at by from its after over says will that this".split())


def is_spanish(text: str) -> bool:
    words = re.findall(r"[a-záéíóúñ]+", (text or "").lower())
    es = sum(w in ES_HINT for w in words) + 2 * len(re.findall(r"[áéíóúñ¿¡]", (text or "").lower()))
    en = sum(w in EN_HINT for w in words)
    return es > en


def _gtx(texts: list) -> list:
    """Traducción gratuita (Google Translate público). Una línea por texto."""
    q = "\n".join(t.replace("\n", " ") for t in texts)
    url = "https://translate.googleapis.com/translate_a/single?client=gtx&sl=auto&tl=es&dt=t&q=" + urllib.parse.quote(q)
    data = json.loads(fetch(url, timeout=20))
    out = "".join(seg[0] for seg in data[0] if seg and seg[0])
    lines = [l.strip() for l in out.split("\n")]
    if len(lines) != len(texts):
        raise ValueError(f"se esperaban {len(texts)} líneas y llegaron {len(lines)}")
    return lines


def translate_items(items: list, prev: dict):
    """Traduce títulos y extractos que no estén en español. Reutiliza traducciones anteriores."""
    cache = {}
    for it in prev.get("items", []):
        if it.get("title_orig"):
            cache[it["title_orig"]] = it["title"]
        for a in it.get("also", []):
            if a.get("title_orig"):
                cache[a["title_orig"]] = a["title"]
    cache.update(prev.get("tr_cache") or {})

    fields = []  # (objeto, campo)
    for it in items:
        fields += [(it, "title"), (it, "summary")]
        fields += [(a, "title") for a in it.get("also", [])]
    pending = []
    for obj, f in fields:
        text = obj.get(f) or ""
        if not text or is_spanish(text):
            continue
        if text in cache:
            obj[f + "_orig"], obj[f] = text, cache[text]
        else:
            pending.append((obj, f, text))

    done = fail = 0
    streak = 0  # lotes fallidos seguidos: si el traductor nos bloquea, no se insiste
    batch, size = [], 0
    def flush(b):
        nonlocal done, fail, streak
        if not b:
            return
        res = None
        if streak < 3:
            for wait in (0, 3):
                time.sleep(wait)
                try:
                    res = _gtx([t for _, _, t in b])
                    break
                except ValueError:  # el lote volvió desordenado: se traduce de a uno
                    res = []
                    for _, _, t in b:
                        try:
                            res.append(_gtx([t])[0])
                        except Exception:
                            res.append(None)
                        time.sleep(0.2)
                    break
                except Exception:
                    continue
        streak = 0 if res else streak + 1
        for (obj, f, t), r in zip(b, res or [None] * len(b)):
            if r:
                obj[f + "_orig"], obj[f] = t, r
                cache[t] = r
                done += 1
            else:
                fail += 1
        time.sleep(0.3)
    for entry in pending:
        if size + len(entry[2]) > 1800 or len(batch) >= 25:
            flush(batch)
            batch, size = [], 0
        batch.append(entry)
        size += len(entry[2]) + 1
    flush(batch)
    print(f"Traducción: {done} textos nuevos, {len(fields) and sum(1 for o, f in fields if o.get(f + '_orig'))} en total, {fail} fallaron.")
    # se guardan aparte solo los extractos: los títulos ya quedan en cada noticia (title_orig)
    used = {o["summary_orig"]: o["summary"] for o, f in fields if f == "summary" and o.get("summary_orig")}
    for it in items:  # del extracto basta con la versión traducida
        it.pop("summary_orig", None)
    return used


# ---------------------------------------------------------------- IA (opcional)
def previous_output():
    try:
        with open(OUT, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def gemini(prompt: str, key: str):
    models = [m for m in [os.environ.get("GEMINI_MODEL"), "gemini-flash-latest", "gemini-flash-lite-latest", "gemini-2.5-flash"] if m]
    body = json.dumps({"contents": [{"parts": [{"text": prompt}]}],
                       "generationConfig": {"responseMimeType": "application/json", "temperature": 0.3}}).encode()
    for model in models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json", "x-goog-api-key": key})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                data = json.loads(r.read())
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            text = re.sub(r"^```(json)?|```$", "", text.strip()).strip()
            print(f"IA: respuesta de {model}.")
            return json.loads(text)
        except Exception as ex:
            print(f"IA: falló {model}: {ex}")
    return None


def ai_enrich(items: list, cfg: dict, prev: dict) -> dict:
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    interval = int(os.environ.get("AI_INTERVAL_MIN", "180"))
    prev_ai = prev.get("ai") or {}
    if not key:
        print("IA: sin GEMINI_API_KEY, se usan historias automáticas.")
        return {}
    try:
        if prev_ai.get("at") and (NOW - datetime.fromisoformat(prev_ai["at"])).total_seconds() < interval * 60:
            print(f"IA: se reutiliza el resultado de {prev_ai['at']}.")
            return prev_ai
    except Exception:
        pass

    top = items[:60]
    listado = "\n".join(f'{it["id"]} | {it["source"]} | {it["title"]} | {it["summary"][:160]}' for it in top)
    temas = ", ".join(cfg.get("temas", {}).keys())
    prompt = f"""Eres el analista personal de esta persona. Perfil: {cfg.get('perfil', '')}

Noticias más relevantes de las últimas horas (id | medio | título | extracto):
{listado}

Responde SOLO con JSON válido, todo en español neutro, tono sobrio de periódico económico (sin exclamaciones ni emojis):
{{
 "brief": ["4 a 6 frases. Cada una cuenta un hecho distinto y por qué importa. Máximo 35 palabras cada una."],
 "historias": [
   {{"titulo": "titular en español, máximo 12 palabras",
     "resumen": "2 o 3 frases con los datos clave",
     "por_que_importa": "1 frase sobre el impacto en empresas, empleo o inversión",
     "tema": "uno de: {temas}",
     "ids": ["ids de TODAS las noticias de la lista que tratan este mismo hecho"]}}
 ],
 "resumenes": {{"<id>": "1 frase en español: qué pasó"}},
 "titulos": {{"<id>": "el título traducido a un español natural, solo para los títulos que no estén en español"}}
}}
Reglas: 5 a 8 historias ordenadas por importancia; agrupa en una misma historia los titulares del mismo hecho aunque estén redactados distinto o en otro idioma. Incluye en "resumenes" todos los id. No inventes datos que no estén en la lista."""
    result = gemini(prompt, key)
    if not result:
        return prev_ai
    by_id = {it["id"]: it for it in top}
    stories = []
    for s in result.get("historias", [])[:8]:
        links = [by_id[i]["link"] for i in s.get("ids", []) if i in by_id]
        if links:
            stories.append({k: str(s.get(k, "")) for k in ("titulo", "resumen", "por_que_importa", "tema")} | {"links": links})
    summaries = {by_id[i]["link"]: str(t) for i, t in result.get("resumenes", {}).items() if i in by_id}
    titles = {by_id[i]["link"]: str(t) for i, t in (result.get("titulos") or {}).items() if i in by_id and t}
    return {"at": NOW.isoformat(timespec="seconds"), "brief": [str(p) for p in result.get("brief", [])][:6],
            "stories": stories, "summaries": summaries, "titles": titles}


def attach_ai(items: list, ai: dict) -> list:
    by_link = {}
    for it in items:
        by_link[it["link"]] = it["id"]
        for a in it["also"]:
            by_link.setdefault(a["link"], it["id"])
    for it in items:
        s = (ai.get("summaries") or {}).get(it["link"])
        if s:
            it["ai_summary"] = s
    stories = []
    for s in ai.get("stories", []):
        ids = list(dict.fromkeys(by_link[l] for l in s["links"] if l in by_link))
        if ids:
            stories.append({k: v for k, v in s.items() if k != "links"} | {"ids": ids, "ai": True})
    return stories


def auto_stories(items: list, trend_list: list, k: int = 5) -> list:
    """Sin IA: una historia por cada tendencia fuerte, con sus titulares."""
    stories, used = [], set()
    for t in trend_list:
        rx = re.compile(r"(?<![a-z0-9])" + re.escape(norm(t["term"])) + r"(?![a-z0-9])")
        group = [it for it in items[:80] if it["id"] not in used and rx.search(norm(it.get("title_orig") or it["title"]))]
        if len(group) < 3:
            continue
        lead = max(group[:8], key=lambda it: (bool(rx.search(norm(it.get("title_orig") or it["title"])[:len(it.get("title_orig") or it["title"]) // 2])), bool(it.get("ai_summary") or it["summary"]), it["n_sources"]))
        group.remove(lead)
        group.insert(0, lead)
        used.update(it["id"] for it in group)
        stories.append({"titulo": lead["title"], "resumen": lead.get("ai_summary") or lead["summary"],
                        "por_que_importa": "", "tema": lead["topics"][0], "ids": [it["id"] for it in group[:10]],
                        "foco": t["term"], "ai": False})
        if len(stories) >= k:
            break
    return stories


# ---------------------------------------------------------------- main
def main():
    with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    prev = previous_output()
    with ThreadPoolExecutor(max_workers=2) as pool:
        f_news, f_mk = pool.submit(collect, cfg), pool.submit(markets, cfg, prev.get("markets") or [])
        raw, mk = f_news.result(), f_mk.result()
    print(f"Noticias leídas: {len(raw)}")
    items = score_clusters(cluster(raw), cfg)
    print(f"Noticias relevantes tras filtrar y agrupar: {len(items)}")
    if not items:
        sys.exit("No se obtuvo ninguna noticia; se conserva el news.json anterior.")
    tr = trends(items)
    ai = ai_enrich(items, cfg, prev)
    for it in items:  # títulos traducidos por la IA (más naturales que la traducción automática)
        t = (ai.get("titles") or {}).get(it["link"])
        if t and not is_spanish(it["title"]):
            it["title_orig"], it["title"] = it["title"], t
    tr_cache = translate_items(items, prev) if os.environ.get("TRADUCIR", "1") != "0" else {}
    stories = attach_ai(items, ai) or auto_stories(items, tr)
    out = {
        "generated_at": NOW.isoformat(timespec="seconds"),
        "brief": ai.get("brief") or None,
        "stories": stories,
        "trends": tr,
        "markets": merge_markets(cfg, mk, prev.get("markets") or []),
        "ai": ai or None,
        "items": items,
        "tr_cache": tr_cache,
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    print(f"Listo: news.json ({os.path.getsize(OUT)//1024} KB), {len(stories)} historias, {len(tr)} tendencias.")


if __name__ == "__main__":
    main()
