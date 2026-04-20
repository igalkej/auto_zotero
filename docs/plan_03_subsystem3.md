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

- Path: `~/.config/zotero-mcp/chroma_db/` (default) o configurable.
- **Shared con S2**: `S2_CHROMA_PATH` en `.env` apunta a este mismo path. S2 lee-only.

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

# 4. Build del índice inicial
zotero-mcp update-db --fulltext
# Tarda ~30-60 min para 1500 papers.
# Costo: ~$1-2 en embeddings.

# 5. Verificar
zotero-mcp db-status
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

Cuando:
- S2 agrega nuevos papers → el índice de ChromaDB se desincroniza.
- Se cambia el modelo de embeddings.
- Pasa >1 mes sin update.

Comando:
```bash
zotero-mcp update-db          # incremental, solo nuevos
zotero-mcp update-db --force-rebuild   # rebuild completo
```

**Automatización**: cron job diario que corre `update-db`, configurado en `docs/s3-setup.md`. O manualmente tras cada sesión de triage en S2.

### 7.2 Troubleshooting

Ver `docs/troubleshooting.md` → sección S3.

Problemas comunes:
- **MCP server no aparece en Claude**: verificar path del comando, reiniciar Claude Desktop.
- **Semantic search devuelve vacío**: verificar `zotero-mcp db-status`, rebuild si hace falta.
- **Alto consumo de tokens**: los resultados del MCP son verbose por default. Prompts del usuario pueden pedir outputs más compactos.

---

## 8. Integración con S2 (shared ChromaDB)

S2 usa el mismo ChromaDB para su criterio `score_semantic`. Implicancias:

- **Path compartido**: configurar `S2_CHROMA_PATH` en `.env` al mismo path que `zotero-mcp` (default `~/.config/zotero-mcp/chroma_db/`).
- **S2 solo lee**: nunca escribe a ChromaDB, solo queries.
- **Si ChromaDB vacía o desincronizada**: S2 degrada gracefully, `score_semantic=0.5` (neutral).

---

## 9. Deliverables de la implementación de S3

- [ ] `docs/s3-setup.md` con guía paso a paso por OS.
- [ ] `docs/s3-usage.md` con prompts recomendados para cada modo.
- [ ] `scripts/validate-s3.py` con queries de prueba automatizadas.
- [ ] `docs/s3-troubleshooting.md` con problemas comunes.
- [ ] `docs/decisions/006-zotero-mcp.md` ADR justificando no desarrollar custom.
- [ ] Script helper `scripts/reindex-s3.sh` (y `.ps1`) para re-indexación fácil.
- [ ] Verificación de que `S2_CHROMA_PATH` está documentado coherentemente entre S2 y S3.

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
