#!/usr/bin/env node
/** AstrBot serves committed assets. Keep editable sources; rebuild after edits. */
import { readFileSync, readdirSync, mkdirSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { build, transform } from "esbuild";
const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const dashboard = join(root, "pages", "dashboard"), output = join(dashboard, "assets");
const check = process.argv.includes("--check");
const read = name => readFileSync(join(dashboard, name), "utf8").replace(/\r\n/g, "\n");
const icons = JSON.parse(read("vendor/lucide-icons.json"));
function sources(dir = "") {
  return readdirSync(join(dashboard, dir), { withFileTypes: true }).flatMap(entry => {
    const path = join(dir, entry.name);
    return entry.isDirectory() ? ["assets", "vendor"].includes(entry.name) ? [] : sources(path) : /\.(html|js)$/.test(path) ? [path] : [];
  });
}
const used = [...new Set(sources().flatMap(path => [...read(path).matchAll(/data-lucide="([a-z0-9-]+)"/g)].map(match => match[1])))].sort();
for (const name of used) if (!icons[name]) throw new Error(`Missing local icon ${name}: add its Lucide icon data to vendor/lucide-icons.json`);
const subset = `/*! Lucide icon subset; ISC, see vendor/LUCIDE_LICENSE. */\nwindow.AnamIcons=${JSON.stringify(Object.fromEntries(used.map(name => [name, icons[name]])))};\n${read("vendor/lucide-runtime.js")}`;
const graphFiles = ["graph-shared.js", "graph-layout-core.js", "graph-renderer.js", "graph-interaction.js", "graph-2d.js", "graph-ui.js"];
const css = await transform(["styles.css", "art-direction.css", "workflows.css", "administration.css"].map(read).join("\n"), { loader: "css", minify: true });
const base = await transform(subset + "\n;\n" + read("i18n.js"), { loader: "js", target: "es2020", minify: true, legalComments: "inline" });
const graph = await transform(graphFiles.map(name => read(name) + "\n;").join("\n"), { loader: "js", target: "es2020", minify: true });
const app = await build({ absWorkingDir: dashboard, entryPoints: ["app.js"], bundle: true, minify: true, format: "iife", platform: "browser", target: "es2020", write: false, legalComments: "inline" });
const artifacts = new Map([["styles.min.css", css.code], ["base.min.js", base.code], ["graph.min.js", graph.code], ["app.min.js", app.outputFiles[0].text]]);
mkdirSync(output, { recursive: true });
let stale = false;
for (const [name, content] of artifacts) {
  const path = join(output, name);
  let current = ""; try { current = readFileSync(path, "utf8").replace(/\r\n/g, "\n"); } catch {}
  if (current === content) continue;
  stale = true;
  if (check) console.error(`stale dashboard bundle: assets/${name}`);
  else { writeFileSync(path, content, "utf8"); console.log(`wrote assets/${name} (${Buffer.byteLength(content)} bytes)`); }
}
if (check && stale) { console.error("Run npm run dashboard:build and commit the generated assets."); process.exitCode = 1; }
else if (check) console.log("dashboard bundles and icon subset are up to date");
