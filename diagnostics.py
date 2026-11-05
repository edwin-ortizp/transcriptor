"""Read-only health checks for restricted machines. Reports never include keys or session data."""
import importlib
import sys
import tempfile
from pathlib import Path

DEPENDENCIES = (
    ('spylls', 'Palabras sospechosas'),
    ('keyring', 'Almacén de claves de Windows'),
    ('av', 'Compresión de audio'),
    ('faster_whisper', 'Transcripción con Whisper'),
    ('onnxruntime', 'Transcripción con modelos ONNX'),
)
PROBE_SERVICE = 'Transcriptor.diagnostics'
PROBE_USER = 'probe'


def install_command():
    # pythonw has no console, so the suggested command must use its python.exe sibling.
    executable = Path(sys.executable)
    python = executable.with_name('python.exe') if executable.stem.lower() == 'pythonw' else executable
    return f'"{python}" -m pip install --user -r requirements.txt'


def reason(exc):
    # Only the type and Windows code: messages can contain paths or user data.
    code = getattr(exc, 'winerror', None)
    return type(exc).__name__ + (f' (código {code})' if code else '')


def result(check, label, affects, state='ok', detail='', action=''):
    return {'id':check, 'label':label, 'affects':affects, 'state':state, 'ok':state == 'ok',
            'detail':detail, 'action':action}


def check_dependency(module, affects, importer=importlib.import_module):
    check = f'dependency:{module}'
    try:
        importer(module)
    except ModuleNotFoundError as exc:
        # A nested missing module still means this feature needs a reinstall.
        return result(check, module, affects, 'missing', reason(exc),
                      f'Instala las dependencias con: {install_command()}')
    except Exception as exc:
        return result(check, module, affects, 'blocked', reason(exc),
                      'Está instalada pero no carga. Reinstálala; si sigue igual, puede estar bloqueada por una política del equipo.')
    return result(check, module, affects)


def check_dictionaries(load):
    affects = 'Palabras sospechosas'
    try:
        load()
    except ModuleNotFoundError as exc:
        return result('dictionaries', 'Diccionarios', affects, 'missing', reason(exc),
                      f'Instala las dependencias con: {install_command()}')
    except Exception as exc:
        return result('dictionaries', 'Diccionarios', affects, 'error', reason(exc),
                      'Revisa que la carpeta dictionaries esté completa y se pueda leer.')
    return result('dictionaries', 'Diccionarios', affects)


def check_writable(folder):
    affects = 'Guardar sesiones y revisiones'
    try:
        with tempfile.NamedTemporaryFile(dir=folder(), prefix='.transcriptor-diagnostics-'):
            pass
    except Exception as exc:
        return result('sessions_folder', 'Carpeta de sesiones', affects, 'blocked', reason(exc),
                      'Elige otra carpeta o revisa sus permisos.')
    return result('sessions_folder', 'Carpeta de sesiones', affects)


def windows_vault():
    from keyring.backends.Windows import WinVaultKeyring
    return WinVaultKeyring()


def check_vault(backend=windows_vault):
    affects = 'Guardar la clave del proveedor'
    label = 'Almacén de claves de Windows'
    store = None
    try:
        store = backend()
        store.set_password(PROBE_SERVICE, PROBE_USER, 'diagnostico')
        if store.get_password(PROBE_SERVICE, PROBE_USER) != 'diagnostico':
            raise ValueError('lectura distinta')
    except ModuleNotFoundError as exc:
        return result('vault', label, affects, 'missing', reason(exc),
                      f'Instala las dependencias con: {install_command()}')
    except Exception as exc:
        return result('vault', label, affects, 'blocked', reason(exc),
                      'Windows no permite guardar la clave; puede ser una política del equipo.')
    finally:
        if store is not None:
            try:
                store.delete_password(PROBE_SERVICE, PROBE_USER)
            except Exception:
                pass
    return result('vault', label, affects)


def run(load_dictionaries, sessions_folder, importer=importlib.import_module, vault=windows_vault):
    checks = [check_dependency(module, affects, importer) for module, affects in DEPENDENCIES]
    checks += [check_dictionaries(load_dictionaries), check_writable(sessions_folder), check_vault(vault)]
    return {'ok':all(item['ok'] for item in checks), 'checks':checks}
