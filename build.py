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
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(read_source, sources))
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
STOOQ = {"^GSPC": "^spx", "^IXIC": "^ndq", "^DJI": "^dji", "BTC-USD": "btcusd", "ETH-USD": "ethusd",
         "GC=F": "xauusd", "CL=F": "cl.f", "EURUSD=X": "eurusd"}


def _yahoo(sym):
    for host in ("query1", "query2"):
        url = f"https://{host}.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(sym)}?range=1mo&interval=1d"
        try:
            r = json.loads(fetch(url, timeout=12))["chart"]["result"][0]
            closes = [c for c in r["indicators"]["quote"][0]["close"] if c is not None]
            price = r["meta"].get("regularMarketPrice") or closes[-1]
            if closes and abs(closes[-1] - price) / price > 0.0005:
                closes.append(price)  # el día de hoy aún no tiene vela diaria
            return closes, price
        except Exception as ex:
            last = ex
            time.sleep(1.5)
    raise last


def _stooq(sym, t):
    code = t.get("stooq") or STOOQ.get(sym) or (sym.lower() + ".us")
    d1 = datetime.fromtimestamp(NOW.timestamp() - 40 * 86400, tz=timezone.utc).strftime("%Y%m%d")
    url = f"https://stooq.com/q/d/l/?s={urllib.parse.quote(code)}&i=d&d1={d1}&d2={NOW.strftime('%Y%m%d')}"
    rows = fetch(url, timeout=12).decode().strip().splitlines()[1:]
    closes = [float(r.split(",")[4]) for r in rows if r.count(",") >= 4 and r.split(",")[4] not in ("", "null")]
    if len(closes) < 2:
        raise ValueError("sin datos")
    return closes, closes[-1]


def markets(cfg: dict) -> list:
    res = []
    for t in cfg.get("mercados", []):  # de uno en uno: los proveedores gratis bloquean ráfagas
        sym = t["symbol"]
        closes = price = None
        for name, fn in (("Yahoo", lambda: _yahoo(sym)), ("Stooq", lambda: _stooq(sym, t))):
            try:
                closes, price = fn()
                break
            except Exception as ex:
                print(f"  ✗ mercado {sym} ({name}): {ex}")
        if closes:
            prev = closes[-2] if len(closes) >= 2 else None
            res.append({"symbol": sym, "name": t.get("nombre", sym), "price": round(price, 4),
                        "change": round((price / prev - 1) * 100, 2) if prev else None,
                        "spark": [round(c, 4) for c in closes[-22:]]})
        time.sleep(0.4)
    print(f"Mercados: {len(res)} de {len(cfg.get('mercados', []))}")
    return res


def merge_markets(cfg, fresh, old):
    """Si un precio no se pudo actualizar, se conserva el último conocido."""
    got = {m["symbol"]: m for m in fresh}
    prev = {m["symbol"]: m for m in old}
    return [got.get(t["symbol"]) or prev.get(t["symbol"]) for t in cfg.get("mercados", [])
            if got.get(t["symbol"]) or prev.get(t["symbol"])]


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
 "resumenes": {{"<id>": "1 frase en español: qué pasó"}}
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
    return {"at": NOW.isoformat(timespec="seconds"), "brief": [str(p) for p in result.get("brief", [])][:6],
            "stories": stories, "summaries": summaries}


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
        group = [it for it in items[:80] if it["id"] not in used and rx.search(norm(it["title"]))]
        if len(group) < 3:
            continue
        lead = max(group[:6], key=lambda it: (bool(it.get("ai_summary") or it["summary"]), it["n_sources"]))
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
        f_news, f_mk = pool.submit(collect, cfg), pool.submit(markets, cfg)
        raw, mk = f_news.result(), f_mk.result()
    print(f"Noticias leídas: {len(raw)}")
    items = score_clusters(cluster(raw), cfg)
    print(f"Noticias relevantes tras filtrar y agrupar: {len(items)}")
    if not items:
        sys.exit("No se obtuvo ninguna noticia; se conserva el news.json anterior.")
    tr = trends(items)
    ai = ai_enrich(items, cfg, prev)
    stories = attach_ai(items, ai) or auto_stories(items, tr)
    out = {
        "generated_at": NOW.isoformat(timespec="seconds"),
        "brief": ai.get("brief") or None,
        "stories": stories,
        "trends": tr,
        "markets": merge_markets(cfg, mk, prev.get("markets") or []),
        "ai": ai or None,
        "items": items,
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    print(f"Listo: news.json ({os.path.getsize(OUT)//1024} KB), {len(stories)} historias, {len(tr)} tendencias.")


if __name__ == "__main__":
    main()
