#!/usr/bin/env node
// team — CLI determinista del equipo de skills (sin dependencias).
// Uso: node .claude/team.mjs <comando> [args]
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { execSync, spawnSync } from 'node:child_process';
import crypto from 'node:crypto';
import { fileURLToPath, pathToFileURL } from 'node:url';

const VERSION = '0.10.0';
const FOLDERS = ['to-do', 'in-progress', 'done', 'archived'];
const STATES = {
  'Borrador': 'to-do', 'Aprobado': 'to-do',
  'En desarrollo': 'in-progress', 'En verificación': 'in-progress',
  'Cerrado': 'done', 'Descartado': 'archived', 'Archivado': 'archived',
};
const LEVELS = ['XS', 'S', 'M', 'L'];
const REQUIRED = { XS: [0, 7, 8, 9], S: [0, 1, 7, 8, 9], M: [0, 1, 2, 3, 4, 5, 6, 7, 8, 9], L: [0, 1, 2, 3, 4, 5, 6, 7, 8, 9] };
const LABELS = ['En palabras simples', 'Resumen', 'Producto', 'UX/UI', 'Diseño técnico', 'Pruebas', 'Documentación', 'Tareas', 'Bitácora', 'Historial'];

const ROOT = findRoot(process.cwd());
const DOCS = path.join(ROOT, 'docs');
const BK = path.join(DOCS, 'backlog');
const SELF = fileURLToPath(import.meta.url);

function findRoot(start) {
  let c = start;
  for (;;) {
    if (fs.existsSync(path.join(c, '.git')) || fs.existsSync(path.join(c, 'docs', 'backlog'))) return c;
    const p = path.dirname(c);
    if (p === c) return start;
    c = p;
  }
}
const pad = n => String(n).padStart(2, '0');
function today() { const d = new Date(); return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`; }
process.stdout.on('error', e => { if (e.code === 'EPIPE') process.exit(0); });
const out = s => process.stdout.write(s + '\n');
function fail(msg) { process.stderr.write('✖ ' + msg + '\n'); process.exit(1); }
const rel = p => path.relative(ROOT, p).split(path.sep).join('/');

function slugify(s) {
  return s.normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase()
    .replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 40) || 'cambio';
}

// ---------- lectura de cambios ----------
function listChanges() {
  const res = [];
  for (const folder of FOLDERS) {
    const dir = path.join(BK, folder);
    if (!fs.existsSync(dir)) continue;
    for (const name of fs.readdirSync(dir)) {
      const m = /^CHG-(\d+)/.exec(name);
      const file = path.join(dir, name, 'CHANGE.md');
      if (!m || !fs.existsSync(file)) continue;
      const text = fs.readFileSync(file, 'utf8');
      res.push({ num: +m[1], id: `CHG-${m[1]}`, name, folder, dir: path.join(dir, name), file, text, ...parse(text) });
    }
  }
  return res.sort((a, b) => a.num - b.num);
}
function parse(text) {
  const title = (/^# CHG-\d+\s*·\s*(.+)$/m.exec(text) || [])[1]?.trim() || '';
  const state = (/^\*\*Estado:\*\*\s*(.+?)\s*$/m.exec(text) || [])[1] || '';
  const level = (/^\*\*Nivel:\*\*\s*(XS|S|M|L)\b/m.exec(text) || [])[1] || 'M';
  const model = (/^\*\*Modelo sugerido:\*\*\s*(.+?)\s*$/m.exec(text) || [])[1] || '';
  const tasks = [...text.replace(/<!--[\s\S]*?-->/g, '').matchAll(/^- \[( |x|X)\] (.*)$/gm)];
  const done = tasks.filter(t => t[1] !== ' ').length;
  return { title, state, level, model, tasks, done, total: tasks.length };
}
function find(idArg) {
  if (!idArg) fail('Falta el ID del cambio (ej. CHG-003 o 3).');
  const n = +String(idArg).replace(/^CHG-?/i, '');
  if (!Number.isFinite(n)) fail(`ID inválido: ${idArg}`);
  const c = listChanges().find(x => x.num === n);
  if (!c) fail(`No existe CHG-${String(n).padStart(3, '0')} en docs/backlog/.`);
  return c;
}

// ---------- escritura ----------
function setState(text, state) { return text.replace(/^\*\*Estado:\*\*.*$/m, `**Estado:** ${state}`); }
function setDate(text, label, value) {
  return text.replace(new RegExp(`(\\*\\*${label}:\\*\\*)\\s*[^·\\n]*`), `$1 ${value} `).replace(/ +\n/g, '\n');
}
function addHistory(text, msg) {
  return text.replace(/\s*$/, '') + `\n- ${today()} — ${msg}\n`;
}
function isGit() {
  try { execSync('git rev-parse --is-inside-work-tree', { cwd: ROOT, stdio: 'pipe' }); return true; } catch { return false; }
}
function moveTo(c, folder) {
  if (c.folder === folder) return c.dir;
  const destDir = path.join(BK, folder);
  fs.mkdirSync(destDir, { recursive: true });
  const dest = path.join(destDir, c.name);
  if (fs.existsSync(dest)) fail(`Ya existe ${rel(dest)}.`);
  let moved = false;
  if (isGit()) {
    try { execSync(`git mv "${c.dir}" "${dest}"`, { cwd: ROOT, stdio: 'pipe' }); moved = true; } catch { /* no versionado aún */ }
  }
  if (!moved) fs.renameSync(c.dir, dest);
  return dest;
}
function save(c, text, folder) {
  const dir = folder ? moveTo(c, folder) : c.dir;
  fs.writeFileSync(path.join(dir, 'CHANGE.md'), text);
  syncPlan();
  return dir;
}

// ---------- validación ----------
function sectionBody(text, n) {
  const m = new RegExp(`^## ${n}\\.`, 'm').exec(text);
  if (!m) return null;
  const rest = text.slice(text.indexOf('\n', m.index) + 1);
  const nx = /^## \d+\./m.exec(rest);
  return nx ? rest.slice(0, nx.index) : rest;
}
function hasContent(body) {
  return body.split('\n').some(l => {
    const s = l.trim().replace(/^[-*]\s*/, '');
    if (!s || s.startsWith('<!--') || s.startsWith('#') || /^\|?[\s|:-]+\|?$/.test(s)) return false;
    if (/no aplica/i.test(s)) return true;
    if (s.startsWith('|')) return !/^\|\s*Caso\s*\|/.test(s);
    if (/:\s*$/.test(s) || /:\*\*\s*$/.test(s)) return false;
    return true;
  });
}
// Una parte de §3 "no aplica" solo si lo dice su primera línea con contenido.
function saysNotApplicable(body) {
  const first = body.replace(/<!--[\s\S]*?-->/g, '').split(/\r?\n/).map(l => l.trim().replace(/^[-*]\s*/, '')).find(l => l && !l.startsWith('#'));
  return !!first && /^no aplica/i.test(first);
}
// Boceto: un bloque de código con contenido que no sea Mermaid.
function hasSketch(body) {
  return [...body.matchAll(/^\s*```([^\n]*)\n([\s\S]*?)^\s*```/gm)].some(m => !/mermaid/i.test(m[1]) && m[2].trim());
}
// Divide §3 en Experiencia y Visual. Sin subsecciones (CHG antiguo) todo cuenta como Experiencia.
function designParts(c) {
  const body = sectionBody(c.text, 3);
  if (body === null) return null;
  const clean = body.replace(/<!--[\s\S]*?-->/g, '');
  const ui = /^### 3\.2\b/m.exec(clean);
  if (!ui) return { ux: clean, ui: null };
  return { ux: clean.slice(0, ui.index), ui: clean.slice(ui.index) };
}
function validate(c, { forApproval = false } = {}) {
  const errors = [], warnings = [];
  const strict = forApproval ? errors : warnings;
  const req = REQUIRED[c.level];
  req.forEach(n => { if (sectionBody(c.text, n) === null) errors.push(`Falta la sección "## ${n}. ${LABELS[n]}" (nivel ${c.level}).`); });
  if (!STATES[c.state]) errors.push(`Estado desconocido: "${c.state}".`);
  else if (STATES[c.state] !== c.folder) errors.push(`Estado "${c.state}" no corresponde a la carpeta ${c.folder}/ (debería estar en ${STATES[c.state]}/).`);
  if (!c.title || c.title.includes('<')) errors.push('Falta el título del cambio.');
  for (const n of req.filter(n => n <= 6)) {
    const b = sectionBody(c.text, n);
    if (b !== null && !hasContent(b)) strict.push(`§${n} ${LABELS[n]} está vacía (llénala o escribe "No aplica — motivo").`);
  }
  // El boceto solo se exige al aprobar: los CHG ya aprobados no se revalidan con esta regla.
  const s3 = sectionBody(c.text, 3);
  if (forApproval && s3 !== null && c.level !== 'XS' && hasContent(s3.replace(/<!--[\s\S]*?-->/g, ''))) {
    const parts = designParts(c);
    const applies = [parts.ux, parts.ui].filter(p => p !== null && !saysNotApplicable(p));
    if (applies.length && !hasSketch(s3)) errors.push('§3 describe UI pero no tiene boceto ASCII en un bloque de código (si no hay pantallas, escribe "No aplica — motivo").');
  }
  if (c.total === 0) strict.push('§7 no tiene tareas ("- [ ] T1 · ...").');
  c.tasks.forEach(t => {
    const line = t[2];
    const id = (/^(T\d+)/.exec(line) || [])[1] || line.slice(0, 20);
    if (!/Hecho cuando:/i.test(line)) strict.push(`Tarea ${id} sin "Hecho cuando:".`);
    if (!/Resp:/i.test(line)) strict.push(`Tarea ${id} sin "Resp:".`);
  });
  if (c.level === 'L' && !c.text.includes('Dividido en:')) warnings.push('Nivel L: considera dividirlo en varios CHG de nivel M (anota "Dividido en: CHG-…").');
  const open = (sectionBody(c.text, 9) || '').split('\n').filter(l => /\?\s*(→\s*)?$/.test(l.trim()));
  if (open.length) strict.push(`§9 tiene ${open.length} pregunta(s) sin decisión.`);
  return { errors, warnings };
}
function report(c, r) {
  if (!r.errors.length && !r.warnings.length) { out(`✔ ${c.id} válido.`); return; }
  r.errors.forEach(e => out(`✖ ${e}`));
  r.warnings.forEach(w => out(`⚠ ${w}`));
}

// ---------- TEAM_PLAN ----------
function syncPlan() {
  const file = path.join(DOCS, 'TEAM_PLAN.md');
  const rows = listChanges().filter(c => c.folder !== 'archived')
    .map(c => `| ${c.id} | ${c.level} | ${c.title} | ${c.folder} | ${c.state} | ${c.done}/${c.total} |`);
  const table = ['<!-- team:table:start -->', '| CHG | Nivel | Título | Carpeta | Estado | Tareas |', '|---|---|---|---|---|---|', ...rows, '<!-- team:table:end -->'].join('\n');
  let text = fs.existsSync(file) ? fs.readFileSync(file, 'utf8') : planTemplate();
  text = /<!-- team:table:start -->[\s\S]*<!-- team:table:end -->/.test(text)
    ? text.replace(/<!-- team:table:start -->[\s\S]*<!-- team:table:end -->/, table)
    : text.replace(/\s*$/, '\n\n' + table + '\n');
  text = text.replace(/^_Última actualización:.*_$/m, `_Última actualización: ${today()}_`);
  fs.mkdirSync(DOCS, { recursive: true });
  fs.writeFileSync(file, text);
}
function planTemplate() {
  return `# Plan del equipo\n_Última actualización: ${today()}_\n\n> Tabla generada por \`node .claude/team.mjs\`. No la edites a mano.\n\n<!-- team:table:start -->\n<!-- team:table:end -->\n\n## Prioridades y dependencias\n\n## Decisiones y lecciones\n`;
}

function header(id, title, level, state, model) {
  return `# ${id} · ${title}
**Estado:** ${state}
**Nivel:** ${level}
**Modelo sugerido:** ${model}
<!-- Estados y nivel se cambian SOLO con node .claude/team.mjs. Modelo: un perfil de docs/TEAM_MODELS.md -->
**Creado:** ${today()} · **Aprobado:** ${state === 'Borrador' ? '—' : today()} · **Cerrado:** —
**Relacionado:** —

## 0. En palabras simples
<!-- Para el usuario: sin jerga. Si no se entiende en 30 segundos, reescríbelo. -->
- Qué vas a ver distinto:
- Cómo comprobarlo tú:
`;
}
const S_SUMMARY = `
## 1. Resumen
<!-- Qué y por qué, 2-3 líneas. Agrega solo la sección del rol afectado (## 3. UX/UI, ## 4. Diseño técnico o ## 5. Pruebas) si hace falta. Si hay UI, §3 lleva un boceto ASCII de la zona que cambia. -->
`;
const FULL_ROLES = `
## 1. Resumen
<!-- Qué se cambia y por qué (3-5 líneas). Entendimiento aprobado por el usuario. -->

## 2. Producto (product-manager)
- Problema / usuario:
- Incluye:
- No incluye:
- Criterios de aceptación (Dado/Cuando/Entonces):

## 3. UX/UI
<!-- Sin pantallas: escribe "No aplica — motivo" en cada parte. Con UI, approve exige un boceto ASCII en un bloque de código. Si hay flujo, agrega un diagrama en un bloque mermaid. -->
### 3.1 Experiencia (ux-designer)
- Flujo y cantidad de pasos:
- Qué contiene cada pantalla y qué es lo más importante:
- Estados (vacío / carga / error / éxito / validaciones):
- Mensajes y textos:
- Accesibilidad de uso (teclado, foco, errores):

### 3.2 Visual (ui-designer)
- Distribución:
- Componentes existentes a reutilizar (ruta):
- Jerarquía, color, tipografía y espaciado (tokens):
- Responsive y contraste:

## 4. Diseño técnico (software-architect)
- Archivos a crear / modificar:
- Enfoque y patrones a seguir:
- Riesgos y mitigación:
- Qué NO tocar:

## 5. Pruebas (qa-tester)
| Caso | Tipo | Nivel | Test |
|---|---|---|---|
- Comando para verificar:
- Regresión a vigilar:

## 6. Documentación (tech-writer)
- Docs a actualizar:
`;
function tail(firstTask) {
  return `
## 7. Tareas
<!-- Primero lo que cambia para el usuario; el detalle técnico después. Formato:
- [ ] T1 · qué cambia (en simple) — Resp: dev-implementer — Modelo: perfil — Archivos: … — Hecho cuando: … -->
${firstTask || ''}
## 8. Bitácora de implementación (dev-implementer)

## 9. Preguntas abiertas, decisiones e historial
`;
}
function changeTemplate(id, title, level, model) {
  const body = level === 'S' ? S_SUMMARY : FULL_ROLES;
  return header(id, title, level, 'Borrador', model) + body + tail() + `- ${today()} — Creado en to-do/ (Borrador, nivel ${level})\n`;
}
function quickTemplate(id, title, model) {
  return header(id, title, 'XS', 'En desarrollo', model)
    .replace('- Qué vas a ver distinto:', `- Qué vas a ver distinto: ${title}`)
    .replace('- Cómo comprobarlo tú:', '- Cómo comprobarlo tú: revisarlo en la pantalla afectada')
    + tail(`- [ ] T1 · ${title} — Resp: dev-implementer — Hecho cuando: el usuario lo confirma`)
    + `- ${today()} — Cambio rápido (XS) aprobado por solicitud directa del usuario → in-progress/\n`;
}

function modelsTemplate() {
  return `# Modelos del equipo
_Última revisión: ${today()}_

Perfiles que usan el triage y las tareas ("Modelo sugerido: perfil"). Cambia de proveedor editando solo esta tabla.

| Perfil | Para qué | Proveedor / modelo (tú lo defines) |
|---|---|---|
| rapido | XS, textos, ajustes de UI, tareas mecánicas | |
| local | cambios acotados de código sin datos sensibles; trabajo offline | |
| estandar | S y M: implementación normal | |
| razonamiento | L, arquitectura, depuración difícil, crítica | |

## Reglas
- Por defecto: XS → rapido · S → estandar · M → estandar (diseño con razonamiento) · L → razonamiento.
- Código o datos sensibles → nunca a un proveedor no autorizado.
`;
}

// ---------- init ----------
const CLAUDE_BLOCK = `<!-- team:start -->
## Cómo trabaja este repo (equipo de skills)
- Lineamientos: \`docs/PRODUCT.md\`, \`docs/ARCHITECTURE.md\`, \`docs/UX-DESIGN.md\`, \`docs/UI-DESIGN.md\`, \`docs/TESTING.md\`, \`docs/DOCUMENTATION.md\`. Respétalos siempre.
- Hallazgos por rol: \`docs/backlog/*.md\`. Cambios a implementar: \`docs/backlog/{to-do,in-progress,done,archived}/CHG-NNN-*/CHANGE.md\`.
- Punto de entrada para el usuario: la skill \`team\` (Claude Code: \`/team\` · Codex: \`$team\`) elige el rol.
- Todo cambio de código pasa por un CHG aprobado: la skill \`dev-team-lead\` lo especifica y \`dev-implementer\` lo implementa (Claude Code: \`/nombre\` · Codex: \`$nombre\` · Gemini: pide usar la skill por su nombre).
- Estados y numeración SOLO con \`node .claude/team.mjs\` (\`help\` para ver todo). \`loop\` y \`critique\` son opcionales y solo los inicia el usuario. Modelos por perfil: \`docs/TEAM_MODELS.md\`.
- Regla para cualquier agente: no edites archivos fuera de \`docs/\` si no hay un CHG "En desarrollo" (\`node .claude/team.mjs status\`). En Claude Code lo impone un hook (\`.claude/hooks/team-guard.mjs\`); en otros CLI respétala igual. Nunca la desactives sin permiso del usuario.
<!-- team:end -->`;

function init() {
  const created = [];
  for (const f of FOLDERS) {
    const d = path.join(BK, f);
    if (!fs.existsSync(d)) { fs.mkdirSync(d, { recursive: true }); created.push(rel(d) + '/'); }
    const keep = path.join(d, '.gitkeep');
    if (!fs.existsSync(keep)) fs.writeFileSync(keep, '');
  }
  if (!fs.existsSync(path.join(DOCS, 'TEAM_PLAN.md'))) { syncPlan(); created.push('docs/TEAM_PLAN.md'); }
  if (!fs.existsSync(CONFIG_FILE)) { fs.mkdirSync(path.dirname(CONFIG_FILE), { recursive: true }); fs.writeFileSync(CONFIG_FILE, JSON.stringify(DEFAULT_CONFIG, null, 2) + '\n'); created.push('.claude/team.json (config de loop, opcional)'); }
  const mf = path.join(DOCS, 'TEAM_MODELS.md');
  if (!fs.existsSync(mf)) { fs.writeFileSync(mf, modelsTemplate()); created.push('docs/TEAM_MODELS.md (completa tus proveedores)'); }

  const cdir = path.join(ROOT, '.claude');
  fs.mkdirSync(path.join(cdir, 'hooks'), { recursive: true });
  const cliDest = path.join(cdir, 'team.mjs');
  if (path.resolve(SELF) !== path.resolve(cliDest)) { const ex = fs.existsSync(cliDest); fs.copyFileSync(SELF, cliDest); created.push('.claude/team.mjs' + (ex ? ' (actualizado)' : '')); }
  const guardSrc = path.join(path.dirname(SELF), 'team-guard.mjs');
  const guardDest = path.join(cdir, 'hooks', 'team-guard.mjs');
  if (fs.existsSync(guardSrc) && path.resolve(guardSrc) !== path.resolve(guardDest)) { const ex = fs.existsSync(guardDest); fs.copyFileSync(guardSrc, guardDest); created.push('.claude/hooks/team-guard.mjs' + (ex ? ' (actualizado)' : '')); }

  const sfile = path.join(cdir, 'settings.json');
  let settings = {};
  if (fs.existsSync(sfile)) { try { settings = JSON.parse(fs.readFileSync(sfile, 'utf8')); } catch { fail('.claude/settings.json no es JSON válido; corrígelo y repite.'); } }
  settings.hooks ??= {};
  settings.hooks.PreToolUse ??= [];
  const cmd = 'node "$CLAUDE_PROJECT_DIR/.claude/hooks/team-guard.mjs"';
  const already = JSON.stringify(settings.hooks.PreToolUse).includes('team-guard.mjs');
  if (!already) {
    settings.hooks.PreToolUse.push({ matcher: 'Edit|Write|MultiEdit|NotebookEdit', hooks: [{ type: 'command', command: cmd }] });
    fs.writeFileSync(sfile, JSON.stringify(settings, null, 2) + '\n');
    created.push('.claude/settings.json (hook team-guard)');
  }

  for (const f of ['CLAUDE.md', 'AGENTS.md', 'GEMINI.md']) {
    const file = path.join(ROOT, f);
    const cur = fs.existsSync(file) ? fs.readFileSync(file, 'utf8') : '';
    if (cur.includes('<!-- team:start -->')) continue;
    fs.writeFileSync(file, (cur ? cur.replace(/\s*$/, '\n\n') : '') + CLAUDE_BLOCK + '\n');
    created.push(`${f} (bloque del equipo)`);
  }
  registerProjects([ROOT]);
  out(created.length ? '✔ Inicializado:\n  ' + created.join('\n  ') : '✔ Ya estaba inicializado; nada que hacer.');
  out('Lineamientos de cada rol: se crean con sus skills (requieren aprobación).');
}


// ---------- registro de proyectos ----------
const REGISTRY = path.join(process.env.AGENT_TEAM_HOME || path.join(os.homedir(), '.agent-team'), 'projects.json');
const samePath = (a, b) => process.platform === 'win32' ? a.toLowerCase() === b.toLowerCase() : a === b;
function loadProjects() {
  if (!fs.existsSync(REGISTRY)) return [];
  try { return JSON.parse(fs.readFileSync(REGISTRY, 'utf8')).projects || []; }
  catch { fail(`${REGISTRY} no es JSON válido; corrígelo o bórralo.`); }
}
function saveProjects(list) {
  fs.mkdirSync(path.dirname(REGISTRY), { recursive: true });
  fs.writeFileSync(REGISTRY, JSON.stringify({ projects: list }, null, 2) + '\n');
}
function registerProjects(roots) {
  const list = loadProjects();
  const added = roots.map(r => path.resolve(r)).filter(r => !list.some(x => samePath(x, r)));
  if (added.length) saveProjects([...list, ...added]);
  return added;
}
function projectsCommand(args) {
  const [sub, target] = args;
  if (sub === 'scan') {
    if (!target) fail('Uso: projects scan <carpeta>');
    const base = path.resolve(target);
    if (!fs.existsSync(base)) fail(`No existe ${base}.`);
    const found = [base, ...fs.readdirSync(base, { withFileTypes: true }).filter(d => d.isDirectory()).map(d => path.join(base, d.name))]
      .filter(d => fs.existsSync(path.join(d, '.claude', 'team.mjs')));
    const added = registerProjects(found);
    out(`✔ ${found.length} proyecto(s) encontrados, ${added.length} nuevo(s).`);
    added.forEach(a => out(`  ${a}`));
    return;
  }
  if (sub === 'remove') {
    if (!target) fail('Uso: projects remove <carpeta>');
    const list = loadProjects();
    const left = list.filter(x => !samePath(x, path.resolve(target)));
    if (left.length === list.length) fail(`${path.resolve(target)} no está registrado.`);
    saveProjects(left);
    out(`✔ Quitado del registro: ${path.resolve(target)} (sus archivos no se tocan).`);
    return;
  }
  if (sub) fail('Uso: projects | projects scan <carpeta> | projects remove <carpeta>');
  const list = loadProjects();
  if (!list.length) out('Sin proyectos registrados. Detéctalos con: team projects scan <carpeta>');
  list.forEach(p => out(`${(fs.existsSync(p) ? copyVersion(p) : 'no existe').padEnd(12)} ${p}`));
}

// ---------- actualización de proyectos ----------
const TEAM_BLOCK_RE = /<!-- team:start -->[\s\S]*?<!-- team:end -->/;
const sameText = (a, b) => a.replace(/\r\n/g, '\n') === b.replace(/\r\n/g, '\n');
function copyVersion(root) {
  const f = path.join(root, '.claude', 'team.mjs');
  if (!fs.existsSync(f)) return 'sin instalar';
  return (/^const VERSION = '([^']+)'/m.exec(fs.readFileSync(f, 'utf8')) || [])[1] || 'sin versión';
}
function compareVersions(a, b) {
  const num = v => /^\d/.test(v) ? v.split('.').map(Number) : [0];
  const x = num(a), y = num(b);
  for (let i = 0; i < 3; i++) { const d = (x[i] || 0) - (y[i] || 0); if (d) return d; }
  return 0;
}
function isGlobalCli(root) { return !samePath(path.resolve(SELF), path.resolve(root, '.claude', 'team.mjs')); }
// Calcula los cambios sin escribir nada.
function projectChanges(root) {
  const changes = [], warnings = [];
  const tool = (src, relDest) => {
    if (!fs.existsSync(src)) return;
    const content = fs.readFileSync(src, 'utf8');
    const dest = path.join(root, relDest);
    if (!fs.existsSync(dest) || !sameText(fs.readFileSync(dest, 'utf8'), content)) changes.push({ rel: relDest, content });
  };
  tool(SELF, '.claude/team.mjs');
  tool(path.join(path.dirname(SELF), 'team-guard.mjs'), '.claude/hooks/team-guard.mjs');
  for (const f of ['CLAUDE.md', 'AGENTS.md', 'GEMINI.md']) {
    const file = path.join(root, f);
    if (!fs.existsSync(file)) continue;
    const cur = fs.readFileSync(file, 'utf8');
    if (!TEAM_BLOCK_RE.test(cur)) continue;
    const block = cur.includes('\r\n') ? CLAUDE_BLOCK.replace(/\n/g, '\r\n') : CLAUDE_BLOCK;
    const next = cur.replace(TEAM_BLOCK_RE, () => block);
    if (!sameText(cur, next)) changes.push({ rel: f, content: next });
  }
  const sfile = path.join(root, '.claude', 'settings.json');
  try {
    const settings = fs.existsSync(sfile) ? JSON.parse(fs.readFileSync(sfile, 'utf8')) : {};
    if (!JSON.stringify(settings.hooks?.PreToolUse || []).includes('team-guard.mjs')) {
      settings.hooks ??= {};
      settings.hooks.PreToolUse ??= [];
      settings.hooks.PreToolUse.push({ matcher: 'Edit|Write|MultiEdit|NotebookEdit', hooks: [{ type: 'command', command: 'node "$CLAUDE_PROJECT_DIR/.claude/hooks/team-guard.mjs"' }] });
      changes.push({ rel: '.claude/settings.json', content: JSON.stringify(settings, null, 2) + '\n' });
    }
  } catch { warnings.push('.claude/settings.json no es JSON válido; no se revisó el hook.'); }
  return { changes, warnings };
}
function applyChanges(root, changes) {
  for (const c of changes) {
    const dest = path.join(root, c.rel);
    fs.mkdirSync(path.dirname(dest), { recursive: true });
    fs.writeFileSync(dest, c.content);
  }
}
function changelogSince(from) {
  const file = path.join(path.dirname(SELF), '..', '..', '..', 'CHANGELOG.md');
  if (!fs.existsSync(file)) return '';
  const text = fs.readFileSync(file, 'utf8').replace(/\r\n/g, '\n');
  return text.split(/^(?=## )/m).filter(s => {
    const v = (/^## .*\bv(\d+(?:\.\d+){0,2})\s*$/m.exec(s.split('\n')[0]) || [])[1];
    return v && compareVersions(v, VERSION) <= 0 && compareVersions(v, from) > 0;
  }).join('').trim();
}
function confirm(question) {
  if (!process.stdin.isTTY) return false;
  process.stdout.write(`${question} [s/N] `);
  const buf = Buffer.alloc(16);
  const n = fs.readSync(0, buf, 0, 16, null);
  return /^s/i.test(buf.toString('utf8', 0, n).trim());
}
function update(args) {
  const all = args.includes('--all'), yes = args.includes('--yes'), dry = args.includes('--dry-run');
  if (!isGlobalCli(ROOT)) fail('Esta es la copia del proyecto. Actualiza con el comando global: team update');
  let roots;
  if (all) {
    roots = loadProjects();
    if (!roots.length) fail('Sin proyectos registrados. Detéctalos con: team projects scan <carpeta>');
  } else {
    if (!fs.existsSync(path.join(ROOT, '.claude', 'team.mjs'))) fail('Este repo no usa el equipo. Para instalarlo: team init');
    roots = [ROOT];
  }
  const pending = [];
  let lowest = VERSION;
  for (const root of roots) {
    if (!fs.existsSync(root)) { out(`⚠ ${root}: la carpeta no existe; se omite (quítala con: team projects remove "${root}").`); continue; }
    const from = copyVersion(root);
    if (from === 'sin instalar') { out(`⚠ ${root}: no tiene el equipo instalado; se omite.`); continue; }
    if (compareVersions(from, VERSION) > 0) { out(`⚠ ${root}: tiene ${from}, más nueva que ${VERSION}; se omite.`); continue; }
    const { changes, warnings } = projectChanges(root);
    warnings.forEach(w => out(`⚠ ${root}: ${w}`));
    if (!changes.length) { out(`✔ ${root}: al día (${from}).`); continue; }
    out(`↑ ${root}: ${from} → ${VERSION}`);
    changes.forEach(c => out(`    ${c.rel}`));
    pending.push({ root, changes });
    if (compareVersions(from, lowest) < 0) lowest = from;
  }
  if (!pending.length) { out('Nada que actualizar.'); return; }
  const notes = changelogSince(lowest);
  if (notes) out(`\nNovedades:\n${notes}`);
  if (dry) { out('\n(dry-run: no se modificó nada)'); return; }
  if (!yes && !confirm(`\n¿Actualizar ${pending.length} proyecto(s)?`)) {
    out(process.stdin.isTTY ? 'Cancelado; no se modificó nada.' : '\nNo se modificó nada. Para aplicarlo sin preguntar, repite con --yes.');
    return;
  }
  pending.forEach(p => applyChanges(p.root, p.changes));
  out(`✔ ${pending.length} proyecto(s) actualizados. No se hizo ningún commit: revisa y confirma en cada repo.`);
}
function outdatedWarning() {
  if (!isGlobalCli(ROOT)) return;
  const v = copyVersion(ROOT);
  if (v !== 'sin instalar' && compareVersions(v, VERSION) < 0) out(`\n⚠ Este repo usa la versión ${v} del equipo y hay ${VERSION}. Actualízalo con: team update`);
}

// ---------- configuración del loop (.claude/team.json) ----------
const CONFIG_FILE = path.join(ROOT, '.claude', 'team.json');
const DEFAULT_CONFIG = {
  _info: 'Configuración de "team loop" y "team critique". Solo la edita el usuario.',
  agent: ['claude', '-p', '--permission-mode', 'acceptEdits'],
  testCommand: '',
  batch: 3,
  maxTries: 3,
  maxDiffLines: { S: 300, M: 1000 },
  skillPrefix: '/',
  _ejemplos: {
    claude: ['claude', '-p', '--permission-mode', 'acceptEdits'],
    codex: ['codex', 'exec', '--full-auto', '{prompt}'],
    gemini: ['gemini', '--approval-mode', 'auto_edit', '-p', '{prompt}'],
    nota: 'Para Codex usa skillPrefix "$"; para Gemini "" (texto). Verifica los flags con --help de tu versión.',
  },
};
function loadConfig() {
  if (!fs.existsSync(CONFIG_FILE)) return { ...DEFAULT_CONFIG };
  try { return { ...DEFAULT_CONFIG, ...JSON.parse(fs.readFileSync(CONFIG_FILE, 'utf8')) }; }
  catch { fail('.claude/team.json no es JSON válido.'); }
}

// ---------- utilidades git / agente ----------
function git(argv, opts = {}) {
  const r = spawnSync('git', argv, { cwd: ROOT, encoding: 'utf8', ...opts });
  if (r.status !== 0 && !opts.allowFail) fail(`git ${argv.join(' ')}: ${(r.stderr || '').trim()}`);
  return opts.raw ? (r.stdout || '') : (r.stdout || '').trim();
}
function changedFiles() {
  git(['add', '-A']);
  const names = git(['diff', '--cached', '--name-only']).split('\n').filter(Boolean);
  const lines = git(['diff', '--cached', '--numstat']).split('\n').filter(Boolean)
    .reduce((s, l) => { const [a, d] = l.split('\t'); return s + (+a || 0) + (+d || 0); }, 0);
  return { names, lines };
}
function runAgent(cfg, prompt) {
  const argv = cfg.agent.map(a => a === '{prompt}' ? prompt : a);
  const viaStdin = !cfg.agent.includes('{prompt}');
  const r = spawnSync(argv[0], argv.slice(1), {
    cwd: ROOT, encoding: 'utf8', input: viaStdin ? prompt : undefined,
    shell: process.platform === 'win32', stdio: ['pipe', 'inherit', 'inherit'],
    env: { ...process.env, TEAM_LOOP: '1' },
  });
  if (r.error) fail(`No se pudo ejecutar el agente "${argv[0]}": ${r.error.message}. Revisa "agent" en .claude/team.json.`);
  return r.status;
}
function runTests(cmdline) {
  const r = spawnSync(cmdline, { cwd: ROOT, encoding: 'utf8', shell: true });
  const output = ((r.stdout || '') + '\n' + (r.stderr || '')).trim();
  return { ok: r.status === 0, output };
}
const fingerprint = s => crypto.createHash('sha1').update(s.replace(/\d+(\.\d+)?\s*(ms|s)\b/g, '').replace(/\d{2}:\d{2}:\d{2}/g, '').slice(-3000)).digest('hex');
const lastLines = (s, n = 60) => s.split('\n').slice(-n).join('\n');

// ---------- alcance permitido ----------
function allowedPaths(c, task) {
  const grab = s => (s || '').split(/[,;]/).map(x => x.replace(/[`*]/g, '').replace(/\(.*?\)/g, '').trim())
    .filter(x => x && /[./]/.test(x) && !/\s/.test(x));
  const s4 = sectionBody(c.text, 4) || '';
  const fromS4 = grab((/Archivos a crear \/ modificar:(.*)$/m.exec(s4) || [])[1]);
  const fromTask = grab((/Archivos:\s*([^—]*)/.exec(task) || [])[1]);
  return [...new Set([...fromS4, ...fromTask])];
}
function outOfScope(files, allowed) {
  const isTest = f => /(^|\/)(tests?|__tests__|spec)\//i.test(f) || /\.(test|spec)\.[a-z]+$/i.test(f);
  const ok = f => f.startsWith('docs/') || isTest(f) || allowed.some(a => {
    const p = a.replace(/^\.\//, '');
    if (p.includes('*')) return new RegExp('^' + p.split('*').map(x => x.replace(/[.+?^${}()|[\]\\]/g, '\\$&')).join('.*') + '$').test(f);
    return f === p || f.startsWith(p.replace(/\/?$/, '/'));
  });
  return files.filter(f => !ok(f));
}

// ---------- prompts ----------
function taskPrompt(c, taskId, extra = '') {
  const pre = loadConfig().skillPrefix ?? '/';
  return `${pre ? pre + 'dev-implementer' : 'Usa la skill dev-implementer.'} ${c.id}
Estás dentro de "team loop" (modo no interactivo). Implementa SOLO la tarea ${taskId} de ${rel(path.join(BK, 'in-progress', c.name, 'CHANGE.md'))}.
Reglas:
- No hagas preguntas: no hay nadie para responder.
- Toca solo los archivos que permiten §4 y la tarea. No ejecutes comandos de team.mjs ni hagas commits.
- Al terminar, marca [x] ${taskId} en §7 y agrega una línea en §8 (qué cambió, en simple).
- Si la spec no cuadra con el código, es ambigua, o necesitas tocar algo fuera del alcance: NO implementes. Agrega en §9 una línea que empiece exactamente con "DESVIACIÓN:" explicando el problema y termina.${extra}`;
}
function critiquePrompt(c) {
  const pre = loadConfig().skillPrefix ?? '/';
  return `${pre ? pre + 'dev-team-lead' : 'Usa la skill dev-team-lead.'}
Actúa como CRÍTICO de especificaciones. Lee ${rel(c.file)} y los lineamientos en docs/. NO implementes nada y NO edites otra cosa que §9 de ese archivo.
Busca: ambigüedades, contradicciones, supuestos no confirmados, criterios no verificables, tareas que no se podrían hacer sin adivinar, riesgos de romper algo existente, y si §0 "En palabras simples" no refleja lo que dicen las tareas.
Por cada problema agrega en §9 una línea: "- ${today()} — CRÍTICO: <pregunta concreta>?" (debe terminar en "?"). Máximo 7, priorizadas. Si no hay problemas importantes, agrega "- ${today()} — CRÍTICO: sin observaciones."`;
}

// ---------- loop ----------
function devTasks(c) {
  return c.tasks.map(t => ({ done: t[1] !== ' ', line: t[2], id: (/^(T\d+)/.exec(t[2]) || [])[1] }))
    .filter(t => t.id && /Resp:\s*dev-implementer/i.test(t.line));
}
// Marca la tarea real: la alternativa de comentario evita tocar el ejemplo de la plantilla.
function markTaskDone(text, id) {
  let marked = false;
  return text.replace(new RegExp(`<!--[\\s\\S]*?-->|^- \\[ \\] ${id}\\b`, 'gm'), m => {
    if (m.startsWith('<!--') || marked) return m;
    marked = true;
    return m.replace('[ ]', '[x]');
  });
}
function stopLoop(c, reason, hint) {
  const fresh = find(c.id);
  fs.writeFileSync(fresh.file, addHistory(fresh.text, `Loop detenido: ${reason}`));
  out(`\n⛔ Loop detenido: ${reason}`);
  if (hint) out(hint);
  out('Revisa los cambios sin confirmar con "git status" / "git diff --cached". Para descartar el intento actual: git reset --hard HEAD');
  process.exit(2);
}
function loop(idArg, flags) {
  const cfg = loadConfig();
  let c = find(idArg);
  if (!isGit()) fail('team loop necesita git (rama y commit por tarea para poder deshacer).');
  if (!['S', 'M'].includes(c.level)) fail(`team loop es para niveles S y M (este es ${c.level}). ${c.level === 'XS' ? 'Un XS se hace directo.' : 'Divide el L en varios M.'}`);
  if (flags.dryRun) {
    if (!['Aprobado', 'En desarrollo'].includes(c.state)) fail(`${c.id} está en "${c.state}"; el loop necesita Aprobado o En desarrollo.`);
    const ts = devTasks(c).filter(t => !t.done); const first = !devTasks(c).some(t => t.done);
    out(`(dry-run) ${c.id} · nivel ${c.level} · rama chg/${c.name.replace(/^CHG-/i, '').toLowerCase()} · ${first ? 'bala trazadora: solo ' + (ts[0] || {}).id : 'lote: ' + ts.slice(0, +(flags.batch || cfg.batch)).map(t => t.id).join(', ')}`);
    ts.slice(0, first ? 1 : +(flags.batch || cfg.batch)).forEach(t => out(`\n--- prompt ${t.id} ---\n${taskPrompt(c, t.id)}`));
    out('\n(dry-run: no se ejecutó ni se modificó nada)');
    return;
  }
  if (c.state === 'Aprobado') { execSync(`node "${SELF}" start ${c.id}`, { cwd: ROOT, stdio: 'inherit' }); c = find(c.id); }
  if (c.state !== 'En desarrollo') fail(`${c.id} está en "${c.state}"; el loop necesita Aprobado o En desarrollo.`);

  const dirty = git(['status', '--porcelain'], { raw: true }).split('\n').filter(Boolean).map(l => l.slice(3).replace(/^"|"$/g, '')).filter(f => !f.startsWith('docs/') && !f.startsWith('.claude/') && !['CLAUDE.md', 'AGENTS.md', 'GEMINI.md'].includes(f));
  if (dirty.length) fail(`Hay cambios sin confirmar fuera de docs/: ${dirty.slice(0, 5).join(', ')}. Haz commit o stash antes del loop.`);

  const branch = `chg/${c.name.replace(/^CHG-/i, '').toLowerCase()}`;
  const current = git(['rev-parse', '--abbrev-ref', 'HEAD']);
  if (current !== branch) {
    const exists = git(['branch', '--list', branch]);
    git(['checkout', ...(exists ? [] : ['-b']), branch]);
    out(`↳ Rama ${branch}`);
  }
  git(['add', '-A']);
  if (git(['diff', '--cached', '--name-only'])) git(['commit', '-q', '-m', `${c.id}: especificación y estado`]);

  const testCmd = cfg.testCommand || ((/Comando para verificar:\s*`?([^`\n]+?)`?\s*$/m.exec(sectionBody(c.text, 5) || '') || [])[1] || '').trim();
  if (!testCmd || /no aplica/i.test(testCmd)) fail('No hay comando de verificación. Defínelo en §5 ("Comando para verificar: …") o en "testCommand" de .claude/team.json. Sin verificación automática no hay loop.');

  let tasks = devTasks(c).filter(t => !t.done);
  if (!tasks.length) { out('No quedan tareas de dev-implementer.'); }
  const anyDone = devTasks(c).some(t => t.done);
  const tracer = !anyDone;
  const batch = tracer ? 1 : Math.max(1, +(flags.batch || cfg.batch));
  tasks = tasks.slice(0, batch);
  const maxTries = Math.max(1, +(flags.tries || cfg.maxTries));
  const maxDiff = (cfg.maxDiffLines || {})[c.level] || 1000;

  out(`\n▶ team loop ${c.id} · nivel ${c.level} · verificación: ${testCmd}`);
  out(tracer ? '  Bala trazadora: solo la primera tarea; luego revisas tú.' : `  Lote: ${tasks.map(t => t.id).join(', ')} · intentos por tarea: ${maxTries} · diff máx.: ${maxDiff} líneas`);
  c = find(c.id); fs.writeFileSync(c.file, addHistory(c.text, `Loop iniciado (${tracer ? 'bala trazadora' : 'lote ' + tasks.map(t => t.id).join(', ')})`));

  for (const task of tasks) {
    out(`\n━━ ${task.id} · ${task.line.split('—')[0].replace(/^T\d+\s*·\s*/, '').trim()}`);
    let extra = '', lastFp = null;
    for (let attempt = 1; attempt <= maxTries; attempt++) {
      out(`  intento ${attempt}/${maxTries}…`);
      const before = find(c.id).text;
      const code = runAgent(cfg, taskPrompt(c, task.id, extra));
      if (code !== 0) stopLoop(c, `el agente terminó con código ${code} en ${task.id}`);
      c = find(c.id);
      const newText = c.text.slice(0) ;
      if (/^.*DESVIACIÓN:/m.test(newText) && (newText.match(/DESVIACIÓN:/g) || []).length > (before.match(/DESVIACIÓN:/g) || []).length)
        stopLoop(c, `el agente reportó una desviación de la spec en ${task.id}`, 'Lee la línea "DESVIACIÓN:" en §9, corrige la spec con /dev-team-lead y vuelve a ejecutar el loop.');
      const { names, lines } = changedFiles();
      const code_ = names.filter(f => !f.startsWith('docs/'));
      if (!code_.length) stopLoop(c, `${task.id} no produjo cambios de código`);
      const allowed = allowedPaths(c, task.line);
      if (allowed.length) {
        const bad = outOfScope(names, allowed);
        if (bad.length) stopLoop(c, `${task.id} tocó archivos fuera del alcance: ${bad.slice(0, 5).join(', ')}`, `Permitidos por la spec: ${allowed.join(', ')}`);
      } else out('  ⚠ La spec no lista archivos (§4 o "Archivos:" en la tarea): sin control de alcance.');
      if (lines > maxDiff) stopLoop(c, `el cambio acumulado es de ${lines} líneas (máx. ${maxDiff} para nivel ${c.level})`, 'Puede que la spec sea más grande de lo previsto: divídela o sube el límite en .claude/team.json.');
      out(`  verificando: ${testCmd}`);
      const t = runTests(testCmd);
      if (t.ok) {
        c = find(c.id);
        let text = c.text;
        text = markTaskDone(text, task.id);
        text = addHistory(text, `Loop: ${task.id} hecho en ${attempt} intento(s), verificación OK`);
        fs.writeFileSync(c.file, text); syncPlan();
        git(['add', '-A']); git(['commit', '-q', '-m', `${c.id} ${task.id}: ${task.line.split('—')[0].replace(/^T\d+\s*·\s*/, '').trim()}`]);
        out(`  ✔ ${task.id} verificado y confirmado (commit).`);
        break;
      }
      const fp = fingerprint(t.output);
      if (fp === lastFp) stopLoop(c, `el mismo error se repitió en ${task.id}`, lastLines(t.output, 25));
      if (attempt === maxTries) stopLoop(c, `${task.id} no pasó la verificación tras ${maxTries} intentos`, lastLines(t.output, 25));
      lastFp = fp;
      extra = `\n\nEl intento anterior falló la verificación (${testCmd}). Corrige SOLO lo necesario para ${task.id}. Salida:\n${lastLines(t.output)}`;
    }
  }

  c = find(c.id);
  const pending = devTasks(c).filter(t => !t.done);
  if (tracer && pending.length) {
    out(`\n⏸ Bala trazadora lista. Revisa ${tasks[0].id} funcionando (guíate por §0). Si va bien: team loop ${c.id}`);
    out(`  Si va mal: git reset --hard HEAD~1, corrige la spec con /dev-team-lead y repite.`);
  } else if (pending.length) {
    out(`\n⏸ Lote terminado. Pendientes: ${pending.map(t => t.id).join(', ')}. Revisa y continúa con: team loop ${c.id}`);
  } else {
    out('\n✔ Todas las tareas de dev-implementer están hechas.');
    execSync(`node "${SELF}" verify ${c.id}`, { cwd: ROOT, stdio: 'inherit' });
    out(`  Demo: sigue §0 "En palabras simples". Rama: ${git(['rev-parse', '--abbrev-ref', 'HEAD'])}`);
  }
}

// ---------- ayuda ----------
const HELP = {
  init:     ['init', 'Instala el equipo en este repo: docs/backlog/, TEAM_PLAN.md, TEAM_MODELS.md, bloque en CLAUDE.md/AGENTS.md/GEMINI.md, hook de guardia (Claude Code) y .claude/team.json. Seguro de repetir: no pisa lo existente.', 'node .claude/team.mjs init'],
  status:   ['status [--all] [--json]', 'Tablero de cambios por carpeta con nivel, estado y avance. --all incluye archived/. --json entrega todos los datos sin modificar nada.', 'node .claude/team.mjs status'],
  next:     ['next [CHG-NNN]', 'Qué sigue en cada cambio activo y quién lo hace.', 'team next'],
  show:     ['show CHG-NNN', 'Resumen de un cambio y sus tareas pendientes.', 'node .claude/team.mjs show 3'],
  quick:    ['quick "título" [--model perfil]', 'XS: cambio trivial (texto, color, botón). Queda aprobado y En desarrollo al instante; luego "close".', 'node .claude/team.mjs quick "Cambiar texto del botón Guardar"'],
  new:      ['new "título" [--level S|M|L] [--model perfil]', 'Crea un CHG en to-do/ (Borrador) con la plantilla del nivel. Por defecto M.', 'node .claude/team.mjs new "Filtro por fecha" --level S'],
  level:    ['level CHG-NNN S|M|L', 'Cambia el nivel de un CHG que aún está en to-do/.', 'node .claude/team.mjs level 4 M'],
  validate: ['validate [CHG-NNN]', 'Revisa estructura, estado/carpeta, tareas y preguntas abiertas. Sin ID revisa todos.', 'node .claude/team.mjs validate 3'],
  critique: ['critique CHG-NNN [--dry-run]', 'Opcional: un agente crítico revisa la spec y escribe preguntas en §9. "approve" queda bloqueado hasta que las respondas.', 'node .claude/team.mjs critique 3'],
  approve:  ['approve CHG-NNN', 'Compuerta 2: Borrador → Aprobado. Falla si hay secciones vacías, tareas sin "Hecho cuando" o preguntas sin responder.', 'node .claude/team.mjs approve 3'],
  start:    ['start CHG-NNN [--force]', 'Aprobado → in-progress/ En desarrollo. Desde aquí el hook permite editar código.', 'node .claude/team.mjs start 3'],
  loop:     ['loop CHG-NNN [--batch N] [--tries N] [--dry-run]', 'Opcional: implementación automática con verificación: rama chg/…, un commit por tarea, bala trazadora (solo T1 la primera vez), reintentos con el error, y se detiene si sale del alcance, el diff crece demasiado, el error se repite o el agente reporta DESVIACIÓN. Solo S y M. Usa .claude/team.json.', 'node .claude/team.mjs loop 3 --dry-run'],
  verify:   ['verify CHG-NNN', 'En desarrollo → En verificación (exige las tareas de dev-implementer hechas).', 'node .claude/team.mjs verify 3'],
  reopen:   ['reopen CHG-NNN "motivo"', 'En verificación → En desarrollo (QA o el demo falló).', 'node .claude/team.mjs reopen 3 "el filtro ignora la zona horaria"'],
  close:    ['close CHG-NNN', 'Compuerta 3: → done/ Cerrado. Exige todas las tareas hechas (XS cierra directo).', 'node .claude/team.mjs close 3'],
  archive:  ['archive CHG-NNN "motivo"', '→ archived/ (Descartado o Archivado).', 'node .claude/team.mjs archive 5 "ya no se necesita"'],
  update:   ['update [--all] [--dry-run] [--yes]', 'Actualiza el CLI, la guardia y el bloque de reglas de este repo, o de todos los registrados con --all. Muestra antes qué cambiará, pide confirmación y nunca hace commits ni toca docs/.', 'team update --all --dry-run'],
  projects: ['projects [scan <carpeta> | remove <carpeta>]', 'Lista los proyectos registrados, detecta los de una carpeta o quita uno del registro.', 'team projects scan C:\\laragon\\www'],
  lessons:  ['lessons', 'Lecciones por incorporar a los documentos y cambios cerrados sin revisar.', 'team lessons'],
  ui:       ['ui [--port N] [--no-open]', 'Abre en el navegador el panel de solo lectura con todos los proyectos registrados.', 'team ui'],
  help:     ['help [comando | flujo]', 'Esta ayuda. "help flujo" muestra qué usar según el tamaño de la tarea.', 'node .claude/team.mjs help loop'],
};
const FLOW = `Qué usar según la tarea
  XS  botón, texto, color      quick "…"  → implementar → close
  S   ajuste acotado            new "…" --level S → llenar → approve → start → implementar → verify → close
  M   funcionalidad             /dev-team-lead (entrevista + roles) → approve → start → implementar → verify → close
  L   iniciativa grande         dividir en varios M

Opcionales (tú decides cuándo):
  critique CHG   antes de approve: un crítico busca huecos en la spec
  loop CHG       en lugar de implementar a mano: automático, con frenos y un commit por tarea

Sin loop, implementas con /dev-implementer CHG-NNN como siempre.`;
function nextStep(c) {
  switch (c.state) {
    case 'Borrador': return `completar la spec y aprobar: team approve ${c.id}`;
    case 'Aprobado': return `implementar: /dev-implementer ${c.id}`;
    case 'En desarrollo': {
      const t = c.tasks.find(x => x[1] === ' ');
      if (!t) return c.level === 'XS' ? `cerrar: team close ${c.id}` : `pasar a verificación: team verify ${c.id}`;
      const id = (/^(T\d+)/.exec(t[2]) || [])[1] || 'siguiente tarea';
      const resp = (/Resp:\s*([^—]+)/.exec(t[2]) || [])[1]?.trim() || 'sin responsable';
      return `${id} (${resp})`;
    }
    case 'En verificación': return `verificar: /qa-tester ${c.id}; si pasa: team close ${c.id}`;
    default: return '';
  }
}
const ROLE_SECTIONS = { pm: 2, arquitectura: 4, qa: 5, docs: 6 };
const statusOf = body => saysNotApplicable(body) ? 'no aplica' : hasContent(body) ? 'aportó' : 'pendiente';
// Indica si la sección del rol tiene contenido, no si su skill se ejecutó.
function roleStatus(c, n) {
  const body = sectionBody(c.text, n);
  if (body === null) return REQUIRED[c.level].includes(n) ? 'pendiente' : 'no aplica';
  return statusOf(body.replace(/<!--[\s\S]*?-->/g, ''));
}
function roles(c) {
  const parts = designParts(c);
  const missing = REQUIRED[c.level].includes(3) ? 'pendiente' : 'no aplica';
  const r = Object.fromEntries(Object.entries(ROLE_SECTIONS).map(([k, n]) => [k, roleStatus(c, n)]));
  return { ...r, ux: parts ? statusOf(parts.ux) : missing, ui: parts?.ui ? statusOf(parts.ui) : parts ? 'no aplica' : missing };
}
// Lecciones de §9: "LECCIÓN(rol): texto", incorporada si termina en "→ docs/ARCHIVO"; "LECCIÓN: ninguna" las descarta.
function lessonsOf(c) {
  const clean = (sectionBody(c.text, 9) || '').replace(/<!--[\s\S]*?-->/g, '');
  const found = [...clean.matchAll(/^.*?LECCI[ÓO]N(?:\(([^)]*)\))?:[ \t]*(.+?)[ \t]*\r?$/gm)].map(m => {
    const target = (/→\s*(docs\/\S+)$/.exec(m[2]) || [])[1] || null;
    return { role: (m[1] || '').trim(), text: m[2].replace(/\s*→\s*docs\/\S+$/, ''), target };
  });
  const items = found.filter(l => !/^ninguna\.?$/i.test(l.text));
  return { items, declared: found.length > 0 };
}
function lessonsSummary(c) {
  const { items, declared } = lessonsOf(c);
  return { items, unreviewed: !declared && c.folder === 'done' && c.level !== 'XS' };
}
function lessonsCommand() {
  if (!fs.existsSync(BK)) fail('No existe docs/backlog/. Ejecuta primero: team init');
  const all = listChanges().filter(c => c.folder !== 'archived').map(c => ({ c, ...lessonsSummary(c) }));
  const pending = all.flatMap(x => x.items.filter(l => !l.target).map(l => ({ ...l, id: x.c.id })));
  const unreviewed = all.filter(x => x.unreviewed).map(x => x.c.id);
  const done = all.reduce((s, x) => s + x.items.filter(l => l.target).length, 0);
  out(`Por incorporar: ${pending.length} · Incorporadas: ${done}`);
  pending.forEach(l => out(`  ${l.id}  ${l.role ? `(${l.role}) ` : ''}${l.text}`));
  if (pending.length) out('Al incorporar una, agrega al final de su línea en §9: → docs/ARCHIVO.md');
  if (unreviewed.length) out(`Cerrados sin revisar: ${unreviewed.join(', ')}`);
}
function snapshot() {
  const base = { project: path.basename(ROOT), root: ROOT, version: VERSION, copyVersion: copyVersion(ROOT) };
  if (!fs.existsSync(BK)) return { ...base, initialized: false, changes: [] };
  return {
    ...base,
    initialized: true,
    changes: listChanges().map(c => ({
      id: c.id, title: c.title, state: c.state, level: c.level, folder: c.folder, model: c.model,
      file: rel(c.file), done: c.done, total: c.total,
      tasks: c.tasks.map(t => ({
        id: (/^(T\d+)/.exec(t[2]) || [])[1] || '',
        title: t[2].split('—')[0].replace(/^T\d+\s*·\s*/, '').trim(),
        resp: (/Resp:\s*([^—]+)/.exec(t[2]) || [])[1]?.trim() || '',
        done: t[1] !== ' ',
      })),
      roles: roles(c),
      lessons: lessonsSummary(c),
      log: (sectionBody(c.text, 8) || '').split(/\r?\n/).filter(l => l.startsWith('- ')).slice(-5).map(l => l.slice(2)),
      next: nextStep(c),
    })),
  };
}
function nextCommand(idArg) {
  if (idArg) { const c = find(idArg); out(`${c.id} · ${c.title} · ${c.state}\n  Sigue: ${nextStep(c) || 'nada; está terminado.'}`); return; }
  if (!fs.existsSync(BK)) fail('No existe docs/backlog/. Ejecuta primero: team init');
  const active = listChanges().filter(c => !['done', 'archived'].includes(c.folder));
  if (!active.length) { out('Nada pendiente.'); return; }
  active.forEach(c => out(`${c.id}  ${c.state.padEnd(16)} ${nextStep(c)}`));
}
function summary() {
  out(`team ${VERSION}`);
  if (!fs.existsSync(BK)) { out('\nEste repo aún no usa el equipo. Para instalarlo: team init'); out('Ver todos los comandos: team help'); return; }
  const all = listChanges();
  const active = all.filter(c => !['done', 'archived'].includes(c.folder));
  const closed = all.filter(c => c.folder === 'done').length;
  out(`${path.basename(ROOT)}: ${active.length} activo(s), ${closed} cerrado(s)`);
  if (!active.length) out('\nNada pendiente. Para empezar: /team quiero …');
  active.forEach(c => out(`\n${c.id}  ${c.level}  ${c.state}  ${c.done}/${c.total}  ${c.title}\n  Sigue: ${nextStep(c)}`));
  outdatedWarning();
  out('\nVer todos los comandos: team help');
}
function help(topic) {
  if (topic === 'flujo' || topic === 'flow') { out(FLOW); return; }
  if (topic && HELP[topic]) { const [u, d, e] = HELP[topic]; out(`${u}\n\n${d}\n\nEjemplo:\n  ${e}`); return; }
  if (topic) out(`Comando desconocido: ${topic}\n`);
  out('team — CLI del equipo de skills   (team <comando>, o node .claude/team.mjs <comando>)\n');
  const groups = [['Instalar y ver', ['init', 'ui', 'status', 'next', 'lessons', 'show', 'help']], ['Crear', ['quick', 'new', 'level']], ['Especificar', ['validate', 'critique', 'approve']], ['Implementar', ['start', 'loop', 'verify', 'reopen']], ['Terminar', ['close', 'archive']], ['Proyectos', ['update', 'projects']]];
  for (const [g, cmds] of groups) { out(g); cmds.forEach(k => out(`  ${HELP[k][0].padEnd(52)} ${HELP[k][1].split(/[.:] /)[0].replace(/\.$/, '').slice(0, 70)}`)); out(''); }
  out('Más detalle: help <comando>  ·  Qué usar según la tarea: help flujo');
}

// ---------- comandos ----------
const [cmd, ...args] = process.argv.slice(2);
const needInit = () => { if (!fs.existsSync(BK)) fail('No existe docs/backlog/. Ejecuta primero: node .claude/team.mjs init'); };

switch (cmd) {
  case 'init': init(); break;

  case 'new':
  case 'quick': {
    needInit();
    const flag = (k) => { const i = args.indexOf(k); return i >= 0 ? args[i + 1] : undefined; };
    const skip = new Set(); ['--level', '--model'].forEach(k => { const i = args.indexOf(k); if (i >= 0) { skip.add(i); skip.add(i + 1); } });
    const title = args.filter((a, i) => !skip.has(i) && !a.startsWith('--')).join(' ').trim();
    if (!title) fail(`Uso: ${cmd} "Título" ${cmd === 'new' ? '[--level S|M|L] ' : ''}[--model perfil]`);
    const level = cmd === 'quick' ? 'XS' : (flag('--level') || 'M').toUpperCase();
    if (cmd === 'new' && !['S', 'M', 'L'].includes(level)) fail('--level debe ser S, M o L (para XS usa quick).');
    const model = flag('--model') || { XS: 'rapido', S: 'estandar', M: 'estandar', L: 'razonamiento' }[level];
    if (cmd === 'quick') {
      const active = listChanges().filter(x => x.state === 'En desarrollo');
      if (active.length && !args.includes('--force')) fail(`Ya hay un cambio En desarrollo: ${active.map(x => x.id).join(', ')}. Termínalo o usa --force.`);
    }
    const num = Math.max(0, ...listChanges().map(c => c.num)) + 1;
    const id = `CHG-${String(num).padStart(3, '0')}`;
    const folder = cmd === 'quick' ? 'in-progress' : 'to-do';
    const dir = path.join(BK, folder, `${id}-${slugify(title)}`);
    fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(path.join(dir, 'CHANGE.md'), cmd === 'quick' ? quickTemplate(id, title, model) : changeTemplate(id, title, level, model));
    syncPlan();
    out(`✔ ${id} (${level}, modelo ${model}) → ${rel(path.join(dir, 'CHANGE.md'))}`);
    if (cmd === 'quick') out(`  Ya está En desarrollo: implementa y luego "close ${id}".`);
    break;
  }

  case 'level': {
    const c = find(args[0]); const lv = (args[1] || '').toUpperCase();
    if (!['S', 'M', 'L'].includes(lv)) fail('Uso: level CHG-NNN S|M|L');
    if (c.folder !== 'to-do') fail('Solo se cambia el nivel en to-do/.');
    let t = c.text.replace(/^\*\*Nivel:\*\*.*$/m, `**Nivel:** ${lv}`);
    t = addHistory(t, `Nivel ${c.level} → ${lv}`);
    save(c, t);
    out(`✔ ${c.id} ahora es nivel ${lv}.` + (lv !== 'S' && c.level === 'S' ? ' Agrega las secciones §2-§6 que falten.' : ''));
    break;
  }

  case 'next': nextCommand(args[0]); break;

  case 'lessons': lessonsCommand(); break;

  case 'status': {
    if (args.includes('--json')) { out(JSON.stringify(snapshot(), null, 2)); break; }
    needInit();
    const all = listChanges();
    const show = args.includes('--all') ? FOLDERS : FOLDERS.filter(f => f !== 'archived');
    for (const f of show) {
      const items = all.filter(c => c.folder === f);
      out(`\n${f}/ (${items.length})`);
      items.forEach(c => out(`  ${c.id}  ${c.level.padEnd(2)} ${c.state.padEnd(16)} ${String(c.done + '/' + c.total).padEnd(6)} ${c.title}`));
    }
    const bad = all.filter(c => STATES[c.state] !== c.folder);
    if (bad.length) out(`\n⚠ Estado y carpeta no coinciden en: ${bad.map(c => c.id).join(', ')} (ejecuta validate).`);
    syncPlan();
    outdatedWarning();
    break;
  }

  case 'update': update(args); break;

  case 'ui': {
    const panel = path.join(path.dirname(SELF), 'team-ui.mjs');
    if (!fs.existsSync(panel)) fail('El panel solo está en la instalación global. Usa el comando: team ui');
    const { startPanel } = await import(pathToFileURL(panel).href);
    const i = args.indexOf('--port');
    startPanel({ cli: SELF, projects: loadProjects, version: VERSION, port: i >= 0 ? +args[i + 1] : 4599, open: !args.includes('--no-open'), log: out });
    break;
  }

  case 'projects': projectsCommand(args); break;

  case 'show': { const c = find(args[0]); out(rel(c.file)); out(`${c.id} · ${c.title} · ${c.level} · ${c.state} · modelo ${c.model} · tareas ${c.done}/${c.total}`); c.tasks.filter(t => t[1] === ' ').forEach(t => out(`  [ ] ${t[2].split('—')[0].trim()}`)); break; }

  case 'validate': {
    needInit();
    const list = args[0] ? [find(args[0])] : listChanges().filter(c => c.folder !== 'archived');
    let bad = 0;
    for (const c of list) { const r = validate(c, { forApproval: c.state === 'Borrador' && false }); out(`— ${c.id}`); report(c, r); if (r.errors.length) bad++; }
    if (!list.length) out('No hay cambios.');
    process.exit(bad ? 1 : 0);
  }

  case 'approve': {
    const c = find(args[0]);
    if (c.state !== 'Borrador') fail(`${c.id} está en "${c.state}"; solo se aprueba un Borrador.`);
    const r = validate(c, { forApproval: true });
    if (r.errors.length) { report(c, r); fail(`${c.id} no se puede aprobar todavía.`); }
    let t = setState(c.text, 'Aprobado'); t = setDate(t, 'Aprobado', today()); t = addHistory(t, 'Aprobado por el usuario (Compuerta 2)');
    save(c, t); out(`✔ ${c.id} Aprobado. Siguiente: /dev-implementer ${c.id}`);
    break;
  }

  case 'start': {
    const c = find(args[0]);
    if (c.state === 'En desarrollo') { out(`${c.id} ya está En desarrollo.`); break; }
    if (c.state !== 'Aprobado') fail(`${c.id} está en "${c.state}". Solo se inicia un cambio Aprobado (usa /dev-team-lead).`);
    const active = listChanges().filter(x => x.state === 'En desarrollo');
    if (active.length && !args.includes('--force')) fail(`Ya hay un cambio En desarrollo: ${active.map(x => x.id).join(', ')}. Termínalo o usa --force.`);
    let t = setState(c.text, 'En desarrollo'); t = addHistory(t, 'Movido a in-progress/ (En desarrollo)');
    const dir = save(c, t, 'in-progress'); out(`✔ ${c.id} En desarrollo → ${rel(dir)}`);
    break;
  }

  case 'verify': {
    const c = find(args[0]);
    if (c.state !== 'En desarrollo') fail(`${c.id} está en "${c.state}"; debe estar En desarrollo.`);
    const pendingDev = c.tasks.filter(t => t[1] === ' ' && /Resp:\s*dev-implementer/i.test(t[2]));
    if (pendingDev.length) fail(`Faltan tareas de dev-implementer: ${pendingDev.map(t => (/^(T\d+)/.exec(t[2]) || [])[1]).join(', ')}`);
    let t = setState(c.text, 'En verificación'); t = addHistory(t, 'Implementación terminada → En verificación');
    save(c, t); out(`✔ ${c.id} En verificación. Siguiente: /qa-tester verificación de ${c.id}`);
    break;
  }

  case 'reopen': {
    const c = find(args[0]);
    if (c.state !== 'En verificación') fail(`${c.id} está en "${c.state}"; solo se reabre desde En verificación.`);
    const why = args.slice(1).join(' ') || 'verificación fallida';
    let t = setState(c.text, 'En desarrollo'); t = addHistory(t, `Reabierto: ${why}`);
    save(c, t); out(`✔ ${c.id} vuelve a En desarrollo.`);
    break;
  }

  case 'close': {
    const c = find(args[0]);
    if (!(c.state === 'En verificación' || (c.level === 'XS' && c.state === 'En desarrollo'))) fail(`${c.id} está en "${c.state}"; solo se cierra desde En verificación${c.level === 'XS' ? '' : ' (los XS también desde En desarrollo)'}.`);
    const pending = c.tasks.filter(t => t[1] === ' ');
    if (pending.length) fail(`Tareas sin marcar: ${pending.map(t => (/^(T\d+)/.exec(t[2]) || [])[1]).join(', ')}`);
    let t = setState(c.text, 'Cerrado'); t = setDate(t, 'Cerrado', today()); t = addHistory(t, 'Cerrado por el usuario (Compuerta 3) → done/');
    const dir = save(c, t, 'done'); out(`✔ ${c.id} Cerrado → ${rel(dir)}`);
    if (c.level !== 'XS' && !lessonsOf(c).declared) out(`⚠ ${c.id} no declara lecciones. Agrega en §9 "LECCIÓN(rol): texto", o "LECCIÓN: ninguna" si no hubo.`);
    break;
  }

  case 'archive': {
    const c = find(args[0]);
    if (c.folder === 'archived') { out(`${c.id} ya está archivado.`); break; }
    const why = args.slice(1).join(' ');
    if (!why) fail('Uso: archive CHG-NNN "motivo"');
    const state = c.state === 'Cerrado' ? 'Archivado' : 'Descartado';
    let t = setState(c.text, state); t = addHistory(t, `${state}: ${why}`);
    const dir = save(c, t, 'archived'); out(`✔ ${c.id} ${state} → ${rel(dir)}`);
    break;
  }

  case 'critique': {
    const c = find(args[0]);
    if (!['Borrador', 'Aprobado'].includes(c.state)) fail(`${c.id} está en "${c.state}"; la crítica es antes de implementar.`);
    const prompt = critiquePrompt(c);
    if (args.includes('--dry-run')) { out(prompt); break; }
    const before = (c.text.match(/CRÍTICO:/g) || []).length;
    const code = runAgent(loadConfig(), prompt);
    if (code !== 0) fail(`El agente terminó con código ${code}.`);
    const after = find(c.id);
    const added = after.text.split('\n').filter(l => l.includes('CRÍTICO:')).slice(before);
    out(added.length ? `\nObservaciones del crítico (${c.id} §9):\n` + added.join('\n') : 'El crítico no agregó observaciones.');
    if (added.some(l => /\?\s*$/.test(l))) out('\nResponde cada pregunta en §9 (agrega "→ decisión" al final de la línea) antes de approve.');
    break;
  }

  case 'loop': {
    const flag = k => { const i = args.indexOf(k); return i >= 0 ? args[i + 1] : undefined; };
    loop(args[0], { batch: flag('--batch'), tries: flag('--tries'), dryRun: args.includes('--dry-run') });
    break;
  }

  case undefined: summary(); break;

  case '--version':
  case '-v':
    out(VERSION);
    break;

  case 'help':
  case '--help':
  case '-h':
    help(args[0]);
    break;

  default:
    help(cmd);
    process.exit(1);
}
