# plan_taxonomy.md — Taxonomía de tags

**Propósito**: definir el vocabulario canónico de tags que aplica el Subsistema 1 durante el tagging automático, y que consume el Subsistema 2 como señal de matching.

**Estado**: **TEMPLATE — requiere completarse por el usuario antes de correr S1 Etapa 05**.

---

## 1. Principios de diseño de la taxonomía

- **Dos niveles únicamente**: TEMA (sustantivo del paper) y METODO (cómo aborda el problema). Geografía y tipo de documento van en campos nativos de Zotero (`Place`, `Item Type`), no en tags.
- **Tags estables**: entre 25 y 40 total. Menos = sub-clasificación; más = cada uno se usa pocas veces y pierde utilidad.
- **Ortografía consistente**: todo en kebab-case, minúsculas, sin tildes (por compatibilidad cross-platform).
- **Cobertura exhaustiva del corpus**: todo paper debería matchear al menos 1 TEMA y 1 METODO. Si hay áreas sin cobertura, agregar tag nuevo.
- **Mutuamente no exclusivos**: un paper puede tener múltiples tags de TEMA.

---

## 2. TEMA — Áreas temáticas

> **Usuario: reemplazar esta lista con tus áreas reales. La lista abajo es plantilla sugerida basada en economía/ciencias sociales con foco LATAM; adaptar al perfil real de tu biblioteca.**

```yaml
tema:
  # Macro
  - macro-crecimiento
  - macro-ciclos
  - macro-inflacion
  - macro-monetaria
  - macro-fiscal
  - macro-deuda

  # Meso / sectores
  - comercio-internacional
  - sistema-financiero
  - mercado-laboral
  - informalidad
  - desarrollo-productivo

  # Micro / hogares
  - pobreza
  - desigualdad
  - educacion
  - salud
  - genero

  # Instituciones y gobernanza
  - instituciones
  - corrupcion
  - capacidad-estatal

  # Ambiente
  - cambio-climatico
  - recursos-naturales

  # Metodológica-temáticos
  - demografia
  - migracion
  - tecnologia-digitalizacion
```

---

## 3. METODO — Aproximación metodológica

```yaml
metodo:
  # Empíricos
  - empirico-rct               # randomized controlled trial
  - empirico-quasi-exp         # diff-in-diff, RDD, IV, synthetic control
  - empirico-obs               # panel, series de tiempo, cross-section simple
  - empirico-estructural       # modelos estructurales estimados

  # Teóricos / computacionales
  - teorico-analitico          # modelos puramente formales
  - simulacion                 # DSGE, ABM, microsimulación

  # Síntesis
  - meta-analisis
  - revision-literatura

  # Cualitativos
  - narrativo-historico
  - caso-estudio

  # Policy
  - policy-analysis            # normativo, orientado a política
```

---

## 4. Reglas de aplicación (para el LLM tagger)

Estas reglas van al prompt de Etapa 05 del S1:

1. **Mínimo 1 de TEMA y 1 de METODO**. Si el paper no encaja claramente en ninguno de TEMA, aplicar tag `sin-clasificar-tema`.
2. **Máximo 4 de TEMA y 2 de METODO**. Más de esto es ruido.
3. **Si un tag no está en la lista, NO inventarlo**. Usar el más cercano o dejar sin ese nivel.
4. **Preferir específico sobre general**: si un paper es de política monetaria en específico, usar `macro-monetaria`, no `macro-fiscal` como aproximación.
5. **Si hay ambigüedad**, aplicar 2 tags en vez de adivinar uno.

---

## 5. Evolución de la taxonomía

- La primera corrida del S1 puede generar "huecos" (papers que no matchean bien ningún tag).
- El reporte de Etapa 06 (`validation`) reporta:
  - Tags con `count < 3` (infrequentes, candidatos a merge).
  - Tags con `count > 30%` del corpus (demasiado genéricos, candidatos a split).
  - Papers sin tags (revisar manualmente).
- Tras primera corrida, el usuario puede editar `config/taxonomy.yaml` y re-correr Etapa 05 con flag `--re-tag` (cost: ~$0.50).
- No hacer esto más de una vez al mes — tasa de cambio alta degrada la utilidad del tagging para S2.

---

## 6. Formato canónico

El sistema lee la taxonomía desde `config/taxonomy.yaml`. El formato es:

```yaml
# config/taxonomy.yaml

version: 1

tema:
  - id: macro-crecimiento
    description: "Determinantes del crecimiento económico de largo plazo"
    synonyms: ["growth", "crecimiento", "desarrollo económico"]

  - id: macro-ciclos
    description: "Fluctuaciones de corto plazo, recesiones, expansiones"
    synonyms: ["business cycles", "ciclos económicos"]

  # ...

metodo:
  - id: empirico-rct
    description: "Randomized controlled trials / experimentos aleatorizados"
    synonyms: ["RCT", "experimento"]

  # ...
```

**Los `synonyms` son hints al LLM** para reconocer cuándo aplicar el tag aunque el paper use terminología distinta. No se aplican como tags literalmente.

---

## 7. TODO para el usuario antes de correr S1 Etapa 05

- [ ] Revisar esta lista sugerida y ajustarla al perfil real del corpus.
- [ ] Completar descripciones y synonyms de cada tag.
- [ ] Guardar en `config/taxonomy.yaml` y cambiar `status: template` por `status: customized`.
- [ ] Commit a repo antes de correr `zotai s1 tag`.

---

## 8. Adaptar la taxonomía a tu dominio

La lista de `config/taxonomy.yaml` viene sesgada a economía / ciencias sociales con foco LATAM. Si tu corpus es de otro dominio (biomedicina, física de altas energías, derecho, humanidades digitales, etc.), tenés que reemplazarla — no sumarle. El objetivo es que las etiquetas reflejen *tu* campo, no una sobre-cobertura de múltiples campos.

### 8.1 Qué hace una buena tag

Una tag útil cumple cuatro condiciones:

1. **Granularidad adecuada**: ni tan amplia que taggée al 60% del corpus (`economia`, `biologia` — inútil para búsqueda), ni tan angosta que solo matchée 1-2 papers (`efectos-del-covid-sobre-la-tasa-de-actividad-femenina-en-CABA`). Apuntar a 3-10% del corpus por tag como zona saludable.
2. **Ortogonalidad**: dos tags del mismo nivel no deberían ser casi-sinónimos. Si existieran `macro-monetaria` y `banca-central`, habría que elegir uno y mandar el otro a `synonyms`. En caso de duda, aplicar el **test del LLM**: ¿un tagger razonable podría dudar entre estas dos tags para el mismo paper? Si sí, son redundantes.
3. **Estabilidad semántica**: la tag significa hoy lo mismo que va a significar dentro de 2 años. Evitar tags acopladas a eventos o modas (`post-pandemia`, `era-IA`) — esas son mejor en el `Date` nativo de Zotero.
4. **Referible en una frase**: si no podés escribir una `description` de 1 línea para la tag, la tag no está clara todavía. Los `synonyms` son para variaciones de vocabulario (inglés/castellano, acrónimos), no para pegar varios conceptos.

### 8.2 Cómo pensar TEMA vs METODO

**TEMA responde "¿de qué es?"**. **METODO responde "¿cómo lo aborda?"**.

Si te encontrás con una tag candidata en la que no está claro cuál de los dos es, probablemente estás mezclando: `tema-teorico` es malo (teórico es método), `empirico-macro` es malo (macro es tema). Las dos dimensiones se componen: un paper puede ser `macro-fiscal` × `empirico-quasi-exp`.

Si tu dominio no tiene una división natural tema/método (p.ej. muchas humanidades), podés dejar METODO con 2-3 tags muy amplios (`narrativo-historico`, `caso-estudio`, `revision-literatura`) y poner el grueso del poder discriminativo en TEMA.

### 8.3 Walkthrough: adaptar a biomedicina

Para orientar, un ejemplo mínimo de cómo se vería la plantilla aplicada a un corpus de biomedicina traslacional:

```yaml
tema:
  - id: oncologia-solida
    description: "Tumores sólidos: mama, pulmón, colorrectal, próstata, etc."
    synonyms: ["solid tumors", "breast cancer", "lung cancer"]
  - id: hematologia
    description: "Leucemias, linfomas, mieloma"
    synonyms: ["leukemia", "lymphoma"]
  - id: inmunoterapia
    description: "Checkpoint inhibitors, CAR-T, vacunas terapéuticas"
    synonyms: ["immunotherapy", "CAR-T", "checkpoint inhibitors"]
  - id: farmacogenomica
    description: "Variación genética y respuesta a drogas"
    synonyms: ["pharmacogenomics"]
  # ... (continuar 20-30 más)

metodo:
  - id: ensayo-clinico-fase-iii
    description: "Ensayos clínicos de fase III, randomizados"
    synonyms: ["phase III", "randomized clinical trial"]
  - id: ensayo-clinico-temprano
    description: "Fase I / II, pilotos, seguridad"
    synonyms: ["phase I", "phase II"]
  - id: estudio-preclinico
    description: "Modelos animales, in vitro"
    synonyms: ["preclinical", "in vivo", "in vitro"]
  - id: observacional-cohorte
    description: "Estudios de cohorte, case-control"
    synonyms: ["cohort study", "case-control"]
  # ... (continuar hasta ~10-12)
```

Este ejemplo ilustra el patrón: **TEMA se ancla a subespecialidades del dominio; METODO a niveles de evidencia / tipos de estudio del dominio**. La división es paralela a la de economía (TEMA = áreas, METODO = estrategia empírica), solo que el contenido cambia.

### 8.4 Smoke-test antes de correr `zotai s1 tag`

Antes del primer `--apply`, usar `--preview` sobre una muestra del corpus (20-30 items variados) y revisar el CSV resultante:

- [ ] **Cobertura**: al menos 1 TEMA y 1 METODO por item.
- [ ] **Reparto**: ningún tag aparece en >80% de los items (si pasa: tag demasiado amplio, split).
- [ ] **Reparto**: ningún tag aparece en 0% (si pasa: tag nunca matchea, considerar borrar o ajustar `description`/`synonyms`).
- [ ] **Consistencia manual**: elegir 5 items al azar y chequear que los tags que el LLM aplicó son los que vos hubieras elegido. Si hay desacuerdo sistemático en un tag, revisar su `description`.
- [ ] **Tags inventados**: verificar que el CSV no tiene valores fuera de la taxonomía (el stage valida esto, pero conviene chequear a ojo).

Si el smoke-test es OK, correr `--apply`. Si no, editar `config/taxonomy.yaml` y repetir el preview — la iteración es barata ($0.001 por 25 items).
