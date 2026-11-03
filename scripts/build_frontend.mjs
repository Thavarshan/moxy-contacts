import { copyFile, mkdir, readFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const source = resolve(root, "assets/css/style.css");
const output = resolve(root, "public/css/style.css");
const checkOnly = process.argv.includes("--check");

if (checkOnly) {
  const [sourceText, outputText] = await Promise.all([readFile(source, "utf8"), readFile(output, "utf8")]);
  if (sourceText !== outputText) {
    process.stderr.write("public/css/style.css is out of date; run npm run build.\n");
    process.exitCode = 1;
  }
} else {
  await mkdir(dirname(output), { recursive: true });
  await copyFile(source, output);
}
