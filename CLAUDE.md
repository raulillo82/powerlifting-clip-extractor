# Instrucciones del proyecto

## CI/CD — Despliegues

Los despliegues los hace el propio servidor (RPi5): el temporizador de usuario
`powerlifting-deploy.timer` comprueba cada minuto el repositorio público y
ejecuta `~/.local/bin/powerlifting-deploy.sh`. **No hay runner autoalojado y no
debe volver a haberlo**: el repositorio es público. Ningún workflow debe usar
`runs-on: self-hosted`.

| Entorno | Qué sigue | Cómo cambia |
|---|---|---|
| Producción | `origin/main` | Al fusionar en `main` |
| Staging | La rama escrita en el fichero `staging_branch` de la rama `deploy-state` | `staging.yml` o a mano (abajo) |

Workflows en `.github/workflows/`:

| Workflow | Cuándo dispara | Qué hace |
|---|---|---|
| `tests.yml` | Cualquier push o PR | Ejecuta `ruff check .` y los tests |
| `staging.yml` | PR hacia `main` abierta o actualizada, o manual | Escribe la rama en `deploy-state`. Al cerrar una PR deja `none` solo si staging estaba en esa rama |

Dependabot (`.github/dependabot.yml`) revisa cada mes `pip`, `docker` y `github-actions`.
Sus PR pasan los tests, no tocan staging y no se fusionan solas. Las alertas de
seguridad van aparte y abren PR en cuanto aparecen.

### Imagen base

El `FROM` va fijado con `tumbleweed:latest@sha256:…`, el digest del índice
multiarquitectura. No uses una etiqueta con fecha: cada una lleva solo algunas
arquitecturas y puede faltar arm64. `tests.yml` compila la imagen en arm64 y la arranca.

El digest no fija los paquetes: `zypper dup` los toma de los repositorios al compilar
y esa capa queda en caché hasta que cambie el digest o ese paso. Producción recompila
al fusionar, así que su imagen no es la misma que la de staging. Actualizar el digest
es lo que trae un `yt-dlp` nuevo: si YouTube empieza a dar 403, adelanta esa PR.

### Elegir la rama de staging

```bash
gh workflow run staging.yml --repo raulillo82/powerlifting-clip-extractor -f branch=<rama>
```

Hace falta para las ramas sin PR hacia `main`, como `feature/ipf-ocr-support`
(su PR apunta a otra rama y `staging.yml` no se lanza). Con `-f branch=none`,
staging deja de actualizarse y sigue con lo que tenía.

Cada push a la rama elegida se despliega solo en 1-4 minutos.

### Comprobar qué hay desplegado

```bash
gh api 'repos/raulillo82/powerlifting-clip-extractor/contents/staging_branch?ref=deploy-state' \
  -H 'Accept: application/vnd.github.raw'
ssh raspberrypi5 'git -C ~/powerlifting-clip-extractor-staging branch --show-current'
ssh raspberrypi5 'journalctl --user -u powerlifting-deploy -n 30 --no-pager'
ssh raspberrypi5 'git -C ~/powerlifting-clip-extractor log -1 --oneline'
```

Un commit que no despliega se reintenta tres veces (las compilaciones dependen de
los mirrors de openSUSE) y luego se queda parado hasta el siguiente commit. El
fallo solo aparece en ese journal: GitHub no lo muestra. Para reintentar:

```bash
ssh raspberrypi5 'rm ~/.local/state/powerlifting-deploy/production.failed'
```

(o `staging.failed`).

---

## Staging y PR abiertas

Abrir o actualizar una PR hacia `main` cambia la rama de staging y sustituye lo
que hubiera. Fusionar en `main` despliega producción.

**Al crear una PR nueva:** antes de abrirla, apunta la rama actual de staging con
los dos primeros comandos de arriba. Si no coinciden, o es `none`, mira
`git -C ~/powerlifting-clip-extractor-staging reflog` y avisa a Raúl. No reinicies
staging con un OCR en marcha.

**Después de fusionar:**

1. Espera a que termine el despliegue de producción (journal de arriba) y
   comprueba que los servicios siguen activos.
2. Trae `main` a todas las demás PR abiertas, con commits firmados. Si hay un
   conflicto, para y avisa.
3. Deja en staging la rama apuntada, con `gh workflow run staging.yml`. Cada push a
   una PR hacia `main` puede cambiar staging, así que hazlo al final.
4. Comprueba el resultado.

Cambiar `ansible/` **no** se aplica al fusionar: las unidades systemd, el script de
despliegue y nginx se instalan con Ansible o a mano. Si un cambio de imagen depende
de un cambio de unidad, decide antes el orden para no dejar producción caída.

---

## Infraestructura

- **Producción**: RPi5, contenedor Podman `powerlifting`, servicio systemd `powerlifting`
- **Staging**: RPi5, contenedor Podman `powerlifting-staging`, servicio systemd `powerlifting-staging`
- **Directorio staging**: `~/powerlifting-clip-extractor-staging/`
- **Lifts (staging)**: `~/powerlifting-clip-extractor-staging/lifts/`
- **Entrar al contenedor**: `ssh raspberrypi5` → `podman exec -it powerlifting-staging bash`
- **URL de staging**: `https://raulillo82.duckdns.org/powerlifting-staging/`
- **Registro en staging**: hay que entrar antes como admin. Sin sesión, `_staging_gate` bloquea `/static/`, el captcha no carga y sale "Verification failed". Es a propósito: no es un fallo de `altcha`.

## Ramas

- `main` → producción
- `deploy-state` → estado de staging; no la borres ni le abras PR
- `experimental/ocr-auto-timestamps` → rama base para desarrollo OCR IPF
- PR de features → abrir contra `experimental/ocr-auto-timestamps` o `main` según corresponda
