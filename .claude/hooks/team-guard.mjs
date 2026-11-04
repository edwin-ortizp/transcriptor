#!/usr/bin/env node
// team-guard — hook PreToolUse de Claude Code.
// Bloquea ediciones de archivos fuera de docs/ si no hay un CHG "En desarrollo".
// Desactivar temporalmente (solo el usuario): crear .claude/team-guard.off o TEAM_GUARD=off
import fs from 'node:fs';
import path from 'node:path';

let raw = '';
process.stdin.on('data', d => (raw += d)).on('end', () => {
  let input;
  try { input = JSON.parse(raw); } catch { process.exit(0); }
  const root = process.env.CLAUDE_PROJECT_DIR || input.cwd || process.cwd();
  const ti = input.tool_input || {};
  const fp = ti.file_path || ti.notebook_path;
  if (!fp) process.exit(0);

  const rel = path.relative(root, path.resolve(root, fp)).split(path.sep).join('/');
  if (rel.startsWith('../') || path.isAbsolute(rel)) process.exit(0); // fuera del proyecto: no aplica

  const block = msg => { process.stderr.write(`team-guard: ${msg}\n`); process.exit(2); };

  // La configuración de la guardia solo la cambia el usuario.
  if (rel === '.claude/team-guard.off' || rel.startsWith('.claude/hooks/') || /^\.claude\/settings(\.local)?\.json$/.test(rel) || rel === '.claude/team.mjs' || rel === '.claude/team.json') {
    block(`${rel} es configuración del equipo; solo el usuario puede modificarla a mano.`);
  }

  if (process.env.TEAM_GUARD === 'off' || fs.existsSync(path.join(root, '.claude', 'team-guard.off'))) process.exit(0);

  // Documentación: permitida (las skills piden aprobación antes de escribir).
  if (rel.startsWith('docs/') || ['CLAUDE.md', 'AGENTS.md', 'GEMINI.md'].includes(rel)) process.exit(0);

  // Código: solo con un cambio En desarrollo.
  const ip = path.join(root, 'docs', 'backlog', 'in-progress');
  const active = [];
  try {
    for (const d of fs.readdirSync(ip)) {
      const f = path.join(ip, d, 'CHANGE.md');
      if (fs.existsSync(f) && /^\*\*Estado:\*\*\s*En desarrollo\s*$/m.test(fs.readFileSync(f, 'utf8'))) active.push(d);
    }
  } catch { /* sin backlog */ }
  if (active.length) process.exit(0);

  block(`edición de "${rel}" bloqueada: no hay ningún CHG "En desarrollo" en docs/backlog/in-progress/. ` +
    `Especifica el cambio con /dev-team-lead, apruébalo e inícialo con /dev-implementer (node .claude/team.mjs start CHG-NNN). ` +
    `Si el usuario quiere editar sin CHG, él debe crear .claude/team-guard.off.`);
});
