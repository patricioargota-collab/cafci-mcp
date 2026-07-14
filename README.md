# cafci-mcp

Conector MCP de rendimientos de FCI (familias **Fima** de Galicia y **Pionero** de Macro), con serie diaria de VCP oficial CAFCI en SQLite sobre volumen persistente de Railway.

## Fuentes

- **Ingesta**: ArgentinaDatos (`api.argentinadatos.com/v1/finanzas/fci/{categoria}/{fecha}`) — republica los datos diarios de CAFCI en JSON, sin autenticación. Categorías: mercadoDinero, rentaFija, rentaVariable, rentaMixta, otros.
- **Contraste**: API oficial CAFCI (`api.cafci.org.ar/fondo/{id}/clase/{id}/rendimiento/{desde}/{hasta}`), por fondo/clase ID mapeado.

## Herramientas

| Tool | Función |
|---|---|
| `fci_actualizar` | Carga el último dato diario de las 5 categorías (solo familias configuradas). Idempotente. |
| `fci_backfill(desde, hasta, max_dias=10)` | Carga histórica por tramos, commit por día (a prueba de gateway timeout). |
| `fci_listar_fondos(familia?)` | Fondos indexados con cobertura de fechas. |
| `fci_rendimiento(fondo, desde?, hasta?, dias?)` | Rendimiento directo + TNA/TEA equivalentes sobre serie local. |
| `fci_comparar(dias=30, familia?, categoria?)` | Ranking de fondos por rendimiento en ventana. |
| `fci_serie(fondo, desde, hasta)` | Serie diaria VCP/patrimonio. |
| `fci_estado_indice` | Diagnóstico del índice local. |
| `fci_registrar_cafci_id(fondo, fondo_id, clase_id)` | Mapea fondo → IDs oficiales CAFCI. |
| `fci_cafci_rendimiento(fondo, desde, hasta)` | Rendimiento oficial en vivo desde api.cafci.org.ar. |

## Despliegue en Railway (patrón estándar)

1. Crear repo `cafci-mcp` en la org `patricioargota-collab` y subir estos archivos.
2. En Railway: **New Project → Deploy from GitHub repo** → seleccionar `cafci-mcp`.
3. **Variables**: agregar `PORT=8000` (explícita, como siempre).
4. **Volumen**: agregar volumen persistente montado en `/data`.
5. **Settings → Networking → Generate Domain** (puerto 8000).
6. Verificar: `https://<dominio>.up.railway.app/mcp` debe responder (405/406 a un GET simple es normal; el 421 "Invalid Host header" indicaría DNS rebinding activo — ya está deshabilitado en `main()`).
7. En Claude.ai → Configuración → Conectores → **Agregar conector personalizado** → URL `https://<dominio>.up.railway.app/mcp`.

## Primera carga

1. `fci_actualizar` → trae el último día.
2. `fci_backfill("2026-01-01", "2026-07-14")` → histórico por tramos de 10 días hábiles; relanzar con la fecha que indique la respuesta hasta completar.
3. `fci_estado_indice` → verificar cobertura.

## Configuración opcional

- `FCI_FAMILIAS` (env): familias a indexar, default `fima,pionero`.
- `DB_PATH` (env): ruta de la base, default `/data/fci.db`.
