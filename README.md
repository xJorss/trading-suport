# Trading Copilot

Copiloto de señales para Binance Futures. No ejecuta órdenes automáticamente (la ejecución es manual); su trabajo es escanear el mercado, aplicar dos estrategias con gestión de riesgo y avisarte cuando hay una oportunidad clara.

## Características
- **Modo Seguro (Safe):** Opera en 1h con filtro de tendencia EMA200 y cruces de EMA9/21. Stop Loss 3.5%, Take Profit 7%. Leverage 3x.
- **Modo Agresivo (Aggressive):** Opera en 5m con rupturas de Donchian, filtro EMA200 y ATR dinámico. Stop Loss (ATR*2), Take Profit (ATR*6). Leverage 3x.
- **Gestión de Riesgo:** Tamaño de posición calculado automáticamente según el capital virtual (Paper Trading).
- **Alertas por Telegram:** Envío de señales con entrada, SL, TP, leverage y tamaño sugerido.
- **Panel Web:** Gráficos de TradingView, precios en vivo, curva de capital y historial de operaciones.
- **Backtesting:** Script para probar las estrategias contra datos históricos reales de Binance, incluyendo comisiones.

## Stack Tecnológico
- **Backend:** Python 3.10+, FastAPI, WebSockets.
- **Frontend:** React 18, Vite, TypeScript, Tailwind CSS, Recharts.
- **Fuente de Datos:** Binance Futures (WebSocket/REST klines).
- **Despliegue:** Docker + Docker Compose (backend `python:3.11-slim`, frontend multi-stage con nginx).

## Cómo ejecutar el proyecto localmente

### Requisitos previos
- Python 3.10+
- Node.js 18+

### 1. Backend (Motor de señales)
```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

### 2. Frontend (Interfaz Web)
En otra terminal:
```powershell
cd frontend
npm install
npm run dev
```

### 3. Backtesting
Para probar las estrategias contra datos históricos (1000 velas por defecto):
```powershell
cd backend
.\.venv\Scripts\python.exe backtest.py --candles=1000
```

Opciones habituales:

| flag | por defecto | qué hace |
| --- | --- | --- |
| `--candles` | `1000` | velas cerradas a descargar por par y timeframe |
| `--pairs` | `BTCUSDT,ETHUSDT` | símbolos separados por coma |
| `--fee` | `0.0004` | comisión taker por lado (0.04%); `0` la desactiva |
| `--no-trail` | — | desactiva el break-even trailing |

El reporte separa PnL bruto, comisiones y PnL neto, y añade win rate, profit factor, drawdown máximo y desglose de salidas.

## Ejecución con Docker

La forma más rápida de levantar todo. Solo necesitas Docker con Compose v2.24 o superior (por el `env_file` opcional).

```powershell
docker compose up -d --build
```

| Servicio | URL |
| --- | --- |
| Panel web | http://localhost:5173 |
| API + Swagger | http://localhost:8000/docs |
| Health check | http://localhost:8000/api/health |

El frontend se sirve con nginx, que además enruta `/api` y `/ws` hacia `backend:8000`. Por eso el navegador solo habla con el puerto 5173: no hay CORS y no hay que configurar ninguna URL absoluta.

Comandos habituales:

```powershell
docker compose logs -f backend     # seguir el motor
docker compose restart backend     # aplicar cambios en pairs.json
docker compose down                # parar y eliminar contenedores
docker compose down -v             # parar y borrar también el estado
```

**Persistencia**

- El estado del paper trading (`engine_state.json`) vive en el volumen nombrado `trading-copilot-engine-state`, montado en `/app/state`. Sobrevive a reinicios y a `docker compose down`; solo se pierde con `down -v`.
- `backend/pairs.json` se monta en solo lectura, así que editar el mercado y reiniciar el servicio no requiere reconstruir la imagen.
- Las credenciales de Telegram se inyectan con `env_file` desde `backend/.env`. **No** se monta como volumen a propósito: si el archivo no existe, un bind mount crearía un directorio vacío en su lugar y rompería el arranque. Sin `.env`, el motor levanta con las alertas desactivadas.

**Variables de entorno**

| Variable | Servicio | Efecto |
| --- | --- | --- |
| `STATE_FILE_PATH` | backend | Ruta del snapshot. Ya viene fijada a `/app/state/engine_state.json`. |
| `TELEGRAM_BOT_TOKEN` | backend | Token del bot; vacío desactiva las alertas. |
| `TELEGRAM_CHAT_ID` | backend | Chat destino de las señales. |
| `VITE_API_BASE` | frontend (build) | Origen absoluto del backend. Sin definir, el panel usa rutas relativas. |

## Configuración de Pares

Los pares y timeframes se leen de `backend/pairs.json`:

```json
{"pairs": ["BTCUSDT", "ETHUSDT"], "timeframes": ["5m", "1h"]}
```

Reglas de lectura:

- Los símbolos se normalizan a mayúsculas y se deduplican en el orden declarado.
- Solo se aceptan los timeframes con estrategia asociada: `5m` (Aggressive) y `1h` (Safe). Cualquier otro se descarta con un aviso en el log.
- Máximo 20 pares; el resto se ignora.
- Si el archivo falta, está corrupto o queda vacío, el motor arranca con `BTCUSDT`/`ETHUSDT` en `5m`/`1h` y lo registra. Nunca se niega a arrancar por un error de configuración.
- El BOM de UTF-8 se tolera, así que puedes editar el archivo con Notepad sin romperlo.

Tras editarlo, reinicia el motor:

```powershell
# local
# Ctrl+C en uvicorn y volver a lanzarlo

# docker
docker compose restart backend
```

Para confirmar qué configuración está activa:

```powershell
curl http://localhost:8000/api/health
```

El campo `market` devuelve los pares y timeframes en uso, más `source` (`file` si se leyó el JSON, `defaults` si se usó el fallback). El mismo bloque viaja en cada frame del WebSocket como `state.market`.

## Alertas por Telegram (opcional)

Las alertas se activan solas si existen las credenciales; si faltan, el motor funciona igual y el panel no se ve afectado.

1. Crea un bot con **@BotFather** en Telegram (`/newbot`) y copia el token.
2. Abre el chat con tu bot y pulsa **Start**.
3. Pide tu id con `https://api.telegram.org/bot<TOKEN>/getUpdates` y busca `"chat":{"id":...}`.
4. Copia `backend/.env.example` a `backend/.env` y rellena:

```dotenv
TELEGRAM_BOT_TOKEN=123456789:AA...tu_token
TELEGRAM_CHAT_ID=123456789
```

5. Reinicia el backend. En el log debe aparecer `Telegram alerts enabled`.

El `.env` está en `.gitignore`: nunca subas el token al repositorio.

## Aviso
Este software es una herramienta de análisis y simulación. No es consejo financiero. El trading en futuros con apalancamiento implica un riesgo significativo de pérdida. Úsalo bajo tu propia responsabilidad.
