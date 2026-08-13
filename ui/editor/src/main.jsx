import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import App from "./App.jsx";
import Viewer from "./Viewer.jsx";
import { viewerRequest } from "./catalog-graph.mjs";
import "./app.css";

// One bundle, two things it can be. The Runtime's own pages embed the viewer
// to draw a Workflow; opening /editor/ without the flag is the editor, as it
// was.
const { readOnly } = viewerRequest(globalThis.location?.search);

createRoot(document.getElementById("root")).render(
  <StrictMode>{readOnly ? <Viewer /> : <App />}</StrictMode>,
);
