import { Navigate, Route, Routes } from "react-router-dom";

import { CatalogBuilderPage } from "./CatalogBuilderPage";

export function App() {
  return (
    <Routes>
      <Route path="/catalog-builder" element={<CatalogBuilderPage />} />
      <Route path="*" element={<Navigate to="/catalog-builder" replace />} />
    </Routes>
  );
}
