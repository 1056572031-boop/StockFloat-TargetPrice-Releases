import { execFileSync } from "node:child_process";
import { promises as fs } from "node:fs";
import { resolve, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = fileURLToPath(new URL(".", import.meta.url));
const output = resolve(root, "dist/pages");
const constants = await fs.readFile(resolve(root, "../../sf_constants.py"), "utf8");
const match = constants.match(/^APP_VERSION\s*=\s*'([0-9]+\.[0-9]+\.[0-9]+)'/m);
const version = process.env.SF_APP_VERSION || match?.[1];
if (!version || !/^\d+\.\d+\.\d+$/.test(version)) throw new Error("APP_VERSION is unavailable");

// The logo is exported from sf_icons.render_app_icon (the same function that draws
// icon.icns), so the site can never drift from the shipped application icon.
const python = process.env.SF_PYTHON || resolve(root, "../../.venv/bin/python");
const iconOutput = execFileSync(python, [resolve(root, "export-icon.py"), "128"], {
  encoding: "utf8", env: { ...process.env, QT_QPA_PLATFORM: "offscreen" },
});
const icons = JSON.parse(iconOutput.trim().split("\n").at(-1));
for (const mode of ["light", "dark"]) {
  if (typeof icons[mode] !== "string" || icons[mode].length < 512) throw new Error(`icon ${mode} is unavailable`);
}
const dataUri = mode => `data:image/png;base64,${icons[mode]}`;

const source = await fs.readFile(resolve(root, "src/index.mjs"), "utf8");
for (const placeholder of ['"__CURRENT_VERSION__"', '"__ICON_LIGHT__"', '"__ICON_DARK__"']) {
  if (!source.includes(placeholder)) throw new Error(`${placeholder} is missing from src/index.mjs`);
}
await fs.mkdir(output, { recursive: true });
const built = source
  .replaceAll('"__CURRENT_VERSION__"', JSON.stringify(version))
  .replaceAll('"__ICON_LIGHT__"', JSON.stringify(dataUri("light")))
  .replaceAll('"__ICON_DARK__"', JSON.stringify(dataUri("dark")));
await fs.writeFile(join(output, "_worker.js"), built);
await fs.writeFile(join(output, "_routes.json"), JSON.stringify({ version: 1, include: ["/*"], exclude: [] }) + "\n");
console.log(JSON.stringify({ pagesOutput: output, version, iconBytes: Buffer.byteLength(built) }));
