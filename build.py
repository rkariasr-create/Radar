#!/usr/bin/env python3
"""
Radar 360 — junta noticias de muchas fuentes (RSS + Google News),
quita duplicados, las puntúa según tu perfil (config.json) y genera news.json.

Si existe la variable GEMINI_API_KEY (gratis en Google AI Studio), además
pide a la IA un "brief del día" y un resumen en español de las noticias top.
Sin esa clave todo funciona igual, solo sin resúmenes de IA.
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
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import feedparser

ROOT = os.path.dirname(os.path.abspath(__file__))
UA = "Mozilla/5.0 (compatible; Radar360/1.0; +https://github.com)"
NOW = datetime.now(timezone.utc)

STOPWORDS = set("""
a al algo ante como con contra de del desde donde el ella ellos en entre era es esa ese esta este fue ha han hay la las le les lo los mas me muy no nos o para pero por que se sin sobre son su sus tambien un una unos y ya
the a an and or of to in on for with at by from is are was were be been it its this that as after over into new says say will can how why what who than more about up out his her their your our not but has have
""".split())


# ---------------------------------------------------------------- utilidades
def norm(text: str) -> str:
    """minúsculas, sin acentos, espacios simples."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", text.lower()).strip()


def strip_tags(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def kw_regex(words):
    """Regex con límites de palabra para una lista de palabras clave."""
    parts = [re.escape(norm(w)) for w in words if w.strip()]
    if not parts:
        return None
    return re.compile(r"(?<![a-z0-9])(" + "|".join(sorted(parts, key=len, reverse=True)) + r")(?![a-z0-9])")


def tokens(title: str) -> set:
    words = re.findall(r"[a-z0-9]+", norm(title))
    return {w for w in words if len(w) > 2 and w not in STOPWORDS}


def gnews_url(q: str, idioma: str) -> str:
    if idioma == "es":
        hl, gl, ceid = "es-419", "US", "US:es-419"
    else:
        hl, gl, ceid = "en-US", "US", "US:en"
    query = urllib.parse.quote(f"{q} when:2d")
    return f"https://news.google.com/rss/search?q={query}&hl={hl}&gl={gl}&ceid={ceid}"


def fetch(url: str, timeout: int = 20) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/rss+xml, application/xml, text/xml, */*"})
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


# ---------------------------------------------------------------- recolección
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
        # En Google News el medio real viene en <source> y al final del título
        if src.get("google"):
            gsrc = e.get("source") or {}
            real = gsrc.get("title") if isinstance(gsrc, dict) else None
            if not real and " - " in title:
                real = title.rsplit(" - ", 1)[1]
            if real:
                source_name = real.strip()
                if title.endswith(" - " + source_name):
                    title = title[: -len(" - " + source_name)].strip()
        summary = "" if src.get("google") else strip_tags(e.get("summary", ""))[:320]
        items.append({
            "title": title,
            "link": link,
            "source": source_name,
            "published": entry_time(e),
            "summary": summary,
            "feed_topic": src.get("tema"),
            "feed_weight": src.get("peso", 0),
        })
    print(f"  ✓ {name}: {len(items)}")
    return items


def collect(cfg: dict) -> list:
    sources = list(cfg.get("fuentes", []))
    for b in cfg.get("busquedas_google_news", []):
        sources.append({
            "nombre": f"Google News · {b['q']}",
            "url": gnews_url(b["q"], b.get("idioma", "es")),
            "tema": b.get("tema"),
            "google": True,
        })
    print(f"Leyendo {len(sources)} fuentes…")
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(read_source, sources))
    return [it for batch in results for it in batch]


# ---------------------------------------------------------------- puntuación
def cluster(items: list) -> list:
    """Agrupa la misma noticia publicada por varios medios."""
    clusters = []
    for it in sorted(items, key=lambda x: x["published"] or NOW, reverse=True):
        tk = tokens(it["title"])
        it["_tk"] = tk
        placed = False
        if len(tk) >= 3:
            for c in clusters:
                inter = len(tk & c["_tk"])
                union = len(tk | c["_tk"]) or 1
                if inter >= 3 and inter / union >= 0.45:
                    c["members"].append(it)
                    placed = True
                    break
        if not placed:
            clusters.append({"_tk": tk, "members": [it]})
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
        lead = max(members, key=lambda m: (m["feed_weight"], len(m["summary"])))
        text = norm(" ".join(m["title"] + " " + m["summary"] for m in members))
        if exclude_re and exclude_re.search(text):
            continue
        newest = max((m["published"] for m in members if m["published"]), default=None)
        age_h = (NOW - newest).total_seconds() / 3600 if newest else 24
        if age_h > max_age:
            continue

        feed_topics = [m["feed_topic"] for m in members if m["feed_topic"] in topics]
        topic_scores = {}
        for key, rx in topic_re.items():
            hits = len(set(rx.findall(text))) if rx else 0
            # el tema de la fuente suma solo si la mayoría de medios lo comparte
            if feed_topics.count(key) * 2 >= len(members):
                hits += 1
            if hits:
                topic_scores[key] = min(hits, 3) * topics[key].get("peso", 1)
        if not topic_scores and feed_topics:
            key = max(set(feed_topics), key=feed_topics.count)
            topic_scores[key] = topics[key].get("peso", 1)
        if not topic_scores:
            continue

        n_sources = len({m["source"] for m in members})
        urgent = len(set(urgent_re.findall(text))) if urgent_re else 0
        base = sum(topic_scores.values()) + 6 * math.log2(n_sources) + 2 * min(urgent, 2)
        base += max(m["feed_weight"] for m in members)
        recency = 0.35 + 0.65 * math.exp(-max(age_h, 0) / 16)
        score = round(base * recency, 2)

        seen, others = {lead["source"]}, []
        for m in members:
            if m["source"] not in seen:
                seen.add(m["source"])
                others.append({"source": m["source"], "link": m["link"]})

        out.append({
            "id": f"n{len(out)}",
            "title": lead["title"],
            "link": lead["link"],
            "source": lead["source"],
            "published": newest.isoformat() if newest else None,
            "summary": lead["summary"],
            "topics": sorted(topic_scores, key=topic_scores.get, reverse=True),
            "n_sources": n_sources,
            "also": others[:6],
            "urgent": urgent > 0,
            "score": score,
        })
    out.sort(key=lambda x: x["score"], reverse=True)
    for i, it in enumerate(out):
        it["id"] = f"n{i}"
    return out[: cfg.get("max_noticias", 150)]


# ---------------------------------------------------------------- IA opcional
def ai_enrich(items: list, cfg: dict):
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key or not items:
        print("IA: sin GEMINI_API_KEY, se omiten resúmenes.")
        return None
    top = items[:40]
    listado = "\n".join(f'{it["id"]} | {it["source"]} | {it["title"]} | {it["summary"][:200]}' for it in top)
    prompt = f"""Eres el analista personal de esta persona. Perfil: {cfg.get('perfil', '')}

Estas son las noticias más relevantes de las últimas horas (id | medio | título | extracto):
{listado}

Responde SOLO con JSON válido con esta forma:
{{"brief": ["5 a 7 puntos en español, cada uno de 1-2 frases: qué pasó y por qué importa para las empresas o los mercados"],
  "resumenes": {{"<id>": "1 frase en español: qué pasó y su impacto"}}}}
Incluye en "resumenes" todos los id. No inventes datos que no estén en los titulares."""
    models = [m for m in [os.environ.get("GEMINI_MODEL"), "gemini-flash-latest", "gemini-2.5-flash"] if m]
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.3},
    }).encode()
    for model in models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json", "x-goog-api-key": key})
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                data = json.loads(r.read())
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            text = re.sub(r"^```(json)?|```$", "", text.strip()).strip()
            result = json.loads(text)
            res = result.get("resumenes", {})
            for it in items:
                if it["id"] in res:
                    it["ai_summary"] = str(res[it["id"]])
            print(f"IA: brief generado con {model}.")
            return [str(p) for p in result.get("brief", [])][:7]
        except Exception as ex:
            print(f"IA: falló {model}: {ex}")
    return None


# ---------------------------------------------------------------- main
def main():
    with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    raw = collect(cfg)
    print(f"Noticias leídas: {len(raw)}")
    items = score_clusters(cluster(raw), cfg)
    print(f"Noticias relevantes tras filtrar y agrupar: {len(items)}")
    if not items:
        sys.exit("No se obtuvo ninguna noticia; se conserva el news.json anterior.")
    brief = ai_enrich(items, cfg)
    out = {"generated_at": NOW.isoformat(), "brief": brief, "items": items}
    with open(os.path.join(ROOT, "news.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print("Listo: news.json actualizado.")


if __name__ == "__main__":
    main()
