// Fail if the static export contains inline scripts or styles, which the CSP forbids.
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";

function* htmlFiles(dir) {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) yield* htmlFiles(path);
    else if (entry.name.endsWith(".html")) yield path;
  }
}

const problems = [];
for (const file of htmlFiles("out")) {
  const html = readFileSync(file, "utf8");
  for (const [tag, attrs] of html.matchAll(/<script\b([^>]*)>/g)) {
    if (!/\bsrc=/.test(attrs) && !/type="application\/json"/.test(attrs)) problems.push(`${file}: ${tag}`);
  }
  if (/<style\b/.test(html)) problems.push(`${file}: <style>`);
  if (/\sstyle="/.test(html)) problems.push(`${file}: style attribute`);
  if (/\son[a-z]+="/.test(html)) problems.push(`${file}: inline event handler`);
}
if (problems.length) {
  console.error(`Inline script or style found; the CSP forbids it:\n${problems.join("\n")}`);
  process.exit(1);
}
console.log("Static export has no inline scripts or styles.");
