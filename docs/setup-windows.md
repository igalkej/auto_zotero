# Setup — Windows

Guía paso a paso para Windows 10/11 con WSL2 y Docker Desktop. Si algo
falla, mirá `docs/troubleshooting.md`.

---

## 1. Prerequisitos

1. **WSL2** habilitado:
   ```powershell
   wsl --install
   ```
   Reiniciar. Al reiniciar se instala Ubuntu por default.
2. **Docker Desktop for Windows** con integración WSL2 encendida
   (Settings → Resources → WSL Integration → tu distro).
3. **Zotero 7** desktop instalado, con la API local habilitada:
   Preferences → Advanced → "Allow other applications on this
   computer to communicate with Zotero". La API vive en
   `http://localhost:23119`.
4. **API key de Zotero** — https://www.zotero.org/settings/keys (read+write).
5. **API key de OpenAI** — https://platform.openai.com/api-keys.
6. **git** — `winget install Git.Git` o desde https://git-scm.com/download/win.

---

## 2. Clonar e instalar

PowerShell (o la shell de WSL — ambas funcionan igual; Docker Desktop
integra automáticamente):

```powershell
git clone https://github.com/igalkej/auto_zotero.git zotero-ai-toolkit
cd zotero-ai-toolkit
copy .env.example .env
```

Editá `.env` con tus credenciales. Los campos mínimos para S1:

```bash
ZOTERO_API_KEY=...
ZOTERO_LIBRARY_ID=...                   # userID de zotero.org/settings/keys
OPENAI_API_KEY=sk-...
PDF_SOURCE_FOLDERS=/data/folder1,/data/folder2
USER_EMAIL=tu@email.com                 # requerido por OpenAlex
```

Montá tus carpetas de PDFs en `.\data\` del repo. El
`docker-compose.yml` monta `./data` en `/data:ro` dentro del
container.

---

## 3. Networking — `host.docker.internal`

En Windows Docker Desktop, `host.docker.internal` resuelve automáticamente
al host. El proyecto inyecta `extra_hosts: "host.docker.internal:host-gateway"`
en `docker-compose.yml` (ver ADR 013), y `.env.example` define
`ZOTERO_LOCAL_API_HOST=http://host.docker.internal:23119` por
default dentro de los containers.

**Gotcha histórico**: versiones viejas de los docs sugerían
`network_mode: host`. En Windows (y macOS) eso se silenciaba y la
conexión a Zotero fallaba. Si editaste `docker-compose.yml` con un
`network_mode: host`, sacalo — el bridge + `host-gateway` es el path
correcto cross-platform.

---

## 4. Primera corrida

PowerShell:

```powershell
# Status — siempre seguro; no escribe nada.
docker compose --profile onboarding run --rm onboarding zotai s1 status

# Pipeline completo S1 (interactivo).
.\scripts\run-pipeline.ps1
```

En el primer arranque `docker compose build` baja ~800 MB. Corridas
siguientes son mucho más rápidas.

`run-pipeline.ps1` pasa todos los args al CLI, así que:

```powershell
.\scripts\run-pipeline.ps1 --yes
.\scripts\run-pipeline.ps1 --yes --tag-mode preview
.\scripts\run-pipeline.ps1 --allow-template-taxonomy
```

Si PowerShell se queja por la execution policy:

```powershell
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
```

---

## 5. Permisos de volúmenes

A diferencia de Linux, Docker Desktop for Windows hace la traducción
de permisos entre Windows y los UIDs del container automáticamente.
No deberías tener que hacer `chown` manual.

Si ves errores `Permission denied` sobre `/workspace/`, es casi
siempre Windows Defender / antivirus bloqueando el proyecto. Agregá
la carpeta del repo como excepción.

---

## 6. Task Scheduler para S2 worker (opcional)

Bajo ADR 012 el worker de S2 es APScheduler in-process por default.
Si preferís no dejar el dashboard corriendo 24/7:

1. `.env`: `S2_WORKER_DISABLED=true`.
2. Task Scheduler:
   - Create Task → Triggers → Daily, repetir cada 6 horas.
   - Action → Start a program:
     - Program: `powershell.exe`
     - Arguments:
       `-NoProfile -Command "cd C:\ruta\al\repo; docker compose --profile onboarding run --rm onboarding zotai s2 fetch-once"`

---

## 7. Troubleshooting

- **Docker Desktop no arranca**: verificar WSL2 activo
  (`wsl --status`). Si dice "WSL2 not installed", re-ejecutar
  `wsl --install`.
- **Zotero no responde desde el container**: verificar que Zotero
  Desktop esté abierto **en Windows** (no en WSL), y que Settings →
  Advanced → Allow other applications esté marcado.
- **Path mapping raro**: si tu repo está en WSL (`\\wsl$\Ubuntu\...`),
  Docker Desktop lee los volúmenes directamente. Si está en el
  filesystem Windows (`C:\Users\...`), Docker Desktop hace traducción
  transparente, pero el I/O es más lento. Preferí WSL.
- **OCR muy lento**: bajá `OCR_PARALLEL_PROCESSES` en `.env`.

Ver `docs/troubleshooting.md` para más casos.

---

## 8. Referencias

- `docs/plan_00_overview.md` — arquitectura.
- `docs/plan_01_subsystem1.md` — S1 pipeline.
- `docs/economics.md` — costos esperados.
- `docs/decisions/001-use-docker.md` — por qué Docker.
- `docs/decisions/013-bridge-networking-host-docker-internal.md` — por
  qué `host.docker.internal`.
- `CLAUDE.md` — convenciones del repo.
