# Bet365 Replicator

Panel web para replicar apuestas en múltiples cuentas de Bet365 usando la API de QRSolver.

## Archivos del proyecto

```
bet365-replicator/
├── app.py           ← Backend Python (el servidor)
├── index.html       ← Panel web (lo que ves en el móvil)
├── requirements.txt ← Librerías necesarias
├── Procfile         ← Configuración para Railway
└── README.md        ← Este archivo
```

## Cómo subir a Railway (gratis)

### Paso 1: Crear cuenta en GitHub
1. Ve a https://github.com
2. Crea una cuenta gratuita
3. Crea un repositorio nuevo llamado `bet365-replicator`

### Paso 2: Subir los archivos
1. En tu repositorio, haz clic en "Add file" → "Upload files"
2. Sube todos los archivos de esta carpeta (app.py, index.html, requirements.txt, Procfile)
3. Clic en "Commit changes"

### Paso 3: Desplegar en Railway
1. Ve a https://railway.app
2. Inicia sesión con tu cuenta de GitHub
3. Clic en "New Project" → "Deploy from GitHub repo"
4. Selecciona el repositorio `bet365-replicator`
5. Railway detectará automáticamente Python y lo desplegará
6. En 2-3 minutos tendrás una URL pública (ej: https://bet365-replicator.up.railway.app)

### Paso 4: Usar la app desde el móvil
1. Abre la URL de Railway en tu móvil
2. Ve a "Cuentas" → agrega tus 5 cuentas
3. Toca "Conectar todas"
4. Ve a "Apuesta", pega el link de Bet365 y toca el botón verde

## Funciones del panel

- ✅ Gestión de hasta N cuentas
- ✅ Login simultáneo en todas las cuentas
- ✅ Replicar apuesta con un solo clic
- ✅ Ver saldo de cada cuenta
- ✅ Keepalive automático cada 9 minutos
- ✅ Historial de apuestas
- ✅ Panel optimizado para móvil

## Notas importantes

- Cada cuenta necesita su propia API key de QRSolver
- El keepalive se envía automáticamente cada 9 minutos para mantener las sesiones activas
- Las apuestas se ejecutan en paralelo en todas las cuentas simultáneamente
