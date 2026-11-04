# CHG-001 · Transcripciones Markdown y apertura en Obsidian e Insiders
**Estado:** En verificación
**Nivel:** S
**Modelo sugerido:** estandar
<!-- Estados y nivel se cambian SOLO con node .claude/team.mjs. Modelo: un perfil de docs/TEAM_MODELS.md -->
**Creado:** 2026-11-03 · **Aprobado:** 2026-11-03 · **Cerrado:** —
**Relacionado:** —

## 0. En palabras simples
- Qué vas a ver distinto: las transcripciones nuevas y existentes usarán `.md`. Junto al título habrá botones con los logos de Obsidian y VS Code Insiders para abrir el documento consultado.
- Cómo comprobarlo tú: seleccionar una sesión, abrir Transcripción o Análisis y pulsar cada logo; el documento debe abrirse en la aplicación elegida, en su ubicación original.

## 1. Resumen
Cambiar el formato de transcripción y agregar apertura explícita en ambas aplicaciones. Incluye conversión de sesiones existentes conservando su contenido y relaciones. Especificación aprobada por el usuario en el chat el 2026-11-03.

## 2. Producto
- Convertir las sesiones existentes conservando contenido, identificadores, audio, análisis e historial de edición.
- Guardar nuevas transcripciones únicamente en `.md`; mantener el análisis separado en `.analysis.md`.
- No sobrescribir archivos ante colisiones ni convertir sesiones mientras se graban o procesan.
- No crear bóvedas ni copiar documentos para abrirlos.
- No aplica consulta adicional de producto: alcance explícito del usuario; cambio S revisado con arquitecto y QA.

## 3. UX/UI
Dos botones compactos junto al título, con logos, nombres accesibles y descripción al pasar el cursor. En Transcripción abren su `.md`; en Análisis, su `.analysis.md`. Se ocultan en las demás pestañas y quedan deshabilitados cuando no existe el documento. Los errores aparecen en la vista actual. Conservar los patrones de `static/manager.html`, `static/manager.js` y `static/manager.css`.
No aplica consulta adicional de UX: se conserva la interfaz existente y la ubicación aprobada.

## 4. Diseño técnico (software-architect)
Revisión realizada aplicando la skill software-architect, sin auditoría general ni modificación de sus lineamientos.

- Archivos previstos: `app.py`, `session_repository.py`, `session_edits.py`, `static/manager.html`, `static/manager.js`, `static/manager.css`, `tools/convert_transcripts_to_md.py`, `test_sessions.py`, `test_session_edits.py`, `test_transcript_migration.py`, `tools/validate_session_ui.py`, `README.md`.
- Actualizar creación, descubrimiento, apertura, renombrado, edición y referencias a las transcripciones. Conservar los contratos internos cuando sea posible; no renombrar campos como `txt_path` solo por estética.
- Excluir `.analysis.md` del descubrimiento de transcripciones. Conservar `.session`, audio y análisis; actualizar manifiestos, índices externos y referencias persistidas que dependan de la ruta. Preservar IDs de sesiones antiguas, actualmente derivados de la ruta cuando no tienen metadatos.
- Conversión recuperable: inspeccionar conflictos antes de modificar; conservar contenido; actualizar referencias y archivos de edición asociados; no sobrescribir destinos. Verificar con datos temporales antes de convertir las sesiones reales. La conversión no debe ejecutarse como efecto de listar o abrir una sesión.
- Apertura: resolver el documento desde la sesión seleccionada y una aplicación permitida; invocar el ejecutable con argumentos separados. No aceptar comandos o rutas arbitrarios del cliente.
- Obsidian: candidato instalado `C:\Program Files\Obsidian\Obsidian.exe`, pasando la ruta absoluta del `.md`. Se comprobó en el código local de la aplicación 1.14.5 el tratamiento de archivos externos; falta prueba visual. El ejecutable informa versión de instalador 1.8.10: comprobar compatibilidad y comunicar si necesita actualización, sin instalar por cuenta propia.
- Insiders: usar VS Code Insiders, disponible mediante `code-insiders.cmd`, y comprobar la resolución del ejecutable en Windows. No sustituirlo por VS Code estable.
- Evidencia: `app.py` crea `.txt` y expone apertura de análisis con `os.startfile`; `session_repository.py` y `session_edits.py` reconocen actualmente `.txt` y tienen referencias asociadas que deben conservarse. Inspección inicial: 9 `.txt` locales; recontar antes de convertir, pues el inventario puede variar.
- Fuente oficial: https://help.obsidian.md/manage-notes — archivos externos desde Obsidian 1.14, en su ubicación original; requiere instalador actualizado. No usar el URI dependiente de una bóveda para este flujo.

## 5. Pruebas (qa-tester)
Revisión realizada aplicando la skill qa-tester al diseño de pruebas; no se ha ejecutado la suite ni validado la implementación.

- En directorios temporales: sesiones con y sin manifiesto, audio, análisis y ediciones; preservar contenido e IDs tras convertir y reabrir.
- Colisiones: destino `.md` existente permanece intacto; no sobrescribir ni perder el origen. Fallos: recuperar la operación sin dejar referencias rotas. Repetición: no duplicar sesiones ni confundir análisis con transcripción.
- Regresión: crear, listar, abrir, editar, renombrar y eliminar una sesión temporal; conservar reproducción y análisis. Probar sesiones activas y trabajos en curso: no convertirlos.
- Apertura: ambos documentos en ambas aplicaciones, rutas con espacios y acentos, aplicación ausente, documento ausente y errores visibles. Automatizar la selección de aplicación sin lanzar programas reales desde los tests.
- UI manual: pestañas Transcripción/Análisis y restantes; nombres accesibles, navegación por teclado y ventana de tamaño mínimo del gestor. Comprobar apertura real con documentos de prueba, sin bóvedas ni copias.
- Comando de regresión: `python -m unittest test_sessions test_session_edits test_analysis test_analysis_pipeline test_reliability test_transcript_migration`. El módulo de migración se creará durante T2.

## 6. Documentación
Actualizar `README.md` con el formato, conversión recuperable y requisitos de apertura en Obsidian e Insiders. No aplica consulta adicional de tech-writer: actualización acotada incluida en T4.

## 7. Tareas
- [x] T1 · Guardar y gestionar transcripciones Markdown — Resp: dev-implementer — Modelo: estandar — Archivos: app.py, session_repository.py, session_edits.py, test_sessions.py, test_session_edits.py — Hecho cuando: las nuevas sesiones usan únicamente .md y funcionan listado, apertura, edición, renombrado y eliminación con sus archivos asociados, sin listar análisis como transcripciones.
- [x] T2 · Convertir las transcripciones existentes sin perder sus relaciones — Resp: dev-implementer — Modelo: estandar — Archivos: tools/convert_transcripts_to_md.py, session_repository.py, session_edits.py, test_transcript_migration.py — Hecho cuando: la conversión recuperable pasa los casos temporales de conservación, colisión y fallo; después se convierten las existentes con captura y procesamiento detenidos, se verifica reapertura y se informa resultado y cualquier conflicto sin sobrescribir archivos.
- [x] T3 · Abrir el documento consultado en Obsidian o VS Code Insiders — Resp: dev-implementer — Modelo: estandar — Archivos: app.py, static/manager.html, static/manager.js, static/manager.css, test_sessions.py, tools/validate_session_ui.py — Hecho cuando: ambos logos junto al título abren el documento de la pestaña activa en la aplicación elegida y cumplen estados, accesibilidad y manejo de errores definidos.
- [x] T4 · Verificar el resultado y documentar su uso — Resp: dev-implementer — Modelo: estandar — Archivos: README.md, tools/validate_session_ui.py, docs/backlog/to-do/CHG-001-transcripciones-markdown-y-apertura-en-o/CHANGE.md — Hecho cuando: se verifican los criterios de §5, se documentan formato y conversión, y la bitácora diferencia pruebas automatizadas, comprobación visual y conversión real, indicando cualquier limitación pendiente. El CLI actualizará la ubicación del cambio al iniciar desarrollo.

## 8. Bitácora de implementación (dev-implementer)
- 2026-11-03 — Inicio de implementación en rama main mediante `team start`. Solo existían archivos sin seguimiento de la instalación aprobada de team; no había cambios funcionales previos. No existen todavía lineamientos de arquitectura, UX o testing en docs/.
- 2026-11-03 — Base antes de editar: `python -m unittest discover` → 177 pruebas pasan. Aviso de deprecación existente de Starlette/httpx; sin cambios de dependencias.

- 2026-11-03 — T1 completada: nuevas capturas .md; compatibilidad con .txt conservada; descubrimiento excluye .analysis.md; apertura, edición, renombrado y archivos asociados admiten Markdown. Archivos: app.py, session_repository.py, session_edits.py, test_sessions.py y test_session_edits.py. Se agregaron tres pruebas de captura/reapertura y gestión de Markdown con y sin manifiesto. Verificación: 51 pruebas de sesiones/edición y suite completa de 180 pruebas pasan; git diff --check limpio. Sin conversión de archivos reales, apertura visual ni cambios de dependencias. T2–T4 pendientes.

- 2026-11-03 — T2: herramienta tools/convert_transcripts_to_md.py y pruebas test_transcript_migration.py. Vista previa sin escrituras; bloqueo con aplicación abierta o trabajos pendientes/en curso; control de colisiones y cambios desde la vista previa; registro duradero con recuperación conservadora. Se conserva el registro confirmado en state/transcript-md-migration.json (datos locales ignorados por Git).
- 2026-11-03 — Conversión real de 10 transcripciones, con cierre normal y posterior reinicio de Transcriptor. Verificados hashes originales de las 10 transcripciones, IDs, títulos, historia, marcadores, referencias de audio, contexto e insights. Listado reabierto mediante API: 10 sesiones, cero avisos, cero TXT restantes; todos los audios referenciados disponibles. No se probó aún apertura visual en editores (T3).
- 2026-11-03 — La comprobación real detectó que un manifiesto v1 no tenía created_at y su fecha visible dependía de mtime, modificado al actualizar su referencia. No fue posible recuperar con certeza el mtime anterior. El usuario eligió explícitamente 2026-10-28T10:38:10 desde el nombre original; se fijó created_at con esa fecha, con respaldo de la corrección en state/transcript-md-date-adjustment.json. La herramienta ahora conserva tiempos de modificación y fija el fallback original en manifiestos sin fecha; agrega dos regresiones de fecha y recuperación.

- 2026-11-03 — T2 completada: 12 pruebas específicas pasan; suite completa final `python -m unittest discover` → 192 pruebas pasan (99,766 s); git diff --check limpio. Sin commits ni cambios de dependencias. Pendientes T3 y T4.

- 2026-11-03 — T3 completada: logos compactos junto al título, nombres accesibles según pestaña, ocultos en las demás pestañas, deshabilitados si falta documento, privacidad o carga, y errores en aviso existente. Endpoint de apertura limitado a transcripción/análisis y Obsidian/Insiders; rutas resueltas por sesión y lanzamiento sin shell. Disponibilidad consultada sin crear documentos. Se conserva la apertura predeterminada previa del análisis.
- 2026-11-03 — T3 archivos: app.py, static/manager.html, static/manager.js, static/manager.css, test_sessions.py y tools/validate_session_ui.py. Pruebas específicas: cuatro combinaciones documento/editor, rutas con espacios/acentos/emoji, solicitudes inválidas, aplicación ausente, documento ausente y error del sistema. Insiders prioriza la instalación del usuario: el PATH local apuntaba a una versión de sistema anterior que no abrió el archivo en la instancia actual. Regresión añadida para esa prioridad.
- 2026-11-03 — Verificación T3: 54 pruebas de sesiones/edición y suite completa final de 195 pruebas pasan; node --check static/manager.js y git diff --check limpios. python tools/validate_session_ui.py --editors-only pasa en Edge: ambas pestañas/aplicaciones, estados ausentes, pestañas restantes, teclado, mensajes de error y ventanas 1100x760 / 760x500. Evidencia temporal: C:/Users/edwin/AppData/Local/Temp/transcriptor-ui-evidence-6vlnwsts.
- 2026-11-03 — Apertura real verificada con documentos sintéticos fuera de bóvedas mediante el mismo manejador: transcripción y análisis en Obsidian 1.14.5 y VS Code Insiders del perfil; títulos de ventanas confirmados con computer-use. No hizo falta actualizar Obsidian. Originales en C:/Users/edwin/AppData/Local/Temp/transcriptor-editor-CHG001-ymlikkhc; sin .obsidian en esa carpeta. Se conservan para que los editores no pierdan sus archivos abiertos.
- 2026-11-03 — Reinicio normal de Transcriptor sin captura/análisis en curso: 10 sesiones, 10 transcripciones y 3 documentos de análisis disponibles; cero avisos. Pendiente T4 (documentación y verificación final del cambio).

- 2026-11-03 — T4 en curso: README actualizado a Markdown, apertura en ambos editores, conversión explícita, recuperación y comprobaciones. La validación UI general encontró un selector ambiguo preexistente entre diccionario e insights; se acotó al panel de mappings dentro del validador permitido.

- 2026-11-03 — Validación general UI adicional: después de corregir el selector del validador, avanzó por diccionario/revisión/ediciones pero se detuvo en validate_browser, prueba de audio (#probeResults): esperaba «Señal detectada» y recibió «Fuente actualizada. Repite la prueba.». No se modificó captura ni se amplió la investigación fuera del cambio. La validación específica --editors-only y las 195 pruebas automatizadas de T3 permanecen como últimas comprobaciones funcionales satisfactorias. T4 no marcada ni pasada a verificación mientras se resuelve la ampliación de alcance para review.py.

- 2026-11-03 — T4 completada: README documenta formato Markdown, apertura, conversión y recuperación, comandos de validación y limitaciones conocidas. Se conserva como última suite completa satisfactoria la de T3: 195 pruebas, sin cambios funcionales posteriores; el ajuste del selector del validador compila y git diff --check pasa. La validación UI general adicional quedó incompleta en la prueba de audio y se entrega como limitación a QA; no se declara aprobada. El usuario difirió expresamente el caso de revisión de Markdown sin manifiesto. Implementación terminada, pendiente verificación independiente y cierre por dev-team-lead.

## 9. Preguntas abiertas, decisiones e historial
- 2026-11-03 — Creado en to-do/ (Borrador, nivel S)
- 2026-11-03 — Usuario eligió nivel S, autorizó instalar team y convertir también las transcripciones existentes a Markdown.
- 2026-11-03 — Usuario autorizó investigar la apertura directa en Obsidian; comprobado soporte en código instalado, pendiente comprobación visual.
- 2026-11-03 — Usuario aprobó la especificación mostrada en chat y su guardado (opción A). Sin preguntas de alcance abiertas.
- Perfil estandar: proveedores pendientes de configurar por el usuario en docs/TEAM_MODELS.md; no se seleccionaron proveedores externos.
- 2026-11-03 — Aprobado por el usuario (Compuerta 2)
- 2026-11-03 — Movido a in-progress/ (En desarrollo)
- 2026-11-03 — Usuario autorizó continuar con T2 y eligió la fecha 2026-10-28T10:38:10 para el manifiesto antiguo sin fecha, tras informar la diferencia detectada.
- 2026-11-03 — DESVIACIÓN detectada en T4: review.py:146 solo reconoce .txt como transcripción sin manifiesto. Dos .md independientes apuntan al mismo review.json de la carpeta; antes cada TXT usaba su propio .review.json. Reproducción sintética confirmada. No afecta las 10 sesiones reales convertidas, que tienen .session. Propuesta: reconocer .md en esa condición y añadir regresión en test_review.py. El usuario decidió expresamente dejar ese ajuste para otro cambio; no se añadieron review.py ni test_review.py al alcance.
- 2026-11-03 — Decisión del usuario: dejar para otro cambio la revisión de palabras de Markdown sin .session. Se documenta la limitación; no bloquea la entrega de CHG-001 a verificación.
- 2026-11-03 — Implementación terminada → En verificación
