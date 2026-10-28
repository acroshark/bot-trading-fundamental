#!/bin/bash
# Prepara una sesión de Claude en la nube (Linux, claude.ai/code): instala las dependencias para poder pasar las pruebas.
# Lo lanza el gancho SessionStart de .claude/settings.json. En el PC del usuario (sin CLAUDE_CODE_REMOTE) no hace nada.
[ "$CLAUDE_CODE_REMOTE" = "true" ] || exit 0
cd "${CLAUDE_PROJECT_DIR:-.}" || exit 0
python3 -m pip install -q -r requirements.txt || echo "Aviso: no se pudieron instalar todas las dependencias (¿red del entorno en «Ninguna»?)" >&2
exit 0
