import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const staticDir = path.resolve(__dirname, "../../static");
const skip = new Set(["index.html", "404.html", "_not-found.html"]);

for (const entry of fs.readdirSync(staticDir)) {
  if (!entry.endsWith(".html") || skip.has(entry)) continue;

  const route = entry.slice(0, -5);
  const routeDir = path.join(staticDir, route);
  const indexPath = path.join(routeDir, "index.html");
  const htmlPath = path.join(staticDir, entry);

  if (!fs.existsSync(routeDir)) {
    fs.mkdirSync(routeDir, { recursive: true });
  }

  if (!fs.existsSync(indexPath)) {
    fs.copyFileSync(htmlPath, indexPath);
  }
}
