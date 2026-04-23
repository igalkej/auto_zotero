# plan_03_subsystem3.md — Subsistema 3: Acceso via MCP

**Estado**: Spec cerrada. Implementación ligera (~4-6h).
**Estimación**: medio día de desarrollo.
**Complejidad relativa**: baja. Mayormente configuración, no código propio.

---

## 1. Propósito del subsistema

Permitir al investigador consultar su biblioteca Zotero desde Claude Desktop mediante lenguaje natural, cubriendo tres modos de uso:

- **Descubrimiento interno** (60% del uso): *"¿qué tengo sobre X?"*
- **Cita de respaldo** (30%): *"necesito fuente para esta afirmación"*
- **Síntesis puntual** (10%, safeguard): *"¿este paper específico dice realmente X?"*

**Este subsistema no se desarrolla desde cero**: se construye sobre `zotero-mcp` (de 54yyyu), un proyecto MCP server existente, maduro, con semantic search y annotations.

---

## 2. Criterios de éxito

- **Descubrimiento (recall@20)**: ≥80% en queries donde se conoce la respuesta esperada. Test: 10 temas conocidos del usuario, verificar que ≥8 recuperan >80% de los papers relevantes.
- **Cita (precision@3)**: ≥70%. Test: 10 afirmaciones del usuario, verificar que al menos 7 de las top-3 sugerencias son válidas como cita.
- **Síntesis puntual (fidelidad)**: 0 alucinaciones detectadas en 10 verificaciones manuales.
- **Latencia**: <5s para queries de descubrimiento, <15s para síntesis puntual.

---

## 3. Anti-objetivos

- **No desarrollar MCP server propio**: `zotero-mcp` existe y es maduro.
- **No forkear** `zotero-mcp` salvo necesidad específica documentada.
- **No síntesis temática multi-paper** en v1 (queda fuera por tensión 4 del producto).
- **No integración con editores** (Overleaf/Jupyter/Docs) en v1 — el `.bib` exportado por Better BibTeX lo resuelve por afuera.

---

## 4. Componentes

### 4.1 `zotero-mcp` server

**Fuente**: https://github.com/54yyyu/zotero-mcp

**Instalación**: via `uv tool install "zotero-mcp-server[semantic]"`.

**Configuración clave**:
- Embeddings: **OpenAI `text-embedding-3-large`** (no el default MiniLM; ver ADR 004).
- Fulltext indexing: **sí** (`update-db --fulltext`).
- Local API: sí (Zotero Desktop abierto).

**Herramientas MCP que expone**:
- `zotero_search` — búsqueda por keywords.
- `zotero_semantic_search` — búsqueda por significado.
- `zotero_fulltext` — texto completo de un paper.
- `zotero_item_details` — metadata completa.
- `zotero_pdf_annotations` — anotaciones y highlights.

### 4.2 Claude Desktop

**Config file**:
- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`
- Linux: `~/.config/Claude/claude_desktop_config.json`

**Contenido**:
```json
{
  "mcpServers": {
    "zotero": {
      "command": "zotero-mcp",
      "args": ["serve"],
      "env": {
        "ZOTERO_LOCAL": "true",
        "ZOTERO_EMBEDDING_MODEL": "openai",
        "OPENAI_EMBEDDING_MODEL": "text-embedding-3-large",
        "OPENAI_API_KEY": "${OPENAI_API_KEY}"
      }
    }
  }
}
```

### 4.3 ChromaDB local

Bajo **ADR 015**, S2 es el owner del índice y S3 (`zotero-mcp serve`) es lector puro. La inversión respecto al diseño original simplifica el mantenimiento (no hay que sincronizar dos paths, ni invocar `zotero-mcp update-db` cron / manual) y elimina la ventana de staleness silenciosa.

- **Path en el host** (donde S3 lee): `~/.config/zotero-mcp/chroma_db/` por default. Configurable en `.env` via `ZOTERO_MCP_CHROMA_HOST_PATH` — el path debe coincidir con lo que `zotero-mcp setup` haya elegido al configurar Claude Desktop.
- **Path en el container S2** (donde S2 escribe): `/workspace/chroma_db`. Es siempre este valor, consistente con la convención `/workspace/*` del resto del container. `S2_CHROMA_PATH=/workspace/chroma_db` por default.
- **El puente**: el servicio `dashboard` de `docker-compose.yml` monta el path del host en `/workspace/chroma_db:rw`. Ver **ADR 011** (`docs/decisions/011-chromadb-bind-mount.md`) — mecanismo del bind mount, ahora amended para `:rw` por ADR 015.
- **S2 escribe, S3 lee.** No se ejecuta `zotero-mcp update-db` en ningún flujo operativo del proyecto. La actualización del índice ocurre dentro de S2: backfill inicial via `zotai s2 backfill-index`, mantenimiento continuo via reconciliación por diff en cada ciclo del worker (paso 0, ver `plan_02` §4 / §9). Schema de lo que S2 escribe está documentado en ADR 015 §6 — contrato explícito para que `zotero-mcp serve` pueda leerlo de manera estable.

---

## 5. Instalación: guía paso a paso

Este subsistema es más guía que código. El deliverable principal es documentación clara.

### 5.1 Prerequisitos

- Python 3.11+ en el sistema (no solo en Docker; `zotero-mcp` corre en el host).
- Zotero 7 con API local habilitada.
- Claude Desktop instalado.
- Biblioteca Zotero poblada (S1 completado).
- OpenAI API key.

### 5.2 Instalación

```bash
# 1. Instalar uv si no está (macOS/Linux)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows:
# powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

# 2. Instalar zotero-mcp
uv tool install "zotero-mcp-server[semantic]"

# 3. Setup interactivo
zotero-mcp setup
# Responder:
#   - Use local Zotero API: yes
#   - Embedding model: openai
#   - OpenAI model: text-embedding-3-large
#   - Provide OpenAI API key
# IMPORTANTE: anotar el path de ChromaDB que setup elige y, si difiere
# del default (~/.config/zotero-mcp/chroma_db), setear
# ZOTERO_MCP_CHROMA_HOST_PATH en .env del container S2 para que
# coincidan. ADR 011 explica por qué la coordinación es necesaria.

# 4. Verificar conectividad MCP (sin construir índice)
zotero-mcp db-status
# El índice se construye desde S2 con `zotai s2 backfill-index`,
# NO con `zotero-mcp update-db`. Bajo ADR 015 update-db no se usa
# en ningún flujo operativo del proyecto.
```

### 5.3 Configurar Claude Desktop

1. Abrir Claude Desktop.
2. Editar `claude_desktop_config.json` (path según OS).
3. Pegar la config de 4.2.
4. Reiniciar Claude Desktop.
5. Verificar: ícono de tools aparece en el chat, `zotero_*` tools disponibles.

### 5.4 Validación

Script de testing: `scripts/validate-s3.py`.

Corre automáticamente 10 queries de prueba y reporta:
- Tasa de respuesta correcta (recall@20).
- Tiempo promedio.
- Errores.

---

## 6. Protocolo de uso recomendado (para usuarios)

Documento `docs/s3-usage.md` con:

### 6.1 Modo descubrimiento

Query natural:
> *"Buscá en mi Zotero papers sobre política fiscal anticíclica en economías emergentes."*

Prompt adicional sugerido para mejor recall:
> *"Usá búsqueda semántica, no solo keywords. Devolveme 10-15 resultados ordenados por relevancia, con título, autores, año, y una frase explicando por qué matchea."*

### 6.2 Modo cita

Query:
> *"Estoy escribiendo: 'La informalidad en América Latina se correlaciona negativamente con productividad.' ¿Qué papers de mi Zotero pueden respaldar esta afirmación?"*

**Safeguard obligatorio**:
> *"De los 3 top candidatos, abrí el fulltext de cada uno y verificá: ¿el paper efectivamente hace esa afirmación? Citame el passage exacto."*

Este paso SIEMPRE es manual. El usuario no confía en la sugerencia sin verificación.

### 6.3 Modo síntesis puntual

Query:
> *"Leé el paper de [Autor, Año] completo y decime si discute [tema específico]. Dame citas textuales si sí."*

Claude usa `zotero_fulltext` para acceder al PDF via Zotero, lee, responde.

---

## 7. Mantenimiento

### 7.1 Re-indexación

**Bajo ADR 015 esto es responsabilidad de S2, no del usuario ni de S3.** El reconcile corre automáticamente como paso 0 de cada ciclo del worker (default cada 6h via APScheduler — ver ADR 012). Si querés forzar un reconcile fuera de ciclo:

```bash
docker compose run --rm onboarding zotai s2 reconcile
```

Para inspeccionar el estado actual del índice (cuántos docs, último ciclo, pendientes, huérfanos), abrir el dashboard de S2 en `http://127.0.0.1:8000/metrics`.

**Casos especiales:**
- **Bibliografía recién migrada** (S1 corrió, ChromaDB vacía): el primer comando es `zotai s2 backfill-index` con `S2_MAX_COST_USD_BACKFILL` como cap. Tarda 30-60 min para ~1500 papers, cuesta $1-2.
- **Cambio de modelo de embeddings**: vaciar ChromaDB manualmente (`rm -rf $ZOTERO_MCP_CHROMA_HOST_PATH`) y re-correr `backfill-index`. El cambio de modelo es muy infrecuente; no hay automatización para esto.
- **Index corrupto o sospechoso**: el reconcile es auto-curativo — borra y re-embebe lo que detecte como divergente. No hay comando "rebuild" porque no hace falta.

**No usar `zotero-mcp update-db`.** Bajo ADR 015 ese comando no es parte del flujo operativo del proyecto; usarlo manualmente puede generar un schema inconsistente con el que escribe S2 (ver ADR 015 §6 para el contrato).

### 7.2 Troubleshooting

Ver `docs/troubleshooting.md` → sección S3.

Problemas comunes:
- **MCP server no aparece en Claude**: verificar path del comando, reiniciar Claude Desktop.
- **Semantic search devuelve vacío**: verificar `zotero-mcp db-status`, rebuild si hace falta.
- **Alto consumo de tokens**: los resultados del MCP son verbose por default. Prompts del usuario pueden pedir outputs más compactos.

---

## 8. Integración con S2 (shared ChromaDB, S2 es owner)

Bajo **ADR 015**, la dirección de la integración se invierte respecto a versiones previas:

- **S2 es el owner del store**. Mantiene el invariante "todo item no-cuarentenado en Zotero está indexado" via `reconcile_embeddings()` en cada ciclo del worker. Schema documentado en ADR 015 §6.
- **S3 (`zotero-mcp serve`) lee el mismo store** sobre el path host. No invoca `zotero-mcp update-db` en ningún flujo del proyecto.
- **Un solo store físico en disco**: el que vive en `${ZOTERO_MCP_CHROMA_HOST_PATH:-$HOME/.config/zotero-mcp/chroma_db}` en el host. Sin copia, sin sync job. Bind-mounted al container de S2 como `/workspace/chroma_db:rw` (ver ADR 011 amended por ADR 015).
- **Si ChromaDB tiene <`semantic_scoring.min_corpus_size` documentos**: S2 degrada gracefully, `score_semantic=neutral_fallback` (0.5). El dashboard muestra un warning apuntando a "ejecutá `zotai s2 backfill-index`".
- **Compatibilidad de schema**: validada empíricamente (Fase 2 del plan de implementación de ADR 015). Cualquier upgrade de `zotero-mcp` debe re-validarse contra el script `scripts/validate_chromadb_schema.py`; el resultado se loguea en CHANGELOG.

---

## 9. Deliverables de la implementación de S3

- [ ] `docs/s3-setup.md` con guía paso a paso por OS — incluyendo coordinación de `ZOTERO_MCP_CHROMA_HOST_PATH` con la config de zotero-mcp (ADR 011 + ADR 015).
- [ ] `docs/s3-usage.md` con prompts recomendados para cada modo.
- [ ] `scripts/validate-s3.py` con queries de prueba automatizadas (recall@20, latencia).
- [ ] `docs/s3-troubleshooting.md` con problemas comunes — incluyendo "MCP no encuentra docs tras upgrade de zotero-mcp" → re-correr `scripts/validate_chromadb_schema.py`.
- [ ] ADR 006 (zotero-mcp como upstream MCP server) y ADR 009 (S1/S2 usan pyzotero, S3 usa MCP) **ya escritos en PR #36**, parcialmente superseded por ADR 015.

**Removed deliverables (eran parte del diseño previo, ya no aplican bajo ADR 015):**
- ~~Script helper `scripts/reindex-s3.sh` (y `.ps1`)~~. La re-indexación es responsabilidad de S2 (`zotai s2 reconcile` o el ciclo automático del worker), no del usuario via shell. Ver §7.1.
- ~~Paso "Build del índice inicial" en §5.2~~. El backfill se dispara desde S2 con `zotai s2 backfill-index`.

---

## 10. Fuera de alcance

- Custom MCP server.
- Integración con editores (Overleaf, Jupyter, Docs).
- Citation networks.
- Cross-library queries (multi-usuario).
- Agentic workflows (Claude decide solo cuándo consultar Zotero vs otra fuente).

---

## 11. Orden de ejecución en contexto del proyecto

Este subsistema se implementa **después del S1**, **antes del S2**.

Razones:
- Requiere biblioteca poblada (S1).
- Es el primer punto donde el usuario experimenta valor del producto (cierra MVP).
- Su ChromaDB es usada por el S2, por lo que debe estar operativo antes de que el S2 dependa de ella.
