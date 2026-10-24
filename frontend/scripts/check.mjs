// Проверка фронтенда без зависимостей: синтаксис каждого модуля (node --check) и то, что
// каждое импортируемое имя действительно экспортируется (опечатка иначе всплывёт только в браузере).
// Запуск: npm run check   (или node scripts/check.mjs)
import { execFileSync } from "node:child_process";
import { readdirSync, readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const jsDir = join(root, "js");
const files = readdirSync(jsDir).filter((f) => f.endsWith(".js"));
const errors = [];

const exportsOf = (src) => {
  const names = new Set();
  for (const m of src.matchAll(/export\s+(?:async\s+)?(?:function\*?|const|let|class)\s+([A-Za-z_$][\w$]*)/g)) names.add(m[1]);
  for (const m of src.matchAll(/export\s*\{([^}]*)\}/g)) {
    for (const part of m[1].split(",")) {
      const name = part.trim().split(/\s+as\s+/).pop();
      if (name) names.add(name);
    }
  }
  return names;
};

const sources = Object.fromEntries(files.map((f) => [f, readFileSync(join(jsDir, f), "utf8")]));
const exported = Object.fromEntries(files.map((f) => [f, exportsOf(sources[f])]));

for (const file of files) {
  try {
    execFileSync(process.execPath, ["--check", "--input-type=module"], { input: sources[file], stdio: ["pipe", "pipe", "pipe"] });
  } catch (err) {
    errors.push(`${file}: синтаксическая ошибка\n${err.stderr.toString().trim()}`);
  }
  for (const m of sources[file].matchAll(/import\s*\{([^}]*)\}\s*from\s*"\.\/([\w-]+\.js)"/g)) {
    const target = m[2];
    if (!exported[target]) {
      errors.push(`${file}: импорт из несуществующего модуля ./${target}`);
      continue;
    }
    for (const part of m[1].split(",")) {
      const name = part.trim().split(/\s+as\s+/)[0];
      if (name && !exported[target].has(name)) errors.push(`${file}: ./${target} не экспортирует «${name}»`);
    }
  }
}

const html = readFileSync(join(root, "index.html"), "utf8");
for (const m of html.matchAll(/src="js\/([\w-]+\.js)"/g)) {
  if (!files.includes(m[1])) errors.push(`index.html: нет файла js/${m[1]}`);
}

if (errors.length) {
  console.error(errors.join("\n"));
  console.error(`\nОшибок: ${errors.length}`);
  process.exit(1);
}
console.log(`OK: ${files.length} модулей, синтаксис и импорты в порядке`);
