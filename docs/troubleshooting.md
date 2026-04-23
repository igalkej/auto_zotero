# Troubleshooting

Referencia de problemas comunes y cómo resolverlos. Agrupados por
categoría — si no encontrás tu caso, corré primero
`docker compose --profile onboarding run --rm onboarding zotai s1 status`
y pegá el output en el issue tracker.

---

## 1. Docker

### "Cannot connect to the Docker daemon"
Docker no está corriendo. En Windows/macOS abrí Docker Desktop; en
Linux: `sudo systemctl start docker` (o `sudo systemctl enable --now docker`
para arrancar al boot).

### `docker compose build` falla con un error de red
Retry simple con `--pull`:
```bash
docker compose --profile onboarding build --pull --no-cache
```
Si falla al descargar `ghcr.io/astral-sh/uv:0.4`, ver si tu proxy
corporativo bloquea `ghcr.io` o si el DNS corta. Podés forzar el
registry via `docker login ghcr.io` con un PAT vacío (ghcr expone
imágenes públicas, pero algunos proxies requieren auth previa).

### "Permission denied" al montar `./workspace/`
En Linux, el container corre como UID 1000 (`zotai` del Dockerfile).
Si el UID del host no coincide:
```bash
sudo chown -R 1000:1000 ./workspace ./data
```
En Windows con Docker Desktop, Windows Defender a veces bloquea las
escrituras — agregá la carpeta del repo como excepción.

### La imagen crece desmesuradamente
`tesseract-ocr-*` son ~300 MB por idioma. Si no necesitás español,
editá el Dockerfile y dejá sólo `tesseract-ocr-eng`. También corré
`docker image prune` regularmente.

---

## 2. Zotero connectivity

### "Cannot reach Zotero: local API requires Zotero Desktop to be open"
Checklist en orden:

1. Zotero Desktop está abierto (no sólo instalado).
2. Preferences → Advanced → "Allow other applications on this computer
   to communicate with Zotero" está marcado.
3. `ZOTERO_LOCAL_API=true` en `.env`.
4. Probá desde el host: `curl http://localhost:23119/api/` devuelve JSON.
5. Desde el container:
   ```bash
   docker compose --profile onboarding run --rm onboarding \
       curl -sv http://host.docker.internal:23119/api/
   ```
   Si falla acá, el problema es el bridge → host path. Ver ADR 013.

### Zotero corre en otra máquina
Editar `.env`:
```bash
ZOTERO_LOCAL_API_HOST=http://<IP-de-Zotero>:23119
```
Y verificar que el firewall permita el puerto 23119 entrante.

### Auth errors con la Web API (web fallback)
Checkear `ZOTERO_API_KEY` y `ZOTERO_LIBRARY_ID` en `.env`. La key tiene
que tener permisos read+write; la `library_id` es el número numérico
de https://www.zotero.org/settings/keys, no el username.

### Firewall bloquea `host.docker.internal:23119`
En Windows, Windows Defender Firewall a veces bloquea el tráfico
desde la red de Docker al host. Agregar una regla inbound que permita
el puerto 23119 desde `Docker Desktop Networks` (o simplemente desde
`172.0.0.0/12`).

---

## 3. OCR

### Stage 02 aborta con "disk space"
El chequeo pre-flight exige ≥ 2× el tamaño total del corpus libre en
el volumen de staging. Liberá espacio o montá `./workspace/` en un
disco más grande.

### OCR muy lento
Cada worker de tesseract usa ~300 MB de RAM. Si el host tiene poco
RAM, bajá `OCR_PARALLEL_PROCESSES` en `.env` — `2` suele ser el sweet
spot en laptops.

### "OCR produjo texto vacío"
Algún PDF escaneado tiene tan mala calidad que tesseract no recupera
texto. El item queda marcado `ocr_failed=True` y avanza igual;
Stage 04 intentará enriquecer por título. Si el título tampoco se
extrae bien, va a cuarentena (Stage 04e).

### `--force-ocr` en vez del default
Por default `skip_text=True` (no re-OCR si ya hay texto). Si sospechás
que el OCR previo fue malo:
```bash
docker compose --profile onboarding run --rm onboarding zotai s1 ocr --force-ocr
```

---

## 4. PDFs grandes (>20 MB)

### "attachment_simple failed: Request Entity Too Large"
Zotero API tiene un límite práctico cerca de 20 MB. El pipeline
fail-loud actual marca el item como `status=failed` y continúa. No
hay handling automático todavía —
[issue #39](https://github.com/igalkej/auto_zotero/issues/39) lo
trackea. Opciones manuales mientras tanto:

1. Importá el PDF a mano en Zotero Desktop (drag-and-drop). Stage 03
   lo detecta en la próxima corrida via DOI y hace dedup.
2. Comprimí el PDF (`ocrmypdf --optimize 3 --output-type pdf <in> <out>`)
   y re-colocalo en `PDF_SOURCE_FOLDERS`.

---

## 5. Budget exceeded mid-run

### Stage 01 o Stage 04 aborta con "Budget exceeded"
El cap de `.env` es duro — el pipeline prefiere abortar antes que
gastar demás. Subir el cap y re-correr; los items ya procesados se
saltean (Stage N es idempotente por `stage_completed`).

```bash
# Ejemplo: bump Stage 04 de 2.00 a 4.00
echo 'MAX_COST_USD_STAGE_04=4.00' >> .env
docker compose --profile onboarding run --rm onboarding zotai s1 enrich --substage all
```

Para corpus LATAM-heavy, plan_01 sugiere
`MAX_COST_USD_STAGE_04=4.00` como default (ver §"Aviso — corpus LATAM-heavy"
en plan_01 §3 Etapa 04).

### "run-all" se interrumpió al superar budget en Stage 04
El orchestrator captura `BudgetExceededError` y rutea los items
restantes a 04e (quarantine) en vez de volver a llamar al LLM. Para
rescatar items quarantinados después de subir el cap:

```bash
# 1. Levantar el cap.
# 2. Re-correr sólo stage 04:
docker compose --profile onboarding run --rm onboarding zotai s1 enrich --substage all
```
Los items que estaban en cuarentena con `last_error` apuntando al
budget vuelven a pasar por el cascade completo (`in_quarantine=False`
selector es estricto — si ya están en cuarentena, no re-procesás
automáticamente; sacalos de la colección Quarantine en Zotero
Desktop o reseteá `in_quarantine=False` en `state.db` primero).

---

## 6. Interrupted runs y resume

### Ctrl+C durante `run-all`
`run_all` captura `KeyboardInterrupt` entre stages y sale ordenado:
el estado commitado hasta ahí queda durable. Re-correr el mismo
comando salta los stages completos y arranca desde el próximo.

### El proceso fue matado sin chance de cleanup
Cada stage commitea por-item (no all-or-nothing), así que los
`state.db` rows ya insertados siguen ahí. Ejecutar `zotai s1 status`
muestra dónde quedó. Re-correr el stage específico (o `run-all`)
resume sin duplicar trabajo.

### "alembic_version" out-of-sync después de un crash
Raro, pero si `state.db` queda en un estado inconsistente:

```bash
# Backup primero.
cp workspace/state.db workspace/state.db.bak

# Ver revisión actual:
docker compose --profile onboarding run --rm onboarding \
    alembic -c /app/alembic.ini current

# Si necesitás bajar todo:
docker compose --profile onboarding run --rm onboarding \
    alembic -c /app/alembic.ini downgrade base

# Subir a head:
docker compose --profile onboarding run --rm onboarding \
    alembic -c /app/alembic.ini upgrade head
```

---

## 7. Log y reportes

Todos los stages escriben un CSV por corrida en `./workspace/reports/`:

- `inventory_report_<ts>.csv` — Stage 01, incluye clasificación.
- `excluded_report_<ts>.csv` — Stage 01, PDFs rechazados (no entran a state.db).
- `ocr_report_<ts>.csv` — Stage 02.
- `import_report_<ts>.csv` — Stage 03.
- `enrich_report_<ts>.csv` — Stage 04.
- `quarantine_report_<ts>.csv` — Stage 04e (sólo si hubo cuarentenas).
- `tag_report_<ts>.csv` — Stage 05.
- `s1_validation_<ts>.{html,csv}` — Stage 06.

Ante dudas, **siempre** empezá por:

```bash
docker compose --profile onboarding run --rm onboarding zotai s1 status
docker compose --profile onboarding run --rm onboarding zotai s1 validate --open-report
```

El status muestra en qué stage hay items atascados; el validation
report lista issues puntuales con links a Zotero.

---

## Referencias cruzadas

- ADR 013 — networking bridge + `host.docker.internal`.
- ADR 014 — dedup policy de Stage 03.
- ADR 015 — ownership ChromaDB (relevante para S2/S3).
- `docs/economics.md` — caps de budget explicados.
- `docs/setup-linux.md` / `docs/setup-windows.md` — instalación inicial.
