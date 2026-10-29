# Vocabularios del sistema

Estos archivos son datos compartidos de la aplicación, versionados en el repo y de solo lectura desde la interfaz. No contienen mappings personales y no producen reemplazos automáticos.

## Español e inglés

`es/` y `en/` conservan los archivos Hunspell originales de [wooorm/dictionaries](https://github.com/wooorm/dictionaries), sus licencias, README y metadatos de paquete. `sources.json` registra el commit exacto, la versión de cada paquete, la URL, el tamaño y el SHA-256 de cada archivo incorporado. Los archivos del proveedor no se han modificado. `.gitattributes` desactiva su normalización de saltos de línea para conservar los mismos bytes y checksums en cada checkout.

La consulta de la interfaz muestra las entradas léxicas de `.dic`, sin la línea inicial de conteo, los indicadores después de `/` ni la información morfológica. Los archivos `.aff` se conservan completos: la vista de términos no expande sus afijos. La búsqueda de palabras sospechosas sí consulta esas reglas mediante `spylls`. Las listas no son exhaustivas para todas las flexiones, conjugaciones o variantes regionales. No encontrar un término aquí no demuestra que sea incorrecto.

La aplicación lee estos datos localmente. No consulta ni descarga vocabularios durante el uso.

Para actualizarlos, escoger un commit del proveedor, incorporar juntos `.dic`, `.aff`, `license`, `readme.md` y `package.json`, actualizar `sources.json` y ejecutar `python -m unittest -v test_dictionary`. Conservar las licencias y la procedencia de cada idioma.

## Términos propios

`terms.tsv` comienza vacío, salvo su comentario de cabecera. Cada entrada utiliza dos o tres columnas separadas por tabulaciones:

```text
término<TAB>idioma<TAB>nota opcional
```

El idioma debe ser `es`, `en` o `both`. Las líneas vacías y las que comienzan por `#` se ignoran. Guardar en UTF-8; respetar la grafía y los acentos deseados. Los cambios se realizan mediante el repo y se cargan al reiniciar la aplicación.

Un término propio indica vocabulario de referencia. No es una regla de reemplazo ni una traducción. Para corregir una forma reconocida se utiliza el archivo TSV personal de mappings, separado de esta carpeta.

El vocabulario personal se guarda fuera de esta carpeta, en `%LOCALAPPDATA%\Transcriptor\terms.tsv`. No se incorpora al repo y no sustituye el glosario compartido.
