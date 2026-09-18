# Frontend

React + TypeScript + Vite workspace for the local Catalog Builder.

```powershell
npm install
npm run dev
```

The Vite development server proxies `/api` reads to the FastAPI
server at `http://127.0.0.1:8000`. Open `/catalog-builder` in the frontend.

Optional: set `VITE_API_BASE_URL` when the API is hosted on another origin.
