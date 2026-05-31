# Catálogo de formatos de retransmisión — powerlifting

Generado con `analyze_format.py` + conocimiento previo de los jobs OCR.

**Leyenda columnas:**
- Timer región: posición normalizada en el frame donde aparece el cronómetro de pausa
- Timer fondo: color de fondo del cronómetro (según detección de píxeles por color)
- Banner región: posición donde aparece el overlay con el nombre del atleta
- Banner fondo: color predominante del overlay
- OCR actual: ✅ detecta correctamente / ⚠️ parcial / ❌ no detecta

---

| Fed | Competición | Vídeo | Timer región | Timer fondo | Banner región | Banner fondo | OCR actual | Notas |
|-----|-------------|-------|-------------|-------------|--------------|-------------|-----------|-------|
| AEP | Masters 2026 S3 | [I3LHqLA8Xao](https://youtube.com/watch?v=I3LHqLA8Xao) | `top_right` / `bottom_right` | rojo | `bottom_left` | amarillo | ✅ | Timer en sup-der confirmado; banner izq inf con fondo amarillo HSV H=10-33 |
| AEP | Junior 2026 S1 | [Q4v5kb_32rU](https://youtube.com/watch?v=Q4v5kb_32rU) | `top_right` | rojo | `bottom_left`? | amarillo | ✅ | Misma familia que Masters. Script detecta texto en `bottom_right` (62%) — posiblemente mismo banner, región ligeramente desplazada |
| AEP | Young Ambition Cup II | [ifNGcztSAwg](https://youtube.com/watch?v=ifNGcztSAwg) | desconocida | desconocido | `full_top` | **blanco** | ❌ | Banner en la franja **superior** a todo lo ancho, fondo blanco. Script: text=100% en `banner_full_top`. Timer nunca detectado en primera muestra — posiblemente ausente o en posición no probada |
| AEP | Junior 2025 Día 2 | [kI4z0Rpj2YQ](https://youtube.com/watch?v=kI4z0Rpj2YQ) | desconocida | desconocido | `bottom_left` + `full_top` | blanco + dark | ❌ | Vídeo de 11h con múltiples sesiones del día. Script detecta texto al 100% tanto en `bottom_left` (var=28, dark) como en `full_top` (var=53, white) — sugiere que diferentes sesiones tienen formatos distintos dentro del mismo vídeo |
| IPF | World Classic Open | [8CV8Kl7EHNQ](https://youtube.com/watch?v=8CV8Kl7EHNQ) | desconocida | desconocido | `top_right` / `timer_top_right` | **blanco** | ❓ | Text=62% en `timer_top_right` y `banner_top_right`. Banner parece estar en la **esquina superior derecha** — misma zona que el timer AEP. Sin timer detectado |
| EPF | European Equipped 2026 | [lm7Oz3BLRKY](https://youtube.com/watch?v=lm7Oz3BLRKY) | desconocida | desconocido | `banner_center_bot` / `full_bottom` | dark | ❓ | Texto al 100% en `banner_center_bot` y 88% en `banner_bottom_right` y `full_bottom`. Banner parece estar en la **franja inferior central o derecha**. Muchas regiones con texto → overlay ancho |
| USAPL | Open Nationals 2026 | [GCT-83GaZfs](https://youtube.com/watch?v=GCT-83GaZfs) | desconocida | desconocido | `full_top` / `top_right` | dark | ❓ | Text=57% en `banner_full_top`, 43% en `banner_top_right`. Banner en la **franja superior**, similar a Young Ambition Cup |

---

## Patrones identificados

| Patrón | Federaciones / competiciones |
|--------|------------------------------|
| **Banner inferior izquierdo, fondo amarillo** (AEP estándar) | AEP Masters 2026, AEP Junior 2026 |
| **Banner superior completo, fondo blanco/claro** | AEP Young Ambition Cup II, posiblemente sesiones de AEP Junior 2025 |
| **Banner superior derecha** | IPF World Classic |
| **Banner inferior central/ancho** | EPF European Equipped |
| **Banner superior (franja)** | USAPL |
| **Timer esquina superior derecha, fondo rojo** | AEP Junior 2026, AEP Masters 2026 (posiblemente ambas ramas del mismo sistema de producción) |

---

## Limitaciones del análisis actual

1. **Timer no detectado**: la mayoría de frames muestreados son de competición activa, no de pausas entre movimientos. El timer solo aparece durante los ~10 min de pausa. Re-muestrear con timestamps de pausas confirmaría la posición del timer para cada federación.

2. **Color de fondo incorrecto**: el `dominant_bg` se calcula sobre toda la región, que incluye mucho contenido de vídeo (color oscuro). El color real del overlay (amarillo AEP, blanco IPF/USAPL) queda diluido. Necesita bounding-box del overlay, no región completa.

3. **AEP Junior 2025 Día 2 es un vídeo de 11h** con múltiples sesiones, por lo que puede tener formatos mezclados. Para este vídeo conviene analizar subsecciones específicas.

4. **Número de frames bajo** (7-8 por competición): con más frames o mejores timestamps el porcentaje de acierto sería más fiable.

---

*Última actualización: 2026-05-31 — 7 competiciones analizadas*
