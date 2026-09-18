import React from "react";
import { createRoot } from "react-dom/client";
import App from "./App.jsx";
import { initToken } from "./lib/auth.js";

initToken(); // capture ?token= into sessionStorage before any API call
const root = createRoot(document.getElementById("root"));
root.render(<App />);
