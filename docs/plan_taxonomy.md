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
- [ ] Guardar en `config/taxonomy.yaml`.
- [ ] Commit a repo antes de correr `zotai s1 tag`.
