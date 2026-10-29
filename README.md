# Transcriptor de reuniones (Whisper local)

Transcribe en vivo el audio que suena en el PC y, opcionalmente, el micrófono. Usa faster-whisper en local, así que el audio no sale del equipo. El análisis de texto mediante API es opcional y está desactivado hasta configurarlo y habilitarlo.

## Uso

```bash
pip install -r requirements.txt
# Solo en equipos con GPU NVIDIA:
pip uninstall -y onnxruntime
pip install -r requirements-gpu.txt

python app.py                  # abre la ventana propia (puerto 8791 o el siguiente libre)
python app.py --browser        # en el navegador
python app.py --download turbo # descarga Whisper large-v3-turbo (~1,5 GB) y sale; también: parakeet
python app.py --cpu            # forzar CPU
python app.py --threads 4      # núcleos a usar en CPU
```

**Modelos:** la app busca primero en la carpeta `modelos/` y luego en las de SpeechPulse (`%LOCALAPPDATA%\SpeechPulse\models`, cuyas DLL de CUDA también aprovecha). Si un modelo no está, aparece en el selector como "(descargar …)": al pulsar Iniciar o "Descargar ahora" se baja **una sola vez** a `modelos/` con barra de progreso. Más carpetas: variable `TRANSCRIPTOR_MODELS_DIR`.

## Repositorio y modelos

El repositorio Git contiene el código, la fuente Nunito y los vocabularios globales de español e inglés (~1,5 MB), con sus licencias. El `.gitignore` deja fuera:

| Excluido | Por qué |
|---|---|
| `modelos/` | Pesan 0,6–1,5 GB; GitHub rechaza archivos > 100 MB. Cada equipo los obtiene una vez. |
| `transcripciones/` | Son las conversaciones grabadas: privadas. |
| `settings.json` | Preferencias de cada equipo (IDs de micrófono, etc.). Se crea solo con valores por defecto. |
| `transcriptor.log`, `__pycache__/` | Archivos generados. |

**De dónde salen los modelos** (se descargan de Hugging Face; son las mismas fuentes que usan por defecto las librerías faster-whisper y onnx-asr):

| Modelo | Repositorio que se descarga | Modelo original y licencia |
|---|---|---|
| Whisper large-v3-turbo | [mobiuslabsgmbh/faster-whisper-large-v3-turbo](https://huggingface.co/mobiuslabsgmbh/faster-whisper-large-v3-turbo) — conversión a CTranslate2 de Mobius Labs | OpenAI `whisper-large-v3-turbo`, MIT |
| Parakeet TDT v3 | [istupakov/parakeet-tdt-0.6b-v3-onnx](https://huggingface.co/istupakov/parakeet-tdt-0.6b-v3-onnx) — conversión a ONNX (int8) | NVIDIA `parakeet-tdt-0.6b-v3`, CC-BY-4.0 |

**Copia en GitHub (si Hugging Face está bloqueado):** los mismos modelos están como `.zip` en la release [`modelos-v1`](https://github.com/edwin-ortizp/transcriptor/releases/tag/modelos-v1) de este repositorio, con su licencia y `ORIGEN.md` dentro (`whisper-large-v3-turbo.zip` ~1,5 GB, `parakeet-tdt-v3.zip` ~640 MB). Se generan con `python tools/empaquetar_modelos.py` a partir de los modelos de SpeechPulse. Los `.zip` no están en el historial de Git: son archivos adjuntos de la release.

**Formas de tener el modelo en un equipo:**
1. **Descarga automática** (`python app.py --download turbo` o el botón en la app): intenta Hugging Face y, si no responde, usa la release de GitHub.
   - **Solo GitHub** (equipos donde Hugging Face está bloqueado): `python app.py --download turbo --source github`, o en ⚙ → "Descargar modelos desde" → "Solo GitHub" (queda guardado). Así la app **nunca** contacta Hugging Face: ni para descargar ni las librerías por su cuenta (`HF_HUB_OFFLINE=1`). Con un modelo ya instalado y el análisis mediante API desactivado, la transcripción no necesita conexiones de red.
2. **Importar un .zip** bajado a mano desde la release (con el navegador): ⚙ → "Importar modelo (.zip)…" o `python app.py --import whisper-large-v3-turbo.zip`.
3. **Copiar la carpeta** de un modelo a `modelos/` (p. ej. `multi_turbo` de SpeechPulse).
4. **Tener SpeechPulse instalado.**

En ⚙, debajo del modelo, se ve de dónde viene el elegido.

## Instalar en un portátil (sin GPU ni SpeechPulse)

1. Instala Python 3.11 o superior y `git clone` del repositorio (trae solo el código).
2. En la carpeta: `pip install -r requirements.txt` (no instales `requirements-gpu.txt`).
3. Consigue el modelo **una vez**, de cualquiera de estas formas:
   - `python app.py --download turbo` (~1,5 GB), o
   - abre la app, elige "Whisper large-v3-turbo (descargar …)" y pulsa Iniciar, o
   - **sin internet:** copia la carpeta `%LOCALAPPDATA%\SpeechPulse\models\multi_turbo` del PC a `transcriptor\modelos\multi_turbo` en el portátil (USB o Sync). Se detecta sola.
4. Abre `transcriptor.bat`. En ⚙ → "Uso del procesador" eliges cuántos núcleos usa (Ligero por defecto).

Para tener un respaldo rápido: `python app.py --download parakeet` (~640 MB).

Las transcripciones se guardan en `transcripciones/AAAA-MM-DD_HHMMSS <título>.txt` (el título se edita en la cabecera de la ventana), una línea por fragmento:

```
[00:12:34] [Otros] texto de la persona remota
[00:12:41] [Yo] mi respuesta
```

**Ventana:** widget sin marco que se arrastra desde la cabecera o el título (doble clic en el título para renombrarlo). 📌 la deja siempre encima, ⚙ abre los ajustes, — minimiza y ✕ termina la captura y oculta el widget; la aplicación permanece en la bandeja. Desde allí puedes volver a abrirla o elegir Salir. Los círculos YO/OT se iluminan cuando cada fuente habla. Tipografía Nunito incluida en `static/fonts` (licencia OFL). Sin consola: doble clic en `transcriptor.bat`.

**Fuentes en el pie, siempre a la mano y cambiables en caliente:** 🎙 enciende/apaga el micrófono (tachado = apagado) y su ▾ elige cuál; 🔊 hace lo mismo con la salida de audio capturada (Otros). Se pueden cambiar en plena grabación sin reiniciar: las horas siguen continuas y el .txt anota el cambio (`[00:12:03] — micrófono: Logitech BRIO —`).

**Ajustes (⚙):** modelo, idioma, transparencia (30–100 %, con opción "opaca al pasar el mouse") y diccionario. Todo se recuerda en `settings.json`; si un dispositivo guardado ya no existe, se usa el predeterminado.

## Diccionarios globales y mappings personales

En **Sesiones → Diccionario** hay tres vistas independientes:

- **Términos del sistema:** vocabulario general de español e inglés, más el glosario de términos propios. Permite buscar y paginar entradas, consultar su origen, versión y número de términos. Es de solo lectura: los cambios se realizan en el repo. Los datos Hunspell y sus licencias están en `dictionaries/`, con commit y checksums en `sources.json`; no se descargan durante el uso. Se muestran entradas de referencia, sin expandir afijos ni afirmar que todas las formas derivadas o variantes regionales estén incluidas. El glosario propio comienza vacío y su formato está documentado en `dictionaries/README.md`.
- **Mis términos:** vocabulario personal de palabras o términos aceptados. Se guarda en `%LOCALAPPDATA%\Transcriptor\terms.tsv`, fuera del repo, con un término por línea en UTF-8. Permite buscar, agregar y eliminar entradas, con revisión del archivo, respaldos y guardado seguro. No genera reemplazos ni formas derivadas; mantiene acentos y agrupa diferencias de mayúsculas.
- **Mis mappings:** reglas del usuario, como `si go` → `Siigo`. Puedes buscar, agregar, editar y eliminar reglas; ver el archivo de cada una y probar texto con las reglas guardadas. Un destino vacío elimina la coincidencia. No se agregan términos globales al guardar un mapping.

Se conservan los TSV existentes, con formato `origen\tdestino\tword|any\tcase|nocase`. `word` compara una palabra o frase completa, `any` cualquier parte; `case` distingue mayúsculas y `nocase` las ignora. Se admiten las variantes antiguas de dos o tres columnas, con `word` y `nocase` como valores por defecto. Se preserva la prioridad actual de grupos y de reglas largas.

La selección mantiene esta prioridad: `--dict ruta.tsv` > variable `TRANSCRIPTOR_DICT` > preferencias > `~/Sync/android-apps-backups/dictate-keyboard/dictate_mappings-tcl.tsv`. Varios archivos se separan con `;`. El gestor muestra la configuración efectiva y exige elegir dónde agregar una regla si hay varios archivos. Los controles anteriores de ruta y apertura externa siguen en ⚙ del widget. Una preferencia no sustituye un `--dict` o variable de entorno activos.

Si no existe ningún archivo efectivo y no hay una configuración de mayor prioridad, el gestor ofrece crear o utilizar `%LOCALAPPDATA%\Transcriptor\mappings.tsv`, fuera del repositorio y propio del usuario de Windows. Esta acción es explícita y no traslada archivos anteriores. Los vocabularios de `dictionaries/` están protegidos contra escritura incluso si se intenta configurarlos como mappings.

Antes de modificar un TSV existente se crea a su lado un respaldo con sufijo `.bak-<fecha>-<id>`. Se conservan comentarios, líneas vacías, BOM UTF-8, saltos de línea y el orden de las reglas. Las líneas malformadas se muestran como avisos, se ignoran al aplicar correcciones y no se eliminan al editar otras filas. Se rechazan campos vacíos en el origen, tabulaciones o saltos de línea dentro de los campos y nuevas combinaciones duplicadas de origen, coincidencia y tratamiento de mayúsculas.

El guardado comprueba la revisión del archivo y utiliza un temporal y reemplazo atómico. Si otro programa cambia el TSV, no se sobrescribe silenciosamente: el formulario conserva el borrador y **Recargar** permite revisar el estado actual. Si la regla original sigue siendo identificable, se puede guardar el borrador sobre esa regla; si cambió o desapareció, hay que reabrirla o elegir explícitamente usar el borrador como una regla nueva.

**Los cambios afectan a las próximas grabaciones.** Cada sesión fija sus reglas y las palabras de orientación de Whisper al iniciar. Editar mappings no altera una grabación en curso ni reescribe transcripciones anteriores. **Probar texto** usa las reglas actualmente guardadas y no ejecuta el motor de transcripción.

La gestión de vocabulario y mappings no utiliza la API ni modifica automáticamente las transcripciones. La búsqueda local de términos no reconocidos se describe a continuación.

## Palabras sospechosas

En el detalle de una sesión hay una cuarta pestaña: **Palabras sospechosas**. Pulsa **Buscar palabras** para revisar una sesión terminada; también funciona con sesiones anteriores y TXT antiguos. Abrir la pestaña, terminar una grabación o cambiar un diccionario no inicia la búsqueda automáticamente.

La búsqueda es local, sin llamadas a la API. Compara el texto guardado con español e inglés mediante `spylls` y sus reglas Hunspell (`.dic` y `.aff`), el glosario del repo, los términos personales y las coincidencias de mappings efectivos. Reconoce formas derivadas como plurales y conjugaciones. Una frase conocida no hace válidas sus palabras sueltas fuera de esa frase. Los mappings respetan sus opciones de coincidencia y mayúsculas.

Se agrupan términos equivalentes en Unicode y mayúsculas, conservando acentos y variantes para mostrarlos. Se excluyen números puros, enlaces con esquema (como `https://` o `ftp://`) o prefijo `www.` y correos. Se conservan términos técnicos con letras y números, guiones y apóstrofos internos. Los resultados muestran frecuencia, variantes y contextos con tiempos; se ordenan por frecuencia y permiten filtrar, buscar y paginar. Pulsar un contexto lleva al bloque transcrito y posiciona el audio si está disponible, respetando el bloqueo durante otra captura del equipo.

«Sospechosa» significa **término no reconocido**, no un error confirmado. Los nombres, siglas y vocabulario especializado pueden necesitar aceptación manual. Una palabra válida usada equivocadamente en una frase puede pasar inadvertida: esta revisión no comprueba el significado de la conversación ni la certeza acústica del motor.

Acciones por término:

- **Agregar al diccionario personal:** reconocerlo para todas tus reuniones; no modifica el repo ni crea un reemplazo. Se retira de los pendientes de esta sesión.
- **Ignorar en esta reunión:** retirarlo solo de los pendientes de esa sesión, con opción **Deshacer ignorar**. Puede volver a aparecer en otras reuniones.
- **Crear mapping:** abrir el editor con el origen precargado. Debes indicar y guardar el reemplazo expresamente; el mapping afecta a próximas grabaciones, sin reescribir esta transcripción. Solo se registra como atendido si la regla cubre todas las variantes del término en la sesión.

Los filtros **Pendientes** y **Revisadas** conservan las decisiones al reabrir la aplicación. Los términos aceptados se gestionan en **Diccionario → Mis términos**. Si eliminas uno o retiras un mapping, puede volver a aparecer al buscar nuevamente. Las decisiones de ignorar permanecen propias de la reunión.

Los resultados y decisiones se guardan en `review.json` dentro de la carpeta `.session`; para un TXT antiguo, en un archivo vecino `.review.json`. No se modifican el TXT, el manifiesto ni el análisis. Si cambian el texto, los vocabularios o los mappings, aparece **Resultados desactualizados**; debes pulsar Buscar palabras para recalcular. Una búsqueda fallida conserva los resultados anteriores y muestra el error.

La búsqueda trabaja fuera de la interfaz y puede ejecutarse para una reunión anterior mientras el widget captura otra. La discreción también oculta esta pestaña y el vocabulario personal.

## Texto en vivo

Mientras alguien habla, la frase en curso se retranscribe cada ~0,6 s y se muestra en gris con un cursor (texto provisional). Al detectar una pausa, se reemplaza por la versión final: mejor contexto, diccionario aplicado y es la única que se guarda en el .txt. Se activa o desactiva en ⚙ → "Transcripción en vivo". Si el motor va lento (p. ej. Whisper en CPU), el provisional se pausa solo para no retrasar los finales.

## Whisper vs Parakeet

| | Whisper (large-v3-turbo) | Parakeet TDT v3 |
|---|---|---|
| Retraso | ~8–12 s | ~2–4 s |
| Idioma | se elige en la UI | autodetecta (25 idiomas europeos) |
| Precisión en español | la mejor | un poco menor |

El Parakeet de SpeechPulse (`models\parakeet_tdt`) se carga directo con `onnx-asr`; sin SpeechPulse se descarga de Hugging Face.

## Requisitos y equipos sin GPU

Medido en el PC de desarrollo forzando CPU (int8):

| Modelo | Núcleos | Velocidad | RAM |
|---|---|---|---|
| Whisper large-v3-turbo, bloques largos (~24 s) | 3 | ~2× tiempo real | ~1,9 GB |
| Whisper large-v3-turbo, bloques cortos (3–12 s) | 4 | ~1× (no alcanza) | ~1,9 GB |
| Parakeet TDT v3 | 4 | 14× tiempo real | ~0,8 GB |
| Parakeet TDT v3 | 2 | 10× tiempo real | ~0,8 GB |

Whisper siempre procesa una ventana de 30 s: en CPU tarda casi lo mismo con 3 s que con 24 s de audio. Por eso, **sin GPU, Whisper usa bloques largos (15–28 s) y no muestra texto en vivo**: así rinde ~2× tiempo real. El texto aparece por bloques, con unos 20–40 s de retraso; si se acumula trabajo, el estado muestra "con atraso" y se recupera en las pausas. Al terminar o cerrar, la app sigue hasta transcribir todo lo pendiente (nunca queda incompleto el .txt).

Un portátil de hace varios años puede ser 2–3× más lento que el PC de las mediciones: con Turbo puede ir justo; sube "Uso del procesador" a Equilibrado o Máximo si se atrasa, o usa Parakeet, que va sobrado.

**Mínimo recomendado sin GPU:** CPU de 4 núcleos con AVX2 (Intel Core 6.ª gen. / Ryzen o posterior), **8 GB de RAM** para Turbo (Parakeet funciona con menos) y ~3 GB de disco.

**Para no trabar el equipo:** la app siempre corre con prioridad "por debajo de lo normal" (Windows atiende primero a Teams, navegador, etc.). Sin GPU usa por defecto la mitad de los núcleos (⚙ → "Uso del procesador": Ligero / Equilibrado / Máximo, o `--threads N`). En las pruebas: Turbo en CPU ~20 % del procesador de media; Parakeet ~9 %.

## Notas
- **[Otros]** es todo lo que suena por la salida elegida (Teams, Meet, Zoom…). **[Yo]** es el micrófono elegido.
- Con el micrófono activo, usa audífonos; si no, el mic capta a los demás y el texto sale duplicado.
- No distingue entre las distintas personas remotas: todas quedan como [Otros].

## Carpeta de guardado y sesiones

En ⚙ → **Carpeta de transcripciones** puedes escribir una ruta o usar **Examinar…** en la ventana nativa. **Aplicar** comprueba que se pueda escribir y guarda la preferencia; **Restablecer** vuelve a `transcripciones/`. En navegador se introduce la ruta manualmente. **Abrir carpeta** abre el destino configurado. Los cambios se aplican a la siguiente sesión: nunca trasladan el archivo actual.

- **Iniciar** crea un TXT exclusivo. Si el nombre ya existe, añade un número sin sobrescribirlo.
- **Pausar** detiene la captura y procesa lo pendiente. Después, **Reanudar** continúa en el mismo TXT; las marcas de tiempo excluyen la pausa. Ambas acciones quedan anotadas.
- **Terminar** procesa todo el audio pendiente y cierra el TXT, también si estabas en pausa.
- **Nueva sesión** limpia la pantalla y restablece el título a «Reunión». Pulsa **Iniciar** para grabar en otro archivo sin cerrar el programa. El archivo anterior se conserva.

Durante el procesamiento se muestra «Pausando…» o «Terminando…» y se bloquean las acciones incompatibles. Modelo, idioma y uso del procesador se mantienen hasta terminar. La sesión finalizada permanece visible y se puede copiar o abrir su TXT; las sesiones nuevas también se pueden reabrir desde su archivo `session.json` (ver abajo).

## Widget y gestor de sesiones

La aplicación tiene dos ventanas independientes:

- **Widget:** texto en vivo, fuentes, pausa/reanudación, terminar, marcar momento (⚑), discreción (◉) y acceso a **Sesiones** (▤). Mantiene sus ajustes locales de captura. Abrir o analizar otra conversación no modifica la grabación activa.
- **Sesiones:** ventana amplia de 1100 × 760 con listado, búsqueda por título, actualización y apertura de archivos externos. El detalle ofrece pestañas **Transcripción**, **Marcadores**, **Análisis** y **Palabras sospechosas**. En navegador se abre `/manager` en una pestaña con nombre para reutilizarla.

El listado consulta la carpeta configurada y las sesiones abiertas explícitamente. Incluye los TXT anteriores como sesiones de solo lectura, sin audio ni contexto; interpreta sus marcas temporales cuando existen. Los manifiestos incompatibles y archivos ausentes generan avisos. No se migran ni reescriben conversaciones al listarlas o abrirlas.

### Preparar la próxima sesión

Desde el gestor, **Preparar sesión** permite establecer título, contexto, respaldo y formato de análisis, elegir las fuentes y comprobar el audio. Guarda la preparación e inicia desde el widget; preparar cada reunión no es obligatorio.

- **Probar audio:** muestra nombres, niveles y estados independientes (señal, silencio, apagado o error). El silencio no es un fallo. No carga modelos, transcribe ni guarda audio. Se detiene al cerrar la preparación, cambiar una fuente o iniciar, y termina automáticamente después de un minuto.
- **Contexto:** hasta 2.000 caracteres. El texto que exceda el límite permanece visible y bloquea el guardado. Whisper lo recibe junto con el contexto reciente y conserva las palabras del diccionario; Parakeet lo guarda como referencia. Se fija al iniciar, se consume una sola vez y no se añade como intervención al TXT. Se puede consultar en el detalle de la sesión.
- **Guardar respaldo:** desactivado inicialmente; recuerda la última elección. Guarda las fuentes habilitadas, incluidos los silencios y excluyendo pausas. No se puede cambiar durante la grabación.
- **Formato:** Reunión, Entrevista o Clase. Empieza en Reunión, recuerda la elección y la fija con la sesión. Al regenerar el análisis puedes escoger otro formato.

Cada sesión nueva conserva el TXT habitual y una carpeta del mismo nombre con extensión `.session`. Contiene `session.json` (formato 2, compatible en lectura con formato 1) y, si activaste respaldo, `audio-0001.wav`, etc. El audio usa PCM de 16 kHz y 16 bits, con «Yo» en el canal izquierdo y «Otros» en el derecho; una fuente apagada ocupa silencio. Las partes duran hasta 30 minutos, unos 115 MB; una hora ocupa unos 230 MB. No se elimina audio automáticamente.

### Revisar texto, audio y marcadores

**Marcar momento** en el widget toma el tiempo de captura, aunque el motor lleve atraso. En el gestor puedes navegar, renombrar o eliminar marcadores; las anotaciones se reflejan también en el TXT.

Al terminar, el reproductor permite escuchar, cambiar volumen y velocidad y recorrer las partes de audio. Pulsar un bloque o marcador lleva a su momento. La sincronización es por bloque, no por palabra; ambas fuentes pueden resaltarse simultáneamente. La reproducción se bloquea mientras otra sesión capture el audio del equipo, y se pausa al activar discreción o cambiar de pestaña de contenido.

**Abrir archivo** acepta `session.json` y TXT: selector nativo en Windows y ruta manual en navegador. No cambia la sesión del widget. Para trasladar una sesión conserva juntos el TXT y su carpeta `.session`. Si falta audio, el texto sigue disponible; si falla el respaldo se muestra como incompleto sin detener por sí mismo la transcripción.

## Análisis mediante una API compatible

En el gestor, abre **Configuración** y guarda la URL base (por ejemplo, `https://api.openai.com/v1`), identificador del modelo y clave. La clave se asocia a la URL y se almacena explícitamente mediante Windows Credential Manager; nunca se devuelve a la interfaz ni se escribe en JSON o registros. Dejar el campo vacío conserva la clave guardada para ese destino. El almacenamiento seguro requiere Windows; no se usa una alternativa en texto plano.

**Probar conexión guardada** envía una solicitud mínima y sintética, sin conversaciones, al endpoint `/chat/completions`. Esa prueba puede consumir cuota del proveedor. No verifica capacidades adicionales. Si tu modelo exige `max_completion_tokens`, selecciónalo en las opciones avanzadas; el contrato general utiliza `max_tokens`. Un rechazo de parámetros o una respuesta no compatible se muestra como error, sin reintentos automáticos.

**Analizar automáticamente las nuevas sesiones al terminar** está apagado inicialmente. Al activarlo autorizas enviar al proveedor configurado el texto, contexto y marcadores de las nuevas sesiones terminadas; el audio no se envía. No se analizan automáticamente reuniones anteriores. El acceso y cobro de la API son independientes de una suscripción a ChatGPT.

| Formato | Contenido |
|---|---|
| Reunión | Resumen, temas, decisiones, compromisos y asuntos pendientes |
| Entrevista | Temas, preguntas y respuestas relevantes, afirmaciones y puntos por aclarar |
| Clase | Resumen, conceptos, explicaciones, ejemplos y dudas |

Los prompts incluidos piden referencias temporales, separan hechos de inferencias, evitan inventar participantes, responsables o fechas y tratan la transcripción como material, no como instrucciones. El resultado sigue siendo un análisis generado que conviene revisar; no modifica el texto original ni ejecuta tareas externas.

El análisis se encola después de guardar todo el texto, no al pausar. Un único trabajador procesa la cola sin impedir otra grabación. Primero extrae notas compactas por secciones y después las consolida por niveles para producir una **Síntesis**. **Detalle por secciones** conserva las notas parciales, con referencias para ir al texto y al audio cuando exista. No recorta silenciosamente la transcripción ni acepta respuestas que el proveedor declare truncadas. Si un nivel no reduce las notas, intenta una compactación adicional acotada; si tampoco funciona, se detiene y conserva los avances.

En **Límites y tarifas opcionales**, configura los límites reales del proveedor. Los valores iniciales son una ventana de contexto de **8.192 tokens**, salida máxima de **2.000**, presupuestos de **384** para extracción y consolidación y **1.200** para síntesis final, tope adicional de **12.000 caracteres** por sección, **20 llamadas totales** y **120 segundos** por solicitud. No se deducen capacidades del nombre del modelo. El cálculo incluye instrucciones, contexto, marcadores y salida reservada, dejando un **15 % de margen**. El contexto que no cabe produce un error antes de enviar solicitudes; no se omite.

Por defecto se utiliza una **estimación conservadora UTF-8**, contando bytes como tokens más una reserva para el formato de mensajes. Puede producir más partes y llamadas que un conteo exacto. Opcionalmente puedes instalar `tiktoken` y preparar su caché de codificaciones, y seleccionar explícitamente `cl100k_base` u `o200k_base` solo si corresponde al modelo. La primera preparación de esa caché puede necesitar red; UTF-8 funciona sin esa dependencia. Ninguna estimación garantiza que un proveedor con formato o contabilidad propios acepte la solicitud.

Los límites locales opcionales de **solicitudes/minuto** y **tokens/minuto** se aplican en una ventana móvil de 60 segundos, compartida entre los trabajos. La aplicación reserva entrada y salida antes de transmitir y ajusta la reserva con el consumo informado. Mientras no haya cupo, muestra **Esperando cuota** con una espera aproximada. Un HTTP 429 detiene el trabajo sin repetirlo; si el proveedor indica `Retry-After`, se conserva esa espera para la continuación explícita. Las cuotas del proveedor siguen siendo la autoridad y pueden incluir límites adicionales.

Estados: **Pendiente**, **Analizando**, **Completado**, **Error** e **Interrumpido**, además de la fase **Esperando cuota**. **Continuar análisis** reutiliza las etapas compatibles del mismo trabajo, incluso después de reiniciar. Puedes aumentar el máximo total o ajustar cuotas sin repetir las partes completadas; las llamadas ya realizadas siguen contando para ese máximo. Cambiar los presupuestos de salida invalida únicamente las etapas cuyo contenido o salida solicitada cambien. Cambiar texto, plantilla, proveedor, modelo, instrucciones, contador o configuración de entrada exige regenerar. **Regenerar** comienza un trabajo nuevo; conserva el último resultado completo hasta que el nuevo termine.

El resultado se guarda en `analysis.json` dentro de `.session`; para un TXT antiguo, en un archivo vecino `.analysis.json`. Incluye fecha, modelo, versión del prompt, detalle por secciones y consumo comunicado por el proveedor. Solo se calcula una estimación en USD si introduces ambas tarifas por millón de tokens y todas las llamadas devuelven el consumo necesario. Si falta información de consumo, se señala como incompleta. Los análisis antiguos siguen siendo legibles, pero no ofrecen etapas que nunca se guardaron.

La cola y sus etapas versionadas se guardan atómicamente en `state/analysis-queue.json`; incluyen huellas de las entradas, dependencias, resultados y reservas de cuota. Los archivos abiertos explícitamente se registran en `state/session-index.json`; ambos están excluidos de Git. Los trabajos pendientes se recuperan al iniciar, incluso si cambiaste la carpeta de salida. Los que estaban en vuelo quedan interrumpidos: una solicitud enviada podría haber consumido cuota y no se repite automáticamente. Continuar una solicitud incierta requiere confirmar ese posible consumo duplicado. Cuando no se puede reconstruir el consumo reciente de un trabajo antiguo interrumpido, se reserva una espera conservadora de 60 segundos. Abrir una sesión o consultar sus resultados nunca inicia análisis.

Las APIs nuevas son `GET /analysis-jobs/{id}/stages` y `POST /analysis-jobs/{id}/continue` (con `acknowledge_uncertain` cuando corresponda). La configuración del perfil amplía `/analysis-settings`; los eventos de análisis incluyen etapa, progreso y espera. La prueba con un proveedor real sigue siendo necesaria para validar sus límites y compatibilidad; las regresiones usan únicamente servidores y conversaciones sintéticos.

## Bandeja y cierre

En Windows la bandeja ofrece **Mostrar widget**, **Abrir sesiones**, estado del trabajo y **Salir**. Cada ventana tiene una sola instancia. Cerrar el gestor lo oculta. Cerrar el widget termina la captura y procesa todo lo pendiente, pero deja la aplicación funcionando para completar el análisis. Las notificaciones no incluyen contenido de la conversación.

**Salir** detiene la captura, guarda la transcripción pendiente y cierra el proceso. Conserva pendientes para la próxima ejecución y marca como interrumpida una solicitud sin resultado guardado. En navegador no hay bandeja: el backend trabaja mientras su proceso permanezca abierto.

## Discreción al compartir pantalla

El botón **◉** del widget y **Ocultar contenido** del gestor comparten estado: ocultan texto, título, contexto, marcadores, resultados, rutas y paneles en ambas ventanas. La captura, el guardado y el análisis continúan; la reproducción se pausa y se bloquea la copia desde la interfaz. El estado se conserva al reconectar, recargar o crear una sesión dentro de la misma ejecución.

La activación es manual. No detecta pantallas compartidas ni excluye las ventanas de capturas.

## Validación de desarrollo

`python -m unittest -v` ejecuta pruebas aisladas de captura, persistencia, consulta independiente, credenciales simuladas y análisis contra un proveedor HTTP local. No usa modelos, conversaciones ni claves reales.

Con Playwright y Edge instalados, `python tools/validate_session_ui.py` recorre widget y gestor en navegador. `python tools/validate_session_ui.py --native` comprueba las dos ventanas reales WebView2 y la bandeja de Windows. Ambas variantes usan dispositivos y proveedor sintéticos, con datos en carpetas temporales; al terminar informan la ubicación de las capturas de evidencia.

La compatibilidad con un proveedor y modelo reales queda pendiente hasta configurarlos y ejecutar expresamente la prueba de conexión. Ninguna validación de desarrollo envía conversaciones existentes.
