import { copyFile, mkdir, readFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const assets = [
  {
    source: resolve(root, "assets/css/style.css"),
    output: resolve(root, "public/css/style.css"),
  },
  {
    source: resolve(root, "assets/js/contact-editor.js"),
    output: resolve(root, "public/js/contact-editor.js"),
  },
];
const checkOnly = process.argv.includes("--check");

if (checkOnly) {
  for (const asset of assets) {
    const [sourceText, outputText] = await Promise.all([
      readFile(asset.source, "utf8"),
      readFile(asset.output, "utf8"),
    ]);
    if (sourceText !== outputText) {
      process.stderr.write(`${asset.output.slice(root.length + 1)} is out of date; run npm run build.\n`);
      process.exitCode = 1;
    }
  }
} else {
  for (const asset of assets) {
    await mkdir(dirname(asset.output), { recursive: true });
    await copyFile(asset.source, asset.output);
  }
}
