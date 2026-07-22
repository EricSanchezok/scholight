import { readFileSync, readdirSync } from "node:fs";
import { extname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const frontendRoot = resolve(fileURLToPath(new URL("..", import.meta.url)));
const sourceRoot = join(frontendRoot, "src");
const allowedColorSource = join(sourceRoot, "styles", "tokens.css");

function walk(directory) {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const path = join(directory, entry.name);
    return entry.isDirectory() ? walk(path) : [path];
  });
}

const sourceFiles = walk(sourceRoot);
const cssFiles = sourceFiles.filter((path) => extname(path) === ".css");
const uiFiles = sourceFiles.filter(
  (path) =>
    extname(path) === ".tsx" &&
    !path.endsWith(".test.tsx") &&
    !path.endsWith(join("app", "motion.tsx")),
);
const codeFiles = sourceFiles.filter(
  (path) => [".ts", ".tsx"].includes(extname(path)) && !path.includes(`${join("src", "i18n")}/`),
);
const findings = [];
const definitions = new Set();
const styleClasses = new Set();
const usages = [];

for (const path of cssFiles) {
  const content = readFileSync(path, "utf8");
  const displayPath = relative(frontendRoot, path);
  const lineCount = content.split("\n").length;
  if (lineCount > 900)
    findings.push(`${displayPath}:1 stylesheet exceeds 900 lines (${lineCount})`);
  for (const match of content.matchAll(/\.([A-Za-z_][A-Za-z0-9_-]*)/g)) {
    styleClasses.add(match[1]);
  }
  for (const match of content.matchAll(/--([a-z0-9-]+)\s*:/gi)) definitions.add(match[1]);
  for (const match of content.matchAll(/var\(--([a-z0-9-]+)/gi)) {
    usages.push({ token: match[1], path: displayPath, index: match.index ?? 0, content });
  }
  if (path !== allowedColorSource) {
    for (const match of content.matchAll(
      /#[0-9a-f]{3,8}\b|(?:rgb|hsl)a?\([^)]*\)|(?<=[:\s])(?:white|black)(?=[;\s])/gi,
    )) {
      findings.push(`${displayPath}:${lineOf(content, match.index)} raw color ${match[0]}`);
    }
  }
  for (const match of content.matchAll(/@keyframes\b|\banimation(?:-name)?\s*:/gi)) {
    findings.push(`${displayPath}:${lineOf(content, match.index)} handwritten animation`);
  }
}

for (const usage of usages) {
  if (!definitions.has(usage.token) && !usage.token.startsWith("radix-")) {
    findings.push(
      `${usage.path}:${lineOf(usage.content, usage.index)} undefined token --${usage.token}`,
    );
  }
}

for (const path of uiFiles) {
  const content = readFileSync(path, "utf8");
  const displayPath = relative(frontendRoot, path);
  const lineCount = content.split("\n").length;
  if (lineCount > 400) findings.push(`${displayPath}:1 UI module exceeds 400 lines (${lineCount})`);
  for (const match of content.matchAll(/["'`]\/(?!\/)/g)) {
    findings.push(`${displayPath}:${lineOf(content, match.index)} route outside app/routes.ts`);
  }
  for (const match of content.matchAll(/\b(?:duration|delay)\s*:\s*[0-9]/g)) {
    findings.push(
      `${displayPath}:${lineOf(content, match.index)} motion timing outside app/motion.tsx`,
    );
  }
  for (const match of content.matchAll(/\bstyles\.([A-Za-z_][A-Za-z0-9_]*)/g)) {
    if (!styleClasses.has(match[1])) {
      findings.push(`${displayPath}:${lineOf(content, match.index)} undefined style .${match[1]}`);
    }
  }
}

for (const path of codeFiles) {
  const content = readFileSync(path, "utf8");
  const displayPath = relative(frontendRoot, path);
  for (const match of content.matchAll(
    /\bIntl\.(?:DateTimeFormat|NumberFormat|RelativeTimeFormat)\b/g,
  )) {
    findings.push(`${displayPath}:${lineOf(content, match.index)} locale formatting outside i18n`);
  }
}

if (findings.length) {
  console.error("Frontend architecture checks failed:\n");
  findings.forEach((finding) => console.error(`- ${finding}`));
  process.exit(1);
}

console.log(
  `Frontend architecture checks passed (${cssFiles.length} stylesheets, ${definitions.size} tokens, ${uiFiles.length} UI modules).`,
);

function lineOf(content, index = 0) {
  return content.slice(0, index).split("\n").length;
}
