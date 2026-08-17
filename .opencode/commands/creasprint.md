---
description: Crea un sprint en Phorge desde un archivo local tras seleccionar proyecto y confirmar la publicación Phriction.
agent: build
---

Ejecuta `/creasprint $1` siguiendo estrictamente este flujo:

1. Valida que `$1` esté presente y no esté vacío. Si falta, detente e indica el uso: `/creasprint <ruta-relativa>`.
2. Trata `$1` exclusivamente como una ruta relativa al workspace. Rechaza rutas absolutas y cualquier ruta cuya normalización salga del workspace.
3. Comprueba que la ruta exista y sea un archivo. Si no existe o no es un archivo, detente sin llamar a Phorge.
4. Carga la skill `phorge-conduit` y lee íntegramente `references/sprints.md` desde su directorio.
5. Extrae de esa referencia todos los proyectos configurados y los mapeos `@usuario: Exact Sprint Tag` existentes. Rechaza la operación si la configuración de proyectos está vacía, incompleta o contiene `<CONFIGURAR>`. El mapa de responsables puede estar vacío; la tool validará que exista un mapping para cada owner que se asigne o reasigne. No intentes descubrir ni completar valores con Phorge.
6. Usa la herramienta `question` para mostrar los nombres visibles de los proyectos válidos y solicitar que el usuario seleccione exactamente uno. No elijas por defecto ni deduzcas el proyecto.
7. Lee el archivo fuente completo, sin truncarlo, y conserva literalmente su texto como `source_text`.
8. Presenta antes de mutar:
   - archivo fuente y ruta relativa normalizada;
   - proyecto seleccionado;
   - `wikiBasePath` de destino;
   - `defaultTag` que se aplicará;
   - acción: crear o actualizar las tareas del sprint y publicar su documento Phriction.
   - contenido Markdown fuente completo, indicando que será la base del contenido Phriction generado por la tool.
9. Después de la selección del proyecto y de presentar esos datos, usa `question` para pedir confirmación Phriction explícita. Ofrece una opción inequívoca como `Confirmar creación del sprint` y otra para cancelar. Una respuesta ambigua, distinta de la opción afirmativa o la cancelación detiene la operación.
10. Solo tras esa confirmación, llama exactamente una vez a la herramienta de negocio `phorge_create_sprint`, sin reintentos, con únicamente estos argumentos:
    - `source_path`: ruta relativa normalizada;
    - `source_text`: contenido completo y literal del archivo;
    - `project_config`: configuración completa del proyecto seleccionado, con `name`, `wikiBasePath` y `defaultTag`;
    - `owner_sprint_tags`: mapa completo configurado en la referencia, convirtiendo cada línea `@usuario: Exact Sprint Tag` en usuario y nombre exacto de tag.
11. Nunca llames manualmente a herramientas Phorge de nivel inferior para buscar, validar, crear, editar, mover o publicar partes del sprint. `phorge_create_sprint` encapsula toda la operación. Si la herramienta falla, informa del fallo y no la vuelvas a llamar.
12. Muestra el resultado de forma estructurada usando los campos devueltos por la herramienta. Separa como mínimo: estado global, proyecto y destino Phriction, tareas creadas, tareas actualizadas o reutilizadas, relaciones/columnas aplicadas, avisos y errores. No inventes campos ausentes; indica `No informado` cuando corresponda.

No solicites confirmación antes de seleccionar el proyecto. La confirmación afirmativa es válida únicamente para el destino y la acción presentados inmediatamente antes de la llamada única.
