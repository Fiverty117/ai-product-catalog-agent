import { Link, Navigate, Route, Routes } from "react-router-dom";

import { CatalogBuilderPage } from "./CatalogBuilderPage";
import { CatalogHistoryDetailPage, CatalogHistoryPage } from "./CatalogHistoryPage";
import { CategoryManagementPage } from "./CategoryManagementPage";
import { ProductIntakePage } from "./ProductIntakePage";

export function App() {
  return (
    <>
      <nav className="workspace-nav" aria-label="Workspaces"><Link to="/product-intake">Product Intake</Link><Link to="/catalog-builder">Catalog Builder</Link><Link to="/catalogs">Catalogs</Link><Link to="/categories">Categories</Link></nav>
      <Routes>
        <Route path="/product-intake" element={<ProductIntakePage />} />
        <Route path="/product-intake/:intakeId" element={<ProductIntakePage />} />
        <Route path="/catalog-builder" element={<CatalogBuilderPage />} />
        <Route path="/catalogs" element={<CatalogHistoryPage />} />
        <Route path="/catalogs/:buildId" element={<CatalogHistoryDetailPage />} />
        <Route path="/categories" element={<CategoryManagementPage />} />
        <Route path="*" element={<Navigate to="/catalog-builder" replace />} />
      </Routes>
    </>
  );
}
