import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import Viewer from "./Viewer.jsx";
import "./app.css";

createRoot(document.getElementById("root")).render(
  <StrictMode><Viewer /></StrictMode>,
);
