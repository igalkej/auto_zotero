# Setup — Linux

Guía paso a paso para correr el toolkit en Linux (Ubuntu 22.04+, Fedora
39+, Arch). Si algo falla, mirá `docs/troubleshooting.md`.

---

## 1. Prerequisitos

1. **Docker Engine** 20.10+ con el plugin `docker-compose-v2`.
   ```bash
   # Ubuntu / Debian (siguiendo la guía oficial de Docker):
   curl -fsSL https://get.docker.com | sudo sh
   sudo usermod -aG docker "$USER"  # re-login después
   ```
   Verificar: `docker --version && docker compose version`.
2. **Zotero 7** desktop, con la API local habilitada:
   Preferences → Advanced → "Allow other applications on this computer
   to communicate with Zotero". La API vive en `http://localhost:23119`.
3. **API key de Zotero** — https://www.zotero.org/settings/keys (read+write
   sobre tu biblioteca personal).
4. **API key de OpenAI** — https://platform.openai.com/api-keys.
5. **Python 3.11+** en el host *solo si vas a configurar S3* (el MCP
   server corre en el host, fuera de Docker). Para S1 únicamente no
   hace falta.

---

## 2. Clonar e instalar

```bash
git clone https://github.com/igalkej/auto_zotero.git zotero-ai-toolkit
cd zotero-ai-toolkit
cp .env.example .env
```

Editá `.env` con tus credenciales. Los campos mínimos para S1:

```bash
ZOTERO_API_KEY=...
ZOTERO_LIBRARY_ID=...          # userID de https://www.zotero.org/settings/keys
OPENAI_API_KEY=sk-...
PDF_SOURCE_FOLDERS=/data/folder1,/data/folder2   # rutas dentro del container
USER_EMAIL=tu@email.com        # requerido por OpenAlex para el "polite pool"
```

Montá tus carpetas de PDFs en `./data/` del repo (por ejemplo
`ln -s ~/Descargas/papers data/folder1`). El `docker-compose.yml`
monta `./data` en `/data:ro`.

---

## 3. Verificar networking

En Linux la red bridge de Docker habla con el host a través de
`host.docker.internal`, que el proyecto inyecta via
`extra_hosts: "host.docker.internal:host-gateway"` en
`docker-compose.yml` (ver `docs/decisions/013-bridge-networking-host-docker-internal.md`).
No necesitás hacer nada extra — sólo asegurate de que Zotero Desktop
esté abierto **en el mismo host** donde corrés Docker.

Si corrés Zotero en otra máquina, editá `.env`:

```bash
ZOTERO_LOCAL_API_HOST=http://<IP-de-Zotero>:23119
```

---

## 4. Primera corrida

```bash
# Status — siempre seguro; no escribe nada.
docker compose --profile onboarding run --rm onboarding zotai s1 status

# Pipeline completo S1 (interactivo, prompts entre etapas).
./scripts/run-pipeline.sh
```

En el primer arranque `docker compose build` baja ~800 MB de imágenes
base + deps (tesseract incluido). Las corridas siguientes son mucho
más rápidas.

`run-pipeline.sh` pasa todos los args al CLI de Typer, así que:

```bash
./scripts/run-pipeline.sh --yes                       # sin prompts
./scripts/run-pipeline.sh --yes --tag-mode preview    # stop antes de Stage 06
./scripts/run-pipeline.sh --allow-template-taxonomy   # probar sin customizar taxonomía
```

---

## 5. Dashboard S2 (cuando esté implementado)

S2 todavía no está mergeado (issues #12–#15), pero cuando lo esté:

```bash
docker compose up dashboard   # localhost:8000
```

El servicio `dashboard` monta ChromaDB desde el host
(`${ZOTERO_MCP_CHROMA_HOST_PATH:-~/.config/zotero-mcp/chroma_db}`)
en modo `:rw` — es owner del índice bajo ADR 015, y `zotero-mcp serve`
en el host sólo lo lee.

---

## 6. Cron para S2 worker (opcional)

Bajo ADR 012 el worker de S2 es APScheduler in-process por default.
Si preferís no dejar el dashboard corriendo 24/7:

```bash
# /etc/crontab o crontab -e
0 */6 * * * cd /ruta/a/zotero-ai-toolkit && \
    docker compose --profile onboarding run --rm onboarding zotai s2 fetch-once
```

Y en `.env`:

```bash
S2_WORKER_DISABLED=true
```

---

## 7. Troubleshooting

- Permisos en `./workspace/`: el container corre como UID 1000 (el
  usuario `zotai` del Dockerfile). Si montás `./workspace/` desde un
  host con otro UID, cambiá la ownership:
  ```bash
  sudo chown -R 1000:1000 ./workspace
  ```
- Zotero no responde: verificar que esté abierto + Settings → Advanced
  → Allow other applications marcado.
- OCR muy lento: bajá `OCR_PARALLEL_PROCESSES` en `.env` si el host
  tiene poco RAM; cada worker de tesseract usa ~300 MB.

Ver `docs/troubleshooting.md` para más casos.

---

## 8. Referencias

- `docs/plan_00_overview.md` — arquitectura general.
- `docs/plan_01_subsystem1.md` — S1 pipeline detallado.
- `docs/economics.md` — costos esperados por etapa.
- `docs/decisions/001-use-docker.md` — por qué Docker.
- `docs/decisions/013-bridge-networking-host-docker-internal.md` — por
  qué `host.docker.internal` y no `network_mode: host`.
- `CLAUDE.md` — convenciones del repo.
