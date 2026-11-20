# Radar 360

Tu página propia de noticias de IA, startups, economía, bolsa, bitcoin y marketing.
Se actualiza sola cada 3 horas y cuesta $0 (GitHub Pages + GitHub Actions + Google AI Studio gratis).

## Archivos
- `index.html`: la página.
- `config.json`: tu perfil, los temas, las palabras clave, las fuentes y los tickers. **Lo único que necesitas editar.**
- `build.py`: lee las fuentes, quita duplicados, puntúa y genera `news.json`.
- `.github/workflows/actualizar.yml`: corre `build.py` cada 3 horas y publica la página.

## Personalizar (editando config.json en GitHub)
- **Temas**: agrega palabras a `"palabras"` y sube `"peso"` (1 a 5) a lo que más te importe.
- **Fuentes**: añade un bloque `{ "nombre": "...", "url": "URL del RSS", "tema": "ia" }`.
- **Búsquedas**: en `busquedas_google_news` agrega cualquier tema, por ejemplo `{ "q": "Ecuador economía", "idioma": "es", "tema": "economia" }`.
- **Acciones**: en `tickers` agrega por ejemplo `{ "proName": "NYSE:KO", "title": "Coca-Cola" }`.
- **Ignorar**: palabras en `excluir`.

Cada vez que guardas un cambio, la página se reconstruye sola en 1 a 2 minutos.

## Si algo falla
- En **Actions** abre la última ejecución. Las fuentes con ✗ no respondieron; puedes borrarlas de `config.json`.
- Si GitHub te avisa por correo que desactivó el workflow por inactividad, entra a Actions y pulsa **Enable workflow**.

## Botón «Consultar» (asistente)
- **💬 Consultar** (abajo a la derecha), **💬 Preguntar sobre esto** en cada noticia, o **selecciona cualquier palabra** y toca «¿Qué es…?».
- Por defecto abre Claude (app o web) con tu pregunta y el contexto de la noticia ya escritos: $0 con tu cuenta de Claude.
- Para respuestas dentro de la página: ⚙️ → Gemini → pega tu clave gratis de aistudio.google.com. La clave se guarda solo en tu dispositivo, nunca en GitHub.
- Opción de pago: ⚙️ → Claude API, con una clave de console.anthropic.com.
- **Nunca pongas claves en config.json**: el repositorio es público.
