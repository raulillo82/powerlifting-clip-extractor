# Instrucciones del proyecto

## CI/CD — Deploy automático a staging

Hay tres workflows en `.github/workflows/`:

| Workflow | Cuándo dispara | Qué hace |
|---|---|---|
| `tests.yml` | Cualquier push | Ejecuta los 183 tests |
| `deploy.yml` | Push a `main` → producción; push a rama con `STAGING_BRANCH` activa → staging; manual via `workflow_dispatch` | Build + restart del contenedor Podman |
| `staging.yml` | PR abierto/actualizado hacia `main` | Igual que deploy staging, pero actualiza también la variable `STAGING_BRANCH` automáticamente |

### Activar auto-deploy en una rama feature

```bash
gh variable set STAGING_BRANCH --body "feature/nombre-de-la-rama"
```

A partir de ese momento, **cada push a esa rama despliega automáticamente a staging** (self-hosted runner en RPi5). El contenedor tarda ~2-3 min en reconstruirse.

### Desactivar (cuando termines con la rama)

```bash
gh variable set STAGING_BRANCH --body "none"
```

### Verificar el valor actual

```bash
gh variable list
```

### Deploy manual puntual (sin cambiar STAGING_BRANCH)

```bash
gh workflow run deploy.yml -f target=staging
```

---

## Infraestructura

- **Producción**: RPi5, contenedor Podman `powerlifting`, servicio systemd `powerlifting`
- **Staging**: RPi5, contenedor Podman `powerlifting-staging`, servicio systemd `powerlifting-staging`
- **Directorio staging**: `~/powerlifting-clip-extractor-staging/`
- **Lifts (staging)**: `~/powerlifting-clip-extractor-staging/lifts/`
- **Entrar al contenedor**: `ssh raspberrypi5` → `podman exec -it powerlifting-staging bash`

## Ramas

- `main` → producción
- `experimental/ocr-auto-timestamps` → rama base para desarrollo OCR IPF
- PR de features → abrir contra `experimental/ocr-auto-timestamps` o `main` según corresponda
