# trading-copilot — frontend

React 18 + Vite + TypeScript + Tailwind. Consumes the Python engine over
`ws://127.0.0.1:8000/ws`.

## Arranque desde cero

```powershell
# desde C:\Users\Jordy\Desktop\AgenteSoporte
cd frontend

# solo la primera vez, si la carpeta no tuviera package.json:
# npm create vite@latest frontend -- --template react-ts

npm install
npm run dev
```

Abre `http://localhost:5173`.

## Backend (necesario para datos reales)

```powershell
cd ..\backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn main:app --reload
```

`GET http://127.0.0.1:8000/api/health` debe reportar `feed_connected: true`.
Si el frontend dice "WS desconectado", el backend no está corriendo o el
puerto 8000 está ocupado.

## Scripts

| comando             | qué hace                                  |
| ------------------- | ----------------------------------------- |
| `npm run dev`       | Vite dev server con HMR en :5173          |
| `npm run typecheck` | `tsc --noEmit`                            |
| `npm run build`     | typecheck + build de producción a `dist/` |
| `npm run preview`   | sirve `dist/` para probar el build        |

## Notas

- La URL del socket está en `src/hooks/useEngineSocket.ts` (`DEFAULT_URL`).
  Cámbiala ahí para apuntar a otro host; el hook la acepta como argumento.
- El proxy de `vite.config.ts` solo sirve para `/api`; el WebSocket va directo
  al puerto 8000 (el backend tiene CORS abierto en dev).
- El backend manda las señales con la más reciente al final; `App.tsx` invierte
  el array para pintar la más nueva primero.
