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
