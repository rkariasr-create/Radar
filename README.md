# Radar

Tu resumen diario de IA, startups, economía, bolsa, cripto y marketing. Se actualiza solo cada hora y cuesta $0
(GitHub Pages + GitHub Actions + Google AI Studio gratis).

## Qué hace
- Lee unas 27 fuentes (medios y búsquedas de Google News) y junta la misma noticia publicada por varios medios.
- Ordena todo según tu perfil y detecta los nombres que más se repiten (En tendencia).
- Con la clave de Gemini escribe en español el resumen del día, las historias principales y un resumen por noticia.
- Muestra precios de mercado (acciones con retraso, cripto en vivo).
- Botón **Preguntar**: explica cualquier término o noticia (abre Claude, o responde dentro de la página con Gemini).
- Se puede instalar en el celular como app y abre aunque no haya conexión.

## Archivos
- `index.html`: la página. `sw.js` y `manifest.webmanifest`: la hacen instalable y rápida.
- `config.json`: tu perfil, temas, palabras clave, fuentes y mercados. **Lo único que necesitas editar.**
- `build.py`: genera `news.json`. `.github/workflows/actualizar.yml`: lo corre cada hora y publica la página.

## Activar los resúmenes en español (recomendado)
1. Crea una clave gratis en aistudio.google.com (Get API key).
2. En GitHub: Settings, Secrets and variables, Actions, New repository secret.
   Nombre `GEMINI_API_KEY`, valor: tu clave.
3. Actions, Actualizar Radar, Run workflow.

La IA corre como máximo cada 3 horas para no pasar el límite gratis; entre medias se reutiliza el último resumen.

## Personalizar (editando config.json en GitHub)
- **Temas**: agrega palabras a `palabras` y sube `peso` (1 a 5) a lo que más te importe.
- **Fuentes**: `{ "nombre": "...", "url": "URL del RSS", "tema": "ia" }`.
- **Búsquedas**: en `busquedas_google_news`, por ejemplo `{ "q": "Ecuador economía", "idioma": "es", "tema": "economia" }`.
- **Mercados**: en `mercados`, con el símbolo de Yahoo Finance, por ejemplo `{ "symbol": "KO", "nombre": "Coca-Cola" }`.
- **Ignorar**: palabras en `excluir`.

**Nunca pongas claves en config.json**: el repositorio es público.
